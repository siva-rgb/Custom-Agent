"""FR-64..FR-66 (M16): the plan as a persisted, immutable object.

A planner emits a JSON document; `plan_from_document` checks it against the shipped
schema, then builds a `PlanVersion` whose every field is checked again at
construction, so a plan built in code and a plan read from a planner meet the same
rules. A plan is a DAG (ADR-02, P2-D17): a dependency on an unknown node, and any
cycle, are refused before anything is stored.

A version never changes once built. Every mapping and list it holds is frozen all the
way down, because `plan_hash` is computed once and a plan that could be edited after
hashing would carry a hash that lies. A replan is a new version whose `parent_plan`
names the one it replaced (ADR-03).

`acceptance_criteria` and `budget_reservation` have their stored shapes here and no
behaviour: M19 evaluates criteria and M17 enforces reservations, without changing
either shape, so a plan written now stays valid then (P2-D16, DECISION-8f8cc54c).

`RunStateStore` holds a run's plan versions and each node's status. A transition
writes the status and, for a node starting or finishing, emits an event through the
run's own sink, with the node id in the envelope's task_id.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import threading
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Protocol

import jsonschema
from jsonschema.exceptions import best_match

from agentsdk.artifacts import RunAdmission, refuse_bad_scope, utc_now
from agentsdk.errors import InvalidPlan, PlanIntegrityError, PlanNotFound
from agentsdk.events import EventSink, EventType
from agentsdk.primitives import unstorable_reason

__all__ = [
    "CRITERION_KINDS",
    "NODE_STATUSES",
    "PLAN_SCHEMA",
    "PLAN_SCHEMA_PATH",
    "AcceptanceCriterion",
    "BudgetReservation",
    "InMemoryRunStateStore",
    "InvalidPlan",
    "NodeRetryPolicy",
    "PlanNode",
    "PlanIntegrityError",
    "PlanNotFound",
    "PlanVersion",
    "RunStateStore",
    "plan_from_document",
]

PLAN_SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "plan.schema.json"
PLAN_SCHEMA: dict[str, Any] = json.loads(PLAN_SCHEMA_PATH.read_text(encoding="utf-8"))
_PLAN_VALIDATOR = jsonschema.Draft202012Validator(PLAN_SCHEMA)

NODE_STATUSES = ("pending", "ready", "running", "done", "failed", "skipped", "cancelled")
FINAL_STATUSES = frozenset({"done", "failed", "skipped", "cancelled"})
CRITERION_KINDS = ("output_schema", "artifact_exists", "tool_succeeds", "critic")
# Which optional fields each criterion kind requires, and which it may carry.
_CRITERION_REQUIRES = {"output_schema": (), "artifact_exists": ("target",), "tool_succeeds": ("target",), "critic": ("description",)}
_CRITERION_ALLOWS = {"output_schema": (), "artifact_exists": ("target",), "tool_succeeds": ("target", "arguments"), "critic": ("description",)}
_INT_CEILING = 2**31 - 1  # INTEGER columns, as FR-43 bounds its limits
# Deeper JSON is refused. Every later step walks it recursively -- copying, freezing,
# the storability check, JSON Schema's own check -- and at 200 levels one of them
# raised RecursionError instead of InvalidPlan (DECISION-6d073ac0, F5).
MAX_JSON_DEPTH = 64


# --- checks shared by every type -------------------------------------------------------------------


def _text(name: str, value: Any) -> str:
    if type(value) is not str:
        raise InvalidPlan(f"{name} must be a str, got {type(value).__name__}")
    if not value:
        raise InvalidPlan(f"{name} must not be empty")
    reason = unstorable_reason(value)
    if reason is not None:
        raise InvalidPlan(f"{name} cannot be stored: {reason}")
    return value


def _optional_text(name: str, value: Any) -> str | None:
    return None if value is None else _text(name, value)


def _texts(name: str, value: Any, *, unique: bool = False) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise InvalidPlan(f"{name} must be a list or tuple of str, got {type(value).__name__}")
    items = tuple(_text(f"{name}[{i}]", item) for i, item in enumerate(value))
    if unique and len(set(items)) != len(items):
        raise InvalidPlan(f"{name} names {sorted({i for i in items if items.count(i) > 1})} more than once")
    return items


def _count(name: str, value: Any) -> int:
    if type(value) is not int:
        raise InvalidPlan(f"{name} must be an int, got {type(value).__name__}")
    if not 1 <= value <= _INT_CEILING:
        raise InvalidPlan(f"{name} must be between 1 and {_INT_CEILING}, got {value}")
    return value


def _frozen(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _frozen(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_frozen(item) for item in value)
    return value


def _thawed(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thawed(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thawed(item) for item in value]
    return value


def _too_deep(value: Any) -> bool:
    """Whether `value` nests more than MAX_JSON_DEPTH levels; iterative, so it cannot
    itself recurse too deep."""
    pending = [(value, 1)]
    while pending:
        item, depth = pending.pop()
        if isinstance(item, Mapping):
            children = list(item.values())
        elif isinstance(item, (list, tuple)):
            children = list(item)
        else:
            continue
        if depth > MAX_JSON_DEPTH:
            return True
        pending.extend((child, depth + 1) for child in children)
    return False


def _json_object(name: str, value: Any) -> Mapping[str, Any]:
    """A JSON object this plan owns: copied, storable, string-keyed and frozen."""
    if not isinstance(value, Mapping):
        raise InvalidPlan(f"{name} must be a mapping, got {type(value).__name__}")
    if _too_deep(value):
        raise InvalidPlan(f"{name} is nested more than {MAX_JSON_DEPTH} levels deep")
    try:
        owned = copy.deepcopy(_thawed(value))
        text = json.dumps(owned, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise InvalidPlan(f"{name} is not plain JSON: {exc}") from None
    if json.loads(text) != owned:
        # Non-string keys, which json.dumps turns into strings, so the stored plan
        # would read back different from the one built.
        raise InvalidPlan(f"{name} does not survive a JSON round trip unchanged")
    reason = unstorable_reason(owned)
    if reason is not None:
        raise InvalidPlan(f"{name} cannot be stored: {reason}")
    return _frozen(owned)


def _uuid(name: str, value: Any) -> str:
    # Imported here, not at the top: agentsdk.postgres imports this module.
    from agentsdk.postgres import column_rejection_reason

    if type(value) is not str:
        raise InvalidPlan(f"{name} must be a str, got {type(value).__name__}")
    reason = column_rejection_reason(value, "UUID")
    if reason is not None:
        raise InvalidPlan(f"{name} cannot be stored as a UUID: {reason}")
    return value


# --- the stored shapes -----------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class AcceptanceCriterion:
    """FR-74's criterion, stored now and evaluated by M19 (P2-D16).

    `output_schema` takes nothing else; `artifact_exists` names an artifact by its
    uri in `target`; `tool_succeeds` names a tool in `target`, with optional
    `arguments`; `critic` carries a `description` and is refused at runtime until
    Phase 3 builds the critic. A field another kind uses is refused, not ignored.
    """

    kind: str
    target: str | None = None
    arguments: Mapping[str, Any] | None = None
    description: str | None = None

    def __post_init__(self) -> None:
        if type(self.kind) is not str or self.kind not in CRITERION_KINDS:
            raise InvalidPlan(f"kind must be one of {', '.join(CRITERION_KINDS)}, got {self.kind!r}")
        for name in ("target", "arguments", "description"):
            value = getattr(self, name)
            if name in _CRITERION_REQUIRES[self.kind] and value is None:
                raise InvalidPlan(f"a {self.kind} criterion needs {name}")
            if name not in _CRITERION_ALLOWS[self.kind] and value is not None:
                raise InvalidPlan(f"a {self.kind} criterion takes no {name}")
        if self.target is not None:
            _text("target", self.target)
        if self.description is not None:
            _text("description", self.description)
        if self.arguments is not None:
            object.__setattr__(self, "arguments", _json_object("arguments", self.arguments))


@dataclass(frozen=True, kw_only=True)
class BudgetReservation:
    """FR-67's reservation, stored now and enforced by M17: USD, tokens, or both."""

    usd: Decimal | None = None
    tokens: int | None = None

    def __post_init__(self) -> None:
        if self.usd is None and self.tokens is None:
            raise InvalidPlan("a budget reservation must set usd, tokens or both")
        if self.usd is not None:
            if not isinstance(self.usd, Decimal):
                raise InvalidPlan(f"usd must be a Decimal, got {type(self.usd).__name__}")
            if not self.usd.is_finite() or self.usd <= 0:
                raise InvalidPlan(f"usd must be a finite amount above 0, got {self.usd}")
        if self.tokens is not None:
            _count("tokens", self.tokens)


@dataclass(frozen=True, kw_only=True)
class NodeRetryPolicy:
    """How many times a node may be attempted; its behaviour arrives with M19."""

    max_attempts: int

    def __post_init__(self) -> None:
        _count("max_attempts", self.max_attempts)


@dataclass(frozen=True, kw_only=True)
class PlanNode:
    """FR-64. One step of a plan. `side_effecting` defaults to True, so a node that
    says nothing is treated as the unsafe case and never rerun once done (P2-D18)."""

    node_id: str
    objective: str
    dependencies: tuple[str, ...] = ()
    assigned_role: str
    input_refs: tuple[str, ...] = ()
    expected_output_schema: Mapping[str, Any] | None = None
    acceptance_criteria: tuple[AcceptanceCriterion, ...] = ()
    budget_reservation: BudgetReservation | None = None
    side_effecting: bool = True
    timeout: float | None = None
    retry_policy: NodeRetryPolicy | None = None
    risk_class: str | None = None

    def __post_init__(self) -> None:
        set_ = lambda name, value: object.__setattr__(self, name, value)  # noqa: E731
        _text("node_id", self.node_id)
        _text("objective", self.objective)
        _text("assigned_role", self.assigned_role)
        set_("dependencies", _texts("dependencies", self.dependencies, unique=True))
        set_("input_refs", _texts("input_refs", self.input_refs))
        if self.expected_output_schema is not None:
            schema = _json_object("expected_output_schema", self.expected_output_schema)
            thawed = _thawed(schema)
            try:
                jsonschema.validators.validator_for(thawed, default=jsonschema.Draft202012Validator).check_schema(thawed)
            except jsonschema.SchemaError as exc:
                raise InvalidPlan(f"expected_output_schema is not a valid JSON Schema: {exc.message}") from None
            set_("expected_output_schema", schema)
        if not isinstance(self.acceptance_criteria, (list, tuple)) or not all(
            isinstance(c, AcceptanceCriterion) for c in self.acceptance_criteria
        ):
            raise InvalidPlan("acceptance_criteria must be a list or tuple of AcceptanceCriterion")
        set_("acceptance_criteria", tuple(self.acceptance_criteria))
        if self.budget_reservation is not None and not isinstance(self.budget_reservation, BudgetReservation):
            raise InvalidPlan(f"budget_reservation must be a BudgetReservation, got {type(self.budget_reservation).__name__}")
        if type(self.side_effecting) is not bool:
            raise InvalidPlan(f"side_effecting must be a bool, got {type(self.side_effecting).__name__}")
        if self.timeout is not None:
            if type(self.timeout) not in (int, float) or not math.isfinite(self.timeout) or self.timeout <= 0:
                raise InvalidPlan(f"timeout must be a finite number of seconds above 0, got {self.timeout!r}")
            set_("timeout", float(self.timeout))
        if self.retry_policy is not None and not isinstance(self.retry_policy, NodeRetryPolicy):
            raise InvalidPlan(f"retry_policy must be a NodeRetryPolicy, got {type(self.retry_policy).__name__}")
        _optional_text("risk_class", self.risk_class)


@dataclass(frozen=True, kw_only=True)
class PlanVersion:
    """FR-65. One immutable version of a run's plan; a replan is a new version whose
    `parent_plan` is the (plan_id, version) it replaced."""

    plan_id: str
    version: int
    run_id: str
    nodes: tuple[PlanNode, ...]
    parent_plan: tuple[str, int] | None = None
    created_at: datetime | None = None
    plan_hash: str = field(init=False)

    def __post_init__(self) -> None:
        set_ = lambda name, value: object.__setattr__(self, name, value)  # noqa: E731
        _uuid("plan_id", self.plan_id)
        _count("version", self.version)
        _uuid("run_id", self.run_id)
        if self.parent_plan is not None:
            if not isinstance(self.parent_plan, (list, tuple)) or len(self.parent_plan) != 2:
                raise InvalidPlan("parent_plan must be a (plan_id, version) pair")
            set_("parent_plan", (_uuid("parent_plan plan_id", self.parent_plan[0]),
                                 _count("parent_plan version", self.parent_plan[1])))
        if self.created_at is None:
            set_("created_at", utc_now())
        elif not isinstance(self.created_at, datetime) or self.created_at.utcoffset() is None:
            raise InvalidPlan("created_at must be a timezone-aware datetime")
        if not isinstance(self.nodes, (list, tuple)):
            raise InvalidPlan(f"nodes must be a list or tuple of PlanNode, got {type(self.nodes).__name__}")
        if not self.nodes:
            raise InvalidPlan("nodes must hold at least one PlanNode")
        for item in self.nodes:
            if not isinstance(item, PlanNode):
                raise InvalidPlan(f"nodes must hold PlanNode values, got {type(item).__name__}")
        set_("nodes", tuple(self.nodes))
        _refuse_anything_but_a_dag(self.nodes)
        set_("plan_hash", _canonical_hash(self.to_document()))

    def node(self, node_id: str) -> PlanNode:
        for item in self.nodes:
            if item.node_id == node_id:
                return item
        raise PlanNotFound(f"plan {self.plan_id} version {self.version} has no node {node_id!r}")

    def to_document(self) -> dict[str, Any]:
        """The planner's form of this plan: plain JSON, every field present."""
        return {"nodes": [_node_document(item) for item in self.nodes]}


def _refuse_anything_but_a_dag(nodes: tuple[PlanNode, ...]) -> None:
    ids = [item.node_id for item in nodes]
    duplicated = sorted({i for i in ids if ids.count(i) > 1})
    if duplicated:
        raise InvalidPlan(f"duplicate node_id {', '.join(repr(i) for i in duplicated)}")
    known = set(ids)
    edges = {item.node_id: item.dependencies for item in nodes}
    for item in nodes:
        for dependency in item.dependencies:
            if dependency not in known:
                raise InvalidPlan(f"node {item.node_id!r} depends on unknown node {dependency!r}")
    # Depth-first, keeping the path, so the refusal names the cycle itself rather
    # than every node downstream of it.
    done: set[str] = set()
    for start in ids:
        path: list[str] = []
        on_path: set[str] = set()
        stack: list[tuple[str, int]] = [(start, 0)]
        while stack:
            current, index = stack.pop()
            if index == 0:
                if current in done:
                    continue
                path.append(current)
                on_path.add(current)
            dependencies = edges[current]
            if index < len(dependencies):
                stack.append((current, index + 1))
                following = dependencies[index]
                if following in on_path:
                    cycle = path[path.index(following):] + [following]
                    raise InvalidPlan(f"the dependencies form a cycle: {' -> '.join(repr(n) for n in cycle)}")
                if following not in done:
                    stack.append((following, 0))
            else:
                path.pop()
                on_path.discard(current)
                done.add(current)


def _node_document(item: PlanNode) -> dict[str, Any]:
    reservation = item.budget_reservation
    return {
        "node_id": item.node_id,
        "objective": item.objective,
        "dependencies": list(item.dependencies),
        "assigned_role": item.assigned_role,
        "input_refs": list(item.input_refs),
        "expected_output_schema": _thawed(item.expected_output_schema),
        "acceptance_criteria": [
            {"kind": c.kind, "target": c.target, "arguments": _thawed(c.arguments), "description": c.description}
            for c in item.acceptance_criteria
        ],
        "budget_reservation": None if reservation is None else {
            # Fixed-point, never str(): str(Decimal("1E-7")) is "1E-7", which the
            # document's price pattern refuses, so the plan would store and never read back.
            "usd": None if reservation.usd is None else format(reservation.usd, "f"),
            "tokens": reservation.tokens,
        },
        "side_effecting": item.side_effecting,
        "timeout": item.timeout,
        "retry_policy": None if item.retry_policy is None else {"max_attempts": item.retry_policy.max_attempts},
        "risk_class": item.risk_class,
    }


def canonical_text(document: dict[str, Any]) -> str:
    """The exact text a plan is hashed over, and the text Postgres stores: kept as text,
    not JSONB, because JSONB rewrites numbers (1e16 comes back as 10000000000000000,
    -0.0 as 0) and the plan would read back with another hash (DECISION-6d073ac0, F3)."""
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _canonical_hash(document: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_text(document).encode("utf-8")).hexdigest()


def plan_from_document(
    document: Any,
    *,
    plan_id: str,
    version: int,
    run_id: str,
    parent_plan: tuple[str, int] | None = None,
    created_at: datetime | None = None,
) -> PlanVersion:
    """FR-65. Build a plan from a planner's JSON document, or refuse it whole.

    The document is checked against PLAN_SCHEMA, and a refusal names the failing
    place in `InvalidPlan.path`. The plan keeps no reference into the document --
    every node copies and freezes what it holds -- so a planner that edits the
    document afterwards cannot change the plan. Nothing is stored here, so a
    refused document leaves nothing behind.
    """
    error = best_match(_PLAN_VALIDATOR.iter_errors(document))
    if error is not None:
        path = "/".join(str(part) for part in error.absolute_path)
        raise InvalidPlan(f"the plan document is invalid at {path or 'its root'}: {error.message}", path=path)
    nodes = []
    for index, raw in enumerate(document["nodes"]):
        try:
            nodes.append(_node_from_document(raw))
        except InvalidPlan as exc:
            exc.path = f"nodes/{index}" if exc.path is None else exc.path
            raise
    try:
        _refuse_anything_but_a_dag(tuple(nodes))
    except InvalidPlan as exc:
        exc.path = "nodes"
        raise
    return PlanVersion(
        plan_id=plan_id, version=version, run_id=run_id, nodes=tuple(nodes),
        parent_plan=parent_plan, created_at=created_at,
    )


def _node_from_document(raw: Mapping[str, Any]) -> PlanNode:
    reservation = raw.get("budget_reservation")
    retry = raw.get("retry_policy")
    return PlanNode(
        node_id=raw["node_id"],
        objective=raw["objective"],
        dependencies=tuple(raw.get("dependencies", ())),
        assigned_role=raw["assigned_role"],
        input_refs=tuple(raw.get("input_refs", ())),
        expected_output_schema=raw.get("expected_output_schema"),
        acceptance_criteria=tuple(
            AcceptanceCriterion(
                kind=c["kind"], target=c.get("target"), arguments=c.get("arguments"), description=c.get("description"),
            )
            for c in raw.get("acceptance_criteria", ())
        ),
        budget_reservation=None if reservation is None else BudgetReservation(
            usd=None if reservation.get("usd") is None else Decimal(reservation["usd"]),
            tokens=reservation.get("tokens"),
        ),
        side_effecting=raw.get("side_effecting", True),
        timeout=raw.get("timeout"),
        retry_policy=None if retry is None else NodeRetryPolicy(max_attempts=retry["max_attempts"]),
        risk_class=raw.get("risk_class"),
    )


# --- node status -----------------------------------------------------------------------------------


def checked_status(status: Any) -> str:
    if type(status) is not str or status not in NODE_STATUSES:
        raise InvalidPlan(f"status must be one of {', '.join(NODE_STATUSES)}, got {status!r}")
    return status


def refuse_transition(node_id: str, current: str, status: str) -> None:
    """The two rules every store applies: a finished node is final, and nothing
    returns to pending, which only a newly stored plan's nodes are."""
    if current in FINAL_STATUSES:
        raise InvalidPlan(f"node {node_id!r} is {current}, which is final")
    if status == "pending":
        raise InvalidPlan(f"node {node_id!r} cannot return to pending")


def emit_transition(sink: EventSink, plan_id: str, version: int, node_id: str, status: str) -> None:
    """FR-66: starting and finishing emit through the run's own sink; the rest do not."""
    if status == "running":
        event_type = EventType.PLAN_NODE_STARTED
    elif status in FINAL_STATUSES:
        event_type = EventType.PLAN_NODE_FINISHED
    else:
        return
    sink.emit(event_type, {"plan_id": plan_id, "version": version, "status": status}, task_id=node_id)


class RunStateStore(Protocol):
    """FR-65, FR-66: a run's plan versions and each node's status, in one scope."""

    async def put_plan(self, plan: PlanVersion) -> None: ...

    async def get_plan(self, plan_id: str, version: int) -> PlanVersion: ...

    async def versions(self, plan_id: str) -> tuple[PlanVersion, ...]: ...

    async def node_states(self, plan_id: str, version: int) -> Mapping[str, str]: ...

    async def transition(self, plan_id: str, version: int, node_id: str, status: str, *, sink: EventSink) -> None: ...


class InMemoryRunStateStore:
    """The in-memory RunStateStore: the same rules as Postgres, held in dicts.

    A plan's run is accepted only when `runs(tenant_id, project_id, run_id)` returns
    True; built without that callable the store refuses every run, because it has no
    other way to know a run belongs to this scope (as InMemoryArtifactStore).

    A plan is identified by tenant, project, plan_id and version, as on Postgres, so
    another scope can neither take a plan's id nor learn that it exists; and every
    version of one plan belongs to one run (DECISION-6d073ac0, F1).
    """

    def __init__(self, tenant_id: str, project_id: str, *, runs: RunAdmission | None = None) -> None:
        refuse_bad_scope(tenant_id, project_id)
        self._tenant_id = tenant_id
        self._project_id = project_id
        self._runs = runs
        # Shared by stores from for_scope(), as one database is shared by every scope.
        self._plans: dict[tuple[str, str, str, int], PlanVersion] = {}
        self._states: dict[tuple[str, str, str, int], dict[str, str]] = {}
        self._lock = threading.Lock()

    def for_scope(self, tenant_id: str, project_id: str) -> InMemoryRunStateStore:
        """A store over the same plans, bound to another tenant and project."""
        other = InMemoryRunStateStore(tenant_id, project_id, runs=self._runs)
        other._plans, other._states, other._lock = self._plans, self._states, self._lock
        return other

    def _key(self, plan_id: Any, version: Any) -> tuple[str, str, Any, Any]:
        return (self._tenant_id, self._project_id, plan_id, version)

    def _mine(self, plan_id: Any, version: Any) -> PlanVersion:
        found = self._plans.get(self._key(plan_id, version)) if isinstance(plan_id, str) and type(version) is int else None
        if found is None:
            raise PlanNotFound(f"no plan {plan_id} version {version} in this scope")
        return found

    async def put_plan(self, plan: PlanVersion) -> None:
        if not isinstance(plan, PlanVersion):
            raise InvalidPlan(f"put_plan needs a PlanVersion, got {type(plan).__name__}")
        with self._lock:
            if self._runs is None or not self._runs(self._tenant_id, self._project_id, plan.run_id):
                raise InvalidPlan(f"run {plan.run_id} is not a run of this store's tenant and project")
            # The parent first: it is what the caller named.
            if plan.parent_plan is not None:
                parent = self._plans.get(self._key(*plan.parent_plan))
                if parent is None or parent.run_id != plan.run_id:
                    raise InvalidPlan(f"parent_plan {plan.parent_plan} is not a stored version of this run's plans")
            owner = next((p.run_id for k, p in self._plans.items() if k[:3] == self._key(plan.plan_id, 0)[:3]), None)
            if owner is not None and owner != plan.run_id:
                raise InvalidPlan(f"plan {plan.plan_id} belongs to run {owner}, not run {plan.run_id}")
            if self._key(plan.plan_id, plan.version) in self._plans:
                raise InvalidPlan(f"plan {plan.plan_id} version {plan.version} is already stored")
            self._plans[self._key(plan.plan_id, plan.version)] = plan
            self._states[self._key(plan.plan_id, plan.version)] = {item.node_id: "pending" for item in plan.nodes}

    async def get_plan(self, plan_id: str, version: int) -> PlanVersion:
        with self._lock:
            return self._mine(plan_id, version)

    async def versions(self, plan_id: str) -> tuple[PlanVersion, ...]:
        with self._lock:
            mine = [p for key, p in self._plans.items() if key[:3] == self._key(plan_id, 0)[:3]]
        return tuple(sorted(mine, key=lambda p: p.version))

    async def node_states(self, plan_id: str, version: int) -> Mapping[str, str]:
        with self._lock:
            self._mine(plan_id, version)
            return MappingProxyType(dict(self._states[self._key(plan_id, version)]))

    async def transition(self, plan_id: str, version: int, node_id: str, status: str, *, sink: EventSink) -> None:
        checked_status(status)
        with self._lock:
            plan = self._mine(plan_id, version)
            plan.node(node_id)
            states = self._states[self._key(plan_id, version)]
            refuse_transition(node_id, states[node_id], status)
            states[node_id] = status
            # Inside the lock, so this store's rows and the run's events are in the
            # same order.
            emit_transition(sink, plan_id, version, node_id, status)
