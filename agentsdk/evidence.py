"""Source versions and the scoped resource cache (FR-89 to FR-91, M22).

Every fetch inside a Runner records an immutable `EvidenceSourceVersion`: what was
requested, what answered, when, and the content as an M13 artifact verified against its
hash on every read. A run's `ResourceCache` serves a fresh version to any reader within
its scope's reach without a request, and copies one stored outside the reader's own
tenant and project rather than sharing it (NFR-25). Nothing here updates or deletes a
version: a refresh is a new version naming the one it replaced (NFR-26).
"""

from __future__ import annotations

import hashlib
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Awaitable, Callable, Protocol
from urllib.parse import urlsplit, urlunsplit

from .artifacts import canonical_id
from .errors import ArtifactIntegrityError, ArtifactNotFound
from .primitives import ContentProvenance, unstorable_reason

__all__ = [
    "CacheScope",
    "EvidenceSourceVersion",
    "EvidenceStore",
    "InMemoryEvidenceStore",
    "ResourceCache",
    "ResourceCacheKey",
    "canonical_uri",
]

DEFAULT_FRESHNESS_SECONDS = 3600
_SHA256 = re.compile(r"[0-9a-f]{64}")
_DEFAULT_PORTS = {"http": 80, "https": 443}


class CacheScope(str, Enum):
    """Who a version may be served to (ADR-31, P3-D7)."""

    PUBLIC_GLOBAL = "public_global"  # any tenant, by copy outside the origin's project
    TENANT = "tenant"  # the same tenant, by copy outside the origin's project
    PROJECT = "project"  # the same tenant and project: the built-in fetch's default
    SESSION = "session"  # the same top-level run (its ledger_run_id)
    NO_CACHE = "no_cache"  # nobody; still recorded


def canonical_uri(url: str) -> str:
    """FR-89: scheme and host lowercased, a default port and the fragment removed, the
    path and query kept as sent."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS or not parts.hostname:
        raise ValueError(f"canonical_uri needs an http(s) URL with a host, got {url!r}")
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    netloc = host if port is None or port == _DEFAULT_PORTS[scheme] else f"{host}:{port}"
    return urlunsplit((scheme, netloc, parts.path, parts.query, ""))


def _text(name: str, value: Any) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{name} must be non-empty text, got {value!r}")
    reason = unstorable_reason(value)
    if reason is not None:
        raise ValueError(f"{name} cannot be stored: {reason}")
    return value


def _uuid(name: str, value: Any) -> str:
    key = canonical_id(value) if isinstance(value, str) else None
    if key is None:
        raise ValueError(f"{name} must be a canonical UUID as text, got {value!r}")
    return key


def _http_uri(name: str, value: Any) -> str:
    _text(name, value)
    if urlsplit(value).scheme.lower() not in _DEFAULT_PORTS:
        raise ValueError(f"{name} must be an http(s) URL, got {value!r}")
    return value


def _hash(name: str, value: Any, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if type(value) is not str or not _SHA256.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase hex SHA-256, got {value!r}")
    return value


@dataclass(frozen=True)
class EvidenceSourceVersion:
    """FR-89: one immutable version of a source, every field validated by name."""

    source_version_id: str
    canonical_uri: str
    final_uri: str
    retrieval_time: datetime
    content_hash: str
    artifact_id: str
    media_type: str
    cache_scope: CacheScope
    auth_scope_hash: str | None
    prior_version: str | None
    provenance: ContentProvenance
    tenant_id: str
    project_id: str
    source_run: str

    def __post_init__(self) -> None:
        put = lambda name, value: object.__setattr__(self, name, value)  # noqa: E731
        put("source_version_id", _uuid("source_version_id", self.source_version_id))
        _http_uri("canonical_uri", self.canonical_uri)
        _http_uri("final_uri", self.final_uri)
        if not isinstance(self.retrieval_time, datetime) or self.retrieval_time.utcoffset() is None:
            raise ValueError(f"retrieval_time must be a timezone-aware datetime, got {self.retrieval_time!r}")
        _hash("content_hash", self.content_hash)
        put("artifact_id", _uuid("artifact_id", self.artifact_id))
        _text("media_type", self.media_type)
        if not isinstance(self.cache_scope, CacheScope):
            raise ValueError(f"cache_scope must be a CacheScope, got {self.cache_scope!r}")
        if self.auth_scope_hash is not None:
            _hash("auth_scope_hash", self.auth_scope_hash)
        if self.prior_version is not None:
            put("prior_version", _uuid("prior_version", self.prior_version))
        if not isinstance(self.provenance, ContentProvenance):
            raise ValueError(f"provenance must be a ContentProvenance, got {type(self.provenance).__name__}")
        _text("tenant_id", self.tenant_id)
        _text("project_id", self.project_id)
        put("source_run", _uuid("source_run", self.source_run))
        if self.cache_scope is CacheScope.PUBLIC_GLOBAL and self.auth_scope_hash is not None:
            # ADR-31: authenticated content is never globally shared.
            raise ValueError("a PUBLIC_GLOBAL version must have no auth_scope_hash")

    @property
    def uri(self) -> str:
        return f"urn:agentsdk:source:{self.source_version_id}"


@dataclass(frozen=True)
class ResourceCacheKey:
    """FR-90: what a cached version is found by -- the requested URI, never the final one."""

    canonical_uri: str
    auth_scope_hash: str | None = None
    request_variant: str | None = None


def reaches(scope: CacheScope, owner: tuple[str, str, str], reader: tuple[str, str, str]) -> bool:
    """FR-90: whether a version stored by `owner` (tenant, project, ledger run) may be
    served to `reader`."""
    if scope is CacheScope.PUBLIC_GLOBAL:
        return True
    if scope is CacheScope.TENANT:
        return owner[0] == reader[0]
    if scope is CacheScope.PROJECT:
        return owner[:2] == reader[:2]
    if scope is CacheScope.SESSION:
        return owner == reader
    return False


class EvidenceStore(Protocol):
    """Where versions are kept. Synchronous, as SessionStore is: callers on an event
    loop go through a worker thread (FR-20, FR-81). Nothing updates or deletes."""

    def record(self, version: EvidenceSourceVersion, *, ledger_run_id: str,
               request_variant: str | None = None) -> None: ...

    def get(self, tenant_id: str, project_id: str, source_version_id: str) -> EvidenceSourceVersion: ...

    def reachable(self, key: ResourceCacheKey, *, tenant_id: str, project_id: str,
                  ledger_run_id: str) -> list[EvidenceSourceVersion]: ...


class InMemoryEvidenceStore:
    """FR-90's store for a Runner without persistence: one per Runner, holding every
    tenant's versions as one database would, and nothing outlives it."""

    def __init__(self) -> None:
        # id -> (version, ledger run, request variant), in the order recorded.
        self._rows: dict[str, tuple[EvidenceSourceVersion, str, str | None]] = {}
        self._lock = threading.Lock()

    def record(self, version: EvidenceSourceVersion, *, ledger_run_id: str,
               request_variant: str | None = None) -> None:
        if not isinstance(version, EvidenceSourceVersion):
            raise ValueError(f"version must be an EvidenceSourceVersion, got {type(version).__name__}")
        ledger = _uuid("ledger_run_id", ledger_run_id)
        with self._lock:
            if version.source_version_id in self._rows:
                raise ValueError(f"source version {version.source_version_id} is already recorded; versions are immutable")
            self._rows[version.source_version_id] = (version, ledger, request_variant)

    def get(self, tenant_id: str, project_id: str, source_version_id: str) -> EvidenceSourceVersion:
        with self._lock:
            row = self._rows.get(canonical_id(source_version_id) or "")
        if row is None or (row[0].tenant_id, row[0].project_id) != (tenant_id, project_id):
            raise LookupError(f"no source version {source_version_id!r} in this tenant and project")
        return row[0]

    def reachable(self, key: ResourceCacheKey, *, tenant_id: str, project_id: str,
                  ledger_run_id: str) -> list[EvidenceSourceVersion]:
        reader = (tenant_id, project_id, ledger_run_id)
        with self._lock:
            rows = list(self._rows.values())
        found = [
            v for v, ledger, variant in rows
            if (v.canonical_uri, v.auth_scope_hash, variant) == (key.canonical_uri, key.auth_scope_hash, key.request_variant)
            and reaches(v.cache_scope, (v.tenant_id, v.project_id, ledger), reader)
        ]
        # Newest first; among equal retrieval times (a copy carries its original's),
        # the later recorded.
        return [v for _, v in sorted(enumerate(found), key=lambda p: (p[1].retrieval_time, p[0]), reverse=True)]


@dataclass(frozen=True)
class Served:
    version: EvidenceSourceVersion
    content: bytes


class ResourceCache:
    """FR-90: one run's view of the evidence store -- what it may be served, and where
    what it fetches is recorded. Built by the Runner for each run's tools (FR-76)."""

    def __init__(
        self,
        store: Callable[[], Any],
        artifacts_for: Callable[[str, str], Any],
        *,
        tenant_id: str,
        project_id: str,
        run_id: str,
        ledger_run_id: str,
        node_id: str | None = None,
        call: Callable[..., Awaitable[Any]],
        emit: Callable[[Any, dict[str, Any]], Awaitable[Any]] | None = None,
    ) -> None:
        self._store, self._artifacts_for = store, artifacts_for
        self.tenant_id, self.project_id, self.run_id = tenant_id, project_id, run_id
        self.ledger_run_id, self.node_id = ledger_run_id, node_id
        # Store calls go through this, which runs them on a worker thread (FR-81).
        self._call, self._emit = call, emit

    def _own(self, version: EvidenceSourceVersion) -> bool:
        return (version.tenant_id, version.project_id) == (self.tenant_id, self.project_id)

    async def _content(self, version: EvidenceSourceVersion) -> bytes | None:
        """The version's content if it reads back against its hash, else None."""
        try:
            content = await self._artifacts_for(version.tenant_id, version.project_id).get(version.artifact_id)
        except (ArtifactNotFound, ArtifactIntegrityError):
            return None
        return content if hashlib.sha256(content).hexdigest() == version.content_hash else None

    async def serve(
        self, key: ResourceCacheKey, *, now: datetime, freshness_seconds: float, scope: CacheScope, created_by: str
    ) -> tuple[Served | None, str | None]:
        """A fresh version within reach, copied in if it is another tenant's or project's,
        or None and the version a fetch would replace: this tenant and project's newest
        for the key, stale or unreadable."""
        found = await self._call(self._store().reachable, key, tenant_id=self.tenant_id,
                                 project_id=self.project_id, ledger_run_id=self.ledger_run_id)
        fresh = [v for v in found if timedelta(0) <= now - v.retrieval_time < timedelta(seconds=freshness_seconds)]
        # Our own first: a copy is made only when nothing of ours will do.
        for version in sorted(fresh, key=lambda v: not self._own(v)):
            content = await self._content(version)
            if content is None:
                continue
            copied = not self._own(version)
            if copied:
                version = await self._record(
                    key, content, final_uri=version.final_uri, media_type=version.media_type, scope=scope,
                    retrieval_time=version.retrieval_time, prior=self._prior(found), created_by=created_by)
            await self._event("SOURCE_SERVED", version, copied=copied,
                              age_seconds=(now - version.retrieval_time).total_seconds())
            return Served(version, content), None
        return None, self._prior(found)

    def _prior(self, found: list[EvidenceSourceVersion]) -> str | None:
        own = [v for v in found if self._own(v)]
        return own[0].source_version_id if own else None

    async def record(
        self, key: ResourceCacheKey, content: bytes, *, final_uri: str, media_type: str, scope: CacheScope,
        retrieval_time: datetime, prior: str | None, created_by: str,
    ) -> EvidenceSourceVersion:
        version = await self._record(key, content, final_uri=final_uri, media_type=media_type, scope=scope,
                                     retrieval_time=retrieval_time, prior=prior, created_by=created_by)
        await self._event("SOURCE_FETCHED", version, copied=False)
        return version

    async def _record(
        self, key: ResourceCacheKey, content: bytes, *, final_uri: str, media_type: str, scope: CacheScope,
        retrieval_time: datetime, prior: str | None, created_by: str,
    ) -> EvidenceSourceVersion:
        from .tools import ResultProvenance

        version_id = str(uuid.uuid4())
        provenance = ResultProvenance.external().for_source(f"urn:agentsdk:source:{version_id}")
        ref = await self._artifacts_for(self.tenant_id, self.project_id).put(
            content, mime_type="text/plain", provenance=provenance, created_by_agent=created_by,
            source_run=self.run_id, source_task=self.node_id, expires_at=None,
        )
        version = EvidenceSourceVersion(
            source_version_id=version_id, canonical_uri=key.canonical_uri, final_uri=final_uri,
            retrieval_time=retrieval_time, content_hash=ref.content_hash, artifact_id=ref.artifact_id,
            media_type=media_type, cache_scope=scope, auth_scope_hash=key.auth_scope_hash, prior_version=prior,
            provenance=provenance, tenant_id=self.tenant_id, project_id=self.project_id, source_run=self.run_id,
        )
        await self._call(self._store().record, version, ledger_run_id=self.ledger_run_id,
                         request_variant=key.request_variant)
        return version

    async def _event(self, kind: str, version: EvidenceSourceVersion, **extra: Any) -> None:
        if self._emit is None:
            return
        from .events import EventType

        payload = {"source_uri": version.uri, "canonical_uri": version.canonical_uri,
                   "cache_scope": version.cache_scope.value, **extra}
        await self._emit(getattr(EventType, kind), payload)
