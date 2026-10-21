"""M34 gate: the catalog, the approval policy and a changed tool (FR-127 to FR-129, AC-91,
NFR-35, NFR-36, NFR-38).

No MCP tool runs unless its current version -- its `tool_hash` -- is accepted for the run's
tenant and project. A tool seen for the first time, or changed, is blocked and named; accepting
it makes it usable; the catalog is read again on a list-changed notification of either era,
when a listing's `ttlMs` has passed, and when a listing is older than 60 seconds, whatever its
`ttlMs`; and a
standing decision applies only to the hash it was made for.

Against real local stdio servers of both eras, in a catalog mode whose tools come from a state
file each test rewrites. The catalog's clock is replaced, so no test waits on time.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
import uuid

import psycopg
import pytest

from agentsdk import RunConfig, Runner, RunStatus, ToolBlocked, ToolError, UnknownMcpServer, UnknownToolVersion
from agentsdk.events import EventType
from agentsdk.identity import PrincipalContext
from agentsdk.mcp import McpAcceptance, StdioServer, tool_hash
from agentsdk.primitives import Origin, TrustZone
from agentsdk.tools import ApprovalPolicy, RiskClass
from tests.test_mcp_client import (DSN, FIXTURE, PACKAGES, PYTHON, STORES, Turns, agent, events, results, run,
                                   runner_for)

pytestmark = pytest.mark.skipif(__import__("sys").platform != "win32", reason="NFR-39 is measured on Windows (D11)")

PROJECT = "p-m34"
OWNER = PrincipalContext(agent_principal="catalog-ui", user_principal="owner")
DEPLOYER = PrincipalContext(agent_principal="deploy", service_principal="deployer")
ECHO = {"name": "echo", "description": "Echo the text back.",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}}
BLOCKED = EventType.MCP_TOOL_BLOCKED


@pytest.fixture(autouse=True)
def remove_what_the_test_wrote():
    yield
    if DSN:
        with psycopg.connect(DSN, autocommit=True) as conn:
            ids = [r[0] for r in conn.execute("SELECT run_id FROM runs WHERE tenant_id LIKE 'SYN-m34-%'")]
            conn.execute("DELETE FROM interruption_decisions WHERE interruption_id IN (SELECT interruption_id"
                         " FROM interruptions WHERE run_id = ANY(%s))", (ids,))
            conn.execute("DELETE FROM interruptions WHERE run_id = ANY(%s)", (ids,))
            conn.execute("DELETE FROM standing_decisions WHERE run_id = ANY(%s)", (ids,))
            for table in ("run_events", "messages", "execution_manifests"):
                conn.execute(f"DELETE FROM {table} WHERE run_id = ANY(%s)", (ids,))
            conn.execute("DELETE FROM runs WHERE run_id = ANY(%s)", (ids,))
            # Catalog rows carry a tenancy, not a run: each test's tenancy is its own.
            conn.execute("DELETE FROM mcp_tool_acceptances WHERE tenant_id LIKE 'SYN-m34-%'")
            conn.execute("DELETE FROM mcp_tool_versions WHERE tenant_id LIKE 'SYN-m34-%'")


def tenancy() -> str:
    return f"SYN-m34-{uuid.uuid4().hex[:10]}"


def state_file(tmp_path, tools, ttl_ms=None, name="state.json"):
    path = tmp_path / name
    path.write_text(json.dumps({"tools": tools, **({"ttlMs": ttl_ms} if ttl_ms is not None else {})}), encoding="utf-8")
    return path


def catalog_server(kind, state, reads=None, alias=None, **fields):
    args = [str(FIXTURE), kind, "--catalog", str(state), *(["--reads", str(reads)] if reads else [])]
    fields.setdefault("approval", "never")
    fields.setdefault("connect_timeout", 120)
    return StdioServer(alias=alias or kind, command=PYTHON, args=tuple(args), env=dict(PACKAGES), **fields)


class Clock:
    """The catalog's clock, replaced: it moves only when a test moves it."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def frozen(runner) -> Clock:
    clock = Clock()
    runner._mcp.clock = clock
    return clock


def blocked(result) -> dict[str, dict]:
    return {e.payload["tool"]: e.payload for e in events(result, BLOCKED)}


async def accept_all(runner, result, tenant, alias):
    hashes = [p["tool_hash"] for p in blocked(result).values() if p.get("tool_hash")]
    return await runner.accept_tools(tenant, PROJECT, alias, hashes, principal=OWNER)


def reads(path) -> int:
    return len(path.read_text(encoding="utf-8").split()) if path.exists() else 0


def offered(request) -> set[str]:
    return {t["function"]["name"] for t in request.tools}


# =================================================================================================
# FR-127: first sight, changes and acceptance
# =================================================================================================


@pytest.mark.parametrize("store", STORES)
@pytest.mark.parametrize("kind", ["legacy", "modern"])
async def test_a_tool_seen_for_the_first_time_is_unavailable_until_accepted(store, kind, tmp_path):
    tenant, model = tenancy(), Turns()
    runner = runner_for([catalog_server(kind, state_file(tmp_path, [ECHO]))], store, model=model)
    async with runner:
        frozen(runner)
        first = await run(runner, agent(f"{kind}__*"), [[[f"{kind}__echo", {"text": "x"}]]], tenant, PROJECT)
        seen = blocked(first)["echo"]
        assert seen["previous_hash"] is None and seen["changed"] is None, seen
        assert f"{kind}__echo" not in offered(model.requests[0])
        assert (await results(runner, first, tenant, PROJECT))["t0-0"].content.startswith("ToolNotFound")
        [row] = await runner.accept_tools(tenant, PROJECT, kind, [seen["tool_hash"]], principal=OWNER)
        assert (row.tenant_id, row.project_id, row.alias, row.tool_name, row.tool_hash) == (
            tenant, PROJECT, kind, "echo", seen["tool_hash"])
        assert row.principal == OWNER
        second = await run(runner, agent(f"{kind}__*"), [[[f"{kind}__echo", {"text": "x"}]]], tenant, PROJECT)
    assert "echo" not in blocked(second)
    assert (await results(runner, second, tenant, PROJECT))["t0-0"].content.startswith("called")


PARTS = {
    "description": lambda tool: {**tool, "description": "Echo the text back, loudly."},
    "input_schema": lambda tool: {**tool, "inputSchema": {"type": "object", "properties": {
        "text": {"type": "string"}, "loud": {"type": "boolean"}}}},
    "output_schema": lambda tool: {**tool, "outputSchema": {"type": "object"}},
    "annotations": lambda tool: {**tool, "annotations": {"readOnlyHint": True}},
}


@pytest.mark.parametrize("store", STORES)
@pytest.mark.parametrize("part", PARTS)
async def test_a_change_to_each_part_blocks_the_tool_and_names_that_part(store, part, tmp_path):
    tenant, model = tenancy(), Turns()
    runner = runner_for([catalog_server("legacy", state_file(tmp_path, [ECHO]))], store, model=model)
    async with runner:
        clock = frozen(runner)
        first = await run(runner, agent("legacy__*"), [], tenant, PROJECT)
        [accepted] = [a for a in await accept_all(runner, first, tenant, "legacy") if a.tool_name == "echo"]
        used = await run(runner, agent("legacy__*"), [[["legacy__echo", {"text": "x"}]]], tenant, PROJECT)
        assert (await results(runner, used, tenant, PROJECT))["t0-0"].content.startswith("called"), "usable first"
        state_file(tmp_path, [PARTS[part](ECHO)])
        clock.now += 61  # past the 60 s a listing with no ttlMs is kept: the next turn reads it
        second = await run(runner, agent("legacy__*"), [[["legacy__echo", {"text": "x"}]]], tenant, PROJECT)
    change = blocked(second)["echo"]
    assert change["previous_hash"] == accepted.tool_hash and change["tool_hash"] != accepted.tool_hash
    assert change["changed"] == [part], change
    assert "legacy__echo" not in offered(model.requests[-2])


@pytest.mark.parametrize("kind", ["legacy", "modern"])
async def test_a_call_in_the_same_turn_as_the_change_is_refused_tool_blocked(kind, tmp_path):
    """AC-91: a model's call to a tool blocked since it saw it is refused ToolBlocked -- here the
    change lands between two calls of one turn, so the model saw the old version."""
    tenant = tenancy()
    runner = runner_for([catalog_server(kind, state_file(tmp_path, [ECHO]))], "memory")
    async with runner:
        frozen(runner)
        first = await run(runner, agent(f"{kind}__*"), [], tenant, PROJECT)
        await accept_all(runner, first, tenant, kind)
        state_file(tmp_path, [PARTS["description"](ECHO)])
        second = await run(runner, agent(f"{kind}__*"), [
            [[f"{kind}__notify", {}], [f"{kind}__echo", {"text": "x"}]]], tenant, PROJECT)
    got = await results(runner, second, tenant, PROJECT)
    assert got["t0-0"].content == "notified"
    assert got["t0-1"].is_error
    assert got["t0-1"].content == f"ToolBlocked: {kind}__echo is not accepted in its current version", got["t0-1"].content
    [called] = [e for e in events(second, EventType.TOOL_CALLED) if e.payload["tool_call_id"] == "t0-1"]
    assert called.payload["started_at"] is None, "refused at step 1, never reaching step 6"


@pytest.mark.parametrize("kind", ["legacy", "modern"])
async def test_a_call_reaches_the_server_only_under_its_accepted_hash(kind, tmp_path):
    """NFR-36 where a call leaves for the server, beneath step 1: a tool object the run was handed
    before the change, called directly, is refused there too."""
    from agentsdk.tools import ToolRegistry

    tenant = tenancy()
    runner = runner_for([catalog_server(kind, state_file(tmp_path, [ECHO]))], "memory")
    async with runner:
        frozen(runner)
        first = await run(runner, agent(f"{kind}__*"), [], tenant, PROJECT)
        await accept_all(runner, first, tenant, kind)
        view = runner._mcp.view(ToolRegistry(), (f"{kind}__*",), tenant, PROJECT)
        await view.prepare()
        echo, notify = view.get(f"{kind}__echo"), view.get(f"{kind}__notify")
        assert (await echo.fn(text="before")).content.startswith("called")
        state_file(tmp_path, [PARTS["description"](ECHO)])
        await notify.fn()
        deadline = time.monotonic() + 20
        while True:  # the notification's read lands in the background
            try:
                await echo.fn(text="after")
            except ToolBlocked as exc:
                assert str(exc) == f"{kind}__echo is not accepted in its current version"
                break
            assert time.monotonic() < deadline, "the changed tool still reached the server"
            await asyncio.sleep(0.05)


async def test_two_tools_with_the_same_content_are_two_versions(tmp_path):
    """The hash covers the name (DECISION-383e6df2): accepting read_file never accepts a
    delete_file listed with the same description and schema."""
    same = {"description": "", "inputSchema": {"type": "object"}}
    assert tool_hash("read_file", "", {"type": "object"}) != tool_hash("delete_file", "", {"type": "object"})
    tenant = tenancy()
    runner = runner_for([catalog_server("legacy", state_file(tmp_path, [{"name": "read_file", **same},
                                                                        {"name": "delete_file", **same}]))], "memory")
    async with runner:
        frozen(runner)
        first = await run(runner, agent("legacy__*"), [], tenant, PROJECT)
        seen = blocked(first)
        assert seen["read_file"]["tool_hash"] != seen["delete_file"]["tool_hash"]
        await runner.accept_tools(tenant, PROJECT, "legacy", [seen["read_file"]["tool_hash"]], principal=OWNER)
        second = await run(runner, agent("legacy__*"), [[["legacy__delete_file", {}]]], tenant, PROJECT)
    assert "delete_file" in blocked(second) and "read_file" not in blocked(second)
    assert (await results(runner, second, tenant, PROJECT))["t0-0"].content.startswith("ToolNotFound")


async def test_a_listing_stale_before_a_call_is_read_first(tmp_path):
    """FR-127: before a call to the server, a listing older than 60 seconds (no ttlMs) is read
    again. Here the clock passes 60 s between two calls of one turn, the server has changed the
    tool meanwhile without saying so, and the call is refused at step 1 rather than sent."""
    tenant = tenancy()
    clock = Clock()

    def tick() -> str:
        clock.now += 61
        return "ticked"

    from agentsdk.tools import Tool, ToolSpec
    ticker = Tool(spec=ToolSpec(name="tick", description="Move the clock.", input_schema={"type": "object"}), fn=tick)
    runner = runner_for([catalog_server("legacy", state_file(tmp_path, [ECHO]))], "memory", tools=[ticker])
    async with runner:
        runner._mcp.clock = clock
        first = await run(runner, agent("legacy__*", "tick"), [], tenant, PROJECT)
        await accept_all(runner, first, tenant, "legacy")
        state_file(tmp_path, [PARTS["description"](ECHO)])  # changed, and not announced
        second = await run(runner, agent("legacy__*", "tick"), [[["tick", {}], ["legacy__echo", {"text": "x"}]]],
                           tenant, PROJECT)
    got = await results(runner, second, tenant, PROJECT)
    assert got["t0-0"].content == "ticked"
    assert got["t0-1"].content == "ToolBlocked: legacy__echo is not accepted in its current version", got["t0-1"].content
    [called] = [e for e in events(second, EventType.TOOL_CALLED) if e.payload["tool_call_id"] == "t0-1"]
    assert called.payload["started_at"] is None, "refused at step 1, never sent"


def test_tool_blocked_is_an_exported_tool_error():
    assert issubclass(ToolBlocked, ToolError)
    assert issubclass(UnknownMcpServer, LookupError) and issubclass(UnknownToolVersion, LookupError)


@pytest.mark.parametrize("store", STORES)
async def test_accept_tools_refuses_in_the_stated_order(store, tmp_path):
    tenant, other = tenancy(), tenancy()
    runner = runner_for([catalog_server("legacy", state_file(tmp_path, [ECHO]))], store)
    async with runner:
        frozen(runner)
        first = await run(runner, agent("legacy__*"), [], tenant, PROJECT)
        known = blocked(first)["echo"]["tool_hash"]
        agent_only = PrincipalContext(agent_principal="an-agent")
        # 1. a principal naming no user or service, before anything else is looked at
        with pytest.raises(ValueError):
            await runner.accept_tools(tenant, PROJECT, "nope", ["0" * 64], principal=agent_only)
        # 2. an alias the Runner was not given, before the hash is looked at
        with pytest.raises(UnknownMcpServer):
            await runner.accept_tools(tenant, PROJECT, "nope", ["0" * 64], principal=OWNER)
        # 3. a hash with no version row in that tenancy -- exactly as for one of another tenancy
        for tenancy_, hashes in ((tenant, ["0" * 64]), (other, [known])):
            with pytest.raises(UnknownToolVersion):
                await runner.accept_tools(tenancy_, PROJECT, "legacy", hashes, principal=OWNER)
        [made] = await runner.accept_tools(tenant, PROJECT, "legacy", [known], principal=OWNER)
        [again] = await runner.accept_tools(tenant, PROJECT, "legacy", [known], principal=DEPLOYER)
    assert again == made, "a hash already accepted is returned unchanged"


async def test_a_runner_with_no_servers_raises_unknown_mcp_server():
    runner = Runner({"m": Turns()})
    with pytest.raises(UnknownMcpServer):
        await runner.accept_tools("t", PROJECT, "legacy", ["0" * 64], principal=OWNER)


@pytest.mark.parametrize("store", STORES)
async def test_rows_read_back_field_for_field_and_are_never_rewritten(store, tmp_path):
    tenant = tenancy()
    runner = runner_for([catalog_server("legacy", state_file(tmp_path, [ECHO]))], store)
    async with runner:
        frozen(runner)
        first = await run(runner, agent("legacy__*"), [], tenant, PROJECT)
        [made] = [a for a in await accept_all(runner, first, tenant, "legacy") if a.tool_name == "echo"]
        await run(runner, agent("legacy__*"), [], tenant, PROJECT)  # seen again: its version row stays
    catalog = runner._catalog_store()
    version = await asyncio.to_thread(catalog.version, tenant, PROJECT, "legacy", made.tool_hash)
    assert (version.tenant_id, version.project_id, version.alias, version.tool_name, version.tool_hash, version.kind) == (
        tenant, PROJECT, "legacy", "echo", made.tool_hash, "tool")
    assert version.entry == {"name": "echo", "description": ECHO["description"], "inputSchema": ECHO["inputSchema"],
                             "outputSchema": None, "annotations": None}
    assert tool_hash("echo", ECHO["description"], ECHO["inputSchema"]) == made.tool_hash
    accepted = await asyncio.to_thread(catalog.acceptances, tenant, PROJECT, "legacy", [made.tool_hash])
    assert accepted == {made.tool_hash: made}


def test_no_catalog_statement_or_method_updates_or_deletes_a_row():
    from agentsdk.catalog import InMemoryCatalogStore
    from agentsdk.postgres import PostgresCatalogStore

    postgres = inspect.getsource(PostgresCatalogStore).upper()
    assert "UPDATE " not in postgres and "DELETE " not in postgres and "ON CONFLICT DO UPDATE" not in postgres
    memory = inspect.getsource(InMemoryCatalogStore)
    for mutation in ("del ", ".pop(", ".remove(", ".clear(", ".discard("):
        assert mutation not in memory, mutation


@pytest.mark.parametrize("store", STORES)
async def test_another_tenancys_acceptance_never_makes_a_tool_usable(store, tmp_path):
    tenant, other = tenancy(), tenancy()
    runner = runner_for([catalog_server("legacy", state_file(tmp_path, [ECHO]))], store)
    async with runner:
        frozen(runner)
        first = await run(runner, agent("legacy__*"), [], tenant, PROJECT)
        await accept_all(runner, first, tenant, "legacy")
        theirs = await run(runner, agent("legacy__*"), [[["legacy__echo", {"text": "x"}]]], other, PROJECT)
    assert "echo" in blocked(theirs)
    assert (await results(runner, theirs, other, PROJECT))["t0-0"].content.startswith("ToolNotFound")


async def test_a_failure_to_read_acceptances_leaves_the_tool_unavailable(tmp_path):
    """NFR-36: the store fails closed."""
    tenant = tenancy()
    runner = runner_for([catalog_server("legacy", state_file(tmp_path, [ECHO]))], "memory")
    async with runner:
        frozen(runner)
        first = await run(runner, agent("legacy__*"), [], tenant, PROJECT)
        await accept_all(runner, first, tenant, "legacy")
        catalog = runner._catalog_store()

        def broken(*args, **kwargs):
            raise ConnectionError("the store is down")

        catalog.acceptances = broken
        # A new tenancy's connection must read acceptances, and cannot.
        other = tenancy()
        result = await run(runner, agent("legacy__*"), [[["legacy__echo", {"text": "x"}]]], other, PROJECT)
    assert result.status is RunStatus.COMPLETED
    assert blocked(result)["echo"]["error"] == "ConnectionError", blocked(result)["echo"]
    assert (await results(runner, result, other, PROJECT))["t0-0"].content.startswith("ToolNotFound")


@pytest.mark.parametrize("store", STORES)
async def test_a_servers_accept_records_its_rows_when_its_tenancy_first_lists_the_tool(store, tmp_path):
    tenant = tenancy()
    known = tool_hash("echo", ECHO["description"], ECHO["inputSchema"])
    server = catalog_server("legacy", state_file(tmp_path, [ECHO]),
                            accept=(McpAcceptance(tenant, PROJECT, DEPLOYER, (known,)),))
    runner = runner_for([server], store)
    async with runner:
        frozen(runner)
        result = await run(runner, agent("legacy__*"), [[["legacy__echo", {"text": "x"}]]], tenant, PROJECT)
    assert "echo" not in blocked(result) and "notify" in blocked(result), "only the hashes it names"
    assert (await results(runner, result, tenant, PROJECT))["t0-0"].content.startswith("called")
    rows = await asyncio.to_thread(runner._catalog_store().acceptances, tenant, PROJECT, "legacy", [known])
    assert rows[known].principal == DEPLOYER


@pytest.mark.parametrize("accept, reason", [
    ((McpAcceptance("t", PROJECT, OWNER, ("0" * 64,)),), "service principal"),
    ((McpAcceptance("t", PROJECT, DEPLOYER, ("0" * 64,)), McpAcceptance("t", PROJECT, DEPLOYER, ("1" * 64,))),
     "one per tenancy"),
    ((McpAcceptance("t", PROJECT, DEPLOYER, ("not-a-hash",)),), "SHA-256"),
])
def test_an_invalid_accept_is_refused_when_built(accept, reason):
    with pytest.raises(ValueError, match=reason):
        StdioServer(alias="ok", command=PYTHON, accept=accept)


# =================================================================================================
# FR-127: when the catalog is read
# =================================================================================================


async def wait_for_reads(path, count, timeout=20.0):
    deadline = time.monotonic() + timeout
    while reads(path) < count:
        assert time.monotonic() < deadline, f"{reads(path)} reads, not {count}"
        await asyncio.sleep(0.05)


@pytest.mark.parametrize("kind", ["legacy", "modern"])
async def test_a_list_changed_notification_causes_a_read(kind, tmp_path):
    tenant, log = tenancy(), tmp_path / "reads"
    runner = runner_for([catalog_server(kind, state_file(tmp_path, [ECHO]), reads=log)], "memory")
    async with runner:
        frozen(runner)
        first = await run(runner, agent(f"{kind}__*"), [], tenant, PROJECT)
        await accept_all(runner, first, tenant, kind)
        await run(runner, agent(f"{kind}__*"), [], tenant, PROJECT)
        before = reads(log)
        await run(runner, agent(f"{kind}__*"), [[[f"{kind}__notify", {}]]], tenant, PROJECT)
        await wait_for_reads(log, before + 1)


async def test_a_passed_ttl_causes_a_read_and_an_unpassed_one_does_not(tmp_path):
    """ttlMs is a 2026-07-28 field, so this is the modern era's."""
    tenant, log = tenancy(), tmp_path / "reads"
    runner = runner_for([catalog_server("modern", state_file(tmp_path, [ECHO], ttl_ms=30_000), reads=log)], "memory")
    async with runner:
        clock = frozen(runner)
        await run(runner, agent("modern__*"), [], tenant, PROJECT)
        assert reads(log) == 1
        clock.now += 29
        await run(runner, agent("modern__*"), [], tenant, PROJECT)
        assert reads(log) == 1, "within ttlMs, and well within 60 s"
        clock.now += 2
        await run(runner, agent("modern__*"), [], tenant, PROJECT)
        assert reads(log) == 2, "ttlMs passed"


async def test_a_ttl_longer_than_60_seconds_does_not_keep_a_listing_past_60_seconds(tmp_path):
    """M34 round 2 review: a server passing ttlMs 600000 kept a quietly changed tool running
    61 s later. The owner capped it: no server chooses how long its own listing is trusted."""
    tenant, log = tenancy(), tmp_path / "reads"
    runner = runner_for([catalog_server("modern", state_file(tmp_path, [ECHO], ttl_ms=600_000), reads=log)],
                        "memory")
    async with runner:
        clock = frozen(runner)
        await run(runner, agent("modern__*"), [], tenant, PROJECT)
        clock.now += 59
        await run(runner, agent("modern__*"), [], tenant, PROJECT)
        assert reads(log) == 1, "well within ttlMs, and within 60 s"
        clock.now += 2
        await run(runner, agent("modern__*"), [], tenant, PROJECT)
        assert reads(log) == 2, "within ttlMs, but past 60 s"


async def test_a_listing_with_no_ttl_is_read_again_after_60_seconds(tmp_path):
    tenant, log = tenancy(), tmp_path / "reads"
    runner = runner_for([catalog_server("legacy", state_file(tmp_path, [ECHO]), reads=log)], "memory")
    async with runner:
        clock = frozen(runner)
        await run(runner, agent("legacy__*"), [], tenant, PROJECT)
        clock.now += 59
        await run(runner, agent("legacy__*"), [], tenant, PROJECT)
        assert reads(log) == 1
        clock.now += 2
        await run(runner, agent("legacy__*"), [], tenant, PROJECT)
        assert reads(log) == 2


# =================================================================================================
# FR-128: the approval policy
# =================================================================================================


def specs_of(runner, tenant, kind="legacy"):
    async def go():
        view = runner._mcp.view(runner._registry, (f"{kind}__*",), tenant, PROJECT)
        await view.prepare()
        return {name: view.get(name).spec for name in view.names()}
    return go()


ANNOTATED = [
    {"name": "reader", "description": "Reads.", "inputSchema": {"type": "object"},
     "annotations": {"readOnlyHint": True, "idempotentHint": True}},
    {"name": "writer", "description": "Writes.", "inputSchema": {"type": "object"},
     "annotations": {"readOnlyHint": False, "destructiveHint": False}},
    {"name": "wiper", "description": "Wipes.", "inputSchema": {"type": "object"},
     "annotations": {"destructiveHint": True}},
]


async def accepted_runner(tmp_path, tools, **fields):
    tenant = tenancy()
    runner = runner_for([catalog_server("legacy", state_file(tmp_path, tools), **fields)], "memory")
    frozen(runner)
    first = await run(runner, agent("legacy__*"), [], tenant, PROJECT)
    await accept_all(runner, first, tenant, "legacy")
    return runner, tenant


@pytest.mark.parametrize("approval, expected", [
    ("require", {"reader": True, "writer": True, "wiper": True}),
    ("never", {"reader": False, "writer": False, "wiper": False}),
    ("by_annotations", {"reader": False, "writer": True, "wiper": True}),
])
async def test_each_approval_policy_maps_as_stated(approval, expected, tmp_path):
    runner, tenant = await accepted_runner(tmp_path, ANNOTATED, approval=approval, trusted=True)
    async with runner:
        specs = await specs_of(runner, tenant)
    assert {name: specs[f"legacy__{name}"].approval_policy is ApprovalPolicy.REQUIRE_APPROVAL
            for name in expected} == expected


async def test_a_trusted_servers_annotations_set_the_spec(tmp_path):
    runner, tenant = await accepted_runner(tmp_path, ANNOTATED, trusted=True)
    async with runner:
        specs = await specs_of(runner, tenant)
    reader, writer, wiper = (specs[f"legacy__{n}"] for n in ("reader", "writer", "wiper"))
    assert (reader.risk_class, reader.read_only, reader.idempotent, reader.concurrency_safe) == (
        RiskClass.READ_ONLY, True, True, True)
    assert (writer.risk_class, writer.read_only, writer.concurrency_safe) == (RiskClass.WRITE, False, False)
    assert (wiper.risk_class, wiper.read_only, wiper.concurrency_safe) == (RiskClass.DESTRUCTIVE, False, False)


async def test_an_untrusted_servers_annotations_change_nothing(tmp_path):
    runner, tenant = await accepted_runner(tmp_path, ANNOTATED)
    async with runner:
        specs = await specs_of(runner, tenant)
    for name in ("reader", "writer", "wiper"):
        spec = specs[f"legacy__{name}"]
        assert (spec.risk_class, spec.read_only, spec.idempotent, spec.concurrency_safe) == (
            RiskClass.DESTRUCTIVE, False, False, False), name


@pytest.mark.parametrize("fields", [{"approval": "by_annotations"},
                                    {"approval_overrides": {"echo": "by_annotations"}}])
def test_by_annotations_is_refused_for_an_untrusted_server(fields):
    with pytest.raises(ValueError, match="trusted"):
        StdioServer(alias="ok", command=PYTHON, **fields)


async def test_an_override_naming_no_listed_tool_makes_every_tool_ask(tmp_path):
    """The owner's choice, M34: a mistyped or stale override can never leave a tool unapproved."""
    runner, tenant = await accepted_runner(tmp_path, ANNOTATED, approval="never",
                                           approval_overrides={"wipper": "require"})
    async with runner:
        specs = await specs_of(runner, tenant)
    assert all(specs[f"legacy__{n}"].approval_policy is ApprovalPolicy.REQUIRE_APPROVAL
               for n in ("reader", "writer", "wiper"))


async def test_the_unmatched_overrides_are_named_when_the_server_connects(tmp_path):
    tenant = tenancy()
    runner = runner_for([catalog_server("legacy", state_file(tmp_path, [ECHO]),
                                        approval_overrides={"ecco": "require"})], "memory")
    async with runner:
        frozen(runner)
        result = await run(runner, agent("legacy__*"), [], tenant, PROJECT)
    [connected] = events(result, EventType.MCP_SERVER_CONNECTED)
    assert connected.payload["unmatched_overrides"] == ["ecco"]


# =================================================================================================
# FR-128: the approval request, and standing decisions bound to a hash
# =================================================================================================


async def pending(runner, tenant, run_id, count, timeout=30.0):
    deadline = time.monotonic() + timeout
    while True:
        views = await runner.interruptions(tenant, PROJECT, run_id=run_id, pending_only=True)
        if len(views) >= count:
            return sorted(views, key=lambda v: v.interruption.created_at)
        assert time.monotonic() < deadline, f"only {len(views)} of {count} pending"
        await asyncio.sleep(0.05)


@pytest.mark.parametrize("store", STORES)
async def test_the_request_shows_the_arguments_hash_and_accepted_description(store, tmp_path):
    tenant = tenancy()
    runner = runner_for([catalog_server("legacy", state_file(tmp_path, [ECHO]), approval="require")], store)
    async with runner:
        frozen(runner)
        first = await run(runner, agent("legacy__*"), [], tenant, PROJECT)
        [made] = [a for a in await accept_all(runner, first, tenant, "legacy") if a.tool_name == "echo"]
        handle = await runner.start(agent("legacy__*"), json.dumps([[["legacy__echo", {"text": "hello"}]]]),
                                    RunConfig(tenant_id=tenant, project_id=PROJECT))
        [view] = await pending(runner, tenant, handle.run_id, 1)
        request = view.interruption.request_payload
        assert request["arguments"] == {"text": "hello"}
        assert (request["alias"], request["tool_hash"], request["description"]) == (
            "legacy", made.tool_hash, ECHO["description"])
        await handle.approve(view.interruption.interruption_id, principal=OWNER)
        result = await handle.result()
    assert (await results(runner, result, tenant, PROJECT))["t0-0"].content.startswith("called")


@pytest.mark.parametrize("store", STORES)
async def test_a_standing_decision_does_not_apply_after_its_tools_hash_changes(store, tmp_path):
    """NFR-36: a sticky approval of echo's first version decides its next call of that version,
    and not a call after the change, though the changed version is accepted: that one is asked
    again. Both versions are accepted in code; the change lands when the run calls notify."""
    tenant = tenancy()
    changed = PARTS["description"](ECHO)
    hashes = (tool_hash("echo", ECHO["description"], ECHO["inputSchema"]),
              tool_hash("echo", changed["description"], changed["inputSchema"]),
              tool_hash("notify", "Announce a change.", {"type": "object"}))
    server = catalog_server("legacy", state_file(tmp_path, [ECHO]), approval="require",
                            approval_overrides={"notify": "never"},
                            accept=(McpAcceptance(tenant, PROJECT, DEPLOYER, hashes),))
    runner = runner_for([server], store)
    async with runner:
        frozen(runner)
        await run(runner, agent("legacy__*"), [], tenant, PROJECT)  # the first listing, read now
        state_file(tmp_path, [changed])  # served at the next read, which only notify causes
        turns = [[["legacy__echo", {"text": "1"}]], [["legacy__echo", {"text": "2"}], ["legacy__notify", {}]],
                 [["legacy__echo", {"text": "3"}]]]
        handle = await runner.start(agent("legacy__*"), json.dumps(turns), RunConfig(tenant_id=tenant, project_id=PROJECT))
        [one] = await pending(runner, tenant, handle.run_id, 1)
        assert one.interruption.request_payload["tool_hash"] == hashes[0]
        await handle.approve(one.interruption.interruption_id, principal=OWNER, sticky=True)
        [three] = await pending(runner, tenant, handle.run_id, 1)
        assert three.interruption.request_payload["tool_hash"] == hashes[1]
        assert three.interruption.request_payload["arguments"] == {"text": "3"}
        await handle.approve(three.interruption.interruption_id, principal=OWNER)
        result = await handle.result()
    got = await results(runner, result, tenant, PROJECT)
    assert [got[c].content.split(" ")[0] for c in ("t0-0", "t1-0", "t1-1", "t2-0")] == [
        "called", "called", "notified", "called"]
    decided = await runner.interruptions(tenant, PROJECT, run_id=result.run_id, pending_only=False)
    by_text = {v.interruption.request_payload["arguments"]["text"]: v.decision.source for v in decided}
    assert by_text == {"1": "principal", "2": "standing", "3": "principal"}, by_text


# =================================================================================================
# FR-129: the example
# =================================================================================================


@pytest.mark.parametrize("store", STORES)
async def test_example_28_shows_every_step_on_both_stores(store):
    import importlib.util

    from tests.test_mcp_client import REPO

    spec = importlib.util.spec_from_file_location("example_28", REPO / "scripts" / "28_mcp_catalog.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.TENANT = tenancy()  # under this file's cleanup
    checks, _ = await module.offline(store)
    assert [label for label, passed in checks if not passed] == [] and len(checks) == 6


# =================================================================================================
# M34 pre-review: the gate's siblings
# =================================================================================================


@pytest.mark.parametrize("store", STORES)
async def test_a_call_that_waited_for_approval_is_checked_again_as_it_leaves(store, tmp_path):
    """Pre-review D1: step 1 read a stale listing, then the call waited for its approval, and
    reached the server on what the last read said. It is checked again as it leaves, the
    listing read first when stale, however long it waited."""
    tenant, log = tenancy(), tmp_path / "reads"
    runner = runner_for([catalog_server("legacy", state_file(tmp_path, [ECHO]), reads=log, approval="require")], store)
    async with runner:
        clock = frozen(runner)
        first = await run(runner, agent("legacy__*"), [], tenant, PROJECT)
        await accept_all(runner, first, tenant, "legacy")
        handle = await runner.start(agent("legacy__*"), json.dumps([[["legacy__echo", {"text": "hello"}]]]),
                                    RunConfig(tenant_id=tenant, project_id=PROJECT))
        [view] = await pending(runner, tenant, handle.run_id, 1)
        state_file(tmp_path, [PARTS["description"](ECHO)])  # changed while the call waits, unannounced
        clock.now += 61
        before = reads(log)
        await handle.approve(view.interruption.interruption_id, principal=OWNER)
        result = await handle.result()
    got = (await results(runner, result, tenant, PROJECT))["t0-0"]
    assert got.content == "ToolBlocked: legacy__echo is not accepted in its current version", got.content
    assert reads(log) > before, "the listing was read again before the call left"


async def test_a_notification_during_a_read_is_never_lost(tmp_path, monkeypatch):
    """Pre-review D2: a read cleared the stale flag when it finished, so a list-changed
    notification that arrived while it checked schemas was lost until the next 60 s."""
    import agentsdk.mcp as mcp
    from agentsdk.tools import ToolRegistry

    tenant = tenancy()
    armed = {"on": False}
    real = mcp._Connection._schema_problems

    async def raced(self, listed):
        verdicts = await real(self, listed)
        if armed["on"]:
            armed["on"] = False  # the server changes echo, and says so, while this read checks schemas
            state_file(tmp_path, [PARTS["description"](ECHO)])
            await self.client.call_tool("notify", {})
            await asyncio.sleep(0.5)
        return verdicts

    monkeypatch.setattr(mcp._Connection, "_schema_problems", raced)
    runner = runner_for([catalog_server("legacy", state_file(tmp_path, [ECHO]))], "memory")
    async with runner:
        clock = frozen(runner)
        first = await run(runner, agent("legacy__*"), [], tenant, PROJECT)
        await accept_all(runner, first, tenant, "legacy")
        view = runner._mcp.view(ToolRegistry(), ("legacy__*",), tenant, PROJECT)
        await view.prepare()
        echo = view.get("legacy__echo")
        armed["on"], clock.now = True, clock.now + 61
        await view.blocked("legacy__echo")  # the read the notification races
        deadline = time.monotonic() + 20
        while await view.blocked("legacy__echo") is None:
            assert time.monotonic() < deadline, "the notification was lost: echo stayed usable"
            await asyncio.sleep(0.05)
        with pytest.raises(ToolBlocked):
            await echo.fn(text="x")


async def test_a_tool_whose_approval_changed_in_the_turn_is_not_run_as_offered(tmp_path):
    """Pre-review D3: a read in the turn removed the tool an override named, so every tool must
    now ask (the owner's rule), but the turn's tool object kept its old policy and ran unasked."""
    from agentsdk.tools import Tool, ToolSpec

    tenant, clock = tenancy(), Clock()

    def named(name):
        return {"name": name, "description": name + " tool", "inputSchema": {"type": "object"}}

    def tick() -> str:
        state_file(tmp_path, [named("safe")])  # the tool the override names is gone
        clock.now += 61
        return "ticked"

    ticker = Tool(spec=ToolSpec(name="tick", description="Move.", input_schema={"type": "object"}), fn=tick)
    server = catalog_server("legacy", state_file(tmp_path, [named("danger"), named("safe")]), approval="never",
                            approval_overrides={"danger": "require"})
    runner = runner_for([server], "memory", tools=[ticker])
    async with runner:
        runner._mcp.clock = clock
        first = await run(runner, agent("legacy__*", "tick"), [], tenant, PROJECT)
        await accept_all(runner, first, tenant, "legacy")
        result = await run(runner, agent("legacy__*", "tick"), [[["tick", {}], ["legacy__safe", {}]]], tenant, PROJECT)
    got = await results(runner, result, tenant, PROJECT)
    assert got["t0-1"].content == ("ToolBlocked: legacy__safe changed since it was offered; it is offered "
                                   "again next turn"), got["t0-1"].content
    [called] = [e for e in events(result, EventType.TOOL_CALLED) if e.payload["tool_call_id"] == "t0-1"]
    assert called.payload["started_at"] is None, "refused at step 1"


async def test_a_tool_whose_approval_changed_is_refused_where_the_call_leaves_too(tmp_path):
    """Pre-review D3 beneath step 1: the turn's tool object, called directly after the read that
    changed every tool's policy, is refused where the call leaves for the server."""
    from agentsdk.tools import ToolRegistry

    tenant = tenancy()

    def named(name):
        return {"name": name, "description": name + " tool", "inputSchema": {"type": "object"}}

    server = catalog_server("legacy", state_file(tmp_path, [named("danger"), named("safe")]), approval="never",
                            approval_overrides={"danger": "require"})
    runner = runner_for([server], "memory")
    async with runner:
        clock = frozen(runner)
        first = await run(runner, agent("legacy__*"), [], tenant, PROJECT)
        await accept_all(runner, first, tenant, "legacy")
        view = runner._mcp.view(ToolRegistry(), ("legacy__*",), tenant, PROJECT)
        await view.prepare()
        safe = view.get("legacy__safe")
        assert (await safe.fn()).content.startswith("called")
        state_file(tmp_path, [named("safe")])
        clock.now += 61
        with pytest.raises(ToolBlocked, match="changed since it was offered"):
            await safe.fn()


async def test_a_server_that_changes_without_end_is_bounded(tmp_path, monkeypatch):
    """Pre-review D4: every distinct version was recorded and kept, without limit. A connection
    records at most _MAX_VERSIONS; past it a tool stays blocked, naming why; and the blocked
    payloads it keeps are this listing's."""
    import agentsdk.mcp as mcp

    monkeypatch.setattr(mcp, "_MAX_VERSIONS", 4)
    tenant = tenancy()
    runner = runner_for([catalog_server("legacy", state_file(tmp_path, [ECHO]))], "memory")
    async with runner:
        clock = frozen(runner)
        for version in range(6):
            state_file(tmp_path, [{**ECHO, "description": f"version {version}"}])
            clock.now += 61
            result = await run(runner, agent("legacy__*"), [], tenant, PROJECT)
        connection = runner._mcp._engine.connections[("legacy", tenant, PROJECT)]
        assert len(connection._recorded) == 4 and len(connection._blocked) <= 2
    assert blocked(result)["echo"]["error"].startswith("VersionLimit"), blocked(result)["echo"]


async def test_idle_notifications_queue_no_events(tmp_path):
    """Pre-review D4: with no run listening, each notification's read queued every blocked tool's
    event again, without limit."""
    from agentsdk.tools import ToolRegistry

    tenant = tenancy()
    runner = runner_for([catalog_server("legacy", state_file(tmp_path, [ECHO]))], "memory")
    async with runner:
        frozen(runner)
        first = await run(runner, agent("legacy__*"), [], tenant, PROJECT)
        await accept_all(runner, first, tenant, "legacy")
        view = runner._mcp.view(ToolRegistry(), ("legacy__*",), tenant, PROJECT)
        await view.prepare()
        state_file(tmp_path, [PARTS["description"](ECHO)])  # echo blocked from the next read on
        notify = view.get("legacy__notify")
        for _ in range(20):
            await notify.fn()
            await asyncio.sleep(0.05)
        await asyncio.sleep(1)
        connection = runner._mcp._engine.connections[("legacy", tenant, PROJECT)]
        assert len(connection._unreported) <= 2, len(connection._unreported)


async def test_a_failing_store_is_reported_once_a_run(tmp_path):
    """Pre-review caveat 1: each turn reported every tool again while the store failed."""
    tenant = tenancy()
    runner = runner_for([catalog_server("legacy", state_file(tmp_path, [ECHO]))], "memory")
    async with runner:
        frozen(runner)

        def broken(*args, **kwargs):
            raise ConnectionError("the store is down")

        runner._catalog_store().acceptances = broken
        result = await run(runner, agent("legacy__*"), [[["legacy__x", {}]] for _ in range(6)], tenant, PROJECT)
    assert len(events(result, BLOCKED)) == 2, [e.payload["tool"] for e in events(result, BLOCKED)]


async def test_a_sticky_reject_of_one_version_does_not_decide_another(tmp_path):
    """NFR-36 in the other direction: a sticky rejection of echo's first version rejects its next
    call of that version, and the changed version's call is asked again."""
    tenant = tenancy()
    changed = PARTS["description"](ECHO)
    hashes = (tool_hash("echo", ECHO["description"], ECHO["inputSchema"]),
              tool_hash("echo", changed["description"], changed["inputSchema"]),
              tool_hash("notify", "Announce a change.", {"type": "object"}))
    server = catalog_server("legacy", state_file(tmp_path, [ECHO]), approval="require",
                            approval_overrides={"notify": "never"},
                            accept=(McpAcceptance(tenant, PROJECT, DEPLOYER, hashes),))
    runner = runner_for([server], "memory")
    async with runner:
        frozen(runner)
        await run(runner, agent("legacy__*"), [], tenant, PROJECT)
        state_file(tmp_path, [changed])
        turns = [[["legacy__echo", {"text": "1"}]], [["legacy__echo", {"text": "2"}], ["legacy__notify", {}]],
                 [["legacy__echo", {"text": "3"}]]]
        handle = await runner.start(agent("legacy__*"), json.dumps(turns), RunConfig(tenant_id=tenant, project_id=PROJECT))
        [one] = await pending(runner, tenant, handle.run_id, 1)
        await handle.reject(one.interruption.interruption_id, principal=OWNER, sticky=True)
        [three] = await pending(runner, tenant, handle.run_id, 1)
        assert three.interruption.request_payload["tool_hash"] == hashes[1]
        await handle.approve(three.interruption.interruption_id, principal=OWNER)
        result = await handle.result()
    decided = await runner.interruptions(tenant, PROJECT, run_id=result.run_id, pending_only=False)
    by_text = {v.interruption.request_payload["arguments"]["text"]: (v.decision.source, v.decision.decision)
               for v in decided}
    assert by_text == {"1": ("principal", "reject"), "2": ("standing", "reject"), "3": ("principal", "approve")}
    assert (await results(runner, result, tenant, PROJECT))["t2-0"].content.startswith("called")


# =================================================================================================
# M34 round 1 review: a failed read again
# =================================================================================================


async def test_after_a_failed_read_again_no_call_is_validated_or_asked_about_against_the_old_listing(
        tmp_path, monkeypatch):
    """Round 1 review: a read again that failed ended the session but kept its tools offered, so
    a call was validated against the old schema, and an approval could be asked for a version
    then refused. The call is refused at step 1 instead, before either, and the next turn starts
    the server again and offers nothing it has not read."""
    import agentsdk.mcp as mcp
    from agentsdk.tools import Tool, ToolSpec

    tenant, clock = tenancy(), Clock()
    real, failing = mcp._read_listing, {"on": False}

    async def read_listing(client):
        if failing["on"]:
            failing["on"] = False
            raise ConnectionError("the listing could not be read")
        return await real(client)

    monkeypatch.setattr(mcp, "_read_listing", read_listing)

    def tick() -> str:
        clock.now += 61
        failing["on"] = True  # the read before the next call fails
        return "ticked"

    ticker = Tool(spec=ToolSpec(name="tick", description="Move.", input_schema={"type": "object"}), fn=tick)
    runner = runner_for([catalog_server("legacy", state_file(tmp_path, [ECHO]), approval="require")], "memory",
                        tools=[ticker])
    async with runner:
        runner._mcp.clock = clock
        first = await run(runner, agent("legacy__*", "tick"), [], tenant, PROJECT)
        await accept_all(runner, first, tenant, "legacy")
        result = await run(runner, agent("legacy__*", "tick"), [[["tick", {}], ["legacy__echo", {"text": "x"}]],
                                                                 [["legacy__echo", {"text": "y"}]]], tenant, PROJECT,
                           approval_timeout=2)  # nobody decides: the second turn's call expires
        asked = await runner.interruptions(tenant, PROJECT, run_id=result.run_id, pending_only=False)
        connection = runner._mcp._engine.connections[("legacy", tenant, PROJECT)]
    got = await results(runner, result, tenant, PROJECT)
    assert got["t0-1"].content == "ToolBlocked: legacy__echo: its server is not running", got["t0-1"].content
    # Round 2 review: labelled as the tool's, as the same refusal at the sink is.
    assert (got["t0-1"].provenance.origin, got["t0-1"].provenance.trust_zone) == (
        Origin.MCP_RESOURCE, TrustZone.UNTRUSTED)
    [called] = [e for e in events(result, EventType.TOOL_CALLED) if e.payload["tool_call_id"] == "t0-1"]
    assert called.payload["started_at"] is None, "refused at step 1"
    assert [v.interruption.tool_call_id for v in asked] == ["t1-0"], "only the next turn's call was asked about"
    assert connection.starts == 2, "the next turn started the server again"


async def test_a_turn_whose_read_again_failed_is_offered_none_of_that_servers_tools(tmp_path, monkeypatch):
    """Round 1 review: the README said a failed read's tools were absent; they were offered from
    the listing before. A turn whose read fails is offered none of them, and is told why."""
    import agentsdk.mcp as mcp

    tenant, model = tenancy(), Turns()
    real, failing = mcp._read_listing, {"on": False}

    async def read_listing(client):
        if failing["on"]:
            failing["on"] = False
            raise ConnectionError("the listing could not be read")
        return await real(client)

    monkeypatch.setattr(mcp, "_read_listing", read_listing)
    runner = runner_for([catalog_server("legacy", state_file(tmp_path, [ECHO]))], "memory", model=model)
    async with runner:
        clock = frozen(runner)
        first = await run(runner, agent("legacy__*"), [], tenant, PROJECT)
        await accept_all(runner, first, tenant, "legacy")
        used = await run(runner, agent("legacy__*"), [[["legacy__echo", {"text": "x"}]]], tenant, PROJECT)
        assert (await results(runner, used, tenant, PROJECT))["t0-0"].content.startswith("called"), "usable first"
        clock.now += 61
        failing["on"] = True  # the turn's own read fails
        start = len(model.requests)
        result = await run(runner, agent("legacy__*"), [], tenant, PROJECT)
    assert not any(name.startswith("legacy__") for name in offered(model.requests[start])), offered(model.requests[start])
    assert [e.payload["error"] for e in events(result, EventType.MCP_SERVER_UNAVAILABLE)] == ["ConnectionError"]
