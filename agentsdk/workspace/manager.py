"""FR-138 (M38): the WorkspaceManager, the only caller of a Runner's WorkspaceProvider (P5-D2).

Each run with a workspace gets a `RunWorkspace`: its tools reach the provider only through it. It
creates the workspace the first time a tool needs one, never before; keys it by the run's tenant,
project and run, so no run reaches another's; records it, each command and its removal (FR-140);
and measures the workspace after each command and write, refusing every later one past
`max_workspace_bytes` (FR-144).

No lock is held across a provider call (FR-138): concurrent first needs share one creation task.
Every record write goes through the run's `control.store`, which finishes once started whatever
cancellation arrives (FR-50, KNOWLEDGE-e22f787f), and what must happen once begun -- a workspace's
creation, a cancelled command's record, a removal -- runs as a task that a cancellation of its
caller waits for rather than abandons (M38 pre-review D2 to D4).

Since M41 (FR-151, FR-152) a workspace is a `_Space` that one run's `RunWorkspace` binds, or, under
`scope="plan"`, every child of one top-level run whose spec is the same. A `"run"` workspace is
removed when its run ends, a `"plan"` one when its top-level run ends, and a kept one when it is
released or its Runner closes. The manager holds at most `max_concurrent` of them, and before its
first one it reaps what a dead Runner left (DECISION-825b81a6).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import hashlib
import json
import posixpath
import threading
import time
import uuid
import weakref
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

from ..errors import BudgetExceeded, WorkspaceRefused, WorkspaceUnavailable
from ..events import EventType
from ..handle import StoredEmit, settle, through
from . import (MEASURE_SECONDS, SETUP_SECONDS, SNAPSHOT_SECONDS, CommandOutcome, SnapshotRef, WorkspaceHandle,
               WorkspaceProvider, WorkspaceSpec, charged)
from .owner import is_dead, this_owner
from .records import CommandRecord, ConnectionRecord, SnapshotRecord, WorkspaceRecord, now
from .tools import files_tar

Emit = Callable[[EventType, dict[str, Any]], Awaitable[Any]]
Call = Callable[..., Awaitable[Any]]
# How long removing a workspace, or killing a cancelled command, may take before the run goes on;
# the removal is still recorded (M41 reaps what a provider left).
_REMOVE_SECONDS = 30.0
_KILL_SECONDS = 10.0
_MEASURE_SECONDS = MEASURE_SECONDS


class _Slots:
    """FR-152: at most `limit` workspaces held at once, whichever loop each run is on. A waiter
    that is cancelled after its slot was handed to it gives that slot on. Each wait names the
    workspace it is for: once one of a plan's children begins creating it, its siblings still
    waiting are released without a slot, so one workspace never takes two (DECISION-b1498111).

    NFR-39 (M45 round 3, DECISION-8e21d1ff): a sandboxed server's engine shares these slots, so
    each waiter is held weakly -- by its waiting task alone. A run abandoned while it waits leaves
    nothing here that reaches it; its waiter, gone, is passed over."""

    class _Waiter:
        def __init__(self, loop: asyncio.AbstractEventLoop, space: object) -> None:
            self.loop, self.future, self.space, self.granted = loop, loop.create_future(), space, False

    def __init__(self, limit: int) -> None:
        self.limit, self.held = limit, 0
        self._lock = threading.Lock()
        self._waiters: deque[weakref.ref[_Slots._Waiter]] = deque()

    def _waiting(self) -> list[_Slots._Waiter]:
        """Under the lock: the waiters still waiting, in order; those gone are dropped, and those whose
        loop was closed under them, which no grant can reach (M45 round 3 pre-review)."""
        alive = [waiter for waiter in (ref() for ref in self._waiters)
                 if waiter is not None and not waiter.loop.is_closed()]
        if len(alive) != len(self._waiters):
            self._waiters = deque(weakref.ref(waiter) for waiter in alive)
        return alive

    async def take(self, waiting: Callable[[int], Awaitable[None]] | None, space: object = None,
                   begun: Callable[[], bool] | None = None) -> bool:
        """True with a slot; False when released because a sibling began creating `space`."""
        with self._lock:
            if self.held < self.limit:
                self.held += 1
                return True
            if begun is not None and begun():
                # A sibling on another loop began it, and released its waiters, between the
                # caller's check and this lock: a wait queued now would never be released
                # (M41 review caveat, DECISION-b7423cae).
                return False
            waiter = _Slots._Waiter(asyncio.get_running_loop(), space)
            self._waiters.append(weakref.ref(waiter))
            held = self.held
        # Told from its own task: a store write awaited here would absorb this wait's cancellation
        # (handle.py), and a wait for a workspace is cancellable (FR-152).
        told = asyncio.ensure_future(waiting(held)) if waiting is not None else None
        try:
            await waiter.future
            if told is not None:
                await through(told)  # the wait is told before anything is created
        except asyncio.CancelledError:
            # Cancelled waiting, or after its grant while the wait was told (M41 pre-review).
            with self._lock:
                granted = waiter.granted
                if not granted and waiter in self._waiting():
                    self._waiters = deque(ref for ref in self._waiters if ref() is not waiter)
            if granted:
                self.give()
            if told is not None:
                await through(told)  # the wait is told before the run's ending is
            raise
        return waiter.granted

    def release(self, space: object) -> None:
        """Every wait for `space` ends without a slot: a sibling is creating it."""
        with self._lock:
            released = [w for w in self._waiting() if w.space is space]
            self._waiters = deque(ref for ref in self._waiters if ref() not in released)
            for waiter in released:
                _scheduled(waiter)

    def give(self) -> None:
        with self._lock:
            for waiter in self._waiting():
                self._waiters.popleft()
                waiter.granted = True
                if _scheduled(waiter):
                    return
                waiter.granted = False
            self.held -= 1


def _scheduled(waiter: _Slots._Waiter) -> bool:
    """Its grant handed to its loop; False when that loop closed, on another thread, after the
    check under the lock -- the slot then goes to the next waiter or back (M45 round 4 NB1)."""
    try:
        waiter.loop.call_soon_threadsafe(_grant, waiter.future)
    except RuntimeError:
        return False
    return True


def _arrival(manager: WorkspaceManager, space: _Space, handle: WorkspaceHandle) -> Callable[[Any], None]:
    """FR-162: a proxy's watcher, as its provider keeps it. NFR-39 (M45 round 3, DECISION-8e21d1ff):
    a sandboxed server's engine shares that provider, so the watcher holds its manager and its
    workspace weakly -- each is held by what owns it -- and a report for either, gone, is dropped."""
    managers, spaces = weakref.ref(manager), weakref.ref(space)

    def arrived(report: Any) -> None:
        manager, space = managers(), spaces()
        if manager is not None and space is not None:
            manager.arrived(space, handle, report)

    return arrived


def _grant(future: asyncio.Future[None]) -> None:
    if not future.done():
        future.set_result(None)


class _Space:
    """One workspace: a run's own, or a plan's, shared by every child bound to it (FR-152)."""

    def __init__(self, spec: WorkspaceSpec, tenant_id: str, project_id: str, run_id: str) -> None:
        self.spec, self.tenant_id, self.project_id = spec, tenant_id, project_id
        self.run_id = run_id  # the run it is recorded under: its run, or its plan's top-level run
        self.creating: asyncio.Future[WorkspaceHandle] | None = None
        self.ending: asyncio.Future[None] | None = None
        self.handle: WorkspaceHandle | None = None
        self.removed = False
        self.slot = False  # whether it holds one of its manager's slots
        self.kept = False  # its run or plan ended and `keep` held it (FR-151)
        self.full: int | None = None  # the size measured past the bound, once it was
        self.unread: str | None = None  # why the size could not be read, once it could not
        self.stale = False  # a command or write left the size unread; the next use reads it first
        self.last_command: str | None = None  # the last command that ended in it (FR-150)
        # FR-162 (M44): with egress, its proxy's reports, recorded in order by one task; each command
        # with the run that ran it, and the binding that created it for a connection of no command.
        self.reports: asyncio.Queue[Any] | None = None
        self.recorder: asyncio.Task[None] | None = None
        self.commands: dict[str, _Binding] = {}
        self.owner: _Binding | None = None
        self.limit_row: str | None = None
        self.proxy_ended = False  # its proxy died: no later command starts (F2, DECISION-26009e04)
        # Round 1 (DECISION-a7a1b37b): F1, the recorder told to end after its current write; F3, an
        # event being written, which a run's end waits for.
        self.stop_booking = False
        self.booking = asyncio.Lock()
        self.limit_counted = 0  # what its limit row counts so far (DECISION-e510a5e3)
        # Rounds 3 and 4 (DECISION-f7114b8b): the connections its recorder could not record, and the
        # refusals at the limit reported so far, counted on its one `unrecorded` row; and whether its
        # recorder has ended, after which a report is counted by a pass of its own.
        self.unbooked = 0
        self.limit_seen = 0
        # Round 5 N6 (DECISION-7a3a2ae2): its id fixed before its first write; each write tries an
        # update on it before an insert.
        self.unrecorded_row = str(uuid.uuid4())
        self.unrecorded_counted = 0
        self.counting = asyncio.Lock()
        self.booked = False
        self.late: asyncio.Task[None] | None = None
        # FR-168 (M46, DECISION-bafa7be8): the commands and writes under way in it, and a snapshot
        # being taken of it, whichever loops they are on; a snapshot is refused while one is under
        # way, and one asked while a snapshot is taken waits for it.
        self.use = threading.Lock()
        self.busy = 0
        self.copying: concurrent.futures.Future[None] | None = None


class _Binding:
    """FR-162: the run a connection is recorded under, and its gate, shut once it has ended (F3)."""

    def __init__(self, run_id: str, emit: Emit, call: Call) -> None:
        self.run_id, self.emit, self.call = run_id, emit, call
        self.gate = _Gate()


class _Gate:
    """F3 at the store (round 5 B1 and N1, DECISION-5c68ddfb): a run's connection events and its end,
    decided under one thread lock. A write enters on its worker thread just before it begins; once
    the gate is shut none does, and the run's end waits only for one already inside."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._shut = False
        self._inside = 0

    def enter(self) -> bool:
        with self._lock:
            if self._shut:
                return False
            self._inside += 1
            return True

    def leave(self) -> None:
        with self._lock:
            self._inside -= 1

    def busy(self) -> bool:
        """A write is inside."""
        with self._lock:
            return self._inside > 0

    def shut(self) -> None:
        """Lets no write in from now on; one inside finishes."""
        with self._lock:
            self._shut = True


async def _gated(binding: _Binding, kind: EventType, payload: dict[str, Any]) -> None:
    """A connection's event, written only while its run's gate is open: on the worker thread, just
    before the write, when the Runner's emitter writes there; else here, just before it is called."""
    if isinstance(binding.emit, StoredEmit):
        await binding.emit(kind, payload, binding.gate)
        return
    if not binding.gate.enter():
        return
    try:
        await binding.emit(kind, payload)
    finally:
        binding.gate.leave()


# Queued behind a proxy's reports (DECISION-a7a1b37b): its recorder's end (F1), and a finished
# command, forgotten once every report naming it is booked (F7).
_STOP = object()


async def _no_event(kind: EventType, payload: dict[str, Any]) -> None:
    return None


async def _stored(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """A store write with no run to write it through (FR-165): on a worker thread (FR-20)."""
    return await asyncio.to_thread(function, *args, **kwargs)


class _Forget:
    def __init__(self, command_id: str) -> None:
        self.command_id = command_id


class _Barrier:
    """Reached once every report queued before it is booked: a run's end waits for it (F3)."""

    def __init__(self) -> None:
        self.reached = asyncio.get_running_loop().create_future()


def _not_ready() -> WorkspaceUnavailable:
    return WorkspaceUnavailable(f"the workspace was not ready within its setup bound of {SETUP_SECONDS} s")


def _under_way(creating: asyncio.Future[Any] | None) -> bool:
    """A creation that is running or succeeded; one that failed is begun again."""
    return creating is not None and not (creating.done() and (creating.cancelled() or creating.exception() is not None))


def _shared(name: str) -> property:
    return property(lambda self: getattr(self._space, name),
                    lambda self, value: setattr(self._space, name, value))


class RunWorkspace:
    """One run's binding to its workspace, created at its first need."""

    handle = _shared("handle")
    removed = _shared("removed")
    _creating = _shared("creating")
    _ending = _shared("ending")
    _full = _shared("full")
    _unread = _shared("unread")
    _stale = _shared("stale")

    def __init__(self, manager: WorkspaceManager, spec: WorkspaceSpec, tenant_id: str, project_id: str,
                 run_id: str, emit: Emit, call: Call, space: _Space | None = None, account: Any = None,
                 stopping: Callable[[], bool] | None = None) -> None:
        self._manager, self.spec = manager, spec
        # Whether the run has been asked to stop: asked after a command's start is written, as the
        # loop's checkpoint is (M54 pre-review F1). None never stops.
        self._stopping = stopping
        # FR-157: the run's own account for its commands' compute, asked before each command and
        # charged after it; None charges nothing. Never shared with another run of a plan workspace.
        self._account = account
        self.tenant_id, self.project_id, self.run_id = tenant_id, project_id, run_id
        self._emit, self._call = emit, call
        self._binding = _Binding(run_id, emit, call)
        self._space = space if space is not None else _Space(spec, tenant_id, project_id, run_id)
        self.detached = False  # its run ended; a plan's workspace lives on without it

    @property
    def workspace_id(self) -> str | None:
        return None if self.handle is None else self.handle.workspace_id

    async def _record(self, method: str, *args: Any) -> Any:
        return await self._call(getattr(self._manager.store(), method), *args)

    async def ensure(self) -> WorkspaceHandle:
        space = self._space
        if self.removed or self.detached:
            raise WorkspaceUnavailable("this run's workspace was removed")
        if self.handle is not None:
            return self.handle
        # The setup has a bound of its own, inside the tools' deadline, so a command that starts
        # ends by its own timeout and never by the executor's (DECISION-e799700b).
        deadline = time.monotonic() + SETUP_SECONDS
        try:
            async with asyncio.timeout(SETUP_SECONDS):
                if not _under_way(space.creating):
                    # Cancellable, both: what dead Runners left is reaped, then a slot is waited
                    # for (FR-151, FR-152). Only the creation, once begun, is waited out by a
                    # cancellation.
                    self._manager.refuse_if_closed()
                    await self._manager.reap()
                # Asked again after the reaping: a sibling may have begun it meanwhile, and
                # released its waiters already. No await stands between this and joining the
                # queue (M41 pre-review).
                if not _under_way(space.creating) and self.handle is None and not self.removed:
                    if not await self._manager.slots.take(self._waiting, space, lambda: _under_way(space.creating)):
                        pass  # released: a sibling began creating it, which is awaited below
                    elif self.removed or self.handle is not None or _under_way(space.creating):
                        self._manager.slots.give()  # a sibling began it meanwhile
                    else:
                        space.slot = True
                        self._manager.spaces.add(space)  # aclose() finishes, then removes, a creation under way
                        space.creating = asyncio.ensure_future(self._create())
                        self._manager.slots.release(space)
        except TimeoutError:
            raise _not_ready() from None
        if self.removed or self.detached or space.creating is None:
            raise WorkspaceUnavailable("this run's workspace was removed")
        if self.handle is not None:
            return self.handle
        creating = space.creating
        try:
            await asyncio.wait({creating}, timeout=max(0.0, deadline - time.monotonic()))
        except asyncio.CancelledError:
            # The run was cancelled: the creation is waited out first, as ever (FR-148).
            with contextlib.suppress(BaseException):
                await through(creating)
            raise
        if not creating.done():
            # Left to finish: it is the space's, which the run's end or aclose() removes.
            raise _not_ready()
        return await through(creating)

    async def _waiting(self, held: int) -> None:
        with contextlib.suppress(Exception):
            await self._emit(EventType.WORKSPACE_WAITING, {"limit": self._manager.slots.limit, "held": held})

    async def _create(self) -> WorkspaceHandle:
        try:
            return await self._creation()
        except BaseException:
            self._manager.spaces.discard(self._space)
            if self._space.slot:
                self._space.slot = False
                self._manager.slots.give()
            raise

    async def _creation(self) -> WorkspaceHandle:
        space = self._space
        self._manager.refuse_if_closed()
        workspace_id = str(uuid.uuid4())
        labels = {
            "agentsdk.tenant": self.tenant_id, "agentsdk.project": self.project_id, "agentsdk.run": space.run_id,
            "agentsdk.workspace": workspace_id, "agentsdk.owner": self._manager.owner,
            "agentsdk.runner": self._manager.runner_id,
        }
        provider = self._manager.provider
        if self.spec.from_snapshot is not None:
            await self._manager.usable_snapshot(self.spec.from_snapshot, self.tenant_id, self.project_id)
        try:
            handle = await provider.create(self.spec, labels)
        except WorkspaceUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - a provider's failure, named by class only
            raise WorkspaceUnavailable(f"the provider could not create a workspace: {type(exc).__name__}") from None
        if self.spec.from_snapshot is not None:
            # FR-169: measured as it was copied, before it is recorded or runs anything.
            await self._manager.within_bound(handle, self.spec)
        if self.spec.files:
            # FR-175: through the checked transfer, bounded as setup is, before it is recorded or
            # runs anything; a workspace whose files did not land is removed, never used.
            try:
                await asyncio.wait_for(provider.import_(handle, files_tar(self.spec.files)), SETUP_SECONDS)
            except Exception as exc:  # noqa: BLE001 - a provider's failure, named by class only
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(provider.remove(handle), _REMOVE_SECONDS)
                raise WorkspaceUnavailable(f"the workspace's files could not be written: {type(exc).__name__}") from None
        try:
            await self._record("record_workspace", WorkspaceRecord(
                workspace_id=handle.workspace_id, tenant_id=self.tenant_id, project_id=self.project_id,
                run_id=space.run_id, scope=self.spec.scope, provider=provider.name, image=self.spec.image,
                limits=self.spec.record(), created_at=now()))
        except Exception as exc:  # noqa: BLE001 - no workspace runs anything unrecorded (pre-review D4)
            try:
                await asyncio.wait_for(provider.remove(handle), _REMOVE_SECONDS)
            except Exception:  # noqa: BLE001
                pass
            raise WorkspaceUnavailable(f"the workspace could not be recorded: {type(exc).__name__}") from None
        self.handle = handle
        watch = getattr(provider, "watch_egress", None)
        if self.spec.egress and watch is not None:
            # FR-162: the proxy's reports, from its own output, recorded by the manager in order.
            if space.owner is None:  # a plan's is its top-level run's, set with the space
                space.owner = self._binding
            space.reports = asyncio.Queue()
            space.recorder = asyncio.ensure_future(self._manager.book(space, handle))
            watch(handle, _arrival(self._manager, space, handle))
        await self._emit(EventType.WORKSPACE_CREATED, {
            "workspace_id": handle.workspace_id, "provider": provider.name, "image": self.spec.image,
            "scope": self.spec.scope})
        return handle

    async def _refuse_if_full(self, handle: WorkspaceHandle, tool: str) -> None:
        if self._stale:
            # DECISION-fe7243f1: the reading a cancelled command skipped, taken before anything
            # else runs here, and failing closed as any reading does. The mark goes only once a
            # reading has ended: a cancellation during it leaves the workspace stale (M40 round 1).
            await self._measure(handle)
            self._stale = False
        if self._full is None and self._unread is None:
            return
        await self._emit(EventType.WORKSPACE_REFUSED, {
            "workspace_id": self.workspace_id, "tool": tool, "bound": "max_workspace_bytes",
            "size": self._full, "limit": self.spec.max_workspace_bytes, "unread": self._unread})
        if self._full is None:
            raise WorkspaceRefused(f"the workspace's size could not be read ({self._unread}), so it is held past "
                                   f"max_workspace_bytes ({self.spec.max_workspace_bytes} B); it refuses every "
                                   f"later command and write")
        raise WorkspaceRefused(f"the workspace holds {self._full} B, past max_workspace_bytes "
                               f"({self.spec.max_workspace_bytes} B); it refuses every later command and write")

    async def _measure(self, handle: WorkspaceHandle) -> None:
        """FR-144: a size that cannot be read is held past the bound, so a workspace the bound
        cannot see refuses as a full one does (fail closed, DECISION-504c7f04)."""
        try:
            size = await asyncio.wait_for(self._manager.provider.size(handle), _MEASURE_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - named by class only
            self._unread = type(exc).__name__
            return
        if size > self.spec.max_workspace_bytes:
            self._full = size

    async def run(self, argv: list[str], *, stdin: bytes | None, timeout: float,
                  tool_call_id: str | None) -> CommandOutcome:
        # A copy under way is waited for within the command's own timeout, and what is left of it is
        # the command's, so the executor's deadline still covers both (DECISION-f15a2771).
        async with self._manager.using(self._space, timeout) as waited:
            return await self._run(argv, stdin=stdin, timeout=timeout - waited, tool_call_id=tool_call_id)

    async def _run(self, argv: list[str], *, stdin: bytes | None, timeout: float,
                   tool_call_id: str | None) -> CommandOutcome:
        # Asked first, as the loop asks before each model call: a run whose budget is spent starts
        # no command, and creates no workspace for one (DECISION-91ec4213).
        if self._account is not None and not self._account.may_run():
            raise BudgetExceeded("this run's budget is spent: the command did not start")
        if self._space.proxy_ended:
            # F2: egress never stops silently; the workspace fails closed (DECISION-26009e04).
            raise WorkspaceUnavailable("this workspace's egress proxy ended: it runs no further command")
        handle = await self.ensure()
        await self._refuse_if_full(handle, "run_command")
        command_id = str(uuid.uuid4())
        # FR-162: a connection naming this command is this run's (DECISION-88dea779).
        self._space.commands[command_id] = self._binding
        await self._emit(EventType.WORKSPACE_COMMAND_STARTED, {
            "command_id": command_id, "workspace_id": handle.workspace_id, "tool_call_id": tool_call_id,
            "argv": list(argv)})
        started_at, started = now(), time.monotonic()
        if self._stopping is not None and self._stopping():
            # M54 pre-review F1: a cancel that arrived while the start was written was absorbed by the
            # store call (FR-50), so it is honoured here: the command never begins, and is recorded
            # cancelled before the cancellation goes on (FR-148).
            if self._space.reports is not None:
                self._space.reports.put_nowait(_Forget(command_id))
            never = CommandOutcome(exit_code=None, stdout=b"", stderr=b"", stdout_cut=False, stderr_cut=False,
                                   stdout_bytes=0, stderr_bytes=0, duration=0.0, killed="cancelled", cpu_seconds=0.0)
            with contextlib.suppress(Exception):  # the cancellation, not its record, decides how the run ends
                await through(asyncio.ensure_future(
                    self._finished(handle, command_id, argv, tool_call_id, started_at, started, never)))
            raise asyncio.CancelledError()
        # The command runs as its own task, so a cancellation of this call reaches the kill below
        # first, and not the command's own ending half way (FR-148).
        running = asyncio.ensure_future(self._manager.provider.run(handle, list(argv), timeout=timeout,
                                                                   stdin=stdin, command_id=command_id))
        reports = self._space.reports
        if reports is not None:
            # F7: forgotten behind every report its provider mapped to it, its token retired by
            # the time its provider's call ends (DECISION-a7a1b37b).
            running.add_done_callback(lambda _: reports.put_nowait(_Forget(command_id)))
        try:
            outcome = await asyncio.shield(running)
        except asyncio.CancelledError:
            # Its run was cancelled: the command is killed and recorded so before the cancellation
            # goes on (FR-140, FR-148; pre-review D3).
            await through(asyncio.ensure_future(
                self._cancelled(handle, command_id, running, argv, tool_call_id, started_at, started)))
            raise
        except Exception as exc:
            # The provider failed with the command begun: it is ended and recorded as an error, by
            # class, and what it may have written is read before the next use (M40 caveat 2,
            # DECISION-825b81a6). The provider's own error still reaches the tool.
            self._stale = True
            duration = time.monotonic() - started
            failed = CommandOutcome(exit_code=None, stdout=b"", stderr=b"", stdout_cut=False, stderr_cut=False,
                                    stdout_bytes=0, stderr_bytes=0, duration=duration, killed="error",
                                    cpu_seconds=charged(None, "error", duration, handle.spec.cpus))
            with contextlib.suppress(Exception):  # the provider's failure, not its record, is reported
                await through(asyncio.ensure_future(self._finished(
                    handle, command_id, argv, tool_call_id, started_at, started, failed,
                    kill_error=type(exc).__name__)))
            raise
        try:
            await self._measure(handle)
        except asyncio.CancelledError:
            # The command ended; its run was cancelled while the size was read. The reading is
            # abandoned and left to the next use, never the record (M40 round 1, DECISION-f3e99c07).
            self._stale = True
            with contextlib.suppress(Exception):  # the cancellation decides how the run ends
                await through(asyncio.ensure_future(
                    self._finished(handle, command_id, argv, tool_call_id, started_at, started, outcome)))
            raise
        await through(asyncio.ensure_future(
            self._finished(handle, command_id, argv, tool_call_id, started_at, started, outcome)))
        return outcome

    async def _cancelled(self, handle: WorkspaceHandle, command_id: str, running: asyncio.Future[CommandOutcome],
                         argv: list[str], tool_call_id: str | None, started_at: Any, started: float) -> None:
        """FR-148: kills the command through its provider and waits for it to end, all within
        `_KILL_SECONDS`, then records it cancelled. A kill that fails or does not return is
        recorded as `kill_error`, by class; what it left is reaped (FR-151). The size is not read
        here, so a slow daemon cannot hold the cancellation past its bound: the next command or
        write reads it first (DECISION-fe7243f1)."""
        deadline = time.monotonic() + _KILL_SECONDS
        kill_error: str | None = None
        try:
            await asyncio.wait_for(self._manager.provider.kill(handle, command_id), _KILL_SECONDS)
        except Exception as exc:  # noqa: BLE001 - named by class only
            kill_error = type(exc).__name__
        try:
            await asyncio.wait_for(asyncio.shield(running), max(0.0, deadline - time.monotonic()))
        except BaseException:  # noqa: BLE001 - the command's own ending, whatever it was, is not the run's
            pass
        if not running.done():
            running.cancel()
            running.add_done_callback(lambda task: task.cancelled() or task.exception())
        duration = time.monotonic() - started
        outcome = CommandOutcome(exit_code=None, stdout=b"", stderr=b"", stdout_cut=False, stderr_cut=False,
                                 stdout_bytes=0, stderr_bytes=0, duration=duration, killed="cancelled",
                                 cpu_seconds=charged(None, "cancelled", duration, handle.spec.cpus))
        self._stale = True
        try:
            await self._finished(handle, command_id, argv, tool_call_id, started_at, started, outcome,
                                 kill_error=kill_error)
        except Exception:  # noqa: BLE001 - the cancellation, not its record, decides how the run ends
            pass

    async def _finished(self, handle: WorkspaceHandle, command_id: str, argv: list[str], tool_call_id: str | None,
                        started_at: Any, started: float, outcome: CommandOutcome, *,
                        kill_error: str | None = None) -> None:
        """Records the command, charges it and tells the run, whatever fails first: a failed record
        still leaves the end told (pre-review D4), and the charge is drawn after the record and
        before the result reaches the model (FR-157). The size is read before this, by the caller."""
        failure: Exception | None = None
        cpu_usec = round(outcome.cpu_seconds * 1_000_000)
        try:
            await self._record("record_command", CommandRecord(
                command_id=command_id, workspace_id=handle.workspace_id, tenant_id=self.tenant_id,
                project_id=self.project_id, run_id=self.run_id, tool_call_id=tool_call_id, argv=list(argv),
                exit_code=outcome.exit_code, killed=outcome.killed, started_at=started_at,
                ended_at=max(now(), started_at), stdout_bytes=outcome.stdout_bytes,
                stderr_bytes=outcome.stderr_bytes, stdout_kept=len(outcome.stdout), stderr_kept=len(outcome.stderr),
                limit=outcome.limit, cpu_usec=cpu_usec))
        except Exception as exc:  # noqa: BLE001
            failure = failure or exc
        cost = None
        if self._account is not None:
            with contextlib.suppress(Exception):  # accounting never fails a run (NFR-11)
                cost = self._account.charge(cpu_usec)
        self._space.last_command = command_id
        await self._emit(EventType.WORKSPACE_COMMAND_ENDED, {
            "command_id": command_id, "workspace_id": handle.workspace_id, "tool_call_id": tool_call_id,
            "exit_code": outcome.exit_code, "killed": outcome.killed, "limit": outcome.limit, "kill_error": kill_error,
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "stdout_bytes": outcome.stdout_bytes, "stderr_bytes": outcome.stderr_bytes,
            "stdout_cut": outcome.stdout_cut, "stderr_cut": outcome.stderr_cut,
            "cpu_seconds": cpu_usec / 1_000_000, "cost_usd": None if cost is None else str(cost)})
        if failure is not None:
            # The model is told the command ran, so it does not run it again thinking it did not.
            raise RuntimeError(f"the command ran (exit code {outcome.exit_code}, killed {outcome.killed}), but "
                               f"its record could not be completed: {type(failure).__name__}")

    async def import_(self, data: bytes) -> None:
        # A write has no timeout of its own: its tool's deadline covers max_command_seconds.
        async with self._manager.using(self._space, self.spec.max_command_seconds):
            handle = await self.ensure()
            await self._refuse_if_full(handle, "write_workspace_file")
            try:
                await self._manager.provider.import_(handle, data)
                await self._measure(handle)
            except asyncio.CancelledError:
                # The write may have landed, unmeasured: the next use reads first (M40 round 1).
                self._stale = True
                raise

    async def export(self, path: str) -> bytes:
        handle = await self.ensure()
        return await self._manager.provider.export(handle, path)

    async def export_artifact(self, path: str, read: Callable[[bytes, str], bytes],
                              put: Callable[[bytes, str], Awaitable[Any]]) -> Any:
        """FR-150: the file at `path` as an artifact, its source the workspace, the path and the
        last command that ended in it before the export began (DECISION-825b81a6). `read` takes
        the regular file out of the export, refusing anything else; `put` stores it."""
        handle = await self.ensure()
        command_id = self._space.last_command
        data = read(await self._manager.provider.export(handle, path), path)
        source = f"workspace://{handle.workspace_id}/{quote(posixpath.normpath(path), safe='/')}"
        if command_id is not None:
            source += f"?command={command_id}"
        return await through(asyncio.ensure_future(put(data, source)))

    async def list(self, path: str, limit: int) -> tuple[list[tuple[str, str, int]], int]:
        handle = await self.ensure()
        return await self._manager.provider.list(handle, path, limit=limit)

    async def end(self, reason: str, emit: Emit) -> None:
        """The run is over: its own workspace is removed and recorded, unless `keep` holds it; a
        plan's workspace lives on until its top-level run ends (FR-151). Never raises but for its
        caller's own cancellation, and only once the removal is done: it runs on every path to a
        run's terminal event, before that event (pre-review D2). Cancelled too, what its proxy
        reported is booked first and an event being written lands before that terminal event
        (DECISION-f7114b8b)."""
        self.detached = True
        try:
            if self.spec.scope != "plan":
                await self._manager.hold_or_remove(self._space, reason, emit, self._call)
        finally:
            await self._manager.settled(self._space, self._binding)


class WorkspaceManager:
    """FR-138: every workspace a Runner creates, through its one provider."""

    def __init__(self, provider: WorkspaceProvider, store: Callable[[], Any], max_concurrent: int) -> None:
        self.provider = provider
        self.store = store
        self.max_concurrent = max_concurrent
        # FR-151: who owns this Runner's workspaces, reaped by once that process is gone, and
        # which Runner of that process created them, removed by at aclose() (DECISION-825b81a6).
        self.owner = this_owner()
        self.runner_id = str(uuid.uuid4())
        self.slots = _Slots(max_concurrent)
        self.spaces: set[_Space] = set()  # every workspace being created or held, not yet removed
        self._plans: dict[tuple[str, str, str, str], _Space] = {}
        self._ended: set[str] = set()  # top-level runs whose plan workspaces were ended
        self._closed = False
        self._reaped = False
        self._reaping: asyncio.Future[None] | None = None
        self._late: set[asyncio.Task[None]] = set()  # passes counting reports after a recorder ended
        self._stop_late = False  # set by aclose past its bound (round 5 N2)
        self._trailing: set[asyncio.Task[Any]] = set()  # a late record's removal, on its own loop (M45 N1)
        self._runs: Callable[[], WorkspaceManager | None] | None = None  # a server side's Runner manager
        # FR-168 (M46): each snapshot this manager took and has not removed, with its tenancy and
        # whether its workspace was kept, which decides whether aclose() keeps it (DECISION-bafa7be8).
        self.snapshots: dict[str, tuple[str, str, bool]] = {}

    def for_servers(self) -> WorkspaceManager:
        """FR-165: the manager a sandboxed server's engine is given -- this one's provider, store,
        owner, Runner label and slots, and none of its runs' workspaces. NFR-39 (M45 round 3,
        DECISION-8e21d1ff): the engine is held by a module-global finalizer, so it holds nothing
        that reaches a run; this manager it reaches only weakly, for whether it was closed."""
        servers = WorkspaceManager(self.provider, self.store, self.max_concurrent)
        servers.owner, servers.runner_id, servers.slots = self.owner, self.runner_id, self.slots
        servers._runs = weakref.ref(self)
        return servers

    def for_run(self, spec: WorkspaceSpec, tenant_id: str, project_id: str, run_id: str, emit: Emit,
                call: Call, *, plan_run_id: str | None = None, account: Any = None,
                top: tuple[Emit, Call] | None = None, stopping: Callable[[], bool] | None = None) -> RunWorkspace:
        """A run's binding. Under `scope="plan"` every run of one top-level run (`plan_run_id`,
        or the run itself) and one spec shares a workspace, recorded under that top-level run; no
        other plan's run reaches it (FR-152). `top` is that run's emitter and store call: a
        connection of no command is its (FR-162, DECISION-e510a5e3)."""
        space = None
        if spec.scope == "plan":
            # A plan whose top-level run already ended its workspaces is over: the run is its own
            # top, so nothing it creates outlives it (M41 pre-review, DECISION-b1498111).
            top_id = str(run_id if plan_run_id is None or str(plan_run_id) in self._ended else plan_run_id)
            key = (tenant_id, project_id, top_id, _spec_key(spec))
            space = self._plans.get(key)
            if space is None:
                space = self._plans[key] = _Space(spec, tenant_id, project_id, top_id)
                if top_id == str(run_id):
                    space.owner = _Binding(top_id, emit, call)
                elif top is not None:
                    space.owner = _Binding(top_id, *top)
                else:  # no emitter of its own given: its rows, under it, with no event
                    space.owner = _Binding(top_id, _no_event, call)
        return RunWorkspace(self, spec, tenant_id, project_id, run_id, emit, call, space, account, stopping)

    async def book(self, space: _Space, handle: WorkspaceHandle) -> None:
        """FR-162: records each report of a workspace's proxy, in order, for as long as it lives. A
        report that cannot be recorded never fails the run: a connection whose row the store refused
        is counted on the workspace's `unrecorded` row (DECISION-f7114b8b)."""
        try:
            while True:
                report = await space.reports.get()
                if report is _STOP or space.stop_booking:
                    # F1: ended by its marker, or told to; never by cancel (DECISION-a7a1b37b). Told
                    # to, what is left is counted in one row (DECISION-e510a5e3).
                    if space.stop_booking:
                        await self._unrecorded(space, handle, report)
                    return
                if isinstance(report, _Forget):
                    space.commands.pop(report.command_id, None)
                    continue
                if isinstance(report, _Barrier):
                    if not report.reached.done():
                        report.reached.set_result(None)
                    continue
                await self._try_book(space, handle, report)
        finally:
            # Round 3 B: what is queued behind its marker is counted, as is what the store refused;
            # what arrives from now on is counted by a pass of its own (DECISION-f7114b8b).
            space.booked = True
            await self._unrecorded(space, handle)

    def arrived(self, space: _Space, handle: WorkspaceHandle, report: Any) -> None:
        """FR-162: a report of a workspace's proxy, queued for its recorder; once that has ended, a
        pass of its own counts it on the `unrecorded` row (DECISION-f7114b8b)."""
        space.reports.put_nowait(report)
        if space.booked and (space.late is None or space.late.done()):
            space.late = asyncio.ensure_future(self._unrecorded(space, handle))
            self._late.add(space.late)
            space.late.add_done_callback(self._late.discard)

    async def _try_book(self, space: _Space, handle: WorkspaceHandle, report: dict) -> None:
        if report.get("kind") == "limit":
            space.limit_seen = max(space.limit_seen, report.get("count") or 0)
        try:
            await self._book(space, handle, report)
        except Exception:  # noqa: BLE001 - counted, never the run's failure (NFR-11, DECISION-f7114b8b)
            if report.get("kind") == "connection":
                space.unbooked += 1

    async def _unrecorded(self, space: _Space, handle: WorkspaceHandle, first: Any = None) -> None:
        """Past its removal's bound, or once its recorder has ended: every connection left in the
        queue, and every refusal at the limit its row does not count, counted on the one `unrecorded`
        row (DECISION-e510a5e3); then a tunnel's end still writes its bytes, and a proxy's death its
        row (DECISION-f7114b8b). Ends once the queue is empty."""
        left = [] if first is None else [first]
        while True:
            while not space.reports.empty():
                left.append(space.reports.get_nowait())
            reports = [r for r in left if isinstance(r, dict)]
            for barrier in (r for r in left if isinstance(r, _Barrier) and not r.reached.done()):
                barrier.reached.set_result(None)
            for report in reports:
                if report.get("kind") == "connection":
                    space.unbooked += 1
                elif report.get("kind") == "limit":
                    space.limit_seen = max(space.limit_seen, report.get("count") or 0)
                elif report.get("kind") == "ended":
                    space.proxy_ended = True
            # The count first: a tunnel's bytes wait on the store that failed to keep up.
            await self._count_unrecorded(space, handle)
            for report in reports:
                if self._stop_late:  # N2: told to by aclose past its bound, after its current write
                    return
                if report.get("kind") in ("end", "ended"):
                    await self._try_book(space, handle, report)
            if space.reports.empty() or self._stop_late:
                return
            left = []

    async def _count_unrecorded(self, space: _Space, handle: WorkspaceHandle) -> None:
        """A workspace's one `unrecorded` row: written once there is anything to count, its count
        updated as more is (DECISION-f7114b8b). Its id is fixed before its first write and each write
        tries an update on it first, so an insert that committed and then raised is updated, never
        written twice (round 5 N6, DECISION-7a3a2ae2)."""
        async with space.counting:
            count = space.unbooked + max(0, space.limit_seen - space.limit_counted)
            if count <= space.unrecorded_counted:
                return
            binding, store = space.owner, self.store()
            with contextlib.suppress(Exception):  # the store is what failed to keep up
                if not await binding.call(store.update_connection, space.tenant_id, space.project_id,
                                          space.unrecorded_row, count=count):
                    await binding.call(store.record_connection, ConnectionRecord(
                        connection_id=space.unrecorded_row, workspace_id=handle.workspace_id,
                        tenant_id=space.tenant_id, project_id=space.project_id, run_id=binding.run_id,
                        command_id=None, host=None, port=None, address=None, allowed=False, reason="unrecorded",
                        at=now(), count=count))
                space.unrecorded_counted = count

    async def _book(self, space: _Space, handle: WorkspaceHandle, report: dict) -> None:
        store, kind = self.store(), report.get("kind")
        command_id = report.get("command_id")
        # A token naming no command was mapped to none by its provider: the workspace's own run.
        binding = space.commands.get(command_id) or space.owner
        run_id, call = binding.run_id, binding.call
        if kind == "end":
            await call(store.update_connection, space.tenant_id, space.project_id, report["id"],
                       bytes_out=report.get("bytes_out"), bytes_in=report.get("bytes_in"))
            return
        if kind == "ended":
            space.proxy_ended = True
        if kind == "limit" and space.limit_row is not None:
            await call(store.update_connection, space.tenant_id, space.project_id, space.limit_row,
                       count=report["count"])
            space.limit_counted = report["count"]
            return
        if kind not in ("connection", "limit", "ended"):
            return
        limit, ended = kind == "limit", kind == "ended"
        record = ConnectionRecord(
            connection_id=report["id"] if kind == "connection" else str(uuid.uuid4()),
            workspace_id=handle.workspace_id,
            tenant_id=space.tenant_id, project_id=space.project_id, run_id=run_id, command_id=command_id,
            host=report.get("host"), port=report.get("port"), address=report.get("address"),
            allowed=bool(kind == "connection" and report.get("allowed")),
            reason="connection_limit" if limit else "proxy_ended" if ended else report["reason"],
            at=_when(report.get("at")),
            count=report["count"] if limit else None)
        try:
            await call(store.record_connection, record)
        except Exception:
            if not ended:
                raise
            # Round 5 N3: a proxy's death is never silent -- its row refused, it is counted on the
            # `unrecorded` row and its event still written (DECISION-0d5473ef).
            space.unbooked += 1
        if limit:
            space.limit_row, space.limit_counted = record.connection_id, record.count
        async with space.booking:
            # F3: once its run has ended, the row alone (DECISION-a7a1b37b): its gate, shut, lets
            # no event in (DECISION-5c68ddfb).
            with contextlib.suppress(Exception):  # the row is the record
                # F4: the limit's one event marks it reached; the row carries the count.
                await _gated(binding, EventType.WORKSPACE_CONNECTION, {
                    "connection_id": record.connection_id, "workspace_id": record.workspace_id,
                    "command_id": record.command_id, "host": record.host, "port": record.port,
                    "address": record.address, "allowed": record.allowed, "reason": record.reason})

    async def _booked(self, space: _Space) -> None:
        """The proxy's last reports recorded before its workspace's removal is, within a bound: its
        recorder ends at a marker queued behind them, or is told to past the bound (F1)."""
        if space.recorder is None:
            return
        space.reports.put_nowait(_STOP)
        await settle(space.recorder, _REMOVE_SECONDS, lambda: setattr(space, "stop_booking", True))

    def refuse_if_closed(self) -> None:
        runs = self if self._runs is None else self._runs()
        if self._closed or runs is None or runs._closed:  # a server side's Runner, gone, was never closed
            raise WorkspaceUnavailable("this Runner was closed: it creates no workspace")

    # --- sandboxed MCP servers (FR-164, FR-165, M45) ----------------------------------------------

    async def open_server(self, spec: WorkspaceSpec, files: Any, tenant_id: str, project_id: str, *,
                          seconds: float) -> _Space:
        """A sandboxed server's workspace, on the loop its connection lives on: a slot within
        `seconds` (its connect_timeout), its files imported through the checked transfer, recorded
        scope `server` with no run -- it is its Runner's tenancy's, as its connection is
        (DECISION-2a12196e) -- and with egress, its connections booked as rows with no run and no
        event (DECISION-61aed587). It is kept out of `spaces`, which the Runner's own loop ends: its
        connection removes it, and `aclose()` sweeps by this Runner's label what is left."""
        self.refuse_if_closed()
        if spec.from_snapshot is not None:
            # Its configuration refuses one first; this is the manager's own check (M46 pre-review F2).
            raise WorkspaceUnavailable("a server's workspace starts from its files, never from a snapshot")
        space = _Space(spec, tenant_id, project_id, None)
        space.owner = _Binding(None, _no_event, _stored)
        try:
            await asyncio.wait_for(self.slots.take(None, space), seconds)
        except TimeoutError:
            raise WorkspaceUnavailable(f"no workspace slot came free within {seconds} s") from None
        space.slot = True
        provider, handle, writing = self.provider, None, None
        labels = {"agentsdk.tenant": tenant_id, "agentsdk.project": project_id,
                  "agentsdk.workspace": str(uuid.uuid4()), "agentsdk.owner": self.owner,
                  "agentsdk.runner": self.runner_id}
        try:
            self.refuse_if_closed()
            handle = await asyncio.wait_for(provider.create(spec, labels), SETUP_SECONDS)
            if files:
                await asyncio.wait_for(provider.import_(handle, files_tar(files)), SETUP_SECONDS)
            # Bounded: its connection's start is (connect_timeout, M33's every-wait rule).
            writing = asyncio.ensure_future(_stored(self.store().record_workspace, WorkspaceRecord(
                workspace_id=handle.workspace_id, tenant_id=tenant_id, project_id=project_id, run_id=None,
                scope="server", provider=provider.name, image=spec.image, limits=spec.record(), created_at=now())))
            await asyncio.wait_for(asyncio.shield(writing), _REMOVE_SECONDS)
        except BaseException as exc:
            if handle is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(provider.remove(handle), _REMOVE_SECONDS)
                # Pre-review F6, round 1 N1: a record write that timed out may have landed, or land
                # later: its removal record follows it whenever it does.
                if writing is not None:
                    # Kept from now, not from when the write lands: a close waits for it (NB1).
                    self._trail(self._removed_after(writing, tenant_id, project_id, handle.workspace_id))
            space.slot = False
            self.slots.give()
            if isinstance(exc, Exception) and not isinstance(exc, WorkspaceUnavailable):
                raise WorkspaceUnavailable(f"the server's workspace could not be made: {type(exc).__name__}") from None
            raise
        space.handle = handle
        watch = getattr(provider, "watch_egress", None)
        if spec.egress and watch is not None:
            space.reports = asyncio.Queue()
            space.recorder = asyncio.ensure_future(self.book(space, handle))
            watch(handle, _arrival(self, space, handle))
        return space

    def _trail(self, work: Awaitable[Any]) -> None:
        """Work that follows a write still under way when its caller gave up, kept until it ends."""
        task = asyncio.ensure_future(work)
        self._trailing.add(task)
        task.add_done_callback(self._trailing.discard)

    async def _removed_after(self, writing: asyncio.Future[Any], tenant_id: str, project_id: str,
                             workspace_id: str) -> None:
        """A record written late, or never: its removal recorded once its write has ended."""
        await asyncio.wait({writing})
        await self._record_removal(tenant_id, project_id, workspace_id, "aclose", None)

    async def trailed(self) -> None:
        """Waits, within the removal bound, for the work that follows a late write, and for a pass
        counting what a server's proxy reported after its recorder ended -- before the loop it runs on
        is closed (M45 round 2 NB1; round 3 pre-review). Never raises but for its caller's cancellation."""
        if self._trailing or self._late:
            await asyncio.wait(self._trailing | self._late, timeout=_REMOVE_SECONDS)

    async def server_launch(self, space: _Space, command: str, args: list[str]) -> Any:
        """How its provider starts the server over its workspace, each time it starts."""
        return await self.provider.server_launch(space.handle, command, list(args))

    async def close_server(self, space: _Space) -> None:
        """Its server has ended for good, with its Runner: its workspace removed, recorded `aclose`."""
        await self.finish(space, "aclose", None, None)

    async def end_plans(self, run_id: str, emit: Emit, call: Call) -> None:
        """FR-151: a top-level run ended, and with it the workspaces its plan's children shared."""
        self._ended.add(str(run_id))
        for key in [key for key in self._plans if key[2] == str(run_id)]:
            space = self._plans.pop(key)
            await self.hold_or_remove(space, "run_ended", emit, call)
            if space.owner is not None and space.owner.run_id == str(run_id):
                await self.settled(space, space.owner)

    async def settled(self, space: _Space, binding: _Binding) -> None:
        """F3: a run is ending. What its workspace's proxy reported so far is booked with its events
        first, within the bound -- one that lives on was not drained -- then its connections are
        rows alone, and an event already being written lands before its terminal event
        (DECISION-a7a1b37b). Cancelled meanwhile, it still does all of that, then raises the cancel
        (round 5 B1, DECISION-0d5473ef)."""
        await through(self._settled_events(space, binding))

    async def _settled_events(self, space: _Space, binding: _Binding) -> None:
        loop = asyncio.get_running_loop()
        until = loop.time() + _REMOVE_SECONDS
        try:
            if space.recorder is not None and not space.recorder.done():
                barrier = _Barrier()
                space.reports.put_nowait(barrier)
                await asyncio.wait({barrier.reached}, timeout=_REMOVE_SECONDS)
        finally:
            # From here its connections are rows alone: an event not yet begun is shut out
            # (DECISION-5c68ddfb).
            binding.gate.shut()
        try:
            await asyncio.wait_for(space.booking.acquire(), max(0.0, until - loop.time()))
        except TimeoutError:
            # Round 5 N1: past the bound, only an event already inside its write is waited for; it
            # lands first, bounded as every store write is (DECISION-5c68ddfb).
            if binding.gate.busy():
                async with space.booking:
                    pass
            return
        space.booking.release()

    def forget(self, run_id: str) -> None:
        """The run is no longer under way: the Runner's own check now names it ended."""
        self._ended.discard(str(run_id))

    async def hold_or_remove(self, space: _Space, reason: str, emit: Emit | None, call: Call | None) -> None:
        if space.spec.keep and reason == "run_ended":
            await through(self._kept(space))
            return
        await self.finish(space, reason, emit, call)

    @staticmethod
    async def _kept(space: _Space) -> None:
        """A kept workspace's creation waited out, then marked kept: one step under `through`, so a
        cancel held during the wait never skips the mark (DECISION-1e35a472)."""
        if _under_way(space.creating):
            with contextlib.suppress(Exception):
                await space.creating
        space.kept = space.handle is not None and not space.removed

    async def finish(self, space: _Space, reason: str, emit: Emit | None, call: Call | None) -> None:
        """Removes a workspace, once, and records it; `emit` is None when no run is left to tell."""
        space.removed = True
        if space.ending is None:
            space.ending = asyncio.ensure_future(self._remove(space, reason, emit, call))
        await through(space.ending)

    async def _remove(self, space: _Space, reason: str, emit: Emit | None, call: Call | None) -> None:
        try:
            handle = space.handle
            if handle is None and space.creating is not None:
                try:
                    handle = await space.creating  # a creation under way is finished, then removed
                except BaseException:  # noqa: BLE001 - nothing was created
                    handle = None
            if handle is None:
                return
            try:
                await asyncio.wait_for(self.provider.remove(handle), _REMOVE_SECONDS)
            except Exception:  # noqa: BLE001 - recorded as removed; what a provider left is reaped
                pass
            await self._booked(space)
            await self._record_removal(space.tenant_id, space.project_id, handle.workspace_id, reason, call)
            if emit is not None:
                try:
                    await emit(EventType.WORKSPACE_REMOVED, {"workspace_id": handle.workspace_id, "reason": reason})
                except Exception:  # noqa: BLE001
                    pass
        finally:
            self.spaces.discard(space)
            if space.slot:
                space.slot = False
                self.slots.give()

    async def _record_removal(self, tenant_id: str | None, project_id: str | None, workspace_id: str, reason: str,
                              call: Call | None) -> None:
        if not tenant_id or not project_id:
            return
        try:
            store = self.store()
            if call is not None:
                await call(store.record_removal, tenant_id, project_id, workspace_id, reason)
            else:
                await asyncio.to_thread(store.record_removal, tenant_id, project_id, workspace_id, reason)
        except Exception:  # noqa: BLE001 - nothing's ending is held up by its record
            pass

    # --- snapshots (FR-168, FR-169; DECISION-bafa7be8) -------------------------------------------

    @contextlib.asynccontextmanager
    async def using(self, space: _Space, seconds: float) -> Any:
        """A command or a write in `space`: counted while under way, and begun only once a snapshot
        being taken of it is done, waited for within the caller's own deadline, `seconds`, and at
        most SNAPSHOT_SECONDS (DECISION-f15a2771); yields the seconds it waited."""
        started = time.monotonic()
        bound = min(seconds, SNAPSHOT_SECONDS)
        while True:
            with space.use:
                copying = space.copying
                if copying is None:
                    space.busy += 1
                    break
            try:
                await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(copying)),
                                       max(0.0, bound - (time.monotonic() - started)))
            except TimeoutError:
                raise WorkspaceUnavailable("the workspace is still being snapshotted") from None
        try:
            yield time.monotonic() - started
        finally:
            with space.use:
                space.busy -= 1

    async def snapshot(self, tenant_id: str, project_id: str, workspace_id: str) -> SnapshotRef:
        """FR-168: a copy of a run's or a plan's workspace of this tenant and project, taken between
        commands, measured, and refused past its spec's `max_workspace_bytes`; recorded, or nothing
        of it is kept."""
        self.refuse_if_closed()
        space = next((s for s in list(self.spaces)
                      if s.handle is not None and not s.removed and s.handle.workspace_id == str(workspace_id)
                      and (s.tenant_id, s.project_id) == (tenant_id, project_id)), None)
        if space is None:
            await self._refuse_if_server(tenant_id, project_id, str(workspace_id))
            raise WorkspaceUnavailable(f"workspace {workspace_id} is no workspace of this Runner's runs in that tenancy")
        with space.use:
            if space.busy:
                raise WorkspaceRefused("a command or a write is under way in the workspace: a snapshot is taken "
                                       "between commands")
            if space.copying is not None:
                raise WorkspaceRefused("a snapshot of the workspace is being taken already")
            space.copying = concurrent.futures.Future()
        try:
            return await self._taken(space)
        finally:
            with space.use:
                copying, space.copying = space.copying, None
            copying.set_result(None)

    async def _taken(self, space: _Space) -> SnapshotRef:
        handle, snapshot_id = space.handle, str(uuid.uuid4())
        labels = {"agentsdk.snapshot": snapshot_id, "agentsdk.of": handle.workspace_id,
                  "agentsdk.tenant": space.tenant_id, "agentsdk.project": space.project_id,
                  "agentsdk.owner": self.owner, "agentsdk.runner": self.runner_id}
        writing: asyncio.Future[Any] | None = None
        try:
            taken = await asyncio.wait_for(self.provider.snapshot(handle, labels), SNAPSHOT_SECONDS)
            if taken.size_bytes is None or taken.size_bytes > space.spec.max_workspace_bytes:
                raise WorkspaceRefused(f"the snapshot holds {taken.size_bytes} B, past max_workspace_bytes "
                                       f"({space.spec.max_workspace_bytes} B): it is not kept")
            record = SnapshotRecord(snapshot_id=snapshot_id, workspace_id=handle.workspace_id,
                                    tenant_id=space.tenant_id, project_id=space.project_id,
                                    size_bytes=taken.size_bytes, created_at=now())
            writing = asyncio.ensure_future(_stored(self.store().record_snapshot, record))
            await asyncio.wait_for(asyncio.shield(writing), _REMOVE_SECONDS)
        except BaseException as exc:
            # Nothing unrecorded is kept, however the taking ended.
            try:
                await asyncio.wait_for(self.provider.remove_snapshot(snapshot_id), _REMOVE_SECONDS)
            except Exception:  # noqa: BLE001 - kept in sight, so aclose() tries again (M46 pre-review F6)
                self.snapshots[snapshot_id] = (space.tenant_id, space.project_id, False)
            if writing is not None:
                # A write that lands after its caller gave up -- cancelled, or past its bound -- is
                # followed by its removal record, as a server workspace's is (M46 pre-review F3).
                self._trail(self._snapshot_removed_after(writing, space.tenant_id, space.project_id, snapshot_id))
            if isinstance(exc, (WorkspaceRefused, WorkspaceUnavailable)) or not isinstance(exc, Exception):
                raise
            raise WorkspaceUnavailable(f"the snapshot could not be taken or recorded: {type(exc).__name__}") from None
        self.snapshots[snapshot_id] = (space.tenant_id, space.project_id, space.spec.keep)
        return SnapshotRef(snapshot_id=snapshot_id, workspace_id=handle.workspace_id, tenant_id=space.tenant_id,
                           project_id=space.project_id, size_bytes=taken.size_bytes)

    async def _snapshot_removed_after(self, writing: asyncio.Future[Any], tenant_id: str, project_id: str,
                                      snapshot_id: str) -> None:
        """A snapshot's record written late, or never, after its copy was removed: its removal
        recorded once its write has ended -- `aclose`, as a server workspace's late record is."""
        await asyncio.wait({writing})
        with contextlib.suppress(Exception):
            await asyncio.wait_for(_stored(self.store().record_snapshot_removal, tenant_id, project_id, snapshot_id,
                                           "aclose"), _REMOVE_SECONDS)

    async def _refuse_if_server(self, tenant_id: str, project_id: str, workspace_id: str) -> None:
        """A sandboxed server's workspace of this Runner -- labelled with no run -- is refused by name
        (DECISION-bafa7be8); anything else is simply not found."""
        wanted = {"agentsdk.workspace": workspace_id, "agentsdk.runner": self.runner_id,
                  "agentsdk.tenant": tenant_id, "agentsdk.project": project_id}
        try:
            owned = await asyncio.wait_for(self.provider.list_owned(wanted), _REMOVE_SECONDS)
        except Exception:  # noqa: BLE001 - not found, then
            return
        if any("agentsdk.run" not in handle.labels and all(handle.labels.get(k) == v for k, v in wanted.items())
               for handle in owned):
            raise WorkspaceRefused("a sandboxed MCP server's workspace is not snapshotted")

    async def _snapshot_of(self, snapshot_id: str, tenant_id: str, project_id: str) -> Any:
        """The provider's snapshot by its id, in this tenancy alone, or None. Its labels are compared
        exactly here as well: a provider's filter may not be (Docker's ignores trailing whitespace,
        M46 pre-review F1)."""
        wanted = {"agentsdk.snapshot": str(snapshot_id), "agentsdk.tenant": tenant_id, "agentsdk.project": project_id}
        found = await asyncio.wait_for(self.provider.list_snapshots(wanted), _REMOVE_SECONDS)
        return next((taken for taken in found
                     if all(taken.labels.get(key) == value for key, value in wanted.items())), None)

    async def usable_snapshot(self, snapshot_id: str, tenant_id: str, project_id: str) -> None:
        """FR-169: a workspace starts only from a snapshot of its own tenant and project that is
        still there; another tenancy's is not found, as a released one is."""
        try:
            found = await self._snapshot_of(snapshot_id, tenant_id, project_id)
        except Exception as exc:  # noqa: BLE001 - named by class only
            raise WorkspaceUnavailable(f"the snapshot could not be looked up: {type(exc).__name__}") from None
        if found is None:
            raise WorkspaceUnavailable(f"snapshot {snapshot_id} is no snapshot of this tenant and project")

    async def within_bound(self, handle: WorkspaceHandle, spec: WorkspaceSpec) -> None:
        """FR-169: a workspace started from a snapshot past its spec's `max_workspace_bytes`, or one
        whose size cannot be read, is removed before it is recorded (fail closed)."""
        try:
            size = await asyncio.wait_for(self.provider.size(handle), _MEASURE_SECONDS)
        except Exception as exc:  # noqa: BLE001 - named by class only
            size, why = None, type(exc).__name__
        if size is None or size > spec.max_workspace_bytes:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.provider.remove(handle), _REMOVE_SECONDS)
            if size is None:
                raise WorkspaceUnavailable(f"the snapshot's copy could not be measured ({why})")
            raise WorkspaceUnavailable(f"the snapshot holds {size} B, past this workspace's max_workspace_bytes "
                                       f"({spec.max_workspace_bytes} B)")

    async def release_snapshot(self, tenant_id: str, project_id: str, snapshot_id: str) -> bool:
        """FR-168: removes a snapshot of this tenant and project, whichever Runner took it, recorded
        `released`. False when there is no such snapshot. Raises `WorkspaceUnavailable` when the
        provider cannot remove it -- on Docker, while a restore is reading it -- and the snapshot
        stays, its row live, to be released again or reaped (DECISION-f15a2771)."""
        try:
            found = await self._snapshot_of(snapshot_id, tenant_id, project_id)
        except Exception as exc:  # noqa: BLE001 - named by class only
            raise WorkspaceUnavailable(f"the snapshot could not be looked up: {type(exc).__name__}") from None
        if found is None:
            return False
        await self._remove_snapshot(found.snapshot_id, tenant_id, project_id, "released")
        return True

    async def _remove_snapshot(self, snapshot_id: str, tenant_id: str | None, project_id: str | None,
                               reason: str) -> None:
        """Removed, then recorded; a removal that fails raises, and leaves it to be released again or
        reaped."""
        await asyncio.wait_for(self.provider.remove_snapshot(snapshot_id), _REMOVE_SECONDS)
        self.snapshots.pop(snapshot_id, None)
        with contextlib.suppress(Exception):  # nothing's ending is held up by its record
            await asyncio.wait_for(asyncio.to_thread(
                self.store().record_snapshot_removal, tenant_id, project_id, snapshot_id, reason), _REMOVE_SECONDS)

    async def release(self, tenant_id: str, project_id: str, workspace_id: str) -> bool:
        """FR-151: removes a kept workspace of this tenant and project whose run or plan has ended,
        recorded `released`. False for any other id."""
        for space in list(self.spaces):
            if (space.kept and not space.removed and space.handle is not None
                    and space.handle.workspace_id == str(workspace_id)
                    and (space.tenant_id, space.project_id) == (tenant_id, project_id)):
                await self.finish(space, "released", None, None)
                return True
        return False

    async def aclose(self) -> None:
        """FR-151: kills every command and removes every workspace this Runner created, kept ones
        included, recorded `aclose`; none is created after. A creation under way is finished,
        then removed. Never raises but for its caller's cancellation."""
        self._closed = True
        for space in list(self.spaces):
            with contextlib.suppress(Exception):
                await self.finish(space, "aclose", None, None)
        # FR-168: a snapshot of a workspace not kept goes too; a kept one's outlives this Runner, for
        # release or reaping (DECISION-bafa7be8).
        # A removal refused while a restore reads the snapshot is tried again within the bound; one
        # still refused is left to reaping (DECISION-f15a2771).
        deadline = time.monotonic() + _REMOVE_SECONDS
        for snapshot_id, (tenant_id, project_id, kept) in list(self.snapshots.items()):
            while not kept:
                try:
                    await self._remove_snapshot(snapshot_id, tenant_id, project_id, "aclose")
                    break
                except Exception:  # noqa: BLE001 - nothing's ending is held up by one snapshot
                    if time.monotonic() >= deadline:
                        break
                    await asyncio.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
        if self._trailing:  # a snapshot's record that landed late, followed by its removal (pre-review F3)
            await asyncio.wait(set(self._trailing), timeout=_REMOVE_SECONDS)
        if self._late:  # a report after its recorder ended, still being counted (DECISION-f7114b8b)
            _, left = await asyncio.wait(set(self._late), timeout=_REMOVE_SECONDS)
            if left:
                # Round 5 N2: past its bound, told to stop after its current write, and cancelled; it
                # ends unless inside a store write that never returns (DECISION-7a3a2ae2).
                self._stop_late = True
                for late in left:
                    late.cancel()
                await asyncio.wait(left, timeout=_REMOVE_SECONDS)
        try:
            left = await self.provider.list_owned({"agentsdk.runner": self.runner_id})
        except Exception:  # noqa: BLE001 - what the provider cannot list is reaped once this process is gone
            return
        for handle in left:
            try:
                await asyncio.wait_for(self.provider.remove(handle), _REMOVE_SECONDS)
            except Exception:  # noqa: BLE001
                continue
            await self._record_removal(handle.labels.get("agentsdk.tenant"), handle.labels.get("agentsdk.project"),
                                       handle.workspace_id, "aclose", None)

    async def reap(self) -> None:
        """FR-151: before this Runner's first workspace, removes those whose owner is provably
        gone, and records them `reaped`. One that fails is tried again before the next creation;
        it never fails the run. Nothing about another workspace reaches a run's events."""
        if self._reaped:
            return
        if self._reaping is None or self._reaping.done():
            self._reaping = asyncio.ensure_future(self._reap())
        try:
            await asyncio.shield(self._reaping)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - tried again before the next creation
            pass

    async def _reap(self) -> None:
        handles = await self.provider.list_owned({})
        for handle in handles:
            # Each in isolation: one workspace that cannot be judged or removed is left for the
            # next Runner that reaps, and never stops the rest (M41 pre-review).
            try:
                owner = handle.labels.get("agentsdk.owner")
                if not owner or not await asyncio.to_thread(is_dead, owner):
                    continue
                await asyncio.wait_for(self.provider.remove(handle), _REMOVE_SECONDS)
            except Exception:  # noqa: BLE001
                continue
            await self._record_removal(handle.labels.get("agentsdk.tenant"), handle.labels.get("agentsdk.project"),
                                       handle.workspace_id, "reaped", None)
        # FR-168: a dead owner's snapshots, each judged and removed as its workspaces are.
        listing = getattr(self.provider, "list_snapshots", None)
        for taken in (await listing({}) if listing is not None else []):
            try:
                owner = taken.labels.get("agentsdk.owner")
                if not owner or not await asyncio.to_thread(is_dead, owner):
                    continue
                await self._remove_snapshot(taken.snapshot_id, taken.labels.get("agentsdk.tenant"),
                                            taken.labels.get("agentsdk.project"), "reaped")
            except Exception:  # noqa: BLE001
                continue
        self._reaped = True


def _when(at: Any) -> Any:
    """A report's time, or now when it gave none that reads."""
    from datetime import datetime

    try:
        return datetime.fromisoformat(at)
    except (TypeError, ValueError):
        return now()


def _spec_key(spec: WorkspaceSpec) -> str:
    """Which plan workspace a spec shares: one per spec, env included, so no child runs under
    limits other than its own (FR-152)."""
    shape = json.dumps([spec.record(), sorted(spec.env.items())], sort_keys=True, default=str)
    return hashlib.sha256(shape.encode("utf-8")).hexdigest()
