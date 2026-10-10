"""M20a gate: an honest compaction record (FR-85, FR-86, AC-68, AC-69).

DECISION-f56a9415 (owner): ContextCompacted records tokens_before and tokens_after on one
basis -- the whole-request estimate of the view before and after -- and the number that
decided the compaction beside them as threshold_measure, with threshold_basis; a
compaction's summary message and its event are written together or not at all, in one
transaction on Postgres. KNOWLEDGE-43a86c0a holds the readings taken.
"""

from __future__ import annotations

import asyncio
import json
import os
import random

import psycopg
import pytest

from agentsdk import AgentSpec, ContextPolicy, Persistence, RunConfig, RunStatus, Runner
from agentsdk import postgres
from agentsdk.config import normalise_database_url
from agentsdk.events import EventType, InMemoryEventSink
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.postgres import PostgresEventStore, RunScope
from agentsdk.primitives import Message, Role, ToolCall
from agentsdk.session import InMemorySessionStore
from agentsdk.tools import ResultProvenance, Tool, ToolSpec

DSN = normalise_database_url(os.environ.get("DATABASE_URL"))
TENANT, PROJECT = "SYN-m20a", "p-m20a"
SUMMARY = "--- summary of earlier"
INSTRUCTIONS = "read every page"


def _tool(name, size, external=False):
    async def fn(n: int = 0) -> str:
        return f"{name} {n}: " + ("lorem ipsum " * size)

    return Tool(
        spec=ToolSpec(name=name, description=f"the {name} tool",
                      input_schema={"type": "object", "properties": {"n": {"type": "integer"}}},
                      **({"result_provenance": ResultProvenance.external()} if external else {})),
        fn=fn,
    )


READ, CLEAN = _tool("read", 100, external=True), _tool("clean", 100)
READER = AgentSpec(id="reader", instructions=INSTRUCTIONS, tool_profile=("read", "clean"))
POLICY = ContextPolicy(context_window=1000, keep_recent_turns=1)


def _chars(request):
    return (len(request.instructions or "") + len(json.dumps(list(request.tools), default=str))
            + sum(_message_chars(m) for m in request.messages))


def _message_chars(message):
    """What FR-85's estimate counts for a message, written out here rather than taken
    from the code under test."""
    size = len(message.content or "")
    for call in message.tool_calls:
        size += len(call.name) + len(json.dumps(call.arguments, default=str))
    for result in message.tool_results:
        size += len(result.content or "")
    return size


class Model:
    """Calls `script[i]` (a tool name or a list of them) on turn i, then answers; answers
    a summarising call with `summary`. `report` is the prompt_tokens it reports: the
    request's real size by default, or a constant (0 reports nothing usable)."""

    def __init__(self, script, *, summary="the summary of the earlier turns", report=None):
        self.script, self.summary, self.report = list(script), summary, report
        self.requests = []

    async def send(self, request):
        tokens = max(1, _chars(request) // 4) if self.report is None else self.report
        if (request.instructions or "").startswith("You compact"):
            return ModelResponse(message=Message(role=Role.ASSISTANT, content=self.summary),
                                 stop_reason=StopReason.END_TURN, usage=Usage(tokens, 3, tokens + 3))
        self.requests.append(request)
        turn = len(self.requests)
        if turn <= len(self.script):
            names = self.script[turn - 1]
            names = [names] if isinstance(names, str) else names
            calls = tuple(ToolCall(id=f"c{turn}-{i}", name=n, arguments={"n": turn}) for i, n in enumerate(names))
            return ModelResponse(message=Message(role=Role.ASSISTANT, tool_calls=calls),
                                 stop_reason=StopReason.TOOL_CALLS, usage=Usage(tokens, 2, tokens + 2))
        return ModelResponse(message=Message(role=Role.ASSISTANT, content="done"),
                             stop_reason=StopReason.END_TURN, usage=Usage(tokens, 2, tokens + 2))


def config(**options):
    return RunConfig(tenant_id=TENANT, project_id=PROJECT, model_override="m:fake", max_turns=40, **options)


def compactions(events):
    return [e.payload for e in events if e.event_type is EventType.CONTEXT_COMPACTED]


def query(sql, params=()):
    with psycopg.connect(DSN) as conn:
        return conn.execute(sql, params).fetchall()


def remove_rows():
    with psycopg.connect(DSN, autocommit=True) as conn:
        ids = [r[0] for r in conn.execute("SELECT run_id FROM runs WHERE tenant_id = %s", (TENANT,)).fetchall()]
        conn.execute("DELETE FROM artifacts WHERE tenant_id = %s", (TENANT,))
        for table in ("run_events", "messages", "execution_manifests"):
            conn.execute(f"DELETE FROM {table} WHERE run_id = ANY(%s)", (ids,))
        conn.execute("DELETE FROM runs WHERE run_id = ANY(%s)", (ids,))


@pytest.fixture(autouse=True)
def remove_what_the_test_wrote():
    yield
    if DSN:
        remove_rows()


@pytest.fixture(params=["memory", "postgres"])
def backend(request):
    return None if request.param == "memory" else Persistence.postgres(DSN, create_schema=False)


async def stored(runner, backend, run_id):
    """The run's stored messages and events, read back from the store itself."""
    if backend is None:
        return list(runner._sessions.history(run_id)), None
    scope = RunScope(run_id=run_id, tenant_id=TENANT, project_id=PROJECT)
    history = await asyncio.to_thread(backend.session_store_for(scope).history, run_id)
    events = await asyncio.to_thread(
        query, "SELECT sequence_no, event_type FROM run_events WHERE run_id = %s ORDER BY sequence_no", (run_id,))
    return list(history), events


# =================================================================================================
# FR-85 and AC-68: one basis, and the deciding measure named
# =================================================================================================


def recompute(history, replaced_by_compaction, instructions, tools):
    """Each compaction's before and after, rebuilt from the stored history and the
    artifacts alone: the view is the task, the latest summary, and every message stored
    before the compaction's summary that no earlier compaction replaced."""
    summaries = [i for i, m in enumerate(history) if m.role is Role.USER and (m.content or "").startswith(SUMMARY)]
    assert len(summaries) == len(replaced_by_compaction), (summaries, len(replaced_by_compaction))
    fixed = len(instructions) + len(json.dumps(list(tools), default=str))

    def estimate(view):
        return (fixed + sum(_message_chars(m) for m in view)) // 4

    dropped, previous, pairs = set(), None, []
    for at, replaced in zip(summaries, replaced_by_compaction):
        rest = [i for i in range(1, at) if i not in summaries]
        before = [history[0]] + ([history[previous]] if previous is not None else []) + [
            history[i] for i in rest if i not in dropped]
        dropped |= set(replaced)
        after = [history[0], history[at]] + [history[i] for i in rest if i not in dropped]
        pairs.append((estimate(before), estimate(after)))
        previous = at
    return pairs


async def run_and_check(backend, script, *, window, keep, compact_at, report, sizes):
    tools = [_tool("read", sizes[0], external=True), _tool("clean", sizes[1])]
    model = Model(script, report=report)
    runner = Runner({"m": model}, tools=tools, persistence=backend)
    policy = ContextPolicy(context_window=window, keep_recent_turns=keep, compact_at=compact_at)
    result = await runner.run(READER, "go", config(context_policy=policy))
    assert result.status is RunStatus.COMPLETED, result.error
    done = compactions(result.events)
    store = runner._artifacts_for(TENANT, PROJECT)
    replaced = []
    for event in done:
        body = json.loads(await store.get(event["artifact"].rsplit(":", 1)[-1]))
        replaced.append([m["stored_index"] for m in body])
    history, _ = await stored(runner, backend, result.run_id)
    pairs = recompute(history, replaced, INSTRUCTIONS, model.requests[0].tools)
    for event, (before, after) in zip(done, pairs):
        assert (event["tokens_before"], event["tokens_after"]) == (before, after), (event, before, after)
        assert event["threshold_measure"] >= compact_at * window, event
        assert event["threshold_basis"] in ("reported_plus_estimate", "estimate"), event
    return done


async def test_ac68_every_compaction_records_its_pair_on_one_basis_and_names_what_decided_it():
    """Varied script, sizes, window, kept turns, threshold and provider reports; the
    property is checked on every compaction, whichever the schedule makes (KNOWLEDGE-14f3ec8b)."""
    rng = random.Random(20261010)
    bases, total = set(), 0
    for _ in range(40):
        script = [rng.choice(["read", "clean", ["read", "clean"], ["clean", "clean"]]) for _ in range(rng.randint(4, 12))]
        done = await run_and_check(
            None, script, window=rng.choice([600, 1000, 1500, 2500]), keep=rng.choice([1, 2, 3]),
            compact_at=rng.choice([0.5, 0.75, 0.9]), report=rng.choice([None, None, 0]),
            sizes=(rng.choice([20, 100, 300]), rng.choice([20, 100, 300])),
        )
        bases |= {e["threshold_basis"] for e in done}
        total += len(done)
    assert total >= 40, f"the premise failed: only {total} compactions"
    assert bases == {"reported_plus_estimate", "estimate"}, bases


async def test_ac68_on_postgres_the_pair_matches_the_stored_history():
    backend = Persistence.postgres(DSN, create_schema=False)
    done = await run_and_check(backend, ["read"] * 8, window=1000, keep=1, compact_at=0.75, report=None, sizes=(100, 100))
    assert done


async def test_the_deciding_measure_is_the_providers_count_when_one_applies():
    """A provider that reports a large prompt for a small history: the decider is its
    count, while the pair, on the estimate's basis, stays small -- and records honestly
    that a summary longer than the little it replaced made the history larger."""
    model = Model(["clean"] * 5, report=900)
    runner = Runner({"m": model}, tools=[_tool("clean", 2)])
    agent = AgentSpec(id="a", instructions=INSTRUCTIONS, tool_profile=("clean",))
    result = await runner.run(agent, "go", config(context_policy=POLICY))
    [first, *_] = compactions(result.events)
    assert first["threshold_basis"] == "reported_plus_estimate" and first["threshold_measure"] >= 900
    assert first["tokens_before"] < 750
    assert first["tokens_after"] > first["tokens_before"], "the pair hid that this compaction grew the history"


async def test_with_no_usable_report_the_deciding_measure_is_the_estimate_and_equals_before():
    model = Model(["read"] * 6, report=0)
    runner = Runner({"m": model}, tools=[READ, CLEAN])
    result = await runner.run(READER, "go", config(context_policy=POLICY))
    done = compactions(result.events)
    assert done and all(e["threshold_basis"] == "estimate" for e in done)
    assert all(e["threshold_measure"] == e["tokens_before"] for e in done)


# =================================================================================================
# FR-86 and AC-69: the summary and its event, both or neither
# =================================================================================================


def _no_partial_record(history, events):
    summaries = [m for m in history if m.role is Role.USER and (m.content or "").startswith(SUMMARY)]
    compacted = [e for e in events if (e[1] if isinstance(e, tuple) else e.event_type.value) == "ContextCompacted"]
    assert len(summaries) == len(compacted), (len(summaries), len(compacted))
    numbers = [e[0] if isinstance(e, tuple) else e.sequence_no for e in events]
    assert numbers == list(range(1, len(numbers) + 1)), numbers
    return len(summaries)


async def test_ac69_a_compaction_that_completes_writes_both(backend):
    runner = Runner({"m": Model(["read"] * 6)}, tools=[READ, CLEAN], persistence=backend)
    result = await runner.run(READER, "go", config(context_policy=POLICY))
    history, rows = await stored(runner, backend, result.run_id)
    assert _no_partial_record(history, rows if rows is not None else result.events) >= 1


async def test_ac69_an_event_that_cannot_be_written_leaves_no_summary_in_memory(monkeypatch):
    real = InMemoryEventSink.emit

    def refuses(self, event_type, payload=None, **identifiers):
        if event_type is EventType.CONTEXT_COMPACTED:
            raise RuntimeError("the event store failed")
        return real(self, event_type, payload, **identifiers)

    monkeypatch.setattr(InMemoryEventSink, "emit", refuses)
    runner = Runner({"m": Model(["read"] * 6)}, tools=[READ, CLEAN])
    result = await runner.run(READER, "go", config(context_policy=POLICY))
    assert result.status is RunStatus.FAILED and "the event store failed" in result.error, result.error
    history, _ = await stored(runner, None, result.run_id)
    assert _no_partial_record(history, result.events) == 0


async def test_ac69_a_summary_that_cannot_be_written_leaves_no_event_in_memory(monkeypatch):
    real = InMemorySessionStore.append

    def refuses(self, run_id, message):
        if message.role is Role.USER and (message.content or "").startswith(SUMMARY):
            raise RuntimeError("the session store failed")
        return real(self, run_id, message)

    monkeypatch.setattr(InMemorySessionStore, "append", refuses)
    runner = Runner({"m": Model(["read"] * 6)}, tools=[READ, CLEAN])
    result = await runner.run(READER, "go", config(context_policy=POLICY))
    assert result.status is RunStatus.FAILED and "the session store failed" in result.error, result.error
    history, _ = await stored(runner, None, result.run_id)
    assert _no_partial_record(history, result.events) == 0


async def test_ac69_an_event_that_cannot_be_written_rolls_the_summary_back_on_postgres(monkeypatch):
    """The event fails after the summary row was inserted, in the same transaction: the
    rollback takes the summary with it."""
    real = PostgresEventStore.write

    def refuses(self, conn, event_type, payload=None, **identifiers):
        if event_type is EventType.CONTEXT_COMPACTED:
            raise RuntimeError("the event store failed")
        return real(self, conn, event_type, payload, **identifiers)

    monkeypatch.setattr(PostgresEventStore, "write", refuses)
    backend = Persistence.postgres(DSN, create_schema=False)
    runner = Runner({"m": Model(["read"] * 6)}, tools=[READ, CLEAN], persistence=backend)
    result = await runner.run(READER, "go", config(context_policy=POLICY))
    assert result.status is RunStatus.FAILED and "the event store failed" in result.error, result.error
    history, rows = await stored(runner, backend, result.run_id)
    assert _no_partial_record(history, rows) == 0
    [(status,)] = await asyncio.to_thread(query, "SELECT status FROM runs WHERE run_id = %s", (result.run_id,))
    assert status == "failed"


async def test_ac69_a_summary_that_cannot_be_written_leaves_no_event_on_postgres(monkeypatch):
    real = postgres._insert_message

    def refuses(conn, scope, run_id, message):
        if message.role is Role.USER and (message.content or "").startswith(SUMMARY):
            raise RuntimeError("the session store failed")
        return real(conn, scope, run_id, message)

    monkeypatch.setattr(postgres, "_insert_message", refuses)
    backend = Persistence.postgres(DSN, create_schema=False)
    runner = Runner({"m": Model(["read"] * 6)}, tools=[READ, CLEAN], persistence=backend)
    result = await runner.run(READER, "go", config(context_policy=POLICY))
    assert result.status is RunStatus.FAILED and "the session store failed" in result.error, result.error
    history, rows = await stored(runner, backend, result.run_id)
    assert _no_partial_record(history, rows) == 0


async def test_the_written_event_reaches_the_runs_handle_in_order(backend):
    """Written inside the transaction, the event still reaches RunResult.events and the
    RunHandle stream once it is stored, at its stored sequence number."""
    runner = Runner({"m": Model(["read"] * 6)}, tools=[READ, CLEAN], persistence=backend)
    handle = await runner.start(READER, "go", config(context_policy=POLICY))
    streamed = [event async for event in handle.events()]
    result = await handle.result()
    assert [e.sequence_no for e in streamed] == [e.sequence_no for e in result.events]
    assert compactions(streamed) and compactions(streamed) == compactions(result.events)
    if backend is not None:
        _, rows = await stored(runner, backend, result.run_id)
        assert [(e.sequence_no, e.event_type.value) for e in result.events] == [tuple(r) for r in rows]


async def test_a_session_store_without_the_combined_write_still_records_both():
    """A custom store keeps working: the two writes are made in turn, as before M20a."""

    class Plain:
        def __init__(self):
            self.inner = InMemorySessionStore()

        def append(self, run_id, message):
            self.inner.append(run_id, message)

        def history(self, run_id):
            return self.inner.history(run_id)

    sessions = Plain()
    runner = Runner({"m": Model(["read"] * 6)}, tools=[READ, CLEAN], session_store=sessions)
    result = await runner.run(READER, "go", config(context_policy=POLICY))
    assert _no_partial_record(sessions.history(result.run_id), result.events) >= 1


def test_the_combined_write_refuses_a_sink_for_another_run():
    sessions = InMemorySessionStore()
    other = InMemoryEventSink(TENANT, PROJECT, "another-run")
    with pytest.raises(ValueError, match="one run's"):
        sessions.append_with_event("this-run", Message(role=Role.USER, content="s"), other,
                                   EventType.CONTEXT_COMPACTED, {})
    assert sessions.history("this-run") == []


def test_the_combined_write_refuses_a_sink_for_another_run_on_postgres():
    backend = Persistence.postgres(DSN, create_schema=False)
    scope = RunScope(run_id="00000000-0000-4000-8000-000000000001", tenant_id=TENANT, project_id=PROJECT)
    sessions = backend.session_store_for(scope)
    other = PostgresEventStore(DSN, TENANT, PROJECT, "00000000-0000-4000-8000-000000000002")
    with pytest.raises(ValueError, match="one run's"):
        sessions.append_with_event(scope.run_id, Message(role=Role.USER, content="s"), other,
                                   EventType.CONTEXT_COMPACTED, {})
