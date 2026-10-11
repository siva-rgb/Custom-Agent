"""M25a gate: the W2 window (FR-109, AC-82).

M25's review (KNOWLEDGE-be223191) found that the claim-lock's wait on a worker thread
swallowed a cancellation delivered in the loop iteration where the thread's future had
just completed: `_finished` read "the future is done" as "the exception is the future's
own". A timeout whose deadline met the work's end then never fired, the executor's tool
timeout included. Each test here makes that window deterministic by handing `_completed`
a future the test completes itself, in the same iteration as the interruption.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import psycopg
import pytest

from agentsdk import claimlock
from agentsdk.claimlock import lock_key
from agentsdk.config import normalise_database_url

DSN = normalise_database_url(os.environ.get("DATABASE_URL"))


def held_locks() -> int:
    with psycopg.connect(DSN) as conn:
        return conn.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid = 4").fetchone()[0]


def in_use() -> int:
    from agentsdk import postgres

    pool = postgres._pool(DSN)
    pool.wait(timeout=10)
    stats = pool.get_stats()
    return stats.get("pool_size", 0) - stats.get("pool_available", 0)


class Undo:
    def __init__(self):
        self.seen = []

    def __call__(self, result):
        self.seen.append(result)


async def _waiting(future, undo):
    """A task inside `_completed`, already waiting on `future`."""
    task = asyncio.create_task(claimlock._completed(future, undo))
    for _ in range(3):
        await asyncio.sleep(0)
    assert not task.done()
    return task


# =================================================================================================
# A cancellation in the window
# =================================================================================================


async def test_a_cancellation_delivered_as_the_thread_finishes_is_raised_after_the_undo():
    future, undo = asyncio.get_running_loop().create_future(), Undo()
    task = await _waiting(future, undo)
    future.set_result("THE LOCK")
    task.cancel()  # the same iteration: the shield's own callback has not run yet
    with pytest.raises(asyncio.CancelledError):
        await task
    assert undo.seen == ["THE LOCK"], "the result was not undone"


async def test_a_cancellation_in_the_window_on_the_database_leaves_no_lock_and_no_connection():
    from agentsdk import postgres

    pool = postgres._pool(DSN)
    key = lock_key("fetch", "SYN-m25a", str(uuid.uuid4()))
    before_locks, before_use = await asyncio.to_thread(held_locks), await asyncio.to_thread(in_use)
    conn = await asyncio.to_thread(claimlock._try_lock, pool, key)
    assert conn is not None
    future = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(claimlock._completed(future, lambda c: claimlock._release(pool, c, key)))
    for _ in range(3):
        await asyncio.sleep(0)
    future.set_result(conn)
    task.cancel()
    raised = False
    try:
        got = await task
    except asyncio.CancelledError:
        raised = True
    else:  # swallowed: the caller holds a lock nobody will release
        await asyncio.to_thread(claimlock._release, pool, got, key)
    assert raised, "the cancellation was swallowed"
    assert await asyncio.to_thread(held_locks) == before_locks, "the lock was left held"
    assert await asyncio.to_thread(in_use) == before_use, "the connection was not returned"


# =================================================================================================
# A timeout in the window
# =================================================================================================


async def test_an_asyncio_timeout_whose_deadline_meets_the_threads_end_still_fires():
    loop = asyncio.get_running_loop()
    future, undo = loop.create_future(), Undo()

    async def call():
        async with asyncio.timeout(None) as deadline:
            def both():
                # The deadline is reached, and then the work ends, in one callback: the
                # timeout's cancellation is queued ahead of the shield's own callback.
                deadline.reschedule(loop.time() - 1)
                future.set_result("THE LOCK")

            loop.call_soon(both)
            return await claimlock._completed(future, undo)

    with pytest.raises(TimeoutError):
        await call()
    assert undo.seen == ["THE LOCK"]


async def test_the_executors_wait_for_whose_deadline_meets_the_threads_end_still_times_out():
    loop = asyncio.get_running_loop()
    future, undo = loop.create_future(), Undo()
    real_time = loop.time
    frozen = real_time()
    loop.time = lambda: frozen  # both timers get the same deadline
    try:
        loop.call_at(frozen + 0.05, future.set_result, "THE LOCK")  # first in the heap
        waiting = asyncio.create_task(asyncio.wait_for(claimlock._completed(future, undo), timeout=0.05))
        for _ in range(3):
            await asyncio.sleep(0)
    finally:
        del loop.time
    assert loop.time is not None and not waiting.done()
    with pytest.raises(TimeoutError):
        await waiting
    assert undo.seen == ["THE LOCK"]


# =================================================================================================
# Any other exception in the window, and the thread's own outcomes
# =================================================================================================


async def test_another_exception_delivered_as_the_thread_finishes_is_raised_after_the_undo():
    future, undo = asyncio.get_running_loop().create_future(), Undo()
    coro = claimlock._completed(future, undo)
    yielded = coro.send(None)  # waiting on the shield of `future`
    future.set_result("THE LOCK")  # done; the shield's callback still queued
    yielded._asyncio_future_blocking = False
    raised = None
    try:
        step = coro.throw(KeyboardInterrupt())
        while True:  # the undo's own wait, driven to its end
            step._asyncio_future_blocking = False
            await asyncio.wait({step})
            step = coro.send(None)
    except StopIteration as stop:
        swallowed = stop.value
    except KeyboardInterrupt as exc:
        raised, swallowed = exc, None
    assert raised is not None, f"swallowed, returning {swallowed!r}"
    assert undo.seen == ["THE LOCK"]


async def test_the_threads_own_failure_and_own_cancellation_are_still_its_own():
    loop = asyncio.get_running_loop()
    failed, undo = loop.create_future(), Undo()
    task = await _waiting(failed, undo)
    failed.set_exception(ValueError("the thread's own"))
    with pytest.raises(ValueError, match="own"):
        await task
    cancelled = loop.create_future()
    task = await _waiting(cancelled, undo)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert undo.seen == [], "an undo ran for work that produced nothing"


async def test_a_result_with_nothing_delivered_is_returned_and_not_undone():
    future, undo = asyncio.get_running_loop().create_future(), Undo()
    task = await _waiting(future, undo)
    future.set_result("THE LOCK")
    assert await task == "THE LOCK"
    assert undo.seen == []


async def test_a_cancellation_delivered_as_the_thread_fails_is_the_one_raised():
    future, undo = asyncio.get_running_loop().create_future(), Undo()
    task = await _waiting(future, undo)
    future.set_exception(ValueError("the thread's own"))
    task.cancel()  # delivered in the same iteration: not the thread's outcome
    with pytest.raises(asyncio.CancelledError):
        await task
    assert undo.seen == []


async def test_another_exception_after_a_cancellation_is_the_one_raised():
    future, undo = asyncio.get_running_loop().create_future(), Undo()
    coro = claimlock._completed(future, undo)
    step = coro.send(None)
    step._asyncio_future_blocking = False
    step = coro.throw(asyncio.CancelledError())  # still waiting
    step._asyncio_future_blocking = False
    step = coro.throw(KeyboardInterrupt())  # still waiting
    future.set_result("THE LOCK")
    raised = None
    try:
        while True:
            step._asyncio_future_blocking = False
            await asyncio.wait({step})
            step = coro.send(None)
    except StopIteration as stop:
        pytest.fail(f"swallowed, returning {stop.value!r}")
    except BaseException as exc:  # noqa: BLE001 - the one raised is what is checked
        raised = exc
    assert isinstance(raised, KeyboardInterrupt), raised
    assert undo.seen == ["THE LOCK"]
