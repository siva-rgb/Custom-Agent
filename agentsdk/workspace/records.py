"""FR-140 (M38): what a workspace and its commands leave in the record (migration 0019).

`InMemoryWorkspaceStore` serves a Runner without persistence; `PostgresWorkspaceStore`
(agentsdk.postgres) keeps the same methods durably. Both are synchronous: callers on an event loop
use a worker thread (FR-81). A workspace row is written once, and its removal once; a command row
is written once. Every read names its tenant and project (NFR-2).
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from typing import Any

REMOVAL_REASONS = ("run_ended", "released", "aclose", "reaped")


@dataclass(frozen=True)
class WorkspaceRecord:
    workspace_id: str
    tenant_id: str
    project_id: str
    run_id: str
    scope: str
    provider: str
    image: str
    limits: dict[str, Any]
    created_at: datetime
    removed_at: datetime | None = None
    removed_reason: str | None = None

    def as_row(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CommandRecord:
    command_id: str
    workspace_id: str
    tenant_id: str
    project_id: str
    run_id: str
    tool_call_id: str | None
    argv: list[str]
    exit_code: int | None
    killed: str | None
    started_at: datetime
    ended_at: datetime
    stdout_bytes: int
    stderr_bytes: int
    stdout_kept: int
    stderr_kept: int
    limit: str | None = None  # the bound that ended it, when killed == "limit" (migration 0021)
    cpu_usec: int | None = None  # what it is charged, in CPU-microseconds (FR-156, migration 0023)

    def as_row(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ConnectionRecord:
    """FR-162 (M44, migration 0024): one connection through a workspace's proxy, allowed or refused,
    from the proxy's own report. `command_id` is None when its token named no command; a
    `connection_limit` row stands for every connection refused at the limit, `count` of them, and an
    `unrecorded` row for every report not recorded within the removal's bound (DECISION-e510a5e3)."""

    connection_id: str
    workspace_id: str
    tenant_id: str
    project_id: str
    run_id: str
    command_id: str | None
    host: str | None
    port: int | None
    address: str | None
    allowed: bool
    reason: str
    at: datetime
    bytes_out: int | None = None
    bytes_in: int | None = None
    count: int | None = None

    def as_row(self) -> dict[str, Any]:
        return asdict(self)


def now() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryWorkspaceStore:
    """FR-140's store for a Runner without persistence: every tenancy's workspaces, one lock."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._spaces: dict[str, WorkspaceRecord] = {}
        self._commands: list[CommandRecord] = []
        self._connections: dict[str, ConnectionRecord] = {}

    def record_workspace(self, record: WorkspaceRecord) -> None:
        with self._lock:
            if record.workspace_id in self._spaces:
                raise ValueError(f"workspace {record.workspace_id} is already recorded")
            self._spaces[record.workspace_id] = record

    def record_removal(self, tenant_id: str, project_id: str, workspace_id: str, reason: str,
                       removed_at: datetime | None = None) -> bool:
        """Fills a workspace's removal, once: False when it was already removed, or is not a
        workspace of this tenant and project."""
        if reason not in REMOVAL_REASONS:
            raise ValueError(f"unknown removal reason: {reason!r}")
        with self._lock:
            record = self._spaces.get(workspace_id)
            if record is None or (record.tenant_id, record.project_id) != (tenant_id, project_id):
                return False
            if record.removed_at is not None:
                return False
            self._spaces[workspace_id] = replace(record, removed_at=removed_at or now(), removed_reason=reason)
            return True

    def record_command(self, record: CommandRecord) -> None:
        with self._lock:
            space = self._spaces.get(record.workspace_id)
            if space is None or (space.tenant_id, space.project_id) != (record.tenant_id, record.project_id):
                raise ValueError(f"no workspace {record.workspace_id} in this tenant and project")
            self._commands.append(record)

    def record_connection(self, record: ConnectionRecord) -> None:
        with self._lock:
            space = self._spaces.get(record.workspace_id)
            if space is None or (space.tenant_id, space.project_id) != (record.tenant_id, record.project_id):
                raise ValueError(f"no workspace {record.workspace_id} in this tenant and project")
            if record.connection_id in self._connections:
                raise ValueError(f"connection {record.connection_id} is already recorded")
            if record.reason == "unrecorded" and any(  # migration 0025's index (DECISION-7a3a2ae2)
                    c.reason == "unrecorded" and c.workspace_id == record.workspace_id
                    for c in self._connections.values()):
                raise ValueError(f"workspace {record.workspace_id} already has its unrecorded row")
            self._connections[record.connection_id] = record

    def update_connection(self, tenant_id: str, project_id: str, connection_id: str, *,
                          bytes_out: int | None = None, bytes_in: int | None = None,
                          count: int | None = None) -> bool:
        """A tunnel's bytes when it ends, or a limit row's count: the only later writes a row takes."""
        with self._lock:
            record = self._connections.get(connection_id)
            if record is None or (record.tenant_id, record.project_id) != (tenant_id, project_id):
                return False
            changes = {k: v for k, v in (("bytes_out", bytes_out), ("bytes_in", bytes_in), ("count", count))
                       if v is not None}
            self._connections[connection_id] = replace(record, **changes)
            return True

    def connections(self, tenant_id: str, project_id: str, run_id: str | None = None) -> list[ConnectionRecord]:
        with self._lock:
            return sorted((c for c in self._connections.values()
                           if (c.tenant_id, c.project_id) == (tenant_id, project_id)
                           and (run_id is None or c.run_id == run_id)), key=lambda c: c.at)

    def workspaces(self, tenant_id: str, project_id: str, run_id: str | None = None) -> list[WorkspaceRecord]:
        with self._lock:
            return sorted((r for r in self._spaces.values()
                           if (r.tenant_id, r.project_id) == (tenant_id, project_id)
                           and (run_id is None or r.run_id == run_id)), key=lambda r: r.created_at)

    def commands(self, tenant_id: str, project_id: str, run_id: str | None = None,
                 workspace_id: str | None = None) -> list[CommandRecord]:
        with self._lock:
            return [c for c in self._commands
                    if (c.tenant_id, c.project_id) == (tenant_id, project_id)
                    and (run_id is None or c.run_id == run_id)
                    and (workspace_id is None or c.workspace_id == workspace_id)]
