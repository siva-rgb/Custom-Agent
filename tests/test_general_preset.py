"""M49: the general preset and the general-purpose subagent (FR-174 to FR-177, AC-107, NFR-50, NFR-52;
DECISION-8aa24890, DECISION-05d0a716).

`general_agent(...)` (`agentsdk.presets`) returns a `GeneralAgent`: an `AgentSpec` with the shipped
instructions, the five workspace tools and a `WorkspaceSpec`, marked `preset=("general", "general-v4")`,
and `config()` giving a `RunConfig` with its budget, 40 turns and `ContextPolicy()`. `run_command` is
`auto`; `export_workspace_file` follows the new `WorkspaceSpec.export_approval`, `REQUIRE_APPROVAL` in
the preset and `AUTO` everywhere else. `WorkspaceSpec.files` is written into a run's workspace when
it is created, checked and bounded before anything is, and recorded as path to SHA-256 only.
`general_subagent(...)` is a child's spec, marked `general-subagent`; `Orchestrator(role_turns=...)`
briefs each named role's children with its turns and every other role with 4, recorded in the
orchestrator run's manifest. Migration 0028 adds `execution_manifests.preset` and `role_turns`.

The gates run on `FakeProvider` and a scripted model; the persisted half removes every row it writes,
by its own tenancies. Manifests are kept on Postgres only.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import uuid
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from agentsdk import AgentSpec, Persistence, RunConfig, RunStatus, Runner
from agentsdk.budget import BudgetPolicy
from agentsdk.config import normalise_database_url
from agentsdk.context_policy import ContextPolicy
from agentsdk.events import EventType
from agentsdk.identity import PrincipalContext
from agentsdk.manifest import agent_spec_hash
from agentsdk.migrate import discover
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.orchestrator import PLAN_TOOL, Orchestrator
from agentsdk.postgres import RunScope
from agentsdk.presets import (
    GENERAL_INSTRUCTIONS,
    GENERAL_TOOLS,
    SUBAGENT_INSTRUCTIONS,
    SUBAGENT_TURNS,
    GeneralAgent,
    general_agent,
    general_subagent,
)
from agentsdk.primitives import Message, Role, ToolCall
from agentsdk.registry import ModelCapabilities, ModelEntry, ModelPricing, ModelRegistry
from agentsdk.scheduler import SchedulerLimits
from agentsdk.tools import ApprovalPolicy
from agentsdk.workspace import FakeProvider, WorkspaceSpec
from agentsdk.workspace.docker import MAX_TRANSFER_BYTES, MAX_TRANSFER_MEMBERS

REPO = Path(__file__).resolve().parent.parent
DSN = normalise_database_url(os.environ.get("DATABASE_URL"))
PREFIX = "SYN-m49-"
PROJECT = "p-m49"
STORES = ["memory", "postgres"]
FAKE_IMAGE = "fake/workspace@sha256:" + "0" * 64
MODEL = "known-model"
WINDOWLESS = "windowless-model"
OPERATOR = PrincipalContext(agent_principal="approval-ui", user_principal="owner")
BUDGET = BudgetPolicy(run_ceiling_usd=Decimal("5"))
FILES = {"data/numbers.txt": b"3\n4\n5\n", "README": b"sum the numbers\n"}


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


def registry() -> ModelRegistry:
    def entry(model_id, window):
        return ModelEntry(provider="test", model_id=model_id, model_version="1", adapter_version="test/1",
                          capabilities=ModelCapabilities(max_context_tokens=window,
                                                         pricing=ModelPricing(input=Decimal("0.01"),
                                                                              output=Decimal("0.10"))))
    return ModelRegistry([entry(MODEL, 100_000), entry(WINDOWLESS, None)])


def preset(**fields) -> GeneralAgent:
    fields.setdefault("model", f"m:{MODEL}")
    fields.setdefault("image", FAKE_IMAGE)
    fields.setdefault("budget", BUDGET)
    fields.setdefault("registry", registry())
    return general_agent(**fields)


def respond(content="", calls=()):
    return ModelResponse(message=Message(role=Role.ASSISTANT, content=content, tool_calls=tuple(calls)),
                         stop_reason=StopReason.TOOL_CALLS if calls else StopReason.END_TURN, usage=Usage(5, 1, 6))


class Turns:
    """The model: the task's last line holds a JSON list of turns, each a list of [tool, arguments]."""

    async def send(self, request):
        first = next(m for m in request.messages if m.role is Role.USER).content
        turns = json.loads(first[first.index("["):].split("\n")[0])
        turn = sum(1 for m in request.messages if m.role is Role.ASSISTANT)
        if turn < len(turns):
            return respond(calls=[ToolCall(id=f"t{turn}-{i}", name=name, arguments=args)
                                  for i, (name, args) in enumerate(turns[turn])])
        return respond("done")


class Planner:
    def __init__(self, nodes):
        self.nodes = nodes

    async def send(self, request):
        if not any(m.role is Role.TOOL for m in request.messages):
            return respond(calls=[ToolCall(id="plan", name=PLAN_TOOL, arguments={"nodes": self.nodes})])
        return respond("the plan is done")


async def _cat(cmd):
    cmd.out(cmd.files.read(cmd.argv[1]).decode())
    return 0


def provider():
    return FakeProvider(commands={"cat": _cat})


def persistence(store):
    return Persistence.postgres(DSN, create_schema=False) if store == "postgres" else None


def runner_for(store="memory", model=None, **options):
    return Runner({"m": model or Turns()}, model_registry=registry(), workspace_provider=provider(),
                  persistence=persistence(store), **options)


def events(result, kind):
    return [e.payload for e in result.events if e.event_type is kind]


def error_types(result) -> dict:
    return {e.payload["tool_call_id"]: e.payload.get("error_type")
            for e in result.events if e.event_type is EventType.TOOL_CALLED}


def stored(sql, *args):
    with psycopg.connect(DSN) as conn:
        return conn.execute(sql, args).fetchall()


async def results(runner, result, tenant):
    sessions = runner._sessions_for(RunScope(run_id=result.run_id, tenant_id=tenant, project_id=PROJECT))
    history = await asyncio.to_thread(sessions.history, result.run_id)
    return {r.tool_call_id: r for m in history for r in m.tool_results}


async def manifest_columns(run_id: str):
    [(preset_, role_turns, workspace)] = await asyncio.to_thread(
        stored, "SELECT preset, role_turns, workspace FROM execution_manifests WHERE run_id = %s::uuid", run_id)
    return preset_, role_turns, workspace


async def run_preset(store, turns, *, agent=None, **config):
    tenant, runner = tenancy(), runner_for(store)
    agent = agent or preset(run_command_approval=ApprovalPolicy.AUTO, export_approval=ApprovalPolicy.AUTO)
    try:
        result = await runner.run(agent.spec, json.dumps(turns),
                                  agent.config(tenant_id=tenant, project_id=PROJECT, **config))
        got = await results(runner, result, tenant)
        rows = await asyncio.to_thread(runner._workspace_store().workspaces, tenant, PROJECT, result.run_id)
    finally:
        await runner.aclose()
    return result, got, rows, tenant


# =================================================================================================
# FR-174: what general_agent builds
# =================================================================================================


def test_the_preset_is_a_general_agent_with_the_workspace_tools_and_no_other():
    agent = preset()
    spec = agent.spec
    assert isinstance(agent, GeneralAgent) and isinstance(spec, AgentSpec)
    assert spec.id == "general" and spec.preferred_model == f"m:{MODEL}"
    assert spec.instructions == GENERAL_INSTRUCTIONS
    assert spec.tool_profile == GENERAL_TOOLS == ("run_command", "read_workspace_file", "write_workspace_file",
                                                  "list_workspace", "export_workspace_file")
    assert spec.preset == ("general", "general-v4")
    assert spec.permission_policy is None
    space = spec.workspace
    assert space.image == FAKE_IMAGE and space.keep is False and space.scope == "run" and space.egress == ()
    assert space.run_command_approval is ApprovalPolicy.AUTO
    assert space.export_approval is ApprovalPolicy.REQUIRE_APPROVAL
    assert dict(space.files) == {}


def test_config_carries_the_budget_forty_turns_and_a_context_policy():
    config = preset().config("t", "p")
    assert isinstance(config, RunConfig)
    assert (config.tenant_id, config.project_id) == ("t", "p")
    assert config.budget == BUDGET and config.max_turns == 40 and config.context_policy == ContextPolicy()
    assert config.budget_lease is None and config.role_turns is None


def test_config_overrides_replace_any_field():
    other = BudgetPolicy(run_ceiling_tokens=100)
    config = preset().config("t", "p", max_turns=7, budget=other, context_policy=ContextPolicy(compact_at=0.5))
    assert config.max_turns == 7 and config.budget == other and config.context_policy.compact_at == 0.5
    with pytest.raises(TypeError):
        preset().config("t", "p", no_such_field=1)


def test_every_argument_reaches_the_spec_it_names():
    agent = preset(egress=("example.com:443",), files=FILES, keep=True, max_turns=12,
                   context_policy=ContextPolicy(keep_recent_turns=3), run_command_approval=ApprovalPolicy.REQUIRE_APPROVAL,
                   export_approval=ApprovalPolicy.AUTO, instructions="be brief", memory_bytes=1024 ** 3,
                   max_command_seconds=200)
    space = agent.spec.workspace
    assert space.egress == ("example.com:443",) and dict(space.files) == FILES and space.keep is True
    assert space.run_command_approval is ApprovalPolicy.REQUIRE_APPROVAL and space.export_approval is ApprovalPolicy.AUTO
    assert space.memory_bytes == 1024 ** 3 and space.max_command_seconds == 200
    assert agent.spec.instructions == "be brief"
    config = agent.config("t", "p")
    assert config.max_turns == 12 and config.context_policy.keep_recent_turns == 3


def test_a_caller_text_is_not_recorded_as_the_shipped_version():
    """A changed text is a new version (FR-176): the shipped version names the shipped text only."""
    assert preset(instructions="be brief").spec.preset == ("general", "custom")
    assert preset(instructions=GENERAL_INSTRUCTIONS).spec.preset == ("general", "general-v4")


def test_the_shipped_registry_is_used_when_none_is_given():
    agent = general_agent(model="openai.gpt-4o-mini", image=FAKE_IMAGE, budget=BUDGET)
    assert agent.spec.preferred_model == "openai.gpt-4o-mini"


@pytest.mark.parametrize("model", [f"m:{WINDOWLESS}", "m:not-registered", "not-registered"])
def test_a_model_whose_window_the_registry_does_not_know_is_refused(model):
    with pytest.raises(ValueError, match="context window"):
        preset(model=model)


@pytest.mark.parametrize("budget", [None, {"run_ceiling_usd": "1"}, Decimal("1")])
def test_a_budget_that_is_no_policy_is_refused(budget):
    with pytest.raises(ValueError, match="BudgetPolicy"):
        preset(budget=budget)


@pytest.mark.parametrize("image", ["python:3.12", "python@sha256:abc", ""])
def test_an_image_not_pinned_by_digest_is_refused(image):
    with pytest.raises(ValueError, match="digest"):
        preset(image=image)


@pytest.mark.parametrize("missing", ["model", "image", "budget"])
def test_model_image_and_budget_are_required(missing):
    fields = {"model": f"m:{MODEL}", "image": FAKE_IMAGE, "budget": BUDGET, "registry": registry()}
    del fields[missing]
    with pytest.raises(TypeError):
        general_agent(**fields)


def test_a_limit_the_workspace_has_not_is_refused():
    with pytest.raises(TypeError):
        preset(gpus=1)


def test_the_instructions_name_no_provider_model_or_provider_feature():
    """NFR-52."""
    for text in (GENERAL_INSTRUCTIONS, SUBAGENT_INSTRUCTIONS):
        found = re.findall(r"openai|anthropic|claude|gpt|bedrock|gemini|llama|mistral|haiku|sonnet|opus|"
                           r"litellm|thinking|cache_control", text, flags=re.IGNORECASE)
        assert found == [], found
        assert len(text) > 200, "a real text, not a placeholder"


def test_the_instructions_ship_as_sdk_source_with_their_version():
    source = (REPO / "agentsdk" / "presets.py").read_text(encoding="utf-8")
    assert "general-v4" in source and "general-subagent-v1" in source


# =================================================================================================
# FR-174: approval in the preset, and the new export_approval
# =================================================================================================


def test_export_approval_defaults_to_auto_so_every_other_workspace_is_as_it_was():
    space = WorkspaceSpec(image=FAKE_IMAGE)
    assert space.export_approval is ApprovalPolicy.AUTO
    assert "export_approval" not in space.record()
    assert WorkspaceSpec(image=FAKE_IMAGE, export_approval=ApprovalPolicy.REQUIRE_APPROVAL).record()[
        "export_approval"] == "require_approval"


def test_an_export_approval_that_is_no_policy_is_refused():
    with pytest.raises(ValueError, match="export_approval"):
        WorkspaceSpec(image=FAKE_IMAGE, export_approval="auto")


def test_the_export_tool_follows_the_specs_export_approval():
    from agentsdk.workspace.tools import specs
    assert specs(WorkspaceSpec(image=FAKE_IMAGE))["export_workspace_file"].approval_policy is ApprovalPolicy.AUTO
    held = specs(WorkspaceSpec(image=FAKE_IMAGE, export_approval=ApprovalPolicy.REQUIRE_APPROVAL))
    assert held["export_workspace_file"].approval_policy is ApprovalPolicy.REQUIRE_APPROVAL
    assert held["run_command"].approval_policy is ApprovalPolicy.REQUIRE_APPROVAL, "run_command keeps its own"


async def _artifact_count(runner, store, tenant, run_id) -> int:
    if store == "postgres":
        [(count,)] = await asyncio.to_thread(
            stored, "SELECT count(*) FROM artifacts WHERE source_run = %s::uuid", run_id)
        return count
    found = runner._memory_artifacts
    return 0 if found is None else sum(1 for ref, _ in found._rows.values() if ref.source_run == run_id)


@pytest.mark.parametrize("store", STORES)
async def test_in_the_preset_an_export_waits_for_approval_and_a_command_does_not(store):
    tenant, runner = tenancy(), runner_for(store)
    agent = preset()
    turns = [[["run_command", {"argv": ["cat", "README"]}]],
             [["write_workspace_file", {"path": "out.txt", "content": "12"}]],
             [["export_workspace_file", {"path": "out.txt"}]]]
    seen: list = []
    try:
        handle = await runner.start(agent.spec, json.dumps(turns),
                                    agent.config(tenant_id=tenant, project_id=PROJECT))

        async def operator():
            while True:
                for view in await runner.interruptions(tenant, PROJECT, run_id=handle.run_id):
                    seen.append((view.interruption.tool_name,
                                 await _artifact_count(runner, store, tenant, handle.run_id)))
                    await runner.decide(tenant, PROJECT, view.interruption.interruption_id, approve=True,
                                        principal=OPERATOR)
                await asyncio.sleep(0.02)

        approving = asyncio.ensure_future(operator())
        try:
            result = await asyncio.wait_for(handle.result(), 30)
        finally:
            approving.cancel()
        after = await _artifact_count(runner, store, tenant, result.run_id)
    finally:
        await runner.aclose()
    assert result.status is RunStatus.COMPLETED, result.error
    assert seen == [("export_workspace_file", 0)], "one interruption, the export's, with no artifact before it"
    assert after == 1
    assert [p["argv"] for p in events(result, EventType.WORKSPACE_COMMAND_STARTED)] == [["cat", "README"]]


# =================================================================================================
# FR-175: files a workspace starts with
# =================================================================================================


@pytest.mark.parametrize("store", STORES)
async def test_the_files_are_in_the_workspace_when_its_first_command_runs(store):
    result, got, rows, _ = await run_preset(store, [[["run_command", {"argv": ["cat", "data/numbers.txt"]}],
                                                     ["read_workspace_file", {"path": "README"}]]],
                                            agent=preset(files=FILES, run_command_approval=ApprovalPolicy.AUTO))
    assert result.status is RunStatus.COMPLETED, result.error
    assert "3\n4\n5" in got["t0-0"].content and got["t0-1"].content == "sum the numbers\n"
    [row] = rows
    assert row.limits["files"] == {path: hashlib.sha256(data).hexdigest() for path, data in FILES.items()}


@pytest.mark.parametrize("store", ["postgres"])
async def test_files_are_recorded_as_path_and_sha256_never_their_content(store):
    result, _, _, _ = await run_preset(store, [], agent=preset(files=FILES))
    _, _, workspace = await manifest_columns(result.run_id)
    assert workspace["files"] == {path: hashlib.sha256(data).hexdigest() for path, data in FILES.items()}
    assert "sum the numbers" not in json.dumps(workspace)
    assert workspace["export_approval"] == "require_approval"


def test_files_and_export_approval_leave_the_agent_spec_hash_unchanged():
    plain, full = preset(), preset(files=FILES, export_approval=ApprovalPolicy.AUTO)
    assert (agent_spec_hash(plain.spec.id, plain.spec.instructions, plain.spec.tool_profile)
            == agent_spec_hash(full.spec.id, full.spec.instructions, full.spec.tool_profile))


def test_a_spec_without_files_records_what_it_did():
    assert "files" not in WorkspaceSpec(image=FAKE_IMAGE).record()


@pytest.mark.parametrize("path", ["", "/abs", "a/../b", "..", "./a", "a//b", "a\\b", "c:x", "a\x00b", "a/"])
def test_a_file_path_that_is_not_clean_and_relative_is_refused(path):
    with pytest.raises(ValueError, match="files"):
        WorkspaceSpec(image=FAKE_IMAGE, files={path: b"x"})


def test_file_content_that_is_not_bytes_is_refused():
    with pytest.raises(ValueError, match="bytes"):
        WorkspaceSpec(image=FAKE_IMAGE, files={"a.txt": "text"})


def test_a_file_that_is_another_files_folder_or_differs_only_by_case_is_refused():
    with pytest.raises(ValueError, match="folder"):
        WorkspaceSpec(image=FAKE_IMAGE, files={"a": b"1", "a/b": b"2"})
    with pytest.raises(ValueError, match="case"):
        WorkspaceSpec(image=FAKE_IMAGE, files={"A.txt": b"1", "a.txt": b"2"})


def test_files_past_the_transfer_bound_are_refused_before_anything_is_written():
    with pytest.raises(ValueError, match="bytes"):
        WorkspaceSpec(image=FAKE_IMAGE, files={"big": b"x" * (MAX_TRANSFER_BYTES + 1)})


def test_files_past_the_member_count_are_refused():
    with pytest.raises(ValueError, match="files"):
        WorkspaceSpec(image=FAKE_IMAGE, files={f"f{i}": b"" for i in range(MAX_TRANSFER_MEMBERS + 1)})


def test_a_path_past_the_workspace_path_bound_is_refused():
    with pytest.raises(ValueError, match="files"):
        WorkspaceSpec(image=FAKE_IMAGE, files={"a" * 4097: b""})


def test_files_with_a_snapshot_are_refused():
    with pytest.raises(ValueError, match="from_snapshot"):
        WorkspaceSpec(image=FAKE_IMAGE, files=FILES, from_snapshot=str(uuid.uuid4()))


def test_files_on_a_plan_workspace_are_refused():
    with pytest.raises(ValueError, match="plan"):
        WorkspaceSpec(image=FAKE_IMAGE, files=FILES, scope="plan")


def test_the_files_are_frozen_with_the_spec():
    given = dict(FILES)
    space = WorkspaceSpec(image=FAKE_IMAGE, files=given)
    given["late"] = b"x"
    assert "late" not in space.files
    with pytest.raises(TypeError):
        space.files["later"] = b"x"


class FailingImport(FakeProvider):
    def __init__(self):
        super().__init__(commands={"cat": _cat})
        self.removed = []

    async def import_(self, handle, data):
        raise OSError("the transfer broke")

    async def remove(self, handle):
        self.removed.append(handle.workspace_id)
        await super().remove(handle)


async def test_a_workspace_whose_files_cannot_be_written_is_removed_and_runs_nothing():
    tenant, prov = tenancy(), FailingImport()
    runner = Runner({"m": Turns()}, model_registry=registry(), workspace_provider=prov)
    agent = preset(files=FILES)
    try:
        result = await runner.run(agent.spec, json.dumps([[["run_command", {"argv": ["cat", "README"]}]]]),
                                  agent.config(tenant_id=tenant, project_id=PROJECT))
        rows = runner._workspace_store().workspaces(tenant, PROJECT, result.run_id)
    finally:
        await runner.aclose()
    assert error_types(result)["t0-0"] == "ToolExecutionError"
    assert events(result, EventType.WORKSPACE_COMMAND_STARTED) == []
    assert len(prov.removed) == 1 and rows == [], "removed, and never recorded as a workspace that ran"


# =================================================================================================
# FR-176: the preset recorded, migration 0028
# =================================================================================================


def test_migration_0028_adds_preset_and_role_turns():
    names = [path.name for _, path in discover()]
    assert names[-1] == "0028_presets.sql", names[-3:]
    text = (REPO / "agentsdk" / "migrations" / "0028_presets.sql").read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS preset JSONB" in text and "ADD COLUMN IF NOT EXISTS role_turns JSONB" in text


@pytest.mark.skipif(not DSN, reason="needs DATABASE_URL")
def test_the_columns_exist_as_nullable_jsonb():
    rows = stored("SELECT column_name, data_type, is_nullable FROM information_schema.columns"
                  " WHERE table_name = 'execution_manifests' AND column_name IN ('preset', 'role_turns')"
                  " ORDER BY column_name")
    assert rows == [("preset", "jsonb", "YES"), ("role_turns", "jsonb", "YES")]


async def test_the_manifest_names_the_preset_and_its_version():
    result, _, _, _ = await run_preset("postgres", [])
    preset_, role_turns, _ = await manifest_columns(result.run_id)
    assert preset_ == {"name": "general", "version": "general-v4"} and role_turns is None


async def test_a_run_without_the_preset_records_no_preset():
    """NFR-50: NULL, as for every run before 0028."""
    tenant, runner = tenancy(), runner_for("postgres")
    try:
        result = await runner.run(AgentSpec(id="plain", instructions="x", preferred_model=f"m:{MODEL}"),
                                  json.dumps([]), RunConfig(tenant_id=tenant, project_id=PROJECT))
    finally:
        await runner.aclose()
    assert await manifest_columns(result.run_id) == (None, None, None)


@pytest.mark.parametrize("bad", [("general",), ("", "v1"), ("general", ""), ["general", "v1"], "general",
                                 ("general", 1)])
def test_a_preset_mark_that_is_not_a_name_and_a_version_is_refused(bad):
    with pytest.raises(ValueError, match="preset"):
        AgentSpec(id="a", instructions="x", preset=bad)


def test_the_preset_mark_leaves_the_agent_spec_hash_as_it_was():
    marked = AgentSpec(id="a", instructions="x", preset=("general", "general-v1"))
    assert agent_spec_hash(marked.id, marked.instructions, marked.tool_profile) == agent_spec_hash("a", "x", ())


# =================================================================================================
# FR-177: the general-purpose subagent and role_turns
# =================================================================================================


def test_the_subagent_is_a_childs_spec_with_its_own_text_and_no_budget_or_turns():
    child = general_subagent(model=f"m:{MODEL}", image=FAKE_IMAGE, registry=registry())
    assert isinstance(child, AgentSpec)
    assert child.id == "general-subagent" and child.instructions == SUBAGENT_INSTRUCTIONS
    assert child.preset == ("general-subagent", "general-subagent-v1")
    assert child.tool_profile == GENERAL_TOOLS and child.workspace.scope == "run"
    assert child.workspace.export_approval is ApprovalPolicy.REQUIRE_APPROVAL
    assert SUBAGENT_TURNS == 20
    for field in ("budget", "max_turns", "context_policy"):
        with pytest.raises(TypeError):
            general_subagent(model=f"m:{MODEL}", image=FAKE_IMAGE, registry=registry(), **{field: None})


def test_the_subagent_is_refused_as_the_preset_is():
    with pytest.raises(ValueError, match="context window"):
        general_subagent(model=f"m:{WINDOWLESS}", image=FAKE_IMAGE, registry=registry())
    with pytest.raises(ValueError, match="digest"):
        general_subagent(model=f"m:{MODEL}", image="python:3", registry=registry())


def _roles():
    child = general_subagent(model=f"m:{MODEL}", image=FAKE_IMAGE, registry=registry(),
                             run_command_approval=ApprovalPolicy.AUTO)
    other = AgentSpec(id="other", instructions="x", preferred_model=f"m:{MODEL}")
    return {"general": child, "other": other}


def _orchestrator(**options):
    return Orchestrator(roles=_roles(), policy=BudgetPolicy(run_ceiling_tokens=1_000_000),
                        limits=SchedulerLimits(max_concurrent_subagents=8),
                        context_policy=ContextPolicy(context_window=1_000_000), **options)


@pytest.mark.parametrize("bad", [{"nobody": 3}, {"general": 0}, {"general": True}, {"general": 2.0}, ["general"],
                                 {1: 3}])
def test_role_turns_naming_no_role_or_no_turns_are_refused(bad):
    with pytest.raises(ValueError, match="role_turns"):
        _orchestrator(role_turns=bad)


def test_the_orchestrators_config_carries_its_role_turns_and_none_without():
    assert _orchestrator(role_turns={"general": SUBAGENT_TURNS}).config(
        tenant_id="t", project_id="p").role_turns == {"general": 20}
    assert _orchestrator().config(tenant_id="t", project_id="p").role_turns is None


def _orchestrator_lease():
    from agentsdk.budget import BudgetGovernor

    return BudgetGovernor.for_run(BudgetPolicy(run_ceiling_tokens=1_000)).orchestrator_lease()


@pytest.mark.parametrize("bad", [{"r": 0}, {"": 3}, {"r": True}, ["r"]])
def test_a_run_configs_role_turns_are_checked(bad):
    """On an orchestrator's lease, so the shape is what refuses them (the control accepts)."""
    lease = _orchestrator_lease()
    assert RunConfig(tenant_id="t", project_id="p", budget_lease=lease, role_turns={"r": 3}).role_turns == {"r": 3}
    with pytest.raises(ValueError, match="role_turns must map"):
        RunConfig(tenant_id="t", project_id="p", budget_lease=lease, role_turns=bad)


def _single_run_lease():
    from agentsdk.budget import SINGLE_RUN, BudgetGovernor

    return BudgetGovernor.for_single_run(BudgetPolicy(run_ceiling_tokens=1_000)).lease(SINGLE_RUN)


@pytest.mark.parametrize("fields", [{}, {"budget": BUDGET}, {"budget_lease": "lease"}, {"max_turns": 20}],
                         ids=["plain", "budgeted", "single-run-lease", "turns"])
@pytest.mark.parametrize("given", [{"general": 20}, {}])
def test_role_turns_on_any_run_but_an_orchestrators_are_refused(fields, given):
    """Round 1 F-A, its second route: a RunConfig built by hand for a run no orchestrator briefs
    would record turns nobody was given."""
    if fields.get("budget_lease") == "lease":
        fields = {"budget_lease": _single_run_lease()}
    with pytest.raises(ValueError, match="role_turns belong to an orchestrator"):
        RunConfig(tenant_id="t", project_id="p", role_turns=given, **fields)


def test_the_general_agents_config_refuses_role_turns():
    """The preset's config() is a RunConfig like any other: a plain run is never an orchestrator's."""
    with pytest.raises(ValueError, match="role_turns belong to an orchestrator"):
        preset().config("t", "p", role_turns={"general": 20})


def _node(node_id, turns, role):
    return {"node_id": node_id, "objective": json.dumps(turns), "assigned_role": role, "dependencies": []}


# Six turns of work: a child briefed with 20 completes it, one briefed with 4 runs out first.
SIX = [[["write_workspace_file", {"path": f"s{i}.txt", "content": "x"}]] for i in range(6)]


async def _plan(store, nodes=None, **options):
    """One orchestrator run over the general subagent and another role; returns the run, its node
    outcomes, its children by agent id, and the tenancy's workspace rows."""
    tenant = tenancy()
    orchestrator = _orchestrator(**options)
    nodes = nodes or [_node("a", [[["run_command", {"argv": ["cat", "README"]}]]], "general"),
                      _node("b", [], "other"),
                      _node("c", [[["write_workspace_file", {"path": "c.txt", "content": "c"}]]], "general")]
    runner = Runner({"planner": Planner(nodes), "m": Turns()}, model_registry=registry(), tools=[orchestrator.tool],
                    workspace_provider=FakeProvider(commands={"cat": _cat}), persistence=persistence(store))
    try:
        result = await runner.run(orchestrator.agent, "go", orchestrator.config(
            tenant_id=tenant, project_id=PROJECT, model_override="planner:p", max_turns=6))
        children = [child for (t, _, child), parent in runner._parents.items()
                    if t == tenant and parent == result.run_id]
        rows = await asyncio.to_thread(runner._workspace_store().workspaces, tenant, PROJECT)
    finally:
        await runner.aclose()
    # Each node's last outcome, as its PlanNodeFinished event records it (FR-66).
    outcomes = {e.task_id: e.payload["status"] for e in result.events if e.event_type is EventType.PLAN_NODE_FINISHED}
    return result, outcomes, children, rows


async def _child_turns(children) -> dict:
    """Each child's turn limit as its runs row holds it, by agent id (Postgres keeps the rows)."""
    found: dict = {}
    for agent_id, max_turns in await asyncio.to_thread(
            stored, "SELECT agent_spec_id, max_turns FROM runs WHERE run_id = ANY(%s::uuid[])", children):
        found.setdefault(agent_id, set()).add(max_turns)
    return found


@pytest.mark.parametrize("store", STORES)
async def test_role_turns_brief_the_general_subagent_with_its_turns_and_every_other_role_with_four(store):
    result, outcomes, children, rows = await _plan(
        store, [_node("a", SIX, "general"), _node("b", [], "other"),
                _node("c", [[["write_workspace_file", {"path": "c.txt", "content": "c"}]]], "general")],
        role_turns={"general": SUBAGENT_TURNS})
    assert result.status is RunStatus.COMPLETED, (result.error, outcomes)
    assert outcomes == {"a": "done", "b": "done", "c": "done"}, "six turns of work fit in the general role's 20"
    if store == "postgres":
        assert await _child_turns(children) == {"general-subagent": {20}, "other": {4}}
    general = [r for r in rows if r.run_id in children]
    assert len(general) == 2 and len({r.workspace_id for r in general}) == 2, "each child its own workspace"


@pytest.mark.parametrize("store", STORES)
async def test_without_role_turns_every_child_is_briefed_with_four(store):
    """NFR-50: an orchestrator without role_turns briefs every child as it did, so six turns of
    work do not fit in the general role's child either."""
    result, outcomes, children, _ = await _plan(store, [_node("a", SIX, "general"), _node("b", [], "other")])
    assert result.status is RunStatus.FAILED and outcomes["a"] == "failed", (result.error, outcomes)
    if store == "postgres":
        assert await _child_turns(children) == {"general-subagent": {4}, "other": {4}}


async def test_role_turns_name_only_the_role_they_brief():
    """A role role_turns names is briefed with its turns; the control, the same work under a role it
    does not name, runs out at 4."""
    _, outcomes, _, _ = await _plan("memory", [_node("a", SIX, "general"), _node("b", SIX, "other")],
                                    role_turns={"general": SUBAGENT_TURNS})
    assert outcomes["a"] == "done" and outcomes["b"] == "failed", outcomes


async def test_role_turns_are_recorded_in_the_orchestrator_runs_manifest_and_null_without():
    given, _, _, _ = await _plan("postgres", role_turns={"general": SUBAGENT_TURNS})
    without, _, _, _ = await _plan("postgres")
    assert (await manifest_columns(given.run_id))[1] == {"general": 20}
    assert (await manifest_columns(without.run_id))[1] is None


async def test_each_child_records_the_general_subagent_preset():
    result, _, children, _ = await _plan("postgres", role_turns={"general": SUBAGENT_TURNS})
    found = await asyncio.to_thread(
        stored, "SELECT r.agent_spec_id, m.preset FROM runs r JOIN execution_manifests m USING (run_id)"
                " WHERE r.run_id = ANY(%s::uuid[]) ORDER BY 1", list(children))
    assert found == [("general-subagent", {"name": "general-subagent", "version": "general-subagent-v1"}),
                     ("general-subagent", {"name": "general-subagent", "version": "general-subagent-v1"}),
                     ("other", None)]


# =================================================================================================
# FR-178: example 42
# =================================================================================================


def _example(name: str):
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py").replace("-", "_"), REPO / "scripts" / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EX42 = _example("42_general_agent.py")
EX42_CHECKS = 4


def _verdicts(checks) -> list:
    """Each check's verdict in order: True passed, False failed, None never exercised."""
    return [passed for _, passed, _ in checks]


@pytest.mark.parametrize("store", STORES)
async def test_example_42_holds_every_check_offline(store):
    checks, runs = await EX42.offline(store, "fake", tenant=tenancy(), quiet=True)
    assert _verdicts(checks) == [True] * EX42_CHECKS, checks
    assert len(runs) == EX42_CHECKS


def test_example_42_runs_offline_as_a_script():
    done = subprocess.run([sys.executable, str(REPO / "scripts" / "42_general_agent.py"), "--offline"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240,
                          env={**os.environ, "PYTHONPATH": str(REPO), "PYTHONIOENCODING": "utf-8"})
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
    assert done.stdout.count("[PASS]") == EX42_CHECKS and "[FAIL]" not in done.stdout


async def test_example_42_fails_the_task_check_when_no_command_solves_it(monkeypatch):
    """The subject removed: the model types the answer into result.txt and runs nothing."""
    typed = ([[("write_workspace_file", {"path": "result.txt", "content": EX42.TOTAL})]], EX42.TOTAL)
    monkeypatch.setitem(EX42.SCRIPTS, EX42.SOLVE, typed)
    checks, _ = await EX42.offline("memory", "fake", tenant=tenancy(), quiet=True)
    assert _verdicts(checks)[0] is None, checks[0]
    assert _verdicts(checks)[1:] == [True] * (EX42_CHECKS - 1), "only the task check depends on it"


async def test_example_42_fails_the_task_check_when_the_answer_is_typed_beside_a_command(monkeypatch):
    """A command reads the files, but result.txt is written by hand, not by the program."""
    turns = [[("write_workspace_file", {"path": "solve.py", "content": EX42.SOLVER})],
             [("run_command", {"argv": ["python", "solve.py"]})],
             [("write_workspace_file", {"path": "result.txt", "content": EX42.TOTAL})]]
    monkeypatch.setitem(EX42.SCRIPTS, EX42.SOLVE, (turns, EX42.TOTAL))
    checks, _ = await EX42.offline("memory", "fake", tenant=tenancy(), quiet=True)
    assert _verdicts(checks)[0] is False, checks[0]


async def test_example_42_fails_the_budget_check_without_the_budget_check(monkeypatch):
    """The subject removed: the lease never refuses, so the runaway runs on."""
    from agentsdk.budget import BudgetLease

    asked = []

    def always(self):
        asked.append(self.node_id)
        return True

    monkeypatch.setattr(BudgetLease, "may_call", always)
    checks, _ = await EX42.offline("memory", "fake", tenant=tenancy(), quiet=True)
    assert asked, "the patch never fired"
    assert _verdicts(checks)[1] is False, checks[1]


async def test_example_42_fails_the_export_check_without_the_exports_approval(monkeypatch):
    """The subject removed: the preset's export no longer asks."""
    built = []
    original = EX42.general_agent

    def unasked(**fields):
        built.append(fields)
        return original(**{**fields, "export_approval": ApprovalPolicy.AUTO})

    monkeypatch.setattr(EX42, "general_agent", unasked)
    checks, _ = await EX42.offline("memory", "fake", tenant=tenancy(), quiet=True)
    assert built, "the patch never fired"
    assert _verdicts(checks)[2] is None, checks[2]


async def test_example_42_fails_the_plan_check_without_role_turns(monkeypatch):
    """The subject removed: the orchestrator is given no role_turns, so its children get 4."""
    made = []

    def without(model, image, registry, ceiling):
        made.append(model)
        child = general_subagent(model=model, image=image, registry=registry)
        return Orchestrator(roles={"general": child}, policy=BudgetPolicy(run_ceiling_usd=ceiling),
                            limits=SchedulerLimits(max_concurrent_subagents=4), context_policy=ContextPolicy())

    monkeypatch.setattr(EX42, "plan_orchestrator", without)
    checks, _ = await EX42.offline("memory", "fake", tenant=tenancy(), quiet=True)
    assert made, "the patch never fired"
    assert _verdicts(checks)[3] is False, checks[3]


@pytest.mark.docker
@pytest.mark.parametrize("store", STORES)
async def test_on_docker_example_42_holds_every_check(store):
    """FR-178 on DockerProvider: real python in the pinned image solves the task from its files."""
    checks, _ = await EX42.offline(store, "docker", tenant=tenancy(), quiet=True)
    assert _verdicts(checks) == [True] * EX42_CHECKS, checks


# =================================================================================================
# The pre-review's findings (F1 to F6)
# =================================================================================================


@pytest.mark.parametrize("files", [
    {"a": b"x" * (MAX_TRANSFER_BYTES - 1000)},  # contents fit; one header and the padding do not
    {f"d/f{i}": b"y" * 3000 for i in range(5000)},  # 15 MB of contents, a 17.9 MB tar
], ids=["one-file", "many-files"])
def test_files_whose_transfer_is_past_its_bound_are_refused_at_construction(files):
    """F1: the bound is the tar the manager sends, not the contents' sum."""
    from agentsdk.workspace.docker import read_tar
    from agentsdk.workspace.tools import files_tar

    with pytest.raises(ValueError, match="transfer"):
        WorkspaceSpec(image=FAKE_IMAGE, files=files)
    with pytest.raises(ValueError):
        read_tar(files_tar(files)), "the control: the transfer itself refuses this tar"


def test_files_whose_transfer_fits_land():
    """F1's control: just under the bound is accepted, and its tar passes the transfer."""
    from agentsdk.workspace.docker import read_tar
    from agentsdk.workspace.tools import files_tar

    files = {f"f{i}": b"z" * (3 * 2**20) for i in range(5)}
    space = WorkspaceSpec(image=FAKE_IMAGE, files=files)
    assert len(files_tar(space.files)) <= MAX_TRANSFER_BYTES
    read_tar(files_tar(space.files))


@pytest.mark.parametrize("model", [f"a:b:{MODEL}", f":{MODEL}"])
def test_a_model_is_read_at_its_first_colon_as_the_runner_reads_it(model):
    """F3: `a:b:known-model` is client `a` and model `b:known-model`, which no registry knows."""
    with pytest.raises(ValueError):
        preset(model=model)


async def test_a_shipped_mark_on_a_changed_text_is_recorded_as_custom():
    """F4: a spec changed after the factory made it records `custom`, never the shipped version."""
    import dataclasses

    agent = preset(run_command_approval=ApprovalPolicy.AUTO)
    changed = GeneralAgent(spec=dataclasses.replace(agent.spec, instructions="my own text"), budget=agent.budget,
                           max_turns=agent.max_turns, context_policy=agent.context_policy)
    assert changed.spec.preset == ("general", "general-v4"), "the mark itself is left as it was"
    kept, _, _, _ = await run_preset("postgres", [], agent=agent)
    result, _, _, _ = await run_preset("postgres", [], agent=changed)
    assert (await manifest_columns(kept.run_id))[0] == {"name": "general", "version": "general-v4"}
    assert (await manifest_columns(result.run_id))[0] == {"name": "general", "version": "custom"}


async def test_a_mark_that_names_no_shipped_version_is_recorded_as_given():
    agent = preset(run_command_approval=ApprovalPolicy.AUTO)
    import dataclasses

    own = GeneralAgent(spec=dataclasses.replace(agent.spec, preset=("mine", "v7")), budget=agent.budget,
                       max_turns=agent.max_turns, context_policy=agent.context_policy)
    result, _, _, _ = await run_preset("postgres", [], agent=own)
    assert (await manifest_columns(result.run_id))[0] == {"name": "mine", "version": "v7"}


def test_an_empty_role_turns_is_none_given():
    """F5: it names no role, so the orchestrator's run records NULL."""
    assert _orchestrator(role_turns={}).config(tenant_id="t", project_id="p").role_turns is None


@pytest.mark.parametrize("own", [{"general": SUBAGENT_TURNS}, None])
@pytest.mark.parametrize("given", [{"general": 3}, {"general": 3, "other": 7}, None, {}])
def test_the_orchestrators_config_refuses_role_turns(own, given):
    """Round 1 F-A: the briefings read the orchestrator's own role_turns, so config() taking another
    would record turns no child was given; every value is refused, None and {} included."""
    with pytest.raises(ValueError, match="role_turns"):
        _orchestrator(role_turns=own).config(tenant_id="t", project_id="p", role_turns=given)


@pytest.mark.parametrize("own, briefed", [({"general": SUBAGENT_TURNS}, {"general-subagent": {20}, "other": {4}}),
                                          ({"general": 3, "other": 7}, {"general-subagent": {3}, "other": {7}}),
                                          (None, {"general-subagent": {4}, "other": {4}})])
async def test_the_recorded_role_turns_are_the_turns_each_child_was_briefed_with(own, briefed):
    """Round 1 F-A: the record and the briefings read one value, checked against the runs rows."""
    result, _, children, _ = await _plan("postgres", role_turns=own)
    assert await _child_turns(children) == briefed
    recorded = (await manifest_columns(result.run_id))[1]
    assert recorded == own
    for agent_id, turns in briefed.items():
        role = "general" if agent_id == "general-subagent" else agent_id
        assert turns == {(recorded or {}).get(role, 4)}, (agent_id, recorded)


@pytest.mark.parametrize("field, value", [("egress", None), ("egress", "example.com:443"), ("registry", {}),
                                          ("registry", "default")])
def test_an_egress_or_registry_of_the_wrong_kind_is_a_value_error(field, value):
    """F6."""
    with pytest.raises(ValueError, match=field):
        preset(**{field: value})


async def test_example_42_fails_the_task_check_when_the_files_never_landed_and_the_model_wrote_its_own(monkeypatch):
    """F2: the given files never reach the workspace; the model writes the numbers itself and solves
    the task with them. The check must still fail: the files were not the workspace's."""
    import io
    import tarfile

    import agentsdk.workspace.manager as manager

    landed = []

    def empty(files):
        landed.append(sorted(files))
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w"):
            pass
        return buffer.getvalue()

    monkeypatch.setattr(manager, "files_tar", empty)
    own = [[("write_workspace_file", {"path": "data/numbers.txt", "content": EX42.NUMBERS.decode()})],
           [("write_workspace_file", {"path": "solve.py", "content": EX42.SOLVER})],
           [("run_command", {"argv": ["python", "solve.py"]})]]
    monkeypatch.setitem(EX42.SCRIPTS, EX42.SOLVE, (own, EX42.TOTAL))
    checks, _ = await EX42.offline("memory", "fake", tenant=tenancy(), quiet=True)
    assert landed, "the patch never fired"
    assert _verdicts(checks)[0] is False, checks[0]


def test_files_past_the_workspaces_own_bound_are_refused():
    """FR-175: counted against max_workspace_bytes before anything is written, whatever the transfer allows."""
    with pytest.raises(ValueError, match="max_workspace_bytes"):
        WorkspaceSpec(image=FAKE_IMAGE, max_workspace_bytes=100, files={"a": b"x" * 101})
    assert WorkspaceSpec(image=FAKE_IMAGE, max_workspace_bytes=100, files={"a": b"x" * 100}).files["a"] == b"x" * 100


async def test_example_42_fails_the_task_check_when_the_workspace_records_no_files(monkeypatch):
    """F2: the files land, but the workspace's record does not name them (FR-175's record)."""
    recorded = []
    original = WorkspaceSpec.record

    def unnamed(self):
        found = original(self)
        recorded.append("files" in found)
        found.pop("files", None)
        return found

    monkeypatch.setattr(WorkspaceSpec, "record", unnamed)
    checks, _ = await EX42.offline("memory", "fake", tenant=tenancy(), quiet=True)
    assert True in recorded, "the patch never removed a record of files"
    assert _verdicts(checks)[0] is False, checks[0]
