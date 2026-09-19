"""14 - Plans: store a run's plan, move its nodes, replan it, and keep every version.

What it shows
  * plan_from_document() checks a planner's JSON against the shipped schema and
    builds an immutable PlanVersion with a plan_hash over what the plan says
  * a plan is a DAG: a dependency cycle is refused before anything is stored
  * put_plan() stores version 1; every node starts pending, and transition()
    moves a node, emitting PlanNodeStarted and PlanNodeFinished through the run's
    own event sink with the node id in the event's task_id
  * a replan is version 2, whose parent_plan names version 1; version 1 is never
    changed, and its node states stay its own
  * a store is bound to one tenant and project: another tenant cannot see the plan

Build Persistence ONCE, at process start and outside the event loop: it creates
or migrates the schema with blocking DDL under a database-wide lock.

Run it
  python scripts/14_plan.py            # live: gateway + PostgreSQL (DATABASE_URL in .env)
  python scripts/14_plan.py --offline  # in memory, a scripted model: no database, no network
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid

from agentsdk import (
    AgentSpec,
    InMemoryRunStateStore,
    InvalidPlan,
    Message,
    PlanNotFound,
    Role,
    RunConfig,
    Runner,
    plan_from_document,
)
from agentsdk.events import InMemoryEventSink
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.session import InMemorySessionStore

TENANT, PROJECT = "example-tenant", "examples"
CONFIG = RunConfig(tenant_id=TENANT, project_id=PROJECT)
AGENT = AgentSpec(id="planner", instructions="Reply with one short sentence.")
TASK = "Say that planning has started."

# What a planner might emit. The orchestrator that asks a model for this arrives in
# M19; here the document is written by hand.
FIRST = {"nodes": [
    {"node_id": "gather", "objective": "Collect this week's release notes", "assigned_role": "researcher",
     "side_effecting": False},
    {"node_id": "summarise", "objective": "Summarise the notes in five lines", "assigned_role": "writer",
     "dependencies": ["gather"],
     "expected_output_schema": {"type": "object", "required": ["summary"],
                                "properties": {"summary": {"type": "string"}}},
     "acceptance_criteria": [{"kind": "output_schema"}],
     "budget_reservation": {"usd": "0.05", "tokens": 4000}},
]}
# The replan keeps gather, narrows summarise and adds a check after it.
SECOND = {"nodes": [
    FIRST["nodes"][0],
    {**FIRST["nodes"][1], "objective": "Summarise only the breaking changes in five lines"},
    {"node_id": "check", "objective": "Confirm every breaking change has a migration note",
     "assigned_role": "reviewer", "dependencies": ["summarise"],
     "acceptance_criteria": [{"kind": "critic", "description": "each breaking change names its migration"}]},
]}
CYCLE = {"nodes": [
    {"node_id": "a", "objective": "wait for b", "assigned_role": "r", "dependencies": ["b"]},
    {"node_id": "b", "objective": "wait for a", "assigned_role": "r", "dependencies": ["a"]},
]}


class ScriptedModel:
    """Offline stand-in for a real model."""

    async def send(self, request):
        return ModelResponse(
            message=Message(role=Role.ASSISTANT, content="Planning has started."),
            stop_reason=StopReason.END_TURN,
            usage=Usage(12, 5, 17),
        )


def live_client():
    from agentsdk.config import Settings
    from agentsdk.providers import OpenAICompatibleModelClient

    settings = Settings.from_env(load_dotfile=False)
    return OpenAICompatibleModelClient(
        base_url=settings.base_url, api_key=settings.api_key, model=settings.default_model
    )


def show(plan):
    parent = "none" if plan.parent_plan is None else f"{plan.parent_plan[0]} version {plan.parent_plan[1]}"
    print(f"plan {plan.plan_id} version {plan.version} (parent: {parent})")
    print(f"  plan_hash {plan.plan_hash}")
    for node in plan.nodes:
        after = f" after {', '.join(node.dependencies)}" if node.dependencies else ""
        print(f"  - {node.node_id} [{node.assigned_role}]{after}: {node.objective}")


async def demonstrate(store, run_id, sink, read_events):
    plan_id = str(uuid.uuid4())

    try:
        plan_from_document(CYCLE, plan_id=str(uuid.uuid4()), version=1, run_id=run_id)
        cycle_refused = None
    except InvalidPlan as error:
        cycle_refused = f"{error} (at {error.path})"
    print(f"a cyclic plan was refused: {cycle_refused}")

    first = plan_from_document(FIRST, plan_id=plan_id, version=1, run_id=run_id)
    await store.put_plan(first)
    show(first)
    for status in ("ready", "running", "done"):
        await store.transition(plan_id, 1, "gather", status, sink=sink)
    await store.transition(plan_id, 1, "summarise", "ready", sink=sink)
    await store.transition(plan_id, 1, "summarise", "running", sink=sink)
    await store.transition(plan_id, 1, "summarise", "failed", sink=sink)
    first_states = dict(await store.node_states(plan_id, 1))
    print(f"version 1 node states: {first_states}")

    second = plan_from_document(SECOND, plan_id=plan_id, version=2, run_id=run_id, parent_plan=(plan_id, 1))
    await store.put_plan(second)
    show(second)
    second_states = dict(await store.node_states(plan_id, 2))
    print(f"version 2 node states: {second_states}")

    stored = await store.versions(plan_id)
    parent = await store.get_plan(*stored[1].parent_plan)
    events = [(kind, node) for kind, node in read_events() if kind.startswith("PlanNode")]
    for kind, node in events:
        print(f"event {kind} task_id={node}")

    try:
        await store.for_scope("another-tenant", PROJECT).get_plan(plan_id, 1)
        hidden = False
    except PlanNotFound:
        hidden = True

    return [
        ("a cyclic plan was refused before anything was stored", cycle_refused is not None),
        ("both versions were stored, in order", [p.version for p in stored] == [1, 2]),
        ("version 2's parent is version 1, unchanged by the replan", parent == first and stored[0] == first),
        ("the replan changed the plan's hash", first.plan_hash != second.plan_hash),
        ("version 1 kept its own node states", first_states == {"gather": "done", "summarise": "failed"}),
        ("version 2 started with every node pending", set(second_states.values()) == {"pending"}),
        ("starting and finishing emitted events carrying the node",
         events == [("PlanNodeStarted", "gather"), ("PlanNodeFinished", "gather"),
                    ("PlanNodeStarted", "summarise"), ("PlanNodeFinished", "summarise")]),
        ("another tenant could not see the plan", hidden),
    ]


async def offline():
    runner = Runner({"scripted": ScriptedModel()}, session_store=InMemorySessionStore())
    run = await runner.run(AGENT, TASK, CONFIG)
    # The in-memory store knows which runs exist only through this callable.
    store = InMemoryRunStateStore(
        TENANT, PROJECT, runs=lambda tenant, project, run_id: (tenant, project, run_id) == (TENANT, PROJECT, run.run_id)
    )
    # The run above has finished and its own in-memory sink went with it, so the
    # node events go to a sink for the same run; on Postgres they join the run's
    # stored events and continue its numbering.
    sink = InMemoryEventSink(TENANT, PROJECT, run.run_id)
    return await demonstrate(store, run.run_id, sink, lambda: [(e.event_type.value, e.task_id) for e in sink.events()])


def live():
    from dotenv import load_dotenv

    import psycopg
    from agentsdk import Persistence
    from agentsdk.config import normalise_database_url
    from agentsdk.postgres import RunScope, close_pools

    load_dotenv()
    dsn = normalise_database_url(os.environ["DATABASE_URL"])
    persistence = Persistence.postgres(dsn)  # once, before the event loop starts

    async def go():
        client = live_client()
        try:
            run = await Runner({"model": client}, persistence=persistence).run(AGENT, TASK, CONFIG)
        finally:
            await client.aclose()
        sink = persistence.event_sink_for(RunScope(run_id=run.run_id, tenant_id=TENANT, project_id=PROJECT))

        def read_events():
            with psycopg.connect(dsn) as conn:
                return conn.execute(
                    "SELECT event_type, task_id FROM run_events WHERE run_id = %s ORDER BY sequence_no", (run.run_id,)
                ).fetchall()

        return await demonstrate(persistence.run_state_store(TENANT, PROJECT), run.run_id, sink, read_events)

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
