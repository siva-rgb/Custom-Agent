"""M21 gate: the carried items (FR-87, FR-88, AC-70).

FR-87 repairs M20a round 1's F1 and F2 (KNOWLEDGE-afac039f): the combined write never
refuses or crashes on a sink that names no scope, and a sink that names one is refused
unless it names the run being written to by tenant and project as well as run id, on
both stores. FR-88's decisions on M20's C4 to C6 are added at M21's pre-flight.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from decimal import Decimal

import psycopg
import pytest

from agentsdk import AgentSpec, ContextPolicy, Persistence, RunConfig, RunStatus, Runner
from agentsdk import api, postgres
from agentsdk.config import normalise_database_url
from agentsdk.events import EventType, InMemoryEventSink
from agentsdk.handle import PublishingSink
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.postgres import PostgresEventStore, RunScope
from agentsdk.primitives import Message, Role, ToolCall
from agentsdk.session import InMemorySessionStore
from agentsdk.tools import Tool, ToolSpec

DSN = normalise_database_url(os.environ.get("DATABASE_URL"))
TENANT, PROJECT = "SYN-m21", "p-m21"
SUMMARY = "--- summary of earlier"


async def page(n: int = 0) -> str:
    return f"page {n}: " + ("lorem ipsum " * 100)


PAGE = Tool(spec=ToolSpec(name="page", description="a page",
                          input_schema={"type": "object", "properties": {"n": {"type": "integer"}}}), fn=page)
READER = AgentSpec(id="reader", instructions="read", tool_profile=("page",))
POLICY = ContextPolicy(context_window=1000, keep_recent_turns=1)


class Model:
    """Reads a page a turn for six turns, then answers; answers a summarising call."""

    def __init__(self):
        self.turn = 0

    async def send(self, request):
        size = sum(len(m.content or "") + sum(len(r.content or "") for r in m.tool_results) for m in request.messages)
        usage = Usage(size // 4 + 1, 2, size // 4 + 3)
        if (request.instructions or "").startswith("You compact"):
            return ModelResponse(message=Message(role=Role.ASSISTANT, content="the summary"),
                                 stop_reason=StopReason.END_TURN, usage=usage)
        self.turn += 1
        if self.turn <= 6:
            call = ToolCall(id=f"c{self.turn}", name="page", arguments={"n": self.turn})
            return ModelResponse(message=Message(role=Role.ASSISTANT, tool_calls=(call,)),
                                 stop_reason=StopReason.TOOL_CALLS, usage=usage)
        return ModelResponse(message=Message(role=Role.ASSISTANT, content="done"),
                             stop_reason=StopReason.END_TURN, usage=usage)


def config():
    return RunConfig(tenant_id=TENANT, project_id=PROJECT, model_override="m:fake", max_turns=20,
                     context_policy=POLICY)


def query(sql, params=()):
    with psycopg.connect(DSN) as conn:
        return conn.execute(sql, params).fetchall()


@pytest.fixture(autouse=True)
def remove_what_the_test_wrote():
    yield
    if DSN:
        with psycopg.connect(DSN, autocommit=True) as conn:
            ids = [r[0] for r in conn.execute("SELECT run_id FROM runs WHERE tenant_id = %s", (TENANT,)).fetchall()]
            conn.execute("DELETE FROM artifacts WHERE tenant_id = %s", (TENANT,))
            for table in ("run_events", "messages", "execution_manifests"):
                conn.execute(f"DELETE FROM {table} WHERE run_id = ANY(%s)", (ids,))
            conn.execute("DELETE FROM runs WHERE run_id = ANY(%s)", (ids,))


def summaries(history):
    return [m for m in history if m.role is Role.USER and (m.content or "").startswith(SUMMARY)]


def compacted(events):
    return [e for e in events if e.event_type is EventType.CONTEXT_COMPACTED]


def scope(tenant=TENANT, project=PROJECT, run_id=None):
    return RunScope(run_id=run_id or str(uuid.uuid4()), tenant_id=tenant, project_id=project)


# =================================================================================================
# F1: a sink that names no scope
# =================================================================================================


class ScopelessSink(InMemoryEventSink):
    """A custom sink that names no scope: the run's PublishingSink then reports None."""

    scope = None


async def test_f1_a_run_whose_sink_names_no_scope_compacts_and_records_both_in_memory(monkeypatch):
    """Under M20a this run failed with a TypeError: PublishingSink.scope is None over
    a scope-less sink, and the check indexed it. Under M20 it completed."""
    monkeypatch.setattr(api, "InMemoryEventSink", ScopelessSink)
    runner = Runner({"m": Model()}, tools=[PAGE])
    result = await runner.run(READER, "go", config())
    assert result.status is RunStatus.COMPLETED, result.error
    history = runner._sessions.history(result.run_id)
    assert len(summaries(history)) == len(compacted(result.events)) >= 1


async def test_f1_a_run_whose_sink_names_no_scope_compacts_and_records_both_on_postgres(monkeypatch):
    monkeypatch.setattr(PostgresEventStore, "scope", None)
    backend = Persistence.postgres(DSN, create_schema=False)
    runner = Runner({"m": Model()}, tools=[PAGE], persistence=backend)
    result = await runner.run(READER, "go", config())
    assert result.status is RunStatus.COMPLETED, result.error
    [(stored,)] = await asyncio.to_thread(
        query, "SELECT count(*) FROM messages WHERE run_id = %s AND content LIKE '--- summary of earlier%%'",
        (result.run_id,))
    [(events,)] = await asyncio.to_thread(
        query, "SELECT count(*) FROM run_events WHERE run_id = %s AND event_type = 'ContextCompacted'",
        (result.run_id,))
    assert stored == events >= 1


def test_f1_a_publishing_sink_over_a_scopeless_sink_keeps_the_take_back():
    """The two writes in turn, and still both or neither in memory."""
    loop = asyncio.new_event_loop()
    try:
        inner = ScopelessSink(TENANT, PROJECT, "r")

        class Fails(ScopelessSink):
            def emit(self, *args, **kwargs):
                raise RuntimeError("the event store failed")

        store = InMemorySessionStore()
        run = scope(run_id="r")
        bound = store.bind(run)
        ok = PublishingSink(inner, lambda e: None, loop)
        bound.append_with_event("r", Message(role=Role.USER, content="s1"), ok, EventType.CONTEXT_COMPACTED, {})
        failing = PublishingSink(Fails(TENANT, PROJECT, "r"), lambda e: None, loop)
        with pytest.raises(RuntimeError, match="the event store failed"):
            bound.append_with_event("r", Message(role=Role.USER, content="s2"), failing, EventType.CONTEXT_COMPACTED, {})
        assert [m.content for m in bound.history("r")] == ["s1"]
        assert len(inner.events()) == 1
    finally:
        loop.close()


# =================================================================================================
# F2: a sink that names another run, tenant or project
# =================================================================================================


@pytest.mark.parametrize("other", ["run", "tenant", "project"])
def test_f2_a_sink_for_another_run_tenant_or_project_is_refused_in_memory(other):
    run = scope(run_id="the-run")
    names = {"run": (TENANT, PROJECT, "another-run"), "tenant": ("SYN-m21-other", PROJECT, "the-run"),
             "project": (TENANT, "p-other", "the-run")}[other]
    sink = InMemoryEventSink(*names)
    store = InMemorySessionStore()
    with pytest.raises(ValueError, match="one run's"):
        store.bind(run).append_with_event("the-run", Message(role=Role.USER, content="s"), sink,
                                          EventType.CONTEXT_COMPACTED, {})
    assert store.bind(run).history("the-run") == [] and sink.events() == ()


@pytest.mark.parametrize("other", ["run", "tenant", "project"])
def test_f2_a_sink_for_another_run_tenant_or_project_is_refused_on_postgres(other):
    run = scope()
    names = {"run": (TENANT, PROJECT, str(uuid.uuid4())), "tenant": ("SYN-m21-other", PROJECT, run.run_id),
             "project": (TENANT, "p-other", run.run_id)}[other]
    sessions = Persistence.postgres(DSN, create_schema=False).session_store_for(run)
    with pytest.raises(ValueError, match="one run's"):
        sessions.append_with_event(run.run_id, Message(role=Role.USER, content="s"),
                                   PostgresEventStore(DSN, *names), EventType.CONTEXT_COMPACTED, {})


def test_f2_two_tenants_runs_with_one_run_id_keep_separate_histories():
    store = InMemorySessionStore()
    a, b = scope(run_id="shared"), scope(tenant="SYN-m21-other", run_id="shared")
    store.bind(a).append("shared", Message(role=Role.USER, content="tenant a"))
    store.bind(b).append("shared", Message(role=Role.USER, content="tenant b"))
    assert [m.content for m in store.bind(a).history("shared")] == ["tenant a"]
    assert [m.content for m in store.bind(b).history("shared")] == ["tenant b"]
    with pytest.raises(ValueError, match="more than one tenant"):
        store.history("shared")


def test_a_bound_view_writes_only_its_own_run():
    store = InMemorySessionStore()
    with pytest.raises(ValueError, match="bound to run"):
        store.bind(scope(run_id="mine")).append("yours", Message(role=Role.USER, content="x"))


async def test_an_unbound_read_still_finds_a_runners_run():
    """The Runner binds its in-memory store per run; reading it back by run id alone,
    as tests and examples do, still works."""
    runner = Runner({"m": Model()}, tools=[PAGE])
    result = await runner.run(READER, "go", RunConfig(tenant_id=TENANT, project_id=PROJECT, model_override="m:fake"))
    assert runner._sessions.history(result.run_id)[0].content == "go"


async def test_a_runners_in_memory_run_is_filed_under_its_own_tenant_and_project():
    """FR-87: the Runner binds its in-memory store to each run, so the run's history is
    found under its tenant and project and under no other with the same run id."""
    runner = Runner({"m": Model()}, tools=[PAGE])
    result = await runner.run(READER, "go", RunConfig(tenant_id=TENANT, project_id=PROJECT, model_override="m:fake"))
    own = runner._sessions.bind(scope(run_id=result.run_id))
    assert own.history(result.run_id)[0].content == "go"
    for other in (scope(tenant="SYN-m21-other", run_id=result.run_id), scope(project="p-other", run_id=result.run_id)):
        assert runner._sessions.bind(other).history(result.run_id) == []


# =================================================================================================
# FR-88: M20's C4 to C6 (DECISION-153046de)
# =================================================================================================


async def test_c4_declared_a_run_that_ends_before_its_first_request_records_what_it_would_be_sent():
    """C4, declared: tools_sent is configuration, written at run start (FR-11)."""
    from agentsdk.hooks import HookAction, HookOutcome, RuntimeHook

    class HaltsFirst(RuntimeHook):
        def before_model(self, request):
            return HookOutcome(action=HookAction.HALT, reason="halted before any request")

    backend = Persistence.postgres(DSN, create_schema=False)
    model = Model()
    runner = Runner({"m": model}, tools=[PAGE], persistence=backend, hook=HaltsFirst())
    result = await runner.run(READER, "go", RunConfig(tenant_id=TENANT, project_id=PROJECT, model_override="m:fake",
                                                       context_policy=ContextPolicy(compact_at=None)))
    assert result.status is RunStatus.FAILED and model.turn == 0
    [(sent,)] = await asyncio.to_thread(
        query, "SELECT tools_sent FROM execution_manifests WHERE run_id = %s", (result.run_id,))
    assert [t["name"] for t in sent] == ["page"], "the declared behaviour changed: update the README's limits"


async def test_c5_declared_a_permission_policy_beyond_the_profile_does_not_widen_what_is_sent():
    """C5, declared: under a ContextPolicy the agent is sent its profile's tools; a tool
    its own permission_policy permits beyond the profile is refused as an unknown tool."""
    from agentsdk.permissions import AllowlistPermissionChecker

    async def other(n: int = 0) -> str:
        return "other"

    OTHER = Tool(spec=ToolSpec(name="other", description="another",
                               input_schema={"type": "object", "properties": {"n": {"type": "integer"}}}), fn=other)
    seen = []

    class Calls:
        async def send(self, request):
            seen.append(sorted(s["function"]["name"] for s in request.tools))
            if len(seen) == 1:
                calls = (ToolCall(id="c1", name="other", arguments={}), ToolCall(id="c2", name="nosuch", arguments={}))
                return ModelResponse(message=Message(role=Role.ASSISTANT, tool_calls=calls),
                                     stop_reason=StopReason.TOOL_CALLS, usage=Usage(5, 1, 6))
            Calls.results = request.messages[-1].tool_results
            return ModelResponse(message=Message(role=Role.ASSISTANT, content="done"),
                                 stop_reason=StopReason.END_TURN, usage=Usage(5, 1, 6))

    agent = AgentSpec(id="a", instructions="i", tool_profile=("page",),
                      permission_policy=AllowlistPermissionChecker({"page", "other"}))
    runner = Runner({"m": Calls()}, tools=[PAGE, OTHER])
    await runner.run(agent, "go", RunConfig(tenant_id=TENANT, project_id=PROJECT, model_override="m:fake",
                                            context_policy=ContextPolicy(compact_at=None)))
    assert seen[0] == ["page"]
    hidden, unknown = Calls.results
    assert hidden.is_error and hidden.content.replace("other", "X") == unknown.content.replace("nosuch", "X")


def _registry():
    """'p', the planner, with a window and a price; 'w', the worker's model, unknown."""
    from agentsdk.registry import ModelCapabilities, ModelEntry, ModelPricing, ModelRegistry

    price = ModelPricing(input=Decimal("0.01"), output=Decimal("0.01"))
    return ModelRegistry([ModelEntry(provider="t", model_id="p", model_version="1", adapter_version="t",
                                     capabilities=ModelCapabilities(max_context_tokens=100_000, pricing=price))])


class Planner:
    def __init__(self):
        self.calls = 0

    async def send(self, request):
        self.calls += 1
        if self.calls <= 2:
            plan = {"nodes": [{"node_id": "n", "objective": "go", "assigned_role": "worker", "dependencies": []}]}
            return ModelResponse(message=Message(role=Role.ASSISTANT, tool_calls=(
                ToolCall(id=f"plan-{self.calls}", name="run_plan", arguments=plan),)),
                stop_reason=StopReason.TOOL_CALLS, usage=Usage(5, 1, 6))
        self.reports = [r.content for m in request.messages for r in m.tool_results]
        return ModelResponse(message=Message(role=Role.ASSISTANT, content="gave up"),
                             stop_reason=StopReason.END_TURN, usage=Usage(5, 1, 6))


class NeverCalled:
    calls = 0

    async def send(self, request):
        NeverCalled.calls += 1
        raise AssertionError("a child ran")


@pytest.mark.parametrize("ceiling", ["tokens", "usd"])
async def test_c6_a_role_that_cannot_run_ends_the_plan_at_submission_and_spends_nothing(ceiling):
    """C6, repaired. Under a token ceiling the worker's model has no known window; under
    a USD ceiling the policy names a window, so the only thing missing is a price. Before
    M21 either was refused at spawn, as a node_error mid-run."""
    from agentsdk import BudgetPolicy
    from agentsdk.orchestrator import Orchestrator

    NeverCalled.calls = 0
    budget = BudgetPolicy(run_ceiling_tokens=100_000) if ceiling == "tokens" else BudgetPolicy(run_ceiling_usd=Decimal("5"))
    window = None if ceiling == "tokens" else ContextPolicy(context_window=100_000)
    orchestrator = Orchestrator(
        roles={"worker": AgentSpec(id="worker", instructions="w", preferred_model="w:w")}, policy=budget,
        context_policy=window)
    planner = Planner()
    runner = Runner({"p": planner, "w": NeverCalled()}, tools=[orchestrator.tool], model_registry=_registry())
    result = await runner.run(orchestrator.agent, "do it",
                              orchestrator.config(tenant_id=TENANT, project_id=PROJECT, model_override="p:p"))
    assert result.status is RunStatus.FAILED, result.error
    assert "role 'worker' cannot run" in result.error and "'w'" in result.error, result.error
    assert "cannot be replanned" in result.error
    assert ("no known window" if ceiling == "tokens" else "USD budget needs a price") in result.error, result.error
    assert NeverCalled.calls == 0 and orchestrator.plans(result.run_id) == ()
    # The second submission is refused: the plan has ended.
    assert any("has ended and cannot run again" in (r or "") for r in planner.reports), planner.reports
    assert len(runner._started) == 1, "a child run was started"


# =================================================================================================
# Round 1 finding (DECISION-f12abafb): unbound calls under concurrency
# =================================================================================================


def test_r1_unbound_calls_never_raise_while_other_threads_add_runs():
    """Round 1: _unbound iterated the store's dict while other threads added keys, so an
    unbound append or read raised 'dictionary changed size during iteration' (8 of 8
    attempts in the reviewer's probe, 0 of 8 before M21). Nothing is patched here: six
    threads bind new runs while six make unbound calls, repeated, with the thread switch
    interval at a microsecond so the threads interleave inside each call, as
    KNOWLEDGE-93fa7f44's sink test does: at the default 5 ms a whole scan can finish
    between switches, and the race hides."""
    import sys
    import threading

    switch = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        _contend_unbound()
    finally:
        sys.setswitchinterval(switch)


def _contend_unbound():
    import threading

    for attempt in range(8):
        store = InMemorySessionStore()
        errors, stop = [], threading.Event()
        own = [f"unbound-{attempt}-{i}" for i in range(6)]

        def binds(worker):
            n = 0
            while not stop.is_set():
                n += 1
                run = scope(run_id=f"bound-{attempt}-{worker}-{n}")
                try:
                    store.bind(run).append(run.run_id, Message(role=Role.USER, content="b"))
                except Exception as exc:  # noqa: BLE001
                    errors.append(repr(exc))

        def unbound(run_id):
            for _ in range(300):
                try:
                    store.append(run_id, Message(role=Role.USER, content="u"))
                    store.history(run_id)
                except Exception as exc:  # noqa: BLE001
                    errors.append(repr(exc))

        writers = [threading.Thread(target=binds, args=(w,)) for w in range(6)]
        readers = [threading.Thread(target=unbound, args=(r,)) for r in own]
        for t in writers + readers:
            t.start()
        for t in readers:
            t.join()
        stop.set()
        for t in writers:
            t.join()
        assert errors == [], f"attempt {attempt}: {len(errors)} errors, e.g. {errors[:2]}"
        assert all(len(store.history(r)) == 300 for r in own)
