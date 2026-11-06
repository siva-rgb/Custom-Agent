"""M48: budgets for plain runs (FR-172, FR-173, AC-106, NFR-50; DECISION-6a07dbc6).

A top-level run given `RunConfig.budget`, a `BudgetPolicy`, is governed as a plan's single child is:
the Runner turns the policy, once at the start, into the one lease of
`BudgetGovernor.for_single_run(policy)`, whose single reservation, `run`, holds the whole ceiling.
The loop asks it before each model call, `run_command` before each command, and each call's and
command's cost is charged when it ends. A run whose ceiling is spent ends FAILED `budget_exceeded`
after the call or command that crossed it is recorded; one whose final answer is the crossing call
completes, its spend over the ceiling recorded, as a child's does.

Refused before the run starts: a budget beside a `budget_lease` (an orchestrator's run has one), on
a run the SubagentPool placed (`depth` set), a budget that is no `BudgetPolicy` (`ValueError`), and a
USD ceiling on a model nothing prices (`UnpricedModel`, FR-69). A run without a budget records what
it did before M48 (NFR-50).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import subprocess
import uuid
from decimal import Decimal

import psycopg
import pytest

from agentsdk import AgentSpec, Persistence, RunConfig, RunStatus, Runner
from agentsdk.budget import BudgetAmount, BudgetGovernor, BudgetPolicy
from agentsdk.config import normalise_database_url
from agentsdk.errors import UnpricedModel
from agentsdk.events import EventType
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.orchestrator import Orchestrator
from agentsdk.plan import BudgetReservation, PlanNode, PlanVersion
from agentsdk.primitives import Message, Role, ToolCall
from agentsdk.registry import ModelCapabilities, ModelEntry, ModelPricing, ModelRegistry
from agentsdk.scope import ToolScope
from agentsdk.subagents import Briefing, SubagentPool
from agentsdk.tools import ApprovalPolicy, Tool, ToolSpec
from agentsdk.workspace import DockerProvider, FakeProvider, WorkspaceSpec

DSN = normalise_database_url(os.environ.get("DATABASE_URL"))
PREFIX = "SYN-m48-"
PROJECT = "p-m48"
STORES = ["memory", "postgres"]
FAKE_IMAGE = "fake/workspace@sha256:" + "0" * 64
IMAGE = "python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df"
TENANTS: list[str] = []
MODEL = "priced-model"
UNPRICED = "unpriced-model"
# One call of Usage(5, 1, 6): 5 prompt tokens at 0.01 and 1 completion token at 0.10 is 0.15 USD.
PRICING = ModelPricing(input=Decimal("0.01"), output=Decimal("0.10"))
CALL_USD = Decimal("0.15")
CALL_TOKENS = 6
CPU_USD = Decimal("0.50")  # a CPU-second


def _docker_leftovers(tenant: str) -> None:
    """Removes what a test left on the daemon for its own tenancy: never anything else."""
    for kind, listing in (("container", ["ps", "-aq"]), ("volume", ["volume", "ls", "-q"])):
        found = subprocess.run(["docker", *listing, "--filter", f"label=agentsdk.tenant={tenant}"],
                               capture_output=True, text=True, timeout=60).stdout.split()
        if found:
            subprocess.run(["docker", kind, "rm", "-f", *found], capture_output=True, timeout=120)


@pytest.fixture(autouse=True)
def remove_what_the_test_wrote():
    TENANTS.clear()
    yield
    if TENANTS and subprocess.run(["docker", "version"], capture_output=True).returncode == 0:
        for tenant in TENANTS:
            _docker_leftovers(tenant)
    if DSN and TENANTS:
        with psycopg.connect(DSN, autocommit=True) as conn:
            ids = [r[0] for r in conn.execute("SELECT run_id FROM runs WHERE tenant_id = ANY(%s)", (TENANTS,))]
            conn.execute("DELETE FROM workspace_commands WHERE tenant_id = ANY(%s)", (TENANTS,))
            conn.execute("DELETE FROM workspaces WHERE tenant_id = ANY(%s)", (TENANTS,))
            for table in ("run_events", "messages", "execution_manifests"):
                conn.execute(f"DELETE FROM {table} WHERE run_id = ANY(%s)", (ids,))
            conn.execute("DELETE FROM runs WHERE run_id = ANY(%s)", (ids,))


def tenancy() -> str:
    tenant = f"{PREFIX}{uuid.uuid4().hex[:10]}"
    TENANTS.append(tenant)
    return tenant


def respond(content="", calls=()):
    return ModelResponse(message=Message(role=Role.ASSISTANT, content=content, tool_calls=tuple(calls)),
                         stop_reason=StopReason.TOOL_CALLS if calls else StopReason.END_TURN, usage=Usage(5, 1, 6))


class Turns:
    """Each turn makes the run_command calls its task lists (a JSON list of turns, each a list of
    argvs), then answers."""

    def __init__(self) -> None:
        self.calls = 0

    async def send(self, request):
        self.calls += 1
        turns = json.loads(request.messages[0].content)
        turn = sum(1 for m in request.messages if m.role is Role.ASSISTANT)
        if turn < len(turns):
            return respond(calls=[ToolCall(id=f"t{turn}-{i}", name="run_command", arguments={"argv": argv})
                                  for i, argv in enumerate(turns[turn])])
        return respond("done")


def _burner(seconds: float):
    async def burn(cmd):
        cmd.cpu(seconds)
        cmd.out(f"burnt {seconds}\n")
        return 0
    return burn


async def _quiet(cmd):
    cmd.out("ok\n")
    return 0


def registry() -> ModelRegistry:
    def entry(model_id, pricing):
        return ModelEntry(provider="test", model_id=model_id, model_version="1", adapter_version="test/1",
                          capabilities=ModelCapabilities(max_context_tokens=100_000, pricing=pricing))
    return ModelRegistry([entry(MODEL, PRICING), entry(UNPRICED, None)])


def make(store: str, provider=None):
    persistence = Persistence.postgres(DSN, create_schema=False) if store == "postgres" else None
    model = Turns()
    runner = Runner({"m": model}, model_registry=registry(), persistence=persistence,
                    workspace_provider=provider or FakeProvider(commands={
                        "burn1": _burner(1.0), "burn3": _burner(3.0), "quiet": _quiet}))
    return runner, model


def agent(image: str = FAKE_IMAGE, model_id: str = MODEL) -> AgentSpec:
    return AgentSpec(id="spender", instructions="x", preferred_model=f"m:{model_id}", tool_profile=("run_command",),
                     workspace=WorkspaceSpec(image=image, run_command_approval=ApprovalPolicy.AUTO,
                                             max_command_seconds=30))


async def _run(store: str, turns: list, budget=None, *, model_id: str = MODEL, **config):
    tenant = tenancy()
    runner, model = make(store)
    try:
        result = await runner.run(agent(model_id=model_id), json.dumps(turns), RunConfig(
            tenant_id=tenant, project_id=PROJECT, max_turns=6, budget=budget, **config))
        rows = await asyncio.to_thread(runner._workspace_store().commands, tenant, PROJECT)
    finally:
        await runner.aclose()
    return result, rows, model


def _ended(result) -> list[dict]:
    return [e.payload for e in result.events if e.event_type is EventType.WORKSPACE_COMMAND_ENDED]


def _model_costs(result) -> list[Decimal]:
    return [Decimal(e.payload["cost_usd"]) for e in result.events if e.event_type is EventType.MODEL_CALLED]


def _manifest(run_id: str):
    with psycopg.connect(DSN) as conn:
        return conn.execute("SELECT budget_policy, budget_reservations, budget_spend FROM execution_manifests "
                            "WHERE run_id = %s", (run_id,)).fetchone()


# =================================================================================================
# The single-run governor
# =================================================================================================


def test_a_single_run_governor_holds_the_whole_ceiling_in_one_lease():
    policy = BudgetPolicy(run_ceiling_usd=Decimal("2.00"), run_ceiling_tokens=1000)
    governor = BudgetGovernor.for_single_run(policy)
    lease = governor.lease("run")
    # No reserve is set aside: no replan or sibling can claim it (FR-172).
    assert lease.reservation == BudgetAmount(usd=Decimal("2.00"), tokens=1000)
    assert governor.lease("run") is lease, "one lease, the same object every time"
    with pytest.raises(KeyError):
        governor.lease("other")
    assert lease.may_call()


def test_a_single_run_governor_refuses_anything_but_a_policy():
    with pytest.raises(ValueError, match="BudgetPolicy"):
        BudgetGovernor.for_single_run({"run_ceiling_usd": "1"})


# =================================================================================================
# FR-172: stopped at the ceiling, on both stores
# =================================================================================================


@pytest.mark.parametrize("store", STORES)
async def test_a_usd_ceiling_stops_the_run_after_the_model_call_that_crossed_it(store):
    # 0.15, then 0.30 past 0.20: the second turn's command finds the ceiling spent, and no third call.
    result, rows, model = await _run(store, [[["quiet"]], [["quiet"]], [["quiet"]]],
                                     BudgetPolicy(run_ceiling_usd=Decimal("0.20")))
    assert result.status is RunStatus.FAILED and result.error == "budget_exceeded"
    assert model.calls == 2 and _model_costs(result) == [CALL_USD, CALL_USD]
    assert [r.argv for r in rows] == [["quiet"]], "the command after the crossing call never started"
    assert result.cost_usd == CALL_USD * 2


@pytest.mark.parametrize("store", STORES)
async def test_a_token_ceiling_stops_the_run_the_same_way(store):
    result, rows, model = await _run(store, [[["quiet"]], [["quiet"]], [["quiet"]]],
                                     BudgetPolicy(run_ceiling_tokens=CALL_TOKENS + 1))
    assert result.status is RunStatus.FAILED and result.error == "budget_exceeded"
    assert model.calls == 2 and len(rows) == 1


@pytest.mark.parametrize("store", STORES)
async def test_compute_that_crosses_the_ceiling_stops_the_run_after_the_command(store):
    result, rows, model = await _run(store, [[["burn3"]], [["burn1"]]],
                                     BudgetPolicy(run_ceiling_usd=Decimal("1.00"), cpu_second_usd=CPU_USD))
    assert result.status is RunStatus.FAILED and result.error == "budget_exceeded"
    assert model.calls == 1, "no model call after the command that crossed the ceiling"
    [ended] = _ended(result)
    assert ended["cpu_seconds"] == 3.0 and Decimal(ended["cost_usd"]) == Decimal("1.50")
    assert [r.argv for r in rows] == [["burn3"]]
    # NFR-23 (FR-173): the run's spend is its model calls' costs and its commands' costs.
    assert result.cost_usd == CALL_USD + Decimal("1.50") == sum(_model_costs(result)) + sum(
        Decimal(p["cost_usd"]) for p in _ended(result))


@pytest.mark.parametrize("store", STORES)
async def test_a_final_answer_on_the_crossing_call_completes_with_its_spend_recorded(store):
    """DECISION-6a07dbc6: as a plan's child does, the answer stands and the overspend is recorded."""
    result, rows, model = await _run(store, [], BudgetPolicy(run_ceiling_usd=Decimal("0.10")))
    assert result.status is RunStatus.COMPLETED and result.output == "done", result.error
    assert model.calls == 1 and result.cost_usd == CALL_USD > Decimal("0.10")


@pytest.mark.parametrize("store", STORES)
async def test_a_run_inside_its_ceiling_completes(store):
    result, rows, model = await _run(store, [[["quiet"]]], BudgetPolicy(run_ceiling_usd=Decimal("5.00")))
    assert result.status is RunStatus.COMPLETED, result.error
    assert model.calls == 2 and len(rows) == 1 and result.cost_usd == CALL_USD * 2


@pytest.mark.parametrize("store", STORES)
async def test_a_second_command_of_the_crossing_turn_does_not_start(store):
    """M48 pre-review F2: asked before it starts, the turn's next command finds the ceiling spent."""
    result, rows, model = await _run(store, [[["burn3"], ["burn3"]]],
                                     BudgetPolicy(run_ceiling_usd=Decimal("1.00"), cpu_second_usd=CPU_USD))
    assert result.status is RunStatus.FAILED and result.error == "budget_exceeded"
    assert [r.argv for r in rows] == [["burn3"]] and model.calls == 1
    assert result.cost_usd == CALL_USD + Decimal("1.50"), "overshoot of one command, no more"


class CachedTurns(Turns):
    """As Turns, but every call after the first reports cache-read tokens, which PRICING does not
    price: that call's cost is unknown (NFR-11)."""

    async def send(self, request):
        response = await super().send(request)
        if self.calls > 1:
            response = dataclasses.replace(response, usage=Usage(5, 1, 6, cache_read_tokens=2))
        return response


async def _run_cached(store: str, turns: list, budget):
    tenant = tenancy()
    persistence = Persistence.postgres(DSN, create_schema=False) if store == "postgres" else None
    model = CachedTurns()
    runner = Runner({"m": model}, model_registry=registry(), persistence=persistence,
                    workspace_provider=FakeProvider(commands={"quiet": _quiet}))
    try:
        result = await runner.run(agent(), json.dumps(turns), RunConfig(
            tenant_id=tenant, project_id=PROJECT, max_turns=6, budget=budget))
    finally:
        await runner.aclose()
    return result, model


@pytest.mark.parametrize("store", STORES)
async def test_an_unknown_cost_leaves_the_spend_unknown_never_zero(store):
    """M48 pre-review F1 (NFR-11, FR-173): under a token ceiling the run goes on, its spend None."""
    result, model = await _run_cached(store, [[["quiet"]]], BudgetPolicy(run_ceiling_tokens=1000))
    assert result.status is RunStatus.COMPLETED and model.calls == 2, result.error
    assert [e.payload["cost_usd"] for e in result.events if e.event_type is EventType.MODEL_CALLED][1] is None
    assert result.cost_usd is None
    if store == "postgres":
        assert _manifest(result.run_id)[2]["usd"] is None


async def test_an_unknown_cost_under_a_usd_ceiling_stops_the_run():
    """An unknown spend cannot be shown to be inside a USD ceiling, so it counts as reached (FR-68)."""
    result, model = await _run_cached("memory", [[["quiet"]], [["quiet"]], [["quiet"]]],
                                      BudgetPolicy(run_ceiling_usd=Decimal("50")))
    assert result.status is RunStatus.FAILED and result.error == "budget_exceeded"
    assert model.calls == 2 and result.cost_usd is None


async def test_the_plan_tool_refuses_a_plain_runs_lease():
    """M48 pre-review F4: an orchestrator's agent run with RunConfig(budget=...) is refused at its
    first plan, plainly, rather than failing later on a lease with no plan to adopt."""
    from types import SimpleNamespace

    orchestrator = Orchestrator(roles={"worker": agent()}, policy=BudgetPolicy(run_ceiling_usd=Decimal("5")))
    lease = BudgetGovernor.for_single_run(BudgetPolicy(run_ceiling_usd=Decimal("1"))).lease("run")
    scope = SimpleNamespace(_lease=lease, _runner=object(), run_id=str(uuid.uuid4()), artifacts=None)
    with pytest.raises(ValueError, match="Orchestrator.config"):
        await orchestrator._run_plan(scope=scope, nodes=[])


async def test_a_started_run_is_governed_by_its_budget_too():
    tenant = tenancy()
    runner, model = make("memory")
    try:
        handle = await runner.start(agent(), json.dumps([[["quiet"]], [["quiet"]], [["quiet"]]]), RunConfig(
            tenant_id=tenant, project_id=PROJECT, max_turns=6, budget=BudgetPolicy(run_ceiling_usd=Decimal("0.20"))))
        result = await handle.result()
    finally:
        await runner.aclose()
    assert result.status is RunStatus.FAILED and result.error == "budget_exceeded" and model.calls == 2


async def test_each_run_has_its_own_ceiling():
    """A policy is turned into a fresh lease per run: one run's spend never stops another."""
    policy = BudgetPolicy(run_ceiling_usd=Decimal("0.20"))
    first, _, _ = await _run("memory", [[["quiet"]], [["quiet"]]], policy)
    second, _, model = await _run("memory", [], policy)
    assert first.error == "budget_exceeded"
    assert second.status is RunStatus.COMPLETED and model.calls == 1


class Delegating:
    """The parent delegates once, then answers; the child calls `noop` every turn until it is stopped."""

    def __init__(self) -> None:
        self.parent = self.child = 0

    async def send(self, request):
        if "CHILD" in request.messages[0].content:
            self.child += 1
            return respond(calls=[ToolCall(id=f"c{self.child}", name="noop", arguments={})])
        self.parent += 1
        if not any(m.role is Role.ASSISTANT for m in request.messages):
            return respond(calls=[ToolCall(id="d", name="delegate", arguments={})])
        return respond("done")


async def _delegate(store: str, governed: bool):
    tenant = tenancy()
    model, children, leases = Delegating(), [], []
    empty = {"type": "object", "properties": {}, "additionalProperties": False}

    async def delegate(scope: ToolScope) -> str:
        leases.append(scope.budget_lease)
        child = await pool.spawn(
            parent=scope, briefing=Briefing(objective="CHILD work", assigned_role="worker", max_turns=6),
            agent=AgentSpec(id="child", instructions="x", preferred_model=f"m:{MODEL}", tool_profile=("noop",)),
            lease=scope.budget_lease if governed else None)
        children.append(child)
        return child.status.value

    persistence = Persistence.postgres(DSN, create_schema=False) if store == "postgres" else None
    runner = Runner({"m": model}, model_registry=registry(), persistence=persistence, tools=[
        Tool(spec=ToolSpec(name="noop", description="x", input_schema=empty), fn=lambda: "ok"),
        Tool(spec=ToolSpec(name="delegate", description="x", input_schema=empty), fn=delegate)])
    pool = SubagentPool(runner)
    try:
        result = await runner.run(
            AgentSpec(id="parent", instructions="x", preferred_model=f"m:{MODEL}", tool_profile=("delegate",)),
            "PARENT", RunConfig(tenant_id=tenant, project_id=PROJECT, max_turns=6,
                                budget=BudgetPolicy(run_ceiling_usd=Decimal("0.40"))))
    finally:
        await runner.aclose()
    assert len(leases) == 1 and leases[0] is not None, "the delegate tool ran and read the run's lease"
    return result, children, model, leases[0]


@pytest.mark.parametrize("store", STORES)
async def test_a_child_spawned_with_the_scopes_budget_lease_stops_at_the_parents_ceiling(store):
    """M48 round 1 F-A: the run's lease is public on its tool scope, so a delegating tool can pass it
    and the child spends from the parent's ceiling: 0.15 (parent), 0.30, 0.45 (child) crosses 0.40,
    the child stops before a third call, and the parent before its next."""
    result, children, model, lease = await _delegate(store, governed=True)
    assert lease.node_id == "run"
    assert [c.status for c in children] == [RunStatus.FAILED] and model.child == 2
    assert result.status is RunStatus.FAILED and result.error == "budget_exceeded" and model.parent == 1
    assert lease.governor.spend == BudgetAmount(usd=CALL_USD * 3, tokens=CALL_TOKENS * 3)


async def test_a_child_spawned_with_no_lease_is_not_governed_by_the_parents_ceiling():
    """The control: the same delegation passing no lease runs the child to its max_turns, outside the
    ceiling, which is why the README tells a delegating tool to pass the scope's lease."""
    result, children, model, lease = await _delegate("memory", governed=False)
    assert [c.status for c in children] == [RunStatus.MAX_TURNS_EXCEEDED] and model.child == 6
    assert result.status is RunStatus.COMPLETED and model.parent == 2
    assert lease.governor.spend == BudgetAmount(usd=CALL_USD * 2, tokens=CALL_TOKENS * 2)


# =================================================================================================
# FR-172: refused before the run starts
# =================================================================================================


def _governed_lease():
    plan = PlanVersion(plan_id=str(uuid.uuid4()), version=1, run_id=str(uuid.uuid4()), nodes=(
        PlanNode(node_id="a", objective="x", assigned_role="worker",
                 budget_reservation=BudgetReservation(usd=Decimal("1.00"))),))
    return BudgetGovernor(BudgetPolicy(run_ceiling_usd=Decimal("5.00")), plan).lease("a")


@pytest.mark.parametrize("extra", [
    pytest.param({"budget_lease": "lease"}, id="beside a lease"),
    pytest.param({"depth": 1}, id="on a pool-placed child"),
])
async def test_a_budget_where_one_is_already_governed_is_refused_before_the_run_starts(extra):
    if extra.get("budget_lease") == "lease":
        extra = {"budget_lease": _governed_lease()}
    tenant = tenancy()
    runner, model = make("memory")
    try:
        with pytest.raises(ValueError, match="budget"):
            await runner.run(agent(), "[]", RunConfig(tenant_id=tenant, project_id=PROJECT,
                                                     budget=BudgetPolicy(run_ceiling_usd=Decimal("1")), **extra))
    finally:
        await runner.aclose()
    assert model.calls == 0


async def test_a_budget_on_an_orchestrators_run_is_refused():
    orchestrator = Orchestrator(roles={"worker": agent()}, policy=BudgetPolicy(run_ceiling_usd=Decimal("5")))
    config = orchestrator.config(tenant_id=tenancy(), project_id=PROJECT,
                                 budget=BudgetPolicy(run_ceiling_usd=Decimal("1")))
    runner, model = make("memory")
    try:
        with pytest.raises(ValueError, match="budget"):
            await runner.run(orchestrator.agent, "plan", config)
    finally:
        await runner.aclose()
    assert model.calls == 0


async def test_a_budget_that_is_no_policy_is_refused():
    runner, model = make("memory")
    try:
        with pytest.raises(ValueError, match="BudgetPolicy"):
            await runner.run(agent(), "[]", RunConfig(tenant_id=tenancy(), project_id=PROJECT,
                                                     budget={"run_ceiling_usd": "1"}))
    finally:
        await runner.aclose()
    assert model.calls == 0


async def test_a_usd_ceiling_on_an_unpriced_model_is_refused_and_a_token_ceiling_is_not():
    with pytest.raises(UnpricedModel):
        await _run("memory", [], BudgetPolicy(run_ceiling_usd=Decimal("1")), model_id=UNPRICED)
    result, _, model = await _run("memory", [[["quiet"]], [["quiet"]]],
                                  BudgetPolicy(run_ceiling_tokens=CALL_TOKENS + 1), model_id=UNPRICED)
    assert result.status is RunStatus.FAILED and result.error == "budget_exceeded" and model.calls == 2


# =================================================================================================
# Records (FR-172, FR-173) and NFR-50
# =================================================================================================


@pytest.mark.skipif(not DSN, reason="needs DATABASE_URL")
async def test_the_manifest_records_the_policy_the_one_lease_and_the_runs_spend():
    policy = BudgetPolicy(run_ceiling_usd=Decimal("1.00"), run_ceiling_tokens=500, cpu_second_usd=CPU_USD)
    result, _, _ = await _run("postgres", [[["burn1"]]], policy)
    assert result.status is RunStatus.COMPLETED, result.error
    recorded_policy, reservations, spend = _manifest(result.run_id)
    assert Decimal(recorded_policy["run_ceiling_usd"]) == Decimal("1.00")
    assert recorded_policy["run_ceiling_tokens"] == 500
    assert Decimal(recorded_policy["cpu_second_usd"]) == CPU_USD
    assert list(reservations) == ["run"]
    assert Decimal(reservations["run"]["usd"]) == Decimal("1.00") and reservations["run"]["tokens"] == 500
    assert Decimal(spend["usd"]) == result.cost_usd == CALL_USD * 2 + CPU_USD
    assert spend["tokens"] == CALL_TOKENS * 2


@pytest.mark.skipif(not DSN, reason="needs DATABASE_URL")
async def test_a_run_without_a_budget_records_no_budget():
    """NFR-50: no budget, no budget columns, the same terminal path as before M48."""
    result, rows, model = await _run("postgres", [[["quiet"]]])
    assert result.status is RunStatus.COMPLETED and model.calls == 2 and len(rows) == 1
    assert _manifest(result.run_id) == (None, None, None)


async def test_a_run_without_a_budget_is_never_stopped_by_one():
    result, rows, model = await _run("memory", [[["burn3"]], [["burn3"]], [["burn3"]]])
    assert result.status is RunStatus.COMPLETED and model.calls == 4 and len(rows) == 3


def test_the_config_field_defaults_to_none_and_replaces_like_any_other():
    config = RunConfig(tenant_id="t", project_id="p")
    assert config.budget is None
    policy = BudgetPolicy(run_ceiling_usd=Decimal("1"))
    assert dataclasses.replace(config, budget=policy).budget is policy


# =================================================================================================
# On a real daemon (pytest -m docker)
# =================================================================================================

BUSY = "import time;t=time.time();[0 for _ in iter(lambda: time.time()-t<2, False)]"


@pytest.mark.docker
async def test_on_docker_a_busy_loop_crossing_the_ceiling_stops_the_run():
    tenant = tenancy()
    runner, model = make("memory", DockerProvider())
    try:
        result = await runner.run(agent(IMAGE), json.dumps([[["python", "-c", BUSY]], [["python", "-c", BUSY]]]),
                                  RunConfig(tenant_id=tenant, project_id=PROJECT, max_turns=6,
                                            budget=BudgetPolicy(run_ceiling_usd=Decimal("0.50"),
                                                                cpu_second_usd=CPU_USD)))
    finally:
        await runner.aclose()
    assert result.status is RunStatus.FAILED and result.error == "budget_exceeded", result.error
    assert model.calls == 1, "no model call after the command that crossed the ceiling"
    [ended] = _ended(result)
    assert ended["cpu_seconds"] >= 1.5 and Decimal(ended["cost_usd"]) >= Decimal("0.75")
