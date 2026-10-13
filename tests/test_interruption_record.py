"""M29 gate: the carried items, and the interruption record (FR-111, FR-112, AC-84, AC-85, NFR-29).

An interruption and its decision are rows before anything waits on one (P4-D2): one row per
call that required approval, at most one decision per interruption -- the database itself
refuses a second -- and standing decisions ordered by a sequence. Every row is bound to its
run's tenant and project by a composite key, and nothing is ever updated or deleted. M29
changes no behaviour: nothing writes these tables until M30.

Store calls made from these async tests go to a worker thread (FR-81). The persisted half
needs DATABASE_URL and fails rather than skips without it; every row written is removed.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest

from agentsdk import AgentSpec, BudgetPolicy, ContextPolicy, Persistence, RunConfig, Runner
from agentsdk.calibration import calibrate
from agentsdk.config import normalise_database_url
from agentsdk.gate import EvidenceVerification, GatePolicy, NodeSubject
from agentsdk.identity import PrincipalContext
from agentsdk.interruptions import (
    InMemoryInterruptionStore,
    InterruptionAlreadyDecided,
    InterruptionDecision,
    InterruptionExpired,
    InterruptionNotWaiting,
    InterruptionView,
    StandingDecision,
    UnknownInterruption,
)
from agentsdk.loop import schema_problem
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.orchestrator import PLAN_TOOL, Orchestrator, _verdict_schema
from agentsdk.outcomes import InterruptionKind, RunInterruption
from agentsdk.primitives import ContentProvenance, Message, Role, ToolCall

REPO = Path(__file__).resolve().parent.parent
DSN = normalise_database_url(os.environ.get("DATABASE_URL"))
TENANT, PROJECT, OTHER_TENANT = "SYN-m29", "p-m29", "SYN-m29-other"
STORES = ["memory", "postgres"]
APPROVER = PrincipalContext(agent_principal="approval-ui", user_principal="alice")


@pytest.fixture(autouse=True)
def remove_what_the_test_wrote():
    yield
    if DSN:
        with psycopg.connect(DSN, autocommit=True) as conn:
            ids = [r[0] for r in conn.execute("SELECT run_id FROM runs WHERE tenant_id = ANY(%s)",
                                              ([TENANT, OTHER_TENANT],))]
            if conn.execute("SELECT to_regclass('interruption_decisions')").fetchone()[0] is not None:
                conn.execute("DELETE FROM interruption_decisions WHERE interruption_id IN (SELECT interruption_id"
                             " FROM interruptions WHERE run_id = ANY(%s))", (ids,))
                conn.execute("DELETE FROM interruptions WHERE run_id = ANY(%s)", (ids,))
                conn.execute("DELETE FROM standing_decisions WHERE run_id = ANY(%s)", (ids,))
            conn.execute("DELETE FROM verifications WHERE ledger_run_id = ANY(%s) OR subject_run = ANY(%s)", (ids, ids))
            conn.execute("DELETE FROM claim_citations WHERE contribution_id IN (SELECT contribution_id FROM "
                         "claim_contributions WHERE run_id = ANY(%s))", (ids,))
            conn.execute("DELETE FROM claim_contributions WHERE run_id = ANY(%s)", (ids,))
            while conn.execute("DELETE FROM claims c WHERE ledger_run_id = ANY(%s) AND NOT EXISTS "
                               "(SELECT 1 FROM claims s WHERE s.supersedes = c.claim_id)", (ids,)).rowcount:
                pass
            conn.execute("DELETE FROM source_versions WHERE source_run = ANY(%s)", (ids,))
            conn.execute("DELETE FROM artifacts WHERE source_run = ANY(%s)", (ids,))
            for table in ("plan_node_states", "plan_versions", "run_events", "messages", "execution_manifests"):
                conn.execute(f"DELETE FROM {table} WHERE run_id = ANY(%s)", (ids,))
            # Children first: a run's row is referenced by its children's parent_run_id.
            while conn.execute("DELETE FROM runs r WHERE run_id = ANY(%s) AND NOT EXISTS "
                               "(SELECT 1 FROM runs c WHERE c.parent_run_id = r.run_id)", (ids,)).rowcount:
                pass


def respond(content="", calls=()):
    return ModelResponse(message=Message(role=Role.ASSISTANT, content=content, tool_calls=tuple(calls)),
                         stop_reason=StopReason.TOOL_CALLS if calls else StopReason.END_TURN, usage=Usage(5, 1, 6))


class Answers:
    async def send(self, request):
        return respond("done")


def runner_for(store):
    return Runner({"m": Answers()},
                  persistence=Persistence.postgres(DSN, create_schema=False) if store == "postgres" else None)


async def run_in(runner, tenant=TENANT, parent=None):
    result = await runner.run(AgentSpec(id="agent", instructions="answer"), "go", RunConfig(
        tenant_id=tenant, project_id=PROJECT, model_override="m:x", parent_run_id=parent))
    return result.run_id


def interruption(run_id, *, top=None, tenant=TENANT, expires_in=3600, node_id=None, tool="write_file"):
    now = datetime.now(timezone.utc)
    return RunInterruption(
        kind=InterruptionKind.TOOL_APPROVAL, run_id=run_id, request_payload={"tool": tool, "arguments": {"path": "a"}},
        tool_call_id="call-1", created_at=now, tenant_id=tenant, project_id=PROJECT, top_run_id=top or run_id,
        node_id=node_id, tool_name=tool, agent_id="agent", trigger="policy", expires_at=now + timedelta(seconds=expires_in))


def decision(item, *, approve=True, source="principal", principal=APPROVER, message=None, standing_id=None):
    return InterruptionDecision(
        decision_id=str(uuid.uuid4()), interruption_id=item.interruption_id, tenant_id=item.tenant_id,
        project_id=item.project_id, decision="approve" if approve else "reject", source=source, message=message,
        principal=principal, standing_id=standing_id, decided_at=datetime.now(timezone.utc))


def standing(run_id, *, approve=True, tool="write_file"):
    return StandingDecision(
        standing_id=str(uuid.uuid4()), run_id=run_id, tenant_id=TENANT, project_id=PROJECT, agent_id="agent",
        tool_name=tool, decision="approve" if approve else "reject", principal=APPROVER, message=None,
        created_at=datetime.now(timezone.utc))


def store_of(runner):
    return runner._interruption_store()


async def call(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


# =================================================================================================
# FR-111, AC-84: the carried items
# =================================================================================================


def _strings(schema, path="$"):
    """Every string-typed node of a JSON Schema, with its path."""
    if isinstance(schema, dict):
        if schema.get("type") == "string":
            yield path, schema
        for key, value in schema.items():
            yield from _strings(value, f"{path}.{key}")
    elif isinstance(schema, list):
        for i, value in enumerate(schema):
            yield from _strings(value, f"{path}[{i}]")


@pytest.mark.parametrize("claims, sources", [([], []), (
    [f"urn:agentsdk:claim:{uuid.uuid4()}", f"urn:agentsdk:claim:{uuid.uuid4()}"], [f"urn:agentsdk:source:{uuid.uuid4()}"])])
def test_every_string_of_the_verdict_schema_carries_an_enum_or_a_pattern(claims, sources):
    unconstrained = [path for path, node in _strings(_verdict_schema(claims, sources, 4000))
                     if "enum" not in node and "pattern" not in node and "const" not in node]
    assert unconstrained == [], unconstrained


def test_a_corrected_answer_the_store_cannot_hold_fails_the_verdict_schema():
    claims, sources = [f"urn:agentsdk:claim:{uuid.uuid4()}"], [f"urn:agentsdk:source:{uuid.uuid4()}"]
    schema = _verdict_schema(claims, sources, 4000)

    def verdict(**fields):
        return json.dumps({"verdict": "corrected", "reason": "r", "corrections": [], **fields})

    for bad in ("a\u0000b", "a\ud800", "\udfff", " \n\t"):
        assert schema_problem(verdict(corrected_answer=bad), schema) is not None, repr(bad)
    assert schema_problem(verdict(corrected_answer="The Rhine, clearly."), schema) is None
    # Beside a verdict that does not use it, any storable text -- the empty string included
    # (M27, found live) -- but never one the store cannot hold.
    approved = {"verdict": "approved", "reason": "fine"}
    assert schema_problem(json.dumps({**approved, "corrected_answer": ""}), schema) is None
    assert schema_problem(json.dumps({**approved, "corrected_answer": "  "}), schema) is None
    assert schema_problem(json.dumps({**approved, "corrected_answer": "x\u0000"}), schema) is not None


LEDGER = str(uuid.uuid4())
T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _row(kind, subject, outcome, detail, minutes):
    return EvidenceVerification(
        verification_id=str(uuid.uuid4()), ledger_run_id=LEDGER, subject=subject, kind=kind, outcome=outcome,
        detail=detail, provenance=ContentProvenance.internal_tool(), tenant_id=TENANT, project_id=PROJECT,
        created_at=T0 + timedelta(minutes=minutes))


def _node(name):
    return NodeSubject(run_id=LEDGER, plan_id="11111111-1111-4111-8111-111111111111", version=1, node_id=name)


def _gate(name, claim, fired, minutes=1, confidence=0.3):
    return _row("gate", _node(name), "critic_required" if fired else "clear",
                {"claims": [{"claim": claim, "fired": list(fired), "values": {"lowest_confidence": confidence}}]
                 if claim else [], "fired": list(fired), "child_run": str(uuid.uuid4())}, minutes)


def _critic(name, claim, verdict, critic_run, minutes=2):
    detail = {"verdict": verdict, "reason": "r", "critic_run": critic_run, "applied": [], "refused": []}
    return [_row("critic", _node(name), verdict, detail, minutes), _row("critic", claim, verdict, detail, minutes)]


def test_the_report_counts_no_claims_by_gate_row_and_never_as_a_claim_signal():
    claim = f"urn:agentsdk:claim:{uuid.uuid4()}"
    rows = [_gate("a", claim, ["low_confidence"]), _gate("b", None, ["no_claims"]), _gate("c", None, ["no_claims"]),
            _gate("d", None, [])]
    made = calibrate(rows, tenant_id=TENANT, project_id=PROJECT, since=None, min_outcomes=1)
    assert "no_claims" not in made.signals
    assert made.no_claims_fired == 2
    assert made.to_json()["no_claims_fired"] == 2


def test_the_critic_table_counts_a_critic_row_by_its_own_node_executions_gate():
    claim = f"urn:agentsdk:claim:{uuid.uuid4()}"
    outcome = _row("external", claim, "incorrect", {"source": "fixture", "detail": "known"}, 9)
    critic_a = _critic("a", claim, "approved", str(uuid.uuid4()))
    # Node b's gate judged the same claim and is in range; node a's gate is not.
    without_own_gate = calibrate([_gate("b", claim, []), *critic_a, outcome], tenant_id=TENANT, project_id=PROJECT,
                                 since=None, min_outcomes=1)
    assert without_own_gate.critic["approved"] == {"correct": 0, "incorrect": 0, "no_outcome": 0}
    with_own_gate = calibrate([_gate("a", claim, ["low_confidence"]), _gate("b", claim, []), *critic_a, outcome],
                              tenant_id=TENANT, project_id=PROJECT, since=None, min_outcomes=1)
    assert with_own_gate.critic["approved"] == {"correct": 0, "incorrect": 1, "no_outcome": 0}


# NFR-29: an ungated plan sends the same requests and records the same events, pinned by a
# committed fixture of a fixed sequential plan.

FIXTURE = REPO / "tests" / "fixtures" / "nfr29_ungated_plan.json"
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# FR-57's timings and the cost fields: they differ by run, so they are excluded by name.
_VARYING = {"started_at", "finished_at", "duration_ms", "latency_ms", "elapsed_ms", "created_at", "timestamp",
            "occurred_at", "first_token_ms", "cost_usd", "budget_spend", "wall_ms", "queued_ms"}


def _plain(value):
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [_plain(v) for v in value]
        return sorted(items, key=json.dumps) if isinstance(value, (set, frozenset)) else items
    if hasattr(value, "value") and not isinstance(value, (str, int, float, bool)):
        return value.value
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _dropped(value):
    if isinstance(value, dict):
        return {k: _dropped(v) for k, v in value.items() if k not in _VARYING}
    if isinstance(value, list):
        return [_dropped(v) for v in value]
    return value


def normalised(record):
    """Every id substituted, inside text included, by a placeholder in order of first
    appearance; the varying fields dropped by name."""
    text = json.dumps(_dropped(_plain(record)), sort_keys=True)
    seen: dict[str, str] = {}
    return json.loads(_UUID.sub(lambda m: seen.setdefault(m.group(0), f"<id-{len(seen) + 1}>"), text))


class Recording:
    def __init__(self, client):
        self.client, self.requests = client, []

    async def send(self, request):
        self.requests.append(request)
        return await self.client.send(request)


class SequentialPlanner:
    async def send(self, request):
        if not any(m.role is Role.TOOL for m in request.messages):
            return respond(calls=[ToolCall(id="plan", name=PLAN_TOOL, arguments={"nodes": [
                {"node_id": "first", "objective": "say one", "assigned_role": "reader", "dependencies": []},
                {"node_id": "second", "objective": "say two", "assigned_role": "reader", "dependencies": ["first"]}]})])
        return respond("the plan is done")


class Writer:
    async def send(self, request):
        return respond("one" if "say one" in request.messages[0].content else "two")


async def _ungated_record(store, gate):
    orchestrator = Orchestrator(roles={"reader": AgentSpec(id="reader", instructions="read", preferred_model="r:r")},
                                policy=BudgetPolicy(run_ceiling_tokens=1_000_000),
                                context_policy=ContextPolicy(context_window=1_000_000), gate=gate)
    planner, reader = Recording(SequentialPlanner()), Recording(Writer())
    runner = Runner({"planner": planner, "r": reader}, tools=[orchestrator.tool],
                    persistence=Persistence.postgres(DSN, create_schema=False) if store == "postgres" else None)
    result = await runner.run(orchestrator.agent, "go", orchestrator.config(
        tenant_id=TENANT, project_id=PROJECT, model_override="planner:p", max_turns=4))
    return normalised({"planner": planner.requests, "reader": reader.requests,
                       "events": [{"type": e.event_type, "task_id": e.task_id, "payload": e.payload}
                                  for e in result.events]})


@pytest.mark.parametrize("store", STORES)
async def test_an_ungated_plan_sends_and_records_exactly_the_committed_fixture(store):
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert await _ungated_record(store, None) == expected
    # A GatePolicy that gates nothing changes nothing either (NFR-29); only the manifest's
    # gate_policy column differs, and it is not part of what a run sends or emits.
    assert await _ungated_record(store, GatePolicy()) == expected


# =================================================================================================
# FR-112, AC-85: the records
# =================================================================================================


def test_a_phase_0_interruption_still_constructs_with_its_defaults():
    plain = RunInterruption(kind=InterruptionKind.TOOL_APPROVAL, run_id=str(uuid.uuid4()))
    assert plain.resume_token is None and plain.tenant_id is None and plain.expires_at is None


@pytest.mark.parametrize("store", STORES)
async def test_an_interruption_its_decision_and_a_standing_decision_read_back_field_for_field(store):
    runner = runner_for(store)
    run_id = await run_in(runner)
    item = interruption(run_id)
    rows = store_of(runner)
    await call(rows.append_interruption, item)
    [view] = await call(rows.interruptions, TENANT, PROJECT, run_id=run_id, pending_only=False)
    assert isinstance(view, InterruptionView) and view.interruption == item and view.decision is None
    assert view.state == "pending" and view.waiting is False
    made = decision(item, approve=False, message="not that file")
    await call(rows.append_decision, made)
    [view] = await call(rows.interruptions, TENANT, PROJECT, run_id=run_id, pending_only=False)
    assert view.decision == made and view.state == "rejected"
    assert await call(rows.interruptions, TENANT, PROJECT, run_id=run_id, pending_only=True) == []
    first = await call(rows.append_standing, standing(run_id))
    second = await call(rows.append_standing, standing(run_id, approve=False))
    assert first.seq is not None and second.seq > first.seq
    assert await call(rows.standing, TENANT, PROJECT, run_id) == [first, second]


@pytest.mark.parametrize("store", STORES)
async def test_a_second_decision_for_one_interruption_is_refused(store):
    runner = runner_for(store)
    run_id = await run_in(runner)
    item = interruption(run_id)
    rows = store_of(runner)
    await call(rows.append_interruption, item)
    await call(rows.append_decision, decision(item))
    with pytest.raises(InterruptionAlreadyDecided):
        await call(rows.append_decision, decision(item, approve=False))
    [view] = await call(rows.interruptions, TENANT, PROJECT, run_id=run_id, pending_only=False)
    assert view.decision.decision == "approve"


def test_the_database_itself_refuses_a_second_decision_and_a_row_of_another_tenants_run():
    async def setup():
        runner = runner_for("postgres")
        run_id = await run_in(runner)
        item = interruption(run_id)
        await call(store_of(runner).append_interruption, item)
        await call(store_of(runner).append_decision, decision(item))
        undecided = interruption(run_id)
        await call(store_of(runner).append_interruption, undecided)
        return run_id, item, undecided

    run_id, item, undecided = asyncio.run(setup())
    with psycopg.connect(DSN) as conn:
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute("INSERT INTO interruption_decisions (decision_id, interruption_id, tenant_id, project_id,"
                         " decision, source, decided_at) VALUES (%s, %s, %s, %s, 'reject', 'expiry', now())",
                         (str(uuid.uuid4()), item.interruption_id, TENANT, PROJECT))
    with psycopg.connect(DSN) as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "INSERT INTO interruptions (interruption_id, tenant_id, project_id, run_id, top_run_id, kind,"
                " tool_call_id, tool_name, agent_id, trigger, request, created_at, expires_at)"
                " VALUES (%s, %s, %s, %s, %s, 'tool_approval', 'c', 't', 'a', 'policy', '{}', now(), now())",
                (str(uuid.uuid4()), OTHER_TENANT, PROJECT, run_id, run_id))
    with psycopg.connect(DSN) as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute("INSERT INTO interruption_decisions (decision_id, interruption_id, tenant_id, project_id,"
                         " decision, source, decided_at) VALUES (%s, %s, %s, %s, 'reject', 'expiry', now())",
                         (str(uuid.uuid4()), undecided.interruption_id, OTHER_TENANT, PROJECT))


@pytest.mark.parametrize("store", STORES)
async def test_no_row_names_another_tenants_or_projects_run_and_no_read_crosses_tenancy(store):
    runner = runner_for(store)
    run_id = await run_in(runner)
    other = await run_in(runner, tenant=OTHER_TENANT)
    rows = store_of(runner)
    for wrong in (interruption(run_id, tenant=OTHER_TENANT), interruption(other),
                  dataclasses.replace(interruption(run_id), project_id="p-other"),
                  interruption(run_id, top=other),
                  # A valid top-level run, but a run of another tenant: each is checked.
                  interruption(other, top=run_id)):
        with pytest.raises(LookupError):
            await call(rows.append_interruption, wrong)
    item = interruption(run_id)
    await call(rows.append_interruption, item)
    with pytest.raises(UnknownInterruption):
        await call(rows.append_decision, dataclasses.replace(decision(item), tenant_id=OTHER_TENANT))
    with pytest.raises(LookupError):
        await call(rows.append_standing, dataclasses.replace(standing(run_id), tenant_id=OTHER_TENANT))
    assert await call(rows.interruptions, OTHER_TENANT, PROJECT, pending_only=False) == []
    with pytest.raises(UnknownInterruption):
        await call(rows.interruption, OTHER_TENANT, PROJECT, item.interruption_id)


@pytest.mark.parametrize("store", STORES)
async def test_an_undecided_interruption_past_its_expiry_reads_as_expired(store):
    runner = runner_for(store)
    run_id = await run_in(runner)
    rows = store_of(runner)
    late = dataclasses.replace(interruption(run_id), expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    await call(rows.append_interruption, late)
    view = await call(rows.interruption, TENANT, PROJECT, late.interruption_id)
    assert view.state == "expired" and view.decision is None
    assert await call(rows.interruptions, TENANT, PROJECT, run_id=run_id, pending_only=True) == []


@pytest.mark.parametrize("store", STORES)
async def test_listing_a_run_lists_every_run_beneath_it(store):
    runner = runner_for(store)
    top = await run_in(runner)
    child = await run_in(runner, parent=top)
    grandchild = await run_in(runner, parent=child)
    beside = await run_in(runner)
    rows = store_of(runner)
    made = [interruption(top), interruption(child, top=top, node_id="n"), interruption(grandchild, top=top),
            interruption(beside)]
    for item in made:
        await call(rows.append_interruption, item)
    listed = {v.interruption.run_id for v in await call(rows.interruptions, TENANT, PROJECT, run_id=top)}
    assert listed == {top, child, grandchild}
    assert {v.interruption.run_id for v in await call(rows.interruptions, TENANT, PROJECT, run_id=child)} == {
        child, grandchild}


def test_a_decision_names_who_or_what_made_it():
    item = interruption(str(uuid.uuid4()))
    with pytest.raises(ValueError, match="principal"):
        decision(item, principal=None)
    with pytest.raises(ValueError, match="user or a service"):
        decision(item, principal=PrincipalContext(agent_principal="the-agent-itself"))
    with pytest.raises(ValueError, match="principal"):
        decision(item, source="expiry")  # expiry and cancellation name no principal
    decision(item, source="expiry", principal=None, approve=False)
    with pytest.raises(ValueError, match="standing"):
        decision(item, source="standing", principal=None)
    decision(item, source="standing", principal=None, standing_id=str(uuid.uuid4()))
    with pytest.raises(ValueError, match="reject"):
        decision(item, source="cancel", principal=None, approve=True)
    with pytest.raises(ValueError, match="message"):
        decision(item, approve=False, message="a\u0000b")


@pytest.mark.parametrize("store", STORES)
def test_no_store_method_updates_or_deletes_a_row(store):
    cls = InMemoryInterruptionStore if store == "memory" else Persistence.postgres(
        DSN, create_schema=False).interruption_store().__class__
    public = [name for name in dir(cls) if not name.startswith("_")]
    assert not [n for n in public if re.search(r"update|delete|remove|replace|clear|set_", n)], public
    assert {"append_interruption", "append_decision", "append_standing", "interruptions", "interruption",
            "standing"} <= set(public)


def test_migration_0014_keys_runs_by_tenancy_and_records_the_approval_timeout():
    with psycopg.connect(DSN) as conn:
        assert conn.execute(
            "SELECT 1 FROM pg_constraint WHERE conrelid = 'runs'::regclass AND contype = 'u'"
            " AND pg_get_constraintdef(oid) = 'UNIQUE (run_id, tenant_id, project_id)'").fetchone()
        assert conn.execute("SELECT data_type FROM information_schema.columns WHERE table_name = 'execution_manifests'"
                            " AND column_name = 'approval_timeout'").fetchone() is not None


def test_the_named_refusals_are_the_types_fr_115_names():
    assert issubclass(UnknownInterruption, LookupError)
    for error in (InterruptionAlreadyDecided, InterruptionExpired, InterruptionNotWaiting):
        assert issubclass(error, Exception) and not issubclass(error, LookupError)
