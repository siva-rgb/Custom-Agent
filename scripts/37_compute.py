"""37 - Compute in budgets: every command's CPU measured, recorded and charged to its plan.

Each command a workspace runs is measured (FR-156): on Docker a wrapper, the container's PID 1,
runs it, kills whatever it left, and reads the container's own CPU counter last, so nothing the
command prints changes what it is charged. A command killed before the reading is charged its wall
time times its `cpus` limit, the most it could have used. With `BudgetPolicy(cpu_second_usd=...)`
each command's cost is drawn from its run's budget lease like a model call's (FR-157): it is on
the command's event, in the child's cost and in the plan's spend, and a command is not started
once its lease is spent.

Two plans run under one policy. In the first, two children each burn a second of CPU and finish
within budget. In the second, a child that reserved less than one command's compute asks for two
burns in one turn: the first crosses its reservation and is recorded and charged; the second is
asked about before it starts, as a model call is, and never starts; the child ends
`budget_exceeded`.

Run it
  python scripts/37_compute.py             # live: gateway + PostgreSQL + Docker
  python scripts/37_compute.py --offline   # in memory, scripted models, FakeProvider
  python scripts/37_compute.py --scripted  # in memory, scripted models, Docker

Live and --scripted need Docker running and the image below on the host:
  docker pull python:3.12-alpine@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from decimal import Decimal

from agentsdk import AgentSpec, Message, Role, RunStatus, Runner
from agentsdk.budget import BudgetPolicy
from agentsdk.context_policy import ContextPolicy
from agentsdk.events import EventType
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.orchestrator import PLAN_TOOL, Orchestrator
from agentsdk.primitives import ToolCall
from agentsdk.registry import ModelCapabilities, ModelEntry, ModelPricing, ModelRegistry
from agentsdk.tools import ApprovalPolicy
from agentsdk.workspace import DockerProvider, FakeProvider, WorkspaceSpec

TENANT, PROJECT = "example-tenant", "examples"
IMAGE = "python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df"
BURN = 1.0  # CPU-seconds each command burns
SLACK = 1.0  # what the interpreter's start may add to a reading on Docker
PRICE = Decimal("0.05")  # a CPU-second
RESERVED = "0.04"  # the second plan's child: less than one command's compute
BURN_ARGV = ["python", "-c", "import time\nt = time.process_time()\n"
             f"while time.process_time() - t < {BURN}: pass\nprint('burnt')\n"]
TOOLS = ("run_command",)


def objective(times: str) -> str:
    how = "as two run_command calls in the same response" if times == "twice" else "with run_command once"
    return f"Run the command with argv {json.dumps(BURN_ARGV)} {how}, then answer in one sentence."


PLANS = {
    "within": [{"node_id": node, "assigned_role": node, "dependencies": [], "objective": objective("once")}
               for node in ("a", "b")],
    "ceiling": [{"node_id": "c", "assigned_role": "c", "dependencies": [], "objective": objective("twice"),
                 "budget_reservation": {"usd": RESERVED}}],
}


def plan_task(name: str) -> str:
    return f"Call {PLAN_TOOL} once with exactly this plan, then answer in one sentence: " + json.dumps(
        {"nodes": PLANS[name]})


# --- FakeProvider's stand-in for the image's python (--offline) ------------------------------------


async def python(cmd) -> int:
    """The fake runs no model-written code (FR-139): it declares this example's burn, and refuses
    any other program."""
    if cmd.argv != BURN_ARGV:
        cmd.err("the fake runs only this example's program\n")
        return 1
    cmd.cpu(BURN)
    cmd.out("burnt\n")
    return 0


def provider(name: str):
    return DockerProvider() if name == "docker" else FakeProvider(commands={"python": python})


def roles(model_id: str) -> dict[str, AgentSpec]:
    return {node: AgentSpec(id=f"burner-{node}", instructions="Use your workspace tools exactly as asked.",
                            preferred_model=model_id, tool_profile=TOOLS,
                            workspace=WorkspaceSpec(image=IMAGE, run_command_approval=ApprovalPolicy.AUTO,
                                                    max_command_seconds=60))
            for node in ("a", "b", "c")}


def orchestrator_for(model_id: str) -> Orchestrator:
    return Orchestrator(roles=roles(model_id),
                        policy=BudgetPolicy(run_ceiling_usd=Decimal("10.00"), cpu_second_usd=PRICE),
                        context_policy=ContextPolicy(context_window=200_000))


# --- offline: scripted models -----------------------------------------------------------------------


def _reply(calls=(), content=""):
    return ModelResponse(message=Message(role=Role.ASSISTANT, content=content, tool_calls=tuple(calls)),
                         stop_reason=StopReason.TOOL_CALLS if calls else StopReason.END_TURN, usage=Usage(20, 5, 25))


class ScriptedModel:
    """Plans when it is the orchestrator; otherwise burns as its objective says, both calls of "twice"
    in one turn, so only the check before a command stops the second (M43 pre-review F5)."""

    async def send(self, request):
        task = next(m.content for m in request.messages if m.role is Role.USER)
        turn = sum(1 for m in request.messages if m.role is Role.ASSISTANT)
        if f"Call {PLAN_TOOL}" in task:
            plan = json.loads(task[task.index("{"):])
            turns = [[(PLAN_TOOL, plan)]]
        else:
            turns = [[("run_command", {"argv": BURN_ARGV})] * (2 if "two run_command calls" in task else 1)]
        if turn < len(turns):
            return _reply([ToolCall(id=f"t{turn}-{i}", name=name, arguments=arguments)
                           for i, (name, arguments) in enumerate(turns[turn])])
        return _reply(content="Done.")


# A model this cheap leaves compute to decide when a child stops.
SCRIPTED_PRICING = ModelPricing(input=Decimal("0.0000001"), output=Decimal("0.0000001"))


def scripted_registry() -> ModelRegistry:
    return ModelRegistry([ModelEntry(provider="scripted", model_id="model", model_version="1",
                                     adapter_version="scripted/1",
                                     capabilities=ModelCapabilities(max_context_tokens=200_000,
                                                                    pricing=SCRIPTED_PRICING))])


class RecordingRunner(Runner):
    """In memory a Runner keeps no child run's events: this one keeps every run's sink, so the
    checks read a child's events in memory as they read PostgreSQL's (as example 36's does)."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.sinks: dict[str, object] = {}

    def _open(self, spec, config):
        opened = super()._open(spec, config)
        self.sinks[opened.scope.run_id] = opened.events
        return opened


# --- the demonstration ------------------------------------------------------------------------------


async def events_of(runner: RecordingRunner, persistence, run_ids) -> dict[str, list[tuple[str, dict]]]:
    """Each run's (event type, payload) in order: from PostgreSQL, or in memory from the sinks."""
    if persistence is None:
        return {r: [(e.event_type.value, e.payload) for e in runner.sinks[r].events()] for r in run_ids}
    import psycopg

    from agentsdk.config import normalise_database_url

    def read() -> dict:
        found = {str(r): [] for r in run_ids}
        with psycopg.connect(normalise_database_url(os.environ["DATABASE_URL"])) as conn:
            for run_id, kind, payload in conn.execute(
                    "SELECT run_id, event_type, payload FROM run_events WHERE run_id = ANY(%s::uuid[])"
                    " ORDER BY run_id, sequence_no", (list(run_ids),)).fetchall():
                found[str(run_id)].append((kind, payload))
        return found

    return await asyncio.to_thread(read)


async def run_costs(persistence, run_ids) -> dict[str, Decimal | None]:
    """Each run's recorded cost_usd, on PostgreSQL; nothing in memory."""
    if persistence is None:
        return {}
    import psycopg

    from agentsdk.config import normalise_database_url

    def read() -> dict:
        with psycopg.connect(normalise_database_url(os.environ["DATABASE_URL"])) as conn:
            return {str(r): c for r, c in conn.execute(
                "SELECT run_id, cost_usd FROM runs WHERE run_id = ANY(%s::uuid[])", (list(run_ids),)).fetchall()}

    return await asyncio.to_thread(read)


def cost_of(events: list[tuple[str, dict]]) -> Decimal | None:
    """A run's spend from its own events (NFR-23 as amended): its ModelCalled and
    WorkspaceCommandEnded costs; None once one is unknown."""
    total: Decimal | None = Decimal(0)
    for kind, payload in events:
        if kind == EventType.MODEL_CALLED.value or (
                kind == EventType.WORKSPACE_COMMAND_ENDED.value and payload.get("cost_usd") is not None):
            total = None if total is None or payload.get("cost_usd") is None else total + Decimal(payload["cost_usd"])
    return total


async def run_plan(runner: RecordingRunner, orchestrator: Orchestrator, persistence, model_id: str, name: str, *,
                   tenant: str, project: str):
    """The plan's result, its governor, and its children as {node: (run id, events)}."""
    config = orchestrator.config(tenant_id=tenant, project_id=project, model_override=model_id, max_turns=6)
    before = set(runner.sinks)
    result = await runner.run(orchestrator.agent, plan_task(name), config)
    opened = [r for r in runner.sinks if r not in before and r != result.run_id]
    events = await events_of(runner, persistence, opened)
    children = {}
    for run_id, found in events.items():
        started = next((p for kind, p in found if kind == EventType.RUN_STARTED.value), {})
        if str(started.get("parent_run_id")) == str(result.run_id):
            children[str(started.get("agent_spec_id", "")).removeprefix("burner-")] = (run_id, found)
    print(f"plan {name} {result.run_id}: {result.status.value}; children "
          f"{ {node: run_id for node, (run_id, _) in children.items()} }")
    return result, config.budget_lease.governor, children


async def demonstrate(runner: RecordingRunner, orchestrator: Orchestrator, persistence, model_id: str, chosen, *,
                      tenant: str, project: str, live: bool):
    store = runner._workspace_store()
    checks = []

    def check(label: str, subject, why_absent: str, failing) -> None:
        """A check whose subject never happened fails; else the first failing clause, or None."""
        if not subject:
            checks.append((label, None, why_absent))
            return
        clause = failing()
        checks.append((label, clause is None, clause))

    # --- the first plan: within its budget ------------------------------------------------------------
    result, governor, children = await run_plan(runner, orchestrator, persistence, model_id, "within",
                                                tenant=tenant, project=project)
    rows = {c.command_id: c for c in await asyncio.to_thread(store.commands, tenant, project)}
    ended = [(node, p) for node, (_, found) in children.items() for kind, p in found
             if kind == EventType.WORKSPACE_COMMAND_ENDED.value]
    burns = [(node, p, rows.get(p["command_id"])) for node, p in ended
             if rows.get(p["command_id"]) is not None and rows[p["command_id"]].argv == BURN_ARGV
             and p.get("exit_code") == 0]
    for node, p, row in burns:
        print(f"  {node}: command {p['command_id']} cpu {p['cpu_seconds']} s, cost {p['cost_usd']} USD")

    check("each child's command was measured: its row and its event carry the same CPU, close to what it burned",
          {node for node, _, _ in burns} == {"a", "b"}, "no child's burn ran to its end",
          lambda: next((f"{node}'s row reads {row.cpu_usec} us, its event {p['cpu_seconds']} s"
                        for node, p, row in burns if row.cpu_usec / 1_000_000 != p["cpu_seconds"]), None)
          or next((f"{node} burnt {BURN} s and read {p['cpu_seconds']} s" for node, p, _ in burns
                   if not 0.9 * BURN <= p["cpu_seconds"] <= BURN + SLACK), None))

    check("each command was charged its CPU at cpu_second_usd, on its event", burns, "no command was measured",
          lambda: next((f"{node}'s command cost {p['cost_usd']}" for node, p, row in burns
                        if p.get("cost_usd") is None
                        or Decimal(p["cost_usd"]) != Decimal(row.cpu_usec) / 1_000_000 * PRICE), None))

    stored = await run_costs(persistence, [run_id for run_id, _ in children.values()])
    spent = {node: cost_of(found) for node, (_, found) in children.items()}
    check("each child's cost is its model calls' and its commands', and the plan's spend is its own and every child's",
          burns and set(spent) == {"a", "b"}, "the plan's children never ran their burns",
          lambda: next((f"{node}'s lease spent {governor.lease(node).spent.usd}, its events {spent[node]}"
                        for node in spent if governor.lease(node).spent.usd != spent[node]), None)
          or next((f"{node}'s run row costs {stored[run_id]}, its events {spent[node]}"
                   for node, (run_id, _) in children.items() if stored and stored.get(run_id) != spent[node]), None)
          or (None if governor.spend.usd == result.cost_usd + sum(spent.values()) else
              f"the plan spent {governor.spend.usd}, its own cost {result.cost_usd} and children's {spent}"))

    if not live:
        check("the first plan completed within its budget, each child completed",
              burns, "no child's burn ran to its end",
              lambda: (f"the plan ended {result.status.value}" if result.status is not RunStatus.COMPLETED else None)
              or next((f"{node} ended {found[-1][0]}" for node, (_, found) in children.items()
                       if not found or found[-1][0] != EventType.RUN_COMPLETED.value), None))

    # --- the second plan: stopped by its compute ceiling ----------------------------------------------
    stopped, governor, children = await run_plan(runner, orchestrator, persistence, model_id, "ceiling",
                                                 tenant=tenant, project=project)
    run_id, found = children.get("c", (None, []))
    reserved = governor.lease("c").reservation.usd if "c" in children else None
    running, crossing, after = Decimal(0), None, []
    for kind, p in found:
        if kind == EventType.WORKSPACE_COMMAND_STARTED.value and crossing is not None:
            after.append(p["command_id"])
        if kind in (EventType.MODEL_CALLED.value, EventType.WORKSPACE_COMMAND_ENDED.value) and p.get("cost_usd"):
            before, running = running, running + Decimal(p["cost_usd"])
            if kind == EventType.WORKSPACE_COMMAND_ENDED.value and before < reserved <= running:
                crossing = p
    started = [p for kind, p in found if kind == EventType.WORKSPACE_COMMAND_STARTED.value]
    terminal = next((p for kind, p in reversed(found) if kind in (EventType.RUN_FAILED.value,
                                                                  EventType.RUN_COMPLETED.value)), {})
    crossed_row = rows_after = None
    if crossing is not None:
        rows_after = {c.command_id: c for c in await asyncio.to_thread(store.commands, tenant, project, run_id)}
        crossed_row = rows_after.get(crossing["command_id"])
    print(f"  c: reserved {reserved}, spent {running}; the crossing command "
          f"{None if crossing is None else crossing['command_id']}; ended {terminal.get('reason')}")
    check("a plan whose children's compute passed its ceiling stopped budget_exceeded, the command that crossed it recorded",
          started, "the child never started a command",
          lambda: (f"no command's cost crossed the reservation of {reserved}: {len(started)} ran, the child "
                   f"ended {terminal.get('reason') or terminal.get('status')}" if crossing is None else None)
          or (None if crossed_row is not None and crossed_row.cpu_usec is not None else
              "the crossing command has no row with its CPU")
          or (f"{len(after)} command(s) started after it" if after else None)
          or (None if terminal.get("reason") == "budget_exceeded" else f"the child ended {terminal}")
          or (None if governor.lease("c").spent.usd == running else
              f"its lease spent {governor.lease('c').spent.usd}, its events {running}"))

    # --- closing --------------------------------------------------------------------------------------
    await runner.aclose()
    left = await chosen.list_owned({"agentsdk.tenant": tenant, "agentsdk.project": project})
    check("after aclose() no workspace of the example remains", burns or crossing is not None,
          "no workspace was ever made", lambda: f"{len(left)} remained" if left else None)
    return checks, [result.run_id, stopped.run_id]


def report(checks) -> list[str]:
    """A check whose subject never happened fails, in every mode."""
    failed = []
    for label, passed, why in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {'' if passed is not None else 'not exercised: '}{label}"
              + (f" -- {why}" if why and not passed else ""))
        if not passed:
            failed.append(label)
    return failed


def docker_ready() -> str | None:
    """Why this example cannot use Docker here, or None."""
    import subprocess

    try:
        if subprocess.run(["docker", "version"], capture_output=True, timeout=30).returncode != 0:
            return "docker not running"
        if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, timeout=30).returncode != 0:
            return "the example image is not on this host (see the docstring to pull it)"
    except (OSError, subprocess.TimeoutExpired):
        return "docker not running"
    return None


async def offline(store: str = "memory", provider_name: str = "fake", *, tenant: str = TENANT,
                  project: str = PROJECT, quiet: bool = False):
    """The checks as (label, passed, why); passed is None when the subject never happened."""
    import contextlib
    import io

    persistence = None
    if store == "postgres":
        from dotenv import load_dotenv

        from agentsdk import Persistence
        from agentsdk.config import normalise_database_url

        load_dotenv()
        persistence = Persistence.postgres(normalise_database_url(os.environ["DATABASE_URL"]), create_schema=False)
    chosen = provider(provider_name)
    orchestrator = orchestrator_for("scripted:model")
    runner = RecordingRunner({"scripted": ScriptedModel()}, tools=[orchestrator.tool], workspace_provider=chosen,
                             persistence=persistence, model_registry=scripted_registry())
    with contextlib.redirect_stdout(io.StringIO()) if quiet else contextlib.nullcontext():
        checks, _ = await demonstrate(runner, orchestrator, persistence, "scripted:model", chosen, tenant=tenant,
                                      project=project, live=False)
    return checks


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
            model_id = f"m:{settings.default_model}"
            orchestrator = orchestrator_for(model_id)
            runner = RecordingRunner({"m": client}, tools=[orchestrator.tool], workspace_provider=docker,
                                     persistence=persistence)
            checks, runs = await demonstrate(runner, orchestrator, persistence, model_id, docker, tenant=TENANT,
                                             project=PROJECT, live=True)
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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="in memory, scripted models, FakeProvider")
    mode.add_argument("--scripted", action="store_true", help="in memory, scripted models, Docker")
    args = parser.parse_args()
    if not args.offline and (why := docker_ready()) is not None:
        print(f"[SKIP] {why}")
        raise SystemExit(0)
    if args.offline or args.scripted:
        checks = asyncio.run(offline(provider_name="fake" if args.offline else "docker"))
    else:
        checks = live()
    failed = report(checks)
    if failed:
        raise SystemExit(f"failed: {failed}")
