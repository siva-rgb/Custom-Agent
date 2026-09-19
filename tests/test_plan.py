"""M16 gate: the plan as a persisted object (FR-64..FR-66, AC-51, AC-52, NFR-22).

Written before the implementation, against the approved specification with FR-64 and
FR-66 as clarified on 2026-09-19 (DECISION-8f8cc54c): M16 fixes the stored shapes of
acceptance criteria and budget reservations with no behaviour, and a node transition
writes its status row and emits its event through that run's own sink.

The surface these tests pin, all in `agentsdk.plan` and exported from `agentsdk`:
  * `PlanNode`, `AcceptanceCriterion`, `BudgetReservation`, `NodeRetryPolicy` and
    `PlanVersion`: frozen, keyword-only dataclasses, validated at construction by field
    name. Every refusal raises `InvalidPlan`, a `ValueError` and an `AgentSDKError`.
  * `plan_from_document(document, *, plan_id, version, run_id, parent_plan=None,
    created_at=None)` and `PlanVersion.to_document()`, the planner's JSON form, which
    `PLAN_SCHEMA` (shipped at `PLAN_SCHEMA_PATH` inside the package) describes. A
    document the schema refuses raises `InvalidPlan` whose `path` is the failing
    location joined with "/", e.g. "nodes/1".
  * `plan_hash` is the SHA-256 hex digest of `to_document()` as canonical JSON
    (sorted keys, no whitespace, UTF-8), so it identifies what the plan says, not
    when or under which id it was stored.
  * `RunStateStore`, a protocol with async `put_plan`, `get_plan`, `versions`,
    `node_states` and `transition`; `InMemoryRunStateStore(tenant, project, *, runs=...)`
    with `for_scope`; and `Persistence.run_state_store(tenant, project)` on Postgres.
    A missing plan raises `PlanNotFound`.
  * `NODE_STATUSES` and the event types `EventType.PLAN_NODE_STARTED` ("PlanNodeStarted")
    and `EventType.PLAN_NODE_FINISHED` ("PlanNodeFinished").

The persisted half needs DATABASE_URL and fails rather than skips without it. Every row
written is removed, node states and plans before the runs they name, and no assertion
prints a credential. Names M16 adds are reached through their modules at call time, so
before the implementation each test fails on its own.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import importlib
import json
import os
import pathlib
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from functools import cache

import jsonschema
import psycopg
import pytest
from dotenv import load_dotenv

import agentsdk
from agentsdk import Persistence, migrate
from agentsdk.config import normalise_database_url
from agentsdk.errors import AgentSDKError
from agentsdk.events import EventType, InMemoryEventSink
from agentsdk.manifest import build_manifest
from agentsdk.migrate import apply_migrations
from agentsdk.postgres import SCHEMA_PATH, PostgresRunStore, RunScope

load_dotenv()

DSN = normalise_database_url(os.environ.get("DATABASE_URL"))
REPO = pathlib.Path(__file__).resolve().parents[1]
TENANT, PROJECT = "SYN-m16", "p-m16"
OTHER_TENANT, OTHER_PROJECT = "SYN-m16-other", "p-m16-other"
T0 = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
STATUSES = ("pending", "ready", "running", "done", "failed", "skipped", "cancelled")
TERMINAL = ("done", "failed", "skipped", "cancelled")
NODE_FIELDS = [
    "node_id", "objective", "dependencies", "assigned_role", "input_refs", "expected_output_schema",
    "acceptance_criteria", "budget_reservation", "side_effecting", "timeout", "retry_policy", "risk_class",
]
PLAN_FIELDS = ["plan_id", "version", "run_id", "nodes", "parent_plan", "created_at", "plan_hash"]
SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}


# --- shared helpers --------------------------------------------------------------------------------


def plan():
    return importlib.import_module("agentsdk.plan")


def invalid():
    return plan().InvalidPlan


def not_found():
    return plan().PlanNotFound


def node(node_id="a", **overrides):
    fields = dict(node_id=node_id, objective=f"do {node_id}", assigned_role="researcher")
    fields.update(overrides)
    return plan().PlanNode(**fields)


def rich_node(node_id="b", dependencies=("a",)):
    p = plan()
    return p.PlanNode(
        node_id=node_id,
        objective="summarise what a found",
        dependencies=dependencies,
        assigned_role="writer",
        input_refs=("artifact://notes",),
        expected_output_schema=SCHEMA,
        acceptance_criteria=(
            p.AcceptanceCriterion(kind="output_schema"),
            p.AcceptanceCriterion(kind="artifact_exists", target="artifact://summary"),
            p.AcceptanceCriterion(kind="tool_succeeds", target="check_links", arguments={"strict": True}),
            p.AcceptanceCriterion(kind="critic", description="the summary is faithful to the notes"),
        ),
        budget_reservation=p.BudgetReservation(usd=Decimal("0.50"), tokens=20_000),
        side_effecting=False,
        timeout=30.0,
        retry_policy=p.NodeRetryPolicy(max_attempts=2),
        risk_class="low",
    )


def version_of(run_id, *, plan_id=None, version=1, parent_plan=None, nodes=None):
    return plan().PlanVersion(
        plan_id=plan_id or str(uuid.uuid4()),
        version=version,
        run_id=run_id,
        nodes=nodes if nodes is not None else (node("a"), rich_node("b")),
        parent_plan=parent_plan,
        created_at=T0,
    )


def canonical_hash(document):
    text = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def query(sql, params=()):
    with psycopg.connect(DSN) as conn:
        return conn.execute(sql, params).fetchall()


def remove_rows():
    with psycopg.connect(DSN, autocommit=True) as conn:
        # Node states, then plans, then what the runs own: each references the next.
        for table in ("plan_node_states", "plan_versions"):
            if conn.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0] is not None:
                conn.execute(f"DELETE FROM {table} WHERE tenant_id LIKE 'SYN-m16%%'")
        ids = [row[0] for row in conn.execute("SELECT run_id FROM runs WHERE tenant_id LIKE 'SYN-m16%%'").fetchall()]
        for table in ("run_events", "messages", "execution_manifests"):
            conn.execute(f"DELETE FROM {table} WHERE run_id = ANY(%s)", (ids,))
        conn.execute("DELETE FROM runs WHERE run_id = ANY(%s)", (ids,))


@pytest.fixture(autouse=True)
def remove_what_the_test_wrote():
    yield
    if DSN:
        remove_rows()


@cache
def persistence():
    return Persistence.postgres(DSN)


def start_run(tenant, project):
    scope = RunScope(run_id=str(uuid.uuid4()), tenant_id=tenant, project_id=project)
    PostgresRunStore(DSN).start_run(
        scope, agent_spec_id="m16", max_turns=1, model_id=None, principal_context=None,
        manifest=build_manifest(sdk_version="m16", agent_spec_id="m16", instructions="i", tool_profile=(),
                                tool_spec_hashes=[], model_id=None),
    )
    return scope.run_id


class Stores:
    """Builds either kind of store. The in-memory one admits exactly the runs this test made."""

    def __init__(self, kind):
        self.kind = kind
        self.known_runs = set()

    def run(self, tenant=TENANT, project=PROJECT):
        run_id = start_run(tenant, project) if self.kind == "postgres" else str(uuid.uuid4())
        self.known_runs.add((tenant, project, run_id))
        return run_id

    def store(self, tenant=TENANT, project=PROJECT):
        if self.kind == "postgres":
            return persistence().run_state_store(tenant, project)
        return plan().InMemoryRunStateStore(tenant, project, runs=lambda t, p, r: (t, p, r) in self.known_runs)

    def sink(self, run_id, tenant=TENANT, project=PROJECT):
        if self.kind == "postgres":
            return persistence().event_sink_for(RunScope(run_id=run_id, tenant_id=tenant, project_id=project))
        return InMemoryEventSink(tenant, project, run_id)

    def events(self, run_id, sink):
        """(event_type, sequence_no, task_id, payload) for every event of the run, in order."""
        if self.kind == "postgres":
            return [
                (row[0], row[1], row[2], row[3])
                for row in query(
                    "SELECT event_type, sequence_no, task_id, payload FROM run_events WHERE run_id=%s ORDER BY sequence_no",
                    (run_id,),
                )
            ]
        return [(e.event_type.value, e.sequence_no, e.task_id, dict(e.payload)) for e in sink.events()]


@pytest.fixture(params=["memory", "postgres"])
def stores(request):
    if request.param == "postgres":
        assert DSN, "the persisted half needs DATABASE_URL and fails rather than skips"
    return Stores(request.param)


def run(coroutine):
    return asyncio.run(coroutine)


# =================================================================================================
# FR-64: the public surface and the node's shape
# =================================================================================================


def test_the_new_public_names_exist():
    names = (
        "PlanNode", "PlanVersion", "AcceptanceCriterion", "BudgetReservation", "NodeRetryPolicy",
        "RunStateStore", "InMemoryRunStateStore", "InvalidPlan", "PlanNotFound", "plan_from_document",
    )
    for name in names:
        assert name in agentsdk.__all__ and hasattr(agentsdk, name), name
    assert issubclass(plan().InvalidPlan, ValueError) and issubclass(plan().InvalidPlan, AgentSDKError)
    assert issubclass(plan().PlanNotFound, AgentSDKError)
    assert "PlanIntegrityError" in agentsdk.__all__ and issubclass(plan().PlanIntegrityError, AgentSDKError)
    assert plan().NODE_STATUSES == STATUSES
    assert EventType.PLAN_NODE_STARTED.value == "PlanNodeStarted"
    assert EventType.PLAN_NODE_FINISHED.value == "PlanNodeFinished"


def test_a_plan_node_is_frozen_keyword_only_and_ordered_as_fr64_lists_it():
    p = plan()
    for kind in (p.PlanNode, p.PlanVersion, p.AcceptanceCriterion, p.BudgetReservation, p.NodeRetryPolicy):
        assert dataclasses.is_dataclass(kind) and kind.__dataclass_params__.frozen, kind
    assert [f.name for f in dataclasses.fields(p.PlanNode)] == NODE_FIELDS
    assert [f.name for f in dataclasses.fields(p.PlanVersion)] == PLAN_FIELDS
    with pytest.raises(TypeError):
        p.PlanNode("a", "do a", (), "researcher")  # keyword-only
    minimal = node("a")
    assert minimal.side_effecting is True, "P2-D18: the unsafe case is the default"
    assert (minimal.dependencies, minimal.input_refs, minimal.acceptance_criteria) == ((), (), ())
    assert (minimal.expected_output_schema, minimal.budget_reservation, minimal.timeout,
            minimal.retry_policy, minimal.risk_class) == (None, None, None, None, None)


BAD_NODE_FIELDS = [
    ("node_id", ""), ("node_id", 7), ("node_id", "\ud800"),
    ("objective", ""), ("objective", None), ("objective", "bad \x00 text"),
    ("assigned_role", ""), ("assigned_role", b"writer"),
    ("dependencies", "a"), ("dependencies", ("x", "x")), ("dependencies", ("",)), ("dependencies", (3,)),
    ("input_refs", "artifact://x"), ("input_refs", ("",)),
    ("expected_output_schema", ["not", "a", "mapping"]), ("expected_output_schema", {"type": "no-such-type"}),
    ("expected_output_schema", {"\ud800": 1}),
    ("acceptance_criteria", ("output_schema",)), ("acceptance_criteria", "output_schema"),
    ("budget_reservation", Decimal("1")), ("budget_reservation", {"usd": "1"}),
    ("side_effecting", 1), ("side_effecting", None),
    ("timeout", 0), ("timeout", -1.0), ("timeout", True), ("timeout", float("nan")), ("timeout", float("inf")),
    ("retry_policy", 3),
    ("risk_class", ""), ("risk_class", "\ud800"),
]


@pytest.mark.parametrize(("field", "value"), BAD_NODE_FIELDS, ids=[f"{f}-{i}" for i, (f, _) in enumerate(BAD_NODE_FIELDS)])
def test_every_invalid_node_field_is_refused_by_field_name(field, value):
    fields = dict(node_id="a", objective="do a", assigned_role="researcher")
    fields[field] = value
    with pytest.raises(invalid(), match=field):
        plan().PlanNode(**fields)


def test_criteria_have_four_kinds_each_with_exactly_its_own_fields():
    C = plan().AcceptanceCriterion
    assert plan().CRITERION_KINDS == ("output_schema", "artifact_exists", "tool_succeeds", "critic")
    C(kind="output_schema")
    C(kind="artifact_exists", target="artifact://x")
    C(kind="tool_succeeds", target="check", arguments={"a": 1})
    C(kind="tool_succeeds", target="check")  # arguments default to none at all
    C(kind="critic", description="judged later")
    refused = [
        dict(kind="unknown"), dict(kind=""),
        dict(kind="artifact_exists"), dict(kind="artifact_exists", target=""),
        dict(kind="tool_succeeds"), dict(kind="tool_succeeds", target="t", arguments=["a"]),
        dict(kind="critic"), dict(kind="critic", description=""),
        # A field that belongs to another kind is a misconfiguration, not an extra.
        dict(kind="output_schema", target="x"), dict(kind="artifact_exists", target="x", arguments={}),
        dict(kind="critic", description="d", target="x"), dict(kind="tool_succeeds", target="t", description="d"),
        dict(kind="tool_succeeds", target="t", arguments={"\ud800": 1}),
    ]
    for fields in refused:
        with pytest.raises(invalid()):
            C(**fields)


def test_a_reservation_is_a_positive_decimal_or_token_count_and_never_empty():
    R = plan().BudgetReservation
    assert R(usd=Decimal("0.25")).tokens is None and R(tokens=10).usd is None
    for fields in (
        dict(), dict(usd=None, tokens=None),
        dict(usd=0.25), dict(usd="0.25"), dict(usd=Decimal("0")), dict(usd=Decimal("-1")),
        dict(usd=Decimal("NaN")), dict(usd=Decimal("Infinity")),
        dict(tokens=0), dict(tokens=True), dict(tokens=1.5), dict(tokens=2**31),
    ):
        with pytest.raises(invalid()):
            R(**fields)
    for bad in (0, -1, True, 2.0, 2**31):
        with pytest.raises(invalid(), match="max_attempts"):
            plan().NodeRetryPolicy(max_attempts=bad)


@pytest.mark.parametrize("usd", ["0.50", "100", "1E+2", "1E-7", "0.0000001", "12345678901234567890.5"])
def test_every_valid_reservation_is_read_back_from_the_store(stores, usd):
    """str(Decimal) switches to exponent form for some values, which the document's
    price pattern refuses: a plan that stores must also read back."""
    run_id = stores.run()
    store = stores.store()
    built = version_of(run_id, nodes=(node("a", budget_reservation=plan().BudgetReservation(usd=Decimal(usd))),))
    jsonschema.validate(built.to_document(), plan().PLAN_SCHEMA)
    run(store.put_plan(built))
    again = run(store.get_plan(built.plan_id, 1))
    assert again == built and again.nodes[0].budget_reservation.usd == Decimal(usd)


# DECISION-6d073ac0, F3: JSONB rewrote these numbers (1e16 came back as the integer
# 10000000000000000, -0.0 as 0), so the plan read back hashed differently from the one
# stored. The controls beside them were never affected and must stay so.
JSON_VALUES = {
    "1e16": {"minimum": 1e16}, "2.5e16": {"minimum": 2.5e16}, "1e300": {"minimum": 1e300},
    "-0.0": {"minimum": -0.0}, "5e-324": {"minimum": 5e-324}, "0.1": {"minimum": 0.1},
    "100.0": {"minimum": 100.0}, "10**100": {"minimum": 10**100}, "unicode": {"title": "caf\u00e9 \U0001F600"},
}


@pytest.mark.parametrize("label", list(JSON_VALUES))
def test_every_json_value_reads_back_exactly_with_its_hash(stores, label):
    run_id = stores.run()
    store = stores.store()
    schema = {"type": "number", **JSON_VALUES[label]}
    built = version_of(run_id, nodes=(
        node("a", expected_output_schema=schema),
        node("b", acceptance_criteria=(plan().AcceptanceCriterion(kind="tool_succeeds", target="t", arguments=schema),)),
    ))
    run(store.put_plan(built))
    again = run(store.get_plan(built.plan_id, 1))
    assert again.plan_hash == built.plan_hash, "AC-51: the hash must not change on the way through the store"
    assert canonical_hash(again.to_document()) == built.plan_hash
    assert json.dumps(again.to_document(), sort_keys=True) == json.dumps(built.to_document(), sort_keys=True)
    assert again == built


def test_a_document_edited_in_the_database_is_refused_on_read():
    assert DSN, "DATABASE_URL must be set"
    stores = Stores("postgres")
    run_id = stores.run()
    store = stores.store()
    built = version_of(run_id)
    run(store.put_plan(built))
    with psycopg.connect(DSN, autocommit=True) as conn:
        changed = conn.execute(
            "UPDATE plan_versions SET document = replace(document, 'do a', 'TAMPERED')"
            " WHERE plan_id = %s AND version = 1 AND tenant_id = %s", (built.plan_id, TENANT),
        ).rowcount
    assert changed == 1
    for read in (lambda: store.get_plan(built.plan_id, 1), lambda: store.versions(built.plan_id)):
        with pytest.raises(plan().PlanIntegrityError, match=built.plan_id):
            run(read())


@pytest.mark.parametrize("depth", [200, 800])
def test_deep_nesting_is_refused_as_an_invalid_plan(depth):
    """DECISION-6d073ac0, F5: every refusal is InvalidPlan, never a RecursionError."""
    schema = {"type": "string"}
    for _ in range(depth):
        schema = {"type": "array", "items": schema}
    with pytest.raises(invalid(), match="expected_output_schema.*deep"):
        node("a", expected_output_schema=schema)
    value = 1
    for _ in range(depth):
        value = [value]
    with pytest.raises(invalid(), match="arguments.*deep"):
        plan().AcceptanceCriterion(kind="tool_succeeds", target="t", arguments={"n": value})
    document = {"nodes": [{"node_id": "a", "objective": "o", "assigned_role": "r", "expected_output_schema": schema}]}
    with pytest.raises(invalid()) as refused:
        plan().plan_from_document(document, plan_id=str(uuid.uuid4()), version=1, run_id=str(uuid.uuid4()))
    assert refused.value.path == "nodes/0"


def test_a_moderately_nested_schema_is_still_accepted():
    schema = {"type": "string"}
    for _ in range(50):
        schema = {"type": "array", "items": schema}
    assert node("a", expected_output_schema=schema).expected_output_schema["type"] == "array"


def test_a_node_is_deeply_immutable_and_owns_its_copies():
    schema = json.loads(json.dumps(SCHEMA))
    arguments = {"strict": True, "tags": ["a"]}
    built = node(
        "a",
        expected_output_schema=schema,
        acceptance_criteria=(plan().AcceptanceCriterion(kind="tool_succeeds", target="t", arguments=arguments),),
    )
    schema["required"].append("injected")
    arguments["tags"].append("injected")
    assert list(built.expected_output_schema["required"]) == ["summary"]
    assert "injected" not in json.dumps(plan().PlanVersion(
        plan_id=str(uuid.uuid4()), version=1, run_id=str(uuid.uuid4()), nodes=(built,), created_at=T0,
    ).to_document())
    with pytest.raises(TypeError):
        built.expected_output_schema["type"] = "array"
    with pytest.raises((TypeError, AttributeError)):
        built.expected_output_schema["properties"]["summary"]["type"] = "integer"
    with pytest.raises((TypeError, AttributeError)):
        built.acceptance_criteria[0].arguments["tags"].append("x")


# =================================================================================================
# FR-64, AC-51: the plan is a DAG and says so at construction
# =================================================================================================


def test_a_dependency_on_an_unknown_node_is_refused_naming_both():
    with pytest.raises(invalid(), match=r"(?s)(?=.*\bb\b)(?=.*\bghost\b)"):
        version_of(str(uuid.uuid4()), nodes=(node("a"), node("b", dependencies=("ghost",))))


@pytest.mark.parametrize("edges", [
    {"a": ("a",)},
    {"a": ("b",), "b": ("a",)},
    {"a": ("c",), "b": ("a",), "c": ("b",)},
])
def test_a_cycle_is_refused_naming_a_node_on_it(edges):
    nodes = tuple(node(n, dependencies=deps) for n, deps in edges.items())
    with pytest.raises(invalid(), match="cycle") as refused:
        version_of(str(uuid.uuid4()), nodes=nodes)
    assert any(re.search(rf"\b{n}\b", str(refused.value)) for n in edges), str(refused.value)


def test_duplicate_node_ids_and_an_empty_plan_are_refused():
    with pytest.raises(invalid(), match=r"\ba\b"):
        version_of(str(uuid.uuid4()), nodes=(node("a"), node("a")))
    with pytest.raises(invalid(), match="nodes"):
        version_of(str(uuid.uuid4()), nodes=())
    with pytest.raises(invalid(), match="nodes"):
        version_of(str(uuid.uuid4()), nodes=("a",))


@pytest.mark.parametrize(("field", "value"), [
    ("plan_id", "not-a-uuid"), ("plan_id", ""), ("run_id", "not-a-uuid"),
    ("version", 0), ("version", True), ("version", "1"), ("version", 2**31),
    ("parent_plan", "x"), ("parent_plan", (str(uuid.uuid4()),)), ("parent_plan", ("not-a-uuid", 1)),
    ("parent_plan", (str(uuid.uuid4()), 0)),
    ("created_at", datetime(2026, 9, 19, 12, 0)), ("created_at", "2026-09-19"),
])
def test_every_invalid_plan_field_is_refused_by_field_name(field, value):
    fields = dict(plan_id=str(uuid.uuid4()), version=2, run_id=str(uuid.uuid4()), nodes=(node("a"),), created_at=T0)
    fields[field] = value
    with pytest.raises(invalid(), match=field):
        plan().PlanVersion(**fields)


# =================================================================================================
# FR-65: the planner's document, its schema and the plan's hash
# =================================================================================================


def test_the_schema_ships_inside_the_package_and_is_a_valid_json_schema():
    p = plan()
    path = pathlib.Path(p.PLAN_SCHEMA_PATH)
    assert path.is_file() and path.resolve().is_relative_to(pathlib.Path(agentsdk.__file__).resolve().parent), path
    assert json.loads(path.read_text(encoding="utf-8")) == p.PLAN_SCHEMA
    jsonschema.Draft202012Validator.check_schema(p.PLAN_SCHEMA)
    patterns = re.search(r"agentsdk = \[([^\]]*)\]", (REPO / "pyproject.toml").read_text(encoding="utf-8")).group(1)
    relative = path.resolve().relative_to(pathlib.Path(agentsdk.__file__).resolve().parent).as_posix()
    assert any(pathlib.PurePosixPath(relative).match(pattern.strip().strip('"')) for pattern in patterns.split(",")), (
        f"{relative} is not in the wheel's package data: {patterns}"
    )


def test_a_plan_round_trips_through_its_document_and_the_document_meets_the_schema():
    original = version_of(str(uuid.uuid4()))
    document = original.to_document()
    jsonschema.validate(document, plan().PLAN_SCHEMA)
    json.dumps(document)  # plain JSON: no Decimal, tuple or mapping proxy leaks out
    again = plan().plan_from_document(
        json.loads(json.dumps(document)), plan_id=original.plan_id, version=1, run_id=original.run_id, created_at=T0,
    )
    assert again == original
    assert again.plan_hash == original.plan_hash == canonical_hash(document)
    assert again.nodes[1].budget_reservation.usd == Decimal("0.50")


def test_the_hash_names_the_content_not_the_storage():
    run_id = str(uuid.uuid4())
    first = version_of(run_id)
    same_content = version_of(str(uuid.uuid4()), version=3, parent_plan=(first.plan_id, 1))
    changed = version_of(run_id, nodes=(node("a", objective="something else"), rich_node("b")))
    assert first.plan_hash == same_content.plan_hash
    assert changed.plan_hash != first.plan_hash
    assert re.fullmatch(r"[0-9a-f]{64}", first.plan_hash)


def test_a_minimal_planner_document_takes_the_defaults():
    built = plan().plan_from_document(
        {"nodes": [{"node_id": "a", "objective": "do a", "assigned_role": "researcher"}]},
        plan_id=str(uuid.uuid4()), version=1, run_id=str(uuid.uuid4()),
    )
    assert built.nodes == (node("a"),)
    assert built.created_at.tzinfo is not None


@pytest.mark.parametrize(("document", "path"), [
    ({}, ""),
    ({"nodes": "a"}, "nodes"),
    ({"nodes": [{"node_id": "a", "objective": "o", "assigned_role": "r"}, {"node_id": "b", "objective": "o"}]}, "nodes/1"),
    ({"nodes": [{"node_id": "a", "objective": "o", "assigned_role": "r", "side_effecting": "yes"}]}, "nodes/0/side_effecting"),
    ({"nodes": [{"node_id": "a", "objective": "o", "assigned_role": "r", "surprise": 1}]}, "nodes/0"),
    ({"nodes": [{"node_id": "a", "objective": "o", "assigned_role": "r",
                 "acceptance_criteria": [{"kind": "vibes"}]}]}, "nodes/0/acceptance_criteria/0/kind"),
    ({"nodes": [{"node_id": "a", "objective": "o", "assigned_role": "r",
                 "budget_reservation": {"usd": 0.5}}]}, "nodes/0/budget_reservation/usd"),
])
def test_a_document_the_schema_refuses_names_the_failing_path(document, path):
    with pytest.raises(invalid()) as refused:
        plan().plan_from_document(document, plan_id=str(uuid.uuid4()), version=1, run_id=str(uuid.uuid4()))
    assert refused.value.path == path, (refused.value.path, str(refused.value))


def test_a_document_that_meets_the_schema_is_still_checked_as_a_dag():
    document = {"nodes": [
        {"node_id": "a", "objective": "o", "assigned_role": "r", "dependencies": ["b"]},
        {"node_id": "b", "objective": "o", "assigned_role": "r", "dependencies": ["a"]},
    ]}
    jsonschema.validate(document, plan().PLAN_SCHEMA)
    with pytest.raises(invalid(), match="cycle") as refused:
        plan().plan_from_document(document, plan_id=str(uuid.uuid4()), version=1, run_id=str(uuid.uuid4()))
    assert refused.value.path == "nodes", refused.value.path


def test_a_node_the_schema_admits_but_construction_refuses_names_its_place():
    # The schema checks shape only; that a critic needs a description is a rule of the type.
    document = {"nodes": [
        {"node_id": "a", "objective": "o", "assigned_role": "r"},
        {"node_id": "b", "objective": "o", "assigned_role": "r", "acceptance_criteria": [{"kind": "critic"}]},
    ]}
    jsonschema.validate(document, plan().PLAN_SCHEMA)
    with pytest.raises(invalid(), match="description") as refused:
        plan().plan_from_document(document, plan_id=str(uuid.uuid4()), version=1, run_id=str(uuid.uuid4()))
    assert refused.value.path == "nodes/1", refused.value.path


def test_a_document_is_copied_so_the_planner_cannot_change_a_built_plan():
    document = {"nodes": [{"node_id": "a", "objective": "do a", "assigned_role": "researcher",
                           "expected_output_schema": json.loads(json.dumps(SCHEMA))}]}
    built = plan().plan_from_document(document, plan_id=str(uuid.uuid4()), version=1, run_id=str(uuid.uuid4()), created_at=T0)
    before = built.plan_hash
    document["nodes"][0]["objective"] = "changed"
    document["nodes"][0]["expected_output_schema"]["required"].append("x")
    assert built.nodes[0].objective == "do a" and built.plan_hash == before == canonical_hash(built.to_document())


# =================================================================================================
# FR-65, AC-51: storing a plan, on both stores
# =================================================================================================


def test_a_plan_round_trips_and_a_replan_never_touches_the_earlier_version(stores):
    run_id = stores.run()
    store = stores.store()
    first = version_of(run_id)
    run(store.put_plan(first))
    assert run(store.get_plan(first.plan_id, 1)) == first

    replanned = version_of(run_id, plan_id=first.plan_id, version=2, parent_plan=(first.plan_id, 1),
                           nodes=(node("a"), rich_node("b"), node("c", dependencies=("b",))))
    run(store.put_plan(replanned))
    got_first, got_second = run(store.get_plan(first.plan_id, 1)), run(store.get_plan(first.plan_id, 2))
    assert got_first == first and got_first.plan_hash == first.plan_hash
    assert got_second == replanned and got_second.parent_plan == (first.plan_id, 1)
    assert run(store.get_plan(*got_second.parent_plan)) == first
    assert run(store.versions(first.plan_id)) == (first, replanned)


def test_a_stored_version_cannot_be_overwritten(stores):
    run_id = stores.run()
    store = stores.store()
    first = version_of(run_id)
    run(store.put_plan(first))
    impostor = version_of(run_id, plan_id=first.plan_id, nodes=(node("z"),))
    with pytest.raises(invalid(), match="already"):
        run(store.put_plan(impostor))
    assert run(store.get_plan(first.plan_id, 1)) == first


def test_a_plan_needs_a_run_in_scope_and_a_parent_that_exists(stores):
    run_id = stores.run()
    store = stores.store()
    other_run = stores.run(OTHER_TENANT, OTHER_PROJECT)
    with pytest.raises(invalid(), match="run"):
        run(store.put_plan(version_of(other_run)))
    with pytest.raises(invalid(), match="run"):
        run(store.put_plan(version_of(str(uuid.uuid4()))))
    orphan = version_of(run_id, version=2, parent_plan=(str(uuid.uuid4()), 1))
    with pytest.raises(invalid(), match="parent"):
        run(store.put_plan(orphan))
    with pytest.raises(not_found()):
        run(store.get_plan(orphan.plan_id, 2))


def test_a_replan_stays_inside_its_run(stores):
    """FR-75: a replan is inside the same run, so its parent must be a version of that run."""
    first_run, second_run = stores.run(), stores.run()
    store = stores.store()
    first = version_of(first_run)
    run(store.put_plan(first))
    elsewhere = version_of(second_run, plan_id=first.plan_id, version=2, parent_plan=(first.plan_id, 1))
    with pytest.raises(invalid(), match="parent"):
        run(store.put_plan(elsewhere))
    assert run(store.versions(first.plan_id)) == (first,)


def test_a_plan_id_is_scoped_so_another_tenant_can_neither_block_nor_detect_it(stores):
    """DECISION-6d073ac0, F1: plan identity is tenant, project, plan_id and version."""
    mine, theirs = stores.run(), stores.run(OTHER_TENANT, OTHER_PROJECT)
    store = stores.store()
    # In memory, two separately built stores share nothing, which would make this vacuous.
    other = store.for_scope(OTHER_TENANT, OTHER_PROJECT) if stores.kind == "memory" else stores.store(OTHER_TENANT, OTHER_PROJECT)
    first = version_of(mine)
    run(store.put_plan(first))
    # The other tenant reuses the id for versions 1 and 2 of its own plan: accepted as
    # its own, with no "already stored" to tell it this tenant's plan exists.
    their_first = version_of(theirs, plan_id=first.plan_id, nodes=(node("x"),))
    their_second = version_of(theirs, plan_id=first.plan_id, version=2, nodes=(node("y"),))
    run(other.put_plan(their_first))
    run(other.put_plan(their_second))
    replanned = version_of(mine, plan_id=first.plan_id, version=2, parent_plan=(first.plan_id, 1))
    run(store.put_plan(replanned))
    assert run(store.versions(first.plan_id)) == (first, replanned)
    assert run(other.versions(first.plan_id)) == (their_first, their_second)
    run(other.transition(first.plan_id, 1, "x", "ready", sink=stores.sink(theirs, OTHER_TENANT, OTHER_PROJECT)))
    assert dict(run(store.node_states(first.plan_id, 1))) == {"a": "pending", "b": "pending"}
    assert dict(run(other.node_states(first.plan_id, 1))) == {"x": "ready"}


def test_a_plan_id_belongs_to_one_run(stores):
    """DECISION-6d073ac0, F1: another run of the same scope cannot take a free version
    number under an existing plan and so block that plan's replan."""
    mine, another = stores.run(), stores.run()
    store = stores.store()
    first = version_of(mine)
    run(store.put_plan(first))
    squatter = version_of(another, plan_id=first.plan_id, version=2, nodes=(node("z"),))
    with pytest.raises(invalid(), match="run"):
        run(store.put_plan(squatter))
    run(store.put_plan(version_of(mine, plan_id=first.plan_id, version=2, parent_plan=(first.plan_id, 1))))


def test_racing_runs_cannot_both_claim_a_new_plan_id(stores):
    """Two runs storing versions of one new plan at once: whichever claims it first owns
    it, and every version the other tries is refused, so a plan never has two runs."""
    runs = (stores.run(), stores.run())
    store = stores.store()

    async def race(plan_id):
        attempts = [version_of(runs[i % 2], plan_id=plan_id, version=i + 1, nodes=(node("a"),)) for i in range(6)]
        return await asyncio.gather(*(store.put_plan(p) for p in attempts), return_exceptions=True)

    for _ in range(5):
        plan_id = str(uuid.uuid4())
        outcomes = run(race(plan_id))
        unexpected = [o for o in outcomes if o is not None and not isinstance(o, invalid())]
        assert not unexpected, unexpected
        owners = {p.run_id for p in run(store.versions(plan_id))}
        assert len(owners) == 1, owners


def test_another_tenant_cannot_see_a_plan(stores):
    run_id = stores.run()
    store = stores.store()
    first = version_of(run_id)
    run(store.put_plan(first))
    other = store.for_scope(OTHER_TENANT, OTHER_PROJECT) if stores.kind == "memory" else stores.store(OTHER_TENANT, OTHER_PROJECT)
    with pytest.raises(not_found()):
        run(other.get_plan(first.plan_id, 1))
    assert run(other.versions(first.plan_id)) == ()
    with pytest.raises(not_found()):
        run(other.node_states(first.plan_id, 1))
    with pytest.raises(not_found()):
        run(other.transition(first.plan_id, 1, "a", "ready", sink=stores.sink(run_id)))
    assert run(store.node_states(first.plan_id, 1))["a"] == "pending"


def test_on_postgres_the_rows_are_scoped_and_the_earlier_row_is_unchanged_by_a_replan():
    assert DSN, "DATABASE_URL must be set"
    stores = Stores("postgres")
    run_id = stores.run()
    store = stores.store()
    first = version_of(run_id)
    run(store.put_plan(first))
    row_sql = "SELECT * FROM plan_versions WHERE plan_id=%s AND version=%s"
    before = query(row_sql, (first.plan_id, 1))
    run(store.put_plan(version_of(run_id, plan_id=first.plan_id, version=2, parent_plan=(first.plan_id, 1))))
    assert query(row_sql, (first.plan_id, 1)) == before
    scoped = query(
        "SELECT DISTINCT tenant_id, project_id FROM plan_versions WHERE plan_id=%s UNION "
        "SELECT DISTINCT tenant_id, project_id FROM plan_node_states WHERE plan_id=%s",
        (first.plan_id, first.plan_id),
    )
    assert scoped == [(TENANT, PROJECT)]


# =================================================================================================
# FR-66: node status and its events, on both stores (DECISION-8f8cc54c)
# =================================================================================================


def test_every_node_starts_pending_and_reads_back_exactly_as_written(stores):
    run_id = stores.run()
    store, sink = stores.store(), stores.sink(run_id)
    first = version_of(run_id)
    run(store.put_plan(first))
    assert dict(run(store.node_states(first.plan_id, 1))) == {"a": "pending", "b": "pending"}
    for status in ("ready", "running", "failed"):
        run(store.transition(first.plan_id, 1, "a", status, sink=sink))
        assert run(store.node_states(first.plan_id, 1))["a"] == status
    assert run(store.node_states(first.plan_id, 1))["b"] == "pending"


def test_starting_and_finishing_a_node_emit_events_carrying_the_node_in_task_id(stores):
    run_id = stores.run()
    store, sink = stores.store(), stores.sink(run_id)
    first = version_of(run_id)
    run(store.put_plan(first))
    before = len(stores.events(run_id, sink))
    run(store.transition(first.plan_id, 1, "a", "ready", sink=sink))
    run(store.transition(first.plan_id, 1, "a", "running", sink=sink))
    run(store.transition(first.plan_id, 1, "a", "done", sink=sink))
    run(store.transition(first.plan_id, 1, "b", "skipped", sink=sink))
    emitted = stores.events(run_id, sink)[before:]
    assert [(kind, task) for kind, _, task, _ in emitted] == [
        ("PlanNodeStarted", "a"), ("PlanNodeFinished", "a"), ("PlanNodeFinished", "b"),
    ], "ready writes a row and emits nothing"
    for _, _, _, payload in emitted:
        assert payload["plan_id"] == first.plan_id and payload["version"] == 1
    assert [payload["status"] for *_, payload in emitted] == ["running", "done", "skipped"]
    numbers = [number for _, number, _, _ in stores.events(run_id, sink)]
    assert numbers == list(range(1, len(numbers) + 1))


@pytest.mark.parametrize("final", TERMINAL)
def test_a_finished_node_is_final(stores, final):
    run_id = stores.run()
    store, sink = stores.store(), stores.sink(run_id)
    first = version_of(run_id)
    run(store.put_plan(first))
    run(store.transition(first.plan_id, 1, "a", final, sink=sink))
    for status in STATUSES:
        with pytest.raises(invalid(), match="final"):
            run(store.transition(first.plan_id, 1, "a", status, sink=sink))
    assert run(store.node_states(first.plan_id, 1))["a"] == final


def test_a_transition_names_a_real_status_a_real_node_and_never_returns_to_pending(stores):
    run_id = stores.run()
    store, sink = stores.store(), stores.sink(run_id)
    first = version_of(run_id)
    run(store.put_plan(first))
    for status in ("", "Running", "paused", None):
        with pytest.raises(invalid(), match="status"):
            run(store.transition(first.plan_id, 1, "a", status, sink=sink))
    with pytest.raises(not_found(), match="ghost"):
        run(store.transition(first.plan_id, 1, "ghost", "ready", sink=sink))
    with pytest.raises(not_found()):
        run(store.transition(first.plan_id, 9, "a", "ready", sink=sink))
    run(store.transition(first.plan_id, 1, "a", "ready", sink=sink))
    with pytest.raises(invalid(), match="pending"):
        run(store.transition(first.plan_id, 1, "a", "pending", sink=sink))
    assert dict(run(store.node_states(first.plan_id, 1))) == {"a": "ready", "b": "pending"}


def test_node_states_belong_to_their_version(stores):
    run_id = stores.run()
    store, sink = stores.store(), stores.sink(run_id)
    first = version_of(run_id)
    run(store.put_plan(first))
    run(store.transition(first.plan_id, 1, "a", "done", sink=sink))
    run(store.put_plan(version_of(run_id, plan_id=first.plan_id, version=2, parent_plan=(first.plan_id, 1))))
    assert run(store.node_states(first.plan_id, 1))["a"] == "done"
    assert run(store.node_states(first.plan_id, 2))["a"] == "pending"


# =================================================================================================
# NFR-22: concurrent transitions keep the record whole
# =================================================================================================


def test_concurrent_transitions_keep_sequence_numbers_unique_and_contiguous(stores):
    run_id = stores.run()
    store, sink = stores.store(), stores.sink(run_id)
    nodes = tuple(node(f"n{i}") for i in range(12))
    first = version_of(run_id, nodes=nodes)
    run(store.put_plan(first))

    async def drive(node_id):
        await store.transition(first.plan_id, 1, node_id, "running", sink=sink)
        await store.transition(first.plan_id, 1, node_id, "done", sink=sink)

    async def everything():
        await asyncio.gather(*(drive(n.node_id) for n in nodes))

    run(everything())
    events = stores.events(run_id, sink)
    numbers = [number for _, number, _, _ in events]
    assert numbers == list(range(1, len(numbers) + 1)) and len(numbers) == 24
    for n in nodes:
        mine = [kind for kind, _, task, _ in events if task == n.node_id]
        assert mine == ["PlanNodeStarted", "PlanNodeFinished"], (n.node_id, mine)
    assert set(run(store.node_states(first.plan_id, 1)).values()) == {"done"}


def test_racing_transitions_of_one_node_finish_it_exactly_once(stores):
    """Contenders race to finish each node: exactly one wins, the rest find it final,
    and each node emits exactly one PlanNodeFinished."""
    run_id = stores.run()
    store, sink = stores.store(), stores.sink(run_id)
    nodes = tuple(node(f"n{i}") for i in range(6))
    first = version_of(run_id, nodes=nodes)
    run(store.put_plan(first))

    async def contend():
        for n in nodes:
            await store.transition(first.plan_id, 1, n.node_id, "running", sink=sink)
        attempts = [(n.node_id, "done" if i % 2 else "failed") for n in nodes for i in range(8)]
        outcomes = await asyncio.gather(
            *(store.transition(first.plan_id, 1, node_id, status, sink=sink) for node_id, status in attempts),
            return_exceptions=True,
        )
        return attempts, outcomes

    attempts, outcomes = run(contend())
    unexpected = [o for o in outcomes if o is not None and not isinstance(o, invalid())]
    assert not unexpected, unexpected
    states = run(store.node_states(first.plan_id, 1))
    finished = [task for kind, _, task, _ in stores.events(run_id, sink) if kind == "PlanNodeFinished"]
    for n in nodes:
        won = [status for (node_id, status), o in zip(attempts, outcomes) if node_id == n.node_id and o is None]
        assert len(won) == 1, (n.node_id, won)
        assert states[n.node_id] == won[0]
        assert finished.count(n.node_id) == 1, (n.node_id, finished)


# =================================================================================================
# AC-52: migration 0007
# =================================================================================================


class Namespace:
    """A throwaway schema, as in M7 to M13: the live database is already migrated."""

    def __init__(self, baseline=True):
        self.baseline = baseline
        self.name = "m16_" + uuid.uuid4().hex[:8]
        self.dsn = DSN + ("&" if "?" in DSN else "?") + f"options=-csearch_path%3D{self.name}"

    def __enter__(self):
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'CREATE SCHEMA "{self.name}"')
            if self.baseline:
                conn.execute(f'SET search_path TO "{self.name}"')
                conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        return self

    def __exit__(self, *exc):
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{self.name}" CASCADE')
        return False

    def shape(self, table):
        with psycopg.connect(self.dsn) as conn:
            columns = set(conn.execute(
                "SELECT column_name, data_type, is_nullable FROM information_schema.columns WHERE table_schema=%s AND table_name=%s",
                (self.name, table),
            ).fetchall())
            constraints = set(conn.execute(
                "SELECT c.conname, pg_get_constraintdef(c.oid) FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid"
                " JOIN pg_namespace n ON n.oid = t.relnamespace WHERE n.nspname=%s AND t.relname=%s",
                (self.name, table),
            ).fetchall())
            indexes = {row[0].replace(f"{self.name}.", "") for row in conn.execute(
                "SELECT indexdef FROM pg_indexes WHERE schemaname=%s AND tablename=%s", (self.name, table)
            ).fetchall()}
        return columns, constraints, indexes


def test_migration_0007_adds_both_tables_scoped_to_a_database_that_holds_runs(monkeypatch):
    assert DSN, "DATABASE_URL must be set"
    real = migrate.discover()
    assert "0007" in [version for version, _ in real], "migration 0007 is not on disk"
    with Namespace() as ns:
        monkeypatch.setattr(migrate, "discover", lambda: [m for m in real if m[0] <= "0006"])
        assert apply_migrations(ns.dsn) == ["0002", "0003", "0004", "0005", "0006"]
        for table in ("plan_versions", "plan_node_states"):
            assert ns.shape(table) == (set(), set(), set()), f"{table} existed before 0007"
        held = str(uuid.uuid4())
        with psycopg.connect(ns.dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO runs (run_id, tenant_id, project_id, agent_spec_id, status, max_turns)"
                " VALUES (%s, %s, %s, 'm16', 'completed', 1)", (held, TENANT, PROJECT),
            )

        monkeypatch.setattr(migrate, "discover", lambda: real)
        assert apply_migrations(ns.dsn) == [v for v, _ in real if v > "0006"]
        with psycopg.connect(ns.dsn) as conn:
            assert conn.execute("SELECT count(*) FROM runs WHERE run_id=%s", (held,)).fetchone()[0] == 1
        for table in ("plan_versions", "plan_node_states"):
            columns, constraints, indexes = ns.shape(table)
            nullable = {name: flag for name, _, flag in columns}
            assert nullable.get("tenant_id") == "NO" and nullable.get("project_id") == "NO", (table, nullable)
            assert nullable.get("run_id") == "NO", (table, nullable)
            assert any(re.search(r"\(tenant_id, project_id\b", index) for index in indexes), (table, indexes)
            assert any("REFERENCES runs(run_id)" in definition for _, definition in constraints), (table, constraints)
        _, constraints, _ = ns.shape("plan_node_states")
        status_checks = [d for _, d in constraints if "CHECK" in d and "status" in d]
        assert status_checks and all(s in status_checks[0] for s in STATUSES), status_checks
        assert any("REFERENCES plan_versions" in d for _, d in constraints), constraints
        columns, constraints, _ = ns.shape("plan_versions")
        assert any("REFERENCES plan_versions" in d for _, d in constraints), "parent_plan must reference a stored version"
        # DECISION-6d073ac0: exact text, so no number is rewritten (F3), and a key that
        # carries the scope, so no tenant can take or detect another's plan id (F1).
        assert ("document", "text", "NO") in columns, columns
        keys = [d for _, d in constraints if d.startswith("PRIMARY KEY")]
        assert keys == ["PRIMARY KEY (tenant_id, project_id, plan_id, version)"], keys
        _, constraints, _ = ns.shape("plan_node_states")
        keys = [d for _, d in constraints if d.startswith("PRIMARY KEY")]
        assert keys == ["PRIMARY KEY (tenant_id, project_id, plan_id, version, node_id)"], keys
        assert apply_migrations(ns.dsn) == [], "a second application changed something"


def test_the_session_guard_watches_the_new_tables():
    conftest = (REPO / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert '("plan_versions", "run_id")' in conftest and '("plan_node_states", "run_id")' in conftest


# =================================================================================================
# The demo command: scripts/14_plan.py
# =================================================================================================


def test_the_example_builds_stores_and_replans_a_plan_offline(tmp_path):
    script = REPO / "scripts" / "14_plan.py"
    assert script.is_file(), "scripts/14_plan.py does not exist"
    env = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL", "BASE_URL", "MODEL_API_KEY")}
    done = subprocess.run(
        [sys.executable, str(script), "--offline"], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120,
    )
    assert done.returncode == 0, done.stdout[-1500:] + done.stderr[-1500:]
    out = done.stdout
    assert re.search(r"version 1\b", out) and re.search(r"version 2\b", out), out
    assert re.search(r"parent[^\n]*version 1\b", out), f"the replan does not show its parent link:\n{out}"
    assert re.search(r"[0-9a-f]{64}", out), "no plan_hash printed"
