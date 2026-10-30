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
import contextlib
import hashlib
import json
import posixpath
import threading
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

from ..errors import WorkspaceRefused, WorkspaceUnavailable
from ..events import EventType
from . import MEASURE_SECONDS, SETUP_SECONDS, CommandOutcome, WorkspaceHandle, WorkspaceProvider, WorkspaceSpec
from .owner import is_dead, this_owner
from .records import CommandRecord, WorkspaceRecord, now

Emit = Callable[[EventType, dict[str, Any]], Awaitable[Any]]
Call = Callable[..., Awaitable[Any]]
# How long removing a workspace, or killing a cancelled command, may take before the run goes on;
# the removal is still recorded (M41 reaps what a provider left).
_REMOVE_SECONDS = 30.0
_KILL_SECONDS = 10.0
_MEASURE_SECONDS = MEASURE_SECONDS


async def _completed(task: asyncio.Future[Any]) -> Any:
    """Waits for `task` to finish even when the caller is cancelled meanwhile, then re-raises
    that cancellation: work begun is never abandoned half done."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError()
    return task.result()


class _Slots:
    """FR-152: at most `limit` workspaces held at once, whichever loop each run is on. A waiter
    that is cancelled after its slot was handed to it gives that slot on. Each wait names the
    workspace it is for: once one of a plan's children begins creating it, its siblings still
    waiting are released without a slot, so one workspace never takes two (DECISION-b1498111)."""

    class _Waiter:
        def __init__(self, loop: asyncio.AbstractEventLoop, space: object) -> None:
            self.loop, self.future, self.space, self.granted = loop, loop.create_future(), space, False

    def __init__(self, limit: int) -> None:
        self.limit, self.held = limit, 0
        self._lock = threading.Lock()
        self._waiters: deque[_Slots._Waiter] = deque()

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
            self._waiters.append(waiter)
            held = self.held
        # Told from its own task: a store write awaited here would absorb this wait's cancellation
        # (handle.py), and a wait for a workspace is cancellable (FR-152).
        told = asyncio.ensure_future(waiting(held)) if waiting is not None else None
        try:
            await waiter.future
            if told is not None:
                await _completed(told)  # the wait is told before anything is created
        except asyncio.CancelledError:
            # Cancelled waiting, or after its grant while the wait was told (M41 pre-review).
            with self._lock:
                granted = waiter.granted
                if not granted and waiter in self._waiters:
                    self._waiters.remove(waiter)
            if granted:
                self.give()
            if told is not None:
                await _completed(told)  # the wait is told before the run's ending is
            raise
        return waiter.granted

    def release(self, space: object) -> None:
        """Every wait for `space` ends without a slot: a sibling is creating it."""
        with self._lock:
            for waiter in [w for w in self._waiters if w.space is space]:
                self._waiters.remove(waiter)
                waiter.loop.call_soon_threadsafe(_grant, waiter.future)

    def give(self) -> None:
        with self._lock:
            while self._waiters:
                waiter = self._waiters.popleft()
                waiter.granted = True
                waiter.loop.call_soon_threadsafe(_grant, waiter.future)
                return
            self.held -= 1


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
                 run_id: str, emit: Emit, call: Call, space: _Space | None = None) -> None:
        self._manager, self.spec = manager, spec
        self.tenant_id, self.project_id, self.run_id = tenant_id, project_id, run_id
        self._emit, self._call = emit, call
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
                await _completed(creating)
            raise
        if not creating.done():
            # Left to finish: it is the space's, which the run's end or aclose() removes.
            raise _not_ready()
        return await _completed(creating)

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
        try:
            handle = await provider.create(self.spec, labels)
        except WorkspaceUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - a provider's failure, named by class only
            raise WorkspaceUnavailable(f"the provider could not create a workspace: {type(exc).__name__}") from None
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
        handle = await self.ensure()
        await self._refuse_if_full(handle, "run_command")
        command_id = str(uuid.uuid4())
        await self._emit(EventType.WORKSPACE_COMMAND_STARTED, {
            "command_id": command_id, "workspace_id": handle.workspace_id, "tool_call_id": tool_call_id,
            "argv": list(argv)})
        started_at, started = now(), time.monotonic()
        # The command runs as its own task, so a cancellation of this call reaches the kill below
        # first, and not the command's own ending half way (FR-148).
        running = asyncio.ensure_future(self._manager.provider.run(handle, list(argv), timeout=timeout,
                                                                   stdin=stdin, command_id=command_id))
        try:
            outcome = await asyncio.shield(running)
        except asyncio.CancelledError:
            # Its run was cancelled: the command is killed and recorded so before the cancellation
            # goes on (FR-140, FR-148; pre-review D3).
            await _completed(asyncio.ensure_future(
                self._cancelled(handle, command_id, running, argv, tool_call_id, started_at, started)))
            raise
        except Exception as exc:
            # The provider failed with the command begun: it is ended and recorded as an error, by
            # class, and what it may have written is read before the next use (M40 caveat 2,
            # DECISION-825b81a6). The provider's own error still reaches the tool.
            self._stale = True
            failed = CommandOutcome(exit_code=None, stdout=b"", stderr=b"", stdout_cut=False, stderr_cut=False,
                                    stdout_bytes=0, stderr_bytes=0, duration=time.monotonic() - started,
                                    killed="error")
            with contextlib.suppress(Exception):  # the provider's failure, not its record, is reported
                await _completed(asyncio.ensure_future(self._finished(
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
                await _completed(asyncio.ensure_future(
                    self._finished(handle, command_id, argv, tool_call_id, started_at, started, outcome)))
            raise
        await _completed(asyncio.ensure_future(
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
        outcome = CommandOutcome(exit_code=None, stdout=b"", stderr=b"", stdout_cut=False, stderr_cut=False,
                                 stdout_bytes=0, stderr_bytes=0, duration=time.monotonic() - started,
                                 killed="cancelled")
        self._stale = True
        try:
            await self._finished(handle, command_id, argv, tool_call_id, started_at, started, outcome,
                                 kill_error=kill_error)
        except Exception:  # noqa: BLE001 - the cancellation, not its record, decides how the run ends
            pass

    async def _finished(self, handle: WorkspaceHandle, command_id: str, argv: list[str], tool_call_id: str | None,
                        started_at: Any, started: float, outcome: CommandOutcome, *,
                        kill_error: str | None = None) -> None:
        """Records the command and tells the run, whatever fails first: a failed record still
        leaves the end told (pre-review D4). The size is read before this, by the caller."""
        failure: Exception | None = None
        try:
            await self._record("record_command", CommandRecord(
                command_id=command_id, workspace_id=handle.workspace_id, tenant_id=self.tenant_id,
                project_id=self.project_id, run_id=self.run_id, tool_call_id=tool_call_id, argv=list(argv),
                exit_code=outcome.exit_code, killed=outcome.killed, started_at=started_at,
                ended_at=max(now(), started_at), stdout_bytes=outcome.stdout_bytes,
                stderr_bytes=outcome.stderr_bytes, stdout_kept=len(outcome.stdout), stderr_kept=len(outcome.stderr),
                limit=outcome.limit))
        except Exception as exc:  # noqa: BLE001
            failure = failure or exc
        self._space.last_command = command_id
        await self._emit(EventType.WORKSPACE_COMMAND_ENDED, {
            "command_id": command_id, "workspace_id": handle.workspace_id, "tool_call_id": tool_call_id,
            "exit_code": outcome.exit_code, "killed": outcome.killed, "limit": outcome.limit, "kill_error": kill_error,
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "stdout_bytes": outcome.stdout_bytes, "stderr_bytes": outcome.stderr_bytes,
            "stdout_cut": outcome.stdout_cut, "stderr_cut": outcome.stderr_cut})
        if failure is not None:
            # The model is told the command ran, so it does not run it again thinking it did not.
            raise RuntimeError(f"the command ran (exit code {outcome.exit_code}, killed {outcome.killed}), but "
                               f"its record could not be completed: {type(failure).__name__}")

    async def import_(self, data: bytes) -> None:
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
        return await _completed(asyncio.ensure_future(put(data, source)))

    async def list(self, path: str, limit: int) -> tuple[list[tuple[str, str, int]], int]:
        handle = await self.ensure()
        return await self._manager.provider.list(handle, path, limit=limit)

    async def end(self, reason: str, emit: Emit) -> None:
        """The run is over: its own workspace is removed and recorded, unless `keep` holds it; a
        plan's workspace lives on until its top-level run ends (FR-151). Never raises but for its
        caller's own cancellation, and only once the removal is done: it runs on every path to a
        run's terminal event, before that event (pre-review D2)."""
        self.detached = True
        if self.spec.scope == "plan":
            return
        await self._manager.hold_or_remove(self._space, reason, emit, self._call)


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

    def for_run(self, spec: WorkspaceSpec, tenant_id: str, project_id: str, run_id: str, emit: Emit,
                call: Call, *, plan_run_id: str | None = None) -> RunWorkspace:
        """A run's binding. Under `scope="plan"` every run of one top-level run (`plan_run_id`,
        or the run itself) and one spec shares a workspace, recorded under that top-level run; no
        other plan's run reaches it (FR-152)."""
        space = None
        if spec.scope == "plan":
            # A plan whose top-level run already ended its workspaces is over: the run is its own
            # top, so nothing it creates outlives it (M41 pre-review, DECISION-b1498111).
            top = str(run_id if plan_run_id is None or str(plan_run_id) in self._ended else plan_run_id)
            key = (tenant_id, project_id, top, _spec_key(spec))
            space = self._plans.get(key)
            if space is None:
                space = self._plans[key] = _Space(spec, tenant_id, project_id, top)
        return RunWorkspace(self, spec, tenant_id, project_id, run_id, emit, call, space)

    def refuse_if_closed(self) -> None:
        if self._closed:
            raise WorkspaceUnavailable("this Runner was closed: it creates no workspace")

    async def end_plans(self, run_id: str, emit: Emit, call: Call) -> None:
        """FR-151: a top-level run ended, and with it the workspaces its plan's children shared."""
        self._ended.add(str(run_id))
        for key in [key for key in self._plans if key[2] == str(run_id)]:
            await self.hold_or_remove(self._plans.pop(key), "run_ended", emit, call)

    def forget(self, run_id: str) -> None:
        """The run is no longer under way: the Runner's own check now names it ended."""
        self._ended.discard(str(run_id))

    async def hold_or_remove(self, space: _Space, reason: str, emit: Emit | None, call: Call | None) -> None:
        if space.spec.keep and reason == "run_ended":
            if _under_way(space.creating):
                with contextlib.suppress(Exception):
                    await _completed(space.creating)
            space.kept = space.handle is not None and not space.removed
            return
        await self.finish(space, reason, emit, call)

    async def finish(self, space: _Space, reason: str, emit: Emit | None, call: Call | None) -> None:
        """Removes a workspace, once, and records it; `emit` is None when no run is left to tell."""
        space.removed = True
        if space.ending is None:
            space.ending = asyncio.ensure_future(self._remove(space, reason, emit, call))
        await _completed(space.ending)

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
        self._reaped = True


def _spec_key(spec: WorkspaceSpec) -> str:
    """Which plan workspace a spec shares: one per spec, env included, so no child runs under
    limits other than its own (FR-152)."""
    shape = json.dumps([spec.record(), sorted(spec.env.items())], sort_keys=True, default=str)
    return hashlib.sha256(shape.encode("utf-8")).hexdigest()
