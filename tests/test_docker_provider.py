"""M39: the Docker provider, hardening and transfer (FR-142 to FR-145, AC-97, NFR-43, NFR-45).

The gates hold what needs no Docker daemon: the exact flags a command container gets, the env
refusals, the transfer filter, the disk bound failing closed, M38's carried caveats and example 33
on `FakeProvider` (DECISION-31b2c79d). Tests marked `docker` hold the rest against a real daemon,
run live before review with `pytest -m docker` (AC-97).
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
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.postgres import RunScope
from agentsdk.primitives import Message, Role, ToolCall
from agentsdk.tools import ApprovalPolicy
from agentsdk.workspace import DockerProvider, FakeProvider, WorkspaceHandle, WorkspaceSpec
from agentsdk.workspace import docker as dk

from tests.workspace_contract import CONTRACT, Vocabulary

REPO = Path(__file__).resolve().parent.parent
DSN = normalise_database_url(os.environ.get("DATABASE_URL"))
PREFIX = "SYN-m39-"
PROJECT = "p-m39"
STORES = ["memory", "postgres"]
FAKE_IMAGE = "fake/workspace@sha256:" + "0" * 64
# The example image, present on the host and never pulled (FR-137); docker tests need it.
IMAGE = "python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df"
SECRET = "m39-secret-" + uuid.uuid4().hex
TOOLS = ("run_command", "read_workspace_file", "write_workspace_file", "list_workspace")


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


async def run(runner, spec, turns, tenant):
    return await runner.run(agent(spec), json.dumps(turns), RunConfig(tenant_id=tenant, project_id=PROJECT))


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


def _example(name: str):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), REPO / "scripts" / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# =================================================================================================
# FR-142: the container a command gets, exactly
# =================================================================================================


def _handle(spec: WorkspaceSpec, workspace_id="w-1") -> WorkspaceHandle:
    return WorkspaceHandle(workspace_id=workspace_id, spec=spec,
                           labels={"agentsdk.tenant": "t", "agentsdk.workspace": workspace_id})


def test_a_command_container_gets_exactly_the_p5_d6_limits_and_the_argv_after_the_image():
    spec = WorkspaceSpec(image=FAKE_IMAGE, memory_bytes=300 * 2**20, cpus=0.5, pids=64, tmp_bytes=8 * 2**20,
                         env={"GREETING": SECRET})
    args = dk.container_args(_handle(spec), "agentsdk-cmd-x", ["--privileged", "-v", "/:/host"], ["GREETING"])
    image_at = args.index(FAKE_IMAGE)
    assert args[image_at + 1:] == ["--privileged", "-v", "/:/host"], "argv must follow the image, as the command"
    flags = args[:image_at]
    expected = ["create", "--pull", "never", "--name", "agentsdk-cmd-x", "-i",
                "--label", "agentsdk.tenant=t", "--label", "agentsdk.workspace=w-1",
                "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only",
                "--tmpfs", f"/tmp:size={8 * 2**20}", "--user", "65534:65534", "--pids-limit", "64",
                "--memory", str(300 * 2**20), "--memory-swap", str(300 * 2**20), "--cpus", "0.50",
                "--log-driver", "none", "-w", "/workspace", "--mount", "type=volume,src=agentsdk-w-1,dst=/workspace,volume-nocopy",
                "-e", "GREETING", "--entrypoint="]
    assert flags == expected
    assert SECRET not in " ".join(args), "an env value reached the command line (NFR-45)"


def test_the_cli_environment_carries_the_values_and_drops_a_host_name_differing_only_in_case(monkeypatch):
    monkeypatch.setenv("M39_SHARED", "host-value")
    env = DockerProvider._env({"m39_shared": SECRET})
    assert env["m39_shared"] == SECRET
    assert not [k for k in env if k.upper() == "M39_SHARED" and k != "m39_shared"], "two spellings reach the CLI"


@pytest.mark.parametrize("names", [["DOCKER_HOST"], ["docker_config"], ["Path"], ["USERPROFILE"], ["HTTPS_PROXY"],
                                   ["GODEBUG"], ["GREETING", "greeting"]])
async def test_an_env_name_the_cli_reads_or_one_differing_only_in_case_fails_the_workspace(names):
    spec = WorkspaceSpec(image=FAKE_IMAGE, env={name: SECRET for name in names})
    # Refused before any docker call: this CLI does not exist.
    with pytest.raises(WorkspaceUnavailable) as caught:
        await DockerProvider(docker="agentsdk-no-such-docker-cli").create(spec, {})
    assert names[-1] in str(caught.value) and SECRET not in str(caught.value)
    assert dk.env_refusal({"GREETING": SECRET, "PYTHONPATH": SECRET}) is None


async def test_an_unreachable_daemon_is_named_as_such_not_as_a_missing_image(monkeypatch):
    """Pre-review P6: `docker image inspect` fails alike for a missing image and a stopped daemon."""
    provider = DockerProvider()
    asked = []

    async def down(*args, **kwargs):
        asked.append(args[0])
        return 1, b"", b""

    monkeypatch.setattr(provider, "_cli", down)
    with pytest.raises(WorkspaceUnavailable, match="daemon could not be reached"):
        await provider.create(WorkspaceSpec(image=FAKE_IMAGE), {})
    assert asked == ["version"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows' command-line limit (pre-review P4)")
async def test_an_argv_past_the_command_line_limit_is_the_commands_failure_not_dockers():
    provider = DockerProvider(docker="agentsdk-no-such-docker-cli")
    handle = _handle(WorkspaceSpec(image=FAKE_IMAGE))
    provider._spaces[handle.workspace_id] = dk._Space(handle)
    outcome = await provider.run(handle, ["echo", "x" * 40_000], timeout=30)
    assert (outcome.exit_code, outcome.killed) == (126, None) and b"argument list too long" in outcome.stderr
    with pytest.raises(ValueError, match="32,767"):
        await provider._spawn(["inspect", "y" * 40_000])


def test_a_volume_whose_labels_name_no_readable_spec_is_still_listed():
    """Pre-review P5: one foreign or damaged label failed list_owned for every caller."""
    for labels in ({}, {"agentsdk.spec": "{not json"}, {"agentsdk.spec": '{"image": "a:tag"}'},
                   {"agentsdk.spec": '{"cpus": 1.0}'}):
        assert dk._recorded(labels) is dk._UNKNOWN_SPEC
    spec = WorkspaceSpec(image=FAKE_IMAGE, pids=64)
    record = {k: v for k, v in spec.record().items() if k not in ("env", "run_command_approval")}
    assert dk._recorded({"agentsdk.spec": json.dumps(record)}) == spec


async def test_list_workspace_names_a_sub_folders_entries():
    """Pre-review P1: the listing named the folder itself for any sub-folder."""
    async def make(cmd):
        cmd.files.write("sub/x", b"12345")
        cmd.files.write("sub/dir/y", b"1")
        return 0

    tenant = tenancy()
    runner = runner_for(FakeProvider(commands={"make": make}))
    spec = WorkspaceSpec(image=FAKE_IMAGE, run_command_approval=ApprovalPolicy.AUTO)
    result = await run(runner, spec, [[["run_command", {"argv": ["make"]}]],
                                      [["list_workspace", {"path": "sub"}]]], tenant)
    got = await results(runner, result, tenant)
    assert error_types(result)["t1-0"] is None and got["t1-0"].content == "dir\tdir\t0\nx\tfile\t5"


async def test_a_missing_cli_fails_the_workspace_unavailable():
    with pytest.raises(WorkspaceUnavailable, match="docker CLI was not found"):
        await DockerProvider(docker="agentsdk-no-such-docker-cli").create(WorkspaceSpec(image=FAKE_IMAGE), {})


# =================================================================================================
# FR-143: what crosses, and what never does
# =================================================================================================


def _tar(*members) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for info, data in members:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return buffer.getvalue()


def _member(name, kind=tarfile.REGTYPE, data=b"", linkname=""):
    info = tarfile.TarInfo(name)
    info.type, info.linkname = kind, linkname
    if kind == tarfile.REGTYPE:
        info.size = len(data)
        return info, data
    return info, None


@pytest.mark.parametrize("member, named", [
    (_member("out", tarfile.SYMTYPE, linkname="/etc/passwd"), "symlink 'out'"),
    (_member("up", tarfile.SYMTYPE, linkname="../../.."), "symlink 'up'"),
    (_member("twin", tarfile.LNKTYPE, linkname="a.txt"), "hard link 'twin'"),
    (_member("pipe", tarfile.FIFOTYPE), "FIFO 'pipe'"),
    (_member("tty", tarfile.CHRTYPE), "character device 'tty'"),
    (_member("disk", tarfile.BLKTYPE), "block device 'disk'"),
    (_member("/etc/cron.d/job", data=b"x"), "'/etc/cron.d/job'"),
    (_member("../escape.txt", data=b"x"), "'../escape.txt'"),
    (_member("a/../../escape.txt", data=b"x"), "'a/../../escape.txt'"),
])
def test_a_transfer_holding_anything_but_regular_files_and_directories_fails_naming_it(member, named):
    with pytest.raises(ValueError) as caught:
        dk.read_tar(_tar(_member("a.txt", data=b"ok"), member))
    assert named in str(caught.value), str(caught.value)
    with pytest.raises(ValueError):
        dk.import_tar(_tar(_member("a.txt", data=b"ok"), member))


def test_a_sparse_member_a_repeated_path_or_a_file_that_is_also_a_folder_fails_the_transfer():
    """Pre-review P7: a 10 KiB stream naming a 256 MiB sparse file was rebuilt at full size."""
    pax = {"GNU.sparse.major": "1", "GNU.sparse.minor": "0", "GNU.sparse.name": "sparse.bin",
           "GNU.sparse.realsize": str(256 * 2**20)}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        info = tarfile.TarInfo("./PaxHeaders/sparse.bin")
        info.pax_headers = pax
        payload = b"0\n" + b"\0" * 510
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    with pytest.raises(ValueError, match="sparse file 'sparse.bin'"):
        dk.read_tar(buffer.getvalue())
    for members in ([_member("a", data=b"1"), _member("a", data=b"2")],
                    [_member("a", tarfile.DIRTYPE), _member("a", data=b"x")],
                    [_member("a", data=b"x"), _member("a", tarfile.DIRTYPE)],
                    [_member("a", data=b"x"), _member("a/b", data=b"y")],
                    [_member("a/b", data=b"y"), _member("a", data=b"x")]):
        with pytest.raises(ValueError, match="'a"):
            dk.read_tar(_tar(*members))
    dk.read_tar(_tar(_member("a", tarfile.DIRTYPE), _member("a/b", data=b"y"), _member("a/c", tarfile.DIRTYPE)))


def test_regular_files_and_directories_cross():
    data = _tar(_member("notes", tarfile.DIRTYPE), _member("notes/a.txt", data=b"payload"), _member("./b.txt"))
    assert [m.name for m in dk.read_tar(data)] == ["notes", "notes/a.txt", "./b.txt"]


def test_a_transfer_over_16_mib_or_10000_members_fails():
    with pytest.raises(ValueError, match="over 16777216 B"):
        dk.read_tar(b"\0" * (dk.MAX_TRANSFER_BYTES + 1))
    many = _tar(*[_member(f"f{i}") for i in range(dk.MAX_TRANSFER_MEMBERS + 1)])
    assert len(many) < dk.MAX_TRANSFER_BYTES
    with pytest.raises(ValueError, match="over 10000 members"):
        dk.read_tar(many)
    dk.read_tar(_tar(*[_member(f"f{i}") for i in range(dk.MAX_TRANSFER_MEMBERS)]))


class _Endless:
    """A `docker cp` that never stops writing: only a bound applied while it is read ends it."""

    def __init__(self):
        self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        self.stderr.feed_eof()
        self.stdin = type("Stdin", (), {"close": lambda self: None})()
        self.returncode = None
        self.fed = 0
        self._ended = asyncio.Event()
        self._feeding = asyncio.ensure_future(self._feed())

    async def _feed(self):
        while self.returncode is None:
            self.stdout.feed_data(b"\0" * 2**20)
            self.fed += 2**20
            await asyncio.sleep(0)

    def kill(self):
        self.returncode = -9
        self.stdout.feed_eof()
        self._ended.set()

    async def wait(self):
        await self._ended.wait()
        return self.returncode


async def test_an_export_that_never_ends_is_cut_at_its_bound_while_it_is_read(monkeypatch):
    provider = DockerProvider(docker=sys.executable)
    handle = _handle(WorkspaceSpec(image=FAKE_IMAGE))
    provider._spaces[handle.workspace_id] = dk._Space(handle)
    copying = _Endless()

    @__import__("contextlib").asynccontextmanager
    async def helper(_handle):
        yield "helper"

    async def spawn(args, env=None):
        assert args[0] == "cp"
        return copying

    monkeypatch.setattr(provider, "_helper", helper)
    monkeypatch.setattr(provider, "_spawn", spawn)
    with pytest.raises(ValueError, match="over 16777216 B"):
        await asyncio.wait_for(provider.export(handle, "big.bin"), 30)
    assert copying.returncode == -9 and copying.fed < 4 * dk.MAX_TRANSFER_BYTES, copying.fed


def test_what_goes_in_is_rebuilt_under_workspace_owned_by_65534_with_every_parent():
    built = dk.import_tar(_tar(_member("deep/er/a.txt", data=b"payload")))
    with tarfile.open(fileobj=io.BytesIO(built)) as tar:
        members = tar.getmembers()
        assert [(m.name, m.isdir(), m.uid, m.gid) for m in members] == [
            ("workspace/deep", True, 65534, 65534), ("workspace/deep/er", True, 65534, 65534),
            ("workspace/deep/er/a.txt", False, 65534, 65534)]
        assert tar.extractfile(members[-1]).read() == b"payload"


# =================================================================================================
# FR-144: a size that cannot be read is held past the bound (DECISION-504c7f04)
# =================================================================================================


class Unmeasurable(FakeProvider):
    """A provider whose workspace's size can be read until it cannot."""

    def __init__(self):
        async def ok(cmd):
            cmd.out("ran\n")
            return 0

        super().__init__(commands={"ok": ok})
        self.readable = True

    async def size(self, handle):
        if not self.readable:
            raise RuntimeError("du ended with exit code 1")
        return await super().size(handle)


@pytest.mark.parametrize("store", STORES)
async def test_a_workspace_whose_size_cannot_be_read_refuses_every_later_command_and_write(store):
    provider = Unmeasurable()
    tenant = tenancy()
    runner = runner_for(provider, store)
    spec = WorkspaceSpec(image=FAKE_IMAGE, run_command_approval=ApprovalPolicy.AUTO)
    original = provider.run

    async def then_unreadable(handle, argv, **kwargs):
        outcome = await original(handle, argv, **kwargs)
        provider.readable = False
        return outcome

    provider.run = then_unreadable
    result = await run(runner, spec, [[["run_command", {"argv": ["ok"]}]], [["run_command", {"argv": ["ok"]}]],
                                      [["write_workspace_file", {"path": "a.txt", "content": "x"}]]], tenant)
    got, types = await results(runner, result, tenant), error_types(result)
    assert result.status is RunStatus.COMPLETED
    assert types["t0-0"] is None and "ran" in got["t0-0"].content, "the command itself still reports its result"
    assert types["t1-0"] == "WorkspaceRefused" and types["t2-0"] == "WorkspaceRefused", types
    refused = events(result, EventType.WORKSPACE_REFUSED)
    assert [(p["tool"], p["bound"], p["unread"]) for p in refused] == [
        ("run_command", "max_workspace_bytes", "RuntimeError"), ("write_workspace_file", "max_workspace_bytes",
                                                                 "RuntimeError")]
    assert len(events(result, EventType.WORKSPACE_COMMAND_STARTED)) == 1, "a refused command started"


# =================================================================================================
# M38's carried caveats (KNOWLEDGE-1e7b2114)
# =================================================================================================


async def test_removing_a_fake_workspace_kills_its_running_command():
    async def nap(cmd):
        await cmd.sleep(30)
        return 0

    await CONTRACT["removing_a_workspace_kills_its_running_commands"](
        FakeProvider(commands={"nap": nap}), Vocabulary(
            say=None, fail=None, nap=lambda s: ["nap", str(s)], fill=None, flood=None, show=None, echo_stdin=None,
            environ=None, unknown=[]), WorkspaceSpec(image=FAKE_IMAGE))


async def test_example_32_judges_a_rejected_call_by_its_error_class_not_its_text(monkeypatch):
    """A rejection's text is "rejected" or "approval expired", never its class name: keyed on
    text, example 32 counted a rejection as a workspace result and failed it as trusted."""
    example = _example("32_workspace.py")
    example.TENANT = tenancy()

    async def rejecting(runner, run_id, decided):
        while True:
            for view in await runner.interruptions(example.TENANT, example.PROJECT, run_id=run_id):
                await runner.decide(example.TENANT, example.PROJECT, view.interruption.interruption_id,
                                    approve=False, principal=example.OPERATOR, message="not this one")
            await asyncio.sleep(0.05)

    monkeypatch.setattr(example, "operator", rejecting)
    runner = Runner({"scripted": example.ScriptedModel()}, workspace_provider=example.provider())
    checks, _ = await example.demonstrate(runner, "scripted:model", live=True, persisted=False)
    verdicts = dict(checks)
    assert verdicts["every workspace result is untrusted, data-only, executable content"] is True, checks


# =================================================================================================
# FR-145: example 33, offline on FakeProvider in the gates
# =================================================================================================


async def test_example_33_holds_on_both_stores_on_the_fake_provider():
    example = _example("33_docker_workspace.py")
    for store in STORES:
        example.TENANT = tenancy()
        checks = await example.offline(store)
        assert [label for label, passed in checks if not passed] == [] and len(checks) >= 5, (store, checks)


def test_example_33_runs_offline():
    done = subprocess.run([sys.executable, str(REPO / "scripts" / "33_docker_workspace.py"), "--offline"],
                          capture_output=True, text=True, timeout=240, cwd=REPO)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "[FAIL]" not in done.stdout and "[PASS]" in done.stdout, done.stdout


# =================================================================================================
# AC-97, against a running Docker daemon (marked docker: run live before review)
# =================================================================================================


DOCKER_WORDS = Vocabulary(
    say=lambda t: ["echo", t], fail=lambda c: ["sh", "-c", f"echo failed >&2; exit {c}"],
    nap=lambda s: ["sleep", str(s)], fill=lambda p, n: ["sh", "-c", f"head -c {n} /dev/zero > {p}"],
    flood=lambda n: ["head", "-c", str(n), "/dev/zero"], show=lambda p: ["cat", p], echo_stdin=lambda: ["cat"],
    environ=lambda: ["env"], unknown=["no-such-command-xyz"])
CONTRACT_SPEC = WorkspaceSpec(image=IMAGE, max_output_bytes=65536, env={"WS_GREETING": SECRET})


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
            if tenant.startswith(PREFIX) or tenant == "SYN-contract":
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


@pytest.mark.docker
@pytest.mark.parametrize("name", sorted(CONTRACT))
async def test_the_contract_holds_on_docker(docker_daemon, name):
    await CONTRACT[name](DockerProvider(), DOCKER_WORDS, CONTRACT_SPEC)


@pytest.mark.docker
async def test_hardening_observed_from_inside_a_command(docker_daemon):
    spec = WorkspaceSpec(image=IMAGE, memory_bytes=300 * 2**20, cpus=0.5, pids=64, tmp_bytes=8 * 2**20)
    provider = DockerProvider()
    handle = await provider.create(spec, {"agentsdk.tenant": tenancy()})
    try:
        probe = ("id -u; id -g; grep -E '^(CapEff|CapPrm|CapBnd|NoNewPrivs):' /proc/self/status; "
                 "echo net=$(ls /sys/class/net | tr '\\n' ' '); touch /etc/x 2>/dev/null; echo root=$?; "
                 "grep ' /tmp ' /proc/mounts; cat /sys/fs/cgroup/memory.max /sys/fs/cgroup/memory.swap.max "
                 "/sys/fs/cgroup/cpu.max /sys/fs/cgroup/pids.max; touch /workspace/mine; echo workspace=$?")
        outcome = await provider.run(handle, ["sh", "-c", probe], timeout=60)
        lines = outcome.stdout.decode().splitlines()
        assert outcome.exit_code == 0, outcome
        assert lines[0:2] == ["65534", "65534"], lines
        caps = dict(line.split(":\t") for line in lines[2:6])
        assert caps == {"CapEff": "0000000000000000", "CapPrm": "0000000000000000", "CapBnd": "0000000000000000",
                        "NoNewPrivs": "1"}, caps
        assert lines[6].split() == ["net=lo"], lines[6]
        assert lines[7] == "root=1", "the root file system is writable"
        assert "size=8192k" in lines[8], lines[8]
        assert lines[9:13] == [str(300 * 2**20), "0", "50000 100000", "64"], lines[9:13]
        assert lines[13] == "workspace=0", "the workspace is not the command's to write"
    finally:
        await provider.remove(handle)


@pytest.mark.docker
async def test_no_env_value_reaches_a_command_line_or_outlives_its_container(docker_daemon, monkeypatch):
    spec = WorkspaceSpec(image=IMAGE, env={"WS_TOKEN": SECRET})
    seen_args: list[list[str]] = []
    real = asyncio.create_subprocess_exec

    async def recording(*args, **kwargs):
        seen_args.append([str(a) for a in args])
        return await real(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", recording)
    provider = DockerProvider()
    handle = await provider.create(spec, {"agentsdk.tenant": tenancy()})
    try:
        running = asyncio.ensure_future(provider.run(handle, ["sh", "-c", "echo $WS_TOKEN; sleep 4"], timeout=60))
        await asyncio.sleep(2.5)
        [name] = _docker("ps", "--filter", f"label=agentsdk.workspace={handle.workspace_id}",
                         "--format", "{{.Names}}").split()
        inspected = json.loads(_docker("inspect", name))[0]
        outcome = await running
        assert outcome.stdout.decode().strip() == SECRET, "the command did not receive its env value"
        for part in (inspected["Config"]["Cmd"], inspected["Args"], inspected["Config"]["Labels"], inspected["Name"],
                     inspected["Config"]["Entrypoint"]):
            assert SECRET not in json.dumps(part), part
        assert all(SECRET not in " ".join(args) for args in seen_args), "a value reached a docker command line"
        assert _docker("ps", "-a", "--filter", f"name={name}", "-q") == "", "the command's container outlived it"
    finally:
        await provider.remove(handle)


@pytest.mark.docker
async def test_a_link_fifo_or_oversized_export_fails_naming_it(docker_daemon):
    provider = DockerProvider()
    handle = await provider.create(WorkspaceSpec(image=IMAGE), {"agentsdk.tenant": tenancy()})
    try:
        made = await provider.run(handle, ["sh", "-c", (
            "echo data > a.txt; ln -s /etc/passwd abs; ln -s ../../.. rel; mkfifo pipe; "
            "mkdir pair; echo data > pair/first; ln pair/first pair/twin; "
            "mkdir many; i=0; while [ $i -le 10000 ]; do : > many/f$i; i=$((i+1)); done; "
            "head -c 17000000 /dev/zero > big.bin")], timeout=300)
        assert made.exit_code == 0, made
        for path, named in (("abs", "symlink 'abs'"), ("rel", "symlink 'rel'"), ("pipe", "FIFO 'pipe'")):
            with pytest.raises(ValueError) as caught:
                await provider.export(handle, path)
            assert named in str(caught.value), (path, str(caught.value))
        # A hard link is a link member only where its first name is in the same stream.
        with pytest.raises(ValueError, match="hard link 'pair/"):
            await provider.export(handle, "pair")
        with pytest.raises(ValueError, match="over 10000 members"):
            await provider.export(handle, "many")
        started = time.monotonic()
        with pytest.raises(ValueError, match="over 16777216 B"):
            await provider.export(handle, "big.bin")
        assert time.monotonic() - started < 60
        exported = dk.read_tar(await provider.export(handle, "a.txt"))
        assert [m.name for m in exported] == ["a.txt"]
    finally:
        await provider.remove(handle)


@pytest.mark.docker
async def test_a_folder_holding_links_fifos_and_more_than_a_transfer_is_listed_as_it_is(docker_daemon):
    """Pre-review P3: a folder holding a link, a FIFO, over 16 MiB or over 10,000 files could not
    be listed; `list` shows each as what it is (DECISION-0004d2dc)."""
    provider = DockerProvider()
    handle = await provider.create(WorkspaceSpec(image=IMAGE), {"agentsdk.tenant": tenancy()})
    try:
        made = await provider.run(handle, ["sh", "-c", (
            "mkdir many; cd many; head -c 17000000 /dev/zero > big.bin; ln -s /etc etc_link; mkfifo pipe; "
            "mkdir sub; echo hi > .hidden; i=0; while [ $i -lt 10000 ]; do : > f$i; i=$((i+1)); done")],
            timeout=300)
        assert made.exit_code == 0, made
        shown, total = await provider.list(handle, "many", limit=1000)
        assert total == 10_005 and len(shown) == 1000, (total, len(shown))
        everything, _ = await provider.list(handle, "many", limit=20_000)
        kinds = {name: (kind, size) for name, kind, size in everything}
        assert kinds["big.bin"] == ("file", 17_000_000) and kinds["etc_link"] == ("link", 0)
        assert kinds["pipe"] == ("other", 0) and kinds["sub"] == ("dir", 0) and kinds[".hidden"] == ("file", 3)
        with pytest.raises(NotADirectoryError):
            await provider.list(handle, "many/etc_link")
    finally:
        await provider.remove(handle)


@pytest.mark.docker
async def test_a_link_in_a_parent_folder_reaches_only_the_images_files(docker_daemon):
    """DECISION-0004d2dc, declared: docker cp resolves a parent link inside its never-started
    container -- the image's files and the volume, never the host, /proc, /tmp or another workspace."""
    provider = DockerProvider()
    tenant = tenancy()
    other = await provider.create(WorkspaceSpec(image=IMAGE), {"agentsdk.tenant": tenant})
    handle = await provider.create(WorkspaceSpec(image=IMAGE), {"agentsdk.tenant": tenant})
    try:
        await provider.run(other, ["sh", "-c", "echo other-workspace > mine.txt"], timeout=60)
        made = await provider.run(handle, ["sh", "-c", (
            "ln -s /etc e; ln -s /proc p; ln -s /tmp t; ln -s / r; echo in-tmp > /tmp/x")], timeout=60)
        assert made.exit_code == 0, made
        with tarfile.open(fileobj=io.BytesIO(await provider.export(handle, "e/passwd"))) as tar:
            assert tar.extractfile(tar.getmembers()[0]).read().startswith(b"root:x:0:0:"), "not the image's file"
        for path in ("p/self/environ", "p/1/cmdline", "t/x", f"r/var/lib/docker/volumes/agentsdk-{other.workspace_id}"
                     "/_data/mine.txt", "r/mnt/c/Windows/win.ini", "r/run/desktop/mnt/host/c/Windows/win.ini"):
            with pytest.raises((FileNotFoundError, ValueError)):
                await provider.export(handle, path)
    finally:
        await provider.remove(handle)
        await provider.remove(other)


@pytest.mark.docker
async def test_output_past_the_bound_is_kept_to_it_and_marked_cut(docker_daemon):
    provider = DockerProvider()
    handle = await provider.create(WorkspaceSpec(image=IMAGE), {"agentsdk.tenant": tenancy()})
    try:
        outcome = await provider.run(handle, ["head", "-c", "20000000", "/dev/zero"], timeout=120)
        assert (outcome.exit_code, outcome.stdout_cut, outcome.stdout_bytes) == (0, True, 20_000_000)
        assert len(outcome.stdout) == WorkspaceSpec(image=IMAGE).max_output_bytes
    finally:
        await provider.remove(handle)


@pytest.mark.docker
async def test_a_command_past_its_memory_ends_killed_by_the_limit(docker_daemon):
    provider = DockerProvider()
    handle = await provider.create(WorkspaceSpec(image=IMAGE, memory_bytes=64 * 2**20), {"agentsdk.tenant": tenancy()})
    try:
        outcome = await provider.run(handle, ["python", "-c", "a = []\nwhile True: a.append(bytearray(2**20))"],
                                     timeout=60)
        assert (outcome.exit_code, outcome.killed) == (None, "limit"), outcome
    finally:
        await provider.remove(handle)


@pytest.mark.docker
@pytest.mark.parametrize("store", STORES)
async def test_a_write_past_the_disk_bound_refuses_the_next_command_and_records_the_overshoot(docker_daemon, store):
    tenant = tenancy()
    runner = runner_for(DockerProvider(), store)
    spec = WorkspaceSpec(image=IMAGE, max_workspace_bytes=2**20, run_command_approval=ApprovalPolicy.AUTO)
    result = await run(runner, spec, [[["run_command", {"argv": ["sh", "-c", "head -c 3000000 /dev/zero > big"]}]],
                                      [["run_command", {"argv": ["echo", "after"]}]]], tenant)
    types = error_types(result)
    assert result.status is RunStatus.COMPLETED
    assert types == {"t0-0": None, "t1-0": "WorkspaceRefused"}, types
    [refused] = events(result, EventType.WORKSPACE_REFUSED)
    assert refused["bound"] == "max_workspace_bytes" and refused["size"] > refused["limit"] == 2**20, refused
    assert events(result, EventType.WORKSPACE_REMOVED) and _ours() == ([], [])


@pytest.mark.docker
async def test_a_folder_the_measurement_cannot_read_refuses_the_next_command(docker_daemon):
    tenant = tenancy()
    runner = runner_for(DockerProvider())
    spec = WorkspaceSpec(image=IMAGE, run_command_approval=ApprovalPolicy.AUTO)
    result = await run(runner, spec, [[["run_command", {"argv": ["sh", "-c", "mkdir hide; head -c 4096 /dev/zero"
                                                                               " > hide/f; chmod 000 hide"]}]],
                                      [["run_command", {"argv": ["echo", "after"]}]]], tenant)
    types = error_types(result)
    assert types == {"t0-0": None, "t1-0": "WorkspaceRefused"}, types
    [refused] = events(result, EventType.WORKSPACE_REFUSED)
    assert refused["unread"] == "WorkspaceUnavailable" and refused["size"] is None, refused


@pytest.mark.docker
async def test_example_33_runs_on_docker_with_the_scripted_model(docker_daemon):
    example = _example("33_docker_workspace.py")
    for store in STORES:
        example.TENANT = tenancy()
        checks = await example.offline(store, provider_name="docker")
        assert [label for label, passed in checks if not passed] == [] and len(checks) >= 8, (store, checks)
