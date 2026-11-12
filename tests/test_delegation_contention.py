"""M54 gate: Phase 5A increment 2's exit signal (FR-190, AC-112, P5A-D4).

Example 45 is the exit signal: delegating general agents of two tenancies, started shuffled with
random delays, each child within its reservation and its files, none at depth 2, every spawn under
approval waiting, nothing crossing tenancy, and a cancelled parent's child cancelled. The gates hold
it on `FakeProvider`, on both stores; a model that does nothing fails every check whose subject is
its work (DECISION-a7d6a1f5). Each check fails without its subject (AC-112): the child's reservation
check, the files bound, the child's lack of the delegation tool, the spawn's approval, the tenancy
filter, and the cancellation of children -- and each removal fails the check that names it, by that
check's own clause.

Tests marked `docker` run before review with `pytest -m docker`: example 45 ten times on each store
on `DockerProvider` (DECISION-61bead9f). Every row and Docker object a test writes is removed by its
own tenancies.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import os
import subprocess
import sys
import uuid
from dataclasses import replace
from pathlib import Path

import psycopg
import pytest

from agentsdk import api, delegation, presets, subagents
from agentsdk.budget import BudgetGovernor
from agentsdk.config import normalise_database_url
from agentsdk.delegation import DELEGATE_TOOL, Delegation
from agentsdk.session import InMemorySessionStore
from agentsdk.tools import ApprovalPolicy
from agentsdk.workspace import manager as mgr
from agentsdk.workspace import records
from agentsdk.workspace import tools as workspace_tools
from tests.test_general_contention import _rewritten

REPO = Path(__file__).resolve().parent.parent
DSN = normalise_database_url(os.environ.get("DATABASE_URL"))
PREFIX = "SYN-m54-"
STORES = ["memory", "postgres"]
TENANTS: list[str] = []
NL = chr(10)


def _example(name: str):
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py").replace("-", "_"), REPO / "scripts" / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EX45 = _example("45_delegation_contention.py")


def _docker_leftovers(tenant: str) -> None:
    """Removes what a mutant left on the daemon for this test's tenancies: never anything else."""
    for owner in (tenant, tenant + EX45.ELSEWHERE):
        for kind, listing in (("container", ["ps", "-aq"]), ("volume", ["volume", "ls", "-q"]),
                              ("network", ["network", "ls", "-q"])):
            found = subprocess.run(["docker", *listing, "--filter", f"label=agentsdk.tenant={owner}"],
                                   capture_output=True, text=True, timeout=60).stdout.split()
            if found:
                subprocess.run(["docker", kind, "rm", "-f", *found], capture_output=True, timeout=120)


@pytest.fixture(autouse=True)
def remove_what_the_test_wrote():
    TENANTS.clear()
    yield
    if TENANTS and subprocess.run(["docker", "version"], capture_output=True).returncode == 0:
        for tenant in TENANTS:
            _docker_leftovers(tenant)
    if DSN and TENANTS:
        owners = [*TENANTS, *(t + EX45.ELSEWHERE for t in TENANTS)]
        with psycopg.connect(DSN, autocommit=True) as conn:
            ids = [r[0] for r in conn.execute("SELECT run_id FROM runs WHERE tenant_id = ANY(%s)", (owners,))]
            for table in ("workspace_snapshots", "workspace_connections", "workspace_commands", "workspaces"):
                conn.execute(f"DELETE FROM {table} WHERE tenant_id = ANY(%s)", (owners,))
            conn.execute("DELETE FROM interruption_decisions WHERE interruption_id IN (SELECT interruption_id"
                         " FROM interruptions WHERE run_id = ANY(%s))", (ids,))
            conn.execute("DELETE FROM interruptions WHERE run_id = ANY(%s)", (ids,))
            conn.execute("DELETE FROM standing_decisions WHERE run_id = ANY(%s)", (ids,))
            conn.execute("DELETE FROM artifacts WHERE tenant_id = ANY(%s)", (owners,))
            for table in ("plan_node_states", "plan_versions", "run_events", "messages", "execution_manifests"):
                conn.execute(f"DELETE FROM {table} WHERE run_id = ANY(%s)", (ids,))
            while conn.execute("DELETE FROM runs r WHERE run_id = ANY(%s) AND NOT EXISTS "
                               "(SELECT 1 FROM runs c WHERE c.parent_run_id = r.run_id)", (ids,)).rowcount:
                pass


def tenancy() -> str:
    tenant = f"{PREFIX}{uuid.uuid4().hex[:10]}"
    TENANTS.append(tenant)
    return tenant


async def example_45(store: str, provider_name: str = "fake", **options):
    """Example 45's checks as {label: (passed, why)}."""
    checks, _ = await EX45.offline(store, provider_name, tenant=tenancy(), quiet=True, **options)
    return {label: (passed, why) for label, passed, why in checks}


def failed(checks: dict) -> dict:
    return {label: why for label, (passed, why) in checks.items() if passed is not True}


BOUNDS = ("each child stayed within its reservation and each parent within its ceiling, each within one call's cost "
          "per running agent; each runaway's child was stopped by its reservation and the runaway by its ceiling")
FILES = ("a child's workspace held only the files its call named; a path outside the parent's workspace, a link and "
         "a file past the transfer's bound were each refused, spawning nothing")
DEPTH = "a child's delegate_task was refused as an unknown tool, and no run reached depth 2"
APPROVAL = "every spawn under REQUIRE_APPROVAL waited for its decision, and the refused one started nothing"
WORKSPACES = ("neither tenancy could release or snapshot the other's workspaces: a parent's kept one, or a child's "
              "while it ran; each could its own")
ROWS = "neither tenancy read the other's rows: workspaces, commands and interruptions, a parent's or a child's"
ANSWERS = "neither tenancy read the other's answers: a parent's or a child's history"
CANCELLED = "cancelling a parent ended its running child cancelled, its long command killed by the cancellation, and the child's workspace was removed"
NOTHING_LEFT = "after aclose() nothing of either tenancy remains: workspace, network, volume or snapshot"
EVERY = {BOUNDS, FILES, DEPTH, APPROVAL, WORKSPACES, ROWS, ANSWERS, CANCELLED, NOTHING_LEFT}


# =================================================================================================
# FR-190 on FakeProvider, both stores
# =================================================================================================


@pytest.mark.parametrize("store", STORES)
async def test_example_45_holds_every_check_offline(store):
    checks = await example_45(store, seed=45)
    assert set(checks) == EVERY and not failed(checks), failed(checks)


@pytest.mark.parametrize("seed", [1, 2, 3])
async def test_example_45_holds_whatever_the_start_order(seed):
    checks = await example_45("memory", seed=seed)
    assert set(checks) == EVERY and not failed(checks), failed(checks)


async def test_example_45_passes_nothing_when_no_model_acts(monkeypatch):
    """A subject that never happened fails its check (DECISION-a7d6a1f5): with no call made, no child
    runs, nothing is refused or asked, and no workspace is made, so every check is unexercised."""
    async def declines(self, request):
        return EX45._reply(content="I will not do that.")

    monkeypatch.setattr(EX45.ScriptedModel, "send", declines)
    checks = await example_45("memory", seed=5)
    assert set(checks) == EVERY
    assert all(passed is None for passed, _ in checks.values()), checks


def test_the_live_tasks_give_each_attempt_verbatim():
    """FR-190: live, the model is given each hostile call verbatim, as the TURNS line the scripted model
    reads: the delegations naming a path outside, a link and a large file, and the link and file it
    makes first."""
    turns = EX45.turns_of(EX45.PIECE)
    assert turns[0] == [[DELEGATE_TOOL, {"objective": EX45.PIECE_CHILD, "files": [EX45.NAMED]}]]
    assert turns[1] == [["run_command", {"argv": ["ln", "-s", "/etc/passwd", EX45.LINK]}]]
    assert turns[2] == [["run_command", {"argv": ["dd", "if=/dev/zero", f"of={EX45.BIG}", "bs=1000000", "count=17"]}]]
    assert [t[0][1]["files"] for t in turns[3:]] == [[EX45.OUTSIDE], [EX45.LINK], [EX45.BIG]]
    assert 17_000_000 > 16 * 2**20, "the file is past the transfer's bound"
    assert "copying every argument verbatim" in EX45.PIECE
    assert EX45.turns_of(EX45.turns_of(EX45.NESTER)[0][0][1]["objective"]) == [[[DELEGATE_TOOL, {
        "objective": "Answer with the word nested."}]]]
    assert [t[0][1]["objective"] for t in EX45.turns_of(EX45.ASKER)] == [EX45.HELLO, EX45.GOODBYE]
    assert EX45.turns_of(EX45.turns_of(EX45.CANCELLER)[0][0][1]["objective"]) == [[["run_command", {
        "argv": ["sleep", EX45.SLEEP_SECONDS]}]]]


def test_the_runaways_ceilings_press_its_childs_reservation_and_its_own():
    """Scripted: the child's reservation (a quarter of 0.90) is below three calls, and the runaway's own
    ceiling is reached by its calls and its child's; live (DECISION-61bead9f), between one call and two."""
    from decimal import Decimal

    from agentsdk.budget import BudgetPolicy

    fraction = BudgetPolicy(run_ceiling_usd=Decimal(1)).delegation_fraction
    call = Decimal("0.10")
    share = (EX45.SCRIPTED_CEILINGS["runaway"] - call) * fraction
    assert 2 * call < share < 3 * call
    # Between a first call (at most 0.00052 measured live) and that call with its child's (at least 0.00072).
    assert Decimal("0.00052") < EX45.LIVE_CEILINGS["runaway"] < Decimal("0.00072") and EX45.LIVE_MODEL == "openai.gpt-4o-mini"
    # Pre-review F5: three live runs of two tenancies, every ceiling spent in full, stay within USD 1.
    assert 3 * 2 * sum(EX45.LIVE_CEILINGS.values()) < Decimal(1)


async def test_a_runaway_whose_childs_work_fits_its_reservation_fails_the_stop_check(monkeypatch):
    """Its subject: priced calls; its property: the reservation stopped the child. A ceiling whose
    quarter pays for the child's whole work ends the child at its turn limit, and the check names that."""
    monkeypatch.setitem(EX45.SCRIPTED_CEILINGS, "runaway", EX45.SCRIPTED_CEILINGS["runaway"] * 50)
    checks = await example_45("memory", seed=8)
    passed, why = checks[BOUNDS]
    assert passed is False and "the runaway's child ended max_turns_exceeded" in why, why


async def test_a_runaway_that_stops_working_fails_the_stop_check(monkeypatch):
    """The child is stopped by its reservation, but the runaway answers after it, below its ceiling."""
    monkeypatch.setitem(EX45.TASKS, "runaway", EX45.told("Hand a helper its work.", [[EX45.delegate(EX45.RUNAWAY_CHILD)]]))
    checks = await example_45("memory", seed=8)
    passed, why = checks[BOUNDS]
    assert passed is False and "the runaway ended completed" in why, why


async def test_a_child_that_existed_before_its_spawns_decision_fails_the_approval_check(monkeypatch):
    """The clause is the operator's own count when it was asked: one child more than the asking run had
    then fails it."""
    import textwrap

    source = textwrap.dedent(inspect.getsource(EX45.operator))
    old = "len(runner.started_under(item.run_id))"
    assert source.count(old) == 1
    scope = dict(vars(EX45))
    exec(compile(source.replace(old, old + " + 1"), EX45.__file__, "exec"), scope)  # noqa: S102
    monkeypatch.setattr(EX45, "operator", scope["operator"])
    checks = await example_45("memory", seed=9)
    passed, why = checks[APPROVAL]
    assert passed is False and "a child existed before its spawn's decision" in why, why


@pytest.mark.parametrize("store", STORES)
async def test_a_cancel_while_a_commands_start_is_written_never_begins_it(monkeypatch, store):
    """M54 pre-review F1: a cancel landing while WorkspaceCommandStarted was written was absorbed by the
    store call, and the command then ran to its end before the run stopped. Now the command never
    begins, and is recorded cancelled, as one killed is (FR-148)."""
    import json
    import time

    from agentsdk import RunConfig, RunStatus
    from agentsdk import handle as handle_module
    from agentsdk.events import EventType
    from agentsdk.workspace import WorkspaceSpec
    from tests.test_workspace_tools import FAKE_IMAGE, PROJECT, Killing, agent, commands_of, events, runner_for

    tenant = tenancy()
    provider = Killing()
    runner = runner_for(provider, store)
    handles, hits = [], []
    original = handle_module.StoredEmit._gated

    def gated(self, gate, event_type, payload):
        if event_type is EventType.WORKSPACE_COMMAND_STARTED:
            hits.append(event_type)
            handles[0].cancel()  # from the store's worker thread, as the write begins
        return original(self, gate, event_type, payload)

    monkeypatch.setattr(handle_module.StoredEmit, "_gated", gated)
    handles.append(await runner.start(
        agent(WorkspaceSpec(image=FAKE_IMAGE, run_command_approval=ApprovalPolicy.AUTO)),
        json.dumps([[["run_command", {"argv": ["nap", "30"]}]]]), RunConfig(tenant_id=tenant, project_id=PROJECT)))
    began = time.monotonic()
    result = await asyncio.wait_for(handles[0].result(), 60)
    assert hits == [EventType.WORKSPACE_COMMAND_STARTED], "the cancel never landed during the start's write"
    assert result.status is RunStatus.CANCELLED and time.monotonic() - began < 10
    assert not provider.started.is_set(), "the command began"
    [ended] = events(result, EventType.WORKSPACE_COMMAND_ENDED)
    assert (ended["killed"], ended["exit_code"]) == ("cancelled", None)
    assert await asyncio.to_thread(commands_of, runner, store, tenant, result.run_id) == [
        (["nap", "30"], None, "cancelled")]


def test_example_45_runs_offline_as_a_program():
    done = subprocess.run([sys.executable, str(REPO / "scripts" / "45_delegation_contention.py"), "--offline"],
                          capture_output=True, text=True, timeout=600, cwd=REPO)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    assert done.stdout.count("[PASS]") == len(EVERY) and "[FAIL]" not in done.stdout, done.stdout[-3000:]
    # The example's own tenancies, written in memory only, leave nothing to remove.


# =================================================================================================
# AC-112: each check fails without its subject
# =================================================================================================


FIRED: list[str] = []


def without(monkeypatch, subject: str) -> None:
    """Takes `subject` away, as AC-112 names it."""
    FIRED.clear()
    if subject == "child's reservation check":
        # A delegated child's lease is checked against its run's ceiling alone, not its reservation.
        _rewritten(monkeypatch, BudgetGovernor, "_may_call_locked", "if reserved_usd is not None:",
                   "if reserved_usd is not None and lease.parent is None:")
    elif subject == "files bound":
        original = delegation.child_spec

        def every_file(agent, files):
            FIRED.append(agent.id)
            return original(agent, {**EX45.FILES, **files})

        monkeypatch.setattr(delegation, "child_spec", every_file)
    elif subject == "child's lack of the delegation tool":
        original = delegation.child_spec

        def widen(agent, files):
            FIRED.append(agent.id)
            spec = original(agent, files)
            return replace(spec, tool_profile=(*spec.tool_profile, DELEGATE_TOOL), delegate=Delegation(agent=agent))

        monkeypatch.setattr(delegation, "child_spec", widen)
        monkeypatch.setattr(api, "_refuse_delegation", lambda spec, config: None)
    elif subject == "spawn's approval":
        # Pre-review F6: in the library, not the example's arguments -- the delegation tool's spec ignores
        # the approval it is given, so the asker's REQUIRE_APPROVAL never reaches the executor.
        original = delegation._spec

        def unasked(approval):
            if approval is ApprovalPolicy.REQUIRE_APPROVAL:
                FIRED.append(approval)
            return original(ApprovalPolicy.AUTO)

        monkeypatch.setattr(delegation, "_spec", unasked)
    elif subject == "WorkspaceManager's key by tenant and project":
        _rewritten(monkeypatch, mgr.WorkspaceManager, "release",
                   "and (space.tenant_id, space.project_id) == (tenant_id, project_id)):", "):")
        _rewritten(monkeypatch, mgr.WorkspaceManager, "snapshot",
                   "and (s.tenant_id, s.project_id) == (tenant_id, project_id)), None)", "), None)")
    elif subject == "release and snapshot refused to everybody":
        # Round 1 F-B: refused to the owner too, which the other tenancy's refusals alone cannot show.
        _rewritten(monkeypatch, mgr.WorkspaceManager, "release",
                   "and (space.tenant_id, space.project_id) == (tenant_id, project_id)):",
                   "and (space.tenant_id, space.project_id) == (tenant_id, project_id) and False):")
        _rewritten(monkeypatch, mgr.WorkspaceManager, "snapshot",
                   "and (s.tenant_id, s.project_id) == (tenant_id, project_id)), None)",
                   "and (s.tenant_id, s.project_id) == (tenant_id, project_id) and False), None)")
    elif subject == "store's tenant predicate (memory)":
        _rewritten(monkeypatch, records.InMemoryWorkspaceStore, "commands",
                   "if (c.tenant_id, c.project_id) == (tenant_id, project_id)", "if c.project_id == project_id")
    elif subject == "store's tenant predicate (postgres)":
        from agentsdk.postgres import PostgresWorkspaceStore

        _rewritten(monkeypatch, PostgresWorkspaceStore, "commands", "WHERE tenant_id = %s AND project_id = %s",
                   "WHERE %s::text IS NOT NULL AND project_id = %s")
    elif subject == "session store's tenant key (memory)":
        _rewritten(monkeypatch, InMemorySessionStore, "bind", "(scope.tenant_id, scope.project_id, scope.run_id)",
                   "('one tenant', scope.project_id, scope.run_id)")
    elif subject == "session store's tenant predicate (postgres)":
        from agentsdk.postgres import PostgresSessionStore

        _rewritten(monkeypatch, PostgresSessionStore, "history",
                   "WHERE run_id = %s AND tenant_id = %s AND project_id = %s",
                   "WHERE run_id = %s AND %s::text IS NOT NULL AND project_id = %s")
    elif subject == "cancellation of children":
        # The child runs on, shielded from its parent's cancellation.
        _rewritten(monkeypatch, subagents.SubagentPool, "_run_child",
                   "result = await self._runner.run(agent, task, config)",
                   "result = await asyncio.shield(self._runner.run(agent, task, config))")
    elif subject == "path pattern":
        # Beyond AC-112's list: the delegation's path schema, so a path outside reaches the tool.
        loose = {**workspace_tools.PATH, "pattern": ".+"}
        original = delegation._spec

        def spec(approval):
            FIRED.append(approval)
            made = original(approval)
            return replace(made, input_schema={**made.input_schema, "properties": {
                **made.input_schema["properties"], "files": {**made.input_schema["properties"]["files"],
                                                             "items": loose}}})

        monkeypatch.setattr(delegation, "_spec", spec)
    elif subject == "transfer bound":
        # Beyond AC-112's list: the 16 MiB bound, both where the export is read and where the child's
        # starting files are measured, which share the one constant.
        from agentsdk.workspace import docker

        # Not in PATCHED: a constant is read, never called, so its effect is the clause's own text --
        # the big file not refused (pre-review F4).
        monkeypatch.setattr(docker, "MAX_TRANSFER_BYTES", 2**31)
    elif subject == "run's budget check":
        # Beyond AC-112's list: every lease may call, so only the turn limits stop a run.
        from agentsdk.budget import BudgetLease

        def always(self):
            FIRED.append(self.node_id)
            return True

        monkeypatch.setattr(BudgetLease, "may_call", always)
    elif subject == "children's spend in the run's":
        # Beyond AC-112's list (M54 mutation run): a child's calls charge its lease, never the run's
        # spend, and its reservation does not stop it, so the ceiling holds its parent's calls alone.
        # The reservation alone gone, the run's ceiling stops the child; the run's spend alone gone, the
        # parent's reservation keeps the whole within one call per agent: only both let the child's
        # spend past the ceiling while its parent's own stays inside.
        _rewritten(monkeypatch, BudgetGovernor, "_charge", "elif self._spent_usd is not None:",
                   "elif self._spent_usd is not None and self._leases[node_id].parent is None:")
        _rewritten(monkeypatch, BudgetGovernor, "_may_call_locked", "if reserved_usd is not None:",
                   "if reserved_usd is not None and lease.parent is None:")
    elif subject == "child's own reservation":
        # Beyond AC-112's list: the child runs on its parent's lease, not the reservation made for it.
        _rewritten(monkeypatch, delegation, "delegate_tool", "agent=spec, lease=child, depth=1",
                   "agent=spec, lease=lease, depth=1")
    elif subject == "the share moved once":
        # Beyond AC-112's list: the child's share leaves its parent's reservation twice, so the parent
        # is stopped short of its ceiling.
        _rewritten(monkeypatch, BudgetGovernor, "reserve_child",
                   "self._reservations[parent_node_id][0] = usd - share_usd",
                   "self._reservations[parent_node_id][0] = usd - 2 * share_usd")
    elif subject == "child's files written":
        # Beyond AC-112's list: a workspace started with one file -- a child's, here -- is recorded with
        # it, but the file never lands.
        _rewritten(monkeypatch, mgr.RunWorkspace, "_creation", "    if self.spec.files:",
                   "    if self.spec.files and len(self.spec.files) != 1:")
    elif subject == "the transfer of only the named files":
        # Pre-review F2: the workspace record names the one file, but the transfer writes the parent's
        # other files beside it, so only what the child sees can show it.
        original = mgr.files_tar

        def more(files):
            if list(files) == [EX45.NAMED]:
                FIRED.append("files")
                files = {**EX45.FILES, **files}
            return original(files)

        monkeypatch.setattr(mgr, "files_tar", more)
    elif subject == "the command run out":
        # Pre-review F1's symptom: the command runs out its run's cancel, recorded as it ended, and
        # only then is the run cancelled.
        _rewritten(monkeypatch, mgr.RunWorkspace, "_run", "outcome = await asyncio.shield(running)",
                   "outcome = await through(running)")
    elif subject in ("a kill that raises", "a kill that kills nothing"):
        # Round 1 F-A: the provider's kill fails, raising or not, so the command is abandoned at the
        # kill's bound -- and recorded killed "cancelled" all the same.
        from agentsdk.workspace import FakeProvider

        async def kill(self, handle, command_id):
            FIRED.append(command_id)
            if subject == "a kill that raises":
                raise RuntimeError("the kill failed")

        monkeypatch.setattr(FakeProvider, "kill", kill)
    elif subject == "the run's ceiling on time":
        # Pre-review F3: the run's ceiling enforced two calls late, and a parent's own reservation never
        # checked, so a run overshoots by more than one call per agent running at once.
        _rewritten(monkeypatch, BudgetGovernor, "_within_ceiling", "self._spent_usd >= self.policy.run_ceiling_usd",
                   "self._spent_usd >= self.policy.run_ceiling_usd + 2 * Decimal('0.10')")
        _rewritten(monkeypatch, BudgetGovernor, "_may_call_locked", "if reserved_usd is not None:",
                   "if reserved_usd is not None and lease.parent is not None:")
    elif subject == "the parent's cancellation":
        from agentsdk.handle import RunHandle

        def ignored(self, reason=None):
            FIRED.append("cancel")

        monkeypatch.setattr(RunHandle, "cancel", ignored)
    elif subject == "a cancelled run's workspace end":
        # Beyond AC-112's list: a run cancelled leaves its workspace for aclose() to find.
        _rewritten(monkeypatch, api.Runner, "_cancelled", "await self._end_workspace(scope.run_id, events, control)",
                   "pass")
    elif subject == "Runner.aclose()":
        from agentsdk import Runner

        async def keeps_everything(self):
            FIRED.append("aclose")

        monkeypatch.setattr(Runner, "aclose", keeps_everything)
    else:
        raise AssertionError(subject)


NAMED = {"child's reservation check": BOUNDS, "files bound": FILES, "child's lack of the delegation tool": DEPTH,
         "spawn's approval": APPROVAL, "WorkspaceManager's key by tenant and project": WORKSPACES,
         "store's tenant predicate (memory)": ROWS, "store's tenant predicate (postgres)": ROWS,
         "session store's tenant key (memory)": ANSWERS, "session store's tenant predicate (postgres)": ANSWERS,
         # No link removal: an absolute link is refused three times over -- the transfer's member kinds,
         # its path filter, and the tool's one-regular-file check -- so no one removal reaches it.
         "cancellation of children": CANCELLED, "path pattern": FILES, "transfer bound": FILES,
         "run's budget check": BOUNDS, "Runner.aclose()": NOTHING_LEFT,
         "children's spend in the run's": BOUNDS, "child's own reservation": BOUNDS, "the share moved once": BOUNDS,
         "child's files written": FILES, "the parent's cancellation": CANCELLED,
         "a cancelled run's workspace end": CANCELLED, "the transfer of only the named files": FILES,
         "the command run out": CANCELLED, "a kill that raises": CANCELLED, "a kill that kills nothing": CANCELLED,
         "release and snapshot refused to everybody": WORKSPACES,
         "the run's ceiling on time": BOUNDS}
# The clause each removal fails, from the example's own wording: the check fails for its subject's
# reason, not by another clause that happens to fail with it.
BECAUSE = {"child's reservation check": lambda why: "against its reservation of" in why,
           "files bound": lambda why: "the child's workspace recorded the files" in why,
           "child's lack of the delegation tool": lambda why: "was not refused as unknown" in why,
           "spawn's approval": lambda why: "0 spawn(s) asked" in why,
           "WorkspaceManager's key by tenant and project": lambda why: "released True" in why,
           "store's tenant predicate (memory)": lambda why: "read commands of" in why,
           "store's tenant predicate (postgres)": lambda why: "read commands of" in why,
           "session store's tenant key (memory)": lambda why: "message(s) of the other tenancy's run" in why,
           "session store's tenant predicate (postgres)": lambda why: "message(s) of the other tenancy's run" in why,
           # Shielded, the child runs on past its sleep, and its next call ends it however its run allows.
           "cancellation of children": lambda why: "the child ended (" in why and "'cancelled'" not in why,
           "path pattern": lambda why: "a path outside the parent's workspace was not refused as ToolValidationError"
                                       in why,
           "transfer bound": lambda why: "a file past the transfer's bound was not refused as ToolExecutionError" in why,
           "run's budget check": lambda why: "runaway: spent" in why and "agent(s) running at once" in why,
           "Runner.aclose()": lambda why: why.startswith("remained: {"),
           "children's spend in the run's": lambda why: "runaway: spent" in why and "agent(s) running at once" in why,
           "child's own reservation": lambda why: "not one delegated reservation of its own" in why,
           "the share moved once": lambda why: "the runaway stopped at" in why and "short of" in why,
           "child's files written": lambda why: "the child's workspace held {'.': [], 'data': None}" in why,
           "the parent's cancellation": lambda why: "the parent ended completed" in why,
           "the transfer of only the named files": lambda why: "'.': ['NOTES.md', 'data']" in why
                                                   and "'data': ['a.txt', 'b.txt']" in why,
           # Run out, it ends as the cancel finds it: marked cancelled, with no kill error, 30 s on.
           "the command run out": lambda why: "[('cancelled', None, False)], not killed" in why,
           "a kill that raises": lambda why: "[('cancelled', 'RuntimeError', False)], not killed" in why,
           "a kill that kills nothing": lambda why: "[('cancelled', None, False)], not killed" in why,
           "release and snapshot refused to everybody":
               lambda why: "its own kept workspace: snapshot taken and released WorkspaceUnavailable, released False, "
                           "still there True" in why,
           "the run's ceiling on time": lambda why: "runaway: spent 1.2000 against 1.00" in why,
           "a cancelled run's workspace end": lambda why: "the child's workspace rows say removed [False], listed True"
                                              in why}
STORE_OF = {"store's tenant predicate (postgres)": "postgres", "session store's tenant predicate (postgres)": "postgres"}
PATCHED = {"files bound", "child's lack of the delegation tool", "spawn's approval", "path pattern", "run's budget check", "Runner.aclose()", "the parent's cancellation",
           "the transfer of only the named files", "a kill that raises", "a kill that kills nothing"}


@pytest.mark.parametrize("subject", list(NAMED))
async def test_on_the_fake_each_check_fails_without_its_subject(monkeypatch, subject):
    without(monkeypatch, subject)
    checks = await example_45(STORE_OF.get(subject, "memory"), seed=3)
    if subject in PATCHED:
        assert FIRED, "the patch never fired"
    passed, why = checks[NAMED[subject]]
    assert passed is False and BECAUSE[subject](why or ""), (subject, passed, why)


async def test_the_bounds_slack_counts_the_children_one_turn_started_together(monkeypatch):
    """Pre-review F3: two delegations in one turn run at once (FR-44's parallel batch), so the agents
    running together are two, and the check's slack is two calls, not one per child ever started."""
    # The nester, at the runaway's ceiling, hands two helpers their work in one turn; with no run's budget
    # check it overspends, while the runaway's own work fits a ceiling fifty times its own.
    twice = [EX45.delegate(EX45.RUNAWAY_CHILD), EX45.delegate(EX45.RUNAWAY_CHILD)]
    monkeypatch.setitem(EX45.TASKS, "nester", EX45.told("Hand two helpers their work, then keep working.",
                                                        [twice, *[[EX45.command("echo", "tick")]] * 4]))
    monkeypatch.setitem(EX45.SCRIPTED_CEILINGS, "nester", EX45.SCRIPTED_CEILINGS["runaway"])
    monkeypatch.setitem(EX45.SCRIPTED_CEILINGS, "runaway", EX45.SCRIPTED_CEILINGS["runaway"] * 50)
    without(monkeypatch, "run's budget check")
    checks = await example_45("memory", seed=8)
    passed, why = checks[BOUNDS]
    assert passed is False and "nester: spent" in (why or "") and "2 child(ren), 2 agent(s) running at once" in why, why


def test_the_bounds_slack_is_one_cost_per_agent_running_at_once_capped_by_the_limit():
    """Round 1 F-C: the arithmetic itself, which the messages above print whatever it is."""
    one, tenth = EX45.Decimal("1.00"), EX45.Decimal("0.10")
    assert [EX45.slack(one, tenth, n, 4) for n in (1, 2, 3, 4, 6)] == [
        EX45.Decimal(v) for v in ("1.10", "1.20", "1.30", "1.40", "1.40")]
    assert EX45.slack(one, EX45.Decimal(0), 3, 4) == one


async def test_the_bounds_slack_is_the_runs_own_limit(monkeypatch):
    """Round 1 F-C: the check asks slack() with the Runner's own max_concurrent_subagents."""
    asked = []
    original = EX45.slack

    def recorded(ceiling, largest, together, limit):
        asked.append(limit)
        return original(ceiling, largest, together, limit)

    monkeypatch.setattr(EX45, "slack", recorded)
    checks = await example_45("memory", seed=3)
    from agentsdk.scheduler import DEFAULT_MAX_CONCURRENT_SUBAGENTS

    assert asked and set(asked) == {DEFAULT_MAX_CONCURRENT_SUBAGENTS} and checks[BOUNDS][0] is True, asked


async def test_rows_is_unexercised_when_no_child_has_rows(monkeypatch):
    """Pre-review F7: the witness needs a child's rows, so a property that held only for parents is not
    a pass. Here a workspace started with other than the parents' files -- every child's -- records
    nothing."""
    _rewritten(monkeypatch, mgr.RunWorkspace, "_record",
               "return await self._call(getattr(self._manager.store(), method), *args)",
               f"return None if len(self.spec.files) != {len(EX45.FILES)} else "
               "await self._call(getattr(self._manager.store(), method), *args)")
    checks = await example_45("memory", seed=3)
    passed, why = checks[ROWS]
    assert passed is None and "none of its children's" in (why or ""), (passed, why)


def _clauses_out(monkeypatch, *olds: str) -> None:
    """Example 45's demonstrate() with each clause made never to fail: an `if` statement, or an
    (old, new) pair for a clause inside an expression."""
    import textwrap

    source = textwrap.dedent(inspect.getsource(EX45.demonstrate))
    for old in olds:
        old, new = old if isinstance(old, tuple) else (old, "if False:")
        assert source.count(old) == 1, old
        source = source.replace(old, new)
    scope = dict(vars(EX45))
    exec(compile(source, EX45.__file__, "exec"), scope)  # noqa: S102
    monkeypatch.setattr(EX45, "demonstrate", scope["demonstrate"])


# A later clause of a check, which its removal reaches only once the earlier clauses it trips first are
# taken out of the example (M54 mutation run): the removal, those clauses, and what the later one says.
LATER = {
    "the piece's children": ("transfer bound", FILES, (
        "if not all(e is not None and e.get(\"is_error\") is True and e.get(\"error_type\") == expected\n"
        "                       for _, e in attempts):",), "the piece started 2 children"),
    "the approved child": ("spawn's approval", APPROVAL, (
        "if len(found[\"spawns\"]) != 2 or not all(v.decision is not None and v.decision.principal == principal\n"
        "                                                 for v in found[\"spawns\"]):",
        "if found[\"asked\"] != [(HELLO, 0, True), (GOODBYE, 1, False)]:"), "2 children, not the one approved"),
    "the refused spawn's error": ("spawn's approval", APPROVAL, (
        "if len(found[\"spawns\"]) != 2 or not all(v.decision is not None and v.decision.principal == principal\n"
        "                                                 for v in found[\"spawns\"]):",
        "if found[\"asked\"] != [(HELLO, 0, True), (GOODBYE, 1, False)]:", "if len(found[\"kids\"]) != 1:"),
        "errors [False, False]"),
    "rows not its own": ("store's tenant predicate (memory)", ROWS, (
        ("readings[s.tenant].items() if ids & own_ids[s.other.tenant]), None)",
         "readings[s.tenant].items() if False), None)"),), "'s commands were not its own"),
    "the child's read": ("child's files written", FILES, (
        "if [found[\"listed\"].get(path) for path in (\".\", \"data\")] != [[\"data\"], [\"a.txt\"]]:",),
        f"the child never read {EX45.NAMED} with a command"),
    "depth 2": ("child's lack of the delegation tool", DEPTH, (
        ("if not all(r is not None and r.is_error and r.provenance.source_uri_or_hash == NOT_FOUND",
         "if not all(True"),), "runs reached depth 2"),
}


@pytest.mark.parametrize("clause", list(LATER))
async def test_on_the_fake_a_later_clause_fails_without_its_subject(monkeypatch, clause):
    subject, label, olds, says = LATER[clause]
    without(monkeypatch, subject)
    _clauses_out(monkeypatch, *olds)
    checks = await example_45("memory", seed=3)
    if subject in PATCHED:
        assert FIRED, "the patch never fired"
    passed, why = checks[label]
    assert passed is False and says in (why or ""), (clause, passed, why)


def test_each_removal_names_a_clause_that_exists_in_the_source():
    """The rewritten clauses are the code's own (_rewritten asserts each once); the patched defaults are
    the ones the removals change."""
    assert presets.general_agent.__kwdefaults__["delegate_approval"] is ApprovalPolicy.AUTO
    assert "delegate_approval" in inspect.signature(presets.general_agent).parameters
    assert "asyncio" in vars(subagents), "the shield removal needs the module's asyncio"
    assert workspace_tools.PATH["pattern"] != ".+"


# =================================================================================================
# On Docker, before review: ten times on each store
# =================================================================================================


@pytest.mark.docker
@pytest.mark.parametrize("repetition", range(10))
@pytest.mark.parametrize("store", STORES)
async def test_example_45_holds_every_check_on_docker(store, repetition):
    checks = await example_45(store, "docker", seed=4500 + repetition)
    assert set(checks) == EVERY and not failed(checks), failed(checks)
