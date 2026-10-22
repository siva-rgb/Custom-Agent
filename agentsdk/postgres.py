"""Postgres-backed stores (FR-9, FR-10, FR-11, NFR-2, LLD 2, 3.9).

One database, several stores. NOT one connection pool: every method opens its
own connection and closes it, which is fine for Phase 0's one-run-at-a-time
profile and is the first thing to change before any real load -- see the
recorded limitation. The protocols these
implement are defined in `session.py` and `events.py`, so the loop cannot tell
whether it is talking to memory or Postgres.

ADR-11 is enforced by the schema, not by these classes remembering: every table
declares tenant_id and project_id NOT NULL, so a row that cannot say who it
belongs to fails at the database rather than being caught by review.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import json
import logging
import sys
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from psycopg_pool import ConnectionPool

import psycopg
from psycopg.types.json import Jsonb

from .artifacts import (
    DEFAULT_MAX_CONTENT_BYTES,
    ArtifactRef,
    canonical_id,
    checked_cap,
    integrity_error,
    not_found,
    prepare_put,
    refuse_bad_scope,
    utc_now,
)
from .errors import InvalidPlan, PlanIntegrityError, PlanNotFound
from .events import SCHEMA_VERSION, EventSink, EventType, RunEvent
from .migrate import apply_migrations
from .model import Usage
from .plan import (
    PlanVersion,
    canonical_text,
    checked_status,
    emit_transition,
    plan_from_document,
    refuse_foreign_sink,
    refuse_transition,
)
from .primitives import (
    UNSTORABLE,
    refuse_unstorable_fields,
    ContentProvenance,
    InstructionAuthority,
    Message,
    Origin,
    Role,
    TaintFlag,
    ToolCall,
    ToolResult,
    TrustZone,
    unstorable_reason,
)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


# One pool per DSN, shared by every store built on it (FR-20).
#
# Before this, every store method opened its own connection: a short run cost
# 40 separate connect / authenticate / close cycles, each a TCP handshake and
# an authentication round trip to do a single INSERT. That is affordable when
# one run happens at a time and is the first thing to collapse under Phase 2's
# fan-out.
#
# Keyed by DSN string rather than held on the store, because PostgresRunStore,
# PostgresSessionStore and PostgresEventStore are constructed separately for
# the same database and would otherwise each hold their own pool.
_POOLS: dict[str, ConnectionPool] = {}
_POOLS_LOCK = threading.Lock()

# Sized against the thread pool that will be calling in: asyncio.to_thread uses
# min(32, cpu_count + 4) workers by default, so a smaller pool would simply
# move the queue from one place to another.
POOL_MAX_SIZE = 32


def _pool(dsn: str) -> ConnectionPool:
    """The pool for this DSN, created once.

    Double-checked under a lock: two threads racing to create the pool for the
    same DSN would otherwise both build one, and whichever lost would leak its
    connections with nothing holding a reference to close them.
    """
    pool = _POOLS.get(dsn)
    if pool is not None:
        return pool
    with _POOLS_LOCK:
        pool = _POOLS.get(dsn)
        if pool is None:
            pool = ConnectionPool(
                dsn,
                min_size=1,
                max_size=POOL_MAX_SIZE,
                # A caller that waits forever for a connection is a hang with
                # no error; one that waits 30 seconds is a slow request with a
                # message naming the pool.
                timeout=30.0,
                # Validate on checkout (M7 round 2). Without it the pool handed
                # out connections the server had already closed -- a restart, a
                # failover, an idle kill -- and every run that drew one failed:
                # five dead connections, five failed runs, where the per-call
                # connections this pool replaced had simply reconnected. The
                # check is a round trip per checkout, made on a worker thread.
                #
                # A connection that dies DURING a write still fails that write,
                # and deliberately so: the insert may have committed before the
                # reply was lost, and retrying a MAX + 1 append would duplicate
                # the message rather than recover it.
                check=ConnectionPool.check_connection,
                open=True,
            )
            _POOLS[dsn] = pool
    return pool


# FR-82 (M18a): whether a checkout from a thread running an event loop is refused.
# Off in production, where a latent violation should cost latency rather than a run;
# tests/conftest.py turns it on for every test, where it must fail loudly
# (DECISION-53ee27f6).
REFUSE_ON_EVENT_LOOP = False
_LOGGED: set[str] = set()


def _checkout(dsn: str) -> contextlib.AbstractContextManager[Any]:
    """A pooled connection for a store method, never taken on the event loop (FR-20).

    Every store method reaches the database through here, so a call site added
    tomorrow is covered without anyone listing it -- the lesson of
    KNOWLEDGE-c0f23fea, which enumerated call sites three times and missed one each
    time. The context manager is built before the check and checks out only when
    entered, so a refused call takes no connection but is still visible to anything
    wrapping the pool.
    """
    connection = _pool(dsn).connection()
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return connection  # a worker thread, or synchronous code with no loop
    method = sys._getframe(1).f_code.co_qualname
    message = (
        f"{method} checked out a database connection on the event loop thread; "
        "store calls made from a coroutine go through asyncio.to_thread (FR-20)"
    )
    if REFUSE_ON_EVENT_LOOP:
        raise RuntimeError(message)
    if method not in _LOGGED:
        _LOGGED.add(method)
        logging.getLogger(__name__).warning(message)
    return connection

def close_pools() -> None:
    """Close every pool. For process shutdown and for tests that count
    connections; not needed during normal operation."""
    with _POOLS_LOCK:
        while _POOLS:
            _, pool = _POOLS.popitem()
            pool.close()


def apply_schema(dsn: str) -> None:
    """Create the baseline, then bring it forward (FR-17) -- under one lock.

    schema.sql alone can only ever CREATE. On a database that already exists it
    is a no-op for anything new, so a column added to it would be silently
    absent and the code would fail later at insert time. Migrations run behind
    the same call, so every existing call site gets them without knowing.

    Both halves run inside apply_migrations' advisory lock. The first version
    ran schema.sql here, in autocommit and outside that lock, so workers
    initialising an empty database together raced on CREATE and all but one
    failed (M7 review rounds 1 and 2). This module now opens no connection of
    its own at all, which is what lets the event-loop test watch the pool
    alone.
    """
    apply_migrations(dsn, baseline=SCHEMA_PATH)


# --- serialisation ----------------------------------------------------------
# Provenance is persisted in full. A stored ToolResult that lost its provenance
# would break the invariant precisely where it matters most -- after the fact,
# when someone is trying to establish where a claim came from.


def _provenance_to_json(p: ContentProvenance) -> dict[str, Any]:
    return {
        "origin": p.origin.value,
        "instruction_authority": p.instruction_authority.value,
        "trust_zone": p.trust_zone.value,
        "taint_flags": sorted(flag.value for flag in p.taint_flags),
        "source_uri_or_hash": p.source_uri_or_hash,
    }


def _provenance_from_json(raw: dict[str, Any]) -> ContentProvenance:
    return ContentProvenance(
        origin=Origin(raw["origin"]),
        instruction_authority=InstructionAuthority(raw["instruction_authority"]),
        trust_zone=TrustZone(raw["trust_zone"]),
        taint_flags=frozenset(TaintFlag(f) for f in raw.get("taint_flags") or ()),
        source_uri_or_hash=raw.get("source_uri_or_hash"),
    )


def _message_to_columns(message: Message) -> tuple[Any, Any]:
    tool_calls = (
        [{"id": c.id, "name": c.name, "arguments": c.arguments, "arguments_error": c.arguments_error}
         for c in message.tool_calls]
        if message.tool_calls
        else None
    )
    tool_results = (
        [
            {
                "tool_call_id": r.tool_call_id,
                "content": r.content,
                "is_error": r.is_error,
                "provenance": _provenance_to_json(r.provenance),
            }
            for r in message.tool_results
        ]
        if message.tool_results
        else None
    )
    return (Jsonb(tool_calls) if tool_calls else None, Jsonb(tool_results) if tool_results else None)


def _message_from_row(row: tuple) -> Message:
    role, content, tool_calls, tool_results = row
    return Message(
        role=Role(role),
        content=content,
        tool_calls=tuple(
            ToolCall(
                id=c["id"],
                name=c["name"],
                arguments=c.get("arguments") or {},
                arguments_error=c.get("arguments_error"),
            )
            for c in (tool_calls or ())
        ),
        tool_results=tuple(
            ToolResult(
                tool_call_id=r["tool_call_id"],
                content=r["content"],
                provenance=_provenance_from_json(r["provenance"]),
                is_error=r.get("is_error", False),
            )
            for r in (tool_results or ())
        ),
    )


# --- stores -----------------------------------------------------------------


@dataclass(frozen=True)
class RunScope:
    """Everything a row needs to be tenant-scoped. Passed, never inferred."""

    run_id: str
    tenant_id: str
    project_id: str

    def __post_init__(self) -> None:
        # These reach NOT NULL columns on every table, so an unstorable one
        # fails the write with an opaque psycopg error at some later point.
        # Refused here instead, where the caller can see which field it was.
        # The shared helper walks dataclasses.fields() rather than the three
        # names this used to list -- a field added here is covered.
        refuse_unstorable_fields(self)


# --- column fitness (M5 round 8) --------------------------------------------
#
# `unstorable_reason` answers "can this be serialised". That is not the same
# question as "does this fit the column it is going to", and round 8 rejected
# on the difference: max_turns was checked with the serialisability predicate
# while runs.max_turns is INTEGER, so 2**31 -- an ordinary int that passes
# RunConfig's own validation -- passed the guard and blew up at the write.
#
# A check that runs and returns the wrong answer is invisible to a test that
# only asks whether a check runs, which is exactly what the round-7 test did.
# So the checks below are keyed by the COLUMN TYPE each value is headed for.

_INT32_MIN, _INT32_MAX = -(2**31), 2**31 - 1


def column_rejection_reason(value: Any, sql_type: str) -> str | None:
    """Why `value` cannot go into a column of this declared type."""
    if sql_type == "UUID" and isinstance(value, uuid.UUID):
        # Before the serialisability test, which refuses a uuid.UUID as a
        # TypeError -- and uuid.UUID is exactly what get_run and PostgresTrace
        # hand back for a run id, so passing one straight back as parent_run_id
        # was refused by the SDK's own output type (M7 round 2). A UUID object
        # cannot be malformed or non-canonical.
        return None
    reason = unstorable_reason(value)
    if reason is not None:
        return reason
    if sql_type == "UUID":
        # Refused HERE rather than at the write, for the same reason max_turns
        # is: a malformed id completes in memory and fails against Postgres
        # with a DataError several frames away from the caller that supplied
        # it. None is allowed -- a top-level run has no parent, which is the
        # common case rather than an exception.
        if value is not None:
            if not isinstance(value, str):
                return f"a {type(value).__name__} is not a UUID"
            try:
                canonical = str(uuid.UUID(value))
            except ValueError:
                return f"{value!r} is not a well-formed UUID"
            if value != canonical:
                # uuid.UUID() is more permissive than a Postgres uuid column:
                # it strips a "urn:uuid:" prefix the column refuses, so the
                # first version of this guard waved that form through and the
                # write failed with a DataError -- a guard answering a different
                # question from the column it protects, M5 round 8's shape,
                # found by a differential probe rather than by review. Only the
                # canonical form is accepted. That also refuses uppercase,
                # braced and unhyphenated forms the column WOULD take, which is
                # the safe direction: every run id this SDK issues is canonical.
                return f"{value!r} is not a canonical UUID (expected {canonical!r})"
    if sql_type == "INTEGER":
        if isinstance(value, bool):
            # The carve-out this replaces was the last surviving instance of
            # round 8's shape: a bool IS an int in Python, so it passed the
            # `int` test, and excluding it from the range check looked
            # harmless because True is trivially in range. But psycopg adapts
            # it to SQL boolean, and the column is integer -- DatatypeMismatch
            # at the write, after completing happily in memory. Copied from
            # token_count without re-asking what the exclusion was FOR.
            return "a bool is not an integer: an INTEGER column refuses it"
        if isinstance(value, int) and not (_INT32_MIN <= value <= _INT32_MAX):
            return (
                f"{value} is outside the range of an INTEGER column "
                f"({_INT32_MIN}..{_INT32_MAX})"
            )
    return None


def _insert_message(conn: Any, scope: RunScope, run_id: str, message: Message) -> None:
    """One message row, on the caller's connection and inside its transaction, with
    the messages lock already taken (FR-9; M20a lets one transaction hold it with an
    event)."""
    tool_calls, tool_results = _message_to_columns(message)
    # sequence_no is computed INSIDE the insert's transaction, so
    # two concurrent appends cannot both read the same max and
    # produce a duplicate. The UNIQUE (run_id, sequence_no)
    # constraint is what makes the race a visible error instead of
    # a silently reordered history.
    #
    # tenant_id and project_id are taken from the RUN ROW, not from
    # the caller's scope. Trusting the scope let a caller file a
    # message under a tenant the run does not belong to, which
    # silently defeats the reason messages.tenant_id is
    # denormalised: isolation without a join is only worth having
    # if the denormalised copy cannot disagree with the original.
    # The scope is still matched in the WHERE, so a caller that
    # thinks it is writing for another tenant gets an error rather
    # than a quietly corrected row.
    cursor = conn.execute(
        """
        INSERT INTO messages (
            message_id, run_id, tenant_id, project_id, sequence_no,
            role, content, tool_calls, tool_results
        )
        SELECT %s, r.run_id, r.tenant_id, r.project_id,
               COALESCE(MAX(m.sequence_no), 0) + 1,
               %s, %s, %s, %s
        FROM runs r LEFT JOIN messages m ON m.run_id = r.run_id
        WHERE r.run_id = %s AND r.tenant_id = %s AND r.project_id = %s
        GROUP BY r.run_id, r.tenant_id, r.project_id
        """,
        (
            uuid.uuid4(),
            message.role.value,
            message.content,
            tool_calls,
            tool_results,
            run_id,
            scope.tenant_id,
            scope.project_id,
        ),
    )
    if cursor.rowcount != 1:
        raise ValueError(
            f"no run {run_id!r} for tenant {scope.tenant_id!r} / project "
            f"{scope.project_id!r}: a message cannot be filed against a "
            "run that does not exist or belongs to someone else"
        )


class PostgresSessionStore:
    """FR-9. Insert-only; no update, no delete."""

    def __init__(self, dsn: str, scope: RunScope | None = None) -> None:
        self._dsn = dsn
        self._scope = scope

    def bind(self, scope: RunScope) -> PostgresSessionStore:
        """A per-run view. Runner binds this so append() knows the tenant."""
        return PostgresSessionStore(self._dsn, scope)

    def _bound(self, run_id: str) -> RunScope:
        scope = self._scope
        if scope is None or scope.run_id != run_id:
            raise ValueError(
                "PostgresSessionStore must be bound to the run's scope before appending; "
                "tenant_id and project_id are mandatory on every row (ADR-11)"
            )
        return scope

    def append(self, run_id: str, message: Message) -> None:
        scope = self._bound(run_id)
        with _checkout(self._dsn) as conn:
            with conn.transaction():
                _serialise_writers(conn, run_id, _LOCK_MESSAGES)
                _insert_message(conn, scope, run_id, message)

    def append_with_event(
        self, run_id: str, message: Message, sink: Any, event_type: EventType, payload: dict[str, Any]
    ) -> RunEvent:
        """FR-86 (M20a): a message and the event that explains it, in one transaction,
        so both are stored or neither is -- a compaction's summary and its
        ContextCompacted. The messages lock, then the events lock: the one order any
        writer takes both in.

        A sink that names no scope, or cannot write inside this transaction, gets the
        two writes in turn, as before M20a; that is a custom sink's limit, declared. A
        sink that names a scope must name this run, by tenant and project as well, or
        it is refused before anything is written (FR-87, M21).
        """
        scope = self._bound(run_id)
        named = getattr(sink, "scope", None)
        if named is not None and tuple(named) != (scope.tenant_id, scope.project_id, run_id):
            raise ValueError("the event sink does not write to this run; message and event must be one run's")
        if named is None or not (hasattr(sink, "write") and hasattr(sink, "stored")):
            self.append(run_id, message)
            return sink.emit(event_type, payload)
        with _checkout(self._dsn) as conn:
            with conn.transaction():
                _serialise_writers(conn, run_id, _LOCK_MESSAGES)
                _insert_message(conn, scope, run_id, message)
                event = sink.write(conn, event_type, payload)
        # After the commit, as emit does: the stored event reaches the run's buffer and
        # its handle only once it is stored.
        sink.stored(event)
        return event

    def history(self, run_id: str) -> list[Message]:
        """Tenant-scoped on READ as well as write.

        A bound store used to return any tenant's messages given a run id.
        Defensible on the grounds that run ids are UUIDv4 and FR-9's signature
        is history(run_id) -- but NFR-2's premise is that tenancy is enforced,
        not merely unguessable, and an id is a capability only until one leaks
        into a log or a support ticket. Enforcing it here costs one WHERE
        clause; the index on (tenant_id, project_id) already exists.
        """
        scope = self._scope
        if scope is None or scope.run_id != run_id:
            raise ValueError(
                "PostgresSessionStore must be bound to the run's scope before reading; "
                "tenancy is enforced on read as well as write (NFR-2)"
            )
        with _checkout(self._dsn) as conn:
            rows = conn.execute(
                """
                SELECT role, content, tool_calls, tool_results
                FROM messages
                WHERE run_id = %s AND tenant_id = %s AND project_id = %s
                ORDER BY sequence_no
                """,
                (run_id, scope.tenant_id, scope.project_id),
            ).fetchall()
        return [_message_from_row(row) for row in rows]


# The two sequence spaces one run owns. Separate keys so appending a message
# does not make an event wait behind it -- they are different sequences and
# have no reason to contend.
_LOCK_MESSAGES = 1
_LOCK_EVENTS = 2


def _serialise_writers(conn: Any, run_id: str, space: int) -> None:
    """Make concurrent writers to one run queue instead of race (FR-19).

    Both write paths compute their sequence number as MAX + 1 inside the
    insert. That is SAFE -- two writers cannot produce the same number
    unnoticed, because UNIQUE (run_id, sequence_no) turns the race into an
    error. It is not AVAILABLE: the loser's row is simply not written, and the
    caller gets a psycopg exception for a write that would have succeeded a
    millisecond later. Measured before this: 12 concurrent appends to one run,
    9 committed and 3 died.

    A bounded retry was the obvious alternative and is the wrong one here. Each
    round of contention lets exactly one writer through, so N simultaneous
    writers need N rounds, and any cap small enough to be safe is too small to
    help at the concurrency Phase 2 introduces.

    An advisory lock inverts that: writers queue, every one of them commits,
    and the number of round trips does not grow with contention. It is
    transaction-scoped, so it is released on commit, on rollback, and if this
    process dies -- a crashed writer cannot wedge a run. hashtext() may collide
    across different run ids, which costs two unrelated runs a moment of
    serialisation and can never cost correctness.

    The UNIQUE constraint stays exactly where it is. This lock is about
    availability; the constraint is what makes the invariant true at rest, and
    it still holds if a future writer forgets to take the lock.
    """
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s), %s)", (run_id, space))


class PostgresEventStore:
    """FR-10. Owns sequence numbering, like the in-memory sink it replaces."""

    def __init__(self, dsn: str, tenant_id: str, project_id: str, run_id: str) -> None:
        self._dsn = dsn
        self._tenant_id = tenant_id
        self._project_id = project_id
        self._run_id = run_id
        self._buffer: list[RunEvent] = []

    @property
    def scope(self) -> tuple[str, str, str]:
        """The tenant, project and run this sink writes to, so a writer can check it
        is writing to the run it means (M19, F2)."""
        return (self._tenant_id, self._project_id, self._run_id)

    def emit(
        self, event_type: EventType, payload: dict[str, Any] | None = None, **identifiers: Any
    ) -> RunEvent:
        with _checkout(self._dsn) as conn:
            event = self.write(conn, event_type, payload, **identifiers)
        self.stored(event)
        return event

    def write(
        self, conn: Any, event_type: EventType, payload: dict[str, Any] | None = None, **identifiers: Any
    ) -> RunEvent:
        """One event row on the caller's connection, inside its transaction, returned
        with its stored sequence_no; `stored` then records it here. emit is the two
        together; M20a's append_with_event writes a message in the same transaction."""
        # sequence_no is assigned by the DATABASE below, not here (FR-18).
        # This process's own count is only ever right when this process is the
        # only writer, which stops being true the moment a run has a subagent
        # or is resumed: a second sink starts counting at 1 again and collides
        # with rows already stored. Measured before the fix -- two sinks on one
        # run, the second died with UniqueViolation and one of the two events
        # was lost. Zero is a placeholder that never reaches the database.
        event = RunEvent(
            event_type=event_type,
            tenant_id=self._tenant_id,
            project_id=self._project_id,
            run_id=self._run_id,
            sequence_no=0,
            payload=payload or {},
            **identifiers,
        )
        _serialise_writers(conn, event.run_id, _LOCK_EVENTS)
        cursor = conn.execute(
            """
            INSERT INTO run_events (
                event_id, schema_version, sequence_no, event_type,
                tenant_id, project_id, run_id,
                agent_id, task_id, tool_call_id, attempt_id,
                parent_event_id, correlation_id, timestamp, payload
            )
            -- Tenancy from the RUN ROW, as messages already does
            -- (DECISION-aed7e4d8). Taking it from the caller let an event
            -- for tenant A's run be filed as tenant B, where it is
            -- invisible in A's trace -- the same decision made
            -- inconsistently one function over, for three rounds.
            --
            -- And the sequence number from the STORED maximum, computed
            -- inside this insert's transaction, exactly as
            -- PostgresSessionStore.append already does (FR-18). Two
            -- concurrent emits cannot both read the same max, and a second
            -- sink continues the sequence instead of restarting it.
            SELECT %s,%s,
                   COALESCE(MAX(e.sequence_no), 0) + 1,
                   %s, r.tenant_id, r.project_id, r.run_id,
                   %s,%s,%s,%s,%s,%s,%s,%s
            FROM runs r LEFT JOIN run_events e ON e.run_id = r.run_id
            WHERE r.run_id = %s AND r.tenant_id = %s AND r.project_id = %s
            GROUP BY r.run_id, r.tenant_id, r.project_id
            RETURNING sequence_no
            """,
            (
                event.event_id,
                event.schema_version,
                event.event_type.value,
                event.agent_id,
                event.task_id,
                event.tool_call_id,
                event.attempt_id,
                event.parent_event_id,
                event.correlation_id,
                event.timestamp,
                Jsonb(_json_safe(event.payload)),
                event.run_id,
                self._tenant_id,
                self._project_id,
            ),
        )
        row = cursor.fetchone()
        if row is None:
            # Zero rows means the run does not exist or belongs to another
            # tenant. Raising rather than returning quietly: an event that
            # was not written is an entry the audit trail silently lacks,
            # which is the failure mode this whole milestone is about.
            raise ValueError(
                f"no run {event.run_id!r} for tenant {self._tenant_id!r} / project "
                f"{self._project_id!r}: an event cannot be filed against a run that "
                "does not exist or belongs to someone else"
            )
        # The stored number, not the one this process guessed. events() and the
        # returned RunEvent must agree with the row, or an in-memory trace and
        # a reconstructed one disagree about order -- which is the thing NFR-3
        # exists to prevent.
        return dataclasses.replace(event, sequence_no=row[0])

    def stored(self, event: RunEvent) -> None:
        """An event this store has written and committed, into the run's buffer."""
        # FR-52. Another writer can commit and append between this insert's commit
        # and this append, so arrival order is not sequence order: widened to 6 ms,
        # that window read back [1, 2, 4, 6, 3, 5, ...]. The buffer is kept in
        # sequence_no order instead, under a lock, so RunResult.events and a
        # RunHandle's stream agree with the stored rows.
        with self._order_lock:
            position = len(self._buffer)
            while position and self._buffer[position - 1].sequence_no > event.sequence_no:
                position -= 1
            self._buffer.insert(position, event)

    def events(self) -> tuple[RunEvent, ...]:
        with self._order_lock:
            return tuple(self._buffer)

    # One lock shared by every store: it is held for a few list operations, never
    # across a database call, so sharing it costs nothing measurable.
    _order_lock = threading.Lock()


def _json_safe(value: Any) -> Any:
    """Payloads carry enums and datetimes; JSONB does not.

    Also the last line of defence for the audit trail. The primitives refuse
    unstorable values upstream, but a payload is assembled here from many
    sources -- ids, model names, a stringified object -- and an event that
    cannot be written is an event the trail simply lacks, which is strictly
    worse than one marked as unstorable. So a value that would fail the write
    is replaced with a marker naming the reason, rather than taking the run
    down with it.
    """
    if isinstance(value, dict):
        return {_json_safe(str(k)): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value") and hasattr(value, "name"):  # Enum
        return _json_safe(value.value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        reason = unstorable_reason(value)
        return value if reason is None else f"{UNSTORABLE}: {reason}"
    rendered = str(value)
    reason = unstorable_reason(rendered)
    return rendered if reason is None else f"{UNSTORABLE}: {reason}"


class PostgresRunStore:
    """The `runs` and `execution_manifests` tables (FR-11)."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def start_run(
        self,
        scope: RunScope,
        *,
        agent_spec_id: str,
        max_turns: int,
        model_id: str | None,
        principal_context: dict[str, Any] | None,
        manifest: dict[str, Any],
        parent_run_id: str | None = None,
    ) -> None:
        """The run row and its manifest are one transaction, not two.

        AC-6 requires exactly one manifest per run. Written over two
        connections that held only while nothing failed in between: an ordinary
        transient error after the first write left a `runs` row nothing could
        explain -- no manifest, and no RunStarted event either, because the
        emit is sequenced after both writes.

        The manifest is a parameter rather than a follow-up call, so "a run
        exists without its manifest" stops being a state this API can express.
        Either both rows commit or neither does.
        """
        # Every value this statement writes, not the two that were named in a
        # review caveat. Round 6's caveat listed tenant_id, project_id,
        # agent_spec_id and model_id; the repair implemented that list, and
        # round 7 rejected on principal_context -- the one caller-supplied
        # value the caveat had not enumerated. Keyed by column so a new column
        # is added here in the same edit that adds it to the INSERT.
        # Keyed by the column's declared type, not by one predicate for
        # everything: see column_rejection_reason.
        for name, value, sql_type in (
            ("agent_spec_id", agent_spec_id, "TEXT"),
            ("model_id", model_id, "TEXT"),
            ("max_turns", max_turns, "INTEGER"),
            ("principal_context", principal_context, "JSONB"),
            ("manifest", manifest, "JSONB"),
            ("parent_run_id", parent_run_id, "UUID"),
        ):
            reason = column_rejection_reason(value, sql_type)
            if reason is not None:
                raise ValueError(f"{name} cannot be stored: {reason}")
        with _checkout(self._dsn) as conn:
            with conn.transaction():
                cursor = conn.execute(
                    """
                    INSERT INTO runs (
                        run_id, tenant_id, project_id, agent_spec_id, status,
                        principal_context, max_turns, model_id, parent_run_id
                    )
                    SELECT %s,%s,%s,%s,'running',%s,%s,%s,%s
                    -- A parent link may only point INSIDE the child's own
                    -- tenant and project (FR-21, ADR-11). The foreign key
                    -- alone would happily let a run in tenant B name a parent
                    -- in tenant A, which puts one tenant's run id in another
                    -- tenant's row and makes A's lineage readable from B.
                    -- Checked in the same statement as the insert, so there is
                    -- no window between the check and the write.
                    WHERE %s::uuid IS NULL
                       OR EXISTS (
                            SELECT 1 FROM runs parent
                            WHERE parent.run_id = %s::uuid
                              AND parent.tenant_id = %s
                              AND parent.project_id = %s
                          )
                    """,
                    (
                        scope.run_id,
                        scope.tenant_id,
                        scope.project_id,
                        agent_spec_id,
                        Jsonb(principal_context) if principal_context else None,
                        max_turns,
                        model_id,
                        parent_run_id,
                        parent_run_id,
                        parent_run_id,
                        scope.tenant_id,
                        scope.project_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError(
                        f"parent run {parent_run_id!r} does not exist in tenant "
                        f"{scope.tenant_id!r} / project {scope.project_id!r}: a run "
                        "may only descend from one its own tenant can see"
                    )
                self._insert_manifest(conn, scope, manifest)

    # FR-31: the Runner hands this store a run's usage and cost with its status.
    # Declared rather than inferred from finish_run's signature: see Runner._finish.
    records_accounting = True
    # FR-68: and the budget a run held, beside its usage and cost (M17).
    records_budget = True

    # FR-31: the columns migration 0003 created, one per Usage field. Named
    # here rather than read off Usage because they are what the migration made;
    # the M9 persistence test compares every one of them with the RunResult.
    _USAGE_COLUMNS = (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
    )

    def finish_run(
        self,
        scope: RunScope,
        status: str,
        usage: Usage | None = None,
        cost_usd: Decimal | None = None,
        budget_spend: dict[str, Any] | None = None,
        price_table_date: str | None = None,
    ) -> None:
        """Tenant-scoped, like every other statement here.

        history() was scoped by DECISION-e692386f on the premise that NFR-2
        means tenancy is enforced rather than unguessable. Leaving the writes
        and the trace unscoped answered the same question the other way one
        function over, which is worse than either answer consistently applied.

        The run's token totals and cost go in the same statement as its status
        (FR-31), so a terminal row never exists without them. A value no column
        can hold is written NULL rather than failing the write -- accounting
        never fails a run (NFR-11) -- and a caller that passes neither, as every
        caller before M9 did, writes NULL: unknown, not zero.
        """
        counts = [self._bigint_or_null(getattr(usage, name, None)) for name in self._USAGE_COLUMNS]
        self._write_terminal_row(scope, status, counts, cost_usd)
        # The budget columns are accounting, and accounting never fails a run (NFR-11).
        # They are written after the status row is committed, in their own statements,
        # so a value this database cannot hold costs the manifest its budget and never
        # the run its status (round 4, J1).
        if budget_spend is not None or price_table_date is not None:
            try:
                self._write_budget_columns(scope, budget_spend, price_table_date)
            except Exception:  # noqa: BLE001 - the run's status is already recorded
                pass

    def _write_terminal_row(
        self, scope: RunScope, status: str, counts: list[int | None], cost_usd: Decimal | None
    ) -> None:
        with _checkout(self._dsn) as conn:
            conn.execute(
                "UPDATE runs SET status = %s, completed_at = %s, "
                + ", ".join(f"{name} = %s" for name in self._USAGE_COLUMNS)
                + ", cost_usd = %s"
                " WHERE run_id = %s AND tenant_id = %s AND project_id = %s",
                (
                    status,
                    datetime.now(timezone.utc),
                    *counts,
                    self._numeric_or_null(cost_usd),
                    scope.run_id,
                    scope.tenant_id,
                    scope.project_id,
                ),
            )

    def _write_budget_columns(
        self, scope: RunScope, budget_spend: dict[str, Any] | None, price_table_date: str | None
    ) -> None:
        with _checkout(self._dsn) as conn:
            if price_table_date is not None:
                # FR-69: the shipped table priced at least one call of this run, which
                # a before_model hook can make true of a run whose recorded model the
                # table does not list (round 3, H5). Only ever filled in, never cleared.
                conn.execute(
                    "UPDATE execution_manifests SET price_table_date = %s"
                    " WHERE run_id = %s AND tenant_id = %s AND project_id = %s AND price_table_date IS NULL",
                    (price_table_date, scope.run_id, scope.tenant_id, scope.project_id),
                )
            if budget_spend is not None:
                # FR-68: the run's final spend, known only now, beside the policy and
                # reservations its manifest already holds.
                conn.execute(
                    "UPDATE execution_manifests SET budget_spend = %s"
                    " WHERE run_id = %s AND tenant_id = %s AND project_id = %s",
                    (Jsonb(budget_spend), scope.run_id, scope.tenant_id, scope.project_id),
                )

    @staticmethod
    def _bigint_or_null(value: Any) -> int | None:
        """A token count as a BIGINT column holds it, or None if it cannot.

        Usage keeps any int a provider sends, and psycopg refuses one past the
        interpreter's digit limit before the database is even asked.
        """
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value if -(2**63) <= value < 2**63 else None

    @staticmethod
    def _numeric_or_null(value: Any) -> Decimal | None:
        """A cost as a NUMERIC column holds it: at most 131072 digits before the
        point and 16383 after. None otherwise, and for anything not a finite
        Decimal."""
        try:
            if not isinstance(value, Decimal) or not value.is_finite():
                return None
            if value.as_tuple().exponent < -16383 or value.adjusted() >= 131072:
                return None
            return value
        except Exception:  # noqa: BLE001 - accounting never fails a run
            return None

    def write_manifest(self, scope: RunScope, manifest: dict[str, Any]) -> None:
        """Write a manifest for a run that already exists.

        Not on the Runner's path -- `start_run` writes the manifest atomically
        with the run row. This one cannot reintroduce that defect: it only ever
        adds a manifest, so it cannot leave a run without one. A second call
        for the same run is refused by the primary key, not by care.
        """
        with _checkout(self._dsn) as conn:
            self._insert_manifest(conn, scope, manifest)

    @staticmethod
    def _insert_manifest(
        conn: psycopg.Connection, scope: RunScope, manifest: dict[str, Any]
    ) -> None:
        """Takes the caller's connection so it can join an open transaction.

        Three columns arrived with migration 0003 (FR-31): the effective output
        limit, the reasoning effort, and the prices the run was costed with.
        The last arrived with 0004 (FR-43): the scheduler limits the run
        executed under. Each is NULL for a manifest built without it.
        """
        pricing = manifest.get("pricing")
        scheduler_limits = manifest.get("scheduler_limits")
        # FR-68, FR-69 (0008): what this run was allowed to spend, and the date of the
        # shipped price table when that table is what priced it. NULL when neither
        # applies, as for every manifest written before 0008.
        budget_policy = manifest.get("budget_policy")
        budget_reservations = manifest.get("budget_reservations")
        # FR-77 (0009): the tools the run was sent; NULL for every manifest before it.
        tools_sent = manifest.get("tools_sent")
        # FR-101 (0012): the gate policy; NULL for an ungated run and every one before it.
        gate_policy = manifest.get("gate_policy")
        # FR-116 (0014): NULL for every manifest before M30.
        approval_timeout = manifest.get("approval_timeout")
        # FR-123 (0016): NULL for every manifest before M33.
        mcp_servers = manifest.get("mcp_servers")
        conn.execute(
            """
                INSERT INTO execution_manifests (
                    run_id, tenant_id, project_id, sdk_version, agent_spec_hash,
                    instructions_hash, model_id, model_version,
                    model_adapter_version, tool_spec_hashes, policy_version,
                    max_output_tokens, reasoning_effort, pricing, scheduler_limits,
                    budget_policy, budget_reservations, price_table_date, tools_sent, gate_policy,
                    approval_timeout, mcp_servers
                )
                -- Tenancy from the run row, as everywhere else. Inside
                -- start_run the row is written in this same transaction, so
                -- the SELECT sees it.
                SELECT r.run_id, r.tenant_id, r.project_id, %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                FROM runs r
                WHERE r.run_id = %s AND r.tenant_id = %s AND r.project_id = %s
            """,
            (
                manifest["sdk_version"],
                manifest["agent_spec_hash"],
                manifest["instructions_hash"],
                manifest.get("model_id"),
                manifest.get("model_version"),
                manifest.get("model_adapter_version"),
                Jsonb(manifest.get("tool_spec_hashes") or []),
                manifest.get("policy_version"),
                manifest.get("max_output_tokens"),
                manifest.get("reasoning_effort"),
                Jsonb(pricing) if pricing is not None else None,
                Jsonb(scheduler_limits) if scheduler_limits is not None else None,
                Jsonb(budget_policy) if budget_policy is not None else None,
                Jsonb(budget_reservations) if budget_reservations is not None else None,
                manifest.get("price_table_date"),
                Jsonb(tools_sent) if tools_sent is not None else None,
                Jsonb(gate_policy) if gate_policy is not None else None,
                approval_timeout,
                Jsonb(mcp_servers) if mcp_servers is not None else None,
                scope.run_id,
                scope.tenant_id,
                scope.project_id,
            ),
        )

    def get_run(self, scope: RunScope) -> dict[str, Any] | None:
        # The usage columns and cost_usd are appended after parent_run_id, so
        # no key an earlier caller reads changes (FR-31).
        keys = (
            "run_id", "tenant_id", "project_id", "agent_spec_id", "status",
            "principal_context", "max_turns", "model_id", "started_at",
            "completed_at", "parent_run_id", *self._USAGE_COLUMNS, "cost_usd",
        )
        with _checkout(self._dsn) as conn:
            row = conn.execute(
                f"SELECT {', '.join(keys)} FROM runs"
                " WHERE run_id = %s AND tenant_id = %s AND project_id = %s",
                (scope.run_id, scope.tenant_id, scope.project_id),
            ).fetchone()
        if row is None:
            return None
        return dict(zip(keys, row))


class PostgresTrace:
    """AC-7: reconstruct a run from persisted state PLUS ordered events.

    Events alone were never the source of truth, so this reads all three and
    says so in its shape rather than pretending the event stream is enough.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._runs = PostgresRunStore(dsn)

    def reconstruct(self, scope: RunScope) -> dict[str, Any]:
        """AC-7's own vehicle, so it enforces tenancy rather than deciding it
        differently from the store it reads beside."""
        run = self._runs.get_run(scope)
        tenancy = (scope.run_id, scope.tenant_id, scope.project_id)
        with _checkout(self._dsn) as conn:
            messages = conn.execute(
                "SELECT sequence_no, role, content, tool_calls, tool_results"
                " FROM messages WHERE run_id = %s AND tenant_id = %s AND project_id = %s"
                " ORDER BY sequence_no",
                tenancy,
            ).fetchall()
            events = conn.execute(
                "SELECT sequence_no, event_type, payload, timestamp"
                " FROM run_events WHERE run_id = %s AND tenant_id = %s AND project_id = %s"
                " ORDER BY sequence_no",
                tenancy,
            ).fetchall()
            manifest = conn.execute(
                "SELECT sdk_version, agent_spec_hash, instructions_hash, model_id,"
                " model_version, model_adapter_version, tool_spec_hashes, policy_version"
                " FROM execution_manifests"
                " WHERE run_id = %s AND tenant_id = %s AND project_id = %s",
                tenancy,
            ).fetchone()
        return {
            "run": run,
            "messages": [
                {"sequence_no": m[0], "role": m[1], "content": m[2],
                 "tool_calls": m[3], "tool_results": m[4]}
                for m in messages
            ],
            "events": [
                {"sequence_no": e[0], "event_type": e[1], "payload": e[2], "timestamp": e[3]}
                for e in events
            ],
            "manifest": manifest,
        }


# --- artifacts (FR-54, FR-55) --------------------------------------------------------


async def _write_then_honour_cancellation(function: Any, *args: Any) -> Any:
    """A write on a worker thread that, once started, finishes before a cancellation
    of the caller is reported.

    Cancelling an asyncio.to_thread call does not stop a thread already running it
    and drops one still queued (KNOWLEDGE-e22f787f), so a caller that gave up could
    see a row appear after it moved on, or not appear at all. The write is shielded
    and awaited to its end, and CancelledError is then raised as the caller asked.
    Unlike a run's store calls (RunControl.store), the cancellation is not absorbed:
    an artifact store has no checkpoint to stop at later.
    """
    future = asyncio.ensure_future(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                continue
            except Exception:  # noqa: BLE001 - the cancellation is what the caller hears
                break
        raise


_ARTIFACT_COLUMNS = (
    "artifact_id, tenant_id, project_id, uri, mime_type, content_hash, size, created_by_agent,"
    " source_run, source_task, provenance, classification, created_at, expires_at"
)


def _artifact_from_row(row: tuple) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=str(row[0]),
        tenant_id=row[1],
        project_id=row[2],
        uri=row[3],
        mime_type=row[4],
        content_hash=row[5],
        size=int(row[6]),
        created_by_agent=row[7],
        source_run=str(row[8]) if row[8] is not None else None,
        source_task=row[9],
        provenance=_provenance_from_json(row[10]),
        classification=row[11],
        created_at=row[12],
        expires_at=row[13],
    )


class PostgresArtifactStore:
    """FR-55's durable store: the artifacts table (migration 0006), bound to one
    tenant and project.

    Every statement is filtered by the store's tenant and project, and every read
    also by expiry, so another scope's artifact, a deleted one, an expired one and
    an unknown id are the same "no row". I/O runs on worker threads through the
    pool (FR-20), and content is read through a binary cursor, so a 10 MiB read
    does not travel as hex (KNOWLEDGE-b0e097e4).
    """

    def __init__(
        self,
        dsn: str,
        tenant_id: str,
        project_id: str,
        *,
        max_content_bytes: int = DEFAULT_MAX_CONTENT_BYTES,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        refuse_bad_scope(tenant_id, project_id)
        self._dsn = dsn
        self._tenant_id = tenant_id
        self._project_id = project_id
        self._cap = checked_cap(max_content_bytes)
        self._clock = clock if clock is not None else utc_now

    def for_scope(self, tenant_id: str, project_id: str) -> PostgresArtifactStore:
        """A store over the same database, bound to another tenant and project."""
        return PostgresArtifactStore(
            self._dsn, tenant_id, project_id, max_content_bytes=self._cap, clock=self._clock
        )

    async def put(
        self,
        content: bytes,
        *,
        mime_type: str,
        provenance: ContentProvenance,
        created_by_agent: str,
        source_run: str | None = None,
        classification: str | None = None,
        expires_at: datetime | None = None,
        source_task: str | None = None,
    ) -> ArtifactRef:
        ref, data = prepare_put(
            content,
            tenant_id=self._tenant_id,
            project_id=self._project_id,
            mime_type=mime_type,
            provenance=provenance,
            created_by_agent=created_by_agent,
            source_run=source_run,
            classification=classification,
            expires_at=expires_at,
            now=self._clock(),
            max_content_bytes=self._cap,
            source_task=source_task,
        )
        try:
            await _write_then_honour_cancellation(self._insert, ref, data)
        except asyncio.CancelledError:
            # The caller never receives this ref, so nothing could reach the artifact
            # by its id: a cancelled put takes back what it wrote, shielded the same
            # way. If the removal fails too, the row stays, and the cancellation is
            # still what the caller hears.
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await _write_then_honour_cancellation(self._remove, ref.artifact_id)
            raise
        return ref

    def _remove(self, artifact_id: str) -> None:
        with _checkout(self._dsn) as conn:
            conn.execute(
                "DELETE FROM artifacts WHERE artifact_id = %s AND tenant_id = %s AND project_id = %s",
                (artifact_id, self._tenant_id, self._project_id),
            )

    def _insert(self, ref: ArtifactRef, data: bytes) -> None:
        with _checkout(self._dsn) as conn:
            cursor = conn.execute(
                """
                INSERT INTO artifacts (
                    artifact_id, tenant_id, project_id, uri, mime_type, content_hash, size,
                    created_by_agent, source_run, source_task, provenance, classification,
                    created_at, expires_at, content
                )
                SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s::uuid, %s, %s, %s, %s, %s, %s
                -- A source run must belong to this artifact's own tenant and project,
                -- checked in the same statement as the insert, as start_run checks a
                -- parent run (FR-21, FR-55).
                WHERE %s::uuid IS NULL
                   OR EXISTS (
                        SELECT 1 FROM runs r
                        WHERE r.run_id = %s::uuid AND r.tenant_id = %s AND r.project_id = %s
                      )
                """,
                (
                    ref.artifact_id,
                    ref.tenant_id,
                    ref.project_id,
                    ref.uri,
                    ref.mime_type,
                    ref.content_hash,
                    ref.size,
                    ref.created_by_agent,
                    ref.source_run,
                    ref.source_task,
                    Jsonb(_provenance_to_json(ref.provenance)),
                    ref.classification,
                    ref.created_at,
                    ref.expires_at,
                    data,
                    ref.source_run,
                    ref.source_run,
                    ref.tenant_id,
                    ref.project_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("source_run must be a run of this store's tenant and project")

    def _select(self, key: str, now: datetime, with_content: bool) -> tuple | None:
        columns = _ARTIFACT_COLUMNS + (", content" if with_content else "")
        with _checkout(self._dsn) as conn:
            with conn.cursor(binary=True) as cursor:
                return cursor.execute(
                    f"SELECT {columns} FROM artifacts"
                    " WHERE artifact_id = %s AND tenant_id = %s AND project_id = %s"
                    " AND (expires_at IS NULL OR expires_at > %s)",
                    (key, self._tenant_id, self._project_id, now),
                ).fetchone()

    async def _visible(self, artifact_id: Any, with_content: bool) -> tuple:
        key = canonical_id(artifact_id)
        row = None if key is None else await asyncio.to_thread(self._select, key, self._clock(), with_content)
        if row is None:
            raise not_found(artifact_id)
        return row

    async def get(self, artifact_id: str) -> bytes:
        row = await self._visible(artifact_id, with_content=True)
        content = bytes(row[14])
        if hashlib.sha256(content).hexdigest() != row[5]:
            raise integrity_error(artifact_id)
        return content

    async def metadata(self, artifact_id: str) -> ArtifactRef:
        return _artifact_from_row(await self._visible(artifact_id, with_content=False))

    def _delete(self, key: str, now: datetime) -> int:
        with _checkout(self._dsn) as conn:
            return conn.execute(
                "DELETE FROM artifacts WHERE artifact_id = %s AND tenant_id = %s AND project_id = %s"
                " AND (expires_at IS NULL OR expires_at > %s)",
                (key, self._tenant_id, self._project_id, now),
            ).rowcount

    async def delete(self, artifact_id: str) -> None:
        key = canonical_id(artifact_id)
        removed = 0 if key is None else await _write_then_honour_cancellation(self._delete, key, self._clock())
        if removed != 1:
            raise not_found(artifact_id)

    def _expire(self, now: datetime) -> int:
        with _checkout(self._dsn) as conn:
            return conn.execute(
                "DELETE FROM artifacts WHERE tenant_id = %s AND project_id = %s AND expires_at <= %s",
                (self._tenant_id, self._project_id, now),
            ).rowcount

    async def expire(self) -> int:
        return await _write_then_honour_cancellation(self._expire, self._clock())


class PostgresRunStateStore:
    """FR-65, FR-66's durable RunStateStore: plan_versions and plan_node_states
    (migration 0007), bound to one tenant and project.

    Every statement is filtered by the store's tenant and project, and they lead every
    key, so another scope's plan and an unknown one are the same "not found", and a
    plan id another scope uses neither blocks nor reveals anything (DECISION-6d073ac0,
    F1). A version is only ever inserted, never updated; its document is stored as the
    exact text it was hashed over, and a row whose text no longer hashes to its stored
    plan_hash is refused on read with PlanIntegrityError (F3). A transition locks its
    node's row, so two transitions of one node cannot both leave a final status; the
    event is emitted after the row is committed, on the same worker thread, through the
    run's own sink. I/O runs on worker threads through the pool (FR-20).
    """

    def __init__(self, dsn: str, tenant_id: str, project_id: str) -> None:
        refuse_bad_scope(tenant_id, project_id)
        self._dsn = dsn
        self._tenant_id = tenant_id
        self._project_id = project_id

    def for_scope(self, tenant_id: str, project_id: str) -> PostgresRunStateStore:
        """A store over the same database, bound to another tenant and project."""
        return PostgresRunStateStore(self._dsn, tenant_id, project_id)

    def _scope(self) -> tuple[str, str]:
        return self._tenant_id, self._project_id

    async def put_plan(self, plan: PlanVersion) -> None:
        if not isinstance(plan, PlanVersion):
            raise InvalidPlan(f"put_plan needs a PlanVersion, got {type(plan).__name__}")
        await _write_then_honour_cancellation(self._insert_plan, plan)

    def _insert_plan(self, plan: PlanVersion) -> None:
        tenant_id, project_id = self._scope()
        with _checkout(self._dsn) as conn, conn.transaction():
            # One plan at a time per scope and plan id, so two runs cannot both find the
            # id unclaimed and both store under it. A hash collision only serialises.
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (json.dumps([tenant_id, project_id, plan.plan_id]),),
            )
            if conn.execute(
                "SELECT 1 FROM runs WHERE run_id = %s AND tenant_id = %s AND project_id = %s",
                (plan.run_id, tenant_id, project_id),
            ).fetchone() is None:
                raise InvalidPlan(f"run {plan.run_id} is not a run of this store's tenant and project")
            parent_id, parent_version = plan.parent_plan if plan.parent_plan is not None else (None, None)
            # The parent first: it is what the caller named.
            if parent_id is not None and conn.execute(
                "SELECT 1 FROM plan_versions WHERE plan_id = %s AND version = %s AND run_id = %s"
                " AND tenant_id = %s AND project_id = %s",
                (parent_id, parent_version, plan.run_id, tenant_id, project_id),
            ).fetchone() is None:
                raise InvalidPlan(f"parent_plan {plan.parent_plan} is not a stored version of this run's plans")
            owner = conn.execute(
                "SELECT run_id FROM plan_versions WHERE tenant_id = %s AND project_id = %s AND plan_id = %s LIMIT 1",
                (tenant_id, project_id, plan.plan_id),
            ).fetchone()
            if owner is not None and str(owner[0]) != plan.run_id:
                raise InvalidPlan(f"plan {plan.plan_id} belongs to run {owner[0]}, not run {plan.run_id}")
            inserted = conn.execute(
                "INSERT INTO plan_versions (tenant_id, project_id, plan_id, version, run_id,"
                " parent_plan_id, parent_version, plan_hash, document, created_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (tenant_id, project_id, plan_id, version) DO NOTHING RETURNING 1",
                (tenant_id, project_id, plan.plan_id, plan.version, plan.run_id, parent_id, parent_version,
                 plan.plan_hash, canonical_text(plan.to_document()), plan.created_at),
            ).fetchone()
            if inserted is None:
                raise InvalidPlan(f"plan {plan.plan_id} version {plan.version} is already stored")
            with conn.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO plan_node_states (plan_id, version, node_id, run_id, tenant_id, project_id, status)"
                    " VALUES (%s, %s, %s, %s, %s, %s, 'pending')",
                    [(plan.plan_id, plan.version, item.node_id, plan.run_id, tenant_id, project_id) for item in plan.nodes],
                )

    @staticmethod
    def _addressable(plan_id: Any, version: Any) -> bool:
        return type(plan_id) is str and column_rejection_reason(plan_id, "UUID") is None and type(version) is int

    async def get_plan(self, plan_id: str, version: int) -> PlanVersion:
        rows = await asyncio.to_thread(self._select_versions, plan_id, version)
        if not rows:
            raise PlanNotFound(f"no plan {plan_id} version {version} in this scope")
        return rows[0]

    async def versions(self, plan_id: str) -> tuple[PlanVersion, ...]:
        return tuple(await asyncio.to_thread(self._select_versions, plan_id, None))

    def _select_versions(self, plan_id: Any, version: int | None) -> list[PlanVersion]:
        if not self._addressable(plan_id, 1 if version is None else version):
            return []
        sql = (
            "SELECT version, run_id, parent_plan_id, parent_version, document, created_at, plan_hash"
            " FROM plan_versions WHERE plan_id = %s AND tenant_id = %s AND project_id = %s"
        )
        params: tuple[Any, ...] = (plan_id, *self._scope())
        if version is not None:
            sql, params = sql + " AND version = %s", params + (version,)
        with _checkout(self._dsn) as conn:
            rows = conn.execute(sql + " ORDER BY version", params).fetchall()
        plans = []
        for number, run_id, parent_id, parent_version, document, created_at, stored_hash in rows:
            try:
                plan = plan_from_document(
                    json.loads(document), plan_id=plan_id, version=number, run_id=str(run_id),
                    parent_plan=None if parent_id is None else (str(parent_id), parent_version),
                    created_at=created_at,
                )
            except (ValueError, InvalidPlan) as exc:
                raise PlanIntegrityError(
                    f"plan {plan_id} version {number} is no longer a valid plan: {type(exc).__name__}"
                ) from None
            if plan.plan_hash != stored_hash:
                raise PlanIntegrityError(f"plan {plan_id} version {number} does not match the hash it was stored with")
            plans.append(plan)
        return plans

    async def node_states(self, plan_id: str, version: int) -> Mapping[str, str]:
        rows = await asyncio.to_thread(self._select_states, plan_id, version)
        if not rows:
            # Every stored version has at least one node, so no rows is no plan.
            raise PlanNotFound(f"no plan {plan_id} version {version} in this scope")
        return MappingProxyType(dict(rows))

    def _select_states(self, plan_id: Any, version: Any) -> list[tuple[str, str]]:
        if not self._addressable(plan_id, version):
            return []
        with _checkout(self._dsn) as conn:
            return conn.execute(
                "SELECT node_id, status FROM plan_node_states"
                " WHERE plan_id = %s AND version = %s AND tenant_id = %s AND project_id = %s ORDER BY node_id",
                (plan_id, version, *self._scope()),
            ).fetchall()

    async def transition(
        self, plan_id: str, version: int, node_id: str, status: str, *, sink: EventSink, reason: str | None = None
    ) -> None:
        checked_status(status)
        await _write_then_honour_cancellation(self._transition, plan_id, version, node_id, status, sink, reason)

    def _transition(
        self, plan_id: Any, version: Any, node_id: Any, status: str, sink: EventSink, reason: str | None = None
    ) -> None:
        if not self._addressable(plan_id, version) or type(node_id) is not str:
            raise PlanNotFound(f"no node {node_id!r} in plan {plan_id} version {version} in this scope")
        with _checkout(self._dsn) as conn, conn.transaction():
            row = conn.execute(
                "SELECT status, run_id FROM plan_node_states"
                " WHERE plan_id = %s AND version = %s AND node_id = %s AND tenant_id = %s AND project_id = %s"
                " FOR UPDATE",
                (plan_id, version, node_id, *self._scope()),
            ).fetchone()
            if row is None:
                raise PlanNotFound(f"no node {node_id!r} in plan {plan_id} version {version} in this scope")
            refuse_foreign_sink(sink, *self._scope(), str(row[1]))
            refuse_transition(node_id, row[0], status)
            conn.execute(
                "UPDATE plan_node_states SET status = %s, updated_at = now()"
                " WHERE plan_id = %s AND version = %s AND node_id = %s AND tenant_id = %s AND project_id = %s",
                (status, plan_id, version, node_id, *self._scope()),
            )
            # Before the commit, while the row lock is held: a racing transition of the
            # same node waits for this event, so the run's events are in the order of the
            # node's rows. After the commit, two racing transitions could record
            # Finished before Started (M16 round 1, F4). An emit that fails rolls the
            # status back with it.
            emit_transition(sink, plan_id, version, node_id, status, reason)


# --- source versions (FR-89, FR-90, M22) ------------------------------------------------------

_VERSION_COLUMNS = (
    "source_version_id, canonical_uri, final_uri, retrieval_time, content_hash, artifact_id, media_type,"
    " cache_scope, auth_scope_hash, prior_version, provenance, tenant_id, project_id, source_run"
)


def _version_from_row(row: tuple) -> Any:
    from .evidence import CacheScope, EvidenceSourceVersion

    return EvidenceSourceVersion(
        source_version_id=str(row[0]), canonical_uri=row[1], final_uri=row[2], retrieval_time=row[3],
        content_hash=row[4], artifact_id=str(row[5]), media_type=row[6], cache_scope=CacheScope(row[7]),
        auth_scope_hash=row[8], prior_version=None if row[9] is None else str(row[9]),
        provenance=_provenance_from_json(row[10]), tenant_id=row[11], project_id=row[12], source_run=str(row[13]),
    )


class PostgresEvidenceStore:
    """FR-89's durable store: the source_versions table (migration 0010).

    Insert-only. A read by id is filtered by the reader's tenant and project. The cache
    lookup is the one statement that reads another tenant's row, and only a
    PUBLIC_GLOBAL one or a same-tenant TENANT one, for the copy FR-90 makes of it
    (NFR-25). Synchronous: callers on an event loop use a worker thread (FR-81).
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def record(self, version: Any, *, ledger_run_id: str, request_variant: str | None = None) -> None:
        from .evidence import EvidenceSourceVersion, _uuid

        if not isinstance(version, EvidenceSourceVersion):
            raise ValueError(f"version must be an EvidenceSourceVersion, got {type(version).__name__}")
        ledger = _uuid("ledger_run_id", ledger_run_id)
        v = version
        with _checkout(self._dsn) as conn:
            conn.execute(
                f"INSERT INTO source_versions ({_VERSION_COLUMNS}, ledger_run_id, request_variant)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (v.source_version_id, v.canonical_uri, v.final_uri, v.retrieval_time, v.content_hash,
                 v.artifact_id, v.media_type, v.cache_scope.value, v.auth_scope_hash, v.prior_version,
                 Jsonb(_provenance_to_json(v.provenance)), v.tenant_id, v.project_id, v.source_run,
                 ledger, request_variant),
            )

    def get(self, tenant_id: str, project_id: str, source_version_id: str) -> Any:
        from .artifacts import canonical_id

        key = canonical_id(source_version_id) if isinstance(source_version_id, str) else None
        row = None
        if key is not None:
            with _checkout(self._dsn) as conn:
                row = conn.execute(
                    f"SELECT {_VERSION_COLUMNS} FROM source_versions"
                    " WHERE source_version_id = %s AND tenant_id = %s AND project_id = %s",
                    (key, tenant_id, project_id),
                ).fetchone()
        if row is None:
            raise LookupError(f"no source version {source_version_id!r} in this tenant and project")
        return _version_from_row(row)

    def reachable(self, key: Any, *, tenant_id: str, project_id: str, ledger_run_id: str) -> list[Any]:
        with _checkout(self._dsn) as conn:
            rows = conn.execute(
                f"SELECT {_VERSION_COLUMNS} FROM source_versions"
                " WHERE canonical_uri = %s AND auth_scope_hash IS NOT DISTINCT FROM %s"
                " AND request_variant IS NOT DISTINCT FROM %s AND ("
                "   cache_scope = 'public_global'"
                "   OR (cache_scope = 'tenant' AND tenant_id = %s)"
                "   OR (cache_scope = 'project' AND tenant_id = %s AND project_id = %s)"
                "   OR (cache_scope = 'session' AND tenant_id = %s AND project_id = %s AND ledger_run_id = %s))"
                " ORDER BY retrieval_time DESC, recorded_at DESC",
                (key.canonical_uri, key.auth_scope_hash, key.request_variant, tenant_id, tenant_id, project_id,
                 tenant_id, project_id, ledger_run_id),
            ).fetchall()
        return [_version_from_row(row) for row in rows]


# --- the evidence ledger (FR-92 to FR-94, M23) ----------------------------------------------

_LOCK_CLAIMS = 3
_CLAIM_COLUMNS = "claim_id, ledger_run_id, statement, claim_key, supersedes, tenant_id, project_id"


def _claim_from_row(row: tuple) -> Any:
    from .ledger import EvidenceClaim

    return EvidenceClaim(
        claim_id=str(row[0]), ledger_run_id=str(row[1]), statement=row[2], claim_key=row[3],
        supersedes=None if row[4] is None else str(row[4]), tenant_id=row[5], project_id=row[6],
    )


class PostgresLedgerStore:
    """FR-92's durable store: claims, contributions and citations (migration 0011).

    Insert-only. Every statement is filtered by tenant, project and ledger. A record or
    supersession checks and writes in one transaction under an advisory lock on the
    ledger and the statement's key (DECISION-9de80b77), and UNIQUE (supersedes) keeps the
    chain from forking even without it. Synchronous: callers on an event loop use a
    worker thread (FR-81).
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    @staticmethod
    def _lock(conn: Any, tenant_id: str, project_id: str, ledger_run_id: str, *keys: str) -> list[int]:
        """The keys' advisory locks, taken in the order of the lock values themselves,
        each once: two keys whose hashes collide are one lock, so ordering by the keys'
        text could cross two writers' orders and deadlock (M23 review caveat 2)."""
        from .claimlock import lock_key

        # Encoded as claim-lock keys are, so no two ledgers' names collide by a delimiter
        # (M24a review caveat P1).
        names = [lock_key(tenant_id, project_id, ledger_run_id, key) for key in keys]
        values = [row[0] for row in conn.execute(
            "SELECT DISTINCT hashtext(name) AS h FROM unnest(%s::text[]) AS name ORDER BY h", (names,)).fetchall()]
        for value in values:
            conn.execute("SELECT pg_advisory_xact_lock(%s, %s)", (value, _LOCK_CLAIMS))
        return values

    @staticmethod
    def _checked(conn: Any, tenant_id: str, project_id: str, contribution: Any) -> Any:
        """M23 review caveat 1: every cited version in the claim's tenant and project, and
        the contribution at least as tainted as each, whoever built it."""
        from .ledger import _SOURCE_URN, ClaimRefused, _covered

        cited = list(dict.fromkeys(contribution.supporting + contribution.contradicting))
        found = {}
        if cited:
            found = {str(row[0]): _provenance_from_json(row[1]) for row in conn.execute(
                "SELECT source_version_id, provenance FROM source_versions WHERE source_version_id = ANY(%s::uuid[])"
                " AND tenant_id = %s AND project_id = %s", (cited, tenant_id, project_id)).fetchall()}
        for vid in cited:
            if vid not in found:
                raise ClaimRefused(f"cited source {_SOURCE_URN}{vid} is not a source version in this tenant and project")
        return _covered(contribution, [found[v] for v in cited])

    @staticmethod
    def _active(conn: Any, tenant_id: str, project_id: str, ledger_run_id: str, key: str) -> Any:
        row = conn.execute(
            f"SELECT {_CLAIM_COLUMNS} FROM claims c WHERE tenant_id = %s AND project_id = %s"
            " AND ledger_run_id = %s AND claim_key = %s"
            " AND NOT EXISTS (SELECT 1 FROM claims s WHERE s.supersedes = c.claim_id)",
            (tenant_id, project_id, ledger_run_id, key),
        ).fetchone()
        return None if row is None else _claim_from_row(row)

    @staticmethod
    def _contribute(conn: Any, claim: Any, new: Any) -> Any:
        from .ledger import _contribution

        made = _contribution(new, claim.claim_id, datetime.now(timezone.utc))
        conn.execute(
            "INSERT INTO claim_contributions (contribution_id, claim_id, tenant_id, project_id, run_id, node_id,"
            " agent_id, confidence, provenance, why, recorded_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (made.contribution_id, claim.claim_id, claim.tenant_id, claim.project_id, made.run_id, made.node_id,
             made.agent_id, made.confidence, Jsonb(_provenance_to_json(made.provenance)), made.why, made.recorded_at),
        )
        for stance in ("supporting", "contradicting"):
            for position, version in enumerate(getattr(made, stance)):
                conn.execute(
                    "INSERT INTO claim_citations (contribution_id, stance, position, source_version_id)"
                    " VALUES (%s, %s, %s, %s)", (made.contribution_id, stance, position, version))
        return made

    @staticmethod
    def _insert_claim(conn: Any, claim: Any) -> None:
        conn.execute(
            f"INSERT INTO claims ({_CLAIM_COLUMNS}) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (claim.claim_id, claim.ledger_run_id, claim.statement, claim.claim_key, claim.supersedes,
             claim.tenant_id, claim.project_id),
        )

    def record(self, *, tenant_id: str, project_id: str, ledger_run_id: str, statement: str,
               contribution: Any) -> tuple[Any, Any, bool]:
        from .ledger import MAX_STATEMENT, EvidenceClaim, _text, _uuid, claim_key

        key = claim_key(_text("statement", statement, limit=MAX_STATEMENT))
        ledger = _uuid("ledger_run_id", ledger_run_id)
        with _checkout(self._dsn) as conn:
            with conn.transaction():
                contribution = self._checked(conn, tenant_id, project_id, contribution)
                self._lock(conn, tenant_id, project_id, ledger, key)
                claim = self._active(conn, tenant_id, project_id, ledger, key)
                new = claim is None
                if new:
                    claim = EvidenceClaim(str(uuid.uuid4()), ledger, statement, key, None, tenant_id, project_id)
                    self._insert_claim(conn, claim)
                made = self._contribute(conn, claim, contribution)
        return claim, made, new

    def supersede(self, *, tenant_id: str, project_id: str, ledger_run_id: str, old_claim_id: str,
                  statement: str, contribution: Any) -> tuple[Any, Any]:
        from .ledger import _CLAIM_URN, MAX_STATEMENT, ClaimRefused, EvidenceClaim, _text, _uuid, claim_key

        key = claim_key(_text("statement", statement, limit=MAX_STATEMENT))
        ledger = _uuid("ledger_run_id", ledger_run_id)
        with _checkout(self._dsn) as conn:
            with conn.transaction():
                contribution = self._checked(conn, tenant_id, project_id, contribution)
                row = conn.execute(
                    f"SELECT {_CLAIM_COLUMNS} FROM claims WHERE claim_id = %s AND tenant_id = %s"
                    " AND project_id = %s AND ledger_run_id = %s", (old_claim_id, tenant_id, project_id, ledger),
                ).fetchone()
                if row is None:
                    raise ClaimRefused(f"no claim {_CLAIM_URN}{old_claim_id} in this ledger")
                old = _claim_from_row(row)
                self._lock(conn, tenant_id, project_id, ledger, old.claim_key, key)
                after = conn.execute("SELECT claim_id FROM claims WHERE supersedes = %s", (old.claim_id,)).fetchone()
                if after is not None:
                    raise ClaimRefused(f"claim {old.uri} is already superseded by {_CLAIM_URN}{after[0]}")
                other = self._active(conn, tenant_id, project_id, ledger, key)
                if other is not None and other.claim_id != old.claim_id:
                    raise ClaimRefused(f"the new statement matches active claim {other.uri}")
                claim = EvidenceClaim(str(uuid.uuid4()), ledger, statement, key, old.claim_id, tenant_id, project_id)
                self._insert_claim(conn, claim)
                made = self._contribute(conn, claim, contribution)
        return claim, made

    def claim(self, tenant_id: str, project_id: str, ledger_run_id: str, claim_id: str) -> tuple[Any, Any, list[Any]]:
        """One claim, what superseded it, and its contributions (M23 review caveat 5)."""
        from .ledger import _uuid

        ledger = _uuid("ledger_run_id", ledger_run_id)
        with _checkout(self._dsn) as conn:
            row = conn.execute(
                f"SELECT {_CLAIM_COLUMNS} FROM claims WHERE claim_id = %s AND tenant_id = %s AND project_id = %s"
                " AND ledger_run_id = %s", (claim_id, tenant_id, project_id, ledger)).fetchone()
            if row is None:
                raise LookupError(f"no claim {claim_id!r} in this ledger")
            after = conn.execute("SELECT claim_id FROM claims WHERE supersedes = %s", (claim_id,)).fetchone()
            contributions = self._contributions(conn, tenant_id, project_id, ledger, claim_id)
        return _claim_from_row(row), None if after is None else str(after[0]), contributions

    def rows(self, tenant_id: str, project_id: str, ledger_run_id: str) -> tuple[list[Any], list[Any]]:
        from .ledger import _uuid

        ledger = _uuid("ledger_run_id", ledger_run_id)
        with _checkout(self._dsn) as conn:
            claims = [_claim_from_row(r) for r in conn.execute(
                f"SELECT {_CLAIM_COLUMNS} FROM claims WHERE tenant_id = %s AND project_id = %s AND ledger_run_id = %s"
                " ORDER BY recorded_at, claim_id", (tenant_id, project_id, ledger)).fetchall()]
            contributions = self._contributions(conn, tenant_id, project_id, ledger, None)
        return claims, contributions

    # --- FR-100 (M26): verifications -----------------------------------------------------------

    def append_verifications(self, rows: Any) -> None:
        """Every row in one transaction, each subject checked in its own tenant, project
        and ledger first: a claim of the ledger, or a plan node stored for a run that is
        the ledger's run or descends from it. One refused row refuses them all."""
        from .gate import EvidenceVerification

        rows = list(rows)
        if not all(isinstance(r, EvidenceVerification) for r in rows):
            raise ValueError("append_verifications takes EvidenceVerification rows")
        with _checkout(self._dsn) as conn:
            with conn.transaction():
                for row in rows:
                    self._subject_checked(conn, row)
                    node = None if row.claim_id is not None else row.subject
                    conn.execute(
                        "INSERT INTO verifications (verification_id, tenant_id, project_id, ledger_run_id,"
                        " subject_claim, subject_run, subject_plan, subject_version, subject_node, kind, outcome,"
                        " detail, provenance, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (row.verification_id, row.tenant_id, row.project_id, row.ledger_run_id, row.claim_id,
                         None if node is None else node.run_id, None if node is None else node.plan_id,
                         None if node is None else node.version, None if node is None else node.node_id,
                         row.kind, row.outcome, Jsonb(row.detail), Jsonb(_provenance_to_json(row.provenance)),
                         row.created_at))

    @staticmethod
    def _subject_checked(conn: Any, row: Any) -> None:
        if row.claim_id is not None:
            found = conn.execute(
                "SELECT 1 FROM claims WHERE claim_id = %s AND tenant_id = %s AND project_id = %s"
                " AND ledger_run_id = %s", (row.claim_id, row.tenant_id, row.project_id, row.ledger_run_id),
            ).fetchone()
            if found is None:
                raise LookupError(f"claim {row.subject} is not in ledger {row.ledger_run_id} of this tenant "
                                  "and project")
            return
        node = row.subject
        found = conn.execute(
            "WITH RECURSIVE up (run_id, parent_run_id, depth) AS ("
            "  SELECT run_id, parent_run_id, 0 FROM runs WHERE run_id = %s AND tenant_id = %s AND project_id = %s"
            "  UNION ALL SELECT r.run_id, r.parent_run_id, up.depth + 1 FROM runs r"
            "  JOIN up ON r.run_id = up.parent_run_id"
            "  WHERE r.tenant_id = %s AND r.project_id = %s AND up.depth < 64)"
            " SELECT 1 FROM plan_node_states s WHERE s.tenant_id = %s AND s.project_id = %s AND s.plan_id = %s"
            " AND s.version = %s AND s.node_id = %s AND s.run_id = %s"
            " AND EXISTS (SELECT 1 FROM up WHERE up.run_id = %s)",
            (node.run_id, row.tenant_id, row.project_id, row.tenant_id, row.project_id,
             row.tenant_id, row.project_id, node.plan_id, node.version, node.node_id, node.run_id,
             row.ledger_run_id),
        ).fetchone()
        if found is None:
            raise LookupError(f"node {node} is not a plan node of the runs of ledger {row.ledger_run_id} in this "
                              "tenant and project")

    def verifications(self, tenant_id: str, project_id: str, ledger_run_id: str, *, subject: Any = None,
                      kinds: Any = None) -> list[Any]:
        from .gate import NodeSubject
        from .ledger import _claim_id, _uuid

        where, params = ["ledger_run_id = %s"], [_uuid("ledger_run_id", ledger_run_id)]
        if isinstance(subject, NodeSubject):
            where.append("subject_run = %s AND subject_plan = %s AND subject_version = %s AND subject_node = %s")
            params += [subject.run_id, subject.plan_id, subject.version, subject.node_id]
        elif subject is not None:
            where.append("subject_claim = %s")
            params.append(_claim_id(subject))
        return self._verifications(tenant_id, project_id, where, params, kinds)

    def verifications_since(self, tenant_id: str, project_id: str, since: Any = None, *,
                            kinds: Any = None) -> list[Any]:
        where, params = [], []
        if since is not None:
            where.append("created_at >= %s")
            params.append(since)
        return self._verifications(tenant_id, project_id, where, params, kinds)

    def _verifications(self, tenant_id: str, project_id: str, where: list[str], params: list[Any],
                       kinds: Any) -> list[Any]:
        from .gate import EvidenceVerification, NodeSubject

        if kinds is not None:
            where = [*where, "kind = ANY(%s)"]
            params = [*params, list(kinds)]
        clause = "".join(f" AND {w}" for w in where)
        with _checkout(self._dsn) as conn:
            rows = conn.execute(
                "SELECT verification_id, ledger_run_id, subject_claim, subject_run, subject_plan, subject_version,"
                " subject_node, kind, outcome, detail, provenance, tenant_id, project_id, created_at"
                f" FROM verifications WHERE tenant_id = %s AND project_id = %s{clause}"
                " ORDER BY created_at, verification_id", (tenant_id, project_id, *params)).fetchall()
        return [EvidenceVerification(
            verification_id=str(r[0]), ledger_run_id=str(r[1]),
            subject=f"urn:agentsdk:claim:{r[2]}" if r[2] is not None else NodeSubject(
                run_id=str(r[3]), plan_id=str(r[4]), version=r[5], node_id=r[6]),
            kind=r[7], outcome=r[8], detail=r[9], provenance=_provenance_from_json(r[10]),
            tenant_id=r[11], project_id=r[12], created_at=r[13]) for r in rows]

    @staticmethod
    def _contributions(conn: Any, tenant_id: str, project_id: str, ledger: str, claim_id: str | None) -> list[Any]:
        from .ledger import ClaimContribution

        one = "" if claim_id is None else " AND k.claim_id = %s"
        rows = conn.execute(
            "SELECT k.contribution_id, k.claim_id, k.run_id, k.node_id, k.agent_id, k.confidence, k.provenance,"
            " k.why, k.recorded_at FROM claim_contributions k JOIN claims c ON c.claim_id = k.claim_id"
            " WHERE c.tenant_id = %s AND c.project_id = %s AND c.ledger_run_id = %s"
            f" AND k.tenant_id = %s AND k.project_id = %s{one} ORDER BY k.recorded_at, k.contribution_id",
            (tenant_id, project_id, ledger, tenant_id, project_id) + (() if claim_id is None else (claim_id,)),
        ).fetchall()
        cited: dict[str, dict[str, list[str]]] = {}
        if rows:
            for cid, stance, version in conn.execute(
                "SELECT contribution_id, stance, source_version_id FROM claim_citations"
                " WHERE contribution_id = ANY(%s) ORDER BY contribution_id, stance, position",
                ([r[0] for r in rows],)).fetchall():
                cited.setdefault(str(cid), {}).setdefault(stance, []).append(str(version))
        contributions = []
        for r in rows:
            cid = str(r[0])
            contributions.append(ClaimContribution(
                run_id=str(r[2]), node_id=r[3], agent_id=r[4],
                supporting=tuple(cited.get(cid, {}).get("supporting", ())),
                contradicting=tuple(cited.get(cid, {}).get("contradicting", ())),
                confidence=r[5], provenance=_provenance_from_json(r[6]), why=r[7],
                contribution_id=cid, claim_id=str(r[1]), recorded_at=r[8],
            ))
        return contributions


class PostgresInterruptionStore:
    """FR-112's durable store: interruptions, their decisions and standing decisions
    (migration 0014).

    Insert-only. Every statement is filtered by tenant and project, and every row
    references its run with its own tenant and project, so the schema refuses a row of
    another tenant's run; the store checks first, to say so as LookupError. The unique
    key on interruption_decisions.interruption_id makes the database refuse a second
    decision, raised as InterruptionAlreadyDecided. Synchronous: callers on an event loop
    use a worker thread (FR-81).
    """

    _COLUMNS = ("i.interruption_id, i.run_id, i.top_run_id, i.node_id, i.kind, i.tool_call_id, i.tool_name,"
                " i.agent_id, i.trigger, i.request, i.created_at, i.expires_at, i.tenant_id, i.project_id,"
                " d.decision_id, d.decision, d.source, d.message, d.principal, d.standing_id, d.decided_at")

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    @staticmethod
    def _run_checked(conn: Any, tenant_id: str, project_id: str, *run_ids: str) -> None:
        for run_id in run_ids:
            if conn.execute("SELECT 1 FROM runs WHERE run_id = %s AND tenant_id = %s AND project_id = %s",
                            (run_id, tenant_id, project_id)).fetchone() is None:
                raise LookupError(f"run {run_id} is not a run of this tenant and project")

    def append_interruption(self, item: Any, decision: Any = None) -> None:
        """Records an interruption and, with `decision` -- the standing decision covering it
        (FR-114) -- its decision in the same transaction, so it is never seen pending."""
        from .interruptions import InterruptionDecision, checked_interruption

        item = checked_interruption(item)
        if decision is not None and (not isinstance(decision, InterruptionDecision)
                                     or decision.interruption_id != item.interruption_id):
            raise ValueError("a decision recorded with an interruption must be that interruption's")
        with _checkout(self._dsn) as conn:
            with conn.transaction():
                self._run_checked(conn, item.tenant_id, item.project_id, item.run_id, item.top_run_id)
                conn.execute(
                    "INSERT INTO interruptions (interruption_id, tenant_id, project_id, run_id, top_run_id, node_id,"
                    " kind, tool_call_id, tool_name, agent_id, trigger, request, created_at, expires_at)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (item.interruption_id, item.tenant_id, item.project_id, item.run_id, item.top_run_id,
                     item.node_id, item.kind.value, item.tool_call_id, item.tool_name, item.agent_id, item.trigger,
                     Jsonb(item.request_payload), item.created_at, item.expires_at))
                if decision is not None:
                    self._decision_inserted(conn, decision)

    def append_decision(self, decision: Any) -> None:
        from .interruptions import InterruptionDecision

        if not isinstance(decision, InterruptionDecision):
            raise ValueError(f"a decision must be an InterruptionDecision, got {type(decision).__name__}")
        with _checkout(self._dsn) as conn:
            with conn.transaction():
                self._decision_inserted(conn, decision)

    @staticmethod
    def _decision_inserted(conn: Any, decision: Any) -> None:
        """Checks and inserts one decision inside the caller's transaction."""
        from .interruptions import InterruptionAlreadyDecided, UnknownInterruption

        found = conn.execute(
            "SELECT run_id FROM interruptions WHERE interruption_id = %s AND tenant_id = %s"
            " AND project_id = %s", (decision.interruption_id, decision.tenant_id, decision.project_id),
        ).fetchone()
        if found is None:
            raise UnknownInterruption(
                f"no interruption {decision.interruption_id} in this tenant and project")
        if decision.standing_id is not None and conn.execute(
                "SELECT 1 FROM standing_decisions WHERE standing_id = %s AND run_id = %s AND tenant_id = %s"
                " AND project_id = %s", (decision.standing_id, found[0], decision.tenant_id,
                                         decision.project_id)).fetchone() is None:
            raise LookupError(f"standing decision {decision.standing_id} is not one of this run's")
        inserted = conn.execute(
            "INSERT INTO interruption_decisions (decision_id, interruption_id, tenant_id, project_id,"
            " decision, source, message, principal, standing_id, decided_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (interruption_id) DO NOTHING",
            (decision.decision_id, decision.interruption_id, decision.tenant_id, decision.project_id,
             decision.decision, decision.source, decision.message,
             None if decision.principal is None else Jsonb(decision.principal.to_json()),
             decision.standing_id, decision.decided_at)).rowcount
        if not inserted:
            raise InterruptionAlreadyDecided(f"interruption {decision.interruption_id} is already decided")

    def append_standing(self, standing: Any) -> Any:
        from .interruptions import StandingDecision

        if not isinstance(standing, StandingDecision):
            raise ValueError(f"a standing decision must be a StandingDecision, got {type(standing).__name__}")
        with _checkout(self._dsn) as conn:
            with conn.transaction():
                self._run_checked(conn, standing.tenant_id, standing.project_id, standing.run_id)
                [seq] = conn.execute(
                    "INSERT INTO standing_decisions (standing_id, tenant_id, project_id, run_id, agent_id, tool_name,"
                    " decision, message, principal, created_at, tool_hash) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                    " RETURNING seq",
                    (standing.standing_id, standing.tenant_id, standing.project_id, standing.run_id,
                     standing.agent_id, standing.tool_name, standing.decision, standing.message,
                     Jsonb(standing.principal.to_json()), standing.created_at, standing.tool_hash)).fetchone()
        return dataclasses.replace(standing, seq=seq)

    def _views(self, conn: Any, where: str, params: tuple[Any, ...], now: Any) -> list[Any]:
        from .interruptions import InterruptionDecision, InterruptionView, principal_from_json, state_of
        from .outcomes import InterruptionKind, RunInterruption

        views = []
        for r in conn.execute(
                f"SELECT {self._COLUMNS} FROM interruptions i LEFT JOIN interruption_decisions d"
                f" ON d.interruption_id = i.interruption_id WHERE {where} ORDER BY i.created_at, i.interruption_id",
                params).fetchall():
            item = RunInterruption(
                kind=InterruptionKind(r[4]), run_id=str(r[1]), request_payload=r[9], interruption_id=str(r[0]),
                tool_call_id=r[5], created_at=r[10], tenant_id=r[12], project_id=r[13], top_run_id=str(r[2]),
                node_id=r[3], tool_name=r[6], agent_id=r[7], trigger=r[8], expires_at=r[11])
            decision = None if r[14] is None else InterruptionDecision(
                decision_id=str(r[14]), interruption_id=str(r[0]), tenant_id=r[12], project_id=r[13],
                decision=r[15], source=r[16], message=r[17], principal=principal_from_json(r[18]),
                standing_id=None if r[19] is None else str(r[19]), decided_at=r[20])
            views.append(InterruptionView(item, decision, state_of(item, decision, now)))
        return views

    def interruptions(self, tenant_id: str, project_id: str, *, run_id: str | None = None,
                      pending_only: bool = False, now: Any = None) -> list[Any]:
        from .artifacts import canonical_id

        now = now or datetime.now(timezone.utc)
        with _checkout(self._dsn) as conn:
            if run_id is None:
                views = self._views(conn, "i.tenant_id = %s AND i.project_id = %s", (tenant_id, project_id), now)
            else:
                top = canonical_id(run_id) if isinstance(run_id, str) else None
                if top is None:
                    return []
                # The run and every run beneath it, within this tenant and project.
                views = self._views(
                    conn,
                    "i.tenant_id = %s AND i.project_id = %s AND i.run_id IN (WITH RECURSIVE down (run_id, depth) AS ("
                    " SELECT run_id, 0 FROM runs WHERE run_id = %s AND tenant_id = %s AND project_id = %s"
                    " UNION ALL SELECT r.run_id, down.depth + 1 FROM runs r JOIN down ON r.parent_run_id = down.run_id"
                    " WHERE r.tenant_id = %s AND r.project_id = %s AND down.depth < 64) SELECT run_id FROM down)",
                    (tenant_id, project_id, top, tenant_id, project_id, tenant_id, project_id), now)
        return [v for v in views if v.state == "pending"] if pending_only else views

    def interruption(self, tenant_id: str, project_id: str, interruption_id: str, now: Any = None) -> Any:
        from .artifacts import canonical_id
        from .interruptions import UnknownInterruption

        key = canonical_id(interruption_id) if isinstance(interruption_id, str) else None
        if key is not None:
            with _checkout(self._dsn) as conn:
                views = self._views(conn, "i.interruption_id = %s AND i.tenant_id = %s AND i.project_id = %s",
                                    (key, tenant_id, project_id), now or datetime.now(timezone.utc))
            if views:
                return views[0]
        raise UnknownInterruption(f"no interruption {interruption_id} in this tenant and project")

    def standing(self, tenant_id: str, project_id: str, run_id: str) -> list[Any]:
        from .artifacts import canonical_id
        from .interruptions import StandingDecision, principal_from_json

        key = canonical_id(run_id) if isinstance(run_id, str) else None
        if key is None:
            return []
        with _checkout(self._dsn) as conn:
            rows = conn.execute(
                "SELECT standing_id, run_id, agent_id, tool_name, decision, principal, created_at, message, seq,"
                " tool_hash FROM standing_decisions WHERE tenant_id = %s AND project_id = %s AND run_id = %s"
                " ORDER BY seq", (tenant_id, project_id, key)).fetchall()
        return [StandingDecision(
            standing_id=str(r[0]), run_id=str(r[1]), tenant_id=tenant_id, project_id=project_id, agent_id=r[2],
            tool_name=r[3], decision=r[4], principal=principal_from_json(r[5]), created_at=r[6], message=r[7],
            seq=r[8], tool_hash=r[9]) for r in rows]


class PostgresCatalogStore:
    """FR-127's durable store: the tool versions each tenancy has seen and the hashes it has
    accepted (migration 0017).

    Insert-only: a version already recorded is kept as first seen, and a hash already accepted
    is returned unchanged. Every statement names its tenant and project, and an acceptance
    references its version by the composite key, so the schema refuses one for a version the
    tenancy never saw. Synchronous: callers on an event loop use a worker thread (FR-81).
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    @staticmethod
    def _bounded(conn: Any) -> None:
        """Inside a transaction: each statement of it ends within the bound, so a worker thread
        reading the catalog for a connection never outlives the read it serves (M34)."""
        conn.execute("SET LOCAL statement_timeout = '20s'")

    @staticmethod
    def _version(row: Any) -> Any:
        from .catalog import ToolVersion

        return ToolVersion(tenant_id=row[0], project_id=row[1], kind=row[2], alias=row[3], tool_name=row[4],
                           tool_hash=row[5], entry=row[6], first_seen_at=row[7])

    @staticmethod
    def _acceptance(row: Any) -> Any:
        from .catalog import ToolAcceptance
        from .interruptions import principal_from_json

        return ToolAcceptance(acceptance_id=str(row[0]), tenant_id=row[1], project_id=row[2], alias=row[3],
                              tool_name=row[4], tool_hash=row[5], principal=principal_from_json(row[6]),
                              accepted_at=row[7])

    _VERSION = "tenant_id, project_id, kind, alias, tool_name, tool_hash, entry, first_seen_at"
    _ACCEPTANCE = "acceptance_id, tenant_id, project_id, alias, tool_name, tool_hash, principal, accepted_at"

    def record_versions(self, versions: list[Any]) -> None:
        """Each version a connection has seen, kept as first seen. One transaction."""
        from .catalog import ToolVersion

        for version in versions:
            if not isinstance(version, ToolVersion):
                raise ValueError(f"a version must be a ToolVersion, got {type(version).__name__}")
        with _checkout(self._dsn) as conn:
            with conn.transaction():
                self._bounded(conn)
                for version in versions:
                    conn.execute(
                        f"INSERT INTO mcp_tool_versions ({self._VERSION}) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)"
                        " ON CONFLICT (tenant_id, project_id, alias, tool_hash) DO NOTHING",
                        (version.tenant_id, version.project_id, version.kind, version.alias, version.tool_name,
                         version.tool_hash, Jsonb(version.entry), version.first_seen_at))

    def version(self, tenant_id: str, project_id: str, alias: str, tool_hash: str) -> Any:
        with _checkout(self._dsn) as conn, conn.transaction():
            self._bounded(conn)
            row = conn.execute(
                f"SELECT {self._VERSION} FROM mcp_tool_versions WHERE tenant_id = %s AND project_id = %s"
                " AND alias = %s AND tool_hash = %s", (tenant_id, project_id, alias, tool_hash)).fetchone()
        return None if row is None else self._version(row)

    def accept(self, tenant_id: str, project_id: str, alias: str, tool_hashes: list[str], principal: Any,
               accepted_at: Any = None) -> list[Any]:
        """FR-127: every hash must have a version row in this tenancy, or nothing is recorded;
        a hash already accepted is returned unchanged. One transaction."""
        from .catalog import _decider, checked_hash
        from .errors import UnknownToolVersion

        _decider(principal)
        hashes = [checked_hash(h) for h in tool_hashes]
        accepted_at = accepted_at or datetime.now(timezone.utc)
        with _checkout(self._dsn) as conn:
            with conn.transaction():
                self._bounded(conn)
                names = {}
                for h in hashes:
                    row = conn.execute(
                        "SELECT tool_name FROM mcp_tool_versions WHERE tenant_id = %s AND project_id = %s"
                        " AND alias = %s AND tool_hash = %s", (tenant_id, project_id, alias, h)).fetchone()
                    if row is None:
                        raise UnknownToolVersion(f"no version {h} of a tool of {alias} in this tenant and project")
                    names[h] = row[0]
                for h in hashes:
                    conn.execute(
                        f"INSERT INTO mcp_tool_acceptances ({self._ACCEPTANCE}) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)"
                        " ON CONFLICT (tenant_id, project_id, alias, tool_hash) DO NOTHING",
                        (str(uuid.uuid4()), tenant_id, project_id, alias, names[h], h,
                         Jsonb(principal.to_json()), accepted_at))
                found = self._acceptances(conn, tenant_id, project_id, alias, hashes)
        return [found[h] for h in hashes]

    def _acceptances(self, conn: Any, tenant_id: str, project_id: str, alias: str, hashes: list[str]) -> dict:
        rows = conn.execute(
            f"SELECT {self._ACCEPTANCE} FROM mcp_tool_acceptances WHERE tenant_id = %s AND project_id = %s"
            " AND alias = %s AND tool_hash = ANY(%s)", (tenant_id, project_id, alias, list(hashes))).fetchall()
        return {r[5]: self._acceptance(r) for r in rows}

    def acceptances(self, tenant_id: str, project_id: str, alias: str, tool_hashes: list[str]) -> dict[str, Any]:
        with _checkout(self._dsn) as conn, conn.transaction():
            self._bounded(conn)
            return self._acceptances(conn, tenant_id, project_id, alias, list(tool_hashes))

    def last_accepted(self, tenant_id: str, project_id: str, alias: str, tool_names: list[str],
                      kind: str = "tool") -> dict[str, Any]:
        """Each named entry's most recently accepted version of that kind, for those with one: a
        prompt and a tool of one name are never told from each other (M35)."""
        columns = ", ".join(f"v.{c.strip()}" for c in self._VERSION.split(","))
        with _checkout(self._dsn) as conn, conn.transaction():
            self._bounded(conn)
            rows = conn.execute(
                f"SELECT DISTINCT ON (a.tool_name) {columns} FROM mcp_tool_acceptances a"
                " JOIN mcp_tool_versions v USING (tenant_id, project_id, alias, tool_hash)"
                " WHERE a.tenant_id = %s AND a.project_id = %s AND a.alias = %s AND a.tool_name = ANY(%s)"
                " AND v.kind = %s ORDER BY a.tool_name, a.seq DESC",
                (tenant_id, project_id, alias, list(tool_names), kind)).fetchall()
        return {r[4]: self._version(r) for r in rows}
