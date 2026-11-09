"""M51 gate: Phase 5A increment 1's exit signal (FR-183, AC-109, P5A-D4).

Example 43 is the exit signal: general agents and general subagents of two tenancies, started
shuffled with random delays, each inside its budget, its sandbox, its approvals and its tenancy.
The gates hold it on `FakeProvider`, on both stores; a child that does nothing fails every check
whose subject is its work (DECISION-a7d6a1f5). Each check fails without its subject (AC-109): the
plain run's budget check, the export's approval, the workspace's egress check, and the tenancy
filter -- the `WorkspaceManager`'s key by tenant and project and the store's tenant predicate, each
removed on its own -- and each removal fails the check that names it, by that check's own clause.

Tests marked `docker` run before review with `pytest -m docker`: example 43 ten times on each store
on `DockerProvider` (DECISION-7830ca10). Every row and Docker object a test writes is removed by its
own tenancies.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import os
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path

import psycopg
import pytest

from agentsdk.budget import BudgetLease
from agentsdk.config import normalise_database_url
from agentsdk import presets
from agentsdk.tools import ApprovalPolicy
from agentsdk.workspace import egress as eg
from agentsdk.workspace import manager as mgr
from agentsdk.workspace import records

REPO = Path(__file__).resolve().parent.parent
DSN = normalise_database_url(os.environ.get("DATABASE_URL"))
PREFIX = "SYN-m51-"
STORES = ["memory", "postgres"]
TENANTS: list[str] = []
NL = chr(10)


def _example(name: str):
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py").replace("-", "_"), REPO / "scripts" / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EX43 = _example("43_general_contention.py")


def _docker_leftovers(tenant: str) -> None:
    """Removes what a mutant left on the daemon for this test's tenancies: never anything else."""
    for owner in (tenant, tenant + EX43.ELSEWHERE):
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
        owners = [*TENANTS, *(t + EX43.ELSEWHERE for t in TENANTS)]
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


async def example_43(store: str, provider_name: str = "fake", **options):
    """Example 43's checks as {label: (passed, why)}."""
    checks, _ = await EX43.offline(store, provider_name, tenant=tenancy(), quiet=True, **options)
    return {label: (passed, why) for label, passed, why in checks}


def failed(checks: dict) -> dict:
    return {label: why for label, (passed, why) in checks.items() if passed is not True}


ALLOWED = "each tenancy's prober fetched its allowed host through its proxy, recorded allowed by its command"
REFUSED = ("an IP literal, an unlisted host, a privately resolving name and the host's database port were each "
           "refused by the proxy and recorded by reason")
DIRECT = ("a direct connection, the host's database port directly and the other tenancy's neighbour were "
          "unreachable inside the workspace, each against a control")
STOPPED = "each tenancy's runaway was stopped by its own budget within one call's cost of its ceiling"
BOUNDED = "every run's spend stayed within its ceiling and one call's cost"
EXPORTS = "every export waited for its tenancy's approval and was recorded with its decision, none before it"
ROWS = "neither tenancy read the other's rows: commands, connections and interruptions"
CROSSING = "neither tenancy could decide the other's interruption or list it by its run"
WORKSPACE = "neither tenancy could release or snapshot the other's kept workspace"
SNAPSHOT = "neither tenancy could start a workspace from the other's snapshot or release it"
ARTIFACT = "neither tenancy could read the other's exported artifact"
NOTHING_LEFT = "after aclose() nothing of either tenancy remains: workspace, network, proxy, volume or snapshot"
OFFLINE = {ALLOWED, REFUSED, STOPPED, BOUNDED, EXPORTS, ROWS, CROSSING, WORKSPACE, SNAPSHOT, ARTIFACT, NOTHING_LEFT}
ON_DOCKER = OFFLINE | {DIRECT}


# =================================================================================================
# FR-183 on FakeProvider, both stores
# =================================================================================================


@pytest.mark.parametrize("store", STORES)
async def test_example_43_holds_every_check_offline(store):
    checks = await example_43(store, seed=43)
    assert set(checks) == OFFLINE and not failed(checks), failed(checks)


@pytest.mark.parametrize("seed", [1, 2, 3])
async def test_example_43_holds_whatever_the_start_order(seed):
    checks = await example_43("memory", seed=seed)
    assert set(checks) == OFFLINE and not failed(checks), failed(checks)


async def test_example_43_passes_nothing_whose_subject_is_a_childs_work_when_no_child_acts(monkeypatch):
    """A subject that never happened fails its check (DECISION-a7d6a1f5): with no child acting, no run
    reaches its ceiling, so the spend check is not exercised either (pre-review F3). Only the closing
    check may pass, when a workspace was made before aclose()."""
    async def declines(self, request):
        return EX43._reply(content="I will not do that.")

    monkeypatch.setattr(EX43.ScriptedChild, "send", declines)
    checks = await example_43("memory", seed=5)
    passed = {label for label, (ok, _) in checks.items() if ok is True}
    assert passed <= {NOTHING_LEFT}, passed
    assert checks[BOUNDED][0] is None and checks[BOUNDED][1] == "no run's spend reached its ceiling"
    for label in OFFLINE - {NOTHING_LEFT}:
        assert checks[label][0] is not True, label


async def test_a_runaway_that_ends_on_its_turn_limit_fails_the_budget_check(monkeypatch):
    """Its subject: priced calls; its property: the budget stopped it. A ceiling the runaway never
    reaches ends it at its turns, and the check names that."""
    monkeypatch.setitem(EX43.SCRIPTED_CEILINGS, "runaway", EX43.SCRIPTED_CEILINGS["task"] * 10)
    checks = await example_43("memory", seed=8)
    passed, why = checks[STOPPED]
    assert passed is False and "it ended completed" in why, why


async def test_a_fetch_the_store_did_not_record_fails_the_allowed_check(monkeypatch):
    """The probe's output alone is not the witness: the store's allowed connection by its command is."""
    monkeypatch.setattr(records.InMemoryWorkspaceStore, "connections",
                        lambda self, tenant_id, project_id, run_id=None: [])
    checks = await example_43("memory", seed=9)
    passed, why = checks[ALLOWED]
    assert passed is False and "no allowed connection to example.com recorded" in why, why


def test_the_live_tasks_give_each_attempt_verbatim():
    """FR-183: live, the model is given each hostile attempt verbatim: the probe's argv, every export
    and the runaway's command are in the tasks as the TURNS line the scripted model reads."""
    task = EX43.prober_task("10.0.0.9", "93.184.215.14")
    turns = EX43.turns_of(task)
    assert turns[0][0] == ["run_command", {"argv": EX43.probe_argv("10.0.0.9", "93.184.215.14")}]
    assert turns[-1][0] == ["export_workspace_file", {"path": "report.txt"}]
    assert "copying every argument verbatim" in task
    assert EX43.turns_of(EX43.RUNAWAY) == [[["run_command", {"argv": ["echo", "tick"]}]]] * EX43.RUNAWAY_TURNS
    for name in EX43.CHILDREN:
        assert EX43.turns_of(EX43.child_objective(name, "t"))[-1][0] == ["export_workspace_file", {"path": f"{name}.txt"}]


def test_a_probe_is_recognised_by_its_program_whatever_its_spacing_and_nothing_else():
    argv = EX43.probe_argv("10.0.0.9", "1.2.3.4")
    assert EX43.runs_probe(argv)
    assert EX43.runs_probe([*argv[:2], argv[2].replace(", ", ","), *argv[3:]])
    assert not EX43.runs_probe([*argv[:2], argv[2] + NL + "print(1)", *argv[3:]])
    assert not EX43.runs_probe(argv[:4]) and not EX43.runs_probe(None) and not EX43.runs_probe(["python", "-c", "("])


def test_example_43_runs_offline_as_a_program():
    done = subprocess.run([sys.executable, str(REPO / "scripts" / "43_general_contention.py"), "--offline"],
                          capture_output=True, text=True, timeout=600, cwd=REPO)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    assert done.stdout.count("[PASS]") == len(OFFLINE) and "[FAIL]" not in done.stdout, done.stdout[-3000:]
    # The example's own tenancies, written in memory only, leave nothing to remove.


# =================================================================================================
# AC-109: each check fails without its subject
# =================================================================================================


def _rewritten(monkeypatch, owner, name: str, old: str, new: str) -> None:
    """Replaces method `name` of `owner` with its own source, `old` made `new` exactly once."""
    function = getattr(owner, name)
    source = textwrap.dedent(inspect.getsource(function))
    assert source.count(old) == 1, (name, old)
    module = sys.modules[function.__module__]
    scope = dict(vars(module))
    exec(compile(source.replace(old, new), inspect.getsourcefile(function), "exec"), scope)  # noqa: S102
    monkeypatch.setattr(owner, name, scope[name])


class _Everything:
    def __contains__(self, item) -> bool:
        return True


def without(monkeypatch, subject: str) -> None:
    """Takes `subject` away, as AC-109 names it."""
    if subject == "plain run's budget check":
        monkeypatch.setattr(BudgetLease, "may_call", lambda self: True)
    elif subject == "export's approval":
        for preset in (presets.general_agent, presets.general_subagent):
            monkeypatch.setitem(preset.__kwdefaults__, "export_approval", ApprovalPolicy.AUTO)
    elif subject == "workspace's egress check":
        # The fake runs the proxy in process: its allow list holds every host. On Docker the same
        # line would be removed from the source the proxy runs (eg.SOURCE), as M47's tests do.
        real = eg.Proxy.__init__

        def init(self, *args, **kwargs):
            real(self, *args, **kwargs)
            self.allow = _Everything()

        monkeypatch.setattr(eg.Proxy, "__init__", init)
    elif subject == "WorkspaceManager's key by tenant and project":
        _rewritten(monkeypatch, mgr.WorkspaceManager, "release",
                   "and (space.tenant_id, space.project_id) == (tenant_id, project_id)):",
                   "):")
        _rewritten(monkeypatch, mgr.WorkspaceManager, "snapshot",
                   "and (s.tenant_id, s.project_id) == (tenant_id, project_id)), None)",
                   "), None)")
    elif subject == "store's tenant predicate (memory)":
        _rewritten(monkeypatch, records.InMemoryWorkspaceStore, "commands",
                   "if (c.tenant_id, c.project_id) == (tenant_id, project_id)", "if c.project_id == project_id")
    elif subject == "store's tenant predicate (postgres)":
        from agentsdk import postgres

        store = next(c for c in vars(postgres).values() if isinstance(c, type) and "commands" in vars(c)
                     and "workspace_commands" in inspect.getsource(vars(c)["commands"]))
        _rewritten(monkeypatch, store, "commands", "WHERE tenant_id = %s AND project_id = %s",
                   "WHERE %s::text IS NOT NULL AND project_id = %s")
    # Beyond AC-109's list, so that no check of example 43 stands untested (M51 mutation run, pre-review
    # F2 and N4).
    elif subject == "interruption listing's tenant check (memory)":
        from agentsdk.interruptions import InMemoryInterruptionStore

        _rewritten(monkeypatch, InMemoryInterruptionStore, "interruptions",
                   "if (i.tenant_id, i.project_id) == (tenant_id, project_id)" + NL + "                 and (top",
                   "if (top")
    elif subject == "interruption store's tenant check (memory)":
        from agentsdk.interruptions import InMemoryInterruptionStore

        _rewritten(monkeypatch, InMemoryInterruptionStore, "interruption",
                   "if item is None or (item.tenant_id, item.project_id) != (tenant_id, project_id):",
                   "if item is None:")
    elif subject == "interruption store's tenant check (postgres)":
        from agentsdk.postgres import PostgresInterruptionStore

        _rewritten(monkeypatch, PostgresInterruptionStore, "interruption",
                   '"i.interruption_id = %s AND i.tenant_id = %s AND i.project_id = %s"',
                   '"i.interruption_id = %s AND %s::text IS NOT NULL AND i.project_id = %s"')
    elif subject == "connections' tenant predicate (postgres)":
        from agentsdk.postgres import PostgresWorkspaceStore

        _rewritten(monkeypatch, PostgresWorkspaceStore, "connections", "WHERE tenant_id = %s AND project_id = %s",
                   "WHERE %s::text IS NOT NULL AND project_id = %s")
    elif subject == "interruptions' tenant predicate (postgres)":
        from agentsdk.postgres import PostgresInterruptionStore

        _rewritten(monkeypatch, PostgresInterruptionStore, "interruptions",
                   '"i.tenant_id = %s AND i.project_id = %s", (tenant_id, project_id), now)',
                   '"%s::text IS NOT NULL AND i.project_id = %s", (tenant_id, project_id), now)')
    elif subject == "snapshot's tenancy check":
        async def any_tenancy(self, snapshot_id, tenant_id, project_id):
            return None

        monkeypatch.setattr(mgr.WorkspaceManager, "usable_snapshot", any_tenancy)
    elif subject == "artifact store's tenant predicate":
        from agentsdk.artifacts import InMemoryArtifactStore

        _rewritten(monkeypatch, InMemoryArtifactStore, "_visible",
                   "if (ref.tenant_id, ref.project_id) != (self._tenant_id, self._project_id) or is_expired(",
                   "if is_expired(")
    elif subject == "Runner.aclose()":
        from agentsdk import Runner

        async def keeps_everything(self):
            return None

        monkeypatch.setattr(Runner, "aclose", keeps_everything)
    else:
        raise AssertionError(subject)


NAMED = {"plain run's budget check": STOPPED, "export's approval": EXPORTS, "workspace's egress check": REFUSED,
         "WorkspaceManager's key by tenant and project": WORKSPACE, "store's tenant predicate (memory)": ROWS,
         "store's tenant predicate (postgres)": ROWS, "snapshot's tenancy check": SNAPSHOT,
         "artifact store's tenant predicate": ARTIFACT, "Runner.aclose()": NOTHING_LEFT,
         "interruption store's tenant check (memory)": CROSSING,
         "interruption listing's tenant check (memory)": CROSSING, "interruption store's tenant check (postgres)": CROSSING,
         "connections' tenant predicate (postgres)": ROWS, "interruptions' tenant predicate (postgres)": ROWS}
# The clause each removal fails, from the example's own wording: the check fails for its subject's
# reason, not by another clause that happens to fail with it.
BECAUSE = {"plain run's budget check": lambda why: "it ended " in why and BUDGET_WORDING not in why,
           "export's approval": lambda why: "an export did not wait: 0 interruption(s)" in why,
           "workspace's egress check": lambda why: "not_listed: " in why,
           "WorkspaceManager's key by tenant and project": lambda why: "released True" in why,
           "store's tenant predicate (memory)": lambda why: "read commands of" in why,
           "store's tenant predicate (postgres)": lambda why: "read commands of" in why,
           "snapshot's tenancy check": lambda why: "the other tenancy started ['" in why,
           "artifact store's tenant predicate": lambda why: "from the other tenancy" in why,
           "Runner.aclose()": lambda why: why.startswith("remained: {"),
           "interruption store's tenant check (memory)": lambda why: "already decided the other tenancy's" in why,
           "interruption listing's tenant check (memory)": lambda why: "of the other tenancy's interruptions by its run" in why,
           "interruption store's tenant check (postgres)": lambda why: "already decided the other tenancy's" in why,
           "connections' tenant predicate (postgres)": lambda why: "read connections of" in why,
           "interruptions' tenant predicate (postgres)": lambda why: "read interruptions of" in why}
BUDGET_WORDING = "budget_exceeded"
STORE_OF = {name: "postgres" for name in ("store's tenant predicate (postgres)",
                                          "interruption store's tenant check (postgres)",
                                          "connections' tenant predicate (postgres)",
                                          "interruptions' tenant predicate (postgres)")}


@pytest.mark.parametrize("subject", list(NAMED))
async def test_on_the_fake_each_check_fails_without_its_subject(monkeypatch, subject):
    without(monkeypatch, subject)
    checks = await example_43(STORE_OF.get(subject, "memory"), seed=3)
    passed, why = checks[NAMED[subject]]
    assert passed is False and BECAUSE[subject](why or ""), (subject, passed, why)


def test_each_removal_names_a_clause_that_exists_in_the_source():
    """The rewritten clauses are the code's own: a removal that matched nothing would test nothing."""
    assert "if (host, int(port)) not in self.allow:" in inspect.getsource(eg.Proxy)
    assert "    def may_call(self)" in inspect.getsource(BudgetLease)
    assert presets.general_agent.__kwdefaults__["export_approval"] is ApprovalPolicy.REQUIRE_APPROVAL
    assert presets.general_subagent.__kwdefaults__["export_approval"] is ApprovalPolicy.REQUIRE_APPROVAL


# =================================================================================================
# On Docker, before review: ten times on each store
# =================================================================================================


@pytest.mark.docker
@pytest.mark.parametrize("repetition", range(10))
@pytest.mark.parametrize("store", STORES)
async def test_example_43_holds_every_check_on_docker(store, repetition):
    checks = await example_43(store, "docker", seed=1000 + repetition)
    assert set(checks) == ON_DOCKER and not failed(checks), failed(checks)
