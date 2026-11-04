"""M46 gate: snapshots (FR-168 to FR-170, AC-104, NFR-47, NFR-49; DECISION-bafa7be8).

`Runner.snapshot_workspace` copies a run's or a plan's workspace, between commands, into a snapshot
of the same tenancy; `WorkspaceSpec(from_snapshot=...)` starts a new workspace as its copy. A
snapshot is measured and refused past its workspace's `max_workspace_bytes`; it is refused while a
command or a write is under way, and one started while the copy runs waits for it. Another
tenancy's snapshot, a released one, and one past the new spec's bound fail the new workspace
`WorkspaceUnavailable`. A snapshot is a `workspace_snapshots` row; `release_snapshot` removes it,
`aclose()` removes it unless its workspace was kept, and reaping removes a dead owner's.

The gates run on `FakeProvider`; tests marked `docker` hold the same against a real daemon. Store
calls from async tests go to a worker thread (FR-81); the persisted half removes every row it
writes, by its own tenancies.
"""

from __future__ import annotations

import asyncio
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
from agentsdk.errors import WorkspaceRefused, WorkspaceUnavailable
from agentsdk.events import EventType
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.postgres import RunScope
from agentsdk.primitives import Message, Role, ToolCall
from agentsdk.tools import ApprovalPolicy
from agentsdk.workspace import DockerProvider, FakeProvider, SnapshotRef, WorkspaceSpec
from agentsdk.workspace import owner as ow

REPO = Path(__file__).resolve().parent.parent
DSN = normalise_database_url(os.environ.get("DATABASE_URL"))
PREFIX = "SYN-m46-"
PROJECT = "p-m46"
STORES = ["memory", "postgres"]
FAKE_IMAGE = "fake/workspace@sha256:" + "0" * 64
# The example image, present on the host and never pulled (FR-137); docker tests need it.
IMAGE = "python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df"
TOOLS = ("run_command", "read_workspace_file", "write_workspace_file", "list_workspace")


@pytest.fixture(autouse=True)
def remove_what_the_test_wrote():
    yield
    if DSN:
        with psycopg.connect(DSN, autocommit=True) as conn:
            ids = [r[0] for r in conn.execute("SELECT run_id FROM runs WHERE tenant_id LIKE %s", (PREFIX + "%",))]
            conn.execute("DELETE FROM workspace_snapshots WHERE tenant_id LIKE %s", (PREFIX + "%",))
            conn.execute("DELETE FROM workspace_commands WHERE tenant_id LIKE %s", (PREFIX + "%",))
            conn.execute("DELETE FROM workspaces WHERE tenant_id LIKE %s", (PREFIX + "%",))
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


def tenancy() -> str:
    return f"{PREFIX}{uuid.uuid4().hex[:10]}"


# =================================================================================================
# The runs
# =================================================================================================


async def _nap(cmd):
    await cmd.sleep(float(cmd.argv[1]))
    return 0


async def _make(cmd):
    """make PATH TEXT [TIMES]: writes TEXT, repeated TIMES, to PATH."""
    cmd.files.write(cmd.argv[1], cmd.argv[2].encode() * int(cmd.argv[3] if len(cmd.argv) > 3 else 1))
    return 0


async def _link(cmd):
    """ln -s TARGET PATH, as on Docker."""
    cmd.files.link(cmd.argv[3], cmd.argv[2])
    return 0


COMMANDS = {"nap": _nap, "make": _make, "ln": _link}


def provider():
    return FakeProvider(commands=COMMANDS)


def respond(content="", calls=()):
    return ModelResponse(message=Message(role=Role.ASSISTANT, content=content, tool_calls=tuple(calls)),
                         stop_reason=StopReason.TOOL_CALLS if calls else StopReason.END_TURN, usage=Usage(5, 1, 6))


class Turns:
    """The model: the task holds a JSON list of turns, each a list of [tool, arguments]. A turn
    `"pause"` waits for `go` before the next, telling `paused` -- the application's moment
    between commands."""

    def __init__(self):
        self.paused, self.go = asyncio.Event(), asyncio.Event()

    async def send(self, request):
        text = request.messages[0].content
        written = json.loads(text[text.index("["):].split("\n")[0])
        turns = [t for t in written if t != "pause"]
        # A pause holds back the turn after it: that turn's index among the turns themselves.
        paused_before = {i - written[:i].count("pause") for i, t in enumerate(written) if t == "pause"}
        turn = sum(1 for m in request.messages if m.role is Role.ASSISTANT)
        if turn in paused_before:
            self.paused.set()
            await self.go.wait()
        if turn < len(turns):
            return respond(calls=[ToolCall(id=f"t{turn}-{i}", name=name, arguments=args)
                                  for i, (name, args) in enumerate(turns[turn])])
        return respond("done")


def space(**fields) -> WorkspaceSpec:
    return WorkspaceSpec(image=fields.pop("image", FAKE_IMAGE), run_command_approval=ApprovalPolicy.AUTO, **fields)


def agent(spec: WorkspaceSpec, agent_id="coder") -> AgentSpec:
    return AgentSpec(id=agent_id, instructions="use the workspace", preferred_model="m:x", tool_profile=TOOLS,
                     workspace=spec)


def persistence(store):
    return Persistence.postgres(DSN, create_schema=False) if store == "postgres" else None


def runner_for(prov, store="memory", model=None):
    return Runner({"m": model or Turns()}, workspace_provider=prov, persistence=persistence(store))


async def run(runner, spec, turns, tenant):
    return await runner.run(agent(spec), json.dumps(turns), RunConfig(tenant_id=tenant, project_id=PROJECT))


async def results(runner, result, tenant):
    """Each tool call's result, by its id, as the run's history holds it."""
    sessions = runner._sessions_for(RunScope(run_id=result.run_id, tenant_id=tenant, project_id=PROJECT))
    history = await asyncio.to_thread(sessions.history, result.run_id)
    return {r.tool_call_id: r for m in history for r in m.tool_results}


def error_types(result) -> dict:
    """Each call's error class, from its ToolCalled event (DECISION-94bfee7c)."""
    return {e.payload["tool_call_id"]: e.payload.get("error_type")
            for e in result.events if e.event_type is EventType.TOOL_CALLED}


def created(result) -> str:
    [payload] = [e.payload for e in result.events if e.event_type is EventType.WORKSPACE_CREATED]
    return payload["workspace_id"]


def stored(sql, *args):
    with psycopg.connect(DSN) as conn:
        return conn.execute(sql, args).fetchall()


async def snapshot_rows(runner, store, tenant):
    """Every snapshot row of the tenancy as (snapshot_id, workspace_id, size_bytes, removed_reason)."""
    if store == "postgres":
        return [(str(s), str(w), size, why) for s, w, size, why in await asyncio.to_thread(
            stored, "SELECT snapshot_id, workspace_id, size_bytes, removed_reason FROM workspace_snapshots"
                    " WHERE tenant_id = %s ORDER BY created_at", tenant)]
    return [(s.snapshot_id, s.workspace_id, s.size_bytes, s.removed_reason)
            for s in runner._workspace_store().snapshots(tenant, PROJECT)]


async def recorded_spec(runner, store, result, tenant):
    """The workspace spec recorded for the run: its manifest's on Postgres, which alone keeps
    manifests, and its workspaces row's in memory -- both `WorkspaceSpec.record()`."""
    if store == "postgres":
        [(recorded,)] = await asyncio.to_thread(
            stored, "SELECT workspace FROM execution_manifests WHERE run_id = %s::uuid", result.run_id)
        return recorded
    [row] = runner._workspace_store().workspaces(tenant, PROJECT, result.run_id)
    return row.limits


BUILT = [[["write_workspace_file", {"path": "a.txt", "content": "one"}],
          ["run_command", {"argv": ["make", "dir/b.txt", "two"]}],
          ["run_command", {"argv": ["ln", "-s", "a.txt", "l"]}]]]
READ_BACK = [[["read_workspace_file", {"path": "a.txt"}], ["read_workspace_file", {"path": "dir/b.txt"}],
              ["list_workspace", {"path": "."}]]]


def refused(result) -> bool:
    """Every call failed and no workspace was made: the error's class is not on the record, and no
    check keys on its message (DECISION-94bfee7c)."""
    return (set(error_types(result).values()) == {"ToolExecutionError"}
            and not any(e.event_type is EventType.WORKSPACE_CREATED for e in result.events))


async def _kept_snapshot(runner, tenant, **fields):
    """A kept workspace built up by one run, then snapshotted once that run has ended."""
    built = await run(runner, space(keep=True, **fields), BUILT, tenant)
    assert built.status is RunStatus.COMPLETED, built.error
    workspace_id = created(built)
    return workspace_id, await runner.snapshot_workspace(tenant, PROJECT, workspace_id)


# =================================================================================================
# FR-168: taking one
# =================================================================================================


@pytest.mark.parametrize("store", STORES)
async def test_a_snapshot_restores_the_state_at_its_taking_links_kept(store):
    """FR-168, FR-169, AC-104: built, snapshotted between commands, changed; a second workspace
    started from the snapshot reads the state at the snapshot, its link a link."""
    tenant, prov = tenancy(), provider()
    model = Turns()
    runner = runner_for(prov, store, model)
    handle = await runner.start(agent(space()), json.dumps(BUILT + ["pause"] + [[
        ["write_workspace_file", {"path": "a.txt", "content": "changed"}],
        ["write_workspace_file", {"path": "c.txt", "content": "later"}]]]),
        RunConfig(tenant_id=tenant, project_id=PROJECT))
    await asyncio.wait_for(model.paused.wait(), 30)
    workspace_id = [h.workspace_id for h in await prov.list_owned({})][0]
    ref = await runner.snapshot_workspace(tenant, PROJECT, workspace_id)
    model.go.set()
    changed = await handle.result()
    assert changed.status is RunStatus.COMPLETED, changed.error
    assert isinstance(ref, SnapshotRef) and ref.workspace_id == workspace_id
    assert (ref.tenant_id, ref.project_id) == (tenant, PROJECT) and ref.size_bytes > 0

    restored = await run(runner, space(from_snapshot=ref.snapshot_id), READ_BACK, tenant)
    assert restored.status is RunStatus.COMPLETED, restored.error
    got = await results(runner, restored, tenant)
    assert got["t0-0"].content == "one" and got["t0-1"].content == "two"
    assert "l\tlink" in got["t0-2"].content, got["t0-2"].content
    assert "c.txt" not in got["t0-2"].content, "a file written after the snapshot is there"
    assert created(restored) != workspace_id
    [(snapshot_id, of, size, why)] = await snapshot_rows(runner, store, tenant)
    assert (snapshot_id, of, size, why) == (ref.snapshot_id, workspace_id, ref.size_bytes, None)
    assert (await recorded_spec(runner, store, restored, tenant))["from_snapshot"] == ref.snapshot_id
    await runner.aclose()


@pytest.mark.parametrize("store", STORES)
async def test_a_snapshot_past_its_workspaces_bound_is_refused_and_nothing_of_it_remains(store):
    tenant, prov = tenancy(), provider()
    runner = runner_for(prov, store)
    built = await run(runner, space(keep=True, max_workspace_bytes=8192),
                      [[["run_command", {"argv": ["make", "big.bin", "x", "20000"]}]]], tenant)
    with pytest.raises(WorkspaceRefused, match="max_workspace_bytes"):
        await runner.snapshot_workspace(tenant, PROJECT, created(built))
    assert await prov.list_snapshots({}) == [] and await snapshot_rows(runner, store, tenant) == []
    await runner.aclose()


async def test_a_snapshot_is_refused_while_a_command_runs():
    """DECISION-bafa7be8: between commands -- one under way refuses it, and nothing is taken."""
    tenant, prov = tenancy(), provider()
    runner = runner_for(prov)
    handle = await runner.start(agent(space()), json.dumps([[["run_command", {"argv": ["nap", "30"]}]]]),
                                RunConfig(tenant_id=tenant, project_id=PROJECT))
    async for event in handle.events():
        if event.event_type is EventType.WORKSPACE_COMMAND_STARTED:
            workspace_id = event.payload["workspace_id"]
            break
    with pytest.raises(WorkspaceRefused, match="command"):
        await runner.snapshot_workspace(tenant, PROJECT, workspace_id)
    handle.cancel()
    await handle.result()
    assert await prov.list_snapshots({}) == []
    await runner.aclose()


class _SlowCopy(FakeProvider):
    """Its copy waits for `go`, telling `copying` first."""

    def __init__(self):
        super().__init__(commands=COMMANDS)
        self.copying, self.go = asyncio.Event(), asyncio.Event()

    async def snapshot(self, handle, labels):
        self.copying.set()
        await self.go.wait()
        return await super().snapshot(handle, labels)


async def test_a_write_begun_while_the_copy_runs_waits_for_it():
    """DECISION-bafa7be8: the copy holds the workspace; a write asked meanwhile lands after it."""
    tenant, prov, model = tenancy(), _SlowCopy(), Turns()
    runner = runner_for(prov, model=model)
    handle = await runner.start(agent(space()), json.dumps(BUILT + ["pause"] + [[
        ["write_workspace_file", {"path": "a.txt", "content": "changed"}]]]),
        RunConfig(tenant_id=tenant, project_id=PROJECT))
    await asyncio.wait_for(model.paused.wait(), 30)
    [owned] = await prov.list_owned({})
    taking = asyncio.ensure_future(runner.snapshot_workspace(tenant, PROJECT, owned.workspace_id))
    await asyncio.wait_for(prov.copying.wait(), 30)
    model.go.set()
    await asyncio.sleep(0.3)
    assert prov._spaces[owned.workspace_id].files.read("a.txt") == b"one", "the write did not wait for the copy"
    prov.go.set()
    ref = await asyncio.wait_for(taking, 30)
    changed = await asyncio.wait_for(handle.result(), 30)
    assert changed.status is RunStatus.COMPLETED and not any(error_types(changed).values()), error_types(changed)
    restored = await run(runner, space(from_snapshot=ref.snapshot_id), READ_BACK, tenant)
    assert (await results(runner, restored, tenant))["t0-0"].content == "one"
    await runner.aclose()


async def _held_copy(runner, prov, model, tenant, turns, **fields):
    """A run of `turns` after BUILT, its workspace kept, let go while a copy of it is held."""
    handle = await runner.start(agent(space(keep=True, **fields)), json.dumps(BUILT + ["pause"] + turns),
                                RunConfig(tenant_id=tenant, project_id=PROJECT))
    await asyncio.wait_for(model.paused.wait(), 30)
    [owned] = await prov.list_owned({})
    taking = asyncio.ensure_future(runner.snapshot_workspace(tenant, PROJECT, owned.workspace_id))
    await asyncio.wait_for(prov.copying.wait(), 30)
    model.go.set()
    return handle, taking, owned


def _ended(result) -> list[dict]:
    return [e.payload for e in result.events if e.event_type is EventType.WORKSPACE_COMMAND_ENDED]


async def test_a_command_waits_for_the_copy_only_within_its_own_timeout():
    """DECISION-f15a2771 (round 1 finding 4): the wait is the command's own deadline, not 600 s."""
    tenant, prov, model = tenancy(), _SlowCopy(), Turns()
    runner = runner_for(prov, model=model)
    handle, taking, _ = await _held_copy(runner, prov, model, tenant, [[
        ["run_command", {"argv": ["nap", "0"], "timeout_seconds": 2}]]])
    started = time.monotonic()
    ended = await asyncio.wait_for(handle.result(), 30)
    assert time.monotonic() - started < 20
    assert not any(e.payload["argv"] == ["nap", "0"] for e in ended.events
                   if e.event_type is EventType.WORKSPACE_COMMAND_STARTED), "the command ran during the copy"
    assert error_types(ended), "the command's refusal never reached the model"
    prov.go.set()
    await asyncio.wait_for(taking, 30)
    await runner.aclose()


async def test_the_wait_for_the_copy_is_taken_from_the_commands_timeout():
    tenant, prov, model = tenancy(), _SlowCopy(), Turns()
    runner = runner_for(prov, model=model)
    handle, taking, _ = await _held_copy(runner, prov, model, tenant, [[
        ["run_command", {"argv": ["nap", "3"], "timeout_seconds": 4}]]])
    await asyncio.sleep(2)
    prov.go.set()
    await asyncio.wait_for(taking, 30)
    ended = await asyncio.wait_for(handle.result(), 30)
    assert _ended(ended)[-1]["killed"] == "timeout", "the command was given its whole timeout after the wait"
    await runner.aclose()


async def test_a_write_waits_for_the_copy_only_within_max_command_seconds():
    tenant, prov, model = tenancy(), _SlowCopy(), Turns()
    runner = runner_for(prov, model=model)
    handle, taking, owned = await _held_copy(runner, prov, model, tenant, [[
        ["write_workspace_file", {"path": "a.txt", "content": "changed"}]]], max_command_seconds=2)
    ended = await asyncio.wait_for(handle.result(), 30)
    assert error_types(ended), "the write's refusal never reached the model"
    prov.go.set()
    await asyncio.wait_for(taking, 30)
    assert prov._spaces[owned.workspace_id].files.read("a.txt") == b"one", "the write landed after all"
    await runner.aclose()


class _InUse(FakeProvider):
    """Refuses `refusals` removals of a snapshot, as Docker does one a restore is reading."""

    def __init__(self, refusals):
        super().__init__(commands=COMMANDS)
        self.refusals = refusals

    async def remove_snapshot(self, snapshot_id):
        if self.refusals:
            self.refusals -= 1
            raise WorkspaceUnavailable("the volume is in use")
        await super().remove_snapshot(snapshot_id)


async def _unkept_snapshot(runner, prov, model, tenant):
    handle = await runner.start(agent(space()), json.dumps(BUILT + ["pause"]),
                                RunConfig(tenant_id=tenant, project_id=PROJECT))
    await asyncio.wait_for(model.paused.wait(), 30)
    [owned] = await prov.list_owned({})
    ref = await runner.snapshot_workspace(tenant, PROJECT, owned.workspace_id)
    model.go.set()
    await handle.result()
    return ref


async def test_a_release_refused_while_the_snapshot_is_read_raises_and_may_be_asked_again():
    """DECISION-f15a2771 (round 1 finding 3)."""
    tenant, prov = tenancy(), _InUse(1)
    runner = runner_for(prov)
    _, ref = await _kept_snapshot(runner, tenant)
    with pytest.raises(WorkspaceUnavailable):
        await runner.release_snapshot(tenant, PROJECT, ref.snapshot_id)
    assert [s.snapshot_id for s in await prov.list_snapshots({})] == [ref.snapshot_id]
    assert [why for *_, why in await snapshot_rows(runner, "memory", tenant)] == [None]
    assert await runner.release_snapshot(tenant, PROJECT, ref.snapshot_id) is True
    assert [why for *_, why in await snapshot_rows(runner, "memory", tenant)] == ["released"]
    await runner.aclose()


async def test_aclose_tries_again_a_removal_refused_while_the_snapshot_is_read():
    tenant, prov, model = tenancy(), _InUse(2), Turns()
    runner = runner_for(prov, model=model)
    ref = await _unkept_snapshot(runner, prov, model, tenant)
    await asyncio.wait_for(runner.aclose(), 60)
    assert await prov.list_snapshots({}) == [], "aclose() never tried the removal again"
    assert {s: why for s, _, _, why in await snapshot_rows(runner, "memory", tenant)} == {ref.snapshot_id: "aclose"}


async def test_aclose_leaves_a_snapshot_still_read_past_its_bound_to_reaping(monkeypatch):
    from agentsdk.workspace import manager as wm

    monkeypatch.setattr(wm, "_REMOVE_SECONDS", 2.0)
    tenant, prov, model = tenancy(), _InUse(10**6), Turns()
    runner = runner_for(prov, model=model)
    ref = await _unkept_snapshot(runner, prov, model, tenant)
    started = time.monotonic()
    await asyncio.wait_for(runner.aclose(), 30)
    assert time.monotonic() - started < 15, "aclose() waited past its bound"
    assert [s.snapshot_id for s in await prov.list_snapshots({})] == [ref.snapshot_id]
    assert [why for *_, why in await snapshot_rows(runner, "memory", tenant)] == [None], "recorded removed, yet kept"
    prov.refusals = 0
    await prov.remove_snapshot(ref.snapshot_id)


async def test_only_a_runs_or_a_plans_workspace_of_this_tenancy_is_snapshotted():
    """DECISION-bafa7be8: a sandboxed server's workspace is refused; an unknown id, or one of another
    tenancy, is no workspace of this Runner's."""
    tenant, prov = tenancy(), provider()
    runner = runner_for(prov)
    workspace_id, kept = await _kept_snapshot(runner, tenant)
    with pytest.raises(WorkspaceUnavailable):
        await runner.snapshot_workspace(tenancy(), PROJECT, workspace_id)
    with pytest.raises(WorkspaceUnavailable):
        await runner.snapshot_workspace(tenant, "elsewhere", workspace_id)
    with pytest.raises(WorkspaceUnavailable):
        await runner.snapshot_workspace(tenant, PROJECT, str(uuid.uuid4()))
    server = await runner._workspaces.for_servers().open_server(space(), {}, tenant, PROJECT, seconds=5)
    with pytest.raises(WorkspaceRefused, match="server"):
        await runner.snapshot_workspace(tenant, PROJECT, server.handle.workspace_id)
    await runner.aclose()
    assert [s.snapshot_id for s in await prov.list_snapshots({})] == [kept.snapshot_id], "a refusal took a snapshot"


async def test_a_second_snapshot_while_one_is_taken_is_refused():
    tenant, prov = tenancy(), _SlowCopy()
    runner = runner_for(prov)
    built = await run(runner, space(keep=True), BUILT, tenant)
    taking = asyncio.ensure_future(runner.snapshot_workspace(tenant, PROJECT, created(built)))
    await asyncio.wait_for(prov.copying.wait(), 30)
    with pytest.raises(WorkspaceRefused, match="being taken"):
        # Bounded: unrefused, it would wait out the first copy (M46 mutation run).
        await asyncio.wait_for(runner.snapshot_workspace(tenant, PROJECT, created(built)), 10)
    prov.go.set()
    ref = await asyncio.wait_for(taking, 30)
    assert [s.snapshot_id for s in await prov.list_snapshots({})] == [ref.snapshot_id]
    assert await runner.release_snapshot(tenant, PROJECT, ref.snapshot_id) is True
    await runner.aclose()


class _HangsAfterTheCopy(FakeProvider):
    """Its copy is made, then never returns: the taking is cancelled with a snapshot in hand."""

    def __init__(self):
        super().__init__(commands=COMMANDS)
        self.copied = asyncio.Event()

    async def snapshot(self, handle, labels):
        await super().snapshot(handle, labels)
        self.copied.set()
        await asyncio.Event().wait()


@pytest.mark.parametrize("store", STORES)
async def test_a_snapshot_cancelled_or_unrecorded_leaves_nothing(store, monkeypatch):
    """Nothing unrecorded is kept, however the taking ended -- and the workspace takes commands again."""
    tenant, prov = tenancy(), _HangsAfterTheCopy()
    runner = runner_for(prov, store)
    built = await run(runner, space(keep=True), BUILT, tenant)
    taking = asyncio.ensure_future(runner.snapshot_workspace(tenant, PROJECT, created(built)))
    await asyncio.wait_for(prov.copied.wait(), 30)
    assert len(await prov.list_snapshots({})) == 1, "the copy was never made, so its removal is not shown"
    taking.cancel()
    with pytest.raises(asyncio.CancelledError):
        await taking
    assert await prov.list_snapshots({}) == [] and await snapshot_rows(runner, store, tenant) == []

    store_of = type(runner._workspace_store())

    def fails(self, record):
        raise RuntimeError("the store went away")

    monkeypatch.setattr(store_of, "record_snapshot", fails)
    plain = FakeProvider(commands=COMMANDS)
    runner = runner_for(plain, store)
    built = await run(runner, space(keep=True), BUILT, tenant)
    with pytest.raises(WorkspaceUnavailable, match="RuntimeError"):
        await runner.snapshot_workspace(tenant, PROJECT, created(built))
    assert await plain.list_snapshots({}) == []
    monkeypatch.undo()
    assert await snapshot_rows(runner, store, tenant) == []
    ref = await runner.snapshot_workspace(tenant, PROJECT, created(built))  # nothing was left holding it
    assert await runner.release_snapshot(tenant, PROJECT, ref.snapshot_id) is True
    await runner.aclose()


async def test_the_copy_runs_hardened_as_a_command_over_both_volumes_and_pulls_nothing():
    """NFR-47: a snapshot leaves its workspace only inside Docker -- copied by a container with no
    network, every capability dropped, a read-only root, the command limits, the source read-only."""
    from agentsdk.workspace import WorkspaceHandle
    from agentsdk.workspace import docker as dk

    class Recording(DockerProvider):
        def __init__(self):
            super().__init__()
            self.calls = []

        async def _cli(self, *args, **kwargs):
            self.calls.append(list(args))
            return 0, b"8\t/workspace\n", b""

    prov = Recording()
    handle = WorkspaceHandle(workspace_id="w-1", spec=space(image=IMAGE), labels={"agentsdk.workspace": "w-1"})
    assert await prov._copied(handle, dk.snapshot_volume("s-1"), dk.volume_name("w-1")) == 8192
    [args] = [c for c in prov.calls if c[0] == "run"]
    joined = " ".join(args)
    for part in ("--pull never", "--network none", "--cap-drop ALL", "--security-opt no-new-privileges",
                 "--read-only", f"--user {dk.UID}:{dk.UID}", "--log-driver none",
                 "type=volume,src=agentsdk-snap-s-1,dst=/snapshot,readonly",
                 "type=volume,src=agentsdk-w-1,dst=/workspace,volume-nocopy", f"--entrypoint= {IMAGE}"):
        assert part in joined, part
    assert args[args.index("--memory") + 1] == str(handle.spec.memory_bytes)
    assert any(c[:2] == ["rm", "-fv"] for c in prov.calls), "the copy's container was not removed"


# =================================================================================================
# FR-169: starting from one
# =================================================================================================


@pytest.mark.parametrize("store", STORES)
async def test_another_tenancys_a_released_and_an_oversized_snapshot_fail_the_workspace(store):
    """FR-169, AC-104: each fails the new workspace WorkspaceUnavailable, and no workspace is made."""
    tenant, prov = tenancy(), provider()
    runner = runner_for(prov, store)
    _, ref = await _kept_snapshot(runner, tenant)
    _, released = await _kept_snapshot(runner, tenant)
    assert await runner.release_snapshot(tenant, PROJECT, released.snapshot_id) is True
    before = {h.workspace_id for h in await prov.list_owned({})}
    for owner, spec in ((tenancy(), space(from_snapshot=ref.snapshot_id)),
                        (tenant, space(from_snapshot=released.snapshot_id)),
                        (tenant, space(from_snapshot=str(uuid.uuid4()))),
                        (tenant, space(from_snapshot=ref.snapshot_id, max_workspace_bytes=ref.size_bytes - 1))):
        assert refused(await run(runner, spec, READ_BACK, owner)), spec
    assert {h.workspace_id for h in await prov.list_owned({})} == before, "a refused restore made a workspace"
    within = await run(runner, space(from_snapshot=ref.snapshot_id, max_workspace_bytes=ref.size_bytes), READ_BACK,
                       tenant)
    assert (await results(runner, within, tenant))["t0-0"].content == "one", "a snapshot at the bound is refused"
    await runner.aclose()


class _TrimsLabels(FakeProvider):
    """Matches a label as Docker's `--filter label=` does: a value's trailing whitespace ignored."""

    def __init__(self):
        super().__init__(commands=COMMANDS)

    async def list_snapshots(self, labels):
        return [taken for taken, _ in self._snapshots.values()
                if all(taken.labels.get(k, "").rstrip() == v.rstrip() for k, v in labels.items())]


ALMOST = (" ", "\t", "\n")


async def test_a_tenancy_differing_only_by_trailing_whitespace_reaches_no_snapshot():
    """M46 pre-review F1: Docker's filter matched `T ` to T, and the manager trusted it -- another
    tenancy read T's files and released T's snapshot. Labels are compared exactly."""
    tenant, prov = tenancy(), _TrimsLabels()
    runner = runner_for(prov)
    _, ref = await _kept_snapshot(runner, tenant)
    for other in [(tenant + tail, PROJECT) for tail in ALMOST] + [(tenant, PROJECT + tail) for tail in ALMOST]:
        made = await runner.run(agent(space(from_snapshot=ref.snapshot_id)), json.dumps(READ_BACK),
                                RunConfig(tenant_id=other[0], project_id=other[1]))
        assert refused(made), other
        assert await runner.release_snapshot(*other, ref.snapshot_id) is False, other
    assert [s.snapshot_id for s in await prov.list_snapshots({})] == [ref.snapshot_id]
    assert await runner.release_snapshot(tenant, PROJECT, ref.snapshot_id) is True
    await runner.aclose()


async def test_a_servers_workspace_never_starts_from_a_snapshot():
    """M46 pre-review F2: a server's spec naming another tenancy's snapshot was opened as its copy,
    past its bound too. Its configuration refuses it, and the manager does too."""
    from agentsdk.workspace import server_files

    with pytest.raises(ValueError, match="never from a snapshot"):
        server_files({}, None, space(from_snapshot=str(uuid.uuid4())), {})
    tenant, prov = tenancy(), provider()
    runner = runner_for(prov)
    _, ref = await _kept_snapshot(runner, tenant)
    with pytest.raises(WorkspaceUnavailable, match="never from a snapshot"):
        await runner._workspaces.for_servers().open_server(space(from_snapshot=ref.snapshot_id), {}, tenant, PROJECT,
                                                           seconds=5)
    assert {h.labels.get("agentsdk.run") is None for h in await prov.list_owned({})} == {False}
    assert await runner.release_snapshot(tenant, PROJECT, ref.snapshot_id) is True
    await runner.aclose()


async def test_a_record_that_lands_after_its_taking_was_cancelled_is_followed_by_its_removal():
    """M46 pre-review F3: the write went on in its thread after the cancel, and left a row of a
    snapshot that was removed. Its removal is recorded once it lands, before aclose() returns."""
    import threading

    from agentsdk.workspace.records import InMemoryWorkspaceStore

    class Slow(InMemoryWorkspaceStore):
        def __init__(self):
            super().__init__()
            self.writing, self.land = threading.Event(), threading.Event()

        def record_snapshot(self, record):
            self.writing.set()
            self.land.wait(30)
            super().record_snapshot(record)

    tenant, prov = tenancy(), provider()
    runner = runner_for(prov)
    built = await run(runner, space(keep=True), BUILT, tenant)
    store = Slow()
    runner._workspaces.store = lambda: store
    taking = asyncio.ensure_future(runner.snapshot_workspace(tenant, PROJECT, created(built)))
    assert await asyncio.to_thread(store.writing.wait, 30)
    taking.cancel()
    with pytest.raises(asyncio.CancelledError):
        await taking
    assert await prov.list_snapshots({}) == [], "the cancelled snapshot's copy was kept"
    store.land.set()
    await runner.aclose()
    [row] = store.snapshots(tenant, PROJECT)
    assert row.removed_reason == "aclose", "a row says live with no snapshot behind it"


async def test_a_copy_whose_removal_failed_is_removed_with_aclose():
    """M46 pre-review F6: a failed taking whose removal also failed left a copy nothing tracked."""
    class Stuck(FakeProvider):
        def __init__(self):
            super().__init__(commands=COMMANDS)
            self.removable = False

        async def snapshot(self, handle, labels):
            await super().snapshot(handle, labels)
            raise RuntimeError("the copy went wrong after it was made")

        async def remove_snapshot(self, snapshot_id):
            if not self.removable:
                raise RuntimeError("the daemon refused")
            await super().remove_snapshot(snapshot_id)

    tenant, prov = tenancy(), Stuck()
    runner = runner_for(prov)
    built = await run(runner, space(keep=True), BUILT, tenant)
    with pytest.raises(WorkspaceUnavailable):
        await runner.snapshot_workspace(tenant, PROJECT, created(built))
    assert len(await prov.list_snapshots({})) == 1
    prov.removable = True
    await runner.aclose()
    assert await prov.list_snapshots({}) == [], "aclose() never tried the copy again"


async def test_the_fake_starts_no_workspace_from_a_snapshot_it_does_not_hold():
    """A snapshot released between the manager's check and the creation fails the provider's own."""
    prov = provider()
    with pytest.raises(WorkspaceUnavailable):
        await prov.create(space(from_snapshot=str(uuid.uuid4())), {"agentsdk.workspace": str(uuid.uuid4())})
    assert await prov.list_owned({}) == []


def test_the_tracked_tables_include_the_snapshots_and_the_connections():
    """NFR-16, NFR-38: a test that leaves a row in either fails AC-44; neither carries a run id, so
    no run-id cleanup reaches it. `workspace_connections` (M44) was found missing at M46."""
    from tests import conftest

    assert {"workspace_snapshots", "workspace_connections"} <= set(conftest._RUN_TABLES)


def test_a_spec_without_a_snapshot_records_what_it_did_before():
    """NFR-49: `from_snapshot` is named only when set."""
    assert "from_snapshot" not in space().record()
    assert space(from_snapshot="s-1").record()["from_snapshot"] == "s-1"
    with pytest.raises(ValueError):
        space(from_snapshot="")
    with pytest.raises(ValueError):
        space(from_snapshot=3)


# =================================================================================================
# FR-168: its lifetime
# =================================================================================================


@pytest.mark.parametrize("store", STORES)
async def test_a_released_snapshot_is_gone_and_recorded_released(store):
    tenant, prov = tenancy(), provider()
    runner = runner_for(prov, store)
    _, ref = await _kept_snapshot(runner, tenant)
    assert await runner.release_snapshot(tenancy(), PROJECT, ref.snapshot_id) is False
    assert await runner.release_snapshot(tenant, PROJECT, str(uuid.uuid4())) is False
    assert [s.snapshot_id for s in await prov.list_snapshots({})] == [ref.snapshot_id]
    assert await runner.release_snapshot(tenant, PROJECT, ref.snapshot_id) is True
    assert await prov.list_snapshots({}) == []
    assert [why for *_, why in await snapshot_rows(runner, store, tenant)] == ["released"]
    assert await runner.release_snapshot(tenant, PROJECT, ref.snapshot_id) is False
    await runner.aclose()


@pytest.mark.parametrize("store", STORES)
async def test_aclose_keeps_a_kept_workspaces_snapshot_for_another_runner_and_removes_the_rest(store):
    """DECISION-bafa7be8: a kept workspace's snapshot outlives its Runner, and a Runner of the same
    provider and tenancy starts from it; a snapshot of a workspace not kept goes with aclose()."""
    tenant, prov, model = tenancy(), provider(), Turns()
    runner = runner_for(prov, store, model)
    _, kept = await _kept_snapshot(runner, tenant)
    handle = await runner.start(agent(space()), json.dumps(BUILT + ["pause"]),
                                RunConfig(tenant_id=tenant, project_id=PROJECT))
    await asyncio.wait_for(model.paused.wait(), 30)
    [plain_id] = [h.workspace_id for h in await prov.list_owned({}) if h.workspace_id != kept.workspace_id]
    plain = await runner.snapshot_workspace(tenant, PROJECT, plain_id)
    model.go.set()
    await handle.result()
    await runner.aclose()
    assert [s.snapshot_id for s in await prov.list_snapshots({})] == [kept.snapshot_id]
    assert await prov.list_owned({}) == [], "aclose() kept a workspace"
    rows = {s: why for s, _, _, why in await snapshot_rows(runner, store, tenant)}
    assert rows == {kept.snapshot_id: None, plain.snapshot_id: "aclose"}

    later = runner_for(prov, store)
    restored = await run(later, space(from_snapshot=kept.snapshot_id), READ_BACK, tenant)
    assert (await results(later, restored, tenant))["t0-0"].content == "one"
    assert await later.release_snapshot(tenant, PROJECT, kept.snapshot_id) is True
    assert await prov.list_snapshots({}) == []
    await later.aclose()


def _dead_label() -> str:
    host, pid, start = ow.this_owner().rsplit(":", 2)
    return f"{host}:{pid}:{int(start) + 1}"


@pytest.mark.parametrize("store", STORES)
async def test_a_runner_reaps_a_dead_owners_snapshots_and_records_them(store):
    tenant, prov = tenancy(), provider()
    dead = runner_for(prov, store)
    dead._workspaces.owner = _dead_label()
    _, gone = await _kept_snapshot(dead, tenant)
    await dead.aclose()
    alive = runner_for(prov, store)
    _, mine = await _kept_snapshot(alive, tenant)
    assert {s.snapshot_id for s in await prov.list_snapshots({})} == {mine.snapshot_id}
    if store == "postgres":
        rows = {s: why for s, _, _, why in await snapshot_rows(alive, store, tenant)}
        assert rows == {gone.snapshot_id: "reaped", mine.snapshot_id: None}
    await alive.aclose()


# =================================================================================================
# FR-170: example 40
# =================================================================================================


def _example():
    import importlib.util

    spec = importlib.util.spec_from_file_location("ex40_snapshots", REPO / "scripts" / "40_snapshots.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("store", STORES)
async def test_example_40_holds_every_check_offline(store):
    checks = await _example().offline(store, "fake", tenant=tenancy())
    assert len(checks) >= 7 and [(label, why) for label, passed, why in checks if passed is not True] == []


async def test_example_40_passes_nothing_when_no_snapshot_is_taken(monkeypatch):
    async def refused(self, handle, labels):
        raise WorkspaceUnavailable("no snapshot is taken")

    monkeypatch.setattr(FakeProvider, "snapshot", refused)
    with pytest.raises(WorkspaceUnavailable):
        await _example().offline("memory", "fake", tenant=tenancy())


async def test_example_40_fails_its_restore_check_when_the_restore_starts_empty(monkeypatch):
    real = FakeProvider.create

    async def empty(self, spec, labels):
        from dataclasses import replace

        return await real(self, replace(spec, from_snapshot=None), labels)

    monkeypatch.setattr(FakeProvider, "create", empty)
    checks = {label: passed for label, passed, _ in await _example().offline("memory", "fake", tenant=tenancy())}
    assert checks["a second workspace started from the snapshot reads the state at the snapshot"] is False
    assert checks["the kept workspace was changed after the snapshot (the control)"] is True


async def test_example_40_fails_its_control_when_nothing_changes_after_the_snapshot():
    module = _example()
    module.BUILT = module.BUILT[:2]  # the pause, and no change after it
    checks = {label: passed for label, passed, _ in await module.offline("memory", "fake", tenant=tenancy())}
    assert checks["the kept workspace was changed after the snapshot (the control)"] is False


async def test_example_40_fails_its_control_cleanly_when_its_writes_never_land(monkeypatch):
    """M46 pre-review: with no write landing, the control's read raised instead of failing."""
    async def nothing(self, handle, data):
        return None

    monkeypatch.setattr(FakeProvider, "import_", nothing)
    checks = {label: passed for label, passed, _ in await _example().offline("memory", "fake", tenant=tenancy())}
    assert checks["the kept workspace was changed after the snapshot (the control)"] is False


async def test_example_40_fails_its_tenancy_check_when_another_tenancy_may_start_from_it(monkeypatch):
    from agentsdk.workspace.manager import WorkspaceManager

    async def unchecked(self, snapshot_id, tenant_id, project_id):
        return None

    monkeypatch.setattr(WorkspaceManager, "usable_snapshot", unchecked)
    checks = {label: passed for label, passed, _ in await _example().offline("memory", "fake", tenant=tenancy())}
    assert checks["another tenancy's workspace cannot start from it"] is False


# =================================================================================================
# On Docker
# =================================================================================================


@pytest.mark.docker
async def test_on_docker_no_workspace_starts_from_a_snapshot_the_daemon_does_not_hold():
    """Mounting a volume that is not there would make an empty one: the provider checks first."""
    assert _docker_ready(), "docker or the example image is missing"
    prov, missing, tenant = DockerProvider(), str(uuid.uuid4()), tenancy()
    try:
        with pytest.raises(WorkspaceUnavailable, match="does not exist"):
            await prov.create(space(image=IMAGE, from_snapshot=missing),
                              {"agentsdk.workspace": str(uuid.uuid4()), "agentsdk.tenant": tenant})
        assert await prov.list_owned({"agentsdk.tenant": tenant}) == []
        assert _volumes(f"agentsdk-snap-{missing}") == [], "a volume was made for the missing snapshot"
    finally:
        await _clean(prov, tenant, missing)


def _volumes(name: str) -> list[str]:
    listed = subprocess.run(["docker", "volume", "ls", "-q", "--filter", f"name={name}"],
                            capture_output=True, text=True, timeout=60)
    assert listed.returncode == 0, listed.stderr
    return listed.stdout.split()


async def _clean(prov, tenant: str, snapshot_id: str) -> None:
    """What a failing check left: this tenancy's workspaces, and the snapshot's volume by its name."""
    for handle in await prov.list_owned({"agentsdk.tenant": tenant}):
        await prov.remove(handle)
    for name in _volumes(f"agentsdk-snap-{snapshot_id}"):
        subprocess.run(["docker", "volume", "rm", "-f", name], capture_output=True, timeout=60)


@pytest.mark.docker
async def test_on_docker_a_snapshot_released_while_it_is_copied_starts_no_workspace():
    """Released between the provider's check and the mount, the mount made an empty volume in its
    place: that volume goes, and the workspace fails rather than starting empty."""
    assert _docker_ready(), "docker or the example image is missing"
    tenant = tenancy()

    class ReleasedMeanwhile(DockerProvider):
        """The first look finds the snapshot; it is gone by the mount."""

        def __init__(self):
            super().__init__()
            self.looked = 0

        async def list_snapshots(self, labels):
            self.looked += 1
            if self.looked == 1:
                from agentsdk.workspace import SnapshotHandle

                return [SnapshotHandle(labels["agentsdk.snapshot"], {})]
            return await super().list_snapshots(labels)

    prov, missing = ReleasedMeanwhile(), str(uuid.uuid4())
    try:
        with pytest.raises(WorkspaceUnavailable, match="removed while it was copied"):
            await prov.create(space(image=IMAGE, from_snapshot=missing),
                              {"agentsdk.workspace": str(uuid.uuid4()), "agentsdk.tenant": tenant})
        assert prov.looked == 2, "the race was not reached"
        assert await prov.list_owned({"agentsdk.tenant": tenant}) == []
        assert _volumes(f"agentsdk-snap-{missing}") == [], "the empty volume the mount made was left"
    finally:
        await _clean(prov, tenant, missing)


@pytest.mark.docker
async def test_on_docker_a_tenancy_differing_only_by_trailing_whitespace_reaches_no_snapshot():
    """M46 pre-review F1, on the daemon whose filter ignores a value's trailing whitespace."""
    assert _docker_ready(), "docker or the example image is missing"
    tenant, prov = tenancy(), DockerProvider()
    runner = runner_for(prov)
    try:
        built = await run(runner, space(image=IMAGE, keep=True), DOCKER_BUILT, tenant)
        ref = await runner.snapshot_workspace(tenant, PROJECT, created(built))
        # The provider itself, not only the manager over it, answers exactly.
        for tail in ALMOST:
            assert await prov.list_snapshots({"agentsdk.tenant": tenant + tail}) == [], repr(tail)
            assert await prov.list_owned({"agentsdk.tenant": tenant + tail}) == [], repr(tail)
        assert len(await prov.list_owned({"agentsdk.tenant": tenant})) == 1
        for other in [(tenant + tail, PROJECT) for tail in ALMOST] + [(tenant, PROJECT + " ")]:
            made = await runner.run(agent(space(image=IMAGE, from_snapshot=ref.snapshot_id)),
                                    json.dumps(DOCKER_READ_BACK), RunConfig(tenant_id=other[0], project_id=other[1]))
            assert refused(made), other
            assert await runner.release_snapshot(*other, ref.snapshot_id) is False, other
        assert [s.snapshot_id for s in await prov.list_snapshots({"agentsdk.tenant": tenant})] == [ref.snapshot_id]
        assert await runner.release_snapshot(tenant, PROJECT, ref.snapshot_id) is True
    finally:
        await runner.aclose()
    assert await prov.list_snapshots({"agentsdk.tenant": tenant}) == []


@pytest.mark.docker
async def test_on_docker_a_sparse_file_past_the_bound_is_refused_before_anything_is_written():
    """M46 pre-review F4: `cp -a` wrote a sparse file out in full, so a workspace `du` counted as
    nearly empty made the host write gigabytes before the snapshot was refused. The copy counts
    apparent sizes first, and writes nothing past the bound."""
    assert _docker_ready(), "docker or the example image is missing"
    from agentsdk.workspace import docker as dk

    tenant, prov = tenancy(), DockerProvider()
    runner = runner_for(prov)
    target = f"agentsdk-snap-{uuid.uuid4()}"
    try:
        built = await run(runner, space(image=IMAGE, keep=True),
                          [[["run_command", {"argv": ["truncate", "-s", "2G", "sparse.bin"]}]]], tenant)
        workspace_id = created(built)
        assert await prov.size(next(h for h in await prov.list_owned({"agentsdk.tenant": tenant}))) < 2**20, \
            "the file is not sparse, so this shows nothing"
        with pytest.raises(WorkspaceRefused, match="max_workspace_bytes"):
            await runner.snapshot_workspace(tenant, PROJECT, workspace_id)
        # The copy itself, into a volume of the test's own, its root prepared as a snapshot's is so
        # that the copy's user can write there: nothing written into it (round 1 finding 5).
        [handle] = await prov.list_owned({"agentsdk.tenant": tenant})
        assert subprocess.run(["docker", "volume", "create", target], capture_output=True, timeout=60).returncode == 0
        async with prov._helper(handle, target) as helper:
            assert (await prov._cli("cp", "-a", "-", f"{helper}:/", stdin=dk._root_tar()))[0] == 0
        with pytest.raises(WorkspaceRefused):
            await prov._copied(handle, dk.volume_name(workspace_id), target)

        def used() -> int:
            out = subprocess.run(["docker", "run", "--rm", "--pull", "never", "--network", "none", "-v",
                                  f"{target}:/t", "--entrypoint=", IMAGE, "du", "-sk", "/t"],
                                 capture_output=True, text=True, timeout=120)
            assert out.returncode == 0, out.stderr
            return int(out.stdout.split()[0])

        assert used() < 64
        # The control: the copy's user, in the copy's container, can write there, and du counts it.
        wrote = subprocess.run(["docker", "run", "--rm", "--pull", "never", *dk.hardening(handle.spec), "-v",
                                f"{target}:/t", "--entrypoint=", IMAGE, "sh", "-c",
                                "head -c 131072 /dev/zero > /t/control.bin"], capture_output=True, timeout=120)
        assert wrote.returncode == 0, wrote.stderr
        assert used() >= 128, "the copy's user cannot write into the target, so this shows nothing"
    finally:
        subprocess.run(["docker", "volume", "rm", "-f", target], capture_output=True, timeout=60)
        await runner.aclose()
    assert await prov.list_snapshots({"agentsdk.tenant": tenant}) == []
    assert _volumes(target) == []


@pytest.mark.docker
@pytest.mark.parametrize("missing", ["find", "stat", "awk"])
async def test_on_docker_an_image_without_a_counting_tool_fails_the_copy_as_itself(missing, monkeypatch):
    """DECISION-f15a2771 (round 1 finding 1): a tool the count needs, missing, is no bound passed,
    and never a count of nothing that lets the copy run."""
    assert _docker_ready(), "docker or the example image is missing"
    from agentsdk.workspace import docker as dk

    tools = " ".join(t for t in ("find", "stat", "awk", "cp", "du") if t != missing)
    monkeypatch.setattr(dk, "_COPY", f"b={dk.MOUNT}/.bin; mkdir -p $b && for t in {tools}; do "
                                     f"ln -s /bin/busybox $b/$t; done && PATH=$b; " + dk._COPY)
    tenant, prov = tenancy(), DockerProvider()
    runner = runner_for(prov)
    try:
        built = await run(runner, space(image=IMAGE, keep=True), DOCKER_BUILT, tenant)
        with pytest.raises(WorkspaceUnavailable):
            await runner.snapshot_workspace(tenant, PROJECT, created(built))
        assert await prov.list_snapshots({"agentsdk.tenant": tenant}) == []
    finally:
        await runner.aclose()
    assert await prov.list_snapshots({"agentsdk.tenant": tenant}) == []


@pytest.mark.docker
async def test_on_docker_example_40_holds_every_check():
    assert _docker_ready(), "docker or the example image is missing"
    checks = await _example().offline("memory", "docker", tenant=tenancy())
    assert len(checks) >= 7 and [(label, why) for label, passed, why in checks if passed is not True] == []


def _docker_ready() -> bool:
    try:
        return subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


DOCKER_BUILT = [[["write_workspace_file", {"path": "a.txt", "content": "one"}],
                 ["run_command", {"argv": ["sh", "-c", "mkdir -p dir && printf two > dir/b.txt"]}],
                 ["run_command", {"argv": ["ln", "-s", "a.txt", "l"]}],
                 ["run_command", {"argv": ["ln", "a.txt", "hard"]}]]]
DOCKER_READ_BACK = [[["read_workspace_file", {"path": "a.txt"}], ["read_workspace_file", {"path": "dir/b.txt"}],
                     ["list_workspace", {"path": "."}],
                     ["run_command", {"argv": ["sh", "-c", "readlink l; stat -c %h a.txt"]}]]]


@pytest.mark.docker
@pytest.mark.parametrize("store", STORES)
async def test_on_docker_a_snapshot_restores_the_state_at_its_taking_links_kept(store):
    assert _docker_ready(), "docker or the example image is missing"
    tenant, prov, model = tenancy(), DockerProvider(), Turns()
    runner = runner_for(prov, store, model)
    try:
        handle = await runner.start(agent(space(image=IMAGE)), json.dumps(DOCKER_BUILT + ["pause"] + [[
            ["write_workspace_file", {"path": "a.txt", "content": "changed"}],
            ["write_workspace_file", {"path": "c.txt", "content": "later"}]]]),
            RunConfig(tenant_id=tenant, project_id=PROJECT))
        await asyncio.wait_for(model.paused.wait(), 300)
        [workspace_id] = [h.workspace_id for h in await prov.list_owned({"agentsdk.tenant": tenant})]
        ref = await runner.snapshot_workspace(tenant, PROJECT, workspace_id)
        model.go.set()
        assert (await handle.result()).status is RunStatus.COMPLETED
        restored = await run(runner, space(image=IMAGE, from_snapshot=ref.snapshot_id), DOCKER_READ_BACK, tenant)
        got = await results(runner, restored, tenant)
        assert got["t0-0"].content == "one" and got["t0-1"].content == "two"
        assert "l\tlink" in got["t0-2"].content and "c.txt" not in got["t0-2"].content, got["t0-2"].content
        assert "a.txt\n2" in got["t0-3"].content, "the link or the hard link was not kept"
        [(_, _, size, why)] = await snapshot_rows(runner, store, tenant)
        assert size == ref.size_bytes and why is None
        assert await runner.release_snapshot(tenant, PROJECT, ref.snapshot_id) is True
    finally:
        await runner.aclose()
    assert await prov.list_snapshots({"agentsdk.tenant": tenant}) == []
    assert await prov.list_owned({"agentsdk.tenant": tenant}) == []


@pytest.mark.docker
async def test_on_docker_a_snapshot_past_a_bound_is_refused_and_another_tenancys_fails_the_workspace():
    assert _docker_ready(), "docker or the example image is missing"
    tenant, prov = tenancy(), DockerProvider()
    runner = runner_for(prov)
    try:
        built = await run(runner, space(image=IMAGE, keep=True), DOCKER_BUILT, tenant)
        ref = await runner.snapshot_workspace(tenant, PROJECT, created(built))
        for owner, spec in ((tenancy(), space(image=IMAGE, from_snapshot=ref.snapshot_id)),
                            (tenant, space(image=IMAGE, from_snapshot=ref.snapshot_id,
                                           max_workspace_bytes=ref.size_bytes - 1))):
            assert refused(await run(runner, spec, DOCKER_READ_BACK, owner)), spec
        full = await run(runner, space(image=IMAGE, keep=True, max_workspace_bytes=8192),
                         [[["run_command", {"argv": ["sh", "-c", "head -c 20000 /dev/zero > big.bin"]}]]], tenant)
        with pytest.raises(WorkspaceRefused, match="max_workspace_bytes"):
            await runner.snapshot_workspace(tenant, PROJECT, created(full))
        assert [s.snapshot_id for s in await prov.list_snapshots({"agentsdk.tenant": tenant})] == [ref.snapshot_id]
    finally:
        await runner.aclose()
    # A kept workspace's snapshot outlives aclose() (DECISION-bafa7be8): released here, by its id.
    assert [s.snapshot_id for s in await prov.list_snapshots({"agentsdk.tenant": tenant})] == [ref.snapshot_id]
    later = runner_for(prov)
    try:
        assert await later.release_snapshot(tenant, PROJECT, ref.snapshot_id) is True
    finally:
        await later.aclose()
    assert await prov.list_snapshots({"agentsdk.tenant": tenant}) == []
