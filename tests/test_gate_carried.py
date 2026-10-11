"""M25 gate: the carried items (FR-99, AC-77).

M24's and M24a's reviews (KNOWLEDGE-4301be95, KNOWLEDGE-06a8cf2f) left caveats carried to
Phase 3's second increment; the owner had them settled first (DECISION-581a647b). B1, W2,
K1, P1, P2, C5 and example 21's taint check are repaired here; W1, C6 and B2 are declared,
and asserted as declared.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest

from agentsdk import AgentSpec, RunConfig, Runner
from agentsdk import claimlock
from agentsdk.builtin_tools import fetch_tool
from agentsdk.claimlock import ATTEMPTS, ClaimLock, KeyFailed, lock_key
from agentsdk.config import normalise_database_url
from agentsdk.events import EventType
from agentsdk.manifest import build_manifest
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.postgres import PostgresLedgerStore
from agentsdk.primitives import (
    ContentProvenance, InstructionAuthority, Message, Origin, Role, TaintFlag, ToolCall, TrustZone,
)

DSN = normalise_database_url(os.environ.get("DATABASE_URL"))
REPO = Path(__file__).resolve().parent.parent


def held_locks() -> int:
    with psycopg.connect(DSN) as conn:
        return conn.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid = 4").fetchone()[0]


def in_use(settled: bool = True) -> int:
    """Pooled connections checked out; `settled` first waits for the pool to finish
    opening its own, which it cannot while a holder has its only connection."""
    from agentsdk import postgres

    pool = postgres._pool(DSN)
    if settled:
        pool.wait(timeout=10)
    stats = pool.get_stats()
    return stats.get("pool_size", 0) - stats.get("pool_available", 0)


class NoDelay:
    async def __call__(self, seconds):
        await asyncio.sleep(0)


# =================================================================================================
# B1: a waiter cancelled in its backoff spends no attempt
# =================================================================================================


class FirstBackoffHangs:
    """The first backoff never ends, so the waiter in it can be cancelled there."""

    def __init__(self):
        self.entered = asyncio.Event()
        self.calls = 0

    async def __call__(self, seconds):
        self.calls += 1
        if self.calls == 1:
            self.entered.set()
            await asyncio.Event().wait()
        await asyncio.sleep(0)


async def test_b1_a_waiter_cancelled_during_its_backoff_spends_no_attempt_and_the_next_retries():
    backoff = FirstBackoffHangs()
    lock = ClaimLock(sleep=backoff, random_fraction=lambda: 0.0)
    attempts = []

    async def op(waited):
        attempts.append(len(attempts))
        await asyncio.sleep(0.001)
        raise RuntimeError("down")

    calls = [asyncio.create_task(lock.run("k", op)) for _ in range(4)]
    await backoff.entered.wait()
    calls[1].cancel()  # in its backoff, before its operation started
    outcomes = await asyncio.gather(*calls, return_exceptions=True)
    assert isinstance(outcomes[0], RuntimeError) and isinstance(outcomes[1], asyncio.CancelledError)
    assert len(attempts) == 1 + ATTEMPTS, f"{len(attempts) - 1} attempts after the failure, not {ATTEMPTS}"
    assert all(isinstance(o, KeyFailed) for o in outcomes[2:]), outcomes


class BackoffFails:
    async def __call__(self, seconds):
        raise RuntimeError("the backoff failed")


async def test_b1_a_backoff_that_fails_spends_its_attempt_so_the_retries_still_end():
    lock = ClaimLock(sleep=BackoffFails(), random_fraction=lambda: 0.0)

    async def op(waited):
        await asyncio.sleep(0.001)
        raise RuntimeError("down")

    outcomes = await asyncio.wait_for(
        asyncio.gather(*(lock.run("k", op) for _ in range(3)), return_exceptions=True), 5)
    assert isinstance(outcomes[0], RuntimeError) and all(isinstance(o, KeyFailed) for o in outcomes[1:]), outcomes


# =================================================================================================
# W2: an exception delivered while waiting on a thread is raised, after the cleanup
# =================================================================================================


async def _drive(coro, throw: BaseException, when):
    """Step `coro` by hand, as a task would, throwing `throw` into it at the first await
    for which `when()` holds: asyncio itself delivers no such exception to a task."""
    thrown, value = False, None
    while True:
        try:
            if not thrown and when():
                thrown = True
                yielded = coro.throw(throw)
            else:
                yielded = coro.send(value)
        except StopIteration as stop:
            return stop.value
        yielded._asyncio_future_blocking = False
        if not thrown:
            await asyncio.sleep(0.05)
            if not yielded.done():
                value = None
                continue
        await asyncio.wait({yielded})
        value = None


async def test_w2_an_interrupt_delivered_while_the_lock_is_taken_is_raised_once_the_lock_is_released():
    from agentsdk import postgres

    pool = postgres._pool(DSN)
    key = lock_key("fetch", "SYN-m25", str(uuid.uuid4()))
    taken, go_on = threading.Event(), threading.Event()

    def slow_try(pool, key):
        got = claimlock._try_lock(pool, key)
        taken.set()
        go_on.wait(10)  # the lock is held, and the caller has not heard so
        return got

    before_locks, before_use = await asyncio.to_thread(held_locks), await asyncio.to_thread(in_use)
    coro = claimlock._completed(asyncio.to_thread(slow_try, pool, key), lambda c: claimlock._release(pool, c, key))

    def when():
        if taken.is_set():
            threading.Timer(0.2, go_on.set).start()
            return True
        return False

    raised = False
    try:
        got = await _drive(coro, KeyboardInterrupt(), when)
    except KeyboardInterrupt:
        raised = True
    else:
        if got is not None:  # swallowed: the caller holds a lock nobody will release
            await asyncio.to_thread(claimlock._release, pool, got, key)
    assert raised, "the interrupt was swallowed"
    assert go_on.is_set(), "raised before the thread's work finished"
    assert await asyncio.to_thread(held_locks) == before_locks, "the lock was left held"
    assert await asyncio.to_thread(in_use) == before_use, "the connection was not returned"


async def test_w2_the_threads_own_failure_is_still_its_own_outcome():
    def fails():
        raise ValueError("the thread's own")

    with pytest.raises(ValueError, match="own"):
        await claimlock._completed(asyncio.to_thread(fails), None)


# =================================================================================================
# K1: every KeyFailed names the failure that spent the key
# =================================================================================================


async def test_k1_every_key_failed_after_the_retries_carries_the_last_failure():
    lock = ClaimLock(sleep=NoDelay(), random_fraction=lambda: 0.0)
    n = []

    async def op(waited):
        n.append(1)
        await asyncio.sleep(0.001)
        raise RuntimeError(f"failure {len(n)}")

    outcomes = await asyncio.gather(*(lock.run("k", op) for _ in range(4)), return_exceptions=True)
    last = f"failure {1 + ATTEMPTS}"
    for raised in outcomes[1:]:
        assert isinstance(raised, KeyFailed), outcomes
        assert isinstance(raised.__cause__, RuntimeError) and str(raised.__cause__) == last, raised.__cause__


async def test_k1_a_key_failed_by_a_waiters_own_failure_carries_that_failure():
    lock = ClaimLock(sleep=NoDelay(), random_fraction=lambda: 0.0)
    # The holder fails; the retrier fails once and then succeeds (two attempts); the next
    # waiter's own failure spends the last; the call after it finds the key failed.
    script = iter(["fail", "fail", "ok", "own", "never"])

    async def op(waited):
        step = next(script)
        await asyncio.sleep(0.001)
        if step != "ok":
            raise RuntimeError(step)
        return "ok"

    outcomes = await asyncio.gather(*(lock.run("k", op) for _ in range(4)), return_exceptions=True)
    assert outcomes[1] == "ok" and str(outcomes[2]) == "own", outcomes
    assert isinstance(outcomes[3], KeyFailed), outcomes
    assert outcomes[3].__cause__ is outcomes[2], outcomes[3].__cause__


async def test_k1_a_key_failed_by_a_cancelled_last_attempt_carries_the_cancellation():
    lock = ClaimLock(sleep=NoDelay(), random_fraction=lambda: 0.0)
    started, n = asyncio.Event(), []

    async def op(waited):
        n.append(1)
        if len(n) == 1 + ATTEMPTS:  # the last attempt: hangs until cancelled
            started.set()
            await asyncio.Event().wait()
        await asyncio.sleep(0.001)
        raise RuntimeError("down")

    calls = [asyncio.create_task(lock.run("k", op)) for _ in range(3)]
    await started.wait()
    calls[1].cancel()
    outcomes = await asyncio.gather(*calls, return_exceptions=True)
    assert isinstance(outcomes[1], asyncio.CancelledError)
    assert isinstance(outcomes[2], KeyFailed), outcomes
    assert isinstance(outcomes[2].__cause__, asyncio.CancelledError), outcomes[2].__cause__


# =================================================================================================
# P1, P2: no two names collide by a delimiter
# =================================================================================================


def test_p1_two_ledgers_whose_lock_names_would_join_alike_take_different_locks():
    ledger = str(uuid.uuid4())
    with psycopg.connect(DSN) as conn:
        with conn.transaction():
            one = PostgresLedgerStore._lock(conn, "SYN-m25|a", "p", ledger, "k")
        with conn.transaction():
            two = PostgresLedgerStore._lock(conn, "SYN-m25", "a|p", ledger, "k")
    assert one != two


def _hash(agent_spec_id, instructions, tool_profile):
    return build_manifest(sdk_version="x", agent_spec_id=agent_spec_id, instructions=instructions,
                          tool_profile=tool_profile, tool_spec_hashes=[], model_id=None)["agent_spec_hash"]


def test_p2_two_specs_whose_parts_would_join_alike_hash_differently_and_say_their_encoding():
    pairs = [
        (("a|b", "c", ()), ("a", "b|c", ())),
        (("a", "b|", ("t",)), ("a", "b", ("|t",))),
        (("a", "b", ("x,y",)), ("a", "b", ("x", "y"))),
    ]
    for first, second in pairs:
        one, two = _hash(*first), _hash(*second)
        assert one.startswith("v2:") and two.startswith("v2:"), (one, two)
        assert one != two, (first, second)
    # Still a function of what changes behaviour, and of nothing else.
    assert _hash("a", "b", ("x", "y")) == _hash("a", "b", ("y", "x"))


# =================================================================================================
# C5: a waited fetch answered with a non-2xx records its wait
# =================================================================================================


class SlowNotFound:
    def __init__(self):
        self.requests = 0

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        self.requests += 1
        await asyncio.sleep(0.2)
        writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n"
                     b"Connection: close\r\n\r\nno")
        await writer.drain()
        writer.close()


class Fetches:
    async def send(self, request):
        if any(m.tool_results for m in request.messages):
            return ModelResponse(message=Message(role=Role.ASSISTANT, content="done"),
                                 stop_reason=StopReason.END_TURN, usage=Usage(1, 1, 2))
        return ModelResponse(message=Message(role=Role.ASSISTANT, tool_calls=(
            ToolCall(id="c", name="fetch_url", arguments={"url": "http://rivers.test/gone"}),)),
            stop_reason=StopReason.TOOL_CALLS, usage=Usage(1, 1, 2))


async def test_c5_a_waited_fetch_answered_404_records_its_wait_and_an_unwaited_one_nothing_new():
    async with SlowNotFound() as pages:
        async def resolve(host, port):
            return ["1.1.1.1"]

        runner = Runner({"m": Fetches()}, tools=[fetch_tool(
            ["rivers.test"], _resolve=resolve, _connect=lambda ip, port: ("127.0.0.1", pages.port))])
        agent = AgentSpec(id="a", instructions="i", tool_profile=("fetch_url",))
        results = await asyncio.gather(*(runner.run(agent, "go", RunConfig(
            tenant_id="SYN-m25", project_id="p", model_override="m:x")) for _ in range(2)))
        assert pages.requests == 2
    uncached = [e.payload for r in results for e in r.events if e.event_type is EventType.SOURCE_UNCACHED]
    assert len(uncached) == 1, uncached  # the waiter's; the holder waited on no one
    payload = uncached[0]
    assert payload["status"] == 404 and payload["canonical_uri"] == "http://rivers.test/gone", payload
    assert payload["wait"]["took_result"] is False and payload["wait"]["seconds"] > 0, payload
    assert not any(e.event_type in (EventType.SOURCE_FETCHED, EventType.SOURCE_SERVED)
                   for r in results for e in r.events)


# =================================================================================================
# Example 21: the taint check is not vacuous for the correction
# =================================================================================================


def _example_21():
    spec = importlib.util.spec_from_file_location("example_21", REPO / "scripts" / "21_contention.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_example_21s_correction_cites_a_page_and_its_taint_check_holds():
    checks, _, _ = await _example_21().offline("memory", seed=25, quiet=True)
    results = dict(checks)
    assert results.get("every claim, the correction included, is at least as tainted as the pages it cites") is True, results


def test_example_21s_taint_check_fails_against_a_clean_provenance():
    module = _example_21()
    page = SimpleNamespace(provenance=ContentProvenance(
        origin=Origin.EXTERNAL_TOOL, instruction_authority=InstructionAuthority.DATA_ONLY,
        trust_zone=TrustZone.UNTRUSTED, taint_flags=frozenset({TaintFlag.EXTERNAL_CONTENT})))
    tainted = SimpleNamespace(supporting=("v",), contradicting=(), provenance=ContentProvenance.from_model(page.provenance))
    clean = SimpleNamespace(supporting=("v",), contradicting=(), provenance=ContentProvenance.internal_tool())
    assert module.taint_holds([tainted], lambda vid: page) is True
    assert module.taint_holds([tainted, clean], lambda vid: page) is False
    # Untrusted but missing the page's flags: the flags are checked, not only the zone.
    unflagged = SimpleNamespace(supporting=("v",), contradicting=(), provenance=ContentProvenance(
        origin=Origin.MODEL, instruction_authority=InstructionAuthority.ADVISORY, trust_zone=TrustZone.UNTRUSTED))
    assert module.taint_holds([unflagged], lambda vid: page) is False
    assert module.taint_holds([], lambda vid: page) is False


# =================================================================================================
# Declared: W1, C6 (B2 is asserted by test_claim_lock_followup.py's test_c4)
# =================================================================================================


async def test_w1_declared_a_cancelled_call_waits_for_its_thread_however_long_it_runs():
    release = threading.Event()

    def slow():
        release.wait(10)
        return None

    task = asyncio.create_task(claimlock._completed(asyncio.to_thread(slow), None))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.sleep(0.5)
    assert not task.done(), "the call returned while its thread still ran"
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_c6_declared_a_holder_keeps_its_pooled_connection_while_its_operation_runs():
    before = await asyncio.to_thread(in_use)
    seen = []

    async def op(waited):
        seen.append(await asyncio.to_thread(in_use, False))
        return "done"

    assert await ClaimLock(DSN).run(lock_key("fetch", "SYN-m25", str(uuid.uuid4())), op) == "done"
    assert seen == [before + 1], (before, seen)
    assert await asyncio.to_thread(in_use) == before
