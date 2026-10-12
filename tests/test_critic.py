"""M27 gate: the critic (FR-104 to FR-106, AC-79).

A gated node whose rule signals fire is judged once by the policy's critic, a child of the
orchestrator run drawing on the node's own reservation, briefed with the node's answer, a
rendering of its claims and the content of the sources they cite, all as data. Its verdict
decides the node: approved, corrected (the critic's answer, and its corrections applied as
supersessions), or escalate. Nothing the critic touches gets cleaner (NFR-28).

Store calls made from these async tests go to a worker thread (FR-81). The persisted half
needs DATABASE_URL and fails rather than skips without it; every row written is removed.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import re

import psycopg
import pytest

from agentsdk import AgentSpec, BudgetPolicy, ContextPolicy, Persistence, RunStatus, Runner, SchedulerLimits
from agentsdk.builtin_tools import fetch_tool
from agentsdk.config import normalise_database_url
from agentsdk.events import EventType
from agentsdk.gate import GatePolicy
from agentsdk.ledger import ledger_tools
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.orchestrator import PLAN_TOOL, Orchestrator
from agentsdk.postgres import RunScope
from agentsdk.primitives import Message, Role, TaintFlag, ToolCall
from agentsdk.subagents import SubagentPool

DSN = normalise_database_url(os.environ.get("DATABASE_URL"))
TENANT, PROJECT = "SYN-m27", "p-m27"
PUBLIC = "93.184.216.34"
URL = "http://rivers.test/rhine"
CLAIM_URI = re.compile(r"urn:agentsdk:claim:[0-9a-f-]{36}")
SOURCE_URI = re.compile(r"urn:agentsdk:source:[0-9a-f-]{36}")


def respond(content="", calls=(), tokens=6):
    return ModelResponse(message=Message(role=Role.ASSISTANT, content=content, tool_calls=tuple(calls)),
                         stop_reason=StopReason.TOOL_CALLS if calls else StopReason.END_TURN,
                         usage=Usage(tokens - 1, 1, tokens))


class Planner:
    """Submits plan i on its i-th call while it has plans, then answers."""

    def __init__(self, *plans):
        self.plans = list(plans)

    async def send(self, request):
        shown = [m for m in request.messages if m.role is Role.TOOL]
        if len(shown) < len(self.plans):
            return respond(calls=[ToolCall(id=f"plan-{len(shown)}", name=PLAN_TOOL,
                                           arguments=self.plans[len(shown)])])
        return respond("the plan is done")


class Reader:
    """A node's child: its objective is a JSON list of [tool, arguments], one a turn,
    then [\"answer\", text] if given, else "read"."""

    async def send(self, request):
        turns = json.loads(request.messages[0].content.split("\n")[0])
        calls = [t for t in turns if t[0] != "answer"]
        answer = next((t[1] for t in turns if t[0] == "answer"), "read")
        done = sum(len(m.tool_results) for m in request.messages if m.role is Role.TOOL)
        if done < len(calls):
            name, arguments = calls[done]
            return respond(calls=[ToolCall(id=f"c{done}", name=name, arguments=arguments)])
        return respond(answer)


class Critic:
    """The critic's model. `verdicts` maps a word in the critic criterion's description to
    a function of (claim uris, source uris, attempt) giving the verdict's text."""

    def __init__(self, verdicts, before=None):
        self.verdicts, self.before = verdicts, before
        self.runs, self.requests = 0, []

    async def send(self, request):
        self.requests.append(request)
        content = request.messages[0].content
        attempt = sum(1 for m in request.messages if m.role is Role.ASSISTANT)
        if attempt == 0:
            self.runs += 1
        claims = list(dict.fromkeys(CLAIM_URI.findall(content)))
        sources = list(dict.fromkeys(SOURCE_URI.findall(content)))
        if self.before is not None:
            await self.before(claims, sources)
        for word, verdict in self.verdicts.items():
            if f"judge it ({word})" in content:
                return respond(verdict(claims, sources, attempt))
        return respond(json.dumps({"verdict": "approved", "reason": "nothing to say"}))


def approved(claims, sources, attempt):
    return json.dumps({"verdict": "approved", "reason": "the claims hold"})


def escalated(claims, sources, attempt):
    return json.dumps({"verdict": "escalate", "reason": "cannot tell"})


def corrected(answer, statement):
    def verdict(claims, sources, attempt):
        return json.dumps({"verdict": "corrected", "reason": "the page says otherwise", "corrected_answer": answer,
                           "corrections": [{"claim": claims[0], "statement": statement, "supporting": sources[:1]}]})
    return verdict


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


READER = AgentSpec(id="reader", instructions="read", preferred_model="reader:r", tool_profile=("fetch_url", "record_claim"))
CRITIC = AgentSpec(id="critic", instructions="judge", preferred_model="critic:c")
RECORD, SUPERSEDE = ledger_tools()
READ = ["fetch_url", {"url": URL}]


def doubted(statement, *, confidence=0.3):
    """A claim citing the page, with a confidence low enough to fire low_confidence."""
    return ["record_claim", {"statement": statement, "supporting": [URL], "confidence": confidence}]


def node(node_id, turns, word, **fields):
    return {"node_id": node_id, "objective": json.dumps(turns), "assigned_role": "reader", "dependencies": [],
            "acceptance_criteria": [{"kind": "critic", "description": f"judge it ({word})"}], **fields}


def setup(pages, *plans, critic, store="memory", gate=None, limits=None, policy=None):
    orchestrator = Orchestrator(
        roles={"reader": READER}, policy=policy or BudgetPolicy(run_ceiling_tokens=1_000_000), limits=limits,
        context_policy=ContextPolicy(context_window=1_000_000), gate=gate or GatePolicy(critic=CRITIC))
    runner = Runner({"planner": Planner(*plans), "reader": Reader(), "critic": critic},
                    tools=[orchestrator.tool, fetch(pages), RECORD],
                    persistence=Persistence.postgres(DSN, create_schema=False) if store == "postgres" else None)
    return orchestrator, runner


async def orchestrate(runner, orchestrator):
    return await runner.run(orchestrator.agent, "go", orchestrator.config(
        tenant_id=TENANT, project_id=PROJECT, model_override="planner:p", max_turns=8))


def finished(result, node_id):
    return [(e.payload["status"], e.payload.get("reason")) for e in result.events
            if e.event_type is EventType.PLAN_NODE_FINISHED and e.task_id == node_id]


def events(result, kind):
    return [e for e in result.events if e.event_type is kind]


async def ledger(runner, run_id, method="active", *args):
    return await asyncio.to_thread(getattr(runner.evidence_ledger(TENANT, PROJECT, run_id), method), *args)


async def verifications(runner, run_id, **filters):
    return await asyncio.to_thread(runner._ledger_store().verifications, TENANT, PROJECT, run_id, **filters)


@pytest.fixture(autouse=True)
def remove_what_the_test_wrote():
    yield
    if DSN:
        with psycopg.connect(DSN, autocommit=True) as conn:
            ids = [r[0] for r in conn.execute("SELECT run_id FROM runs WHERE tenant_id = %s", (TENANT,))]
            conn.execute("DELETE FROM verifications WHERE ledger_run_id = ANY(%s) OR subject_run = ANY(%s)", (ids, ids))
            conn.execute("DELETE FROM claim_citations WHERE contribution_id IN (SELECT contribution_id FROM "
                         "claim_contributions WHERE run_id = ANY(%s))", (ids,))
            conn.execute("DELETE FROM claim_contributions WHERE run_id = ANY(%s)", (ids,))
            while conn.execute("DELETE FROM claims c WHERE ledger_run_id = ANY(%s) AND NOT EXISTS "
                               "(SELECT 1 FROM claims s WHERE s.supersedes = c.claim_id)", (ids,)).rowcount:
                pass
            conn.execute("DELETE FROM source_versions WHERE source_run = ANY(%s)", (ids,))
            conn.execute("DELETE FROM artifacts WHERE source_run = ANY(%s)", (ids,))
            conn.execute("DELETE FROM plan_node_states WHERE run_id = ANY(%s)", (ids,))
            conn.execute("DELETE FROM plan_versions WHERE run_id = ANY(%s)", (ids,))
            for table in ("run_events", "messages", "execution_manifests"):
                conn.execute(f"DELETE FROM {table} WHERE run_id = ANY(%s)", (ids,))
            conn.execute("DELETE FROM runs WHERE run_id = ANY(%s)", (ids,))


# =================================================================================================
# FR-104, FR-105: the three verdicts, on both stores
# =================================================================================================


@pytest.mark.parametrize("store", ["memory", "postgres"])
async def test_an_approved_node_is_done_and_the_critic_ran_once_as_a_child_of_the_orchestrator(store):
    critic = Critic({"approve": approved})
    async with Pages() as pages:
        orchestrator, runner = setup(pages, {"nodes": [node("n", [READ, doubted("The Rhine reaches the North Sea.")],
                                                            "approve")]}, critic=critic, store=store)
        result = await orchestrate(runner, orchestrator)
    assert finished(result, "n") == [("done", None)]
    assert critic.runs == 1
    [verdict] = events(result, EventType.CRITIC_VERDICT)
    assert verdict.task_id == "n" and verdict.payload["verdict"] == "approved" and verdict.payload["superseded"] == []
    critic_run = verdict.payload["critic_run"]
    rows = await verifications(runner, result.run_id, kinds=("critic",))
    [view] = await ledger(runner, result.run_id)
    assert {(r.claim_id or "node") for r in rows} == {view.claim.claim_id, "node"}
    assert all(r.outcome == "approved" and r.detail["critic_run"] == critic_run for r in rows)
    assert verdict.payload["verification"] in {r.uri for r in rows}
    # The critic's briefing: the answer, the claims and the page, each a data input in
    # its request's provenance manifest; the claims rendering at least as tainted as the claim.
    first = critic.requests[0]
    briefed = [e for e in first.metadata.get("provenance", []) if "briefed_input" in e]
    assert len(briefed) == 3, first.metadata
    assert first.messages[0].content.count("--- data input ") == 3
    # The claims rendering and the page: at least as tainted as the claim.
    assert all(TaintFlag.EXTERNAL_CONTENT.value in e["taint_flags"] for e in briefed[1:]), briefed
    assert "judge it (approve)" in first.messages[0].content
    assert not first.tools, "the critic has no tools"


@pytest.mark.parametrize("store", ["memory", "postgres"])
async def test_a_correction_supersedes_the_claim_as_the_critic_and_the_node_takes_the_corrected_answer(store):
    critic = Critic({"correct": corrected("The Rhine reaches the North Sea, not the Black Sea.",
                                          "The Rhine reaches the North Sea.")})
    async with Pages() as pages:
        orchestrator, runner = setup(pages, {"nodes": [node("n", [READ, doubted("The Rhine reaches the Black Sea."),
                                                                  ["answer", "The Rhine reaches the Black Sea."]],
                                                            "correct")]}, critic=critic, store=store)
        result = await orchestrate(runner, orchestrator)
    assert finished(result, "n") == [("done", None)]
    [verdict] = events(result, EventType.CRITIC_VERDICT)
    critic_run = verdict.payload["critic_run"]
    [active] = await ledger(runner, result.run_id)
    assert active.claim.statement == "The Rhine reaches the North Sea."
    [contribution] = active.contributions
    assert contribution.run_id == critic_run and contribution.agent_id == "critic"
    assert contribution.node_id == "n#critic" and contribution.confidence is None
    assert TaintFlag.EXTERNAL_CONTENT in active.provenance.taint_flags
    chain = await ledger(runner, result.run_id, "history", active.claim.claim_id)
    assert [c.statement for c in chain] == ["The Rhine reaches the Black Sea.", "The Rhine reaches the North Sea."]
    assert verdict.payload["superseded"] == [chain[0].uri]
    [superseded] = events(result, EventType.CLAIM_SUPERSEDED)
    assert superseded.payload["claim_uri"] == active.claim.uri
    # The node's output is the corrected answer, at least as tainted as the answer and the critic.
    run = orchestrator._runs[result.run_id]
    answer, provenance = run.answers["n"]
    assert answer == "The Rhine reaches the North Sea, not the Black Sea."
    assert TaintFlag.EXTERNAL_CONTENT in provenance.taint_flags
    stored = await runner._artifacts_for(TENANT, PROJECT).get(run.outputs["n"])
    assert stored.decode() == answer


@pytest.mark.parametrize("store", ["memory", "postgres"])
async def test_escalate_fails_the_node_and_the_rerun_is_gated_afresh_with_its_own_critic_run(store):
    critic = Critic({"escalate": escalated, "approve": approved})
    turns = [READ, doubted("The Rhine reaches the North Sea.")]
    async with Pages() as pages:
        orchestrator, runner = setup(pages, {"nodes": [node("n", turns, "escalate")]},
                                     {"nodes": [node("n", turns, "approve")]}, critic=critic, store=store)
        result = await orchestrate(runner, orchestrator)
    first, second = finished(result, "n")
    assert first[0] == "failed" and first[1] == "critic_escalated: cannot tell"
    assert second == ("done", None)
    assert critic.runs == 2, "one critic run per execution"
    assert [e.payload["verdict"] for e in events(result, EventType.CRITIC_VERDICT)] == ["escalate", "approved"]
    assert len(orchestrator.plans(result.run_id)) == 2


# =================================================================================================
# FR-104: the briefing, the budget and the spawn
# =================================================================================================


async def test_clean_signals_run_no_critic():
    critic = Critic({})
    async with Pages() as pages:
        orchestrator, runner = setup(pages, {"nodes": [node("n", [READ, doubted("Fine.", confidence=0.9)], "x")]},
                                     critic=critic)
        result = await orchestrate(runner, orchestrator)
    assert finished(result, "n") == [("done", None)] and critic.runs == 0
    assert events(result, EventType.CRITIC_VERDICT) == []


async def test_a_node_whose_reservation_cannot_pay_for_a_call_fails_budget_exceeded_with_no_critic_run():
    critic = Critic({"approve": approved})
    async with Pages() as pages:
        # Three child calls of 6 tokens exactly spend a reservation of 18.
        orchestrator, runner = setup(pages, {"nodes": [node("n", [READ, doubted("The Rhine.")], "approve",
                                                            budget_reservation={"tokens": 18})]}, critic=critic)
        result = await orchestrate(runner, orchestrator)
    assert finished(result, "n") == [("failed", "budget_exceeded")]
    assert critic.runs == 0
    assert orchestrator._runs[result.run_id].pool.spawned(result.run_id) == 1, "a critic run was started"
    assert len(orchestrator.plans(result.run_id)) == 1, "budget_exceeded is not replanned"


async def test_a_critic_that_runs_out_of_the_nodes_reservation_fails_it_budget_exceeded_unreplanned():
    def never_valid(claims, sources, attempt):
        return json.dumps({"verdict": "approved"})  # no reason: re-asked, and the re-ask cannot be paid for

    critic = Critic({"approve": never_valid})
    async with Pages() as pages:
        # Three child calls of 6 tokens and one critic call spend a reservation of 24.
        orchestrator, runner = setup(pages, {"nodes": [node("n", [READ, doubted("The Rhine.")], "approve",
                                                            budget_reservation={"tokens": 24})]},
                                     {"nodes": [node("n", [READ, doubted("The Rhine.")], "approve")]}, critic=critic)
        result = await orchestrate(runner, orchestrator)
    assert finished(result, "n") == [("failed", "budget_exceeded")]
    assert critic.runs == 1 and len(critic.requests) == 1
    assert len(orchestrator.plans(result.run_id)) == 1, "budget_exceeded is not replanned"


async def test_the_critic_draws_on_the_nodes_own_lease(monkeypatch):
    critic = Critic({"approve": approved})
    leases = []
    real = SubagentPool.spawn

    async def spawn(self, **kwargs):
        leases.append((kwargs.get("node_id"), kwargs.get("lease"), kwargs.get("depth")))
        return await real(self, **kwargs)

    monkeypatch.setattr(SubagentPool, "spawn", spawn)
    async with Pages() as pages:
        orchestrator, runner = setup(pages, {"nodes": [node("n", [READ, doubted("The Rhine.")], "approve")]},
                                     critic=critic)
        result = await orchestrate(runner, orchestrator)
    assert finished(result, "n") == [("done", None)]
    (child, child_lease, child_depth), (critic_node, critic_lease, critic_depth) = leases
    assert (child, critic_node) == ("n", "n#critic") and critic_lease is child_lease and critic_depth == child_depth
    assert critic_lease.released


async def test_a_briefing_over_the_limit_fails_the_node_with_no_critic_run():
    critic = Critic({"approve": approved})
    async with Pages() as pages:
        orchestrator, runner = setup(pages, {"nodes": [node("n", [READ, doubted("The Rhine.")], "approve")]},
                                     critic=critic, gate=GatePolicy(critic=CRITIC, max_briefing_chars=40))
        result = await orchestrate(runner, orchestrator)
    [(status, reason), *_] = finished(result, "n")
    assert status == "failed" and reason == "critic_failed: briefing_too_large", reason
    assert critic.runs == 0


async def test_a_refused_critic_spawn_fails_the_node_spawn_refused():
    critic = Critic({"approve": approved})
    async with Pages() as pages:
        orchestrator, runner = setup(pages, {"nodes": [node("n", [READ, doubted("The Rhine.")], "approve")]},
                                     critic=critic, limits=SchedulerLimits(max_tasks_per_run=1))
        result = await orchestrate(runner, orchestrator)
    [(status, reason), *_] = finished(result, "n")
    assert status == "failed" and reason.startswith("critic_failed: spawn_refused:"), reason
    assert critic.runs == 0


async def test_an_invalid_verdict_is_re_asked_once_then_fails_the_node():
    def never_valid(claims, sources, attempt):
        return json.dumps({"verdict": "approved"})  # no reason

    def outside(claims, sources, attempt):  # names a claim that is not this node's
        return json.dumps({"verdict": "corrected", "reason": "r", "corrected_answer": "x", "corrections": [
            {"claim": "urn:agentsdk:claim:00000000-0000-4000-8000-000000000000", "statement": "s",
             "supporting": sources[:1]}]})

    def unbriefed(claims, sources, attempt):  # cites a source the critic was not shown
        return json.dumps({"verdict": "corrected", "reason": "r", "corrected_answer": "x", "corrections": [
            {"claim": claims[0], "statement": "s",
             "supporting": ["urn:agentsdk:source:00000000-0000-4000-8000-000000000000"]}]})

    for verdict in (never_valid, outside, unbriefed):
        critic = Critic({"approve": verdict})
        async with Pages() as pages:
            orchestrator, runner = setup(pages, {"nodes": [node("n", [READ, doubted("The Rhine.")], "approve")]},
                                         critic=critic)
            result = await orchestrate(runner, orchestrator)
        [(status, reason), *_] = finished(result, "n")
        assert status == "failed" and reason == "critic_failed: output_contract_violation", reason
        assert critic.runs == 1 and len([r for r in critic.requests]) == 2, "re-asked exactly once"
        assert await verifications(runner, result.run_id, kinds=("critic",)) == []


async def test_an_empty_corrected_answer_is_accepted_beside_a_verdict_that_does_not_use_it():
    """Found live: a model fills every field of the verdict, so `approved` arrived with
    "corrected_answer": "" and was refused twice. Only a correction needs an answer."""
    def filled(claims, sources, attempt):
        return json.dumps({"verdict": "approved", "reason": "fine", "corrected_answer": "", "corrections": [],
                           "paraphrases": []})

    def empty_correction(claims, sources, attempt):
        return json.dumps({"verdict": "corrected", "reason": "r", "corrected_answer": "", "corrections": []})

    critic = Critic({"approve": filled, "correct": empty_correction})
    async with Pages() as pages:
        orchestrator, runner = setup(pages, {"nodes": [node("a", [READ, doubted("A.")], "approve"),
                                                        node("c", [READ, doubted("C.")], "correct")]}, critic=critic)
        result = await orchestrate(runner, orchestrator)
    assert finished(result, "a") == [("done", None)]
    [(status, reason), *_] = finished(result, "c")
    assert status == "failed" and reason == "critic_failed: output_contract_violation", reason


async def test_a_corrected_answer_that_fails_the_nodes_schema_is_refused_with_nothing_superseded():
    schema = {"type": "object", "required": ["summary"], "properties": {"summary": {"type": "string"}}}
    critic = Critic({"correct": corrected("plain text, not the schema", "The Rhine reaches the North Sea.")})
    async with Pages() as pages:
        orchestrator, runner = setup(pages, {"nodes": [node(
            "n", [READ, doubted("The Rhine reaches the Black Sea."), ["answer", json.dumps({"summary": "s"})]],
            "correct", expected_output_schema=schema)]}, critic=critic)
        result = await orchestrate(runner, orchestrator)
    [(status, reason), *_] = finished(result, "n")
    assert status == "failed" and reason.startswith("critic_failed: correction refused: output_schema:"), reason
    [view] = await ledger(runner, result.run_id)
    assert view.claim.statement == "The Rhine reaches the Black Sea." and view.claim.supersedes is None
    assert events(result, EventType.CLAIM_SUPERSEDED) == []


async def test_a_correction_the_store_refuses_fails_the_node_and_keeps_those_applied():
    def two(claims, sources, attempt):
        return json.dumps({"verdict": "corrected", "reason": "r", "corrected_answer": "x", "corrections": [
            {"claim": claims[0], "statement": "Merged statement.", "supporting": sources[:1]},
            {"claim": claims[1], "statement": "Merged statement.", "supporting": sources[:1]}]})

    critic = Critic({"correct": two})
    async with Pages() as pages:
        orchestrator, runner = setup(pages, {"nodes": [node(
            "n", [READ, doubted("First claim."), doubted("Second claim.")], "correct")]}, critic=critic)
        result = await orchestrate(runner, orchestrator)
    [(status, reason), *_] = finished(result, "n")
    assert status == "failed" and reason.startswith("critic_failed: correction refused:"), reason
    statements = sorted(v.claim.statement for v in await ledger(runner, result.run_id))
    assert statements == ["Merged statement.", "Second claim."], "the first correction stays applied"
    rows = await verifications(runner, result.run_id, kinds=("critic",))
    assert rows and all(len(r.detail["applied"]) == 1 and len(r.detail["refused"]) == 1 for r in rows)


async def test_a_correction_that_lost_a_race_to_another_supersession_fails_the_node_and_it_replans():
    async def supersede_first(claims, sources):
        if claims and not supersede_first.done:
            supersede_first.done = True
            store = supersede_first.runner._ledger_store()
            from agentsdk.ledger import NewContribution
            from agentsdk.primitives import ContentProvenance

            view = await ledger(supersede_first.runner, supersede_first.top(), "claim", claims[0])
            await asyncio.to_thread(
                store.supersede, tenant_id=TENANT, project_id=PROJECT, ledger_run_id=supersede_first.top(),
                old_claim_id=view.claim.claim_id, statement="Someone else's correction.",
                contribution=NewContribution(run_id=supersede_first.top(), node_id=None, agent_id="other",
                                             supporting=(), contradicting=(), confidence=None,
                                             provenance=ContentProvenance.internal_tool(), why="first"))

    supersede_first.done = False
    critic = Critic({"correct": corrected("x", "The Rhine reaches the North Sea."), "approve": approved},
                    before=supersede_first)
    turns = [READ, doubted("The Rhine reaches the Black Sea.")]
    async with Pages() as pages:
        orchestrator, runner = setup(pages, {"nodes": [node("n", turns, "correct")]},
                                     {"nodes": [node("n", turns, "approve")]}, critic=critic)
        supersede_first.runner = runner
        supersede_first.top = lambda: next(iter(orchestrator._runs))
        result = await orchestrate(runner, orchestrator)
    first, second = finished(result, "n")
    assert first[0] == "failed" and "already superseded" in first[1], first
    assert second == ("done", None)


async def test_the_orchestrator_hears_the_critic_under_the_critics_label_whatever_the_verdict(monkeypatch):
    real = SubagentPool.spawn

    async def spawn(self, **kwargs):
        result = await real(self, **kwargs)
        if kwargs.get("node_id", "").endswith("#critic"):
            result = dataclasses.replace(result, provenance=result.provenance.with_taint(TaintFlag.SECRET_BEARING))
        return result

    monkeypatch.setattr(SubagentPool, "spawn", spawn)
    critic = Critic({"approve": approved, "escalate": escalated})
    async with Pages() as pages:
        orchestrator, runner = setup(pages, {"nodes": [
            node("a", [READ, doubted("A.")], "approve"), node("e", [READ, doubted("E.")], "escalate")]},
            {"nodes": [node("e", [READ, doubted("E.")], "approve")]}, critic=critic)
        result = await orchestrate(runner, orchestrator)
    sessions = runner._sessions_for(RunScope(run_id=result.run_id, tenant_id=TENANT, project_id=PROJECT))
    history = await asyncio.to_thread(sessions.history, result.run_id)
    reports = [r for m in history for r in m.tool_results]  # run_plan is the orchestrator's only tool
    assert reports and all(TaintFlag.SECRET_BEARING in r.provenance.taint_flags for r in reports), reports


async def test_with_no_critic_configured_a_critic_criterion_still_fails_criterion_not_available():
    async with Pages() as pages:
        orchestrator = Orchestrator(roles={"reader": READER}, policy=BudgetPolicy(run_ceiling_tokens=1_000_000),
                                    context_policy=ContextPolicy(context_window=1_000_000))
        runner = Runner({"planner": Planner({"nodes": [node("n", [READ, doubted("A.")], "x")]}), "reader": Reader()},
                        tools=[orchestrator.tool, fetch(pages), RECORD])
        result = await orchestrate(runner, orchestrator)
    [(status, reason)] = finished(result, "n")
    assert status == "failed" and reason.startswith("criterion_not_available: critic judge it (x)"), reason


# =================================================================================================
# FR-106: example 22, offline on both stores
# =================================================================================================


@pytest.mark.parametrize("store", ["memory", "postgres"])
async def test_example_22_runs_offline_with_every_check_passing(store):
    import importlib.util
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "22_critic.py"
    spec = importlib.util.spec_from_file_location("example_22", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    checks, runs = await module.offline(store, quiet=True)
    failed = [label for label, passed in checks if not passed]
    assert len(checks) == 7 and not failed, failed
    if store == "postgres":
        _remove_example_runs(runs)


def _remove_example_runs(runs):
    """The example writes under example-tenant: removed by the run ids it returned, and
    every run beneath them, never by its tenant."""
    with psycopg.connect(DSN, autocommit=True) as conn:
        ids = [r[0] for r in conn.execute(
            "WITH RECURSIVE tree AS (SELECT run_id FROM runs WHERE run_id = ANY(%s::uuid[]) UNION "
            "SELECT r.run_id FROM runs r JOIN tree t ON r.parent_run_id = t.run_id) SELECT run_id FROM tree",
            (runs,)).fetchall()]
        ledgers = list(runs)
        conn.execute("DELETE FROM verifications WHERE ledger_run_id = ANY(%s::uuid[])", (ledgers,))
        conn.execute("DELETE FROM claim_citations WHERE contribution_id IN (SELECT contribution_id FROM "
                     "claim_contributions WHERE run_id = ANY(%s))", (ids,))
        conn.execute("DELETE FROM claim_contributions WHERE run_id = ANY(%s)", (ids,))
        while conn.execute("DELETE FROM claims c WHERE ledger_run_id = ANY(%s::uuid[]) AND NOT EXISTS "
                           "(SELECT 1 FROM claims s WHERE s.supersedes = c.claim_id)", (ledgers,)).rowcount:
            pass
        conn.execute("DELETE FROM source_versions WHERE source_run = ANY(%s)", (ids,))
        conn.execute("DELETE FROM artifacts WHERE source_run = ANY(%s)", (ids,))
        conn.execute("DELETE FROM plan_node_states WHERE run_id = ANY(%s)", (ids,))
        conn.execute("DELETE FROM plan_versions WHERE run_id = ANY(%s)", (ids,))
        for table in ("run_events", "messages", "execution_manifests"):
            conn.execute(f"DELETE FROM {table} WHERE run_id = ANY(%s)", (ids,))
        conn.execute("DELETE FROM runs WHERE run_id = ANY(%s)", (ids,))
