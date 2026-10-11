"""M24a gate: the claim-lock's carried caveats (FR-98, AC-76).

M24's review (KNOWLEDGE-4301be95) found that a second cancellation could leak a held
advisory lock and a pooled connection (C1), that a cancelled attempt or a waiter's own
failure reopened the three-attempt budget (C3, C4), and that keys joined with a delimiter
let two reaches share one (C2). Settled before increment 2 (DECISION-a13245a0).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

import psycopg
import pytest

from agentsdk import AgentSpec, RunConfig, Runner
from agentsdk import claimlock
from agentsdk.builtin_tools import fetch_tool
from agentsdk.claimlock import ATTEMPTS, ClaimLock, KeyFailed, lock_key
from agentsdk.config import normalise_database_url
from agentsdk.events import EventType
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.primitives import Message, Role, ToolCall

DSN = normalise_database_url(os.environ.get("DATABASE_URL"))


def held_locks() -> int:
    """Advisory locks in the claim-lock's space, held by anyone."""
    with psycopg.connect(DSN) as conn:
        return conn.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid = 4").fetchone()[0]


def in_use() -> int:
    """Pooled connections checked out, once the pool has finished opening its own."""
    from agentsdk import postgres

    pool = postgres._pool(DSN)
    pool.wait(timeout=10)
    stats = pool.get_stats()
    return stats.get("pool_size", 0) - stats.get("pool_available", 0)


async def _cancel_repeatedly(task: asyncio.Task, times: int = 3, gap: float = 0.02) -> None:
    for _ in range(times):
        if task.done():
            break
        task.cancel()
        await asyncio.sleep(gap)
    with pytest.raises(asyncio.CancelledError):
        await task


# =================================================================================================
# C1: nothing leaks, however many times a call is cancelled
# =================================================================================================


@pytest.mark.parametrize("window", ["acquiring", "undoing", "holding", "releasing"])
async def test_c1_repeated_cancellation_leaves_no_lock_held_and_every_connection_returned(monkeypatch, window):
    real_try, real_release = claimlock._try_lock, claimlock._release

    def slow_try(pool, key):
        got = real_try(pool, key)
        time.sleep(0.2)  # the window between the lock being taken and the caller hearing so
        return got

    def slow_release(pool, conn, key):
        time.sleep(0.2)
        return real_release(pool, conn, key)

    if window in ("acquiring", "undoing"):
        monkeypatch.setattr(claimlock, "_try_lock", slow_try)
    if window in ("undoing", "releasing"):
        monkeypatch.setattr(claimlock, "_release", slow_release)
    before_locks, before_use = await asyncio.to_thread(held_locks), await asyncio.to_thread(in_use)
    entered = asyncio.Event()

    async def op(waited):
        entered.set()
        if window == "releasing":
            return "done"
        await asyncio.sleep(5)

    lock = ClaimLock(DSN)
    key = lock_key("fetch", "SYN-m24a", str(uuid.uuid4()))
    task = asyncio.create_task(lock.run(key, op))
    if window == "acquiring":
        await asyncio.sleep(0.05)
        await _cancel_repeatedly(task)
    elif window == "undoing":
        # Cancelled while acquiring, and again and again while the lock it got is undone.
        await asyncio.sleep(0.05)
        await _cancel_repeatedly(task, times=15, gap=0.04)
    elif window == "holding":
        await entered.wait()
        await _cancel_repeatedly(task)
    else:
        await entered.wait()
        await asyncio.sleep(0.05)  # now inside the slow release
        await _cancel_repeatedly(task)
    if window != "undoing":
        await asyncio.sleep(0.4)  # any thread the call left behind has finished by now
    # When undoing, checked at once: a cancelled call returns only once its lock is released.
    assert await asyncio.to_thread(held_locks) == before_locks, "an advisory lock was left held"
    assert await asyncio.to_thread(in_use) == before_use, "a pooled connection was not returned"
    # And the key is free for the next call at once.
    assert await asyncio.wait_for(ClaimLock(DSN).run(key, lambda w: asyncio.sleep(0, result="free")), 2) == "free"


# =================================================================================================
# C3, C4: three attempts in all after a failure
# =================================================================================================


class NoDelay:
    async def __call__(self, seconds):
        await asyncio.sleep(0)


async def test_c3_a_cancelled_attempt_counts_and_the_key_still_makes_three_in_all():
    lock = ClaimLock(sleep=NoDelay(), random_fraction=lambda: 0.0)
    attempts, hold = [], asyncio.Event()

    async def op(waited):
        attempts.append(len(attempts))
        if len(attempts) == 1:
            await asyncio.sleep(0.01)  # failing while the others wait on the key
            raise RuntimeError("the holder fails")
        if len(attempts) == 2:
            await hold.wait()  # the retrier's first attempt, which is cancelled
        raise RuntimeError("still down")

    calls = [asyncio.create_task(lock.run("k", op)) for _ in range(5)]
    await asyncio.sleep(0.05)
    calls[1].cancel()
    outcomes = await asyncio.gather(*calls, return_exceptions=True)
    assert isinstance(outcomes[0], RuntimeError) and isinstance(outcomes[1], asyncio.CancelledError)
    assert all(isinstance(o, KeyFailed) for o in outcomes[2:]), outcomes
    assert len(attempts) == 1 + ATTEMPTS, f"{len(attempts) - 1} attempts after the failure"


async def test_c4_a_waiters_own_failure_draws_on_the_same_three():
    lock = ClaimLock(sleep=NoDelay(), random_fraction=lambda: 0.0)
    script = iter(["fail", "ok", "fail", "fail", "never", "never"])
    ran = []

    async def op(waited):
        step = next(script)
        ran.append(step)
        await asyncio.sleep(0.001)
        if step != "ok":
            raise RuntimeError(step)
        return "ok"

    outcomes = await asyncio.gather(*(lock.run("k", op) for _ in range(5)), return_exceptions=True)
    # The holder fails; the retrier's first attempt succeeds; the next waiter's own
    # operation fails, which is the second attempt; the waiter after it retries once
    # more, the third, and fails; the last call fails without running.
    assert ran == ["fail", "ok", "fail", "fail"], ran
    assert outcomes[1] == "ok" and isinstance(outcomes[2], RuntimeError)
    assert isinstance(outcomes[3], KeyFailed) and isinstance(outcomes[4], KeyFailed), outcomes


async def test_the_budget_is_fresh_once_no_call_is_left_on_the_key():
    lock = ClaimLock(sleep=NoDelay(), random_fraction=lambda: 0.0)

    async def fails(waited):
        raise RuntimeError("down")

    with pytest.raises(RuntimeError):
        await lock.run("k", fails)
    assert await lock.run("k", lambda w: asyncio.sleep(0, result="up")) == "up"


# =================================================================================================
# C2: no two reaches share a key
# =================================================================================================


def test_c2_parts_that_would_join_the_same_give_different_keys():
    assert lock_key("fetch", "a|b", "c") != lock_key("fetch", "a", "b|c")
    assert lock_key("claim", 'x","y', "z") != lock_key("claim", "x", "y", "z")
    assert json.loads(lock_key("fetch", "a|b", "c")) == ["fetch", "a|b", "c"]


class Slow:
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
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()


class Fetches:
    async def send(self, request):
        if any(m.tool_results for m in request.messages):
            return ModelResponse(message=Message(role=Role.ASSISTANT, content="done"),
                                 stop_reason=StopReason.END_TURN, usage=Usage(1, 1, 2))
        return ModelResponse(message=Message(role=Role.ASSISTANT, tool_calls=(
            ToolCall(id="c", name="fetch_url", arguments={"url": "http://rivers.test/a"}),)),
            stop_reason=StopReason.TOOL_CALLS, usage=Usage(1, 1, 2))


async def test_c2_two_reaches_whose_names_join_the_same_never_wait_on_each_other():
    async with Slow() as pages:
        async def resolve(host, port):
            return ["1.1.1.1"]

        runner = Runner({"m": Fetches()}, tools=[fetch_tool(
            ["rivers.test"], _resolve=resolve, _connect=lambda ip, port: ("127.0.0.1", pages.port))])
        agent = AgentSpec(id="a", instructions="i", tool_profile=("fetch_url",))
        reaches = [("SYN-m24a|x", "p"), ("SYN-m24a", "x|p")]
        results = await asyncio.gather(*(runner.run(agent, "go", RunConfig(
            tenant_id=t, project_id=p, model_override="m:x")) for t, p in reaches))
        assert pages.requests == 2
    fetched = [e.payload for r in results for e in r.events if e.event_type is EventType.SOURCE_FETCHED]
    assert len(fetched) == 2 and not any("wait" in p for p in fetched), fetched
