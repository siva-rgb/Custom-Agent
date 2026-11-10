"""44 - Delegation: the general agent hands a piece of its task to a helper through `delegate_task`.

`general_agent(..., delegate=True)` adds `delegate_task` (FR-184 to FR-186). Each call copies the
files it names from the agent's workspace into a fresh workspace for a general subagent, reserves a
quarter of what the agent has left for it, runs it one level deep, and returns its answer. Four
checks, each against the store and failing when its subject never happened (KNOWLEDGE-1afcf6b2,
DECISION-a7d6a1f5):

  1. a delegated piece is done by the child: its run names the parent, its workspace holds only the
     named file (by SHA-256), it read that file (a read call, or a command or program of its own),
     and its answer is in the parent's tool result;
  2. the child's reservation: its own manifest names it (on PostgreSQL; in memory, the lease it ran
     on), it is a quarter of what the parent had left, and the child spent within it;
  3. a child cannot delegate: a child's delegate_task call is refused as an unknown tool, and no run
     reaches depth 2;
  4. a spawn under REQUIRE_APPROVAL waits for its decision: the approved one starts its child only
     after the decision, and the refused one starts nothing.

Checks 1 and 2 use the gateway model when live; every export either run asks to make is refused, as
the comparison refuses them, so nothing waits for a person. Checks 3 and 4 are scripted in every
mode, so the child's attempt and the refused spawn always happen.

Run it
  python scripts/44_delegation.py             # live: gateway + PostgreSQL + Docker
  python scripts/44_delegation.py --offline   # in memory, scripted model, FakeProvider
  python scripts/44_delegation.py --scripted  # in memory, scripted model, Docker

Docker modes need Docker running and the image below on the host:
  docker pull python:3.12-alpine@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from decimal import Decimal

from agentsdk import Message, Role, RunStatus, Runner
from agentsdk.budget import BudgetPolicy
from agentsdk.delegation import DELEGATE_TOOL
from agentsdk.events import EventType
from agentsdk.identity import PrincipalContext
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.postgres import RunScope
from agentsdk.presets import general_agent
from agentsdk.primitives import ToolCall
from agentsdk.registry import ModelCapabilities, ModelEntry, ModelPricing, ModelRegistry, default_registry
from agentsdk.tools import ApprovalPolicy
from agentsdk.workspace import DockerProvider, FakeProvider

TENANT = "examples"
IMAGE = "python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df"
FAKE_IMAGE = "fake/workspace@sha256:" + "0" * 64
OPERATOR = PrincipalContext(agent_principal="example-44", user_principal="operator")
FRACTION = BudgetPolicy(run_ceiling_usd=Decimal("1")).delegation_fraction

FILES = {"data/a.txt": b"17\n25\n", "data/b.txt": b"8\n50\n",
         "NOTES.md": b"data/a.txt and data/b.txt hold one whole number per line.\n"}
# What check 1's subject-removed test adds to the named files.
EXTRA = ("NOTES.md", "data/b.txt")
NAMED = "data/a.txt"
SUM = "42"

PIECE = ("Use delegate_task exactly once: give the helper only the file data/a.txt, and ask it to add up the "
         "whole numbers in that file with a command and answer with the total alone. Then answer with the "
         "helper's total.")
CHILD_SUM = "Add up the whole numbers in data/a.txt"
NEST = "Use delegate_task to ask a helper to try to delegate a task of its own, then answer with what it said."
CHILD_NEST = "Try to call delegate_task yourself"
ASK = "Use delegate_task twice, in two turns: first to say hello, then to say goodbye."
CHILD_HELLO, CHILD_BYE = "Answer with the word hello", "Answer with the word goodbye"

# The scripted model's tasks, turn by turn; its answer follows the last turn.
SCRIPTS = {
    PIECE: ([[(DELEGATE_TOOL, {"objective": CHILD_SUM + " with a command and answer with the total alone.",
                               "files": [NAMED]})]], SUM),
    CHILD_SUM: ([[("run_command", {"argv": ["cat", NAMED]})]], SUM),
    NEST: ([[(DELEGATE_TOOL, {"objective": CHILD_NEST + ", then answer."})]], "the helper answered"),
    CHILD_NEST: ([[(DELEGATE_TOOL, {"objective": "anything"})]], "I could not delegate"),
    ASK: ([[(DELEGATE_TOOL, {"objective": CHILD_HELLO + "."})], [(DELEGATE_TOOL, {"objective": CHILD_BYE + "."})]],
          "asked twice"),
    CHILD_HELLO: ([], "hello"),
    CHILD_BYE: ([], "goodbye"),
}


def respond(content="", calls=()):
    return ModelResponse(message=Message(role=Role.ASSISTANT, content=content, tool_calls=tuple(calls)),
                         stop_reason=StopReason.TOOL_CALLS if calls else StopReason.END_TURN, usage=Usage(500, 50, 550))


class ScriptedModel:
    """Plays each task's turns. A child's task is its briefing, so the child scripts are found by the
    objective its parent wrote."""

    async def send(self, request):
        task = next(m.content for m in request.messages if m.role is Role.USER)
        # The parents' tasks first: a child's briefing never holds one.
        script = next((s for text, s in SCRIPTS.items() if task.startswith(text)), None)
        if script is None:
            return respond("I was given no task I know.")
        turns, answer = script
        turn = sum(1 for m in request.messages if m.role is Role.ASSISTANT)
        if turn < len(turns):
            return respond(calls=[ToolCall(id=f"t{turn}-{i}", name=name, arguments=args)
                                  for i, (name, args) in enumerate(turns[turn])])
        return respond(answer)


async def _cat(cmd):
    if len(cmd.argv) != 2 or not cmd.files.exists(cmd.argv[1]):
        cmd.err(f"cat: {cmd.argv[1:]!r}: No such file or directory\n")
        return 1
    cmd.out(cmd.files.read(cmd.argv[1]).decode())
    return 0


def provider(name: str):
    return DockerProvider() if name == "docker" else FakeProvider(commands={"cat": _cat})


def _entry(model_id: str, price: Decimal) -> ModelEntry:
    return ModelEntry(provider="example", model_id=model_id, model_version="1", adapter_version="example/1",
                      capabilities=ModelCapabilities(max_context_tokens=100_000,
                                                     pricing=ModelPricing(input=price, output=price * 10)))


def scripted_registry() -> ModelRegistry:
    return ModelRegistry([_entry("scripted", Decimal("0.0001"))])


def live_registry() -> ModelRegistry:
    """The shipped models, and the scripted one priced at nothing, since it calls no provider."""
    return ModelRegistry([*default_registry().entries(), _entry("scripted", Decimal(0))])


class RecordingRunner(Runner):
    """Keeps every run's event sink and configuration, so a child's events and lease are read in memory
    as on PostgreSQL."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.sinks: dict[str, object] = {}
        self.configs: dict[str, object] = {}
        self.reserved: dict[str, dict] = {}

    def _open(self, spec, config):
        opened = super()._open(spec, config)
        run_id = opened.scope.run_id
        self.sinks[run_id] = opened.events
        self.configs[run_id] = config
        lease = config.budget_lease
        if lease is not None:
            # As the manifest names it, at the run's start: a released lease shrinks to its spend.
            usd = lease.reservation.usd
            self.reserved[run_id] = {lease.node_id: {"usd": None if usd is None else str(usd),
                                                     "tokens": lease.reservation.tokens}}
        return opened


def _stored(sql: str, *args):
    import psycopg

    from agentsdk.config import normalise_database_url

    with psycopg.connect(normalise_database_url(os.environ["DATABASE_URL"])) as conn:
        return conn.execute(sql, args).fetchall()


async def events_of(runner: RecordingRunner, persistence, run_id: str) -> list[tuple[str, dict]]:
    if persistence is None:
        return [(e.event_type.value, e.payload) for e in runner.sinks[run_id].events()]
    rows = await asyncio.to_thread(_stored, "SELECT event_type, payload FROM run_events WHERE run_id = %s::uuid"
                                            " ORDER BY sequence_no", run_id)
    return [(kind, payload) for kind, payload in rows]


async def history_of(runner: Runner, tenant: str, project: str, run_id: str):
    sessions = runner._sessions_for(RunScope(run_id=run_id, tenant_id=tenant, project_id=project))
    return await asyncio.to_thread(sessions.history, run_id)


async def tool_results(runner, tenant, project, run_id) -> list[tuple[ToolCall, object]]:
    """Each call the run's model made, with its result, in order."""
    history = await history_of(runner, tenant, project, run_id)
    results = {r.tool_call_id: r for m in history for r in m.tool_results}
    return [(call, results.get(call.id)) for m in history for call in m.tool_calls]


async def children_of(runner: RecordingRunner, persistence, run_id: str) -> list[str]:
    """The runs whose parent is `run_id`: from the runs table, or in memory from their configurations."""
    if persistence is not None:
        rows = await asyncio.to_thread(_stored, "SELECT run_id::text FROM runs WHERE parent_run_id = %s::uuid"
                                                " ORDER BY started_at", run_id)
        return [r[0] for r in rows]
    return [r for r, config in runner.configs.items() if config.parent_run_id == run_id]


async def reservations_of(runner: RecordingRunner, persistence, run_id: str) -> dict | None:
    """The run's manifest's budget_reservations, or in memory the lease it ran on, as the manifest
    would name it."""
    if persistence is not None:
        rows = await asyncio.to_thread(_stored, "SELECT budget_reservations FROM execution_manifests"
                                                " WHERE run_id = %s::uuid", run_id)
        return rows[0][0] if rows else None
    return runner.reserved.get(run_id)


async def refuse_all(runner: Runner, tenant: str, project: str, run_id: str, refused: list) -> None:
    """Refuses every request for approval of the run and the runs beneath it, noting each tool."""
    decided: set[str] = set()
    while True:
        for view in await runner.interruptions(tenant, project, run_id=run_id):
            item = view.interruption
            if item.interruption_id not in decided:
                decided.add(item.interruption_id)
                refused.append(item.tool_name)
                await runner.decide(tenant, project, item.interruption_id, approve=False, principal=OPERATOR)
        await asyncio.sleep(0.05)


async def decide_in_order(runner: Runner, tenant: str, project: str, run_id: str, seen: list, children) -> None:
    """Approves the run's first spawn and refuses every later one, noting how many children existed
    when each was asked."""
    decided: set[str] = set()
    while True:
        for view in await runner.interruptions(tenant, project, run_id=run_id):
            item = view.interruption
            if item.interruption_id in decided:
                continue
            approve = not decided
            seen.append((item.tool_name, len(await children()), approve))
            decided.add(item.interruption_id)
            await runner.decide(tenant, project, item.interruption_id, approve=approve, principal=OPERATOR)
        await asyncio.sleep(0.05)


async def demonstrate(runner: RecordingRunner, chosen, *, model: str, registry: ModelRegistry, image: str,
                      persistence, tenant: str, project: str, ceiling: Decimal, say=print):
    checks: list = []

    def check(label: str, subject, why_absent: str, failing) -> None:
        if not subject:
            checks.append((label, None, why_absent))
            return
        clause = failing()
        checks.append((label, clause is None, clause))

    store = runner._workspace_store()

    def agent(model_: str, **fields):
        return general_agent(model=model_, image=image, budget=BudgetPolicy(run_ceiling_usd=ceiling),
                             registry=registry, files=FILES, delegate=True, max_turns=10, **fields)

    # 1 and 2. A delegated piece, and its child's reservation. A live model may export a file, which
    # the preset asks approval for, in the parent or the child; every such request is refused here, as
    # the comparison refuses them, so the run never waits for a person.
    parent = agent(model)
    handle = await runner.start(parent.spec, PIECE, parent.config(tenant, project))
    refused: list = []
    refusing = asyncio.ensure_future(refuse_all(runner, tenant, project, handle.run_id, refused))
    try:
        solved = await asyncio.wait_for(handle.result(), 600)
    finally:
        refusing.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await refusing
    if refused:
        say(f"refused {len(refused)} request(s) for approval: {refused}")
    said = await events_of(runner, persistence, solved.run_id)
    calls = await tool_results(runner, tenant, project, solved.run_id)
    delegated = [(c, r) for c, r in calls if c.name == DELEGATE_TOOL and r is not None and not r.is_error]
    children = await children_of(runner, persistence, solved.run_id)
    say(f"piece run {solved.run_id}: {solved.status.value} {solved.error or ''} answer={solved.output!r};"
        f" children {children}")
    child = children[0] if children else None
    rows = await asyncio.to_thread(store.workspaces, tenant, project, child) if child else []
    commands = await asyncio.to_thread(store.commands, tenant, project, child) if child else []
    reports = [json.loads(r.content) for _, r in delegated]
    # The child read the file: a read_workspace_file of it, or a command that names it or runs a file the
    # child wrote whose text names it (the child's workspace is gone when it ends, so its own calls are
    # the witness). Live, M52 saw both: a program reading the file, and a read call followed by a
    # program holding the numbers.
    child_calls = [(c, r) for c, r in (await tool_results(runner, tenant, project, child) if child else [])
                   if r is not None and not r.is_error]
    written = {c.arguments.get("path"): str(c.arguments.get("content") or "")
               for c, _ in child_calls if c.name == "write_workspace_file"}
    programs = {path for path, text in written.items() if NAMED in text}
    read_calls = [c for c, _ in child_calls if c.name == "read_workspace_file" and c.arguments.get("path") == NAMED]

    def reads(argv) -> bool:
        return NAMED in " ".join(argv) or any(a in programs or a.lstrip("./") in programs for a in argv[1:])

    check("a delegated piece was done by the child with only the named file", child and delegated,
          "the run delegated nothing that ran",
          lambda: (None if solved.status is RunStatus.COMPLETED else f"the parent ended {solved.status.value}")
          or (None if [r.limits.get("files") for r in rows] == [{NAMED: hashlib.sha256(FILES[NAMED]).hexdigest()}]
              else f"the child's workspace recorded the files {[r.limits.get('files') for r in rows]}")
          or (None if read_calls or any(c.exit_code == 0 and reads(list(c.argv)) for c in commands)
              else f"the child never read {NAMED}: commands {[c.argv for c in commands]}")
          or (None if reports and reports[0]["run_id"] == child and SUM in (reports[0]["answer"] or "")
              else f"the parent's tool result was {reports[:1]}, not the child's total {SUM}"))

    reserved = await reservations_of(runner, persistence, child) if child else None
    first_call = next((i for i, (k, p) in enumerate(said) if k == EventType.TOOL_CALLED.value
                       and p.get("name") == DELEGATE_TOOL), len(said))
    before = sum((Decimal(p["cost_usd"]) for k, p in said[:first_call]
                  if k == EventType.MODEL_CALLED.value and p.get("cost_usd")), Decimal(0))
    expected = ((ceiling - before) * FRACTION).normalize()
    child_said = await events_of(runner, persistence, child) if child else []
    child_costs = [Decimal(p["cost_usd"]) for k, p in child_said if k == EventType.MODEL_CALLED.value
                   and p.get("cost_usd")]
    names = list(reserved or {})
    held = (reserved or {}).get(names[0]) or {} if names else {}
    say(f"child {child}: reservation {reserved}; expected {expected} USD; spent {sum(child_costs)}")
    check("the child ran on its own reservation, a quarter of what the parent had left, and spent within it",
          child and reserved is not None, "no child, or no reservation recorded for it",
          lambda: (None if len(names) == 1 and names[0].startswith("delegated:")
                   else f"the child's reservations are {names}, not one delegated reservation of its own")
          or (None if held.get("usd") is not None and Decimal(held["usd"]).normalize() == expected
              else f"it reserved {held.get('usd')} USD, not {expected}")
          or (None if sum(child_costs) <= Decimal(held["usd"]) + (max(child_costs) if child_costs else 0)
              else f"the child spent {sum(child_costs)} against {held['usd']}"))

    # 3. A child cannot delegate (scripted in every mode).
    nester = agent("s:scripted")
    nested = await runner.run(nester.spec, NEST, nester.config(tenant, project))
    kids = await children_of(runner, persistence, nested.run_id)
    attempts = [(c, r) for kid in kids for c, r in await tool_results(runner, tenant, project, kid)
                if c.name == DELEGATE_TOOL]
    grandchildren = [g for kid in kids for g in await children_of(runner, persistence, kid)]
    say(f"nest run {nested.run_id}: children {kids}; their delegate_task results "
        f"{[(r.is_error, str(r.content)[:60]) for _, r in attempts if r is not None]}; grandchildren {grandchildren}")
    check("a child's delegate_task was refused (no such tool), and no run reached depth 2", attempts,
          "no child tried to delegate",
          # DECISION-94bfee7c: keyed on the result's provenance source, never on its text.
          lambda: (None if all(r is not None and r.is_error
                               and r.provenance.source_uri_or_hash == "urn:agentsdk:tool-error:ToolNotFound"
                               for _, r in attempts)
                   else f"a child's delegate_task was not refused as unknown: {attempts}")
          or (None if not grandchildren else f"runs reached depth 2: {grandchildren}"))

    # 4. A spawn under REQUIRE_APPROVAL waits (scripted in every mode).
    asker = agent("s:scripted", delegate_approval=ApprovalPolicy.REQUIRE_APPROVAL)
    handle = await runner.start(asker.spec, ASK, asker.config(tenant, project))
    seen: list = []
    deciding = asyncio.ensure_future(decide_in_order(runner, tenant, project, handle.run_id, seen,
                                                     lambda: children_of(runner, persistence, handle.run_id)))
    try:
        asked = await asyncio.wait_for(handle.result(), 300)
    finally:
        deciding.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await deciding
    views = await runner.interruptions(tenant, project, run_id=asked.run_id, pending_only=False)
    spawned = await children_of(runner, persistence, asked.run_id)
    asked_calls = await tool_results(runner, tenant, project, asked.run_id)
    say(f"approval run {asked.run_id}: {asked.status.value}; asked {seen}; children {spawned}")
    spawns = [v for v in views if v.interruption.tool_name == DELEGATE_TOOL]
    check("a spawn under REQUIRE_APPROVAL waited for its decision, and the refused one started nothing", spawns,
          "no spawn asked for approval",
          lambda: (None if asked.status is RunStatus.COMPLETED else f"the run ended {asked.status.value}")
          or (None if len(spawns) == 2 and all(v.decision is not None and v.decision.principal == OPERATOR
                                               for v in spawns)
              else f"{len(spawns)} spawns asked, decisions {[v.decision for v in spawns]}")
          or (None if [n for _, n, _ in seen] == [0, 1] else f"children existed before a decision: {seen}")
          or (None if len(spawned) == 1 else f"{len(spawned)} children, not the one approved")
          or (None if [r.is_error for c, r in asked_calls if c.name == DELEGATE_TOOL] == [False, True]
              else "the refused spawn was not a tool error"))
    return checks, [solved.run_id, nested.run_id, asked.run_id]


def report(checks) -> list[str]:
    failed = []
    for label, passed, why in checks:
        if passed is None:
            print(f"[FAIL] not exercised: {label} ({why})")
        else:
            print(f"[{'PASS' if passed else 'FAIL'}] {label}" + ("" if passed else f" -- {why}"))
        if not passed:
            failed.append(label)
    return failed


def docker_ready() -> str | None:
    if shutil.which("docker") is None:
        return "docker is not installed"
    found = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, timeout=60)
    return None if found.returncode == 0 else f"the image {IMAGE} is not on this host"


def _runner(models: dict, chosen, persistence, registry: ModelRegistry) -> RecordingRunner:
    return RecordingRunner(models, model_registry=registry, workspace_provider=chosen, persistence=persistence,
                           max_concurrent_workspaces=8)


async def offline(store: str = "memory", provider_name: str = "fake", *, tenant: str = TENANT,
                  project: str | None = None, quiet: bool = False):
    """Every check with the scripted model; the tests call it on both stores."""
    persistence = None
    if store == "postgres":
        from dotenv import load_dotenv

        from agentsdk import Persistence
        from agentsdk.config import normalise_database_url

        load_dotenv()
        persistence = Persistence.postgres(normalise_database_url(os.environ["DATABASE_URL"]), create_schema=False)
    chosen = provider(provider_name)
    project = project or f"delegation-{uuid.uuid4().hex[:8]}"
    image = FAKE_IMAGE if provider_name == "fake" else IMAGE
    scripted = ScriptedModel()
    runner = _runner({"m": scripted, "s": scripted}, chosen, persistence, scripted_registry())
    try:
        return await demonstrate(runner, chosen, model="m:scripted", registry=scripted_registry(), image=image,
                                 persistence=persistence, tenant=tenant, project=project, ceiling=Decimal("5"),
                                 say=(lambda *a: None) if quiet else print)
    finally:
        await runner.aclose()


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
        docker, runner = DockerProvider(), None
        try:
            runner = _runner({"m": client, "s": ScriptedModel()}, docker, persistence, live_registry())
            checks, runs = await demonstrate(
                runner, docker, model=f"m:{settings.default_model}", registry=live_registry(), image=IMAGE,
                persistence=persistence, tenant=TENANT, project=f"delegation-{uuid.uuid4().hex[:8]}",
                ceiling=Decimal("0.20"))
            print(f"\nrun ids (to remove the demo's rows, children included by parent): {' '.join(runs)}")
            return checks
        finally:
            try:
                if runner is not None:
                    await runner.aclose()
            finally:
                await client.aclose()

    try:
        return asyncio.run(go())
    finally:
        close_pools()


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="in memory, scripted model, FakeProvider")
    mode.add_argument("--scripted", action="store_true", help="in memory, scripted model, Docker")
    args = parser.parse_args()
    if not args.offline and (why := docker_ready()) is not None:
        print(f"[SKIP] {why}")
        raise SystemExit(0)
    if args.offline or args.scripted:
        checks, _ = asyncio.run(offline(provider_name="fake" if args.offline else "docker"))
    else:
        checks = live()
    failed = report(checks)
    if failed:
        raise SystemExit(f"failed: {failed}")
