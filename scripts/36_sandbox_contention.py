"""36 - Hostile code in a plan's sandboxed children: contained, recorded and cleaned up.

Phase 5's exit signal (P5-D14, FR-154). An orchestrated plan runs eight children, each in a
workspace of its own, and each runs a program that attacks its sandbox:

  net      connects to the host's database port and a public address, and resolves a name
  links    makes links out of its workspace, absolute and relative, then exports them
  fork     forks until fork fails, or 4 x `pids` processes
  memory   allocates until it is killed, or 4 x `memory_bytes`
  disk     writes `max_workspace_bytes` and 4 MiB more, then asks for another command
  output   prints until its timeout
  env      prints its environment
  sigterm  ignores SIGTERM, forks a process that leaves its group, and is cancelled -- it runs
           last, and the operator cancels the whole plan while it runs

Each program stops on its own past its bound, so a bound taken away is seen to fail and the host
is never at risk (DECISION-21e7f6fb). Before the plan, a Runner is killed mid-command -- a real
process on Docker -- and the plan's Runner reaps what it left before its first workspace. An
operator approves every command (`run_command` needs approval). Then `Runner.aclose()`.

Every check names its subject and has a witness, independent of the property, that the subject
happened (KNOWLEDGE-1afcf6b2); a subject that never happened is a failure, in every mode
(DECISION-a7d6a1f5, DECISION-21e7f6fb). Checks read the store, the provider and the commands' own
output -- never an error's text (DECISION-94bfee7c). In memory a child's events are not kept, so
checks there read the command and workspace rows, the histories and the interruptions.

Run it
  python scripts/36_sandbox_contention.py             # live: gateway + PostgreSQL + Docker
  python scripts/36_sandbox_contention.py --offline   # in memory, scripted children, FakeProvider
  python scripts/36_sandbox_contention.py --scripted  # in memory, scripted children, Docker

Live and --scripted need Docker running and the image below on the host:
  docker pull python:3.12-alpine@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import json
import os
import random
import secrets
import subprocess
import sys
import time
import uuid
from pathlib import Path

from agentsdk import AgentSpec, Message, Role, RunConfig, RunStatus, Runner
from agentsdk.budget import BudgetPolicy
from agentsdk.context_policy import ContextPolicy
from agentsdk.events import EventType
from agentsdk.identity import PrincipalContext
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.orchestrator import PLAN_TOOL, Orchestrator
from agentsdk.postgres import RunScope
from agentsdk.primitives import InstructionAuthority, TaintFlag, ToolCall, TrustZone
from agentsdk.scheduler import SchedulerLimits
from agentsdk.tools import ApprovalPolicy
from agentsdk.workspace import DockerProvider, FakeProvider, WorkspaceSpec
from agentsdk.workspace import owner as workspace_owner

TENANT, PROJECT = "example-tenant", "examples"
IMAGE = "python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df"
OPERATOR = PrincipalContext(agent_principal="example-36", user_principal="operator")
MiB = 2**20
COMMAND_SECONDS = 8.0  # every probe's timeout
KILL_SLACK = 15.0  # a timed-out command's container is removed within this, past its timeout
CANCEL_BOUND = 10.0  # FR-148: a cancelled command is killed and recorded within this
PIDS = 32
MEMORY_BYTES = 64 * MiB
DISK_BYTES = 8 * MiB
OUTPUT_BYTES = 64 * 1024
# Live, the plan and each child's calls are copied out whole: more than a model's default output.
LIVE_OUTPUT_TOKENS = 8000
HOST_VALUE_NAME = "AGENTSDK_EXAMPLE_36_HOST"  # set in this process; must reach no workspace and no record
TOOLS = ("run_command", "read_workspace_file", "write_workspace_file", "list_workspace", "export_workspace_file")
# Errors raised before a tool runs keep FR-40's labels, not NFR-42's.
BEFORE_RUNNING = {"ToolNotFound", "ToolValidationError", "ToolPermissionDenied", "ToolApprovalRequired",
                  "ToolApprovalRejected", "ToolApprovalCheckFailed", "ToolBlocked"}

# --- the probes: what each child runs ------------------------------------------------------------------

# No blank lines in a probe: a live model copying one out may drop one (M42 live run).
# Each attempt has 5 s in a thread of its own: with no network a resolver retries for as long as it
# likes, so one unanswered in time failed, and the probe ends in about 15 s whatever the load.
NET = """import socket, threading
def connect(host, port, out):
    try:
        socket.create_connection((host, port), timeout=3).close()
        out.append("CONNECTED")
    except OSError as e:
        out.append("failed " + type(e).__name__)
def resolve(host, out):
    try:
        out.append("RESOLVED " + socket.gethostbyname(host))
    except OSError as e:
        out.append("failed " + type(e).__name__)
for label, attempt, args in (("host.docker.internal 5432", connect, ("host.docker.internal", 5432)),
                             ("1.1.1.1 443", connect, ("1.1.1.1", 443)), ("resolve example.com", resolve, ("example.com",))):
    print("try", label, flush=True)
    out = []
    worker = threading.Thread(target=attempt, args=(*args, out), daemon=True)
    worker.start()
    worker.join(5)
    print(out[0] if out else "failed unanswered", label, flush=True)
print("done", flush=True)
"""
LINKS = """import os
os.symlink("/etc/passwd", "abs")
os.symlink("../../../../etc/passwd", "rel")
print("linked", sorted(os.listdir(".")), flush=True)
"""
FORK = f"""import os, time
n = 0
try:
    while n < {4 * PIDS}:
        if os.fork() == 0:
            time.sleep(600)
            os._exit(0)
        n += 1
except OSError as e:
    print("fork failed", type(e).__name__, flush=True)
print("forked", n, flush=True)
time.sleep(600)
"""
MEMORY = f"""held = []
total = 0
while total < {4 * MEMORY_BYTES}:
    held.append(b"x" * {8 * MiB})
    total += {8 * MiB}
    print("allocated", total, flush=True)
print("allocated all", total, flush=True)
"""
DISK = f"""total = 0
with open("fill.bin", "wb") as f:
    while total < {DISK_BYTES + 4 * MiB}:
        f.write(b"x" * {MiB})
        total += {MiB}
print("wrote", total, flush=True)
"""
AFTER = "print('after')\n"
OUTPUT = """import sys, time
while True:
    sys.stdout.write("y" * 65536)
    sys.stdout.flush()
    time.sleep(0.01)
"""
ENV = """import os
for name, value in sorted(os.environ.items()):
    print(name + "=" + value, flush=True)
"""
SIGTERM = """import os, signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
if os.fork() == 0:
    os.setsid()
    print("detached", os.getpid(), os.getsid(0), flush=True)
    time.sleep(600)
    os._exit(0)
print("parent", os.getpid(), os.getpgrp(), flush=True)
time.sleep(600)
"""
ARGV = {name: ["python", "-c", src] for name, src in
        (("net", NET), ("links", LINKS), ("fork", FORK), ("memory", MEMORY), ("disk", DISK), ("after", AFTER),
         ("output", OUTPUT), ("env", ENV), ("sigterm", SIGTERM))}


def runs_probe(argv, name: str) -> bool:
    """Whether `argv` runs probe `name`: `python -c` with the same program, however it is spaced. A
    live model copying a probe drops spaces after commas (M42 live run); the syntax tree is the
    program, so anything that would run differently is not the probe."""
    probe = ARGV[name]
    if not isinstance(argv, list) or len(argv) != len(probe) or argv[:2] != probe[:2] or not isinstance(argv[2], str):
        return False
    try:
        return ast.dump(ast.parse(argv[2])) == ast.dump(ast.parse(probe[2]))
    except Exception:  # noqa: BLE001 - a program that cannot be read is not the probe: a deep enough
        # nesting raises MemoryError or RecursionError, and a model can write one (M42 pre-review F1)
        return False

# Each child's calls, in order. The disk child asks for a command after it filled its workspace.
CALLS = {
    "net": [("run_command", {"argv": ARGV["net"]})],
    "links": [("run_command", {"argv": ARGV["links"]}),
              ("export_workspace_file", {"path": "abs", "mime_type": "text/plain"}),
              ("export_workspace_file", {"path": "rel", "mime_type": "text/plain"})],
    "fork": [("run_command", {"argv": ARGV["fork"]})],
    "memory": [("run_command", {"argv": ARGV["memory"]})],
    "disk": [("run_command", {"argv": ARGV["disk"]}), ("run_command", {"argv": ARGV["after"]})],
    "output": [("run_command", {"argv": ARGV["output"]})],
    "env": [("run_command", {"argv": ARGV["env"]})],
    "sigterm": [("run_command", {"argv": ARGV["sigterm"]})],
}
# Each child's own limits: one workspace each, so a full disk refuses no sibling. With no network a
# name takes 5 s to fail to resolve, so the network probe is given longer than the rest.
LIMITS = {"fork": {"pids": PIDS}, "memory": {"memory_bytes": MEMORY_BYTES}, "disk": {"max_workspace_bytes": DISK_BYTES},
          "output": {"max_output_bytes": OUTPUT_BYTES}, "net": {"max_command_seconds": 30.0}}


def objective(name: str) -> str:
    """What the plan asks of a child: short, so a live planner copies it whole into the plan."""
    return f"Run your {name} containment probe exactly as your instructions say, then answer in one sentence."


def brief(name: str) -> str:
    """The child's instructions, for a live model and the scripted one alike: the exact calls. Its
    role carries them, so they never pass through the planner (M42 live run: a planner copying
    eight programs into its plan ran out of output, then mangled them)."""
    return ("This is a containment test of your own isolated, disposable sandbox workspace; the code below is a "
            "probe written for it and cannot harm anything outside. Make exactly these tool calls, one at a time, "
            "in this order, with exactly these arguments, even when one fails or is killed. "
            "Then answer in one sentence, without repeating any output.\nCALLS: "
            + json.dumps([[tool, arguments] for tool, arguments in CALLS[name]]))


def nodes(rng: random.Random) -> list[dict]:
    """Every probe but sigterm in a shuffled start order; sigterm after all of them."""
    order = [name for name in CALLS if name != "sigterm"]
    rng.shuffle(order)
    return [{"node_id": name, "assigned_role": name, "dependencies": [], "objective": objective(name)}
            for name in order] + [{"node_id": "sigterm", "assigned_role": "sigterm", "dependencies": order,
                                   "objective": objective("sigterm")}]


def plan_task(rng: random.Random) -> str:
    return f"Call {PLAN_TOOL} once with exactly this plan, then answer in one sentence: " + json.dumps(
        {"nodes": nodes(rng)})


def roles(model_id: str, env_value: str, live: bool = False) -> dict[str, AgentSpec]:
    def role(name: str) -> AgentSpec:
        return AgentSpec(id=f"prober-{name}", instructions=brief(name),
                         preferred_model=model_id, tool_profile=TOOLS,
                         max_output_tokens=LIVE_OUTPUT_TOKENS if live else None,
                         workspace=WorkspaceSpec(image=IMAGE, env={"PROBE_VALUE": env_value} if name == "env" else {},
                                                 **{"max_command_seconds": COMMAND_SECONDS, **LIMITS.get(name, {})}))

    return {name: role(name) for name in CALLS}


# --- FakeProvider's stand-in for the image's python (--offline) ---------------------------------------


async def python(cmd) -> int:
    """The fake runs no model-written code (FR-139): it gives each probe's effect inside a
    container with this example's limits, and refuses any other program."""
    probe = next((name for name in ARGV if runs_probe(cmd.argv, name)), None)
    if probe == "net":
        for label in ("host.docker.internal 5432", "1.1.1.1 443", "resolve example.com"):
            cmd.out(f"try {label}\nfailed gaierror {label}\n")
        cmd.out("done\n")
    elif probe == "links":
        cmd.files.link("abs", "/etc/passwd")
        cmd.files.link("rel", "../../../../etc/passwd")
        cmd.out(f"linked {sorted(name for name, _, _ in cmd.files.listdir())}\n")
    elif probe == "fork":
        cmd.out(f"fork failed BlockingIOError\nforked {PIDS - 1}\n")
        await cmd.sleep(600)
    elif probe == "memory":
        total = 0
        while total + 8 * MiB < MEMORY_BYTES:
            total += 8 * MiB
            cmd.out(f"allocated {total}\n")
        cmd.limit("memory_bytes")
    elif probe == "disk":
        cmd.files.write("fill.bin", b"x" * (DISK_BYTES + 4 * MiB))
        cmd.out(f"wrote {DISK_BYTES + 4 * MiB}\n")
    elif probe == "after":
        cmd.out("after\n")
    elif probe == "output":
        while True:
            cmd.out("y" * 65536)
            await cmd.sleep(0.01)
    elif probe == "env":
        for name, value in sorted(cmd.env.items()):
            cmd.out(f"{name}={value}\n")
    elif probe == "sigterm":
        cmd.out("parent 1 1\n")
        await cmd.sleep(600)
    else:
        cmd.err("the fake runs only this example's probes\n")
        return 1
    return 0


async def sleep(cmd) -> int:
    await cmd.sleep(float(cmd.argv[1]))
    return 0


def provider(name: str):
    return DockerProvider() if name == "docker" else FakeProvider(commands={"python": python, "sleep": sleep})


# --- scripted models ----------------------------------------------------------------------------------


def _reply(calls=(), content=""):
    return ModelResponse(message=Message(role=Role.ASSISTANT, content=content, tool_calls=tuple(calls)),
                         stop_reason=StopReason.TOOL_CALLS if calls else StopReason.END_TURN, usage=Usage(20, 5, 25))


class ScriptedPlanner:
    async def send(self, request):
        if any(m.role is Role.TOOL for m in request.messages):
            return _reply(content="The plan ran.")
        task = next(m.content for m in request.messages if m.role is Role.USER)
        return _reply([ToolCall(id="plan", name=PLAN_TOOL, arguments=json.loads(task[task.index("{"):]))])


class ScriptedChild:
    """Makes its brief's calls, one a turn, after a random delay. With `quote`, each turn also
    writes the last result it read, verbatim, into a file of its workspace: a model quoting
    earlier results into a later call's arguments (DECISION-a00773b0). The quote goes beside the
    next call, in the same turn, since a plan's child has four turns; after the last call it has a
    turn of its own where one is left -- every child but links."""

    def __init__(self, rng: random.Random, delay: float, quote: bool = False) -> None:
        self.rng, self.delay, self.quote = rng, delay, quote

    async def send(self, request):
        await asyncio.sleep(self.rng.random() * self.delay)
        text = request.instructions or ""
        calls = [tuple(c) for c in json.loads(text[text.index("CALLS: ") + 7:].split("\n")[0])]
        turn = sum(1 for m in request.messages if m.role is Role.ASSISTANT)
        made = [ToolCall(id=f"t{turn}", name=name, arguments=arguments)
                for name, arguments in calls[turn:turn + 1]]
        read = [r for m in request.messages for r in m.tool_results if r.tool_call_id.startswith("t")]
        if self.quote and read and (made or (turn == len(calls) and turn + 2 <= 4)):
            made.append(ToolCall(id=f"q{turn}", name="write_workspace_file",
                                 arguments={"path": f"quoted-{turn}.txt", "content": read[-1].content}))
        return _reply(made) if made else _reply(content="Done.")


class RecordingRunner(Runner):
    """In memory a Runner keeps no child run's events: this one keeps every run's sink -- the plan's
    and each child's, which the orchestrator opens through `_open` -- so the checks read a child's
    events in memory as they read PostgreSQL's (M42 pre-review F3, DECISION-4689af02)."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.sinks: dict[str, object] = {}

    def _open(self, spec, config):
        opened = super()._open(spec, config)
        self.sinks[opened.scope.run_id] = opened.events
        return opened


class Sleeper:
    """The Runner that is killed: it starts one long command and waits."""

    async def send(self, request):
        if any(m.role is Role.TOOL for m in request.messages):
            return _reply(content="Done.")
        return _reply([ToolCall(id="t0", name="run_command", arguments={"argv": ["sleep", "3600"]})])


def doomed_agent(model_id: str) -> AgentSpec:
    return AgentSpec(id="doomed", instructions="Run the command you are told to.", preferred_model=model_id,
                     tool_profile=("run_command",),
                     workspace=WorkspaceSpec(image=IMAGE, run_command_approval=ApprovalPolicy.AUTO,
                                             # Its command outlives any plan: on the fake its Runner is in
                                             # this process, and must not end its run itself (M42).
                                             max_command_seconds=3600))


# --- what the provider holds ------------------------------------------------------------------------


def _docker(*args: str) -> list[str] | None:
    try:
        done = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return [line for line in done.stdout.split("\n") if line.strip()] if done.returncode == 0 else None


async def remains(chosen, label: str, value: str) -> list[str] | None:
    """What the provider still holds labelled so: containers and volumes on Docker, workspaces on
    the fake; None where Docker cannot be asked."""
    if isinstance(chosen, DockerProvider):
        found = await asyncio.gather(
            asyncio.to_thread(_docker, "ps", "-aq", "--filter", f"label={label}={value}"),
            asyncio.to_thread(_docker, "volume", "ls", "-q", "--filter", f"label={label}={value}"))
        return None if None in found else found[0] + found[1]
    return [h.workspace_id for h in await chosen.list_owned({label: value})]


async def running_in(chosen, label: str, value: str) -> str | None:
    """A witness that a command is under way in a workspace labelled so, independent of what is
    checked about it; None while none is. On Docker the sigterm probe's container must hold
    python processes in two sessions: the one that left its group is there."""
    if isinstance(chosen, DockerProvider):
        for container in await asyncio.to_thread(
                _docker, "ps", "-q", "--filter", f"label={label}={value}", "--filter", "status=running") or []:
            top = await asyncio.to_thread(_docker, "top", container, "-eo", "pid,sid,args") or []
            sessions = {line.split()[1] for line in top[1:] if "python" in line and len(line.split()) > 2}
            if label == "agentsdk.workspace" or len(sessions) >= 2:
                return f"container {container[:12]}: python in {len(sessions)} session(s)"
        return None
    for space in chosen._spaces.values():
        if space.handle.labels.get(label) == value and space.running:
            return f"workspace {space.handle.workspace_id}: {len(space.running)} command(s) running"
    return None


# --- the killed Runner ------------------------------------------------------------------------------


def _dead_label() -> str:
    """The owner label of this process with another start time: a process that is gone."""
    host, pid, start = workspace_owner.this_owner().rsplit(":", 2)
    return f"{host}:{pid}:{int(start) + 1}"


async def kill_a_runner(chosen, tenant: str, project: str, store: str, persistence):
    """A Runner killed mid-command. On Docker a real process, killed; on the fake -- which lives
    in this process -- a Runner in it labelled as owned by a process that is gone, left running.
    Returns (workspace id, run id, witness, the in-process Runner and its handle, or None)."""
    if isinstance(chosen, DockerProvider):
        child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--child-runner", tenant, project,
                                  store], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            line = await asyncio.wait_for(asyncio.to_thread(child.stdout.readline), 180)
        except asyncio.TimeoutError:
            line = ""
        parts = line.split()
        workspace_id, run_id = (parts[1], parts[2]) if len(parts) == 3 and parts[0] == "COMMAND" else (None, None)
        witness = None
        deadline = time.monotonic() + 60
        while workspace_id and witness is None and time.monotonic() < deadline:
            witness = await running_in(chosen, "agentsdk.workspace", workspace_id)
            if witness is None:
                await asyncio.sleep(0.5)
        child.kill()
        await asyncio.to_thread(child.wait, 30)
        return workspace_id, run_id, witness, None
    dead = Runner({"sleeper": Sleeper()}, workspace_provider=chosen, persistence=persistence)
    dead._workspaces.owner = _dead_label()
    handle = await dead.start(doomed_agent("sleeper:m"), "Run sleep 3600.",
                              RunConfig(tenant_id=tenant, project_id=project, max_turns=3))
    witness, workspace_id = None, None
    deadline = time.monotonic() + 30
    while witness is None and time.monotonic() < deadline:
        for h in await chosen.list_owned({"agentsdk.run": handle.run_id}):
            workspace_id = h.workspace_id
            witness = await running_in(chosen, "agentsdk.workspace", workspace_id)
        await asyncio.sleep(0.02)
    return workspace_id, handle.run_id, witness, (dead, handle)


async def child_runner(tenant: str, project: str, store: str) -> None:
    """--child-runner: the Runner that is killed. Prints its command's workspace once it runs."""
    persistence = None
    if store == "postgres":
        from dotenv import load_dotenv

        from agentsdk import Persistence
        from agentsdk.config import normalise_database_url

        load_dotenv()
        persistence = Persistence.postgres(normalise_database_url(os.environ["DATABASE_URL"]), create_schema=False)
    runner = Runner({"sleeper": Sleeper()}, workspace_provider=DockerProvider(), persistence=persistence)
    handle = await runner.start(doomed_agent("sleeper:m"), "Run sleep 3600.",
                                RunConfig(tenant_id=tenant, project_id=project, max_turns=3))
    async for event in handle.events():
        if event.event_type is EventType.WORKSPACE_COMMAND_STARTED:
            print("COMMAND", event.payload["workspace_id"], handle.run_id, flush=True)
    await handle.result()


# --- the operator -----------------------------------------------------------------------------------


async def operator(runner: Runner, tenant: str, project: str, top: str, rng: random.Random, seen: dict) -> None:
    """Approves every command of the plan, in a shuffled order; notes the sigterm child."""
    decided: set[str] = set()
    while True:
        views = [v for v in await runner.interruptions(tenant, project, run_id=top)
                 if v.interruption.interruption_id not in decided]
        rng.shuffle(views)
        for view in views:
            decided.add(view.interruption.interruption_id)
            argv = (view.interruption.request_payload.get("arguments") or {}).get("argv")
            try:
                await runner.decide(tenant, project, view.interruption.interruption_id, approve=True,
                                    principal=OPERATOR)
            except Exception:  # noqa: BLE001 - judged by the checks
                continue
            if runs_probe(argv, "sigterm"):
                seen["sigterm"].append(view.interruption.run_id)
        await asyncio.sleep(0.05)


async def canceller(handle, chosen, seen: dict) -> None:
    """Once the sigterm probe runs -- on Docker, with its detached process -- cancels the plan."""
    while not seen["sigterm"]:
        await asyncio.sleep(0.05)
    child = seen["sigterm"][0]
    deadline = time.monotonic() + 90
    while seen["under_way"] is None and time.monotonic() < deadline:
        seen["under_way"] = await running_in(chosen, "agentsdk.run", child)
        if seen["under_way"] is None:
            await asyncio.sleep(0.25)
    seen["asked"] = time.monotonic()
    handle.cancel()


# --- the demonstration ------------------------------------------------------------------------------


def stdout_of(content: str) -> str:
    """A command's stdout, from what the model read of it (workspace.tools.describe)."""
    if "--- stdout ---\n" not in content:
        return ""
    return content.split("--- stdout ---\n", 1)[1].split("\n--- stderr ---", 1)[0]


async def demonstrate(runner: Runner, orchestrator: Orchestrator, chosen, *, model_id: str, planner_id: str,
                      tenant: str, project: str, persistence, store: str, rng: random.Random, env_value: str,
                      live: bool, say=print):
    host_value = os.environ[HOST_VALUE_NAME]
    workspaces = runner._workspace_store()

    # --- a Runner killed mid-command -----------------------------------------------------------------
    dead_project = f"{project}-dead"
    dead_workspace, dead_run, dead_witness, in_process = await kill_a_runner(
        chosen, tenant, dead_project, store, persistence)
    say(f"killed Runner: run {dead_run}, workspace {dead_workspace}: {dead_witness or 'never seen running'}")

    # --- the plan -----------------------------------------------------------------------------------
    handle = await runner.start(orchestrator.agent, plan_task(rng), orchestrator.config(
        tenant_id=tenant, project_id=project, model_override=planner_id, max_turns=6, approval_timeout=300,
        max_output_tokens=LIVE_OUTPUT_TOKENS if live else None))
    seen = {"sigterm": [], "under_way": None, "asked": None}
    ended = asyncio.ensure_future(handle.result())
    helpers = [asyncio.ensure_future(operator(runner, tenant, project, handle.run_id, rng, seen)),
               asyncio.ensure_future(canceller(handle, chosen, seen))]
    try:
        result = await ended
    finally:
        for helper in helpers:
            helper.cancel()
    reported = time.monotonic()
    sigterm_child = seen["sigterm"][0] if seen["sigterm"] else None
    left_at_cancel = await remains(chosen, "agentsdk.run", sigterm_child) if sigterm_child else None
    after_plan = await remains(chosen, "agentsdk.tenant", tenant)
    dead_left = await remains(chosen, "agentsdk.workspace", dead_workspace) if dead_workspace else None
    if in_process is not None:
        dead_runner, dead_handle = in_process
        try:
            await asyncio.wait_for(dead_handle.result(), 60)
        except asyncio.TimeoutError:
            dead_handle.cancel()
        await dead_runner.aclose()
    say(f"plan {result.run_id}: {result.status.value}")

    # --- what was recorded ----------------------------------------------------------------------------
    views = await runner.interruptions(tenant, project, run_id=result.run_id, pending_only=False)
    children = sorted(orchestrator._runs[result.run_id].children) if result.run_id in orchestrator._runs else []
    # A child cancelled with its plan is never reported to the orchestrator: the operator saw it.
    children = sorted(set(children) | set(seen["sigterm"]) | await descendants(persistence, result.run_id))
    histories = {}
    for child in children:
        sessions = runner._sessions_for(RunScope(run_id=child, tenant_id=tenant, project_id=project))
        histories[child] = await asyncio.to_thread(sessions.history, child)
    commands = await asyncio.to_thread(workspaces.commands, tenant, project)
    spaces = await asyncio.to_thread(workspaces.workspaces, tenant, project)
    dead_spaces = await asyncio.to_thread(workspaces.workspaces, tenant, dead_project)
    events = await child_events(runner, persistence, children)
    artifacts = await stored_artifacts(runner, persistence, tenant, project, children)

    # Whatever the plan did, what the killed Runner left goes now: a plan that never made a
    # workspace never reaped. Its check above read what was left before this.
    await runner._workspaces.reap()
    await runner.aclose()
    at_end = await remains(chosen, "agentsdk.tenant", tenant)
    stored, own = await stored_text(runner, persistence, tenant, project, [result.run_id, *children], result,
                                    histories, views, spaces, commands)

    calls, returned = [], {}
    for child, history in histories.items():
        for m in history:
            if m.role is Role.ASSISTANT:
                calls += [(child, c.id, c.name, dict(c.arguments)) for c in m.tool_calls]
            for r in m.tool_results:
                returned[(child, r.tool_call_id)] = r
    rows = {(c.run_id, c.tool_call_id): c for c in commands}

    def probe(name: str):
        """Each (child, call id, row, stdout) of a run_command whose argv is the probe's own."""
        return [(child, cid, rows.get((child, cid)), stdout_of(returned[(child, cid)].content)
                 if (child, cid) in returned else "")
                for child, cid, tool, arguments in calls if tool == "run_command" and runs_probe(arguments.get("argv"), name)]

    def lines(text: str, word: str) -> list[list[str]]:
        return [line.split() for line in text.split("\n") if line.split()[:1] == [word]]

    checks = []

    def check(label: str, subject, why_absent: str, failing) -> None:
        """A check whose subject never happened fails; else the first failing clause, or None."""
        if not subject:
            checks.append((label, None, why_absent))
            return
        clause = failing()
        checks.append((label, clause is None, clause))

    # Network: the probe ran its three attempts to the end at least once; no run of it -- one killed
    # half way included -- reached anything (M42 pre-review F2).
    net = [p for p in probe("net") if p[2] is not None and p[2].exit_code == 0 and "done" in p[3].split()]
    check("no connection or name resolution succeeded", net,
          "the network probe never ran to its end: " + repr([(row.exit_code, row.killed) if row else "no row"
                                                              for _, _, row, _ in probe("net")]),
          lambda: next((f"{child} reached out: {line}" for child, _, _, out in probe("net")
                        for line in out.split("\n") if line.split()[:1] in (["CONNECTED"], ["RESOLVED"])), None)
          or next((f"{child} made {len(lines(out, 'try'))} attempts" for child, _, _, out in net
                   if len(lines(out, "try")) != 3), None))

    # Links: made (the probe said so, exit 0) and both exported; every export failed, no artifact.
    linked = [p for p in probe("links") if p[2] is not None and p[2].exit_code == 0 and lines(p[3], "linked")]
    exports = [(child, cid, arguments["path"]) for child, cid, tool, arguments in calls
               if tool == "export_workspace_file" and child in {p[0] for p in linked}
               and arguments.get("path") in ("abs", "rel")]
    check("no link crossed: every export of one failed, and nothing left the workspace",
          linked and {path for _, _, path in exports} == {"abs", "rel"}, "the links were never made and exported",
          lambda: next((f"the export of {path} by {child} succeeded" for child, cid, path in exports
                        if (child, cid) not in returned or not returned[(child, cid)].is_error), None)
          or next((f"an artifact of {ref} left {child}" for child, ref in artifacts
                   if child in {c for c, _, _ in exports}), None))

    # Pids: the bomb printed how far it got; it stopped below the bound, and ended at its timeout.
    forked = [p for p in probe("fork") if p[2] is not None and lines(p[3], "forked")]
    check("a fork bomb stopped at pids, and ended at its timeout", forked, "the fork bomb never said how far it got",
          lambda: next((f"{child} forked {lines(out, 'forked')[0][1]} processes, past pids {PIDS}"
                        for child, _, _, out in forked if int(lines(out, "forked")[0][1]) >= PIDS), None)
          or next((f"{child}'s fork bomb ended {row.killed}" for child, _, row, _ in forked
                   if row.killed != "timeout"), None)
          # Failed at the pid limit, which fork reports as EAGAIN -- not any early error (pre-review F4).
          or next((f"{child}'s fork never failed at the pid limit" for child, _, _, out in forked
                   if ["fork", "failed", "BlockingIOError"] not in lines(out, "fork")), None))

    # Memory: it allocated; it was killed at memory_bytes, named so, before it held that much.
    allocated = [p for p in probe("memory") if p[2] is not None and lines(p[3], "allocated")]
    memory_events = [e for e in events if e[1] == EventType.WORKSPACE_COMMAND_ENDED.value
                     and e[2].get("command_id") in {row.command_id for _, _, row, _ in allocated}]
    check("an allocation without end was killed at memory_bytes, recorded by name", allocated,
          "the allocation never began",
          lambda: next((f"{child}'s allocation ended {row.killed}, limit {row.limit}" for child, _, row, _ in allocated
                        if (row.killed, row.limit) != ("limit", "memory_bytes")), None)
          or next((f"{child} held {lines(out, 'allocated')[-1][-1]} B" for child, _, _, out in allocated
                   if lines(out, "allocated")[-1][1] == "all"
                   or int(lines(out, "allocated")[-1][-1]) > MEMORY_BYTES), None)
          or next((f"its event names {e[2].get('limit')}" for e in memory_events if e[2].get("limit") != "memory_bytes"),
                  None))

    # Disk: it wrote past the bound and asked again; the next command was refused and never ran.
    filled = [p for p in probe("disk") if p[2] is not None and p[2].exit_code == 0 and lines(p[3], "wrote")
              and int(lines(p[3], "wrote")[0][1]) > DISK_BYTES]
    afters = [(child, cid) for child, cid, tool, arguments in calls if tool == "run_command"
              and runs_probe(arguments.get("argv"), "after") and child in {p[0] for p in filled}]
    refusals = [e for e in events if e[1] == EventType.WORKSPACE_REFUSED.value and e[0] in {c for c, _ in afters}]
    check("past max_workspace_bytes the next command was refused, named by the bound", filled and afters,
          "the disk was never filled past its bound and asked again",
          lambda: next((f"{child}'s command after the bound ran" for child, cid in afters
                        if (child, cid) in rows or not returned.get((child, cid)) or not returned[(child, cid)].is_error),
                       None)
          # On both stores: in memory the children's events are kept too (pre-review F3).
          or (None if refusals and all(e[2].get("bound") == "max_workspace_bytes"
                                       and e[2].get("size", 0) > e[2].get("limit", 0) for e in refusals)
              else "no refusal names max_workspace_bytes"))

    # Output: it printed; what was kept is max_output_bytes, the rest counted, and it timed out.
    printed = [p for p in probe("output") if p[2] is not None and p[2].stdout_bytes > 0]
    check("output without end was kept to max_output_bytes, and ended at its timeout", printed,
          "the printing probe never printed",
          lambda: next((f"{child} kept {row.stdout_kept} of {row.stdout_bytes} B, ended {row.killed}"
                        for child, _, row, _ in printed
                        if not (row.stdout_bytes > OUTPUT_BYTES and row.stdout_kept == OUTPUT_BYTES
                                and row.killed == "timeout")), None))

    # Timeouts: each command killed at its timeout ended within its kill bound; nothing of its run stays.
    timed_out = [row for row in commands if row.killed == "timeout"]
    check("no command outlived its timeout", timed_out and after_plan is not None, "no command reached its timeout",
          lambda: next((f"{row.command_id} took {(row.ended_at - row.started_at).total_seconds():.1f} s"
                        for row in timed_out
                        if (row.ended_at - row.started_at).total_seconds() > COMMAND_SECONDS + KILL_SLACK), None)
          or (f"{len(after_plan)} container(s) or volume(s) remained after the plan" if after_plan else None))

    # Cancel: the probe ran (on Docker, its detached process seen) when the plan was cancelled; when
    # the plan reported cancelled, nothing of that child remained, within CANCEL_BOUND.
    sigterm_rows = [row for row in commands if row.run_id == sigterm_child]
    check("a cancelled command that ignores SIGTERM and left its group was gone when the plan reported cancelled",
          seen["under_way"] and seen["asked"] is not None and left_at_cancel is not None,
          "the sigterm probe was never under way at the cancel",
          lambda: (f"the plan ended {result.status.value}" if result.status is not RunStatus.CANCELLED else None)
          or (f"{left_at_cancel} remained" if left_at_cancel else None)
          or (f"reported {reported - seen['asked']:.1f} s after the cancel"
              if reported - seen["asked"] > CANCEL_BOUND + 2 * KILL_SLACK else None)
          or (None if [r.killed for r in sigterm_rows] == ["cancelled"] else
              f"its command rows: {[r.killed for r in sigterm_rows]}"))

    # Reaping: the killed Runner's command ran; the next Runner removed its workspace and recorded it.
    check("a Runner killed mid-command left nothing once the next Runner started",
          dead_witness and dead_left is not None, "the killed Runner's command was never seen running",
          lambda: (f"{dead_left} remained" if dead_left else None)
          or (None if persistence is None or [w.removed_reason for w in dead_spaces] == ["reaped"]
              else f"its workspace row reads {[w.removed_reason for w in dead_spaces]}"))

    # The end: after aclose() and reaping, nothing the example's Runners made remains.
    check("after aclose() and reaping no container or volume with this tenancy's labels remains",
          spaces and at_end is not None, "no workspace was ever made",
          lambda: f"{at_end} remained" if at_end else None)

    # Secrets: the env value reached its command; no host value is anywhere; the env value is in
    # no record the SDK writes of its own (only command output and what the model wrote hold it).
    printed_env = [p for p in probe("env") if p[2] is not None and f"PROBE_VALUE={env_value}" in p[3].split("\n")]
    check("no host value reached a workspace or a record, and the env value only what printed or quoted it",
          printed_env and stored, "the env value never reached the command that prints it",
          lambda: (f"{sum(host_value in text for text in stored)} record(s) hold the host value"
                   if any(host_value in text for text in stored) else None)
          or (f"{sum(env_value in text for text in own)} record(s) of the SDK's own hold the env value"
              if any(env_value in text for text in own) else None))

    # Approvals: every command row's call was approved before it started, and ran once.
    approved = {(v.interruption.run_id, v.interruption.tool_call_id): v.decision.decided_at for v in views
                if v.decision is not None and v.decision.decision == "approve"}
    check("every command ran only after an approve decision, and at most once", commands, "no command ran",
          lambda: next((f"{row.command_id} ran without an approval before it" for row in commands
                        if (row.run_id, row.tool_call_id) not in approved
                        or row.started_at < approved[(row.run_id, row.tool_call_id)]), None)
          or (None if len(rows) == len(commands) else "a call ran more than once"))

    # Labels: every result of a workspace tool that ran is untrusted, data-only, external, executable.
    before = {(e[0], e[2].get("tool_call_id")) for e in events
              if e[1] == EventType.TOOL_CALLED.value and e[2].get("error_type") in BEFORE_RUNNING}
    judged = [r for (child, cid), r in returned.items()
              if any(c == child and i == cid and tool in TOOLS for c, i, tool, _ in calls) and (child, cid) not in before]
    check("every workspace result is untrusted and data-only", judged, "no workspace tool returned",
          lambda: next((f"{r.tool_call_id} is {r.provenance.trust_zone.value}" for r in judged
                        if not (r.provenance.trust_zone is TrustZone.UNTRUSTED
                                and r.provenance.instruction_authority is InstructionAuthority.DATA_ONLY
                                and {TaintFlag.EXTERNAL_CONTENT, TaintFlag.EXECUTABLE_CONTENT}
                                <= set(r.provenance.taint_flags))), None))

    if not live:
        ran = {name for name in ARGV if probe(name)}
        check("every probe ran, and the plan was cancelled during the last", True, "",
              lambda: (f"never ran: {sorted(set(ARGV) - ran)}" if ran != set(ARGV) else None)
              or (None if result.status is RunStatus.CANCELLED else f"the plan ended {result.status.value}"))
    return checks, [result.run_id, *children, *([dead_run] if dead_run else [])]


async def descendants(persistence, top: str) -> set:
    """Every run under the plan, from the store's parent links; in memory, none beyond the
    orchestrator's own record."""
    if persistence is None:
        return set()
    import psycopg

    from agentsdk.config import normalise_database_url

    def read() -> set:
        with psycopg.connect(normalise_database_url(os.environ["DATABASE_URL"])) as conn:
            return {str(r) for (r,) in conn.execute(
                "WITH RECURSIVE tree(run_id) AS (SELECT run_id FROM runs WHERE parent_run_id = %s::uuid UNION "
                "SELECT r.run_id FROM runs r JOIN tree t ON r.parent_run_id = t.run_id) SELECT run_id FROM tree",
                (top,)).fetchall()}

    return await asyncio.to_thread(read)


async def child_events(runner, persistence, children: list) -> list:
    """(run id, event type, payload) of every child's events: from PostgreSQL, or in memory from
    the sinks a `RecordingRunner` kept."""
    if persistence is None:
        return [(child, e.event_type.value, e.payload) for child in children
                for e in (runner.sinks[child].events() if child in getattr(runner, "sinks", {}) else ())]
    import psycopg

    from agentsdk.config import normalise_database_url

    def read() -> list:
        with psycopg.connect(normalise_database_url(os.environ["DATABASE_URL"])) as conn:
            return [(str(r), t, p) for r, t, p in conn.execute(
                "SELECT run_id, event_type, payload FROM run_events WHERE run_id = ANY(%s::uuid[]) ORDER BY run_id, sequence_no",
                (children,)).fetchall()]

    return await asyncio.to_thread(read)


async def stored_artifacts(runner: Runner, persistence, tenant: str, project: str, children: list) -> list:
    """(source run, artifact id) of every artifact the children exported from a workspace: its
    source names one (FR-150). A child's answer is an artifact too, of the plan's making."""
    store = runner._artifacts_for(tenant, project)
    if persistence is None:
        ids = [ref.artifact_id for ref, _ in store._rows.values() if ref.source_run in children]
    else:
        import psycopg

        from agentsdk.config import normalise_database_url

        def read() -> list:
            with psycopg.connect(normalise_database_url(os.environ["DATABASE_URL"])) as conn:
                return [str(a) for (a,) in conn.execute(
                    "SELECT artifact_id FROM artifacts WHERE source_run = ANY(%s::uuid[])", (children,)).fetchall()]

        ids = await asyncio.to_thread(read)
    found = []
    for artifact_id in ids:
        ref = await store.metadata(artifact_id)
        if (ref.provenance.source_uri_or_hash or "").startswith("workspace://"):
            found.append((ref.source_run, artifact_id))
    return found


# Events the SDK writes from what it knows itself: no workspace value belongs in one.
SDK_OWN_EVENTS = {e.value for e in (
    EventType.RUN_STARTED, EventType.RUN_FAILED, EventType.RUN_CANCELLED, EventType.MODEL_CALLED, EventType.TOOL_CALLED,
    EventType.WORKSPACE_CREATED, EventType.WORKSPACE_COMMAND_ENDED, EventType.WORKSPACE_REFUSED,
    EventType.WORKSPACE_WAITING, EventType.WORKSPACE_REMOVED)}


async def stored_text(runner, persistence, tenant: str, project: str, runs: list, result, histories, views, spaces,
                      commands):
    """(every record, as text; the records the SDK writes of its own, as text). A command's argv
    is what a model wrote, recorded as written (NFR-45), so it is left out of the second."""
    def command_text(c) -> str:
        return repr({k: v for k, v in c.as_row().items() if k != "argv"})

    if persistence is None:
        # Every run's events this Runner opened, the plan's and each child's (DECISION-4689af02).
        payloads = [(e.event_type.value, repr(e.payload)) for sink in getattr(runner, "sinks", {}).values()
                    for e in sink.events()] or [(e.event_type.value, repr(e.payload)) for e in result.events]
        everything = ([text for _, text in payloads] + [repr(m) for h in histories.values() for m in h]
                      + [repr(v) for v in views] + [repr(w) for w in spaces] + [repr(c) for c in commands]
                      + [repr(result.error), repr(result.output)])
        own = ([text for kind, text in payloads if kind in SDK_OWN_EVENTS]
               + [repr(w) for w in spaces] + [command_text(c) for c in commands] + [repr(result.error)])
        return everything, own
    import psycopg

    from agentsdk.config import normalise_database_url

    def read() -> tuple[list[str], list[str]]:
        everything, own = [], []
        with psycopg.connect(normalise_database_url(os.environ["DATABASE_URL"])) as conn:
            for table in ("runs", "messages", "run_events", "execution_manifests", "plan_versions", "plan_node_states",
                          "interruptions"):
                for row in conn.execute(f"SELECT to_jsonb(t) FROM {table} t WHERE run_id = ANY(%s::uuid[])",
                                        (runs,)).fetchall():
                    text = json.dumps(row[0], default=str)
                    everything.append(text)
                    if table in ("runs", "execution_manifests") or (
                            table == "run_events" and row[0].get("event_type") in SDK_OWN_EVENTS):
                        own.append(text)
            for query in ("SELECT to_jsonb(d) FROM interruption_decisions d WHERE interruption_id IN (SELECT "
                          "interruption_id FROM interruptions WHERE run_id = ANY(%s::uuid[]))",
                          "SELECT to_jsonb(a) FROM artifacts a WHERE source_run = ANY(%s::uuid[])"):
                everything += [json.dumps(r[0], default=str) for r in conn.execute(query, (runs,)).fetchall()]
            for table in ("workspaces", "workspace_commands"):
                for row in conn.execute(f"SELECT to_jsonb(t) FROM {table} t WHERE tenant_id = %s AND project_id = %s",
                                        (tenant, project)).fetchall():
                    everything.append(json.dumps(row[0], default=str))
                    own.append(json.dumps({k: v for k, v in row[0].items() if k != "argv"}, default=str))
        return everything, own

    return await asyncio.to_thread(read)


# --- running it ---------------------------------------------------------------------------------------


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
    """Why this example cannot use Docker here, or None."""
    try:
        if subprocess.run(["docker", "version"], capture_output=True, timeout=30).returncode != 0:
            return "docker not running"
        if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, timeout=30).returncode != 0:
            return "the example image is not on this host (see the docstring to pull it)"
    except (OSError, subprocess.TimeoutExpired):
        return "docker not running"
    return None


def orchestrator_for(model_id: str, env_value: str, live: bool = False) -> Orchestrator:
    return Orchestrator(roles=roles(model_id, env_value, live), policy=BudgetPolicy(run_ceiling_tokens=4_000_000),
                        limits=SchedulerLimits(max_concurrent_subagents=len(CALLS) + 2),
                        context_policy=ContextPolicy(context_window=200_000))


async def _with_host_value(go):
    """A value in this process's environment, which no workspace and no record may hold."""
    os.environ[HOST_VALUE_NAME] = secrets.token_hex(16)
    try:
        return await go()
    finally:
        os.environ.pop(HOST_VALUE_NAME, None)


async def offline(store: str = "memory", provider_name: str = "fake", *, seed: int | None = None,
                  tenant: str = TENANT, project: str | None = None, quote: bool = False, quiet: bool = False):
    """The exit signal with scripted children; the tests call it, ten times on Docker. A project of
    its own each time, so one repetition's records never meet another's."""
    rng = random.Random(seed)
    persistence = None
    if store == "postgres":
        from dotenv import load_dotenv

        from agentsdk import Persistence
        from agentsdk.config import normalise_database_url

        load_dotenv()
        persistence = Persistence.postgres(normalise_database_url(os.environ["DATABASE_URL"]), create_schema=False)
    chosen = provider(provider_name)
    env_value = secrets.token_hex(16)
    orchestrator = orchestrator_for("child:m", env_value)
    delay = 0.5 if provider_name == "docker" else 0.01
    kind = RecordingRunner if persistence is None else Runner  # in memory, the children's events kept
    runner = kind({"planner": ScriptedPlanner(), "child": ScriptedChild(rng, delay, quote)},
                  tools=[orchestrator.tool], workspace_provider=chosen, persistence=persistence,
                  max_concurrent_workspaces=len(CALLS) + 1)

    async def go():
        return await demonstrate(runner, orchestrator, chosen, model_id="child:m", planner_id="planner:p",
                                 tenant=tenant, project=project or f"contention-{uuid.uuid4().hex[:8]}",
                                 persistence=persistence, store=store, rng=rng, env_value=env_value, live=False,
                                 say=(lambda *a: None) if quiet else print)

    return await _with_host_value(go)


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
            model_id = f"m:{settings.default_model}"
            env_value = secrets.token_hex(16)
            docker = DockerProvider()
            orchestrator = orchestrator_for(model_id, env_value, live=True)
            runner = Runner({"m": client}, tools=[orchestrator.tool], workspace_provider=docker,
                            persistence=persistence, max_concurrent_workspaces=len(CALLS) + 1)

            async def demo():
                return await demonstrate(runner, orchestrator, docker, model_id=model_id, planner_id=model_id,
                                         tenant=TENANT, project=f"contention-{uuid.uuid4().hex[:8]}",
                                         persistence=persistence, store="postgres", rng=random.Random(),
                                         env_value=env_value, live=True)

            checks, runs = await _with_host_value(demo)
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
    if sys.argv[1:2] == ["--child-runner"]:
        asyncio.run(child_runner(*sys.argv[2:5]))
        raise SystemExit(0)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="in memory, scripted children, FakeProvider")
    mode.add_argument("--scripted", action="store_true", help="in memory, scripted children, Docker")
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
