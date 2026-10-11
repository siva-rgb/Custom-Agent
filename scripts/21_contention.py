"""21 - Contention: eight agents, four pages, one fetch each, one claim per statement.

Phase 3's exit signal (P3-D3, FR-96). An orchestrated plan runs eight readers at once.
Each fetches two of four overlapping pages and records claims about them, several of
them the same statement in different case and spacing. Then an editor corrects one
claim, and a second tenant reads two of the same pages. Checked against the store:

  * each page fetched once per cache scope, however many agents asked for it: the
    others waited on the claim-lock and were served the holder's version (FR-95)
  * each statement stored as one claim, with every reader that recorded it as a
    contributor and every page they cited as a source (FR-92, FR-93)
  * the corrected claim's supersede history readable, oldest first (FR-94)
  * no source served to the second tenant: it fetched the pages itself (NFR-25)
  * every claim at least as tainted as the pages it cites (P3-D11)

Run it
  python scripts/21_contention.py            # live: gateway + PostgreSQL (DATABASE_URL in .env)
  python scripts/21_contention.py --offline  # in memory, local pages and scripted agents
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
from decimal import Decimal
from urllib.parse import urlsplit

from agentsdk import (
    AgentSpec,
    BudgetPolicy,
    ContextPolicy,
    Message,
    Orchestrator,
    Role,
    RunConfig,
    RunStatus,
    Runner,
    SchedulerLimits,
    SubagentPool,
)
from agentsdk.builtin_tools import fetch_tool
from agentsdk.events import EventType
from agentsdk.ledger import claim_key, ledger_tools
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.postgres import RunScope
from agentsdk.primitives import ToolCall, TrustZone
from agentsdk.subagents import Briefing

TENANT, PROJECT, OTHER_TENANT = "example-tenant", "examples", "example-other-tenant"
READERS = 8
TOOLS = ("fetch_url", "record_claim", "supersede_claim")


class Corpus:
    """Four pages and what each says; a statement about all of them; a wrong claim about
    the first page and its correction."""

    def __init__(self, facts: dict[str, str], shared: str, wrong: str, right: str) -> None:
        self.pages, self.facts = list(facts), facts
        self.shared, self.wrong, self.right = shared, wrong, right


RIVERS = Corpus(
    {"http://rivers.example/rhine": "The Rhine flows into the North Sea.",
     "http://rivers.example/danube": "The Danube flows into the Black Sea.",
     "http://rivers.example/elbe": "The Elbe flows into the North Sea.",
     "http://rivers.example/oder": "The Oder flows into the Baltic Sea."},
    shared="Four major rivers drain central Europe.",
    wrong="The Rhine flows into the Black Sea.", right="The Rhine flows into the North Sea, not the Black Sea.")
DOMAINS = Corpus(
    {"https://example.com/": "Example.com is for use in documentation examples.",
     "https://www.iana.org/help/example-domains": "IANA maintains the example domains.",
     "https://www.iana.org/domains/reserved": "IANA lists the reserved domain names.",
     "https://www.iana.org/domains/root": "IANA manages the DNS root zone."},
    shared="IANA publishes pages about reserved and example domains.",
    wrong="Example.com is a commercial website.", right="Example.com is for documentation examples, not a commercial website.")


def _spoken(statement: str, n: int) -> str:
    """The same statement as different readers write it."""
    return [statement, statement.upper(), "  " + statement.replace(" ", "  "), statement.lower()][n % 4]


def pages_of(n: int, corpus: Corpus) -> list[str]:
    return [corpus.pages[n % 4], corpus.pages[(n + 1) % 4]]


def reader_steps(n: int, corpus: Corpus) -> list[list[list]]:
    """Reader n's turns: fetch pages n and n+1 (of four) together, then record what each
    says and the shared statement citing both; reader 0 also records a wrong claim, for
    the editor. Two turns of tool calls fit an orchestrated child's four."""
    pages = pages_of(n, corpus)
    records = [["record_claim", {"statement": _spoken(corpus.facts[page], n), "supporting": [page]}] for page in pages]
    records.append(["record_claim", {"statement": _spoken(corpus.shared, n), "supporting": pages}])
    if n == 0:
        records.append(["record_claim", {"statement": corpus.wrong, "supporting": [corpus.pages[0]]}])
    return [[["fetch_url", {"url": page}] for page in pages], records]


def described(turns: list[list[list]]) -> str:
    """Turns as a live agent is told them."""
    lines = []
    for i, calls in enumerate(turns, 1):
        made = "; ".join(f"{name} with exactly these arguments: {json.dumps(args)}" for name, args in calls)
        lines.append(f"Turn {i}: in one response, call {made}")
    return ("Do exactly these turns, making every call of a turn in the same response, then answer 'done'.\n"
            + "\n".join(lines))


def briefed(turns: list[list[list]], live: bool) -> str:
    return described(turns) if live else json.dumps(turns)


class ScriptedReader:
    """Offline: makes the calls its briefing lists, one a turn, after a random delay."""

    def __init__(self, delay):
        self.delay = delay

    async def send(self, request):
        await self.delay()
        text = request.messages[0].content
        turns = json.loads(text[text.index("["):]) if "[" in text else []
        done = sum(1 for m in request.messages if m.tool_results)
        if done < len(turns):
            calls = tuple(ToolCall(id=f"c{done}-{i}", name=name, arguments=arguments)
                          for i, (name, arguments) in enumerate(turns[done]))
            return ModelResponse(message=Message(role=Role.ASSISTANT, tool_calls=calls),
                                 stop_reason=StopReason.TOOL_CALLS, usage=Usage(20, 5, 25))
        return ModelResponse(message=Message(role=Role.ASSISTANT, content="done"),
                             stop_reason=StopReason.END_TURN, usage=Usage(20, 5, 25))


class ScriptedPlanner:
    """Offline: submits the plan its task holds, then answers."""

    async def send(self, request):
        if not any(m.tool_results for m in request.messages):
            text = request.messages[0].content
            plan = json.loads(text[text.index("{"):])
            return ModelResponse(message=Message(role=Role.ASSISTANT, tool_calls=(
                ToolCall(id="plan", name="run_plan", arguments=plan),)), stop_reason=StopReason.TOOL_CALLS,
                usage=Usage(20, 5, 25))
        return ModelResponse(message=Message(role=Role.ASSISTANT, content="The readers are done."),
                             stop_reason=StopReason.END_TURN, usage=Usage(20, 5, 25))


# Live, the planner repeats an eight-node plan in one response: room for it.
LIVE_OUTPUT_TOKENS = 8000


def orchestrator_for(reader: str, live: bool = False) -> Orchestrator:
    roles = {"reader": AgentSpec(id="reader", instructions="You are a careful researcher.", tool_profile=TOOLS,
                                 preferred_model=reader, max_output_tokens=LIVE_OUTPUT_TOKENS if live else None)}
    return Orchestrator(roles=roles, policy=BudgetPolicy(run_ceiling_tokens=2_000_000),
                        limits=SchedulerLimits(max_concurrent_subagents=READERS),
                        context_policy=ContextPolicy(compact_at=None))


def registry():
    from agentsdk.registry import ModelCapabilities, ModelEntry, ModelPricing, ModelRegistry

    price = ModelPricing(input=Decimal("0.000001"), output=Decimal("0.000001"))
    return ModelRegistry([ModelEntry(provider="scripted", model_id=m, model_version="1", adapter_version="scripted",
                                     capabilities=ModelCapabilities(max_context_tokens=200_000, pricing=price))
                          for m in ("planner", "reader")])


async def contend(runner, orchestrator, corpus: Corpus, *, planner: str, reader: str, live: bool,
                  rng: random.Random, say=print, tenants: tuple[str, str] = (TENANT, OTHER_TENANT),
                  project: str = PROJECT):
    """Run the plan, the correction and the second tenant; check the store. Returns the
    checks and every run id it made."""
    order = list(range(READERS))
    rng.shuffle(order)  # the readers' start order
    nodes = [{"node_id": f"reader-{n}", "assigned_role": "reader", "dependencies": [],
              "objective": briefed(reader_steps(n, corpus), live)} for n in order]
    tenant, other_tenant = tenants
    lead = "Call run_plan once with exactly this document, then answer in one sentence.\n" if live else ""
    top = await runner.run(orchestrator.agent, lead + json.dumps({"nodes": nodes}),
                           orchestrator.config(tenant_id=tenant, project_id=project, model_override=planner, max_turns=6,
                                               max_output_tokens=LIVE_OUTPUT_TOKENS if live else None))
    ledger = runner.evidence_ledger(tenant, project, top.run_id)
    claims = {v.claim.claim_key: v for v in await asyncio.to_thread(ledger.active)}

    # The editor corrects the wrong claim, in the same ledger.
    wrong = claims.get(claim_key(corpus.wrong))
    # One call, citing nothing: a live model makes a fetch and a claim citing it in one
    # response, and a result of the same batch is not yet in the run's history (FR-93).
    editor_steps = [] if wrong is None else [[["supersede_claim", {
        "claim": wrong.claim.uri, "statement": corpus.right, "why": "the first page says otherwise"}]]]
    editor = await SubagentPool(runner).spawn(
        parent=RunScope(run_id=top.run_id, tenant_id=tenant, project_id=project),
        agent=AgentSpec(id="editor", tool_profile=TOOLS, instructions=(
            "You apply corrections to an evidence ledger exactly as they are given: call each tool named, "
            "with exactly the arguments given, never rewording a statement or choosing another tool."),
                        preferred_model=reader, max_output_tokens=LIVE_OUTPUT_TOKENS if live else None),
        briefing=Briefing(objective=briefed(editor_steps, live), assigned_role="editor", max_turns=6),
    )

    # A second tenant reads two of the same pages.
    other = await runner.run(
        AgentSpec(id="reader", instructions="You are a careful researcher.", tool_profile=TOOLS),
        briefed(reader_steps(0, corpus)[:1], live),
        RunConfig(tenant_id=other_tenant, project_id=project, model_override=reader, max_turns=6))

    finished = [e for e in top.events if e.event_type is EventType.PLAN_NODE_FINISHED]
    views = await asyncio.to_thread(ledger.active)
    claims = {v.claim.claim_key: v for v in views}
    shared = claims.get(claim_key(corpus.shared))
    right = claims.get(claim_key(corpus.right))
    chain = await asyncio.to_thread(ledger.history, right.claim.claim_id) if right else []
    store = runner._evidence_store()
    taint_ok = True
    for view in views:
        for vid in view.supporting + view.contradicting:
            source = await asyncio.to_thread(store.get, tenant, project, vid)
            taint_ok &= source.provenance.taint_flags <= view.provenance.taint_flags
            taint_ok &= (view.provenance.trust_zone is TrustZone.UNTRUSTED
                         or source.provenance.trust_zone is not TrustZone.UNTRUSTED)
    other_sources = [(e.event_type, e.payload) for e in other.events
                     if e.event_type in (EventType.SOURCE_SERVED, EventType.SOURCE_FETCHED)]
    waits = runner._claim_lock.waits

    say(f"plan run {top.run_id}: {top.status.value}; {len(finished)} readers finished")
    for view in views:
        say(f"  {view.claim.statement!r}: {len(view.contributions)} contributions, "
            f"{len(view.supporting)} sources, {view.provenance.trust_zone.value}")
    if chain:
        say("  corrected: " + " -> ".join(repr(c.statement) for c in chain))
    say(f"second tenant {other.run_id}: {[e.value for e, _ in other_sources]}")
    checks = [
        ("the plan, the editor and the second tenant completed",
         top.status is RunStatus.COMPLETED and editor.status is RunStatus.COMPLETED and other.status is RunStatus.COMPLETED),
        ("every page's statement is one claim, contributed by every reader that read the page",
         all(claim_key(corpus.facts[p]) in claims
             and len({c.run_id for c in claims[claim_key(corpus.facts[p])].contributions})
             == sum(p in pages_of(n, corpus) for n in range(READERS)) for p in corpus.pages)),
        ("the shared statement is one claim with all eight readers and all four pages",
         shared is not None and len({c.run_id for c in shared.contributions}) == READERS and len(shared.supporting) == 4),
        ("the corrected claim's history reads back oldest first",
         [c.statement for c in chain] == [corpus.wrong, corpus.right]),
        ("no source was served to the second tenant",
         len(other_sources) == 2 and all(kind is EventType.SOURCE_FETCHED for kind, _ in other_sources)),
        ("every claim is at least as tainted as the pages it cites", bool(views) and taint_ok),
    ]
    runs = [top.run_id, editor.run_id, other.run_id]
    return checks, runs, waits


async def offline(store: str = "memory", seed: int | None = None, quiet: bool = False,
                  tenants: tuple[str, str] = (TENANT, OTHER_TENANT), project: str = PROJECT):
    """The exit signal on local pages, in memory or on Postgres; the gate test calls it."""
    rng = random.Random(seed)
    hits: dict[str, int] = {}

    async def serve(reader_, writer):
        head = (await reader_.readuntil(b"\r\n\r\n")).decode("latin-1")
        url = "http://rivers.example" + head.split(" ", 2)[1]
        hits[url] = hits.get(url, 0) + 1
        await asyncio.sleep(0.05)  # slow enough that readers overlap on a page
        body = f"<html><p>{RIVERS.facts[url]}</p></html>".encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                     + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async def resolve(host, port_):
        return ["93.184.215.14"]  # a public address, which the dial below maps to the local pages

    async def delay():
        await asyncio.sleep(rng.random() * 0.01)

    fetch = fetch_tool(["rivers.example"], _resolve=resolve, _connect=lambda ip, port_: ("127.0.0.1", port))
    record, supersede = ledger_tools()
    persistence = None
    if store == "postgres":
        from dotenv import load_dotenv

        from agentsdk import Persistence
        from agentsdk.config import normalise_database_url

        load_dotenv()
        persistence = Persistence.postgres(normalise_database_url(os.environ["DATABASE_URL"]), create_schema=False)
    orchestrator = orchestrator_for("reader:reader")
    runner = Runner({"planner": ScriptedPlanner(), "reader": ScriptedReader(delay)},
                    tools=[orchestrator.tool, fetch, record, supersede], persistence=persistence,
                    model_registry=registry())
    try:
        checks, runs, waits = await contend(runner, orchestrator, RIVERS, planner="planner:planner",
                                            reader="reader:reader", live=False, rng=rng,
                                            say=(lambda *a: None) if quiet else print, tenants=tenants,
                                            project=project)
    finally:
        server.close()
        await server.wait_closed()
    # Tenant one's readers and editor fetch every page once; the second tenant fetches two more.
    expected = {page: 1 + (page in RIVERS.pages[:2]) for page in RIVERS.pages}
    if not quiet:
        print(f"requests per page: {[hits.get(p, 0) for p in RIVERS.pages]}; calls that waited on the claim-lock: {waits}")
    checks.append(("each page was fetched once per tenant, however many readers asked", hits == expected))
    return checks, runs, waits


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
            model = f"m:{settings.default_model}"
            orchestrator = orchestrator_for(model, live=True)
            hosts = sorted({urlsplit(p).hostname for p in DOMAINS.pages})
            runner = Runner({"m": client}, tools=[orchestrator.tool, fetch_tool(hosts), record, supersede],
                            persistence=persistence)
            checks, runs, waits = await contend(runner, orchestrator, DOMAINS, planner=model, reader=model, live=True,
                                                rng=random.Random())
            print(f"calls that waited on the claim-lock: {waits}")
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
    parser.add_argument("--offline", action="store_true", help="run in memory with local pages and scripted agents")
    checks = asyncio.run(offline())[0] if parser.parse_args().offline else live()
    for label, passed in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {label}")
    failed = [label for label, passed in checks if not passed]
    if failed:
        raise SystemExit(f"failed: {failed}")
