"""M45 gate: stdio MCP servers in the sandbox (FR-164 to FR-167).

Written before the implementation, against FR-164 to FR-167 as the owner settled them in the
pre-flight (DECISION-b478705b, corrected by DECISION-2a12196e and DECISION-61aed587):
  * `StdioServer(..., workspace=WorkspaceSpec(...), files={path: bytes})`, stdio only; with a
    workspace its own `env` or `cwd` raises ValueError naming `WorkspaceSpec.env`, as does a spec
    with `keep=True` or scope `plan`; `files` without a workspace raises too;
  * the server's workspace is created when its connection opens, its files imported through the
    checked transfer, recorded in `workspaces` scope `server` with no run (migration 0026), and
    removed when its Runner closes it (`aclose`), when reaped, or swept by its Runner's label;
  * a crash's one restart runs a new process over the same workspace, its files kept;
  * it waits for a workspace slot within its `connect_timeout`;
  * on FakeProvider the server is a local process whose working directory is a temporary folder
    holding its files; on Docker a hardened container (`docker run -i`) over its volume;
  * a provider gives the launch: `server_launch(handle, command, args) -> ServerLaunch(command,
    args, env, cwd)`.

The legacy-era M33 fixture is the server: standard library only, given through `files`.
Windows only, as the M33 tests (NFR-39 measured by process id). Tests marked `docker` run in the
`docker` gate. The persisted half removes every row it writes, by its own tenancies.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import json
import os
import re
import subprocess
import sys
import time
import uuid
import warnings
from pathlib import Path

import psycopg
import pytest

from agentsdk import AgentSpec, Persistence, RunConfig, Runner
from agentsdk.config import normalise_database_url
from agentsdk.mcp import StdioServer
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.postgres import RunScope
from agentsdk.primitives import Message, Role, ToolCall
from agentsdk.workspace import DockerProvider, FakeProvider, WorkspaceHandle, WorkspaceSpec
from agentsdk.workspace import docker as dk

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="NFR-39 is measured on Windows (D11)")

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests" / "fixtures" / "mcp_servers.py"
DSN = normalise_database_url(os.environ.get("DATABASE_URL"))
PREFIX, PROJECT = "SYN-m45-", "p-m45"
STORES = ["memory", "postgres"]
PYTHON = sys._base_executable  # the real interpreter, which the mcp library's Job Object holds
FAKE_IMAGE = "fake/workspace@sha256:" + "0" * 64
IMAGE = "python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df"
TENANTS: list[str] = []


def _every_listed_version_accepted(store, tenant_id, project_id, alias, versions, pending, accept):
    return set(pending.values()), {}


@pytest.fixture(autouse=True)
def every_tool_accepted(monkeypatch):
    """As M33's tests: the catalog is M34's, tested there; here every listed version is accepted."""
    import agentsdk.mcp as mcp

    monkeypatch.setattr(mcp, "_read_catalog", _every_listed_version_accepted)


def _docker_leftovers(tenant: str) -> None:
    """Removes what a test left on the daemon for its own tenancy: never anything else."""
    for kind, listing in (("container", ["ps", "-aq"]), ("volume", ["volume", "ls", "-q"]),
                          ("network", ["network", "ls", "-q"])):
        found = subprocess.run(["docker", *listing, "--filter", f"label=agentsdk.tenant={tenant}"],
                               capture_output=True, text=True, timeout=60).stdout.split()
        if found:
            subprocess.run(["docker", kind, "rm", "-f", *found] if kind != "network" else
                           ["docker", "network", "rm", *found], capture_output=True, timeout=120)


@pytest.fixture(autouse=True)
def remove_what_the_test_wrote():
    TENANTS.clear()
    yield
    if TENANTS and subprocess.run(["docker", "version"], capture_output=True).returncode == 0:
        for tenant in TENANTS:
            _docker_leftovers(tenant)
    if DSN and TENANTS:
        with psycopg.connect(DSN, autocommit=True) as conn:
            ids = [r[0] for r in conn.execute("SELECT run_id FROM runs WHERE tenant_id = ANY(%s)", (TENANTS,))]
            if conn.execute("SELECT to_regclass('workspace_connections')").fetchone()[0] is not None:
                conn.execute("DELETE FROM workspace_connections WHERE tenant_id = ANY(%s)", (TENANTS,))
            conn.execute("DELETE FROM workspace_commands WHERE tenant_id = ANY(%s)", (TENANTS,))
            conn.execute("DELETE FROM workspaces WHERE tenant_id = ANY(%s)", (TENANTS,))
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


def sandboxed(image=FAKE_IMAGE, command=PYTHON, pids=None, extra=(), alias="legacy", spec_fields=None,
              **fields) -> StdioServer:
    """The legacy M33 fixture, given as a file and run from its workspace's root."""
    fields.setdefault("approval", "never")
    fields.setdefault("connect_timeout", 120)
    args = ("mcp_servers.py", "legacy", *(["--pids", str(pids)] if pids else []), *extra)
    return StdioServer(alias=alias, command=command, args=args,
                       workspace=WorkspaceSpec(image=image, **(spec_fields or {})),
                       files={"mcp_servers.py": FIXTURE.read_bytes()}, **fields)


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


def agent(*profile):
    return AgentSpec(id="agent", instructions="use the tools", preferred_model="m:x", tool_profile=tuple(profile))


def runner_for(servers, provider, store="memory", **fields):
    return Runner({"m": Turns()}, mcp_servers=list(servers), workspace_provider=provider,
                  persistence=Persistence.postgres(DSN, create_schema=False) if store == "postgres" else None,
                  **fields)


async def run(runner, spec, turns, tenant, **fields):
    return await runner.run(spec, json.dumps(turns), RunConfig(tenant_id=tenant, project_id=PROJECT, **fields))


async def results(runner, result, tenant):
    sessions = runner._sessions_for(RunScope(run_id=result.run_id, tenant_id=tenant, project_id=PROJECT))
    history = await asyncio.to_thread(sessions.history, result.run_id)
    return {r.tool_call_id: r for m in history for r in m.tool_results}


async def rows(runner, tenant):
    return await asyncio.to_thread(runner._workspace_store().workspaces, tenant, PROJECT)


def alive(pid: int) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x00100000 | 0x1000, False, pid)
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == 0x102
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


# =================================================================================================
# FR-164: the configuration
# =================================================================================================


def test_a_sandboxed_server_records_its_spec_and_its_files_by_name():
    server = sandboxed()
    record = server.record()
    assert record["workspace"] == WorkspaceSpec(image=FAKE_IMAGE).record()
    assert record["files"] == ["mcp_servers.py"] and "legacy-fixture" not in json.dumps(record)
    assert "workspace" not in StdioServer(alias="plain", command=PYTHON).record()  # NFR: as before


@pytest.mark.parametrize("field", ["env", "cwd"])
def test_its_own_env_or_cwd_with_a_workspace_raises(field):
    value = {"A": "1"} if field == "env" else str(REPO)
    with pytest.raises(ValueError, match="WorkspaceSpec.env"):
        StdioServer(alias="s", command=PYTHON, workspace=WorkspaceSpec(image=FAKE_IMAGE), **{field: value})


@pytest.mark.parametrize("spec_fields", [{"keep": True}, {"scope": "plan"}])
def test_a_kept_or_plan_scoped_server_workspace_raises(spec_fields):
    with pytest.raises(ValueError, match="server"):
        StdioServer(alias="s", command=PYTHON, workspace=WorkspaceSpec(image=FAKE_IMAGE, **spec_fields))


def test_files_without_a_workspace_raise():
    with pytest.raises(ValueError, match="files"):
        StdioServer(alias="s", command=PYTHON, files={"a.py": b"x"})


@pytest.mark.parametrize("path", ["", "/abs.py", "../up.py", "a/../b.py", "a\\b.py", "a//b.py", ".", "C:x.py"])
def test_each_unclean_file_path_raises(path):
    with pytest.raises(ValueError, match="files"):
        StdioServer(alias="s", command=PYTHON, workspace=WorkspaceSpec(image=FAKE_IMAGE), files={path: b"x"})


def test_a_file_that_is_not_bytes_raises():
    with pytest.raises(ValueError, match="files"):
        StdioServer(alias="s", command=PYTHON, workspace=WorkspaceSpec(image=FAKE_IMAGE), files={"a.py": "text"})


def test_a_runner_with_a_sandboxed_server_and_no_provider_raises():
    with pytest.raises(ValueError, match="workspace_provider"):
        Runner({"m": Turns()}, mcp_servers=[sandboxed()])


# =================================================================================================
# FR-165: its lifetime, on FakeProvider
# =================================================================================================


def test_the_fake_launches_a_server_in_a_folder_holding_its_files_with_its_specs_env_alone():
    async def main():
        provider = FakeProvider()
        spec = WorkspaceSpec(image=FAKE_IMAGE, env={"ONLY": "this"})
        handle = await provider.create(spec, {"agentsdk.workspace": str(uuid.uuid4())})
        from agentsdk.workspace.tools import files_tar

        await provider.import_(handle, files_tar({"pkg/a.py": b"print(1)", "b.txt": b"b"}))
        launch = await provider.server_launch(handle, "python3", ["pkg/a.py"])
        folder = Path(launch.cwd)
        try:
            assert (launch.command, list(launch.args), dict(launch.env)) == ("python3", ["pkg/a.py"], {"ONLY": "this"})
            assert (folder / "pkg" / "a.py").read_bytes() == b"print(1)" and (folder / "b.txt").read_bytes() == b"b"
        finally:
            await provider.remove(handle)
        assert not folder.exists()  # removed with its workspace

    asyncio.run(main())


@pytest.mark.parametrize("store", STORES)
async def test_on_the_fake_a_sandboxed_server_serves_and_is_recorded_scope_server_with_no_run(store, tmp_path):
    tenant = tenancy()
    pids = tmp_path / "pids"
    provider = FakeProvider()
    runner = runner_for([sandboxed(pids=pids)], provider, store)
    try:
        result = await run(runner, agent("legacy__echo"), [[["legacy__echo", {"text": "hi"}]]], tenant)
        [content] = [r.content for r in (await results(runner, result, tenant)).values()]
        assert "hi" in content, content
        [row] = await rows(runner, tenant)
        assert (row.scope, row.run_id, row.provider, row.removed_reason) == ("server", None, "fake", None)
        [space] = provider._spaces.values()
        folder = space.folder
        assert (folder / "mcp_servers.py").read_bytes() == FIXTURE.read_bytes()
    finally:
        await runner.aclose()
    [row] = await rows(runner, tenant)
    assert row.removed_reason == "aclose" and not folder.exists()
    assert pids_in(pids) and gone(pids_in(pids))


async def test_on_the_fake_a_crash_restarts_once_over_the_same_workspace(tmp_path):
    tenant = tenancy()
    pids = tmp_path / "pids"
    provider = FakeProvider()
    folders, real = [], FakeProvider.server_launch

    async def noted(self, handle, command, args):
        launch = await real(self, handle, command, args)
        folders.append(self._spaces[handle.workspace_id].folder)
        return launch

    provider.server_launch = noted.__get__(provider)
    runner = runner_for([sandboxed(pids=pids)], provider)
    try:
        await run(runner, agent("legacy__die", "legacy__echo"),
                  [[["legacy__die", {}]], [["legacy__echo", {"text": "again"}]]], tenant)
        assert len(pids_in(pids)) == 2  # restarted once
        [row] = await rows(runner, tenant)  # over the same workspace, its folder kept
        assert row.removed_reason is None and len(provider._spaces) == 1
        [space] = provider._spaces.values()
        assert folders == [space.folder] * 2, folders
    finally:
        await runner.aclose()
    assert gone(pids_in(pids))


async def test_a_sandboxed_server_waits_for_a_slot_within_its_connect_timeout(tmp_path):
    tenant = tenancy()
    provider = FakeProvider()
    runner = runner_for([sandboxed(connect_timeout=2)], provider, max_concurrent_workspaces=1)
    assert await runner._workspaces.slots.take(None)  # every slot held
    try:
        started = time.monotonic()
        result = await run(runner, agent("legacy__echo"), [[["legacy__echo", {"text": "hi"}]]], tenant)
        assert time.monotonic() - started < 60
        assert await rows(runner, tenant) == [] and not provider._spaces
        assert all("hi" not in r.content for r in (await results(runner, result, tenant)).values())
    finally:
        runner._workspaces.slots.give()
        await runner.aclose()


# =================================================================================================
# Migration 0026
# =================================================================================================


def test_migration_0026_admits_scope_server_with_no_run_and_only_then():
    from agentsdk import migrate

    versions = [version for version, _ in migrate.discover()]
    assert "0026" in versions  # not the latest: later milestones add their own
    if not DSN:
        pytest.skip("no database")
    tenant = tenancy()
    with psycopg.connect(DSN, autocommit=True) as conn:
        def insert(scope, run_id):
            conn.execute("INSERT INTO workspaces (workspace_id, tenant_id, project_id, run_id, scope, provider,"
                         " image, limits, created_at) VALUES (%s, %s, %s, %s, %s, 'fake', %s, '{}', now())",
                         (str(uuid.uuid4()), tenant, PROJECT, run_id, scope, FAKE_IMAGE))

        insert("server", None)
        server_workspace = conn.execute("SELECT workspace_id FROM workspaces WHERE tenant_id = %s",
                                        (tenant,)).fetchone()[0]
        # Pre-review F3: a server workspace's connection has no run, and only its.
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("INSERT INTO workspace_connections (connection_id, workspace_id, tenant_id, project_id,"
                         " run_id, allowed, reason, at) VALUES (%s, %s, %s, %s, %s, false, 'not_listed', now())",
                         (str(uuid.uuid4()), server_workspace, tenant, PROJECT, str(uuid.uuid4())))
        for scope in ("run", "plan"):
            with pytest.raises(psycopg.errors.CheckViolation):
                insert(scope, None)
        with pytest.raises(psycopg.errors.CheckViolation):
            insert("other", None)


# =================================================================================================
# FR-165: on Docker
# =================================================================================================


def test_the_docker_launch_is_an_attached_hardened_container_over_the_workspace():
    handle = WorkspaceHandle(workspace_id=str(uuid.uuid4()), spec=WorkspaceSpec(image=IMAGE, env={"KEY": "never-on-the-line"}),
                             labels={"agentsdk.workspace": "w", "agentsdk.tenant": "t"})
    args = dk.server_args(handle, "agentsdk-srv-x", "python3", ["mcp_servers.py", "legacy"], ["KEY"])
    assert args[:4] == ["run", "-i", "--pull", "never"] and "--rm" not in args
    for flag in ("--cap-drop", "--read-only", "--user", "--pids-limit", "--memory"):
        assert flag in args, flag
    assert args[args.index("-w") + 1] == "/workspace" and "--label" in args
    assert args[args.index(IMAGE):] == [IMAGE, "python3", "mcp_servers.py", "legacy"]
    assert not any("never-on-the-line" in a for a in args)  # values by the CLI's environment only
    # The image's entrypoint cleared, as a command's is (pre-review F1); no wrapper after it.
    assert args[args.index("--entrypoint=") + 1] == IMAGE and "sh" not in args


@pytest.mark.docker
@pytest.mark.parametrize("store", STORES)
async def test_on_docker_a_sandboxed_server_serves_and_nothing_of_it_remains_after_aclose(store):
    tenant = tenancy()
    runner = runner_for([sandboxed(image=IMAGE, command="python3")], DockerProvider(), store)
    try:
        result = await run(runner, agent("legacy__echo"), [[["legacy__echo", {"text": "hi"}]]], tenant)
        [content] = [r.content for r in (await results(runner, result, tenant)).values()]
        assert "hi" in content, content
        [row] = await rows(runner, tenant)
        assert (row.scope, row.run_id, row.provider) == ("server", None, "docker")
    finally:
        await runner.aclose()
    [row] = await rows(runner, tenant)
    assert row.removed_reason == "aclose"
    assert await DockerProvider().list_owned({"agentsdk.tenant": tenant}) == []
    left = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=agentsdk.tenant={tenant}"],
                          capture_output=True, text=True, timeout=60).stdout.split()
    assert left == []


@pytest.mark.docker
async def test_on_docker_a_server_that_ignores_its_stdins_end_is_removed_with_its_container():
    tenant = tenancy()
    runner = runner_for([sandboxed(image=IMAGE, command="python3", extra=("--deaf",))], DockerProvider())
    try:
        await run(runner, agent("legacy__echo"), [[["legacy__echo", {"text": "hi"}]]], tenant)
    finally:
        await runner.aclose()
    left = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=agentsdk.tenant={tenant}"],
                          capture_output=True, text=True, timeout=60).stdout.split()
    assert left == []


@pytest.mark.docker
async def test_on_docker_a_crash_restarts_once_over_the_same_workspace():
    tenant = tenancy()
    runner = runner_for([sandboxed(image=IMAGE, command="python3")], DockerProvider())
    try:
        result = await run(runner, agent("legacy__die", "legacy__echo"),
                           [[["legacy__die", {}]], [["legacy__echo", {"text": "again"}]]], tenant)
        assert any("again" in r.content for r in (await results(runner, result, tenant)).values())
        [row] = await rows(runner, tenant)
        assert row.removed_reason is None
        servers = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=agentsdk.tenant={tenant}",
                                  "--filter", "label=agentsdk.role=server"],
                                 capture_output=True, text=True, timeout=60).stdout.split()
        assert len(servers) == 1, servers  # the crashed one removed before the restart
    finally:
        await runner.aclose()


# =================================================================================================
# FR-167: example 39
# =================================================================================================


EX39_NAME = "39_sandboxed_mcp.py"
NETWORK = "the server could not reach the network: example.com's address, which the host reaches"
HOST_CHECK = "the server could not reach the host's address and port, which a container on the bridge reaches"
NEIGHBOUR = "the server could not reach a neighbour workspace's proxy, which answers on its own address"


def _example(name: str):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name.removesuffix(".py").replace("-", "_"), REPO / "scripts" / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def example_39(store: str, provider_name: str = "fake", module=None) -> dict:
    checks = await (module or _example(EX39_NAME)).offline(store, provider_name, tenant=tenancy(), quiet=True)
    return {label: (passed, why) for label, passed, why in checks}


@pytest.mark.parametrize("store", STORES)
async def test_example_39_holds_every_check_offline(store):
    checks = await example_39(store)
    assert len(checks) >= 4 and {label: why for label, (passed, why) in checks.items() if passed is not True} == {}
    # F8: what a local process cannot fail is checked on Docker only.
    assert not {NETWORK, HOST_CHECK, NEIGHBOUR} & set(checks)


async def test_example_39_passes_nothing_when_its_server_never_starts(monkeypatch):
    async def refused(self, handle, command, args):
        raise mgr_unavailable("no server starts")

    from agentsdk.errors import WorkspaceUnavailable as mgr_unavailable

    monkeypatch.setattr(FakeProvider, "server_launch", refused)
    checks = await example_39("memory")
    # Only the cleanup may pass: the server's workspace was made, and its launch then refused.
    passed = [label for label, (ok, _) in checks.items() if ok is True]
    assert all(label.startswith("after aclose()") for label in passed), passed
    assert any("served" in label and ok is not True for label, (ok, _) in checks.items())


def test_example_39_runs_offline_as_a_program():
    done = subprocess.run([sys.executable, str(REPO / "scripts" / EX39_NAME), "--offline"],
                          capture_output=True, text=True, timeout=600, cwd=REPO)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-2000:]
    assert "[FAIL]" not in done.stdout and done.stdout.count("[PASS]") >= 4


def test_example_39_is_listed():
    assert EX39_NAME in (REPO / "README.md").read_text(encoding="utf-8")
    assert f'"{EX39_NAME}"' in (REPO / "tests" / "test_distribution.py").read_text(encoding="utf-8")


@pytest.mark.docker
@pytest.mark.parametrize("store", STORES)
async def test_on_docker_example_39_holds_every_check(store):
    checks = await example_39(store, "docker")
    assert {NETWORK, HOST_CHECK, NEIGHBOUR} <= set(checks)
    assert {label: why for label, (passed, why) in checks.items() if passed is not True} == {}


@pytest.mark.docker
@pytest.mark.parametrize("control", ["network", "host", "neighbour"])
async def test_on_docker_example_39s_checks_fail_without_their_controls(monkeypatch, control):
    """No pass from a dead address: each way out's check fails when its control does not answer."""
    module = _example(EX39_NAME)
    if control == "network":
        monkeypatch.setattr(module, "host_reaches", lambda address: False)
    else:
        real = module.controls

        async def without(chosen, neighbour):
            found = await real(chosen, neighbour)
            assert found == {"host": True, "neighbour": True}, found  # both answer, as they should
            return {**found, control: False}

        monkeypatch.setattr(module, "controls", without)
    checks = await example_39("memory", "docker", module)
    label = {"network": NETWORK, "host": HOST_CHECK, "neighbour": NEIGHBOUR}[control]
    assert checks[label][0] is False, checks[label]


async def test_a_server_workspace_that_cannot_be_made_is_removed_and_frees_its_slot(monkeypatch):
    """Its files refused at the import: what was created goes, unrecorded, and the slot is given back."""
    async def refused(self, handle, data):
        raise ValueError("the transfer filter refused it")

    monkeypatch.setattr(FakeProvider, "import_", refused)
    tenant = tenancy()
    provider = FakeProvider()
    runner = runner_for([sandboxed()], provider, max_concurrent_workspaces=1)
    try:
        await run(runner, agent("legacy__echo"), [[["legacy__echo", {"text": "hi"}]]], tenant)
        assert not provider._spaces and await rows(runner, tenant) == []
        assert runner._workspaces.slots.held == 0
    finally:
        await runner.aclose()


@pytest.mark.parametrize("outcome, answers, refused_reaches, failing", [
    ("failed OSError", True, False, False), ("failed TimeoutError", True, True, False),
    ("failed ConnectionRefusedError", True, True, True), ("failed ConnectionRefusedError", True, False, False),
    ("connected", True, False, True), (None, True, False, True), ("failed OSError", False, False, True),
])
def test_example_39s_way_out_check_needs_a_failure_inside_and_an_answer_outside(outcome, answers, refused_reaches,
                                                                                   failing):
    clause = _example(EX39_NAME).failed_inside(outcome, answers=answers, refused_reaches=refused_reaches)
    assert (clause is not None) is failing, clause


async def test_closing_its_servers_alone_removes_a_server_workspace():
    """A server's close removes its workspace itself -- as when a Runner collected without aclose()
    ends its servers (NFR-39) -- not only the sweep that Runner.aclose() makes after it."""
    tenant = tenancy()
    provider = FakeProvider()
    runner = runner_for([sandboxed()], provider)
    try:
        await run(runner, agent("legacy__echo"), [[["legacy__echo", {"text": "hi"}]]], tenant)
        [space] = provider._spaces.values()
        folder = space.folder
        await runner._mcp.aclose()  # the servers alone, no workspace sweep
        [row] = await rows(runner, tenant)
        assert row.removed_reason == "aclose" and not provider._spaces and not folder.exists()
        assert runner._workspaces.slots.held == 0  # its slot given back (pre-review F3)
    finally:
        await runner.aclose()



@pytest.mark.parametrize("files", [{"a": b"1", "a/b.py": b"2"}, {"A.py": b"1", "a.py": b"2"}])
def test_files_that_are_not_one_tree_raise(files):
    """Pre-review F5: a path that is also another's folder, or two told apart by case only."""
    with pytest.raises(ValueError, match="files"):
        StdioServer(alias="s", command=PYTHON, workspace=WorkspaceSpec(image=FAKE_IMAGE), files=files)


def test_a_sandboxed_servers_start_and_close_bounds_include_its_workspace():
    """Pre-review F2: the caller's deadline holds every wait a sandboxed start adds."""
    from agentsdk import mcp
    from agentsdk.workspace import SERVER_CLOSE_SECONDS, SERVER_START_SECONDS, SETUP_SECONDS
    from agentsdk.workspace import manager

    assert SERVER_START_SECONDS == 2 * SETUP_SECONDS + manager._REMOVE_SECONDS + 2 * dk._CLI_SECONDS
    assert SERVER_CLOSE_SECONDS == 3 * manager._REMOVE_SECONDS
    plain = StdioServer(alias="p", command=PYTHON, connect_timeout=5)
    boxed = StdioServer(alias="b", command=PYTHON, connect_timeout=5, workspace=WorkspaceSpec(image=FAKE_IMAGE))
    assert mcp._ensure_bound(boxed) == mcp._ensure_bound(plain) + 5 + SERVER_START_SECONDS


async def test_a_closed_manager_makes_no_server_workspace():
    from agentsdk.errors import WorkspaceUnavailable
    from agentsdk.workspace.manager import WorkspaceManager
    from agentsdk.workspace.records import InMemoryWorkspaceStore

    provider = FakeProvider()
    manager = WorkspaceManager(provider, InMemoryWorkspaceStore, 1)
    await manager.aclose()
    assert await manager.slots.take(None)  # its one slot held: a closed manager must not wait for it
    started = time.monotonic()
    with pytest.raises(WorkspaceUnavailable, match="closed"):
        await manager.open_server(WorkspaceSpec(image=FAKE_IMAGE), {}, tenancy(), PROJECT, seconds=30)
    assert time.monotonic() - started < 5 and not provider._spaces and manager.slots.held == 1


async def test_a_server_side_refuses_once_its_runners_manager_is_closed_or_gone():
    """Round 3 (DECISION-8e21d1ff): the engine's server side shares its Runner's manager's provider,
    owner, label and slots, and reaches that manager only weakly, for whether it was closed; once
    it is gone, its Runner was dropped unclosed, so nothing more is made."""
    import gc

    from agentsdk.errors import WorkspaceUnavailable
    from agentsdk.workspace.manager import WorkspaceManager
    from agentsdk.workspace.records import InMemoryWorkspaceStore

    provider = FakeProvider()
    manager = WorkspaceManager(provider, InMemoryWorkspaceStore, 1)
    servers = manager.for_servers()
    assert servers is not manager and servers.slots is manager.slots and servers.provider is provider
    assert (servers.owner, servers.runner_id, servers.store) == (manager.owner, manager.runner_id, manager.store)
    await manager.aclose()
    with pytest.raises(WorkspaceUnavailable, match="closed"):
        await servers.open_server(WorkspaceSpec(image=FAKE_IMAGE), {}, tenancy(), PROJECT, seconds=1)
    orphan = WorkspaceManager(provider, InMemoryWorkspaceStore, 1).for_servers()
    gc.collect()
    assert orphan._runs() is None
    with pytest.raises(WorkspaceUnavailable, match="closed"):
        await orphan.open_server(WorkspaceSpec(image=FAKE_IMAGE), {}, tenancy(), PROJECT, seconds=1)
    assert not provider._spaces and manager.slots.held == 0


async def test_a_server_waiting_for_a_slot_while_its_runner_closes_makes_no_workspace():
    """Round 3 pre-review S3: the check after the slot is taken was untested -- its Runner closed
    while it waited, the slot it is then given goes back, and nothing is made."""
    from agentsdk.errors import WorkspaceUnavailable
    from agentsdk.workspace.manager import WorkspaceManager
    from agentsdk.workspace.records import InMemoryWorkspaceStore

    provider = FakeProvider()
    manager = WorkspaceManager(provider, InMemoryWorkspaceStore, 1)
    servers = manager.for_servers()
    assert await manager.slots.take(None)  # the one slot, held by a run
    opening = asyncio.ensure_future(servers.open_server(WorkspaceSpec(image=FAKE_IMAGE), {}, tenancy(), PROJECT,
                                                        seconds=10))
    await asyncio.sleep(0.1)
    await manager.aclose()
    manager.slots.give()  # the run's slot handed to the waiting server
    with pytest.raises(WorkspaceUnavailable, match="closed"):
        await opening
    assert not provider._spaces and manager.slots.held == 0


def test_a_waiter_whose_loop_was_closed_is_passed_over():
    """Round 3 pre-review: a waiter still held, its loop closed under it, can be granted nothing;
    given the slot, it lost it and the give raised. It is passed over, and the next waiter gets it."""
    from agentsdk.workspace.manager import _Slots

    slots = _Slots(1)
    closed = asyncio.new_event_loop()

    async def first():
        assert await slots.take(None)
        return asyncio.ensure_future(slots.take(None))

    waiting = closed.run_until_complete(first())
    closed.run_until_complete(asyncio.sleep(0.05))
    closed.close()  # `waiting` is still held

    async def second():
        later = asyncio.ensure_future(slots.take(None))
        await asyncio.sleep(0.05)
        slots.give()
        return await asyncio.wait_for(later, 5)

    try:
        assert asyncio.run(second()) is True and slots.held == 1 and not slots._waiters
    finally:
        del waiting


async def _server_folder_in_use(seconds: float):
    """A fake server workspace whose folder is a live process's working folder for `seconds`: on
    Windows nothing in it can be removed until that process has ended."""
    provider = FakeProvider()
    handle = await provider.create(WorkspaceSpec(image=FAKE_IMAGE), {"agentsdk.workspace": str(uuid.uuid4())})
    launch = await provider.server_launch(handle, sys.executable, [])
    held = subprocess.Popen([sys.executable, "-c", f"import time; print('up', flush=True); time.sleep({seconds})"],
                            cwd=launch.cwd, stdout=subprocess.PIPE)
    assert held.stdout.readline().strip() == b"up"  # running in it: before that, the folder is not yet held
    return provider, handle, Path(launch.cwd), held


@pytest.mark.skipif(sys.platform != "win32", reason="a process's working folder is held only on Windows")
async def test_a_server_folder_still_in_use_is_removed_once_its_process_has_ended():
    """Round 4 NB2: the fake removed a server's folder once, ignoring errors, while its process
    was still ending -- an empty folder left behind each time. It tries again, as Docker does."""
    provider, handle, folder, held = await _server_folder_in_use(1.0)
    try:
        await asyncio.wait_for(provider.remove(handle), 30)
        assert not folder.exists() and await provider.list_owned({}) == []
    finally:
        held.kill()
        held.wait(10)


@pytest.mark.skipif(sys.platform != "win32", reason="a process's working folder is held only on Windows")
async def test_a_server_folder_that_stays_in_use_fails_the_removal_and_is_left_to_the_reap():
    """Round 4 NB2: a folder that cannot be removed raises, as Docker's removal does, and stays
    listed, so a reap finds it once its process has ended."""
    from agentsdk.errors import WorkspaceUnavailable

    provider, handle, folder, held = await _server_folder_in_use(120)
    try:
        with pytest.raises(WorkspaceUnavailable):
            await asyncio.wait_for(provider.remove(handle), 30)
        assert folder.exists() and await provider.list_owned({}) == [handle]
    finally:
        held.kill()
        held.wait(10)
    await asyncio.wait_for(provider.remove(handle), 30)
    assert not folder.exists() and await provider.list_owned({}) == []


class _ClosesMeanwhile:
    """A waiter's loop closed on another thread after the slots checked it: open when asked, then
    refusing the grant, as a real loop does once closed."""

    def is_closed(self):
        return False

    def call_soon_threadsafe(self, *args):
        raise RuntimeError("Event loop is closed")


async def test_a_slot_whose_waiters_loop_closes_meanwhile_goes_to_the_next_or_back():
    """Round 4 NB1: the give popped a waiter and marked it granted, then raised from its closed
    loop with `held` never lowered -- the slot lost for the Runner's life. It goes to the next
    waiter, or back to the count."""
    from agentsdk.workspace.manager import _Slots

    slots = _Slots(1)
    assert await slots.take(None)
    lost = asyncio.ensure_future(slots.take(None))
    later = asyncio.ensure_future(slots.take(None))
    await asyncio.sleep(0.05)
    slots._waiters[0]().loop = _ClosesMeanwhile()
    slots.give()
    assert await asyncio.wait_for(later, 5) is True and slots.held == 1 and not slots._waiters
    lost.cancel()

    slots.give()
    lost = asyncio.ensure_future(slots.take(None))
    assert await slots.take(None)
    await asyncio.sleep(0.05)
    slots._waiters[0]().loop = _ClosesMeanwhile()
    slots.give()
    assert slots.held == 0 and not slots._waiters
    assert await asyncio.wait_for(slots.take(None), 5) is True
    lost.cancel()


async def test_a_release_whose_waiters_loop_closes_meanwhile_still_releases_the_rest():
    """Round 4 NB1, its second copy: one closed loop raised out of the release, and the waits
    after it, for the same workspace, were never ended."""
    from agentsdk.workspace.manager import _Slots

    slots, space = _Slots(1), object()
    assert await slots.take(None)
    lost = asyncio.ensure_future(slots.take(None, space))
    later = asyncio.ensure_future(slots.take(None, space))
    await asyncio.sleep(0.05)
    slots._waiters[0]().loop = _ClosesMeanwhile()
    slots.release(space)
    assert await asyncio.wait_for(later, 5) is False and slots.held == 1 and not slots._waiters
    lost.cancel()


async def test_the_engines_close_waits_for_a_servers_late_count():
    """Round 3 pre-review: a report after a server workspace's recorder ended is counted by a pass
    of its own on the engine's server side, which no `aclose()` waits for: the engine's close does,
    through `trailed()`, before it ends its loop's tasks."""
    from agentsdk.workspace.manager import WorkspaceManager
    from agentsdk.workspace.records import InMemoryWorkspaceStore

    store, provider = InMemoryWorkspaceStore(), FakeProvider()
    manager = WorkspaceManager(provider, lambda: store, 1)
    servers = manager.for_servers()
    space = await servers.open_server(WorkspaceSpec(image=FAKE_IMAGE, egress=EGRESS), {}, tenancy(), PROJECT,
                                      seconds=5)
    handle = space.handle
    await servers.close_server(space)
    assert space.booked
    real = servers._count_unrecorded

    async def slow(space, handle):
        await asyncio.sleep(0.5)
        await real(space, handle)

    servers._count_unrecorded = slow
    servers.arrived(space, handle, {"kind": "connection", "id": "c1", "allowed": False, "reason": "not_allowed"})
    [late] = servers._late
    await servers.trailed()
    assert late.done()


async def test_a_failing_wait_for_trailing_work_never_skips_the_engines_ending_of_its_tasks(monkeypatch):
    """Round 3 NB-D: the engine's close awaited the trailing work unguarded, while the closes
    beside it are guarded; an exception there skipped ending the loop's tasks, and a caller
    still waiting on one waited out its whole bound."""
    from agentsdk import mcp
    from agentsdk.workspace.manager import WorkspaceManager
    from agentsdk.workspace.records import InMemoryWorkspaceStore

    async def broken(self):
        raise RuntimeError("the wait broke")

    monkeypatch.setattr(WorkspaceManager, "trailed", broken)
    clients = mcp.McpClients([sandboxed()], frozenset())
    clients.use_workspaces(WorkspaceManager(FakeProvider(), InMemoryWorkspaceStore, 1))

    async def forever():
        await asyncio.sleep(3600)

    waiting = asyncio.ensure_future(clients._engine.submit(forever(), 120))
    await asyncio.sleep(0.5)
    started = time.monotonic()
    await clients.aclose()
    await asyncio.wait({waiting}, timeout=30)
    assert waiting.done() and time.monotonic() - started < 30
    with pytest.raises(mcp._Closed):
        waiting.result()


async def test_a_workspace_whose_record_write_timed_out_is_recorded_removed(monkeypatch):
    """Pre-review F6: the write lands after its bound; the workspace goes, and its row reads removed."""
    from agentsdk.workspace import manager as mgr
    from agentsdk.workspace import records

    monkeypatch.setattr(mgr, "_REMOVE_SECONDS", 0.3)
    real = records.InMemoryWorkspaceStore.record_workspace

    def late(self, record):
        real(self, record)
        time.sleep(0.6)

    monkeypatch.setattr(records.InMemoryWorkspaceStore, "record_workspace", late)
    tenant = tenancy()
    provider = FakeProvider()
    runner = runner_for([sandboxed()], provider)
    try:
        await run(runner, agent("legacy__echo"), [[["legacy__echo", {"text": "hi"}]]], tenant)
        await asyncio.sleep(0.5)
        written = await rows(runner, tenant)
        assert not provider._spaces and written  # it did land
        assert all(r.removed_reason == "aclose" for r in written), written
    finally:
        await runner.aclose()


async def test_a_dead_server_gives_back_its_workspace_and_slot_at_once():
    """Pre-review F7 (DECISION-5470ab57): its restart spent, it dies again: its workspace goes now."""
    tenant = tenancy()
    provider = FakeProvider()
    runner = runner_for([sandboxed()], provider, max_concurrent_workspaces=1)
    try:
        await run(runner, agent("legacy__die", "legacy__echo"),
                  [[["legacy__die", {}]], [["legacy__echo", {"text": "a"}]], [["legacy__die", {}]],
                   [["legacy__echo", {"text": "b"}]]], tenant)
        [row] = await rows(runner, tenant)
        assert row.removed_reason == "aclose" and not provider._spaces
        assert runner._workspaces.slots.held == 0
    finally:
        await runner.aclose()


@pytest.mark.docker
@pytest.mark.parametrize("store", STORES)
async def test_on_docker_a_servers_egress_is_recorded_as_rows_with_no_run(store):
    """Pre-review F3 (DECISION-61aed587): a sandboxed server with egress, its connection a row with
    no run and no command, on both stores."""
    ex39 = _example(EX39_NAME)
    tenant = tenancy()
    provider = DockerProvider()
    server = StdioServer(alias="probe", command="python3", args=("server.py",), approval="never",
                         connect_timeout=120, workspace=WorkspaceSpec(image=IMAGE, egress=("example.com:443",)),
                         files={"server.py": ex39.SERVER.encode("utf-8")})
    runner = runner_for([server], provider, store)
    found = []
    try:
        await run(runner, agent("probe__echo"), [[["probe__echo", {"text": "up"}]]], tenant)
        [space] = provider._spaces.values()
        await run(runner, agent("probe__reach"), [[["probe__reach", {"host": space.address, "port": 3128}]]],
                  tenant)
        for _ in range(100):
            found = await asyncio.to_thread(runner._workspace_store().connections, tenant, PROJECT)
            if found:
                break
            await asyncio.sleep(0.1)
    finally:
        await runner.aclose()
    assert found and all(r.run_id is None and r.command_id is None for r in found), found


async def test_the_engine_close_bound_holds_a_sandboxed_servers_close(monkeypatch):
    """Pre-review F2: Runner.aclose() waits for its servers within a bound that holds the
    removal of a sandboxed server's workspace."""
    from agentsdk import mcp
    from agentsdk.workspace import SERVER_CLOSE_SECONDS

    seen, real = [], asyncio.wait_for

    async def noted(awaitable, timeout):
        seen.append(timeout)
        return await real(awaitable, timeout)

    clients = mcp.McpClients([sandboxed()], frozenset())
    monkeypatch.setattr(mcp.asyncio, "wait_for", noted)
    await clients.aclose()
    assert seen and seen[0] >= mcp._ensure_bound(sandboxed()) + SERVER_CLOSE_SECONDS


async def test_example_39s_cleanup_check_fails_when_the_servers_folder_remains(monkeypatch):
    """Pre-review F4: the check read only the listing and the rows, never the folder on disk."""
    import agentsdk.workspace as ws

    left, real = [], ws.shutil.rmtree
    monkeypatch.setattr(ws.shutil, "rmtree", lambda path, *args, **kwargs: left.append(path))
    checks = await example_39("memory")
    monkeypatch.undo()
    for path in left:
        real(path, ignore_errors=True)
    [label] = [label for label in checks if label.startswith("after aclose()")]
    assert left and checks[label][0] is False, checks[label]


async def test_a_dead_runners_server_workspace_is_reaped():
    """AC-103: a dead Runner's server workspace is gone after reaping, recorded `reaped`."""
    from agentsdk.workspace.manager import WorkspaceManager
    from agentsdk.workspace.owner import this_owner
    from agentsdk.workspace.records import InMemoryWorkspaceStore

    host = this_owner().rsplit(":", 2)[0]
    provider, store, tenant = FakeProvider(), InMemoryWorkspaceStore(), tenancy()
    dead = WorkspaceManager(provider, lambda: store, 1)
    dead.owner = f"{host}:4194303:1"  # a process that is not running on this host
    space = await dead.open_server(WorkspaceSpec(image=FAKE_IMAGE), {"a.py": b"x"}, tenant, PROJECT, seconds=1)
    await WorkspaceManager(provider, lambda: store, 1).reap()
    [row] = store.workspaces(tenant, PROJECT)
    assert not provider._spaces and row.workspace_id == space.handle.workspace_id
    assert (row.scope, row.run_id, row.removed_reason) == ("server", None, "reaped")


def test_a_collected_runners_sandboxed_server_and_its_workspace_are_gone(tmp_path):
    """Round 1 B1 (NFR-39): a Runner with a sandboxed server, collected with its event loop closed
    and never closed, still ends its server and removes its workspace. Its manager's store getter
    held the Runner, which the MCP engine's finalizer then kept alive for ever."""
    import gc
    import weakref

    tenant, pids = tenancy(), tmp_path / "pids"
    provider = FakeProvider()

    async def main():
        runner = runner_for([sandboxed(pids=pids)], provider)
        await run(runner, agent("legacy__echo"), [[["legacy__echo", {"text": "hi"}]]], tenant)
        return weakref.ref(runner), runner._memory_workspaces, [s.folder for s in provider._spaces.values()]

    collected, store, folders = asyncio.run(main())  # its loop closed; it was never closed
    gc.collect()
    assert collected() is None, "the Runner was not collected"
    deadline = time.monotonic() + 30
    while provider._spaces and time.monotonic() < deadline:
        time.sleep(0.1)
    assert folders and not provider._spaces and not any(f.exists() for f in folders)
    assert [r.removed_reason for r in store.workspaces(tenant, PROJECT)] == ["aclose"]
    assert pids_in(pids) and gone(pids_in(pids))


async def test_a_record_write_that_lands_after_its_bound_is_then_recorded_removed(monkeypatch):
    """Round 1 N1: the write past its bound landed after the removal, an unremoved row for a
    workspace that was gone; its removal record now follows it when it lands."""
    from agentsdk.workspace import manager as mgr
    from agentsdk.workspace import records

    monkeypatch.setattr(mgr, "_REMOVE_SECONDS", 0.3)
    real = records.InMemoryWorkspaceStore.record_workspace

    def late(self, record):
        time.sleep(0.6)  # past its bound, then it lands
        real(self, record)

    monkeypatch.setattr(records.InMemoryWorkspaceStore, "record_workspace", late)
    tenant = tenancy()
    provider = FakeProvider()
    runner = runner_for([sandboxed()], provider)
    try:
        await run(runner, agent("legacy__echo"), [[["legacy__echo", {"text": "hi"}]]], tenant)
        for _ in range(50):
            written = await rows(runner, tenant)
            if written and all(r.removed_reason for r in written):
                break
            await asyncio.sleep(0.1)
        assert not provider._spaces and written, written
        assert all(r.removed_reason == "aclose" for r in written), written
    finally:
        await runner.aclose()


# =================================================================================================
# NFR-39 as a property (M45 round 2, DECISION-3756a232; round 3, DECISION-8e21d1ff): nothing the MCP
# engine reaches leads back to a Runner, whatever its runs did. Rounds 1 to 3 each found one edge,
# all through the Runner's manager, which holds run state by design: the engine now reaches it only
# weakly, and the two things it shares with it -- the slots and the provider -- hold no run strongly.
# =================================================================================================


def _cells(function) -> list:
    out = []
    for cell in function.__closure__ or ():
        try:
            out.append(cell.cell_contents)
        except ValueError:  # an empty cell
            pass
    return out


def _paths_to(start, is_target) -> list[str]:
    """Every path from `start` to an object `is_target` accepts: through objects, closures,
    defaults, a function's own attributes, bound methods and class attributes -- never a function's
    or a module's globals, which reach everything (declared). A weak reference is not followed."""
    import gc
    import types

    def referents(obj):
        if isinstance(obj, types.FunctionType):
            return (_cells(obj) + list(obj.__defaults__ or ()) + list((obj.__kwdefaults__ or {}).values())
                    + list(vars(obj).values()))
        if isinstance(obj, types.MethodType):
            return [obj.__self__, obj.__func__]
        if isinstance(obj, type):
            return list(vars(obj).values()) + list(obj.__mro__[1:])
        if isinstance(obj, types.ModuleType):
            return []
        return gc.get_referents(obj)

    seen, queue, found = {id(start)}, [(start, type(start).__name__)], []
    while queue:
        obj, path = queue.pop(0)
        for ref in referents(obj):
            if id(ref) in seen:
                continue
            seen.add(id(ref))
            step = f"{path} -> {type(ref).__name__}"
            if is_target(ref):
                found.append(step)
            else:
                queue.append((ref, step))
    return found


def _run_state_reachable_from(start) -> list[str]:
    """Every path from `start` to a Runner, a run's control or handle, an event sink, a run's
    workspace, a Runner's manager (one that is no server side), or a waiter for a slot."""
    from agentsdk.events import InMemoryEventSink
    from agentsdk.handle import PublishingSink, RunControl, RunHandle
    from agentsdk.workspace.manager import RunWorkspace, WorkspaceManager, _Slots

    targets = (Runner, RunControl, RunHandle, PublishingSink, InMemoryEventSink, RunWorkspace, _Slots._Waiter)
    return _paths_to(start, lambda obj: isinstance(obj, targets)
                     or (isinstance(obj, WorkspaceManager) and obj._runs is None))


class _Mark:
    """What the walker's own test looks for."""


class _Holds:
    kept = None


def test_the_walker_follows_every_kind_of_strong_reference_and_no_weak_one():
    """Round 3 NB-C: the walker missed keyword defaults, a function's attributes and class
    attributes. Each holder below reaches a mark one way; the weak one must not."""
    import functools
    import types
    import weakref

    def is_mark(obj):
        return isinstance(obj, _Mark)

    def closure(mark):
        return lambda: mark

    def default(x=_Mark()):
        return x

    def keyword(*, x=_Mark()):
        return x

    def attribute():
        return None

    attribute.held = _Mark()
    holder = type("Holder", (_Holds,), {})
    holder.kept = _Mark()

    async def suspended(mark):
        await asyncio.Event().wait()
        return mark

    coroutine = suspended(_Mark())
    holders = {"closure": closure(_Mark()), "default": default, "keyword default": keyword,
               "function attribute": attribute, "class attribute": holder(),
               "bound method": types.MethodType(lambda self: None, _Mark()),
               "partial": functools.partial(print, _Mark()), "coroutine local": coroutine,
               "contained": {"a": [(_Mark(),)]}}
    try:
        for kind, start in holders.items():
            assert _paths_to(start, is_mark), kind
        mark = _Mark()
        assert _paths_to(weakref.ref(mark), is_mark) == []
        assert _paths_to({"ref": weakref.ref(mark)}, is_mark) == []
    finally:
        coroutine.close()


class _HangsAfterACommand:
    """Runs one command, then waits until it is cancelled."""

    def __init__(self, argv=("ok",)) -> None:
        self.argv = list(argv)

    async def send(self, request):
        if not any(m.role is Role.ASSISTANT for m in request.messages):
            return respond(calls=[ToolCall(id="t0", name="run_command", arguments={"argv": self.argv})])
        await asyncio.Event().wait()


class _Fails:
    """Makes its turns' calls, then raises where it would answer."""

    def __init__(self) -> None:
        self.turns = Turns()

    async def send(self, request):
        turns = json.loads(request.messages[0].content.split("\n")[0])
        if sum(1 for m in request.messages if m.role is Role.ASSISTANT) >= len(turns):
            raise RuntimeError("the model broke")
        return await self.turns.send(request)


async def _ok(cmd):
    cmd.out("ok\n")
    return 0


EGRESS = ("example.com:443",)
SPECS = {"kept": {"keep": True}, "kept with egress": {"keep": True, "egress": EGRESS}, "plan": {"scope": "plan"},
         "plan with egress": {"scope": "plan", "egress": EGRESS}, "egress": {"egress": EGRESS},
         "mid-command with egress": {"egress": EGRESS}, "mid-connection": {"egress": EGRESS}}
SHAPES = ["plain", "kept", "kept with egress", "plan", "plan with egress", "egress", "failed", "cancelled",
          "waiting for a slot"]
# Abandoned (round 3 B1): its loop closed with the run still under way, the Runner dropped, never
# closed -- what NFR-39 names. Nothing at the run's end runs. Mid-command, its provider still keeps
# the command a run awaits; mid-connection, the fake's proxy keeps the command's task as its
# handler (round 3, DECISION-8e21d1ff and its pre-review).
ABANDONED = ["plain", "kept", "kept with egress", "plan", "plan with egress", "egress", "waiting for a slot",
             "mid-command", "mid-command with egress", "mid-connection"]
CASES = [(shape, "ended") for shape in SHAPES] + [(shape, "abandoned") for shape in ABANDONED]


async def _driven(shape: str, mode: str, provider, started, *, image: str = FAKE_IMAGE, server=None,
                  hang: tuple[str, ...] = ("hang",)) -> Runner:
    """A Runner with a sandboxed server, whose one run had `shape`, and ended or was abandoned once
    `started()` (its command ran, or is running)."""
    from agentsdk.tools import ApprovalPolicy

    workspace = WorkspaceSpec(image=image, run_command_approval=ApprovalPolicy.AUTO, **SPECS.get(shape, {}))
    the_agent = AgentSpec(id="a", instructions="x", preferred_model="m:x", tool_profile=("run_command", "legacy__echo"),
                          workspace=workspace)
    hangs = shape == "cancelled" or mode == "abandoned"
    argv = ["connect"] if shape == "mid-connection" else list(hang) if shape.startswith("mid-command") else ["ok"]
    model = (Turns() if shape == "waiting for a slot" else _HangsAfterACommand(argv) if hangs
             else _Fails() if shape == "failed" else Turns())
    runner = Runner({"m": model}, mcp_servers=[server or sandboxed()], workspace_provider=provider,
                    max_concurrent_workspaces=1 if shape == "waiting for a slot" else 4)
    config = RunConfig(tenant_id=tenancy(), project_id=PROJECT, max_turns=4)
    turns = json.dumps([[["legacy__echo", {"text": "hi"}], ["run_command", {"argv": ["ok"]}]]])
    if shape == "waiting for a slot":
        # The server takes the only slot first, so the run's workspace waits for it.
        await runner.run(AgentSpec(id="b", instructions="x", preferred_model="m:x", tool_profile=("legacy__echo",)),
                         json.dumps([[["legacy__echo", {"text": "hi"}]]]), config)
        assert runner._workspaces.slots.held == 1
    if not hangs and shape != "waiting for a slot":
        await runner.run(the_agent, turns, config)
        return runner
    handle = await runner.start(the_agent, "go" if hangs and shape != "waiting for a slot" else turns, config)
    deadline = time.monotonic() + 120
    waited = (lambda: runner._workspaces.slots._waiters) if shape == "waiting for a slot" else started
    while time.monotonic() < deadline and not waited():
        await asyncio.sleep(0.05)
    assert waited(), "the run never reached its shape"
    if mode == "ended":
        handle.cancel()
        await asyncio.wait({asyncio.ensure_future(handle.result())}, timeout=30)
    return runner


def _check_collected(shape: str, mode: str, provider, started, forget, **driven) -> None:
    """Its event loop closed (NFR-39's condition: a live loop is a root of its own, and reaches all
    it schedules) and the Runner still held, so the walk is not vacuous, the engine reaches no run
    state and reaches the Runner's manager only weakly; then the Runner, dropped and never closed,
    is collected, and its server's workspace removed by the engine's finalizer."""
    import gc
    import weakref

    loop = asyncio.new_event_loop()
    collected, server, walked = None, None, False
    try:
        try:
            runner = loop.run_until_complete(_driven(shape, mode, provider, started, **driven))
        finally:
            loop.close()
        engine = runner._mcp._engine
        assert engine.sandbox is not runner._workspaces and engine.sandbox._runs() is runner._workspaces
        [server] = [c._server_space.handle.workspace_id for c in engine.connections.values() if c._server_space]
        found = _run_state_reachable_from(engine)
        assert found == [], found
        walked = True
        collected = weakref.ref(runner)
        del runner, engine
        forget()  # what the test itself kept of a command
    finally:
        with _reports_as_text():
            gc.collect()
            try:
                if walked:  # a walk that failed keeps its paths as the failure (pre-review S1)
                    assert collected() is None, f"{shape}, {mode}: the Runner was not collected"
                    deadline = time.monotonic() + 60
                    while server in provider._spaces and time.monotonic() < deadline:
                        time.sleep(0.1)
                    assert server not in provider._spaces, "its server's workspace was not removed"
            finally:
                # What a dropped Runner leaves -- a kept or abandoned run's workspace -- is reaped
                # once its process is gone (FR-151); this test removes it now.
                asyncio.run(_remove_all(provider))


# What a loop closed under a dropped Runner reports, collected (pre-review S2): anything else fails.
EXPECTED_REPORTS = re.compile(
    r"(RuntimeError: (Event loop is closed|There is no current event loop.*|no running event loop|coroutine ignored GeneratorExit)"
    r"|RuntimeWarning: coroutine '.*' was never awaited|ResourceWarning: (unclosed transport .*|subprocess \d+ is still running))$")


@contextlib.contextmanager
def _reports_as_text():
    """Collected, a dropped Runner's coroutines and transports, pending on its closed loop, are
    closed, and report that what their `finally` or `__del__` would do on it cannot run; removing
    its workspaces afterwards reports the same. pytest keeps each report's object -- an
    unraisable's, a warning's source -- which would bring back what the collection frees: here a
    report is noted as text alone, and must be one of those (EXPECTED_REPORTS)."""
    noted, hook, show = [], sys.unraisablehook, warnings.showwarning

    def unraisable(report):
        noted.append(f"{report.exc_type.__name__}: {report.exc_value}")

    def warned(message, category, filename, lineno, file=None, line=None):
        noted.append(f"{category.__name__}: {message}")

    sys.unraisablehook = unraisable
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("always")
            warnings.showwarning = warned
            yield noted
    finally:
        sys.unraisablehook, warnings.showwarning = hook, show
    unexpected = [report for report in noted if not EXPECTED_REPORTS.match(report)]
    assert unexpected == [], unexpected


def test_reports_are_checked_by_kind_not_only_noted():
    """Round 3 pre-review S2: what a closed loop reports is allowed; anything else fails the test."""
    with _reports_as_text() as noted:
        warnings.warn("unclosed transport <_ProactorBasePipeTransport closed>", ResourceWarning)
    assert noted == ["ResourceWarning: unclosed transport <_ProactorBasePipeTransport closed>"]
    with pytest.raises(AssertionError, match="a finalizer broke"):
        with _reports_as_text():
            warnings.warn("a finalizer broke", UserWarning)


@pytest.mark.parametrize("shape, mode", CASES)
def test_whatever_its_run_did_the_engine_reaches_no_run_state_and_its_runner_is_collected(shape, mode):
    """Round 3 (DECISION-8e21d1ff): each shape, ended or abandoned, on the fake."""
    ran = []

    async def ok(cmd):
        ran.append(cmd)
        return await _ok(cmd)

    async def hang(cmd):
        ran.append(cmd)
        await asyncio.Event().wait()

    async def connect(cmd):
        ran.append(cmd)
        await cmd.proxy(b"CONNECT example.com:443 HTTP/1.1\r\n")  # a head never finished
        return 0

    provider = FakeProvider(commands={"ok": ok, "hang": hang, "connect": connect})

    def started():
        return ran and (shape != "mid-connection"
                        or any(s.proxy is not None and s.proxy.handlers for s in list(provider._spaces.values())))

    _check_collected(shape, mode, provider, started, ran.clear)


@pytest.mark.docker
@pytest.mark.parametrize("shape", ["mid-command", "mid-command with egress"])
def test_on_docker_a_runner_abandoned_mid_command_is_collected(shape):
    """Round 3 (DECISION-8e21d1ff): on Docker, the provider keeps a running command's container and
    kill, which its run awaits; abandoned there, the Runner is still collected. Checked in a process
    of its own: on Docker a closed loop's transports report from threads and timers past any window
    this process could guard, and pytest keeps each report's object -- which brings the Runner
    back. What that process leaves, its owner gone, is reaped as FR-151 reaps it."""
    from agentsdk.workspace.manager import WorkspaceManager
    from agentsdk.workspace.records import InMemoryWorkspaceStore

    done = subprocess.run([sys.executable, __file__, shape], cwd=REPO, capture_output=True, text=True,
                          timeout=600)
    store = InMemoryWorkspaceStore()
    asyncio.run(WorkspaceManager(DockerProvider(), lambda: store, 1).reap())
    assert done.returncode == 0 and done.stdout.strip().splitlines()[-1:] == ["COLLECTED"], done.stdout[-2000:]


async def _remove_all(provider: FakeProvider) -> None:
    for space in list(provider._spaces.values()):
        with contextlib.suppress(Exception):
            await provider.remove(space.handle)


def test_a_waiter_for_a_slot_is_held_by_its_task_alone():
    """Round 3 (DECISION-8e21d1ff): the slots a sandboxed server's engine shares reach no waiting
    task; a waiter whose run was abandoned is passed over, and the next waiter gets the slot."""
    import gc

    from agentsdk.workspace.manager import _Slots

    slots = _Slots(1)
    abandoned = asyncio.new_event_loop()

    async def first():
        assert await slots.take(None)  # the one slot
        return asyncio.ensure_future(slots.take(None))

    waiting = abandoned.run_until_complete(first())
    abandoned.run_until_complete(asyncio.sleep(0.05))
    assert len(slots._waiters) == 1 and not waiting.done()
    assert _paths_to(slots, lambda obj: isinstance(obj, asyncio.Task)) == []
    abandoned.close()
    del waiting
    gc.collect()

    async def second():
        later = asyncio.ensure_future(slots.take(None))
        await asyncio.sleep(0.05)
        slots.give()
        return await asyncio.wait_for(later, 5)

    assert asyncio.run(second()) is True and slots.held == 1 and not slots._waiters


def test_a_providers_egress_watcher_holds_neither_its_manager_nor_its_workspace():
    """Round 3 (DECISION-8e21d1ff): the provider a sandboxed server's engine shares keeps each
    workspace's watcher; that watcher reaches no manager and no workspace of a run."""
    import gc
    import weakref

    from agentsdk.workspace.manager import WorkspaceManager, _arrival, _Space

    manager = WorkspaceManager(FakeProvider(), lambda: None, 1)
    space = _Space(WorkspaceSpec(image=FAKE_IMAGE), "t", "p", "r")
    watcher = _arrival(manager, space, None)
    assert _paths_to(watcher, lambda obj: isinstance(obj, (WorkspaceManager, _Space))) == []
    gone = weakref.ref(space)
    del space
    gc.collect()
    assert gone() is None
    watcher({"kind": "connection"})  # its workspace gone: dropped, never raised


async def test_a_late_record_still_being_written_at_close_is_recorded_removed_by_the_close(monkeypatch):
    """Round 2 NB1: the removal that follows a late write was a task nothing waited for, so a
    close before it ran ended it with the loop, and the row stayed unremoved."""
    from agentsdk.workspace import manager as mgr
    from agentsdk.workspace import records

    monkeypatch.setattr(mgr, "_REMOVE_SECONDS", 1.0)
    real = records.InMemoryWorkspaceStore.record_workspace

    def late(self, record):
        time.sleep(1.5)  # past its bound, and still writing as the Runner closes
        real(self, record)

    monkeypatch.setattr(records.InMemoryWorkspaceStore, "record_workspace", late)
    tenant = tenancy()
    provider = FakeProvider()
    runner = runner_for([sandboxed()], provider)
    try:
        await run(runner, agent("legacy__echo"), [[["legacy__echo", {"text": "hi"}]]], tenant)
    finally:
        await runner.aclose()  # at once: no wait for the late write but the close's own
    written = await rows(runner, tenant)
    assert written and all(r.removed_reason == "aclose" for r in written), written


# =================================================================================================
# The requirements trace's gaps (M45 round 2, DECISION-3756a232)
# =================================================================================================


async def test_a_sandboxed_servers_results_stay_untrusted_mcp_content():
    """FR-166: a sandboxed server's results keep FR-125's labels."""
    from agentsdk.primitives import InstructionAuthority, Origin, TrustZone

    tenant = tenancy()
    runner = runner_for([sandboxed()], FakeProvider())
    try:
        result = await run(runner, agent("legacy__echo"), [[["legacy__echo", {"text": "hi"}]]], tenant)
        [found] = (await results(runner, result, tenant)).values()
    finally:
        await runner.aclose()
    assert "hi" in found.content
    assert (found.provenance.origin, found.provenance.trust_zone, found.provenance.instruction_authority) == (
        Origin.MCP_RESOURCE, TrustZone.UNTRUSTED, InstructionAuthority.DATA_ONLY)


@pytest.mark.skipif(not DSN, reason="no database")
async def test_the_stored_manifest_records_a_sandboxed_servers_spec_and_its_files_by_name():
    """FR-164: the manifest a run stores, not only the server's record."""
    tenant = tenancy()
    runner = runner_for([sandboxed()], FakeProvider(), "postgres")
    try:
        result = await run(runner, agent("legacy__echo"), [[["legacy__echo", {"text": "hi"}]]], tenant)
    finally:
        await runner.aclose()

    def read():
        with psycopg.connect(DSN) as conn:
            return conn.execute("SELECT mcp_servers FROM execution_manifests WHERE run_id = %s",
                                (result.run_id,)).fetchone()[0]

    [entry] = await asyncio.to_thread(read)
    assert entry["workspace"] == WorkspaceSpec(image=FAKE_IMAGE).record() and entry["files"] == ["mcp_servers.py"]
    assert "legacy-fixture" not in json.dumps(entry)  # a file's name, never its content


async def test_without_a_sandboxed_server_nothing_of_m45_happens(tmp_path):
    """NFR-49: a plain stdio server beside a workspace provider runs as before -- no server
    workspace, no folder, no row, and no workspace in its record."""
    tenant = tenancy()
    provider = FakeProvider()
    plain = StdioServer(alias="legacy", command=PYTHON, args=(str(FIXTURE), "legacy"), approval="never",
                        connect_timeout=120)
    runner = runner_for([plain], provider)
    try:
        result = await run(runner, agent("legacy__echo"), [[["legacy__echo", {"text": "hi"}]]], tenant)
        assert any("hi" in r.content for r in (await results(runner, result, tenant)).values())
        assert not provider._spaces and await rows(runner, tenant) == []
        assert "workspace" not in plain.record() and runner._mcp.sandboxed() == []
    finally:
        await runner.aclose()


if __name__ == "__main__":
    # The Docker half of the round 3 collection test, in a process of its own (above).
    docker_provider = DockerProvider()
    _check_collected(sys.argv[1], "abandoned", docker_provider,
                     # its container running: its call waits on its kill (pre-review B2)
                     lambda: any(command.kill._waiters for space in list(docker_provider._spaces.values())
                                 for command in list(space.running.values())), lambda: None,
                     image=IMAGE, server=sandboxed(image=IMAGE, command="python3"), hang=("sleep", "300"))
    print("COLLECTED")
