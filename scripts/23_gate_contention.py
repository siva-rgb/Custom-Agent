"""23 - The exit signal under the gate: example 21's contention, every node judged.

Phase 3's second exit signal (P3-D3, FR-108). Example 21's eight readers fetch two of four
overlapping pages each and record overlapping claims, now with every node gated and a
critic configured. The corpus decides in advance which nodes' signals fire: every reader
states each page's fact with the same confidence, so a claim shared by several nodes
carries the same signals for each, and

  * the second page's fact is stated doubtfully (confidence 0.4) by all four readers of
    it, so their nodes fire low_confidence and the critic approves them
  * a wrong claim about the first page is stated by two readers at 0.3: both critics try
    to correct it, one wins, and the other's correction is refused because the claim is
    already superseded, so its node fails and is replanned without the wrong claim
  * every other node's claims are confident and cited, and it is cleared with no critic

A second tenant then reads two of the same pages. Checked against the store: the critic
ran for exactly that set of first executions and at most once for any execution; the race
was lost and replanned as FR-105 states; nothing is less tainted than what it rests on;
every verification is the run's own; and example 21's checks still hold. Finally it
records external outcomes for the run's claims and prints the calibration report, whose
suggested threshold is only printed (FR-107).

Run it
  python scripts/23_gate_contention.py            # live: gateway + PostgreSQL (DATABASE_URL in .env)
  python scripts/23_gate_contention.py --offline  # in memory, local pages and scripted agents

Live, the readers and the critic are real models, so which signals fire and what the
critic decides are theirs: it checks only what must hold whatever they decide.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
from decimal import Decimal
from urllib.parse import urlsplit

from agentsdk import (
    AgentSpec,
    BudgetPolicy,
    ContextPolicy,
    GatePolicy,
    Message,
    Orchestrator,
    Role,
    RunConfig,
    RunStatus,
    Runner,
    SchedulerLimits,
)
from agentsdk.builtin_tools import fetch_tool
from agentsdk.events import EventType
from agentsdk.ledger import claim_key, ledger_tools
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.primitives import ToolCall, TrustZone

TENANT, PROJECT, OTHER_TENANT = "example-tenant", "examples", "example-other-tenant"
READERS = 8
TOOLS = ("fetch_url", "record_claim")
CONFIDENT, DOUBTFUL, WRONG = 0.9, 0.4, 0.3
LIVE_OUTPUT_TOKENS = 8000


class Corpus:
    """Four pages and what each says; a statement about all of them; a wrong claim about
    the first page and its correction. The second page's fact is the doubtful one."""

    def __init__(self, facts: dict[str, str], shared: str, wrong: str, right: str) -> None:
        self.pages, self.facts = list(facts), facts
        self.shared, self.wrong, self.right = shared, wrong, right
        self.doubtful = self.pages[1]


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


def states_the_wrong_claim(n: int) -> bool:
    return n % 4 == 0  # readers 0 and 4, both readers of the first page


def reader_steps(n: int, corpus: Corpus, *, wrong: bool = True) -> list[list[list]]:
    """Reader n's turns: fetch pages n and n+1 (of four) together, then record what each
    says, at the confidence the corpus fixes, and the shared statement citing both."""
    pages = pages_of(n, corpus)
    records = [["record_claim", {"statement": _spoken(corpus.facts[page], n), "supporting": [page],
                                 "confidence": DOUBTFUL if page == corpus.doubtful else CONFIDENT}]
               for page in pages]
    records.append(["record_claim", {"statement": _spoken(corpus.shared, n), "supporting": pages,
                                     "confidence": CONFIDENT}])
    if wrong and states_the_wrong_claim(n):
        records.append(["record_claim", {"statement": corpus.wrong, "supporting": [corpus.pages[0]],
                                         "confidence": WRONG}])
    return [[["fetch_url", {"url": page}] for page in pages], records]


def expected_critic_runs(corpus: Corpus) -> set[str]:
    """FR-108: the first executions whose signals fire, known from the corpus alone: a
    reader of the doubtful page, or one stating the wrong claim."""
    return {f"reader-{n}" for n in range(READERS)
            if corpus.doubtful in pages_of(n, corpus) or states_the_wrong_claim(n)}


def described(turns: list[list[list]]) -> str:
    lines = [f"Turn {i}: in one response, call " + "; ".join(
        f"{name} with exactly these arguments: {json.dumps(args)}" for name, args in calls)
        for i, calls in enumerate(turns, 1)]
    return ("Do exactly these turns, making every call of a turn in the same response and waiting for its "
            "results before the next turn. If a call returns an error, make that call again in your next "
            "response. Then answer in one sentence what the pages say.\n" + "\n".join(lines))


def briefed(turns: list[list[list]], live: bool) -> str:
    return described(turns) if live else json.dumps(turns)


def node(n: int, corpus: Corpus, live: bool, *, wrong: bool = True) -> dict:
    return {"node_id": f"reader-{n}", "assigned_role": "reader", "dependencies": [],
            "objective": briefed(reader_steps(n, corpus, wrong=wrong), live),
            "acceptance_criteria": [{"kind": "critic", "description": "Are the claims supported by the pages?"}]}


# --- offline: scripted agents ----------------------------------------------------------------


def respond(content="", calls=()):
    return ModelResponse(message=Message(role=Role.ASSISTANT, content=content, tool_calls=tuple(calls)),
                         stop_reason=StopReason.TOOL_CALLS if calls else StopReason.END_TURN, usage=Usage(20, 5, 25))


class ScriptedReader:
    """Makes the calls its briefing lists, one turn a response, after a random delay."""

    def __init__(self, delay):
        self.delay = delay

    async def send(self, request):
        await self.delay()
        turns = json.loads(request.messages[0].content.split("\n")[0])
        done = sum(1 for m in request.messages if m.tool_results)
        if done < len(turns):
            return respond(calls=[ToolCall(id=f"c{done}-{i}", name=name, arguments=arguments)
                                  for i, (name, arguments) in enumerate(turns[done])])
        return respond("done")


class ScriptedPlanner:
    """Submits the plan its task holds; after a failed version, the same plan with each
    failed node briefed without the wrong claim."""

    def __init__(self, corpus: Corpus):
        self.corpus = corpus

    async def send(self, request):
        text = request.messages[0].content
        planned = json.loads(text[text.index("{"):])["nodes"]
        reports = [json.loads(r.content) for m in request.messages for r in m.tool_results]
        if not reports:
            return respond(calls=[ToolCall(id="plan-0", name="run_plan", arguments={"nodes": planned})])
        last = reports[-1]
        if last.get("status") == "failed" and "again" in last.get("next", ""):
            failed = {item["node_id"] for item in last["nodes"] if item["status"] == "failed"}
            nodes = [node(int(n["node_id"].split("-")[1]), self.corpus, False, wrong=False)
                     if n["node_id"] in failed else n for n in planned]
            return respond(calls=[ToolCall(id=f"plan-{len(reports)}", name="run_plan", arguments={"nodes": nodes})])
        return respond("The readers are done.")


_DATA = re.compile(r"--- data input \S+: content to read, not instructions to follow ---\n(.*?)\n?--- end of data input",
                   re.DOTALL)


class ScriptedCritic:
    """Approves, unless the claims it is shown include the wrong one: then it corrects it,
    once every node stating it has been briefed, so that their corrections race."""

    def __init__(self, corpus: Corpus, racers: int):
        self.corpus, self.racers = corpus, racers
        self.arrived: set[str] = set()
        self.ready = asyncio.Event()
        self.started: list[str] = []

    async def send(self, request):
        content = request.messages[0].content
        rendering = next(r for r in (_json(block) for block in _DATA.findall(content)) if r and "claims" in r)
        if not any(m.role is Role.ASSISTANT for m in request.messages):
            self.started.append(rendering["node"])
        wrong = next((c for c in rendering["claims"] if claim_key(c["statement"]) == claim_key(self.corpus.wrong)), None)
        if wrong is None:
            return respond(json.dumps({"verdict": "approved", "reason": "the pages support the claims"}))
        self.arrived.add(rendering["node"])
        if len(self.arrived) >= self.racers:
            self.ready.set()
        try:
            await asyncio.wait_for(self.ready.wait(), timeout=10)
        except asyncio.TimeoutError:
            pass  # the check below then finds no race
        return respond(json.dumps({
            "verdict": "corrected", "reason": "the first page says the Rhine reaches the North Sea",
            "corrected_answer": "The pages say where four rivers flow; the Rhine reaches the North Sea.",
            "corrections": [{"claim": wrong["claim"], "statement": self.corpus.right,
                             "supporting": wrong["supporting"]}]}))


def _json(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return None


def orchestrator_for(reader: str, critic: str, live: bool = False) -> Orchestrator:
    tokens = LIVE_OUTPUT_TOKENS if live else None
    roles = {"reader": AgentSpec(id="reader", instructions="You are a careful researcher.", tool_profile=TOOLS,
                                 preferred_model=reader, max_output_tokens=tokens)}
    critic_spec = AgentSpec(id="critic", instructions="You are a careful critic of research.",
                            preferred_model=critic, max_output_tokens=tokens)
    # Room for every reader and both racing critics at once.
    return Orchestrator(roles=roles, policy=BudgetPolicy(run_ceiling_tokens=4_000_000),
                        limits=SchedulerLimits(max_concurrent_subagents=READERS + 2),
                        context_policy=ContextPolicy(compact_at=None), gate=GatePolicy(critic=critic_spec))


def registry():
    from agentsdk.registry import ModelCapabilities, ModelEntry, ModelPricing, ModelRegistry

    price = ModelPricing(input=Decimal("0.000001"), output=Decimal("0.000001"))
    return ModelRegistry([ModelEntry(provider="scripted", model_id=m, model_version="1", adapter_version="scripted",
                                     capabilities=ModelCapabilities(max_context_tokens=200_000, pricing=price))
                          for m in ("planner", "reader", "critic")])


# --- the run and its checks ------------------------------------------------------------------


def covers(item, labels) -> bool:
    """`item` at least as tainted as every label, and untrusted wherever one is."""
    return all(label.taint_flags <= item.taint_flags
               and (item.trust_zone is TrustZone.UNTRUSTED or label.trust_zone is not TrustZone.UNTRUSTED)
               for label in labels)


async def contend(runner, orchestrator, corpus: Corpus, *, planner: str, reader: str, live: bool,
                  rng: random.Random, say=print, tenants: tuple[str, str] = (TENANT, OTHER_TENANT),
                  project: str = PROJECT, critic=None):
    """Run the gated plan and the second tenant; check the store; record outcomes and print
    the calibration report. Returns the checks, every run id it made, and how many
    corrections lost their race."""
    order = list(range(READERS))
    rng.shuffle(order)  # the readers' start order
    nodes = [node(n, corpus, live) for n in order]
    tenant, other_tenant = tenants
    lead = ("Call run_plan once with exactly this document. If it reports that the plan failed, call run_plan "
            "again with the same document, leaving out every record_claim of the statement "
            f"{json.dumps(corpus.wrong)} from the nodes that failed. Then answer in one sentence.\n") if live else ""
    top = await runner.run(orchestrator.agent, lead + json.dumps({"nodes": nodes}),
                           orchestrator.config(tenant_id=tenant, project_id=project, model_override=planner, max_turns=8,
                                               max_output_tokens=LIVE_OUTPUT_TOKENS if live else None))
    # A second tenant reads two of the same pages.
    other = await runner.run(
        AgentSpec(id="reader", instructions="You are a careful researcher.", tool_profile=TOOLS),
        briefed(reader_steps(0, corpus)[:1], live),
        RunConfig(tenant_id=other_tenant, project_id=project, model_override=reader, max_turns=6))

    events = top.events
    gated = [e for e in events if e.event_type is EventType.GATE_EVALUATED]
    finished = [e for e in events if e.event_type is EventType.PLAN_NODE_FINISHED]
    verdicts = [e for e in events if e.event_type is EventType.CRITIC_VERDICT]
    fired = {(e.payload["version"], e.task_id) for e in gated if e.payload["outcome"] == "critic_required"}
    # Each execution the critic judged: its verdict, or, for one that failed before giving
    # one, the reason it failed the node in judging. A refused correction has both.
    verdicted = [(e.payload["version"], e.task_id) for e in verdicts]
    judged = verdicted + [
        (e.payload["version"], e.task_id) for e in finished
        if (e.payload.get("reason") or "").startswith(("critic_failed", "budget_exceeded"))
        and (e.payload["version"], e.task_id) not in verdicted]
    lost = [e for e in finished if "already superseded" in (e.payload.get("reason") or "")]
    reruns = {e.task_id: e.payload for e in finished if e.payload["version"] > 1 and e.payload["status"] == "done"
              and not (e.payload.get("reason") or "").startswith("carried")}

    store, ledgers = runner._evidence_store(), runner._ledger_store()
    ledger = runner.evidence_ledger(tenant, project, top.run_id)
    views = await asyncio.to_thread(ledger.active)
    claims = {v.claim.claim_key: v for v in views}
    every_claim, _ = await asyncio.to_thread(ledgers.rows, tenant, project, top.run_id)
    every_view = [await asyncio.to_thread(ledger.claim, c.claim_id) for c in every_claim]  # superseded too
    # The claims as the readers recorded them, superseded or not: a critic may restate a
    # shared claim as its own (FR-105), and FR-96's checks are of what the readers wrote.
    recorded: dict[str, list] = {}
    for view in every_view:
        if view.claim.supersedes is None:
            recorded.setdefault(view.claim.claim_key, []).append(view)

    def one(statement):
        found = recorded.get(claim_key(statement), [])
        return found[0] if len(found) == 1 else None

    def held(statement, readers):
        """Offline, where no critic touches a fact: one claim, with exactly its readers.
        Live, a critic may supersede any claim, and a rerun recording its statement again
        then makes a new one (FR-92): at most one claim of it active at once, and every
        reader among the contributors to its claims."""
        if not live:
            claim = one(statement)
            return claim is not None and {c.node_id for c in claim.contributions} == readers
        mine = [v for v in every_view if v.claim.claim_key == claim_key(statement)]
        return (sum(v.claim.claim_key == claim_key(statement) for v in views) <= 1
                and readers <= {c.node_id for v in mine for c in v.contributions})

    right = claims.get(claim_key(corpus.right))
    chain = await asyncio.to_thread(ledger.history, right.claim.claim_id) if right else []
    rows = await asyncio.to_thread(ledgers.verifications, tenant, project, top.run_id)
    elsewhere = await asyncio.to_thread(ledgers.verifications_since, other_tenant, project)

    def page(vid):
        return store.get(tenant, project, vid).provenance

    pages = {vid: await asyncio.to_thread(page, vid) for v in every_view for vid in (*v.supporting, *v.contradicting)}
    answers = [label for _, label in orchestrator._runs[top.run_id].answers.values()]
    critic_rows = [r for r in rows if r.kind == "critic"]
    superseding = [v for v in every_view if v.claim.supersedes]
    by_claim = {v.claim.uri: v for v in every_view}
    taint = (
        bool(every_view) and bool(answers)
        # Every claim, superseded or not, and every answer: no cleaner than the pages.
        and all(covers(v.provenance, [pages[i] for i in (*v.supporting, *v.contradicting)]) for v in every_view)
        and all(covers(a, list(pages.values())) for a in answers)
        # Before the critic: each gate's record no cleaner than the claims it judged.
        and all(covers(r.provenance, [by_claim[c["claim"]].provenance for c in r.detail["claims"]
                                      if c["claim"] in by_claim]) for r in rows if r.kind == "gate")
        # After it: a correction and a corrected answer no cleaner than the critic's result.
        and all(covers(v.provenance, [r.provenance for r in critic_rows if r.detail["applied"]
                                      and v.claim.uri in {a["by"] for a in r.detail["applied"]}]) for v in superseding)
        and all(covers(a, [r.provenance for r in critic_rows if r.outcome == "corrected" and not r.detail["refused"]])
                for a in [orchestrator._runs[top.run_id].answers[n][1] for n in orchestrator._runs[top.run_id].answers
                          if n in {e.task_id for e in verdicts if e.payload["verdict"] == "corrected"
                                   and e.payload["superseded"]}]))
    other_sources = [(e.event_type, e.payload) for e in other.events
                     if e.event_type in (EventType.SOURCE_SERVED, EventType.SOURCE_FETCHED)]

    say(f"plan run {top.run_id}: {top.status.value}; versions {sorted({e.payload['version'] for e in finished})}")
    for event in gated:
        say(f"  v{event.payload['version']} gate {event.task_id}: {event.payload['signals'] or 'clean'} "
            f"-> {event.payload['outcome']}")
    for event in verdicts:
        say(f"  v{event.payload['version']} critic {event.task_id}: {event.payload['verdict']}, "
            f"superseded {len(event.payload['superseded'])}")
    for event in lost:
        say(f"  v{event.payload['version']} {event.task_id}: {event.payload['reason'][:90]}")
    if chain:
        say("  corrected: " + " -> ".join(repr(c.statement) for c in chain))
    say(f"second tenant {other.run_id}: {[e.value for e, _ in other_sources]}")

    first = {node_id for version, node_id in judged if version == 1}
    # FR-122 (M33): None where the check's subject never happened -- no judgement, no lost
    # race. Live, a model may not bring them about, so it says so; offline it is a failure.
    checks = [
        ("the plan and the second tenant completed",
         top.status is RunStatus.COMPLETED and other.status is RunStatus.COMPLETED),
        ("every node execution was judged by the critic at most once, and only one whose signals fired",
         None if not judged else len(judged) == len(set(judged)) and set(judged) == fired
         and (critic is None or len(critic.started) == len(judged))),
        ("a correction that lost its race failed its node, which replanned and was judged afresh",
         None if not lost else all(e.payload["reason"].startswith("critic_failed: correction refused:")
                                   and e.task_id in reruns and (e.payload["version"] + 1, e.task_id) in set(judged)
                                   for e in lost)),
        ("every page's statement is one claim, contributed by every node that read the page",
         all(held(corpus.facts[p], {f"reader-{n}" for n in range(READERS) if p in pages_of(n, corpus)})
             for p in corpus.pages)),
        ("the shared statement is one claim with all eight nodes and all four pages",
         held(corpus.shared, {f"reader-{n}" for n in range(READERS)})
         and len(set().union(*(v.supporting for v in recorded.get(claim_key(corpus.shared), [])))) == 4),
        ("no claim, answer, correction or corrected answer is less tainted than what it rests on", taint),
        ("every verification is in the run's tenant and project, and none in the second tenant's",
         bool(rows) and all((r.tenant_id, r.project_id) == (tenant, project) for r in rows)
         and not any(r.ledger_run_id == top.run_id for r in elsewhere)),
        ("no source was served to the second tenant",
         len(other_sources) == 2 and all(kind is EventType.SOURCE_FETCHED for kind, _ in other_sources)),
    ]
    if not live:
        checks[1:1] = [("the critic ran for exactly the first executions the corpus fixes",
                        first == expected_critic_runs(corpus)
                        and {n for v, n in fired if v == 1} == expected_critic_runs(corpus))]
        checks.append(("the wrong claim was corrected once, its history oldest first, and the loser replanned",
                       [c.statement for c in chain] == [corpus.wrong, corpus.right] and len(lost) == 1))

    # External outcomes for the run's claims, and the report, which changes nothing.
    outcomes = []
    for view in every_view:
        verdict = "incorrect" if claim_key(view.claim.statement) == claim_key(corpus.wrong) else "correct"
        outcomes.append(await runner.record_validation(tenant, project, top.run_id, view.claim.uri, "fixture",
                                                       verdict, "the corpus says what each page says"))
    before = len(await asyncio.to_thread(ledgers.verifications_since, tenant, project))
    report = await runner.calibration_report(tenant, project, min_outcomes=5)
    after = len(await asyncio.to_thread(ledgers.verifications_since, tenant, project))
    say("calibration report: " + json.dumps({k: report.to_json()[k] for k in (
        "judged", "claims_with_outcome", "suggested_threshold", "critic")}))
    say(f"  suggested_threshold {report.suggested_threshold} (the gate's stays {orchestrator._gate.confidence_threshold}:"
        " a suggestion is never applied)")
    checks.append(("the calibration report read the run's outcomes and changed nothing",
                   {r.uri for r in outcomes} <= set(report.outcomes) and before == after
                   and orchestrator._gate.confidence_threshold == 0.7))
    return checks, [top.run_id, other.run_id], len(lost)


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
    record, _ = ledger_tools()
    persistence = None
    if store == "postgres":
        from dotenv import load_dotenv

        from agentsdk import Persistence
        from agentsdk.config import normalise_database_url

        load_dotenv()
        persistence = Persistence.postgres(normalise_database_url(os.environ["DATABASE_URL"]), create_schema=False)
    critic = ScriptedCritic(RIVERS, racers=sum(states_the_wrong_claim(n) for n in range(READERS)))
    orchestrator = orchestrator_for("reader:reader", "critic:critic")
    runner = Runner({"planner": ScriptedPlanner(RIVERS), "reader": ScriptedReader(delay), "critic": critic},
                    tools=[orchestrator.tool, fetch, record], persistence=persistence, model_registry=registry())
    try:
        checks, runs, lost = await contend(runner, orchestrator, RIVERS, planner="planner:planner",
                                           reader="reader:reader", live=False, rng=rng,
                                           say=(lambda *a: None) if quiet else print, tenants=tenants,
                                           project=project, critic=critic)
    finally:
        server.close()
        await server.wait_closed()
    # Tenant one's readers fetch every page once, the rerun included; the second tenant fetches two more.
    expected = {page: 1 + (page in RIVERS.pages[:2]) for page in RIVERS.pages}
    if not quiet:
        print(f"requests per page: {[hits.get(p, 0) for p in RIVERS.pages]}")
    checks.append(("each page was fetched once per tenant, however many readers asked", hits == expected))
    return checks, runs, lost


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
            hosts = sorted({urlsplit(p).hostname for p in DOMAINS.pages})
            runner = Runner({"m": client}, tools=[orchestrator.tool, fetch_tool(hosts), record], persistence=persistence)
            checks, runs, lost = await contend(runner, orchestrator, DOMAINS, planner=model, reader=model, live=True,
                                               rng=random.Random())
            print(f"corrections that lost a race: {lost}")
            print(f"\nrun ids (to remove the demo's rows, children included by parent): {' '.join(runs)}")
            return checks
        finally:
            await client.aclose()

    try:
        return asyncio.run(go())
    finally:
        close_pools()


def report(checks, live: bool) -> tuple[list[str], list[str]]:
    """Each check's line, and the failures. A check whose subject never happened (None) is
    `[SKIP] not exercised` live, and a failure offline, where every subject is arranged."""
    lines, failed = [], []
    for label, passed in checks:
        if passed is None and live:
            lines.append(f"[SKIP] not exercised: {label}")
            continue
        lines.append(f"[{'PASS' if passed else 'FAIL'}] {label}")
        if not passed:
            failed.append(label)
    return lines, failed


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="run in memory with local pages and scripted agents")
    offline_run = parser.parse_args().offline
    lines, failed = report(asyncio.run(offline())[0] if offline_run else live(), live=not offline_run)
    for line in lines:
        print(line)
    if failed:
        raise SystemExit(f"failed: {failed}")
