"""Native MCP: an agent uses the tools of MCP servers (Phase 4, increment 2).

M33 (FR-123 to FR-125): servers configured in code and connected over stdio, in either era --
2026-07-28's `server/discover` or the legacy `initialize` -- through the `mcp` library's
`Client`. A connection serves one tenant and project on one event loop (P4-D28). Each tool
becomes a `Tool` named `<alias>__<name>`, sent only to agents whose profile names it
(P4-D30), and everything a server returns is untrusted data (NFR-35).

This module imports only the standard library at import time. The `mcp` library, the
`agentsdk[mcp]` extra, is imported when a `Runner` is given servers (NFR-34).
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import pathlib
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import weakref
from collections import Counter
from datetime import datetime, timezone
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..catalog import ToolVersion, changed_parts, checked_hash, tool_entry, tool_hash
from ..errors import ToolBlocked, ToolExecutionError
from ..events import EventType
from ..primitives import InstructionAuthority, Origin, TaintFlag, TrustZone, unstorable_reason
from ..tools import ApprovalPolicy, ResultProvenance, RiskClass, Tool, ToolOutput, ToolRegistry, ToolSpec

__all__ = ["McpAcceptance", "StdioServer", "tool_hash"]

_ALIAS = re.compile(r"[a-z][a-z0-9-]{0,23}")
_QUALIFIED = re.compile(r"[A-Za-z0-9_-]{1,64}")
_APPROVALS = ("require", "never", "by_annotations")
_MAX_TOOLS = 1000
# M33 round 2: every wait on what a server controls is bounded -- a listing by pages as well as
# tools, closing by seconds, and a failed connection is not tried again for a while, so a
# server that never answers costs one connect_timeout a minute, not one a turn.
_MAX_PAGES = 100
_CLOSE_SECONDS = 10.0
# How far past call_timeout the SDK's own deadline sits (M33 rounds 3 and 4).
_WEDGE_MARGIN = 5.0
_RETRY_SECONDS = 60.0
# FR-127 (M34): a listing is read again once it is this old, whatever ttlMs it passed (the owner,
# at round 2: a server does not choose how long its own changed tool keeps running), and a catalog read --
# every store call of one listing, on a worker thread -- is bounded by the other.
_FRESH_SECONDS = 60.0
_STORE_SECONDS = 30.0
# Versions one connection records, at most: a server that changes its tools without end fills
# neither the catalog nor memory; a tool past the cap stays blocked, naming why (pre-review D4).
_MAX_VERSIONS = 10 * _MAX_TOOLS
# NFR-35: whatever a server returns, and whatever `trusted` says.
_UNTRUSTED = ResultProvenance(origin=Origin.MCP_RESOURCE, trust_zone=TrustZone.UNTRUSTED,
                              instruction_authority=InstructionAuthority.DATA_ONLY,
                              taint_flags=frozenset({TaintFlag.EXTERNAL_CONTENT}))
_CONNECTION_CLOSED, _REQUEST_TIMEOUT = -32000, -32001
# The 2026-07-28 era answers server/discover; anything older came through initialize.
_MODERN = "2026-07-28"


@dataclass(frozen=True)
class McpAcceptance:
    """FR-127: tool hashes accepted in code for one tenancy -- recorded, by its service
    principal, when that tenancy's connection first lists each tool. Checked by the server
    config that holds it."""

    tenant_id: str
    project_id: str
    principal: Any
    tool_hashes: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_hashes", tuple(self.tool_hashes))


@dataclass(frozen=True)
class StdioServer:
    """An MCP server the SDK launches and speaks to over stdio (FR-123, P4-D25).

    `env` is added to the `mcp` library's minimal inherited environment; its values reach
    the server and nothing else (NFR-37). `allow`, when given, is the only tools offered,
    then `deny` removes some. `approval` is `require` (the default, P4-D20) or `never`,
    per tool in `approval_overrides`. `call_timeout` bounds each round trip, in seconds,
    and `connect_timeout` the start, the protocol exchange and the whole listing.
    `protocol` is `auto` (P4-D17), `legacy`, or a pinned revision.

    M34 (FR-127, FR-128): `approval` may also be `by_annotations`, for a `trusted` server only;
    `accept` holds an `McpAcceptance` per tenancy, at most one each.
    """

    alias: str
    command: str
    args: tuple[str, ...] = ()
    env: Mapping[str, str] | None = field(default=None, repr=False)  # values never shown (NFR-37)
    cwd: str | None = None
    allow: tuple[str, ...] | None = None
    deny: tuple[str, ...] = ()
    approval: str = "require"
    approval_overrides: Mapping[str, str] = field(default_factory=dict)
    trusted: bool = False
    call_timeout: float = 60.0
    connect_timeout: float = 30.0
    protocol: str = "auto"
    accept: tuple[McpAcceptance, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.alias, str) or not _ALIAS.fullmatch(self.alias):
            raise ValueError(f"alias must match {_ALIAS.pattern}, got {self.alias!r}")
        if not isinstance(self.command, str) or not self.command or unstorable_reason(self.command):
            raise ValueError("command must be non-empty storable text")
        object.__setattr__(self, "args", tuple(self.args))
        object.__setattr__(self, "deny", tuple(self.deny))
        if self.allow is not None:
            object.__setattr__(self, "allow", tuple(self.allow))
        object.__setattr__(self, "env", dict(self.env or {}))
        object.__setattr__(self, "approval_overrides", dict(self.approval_overrides))
        for value in (*self.args, *self.deny, *(self.allow or ())):
            if not isinstance(value, str) or unstorable_reason(value):
                raise ValueError("args, allow and deny must be storable text")
        # M33 review caveat (M34 pre-flight): these name a tool as its server lists it. A qualified
        # name matched nothing, so in deny it failed open -- the tool stayed offered.
        for value in (*self.deny, *(self.allow or ()), *self.approval_overrides):
            if isinstance(value, str) and value.startswith(f"{self.alias}__"):
                raise ValueError(f"allow, deny and approval_overrides name a tool as its server lists it, "
                                 f"without the alias: {value!r}")
        for name, value in self.env.items():
            if not isinstance(name, str) or not isinstance(value, str) or unstorable_reason(name) or unstorable_reason(value):
                raise ValueError(f"env {name!r} must map text to storable text")
        for policy in (self.approval, *self.approval_overrides.values()):
            if policy not in _APPROVALS:
                raise ValueError(f"approval must be one of {', '.join(_APPROVALS)}, got {policy!r}")
            if policy == "by_annotations" and self.trusted is not True:
                raise ValueError("approval by_annotations is for a trusted server only: an untrusted "
                                 "server's annotations change nothing (FR-128)")
        object.__setattr__(self, "accept", tuple(self.accept))
        tenancies = set()
        for item in self.accept:
            if not isinstance(item, McpAcceptance):
                raise ValueError(f"accept holds McpAcceptance values, got {type(item).__name__}")
            if (item.tenant_id, item.project_id) in tenancies:
                raise ValueError(f"accept allows one per tenancy, and names {item.tenant_id}/{item.project_id} "
                                 f"twice")
            tenancies.add((item.tenant_id, item.project_id))
            for name in ("tenant_id", "project_id"):
                value = getattr(item, name)
                if not isinstance(value, str) or not value or unstorable_reason(value):
                    raise ValueError(f"an McpAcceptance's {name} must be non-empty storable text")
            principal = item.principal
            if getattr(principal, "service_principal", None) is None or not hasattr(principal, "to_json"):
                raise ValueError("an McpAcceptance's principal must be a service principal (FR-127)")
            for value in item.tool_hashes:
                try:
                    checked_hash(value)
                except ValueError:
                    raise ValueError(f"an McpAcceptance names tool hashes, each a SHA-256 in hex, got {value!r}") from None
        for name in ("call_timeout", "connect_timeout"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a finite number of seconds over 0, got {value!r}")
        if not isinstance(self.protocol, str) or not self.protocol or unstorable_reason(self.protocol):
            raise ValueError(f"protocol must be auto, legacy or a revision, got {self.protocol!r}")

    def record(self) -> dict[str, Any]:
        """FR-123: what the manifest records -- names, never values (NFR-37)."""
        return {"alias": self.alias, "kind": "stdio", "command": self.command, "args": list(self.args),
                "env": sorted(self.env), "trusted": self.trusted,
                "allow": None if self.allow is None else list(self.allow), "deny": list(self.deny),
                "approval": self.approval, "approval_overrides": dict(sorted(self.approval_overrides.items())),
                "call_timeout": self.call_timeout, "connect_timeout": self.connect_timeout, "protocol": self.protocol}


def _named(profile: tuple[str, ...], alias: str, name: str) -> bool:
    """P4-D30: an MCP tool reaches an agent only when its profile names it."""
    return f"{alias}__{name}" in profile or f"{alias}__*" in profile


def _entry(entry: Any) -> dict[str, Any]:
    """FR-127: a listed tool as the catalog records and hashes it, annotations by wire name."""
    annotations = None if entry.annotations is None else entry.annotations.model_dump(by_alias=True, exclude_none=True)
    return tool_entry(entry.name, entry.description, entry.input_schema, entry.output_schema, annotations)


def _hash_of(entry: dict[str, Any]) -> str:
    return tool_hash(entry["name"], entry["description"], entry["inputSchema"], entry["outputSchema"],
                     entry["annotations"])


def _read_catalog(store: Any, tenant_id: str, project_id: str, alias: str, versions: list[ToolVersion],
                  pending: dict[str, str], accept: McpAcceptance | None) -> tuple[set[str], dict[str, Any]]:
    """FR-127, one catalog read of a listing, on a worker thread: records the versions not yet
    recorded, records this tenancy's acceptance in code for the hashes it names, and answers
    which of `pending` (name -> hash) are accepted, with each blocked tool's last accepted
    version. A failure raises, and the caller leaves the pending tools unavailable (NFR-36)."""
    if versions:
        store.record_versions(versions)
    if not pending:
        return set(), {}
    if accept is not None:
        wanted = [h for h in pending.values() if h in accept.tool_hashes]
        if wanted:
            store.accept(tenant_id, project_id, alias, wanted, accept.principal)
    found = set(store.acceptances(tenant_id, project_id, alias, list(pending.values())))
    blocked = [name for name, h in pending.items() if h not in found]
    return found, (store.last_accepted(tenant_id, project_id, alias, blocked) if blocked else {})


# M33 round 5: no schema a server supplied is ever evaluated in this process. Validating against
# one can take time exponential in what the server chose -- a regular expression (and Python's
# `re` holds the GIL, so no thread contains it), or deep anyOf nesting with none -- so every check
# against a server schema runs in a short-lived process of the real interpreter, ended at this
# deadline. Round 4 isolated only schemas carrying a regular expression, on the premise that the
# rest could not stall; they can. tests/test_mcp_client.py checks the invariant at run time.
_VALIDATION_SECONDS = 10.0
_ISOLATED = """
import json, site, sys
site.addsitedir(sys.argv[1])
import jsonschema
from referencing import Registry


def problem(schema):
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return "its input schema is not a JSON Schema object"
    try:
        jsonschema.validators.validator_for(schema).check_schema(schema)
    except jsonschema.SchemaError as exc:
        return "its input schema is invalid: " + exc.message
    except Exception as exc:
        return "its input schema cannot be read: " + type(exc).__name__
    return None


def output_problem(schema):
    if schema is None:
        return None
    try:
        jsonschema.validators.validator_for(schema).check_schema(schema)
    except jsonschema.SchemaError as exc:
        return "its output schema is invalid: " + exc.message
    except Exception as exc:
        return "its output schema cannot be read: " + type(exc).__name__
    return None


request = json.loads(sys.stdin.read())
if "check" in request:
    outputs = request.get("outputs") or [None] * len(request["check"])
    print(json.dumps({"problems": [problem(schema) or output_problem(output)
                                   for schema, output in zip(request["check"], outputs)]}))
else:
    try:
        cls = jsonschema.validators.validator_for(request["schema"])
        cls.check_schema(request["schema"])
        # An empty registry: a reference resolves only within the schema itself, never to a URL,
        # a file or a share (robustness pre-review).
        error = jsonschema.exceptions.best_match(
            cls(request["schema"], registry=Registry()).iter_errors(request["instance"]))
    except jsonschema.SchemaError as exc:
        print(json.dumps({"schema": exc.message}))
    except Exception as exc:
        print(json.dumps({"schema": type(exc).__name__}))
    else:
        print(json.dumps({"invalid": error.message} if error is not None else {}))
"""


class _Unchecked(Exception):
    """A check that could not run, or did not finish: never a pass."""


def _isolated_or_raise(request: dict[str, Any]) -> dict[str, Any]:
    """One request answered by a process of its own, or `_Unchecked`. Blocking: run it on a worker
    thread. The real interpreter, not a launcher, so the deadline ends the process doing the
    work. A process that fails, or says nothing, fails closed (robustness pre-review)."""
    import jsonschema

    packages = str(pathlib.Path(jsonschema.__file__).resolve().parent.parent)
    # The request is written before the timeout is armed: bounded only because _ISOLATED reads all
    # of its input before evaluating anything. Keep it so (M33 round 5 review, C11).
    try:
        done = subprocess.run([sys._base_executable, "-I", "-c", _ISOLATED, packages], input=json.dumps(request),
                              capture_output=True, text=True, timeout=_VALIDATION_SECONDS)
    except subprocess.TimeoutExpired:
        raise _Unchecked(f"it could not be checked within {_VALIDATION_SECONDS} s") from None
    except (OSError, ValueError, TypeError) as exc:
        raise _Unchecked(f"it could not be checked: {type(exc).__name__}") from None
    try:
        verdict = json.loads(done.stdout) if done.returncode == 0 and done.stdout.strip() else None
    except ValueError:
        verdict = None
    if not isinstance(verdict, dict):
        raise _Unchecked("it could not be checked: the checking process gave no verdict")
    return verdict


# Listed tools per checking process (M33 round 6): one process for a whole listing refused an
# honest server of about 45k properties at the deadline. The chunks run side by side within it.
_CHECK_CHUNK = 50


def _check_chunk(schemas: list[Any], outputs: list[Any] | None = None) -> list[str | None]:
    """One problem, or None, per input schema and its output schema, if any, checked in a process
    of its own. A verdict that does not answer every schema, with text or None, is no verdict
    (robustness pass 2), and a chunk with no verdict has every one of its schemas refused: never a
    pass. Blocking."""
    try:
        problems = _isolated_or_raise({"check": schemas, "outputs": outputs}).get("problems")
        if not isinstance(problems, list) or len(problems) != len(schemas) or not all(
                problem is None or isinstance(problem, str) for problem in problems):
            raise _Unchecked("it could not be checked: the verdict did not answer every schema")
    except _Unchecked as exc:
        return [f"its input schema: {exc}"] * len(schemas)
    return problems


def _check_listing(schemas: list[Any], outputs: list[Any] | None = None) -> list[str | None]:
    """One problem, or None, per listed tool -- its input schema, then its output schema, which
    round 6 checks here too, so a tool is never offered only for every call of it to fail -- in
    order. Blocking: each chunk's process ends by the validation deadline, so all of them do, side
    by side."""
    outputs = [None] * len(schemas) if outputs is None else outputs
    starts = range(0, len(schemas), _CHECK_CHUNK)
    chunks = [(schemas[s:s + _CHECK_CHUNK], outputs[s:s + _CHECK_CHUNK]) for s in starts]
    if len(chunks) <= 1:
        return _check_chunk(*chunks[0]) if chunks else []
    with concurrent.futures.ThreadPoolExecutor(len(chunks), thread_name_prefix="agentsdk-mcp-check") as pool:
        return [problem for chunk in pool.map(lambda pair: _check_chunk(*pair), chunks) for problem in chunk]


def _schema_problem(schema: Any) -> str | None:
    """Why a server's input schema is refused, or None; checked in a process of its own."""
    return _check_chunk([schema])[0]


def validate_untrusted(instance: Any, schema: Any) -> tuple[str | None, str | None]:
    """(why the instance is invalid, why the schema is) -- each None when it is not -- against a
    schema a server supplied, checked in a process of its own. Blocking: run it on a worker
    thread. Never (None, None) unless the check ran and passed."""
    try:
        verdict = _isolated_or_raise({"instance": instance, "schema": schema})
    except _Unchecked as exc:
        return str(exc), None
    return verdict.get("invalid"), verdict.get("schema")


def _block(block: Any) -> str:
    """One content block as text: images and blobs are named, never inlined (FR-125)."""
    kind = getattr(block, "type", "content")
    if kind == "text":
        return block.text
    if kind in ("image", "audio"):
        return f"[{kind} content omitted: {block.mime_type}, {_size(block.data)}]"
    if kind == "resource":
        resource = block.resource
        if getattr(resource, "text", None) is not None:
            return resource.text
        return f"[resource content omitted: {resource.mime_type}, {_size(resource.blob)}]"
    return f"[{kind} content omitted]"


def _size(data: str) -> str:
    """A block's decoded size; data that does not decode is named so, never failing the result
    (round 6 pre-review)."""
    try:
        return f"{len(base64.b64decode(data))} bytes"
    except ValueError:  # binascii.Error included
        return "data that is not valid base64"


class _InvalidOutput(RuntimeError):
    """A result whose structured content its tool's output schema refuses, or could not check."""


def _guard_output_validation(client: Any) -> None:
    """Left alone, the library would check a result's structured content against the tool's output
    schema itself, on this loop. That is a server's schema, so the SDK replaces the check with its
    own, in a process of its own, every time, null content included. Pinned with mcp==2.3.0."""
    session = client.session

    async def validate_tool_result(name: str, result: Any) -> None:
        schema = session._tool_output_schemas.get(name)
        if schema is None:
            return  # nothing declared, nothing to check
        if result.structured_content is None and "structured_content" not in result.model_fields_set:
            # Declared and not returned: the library refuses this, and so does the SDK, whatever
            # the schema would accept (robustness pass 2).
            raise _InvalidOutput(f"Invalid structured content returned by tool {name}: none was returned")
        # A null result is checked like any other: the library validated `None` here, in this
        # process, against the server's schema (robustness pre-review).
        invalid, unreadable = await asyncio.to_thread(validate_untrusted, result.structured_content, schema)
        if invalid is not None or unreadable is not None:
            raise _InvalidOutput(f"Invalid structured content returned by tool {name}: {invalid or unreadable}")

    session.validate_tool_result = validate_tool_result


class _Connection:
    """One server for one tenant and project on one loop (P4-D28). The `mcp` Client lives
    in its own task, since its task groups must be left in the task that entered them.

    M33 round 4: its whole life goes through one lock. `ensure` is the only way a server is
    ever started, and `close` makes it final under the same lock: once closed, no holder of
    this object -- a turn's refresh, a tool from an earlier turn, anything -- starts a server
    again. A session is the pair (client, task) one start made; whatever ends a session ends
    that one, never a newer one.
    """

    def __init__(self, server: StdioServer, registered: frozenset[str], submit: Any,
                 tenancy: tuple[str, str] = ("", ""), catalog: Any = None, clock: Any = time.monotonic) -> None:
        self.server, self._registered, self._submit = server, registered, submit
        self.client: Any = None
        self.tools: dict[str, Tool] = {}
        # FR-127 (M34): the catalog of this connection's tenancy. `usable` maps each listed tool
        # to its hash while that hash is accepted, and is replaced whole, never edited, so a
        # reader on another loop sees one listing's answer or the next one's.
        self._tenancy, self._catalog, self._clock = tenancy, catalog, clock
        self._accept = next((a for a in server.accept if (a.tenant_id, a.project_id) == tenancy), None)
        self.usable: dict[str, str] = {}
        self._entries: dict[str, dict[str, Any]] = {}
        self._recorded: set[str] = set()
        self._blocked: dict[tuple[str, str], dict[str, Any]] = {}  # (name, hash) -> McpToolBlocked
        self._listed_at: float | None = None
        self._ttl: float | None = None
        self._stale = False  # a list-changed notification since the last read
        self._changes = 0  # notifications seen: a read that saw none arrive during it clears _stale
        self._listener: asyncio.Task | None = None
        self._skips: set[tuple[str, str]] = set()
        # Each schema pair's verdict, by its canonical JSON: a listing read again unchanged -- each
        # turn, for a server whose ttlMs is 0 -- starts no checking process (M34).
        self._verdicts: dict[str, str | None] = {}
        self._unmatched: list[str] = []
        self.era: str | None = None
        self.starts = 0
        self.dead = False  # restarted once already, and exited again (FR-124)
        self.closed = False  # its Runner closed it: final (NFR-39)
        self._failed_at: float | None = None
        self._why = "NotStarted"  # the cause it is down, as MCP_SERVER_UNAVAILABLE names it
        # Events of a start a call made: no run is listening then, so the next ensure reports them.
        self._unreported: list[tuple[EventType, dict[str, Any]]] = []
        self._task: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None
        self._lock = asyncio.Lock()

    async def ensure(self) -> list[tuple[EventType, dict[str, Any]]]:
        """Connected, or a reason it is not: the events to emit on the run that asked, those of a
        start a call made first."""
        events = await self._ensure()
        events, self._unreported = self._unreported + events, []
        # M33 round 6, FR-124: whatever keeps a server down -- a failure just now, the retry
        # interval after one, the restart-once rule -- a run that asks for it is told. Asserted
        # of the outcome, not of each path to it; the run's view reports it once a run.
        if self.client is None and not self.closed and not any(
                kind is EventType.MCP_SERVER_UNAVAILABLE for kind, _ in events):
            events.append((EventType.MCP_SERVER_UNAVAILABLE, {"alias": self.server.alias, "error": self._why}))
        return events

    async def _ensure(self) -> list[tuple[EventType, dict[str, Any]]]:
        """The only place a server is started. A server already connected has its listing read
        again when it is stale (FR-127), and its catalog read for the tools not yet usable."""
        async with self._lock:
            if self.closed or self.dead:
                return []
            if self.client is not None:
                events = await self._reread() if self._is_stale() else []
                return events + (await self._catalogue() if self.client is not None else [])
            if self.starts >= 2:  # FR-124: restarted once in its life already
                self.dead, self._why = True, "ServerExited"
                return []
            loop = asyncio.get_running_loop()
            if self._failed_at is not None and loop.time() - self._failed_at < _RETRY_SECONDS:
                return []
            seen = self._changes
            try:
                listed, ttl = await self._open()
                if self.closed:  # closed while it started: end it, report nothing
                    await self._discard(self.client, self._task)
                    return []
                events = [(EventType.MCP_SERVER_CONNECTED, {
                    "alias": self.server.alias, "era": self.era,
                    "server": {"name": getattr(self._info, "name", None),
                               "version": getattr(self._info, "version", None)}})]
                events += self._list(listed, await self._schema_problems(listed))
                events[0][1]["unmatched_overrides"] = list(self._unmatched)
                self._read_at(ttl, seen)
                events += await self._catalogue()
            except BaseException as exc:  # noqa: BLE001 - any exit, a cancellation included (robustness pass 2)
                await self._discard(self.client, self._task)
                if not isinstance(exc, Exception):
                    raise  # cancelled: the session it began is ended, and the cancellation goes on
                self._failed_at = loop.time()
                while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
                    exc = exc.exceptions[0]  # the cause, not the task group that carried it
                self._why = type(exc).__name__
                return [(EventType.MCP_SERVER_UNAVAILABLE, {"alias": self.server.alias, "error": self._why})]
            self._failed_at = None
            self.starts += 1
            return events

    def _is_stale(self) -> bool:
        """FR-127: a notification since the last read, a passed ttlMs, or a listing older than 60
        seconds, whatever its ttlMs -- by the catalog's clock, which tests replace."""
        if self._stale or self._listed_at is None:
            return True
        kept = self._ttl if self._ttl is not None and self._ttl < _FRESH_SECONDS else _FRESH_SECONDS
        return self._clock() - self._listed_at >= kept

    def _read_at(self, ttl: float | None, seen: int) -> None:
        """A read that began when `seen` notifications had arrived: still stale if any came during
        it -- so a notification is never lost to the read it raced (pre-review D2)."""
        self._listed_at, self._ttl, self._stale = self._clock(), ttl, self._changes != seen

    async def _reread(self) -> list[tuple[EventType, dict[str, Any]]]:
        """Reads the listing again on the session there is, under the lock. A listing that cannot
        be read ends that session: its tools are absent until the server starts again."""
        client, task, seen = self.client, self._task, self._changes
        try:
            listed, ttl = await asyncio.wait_for(_read_listing(client), self.server.connect_timeout)
            problems = await self._schema_problems(listed)
        except Exception as exc:  # noqa: BLE001 - unreadable: the session is ended, fail closed
            await self._discard(client, task)
            self._why = type(exc).__name__
            return [(EventType.MCP_SERVER_UNAVAILABLE, {"alias": self.server.alias, "error": self._why})]
        events = self._list(listed, problems)
        self._read_at(ttl, seen)
        if self.era == _MODERN and (self._listener is None or self._listener.done()):
            self._listen(client, task)  # FR-127: reopened at the next read if it closed
        return events

    def _changed(self) -> None:
        """A list-changed notification: the catalog is read now, in the background, and its
        events reported at the next ensure (FR-127)."""
        self._changes += 1
        self._stale = True
        if not self.closed and self.client is not None:
            asyncio.get_running_loop().create_task(self._read_soon())

    async def _read_soon(self) -> None:
        async with self._lock:
            if self.closed or self.client is None or not self._stale:
                return
            self._unreported += await self._reread()
            if self.client is not None:
                await self._catalogue()  # its blocked tools are reported at every ensure

    def _listen(self, client: Any, task: asyncio.Task | None) -> None:
        """FR-127, the 2026-07-28 era: a `subscriptions/listen` stream for tool list changes,
        living no longer than its session, which cancels it as it ends."""

        async def listen() -> None:
            try:
                async with client.listen(tools_list_changed=True) as subscription:
                    async for _ in subscription:
                        self._changed()
            except Exception:  # noqa: BLE001 - not offered, or lost: nothing is known any more
                pass
            if self._task is task:
                self._changes += 1  # events may have been missed: the next read is a fresh one
                self._stale = True

        self._listener = asyncio.get_running_loop().create_task(listen())

    async def _catalogue(self) -> list[tuple[EventType, dict[str, Any]]]:
        """FR-127: which listed tools are usable -- their current hash accepted for this
        tenancy -- and McpToolBlocked for each that is not, every time, so each run is told.
        A catalog that cannot be read leaves every tool not already usable unavailable (NFR-36)."""
        tenant_id, project_id = self._tenancy
        alias, now = self.server.alias, datetime.now(timezone.utc)
        hashes = {name: tool.spec.configuration["tool_hash"] for name, tool in self.tools.items()}
        versions = [ToolVersion(tenant_id=tenant_id, project_id=project_id, alias=alias, tool_name=name,
                                tool_hash=h, entry=self._entries[name], first_seen_at=now)
                    for name, h in hashes.items() if h not in self._recorded]
        room = max(0, _MAX_VERSIONS - len(self._recorded))
        over = {v.tool_name for v in versions[room:]}  # past the cap: not recorded, so not usable
        versions = versions[:room]
        pending = {name: h for name, h in hashes.items() if self.usable.get(name) != h and name not in over}
        error = None
        try:
            found, previous = await asyncio.wait_for(asyncio.to_thread(
                _read_catalog, self._catalog(), tenant_id, project_id, alias, versions, pending, self._accept),
                _STORE_SECONDS)
            self._recorded |= {v.tool_hash for v in versions}
        except Exception as exc:  # noqa: BLE001 - the store fails closed
            found, previous, error = set(), {}, type(exc).__name__
        self.usable = {name: h for name, h in hashes.items()
                       if name not in over and (self.usable.get(name) == h or h in found)}
        self._blocked = {key: value for key, value in self._blocked.items() if hashes.get(key[0]) == key[1]}
        events = []
        for name, h in hashes.items():
            if name in self.usable:
                continue
            if name in over:
                events.append((EventType.MCP_TOOL_BLOCKED, {
                    "alias": alias, "tool": name, "tool_hash": h, "previous_hash": None, "changed": None,
                    "error": f"VersionLimit: this connection has recorded {_MAX_VERSIONS} versions"}))
                continue
            payload = self._blocked.get((name, h))
            if payload is None or error is not None:
                before = previous.get(name)
                payload = {"alias": alias, "tool": name, "tool_hash": h,
                           "previous_hash": None if before is None else before.tool_hash,
                           "changed": None if before is None else changed_parts(before.entry, self._entries[name])}
                if error is not None:
                    payload["error"] = error
                else:
                    self._blocked[(name, h)] = payload
            events.append((EventType.MCP_TOOL_BLOCKED, dict(payload)))
        return events

    async def current(self, name: str) -> tuple[Tool | None, str | None]:
        """FR-127, NFR-36: the tool a call under `name` may run now -- the connection's current
        tool object, while its hash is accepted -- and, when there is none because the server is
        down, why. A session that ended is started again first (the restart-once rule) and a
        stale listing read again, so a call is validated and asked about only against what the
        server lists now (round 1 review). What executor step 1 checks, and what the call checks
        again as it leaves (pre-review D1)."""
        self._unreported += [e for e in await self._ensure() if e[0] is not EventType.MCP_TOOL_BLOCKED]
        if self.closed or self.client is None:
            return None, ("closed with its Runner" if self.closed else "not running")
        tool = self.tools.get(name)
        if tool is None or self.usable.get(name) != tool.spec.configuration["tool_hash"]:
            return None, None
        return tool, None

    def _on_message(self, message: Any) -> Any:
        """The library's message handler: a legacy-era tools list-changed notification."""
        from mcp.types import ToolListChangedNotification

        async def handle() -> None:
            if isinstance(message, ToolListChangedNotification):
                self._changed()

        return handle()

    async def _open(self) -> tuple[list[Any], float | None]:
        from mcp import Client, StdioServerParameters
        from mcp.client.stdio import stdio_client

        server, ready = self.server, asyncio.get_running_loop().create_future()
        stop = asyncio.Event()

        async def serve() -> None:
            # Its stderr is dropped: never an error, never content, never logged (FR-124).
            with open(os.devnull, "w") as devnull:
                try:
                    params = StdioServerParameters(command=server.command, args=list(server.args),
                                                   env=dict(server.env) or None, cwd=server.cwd)
                    # No response cache: the SDK decides when a listing is read again (FR-127).
                    async with Client(stdio_client(params, errlog=devnull), mode=server.protocol, cache=None,
                                      message_handler=self._on_message) as client:
                        _guard_output_validation(client)
                        listed, ttl = await _read_listing(client)
                        if not ready.done():
                            ready.set_result((client, listed, ttl))
                        try:
                            await stop.wait()
                        finally:
                            listener = self._listener
                            if listener is not None and not listener.done() and self._task is asyncio.current_task():
                                listener.cancel()
                                await asyncio.wait([listener], timeout=_CLOSE_SECONDS)
                except BaseException as exc:
                    if not ready.done():
                        ready.set_exception(exc if isinstance(exc, Exception) else ConnectionError("cancelled"))
                    if not isinstance(exc, Exception):
                        raise
                finally:
                    # Only this session's client: a newer session is not this task's to end.
                    if self._task is asyncio.current_task():
                        self.client = None

        task = asyncio.create_task(serve())
        self._task, self._stop = task, stop
        try:
            client, listed, ttl = await asyncio.wait_for(ready, server.connect_timeout)
        except BaseException:
            await _ended(task)
            raise
        self.client, self.era = client, _MODERN if client.protocol_version == _MODERN else client.protocol_version
        self._info = client.server_info
        if self.era == _MODERN:
            self._listen(client, task)
        return listed, ttl

    async def _schema_problems(self, listed: list[Any]) -> list[str | None]:
        """Each listed tool's input-schema problem, checked in processes of their own; a chunk past
        the deadline has its tools skipped, and the server's other tools stay. A pair of schemas
        already judged by this connection keeps its verdict; one that could not be checked is
        checked again at the next read."""
        entries = listed[:_MAX_TOOLS]
        keys = [_canonical([entry.input_schema, entry.output_schema]) for entry in entries]
        unknown = [index for index, key in enumerate(keys) if key is None or key not in self._verdicts]
        if unknown:
            found = await asyncio.to_thread(_check_listing, [entries[i].input_schema for i in unknown],
                                            [entries[i].output_schema for i in unknown])
            fresh = dict(zip(unknown, found))
            if len(self._verdicts) > 4 * _MAX_TOOLS:
                self._verdicts = {}  # kept for a listing read again unchanged, not for ever (D4)
            for index, problem in fresh.items():
                if keys[index] is not None and not (problem or "").startswith("its input schema: it could not"):
                    self._verdicts[keys[index]] = problem
        else:
            fresh = {}
        return [fresh[i] if i in fresh else self._verdicts[keys[i]] for i in range(len(entries))]

    def _list(self, listed: list[Any], problems: list[str | None]) -> list[tuple[EventType, dict[str, Any]]]:
        alias, events = self.server.alias, []
        counts = Counter(entry.name for entry in listed)
        # The owner's choice (M34): an override naming no listed tool -- a typo, or a tool since
        # removed -- makes every tool of this server ask, so it can never leave one unapproved.
        self._unmatched = sorted(set(self.server.approval_overrides) - set(counts))
        tools, entries = {}, {}
        for index, entry in enumerate(listed):
            qualified = f"{alias}__{entry.name}"
            if index >= _MAX_TOOLS:
                reason = f"over {_MAX_TOOLS} tools"
            elif not entry.name:
                reason = "it has no name"
            elif not _QUALIFIED.fullmatch(qualified):
                reason = "its qualified name is not 1 to 64 of A-Z a-z 0-9 _ -"
            elif counts[entry.name] > 1:
                reason = "the listing names it more than once"
            elif self.server.allow is not None and entry.name not in self.server.allow:
                reason = "not in allow"
            elif entry.name in self.server.deny:
                reason = "in deny"
            elif qualified in self._registered:
                reason = "a registered tool has its name"
            elif (unstorable := unstorable_reason({"description": entry.description, "input_schema": entry.input_schema,
                                                  "output_schema": entry.output_schema})) is not None:
                # Everything the SDK holds of a tool reaches a model request or a record: a NUL or a
                # lone surrogate in it would fail the run, not the tool (M33 round 6).
                reason = f"it cannot be stored: {unstorable}"
            else:
                reason = problems[index]
            if reason is None:
                try:
                    entries[entry.name] = _entry(entry)
                    tool = self._tool(entry, entries[entry.name])
                    kept = self.tools.get(entry.name)
                    # The object a turn was offered stays the tool while nothing of its spec has
                    # changed -- hash, approval policy, risk; any change makes it another (D3).
                    tools[entry.name] = kept if kept is not None and kept.spec == tool.spec else tool
                except Exception as exc:  # noqa: BLE001 - one entry the SDK cannot hold (a NUL, say): that tool alone is skipped
                    reason = f"it cannot be offered as a tool: {type(exc).__name__}"
            if reason is not None and (entry.name, reason) not in self._skips:
                self._skips.add((entry.name, reason))  # each skip reported once, not at every read
                # The reason may quote the server's schema: cut, as its name is (round 6 pre-review).
                events.append((EventType.MCP_TOOL_SKIPPED, {"alias": alias, "tool": entry.name[:200], "reason": reason[:500]}))
        self.tools, self._entries = tools, entries
        self._skips = {skip for skip in self._skips if skip[0] in counts}  # this listing's (D4)
        return events

    def _tool(self, entry: Any, recorded: dict[str, Any]) -> Tool:
        server, name = self.server, entry.name
        policy = "require" if self._unmatched else server.approval_overrides.get(name, server.approval)
        # FR-128: a trusted server's annotations set the spec; an untrusted server's are recorded
        # in the catalog and change nothing.
        hints = recorded["annotations"] if server.trusted else None
        hints = hints or {}
        read_only = hints.get("readOnlyHint") is True
        risk = RiskClass.READ_ONLY if read_only else (
            RiskClass.WRITE if hints.get("destructiveHint") is False else RiskClass.DESTRUCTIVE)
        ask = policy == "require" or (policy == "by_annotations" and not read_only)
        digest = _hash_of(recorded)
        spec = ToolSpec(
            name=f"{server.alias}__{name}", description=entry.description or "", input_schema=entry.input_schema,
            risk_class=risk, read_only=read_only, idempotent=hints.get("idempotentHint") is True,
            concurrency_safe=read_only,
            approval_policy=ApprovalPolicy.REQUIRE_APPROVAL if ask else ApprovalPolicy.AUTO,
            # The client times each round trip; the executor sets no deadline (FR-125).
            timeout_seconds=None,
            result_provenance=_UNTRUSTED,
            configuration={"mcp_server": server.alias, "tool_hash": digest},
        )

        async def call(**arguments: Any) -> ToolOutput:
            # Called on the run's loop; the call itself runs on the Runner's MCP loop.
            return await self._submit(self._call(name, arguments, entry.output_schema, tool), _call_bound(server))

        tool = Tool(spec=spec, fn=call)
        return tool

    async def _call(self, name: str, arguments: dict[str, Any], output_schema: Any, tool: Tool) -> ToolOutput:
        from mcp import MCPError

        alias = self.server.alias
        if self.client is None:
            self._unreported += await self._ensure()
        if self.closed:
            raise ToolExecutionError(f"mcp server {alias} was closed with its Runner")
        # NFR-36, asserted where a call leaves for the server, whatever it waited on since step 1
        # -- an approval, a slot: it runs only as the tool the connection offers now, under a
        # listing read again first if it is stale (pre-review D1). A call in flight finishes
        # against the version it was sent under.
        offered, down = await self.current(name)
        if down is not None:
            raise ToolExecutionError(f"mcp server {alias} is {down}")
        if offered is None:
            raise ToolBlocked(f"{alias}__{name} is not accepted in its current version")
        if offered is not tool:
            raise ToolBlocked(f"{alias}__{name} changed since it was offered; it is offered again next turn")
        client, task = self.client, self._task  # this call's session
        if client is None:
            raise ToolExecutionError(f"mcp server {alias} exited")
        timeout = float(self.server.call_timeout)
        clock = asyncio.get_running_loop()
        started = clock.time()

        async def wedged() -> bool:
            # Reaching the SDK's own deadline means the request was never taken -- whatever the
            # library then raised: it reports our cancellation as its own request timeout. The
            # stream may hold half a message, so this session is ended, and the next call starts
            # the server again under the restart-once rule. An honest timeout comes at
            # call_timeout, well before (M33 round 4).
            if clock.time() - started < timeout + _WEDGE_MARGIN - 0.5:
                return False
            await self._discard(client, task)
            return True

        try:
            # The SDK owns every wait on a server: the library arms its read timeout only once the
            # request is written, and a server that stops reading its input blocks the write. The
            # margin leaves the library's own timeout to decide a slow, honest tool; this loop is
            # the Runner's own, so a stalled run's loop does not eat into it.
            result = await asyncio.wait_for(client.call_tool(name, arguments, read_timeout_seconds=timeout),
                                            timeout + _WEDGE_MARGIN)
        except TimeoutError:
            await wedged()
            raise TimeoutError(f"mcp server {alias} did not take or answer the request within "
                               f"{timeout + _WEDGE_MARGIN} s") from None
        except MCPError as exc:
            if exc.code == _REQUEST_TIMEOUT:
                # The executor reports a TimeoutError as ToolTimeout, as for any tool.
                if await wedged():
                    raise TimeoutError(f"mcp server {alias} did not take the request within "
                                       f"{timeout + _WEDGE_MARGIN} s") from None
                raise TimeoutError(f"mcp server {alias} did not answer within {timeout} s") from None
            if exc.code == _CONNECTION_CLOSED:
                await self._discard(client, task)
                raise ToolExecutionError(f"mcp server {alias} exited") from None
            raise ToolExecutionError(f"mcp server {alias} answered error {exc.code}: {exc.message}") from None
        except _InvalidOutput:
            # The SDK's own check of structured content, which replaced the library's.
            raise ToolExecutionError("output does not match its schema") from None
        text = "\n".join(_block(block) for block in result.content)
        if result.is_error:
            raise ToolExecutionError(text or "the tool reported an error")
        # ponytail: structured content joins the text only when no text block carries it, as
        # the spec asks a server to serialize it into one; any other block stays named beside it.
        if output_schema is not None and not any(getattr(b, "type", None) == "text" for b in result.content):
            text = "\n".join([*([text] if text else []), json.dumps(result.structured_content)])
        return ToolOutput(content=text, source_uri=f"mcp://{alias}/{name}")

    async def _discard(self, client: Any, task: asyncio.Task | None) -> None:
        """Ends one session: unsets its client if it is still the current one, and ends its task."""
        if self.client is client:
            self.client = None
        await _ended(task)

    async def close(self) -> None:
        """Final: under the lock every start takes, so a start in progress finishes first and is
        then ended, and nothing starts after. Bounded by connect_timeout and the close bound."""
        self.closed = True
        async with self._lock:
            self.closed = True
            if self._stop is not None:
                self._stop.set()
            task = self._task
            if task is not None and not task.done():
                await asyncio.wait([task], timeout=_CLOSE_SECONDS)
            await _ended(task)
            self.client = None


def _canonical(value: Any) -> str | None:
    """Canonical JSON text, or None for a value that has none."""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return None


async def _read_listing(client: Any) -> tuple[list[Any], float | None]:
    """Every page of a listing, within _MAX_PAGES and _MAX_TOOLS, and its ttl in seconds -- None
    when the server sent no ttlMs (the library would default it to 0, immediately stale)."""
    listed, cursor, ttl = [], None, None
    for _ in range(_MAX_PAGES):
        page = await client.list_tools(cursor=cursor)
        listed += page.tools
        if "ttl_ms" in page.model_fields_set:
            ttl = page.ttl_ms / 1000 if ttl is None else min(ttl, page.ttl_ms / 1000)
        if not (cursor := page.next_cursor) or len(listed) > _MAX_TOOLS:
            return listed, ttl
    raise ValueError(f"the listing ran past {_MAX_PAGES} pages")


async def _ended(task: asyncio.Task | None) -> None:
    """Cancels a session's task -- its Client then ends the server -- within the close bound."""
    if task is not None and not task.done():
        task.cancel()
        await asyncio.wait([task], timeout=_CLOSE_SECONDS)


def _ensure_bound(server: StdioServer) -> float:
    """How long a start may take, every inner bound included: the caller's own deadline."""
    return 2 * server.connect_timeout + 2 * _VALIDATION_SECONDS + 2 * _STORE_SECONDS + 3 * _CLOSE_SECONDS + 5


def _call_bound(server: StdioServer) -> float:
    """How long a call may take, a restart and its output check included."""
    return _ensure_bound(server) + server.call_timeout + _WEDGE_MARGIN + _VALIDATION_SECONDS + 2 * _CLOSE_SECONDS


class _Closed(ToolExecutionError):
    """The Runner was closed: nothing is submitted to its MCP loop again."""


class _Engine:
    """The one event loop, on a thread of its own, that every MCP connection of a Runner lives on
    (M33 round 4). Runs may be on any loop; their calls cross into this one. So a server never
    depends on a caller's loop staying alive, running or idle, and closing is one act."""

    def __init__(self) -> None:
        # Decides "closed" against every submission. Reentrant: the finalizer of a collected
        # Runner calls close(), and a collection can happen on a thread inside submit().
        self._lock = threading.RLock()
        self.loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self.closed = False
        # Resolved only by the close itself, once every server has ended and every task on the
        # loop has finished; a caller awaits it shielded, so cancelling one wait cancels nothing
        # else (pre-review 2, defect 1).
        self._done: concurrent.futures.Future | None = None
        # Touched only on self.loop.
        self.connections: dict[tuple[str, str, str], _Connection] = {}
        self.servers: dict[str, StdioServer] = {}
        self.registered: frozenset[str] = frozenset()
        # FR-127 (M34): where the catalog is kept, and the clock it reads -- replaced by tests.
        self.catalog: Any = None
        self.clock: Any = time.monotonic

    def connection(self, alias: str, tenant_id: str, project_id: str) -> _Connection:
        """On self.loop only."""
        key = (alias, tenant_id, project_id)
        if key not in self.connections:
            self.connections[key] = _Connection(self.servers[alias], self.registered, self.submit,
                                                (tenant_id, project_id), self.catalog, lambda: self.clock())
        return self.connections[key]

    async def submit(self, coroutine: Any, timeout: float) -> Any:
        """Runs a coroutine on the MCP loop and awaits it from the caller's loop, for at most
        `timeout` seconds: every bound inside lives on the MCP loop, so the caller holds one of its
        own, and a stalled loop never holds a run (robustness pre-review)."""
        with self._lock:
            if self.closed:
                coroutine.close()
                raise _Closed("mcp servers were closed with their Runner")
            if self.loop is None:
                self.loop = asyncio.new_event_loop()
                self._thread = threading.Thread(target=_serve_forever, args=(self.loop, self), name="agentsdk-mcp",
                                                daemon=True)
                self._thread.start()
            future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        # asyncio.wait, not wait_for: a TimeoutError the call itself raised (an honest timeout at
        # call_timeout) must reach the caller as itself, not read as this deadline passing.
        wrapped = asyncio.wrap_future(future)
        try:
            done, _ = await asyncio.wait({wrapped}, timeout=timeout)
        except asyncio.CancelledError:
            wrapped.cancel()  # the caller was cancelled: so is its work on the MCP loop
            raise
        if not done:
            wrapped.cancel()
            raise _Closed(f"the mcp loop did not answer within {timeout} s")
        try:
            return wrapped.result()
        except asyncio.CancelledError:
            # Cancelled by the close draining the loop, not by this caller: a closed Runner,
            # reported as such, never a cancellation of the caller's run.
            raise _Closed("mcp servers were closed with their Runner") from None

    def close(self) -> concurrent.futures.Future:
        """Idempotent: starts the one close, and returns the future it alone resolves. Nobody
        holds the close's own task, so no caller can cancel it."""
        with self._lock:
            if self._done is None:
                self.closed = True
                self._done = concurrent.futures.Future()
                if self.loop is None:
                    self._done.set_result(None)
                else:
                    asyncio.run_coroutine_threadsafe(self._close_all(), self.loop)
            return self._done

    async def _close_all(self) -> None:
        """On self.loop. Nothing new is submitted now, so these are all the connections there are.
        Every task left on the loop is then ended, so every submitted future resolves before the
        loop stops and no caller waits on a stopped loop."""
        try:
            connections = list(self.connections.values())
            for connection in connections:
                connection.closed = True
            await asyncio.gather(*(connection.close() for connection in connections), return_exceptions=True)
            here = asyncio.current_task()
            for _ in range(5):  # a submission accepted just before "closed" was set may still be starting
                await asyncio.sleep(0)
                rest = [task for task in asyncio.all_tasks() if task is not here]
                if not rest:
                    break
                for task in rest:
                    task.cancel()
                await asyncio.wait(rest, timeout=_CLOSE_SECONDS)
        finally:
            # The close is resolved by the thread, once the loop is closed (round 5 review, C8).
            asyncio.get_running_loop().call_soon(asyncio.get_running_loop().stop)


def _serve_forever(loop: asyncio.AbstractEventLoop, engine: _Engine) -> None:
    asyncio.set_event_loop(loop)
    try:
        loop.run_forever()
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(loop.shutdown_default_executor())
        finally:
            loop.close()
            # Only now is the close done: every server ended, the loop and its executor closed.
            if engine._done is not None and not engine._done.done():
                engine._done.set_result(None)


class _McpAllowlist:
    """A permission checker allowing, by its wildcard, only a tool a configured server listed;
    everything else is its inner checker's to decide."""

    def __init__(self, inner: Any, view: RunTools) -> None:
        self._inner, self._view = inner, view

    def check(self, tool_call: Any, principal_context: Any = None) -> Any:
        from ..permissions import Decision, PermissionResult

        # M33 round 6: the property, not a name shape. A name is allowed here only when the tool
        # the run would execute under it is one a connection built from its server's listing;
        # round 5 asked only whether the name began with a configured alias.
        if self._view.served_by_wildcard(tool_call.name):
            return PermissionResult(Decision.ALLOW, "in allowlist")
        return self._inner.check(tool_call, principal_context)


class McpClients:
    """A Runner's MCP servers and their connections (FR-123, FR-124), all on one loop of their
    own (`_Engine`)."""

    def __init__(self, servers: list[StdioServer], registered: frozenset[str], catalog: Any = None) -> None:
        try:
            import mcp  # noqa: F401
        except ImportError:
            raise ImportError("MCP servers need the mcp library: pip install 'agentsdk[mcp]'") from None
        aliases = [server.alias for server in servers]
        if len(set(aliases)) != len(aliases):
            raise ValueError(f"every server alias must be unique, got {aliases}")
        self.servers = {server.alias: server for server in servers}
        self._engine = _Engine()
        self._engine.servers, self._engine.registered = self.servers, registered
        if catalog is None:
            from ..catalog import InMemoryCatalogStore

            store = InMemoryCatalogStore()
            catalog = lambda: store  # noqa: E731
        self._engine.catalog = catalog
        # A Runner collected without aclose still ends its servers (NFR-39).
        weakref.finalize(self, self._engine.close)

    def records(self) -> list[dict[str, Any]]:
        return [server.record() for server in self.servers.values()]

    @property
    def clock(self) -> Any:
        """FR-127: the catalog's clock, in seconds, monotonic; tests replace it."""
        return self._engine.clock

    @clock.setter
    def clock(self, value: Any) -> None:
        self._engine.clock = value

    def checker(self, checker: Any, view: RunTools) -> Any:
        """P4-D30: an agent's default allowlist, plus every tool a configured server listed that
        its profile names as `<alias>__*`. A registered tool is never allowed by a wildcard,
        whatever its name (pre-review 3, D2; M33 round 6)."""
        if not any(f"{alias}__*" in view.profile for alias in self.servers):
            return checker
        return _McpAllowlist(checker, view)

    def view(self, base: ToolRegistry, profile: tuple[str, ...], tenant_id: str, project_id: str) -> RunTools:
        return RunTools(self, base, profile, tenant_id, project_id)

    async def ensure(self, alias: str, tenant_id: str, project_id: str) -> tuple[list, dict[str, Tool]]:
        """The tenancy's connection to a server, started if it may be: its events and its tools.
        Nothing, once the Runner is closed."""

        async def on_the_mcp_loop():
            connection = self._engine.connection(alias, tenant_id, project_id)
            events = await connection.ensure()
            # FR-127: only the tools whose current hash is accepted for this tenancy, and none of a
            # server whose session has ended: its listing is no longer known (round 1 review).
            if connection.client is None:
                return events, {}
            return events, {name: tool for name, tool in connection.tools.items()
                            if connection.usable.get(name) == tool.spec.configuration["tool_hash"]}

        try:
            return await self._engine.submit(on_the_mcp_loop(), _ensure_bound(self.servers[alias]))
        except _Closed:
            if self._engine.closed:
                return [], {}
            # The MCP loop did not answer in time: the run is told, as for any server that is
            # down, rather than losing the server's tools in silence (round 6 pre-review).
            return [(EventType.MCP_SERVER_UNAVAILABLE, {"alias": alias, "error": "McpLoopUnanswered"})], {}

    async def current(self, alias: str, tenant_id: str, project_id: str, name: str) -> tuple[Tool | None, str | None]:
        """FR-127: the tool a call under `name` may run now, for this tenancy, or None and, when
        its server is down, why."""

        async def on_the_mcp_loop():
            return await self._engine.connection(alias, tenant_id, project_id).current(name)

        return await self._engine.submit(on_the_mcp_loop(), _ensure_bound(self.servers[alias]))

    async def aclose(self) -> None:
        """Closes the Runner to MCP for good (FR-124, NFR-39): every connection, whichever loop
        its runs were on, and none is opened after. Every caller waits for the one close; a
        caller cancelled while waiting stops waiting, and the close goes on."""
        if self._engine.loop is not None and asyncio.get_running_loop() is self._engine.loop:
            raise RuntimeError("Runner.aclose() cannot be awaited on the Runner's own MCP loop")
        bound = max(_ensure_bound(s) for s in self.servers.values()) + 2 * _CLOSE_SECONDS
        await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(self._engine.close())), bound)
        thread = self._engine._thread
        if thread is not None and thread.is_alive():  # resolved as it returns: gone in a moment
            await asyncio.to_thread(thread.join, _CLOSE_SECONDS)


class RunTools(ToolRegistry):
    """A run's tools: its own view, plus the MCP tools its agent names, rebuilt each turn
    from its tenancy's connections (FR-125). Never registered in the Runner's registry."""

    def __init__(self, clients: McpClients, base: ToolRegistry, profile: tuple[str, ...],
                 tenant_id: str, project_id: str) -> None:
        super().__init__()
        self._clients, self._base, self._profile = clients, base, profile
        self.profile = profile
        self._tenancy = (tenant_id, project_id)
        self._aliases = [a for a in clients.servers if any(p.startswith(f"{a}__") for p in profile)]
        self._pending: list[tuple[EventType, dict[str, Any]]] = []
        self._prepared = False
        self._known: dict[str, dict[str, Tool]] = {}
        self._down: set[str] = set()  # aliases this run has been told are unavailable
        self._told: set[tuple[str, str, str, str | None]] = set()  # (alias, tool, hash, error) told blocked
        self._tools = {name: base.get(name) for name in base.names()}

    async def refresh(self) -> list[tuple[EventType, dict[str, Any]]]:
        """Rebuilds the tools; returns the events to emit, those of earlier refreshes first.
        The first turn uses the connections `prepare` just tried, rather than trying again."""
        tools = {name: self._base.get(name) for name in self._base.names()}
        prepared, self._prepared = self._prepared, False
        for alias in self._aliases:
            if not prepared:
                events, self._known[alias] = await self._clients.ensure(alias, *self._tenancy)
                for kind, payload in events:
                    # A server that stays down is reported once a run, not once a turn.
                    if kind is EventType.MCP_SERVER_UNAVAILABLE:
                        if alias in self._down:
                            continue
                        self._down.add(alias)
                    elif kind is EventType.MCP_TOOL_BLOCKED:
                        told = (alias, payload["tool"], payload["tool_hash"], payload.get("error"))
                        if told in self._told:
                            continue  # each blocked version, and each failure, reported once a run
                        self._told.add(told)
                    elif kind is EventType.MCP_SERVER_CONNECTED:
                        self._down.discard(alias)
                    self._pending.append((kind, payload))
            for name, tool in self._known.get(alias, {}).items():
                if _named(self._profile, alias, name) and tool.name not in tools:
                    tools[tool.name] = tool
        self._tools = tools
        events, self._pending = self._pending, []
        return events

    async def blocked(self, name: str) -> str | None:
        """FR-127, executor step 1: why a server tool this run was shown cannot be called now --
        its server changed it, or it is no longer accepted -- or None. Asked of its connection,
        which reads the catalog first when the listing is stale. Any other tool: None."""
        alias, _, listed = name.partition("__")
        tool = self._tools.get(name)
        if tool is None or self._known.get(alias, {}).get(listed) is not tool:
            return None
        current, down = await self._clients.current(alias, *self._tenancy, listed)
        if down is not None:
            return f"{name}: its server is {down}"
        if current is None:
            return f"{name} is not accepted in its current version"
        if current is not tool:
            return f"{name} changed since it was offered; it is offered again next turn"
        return None

    def served_by_wildcard(self, name: str) -> bool:
        """Whether the tool this run executes under `name`, this turn, is the very object a
        connection built from its server's listing, and the profile names that server as
        `<alias>__*`. A registered tool is never one, so no name it has can make it one."""
        alias, _, listed = name.partition("__")
        tool = self._tools.get(name)
        return (tool is not None and f"{alias}__*" in self._profile
                and self._known.get(alias, {}).get(listed) is tool)

    async def prepare(self) -> None:
        """Before the manifest: the tools usable at start, their events kept for the loop."""
        self._pending = await self.refresh()
        self._prepared = True
