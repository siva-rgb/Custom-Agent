"""M22 gate: source versions and the scoped resource cache (FR-89 to FR-91, NFR-25, AC-71, AC-72).

Every fetch inside a Runner records an immutable EvidenceSourceVersion, its content an
M13 artifact, and the run's resource cache serves a fresh version to any reader within
its scope's reach without a request, copying it into the reader's own tenant and project
when it was stored outside them. The ledger run id FR-92 names is pulled forward so
SESSION reaches the top-level run (DECISION-0d384c8d).

Requests are counted at a local server (AC-30's seam); time is the fetch tool's clock seam.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from agentsdk import AgentSpec, Persistence, RunConfig, RunStatus, Runner
from agentsdk import migrate
from agentsdk.builtin_tools import fetch_tool
from agentsdk.config import normalise_database_url
from agentsdk.evidence import (
    CacheScope,
    EvidenceSourceVersion,
    InMemoryEvidenceStore,
    ResourceCacheKey,
    canonical_uri,
)
from agentsdk.events import EventType
from agentsdk.migrate import apply_migrations
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.postgres import SCHEMA_PATH, PostgresEvidenceStore, RunScope
from agentsdk.primitives import ContentProvenance, Message, Role, ToolCall
from agentsdk.tools import ResultProvenance

DSN = normalise_database_url(os.environ.get("DATABASE_URL"))
TENANT, PROJECT = "SYN-m22", "p-m22"
OTHER_TENANT, OTHER_PROJECT = "SYN-m22-other", "p-m22-other"
PUBLIC = "1.1.1.1"
T0 = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
STORES = ["memory", "postgres"]


# =================================================================================================
# The seams: a counting server, the network mapping, a clock, a scripted model
# =================================================================================================


class Pages:
    """A local HTTP server answering by path, counting the requests each path gets."""

    def __init__(self, table):
        self.table, self.requests = table, {}

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
            path = head.split(" ", 2)[1]
            self.requests[path] = self.requests.get(path, 0) + 1
            status, body, kind = self.table[path]
            if callable(body):
                body = body()
            reason = {200: "OK", 302: "Found", 404: "Not Found", 500: "Server Error"}[status]
            extra = f"Location: {kind}\r\n" if status == 302 else f"Content-Type: {kind}\r\n"
            writer.write(f"HTTP/1.1 {status} {reason}\r\n{extra}Content-Length: {len(body)}\r\n"
                         f"Connection: close\r\n\r\n".encode() + body)
            await writer.drain()
        finally:
            writer.close()

    def count(self, path="/page"):
        return self.requests.get(path, 0)


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


def tool(pages, clock, **options):
    async def resolve(host, port):
        return [PUBLIC]

    def connect(ip, port):
        return ("127.0.0.1", pages.port)

    return fetch_tool(["allowed.test", "other.test"], _resolve=resolve, _connect=connect, _clock=clock, **options)


class Fetcher:
    """Calls each (tool, url) in the task, one a turn, then answers."""

    async def send(self, request):
        plan = json.loads(request.messages[0].content)
        done = sum(len(m.tool_results) for m in request.messages)
        if done < len(plan):
            name, url = plan[done]
            call = ToolCall(id=f"c{done}", name=name, arguments={"url": url})
            return ModelResponse(message=Message(role=Role.ASSISTANT, tool_calls=(call,)),
                                 stop_reason=StopReason.TOOL_CALLS, usage=Usage(5, 1, 6))
        return ModelResponse(message=Message(role=Role.ASSISTANT, content="done"),
                             stop_reason=StopReason.END_TURN, usage=Usage(5, 1, 6))


READER = AgentSpec(id="reader", instructions="fetch", tool_profile=("fetch_url", "fetch_scoped"))


def backend_for(store):
    return Persistence.postgres(DSN, create_schema=False) if store == "postgres" else None


def evidence_of(runner):
    return runner._evidence_store()


async def fetch(runner, calls, tenant=TENANT, project=PROJECT):
    """One run making `calls`; its result and each call's tool result, in order."""
    result = await runner.run(READER, json.dumps(calls),
                              RunConfig(tenant_id=tenant, project_id=project, model_override="m:fake", max_turns=20))
    assert result.status is RunStatus.COMPLETED, result.error
    scope = RunScope(run_id=result.run_id, tenant_id=tenant, project_id=project)
    sessions = runner._sessions_for(scope)
    history = await asyncio.to_thread(sessions.history, result.run_id)
    return result, [r for m in history for r in m.tool_results]


def source_events(result):
    return [e for e in result.events if e.event_type in (EventType.SOURCE_FETCHED, EventType.SOURCE_SERVED)]


def version_id(result_):
    uri = result_.provenance.source_uri_or_hash
    assert uri.startswith("urn:agentsdk:source:"), uri
    return uri.rsplit(":", 1)[1]


async def version(runner, tenant, project, vid):
    return await asyncio.to_thread(evidence_of(runner).get, tenant, project, vid)


def query(sql, params=()):
    with psycopg.connect(DSN) as conn:
        return conn.execute(sql, params).fetchall()


@pytest.fixture(autouse=True)
def remove_what_the_test_wrote():
    yield
    if DSN:
        with psycopg.connect(DSN, autocommit=True) as conn:
            tenants = [TENANT, OTHER_TENANT]
            ids = [r[0] for r in conn.execute("SELECT run_id FROM runs WHERE tenant_id = ANY(%s)", (tenants,))]
            conn.execute("DELETE FROM source_versions WHERE source_run = ANY(%s)", (ids,))
            conn.execute("DELETE FROM artifacts WHERE source_run = ANY(%s)", (ids,))
            for table in ("run_events", "messages", "execution_manifests"):
                conn.execute(f"DELETE FROM {table} WHERE run_id = ANY(%s)", (ids,))
            conn.execute("DELETE FROM runs WHERE run_id = ANY(%s)", (ids,))


PAGE = {"/page": (200, b"the page", "text/html; charset=utf-8")}


# =================================================================================================
# FR-89, AC-71: the version
# =================================================================================================


def a_version(**fields):
    base = dict(
        source_version_id=str(uuid.uuid4()), canonical_uri="https://allowed.test/a?b=1",
        final_uri="https://allowed.test/final", retrieval_time=T0, content_hash="ab" * 32,
        artifact_id=str(uuid.uuid4()), media_type="text/html", cache_scope=CacheScope.PROJECT,
        auth_scope_hash=None, prior_version=None, provenance=ResultProvenance.external().for_source(None),
        tenant_id=TENANT, project_id=PROJECT, source_run=str(uuid.uuid4()),
    )
    base.update(fields)
    return EvidenceSourceVersion(**base)


def test_a_version_has_its_uri_and_is_frozen():
    v = a_version()
    assert v.uri == f"urn:agentsdk:source:{v.source_version_id}"
    with pytest.raises(dataclasses.FrozenInstanceError):
        v.content_hash = "cd" * 32


BAD = [
    ("source_version_id", "not-a-uuid"), ("source_version_id", None),
    ("canonical_uri", ""), ("canonical_uri", "ftp://allowed.test/"), ("canonical_uri", 5),
    ("final_uri", ""), ("final_uri", "x\x00y"),
    ("retrieval_time", datetime(2026, 1, 1)), ("retrieval_time", "2026-01-01"),
    ("content_hash", "AB" * 32), ("content_hash", "ab" * 31), ("content_hash", None),
    ("artifact_id", "nope"),
    ("media_type", ""), ("media_type", None),
    ("cache_scope", "everyone"), ("cache_scope", None),
    ("auth_scope_hash", ""), ("auth_scope_hash", 7),
    ("prior_version", "nope"),
    ("provenance", {"origin": "external_tool"}), ("provenance", None),
    ("tenant_id", ""), ("tenant_id", "a\x00b"), ("project_id", ""), ("project_id", None),
    ("source_run", "nope"), ("source_run", None),
]


@pytest.mark.parametrize("name,value", BAD, ids=[f"{n}={v!r}" for n, v in BAD])
def test_every_field_is_refused_by_name_at_construction(name, value):
    with pytest.raises(ValueError, match=name):
        a_version(**{name: value})


def test_authenticated_content_is_never_public_global():
    a_version(cache_scope=CacheScope.PUBLIC_GLOBAL)
    a_version(cache_scope=CacheScope.TENANT, auth_scope_hash="cd" * 32)
    with pytest.raises(ValueError, match="PUBLIC_GLOBAL.*auth_scope_hash|auth_scope_hash.*PUBLIC_GLOBAL"):
        a_version(cache_scope=CacheScope.PUBLIC_GLOBAL, auth_scope_hash="cd" * 32)


@pytest.mark.parametrize("given,canonical", [
    ("HTTPS://Allowed.TEST/Path?Q=A#frag", "https://allowed.test/Path?Q=A"),
    ("http://allowed.test:80/a", "http://allowed.test/a"),
    ("https://allowed.test:443/a", "https://allowed.test/a"),
    ("https://allowed.test:8443/a", "https://allowed.test:8443/a"),
    ("http://allowed.test/a%2Fb?x=1&y=2", "http://allowed.test/a%2Fb?x=1&y=2"),
    ("http://allowed.test", "http://allowed.test"),
])
def test_the_canonical_uri_lowercases_scheme_and_host_and_drops_default_port_and_fragment(given, canonical):
    assert canonical_uri(given) == canonical


def test_neither_store_exposes_a_method_that_updates_or_deletes_a_version():
    for store in (InMemoryEvidenceStore, PostgresEvidenceStore):
        public = [name for name in dir(store) if not name.startswith("_")]
        assert not [n for n in public if any(w in n.lower() for w in ("update", "delete", "remove", "replace", "set", "clear", "expire"))], public


@pytest.mark.parametrize("store", STORES)
async def test_a_fetched_version_reads_back_field_for_field(store):
    async with Pages(PAGE) as pages:
        clock = Clock()
        runner = Runner({"m": Fetcher()}, tools=[tool(pages, clock)], persistence=backend_for(store))
        result, [got] = await fetch(runner, [["fetch_url", "HTTP://Allowed.TEST:80/page#top"]])
    v = await version(runner, TENANT, PROJECT, version_id(got))
    # The key is canonical; the final URI is the URL as fetched, as FR-38 named it.
    assert (v.canonical_uri, v.final_uri, v.retrieval_time) == ("http://allowed.test/page", "http://allowed.test:80/page#top", T0)
    assert (v.media_type, v.cache_scope, v.auth_scope_hash, v.prior_version) == ("text/html", CacheScope.PROJECT, None, None)
    assert (v.tenant_id, v.project_id, v.source_run) == (TENANT, PROJECT, result.run_id)
    assert v.provenance == ResultProvenance.external().for_source(v.uri)
    content = await runner._artifacts_for(TENANT, PROJECT).get(v.artifact_id)
    assert hashlib.sha256(content).hexdigest() == v.content_hash
    assert content.decode() == got.content == "[HTTP 200]\nthe page"
    ref = await runner._artifacts_for(TENANT, PROJECT).metadata(v.artifact_id)
    assert ref.expires_at is None and ref.source_run == result.run_id

    # And a version recorded directly, with every optional field set, reads back equal.
    full = dataclasses.replace(v, source_version_id=str(uuid.uuid4()), prior_version=v.source_version_id,
                               cache_scope=CacheScope.TENANT, auth_scope_hash="cd" * 32,
                               retrieval_time=T0 + timedelta(seconds=1))
    await asyncio.to_thread(evidence_of(runner).record, full, ledger_run_id=result.run_id)
    assert await version(runner, TENANT, PROJECT, full.source_version_id) == full


@pytest.mark.parametrize("store", STORES)
async def test_a_version_is_not_readable_from_another_tenant_or_project(store):
    async with Pages(PAGE) as pages:
        runner = Runner({"m": Fetcher()}, tools=[tool(pages, Clock())], persistence=backend_for(store))
        _, [got] = await fetch(runner, [["fetch_url", "http://allowed.test/page"]])
    for tenant, project in ((OTHER_TENANT, PROJECT), (TENANT, OTHER_PROJECT)):
        with pytest.raises(LookupError):
            await version(runner, tenant, project, version_id(got))


@pytest.mark.parametrize("damage", ["tampered", "missing", "version hash"])
@pytest.mark.parametrize("store", STORES)
async def test_a_version_whose_content_no_longer_matches_is_a_miss_and_refetched(store, damage):
    async with Pages(PAGE) as pages:
        runner = Runner({"m": Fetcher()}, tools=[tool(pages, Clock())], persistence=backend_for(store))
        _, [first] = await fetch(runner, [["fetch_url", "http://allowed.test/page"]])
        old = await version(runner, TENANT, PROJECT, version_id(first))
        await _damage(runner, store, old.artifact_id, damage)
        result, [second] = await fetch(runner, [["fetch_url", "http://allowed.test/page"]])
        assert pages.count() == 2, "a damaged version was served"
    new = await version(runner, TENANT, PROJECT, version_id(second))
    assert new.prior_version == old.source_version_id and second.content == "[HTTP 200]\nthe page"
    assert [e.event_type for e in source_events(result)] == [EventType.SOURCE_FETCHED]


async def _damage(runner, store, artifact_id, damage):
    if damage == "version hash":
        # The artifact intact, the version naming another hash: still a miss.
        if store == "postgres":
            def alter():
                with psycopg.connect(DSN, autocommit=True) as conn:
                    conn.execute("UPDATE source_versions SET content_hash = %s WHERE artifact_id = %s",
                                 ("0" * 64, artifact_id))

            await asyncio.to_thread(alter)
        else:
            rows = runner._memory_evidence._rows
            vid = next(k for k, (v, _, _) in rows.items() if v.artifact_id == artifact_id)
            v, ledger, variant = rows[vid]
            rows[vid] = (dataclasses.replace(v, content_hash="0" * 64), ledger, variant)
        return
    if store == "postgres":
        sql = ("UPDATE artifacts SET content = 'tampered'::bytea WHERE artifact_id = %s" if damage == "tampered"
               else "DELETE FROM artifacts WHERE artifact_id = %s")
        def damage_row():
            with psycopg.connect(DSN, autocommit=True) as conn:
                conn.execute(sql, (artifact_id,))

        await asyncio.to_thread(damage_row)
    elif damage == "tampered":
        rows = runner._memory_artifacts._rows
        rows[artifact_id] = (rows[artifact_id][0], b"tampered")
    else:
        await runner._artifacts_for(TENANT, PROJECT).delete(artifact_id)


def test_migration_0010_adds_the_table_and_applies_once(monkeypatch):
    assert DSN, "DATABASE_URL must be set"
    real = migrate.discover()
    assert "0010" in [v for v, _ in real], "migration 0010 is not on disk"
    name = "m22_" + uuid.uuid4().hex[:8]
    scratch = DSN + ("&" if "?" in DSN else "?") + f"options=-csearch_path%3D{name}"
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{name}"')
    try:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'SET search_path TO "{name}"')
            conn.execute(schema_sql)
        assert "0010" in apply_migrations(scratch)
        snapshot = """SELECT column_name, data_type, is_nullable FROM information_schema.columns
                      WHERE table_schema = %s AND table_name = 'source_versions' ORDER BY column_name"""
        with psycopg.connect(scratch) as conn:
            before = conn.execute(snapshot, (name,)).fetchall()
            checks = conn.execute(
                "SELECT count(*) FROM information_schema.check_constraints WHERE constraint_schema = %s",
                (name,)).fetchone()[0]
        assert apply_migrations(scratch) == []
        with psycopg.connect(scratch, autocommit=True) as conn:
            assert conn.execute(snapshot, (name,)).fetchall() == before
            # The file itself, run again by hand, changes nothing either (FR-17).
            conn.execute(dict(real)["0010"].read_text(encoding="utf-8"))
            assert conn.execute(snapshot, (name,)).fetchall() == before
            assert conn.execute(
                "SELECT count(*) FROM information_schema.check_constraints WHERE constraint_schema = %s",
                (name,)).fetchone()[0] == checks
        columns = {c: (t, n) for c, t, n in before}
        assert columns["tenant_id"] == ("text", "NO") and columns["project_id"] == ("text", "NO")
        with psycopg.connect(scratch) as conn:
            indexes = [r[0] for r in conn.execute(
                "SELECT indexdef FROM pg_indexes WHERE schemaname = %s AND tablename = 'source_versions'", (name,))]
        assert any("(tenant_id, project_id" in d for d in indexes), indexes
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{name}" CASCADE')


def test_the_table_refuses_public_global_with_a_credential():
    with psycopg.connect(DSN) as conn:
        checks = [r[0] for r in conn.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = 'source_versions'::regclass "
            "AND contype = 'c'")]
    assert any("public_global" in c and "auth_scope_hash IS NULL" in c for c in checks), checks


# =================================================================================================
# FR-90, FR-91, AC-72: the cache
# =================================================================================================


@pytest.mark.parametrize("store", STORES)
async def test_a_page_fetched_twice_within_freshness_is_requested_once(store):
    async with Pages(PAGE) as pages:
        clock = Clock()
        runner = Runner({"m": Fetcher()}, tools=[tool(pages, clock)], persistence=backend_for(store))
        result, [first, second] = await fetch(runner, [["fetch_url", "http://allowed.test/page"]] * 2)
        assert pages.count() == 1
    assert second.content == first.content == "[HTTP 200]\nthe page"
    assert second.provenance == first.provenance and version_id(first) == version_id(second)
    fetched, served = source_events(result)
    assert (fetched.event_type, served.event_type) == (EventType.SOURCE_FETCHED, EventType.SOURCE_SERVED)
    uri = first.provenance.source_uri_or_hash
    assert fetched.payload == {"source_uri": uri, "canonical_uri": "http://allowed.test/page",
                               "cache_scope": "project", "copied": False}
    assert served.payload == {"source_uri": uri, "canonical_uri": "http://allowed.test/page",
                              "cache_scope": "project", "copied": False, "age_seconds": 0.0}


@pytest.mark.parametrize("store", STORES)
async def test_after_freshness_a_refetch_records_a_new_version_naming_the_old(store):
    async with Pages(PAGE) as pages:
        clock = Clock()
        runner = Runner({"m": Fetcher()}, tools=[tool(pages, clock, freshness_seconds=60)],
                        persistence=backend_for(store))
        _, [first] = await fetch(runner, [["fetch_url", "http://allowed.test/page"]])
        clock.now = T0 + timedelta(seconds=59)
        served, [hit] = await fetch(runner, [["fetch_url", "http://allowed.test/page"]])
        assert pages.count() == 1 and source_events(served)[0].payload["age_seconds"] == 59.0
        clock.now = T0 + timedelta(seconds=60)
        _, [again] = await fetch(runner, [["fetch_url", "http://allowed.test/page"]])
        assert pages.count() == 2
    new = await version(runner, TENANT, PROJECT, version_id(again))
    assert new.prior_version == version_id(first) and new.retrieval_time == T0 + timedelta(seconds=60)


async def test_the_default_freshness_is_an_hour():
    async with Pages(PAGE) as pages:
        clock = Clock()
        runner = Runner({"m": Fetcher()}, tools=[tool(pages, clock)])
        await fetch(runner, [["fetch_url", "http://allowed.test/page"]])
        clock.now = T0 + timedelta(seconds=3599)
        await fetch(runner, [["fetch_url", "http://allowed.test/page"]])
        assert pages.count() == 1
        clock.now = T0 + timedelta(seconds=3600)
        await fetch(runner, [["fetch_url", "http://allowed.test/page"]])
        assert pages.count() == 2


@pytest.mark.parametrize("store", STORES)
async def test_a_non_2xx_response_is_never_cached(store):
    table = {"/page": (404, b"gone", "text/plain"), "/moved": (302, b"", "http://allowed.test/page")}
    async with Pages(table) as pages:
        runner = Runner({"m": Fetcher()}, tools=[tool(pages, Clock())], persistence=backend_for(store))
        result, results = await fetch(runner, [["fetch_url", "http://allowed.test/page"]] * 2
                                      + [["fetch_url", "http://allowed.test/moved"]])
        assert pages.count() == 3 and pages.count("/moved") == 1
    assert [r.content for r in results] == ["[HTTP 404]\ngone"] * 3
    assert [r.provenance.source_uri_or_hash for r in results] == ["http://allowed.test/page"] * 3
    assert source_events(result) == []


@pytest.mark.parametrize("store", STORES)
async def test_a_redirected_fetch_is_keyed_by_the_requested_uri(store):
    table = {"/page": PAGE["/page"], "/moved": (302, b"", "http://allowed.test/page")}
    async with Pages(table) as pages:
        runner = Runner({"m": Fetcher()}, tools=[tool(pages, Clock())], persistence=backend_for(store))
        _, [moved, again, direct] = await fetch(runner, [["fetch_url", "http://allowed.test/moved"]] * 2
                                                + [["fetch_url", "http://allowed.test/page"]])
        assert pages.count("/moved") == 1 and pages.count() == 2, "the final uri was used as a key"
    v = await version(runner, TENANT, PROJECT, version_id(moved))
    assert (v.canonical_uri, v.final_uri) == ("http://allowed.test/moved", "http://allowed.test/page")
    assert version_id(again) == version_id(moved) != version_id(direct)


@pytest.mark.parametrize("store", STORES)
async def test_a_refused_fetch_records_nothing(store):
    table = {"/page": (200, b"\x00\x01", "application/octet-stream")}
    async with Pages(table) as pages:
        runner = Runner({"m": Fetcher()}, tools=[tool(pages, Clock())], persistence=backend_for(store))
        result, results = await fetch(runner, [["fetch_url", "http://denied.test/page"],
                                               ["fetch_url", "http://allowed.test/page"]])
    assert all(r.is_error for r in results) and source_events(result) == []
    assert await asyncio.to_thread(evidence_of(runner).reachable, ResourceCacheKey("http://allowed.test/page"),
                                   tenant_id=TENANT, project_id=PROJECT, ledger_run_id=result.run_id) == []


async def test_a_fetch_with_no_run_scope_keeps_the_final_url_and_records_nothing():
    from agentsdk.executor import Completed, ToolExecutor
    from agentsdk.permissions import AllowlistPermissionChecker
    from agentsdk.tools import ToolRegistry

    table = {"/page": PAGE["/page"], "/moved": (302, b"", "http://allowed.test/page")}
    async with Pages(table) as pages:
        registry = ToolRegistry()
        registry.register(tool(pages, Clock()))
        executor = ToolExecutor(registry, AllowlistPermissionChecker({"fetch_url"}), emit=lambda *a: None)
        outcome = await executor.execute(ToolCall(id="c", name="fetch_url", arguments={"url": "http://allowed.test/moved"}))
    assert isinstance(outcome, Completed) and outcome.result.provenance.source_uri_or_hash == "http://allowed.test/page"


async def test_nothing_survives_a_runner_without_persistence():
    async with Pages(PAGE) as pages:
        for _ in range(2):
            runner = Runner({"m": Fetcher()}, tools=[tool(pages, Clock())])
            await fetch(runner, [["fetch_url", "http://allowed.test/page"]])
        assert pages.count() == 2


# -------------------------------------------------------------------------------------------------
# The five scopes against four readers
# -------------------------------------------------------------------------------------------------

READERS = ["same run", "another run", "another project", "another tenant"]
REACH = {
    CacheScope.PUBLIC_GLOBAL: {"same run", "another run", "another project", "another tenant"},
    CacheScope.TENANT: {"same run", "another run", "another project"},
    CacheScope.PROJECT: {"same run", "another run"},
    CacheScope.SESSION: {"same run"},
    CacheScope.NO_CACHE: set(),
}


@pytest.mark.parametrize("reader", READERS)
@pytest.mark.parametrize("scope", list(CacheScope), ids=lambda s: s.name)
@pytest.mark.parametrize("store", STORES)
async def test_each_scope_is_served_exactly_to_its_reach(store, scope, reader):
    async with Pages(PAGE) as pages:
        clock = Clock()
        runner = Runner({"m": Fetcher()}, persistence=backend_for(store),
                        tools=[tool(pages, clock, cache_scope=scope, name="fetch_scoped"), tool(pages, clock)])
        url = "http://allowed.test/page"
        origin_calls = [["fetch_scoped", url]] + ([["fetch_url", url]] if reader == "same run" else [])
        origin, results = await fetch(runner, origin_calls)
        original = await version(runner, TENANT, PROJECT, version_id(results[0]))
        if reader == "same run":
            got, run_id, where = results[1], origin.run_id, (TENANT, PROJECT)
        else:
            where = {"another run": (TENANT, PROJECT), "another project": (TENANT, OTHER_PROJECT),
                     "another tenant": (OTHER_TENANT, PROJECT)}[reader]
            # Later, within freshness: a copy keeps the original's retrieval time.
            clock.now = T0 + timedelta(seconds=5)
            read, [got] = await fetch(runner, [["fetch_url", url]], *where)
            run_id = read.run_id
        served = reader in REACH[scope]
        assert pages.count() == (1 if served else 2), f"{scope.name} to {reader}: served={served}"
    v = await version(runner, *where, version_id(got))
    assert got.content == "[HTTP 200]\nthe page"
    if not served:
        assert v.source_run == run_id and v.prior_version is None
        return
    if where == (TENANT, PROJECT):
        assert v == original, "a hit within the origin's tenant and project is the same version"
        return
    # A copy: its own artifact and version, naming only the original's hash and time.
    assert (v.tenant_id, v.project_id, v.source_run) == (*where, run_id)
    assert v.source_version_id != original.source_version_id and v.artifact_id != original.artifact_id
    assert (v.content_hash, v.retrieval_time) == (original.content_hash, original.retrieval_time)
    assert v.prior_version is None and v.cache_scope is CacheScope.PROJECT
    assert await runner._artifacts_for(*where).get(v.artifact_id) == got.content.encode()
    stored = json.dumps([str(f) for f in dataclasses.astuple(v)])
    for identifier in (original.source_version_id, original.artifact_id, original.source_run):
        assert identifier not in stored and identifier not in got.provenance.source_uri_or_hash
    [event] = source_events(read)
    assert event.event_type is EventType.SOURCE_SERVED and event.payload["copied"] is True
    assert original.source_version_id not in json.dumps(event.payload)


async def test_session_scope_reaches_the_children_of_its_top_level_run_and_no_other():
    from agentsdk.subagents import Briefing, SubagentPool

    async with Pages(PAGE) as pages:
        clock = Clock()
        runner = Runner({"m": Fetcher()},
                        tools=[tool(pages, clock, cache_scope=CacheScope.SESSION, name="fetch_scoped"), tool(pages, clock)])
        url = "http://allowed.test/page"
        top, _ = await fetch(runner, [["fetch_scoped", url]])
        pool = SubagentPool(runner)
        parent = RunScope(run_id=top.run_id, tenant_id=TENANT, project_id=PROJECT)
        child = await pool.spawn(parent=parent, briefing=Briefing(objective=json.dumps([["fetch_url", url]]),
                                                                   assigned_role="reader"), agent=READER)
        assert child.status is RunStatus.COMPLETED and pages.count() == 1, "the child was not served"
        await fetch(runner, [["fetch_url", url]])
        assert pages.count() == 2, "another top-level run was served a SESSION version"


async def test_the_ledger_run_id_is_the_top_level_run_for_every_run_the_runner_builds(monkeypatch):
    from agentsdk import api
    from agentsdk.subagents import Briefing, SubagentPool

    seen = {}
    real = api.ToolExecutor

    def recording(*args, **kwargs):
        scope = kwargs["scope"]
        seen[scope.run_id] = scope.ledger_run_id
        return real(*args, **kwargs)

    monkeypatch.setattr(api, "ToolExecutor", recording)
    runner = Runner({"m": Fetcher()})
    top, _ = await fetch(runner, [])
    pool = SubagentPool(runner)
    child = await pool.spawn(parent=RunScope(run_id=top.run_id, tenant_id=TENANT, project_id=PROJECT),
                             briefing=Briefing(objective="[]", assigned_role="reader"), agent=READER)
    from agentsdk.scope import ToolScope
    grandchild = await pool.spawn(
        parent=ToolScope(run_id=child.run_id, tenant_id=TENANT, project_id=PROJECT, ledger_run_id=top.run_id),
        briefing=Briefing(objective="[]", assigned_role="reader"), agent=READER)
    assert seen == {top.run_id: top.run_id, child.run_id: top.run_id, grandchild.run_id: top.run_id}


def test_a_run_config_refuses_a_ledger_run_id_that_is_not_a_uuid():
    with pytest.raises(ValueError, match="ledger_run_id"):
        RunConfig(tenant_id=TENANT, project_id=PROJECT, ledger_run_id="not-a-uuid")


# -------------------------------------------------------------------------------------------------
# The tool's configuration
# -------------------------------------------------------------------------------------------------

DEFAULT_HASH = "1c16f3bc764bf0d9a538e6d678c5e8b3f1bc048e5e5f8f7e3a120bd5ce6be83c"  # fetch_tool(["allowed.test"]) at M21a


def test_a_default_fetch_tool_keeps_its_schema_hash():
    assert fetch_tool(["allowed.test"]).spec.schema_hash() == DEFAULT_HASH
    assert fetch_tool(["allowed.test"], cache_scope=CacheScope.PROJECT, host_scopes={},
                      freshness_seconds=3600).spec.schema_hash() == DEFAULT_HASH


@pytest.mark.parametrize("options", [
    {"cache_scope": CacheScope.TENANT}, {"cache_scope": CacheScope.NO_CACHE},
    {"host_scopes": {"allowed.test": CacheScope.PUBLIC_GLOBAL}}, {"freshness_seconds": 60},
])
def test_a_non_default_cache_configuration_changes_the_schema_hash(options):
    spec = fetch_tool(["allowed.test"], **options).spec
    assert spec.schema_hash() != DEFAULT_HASH
    assert set(options) <= set(spec.configuration)


@pytest.mark.parametrize("options,match", [
    ({"cache_scope": "project"}, "cache_scope"),
    ({"host_scopes": {"elsewhere.test": CacheScope.TENANT}}, "host_scopes"),
    ({"host_scopes": {"allowed.test": "tenant"}}, "host_scopes"),
    ({"freshness_seconds": 0}, "freshness_seconds"),
    ({"freshness_seconds": True}, "freshness_seconds"),
])
def test_a_bad_cache_configuration_is_refused_by_name(options, match):
    with pytest.raises((TypeError, ValueError), match=match):
        fetch_tool(["allowed.test"], **options)


async def test_host_scopes_choose_the_scope_by_requested_host():
    async with Pages(PAGE) as pages:
        clock = Clock()
        runner = Runner({"m": Fetcher()}, tools=[tool(pages, clock, host_scopes={"other.test": CacheScope.NO_CACHE})])
        result, [a, b] = await fetch(runner, [["fetch_url", "http://allowed.test/page"],
                                              ["fetch_url", "http://other.test/page"]])
    scopes = [e.payload["cache_scope"] for e in source_events(result)]
    assert scopes == ["project", "no_cache"]
    v = await version(runner, TENANT, PROJECT, version_id(b))
    assert v.cache_scope is CacheScope.NO_CACHE


# =================================================================================================
# The demo command: scripts/19_sources.py
# =================================================================================================


def test_the_example_shows_a_fetch_served_refreshed_and_kept_from_another_tenant_offline():
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    run = subprocess.run([sys.executable, str(repo / "scripts" / "19_sources.py"), "--offline"],
                         capture_output=True, text=True, timeout=120, cwd=repo)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "[FAIL]" not in run.stdout and run.stdout.count("[PASS]") == 7, run.stdout


# =================================================================================================
# What the cache must not do
# =================================================================================================


async def test_a_cached_page_is_never_served_for_a_url_this_tools_allowlist_refuses():
    async with Pages(PAGE) as pages:
        clock = Clock()

        async def resolve(host, port):
            return [PUBLIC]

        narrow = fetch_tool(["allowed.test"], name="fetch_scoped", _resolve=resolve,
                            _connect=lambda ip, port: ("127.0.0.1", pages.port), _clock=clock)
        runner = Runner({"m": Fetcher()}, tools=[tool(pages, clock), narrow])
        _, [wide] = await fetch(runner, [["fetch_url", "http://other.test/page"]])
        result, [refused] = await fetch(runner, [["fetch_scoped", "http://other.test/page"]])
    assert not wide.is_error and refused.is_error and "allowlist" in refused.content
    assert source_events(result) == []


async def test_a_no_cache_fetch_is_never_served_even_a_fresh_version():
    async with Pages(PAGE) as pages:
        clock = Clock()
        runner = Runner({"m": Fetcher()}, tools=[
            tool(pages, clock), tool(pages, clock, cache_scope=CacheScope.NO_CACHE, name="fetch_scoped")])
        result, [first, second] = await fetch(runner, [["fetch_url", "http://allowed.test/page"],
                                                       ["fetch_scoped", "http://allowed.test/page"]])
        assert pages.count() == 2
    v = await version(runner, TENANT, PROJECT, version_id(second))
    assert v.cache_scope is CacheScope.NO_CACHE and v.prior_version is None
    assert [e.event_type for e in source_events(result)] == [EventType.SOURCE_FETCHED] * 2


@pytest.mark.parametrize("store", STORES)
async def test_a_readers_own_fresh_version_is_served_before_a_newer_one_is_copied(store):
    async with Pages(PAGE) as pages:
        clock = Clock()
        runner = Runner({"m": Fetcher()}, persistence=backend_for(store), tools=[
            tool(pages, clock), tool(pages, clock, cache_scope=CacheScope.PUBLIC_GLOBAL, name="fetch_scoped")])
        url = "http://allowed.test/page"
        _, [own] = await fetch(runner, [["fetch_url", url]], OTHER_TENANT, PROJECT)
        clock.now = T0 + timedelta(seconds=10)
        await fetch(runner, [["fetch_scoped", url]])
        clock.now = T0 + timedelta(seconds=20)
        result, [again] = await fetch(runner, [["fetch_url", url]], OTHER_TENANT, PROJECT)
        assert pages.count() == 2
    assert version_id(again) == version_id(own), "a newer foreign version was copied over a fresh one of its own"
    assert source_events(result)[0].payload["copied"] is False
