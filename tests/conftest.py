"""Checks that span the whole test session.

AC-44 / NFR-16 (M11): a full regression run leaves the store as it found it.
Before Phase 2 the golden-eval tests left 13 runs behind on every full run --
measured on 2026-09-14 against the M11 baseline -- and each file's own cleanup
could not see what another file left. So the set of run ids in every run-scoped
table is taken when the session starts and compared when it ends: a run added,
or one removed that the suite did not write, fails the session.

With no database configured this fails rather than skips (AC-19's rule): a
check that skips to green proves nothing.

FR-81 (M18a): every store call made from a coroutine goes to a worker thread
(FR-20, DECISION-6b62d1f5), asserted of every test rather than of a list of run
paths. That list missed a call site three times (KNOWLEDGE-c0f23fea).
"""

from __future__ import annotations

import asyncio
import os
import sys

import psycopg
import pytest
from dotenv import load_dotenv

from agentsdk.config import normalise_database_url
from agentsdk import postgres
from agentsdk.postgres import apply_schema

load_dotenv()

_DSN = normalise_database_url(os.environ.get("DATABASE_URL"))
# Each table, and the id column whose set of values the session must leave as it
# found it. From M13 that includes every artifact, which a run's cleanup would
# otherwise not see, and from M16 every stored plan version and node state (AC-63).
_TRACKED = (
    ("runs", "run_id"),
    ("messages", "run_id"),
    ("run_events", "run_id"),
    ("execution_manifests", "run_id"),
    ("artifacts", "artifact_id"),
    ("plan_versions", "run_id"),
    ("plan_node_states", "run_id"),
    ("source_versions", "source_version_id"),
)
_RUN_TABLES = tuple(table for table, _ in _TRACKED)


def _run_ids() -> dict[str, set]:
    with psycopg.connect(_DSN) as conn:
        found = {}
        for table, column in _TRACKED:
            # A table a pending migration has not created yet has nothing to compare.
            if conn.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0] is None:
                found[table] = set()
                continue
            found[table] = {row[0] for row in conn.execute(f"SELECT DISTINCT {column} FROM {table}").fetchall()}
        return found


@pytest.fixture(scope="session", autouse=True)
def the_store_is_left_as_it_was_found():
    assert _DSN, "AC-44 compares the store before and after the session: DATABASE_URL must be set"
    # So the tables exist to be read on a database this session is the first to use.
    apply_schema(_DSN)
    before = _run_ids()
    yield
    after = _run_ids()
    changed = {
        table: {"added": len(after[table] - before[table]), "removed": len(before[table] - after[table])}
        for table in _RUN_TABLES
        if after[table] != before[table]
    }
    assert not changed, f"the test session changed the store's runs: {changed}"


def _store_method() -> str:
    """The postgres.py method that asked for a connection, or else whoever did."""
    frame = sys._getframe(2)
    caller = frame
    while frame is not None:
        if frame.f_code.co_filename == postgres.__file__ and frame.f_code.co_name != "_checkout":
            return frame.f_code.co_qualname
        frame = frame.f_back
    return f"{caller.f_code.co_qualname} ({caller.f_code.co_filename}:{caller.f_lineno})"


class _LoopWatchedPool:
    """A real pool that notes every checkout asked for from a thread running an event loop."""

    def __init__(self, pool, on_loop: list[str]) -> None:
        self._pool, self._on_loop = pool, on_loop

    def connection(self, *args, **kwargs):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass  # a worker thread, or a synchronous test
        else:
            self._on_loop.append(_store_method())
        return self._pool.connection(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._pool, name)


@pytest.fixture(autouse=True)
def no_store_call_runs_on_the_event_loop(request):
    """FR-81: fails any test that checks out a connection on a thread running a loop.

    It records rather than raises, and fails at teardown: a run's total boundary
    turns an exception into a failed RunResult, and a test that never looks at the
    status would pass. FR-82's refusal is turned on here as well, so the same call
    also fails where it is made -- two checks that do not depend on each other
    (KNOWLEDGE-aa97d748). Its own MonkeyPatch, so a test that undoes its own
    patches does not undo this one.
    """
    on_loop: list[str] = []
    real_pool = postgres._pool
    watched: dict[int, _LoopWatchedPool] = {}

    def watched_pool(dsn):
        # One wrapper per pool, so "one pool per DSN" still holds by identity.
        pool = real_pool(dsn)
        return watched.setdefault(id(pool), _LoopWatchedPool(pool, on_loop))

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(postgres, "_pool", watched_pool)
        patch.setattr(postgres, "REFUSE_ON_EVENT_LOOP", True)
        yield on_loop
    if on_loop:
        pytest.fail(
            f"{request.node.nodeid}: store I/O ran on the event loop thread, from "
            f"{', '.join(sorted(set(on_loop)))} (FR-20, FR-81)",
            pytrace=False,
        )
