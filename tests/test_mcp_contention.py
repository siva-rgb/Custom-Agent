"""M37 gate: hostile MCP servers under orchestration, the exit signal (FR-135, AC-94, NFR-22,
NFR-34 to NFR-39), and its pre-flight (DECISION-8e3e2ea1).

Example 31 is the exit signal: an orchestrated plan whose children use five real local MCP
servers -- one changing a tool mid-run, one poisoning a description, one shadowing another's
tool name, one injecting instructions in a result, one asking for input -- with a scripted
approver deciding from the top-level run. It runs ten times on both stores with the children's
start order shuffled and random delays, and each check fails when its subject is removed.

The pre-flight's two owner decisions: `Runner.mcp_blocked` lists what a tenancy's connection
blocks now, so an approver can accept a version only a child's run met (FR-127); and an input
request over 64 KiB is refused, recorded nowhere, and fails its call (FR-133).

Windows only, as NFR-39 is measured there (D11). The persisted half needs DATABASE_URL and
removes every row it writes.
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid

import psycopg
import pytest

from agentsdk import RunConfig, UnknownMcpServer
from agentsdk.events import EventType
from agentsdk.identity import PrincipalContext
from agentsdk.mcp import McpBlocked
from tests.test_mcp_catalog import ECHO, PARTS, Clock, catalog_server, reads, state_file
from tests.test_mcp_client import DSN, REPO, STORES, agent, results, run, runner_for
from tests.test_mcp_http import KEY, accepted, serve, web  # noqa: F401 - the fixtures, used by name

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="NFR-39 is measured on Windows (D11)")

PREFIX = "SYN-m37-"
PROJECT = "p-m37"
OWNER = PrincipalContext(agent_principal="catalog-ui", user_principal="owner")
CAP = 64 * 1024
TOOLS = [{"name": "ask", "description": "The fixture's ask.", "inputSchema": {"type": "object"}}]


@pytest.fixture(autouse=True)
def remove_what_the_test_wrote():
    yield
    if DSN:
        with psycopg.connect(DSN, autocommit=True) as conn:
            ids = [r[0] for r in conn.execute("SELECT run_id FROM runs WHERE tenant_id LIKE %s", (PREFIX + "%",))]
            conn.execute("DELETE FROM interruption_decisions WHERE interruption_id IN (SELECT interruption_id"
                         " FROM interruptions WHERE run_id = ANY(%s))", (ids,))
            conn.execute("DELETE FROM interruptions WHERE run_id = ANY(%s)", (ids,))
            conn.execute("DELETE FROM standing_decisions WHERE run_id = ANY(%s)", (ids,))
            conn.execute("DELETE FROM artifacts WHERE source_run = ANY(%s)", (ids,))
            for table in ("plan_node_states", "plan_versions", "run_events", "messages", "execution_manifests"):
                conn.execute(f"DELETE FROM {table} WHERE run_id = ANY(%s)", (ids,))
            while conn.execute("DELETE FROM runs r WHERE run_id = ANY(%s) AND NOT EXISTS "
                               "(SELECT 1 FROM runs c WHERE c.parent_run_id = r.run_id)", (ids,)).rowcount:
                pass
            conn.execute("DELETE FROM artifacts WHERE tenant_id LIKE %s", (PREFIX + "%",))
            conn.execute("DELETE FROM mcp_tool_acceptances WHERE tenant_id LIKE %s", (PREFIX + "%",))
            conn.execute("DELETE FROM mcp_tool_versions WHERE tenant_id LIKE %s", (PREFIX + "%",))


def tenancy() -> str:
    return f"{PREFIX}{uuid.uuid4().hex[:10]}"


async def settled(log, quiet=1.0, timeout=20.0) -> int:
    """The read count once no read has come for `quiet` seconds."""
    import asyncio
    import time

    deadline, count = time.monotonic() + timeout, reads(log)
    while time.monotonic() < deadline:
        await asyncio.sleep(quiet)
        if reads(log) == count:
            return count
        count = reads(log)
    raise AssertionError("the fixture kept reading its listing")


def by_name(listed) -> dict:
    return {item.name: item for item in listed}


def _example():
    import importlib.util

    spec = importlib.util.spec_from_file_location("example_31", REPO / "scripts" / "31_mcp_contention.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# =================================================================================================
# FR-127 (owner, M37 pre-flight): Runner.mcp_blocked
# =================================================================================================


@pytest.mark.parametrize("store", STORES)
@pytest.mark.parametrize("kind", ["legacy", "modern"])
async def test_mcp_blocked_lists_first_sight_nothing_once_accepted_and_a_change_once_the_listing_is_stale(
        store, kind, tmp_path):
    tenant, clock, log = tenancy(), Clock(), tmp_path / "reads"
    # A long ttlMs: a modern server with no cache hint lists with ttlMs 0, stale at once (FR-127).
    runner = runner_for([catalog_server(kind, state_file(tmp_path, [ECHO], ttl_ms=600_000), reads=log)], store)
    async with runner:
        runner._mcp.clock = clock
        listed = await runner.mcp_blocked(tenant, PROJECT, kind)
        assert all(isinstance(item, McpBlocked) for item in listed)
        first = by_name(listed)["echo"]  # the fixture's catalog mode also lists `notify`
        assert (first.kind, first.name, first.previous_hash, first.changed, first.error) == (
            "tool", "echo", None, None, None)
        rows = await runner.accept_tools(tenant, PROJECT, kind, [b.tool_hash for b in listed], principal=OWNER)
        assert sorted(r.tool_name for r in rows) == sorted(by_name(listed)), "the hashes it lists are accept_tools'"
        assert await runner.mcp_blocked(tenant, PROJECT, kind) == ()
        # The modern era's listen subscription reads in the background as it opens, and a
        # notification during a read leaves the listing stale once more: both are spent first.
        await settled(log)
        assert await runner.mcp_blocked(tenant, PROJECT, kind) == ()
        before = await settled(log)
        state_file(tmp_path, [PARTS["description"](ECHO)], ttl_ms=600_000)  # changed, and not announced
        assert await runner.mcp_blocked(tenant, PROJECT, kind) == (), "a listing not yet stale is not read again"
        assert reads(log) == before
        clock.now += 61
        listed = await runner.mcp_blocked(tenant, PROJECT, kind)
        assert reads(log) == before + 1, "a stale listing is read again first"
        if kind == "modern":
            return  # the fixture's modern era serves a rewritten state only once `notify` announces it
        [changed] = listed
        assert (changed.name, changed.previous_hash, changed.changed) == ("echo", first.tool_hash, ["description"])
        assert changed.tool_hash != first.tool_hash
        await runner.accept_tools(tenant, PROJECT, kind, [changed.tool_hash], principal=OWNER)
        assert await runner.mcp_blocked(tenant, PROJECT, kind) == ()


async def test_mcp_blocked_lists_another_tenancys_acceptance_as_still_blocked(tmp_path):
    """It reads the tenancy it names: one tenancy's acceptance unblocks nothing in another."""
    mine, theirs = tenancy(), tenancy()
    runner = runner_for([catalog_server("modern", state_file(tmp_path, [ECHO]))])
    async with runner:
        listed = await runner.mcp_blocked(theirs, PROJECT, "modern")
        await runner.accept_tools(theirs, PROJECT, "modern", [b.tool_hash for b in listed], principal=OWNER)
        assert await runner.mcp_blocked(theirs, PROJECT, "modern") == ()
        still = await runner.mcp_blocked(mine, PROJECT, "modern")
    assert by_name(still) == by_name(listed) and all(b.previous_hash is None for b in still)


async def test_mcp_blocked_names_a_catalog_it_could_not_read(monkeypatch, tmp_path):
    """FR-127, NFR-36: a failure to read acceptances blocks the tool, and says why."""
    import agentsdk.mcp as mcp

    def unreadable(*args):
        raise RuntimeError("the catalog is down")

    monkeypatch.setattr(mcp, "_read_catalog", unreadable)
    runner = runner_for([catalog_server("modern", state_file(tmp_path, [ECHO]))])
    async with runner:
        listed = await runner.mcp_blocked(tenancy(), PROJECT, "modern")
    assert sorted(b.name for b in listed) == ["echo", "notify"] and {b.error for b in listed} == {"RuntimeError"}


async def test_mcp_blocked_refuses_as_mcp_resources_does(tmp_path):
    with pytest.raises(UnknownMcpServer):
        await runner_for([]).mcp_blocked(tenancy(), PROJECT, "modern")
    runner = runner_for([catalog_server("modern", state_file(tmp_path, [ECHO]))])
    async with runner:
        with pytest.raises(UnknownMcpServer):
            await runner.mcp_blocked(tenancy(), PROJECT, "elsewhere")
        for tenant, project in (("", PROJECT), (tenancy(), ""), (None, PROJECT)):
            with pytest.raises(ValueError):
                await runner.mcp_blocked(tenant, project, "modern")


# =================================================================================================
# FR-133 (owner, M37 pre-flight): an input request over 64 KiB
# =================================================================================================


def _request_size(message, field, value) -> int:
    return len(json.dumps({"message": message, field: value}))


def _sized(mode: str, size: int) -> dict:
    """The `ask` fixture's arguments for one request whose message, with its schema or URL, is
    `size` characters of JSON -- the measure the cap is stated in."""
    if mode == "url":
        base = "http://127.0.0.1:9/consent?pad="
        pad = size - _request_size("Open the page 0", "url", base)
        return {"mode": "url", "url": base + "a" * pad}
    schema = {"type": "object", "properties": {"name": {"type": "string", "description": ""}}}
    pad = size - _request_size("Who are you? (0)", "requested_schema", schema)
    schema["properties"]["name"]["description"] = "a" * pad
    return {"mode": "form", "schema": schema}


@pytest.mark.parametrize("store", STORES)
@pytest.mark.parametrize("mode", ["form", "url"])
@pytest.mark.parametrize("kind", ["modern", "legacy"])
async def test_an_input_request_over_64_kib_records_nothing_and_fails_its_call(store, mode, kind, serve, accepted):
    served, tenant = serve(kind, tools=TOOLS), tenancy()
    runner = runner_for([web(served)], store)
    async with runner:
        result = await run(runner, agent("web__*"),
                           [[["web__ask", {"times": 1, **_sized(mode, CAP + 1)}]]], tenant, PROJECT,
                           approval_timeout=3)  # a request wrongly recorded expires, rather than hanging the test
        got = await results(runner, result, tenant, PROJECT)
        recorded = await runner.interruptions(tenant, PROJECT, run_id=result.run_id, pending_only=False)
    assert got["t0-0"].is_error and got["t0-0"].content == "ToolExecutionError: input request too large", got["t0-0"]
    assert recorded == [], "nothing of a request over the cap is recorded"
    assert not [e for e in result.events if e.event_type is EventType.INPUT_REQUESTED]


@pytest.mark.parametrize("store", STORES)
@pytest.mark.parametrize("after", [1, 3])
async def test_once_a_request_is_refused_the_calls_later_requests_ask_nobody(store, after, serve, accepted):
    """M37 pre-review D1, D2: on the legacy era a server may go on asking after a request is
    refused too large. Nothing later is recorded or put to a person -- an answer would reach a
    server whose call has already failed -- and the call fails for the first reason, though the
    later requests take it past three."""
    served, tenant = serve("legacy", tools=TOOLS), tenancy()
    small = {"type": "object", "properties": {"name": {"type": "string"}}}
    schemas = [_sized("form", CAP + 1)["schema"]] + [small] * after
    runner = runner_for([web(served)], store)
    async with runner:
        result = await run(runner, agent("web__*"), [[["web__ask", {"times": 1 + after, "schema": schemas}]]],
                           tenant, PROJECT, approval_timeout=3)
        got = await results(runner, result, tenant, PROJECT)
        recorded = await runner.interruptions(tenant, PROJECT, run_id=result.run_id, pending_only=False)
    assert got["t0-0"].content == "ToolExecutionError: input request too large", got["t0-0"]
    assert recorded == [], "a request after the refusal was recorded"
    assert not [e for e in result.events if e.event_type in (EventType.INPUT_REQUESTED, EventType.INPUT_ANSWERED)]


@pytest.mark.parametrize("mode", ["form", "url"])
@pytest.mark.parametrize("kind", ["modern", "legacy"])
async def test_an_input_request_of_exactly_64_kib_is_recorded(mode, kind, serve, accepted):
    served, tenant = serve(kind, tools=TOOLS), tenancy()
    runner = runner_for([web(served)])
    async with runner:
        handle = await runner.start(agent("web__*"),
                                    json.dumps([[["web__ask", {"times": 1, **_sized(mode, CAP)}]]]),
                                    RunConfig(tenant_id=tenant, project_id=PROJECT))
        [view] = await _asked(runner, tenant, handle.run_id)
        await runner.respond(tenant, PROJECT, view.interruption.interruption_id, action="decline", principal=OWNER)
        result = await handle.result()
        got = await results(runner, result, tenant, PROJECT)
    payload = view.interruption.request_payload
    field = "url" if mode == "url" else "requested_schema"
    assert _request_size(payload["message"], field, payload[field]) == CAP, "the request arrived as it was sent"
    assert "too large" not in got["t0-0"].content and not got["t0-0"].is_error, got["t0-0"]


async def _asked(runner, tenant, run_id, timeout=60.0):
    import asyncio
    import time

    deadline = time.monotonic() + timeout
    while True:
        views = [v for v in await runner.interruptions(tenant, PROJECT, run_id=run_id)
                 if v.interruption.kind.value != "tool_approval"]
        if views:
            return views
        assert time.monotonic() < deadline, "no input request was recorded"
        await asyncio.sleep(0.05)


# =================================================================================================
# FR-135, AC-94: the exit signal, ten times on both stores
# =================================================================================================


def failing(checks) -> list[str]:
    return [label for label, passed, _ in checks if passed is not True]


def verdict(checks) -> dict:
    return {label: passed for label, passed, _ in checks}


@pytest.mark.parametrize("store", STORES)
async def test_the_exit_signal_holds_ten_times_with_shuffled_starts_and_random_delays(store):
    example = _example()
    for seed in range(10):
        checks, runs = await example.offline(store, seed=seed, quiet=True, tenant=tenancy(), project=f"{PROJECT}-{seed}")
        assert failing(checks) == [], f"seed {seed}: {checks}"
        assert len(runs) == 1 + len(example.WORKERS), "the plan run and one child a worker"


@pytest.mark.parametrize("removed, check", [
    ("change", "CHANGED"),
    ("shadowing", "NAMES"),
    ("injection", "UNTRUSTED"),
    ("input", "INPUT"),
    ("approval", "APPROVED"),
])
async def test_each_check_fails_without_its_subject(monkeypatch, removed, check):
    """AC-94: removing the change, the shadowing tool, the injection, the input request or the
    approval fails the check that names it, and no other."""
    example = _example()
    monkeypatch.setattr(example, "WAIT_BOUND", 2.0)  # without a change, the reviser's wait ends sooner
    checks, _ = await example.offline("memory", seed=0, quiet=True, tenant=tenancy(), project=PROJECT,
                                      without=frozenset({removed}))
    assert failing(checks) == [getattr(example, check)], checks


async def test_on_postgres_the_change_is_an_mcp_tool_blocked_row_of_the_child_that_met_it():
    """FR-135: the changed tool's McpToolBlocked names it, on the run of the child that met it,
    with the part that changed and the version it changed from."""
    example = _example()
    tenant = tenancy()
    checks, runs = await example.offline("postgres", seed=3, quiet=True, tenant=tenant, project=PROJECT)
    assert failing(checks) == [], checks
    with psycopg.connect(DSN) as conn:
        rows = conn.execute("SELECT run_id, payload FROM run_events WHERE run_id = ANY(%s) AND event_type = %s",
                            (runs[1:], EventType.MCP_TOOL_BLOCKED.value)).fetchall()
        [[first]] = conn.execute("SELECT tool_hash FROM mcp_tool_acceptances WHERE tenant_id = %s AND alias = 'catalog'"
                                 " AND tool_name = 'lookup' ORDER BY accepted_at LIMIT 1", (tenant,)).fetchall()
    changed = [(run_id, p) for run_id, p in rows if p["tool"] == "lookup" and p["changed"] == ["description"]]
    assert changed and all(p["alias"] == "catalog" and p["previous_hash"] == first for _, p in changed), rows


def test_example_31_runs_offline():
    done = subprocess.run([sys.executable, str(REPO / "scripts" / "31_mcp_contention.py"), "--offline"],
                          capture_output=True, text=True, timeout=300, cwd=REPO)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "[FAIL]" not in done.stdout and "[SKIP]" not in done.stdout and "[PASS]" in done.stdout, done.stdout


def test_a_check_whose_subject_never_happened_is_skipped_live_and_fails_offline_and_each_names_its_clause():
    example = _example()
    checks = [("held", True, None), ("absent", None, "nothing ran"), ("broken", False, "a call ran twice")]
    assert example.report(checks, live=True) == (
        ["[PASS] held", "[SKIP] not exercised: absent (nothing ran)", "[FAIL] broken -- a call ran twice"], ["broken"])
    assert example.report(checks, live=False)[1] == ["absent", "broken"]


# =================================================================================================
# M37 round 1 (KNOWLEDGE-1afcf6b2): the live checks hold, held to violations by construction
# =================================================================================================
# Each runs scripted children with strict=False -- the checks as a live run judges them -- and
# either a violation the SDK could commit (patched at runtime here, never on disk) or a shape a
# live model produces. A live check must FAIL over a violation, never SKIP, and must not fail
# where nothing was violated.


async def test_a_call_approved_then_refused_and_made_again_fails_no_check(monkeypatch):
    """Round 1, D1: R4 is approved, then refused as it leaves (the change); the model makes the
    same call again once the change is accepted, as a live model does. Matching each execution
    to the earliest unused approval of its arguments left R4's approval to the second call and
    read the child's order backwards. Nothing was violated, so nothing may fail."""
    example = _example()
    first, _, _ = example.WORKERS["reviser"]
    again = [["catalog__lookup", {"word": "tool", "ticket": "R5"}], ["catalog__lookup", {"word": "agent", "ticket": "R4"}]]
    monkeypatch.setattr(example, "WORKERS", {**example.WORKERS, "reviser": [first, [["wait", {}]], again]})
    checks, _ = await example.offline("memory", seed=0, quiet=True, tenant=tenancy(), project=PROJECT, strict=False)
    assert [label for label, passed, _ in checks if passed is False] == [], checks
    assert verdict(checks)[example.APPROVED] is True, checks


async def test_calls_run_without_approval_fail_approved_live_though_no_turn_made_two(monkeypatch):
    """Round 1, D2: the subject was a turn with two such calls, read from the histories, so an
    SDK that asked no approval was reported SKIP when no turn made two."""
    example = _example()
    one_a_turn = [[["catalog__lookup", {"word": "tool", "ticket": "S1"}]],
                  [["shadow__lookup", {"word": "tool", "ticket": "S2"}]]]
    monkeypatch.setattr(example, "WORKERS", {"shadowed": one_a_turn})
    checks, _ = await example.offline("memory", seed=0, quiet=True, tenant=tenancy(), project=PROJECT,
                                      without=frozenset({"approval"}), strict=False)
    assert verdict(checks)[example.APPROVED] is False, checks
    assert example.APPROVED in example.report(checks, live=True)[1]


async def test_a_call_routed_to_another_server_fails_names_live(monkeypatch):
    """Round 1, D3: the subject was the shadow server running a lookup -- the routing under test.
    Every shadow__lookup is sent to the catalog server here, so the shadow server runs nothing."""
    import agentsdk.mcp as mcp

    example = _example()
    monkeypatch.setattr(example, "WORKERS", {"shadowed": example.WORKERS["shadowed"]})
    connections = []
    init, call = mcp._Connection.__init__, mcp._Connection._call

    def tracked(self, *args, **kwargs):
        init(self, *args, **kwargs)
        connections.append(self)

    async def misrouted(self, name, arguments, *rest, **kwargs):
        if self.server.alias != "shadow":
            return await call(self, name, arguments, *rest, **kwargs)
        other = next(c for c in connections if c.server.alias == "catalog" and c._tenancy == self._tenancy)
        answer = await other.client.call_tool(name, arguments)
        return mcp.ToolOutput(content=answer.content[0].text, source_uri=f"mcp://shadow/{name}")

    monkeypatch.setattr(mcp._Connection, "__init__", tracked)
    monkeypatch.setattr(mcp._Connection, "_call", misrouted)
    checks, _ = await example.offline("memory", seed=0, quiet=True, tenant=tenancy(), project=PROJECT, strict=False)
    assert verdict(checks)[example.NAMES] is False, checks


async def test_results_labelled_trusted_fail_untrusted_live_though_the_read_was_skipped(monkeypatch):
    """Round 1, D4: UNTRUSTED's subject was the injection as a result, an error and a read, all
    three, so a model that skipped the read turned trusted-labelled results into SKIP."""
    import dataclasses

    import agentsdk.mcp as mcp
    from agentsdk.primitives import TrustZone

    example = _example()
    monkeypatch.setattr(example, "WORKERS", {"injected": example.WORKERS["injected"][:1]})  # no read
    monkeypatch.setattr(mcp, "_UNTRUSTED", dataclasses.replace(mcp._UNTRUSTED, trust_zone=TrustZone.TRUSTED_SOURCE))
    checks, _ = await example.offline("memory", seed=0, quiet=True, tenant=tenancy(), project=PROJECT, strict=False)
    assert verdict(checks)[example.UNTRUSTED] is False, checks


async def test_example_30_reports_a_request_the_model_never_made_as_not_exercised_live_only():
    """Round 1 caveat, the owner's choice to fix in round 2: example 30 failed live when the model
    skipped connect_calendar. A request never made tests nothing: None live, a failure offline."""
    import importlib.util

    from agentsdk.model import ModelResponse, StopReason, Usage
    from agentsdk.primitives import Message, Role, ToolCall

    spec = importlib.util.spec_from_file_location("example_30", REPO / "scripts" / "30_mcp_input.py")
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)

    class RegistersOnly:
        async def send(self, request):
            first = sum(1 for m in request.messages if m.role is Role.ASSISTANT) == 0
            if first and request.messages[0].content == example.TASK:
                return ModelResponse(message=Message(role=Role.ASSISTANT, tool_calls=(
                    ToolCall(id="t0", name="intake__register", arguments={}),)), stop_reason=StopReason.TOOL_CALLS,
                    usage=Usage(20, 5, 25))
            return ModelResponse(message=Message(role=Role.ASSISTANT, content="done"), stop_reason=StopReason.END_TURN,
                                 usage=Usage(20, 5, 25))

    url = "the url request was recorded with the full URL and declined, and the server was told"
    live, _ = await example.demonstrate(RegistersOnly(), "m:scripted", live=True)
    offline, _ = await example.demonstrate(RegistersOnly(), "m:scripted")
    assert {label: passed for label, passed in live}[url] is None
    assert {label: passed for label, passed in offline}[url] is False
    assert all(passed for label, passed in live if label != url), live


async def test_an_env_value_in_the_manifest_fails_secrets_live_though_poison_was_never_called(monkeypatch):
    """Round 1, D4: SECRETS's subject was both values reaching their servers, so with poison never
    called an env value written into the manifest was reported SKIP."""
    from agentsdk.mcp import StdioServer

    example = _example()
    monkeypatch.setattr(example, "WORKERS", {k: v for k, v in example.WORKERS.items() if k != "poisoned"})
    record = StdioServer.record

    def leaky(self):
        return {**record(self), "env": dict(self.env or {})}  # the values, which NFR-37 forbids

    monkeypatch.setattr(StdioServer, "record", leaky)
    checks, _ = await example.offline("postgres", seed=0, quiet=True, tenant=tenancy(), project=PROJECT, strict=False)
    assert verdict(checks)[example.SECRETS] is False, checks


# =================================================================================================
# M37 round 2 (KNOWLEDGE-529f7200): each execution told to its one call by the id it returns
# =================================================================================================


async def test_malformed_calls_a_live_model_makes_fail_no_check(monkeypatch):
    """Round 2, D1: calls were matched to executions by the model's arguments, and an error raised
    before a call left counted as a server's. An extra argument (the server drops it), a missing
    one and a wrong type (refused before sending) each failed checks with nothing violated."""
    example = _example()
    malformed = [[["catalog__lookup", {"word": "agent", "ticket": "M1", "extra": "x"}]],
                 [["catalog__lookup", {"word": "agent"}]],
                 [["catalog__lookup", {"word": 7, "ticket": "M3"}]]]
    monkeypatch.setattr(example, "WORKERS", {**example.WORKERS, "malformed": malformed})
    checks, _ = await example.offline("memory", seed=0, quiet=True, tenant=tenancy(), project=PROJECT, strict=False)
    assert [label for label, passed, _ in checks if passed is False] == [], checks


async def test_a_repeat_run_without_approval_fails_approved_though_its_twin_was_approved(monkeypatch):
    """Round 2, D2: R4 is approved, then refused as it leaves; the model makes the same call again,
    and here the SDK runs that repeat without asking anyone. Matching approvals by existence let
    R4's surplus approval pay for it. The repeat's own call was never approved."""
    from agentsdk.executor import ToolExecutor

    example = _example()
    first, _, _ = example.WORKERS["reviser"]
    again = [["catalog__lookup", {"word": "tool", "ticket": "R5"}], ["catalog__lookup", {"word": "agent", "ticket": "R4"}]]
    monkeypatch.setattr(example, "WORKERS", {**example.WORKERS, "reviser": [first, [["wait", {}]], again]})
    approval = ToolExecutor._approval

    async def unasked(self, prepared, hooked):
        call = prepared.tool_call
        if call.id == "t2-1" and call.arguments.get("ticket") == "R4":
            return None  # the repeat runs with no approval asked
        return await approval(self, prepared, hooked)

    monkeypatch.setattr(ToolExecutor, "_approval", unasked)
    checks, _ = await example.offline("memory", seed=0, quiet=True, tenant=tenancy(), project=PROJECT, strict=False)
    assert verdict(checks)[example.APPROVED] is False, checks


async def test_an_env_value_in_the_manifest_fails_secrets_live_though_no_server_was_called(monkeypatch):
    """Round 2 caveat: SECRETS's subject was that a server ran anything, so a configured value
    written into the manifest of a run that called no server was reported SKIP."""
    from agentsdk.mcp import StdioServer

    example = _example()
    monkeypatch.setattr(example, "WORKERS", {"idle": []})
    record = StdioServer.record

    def leaky(self):
        return {**record(self), "env": dict(self.env or {})}

    monkeypatch.setattr(StdioServer, "record", leaky)
    checks, _ = await example.offline("postgres", seed=0, quiet=True, tenant=tenancy(), project=PROJECT, strict=False)
    assert verdict(checks)[example.SECRETS] is False, checks


async def test_arguments_changed_after_approval_fail_approved(monkeypatch):
    """Pre-review W5, held under round 3's link: the server receives arguments other than those
    approved -- the SDK changes them on the way -- and APPROVED fails."""
    import agentsdk.mcp as mcp

    example = _example()
    monkeypatch.setattr(example, "WORKERS", {"shadowed": example.WORKERS["shadowed"]})
    call = mcp._Connection._call

    async def tampered(self, name, arguments, *rest, **kwargs):
        if name == "lookup":
            arguments = {**arguments, "word": "hijacked"}
        return await call(self, name, arguments, *rest, **kwargs)

    monkeypatch.setattr(mcp._Connection, "_call", tampered)
    checks, _ = await example.offline("memory", seed=0, quiet=True, tenant=tenancy(), project=PROJECT, strict=False)
    assert verdict(checks)[example.APPROVED] is False, checks


async def test_a_result_forging_another_execution_or_answering_two_calls_fails_names(monkeypatch):
    """Round 3's link is the id each server returns: a result carrying an id no server journalled,
    or one execution's id in two calls' results, fails NAMES."""
    import agentsdk.mcp as mcp

    example = _example()
    monkeypatch.setattr(example, "WORKERS", {"poisoned": example.WORKERS["poisoned"]})
    call = mcp._Connection._call

    async def forged(self, name, arguments, *rest, **kwargs):
        output = await call(self, name, arguments, *rest, **kwargs)
        return mcp.ToolOutput(content="[exec poison#1-999] " + output.content, source_uri=output.source_uri)

    monkeypatch.setattr(mcp._Connection, "_call", forged)
    checks, _ = await example.offline("memory", seed=0, quiet=True, tenant=tenancy(), project=PROJECT, strict=False)
    assert verdict(checks)[example.NAMES] is False, checks


# =================================================================================================
# M37 round 3 (KNOWLEDGE-02dfeff6): the pre-flight rule, DECISION-a00773b0 -- every live check
# holds under a model that quotes earlier results into later calls' arguments
# =================================================================================================


async def test_a_model_quoting_earlier_results_ids_included_fails_no_check(monkeypatch):
    """Round 3, D1: an execution's id was read anywhere in a result, and three servers echo their
    arguments; a model that quotes an earlier result into a later call -- ordinary behaviour --
    made one execution seem to answer two calls, and NAMES and APPROVED failed with nothing
    violated. Ids now count only where their server puts them."""
    example = _example()
    quoter = [[["poison__summarize", {"text": "the release waits on review", "ticket": "Q1"}]],
              [["poison__summarize", {"text": "$LAST", "ticket": "Q2"}]],
              [["shadow__lookup", {"word": "$LAST", "ticket": "Q3"}]]]
    monkeypatch.setattr(example, "WORKERS", {**example.WORKERS, "quoter": quoter})
    checks, _ = await example.offline("memory", seed=0, quiet=True, tenant=tenancy(), project=PROJECT, strict=False)
    assert [label for label, passed, _ in checks if passed is False] == [], checks


async def test_a_model_typing_an_execution_id_into_an_argument_fails_no_check(monkeypatch):
    """Round 3, D1's first shape: an id no server journalled, typed by the model, echoed back."""
    example = _example()
    typist = [[["shadow__lookup", {"word": "[exec catalog#4242-7] tool", "ticket": "T1"}],
               ["catalog__lookup", {"word": "[exec shadow#1-1] agent", "ticket": "T2"}]]]
    monkeypatch.setattr(example, "WORKERS", {**example.WORKERS, "typist": typist})
    checks, _ = await example.offline("memory", seed=0, quiet=True, tenant=tenancy(), project=PROJECT, strict=False)
    assert [label for label, passed, _ in checks if passed is False] == [], checks


async def test_every_run_under_the_plan_is_returned_from_the_stores_parent_links():
    """Round 3 caveat: a cancelled plan printed 4 of its 6 run ids, so cleanup by printed ids left
    runs behind. On PostgreSQL the ids are read from the store's parent links."""
    example = _example()
    tenant = tenancy()
    checks, runs = await example.offline("postgres", seed=1, quiet=True, tenant=tenant, project=PROJECT)
    assert failing(checks) == [], checks
    with psycopg.connect(DSN) as conn:
        stored = {str(r) for (r,) in conn.execute("SELECT run_id FROM runs WHERE tenant_id = %s", (tenant,))}
    assert set(runs) == stored, "a run under the plan was not returned"
    # From the store alone: what a cancelled plan's orchestrator never recorded is found too.
    from agentsdk import Persistence

    found = await example.descendants(Persistence.postgres(DSN, create_schema=False), runs[0], [])
    assert found == sorted(runs[1:]), "the store's parent links were not read"


async def test_results_without_nfr_35s_taint_fail_untrusted_live(monkeypatch):
    """Round 3 caveat: UNTRUSTED never checked the EXTERNAL_CONTENT taint NFR-35 names."""
    import dataclasses

    import agentsdk.mcp as mcp

    example = _example()
    monkeypatch.setattr(mcp, "_UNTRUSTED", dataclasses.replace(mcp._UNTRUSTED, taint_flags=frozenset()))
    checks, _ = await example.offline("memory", seed=0, quiet=True, tenant=tenancy(), project=PROJECT, strict=False)
    assert verdict(checks)[example.UNTRUSTED] is False, checks


async def test_an_sdk_refusing_the_change_only_as_a_call_leaves_fails_changed_on_postgres(monkeypatch):
    """Round 3 caveat, the owner's choice to witness both catalog guards: with executor step 1's
    guard gone, D1 is still refused -- as it leaves, having reached step 6 -- and the strict check,
    reading its ToolCalled on PostgreSQL, fails. Example 31's step_one witness."""
    import agentsdk.mcp as mcp

    example = _example()

    async def unguarded(self, name):
        return None

    monkeypatch.setattr(mcp.RunTools, "blocked", unguarded)
    checks, _ = await example.offline("postgres", seed=0, quiet=True, tenant=tenancy(), project=PROJECT)
    assert verdict(checks)[example.CHANGED] is False, checks


# =================================================================================================
# FR-50, FR-133 (the owner, DECISION-7888353c): a cancelled run's input request is decided first
# =================================================================================================


@pytest.mark.parametrize("kind", ["modern", "legacy"])
async def test_a_cancelled_runs_input_request_is_decided_before_the_run_reports_cancelled(kind, serve, accepted,
                                                                                         monkeypatch):
    """M36's caveat 2, which failed M37 round 3's regression gate (about 1 run in 10, on M36's own
    code too): the caller's cancellation left the call's work on the MCP loop to finish alone, so
    the run reported cancelled before the request's cancel decision was written, contrary to
    FR-50. Held open here by a cancel write that takes half a second (KNOWLEDGE-14f3ec8b)."""
    import time as clock

    from agentsdk import RunStatus
    from agentsdk.interruptions import InMemoryInterruptionStore

    write = InMemoryInterruptionStore.append_decision

    def slow(self, decision):
        if decision.decision == "cancel":
            clock.sleep(0.5)
        return write(self, decision)

    monkeypatch.setattr(InMemoryInterruptionStore, "append_decision", slow)
    served, tenant = serve(kind, tools=TOOLS), tenancy()
    runner = runner_for([web(served)])
    async with runner:
        handle = await runner.start(agent("web__*"), json.dumps([[["web__ask", {"times": 1}]]]),
                                    RunConfig(tenant_id=tenant, project_id=PROJECT))
        await _asked(runner, tenant, handle.run_id)
        handle.cancel()
        result = await handle.result()
        views = await runner.interruptions(tenant, PROJECT, run_id=handle.run_id, pending_only=False)
    assert result.status is RunStatus.CANCELLED
    decided = [(v.decision.decision, v.decision.source) if v.decision else None for v in views
               if v.interruption.kind.value == "additional_user_input"]
    assert decided == [("cancel", "cancel")], "the run reported cancelled before its request was decided"


# =================================================================================================
# AC-94, NFR-34: no new runtime dependency; mcp only in its extras
# =================================================================================================


def test_the_runtime_dependencies_are_unchanged_and_mcp_is_only_in_its_extras():
    import tomllib

    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert sorted(project["dependencies"]) == ["httpx==0.28.1", "jsonschema==4.26.0", "psycopg[binary]==3.3.5",
                                               "psycopg_pool==3.2.6", "python-dotenv==1.2.3"]
    holding = sorted(extra for extra, pins in project["optional-dependencies"].items()
                     if any(pin.split("==")[0] == "mcp" for pin in pins))
    assert holding == ["examples", "mcp"]
    assert "mcp==2.3.0" in project["optional-dependencies"]["mcp"]
