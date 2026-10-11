"""The evidence ledger (FR-92 to FR-94, M23).

A top-level run's ledger holds the claims its agents record, each a statement bound to
the source versions it rests on. One statement, however it is cased, spaced or
normalised, is one claim; every agent that records it adds a contribution, and the
claim's sources and labels are derived from its contributions when it is read, so a
clean contribution never lowers them (P3-D11, ADR-26). Nothing is updated or deleted:
a supersession is a new claim naming the one it replaces, and the store keeps that
history a chain that never forks (NFR-26).
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import unicodedata
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Protocol

from .artifacts import canonical_id
from .primitives import ContentProvenance, unstorable_reason
from .tools import Tool, ToolOutput, ToolSpec

__all__ = [
    "ClaimContribution",
    "ClaimRefused",
    "ClaimView",
    "EvidenceClaim",
    "EvidenceLedger",
    "InMemoryLedgerStore",
    "LedgerStore",
    "NewContribution",
    "claim_key",
    "ledger_tools",
]

MAX_STATEMENT = 4000
_SHA256 = re.compile(r"[0-9a-f]{64}")
_WHITESPACE = re.compile(r"\s+")
_SOURCE_URN = "urn:agentsdk:source:"
_CLAIM_URN = "urn:agentsdk:claim:"


class ClaimRefused(ValueError):
    """A record or supersession the ledger will not make; nothing was written."""


def normalised(statement: str) -> str:
    """P3-D9: Unicode NFC, then case-folded, then NFC again -- case-folding alone is not
    canonically closed (M23 review caveat 4) -- then every run of whitespace one space and
    the ends trimmed."""
    folded = unicodedata.normalize("NFC", unicodedata.normalize("NFC", statement).casefold())
    return _WHITESPACE.sub(" ", folded).strip()


def claim_key(statement: str) -> str:
    """The SHA-256 of the normalised statement's UTF-8 (FR-92)."""
    return hashlib.sha256(normalised(statement).encode("utf-8")).hexdigest()


def _text(name: str, value: Any, *, limit: int | None = None) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{name} must be non-empty text, got {value!r}")
    reason = unstorable_reason(value)
    if reason is not None:
        raise ValueError(f"{name} cannot be stored: {reason}")
    if limit is not None and len(value) > limit:
        raise ValueError(f"{name} is {len(value)} characters; the limit is {limit}")
    return value


def _uuid(name: str, value: Any, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    key = canonical_id(value) if isinstance(value, str) else None
    if key is None:
        raise ValueError(f"{name} must be a canonical UUID as text, got {value!r}")
    return key


@dataclass(frozen=True)
class EvidenceClaim:
    """FR-92: one claim in one ledger. Its sources and labels are its contributions'."""

    claim_id: str
    ledger_run_id: str
    statement: str
    claim_key: str
    supersedes: str | None
    tenant_id: str
    project_id: str

    def __post_init__(self) -> None:
        _uuid("claim_id", self.claim_id)
        _uuid("ledger_run_id", self.ledger_run_id)
        _text("statement", self.statement, limit=MAX_STATEMENT)
        if type(self.claim_key) is not str or not _SHA256.fullmatch(self.claim_key) \
                or self.claim_key != claim_key(self.statement):
            raise ValueError(f"claim_key must be the key of the statement, got {self.claim_key!r}")
        _uuid("supersedes", self.supersedes, optional=True)
        _text("tenant_id", self.tenant_id)
        _text("project_id", self.project_id)

    @property
    def uri(self) -> str:
        return f"{_CLAIM_URN}{self.claim_id}"


@dataclass(frozen=True)
class NewContribution:
    """What a contribution says, before the store gives it an id and a time."""

    run_id: str
    node_id: str | None
    agent_id: str
    supporting: tuple[str, ...]
    contradicting: tuple[str, ...]
    confidence: float | None
    provenance: ContentProvenance
    why: str | None

    def __post_init__(self) -> None:
        _uuid("run_id", self.run_id)
        if self.node_id is not None:
            _text("node_id", self.node_id)
        _text("agent_id", self.agent_id)
        for name in ("supporting", "contradicting"):
            ids = getattr(self, name)
            if not isinstance(ids, tuple) or any(canonical_id(i) is None for i in ids):
                raise ValueError(f"{name} must be a tuple of source version ids, got {ids!r}")
        if self.confidence is not None and (
            isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float))
            or not 0 <= self.confidence <= 1
        ):
            raise ValueError(f"confidence must be a number from 0 to 1, or None, got {self.confidence!r}")
        if not isinstance(self.provenance, ContentProvenance):
            raise ValueError("provenance must be a ContentProvenance")
        if self.why is not None:
            _text("why", self.why, limit=MAX_STATEMENT)


@dataclass(frozen=True)
class ClaimContribution(NewContribution):
    """FR-92: one agent's contribution to one claim, written once."""

    contribution_id: str = ""
    claim_id: str = ""
    recorded_at: datetime | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        _uuid("contribution_id", self.contribution_id)
        _uuid("claim_id", self.claim_id)
        if not isinstance(self.recorded_at, datetime) or self.recorded_at.utcoffset() is None:
            raise ValueError("recorded_at must be a timezone-aware datetime")


@dataclass(frozen=True)
class ClaimView:
    """A claim as read: its contributions, and what is derived from them."""

    claim: EvidenceClaim
    active: bool
    superseded_by: str | None
    contributions: tuple[ClaimContribution, ...]
    supporting: tuple[str, ...]
    contradicting: tuple[str, ...]
    provenance: ContentProvenance


class LedgerStore(Protocol):
    """Where ledgers are kept. Synchronous; callers on a loop use a worker thread
    (FR-81). `record` and `supersede` check and write under a lock on the ledger and
    the statement's key, so concurrent records of one statement are one claim and a
    claim is superseded at most once (DECISION-9de80b77)."""

    def record(self, *, tenant_id: str, project_id: str, ledger_run_id: str, statement: str,
               contribution: NewContribution) -> tuple[EvidenceClaim, ClaimContribution, bool]: ...

    def supersede(self, *, tenant_id: str, project_id: str, ledger_run_id: str, old_claim_id: str,
                  statement: str, contribution: NewContribution) -> tuple[EvidenceClaim, ClaimContribution]: ...

    def rows(self, tenant_id: str, project_id: str,
             ledger_run_id: str) -> tuple[list[EvidenceClaim], list[ClaimContribution]]: ...

    def claim(self, tenant_id: str, project_id: str, ledger_run_id: str,
              claim_id: str) -> tuple[EvidenceClaim, str | None, list[ClaimContribution]]: ...


def _covered(contribution: NewContribution, cited: list[ContentProvenance]) -> NewContribution:
    """M23 review caveat 1: a contribution at least as tainted as every version it cites,
    whoever built it."""
    return replace(contribution, provenance=ContentProvenance.from_model(contribution.provenance, *cited))


def _contribution(new: NewContribution, claim_id: str, at: datetime) -> ClaimContribution:
    return ClaimContribution(**{f: getattr(new, f) for f in NewContribution.__dataclass_fields__},
                             contribution_id=str(uuid.uuid4()), claim_id=claim_id, recorded_at=at)


class InMemoryLedgerStore:
    """FR-92's store for a Runner without persistence: every tenant's ledgers, one lock.

    It checks every citation against `evidence`, the store the versions are in (M23
    review caveat 1); a store given none refuses any citation, having nothing to check
    it against."""

    def __init__(self, evidence: Any = None) -> None:
        self._claims: list[EvidenceClaim] = []
        self._contributions: list[ClaimContribution] = []
        self._lock = threading.Lock()
        self._evidence = evidence

    def _checked(self, tenant_id: str, project_id: str, contribution: NewContribution) -> NewContribution:
        cited = []
        for vid in contribution.supporting + contribution.contradicting:
            try:
                if self._evidence is None:
                    raise LookupError(vid)
                cited.append(self._evidence.get(tenant_id, project_id, vid).provenance)
            except LookupError:
                raise ClaimRefused(f"cited source {_SOURCE_URN}{vid} is not a source version in this tenant "
                                   "and project") from None
        return _covered(contribution, cited)

    def _ledger(self, tenant_id: str, project_id: str, ledger_run_id: str) -> list[EvidenceClaim]:
        return [c for c in self._claims
                if (c.tenant_id, c.project_id, c.ledger_run_id) == (tenant_id, project_id, ledger_run_id)]

    def _active(self, claims: list[EvidenceClaim], key: str) -> EvidenceClaim | None:
        superseded = {c.supersedes for c in claims}
        return next((c for c in claims if c.claim_key == key and c.claim_id not in superseded), None)

    def record(self, *, tenant_id: str, project_id: str, ledger_run_id: str, statement: str,
               contribution: NewContribution) -> tuple[EvidenceClaim, ClaimContribution, bool]:
        key = claim_key(_text("statement", statement, limit=MAX_STATEMENT))
        contribution = self._checked(tenant_id, project_id, contribution)
        with self._lock:
            claim = self._active(self._ledger(tenant_id, project_id, ledger_run_id), key)
            new = claim is None
            if new:
                claim = EvidenceClaim(str(uuid.uuid4()), _uuid("ledger_run_id", ledger_run_id), statement, key,
                                      None, tenant_id, project_id)
                self._claims.append(claim)
            made = _contribution(contribution, claim.claim_id, datetime.now(timezone.utc))
            self._contributions.append(made)
            return claim, made, new

    def supersede(self, *, tenant_id: str, project_id: str, ledger_run_id: str, old_claim_id: str,
                  statement: str, contribution: NewContribution) -> tuple[EvidenceClaim, ClaimContribution]:
        key = claim_key(_text("statement", statement, limit=MAX_STATEMENT))
        contribution = self._checked(tenant_id, project_id, contribution)
        with self._lock:
            claims = self._ledger(tenant_id, project_id, ledger_run_id)
            old = next((c for c in claims if c.claim_id == old_claim_id), None)
            if old is None:
                raise ClaimRefused(f"no claim {_CLAIM_URN}{old_claim_id} in this ledger")
            after = next((c for c in claims if c.supersedes == old.claim_id), None)
            if after is not None:
                raise ClaimRefused(f"claim {old.uri} is already superseded by {after.uri}")
            other = self._active(claims, key)
            if other is not None and other.claim_id != old.claim_id:
                raise ClaimRefused(f"the new statement matches active claim {other.uri}")
            claim = EvidenceClaim(str(uuid.uuid4()), old.ledger_run_id, statement, key, old.claim_id,
                                  tenant_id, project_id)
            self._claims.append(claim)
            made = _contribution(contribution, claim.claim_id, datetime.now(timezone.utc))
            self._contributions.append(made)
            return claim, made

    def rows(self, tenant_id: str, project_id: str,
             ledger_run_id: str) -> tuple[list[EvidenceClaim], list[ClaimContribution]]:
        with self._lock:
            claims = self._ledger(tenant_id, project_id, ledger_run_id)
            ids = {c.claim_id for c in claims}
            return claims, [c for c in self._contributions if c.claim_id in ids]

    def claim(self, tenant_id: str, project_id: str, ledger_run_id: str,
              claim_id: str) -> tuple[EvidenceClaim, str | None, list[ClaimContribution]]:
        with self._lock:
            claims = self._ledger(tenant_id, project_id, ledger_run_id)
            found = next((c for c in claims if c.claim_id == claim_id), None)
            if found is None:
                raise LookupError(f"no claim {claim_id!r} in this ledger")
            after = next((c.claim_id for c in claims if c.supersedes == claim_id), None)
            return found, after, [c for c in self._contributions if c.claim_id == claim_id]


def _union(lists: Any) -> tuple[str, ...]:
    return tuple(dict.fromkeys(i for ids in lists for i in ids))


class EvidenceLedger:
    """FR-94: one ledger, read within its tenant and project. Synchronous."""

    def __init__(self, store: Any, tenant_id: str, project_id: str, ledger_run_id: str) -> None:
        self._store = store
        self.tenant_id, self.project_id, self.ledger_run_id = tenant_id, project_id, ledger_run_id

    def _read(self) -> tuple[dict[str, EvidenceClaim], dict[str, str], list[ClaimContribution]]:
        claims, contributions = self._store.rows(self.tenant_id, self.project_id, self.ledger_run_id)
        by_id = {c.claim_id: c for c in claims}
        superseded_by = {c.supersedes: c.claim_id for c in claims if c.supersedes is not None}
        return by_id, superseded_by, contributions

    @staticmethod
    def _view(claim: EvidenceClaim, superseded_by: dict[str, str], contributions: list[ClaimContribution]) -> ClaimView:
        own = tuple(c for c in contributions if c.claim_id == claim.claim_id)
        return ClaimView(
            claim=claim, active=claim.claim_id not in superseded_by, superseded_by=superseded_by.get(claim.claim_id),
            contributions=own, supporting=_union(c.supporting for c in own),
            contradicting=_union(c.contradicting for c in own),
            provenance=ContentProvenance.from_model(*(c.provenance for c in own)),
        )

    def active(self) -> list[ClaimView]:
        """Every claim not superseded, in the order recorded."""
        by_id, superseded_by, contributions = self._read()
        return [self._view(c, superseded_by, contributions) for c in by_id.values() if c.claim_id not in superseded_by]

    def claim(self, claim_id: str) -> ClaimView:
        """One claim, read on its own (M23 review caveat 5: not the whole ledger)."""
        key = _claim_id(claim_id)
        if key is None:
            raise LookupError(f"no claim {claim_id!r} in this ledger")
        claim, after, contributions = self._store.claim(self.tenant_id, self.project_id, self.ledger_run_id, key)
        return self._view(claim, {} if after is None else {key: after}, contributions)

    def history(self, claim_id: str) -> list[EvidenceClaim]:
        """The supersede chain the claim belongs to, oldest first."""
        by_id, superseded_by, _ = self._read()
        key = _claim_id(claim_id)
        if key not in by_id:
            raise LookupError(f"no claim {claim_id!r} in this ledger")
        while by_id[key].supersedes is not None:
            key = by_id[key].supersedes
        chain = [by_id[key]]
        while chain[-1].claim_id in superseded_by:
            chain.append(by_id[superseded_by[chain[-1].claim_id]])
        return chain


def _claim_id(ref: Any) -> str | None:
    if isinstance(ref, str) and ref.startswith(_CLAIM_URN):
        ref = ref[len(_CLAIM_URN):]
    return canonical_id(ref) if isinstance(ref, str) else None


def _version_id(ref: Any) -> str | None:
    if isinstance(ref, str) and ref.startswith(_SOURCE_URN):
        ref = ref[len(_SOURCE_URN):]
    return canonical_id(ref) if isinstance(ref, str) else None


class RunLedger:
    """One run's way into its top-level run's ledger, built by the Runner for its tools."""

    def __init__(
        self,
        store: Callable[[], Any],
        evidence: Callable[[], Any],
        *,
        tenant_id: str,
        project_id: str,
        ledger_run_id: str,
        run_id: str,
        node_id: str | None,
        agent_id: str,
        call: Callable[..., Awaitable[Any]],
        taken_in: Callable[[], Awaitable[tuple[ContentProvenance, ...]]],
        emit: Callable[[Any, dict[str, Any]], Awaitable[Any]] | None = None,
        lock: Any = None,
    ) -> None:
        self._lock = lock  # FR-95 (M24): the Runner's claim-lock, or None
        self._store, self._evidence, self._call, self._taken_in, self._emit = store, evidence, call, taken_in, emit
        self.tenant_id, self.project_id, self.ledger_run_id = tenant_id, project_id, ledger_run_id
        self.run_id, self.node_id, self.agent_id = run_id, node_id, agent_id

    def reader(self) -> EvidenceLedger:
        return EvidenceLedger(self._store(), self.tenant_id, self.project_id, self.ledger_run_id)

    async def _version(self, vid: str) -> Any:
        return await self._call(self._evidence().get, self.tenant_id, self.project_id, vid)

    async def _cite(self, refs: Any, inputs: tuple[ContentProvenance, ...]) -> list[Any]:
        """Each cited version, which must be in this tenant and project (FR-93): named
        by its uri, or by a URL this run fetched -- the version this run was given for
        it most recently, found among what it has taken in (DECISION-9de80b77's
        addendum: a model sees a page, not its version's uri)."""
        found = []
        for ref in refs:
            vid = _version_id(ref)
            try:
                if vid is not None:
                    found.append(await self._version(vid))
                    continue
                found.append(await self._read(ref, inputs))
            except LookupError:
                raise ClaimRefused(
                    f"cited source {ref!r} is neither a source version in this tenant and project "
                    "nor a URL this run has fetched") from None
        return found

    async def _read(self, url: Any, inputs: tuple[ContentProvenance, ...]) -> Any:
        from .evidence import canonical_uri

        try:
            wanted = canonical_uri(url) if isinstance(url, str) else None
        except ValueError:
            wanted = None
        if wanted is None:
            raise LookupError(url)
        for provenance in reversed(inputs):
            vid = _version_id(provenance.source_uri_or_hash)
            if vid is None:
                continue
            version = await self._version(vid)
            if wanted in (version.canonical_uri, canonical_uri(version.final_uri)):
                return version
        raise LookupError(url)

    async def _new(self, supporting: Any, contradicting: Any, confidence: Any, why: str | None) -> NewContribution:
        inputs = await self._taken_in()
        backing, against = await self._cite(supporting, inputs), await self._cite(contradicting, inputs)
        return NewContribution(
            run_id=self.run_id, node_id=self.node_id, agent_id=self.agent_id,
            supporting=tuple(v.source_version_id for v in backing),
            contradicting=tuple(v.source_version_id for v in against),
            confidence=confidence,
            provenance=ContentProvenance.from_model(*(v.provenance for v in backing + against), *inputs),
            why=why,
        )

    async def record(self, statement: str, supporting: Any = (), contradicting: Any = (),
                     confidence: Any = None) -> tuple[ClaimView, bool, ClaimContribution]:
        """FR-93, under the claim-lock for the statement's key in this ledger (FR-95): a
        record that found the key held waits, then contributes to the claim the holder
        stored."""
        key = claim_key(_text("statement", statement, limit=MAX_STATEMENT))

        async def op(waited: float | None) -> tuple[ClaimView, bool, ClaimContribution]:
            contribution = await self._new(supporting, contradicting, confidence, None)
            claim, made, new = await self._call(
                self._store().record, tenant_id=self.tenant_id, project_id=self.project_id,
                ledger_run_id=self.ledger_run_id, statement=statement, contribution=contribution)
            view = await self._call(self.reader().claim, claim.claim_id)
            if self._emit is not None:
                from .events import EventType

                payload = {"claim_uri": claim.uri, "new": new, "contribution": _described(made),
                           "labels": _labels(view.provenance)}
                if waited is not None:
                    payload["wait"] = {"seconds": waited, "took_result": not new}
                await self._emit(EventType.CLAIM_RECORDED, payload)
            return view, new, made

        if self._lock is None:
            return await op(None)
        return await self._lock.run("|".join(("claim", self.tenant_id, self.project_id, self.ledger_run_id, key)), op)

    async def supersede(self, claim: Any, statement: str, supporting: Any, contradicting: Any,
                        why: str) -> ClaimView:
        old = _claim_id(claim)
        if old is None:
            raise ClaimRefused(f"{claim!r} is not a claim uri")
        contribution = await self._new(supporting, contradicting, None, why)
        claim, _ = await self._call(
            self._store().supersede, tenant_id=self.tenant_id, project_id=self.project_id,
            ledger_run_id=self.ledger_run_id, old_claim_id=old, statement=statement, contribution=contribution)
        view = await self._call(self.reader().claim, claim.claim_id)
        if self._emit is not None:
            from .events import EventType

            await self._emit(EventType.CLAIM_SUPERSEDED, {
                "claim_uri": claim.uri, "supersedes_uri": f"{_CLAIM_URN}{old}", "why": why})
        return view


def _labels(p: ContentProvenance) -> dict[str, Any]:
    return {"origin": p.origin.value, "trust_zone": p.trust_zone.value,
            "instruction_authority": p.instruction_authority.value,
            "taint_flags": sorted(f.value for f in p.taint_flags)}


def _described(c: ClaimContribution) -> dict[str, Any]:
    return {
        "contribution_id": c.contribution_id, "run_id": c.run_id, "node_id": c.node_id, "agent_id": c.agent_id,
        "supporting": [f"{_SOURCE_URN}{v}" for v in c.supporting],
        "contradicting": [f"{_SOURCE_URN}{v}" for v in c.contradicting],
        "confidence": c.confidence, "labels": _labels(c.provenance),
    }


def _report(view: ClaimView, new: bool | None = None) -> ToolOutput:
    body = {"claim": view.claim.uri, "statement": view.claim.statement, "contributions": len(view.contributions),
            "supporting": [f"{_SOURCE_URN}{v}" for v in view.supporting],
            "contradicting": [f"{_SOURCE_URN}{v}" for v in view.contradicting],
            "labels": _labels(view.provenance)}
    if new is not None:
        body["new"] = new
    if view.claim.supersedes is not None:
        body["supersedes"] = f"{_CLAIM_URN}{view.claim.supersedes}"
    # The claim's labels ride on the result (M19's taken_in): a claim drawn from
    # untrusted pages makes an untrusted result.
    return ToolOutput(json.dumps(body), source_uri=view.claim.uri, taken_in=(view.provenance,))


_SOURCES = {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 2048}, "maxItems": 50}
_STATEMENT = {"type": "string", "minLength": 1, "maxLength": MAX_STATEMENT}


def _ledger_of(scope: Any, tool: str) -> RunLedger:
    ledger = getattr(scope, "ledger", None)
    if ledger is None:
        raise ValueError(f"{tool} records into a run's ledger, so it runs only inside a Runner")
    return ledger


def ledger_tools(*, record_name: str = "record_claim", supersede_name: str = "supersede_claim") -> tuple[Tool, Tool]:
    """FR-93, FR-94: the two built-in claim tools. An agent records claims when its
    tool_profile names them (P3-D8)."""
    from .scope import ToolScope  # noqa: F401 - the annotation below names it

    async def record_claim(statement: str, scope: ToolScope = None, supporting: list[str] = (),
                           contradicting: list[str] = (), confidence: float | None = None) -> ToolOutput:
        view, new, _ = await _ledger_of(scope, record_name).record(statement, supporting, contradicting, confidence)
        return _report(view, new)

    async def supersede_claim(claim: str, statement: str, why: str, scope: ToolScope = None,
                              supporting: list[str] = (), contradicting: list[str] = ()) -> ToolOutput:
        view = await _ledger_of(scope, supersede_name).supersede(claim, statement, supporting, contradicting, why)
        return _report(view)

    record = Tool(spec=ToolSpec(
        name=record_name,
        description=("Record a claim in this run's evidence ledger: a statement, the sources that support or "
                     "contradict it -- each a URL you have fetched in this run, or a source version uri "
                     "(urn:agentsdk:source:...) -- and your confidence from 0 to 1. A statement already recorded "
                     "gains your contribution."),
        input_schema={"type": "object", "properties": {
            "statement": _STATEMENT, "supporting": _SOURCES, "contradicting": _SOURCES,
            "confidence": {"type": ["number", "null"], "minimum": 0, "maximum": 1}},
            "required": ["statement"], "additionalProperties": False},
    ), fn=record_claim)
    supersede = Tool(spec=ToolSpec(
        name=supersede_name,
        description=("Replace an active claim (urn:agentsdk:claim:...) in this run's evidence ledger with a "
                     "corrected statement and its sources (URLs you have fetched in this run, or source version "
                     "uris), saying why. A claim can be superseded once."),
        input_schema={"type": "object", "properties": {
            "claim": {"type": "string", "minLength": 1, "maxLength": 200}, "statement": _STATEMENT,
            "supporting": _SOURCES, "contradicting": _SOURCES,
            "why": {"type": "string", "minLength": 1, "maxLength": MAX_STATEMENT}},
            "required": ["claim", "statement", "why"], "additionalProperties": False},
    ), fn=supersede_claim)
    return record, supersede
