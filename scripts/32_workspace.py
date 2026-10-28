"""32 - A workspace: an agent runs commands somewhere other than the host.

An agent whose spec has a `WorkspaceSpec` is given a workspace the first time one of its
workspace tools needs it (FR-137, FR-138), and every command it runs there is recorded (FR-140).
Here the workspace is `FakeProvider`'s: in process, its commands Python functions this file
registers, so no host process and no model-written code ever runs. Example 33 runs the same
agent in a Docker container.

It shows a command's output reaching the model, a non-zero exit read as the command's result,
`run_command` waiting for an operator's approval (P5-D10), a workspace past `max_workspace_bytes`
refusing the next command by name, every workspace result labelled untrusted executable data
(NFR-42), and the workspace removed and recorded when its run ends.

Run it
  python scripts/32_workspace.py            # live: gateway + PostgreSQL
  python scripts/32_workspace.py --offline  # in memory, scripted model
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from agentsdk import AgentSpec, Message, Role, RunConfig, RunStatus, Runner
from agentsdk.events import EventType
from agentsdk.identity import PrincipalContext
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.postgres import RunScope
from agentsdk.primitives import InstructionAuthority, TaintFlag, ToolCall, TrustZone
from agentsdk.workspace import FakeProvider, WorkspaceSpec

TENANT, PROJECT = "example-tenant", "examples"
# A FakeProvider runs no image; a WorkspaceSpec still names one by digest (P5-D6).
IMAGE = "example/fake-workspace@sha256:" + "0" * 64
LIMIT = 4096  # max_workspace_bytes, small so the demo can pass it
GREETING = "hello from the spec"  # an env value: commands see it; no record does
OPERATOR = PrincipalContext(agent_principal="example-32", user_principal="operator")
TASK = ("In your workspace: write notes.txt saying 'the sandbox holds this', run ['count', 'notes.txt'], "
        "run ['count', 'missing.txt'], run ['fill', 'big.bin', '5000'], run ['count', 'notes.txt'] again, "
        "then answer in one sentence.")


async def count(cmd) -> int:
    """`count <file>`: prints how many words the file holds; exits 1 when there is no such file."""
    try:
        words = len(cmd.files.read(cmd.argv[1]).split())
    except (IndexError, FileNotFoundError):
        cmd.err(f"count: no such file: {cmd.argv[1:] or ''}\n")
        return 1
    cmd.out(f"{words} words\n")
    return 0


async def fill(cmd) -> int:
    """`fill <file> <bytes>`: writes that many bytes."""
    cmd.files.write(cmd.argv[1], b"x" * int(cmd.argv[2]))
    return 0


async def greet(cmd) -> int:
    """`greet`: prints the GREETING the spec gave the workspace."""
    cmd.out(cmd.env.get("GREETING", "") + "\n")
    return 0


def provider() -> FakeProvider:
    return FakeProvider(commands={"count": count, "fill": fill, "greet": greet})


def agent(model_id: str) -> AgentSpec:
    return AgentSpec(
        id="workspace-user", instructions="Use your workspace tools exactly as asked.", preferred_model=model_id,
        tool_profile=("run_command", "read_workspace_file", "write_workspace_file", "list_workspace"),
        workspace=WorkspaceSpec(image=IMAGE, max_workspace_bytes=LIMIT, env={"GREETING": GREETING}))


class ScriptedModel:
    """Offline: does what the task says, one call a turn."""

    CALLS = [("write_workspace_file", {"path": "notes.txt", "content": "the sandbox holds this"}),
             ("run_command", {"argv": ["count", "notes.txt"]}),
             ("run_command", {"argv": ["count", "missing.txt"]}),
             ("run_command", {"argv": ["fill", "big.bin", "5000"]}),
             ("run_command", {"argv": ["count", "notes.txt"]})]

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


async def demonstrate(runner: Runner, model_id: str, *, live: bool, persisted: bool):
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

    print(f"run {result.run_id}: {result.status.value}")
    for call_id, r in results.items():
        name, arguments = issued.get(call_id, ("?", {}))
        print(f"  {name} {json.dumps(arguments)} -> {'error: ' if r.is_error else ''}"
              f"{r.content[:100]!r}  [{r.provenance.trust_zone.value}]")

    untrusted = [r for call_id, r in results.items() if issued.get(call_id, ("",))[0] in agent(model_id).tool_profile
                 and not r.content.startswith(("ToolValidationError", "ToolApprovalRejected"))]
    removed = [e.payload for e in result.events if e.event_type is EventType.WORKSPACE_REMOVED]
    checks = [
        # Whatever a live model does: nothing ran unapproved, every result is untrusted, and a
        # workspace that was created was removed before the run's last event.
        # By call: each command that started is a call the operator approved, and no call ran twice.
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
        checks += [
            ("the run completed", result.status is RunStatus.COMPLETED),
            ("a command's output reached the model", "4 words" in results["t1"].content),
            ("a non-zero exit was the command's result, not a tool failure",
             not results["t2"].is_error and "exit code: 1" in results["t2"].content),
            ("past max_workspace_bytes the next command was refused, naming the bound",
             results["t4"].is_error and results["t4"].content.startswith("WorkspaceRefused")
             and "max_workspace_bytes" in results["t4"].content and ran == [c[1]["argv"] for c in ScriptedModel.CALLS[1:4]]),
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
                           spaces == [("fake", "run_ended")] and [list(argv) for argv, _ in commands] == ran
                           and manifest["env"] == ["GREETING"] and GREETING not in json.dumps(manifest))))
    return checks, [result.run_id]


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


async def offline(store: str = "memory"):
    persistence = None
    if store == "postgres":
        from dotenv import load_dotenv

        from agentsdk import Persistence
        from agentsdk.config import normalise_database_url

        load_dotenv()
        persistence = Persistence.postgres(normalise_database_url(os.environ["DATABASE_URL"]), create_schema=False)
    runner = Runner({"scripted": ScriptedModel()}, workspace_provider=provider(), persistence=persistence)
    checks, _ = await demonstrate(runner, "scripted:model", live=False, persisted=store == "postgres")
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
            runner = Runner({"m": client}, workspace_provider=provider(), persistence=persistence)
            checks, runs = await demonstrate(runner, f"m:{settings.default_model}", live=True, persisted=True)
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
    parser.add_argument("--offline", action="store_true", help="run in memory with a scripted model")
    offline_run = parser.parse_args().offline
    failed = report(asyncio.run(offline()) if offline_run else live(), live=not offline_run)
    if failed:
        raise SystemExit(f"failed: {failed}")
