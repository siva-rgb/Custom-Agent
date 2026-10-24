"""M33 gate: the stdio MCP client (FR-123 to FR-126, AC-90, NFR-34, NFR-35, NFR-37 to NFR-39).

An agent uses the tools of real local MCP servers, one of each era, over stdio: offered as
`<alias>__<name>` to agents whose profile names them, filtered, skipped when a listing is
hostile, every result untrusted. A connection serves one tenancy on one loop, restarts once,
and no server process outlives its Runner.

Windows only: NFR-39 is measured by process id on Windows (the Job Object). Store calls from
async tests go to a worker thread (FR-81); the persisted half removes every row it writes.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psycopg
import pytest

from agentsdk import AgentSpec, Persistence, RunConfig, RunStatus, Runner
from agentsdk.config import normalise_database_url
from agentsdk.events import EventType
from agentsdk.mcp import StdioServer
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.postgres import RunScope
from agentsdk.primitives import InstructionAuthority, Message, Origin, Role, TaintFlag, ToolCall, TrustZone
from agentsdk.errors import ToolExecutionError
from agentsdk.tools import Tool, ToolRegistry, ToolSpec

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="NFR-39 is measured on Windows (D11)")

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests" / "fixtures" / "mcp_servers.py"
DSN = normalise_database_url(os.environ.get("DATABASE_URL"))
TENANT, PROJECT = "SYN-m33", "p-m33"
STORES = ["memory", "postgres"]
SECRET = "fixture-secret-" + uuid.uuid4().hex
# The real interpreter, not the venv's python.exe: that is a launcher, which starts the real
# one before the mcp library's Job Object holds it, so a server's children could escape
# (declared, NFR-39). The fixture adds the venv's packages itself, from FIXTURE_SITE.
PYTHON = sys._base_executable
PACKAGES = {"FIXTURE_SITE": [p for p in __import__("site").getsitepackages() if p.endswith("site-packages")][0]}


def _every_listed_version_accepted(store, tenant_id, project_id, alias, versions, pending, accept):
    return set(pending.values()), {}


@pytest.fixture(autouse=True)
def every_tool_accepted(monkeypatch):
    """M34 (FR-127): no MCP tool is usable until its version is accepted for the tenancy. These
    tests are of the client M33 built, not of the catalog, so here every version a connection
    lists reads as accepted, through the one function every catalog read goes through.
    tests/test_mcp_catalog.py tests the catalog itself."""
    import agentsdk.mcp as mcp

    monkeypatch.setattr(mcp, "_read_catalog", _every_listed_version_accepted)


@pytest.fixture(autouse=True)
def remove_what_the_test_wrote():
    yield
    if DSN:
        with psycopg.connect(DSN, autocommit=True) as conn:
            ids = [r[0] for r in conn.execute("SELECT run_id FROM runs WHERE tenant_id LIKE %s", (TENANT + "%",))]
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


def server(kind, alias=None, pids=None, extra=(), **fields):
    args = [str(FIXTURE), kind, *(["--pids", str(pids)] if pids else []), *extra]
    fields.setdefault("approval", "never")
    # Generous: a cold start of the modern fixture on a loaded host once passed 30 s. The tests
    # of the bound itself set their own.
    fields.setdefault("connect_timeout", 120)
    fields["env"] = {**PACKAGES, **fields.get("env", {})}
    return StdioServer(alias=alias or kind, command=PYTHON, args=tuple(args), **fields)


def respond(content="", calls=()):
    return ModelResponse(message=Message(role=Role.ASSISTANT, content=content, tool_calls=tuple(calls)),
                         stop_reason=StopReason.TOOL_CALLS if calls else StopReason.END_TURN, usage=Usage(5, 1, 6))


class Turns:
    """The model: its task is a JSON list of turns, each a list of [tool, arguments]."""

    def __init__(self):
        self.requests = []

    async def send(self, request):
        self.requests.append(request)
        turns = json.loads(request.messages[0].content.split("\n")[0])
        turn = sum(1 for m in request.messages if m.role is Role.ASSISTANT)
        if turn < len(turns):
            return respond(calls=[ToolCall(id=f"t{turn}-{i}", name=name, arguments=args)
                                 for i, (name, args) in enumerate(turns[turn])])
        return respond("done")


def agent(*profile):
    return AgentSpec(id="agent", instructions="use the tools", preferred_model="m:x", tool_profile=tuple(profile))


def runner_for(servers, store="memory", model=None, tools=()):
    return Runner({"m": model or Turns()}, tools=list(tools), mcp_servers=list(servers),
                  persistence=Persistence.postgres(DSN, create_schema=False) if store == "postgres" else None)


async def run(runner, spec, turns, tenant=TENANT, project=PROJECT, **fields):
    return await runner.run(spec, json.dumps(turns), RunConfig(tenant_id=tenant, project_id=project, **fields))


async def results(runner, result, tenant=TENANT, project=PROJECT):
    sessions = runner._sessions_for(RunScope(run_id=result.run_id, tenant_id=tenant, project_id=project))
    history = await asyncio.to_thread(sessions.history, result.run_id)
    return {r.tool_call_id: r for m in history for r in m.tool_results}


def events(result, kind):
    return [e for e in result.events if e.event_type is kind]


def alive(pid: int) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x00100000 | 0x1000, False, pid)  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT: still running
    finally:
        kernel32.CloseHandle(handle)


def pids_in(path: Path) -> list[int]:
    return [int(line) for line in path.read_text(encoding="utf-8").split()] if path.exists() else []


def gone(pids, timeout=10.0) -> bool:
    deadline = time.monotonic() + timeout
    while any(alive(p) for p in pids):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.1)
    return True


UNTRUSTED = (Origin.MCP_RESOURCE, TrustZone.UNTRUSTED, InstructionAuthority.DATA_ONLY)


def untrusted(result):
    p = result.provenance
    return ((p.origin, p.trust_zone, p.instruction_authority) == UNTRUSTED
            and TaintFlag.EXTERNAL_CONTENT in p.taint_flags)


# =================================================================================================
# FR-123: configuration
# =================================================================================================


@pytest.mark.parametrize("fields, reason", [
    ({"alias": "Bad"}, "alias"),
    ({"alias": "has__two"}, "alias"),
    ({"alias": "a" * 25}, "alias"),
    ({"env": {"X": "nul\x00"}}, "env"),
    ({"approval": "sometimes"}, "approval"),
    ({"approval_overrides": {"echo": "sometimes"}}, "approval"),
    ({"call_timeout": 0}, "call_timeout"),
    ({"connect_timeout": float("inf")}, "connect_timeout"),
    ({"protocol": ""}, "protocol"),
    # M33 review caveat (M34 pre-flight): a qualified name matched nothing, so in deny it failed open.
    ({"deny": ("ok__wipe",)}, "without the alias"),
    ({"allow": ("ok__echo",)}, "without the alias"),
    ({"approval_overrides": {"ok__echo": "never"}}, "without the alias"),
])
def test_an_invalid_server_config_is_refused_when_built(fields, reason):
    with pytest.raises(ValueError, match=reason):
        StdioServer(**{"alias": "ok", "command": sys.executable, **fields})


def test_a_duplicate_alias_is_refused():
    with pytest.raises(ValueError, match="alias"):
        runner_for([server("modern", alias="same"), server("legacy", alias="same")])


def test_the_config_records_names_never_values():
    record = server("modern", env={"FIXTURE_SECRET": SECRET}, allow=("echo",), deny=("die",)).record()
    assert record["kind"] == "stdio" and record["env"] == ["FIXTURE_SECRET", "FIXTURE_SITE"]
    assert SECRET not in json.dumps(record)
    assert record["allow"] == ["echo"] and record["deny"] == ["die"] and record["command"] == PYTHON


# =================================================================================================
# FR-125: what is offered, to whom
# =================================================================================================


@pytest.mark.parametrize("store", STORES)
async def test_tools_are_offered_qualified_filtered_and_only_to_agents_that_name_them(store):
    model = Turns()
    runner = runner_for([server("modern", allow=("echo", "fail", "die"), deny=("die",))], store, model)
    async with runner:
        await run(runner, agent("modern__*"), [])
        await run(runner, agent("modern__echo"), [])
        await run(runner, agent("something_else"), [])
    offered = [sorted(t["function"]["name"] for t in r.tools) for r in model.requests]
    assert offered == [["modern__echo", "modern__fail"], ["modern__echo"], []]


async def test_a_hostile_listing_is_skipped_and_recorded():
    clash = Tool(spec=ToolSpec(name="legacy__clash", description="mine", input_schema={"type": "object"}),
                 fn=lambda: "the application's own")
    model = Turns()
    runner = runner_for([server("legacy")], model=model, tools=[clash])
    async with runner:
        result = await run(runner, agent("legacy__*", "legacy__clash"), [[["legacy__clash", {}]]])
    skipped = {e.payload["tool"]: e.payload["reason"] for e in events(result, EventType.MCP_TOOL_SKIPPED)}
    assert set(skipped) == {"dotted.name", "twice", "bad_schema", "clash"}, skipped
    offered = {t["function"]["name"] for t in model.requests[0].tools}
    assert "legacy__echo" in offered and not offered & {"legacy__dotted.name", "legacy__twice", "legacy__bad_schema"}
    assert (await results(runner, result))["t0-0"].content == "the application's own"


# =================================================================================================
# FR-125: calls and their results
# =================================================================================================


@pytest.mark.parametrize("store", STORES)
@pytest.mark.parametrize("kind", ["modern", "legacy"])
async def test_each_kind_of_result_reaches_the_model_untrusted(store, kind):
    runner = runner_for([server(kind, call_timeout=1)], store)
    async with runner:
        result = await run(runner, agent(f"{kind}__*"), [
            [["%s__echo" % kind, {"text": "hello"}]],
            [["%s__fail" % kind, {}]],
            [["%s__slow" % kind, {"seconds": 5}]],
        ])
    got = await results(runner, result)
    mcp_events = [(e.event_type.value, e.payload) for e in result.events if e.event_type.value.startswith("Mcp")]
    assert got["t0-0"].content == "hello" and not got["t0-0"].is_error, mcp_events
    assert got["t1-0"].is_error and ("failed on purpose" in got["t1-0"].content
                                     or f"Error executing tool fail" in got["t1-0"].content), got["t1-0"].content
    assert got["t2-0"].is_error and got["t2-0"].content.startswith("ToolTimeout")
    for call_id in ("t0-0", "t1-0", "t2-0"):
        assert untrusted(got[call_id]), got[call_id].provenance
    assert got["t0-0"].provenance.source_uri_or_hash == f"mcp://{kind}/echo"
    connected = events(result, EventType.MCP_SERVER_CONNECTED)
    assert [e.payload["era"] for e in connected] == ["2026-07-28" if kind == "modern" else "2025-11-25"]


async def test_a_json_rpc_error_an_output_mismatch_and_an_image_give_the_stated_results():
    runner = runner_for([server("legacy")])
    async with runner:
        result = await run(runner, agent("legacy__*"), [
            [["legacy__rpc_error", {}]], [["legacy__shaped", {}]], [["legacy__counted", {}]], [["legacy__picture", {}]]])
    got = await results(runner, result)
    assert got["t0-0"].is_error and "-32602" in got["t0-0"].content and "refuses these arguments" in got["t0-0"].content
    # Also pins the mapping of the mcp library's own output check, which keys on its message
    # ("structured content"): a library that words it otherwise fails here (M33 round 1 caveat).
    assert got["t1-0"].is_error and "output does not match its schema" in got["t1-0"].content
    assert not got["t2-0"].is_error and got["t2-0"].content == "n is 1"
    assert got["t3-0"].content == "a picture:\n[image content omitted: image/png, 8 bytes]"


async def test_a_call_that_needs_approval_by_default_is_asked():
    runner = runner_for([server("modern", approval="require")])
    async with runner:
        handle = await runner.start(agent("modern__echo"), json.dumps([[["modern__echo", {"text": "hi"}]]]),
                                    RunConfig(tenant_id=TENANT, project_id=PROJECT, approval_timeout=0.5))
        result = await handle.result()
    [asked] = events(result, EventType.APPROVAL_REQUESTED)
    assert asked.payload["tool_name"] == "modern__echo"
    assert (await results(runner, result))["t0-0"].content == "approval expired"


async def test_cancelling_a_run_cancels_its_call():
    runner = runner_for([server("modern")])
    async with runner:
        handle = await runner.start(agent("modern__slow"), json.dumps([[["modern__slow", {"seconds": 30}]]]),
                                    RunConfig(tenant_id=TENANT, project_id=PROJECT))
        await asyncio.sleep(3)
        started = time.monotonic()
        handle.cancel("stop")
        result = await handle.result()
    assert result.status is RunStatus.CANCELLED and time.monotonic() - started < 5


# =================================================================================================
# FR-124: connections and their lifecycle
# =================================================================================================


async def test_a_connection_serves_one_tenancy_and_is_shared_within_it(tmp_path):
    pids = tmp_path / "pids"
    runner = runner_for([server("modern", pids=pids)])
    async with runner:
        await run(runner, agent("modern__echo"), [[["modern__echo", {"text": "a"}]]])
        await run(runner, agent("modern__echo"), [[["modern__echo", {"text": "b"}]]])
        assert len(pids_in(pids)) == 1, "two runs of one tenancy share one connection"
        await run(runner, agent("modern__echo"), [[["modern__echo", {"text": "c"}]]], tenant=TENANT + "-other")
        assert len(pids_in(pids)) == 2, "another tenancy never uses it"
    assert gone(pids_in(pids))


async def test_a_server_that_exits_fails_its_call_restarts_once_then_fails_its_tools(tmp_path):
    pids = tmp_path / "pids"
    runner = runner_for([server("legacy", pids=pids)])
    async with runner:
        result = await run(runner, agent("legacy__*"), [
            [["legacy__die", {}]], [["legacy__echo", {"text": "back"}]], [["legacy__die", {}]],
            [["legacy__echo", {"text": "again"}]]])
    got = await results(runner, result)
    assert got["t0-0"].is_error and "mcp server legacy exited" in got["t0-0"].content
    assert got["t1-0"].content == "back", "restarted once"
    assert got["t2-0"].is_error and got["t3-0"].is_error, "a second exit fails its tools"
    assert len(pids_in(pids)) == 2


async def test_an_unreachable_server_leaves_its_tools_absent_and_the_run_runs():
    model = Turns()
    missing = StdioServer(alias="missing", command=str(REPO / "no-such-program.exe"), approval="never")
    runner = runner_for([missing], model=model)
    async with runner:
        result = await run(runner, agent("missing__*"), [])
    assert result.status is RunStatus.COMPLETED and not model.requests[0].tools
    [unavailable] = events(result, EventType.MCP_SERVER_UNAVAILABLE)
    assert unavailable.payload["alias"] == "missing"


async def test_a_listing_the_library_refuses_leaves_the_server_unavailable():
    """The mcp library validates a whole listing: one input schema whose type is not object
    refuses it all. The server's tools are then absent, and the cause is named."""
    model = Turns()
    runner = runner_for([server("legacy", extra=("--bad-listing",))], model=model)
    async with runner:
        result = await run(runner, agent("legacy__*"), [])
    assert result.status is RunStatus.COMPLETED and not model.requests[0].tools
    assert [e.payload["error"] for e in events(result, EventType.MCP_SERVER_UNAVAILABLE)][0] == "ValidationError"


@pytest.mark.parametrize("flag, connect_timeout", [
    ("--banner", 2), ("--silent", 2),
    # A long connect_timeout, so only the page cap can end an endless listing.
    ("--endless-pages", 120),
])
async def test_a_server_that_never_answers_is_unavailable_within_its_bound(flag, connect_timeout, tmp_path):
    """M33 round 1: starting and listing had no bound, so a server that started and never spoke
    MCP hung the run before it was recorded. Every wait on a server is bounded now, and one
    that failed is not tried again on the next turn (the run takes two turns here)."""
    pids, model = tmp_path / "pids", Turns()
    runner = runner_for([server("legacy", pids=pids, extra=(flag,), connect_timeout=connect_timeout)], model=model)

    async def go():
        async with runner:
            return await run(runner, agent("legacy__*"), [[["legacy__echo", {"text": "x"}]]])

    started = time.monotonic()
    result = await asyncio.wait_for(go(), 60)  # a missing bound fails here, not by hanging
    assert time.monotonic() - started < 30, time.monotonic() - started
    assert result.status is RunStatus.COMPLETED and not any(r.tools for r in model.requests)
    [unavailable] = events(result, EventType.MCP_SERVER_UNAVAILABLE)
    assert unavailable.payload["alias"] == "legacy"
    assert gone(pids_in(pids))


@pytest.mark.parametrize("size", [70_000, 2_000_000])
async def test_every_call_after_a_wedged_write_returns_within_its_bound(size, tmp_path):
    """M33 round 2: the library arms its read timeout only after the request is written, so a
    request that fills the pipe of a server no longer reading its input blocked with no bound,
    and every call after it too. The SDK owns each call's deadline: the wedged call fails
    ToolTimeout, its connection -- which may hold half a message -- is ended, and the next call
    starts the server again (once)."""
    pids = tmp_path / "pids"
    runner = runner_for([server("legacy", pids=pids, call_timeout=2)])

    async def go():
        async with runner:
            return await run(runner, agent("legacy__*"), [
                [["legacy__freeze", {}]], [["legacy__echo", {"text": "x" * size}]],
                [["legacy__echo", {"text": "after"}]], [["legacy__echo", {"text": "and again"}]]])

    started = time.monotonic()
    result = await asyncio.wait_for(go(), 90)  # a missing bound fails here, not by hanging
    got = await results(runner, result)
    assert got["t0-0"].content == "frozen"
    assert got["t1-0"].is_error and got["t1-0"].content.startswith("ToolTimeout"), got["t1-0"].content[:200]
    assert got["t2-0"].content == "after" and got["t3-0"].content == "and again"
    assert time.monotonic() - started < 45, time.monotonic() - started
    assert len(pids_in(pids)) == 2 and gone(pids_in(pids)), "the wedged server was ended, and one restarted"


async def test_a_run_in_flight_across_aclose_starts_no_server(tmp_path):
    """M33 round 2: aclose forgot this loop's connections without closing the Runner to them, so
    a run still going started a new server on its next turn."""
    pids = tmp_path / "pids"
    runner = runner_for([server("modern", pids=pids)])
    handle = await runner.start(agent("modern__*"), json.dumps([[["modern__slow", {"seconds": 3}]],
                                                                [["modern__echo", {"text": "late"}]]]),
                                RunConfig(tenant_id=TENANT, project_id=PROJECT))
    deadline = time.monotonic() + 60
    while not pids_in(pids):
        assert time.monotonic() < deadline, "the server never started"
        await asyncio.sleep(0.05)
    await runner.aclose()
    result = await asyncio.wait_for(handle.result(), 60)
    got = await results(runner, result)
    assert result.status is RunStatus.COMPLETED
    assert got["t1-0"].is_error, "the closed Runner offers the server's tools no more"
    assert len(pids_in(pids)) == 1 and gone(pids_in(pids)), pids_in(pids)


async def test_after_aclose_no_holder_of_a_connection_starts_a_server(tmp_path):
    """M33 round 3: aclose forgot its connections without closing them, so anything still
    holding one -- a tool from an earlier turn, the connection itself -- started a server
    again, which no later aclose could reach. The gate every start passes through is closed now,
    so the guarantee holds for every holder, not only the next turn's refresh."""
    pids = tmp_path / "pids"
    runner = runner_for([server("modern", pids=pids)])
    view = runner._mcp.view(ToolRegistry(), ("modern__*",), TENANT, PROJECT)
    await view.prepare()
    held_tool = view.get("modern__echo")
    assert len(pids_in(pids)) == 1
    await runner.aclose()
    assert await runner._mcp.ensure("modern", TENANT, PROJECT) == ([], {})
    with pytest.raises(ToolExecutionError, match="closed"):
        await held_tool.fn(text="late")
    await view.refresh()  # the first turn reuses what prepare found
    await view.refresh()  # the next asks again: a closed Runner offers nothing, starts nothing
    assert "modern__echo" not in view
    await runner.aclose()  # twice is harmless
    assert len(pids_in(pids)) == 1 and gone(pids_in(pids)), pids_in(pids)


async def test_aclose_during_a_connect_ends_the_server_it_was_starting(tmp_path):
    pids = tmp_path / "pids"
    runner = runner_for([server("legacy", pids=pids, extra=("--slow-start", "2"))])
    view = runner._mcp.view(ToolRegistry(), ("legacy__*",), TENANT, PROJECT)
    connecting = asyncio.create_task(view.prepare())
    deadline = time.monotonic() + 30
    while not pids_in(pids):
        assert time.monotonic() < deadline, "the server never started"
        await asyncio.sleep(0.05)
    await asyncio.wait_for(runner.aclose(), 60)
    await asyncio.wait_for(connecting, 60)
    assert await runner._mcp.ensure("legacy", TENANT, PROJECT) == ([], {})
    assert len(pids_in(pids)) == 1 and gone(pids_in(pids)), pids_in(pids)


async def test_a_stale_session_never_unsets_the_one_after_it(tmp_path):
    """M33 round 3 caveat: a session's task, ending, unset the client whatever session was then
    current, so a call that restarted the server during a discard was left with a live but
    unusable server and its one restart spent."""
    pids = tmp_path / "pids"
    runner = runner_for([server("legacy", pids=pids)])

    async def on_the_mcp_loop():  # a connection lives on its Runner's MCP loop
        connection = runner._mcp._engine.connection("legacy", TENANT, PROJECT)
        await connection.ensure()
        stale = connection._task
        connection.client = None  # as a discard leaves it, before its task has ended
        await connection.ensure()  # the restart
        assert connection._task is not stale
        stale.cancel()
        await asyncio.wait([stale], timeout=15)
        assert connection.client is not None, "the stale session unset the new one"

    await runner._mcp._engine.submit(on_the_mcp_loop(), timeout=120)
    _, tools = await runner._mcp.ensure("legacy", TENANT, PROJECT)
    assert (await tools["echo"].fn(text="still here")).content == "still here"
    await runner.aclose()
    assert len(pids_in(pids)) == 2 and gone(pids_in(pids))


async def test_an_honest_slow_tool_keeps_its_connection(tmp_path):
    """The library's read timeout decides a slow tool, well inside the SDK's own deadline, so
    its connection is kept: no restart, and the next call is served by the same process."""
    pids = tmp_path / "pids"
    # The modern fixture serves calls concurrently; the legacy one would queue the echo behind.
    runner = runner_for([server("modern", pids=pids, call_timeout=1)])
    async with runner:
        result = await run(runner, agent("modern__*"), [[["modern__slow", {"seconds": 2.5}]],
                                                        [["modern__echo", {"text": "same"}]]])
    got = await results(runner, result)
    # The executor words every tool timeout itself; one process is what shows the connection kept.
    assert got["t0-0"].content.startswith("ToolTimeout")
    assert got["t1-0"].content == "same" and len(pids_in(pids)) == 1


def test_aclose_closes_the_connections_of_every_loop(tmp_path):
    """FR-124: aclose closes every connection. One opened by a run on another thread's loop is
    closed on that loop, and nothing reopens there afterwards."""
    import threading

    pids = tmp_path / "pids"
    runner = runner_for([server("modern", pids=pids)])
    other = asyncio.new_event_loop()
    thread = threading.Thread(target=other.run_forever, daemon=True)
    thread.start()
    try:
        asyncio.run_coroutine_threadsafe(
            run(runner, agent("modern__echo"), [[["modern__echo", {"text": "there"}]]]), other).result(120)
        assert len(pids_in(pids)) == 1 and not gone(pids_in(pids), timeout=0.5)
        asyncio.run(asyncio.wait_for(runner.aclose(), 60))
        assert gone(pids_in(pids)), "the other loop's server outlived aclose"
        late = asyncio.run_coroutine_threadsafe(
            run(runner, agent("modern__echo"), [[["modern__echo", {"text": "late"}]]]), other).result(120)
        assert late.status is RunStatus.COMPLETED and len(pids_in(pids)) == 1
    finally:
        other.call_soon_threadsafe(other.stop)
        thread.join(10)
        other.close()


def test_a_server_whose_loop_is_idle_still_ends_with_aclose(tmp_path):
    """Pre-review D1: a loop that ran a run and then sat idle -- not running, not closed --
    kept its server through aclose from another loop. Servers live on the Runner's own MCP
    loop now, whatever loop the run was on."""
    pids = tmp_path / "pids"
    runner = runner_for([server("modern", pids=pids)])
    idle = asyncio.new_event_loop()
    try:
        idle.run_until_complete(run(runner, agent("modern__echo"), [[["modern__echo", {"text": "x"}]]]))
        assert len(pids_in(pids)) == 1
        asyncio.run(asyncio.wait_for(runner.aclose(), 60))
        assert gone(pids_in(pids)), "the idle loop's server outlived aclose"
    finally:
        idle.close()


def test_a_tool_kept_from_one_loop_and_called_on_another_starts_nothing_aclose_misses(tmp_path):
    """Pre-review D2: a tool made on a finished loop, called on a new one, started a server keyed
    to the dead loop, which aclose skipped."""
    pids = tmp_path / "pids"
    runner = runner_for([server("modern", pids=pids)])

    async def tool_on_loop_a():
        view = runner._mcp.view(ToolRegistry(), ("modern__*",), TENANT, PROJECT)
        await view.prepare()
        return view.get("modern__echo")

    held = asyncio.run(tool_on_loop_a())
    assert (asyncio.run(held.fn(text="from B"))).content == "from B"
    assert len(pids_in(pids)) == 1, "one connection, wherever its caller's loop is"
    asyncio.run(asyncio.wait_for(runner.aclose(), 60))
    assert gone(pids_in(pids))


async def test_every_aclose_returns_only_once_the_servers_have_ended(tmp_path):
    """Pre-review D3: a second aclose returned at once while the first was still ending them."""
    pids = tmp_path / "pids"
    runner = runner_for([server("modern", pids=pids)])
    await run(runner, agent("modern__echo"), [[["modern__echo", {"text": "x"}]]])
    await asyncio.wait_for(asyncio.gather(runner.aclose(), runner.aclose()), 60)
    assert gone(pids_in(pids), timeout=1), "an aclose returned before the servers ended"
    await asyncio.wait_for(runner.aclose(), 10)


def test_connections_opened_while_aclose_runs_never_outlive_it(tmp_path):
    """Pre-review D4: a connection made on another thread while aclose ran survived it in 8 of 40
    trials. One lock decides "closed" against every new submission now."""
    import threading

    pids = tmp_path / "pids"
    runner = runner_for([server("legacy", pids=pids)])
    asyncio.run(run(runner, agent("legacy__echo"), [[["legacy__echo", {"text": "x"}]]]))
    started = threading.Event()

    def opener(index):
        started.set()
        for attempt in range(5):
            try:
                asyncio.run(runner._mcp.ensure("legacy", f"{TENANT}-race-{index}-{attempt}", PROJECT))
            except Exception:  # noqa: BLE001 - refused once closed is what the test wants
                pass

    threads = [threading.Thread(target=opener, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    started.wait(10)
    asyncio.run(asyncio.wait_for(runner.aclose(), 120))
    for thread in threads:
        thread.join(120)
    assert gone(pids_in(pids), timeout=15), [p for p in pids_in(pids) if alive(p)]


def test_a_schema_the_validator_cannot_even_read_is_a_skip_not_a_crash():
    """Pre-review risk 1: a `$schema` that is no string makes check_schema raise TypeError, not
    SchemaError, which escaped the listing and would have crashed the run."""
    from agentsdk.mcp import _schema_problem

    assert _schema_problem({"type": "object", "$schema": []})
    assert _schema_problem({"type": "object", "properties": {"x": {"type": "string"}}}) is None


def test_a_cancelled_aclose_cancels_only_its_own_wait(tmp_path):
    """Pre-review 2, defect 1: every aclose awaited the one close future, so a caller's deadline
    cancelled the close itself; the engine loop stopped anyway, servers outlived it, and a call
    in flight from another loop waited forever. A cancelled aclose now stops waiting, nothing
    more, and the next aclose finishes the close."""
    import threading

    pids = tmp_path / "pids"
    runner = runner_for([server("legacy", pids=pids, extra=("--deaf",))])
    asyncio.run(run(runner, agent("legacy__spawn"), [[["legacy__spawn", {}]]]))
    outcome = {}

    def caller():
        try:
            outcome["result"] = asyncio.run(run(runner, agent("legacy__slow"), [[["legacy__slow", {"seconds": 20}]]]))
        except BaseException as exc:  # noqa: BLE001 - recorded, and judged below
            outcome["error"] = exc

    thread = threading.Thread(target=caller, daemon=True)
    thread.start()
    time.sleep(2)  # the slow call is in flight

    async def cancelled_then_complete():
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(runner.aclose(), 0.05)
        await asyncio.wait_for(runner.aclose(), 60)
        await asyncio.wait_for(runner.aclose(), 10)  # and again, done already

    asyncio.run(cancelled_then_complete())
    thread.join(60)
    assert not thread.is_alive(), "a call in flight across the close never returned"
    assert "error" not in outcome, outcome
    assert gone(pids_in(pids)), [p for p in pids_in(pids) if alive(p)]
    deadline = time.monotonic() + 10
    while any(t.name == "agentsdk-mcp" for t in threading.enumerate()):
        assert time.monotonic() < deadline, "the MCP loop's thread outlived the close"
        time.sleep(0.05)


def test_a_collection_inside_a_submission_does_not_deadlock_the_finalizer(tmp_path):
    """Pre-review 2, defect 2: the finalizer of a collected Runner takes the engine's lock, which
    a submission on the same thread may hold when the collection happens. Run in a process of
    its own, since it patches asyncio to place the collection there."""
    script = (
        "import asyncio, gc, sys, threading\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from tests.test_mcp_client import runner_for, server, pids_in, gone\n"
        "import agentsdk.mcp, tests.test_mcp_client as fixture\n"
        "agentsdk.mcp._read_catalog = fixture._every_listed_version_accepted  # as the autouse fixture\n"
        "from agentsdk.tools import ToolRegistry\n"
        "import pathlib\n"
        "pids = pathlib.Path(sys.argv[2])\n"
        "async def main():\n"
        "    runner = runner_for([server('legacy', pids=pids)])\n"
        "    view = runner._mcp.view(ToolRegistry(), ('legacy__*',), 't', 'p')\n"
        "    await view.prepare()\n"
        "    echo = view.get('legacy__echo')\n"
        "    await echo.fn(text='hi')\n"
        "    runner.cycle = runner\n"
        "    del runner, view\n"
        "    gc.disable()\n"
        "    real = asyncio.run_coroutine_threadsafe\n"
        "    def with_gc(*a, **k):\n"
        "        asyncio.run_coroutine_threadsafe = real\n"
        "        gc.collect()\n"
        "        return real(*a, **k)\n"
        "    asyncio.run_coroutine_threadsafe = with_gc\n"
        "    done = threading.Event()\n"
        "    def caller():\n"
        "        try:\n"
        "            asyncio.run(echo.fn(text='again'))\n"
        "        except BaseException:\n"
        "            pass\n"
        "        done.set()\n"
        "    threading.Thread(target=caller, daemon=True).start()\n"
        "    print('finished', done.wait(30))\n"
        "    print('gone', gone(pids_in(pids), timeout=15))\n"
        "asyncio.run(main())\n"
    )
    pids = tmp_path / "pids"
    done = subprocess.run([sys.executable, "-c", script, str(REPO), str(pids)], cwd=REPO, capture_output=True,
                          text=True, timeout=120)
    assert "finished True" in done.stdout and "gone True" in done.stdout, done.stdout + done.stderr[-2000:]


_REGEX_SCENARIO = """
import asyncio, json, site, sys, time
site.addsitedir(sys.argv[1])
sys.path.insert(0, sys.argv[2])
from tests.test_mcp_client import runner_for, server, run, agent, results

async def main():
    runner = runner_for([server("legacy")])
    beats, stop = [], asyncio.Event()

    async def heartbeat():
        while not stop.is_set():
            beats.append(time.monotonic())
            await asyncio.sleep(0.05)

    async with runner:
        beating = asyncio.create_task(heartbeat())
        started = time.monotonic()
        result = await run(runner, agent("legacy__*"), [[json.loads(sys.argv[3])]])
        elapsed = time.monotonic() - started
        stop.set()
        await beating
    got = await results(runner, result)
    gap = max(b - a for a, b in zip(beats, beats[1:]))
    print(json.dumps({"error": got["t0-0"].is_error, "elapsed": elapsed, "gap": gap}))

asyncio.run(main())
"""


@pytest.mark.parametrize("call", [["legacy__redos_in", {"s": "a" * 40 + "!"}], ["legacy__redos_out", {}],
                                  ["legacy__deep_in", {"v": 1}], ["legacy__deep_out", {}],
                                  # Robustness pre-review: a null result, which the library validated here.
                                  ["legacy__null_out", {}]])
def test_a_servers_schema_never_runs_in_this_process(call):
    """Pre-review 3, D1: a server's schema `pattern` ran in this process, and Python's regular
    expressions hold the GIL, so a catastrophic one froze every thread past every bound. A schema
    carrying one is validated in a short-lived process of its own, ended at its deadline; the
    call fails, and the process went on meanwhile. Run in a process of its own (the real
    interpreter, so a timeout ends it), so that a freeze fails this test instead of freezing it."""
    try:
        done = subprocess.run([PYTHON, "-c", _REGEX_SCENARIO, PACKAGES["FIXTURE_SITE"], str(REPO), json.dumps(call)],
                              cwd=REPO, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        pytest.fail("a server's regular expression froze the process past 120 s")
    assert done.returncode == 0, done.stderr[-2000:]
    verdict = json.loads(done.stdout.strip().splitlines()[-1])
    assert verdict["error"] and verdict["elapsed"] < 60, verdict
    assert verdict["gap"] < 2, f"the process froze for {verdict['gap']:.1f} s"


async def test_a_pattern_that_holds_still_validates_and_passes():
    runner = runner_for([server("legacy")])
    async with runner:
        result = await run(runner, agent("legacy__redos_in"), [[["legacy__redos_in", {"s": "aaaa"}]],
                                                                [["legacy__redos_in", {"s": "aab"}]]])
    got = await results(runner, result)
    assert got["t0-0"].content == "accepted aaaa"
    assert got["t1-0"].is_error and got["t1-0"].content.startswith("ToolValidationError"), got["t1-0"].content


async def test_the_wildcard_names_mcp_server_tools_only():
    """Pre-review 3, D2: `ops__*` let a registered `ops__wipe` run, even on a Runner with no MCP
    server, where before M33 the call was denied (NFR-38). The wildcard is the MCP view's now,
    and names only a configured server's tools."""
    wiped = []
    wipe = Tool(spec=ToolSpec(name="ops__wipe", description="wipe", input_schema={"type": "object"}),
                fn=lambda: wiped.append(1) or "wiped")
    for servers in ([], [server("legacy")]):
        runner = runner_for(servers, tools=[wipe])
        async with runner:
            result = await run(runner, agent("ops__*", "legacy__*"), [[["ops__wipe", {}]], [["legacy__echo", {"text": "x"}]]])
        got = await results(runner, result)
        assert got["t0-0"].content.startswith("ToolPermissionDenied"), got["t0-0"].content
        if servers:
            assert got["t1-0"].content == "x"
    assert wiped == []


async def test_a_restart_a_call_makes_is_recorded_on_the_run():
    """Pre-review 3, D3: a restart a call made recorded no events, so the record showed one
    connection where the server started twice. They reach the run at its next turn."""
    runner = runner_for([server("legacy")])
    async with runner:
        result = await run(runner, agent("legacy__*"), [[["legacy__die", {}], ["legacy__echo", {"text": "back"}]],
                                                        [["legacy__echo", {"text": "again"}]]])
    got = await results(runner, result)
    assert got["t0-1"].content == "back" and got["t1-0"].content == "again"
    assert len(events(result, EventType.MCP_SERVER_CONNECTED)) == 2


def test_a_server_config_never_shows_its_env_values():
    """Pre-review 3, C1: the dataclass repr showed env values."""
    assert SECRET not in repr(server("modern", env={"FIXTURE_SECRET": SECRET}))


async def test_a_tool_registered_later_is_never_replaced_by_a_server_tool():
    """Pre-review 3, C2: a tool registered after the Runner was built, under a server tool's
    qualified name, was overwritten in the run's view by the server's tool."""
    registry = ToolRegistry()
    runner = Runner({"m": Turns()}, tool_registry=registry, mcp_servers=[server("legacy")])
    registry.register(Tool(spec=ToolSpec(name="legacy__echo", description="mine", input_schema={"type": "object"}),
                           fn=lambda **a: "the application's own"))
    async with runner:
        result = await run(runner, agent("legacy__*", "legacy__echo"), [[["legacy__echo", {"text": "x"}]]])
    assert (await results(runner, result))["t0-0"].content == "the application's own"


async def test_a_closed_connection_starts_no_server_on_its_own_loop_either(tmp_path):
    """Two guards make close final: the engine refuses every submission after it, and each
    connection refuses to start once closed. Each is tested alone (KNOWLEDGE-aa97d748): this one
    drives a connection on the MCP loop itself, past the engine's guard."""
    pids = tmp_path / "pids"
    runner = runner_for([server("legacy", pids=pids)])

    async def on_the_mcp_loop():
        connection = runner._mcp._engine.connection("legacy", TENANT, PROJECT)
        await connection.ensure()
        await connection.close()
        assert connection.closed and await connection.ensure() == [] and connection.client is None
        with pytest.raises(ToolExecutionError, match="closed"):
            await connection._call("echo", {"text": "late"}, None, "0" * 64)

    await runner._mcp._engine.submit(on_the_mcp_loop(), timeout=120)
    await runner.aclose()
    assert len(pids_in(pids)) == 1 and gone(pids_in(pids)), pids_in(pids)


def test_work_pending_on_the_mcp_loop_at_close_ends_as_closed(tmp_path):
    """The close ends every task left on the MCP loop before stopping it, and a caller whose work
    it ended is told the Runner closed -- never left waiting, never handed a cancellation of its
    own run."""
    import threading

    runner = runner_for([server("legacy")])
    asyncio.run(run(runner, agent("legacy__echo"), [[["legacy__echo", {"text": "x"}]]]))
    outcome = {}

    async def waits_on_the_mcp_loop():
        await asyncio.Event().wait()

    def caller():
        try:
            asyncio.run(runner._mcp._engine.submit(waits_on_the_mcp_loop(), timeout=120))
        except BaseException as exc:  # noqa: BLE001 - judged below
            outcome["error"] = exc

    thread = threading.Thread(target=caller, daemon=True)
    thread.start()
    time.sleep(1)
    asyncio.run(asyncio.wait_for(runner.aclose(), 60))
    thread.join(30)
    assert not thread.is_alive(), "a caller waited on the stopped MCP loop"
    assert isinstance(outcome.get("error"), ToolExecutionError) and "closed" in str(outcome["error"]), outcome


async def test_no_server_schema_is_ever_evaluated_in_this_process(monkeypatch):
    """M33 round 4 bounded schemas carrying a regular expression and validated the rest here, on
    the premise that they could not stall; deep anyOf nesting stalls without one. The invariant
    is now that no server-supplied schema is evaluated in this process at all -- checked, not
    asserted: every schema jsonschema builds a validator for during a run of MCP tools must be
    one of JSON Schema's own metaschemas."""
    import jsonschema

    seen = []
    real = jsonschema.validators.validator_for

    def recording(schema, *args, **kwargs):
        seen.append(schema)
        return real(schema, *args, **kwargs)

    monkeypatch.setattr(jsonschema.validators, "validator_for", recording)
    monkeypatch.setattr(jsonschema, "validate", lambda *a, **k: seen.append(a[1] if len(a) > 1 else k.get("schema")))
    runner = runner_for([server("legacy"), server("modern")])  # both eras: the modern one walks schemas too
    started = time.monotonic()
    async with runner:
        result = await run(runner, agent("legacy__*", "modern__echo"), [
            [["legacy__echo", {"text": "x"}]], [["legacy__counted", {}]], [["legacy__shaped", {}]],
            [["legacy__redos_in", {"s": "aaa"}]], [["legacy__deep_in", {"v": 1}]],
            [["legacy__null_out", {}]], [["modern__echo", {"text": "y"}]]])
    assert result.status is RunStatus.COMPLETED and time.monotonic() - started < 90
    foreign = [s for s in seen if not (isinstance(s, dict) and str(s.get("$id", "")).startswith("https://json-schema.org/"))]
    assert foreign == [], f"{len(foreign)} server schema(s) evaluated in this process"


async def test_a_schema_never_reaches_beyond_itself():
    """Robustness pre-review: the isolated check resolved a `$ref` to a URL, a local file or a UNC
    path -- on Windows a route to leaking credentials. It resolves only references within the
    schema now: this one is unreadable, quickly, and the address is never contacted."""
    import socket
    import threading

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    listener.settimeout(0.2)
    contacted, stop = [], threading.Event()

    def watch():
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
                contacted.append(1)
                connection.close()
            except OSError:
                pass

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    port = str(listener.getsockname()[1])
    try:
        runner = runner_for([server("legacy", extra=("--ref-port", port))])
        started = time.monotonic()
        async with runner:
            result = await run(runner, agent("legacy__remote_ref"), [[["legacy__remote_ref", {"v": 1}]]])
        got = await results(runner, result)
    finally:
        stop.set()
        watcher.join(5)
        listener.close()
    assert got["t0-0"].is_error, got["t0-0"].content
    assert contacted == [], "the schema's URL was fetched"
    assert time.monotonic() - started < 30


def test_a_check_that_could_not_run_never_passes(monkeypatch):
    """Robustness pre-review: a child that died with no output read as "valid". It fails closed."""
    import subprocess as real_subprocess

    import agentsdk.mcp as mcp

    for code, stdout in ((1, ""), (1, "{}"), (0, ""), (0, "   "), (0, "not json"), (0, "[]"), (0, "null")):
        monkeypatch.setattr(mcp.subprocess, "run",
                            lambda *a, code=code, stdout=stdout, **k: real_subprocess.CompletedProcess(a[0], code, stdout=stdout))
        invalid, unreadable = mcp.validate_untrusted({"n": "x"}, {"type": "object"})
        assert (invalid, unreadable) != (None, None), (code, stdout)
        assert "could not be checked" in (invalid or unreadable), (code, stdout)
        with pytest.raises(Exception, match="could not be checked"):
            mcp._isolated_or_raise({"check": [{"type": "object"}]})
    # A verdict for fewer schemas than were sent is no verdict.
    monkeypatch.setattr(mcp.subprocess, "run",
                        lambda *a, **k: real_subprocess.CompletedProcess(a[0], 0, stdout='{"problems": []}'))
    problems = mcp._check_listing([{"type": "object"}, {"type": "object"}])
    assert len(problems) == 2 and all("could not be checked" in p for p in problems), problems
    # And a verdict that is not text or None, for every schema, is no verdict either.
    monkeypatch.setattr(mcp.subprocess, "run",
                        lambda *a, **k: real_subprocess.CompletedProcess(a[0], 0, stdout='{"problems": [1, null]}'))
    assert all("could not be checked" in p for p in mcp._check_listing([{"type": "object"}, {"type": "object"}]))


async def test_a_stalled_mcp_loop_never_holds_its_caller_past_the_bound():
    """Robustness pre-review: every bound lived on the MCP loop, and the caller awaited it with no
    deadline of its own, so anything that stalled the loop held its callers. The caller has one."""
    runner = runner_for([server("legacy")])

    async def stalls_the_loop():
        time.sleep(4)  # the loop itself is busy: nothing scheduled on it can run

    started = time.monotonic()
    with pytest.raises(ToolExecutionError, match="did not answer"):
        await runner._mcp._engine.submit(stalls_the_loop(), timeout=1)
    assert time.monotonic() - started < 3
    await runner.aclose()


async def test_a_start_cancelled_part_way_leaves_nothing_half_open(tmp_path):
    """Robustness pass 2: a cancellation after the server answered and before its listing was
    checked left a client set with no tools, so every later ensure returned nothing, for good.
    Any exit from a start ends the session it began."""
    pids = tmp_path / "pids"
    runner = runner_for([server("legacy", pids=pids)])
    import agentsdk.mcp as mcp

    real = mcp._check_listing
    entered = asyncio.Event()

    def slow_check(*listing):
        entered.set()
        time.sleep(2)  # on a worker thread: the window the cancellation lands in
        return real(*listing)

    mcp._check_listing = slow_check
    try:
        first = asyncio.create_task(runner._mcp.ensure("legacy", TENANT, PROJECT))
        deadline = time.monotonic() + 60
        while not pids_in(pids):
            assert time.monotonic() < deadline
            await asyncio.sleep(0.05)
        await asyncio.sleep(1)  # the server answered; its listing is being checked
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
    finally:
        mcp._check_listing = real
    _, tools = await runner._mcp.ensure("legacy", TENANT, PROJECT)
    assert "echo" in tools, "the half-open session stayed, with no tools"
    await runner.aclose()
    assert gone(pids_in(pids))


async def test_a_declared_output_with_none_returned_fails():
    """Robustness pass 2: a declared output that was not returned is refused, as the library refuses
    it, whatever the schema would accept. (mcp 2.3.0 lists only object-typed output schemas, which
    never accept null, so this pins the outcome; the hardening mirrors the library.)"""
    runner = runner_for([server("legacy")])
    async with runner:
        result = await run(runner, agent("legacy__absent_out"), [[["legacy__absent_out", {}]]])
    got = (await results(runner, result))["t0-0"]
    assert got.is_error and "output does not match its schema" in got.content, got.content


def test_every_wait_in_the_mcp_module_names_its_bound():
    """M33 round 5, the reviewers' structural ask: rounds 1 to 4 each bounded the waits someone
    listed and missed one. Round 6 inverts it: the test no longer looks for known wait forms.
    Every await, `async with`, `async for` and `with` in agentsdk/mcp must name its bound here,
    and so must every call, unless it is the module's own code (checked here in turn) or is named
    below as waiting on nothing. An unknown call form fails, as does a wait or subprocess call
    with no timeout, and a stale entry in either table."""
    import ast

    bounds = {
        # (function, the awaited expression, or the block or call): what bounds it
        ("ensure", "self._ensure()"): "_ensure's own bounds",
        ("_ensure", "self._open()"): "connect_timeout, then the close bound",
        ("_ensure", "self._discard(self.client, self._task)"): "the close bound",
        ("_ensure", "self._schema_problems(listed)"): "the validation deadline",
        ("_read_listing", "client.list_tools(cursor=cursor)"):
            "its caller's: connect_timeout (ready, or the re-read's wait_for), _MAX_PAGES",
        ("serve", "_read_listing(client)"): "connect_timeout (ready)",
        ("serve", "asyncio.wait([listener], timeout=_CLOSE_SECONDS)"): "_CLOSE_SECONDS",
        ("serve", "stop.wait()"): "set by close; the task is cancelled by _ended within the close bound",
        ("_open", "asyncio.wait_for(ready, server.connect_timeout)"): "connect_timeout",
        ("_open", "_ended(task)"): "the close bound",
        ("call", "self._submit(self._call(name, arguments, entry.output_schema, tool, asking), _asked_bound(server, asking))"):
            "_call_bound, and each of three waits' approval_timeout, held by the caller",
        ("_call", "self.current(name)"): "current's own bounds, inside _call_bound",
        ("current", "self._ensure()"): "_ensure's own bounds",
        ("_call", "self._ensure()"): "_ensure's own bounds",
        # M36: the deadline counts only while the call does not wait on a person, whose wait is
        # bounded by approval_timeout in _ask; the request itself carries call_timeout as before.
        ("_call", "_within(request, timeout + _WEDGE_MARGIN, asking, spent)"):
            "call_timeout + margin, counted while not waiting on a person; that wait, approval_timeout",
        ("_within", "asyncio.wait({task}, timeout=0.25 if asking is not None else max(0.0, budget - spent[0]))"):
            "a quarter second, or the budget left",
        ("_within", "asyncio.wait([task], timeout=_CLOSE_SECONDS)"): "_CLOSE_SECONDS",
        ("_within", "call task.result"): "a task known to be done: returns at once",
        ("_call", "_ended_waits(asking)"): "_CLOSE_SECONDS, then _STORE_SECONDS",
        ("_ended_waits", "asyncio.wait(waiting, timeout=_CLOSE_SECONDS)"): "_CLOSE_SECONDS",
        ("_ended_waits", "asyncio.wait(asking.closings, timeout=_STORE_SECONDS)"): "_STORE_SECONDS",
        ("_call", "self._discard(client, task)"): "the close bound",
        ("_call", "wedged()"): "the close bound",
        ("serve", "stack.enter_async_context(Client(transport, mode=server.protocol, cache=None, message_handler=self._on_message, elicitation_callback=self._on_elicit))"):
            "entered within connect_timeout (ready); left within the library's termination grace and the close bound",
        ("wedged", "self._discard(client, task)"): "the close bound",
        ("_discard", "_ended(task)"): "the close bound",
        ("close", "asyncio.wait([task], timeout=_CLOSE_SECONDS)"): "_CLOSE_SECONDS",
        ("close", "_ended(task)"): "the close bound",
        ("_ended", "asyncio.wait([task], timeout=_CLOSE_SECONDS)"): "_CLOSE_SECONDS",
        ("validate_tool_result", "asyncio.to_thread(validate_untrusted, result.structured_content, schema)"): "the validation deadline",
        ("submit", "asyncio.wait({wrapped}, timeout=timeout)"): "the caller's own timeout",
        ("submit", "call wrapped.result"): "an asyncio future known to be done: returns at once",
        # M37 round 3 (DECISION-7888353c): a cancelled caller waits for its work on the MCP loop.
        ("submit", "asyncio.wait({asyncio.wrap_future(ended)}, timeout=_CLOSE_SECONDS + _STORE_SECONDS)"):
            "_CLOSE_SECONDS + _STORE_SECONDS, 10 + 30 = 40 s: the work's waiters ending, then its cancel writes",
        ("submit", "call started.is_set"): "reads a threading.Event: returns at once",
        ("submit", "call threading.Event"): "constructs an event: returns at once",
        ("tracked", "call ended.done"): "reads a concurrent future: returns at once",
        ("tracked", "call ended.set_result"): "sets a concurrent future: returns at once",
        ("tracked", "call started.set"): "sets a threading.Event: returns at once",
        ("tracked", "coroutine"): "the work submit runs, whose bound is submit's own timeout",
        ("_close_all", "asyncio.gather(*(connection.close() for connection in connections), return_exceptions=True)"): "each close is bounded",
        ("_close_all", "asyncio.sleep(0)"): "one loop pass",
        ("_close_all", "asyncio.wait(rest, timeout=_CLOSE_SECONDS)"): "_CLOSE_SECONDS",
        ("on_the_mcp_loop", "connection.ensure()"): "_ensure's own bounds",
        ("ensure", "self._engine.submit(on_the_mcp_loop(), _ensure_bound(self.servers[alias]))"): "_ensure_bound, held by the caller",
        ("aclose", "asyncio.wait_for(asyncio.shield(asyncio.wrap_future(self._engine.close())), bound)"): "the caller's own bound",
        ("aclose", "asyncio.to_thread(thread.join, _CLOSE_SECONDS)"): "_CLOSE_SECONDS",
        ("refresh", "self._clients.ensure(alias, *self._tenancy)"): "the engine call bounds",
        ("prepare", "self.refresh()"): "refresh's bounds",
        ("_schema_problems", "asyncio.to_thread(_check_listing, [entries[i].input_schema for i in unknown], [entries[i].output_schema for i in unknown])"): "the validation deadline",
        # M34: the catalog, re-reads and list-changed notifications
        ("_ensure", "self._reread()"): "its own bounds: connect_timeout, the validation deadline, the close bound",
        ("_ensure", "self._catalogue()"): "_STORE_SECONDS",
        ("_reread", "asyncio.wait_for(_read_listing(client), self.server.connect_timeout)"): "connect_timeout",
        ("_reread", "self._schema_problems(listed)"): "the validation deadline",
        ("_reread", "self._discard(client, task)"): "the close bound",
        ("_read_soon", "async with self._lock"): "held only by _ensure, current, _read_soon and close, all bounded",
        ("_read_soon", "self._reread()"): "its own bounds", ("_read_soon", "self._catalogue()"): "_STORE_SECONDS",
        ("current", "self._engine.submit(on_the_mcp_loop(), _ensure_bound(self.servers[alias]))"): "_ensure_bound, held by the caller",
        ("on_the_mcp_loop", "self._engine.connection(alias, tenant_id, project_id).current(name)"): "current's own bounds",
        ("blocked", "self._clients.current(alias, *self._tenancy, listed)"): "the engine call bound",
        ("_catalogue", "asyncio.wait_for(asyncio.to_thread(_read_catalog, self._catalog(), tenant_id, project_id, alias, versions, pending, self._accept), _STORE_SECONDS)"): "_STORE_SECONDS",
        ("_read_catalog", "call store.record_versions"): "on a worker thread awaited within _STORE_SECONDS; each statement ends within its statement_timeout",
        ("_read_catalog", "call store.accept"): "on a worker thread awaited within _STORE_SECONDS; each statement ends within its statement_timeout",
        ("_read_catalog", "call store.acceptances"): "on a worker thread awaited within _STORE_SECONDS; each statement ends within its statement_timeout",
        ("_read_catalog", "call store.last_accepted"): "on a worker thread awaited within _STORE_SECONDS; each statement ends within its statement_timeout",
        ("listen", "async with client.listen(tools_list_changed=True)"):
            "its session's life: the session's end cancels it and waits _CLOSE_SECONDS; nothing else waits on it",
        ("listen", "async for subscription"):
            "its session's life: the session's end cancels it and waits _CLOSE_SECONDS; nothing else waits on it",
        ("_read_soon", "async with self._lock"): "held only by _ensure, current, _read_soon and close, all bounded",
        ("_isolated_or_raise", "call subprocess.run"): "its timeout, _VALIDATION_SECONDS",
        ("_check_listing", "with concurrent.futures.ThreadPoolExecutor(len(chunks), thread_name_prefix='agentsdk-mcp-check')"):
            "each worker's process ends by the validation deadline, so its exit does",
        ("_check_listing", "call pool.map"): "each worker's process ends by the validation deadline",
        ("_serve_forever", "call loop.run_forever"): "the engine thread's body: runs until _close_all stops it",
        ("_serve_forever", "call loop.run_until_complete"):
            "every task already ended by _close_all; executor work ends by the validation deadline",
        ("_ensure", "async with self._lock"): "held only by _ensure, current, _read_soon and close, all bounded",
        ("close", "async with self._lock"): "held only by _ensure, current, _read_soon and close, all bounded",
        ("submit", "with self._lock"): "held only for bookkeeping, never across a wait",
        ("serve", "with open(os.devnull, 'w')"): "opens a file; waits on nothing",
        ("close", "with self._lock"): "held only for bookkeeping, never across a wait",
        # M35: Streamable HTTP (FR-130), resources and prompts (FR-131)
        ("serve", "async with contextlib.AsyncExitStack()"):
            "entered within connect_timeout (ready); left within the library's termination grace and the close bound",
        ("serve", "stack.enter_async_context(_http_client(server, nonce))"): "an httpx2 client: entering opens nothing",
        ("handle_async_request", "self._inner.handle_async_request(request)"):
            "httpx2's connect timeout, and the caller's wait: call_timeout + margin, or connect_timeout",
        ("handle_async_request", "async for response.stream"):
            "the body, cut past 4 MiB as counted; its time is the caller's wait",
        ("handle_async_request", "response.aclose()"): "closes one response's stream",
        ("__aiter__", "async for self._inner"): "an event stream, ended past 4 MiB; its time is the caller's wait",
        ("aclose", "self._inner.aclose()"): "closes the transport's pool, within the session's close bound",
        ("_up", "self._ensure()"): "_ensure's own bounds",
        ("blocked", "self._ensure()"): "_ensure's own bounds",  # M37 (FR-127, mcp_blocked)
        ("_request", "_within(task, timeout + _WEDGE_MARGIN, asking, [0.0])"):
            "call_timeout + margin, counted while not waiting on a person",
        ("_request", "_ended_waits(asking)"): "_CLOSE_SECONDS, then _STORE_SECONDS",
        ("_pages", "self._request(method(cursor=cursor))"): "_request's call_timeout + margin, _MAX_PAGES times",
        ("_gate", "asyncio.wait_for(asyncio.to_thread(_read_entries, self._catalog(), tenant_id, project_id, alias, versions, hashes, self._accept, kind), _STORE_SECONDS)"): "_STORE_SECONDS",
        ("_read_entries", "call store.record_versions"): "on a worker thread awaited within _STORE_SECONDS; each statement ends within its statement_timeout",
        ("_read_entries", "call store.accept"): "on a worker thread awaited within _STORE_SECONDS; each statement ends within its statement_timeout",
        ("_read_entries", "call store.acceptances"): "on a worker thread awaited within _STORE_SECONDS; each statement ends within its statement_timeout",
        ("_read_entries", "call store.last_accepted"): "on a worker thread awaited within _STORE_SECONDS; each statement ends within its statement_timeout",
        ("resources", "self._up()"): "_ensure's own bounds",
        ("resources", "self._pages(client.list_resources, 'resources')"): "_pages' bounds",
        ("resources", "self._pages(client.list_resource_templates, 'resource_templates')"): "_pages' bounds",
        ("resources", "self._gate('resource_template', entries)"): "_STORE_SECONDS",
        ("read", "self._up()"): "_ensure's own bounds",
        ("read", "self._request(client.read_resource(uri))"): "call_timeout + margin",
        ("_read_as_tool", "self.current(_READER)"): "current's own bounds, inside _call_bound",
        ("_read_as_tool", "self._request(self.client.read_resource(uri), asking)"):
            "call_timeout + margin, counted while not waiting on a person",
        ("_prompt_entries", "self._up()"): "_ensure's own bounds",
        ("_prompt_entries", "self._pages(client.list_prompts, 'prompts')"): "_pages' bounds",
        ("prompts", "self._prompt_entries()"): "_pages' bounds", ("prompts", "self._gate('prompt', entries)"): "_STORE_SECONDS",
        ("prompt", "self._prompt_entries()"): "_pages' bounds",
        ("prompt", "self._gate('prompt', {name: entries[name]})"): "_STORE_SECONDS",
        ("prompt", "self._request(self.client.get_prompt(name, arguments))"): "call_timeout + margin",
        ("call", "self._submit(self._read_as_tool(uri, tool, asking), _asked_bound(server, asking))"):
            "_call_bound, and each of three waits' approval_timeout, held by the caller",
        ("application", "self._engine.submit(on_the_mcp_loop(), _call_bound(self.servers[alias]))"):
            "_call_bound, held by the caller",
        ("on_the_mcp_loop", "getattr(self._engine.connection(alias, tenant_id, project_id), method)(*args)"):
            "one of resources, read, prompts or prompt: each bounded above",
        # M36 (FR-133): a server's request for more input
        ("_ask", "asyncio.wait_for(asyncio.to_thread(store.append_interruption, item), _STORE_SECONDS)"): "_STORE_SECONDS",
        ("_ask", "asyncio.wait_for(asyncio.to_thread(store.interruption, tenant_id, project_id, ident), _STORE_SECONDS)"): "_STORE_SECONDS",
        ("_ask", "asyncio.wait_for(asyncio.to_thread(store.append_decision, expiry), _STORE_SECONDS)"): "_STORE_SECONDS",
        ("_ask", "asyncio.wait_for(event.wait(), timeout=pause)"): "five seconds at most, or the time left to its expiry",
        ("_ask", "self._emit(asking, EventType.INPUT_REQUESTED, _input_event(item))"): "_STORE_SECONDS",
        ("_ask", "self._emit(asking, EventType.INPUT_ANSWERED, {**_input_event(item), 'action': decision.decision})"): "_STORE_SECONDS",
        ("_emit", "asyncio.wait_for(asyncio.to_thread(events.emit, event_type, payload, task_id=asking.scope.node_id), _STORE_SECONDS)"): "_STORE_SECONDS",
        ("_emit", "with contextlib.suppress(Exception)"): "suppresses; waits on nothing",
        ("_quietly", "asyncio.wait_for(asyncio.to_thread(write, *args), _STORE_SECONDS)"): "_STORE_SECONDS",
        ("_quietly", "with contextlib.suppress(Exception)"): "suppresses; waits on nothing",
        ("_unsupported", "asyncio.wait_for(asyncio.to_thread(store.append_interruption, item, decided), _STORE_SECONDS)"): "_STORE_SECONDS",
        ("_on_elicit", "self._ask(asking, 'additional_user_input', payload)"):
            "_ask's bounds: the store bound, and the request's expiry, approval_timeout",
        ("_on_elicit", "self._emit(candidate, EventType.INPUT_UNATTRIBUTED, {'alias': self.server.alias, 'tool': candidate.tool})"): "_STORE_SECONDS",
        ("seen", "dispatch(context, request)"):
            "the library's own dispatch: an elicitation is _on_elicit's, bounded; sampling and roots answered at once",
        ("seen", "self._unsupported(asking, method)"): "_STORE_SECONDS",
    }
    # Calls that wait on nothing, each by its callee as written. Every cost here is at most linear
    # in what it is given: anything that is not belongs in `bounds`, with its bound.
    waits_on_nothing = {
        # builtins, exceptions, constructors and the SDK's own value types
        "all", "any", "dict", "enumerate", "float", "frozenset", "getattr", "isinstance", "len", "list",
        "max", "range", "set", "sorted", "str", "tuple", "type", "super", "super().__init__",
        "object.__setattr__", "dataclass", "field", "math.isfinite", "Counter",
        "ConnectionError", "ImportError", "RuntimeError", "TimeoutError", "ValueError", "ToolExecutionError",
        "PermissionResult", "ResultProvenance", "Tool", "ToolOutput", "ToolSpec", "unstorable_reason",
        "StdioServerParameters", "Client", "stdio_client",  # constructed here, entered by `async with`
        # text and data, linear in their input
        "_ALIAS.fullmatch", "_QUALIFIED.fullmatch", "name.partition", "p.startswith", "value.startswith", "done.stdout.strip",
        "json.dumps",
        "pathlib.Path(jsonschema.__file__).resolve",
        # lookups in the SDK's own dicts, lists and sets
        "base.get", "base.names", "self._base.get", "self._base.names", "self._known.get",
        "self._known.get(alias, {}).get", "self._known.get(alias, {}).items", "self._tools.get",
        "self.connections.values",
        "self.env.items", "self.servers.values", "server.approval_overrides.get",
        "session._tool_output_schemas.get", "verdict.get", "_isolated_or_raise({'check': schemas, 'outputs': outputs}).get",
        "events.append", "self._pending.append", "self._down.add", "self._down.discard",
        "self._engine.connection", "server.record", "self._view.served_by_wildcard",
        # asyncio and threading bookkeeping: none of these waits
        "asyncio.Event", "asyncio.Lock", "asyncio.all_tasks", "asyncio.create_task", "asyncio.current_task",
        "asyncio.get_running_loop", "asyncio.get_running_loop().call_soon", "asyncio.get_running_loop().create_future",
        "asyncio.new_event_loop", "asyncio.run_coroutine_threadsafe", "asyncio.set_event_loop", "asyncio.shield",
        "asyncio.wrap_future", "concurrent.futures.Future", "concurrent.futures.ThreadPoolExecutor",
        "threading.RLock", "threading.Thread", "weakref.finalize", "self._thread.start", "thread.is_alive",
        "clock.time", "loop.time", "loop.close", "task.cancel", "task.done", "wrapped.cancel", "coroutine.close",
        "ready.done", "ready.set_exception", "ready.set_result", "self._stop.set", "engine._done.done",
        "engine._done.set_result", "self._done.set_result",
        "self._engine.close",  # returns the close's future at once
        # coroutines created here and awaited by an expression in `bounds`
        "client.call_tool", "connection.close", "loop.shutdown_asyncgens", "loop.shutdown_default_executor",
        # the application's own permission checker, called as it is without MCP
        "self._inner.check",
        # M34: the catalog's values and bookkeeping
        "InMemoryCatalogStore", "ToolVersion", "ToolBlocked", "checked_hash", "changed_parts", "tool_entry",
        "next", "hasattr", "min", "zip", "datetime.now", "self._clock",  # the catalog's clock: monotonic, or a test's
        "self._catalog",  # the store factory: builds a store object, connects to nothing
        "tenancies.add", "self._skips.add", "self._told.add", "self.usable.get", "connection.usable.get",
        "hashes.items", "previous.get", "self._blocked.get", "self.tools.items", "connection.tools.items",
        "pending.items", "pending.values", "fresh.items", "hints.get",
        "self._listener.done", "listener.done", "listener.cancel",
        "hashes.get", "self._blocked.items", "self.tools.get", "payload.get",
        "asyncio.get_running_loop().create_task",  # schedules; awaits nothing
        "asyncio.to_thread",  # a coroutine, awaited within the wait_for named in `bounds`
        "client.listen",  # a context manager, entered by the `async with` named in `bounds`
        # M35: values, text and bookkeeping, each linear in what it is given
        "entry_hash", "prompt_entry", "template_entry", "_UNTRUSTED.for_source", "secrets.token_hex",
        "_TOKEN.fullmatch", "_MIME.fullmatch", "_FIELD_VALUE.fullmatch", "name.lower", "host.lower", "parts.scheme.lower", "kind.startswith",
        "urlsplit", "ipaddress.ip_address", "headers.items", "data.get", "_REFUSALS.get",
        "server.approval_overrides.items", "server.approval_overrides.values", "response.headers.get",
        "entries.items", "accepted.items", "blocked.append", "content.text.encode", "bytes", "bytearray",
        "value.model_dump", "a.model_dump",  # pydantic's dump of a parsed model: linear
        "httpx2.Response", "httpx2.ReadError", "httpx2.Timeout", "httpx2.AsyncHTTPTransport", "httpx2.AsyncClient",
        "contextlib.AsyncExitStack", "streamable_http_client",  # constructed here, entered by `async with`
        "client.read_resource", "self.client.read_resource", "self.client.get_prompt",  # awaited by `_request`
        "method",  # a listing method of the client: a coroutine awaited by `_request`
        "response.headers.get('content-encoding', 'identity').strip",
        "response.headers.get('content-encoding', 'identity').strip().lower",
        "response.headers.get('content-type', '').lower",
        "json.dumps(listed).encode", "json.loads(request.content or b'{}').get",  # text, linear
        # M36: values, bookkeeping and the library's request clock, none of which waits
        "contextvars.ContextVar", "contextvars.copy_context", "_CALL.set", "_CALL.reset", "_CALL.get",
        "asking.context.get", "asking.waiters.add", "asking.waiters.discard", "asking.closings.append", "object", "self._in_flight.append",
        "self._in_flight.remove", "clock.pause", "clock.resume", "event.clear", "event.wait", "waits.register",
        "waits.unregister", "runner._interruption_store", "asking.scope._runner._interruption_store",
        "(item.expires_at - datetime.now(timezone.utc)).total_seconds", "InterruptionKind", "RunInterruption",
        "InterruptionDecision", "timedelta", "uuid.uuid4", "item.request_payload.get", "ElicitResult", "ErrorData",
        "contextlib.suppress", "_MIME_PARAMETER.fullmatch", "part.strip", "value.split", "configured.items",
    }
    # M33 review caveat (M34 pre-flight): a call whose cost or reach is its argument's -- a path
    # opened, a pattern compiled, text parsed or decoded -- is exempt only as written here, with
    # that argument. `open(server_path)` or `re.compile(server_pattern)` fails the test.
    by_argument = {"open", "re.compile", "json.loads", "base64.b64decode", "pathlib.Path"}
    exact = {
        "open(os.devnull, 'w')": "the null device",
        "re.compile('[a-z][a-z0-9-]{0,23}')": "the SDK's own pattern, linear",
        "re.compile('[A-Za-z0-9_-]{1,64}')": "the SDK's own pattern, linear",
        "json.loads(done.stdout)": "the checker child's verdict: linear in what it printed",
        "base64.b64decode(data)": "a block's data: linear",
        're.compile("[!#$%&\'*+.^_`|~0-9A-Za-z-]+")': "the SDK's own pattern, linear",
        "re.compile('[!-~](?:[ -~\\t]*[!-~])?')": "the SDK's own pattern, linear",
        "re.compile('[A-Za-z0-9!#$&^_.+-]{1,127}/[A-Za-z0-9!#$&^_.+-]{1,127}')": "the SDK's own pattern, linear",
        're.compile(\'[A-Za-z0-9!#$&^_.+-]{1,127}=(?:[A-Za-z0-9!#$&^_.+-]{1,127}|"[^"\\\\s\\\\\\\\]{0,125}")\')':
            "the SDK's own pattern, linear",
        "base64.b64decode(content.blob, validate=True)": "a resource's blob: linear",
        "json.loads(request.content or b'{}')": "the SDK's own request body: linear",
        "pathlib.Path(jsonschema.__file__)": "the installed package's own path",
    }
    source = (REPO / "agentsdk" / "mcp" / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    used_exact = set()
    own = {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    found, untimed, used, awaited = set(), [], set(), set()

    def timeout_of(call, position):
        keyword = [k.value for k in call.keywords if k.arg == "timeout"]
        value = keyword[0] if keyword else (call.args[position] if len(call.args) > position else None)
        return None if value is None or ast.unparse(value) == "None" else value

    def visit(node, function):
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else function
            if isinstance(child, ast.Await):
                found.add((function, ast.unparse(child.value)))
                awaited.add(id(child.value))
            if isinstance(child, (ast.With, ast.AsyncWith)):
                kind = "async with " if isinstance(child, ast.AsyncWith) else "with "
                for item in child.items:
                    found.add((function, kind + ast.unparse(item.context_expr)))
            if isinstance(child, ast.AsyncFor) or (isinstance(child, ast.comprehension) and child.is_async):
                found.add((function, "async for " + ast.unparse(child.iter)))
            if isinstance(child, ast.Call):
                callee = ast.unparse(child.func)
                if callee in ("subprocess.run", "subprocess.Popen", "subprocess.check_output", "subprocess.call") \
                        and timeout_of(child, 99) is None:
                    untimed.append(ast.unparse(child))
                if callee in ("asyncio.wait_for", "asyncio.wait") and timeout_of(child, 1 if callee.endswith("for") else 99) is None:
                    untimed.append(ast.unparse(child))
                bare = callee.removeprefix("self.")
                if id(child) in awaited:
                    pass  # its wait is the await's, named in `bounds`
                elif isinstance(child.func, ast.Attribute) and isinstance(child.func.value, ast.Constant):
                    pass  # a method of a literal: ", ".join(...)
                elif bare in own:
                    pass  # the module's own code, whose every call is checked here too
                elif callee in by_argument:
                    if ast.unparse(child) in exact:
                        used_exact.add(ast.unparse(child))
                    else:
                        found.add((function, "call " + ast.unparse(child)))
                elif callee in waits_on_nothing:
                    used.add(callee)
                else:
                    found.add((function, "call " + callee))
            visit(child, name)

    visit(tree, "<module>")
    unlisted = sorted(found - set(bounds))
    stale = sorted(set(bounds) - found)
    assert not untimed, f"a wait or subprocess call with no timeout: {untimed}"
    assert not unlisted, f"waits, or calls not known to wait on nothing, that name no bound: {unlisted}"
    assert not stale, f"bounds listed for waits that no longer exist: {stale}"
    assert not waits_on_nothing - used, f"calls listed that no longer exist: {sorted(waits_on_nothing - used)}"
    assert not set(exact) - used_exact, f"calls listed that no longer exist: {sorted(set(exact) - used_exact)}"
    # And the one call site outside the package that waits on a server's schema.
    executor = (REPO / "agentsdk" / "executor.py").read_text(encoding="utf-8")
    assert "asyncio.to_thread(\n                    validate_untrusted, tool_call.arguments, tool.spec.input_schema)" in executor
    assert "timeout=_VALIDATION_SECONDS" in source, "validate_untrusted's process carries the validation deadline"


def test_every_loop_in_the_mcp_module_names_what_it_iterates():
    """Round 6 pre-review: the bounds test examined calls and awaits only, so `for line in f`,
    `list(f)`, `while True`, a recursion or a quadratic loop could enter unseen. Every `for`, every
    comprehension and every builtin that consumes an iterable must name what it iterates and why
    that is finite and in memory; every `while`, nested loop, recursion and import inside a
    function must name its bound. An unknown one fails, as does a stale entry."""
    import ast

    config = "the developer's own configuration, finite"
    built = "a list or dict the SDK built in memory, linear in a server's data"
    iterations = {
        ("<module>", "{TaintFlag.EXTERNAL_CONTENT}"): "a literal",
        ("__init__", "aliases"): config, ("__init__", "servers"): config, ("__init__", "clients.servers"): config,
        ("__init__", "profile"): config, ("__init__", "base.names()"): "the registry's names, a tuple",
        # M34
        ("__init__", "server.accept"): config, ("__post_init__", "self.tool_hashes"): config,
        ("_list", "self.server.approval_overrides"): config, ("_list", "counts"): built,
        ("_ensure", "self._unmatched"): built, ("_catalogue", "hashes.items()"): built,
        ("_catalogue", "self.tools.items()"): built, ("_catalogue", "versions"): built,
        ("_catalogue", "payload"): "one event's payload, a dict the SDK built",
        ("_read_catalog", "pending.items()"): built, ("_read_catalog", "pending.values()"): built,
        ("_read_catalog", "store.acceptances(tenant_id, project_id, alias, list(pending.values()))"):
            "the store's answer for the hashes asked: at most _MAX_TOOLS",
        ("_read_listing", "range(_MAX_PAGES)"): "_MAX_PAGES", ("_read_listing", "ttl"): "a number, not an iterable",
        ("_schema_problems", "entries"): built, ("_schema_problems", "enumerate(keys)"): built,
        ("_schema_problems", "found"): built, ("_schema_problems", "fresh.items()"): built,
        ("_schema_problems", "range(len(entries))"): built, ("_schema_problems", "unknown"): built,
        ("_schema_problems", "zip(unknown, found)"): built,
        ("_catalogue", "self._blocked.items()"): "this listing's blocked payloads: at most _MAX_TOOLS",
        ("_catalogue", "versions[room:]"): built, ("current", "await self._ensure()"): "the events of one ensure: a list the SDK built", ("_list", "self._skips"): "this listing's skips: at most _MAX_TOOLS", ("on_the_mcp_loop", "connection.tools.items()"): built,
        ("__post_init__", "self.args"): config,  # M35: StdioServer's own; the common fields are _check_common's
        ("__post_init__", "self.env or {}"): config, ("__post_init__", "self.env.items()"): config,
        ("record", "self.args"): config, ("record", "self.env"): config,
        ("records", "self.servers.values()"): config, ("aclose", "self.servers.values()"): config,
        ("checker", "self.servers"): config, ("serve", "server.args"): config, ("serve", "server.env"): config,
        ("_call", "result.content"): "the result's blocks, parsed by the library: " + built,
        ("_check_chunk", "problems"): "the child's verdict, its length checked first",
        ("_check_listing", "starts"): "a range over the listing, at most _MAX_TOOLS",
        ("_check_listing", "pool.map(lambda pair: _check_chunk(*pair), chunks)"): "at most 20 chunks, each by the deadline",
        ("_check_listing", "chunk"): "one chunk's verdict, at most _CHECK_CHUNK",
        ("_close_all", "asyncio.all_tasks()"): "the loop's tasks, a set", ("_close_all", "connections"): built,
        ("_close_all", "range(5)"): "five passes", ("_close_all", "rest"): built,
        ("_close_all", "self.connections.values()"): built,
        ("_list", "enumerate(listed)"): built, ("_list", "listed"): built,
        ("ensure", "events"): built, ("refresh", "events"): built, ("refresh", "self._aliases"): config,
        ("refresh", "self._base.names()"): "the registry's names, a tuple",
        ("refresh", "self._known.get(alias, {}).items()"): built,
        # M35
        ("<module>", "{'accept', 'accept-encoding', 'connection', 'content-length', 'content-type', 'host', "
                     "'last-event-id', 'mcp-protocol-version', 'mcp-session-id', 'transfer-encoding'}"): "a literal",
        # M36
        ("_caller", "self._in_flight"): "the calls in flight on one connection: " + built,
        ("_ended_waits", "asking.waiters"): "one call's input requests: at most three",
        ("_ended_waits", "waiting"): "one call's input requests: at most three",
        ("_on_elicit", "candidates"): "the calls in flight on one connection: " + built,
        ("_mime", "parameters"): "a content type's parameters: linear in its text",
        ("_mime", "value.split(';')"): "a content type's parameters: linear in its text",
        ("handle_async_request", "configured.items()"): config, ("_http_client", "server.headers"): config,
        ("__post_init__", "headers.items()"): config,
        ("__post_init__", "self.headers or {}"): config, ("record", "self.headers"): config,
        ("_check_common", "('call_timeout', 'connect_timeout')"): "a literal",
        ("_check_common", "('tenant_id', 'project_id')"): "a literal",
        ("_check_common", "(*server.deny, *(server.allow or ()))"): config,
        ("_check_common", "(*server.deny, *(server.allow or ()), *server.approval_overrides)"): config,
        ("_check_common", "(server.approval, *server.approval_overrides.values())"): config,
        ("_check_common", "item.tool_hashes"): config, ("_check_common", "server.accept"): config,
        ("_check_common", "server.allow"): config, ("_check_common", "server.approval_overrides"): config,
        ("_check_common", "server.deny"): config,
        ("_common_record", "server.allow"): config, ("_common_record", "server.approval_overrides.items()"): config,
        ("_common_record", "server.deny"): config, ("_common_record", "sorted(server.approval_overrides.items())"): config,
        ("_gate", "entries.items()"): built, ("_gate", "hashes.items()"): built, ("_gate", "versions"): built,
        ("_pages", "range(_MAX_PAGES)"): "_MAX_PAGES",
        ("_prompt_entries", "listed"): "a listing cut at _MAX_TOOLS", ("_prompt_entries", "p.arguments or []"): built,
        ("_read_entries", "pending.items()"): built, ("_read_entries", "pending.values()"): built,
        ("_read_entries", "store.acceptances(tenant_id, project_id, alias, list(pending.values()))"):
            "the store's answer for the hashes asked: at most _MAX_TOOLS",
        ("_up", "await self._ensure()"): "the events of one ensure: a list the SDK built",
        # M37 (FR-127 mcp_blocked; FR-133's request cap)
        ("blocked", "events"): "the events of one ensure: a list the SDK built",
        ("_request_size", "('message', 'requested_schema', 'url')"): "a literal",
        ("_read_as_tool", "result.contents"): "a read's contents, within the 4 MiB body: " + built,
        ("read", "contents"): built, ("read", "result.contents"): "a read's contents: " + built,
        ("prompt", "result.messages"): "a prompt's messages: " + built,
        ("prompts", "accepted.items()"): built, ("prompts", "blocked"): built,
        ("prompts", "entries[n]['arguments']"): built,
        ("resources", "accepted.items()"): built, ("resources", "blocked"): built,
        ("resources", "listed"): "a listing cut at _MAX_TOOLS", ("resources", "templates"): "a listing cut at _MAX_TOOLS",
    }
    bounds = {
        ("while", "_cause", "isinstance(exc, BaseExceptionGroup) and exc.exceptions"):
            "descends one exception group per pass, to its first leaf",
        ("nested", "__init__", "clients.servers / profile"): "configuration by configuration",
        ("nested", "_check_listing", "pool.map(lambda pair: _check_chunk(*pair), chunks) / chunk"):
            "flattens the verdicts: linear",
        ("nested", "_close_all", "range(5) / asyncio.all_tasks()"): "five passes",
        ("nested", "_close_all", "range(5) / rest"): "five passes",
        ("nested", "refresh", "self._aliases / events"): "each alias's own events: linear",
        ("nested", "refresh", "self._aliases / self._known.get(alias, {}).items()"): "each alias's own tools: linear",
        ("import", "__init__", "import mcp"): "a module of the venv, imported once",
        ("import", "_call", "from mcp import MCPError"): "a module of the venv, imported once",
        ("import", "_isolated_or_raise", "import jsonschema"): "a module of the venv, imported once",
        ("import", "_open", "from mcp import Client, StdioServerParameters"): "a module of the venv, imported once",
        ("import", "_open", "from mcp.client.stdio import stdio_client"): "a module of the venv, imported once",
        ("import", "check", "from ..permissions import Decision, PermissionResult"): "the SDK's own module",
        ("import", "__init__", "from ..catalog import InMemoryCatalogStore"): "the SDK's own module",
        ("import", "_on_message", "from mcp.types import ToolListChangedNotification"): "a module of the venv, imported once",
        ("nested", "_check_common", "server.accept / ('tenant_id', 'project_id')"): "configuration by a two-name literal",
        ("nested", "_check_common", "server.accept / item.tool_hashes"): "configuration by configuration",
        # M35
        ("import", "listen", "from mcp import MCPError"): "a module of the venv, imported once",
        # M36 (FR-133)
        ("while", "_ask", "decision is None"):
            "ends at the decision, or at the request's expiry (approval_timeout), which decides it",
        ("while", "_within", "True"): "ends with the request, or once the budget is spent",
        ("import", "_ask", "from ..interruptions import InterruptionAlreadyDecided, InterruptionDecision"):
            "the SDK's own module",
        ("import", "_ask", "from ..outcomes import InterruptionKind"): "the SDK's own module",
        ("import", "_interruption", "from ..outcomes import RunInterruption"): "the SDK's own module",
        ("import", "_interruption", "from datetime import timedelta"): "the standard library",
        ("import", "_unsupported", "from ..interruptions import InterruptionDecision"): "the SDK's own module",
        ("import", "_unsupported", "from ..outcomes import InterruptionKind"): "the SDK's own module",
        ("import", "_clock_of", "from mcp.shared._request_clock import _clock"): "a module of the venv, imported once",
        ("import", "_on_elicit", "from mcp.types import ElicitResult"): "a module of the venv, imported once",
        ("import", "_see_input_requests", "from mcp.types import CreateMessageRequest, ListRootsRequest"):
            "a module of the venv, imported once",
        ("import", "_refused", "from mcp.types import ErrorData"): "a module of the venv, imported once",  # M37: was _too_many
        ("import", "answer_problem", "from mcp.types import ElicitResult"): "a module of the venv, imported once",
        ("import", "listen", "from mcp.client.subscriptions import ListenNotSupportedError"): "a module of the venv, imported once",
        ("nested", "_prompt_entries", "listed / p.arguments or []"): "each prompt's own arguments: linear",
        ("import", "_check_url", "from urllib.parse import urlsplit"): "the standard library",
        ("import", "_check_url", "import ipaddress"): "the standard library",
        ("import", "_http_client", "import httpx2"): "a module of the venv, imported once",
        ("import", "_request", "from mcp import MCPError"): "a module of the venv, imported once",
        ("import", "serve", "from mcp.client.streamable_http import streamable_http_client"): "a module of the venv, imported once",
    }
    consumers = {"list", "sorted", "set", "dict", "tuple", "any", "all", "max", "min", "sum", "enumerate",
                 "frozenset", "Counter", "zip", "map", "filter", "reversed"}
    literals = (ast.Constant, ast.Tuple, ast.List, ast.ListComp, ast.GeneratorExp, ast.SetComp, ast.DictComp,
                ast.JoinedStr, ast.BinOp)
    tree = ast.parse((REPO / "agentsdk" / "mcp" / "__init__.py").read_text(encoding="utf-8"))
    iterated, found = set(), set()

    def visit(node, function, loops):
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else function
            outer = loops if name == function else ()
            if isinstance(child, ast.For):
                iterated.add((function, ast.unparse(child.iter)))
                if outer:
                    found.add(("nested", function, outer[-1] + " / " + ast.unparse(child.iter)))
            if isinstance(child, ast.While):
                found.add(("while", function, ast.unparse(child.test)))
            if isinstance(child, (ast.Import, ast.ImportFrom)) and function != "<module>":
                found.add(("import", function, ast.unparse(child)))
            if isinstance(child, ast.Call):
                callee = ast.unparse(child.func)
                if callee.removeprefix("self.") == function:
                    found.add(("recursion", function, function))
                if isinstance(child.func, ast.Name) and callee in consumers:
                    for argument in child.args:
                        if not isinstance(argument, literals):
                            iterated.add((function, ast.unparse(argument)))
            if isinstance(child, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                stack = outer
                for generator in child.generators:
                    iterated.add((function, ast.unparse(generator.iter)))
                    if stack:
                        found.add(("nested", function, stack[-1] + " / " + ast.unparse(generator.iter)))
                    visit(generator.iter, function, stack)
                    stack = (*stack, ast.unparse(generator.iter))
                    for condition in generator.ifs:
                        visit(condition, function, stack)
                for part in ([child.elt] if hasattr(child, "elt") else [child.key, child.value]):
                    visit(ast.Module(body=[ast.Expr(part)], type_ignores=[]), function, stack)
                continue
            visit(child, name, (*outer, ast.unparse(child.iter)) if isinstance(child, ast.For) else outer)

    visit(tree, "<module>", ())
    assert not iterated - set(iterations), f"iterations that name nothing: {sorted(iterated - set(iterations))}"
    assert not set(iterations) - iterated, f"iterations listed that no longer exist: {sorted(set(iterations) - iterated)}"
    assert not found - set(bounds), f"loops, recursions or imports that name no bound: {sorted(found - set(bounds))}"
    assert not set(bounds) - found, f"bounds listed that no longer exist: {sorted(set(bounds) - found)}"


def test_the_bounds_test_fails_on_a_wait_form_it_does_not_know(monkeypatch, tmp_path):
    """Round 5 review: the bounds test looked for known wait forms, and missed `async for`,
    `.get()`, `os.read`, `run_until_complete` and `subprocess.check_output`; round 6's pre-review
    found loops, recursion and imports unseen too. Each, added to a copy of the module, fails one
    of the two invariant tests."""
    import ast

    real = (REPO / "agentsdk" / "mcp" / "__init__.py").read_text(encoding="utf-8")
    additions = [
        "async def _probe(q):\n    async for item in q:\n        pass\n",
        "def _probe(q):\n    return q.get()\n",
        "def _probe(fd):\n    return os.read(fd, 1)\n",
        "def _probe(loop, c):\n    return loop.run_until_complete(c)\n",
        "def _probe():\n    return subprocess.check_output(['x'])\n",
        "async def _probe(e):\n    await asyncio.wait_for(e.wait(), None)\n",
    ]
    package = tmp_path / "agentsdk" / "mcp"
    package.mkdir(parents=True)
    (tmp_path / "agentsdk" / "executor.py").write_bytes((REPO / "agentsdk" / "executor.py").read_bytes())
    monkeypatch.setattr(sys.modules[__name__], "REPO", tmp_path)
    # Round 6 pre-review: loops, recursion and imports, which the call-only walk did not see.
    additions += [
        "def _probe(f):\n    for line in f:\n        pass\n",
        "def _probe():\n    for line in sys.stdin:\n        pass\n",
        "def _probe(f):\n    return list(f)\n",
        "def _probe(f):\n    return sorted(f)\n",
        "def _probe(x):\n    while True:\n        x = x + 1\n",
        "def _probe(n):\n    return _probe(n + 1)\n",
        "def _probe(listed):\n    return [a for a in listed for b in listed if a is b]\n",
        "def _probe():\n    import socket\n    return socket\n",
        # M34 pre-flight: exemptions keyed on the argument, not the callee.
        "def _probe(path):\n    with open(path) as f:\n        return f\n",
        "def _probe(pattern):\n    return re.compile(pattern)\n",
        "def _probe(text):\n    return json.loads(text)\n",
    ]
    checks = (test_every_wait_in_the_mcp_module_names_its_bound, test_every_loop_in_the_mcp_module_names_what_it_iterates)
    for addition in additions:
        ast.parse(addition)
        (package / "__init__.py").write_text(real + "\n\n" + addition, encoding="utf-8")
        failed = []
        for check in checks:
            try:
                check()
            except AssertionError:
                failed.append(check.__name__)
        assert failed, f"no invariant test failed on: {addition!r}"
    (package / "__init__.py").write_text(real, encoding="utf-8")
    for check in checks:
        check()  # the copy unchanged passes


def test_a_stalled_test_dumps_its_asyncio_tasks_too(tmp_path):
    """M33 round 2: faulthandler_timeout dumps thread stacks only, so an asyncio stall shows the
    loop polling. The conftest watchdog prints every running loop's task stacks on the same
    timeout. Run in a pytest session of its own, with the suite's conftest copied in unchanged."""
    (tmp_path / "conftest.py").write_bytes((REPO / "tests" / "conftest.py").read_bytes())
    (tmp_path / "pytest.ini").write_text("\n".join(["[pytest]", "asyncio_mode = auto", "faulthandler_timeout = 2", ""]))
    (tmp_path / "test_stuck.py").write_text("\n".join([
        "import asyncio", "", "",
        "async def waits_for_a_peer_forever():", "    await asyncio.Event().wait()", "", "",
        # It passes once its wait times out, so no failure traceback shows the line: only the dump.
        "async def test_stuck():", "    try:", "        await asyncio.wait_for(waits_for_a_peer_forever(), 5)",
        "    except asyncio.TimeoutError:", "        pass", ""]))
    done = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", str(tmp_path)],
                          cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(REPO)}, capture_output=True,
                          text=True, timeout=120)
    output = done.stdout + done.stderr
    if DSN:
        output = output.replace(DSN, "<dsn>")
    # The stack, not only the task's name: the line it waits on.
    assert "asyncio tasks" in output and "await asyncio.Event().wait()" in output, output[-3000:]


async def test_closing_a_server_that_ignores_its_input_ending_is_bounded(tmp_path):
    pids = tmp_path / "pids"
    runner = runner_for([server("legacy", pids=pids, extra=("--deaf",))])
    async with runner:
        await run(runner, agent("legacy__echo"), [[["legacy__echo", {"text": "hi"}]]])
        started = time.monotonic()
    assert time.monotonic() - started < 15 and gone(pids_in(pids))


async def test_the_wildcard_never_allows_a_registered_tool_under_a_configured_alias():
    """Round 4's third finding, re-found by round 5's review: with a server aliased `legacy`
    configured, `legacy__*` allowed a registered `legacy__wipe` the profile never named -- the
    check asked only whether a name began with a configured alias. It asks now whether the tool
    the run executes under that name is one the server listed."""
    ran = []

    def first_party(name):
        return Tool(spec=ToolSpec(name=name, description="first-party", input_schema={"type": "object"}),
                    fn=lambda: ran.append(name) or "ran")

    runner = runner_for([server("legacy")], tools=[first_party("legacy__wipe"), first_party("legacy__clash"),
                                                   first_party("legacy")])
    async with runner:
        result = await run(runner, agent("legacy__*"), [
            [["legacy__wipe", {}]], [["legacy__clash", {}]], [["legacy", {}]], [["legacy__echo", {"text": "x"}]]])
    got = await results(runner, result)
    for call in ("t0-0", "t1-0", "t2-0"):
        assert got[call].content.startswith("ToolPermissionDenied"), (call, got[call].content)
    assert got["t3-0"].content == "x", "a tool the server listed is still allowed by its wildcard"
    assert ran == []


def test_the_wildcard_allows_only_a_tool_its_server_listed():
    """M33 round 1 caveat, `srv__*` allowing a tool named `srv`, and round 6: the check reads the
    run's own view, so only the object a connection built is allowed, whatever the name."""
    from agentsdk.mcp import RunTools, _McpAllowlist
    from agentsdk.permissions import AllowlistPermissionChecker, Decision

    listed = Tool(spec=ToolSpec(name="srv__tool", description="", input_schema={"type": "object"}), fn=lambda: "")
    impostor = Tool(spec=ToolSpec(name="srv__other", description="", input_schema={"type": "object"}), fn=lambda: "")
    view = RunTools.__new__(RunTools)
    view._profile = ("srv__*",)
    view._known = {"srv": {"tool": listed, "other": listed}}
    view._tools = {"srv__tool": listed, "srv__other": impostor, "srv": listed, "srvx__t": listed}
    checker = _McpAllowlist(AllowlistPermissionChecker(set()), view)
    allowed = {name: checker.check(ToolCall(id="c", name=name, arguments={})).decision is Decision.ALLOW
               for name in ("srv__tool", "srv__other", "srv", "srv__", "srvx__t", "other__tool")}
    assert allowed == {"srv__tool": True, "srv__other": False, "srv": False, "srv__": False, "srvx__t": False,
                       "other__tool": False}
    view._profile = ("srv__tool",)  # named outright, not by the wildcard: the inner checker decides
    assert checker.check(ToolCall(id="c", name="srv__tool", arguments={})).decision is Decision.DENY


async def test_an_entry_the_sdk_cannot_hold_skips_that_tool_alone():
    """Round 5 review: a NUL inside one tool's schema made its whole server unavailable, and a tool
    with no name was offered as `legacy__`, refused only when called. Each is skipped with its
    reason, a NUL in a description too, and the server's other tools stay."""
    model = Turns()
    runner = runner_for([server("legacy", extra=("--odd-listing",))], model=model)
    async with runner:
        result = await run(runner, agent("legacy__*"), [[["legacy__echo", {"text": "x"}]], [["legacy__picture_shaped", {}]]])
    assert not events(result, EventType.MCP_SERVER_UNAVAILABLE)
    skipped = {e.payload["tool"]: e.payload["reason"] for e in events(result, EventType.MCP_TOOL_SKIPPED)}
    assert skipped[""] == "it has no name"
    assert skipped["nul_schema"].startswith("it cannot be stored"), skipped["nul_schema"]
    assert skipped["nul_description"].startswith("it cannot be stored"), skipped["nul_description"]
    offered = {t["function"]["name"] for t in model.requests[0].tools}
    assert "legacy__echo" in offered and not offered & {"legacy__", "legacy__nul_schema", "legacy__nul_description"}
    got = await results(runner, result)
    assert got["t0-0"].content == "x"
    # Round 1 caveat: an image beside structured content and no text was replaced by the JSON.
    content = got["t1-0"].content
    assert content.startswith("[image content omitted: image/png") and content.endswith('{"n": 1}'), content


async def test_any_entry_the_sdk_cannot_build_skips_that_tool_alone(monkeypatch):
    """Round 6: the property, beside the cases above -- whatever refuses one listed tool, its
    server's other tools stay and the server is not unavailable."""
    import agentsdk.mcp as mcp

    real = mcp.ToolSpec

    def refusing(**fields):
        if fields["name"] == "legacy__echo":
            raise ValueError("refused")
        return real(**fields)

    monkeypatch.setattr(mcp, "ToolSpec", refusing)
    runner = runner_for([server("legacy")])
    async with runner:
        result = await run(runner, agent("legacy__*"), [[["legacy__counted", {}]]])
    assert not events(result, EventType.MCP_SERVER_UNAVAILABLE)
    skipped = {e.payload["tool"]: e.payload["reason"] for e in events(result, EventType.MCP_TOOL_SKIPPED)}
    assert skipped["echo"] == "it cannot be offered as a tool: ValueError"
    assert (await results(runner, result))["t0-0"].content == "n is 1"


async def test_round_6_pre_review_siblings_hold():
    """Round 6 pre-review: an argument error quoting a server schema was labelled the SDK's own
    trusted text; an invalid output schema was offered, then failed every call; data that is not
    base64 failed a whole result."""
    runner = runner_for([server("legacy", extra=("--odd-listing",))])
    async with runner:
        result = await run(runner, agent("legacy__*"), [[["legacy__hostile_required", {}]], [["legacy__bad_padding", {}]]])
    got = await results(runner, result)
    refused = got["t0-0"]
    assert refused.is_error and "IGNORE ALL PRIOR INSTRUCTIONS" in refused.content, refused.content
    assert (refused.provenance.origin, refused.provenance.trust_zone) == (Origin.MCP_RESOURCE, TrustZone.UNTRUSTED)
    assert refused.provenance.instruction_authority is InstructionAuthority.DATA_ONLY
    skipped = {e.payload["tool"]: e.payload["reason"] for e in events(result, EventType.MCP_TOOL_SKIPPED)}
    assert skipped["bad_output"].startswith("its output schema is invalid"), skipped["bad_output"]
    assert got["t1-0"].content == "a picture:\n[image content omitted: image/png, data that is not valid base64]"


async def test_a_stalled_mcp_loop_is_reported_not_silent(monkeypatch):
    """Round 6 pre-review: a start the MCP loop did not answer in time was read as a closed
    Runner, so the run lost the server's tools with no event."""
    import agentsdk.mcp as mcp

    runner = runner_for([server("legacy")])

    async def stalled(coroutine, timeout):
        coroutine.close()
        raise mcp._Closed(f"the mcp loop did not answer within {timeout} s")

    monkeypatch.setattr(runner._mcp._engine, "submit", stalled)
    result = await run(runner, agent("legacy__*"), [[["legacy__echo", {"text": "x"}]]])
    assert [e.payload for e in events(result, EventType.MCP_SERVER_UNAVAILABLE)] == [
        {"alias": "legacy", "error": "McpLoopUnanswered"}]
    monkeypatch.undo()
    await runner.aclose()


async def test_a_server_that_stays_down_is_reported_to_every_run_once():
    """Round 5 review: inside the retry interval, and after the restart-once rule, a run was told
    nothing of a server it named. Whatever keeps it down, each run that asks is told, once."""
    missing = StdioServer(alias="missing", command=str(REPO / "no-such-program.exe"), approval="never")
    runner = runner_for([missing])
    async with runner:
        # Two turns each: the call to a tool that is not there keeps the run going.
        first = await run(runner, agent("missing__*"), [[["missing__x", {}]], [["missing__x", {}]]])
        second = await run(runner, agent("missing__*"), [[["missing__x", {}]]])  # within the retry interval
    reported = [[e.payload for e in events(result, EventType.MCP_SERVER_UNAVAILABLE)] for result in (first, second)]
    assert reported[0] == reported[1] and len(reported[0]) == 1 and reported[0][0]["alias"] == "missing", reported

    runner = runner_for([server("legacy")])
    async with runner:
        await run(runner, agent("legacy__*"), [[["legacy__die", {}]], [["legacy__die", {}]]])
        after = await run(runner, agent("legacy__*"), [[["legacy__echo", {"text": "x"}]]])
    assert [e.payload for e in events(after, EventType.MCP_SERVER_UNAVAILABLE)] == [
        {"alias": "legacy", "error": "ServerExited"}]


async def test_every_call_in_flight_when_its_server_exits_is_told_it_exited(tmp_path):
    """Round 4 caveat, re-measured: with ten calls in flight, a server that exits failed none of
    them as exited; each waited its call_timeout. Each fails at once now, as exited."""
    import signal

    pids = tmp_path / "pids"
    runner = runner_for([server("legacy", pids=pids, call_timeout=60)])
    async with runner:
        await run(runner, agent("legacy__*"), [[["legacy__echo", {"text": "up"}]]])
        calls = [asyncio.create_task(run(runner, agent("legacy__*"), [[["legacy__slow", {"seconds": 30}]]]))
                 for _ in range(10)]
        await asyncio.sleep(2)
        os.kill(pids_in(pids)[0], signal.SIGTERM)
        started = time.monotonic()
        done = await asyncio.wait_for(asyncio.gather(*calls), 30)
        waited = time.monotonic() - started
    contents = [r.content for result in done for r in (await results(runner, result)).values()]
    assert len(contents) == 10 and all("mcp server legacy exited" in c for c in contents), contents
    assert waited < 10, waited


async def test_aclose_returns_once_the_mcp_thread_has_ended():
    """Round 4 caveat: aclose resolved before the engine thread had exited."""
    import threading

    runner = runner_for([server("legacy")])
    async with runner:
        await run(runner, agent("legacy__echo"), [[["legacy__echo", {"text": "x"}]]])
        assert runner._mcp._engine._thread.is_alive()
    assert not runner._mcp._engine._thread.is_alive()
    assert runner._mcp._engine._thread not in threading.enumerate()


async def test_a_large_honest_listing_is_checked_whole_within_the_deadline():
    """Round 5 review: one process checked a whole listing within 10 s, so an honest server of
    about 45k properties was refused. Checked a part at a time, side by side, every tool stays."""
    model = Turns()
    runner = runner_for([server("legacy", extra=("--wide-listing", "600x120"))], model=model)
    async with runner:
        result = await run(runner, agent("legacy__*"), [[["legacy__wide599", {}]]])
    assert not events(result, EventType.MCP_SERVER_UNAVAILABLE)
    assert not [e for e in events(result, EventType.MCP_TOOL_SKIPPED) if e.payload["tool"].startswith("wide")]
    assert (await results(runner, result))["t0-0"].content == "accepted"


def test_a_listings_chunks_are_checked_side_by_side(monkeypatch):
    """Round 6: each chunk is its own process within the one deadline, so ten take the time of
    one, and a hostile listing costs a start one deadline, never ten in a row."""
    import agentsdk.mcp as mcp

    def one_second(request):
        time.sleep(1)
        return {"problems": [None] * len(request["check"])}

    monkeypatch.setattr(mcp, "_isolated_or_raise", one_second)
    started = time.monotonic()
    assert mcp._check_listing([{"type": "object"}] * mcp._MAX_TOOLS) == [None] * mcp._MAX_TOOLS
    assert time.monotonic() - started < 3, time.monotonic() - started


def test_a_part_of_a_listing_that_cannot_be_checked_skips_only_its_own_tools(monkeypatch):
    """Round 6: a chunk past the deadline refuses its own schemas, never another chunk's, and
    never passes one."""
    import agentsdk.mcp as mcp

    real = mcp._isolated_or_raise

    def one_chunk_stalls(request):
        if any(s.get("stall") for s in request["check"]):
            raise mcp._Unchecked(f"it could not be checked within {mcp._VALIDATION_SECONDS} s")
        return real(request)

    monkeypatch.setattr(mcp, "_isolated_or_raise", one_chunk_stalls)
    size = mcp._CHECK_CHUNK
    schemas = [{"type": "object"} for _ in range(5 * size)]
    schemas[2 * size + 7]["stall"] = True
    problems = mcp._check_listing(schemas)
    assert len(problems) == 5 * size
    assert all(p is None for p in problems[:2 * size] + problems[3 * size:])
    assert all("could not be checked" in p for p in problems[2 * size:3 * size])


@pytest.mark.parametrize("kind", ["modern", "legacy"])
async def test_aclose_leaves_no_server_process_or_child(tmp_path, kind):
    pids = tmp_path / "pids"
    runner = runner_for([server(kind, pids=pids)])
    async with runner:
        await run(runner, agent(f"{kind}__spawn"), [[[f"{kind}__spawn", {}]]])
    started = pids_in(pids)
    assert len(started) == 2 and gone(started), started


def test_no_server_process_outlives_a_loop_closed_without_aclose(tmp_path):
    """NFR-39's second path: the event loop ends, its connection tasks are cancelled."""
    pids = tmp_path / "pids"
    script = (
        "import asyncio, json, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from tests.test_mcp_client import runner_for, server, run, agent\n"
        "import agentsdk.mcp, tests.test_mcp_client as fixture\n"
        "agentsdk.mcp._read_catalog = fixture._every_listed_version_accepted  # as the autouse fixture\n"
        "async def main():\n"
        "    runner = runner_for([server('modern', pids=sys.argv[2])])\n"
        "    await run(runner, agent('modern__spawn'), [[['modern__spawn', {}]]])\n"
        "asyncio.run(main())\n"
    )
    done = subprocess.run([sys.executable, "-c", script, str(REPO), str(pids)], cwd=REPO, capture_output=True,
                          text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    started = pids_in(pids)
    assert len(started) == 2 and gone(started), started


# =================================================================================================
# NFR-37: the manifest names, and no value goes anywhere
# =================================================================================================


async def test_the_manifest_names_each_server_and_no_secret_value_is_stored():
    runner = runner_for([server("modern", env={"FIXTURE_SECRET": SECRET}, deny=("die",))], "postgres")
    async with runner:
        result = await run(runner, agent("modern__has_secret"), [[["modern__has_secret", {}]]])
    assert (await results(runner, result))["t0-0"].content == "yes", "the value reached the server"
    with psycopg.connect(DSN) as conn:
        [manifest] = conn.execute("SELECT mcp_servers, tools_sent FROM execution_manifests WHERE run_id = %s",
                                  (result.run_id,)).fetchall()
        stored = [json.dumps(r, default=str) for table in ("run_events", "messages", "execution_manifests")
                  for r in conn.execute(f"SELECT * FROM {table} WHERE run_id = %s", (result.run_id,)).fetchall()]
    [record] = manifest[0]
    assert record["alias"] == "modern" and record["env"] == ["FIXTURE_SECRET", "FIXTURE_SITE"]
    assert record["deny"] == ["die"] and record["command"] == PYTHON and str(FIXTURE) in record["args"]
    assert "modern__has_secret" in {t["name"] for t in manifest[1]}
    assert not any(SECRET in row for row in stored)


async def test_a_run_with_no_server_records_an_empty_list():
    runner = runner_for([], "postgres")
    result = await run(runner, agent(), [])
    with psycopg.connect(DSN) as conn:
        [[servers]] = conn.execute("SELECT mcp_servers FROM execution_manifests WHERE run_id = %s",
                                   (result.run_id,)).fetchall()
    assert servers == []


# =================================================================================================
# NFR-34: the extra; NFR-38: runs without MCP are unchanged
# =================================================================================================


def test_without_the_extra_the_sdk_runs_and_a_server_names_the_extra():
    script = (
        "import sys, asyncio\n"
        "sys.modules['mcp'] = None\n"
        "import agentsdk\n"
        "from agentsdk import Runner, AgentSpec, RunConfig\n"
        "from agentsdk.mcp import StdioServer\n"
        "from agentsdk.model import ModelResponse, StopReason, Usage\n"
        "from agentsdk.primitives import Message, Role\n"
        "class M:\n"
        "    async def send(self, r):\n"
        "        return ModelResponse(message=Message(role=Role.ASSISTANT, content='ok'),"
        " stop_reason=StopReason.END_TURN, usage=Usage(1, 1, 2))\n"
        "r = asyncio.run(Runner({'m': M()}).run(AgentSpec(id='a', instructions='i', preferred_model='m:x'), 'go',"
        " RunConfig(tenant_id='t', project_id='p')))\n"
        "print(r.status.value)\n"
        "try:\n"
        "    Runner({'m': M()}, mcp_servers=[StdioServer(alias='s', command='x')])\n"
        "except ImportError as exc:\n"
        "    print('refused:', exc)\n"
        # AC-90 (M34): the MCP methods raise UnknownMcpServer on a Runner with no servers.
        "from agentsdk import UnknownMcpServer\n"
        "from agentsdk.identity import PrincipalContext\n"
        "try:\n"
        "    asyncio.run(Runner({'m': M()}).accept_tools('t', 'p', 's', ['0' * 64],"
        " principal=PrincipalContext(agent_principal='a', user_principal='u')))\n"
        "except UnknownMcpServer:\n"
        "    print('accept_tools: UnknownMcpServer')\n"
    )
    done = subprocess.run([sys.executable, "-c", script], cwd=REPO, env=dict(os.environ, PYTHONPATH=str(REPO)),
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.splitlines()[0] == "completed"
    assert "refused:" in done.stdout and "agentsdk[mcp]" in done.stdout
    assert "accept_tools: UnknownMcpServer" in done.stdout


@pytest.mark.parametrize("store", STORES)
async def test_a_run_with_no_server_equals_the_committed_fixture(store):
    import importlib.util

    spec = importlib.util.spec_from_file_location("nfr29", REPO / "tests" / "test_interruption_record.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.TENANT = TENANT + "-nfr38"  # under this file's cleanup, not M29's
    expected = json.loads(module.FIXTURE.read_text(encoding="utf-8"))
    assert await module._ungated_record(store, None) == expected


# =================================================================================================
# FR-126: the example
# =================================================================================================


async def test_example_27_holds_on_postgres():
    import importlib.util

    spec = importlib.util.spec_from_file_location("example_27", REPO / "scripts" / "27_mcp.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.TENANT = TENANT  # under this file's cleanup
    checks, _ = await module.offline("postgres")
    assert [label for label, passed in checks if not passed] == [] and len(checks) == 7


def test_example_27_runs_offline():
    done = subprocess.run([sys.executable, str(REPO / "scripts" / "27_mcp.py"), "--offline"],
                          capture_output=True, text=True, timeout=240, cwd=REPO)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "[FAIL]" not in done.stdout and "[PASS]" in done.stdout, done.stdout
