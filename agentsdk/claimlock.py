"""Claim-lock: one holder per key, across the tasks of a process and the processes
sharing a database (FR-95, ADR-08, P3-D10, M24).

A key is a resource or a claim within one reach -- a tenant and project, and for SESSION
a top-level run -- so no wait, and no result handed to a waiter, crosses a tenant
(NFR-25). Within a process an asyncio lock per key admits one task at a time. With a
database, the admitted task then takes a session-level advisory lock in a lock space of
its own, polling `pg_try_advisory_lock` so that a waiter never blocks a worker thread,
never holds a connection and stays cancellable; the holder keeps that connection for its
operation, and losing it releases the lock.

Waiters run their own operation after the holder, which then finds the holder's result:
a cached version, or the claim to contribute to. When a holder fails, the next waiter
becomes the holder and retries with full jitter -- before its k-th attempt (k from 0) a
random delay between 0 and 0.2 x 2^k seconds -- for at most 3 attempts; if they all fail,
it and every waiter then queued fail naming the key, and none of them retries.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, TypeVar

__all__ = ["ClaimLock", "KeyFailed", "lock_key"]

T = TypeVar("T")

BASE_DELAY = 0.2
ATTEMPTS = 3
_LOCK_SPACE = 4  # postgres.py uses 1 to 3 for transaction-scoped locks


class KeyFailed(RuntimeError):
    """Every attempt on a key failed; this call was one of those waiting on it."""

    def __init__(self, key: str, cause: BaseException | None = None) -> None:
        self.key = key
        reason = "" if cause is None else f": {type(cause).__name__}: {cause}"
        super().__init__(f"claim-lock key {key!r} failed after {ATTEMPTS} attempts{reason}")


def lock_key(*parts: str) -> str:
    """A claim-lock key from its parts, encoded so that no two different lists of parts
    give the same key whatever characters they hold (M24 review caveat C2)."""
    return json.dumps(parts, ensure_ascii=False, separators=(",", ":"))


class _Key:
    __slots__ = ("lock", "users", "holder_failed", "failed", "budget")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.users = 0
        self.holder_failed = False
        self.failed: KeyFailed | None = None
        # Attempts left after a failure, for as long as any call is on the key: None
        # until something fails (M24 review caveats C3, C4).
        self.budget: int | None = None

    def failed_once(self, key: str, exc: BaseException) -> None:
        """An operation on the key failed outside a retry: the holder's, or a waiter's
        own after the holder succeeded. The first opens the budget; any later one
        draws on it rather than opening another."""
        if self.budget is None:
            self.budget = ATTEMPTS
        else:
            self.budget -= 1
        if self.budget <= 0:
            self.failed = KeyFailed(key, exc)
        else:
            self.holder_failed = True


class ClaimLock:
    """Singleflight per key. `run(key, op)` calls `op(waited)`, where `waited` is the
    seconds this call spent waiting for another holder, or None if it waited for none."""

    def __init__(
        self,
        dsn: str | None = None,
        *,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        random_fraction: Callable[[], float] = random.random,
        poll_seconds: float = 0.02,
    ) -> None:
        self._dsn, self._sleep, self._random, self._poll = dsn, sleep, random_fraction, poll_seconds
        self._keys: dict[str, _Key] = {}
        # How many calls have waited on another holder: what a contention run reports.
        self.waits = 0

    async def run(self, key: str, op: Callable[[float | None], Awaitable[T]]) -> T:
        entry = self._keys.get(key)
        if entry is None:
            entry = self._keys[key] = _Key()
        entry.users += 1
        started = time.monotonic()
        queued = entry.lock.locked()
        try:
            async with entry.lock:
                if entry.failed is not None:
                    raise KeyFailed(key, entry.failed.__cause__) from entry.failed
                async with self._database(key) as polled:
                    waited = time.monotonic() - started if queued or polled else None
                    self.waits += waited is not None
                    if entry.holder_failed:
                        return await self._retry(key, entry, op, waited)
                    try:
                        return await op(waited)
                    except BaseException as exc:
                        entry.failed_once(key, exc)
                        raise
        finally:
            entry.users -= 1
            if entry.users == 0 and self._keys.get(key) is entry:
                del self._keys[key]

    async def _retry(self, key: str, entry: _Key, op: Callable[[float | None], Awaitable[T]],
                     waited: float | None) -> T:
        """This call follows a failure: it is the one waiter that retries, drawing on the
        key's budget. An attempt cut off by cancellation counts, and leaves what is left of
        the budget to the next waiter (M24 review caveat C3)."""
        entry.holder_failed = False
        last: BaseException | None = None
        while entry.budget:
            k = ATTEMPTS - entry.budget
            entry.budget -= 1
            try:
                await self._sleep(self._random() * BASE_DELAY * 2 ** k)
                return await op(waited)
            except Exception as exc:  # noqa: BLE001 - every attempt's failure is the key's
                last = exc
            except BaseException as exc:
                if entry.budget:
                    entry.holder_failed = True
                else:
                    entry.failed = KeyFailed(key, exc)
                raise
        entry.failed = KeyFailed(key, last)
        raise entry.failed from last

    @contextlib.asynccontextmanager
    async def _database(self, key: str) -> AsyncIterator[bool]:
        """The key's advisory lock across processes; yields whether it had to wait."""
        if self._dsn is None:
            yield False
            return
        from . import postgres

        pool = postgres._pool(self._dsn)
        polled = False
        while True:
            conn = await _completed(asyncio.to_thread(_try_lock, pool, key), lambda c: _release(pool, c, key))
            if conn is not None:
                break
            polled = True
            await self._sleep(self._poll)
        try:
            yield polled
        finally:
            await _completed(asyncio.to_thread(_release, pool, conn, key), None)


async def _finished(future: asyncio.Future) -> bool:
    """Wait for `future` to finish, however many times the caller is cancelled meanwhile;
    return whether it was. A thread's work cannot be stopped, so its result must be
    waited for and dealt with (M24 review caveat C1: the first version gave up at the
    second cancellation, and leaked a held lock and a pooled connection)."""
    cancelled = False
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:  # noqa: BLE001 - the future's own outcome, read by the caller
            pass
    return cancelled


async def _completed(work: Awaitable[Any], undo: Callable[[Any], Any] | None) -> Any:
    """`work` run to the end even if the caller is cancelled meanwhile, with `undo`
    applied to what it produced before the cancellation goes on -- also to the end."""
    future = asyncio.ensure_future(work)
    if not await _finished(future):
        return future.result()
    if undo is not None and future.exception() is None and future.result() is not None:
        undone = asyncio.ensure_future(asyncio.to_thread(undo, future.result()))
        await _finished(undone)
    raise asyncio.CancelledError()


def _try_lock(pool: Any, key: str) -> Any:
    """A pooled connection holding the key's lock, or None with the connection returned."""
    conn = pool.getconn()
    try:
        got = conn.execute("SELECT pg_try_advisory_lock(hashtext(%s), %s)", (key, _LOCK_SPACE)).fetchone()[0]
        conn.commit()
    except BaseException:
        pool.putconn(conn)
        raise
    if got:
        return conn
    pool.putconn(conn)
    return None


def _release(pool: Any, conn: Any, key: str) -> None:
    try:
        conn.execute("SELECT pg_advisory_unlock(hashtext(%s), %s)", (key, _LOCK_SPACE))
        conn.commit()
    finally:
        pool.putconn(conn)
