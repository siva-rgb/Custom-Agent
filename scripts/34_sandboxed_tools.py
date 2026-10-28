"""34 - Sandboxed tools: an agent writes a program and runs it, and every command ends.

An agent with a workspace writes a small Python program, runs it, and reads back the file it
wrote. Its operator approves each command after reading the full argv, and refuses one. A command
that runs too long is killed at its timeout, and the model is told. Then a second run is cancelled
while its command is still running: the command's container is gone before the run reports
cancelled, within 10 s of the cancel, whatever the process inside it does (FR-148).

Run it
  python scripts/34_sandboxed_tools.py             # live: gateway + PostgreSQL + Docker
  python scripts/34_sandboxed_tools.py --offline   # in memory, scripted model, FakeProvider
  python scripts/34_sandboxed_tools.py --scripted  # in memory, scripted model, Docker

Live and --scripted need Docker running and the image below on the host:
  docker pull python:3.12-alpine@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time

from agentsdk import AgentSpec, Message, Role, RunConfig, RunStatus, Runner
from agentsdk.events import EventType
from agentsdk.identity import PrincipalContext
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.postgres import RunScope
from agentsdk.primitives import InstructionAuthority, TaintFlag, ToolCall, TrustZone
from agentsdk.workspace import DockerProvider, FakeProvider, WorkspaceSpec

TENANT, PROJECT = "example-tenant", "examples"
IMAGE = "python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df"
OPERATOR = PrincipalContext(agent_principal="example-34", user_principal="operator")
PROGRAM = 'total = sum(n * n for n in range(1, 11))\nopen("out.txt", "w").write(f"{total}\\n")\n'
TASK = ("In your workspace: write src/squares.py containing exactly this program:\n" + PROGRAM +
        "then run ['python', 'src/squares.py'], read out.txt, run ['rm', '-rf', 'src'], and run ['sleep', '30'] "
        "with timeout_seconds 2. Then answer in one sentence.")
CANCEL_TASK = "Run ['sleep', '120'] in your workspace, then answer in one sentence."
CANCEL_BOUND = 10.0  # FR-148: the run reports cancelled within this many seconds of the cancel


# --- FakeProvider's stand-ins for the image's commands (--offline) ---------------------------------


async def python(cmd) -> int:
    """A stand-in for the image's python: the fake runs no model-written code (FR-139), so it
    gives the effect of this example's program only, and refuses any other."""
    if cmd.files.read(cmd.argv[1]).decode() != PROGRAM:
        cmd.err("the fake runs only this example's program\n")
        return 1
    cmd.files.write("out.txt", f"{sum(n * n for n in range(1, 11))}\n")
    return 0


async def sleep(cmd) -> int:
    await cmd.sleep(float(cmd.argv[1]))
    return 0


def provider(name: str):
    return DockerProvider() if name == "docker" else FakeProvider(commands={"python": python, "sleep": sleep})


def agent(model_id: str) -> AgentSpec:
    return AgentSpec(
        id="sandboxed-coder", instructions="Use your workspace tools exactly as asked.", preferred_model=model_id,
        tool_profile=("run_command", "read_workspace_file", "write_workspace_file", "list_workspace"),
        workspace=WorkspaceSpec(image=IMAGE))


class ScriptedModel:
    """Offline: does what its task says, one call a turn."""

    CALLS = [("write_workspace_file", {"path": "src/squares.py", "content": PROGRAM}),
             ("run_command", {"argv": ["python", "src/squares.py"]}),
             ("read_workspace_file", {"path": "out.txt"}),
             ("run_command", {"argv": ["rm", "-rf", "src"]}),
             ("run_command", {"argv": ["sleep", "30"], "timeout_seconds": 2})]
    CANCEL_CALLS = [("run_command", {"argv": ["sleep", "120"]})]

    async def send(self, request):
        calls = self.CANCEL_CALLS if request.messages[0].content == CANCEL_TASK else self.CALLS
        turn = sum(1 for m in request.messages if m.role is Role.ASSISTANT)
        if turn < len(calls):
            name, arguments = calls[turn]
            return ModelResponse(message=Message(role=Role.ASSISTANT, tool_calls=(
                ToolCall(id=f"t{turn}", name=name, arguments=arguments),)),
                stop_reason=StopReason.TOOL_CALLS, usage=Usage(20, 5, 25))
        return ModelResponse(message=Message(role=Role.ASSISTANT, content="out.txt holds 385."),
                             stop_reason=StopReason.END_TURN, usage=Usage(20, 5, 25))


async def operator(runner: Runner, run_id: str, decided: list) -> None:
    """Reads each command's full argv: approves it, but refuses one that removes files."""
    while True:
        for view in await runner.interruptions(TENANT, PROJECT, run_id=run_id):
            argv = view.interruption.request_payload["arguments"]["argv"]
            approve = argv[0] != "rm"
            print(f"  operator {'approves' if approve else 'refuses'}: {argv}")
            await runner.decide(TENANT, PROJECT, view.interruption.interruption_id, approve=approve,
                                principal=OPERATOR, message=None if approve else "no removing files")
            decided.append((view.interruption.tool_call_id, approve))
        await asyncio.sleep(0.05)


def _containers_of(run_id: str) -> list[str] | None:
    """The containers labelled with the run, or None where Docker cannot be asked."""
    try:
        done = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=agentsdk.run={run_id}"],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.split() if done.returncode == 0 else None


async def _history(runner: Runner, result):
    sessions = runner._sessions_for(RunScope(run_id=result.run_id, tenant_id=TENANT, project_id=PROJECT))
    return await asyncio.to_thread(sessions.history, result.run_id)


async def demonstrate(runner: Runner, model_id: str, *, live: bool, persisted: bool, provider_name: str,
                      chosen=None):
    # --- the first run: write, run, read, a refusal and a timeout -----------------------------------
    handle = await runner.start(agent(model_id), TASK, RunConfig(tenant_id=TENANT, project_id=PROJECT, max_turns=12))
    decided: list = []
    approving = asyncio.ensure_future(operator(runner, handle.run_id, decided))
    try:
        result = await handle.result()
    finally:
        approving.cancel()
    history = await _history(runner, result)
    issued = {c.id: (c.name, c.arguments) for m in history if m.role is Role.ASSISTANT for c in m.tool_calls}
    results = {r.tool_call_id: r for m in history for r in m.tool_results}
    kinds = [e.event_type for e in result.events]
    started = [e.payload for e in result.events if e.event_type is EventType.WORKSPACE_COMMAND_STARTED]
    ended = {e.payload["tool_call_id"]: e.payload for e in result.events
             if e.event_type is EventType.WORKSPACE_COMMAND_ENDED}
    # Each call's outcome by its error class, from its ToolCalled event, never from its text
    # (DECISION-94bfee7c).
    error_types = {e.payload["tool_call_id"]: e.payload.get("error_type")
                   for e in result.events if e.event_type is EventType.TOOL_CALLED}

    print(f"run {result.run_id} on {provider_name}: {result.status.value}")
    for call_id, r in results.items():
        name, arguments = issued.get(call_id, ("?", {}))
        print(f"  {name} {json.dumps(arguments)[:80]} -> {'error: ' if r.is_error else ''}"
              f"{r.content[:90]!r}  [{r.provenance.trust_zone.value}]")

    def calls(name, first):
        return [call_id for call_id, (called, arguments) in issued.items()
                if called == name and (arguments.get("argv") or [None])[0] == first]

    ran_program = [c for c in calls("run_command", "python") if ended.get(c, {}).get("exit_code") == 0]
    reads = [call_id for call_id, (called, arguments) in issued.items()
             if called == "read_workspace_file" and arguments.get("path") == "out.txt"]
    refused = calls("run_command", "rm")
    slept = [c for c in calls("run_command", "sleep") if c in ended]
    untrusted = [r for call_id, r in results.items() if issued.get(call_id, ("",))[0] in agent(model_id).tool_profile
                 and error_types.get(call_id) not in ("ToolValidationError", "ToolApprovalRejected")]
    removed = [e.payload for e in result.events if e.event_type is EventType.WORKSPACE_REMOVED]
    checks = [
        ("every command ran only after the operator approved its call, and once",
         None if not started else (all((p["tool_call_id"], True) in decided for p in started)
                                   and len({p["tool_call_id"] for p in started}) == len(started))),
        ("every workspace result is untrusted, data-only, executable content",
         None if not untrusted else all(
             r.provenance.trust_zone is TrustZone.UNTRUSTED
             and r.provenance.instruction_authority is InstructionAuthority.DATA_ONLY
             and {TaintFlag.EXTERNAL_CONTENT, TaintFlag.EXECUTABLE_CONTENT} <= set(r.provenance.taint_flags)
             for r in untrusted)),
        ("the program the agent wrote ran, and the file it wrote was read back",
         None if not (ran_program and reads) else any(
             error_types.get(c) is None and results[c].content.strip() == "385" for c in reads)),
        ("the command the operator refused never ran, and the model was told by its error class",
         None if not refused else (all(error_types.get(c) == "ToolApprovalRejected" for c in refused)
                                   and not any(p["argv"][0] == "rm" for p in started))),
        ("a command past its timeout was killed, and the model read that as its result",
         None if not slept else all(ended[c]["killed"] == "timeout" and error_types.get(c) is None
                                    and "killed: timeout" in results[c].content for c in slept)),
        ("the workspace was removed when the run ended, before its last event",
         None if EventType.WORKSPACE_CREATED not in kinds else (
             len(removed) == 1 and removed[0]["reason"] == "run_ended"
             and kinds.index(EventType.WORKSPACE_REMOVED) < len(kinds) - 1)),
    ]
    if not live:
        checks.append(("the run completed", result.status is RunStatus.COMPLETED))

    # --- the second run: cancelled mid-command --------------------------------------------------------
    cancelled = await runner.start(agent(model_id), CANCEL_TASK,
                                   RunConfig(tenant_id=TENANT, project_id=PROJECT, max_turns=4))
    decided_too: list = []
    approving = asyncio.ensure_future(operator(runner, cancelled.run_id, decided_too))

    async def command_started() -> bool:
        async for event in cancelled.events():
            if event.event_type is EventType.WORKSPACE_COMMAND_STARTED:
                return True
        return False

    try:
        began_running = await asyncio.wait_for(command_started(), 120)
    except asyncio.TimeoutError:
        began_running = False
    finally:
        approving.cancel()
    if began_running:
        await asyncio.sleep(2.0)  # the command is under way: on Docker its container is running
    cancelled.cancel()
    asked = time.monotonic()
    stopped = await cancelled.result()
    waited = time.monotonic() - asked
    containers = _containers_of(stopped.run_id) if provider_name == "docker" else []
    owned = await chosen.list_owned({"agentsdk.run": stopped.run_id}) if chosen is not None else []
    order = [e.event_type for e in stopped.events]
    started_too = [e.payload for e in stopped.events if e.event_type is EventType.WORKSPACE_COMMAND_STARTED]
    ended_too = [e.payload for e in stopped.events if e.event_type is EventType.WORKSPACE_COMMAND_ENDED]
    print(f"run {stopped.run_id} cancelled mid-command: {stopped.status.value} {waited:.2f} s after the cancel")
    checks += [
        ("the cancelled run reported cancelled within 10 s of the cancel",
         None if not began_running else (stopped.status is RunStatus.CANCELLED and waited < CANCEL_BOUND)),
        ("its command was recorded killed as cancelled, before the workspace's removal and the run's last event",
         None if not began_running else (
             [(p["killed"], p["exit_code"], p["kill_error"]) for p in ended_too] == [("cancelled", None, None)]
             and order.index(EventType.WORKSPACE_COMMAND_ENDED) < order.index(EventType.WORKSPACE_REMOVED)
             < len(order) - 1)),
        ("no container or workspace of the cancelled run remained when it reported cancelled",
         None if not began_running or containers is None else (containers == [] and owned == [])),
    ]
    runs = [result.run_id, stopped.run_id]

    if persisted:
        import psycopg

        from agentsdk.config import normalise_database_url

        with psycopg.connect(normalise_database_url(os.environ["DATABASE_URL"])) as conn:
            rows = {run_id: [(list(argv), code, killed) for argv, code, killed in conn.execute(
                "SELECT argv, exit_code, killed FROM workspace_commands WHERE run_id = %s ORDER BY started_at",
                (run_id,)).fetchall()] for run_id in runs}
        print(f"  commands: {json.dumps(rows)}")
        expected = {
            result.run_id: [(p["argv"], ended[p["tool_call_id"]]["exit_code"], ended[p["tool_call_id"]]["killed"])
                            for p in started],
            stopped.run_id: [(p["argv"], None, "cancelled") for p in started_too]}
        checks.append(("each command is a row with its exit code and why it was killed",
                       None if not started else rows == expected))
    return checks, runs


def report(checks, live: bool) -> list[str]:
    failed = []
    for label, passed in checks:
        if passed is None:
            print(f"[{'SKIP' if live else 'FAIL'}] not exercised: {label}")
            if not live:
                failed.append(label)
            continue
        print(f"[{'PASS' if passed else 'FAIL'}] {label}")
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


async def offline(store: str = "memory", provider_name: str = "fake"):
    persistence = None
    if store == "postgres":
        from dotenv import load_dotenv

        from agentsdk import Persistence
        from agentsdk.config import normalise_database_url

        load_dotenv()
        persistence = Persistence.postgres(normalise_database_url(os.environ["DATABASE_URL"]), create_schema=False)
    chosen = provider(provider_name)
    runner = Runner({"scripted": ScriptedModel()}, workspace_provider=chosen, persistence=persistence)
    checks, _ = await demonstrate(runner, "scripted:model", live=False, persisted=store == "postgres",
                                  provider_name=provider_name, chosen=chosen)
    return [(label, passed is True) for label, passed in checks]


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
            docker = DockerProvider()
            runner = Runner({"m": client}, workspace_provider=docker, persistence=persistence)
            checks, runs = await demonstrate(runner, f"m:{settings.default_model}", live=True, persisted=True,
                                             provider_name="docker", chosen=docker)
            print(f"\nrun ids (to remove the demo's rows): {' '.join(runs)}")
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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="in memory, scripted model, FakeProvider")
    mode.add_argument("--scripted", action="store_true", help="in memory, scripted model, Docker")
    args = parser.parse_args()
    if not args.offline and (why := docker_ready()) is not None:
        print(f"[SKIP] {why}")
        raise SystemExit(0)
    if args.offline or args.scripted:
        checks = asyncio.run(offline(provider_name="fake" if args.offline else "docker"))
    else:
        checks = live()
    failed = report(checks, live=not (args.offline or args.scripted))
    if failed:
        raise SystemExit(f"failed: {failed}")
