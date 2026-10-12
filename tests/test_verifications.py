"""M26 gate: verifications, the gate's signals and external outcomes (FR-100 to FR-103, AC-78).

Phase 3's second increment (DECISION-f9888168, DECISION-581a647b, DECISION-2cec3b61).
Until M27 builds the critic, a gated node whose signals fire records its verification and
fails `criterion_not_available` (DECISION-4f57d8c5).

Store calls made from these async tests go to a worker thread (FR-81). The persisted half
needs DATABASE_URL and fails rather than skips without it; every row written is removed.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import random
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import psycopg
import pytest

import agentsdk
from agentsdk import AgentSpec, BudgetPolicy, ContextPolicy, Persistence, RunConfig, RunStatus, Runner
from agentsdk.builtin_tools import fetch_tool
from agentsdk.config import normalise_database_url
from agentsdk.events import EventType
from agentsdk.gate import SIGNALS, EvidenceVerification, GatePolicy, NodeSubject, signals_of
from agentsdk.ledger import ClaimContribution, ClaimView, EvidenceClaim, InMemoryLedgerStore, claim_key, ledger_tools
from agentsdk.manifest import build_manifest
from agentsdk import migrate
from agentsdk.migrate import apply_migrations
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.orchestrator import PLAN_TOOL, Orchestrator
from agentsdk.postgres import SCHEMA_PATH, PostgresLedgerStore
from agentsdk.primitives import (
    ContentProvenance, InstructionAuthority, Message, Origin, Role, TaintFlag, ToolCall, TrustZone,
)
from agentsdk.scope import ToolScope

DSN = normalise_database_url(os.environ.get("DATABASE_URL"))
TENANT, PROJECT, OTHER = "SYN-m26", "p-m26", "SYN-m26-other"
PUBLIC = "93.184.216.34"
UNTRUSTED = ContentProvenance(origin=Origin.EXTERNAL_TOOL, instruction_authority=InstructionAuthority.DATA_ONLY,
                              trust_zone=TrustZone.UNTRUSTED, taint_flags=frozenset({TaintFlag.EXTERNAL_CONTENT}))
NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)


# =================================================================================================
# Scripted agents and local pages
# =================================================================================================


def respond(content="", calls=()):
    return ModelResponse(message=Message(role=Role.ASSISTANT, content=content, tool_calls=tuple(calls)),
                         stop_reason=StopReason.TOOL_CALLS if calls else StopReason.END_TURN, usage=Usage(5, 1, 6))


class Planner:
    def __init__(self, document):
        self.document = document

    async def send(self, request):
        if any(m.role is Role.TOOL for m in request.messages):
            return respond("the plan is done")
        return respond(calls=[ToolCall(id="plan", name=PLAN_TOOL, arguments=self.document)])


class Reader:
    """A node's child: its objective is a JSON list of [tool, arguments], one a turn."""

    def __init__(self):
        self.calls = 0

    async def send(self, request):
        self.calls += 1
        turns = json.loads(request.messages[0].content.split("\n")[0])
        done = sum(len(m.tool_results) for m in request.messages if m.role is Role.TOOL)
        if done < len(turns):
            name, arguments = turns[done]
            return respond(calls=[ToolCall(id=f"c{done}", name=name, arguments=arguments)])
        return respond("read")


class Critic:
    def __init__(self):
        self.calls = 0

    async def send(self, request):
        self.calls += 1
        return respond('{"verdict": "approved", "reason": "fine"}')


class Pages:
    def __init__(self):
        self.requests = 0

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            self.requests += 1
            body = b"The Rhine reaches the North Sea."
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                         + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
            await writer.drain()
        finally:
            writer.close()


URL = "http://rivers.test/rhine"
READER = AgentSpec(id="reader", instructions="read", preferred_model="reader:r",
                   tool_profile=("fetch_url", "record_claim"))
CRITIC = AgentSpec(id="critic", instructions="judge", preferred_model="critic:c")
RECORD, SUPERSEDE = ledger_tools()


def fetch(pages):
    async def resolve(host, port):
        return [PUBLIC]

    return fetch_tool(["rivers.test"], _resolve=resolve, _connect=lambda ip, port: ("127.0.0.1", pages.port))


def backend_of(store):
    return Persistence.postgres(DSN, create_schema=False) if store == "postgres" else None


def setup(pages, nodes, *, gate, store="memory", critic=None):
    reader, critic_model = Reader(), critic or Critic()
    orchestrator = Orchestrator(roles={"reader": READER}, policy=BudgetPolicy(run_ceiling_tokens=1_000_000),
                                context_policy=ContextPolicy(context_window=1_000_000), gate=gate)
    runner = Runner({"planner": Planner({"nodes": nodes}), "reader": reader, "critic": critic_model},
                    tools=[orchestrator.tool, fetch(pages), RECORD], persistence=backend_of(store))
    return orchestrator, runner, reader, critic_model


def node(node_id, turns, criteria=()):
    return {"node_id": node_id, "objective": json.dumps(turns), "assigned_role": "reader", "dependencies": [],
            "acceptance_criteria": list(criteria)}


def claim(statement, *, supporting=(), contradicting=(), confidence=0.9):
    arguments = {"statement": statement, "supporting": list(supporting)}
    if contradicting:
        arguments["contradicting"] = list(contradicting)
    if confidence is not None:
        arguments["confidence"] = confidence
    return ["record_claim", arguments]


READ = ["fetch_url", {"url": URL}]
CRITERION = {"kind": "critic", "description": "is it supported"}


async def orchestrate(runner, orchestrator):
    return await runner.run(orchestrator.agent, "go", orchestrator.config(
        tenant_id=TENANT, project_id=PROJECT, model_override="planner:p", max_turns=6))


def finished(result, node_id):
    [found] = [e for e in result.events
               if e.event_type is EventType.PLAN_NODE_FINISHED and e.task_id == node_id]
    return found.payload["status"], found.payload.get("reason")


def gate_events(result):
    return [e for e in result.events if e.event_type is EventType.GATE_EVALUATED]


async def verifications(runner, ledger_run_id, **filters):
    return await asyncio.to_thread(runner._ledger_store().verifications, TENANT, PROJECT, ledger_run_id, **filters)


@pytest.fixture(autouse=True)
def remove_what_the_test_wrote():
    yield
    if DSN:
        with psycopg.connect(DSN, autocommit=True) as conn:
            ids = [r[0] for r in conn.execute("SELECT run_id FROM runs WHERE tenant_id = ANY(%s)", ([TENANT, OTHER],))]
            conn.execute("DELETE FROM verifications WHERE ledger_run_id = ANY(%s) OR subject_run = ANY(%s)", (ids, ids))
            conn.execute("DELETE FROM claim_citations WHERE contribution_id IN (SELECT contribution_id FROM "
                         "claim_contributions WHERE run_id = ANY(%s))", (ids,))
            conn.execute("DELETE FROM claim_contributions WHERE run_id = ANY(%s)", (ids,))
            conn.execute("DELETE FROM claims WHERE ledger_run_id = ANY(%s) AND supersedes IS NOT NULL", (ids,))
            conn.execute("DELETE FROM claims WHERE ledger_run_id = ANY(%s)", (ids,))
            conn.execute("DELETE FROM source_versions WHERE source_run = ANY(%s)", (ids,))
            conn.execute("DELETE FROM artifacts WHERE source_run = ANY(%s)", (ids,))
            conn.execute("DELETE FROM plan_node_states WHERE run_id = ANY(%s)", (ids,))
            conn.execute("DELETE FROM plan_versions WHERE run_id = ANY(%s)", (ids,))
            for table in ("run_events", "messages", "execution_manifests"):
                conn.execute(f"DELETE FROM {table} WHERE run_id = ANY(%s)", (ids,))
            conn.execute("DELETE FROM runs WHERE run_id = ANY(%s)", (ids,))


# =================================================================================================
# FR-100: the record
# =================================================================================================


def a_verification(**fields):
    base = dict(
        verification_id=str(uuid.uuid4()), ledger_run_id=str(uuid.uuid4()),
        subject=f"urn:agentsdk:claim:{uuid.uuid4()}", kind="external", outcome="correct",
        detail={"source": "test", "detail": "a known-answer test passed"}, provenance=ContentProvenance.internal_tool(),
        tenant_id=TENANT, project_id=PROJECT, created_at=NOW,
    )
    base.update(fields)
    return EvidenceVerification(**base)


@pytest.mark.parametrize("field, value", [
    ("verification_id", "not-a-uuid"),
    ("ledger_run_id", "x"),
    ("subject", "urn:agentsdk:claim:nope"),
    ("subject", "https://example.com/"),
    ("subject", 3),
    ("kind", "opinion"),
    ("outcome", "approved"),  # a critic's outcome, not an external one's
    ("detail", "text"),
    ("detail", {"source": "rumour", "detail": "x"}),
    ("detail", {"source": "test"}),
    ("detail", {"source": "test", "detail": "x", "n": float("nan")}),
    ("detail", {"source": "test", "detail": "nul\x00"}),
    ("provenance", "trusted"),
    ("tenant_id", ""),
    ("project_id", None),
    ("created_at", datetime(2026, 10, 12)),
])
def test_a_verification_refuses_each_invalid_field_by_name(field, value):
    with pytest.raises(ValueError, match=field):
        a_verification(**{field: value})


def test_each_kind_takes_only_its_own_outcomes_and_subjects():
    subject = NodeSubject(run_id=str(uuid.uuid4()), plan_id=str(uuid.uuid4()), version=1, node_id="n")
    a_verification(kind="gate", outcome="clear", subject=subject, detail={"claims": []})
    a_verification(kind="critic", outcome="corrected", detail={"reason": "x"})
    with pytest.raises(ValueError, match="subject"):  # a gate judges a node, never a claim alone
        a_verification(kind="gate", outcome="clear", detail={"claims": []})
    with pytest.raises(ValueError, match="outcome"):
        a_verification(kind="gate", outcome="correct", subject=subject, detail={"claims": []})
    for bad in [dict(version=0), dict(version=True), dict(node_id=""), dict(plan_id="p"), dict(run_id=None)]:
        with pytest.raises(ValueError):
            NodeSubject(**{**dataclasses.asdict(subject), **bad})
    assert a_verification().uri.startswith("urn:agentsdk:verification:")


# =================================================================================================
# FR-100: both stores
# =================================================================================================


async def _orchestrated(pages, store, *, gate=None, nodes=None):
    gate = gate or GatePolicy(critic=CRITIC)
    orchestrator, runner, reader, critic = setup(
        pages, nodes or [node("n", [READ, claim("The Rhine reaches the North Sea.", supporting=[URL])], [CRITERION])],
        gate=gate, store=store)
    result = await orchestrate(runner, orchestrator)
    return orchestrator, runner, result


@pytest.mark.parametrize("store", ["memory", "postgres"])
async def test_verifications_read_back_field_for_field_and_refuse_a_subject_outside_the_ledger(store):
    async with Pages() as pages:
        orchestrator, runner, result = await _orchestrated(pages, store)
    ledger = runner._ledger_store()
    [view] = await asyncio.to_thread(runner.evidence_ledger(TENANT, PROJECT, result.run_id).active)
    [version] = orchestrator.plans(result.run_id)
    node_subject = NodeSubject(run_id=result.run_id, plan_id=version.plan_id, version=1, node_id="n")
    mine = [
        a_verification(ledger_run_id=result.run_id, subject=view.claim.uri),
        a_verification(ledger_run_id=result.run_id, subject=node_subject, kind="critic", outcome="escalate",
                       detail={"reason": "unsure", "nested": {"a": [1, 2.5, None, True]}}, provenance=UNTRUSTED),
    ]
    before = await verifications(runner, result.run_id)
    await asyncio.to_thread(ledger.append_verifications, mine)
    after = await verifications(runner, result.run_id)
    added = [v for v in after if v not in before]
    assert sorted(added, key=lambda v: v.verification_id) == sorted(mine, key=lambda v: v.verification_id)
    assert await verifications(runner, result.run_id, subject=view.claim.uri) == [mine[0]]
    assert await verifications(runner, result.run_id, kinds=("critic",)) == [mine[1]]
    since = await asyncio.to_thread(ledger.verifications_since, TENANT, PROJECT, NOW - timedelta(seconds=1))
    assert all(v in since for v in mine)
    assert await asyncio.to_thread(ledger.verifications_since, OTHER, PROJECT, None) == []

    outside = [
        a_verification(ledger_run_id=result.run_id, subject=f"urn:agentsdk:claim:{uuid.uuid4()}"),
        a_verification(ledger_run_id=result.run_id, subject=view.claim.uri, tenant_id=OTHER),
        a_verification(ledger_run_id=result.run_id, subject=view.claim.uri, project_id="p-elsewhere"),
        a_verification(ledger_run_id=str(uuid.uuid4()), subject=view.claim.uri),
        a_verification(ledger_run_id=result.run_id, subject=dataclasses.replace(node_subject, node_id="m")),
        a_verification(ledger_run_id=result.run_id, subject=dataclasses.replace(node_subject, version=2)),
        a_verification(ledger_run_id=result.run_id, subject=dataclasses.replace(node_subject, run_id=str(uuid.uuid4()))),
    ]
    for row in outside:
        with pytest.raises(LookupError):
            # With a good row first: refused as a whole, nothing recorded (one transaction).
            await asyncio.to_thread(ledger.append_verifications, [a_verification(
                ledger_run_id=result.run_id, subject=view.claim.uri), row])
    assert await verifications(runner, result.run_id) == after


@pytest.mark.parametrize("store", ["memory", "postgres"])
async def test_a_node_of_another_runs_plan_is_not_a_subject_of_this_ledger(store):
    async with Pages() as pages:
        orchestrator, runner, first = await _orchestrated(pages, store)
        second = await runner.run(orchestrator.agent, "go", orchestrator.config(
            tenant_id=TENANT, project_id=PROJECT, model_override="planner:p", max_turns=6))
    [version] = orchestrator.plans(second.run_id)
    theirs = NodeSubject(run_id=second.run_id, plan_id=version.plan_id, version=1, node_id="n")
    with pytest.raises(LookupError):
        await asyncio.to_thread(runner._ledger_store().append_verifications, [a_verification(
            ledger_run_id=first.run_id, subject=theirs, kind="critic", outcome="approved", detail={"reason": "x"})])


def test_neither_store_exposes_a_method_that_updates_or_deletes_a_verification():
    for store in (InMemoryLedgerStore, PostgresLedgerStore):
        public = [n for n in dir(store) if not n.startswith("_")]
        assert "append_verifications" in public and "verifications" in public and "verifications_since" in public
        assert not [n for n in public if any(w in n.lower() for w in ("update", "delete", "remove", "replace", "set", "clear"))
                    and n != "verifications_since"], public


def test_migration_0012_adds_the_table_and_the_manifest_column_and_applies_once():
    real = migrate.discover()
    assert "0012" in [v for v, _ in real], "migration 0012 is not on disk"
    name = "m26_" + uuid.uuid4().hex[:8]
    scratch = DSN + ("&" if "?" in DSN else "?") + f"options=-csearch_path%3D{name}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{name}"')
    try:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'SET search_path TO "{name}"')
            conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        assert "0012" in apply_migrations(scratch)
        snapshot = ("SELECT table_name, column_name, data_type, is_nullable FROM information_schema.columns "
                    "WHERE table_schema = %s AND (table_name = 'verifications' OR "
                    "(table_name = 'execution_manifests' AND column_name = 'gate_policy')) ORDER BY 1, 2")
        with psycopg.connect(scratch) as conn:
            before = conn.execute(snapshot, (name,)).fetchall()
        assert apply_migrations(scratch) == []
        with psycopg.connect(scratch, autocommit=True) as conn:
            conn.execute(dict(real)["0012"].read_text(encoding="utf-8"))
            assert conn.execute(snapshot, (name,)).fetchall() == before
        columns = {(t, c): n for t, c, _, n in before}
        assert columns[("verifications", "tenant_id")] == columns[("verifications", "project_id")] == "NO"
        assert columns[("execution_manifests", "gate_policy")] == "YES"
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{name}" CASCADE')


# =================================================================================================
# FR-102: the signals, over generated ledgers
# =================================================================================================


def _view(supporting, contradicting, confidences, critic_confidences=()):
    run = str(uuid.uuid4())
    statement = f"claim {uuid.uuid4()}"
    the_claim = EvidenceClaim(str(uuid.uuid4()), run, statement, claim_key(statement), None, TENANT, PROJECT)

    def contribution(confidence, node_id):
        return ClaimContribution(run_id=run, node_id=node_id, agent_id="a", supporting=(), contradicting=(),
                                 confidence=confidence, provenance=UNTRUSTED, why=None,
                                 contribution_id=str(uuid.uuid4()), claim_id=the_claim.claim_id, recorded_at=NOW)

    contributions = tuple(contribution(c, "n") for c in confidences) + tuple(
        contribution(c, "n#critic") for c in critic_confidences)
    return ClaimView(claim=the_claim, active=True, superseded_by=None, contributions=contributions,
                     supporting=tuple(str(uuid.uuid4()) for _ in range(supporting)),
                     contradicting=tuple(str(uuid.uuid4()) for _ in range(contradicting)), provenance=UNTRUSTED)


def _expected(supporting, contradicting, confidences, policy):
    fired = set()
    if supporting == 0:
        fired.add("unsupported")
    if contradicting:
        fired.add("contradicted")
    if 1 <= supporting < policy.min_sources:
        fired.add("thin_support")
    lowest = [-1.0 if c is None else c for c in confidences]
    if lowest and min(lowest) < policy.confidence_threshold:
        fired.add("low_confidence")
    return fired & set(policy.signals)


def test_each_signal_fires_exactly_when_its_rule_holds_over_generated_ledgers():
    rng = random.Random(26)
    for _ in range(600):
        threshold = rng.choice([0.0, 0.5, 0.7, 0.7000001, 1.0])
        policy = GatePolicy(critic=CRITIC, min_sources=rng.randint(1, 4), confidence_threshold=threshold,
                            signals=tuple(s for s in SIGNALS if rng.random() < 0.8))
        supporting, contradicting = rng.randint(0, 5), rng.choice([0, 0, 1, 2])
        confidences = [rng.choice([None, 0.0, 0.5, 0.7, threshold, 0.9, 1.0]) for _ in range(rng.randint(1, 3))]
        critic = [rng.choice([None, 0.0])] if rng.random() < 0.3 else []
        found = signals_of(_view(supporting, contradicting, confidences, critic), policy)
        assert set(found["fired"]) == _expected(supporting, contradicting, confidences, policy), (
            supporting, contradicting, confidences, policy, found)
        assert not {"unsupported", "thin_support"} <= set(found["fired"])
        assert found["values"]["supporting"] == supporting


def test_the_signal_edges():
    policy = GatePolicy(critic=CRITIC, min_sources=2, confidence_threshold=0.7)
    assert signals_of(_view(1, 0, [0.9]), policy)["fired"] == ["thin_support"]
    assert signals_of(_view(2, 0, [0.9]), policy)["fired"] == []
    assert signals_of(_view(2, 0, [0.7]), policy)["fired"] == []  # at the threshold is not below it
    assert signals_of(_view(2, 0, [0.6999]), policy)["fired"] == ["low_confidence"]
    assert signals_of(_view(2, 0, [None]), policy)["fired"] == ["low_confidence"]
    assert signals_of(_view(2, 0, [0.9], critic_confidences=[None]), policy)["fired"] == []
    assert signals_of(_view(0, 1, [0.9]), policy)["fired"] == ["unsupported", "contradicted"]
    assert signals_of(_view(1, 0, [0.9]), GatePolicy(critic=CRITIC))["fired"] == []  # never at the default


# =================================================================================================
# FR-102: the gate in a plan
# =================================================================================================


@pytest.mark.parametrize("store", ["memory", "postgres"])
async def test_clean_signals_satisfy_the_criterion_with_no_model_call_and_record_a_clear_verification(store):
    async with Pages() as pages:
        orchestrator, runner, reader, critic = setup(
            pages, [node("n", [READ, claim("The Rhine reaches the North Sea.", supporting=[URL])], [CRITERION])],
            gate=GatePolicy(critic=CRITIC), store=store)
        result = await orchestrate(runner, orchestrator)
    assert finished(result, "n") == ("done", None)
    assert critic.calls == 0
    [event] = gate_events(result)
    [row] = await verifications(runner, result.run_id, kinds=("gate",))
    [version] = orchestrator.plans(result.run_id)
    assert row.kind == "gate" and row.outcome == "clear" and row.subject == NodeSubject(
        run_id=result.run_id, plan_id=version.plan_id, version=1, node_id="n")
    assert event.task_id == "n" and event.payload == {
        "plan_id": version.plan_id, "version": 1, "signals": [], "outcome": "clear", "verification": row.uri}
    [judged] = row.detail["claims"]
    assert judged["fired"] == [] and judged["values"]["supporting"] == 1
    # At least as tainted as the claims and the answer it judged.
    assert TaintFlag.EXTERNAL_CONTENT in row.provenance.taint_flags


@pytest.mark.parametrize("store", ["memory", "postgres"])
async def test_a_signal_that_fires_records_critic_required_and_fails_the_node_until_m27(store):
    async with Pages() as pages:
        orchestrator, runner, reader, critic = setup(
            pages, [node("n", [READ, claim("The Rhine reaches the Black Sea.")], [CRITERION])],
            gate=GatePolicy(critic=CRITIC), store=store)
        result = await orchestrate(runner, orchestrator)
    status, reason = finished(result, "n")
    assert status == "failed" and reason.startswith("criterion_not_available: critic"), reason
    assert critic.calls == 0
    [row] = await verifications(runner, result.run_id, kinds=("gate",))
    assert row.outcome == "critic_required" and row.detail["claims"][0]["fired"] == ["unsupported"]
    assert [e.payload["signals"] for e in gate_events(result)][0] == ["unsupported"]


async def test_no_claims_fires_only_for_a_node_carrying_a_critic_criterion():
    async with Pages() as pages:
        orchestrator, runner, _, _ = setup(
            pages, [node("with", [READ], [CRITERION]), node("without", [READ])],
            gate=GatePolicy(critic=CRITIC, gate_all_nodes=True))
        result = await orchestrate(runner, orchestrator)
    assert finished(result, "with")[0] == "failed"
    assert finished(result, "without") == ("done", None)
    by_node = {e.task_id: e.payload for e in gate_events(result)}
    assert by_node["with"]["signals"] == ["no_claims"] and by_node["without"]["outcome"] == "clear"


async def test_the_gate_judges_only_this_executions_claims():
    async with Pages() as pages:
        orchestrator, runner, _, _ = setup(pages, [
            node("bad", [READ, claim("Unsourced claim one.")]),
            node("good", [READ, claim("The Rhine reaches the North Sea.", supporting=[URL])], [CRITERION]),
        ], gate=GatePolicy(critic=CRITIC))
        result = await orchestrate(runner, orchestrator)
    assert finished(result, "good") == ("done", None)
    assert [e.task_id for e in gate_events(result)] == ["good"]


@pytest.mark.parametrize("position", ["before", "after"])
async def test_a_failing_deterministic_criterion_fails_the_node_with_no_gate_wherever_it_stands(position):
    failing = {"kind": "artifact_exists", "target": f"urn:agentsdk:artifact:{uuid.uuid4()}"}
    criteria = [CRITERION, failing] if position == "after" else [failing, CRITERION]
    async with Pages() as pages:
        orchestrator, runner, _, _ = setup(pages, [node("n", [READ], criteria)], gate=GatePolicy(critic=CRITIC))
        result = await orchestrate(runner, orchestrator)
    status, reason = finished(result, "n")
    assert status == "failed" and reason.startswith("acceptance_criterion_failed: artifact_exists"), reason
    assert gate_events(result) == []
    assert await verifications(runner, result.run_id) == []


async def test_with_no_critic_configured_a_critic_criterion_is_refused_as_before():
    async with Pages() as pages:
        orchestrator, runner, _, _ = setup(pages, [node("n", [READ], [CRITERION])], gate=None)
        result = await orchestrate(runner, orchestrator)
    status, reason = finished(result, "n")
    assert status == "failed" and reason.startswith("criterion_not_available: critic is it supported"), reason
    assert gate_events(result) == []


async def test_the_nodes_reservation_is_held_until_the_gate_has_finished(monkeypatch):
    held = []
    async with Pages() as pages:
        orchestrator, runner, _, _ = setup(
            pages, [node("n", [READ, claim("The Rhine reaches the North Sea.", supporting=[URL])], [CRITERION])],
            gate=GatePolicy(critic=CRITIC))
        store = runner._ledger_store()
        real = store.append_verifications

        def watched(rows):
            held.append(watched.lease._released)
            return real(rows)

        original_attempt = orchestrator._attempt

        async def attempt(run, node_, scope, governor, labels, *rest):
            watched.lease = governor.lease(node_.node_id)
            return await original_attempt(run, node_, scope, governor, labels, *rest)

        monkeypatch.setattr(store, "append_verifications", watched)
        monkeypatch.setattr(orchestrator, "_attempt", attempt)
        result = await orchestrate(runner, orchestrator)
    assert finished(result, "n") == ("done", None)
    assert held == [False], "the reservation was released before the gate"
    assert watched.lease._released


# =================================================================================================
# FR-101: the policy
# =================================================================================================


@pytest.mark.parametrize("field, value", [
    ("gate_all_nodes", 1),
    ("signals", ("unsupported", "made_up")),
    ("signals", ("unsupported", "unsupported")),
    ("signals", "unsupported"),
    ("min_sources", 0),
    ("min_sources", True),
    ("min_sources", 1.5),
    ("confidence_threshold", -0.1),
    ("confidence_threshold", 1.01),
    ("confidence_threshold", float("nan")),
    ("confidence_threshold", True),
    ("max_briefing_chars", 0),
    ("critic", "critic"),
])
def test_gate_policy_refuses_each_invalid_field_by_name(field, value):
    with pytest.raises(ValueError, match=field):
        GatePolicy(**{"critic": CRITIC, field: value})


def test_gate_policy_refuses_gating_every_node_without_a_critic_and_a_critic_with_any_tool():
    with pytest.raises(ValueError, match="gate_all_nodes"):
        GatePolicy(gate_all_nodes=True)
    with pytest.raises(ValueError, match="tool"):
        GatePolicy(critic=dataclasses.replace(CRITIC, tool_profile=("fetch_url",)))
    assert GatePolicy().signals == SIGNALS and GatePolicy(signals=["no_claims", "unsupported"]).signals == (
        "unsupported", "no_claims")
    with pytest.raises(ValueError, match="gate"):
        RunConfig(tenant_id=TENANT, project_id=PROJECT, gate_policy={"critic": None})


def test_the_manifest_records_the_effective_policy():
    policy = GatePolicy(critic=CRITIC, min_sources=2)
    recorded = build_manifest(sdk_version="x", agent_spec_id="o", instructions="i", tool_profile=(),
                              tool_spec_hashes=[], model_id=None, gate_policy=policy.to_json())["gate_policy"]
    assert recorded["min_sources"] == 2 and recorded["critic"]["id"] == "critic"
    assert recorded["critic"]["spec_hash"].startswith("v2:")
    changed = GatePolicy(critic=dataclasses.replace(CRITIC, instructions="judge harder")).to_json()
    assert changed["critic"]["spec_hash"] != recorded["critic"]["spec_hash"]
    assert build_manifest(sdk_version="x", agent_spec_id="o", instructions="i", tool_profile=(),
                          tool_spec_hashes=[], model_id=None)["gate_policy"] is None


async def test_a_gated_runs_manifest_records_its_policy_and_an_ungated_runs_records_null():
    async with Pages() as pages:
        _, _, gated = await _orchestrated(pages, "postgres")
        orchestrator, runner, _, _ = setup(pages, [node("n", [READ])], gate=None, store="postgres")
        ungated = await orchestrate(runner, orchestrator)

    def column(run_id):
        with psycopg.connect(DSN) as conn:
            return conn.execute("SELECT gate_policy FROM execution_manifests WHERE run_id = %s", (run_id,)).fetchone()[0]

    recorded = await asyncio.to_thread(column, gated.run_id)
    assert recorded["critic"]["id"] == "critic" and recorded["signals"] == list(SIGNALS)
    assert await asyncio.to_thread(column, ungated.run_id) is None


async def test_an_unrunnable_critic_ends_the_plan_at_submission():
    async with Pages() as pages:
        orchestrator, runner, reader, _ = setup(
            pages, [node("n", [READ], [CRITERION])],
            gate=GatePolicy(critic=dataclasses.replace(CRITIC, preferred_model="nowhere:x")))
        result = await orchestrate(runner, orchestrator)
    assert result.status is RunStatus.FAILED
    assert "critic" in (result.error or "") and "cannot be replanned" in (result.error or ""), result.error
    assert reader.calls == 0


# =================================================================================================
# FR-102, FR-103: signals for any claim, and external outcomes
# =================================================================================================


@pytest.mark.parametrize("store", ["memory", "postgres"])
async def test_record_validation_records_an_external_outcome_and_changes_no_claims_provenance(store):
    async with Pages() as pages:
        orchestrator, runner, result = await _orchestrated(pages, store)
    ledger = runner.evidence_ledger(TENANT, PROJECT, result.run_id)
    [view] = await asyncio.to_thread(ledger.active)
    [version] = orchestrator.plans(result.run_id)
    recorded = await runner.record_validation(TENANT, PROJECT, result.run_id, view.claim.uri, "human",
                                              "incorrect", "an editor checked the map")
    node_subject = NodeSubject(run_id=result.run_id, plan_id=version.plan_id, version=1, node_id="n")
    await runner.record_validation(TENANT, PROJECT, result.run_id, node_subject, "downstream", "correct", "it shipped")
    assert recorded.kind == "external" and recorded.outcome == "incorrect"
    assert recorded.detail == {"source": "human", "detail": "an editor checked the map"}
    [after] = await asyncio.to_thread(ledger.active)
    assert after.provenance == view.provenance and after.contributions == view.contributions
    for bad in [dict(source="rumour"), dict(verdict="maybe"), dict(detail="")]:
        arguments = dict(source="test", verdict="correct", detail="x") | bad
        with pytest.raises(ValueError):
            await runner.record_validation(TENANT, PROJECT, result.run_id, view.claim.uri, **arguments)
    with pytest.raises(LookupError):
        await runner.record_validation(OTHER, PROJECT, result.run_id, view.claim.uri, "test", "correct", "x")
    assert len(await verifications(runner, result.run_id, kinds=("external",))) == 2


def test_record_validation_is_reachable_from_no_tool():
    assert hasattr(Runner, "record_validation")
    assert not any("validation" in f.name for f in dataclasses.fields(ToolScope))
    from agentsdk.ledger import EvidenceLedger, RunLedger

    for reachable in (EvidenceLedger, RunLedger):
        assert not any("validation" in n or n.startswith("append") for n in dir(reachable)), reachable
    assert not any("validation" in tool.spec.name for tool in ledger_tools())


@pytest.mark.parametrize("store", ["memory", "postgres"])
async def test_signals_for_a_claim_recorded_outside_a_plan_record_nothing(store):
    async with Pages() as pages:
        runner = Runner({"reader": Reader()}, tools=[fetch(pages), RECORD], persistence=backend_of(store))
        result = await runner.run(READER, json.dumps([READ, claim("Loose claim.", confidence=None)]),
                                  RunConfig(tenant_id=TENANT, project_id=PROJECT, model_override="reader:r"))
    ledger = runner.evidence_ledger(TENANT, PROJECT, result.run_id)
    [view] = await asyncio.to_thread(ledger.active)
    found = await asyncio.to_thread(ledger.signals, view.claim.uri, GatePolicy())
    assert set(found["fired"]) == {"unsupported", "low_confidence"}
    assert await verifications(runner, result.run_id) == []


def test_the_new_public_types_are_exported():
    for name in ("EvidenceVerification", "GatePolicy"):
        assert name in agentsdk.__all__ and hasattr(agentsdk, name), name
    assert EventType.GATE_EVALUATED.value == "GateEvaluated"
