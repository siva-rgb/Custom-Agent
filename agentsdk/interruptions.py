"""The interruption record (FR-112, M29): what a call that required approval asked, and
what was decided about it.

A pause and each decision are rows, never a client-held snapshot (P4-D2,
KNOWLEDGE-5b3b78e5). One interruption per call that required approval; at most one
decision per interruption, which the PostgreSQL store's unique key refuses a second of;
standing decisions ordered by a sequence, so the latest wins without a tie (P4-D15).
Every row is bound to its run's tenant and project, and nothing is ever updated or
deleted: an interruption's state is read from whether its decision exists and from the
time.

M29 adds the record only. Nothing writes it until M30's approval manager.
"""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable

from .artifacts import canonical_id
from .identity import PrincipalContext
from .outcomes import InterruptionKind, RunInterruption
from .primitives import unstorable_reason

__all__ = [
    "DECISIONS",
    "SOURCES",
    "TRIGGERS",
    "InMemoryInterruptionStore",
    "InterruptionAlreadyDecided",
    "InterruptionDecision",
    "InterruptionExpired",
    "InterruptionNotWaiting",
    "InterruptionView",
    "StandingDecision",
    "UnknownInterruption",
    "checked_interruption",
    "state_of",
]

TRIGGERS = ("policy", "predicate", "hook")  # FR-113, in the order they are tried
DECISIONS = ("approve", "reject")
SOURCES = ("principal", "standing", "expiry", "cancel")


class UnknownInterruption(LookupError):
    """No interruption by that id in the caller's tenant and project. One in another
    tenant or project is reported exactly so, never as decided (NFR-31)."""


class InterruptionAlreadyDecided(Exception):
    """The interruption has its one decision already."""


class InterruptionExpired(Exception):
    """The interruption is past its `expires_at`."""


class InterruptionNotWaiting(Exception):
    """No run in this Runner is waiting on the interruption (P4-D14)."""


def _id(name: str, value: Any) -> str:
    key = canonical_id(value) if isinstance(value, str) else None
    if key is None:
        raise ValueError(f"{name} must be a canonical UUID as text, got {value!r}")
    return key


def _text(name: str, value: Any, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if type(value) is not str or not value.strip():
        raise ValueError(f"{name} must be non-empty text, got {value!r}")
    reason = unstorable_reason(value)
    if reason is not None:
        raise ValueError(f"{name} cannot be stored: {reason}")
    return value


def _when(name: str, value: Any) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime, got {value!r}")
    return value


def _decider(name: str, value: Any) -> PrincipalContext:
    """FR-115: a principal naming a user or a service; the agent alone may not decide."""
    if not isinstance(value, PrincipalContext):
        raise ValueError(f"{name} must be a PrincipalContext, got {type(value).__name__}")
    if not (value.user_principal or value.service_principal):
        raise ValueError(f"{name} must name a user or a service, not only an agent")
    return value


def _message(value: Any) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise ValueError(f"message must be text or None, got {type(value).__name__}")
    reason = unstorable_reason(value)
    if reason is not None:
        raise ValueError(f"message cannot be stored: {reason}")
    return value


def principal_from_json(data: Any) -> PrincipalContext | None:
    if data is None:
        return None
    return PrincipalContext(**{**data, "scopes": tuple(data.get("scopes") or ())})


@dataclass(frozen=True)
class InterruptionDecision:
    """FR-112: the one decision of an interruption. `source` says who or what made it: a
    principal (named), a standing decision (named by `standing_id`), the interruption's
    expiry or the run's cancellation (both rejections, with no principal)."""

    decision_id: str
    interruption_id: str
    tenant_id: str
    project_id: str
    decision: str
    source: str
    decided_at: datetime
    message: str | None = None
    principal: PrincipalContext | None = None
    standing_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision_id", _id("decision_id", self.decision_id))
        object.__setattr__(self, "interruption_id", _id("interruption_id", self.interruption_id))
        _text("tenant_id", self.tenant_id)
        _text("project_id", self.project_id)
        if self.decision not in DECISIONS:
            raise ValueError(f"decision must be one of {', '.join(DECISIONS)}, got {self.decision!r}")
        if self.source not in SOURCES:
            raise ValueError(f"source must be one of {', '.join(SOURCES)}, got {self.source!r}")
        if self.source == "principal":
            _decider("principal", self.principal)
        elif self.principal is not None:
            raise ValueError(f"a decision of source {self.source} names no principal")
        if self.source == "standing":
            object.__setattr__(self, "standing_id", _id("standing_id", self.standing_id))
        elif self.standing_id is not None:
            raise ValueError(f"only a standing decision names a standing_id, not one of source {self.source}")
        if self.source in ("expiry", "cancel") and self.decision != "reject":
            raise ValueError(f"a decision of source {self.source} is a reject")
        _message(self.message)
        _when("decided_at", self.decided_at)


@dataclass(frozen=True)
class StandingDecision:
    """FR-112, P4-D15: a sticky decision for one run, agent and tool, made by a named
    principal. `seq` is assigned by the store; the latest wins. `tool_hash` (FR-128, M34) is
    the version of the tool it was made for -- an MCP tool's `tool_hash`, None for any other
    tool -- and it applies only to a request of that same version."""

    standing_id: str
    run_id: str
    tenant_id: str
    project_id: str
    agent_id: str
    tool_name: str
    decision: str
    principal: PrincipalContext
    created_at: datetime
    message: str | None = None
    seq: int | None = None
    tool_hash: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "standing_id", _id("standing_id", self.standing_id))
        if self.tool_hash is not None:
            from .catalog import checked_hash

            checked_hash(self.tool_hash)
        object.__setattr__(self, "run_id", _id("run_id", self.run_id))
        for name in ("tenant_id", "project_id", "agent_id", "tool_name"):
            _text(name, getattr(self, name))
        if self.decision not in DECISIONS:
            raise ValueError(f"decision must be one of {', '.join(DECISIONS)}, got {self.decision!r}")
        _decider("principal", self.principal)
        _message(self.message)
        _when("created_at", self.created_at)
        if self.seq is not None and (type(self.seq) is not int or self.seq < 1):
            raise ValueError(f"seq must be a positive int or None, got {self.seq!r}")


@dataclass(frozen=True)
class InterruptionView:
    """An interruption with its decision or none, its state, and whether a run in this
    Runner is waiting on it (P4-D14; a store alone never knows, so False there)."""

    interruption: RunInterruption
    decision: InterruptionDecision | None
    state: str
    waiting: bool = False


def checked_interruption(item: Any) -> RunInterruption:
    """The fields FR-112 requires of an interruption about to be recorded."""
    if not isinstance(item, RunInterruption):
        raise ValueError(f"an interruption must be a RunInterruption, got {type(item).__name__}")
    if item.kind is not InterruptionKind.TOOL_APPROVAL:
        raise ValueError(f"only tool_approval interruptions are recorded in this increment, got {item.kind!r}")
    _id("interruption_id", item.interruption_id)
    _id("run_id", item.run_id)
    _id("top_run_id", item.top_run_id)
    for name in ("tenant_id", "project_id", "tool_call_id", "tool_name", "agent_id"):
        _text(name, getattr(item, name))
    _text("node_id", item.node_id, optional=True)
    if item.trigger not in TRIGGERS:
        raise ValueError(f"trigger must be one of {', '.join(TRIGGERS)}, got {item.trigger!r}")
    if not isinstance(item.request_payload, dict):
        raise ValueError("request_payload must be a JSON object")
    reason = unstorable_reason(item.request_payload)
    if reason is not None:
        raise ValueError(f"the request cannot be stored: {reason}")
    _when("created_at", item.created_at)
    _when("expires_at", item.expires_at)
    return replace(item, interruption_id=_id("interruption_id", item.interruption_id),
                   run_id=_id("run_id", item.run_id), top_run_id=_id("top_run_id", item.top_run_id))


def state_of(item: RunInterruption, decision: InterruptionDecision | None, now: datetime | None = None) -> str:
    """FR-112: decided when the decision exists, else expired once `expires_at` has
    passed, else pending."""
    if decision is not None:
        if decision.source == "expiry":
            return "expired"
        if decision.source == "cancel":
            return "cancelled"
        return "approved" if decision.decision == "approve" else "rejected"
    now = now or datetime.now(timezone.utc)
    return "expired" if item.expires_at is not None and now >= item.expires_at else "pending"


def _copied(item: RunInterruption) -> RunInterruption:
    return replace(item, request_payload=copy.deepcopy(item.request_payload))


class ApprovalWaits:
    """FR-115 (M30): the interruptions a Runner's runs are waiting on in this process, and
    how to wake each. A decision is recorded in the store first; the wake only tells the
    waiting run to read it, so a wake that is lost delays a decision and never loses it.
    Safe from any thread: the wake is handed to the waiting run's own loop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._waiting: dict[str, tuple[Any, Any, str]] = {}

    def register(self, interruption_ids: list[str], loop: Any, event: Any, run_id: str) -> None:
        with self._lock:
            for interruption_id in interruption_ids:
                self._waiting[interruption_id] = (loop, event, run_id)

    def unregister(self, interruption_ids: list[str]) -> None:
        with self._lock:
            for interruption_id in interruption_ids:
                self._waiting.pop(interruption_id, None)

    def waiting(self, interruption_id: str) -> bool:
        with self._lock:
            return interruption_id in self._waiting

    def wake(self, interruption_id: str) -> None:
        with self._lock:
            entry = self._waiting.get(interruption_id)
        if entry is None:
            return
        loop, event, _ = entry
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:  # the run's loop has closed; nothing is waiting any more
            pass


class InMemoryInterruptionStore:
    """FR-112's store for a Runner without persistence: every tenant's interruptions, one
    lock. `runs(tenant_id, project_id, run_id)` says whether a run exists in that tenant
    and project -- the composite key PostgreSQL holds -- and `parent(tenant_id,
    project_id, run_id)` names a run's parent, for listing the runs beneath one."""

    def __init__(self, runs: Callable[[str, str, str], bool],
                 parent: Callable[[str, str, str], str | None] | None = None) -> None:
        self._runs, self._parent = runs, parent or (lambda tenant, project, run: None)
        self._lock = threading.Lock()
        self._interruptions: dict[str, RunInterruption] = {}
        self._decisions: dict[str, InterruptionDecision] = {}
        self._standing: list[StandingDecision] = []

    def _run(self, tenant_id: str, project_id: str, run_id: str) -> None:
        if not self._runs(tenant_id, project_id, run_id):
            raise LookupError(f"run {run_id} is not a run of this tenant and project")

    def append_interruption(self, item: RunInterruption, decision: InterruptionDecision | None = None) -> None:
        """Records an interruption and, with `decision` -- the standing decision covering it
        (FR-114) -- its decision in the same step, so it is never seen pending. Everything
        is checked before anything is recorded."""
        item = checked_interruption(item)
        if decision is not None and (not isinstance(decision, InterruptionDecision)
                                     or decision.interruption_id != item.interruption_id):
            raise ValueError("a decision recorded with an interruption must be that interruption's")
        with self._lock:
            self._run(item.tenant_id, item.project_id, item.run_id)
            self._run(item.tenant_id, item.project_id, item.top_run_id)
            if item.interruption_id in self._interruptions:
                raise ValueError(f"interruption {item.interruption_id} is already recorded")
            if decision is not None:
                self._decision_checked(item, decision)
            # Held as a copy and handed out as copies: no caller's object aliases the record.
            self._interruptions[item.interruption_id] = _copied(item)
            if decision is not None:
                self._decisions[item.interruption_id] = decision

    def append_decision(self, decision: InterruptionDecision) -> None:
        if not isinstance(decision, InterruptionDecision):
            raise ValueError(f"a decision must be an InterruptionDecision, got {type(decision).__name__}")
        with self._lock:
            self._decision_checked(self._interruptions.get(decision.interruption_id), decision)
            if decision.interruption_id in self._decisions:
                raise InterruptionAlreadyDecided(f"interruption {decision.interruption_id} is already decided")
            self._decisions[decision.interruption_id] = decision

    def _decision_checked(self, item: RunInterruption | None, decision: InterruptionDecision) -> None:
        """The decision is of an interruption in its own tenant and project, and a standing
        decision it applies is one of that interruption's run. The caller holds the lock."""
        if item is None or (item.tenant_id, item.project_id) != (decision.tenant_id, decision.project_id):
            raise UnknownInterruption(f"no interruption {decision.interruption_id} in this tenant and project")
        if decision.standing_id is not None and not any(
                s.standing_id == decision.standing_id and s.run_id == item.run_id for s in self._standing):
            raise LookupError(f"standing decision {decision.standing_id} is not one of this run's")

    def append_standing(self, standing: StandingDecision) -> StandingDecision:
        if not isinstance(standing, StandingDecision):
            raise ValueError(f"a standing decision must be a StandingDecision, got {type(standing).__name__}")
        with self._lock:
            self._run(standing.tenant_id, standing.project_id, standing.run_id)
            if any(s.standing_id == standing.standing_id for s in self._standing):
                raise ValueError(f"standing decision {standing.standing_id} is already recorded")
            made = replace(standing, seq=len(self._standing) + 1)
            self._standing.append(made)
            return made

    def _beneath(self, tenant_id: str, project_id: str, run_id: str, top: str) -> bool:
        """Whether `run_id` is `top` or a run beneath it."""
        seen, run = set(), run_id
        while run is not None and run not in seen:
            if run == top:
                return True
            seen.add(run)
            run = self._parent(tenant_id, project_id, run)
        return False

    def interruptions(self, tenant_id: str, project_id: str, *, run_id: str | None = None,
                      pending_only: bool = False, now: datetime | None = None) -> list[InterruptionView]:
        now = now or datetime.now(timezone.utc)
        top = None if run_id is None else canonical_id(run_id) if isinstance(run_id, str) else None
        if run_id is not None and top is None:
            return []  # a run_id given but unreadable names no run, as PostgreSQL finds none
        with self._lock:
            found = [(i, self._decisions.get(i.interruption_id)) for i in self._interruptions.values()
                     if (i.tenant_id, i.project_id) == (tenant_id, project_id)
                     and (top is None or self._beneath(tenant_id, project_id, i.run_id, top))]
        views = [InterruptionView(_copied(i), d, state_of(i, d, now))
                 for i, d in sorted(found, key=lambda p: p[0].created_at)]
        return [v for v in views if v.state == "pending"] if pending_only else views

    def interruption(self, tenant_id: str, project_id: str, interruption_id: str,
                     now: datetime | None = None) -> InterruptionView:
        key = canonical_id(interruption_id) if isinstance(interruption_id, str) else None
        with self._lock:
            item = self._interruptions.get(key) if key else None
            if item is None or (item.tenant_id, item.project_id) != (tenant_id, project_id):
                raise UnknownInterruption(f"no interruption {interruption_id} in this tenant and project")
            decision = self._decisions.get(key)
        return InterruptionView(_copied(item), decision, state_of(item, decision, now))

    def standing(self, tenant_id: str, project_id: str, run_id: str) -> list[StandingDecision]:
        key = canonical_id(run_id) if isinstance(run_id, str) else None
        with self._lock:
            return [s for s in self._standing
                    if (s.tenant_id, s.project_id, s.run_id) == (tenant_id, project_id, key)]
