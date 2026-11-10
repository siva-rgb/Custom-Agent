"""M52: child reservations and the delegation tool (FR-184 to FR-187, AC-110, NFR-53, NFR-54;
DECISION-8b115c86, DECISION-764be0d8).

A run whose spec names `delegate_task` hands a piece of its task to a child through that tool. Each
call reserves `BudgetPolicy.delegation_fraction` of the parent reservation's remainder for the child,
copies the files it names from the parent's workspace into the child's fresh one, runs the child
through a `SubagentPool` at depth 1, and returns the child's answer, run id and status, labelled with
the child's provenance. A child has no delegation tool. Refusals are tool errors; a spawn may need
approval; cancelling the parent cancels its children.

The gates run on `FakeProvider` and a scripted model; the persisted half removes every row it writes,
by its own tenancies.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import uuid
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from agentsdk import AgentSpec, Persistence, RunConfig, RunStatus, Runner
from agentsdk.budget import SINGLE_RUN, BudgetExceeded, BudgetGovernor, BudgetPolicy
from agentsdk.config import normalise_database_url
from agentsdk.delegation import DELEGATE_TOOL, MAX_DELEGATED_FILES, Delegation
from agentsdk.events import EventType
from agentsdk.identity import PrincipalContext
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.postgres import RunScope
from agentsdk.presets import (
    GENERAL_INSTRUCTIONS,
    GENERAL_TOOLS,
    GENERAL_VERSION,
    SUBAGENT_TURNS,
    general_agent,
    general_subagent,
)
from agentsdk.primitives import Message, Role, TaintFlag, ToolCall, TrustZone
from agentsdk.registry import ModelCapabilities, ModelEntry, ModelPricing, ModelRegistry
from agentsdk.scheduler import SchedulerLimits
from agentsdk.subagents import Briefing, SubagentPool
from agentsdk.tools import ApprovalPolicy
from agentsdk.workspace import FakeProvider, WorkspaceSpec

REPO = Path(__file__).resolve().parent.parent
DSN = normalise_database_url(os.environ.get("DATABASE_URL"))
PREFIX = "SYN-m52-"
PROJECT = "p-m52"
STORES = ["memory", "postgres"]
FAKE_IMAGE = "fake/workspace@sha256:" + "0" * 64
MODEL = "known-model"
OPERATOR = PrincipalContext(agent_principal="approval-ui", user_principal="owner")
CEILING = Decimal("5")
FILES = {"data/a.txt": b"alpha\n", "data/b.txt": b"beta\n", "NOTES.md": b"not for the child\n"}
# Every task the scripted model was given, parent and child alike, as its first user message.
TASKS: list[str] = []
# An unknown tool's result, by its provenance source (DECISION-94bfee7c: never by its text).
NOT_FOUND = "urn:agentsdk:tool-error:ToolNotFound"
TOOL_RAISED = "ToolExecutionError"  # ToolCalled.error_type of any failure the tool itself raised


@pytest.fixture(autouse=True)
def remove_what_the_test_wrote():
    yield
    if DSN:
        with psycopg.connect(DSN, autocommit=True) as conn:
            ids = [r[0] for r in conn.execute("SELECT run_id FROM runs WHERE tenant_id LIKE %s", (PREFIX + "%",))]
            conn.execute("DELETE FROM workspace_commands WHERE tenant_id LIKE %s", (PREFIX + "%",))
            conn.execute("DELETE FROM workspaces WHERE tenant_id LIKE %s", (PREFIX + "%",))
            conn.execute("DELETE FROM interruption_decisions WHERE interruption_id IN (SELECT interruption_id"
                         " FROM interruptions WHERE run_id = ANY(%s))", (ids,))
            conn.execute("DELETE FROM interruptions WHERE run_id = ANY(%s)", (ids,))
            conn.execute("DELETE FROM standing_decisions WHERE run_id = ANY(%s)", (ids,))
            conn.execute("DELETE FROM artifacts WHERE source_run = ANY(%s)", (ids,))
            for table in ("plan_node_states", "plan_versions", "run_events", "messages", "execution_manifests"):
                conn.execute(f"DELETE FROM {table} WHERE run_id = ANY(%s)", (ids,))
            while conn.execute("DELETE FROM runs r WHERE run_id = ANY(%s) AND NOT EXISTS "
                               "(SELECT 1 FROM runs c WHERE c.parent_run_id = r.run_id)", (ids,)).rowcount:
                pass


def tenancy() -> str:
    return f"{PREFIX}{uuid.uuid4().hex[:10]}"


def registry() -> ModelRegistry:
    pricing = ModelPricing(input=Decimal("0.01"), output=Decimal("0.10"))
    return ModelRegistry([ModelEntry(provider="test", model_id=MODEL, model_version="1", adapter_version="test/1",
                                     capabilities=ModelCapabilities(max_context_tokens=100_000, pricing=pricing))])


def preset(**fields):
    fields.setdefault("model", f"m:{MODEL}")
    fields.setdefault("image", FAKE_IMAGE)
    fields.setdefault("budget", BudgetPolicy(run_ceiling_usd=CEILING))
    fields.setdefault("registry", registry())
    fields.setdefault("files", FILES)
    fields.setdefault("export_approval", ApprovalPolicy.AUTO)
    fields.setdefault("delegate", True)
    return general_agent(**fields)


def respond(content="", calls=()):
    return ModelResponse(message=Message(role=Role.ASSISTANT, content=content, tool_calls=tuple(calls)),
                         stop_reason=StopReason.TOOL_CALLS if calls else StopReason.END_TURN, usage=Usage(5, 1, 6))


class Turns:
    """The model, parent and child alike: the task's first `[` starts a JSON list of turns, each a list
    of [tool, arguments]; a child's turns are its objective. The answer follows the last turn."""

    async def send(self, request):
        first = next(m for m in request.messages if m.role is Role.USER).content
        turns = json.loads(first[first.index("["):].split("\n")[0])
        turn = sum(1 for m in request.messages if m.role is Role.ASSISTANT)
        if turn == 0:
            TASKS.append(first)
        if turn < len(turns):
            return respond(calls=[ToolCall(id=f"t{turn}-{i}", name=name, arguments=args)
                                  for i, (name, args) in enumerate(turns[turn])])
        seen = {str(r.content)[:200] for m in request.messages if m.role is Role.TOOL for r in m.tool_results}
        return respond("done: " + ",".join(sorted(seen)))


def delegate(child_turns, files=()):
    return [DELEGATE_TOOL, {"objective": json.dumps(child_turns), "files": list(files)}]


async def _cat(cmd):
    cmd.out(cmd.files.read(cmd.argv[1]).decode())
    return 0


class Gate:
    """A command each child runs: it waits until `size` children are inside it at once."""

    def __init__(self, size: int) -> None:
        self.size, self.inside, self.most = size, 0, 0
        self.full = asyncio.Event()

    async def __call__(self, cmd):
        self.inside += 1
        self.most = max(self.most, self.inside)
        if self.inside >= self.size:
            self.full.set()
        try:
            await asyncio.wait_for(self.full.wait(), 10)
        finally:
            self.inside -= 1
        cmd.out("met\n")
        return 0


class Block:
    """A command that never ends until it is cancelled; `started` is set once it runs."""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def __call__(self, cmd):
        self.started.set()
        await asyncio.Event().wait()
        return 0


def provider(**commands):
    return FakeProvider(commands={"cat": _cat, **commands})


def persistence(store):
    return Persistence.postgres(DSN, create_schema=False) if store == "postgres" else None


def runner_for(store="memory", chosen=None, **options):
    return Runner({"m": Turns()}, model_registry=registry(), workspace_provider=chosen or provider(),
                  persistence=persistence(store), **options)


def stored(sql, *args):
    with psycopg.connect(DSN) as conn:
        return conn.execute(sql, args).fetchall()


async def results(runner, run_id, tenant):
    sessions = runner._sessions_for(RunScope(run_id=run_id, tenant_id=tenant, project_id=PROJECT))
    history = await asyncio.to_thread(sessions.history, run_id)
    return {r.tool_call_id: r for m in history for r in m.tool_results}


def called(result) -> dict:
    """Each ToolCalled of the run, by its call id."""
    return {e.payload["tool_call_id"]: e.payload for e in result.events if e.event_type is EventType.TOOL_CALLED}


async def run_parent(store, turns, *, agent=None, chosen=None, runner_options=None, **config):
    tenant = tenancy()
    runner = runner_for(store, chosen, **(runner_options or {}))
    agent = agent or preset()
    try:
        result = await runner.run(agent.spec, json.dumps(turns), agent.config(tenant_id=tenant, project_id=PROJECT,
                                                                              **config))
        got = await results(runner, result.run_id, tenant)
        store_ = runner._workspace_store()
        children = [json.loads(got[call].content)["run_id"] for call, event in called(result).items()
                    if event["name"] == DELEGATE_TOOL and not event["is_error"]]
        spaces = {child: await asyncio.to_thread(store_.workspaces, tenant, PROJECT, child) for child in children if child}
        commands = await asyncio.to_thread(store_.commands, tenant, PROJECT)
        # Read while the Runner is open: in memory a child's history is this Runner's.
        run_parent.children = {child: await results(runner, child, tenant) for child in children if child}
    finally:
        await runner.aclose()
    return result, got, spaces, commands, tenant


# =================================================================================================
# FR-184: nested reservations
# =================================================================================================


def test_the_share_defaults_to_a_quarter_and_is_a_fraction():
    assert BudgetPolicy(run_ceiling_usd=CEILING).delegation_fraction == Decimal("0.25")
    # DECISION-94bfee7c: each refusal by its class beside a control differing in that field alone.
    assert BudgetPolicy(run_ceiling_usd=CEILING, delegation_fraction=Decimal("0.5")).delegation_fraction == Decimal("0.5")
    for bad in (Decimal(0), Decimal(1), Decimal("1.5"), 0.25, Decimal("NaN")):
        with pytest.raises(ValueError):
            BudgetPolicy(run_ceiling_usd=CEILING, delegation_fraction=bad)


def test_a_child_reserves_its_share_of_the_parents_remainder_and_returns_what_it_did_not_spend():
    governor = BudgetGovernor.for_single_run(BudgetPolicy(run_ceiling_usd=Decimal("8"), run_ceiling_tokens=800))
    parent = governor.lease(SINGLE_RUN)
    parent.charge(Usage(100, 0, 100), Decimal("4"))
    child = governor.reserve_child(SINGLE_RUN, "c1")
    assert child.reservation.usd == Decimal("1") and child.reservation.tokens == 175, "a quarter of 4 USD and 700"
    assert parent.reservation.usd == Decimal("7") and parent.reservation.tokens == 625
    child.charge(Usage(25, 0, 25), Decimal("0.25"))
    child.release()
    assert parent.reservation.usd == Decimal("7.75") and parent.reservation.tokens == 775, "the unspent came back"
    assert child.reservation.usd == Decimal("0.25") and child.reservation.tokens == 25, "the child keeps its spend"
    assert governor.spend.usd == Decimal("4.25") and governor.spend.tokens == 125
    total = governor.allocation.reservations
    assert sum(a.usd for a in total.values()) == Decimal("8"), "the reservations still sum to the ceiling"


def test_a_parent_whose_children_hold_its_budget_stops_before_it_overspends():
    governor = BudgetGovernor.for_single_run(BudgetPolicy(run_ceiling_usd=Decimal("4"),
                                                          delegation_fraction=Decimal("0.5")))
    parent = governor.lease(SINGLE_RUN)
    governor.reserve_child(SINGLE_RUN, "c1")
    governor.reserve_child(SINGLE_RUN, "c2")
    assert parent.reservation.usd == Decimal("1")
    parent.charge(Usage(1, 0, 1), Decimal("1"))
    assert not parent.may_call(), "its own remainder is spent though the run's ceiling is not"
    assert governor.lease("c1").may_call() and governor.lease("c2").may_call()


def test_a_reservation_is_refused_when_the_share_is_nothing_or_the_parent_may_not_call():
    tiny = BudgetGovernor.for_single_run(BudgetPolicy(run_ceiling_tokens=3))
    tiny.lease(SINGLE_RUN)
    with pytest.raises(BudgetExceeded):
        tiny.reserve_child(SINGLE_RUN, "c1")
    spent = BudgetGovernor.for_single_run(BudgetPolicy(run_ceiling_usd=Decimal("1")))
    spent.lease(SINGLE_RUN).charge(Usage(1, 0, 1), Decimal("1"))
    with pytest.raises(BudgetExceeded):
        spent.reserve_child(SINGLE_RUN, "c1")
    fresh = BudgetGovernor.for_single_run(BudgetPolicy(run_ceiling_usd=Decimal("1")))
    with pytest.raises(KeyError):
        fresh.reserve_child("nobody", "c1")
    fresh.lease(SINGLE_RUN)
    fresh.reserve_child(SINGLE_RUN, "c1")
    with pytest.raises(ValueError):
        fresh.reserve_child(SINGLE_RUN, "c1")
    fresh.reserve_child(SINGLE_RUN, "c2")  # the control: only the id differs
    assert tiny.allocation.reservations.keys() == {SINGLE_RUN}, "a refusal reserved nothing"


def test_no_child_is_reserved_once_the_run_is_at_its_ceiling_though_the_parent_has_a_remainder():
    """FR-68: a child that overshot its reservation can take the run to its ceiling; the parent's own
    remainder is then not spendable, so no new child starts."""
    governor = BudgetGovernor.for_single_run(BudgetPolicy(run_ceiling_usd=Decimal("4")))
    parent = governor.lease(SINGLE_RUN)
    child = governor.reserve_child(SINGLE_RUN, "c1")
    child.charge(Usage(1, 0, 1), Decimal("4"))  # one call past its 1 USD: the run is at 4
    assert parent.reservation.usd - parent.spent.usd == Decimal("3"), "the parent still holds a remainder"
    with pytest.raises(BudgetExceeded):
        governor.reserve_child(SINGLE_RUN, "c2")
    unknown = BudgetGovernor.for_single_run(BudgetPolicy(run_ceiling_usd=Decimal("4")))
    unknown.lease(SINGLE_RUN).charge(Usage(1, 0, 1), None)  # a call nothing could price
    with pytest.raises(BudgetExceeded):
        unknown.reserve_child(SINGLE_RUN, "c1")


@pytest.mark.parametrize("store", STORES)
async def test_a_childs_unspent_reservation_returns_to_its_parent_when_it_ends(store):
    runner = RecordingRunner({"m": Turns()}, model_registry=registry(), workspace_provider=provider(),
                             persistence=persistence(store))
    tenant, agent = tenancy(), preset()
    try:
        result = await runner.run(agent.spec, json.dumps([[delegate([])]]),
                                  agent.config(tenant_id=tenant, project_id=PROJECT))
    finally:
        await runner.aclose()
    assert result.status is RunStatus.COMPLETED, result.error
    parent = runner.configs[result.run_id].budget_lease
    [child] = [c.budget_lease for run_id, c in runner.configs.items() if run_id != result.run_id]
    assert child.released and child.parent == SINGLE_RUN
    assert child.reservation.usd == child.spent.usd > 0, "the child keeps only what it spent"
    assert parent.reservation.usd == CEILING - child.spent.usd, "the rest came back to the parent"


# =================================================================================================
# FR-185: refusals at construction and before the run
# =================================================================================================


def _space(**fields):
    return WorkspaceSpec(image=FAKE_IMAGE, **fields)


def _child(**fields):
    return general_subagent(model=f"m:{MODEL}", image=FAKE_IMAGE, registry=registry(), **fields)


def test_a_spec_naming_the_tool_needs_a_delegation_a_workspace_and_a_child_that_cannot_delegate():
    # DECISION-94bfee7c: each refusal by its class, beside its control, which differs in one field and builds.
    child = _child()
    control = Delegation(agent=child)
    delegating = AgentSpec(id="c", instructions="x", tool_profile=(DELEGATE_TOOL,), workspace=_space(),
                           delegate=control)
    with pytest.raises(ValueError):  # no delegate
        AgentSpec(id="c", instructions="x", tool_profile=(DELEGATE_TOOL,), workspace=_space())
    with pytest.raises(ValueError):  # no workspace
        AgentSpec(id="c", instructions="x", tool_profile=(DELEGATE_TOOL,), delegate=control)
    with pytest.raises(ValueError):  # a child that delegates
        Delegation(agent=delegating)
    with pytest.raises(ValueError):  # a child with no workspace
        Delegation(agent=AgentSpec(id="c", instructions="x"))
    Delegation(agent=AgentSpec(id="c", instructions="x", workspace=_space()))
    for bad in ({"max_turns": 0}, {"max_turns": True}, {"approval": "auto"}):
        with pytest.raises(ValueError):
            Delegation(agent=child, **bad)
    assert Delegation(agent=child, max_turns=1, approval=ApprovalPolicy.REQUIRE_APPROVAL).max_turns == 1
    with pytest.raises(ValueError):  # not an AgentSpec
        Delegation(agent="general")


def test_a_child_whose_workspace_would_hold_more_than_the_named_files_is_refused():
    """Pre-review F2, NFR-54: a plan's workspace is shared with the parent, and a snapshot starts as a
    copy, so neither child would start with only the files its call names."""
    child = _child()
    Delegation(agent=child)  # the control: scope 'run', no snapshot
    for fields in ({"scope": "plan"}, {"from_snapshot": "snap-1"}):
        with pytest.raises(ValueError):
            Delegation(agent=replace(child, workspace=replace(child.workspace, **fields)))


def test_a_child_may_reach_no_host_its_parent_may_not():
    """Pre-review N1, NFR-54: a spawn never widens what its parent may reach."""
    child = _child(egress=("example.com:443",))
    with pytest.raises(ValueError):  # the control below differs only in the parent's egress
        AgentSpec(id="a", instructions="x", tool_profile=(DELEGATE_TOOL,), workspace=_space(),
                  delegate=Delegation(agent=child))
    AgentSpec(id="a", instructions="x", tool_profile=(DELEGATE_TOOL,), delegate=Delegation(agent=child),
              workspace=_space(egress=("example.com:443", "other.org:443")))


class SameIds(Turns):
    """A provider that numbers its tool calls per response, so ids repeat across turns."""

    async def send(self, request):
        response = await super().send(request)
        calls = tuple(replace(c, id=f"call_{i}") for i, c in enumerate(response.message.tool_calls))
        return replace(response, message=replace(response.message, tool_calls=calls))


@pytest.mark.parametrize("store", STORES)
async def test_every_call_delegates_though_the_provider_repeats_its_call_ids(store):
    """Pre-review F1: the reservation's name never comes from the model's tool-call id."""
    runner = RecordingRunner({"m": SameIds()}, model_registry=registry(), workspace_provider=provider(),
                             persistence=persistence(store))
    tenant, agent = tenancy(), preset()
    try:
        result = await runner.run(agent.spec, json.dumps([[delegate([])], [delegate([])]]),
                                  agent.config(tenant_id=tenant, project_id=PROJECT))
    finally:
        await runner.aclose()
    assert result.status is RunStatus.COMPLETED, result.error
    children = [c.budget_lease.node_id for run_id, c in runner.configs.items() if run_id != result.run_id]
    assert len(children) == 2 and len(set(children)) == 2, children
    assert all(not e.payload["is_error"] for e in result.events if e.event_type is EventType.TOOL_CALLED)


@pytest.mark.parametrize("store", STORES)
async def test_a_delegating_run_with_no_budget_or_placed_by_the_pool_is_refused_before_it_starts(store):
    agent = preset()
    runner = runner_for(store)
    tenant = tenancy()
    lease = BudgetGovernor.for_single_run(BudgetPolicy(run_ceiling_usd=CEILING)).lease(SINGLE_RUN)
    try:
        with pytest.raises(ValueError):  # no budget
            await runner.start(agent.spec, "[]", RunConfig(tenant_id=tenant, project_id=PROJECT))
        with pytest.raises(ValueError):  # placed by the pool
            await runner.start(agent.spec, "[]", RunConfig(tenant_id=tenant, project_id=PROJECT, budget_lease=lease,
                                                           depth=1))
        if store == "postgres":
            assert stored("SELECT count(*) FROM runs WHERE tenant_id = %s", tenant) == [(0,)], "nothing was created"
        # The control: the lease and no depth, each refusal's one difference, runs.
        result = await runner.run(agent.spec, "[]", RunConfig(tenant_id=tenant, project_id=PROJECT, budget_lease=lease))
    finally:
        await runner.aclose()
    assert result.status is RunStatus.COMPLETED, result.error


# =================================================================================================
# FR-185, FR-186: one call
# =================================================================================================


@pytest.mark.parametrize("store", STORES)
async def test_a_delegated_piece_is_done_by_a_child_with_only_the_named_files(store):
    child_turns = [[["run_command", {"argv": ["cat", "data/a.txt"]}]]]
    TASKS.clear()
    result, got, spaces, commands, tenant = await run_parent(
        store, [[delegate(child_turns, ["data/a.txt", "data/b.txt"])]])
    assert result.status is RunStatus.COMPLETED, result.error
    report = json.loads(got["t0-0"].content)
    child = report["run_id"]
    assert report["status"] == "completed" and "alpha" in report["answer"], report
    briefed = [t for t in TASKS if t.startswith(json.dumps(child_turns))]
    assert briefed == [json.dumps(child_turns) + "\n\nFiles copied into your workspace for this task: "
                                                 "data/a.txt, data/b.txt."], "the briefing names the files"
    [row] = spaces[child]
    assert row.limits["files"] == {p: hashlib.sha256(FILES[p]).hexdigest() for p in ("data/a.txt", "data/b.txt")}, \
        "only the named files, by SHA-256; NOTES.md stayed with the parent"
    assert [c.argv for c in commands if c.run_id == child] == [["cat", "data/a.txt"]]
    if store == "postgres":
        [(parent, agent_id, depth_turns)] = stored(
            "SELECT parent_run_id::text, agent_spec_id, max_turns FROM runs WHERE run_id = %s::uuid", child)
        assert (parent, agent_id, depth_turns) == (result.run_id, "general-subagent", SUBAGENT_TURNS)
        [(reservations,)] = stored("SELECT budget_reservations FROM execution_manifests WHERE run_id = %s::uuid", child)
        [first] = [Decimal(e.payload["cost_usd"]) for e in result.events if e.event_type is EventType.MODEL_CALLED][:1]
        [(name, held)] = reservations.items()
        assert name.startswith("delegated:") and len(name) == len("delegated:") + 32, name
        assert held == {"usd": str((CEILING - first) * Decimal("0.25")), "tokens": None}


@pytest.mark.parametrize("store", STORES)
async def test_a_childs_answer_carries_the_taint_of_what_it_read(store):
    tainted = [[["run_command", {"argv": ["cat", "data/a.txt"]}]]]
    clean: list = []
    result, got, _, _, _ = await run_parent(store, [[delegate(tainted, ["data/a.txt"]), delegate(clean)]])
    assert result.status is RunStatus.COMPLETED, result.error
    dirty, plain = got["t0-0"].provenance, got["t0-1"].provenance
    assert TaintFlag.EXECUTABLE_CONTENT in dirty.taint_flags and dirty.trust_zone is TrustZone.UNTRUSTED
    assert TaintFlag.EXECUTABLE_CONTENT not in plain.taint_flags, "the control: a child that read nothing"


@pytest.mark.parametrize("store", STORES)
async def test_calls_in_one_response_run_their_children_at_once(store):
    gate = Gate(2)
    meet = [[["run_command", {"argv": ["meet"]}]]]
    result, got, _, _, _ = await run_parent(store, [[delegate(meet), delegate(meet)]], chosen=provider(meet=gate))
    assert result.status is RunStatus.COMPLETED, result.error
    assert gate.most == 2, "both children were inside the command together"
    assert [json.loads(got[i].content)["status"] for i in ("t0-0", "t0-1")] == ["completed", "completed"]


# =================================================================================================
# FR-185: refusals are tool errors
# =================================================================================================


@pytest.mark.parametrize("store", STORES)
@pytest.mark.parametrize("files, why", [
    (["../outside"], "pattern"),
    (["missing.txt"], "missing"),
    ([f"f{i}" for i in range(MAX_DELEGATED_FILES + 1)], "maxItems"),
    (["data"], "folder"),
], ids=["outside", "missing", "too-many", "a-folder"])
async def test_a_call_refused_by_its_files_spawns_nothing_and_takes_no_task_count(store, files, why):
    limits = SchedulerLimits(max_tasks_per_run=1)
    result, got, spaces, _, _ = await run_parent(
        store, [[delegate([], files)], [delegate([])]], runner_options={"scheduler_limits": limits})
    assert result.status is RunStatus.COMPLETED, result.error
    assert called(result)["t0-0"]["is_error"] is True, why
    second = json.loads(got["t1-0"].content)
    assert second["status"] == "completed", "the refused call took no task count: the one allowed still runs"
    assert list(spaces) == [second["run_id"]], "the refused call spawned no run"


@pytest.mark.parametrize("store", STORES)
async def test_files_past_the_childs_bound_are_refused_before_a_run(store):
    fits = preset(files={"big.bin": b"x" * 4096}, max_workspace_bytes=4096 + 1)
    small = replace(fits.spec.delegate, agent=replace(
        fits.spec.delegate.agent, workspace=replace(fits.spec.delegate.agent.workspace, max_workspace_bytes=1024)))
    agent = replace(fits, spec=replace(fits.spec, delegate=small))
    result, got, spaces, _, _ = await run_parent(store, [[delegate([], ["big.bin"])]], agent=agent)
    # Every failure a tool raises reaches the model as ToolExecutionError (README, M42), so the cause is
    # shown by a control, never by the text (DECISION-94bfee7c).
    assert called(result)["t0-0"]["error_type"] == TOOL_RAISED
    assert not spaces
    # The control: the same call, with the child's bound the only change, runs.
    result, got, spaces, _, _ = await run_parent(store, [[delegate([], ["big.bin"])]], agent=fits)
    assert called(result)["t0-0"]["is_error"] is False and len(spaces) == 1


@pytest.mark.parametrize("store", STORES)
async def test_a_call_past_the_task_limit_or_the_budget_share_is_a_tool_error(store):
    limits = SchedulerLimits(max_tasks_per_run=1)
    result, got, spaces, _, _ = await run_parent(store, [[delegate([])], [delegate([])]],
                                                 runner_options={"scheduler_limits": limits})
    assert result.status is RunStatus.COMPLETED, result.error
    # The same call ran once; only the count differs (the cause by a control, as above).
    assert called(result)["t0-0"]["is_error"] is False and called(result)["t1-0"]["error_type"] == TOOL_RAISED
    assert len(spaces) == 1
    tiny = preset(budget=BudgetPolicy(run_ceiling_tokens=9))  # its first call spends 6: a share of 0 tokens
    result, got, spaces, _, _ = await run_parent(store, [[delegate([])]], agent=tiny)
    assert called(result)["t0-0"]["error_type"] == TOOL_RAISED
    assert not spaces, "a refused reservation spawned nothing"
    # The control: the same call under a ceiling that leaves a share runs.
    roomy = preset(budget=BudgetPolicy(run_ceiling_tokens=1000))
    result, got, spaces, _, _ = await run_parent(store, [[delegate([])]], agent=roomy)
    assert called(result)["t0-0"]["is_error"] is False and len(spaces) == 1


@pytest.mark.parametrize("store", STORES)
async def test_a_child_that_tries_to_delegate_is_refused_as_an_unknown_tool(store):
    nested = [[delegate([])]]
    result, got, spaces, _, tenant = await run_parent(store, [[delegate(nested)]])
    report = json.loads(got["t0-0"].content)
    child_results = run_parent.children[report["run_id"]]
    assert report["status"] == "completed"
    assert [r.is_error for r in child_results.values()] == [True]
    assert next(iter(child_results.values())).provenance.source_uri_or_hash == NOT_FOUND
    assert len(spaces) == 1, "no run reached depth 2"


# =================================================================================================
# FR-185, FR-186: cancellation and approval
# =================================================================================================


@pytest.mark.parametrize("store", STORES)
async def test_cancelling_the_parent_cancels_its_running_child_and_removes_its_workspace(store):
    block = Block()
    chosen = provider(block=block)
    runner = runner_for(store, chosen)
    tenant, agent = tenancy(), preset()
    try:
        handle = await runner.start(agent.spec, json.dumps([[delegate([[["run_command", {"argv": ["block"]}]]])]]),
                                    agent.config(tenant_id=tenant, project_id=PROJECT))
        await asyncio.wait_for(block.started.wait(), 30)
        handle.cancel()
        result = await asyncio.wait_for(handle.result(), 30)
        rows = await asyncio.to_thread(runner._workspace_store().workspaces, tenant, PROJECT)
    finally:
        await runner.aclose()
    assert result.status is RunStatus.CANCELLED
    [child_row] = [r for r in rows if r.run_id != result.run_id]
    assert child_row.removed_at is not None, "the child's workspace was removed"
    if store == "postgres":
        [(status,)] = stored("SELECT status FROM runs WHERE run_id = %s::uuid", child_row.run_id)
        assert status == "cancelled"


@pytest.mark.parametrize("store", STORES)
@pytest.mark.parametrize("approve", [True, False], ids=["approved", "refused"])
async def test_a_spawn_under_require_approval_waits_and_a_refused_one_spawns_nothing(store, approve):
    runner = runner_for(store)
    tenant, agent = tenancy(), preset(delegate_approval=ApprovalPolicy.REQUIRE_APPROVAL)
    asked: list = []

    def children() -> list:
        """The runs this Runner started under the parent: the pool's children, on either store."""
        return [key for key, parent in runner._parents.items() if parent == handle.run_id]

    try:
        handle = await runner.start(agent.spec, json.dumps([[delegate([])]]),
                                    agent.config(tenant_id=tenant, project_id=PROJECT))

        async def operator():
            while True:
                for view in await runner.interruptions(tenant, PROJECT, run_id=handle.run_id):
                    asked.append((view.interruption.tool_name, len(children())))
                    await runner.decide(tenant, PROJECT, view.interruption.interruption_id, approve=approve,
                                        principal=OPERATOR)
                await asyncio.sleep(0.02)

        approving = asyncio.ensure_future(operator())
        try:
            result = await asyncio.wait_for(handle.result(), 30)
        finally:
            approving.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await approving
        started = children()
    finally:
        await runner.aclose()
    assert result.status is RunStatus.COMPLETED, result.error
    assert asked == [(DELEGATE_TOOL, 0)], "one interruption, before any child run"
    assert len(started) == (1 if approve else 0)
    assert called(result)["t0-0"]["is_error"] is (not approve)


# =================================================================================================
# FR-186 and NFR-53: the preset, and what does not change
# =================================================================================================


def test_the_delegating_preset_gives_the_general_subagent_its_documented_turns():
    agent = preset()
    assert agent.spec.tool_profile == (*GENERAL_TOOLS, DELEGATE_TOOL)
    child = agent.spec.delegate.agent
    assert child == general_subagent(model=f"m:{MODEL}", image=FAKE_IMAGE, registry=registry(),
                                     export_approval=ApprovalPolicy.AUTO)
    assert agent.spec.delegate.max_turns == SUBAGENT_TURNS and agent.spec.delegate.approval is ApprovalPolicy.AUTO
    assert GENERAL_VERSION == "general-v5" and agent.spec.preset == ("general", "general-v5")
    assert DELEGATE_TOOL in GENERAL_INSTRUCTIONS


def test_the_delegating_presets_child_has_the_parents_egress_and_limits():
    agent = preset(egress=("example.com:443",), memory_bytes=256 * 1024 * 1024)
    child = agent.spec.delegate.agent.workspace
    assert child.egress == agent.spec.workspace.egress == ("example.com:443",)
    assert child.memory_bytes == agent.spec.workspace.memory_bytes == 256 * 1024 * 1024
    assert child.files == {}, "the child starts with only what a call names"


class RecordingRunner(Runner):
    """Keeps each run's configuration, so a child's is read on either store."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.configs: dict = {}

    def _open(self, spec, config):
        opened = super()._open(spec, config)
        self.configs[opened.scope.run_id] = config
        return opened


@pytest.mark.parametrize("store", STORES)
async def test_a_child_has_its_parents_tenant_project_and_principal(store):
    """NFR-54: a spawn never widens what its parent may reach."""
    principal = PrincipalContext(agent_principal="parent-agent", user_principal="someone")
    runner = RecordingRunner({"m": Turns()}, model_registry=registry(), workspace_provider=provider(),
                             persistence=persistence(store))
    tenant, agent = tenancy(), preset()
    try:
        result = await runner.run(agent.spec, json.dumps([[delegate([])]]),
                                  agent.config(tenant_id=tenant, project_id=PROJECT, principal_context=principal))
    finally:
        await runner.aclose()
    [child] = [c for run_id, c in runner.configs.items() if run_id != result.run_id]
    assert (child.tenant_id, child.project_id, child.principal_context) == (tenant, PROJECT, principal)
    assert child.parent_run_id == result.run_id and child.depth == 1
    if store == "postgres":
        [(recorded,)] = stored("SELECT principal_context FROM runs WHERE parent_run_id = %s::uuid", result.run_id)
        assert recorded == principal.to_json()


def test_without_delegate_the_preset_has_no_delegation():
    agent = preset(delegate=False)
    assert agent.spec.tool_profile == GENERAL_TOOLS and agent.spec.delegate is None


@pytest.mark.parametrize("store", ["postgres"])
async def test_a_run_without_the_tool_records_no_delegation_fraction(store):
    plain = preset(delegate=False)
    result, _, _, _, _ = await run_parent(store, [], agent=plain)
    delegating, _, _, _, _ = await run_parent(store, [], agent=preset())
    [(policy,)] = stored("SELECT budget_policy FROM execution_manifests WHERE run_id = %s::uuid", result.run_id)
    [(named,)] = stored("SELECT budget_policy FROM execution_manifests WHERE run_id = %s::uuid", delegating.run_id)
    assert "delegation_fraction" not in policy, "NFR-53: the manifest it recorded before M52"
    assert named["delegation_fraction"] == "0.25"


async def test_the_pool_still_places_children_as_before():
    """NFR-53: the orchestrator's spawn path is untouched -- a pool child with no lease runs."""
    runner = runner_for()
    tenant = tenancy()
    try:
        pool = SubagentPool(runner)
        got = await pool.spawn(parent=RunScope(run_id=str(uuid.uuid4()), tenant_id=tenant, project_id=PROJECT),
                               briefing=Briefing(objective="[]", assigned_role="r"),
                               agent=AgentSpec(id="plain", instructions="x"))
    finally:
        await runner.aclose()
    assert got.status is RunStatus.COMPLETED


class Planner:
    """Submits a one-node plan for 'worker' twice, then gives up."""

    def __init__(self):
        self.calls = 0

    async def send(self, request):
        self.calls += 1
        if self.calls <= 2:
            plan = {"nodes": [{"node_id": "n", "objective": "go", "assigned_role": "worker", "dependencies": []}]}
            return respond(calls=[ToolCall(id=f"plan-{self.calls}", name="run_plan", arguments=plan)])
        return respond("gave up")


class NeverCalled:
    calls = 0

    async def send(self, request):
        NeverCalled.calls += 1
        return respond("ran")


@pytest.mark.parametrize("delegating", [True, False], ids=["delegating", "control"])
async def test_a_delegating_plan_role_is_refused_at_submission_and_spends_nothing(delegating):
    """M52 round 1 F2: a pool child cannot delegate, and the Orchestrator places every role as one, so a
    delegating role is a configuration error at submission, as C6 (M21) made every unrunnable role --
    not a node's failure at spawn that invites a replan. The control is the same role without the tool."""
    from agentsdk.orchestrator import Orchestrator

    NeverCalled.calls = 0
    worker = preset(delegate=delegating).spec
    orchestrator = Orchestrator(roles={"worker": worker}, policy=BudgetPolicy(run_ceiling_usd=CEILING))
    runner = Runner({"p": Planner(), "m": NeverCalled()}, tools=[orchestrator.tool], model_registry=registry(),
                    workspace_provider=provider())
    try:
        result = await runner.run(orchestrator.agent, "do it", orchestrator.config(
            tenant_id=tenancy(), project_id=PROJECT, model_override=f"p:{MODEL}"))
        plans = orchestrator.plans(result.run_id)
    finally:
        await runner.aclose()
    if delegating:
        assert result.status is RunStatus.FAILED
        assert plans == () and NeverCalled.calls == 0 and len(runner._started) == 1, "a child was spawned"
    else:
        assert plans != () and NeverCalled.calls > 0, "the control's role ran"


# =================================================================================================
# FR-187: example 44
# =================================================================================================


def _example(name: str):
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py").replace("-", "_"), REPO / "scripts" / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EX44 = _example("44_delegation.py")
EX44_CHECKS = 4


def _verdicts(checks) -> list:
    return [passed for _, passed, _ in checks]


@pytest.mark.parametrize("store", STORES)
async def test_example_44_holds_every_check_offline(store):
    checks, runs = await EX44.offline(store, "fake", tenant=tenancy(), quiet=True)
    assert _verdicts(checks) == [True] * EX44_CHECKS, checks
    assert runs


def test_example_44_runs_offline_as_a_script():
    done = subprocess.run([sys.executable, str(REPO / "scripts" / "44_delegation.py"), "--offline"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240,
                          env={**os.environ, "PYTHONPATH": str(REPO), "PYTHONIOENCODING": "utf-8"})
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
    assert done.stdout.count("[PASS]") == EX44_CHECKS and "[FAIL]" not in done.stdout


async def test_example_44_refuses_an_export_the_child_asks_for_rather_than_waiting(monkeypatch):
    """Found live in M52: the gateway model's child exported a file, and the run waited for a person.
    Each export of checks 1 and 2 is refused, so the run ends."""
    turns, answer = EX44.SCRIPTS[EX44.CHILD_SUM]
    exporting = [[("export_workspace_file", {"path": EX44.NAMED})], *turns]
    monkeypatch.setitem(EX44.SCRIPTS, EX44.CHILD_SUM, (exporting, answer))
    checks, _ = await asyncio.wait_for(EX44.offline("memory", "fake", tenant=tenancy(), quiet=True), 60)
    assert _verdicts(checks) == [True] * EX44_CHECKS, checks


@pytest.mark.parametrize("program, passes", [
    ("total = sum(int(n) for n in open('data/a.txt'))", True),
    ("total = 42", False),
], ids=["a-program-reading-the-file", "a-program-that-does-not"])
async def test_example_44_counts_a_program_the_child_wrote_that_reads_the_file(monkeypatch, program, passes):
    """Found live in M52: the gateway model's child ran `python3 data/sum.py`, which names no file, though
    the program it wrote reads data/a.txt. The fake's cat stands in for running it."""
    _, answer = EX44.SCRIPTS[EX44.CHILD_SUM]
    turns = [[("write_workspace_file", {"path": "data/sum.py", "content": program})],
             [("run_command", {"argv": ["cat", "data/sum.py"]})]]
    monkeypatch.setitem(EX44.SCRIPTS, EX44.CHILD_SUM, (turns, answer))
    checks, _ = await EX44.offline("memory", "fake", tenant=tenancy(), quiet=True)
    assert _verdicts(checks)[0] is passes, checks[0]


@pytest.mark.parametrize("path, passes", [("data/a.txt", True), ("data/other.txt", False)],
                         ids=["the-named-file", "another-file"])
async def test_example_44_counts_a_read_call_of_the_named_file(monkeypatch, path, passes):
    """Found live in M52: the gateway model's child read data/a.txt with read_workspace_file, then ran a
    program holding the numbers it read."""
    _, answer = EX44.SCRIPTS[EX44.CHILD_SUM]
    turns = [[("write_workspace_file", {"path": "data/other.txt", "content": "1\n"})],
             [("read_workspace_file", {"path": path})]]
    monkeypatch.setitem(EX44.SCRIPTS, EX44.CHILD_SUM, (turns, answer))
    checks, _ = await EX44.offline("memory", "fake", tenant=tenancy(), quiet=True)
    assert _verdicts(checks)[0] is passes, checks[0]


async def test_example_44_fails_the_piece_check_when_the_child_gets_every_file(monkeypatch):
    """The subject removed: the delegation copies the parent's whole workspace, not the named files."""
    from agentsdk import delegation

    copied = []
    original = delegation.named_files

    async def everything(binding, paths):
        copied.append(paths)
        return await original(binding, [*paths, *EX44.EXTRA])

    monkeypatch.setattr(delegation, "named_files", everything)
    checks, _ = await EX44.offline("memory", "fake", tenant=tenancy(), quiet=True)
    assert copied, "the patch never fired"
    assert _verdicts(checks)[0] is False, checks[0]


async def test_example_44_fails_the_reservation_check_without_the_childs_reservation(monkeypatch):
    """The subject removed: the child runs on its parent's own lease."""
    copied = []

    def parents(self, parent_node_id, child_id):
        copied.append(child_id)
        return self.lease(parent_node_id)

    monkeypatch.setattr(BudgetGovernor, "reserve_child", parents)
    checks, _ = await EX44.offline("memory", "fake", tenant=tenancy(), quiet=True)
    assert copied, "the patch never fired"
    assert _verdicts(checks)[1] is False, checks[1]


async def test_example_44_fails_the_depth_check_when_a_child_may_delegate(monkeypatch):
    """The subject removed: the child is given the delegation tool, and its run is not refused."""
    from agentsdk import api, delegation

    widened, unrefused = [], []
    original = delegation.child_spec

    def widen(agent, files):
        widened.append(agent.id)
        spec = original(agent, files)
        return replace(spec, tool_profile=(*spec.tool_profile, DELEGATE_TOOL), delegate=Delegation(agent=agent))

    monkeypatch.setattr(delegation, "child_spec", widen)
    monkeypatch.setattr(api, "_refuse_delegation", lambda spec, config: unrefused.append(spec.id))
    checks, _ = await EX44.offline("memory", "fake", tenant=tenancy(), quiet=True)
    assert widened, "the patch never fired"
    assert _verdicts(checks)[2] is False, checks[2]


async def test_example_44_fails_the_approval_check_when_the_spawn_does_not_ask(monkeypatch):
    built = []
    original = EX44.general_agent

    def unasked(**fields):
        built.append(fields)
        return original(**{**fields, "delegate_approval": ApprovalPolicy.AUTO})

    monkeypatch.setattr(EX44, "general_agent", unasked)
    checks, _ = await EX44.offline("memory", "fake", tenant=tenancy(), quiet=True)
    assert built, "the patch never fired"
    assert _verdicts(checks)[3] is None, checks[3]


@pytest.mark.docker
@pytest.mark.parametrize("store", STORES)
async def test_on_docker_example_44_holds_every_check(store):
    checks, _ = await EX44.offline(store, "docker", tenant=tenancy(), quiet=True)
    assert _verdicts(checks) == [True] * EX44_CHECKS, checks
