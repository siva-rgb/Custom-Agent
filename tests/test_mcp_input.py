"""M36 gate: input-required results and elicitation (FR-133, FR-134, AC-93, NFR-30, NFR-31,
NFR-38), and the items M35's approval carried (KNOWLEDGE-dc116498).

A server's request for more input, during a call, pauses that call as an interruption the
application answers with `Runner.respond`. mcp 2.3.0 drives 2026-07-28's input_required retry
itself and hands every input request to one callback, the legacy era's elicitation/create too
(KNOWLEDGE-fe6eb6d4); on the legacy era a request is attributed only when one call is in flight
on the connection (the owner, DECISION-d4a40cfe).

Against the local HTTP server of M35 (tests/fixtures/mcp_http_servers.py) in both eras, whose
`ask`, `sample`, `roots` and `slow` tools ask for input as a real server does.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from agentsdk import RunConfig, RunStatus
from agentsdk.events import EventType
from agentsdk.identity import PrincipalContext
from agentsdk.interruptions import (InterruptionAlreadyDecided, InterruptionExpired, InterruptionKindMismatch,
                                    InterruptionNotWaiting, UnknownInterruption)
from agentsdk.mcp import HttpServer
from agentsdk.primitives import TrustZone
from tests.test_mcp_client import DSN, REPO, STORES, Turns, agent, events, results, runner_for
from tests.test_mcp_http import KEY, accepted, serve  # noqa: F401 - the fixtures, used by name

pytestmark = pytest.mark.skipif(__import__("sys").platform != "win32", reason="NFR-39 is measured on Windows (D11)")

PROJECT = "p-m36"
OWNER = PrincipalContext(agent_principal="input-ui", user_principal="owner")
ERAS = {"modern": "2026-07-28", "legacy": "2025-11-25"}
TOOLS = [{"name": n, "description": f"The fixture's {n}.", "inputSchema": {"type": "object"}}
         for n in ("ask", "sample", "roots", "slow", "echo")]
FORM = {"type": "object", "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
        "required": ["name"]}


@pytest.fixture(autouse=True)
def remove_what_the_test_wrote():
    yield
    if DSN:
        with psycopg.connect(DSN, autocommit=True) as conn:
            ids = [r[0] for r in conn.execute("SELECT run_id FROM runs WHERE tenant_id LIKE 'SYN-m36-%'")]
            conn.execute("DELETE FROM interruption_decisions WHERE interruption_id IN (SELECT interruption_id"
                         " FROM interruptions WHERE run_id = ANY(%s))", (ids,))
            conn.execute("DELETE FROM interruptions WHERE run_id = ANY(%s)", (ids,))
            conn.execute("DELETE FROM standing_decisions WHERE run_id = ANY(%s)", (ids,))
            for table in ("run_events", "messages", "execution_manifests"):
                conn.execute(f"DELETE FROM {table} WHERE run_id = ANY(%s)", (ids,))
            conn.execute("DELETE FROM artifacts WHERE tenant_id LIKE 'SYN-m36-%'")
            while conn.execute("DELETE FROM runs r WHERE run_id = ANY(%s) AND NOT EXISTS "
                               "(SELECT 1 FROM runs c WHERE c.parent_run_id = r.run_id)", (ids,)).rowcount:
                pass
            conn.execute("DELETE FROM mcp_tool_acceptances WHERE tenant_id LIKE 'SYN-m36-%'")
            conn.execute("DELETE FROM mcp_tool_versions WHERE tenant_id LIKE 'SYN-m36-%'")


def tenancy() -> str:
    return f"SYN-m36-{uuid.uuid4().hex[:10]}"


def web(served, **fields):
    fields.setdefault("approval", "never")
    fields.setdefault("connect_timeout", 60)
    return HttpServer(alias="web", url=served.url, headers={"X-Fixture-Key": KEY}, **fields)


async def start(runner, tenant, turns, **fields):
    return await runner.start(agent("web__*"), json.dumps(turns), RunConfig(tenant_id=tenant, project_id=PROJECT,
                                                                          **fields))


async def asked(runner, tenant, run_id, count=1, timeout=60.0, pending_only=True):
    deadline = time.monotonic() + timeout
    while True:
        views = [v for v in await runner.interruptions(tenant, PROJECT, run_id=run_id, pending_only=pending_only)
                 if v.interruption.kind.value != "tool_approval"]
        if len(views) >= count:
            return sorted(views, key=lambda v: v.interruption.created_at)
        assert time.monotonic() < deadline, f"only {len(views)} of {count} input requests"
        await asyncio.sleep(0.05)


def answered(text: str) -> list:
    assert text.startswith("answered "), text
    return json.loads(text[len("answered "):])


# =================================================================================================
# FR-133: the kinds, and what is recorded
# =================================================================================================


@pytest.mark.parametrize("store", STORES)
@pytest.mark.parametrize("kind", ERAS)
async def test_a_form_elicitation_pauses_the_call_and_an_accepted_answer_continues_it(store, kind, serve, accepted):
    served, tenant = serve(kind, tools=TOOLS), tenancy()
    # A call_timeout far shorter than the wait: no call_timeout runs while the call waits.
    runner = runner_for([web(served, call_timeout=2)], store)
    async with runner:
        handle = await start(runner, tenant, [[["web__ask", {"times": 1}]]])
        [view] = await asked(runner, tenant, handle.run_id)
        await asyncio.sleep(3)
        decision = await runner.respond(tenant, PROJECT, view.interruption.interruption_id, action="accept",
                                        content={"name": "Ada", "age": 36}, principal=OWNER)
        result = await handle.result()
    item = view.interruption
    assert (item.kind.value, item.trigger, item.tool_name, item.tool_call_id) == (
        "additional_user_input", "server", "web__ask", "t0-0")
    assert item.request_payload["alias"] == "web" and item.request_payload["tool"] == "web__ask"
    assert item.request_payload["mode"] == "form" and item.request_payload["requested_schema"] == FORM
    assert item.request_payload["message"] == "Who are you? (0)"
    assert (decision.decision, decision.source, decision.principal) == ("accept", "principal", OWNER)
    assert decision.response == {"name": "Ada", "age": 36}
    got = (await results(runner, result, tenant, PROJECT))["t0-0"]
    assert answered(got.content) == [{"action": "accept", "content": {"name": "Ada", "age": 36}}]
    assert result.status is RunStatus.COMPLETED


async def test_the_sdks_own_deadline_does_not_run_while_the_call_waits(serve, accepted):
    """FR-133: no call_timeout runs during the wait -- the SDK's own deadline (call_timeout plus its
    margin, here 6 s) included: the person answers after 8 s, and the call goes on."""
    served, tenant = serve("modern", tools=TOOLS), tenancy()
    runner = runner_for([web(served, call_timeout=1)])
    async with runner:
        handle = await start(runner, tenant, [[["web__ask", {}]]])
        [view] = await asked(runner, tenant, handle.run_id)
        await asyncio.sleep(8)
        await runner.respond(tenant, PROJECT, view.interruption.interruption_id, action="accept",
                             content={"name": "Ada"}, principal=OWNER)
        result = await handle.result()
    assert answered((await results(runner, result, tenant, PROJECT))["t0-0"].content) == [
        {"action": "accept", "content": {"name": "Ada"}}]


async def test_a_legacy_stdio_request_with_one_call_in_flight_is_attributed_to_it(accepted):
    """DECISION-d4a40cfe: over stdio nothing names the call, and the one in flight is it."""
    from tests.test_mcp_client import server

    tenant = tenancy()
    runner = runner_for([server("legacy", approval="never")])
    async with runner:
        handle = await runner.start(agent("legacy__*"), json.dumps([[["legacy__ask", {}]]]),
                                    RunConfig(tenant_id=tenant, project_id=PROJECT))
        views = []
        deadline = time.monotonic() + 60
        while not views:
            views = await runner.interruptions(tenant, PROJECT, run_id=handle.run_id)
            assert time.monotonic() < deadline
            await asyncio.sleep(0.05)
        await handle.respond(views[0].interruption.interruption_id, action="accept", content={"name": "Ada"},
                             principal=OWNER)
        result = await handle.result()
    assert views[0].interruption.request_payload["mode"] == "form"
    assert answered((await results(runner, result, tenant, PROJECT))["t0-0"].content) == [
        {"action": "accept", "content": {"name": "Ada"}}]


@pytest.mark.parametrize("kind", ERAS)
async def test_the_events_name_the_request_and_never_the_response(kind, serve, accepted):
    served, tenant = serve(kind, tools=TOOLS), tenancy()
    runner = runner_for([web(served)])
    async with runner:
        handle = await start(runner, tenant, [[["web__ask", {}]]])
        [view] = await asked(runner, tenant, handle.run_id)
        await runner.respond(tenant, PROJECT, view.interruption.interruption_id, action="accept",
                             content={"name": "Secret-Name-123"}, principal=OWNER)
        result = await handle.result()
    [requested] = events(result, EventType.INPUT_REQUESTED)
    [answer] = events(result, EventType.INPUT_ANSWERED)
    for event in (requested, answer):
        assert {k: event.payload[k] for k in ("interruption_id", "kind", "alias", "tool")} == {
            "interruption_id": view.interruption.interruption_id, "kind": "additional_user_input",
            "alias": "web", "tool": "web__ask"}
        assert "Secret-Name-123" not in json.dumps(event.payload)
    assert answer.payload["action"] == "accept"


@pytest.mark.parametrize("kind", ERAS)
async def test_a_url_elicitation_records_the_full_url_and_the_sdk_never_opens_it(kind, serve, accepted):
    served, tenant = serve(kind, tools=TOOLS), tenancy()
    consent = f"{served.base}/consent?token=abc&state=xyz"  # served and watched: never fetched
    runner = runner_for([web(served)])
    async with runner:
        handle = await start(runner, tenant, [[["web__ask", {"mode": "url", "url": consent}]]])
        [view] = await asked(runner, tenant, handle.run_id)
        await runner.respond(tenant, PROJECT, view.interruption.interruption_id, action="accept", principal=OWNER)
        result = await handle.result()
    assert view.interruption.request_payload["mode"] == "url" and view.interruption.request_payload["url"] == consent
    got = (await results(runner, result, tenant, PROJECT))["t0-0"]
    assert answered(got.content) == [{"action": "accept", "content": None}]
    assert not any("/consent" in hit for hit in served.hits()), "the SDK opened the URL"


@pytest.mark.parametrize("action", ["decline", "cancel"])
@pytest.mark.parametrize("kind", ERAS)
async def test_decline_and_cancel_reach_the_server_as_that_action(kind, action, serve, accepted):
    served, tenant = serve(kind, tools=TOOLS), tenancy()
    runner = runner_for([web(served)])
    async with runner:
        handle = await start(runner, tenant, [[["web__ask", {}]]])
        [view] = await asked(runner, tenant, handle.run_id)
        decision = await handle.respond(view.interruption.interruption_id, action=action, principal=OWNER)
        result = await handle.result()
    assert decision.decision == action and decision.response is None
    assert answered((await results(runner, result, tenant, PROJECT))["t0-0"].content) == [
        {"action": action, "content": None}]


@pytest.mark.parametrize("store", STORES)
@pytest.mark.parametrize("kind", ERAS)
async def test_expiry_reaches_the_server_as_cancel(store, kind, serve, accepted):
    served, tenant = serve(kind, tools=TOOLS), tenancy()
    runner = runner_for([web(served, call_timeout=1)], store)
    async with runner:
        handle = await start(runner, tenant, [[["web__ask", {}]]], approval_timeout=2)
        [view] = await asked(runner, tenant, handle.run_id)
        result = await handle.result()
        [after] = await asked(runner, tenant, handle.run_id, pending_only=False)
    assert (after.decision.decision, after.decision.source, after.state) == ("cancel", "expiry", "expired")
    assert answered((await results(runner, result, tenant, PROJECT))["t0-0"].content) == [
        {"action": "cancel", "content": None}]


@pytest.mark.parametrize("kind", ERAS)
async def test_cancelling_the_run_decides_the_request_cancel(kind, serve, accepted):
    served, tenant = serve(kind, tools=TOOLS), tenancy()
    runner = runner_for([web(served)])
    async with runner:
        handle = await start(runner, tenant, [[["web__ask", {}]]])
        [view] = await asked(runner, tenant, handle.run_id)
        handle.cancel()
        result = await handle.result()
        [after] = await asked(runner, tenant, handle.run_id, pending_only=False)
    assert result.status is RunStatus.CANCELLED
    assert (after.decision.decision, after.decision.source, after.state) == ("cancel", "cancel", "cancelled")


@pytest.mark.parametrize("kind", ERAS)
async def test_a_fourth_input_request_fails_the_call(kind, serve, accepted):
    served, tenant = serve(kind, tools=TOOLS), tenancy()
    runner = runner_for([web(served)])
    async with runner:
        # A short approval_timeout: a fourth request wrongly asked would expire, not hang the test.
        handle = await start(runner, tenant, [[["web__ask", {"times": 4}]]], approval_timeout=20)
        for n in range(3):
            views = await asked(runner, tenant, handle.run_id, count=n + 1, pending_only=False)
            await runner.respond(tenant, PROJECT, views[-1].interruption.interruption_id, action="accept",
                                 content={"name": f"n{n}"}, principal=OWNER)
        result = await handle.result()
        every = await asked(runner, tenant, handle.run_id, count=3, pending_only=False)
    got = (await results(runner, result, tenant, PROJECT))["t0-0"]
    assert got.content == "ToolExecutionError: too many input requests", got.content
    assert len(every) == 3, "the fourth was refused, never recorded"


@pytest.mark.parametrize("kind", ERAS)
async def test_the_answered_call_continues_once_under_its_original_approval(kind, serve, accepted):
    """NFR-30: the retry continues the same call -- one approval, one execution -- and the
    approved request the tool was handed is the one the person approved."""
    served, tenant = serve(kind, tools=TOOLS), tenancy()
    runner = runner_for([web(served, approval="require")])
    async with runner:
        handle = await start(runner, tenant, [[["web__ask", {}]]])
        approval = None
        deadline = time.monotonic() + 60
        while approval is None:
            views = await runner.interruptions(tenant, PROJECT, run_id=handle.run_id)
            approval = next((v for v in views if v.interruption.kind.value == "tool_approval"), None)
            assert time.monotonic() < deadline
            await asyncio.sleep(0.05)
        await handle.approve(approval.interruption.interruption_id, principal=OWNER)
        [view] = await asked(runner, tenant, handle.run_id)
        await runner.respond(tenant, PROJECT, view.interruption.interruption_id, action="accept",
                             content={"name": "Ada"}, principal=OWNER)
        result = await handle.result()
        every = await runner.interruptions(tenant, PROJECT, run_id=handle.run_id, pending_only=False)
    assert sorted(v.interruption.kind.value for v in every) == ["additional_user_input", "tool_approval"]
    called = [e for e in events(result, EventType.TOOL_CALLED) if e.payload["tool_call_id"] == "t0-0"]
    assert len(called) == 1 and not called[0].payload["is_error"]
    assert view.interruption.request_payload["approved_by"] == approval.interruption.interruption_id


# =================================================================================================
# FR-133: answering, its refusals and its races
# =================================================================================================


@pytest.mark.parametrize("store", STORES)
async def test_content_that_does_not_match_the_requested_schema_is_refused(store, serve, accepted):
    served, tenant = serve("modern", tools=TOOLS), tenancy()
    runner = runner_for([web(served)], store)
    async with runner:
        handle = await start(runner, tenant, [[["web__ask", {}]]])
        [view] = await asked(runner, tenant, handle.run_id)
        ident = view.interruption.interruption_id
        for content in (None, {"age": 3}, {"name": 5}, {"name": "x", "age": "old"}, "Ada"):
            with pytest.raises(ValueError, match="content"):
                await runner.respond(tenant, PROJECT, ident, action="accept", content=content, principal=OWNER)
        with pytest.raises(ValueError, match="content"):
            await runner.respond(tenant, PROJECT, ident, action="decline", content={"name": "x"}, principal=OWNER)
        [still] = await asked(runner, tenant, handle.run_id)
        assert still.decision is None, "a refused answer recorded something"
        await runner.respond(tenant, PROJECT, ident, action="accept", content={"name": "Ada"}, principal=OWNER)
        await handle.result()


@pytest.mark.parametrize("store", STORES)
async def test_respond_refuses_in_decides_order_with_kind_mismatch_second(store, serve, accepted):
    served, tenant, other = serve("modern", tools=TOOLS), tenancy(), tenancy()
    runner = runner_for([web(served, approval_overrides={"echo": "require"})], store)
    async with runner:
        handle = await start(runner, tenant, [[["web__ask", {}]]])
        [view] = await asked(runner, tenant, handle.run_id)
        ident = view.interruption.interruption_id
        # 1. tenancy: another tenant's interruption reads as unknown, before its kind is told.
        with pytest.raises(UnknownInterruption):
            await runner.respond(other, PROJECT, ident, action="accept", content={"name": "x"}, principal=OWNER)
        with pytest.raises(UnknownInterruption):
            await runner.decide(other, PROJECT, ident, approve=True, principal=OWNER)
        # 2. kind: an approval is not answered, nor an input request decided.
        for call in (runner.decide(tenant, PROJECT, ident, approve=True, principal=OWNER),
                     runner.decide(tenant, PROJECT, ident, approve=False, principal=OWNER),
                     handle.approve(ident, principal=OWNER), handle.reject(ident, principal=OWNER)):
            with pytest.raises(InterruptionKindMismatch):
                await call
        # the principal names a user or a service
        with pytest.raises(ValueError):
            await runner.respond(tenant, PROJECT, ident, action="decline",
                                 principal=PrincipalContext(agent_principal="a"))
        with pytest.raises(ValueError):
            await runner.respond(tenant, PROJECT, ident, action="approve", principal=OWNER)
        await runner.respond(tenant, PROJECT, ident, action="decline", principal=OWNER)
        # 3. decided already -- and the kind is still told first, decided or not
        with pytest.raises(InterruptionAlreadyDecided):
            await runner.respond(tenant, PROJECT, ident, action="cancel", principal=OWNER)
        with pytest.raises(InterruptionKindMismatch):
            await runner.decide(tenant, PROJECT, ident, approve=True, principal=OWNER)
        await handle.result()
        approving = await start(runner, tenant, [[["web__echo", {"text": "x"}]]])
        deadline = time.monotonic() + 60
        while not (views := await runner.interruptions(tenant, PROJECT, run_id=approving.run_id)):
            assert time.monotonic() < deadline
            await asyncio.sleep(0.05)
        with pytest.raises(InterruptionKindMismatch):
            await runner.respond(tenant, PROJECT, views[0].interruption.interruption_id, action="accept",
                                 principal=OWNER)
        await approving.approve(views[0].interruption.interruption_id, principal=OWNER)
        await approving.result()


async def test_respond_refuses_an_expired_request_and_one_no_run_waits_on(serve, accepted):
    served, tenant = serve("modern", tools=TOOLS), tenancy()
    runner = runner_for([web(served)])
    async with runner:
        handle = await start(runner, tenant, [[["web__ask", {}]]], approval_timeout=1)
        [view] = await asked(runner, tenant, handle.run_id)
        store = runner._interruption_store()
        # Expired, read from the clock before the run itself records it.
        late = datetime.now(timezone.utc) + timedelta(seconds=5)
        real = __import__("agentsdk.api", fromlist=["datetime"])
        await asyncio.sleep(1.2)
        with pytest.raises((InterruptionExpired, InterruptionAlreadyDecided)):
            await runner.respond(tenant, PROJECT, view.interruption.interruption_id, action="accept",
                                 content={"name": "x"}, principal=OWNER)
        await handle.result()
    # A request no run of this Runner waits on: another Runner over the same store.
    second = runner_for([web(served)])
    second._memory_interruptions = store
    async with second:
        item = view.interruption
        with pytest.raises((InterruptionNotWaiting, InterruptionAlreadyDecided)):
            await second.respond(tenant, PROJECT, item.interruption_id, action="decline", principal=OWNER)
    del late, real


@pytest.mark.parametrize("store", STORES)
async def test_a_race_between_respond_and_expiry_records_exactly_one_decision(store, serve, accepted):
    served, tenant = serve("modern", tools=TOOLS), tenancy()
    runner = runner_for([web(served)], store)
    async with runner:
        handle = await start(runner, tenant, [[["web__ask", {}]]], approval_timeout=2)
        [view] = await asked(runner, tenant, handle.run_id)
        await asyncio.sleep(max(0.0, (view.interruption.expires_at - datetime.now(timezone.utc)).total_seconds() - 0.05))
        outcome = None
        try:
            outcome = await runner.respond(tenant, PROJECT, view.interruption.interruption_id, action="decline",
                                           principal=OWNER)
        except (InterruptionExpired, InterruptionAlreadyDecided, InterruptionNotWaiting):
            pass
        result = await handle.result()
        [after] = await asked(runner, tenant, handle.run_id, pending_only=False)
    action = answered((await results(runner, result, tenant, PROJECT))["t0-0"].content)[0]["action"]
    assert after.decision is not None
    assert action == after.decision.decision, "the server was told what the store holds"
    if outcome is not None:
        assert after.decision.decision_id == outcome.decision_id


# =================================================================================================
# FR-133: the store, and migration 0018
# =================================================================================================


def test_migration_0018_refuses_a_decision_of_the_other_kind():
    """AC-93: an input decision of approve, and an approval decision of accept, are refused by the
    database itself; expiry and cancel decide cancel for an input request."""
    tenant = tenancy()
    run_id, approval, request = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    with psycopg.connect(DSN) as conn:
        conn.execute("INSERT INTO runs (run_id, tenant_id, project_id, agent_spec_id, status, max_turns)"
                     " VALUES (%s, %s, %s, 'a', 'running', 1)", (run_id, tenant, PROJECT))
        for ident, kind, trigger in ((approval, "tool_approval", "policy"), (request, "additional_user_input", "server")):
            conn.execute("INSERT INTO interruptions (interruption_id, tenant_id, project_id, run_id, top_run_id, kind,"
                         " tool_call_id, tool_name, agent_id, trigger, request, created_at, expires_at)"
                         " VALUES (%s,%s,%s,%s,%s,%s,'c','t','a',%s,'{}',%s,%s)",
                         (ident, tenant, PROJECT, run_id, run_id, kind, trigger, now, now + timedelta(hours=1)))
        conn.commit()
        refused = [
            (request, "approve", "principal"), (approval, "accept", "principal"), (request, "reject", "expiry"),
            (approval, "cancel", "cancel"), (request, "decline", "expiry"),
        ]
        for ident, decision, source in refused:
            with pytest.raises(psycopg.errors.IntegrityError):
                with conn.transaction():
                    conn.execute(
                        "INSERT INTO interruption_decisions (decision_id, interruption_id, tenant_id, project_id,"
                        " decision, source, principal, decided_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        (str(uuid.uuid4()), ident, tenant, PROJECT, decision, source,
                         '{"agent_principal": "a", "user_principal": "u"}' if source == "principal" else None, now))
        # A trigger other than server is refused for an input request, and server for an approval.
        for kind, trigger in (("additional_user_input", "policy"), ("tool_approval", "server")):
            with pytest.raises(psycopg.errors.IntegrityError):
                with conn.transaction():
                    conn.execute(
                        "INSERT INTO interruptions (interruption_id, tenant_id, project_id, run_id, top_run_id, kind,"
                        " tool_call_id, tool_name, agent_id, trigger, request, created_at, expires_at)"
                        " VALUES (%s,%s,%s,%s,%s,%s,'c','t','a',%s,'{}',%s,%s)",
                        (str(uuid.uuid4()), tenant, PROJECT, run_id, run_id, kind, trigger, now, now))
        conn.execute("DELETE FROM interruptions WHERE run_id = %s", (run_id,))
        conn.execute("DELETE FROM runs WHERE run_id = %s", (run_id,))
        conn.commit()


def test_the_in_memory_store_refuses_what_the_migration_does():
    from agentsdk.interruptions import InMemoryInterruptionStore, InterruptionDecision
    from agentsdk.outcomes import InterruptionKind, RunInterruption

    tenant, run = tenancy(), str(uuid.uuid4())
    store = InMemoryInterruptionStore(runs=lambda *a: True)
    now = datetime.now(timezone.utc)
    approval = RunInterruption(kind=InterruptionKind.TOOL_APPROVAL, run_id=run, top_run_id=run, tenant_id=tenant,
                               project_id=PROJECT, tool_call_id="c", tool_name="t", agent_id="a", trigger="policy",
                               request_payload={}, created_at=now, expires_at=now + timedelta(hours=1))
    request = RunInterruption(kind=InterruptionKind.ADDITIONAL_USER_INPUT, run_id=run, top_run_id=run,
                              tenant_id=tenant, project_id=PROJECT, tool_call_id="c", tool_name="t", agent_id="a",
                              trigger="server", request_payload={}, created_at=now, expires_at=now + timedelta(hours=1))
    store.append_interruption(approval)
    store.append_interruption(request)
    for item in (replace_kind(approval, "server"), replace_kind(request, "policy")):
        with pytest.raises(ValueError):
            store.append_interruption(item)

    def decision(item, value, source="principal"):
        return InterruptionDecision(decision_id=str(uuid.uuid4()), interruption_id=item.interruption_id,
                                    tenant_id=tenant, project_id=PROJECT, decision=value, source=source,
                                    decided_at=now, principal=OWNER if source == "principal" else None)

    for item, value, source in ((request, "approve", "principal"), (approval, "accept", "principal"),
                                (request, "reject", "expiry"), (approval, "cancel", "cancel")):
        with pytest.raises((ValueError, InterruptionKindMismatch)):
            store.append_decision(decision(item, value, source))
    with pytest.raises(ValueError):
        decision(request, "accept", "expiry")  # expiry and cancel decide cancel or reject, never accept


def replace_kind(item, trigger):
    from dataclasses import replace

    return replace(item, interruption_id=str(uuid.uuid4()), trigger=trigger)


# =================================================================================================
# FR-133: requests the SDK does not support, and attribution on the legacy era
# =================================================================================================


@pytest.mark.parametrize("store", STORES)
@pytest.mark.parametrize("tool", ["sample", "roots"])
@pytest.mark.parametrize("kind", ERAS)
async def test_a_sampling_or_roots_request_is_answered_unsupported_and_recorded(store, kind, tool, serve, accepted):
    served, tenant = serve(kind, tools=TOOLS), tenancy()
    runner = runner_for([web(served)], store)
    async with runner:
        result = await (await start(runner, tenant, [[[f"web__{tool}", {}]]])).result()
        [view] = await asked(runner, tenant, result.run_id, pending_only=False)
    item = view.interruption
    assert (item.kind.value, item.trigger, item.tool_call_id) == ("external_tool_input", "server", "t0-0")
    assert item.request_payload["method"] == ("sampling/createMessage" if tool == "sample" else "roots/list")
    assert (view.decision.decision, view.decision.source) == ("decline", "unsupported")
    got = (await results(runner, result, tenant, PROJECT))["t0-0"]
    if kind == "legacy":
        assert "not supported" in got.content, got.content  # the server was told, and said so
    else:
        assert got.is_error and "not supported" in got.content, got.content
    assert not events(result, EventType.INPUT_REQUESTED), "nothing was asked of a person"


async def test_the_client_declares_elicitation_and_never_sampling_or_roots(serve, accepted):
    served, tenant = serve("legacy", tools=TOOLS), tenancy()
    runner = runner_for([web(served)])
    async with runner:
        await (await start(runner, tenant, [])).result()
        connection = runner._mcp._engine.connections[("web", tenant, PROJECT)]
        capabilities = connection.client.session._build_capabilities("2025-11-25")
    assert capabilities.elicitation is not None and capabilities.elicitation.form is not None
    assert capabilities.elicitation.url is not None
    assert capabilities.sampling is None and capabilities.roots is None


async def test_a_legacy_stdio_request_with_two_calls_in_flight_is_answered_cancel_and_both_runs_told(accepted):
    """The owner (DECISION-d4a40cfe): over stdio a legacy-era input request names no call, so
    with two calls in flight on one connection it is answered cancel, and each run is told."""
    from tests.test_mcp_client import server

    tenant = tenancy()
    runner = runner_for([server("legacy", approval="never")])
    async with runner:
        await (await runner.start(agent("legacy__*"), "[]", RunConfig(tenant_id=tenant, project_id=PROJECT))).result()
        # This server serves one request at a time: the asking call asks after two seconds, while
        # the echo -- started meanwhile -- waits behind it, in flight on the client.
        # A short approval_timeout: a request wrongly attributed would expire, and be recorded.
        ask = await runner.start(agent("legacy__*"), json.dumps([[["legacy__ask", {"delay": 2}]]]),
                                 RunConfig(tenant_id=tenant, project_id=PROJECT, approval_timeout=5))
        await asyncio.sleep(0.5)
        echo = await runner.start(agent("legacy__*"), json.dumps([[["legacy__echo", {"text": "after"}]]]),
                                  RunConfig(tenant_id=tenant, project_id=PROJECT))
        ask_result, echo_result = await ask.result(), await echo.result()
        recorded = await runner.interruptions(tenant, PROJECT, run_id=ask.run_id, pending_only=False)
    assert answered((await results(runner, ask_result, tenant, PROJECT))["t0-0"].content) == [
        {"action": "cancel", "content": None}]
    assert (await results(runner, echo_result, tenant, PROJECT))["t0-0"].content == "after"
    assert recorded == [], "nothing recorded for a run it could not be told belonged to it"
    for result in (ask_result, echo_result):
        assert events(result, EventType.INPUT_UNATTRIBUTED), result.run_id


@pytest.mark.parametrize("kind", ERAS)
async def test_over_http_a_request_with_two_calls_in_flight_is_attributed_exactly(kind, serve, accepted):
    """Over HTTP the request comes on its own call's response stream, read in that call's
    context, on either era: no other call in flight confuses it."""
    served, tenant = serve(kind, tools=TOOLS), tenancy()
    runner = runner_for([web(served)])
    async with runner:
        await (await start(runner, tenant, [])).result()
        ask = await start(runner, tenant, [[["web__ask", {"delay": 2}]]])
        await asyncio.sleep(0.5)
        slow = await start(runner, tenant, [[["web__slow", {"seconds": 4}]]])
        [view] = await asked(runner, tenant, ask.run_id)
        await runner.respond(tenant, PROJECT, view.interruption.interruption_id, action="accept",
                             content={"name": "Ada"}, principal=OWNER)
        await slow.result()
        result = await ask.result()
    assert view.interruption.run_id == ask.run_id
    assert not events(result, EventType.INPUT_UNATTRIBUTED)
    assert answered((await results(runner, result, tenant, PROJECT))["t0-0"].content)[0]["action"] == "accept"


# =================================================================================================
# M36 pre-review: found by runtime probes, each fixed
# =================================================================================================


@pytest.mark.parametrize("kind", ERAS)
async def test_requests_asked_together_keep_the_call_waiting_until_the_last_is_answered(kind, serve, accepted):
    """Pre-review D1: the first answer of two asked at once set the call running again, so it
    failed at call_timeout while the second still waited."""
    served, tenant = serve(kind, tools=TOOLS), tenancy()
    runner = runner_for([web(served, call_timeout=1)])
    async with runner:
        handle = await start(runner, tenant, [[["web__ask", {"together": 2}]]])
        first, second = await asked(runner, tenant, handle.run_id, count=2)
        await runner.respond(tenant, PROJECT, first.interruption.interruption_id, action="accept",
                             content={"name": "one"}, principal=OWNER)
        await asyncio.sleep(8)  # past call_timeout and its margin, while the second still waits
        await runner.respond(tenant, PROJECT, second.interruption.interruption_id, action="accept",
                             content={"name": "two"}, principal=OWNER)
        result = await handle.result()
    got = answered((await results(runner, result, tenant, PROJECT))["t0-0"].content)
    assert sorted(a["content"]["name"] for a in got) == ["one", "two"], got


async def test_a_wait_lasts_its_approval_timeout_not_the_calls_bound(serve, accepted, monkeypatch):
    """Pre-review D2: the caller's deadline for a call was its call bound, about 270 s, so a person
    answering later than that lost the call, whatever approval_timeout said. The bound here is
    cut to 10 s; the person answers after 15."""
    import agentsdk.mcp as mcp

    real = mcp._call_bound
    monkeypatch.setattr(mcp, "_call_bound", lambda server: 10.0)
    served, tenant = serve("modern", tools=TOOLS), tenancy()
    runner = runner_for([web(served)])
    try:
        async with runner:
            handle = await start(runner, tenant, [[["web__ask", {}]]], approval_timeout=60)
            [view] = await asked(runner, tenant, handle.run_id)
            await asyncio.sleep(15)
            await runner.respond(tenant, PROJECT, view.interruption.interruption_id, action="accept",
                                 content={"name": "late"}, principal=OWNER)
            result = await handle.result()
    finally:
        monkeypatch.setattr(mcp, "_call_bound", real)
    assert answered((await results(runner, result, tenant, PROJECT))["t0-0"].content) == [
        {"action": "accept", "content": {"name": "late"}}]


@pytest.mark.parametrize("content, schema", [
    ({"name": "a", 1: 2}, None),
    ({"name": {"nested": {"deep": 1}}}, {"type": "object", "properties": {"name": {"type": "object"}}}),
    ({"name": "x" * 70_000}, None),
])
async def test_an_answer_the_server_could_not_be_given_is_refused_before_it_is_recorded(content, schema, serve,
                                                                                         accepted):
    """Pre-review D3: content the schema allowed but the protocol's answer does not -- a key that is
    not text, a nested object -- was recorded accept and never reached the server; C3: an answer
    had no size bound."""
    served, tenant = serve("modern", tools=TOOLS), tenancy()
    runner = runner_for([web(served)])
    async with runner:
        handle = await start(runner, tenant, [[["web__ask", {"schema": schema} if schema else {}]]])
        [view] = await asked(runner, tenant, handle.run_id)
        with pytest.raises(ValueError, match="content"):
            await runner.respond(tenant, PROJECT, view.interruption.interruption_id, action="accept",
                                 content=content, principal=OWNER)
        [still] = await asked(runner, tenant, handle.run_id)
        assert still.decision is None, "a refused answer was recorded"
        await runner.respond(tenant, PROJECT, view.interruption.interruption_id, action="decline", principal=OWNER)
        await handle.result()


@pytest.mark.parametrize("kind", ERAS)
async def test_sampling_and_roots_count_toward_the_three_requests(kind, serve, accepted):
    """Pre-review D4: a server could ask for sampling without end, each request a row."""
    served, tenant = serve(kind, tools=TOOLS), tenancy()
    runner = runner_for([web(served)])
    # On 2026-07-28 the first refusal fails the call, so a flood comes as one round of many; on the
    # legacy era the server is refused each time and may ask again.
    asks = {"together": 6} if kind == "modern" else {"times": 6}
    async with runner:
        result = await (await start(runner, tenant, [[["web__sample", asks]]])).result()
        recorded = await runner.interruptions(tenant, PROJECT, run_id=result.run_id, pending_only=False)
    # Legacy: one at a time, so exactly three. 2026-07-28: the round's requests are dispatched
    # together, and the first refusal cancels its siblings, so one to three are recorded -- never more.
    assert len(recorded) == 3 if kind == "legacy" else 1 <= len(recorded) <= 3, len(recorded)
    got = (await results(runner, result, tenant, PROJECT))["t0-0"].content
    if kind == "legacy":
        assert got == "ToolExecutionError: too many input requests", got
    else:
        assert got.startswith("ToolExecutionError:") and "not supported" in got or "too many" in got, got


@pytest.mark.parametrize("kind", ERAS)
async def test_an_application_reads_request_is_cancelled_and_no_run_is_told(kind, serve, accepted):
    """Pre-review D5: an mcp_read that asked for input, while one run's call was in flight, told
    that run it could not be attributed -- though the read's own context names it as no call's."""
    served, tenant = serve(kind, tools=TOOLS), tenancy()
    runner = runner_for([web(served)])
    async with runner:
        await (await start(runner, tenant, [])).result()
        slow = await start(runner, tenant, [[["web__slow", {"seconds": 4}]]])
        await asyncio.sleep(1)
        ref = await runner.mcp_read(tenant, PROJECT, "web", "ask://form")
        content = await runner._artifacts_for(tenant, PROJECT).get(ref.artifact_id)
        result = await slow.result()
    assert answered(content.decode()) == [{"action": "cancel", "content": None}]
    assert not events(result, EventType.INPUT_UNATTRIBUTED), "a run was told of a request that was not its"


@pytest.mark.parametrize("kind", ERAS)
async def test_a_read_resource_calls_request_is_recorded_on_its_run(kind, serve, accepted):
    """Pre-review D6: `<alias>__read_resource` is a run's call, yet a request during its read was
    cancelled with no record and no event."""
    served, tenant = serve(kind, tools=TOOLS), tenancy()
    runner = runner_for([web(served, resources_as_tool=True)])
    async with runner:
        handle = await start(runner, tenant, [[["web__read_resource", {"uri": "ask://form"}]]])
        [view] = await asked(runner, tenant, handle.run_id)
        await handle.respond(view.interruption.interruption_id, action="accept", content={"name": "Ada"},
                             principal=OWNER)
        result = await handle.result()
    assert view.interruption.tool_name == "web__read_resource" and view.interruption.tool_call_id == "t0-0"
    assert answered((await results(runner, result, tenant, PROJECT))["t0-0"].content) == [
        {"action": "accept", "content": {"name": "Ada"}}]


async def test_an_accepted_answer_is_stored_with_its_labels(serve, accepted):
    """Pre-review C5: the response's labels were defined and stored nowhere."""
    from agentsdk.interruptions import RESPONSE_PROVENANCE

    served, tenant = serve("modern", tools=TOOLS), tenancy()
    runner = runner_for([web(served)], "postgres")
    async with runner:
        handle = await start(runner, tenant, [[["web__ask", {}]]])
        [view] = await asked(runner, tenant, handle.run_id)
        made = await runner.respond(tenant, PROJECT, view.interruption.interruption_id, action="accept",
                                    content={"name": "Ada"}, principal=OWNER)
        await handle.result()
        [after] = await asked(runner, tenant, handle.run_id, pending_only=False)
    assert made.response_provenance == RESPONSE_PROVENANCE and after.decision.response == {"name": "Ada"}
    with psycopg.connect(DSN) as conn:
        [[stored]] = conn.execute("SELECT response FROM interruption_decisions WHERE interruption_id = %s",
                                  (view.interruption.interruption_id,)).fetchall()
    assert stored == {"content": {"name": "Ada"}, "provenance": {
        "origin": "user", "instruction_authority": "data_only", "trust_zone": "untrusted",
        "taint_flags": ["user_controlled"]}}


# =================================================================================================
# Carried from M35 (KNOWLEDGE-dc116498)
# =================================================================================================


def test_no_configured_header_value_is_in_the_http_clients_repr():
    """M35 review: httpx2 masks only Authorization in a client's headers' repr, so a custom API-key
    header showed in plain text. The configured headers live in the transport alone."""
    from agentsdk.mcp import _http_client

    server = HttpServer(alias="web", url="https://example.com/mcp",
                        headers={"X-Api-Key": "KEY-1-SECRET", "Cookie": "KEY-2-SECRET", "Authorization": "KEY-3-SECRET"})
    client = _http_client(server, "nonce")
    for text in (repr(client), repr(client.headers), str(client.headers), repr(client._transport),
                 repr(vars(client).get("_headers"))):
        assert "SECRET" not in text, text


def test_accept_encoding_cannot_be_configured():
    with pytest.raises(ValueError, match="the SDK sets"):
        HttpServer(alias="web", url="https://example.com/mcp", headers={"ACCEPT-encoding": "gzip"})


@pytest.mark.parametrize("given, stored", [
    ("text/html; charset=utf-8", "text/html;charset=utf-8"), ("text/plain", "text/plain"),
    ("text/html; charset=utf-8; q=0.9", "text/html;charset=utf-8;q=0.9"),
    ("text/html; bad parameter", "text/plain"), ("../../etc", "text/plain"),
    ('text/html; charset="utf-8"', 'text/html;charset="utf-8"'),  # pre-review C6
])
async def test_a_resources_content_type_keeps_its_parameters(given, stored, serve):
    note = {"uri": "notes://today", "name": "today", "mimeType": given, "text": "hello"}
    served, tenant = serve("modern", resources=[note]), tenancy()
    runner = runner_for([web(served)])
    async with runner:
        ref = await runner.mcp_read(tenant, PROJECT, "web", "notes://today")
    assert ref.mime_type == stored


# =================================================================================================
# FR-134: the example
# =================================================================================================


@pytest.mark.parametrize("store", STORES)
async def test_example_30_shows_every_step_on_both_stores(store):
    import importlib.util

    spec = importlib.util.spec_from_file_location("example_30", REPO / "scripts" / "30_mcp_input.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.TENANT = tenancy()  # under this file's cleanup
    checks, _ = await module.offline(store)
    assert [label for label, passed in checks if not passed] == [] and len(checks) >= 4


def test_an_accepted_answer_is_labelled_user_controlled():
    from agentsdk.interruptions import RESPONSE_PROVENANCE

    from agentsdk.primitives import InstructionAuthority, Origin, TaintFlag

    assert (RESPONSE_PROVENANCE.origin, RESPONSE_PROVENANCE.instruction_authority) == (
        Origin.USER, InstructionAuthority.DATA_ONLY)
    assert RESPONSE_PROVENANCE.trust_zone is TrustZone.UNTRUSTED
    assert TaintFlag.USER_CONTROLLED in RESPONSE_PROVENANCE.taint_flags
