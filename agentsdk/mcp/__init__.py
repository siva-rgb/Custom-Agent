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
import hashlib
import pathlib
import json
import math
import os
import re
import subprocess
import sys
import threading
import weakref
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..errors import ToolExecutionError
from ..events import EventType
from ..primitives import InstructionAuthority, Origin, TaintFlag, TrustZone, unstorable_reason
from ..tools import ApprovalPolicy, ResultProvenance, RiskClass, Tool, ToolOutput, ToolRegistry, ToolSpec

__all__ = ["StdioServer"]

_ALIAS = re.compile(r"[a-z][a-z0-9-]{0,23}")
_QUALIFIED = re.compile(r"[A-Za-z0-9_-]{1,64}")
_APPROVALS = ("require", "never")
_MAX_TOOLS = 1000
# M33 round 2: every wait on what a server controls is bounded -- a listing by pages as well as
# tools, closing by seconds, and a failed connection is not tried again for a while, so a
# server that never answers costs one connect_timeout a minute, not one a turn.
_MAX_PAGES = 100
_CLOSE_SECONDS = 10.0
# How far past call_timeout the SDK's own deadline sits (M33 rounds 3 and 4).
_WEDGE_MARGIN = 5.0
_RETRY_SECONDS = 60.0
# NFR-35: whatever a server returns, and whatever `trusted` says.
_UNTRUSTED = ResultProvenance(origin=Origin.MCP_RESOURCE, trust_zone=TrustZone.UNTRUSTED,
                              instruction_authority=InstructionAuthority.DATA_ONLY,
                              taint_flags=frozenset({TaintFlag.EXTERNAL_CONTENT}))
_CONNECTION_CLOSED, _REQUEST_TIMEOUT = -32000, -32001
# The 2026-07-28 era answers server/discover; anything older came through initialize.
_MODERN = "2026-07-28"


@dataclass(frozen=True)
class StdioServer:
    """An MCP server the SDK launches and speaks to over stdio (FR-123, P4-D25).

    `env` is added to the `mcp` library's minimal inherited environment; its values reach
    the server and nothing else (NFR-37). `allow`, when given, is the only tools offered,
    then `deny` removes some. `approval` is `require` (the default, P4-D20) or `never`,
    per tool in `approval_overrides`. `call_timeout` bounds each round trip, in seconds,
    and `connect_timeout` the start, the protocol exchange and the whole listing.
    `protocol` is `auto` (P4-D17), `legacy`, or a pinned revision.
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
        for name, value in self.env.items():
            if not isinstance(name, str) or not isinstance(value, str) or unstorable_reason(name) or unstorable_reason(value):
                raise ValueError(f"env {name!r} must map text to storable text")
        for policy in (self.approval, *self.approval_overrides.values()):
            if policy not in _APPROVALS:
                raise ValueError(f"approval must be one of {', '.join(_APPROVALS)}, got {policy!r}")
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


def _hash(entry: Any) -> str:
    """The tool's version: its description, schemas and annotations (FR-125, FR-127)."""
    body = {"description": entry.description, "input_schema": entry.input_schema,
            "output_schema": entry.output_schema,
            "annotations": None if entry.annotations is None else entry.annotations.model_dump(exclude_none=True)}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


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

    def __init__(self, server: StdioServer, registered: frozenset[str], submit: Any) -> None:
        self.server, self._registered, self._submit = server, registered, submit
        self.client: Any = None
        self.tools: dict[str, Tool] = {}
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
        """The only place a server is started."""
        async with self._lock:
            if self.closed or self.dead or self.client is not None:
                return []
            if self.starts >= 2:  # FR-124: restarted once in its life already
                self.dead, self._why = True, "ServerExited"
                return []
            loop = asyncio.get_running_loop()
            if self._failed_at is not None and loop.time() - self._failed_at < _RETRY_SECONDS:
                return []
            try:
                listed = await self._open()
                if self.closed:  # closed while it started: end it, report nothing
                    await self._discard(self.client, self._task)
                    return []
                events = self._list(listed, await self._schema_problems(listed))
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

    async def _open(self) -> list[Any]:
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
                    async with Client(stdio_client(params, errlog=devnull), mode=server.protocol) as client:
                        _guard_output_validation(client)
                        listed, cursor = [], None
                        for _ in range(_MAX_PAGES):
                            page = await client.list_tools(cursor=cursor)
                            listed += page.tools
                            if not (cursor := page.next_cursor) or len(listed) > _MAX_TOOLS:
                                break
                        else:
                            raise ValueError(f"the listing ran past {_MAX_PAGES} pages")
                        if not ready.done():
                            ready.set_result((client, listed))
                        await stop.wait()
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
            client, listed = await asyncio.wait_for(ready, server.connect_timeout)
        except BaseException:
            await _ended(task)
            raise
        self.client, self.era = client, _MODERN if client.protocol_version == _MODERN else client.protocol_version
        self._info = client.server_info
        return listed

    async def _schema_problems(self, listed: list[Any]) -> list[str | None]:
        """Each listed tool's input-schema problem, checked in processes of their own; a chunk past
        the deadline has its tools skipped, and the server's other tools stay."""
        schemas = [entry.input_schema for entry in listed[:_MAX_TOOLS]]
        outputs = [entry.output_schema for entry in listed[:_MAX_TOOLS]]
        return await asyncio.to_thread(_check_listing, schemas, outputs)

    def _list(self, listed: list[Any], problems: list[str | None]) -> list[tuple[EventType, dict[str, Any]]]:
        alias = self.server.alias
        events = [(EventType.MCP_SERVER_CONNECTED, {
            "alias": alias, "era": self.era,
            "server": {"name": getattr(self._info, "name", None), "version": getattr(self._info, "version", None)}})]
        counts = Counter(entry.name for entry in listed)
        tools = {}
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
                    tools[entry.name] = self._tool(entry)
                except Exception as exc:  # noqa: BLE001 - one entry the SDK cannot hold (a NUL, say): that tool alone is skipped
                    reason = f"it cannot be offered as a tool: {type(exc).__name__}"
            if reason is not None:
                # The reason may quote the server's schema: cut, as its name is (round 6 pre-review).
                events.append((EventType.MCP_TOOL_SKIPPED, {"alias": alias, "tool": entry.name[:200], "reason": reason[:500]}))
        self.tools = tools
        return events

    def _tool(self, entry: Any) -> Tool:
        server, name = self.server, entry.name
        policy = server.approval_overrides.get(name, server.approval)
        spec = ToolSpec(
            name=f"{server.alias}__{name}", description=entry.description or "", input_schema=entry.input_schema,
            risk_class=RiskClass.DESTRUCTIVE, read_only=False, idempotent=False, concurrency_safe=False,
            approval_policy=ApprovalPolicy.REQUIRE_APPROVAL if policy == "require" else ApprovalPolicy.AUTO,
            # The client times each round trip; the executor sets no deadline (FR-125).
            timeout_seconds=None,
            result_provenance=_UNTRUSTED,
            configuration={"mcp_server": server.alias, "tool_hash": _hash(entry)},
        )

        async def call(**arguments: Any) -> ToolOutput:
            # Called on the run's loop; the call itself runs on the Runner's MCP loop.
            return await self._submit(self._call(name, arguments, entry.output_schema), _call_bound(server))

        return Tool(spec=spec, fn=call)

    async def _call(self, name: str, arguments: dict[str, Any], output_schema: Any) -> ToolOutput:
        from mcp import MCPError

        alias = self.server.alias
        if self.client is None:
            self._unreported += await self._ensure()
        if self.closed:
            raise ToolExecutionError(f"mcp server {alias} was closed with its Runner")
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


async def _ended(task: asyncio.Task | None) -> None:
    """Cancels a session's task -- its Client then ends the server -- within the close bound."""
    if task is not None and not task.done():
        task.cancel()
        await asyncio.wait([task], timeout=_CLOSE_SECONDS)


def _ensure_bound(server: StdioServer) -> float:
    """How long a start may take, every inner bound included: the caller's own deadline."""
    return server.connect_timeout + _VALIDATION_SECONDS + 3 * _CLOSE_SECONDS + 5


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

    def connection(self, alias: str, tenant_id: str, project_id: str) -> _Connection:
        """On self.loop only."""
        key = (alias, tenant_id, project_id)
        if key not in self.connections:
            self.connections[key] = _Connection(self.servers[alias], self.registered, self.submit)
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

    def __init__(self, servers: list[StdioServer], registered: frozenset[str]) -> None:
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
        # A Runner collected without aclose still ends its servers (NFR-39).
        weakref.finalize(self, self._engine.close)

    def records(self) -> list[dict[str, Any]]:
        return [server.record() for server in self.servers.values()]

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
            return await connection.ensure(), dict(connection.tools)

        try:
            return await self._engine.submit(on_the_mcp_loop(), _ensure_bound(self.servers[alias]))
        except _Closed:
            if self._engine.closed:
                return [], {}
            # The MCP loop did not answer in time: the run is told, as for any server that is
            # down, rather than losing the server's tools in silence (round 6 pre-review).
            return [(EventType.MCP_SERVER_UNAVAILABLE, {"alias": alias, "error": "McpLoopUnanswered"})], {}

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
                    elif kind is EventType.MCP_SERVER_CONNECTED:
                        self._down.discard(alias)
                    self._pending.append((kind, payload))
            for name, tool in self._known.get(alias, {}).items():
                if _named(self._profile, alias, name) and tool.name not in tools:
                    tools[tool.name] = tool
        self._tools = tools
        events, self._pending = self._pending, []
        return events

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
