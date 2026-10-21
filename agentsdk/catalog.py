"""The MCP catalog (FR-127, M34): every version of a tool a tenancy's connection has seen, and
the versions accepted for that tenancy.

A tool is usable only while its current `tool_hash` is accepted for the run's tenant and
project (ADR-28, P4-D21, P4-D29). Rows are written once and never updated or deleted, and
every row carries its tenant and project (NFR-2). The hash covers the tool's name as well as
its description, schemas and annotations: without it, two tools of one server with the same
description and schemas would share a hash, and accepting one would accept the other
(DECISION recorded at M34).

Standard library only: `agentsdk.mcp` imports this without the `mcp` extra (NFR-34).
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .errors import UnknownToolVersion
from .identity import PrincipalContext
from .primitives import unstorable_reason

__all__ = ["InMemoryCatalogStore", "PARTS", "ToolAcceptance", "ToolVersion", "changed_parts", "tool_entry",
           "tool_hash"]

_HASH = re.compile(r"[0-9a-f]{64}")
KINDS = ("tool", "prompt", "resource_template")  # prompts and resource templates arrive with M35
# The parts McpToolBlocked names, by the entry key each is read from.
PARTS = {"description": "description", "input_schema": "inputSchema", "output_schema": "outputSchema",
         "annotations": "annotations"}


def tool_entry(name: str, description: str | None, input_schema: Any, output_schema: Any = None,
               annotations: Any = None) -> dict[str, Any]:
    """A tool as the catalog records it: its wire fields, annotations in their wire names."""
    return {"name": name, "description": description, "inputSchema": input_schema,
            "outputSchema": output_schema, "annotations": annotations}


def tool_hash(name: str, description: str | None, input_schema: Any, output_schema: Any = None,
              annotations: Any = None) -> str:
    """FR-127: the SHA-256 of the canonical JSON of a tool's entry. What a server's `accept`
    names, and what `Runner.accept_tools` takes."""
    entry = tool_entry(name, description, input_schema, output_schema, annotations)
    return hashlib.sha256(json.dumps(entry, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def changed_parts(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """The parts of a tool that differ between two of its entries, in a fixed order."""
    return [part for part, key in PARTS.items() if before.get(key) != after.get(key)]


def checked_hash(value: Any) -> str:
    if type(value) is not str or not _HASH.fullmatch(value):
        raise ValueError(f"a tool hash is a SHA-256 in lowercase hex, got {value!r}")
    return value


def _text(name: str, value: Any) -> str:
    if type(value) is not str or not value or unstorable_reason(value) is not None:
        raise ValueError(f"{name} must be non-empty storable text, got {value!r}")
    return value


def _decider(value: Any) -> PrincipalContext:
    """FR-127: a principal naming a user or a service, as for a decision (FR-115)."""
    if not isinstance(value, PrincipalContext):
        raise ValueError(f"principal must be a PrincipalContext, got {type(value).__name__}")
    if not (value.user_principal or value.service_principal):
        raise ValueError("principal must name a user or a service, not only an agent")
    return value


@dataclass(frozen=True)
class ToolVersion:
    """One version of a tool a tenancy's connection has seen (`mcp_tool_versions`)."""

    tenant_id: str
    project_id: str
    alias: str
    tool_name: str
    tool_hash: str
    entry: dict[str, Any]
    first_seen_at: datetime
    kind: str = "tool"

    def __post_init__(self) -> None:
        for name in ("tenant_id", "project_id", "alias", "tool_name"):
            _text(name, getattr(self, name))
        checked_hash(self.tool_hash)
        if self.kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}, got {self.kind!r}")
        if not isinstance(self.entry, dict) or unstorable_reason(self.entry) is not None:
            raise ValueError("entry must be a storable JSON object")
        if not isinstance(self.first_seen_at, datetime) or self.first_seen_at.utcoffset() is None:
            raise ValueError("first_seen_at must be a timezone-aware datetime")


@dataclass(frozen=True)
class ToolAcceptance:
    """One accepted tool hash of a tenancy (`mcp_tool_acceptances`)."""

    acceptance_id: str
    tenant_id: str
    project_id: str
    alias: str
    tool_name: str
    tool_hash: str
    principal: PrincipalContext
    accepted_at: datetime

    def __post_init__(self) -> None:
        for name in ("tenant_id", "project_id", "alias", "tool_name"):
            _text(name, getattr(self, name))
        checked_hash(self.tool_hash)
        _decider(self.principal)
        if not isinstance(self.accepted_at, datetime) or self.accepted_at.utcoffset() is None:
            raise ValueError("accepted_at must be a timezone-aware datetime")


class InMemoryCatalogStore:
    """FR-127's store for a Runner without persistence: every tenancy's catalog, one lock.
    Insert-only: a version or an acceptance already recorded is returned as it was."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._versions: dict[tuple[str, str, str, str], ToolVersion] = {}
        self._acceptances: dict[tuple[str, str, str, str], ToolAcceptance] = {}
        self._order: list[tuple[str, str, str, str]] = []  # acceptances, oldest first

    def record_versions(self, versions: list[ToolVersion]) -> None:
        """Each version a connection has seen, kept as first seen."""
        for version in versions:
            if not isinstance(version, ToolVersion):
                raise ValueError(f"a version must be a ToolVersion, got {type(version).__name__}")
        with self._lock:
            for version in versions:
                self._versions.setdefault((version.tenant_id, version.project_id, version.alias, version.tool_hash),
                                          version)

    def version(self, tenant_id: str, project_id: str, alias: str, tool_hash: str) -> ToolVersion | None:
        with self._lock:
            return self._versions.get((tenant_id, project_id, alias, tool_hash))

    def accept(self, tenant_id: str, project_id: str, alias: str, tool_hashes: list[str],
               principal: PrincipalContext, accepted_at: datetime | None = None) -> list[ToolAcceptance]:
        """FR-127: every hash must have a version row in this tenancy, or nothing is recorded;
        a hash already accepted is returned unchanged."""
        _decider(principal)
        hashes = [checked_hash(h) for h in tool_hashes]
        accepted_at = accepted_at or datetime.now(timezone.utc)
        with self._lock:
            versions = [self._versions.get((tenant_id, project_id, alias, h)) for h in hashes]
            missing = [h for h, v in zip(hashes, versions) if v is None]
            if missing:
                raise UnknownToolVersion(f"no version {missing[0]} of a tool of {alias} in this tenant and project")
            made = []
            for h, v in zip(hashes, versions):
                key = (tenant_id, project_id, alias, h)
                if key not in self._acceptances:
                    self._acceptances[key] = ToolAcceptance(
                        acceptance_id=str(uuid.uuid4()), tenant_id=tenant_id, project_id=project_id, alias=alias,
                        tool_name=v.tool_name, tool_hash=h, principal=principal, accepted_at=accepted_at)
                    self._order.append(key)
                made.append(self._acceptances[key])
            return made

    def acceptances(self, tenant_id: str, project_id: str, alias: str,
                    tool_hashes: list[str]) -> dict[str, ToolAcceptance]:
        with self._lock:
            found = {h: self._acceptances.get((tenant_id, project_id, alias, h)) for h in tool_hashes}
        return {h: a for h, a in found.items() if a is not None}

    def last_accepted(self, tenant_id: str, project_id: str, alias: str,
                      tool_names: list[str]) -> dict[str, ToolVersion]:
        """Each named tool's most recently accepted version, for those with one: what a change
        is told from."""
        wanted, found = set(tool_names), {}
        with self._lock:
            for key in reversed(self._order):
                name = self._acceptances[key].tool_name
                if key[:3] == (tenant_id, project_id, alias) and name in wanted and name not in found:
                    found[name] = self._versions[key]
        return found
