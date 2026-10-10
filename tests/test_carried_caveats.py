"""M21a gate: the carried caveats (FR-97, AC-75).

M21's round 2 approval carried four caveats (KNOWLEDGE-8e29cfe3); the owner settled them
before M22 (DECISION-23ae5809). Two are repaired on the in-memory session store: a
combined write files its run under one key however a first bound write interleaves, and
the first binding of a run id adopts the history written to it unbound. Two are
declared, each asserted here as declared: a bound combined write refuses a sink for
another run, which the Runner never builds; and on Postgres a scope-less sink whose emit
fails leaves the summary without its event.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from decimal import Decimal

import psycopg
import pytest

from agentsdk import AgentSpec, BudgetPolicy, ContextPolicy, Persistence, RunConfig, RunStatus, Runner
from agentsdk import api
from agentsdk.config import normalise_database_url
from agentsdk.events import EventType, InMemoryEventSink
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.postgres import RunScope
from agentsdk.primitives import Message, Role, ToolCall
from agentsdk.session import InMemorySessionStore

DSN = normalise_database_url(os.environ.get("DATABASE_URL"))
TENANT, PROJECT, OTHER = "SYN-m21a", "p-m21a", "SYN-m21a-other"


def scope(tenant=TENANT, project=PROJECT, run_id="r"):
    return RunScope(run_id=run_id, tenant_id=tenant, project_id=project)


def said(history):
    return [m.content for m in history]


def query(sql, params=()):
    with psycopg.connect(DSN) as conn:
        return conn.execute(sql, params).fetchall()


@pytest.fixture(autouse=True)
def remove_what_the_test_wrote():
    yield
    if DSN:
        with psycopg.connect(DSN, autocommit=True) as conn:
            ids = [r[0] for r in conn.execute("SELECT run_id FROM runs WHERE tenant_id = %s", (TENANT,)).fetchall()]
            for table in ("run_events", "messages", "execution_manifests"):
                conn.execute(f"DELETE FROM {table} WHERE run_id = ANY(%s)", (ids,))
            conn.execute("DELETE FROM runs WHERE run_id = ANY(%s)", (ids,))


class Fails(InMemoryEventSink):
    def emit(self, *args, **kwargs):
        raise RuntimeError("the event store failed")


# =================================================================================================
# Caveat 1, repaired: one key per combined write
# =================================================================================================


def _first_bound_write_between_key_and_append(monkeypatch, store):
    """A wrapper on append, as a test double would be: the first call makes the run's
    first bound write, then lets the combined write's own append through. Under M21 the
    combined write had already taken its key, (None, None, run), before this happened."""
    original = InMemorySessionStore.append
    done = []

    def append(self, run_id, message):
        if not done:
            done.append(True)
            store.bind(scope(run_id=run_id)).append(run_id, Message(role=Role.USER, content="bound"))
        return original(self, run_id, message)

    monkeypatch.setattr(InMemorySessionStore, "append", append)


@pytest.mark.parametrize("event", ["recorded", "fails"])
def test_caveat1_a_combined_write_racing_the_first_bound_write_files_the_run_under_one_key(monkeypatch, event):
    store = InMemorySessionStore()
    _first_bound_write_between_key_and_append(monkeypatch, store)
    sink = InMemoryEventSink(TENANT, PROJECT, "r") if event == "recorded" else Fails(TENANT, PROJECT, "r")
    summary = Message(role=Role.USER, content="summary")
    if event == "recorded":
        store.append_with_event("r", summary, sink, EventType.CONTEXT_COMPACTED, {})
        expected = ["bound", "summary"]
    else:
        with pytest.raises(RuntimeError, match="the event store failed"):
            store.append_with_event("r", summary, sink, EventType.CONTEXT_COMPACTED, {})
        expected = ["bound"]
    # One run, one history: unbound and bound reads agree. Under M21 the take-back's
    # second key left the id under two keys, and the unbound read was refused.
    assert said(store.history("r")) == expected
    assert said(store.bind(scope()).history("r")) == expected


def test_caveat1_the_write_lands_on_the_run_it_began_on_when_a_second_tenant_binds_the_id(monkeypatch):
    """The key is derived once: an unbound combined write that began on tenant A's run
    writes there, though tenant B binds the same run id before its append."""
    store = InMemorySessionStore()
    store.bind(scope()).append("r", Message(role=Role.USER, content="a"))
    original = InMemorySessionStore.append

    def append(self, run_id, message):
        if message.content == "summary":
            store.bind(scope(tenant=OTHER)).append(run_id, Message(role=Role.USER, content="b"))
        return original(self, run_id, message)

    monkeypatch.setattr(InMemorySessionStore, "append", append)
    store.append_with_event("r", Message(role=Role.USER, content="summary"), InMemoryEventSink(TENANT, PROJECT, "r"),
                            EventType.CONTEXT_COMPACTED, {})
    assert said(store.bind(scope()).history("r")) == ["a", "summary"]
    assert said(store.bind(scope(tenant=OTHER)).history("r")) == ["b"]


def test_caveat1_the_take_back_finds_the_summary_after_two_tenants_bind_the_id(monkeypatch):
    """The history is taken once, before the write: a take-back that looked the run up
    again after tenant B bound the id would be refused as ambiguous, and leave the
    summary in tenant A's history without its event."""
    store = InMemorySessionStore()
    original = InMemorySessionStore.append

    def append(self, run_id, message):
        if message.content == "summary":
            store.bind(scope()).append(run_id, Message(role=Role.USER, content="a"))
            original(self, run_id, message)
            store.bind(scope(tenant=OTHER)).append(run_id, Message(role=Role.USER, content="b"))
            return None
        return original(self, run_id, message)

    monkeypatch.setattr(InMemorySessionStore, "append", append)
    with pytest.raises(RuntimeError, match="the event store failed"):
        store.append_with_event("r", Message(role=Role.USER, content="summary"), Fails(TENANT, PROJECT, "r"),
                                EventType.CONTEXT_COMPACTED, {})
    assert said(store.bind(scope()).history("r")) == ["a"]
    assert said(store.bind(scope(tenant=OTHER)).history("r")) == ["b"]


# =================================================================================================
# Caveat 2, repaired: the first binding adopts the unbound history
# =================================================================================================


def test_caveat2_the_first_binding_adopts_what_was_written_to_the_run_id_unbound():
    store = InMemorySessionStore()
    store.append("r", Message(role=Role.USER, content="unbound"))
    store.bind(scope()).append("r", Message(role=Role.USER, content="bound"))
    store.append("r", Message(role=Role.USER, content="unbound again"))
    assert said(store.history("r")) == ["unbound", "bound", "unbound again"]
    assert said(store.bind(scope()).history("r")) == ["unbound", "bound", "unbound again"]


def test_caveat2_a_first_bound_read_adopts_it_too():
    store = InMemorySessionStore()
    store.append("r", Message(role=Role.USER, content="unbound"))
    assert said(store.bind(scope()).history("r")) == ["unbound"]
    store.append("r", Message(role=Role.USER, content="after"))
    assert said(store.bind(scope()).history("r")) == ["unbound", "after"]


def test_caveat2_a_second_tenant_with_the_same_run_id_still_keeps_its_own_history():
    store = InMemorySessionStore()
    store.append("r", Message(role=Role.USER, content="unbound"))
    store.bind(scope()).append("r", Message(role=Role.USER, content="first"))
    store.bind(scope(tenant=OTHER)).append("r", Message(role=Role.USER, content="second"))
    assert said(store.bind(scope()).history("r")) == ["unbound", "first"]
    assert said(store.bind(scope(tenant=OTHER)).history("r")) == ["second"]
    assert said(store.bind(scope(project="p-other")).history("r")) == []
    with pytest.raises(ValueError, match="more than one tenant"):
        store.history("r")


# =================================================================================================
# Caveat 3, declared: a bound combined write refuses another run's sink; the Runner never
# builds one
# =================================================================================================


def test_caveat3_declared_a_bound_combined_write_refuses_a_sink_for_another_run():
    store = InMemorySessionStore()
    sink = InMemoryEventSink(TENANT, PROJECT, "another")
    with pytest.raises(ValueError, match="one run's"):
        store.bind(scope()).append_with_event("r", Message(role=Role.USER, content="s"), sink,
                                              EventType.CONTEXT_COMPACTED, {})
    assert store.bind(scope()).history("r") == [] and sink.events() == ()


class Planner:
    """Submits one plan with one worker node, then answers."""

    def __init__(self):
        self.calls = 0

    async def send(self, request):
        self.calls += 1
        if self.calls == 1:
            plan = {"nodes": [{"node_id": "n", "objective": "go", "assigned_role": "worker", "dependencies": []}]}
            return ModelResponse(message=Message(role=Role.ASSISTANT, tool_calls=(
                ToolCall(id="plan-1", name="run_plan", arguments=plan),)),
                stop_reason=StopReason.TOOL_CALLS, usage=Usage(5, 1, 6))
        return ModelResponse(message=Message(role=Role.ASSISTANT, content="planned"),
                             stop_reason=StopReason.END_TURN, usage=Usage(5, 1, 6))


class Answers:
    async def send(self, request):
        return ModelResponse(message=Message(role=Role.ASSISTANT, content="done"),
                             stop_reason=StopReason.END_TURN, usage=Usage(5, 1, 6))


def _registry():
    from agentsdk.registry import ModelCapabilities, ModelEntry, ModelPricing, ModelRegistry

    price = ModelPricing(input=Decimal("0.01"), output=Decimal("0.01"))
    return ModelRegistry([
        ModelEntry(provider="t", model_id=m, model_version="1", adapter_version="t",
                   capabilities=ModelCapabilities(max_context_tokens=100_000, pricing=price))
        for m in ("p", "w")])


async def test_caveat3_every_sink_the_runner_builds_names_its_own_run(monkeypatch):
    """A top-level run, a subagent and an orchestrator's child: every loop the Runner
    builds is handed a sink naming that loop's own tenant, project and run id, so the
    refusal above is reachable only by a caller driving the loop itself."""
    from agentsdk.orchestrator import Orchestrator
    from agentsdk.subagents import Briefing, SubagentPool

    seen = []

    class Recording(api.AgentLoop):
        async def run(self, run_id, *args, **kwargs):
            seen.append((run_id, tuple(self._events.scope)))
            return await super().run(run_id, *args, **kwargs)

    monkeypatch.setattr(api, "AgentLoop", Recording)
    worker = AgentSpec(id="worker", instructions="w", preferred_model="w:w")
    orchestrator = Orchestrator(roles={"worker": worker}, policy=BudgetPolicy(run_ceiling_tokens=100_000))
    runner = Runner({"p": Planner(), "w": Answers()}, tools=[orchestrator.tool], model_registry=_registry())

    top = await runner.run(worker, "go", RunConfig(tenant_id=TENANT, project_id=PROJECT, model_override="w:w"))
    parent = RunScope(run_id=str(uuid.uuid4()), tenant_id=OTHER, project_id="p-sub")
    child = await SubagentPool(runner, context_policy=ContextPolicy(compact_at=None)).spawn(
        parent=parent, briefing=Briefing(objective="go", assigned_role="worker"), agent=worker)
    planned = await runner.run(orchestrator.agent, "do it",
                               orchestrator.config(tenant_id=TENANT, project_id="p-orch", model_override="p:p"))
    assert (top.status, child.status, planned.status) == (RunStatus.COMPLETED,) * 3, planned.error

    runs = {run_id: named for run_id, named in seen}
    assert runs[top.run_id] == (TENANT, PROJECT, top.run_id)
    assert runs[child.run_id] == (OTHER, "p-sub", child.run_id)
    assert runs[planned.run_id] == (TENANT, "p-orch", planned.run_id)
    children = [r for r in runs if r not in (top.run_id, child.run_id, planned.run_id)]
    assert len(children) == 1, seen
    assert runs[children[0]] == (TENANT, "p-orch", children[0])
    assert all(named[2] == run_id for run_id, named in seen), seen


# =================================================================================================
# Caveat 4, declared (M20a): Postgres keeps no take-back for a scope-less sink
# =================================================================================================


async def test_caveat4_declared_on_postgres_a_failing_scopeless_sink_leaves_the_summary_without_its_event():
    class Scopeless:
        def emit(self, *args, **kwargs):
            raise RuntimeError("the event store failed")

    backend = Persistence.postgres(DSN, create_schema=False)
    runner = Runner({"w": Answers()}, persistence=backend)
    run = await runner.run(AgentSpec(id="a", instructions="i"), "go",
                           RunConfig(tenant_id=TENANT, project_id=PROJECT, model_override="w:w"))
    assert run.status is RunStatus.COMPLETED, run.error
    sessions = backend.session_store_for(scope(run_id=run.run_id))
    with pytest.raises(RuntimeError, match="the event store failed"):
        await asyncio.to_thread(sessions.append_with_event, run.run_id, Message(role=Role.USER, content="summary"),
                                Scopeless(), EventType.CONTEXT_COMPACTED, {})
    [(stored,)] = await asyncio.to_thread(
        query, "SELECT count(*) FROM messages WHERE run_id = %s AND content = 'summary'", (run.run_id,))
    [(events,)] = await asyncio.to_thread(
        query, "SELECT count(*) FROM run_events WHERE run_id = %s AND event_type = 'ContextCompacted'", (run.run_id,))
    assert (stored, events) == (1, 0), "the declared behaviour changed: update the README's limits"
