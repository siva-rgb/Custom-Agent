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
