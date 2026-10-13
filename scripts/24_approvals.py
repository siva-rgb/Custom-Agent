"""24 - Approvals: a call that needs a person's decision waits for it, in issue order.

Phase 4's first increment (FR-113 to FR-117). One agent works through four turns of tool
calls. `write_note` needs approval by its declared policy; `publish` needs it only when the
audience is public (a predicate over its arguments); `read_clock` needs none. A scripted
approver -- an application's approval screen -- reads the pending interruptions from the
store and decides:

  * turn 1  write a (approved), read the clock (free), write b (rejected, with a reason):
            the free call waits behind the first pending one, and all three keep the
            order the model gave them
  * turn 2  write c, approved as sticky: the agent's later write_note calls are decided
            by that standing decision, with nobody asked
  * turn 3  write d (decided standing), publish to the public (still asked: a predicate
            flagged it, which a sticky decision never covers), approved
  * turn 4  another public publish, which nobody decides: it expires and the model is
            told so

Checked against the store: every call ran at most once and only when approved, in issue
order; every decision names its source, and its principal where a person decided; the
rejection's reason reached the model; and the model was never called while a call waited.

Run it
  python scripts/24_approvals.py            # live: gateway + PostgreSQL (DATABASE_URL in .env)
  python scripts/24_approvals.py --offline  # in memory, a scripted model

Live, the model is real, so which calls it makes are its own; the checks are the ones
that must hold whatever it does.
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
from agentsdk.primitives import ToolCall
from agentsdk.tools import ApprovalPolicy, Tool, ToolSpec

TENANT, PROJECT = "example-tenant", "examples"
APPROVER = PrincipalContext(agent_principal="approval-screen", user_principal="example-reviewer")
TIMEOUT = 2.0  # seconds a call waits before it expires; short, so the last one does
LIVE_OUTPUT_TOKENS = 2000

TURNS = [
    [["write_note", {"title": "a", "text": "Plan the launch."}], ["read_clock", {}],
     ["write_note", {"title": "b", "text": "Delete the archive."}]],
    [["write_note", {"title": "c", "text": "Book the venue."}]],
    [["write_note", {"title": "d", "text": "Order the cake."}],
     ["publish", {"audience": "public", "topic": "launch"}]],
    [["publish", {"audience": "public", "topic": "late"}]],
]


class Desk:
    """The tools' world: notes written and posts published, in the order they happened."""

    def __init__(self) -> None:
        self.ran: list[tuple[str, dict]] = []

    def tools(self) -> list[Tool]:
        def write_note(title: str, text: str) -> str:
            self.ran.append(("write_note", {"title": title, "text": text}))
            return f"note {title} written"

        def read_clock() -> str:
            self.ran.append(("read_clock", {}))
            return "it is 09:00"

        def publish(audience: str, topic: str) -> str:
            self.ran.append(("publish", {"audience": audience, "topic": topic}))
            return f"published {topic} to {audience}"

        def spec(name, properties, policy):
            return ToolSpec(name=name, description=name, approval_policy=policy, input_schema={
                "type": "object", "properties": {k: {"type": "string"} for k in properties},
                "required": list(properties), "additionalProperties": False})

        return [
            Tool(spec=spec("write_note", ("title", "text"), ApprovalPolicy.REQUIRE_APPROVAL), fn=write_note),
            Tool(spec=spec("read_clock", (), ApprovalPolicy.AUTO), fn=read_clock),
            Tool(spec=spec("publish", ("audience", "topic"), ApprovalPolicy.AUTO), fn=publish,
                 needs_approval=lambda arguments: arguments.get("audience") == "public"),
        ]


AGENT_TOOLS = ("write_note", "read_clock", "publish")


def agent(model: str, live: bool) -> AgentSpec:
    return AgentSpec(id="assistant", instructions="You are an assistant that keeps notes and publishes posts.",
                     preferred_model=model, tool_profile=AGENT_TOOLS,
                     max_output_tokens=LIVE_OUTPUT_TOKENS if live else None)


def described() -> str:
    lines = [f"Turn {i}: in one response, call " + "; ".join(
        f"{name} with exactly these arguments: {json.dumps(args)}" for name, args in calls)
        for i, calls in enumerate(TURNS, 1)]
    return ("Do exactly these turns, making every call of a turn in the same response and waiting for its results "
            "before the next turn. If a call is rejected or expires, do not repeat it. Then answer 'done'.\n"
            + "\n".join(lines))


class ScriptedModel:
    """Offline: makes each turn's calls in one response, then answers."""

    async def send(self, request):
        turn = sum(1 for m in request.messages if m.role is Role.ASSISTANT)
        if turn < len(TURNS):
            calls = tuple(ToolCall(id=f"t{turn}-{i}", name=name, arguments=args)
                          for i, (name, args) in enumerate(TURNS[turn]))
            return ModelResponse(message=Message(role=Role.ASSISTANT, tool_calls=calls),
                                 stop_reason=StopReason.TOOL_CALLS, usage=Usage(20, 5, 25))
        return ModelResponse(message=Message(role=Role.ASSISTANT, content="done"), stop_reason=StopReason.END_TURN,
                             usage=Usage(20, 5, 25))


async def approve_as_scripted(runner: Runner, handle) -> None:
    """The approval screen: decide each pending call by what it would do, until the run ends."""
    decided: set[str] = set()
    while not handle._control.task.done():
        for view in await runner.interruptions(TENANT, PROJECT, run_id=handle.run_id):
            item = view.interruption
            if item.interruption_id in decided:
                continue
            arguments = item.request_payload.get("arguments", {})
            topic, title = arguments.get("topic"), arguments.get("title")
            if topic == "late":
                continue  # nobody looks at this one: it expires
            decided.add(item.interruption_id)
            try:
                if title == "b":
                    await handle.reject(item.interruption_id, principal=APPROVER,
                                        message="the archive must be kept for audit")
                else:
                    await handle.approve(item.interruption_id, principal=APPROVER, sticky=title == "c")
            except Exception:  # noqa: BLE001 - decided meanwhile (a standing decision), or expired
                pass
        await asyncio.sleep(0.02)


async def demonstrate(runner: Runner, desk: Desk, model: str, live: bool, say=print):
    task = described() if live else "go"
    handle = await runner.start(agent(model, live), task, RunConfig(
        tenant_id=TENANT, project_id=PROJECT, max_turns=8, approval_timeout=TIMEOUT,
        max_output_tokens=LIVE_OUTPUT_TOKENS if live else None))
    await asyncio.gather(approve_as_scripted(runner, handle), handle.result())
    result = await handle.result()
    views = await runner.interruptions(TENANT, PROJECT, run_id=result.run_id, pending_only=False)
    by_call = {v.interruption.tool_call_id: v for v in views}
    sessions = runner._sessions_for(RunScope(run_id=result.run_id, tenant_id=TENANT, project_id=PROJECT))
    history = await asyncio.to_thread(sessions.history, result.run_id)
    issued = [(c.id, c.name, dict(c.arguments)) for m in history if m.role is Role.ASSISTANT for c in m.tool_calls]
    returned = {r.tool_call_id: r for m in history for r in m.tool_results}

    say(f"run {result.run_id}: {result.status.value}")
    for cid, name, args in issued:
        view = by_call.get(cid)
        verdict = "no approval needed" if view is None else (
            f"{view.state} ({view.decision.source})" if view.decision else view.state)
        say(f"  {name} {json.dumps(args)}: {verdict} -> {returned[cid].content if cid in returned else '?'}")

    def approved(cid):
        view = by_call.get(cid)
        return view is None or (view.decision is not None and view.decision.decision == "approve")

    ran_keys = [(name, json.dumps(args, sort_keys=True)) for name, args in desk.ran]
    expected = [(name, json.dumps(args, sort_keys=True)) for cid, name, args in issued if approved(cid)]
    # Between each ApprovalRequested and the ApprovalDecided of the same interruption, no
    # model call was made.
    stream = list(result.events)
    quiet = True
    for i, event in enumerate(stream):
        if event.event_type is EventType.APPROVAL_REQUESTED:
            ends = [j for j in range(i, len(stream)) if stream[j].event_type is EventType.APPROVAL_DECIDED
                    and stream[j].payload["interruption"] == event.payload["interruption"]]
            quiet &= bool(ends) and not any(e.event_type is EventType.MODEL_CALLED for e in stream[i:ends[0]])
    rejections = [v for v in views if v.decision is not None and v.decision.decision == "reject"
                  and v.decision.source == "principal"]
    checks = [
        ("the run completed", result.status is RunStatus.COMPLETED),
        ("every call ran at most once, only when approved, and in the order the model issued it",
         ran_keys == expected),
        ("every decision names its source, and its principal where a person decided",
         all(v.decision is not None and (v.decision.source != "principal" or v.decision.principal is not None)
             for v in views)),
        ("every rejection's reason reached the model",
         all(returned[v.interruption.tool_call_id].content == f"rejected: {v.decision.message}" for v in rejections)),
        ("the model was never called while a call was waiting", quiet),
    ]
    if not live:
        sources = {cid: (v.decision.source, v.decision.decision) for cid, v in by_call.items()}
        checks += [
            ("write a approved, write b rejected with its reason, the clock read in between",
             sources.get("t0-0") == ("principal", "approve") and sources.get("t0-2") == ("principal", "reject")
             and [name for name, _ in desk.ran[:2]] == ["write_note", "read_clock"]),
            ("write c's sticky approval decided write d, and nobody was asked",
             sources.get("t1-0") == ("principal", "approve") and sources.get("t2-0") == ("standing", "approve")
             and not any(e.payload.get("tool_call_id") == "t2-0" for e in result.events
                         if e.event_type is EventType.APPROVAL_REQUESTED)),
            ("the public publish was still asked about, and the late one expired",
             sources.get("t2-1") == ("principal", "approve") and sources.get("t3-0") == ("expiry", "reject")
             and returned["t3-0"].content == "approval expired"),
        ]
    return checks, [result.run_id]


async def offline(store: str = "memory", quiet: bool = False):
    """The example with a scripted model, in memory or on Postgres; the gate test calls it."""
    persistence = None
    if store == "postgres":
        from agentsdk import Persistence
        from agentsdk.config import normalise_database_url

        persistence = Persistence.postgres(normalise_database_url(os.environ["DATABASE_URL"]), create_schema=False)
    desk = Desk()
    runner = Runner({"m": ScriptedModel()}, tools=desk.tools(), persistence=persistence)
    return await demonstrate(runner, desk, "m:scripted", live=False, say=(lambda *a: None) if quiet else print)


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
            desk = Desk()
            runner = Runner({"m": client}, tools=desk.tools(), persistence=persistence)
            checks, runs = await demonstrate(runner, desk, f"m:{settings.default_model}", live=True)
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
