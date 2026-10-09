"""17 - The orchestrator: run a plan, fail a node's check, replan, and keep what was done.

What it shows
  * an orchestrator is one run whose agent has a run_plan tool: the plan it submits is
    stored as a PlanVersion and its four nodes run as child runs of it (FR-73)
  * a node is done only when its acceptance criteria hold: here "check" names a tool
    that must succeed, run with that node's own permissions -- and the first plan names
    one the node may not run (FR-74)
  * the failure replans inside the same run: a second PlanVersion whose parent_plan is
    the first, and the side-effecting node that already ran is carried as done, never
    run again (FR-75)

The plans are scripted, so the replan happens every time; in the live run the children
answer with the gateway's model and everything is stored in PostgreSQL.

Run it
  python scripts/17_orchestrator.py            # live: gateway + PostgreSQL (DATABASE_URL in .env)
  python scripts/17_orchestrator.py --offline  # in memory, a scripted model: no database, no network
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from agentsdk import AgentSpec, BudgetPolicy, ContextPolicy, Message, Role, RunStatus, Runner
from agentsdk.events import EventType
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.orchestrator import PLAN_TOOL, Orchestrator
from agentsdk.primitives import ToolCall
from agentsdk.tools import Tool, ToolSpec

TENANT, PROJECT = "example-tenant", "examples"
NOTES = []


async def save_note(text: str) -> str:
    """The side effect: a note that must be written once, whatever is replanned."""
    NOTES.append(text)
    return f"saved note {len(NOTES)}"


async def gate_closed() -> str:
    raise RuntimeError("the release gate is closed")


async def gate_open() -> str:
    return "the release gate is open"


NO_ARGUMENTS = {"type": "object"}
TOOLS = [
    Tool(spec=ToolSpec(name="save_note", description="Save a note.", input_schema={
        "type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}), fn=save_note),
    Tool(spec=ToolSpec(name="gate_closed", description="Check the gate.", input_schema=NO_ARGUMENTS), fn=gate_closed),
    Tool(spec=ToolSpec(name="gate_open", description="Check the gate.", input_schema=NO_ARGUMENTS), fn=gate_open),
]


def the_plan(gate):
    return {"nodes": [
        {"node_id": "fetch", "objective": "Save a note that the release was requested.", "assigned_role": "writer"},
        {"node_id": "draft", "objective": "Draft one sentence announcing the release.", "assigned_role": "worker",
         "dependencies": ["fetch"], "side_effecting": False},
        {"node_id": "check", "objective": "Reply in one sentence that the draft can go out.", "assigned_role": "worker",
         "dependencies": ["draft"], "acceptance_criteria": [{"kind": "tool_succeeds", "target": gate}]},
        {"node_id": "publish", "objective": "Say the release is published.", "assigned_role": "worker",
         "dependencies": ["check"]},
    ]}


class ScriptedPlanner:
    """Submits the first plan, then the corrected one, then answers."""

    plans = [the_plan("gate_closed"), the_plan("gate_open")]

    async def send(self, request):
        shown = [m.tool_results[0] for m in request.messages if m.role is Role.TOOL]
        for result in shown[len(getattr(self, "printed", [])):]:
            report = json.loads(result.content)
            print(f"version {report['version']}: {report['status']}")
            for item in report["nodes"]:
                print(f"  {item['node_id']:8} {item['status']:9} {item.get('reason', '')}")
        self.printed = shown
        if len(shown) < len(self.plans):
            call = ToolCall(id=f"plan-{len(shown)}", name=PLAN_TOOL, arguments=self.plans[len(shown)])
            return ModelResponse(message=Message(role=Role.ASSISTANT, content="", tool_calls=(call,)),
                                 stop_reason=StopReason.TOOL_CALLS, usage=Usage(30, 10, 40))
        return ModelResponse(message=Message(role=Role.ASSISTANT, content="The release is published."),
                             stop_reason=StopReason.END_TURN, usage=Usage(30, 10, 40))


class ScriptedWorker:
    """Offline children: the first node saves its note through the tool, the rest answer."""

    async def send(self, request):
        objective = request.messages[0].content.split("\n")[0]
        if objective.startswith("Save a note") and not any(m.role is Role.TOOL for m in request.messages):
            call = ToolCall(id="note", name="save_note", arguments={"text": "release requested"})
            return ModelResponse(message=Message(role=Role.ASSISTANT, content="", tool_calls=(call,)),
                                 stop_reason=StopReason.TOOL_CALLS, usage=Usage(20, 5, 25))
        return ModelResponse(message=Message(role=Role.ASSISTANT, content=f"Done: {objective}"),
                             stop_reason=StopReason.END_TURN, usage=Usage(20, 5, 25))


async def demonstrate(runner, orchestrator, planner_key):
    result = await runner.run(
        orchestrator.agent, "Release the new version.",
        orchestrator.config(tenant_id=TENANT, project_id=PROJECT, model_override=planner_key, max_turns=6),
    )
    versions = orchestrator.plans(result.run_id)
    print(f"orchestrator run {result.run_id}: {result.status.value}")
    for version in versions:
        print(f"plan {version.plan_id} version {version.version} parent_plan={version.parent_plan}")
    started = [e.task_id for e in result.events if e.event_type is EventType.PLAN_NODE_STARTED]
    carried = [e.task_id for e in result.events if e.event_type is EventType.PLAN_NODE_FINISHED
               and str(e.payload.get("reason", "")).startswith("carried")]
    print(f"nodes started: {started}")
    print(f"carried into version 2 without running: {carried}")
    # Only the writer role may call save_note, so every note comes from fetch's one child run.
    print(f"save_note calls, all from fetch's one child run: {len(NOTES)}")
    first_check = [e.payload.get("reason", "") for e in result.events
                   if e.event_type is EventType.PLAN_NODE_FINISHED and e.task_id == "check"]
    return [
        ("the first plan's check failed its acceptance criterion",
         bool(first_check) and first_check[0].startswith("acceptance_criterion_failed")),
        ("the replan is a second version whose parent is the first",
         len(versions) == 2 and versions[1].parent_plan == (versions[0].plan_id, 1)),
        ("the side-effecting node ran once and was carried, not rerun",
         started.count("fetch") == 1 and "fetch" in carried),
        ("the run completed with every node of its last plan done",
         result.status is RunStatus.COMPLETED and started.count("publish") == 1),
    ]


def an_orchestrator(worker_model):
    instructions = "Do the objective in one short sentence."
    return Orchestrator(
        roles={
            "writer": AgentSpec(id="writer", instructions=instructions, preferred_model=worker_model,
                                tool_profile=("save_note",)),
            # A criterion's tool runs with its node's own permissions (FR-74): this role may run
            # gate_open and not gate_closed, so the first plan's check fails however the model
            # behaves. Since M20 the child is sent only gate_open's schema (FR-77); the
            # instruction still says the orchestrator checks the gate, not the child.
            "worker": AgentSpec(id="worker", instructions=instructions + " Do not call any tool.",
                                preferred_model=worker_model, tool_profile=("gate_open",)),
        },
        policy=BudgetPolicy(run_ceiling_tokens=200_000),
        # The scripted offline model is in no ModelRegistry, so the policy names the window
        # it compacts against (FR-78, DECISION-468e2bfa).
        context_policy=ContextPolicy(context_window=128_000),
    )


async def offline():
    orchestrator = an_orchestrator("worker:scripted")
    runner = Runner({"planner": ScriptedPlanner(), "worker": ScriptedWorker()}, tools=[orchestrator.tool, *TOOLS])
    checks = await demonstrate(runner, orchestrator, "planner:scripted")
    return checks + [("the note was written exactly once", len(NOTES) == 1)]


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
        client = OpenAICompatibleModelClient(base_url=settings.base_url, api_key=settings.api_key, model=settings.default_model)
        try:
            orchestrator = an_orchestrator(f"worker:{settings.default_model}")
            runner = Runner({"planner": ScriptedPlanner(), "worker": client},
                            tools=[orchestrator.tool, *TOOLS], persistence=persistence)
            return await demonstrate(runner, orchestrator, "planner:scripted")
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
    checks = asyncio.run(offline()) if parser.parse_args().offline else live()
    for label, passed in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {label}")
    failed = [label for label, passed in checks if not passed]
    if failed:
        raise SystemExit(f"failed: {failed}")
