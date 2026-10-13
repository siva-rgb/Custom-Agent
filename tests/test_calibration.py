"""M28 gate: calibration, and the exit signal under contention (FR-107, FR-108, AC-80, AC-81, NFR-27).

The calibration report reads the gate, critic and external verifications of one tenant
and project and computes, changing nothing: per signal, per threshold, and a suggested
threshold that is only ever printed (P3-D19). The exit signal is example 21's contention
run under a gate, with a scripted critic whose runs the corpus fixes in advance.

Store calls made from these async tests go to a worker thread (FR-81). The persisted half
needs DATABASE_URL and fails rather than skips without it; every row written is removed.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest

from agentsdk import AgentSpec, BudgetPolicy, ContextPolicy, Persistence, Runner
from agentsdk.builtin_tools import fetch_tool
from agentsdk.calibration import THRESHOLDS, calibrate
from agentsdk.config import normalise_database_url
from agentsdk.gate import SIGNALS, EvidenceVerification, GatePolicy, NodeSubject
from agentsdk.ledger import ledger_tools
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.orchestrator import PLAN_TOOL, Orchestrator
from agentsdk.primitives import ContentProvenance, Message, Role, ToolCall

REPO = Path(__file__).resolve().parent.parent
DSN = normalise_database_url(os.environ.get("DATABASE_URL"))
TENANT, PROJECT, OTHER_TENANT = "SYN-m28", "p-m28", "SYN-m28-other"
STORES = ["memory", "postgres"]
COLUMNS = ("correct", "incorrect", "no_outcome")


@pytest.fixture(autouse=True)
def remove_what_the_test_wrote():
    yield
    if DSN:
        with psycopg.connect(DSN, autocommit=True) as conn:
            ids = [r[0] for r in conn.execute("SELECT run_id FROM runs WHERE tenant_id = ANY(%s)",
                                              ([TENANT, OTHER_TENANT],))]
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
            conn.execute("DELETE FROM runs WHERE run_id = ANY(%s)", (ids,))


# =================================================================================================
# FR-107, AC-80: the report's arithmetic, over a fixture of rows
# =================================================================================================

LEDGER = str(uuid.uuid4())
T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
CLAIM = {name: f"urn:agentsdk:claim:{uuid.uuid4()}" for name in "ABCDEF"}


def at(minutes):
    return T0 + timedelta(minutes=minutes)


def row(kind, subject, outcome, detail, minutes, tenant=TENANT):
    return EvidenceVerification(
        verification_id=str(uuid.uuid4()), ledger_run_id=LEDGER, subject=subject, kind=kind, outcome=outcome,
        detail=detail, provenance=ContentProvenance.internal_tool(), tenant_id=tenant, project_id=PROJECT,
        created_at=at(minutes))


def gate(minutes, *judged):
    """A gate row judging claims, each (name, fired signals, lowest confidence)."""
    node = NodeSubject(run_id=LEDGER, plan_id=str(uuid.uuid4()), version=1, node_id=f"n{minutes}")
    claims = [{"claim": CLAIM[name], "fired": list(fired),
               "values": {"lowest_confidence": confidence, "confidence_threshold": 0.7}}
              for name, fired, confidence in judged]
    fired = sorted({s for _, f, _ in judged for s in f})
    return row("gate", node, "critic_required" if fired else "clear", {"claims": claims, "fired": fired}, minutes)


def external(name, verdict, minutes):
    subject = CLAIM[name] if name in CLAIM else name
    return row("external", subject, verdict, {"source": "fixture", "detail": "known answer"}, minutes)


def critic(name, verdict, minutes, gated):
    """A critic's rows as the orchestrator writes them: one on the node execution the gate
    row `gated` judged, one on the claim, sharing a critic_run (FR-111: a critic row counts
    only when its own execution's gate row is read)."""
    detail = {"verdict": verdict, "reason": "r", "critic_run": str(uuid.uuid4())}
    return [row("critic", gated.subject, verdict, detail, minutes), row("critic", CLAIM[name], verdict, detail, minutes)]


# Computed by hand in the tests below. A judged twice; E judged with no outcome; F has an
# outcome and no gate; a node's outcome is read and named but counted nowhere.
GATES = [gate(1, ("A", ["low_confidence"], 0.3), ("B", [], 0.8), ("C", ["unsupported", "low_confidence"], None)),
         gate(2, ("A", [], 0.75), ("D", ["contradicted"], 0.5), ("E", [], 0.9))]
NODE_OUTCOME = row("external", NodeSubject(run_id=LEDGER, plan_id=str(uuid.uuid4()), version=1, node_id="n"),
                   "correct", {"source": "human", "detail": "looked right"}, 9)
OUTCOMES = [external("A", "incorrect", 3), external("A", "correct", 4), external("B", "incorrect", 3),
            external("C", "incorrect", 3), external("D", "correct", 3), external("F", "correct", 3), NODE_OUTCOME]
CRITICS = [*critic("A", "approved", 2, GATES[0]), *critic("C", "corrected", 2, GATES[0]),
           *critic("E", "escalate", 3, GATES[1])]


def report(rows, min_outcomes=4, **kwargs):
    return calibrate(rows, tenant_id=TENANT, project_id=PROJECT, since=None, min_outcomes=min_outcomes, **kwargs)


def counts(correct=0, incorrect=0, no_outcome=0):
    return {"correct": correct, "incorrect": incorrect, "no_outcome": no_outcome}


def test_each_signal_is_counted_fired_and_quiet_by_external_outcome_as_computed_by_hand():
    made = report([*GATES, *OUTCOMES, *CRITICS])
    # Units: (A 0.3 low), (B 0.8), (C None unsupported low), (A 0.75), (D 0.5 contradicted), (E 0.9);
    # outcomes A correct (its latest), B incorrect, C incorrect, D correct, E none.
    assert made.judged == counts(correct=3, incorrect=2, no_outcome=1)
    assert made.signals == {
        "unsupported": {"fired": counts(incorrect=1), "quiet": counts(3, 1, 1)},
        "contradicted": {"fired": counts(correct=1), "quiet": counts(2, 2, 1)},
        "thin_support": {"fired": counts(), "quiet": counts(3, 2, 1)},
        "low_confidence": {"fired": counts(1, 1), "quiet": counts(2, 1, 1)},
    }
    # no_claims fires on a node, never on a claim: counted by gate row instead (FR-111).
    assert list(made.signals) == [s for s in SIGNALS if s != "no_claims"]
    assert made.no_claims_fired == 0


def test_each_threshold_counts_the_incorrect_and_correct_claims_it_would_flag():
    made = report([*GATES, *OUTCOMES])
    assert [t["threshold"] for t in made.thresholds] == [k / 20 for k in range(1, 20)] == list(THRESHOLDS)
    for t in made.thresholds:
        value = t["threshold"]
        # Incorrect: B at 0.8 and C at None (always below); correct: A 0.3, A 0.75, D 0.5.
        assert t["incorrect_flagged"] == 1 + (0.8 < value), t
        assert t["correct_flagged"] == sum(c < value for c in (0.3, 0.75, 0.5)), t


def test_a_confidence_exactly_at_a_threshold_is_not_flagged_by_it():
    made = report([*GATES, *OUTCOMES])
    at_08 = next(t for t in made.thresholds if t["threshold"] == 0.8)
    assert at_08["incorrect_flagged"] == 1, "B at exactly 0.8 is not below 0.8"
    # So the smallest value flagging every incorrect claim is the next one up.
    assert made.suggested_threshold == 0.85


def test_the_suggestion_is_none_below_min_outcomes_counted_as_distinct_judged_claims():
    rows = [*GATES, *OUTCOMES]
    assert report(rows).claims_with_outcome == 4, "A, B, C and D: F has no gate, the node is not a claim"
    assert report(rows, min_outcomes=4).suggested_threshold == 0.85
    assert report(rows, min_outcomes=5).suggested_threshold is None
    # Twenty outcomes on one claim are still one claim with an outcome (DECISION-4f1acdb2).
    many = [gate(1, ("A", [], 0.3)), *(external("A", "incorrect", 2 + i) for i in range(20))]
    assert report(many, min_outcomes=2).suggested_threshold is None
    assert report(many, min_outcomes=1).suggested_threshold == 0.35


def test_the_suggestion_is_none_with_no_incorrect_outcome_or_when_no_value_flags_them_all():
    correct_only = [gate(1, ("A", [], 0.3), ("B", [], 0.9)), external("A", "correct", 2), external("B", "correct", 2)]
    assert report(correct_only, min_outcomes=1).suggested_threshold is None
    high = [gate(1, ("A", [], 0.95), ("B", [], 0.2)), external("A", "incorrect", 2), external("B", "incorrect", 2)]
    assert report(high, min_outcomes=1).suggested_threshold is None, "0.95 is not below any of the nineteen values"
    unknown = [gate(1, ("A", [], None)), external("A", "incorrect", 2)]
    assert report(unknown, min_outcomes=1).suggested_threshold == 0.05, "None counts as below"


def test_a_claim_with_two_outcomes_counts_by_its_latest_whatever_order_the_rows_arrive_in():
    later_correct = [gate(1, ("A", [], 0.3)), external("A", "incorrect", 2), external("A", "correct", 5)]
    assert report(later_correct, min_outcomes=1).judged == counts(correct=1)
    assert report(list(reversed(later_correct)), min_outcomes=1).judged == counts(correct=1)
    later_incorrect = [gate(1, ("A", [], 0.3)), external("A", "correct", 2), external("A", "incorrect", 5)]
    assert report(list(reversed(later_incorrect)), min_outcomes=1).judged == counts(incorrect=1)
    # At the same instant, which the stores order differently, incorrect counts: the cautious reading.
    tied = [gate(1, ("A", [], 0.3)), external("A", "incorrect", 2), external("A", "correct", 2)]
    for rows in (tied, list(reversed(tied))):
        assert report(rows, min_outcomes=1).judged == counts(incorrect=1)


def test_critic_verdicts_are_reported_separately_and_never_move_the_suggestion():
    without, with_critics = report([*GATES, *OUTCOMES]), report([*GATES, *OUTCOMES, *CRITICS])
    assert with_critics.critic == {"approved": counts(correct=1), "corrected": counts(incorrect=1),
                                   "escalate": counts(no_outcome=1)}
    assert without.critic == {"approved": counts(), "corrected": counts(), "escalate": counts()}
    assert (with_critics.suggested_threshold, with_critics.thresholds, with_critics.signals) == (
        without.suggested_threshold, without.thresholds, without.signals)
    # Even a critic that approved every incorrect claim many times over.
    flood = [r for name in "BC" for _ in range(30) for r in critic(name, "approved", 5, GATES[0])]
    assert report([*GATES, *OUTCOMES, *flood]).suggested_threshold == without.suggested_threshold


def test_the_report_names_every_outcome_it_read_and_their_date_range():
    made = report([*GATES, *OUTCOMES, *CRITICS])
    assert set(made.outcomes) == {r.uri for r in OUTCOMES}, "the unjudged claim's and the node's included"
    assert (made.outcomes_from, made.outcomes_to) == (at(3), at(9))
    assert report(GATES).outcomes == () and report(GATES).outcomes_from is None
    assert json.loads(json.dumps(made.to_json()))["suggested_threshold"] == 0.85


def test_the_report_refuses_arguments_it_cannot_honour():
    for bad in (True, -1, 2.5, "20"):
        with pytest.raises(ValueError, match="min_outcomes"):
            report([], min_outcomes=bad)
    with pytest.raises(ValueError, match="since"):
        calibrate([], tenant_id=TENANT, project_id=PROJECT, since=datetime(2026, 1, 1), min_outcomes=1)


# =================================================================================================
# FR-107, AC-80: Runner.calibration_report on both stores
# =================================================================================================

PUBLIC = "93.184.216.34"
URL = "http://rivers.test/rhine"
READER = AgentSpec(id="reader", instructions="read", preferred_model="reader:r", tool_profile=("fetch_url", "record_claim"))
CRITIC_SPEC = AgentSpec(id="critic", instructions="judge", preferred_model="critic:c")


def respond(content="", calls=()):
    return ModelResponse(message=Message(role=Role.ASSISTANT, content=content, tool_calls=tuple(calls)),
                         stop_reason=StopReason.TOOL_CALLS if calls else StopReason.END_TURN, usage=Usage(5, 1, 6))


class Planner:
    def __init__(self, plan):
        self.plan = plan

    async def send(self, request):
        if not any(m.role is Role.TOOL for m in request.messages):
            return respond(calls=[ToolCall(id="plan", name=PLAN_TOOL, arguments=self.plan)])
        return respond("done")


class Reader:
    async def send(self, request):
        turns = json.loads(request.messages[0].content.split("\n")[0])
        done = sum(len(m.tool_results) for m in request.messages if m.role is Role.TOOL)
        if done < len(turns):
            return respond(calls=[ToolCall(id=f"c{done}", name=turns[done][0], arguments=turns[done][1])])
        return respond("read")


class Approver:
    async def send(self, request):
        return respond(json.dumps({"verdict": "approved", "reason": "fine"}))


class Page:
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


def claimed(statement, confidence):
    return ["record_claim", {"statement": statement, "supporting": [URL], "confidence": confidence}]


async def gated_run(store, tenant, page):
    """One gated node recording a doubtful and a confident claim, which the critic approves."""
    async def resolve(host, port):
        return [PUBLIC]

    turns = [["fetch_url", {"url": URL}], claimed("The Rhine is short.", 0.3), claimed("The Rhine is wet.", 0.9)]
    plan = {"nodes": [{"node_id": "n", "objective": json.dumps(turns), "assigned_role": "reader", "dependencies": [],
                       "acceptance_criteria": [{"kind": "critic", "description": "judge it"}]}]}
    orchestrator = Orchestrator(roles={"reader": READER}, policy=BudgetPolicy(run_ceiling_tokens=1_000_000),
                                context_policy=ContextPolicy(context_window=1_000_000),
                                gate=GatePolicy(critic=CRITIC_SPEC))
    record, _ = ledger_tools()
    fetch = fetch_tool(["rivers.test"], _resolve=resolve, _connect=lambda ip, port: ("127.0.0.1", page.port))
    runner = Runner({"planner": Planner(plan), "reader": Reader(), "critic": Approver()},
                    tools=[orchestrator.tool, fetch, record],
                    persistence=Persistence.postgres(DSN, create_schema=False) if store == "postgres" else None)
    result = await runner.run(orchestrator.agent, "go", orchestrator.config(
        tenant_id=tenant, project_id=PROJECT, model_override="planner:p", max_turns=6))
    views = await asyncio.to_thread(runner.evidence_ledger(tenant, PROJECT, result.run_id).active)
    return runner, orchestrator, result, {v.claim.statement: v.claim.uri for v in views}


async def everything(runner, tenant):
    """Every row the report could touch, as read: the tenant's verifications and claims."""
    rows = await asyncio.to_thread(runner._ledger_store().verifications_since, tenant, PROJECT)
    ledgers = {r.ledger_run_id for r in rows}
    claims = [await asyncio.to_thread(runner._ledger_store().rows, tenant, PROJECT, ledger) for ledger in ledgers]
    return sorted((r.uri, r.outcome, json.dumps(r.detail, sort_keys=True)) for r in rows), repr(claims)


@pytest.mark.parametrize("store", STORES)
async def test_the_report_reads_one_tenant_and_project_and_changes_nothing(store):
    async with Page() as page:
        runner, orchestrator, result, claims = await gated_run(store, TENANT, page)
        short, wet = claims["The Rhine is short."], claims["The Rhine is wet."]
        mine = [await runner.record_validation(TENANT, PROJECT, result.run_id, short, "fixture", "incorrect", "no"),
                await runner.record_validation(TENANT, PROJECT, result.run_id, wet, "fixture", "correct", "yes")]
        # A second tenant on the same runner, whose outcomes would move the suggestion to
        # 0.95 if they entered the report: its confident claim is incorrect.
        other_runner, _, other, other_claims = await gated_run(store, OTHER_TENANT, page)
        for uri in other_claims.values():
            await other_runner.record_validation(OTHER_TENANT, PROJECT, other.run_id, uri, "fixture", "incorrect", "x")
    before = (await everything(runner, TENANT), await everything(runner, OTHER_TENANT))
    made = await runner.calibration_report(TENANT, PROJECT, min_outcomes=2)
    after = (await everything(runner, TENANT), await everything(runner, OTHER_TENANT))
    assert before == after, "a report changes no row"
    assert orchestrator._gate.confidence_threshold == 0.7, "and no configuration"
    assert made.judged == counts(correct=1, incorrect=1)
    assert made.suggested_threshold == 0.35, "0.3 incorrect: the smallest value above it"
    assert set(made.outcomes) == {r.uri for r in mine}
    assert (made.outcomes_from, made.outcomes_to) == (mine[0].created_at, mine[1].created_at)
    assert made.signals["low_confidence"]["fired"] == counts(incorrect=1)
    assert made.critic["approved"] == counts(correct=1, incorrect=1)
    assert (made.tenant_id, made.project_id, made.min_outcomes) == (TENANT, PROJECT, 2)
    # Read from a time after everything: nothing in range.
    later = await runner.calibration_report(TENANT, PROJECT, since=datetime.now(timezone.utc) + timedelta(hours=1))
    assert later.outcomes == () and later.judged == counts() and later.suggested_threshold is None


async def test_the_report_refuses_bad_arguments_before_reading():
    runner = Runner({"critic": Approver()})
    with pytest.raises(ValueError, match="min_outcomes"):
        await runner.calibration_report(TENANT, PROJECT, min_outcomes=0.5)
    with pytest.raises(ValueError, match="since"):
        await runner.calibration_report(TENANT, PROJECT, since="2026-10-01")


# =================================================================================================
# FR-108, AC-81: the exit signal under the gate, on both stores
# =================================================================================================


def _example():
    spec = importlib.util.spec_from_file_location("example_23", REPO / "scripts" / "23_gate_contention.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("store", STORES)
async def test_the_exit_signal_holds_under_the_gate_ten_times_with_shuffled_starts_and_random_delays(store):
    example = _example()
    races = 0
    for seed in range(10):
        # A project of its own each time: the database keeps the last one's fresh versions.
        checks, _, lost = await example.offline(store, seed=seed, quiet=True, tenants=(TENANT, OTHER_TENANT),
                                                project=f"{PROJECT}-{seed}")
        failed = [label for label, passed in checks if not passed]
        assert failed == [], f"seed {seed}: {failed}"
        races += lost
    assert races == 10, "every run had one correction lose its race to another node's critic"


def test_example_23_runs_offline():
    done = subprocess.run([sys.executable, str(REPO / "scripts" / "23_gate_contention.py"), "--offline"],
                          capture_output=True, text=True, timeout=180, cwd=REPO)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "[FAIL]" not in done.stdout and done.stdout.count("[PASS]") == 12, done.stdout
    assert "suggested_threshold" in done.stdout


def test_no_new_runtime_dependency():
    """NFR-27: Phase 3's second increment adds no runtime dependency."""
    import tomllib

    declared = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    assert sorted(declared) == ["httpx==0.28.1", "jsonschema==4.26.0", "psycopg[binary]==3.3.5",
                                "psycopg_pool==3.2.6", "python-dotenv==1.2.3"]
