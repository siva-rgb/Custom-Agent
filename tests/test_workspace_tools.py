"""M40 gate: timeouts and cancellation (FR-148, FR-149, AC-98, NFR-42, NFR-44).

A command ends on its own, on its timeout, or on its run's cancellation, and in every case its
container is gone before its result is recorded. On cancellation the manager kills the command
through its provider, bounded at 10 s, and a kill that fails is recorded as `kill_error` on
`WorkspaceCommandEnded` (DECISION-d4ede901). M39's carried items come first, each with a test
that fails without it (DECISION-bf71f0f2).

The gates run on `FakeProvider`; tests marked `docker` hold the same against a real daemon, run
live before review with `pytest -m docker` (AC-98). Store calls from async tests go to a worker
thread (FR-81); the persisted half removes every row it writes, by the run ids of its own tenancies.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
import tarfile
import time
import uuid
from pathlib import Path

import psycopg
import pytest

from agentsdk import AgentSpec, Persistence, RunConfig, RunStatus, Runner
from agentsdk.config import normalise_database_url
from agentsdk.errors import WorkspaceUnavailable
from agentsdk.events import EventType
from agentsdk.identity import PrincipalContext
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.postgres import RunScope
from agentsdk.primitives import InstructionAuthority, Message, Origin, Role, TaintFlag, ToolCall, TrustZone
from agentsdk.tools import ApprovalPolicy
from agentsdk.workspace import DockerProvider, FakeProvider, WorkspaceHandle, WorkspaceSpec
from agentsdk.workspace import docker as dk

REPO = Path(__file__).resolve().parent.parent
DSN = normalise_database_url(os.environ.get("DATABASE_URL"))
PREFIX = "SYN-m40-"
PROJECT = "p-m40"
STORES = ["memory", "postgres"]
FAKE_IMAGE = "fake/workspace@sha256:" + "0" * 64
# The example image, present on the host and never pulled (FR-137); docker tests need it.
IMAGE = "python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df"
# redis:7.4-alpine, which declares VOLUME /data: on this host already, never pulled.
VOLUME_IMAGE = "redis@sha256:e7723ff73d963f5cc6d9c4643ea3d989527a402a319239054e9472a7fb9219a2"
OPERATOR = PrincipalContext(agent_principal="approval-ui", user_principal="owner")
TOOLS = ("run_command", "read_workspace_file", "write_workspace_file", "list_workspace")
# A process that ignores SIGTERM and SIGINT, with a child that left its process group (FR-148).
STUBBORN = ["sh", "-c", "setsid sh -c 'trap : TERM INT; while :; do sleep 1; done' & "
                        "trap : TERM INT; while :; do sleep 1; done"]


@pytest.fixture(autouse=True)
def remove_what_the_test_wrote():
    yield
    if DSN:
        with psycopg.connect(DSN, autocommit=True) as conn:
            ids = [r[0] for r in conn.execute("SELECT run_id FROM runs WHERE tenant_id LIKE %s", (PREFIX + "%",))]
            conn.execute("DELETE FROM workspace_commands WHERE run_id = ANY(%s)", (ids,))
            conn.execute("DELETE FROM workspaces WHERE run_id = ANY(%s)", (ids,))
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


async def _say(cmd):
    cmd.out(" ".join(cmd.argv[1:]) + "\n")
    return 0


class Killing(FakeProvider):
    """A FakeProvider that counts each kill and says when a command has started."""

    def __init__(self):
        super().__init__(commands={"nap": _nap, "say": _say})
        self.kills, self.started = [], asyncio.Event()

    async def run(self, handle, argv, **kwargs):
        self.started.set()
        return await super().run(handle, argv, **kwargs)

    async def kill(self, handle, command_id):
        self.kills.append(command_id)
        await super().kill(handle, command_id)


class FailingKill(Killing):
    """Its kill ends the command, then reports a failure, as a docker rm -f that errs can."""

    async def kill(self, handle, command_id):
        await super().kill(handle, command_id)
        raise RuntimeError("the kill could not be confirmed")


class HangingKill(Killing):
    """Its kill never returns, as a daemon that stopped answering."""

    async def kill(self, handle, command_id):
        self.kills.append(command_id)
        await asyncio.sleep(3600)


def respond(content="", calls=()):
    return ModelResponse(message=Message(role=Role.ASSISTANT, content=content, tool_calls=tuple(calls)),
                         stop_reason=StopReason.TOOL_CALLS if calls else StopReason.END_TURN, usage=Usage(5, 1, 6))


class Turns:
    """The model: its task is a JSON list of turns, each a list of [tool, arguments]."""

    async def send(self, request):
        turns = json.loads(request.messages[0].content.split("\n")[0])
        turn = sum(1 for m in request.messages if m.role is Role.ASSISTANT)
        if turn < len(turns):
            return respond(calls=[ToolCall(id=f"t{turn}-{i}", name=name, arguments=args)
                                 for i, (name, args) in enumerate(turns[turn])])
        return respond("done")


def agent(space: WorkspaceSpec) -> AgentSpec:
    return AgentSpec(id="coder", instructions="use the workspace", preferred_model="m:x", tool_profile=TOOLS,
                     workspace=space)


def runner_for(provider, store="memory"):
    return Runner({"m": Turns()}, workspace_provider=provider,
                  persistence=Persistence.postgres(DSN, create_schema=False) if store == "postgres" else None)


async def results(runner, result, tenant):
    sessions = runner._sessions_for(RunScope(run_id=result.run_id, tenant_id=tenant, project_id=PROJECT))
    history = await asyncio.to_thread(sessions.history, result.run_id)
    return {r.tool_call_id: r for m in history for r in m.tool_results}


def error_types(result) -> dict:
    """Each call's error class, from its ToolCalled event: never read off a result's text
    (DECISION-94bfee7c)."""
    return {e.payload["tool_call_id"]: e.payload.get("error_type")
            for e in result.events if e.event_type is EventType.TOOL_CALLED}


def events(result, kind):
    return [e.payload for e in result.events if e.event_type is kind]


def kinds(result):
    return [e.event_type for e in result.events]


def stored(sql, *args):
    with psycopg.connect(DSN) as conn:
        return conn.execute(sql, args).fetchall()


def commands_of(runner, store, tenant, run_id):
    """The run's command rows, as (argv, exit_code, killed), from the store the Runner used."""
    if store == "postgres":
        return [(list(argv), code, killed) for argv, code, killed in stored(
            "SELECT argv, exit_code, killed FROM workspace_commands WHERE run_id = %s::uuid ORDER BY started_at",
            run_id)]
    return [(c.argv, c.exit_code, c.killed) for c in runner._workspace_store().commands(tenant, PROJECT, run_id=run_id)]


async def cancelled_mid_command(runner, provider, spec, argv, tenant, *, started=None):
    """Starts a run whose one call runs `argv`, cancels it once the command has begun, and returns
    the result and the seconds from the cancel to the result."""
    handle = await runner.start(agent(spec), json.dumps([[["run_command", {"argv": argv}]]]),
                                RunConfig(tenant_id=tenant, project_id=PROJECT))
    await (started() if started else asyncio.wait_for(provider.started.wait(), 30))
    await asyncio.sleep(0.3)
    handle.cancel()
    began = time.monotonic()
    result = await handle.result()
    return result, time.monotonic() - began


def _example(name: str):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), REPO / "scripts" / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# =================================================================================================
# M39's carried items (DECISION-bf71f0f2, DECISION-d4ede901)
# =================================================================================================


@pytest.mark.parametrize("cpus", [1e-12, 1e-06, 1e-05, 0.001, 0.005, 1 / 3, 0.125, 2.999, 16.01])
def test_cpus_outside_whole_hundredths_or_its_range_is_refused(cpus):
    """1e-06 reached docker as '1e-06' and gave no CPU limit at all; 1/3 failed every create."""
    with pytest.raises(ValueError):
        WorkspaceSpec(image=FAKE_IMAGE, cpus=cpus)


@pytest.mark.parametrize("cpus, flag", [(0.01, "0.01"), (0.29, "0.29"), (0.5, "0.50"), (1, "1.00"), (2.5, "2.50"),
                                        (16, "16.00")])
def test_cpus_in_whole_hundredths_reach_docker_as_fixed_decimals(cpus, flag):
    args = dk.hardening(WorkspaceSpec(image=FAKE_IMAGE, cpus=cpus))
    assert args[args.index("--cpus") + 1] == flag


def test_a_command_container_keeps_no_log_on_the_daemon():
    """P5-D6 amended: the daemon stores nothing of a command's output (DECISION-d4ede901)."""
    args = dk.hardening(WorkspaceSpec(image=FAKE_IMAGE))
    assert args[args.index("--log-driver") + 1] == "none"
    assert "--log-opt" not in args


class ScriptedCli(DockerProvider):
    """A DockerProvider whose docker calls are answered by exit code per subcommand, and recorded."""

    def __init__(self, codes=None):
        super().__init__()
        self.calls, self.codes = [], dict(codes or {})

    async def _cli(self, *args, **kwargs):
        self.calls.append(list(args))
        return self.codes.get(args[0], 0), b"", b""


def _running(provider: DockerProvider) -> WorkspaceHandle:
    handle = WorkspaceHandle(workspace_id="w-1", spec=WorkspaceSpec(image=FAKE_IMAGE), labels={})
    space = dk._Space(handle)
    space.running["c-1"] = dk._Command("agentsdk-cmd-1")
    provider._spaces["w-1"] = space
    return handle


async def test_every_container_is_removed_with_the_volumes_its_image_declares():
    """M39 caveat: rm -f left an image's declared VOLUME behind, one per command."""
    provider = ScriptedCli()
    await provider._rm("agentsdk-cmd-x", "agentsdk-cmd-y")
    await provider.kill(_running(provider), "c-1")
    assert [c for c in provider.calls if c[0] == "rm"] == [["rm", "-fv", "agentsdk-cmd-x", "agentsdk-cmd-y"],
                                                           ["rm", "-fv", "agentsdk-cmd-1"]]


async def test_a_kill_that_leaves_the_container_standing_raises():
    provider = ScriptedCli({"rm": 1, "inspect": 0})
    handle = _running(provider)
    with pytest.raises(WorkspaceUnavailable):
        await provider.kill(handle, "c-1")
    assert provider._spaces["w-1"].running["c-1"].kill.is_set(), "the command was not told it is killed"


async def test_a_kill_of_a_container_already_gone_returns():
    provider = ScriptedCli({"rm": 1, "inspect": 1})
    await provider.kill(_running(provider), "c-1")
    await provider.kill(_running(provider), "no-such-command")


async def test_a_kill_whose_daemon_cannot_say_the_container_is_gone_raises():
    """M40 round 1 caveat: a failed rm with a daemon that cannot answer inspect read as gone, so
    kill_error was None. Asked by `docker version`, never by docker's message (DECISION-f3e99c07)."""
    provider = ScriptedCli({"rm": 1, "inspect": 1, "version": 1})
    with pytest.raises(WorkspaceUnavailable):
        await provider.kill(_running(provider), "c-1")
    assert [c[0] for c in provider.calls] == ["rm", "inspect", "version"]


def test_cpus_is_kept_as_docker_receives_it():
    """M40 round 1 caveat: a value within 1e-9 of a hundredth reached docker rounded but was
    recorded as given (DECISION-f3e99c07)."""
    spec = WorkspaceSpec(image=FAKE_IMAGE, cpus=0.29 + 5e-12)
    assert spec.cpus == 0.29 and spec.record()["cpus"] == 0.29
    assert WorkspaceSpec(image=FAKE_IMAGE, cpus=2).record()["cpus"] == 2


def _ended(code, killed=None, out=b"", cut=False):
    stdout, stderr = dk._Kept(1 << 20), dk._Kept(1 << 20)
    stdout.data += out
    stdout.count = len(out) + (1 if cut else 0)
    return dk._Ended(code, stdout, stderr, killed)


@pytest.mark.parametrize("ended, error", [
    (_ended(22), PermissionError), (_ended(23), PermissionError), (_ended(20), FileNotFoundError),
    (_ended(21), NotADirectoryError), (_ended(1), WorkspaceUnavailable), (_ended(None, "timeout"), WorkspaceUnavailable),
    (_ended(0, out=b"file\t1\ta\0"), WorkspaceUnavailable), (_ended(0, out=b"total\t1\t\0", cut=True), WorkspaceUnavailable),
])
async def test_a_listing_that_fails_raises_a_documented_error(monkeypatch, ended, error):
    """M39 review: an unreadable folder raised a bare RuntimeError, outside list's documented types."""
    provider = DockerProvider()
    handle = _running(provider)

    async def contained(*args, **kwargs):
        return ended

    monkeypatch.setattr(provider, "_contained", contained)
    with pytest.raises(error) as caught:
        await provider.list(handle, "sub")
    assert type(caught.value) is error


@pytest.mark.parametrize("ended", [_ended(1), _ended(None, "timeout"), _ended(0, out=b"")])
async def test_a_disk_reading_that_fails_raises_workspace_unavailable(monkeypatch, ended):
    provider = DockerProvider()
    handle = _running(provider)

    async def contained(*args, **kwargs):
        return ended

    monkeypatch.setattr(provider, "_contained", contained)
    with pytest.raises(WorkspaceUnavailable):
        await provider.size(handle)


def _tar(*entries) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            if data is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


async def test_the_fake_counts_a_workspaces_size_as_du_does():
    """M39 review: the fake counted file bytes only, so 2000 empty folders read 0 B on the fake and
    8 MB on Docker; a bound tuned on the fake did not hold (DECISION-d4ede901)."""
    provider = FakeProvider()
    handle = await provider.create(WorkspaceSpec(image=FAKE_IMAGE), {})
    assert await provider.size(handle) == 0, "the root is not counted"
    await provider.import_(handle, _tar(("a/b/c.txt", b"x"), ("d.bin", b"y" * 5000), ("e.txt", b""), ("f", None)))
    # Folders a, a/b and f: 4 KiB each; c.txt one block, d.bin two, e.txt none.
    assert await provider.size(handle) == 3 * 4096 + 4096 + 2 * 4096


async def test_example_32_judges_the_refused_bound_by_its_error_class_not_its_text(monkeypatch):
    """M39 caveat 7: example 32's refusal check read the error's class, but no test failed when it
    read the text instead -- a refusal's text began with the class name anyway. Here the text
    holds no class name, so only a check by class passes."""
    import agentsdk.executor as executor

    monkeypatch.setattr(executor, "describe_exception", lambda exc: str(exc))
    example = _example("32_workspace.py")
    example.TENANT = tenancy()
    runner = Runner({"scripted": example.ScriptedModel()}, workspace_provider=example.provider())
    checks, _ = await example.demonstrate(runner, "scripted:model", live=False, persisted=False)
    verdicts = dict(checks)
    assert verdicts["past max_workspace_bytes the next command was refused, naming the bound"] is True, checks


# =================================================================================================
# FR-148: a command past its timeout, and one whose run is cancelled
# =================================================================================================


async def test_a_command_past_its_timeout_is_a_result_the_model_reads():
    tenant = tenancy()
    provider = Killing()
    runner = runner_for(provider)
    spec = WorkspaceSpec(image=FAKE_IMAGE, run_command_approval=ApprovalPolicy.AUTO)
    result = await runner.run(agent(spec), json.dumps([[["run_command", {"argv": ["nap", "30"],
                                                                         "timeout_seconds": 0.5}]]]),
                              RunConfig(tenant_id=tenant, project_id=PROJECT))
    got = (await results(runner, result, tenant))["t0-0"]
    assert result.status is RunStatus.COMPLETED and error_types(result) == {"t0-0": None}
    assert not got.is_error and "killed: timeout" in got.content, got.content
    [ended] = events(result, EventType.WORKSPACE_COMMAND_ENDED)
    assert (ended["killed"], ended["exit_code"], ended["kill_error"]) == ("timeout", None, None)


@pytest.mark.parametrize("store", STORES)
async def test_a_cancelled_runs_command_is_killed_through_its_provider_and_recorded(store):
    tenant = tenancy()
    provider = Killing()
    runner = runner_for(provider, store)
    result, waited = await cancelled_mid_command(runner, provider, WorkspaceSpec(
        image=FAKE_IMAGE, run_command_approval=ApprovalPolicy.AUTO), ["nap", "30"], tenant)
    assert result.status is RunStatus.CANCELLED and waited < 10
    [started] = events(result, EventType.WORKSPACE_COMMAND_STARTED)
    assert provider.kills == [started["command_id"]], "the command was not killed through its provider"
    [ended] = events(result, EventType.WORKSPACE_COMMAND_ENDED)
    assert (ended["killed"], ended["exit_code"], ended["kill_error"]) == ("cancelled", None, None)
    order = kinds(result)
    assert order.index(EventType.WORKSPACE_COMMAND_ENDED) < order.index(EventType.WORKSPACE_REMOVED) < len(order) - 1
    assert await asyncio.to_thread(commands_of, runner, store, tenant, result.run_id) == [
        (["nap", "30"], None, "cancelled")]


@pytest.mark.parametrize("store", STORES)
async def test_a_kill_that_fails_is_recorded_and_the_run_still_reports_cancelled(store):
    tenant = tenancy()
    provider = FailingKill()
    runner = runner_for(provider, store)
    result, waited = await cancelled_mid_command(runner, provider, WorkspaceSpec(
        image=FAKE_IMAGE, run_command_approval=ApprovalPolicy.AUTO), ["nap", "30"], tenant)
    assert result.status is RunStatus.CANCELLED and waited < 10
    [ended] = events(result, EventType.WORKSPACE_COMMAND_ENDED)
    assert (ended["killed"], ended["kill_error"]) == ("cancelled", "RuntimeError"), ended
    assert await asyncio.to_thread(commands_of, runner, store, tenant, result.run_id) == [
        (["nap", "30"], None, "cancelled")]


async def test_a_kill_that_never_returns_is_bounded_at_10_s_and_recorded():
    """FR-148: the run reports cancelled only after the kill returns, bounded at 10 s (FR-50)."""
    tenant = tenancy()
    provider = HangingKill()
    runner = runner_for(provider)
    result, waited = await cancelled_mid_command(runner, provider, WorkspaceSpec(
        image=FAKE_IMAGE, run_command_approval=ApprovalPolicy.AUTO), ["nap", "60"], tenant)
    assert result.status is RunStatus.CANCELLED
    assert 9 < waited < 15, f"the cancelled run took {waited:.1f} s to report"
    [ended] = events(result, EventType.WORKSPACE_COMMAND_ENDED)
    assert (ended["killed"], ended["kill_error"]) == ("cancelled", "TimeoutError"), ended


class SlowSize(Killing):
    """Reading its size takes a minute, as a du container on a slow daemon can."""

    def __init__(self):
        super().__init__()
        self.readings = 0

    async def size(self, handle):
        self.readings += 1
        await asyncio.sleep(60)
        return await super().size(handle)


async def test_a_cancellation_reads_no_size_so_a_slow_reading_cannot_hold_it():
    """Pre-review F1: after the kill, the size was read (up to 120 s) before the run reported
    cancelled; with the du container held 20 s it reported 20.9 s after the cancel."""
    tenant = tenancy()
    provider = SlowSize()
    runner = runner_for(provider)
    result, waited = await cancelled_mid_command(runner, provider, WorkspaceSpec(
        image=FAKE_IMAGE, run_command_approval=ApprovalPolicy.AUTO), ["nap", "30"], tenant)
    assert result.status is RunStatus.CANCELLED and waited < 5, waited
    assert provider.readings == 0
    [ended] = events(result, EventType.WORKSPACE_COMMAND_ENDED)
    assert ended["killed"] == "cancelled"


async def test_after_a_cancelled_command_the_next_use_reads_the_size_first_and_fails_closed():
    """DECISION-fe7243f1: the reading a cancelled command skipped is taken by the next command or
    write in that workspace, before it runs; a reading that fails refuses it (FR-144)."""
    from agentsdk.errors import WorkspaceRefused
    from agentsdk.workspace.manager import WorkspaceManager
    from agentsdk.workspace.records import InMemoryWorkspaceStore

    class Unreadable(Killing):
        def __init__(self):
            super().__init__()
            self.readings, self.ran = 0, []

        async def size(self, handle):
            self.readings += 1
            raise WorkspaceUnavailable("du could not read it")

        async def run(self, handle, argv, **kwargs):
            self.ran.append(list(argv))
            return await super().run(handle, argv, **kwargs)

    async def call(fn, *args):
        return fn(*args)

    async def emit(kind, payload):
        seen.append((kind, payload))

    seen: list = []
    store = InMemoryWorkspaceStore()
    provider = Unreadable()
    space = WorkspaceManager(provider, lambda: store, 4).for_run(
        WorkspaceSpec(image=FAKE_IMAGE), tenancy(), PROJECT, str(uuid.uuid4()), emit, call)
    first = asyncio.ensure_future(space.run(["nap", "30"], stdin=None, timeout=60, tool_call_id="c-1"))
    await asyncio.wait_for(provider.started.wait(), 30)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert provider.readings == 0, "the cancelled command's size was read on the cancel path"
    for attempt in (lambda: space.run(["say", "x"], stdin=None, timeout=60, tool_call_id="c-2"),
                    lambda: space.import_(_tar(("a.txt", b"x")))):
        with pytest.raises(WorkspaceRefused):
            await attempt()
    assert provider.readings == 1 and provider.ran == [["nap", "30"]], (provider.readings, provider.ran)
    refused = [p for kind, p in seen if kind is EventType.WORKSPACE_REFUSED]
    assert [p["unread"] for p in refused] == ["WorkspaceUnavailable", "WorkspaceUnavailable"], refused
    await space.end("run_ended", emit)


class Reading(Killing):
    """A FakeProvider whose size reading can be made slow (a minute) or failing, and is counted."""

    def __init__(self, mode="ok"):
        super().__init__()
        self.mode, self.readings, self.reading, self.ran = mode, 0, asyncio.Event(), []

    async def run(self, handle, argv, **kwargs):
        self.ran.append(list(argv))
        return await super().run(handle, argv, **kwargs)

    async def size(self, handle):
        self.readings += 1
        self.reading.set()
        if self.mode == "slow":
            await asyncio.sleep(60)
        if self.mode == "fail":
            raise WorkspaceUnavailable("du could not read it")
        return await super().size(handle)


def _bound(provider, seen):
    """A RunWorkspace on `provider`, in memory, its events into `seen`."""
    from agentsdk.workspace.manager import WorkspaceManager
    from agentsdk.workspace.records import InMemoryWorkspaceStore

    async def call(fn, *args):
        return fn(*args)

    async def emit(kind, payload):
        seen.append((kind, payload))

    store = InMemoryWorkspaceStore()
    space = WorkspaceManager(provider, lambda: store, 4).for_run(
        WorkspaceSpec(image=FAKE_IMAGE), tenancy(), PROJECT, str(uuid.uuid4()), emit, call)
    return space, store, emit


@pytest.mark.parametrize("store", STORES)
async def test_a_cancel_during_the_reading_after_a_command_ended_still_records_it(store):
    """M40 round 1: a cancellation that arrived while the size was read after a command had ended
    left only WorkspaceCommandStarted -- no WorkspaceCommandEnded and no row. Now the reading is
    abandoned, the workspace marked stale, and the command's real outcome recorded
    (DECISION-f3e99c07), without waiting for the reading."""
    tenant = tenancy()
    provider = Reading("slow")
    runner = runner_for(provider, store)
    handle = await runner.start(agent(WorkspaceSpec(image=FAKE_IMAGE, run_command_approval=ApprovalPolicy.AUTO)),
                                json.dumps([[["run_command", {"argv": ["say", "hi"]}]]]),
                                RunConfig(tenant_id=tenant, project_id=PROJECT))
    await asyncio.wait_for(provider.reading.wait(), 30)
    handle.cancel()
    began = time.monotonic()
    result = await handle.result()
    assert result.status is RunStatus.CANCELLED and time.monotonic() - began < 5
    [ended] = events(result, EventType.WORKSPACE_COMMAND_ENDED)
    assert (ended["exit_code"], ended["killed"], ended["kill_error"], ended["stdout_bytes"]) == (0, None, None, 3)
    assert await asyncio.to_thread(commands_of, runner, store, tenant, result.run_id) == [(["say", "hi"], 0, None)]
    order = kinds(result)
    assert order.index(EventType.WORKSPACE_COMMAND_ENDED) < order.index(EventType.WORKSPACE_REMOVED) < len(order) - 1


async def test_after_a_cancel_during_the_reading_the_next_use_reads_first():
    """DECISION-f3e99c07: the reading a cancellation abandoned is left to the next use, which
    takes it before anything runs and fails closed."""
    seen: list = []
    provider = Reading("slow")
    space, _, emit = _bound(provider, seen)
    first = asyncio.ensure_future(space.run(["say", "x"], stdin=None, timeout=60, tool_call_id="c-1"))
    await asyncio.wait_for(provider.reading.wait(), 30)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert [p["exit_code"] for kind, p in seen if kind is EventType.WORKSPACE_COMMAND_ENDED] == [0]
    provider.mode = "fail"
    from agentsdk.errors import WorkspaceRefused

    with pytest.raises(WorkspaceRefused):
        await space.run(["say", "y"], stdin=None, timeout=60, tool_call_id="c-2")
    assert provider.ran == [["say", "x"]] and provider.readings == 2, (provider.ran, provider.readings)
    await space.end("run_ended", emit)


async def test_a_cancel_during_the_deferred_reading_keeps_the_workspace_stale():
    """M40 round 1: the stale mark was cleared before the deferred reading was awaited, so a
    cancellation during it let the next command run unmeasured (fail open)."""
    seen: list = []
    provider = Reading()
    space, _, emit = _bound(provider, seen)
    first = asyncio.ensure_future(space.run(["nap", "30"], stdin=None, timeout=60, tool_call_id="c-1"))
    await asyncio.wait_for(provider.started.wait(), 30)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    provider.mode, provider.reading = "slow", asyncio.Event()
    second = asyncio.ensure_future(space.run(["say", "x"], stdin=None, timeout=60, tool_call_id="c-2"))
    await asyncio.wait_for(provider.reading.wait(), 30)
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    provider.mode = "fail"
    from agentsdk.errors import WorkspaceRefused

    with pytest.raises(WorkspaceRefused):
        await space.run(["say", "y"], stdin=None, timeout=60, tool_call_id="c-3")
    assert provider.ran == [["nap", "30"]] and provider.readings == 2, (provider.ran, provider.readings)
    await space.end("run_ended", emit)


async def test_a_write_cancelled_during_its_reading_leaves_the_workspace_stale():
    """M40 round 1: a write cancelled while its size was read set no stale mark, so the next
    command ran without the reading."""
    seen: list = []
    provider = Reading("slow")
    space, _, emit = _bound(provider, seen)
    writing = asyncio.ensure_future(space.import_(_tar(("a.txt", b"x" * 100))))
    await asyncio.wait_for(provider.reading.wait(), 30)
    writing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await writing
    provider.mode = "fail"
    from agentsdk.errors import WorkspaceRefused

    with pytest.raises(WorkspaceRefused):
        await space.run(["say", "y"], stdin=None, timeout=60, tool_call_id="c-1")
    assert provider.ran == [] and provider.readings == 2, (provider.ran, provider.readings)
    await space.end("run_ended", emit)


class Paced(Killing):
    """Each size reading takes 0.1 s, as a du container does, so a sweep of cancels lands in every
    phase of a command's life: its start, its run, its reading, its record."""

    async def size(self, handle):
        await asyncio.sleep(0.1)
        return await super().size(handle)


async def _swept(runner, provider, spec, turns, delay, tenant):
    handle = await runner.start(agent(spec), json.dumps(turns), RunConfig(tenant_id=tenant, project_id=PROJECT))
    await asyncio.sleep(delay)
    handle.cancel()
    return await handle.result()


def _ledger_holds(runner, result, tenant, store="memory") -> tuple[bool, str]:
    """Every command that started is ended once and recorded once (FR-140, FR-148)."""
    started = [p["command_id"] for p in events(result, EventType.WORKSPACE_COMMAND_STARTED)]
    ended = [p["command_id"] for p in events(result, EventType.WORKSPACE_COMMAND_ENDED)]
    if store == "postgres":
        rows = [r[0] for r in stored("SELECT command_id::text FROM workspace_commands WHERE run_id = %s::uuid",
                                     result.run_id)]
    else:
        rows = [c.command_id for c in runner._workspace_store().commands(tenant, PROJECT, run_id=result.run_id)]
    return sorted(started) == sorted(ended) == sorted(rows), f"started {started} ended {ended} rows {rows}"


async def test_wherever_a_cancel_lands_every_command_that_started_is_ended_and_recorded():
    """M40 round 1's lesson: when every command must end recorded, sweep the cancel across a
    command's whole life and check the record, not only the resources (KNOWLEDGE-d6b2ea68)."""
    spec = WorkspaceSpec(image=FAKE_IMAGE, run_command_approval=ApprovalPolicy.AUTO)
    turns = [[["run_command", {"argv": ["nap", "0.1"]}]], [["write_workspace_file", {"path": "a", "content": "x"}]],
             [["run_command", {"argv": ["say", "hi"]}]]]
    phases = set()
    for step in range(30):
        tenant = tenancy()
        provider = Paced()
        runner = runner_for(provider)
        result = await _swept(runner, provider, spec, turns, step * 0.02, tenant)
        held, why = _ledger_holds(runner, result, tenant)
        assert held, (step, why)
        phases.add(len(events(result, EventType.WORKSPACE_COMMAND_STARTED)))
    assert phases >= {0, 1, 2}, f"the sweep never reached some phase: {phases}"


async def test_a_command_that_ended_on_its_own_is_never_killed():
    tenant = tenancy()
    provider = Killing()
    runner = runner_for(provider)
    spec = WorkspaceSpec(image=FAKE_IMAGE, run_command_approval=ApprovalPolicy.AUTO)
    result = await runner.run(agent(spec), json.dumps([[["run_command", {"argv": ["say", "hi"]}]]]),
                              RunConfig(tenant_id=tenant, project_id=PROJECT))
    assert result.status is RunStatus.COMPLETED and provider.kills == []
    [ended] = events(result, EventType.WORKSPACE_COMMAND_ENDED)
    assert (ended["exit_code"], ended["killed"], ended["kill_error"]) == (0, None, None)


# =================================================================================================
# FR-149: example 34, offline on FakeProvider in the gates
# =================================================================================================


async def test_example_34_holds_on_both_stores_on_the_fake_provider():
    example = _example("34_sandboxed_tools.py")
    for store in STORES:
        example.TENANT = tenancy()
        checks = await example.offline(store)
        assert [label for label, passed in checks if not passed] == [] and len(checks) >= 9, (store, checks)


def test_example_34_runs_offline():
    done = subprocess.run([sys.executable, str(REPO / "scripts" / "34_sandboxed_tools.py"), "--offline"],
                          capture_output=True, text=True, timeout=240, cwd=REPO)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "[FAIL]" not in done.stdout and "[PASS]" in done.stdout, done.stdout


# =================================================================================================
# AC-98, against a running Docker daemon (marked docker: run live before review)
# =================================================================================================


def _docker(*args) -> str:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120).stdout


def _ours() -> tuple[list[str], list[str]]:
    """The containers and volumes this file's tests made: labelled by its tenants only."""
    containers, volumes = [], []
    for kind, out in (("c", _docker("ps", "-a", "--filter", "label=agentsdk.workspace", "--format",
                                    '{{.ID}} {{.Label "agentsdk.tenant"}}')),
                      ("v", _docker("volume", "ls", "--filter", "label=agentsdk.workspace", "--format",
                                    '{{.Name}} {{.Label "agentsdk.tenant"}}'))):
        for line in out.splitlines():
            name, _, tenant = line.partition(" ")
            if tenant.startswith(PREFIX):
                (containers if kind == "c" else volumes).append(name)
    return containers, volumes


@pytest.fixture
def docker_daemon():
    try:
        up = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        up = False
    if not up:
        pytest.skip("docker is not running, or the example image is not on this host")
    yield
    containers, volumes = _ours()
    if containers:
        _docker("rm", "-f", *containers)
    if volumes:
        _docker("volume", "rm", "-f", *volumes)
    assert _ours() == ([], []), "a test left a container or volume behind"
    assert (containers, volumes) == ([], []), f"left behind by the provider: {containers} {volumes}"


def _containers_of(label: str, value: str) -> list[str]:
    return _docker("ps", "-aq", "--filter", f"label={label}={value}").split()


async def _approving(runner, tenant, run_id, decided, refuse=("rm",)):
    """The operator: approves each command, but refuses one whose argv starts with `refuse`."""
    while True:
        for view in await runner.interruptions(tenant, PROJECT, run_id=run_id):
            argv = view.interruption.request_payload["arguments"]["argv"]
            approve = argv[0] not in refuse
            await runner.decide(tenant, PROJECT, view.interruption.interruption_id, approve=approve,
                                principal=OPERATOR, message=None if approve else "not this one")
            decided.append((view.interruption.tool_call_id, approve))
        await asyncio.sleep(0.05)


@pytest.mark.docker
@pytest.mark.parametrize("store", STORES)
async def test_the_tools_and_their_approval_hold_on_docker(docker_daemon, store):
    """AC-98: FR-146 and FR-147's behaviour, settled on FakeProvider in M38, on DockerProvider."""
    tenant = tenancy()
    runner = runner_for(DockerProvider(), store)
    turns = [[["write_workspace_file", {"path": "src/prog.py", "content": "open('out.txt', 'w').write('45')\n"}]],
             [["run_command", {"argv": ["python", "src/prog.py"]}]],
             [["read_workspace_file", {"path": "out.txt"}], ["list_workspace", {}]],
             [["run_command", {"argv": ["rm", "-rf", "src"]}]],
             [["read_workspace_file", {"path": "src/prog.py"}]]]
    handle = await runner.start(agent(WorkspaceSpec(image=IMAGE)), json.dumps(turns),
                                RunConfig(tenant_id=tenant, project_id=PROJECT))
    decided: list = []
    approving = asyncio.ensure_future(_approving(runner, tenant, handle.run_id, decided))
    try:
        result = await handle.result()
    finally:
        approving.cancel()
    got = await results(runner, result, tenant)
    assert result.status is RunStatus.COMPLETED, result.error
    assert decided == [("t1-0", True), ("t3-0", False)], decided
    assert error_types(result)["t3-0"] == "ToolApprovalRejected"
    assert [p["argv"] for p in events(result, EventType.WORKSPACE_COMMAND_STARTED)] == [["python", "src/prog.py"]]
    assert "exit code: 0" in got["t1-0"].content and got["t2-0"].content == "45"
    assert "out.txt" in got["t2-1"].content and "src" in got["t2-1"].content
    assert "open('out.txt'" in got["t4-0"].content, "the refused command ran"
    for key, r in got.items():
        if error_types(result).get(key) == "ToolApprovalRejected":
            continue
        p = r.provenance
        assert (p.origin, p.trust_zone, p.instruction_authority) == (
            Origin.INTERNAL_TOOL, TrustZone.UNTRUSTED, InstructionAuthority.DATA_ONLY), (key, p)
        assert {TaintFlag.EXTERNAL_CONTENT, TaintFlag.EXECUTABLE_CONTENT} <= set(p.taint_flags), (key, p)
    assert _containers_of("agentsdk.run", result.run_id) == []


@pytest.mark.docker
async def test_a_stubborn_command_past_its_timeout_is_gone_when_its_outcome_returns(docker_daemon):
    provider = DockerProvider()
    handle = await provider.create(WorkspaceSpec(image=IMAGE), {"agentsdk.tenant": tenancy()})
    try:
        started = time.monotonic()
        outcome = await provider.run(handle, STUBBORN, timeout=3)
        assert (outcome.exit_code, outcome.killed) == (None, "timeout"), outcome
        assert _containers_of("agentsdk.workspace", handle.workspace_id) == [], "the container outlived its timeout"
        assert time.monotonic() - started < 20
    finally:
        await provider.remove(handle)


@pytest.mark.docker
@pytest.mark.parametrize("store", STORES)
async def test_a_cancelled_run_reports_cancelled_only_once_its_stubborn_command_is_gone(docker_daemon, store):
    """AC-98: a process ignoring SIGTERM, with a child that left its group, ends with its container,
    and the run reports cancelled only after that, within 10 s of the cancel."""
    tenant = tenancy()
    provider = DockerProvider()
    runner = runner_for(provider, store)
    handle = await runner.start(agent(WorkspaceSpec(image=IMAGE, run_command_approval=ApprovalPolicy.AUTO)),
                                json.dumps([[["run_command", {"argv": STUBBORN}]]]),
                                RunConfig(tenant_id=tenant, project_id=PROJECT))
    deadline = time.monotonic() + 60
    while not _docker("ps", "-q", "--filter", f"label=agentsdk.run={handle.run_id}",
                      "--filter", "name=agentsdk-cmd-").split():
        assert time.monotonic() < deadline, "the command never started"
        await asyncio.sleep(0.2)
    await asyncio.sleep(2)  # the child has left its group by now
    handle.cancel()
    began = time.monotonic()
    result = await handle.result()
    waited = time.monotonic() - began
    assert _containers_of("agentsdk.run", result.run_id) == [], "a container outlived the cancelled run"
    assert result.status is RunStatus.CANCELLED and waited < 10, (result.status, waited)
    [ended] = events(result, EventType.WORKSPACE_COMMAND_ENDED)
    assert (ended["killed"], ended["kill_error"]) == ("cancelled", None), ended
    assert await asyncio.to_thread(commands_of, runner, store, tenant, result.run_id) == [
        (STUBBORN, None, "cancelled")]


@pytest.mark.docker
async def test_a_docker_kill_that_fails_is_recorded_and_the_container_still_goes(docker_daemon):
    class FirstRemovalFails(DockerProvider):
        """The command container's first two removals fail: the kill's, and the command's own
        that races it, so the kill finds its container still standing whichever comes first."""

        def __init__(self):
            super().__init__()
            self.failed = 0

        async def _cli(self, *args, **kwargs):
            if args[:2] == ("rm", "-fv") and args[2].startswith("agentsdk-cmd-") and self.failed < 2:
                self.failed += 1
                return 1, b"", b""
            return await super()._cli(*args, **kwargs)

    tenant = tenancy()
    provider = FirstRemovalFails()
    runner = runner_for(provider)
    handle = await runner.start(agent(WorkspaceSpec(image=IMAGE, run_command_approval=ApprovalPolicy.AUTO)),
                                json.dumps([[["run_command", {"argv": ["sleep", "60"]}]]]),
                                RunConfig(tenant_id=tenant, project_id=PROJECT))
    deadline = time.monotonic() + 60
    while not _docker("ps", "-q", "--filter", f"label=agentsdk.run={handle.run_id}",
                      "--filter", "name=agentsdk-cmd-").split():
        assert time.monotonic() < deadline, "the command never started"
        await asyncio.sleep(0.2)
    handle.cancel()
    result = await handle.result()
    assert provider.failed == 2 and result.status is RunStatus.CANCELLED
    [ended] = events(result, EventType.WORKSPACE_COMMAND_ENDED)
    assert (ended["killed"], ended["kill_error"]) == ("cancelled", "WorkspaceUnavailable"), ended
    assert _containers_of("agentsdk.run", result.run_id) == []


@pytest.mark.docker
@pytest.mark.parametrize("store", STORES)
async def test_wherever_a_cancel_lands_on_docker_every_command_is_ended_and_recorded(docker_daemon, store):
    """M40 round 1: on Docker the window was the du container's whole life. The cancel is swept
    across a command's start, run, reading and record; the ledger and the containers are checked."""
    spec = WorkspaceSpec(image=IMAGE, run_command_approval=ApprovalPolicy.AUTO)
    turns = [[["run_command", {"argv": ["echo", "hi"]}]], [["run_command", {"argv": ["echo", "again"]}]]]
    for step in range(12):
        tenant = tenancy()
        provider = DockerProvider()
        runner = runner_for(provider, store)
        result = await _swept(runner, provider, spec, turns, 1.5 + step * 0.25, tenant)
        held, why = await asyncio.to_thread(_ledger_holds, runner, result, tenant, store)
        assert held, (step, why)
        assert _containers_of("agentsdk.run", result.run_id) == [], step


@pytest.mark.docker
async def test_a_folder_that_can_be_entered_but_not_read_is_refused_not_listed_empty(docker_daemon):
    """Pre-review F2: with search but no read permission, cd succeeded, the globs matched nothing,
    and the folder listed as ([], 0)."""
    provider = DockerProvider()
    handle = await provider.create(WorkspaceSpec(image=IMAGE), {"agentsdk.tenant": tenancy()})
    try:
        made = await provider.run(handle, ["sh", "-c", "mkdir xonly noread; touch xonly/f noread/f; "
                                                       "chmod 311 xonly; chmod 000 noread"], timeout=60)
        assert made.exit_code == 0, made
        for path in ("xonly", "noread"):
            with pytest.raises(PermissionError):
                await provider.list(handle, path)
    finally:
        await provider.remove(handle)


@pytest.mark.docker
async def test_example_34_runs_on_docker_with_the_scripted_model(docker_daemon):
    example = _example("34_sandboxed_tools.py")
    for store in STORES:
        example.TENANT = tenancy()
        checks = await example.offline(store, provider_name="docker")
        assert [label for label, passed in checks if not passed] == [] and len(checks) >= 10, (store, checks)


@pytest.mark.docker
async def test_an_images_declared_volume_goes_with_its_command(docker_daemon):
    """M39 caveat 2: an image that declares VOLUME left an anonymous volume per command. This
    image declares /data; it is used only if already on the host, and never pulled."""
    if subprocess.run(["docker", "image", "inspect", VOLUME_IMAGE], capture_output=True, timeout=60).returncode:
        pytest.skip("the image declaring a volume is not on this host")
    before = set(_docker("volume", "ls", "-q", "--filter", "dangling=true").split())
    provider = DockerProvider()
    handle = await provider.create(WorkspaceSpec(image=VOLUME_IMAGE), {"agentsdk.tenant": tenancy()})
    try:
        outcome = await provider.run(handle, ["ls", "-d", "/data"], timeout=60)
        assert outcome.exit_code == 0, outcome
    finally:
        await provider.remove(handle)
    left = set(_docker("volume", "ls", "-q", "--filter", "dangling=true").split()) - before
    if left:
        _docker("volume", "rm", *left)  # made by this test's own containers
    assert left == set(), f"volumes left behind: {sorted(left)}"
