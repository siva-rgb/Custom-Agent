"""M42 gate: hostile code in a plan's sandboxed children (FR-154, AC-100, NFR-40, NFR-42 to NFR-45).

Example 36 is the exit signal. The gates hold its plan on `FakeProvider`, on both stores, with
scripted children and with children that quote earlier results into later calls
(DECISION-a00773b0); a child that does nothing fails every check, never passes one
(DECISION-a7d6a1f5). Each check fails without its subject: here for the transfer filter, the disk
bound and reaping, and on a real daemon for all six AC-100 names. A command ended at a bound names
it (DECISION-21e7f6fb, migration 0021). M41's carried items come with a test each that fails
without them (DECISION-b7423cae).

Tests marked `docker` run live before review with `pytest -m docker`: the plan ten times on each
store with shuffled starts and random delays, and the six removals. The persisted half removes
every row it writes, by its own tenancies.
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import tomllib
import uuid
from pathlib import Path

import psycopg
import pytest

from agentsdk import AgentSpec, Persistence, RunConfig, RunStatus, Runner
from agentsdk.config import normalise_database_url
from agentsdk.events import EventType
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.postgres import RunScope
from agentsdk.primitives import Message, Role, ToolCall
from agentsdk.tools import ApprovalPolicy
from agentsdk.workspace import DockerProvider, FakeProvider, WorkspaceSpec
from agentsdk.workspace import docker as dk
from agentsdk.workspace import manager as mgr
from agentsdk.workspace import tools as wt
from agentsdk.workspace.records import CommandRecord, now

REPO = Path(__file__).resolve().parent.parent
DSN = normalise_database_url(os.environ.get("DATABASE_URL"))
PREFIX = "SYN-m42-"
PROJECT = "p-m42"
STORES = ["memory", "postgres"]
FAKE_IMAGE = "fake/workspace@sha256:" + "0" * 64
TENANTS: list[str] = []


def _example(name: str):
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py").replace("-", "_"), REPO / "scripts" / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EX36 = _example("36_sandbox_contention.py")


def _docker_leftovers(tenant: str) -> None:
    """Removes what a mutant left on the daemon for this test's tenancy: never anything else."""
    for kind, listing in (("container", ["ps", "-aq"]), ("volume", ["volume", "ls", "-q"])):
        found = subprocess.run(["docker", *listing, "--filter", f"label=agentsdk.tenant={tenant}"],
                               capture_output=True, text=True, timeout=60).stdout.split()
        if found:
            subprocess.run(["docker", kind, "rm", "-f", *found], capture_output=True, timeout=120)


@pytest.fixture(autouse=True)
def remove_what_the_test_wrote():
    TENANTS.clear()
    yield
    if any(t for t in TENANTS) and subprocess.run(["docker", "version"], capture_output=True).returncode == 0:
        for tenant in TENANTS:
            _docker_leftovers(tenant)
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
    tenant = f"{PREFIX}{uuid.uuid4().hex[:10]}"
    TENANTS.append(tenant)
    return tenant


async def example_36(store: str, provider_name: str = "fake", **options):
    """Example 36's checks as {label: (passed, why)}."""
    checks, _ = await EX36.offline(store, provider_name, tenant=tenancy(), quiet=True, **options)
    return {label: (passed, why) for label, passed, why in checks}


def failed(checks: dict) -> dict:
    return {label: why for label, (passed, why) in checks.items() if passed is not True}


NETWORK = "no connection or name resolution succeeded"
LINKS = "no link crossed: every export of one failed, and nothing left the workspace"
PIDS = "a fork bomb stopped at pids, and ended at its timeout"
DISK = "past max_workspace_bytes the next command was refused, named by the bound"
CANCEL = "a cancelled command that ignores SIGTERM and left its group was gone when the plan reported cancelled"
REAP = "a Runner killed mid-command left nothing once the next Runner started"


# =================================================================================================
# A command ended at a bound names it (DECISION-21e7f6fb, migration 0021)
# =================================================================================================


def respond(content="", calls=()):
    return ModelResponse(message=Message(role=Role.ASSISTANT, content=content, tool_calls=tuple(calls)),
                         stop_reason=StopReason.TOOL_CALLS if calls else StopReason.END_TURN, usage=Usage(5, 1, 6))


class Calls:
    """Makes the run_command calls its task lists as JSON, one a turn."""

    async def send(self, request):
        argvs = json.loads(request.messages[0].content)
        turn = sum(1 for m in request.messages if m.role is Role.ASSISTANT)
        if turn < len(argvs):
            return respond(calls=[ToolCall(id=f"t{turn}", name="run_command", arguments={"argv": argvs[turn]})])
        return respond("done")


async def _eat(cmd):
    cmd.limit("memory_bytes")
    return 0


async def _nap(cmd):
    await cmd.sleep(30)
    return 0


async def _bounded_run(store: str, argvs: list) -> tuple:
    tenant = tenancy()
    persistence = Persistence.postgres(DSN, create_schema=False) if store == "postgres" else None
    runner = Runner({"m": Calls()}, workspace_provider=FakeProvider(commands={"eat": _eat, "nap": _nap}),
                    persistence=persistence)
    agent = AgentSpec(id="bounded", instructions="x", preferred_model="m:x", tool_profile=("run_command",),
                      workspace=WorkspaceSpec(image=FAKE_IMAGE, run_command_approval=ApprovalPolicy.AUTO,
                                              max_command_seconds=0.2))
    result = await runner.run(agent, json.dumps(argvs), RunConfig(tenant_id=tenant, project_id=PROJECT, max_turns=6))
    rows = await asyncio.to_thread(runner._workspace_store().commands, tenant, PROJECT)
    sessions = runner._sessions_for(RunScope(run_id=result.run_id, tenant_id=tenant, project_id=PROJECT))
    results = [r for m in await asyncio.to_thread(sessions.history, result.run_id) for r in m.tool_results]
    await runner.aclose()
    return result, rows, tenant, results


@pytest.mark.parametrize("store", STORES)
async def test_a_command_ended_at_a_bound_names_it_in_its_outcome_event_and_row(store):
    result, rows, _, results = await _bounded_run(store, [["eat"], ["nap"]])
    assert result.status is RunStatus.COMPLETED, result.error
    ended = [e.payload for e in result.events if e.event_type is EventType.WORKSPACE_COMMAND_ENDED]
    assert [(p["killed"], p["limit"]) for p in ended] == [("limit", "memory_bytes"), ("timeout", None)]
    assert sorted((r.killed, r.limit) for r in rows) == [("limit", "memory_bytes"), ("timeout", None)]
    assert "killed: limit (memory_bytes)" in results[0].content
    assert "killed: timeout" in results[1].content and "(" not in results[1].content.split("\n")[1]


async def test_the_store_refuses_a_bound_on_a_command_not_killed_at_a_limit():
    _, rows, tenant, _ = await _bounded_run("postgres", [["eat"]])
    store = Persistence.postgres(DSN, create_schema=False).workspace_store()
    [row] = rows
    bad = CommandRecord(command_id=str(uuid.uuid4()), workspace_id=row.workspace_id, tenant_id=tenant,
                        project_id=PROJECT, run_id=row.run_id, tool_call_id=None, argv=["x"], exit_code=0,
                        killed=None, started_at=now(), ended_at=now(), stdout_bytes=0, stderr_bytes=0,
                        stdout_kept=0, stderr_kept=0, limit="memory_bytes")
    with pytest.raises(psycopg.errors.CheckViolation):
        await asyncio.to_thread(store.record_command, bad)


# =================================================================================================
# The fake makes links, and refuses them as Docker does
# =================================================================================================


async def test_a_fake_link_is_listed_as_one_and_its_export_refused_by_the_transfer_filter():
    prov = FakeProvider()
    handle = await prov.create(WorkspaceSpec(image=FAKE_IMAGE), {})
    space = prov._space(handle)
    space.files.link("abs", "/etc/passwd")
    space.files.link("dir/rel", "../../etc/passwd")
    assert space.files.listdir() == [("abs", "link", 0), ("dir", "dir", 0)]
    for path in ("abs", "dir/rel", "dir"):
        with pytest.raises(ValueError, match="symlink"):
            await prov.export(handle, path)
    with pytest.raises(PermissionError):
        space.files.write("abs", "through it")  # a command's write has no tree outside to reach


async def test_an_import_onto_or_under_a_fake_link_does_as_docker_does():
    """M42 pre-review F5, compared live: an import onto a link replaces it with the file; one under
    a link used as a parent is refused with ValueError, before anything is written."""
    prov = FakeProvider()
    handle = await prov.create(WorkspaceSpec(image=FAKE_IMAGE), {})
    space = prov._space(handle)
    space.files.link("abs", "/etc/passwd")
    space.files.link("dir", "/etc")
    await prov.import_(handle, wt.one_file_tar("abs", b"new"))
    assert space.files.read("abs") == b"new" and ("abs", "file", 3) in space.files.listdir()
    with pytest.raises(ValueError, match="link"):
        await prov.import_(handle, wt.one_file_tar("dir/passwd", b"x"))
    assert not space.files.exists("dir/passwd") and ("dir", "link", 0) in space.files.listdir()


# =================================================================================================
# FR-154: example 36 on the fake, both stores
# =================================================================================================


@pytest.mark.parametrize("store", STORES)
async def test_example_36_holds_every_check_offline(store):
    checks = await example_36(store, seed=42)
    assert len(checks) == 14 and not failed(checks), failed(checks)


@pytest.mark.parametrize("store", STORES)
async def test_example_36_holds_under_children_that_quote_earlier_results(store, monkeypatch):
    """DECISION-a00773b0: each child also writes the results it read, verbatim, into later calls
    -- the env value included -- and every check still holds."""
    quoted: list[str] = []
    real = EX36.ScriptedChild.send

    async def send(self, request):
        response = await real(self, request)
        quoted.extend(c.arguments["content"] for c in response.message.tool_calls if c.id.startswith("q"))
        return response

    monkeypatch.setattr(EX36.ScriptedChild, "send", send)
    checks = await example_36(store, seed=7, quote=True)
    assert len(checks) == 14 and not failed(checks), failed(checks)
    # Its subject: results were quoted, the env command's own output -- the value -- among them.
    assert len(quoted) >= 8 and any("PROBE_VALUE=" in text for text in quoted), len(quoted)


async def test_example_36_passes_nothing_when_no_child_does_anything(monkeypatch):
    """A subject that never happened fails its check (DECISION-a7d6a1f5): with children that make
    no call, no check passes."""
    async def declines(self, request):
        return EX36._reply(content="I will not run that.")

    monkeypatch.setattr(EX36.ScriptedChild, "send", declines)
    checks = await example_36("memory")
    assert [label for label, (passed, _) in checks.items() if passed is True] == []
    assert sum(1 for passed, _ in checks.values() if passed is None) >= 11


MEMORY = "an allocation without end was killed at memory_bytes, recorded by name"
SECRETS = "no host value reached a workspace or a record, and the env value only what printed or quoted it"


def _violation(monkeypatch, name: str) -> None:
    """M42 pre-review's violations, each one the SDK breaking a property a check names."""
    if name == "a refusal that names no bound":
        real = mgr.RunWorkspace._refuse_if_full

        async def unrelated(self, handle, tool):
            if self._full is not None:
                raise mgr.WorkspaceUnavailable("unrelated failure")
            return await real(self, handle, tool)

        monkeypatch.setattr(mgr.RunWorkspace, "_refuse_if_full", unrelated)
        return
    real_finished = mgr.RunWorkspace._finished

    async def finished(self, handle, command_id, argv, tool_call_id, started_at, started, outcome, *,
                       kill_error=None):
        if name == "an event naming another bound than its row":
            emit = self._emit

            async def other_bound(kind, payload):
                if kind is EventType.WORKSPACE_COMMAND_ENDED and payload.get("limit"):
                    payload = {**payload, "limit": "pids"}
                return await emit(kind, payload)

            self._emit = other_bound
        elif name == "the env value in an event of the SDK's own":
            kill_error = kill_error or handle.spec.env.get("PROBE_VALUE")
        return await real_finished(self, handle, command_id, argv, tool_call_id, started_at, started, outcome,
                                   kill_error=kill_error)

    monkeypatch.setattr(mgr.RunWorkspace, "_finished", finished)


@pytest.mark.parametrize("violation, label", [("a refusal that names no bound", DISK),
                                              ("an event naming another bound than its row", MEMORY),
                                              ("the env value in an event of the SDK's own", SECRETS)])
async def test_in_memory_each_check_reads_the_childrens_events(monkeypatch, violation, label):
    """Pre-review F3: in memory a Runner keeps no child's events, so these passed there; example 36's
    RecordingRunner keeps them, and each violation fails the check that names it."""
    _violation(monkeypatch, violation)
    checks = await example_36("memory", seed=3)
    assert checks[label][0] is False, checks[label]


async def test_a_connection_in_a_run_of_the_network_probe_that_was_killed_is_seen(monkeypatch):
    """Pre-review F2: the probe run twice, the first reaching out and then killed at its timeout,
    the second clean; the check reads every run of the probe."""
    real_python, real_send = EX36.python, EX36.ScriptedChild.send

    async def python(cmd):
        if cmd.argv == EX36.ARGV["net"] and not cmd.files.exists("ran-once"):
            cmd.files.write("ran-once", "1")
            cmd.out("try host.docker.internal 5432\nCONNECTED host.docker.internal 5432\n")
            await cmd.sleep(600)
        return await real_python(cmd)

    async def send(self, request):
        response = await real_send(self, request)
        twice = [c for c in response.message.tool_calls if c.arguments.get("argv") == EX36.ARGV["net"]
                 and c.id == "t0"]
        if twice:
            extra = ToolCall(id="t0b", name="run_command", arguments={**twice[0].arguments, "timeout_seconds": 0.5})
            return EX36._reply([extra, *response.message.tool_calls])
        return response

    monkeypatch.setattr(EX36, "python", python)
    monkeypatch.setattr(EX36.ScriptedChild, "send", send)
    checks = await example_36("memory", seed=3)
    assert checks[NETWORK][0] is False and "CONNECTED" in checks[NETWORK][1], checks[NETWORK]


def test_a_probe_is_recognised_by_its_program_whatever_its_spacing_and_nothing_else():
    """A live model copies a probe with spaces dropped (M42 live run); the witness that the probe
    ran is the same program, never a lookalike."""
    for name, argv in EX36.ARGV.items():
        assert EX36.runs_probe(argv, name)
        assert EX36.runs_probe([*argv[:2], argv[2].replace(", ", ",")], name)
    links = EX36.ARGV["links"]
    assert not EX36.runs_probe([*links[:2], links[2].replace('"abs"', '"elsewhere"')], "links")
    assert not EX36.runs_probe([*links[:2], links[2] + "print(1)\n"], "links")
    assert not EX36.runs_probe(["python3", *links[1:]], "links")
    assert not EX36.runs_probe([*links[:2], "("], "links")
    assert not EX36.runs_probe(links, "net")
    # Pre-review F1: a nesting deep enough for MemoryError fits the argv schema; it is not the
    # probe, and never stops the operator.
    assert not EX36.runs_probe([*links[:2], "-" * 60000 + "1"], "links")


def test_example_36_runs_offline_as_a_program():
    done = subprocess.run([sys.executable, str(REPO / "scripts" / "36_sandbox_contention.py"), "--offline"],
                          capture_output=True, text=True, timeout=600, cwd=REPO)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-2000:]
    assert done.stdout.count("[PASS]") == 14 and "FAIL" not in done.stdout


# =================================================================================================
# AC-100: each check fails without its subject
# =================================================================================================


def _passes_links(data: bytes, path: str) -> bytes:
    """A transfer filter taken away: a link's export is read as its target's name."""
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        member = [m for m in tar.getmembers() if m.name.removeprefix("./") not in ("", ".")][0]
        return member.linkname.encode() if member.issym() else tar.extractfile(member).read()


def without(monkeypatch, subject: str) -> None:
    """Takes `subject` away, as AC-100 names it."""
    import agentsdk.workspace as ws

    if subject == "network":
        real = dk.hardening

        def open_network(spec):
            args = real(spec)
            at = args.index("--network")
            return args[:at] + args[at + 2:]

        monkeypatch.setattr(dk, "hardening", open_network)
    elif subject == "transfer filter":
        monkeypatch.setattr(dk, "read_tar", lambda data: [])
        monkeypatch.setattr(ws, "read_tar", lambda data: [])
        monkeypatch.setattr(wt, "one_file", _passes_links)
    elif subject == "pid limit":
        real = dk.hardening

        def unlimited(spec):
            args = real(spec)
            at = args.index("--pids-limit")
            return args[:at] + args[at + 2:]

        monkeypatch.setattr(dk, "hardening", unlimited)
    elif subject == "disk bound":
        async def never_full(self, handle, tool):
            return None

        monkeypatch.setattr(mgr.RunWorkspace, "_refuse_if_full", never_full)
    elif subject == "kill on cancel":
        # Everything that ends a command's container when its run is cancelled: the provider's
        # kill, the command's own removal, and the sweep of the run's workspace by container id.
        spared: set[str] = set()
        real_rm = DockerProvider._rm

        async def no_kill(self, handle, command_id):
            space = self._spaces.get(handle.workspace_id)
            command = None if space is None else space.running.get(command_id)
            if command is not None:
                spared.add(command.name)
                code, out, _ = await self._cli("ps", "-aq", "--filter", f"name=^/{command.name}$")
                spared.update(out.decode().split())

        async def rm(self, *names):
            return await real_rm(self, *[name for name in names if name not in spared])

        monkeypatch.setattr(DockerProvider, "kill", no_kill)
        monkeypatch.setattr(DockerProvider, "_rm", rm)
    elif subject == "reaping":
        async def no_reaping(self):
            return None

        monkeypatch.setattr(mgr.WorkspaceManager, "reap", no_reaping)
    else:
        raise AssertionError(subject)


NAMED = {"network": NETWORK, "transfer filter": LINKS, "pid limit": PIDS, "disk bound": DISK,
         "kill on cancel": CANCEL, "reaping": REAP}


@pytest.mark.parametrize("subject", ["transfer filter", "disk bound", "reaping"])
async def test_on_the_fake_each_check_fails_without_its_subject(monkeypatch, subject):
    without(monkeypatch, subject)
    checks = await example_36("memory", seed=3)
    assert checks[NAMED[subject]][0] is False, checks[NAMED[subject]]


# =================================================================================================
# M41's carried items (DECISION-b7423cae)
# =================================================================================================


async def test_example_35s_aclose_check_fails_when_aclose_removes_nothing(monkeypatch):
    async def removes_nothing(self):
        return None

    monkeypatch.setattr(mgr.WorkspaceManager, "aclose", removes_nothing)
    checks = await _example("35_workspace_plan.py").offline(None, "fake", tenant=tenancy(), project=PROJECT)
    [(label, passed)] = [(label, passed) for label, passed in checks if "aclose()" in label]
    assert passed is False, label


async def test_a_wait_for_a_workspace_a_sibling_already_began_is_let_go_at_once():
    """Between a caller's check and the slots' lock, a sibling on another loop may begin the
    workspace and release its waiters: a wait queued then would never be released."""
    slots = mgr._Slots(1)
    assert await slots.take(None)
    space = object()
    assert await asyncio.wait_for(slots.take(None, space, lambda: True), 2) is False
    assert slots.held == 1 and not slots._waiters


def test_the_refusal_of_a_workspace_takes_no_argument_it_ignores():
    import inspect

    assert list(inspect.signature(Runner._workspace_refusal).parameters) == ["self"]


# =================================================================================================
# A workspace's setup has a bound of its own (DECISION-e799700b)
# =================================================================================================


def test_every_workspace_tools_deadline_is_past_the_setup_bound_and_the_commands_own_timeout():
    """Example 36's ten-in-a-row run: a deadline of max_command_seconds + 30 also covered the
    setup, so under load a started command was cut off by the executor, recorded cancelled. The
    size read before a command is inside it too (pre-review F8, DECISION-4689af02)."""
    spec = WorkspaceSpec(image=FAKE_IMAGE, max_command_seconds=7)
    assert {name: s.timeout_seconds for name, s in wt.specs(spec).items()} == {
        name: mgr.SETUP_SECONDS + 2 * mgr.MEASURE_SECONDS + 7 + 30.0 for name in wt.NAMES}  # FR-155 (M43)
    assert mgr.MEASURE_SECONDS == mgr._MEASURE_SECONDS == 120.0
    assert mgr.SETUP_SECONDS == wt.SETUP_SECONDS == 120.0


class SlowCreate(FakeProvider):
    def __init__(self, seconds: float, **options) -> None:
        super().__init__(**options)
        self.seconds = seconds

    async def create(self, spec, labels):
        await asyncio.sleep(self.seconds)
        return await super().create(spec, labels)


def _bounded(monkeypatch, seconds: float) -> None:
    monkeypatch.setattr(mgr, "SETUP_SECONDS", seconds)
    monkeypatch.setattr(wt, "SETUP_SECONDS", seconds)


async def _one_command(runner, tenant: str, spec: WorkspaceSpec, argv=("nap",)):
    agent = AgentSpec(id="bounded", instructions="x", preferred_model="m:x", tool_profile=("run_command",),
                      workspace=spec)
    return await runner.run(agent, json.dumps([list(argv)]), RunConfig(tenant_id=tenant, project_id=PROJECT,
                                                                        max_turns=3))


def _refused(result) -> list:
    return [(e.payload.get("error_type"), e.payload["is_error"]) for e in result.events
            if e.event_type is EventType.TOOL_CALLED]


async def test_a_creation_past_the_setup_bound_fails_the_call_and_its_workspace_still_goes(monkeypatch):
    _bounded(monkeypatch, 0.3)
    prov = SlowCreate(1.0, commands={"nap": _nap})
    runner = Runner({"m": Calls()}, workspace_provider=prov)
    tenant = tenancy()
    result = await _one_command(runner, tenant, WorkspaceSpec(image=FAKE_IMAGE,
                                                              run_command_approval=ApprovalPolicy.AUTO))
    assert _refused(result) == [("ToolExecutionError", True)], _refused(result)
    assert not [e for e in result.events if e.event_type is EventType.WORKSPACE_COMMAND_STARTED]
    await asyncio.sleep(1.2)  # the creation, left to finish, belongs to the space
    await runner.aclose()
    assert await prov.list_owned({"agentsdk.tenant": tenant}) == []


async def test_a_slot_wait_past_the_setup_bound_fails_the_call(monkeypatch):
    _bounded(monkeypatch, 0.3)
    prov = FakeProvider(commands={"nap": _nap})
    runner = Runner({"m": Calls()}, workspace_provider=prov, max_concurrent_workspaces=1)
    tenant = tenancy()
    kept = WorkspaceSpec(image=FAKE_IMAGE, run_command_approval=ApprovalPolicy.AUTO, keep=True,
                         max_command_seconds=0.1)
    held = await _one_command(runner, tenant, kept)  # its workspace outlives it, holding the one slot
    assert held.status is RunStatus.COMPLETED
    waited = await _one_command(runner, tenant, WorkspaceSpec(image=FAKE_IMAGE,
                                                              run_command_approval=ApprovalPolicy.AUTO))
    assert _refused(waited) == [("ToolExecutionError", True)], _refused(waited)
    assert [e for e in waited.events if e.event_type is EventType.WORKSPACE_WAITING]
    await runner.aclose()


async def test_a_reaping_past_the_setup_bound_fails_the_call(monkeypatch):
    _bounded(monkeypatch, 0.3)

    async def slow_reaping(self):
        await asyncio.sleep(2)
        self._reaped = True

    monkeypatch.setattr(mgr.WorkspaceManager, "_reap", slow_reaping)
    runner = Runner({"m": Calls()}, workspace_provider=FakeProvider(commands={"nap": _nap}))
    result = await _one_command(runner, tenancy(), WorkspaceSpec(image=FAKE_IMAGE,
                                                                  run_command_approval=ApprovalPolicy.AUTO))
    assert _refused(result) == [("ToolExecutionError", True)], _refused(result)
    await runner.aclose()


async def test_a_creation_inside_the_setup_bound_runs_its_command(monkeypatch):
    _bounded(monkeypatch, 2.0)
    runner = Runner({"m": Calls()}, workspace_provider=SlowCreate(0.5, commands={"eat": _eat}))
    result = await _one_command(runner, tenancy(), WorkspaceSpec(image=FAKE_IMAGE,
                                                                  run_command_approval=ApprovalPolicy.AUTO),
                                argv=("eat",))
    assert _refused(result) == [(None, False)], _refused(result)
    await runner.aclose()


# =================================================================================================
# NFR-40: the package's runtime dependencies are unchanged
# =================================================================================================


def test_the_runtime_dependencies_are_unchanged():
    declared = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    assert declared == ["httpx==0.28.1", "jsonschema==4.26.0", "psycopg[binary]==3.3.5", "psycopg_pool==3.2.6",
                        "python-dotenv==1.2.3"]


# =================================================================================================
# On a real daemon (pytest -m docker)
# =================================================================================================


@pytest.mark.docker
@pytest.mark.parametrize("store", STORES)
async def test_on_docker_example_36_holds_every_check_ten_times(store):
    """AC-100: ten times on each store, the children's start order shuffled and their calls
    delayed at random."""
    failures = {}
    for seed in range(10):
        checks = await example_36(store, "docker", seed=seed)
        if len(checks) != 14 or failed(checks):
            failures[seed] = failed(checks)
    assert not failures, failures


@pytest.mark.docker
@pytest.mark.parametrize("subject", list(NAMED))
async def test_on_docker_each_check_fails_without_its_subject(monkeypatch, subject):
    without(monkeypatch, subject)
    checks = await example_36("memory", "docker", seed=5)
    assert checks[NAMED[subject]][0] is False, checks[NAMED[subject]]


@pytest.mark.docker
async def test_on_docker_a_command_out_of_memory_is_killed_at_memory_bytes_by_name():
    docker = DockerProvider()
    handle = await docker.create(WorkspaceSpec(image=EX36.IMAGE, memory_bytes=EX36.MEMORY_BYTES), {
        "agentsdk.workspace": str(uuid.uuid4()), "agentsdk.tenant": tenancy()})
    # A child killed for memory under a parent that exits on its own: the command was not ended
    # at its bound (M42 pre-review F6, DECISION-4689af02).
    child_oom = ("import os\npid = os.fork()\nif pid == 0:\n    held = [b'x' * 8388608 for _ in range(64)]\n"
                 "    os._exit(0)\nos.waitpid(pid, 0)\nprint('parent finished normally')\n")
    try:
        eaten = await docker.run(handle, EX36.ARGV["memory"], timeout=60)
        quiet = await docker.run(handle, ["python", "-c", "print(1)"], timeout=60)
        survived = await docker.run(handle, ["python", "-c", child_oom], timeout=60)
    finally:
        await docker.remove(handle)
    assert (eaten.killed, eaten.limit, eaten.exit_code) == ("limit", "memory_bytes", None), eaten
    assert (quiet.killed, quiet.limit, quiet.exit_code) == (None, None, 0)
    assert (survived.killed, survived.limit, survived.exit_code) == (None, None, 0), survived
    assert b"parent finished normally" in survived.stdout


@pytest.mark.docker
def test_example_36_runs_scripted_on_docker():
    done = subprocess.run([sys.executable, str(REPO / "scripts" / "36_sandbox_contention.py"), "--scripted"],
                          capture_output=True, text=True, timeout=900, cwd=REPO)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-2000:]
    assert done.stdout.count("[PASS]") == 14
