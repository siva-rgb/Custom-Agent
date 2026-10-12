"""22 - The critic: rule signals first, a critic only when they fire, at most once a node.

Phase 3's second increment (FR-104 to FR-106). A plan of three nodes, each carrying a
`critic` criterion, runs under a GatePolicy. Each node's reader fetches a page and records
a claim about it:

  * clean    -- the claim cites the page with high confidence: the signals are clean, so
                the node is cleared with no model call
  * approve  -- low confidence fires a signal; the critic is called once and approves
  * correct  -- low confidence again; the critic corrects the claim, which is superseded
                as the critic's own contribution, and the node takes its corrected answer

Then a second plan whose critic escalates: the node fails `critic_escalated`, the plan is
replanned, and the rerun is gated afresh with its own one critic run. Checked against the
store: the critic ran exactly for the executions whose signals fired, once each, and no
claim, answer or correction is less tainted than the page it rests on (NFR-28).

Run it
  python scripts/22_critic.py            # live: gateway + PostgreSQL (DATABASE_URL in .env)
  python scripts/22_critic.py --offline  # in memory, local pages and scripted agents

Live, the critic is a real model, so its verdicts are its own: the run prints them, and
checks only what must hold whatever it decides.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from urllib.parse import urlsplit

from agentsdk import (
    AgentSpec,
    BudgetPolicy,
    ContextPolicy,
    GatePolicy,
    Message,
    Orchestrator,
    Role,
    RunStatus,
    Runner,
)
from agentsdk.builtin_tools import fetch_tool
from agentsdk.events import EventType
from agentsdk.ledger import ledger_tools
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.primitives import ToolCall, TrustZone

TENANT, PROJECT = "example-tenant", "examples"
TOOLS = ("fetch_url", "record_claim")
LIVE_OUTPUT_TOKENS = 4000
OFFLINE_PAGE = "http://rivers.example/rhine"
LIVE_PAGE = "https://example.com/"


def reader_turns(page: str, statement: str, confidence: float) -> list[list]:
    return [[["fetch_url", {"url": page}]],
            [["record_claim", {"statement": statement, "supporting": [page], "confidence": confidence}]]]


def described(turns: list[list[list]]) -> str:
    lines = [f"Turn {i}: in one response, call " + "; ".join(
        f"{name} with exactly these arguments: {json.dumps(args)}" for name, args in calls)
        for i, calls in enumerate(turns, 1)]
    # Live, a model given a fetch and a claim citing it may batch them, and a citation of a
    # page fetched in the same batch is refused (FR-93): it is told to make a failed call again.
    return ("Do exactly these turns, making every call of a turn in the same response and waiting for its "
            "results before the next turn. If a call returns an error, make that call again in your next "
            "response. Then answer in one sentence what the page says.\n" + "\n".join(lines))


def briefed(turns: list[list[list]], live: bool) -> str:
    return described(turns) if live else json.dumps(turns)


def node(node_id: str, turns: list, question: str, live: bool) -> dict:
    return {"node_id": node_id, "objective": briefed(turns, live), "assigned_role": "reader", "dependencies": [],
            "acceptance_criteria": [{"kind": "critic", "description": question}]}


def plans(page: str, live: bool) -> tuple[dict, dict, dict]:
    """The three-node plan, and the escalating plan with the version that replaces it."""
    first = {"nodes": [
        node("clean", reader_turns(page, "The page describes example domains.", 0.95),
             "Is the claim supported by the page? (clean)", live),
        node("approve", reader_turns(page, "The page is about domains reserved for documentation.", 0.4),
             "Is the claim supported by the page? (approve)", live),
        node("correct", reader_turns(page, "The page sells domain names.", 0.4),
             "Is the claim supported by the page? (correct)", live),
    ]}
    escalating = {"nodes": [node("doubtful", reader_turns(page, "The page is older than any other page.", 0.4),
                                 "Can this be judged from the page? (escalate)", live)]}
    rerun = {"nodes": [node("doubtful", reader_turns(page, "The page is a web page.", 0.4),
                            "Is the claim supported by the page? (approve)", live)]}
    return first, escalating, rerun


# --- offline: scripted agents ----------------------------------------------------------------


def respond(content="", calls=()):
    return ModelResponse(message=Message(role=Role.ASSISTANT, content=content, tool_calls=tuple(calls)),
                         stop_reason=StopReason.TOOL_CALLS if calls else StopReason.END_TURN, usage=Usage(20, 5, 25))


class ScriptedPlanner:
    """Submits each plan its task holds, one a call, then answers."""

    async def send(self, request):
        text = request.messages[0].content
        documents = json.loads(text[text.index("["):])
        done = sum(1 for m in request.messages if m.tool_results)
        if done < len(documents):
            return respond(calls=[ToolCall(id=f"plan-{done}", name="run_plan", arguments=documents[done])])
        return respond("The plan is done.")


class ScriptedReader:
    async def send(self, request):
        turns = json.loads(request.messages[0].content.split("\n")[0])
        done = sum(1 for m in request.messages if m.tool_results)
        if done < len(turns):
            return respond(calls=[ToolCall(id=f"c{done}-{i}", name=name, arguments=arguments)
                                  for i, (name, arguments) in enumerate(turns[done])])
        return respond("The page describes the domains it reserves for examples.")


class ScriptedCritic:
    """Approves, corrects or escalates as the criterion's marker says."""

    async def send(self, request):
        import re

        content = request.messages[0].content
        claims = list(dict.fromkeys(re.findall(r"urn:agentsdk:claim:[0-9a-f-]{36}", content)))
        sources = list(dict.fromkeys(re.findall(r"urn:agentsdk:source:[0-9a-f-]{36}", content)))
        if "(correct)" in content:
            verdict = {"verdict": "corrected", "reason": "the page reserves domains for documentation; it sells nothing",
                       "corrected_answer": "The page describes domains reserved for documentation, not for sale.",
                       "corrections": [{"claim": claims[0], "statement": "The page does not sell domain names.",
                                        "supporting": sources[:1]}]}
        elif "(escalate)" in content:
            verdict = {"verdict": "escalate", "reason": "the page says nothing about its age"}
        else:
            verdict = {"verdict": "approved", "reason": "the page supports the claim"}
        return respond(json.dumps(verdict))


def orchestrator_for(reader: str, critic: str, live: bool = False) -> Orchestrator:
    tokens = LIVE_OUTPUT_TOKENS if live else None
    roles = {"reader": AgentSpec(id="reader", instructions="You are a careful researcher.", tool_profile=TOOLS,
                                 preferred_model=reader, max_output_tokens=tokens)}
    critic_spec = AgentSpec(id="critic", instructions="You are a careful critic of research.",
                            preferred_model=critic, max_output_tokens=tokens)
    return Orchestrator(roles=roles, policy=BudgetPolicy(run_ceiling_tokens=2_000_000),
                        context_policy=ContextPolicy(compact_at=None), gate=GatePolicy(critic=critic_spec))


# --- the run and its checks ------------------------------------------------------------------


def taint_holds(claims, answers, source_of) -> bool:
    """Every claim and every node answer at least as tainted as the pages: no less taint,
    and untrusted wherever a page is. False for nothing to check."""
    held = bool(claims)
    pages = [source_of(vid) for view in claims for vid in (*view.supporting, *view.contradicting)]
    for item in [*(v.provenance for v in claims), *answers]:
        for page in pages:
            held &= page.provenance.taint_flags <= item.taint_flags
            held &= item.trust_zone is TrustZone.UNTRUSTED or page.provenance.trust_zone is not TrustZone.UNTRUSTED
    return held


async def run_plans(runner, orchestrator, documents, planner, live):
    lead = ("Call run_plan with exactly this document, then answer in one sentence.\n" if live
            else json.dumps(documents))
    task = lead + (json.dumps(documents[0]) if live else "")
    return await runner.run(orchestrator.agent, task, orchestrator.config(
        tenant_id=TENANT, project_id=PROJECT, model_override=planner, max_turns=8,
        max_output_tokens=LIVE_OUTPUT_TOKENS if live else None))


async def demonstrate(runner, orchestrator, page, planner, live, say):
    first, escalating, rerun = plans(page, live)
    top = await run_plans(runner, orchestrator, [first], planner, live)
    second = await run_plans(runner, orchestrator, [escalating, rerun], planner, live)

    checks, store = [], runner._evidence_store()
    for result, label in ((top, "three-node plan"), (second, "escalating plan")):
        gated = [e for e in result.events if e.event_type is EventType.GATE_EVALUATED]
        verdicts = [e for e in result.events if e.event_type is EventType.CRITIC_VERDICT]
        finished = [(e.task_id, e.payload["status"], e.payload.get("reason")) for e in result.events
                    if e.event_type is EventType.PLAN_NODE_FINISHED]
        say(f"{label} {result.run_id}: {result.status.value}")
        for event in gated:
            say(f"  gate {event.task_id}: signals {event.payload['signals'] or 'none'} -> {event.payload['outcome']}")
        for event in verdicts:
            say(f"  critic {event.task_id}: {event.payload['verdict']}, superseded {event.payload['superseded']}")
        for node_id, status, reason in finished:
            say(f"  node {node_id}: {status}{f' ({reason})' if reason else ''}")
        # Each node execution, by plan version and node: the critic's outcome is its verdict, or
        # the reason the node failed in judging (FR-104) -- one for each execution that fired,
        # none for one that was cleared, and never two.
        fired = {(e.payload["version"], e.task_id) for e in gated if e.payload["outcome"] == "critic_required"}
        judged = [(e.payload["version"], e.task_id) for e in verdicts] + [
            (e.payload["version"], e.task_id) for e in result.events if e.event_type is EventType.PLAN_NODE_FINISHED
            and (e.payload.get("reason") or "").startswith(("critic_failed", "budget_exceeded"))]
        checks.append((f"{label}: the critic ran exactly for the executions whose signals fired, once each",
                       sorted(judged) == sorted(fired)))
        ledger = runner.evidence_ledger(TENANT, PROJECT, result.run_id)
        claims = await asyncio.to_thread(ledger.active)
        run = orchestrator._runs[result.run_id]
        checks.append((f"{label}: no claim or answer is less tainted than the page it rests on",
                       await asyncio.to_thread(taint_holds, claims, [p for _, p in run.answers.values()],
                                               lambda vid: store.get(TENANT, PROJECT, vid))))
        for view in claims:
            say(f"  claim {view.claim.statement!r}: {view.provenance.trust_zone.value}, "
                f"{sorted(f.value for f in view.provenance.taint_flags)}")
    if not live:
        statuses = {(e.task_id, e.payload["status"]) for e in top.events if e.event_type is EventType.PLAN_NODE_FINISHED}
        by_node = {e.task_id: e.payload for e in top.events if e.event_type is EventType.CRITIC_VERDICT}
        checks.append(("the clean node was cleared with no critic, the others judged and done",
                       statuses == {("clean", "done"), ("approve", "done"), ("correct", "done")}
                       and set(by_node) == {"approve", "correct"}))
        ledger = runner.evidence_ledger(TENANT, PROJECT, top.run_id)
        corrected = [v for v in await asyncio.to_thread(ledger.active) if v.claim.supersedes]
        chain = await asyncio.to_thread(ledger.history, corrected[0].claim.claim_id) if corrected else []
        checks.append(("the correction superseded the claim, as the critic's contribution",
                       len(chain) == 2 and corrected[0].contributions[0].agent_id == "critic"))
        reruns = [(e.payload["status"], e.payload.get("reason", "")) for e in second.events
                  if e.event_type is EventType.PLAN_NODE_FINISHED]
        checks.append(("escalate failed the node and the rerun was judged afresh and done",
                       len(reruns) == 2 and reruns[0][1].startswith("critic_escalated")
                       and reruns[1][0] == "done" and len([e for e in second.events
                                                            if e.event_type is EventType.CRITIC_VERDICT]) == 2))
    return checks, [top.run_id, second.run_id]


async def offline(store: str = "memory", quiet: bool = False):
    """The example on a local page, in memory or on Postgres; the gate test calls it."""

    async def serve(reader_, writer):
        await reader_.readuntil(b"\r\n\r\n")
        body = b"<html><p>Example domains are reserved for use in documentation.</p></html>"
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                     + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async def resolve(host, port_):
        return ["93.184.215.14"]  # a public address, which the dial below maps to the local page

    persistence = None
    if store == "postgres":
        from agentsdk import Persistence
        from agentsdk.config import normalise_database_url

        persistence = Persistence.postgres(normalise_database_url(os.environ["DATABASE_URL"]), create_schema=False)
    record, _ = ledger_tools()
    orchestrator = orchestrator_for("reader:r", "critic:c")
    runner = Runner({"planner": ScriptedPlanner(), "reader": ScriptedReader(), "critic": ScriptedCritic()},
                    tools=[orchestrator.tool, fetch_tool(["rivers.example"], _resolve=resolve,
                                                         _connect=lambda ip, port_: ("127.0.0.1", port)), record],
                    persistence=persistence)
    try:
        return await demonstrate(runner, orchestrator, OFFLINE_PAGE, "planner:p", live=False,
                                 say=(lambda *a: None) if quiet else print)
    finally:
        server.close()
        await server.wait_closed()


def live():
    from dotenv import load_dotenv

    from agentsdk import Persistence
    from agentsdk.config import Settings, normalise_database_url
    from agentsdk.postgres import close_pools
    from agentsdk.providers import OpenAICompatibleModelClient

    load_dotenv()
    persistence = Persistence.postgres(normalise_database_url(os.environ["DATABASE_URL"]))
    settings = Settings.from_env(load_dotfile=False)

    async def go():
        client = OpenAICompatibleModelClient(base_url=settings.base_url, api_key=settings.api_key,
                                             model=settings.default_model)
        try:
            record, _ = ledger_tools()
            model = f"m:{settings.default_model}"
            orchestrator = orchestrator_for(model, model, live=True)
            runner = Runner({"m": client}, tools=[orchestrator.tool, fetch_tool([urlsplit(LIVE_PAGE).hostname]), record],
                            persistence=persistence)
            checks, runs = await demonstrate(runner, orchestrator, LIVE_PAGE, model, live=True, say=print)
            print(f"\nrun ids (to remove the demo's rows, children included by parent): {' '.join(runs)}")
            return checks
        finally:
            await client.aclose()

    try:
        return asyncio.run(go())
    finally:
        close_pools()


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="run in memory with a local page and scripted agents")
    checks = asyncio.run(offline())[0] if parser.parse_args().offline else live()
    for label, passed in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {label}")
    failed = [label for label, passed in checks if not passed]
    if failed:
        raise SystemExit(f"failed: {failed}")
