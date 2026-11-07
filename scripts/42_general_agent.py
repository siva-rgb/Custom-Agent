"""42 - The general agent: a preset that works in its sandbox, inside a budget, asking before a file leaves.

`general_agent(...)` gives a ready agent: the shipped instructions, the workspace tools, a workspace
made from a pinned image -- here one that starts with the task's files -- and a run configuration
that always carries a budget, 40 turns and a context policy (FR-174 to FR-176). Four runs, each
checked against the store and failing when its subject never happened (KNOWLEDGE-1afcf6b2,
DECISION-a7d6a1f5):

  1. a task from the workspace's files is solved by code the run wrote and ran: its commands are
     recorded, one of them reads those files, and the answer is read back from the kept workspace;
  2. a runaway is stopped by its budget: a ceiling below one call's cost ends it budget_exceeded
     after the one call that crossed it;
  3. an export waits for approval: its interruption and decision are recorded, and no artifact
     exists before the decision; the commands of the same run waited for nothing;
  4. a plan whose role is the general subagent completes, each child in its own workspace, briefed
     with the role's `role_turns` (FR-177).

Run it
  python scripts/42_general_agent.py             # live: gateway + PostgreSQL + Docker
  python scripts/42_general_agent.py --offline   # in memory, scripted model, FakeProvider
  python scripts/42_general_agent.py --scripted  # in memory, scripted model, Docker

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

from agentsdk import Message, Role, RunConfig, RunStatus, Runner
from agentsdk.budget import BudgetPolicy
from agentsdk.context_policy import ContextPolicy
from agentsdk.events import EventType
from agentsdk.identity import PrincipalContext
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.orchestrator import PLAN_TOOL, Orchestrator
from agentsdk.postgres import RunScope
from agentsdk.presets import SUBAGENT_TURNS, general_agent, general_subagent
from agentsdk.primitives import ToolCall
from agentsdk.registry import ModelCapabilities, ModelEntry, ModelPricing, ModelRegistry, default_registry
from agentsdk.scheduler import SchedulerLimits
from agentsdk.tools import ApprovalPolicy
from agentsdk.workspace import DockerProvider, FakeProvider
from agentsdk.workspace.tools import one_file

TENANT = "examples"
IMAGE = "python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df"
FAKE_IMAGE = "fake/workspace@sha256:" + "0" * 64
OPERATOR = PrincipalContext(agent_principal="example-42", user_principal="operator")

NUMBERS = b"17\n25\n8\n50\n"
TOTAL = "100"
FILES = {"data/numbers.txt": NUMBERS,
         "NOTES.md": b"data/numbers.txt holds one whole number per line.\n"}

SOLVE = ("Add up the numbers in data/numbers.txt. Write a Python program, solve.py, that reads that file and "
         "writes the total to result.txt; run it with python, check result.txt, and answer with the total.")
RUNAWAY = ("Run the command `echo tick` twenty times, one command per turn, and answer only after the "
           "twentieth.")
EXPORT = ("Write the text hello to greeting.txt with write_workspace_file, then export greeting.txt with "
          "export_workspace_file, and answer with the artifact id it returns.")
CHILD = "Write the word {word} into {word}.txt with write_workspace_file, read the file back, and answer with its content."
WORDS = ("alpha", "beta")

# The scripted model's tasks, turn by turn; its answer follows the last turn.
SOLVER = ("import pathlib\n"
          "total = sum(int(line) for line in pathlib.Path('data/numbers.txt').read_text().split())\n"
          "pathlib.Path('result.txt').write_text(str(total))\n"
          "print(total)\n")
SCRIPTS = {
    SOLVE: ([[("list_workspace", {})],
             [("write_workspace_file", {"path": "solve.py", "content": SOLVER})],
             [("run_command", {"argv": ["python", "solve.py"]})],
             [("read_workspace_file", {"path": "result.txt"})]], TOTAL),
    RUNAWAY: ([[("run_command", {"argv": ["echo", "tick"]})]] * 20, "done"),
    EXPORT: ([[("write_workspace_file", {"path": "greeting.txt", "content": "hello"})],
              [("export_workspace_file", {"path": "greeting.txt"})]], "exported"),
    **{CHILD.format(word=w): ([[("write_workspace_file", {"path": f"{w}.txt", "content": w})],
                               [("read_workspace_file", {"path": f"{w}.txt"})]], w) for w in WORDS},
}


def respond(content="", calls=()):
    return ModelResponse(message=Message(role=Role.ASSISTANT, content=content, tool_calls=tuple(calls)),
                         stop_reason=StopReason.TOOL_CALLS if calls else StopReason.END_TURN, usage=Usage(500, 50, 550))


class ScriptedModel:
    """Plays each task's turns: the calls the shipped instructions ask for, then the answer."""

    async def send(self, request):
        task = next(m.content for m in request.messages if m.role is Role.USER)
        script = next((s for text, s in SCRIPTS.items() if text in task), None)
        if script is None:
            return respond("I was given no task I know.")
        turns, answer = script
        turn = sum(1 for m in request.messages if m.role is Role.ASSISTANT)
        if turn < len(turns):
            return respond(calls=[ToolCall(id=f"t{turn}-{i}", name=name, arguments=args)
                                  for i, (name, args) in enumerate(turns[turn])])
        return respond(answer)


class ScriptedPlanner:
    """Submits one plan: a node per word, each run by the general subagent."""

    async def send(self, request):
        if not any(m.role is Role.TOOL for m in request.messages):
            nodes = [{"node_id": w, "objective": CHILD.format(word=w), "assigned_role": "general",
                      "dependencies": []} for w in WORDS]
            return respond(calls=[ToolCall(id="plan", name=PLAN_TOOL, arguments={"nodes": nodes})])
        return respond("the plan is done")


async def _python(cmd):
    """The fake's python: runs the program the run wrote only as far as this example needs -- one that
    reads data/numbers.txt and writes the total -- and fails for anything else, as a real one would."""
    if len(cmd.argv) != 2 or not cmd.files.exists(cmd.argv[1]):
        cmd.err(f"python: can't open file {cmd.argv[1:]!r}\n")
        return 2
    program = cmd.files.read(cmd.argv[1]).decode()
    if "data/numbers.txt" not in program or "result.txt" not in program:
        cmd.err("the fake runs only a program that reads data/numbers.txt and writes result.txt\n")
        return 1
    total = sum(int(line) for line in cmd.files.read("data/numbers.txt").split())
    cmd.files.write("result.txt", str(total).encode())
    cmd.out(f"{total}\n")
    return 0


async def _echo(cmd):
    cmd.out(" ".join(cmd.argv[1:]) + "\n")
    return 0


def provider(name: str):
    return DockerProvider() if name == "docker" else FakeProvider(commands={"python": _python, "echo": _echo})


def scripted_registry() -> ModelRegistry:
    """The scripted models' entries: a known window, and prices so a USD ceiling applies (FR-69)."""
    pricing = ModelPricing(input=Decimal("0.0001"), output=Decimal("0.001"))
    return ModelRegistry([ModelEntry(provider="example", model_id=m, model_version="1", adapter_version="example/1",
                                     capabilities=ModelCapabilities(max_context_tokens=100_000, pricing=pricing))
                          for m in ("scripted", "p")])


def live_registry() -> ModelRegistry:
    """The shipped models, which the shipped price table prices, and the scripted planner, priced at
    nothing because it calls no provider: a USD ceiling refuses a model with no price (FR-69)."""
    planner = ModelEntry(provider="example", model_id="p", model_version="1", adapter_version="example/1",
                         capabilities=ModelCapabilities(max_context_tokens=100_000,
                                                        pricing=ModelPricing(input=Decimal(0), output=Decimal(0))))
    return ModelRegistry([*default_registry().entries(), planner])


class RecordingRunner(Runner):
    """Keeps every run's event sink, so a child's events are read in memory as on PostgreSQL."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.sinks: dict[str, object] = {}

    def _open(self, spec, config):
        opened = super()._open(spec, config)
        self.sinks[opened.scope.run_id] = opened.events
        return opened


def _stored(sql: str, *args):
    import psycopg

    from agentsdk.config import normalise_database_url

    with psycopg.connect(normalise_database_url(os.environ["DATABASE_URL"])) as conn:
        return conn.execute(sql, args).fetchall()


async def events_of(runner: RecordingRunner, persistence, run_id: str) -> list[tuple[str, dict]]:
    """A run's (event type, payload) in order: from PostgreSQL, or in memory from its sink."""
    if persistence is None:
        return [(e.event_type.value, e.payload) for e in runner.sinks[run_id].events()]
    rows = await asyncio.to_thread(_stored, "SELECT event_type, payload FROM run_events WHERE run_id = %s::uuid"
                                            " ORDER BY sequence_no", run_id)
    return [(kind, payload) for kind, payload in rows]


async def calls_of(runner: Runner, tenant: str, project: str, run_id: str) -> list[ToolCall]:
    """Every tool call the run's model made, from its history on either store."""
    sessions = runner._sessions_for(RunScope(run_id=run_id, tenant_id=tenant, project_id=project))
    history = await asyncio.to_thread(sessions.history, run_id)
    return [call for m in history for call in m.tool_calls]


async def kept_file(chosen, workspace_id: str, path: str) -> bytes | None:
    """A file of a kept workspace, through the provider's export (FR-151); None when absent."""
    owned = await chosen.list_owned({"agentsdk.workspace": workspace_id})
    if not owned:
        return None
    try:
        return one_file(await chosen.export(owned[0], path), path)
    except Exception:  # noqa: BLE001 - absent, or not one regular file: either way not the answer
        return None


async def operator(runner: Runner, tenant: str, project: str, run_id: str, artifacts, seen: list) -> None:
    """Approves each request of the run, noting first how many artifacts the run had at that moment."""
    decided: set[str] = set()
    while True:
        for view in await runner.interruptions(tenant, project, run_id=run_id):
            item = view.interruption
            if item.interruption_id in decided:
                continue
            seen.append((item.tool_name, await artifacts()))
            decided.add(item.interruption_id)
            await runner.decide(tenant, project, item.interruption_id, approve=True, principal=OPERATOR)
        await asyncio.sleep(0.05)


async def demonstrate(runner: RecordingRunner, chosen, orchestrator: Orchestrator, *, model: str,
                      registry: ModelRegistry, image: str, persistence, tenant: str, project: str, ceilings: dict,
                      say=print):
    checks: list = []

    def check(label: str, subject, why_absent: str, failing) -> None:
        """A check whose subject never happened fails; else the first failing clause, or None."""
        if not subject:
            checks.append((label, None, why_absent))
            return
        clause = failing()
        checks.append((label, clause is None, clause))

    store = runner._workspace_store()

    # 1. A task from the workspace's files.
    solver = general_agent(model=model, image=image, budget=BudgetPolicy(run_ceiling_usd=ceilings["task"]),
                           registry=registry, files=FILES, keep=True, max_turns=12)
    solved = await runner.run(solver.spec, SOLVE, solver.config(tenant, project))
    say(f"task run {solved.run_id}: {solved.status.value} {solved.error or ''} answer={solved.output!r}")
    made = [p["workspace_id"] for k, p in await events_of(runner, persistence, solved.run_id)
            if k == EventType.WORKSPACE_CREATED.value]
    commands = [c for c in await asyncio.to_thread(store.commands, tenant, project) if c.run_id == solved.run_id]
    calls = await calls_of(runner, tenant, project, solved.run_id)
    kept = made[0] if made else None

    async def reads_the_files(argv: list[str]) -> bool:
        """A command reads the given files when it names one, or runs a file holding a path to one."""
        if any("numbers.txt" in a for a in argv):
            return True
        for name in argv[1:]:
            body = await kept_file(chosen, kept, name) if kept else None
            if body is not None and b"numbers.txt" in body:
                return True
        return False

    readers = [c for c in commands if c.exit_code == 0 and await reads_the_files(list(c.argv))]
    answer = await kept_file(chosen, kept, "result.txt") if kept else None
    typed = [c for c in calls if c.name == "write_workspace_file" and c.arguments.get("path") == "result.txt"]
    # Pre-review F2: the files were the workspace's from the start, not the run's own copies. Its
    # row names them by SHA-256; NOTES.md, which the task never needs, is still there byte for byte;
    # and the run wrote neither given file itself.
    rows = [w for w in await asyncio.to_thread(store.workspaces, tenant, project, solved.run_id)]
    given = {path: hashlib.sha256(data).hexdigest() for path, data in FILES.items()}
    notes = await kept_file(chosen, kept, "NOTES.md") if kept else None
    rewrote = [c.arguments.get("path") for c in calls
               if c.name == "write_workspace_file" and c.arguments.get("path") in FILES]
    check("a task from the workspace's files was solved by code the run wrote and ran", commands,
          "the run recorded no command",
          lambda: (None if solved.status is RunStatus.COMPLETED else f"the run ended {solved.status.value}: {solved.error}")
          or (None if [r.limits.get("files") for r in rows] == [given]
              else f"its workspace recorded the files {[r.limits.get('files') for r in rows]}")
          or (None if notes == FILES["NOTES.md"] and not rewrote
              else f"the given files were not the workspace's own: NOTES.md held {notes!r}, the run wrote {rewrote}")
          or (None if readers else f"no command that succeeded read the files: {[c.argv for c in commands]}")
          or (None if answer is not None and answer.decode().strip() == TOTAL
              else f"the kept workspace's result.txt held {answer!r}, not {TOTAL}")
          or (None if not typed else "result.txt was typed in with write_workspace_file, not computed"))
    if kept:
        await runner.release_workspace(tenant, project, kept)

    # 2. A runaway, stopped by its budget.
    ceiling = ceilings["runaway"]
    runaway = general_agent(model=model, image=image, budget=BudgetPolicy(run_ceiling_usd=ceiling), registry=registry)
    stopped = await runner.run(runaway.spec, RUNAWAY, runaway.config(tenant, project))
    said = await events_of(runner, persistence, stopped.run_id)
    costs = [Decimal(p["cost_usd"]) for k, p in said if k == EventType.MODEL_CALLED.value and p.get("cost_usd")]
    say(f"runaway run {stopped.run_id}: {stopped.status.value} {stopped.error or ''} spent {stopped.cost_usd}"
        f" over {len(costs)} call(s) against {ceiling}")
    check("a runaway was stopped by its budget after the one call that crossed it", costs,
          "the run made no priced model call",
          lambda: (None if stopped.status is RunStatus.FAILED and stopped.error == "budget_exceeded"
                   else f"it ended {stopped.status.value} ({stopped.error}), not budget_exceeded")
          or (None if len(costs) == 1 else f"{len(costs)} model calls, not the one that crossed the ceiling")
          or (None if stopped.cost_usd is not None and stopped.cost_usd >= ceiling and stopped.cost_usd == sum(costs)
              else f"its spend {stopped.cost_usd} is not its calls' {sum(costs)} at or over {ceiling}"))

    # 3. An export waits for approval; the same run's commands do not.
    exporter = general_agent(model=model, image=image, budget=BudgetPolicy(run_ceiling_usd=ceilings["task"]),
                             registry=registry, max_turns=8)
    artifacts = runner._artifacts_for(tenant, project)

    async def artifacts_of(run_id: str) -> int:
        if persistence is not None:
            return (await asyncio.to_thread(_stored, "SELECT count(*) FROM artifacts WHERE source_run = %s::uuid",
                                            run_id))[0][0]
        rows = getattr(runner._memory_artifacts, "_rows", {})
        return sum(1 for ref, _ in rows.values() if ref.source_run == run_id)

    handle = await runner.start(exporter.spec, EXPORT, exporter.config(tenant, project))
    seen: list = []
    approving = asyncio.ensure_future(operator(runner, tenant, project, handle.run_id,
                                               lambda: artifacts_of(handle.run_id), seen))
    try:
        exported = await asyncio.wait_for(handle.result(), 300)
    finally:
        approving.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await approving
    views = await runner.interruptions(tenant, project, run_id=exported.run_id, pending_only=False)
    after = await artifacts_of(exported.run_id)
    del artifacts
    say(f"export run {exported.run_id}: {exported.status.value}; asked {seen}; {after} artifact(s) after")
    exports = [v for v in views if v.interruption.tool_name == "export_workspace_file"]
    check("an export waited for approval: its interruption and decision recorded, no artifact before it", exports,
          "the run asked no export",
          lambda: (None if exported.status is RunStatus.COMPLETED else f"the run ended {exported.status.value}")
          or (None if all(v.decision is not None and v.decision.decision == "approve" and v.decision.principal == OPERATOR for v in exports)
              else "an export's decision was not recorded")
          or (None if seen and all(n == 0 for tool, n in seen if tool == "export_workspace_file")
              else f"an artifact existed before the decision: {seen}")
          or (None if after >= 1 else "no artifact after the approval")
          or (None if all(v.interruption.tool_name == "export_workspace_file" for v in views)
              else f"a command waited too: {[v.interruption.tool_name for v in views]}"))

    # 4. A plan whose role is the general subagent.
    before = set(runner.sinks)
    planned = await runner.run(orchestrator.agent, "Run the plan.", orchestrator.config(
        tenant_id=tenant, project_id=project, model_override="planner:p", max_turns=4))
    children = [r for r in runner.sinks if r not in before and r != planned.run_id]
    briefed, spaces = {}, {}
    for run_id in children:
        for kind, payload in await events_of(runner, persistence, run_id):
            if kind == EventType.RUN_STARTED.value:
                briefed[run_id] = (payload["agent_spec_id"], payload["max_turns"])
            if kind == EventType.WORKSPACE_CREATED.value:
                spaces[run_id] = payload["workspace_id"]
    finished = {e.task_id: e.payload["status"] for e in planned.events if e.event_type is EventType.PLAN_NODE_FINISHED}
    say(f"plan run {planned.run_id}: {planned.status.value}; nodes {finished}; children {briefed}")
    check("a plan whose role is the general subagent completed, each child in its own workspace with its role_turns",
          children, "the plan spawned no child",
          lambda: (None if planned.status is RunStatus.COMPLETED and finished == {w: "done" for w in WORDS}
                   else f"the plan ended {planned.status.value} with nodes {finished}")
          or (None if sorted(briefed.values()) == [("general-subagent", SUBAGENT_TURNS)] * len(WORDS)
              else f"the children were briefed as {sorted(briefed.values())}")
          or (None if len(spaces) == len(WORDS) and len(set(spaces.values())) == len(WORDS)
              else f"the children's workspaces were {spaces}"))
    return checks, [solved.run_id, stopped.run_id, exported.run_id, planned.run_id]


def report(checks) -> list[str]:
    """A check whose subject never happened fails in every mode (DECISION-21e7f6fb)."""
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


def plan_orchestrator(model: str, image: str, registry: ModelRegistry, ceiling: Decimal) -> Orchestrator:
    """FR-177: one role, the general subagent, briefed with its documented turns."""
    child = general_subagent(model=model, image=image, registry=registry)
    return Orchestrator(roles={"general": child}, policy=BudgetPolicy(run_ceiling_usd=ceiling),
                        limits=SchedulerLimits(max_concurrent_subagents=4), context_policy=ContextPolicy(),
                        role_turns={"general": SUBAGENT_TURNS})


def _runner(models: dict, chosen, persistence, registry: ModelRegistry, tools=()) -> RecordingRunner:
    return RecordingRunner(models, model_registry=registry, workspace_provider=chosen, persistence=persistence,
                           tools=list(tools), max_concurrent_workspaces=8)


async def offline(store: str = "memory", provider_name: str = "fake", *, tenant: str = TENANT,
                  project: str | None = None, quiet: bool = False):
    """Every check with the scripted model; the tests call it on both stores. A project of its own each
    time, so one repetition's records never meet another's."""
    persistence = None
    if store == "postgres":
        from dotenv import load_dotenv

        from agentsdk import Persistence
        from agentsdk.config import normalise_database_url

        load_dotenv()
        persistence = Persistence.postgres(normalise_database_url(os.environ["DATABASE_URL"]), create_schema=False)
    chosen = provider(provider_name)
    project = project or f"general-{uuid.uuid4().hex[:8]}"
    registry, image = scripted_registry(), FAKE_IMAGE if provider_name == "fake" else IMAGE
    orchestrator = plan_orchestrator("m:scripted", image, registry, Decimal("5"))
    runner = _runner({"m": ScriptedModel(), "planner": ScriptedPlanner()}, chosen, persistence, registry,
                     tools=[orchestrator.tool])
    try:
        return await demonstrate(runner, chosen, orchestrator, model="m:scripted", registry=registry,
                                 image=image, persistence=persistence,
                                 tenant=tenant, project=project,
                                 ceilings={"task": Decimal("5"), "runaway": Decimal("0.01")},
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
        registry = live_registry()
        try:
            model = f"m:{settings.default_model}"
            orchestrator = plan_orchestrator(model, IMAGE, registry, Decimal("0.50"))
            runner = _runner({"m": client, "planner": ScriptedPlanner()}, docker, persistence, registry,
                             tools=[orchestrator.tool])
            checks, runs = await demonstrate(
                runner, docker, orchestrator, model=model, registry=registry, image=IMAGE,
                persistence=persistence, tenant=TENANT, project=f"general-{uuid.uuid4().hex[:8]}",
                ceilings={"task": Decimal("0.20"), "runaway": Decimal("0.00001")})
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
