"""25 - The write tools: an agent changes files only as a person approves, inside its root.

Phase 4's first increment (FR-118, FR-119). An agent is given `write_file` and `edit_file`
over a temporary folder, both of which need approval by default. An approval screen,
scripted here, is shown each change -- the path, and the file's hash before and after --
and decides:

  * turn 1  edit plan.txt (Monday becomes Tuesday) and create notes/summary.txt: approved,
            and both are written, each in one atomic replace
  * turn 2  write through `escape`, a junction pointing outside the folder, and edit
            plan.txt where the text occurs twice: both refused before anyone is asked
  * turn 3  rewrite notes/summary.txt, which another writer changes after the screen
            showed it and before it is approved: refused, "changed since approval", and
            the other writer's text is kept

Checked on disk and against the store: every change made was approved, and matches the
hash the approver was shown; nothing outside the folder changed; no refusal wrote anything;
and no temporary file is left behind.

Run it
  python scripts/25_write_tools.py            # live: gateway + PostgreSQL (DATABASE_URL in .env)
  python scripts/25_write_tools.py --offline  # in memory, a scripted model

Windows only (the file tools are, D11), with Developer Mode on.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from agentsdk import AgentSpec, Message, Role, RunConfig, RunStatus, Runner
from agentsdk.builtin_tools import edit_file_tool, write_file_tool
from agentsdk.identity import PrincipalContext
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.postgres import RunScope
from agentsdk.primitives import ToolCall

TENANT, PROJECT = "example-tenant", "examples"
APPROVER = PrincipalContext(agent_principal="approval-screen", user_principal="example-reviewer")
LIVE_OUTPUT_TOKENS = 2000
OTHER_WRITER = "Summary, rewritten by someone else.\n"

TURNS = [
    [["edit_file", {"path": "plan.txt", "old": "Monday", "new": "Tuesday"}],
     ["write_file", {"path": "notes/summary.txt", "content": "We sail on Tuesday.\n"}]],
    [["write_file", {"path": "escape/secret.txt", "content": "overwritten"}],
     ["edit_file", {"path": "plan.txt", "old": "the", "new": "a"}]],
    [["write_file", {"path": "notes/summary.txt", "content": "Final summary.\n"}]],
]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def prepare(base: Path) -> tuple[Path, Path]:
    """The agent's folder, with a plan, a notes folder and a junction leading outside it."""
    root, outside = base / "root", base / "outside"
    root.mkdir()
    (root / "notes").mkdir()
    outside.mkdir()
    (root / "plan.txt").write_text("We sail on Monday, the day after the meeting.\n", encoding="utf-8")
    (outside / "secret.txt").write_text("outside the folder\n", encoding="utf-8")
    subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "escape"), str(outside)], check=True, capture_output=True)
    return root, outside


def described() -> str:
    lines = [f"Turn {i}: in one response, call " + "; ".join(
        f"{name} with exactly these arguments: {json.dumps(args)}" for name, args in calls)
        for i, calls in enumerate(TURNS, 1)]
    return ("Do exactly these turns, making every call of a turn in the same response and waiting for its results "
            "before the next turn. Do not repeat a call that fails or is refused. Then answer 'done'.\n"
            + "\n".join(lines))


class ScriptedModel:
    async def send(self, request):
        turn = sum(1 for m in request.messages if m.role is Role.ASSISTANT)
        if turn < len(TURNS):
            calls = tuple(ToolCall(id=f"t{turn}-{i}", name=name, arguments=args)
                          for i, (name, args) in enumerate(TURNS[turn]))
            return ModelResponse(message=Message(role=Role.ASSISTANT, tool_calls=calls),
                                 stop_reason=StopReason.TOOL_CALLS, usage=Usage(20, 5, 25))
        return ModelResponse(message=Message(role=Role.ASSISTANT, content="done"), stop_reason=StopReason.END_TURN,
                             usage=Usage(20, 5, 25))


async def approval_screen(runner: Runner, handle, root: Path, shown: dict) -> None:
    """Approves each change after reading what it would do. Before approving the final
    summary, another writer changes that file: the approval is then of a file that is no
    longer what the screen showed."""
    decided: set[str] = set()
    while not handle._control.task.done():
        for view in await runner.interruptions(TENANT, PROJECT, run_id=handle.run_id):
            item = view.interruption
            if item.interruption_id in decided:
                continue
            decided.add(item.interruption_id)
            shown[item.tool_call_id] = item.request_payload.get("describe")
            if item.request_payload["arguments"].get("content") == "Final summary.\n":
                (root / "notes" / "summary.txt").write_text(OTHER_WRITER, encoding="utf-8")
            try:
                await handle.approve(item.interruption_id, principal=APPROVER)
            except Exception:  # noqa: BLE001 - decided or expired meanwhile
                pass
        await asyncio.sleep(0.02)


async def demonstrate(runner: Runner, root: Path, outside: Path, model: str, live: bool, say=print):
    agent = AgentSpec(id="editor", instructions="You keep a small folder of plans and notes.", preferred_model=model,
                      tool_profile=("write_file", "edit_file"), max_output_tokens=LIVE_OUTPUT_TOKENS if live else None)
    handle = await runner.start(agent, described() if live else "go", RunConfig(
        tenant_id=TENANT, project_id=PROJECT, max_turns=8, max_output_tokens=LIVE_OUTPUT_TOKENS if live else None))
    shown: dict = {}
    await asyncio.gather(approval_screen(runner, handle, root, shown), handle.result())
    result = await handle.result()
    views = {v.interruption.tool_call_id: v
             for v in await runner.interruptions(TENANT, PROJECT, run_id=result.run_id, pending_only=False)}
    sessions = runner._sessions_for(RunScope(run_id=result.run_id, tenant_id=TENANT, project_id=PROJECT))
    history = await asyncio.to_thread(sessions.history, result.run_id)
    issued = [(c.id, c.name, dict(c.arguments)) for m in history if m.role is Role.ASSISTANT for c in m.tool_calls]
    returned = {r.tool_call_id: r for m in history for r in m.tool_results}

    say(f"run {result.run_id}: {result.status.value}")
    for cid, name, args in issued:
        view = views.get(cid)
        state = "refused before asking" if view is None else view.state
        say(f"  {name} {args.get('path')}: {state} -> {returned[cid].content[:110] if cid in returned else '?'}")

    plan = (root / "plan.txt").read_bytes()
    summary = (root / "notes" / "summary.txt")
    leftovers = [p.name for p in root.rglob("*") if ".agentsdk-" in p.name]
    # Each change that was written matches the hash its approver was shown, and every
    # written change had an approved interruption.
    written = [cid for cid, _, _ in issued if cid in returned and not returned[cid].is_error]
    matched = all(views.get(cid) is not None and views[cid].decision is not None
                  and views[cid].decision.decision == "approve" for cid in written)
    checks = [
        ("the run completed", result.status is RunStatus.COMPLETED),
        ("every change written was approved first", matched),
        ("nothing outside the folder changed",
         (outside / "secret.txt").read_text(encoding="utf-8") == "outside the folder\n"),
        ("no temporary file is left behind", leftovers == []),
    ]
    if not live:
        checks += [
            ("the approved edit and the new file match the hashes the approver was shown",
             sha(plan) == shown["t0-0"]["after"] and summary.exists()
             and shown["t0-1"]["before"] is None and shown["t0-1"]["after"] == sha(b"We sail on Tuesday.\n")),
            ("the junction escape and the ambiguous edit were refused before anyone was asked",
             "t1-0" not in views and "t1-1" not in views and returned["t1-0"].is_error and returned["t1-1"].is_error
             and "occurs more than once" in returned["t1-1"].content and b"Tuesday, the day" in plan),
            ("a file changed after the approver saw it was not written, and the other writer's text is kept",
             "changed since approval" in returned["t2-0"].content
             and summary.read_text(encoding="utf-8") == OTHER_WRITER),
        ]
    return checks, [result.run_id]


async def offline(store: str = "memory", quiet: bool = False):
    """The example with a scripted model, in memory or on Postgres; the gate test calls it."""
    persistence = None
    if store == "postgres":
        from agentsdk import Persistence
        from agentsdk.config import normalise_database_url

        persistence = Persistence.postgres(normalise_database_url(os.environ["DATABASE_URL"]), create_schema=False)
    with tempfile.TemporaryDirectory() as base:
        root, outside = prepare(Path(base))
        runner = Runner({"m": ScriptedModel()}, tools=[write_file_tool(root), edit_file_tool(root)],
                        persistence=persistence)
        return await demonstrate(runner, root, outside, "m:scripted", live=False,
                                 say=(lambda *a: None) if quiet else print)


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
            with tempfile.TemporaryDirectory() as base:
                root, outside = prepare(Path(base))
                runner = Runner({"m": client}, tools=[write_file_tool(root), edit_file_tool(root)],
                                persistence=persistence)
                checks, runs = await demonstrate(runner, root, outside, f"m:{settings.default_model}", live=True)
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
    checks = asyncio.run(offline())[0] if parser.parse_args().offline else live()
    for label, passed in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {label}")
    failed = [label for label, passed in checks if not passed]
    if failed:
        raise SystemExit(f"failed: {failed}")
