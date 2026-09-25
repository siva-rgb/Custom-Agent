"""15 - Budgets: split a run's ceiling over a plan, spend it, reclaim it, and stop.

What it shows
  * BudgetPolicy splits a run ceiling once: 20% to the orchestrator, 20% held
    unallocated, and the rest reserved per node -- a greedy proposal capped at
    40% of what is still unallocated, and the nodes that propose nothing
    splitting the remainder equally (FR-67)
  * a node that ends under its reservation returns the difference to the run
    pool, where a later node can be topped up from it
  * enforcement is soft and checked before each model call: an agent at or over
    its reservation makes no further call, so it overshoots by at most one, and
    the run that asks for one anyway ends failed with budget_exceeded (FR-68)
  * an unknown cost leaves the spend unknown, never 0

Run it
  python scripts/15_budget.py            # live: gateway + PostgreSQL (DATABASE_URL in .env)
  python scripts/15_budget.py --offline  # in memory, a scripted model: no database, no network
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from decimal import Decimal

from agentsdk import (
    AgentSpec,
    BudgetGovernor,
    BudgetPolicy,
    Message,
    PlanNode,
    PlanVersion,
    Role,
    RunConfig,
    Runner,
    RunStatus,
)
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.plan import BudgetReservation
from agentsdk.registry import ModelCapabilities, ModelEntry, ModelPricing, ModelRegistry
from agentsdk.session import InMemorySessionStore

TENANT, PROJECT = "example-tenant", "examples"
AGENT = AgentSpec(id="spender", instructions="Reply with one short sentence.")
TASK = "Say that the budget is being spent."
OFFLINE_MODEL = "scripted-model"
# A price for the scripted model, so the offline run costs a known amount.
OFFLINE_PRICING = ModelPricing(input=Decimal("0.001"), output=Decimal("0.01"))


class ScriptedModel:
    async def send(self, request):
        return ModelResponse(
            message=Message(role=Role.ASSISTANT, content="Spending the budget."),
            stop_reason=StopReason.END_TURN,
            usage=Usage(20, 5, 25),
        )


def a_plan(run_id, summarise_usd):
    """Three nodes: one greedy proposal, one deliberately too small, one silent."""
    return PlanVersion(
        plan_id=str(uuid.uuid4()),
        version=1,
        run_id=run_id,
        nodes=(
            PlanNode(node_id="gather", objective="Collect the notes", assigned_role="researcher",
                     budget_reservation=BudgetReservation(usd=Decimal("5.00"))),
            PlanNode(node_id="summarise", objective="Summarise them", assigned_role="writer",
                     dependencies=("gather",), budget_reservation=BudgetReservation(usd=summarise_usd)),
            PlanNode(node_id="check", objective="Check the summary", assigned_role="reviewer",
                     dependencies=("summarise",)),
        ),
    )


def live_client():
    from agentsdk.config import Settings
    from agentsdk.providers import OpenAICompatibleModelClient

    settings = Settings.from_env(load_dotfile=False)
    return OpenAICompatibleModelClient(
        base_url=settings.base_url, api_key=settings.api_key, model=settings.default_model
    )


async def demonstrate(runner, governor, model_id):
    allocation = governor.allocation
    print(f"run ceiling {governor.policy.run_ceiling_usd} USD")
    print(f"  orchestrator reserve {allocation.orchestrator.usd}")
    print(f"  unallocated reserve  {allocation.unallocated.usd}")
    for node_id, amount in allocation.reservations.items():
        print(f"  reservation {node_id:<10} {amount.usd}")

    def config(lease):
        return RunConfig(tenant_id=TENANT, project_id=PROJECT, max_turns=1,
                         model_override=model_id, budget_lease=lease)

    gather = governor.lease("gather")
    first = await runner.run(AGENT, TASK, config(gather))
    print(f"gather: {first.status.value}, cost {first.cost_usd}, spent {gather.spent.usd} of {gather.reservation.usd}")
    before = governor.remaining_unallocated.usd
    gather.release()
    reclaimed = governor.remaining_unallocated.usd - before
    print(f"gather released: reclaimed {reclaimed} to the unallocated reserve, now {governor.remaining_unallocated.usd}")

    # summarise reserved less than one call costs: the first call is allowed,
    # because it is under its reservation until the call is charged.
    summarise = governor.lease("summarise")
    second = await runner.run(AGENT, TASK, config(summarise))
    print(f"summarise: {second.status.value}, spent {summarise.spent.usd} of {summarise.reservation.usd}")
    third = await runner.run(AGENT, TASK, config(summarise))
    print(f"summarise again: {third.status.value}, reason {third.error}")

    checks = [
        ("the ceiling was split 20/20 and the rest reserved per node",
         allocation.orchestrator.usd == governor.policy.run_ceiling_usd * Decimal("0.20")),
        ("the greedy proposal was capped at 40% of the unallocated pool",
         allocation.reservations["gather"].usd < Decimal("5.00")),
        ("the silent node was given what was left", allocation.reservations["check"].usd > 0),
        ("the first node completed inside its reservation", first.status is RunStatus.COMPLETED),
        ("its unspent reservation came back to the run pool", reclaimed > 0),
        ("the second node overshot its reservation by at most one call",
         second.status is RunStatus.COMPLETED and summarise.spent.usd > summarise.reservation.usd),
        ("the next call was refused", third.status is RunStatus.FAILED and third.error == "budget_exceeded"),
        ("the run's spend is the sum of what its leases spent",
         governor.spend.usd == gather.spent.usd + summarise.spent.usd),
    ]
    print(f"run spend {governor.spend.usd} USD of {governor.policy.run_ceiling_usd}")
    return checks


async def offline():
    run_id = str(uuid.uuid4())
    registry = ModelRegistry([
        ModelEntry(provider="scripted", model_id=OFFLINE_MODEL, model_version="1", adapter_version="scripted/1",
                   capabilities=ModelCapabilities(max_context_tokens=100_000, pricing=OFFLINE_PRICING))
    ])
    runner = Runner({"scripted": ScriptedModel()}, session_store=InMemorySessionStore(), model_registry=registry)
    governor = BudgetGovernor(BudgetPolicy(run_ceiling_usd=Decimal("1.00")), a_plan(run_id, Decimal("0.02")))
    return await demonstrate(runner, governor, OFFLINE_MODEL)


def live():
    from dotenv import load_dotenv

    from agentsdk import Persistence
    from agentsdk.config import Settings, normalise_database_url
    from agentsdk.postgres import close_pools

    load_dotenv()
    dsn = normalise_database_url(os.environ["DATABASE_URL"])
    persistence = Persistence.postgres(dsn)  # once, before the event loop starts
    settings = Settings.from_env(load_dotfile=False)

    async def go():
        client = live_client()
        try:
            # The gateway's model needs a price for a USD ceiling; the shipped table
            # lists only openai.gpt-4o-mini, so anything else is priced here (FR-69).
            registry = ModelRegistry([
                ModelEntry(provider="gateway", model_id=settings.default_model, model_version="1",
                           adapter_version="openai-compatible/1",
                           capabilities=ModelCapabilities(
                               max_context_tokens=100_000,
                               pricing=ModelPricing(input=Decimal("0.000001"), output=Decimal("0.000005")),
                           ))
            ])
            runner = Runner({"model": client}, persistence=persistence, model_registry=registry)
            run_id = str(uuid.uuid4())
            # A real call costs a fraction of a cent, so the second node proposes less
            # than one call costs and overshoots on its first.
            governor = BudgetGovernor(
                BudgetPolicy(run_ceiling_usd=Decimal("0.01")), a_plan(run_id, Decimal("0.0000001")),
            )
            return await demonstrate(runner, governor, settings.default_model)
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
