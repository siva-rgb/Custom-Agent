"""M26a gate: the gate's carried caveats (FR-110, AC-83).

M26's review (KNOWLEDGE-306e8bfc), settled before M27 (DECISION-512ecd10): FR-103's
guarantee stated as it holds and tested by value; migration 0013 making 0012's comment true
for claim subjects; the README stating the criteria order; and low_confidence on a claim
only a critic vouched for.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import os
import pathlib
import uuid
from datetime import datetime, timezone

import psycopg
import pytest

from agentsdk import AgentSpec, BudgetPolicy, ContextPolicy, Persistence, RunConfig, RunStatus, Runner
from agentsdk import migrate
from agentsdk.builtin_tools import fetch_tool
from agentsdk.config import normalise_database_url
from agentsdk.gate import EvidenceVerification, GatePolicy, signals_of
from agentsdk.ledger import ClaimContribution, ClaimView, EvidenceClaim, claim_key, ledger_tools
from agentsdk.migrate import apply_migrations
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.orchestrator import PLAN_TOOL, Orchestrator
from agentsdk.postgres import SCHEMA_PATH, PostgresLedgerStore
from agentsdk.primitives import ContentProvenance, InstructionAuthority, Message, Origin, Role, TaintFlag, ToolCall, TrustZone
from agentsdk.scope import ToolScope
from agentsdk.tools import Tool, ToolOutput, ToolSpec

DSN = normalise_database_url(os.environ.get("DATABASE_URL"))
REPO = pathlib.Path(__file__).resolve().parents[1]
TENANT, PROJECT, OTHER = "SYN-m26a", "p-m26a", "SYN-m26a-other"
PUBLIC = "93.184.216.34"
URL = "http://rivers.test/rhine"
UNTRUSTED = ContentProvenance(origin=Origin.EXTERNAL_TOOL, instruction_authority=InstructionAuthority.DATA_ONLY,
                              trust_zone=TrustZone.UNTRUSTED, taint_flags=frozenset({TaintFlag.EXTERNAL_CONTENT}))


def respond(content="", calls=()):
    return ModelResponse(message=Message(role=Role.ASSISTANT, content=content, tool_calls=tuple(calls)),
                         stop_reason=StopReason.TOOL_CALLS if calls else StopReason.END_TURN, usage=Usage(5, 1, 6))


class Scripted:
    """Makes each [tool, arguments] of its first message's JSON, one a turn, then answers."""

    async def send(self, request):
        turns = json.loads(request.messages[0].content.split("\n")[0])
        done = sum(len(m.tool_results) for m in request.messages if m.role is Role.TOOL)
        if done < len(turns):
            name, arguments = turns[done]
            return respond(calls=[ToolCall(id=f"c{done}", name=name, arguments=arguments)])
        return respond("done")


class Planner:
    def __init__(self, document):
        self.document = document

    async def send(self, request):
        if any(m.role is Role.TOOL for m in request.messages):
            return respond("the plan is done")
        return respond(calls=[ToolCall(id="plan", name=PLAN_TOOL, arguments=self.document)])


class Pages:
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
            body = b"The Rhine reaches the North Sea."
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                         + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
            await writer.drain()
        finally:
            writer.close()


def fetch(pages):
    async def resolve(host, port):
        return [PUBLIC]

    return fetch_tool(["rivers.test"], _resolve=resolve, _connect=lambda ip, port: ("127.0.0.1", pages.port))


RECORD, SUPERSEDE = ledger_tools()
READ = ["fetch_url", {"url": URL}]
AGENT = AgentSpec(id="reader", instructions="read", preferred_model="m:x",
                  tool_profile=("fetch_url", "record_claim", "supersede_claim", "see_scope"))


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
# (1) FR-103 as it holds: no model can record an outcome
# =================================================================================================


def exposures(scope: ToolScope, runner: Runner) -> list[str]:
    """The fields of a tool's scope, other than `_runner`, whose value is the Runner or an
    object with a way to append verifications or record an outcome. By value: a field
    renamed, or a new one, is caught as surely as one named for what it holds."""
    found = []
    for f in dataclasses.fields(scope):
        if f.name == "_runner":
            continue
        value = getattr(scope, f.name)
        if value is runner or isinstance(value, Runner) or any(
                callable(getattr(value, name, None)) for name in ("append_verifications", "record_validation")):
            found.append(f.name)
    return found


async def _scope_a_tool_receives():
    seen = []

    async def see_scope(*, scope: ToolScope) -> ToolOutput:
        seen.append(scope)
        return ToolOutput(content="seen")

    tool = Tool(spec=ToolSpec(name="see_scope", description="see", input_schema={"type": "object"}), fn=see_scope)
    runner = Runner({"m": Scripted()}, tools=[tool, RECORD])
    await runner.run(AGENT, json.dumps([["see_scope", {}]]), RunConfig(tenant_id=TENANT, project_id=PROJECT))
    [scope] = seen
    return scope, runner


async def test_no_field_of_a_tools_scope_but_runner_exposes_the_runner_or_a_verification_writer():
    scope, runner = await _scope_a_tool_receives()
    assert scope._runner is runner  # declared: a custom tool is first-party code (DECISION-2bad84bb)
    assert exposures(scope, runner) == [], exposures(scope, runner)


async def test_the_walk_fails_when_another_field_holds_one():
    scope, runner = await _scope_a_tool_receives()
    for name, value in [("ledger", runner._ledger_store()), ("evidence", runner), ("_control", runner)]:
        planted = copy.copy(scope)
        object.__setattr__(planted, name, value)
        assert exposures(planted, runner) == [name], name


def test_no_module_the_sdk_ships_calls_record_validation_but_the_one_defining_it():
    callers = [p.name for p in (REPO / "agentsdk").rglob("*.py")
               if "record_validation" in p.read_text(encoding="utf-8") and p.name != "api.py"]
    assert callers == [], callers


async def test_every_tool_the_sdk_ships_runs_without_calling_record_validation(monkeypatch):
    calls = []
    real = Runner.record_validation

    async def watched(self, *args, **kwargs):
        calls.append(args)
        return await real(self, *args, **kwargs)

    monkeypatch.setattr(Runner, "record_validation", watched)
    async with Pages() as pages:
        # fetch_url, record_claim and supersede_claim, run as an agent would.
        runner = Runner({"m": Scripted()}, tools=[fetch(pages), RECORD, SUPERSEDE])
        result = await runner.run(AGENT, json.dumps([READ, ["record_claim", {
            "statement": "The Rhine reaches the North Sea.", "supporting": [URL], "confidence": 0.9}]]),
            RunConfig(tenant_id=TENANT, project_id=PROJECT))
        assert result.status is RunStatus.COMPLETED, result.error
        [view] = await asyncio.to_thread(runner.evidence_ledger(TENANT, PROJECT, result.run_id).active)
        corrected = await runner.run(AGENT, json.dumps([["supersede_claim", {
            "claim": view.claim.uri, "statement": "The Rhine reaches the North Sea at Rotterdam.",
            "supporting": [URL], "why": "more exact"}]]),
            RunConfig(tenant_id=TENANT, project_id=PROJECT, ledger_run_id=result.run_id))
        assert corrected.status is RunStatus.COMPLETED, corrected.error
        # run_plan, through a gated plan whose node is cleared and one whose signals fire.
        critic = AgentSpec(id="critic", instructions="judge", preferred_model="m:x")
        orchestrator = Orchestrator(roles={"reader": AGENT}, policy=BudgetPolicy(run_ceiling_tokens=1_000_000),
                                    context_policy=ContextPolicy(context_window=1_000_000),
                                    gate=GatePolicy(critic=critic, gate_all_nodes=True))
        plan = {"nodes": [
            {"node_id": "clean", "objective": json.dumps([READ, ["record_claim", {
                "statement": "The Danube reaches the Black Sea.", "supporting": [URL], "confidence": 0.9}]]),
             "assigned_role": "reader", "dependencies": []},
            {"node_id": "flagged", "objective": json.dumps([["record_claim", {"statement": "Unsourced."}]]),
             "assigned_role": "reader", "dependencies": []},
        ]}
        planned = Runner({"planner": Planner(plan), "m": Scripted()},
                         tools=[orchestrator.tool, fetch(pages), RECORD, SUPERSEDE])
        top = await planned.run(orchestrator.agent, "go", orchestrator.config(
            tenant_id=TENANT, project_id=PROJECT, model_override="planner:p", max_turns=6))
        gated = await asyncio.to_thread(planned._ledger_store().verifications, TENANT, PROJECT, top.run_id,
                                        kinds=("gate",))
        assert sorted(v.outcome for v in gated) == ["clear", "critic_required"], gated
    assert calls == []


# =================================================================================================
# (2) 0013: the schema refuses a verification of another ledger's claim
# =================================================================================================


async def _claim_in_a_ledger(pages, tenant=TENANT):
    runner = Runner({"m": Scripted()}, tools=[fetch(pages), RECORD],
                    persistence=Persistence.postgres(DSN, create_schema=False))
    result = await runner.run(AGENT, json.dumps([READ, ["record_claim", {
        "statement": f"Claim {uuid.uuid4()}.", "supporting": [URL]}]]), RunConfig(tenant_id=tenant, project_id=PROJECT))
    [view] = await asyncio.to_thread(runner.evidence_ledger(tenant, PROJECT, result.run_id).active)
    return result.run_id, view


def _row(ledger_run_id, subject, tenant=TENANT, project=PROJECT):
    return EvidenceVerification(
        verification_id=str(uuid.uuid4()), ledger_run_id=ledger_run_id, subject=subject, kind="external",
        outcome="correct", detail={"source": "test", "detail": "x"}, provenance=ContentProvenance.internal_tool(),
        tenant_id=tenant, project_id=project, created_at=datetime.now(timezone.utc))


async def test_with_the_stores_check_bypassed_the_database_refuses_a_verification_of_another_ledgers_claim(monkeypatch):
    async with Pages() as pages:
        ledger, view = await _claim_in_a_ledger(pages)
        other_ledger, _ = await _claim_in_a_ledger(pages)
        other_tenant_ledger, _ = await _claim_in_a_ledger(pages, tenant=OTHER)
    monkeypatch.setattr(PostgresLedgerStore, "_subject_checked", staticmethod(lambda conn, row: None))
    store = PostgresLedgerStore(DSN)
    await asyncio.to_thread(store.append_verifications, [_row(ledger, view.claim.uri)])  # the control lands
    for row in [_row(other_ledger, view.claim.uri), _row(other_tenant_ledger, view.claim.uri, tenant=OTHER),
                _row(ledger, view.claim.uri, project="p-elsewhere")]:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            await asyncio.to_thread(store.append_verifications, [row])


def test_migration_0013_applies_once_and_every_recorded_checksum_matches():
    real = migrate.discover()
    assert "0013" in [v for v, _ in real], "migration 0013 is not on disk"
    name = "m26a_" + uuid.uuid4().hex[:8]
    scratch = DSN + ("&" if "?" in DSN else "?") + f"options=-csearch_path%3D{name}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{name}"')
    try:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'SET search_path TO "{name}"')
            conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        assert "0013" in apply_migrations(scratch)
        snapshot = ("SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE connamespace = "
                    "%s::regnamespace AND conrelid IN (%s::regclass, %s::regclass) ORDER BY 1")
        params = (name, f'"{name}".claims', f'"{name}".verifications')
        with psycopg.connect(scratch) as conn:
            before = conn.execute(snapshot, params).fetchall()
        assert apply_migrations(scratch) == []
        with psycopg.connect(scratch, autocommit=True) as conn:
            conn.execute(dict(real)["0013"].read_text(encoding="utf-8"))
            assert conn.execute(snapshot, params).fetchall() == before
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{name}" CASCADE')
    # The development database: nothing left to apply, and every recorded checksum is the file's.
    assert apply_migrations(DSN) == []
    with psycopg.connect(DSN) as conn:
        recorded = dict(conn.execute("SELECT version, checksum FROM schema_migrations").fetchall())
    assert {v: migrate.checksum(p) for v, p in real} == {v: recorded[v] for v, _ in real}


# =================================================================================================
# (3) the README, (4) a claim only a critic vouched for
# =================================================================================================


def test_the_readme_states_the_criteria_order_and_the_custom_tool_limit():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "deterministic criterion of a node runs before a critic criterion" in readme
    assert "custom tool" in readme and "record_validation" in readme


def _view(*node_ids, confidence=None):
    run, statement = str(uuid.uuid4()), f"claim {uuid.uuid4()}"
    claim = EvidenceClaim(str(uuid.uuid4()), run, statement, claim_key(statement), None, TENANT, PROJECT)
    contributions = tuple(ClaimContribution(
        run_id=run, node_id=n, agent_id="a", supporting=(), contradicting=(), confidence=confidence,
        provenance=UNTRUSTED, why=None, contribution_id=str(uuid.uuid4()), claim_id=claim.claim_id,
        recorded_at=datetime.now(timezone.utc)) for n in node_ids)
    return ClaimView(claim=claim, active=True, superseded_by=None, contributions=contributions,
                     supporting=(str(uuid.uuid4()),), contradicting=(), provenance=UNTRUSTED)


def test_a_claim_only_a_critic_vouched_for_fires_low_confidence():
    policy = GatePolicy()
    assert signals_of(_view("n#critic"), policy)["fired"] == ["low_confidence"]
    assert signals_of(_view("n#critic", "m#critic"), policy)["fired"] == ["low_confidence"]
    assert signals_of(_view("n", "n#critic", confidence=0.9), policy)["fired"] == []
    assert signals_of(_view(), policy)["fired"] == []  # no contributions at all: nothing was vouched
