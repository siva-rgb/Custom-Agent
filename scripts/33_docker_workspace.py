"""33 - A Docker workspace: example 32's agent, its commands in hardened containers.

`DockerProvider` gives a run a Docker volume as its workspace, and runs each command the agent
asks for in its own container over it (FR-142): no network, no capability, no new privileges, a
read-only root, user 65534, and the memory, CPU, process and /tmp limits of its `WorkspaceSpec`.
The image is pinned by digest and never pulled. Files cross only as tar streams the SDK checks
(FR-143), and the workspace's size is read after each command, so past `max_workspace_bytes` the
next command is refused by name (FR-144).

It shows the agent of example 32 at work -- a command's output, a non-zero exit, a command waiting
for an operator's approval, the disk bound refusing the next command -- and then, from inside a
command, the hardening in force, and a file exported from the workspace as a tar stream.

Run it
  python scripts/33_docker_workspace.py             # live: gateway + PostgreSQL + Docker
  python scripts/33_docker_workspace.py --offline   # in memory, scripted model, FakeProvider
  python scripts/33_docker_workspace.py --scripted  # in memory, scripted model, Docker

Live and --scripted need Docker running and the image below on the host:
  docker pull python:3.12-alpine@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import subprocess
import sys
import tarfile

from agentsdk import AgentSpec, Message, Role, RunConfig, RunStatus, Runner
from agentsdk.events import EventType
from agentsdk.identity import PrincipalContext
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.postgres import RunScope
from agentsdk.primitives import InstructionAuthority, TaintFlag, ToolCall, TrustZone
from agentsdk.workspace import DockerProvider, FakeProvider, WorkspaceSpec

TENANT, PROJECT = "example-tenant", "examples"
IMAGE = "python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df"
LIMIT = 6000  # max_workspace_bytes, small so the demo can pass it
GREETING = "hello from the spec"  # an env value: commands see it; no record does
OPERATOR = PrincipalContext(agent_principal="example-33", user_principal="operator")
TASK = ("In your workspace: write notes.txt saying 'the sandbox holds this', run ['wc', '-w', 'notes.txt'], "
        "run ['cat', 'missing.txt'], run ['dd', 'if=/dev/zero', 'of=big.bin', 'bs=8000', 'count=1'], "
        "run ['wc', '-w', 'notes.txt'] again, then answer in one sentence.")


# --- FakeProvider's stand-ins for the image's commands (--offline) ---------------------------------


async def wc(cmd) -> int:
    """`wc -w <file>`: the words the file holds, then its name."""
    cmd.out(f"{len(cmd.files.read(cmd.argv[2]).split())} {cmd.argv[2]}\n")
    return 0


async def cat(cmd) -> int:
    try:
        cmd.out(cmd.files.read(cmd.argv[1]))
    except FileNotFoundError:
        cmd.err(f"cat: can't open '{cmd.argv[1]}': No such file or directory\n")
        return 1
    return 0


async def dd(cmd) -> int:
    """`dd if=/dev/zero of=<file> bs=<n> count=<k>`: writes n*k zero bytes."""
    given = dict(arg.split("=", 1) for arg in cmd.argv[1:])
    cmd.files.write(given["of"], b"\0" * int(given["bs"]) * int(given["count"]))
    return 0


def provider(name: str):
    return DockerProvider() if name == "docker" else FakeProvider(commands={"wc": wc, "cat": cat, "dd": dd})


def agent(model_id: str) -> AgentSpec:
    return AgentSpec(
        id="workspace-user", instructions="Use your workspace tools exactly as asked.", preferred_model=model_id,
        tool_profile=("run_command", "read_workspace_file", "write_workspace_file", "list_workspace"),
        workspace=WorkspaceSpec(image=IMAGE, max_workspace_bytes=LIMIT, env={"GREETING": GREETING}))


class ScriptedModel:
    """Offline: does what the task says, one call a turn."""

    CALLS = [("write_workspace_file", {"path": "notes.txt", "content": "the sandbox holds this"}),
             ("run_command", {"argv": ["wc", "-w", "notes.txt"]}),
             ("run_command", {"argv": ["cat", "missing.txt"]}),
             ("run_command", {"argv": ["dd", "if=/dev/zero", "of=big.bin", "bs=8000", "count=1"]}),
             ("run_command", {"argv": ["wc", "-w", "notes.txt"]})]

    async def send(self, request):
        turn = sum(1 for m in request.messages if m.role is Role.ASSISTANT)
        if turn < len(self.CALLS):
            name, arguments = self.CALLS[turn]
            return ModelResponse(message=Message(role=Role.ASSISTANT, tool_calls=(
                ToolCall(id=f"t{turn}", name=name, arguments=arguments),)),
                stop_reason=StopReason.TOOL_CALLS, usage=Usage(20, 5, 25))
        return ModelResponse(message=Message(role=Role.ASSISTANT, content="notes.txt holds 4 words."),
                             stop_reason=StopReason.END_TURN, usage=Usage(20, 5, 25))


async def operator(runner: Runner, run_id: str, decided: list) -> None:
    """Approves each command the agent asks to run, once it has read the full argv."""
    while True:
        for view in await runner.interruptions(TENANT, PROJECT, run_id=run_id):
            argv = view.interruption.request_payload["arguments"]["argv"]
            print(f"  operator approves: {argv}")
            await runner.decide(TENANT, PROJECT, view.interruption.interruption_id, approve=True,
                                principal=OPERATOR)
            decided.append(view.interruption.tool_call_id)
        await asyncio.sleep(0.05)


async def demonstrate(runner: Runner, model_id: str, *, live: bool, persisted: bool, provider_name: str):
    handle = await runner.start(agent(model_id), TASK, RunConfig(tenant_id=TENANT, project_id=PROJECT, max_turns=10))
    decided: list = []
    approving = asyncio.ensure_future(operator(runner, handle.run_id, decided))
    try:
        result = await handle.result()
    finally:
        approving.cancel()
    sessions = runner._sessions_for(RunScope(run_id=result.run_id, tenant_id=TENANT, project_id=PROJECT))
    history = await asyncio.to_thread(sessions.history, result.run_id)
    issued = {c.id: (c.name, c.arguments) for m in history if m.role is Role.ASSISTANT for c in m.tool_calls}
    results = {r.tool_call_id: r for m in history for r in m.tool_results}
    kinds = [e.event_type for e in result.events]
    started = [e.payload for e in result.events if e.event_type is EventType.WORKSPACE_COMMAND_STARTED]
    ran = [p["argv"] for p in started]
    # Each call's outcome by its error class, from its ToolCalled event, never from its text
    # (DECISION-94bfee7c).
    error_types = {e.payload["tool_call_id"]: e.payload.get("error_type")
                   for e in result.events if e.event_type is EventType.TOOL_CALLED}

    print(f"run {result.run_id} on {provider_name}: {result.status.value}")
    for call_id, r in results.items():
        name, arguments = issued.get(call_id, ("?", {}))
        print(f"  {name} {json.dumps(arguments)} -> {'error: ' if r.is_error else ''}"
              f"{r.content[:100]!r}  [{r.provenance.trust_zone.value}]")

    untrusted = [r for call_id, r in results.items() if issued.get(call_id, ("",))[0] in agent(model_id).tool_profile
                 and error_types.get(call_id) not in ("ToolValidationError", "ToolApprovalRejected")]
    removed = [e.payload for e in result.events if e.event_type is EventType.WORKSPACE_REMOVED]
    checks = [
        # Whatever a live model does: nothing ran unapproved, every result is untrusted, and a
        # workspace that was created was removed before the run's last event.
        ("every command ran only after the operator approved its call, and once",
         None if not started else (all(p["tool_call_id"] in decided for p in started)
                                   and len({p["tool_call_id"] for p in started}) == len(started))),
        ("every workspace result is untrusted, data-only, executable content",
         None if not untrusted else all(
             r.provenance.trust_zone is TrustZone.UNTRUSTED
             and r.provenance.instruction_authority is InstructionAuthority.DATA_ONLY
             and {TaintFlag.EXTERNAL_CONTENT, TaintFlag.EXECUTABLE_CONTENT} <= set(r.provenance.taint_flags)
             for r in untrusted)),
        ("the workspace was removed when the run ended, before its last event",
         None if EventType.WORKSPACE_CREATED not in kinds else (
             len(removed) == 1 and removed[0]["reason"] == "run_ended"
             and kinds.index(EventType.WORKSPACE_REMOVED) < len(kinds) - 1)),
    ]
    if not live:
        refused = [e.payload for e in result.events if e.event_type is EventType.WORKSPACE_REFUSED]
        checks += [
            ("the run completed", result.status is RunStatus.COMPLETED),
            ("a command's output reached the model", "4 notes.txt" in results["t1"].content),
            ("a non-zero exit was the command's result, not a tool failure",
             error_types.get("t2") is None and "exit code: 1" in results["t2"].content),
            ("past max_workspace_bytes the next command was refused, the bound named in its event",
             error_types.get("t4") == "WorkspaceRefused" and [p["bound"] for p in refused] == ["max_workspace_bytes"]
             and ran == [c[1]["argv"] for c in ScriptedModel.CALLS[1:4]]),
        ]
    if persisted:
        import psycopg

        from agentsdk.config import normalise_database_url

        with psycopg.connect(normalise_database_url(os.environ["DATABASE_URL"])) as conn:
            spaces = conn.execute("SELECT provider, removed_reason FROM workspaces WHERE run_id = %s",
                                  (result.run_id,)).fetchall()
            commands = conn.execute("SELECT argv, exit_code FROM workspace_commands WHERE run_id = %s"
                                    " ORDER BY started_at", (result.run_id,)).fetchall()
            [[manifest]] = conn.execute("SELECT workspace FROM execution_manifests WHERE run_id = %s",
                                        (result.run_id,)).fetchall()
        print(f"  workspaces: {spaces}\n  commands: {commands}\n  manifest: {json.dumps(manifest)}")
        checks.append(("the workspace and each command that ran are rows; the manifest names the env, never its value",
                       None if EventType.WORKSPACE_CREATED not in kinds else (
                           spaces == [(provider_name, "run_ended")] and [list(argv) for argv, _ in commands] == ran
                           and manifest["env"] == ["GREETING"] and GREETING not in json.dumps(manifest))))
    return checks, [result.run_id]


# --- what a command sees from inside (Docker only) -------------------------------------------------

PROBE = ("id -u; id -g; grep -E '^(CapEff|NoNewPrivs):' /proc/self/status; echo net=$(ls /sys/class/net); "
         "touch /etc/x 2>/dev/null; echo root=$?; grep -c ' /tmp tmpfs .*size=65536k' /proc/mounts; "
         "cat /sys/fs/cgroup/memory.max /sys/fs/cgroup/memory.swap.max /sys/fs/cgroup/cpu.max "
         "/sys/fs/cgroup/pids.max; echo $GREETING > report.txt")


async def hardening(docker: DockerProvider) -> list:
    spec = agent("x").workspace
    handle = await docker.create(spec, {"agentsdk.tenant": TENANT, "agentsdk.project": PROJECT})
    try:
        outcome = await docker.run(handle, ["sh", "-c", PROBE], timeout=60)
        lines = outcome.stdout.decode().splitlines()
        print("  inside a command:", lines)
        with tarfile.open(fileobj=io.BytesIO(await docker.export(handle, "report.txt"))) as tar:
            [member] = tar.getmembers()
            exported = (member.name, member.isfile(), tar.extractfile(member).read())
        print(f"  exported: {exported[0]} ({len(exported[2])} B)")
    finally:
        await docker.remove(handle)
    left = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=agentsdk.workspace={handle.workspace_id}"],
                          capture_output=True, text=True).stdout + subprocess.run(
        ["docker", "volume", "ls", "-q", "--filter", f"label=agentsdk.workspace={handle.workspace_id}"],
        capture_output=True, text=True).stdout
    return [
        ("a command runs as 65534:65534 with no capability and no_new_privs set",
         lines[:4] == ["65534", "65534", "CapEff:\t0000000000000000", "NoNewPrivs:\t1"]),
        ("a command has no network interface but loopback (--network none)", lines[4:5] == ["net=lo"]),
        ("the root is read-only, and /tmp a tmpfs capped at tmp_bytes", lines[5:7] == ["root=1", "1"]),
        ("the cgroup's memory, swap, CPU and pid limits are the spec's",
         lines[7:11] == [str(spec.memory_bytes), "0", "100000 100000", str(spec.pids)]),
        ("a file the command wrote was exported as a regular file in a tar stream",
         exported == ("report.txt", True, (GREETING + "\n").encode())),
        ("no container or volume of the workspace remains once it is removed", left.split() == []),
    ]


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
                                  provider_name=provider_name)
    if provider_name == "docker":
        checks += await hardening(chosen)
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
                                             provider_name="docker")
            print(f"\nrun ids (to remove the demo's rows): {' '.join(runs)}")
            return checks + await hardening(docker)
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
