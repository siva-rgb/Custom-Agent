"""M24 gate: claim-lock under contention, and the exit signal (FR-95, FR-96, NFR-24, AC-74).

One holder per key -- a page within one reach, or a statement within one ledger -- across
the tasks of a process and the processes sharing a database. A holder that fails is
retried by exactly one waiter with full jitter, for at most three attempts. The exit
signal is example 21, run here on both stores. M23's review caveats are settled here too
(DECISION-eecef939).
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import json
import os
import subprocess
import sys
import textwrap
import unicodedata
import uuid
from pathlib import Path

import psycopg
import pytest

from agentsdk import AgentSpec, Persistence, RunConfig, RunStatus, Runner
from agentsdk.builtin_tools import fetch_tool
from agentsdk.claimlock import ATTEMPTS, BASE_DELAY, ClaimLock, KeyFailed
from agentsdk.config import normalise_database_url
from agentsdk.events import EventType
from agentsdk.evidence import InMemoryEvidenceStore
from agentsdk.ledger import ClaimRefused, InMemoryLedgerStore, NewContribution, claim_key, ledger_tools
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.postgres import PostgresLedgerStore, RunScope
from agentsdk.primitives import ContentProvenance, Message, Role, ToolCall, TrustZone

REPO = Path(__file__).resolve().parent.parent
DSN = normalise_database_url(os.environ.get("DATABASE_URL"))
TENANT, PROJECT, OTHER_TENANT = "SYN-m24", "p-m24", "SYN-m24-other"
PUBLIC = "1.1.1.1"
STORES = ["memory", "postgres"]


def _example():
    spec = importlib.util.spec_from_file_location("example_21", REPO / "scripts" / "21_contention.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def query(sql, params=()):
    with psycopg.connect(DSN) as conn:
        return conn.execute(sql, params).fetchall()


@pytest.fixture(autouse=True)
def remove_what_the_test_wrote():
    yield
    if DSN:
        with psycopg.connect(DSN, autocommit=True) as conn:
            ids = [r[0] for r in conn.execute("SELECT run_id FROM runs WHERE tenant_id = ANY(%s)",
                                              ([TENANT, OTHER_TENANT],))]
            conn.execute("DELETE FROM claim_citations WHERE contribution_id IN (SELECT contribution_id FROM "
                         "claim_contributions WHERE run_id = ANY(%s))", (ids,))
            conn.execute("DELETE FROM claim_contributions WHERE run_id = ANY(%s)", (ids,))
            while conn.execute("DELETE FROM claims c WHERE ledger_run_id = ANY(%s) AND NOT EXISTS "
                               "(SELECT 1 FROM claims s WHERE s.supersedes = c.claim_id)", (ids,)).rowcount:
                pass
            conn.execute("DELETE FROM source_versions WHERE source_run = ANY(%s)", (ids,))
            conn.execute("DELETE FROM artifacts WHERE source_run = ANY(%s)", (ids,))
            for table in ("plan_node_states", "plan_versions", "run_events", "messages", "execution_manifests"):
                conn.execute(f"DELETE FROM {table} WHERE run_id = ANY(%s)", (ids,))
            conn.execute("DELETE FROM runs WHERE run_id = ANY(%s)", (ids,))


# =================================================================================================
# FR-96, AC-74: the exit signal on both stores
# =================================================================================================


@pytest.mark.parametrize("store", STORES)
async def test_the_exit_signal_holds_ten_times_with_shuffled_starts_and_random_delays(store):
    example = _example()
    waits = 0
    for seed in range(10):
        # A project of its own each time: the database keeps the last one's fresh versions.
        checks, _, waited = await example.offline(store, seed=seed, quiet=True, tenants=(TENANT, OTHER_TENANT),
                                                  project=f"{PROJECT}-{seed}")
        failed = [label for label, passed in checks if not passed]
        assert failed == [], f"seed {seed}: {failed}"
        waits += waited
    assert waits > 0, "no call ever waited: the run did not contend"


def test_the_example_runs_offline():
    done = subprocess.run([sys.executable, str(REPO / "scripts" / "21_contention.py"), "--offline"],
                          capture_output=True, text=True, timeout=180, cwd=REPO)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "[FAIL]" not in done.stdout and done.stdout.count("[PASS]") == 7, done.stdout


# =================================================================================================
# FR-95: the lock itself
# =================================================================================================


class Delays:
    def __init__(self):
        self.seen = []

    async def __call__(self, seconds):
        self.seen.append(seconds)
        await asyncio.sleep(0)


async def test_a_holder_that_fails_is_retried_by_exactly_one_waiter_then_every_waiter_fails_naming_the_key():
    delays = Delays()
    lock = ClaimLock(sleep=delays, random_fraction=lambda: 0.999)
    calls, gate = [], asyncio.Event()

    async def op(waited):
        calls.append(waited)
        if len(calls) == 1:
            await gate.wait()
        raise RuntimeError("the source is down")

    tasks = [asyncio.create_task(lock.run("fetch|t|p||project|http://x/", op)) for _ in range(4)]
    await asyncio.sleep(0.01)
    gate.set()
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    assert isinstance(outcomes[0], RuntimeError)
    assert all(isinstance(o, KeyFailed) and "http://x/" in str(o) for o in outcomes[1:]), outcomes
    assert len(calls) == 1 + ATTEMPTS, "a waiter other than the one retrier ran the operation"
    assert len(delays.seen) == ATTEMPTS
    for k, delay in enumerate(delays.seen):
        assert 0 <= delay < BASE_DELAY * 2 ** k, (k, delay)
    assert delays.seen[-1] > BASE_DELAY * 2, "the delays do not grow"
    # Once everyone waiting has failed, the key starts afresh.
    assert await lock.run("fetch|t|p||project|http://x/", lambda w: asyncio.sleep(0, result="fresh")) == "fresh"


async def test_a_retry_that_succeeds_lets_the_other_waiters_through():
    lock = ClaimLock(sleep=Delays(), random_fraction=lambda: 0.5)
    calls = []

    async def op(waited):
        calls.append(waited)
        if len(calls) <= 2:
            await asyncio.sleep(0.01)
            raise RuntimeError("flaky")
        return "page"

    outcomes = await asyncio.gather(*(lock.run("k", op) for _ in range(3)), return_exceptions=True)
    assert isinstance(outcomes[0], RuntimeError) and outcomes[1:] == ["page", "page"]
    assert len(calls) == 4  # the holder, the retrier twice, the last waiter once


async def test_a_cancelled_waiter_leaves_the_holder_running_and_its_result_recorded():
    lock = ClaimLock()
    recorded, release = [], asyncio.Event()

    async def holder(waited):
        await release.wait()
        recorded.append("version")
        return "version"

    first = asyncio.create_task(lock.run("k", holder))
    await asyncio.sleep(0.01)
    waiter = asyncio.create_task(lock.run("k", holder))
    await asyncio.sleep(0.01)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    assert await first == "version" and recorded == ["version"]
    assert await lock.run("k", lambda w: asyncio.sleep(0, result=w)) is None, "the key was left held"


async def test_a_waiter_is_told_how_long_it_waited_and_a_holder_is_not():
    lock = ClaimLock()
    seen = []

    async def op(waited):
        seen.append(waited)
        await asyncio.sleep(0.02)

    await asyncio.gather(lock.run("k", op), lock.run("k", op))
    assert seen[0] is None and seen[1] >= 0.01


async def test_different_keys_never_wait_on_each_other():
    lock = ClaimLock()
    running = []

    async def op(waited):
        running.append(waited)
        await asyncio.sleep(0.02)

    await asyncio.gather(lock.run("claim|SYN-a|p|L|k", op), lock.run("claim|SYN-b|p|L|k", op))
    assert running == [None, None]


async def test_across_processes_a_second_holder_waits_on_the_database_lock_without_a_connection():
    """Two ClaimLocks on one database stand for two processes: the second polls."""
    from agentsdk import postgres

    first, second = ClaimLock(DSN), ClaimLock(DSN, poll_seconds=0.01)
    inside, release, seen = asyncio.Event(), asyncio.Event(), []

    async def hold(waited):
        inside.set()
        await release.wait()

    async def follow(waited):
        seen.append(waited)

    key = f"fetch|{TENANT}|{uuid.uuid4()}"
    holder = asyncio.create_task(first.run(key, hold))
    await inside.wait()
    waiter = asyncio.create_task(second.run(key, follow))
    await asyncio.sleep(0.1)
    stats = postgres._pool(DSN).get_stats()
    assert seen == [] and stats.get("pool_size", 0) - stats.get("pool_available", 0) <= 1, stats
    release.set()
    await holder
    await waiter
    assert seen and seen[0] >= 0.05


async def test_a_waiter_cancelled_while_polling_the_database_leaves_no_lock_behind():
    first, second = ClaimLock(DSN), ClaimLock(DSN, poll_seconds=0.01)
    inside, release = asyncio.Event(), asyncio.Event()

    async def hold(waited):
        inside.set()
        await release.wait()

    key = f"fetch|{TENANT}|{uuid.uuid4()}"
    holder = asyncio.create_task(first.run(key, hold))
    await inside.wait()
    waiter = asyncio.create_task(second.run(key, lambda w: asyncio.sleep(0)))
    await asyncio.sleep(0.05)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    await holder
    third = ClaimLock(DSN)
    assert await asyncio.wait_for(third.run(key, lambda w: asyncio.sleep(0, result=w)), 2) is None


# =================================================================================================
# FR-95 through the tools: fetches and claims that wait say so
# =================================================================================================


class Slow:
    """A page server that takes a while, counting requests."""

    def __init__(self, seconds=0.2):
        self.seconds, self.requests = seconds, 0

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        self.requests += 1
        await asyncio.sleep(self.seconds)
        body = b"The Rhine reaches the North Sea."
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                     + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
        await writer.drain()
        writer.close()


class Scripted:
    async def send(self, request):
        plan = json.loads(request.messages[0].content)
        done = sum(1 for m in request.messages if m.tool_results)
        if done < len(plan):
            calls = tuple(ToolCall(id=f"c{done}-{i}", name=n, arguments=a) for i, (n, a) in enumerate(plan[done]))
            return ModelResponse(message=Message(role=Role.ASSISTANT, tool_calls=calls),
                                 stop_reason=StopReason.TOOL_CALLS, usage=Usage(5, 1, 6))
        return ModelResponse(message=Message(role=Role.ASSISTANT, content="done"),
                             stop_reason=StopReason.END_TURN, usage=Usage(5, 1, 6))


AGENT = AgentSpec(id="reader", instructions="read", tool_profile=("fetch_url", "record_claim", "supersede_claim"))
URL = "http://rivers.test/rhine"


def runner_for(store, pages):
    async def resolve(host, port):
        return [PUBLIC]

    record, supersede = ledger_tools()
    backend = Persistence.postgres(DSN, create_schema=False) if store == "postgres" else None
    fetch = fetch_tool(["rivers.test"], _resolve=resolve, _connect=lambda ip, port: ("127.0.0.1", pages.port))
    return Runner({"m": Scripted()}, tools=[fetch, record, supersede], persistence=backend)


def config(tenant=TENANT, **options):
    return RunConfig(tenant_id=tenant, project_id=PROJECT, model_override="m:fake", max_turns=10, **options)


@pytest.mark.parametrize("store", STORES)
async def test_runs_fetching_one_page_at_once_fetch_it_once_and_the_waiters_say_so(store):
    async with Slow() as pages:
        runner = runner_for(store, pages)
        results = await asyncio.gather(*(runner.run(AGENT, json.dumps([[["fetch_url", {"url": URL}]]]), config())
                                         for _ in range(4)))
        assert pages.requests == 1
    events = [e.payload for r in results for e in r.events
              if e.event_type in (EventType.SOURCE_FETCHED, EventType.SOURCE_SERVED)]
    waited = [p for p in events if "wait" in p]
    assert len(events) == 4 and len(waited) == 3, events
    assert all(p["wait"]["took_result"] is True and p["wait"]["seconds"] > 0 for p in waited)


@pytest.mark.parametrize("store", STORES)
async def test_two_tenants_fetching_one_page_at_once_never_wait_on_each_other(store):
    """NFR-25: a lock key is the page within one reach, so no wait crosses a tenant."""
    async with Slow() as pages:
        runner = runner_for(store, pages)
        results = await asyncio.gather(*(runner.run(AGENT, json.dumps([[["fetch_url", {"url": URL}]]]), config(tenant=t))
                                         for t in (TENANT, OTHER_TENANT)))
        assert pages.requests == 2
    events = [e.payload for r in results for e in r.events if e.event_type is EventType.SOURCE_FETCHED]
    assert len(events) == 2 and not any("wait" in p for p in events), events


@pytest.mark.parametrize("store", STORES)
async def test_records_of_one_statement_at_once_wait_and_contribute_to_the_holders_claim(store, monkeypatch):
    import time

    async with Slow() as pages:
        runner = runner_for(store, pages)
        kind = type(runner._ledger_store())
        real = kind.record

        def slow(self, **kwargs):
            time.sleep(0.1)  # a store write slow enough that the records overlap
            return real(self, **kwargs)

        monkeypatch.setattr(kind, "record", slow)
        top = await runner.run(AGENT, json.dumps([]), config())
        statement = "The Rhine reaches the North Sea."
        results = await asyncio.gather(*(runner.run(AGENT, json.dumps([[["record_claim", {"statement": statement}]]]),
                                                    config(ledger_run_id=top.run_id)) for _ in range(4)))
    recorded = [e.payload for r in results for e in r.events if e.event_type is EventType.CLAIM_RECORDED]
    assert sorted(p["new"] for p in recorded) == [False, False, False, True]
    waited = [p for p in recorded if "wait" in p]
    assert waited and all(p["wait"]["took_result"] is True for p in waited)
    [view] = await asyncio.to_thread(runner.evidence_ledger(TENANT, PROJECT, top.run_id).active)
    assert len(view.contributions) == 4


def test_two_processes_on_one_database_contend_for_one_key_and_fetch_once():
    """AC-74: two Python processes, one database, one page: one request."""
    script = textwrap.dedent('''
        import asyncio, json, sys
        from dotenv import load_dotenv
        load_dotenv()
        import os
        from agentsdk import AgentSpec, Persistence, RunConfig, Runner
        from agentsdk.builtin_tools import fetch_tool
        from agentsdk.config import normalise_database_url
        from agentsdk.model import ModelResponse, StopReason, Usage
        from agentsdk.primitives import Message, Role, ToolCall

        port, tenant, project, url = int(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]

        class Once:
            async def send(self, request):
                if any(m.tool_results for m in request.messages):
                    return ModelResponse(message=Message(role=Role.ASSISTANT, content="done"),
                                         stop_reason=StopReason.END_TURN, usage=Usage(1, 1, 2))
                return ModelResponse(message=Message(role=Role.ASSISTANT, tool_calls=(
                    ToolCall(id="c", name="fetch_url", arguments={"url": url}),)),
                    stop_reason=StopReason.TOOL_CALLS, usage=Usage(1, 1, 2))

        async def resolve(host, p):
            return ["1.1.1.1"]

        async def main():
            backend = Persistence.postgres(normalise_database_url(os.environ["DATABASE_URL"]), create_schema=False)
            runner = Runner({"m": Once()}, persistence=backend,
                            tools=[fetch_tool(["rivers.test"], _resolve=resolve, _connect=lambda ip, p: ("127.0.0.1", port))])
            result = await runner.run(AgentSpec(id="a", instructions="i", tool_profile=("fetch_url",)), "go",
                                      RunConfig(tenant_id=tenant, project_id=project, model_override="m:x"))
            kinds = [e.event_type.value for e in result.events if e.event_type.value.startswith("Source")]
            print(json.dumps({"status": result.status.value, "kinds": kinds}))

        asyncio.run(main())
    ''')

    async def contend():
        async with Slow(seconds=1.5) as pages:
            processes = [await asyncio.create_subprocess_exec(
                sys.executable, "-c", script, str(pages.port), TENANT, PROJECT, URL, cwd=REPO,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE) for _ in range(2)]
            outs = [await p.communicate() for p in processes]
            return pages.requests, outs

    requests, outs = asyncio.run(contend())
    reports = [json.loads(out.decode().strip().splitlines()[-1]) for out, _ in outs]
    assert all(r["status"] == "completed" for r in reports), outs
    assert requests == 1, (requests, reports)
    assert sorted(k for r in reports for k in r["kinds"]) == ["SourceFetched", "SourceServed"], reports


def test_no_new_runtime_dependency():
    """NFR-24: Phase 3's first increment adds no runtime dependency."""
    import tomllib

    declared = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    assert sorted(declared) == ["httpx==0.28.1", "jsonschema==4.26.0", "psycopg[binary]==3.3.5",
                                "psycopg_pool==3.2.6", "python-dotenv==1.2.3"]


# =================================================================================================
# M23's review caveats, settled at M24's pre-flight (DECISION-eecef939)
# =================================================================================================


async def _version(store, tenant=TENANT):
    """A real source version, fetched by a real run in `tenant`."""
    async with Slow(seconds=0) as pages:
        runner = runner_for(store, pages)
        result = await runner.run(AGENT, json.dumps([[["fetch_url", {"url": URL}]]]), config(tenant=tenant))
    sessions = runner._sessions_for(RunScope(run_id=result.run_id, tenant_id=tenant, project_id=PROJECT))
    history = await asyncio.to_thread(sessions.history, result.run_id)
    uri = [r for m in history for r in m.tool_results][0].provenance.source_uri_or_hash
    return runner, result, uri.rsplit(":", 1)[1]


def _clean(run_id, *cited):
    return NewContribution(run_id=run_id, node_id=None, agent_id="direct", supporting=tuple(cited), contradicting=(),
                           confidence=None, provenance=ContentProvenance.from_model(), why=None)


@pytest.mark.parametrize("store", STORES)
async def test_caveat1_the_store_refuses_a_citation_from_another_tenant(store):
    runner, _, foreign = await _version(store, tenant=OTHER_TENANT)
    async with Slow(seconds=0) as pages:
        mine = await runner.run(AGENT, json.dumps([]), config()) if store == "memory" else \
            await runner_for(store, pages).run(AGENT, json.dumps([]), config())
    ledger = runner._ledger_store()
    with pytest.raises(ClaimRefused, match=foreign):
        await asyncio.to_thread(ledger.record, tenant_id=TENANT, project_id=PROJECT, ledger_run_id=mine.run_id,
                                statement="A claim.", contribution=_clean(mine.run_id, foreign))
    assert await asyncio.to_thread(runner.evidence_ledger(TENANT, PROJECT, mine.run_id).active) == []


@pytest.mark.parametrize("store", STORES)
async def test_caveat1_the_store_raises_a_clean_contribution_to_cover_what_it_cites(store):
    runner, result, own = await _version(store)
    ledger = runner._ledger_store()
    await asyncio.to_thread(ledger.record, tenant_id=TENANT, project_id=PROJECT, ledger_run_id=result.run_id,
                            statement="A claim.", contribution=_clean(result.run_id, own))
    [view] = await asyncio.to_thread(runner.evidence_ledger(TENANT, PROJECT, result.run_id).active)
    assert view.contributions[0].provenance.trust_zone is TrustZone.UNTRUSTED
    assert view.provenance.trust_zone is TrustZone.UNTRUSTED


def test_caveat1_a_store_with_no_evidence_store_refuses_any_citation():
    run = str(uuid.uuid4())
    with pytest.raises(ClaimRefused):
        InMemoryLedgerStore().record(tenant_id=TENANT, project_id=PROJECT, ledger_run_id=run, statement="A.",
                                     contribution=_clean(run, str(uuid.uuid4())))


def test_caveat2_keys_are_locked_in_the_order_of_their_lock_values_each_once():
    """Ordered by the lock value itself, so two keys whose hashes collide are one lock and
    two writers can never take the same two locks in opposite orders."""
    keys = [f"k{n}" for n in range(12)]
    ledger = str(uuid.uuid4())
    with psycopg.connect(DSN) as conn:
        with conn.transaction():
            values = PostgresLedgerStore._lock(conn, TENANT, PROJECT, ledger, *reversed(keys), keys[0])
            hashes = {k: conn.execute("SELECT hashtext(%s)", (f"{TENANT}|{PROJECT}|{ledger}|{k}",)).fetchone()[0]
                      for k in keys}
            held = conn.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
                                " AND objid = 3").fetchone()[0]
    assert values == sorted(set(hashes.values())), "not taken in lock-value order, or not each once"
    assert [hashes[k] for k in sorted(keys)] != values, "the keys' text order happens to be their value order"
    assert held == len(values)


async def test_caveat3_a_url_cites_the_runs_own_newer_fetch_over_an_older_briefed_version():
    from datetime import datetime, timedelta, timezone

    class Ticking:
        now = datetime(2026, 10, 11, tzinfo=timezone.utc)

        def __call__(self):
            Ticking.now += timedelta(seconds=61)
            return Ticking.now

    async with Slow(seconds=0) as pages:
        async def resolve(host, port):
            return [PUBLIC]

        record, supersede = ledger_tools()
        tool = fetch_tool(["rivers.test"], freshness_seconds=60, _clock=Ticking(), _resolve=resolve,
                          _connect=lambda ip, port: ("127.0.0.1", pages.port))
        runner = Runner({"m": Scripted()}, tools=[tool, record, supersede])
        first = await runner.run(AGENT, json.dumps([[["fetch_url", {"url": URL}]]]), config())
        [old_result] = [r for m in await asyncio.to_thread(runner._history_for, RunScope(
            run_id=first.run_id, tenant_id=TENANT, project_id=PROJECT)) for r in m.tool_results]
        second = await runner.run(AGENT, json.dumps([[["fetch_url", {"url": URL}]],
                                                     [["record_claim", {"statement": "S.", "supporting": [URL]}]]]),
                                  config(briefed_inputs=((old_result.provenance.source_uri_or_hash, old_result.provenance),)))
    results = [r for m in await asyncio.to_thread(runner._history_for, RunScope(
        run_id=second.run_id, tenant_id=TENANT, project_id=PROJECT)) for r in m.tool_results]
    newer = results[0].provenance.source_uri_or_hash
    assert newer != old_result.provenance.source_uri_or_hash
    [view] = await asyncio.to_thread(runner.evidence_ledger(TENANT, PROJECT, second.run_id).active)
    assert [f"urn:agentsdk:source:{v}" for v in view.supporting] == [newer]


def test_caveat4_case_folding_is_canonically_closed():
    assert claim_key("ΐ is a letter.") == claim_key("Ϊ́ is a letter.")
    assert claim_key(unicodedata.normalize("NFD", "ΐ is a letter.")) == claim_key("ΐ is a letter.")


async def test_caveat5_recording_a_claim_reads_back_only_its_own_claim(monkeypatch):
    def whole_ledger(*args, **kwargs):
        raise AssertionError("record_claim read the whole ledger back")

    monkeypatch.setattr(InMemoryLedgerStore, "rows", whole_ledger)
    async with Slow(seconds=0) as pages:
        runner = runner_for("memory", pages)
        result = await runner.run(AGENT, json.dumps([[["record_claim", {"statement": "One."}]],
                                                     [["record_claim", {"statement": "one."}]]]), config())
    assert result.status is RunStatus.COMPLETED
    recorded = [e.payload for e in result.events if e.event_type is EventType.CLAIM_RECORDED]
    assert [p["new"] for p in recorded] == [True, False]
