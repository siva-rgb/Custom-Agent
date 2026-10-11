"""M23 gate: the evidence ledger (FR-92 to FR-94, NFR-26, AC-73).

Agents record claims into their top-level run's ledger through `record_claim` and
`supersede_claim`. One statement, however it is cased, spaced or normalised, is one claim
with every contribution kept; a claim's sources and labels are derived from its
contributions, so a clean contribution never lowers them; supersession is a chain the
store keeps from forking. M22's review caveats 1 to 5 are settled here
(DECISION-9de80b77).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sys
import threading
import unicodedata
import uuid

import psycopg
import pytest

from agentsdk import AgentSpec, EvidenceLedger, Persistence, RunConfig, RunStatus, Runner
from agentsdk import migrate
from agentsdk.builtin_tools import fetch_tool
from agentsdk.config import normalise_database_url
from agentsdk.events import EventType
from agentsdk.evidence import CacheScope, canonical_uri
from agentsdk.ledger import ClaimRefused, EvidenceClaim, InMemoryLedgerStore, claim_key, ledger_tools
from agentsdk.migrate import apply_migrations
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.postgres import SCHEMA_PATH, PostgresLedgerStore, RunScope
from agentsdk.primitives import ContentProvenance, Message, Origin, Role, TaintFlag, ToolCall, TrustZone
from agentsdk.scope import ToolScope
from agentsdk.subagents import Briefing, SubagentPool

DSN = normalise_database_url(os.environ.get("DATABASE_URL"))
TENANT, PROJECT, OTHER_TENANT = "SYN-m23", "p-m23", "SYN-m23-other"
PUBLIC = "1.1.1.1"
STORES = ["memory", "postgres"]
STATEMENT = "The Rhine flows into the North Sea."


# =================================================================================================
# The seams
# =================================================================================================


class Pages:
    """A local page server answering by path, counting requests."""

    def __init__(self, table):
        self.table, self.requests = table, 0

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
            self.requests += 1
            body = self.table[head.split(" ", 2)[1]]
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                         + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
            await writer.drain()
        finally:
            writer.close()


PAGES = {"/rhine": b"The Rhine reaches the North Sea.", "/danube": b"The Danube reaches the Black Sea."}


def fetch(pages):
    async def resolve(host, port):
        return [PUBLIC]

    return fetch_tool(["rivers.test"], _resolve=resolve, _connect=lambda ip, port: ("127.0.0.1", pages.port))


class Scripted:
    """Makes each [tool, arguments] in its task, one a turn, then answers. An argument
    "$N" is replaced by the source of the Nth tool result in its history."""

    async def send(self, request):
        plan = json.loads(request.messages[0].content)
        results = [r for m in request.messages for r in m.tool_results]
        if len(results) < len(plan):
            name, arguments = plan[len(results)]
            sources = [r.provenance.source_uri_or_hash for r in results]
            resolved = json.loads(json.dumps(arguments), object_hook=lambda d: {
                k: _sub(v, sources) for k, v in d.items()})
            call = ToolCall(id=f"c{len(results)}", name=name, arguments=resolved)
            return ModelResponse(message=Message(role=Role.ASSISTANT, tool_calls=(call,)),
                                 stop_reason=StopReason.TOOL_CALLS, usage=Usage(5, 1, 6))
        return ModelResponse(message=Message(role=Role.ASSISTANT, content="done"),
                             stop_reason=StopReason.END_TURN, usage=Usage(5, 1, 6))


def _sub(value, sources):
    if isinstance(value, list):
        return [_sub(v, sources) for v in value]
    if isinstance(value, str) and value.startswith("$") and value[1:].isdigit():
        return sources[int(value[1:])]
    return value


AGENT = AgentSpec(id="researcher", instructions="research",
                  tool_profile=("fetch_url", "record_claim", "supersede_claim"))
RECORD, SUPERSEDE = ledger_tools()


def runner_for(store, pages, **options):
    backend = Persistence.postgres(DSN, create_schema=False) if store == "postgres" else None
    return Runner({"m": Scripted()}, tools=[fetch(pages), RECORD, SUPERSEDE], persistence=backend, **options)


def config(tenant=TENANT, project=PROJECT, **options):
    return RunConfig(tenant_id=tenant, project_id=project, model_override="m:fake", max_turns=20, **options)


async def run(runner, plan, **options):
    result = await runner.run(AGENT, json.dumps(plan), config(**options))
    assert result.status is RunStatus.COMPLETED, result.error
    return result, await results_of(runner, result.run_id, options.get("tenant", TENANT), options.get("project", PROJECT))


async def results_of(runner, run_id, tenant=TENANT, project=PROJECT):
    sessions = runner._sessions_for(RunScope(run_id=run_id, tenant_id=tenant, project_id=project))
    history = await asyncio.to_thread(sessions.history, run_id)
    return [r for m in history for r in m.tool_results]


async def child(runner, top, plan, *, inputs=()):
    pool = SubagentPool(runner)
    done = await pool.spawn(parent=RunScope(run_id=top.run_id, tenant_id=TENANT, project_id=PROJECT),
                            briefing=Briefing(objective=json.dumps(plan), assigned_role="researcher"), agent=AGENT)
    assert done.status is RunStatus.COMPLETED, done.error
    return done


def ledger(runner, top, tenant=TENANT, project=PROJECT):
    return runner.evidence_ledger(tenant, project, top.run_id)


async def read(runner, top, method, *args, tenant=TENANT, project=PROJECT):
    return await asyncio.to_thread(getattr(ledger(runner, top, tenant, project), method), *args)


def events(result, kind):
    return [e.payload for e in result.events if e.event_type is kind]


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
            conn.execute("DELETE FROM claim_citations WHERE contribution_id IN (SELECT contribution_id FROM "
                         "claim_contributions WHERE run_id = ANY(%s))", (ids,))
            conn.execute("DELETE FROM claim_contributions WHERE run_id = ANY(%s)", (ids,))
            # Newest first, so no claim is removed while a later one still names it.
            conn.execute("DELETE FROM claims WHERE ledger_run_id = ANY(%s) AND supersedes IS NOT NULL", (ids,))
            while conn.execute("DELETE FROM claims c WHERE ledger_run_id = ANY(%s) AND NOT EXISTS "
                               "(SELECT 1 FROM claims s WHERE s.supersedes = c.claim_id)", (ids,)).rowcount:
                pass
            conn.execute("DELETE FROM source_versions WHERE source_run = ANY(%s)", (ids,))
            conn.execute("DELETE FROM artifacts WHERE source_run = ANY(%s)", (ids,))
            for table in ("run_events", "messages", "execution_manifests"):
                conn.execute(f"DELETE FROM {table} WHERE run_id = ANY(%s)", (ids,))
            conn.execute("DELETE FROM runs WHERE run_id = ANY(%s)", (ids,))


def untrusted(view):
    return view.provenance.trust_zone is TrustZone.UNTRUSTED and TaintFlag.EXTERNAL_CONTENT in view.provenance.taint_flags


# =================================================================================================
# FR-92: the claim and its key
# =================================================================================================


@pytest.mark.parametrize("variant", [
    "the rhine flows into the north sea.",
    "  The   Rhine\tflows into\nthe North Sea.  ",
    "THE RHINE FLOWS INTO THE NORTH SEA.",
])
def test_case_and_spacing_do_not_change_the_key(variant):
    assert claim_key(variant) == claim_key(STATEMENT)


def test_nfc_nfd_and_case_folding_do_not_change_the_key():
    composed = unicodedata.normalize("NFC", "Café prices rose.")
    decomposed = unicodedata.normalize("NFD", "Café prices rose.")
    assert composed != decomposed and claim_key(composed) == claim_key(decomposed)
    assert claim_key("Die Straße ist lang.") == claim_key("die strasse ist lang.")


def test_a_paraphrase_is_a_different_claim():
    assert claim_key("The Rhine empties into the North Sea.") != claim_key(STATEMENT)


def test_the_key_is_the_sha256_of_the_normalised_text():
    import hashlib

    assert claim_key("  A  b ") == hashlib.sha256("a b".encode()).hexdigest()


def a_claim(**fields):
    base = dict(claim_id=str(uuid.uuid4()), ledger_run_id=str(uuid.uuid4()), statement=STATEMENT,
                claim_key=claim_key(STATEMENT), supersedes=None, tenant_id=TENANT, project_id=PROJECT)
    base.update(fields)
    return EvidenceClaim(**base)


def test_a_claim_is_frozen_and_has_its_uri():
    c = a_claim()
    assert c.uri == f"urn:agentsdk:claim:{c.claim_id}"
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.statement = "x"


BAD = [("claim_id", "nope"), ("ledger_run_id", "NOPE"), ("statement", ""), ("statement", "a\x00b"),
       ("statement", 5), ("claim_key", "ab" * 32), ("supersedes", "nope"), ("tenant_id", ""), ("project_id", None)]


@pytest.mark.parametrize("name,value", BAD, ids=[f"{n}={v!r}" for n, v in BAD])
def test_every_claim_field_is_refused_by_name(name, value):
    with pytest.raises(ValueError, match=name):
        a_claim(**{name: value})


def test_neither_ledger_store_can_update_or_delete():
    for store in (InMemoryLedgerStore, PostgresLedgerStore):
        public = [n for n in dir(store) if not n.startswith("_")]
        assert not [n for n in public if any(w in n.lower() for w in ("update", "delete", "remove", "replace", "set", "clear"))], public


def test_migration_0011_adds_the_tables_and_applies_once():
    real = migrate.discover()
    assert "0011" in [v for v, _ in real], "migration 0011 is not on disk"
    name = "m23_" + uuid.uuid4().hex[:8]
    scratch = DSN + ("&" if "?" in DSN else "?") + f"options=-csearch_path%3D{name}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{name}"')
    try:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'SET search_path TO "{name}"')
            conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        assert "0011" in apply_migrations(scratch)
        snapshot = ("SELECT table_name, column_name, data_type, is_nullable FROM information_schema.columns "
                    "WHERE table_schema = %s AND table_name IN ('claims', 'claim_contributions', 'claim_citations') "
                    "ORDER BY 1, 2")
        with psycopg.connect(scratch) as conn:
            before = conn.execute(snapshot, (name,)).fetchall()
        assert apply_migrations(scratch) == []
        with psycopg.connect(scratch, autocommit=True) as conn:
            conn.execute(dict(real)["0011"].read_text(encoding="utf-8"))
            assert conn.execute(snapshot, (name,)).fetchall() == before
        columns = {(t, c): n for t, c, _, n in before}
        for table in ("claims", "claim_contributions"):
            assert columns[(table, "tenant_id")] == columns[(table, "project_id")] == "NO"
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{name}" CASCADE')


# =================================================================================================
# FR-93, AC-73: recording
# =================================================================================================


@pytest.mark.parametrize("store", STORES)
async def test_three_agents_recording_one_statement_store_one_claim_with_every_contribution(store):
    async with Pages(PAGES) as pages:
        runner = runner_for(store, pages)
        top, [rhine, _] = await run(runner, [
            ["fetch_url", {"url": "http://rivers.test/rhine"}],
            ["record_claim", {"statement": STATEMENT, "supporting": ["$0"]}]])
        await child(runner, top, [
            ["fetch_url", {"url": "http://rivers.test/danube"}],
            ["record_claim", {"statement": "the  RHINE flows into the north sea.", "supporting": ["$0"]}]])
        await child(runner, top, [
            ["record_claim", {"statement": unicodedata.normalize("NFD", STATEMENT.upper()),
                              "supporting": [rhine.provenance.source_uri_or_hash], "confidence": 0.75}]])
    [view] = await read(runner, top, "active")
    assert view.claim.statement == STATEMENT and len(view.contributions) == 3
    assert len({c.run_id for c in view.contributions}) == 3
    assert len(view.supporting) == 2 and view.contradicting == ()
    assert [c.confidence for c in view.contributions] == [None, None, 0.75]
    assert {c.agent_id for c in view.contributions} == {"researcher"}
    assert untrusted(view)
    # The third child fetched nothing: its contribution is untrusted through the version it cites.
    assert all(c.provenance.trust_zone is TrustZone.UNTRUSTED for c in view.contributions)


@pytest.mark.parametrize("cited", ["another tenant", "nonexistent", "not a uri"])
@pytest.mark.parametrize("store", STORES)
async def test_a_citation_outside_the_tenant_or_nowhere_is_refused_and_records_nothing(store, cited):
    async with Pages(PAGES) as pages:
        runner = runner_for(store, pages)
        _, [foreign] = await run(runner, [["fetch_url", {"url": "http://rivers.test/rhine"}]], tenant=OTHER_TENANT)
        ref = {"another tenant": foreign.provenance.source_uri_or_hash,
               "nonexistent": f"urn:agentsdk:source:{uuid.uuid4()}", "not a uri": "the rhine page"}[cited]
        top, [refused] = await run(runner, [["record_claim", {"statement": STATEMENT, "supporting": [ref]}]])
    assert refused.is_error and ref in refused.content, refused.content
    assert await read(runner, top, "active") == []
    assert events(top, EventType.CLAIM_RECORDED) == []


@pytest.mark.parametrize("store", STORES)
async def test_a_contribution_from_a_run_briefed_with_an_untrusted_input_and_citing_nothing_is_untrusted(store):
    from agentsdk.tools import ResultProvenance

    tainted = ResultProvenance.external().for_source("urn:agentsdk:artifact:brief")
    async with Pages(PAGES) as pages:
        runner = runner_for(store, pages)
        top, [recorded] = await run(runner, [["record_claim", {"statement": STATEMENT}]],
                                    briefed_inputs=(("urn:agentsdk:artifact:brief", tainted),))
    [view] = await read(runner, top, "active")
    assert view.supporting == () and untrusted(view)
    # The tool's result carries the claim's labels (taken_in, M19).
    assert recorded.provenance.trust_zone is TrustZone.UNTRUSTED


@pytest.mark.parametrize("order", ["clean then untrusted", "untrusted then clean"])
@pytest.mark.parametrize("store", STORES)
async def test_a_clean_contribution_never_lowers_a_claims_labels(store, order):
    clean = [["record_claim", {"statement": STATEMENT}]]
    dirty = [["fetch_url", {"url": "http://rivers.test/rhine"}], ["record_claim", {"statement": STATEMENT}]]
    first, second = (clean, dirty) if order == "clean then untrusted" else (dirty, clean)
    async with Pages(PAGES) as pages:
        runner = runner_for(store, pages)
        top, _ = await run(runner, first)
        await child(runner, top, second)
    [view] = await read(runner, top, "active")
    labels = [c.provenance.trust_zone for c in view.contributions]
    assert TrustZone.UNTRUSTED in labels and labels.count(TrustZone.UNTRUSTED) == 1
    assert untrusted(view)


@pytest.mark.parametrize("store", STORES)
async def test_claim_recorded_is_emitted_with_its_fields(store):
    async with Pages(PAGES) as pages:
        runner = runner_for(store, pages)
        top, [page, first, again] = await run(runner, [
            ["fetch_url", {"url": "http://rivers.test/rhine"}],
            ["record_claim", {"statement": STATEMENT, "supporting": ["$0"], "confidence": 0.5}],
            ["record_claim", {"statement": STATEMENT.lower(), "contradicting": ["$0"]}]])
    one, two = events(top, EventType.CLAIM_RECORDED)
    [view] = await read(runner, top, "active")
    assert one["claim_uri"] == two["claim_uri"] == view.claim.uri
    assert (one["new"], two["new"]) == (True, False)
    version = page.provenance.source_uri_or_hash
    assert one["contribution"]["supporting"] == [version] and one["contribution"]["contradicting"] == []
    assert two["contribution"]["contradicting"] == [version] and one["contribution"]["confidence"] == 0.5
    assert one["contribution"]["run_id"] == top.run_id and one["contribution"]["agent_id"] == "researcher"
    assert one["labels"]["trust_zone"] == "untrusted" and set(one) == {"claim_uri", "new", "contribution", "labels"}
    assert json.loads(first.content)["claim"] == view.claim.uri


@pytest.mark.parametrize("store", STORES)
async def test_a_ledger_is_read_only_within_its_tenant_and_project(store):
    async with Pages(PAGES) as pages:
        runner = runner_for(store, pages)
        top, _ = await run(runner, [["record_claim", {"statement": STATEMENT}]])
    [view] = await read(runner, top, "active")
    assert await read(runner, top, "active", tenant=OTHER_TENANT) == []
    assert await read(runner, top, "active", project="p-other") == []
    with pytest.raises(LookupError):
        await read(runner, top, "claim", view.claim.claim_id, tenant=OTHER_TENANT)


async def test_record_claim_outside_a_runner_is_refused():
    from agentsdk.executor import ToolExecutor
    from agentsdk.permissions import AllowlistPermissionChecker
    from agentsdk.tools import ToolRegistry

    registry = ToolRegistry()
    registry.register(RECORD)
    executor = ToolExecutor(registry, AllowlistPermissionChecker({"record_claim"}), emit=lambda *a: None)
    outcome = await executor.execute(ToolCall(id="c", name="record_claim", arguments={"statement": STATEMENT}))
    assert outcome.result.is_error and "Runner" in outcome.result.content


@pytest.mark.parametrize("arguments", [
    {"statement": ""}, {"statement": STATEMENT, "confidence": 1.5}, {"statement": STATEMENT, "confidence": -0.1},
    {"statement": STATEMENT, "supporting": "not a list"},
])
async def test_bad_arguments_are_refused_before_anything_is_recorded(arguments):
    async with Pages(PAGES) as pages:
        runner = runner_for("memory", pages)
        top, [refused] = await run(runner, [["record_claim", arguments]])
    assert refused.is_error and await read(runner, top, "active") == []


# =================================================================================================
# FR-94: supersession
# =================================================================================================


@pytest.mark.parametrize("store", STORES)
async def test_a_chain_of_three_reads_back_oldest_first_and_only_its_tip_is_active(store):
    async with Pages(PAGES) as pages:
        runner = runner_for(store, pages)
        top, [first] = await run(runner, [["record_claim", {"statement": "The river is 1,200 km long."}]])
        first = json.loads(first.content)["claim"]
        _, [second] = await run(runner, [["supersede_claim", {
            "claim": first, "statement": "The river is 1,230 km long.", "supporting": [], "why": "a better survey"}]],
            ledger_run_id=top.run_id)
        second = json.loads(second.content)["claim"]
        superseding, [third] = await run(runner, [["supersede_claim", {
            "claim": second, "statement": "The river is 1,233 km long.", "why": "the official figure"}]],
            ledger_run_id=top.run_id)
        third = json.loads(third.content)["claim"]
    chain = await read(runner, top, "history", second.rsplit(":", 1)[1])
    assert [c.uri for c in chain] == [first, second, third]
    assert [c.statement for c in chain] == ["The river is 1,200 km long.", "The river is 1,230 km long.",
                                           "The river is 1,233 km long."]
    active = {v.claim.uri for v in await read(runner, top, "active")}
    assert third in active and first not in active and second not in active
    tip = await read(runner, top, "claim", third.rsplit(":", 1)[1])
    assert tip.contributions[0].why == "the official figure" and tip.claim.supersedes == second.rsplit(":", 1)[1]
    old = await read(runner, top, "claim", first.rsplit(":", 1)[1])
    assert not old.active and old.superseded_by == second.rsplit(":", 1)[1]
    [event] = events(superseding, EventType.CLAIM_SUPERSEDED)
    assert event == {"claim_uri": third, "supersedes_uri": second, "why": "the official figure"}


@pytest.mark.parametrize("store", STORES)
async def test_superseding_a_superseded_claim_is_refused(store):
    async with Pages(PAGES) as pages:
        runner = runner_for(store, pages)
        top, [first, second, refused] = await run(runner, [
            ["record_claim", {"statement": "Old figure."}],
            ["supersede_claim", {"claim": "$0", "statement": "New figure.", "why": "w"}],
            ["supersede_claim", {"claim": "$0", "statement": "Another figure.", "why": "w"}]])
    assert not second.is_error and refused.is_error and "already superseded" in refused.content
    assert [v.claim.statement for v in await read(runner, top, "active")] == ["New figure."]


@pytest.mark.parametrize("store", STORES)
async def test_a_supersession_matching_another_active_claim_is_refused_naming_it(store):
    async with Pages(PAGES) as pages:
        runner = runner_for(store, pages)
        top, [a, b, refused] = await run(runner, [
            ["record_claim", {"statement": "Claim A."}], ["record_claim", {"statement": "Claim B."}],
            ["supersede_claim", {"claim": "$0", "statement": "claim b.", "why": "w"}]])
    assert refused.is_error and json.loads(b.content)["claim"] in refused.content
    assert len(await read(runner, top, "active")) == 2


@pytest.mark.parametrize("store", STORES)
async def test_a_statement_matching_only_a_superseded_claim_records_a_new_active_claim(store):
    async with Pages(PAGES) as pages:
        runner = runner_for(store, pages)
        top, [old, new, again] = await run(runner, [
            ["record_claim", {"statement": "Old figure."}],
            ["supersede_claim", {"claim": "$0", "statement": "New figure.", "why": "w"}],
            ["record_claim", {"statement": "OLD figure."}]])
    assert json.loads(again.content)["new"] is True
    assert json.loads(again.content)["claim"] != json.loads(old.content)["claim"]
    assert sorted(v.claim.statement for v in await read(runner, top, "active")) == ["New figure.", "OLD figure."]


def _ledger_store(store):
    return PostgresLedgerStore(DSN) if store == "postgres" else InMemoryLedgerStore()


async def _a_top_run(store):
    """A real run for rows to belong to, with no claims of its own."""
    async with Pages(PAGES) as pages:
        runner = runner_for(store, pages)
        top, _ = await run(runner, [])
    return runner, top


def _contribution(top):
    from agentsdk.ledger import NewContribution

    return NewContribution(run_id=top.run_id, node_id=None, agent_id="a", supporting=(), contradicting=(),
                           confidence=None, provenance=ContentProvenance.from_model(), why=None)


@pytest.mark.parametrize("store", STORES)
async def test_two_racing_supersessions_of_one_claim_never_both_succeed(store):
    runner, top = await _a_top_run(store)
    ledger_store = runner._ledger_store()
    scope = dict(tenant_id=TENANT, project_id=PROJECT, ledger_run_id=top.run_id)
    switch = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        for attempt in range(6):
            base, _, _ = await asyncio.to_thread(ledger_store.record, statement=f"Base {attempt}.",
                                                 contribution=_contribution(top), **scope)
            outcomes, gate = [], threading.Barrier(4)

            def supersede(n):
                gate.wait()
                try:
                    ledger_store.supersede(old_claim_id=base.claim_id, statement=f"Racer {attempt}.{n}.",
                                           contribution=dataclasses.replace(_contribution(top), why="race"), **scope)
                    outcomes.append("won")
                except ClaimRefused as exc:
                    outcomes.append(str(exc))

            threads = [threading.Thread(target=supersede, args=(n,)) for n in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            assert outcomes.count("won") == 1, outcomes
            assert all("already superseded" in o for o in outcomes if o != "won"), outcomes
    finally:
        sys.setswitchinterval(switch)


@pytest.mark.parametrize("store", STORES)
async def test_concurrent_records_of_one_new_statement_store_one_claim(store):
    """DECISION-9de80b77: the store serialises per ledger and key, so correctness does
    not wait for M24's claim-lock."""
    runner, top = await _a_top_run(store)
    ledger_store = runner._ledger_store()
    scope = dict(tenant_id=TENANT, project_id=PROJECT, ledger_run_id=top.run_id)
    switch = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        for attempt in range(6):
            news, gate = [], threading.Barrier(6)
            variants = [f"Shared statement {attempt}.", f"shared  STATEMENT {attempt}."]

            def record(n):
                gate.wait()
                _, _, new = ledger_store.record(statement=variants[n % 2], contribution=_contribution(top), **scope)
                news.append(new)

            threads = [threading.Thread(target=record, args=(n,)) for n in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            assert sorted(news) == [False] * 5 + [True], news
    finally:
        sys.setswitchinterval(switch)
    views = await read(runner, top, "active")
    assert len(views) == 6 and all(len(v.contributions) == 6 for v in views)


# =================================================================================================
# M22's review caveats, settled at M23's pre-flight (DECISION-9de80b77)
# =================================================================================================


async def test_caveat1_a_criterion_tools_scope_carries_the_runs_ledger_and_cache():
    from decimal import Decimal

    from agentsdk import BudgetPolicy, Orchestrator
    from agentsdk.registry import ModelCapabilities, ModelEntry, ModelPricing, ModelRegistry
    from agentsdk.tools import Tool, ToolSpec

    seen = []

    async def probe(scope: ToolScope) -> str:
        seen.append((scope.ledger_run_id, scope.evidence is not None, scope.ledger is not None))
        return "ok"

    probe_tool = Tool(spec=ToolSpec(name="probe", description="p", input_schema={"type": "object"}), fn=probe)
    plan = {"nodes": [{"node_id": "n", "objective": "go", "assigned_role": "worker", "dependencies": [],
                       "acceptance_criteria": [{"kind": "tool_succeeds", "target": "probe"}]}]}

    class Planner:
        calls = 0

        async def send(self, request):
            Planner.calls += 1
            if Planner.calls == 1:
                return ModelResponse(message=Message(role=Role.ASSISTANT, tool_calls=(
                    ToolCall(id="p", name="run_plan", arguments=plan),)), stop_reason=StopReason.TOOL_CALLS,
                    usage=Usage(5, 1, 6))
            return ModelResponse(message=Message(role=Role.ASSISTANT, content="done"),
                                 stop_reason=StopReason.END_TURN, usage=Usage(5, 1, 6))

    class Worker:
        async def send(self, request):
            return ModelResponse(message=Message(role=Role.ASSISTANT, content="done"),
                                 stop_reason=StopReason.END_TURN, usage=Usage(5, 1, 6))

    price = ModelPricing(input=Decimal("0.01"), output=Decimal("0.01"))
    registry = ModelRegistry([ModelEntry(provider="t", model_id=m, model_version="1", adapter_version="t",
                                         capabilities=ModelCapabilities(max_context_tokens=100_000, pricing=price))
                              for m in ("p", "w")])
    worker = AgentSpec(id="worker", instructions="w", preferred_model="w:w", tool_profile=("probe",))
    orchestrator = Orchestrator(roles={"worker": worker}, policy=BudgetPolicy(run_ceiling_tokens=100_000))
    runner = Runner({"p": Planner(), "w": Worker()}, tools=[orchestrator.tool, probe_tool], model_registry=registry)
    result = await runner.run(orchestrator.agent, "go",
                              orchestrator.config(tenant_id=TENANT, project_id=PROJECT, model_override="p:p"))
    assert result.status is RunStatus.COMPLETED, result.error
    assert seen == [(result.run_id, True, True)], seen


async def test_caveat2_an_orchestrator_that_is_a_child_passes_its_top_level_ledger_on(monkeypatch):
    from decimal import Decimal

    from agentsdk import BudgetPolicy, Orchestrator, api
    from agentsdk.registry import ModelCapabilities, ModelEntry, ModelPricing, ModelRegistry

    seen = {}
    real = api.ToolExecutor

    def recording(*args, **kwargs):
        scope = kwargs["scope"]
        seen[scope.run_id] = scope.ledger_run_id
        return real(*args, **kwargs)

    monkeypatch.setattr(api, "ToolExecutor", recording)
    plan = {"nodes": [{"node_id": "n", "objective": "go", "assigned_role": "worker", "dependencies": []}]}

    class Planner:
        calls = 0

        async def send(self, request):
            Planner.calls += 1
            if Planner.calls == 1:
                return ModelResponse(message=Message(role=Role.ASSISTANT, tool_calls=(
                    ToolCall(id="p", name="run_plan", arguments=plan),)), stop_reason=StopReason.TOOL_CALLS,
                    usage=Usage(5, 1, 6))
            return ModelResponse(message=Message(role=Role.ASSISTANT, content="done"),
                                 stop_reason=StopReason.END_TURN, usage=Usage(5, 1, 6))

    class Worker:
        async def send(self, request):
            return ModelResponse(message=Message(role=Role.ASSISTANT, content="done"),
                                 stop_reason=StopReason.END_TURN, usage=Usage(5, 1, 6))

    price = ModelPricing(input=Decimal("0.01"), output=Decimal("0.01"))
    registry = ModelRegistry([ModelEntry(provider="t", model_id=m, model_version="1", adapter_version="t",
                                         capabilities=ModelCapabilities(max_context_tokens=100_000, pricing=price))
                              for m in ("p", "w")])
    worker = AgentSpec(id="worker", instructions="w", preferred_model="w:w")
    orchestrator = Orchestrator(roles={"worker": worker}, policy=BudgetPolicy(run_ceiling_tokens=100_000))
    runner = Runner({"p": Planner(), "w": Worker(), "m": Scripted()}, tools=[orchestrator.tool],
                    model_registry=registry)
    top = await runner.run(AgentSpec(id="top", instructions="t"), "[]", config())
    as_child = await runner.run(orchestrator.agent, "go", orchestrator.config(
        tenant_id=TENANT, project_id=PROJECT, model_override="p:p", parent_run_id=top.run_id,
        ledger_run_id=top.run_id))
    assert as_child.status is RunStatus.COMPLETED, as_child.error
    assert set(seen.values()) == {top.run_id} and len(seen) == 3, seen


@pytest.mark.parametrize("given,canonical", [
    ("http://Rivers.TEST./a", "http://rivers.test/a"),
    ("http://bücher.test/a", "http://xn--bcher-kva.test/a"),
    ("http://BÜCHER.test:80/a#f", "http://xn--bcher-kva.test/a"),
])
def test_caveat3_the_cache_keys_host_is_normalised_as_the_allowlist_normalises_it(given, canonical):
    assert canonical_uri(given) == canonical


@pytest.mark.parametrize("store", STORES)
async def test_caveat4_a_version_that_cannot_be_recorded_leaves_no_artifact(store, monkeypatch):
    async with Pages(PAGES) as pages:
        runner = runner_for(store, pages)
        real = runner._evidence_store

        class Failing:
            def __getattr__(self, name):
                return getattr(real(), name)

            def record(self, *args, **kwargs):
                raise RuntimeError("the evidence store failed")

        monkeypatch.setattr(runner, "_evidence_store", lambda: Failing())
        top, [failed] = await run(runner, [["fetch_url", {"url": "http://rivers.test/rhine"}]])
    assert failed.is_error
    if store == "postgres":
        [(count,)] = await asyncio.to_thread(query, "SELECT count(*) FROM artifacts WHERE source_run = %s", (top.run_id,))
    else:
        count = sum(1 for ref, _ in runner._memory_artifacts._rows.values() if ref.source_run == top.run_id)
    assert count == 0


def test_caveat5_a_scope_refuses_a_ledger_run_id_that_is_not_canonical():
    run_id = str(uuid.uuid4())
    with pytest.raises(ValueError, match="ledger_run_id"):
        ToolScope(run_id=run_id, tenant_id=TENANT, project_id=PROJECT, ledger_run_id=run_id.upper())
    assert ToolScope(run_id=run_id, tenant_id=TENANT, project_id=PROJECT, ledger_run_id=run_id).ledger_run_id == run_id


# =================================================================================================
# Citing what was read (the owner's M23 pre-flight addendum)
# =================================================================================================


@pytest.mark.parametrize("store", STORES)
async def test_a_claim_can_cite_a_url_the_run_fetched_as_the_version_it_was_given(store):
    async with Pages(PAGES) as pages:
        runner = runner_for(store, pages)
        top, [page, recorded] = await run(runner, [
            ["fetch_url", {"url": "http://rivers.test/rhine"}],
            ["record_claim", {"statement": STATEMENT, "supporting": ["HTTP://Rivers.TEST/rhine"]}]])
    assert not recorded.is_error, recorded.content
    [view] = await read(runner, top, "active")
    assert [f"urn:agentsdk:source:{v}" for v in view.supporting] == [page.provenance.source_uri_or_hash]


@pytest.mark.parametrize("store", STORES)
async def test_a_url_the_run_never_fetched_is_refused_even_when_another_run_did(store):
    async with Pages(PAGES) as pages:
        runner = runner_for(store, pages)
        top, _ = await run(runner, [["fetch_url", {"url": "http://rivers.test/rhine"}]])
        other, [refused] = await run(runner, [
            ["record_claim", {"statement": STATEMENT, "supporting": ["http://rivers.test/rhine"]}]])
    assert refused.is_error and "http://rivers.test/rhine" in refused.content
    assert await read(runner, other, "active") == []


async def test_a_url_cites_the_newest_version_the_run_was_given():
    from datetime import datetime, timedelta, timezone

    class Ticking:
        """Every fetch is past the last one's freshness, so each records a new version."""

        now = datetime(2026, 10, 11, tzinfo=timezone.utc)

        def __call__(self):
            Ticking.now += timedelta(seconds=61)
            return Ticking.now

    async with Pages(PAGES) as pages:
        async def resolve(host, port):
            return [PUBLIC]

        tool = fetch_tool(["rivers.test"], freshness_seconds=60, _clock=Ticking(), _resolve=resolve,
                          _connect=lambda ip, port: ("127.0.0.1", pages.port))
        runner = Runner({"m": Scripted()}, tools=[tool, RECORD, SUPERSEDE])
        top, [old, new, recorded] = await run(runner, [
            ["fetch_url", {"url": "http://rivers.test/rhine"}], ["fetch_url", {"url": "http://rivers.test/rhine"}],
            ["record_claim", {"statement": STATEMENT, "supporting": ["http://rivers.test/rhine"]}]])
    assert old.provenance.source_uri_or_hash != new.provenance.source_uri_or_hash
    [view] = await read(runner, top, "active")
    assert [f"urn:agentsdk:source:{v}" for v in view.supporting] == [new.provenance.source_uri_or_hash]


def test_the_example_shows_one_claim_two_contributions_and_a_supersession_offline():
    import subprocess
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    done = subprocess.run([sys.executable, str(repo / "scripts" / "20_ledger.py"), "--offline"],
                          capture_output=True, text=True, timeout=120, cwd=repo)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "[FAIL]" not in done.stdout and done.stdout.count("[PASS]") == 7, done.stdout
