"""26 - Approvals under contention: a plan of writers, each change waiting for a person.

Phase 4's first exit signal (P4-D3, FR-120, FR-121). An orchestrated plan runs seven writer
children at once in one folder, each calling `write_file` or `edit_file`, which need
approval. Their approvals are asked on the top-level run, and an approval screen -- scripted
here, deciding in a random order -- decides them from there:

  * a.txt and d.txt  approved
  * b.txt            rejected, with a reason the writer is told
  * base.txt         its first edit approved as sticky, so the writer's second edit -- which
                     needs the first's text -- is decided by that standing decision, with
                     nobody asked
  * c.txt            decided by two people at the same moment, one approving and one
                     rejecting: exactly one decision stands, and the other is refused
  * e.txt            two writes in one turn, both approved: the first lands, and the second
                     is refused because the file changed since its approver saw it -- so
                     the file shows which ran first
  * late.txt         left undecided: it expires, and the writer is told

Checked against the store, the disk and each execution as it starts: every call ran at most
once, only after an approve decision and never while undecided, in its writer's issue
order; exactly one of the two simultaneous decisions was recorded; every interruption has
exactly one decision, in the run's tenant and project; every rejection's reason reached its
writer; and the folder holds exactly what the approved calls wrote.

Run it
  python scripts/26_approval_contention.py            # live: gateway + PostgreSQL
  python scripts/26_approval_contention.py --offline  # in memory, scripted writers
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import random
import sys
import tempfile
import threading
from collections import Counter
from pathlib import Path

from agentsdk import (
    AgentSpec,
    BudgetPolicy,
    ContextPolicy,
    Message,
    Orchestrator,
    Role,
    RunStatus,
    Runner,
    SchedulerLimits,
)
from agentsdk.builtin_tools import edit_file_tool, write_file_tool
from agentsdk.identity import PrincipalContext
from agentsdk.interruptions import InterruptionAlreadyDecided
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.postgres import RunScope
from agentsdk.primitives import ToolCall
from agentsdk.tools import ApprovedCall, Tool

TENANT, PROJECT = "example-tenant", "examples"
APPROVERS = (PrincipalContext(agent_principal="approval-screen", user_principal="example-reviewer"),
             PrincipalContext(agent_principal="approval-screen", user_principal="second-reviewer"))
TIMEOUT = 3.0  # seconds before an undecided call expires: the late one does
LIVE_OUTPUT_TOKENS = 2000
BASE = "one, two, three\n"

WRITERS = {
    "approved-a": [[["write_file", {"path": "a.txt", "content": "A\n"}]]],
    "rejected-b": [[["write_file", {"path": "b.txt", "content": "B\n"}]]],
    # The second edit's text exists only once the first has run, so they do not commute.
    "sticky-base": [[["edit_file", {"path": "base.txt", "old": "one", "new": "1"}]],
                    [["edit_file", {"path": "base.txt", "old": "1, two", "new": "1, 2"}]]],
    "raced-c": [[["write_file", {"path": "c.txt", "content": "C\n"}]]],
    # One turn: run in issue order, the first lands and the second finds the file changed.
    "ordered-e": [[["write_file", {"path": "e.txt", "content": "first\n"}],
                   ["write_file", {"path": "e.txt", "content": "second\n"}]]],
    "late": [[["write_file", {"path": "late.txt", "content": "late\n"}]]],
    "approved-d": [[["write_file", {"path": "d.txt", "content": "D\n"}]]],
}


def described(turns: list) -> str:
    lines = [f"Turn {i}: in one response, call " + "; ".join(
        f"{name} with exactly these arguments: {json.dumps(args)}" for name, args in calls)
        for i, calls in enumerate(turns, 1)]
    return ("Do exactly these turns, waiting for each turn's results before the next. Do not repeat a call that "
            "is rejected, refused or expires. Then answer 'done'.\n" + "\n".join(lines))


def briefed(turns: list, live: bool) -> str:
    return described(turns) if live else json.dumps(turns)


class ScriptedWriter:
    """Offline: makes its briefing's turns, one a response, after a random delay."""

    def __init__(self, delay) -> None:
        self.delay = delay

    async def send(self, request):
        await self.delay()
        text = request.messages[0].content
        turns = json.loads(text[text.index("["):].split("\n")[0])
        turn = sum(1 for m in request.messages if m.role is Role.ASSISTANT)
        if turn < len(turns):
            calls = tuple(ToolCall(id=f"t{turn}-{i}", name=name, arguments=args) for i, (name, args) in enumerate(turns[turn]))
            return ModelResponse(message=Message(role=Role.ASSISTANT, tool_calls=calls),
                                 stop_reason=StopReason.TOOL_CALLS, usage=Usage(20, 5, 25))
        return ModelResponse(message=Message(role=Role.ASSISTANT, content="done"), stop_reason=StopReason.END_TURN,
                             usage=Usage(20, 5, 25))


class ScriptedPlanner:
    async def send(self, request):
        if not any(m.tool_results for m in request.messages):
            text = request.messages[0].content
            return ModelResponse(message=Message(role=Role.ASSISTANT, tool_calls=(
                ToolCall(id="plan", name="run_plan", arguments=json.loads(text[text.index("{"):])),)),
                stop_reason=StopReason.TOOL_CALLS, usage=Usage(20, 5, 25))
        return ModelResponse(message=Message(role=Role.ASSISTANT, content="The writers are done."),
                             stop_reason=StopReason.END_TURN, usage=Usage(20, 5, 25))


def counted(tool: Tool, executions: list, watch: dict) -> Tool:
    """The tool, noting each execution as it starts: its interruption, and whether that
    interruption was still undecided then. Counted at the tool rather than from the calls'
    results, so a call run twice, or before its decision, is seen (M32 round 1, C3)."""

    async def run(approved: ApprovedCall | None = None, **arguments):
        undecided = approved is None or approved.interruption_id in {
            view.interruption.interruption_id
            for view in await watch["runner"].interruptions(watch["tenant"], watch["project"], run_id=watch["run_id"])}
        executions.append((approved.interruption_id if approved else None, undecided))
        return await tool.fn(approved=approved, **arguments)

    return dataclasses.replace(tool, fn=run)


def writing_tools(root: Path, executions: list, watch: dict) -> list[Tool]:
    return [counted(write_file_tool(root), executions, watch), counted(edit_file_tool(root), executions, watch)]


# The paths the screen never decides, so they expire; and those it decides only after a
# delay, in seconds. The gate test changes them to show the signal fails when the screen
# is absent or late (M32 round 2, F1).
UNDECIDED = {"late.txt"}
DELAYED: dict[str, float] = {}


async def approval_screen(runner: Runner, handle, tenant: str, project: str, race: dict, rng: random.Random,
                          screen: dict) -> None:
    """Decides each pending call from the top-level run, by what it would write, in a
    random order: a turn's calls still run in their issue order. `screen` keeps what it
    meant to decide and every decision of its that failed, so a decision that never
    landed is a FAIL rather than silence (F1)."""
    intended, failed = screen.setdefault("intended", {}), screen.setdefault("failed", {})
    seen: set[str] = set()
    later: list[asyncio.Task] = []

    async def decide(interruption_id: str, path: str) -> None:
        await asyncio.sleep(DELAYED.get(path, 0))
        try:
            if path == "c.txt":
                race.update(await decide_twice(runner, tenant, project, interruption_id))
            elif path == "b.txt":
                intended[interruption_id] = "reject"
                await handle.reject(interruption_id, principal=APPROVERS[0], message="b is not to be written")
            else:
                intended[interruption_id] = "approve"
                await handle.approve(interruption_id, principal=APPROVERS[0], sticky=path == "base.txt")
        except Exception as exc:  # noqa: BLE001 - kept, and judged by the checks
            failed[interruption_id] = type(exc).__name__

    while not handle._control.task.done():
        views = await runner.interruptions(tenant, project, run_id=handle.run_id)
        rng.shuffle(views)
        for view in views:
            item = view.interruption
            path = item.request_payload["arguments"].get("path")
            if item.interruption_id in seen or path in UNDECIDED:
                continue
            seen.add(item.interruption_id)
            if path in DELAYED:
                later.append(asyncio.create_task(decide(item.interruption_id, path)))
            else:
                await decide(item.interruption_id, path)
        await asyncio.sleep(0.02)
    await asyncio.gather(*later)


async def decide_twice(runner: Runner, tenant: str, project: str, interruption_id: str) -> dict:
    """Two people decide one call at the same moment, each from their own thread and loop."""
    barrier, outcomes = threading.Barrier(2), {}

    def person(index: int) -> None:
        barrier.wait()
        try:
            asyncio.run(runner.decide(tenant, project, interruption_id, approve=index == 0,
                                      principal=APPROVERS[index]))
            outcomes[index] = "recorded"
        except InterruptionAlreadyDecided:
            outcomes[index] = "refused"
        except Exception as exc:  # noqa: BLE001
            outcomes[index] = type(exc).__name__

    threads = [threading.Thread(target=person, args=(i,)) for i in (0, 1)]
    for thread in threads:
        thread.start()
    await asyncio.to_thread(lambda: [thread.join() for thread in threads])
    return {"outcomes": sorted(outcomes.values()), "interruption": interruption_id}


async def demonstrate(runner: Runner, orchestrator: Orchestrator, root: Path, *, planner: str, live: bool,
                      rng: random.Random, executions: list, watch: dict, tenant: str = TENANT,
                      project: str = PROJECT, say=print):
    order = list(WRITERS)
    rng.shuffle(order)  # the writers' start order
    nodes = [{"node_id": name, "assigned_role": "writer", "dependencies": [],
              "objective": briefed(WRITERS[name], live)} for name in order]
    lead = "Call run_plan once with exactly this document, then answer in one sentence.\n" if live else ""
    handle = await runner.start(orchestrator.agent, lead + json.dumps({"nodes": nodes}), orchestrator.config(
        tenant_id=tenant, project_id=project, model_override=planner, max_turns=6, approval_timeout=TIMEOUT,
        max_output_tokens=LIVE_OUTPUT_TOKENS if live else None))
    watch.update(runner=runner, tenant=tenant, project=project, run_id=handle.run_id)
    race: dict = {}
    screen: dict = {}
    await asyncio.gather(approval_screen(runner, handle, tenant, project, race, rng, screen), handle.result())
    result = await handle.result()
    views = await runner.interruptions(tenant, project, run_id=result.run_id, pending_only=False)

    # Each writer's own record: the calls it issued, in order, and what each returned.
    calls, returned = [], {}
    for child in sorted({v.interruption.run_id for v in views}):
        sessions = runner._sessions_for(RunScope(run_id=child, tenant_id=tenant, project_id=project))
        history = await asyncio.to_thread(sessions.history, child)
        for m in history:
            if m.role is Role.ASSISTANT:
                calls += [(child, c.id, c.name, dict(c.arguments)) for c in m.tool_calls]
            for r in m.tool_results:
                returned[(child, r.tool_call_id)] = r
    by_call = {(v.interruption.run_id, v.interruption.tool_call_id): v for v in views}
    files = {p.name: p.read_text(encoding="utf-8") for p in root.iterdir() if p.is_file()}

    say(f"plan run {result.run_id}: {result.status.value}")
    for child, cid, name, args in calls:
        view = by_call.get((child, cid))
        state = "no approval" if view is None else f"{view.state} ({view.decision.source})" if view.decision else view.state
        say(f"  {args.get('path')} {name}: {state} -> {returned[(child, cid)].content[:70] if (child, cid) in returned else '?'}")
    if race:
        say(f"  the simultaneous decisions on c.txt: {race['outcomes']}")
    say(f"  folder: {json.dumps(files)}")

    # Every lookup below is guarded: a view with no decision -- a call run undecided, the
    # failure this signal exists to catch -- is a FAIL, never an exception (C6).
    by_id = {v.interruption.interruption_id: v for v in views}

    def decided(interruption_id, decision=None):
        view = by_id.get(interruption_id)
        return (view is not None and view.decision is not None
                and (decision is None or view.decision.decision == decision))

    ran = Counter(interruption_id for interruption_id, _ in executions)
    issued = {(child, cid): index for index, (child, cid, _, _) in enumerate(calls)}
    executed = [(by_id[i].interruption.run_id, by_id[i].interruption.tool_call_id) for i, _ in executions if i in by_id]
    written_ok = [(child, cid) for child, cid, _, _ in calls
                  if (child, cid) in returned and not returned[(child, cid)].is_error]
    rejections = [v for v in views if decided(v.interruption.interruption_id, "reject")
                  and v.decision.source == "principal"]
    raced = by_id.get(race.get("interruption"))
    writers = {child for child, _ in executed}
    # Every check below has a witness: something that must have happened for the check to
    # mean anything. A check over an empty list passes having tested nothing (F1).
    checks = [
        ("the plan completed", result.status is RunStatus.COMPLETED),
        ("every call ran at most once, only after an approve decision, never while undecided",
         bool(executions) and all(count == 1 for count in ran.values())
         and all(decided(i, "approve") for i in ran) and not any(undecided for _, undecided in executions)
         and all(key in by_call and by_call[key].interruption.interruption_id in ran for key in written_ok)),
        ("every writer's calls ran in its issue order, and some writer ran more than one",
         len(executed) == len(executions)
         and any(sum(1 for key in executed if key[0] == child) > 1 for child in writers) and all(
             [issued[key] for key in executed if key[0] == child]
             == sorted(issued[key] for key in executed if key[0] == child) for child in writers)),
        ("every decision the approval screen made was recorded as it made it",
         bool(screen["intended"]) and all(
             decided(i, d) and (by_id[i].decision.source == "principal"
                                or (by_id[i].decision.source == "standing"
                                    and screen["failed"].get(i) == "InterruptionAlreadyDecided"))
             for i, d in screen["intended"].items())
         and set(screen["failed"]) <= set(screen["intended"])),
        ("exactly one of the two simultaneous decisions was recorded, and the other refused",
         race.get("outcomes") == ["recorded", "refused"] and raced is not None
         and raced.interruption.request_payload["arguments"].get("path") == "c.txt"
         and decided(raced.interruption.interruption_id) and raced.decision.principal in APPROVERS),
        ("every interruption has exactly one decision, in the run's tenant and project",
         bool(views) and all(decided(v.interruption.interruption_id) and (v.decision.tenant_id, v.decision.project_id)
                             == (tenant, project) for v in views)),
        # Witnessed by a rejection with a reason: the race's losing reject has none.
        ("a person rejected a call with a reason, and every rejection's reason reached its writer",
         any(v.decision.message for v in rejections) and all(getattr(returned.get((v.interruption.run_id, v.interruption.tool_call_id)), "content", None)
             == (f"rejected: {v.decision.message}" if v.decision.message else "rejected") for v in rejections)),
    ]
    if not live:
        def argument(view, name):
            return view.interruption.request_payload["arguments"].get(name)

        c_won = any(argument(v, "path") == "c.txt" and decided(v.interruption.interruption_id, "approve")
                    for v in views)
        expected = {"base.txt": "1, 2, three\n", "a.txt": "A\n", "d.txt": "D\n", "e.txt": "first\n",
                    **({"c.txt": "C\n"} if c_won else {})}
        sources = {argument(v, "old") or argument(v, "content") or argument(v, "path"):
                   v.decision.source if v.decision is not None else None for v in views}
        second = [returned.get(key) for key, view in by_call.items() if argument(view, "content") == "second\n"]
        checks += [
            ("the folder holds exactly what the approved calls wrote",
             files == expected),
            ("base.txt's edits ran in turn order, the second needing the first's text",
             files.get("base.txt") == "1, 2, three\n"),
            ("e.txt's two writes in one turn ran in issue order: the second found the file changed",
             files.get("e.txt") == "first\n" and len(second) == 1 and second[0] is not None
             and "changed since approval" in second[0].content),
            ("the sticky approval decided the writer's second edit, a person rejected b.txt, and the late call expired",
             sources.get("one") == "principal" and sources.get("1, two") == "standing"
             and sources.get("B\n") == "principal" and sources.get("late\n") == "expiry"),
        ]
    return checks, [result.run_id]


def orchestrator_for(writer_model: str, live: bool = False) -> Orchestrator:
    writer = AgentSpec(id="writer", instructions="You write files exactly as told.", preferred_model=writer_model,
                       tool_profile=("write_file", "edit_file"), max_output_tokens=LIVE_OUTPUT_TOKENS if live else None)
    return Orchestrator(roles={"writer": writer}, policy=BudgetPolicy(run_ceiling_tokens=2_000_000),
                        limits=SchedulerLimits(max_concurrent_subagents=len(WRITERS) + 2),
                        context_policy=ContextPolicy(compact_at=None))


async def offline(store: str = "memory", seed: int | None = None, quiet: bool = False, tenant: str = TENANT,
                  project: str = PROJECT):
    """The exit signal with scripted writers, in memory or on Postgres; the gate test calls it."""
    rng = random.Random(seed)

    async def delay():
        await asyncio.sleep(rng.random() * 0.01)

    persistence = None
    if store == "postgres":
        from agentsdk import Persistence
        from agentsdk.config import normalise_database_url

        persistence = Persistence.postgres(normalise_database_url(os.environ["DATABASE_URL"]), create_schema=False)
    with tempfile.TemporaryDirectory() as base:
        root = Path(base)
        (root / "base.txt").write_text(BASE, encoding="utf-8")
        orchestrator = orchestrator_for("w:writer")
        executions, watch = [], {}
        runner = Runner({"planner": ScriptedPlanner(), "w": ScriptedWriter(delay)},
                        tools=[orchestrator.tool, *writing_tools(root, executions, watch)], persistence=persistence)
        return await demonstrate(runner, orchestrator, root, planner="planner:p", live=False, rng=rng,
                                 executions=executions, watch=watch, tenant=tenant, project=project,
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
                root = Path(base)
                (root / "base.txt").write_text(BASE, encoding="utf-8")
                model = f"m:{settings.default_model}"
                orchestrator = orchestrator_for(model, live=True)
                executions, watch = [], {}
                runner = Runner({"m": client}, tools=[orchestrator.tool, *writing_tools(root, executions, watch)],
                                persistence=persistence)
                checks, runs = await demonstrate(runner, orchestrator, root, planner=model, live=True,
                                                 rng=random.Random(), executions=executions, watch=watch)
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
    parser.add_argument("--offline", action="store_true", help="run in memory with scripted writers")
    checks = asyncio.run(offline())[0] if parser.parse_args().offline else live()
    for label, passed in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {label}")
    failed = [label for label, passed in checks if not passed]
    if failed:
        raise SystemExit(f"failed: {failed}")
