"""20 - The evidence ledger: two agents record one claim, and a third corrects another.

What it shows
  * two subagents of one run read the same pages and record claims into their top-level
    run's ledger with record_claim, citing the pages they fetched (FR-93)
  * one statement, written in different case and spacing by the two, is stored once:
    one claim with both contributions and the union of the sources they cited (FR-92)
  * a claim drawn from pages on the internet is untrusted, and stays so (P3-D11)
  * a third subagent supersedes a wrong claim with supersede_claim, saying why; the
    ledger keeps the old claim and reads the chain back oldest first (FR-94)
  * ClaimRecorded and ClaimSuperseded events say what happened

In the live run the agents are the gateway's model, the pages are example.com and the
IANA page about example domains, and the ledger is stored in PostgreSQL.

Run it
  python scripts/20_ledger.py            # live: gateway + PostgreSQL (DATABASE_URL in .env)
  python scripts/20_ledger.py --offline  # in memory, local pages and scripted agents
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from agentsdk import AgentSpec, Message, Role, RunConfig, RunStatus, Runner, SubagentPool
from agentsdk.builtin_tools import fetch_tool
from agentsdk.events import EventType
from agentsdk.ledger import ledger_tools
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.postgres import RunScope
from agentsdk.primitives import ToolCall
from agentsdk.subagents import Briefing

TENANT, PROJECT = "example-tenant", "examples"
RESERVED = "Example domains are reserved for use in documentation."
WRONG = "Anyone can register example.com for their own website."
RIGHT = "Nobody can register example.com: it is reserved by IANA."



def plan(scheme: str) -> dict[str, list]:
    """Each subagent's steps. Offline the pages are served over plain HTTP locally."""
    home, about = f"{scheme}://example.com/", f"{scheme}://www.iana.org/help/example-domains"
    return {
        "reader-a": [("fetch", home), ("record", RESERVED, [home])],
        "reader-b": [("fetch", home), ("fetch", about),
                     ("record", "example domains are RESERVED for use in  documentation.", [home, about]),
                     ("record", WRONG, [home])],
        "editor": [("fetch", about), ("supersede", WRONG, RIGHT, [about], "IANA reserves example domains")],
    }

TOOLS = ("fetch_url", "record_claim", "supersede_claim")


def agent(name: str) -> AgentSpec:
    return AgentSpec(id=name, instructions=(
        "You are a careful researcher. Do exactly the steps you are given, one tool call per turn, "
        "copying statements word for word, then answer 'done'."), tool_profile=TOOLS)


def calls(steps, claims):
    """The steps as the tool calls they are, a claim named by its statement looked up."""
    out = []
    for step in steps:
        if step[0] == "fetch":
            out.append(["fetch_url", {"url": step[1]}])
        elif step[0] == "record":
            out.append(["record_claim", {"statement": step[1], "supporting": step[2]}])
        else:
            out.append(["supersede_claim", {"claim": claims[step[1]], "statement": step[2],
                                            "supporting": step[3], "why": step[4]}])
    return out


def prose(steps, claims):
    """The same steps, as a live agent is told them."""
    lines = []
    for n, (name, arguments) in enumerate(calls(steps, claims), 1):
        lines.append(f"{n}. Call {name} with exactly these arguments: {json.dumps(arguments)}")
    return "Steps:\n" + "\n".join(lines)


class Scripted:
    """Offline: makes the calls in its briefing, one a turn, then answers."""

    async def send(self, request):
        text = request.messages[0].content
        plan = json.loads(text[text.index("["):]) if "[" in text else []
        done = sum(len(m.tool_results) for m in request.messages)
        if done < len(plan):
            name, arguments = plan[done]
            return ModelResponse(message=Message(role=Role.ASSISTANT, tool_calls=(
                ToolCall(id=f"c{done}", name=name, arguments=arguments),)),
                stop_reason=StopReason.TOOL_CALLS, usage=Usage(20, 5, 25))
        return ModelResponse(message=Message(role=Role.ASSISTANT, content="done"),
                             stop_reason=StopReason.END_TURN, usage=Usage(20, 5, 25))


async def demonstrate(runner, model, offline):
    top = await runner.run(AgentSpec(id="lead", instructions="Reply with the word ready."), "Begin.",
                           RunConfig(tenant_id=TENANT, project_id=PROJECT, model_override=model))
    print(f"top-level run {top.run_id}: {top.status.value}")
    pool = SubagentPool(runner)
    parent = RunScope(run_id=top.run_id, tenant_id=TENANT, project_id=PROJECT)
    ledger = runner.evidence_ledger(TENANT, PROJECT, top.run_id)
    claims, children = {}, []
    steps_of = plan("http" if offline else "https")
    for name in ("reader-a", "reader-b", "editor"):
        steps = calls(steps_of[name], claims)
        objective = json.dumps(steps) if offline else prose(steps_of[name], claims)
        result = await pool.spawn(parent=parent, agent=agent(name),
                                  briefing=Briefing(objective=objective, assigned_role=name, max_turns=10))
        children.append(result.run_id)
        print(f"{name} (run {result.run_id}): {result.status.value}")
        claims = {v.claim.statement: v.claim.uri for v in await asyncio.to_thread(ledger.active)}

    active = await asyncio.to_thread(ledger.active)
    print("\nthe ledger's active claims:")
    for view in active:
        who = ", ".join(sorted({c.agent_id for c in view.contributions}))
        print(f"  {view.claim.uri}\n    {view.claim.statement!r}\n    contributions: {len(view.contributions)} "
              f"({who}); sources: {len(view.supporting)}; labels: {view.provenance.trust_zone.value}, "
              f"{sorted(f.value for f in view.provenance.taint_flags)}")
    by_key = {view.claim.claim_key: view for view in active}
    from agentsdk.ledger import claim_key

    reserved = by_key.get(claim_key(RESERVED))
    right = by_key.get(claim_key(RIGHT))
    chain = await asyncio.to_thread(ledger.history, right.claim.claim_id) if right else []
    if chain:
        print("\nthe corrected claim's history, oldest first:")
        for claim in chain:
            print(f"  {claim.uri}: {claim.statement!r}")
        print(f"  why: {right.contributions[0].why!r}")
    return [
        ("every run completed", top.status is RunStatus.COMPLETED),
        ("one claim for the shared statement, with both readers' contributions",
         reserved is not None and {c.agent_id for c in reserved.contributions} == {"reader-a", "reader-b"}),
        ("the shared claim cites both pages", reserved is not None and len(reserved.supporting) == 2),
        ("claims drawn from the internet are untrusted",
         bool(active) and all(v.provenance.trust_zone.value == "untrusted" for v in active)),
        ("the wrong claim was superseded, and the chain reads back oldest first",
         [c.statement for c in chain] == [WRONG, RIGHT]),
        ("the wrong claim is no longer active", claim_key(WRONG) not in by_key),
    ], [top.run_id, *children]


PAGES = {
    "/": b"<html><title>Example Domain</title><p>This domain is for use in documentation examples.</p></html>",
    "/help/example-domains": b"<html><title>Example Domains</title><p>IANA reserves example.com.</p></html>",
}


async def offline():
    hits = []

    async def serve(reader, writer):
        head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
        hits.append(head.split(" ", 2)[1])
        body = PAGES[head.split(" ", 2)[1]]
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                     + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async def resolve(host, port_):
        return ["93.184.215.14"]  # a public address, which the dial below maps to the local pages

    fetch = fetch_tool(["example.com", "www.iana.org"], _resolve=resolve,
                       _connect=lambda ip, port_: ("127.0.0.1", port))
    record, supersede = ledger_tools()
    runner = Runner({"m": Scripted()}, tools=[fetch, record, supersede])
    try:
        checks, _ = await demonstrate(runner, "m:scripted", offline=True)
    finally:
        server.close()
        await server.wait_closed()
    print(f"\nrequests the pages had: {len(hits)} (each page once: the rest were served from the cache)")
    return checks + [("each page was fetched once", sorted(hits) == sorted(PAGES))]


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
        client = OpenAICompatibleModelClient(base_url=settings.base_url, api_key=settings.api_key, model=settings.default_model)
        try:
            record, supersede = ledger_tools()
            runner = Runner({"m": client}, tools=[fetch_tool(["example.com", "www.iana.org"]), record, supersede],
                            persistence=persistence)
            checks, run_ids = await demonstrate(runner, f"m:{settings.default_model}", offline=False)
            print(f"\nrun ids (to remove the demo's rows): {' '.join(run_ids)}")
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
    parser.add_argument("--offline", action="store_true", help="run in memory with local pages and scripted agents")
    checks = asyncio.run(offline()) if parser.parse_args().offline else live()
    for label, passed in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {label}")
    failed = [label for label, passed in checks if not passed]
    if failed:
        raise SystemExit(f"failed: {failed}")
