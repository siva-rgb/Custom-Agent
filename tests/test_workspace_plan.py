"""M41 gate: export, lifetime, reaping, concurrency and plans (FR-150 to FR-153, AC-99, NFR-22, NFR-44).

A file leaves a workspace only as an artifact of its run. A workspace ends with its run, or with
its plan's top-level run, or when a kept one is released or its Runner closes. A Runner reaps
what a dead Runner left -- by the `agentsdk.owner` label, `hostname:pid:start`, never touching an
owner that may be alive (DECISION-825b81a6) -- and holds at most `max_concurrent_workspaces`.
M40's caveat 2 comes first: a provider run that raises is recorded `killed='error'`.

The gates run on `FakeProvider`; tests marked `docker` hold the same against a real daemon, run
live before review with `pytest -m docker`. Store calls from async tests go to a worker thread
(FR-81); the persisted half removes every row it writes, by the run ids of its own tenancies.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psycopg
import pytest

from agentsdk import AgentSpec, Persistence, RunConfig, RunStatus, Runner
from agentsdk.budget import BudgetPolicy
from agentsdk.config import normalise_database_url
from agentsdk.context_policy import ContextPolicy
from agentsdk.errors import WorkspaceUnavailable
from agentsdk.events import EventType
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.orchestrator import PLAN_TOOL, Orchestrator
from agentsdk.postgres import RunScope
from agentsdk.primitives import InstructionAuthority, Message, Origin, Role, TaintFlag, ToolCall, TrustZone
from agentsdk.scheduler import SchedulerLimits
from agentsdk.tools import ApprovalPolicy
from agentsdk.workspace import DockerProvider, FakeProvider, WorkspaceSpec
from agentsdk.workspace import docker as dk
from agentsdk.workspace import owner as ow

REPO = Path(__file__).resolve().parent.parent
DSN = normalise_database_url(os.environ.get("DATABASE_URL"))
PREFIX = "SYN-m41-"
PROJECT = "p-m41"
STORES = ["memory", "postgres"]
FAKE_IMAGE = "fake/workspace@sha256:" + "0" * 64
# The example image, present on the host and never pulled (FR-137); docker tests need it.
IMAGE = "python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df"
TOOLS = ("run_command", "read_workspace_file", "write_workspace_file", "list_workspace", "export_workspace_file")
HOST = socket.gethostname()


@pytest.fixture(autouse=True)
def remove_what_the_test_wrote():
    yield
    if DSN:
        with psycopg.connect(DSN, autocommit=True) as conn:
            ids = [r[0] for r in conn.execute("SELECT run_id FROM runs WHERE tenant_id LIKE %s", (PREFIX + "%",))]
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
    """make PATH TEXT: writes TEXT to PATH, as a command that produces a file."""
    cmd.files.write(cmd.argv[1], cmd.argv[2].encode())
    return 0


def provider(cls=FakeProvider):
    return cls(commands={"nap": _nap, "make": _make})


def respond(content="", calls=()):
    return ModelResponse(message=Message(role=Role.ASSISTANT, content=content, tool_calls=tuple(calls)),
                         stop_reason=StopReason.TOOL_CALLS if calls else StopReason.END_TURN, usage=Usage(5, 1, 6))


class Turns:
    """The model: the task holds a JSON list of turns, each a list of [tool, arguments]."""

    async def send(self, request):
        text = request.messages[0].content
        turns = json.loads(text[text.index("["):].split("\n")[0])
        turn = sum(1 for m in request.messages if m.role is Role.ASSISTANT)
        if turn < len(turns):
            return respond(calls=[ToolCall(id=f"t{turn}-{i}", name=name, arguments=args)
                                  for i, (name, args) in enumerate(turns[turn])])
        return respond("done")


class FailsAfterOne(Turns):
    """Its second call raises, so its run ends failed after a command."""

    async def send(self, request):
        if any(m.role is Role.TOOL for m in request.messages):
            raise RuntimeError("the model went away")
        return await super().send(request)


class Planner:
    def __init__(self, nodes):
        self.nodes = nodes

    async def send(self, request):
        if not any(m.role is Role.TOOL for m in request.messages):
            return respond(calls=[ToolCall(id="plan", name=PLAN_TOOL, arguments={"nodes": self.nodes})])
        return respond("the plan is done")


def space(**fields) -> WorkspaceSpec:
    return WorkspaceSpec(image=fields.pop("image", FAKE_IMAGE), run_command_approval=ApprovalPolicy.AUTO, **fields)


def agent(spec: WorkspaceSpec, agent_id="coder") -> AgentSpec:
    return AgentSpec(id=agent_id, instructions="use the workspace", preferred_model="m:x", tool_profile=TOOLS,
                     workspace=spec)


def persistence(store):
    return Persistence.postgres(DSN, create_schema=False) if store == "postgres" else None


def runner_for(prov, store="memory", **options):
    return Runner({"m": Turns(), "f": FailsAfterOne()}, workspace_provider=prov, persistence=persistence(store),
                  **options)


async def run(runner, spec, turns, tenant, **config):
    return await runner.run(agent(spec), json.dumps(turns), RunConfig(tenant_id=tenant, project_id=PROJECT, **config))


async def history_results(runner, run_id, tenant):
    sessions = runner._sessions_for(RunScope(run_id=run_id, tenant_id=tenant, project_id=PROJECT))
    history = await asyncio.to_thread(sessions.history, run_id)
    return {r.tool_call_id: r for m in history for r in m.tool_results}


def events(result, kind):
    return [e.payload for e in result.events if e.event_type is kind]


def error_types(result) -> dict:
    """Each call's error class, from its ToolCalled event (DECISION-94bfee7c)."""
    return {e.payload["tool_call_id"]: e.payload.get("error_type")
            for e in result.events if e.event_type is EventType.TOOL_CALLED}


def stored(sql, *args):
    with psycopg.connect(DSN) as conn:
        return conn.execute(sql, args).fetchall()


async def workspace_rows(runner, store, tenant):
    """Every workspace row of the tenancy as (workspace_id, run_id, scope, removed_reason)."""
    if store == "postgres":
        return [(str(w), str(r), s, why) for w, r, s, why in await asyncio.to_thread(
            stored, "SELECT workspace_id, run_id, scope, removed_reason FROM workspaces WHERE tenant_id = %s"
                    " ORDER BY created_at", tenant)]
    return [(w.workspace_id, w.run_id, w.scope, w.removed_reason)
            for w in runner._workspace_store().workspaces(tenant, PROJECT)]


async def command_rows(runner, store, tenant):
    """Every command row of the tenancy as (run_id, workspace_id, exit_code, killed)."""
    if store == "postgres":
        return [(str(r), str(w), code, killed) for r, w, code, killed in await asyncio.to_thread(
            stored, "SELECT run_id, workspace_id, exit_code, killed FROM workspace_commands WHERE tenant_id = %s"
                    " ORDER BY started_at", tenant)]
    return [(c.run_id, c.workspace_id, c.exit_code, c.killed)
            for c in runner._workspace_store().commands(tenant, PROJECT)]


async def held(prov):
    return await prov.list_owned({})


def _example(name: str):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), REPO / "scripts" / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# =================================================================================================
# M40's caveat 2, closed (DECISION-825b81a6): a provider run that raises is recorded
# =================================================================================================


class FailingRun(FakeProvider):
    """Its run raises once the command has started, as a daemon that stopped answering."""

    async def run(self, handle, argv, **kwargs):
        raise WorkspaceUnavailable("the docker daemon stopped answering")


@pytest.mark.parametrize("store", STORES)
async def test_a_provider_run_that_raises_is_ended_and_recorded_as_an_error(store):
    """M40 caveat 2: Started had no Ended and no row. Now every start has one end and one row."""
    tenant, runner = tenancy(), runner_for(FailingRun(), store)
    result = await run(runner, space(), [[["run_command", {"argv": ["nap", "0"]}]]], tenant)
    assert result.status is RunStatus.COMPLETED
    started, ended = events(result, EventType.WORKSPACE_COMMAND_STARTED), events(result, EventType.WORKSPACE_COMMAND_ENDED)
    assert len(started) == len(ended) == 1 and started[0]["command_id"] == ended[0]["command_id"]
    assert ended[0]["killed"] == "error" and ended[0]["kill_error"] == "WorkspaceUnavailable"
    assert ended[0]["exit_code"] is None
    assert [(code, killed) for _, _, code, killed in await command_rows(runner, store, tenant)] == [(None, "error")]
    # The provider's error still reaches the tool; its class is on the event (DECISION-94bfee7c).
    assert error_types(result)["t0-0"] == "ToolExecutionError"


async def test_after_a_provider_run_that_raises_the_next_use_reads_the_size_first():
    """The command may have written before it failed: its size is read before anything else."""

    class FailsThenCounts(FailingRun):
        def __init__(self):
            super().__init__()
            self.sizes = 0

        async def size(self, handle):
            self.sizes += 1
            return await super().size(handle)

    prov, tenant = FailsThenCounts(), tenancy()
    runner = runner_for(prov)
    await run(runner, space(), [[["run_command", {"argv": ["nap", "0"]}]],
                                [["write_workspace_file", {"path": "a.txt", "content": "x"}]]], tenant)
    assert prov.sizes == 2, "the write did not read the size the failed command left unread first"


# =================================================================================================
# FR-150: export_workspace_file
# =================================================================================================


@pytest.mark.parametrize("store", STORES)
async def test_an_exported_file_is_an_artifact_of_its_run_naming_the_last_command(store):
    tenant, runner = tenancy(), runner_for(provider(), store)
    result = await run(runner, space(), [
        [["run_command", {"argv": ["make", "out/r.csv", "a,b\n1,2\n"]}]],
        [["export_workspace_file", {"path": "out/r.csv", "mime_type": "text/csv"}]],
    ], tenant)
    assert result.status is RunStatus.COMPLETED, result.error
    command_id = events(result, EventType.WORKSPACE_COMMAND_ENDED)[0]["command_id"]
    workspace_id = events(result, EventType.WORKSPACE_CREATED)[0]["workspace_id"]
    artifact_id = (await history_results(runner, result.run_id, tenant))["t1-0"].content.strip()
    store_ = runner._artifacts_for(tenant, PROJECT)
    ref = await store_.metadata(artifact_id)
    assert await store_.get(artifact_id) == b"a,b\n1,2\n"
    assert ref.source_run == result.run_id and ref.mime_type == "text/csv" and ref.created_by_agent == "coder"
    p = ref.provenance
    assert p.origin is Origin.INTERNAL_TOOL and p.trust_zone is TrustZone.UNTRUSTED
    assert p.instruction_authority is InstructionAuthority.DATA_ONLY
    assert p.taint_flags == frozenset({TaintFlag.EXTERNAL_CONTENT, TaintFlag.EXECUTABLE_CONTENT})
    assert p.source_uri_or_hash == f"workspace://{workspace_id}/out/r.csv?command={command_id}"


async def test_an_export_before_any_command_names_none_and_its_path_is_quoted():
    tenant, runner = tenancy(), runner_for(provider())
    result = await run(runner, space(), [
        [["write_workspace_file", {"path": "a b?c#d.txt", "content": "hi"}]],
        [["export_workspace_file", {"path": "a b?c#d.txt"}]],
    ], tenant)
    workspace_id = events(result, EventType.WORKSPACE_CREATED)[0]["workspace_id"]
    artifact_id = (await history_results(runner, result.run_id, tenant))["t1-0"].content.strip()
    ref = await runner._artifacts_for(tenant, PROJECT).metadata(artifact_id)
    assert ref.mime_type == "application/octet-stream"
    assert ref.provenance.source_uri_or_hash == f"workspace://{workspace_id}/a%20b%3Fc%23d.txt"


@pytest.mark.parametrize("store", STORES)
async def test_a_folder_or_a_missing_file_is_not_exported_and_no_artifact_is_written(store):
    tenant, runner = tenancy(), runner_for(provider(), store)
    result = await run(runner, space(), [
        [["run_command", {"argv": ["make", "d/x.txt", "x"]}]],
        [["export_workspace_file", {"path": "d"}], ["export_workspace_file", {"path": "missing.txt"}]],
    ], tenant)
    results = await history_results(runner, result.run_id, tenant)
    assert results["t1-0"].is_error and results["t1-1"].is_error
    assert error_types(result)["t1-0"] == error_types(result)["t1-1"] == "ToolExecutionError"
    if store == "postgres":
        assert await asyncio.to_thread(stored, "SELECT count(*) FROM artifacts WHERE source_run = %s::uuid",
                                       result.run_id) == [(0,)]


async def test_a_folder_holding_one_file_is_neither_read_nor_exported_as_that_file():
    """M41 pre-flight: the fake packs a folder as `.` and its files, and the folder's own entry was
    filtered out, so a folder with one file was read, and exported, as that file."""
    from agentsdk.workspace.tools import one_file, one_file_tar

    prov = provider()
    handle = await prov.create(space(), {})
    await prov.import_(handle, one_file_tar("d/x.txt", b"x"))
    for path in ("d", "d/", "./d", "."):
        with pytest.raises(IsADirectoryError):
            one_file(await prov.export(handle, path), path)
    assert one_file(await prov.export(handle, "d/x.txt"), "d/x.txt") == b"x"
    tenant, runner = tenancy(), runner_for(provider())
    result = await run(runner, space(), [
        [["write_workspace_file", {"path": "d/x.txt", "content": "x"}]],
        [["read_workspace_file", {"path": "d"}], ["export_workspace_file", {"path": "d"}]],
    ], tenant)
    results = await history_results(runner, result.run_id, tenant)
    assert results["t1-0"].is_error and results["t1-1"].is_error


def test_export_is_a_write_tool_with_a_bounded_mime_type():
    from agentsdk.tools import RiskClass
    from agentsdk.workspace.tools import NAMES, specs

    assert "export_workspace_file" in NAMES
    declared = specs(space())["export_workspace_file"]
    assert declared.risk_class is RiskClass.WRITE
    mime = declared.input_schema["properties"]["mime_type"]
    assert mime["maxLength"] <= 255 and "pattern" in mime
    assert declared.input_schema["required"] == ["path"]


# =================================================================================================
# FR-151: a workspace's lifetime
# =================================================================================================


@pytest.mark.parametrize("store", STORES)
async def test_a_run_workspace_is_removed_when_its_run_ends_completed_failed_or_cancelled(store):
    tenant, prov = tenancy(), provider()
    runner = runner_for(prov, store)
    done = await run(runner, space(), [[["run_command", {"argv": ["nap", "0"]}]]], tenant)
    failing = AgentSpec(id="coder", instructions="x", preferred_model="f:x", tool_profile=TOOLS, workspace=space())
    failed = await runner.run(failing, json.dumps([[["run_command", {"argv": ["nap", "0"]}]]]),
                              RunConfig(tenant_id=tenant, project_id=PROJECT))
    handle = await runner.start(agent(space()), json.dumps([[["run_command", {"argv": ["nap", "30"]}]]]),
                                RunConfig(tenant_id=tenant, project_id=PROJECT))
    async for event in handle.events():
        if event.event_type is EventType.WORKSPACE_COMMAND_STARTED:
            break
    handle.cancel()
    cancelled = await handle.result()
    assert (done.status, failed.status, cancelled.status) == (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED)
    rows = await workspace_rows(runner, store, tenant)
    assert len(rows) == 3 and {why for *_, why in rows} == {"run_ended"}
    assert await held(prov) == []


@pytest.mark.parametrize("store", STORES)
async def test_a_kept_workspace_outlives_its_run_until_released(store):
    tenant, prov = tenancy(), provider()
    runner = runner_for(prov, store)
    result = await run(runner, space(keep=True), [[["write_workspace_file", {"path": "a.txt", "content": "x"}]]], tenant)
    assert result.status is RunStatus.COMPLETED, result.error
    assert EventType.WORKSPACE_REMOVED not in [e.event_type for e in result.events]
    [(workspace_id, _, _, why)] = await workspace_rows(runner, store, tenant)
    assert why is None and [h.workspace_id for h in await held(prov)] == [workspace_id]
    # Another tenancy cannot release it, nor can an unknown id.
    assert await runner.release_workspace(tenancy(), PROJECT, workspace_id) is False
    assert await runner.release_workspace(tenant, PROJECT, str(uuid.uuid4())) is False
    assert [h.workspace_id for h in await held(prov)] == [workspace_id]
    assert await runner.release_workspace(tenant, PROJECT, workspace_id) is True
    assert await held(prov) == []
    assert [why for *_, why in await workspace_rows(runner, store, tenant)] == ["released"]
    assert await runner.release_workspace(tenant, PROJECT, workspace_id) is False


@pytest.mark.parametrize("store", STORES)
async def test_aclose_kills_every_command_and_removes_every_workspace_kept_ones_included(store):
    tenant, prov = tenancy(), provider()
    runner = runner_for(prov, store)
    await run(runner, space(keep=True), [[["write_workspace_file", {"path": "a.txt", "content": "x"}]]], tenant)
    handle = await runner.start(agent(space()), json.dumps([[["run_command", {"argv": ["nap", "60"]}]]]),
                                RunConfig(tenant_id=tenant, project_id=PROJECT))
    async for event in handle.events():
        if event.event_type is EventType.WORKSPACE_COMMAND_STARTED:
            break
    began = time.monotonic()
    await runner.aclose()
    assert time.monotonic() - began < 15
    assert await held(prov) == []
    result = await asyncio.wait_for(handle.result(), 15)
    ended = events(result, EventType.WORKSPACE_COMMAND_ENDED)
    assert len(ended) == 1 and ended[0]["exit_code"] is None, ended
    reasons = sorted(why for *_, why in await workspace_rows(runner, store, tenant))
    assert reasons == ["aclose", "aclose"], reasons


async def test_aclose_leaves_another_runner_of_the_same_process_alone():
    """Both Runners share one owner label (this process); aclose() sweeps by its own
    agentsdk.runner label, so the other Runner keeps its workspace."""
    tenant, prov = tenancy(), provider()
    mine, other = runner_for(prov), runner_for(prov)
    await run(mine, space(keep=True), [[["write_workspace_file", {"path": "a", "content": "x"}]]], tenant)
    await run(other, space(keep=True), [[["write_workspace_file", {"path": "b", "content": "y"}]]], tenant)
    [(theirs, *_)] = await workspace_rows(other, "memory", tenant)
    # The swept label, too: drop mine's record of its workspace, as a removal that failed leaves it.
    mine._workspaces.spaces.clear()
    await mine.aclose()
    assert [h.workspace_id for h in await held(prov)] == [theirs]
    await other.aclose()
    assert await held(prov) == []


async def test_aclose_removes_what_it_holds_when_the_provider_cannot_list():
    class ListFails(FakeProvider):
        async def list_owned(self, labels):
            raise WorkspaceUnavailable("the daemon is going away")

    tenant, prov = tenancy(), ListFails()
    runner = runner_for(prov)
    runner._workspaces._reaped = True  # nothing to reap; the listing fails only at aclose()
    await run(runner, space(keep=True), [[["write_workspace_file", {"path": "a", "content": "x"}]]], tenant)
    assert len(prov._spaces) == 1
    await runner.aclose()
    assert prov._spaces == {}
    assert [why for *_, why in await workspace_rows(runner, "memory", tenant)] == ["aclose"]


async def test_after_aclose_no_workspace_is_created():
    tenant, prov = tenancy(), provider()
    runner = runner_for(prov)
    await runner.aclose()
    result = await run(runner, space(), [[["write_workspace_file", {"path": "a.txt", "content": "x"}]]], tenant)
    assert error_types(result)["t0-0"] == "ToolExecutionError"
    assert not events(result, EventType.WORKSPACE_CREATED)
    assert await held(prov) == []
    await runner.aclose()  # twice is harmless


async def test_a_creation_under_way_when_aclose_begins_is_removed_too():
    class SlowCreate(FakeProvider):
        def __init__(self):
            super().__init__()
            self.creating = asyncio.Event()

        async def create(self, spec, labels):
            self.creating.set()
            await asyncio.sleep(0.5)
            return await super().create(spec, labels)

    tenant, prov = tenancy(), SlowCreate()
    runner = runner_for(prov)
    handle = await runner.start(agent(space()), json.dumps([[["write_workspace_file", {"path": "a", "content": "x"}]]]),
                                RunConfig(tenant_id=tenant, project_id=PROJECT))
    await asyncio.wait_for(prov.creating.wait(), 10)
    await runner.aclose()
    await asyncio.wait_for(handle.result(), 15)
    assert await held(prov) == []


# =================================================================================================
# FR-151: the owner label, and reaping (DECISION-825b81a6)
# =================================================================================================


def test_this_process_owner_names_host_pid_and_start_and_is_alive():
    label = ow.this_owner()
    host, pid, start = label.rsplit(":", 2)
    assert host == HOST and pid == str(os.getpid()) and start not in ("", "?")
    assert ow.this_owner() == label
    assert ow.is_dead(label) is False


def test_an_owner_is_dead_only_when_its_process_is_provably_gone():
    host, pid, start = ow.this_owner().rsplit(":", 2)
    assert ow.is_dead(f"{host}:{pid}:{int(start) + 1}") is True, "a reused pid is a different owner"
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    gone = child.pid
    del child
    assert ow.is_dead(f"{host}:{gone}:1") is True
    # Never dead: another host, an unreadable label, the pre-M41 pid-only label.
    for label in (f"elsewhere:{gone}:1", "garbage", str(gone), "", f"{host}:x:1", f"{host}:{gone}"):
        assert ow.is_dead(label) is False, label


@pytest.mark.skipif(sys.platform != "win32", reason="the System process refuses to be opened on Windows")
def test_an_owner_whose_liveness_cannot_be_read_counts_as_alive():
    assert ow.is_dead(f"{HOST}:4:1") is False


def _dead_label() -> str:
    host, pid, start = ow.this_owner().rsplit(":", 2)
    return f"{host}:{pid}:{int(start) + 1}"


@pytest.mark.parametrize("store", STORES)
async def test_a_runner_reaps_a_dead_owners_workspaces_before_its_first_and_records_them(store):
    tenant, prov = tenancy(), provider()
    dead = runner_for(prov, store)
    dead._workspaces.owner = _dead_label()
    await run(dead, space(keep=True), [[["write_workspace_file", {"path": "a.txt", "content": "x"}]]], tenant)
    [(gone_id, *_)] = await workspace_rows(dead, store, tenant)
    # One alive (this process), one of another host, one unlabelled: none of them is touched.
    alive = await prov.create(space(), {"agentsdk.workspace": str(uuid.uuid4()), "agentsdk.owner": ow.this_owner(),
                                        "agentsdk.tenant": tenant, "agentsdk.project": PROJECT})
    other = await prov.create(space(), {"agentsdk.workspace": str(uuid.uuid4()), "agentsdk.owner": "elsewhere:1:1",
                                        "agentsdk.tenant": tenant, "agentsdk.project": PROJECT})
    bare = await prov.create(space(), {"agentsdk.workspace": str(uuid.uuid4())})
    runner = runner_for(prov, store)
    assert gone_id in [h.workspace_id for h in await held(prov)], "nothing reaps before a first workspace"
    result = await run(runner, space(), [[["write_workspace_file", {"path": "b.txt", "content": "y"}]]], tenant)
    assert result.status is RunStatus.COMPLETED, result.error
    left = {h.workspace_id for h in await held(prov)}
    assert left == {alive.workspace_id, other.workspace_id, bare.workspace_id}
    if store == "postgres":
        # The dead Runner's row is in the shared store: it is recorded reaped.
        rows = {w: why for w, _, _, why in await workspace_rows(runner, store, tenant)}
        assert rows[gone_id] == "reaped"
    # Reaping names nothing of another workspace on this run's events.
    assert gone_id not in json.dumps([e.payload for e in result.events], default=str)


async def test_a_reaping_that_fails_does_not_fail_the_run_and_is_tried_again():
    class ListFails(FakeProvider):
        def __init__(self):
            super().__init__()
            self.listings = 0

        async def list_owned(self, labels):
            self.listings += 1
            if self.listings == 1:
                raise WorkspaceUnavailable("the daemon is starting")
            return await super().list_owned(labels)

    prov, tenant = ListFails(), tenancy()
    runner = runner_for(prov)
    first = await run(runner, space(), [[["write_workspace_file", {"path": "a", "content": "x"}]]], tenant)
    second = await run(runner, space(), [[["write_workspace_file", {"path": "a", "content": "x"}]]], tenant)
    assert first.status is second.status is RunStatus.COMPLETED
    assert prov.listings == 2
    third = await run(runner, space(), [[["write_workspace_file", {"path": "a", "content": "x"}]]], tenant)
    assert third.status is RunStatus.COMPLETED and prov.listings == 2, "a reaping that completed ran again"


async def test_the_runner_labels_its_workspaces_with_its_owner():
    prov, tenant = provider(), tenancy()
    runner = runner_for(prov)
    await run(runner, space(keep=True), [[["write_workspace_file", {"path": "a", "content": "x"}]]], tenant)
    [handle] = await held(prov)
    assert handle.labels["agentsdk.owner"] == ow.this_owner()
    await runner.aclose()


# =================================================================================================
# FR-152: max_concurrent_workspaces
# =================================================================================================


@pytest.mark.parametrize("store", STORES)
async def test_a_run_beyond_max_concurrent_workspaces_waits_and_records_the_wait(store):
    tenant, prov = tenancy(), provider()
    runner = runner_for(prov, store, max_concurrent_workspaces=1)
    first = await runner.start(agent(space()), json.dumps([[["run_command", {"argv": ["nap", "1"]}]]]),
                               RunConfig(tenant_id=tenant, project_id=PROJECT))
    async for event in first.events():
        if event.event_type is EventType.WORKSPACE_CREATED:
            break
    second = await run(runner, space(), [[["write_workspace_file", {"path": "a", "content": "x"}]]], tenant)
    assert (await first.result()).status is RunStatus.COMPLETED
    assert second.status is RunStatus.COMPLETED, second.error
    [waited] = events(second, EventType.WORKSPACE_WAITING)
    assert waited["limit"] == 1 and waited["held"] == 1
    order = [e.event_type for e in second.events]
    assert order.index(EventType.WORKSPACE_WAITING) < order.index(EventType.WORKSPACE_CREATED)


async def test_a_wait_for_a_workspace_is_cancellable_and_leaks_no_slot():
    tenant, prov = tenancy(), provider()
    runner = runner_for(prov, max_concurrent_workspaces=1)
    await run(runner, space(keep=True), [[["write_workspace_file", {"path": "a", "content": "x"}]]], tenant)
    waiting = await runner.start(agent(space()), json.dumps([[["write_workspace_file", {"path": "a", "content": "x"}]]]),
                                 RunConfig(tenant_id=tenant, project_id=PROJECT))
    async for event in waiting.events():
        if event.event_type is EventType.WORKSPACE_WAITING:
            break
    waiting.cancel()
    result = await asyncio.wait_for(waiting.result(), 10)
    assert result.status is RunStatus.CANCELLED
    assert EventType.WORKSPACE_CREATED not in [e.event_type for e in result.events]
    [(kept_id, *_)] = await workspace_rows(runner, "memory", tenant)
    assert await runner.release_workspace(tenant, PROJECT, kept_id) is True
    again = await asyncio.wait_for(
        run(runner, space(), [[["write_workspace_file", {"path": "a", "content": "x"}]]], tenant), 10)
    assert again.status is RunStatus.COMPLETED and not events(again, EventType.WORKSPACE_WAITING)


async def test_a_slot_is_freed_when_a_creation_fails():
    class CreateFailsOnce(FakeProvider):
        def __init__(self):
            super().__init__()
            self.creates = 0

        async def create(self, spec, labels):
            self.creates += 1
            if self.creates == 1:
                raise RuntimeError("no space left")
            return await super().create(spec, labels)

    tenant, prov = tenancy(), CreateFailsOnce()
    runner = runner_for(prov, max_concurrent_workspaces=1)
    # The same run tries again after its creation failed: a slot the failure kept would make it
    # wait on itself for ever.
    failed = await asyncio.wait_for(run(runner, space(), [
        [["write_workspace_file", {"path": "a", "content": "x"}]],
        [["write_workspace_file", {"path": "a", "content": "x"}]]], tenant), 10)
    assert error_types(failed)["t0-0"] == "ToolExecutionError"
    assert error_types(failed)["t1-0"] is None and not events(failed, EventType.WORKSPACE_WAITING)
    ok = await asyncio.wait_for(run(runner, space(), [[["write_workspace_file", {"path": "a", "content": "x"}]]], tenant), 10)
    assert ok.status is RunStatus.COMPLETED and not events(ok, EventType.WORKSPACE_WAITING)


# =================================================================================================
# FR-152: plans (NFR-22)
# =================================================================================================


def node(node_id, turns, deps=(), role="coder"):
    return {"node_id": node_id, "objective": json.dumps(turns), "assigned_role": role, "dependencies": list(deps)}


def orchestrated(prov, store, nodes, roles, planner=None, **options):
    orchestrator = Orchestrator(roles=roles, policy=BudgetPolicy(run_ceiling_tokens=1_000_000),
                                limits=SchedulerLimits(max_concurrent_subagents=8),
                                context_policy=ContextPolicy(context_window=1_000_000))
    runner = Runner({"planner": planner or Planner(nodes), "m": Turns()}, tools=[orchestrator.tool],
                    workspace_provider=prov,
                    persistence=persistence(store), **options)
    return orchestrator, runner


async def exported(runner, tenant, top_run_id) -> list[bytes]:
    """The content of every artifact the top-level run's children exported."""
    store, found = runner._artifacts_for(tenant, PROJECT), []
    for (t, _, child), parent in list(runner._parents.items()):
        if t != tenant or parent != top_run_id:
            continue
        for result in (await history_results(runner, child, tenant)).values():
            try:
                uuid.UUID(result.content.strip())
            except ValueError:
                continue
            if not result.is_error:
                found.append(await store.get(result.content.strip()))
    return found


async def run_plan(orchestrator, runner, tenant):
    return await runner.run(orchestrator.agent, "go", orchestrator.config(
        tenant_id=tenant, project_id=PROJECT, model_override="planner:p", max_turns=6))


class Watching(FakeProvider):
    """Records, at each removal, which workspaces still exist."""

    def __init__(self):
        super().__init__(commands={"nap": _nap, "make": _make})
        self.removals = []

    async def remove(self, handle):
        self.removals.append(handle.workspace_id)
        await super().remove(handle)


@pytest.mark.parametrize("store", STORES)
async def test_a_plans_children_share_one_plan_workspace_removed_when_the_top_level_run_ends(store):
    tenant, prov = tenancy(), Watching()
    shared = agent(space(scope="plan"))
    orchestrator, runner = orchestrated(prov, store, [
        node("a", [[["run_command", {"argv": ["make", "a.txt", "from a"]}]]]),
        node("b", [[["export_workspace_file", {"path": "a.txt"}]]], deps=["a"]),
    ], {"coder": shared})
    result = await run_plan(orchestrator, runner, tenant)
    assert result.status is RunStatus.COMPLETED, result.error
    rows = await workspace_rows(runner, store, tenant)
    assert len(rows) == 1, rows
    [(workspace_id, owner_run, scope, why)] = rows
    assert (owner_run, scope, why) == (result.run_id, "plan", "run_ended")
    assert [w for _, w, _, _ in await command_rows(runner, store, tenant)] == [workspace_id]
    assert await exported(runner, tenant, result.run_id) == [b"from a"]
    assert [e["workspace_id"] for e in events(result, EventType.WORKSPACE_REMOVED)] == [workspace_id]
    assert prov.removals == [workspace_id] and await held(prov) == []


@pytest.mark.parametrize("store", STORES)
async def test_a_plans_run_scoped_children_each_get_their_own_workspace(store):
    tenant, prov = tenancy(), provider()
    orchestrator, runner = orchestrated(prov, store, [
        node("a", [[["write_workspace_file", {"path": "a.txt", "content": "a"}]]]),
        node("b", [[["read_workspace_file", {"path": "a.txt"}]]]),
    ], {"coder": agent(space())})
    result = await run_plan(orchestrator, runner, tenant)
    rows = await workspace_rows(runner, store, tenant)
    assert len(rows) == 2 and {scope for _, _, scope, _ in rows} == {"run"}
    assert result.run_id not in {run for _, run, _, _ in rows}
    assert await held(prov) == []


class NamedPlanner:
    """Plans by the name its task gives: each child writes that name, then another exports it."""

    async def send(self, request):
        if any(m.role is Role.TOOL for m in request.messages):
            return respond("the plan is done")
        name = request.messages[0].content.split()[-1]
        nodes = [node("w", [[["write_workspace_file", {"path": "who.txt", "content": name}]],
                            [["run_command", {"argv": ["nap", "0.2"]}]]]),
                 node("r", [[["export_workspace_file", {"path": "who.txt"}]]], deps=["w"])]
        return respond(calls=[ToolCall(id="plan", name=PLAN_TOOL, arguments={"nodes": nodes})])


async def test_no_child_reaches_another_plans_workspace():
    """Two plans at once on one Runner, each child writing its plan's name and another reading it
    back: each plan keeps its own workspace."""
    tenant, prov = tenancy(), provider()
    orchestrator, runner = orchestrated(prov, "memory", [], {"coder": agent(space(scope="plan"))},
                                        planner=NamedPlanner())
    finished = await asyncio.gather(*(runner.run(orchestrator.agent, f"go {name}", orchestrator.config(
        tenant_id=tenant, project_id=PROJECT, model_override="planner:p", max_turns=6)) for name in ("one", "two")))
    rows = await workspace_rows(runner, "memory", tenant)
    assert len(rows) == 2 and len({w for w, *_ in rows}) == 2
    for name, result in zip(("one", "two"), finished):
        assert result.status is RunStatus.COMPLETED, result.error
        assert [owner for _, owner, _, _ in rows].count(result.run_id) == 1
        assert await exported(runner, tenant, result.run_id) == [name.encode()], name


async def test_children_with_different_workspace_specs_get_different_plan_workspaces():
    """A child never runs under limits other than its own spec's (FR-152 as amended in M41)."""
    tenant, prov = tenancy(), provider()
    orchestrator, runner = orchestrated(prov, "memory", [
        node("a", [[["write_workspace_file", {"path": "a.txt", "content": "a"}]]], role="small"),
        node("b", [[["write_workspace_file", {"path": "b.txt", "content": "b"}]]], role="big"),
        node("c", [[["read_workspace_file", {"path": "a.txt"}]]], deps=["a"], role="small"),
    ], {"small": agent(space(scope="plan", max_workspace_bytes=2**20), "small"),
        "big": agent(space(scope="plan", max_workspace_bytes=2**22), "big")})
    result = await run_plan(orchestrator, runner, tenant)
    assert result.status is RunStatus.COMPLETED, result.error
    rows = await workspace_rows(runner, "memory", tenant)
    assert len(rows) == 2 and all(owner == result.run_id for _, owner, _, _ in rows)


async def test_a_cancelled_plan_removes_its_plan_workspace_and_leaves_no_child_running():
    tenant, prov = tenancy(), provider()
    orchestrator, runner = orchestrated(prov, "memory", [
        node("a", [[["run_command", {"argv": ["nap", "60"]}]]]),
        node("b", [[["run_command", {"argv": ["nap", "60"]}]]]),
    ], {"coder": agent(space(scope="plan"))})
    handle = await runner.start(orchestrator.agent, "go", orchestrator.config(
        tenant_id=tenant, project_id=PROJECT, model_override="planner:p", max_turns=6))
    deadline = time.monotonic() + 15
    while sum(len(s.running) for s in prov._spaces.values()) < 2:
        assert time.monotonic() < deadline, "both children's commands never started"
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.3)
    handle.cancel()
    result = await asyncio.wait_for(handle.result(), 30)
    assert result.status is RunStatus.CANCELLED
    assert await held(prov) == []
    assert [why for *_, why in await workspace_rows(runner, "memory", tenant)] == ["run_ended"]


async def test_a_kept_plan_workspace_outlives_its_plan_until_released():
    tenant, prov = tenancy(), provider()
    orchestrator, runner = orchestrated(prov, "memory", [
        node("a", [[["write_workspace_file", {"path": "a.txt", "content": "a"}]]]),
    ], {"coder": agent(space(scope="plan", keep=True))})
    result = await run_plan(orchestrator, runner, tenant)
    assert result.status is RunStatus.COMPLETED, result.error
    [(workspace_id, _, _, why)] = await workspace_rows(runner, "memory", tenant)
    assert why is None and len(await held(prov)) == 1
    assert await runner.release_workspace(tenant, PROJECT, workspace_id) is True
    assert await held(prov) == []


# =================================================================================================
# The pre-review's findings (KNOWLEDGE-46ddc562, DECISION-b1498111): each repair, held
# =================================================================================================


@pytest.mark.parametrize("slots", [1, 2])
async def test_a_plans_children_behind_one_free_slot_share_it_rather_than_wait_on_each_other(slots):
    """Finding 1: the first child created the plan workspace and kept the slot; the second waited
    for a give only the plan's end makes, so the plan never ended."""
    tenant, prov = tenancy(), provider()
    orchestrator, runner = orchestrated(prov, "memory", [
        node("a", [[["write_workspace_file", {"path": "a.txt", "content": "a"}]]]),
        node("b", [[["write_workspace_file", {"path": "b.txt", "content": "b"}]]]),
        node("c", [[["write_workspace_file", {"path": "c.txt", "content": "c"}]]]),
    ], {"coder": agent(space(scope="plan"))}, max_concurrent_workspaces=slots)
    result = await asyncio.wait_for(run_plan(orchestrator, runner, tenant), 20)
    assert result.status is RunStatus.COMPLETED, result.error
    assert len(await workspace_rows(runner, "memory", tenant)) == 1
    assert runner._workspaces.slots.held == 0


async def test_a_plans_children_queued_behind_a_held_slot_are_released_when_one_creates():
    """Both children queue while another run holds the only slot; when it frees, the first is
    granted it and creates the plan workspace, and its sibling is released to share it."""
    tenant, prov = tenancy(), provider()
    orchestrator, runner = orchestrated(prov, "memory", [
        node("a", [[["write_workspace_file", {"path": "a.txt", "content": "a"}]]]),
        node("b", [[["write_workspace_file", {"path": "b.txt", "content": "b"}]]]),
    ], {"coder": agent(space(scope="plan"))}, max_concurrent_workspaces=1)
    holder = await runner.start(agent(space()), json.dumps([[["run_command", {"argv": ["nap", "1"]}]]]),
                                RunConfig(tenant_id=tenant, project_id=PROJECT))
    async for event in holder.events():
        if event.event_type is EventType.WORKSPACE_CREATED:
            break
    plan = asyncio.ensure_future(run_plan(orchestrator, runner, tenant))
    deadline = time.monotonic() + 10
    while len(runner._workspaces.slots._waiters) < 2:
        assert time.monotonic() < deadline, "the children never queued"
        await asyncio.sleep(0.02)
    await holder.result()
    result = await asyncio.wait_for(plan, 20)
    assert result.status is RunStatus.COMPLETED, result.error
    assert runner._workspaces.slots.held == 0


async def test_a_plans_children_arriving_during_the_first_reaping_share_one_slot():
    """The children wait out a slow first reaping together; the first takes the free slot and
    creates, and the others, looking again after the reaping, wait for that creation."""

    class SlowList(FakeProvider):
        async def list_owned(self, labels):
            await asyncio.sleep(0.5)
            return await super().list_owned(labels)

    tenant, prov = tenancy(), SlowList()
    orchestrator, runner = orchestrated(prov, "memory", [
        node(name, [[["write_workspace_file", {"path": f"{name}.txt", "content": name}]]]) for name in "abc"
    ], {"coder": agent(space(scope="plan"))}, max_concurrent_workspaces=1)
    result = await asyncio.wait_for(run_plan(orchestrator, runner, tenant), 20)
    assert result.status is RunStatus.COMPLETED, result.error
    assert len(await workspace_rows(runner, "memory", tenant)) == 1 and runner._workspaces.slots.held == 0


async def test_a_wait_cancelled_after_its_grant_while_it_is_recorded_gives_the_slot_back():
    """Finding 2: the await of the WorkspaceWaiting record sat outside take's try."""
    from agentsdk.workspace.manager import _Slots

    slots = _Slots(1)
    await slots.take(None)
    telling = asyncio.Event()

    async def slow(held):
        telling.set()
        await asyncio.sleep(0.5)

    waiter = asyncio.ensure_future(slots.take(slow))
    await telling.wait()
    slots.give()  # handed over while the wait is still being recorded
    await asyncio.sleep(0.05)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert slots.held == 0
    await asyncio.wait_for(slots.take(None), 1)


@pytest.mark.parametrize("named", ["ended", "never ran"])
async def test_a_run_naming_a_top_level_run_not_under_way_is_its_own_plans_top(named):
    """Finding 3: its plan workspace was never removed, and held a slot until aclose()."""
    tenant, prov = tenancy(), provider()
    runner = runner_for(prov)
    if named == "ended":
        top = (await run(runner, space(scope="plan"), [[["write_workspace_file", {"path": "a", "content": "x"}]]],
                         tenant)).run_id
    else:
        top = str(uuid.uuid4())
    late = await run(runner, space(scope="plan"), [[["write_workspace_file", {"path": "b", "content": "y"}]]],
                     tenant, ledger_run_id=top)
    assert late.status is RunStatus.COMPLETED, late.error
    rows = await workspace_rows(runner, "memory", tenant)
    assert all(why == "run_ended" for *_, why in rows), rows
    assert [owner for _, owner, _, _ in rows][-1] == late.run_id
    assert await held(prov) == [] and runner._workspaces.slots.held == 0


async def test_a_plan_whose_top_level_run_ended_its_workspaces_is_never_joined_again():
    """The window between a top-level run's end_plans and its leaving the Runner's runs under
    way: a binding then is its own top, not a new space under the ended plan."""
    from agentsdk.workspace.manager import WorkspaceManager
    from agentsdk.workspace.records import InMemoryWorkspaceStore

    async def call(fn, *args):
        return fn(*args)

    async def emit(kind, payload):
        pass

    store = InMemoryWorkspaceStore()
    manager = WorkspaceManager(provider(), lambda: store, 4)
    tenant, top, late = tenancy(), str(uuid.uuid4()), str(uuid.uuid4())
    await manager.end_plans(top, emit, call)
    binding = manager.for_run(space(scope="plan"), tenant, PROJECT, late, emit, call, plan_run_id=top)
    assert binding._space.run_id == late
    manager.forget(top)
    assert manager._ended == set()


@pytest.mark.parametrize("label", ["{host}:²:1", "{host}:١٢:1", "{host}:１:1"])
def test_an_owner_label_whose_pid_is_not_ascii_digits_is_alive(label):
    """Finding 4: '²'.isdigit() is True and int('²') raises."""
    assert ow.is_dead(label.format(host=HOST)) is False


async def test_one_unreadable_owner_label_does_not_stop_reaping_the_rest():
    tenant, prov = tenancy(), provider()
    await prov.create(space(), {"agentsdk.workspace": str(uuid.uuid4()), "agentsdk.owner": f"{HOST}:²:1"})
    dead = await prov.create(space(), {"agentsdk.workspace": str(uuid.uuid4()), "agentsdk.owner": _dead_label()})
    import agentsdk.workspace.manager as mgr

    real = mgr.is_dead

    def raising(label):
        if "²" in label:
            raise RuntimeError("a check that fails")
        return real(label)

    mgr.is_dead = raising
    try:
        runner = runner_for(prov)
        await run(runner, space(), [[["write_workspace_file", {"path": "a", "content": "x"}]]], tenant)
    finally:
        mgr.is_dead = real
    assert dead.workspace_id not in {h.workspace_id for h in await held(prov)}
    assert runner._workspaces._reaped is True


def test_an_export_mime_type_with_parameters_is_refused_by_the_schema():
    import jsonschema

    from agentsdk.workspace.tools import specs

    schema = specs(space())["export_workspace_file"].input_schema
    for ok in ("text/plain", "application/vnd.ms-excel", "text/csv"):
        jsonschema.validate({"path": "a", "mime_type": ok}, schema)
    for bad in ("text/plain; charset=utf-8", "text/plain;", "text", "/plain", "text/plain\n"):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({"path": "a", "mime_type": bad}, schema)


def _tar(*members) -> bytes:
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


async def test_the_fake_refuses_the_transfers_docker_refuses():
    """Finding 8: FakeProvider accepted duplicate members, a file under a file, and exports past
    Docker's 16 MiB and 10,000-member bounds."""
    prov = provider()
    handle = await prov.create(space(), {})
    for stream in (_tar(("a", b"1"), ("a", b"2")), _tar(("f", b"1"), ("f/g", b"2"))):
        with pytest.raises(ValueError):
            await prov.import_(handle, stream)
    assert await prov.list(handle, ".") == ([], 0)
    big = provider()
    handle = await big.create(space(), {})
    big._spaces[handle.workspace_id].files.write("big.bin", b"x" * (16 * 2**20 + 1))
    with pytest.raises(ValueError):
        await big.export(handle, "big.bin")
    many = provider()
    handle = await many.create(space(), {})
    for i in range(10_001):
        many._spaces[handle.workspace_id].files.write(f"d/{i}", b"")
    with pytest.raises(ValueError):
        await many.export(handle, "d")


# =================================================================================================
# The approved M38 refusal, superseded: plan and keep are served
# =================================================================================================


async def test_plan_and_keep_are_no_longer_refused():
    for spec in (space(scope="plan"), space(keep=True)):
        tenant, prov = tenancy(), provider()
        runner = runner_for(prov)
        result = await run(runner, spec, [[["write_workspace_file", {"path": "a", "content": "x"}]]], tenant)
        assert result.status is RunStatus.COMPLETED, result.error
        await runner.aclose()
        assert await held(prov) == []


# =================================================================================================
# FR-153: example 35
# =================================================================================================


@pytest.mark.parametrize("store", STORES)
async def test_example_35_holds_every_check_offline(store):
    example = _example("35_workspace_plan.py")
    if store == "postgres":
        checks = await example.offline(Persistence.postgres(DSN, create_schema=False), "fake",
                                       tenant=tenancy(), project=PROJECT)
    else:
        checks = await example.offline(None, "fake", tenant=tenancy(), project=PROJECT)
    failed = [name for name, ok in checks if not ok]
    assert checks and not failed, failed


def test_example_35_runs_offline_as_a_program():
    done = subprocess.run([sys.executable, str(REPO / "scripts" / "35_workspace_plan.py"), "--offline"],
                          capture_output=True, text=True, timeout=300, cwd=REPO)
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
    assert "FAIL" not in done.stdout


# =================================================================================================
# On a real daemon (pytest -m docker)
# =================================================================================================


def _docker_labelled(owner=None):
    flt = ["--filter", "label=agentsdk.workspace"] + (["--filter", f"label=agentsdk.owner={owner}"] if owner else [])
    containers = subprocess.run(["docker", "ps", "-aq", *flt], capture_output=True, text=True).stdout.split()
    volumes = subprocess.run(["docker", "volume", "ls", "-q", *flt], capture_output=True, text=True).stdout.split()
    return containers, volumes


@pytest.mark.docker
@pytest.mark.parametrize("store", STORES)
async def test_on_docker_an_export_is_an_artifact_and_a_link_is_refused(store):
    tenant, runner = tenancy(), runner_for(DockerProvider(), store)
    try:
        result = await run(runner, space(image=IMAGE), [
            [["run_command", {"argv": ["sh", "-c", "printf 'x,y\\n' > r.csv && ln -s r.csv l.csv"]}]],
            [["export_workspace_file", {"path": "r.csv", "mime_type": "text/csv"}],
             ["export_workspace_file", {"path": "l.csv"}]],
        ], tenant)
        results = await history_results(runner, result.run_id, tenant)
        ref = await runner._artifacts_for(tenant, PROJECT).metadata(results["t1-0"].content.strip())
        assert await runner._artifacts_for(tenant, PROJECT).get(ref.artifact_id) == b"x,y\n"
        command_id = events(result, EventType.WORKSPACE_COMMAND_ENDED)[0]["command_id"]
        assert ref.provenance.source_uri_or_hash.endswith(f"/r.csv?command={command_id}")
        assert results["t1-1"].is_error and error_types(result)["t1-1"] == "ToolExecutionError"
        assert await asyncio.to_thread(stored, "SELECT count(*) FROM artifacts WHERE source_run = %s::uuid",
                                       result.run_id) == [(1,)] if store == "postgres" else True
    finally:
        await runner.aclose()
    assert _docker_labelled(ow.this_owner()) == ([], [])


@pytest.mark.docker
@pytest.mark.parametrize("store", STORES)
async def test_on_docker_a_dead_owners_volume_and_container_are_reaped(store):
    tenant = tenancy()
    dead = runner_for(DockerProvider(), store)
    label = _dead_label()
    dead._workspaces.owner = label
    await run(dead, space(image=IMAGE, keep=True), [[["write_workspace_file", {"path": "a", "content": "x"}]]], tenant)
    [(gone_id, *_)] = await workspace_rows(dead, store, tenant)
    # A stopped container of the dead owner, left in its workspace, as a crash between commands leaves one.
    subprocess.run(["docker", "create", "--label", f"agentsdk.workspace={gone_id}", "--label", f"agentsdk.owner={label}",
                    "-v", f"{dk.volume_name(gone_id)}:/workspace", IMAGE, "true"], capture_output=True, check=True)
    # And a container of the dead owner whose volume is gone: found by its own labels (DECISION-b1498111).
    orphan = str(uuid.uuid4())
    subprocess.run(["docker", "create", "--label", f"agentsdk.workspace={orphan}", "--label", f"agentsdk.owner={label}",
                    "--label", f"agentsdk.tenant={tenant}", "--label", f"agentsdk.project={PROJECT}", IMAGE, "true"],
                   capture_output=True, check=True)
    assert _docker_labelled(label) != ([], [])
    runner = runner_for(DockerProvider(), store)
    try:
        result = await run(runner, space(image=IMAGE), [[["write_workspace_file", {"path": "b", "content": "y"}]]], tenant)
        assert result.status is RunStatus.COMPLETED, result.error
        assert _docker_labelled(label) == ([], [])
        if store == "postgres":
            assert {w: why for w, _, _, why in await workspace_rows(runner, store, tenant)}[gone_id] == "reaped"
    finally:
        await runner.aclose()
        subprocess.run(["docker", "rm", "-fv", *_docker_labelled(label)[0]], capture_output=True)
        subprocess.run(["docker", "volume", "rm", "-f", *_docker_labelled(label)[1]], capture_output=True)


@pytest.mark.docker
async def test_on_docker_aclose_leaves_nothing_the_runner_created():
    """NFR-44: after aclose() no container or volume the Runner created remains."""
    tenant, runner = tenancy(), runner_for(DockerProvider())
    await run(runner, space(image=IMAGE, keep=True), [[["write_workspace_file", {"path": "a", "content": "x"}]]], tenant)
    handle = await runner.start(agent(space(image=IMAGE)), json.dumps([[["run_command", {"argv": ["sleep", "120"]}]]]),
                                RunConfig(tenant_id=tenant, project_id=PROJECT))
    async for event in handle.events():
        if event.event_type is EventType.WORKSPACE_COMMAND_STARTED:
            break
    await asyncio.sleep(1)
    await runner.aclose()
    assert _docker_labelled(ow.this_owner()) == ([], [])
    await asyncio.wait_for(handle.result(), 60)


@pytest.mark.docker
@pytest.mark.parametrize("store", STORES)
async def test_on_docker_a_plan_shares_its_workspace_and_removes_it_at_the_end(store):
    tenant = tenancy()
    orchestrator, runner = orchestrated(DockerProvider(), store, [
        node("a", [[["run_command", {"argv": ["sh", "-c", "echo shared > a.txt"]}]]]),
        node("b", [[["export_workspace_file", {"path": "a.txt"}]]], deps=["a"]),
    ], {"coder": agent(space(image=IMAGE, scope="plan"))})
    try:
        result = await run_plan(orchestrator, runner, tenant)
        assert result.status is RunStatus.COMPLETED, result.error
        [(_, owner_run, scope, why)] = await workspace_rows(runner, store, tenant)
        assert (owner_run, scope, why) == (result.run_id, "plan", "run_ended")
        assert _docker_labelled(ow.this_owner()) == ([], [])
    finally:
        await runner.aclose()


@pytest.mark.docker
def test_example_35_runs_scripted_on_docker():
    done = subprocess.run([sys.executable, str(REPO / "scripts" / "35_workspace_plan.py"), "--scripted"],
                          capture_output=True, text=True, timeout=600, cwd=REPO)
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
    assert "FAIL" not in done.stdout
