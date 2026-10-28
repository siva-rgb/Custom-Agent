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
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from ..errors import WorkspaceRefused, WorkspaceUnavailable
from ..events import EventType
from . import CommandOutcome, WorkspaceHandle, WorkspaceProvider, WorkspaceSpec
from .records import CommandRecord, WorkspaceRecord, now

Emit = Callable[[EventType, dict[str, Any]], Awaitable[Any]]
Call = Callable[..., Awaitable[Any]]
# How long removing a workspace, or killing a cancelled command, may take before the run goes on;
# the removal is still recorded (M41 reaps what a provider left).
_REMOVE_SECONDS = 30.0
_KILL_SECONDS = 10.0


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


class RunWorkspace:
    """One run's workspace, created at its first need."""

    def __init__(self, manager: WorkspaceManager, spec: WorkspaceSpec, tenant_id: str, project_id: str,
                 run_id: str, emit: Emit, call: Call) -> None:
        self._manager, self.spec = manager, spec
        self.tenant_id, self.project_id, self.run_id = tenant_id, project_id, run_id
        self._emit, self._call = emit, call
        self._creating: asyncio.Future[WorkspaceHandle] | None = None
        self._ending: asyncio.Future[None] | None = None
        self.handle: WorkspaceHandle | None = None
        self.removed = False
        self._full: int | None = None  # the size measured past the bound, once it was

    @property
    def workspace_id(self) -> str | None:
        return None if self.handle is None else self.handle.workspace_id

    async def _record(self, method: str, *args: Any) -> Any:
        return await self._call(getattr(self._manager.store(), method), *args)

    async def ensure(self) -> WorkspaceHandle:
        if self.removed:
            raise WorkspaceUnavailable("this run's workspace was removed")
        if self.handle is not None:
            return self.handle
        if self._creating is None or (self._creating.done() and self._creating.exception() is not None):
            self._creating = asyncio.ensure_future(self._create())
        return await _completed(self._creating)

    async def _create(self) -> WorkspaceHandle:
        workspace_id = str(uuid.uuid4())
        labels = {
            "agentsdk.tenant": self.tenant_id, "agentsdk.project": self.project_id, "agentsdk.run": self.run_id,
            "agentsdk.workspace": workspace_id, "agentsdk.owner": self._manager.owner,
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
                run_id=self.run_id, scope=self.spec.scope, provider=provider.name, image=self.spec.image,
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

    async def _refuse_if_full(self, tool: str) -> None:
        if self._full is None:
            return
        await self._emit(EventType.WORKSPACE_REFUSED, {
            "workspace_id": self.workspace_id, "tool": tool, "bound": "max_workspace_bytes",
            "size": self._full, "limit": self.spec.max_workspace_bytes})
        raise WorkspaceRefused(f"the workspace holds {self._full} B, past max_workspace_bytes "
                               f"({self.spec.max_workspace_bytes} B); it refuses every later command and write")

    async def _measure(self, handle: WorkspaceHandle) -> None:
        size = await self._manager.provider.size(handle)
        if size > self.spec.max_workspace_bytes:
            self._full = size

    async def run(self, argv: list[str], *, stdin: bytes | None, timeout: float,
                  tool_call_id: str | None) -> CommandOutcome:
        handle = await self.ensure()
        await self._refuse_if_full("run_command")
        command_id = str(uuid.uuid4())
        await self._emit(EventType.WORKSPACE_COMMAND_STARTED, {
            "command_id": command_id, "workspace_id": handle.workspace_id, "tool_call_id": tool_call_id,
            "argv": list(argv)})
        started_at, started = now(), time.monotonic()
        try:
            outcome = await self._manager.provider.run(handle, list(argv), timeout=timeout, stdin=stdin,
                                                       command_id=command_id)
        except asyncio.CancelledError:
            # Its run was cancelled: the command is killed and recorded so before the cancellation
            # goes on (FR-140; pre-review D3).
            await _completed(asyncio.ensure_future(
                self._cancelled(handle, command_id, argv, tool_call_id, started_at, started)))
            raise
        await self._finished(handle, command_id, argv, tool_call_id, started_at, started, outcome)
        return outcome

    async def _cancelled(self, handle: WorkspaceHandle, command_id: str, argv: list[str], tool_call_id: str | None,
                         started_at: Any, started: float) -> None:
        try:
            await asyncio.wait_for(self._manager.provider.kill(handle, command_id), _KILL_SECONDS)
        except Exception:  # noqa: BLE001 - recorded as cancelled; what a provider left is reaped (M41)
            pass
        outcome = CommandOutcome(exit_code=None, stdout=b"", stderr=b"", stdout_cut=False, stderr_cut=False,
                                 stdout_bytes=0, stderr_bytes=0, duration=time.monotonic() - started,
                                 killed="cancelled")
        try:
            await self._finished(handle, command_id, argv, tool_call_id, started_at, started, outcome)
        except Exception:  # noqa: BLE001 - the cancellation, not its record, decides how the run ends
            pass

    async def _finished(self, handle: WorkspaceHandle, command_id: str, argv: list[str], tool_call_id: str | None,
                        started_at: Any, started: float, outcome: CommandOutcome) -> None:
        """Measures the workspace, records the command and tells the run, whatever fails first: a
        failed record still leaves the bound measured and the end told (pre-review D4)."""
        failure: Exception | None = None
        try:
            await self._measure(handle)
        except Exception as exc:  # noqa: BLE001
            failure = exc
        try:
            await self._record("record_command", CommandRecord(
                command_id=command_id, workspace_id=handle.workspace_id, tenant_id=self.tenant_id,
                project_id=self.project_id, run_id=self.run_id, tool_call_id=tool_call_id, argv=list(argv),
                exit_code=outcome.exit_code, killed=outcome.killed, started_at=started_at,
                ended_at=max(now(), started_at), stdout_bytes=outcome.stdout_bytes,
                stderr_bytes=outcome.stderr_bytes, stdout_kept=len(outcome.stdout), stderr_kept=len(outcome.stderr)))
        except Exception as exc:  # noqa: BLE001
            failure = failure or exc
        await self._emit(EventType.WORKSPACE_COMMAND_ENDED, {
            "command_id": command_id, "workspace_id": handle.workspace_id, "tool_call_id": tool_call_id,
            "exit_code": outcome.exit_code, "killed": outcome.killed,
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "stdout_bytes": outcome.stdout_bytes, "stderr_bytes": outcome.stderr_bytes,
            "stdout_cut": outcome.stdout_cut, "stderr_cut": outcome.stderr_cut})
        if failure is not None:
            # The model is told the command ran, so it does not run it again thinking it did not.
            raise RuntimeError(f"the command ran (exit code {outcome.exit_code}, killed {outcome.killed}), but "
                               f"its record could not be completed: {type(failure).__name__}")

    async def import_(self, data: bytes) -> None:
        handle = await self.ensure()
        await self._refuse_if_full("write_workspace_file")
        await self._manager.provider.import_(handle, data)
        await self._measure(handle)

    async def export(self, path: str) -> bytes:
        handle = await self.ensure()
        return await self._manager.provider.export(handle, path)

    async def end(self, reason: str, emit: Emit) -> None:
        """Removes the workspace, if one was created, and records it. Never raises but for its
        caller's own cancellation, and only once the removal is done: it runs on every path to a
        run's terminal event, before that event (pre-review D2)."""
        self.removed = True
        if self._ending is None:
            self._ending = asyncio.ensure_future(self._end(reason, emit))
        await _completed(self._ending)

    async def _end(self, reason: str, emit: Emit) -> None:
        handle = self.handle
        if handle is None and self._creating is not None:
            try:
                handle = await self._creating  # a creation under way is finished, then removed
            except BaseException:  # noqa: BLE001 - nothing was created
                handle = None
        if handle is None:
            return
        try:
            await asyncio.wait_for(self._manager.provider.remove(handle), _REMOVE_SECONDS)
        except Exception:  # noqa: BLE001 - recorded as removed; what a provider left is reaped (M41)
            pass
        try:
            await self._record("record_removal", self.tenant_id, self.project_id, handle.workspace_id, reason)
        except Exception:  # noqa: BLE001 - the run's own ending is not held up by its record
            pass
        try:
            await emit(EventType.WORKSPACE_REMOVED, {"workspace_id": handle.workspace_id, "reason": reason})
        except Exception:  # noqa: BLE001
            pass


class WorkspaceManager:
    """FR-138: every workspace a Runner creates, through its one provider."""

    def __init__(self, provider: WorkspaceProvider, store: Callable[[], Any], max_concurrent: int) -> None:
        self.provider = provider
        self.store = store
        self.max_concurrent = max_concurrent
        # FR-151 (M41) reaps by this: the process that owns a workspace.
        self.owner = str(os.getpid())

    def for_run(self, spec: WorkspaceSpec, tenant_id: str, project_id: str, run_id: str, emit: Emit,
                call: Call) -> RunWorkspace:
        return RunWorkspace(self, spec, tenant_id, project_id, run_id, emit, call)
