"""M38 gate: the workspace contract, the fake provider and the tools (FR-136 to FR-141, FR-146,
FR-147, AC-95, AC-96, NFR-40 to NFR-42).

A run whose agent has a `WorkspaceSpec` is given a workspace the first time one of its workspace
tools needs it, through the Runner's provider -- here `FakeProvider`, which runs no host process
and no model-written code (DECISION-31b2c79d). Every command and workspace is recorded on both
stores, every result is untrusted, and `run_command` waits for approval by default (P5-D10).
M37's two carried caveats are settled first (KNOWLEDGE-d9ea0d40, DECISION-97fb116b).

Store calls from async tests go to a worker thread (FR-81); the persisted half removes every row
it writes, by the run ids of its own tenancies.
"""

from __future__ import annotations

import asyncio
import json
import math
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
from agentsdk.identity import PrincipalContext
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.postgres import RunScope
from agentsdk.primitives import InstructionAuthority, Message, Origin, Role, TaintFlag, ToolCall, TrustZone
from agentsdk.tools import ApprovalPolicy, RiskClass, Tool, ToolSpec
from agentsdk.workspace import CommandOutcome, FakeProvider, WorkspaceSpec

from tests.workspace_contract import CONTRACT, Vocabulary

REPO = Path(__file__).resolve().parent.parent
DSN = normalise_database_url(os.environ.get("DATABASE_URL"))
PREFIX = "SYN-m38-"
PROJECT = "p-m38"
STORES = ["memory", "postgres"]
IMAGE = "fake/workspace@sha256:" + "0" * 64
SECRET = "workspace-secret-" + uuid.uuid4().hex
APPROVER = PrincipalContext(agent_principal="approval-ui", user_principal="owner")
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


# =================================================================================================
# The fake provider's commands, as the contract spells them
# =================================================================================================


async def _say(cmd):
    cmd.out(" ".join(cmd.argv[1:]) + "\n")
    return 0


async def _fail(cmd):
    cmd.err("failed\n")
    return int(cmd.argv[1])


async def _nap(cmd):
    await cmd.sleep(float(cmd.argv[1]))
    return 0


async def _fill(cmd):
    cmd.files.write(cmd.argv[1], b"x" * int(cmd.argv[2]))
    return 0


async def _flood(cmd):
    cmd.out(b"y" * int(cmd.argv[1]))
    return 0


async def _show(cmd):
    cmd.out(cmd.files.read(cmd.argv[1]))
    return 0


async def _echo_stdin(cmd):
    cmd.out(cmd.stdin or b"")
    return 0


async def _environ(cmd):
    cmd.out("".join(f"{k}={v}\n" for k, v in sorted(cmd.env.items())))
    return 0


async def _stubborn(cmd):
    """Ignores cancellation, then writes: a kill must leave nothing it does afterwards."""
    for _ in range(100):
        try:
            await cmd.sleep(0.05)
        except asyncio.CancelledError:
            continue
    cmd.files.write("late.txt", b"written after the kill")
    return 0


FAKE_COMMANDS = {"say": _say, "fail": _fail, "nap": _nap, "fill": _fill, "flood": _flood, "show": _show,
                 "echo-stdin": _echo_stdin, "environ": _environ, "stubborn": _stubborn}
FAKE_WORDS = Vocabulary(
    say=lambda text: ["say", text], fail=lambda code: ["fail", str(code)], nap=lambda s: ["nap", str(s)],
    fill=lambda path, n: ["fill", path, str(n)], flood=lambda n: ["flood", str(n)], show=lambda p: ["show", p],
    echo_stdin=lambda: ["echo-stdin"], environ=lambda: ["environ"], unknown=["no-such-command"])


class Counting(FakeProvider):
    """A FakeProvider that counts what reached it."""

    def __init__(self, **kwargs):
        super().__init__(commands=FAKE_COMMANDS, **kwargs)
        self.created, self.ran = [], []

    async def create(self, spec, labels):
        handle = await super().create(spec, labels)
        self.created.append((handle, dict(labels)))
        return handle

    async def run(self, handle, argv, **kwargs):
        self.ran.append((handle.workspace_id, list(argv)))
        return await super().run(handle, argv, **kwargs)


# =================================================================================================
# The runs
# =================================================================================================


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


def workspace(**fields):
    fields.setdefault("run_command_approval", ApprovalPolicy.AUTO)
    return WorkspaceSpec(image=IMAGE, **fields)


def agent(*profile, space=None, **fields):
    return AgentSpec(id="coder", instructions="use the workspace", preferred_model="m:x",
                     tool_profile=tuple(profile) or TOOLS, workspace=space, **fields)


def runner_for(store="memory", provider=None, model=None, **fields):
    return Runner({"m": model or Turns()}, workspace_provider=provider, **fields,
                  persistence=Persistence.postgres(DSN, create_schema=False) if store == "postgres" else None)


async def run(runner, spec, turns, tenant=None, **fields):
    return await runner.run(spec, json.dumps(turns), RunConfig(tenant_id=tenant or tenancy(), project_id=PROJECT,
                                                               **fields))


async def results(runner, result, tenant):
    sessions = runner._sessions_for(RunScope(run_id=result.run_id, tenant_id=tenant, project_id=PROJECT))
    history = await asyncio.to_thread(sessions.history, result.run_id)
    return {r.tool_call_id: r for m in history for r in m.tool_results}


def events(result, kind):
    return [e for e in result.events if e.event_type is kind]


def kinds(result):
    return [e.event_type for e in result.events]


async def asked(runner, tenant, run_id, timeout=30.0):
    deadline = time.monotonic() + timeout
    while True:
        views = await runner.interruptions(tenant, PROJECT, run_id=run_id)
        if views:
            return views
        assert time.monotonic() < deadline, "no approval was asked"
        await asyncio.sleep(0.05)


def stored(sql, *args):
    with psycopg.connect(DSN) as conn:
        return conn.execute(sql, args).fetchall()


# =================================================================================================
# FR-136, AC-95: M37's carried caveats in example 31
# =================================================================================================


def _example(name: str):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), REPO / "scripts" / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _verdicts(checks) -> dict:
    return {label: (passed, why) for label, passed, why in checks}


async def test_a_result_whose_id_was_displaced_fails_names_live_and_the_barrier_outlasts_the_hold(monkeypatch):
    """KNOWLEDGE-d9ea0d40 caveat 1: an SDK that wraps a server's text -- so its execution id is no
    longer at the start -- made every result unjudgeable, and live NAMES passed, since the clause
    catching an execution whose result reached no call ran only strictly. It runs live now, for
    every run not cancelled (DECISION-97fb116b). Caveat 2, read off the tools the Runner was given:
    the barrier's timeout outlasts HOLD_BOUND, and the wait tool's WAIT_BOUND."""
    import agentsdk.mcp as mcp

    example = _example("31_mcp_contention.py")
    real = mcp.ToolOutput
    monkeypatch.setattr(mcp, "ToolOutput", lambda *, content, source_uri: real(content="note: " + content,
                                                                               source_uri=source_uri))
    given = {}
    real_runner = example.Runner

    def capturing(*args, **kwargs):
        given.update({t.name: t for t in kwargs.get("tools", ())})
        return real_runner(*args, **kwargs)

    monkeypatch.setattr(example, "Runner", capturing)
    checks, _ = await example.offline("memory", seed=0, quiet=True, tenant=tenancy(), project=PROJECT, strict=False)
    passed, why = _verdicts(checks)[example.NAMES]
    assert passed is False and "reached no call" in why, checks
    assert given["barrier"].spec.timeout_seconds > example.HOLD_BOUND, given["barrier"].spec.timeout_seconds
    assert given["wait"].spec.timeout_seconds > example.WAIT_BOUND, given["wait"].spec.timeout_seconds


async def test_an_execution_whose_call_timed_out_is_no_lost_result_live(monkeypatch):
    """DECISION-97fb116b: a call that timed out ends without its server's result, so its execution
    reached no call; counted against that tool's cut-short calls, it fails nothing live."""
    import agentsdk.mcp as mcp

    example = _example("31_mcp_contention.py")
    real = mcp.ToolOutput

    def built(*, content, source_uri):
        if source_uri == "mcp://shadow/lookup":
            raise TimeoutError
        return real(content=content, source_uri=source_uri)

    monkeypatch.setattr(mcp, "ToolOutput", built)
    checks, _ = await example.offline("memory", seed=0, quiet=True, tenant=tenancy(), project=PROJECT, strict=False)
    passed, why = _verdicts(checks)[example.NAMES]
    assert passed is not False, why


async def test_a_timed_out_call_does_not_cover_another_tools_lost_result(monkeypatch):
    """The count is per qualified tool: shadow's timed-out lookup leaves no slot for catalog's
    lookup, whose id was displaced."""
    import agentsdk.mcp as mcp

    example = _example("31_mcp_contention.py")
    real = mcp.ToolOutput

    def built(*, content, source_uri):
        if source_uri == "mcp://shadow/lookup":
            raise TimeoutError
        if source_uri == "mcp://catalog/lookup":
            content = "note: " + content
        return real(content=content, source_uri=source_uri)

    monkeypatch.setattr(mcp, "ToolOutput", built)
    checks, _ = await example.offline("memory", seed=0, quiet=True, tenant=tenancy(), project=PROJECT, strict=False)
    passed, why = _verdicts(checks)[example.NAMES]
    assert passed is False and "catalog__lookup" in why, checks


# =================================================================================================
# FR-137, AC-96: WorkspaceSpec
# =================================================================================================


def test_a_spec_has_the_documented_defaults():
    spec = WorkspaceSpec(image=IMAGE)
    assert (spec.memory_bytes, spec.cpus, spec.pids, spec.tmp_bytes) == (512 * 2**20, 1.0, 128, 64 * 2**20)
    assert (spec.max_workspace_bytes, spec.max_command_seconds, spec.max_output_bytes) == (2**30, 120, 2**20)
    assert (dict(spec.env), spec.scope, spec.keep) == ({}, "run", False)
    assert spec.run_command_approval is ApprovalPolicy.REQUIRE_APPROVAL


@pytest.mark.parametrize("image", [
    "python:3.12-alpine",  # a tag alone
    "python",
    "python@sha256:" + "0" * 63,
    "python@sha256:" + "G" * 64,
    "python@sha512:" + "0" * 64,
    "@sha256:" + "0" * 64,
    "",
    None,
])
def test_an_image_not_pinned_by_digest_is_refused(image):
    with pytest.raises(ValueError):
        WorkspaceSpec(image=image)


LIMITS = ["memory_bytes", "cpus", "pids", "tmp_bytes", "max_workspace_bytes", "max_command_seconds",
          "max_output_bytes"]


@pytest.mark.parametrize("field", LIMITS)
@pytest.mark.parametrize("value", [0, -1, None, True, math.inf, math.nan, "1", 10**15])
def test_a_limit_at_zero_over_its_maximum_or_switched_off_is_refused(field, value):
    with pytest.raises(ValueError):
        WorkspaceSpec(image=IMAGE, **{field: value})


@pytest.mark.parametrize("env", [
    {"BAD NAME": "x"}, {"1ST": "x"}, {"": "x"}, {"NAME": 1}, {"NAME": "nul\x00"}, {"NAME": "\ud800"}, ["NAME"],
])
def test_an_env_the_store_or_a_shell_could_not_take_is_refused(env):
    with pytest.raises(ValueError):
        WorkspaceSpec(image=IMAGE, env=env)


@pytest.mark.parametrize("fields", [{"scope": "session"}, {"keep": "yes"}, {"run_command_approval": "never"}])
def test_an_unknown_scope_a_non_bool_keep_or_an_unknown_approval_is_refused(fields):
    with pytest.raises(ValueError):
        WorkspaceSpec(image=IMAGE, **fields)


def test_a_spec_never_shows_its_env_values():
    spec = WorkspaceSpec(image=IMAGE, env={"TOKEN": SECRET})
    assert SECRET not in repr(spec) and SECRET not in json.dumps(spec.record())
    assert spec.record()["env"] == ["TOKEN"]


def test_an_agent_spec_takes_only_a_workspace_spec():
    with pytest.raises((TypeError, ValueError)):
        agent(space={"image": IMAGE})


def test_a_workspace_leaves_the_agent_spec_hash_unchanged():
    from agentsdk.manifest import agent_spec_hash

    plain, spaced = agent(), agent(space=workspace())
    assert agent_spec_hash(plain.id, plain.instructions, plain.tool_profile) == agent_spec_hash(
        spaced.id, spaced.instructions, spaced.tool_profile)


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "4"])
def test_a_runner_refuses_a_bad_max_concurrent_workspaces(bad):
    with pytest.raises(ValueError):
        Runner({"m": Turns()}, workspace_provider=FakeProvider(), max_concurrent_workspaces=bad)


@pytest.mark.parametrize("name", [*TOOLS, "export_workspace_file"])
def test_a_registered_tool_named_as_a_workspace_tool_is_refused_beside_a_provider(name):
    """A registered tool runs on the host: none may take a workspace tool's name, or an agent's
    `run_command` could run outside its sandbox."""
    clash = Tool(spec=ToolSpec(name=name, description="host", input_schema={"type": "object"}), fn=lambda: "host")
    with pytest.raises(ValueError):
        Runner({"m": Turns()}, tools=[clash], workspace_provider=FakeProvider())


# =================================================================================================
# FR-139, AC-96: the contract, on FakeProvider
# =================================================================================================


@pytest.mark.parametrize("name", sorted(CONTRACT))
async def test_the_fake_provider_keeps_the_contract(name):
    await CONTRACT[name](FakeProvider(commands=FAKE_COMMANDS), FAKE_WORDS,
                         WorkspaceSpec(image=IMAGE, max_output_bytes=1024, env={"GREETING": "hi"}))


async def test_the_fake_provider_runs_no_host_process(monkeypatch):
    def refused(*args, **kwargs):
        raise AssertionError("FakeProvider started a host process")

    monkeypatch.setattr(subprocess, "Popen", refused)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", refused)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", refused)
    for name in sorted(CONTRACT):
        await CONTRACT[name](FakeProvider(commands=FAKE_COMMANDS), FAKE_WORDS,
                             WorkspaceSpec(image=IMAGE, max_output_bytes=1024, env={"GREETING": "hi"}))


async def test_a_killed_fake_command_that_ignores_cancellation_changes_nothing_after():
    """A kill is total (P5-D5): a command that swallows its cancellation is abandoned, and what it
    does afterwards reaches no workspace."""
    provider = FakeProvider(commands=FAKE_COMMANDS)
    handle = await provider.create(WorkspaceSpec(image=IMAGE), {})
    outcome = await provider.run(handle, ["stubborn"], timeout=0.2)
    assert outcome.killed == "timeout"
    await asyncio.sleep(6)
    assert await provider.size(handle) == 0
    await provider.remove(handle)


async def test_a_fake_command_over_a_limit_is_killed_for_it():
    async def greedy(cmd):
        cmd.limit("memory_bytes")
        return 0

    provider = FakeProvider(commands={"greedy": greedy})
    handle = await provider.create(WorkspaceSpec(image=IMAGE), {})
    outcome = await provider.run(handle, ["greedy"], timeout=5)
    assert (outcome.exit_code, outcome.killed) == (None, "limit")
    await provider.remove(handle)


# =================================================================================================
# FR-138, FR-146, AC-96: the manager and the tools in a run
# =================================================================================================


async def test_a_workspace_is_created_at_its_runs_first_need_and_never_before():
    provider = Counting()
    runner = runner_for(provider=provider)
    idle = await run(runner, agent(space=workspace()), [])
    assert idle.status is RunStatus.COMPLETED and provider.created == []
    assert EventType.WORKSPACE_CREATED not in kinds(idle)
    busy = await run(runner, agent(space=workspace()), [[["run_command", {"argv": ["say", "hi"]}]]])
    assert busy.status is RunStatus.COMPLETED, busy.error
    assert len(provider.created) == 1
    order = kinds(busy)
    assert order.index(EventType.WORKSPACE_CREATED) < order.index(EventType.WORKSPACE_COMMAND_STARTED)
    assert order.count(EventType.WORKSPACE_CREATED) == 1


async def test_runs_never_share_a_workspace_whatever_their_tenancy():
    provider = Counting()
    runner = runner_for(provider=provider)
    tenant = tenancy()
    writer = await run(runner, agent(space=workspace()), [[["write_workspace_file", {"path": "a.txt",
                                                                                    "content": "mine"}]]], tenant)
    for other in (tenant, tenancy()):  # the same tenancy, then another
        reader = await run(runner, agent(space=workspace()), [[["read_workspace_file", {"path": "a.txt"}]]], other)
        got = await results(runner, reader, other)
        assert got["t0-0"].is_error and "mine" not in got["t0-0"].content, got["t0-0"].content
    ids = {h.workspace_id for h, _ in provider.created}
    assert len(ids) == len(provider.created) == 3
    first = provider.created[0][1]
    assert (first["agentsdk.tenant"], first["agentsdk.project"], first["agentsdk.run"]) == (
        tenant, PROJECT, writer.run_id)
    assert first["agentsdk.workspace"] == provider.created[0][0].workspace_id


@pytest.mark.parametrize("store", STORES)
async def test_a_run_with_a_workspace_and_no_provider_fails_before_its_first_model_call(store):
    model = Turns()
    runner = runner_for(store, provider=None, model=model)
    result = await run(runner, agent(space=workspace()), [[["run_command", {"argv": ["say", "hi"]}]]])
    assert result.status is RunStatus.FAILED and result.error.startswith("WorkspaceUnavailable"), result.error
    assert model.requests == []


async def test_the_tools_are_offered_only_to_a_run_with_a_workspace_whose_profile_names_them():
    model = Turns()
    runner = runner_for(provider=Counting(), model=model)
    await run(runner, agent("run_command", space=workspace()), [])
    await run(runner, agent(*TOOLS), [])  # named, but no workspace
    offered = [{s["function"]["name"] for s in r.tools or ()} for r in model.requests]
    assert offered == [{"run_command"}, set()], offered


async def test_a_workspace_tool_called_without_a_workspace_is_unknown():
    tenant = tenancy()
    runner = runner_for(provider=Counting())
    result = await run(runner, agent(*TOOLS), [[["run_command", {"argv": ["say", "hi"]}]]], tenant)
    got = await results(runner, result, tenant)
    assert got["t0-0"].is_error and got["t0-0"].content.startswith("ToolNotFound"), got["t0-0"].content


@pytest.mark.parametrize("path", ["/etc/passwd", "../outside", "a/../../b", "a/..", "..", "C:/x", "a\\b", "",
                                  "a\x00b", "/workspace/a",
                                  # Pre-review D1: a newline hid a `..` from the pattern's `.`, and `$`
                                  # admits one at the end.
                                  "a\n../../etc", "\n/..", "x/\n/../..", "a\n", "a\r/../.."])
async def test_a_path_outside_the_workspace_is_refused_at_step_2(path):
    tenant = tenancy()
    provider = Counting()
    runner = runner_for(provider=provider)
    calls = [["read_workspace_file", {"path": path}], ["write_workspace_file", {"path": path, "content": "x"}],
             ["list_workspace", {"path": path}]]
    result = await run(runner, agent(space=workspace()), [calls], tenant)
    got = await results(runner, result, tenant)
    for index in range(3):
        assert got[f"t0-{index}"].content.startswith("ToolValidationError"), (path, got[f"t0-{index}"].content)
    assert provider.created == [], "a refused path reached the provider"


@pytest.mark.parametrize("arguments", [{"argv": []}, {"argv": "say hi"}, {"argv": ["say", 1]}, {},
                                       {"argv": ["say"], "timeout_seconds": 0},
                                       {"argv": ["say"], "timeout_seconds": 121},
                                       {"argv": ["say\x00"]}, {"argv": ["say", "a\x00b"]}])
async def test_run_command_refuses_arguments_outside_its_schema_at_step_2(arguments):
    tenant = tenancy()
    provider = Counting()
    runner = runner_for(provider=provider)
    result = await run(runner, agent(space=workspace()), [[["run_command", arguments]]], tenant)
    got = await results(runner, result, tenant)
    assert got["t0-0"].content.startswith("ToolValidationError"), got["t0-0"].content
    assert provider.ran == []


async def test_the_tools_do_what_they_say():
    tenant = tenancy()
    runner = runner_for(provider=Counting())
    turns = [[["write_workspace_file", {"path": "src/hello.txt", "content": "hello, workspace"}]],
             [["read_workspace_file", {"path": "src/hello.txt"}], ["list_workspace", {"path": "src"}],
              ["run_command", {"argv": ["show", "src/hello.txt"]}], ["run_command", {"argv": ["fail", "4"]}],
              ["run_command", {"argv": ["echo-stdin"], "stdin": "piped"}]]]
    result = await run(runner, agent(space=workspace()), turns, tenant)
    assert result.status is RunStatus.COMPLETED, result.error
    got = await results(runner, result, tenant)
    assert got["t1-0"].content == "hello, workspace"
    assert "hello.txt" in got["t1-1"].content and "16" in got["t1-1"].content
    shown, failed, piped = got["t1-2"].content, got["t1-3"].content, got["t1-4"].content
    assert "exit code: 0" in shown and "hello, workspace" in shown
    # A non-zero exit is the command's result, not the tool's failure: the model reads it.
    assert not got["t1-3"].is_error and "exit code: 4" in failed and "failed" in failed
    assert "piped" in piped


async def test_every_workspace_result_and_error_is_untrusted_executable_data():
    tenant = tenancy()
    runner = runner_for(provider=Counting())
    turns = [[["write_workspace_file", {"path": "a.txt", "content": "x"}], ["read_workspace_file", {"path": "a.txt"}],
              ["list_workspace", {}], ["run_command", {"argv": ["say", "hi"]}],
              ["run_command", {"argv": ["no-such-command"]}], ["read_workspace_file", {"path": "missing.txt"}]]]
    result = await run(runner, agent(space=workspace()), turns, tenant)
    got = await results(runner, result, tenant)
    assert got["t0-5"].is_error, "reading a missing file is an error"
    for key, r in got.items():
        p = r.provenance
        assert (p.origin, p.trust_zone, p.instruction_authority) == (
            Origin.INTERNAL_TOOL, TrustZone.UNTRUSTED, InstructionAuthority.DATA_ONLY), (key, p)
        assert {TaintFlag.EXTERNAL_CONTENT, TaintFlag.EXECUTABLE_CONTENT} <= set(p.taint_flags), (key, p)


def test_run_commands_schema_refuses_a_nul_in_any_argument():
    """Pre-review D10: no process can be given a NUL. A run's arguments are refused earlier still,
    as unstorable; the schema holds it on its own, for any caller of the spec."""
    import jsonschema

    from agentsdk.workspace.tools import specs

    schema = specs(WorkspaceSpec(image=IMAGE))["run_command"].input_schema
    jsonschema.validate({"argv": ["say", ""]}, schema)
    for argv in (["say\x00"], ["say", "a\x00b"]):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({"argv": argv}, schema)


def test_the_tool_specs_declare_their_risk():
    from agentsdk.workspace.tools import workspace_tools

    specs = {t.spec.name: t.spec for t in workspace_tools(None, WorkspaceSpec(image=IMAGE), TOOLS)}
    assert specs["run_command"].risk_class is RiskClass.DESTRUCTIVE
    assert specs["write_workspace_file"].risk_class is RiskClass.WRITE
    assert specs["read_workspace_file"].risk_class is RiskClass.READ_ONLY
    assert specs["list_workspace"].risk_class is RiskClass.READ_ONLY
    assert specs["run_command"].approval_policy is ApprovalPolicy.REQUIRE_APPROVAL
    assert all(s.approval_policy is ApprovalPolicy.AUTO for n, s in specs.items() if n != "run_command")
    # The executor's deadline never ends a command before the command's own timeout does.
    assert specs["run_command"].timeout_seconds > WorkspaceSpec(image=IMAGE).max_command_seconds


async def test_a_command_sees_the_specs_env_and_nothing_of_the_hosts(monkeypatch):
    monkeypatch.setenv("HOST_ONLY_SECRET", SECRET)
    tenant = tenancy()
    runner = runner_for(provider=Counting())
    result = await run(runner, agent(space=workspace(env={"GREETING": "hi"})),
                       [[["run_command", {"argv": ["environ"]}]]], tenant)
    content = (await results(runner, result, tenant))["t0-0"].content
    assert "GREETING=hi" in content and SECRET not in content and "HOST_ONLY_SECRET" not in content


async def test_past_max_workspace_bytes_the_workspace_refuses_commands_and_writes():
    tenant = tenancy()
    provider = Counting()
    runner = runner_for(provider=provider)
    turns = [[["run_command", {"argv": ["fill", "big.bin", "5000"]}]],
             [["run_command", {"argv": ["say", "after"]}]],
             [["write_workspace_file", {"path": "b.txt", "content": "x"}]]]
    result = await run(runner, agent(space=workspace(max_workspace_bytes=4096)), turns, tenant)
    got = await results(runner, result, tenant)
    assert "exit code: 0" in got["t0-0"].content, "the command that overshot still reports its own result"
    for key in ("t1-0", "t2-0"):
        assert got[key].is_error and got[key].content.startswith("WorkspaceRefused"), got[key].content
        assert "max_workspace_bytes" in got[key].content
    assert [argv for _, argv in provider.ran] == [["fill", "big.bin", "5000"]], "a refused command reached it"
    refused = events(result, EventType.WORKSPACE_REFUSED)
    assert refused and all(e.payload["bound"] == "max_workspace_bytes" for e in refused)
    assert refused[0].payload["size"] >= 5000 and refused[0].payload["limit"] == 4096


async def test_a_command_past_its_timeout_reports_it_to_the_model():
    tenant = tenancy()
    runner = runner_for(provider=Counting())
    result = await run(runner, agent(space=workspace()), [[["run_command", {"argv": ["nap", "30"],
                                                                            "timeout_seconds": 0.5}]]], tenant)
    content = (await results(runner, result, tenant))["t0-0"].content
    assert "killed: timeout" in content, content


async def test_output_is_cut_at_max_output_bytes_and_marked():
    tenant = tenancy()
    runner = runner_for(provider=Counting())
    result = await run(runner, agent(space=workspace(max_output_bytes=1000)),
                       [[["run_command", {"argv": ["flood", "5000"]}]]], tenant)
    content = (await results(runner, result, tenant))["t0-0"].content
    assert content.count("y") == 1000 and "cut" in content, content


async def test_the_workspace_is_removed_when_its_run_ends_before_the_runs_last_event():
    provider = Counting()
    runner = runner_for(provider=provider)
    result = await run(runner, agent(space=workspace()), [[["run_command", {"argv": ["say", "hi"]}]]])
    order = kinds(result)
    assert order.index(EventType.WORKSPACE_REMOVED) < len(order) - 1
    assert order[-1] is EventType.RUN_COMPLETED
    assert await provider.list_owned({}) == []
    removed = events(result, EventType.WORKSPACE_REMOVED)[0].payload
    assert removed["reason"] == "run_ended"


async def test_a_failed_runs_workspace_is_removed_too():
    class Failing(Turns):
        async def send(self, request):
            if any(m.role is Role.TOOL for m in request.messages):
                raise RuntimeError("the model broke")
            return await super().send(request)

    provider = Counting()
    runner = runner_for(provider=provider, model=Failing())
    result = await run(runner, agent(space=workspace()), [[["run_command", {"argv": ["say", "hi"]}]]])
    assert result.status is RunStatus.FAILED
    assert EventType.WORKSPACE_REMOVED in kinds(result) and await provider.list_owned({}) == []


async def test_a_scope_or_keep_m41_brings_is_served():
    """FR-151 and FR-152 arrived in M41: a run asking for a plan workspace or a kept one runs; until
    then it failed before its first model call (tests/test_workspace_plan.py holds their lifetimes)."""
    for space in (workspace(scope="plan"), workspace(keep=True)):
        model = Turns()
        result = await run(runner_for(provider=Counting(), model=model), agent(space=space), [])
        assert result.status is RunStatus.COMPLETED, result.error
        assert model.requests != []


# =================================================================================================
# The pre-review's findings (M38): each repair, held
# =================================================================================================


class Slow(Counting):
    """Removal takes a second, and says when it begins."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.removing = asyncio.Event()

    async def remove(self, handle):
        self.removing.set()
        await asyncio.sleep(1.0)
        await super().remove(handle)


@pytest.mark.parametrize("store", STORES)
async def test_a_cancel_during_the_removal_waits_for_it_and_its_record(store):
    """D2: a cancellation that arrived while the workspace was being removed, on the completion
    path, abandoned the removal, its row and its event: the workspace leaked unrecorded."""
    tenant = tenancy()
    provider = Slow()
    runner = runner_for(store, provider=provider)
    handle = await runner.start(agent(space=workspace()), json.dumps([[["run_command", {"argv": ["say", "x"]}]]]),
                                RunConfig(tenant_id=tenant, project_id=PROJECT))
    await asyncio.wait_for(provider.removing.wait(), 30)
    handle.cancel()
    result = await handle.result()
    assert await provider.list_owned({}) == [], "the workspace leaked"
    order = kinds(result)
    assert EventType.WORKSPACE_REMOVED in order and order[-1] in (EventType.RUN_CANCELLED, EventType.RUN_COMPLETED)
    assert order.index(EventType.WORKSPACE_REMOVED) < len(order) - 1
    space = (await asyncio.to_thread(_records, runner, store, tenant, result.run_id))[0][0]
    assert space["removed_reason"] == "run_ended" and space["removed_at"] is not None


@pytest.mark.parametrize("store", STORES)
async def test_a_command_whose_run_is_cancelled_is_recorded_cancelled(store):
    """D3: a command cut short by its run's cancellation left no row and no WorkspaceCommandEnded."""
    tenant = tenancy()
    provider = Counting()
    runner = runner_for(store, provider=provider)
    handle = await runner.start(agent(space=workspace()), json.dumps([[["run_command", {"argv": ["nap", "30"]}]]]),
                                RunConfig(tenant_id=tenant, project_id=PROJECT))
    deadline = time.monotonic() + 30
    while not provider.ran:
        assert time.monotonic() < deadline
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.2)
    handle.cancel()
    result = await handle.result()
    assert result.status is RunStatus.CANCELLED
    ended = events(result, EventType.WORKSPACE_COMMAND_ENDED)
    assert len(ended) == 1 and ended[0].payload["killed"] == "cancelled" and ended[0].payload["exit_code"] is None
    commands = (await asyncio.to_thread(_records, runner, store, tenant, result.run_id))[1]
    assert [(c["argv"], c["exit_code"], c["killed"]) for c in commands] == [(["nap", "30"], None, "cancelled")]
    order = kinds(result)
    assert order.index(EventType.WORKSPACE_COMMAND_ENDED) < order.index(EventType.WORKSPACE_REMOVED) < len(order) - 1


async def test_a_workspace_whose_row_cannot_be_written_runs_nothing(monkeypatch):
    """D4: the handle was kept before its row was written, so a failed write left a workspace that
    ran commands with no record. Now it is removed, the call fails WorkspaceUnavailable, and the
    next need creates one that is recorded."""
    from agentsdk.workspace.records import InMemoryWorkspaceStore

    real, failures = InMemoryWorkspaceStore.record_workspace, []

    def once(self, record):
        if not failures:
            failures.append(record.workspace_id)
            raise RuntimeError("the store refused")
        return real(self, record)

    monkeypatch.setattr(InMemoryWorkspaceStore, "record_workspace", once)
    tenant = tenancy()
    provider = Counting()
    runner = runner_for(provider=provider)
    result = await run(runner, agent(space=workspace()), [[["run_command", {"argv": ["say", "first"]}]],
                                                          [["run_command", {"argv": ["say", "second"]}]]], tenant)
    got = await results(runner, result, tenant)
    assert got["t0-0"].is_error and "the workspace could not be recorded" in got["t0-0"].content, got["t0-0"].content
    assert [argv for _, argv in provider.ran] == [["say", "second"]], "a command ran in an unrecorded workspace"
    spaces, commands = runner._workspace_store().workspaces(tenant, PROJECT), runner._workspace_store().commands(
        tenant, PROJECT)
    assert failures[0] not in {w.workspace_id for w in spaces}
    assert {c.workspace_id for c in commands} <= {w.workspace_id for w in spaces} and len(commands) == 1


async def test_a_command_whose_row_cannot_be_written_is_told_ran_and_still_measured(monkeypatch):
    """D4: a failed command row hid that the command ran, skipped its end event, and skipped the
    measurement, so the disk bound was not applied."""
    from agentsdk.workspace.records import InMemoryWorkspaceStore

    def refused(self, record):
        raise RuntimeError("the store refused")

    monkeypatch.setattr(InMemoryWorkspaceStore, "record_command", refused)
    tenant = tenancy()
    runner = runner_for(provider=Counting())
    result = await run(runner, agent(space=workspace(max_workspace_bytes=4096)),
                       [[["run_command", {"argv": ["fill", "big.bin", "5000"]}]],
                        [["write_workspace_file", {"path": "b.txt", "content": "x"}]]], tenant)
    got = await results(runner, result, tenant)
    assert "the command ran" in got["t0-0"].content and "exit code 0" in got["t0-0"].content, got["t0-0"].content
    assert len(events(result, EventType.WORKSPACE_COMMAND_ENDED)) == 1
    assert got["t1-0"].content.startswith("WorkspaceRefused"), "the bound was not measured"


async def test_a_server_success_spelled_like_a_timeout_hides_no_lost_execution(monkeypatch):
    """D5: a server's successful result starting `ToolTimeout:` counted as a timed-out call, and
    hid lost executions live. Only the executor's error results count."""
    import agentsdk.mcp as mcp

    example = _example("31_mcp_contention.py")
    real = mcp.ToolOutput

    def spelled(*, content, source_uri):
        if source_uri == "mcp://catalog/lookup":
            content = "ToolTimeout: " + content
        return real(content=content, source_uri=source_uri)

    monkeypatch.setattr(mcp, "ToolOutput", spelled)
    checks, _ = await example.offline("memory", seed=0, quiet=True, tenant=tenancy(), project=PROJECT, strict=False)
    passed, why = _verdicts(checks)[example.NAMES]
    assert passed is False and "catalog__lookup" in why, checks


async def test_a_kill_is_total_however_the_command_holds_its_files():
    """D6: the kill replaced `cmd.files`, so a command that had kept a reference wrote on."""
    async def captured(cmd):
        files = cmd.files
        for _ in range(40):
            try:
                await cmd.sleep(0.05)
            except asyncio.CancelledError:
                continue
        files.write("late.txt", b"after the kill")
        return 0

    provider = FakeProvider(commands={"captured": captured})
    handle = await provider.create(WorkspaceSpec(image=IMAGE), {})
    assert (await provider.run(handle, ["captured"], timeout=0.2)).killed == "timeout"
    await asyncio.sleep(2.5)
    assert await provider.size(handle) == 0
    await provider.remove(handle)


async def test_the_refusal_says_every_later_command_and_write_is_refused():
    """D8: it told the model to remove files, which nothing it may call can do."""
    tenant = tenancy()
    runner = runner_for(provider=Counting())
    result = await run(runner, agent(space=workspace(max_workspace_bytes=10)),
                       [[["run_command", {"argv": ["fill", "a", "20"]}]], [["run_command", {"argv": ["say", "x"]}]]],
                       tenant)
    content = (await results(runner, result, tenant))["t1-0"].content
    assert "refuses every later command and write" in content and "remove files" not in content, content


@pytest.mark.parametrize("first, second", [("a.txt", "a.txt/"), ("dir/f", "dir")])
async def test_an_import_never_turns_a_file_into_a_folder_or_back(first, second):
    """D9: a folder member over an existing file was accepted, leaving both."""
    import io
    import tarfile

    from tests.workspace_contract import regular_file_tar

    provider = FakeProvider()
    handle = await provider.create(WorkspaceSpec(image=IMAGE), {})
    if first.endswith(".txt"):
        await provider.import_(handle, regular_file_tar(first, b"x"))
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            info = tarfile.TarInfo(second.rstrip("/"))
            info.type = tarfile.DIRTYPE
            tar.addfile(info)
        data = buffer.getvalue()
    else:
        await provider.import_(handle, regular_file_tar(first, b"x"))
        data = regular_file_tar(second, b"y")
    with pytest.raises(ValueError):
        await provider.import_(handle, data)
    await provider.remove(handle)


# =================================================================================================
# FR-147, AC-96: run_command needs approval by default
# =================================================================================================


@pytest.mark.parametrize("store", STORES)
async def test_run_command_waits_for_approval_by_default_and_runs_once_after_approve(store):
    tenant = tenancy()
    provider = Counting()
    runner = runner_for(store, provider=provider)
    spec = agent(space=WorkspaceSpec(image=IMAGE))
    handle = await runner.start(spec, json.dumps([[["run_command", {"argv": ["say", "approved"], "stdin": "in"}]]]),
                                RunConfig(tenant_id=tenant, project_id=PROJECT))
    views = await asked(runner, tenant, handle.run_id)
    assert len(views) == 1
    item = views[0].interruption
    assert item.tool_name == "run_command"
    assert item.request_payload["arguments"] == {"argv": ["say", "approved"], "stdin": "in"}
    await asyncio.sleep(0.2)
    assert provider.ran == [], "a command ran before its approval"
    await runner.decide(tenant, PROJECT, item.interruption_id, approve=True, principal=APPROVER)
    result = await handle.result()
    assert result.status is RunStatus.COMPLETED, result.error
    assert [argv for _, argv in provider.ran] == [["say", "approved"]]


async def test_a_rejected_command_never_runs():
    tenant = tenancy()
    provider = Counting()
    runner = runner_for(provider=provider)
    handle = await runner.start(agent(space=WorkspaceSpec(image=IMAGE)),
                                json.dumps([[["run_command", {"argv": ["say", "no"]}]]]),
                                RunConfig(tenant_id=tenant, project_id=PROJECT))
    views = await asked(runner, tenant, handle.run_id)
    await runner.decide(tenant, PROJECT, views[0].interruption.interruption_id, approve=False, principal=APPROVER)
    result = await handle.result()
    assert provider.ran == [] and result.status is RunStatus.COMPLETED


async def test_with_auto_a_command_runs_without_an_approval_and_the_file_tools_never_ask():
    tenant = tenancy()
    provider = Counting()
    runner = runner_for(provider=provider)
    turns = [[["run_command", {"argv": ["say", "free"]}]],
             [["write_workspace_file", {"path": "a.txt", "content": "x"}], ["read_workspace_file", {"path": "a.txt"}],
              ["list_workspace", {}]]]
    result = await run(runner, agent(space=workspace(run_command_approval=ApprovalPolicy.AUTO)), turns, tenant)
    assert result.status is RunStatus.COMPLETED
    assert await runner.interruptions(tenant, PROJECT, run_id=result.run_id, pending_only=False) == []
    assert [argv for _, argv in provider.ran] == [["say", "free"]]
    # Default approval for the file tools too, with run_command's at its default.
    tenant = tenancy()
    result = await run(runner, agent(space=WorkspaceSpec(image=IMAGE)), turns[1:], tenant)
    assert await runner.interruptions(tenant, PROJECT, run_id=result.run_id, pending_only=False) == []


# =================================================================================================
# FR-140, AC-96: the records, on both stores
# =================================================================================================


def _records(runner, store, tenant, run_id):
    """The run's workspace rows and command rows, from the store the Runner used."""
    if store == "postgres":
        spaces = [dict(zip(("workspace_id", "provider", "image", "limits", "run_id", "scope", "created_at",
                            "removed_at", "removed_reason", "tenant_id", "project_id"), row)) for row in stored(
            "SELECT workspace_id::text, provider, image, limits, run_id::text, scope, created_at, removed_at,"
            " removed_reason, tenant_id, project_id FROM workspaces WHERE run_id = %s::uuid", run_id)]
        commands = [dict(zip(("command_id", "workspace_id", "run_id", "tool_call_id", "argv", "exit_code", "killed",
                              "started_at", "ended_at", "stdout_bytes", "stderr_bytes", "stdout_kept", "stderr_kept"),
                             row)) for row in stored(
            "SELECT command_id::text, workspace_id::text, run_id::text, tool_call_id, argv, exit_code, killed,"
            " started_at, ended_at, stdout_bytes, stderr_bytes, stdout_kept, stderr_kept FROM workspace_commands"
            " WHERE run_id = %s::uuid ORDER BY started_at", run_id)]
        return spaces, commands
    records = runner._workspace_store()
    return ([r.as_row() for r in records.workspaces(tenant, PROJECT, run_id=run_id)],
            [c.as_row() for c in records.commands(tenant, PROJECT, run_id=run_id)])


@pytest.mark.parametrize("store", STORES)
async def test_a_workspace_and_its_commands_are_recorded(store):
    tenant = tenancy()
    runner = runner_for(store, provider=Counting())
    spec = workspace(env={"TOKEN": SECRET}, max_output_bytes=1000)
    turns = [[["run_command", {"argv": ["say", "one"]}]], [["run_command", {"argv": ["flood", "3000"]}]],
             [["run_command", {"argv": ["fail", "2"]}]]]
    result = await run(runner, agent(space=spec), turns, tenant)
    assert result.status is RunStatus.COMPLETED, result.error
    spaces, commands = await asyncio.to_thread(_records, runner, store, tenant, result.run_id)
    assert len(spaces) == 1
    space = spaces[0]
    assert (space["provider"], space["image"], space["run_id"], space["scope"]) == ("fake", IMAGE, result.run_id, "run")
    assert (space["tenant_id"], space["project_id"]) == (tenant, PROJECT)
    assert space["limits"] == spec.record()
    assert space["removed_reason"] == "run_ended" and space["removed_at"] >= space["created_at"]
    assert [c["argv"] for c in commands] == [["say", "one"], ["flood", "3000"], ["fail", "2"]]
    assert [c["exit_code"] for c in commands] == [0, 0, 2]
    assert [c["tool_call_id"] for c in commands] == ["t0-0", "t1-0", "t2-0"]
    assert {c["workspace_id"] for c in commands} == {space["workspace_id"]}
    flood = commands[1]
    assert (flood["stdout_bytes"], flood["stdout_kept"], flood["killed"]) == (3000, 1000, None)
    assert all(c["ended_at"] >= c["started_at"] for c in commands)
    started = events(result, EventType.WORKSPACE_COMMAND_STARTED)
    ended = events(result, EventType.WORKSPACE_COMMAND_ENDED)
    assert len(started) == len(ended) == 3
    assert [e.payload["command_id"] for e in ended] == [c["command_id"] for c in commands]
    assert events(result, EventType.WORKSPACE_CREATED)[0].payload["workspace_id"] == space["workspace_id"]


@pytest.mark.parametrize("store", STORES)
async def test_a_workspaces_removal_is_recorded_once(store):
    tenant = tenancy()
    runner = runner_for(store, provider=Counting())
    result = await run(runner, agent(space=workspace()), [[["run_command", {"argv": ["say", "x"]}]]], tenant)
    records = runner._workspace_store()
    space = (await asyncio.to_thread(_records, runner, store, tenant, result.run_id))[0][0]
    again = await asyncio.to_thread(records.record_removal, tenant, PROJECT, space["workspace_id"], "aclose")
    assert again is False
    after = (await asyncio.to_thread(_records, runner, store, tenant, result.run_id))[0][0]
    assert (after["removed_reason"], after["removed_at"]) == (space["removed_reason"], space["removed_at"])
    # Never from another tenancy either.
    assert await asyncio.to_thread(records.record_removal, tenancy(), PROJECT, space["workspace_id"], "aclose") is False


@pytest.mark.parametrize("store", STORES)
async def test_no_env_value_reaches_a_row_an_event_or_the_manifest(store):
    tenant = tenancy()
    runner = runner_for(store, provider=Counting())
    result = await run(runner, agent(space=workspace(env={"TOKEN": SECRET})),
                       [[["run_command", {"argv": ["say", "x"]}]]], tenant)
    assert SECRET not in json.dumps([e.payload for e in result.events], default=str)
    rows = await asyncio.to_thread(_records, runner, store, tenant, result.run_id)
    assert SECRET not in json.dumps(rows, default=str)
    if store == "postgres":
        dumped = json.dumps(stored("SELECT * FROM execution_manifests WHERE run_id = %s::uuid", result.run_id),
                            default=str)
        assert SECRET not in dumped and "TOKEN" in dumped
        events_rows = json.dumps(stored("SELECT payload FROM run_events WHERE run_id = %s::uuid", result.run_id),
                                 default=str)
        assert SECRET not in events_rows


async def test_the_manifest_records_the_workspace_and_none_for_a_run_without_one():
    tenant = tenancy()
    runner = runner_for("postgres", provider=Counting())
    spec = workspace(env={"TOKEN": SECRET})
    with_one = await run(runner, agent(space=spec), [], tenant)
    without = await run(runner, agent(), [], tenant)
    (recorded,), = stored("SELECT workspace FROM execution_manifests WHERE run_id = %s::uuid", with_one.run_id)
    assert recorded == {"provider": "fake", **spec.record()}
    (absent,), = stored("SELECT workspace FROM execution_manifests WHERE run_id = %s::uuid", without.run_id)
    assert absent is None


def test_the_tracked_tables_include_the_workspace_tables():
    from tests import conftest

    assert {"workspaces", "workspace_commands"} <= set(conftest._RUN_TABLES)


# =================================================================================================
# NFR-40, NFR-41
# =================================================================================================


def test_the_runtime_dependencies_are_unchanged_and_the_workspace_package_ships():
    import tomllib

    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    # Against Phase 4's last commit (M37, 6e59220), the tip Phase 5 branched from.
    before = subprocess.run(["git", "show", "6e59220:pyproject.toml"], cwd=REPO, capture_output=True, text=True,
                            check=True).stdout
    assert project["project"]["dependencies"] == tomllib.loads(before)["project"]["dependencies"]
    assert "agentsdk.workspace" in project["tool"]["setuptools"]["packages"]


def test_the_workspace_package_imports_nothing_import_agentsdk_does_not():
    script = ("import sys\nimport agentsdk\nbefore = set(sys.modules)\nimport agentsdk.workspace\n"
              "print(sorted(m for m in set(sys.modules) - before if not m.startswith('agentsdk')))\n")
    done = subprocess.run([sys.executable, "-c", script], cwd=REPO, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    added = json.loads(done.stdout.strip().replace("'", '"'))
    third_party = [m for m in added if m.split(".")[0] not in sys.stdlib_module_names]
    assert third_party == [], third_party


@pytest.mark.parametrize("store", STORES)
async def test_a_run_without_a_workspace_equals_the_committed_fixture_beside_a_provider(store, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("nfr29", REPO / "tests" / "test_interruption_record.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.TENANT = PREFIX + "nfr41"  # under this file's cleanup, not M29's
    expected = json.loads(module.FIXTURE.read_text(encoding="utf-8"))
    assert await module._ungated_record(store, None) == expected
    real = module.Runner
    monkeypatch.setattr(module, "Runner", lambda *a, **k: real(*a, workspace_provider=FakeProvider(), **k))
    assert await module._ungated_record(store, None) == expected


# =================================================================================================
# FR-141: example 32
# =================================================================================================


async def test_example_32_holds_on_both_stores():
    example = _example("32_workspace.py")
    for store in STORES:
        example.TENANT = tenancy()
        checks = await example.offline(store)
        assert [label for label, passed in checks if not passed] == [] and len(checks) >= 5, (store, checks)


def test_example_32_runs_offline():
    done = subprocess.run([sys.executable, str(REPO / "scripts" / "32_workspace.py"), "--offline"],
                          capture_output=True, text=True, timeout=240, cwd=REPO)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "[FAIL]" not in done.stdout and "[PASS]" in done.stdout, done.stdout
