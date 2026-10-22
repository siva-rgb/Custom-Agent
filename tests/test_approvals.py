"""M30 gate: the approval manager in single runs (FR-113 to FR-117, AC-86, NFR-30 to NFR-32).

A call that requires approval -- by its tool's declared policy, a predicate over its
arguments, or a hook -- is recorded as an interruption after step 5 and waits for a
decision in the store. Every approval of a turn is asked at once, and from the first
pending call onward the calls run in issue order once every decision is in (P4-D13): an
approved call runs once, a rejected or expired one returns its rejection to the model,
and the model is not called while any call is pending. Decisions are rows bound to their
tenant, project and principal (P4-D5), sticky ones decide the run's not yet decided calls
flagged by policy (P4-D15), and cancelling a waiting run decides its interruptions
`cancel` (P4-D14).

It opens with M30's pre-flight: the findings of M29's review (KNOWLEDGE-7ebaabe3).

Store calls made from these async tests go to a worker thread (FR-81). The persisted half
needs DATABASE_URL and fails rather than skips without it; every row written is removed.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest

from agentsdk import AgentSpec, Persistence, RunConfig, RunStatus, Runner
from agentsdk.calibration import calibrate
from agentsdk.config import normalise_database_url
from agentsdk.errors import ToolApprovalCheckFailed, ToolApprovalRejected
from agentsdk.events import EventType
from agentsdk.gate import EvidenceVerification, NodeSubject
from agentsdk.hooks import HookAction, HookOutcome, RuntimeHook
from agentsdk.identity import PrincipalContext
from agentsdk.interruptions import (
    InMemoryInterruptionStore,
    InterruptionAlreadyDecided,
    InterruptionDecision,
    InterruptionExpired,
    InterruptionNotWaiting,
    StandingDecision,
    UnknownInterruption,
)
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.outcomes import InterruptionKind, RunInterruption
from agentsdk.permissions import AllowlistPermissionChecker
from agentsdk.postgres import PostgresInterruptionStore, RunScope
from agentsdk.primitives import ContentProvenance, Message, Role, ToolCall
from agentsdk.tools import ApprovalPolicy, Tool, ToolSpec

REPO = Path(__file__).resolve().parent.parent
DSN = normalise_database_url(os.environ.get("DATABASE_URL"))
TENANT, PROJECT, OTHER_TENANT = "SYN-m30", "p-m30", "SYN-m30-other"
STORES = ["memory", "postgres"]
APPROVER = PrincipalContext(agent_principal="approval-ui", user_principal="alice")
SCHEMA = {"type": "object", "properties": {"x": {"type": "string"}}, "additionalProperties": True}


@pytest.fixture(autouse=True)
def remove_what_the_test_wrote():
    yield
    if DSN:
        with psycopg.connect(DSN, autocommit=True) as conn:
            ids = [r[0] for r in conn.execute("SELECT run_id FROM runs WHERE tenant_id = ANY(%s)",
                                              ([TENANT, OTHER_TENANT],))]
            conn.execute("DELETE FROM interruption_decisions WHERE interruption_id IN (SELECT interruption_id"
                         " FROM interruptions WHERE run_id = ANY(%s))", (ids,))
            conn.execute("DELETE FROM interruptions WHERE run_id = ANY(%s)", (ids,))
            conn.execute("DELETE FROM standing_decisions WHERE run_id = ANY(%s)", (ids,))
            for table in ("plan_node_states", "plan_versions", "run_events", "messages", "execution_manifests"):
                conn.execute(f"DELETE FROM {table} WHERE run_id = ANY(%s)", (ids,))
            while conn.execute("DELETE FROM runs r WHERE run_id = ANY(%s) AND NOT EXISTS "
                               "(SELECT 1 FROM runs c WHERE c.parent_run_id = r.run_id)", (ids,)).rowcount:
                pass


def respond(content="", calls=()):
    return ModelResponse(message=Message(role=Role.ASSISTANT, content=content, tool_calls=tuple(calls)),
                         stop_reason=StopReason.TOOL_CALLS if calls else StopReason.END_TURN, usage=Usage(5, 1, 6))


class Script:
    """Turn i makes the calls `turns[i]`, each (tool, arguments, call id); then answers.
    `sent` counts the requests the model received."""

    def __init__(self, *turns):
        self.turns, self.sent, self.seen = list(turns), 0, []

    async def send(self, request):
        self.sent += 1
        self.seen.append(request)
        turn = sum(1 for m in request.messages if m.role is Role.ASSISTANT)
        if turn < len(self.turns):
            return respond(calls=[ToolCall(id=cid, name=name, arguments=args) for name, args, cid in self.turns[turn]])
        return respond("done")


class Ran:
    """What each tool ran, in order, with the arguments it ran with."""

    def __init__(self):
        self.calls = []

    def tool(self, name, *, policy=ApprovalPolicy.REQUIRE_APPROVAL, needs_approval=None, describe=None, safe=False):
        def fn(**arguments):
            self.calls.append((name, dict(arguments)))
            return f"{name} ran"
        return Tool(spec=ToolSpec(name=name, description=name, input_schema=SCHEMA, approval_policy=policy,
                                  concurrency_safe=safe), fn=fn, needs_approval=needs_approval, describe=describe)


# Every tool the tests register: the profile is an allowlist, and a denied call is never
# asked about (FR-113), so an agent allowed nothing would test nothing.
TOOLS = ("policy", "predicate", "hooked", "free", "described", "a", "b", "c", "other", "flagged", "w", "see", "unstorable")
AGENT = AgentSpec(id="agent", instructions="act", preferred_model="m:x", tool_profile=TOOLS)


def runner_for(store, tools, model, hook=None):
    return Runner({"m": model}, tools=tools, hook=hook,
                  persistence=Persistence.postgres(DSN, create_schema=False) if store == "postgres" else None)


def config(tenant=TENANT, **fields):
    return RunConfig(tenant_id=tenant, project_id=PROJECT, max_turns=6, **fields)


async def pending(runner, run_id, count, tenant=TENANT, timeout=10.0):
    """Wait until the run has `count` pending interruptions, and return them."""
    deadline = time.monotonic() + timeout
    while True:
        views = await runner.interruptions(tenant, PROJECT, run_id=run_id, pending_only=True)
        if len(views) >= count:
            # A turn's interruptions share created_at, so the store's order among them is
            # their ids': sorted by call id, they come back in issue order.
            return sorted(views, key=lambda v: v.interruption.tool_call_id)
        assert time.monotonic() < deadline, f"only {len(views)} of {count} interruptions pending"
        await asyncio.sleep(0.01)


def results_of(model):
    """Every tool result the model was sent, by call id, from its last request."""
    last = model.seen[-1]
    return {r.tool_call_id: r for m in last.messages for r in m.tool_results}


def events(result, kind):
    return [e for e in result.events if e.event_type is kind]


# =================================================================================================
# M30 pre-flight: M29's review findings (KNOWLEDGE-7ebaabe3)
# =================================================================================================


def _interruption(run_id, tenant=TENANT):
    now = datetime.now(timezone.utc)
    return RunInterruption(
        kind=InterruptionKind.TOOL_APPROVAL, run_id=run_id, request_payload={"tool": "t"}, tool_call_id="c",
        created_at=now, tenant_id=tenant, project_id=PROJECT, top_run_id=run_id, tool_name="t", agent_id="agent",
        trigger="policy", expires_at=now + timedelta(hours=1))


def _standing(run_id, tenant=TENANT):
    return StandingDecision(standing_id=str(uuid.uuid4()), run_id=run_id, tenant_id=tenant, project_id=PROJECT,
                            agent_id="agent", tool_name="t", decision="approve", principal=APPROVER,
                            created_at=datetime.now(timezone.utc))


def _standing_decision(item, standing_id):
    return InterruptionDecision(decision_id=str(uuid.uuid4()), interruption_id=item.interruption_id,
                                tenant_id=item.tenant_id, project_id=PROJECT, decision="approve", source="standing",
                                standing_id=standing_id, decided_at=datetime.now(timezone.utc))


async def _runs(store):
    runner = runner_for(store, [], Script())
    made = [(await runner.run(AGENT, "go", config(tenant=t))).run_id for t in (TENANT, TENANT, OTHER_TENANT)]
    return runner, made


@pytest.mark.parametrize("store", STORES)
async def test_a_decision_may_apply_only_a_standing_decision_of_its_own_run(store):
    """M29 review finding 1: the guard that keeps a decision from naming another run's or
    tenant's standing decision is now tested on both stores."""
    runner, (run, sibling, other) = await _runs(store)
    rows = runner._interruption_store()
    item = _interruption(run)
    await asyncio.to_thread(rows.append_interruption, item)
    for owner, tenant in ((sibling, TENANT), (other, OTHER_TENANT)):
        theirs = await asyncio.to_thread(rows.append_standing, _standing(owner, tenant))
        with pytest.raises(LookupError):
            await asyncio.to_thread(rows.append_decision, _standing_decision(item, theirs.standing_id))
    mine = await asyncio.to_thread(rows.append_standing, _standing(run))
    await asyncio.to_thread(rows.append_decision, _standing_decision(item, mine.standing_id))


def test_migration_0015_makes_the_standing_reference_tenant_composite():
    async def setup():
        runner, (run, _, other) = await _runs("postgres")
        rows = runner._interruption_store()
        item = _interruption(run)
        await asyncio.to_thread(rows.append_interruption, item)
        theirs = await asyncio.to_thread(rows.append_standing, _standing(other, OTHER_TENANT))
        return item, theirs

    item, theirs = asyncio.run(setup())
    with psycopg.connect(DSN) as conn:
        definitions = [r[0] for r in conn.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = 'interruption_decisions'::regclass"
            " AND contype = 'f'").fetchall()]
        assert any(d.startswith("FOREIGN KEY (standing_id, tenant_id, project_id)") for d in definitions), definitions
        assert not any(d.startswith("FOREIGN KEY (standing_id) ") for d in definitions), definitions
    with psycopg.connect(DSN) as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute("INSERT INTO interruption_decisions (decision_id, interruption_id, tenant_id, project_id,"
                         " decision, source, standing_id, decided_at) VALUES (%s, %s, %s, %s, 'approve', 'standing',"
                         " %s, now())", (str(uuid.uuid4()), item.interruption_id, TENANT, PROJECT, theirs.standing_id))


@pytest.mark.parametrize("store", STORES)
async def test_listing_with_an_unreadable_run_id_lists_nothing(store):
    """M29 review finding 2: an unreadable run_id was read as no filter in memory."""
    runner, (run, _, _) = await _runs(store)
    rows = runner._interruption_store()
    await asyncio.to_thread(rows.append_interruption, _interruption(run))
    assert await asyncio.to_thread(rows.interruptions, TENANT, PROJECT, run_id="not-a-uuid") == []
    assert len(await asyncio.to_thread(rows.interruptions, TENANT, PROJECT)) == 1


@pytest.mark.parametrize("store", STORES)
async def test_only_a_tool_approval_is_recorded(store):
    runner, (run, _, _) = await _runs(store)
    rows = runner._interruption_store()
    # Edited at M36 (FR-133): an MCP server's input request is recorded too, as one of the two
    # input kinds with trigger server. Every other kind is still refused, and so is an input kind
    # under an approval's trigger -- this row's -- so no approval is recorded as an input request.
    for kind in InterruptionKind:
        if kind is not InterruptionKind.TOOL_APPROVAL:
            with pytest.raises(ValueError, match="tool approval or an input request|trigger server"):
                await asyncio.to_thread(rows.append_interruption, dataclasses.replace(_interruption(run), kind=kind))


def test_a_critic_row_without_a_critic_run_never_counts():
    ledger, claim = str(uuid.uuid4()), f"urn:agentsdk:claim:{uuid.uuid4()}"
    node = NodeSubject(run_id=ledger, plan_id=str(uuid.uuid4()), version=1, node_id="a")

    def row(kind, subject, outcome, detail):
        return EvidenceVerification(verification_id=str(uuid.uuid4()), ledger_run_id=ledger, subject=subject,
                                    kind=kind, outcome=outcome, detail=detail,
                                    provenance=ContentProvenance.internal_tool(), tenant_id=TENANT,
                                    project_id=PROJECT, created_at=datetime.now(timezone.utc))

    rows = [row("gate", node, "critic_required", {"claims": [{"claim": claim, "fired": ["low_confidence"],
                                                              "values": {"lowest_confidence": 0.3}}],
                                                  "fired": ["low_confidence"]}),
            # Neither critic row names its critic_run: they join on nothing, not on None.
            row("critic", node, "approved", {"verdict": "approved"}),
            row("critic", claim, "approved", {"verdict": "approved"}),
            row("external", claim, "incorrect", {"source": "fixture", "detail": "known"})]
    made = calibrate(rows, tenant_id=TENANT, project_id=PROJECT, since=None, min_outcomes=1)
    assert made.critic["approved"] == {"correct": 0, "incorrect": 0, "no_outcome": 0}


def test_no_store_statement_or_method_updates_or_deletes_a_row():
    """AC-85, asserted on what the stores do rather than on method names (M29 review)."""
    postgres = inspect.getsource(PostgresInterruptionStore).upper()
    assert "UPDATE " not in postgres and "DELETE " not in postgres
    memory = inspect.getsource(InMemoryInterruptionStore)
    for mutation in ("del ", ".pop(", ".remove(", ".clear(", ".discard("):
        assert mutation not in memory, mutation


# =================================================================================================
# FR-113: what requires approval
# =================================================================================================


@pytest.mark.parametrize("store", STORES)
async def test_each_source_alone_requires_approval_and_records_itself_as_the_trigger(store):
    ran = Ran()

    class Asks(RuntimeHook):
        def before_tool(self, tool_call):
            return HookOutcome(HookAction.REQUIRE_APPROVAL) if tool_call.name == "hooked" else HookOutcome()

    tools = [ran.tool("policy"), ran.tool("predicate", policy=ApprovalPolicy.AUTO, needs_approval=lambda a: True),
             ran.tool("hooked", policy=ApprovalPolicy.AUTO), ran.tool("free", policy=ApprovalPolicy.AUTO)]
    model = Script([("policy", {}, "c1"), ("predicate", {}, "c2"), ("hooked", {}, "c3"), ("free", {}, "c4")])
    runner = runner_for(store, tools, model, hook=Asks())
    handle = await runner.start(AGENT, "go", config())
    views = await pending(runner, handle.run_id, 3)
    assert {v.interruption.tool_name: v.interruption.trigger for v in views} == {
        "policy": "policy", "predicate": "predicate", "hooked": "hook"}
    for view in views:
        await handle.approve(view.interruption.interruption_id, principal=APPROVER)
    result = await handle.result()
    assert result.status is RunStatus.COMPLETED
    assert [name for name, _ in ran.calls] == ["policy", "predicate", "hooked", "free"]


async def test_a_permission_denied_call_is_never_asked_about():
    ran = Ran()
    model = Script([("policy", {}, "c1")])
    runner = Runner({"m": model}, tools=[ran.tool("policy")])
    denied = AgentSpec(id="agent", instructions="act", preferred_model="m:x", tool_profile=())
    result = await runner.run(denied, "go", config())
    assert result.status is RunStatus.COMPLETED and ran.calls == []
    assert await runner.interruptions(TENANT, PROJECT, run_id=result.run_id, pending_only=False) == []


async def test_a_hooks_modify_is_what_the_request_shows_and_what_runs():
    ran = Ran()

    class Rewrites(RuntimeHook):
        def before_tool(self, tool_call):
            return HookOutcome(HookAction.MODIFY, replacement=dataclasses.replace(tool_call, arguments={"x": "safe"}))

    runner = Runner({"m": Script([("policy", {"x": "risky"}, "c1")])}, tools=[ran.tool("policy")], hook=Rewrites())
    handle = await runner.start(AGENT, "go", config())
    [view] = await pending(runner, handle.run_id, 1)
    assert view.interruption.request_payload["arguments"] == {"x": "safe"}
    await handle.approve(view.interruption.interruption_id, principal=APPROVER)
    await handle.result()
    assert ran.calls == [("policy", {"x": "safe"})]


async def test_halt_from_before_tool_is_still_ignored():
    ran = Ran()

    class Halts(RuntimeHook):
        def before_tool(self, tool_call):
            return HookOutcome(HookAction.HALT)

    runner = Runner({"m": Script([("free", {}, "c1")])}, tools=[ran.tool("free", policy=ApprovalPolicy.AUTO)],
                    hook=Halts())
    result = await runner.run(AGENT, "go", config())
    assert result.status is RunStatus.COMPLETED and ran.calls == [("free", {})]


async def test_a_predicate_or_describe_that_raises_or_an_unstorable_request_fails_the_call_with_no_interruption():
    ran = Ran()

    def boom(arguments):
        raise RuntimeError("predicate broke")

    async def bad_describe(arguments):
        raise RuntimeError("describe broke")

    async def unstorable_describe(arguments):
        # Arguments holding a NUL are refused at step 2 already, so only what a tool
        # computes for the approver can make a request no store holds.
        return {"preview": "nul\u0000here"}

    tools = [ran.tool("predicate", policy=ApprovalPolicy.AUTO, needs_approval=boom),
             ran.tool("described", describe=bad_describe), ran.tool("unstorable", describe=unstorable_describe)]
    model = Script([("predicate", {}, "c1"), ("described", {}, "c2"), ("unstorable", {}, "c3")])
    runner = Runner({"m": model}, tools=tools)
    result = await runner.run(AGENT, "go", config())
    assert result.status is RunStatus.COMPLETED and ran.calls == []
    assert await runner.interruptions(TENANT, PROJECT, run_id=result.run_id, pending_only=False) == []
    results = results_of(model)
    for cid in ("c1", "c2", "c3"):
        assert results[cid].is_error and ToolApprovalCheckFailed.__name__ in results[cid].content, results[cid]


async def test_the_executor_refuses_an_unstorable_request_before_any_store_sees_it():
    """The store refuses one too: two guards hide each other (KNOWLEDGE-aa97d748), so the
    executor's is tested on its own, with no store at all."""
    from agentsdk.executor import ToolExecutor
    from agentsdk.outcomes import ApprovalRequired, Failed
    from agentsdk.tools import ToolRegistry

    ran = Ran()

    async def unstorable(arguments):
        return {"preview": "nul\u0000here"}

    async def storable(arguments):
        return {"preview": "fine"}

    for describe, expected in ((unstorable, Failed), (storable, ApprovalRequired)):
        tool = ran.tool("policy", describe=describe)
        registry = ToolRegistry()
        registry.register(tool)
        executor = ToolExecutor(registry=registry, permission_checker=AllowlistPermissionChecker({"policy"}))
        outcome = await executor.prepare(ToolCall(id="c1", name="policy", arguments={}))
        assert isinstance(outcome, expected), outcome
        if expected is Failed:
            assert ToolApprovalCheckFailed.__name__ in outcome.result.content


def test_approval_policy_joins_the_schema_hash_only_when_not_the_default():
    base = ToolSpec(name="t", description="t", input_schema=SCHEMA)
    assert dataclasses.replace(base, approval_policy=ApprovalPolicy.AUTO).schema_hash() == base.schema_hash()
    assert dataclasses.replace(base, approval_policy=ApprovalPolicy.REQUIRE_APPROVAL).schema_hash() != base.schema_hash()


# =================================================================================================
# FR-114: the turn
# =================================================================================================


@pytest.mark.parametrize("store", STORES)
async def test_from_the_first_pending_call_every_call_waits_and_runs_in_issue_order(store):
    ran = Ran()
    tools = [ran.tool("a"), ran.tool("b", policy=ApprovalPolicy.AUTO), ran.tool("c")]
    model = Script([("a", {}, "c1"), ("b", {}, "c2"), ("c", {}, "c3")])
    runner = runner_for(store, tools, model)
    handle = await runner.start(AGENT, "go", config())
    first, second = await pending(runner, handle.run_id, 2)
    await asyncio.sleep(0.05)
    assert ran.calls == [], "the free call waits behind the first pending one"
    assert model.sent == 1, "the model is not called while a call is pending"
    await handle.approve(first.interruption.interruption_id, principal=APPROVER)
    await asyncio.sleep(0.05)
    assert ran.calls == [] and model.sent == 1, "nothing runs until every decision of the turn is in"
    await handle.reject(second.interruption.interruption_id, principal=APPROVER, message="not c")
    result = await handle.result()
    assert result.status is RunStatus.COMPLETED
    assert [name for name, _ in ran.calls] == ["a", "b"], "c was rejected"
    rejected = results_of(model)["c3"]
    assert rejected.is_error and rejected.content == "rejected: not c"
    [decision] = [v.decision for v in await runner.interruptions(TENANT, PROJECT, run_id=result.run_id,
                                                                 pending_only=False)
                  if v.interruption.tool_call_id == "c3"]
    assert rejected.provenance.source_uri_or_hash == f"urn:agentsdk:decision:{decision.decision_id}"
    # ApprovalDecided precedes its call's ToolCalled.
    kinds = [(e.event_type, e.payload.get("tool_call_id") or e.payload.get("tool_call")) for e in result.events
             if e.event_type in (EventType.APPROVAL_DECIDED, EventType.TOOL_CALLED)]
    assert kinds.index((EventType.APPROVAL_DECIDED, "c3")) < kinds.index((EventType.TOOL_CALLED, "c3")), kinds
    assert len(events(result, EventType.APPROVAL_REQUESTED)) == 2


async def test_a_free_call_before_the_first_pending_one_runs_before_the_wait():
    ran = Ran()
    model = Script([("b", {}, "c1"), ("a", {}, "c2")])
    runner = Runner({"m": model}, tools=[ran.tool("a"), ran.tool("b", policy=ApprovalPolicy.AUTO)])
    handle = await runner.start(AGENT, "go", config())
    [view] = await pending(runner, handle.run_id, 1)
    assert ran.calls == [("b", {})]
    await handle.approve(view.interruption.interruption_id, principal=APPROVER)
    await handle.result()
    assert [name for name, _ in ran.calls] == ["b", "a"]


async def test_two_turns_reusing_one_call_id_get_two_interruptions():
    ran = Ran()
    model = Script([("a", {}, "same")], [("a", {}, "same")])
    runner = Runner({"m": model}, tools=[ran.tool("a")])
    handle = await runner.start(AGENT, "go", config())
    for _ in range(2):
        [view] = await pending(runner, handle.run_id, 1)
        await handle.approve(view.interruption.interruption_id, principal=APPROVER)
    await handle.result()
    every = await runner.interruptions(TENANT, PROJECT, run_id=handle.run_id, pending_only=False)
    assert len(every) == 2 and len({v.interruption.interruption_id for v in every}) == 2
    assert len(ran.calls) == 2


# =================================================================================================
# FR-115, P4-D15: sticky decisions
# =================================================================================================


@pytest.mark.parametrize("approve", [True, False])
async def test_a_sticky_decision_decides_the_runs_undecided_policy_calls_of_that_agent_and_tool(approve):
    ran = Ran()
    tools = [ran.tool("a"), ran.tool("other"), ran.tool("flagged", needs_approval=lambda args: True)]
    # Turn 1: two calls of a and one of other; turn 2: a again, and a flagged by its predicate too.
    model = Script([("a", {"x": "1"}, "c1"), ("a", {"x": "2"}, "c2"), ("other", {}, "c3")],
                   [("a", {"x": "3"}, "c4")])
    runner = Runner({"m": model}, tools=tools)
    handle = await runner.start(AGENT, "go", config())
    first, sibling, other = await pending(runner, handle.run_id, 3)
    decide = handle.approve if approve else handle.reject
    await decide(first.interruption.interruption_id, principal=APPROVER, sticky=True)
    await asyncio.sleep(0.1)
    views = {v.interruption.tool_call_id: v for v in await runner.interruptions(
        TENANT, PROJECT, run_id=handle.run_id, pending_only=False)}
    assert views["c2"].decision.source == "standing", "the sibling in the same turn"
    assert views["c3"].decision is None, "not another tool"
    await handle.approve(other.interruption.interruption_id, principal=APPROVER)
    result = await handle.result()
    views = {v.interruption.tool_call_id: v for v in await runner.interruptions(
        TENANT, PROJECT, run_id=result.run_id, pending_only=False)}
    assert views["c4"].decision.source == "standing", "the next turn's call"
    assert {views[c].decision.decision for c in ("c2", "c4")} == {"approve" if approve else "reject"}
    assert len([c for c in ran.calls if c[0] == "a"]) == (3 if approve else 0)


async def test_a_sticky_decision_never_covers_a_call_flagged_by_a_predicate_or_a_hook():
    ran = Ran()

    class Asks(RuntimeHook):
        def before_tool(self, tool_call):
            return HookOutcome(HookAction.REQUIRE_APPROVAL) if tool_call.arguments.get("x") == "hook" else HookOutcome()

    flag = ran.tool("w", policy=ApprovalPolicy.AUTO, needs_approval=lambda args: args.get("x") == "pred")
    model = Script([("w", {"x": "pred"}, "c1")], [("w", {"x": "pred"}, "c2"), ("w", {"x": "hook"}, "c3")])
    runner = Runner({"m": model}, tools=[flag], hook=Asks())
    handle = await runner.start(AGENT, "go", config())
    [first] = await pending(runner, handle.run_id, 1)
    await handle.approve(first.interruption.interruption_id, principal=APPROVER, sticky=True)
    second, third = await pending(runner, handle.run_id, 2)
    assert {second.interruption.trigger, third.interruption.trigger} == {"predicate", "hook"}
    for view in (second, third):
        await handle.approve(view.interruption.interruption_id, principal=APPROVER)
    await handle.result()


async def test_a_call_decided_before_a_sticky_decision_keeps_its_decision():
    ran = Ran()
    model = Script([("a", {"x": "1"}, "c1"), ("a", {"x": "2"}, "c2")])
    runner = Runner({"m": model}, tools=[ran.tool("a")])
    handle = await runner.start(AGENT, "go", config())
    first, second = await pending(runner, handle.run_id, 2)
    await handle.reject(first.interruption.interruption_id, principal=APPROVER)
    await handle.approve(second.interruption.interruption_id, principal=APPROVER, sticky=True)
    result = await handle.result()
    views = {v.interruption.tool_call_id: v for v in await runner.interruptions(
        TENANT, PROJECT, run_id=result.run_id, pending_only=False)}
    assert views["c1"].decision.decision == "reject" and ran.calls == [("a", {"x": "2"})]


async def test_an_interruption_is_decidable_from_the_moment_it_is_recorded(monkeypatch):
    """Found by example 24 under load: an interruption was listed pending before its run
    registered the wait, so an approver deciding at once was refused not waiting."""
    ran, seen = Ran(), []
    runner = Runner({"m": Script([("a", {}, "c1"), ("b", {}, "c2")])}, tools=[ran.tool("a"), ran.tool("b")])
    real = InMemoryInterruptionStore.append_interruption

    def watched(self, item, decision=None):
        real(self, item, decision)
        seen.append(runner._approvals.waiting(item.interruption_id))

    monkeypatch.setattr(InMemoryInterruptionStore, "append_interruption", watched)
    handle = await runner.start(AGENT, "go", config())
    for view in await pending(runner, handle.run_id, 2):
        await handle.approve(view.interruption.interruption_id, principal=APPROVER)
    await handle.result()
    assert seen == [True, True]


async def test_a_call_a_standing_decision_covers_is_never_listed_pending(monkeypatch):
    """Found by example 24: the standing decision was applied after the turn was recorded,
    so an approver could see the covered call pending and decide it as a person."""
    ran, listed = Ran(), []
    model = Script([("a", {"x": "1"}, "c1")], [("a", {"x": "2"}, "c2")])
    runner = Runner({"m": model}, tools=[ran.tool("a")])
    real = InMemoryInterruptionStore.append_interruption

    def watched(self, item, decision=None):
        real(self, item, decision)
        listed.extend(v.interruption.tool_call_id for v in self.interruptions(TENANT, PROJECT, pending_only=True))

    monkeypatch.setattr(InMemoryInterruptionStore, "append_interruption", watched)
    handle = await runner.start(AGENT, "go", config())
    [first] = await pending(runner, handle.run_id, 1)
    await handle.approve(first.interruption.interruption_id, principal=APPROVER, sticky=True)
    result = await handle.result()
    assert "c2" not in listed and ran.calls == [("a", {"x": "1"}), ("a", {"x": "2"})]
    assert not any(e.payload.get("tool_call_id") == "c2" for e in events(result, EventType.APPROVAL_REQUESTED))


# =================================================================================================
# FR-116: expiry and cancellation
# =================================================================================================


@pytest.mark.parametrize("store", STORES)
async def test_an_interruption_past_its_timeout_is_decided_expiry_and_returns_approval_expired(store):
    ran = Ran()
    model = Script([("a", {}, "c1")])
    runner = runner_for(store, [ran.tool("a")], model)
    result = await runner.run(AGENT, "go", config(approval_timeout=0.3))
    assert result.status is RunStatus.COMPLETED and ran.calls == []
    [view] = await runner.interruptions(TENANT, PROJECT, run_id=result.run_id, pending_only=False)
    assert view.decision.source == "expiry" and view.state == "expired"
    assert results_of(model)["c1"].content == "approval expired"


def test_approval_timeout_is_a_finite_number_over_zero():
    for bad in (0, -1, float("inf"), float("nan"), True, "60"):
        with pytest.raises(ValueError, match="approval_timeout"):
            config(approval_timeout=bad)
    assert config().approval_timeout == 3600
    config(approval_timeout=0.5)


@pytest.mark.parametrize("store", STORES)
async def test_cancelling_a_waiting_run_decides_cancel_and_runs_nothing(store):
    ran = Ran()
    runner = runner_for(store, [ran.tool("a"), ran.tool("b")], Script([("a", {}, "c1"), ("b", {}, "c2")]))
    handle = await runner.start(AGENT, "go", config())
    await pending(runner, handle.run_id, 2)
    handle.cancel("stop")
    result = await handle.result()
    assert result.status is RunStatus.CANCELLED and ran.calls == []
    views = await runner.interruptions(TENANT, PROJECT, run_id=result.run_id, pending_only=False)
    assert [v.decision.source for v in views] == ["cancel", "cancel"]
    assert [v.state for v in views] == ["cancelled", "cancelled"]


async def test_the_manifest_records_the_approval_timeout():
    runner = runner_for("postgres", [], Script())
    result = await runner.run(AGENT, "go", config(approval_timeout=90))
    with psycopg.connect(DSN) as conn:
        [timeout] = conn.execute("SELECT approval_timeout FROM execution_manifests WHERE run_id = %s",
                                 (result.run_id,)).fetchone()
    assert timeout == 90


# =================================================================================================
# FR-115: deciding
# =================================================================================================


@pytest.mark.parametrize("store", STORES)
async def test_decide_refuses_what_it_must_with_nothing_recorded(store):
    ran = Ran()
    runner = runner_for(store, [ran.tool("a")], Script([("a", {}, "c1"), ("a", {}, "c2")]))
    handle = await runner.start(AGENT, "go", config())
    first, second = await pending(runner, handle.run_id, 2)
    one, two = first.interruption.interruption_id, second.interruption.interruption_id
    await runner.decide(TENANT, PROJECT, one, approve=True, principal=APPROVER)
    # Another tenant's: unknown, decided or not, exactly as an id that does not exist.
    for interruption_id in (one, two, str(uuid.uuid4())):
        with pytest.raises(UnknownInterruption):
            await runner.decide(OTHER_TENANT, PROJECT, interruption_id, approve=True, principal=APPROVER)
    with pytest.raises(InterruptionAlreadyDecided):
        await runner.decide(TENANT, PROJECT, one, approve=False, principal=APPROVER)
    with pytest.raises(ValueError, match="user or a service"):
        await runner.decide(TENANT, PROJECT, two, approve=True, principal=PrincipalContext(agent_principal="agent"))
    with pytest.raises(UnknownInterruption):
        await handle.approve(str(uuid.uuid4()), principal=APPROVER)
    assert await runner.interruptions(OTHER_TENANT, PROJECT, pending_only=False) == []
    assert (await runner.interruptions(TENANT, PROJECT, run_id=handle.run_id))[0].waiting is True
    await handle.approve(two, principal=APPROVER)
    await handle.result()
    every = await runner.interruptions(TENANT, PROJECT, run_id=handle.run_id, pending_only=False)
    assert [v.decision.decision for v in every] == ["approve", "approve"] and not any(v.waiting for v in every)


async def test_decide_refuses_an_expired_interruption_and_one_no_run_waits_on():
    rows_runner = runner_for("memory", [], Script())
    finished = await rows_runner.run(AGENT, "go", config())
    rows = rows_runner._interruption_store()
    idle = _interruption(finished.run_id)
    late = dataclasses.replace(_interruption(finished.run_id), expires_at=datetime.now(timezone.utc) - timedelta(1))
    for item in (idle, late):
        await asyncio.to_thread(rows.append_interruption, item)
    with pytest.raises(InterruptionNotWaiting):
        await rows_runner.decide(TENANT, PROJECT, idle.interruption_id, approve=True, principal=APPROVER)
    with pytest.raises(InterruptionExpired):
        await rows_runner.decide(TENANT, PROJECT, late.interruption_id, approve=True, principal=APPROVER)
    assert all(v.decision is None for v in await rows_runner.interruptions(TENANT, PROJECT, pending_only=False))


async def test_a_handle_decides_only_its_own_runs_interruptions():
    ran = Ran()
    runner = Runner({"m": Script([("a", {}, "c1")])}, tools=[ran.tool("a")])
    first = await runner.start(AGENT, "go", config())
    second = await runner.start(AGENT, "go", config())
    [theirs] = await pending(runner, second.run_id, 1)
    await pending(runner, first.run_id, 1)
    with pytest.raises(UnknownInterruption):
        await first.approve(theirs.interruption.interruption_id, principal=APPROVER)
    first.cancel()
    second.cancel()
    await asyncio.gather(first.result(), second.result())


@pytest.mark.parametrize("store", STORES)
async def test_two_decisions_racing_and_a_decision_racing_expiry_record_exactly_one(store):
    ran = Ran()
    runner = runner_for(store, [ran.tool("a")], Script([("a", {}, "c1")]))
    handle = await runner.start(AGENT, "go", config())
    [view] = await pending(runner, handle.run_id, 1)
    outcomes = await asyncio.gather(
        *(runner.decide(TENANT, PROJECT, view.interruption.interruption_id, approve=i % 2 == 0, principal=APPROVER)
          for i in range(6)), return_exceptions=True)
    assert len([o for o in outcomes if isinstance(o, InterruptionDecision)]) == 1, outcomes
    assert all(isinstance(o, (InterruptionDecision, InterruptionAlreadyDecided)) for o in outcomes), outcomes
    await handle.result()
    # A decision racing the run's own expiry.
    expiring = runner_for(store, [ran.tool("a")], Script([("a", {}, "c1")]))
    handle = await expiring.start(AGENT, "go", config(approval_timeout=0.4))
    [view] = await pending(expiring, handle.run_id, 1)
    await asyncio.sleep(max(0.0, (view.interruption.expires_at - datetime.now(timezone.utc)).total_seconds()))
    try:
        await expiring.decide(TENANT, PROJECT, view.interruption.interruption_id, approve=True, principal=APPROVER)
    except (InterruptionAlreadyDecided, InterruptionExpired, InterruptionNotWaiting):
        pass
    await handle.result()
    [after] = await expiring.interruptions(TENANT, PROJECT, run_id=handle.run_id, pending_only=False)
    assert after.decision is not None


async def test_decide_awaited_on_another_threads_loop_wakes_the_run():
    ran = Ran()
    runner = Runner({"m": Script([("a", {}, "c1")])}, tools=[ran.tool("a")])
    handle = await runner.start(AGENT, "go", config())
    [view] = await pending(runner, handle.run_id, 1)
    errors = []

    def elsewhere():
        try:
            asyncio.run(runner.decide(TENANT, PROJECT, view.interruption.interruption_id, approve=True,
                                      principal=APPROVER))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=elsewhere)
    started = time.monotonic()
    thread.start()
    result = await handle.result()
    await asyncio.to_thread(thread.join)
    assert errors == [] and ran.calls == [("a", {})] and result.status is RunStatus.COMPLETED
    assert time.monotonic() - started < 2.0, "woken, not left to the five-second re-read"


async def test_a_dropped_wake_still_reaches_the_decision_within_five_seconds(monkeypatch):
    ran = Ran()
    runner = Runner({"m": Script([("a", {}, "c1")])}, tools=[ran.tool("a")])
    monkeypatch.setattr(type(runner._approvals), "wake", lambda self, interruption_id: None)
    handle = await runner.start(AGENT, "go", config())
    [view] = await pending(runner, handle.run_id, 1)
    # Decided only once the run has asked and is waiting, so the decision cannot be found
    # by the run's first read: with the wake dropped, only the re-read can find it.
    async for event in handle.events():
        if event.event_type is EventType.APPROVAL_REQUESTED:
            break
    await asyncio.sleep(0.2)
    started = time.monotonic()
    await runner.decide(TENANT, PROJECT, view.interruption.interruption_id, approve=True, principal=APPROVER)
    await handle.result()
    took = time.monotonic() - started
    assert ran.calls == [("a", {})] and 0.5 < took < 6.5, took


# =================================================================================================
# NFR-30: no store failure lets a call through
# =================================================================================================


async def test_a_store_that_cannot_record_the_interruption_fails_the_call(monkeypatch):
    ran = Ran()
    model = Script([("a", {}, "c1")])
    runner = Runner({"m": model}, tools=[ran.tool("a")])

    def broken(self, item):
        raise RuntimeError("the store is down")

    monkeypatch.setattr(InMemoryInterruptionStore, "append_interruption", broken)
    result = await runner.run(AGENT, "go", config())
    # The call fails, with nothing recorded and nothing run; the run itself goes on.
    assert result.status is RunStatus.COMPLETED and ran.calls == []
    assert results_of(model)["c1"].is_error


async def test_a_store_that_cannot_record_a_decision_leaves_the_call_undecided(monkeypatch):
    ran = Ran()
    runner = Runner({"m": Script([("a", {}, "c1")])}, tools=[ran.tool("a")])
    handle = await runner.start(AGENT, "go", config())
    [view] = await pending(runner, handle.run_id, 1)
    real = InMemoryInterruptionStore.append_decision

    def broken(self, decision):
        raise RuntimeError("the store is down")

    monkeypatch.setattr(InMemoryInterruptionStore, "append_decision", broken)
    with pytest.raises(RuntimeError):
        await runner.decide(TENANT, PROJECT, view.interruption.interruption_id, approve=True, principal=APPROVER)
    await asyncio.sleep(0.1)
    assert ran.calls == []
    monkeypatch.setattr(InMemoryInterruptionStore, "append_decision", real)
    handle.cancel()
    result = await handle.result()
    assert result.status is RunStatus.CANCELLED and ran.calls == []


async def test_a_store_that_cannot_read_decisions_leaves_the_call_waiting(monkeypatch):
    ran = Ran()
    runner = Runner({"m": Script([("a", {}, "c1")])}, tools=[ran.tool("a")])
    handle = await runner.start(AGENT, "go", config())
    [view] = await pending(runner, handle.run_id, 1)
    real = InMemoryInterruptionStore.interruption

    def broken(self, *args, **kwargs):
        raise RuntimeError("the store is down")

    monkeypatch.setattr(InMemoryInterruptionStore, "interruption", broken)
    await asyncio.sleep(0.2)
    assert ran.calls == [] and not handle._control.task.done()
    monkeypatch.setattr(InMemoryInterruptionStore, "interruption", real)
    await handle.approve(view.interruption.interruption_id, principal=APPROVER)
    await handle.result()
    assert ran.calls == [("a", {})]


# =================================================================================================
# FR-115: never a tool (AC-83 extended)
# =================================================================================================


def test_no_module_the_sdk_ships_decides_or_lists_but_the_runner_and_its_handle():
    callers = [p.name for p in (REPO / "agentsdk").rglob("*.py")
               if ("decide(" in p.read_text(encoding="utf-8") or ".interruptions(" in p.read_text(encoding="utf-8"))
               and p.name not in ("api.py", "handle.py", "interruptions.py", "postgres.py")]
    assert callers == [], callers


async def test_no_field_of_a_tools_scope_but_runner_offers_a_way_to_decide():
    seen = []

    async def see_scope(*, scope: RunScope):
        seen.append(scope)
        return "seen"

    tool = Tool(spec=ToolSpec(name="see", description="see", input_schema={"type": "object"},
                              approval_policy=ApprovalPolicy.AUTO), fn=see_scope)
    runner = Runner({"m": Script([("see", {}, "c1")])}, tools=[tool])
    await runner.run(AGENT, "go", config())
    [scope] = seen
    offered = [f.name for f in dataclasses.fields(scope) if f.name != "_runner" and any(
        callable(getattr(getattr(scope, f.name), name, None)) for name in ("decide", "append_decision"))]
    assert offered == [], offered


# =================================================================================================
# FR-117: example 24, offline on both stores
# =================================================================================================


def _example():
    import importlib.util

    spec = importlib.util.spec_from_file_location("example_24", REPO / "scripts" / "24_approvals.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("store", STORES)
async def test_example_24_runs_offline_with_every_check_passing(store):
    checks, runs = await _example().offline(store, quiet=True)
    failed = [label for label, passed in checks if not passed]
    assert len(checks) == 8 and not failed, failed
    if store == "postgres":
        _remove(runs)


def _remove(runs):
    """The example writes under example-tenant: removed by the run ids it returned, never by tenant."""
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DELETE FROM interruption_decisions WHERE interruption_id IN (SELECT interruption_id"
                     " FROM interruptions WHERE run_id = ANY(%s::uuid[]))", (runs,))
        conn.execute("DELETE FROM interruptions WHERE run_id = ANY(%s::uuid[])", (runs,))
        conn.execute("DELETE FROM standing_decisions WHERE run_id = ANY(%s::uuid[])", (runs,))
        for table in ("run_events", "messages", "execution_manifests"):
            conn.execute(f"DELETE FROM {table} WHERE run_id = ANY(%s::uuid[])", (runs,))
        conn.execute("DELETE FROM runs WHERE run_id = ANY(%s::uuid[])", (runs,))


def test_example_24_runs_offline():
    import subprocess
    import sys

    done = subprocess.run([sys.executable, str(REPO / "scripts" / "24_approvals.py"), "--offline"],
                          capture_output=True, text=True, timeout=180, cwd=REPO)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "[FAIL]" not in done.stdout and done.stdout.count("[PASS]") == 8, done.stdout
