"""FR-67, FR-68 (M17): the ADR-06 budget governor.

A run's ceiling is split once, at plan time: an orchestrator reserve, an unallocated
reserve, and a reservation for each node of a `PlanVersion`. The planner proposes each
node's reservation; a proposal above `reservation_cap_fraction` of what is still
unallocated to nodes is capped to it, and the nodes that propose nothing split what is
left equally. Additional allocation comes only from the unallocated reserve, through
`top_up`, which is a deterministic call by the orchestrator and never a model's request
(DECISION-e1bf0327).

Enforcement is soft, and lives in `BudgetLease.may_call()`, which the agent loop asks
before each model call. An agent at or over its reservation makes no further call, so it
can overshoot by at most one call, and the run by at most one call per agent running at
once; that overshoot is charged to the run rather than hidden. Money in Decimal, never
float, because a spend summed across thousands of calls must not drift.

Nothing drives plan nodes until M19, so M17's governor is handed to runs through
`RunConfig.budget_lease` by whatever acts as the orchestrator; budgets for plain
single-agent runs stay out of scope (backlog I-04, DECISION-c274eb02).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal, DecimalException, localcontext
from types import MappingProxyType
from typing import Any, Mapping

from agentsdk.errors import BudgetExceeded
from agentsdk.model import Usage

__all__ = [
    "BudgetAllocation",
    "BudgetAmount",
    "BudgetGovernor",
    "BudgetLease",
    "BudgetPolicy",
    "tokens_of",
]

_INT_CEILING = 2**63 - 1
# Money is allocated in steps of this size, so that equal shares of a pool that
# does not divide exactly still add back up to it (F6). Smaller than any price.
_USD_PLACES = 18
_USD_STEP = Decimal(1).scaleb(-_USD_PLACES)


def _money(name: str, value: Any, *, allow_zero: bool = False) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, Decimal):
        raise ValueError(f"{name} must be a Decimal, got {type(value).__name__}")
    if not value.is_finite():
        raise ValueError(f"{name} must be a finite amount, got {value}")
    if value < 0 or (value == 0 and not allow_zero):
        raise ValueError(f"{name} must be {'at least' if allow_zero else 'above'} 0, got {value}")
    return value


def _tokens(name: str, value: Any, *, allow_zero: bool = False) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise ValueError(f"{name} must be an int, got {type(value).__name__}")
    if value < 0 or (value == 0 and not allow_zero):
        raise ValueError(f"{name} must be {'at least' if allow_zero else 'above'} 0, got {value}")
    if value > _INT_CEILING:
        raise ValueError(f"{name} must be at most {_INT_CEILING}, got {value}")
    return value


def _fraction(name: str, value: Any) -> Decimal:
    if not isinstance(value, Decimal):
        raise ValueError(f"{name} must be a Decimal, got {type(value).__name__}")
    if not value.is_finite() or not (0 < value < 1):
        raise ValueError(f"{name} must be above 0 and below 1, got {value}")
    return value


@dataclass(frozen=True, kw_only=True)
class BudgetAmount:
    """An amount in whichever units have a ceiling. None is "no ceiling in this unit",
    and for a spend it is "not known", which is never the same as zero (NFR-11).

    This type RECORDS an amount; it does not police one. A ceiling, a reservation and a
    top-up are configuration and are checked for sign and range where they are set. A
    spend is whatever happened, and a provider can report anything `Usage` can hold: a
    reading that refused those values made a run die inside its own terminal path
    (round 3, H1).
    """

    usd: Decimal | None = None
    tokens: int | None = None

    def __post_init__(self) -> None:
        if self.usd is not None and (not isinstance(self.usd, Decimal) or not self.usd.is_finite()):
            raise ValueError(f"usd must be a finite Decimal, got {self.usd!r}")
        if self.tokens is not None and type(self.tokens) is not int:
            raise ValueError(f"tokens must be an int, got {type(self.tokens).__name__}")


@dataclass(frozen=True)
class BudgetAllocation:
    """What the plan-time split decided (FR-67)."""

    orchestrator: BudgetAmount
    unallocated: BudgetAmount
    reservations: Mapping[str, BudgetAmount]


@dataclass(frozen=True, kw_only=True)
class BudgetPolicy:
    """FR-67, with P2-D19's figures as its defaults."""

    run_ceiling_usd: Decimal | None = None
    run_ceiling_tokens: int | None = None
    orchestrator_reserve_fraction: Decimal = Decimal("0.20")
    unallocated_reserve_fraction: Decimal = Decimal("0.20")
    reservation_cap_fraction: Decimal = Decimal("0.40")
    # FR-75's limit on how many times one run may replan. M19 enforces it.
    max_replans: int = 3

    def __post_init__(self) -> None:
        _money("run_ceiling_usd", self.run_ceiling_usd)
        _tokens("run_ceiling_tokens", self.run_ceiling_tokens)
        if self.run_ceiling_usd is None and self.run_ceiling_tokens is None:
            raise ValueError("a policy must set run_ceiling_usd, run_ceiling_tokens or both")
        for name in ("orchestrator_reserve_fraction", "unallocated_reserve_fraction", "reservation_cap_fraction"):
            _fraction(name, getattr(self, name))
        if self.orchestrator_reserve_fraction + self.unallocated_reserve_fraction >= 1:
            raise ValueError(
                "orchestrator_reserve_fraction and unallocated_reserve_fraction must leave something for the nodes"
            )
        if type(self.max_replans) is not int or self.max_replans < 0:
            raise ValueError(f"max_replans must be an int of 0 or more, got {self.max_replans!r}")


def _split_usd(policy: BudgetPolicy, nodes: tuple[Any, ...]) -> tuple[Decimal, Decimal, dict[str, Decimal]] | None:
    ceiling = policy.run_ceiling_usd
    if ceiling is None:
        return None
    try:
        with localcontext() as context:
            # Wide enough that a large ceiling still quantises to _USD_PLACES: the
            # default 28 digits raised a raw decimal error at 1e11 (round 3, H4).
            context.prec = 80
            return _split_usd_within(policy, nodes, ceiling)
    except DecimalException as exc:
        raise ValueError(f"run_ceiling_usd cannot be split: {type(exc).__name__}") from None


def _split_usd_within(
    policy: BudgetPolicy, nodes: tuple[Any, ...], ceiling: Decimal
) -> tuple[Decimal, Decimal, dict[str, Decimal]]:
    orchestrator = ceiling * policy.orchestrator_reserve_fraction
    unallocated = ceiling * policy.unallocated_reserve_fraction
    reservations, pool = _grant_usd(policy, nodes, ceiling - orchestrator - unallocated)
    return orchestrator, unallocated + pool, reservations


def _grant_usd(policy: BudgetPolicy, nodes: tuple[Any, ...], pool: Decimal) -> tuple[dict[str, Decimal], Decimal]:
    """FR-67's per-node rule over `pool`: what each node is granted, and what is left."""
    reservations: dict[str, Decimal] = {}
    unproposed = []
    for node in nodes:
        proposed = None if node.budget_reservation is None else node.budget_reservation.usd
        if proposed is None:
            unproposed.append(node.node_id)
            continue
        # The cap is read against what is still unallocated to nodes when this
        # proposal is taken, so no single node can starve the ones after it.
        granted = min(proposed, pool * policy.reservation_cap_fraction)
        reservations[node.node_id] = granted
        pool -= granted
    if unproposed:
        # An equal share that does not terminate -- a pool of 6 over 7 nodes -- cannot
        # be both exact and equal at Decimal's 28 digits. The share is rounded DOWN to
        # _USD_PLACES, so every node gets the same amount and no node gets a fraction
        # of a cent more than the pool holds; what is left over, at most one unit in
        # the last place per node, goes to the unallocated reserve. The split then sums
        # to the ceiling exactly, in any order a consumer adds it up (F6).
        each = (pool / len(unproposed)).quantize(_USD_STEP, rounding=ROUND_DOWN)
        for node_id in unproposed:
            reservations[node_id] = each
        pool -= each * len(unproposed)
    return reservations, pool


def _split_tokens(policy: BudgetPolicy, nodes: tuple[Any, ...]) -> tuple[int, int, dict[str, int]] | None:
    ceiling = policy.run_ceiling_tokens
    if ceiling is None:
        return None
    orchestrator = int(Decimal(ceiling) * policy.orchestrator_reserve_fraction)
    unallocated = int(Decimal(ceiling) * policy.unallocated_reserve_fraction)
    reservations, pool = _grant_tokens(policy, nodes, ceiling - orchestrator - unallocated)
    return orchestrator, unallocated + pool, reservations


def _grant_tokens(policy: BudgetPolicy, nodes: tuple[Any, ...], pool: int) -> tuple[dict[str, int], int]:
    """FR-67's per-node rule over `pool`, in tokens."""
    reservations: dict[str, int] = {}
    unproposed = []
    for node in nodes:
        proposed = None if node.budget_reservation is None else node.budget_reservation.tokens
        if proposed is None:
            unproposed.append(node.node_id)
            continue
        granted = min(proposed, int(Decimal(pool) * policy.reservation_cap_fraction))
        reservations[node.node_id] = granted
        pool -= granted
    if unproposed:
        each = pool // len(unproposed)
        for node_id in unproposed:
            reservations[node_id] = each
        # Whatever integer division could not divide stays unallocated.
        pool -= each * len(unproposed)
    return reservations, pool


class BudgetGovernor:
    """One run's budget: the plan-time split, the leases, the spend and the reclaim."""

    def __init__(self, policy: BudgetPolicy, plan: Any) -> None:
        if not isinstance(policy, BudgetPolicy):
            raise ValueError(f"policy must be a BudgetPolicy, got {type(policy).__name__}")
        nodes = getattr(plan, "nodes", None)
        if not nodes:
            raise ValueError("a governor needs a PlanVersion with at least one node")
        self.policy = policy
        self.plan = plan
        self._lock = threading.Lock()
        usd = _split_usd(policy, nodes)
        tokens = _split_tokens(policy, nodes)
        self._orchestrator = BudgetAmount(
            usd=None if usd is None else usd[0], tokens=None if tokens is None else tokens[0]
        )
        self._unallocated_usd = None if usd is None else usd[1]
        self._unallocated_tokens = None if tokens is None else tokens[1]
        self._reservations: dict[str, list[Any]] = {
            node.node_id: [
                None if usd is None else usd[2][node.node_id],
                None if tokens is None else tokens[2][node.node_id],
            ]
            for node in nodes
        }
        self._leases: dict[str, BudgetLease] = {}
        self._spent_usd: Decimal | None = Decimal(0)
        self._spent_tokens = 0
        # The records of nodes a replan retired (M19): kept, so the allocation still sums
        # to the ceiling, and never funded or retired again.
        self._kept: set[str] = set()

    @classmethod
    def for_run(cls, policy: BudgetPolicy) -> BudgetGovernor:
        """A governor for an orchestrator run, before it has a plan (M19, FR-73).

        The orchestrator reserve is set aside now, because the run's first planning call
        is counted before any plan exists; everything else is unallocated until `adopt`.
        """
        if not isinstance(policy, BudgetPolicy):
            raise ValueError(f"policy must be a BudgetPolicy, got {type(policy).__name__}")
        self = cls.__new__(cls)
        self.policy, self.plan, self._lock = policy, None, threading.Lock()
        usd, tokens = policy.run_ceiling_usd, policy.run_ceiling_tokens
        with localcontext() as context:
            context.prec = 80  # as _split_usd, so a large ceiling still splits (round 3, H4)
            orchestrator_usd = None if usd is None else usd * policy.orchestrator_reserve_fraction
        orchestrator_tokens = None if tokens is None else int(Decimal(tokens) * policy.orchestrator_reserve_fraction)
        self._orchestrator = BudgetAmount(usd=orchestrator_usd, tokens=orchestrator_tokens)
        self._unallocated_usd = None if usd is None else usd - orchestrator_usd
        self._unallocated_tokens = None if tokens is None else tokens - orchestrator_tokens
        self._reservations, self._leases, self._kept = {}, {}, set()
        self._spent_usd, self._spent_tokens = Decimal(0), 0
        return self

    def adopt(self, plan: Any, *, carried: tuple[str, ...] = ()) -> None:
        """Fund a plan version's nodes from what this run has left (FR-67, FR-75).

        The first plan is split exactly as the constructor splits it. A replan keeps
        every spend and every node in `carried` -- done in the version before and
        carried into this one -- and splits what is unallocated, above the unallocated
        reserve, among the new version's other nodes by the same rule, so replanning
        stays inside the run ceiling (FR-68, DECISION-c8d0932a). A node of the old
        version that ran keeps its record under a new key, `node@vN` or the first free
        `node@vN#k`; one that never started gives its reservation back. A new node whose
        id is already one of those records is refused, so no spend is ever written over
        (M19 round 1, D4). Everything is checked before anything moves.
        """
        nodes = getattr(plan, "nodes", None)
        if not nodes:
            raise ValueError("a governor needs a PlanVersion with at least one node")
        with self._lock:
            before = None if self.plan is None else self.plan.version
            leaving = [n for n in self._reservations if n != _ORCHESTRATOR and n not in carried and n not in self._kept]
            for node_id in leaving:
                lease = self._leases.get(node_id)
                if lease is not None and not lease._released:
                    raise ValueError(f"node {node_id!r} is still running and cannot be replanned")
            funded = tuple(n for n in nodes if n.node_id not in carried)
            taken = {_ORCHESTRATOR, *self._kept}
            clash = [n.node_id for n in funded if n.node_id in taken]
            if clash:
                raise ValueError(
                    f"node_id {clash[0]!r} is already a budget record of this run; give the node another id"
                )
            funded_ids = {n.node_id for n in funded}
            for node_id in leaving:
                lease = self._leases.pop(node_id, None)
                amount = self._reservations.pop(node_id)
                if lease is None:
                    if amount[0] is not None and self._unallocated_usd is not None:
                        self._unallocated_usd += amount[0]
                    if amount[1] is not None and self._unallocated_tokens is not None:
                        self._unallocated_tokens += amount[1]
                    continue
                kept, k = f"{node_id}@v{before}", 1
                while kept in self._reservations or kept in funded_ids or kept in leaving:
                    k += 1
                    kept = f"{node_id}@v{before}#{k}"
                self._reservations[kept] = amount
                self._leases[kept] = lease
                self._kept.add(kept)
            if self.policy.run_ceiling_usd is not None:
                with localcontext() as context:
                    context.prec = 80
                    reserve = self.policy.run_ceiling_usd * self.policy.unallocated_reserve_fraction
                    pool = max(Decimal(0), self._unallocated_usd - reserve)
                    granted, left = _grant_usd(self.policy, funded, pool)
                    self._unallocated_usd = self._unallocated_usd - pool + left
            else:
                granted = {}
            if self.policy.run_ceiling_tokens is not None:
                reserve_tokens = int(Decimal(self.policy.run_ceiling_tokens) * self.policy.unallocated_reserve_fraction)
                pool_tokens = max(0, self._unallocated_tokens - reserve_tokens)
                granted_tokens, left_tokens = _grant_tokens(self.policy, funded, pool_tokens)
                self._unallocated_tokens = self._unallocated_tokens - pool_tokens + left_tokens
            else:
                granted_tokens = {}
            for node in funded:
                self._reservations[node.node_id] = [granted.get(node.node_id), granted_tokens.get(node.node_id)]
            self.plan = plan

    # --- what the split decided ---------------------------------------------------------------

    @property
    def allocation(self) -> BudgetAllocation:
        with self._lock:
            held = self._reservations.get(_ORCHESTRATOR)
            orchestrator = (
                self._orchestrator if held is None else BudgetAmount(usd=held[0], tokens=held[1])
            )
            return BudgetAllocation(
                orchestrator=orchestrator,
                unallocated=BudgetAmount(usd=self._unallocated_usd, tokens=self._unallocated_tokens),
                reservations=MappingProxyType({
                    node_id: BudgetAmount(usd=amount[0], tokens=amount[1])
                    for node_id, amount in self._reservations.items()
                    if node_id != _ORCHESTRATOR
                }),
            )

    @property
    def remaining_unallocated(self) -> BudgetAmount:
        with self._lock:
            return BudgetAmount(usd=self._unallocated_usd, tokens=self._unallocated_tokens)

    @property
    def spend(self) -> BudgetAmount:
        with self._lock:
            return BudgetAmount(usd=self._spent_usd, tokens=self._spent_tokens)

    # --- leases -------------------------------------------------------------------------------

    def lease(self, node_id: str) -> BudgetLease:
        """The lease for one node, the same object every time it is asked for."""
        with self._lock:
            if node_id not in self._reservations:
                raise KeyError(node_id)
            if node_id not in self._leases:
                self._leases[node_id] = BudgetLease(self, node_id)
            return self._leases[node_id]

    def orchestrator_lease(self) -> BudgetLease:
        """The orchestrator's own lease, drawn on its reserve: its planning calls are
        counted, not free (FR-73)."""
        with self._lock:
            if _ORCHESTRATOR not in self._reservations:
                self._reservations[_ORCHESTRATOR] = [self._orchestrator.usd, self._orchestrator.tokens]
            if _ORCHESTRATOR not in self._leases:
                self._leases[_ORCHESTRATOR] = BudgetLease(self, _ORCHESTRATOR)
            return self._leases[_ORCHESTRATOR]

    def top_up(self, node_id: str, amount: BudgetAmount) -> None:
        """Move `amount` from the unallocated reserve to a node's reservation.

        Deterministic orchestrator policy only; a model never asks for this. The amount
        must be positive in whichever units it sets: a BudgetAmount records what it is
        given, including a negative (H1), so a top-up that did not check its sign could
        take money back out of a node through a method that only claims to add it
        (M17 round 5, K3).
        """
        if not isinstance(amount, BudgetAmount):
            raise ValueError(f"amount must be a BudgetAmount, got {type(amount).__name__}")
        if amount.usd is None and amount.tokens is None:
            raise ValueError("a top-up must set usd, tokens or both")
        if amount.usd is not None and amount.usd <= 0:
            raise ValueError(f"a top-up's usd must be above 0, got {amount.usd}")
        if amount.tokens is not None and amount.tokens <= 0:
            raise ValueError(f"a top-up's tokens must be above 0, got {amount.tokens}")
        with self._lock:
            if node_id not in self._reservations:
                raise KeyError(node_id)
            lease = self._leases.get(node_id)
            if lease is not None and lease._released:
                # Its remainder has already gone back to the pool; topping it up now
                # would move money nothing can spend and nothing can reclaim.
                raise ValueError(f"node {node_id!r} has released its lease and cannot be topped up")
            # Both units are checked before either moves: a top-up refused on tokens
            # must not have spent the USD half already.
            if amount.usd is not None:
                if self._unallocated_usd is None:
                    raise ValueError("this run has no USD ceiling to draw from")
                if amount.usd > self._unallocated_usd:
                    raise BudgetExceeded(
                        f"the unallocated reserve holds {self._unallocated_usd} USD, less than the {amount.usd} asked"
                    )
            if amount.tokens is not None:
                if self._unallocated_tokens is None:
                    raise ValueError("this run has no token ceiling to draw from")
                if amount.tokens > self._unallocated_tokens:
                    raise BudgetExceeded(
                        f"the unallocated reserve holds {self._unallocated_tokens} tokens,"
                        f" fewer than the {amount.tokens} asked"
                    )
            if amount.usd is not None:
                self._unallocated_usd -= amount.usd
                self._reservations[node_id][0] += amount.usd
            if amount.tokens is not None:
                self._unallocated_tokens -= amount.tokens
                self._reservations[node_id][1] += amount.tokens

    def may_start_child(self) -> bool:
        """FR-68: once a run's spend reaches its ceiling, no new child run starts."""
        with self._lock:
            return self._within_ceiling()

    # --- accounting, called by the leases -------------------------------------------------------

    def _charge(self, node_id: str, usage: Usage | None, cost: Decimal | None) -> None:
        with self._lock:
            tokens = 0 if usage is None else tokens_of(usage)
            self._spent_tokens += tokens
            if cost is None:
                # A call nothing could price: the spend stops being knowable in USD
                # and stays None rather than becoming a comfortable 0 (NFR-11).
                self._spent_usd = None
            elif self._spent_usd is not None:
                self._spent_usd += cost
            lease = self._leases[node_id]
            lease._spent_tokens += tokens
            if cost is None:
                lease._spent_usd = None
            elif lease._spent_usd is not None:
                lease._spent_usd += cost

    def _may_call(self, node_id: str) -> bool:
        with self._lock:
            if not self._within_ceiling():
                return False
            lease = self._leases[node_id]
            if lease._released:
                return False
            reserved_usd, reserved_tokens = self._reservations[node_id]
            if reserved_usd is not None:
                if lease._spent_usd is None or lease._spent_usd >= reserved_usd:
                    return False
            if reserved_tokens is not None and lease._spent_tokens >= reserved_tokens:
                return False
            return True

    def _release(self, node_id: str) -> None:
        with self._lock:
            lease = self._leases[node_id]
            if lease._released:
                return
            lease._released = True
            reserved_usd, reserved_tokens = self._reservations[node_id]
            # Whatever the node did not spend goes back to the unallocated reserve AND
            # stops being its reservation, so the allocation keeps summing to the run
            # ceiling instead of counting the same money twice (round 2, G2).
            if reserved_usd is not None and lease._spent_usd is not None:
                returned = max(Decimal(0), reserved_usd - lease._spent_usd)
                if self._unallocated_usd is not None:
                    self._unallocated_usd += returned
                self._reservations[node_id][0] = reserved_usd - returned
            if reserved_tokens is not None:
                returned_tokens = max(0, reserved_tokens - lease._spent_tokens)
                if self._unallocated_tokens is not None:
                    self._unallocated_tokens += returned_tokens
                self._reservations[node_id][1] = reserved_tokens - returned_tokens

    def _within_ceiling(self) -> bool:
        """Whether the run may still make a call. An unknown USD spend under a USD
        ceiling cannot be shown to be inside it, so it counts as reached."""
        if self.policy.run_ceiling_usd is not None:
            if self._spent_usd is None or self._spent_usd >= self.policy.run_ceiling_usd:
                return False
        if self.policy.run_ceiling_tokens is not None and self._spent_tokens >= self.policy.run_ceiling_tokens:
            return False
        return True


def tokens_of(usage: Usage) -> int:
    """What a call spent in tokens, never below zero.

    A provider that reports the parts but no total still spends them, so the parts are
    added rather than read as zero (round 2, G1). A provider that reports fewer than
    zero has spent nothing we can charge, and must not be able to buy budget back by
    saying so (round 3, H1). Counts are taken as reported and clamped only for the
    arithmetic, as M9 prices them.
    """
    if usage is None:
        return 0
    total = getattr(usage, "total_tokens", 0) or 0
    if total:
        return max(0, int(total))
    parts = (getattr(usage, "prompt_tokens", 0) or 0) + (getattr(usage, "completion_tokens", 0) or 0)
    return max(0, int(parts))


_ORCHESTRATOR = "<orchestrator>"


@dataclass
class BudgetLease:
    """One agent's claim on a node's reservation (FR-68).

    The agent loop asks `may_call()` before every model call and `charge()` after every
    one that answered. `release()` returns whatever the node did not spend.
    """

    governor: BudgetGovernor
    node_id: str
    _spent_usd: Decimal | None = field(default_factory=lambda: Decimal(0), repr=False)
    _spent_tokens: int = field(default=0, repr=False)
    _released: bool = field(default=False, repr=False)

    @property
    def reservation(self) -> BudgetAmount:
        usd, tokens = self.governor._reservations[self.node_id]
        return BudgetAmount(usd=usd, tokens=tokens)

    @property
    def spent(self) -> BudgetAmount:
        return BudgetAmount(usd=self._spent_usd, tokens=self._spent_tokens)

    @property
    def released(self) -> bool:
        return self._released

    def may_call(self) -> bool:
        return self.governor._may_call(self.node_id)

    def charge(self, usage: Usage | None, cost: Decimal | None) -> None:
        self.governor._charge(self.node_id, usage, cost)

    def release(self) -> None:
        self.governor._release(self.node_id)
