"""M43 gate: M42's carried items, then compute measured and charged (FR-155 to FR-158, AC-101, NFR-46,
NFR-48, NFR-49).

FR-155's repairs each come with a test that fails without them (DECISION-97b301ea). Compute follows
P5-D17 and P5-D18 as the owner settled them in the pre-flight (DECISION-91ec4213): one migration
per milestone (0023 adds `workspace_commands.cpu_usec`); `run_command` asks its lease before it
starts; the manifest names `cpu_second_usd` only when one is set; `WorkspaceCommandEnded` carries
`cpu_seconds` and `cost_usd`. NFR-23 counts compute (amended in the same pre-flight).

The surface these tests pin:
  * `CommandOutcome.cpu_seconds`, and `agentsdk.workspace.charged(reading, killed, duration, cpus)`:
    a killed command, or a reading absent, malformed, negative or past `duration x cpus`, is
    charged that ceiling; otherwise its reading.
  * `FakeCommand.cpu(seconds)`, the fake's declared reading.
  * `agentsdk.workspace.docker`: `WRAPPER`, run as `sh -c WRAPPER sh <argv...>`; `MARK`, the
    wrapper line's first word; `TAIL_BYTES` (4 KiB); `reading(tail)` -> (usec, exit code, length of
    the line) when the tail ends with a well-formed wrapper line, else None.
  * `CommandRecord.cpu_usec`; `BudgetPolicy.cpu_second_usd`.

Tests marked `docker` run live before review with `pytest -m docker`. The persisted half removes
every row it writes, by its own tenancies.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import uuid
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from agentsdk import AgentSpec, Persistence, RunConfig, RunStatus, Runner
from agentsdk.budget import BudgetGovernor, BudgetPolicy
from agentsdk.config import normalise_database_url
from agentsdk.events import EventType
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.plan import BudgetReservation, PlanNode, PlanVersion
from agentsdk.postgres import RunScope
from agentsdk.primitives import Message, Role, ToolCall
from agentsdk.registry import ModelCapabilities, ModelEntry, ModelPricing, ModelRegistry
from agentsdk.tools import ApprovalPolicy
from agentsdk.workspace import CommandOutcome, DockerProvider, FakeProvider, WorkspaceHandle, WorkspaceSpec, charged
from agentsdk.workspace import docker as dk
from agentsdk.workspace import manager as mgr
from agentsdk.workspace import tools as wt
from agentsdk.workspace.records import CommandRecord, now

REPO = Path(__file__).resolve().parent.parent
DSN = normalise_database_url(os.environ.get("DATABASE_URL"))
PREFIX = "SYN-m43-"
PROJECT = "p-m43"
STORES = ["memory", "postgres"]
FAKE_IMAGE = "fake/workspace@sha256:" + "0" * 64
TENANTS: list[str] = []
MODEL = "priced-model"
# One call of Usage(5, 1, 6): 5 prompt tokens at 0.01 and 1 completion token at 0.10 is 0.15 USD.
PRICING = ModelPricing(input=Decimal("0.01"), output=Decimal("0.10"))
CALL_USD = Decimal("0.15")
CPU_USD = Decimal("0.50")  # a CPU-second


def _example(name: str):
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py").replace("-", "_"), REPO / "scripts" / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EX35 = _example("35_workspace_plan.py")
EX36 = _example("36_sandbox_contention.py")


def _docker_leftovers(tenant: str) -> None:
    """Removes what a test left on the daemon for its own tenancy: never anything else."""
    for kind, listing in (("container", ["ps", "-aq"]), ("volume", ["volume", "ls", "-q"])):
        found = subprocess.run(["docker", *listing, "--filter", f"label=agentsdk.tenant={tenant}"],
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
        with psycopg.connect(DSN, autocommit=True) as conn:
            ids = [r[0] for r in conn.execute("SELECT run_id FROM runs WHERE tenant_id = ANY(%s)", (TENANTS,))]
            conn.execute("DELETE FROM workspace_commands WHERE tenant_id = ANY(%s)", (TENANTS,))
            conn.execute("DELETE FROM workspaces WHERE tenant_id = ANY(%s)", (TENANTS,))
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


# =================================================================================================
# FR-155: M42's carried items (DECISION-97b301ea)
# =================================================================================================


def test_example_36_judges_each_timeout_by_its_own_probes_max_command_seconds():
    """The network probe runs with 30 s, the rest with COMMAND_SECONDS: a command of it killed at
    its timeout is judged against its own, so neither a late network probe passes nor an
    on-time one fails."""
    assert EX36.command_seconds("net") == 30.0
    assert EX36.command_seconds("fork") == EX36.COMMAND_SECONDS
    late = 30.0 + EX36.KILL_SLACK + 1
    on_time = 30.0 + EX36.KILL_SLACK - 1
    assert EX36.overran(late, "net") is True
    assert EX36.overran(on_time, "net") is False
    assert EX36.overran(on_time, "fork") is True


async def test_example_36s_timeout_check_reads_each_probes_own_bound(monkeypatch):
    """Through the check itself: the network probe's command ends at its timeout 20 s past
    COMMAND_SECONDS + KILL_SLACK and inside its own bound, and the check holds."""
    real_python = EX36.python

    async def python(cmd):
        if cmd.argv == EX36.ARGV["net"]:
            cmd.out("try a 1\ntry b 2\ntry c 3\ndone\n")
            await cmd.sleep(600)
        return await real_python(cmd)

    monkeypatch.setattr(EX36, "python", python)
    # The network probe's own bound is 3 s, every other probe's 0.5 s, the slack 1 s: its command,
    # killed at 3 s, is past COMMAND_SECONDS + KILL_SLACK and inside its own bound plus the slack.
    monkeypatch.setattr(EX36, "LIMITS", {**EX36.LIMITS, "net": {"max_command_seconds": 3.0}})
    monkeypatch.setattr(EX36, "COMMAND_SECONDS", 0.5)
    monkeypatch.setattr(EX36, "KILL_SLACK", 1.0)
    checks, _ = await EX36.offline("memory", "fake", tenant=tenancy(), quiet=True, seed=3)
    [(passed, why)] = [(p, w) for label, p, w in checks if label == "no command outlived its timeout"]
    assert passed is True, why


def test_example_36s_command_seconds_comment_is_no_longer_every_probes():
    source = (REPO / "scripts" / "36_sandbox_contention.py").read_text(encoding="utf-8")
    [line] = [line for line in source.splitlines() if line.startswith("COMMAND_SECONDS =")]
    assert "every probe's timeout" not in line or "but" in line, line


def test_example_35_reports_a_check_that_never_happened_as_failed_in_every_mode(capsys):
    """No SKIP: a check whose subject never happened fails, live included, as example 36's do."""
    import inspect

    assert list(inspect.signature(EX35.report).parameters) == ["checks"]
    failed = EX35.report([("a subject that never happened", None), ("a held one", True)])
    out = capsys.readouterr().out
    assert failed == ["a subject that never happened"]
    assert "[FAIL] not exercised: a subject that never happened" in out
    assert "SKIP" not in inspect.getsource(EX35.report)


async def test_example_36s_memory_check_fails_with_no_command_ended_event_to_judge(monkeypatch):
    """As the disk check requires a refusal: an allocation whose WorkspaceCommandEnded never came
    has nothing to judge the bound's name by."""
    real_finished = mgr.RunWorkspace._finished

    async def finished(self, handle, command_id, argv, tool_call_id, started_at, started, outcome, *,
                       kill_error=None):
        if outcome.killed == "limit":
            emit = self._emit

            async def silent(kind, payload):
                if kind is EventType.WORKSPACE_COMMAND_ENDED:
                    return None
                return await emit(kind, payload)

            self._emit = silent
        return await real_finished(self, handle, command_id, argv, tool_call_id, started_at, started, outcome,
                                   kill_error=kill_error)

    monkeypatch.setattr(mgr.RunWorkspace, "_finished", finished)
    checks, _ = await EX36.offline("memory", "fake", tenant=tenancy(), quiet=True, seed=3)
    [(passed, why)] = [(p, w) for label, p, w in checks
                       if label == "an allocation without end was killed at memory_bytes, recorded by name"]
    assert passed is False, why


def test_the_tools_deadline_counts_both_size_reads_a_stale_workspace_takes():
    """FR-148 as amended by FR-155: the stale reading before a command and the reading after it."""
    spec = WorkspaceSpec(image=FAKE_IMAGE, max_command_seconds=7)
    assert {name: s.timeout_seconds for name, s in wt.specs(spec).items()} == {
        name: mgr.SETUP_SECONDS + 2 * mgr.MEASURE_SECONDS + 7 + 30.0 for name in wt.NAMES}


# =================================================================================================
# FR-156: compute is measured
# =================================================================================================


@pytest.mark.parametrize("reading, killed, duration, cpus, expected", [
    (1.5, None, 2.0, 1.0, 1.5),       # its own reading
    (0.0, None, 2.0, 1.0, 0.0),       # a reading of nothing is a reading
    (None, None, 2.0, 1.5, 3.0),      # absent: the ceiling
    (2.5, None, 2.0, 1.0, 2.0),       # past the ceiling: the ceiling
    (-1.0, None, 2.0, 1.0, 2.0),      # negative: the ceiling
    (float("nan"), None, 2.0, 1.0, 2.0),
    (float("inf"), None, 2.0, 1.0, 2.0),
    (0.1, "timeout", 2.0, 2.0, 4.0),  # killed: the ceiling, whatever it read
    (0.1, "cancelled", 1.0, 1.0, 1.0),
    (0.1, "limit", 1.0, 0.5, 0.5),
    (0.1, "error", 1.0, 1.0, 1.0),
])
def test_a_command_is_charged_its_reading_or_else_its_ceiling(reading, killed, duration, cpus, expected):
    assert charged(reading, killed, duration, cpus) == pytest.approx(expected)


def test_every_command_runs_under_the_wrapper_with_nothing_else_changed():
    handle = WorkspaceHandle("w", WorkspaceSpec(image=FAKE_IMAGE), {"agentsdk.tenant": "t"})
    args = dk.container_args(handle, "c", ["python", "-c", "print(1)"], [])
    at = args.index("--entrypoint=")
    assert args[at:] == ["--entrypoint=", FAKE_IMAGE, "sh", "-c", dk.WRAPPER, "sh", "python", "-c", "print(1)"]
    hardening = dk.hardening(handle.spec)
    start = args.index(hardening[0])
    assert args[start:start + len(hardening)] == hardening and args.count("--network") == 1
    # The command runs as given, its exit code kept; every other process is killed before the
    # reading, the wrapper's last line (P5-D17).
    assert 'env -- "$@"' in dk.WRAPPER and "kill -9 -1" in dk.WRAPPER and "cpu.stat" in dk.WRAPPER
    assert dk.TAIL_BYTES == 4096


NONCE = b"0123456789abcdef0123456789abcdef"
OTHER = b"fedcba9876543210fedcba9876543210"


def _line(usec, code, nonce=NONCE) -> bytes:
    return b"\n" + dk.MARK + b" " + nonce + f" {usec} {code}\n".encode()


@pytest.mark.parametrize("tail, expected", [
    (b"hello\n" + _line(1500000, 0), (1500000, 0)),
    (_line(7, 3), (7, 3)),
    # A forged copy before the wrapper's own: the last line only.
    (b"x" + _line(1, 0, OTHER) + b"more\n" + _line(2500000, 0), (2500000, 0)),
    # A forged copy after which the wrapper never printed: no reading (M43 round 1).
    (b"hello\n" + _line(1, 0) + b"after\n", None),
    # A line without the command's own nonce is never a reading, last or not (DECISION-88395b13).
    (b"hello\n" + _line(1, 130, OTHER), None),
    (b"hello\n" + b"\n" + dk.MARK + b" 1 130\n", None),
    (b"hello\n", None),
    (b"", None),
    (b"\n" + dk.MARK + b" " + NONCE + b" \n", None),
    (b"\n" + dk.MARK + b" " + NONCE + b" abc 0\n", None),
    (b"\n" + dk.MARK + b" " + NONCE + b" 1 0", None),
    (b"\n" + dk.MARK + b" " + NONCE + b" 1 0\nextra", None),
])
def test_the_reading_is_the_tails_last_line_carrying_the_nonce_or_none(tail, expected):
    found = dk.reading(tail, NONCE)
    if expected is None:
        assert found is None
    else:
        usec, code, length = found
        assert (usec, code) == expected and tail.endswith(_line(usec, code)) and length == len(_line(usec, code))


def test_the_wrapper_line_is_taken_off_only_when_it_carries_the_nonce():
    kept = dk._Kept(1024)
    stream = b"out\n" + _line(5, 3)
    kept.data += stream
    kept.count, kept.tail = len(stream), stream
    assert kept.take_reading(NONCE) == (5, 3)
    assert (bytes(kept.data), kept.count) == (b"out\n", 4)
    forged = b"out\n" + _line(5, 3, OTHER)
    kept.data, kept.count, kept.tail = bytearray(forged), len(forged), forged
    assert kept.take_reading(NONCE) is None
    assert bytes(kept.data) == forged, "a forged line is the command's own output, and stays"


def test_each_command_gets_a_nonce_of_its_own_of_128_bits():
    nonces = {dk._nonce() for _ in range(1000)}
    assert len(nonces) == 1000 and all(len(n) == 32 and int(n, 16) >= 0 for n in nonces)


def test_the_wrapper_traps_the_signals_a_command_could_send_it_before_running_the_argv():
    trap = dk.WRAPPER.index("trap ")
    assert trap < dk.WRAPPER.index('env -- "$@"') and trap < dk.WRAPPER.index("read -r n")
    trapped = dk.WRAPPER[trap:].split(";", 1)[0].split()[2:]
    assert set(trapped) == {"INT", "QUIT", "TERM", "HUP", "USR1", "USR2"}
    assert dk.WRAPPER.index("read -r n") < dk.WRAPPER.index('env -- "$@"')


async def test_a_fake_command_declares_its_cpu_and_a_killed_one_is_charged_its_ceiling():
    async def burn(cmd):
        cmd.cpu(1.25)
        return 0

    async def hog(cmd):
        cmd.cpu(0.01)  # a reading a killed command never gets to give
        await cmd.sleep(30)
        return 0

    async def quiet(cmd):
        return 0

    fake = FakeProvider(commands={"burn": burn, "hog": hog, "quiet": quiet})
    handle = await fake.create(WorkspaceSpec(image=FAKE_IMAGE, cpus=2.0), {"agentsdk.tenant": "t"})
    burnt = await fake.run(handle, ["burn"], timeout=5)
    killed = await fake.run(handle, ["hog"], timeout=0.3)
    idle = await fake.run(handle, ["quiet"], timeout=5)
    assert burnt.cpu_seconds == 1.25 and idle.cpu_seconds == 0.0
    assert killed.killed == "timeout" and killed.cpu_seconds == pytest.approx(killed.duration * 2.0)


def test_an_outcome_made_without_a_reading_charges_nothing_until_told():
    outcome = CommandOutcome(exit_code=0, stdout=b"", stderr=b"", stdout_cut=False, stderr_cut=False,
                             stdout_bytes=0, stderr_bytes=0, duration=1.0)
    assert outcome.cpu_seconds == 0.0


# =================================================================================================
# FR-157: compute is charged, and recorded (migration 0023)
# =================================================================================================


def respond(content="", calls=()):
    return ModelResponse(message=Message(role=Role.ASSISTANT, content=content, tool_calls=tuple(calls)),
                         stop_reason=StopReason.TOOL_CALLS if calls else StopReason.END_TURN, usage=Usage(5, 1, 6))


class Turns:
    """Each turn makes the run_command calls its task lists (a JSON list of turns, each a list of
    argvs), then answers."""

    def __init__(self) -> None:
        self.calls = 0

    async def send(self, request):
        self.calls += 1
        turns = json.loads(request.messages[0].content)
        turn = sum(1 for m in request.messages if m.role is Role.ASSISTANT)
        if turn < len(turns):
            return respond(calls=[ToolCall(id=f"t{turn}-{i}", name="run_command", arguments={"argv": argv})
                                  for i, argv in enumerate(turns[turn])])
        return respond("done")


def _burner(seconds: float):
    async def burn(cmd):
        cmd.cpu(seconds)
        cmd.out(f"burnt {seconds}\n")
        return 0
    return burn


async def _nap(cmd):
    await cmd.sleep(30)
    return 0


def _lease(usd: str, price: Decimal | None):
    plan = PlanVersion(plan_id=str(uuid.uuid4()), version=1, run_id=str(uuid.uuid4()), nodes=(
        PlanNode(node_id="a", objective="burn", assigned_role="worker",
                 budget_reservation=BudgetReservation(usd=Decimal(usd))),))
    return BudgetGovernor(BudgetPolicy(run_ceiling_usd=Decimal("10.00"), cpu_second_usd=price), plan).lease("a")


async def _run(store: str, turns: list, *, lease=None, cpus: float = 1.0, max_command_seconds: float = 5.0):
    tenant = tenancy()
    persistence = Persistence.postgres(DSN, create_schema=False) if store == "postgres" else None
    model = Turns()
    registry = ModelRegistry([ModelEntry(provider="test", model_id=MODEL, model_version="1", adapter_version="test/1",
                                         capabilities=ModelCapabilities(max_context_tokens=100_000, pricing=PRICING))])
    runner = Runner({"m": model}, model_registry=registry, persistence=persistence,
                    workspace_provider=FakeProvider(commands={"burn1": _burner(1.0), "burn3": _burner(3.0),
                                                              "nap": _nap}))
    agent = AgentSpec(id="burner", instructions="x", preferred_model=f"m:{MODEL}", tool_profile=("run_command",),
                      workspace=WorkspaceSpec(image=FAKE_IMAGE, run_command_approval=ApprovalPolicy.AUTO, cpus=cpus,
                                              max_command_seconds=max_command_seconds))
    result = await runner.run(agent, json.dumps(turns), RunConfig(tenant_id=tenant, project_id=PROJECT, max_turns=6,
                                                                 budget_lease=lease))
    rows = await asyncio.to_thread(runner._workspace_store().commands, tenant, PROJECT)
    sessions = runner._sessions_for(RunScope(run_id=result.run_id, tenant_id=tenant, project_id=PROJECT))
    results = [r for m in await asyncio.to_thread(sessions.history, result.run_id) for r in m.tool_results]
    await runner.aclose()
    return result, rows, results, model


def _ended(result) -> list[dict]:
    return [e.payload for e in result.events if e.event_type is EventType.WORKSPACE_COMMAND_ENDED]


def _model_costs(result) -> list:
    return [e.payload.get("cost_usd") for e in result.events if e.event_type is EventType.MODEL_CALLED]


@pytest.mark.parametrize("store", STORES)
async def test_a_commands_cpu_is_on_its_outcome_its_event_and_its_row(store):
    result, rows, results, _ = await _run(store, [[["burn3"]]])
    assert result.status is RunStatus.COMPLETED, result.error
    [ended] = _ended(result)
    assert ended["cpu_seconds"] == 3.0 and ended["cost_usd"] is None
    [row] = rows
    assert row.cpu_usec == 3_000_000
    # The model reads what the command printed, not how it was measured.
    assert "burnt 3.0" in results[0].content and "cpu" not in results[0].content.lower()


@pytest.mark.parametrize("store", STORES)
async def test_with_no_price_compute_is_counted_and_nothing_is_charged(store):
    lease = _lease("1.00", None)
    result, rows, _, _ = await _run(store, [[["burn3"]], [["burn3"]]], lease=lease)
    assert result.status is RunStatus.COMPLETED, result.error
    assert [p["cpu_seconds"] for p in _ended(result)] == [3.0, 3.0]
    assert [p["cost_usd"] for p in _ended(result)] == [None, None]
    assert sorted(r.cpu_usec for r in rows) == [3_000_000, 3_000_000]
    assert result.cost_usd == CALL_USD * 3 == lease.spent.usd


@pytest.mark.parametrize("store", STORES)
async def test_compute_past_the_lease_stops_the_run_budget_exceeded_after_its_record(store):
    lease = _lease("1.00", CPU_USD)
    result, rows, _, model = await _run(store, [[["burn3"]], [["burn1"]]], lease=lease)
    assert result.status is RunStatus.FAILED and result.error == "budget_exceeded"
    assert model.calls == 1, "no model call after the command that crossed the ceiling"
    [ended] = _ended(result)
    assert ended["cpu_seconds"] == 3.0 and Decimal(ended["cost_usd"]) == Decimal("1.50")
    [row] = rows
    assert row.cpu_usec == 3_000_000
    assert result.cost_usd == CALL_USD + Decimal("1.50") == lease.spent.usd
    # NFR-23 as amended: the run's spend is its model calls' costs and its commands' costs.
    assert result.cost_usd == sum(Decimal(c) for c in _model_costs(result)) + sum(
        Decimal(p["cost_usd"]) for p in _ended(result))


@pytest.mark.parametrize("store", STORES)
async def test_a_command_whose_lease_is_spent_does_not_start(store):
    """DECISION-91ec4213: asked before it starts, as a model call is. The second command of the
    turn finds the lease spent by the first."""
    lease = _lease("1.00", CPU_USD)
    result, rows, results, _ = await _run(store, [[["burn3"], ["burn1"]]], lease=lease)
    assert result.status is RunStatus.FAILED and result.error == "budget_exceeded"
    assert [r.argv for r in rows] == [["burn3"]]
    started = [e.payload["argv"] for e in result.events if e.event_type is EventType.WORKSPACE_COMMAND_STARTED]
    assert started == [["burn3"]]
    assert results[1].is_error and "budget" in results[1].content.lower()


async def test_a_command_is_refused_when_the_model_call_before_it_spent_the_lease():
    lease = _lease("0.10", CPU_USD)  # one model call, 0.15, spends it
    result, rows, results, model = await _run("memory", [[["burn1"]]], lease=lease)
    assert result.status is RunStatus.FAILED and result.error == "budget_exceeded"
    assert rows == [] and model.calls == 1
    assert results[0].is_error
    # Asked first: no workspace is created for a command that does not start.
    assert not [e for e in result.events if e.event_type is EventType.WORKSPACE_CREATED]


async def test_a_command_its_provider_failed_is_charged_its_ceiling(monkeypatch):
    async def fails(self, handle, argv, *, timeout, stdin=None, command_id=None):
        await asyncio.sleep(0.3)
        raise RuntimeError("the provider failed")

    monkeypatch.setattr(FakeProvider, "run", fails)
    result, _, _, _ = await _run("memory", [[["burn1"]]], lease=_lease("5.00", CPU_USD), cpus=2.0)
    [ended] = _ended(result)
    # Its ceiling, by its own wall time: Windows' clock may read a 0.3 s sleep as 0.297 s (F3).
    assert ended["killed"] == "error" and ended["cpu_seconds"] >= 0.55
    assert ended["cpu_seconds"] == pytest.approx(ended["duration_ms"] / 1000 * 2.0, rel=0.1)
    assert Decimal(ended["cost_usd"]) == Decimal(str(ended["cpu_seconds"])) * CPU_USD


async def test_a_killed_command_is_charged_its_ceiling():
    lease = _lease("5.00", CPU_USD)
    result, rows, _, _ = await _run("memory", [[["nap"]]], lease=lease, cpus=2.0, max_command_seconds=0.3)
    [ended] = _ended(result)
    assert ended["killed"] == "timeout"
    assert ended["cpu_seconds"] >= 0.55
    assert ended["cpu_seconds"] == pytest.approx(ended["duration_ms"] / 1000 * 2.0, rel=0.1)
    assert Decimal(ended["cost_usd"]) == (Decimal(rows[0].cpu_usec) / 1_000_000 * CPU_USD)


async def test_a_command_cancelled_with_its_run_is_charged_its_ceiling():
    lease = _lease("5.00", CPU_USD)
    tenant = tenancy()
    runner = Runner({"m": Turns()}, workspace_provider=FakeProvider(commands={"nap": _nap}),
                    model_registry=ModelRegistry([ModelEntry(
                        provider="test", model_id=MODEL, model_version="1", adapter_version="test/1",
                        capabilities=ModelCapabilities(max_context_tokens=100_000, pricing=PRICING))]))
    agent = AgentSpec(id="burner", instructions="x", preferred_model=f"m:{MODEL}", tool_profile=("run_command",),
                      workspace=WorkspaceSpec(image=FAKE_IMAGE, run_command_approval=ApprovalPolicy.AUTO, cpus=1.5))
    handle = await runner.start(agent, json.dumps([[["nap"]]]), RunConfig(tenant_id=tenant, project_id=PROJECT,
                                                                          max_turns=3, budget_lease=lease))
    async for event in handle.events():
        if event.event_type is EventType.WORKSPACE_COMMAND_STARTED:
            await asyncio.sleep(0.4)
            handle.cancel()
    result = await handle.result()
    await runner.aclose()
    [ended] = _ended(result)
    # Its ceiling: its wall time, from its start to its kill, times cpus 1.5.
    assert ended["killed"] == "cancelled" and ended["cpu_seconds"] >= 0.3 * 1.5
    assert ended["cpu_seconds"] == pytest.approx(ended["duration_ms"] / 1000 * 1.5, rel=0.1)
    assert lease.spent.usd == CALL_USD + Decimal(ended["cost_usd"])


def test_the_policy_refuses_a_cpu_price_that_is_not_a_non_negative_decimal():
    for bad in (Decimal("-0.01"), 0.5, "0.5", Decimal("NaN"), Decimal("Infinity")):
        with pytest.raises(ValueError, match="cpu_second_usd"):
            BudgetPolicy(run_ceiling_usd=Decimal("1"), cpu_second_usd=bad)
    assert BudgetPolicy(run_ceiling_usd=Decimal("1")).cpu_second_usd is None
    assert BudgetPolicy(run_ceiling_usd=Decimal("1"), cpu_second_usd=Decimal("0")).cpu_second_usd == 0


async def test_the_manifest_names_the_price_only_when_one_is_set():
    """NFR-49 and DECISION-91ec4213: a plan without a price records the manifest it did before."""
    assert DSN, "DATABASE_URL must be set"
    priced, _, _, _ = await _run("postgres", [[["burn1"]]], lease=_lease("5.00", CPU_USD))
    unpriced, _, _, _ = await _run("postgres", [[["burn1"]]], lease=_lease("5.00", None))
    with psycopg.connect(DSN) as conn:
        policies = {run_id: conn.execute("SELECT budget_policy FROM execution_manifests WHERE run_id = %s",
                                         (run_id,)).fetchone()[0] for run_id in (priced.run_id, unpriced.run_id)}
    assert Decimal(policies[priced.run_id]["cpu_second_usd"]) == CPU_USD
    assert "cpu_second_usd" not in policies[unpriced.run_id]
    assert set(policies[unpriced.run_id]) == {"run_ceiling_usd", "run_ceiling_tokens", "orchestrator_reserve_fraction",
                                              "unallocated_reserve_fraction", "reservation_cap_fraction",
                                              "max_replans"}


async def test_a_runs_command_event_adds_only_the_compute_fields():
    """NFR-49: the WorkspaceCommandEnded of a run with no new feature is as before, plus
    cpu_seconds and cost_usd."""
    result, _, _, _ = await _run("memory", [[["burn1"]]])
    [ended] = _ended(result)
    assert set(ended) == {"command_id", "workspace_id", "tool_call_id", "exit_code", "killed", "limit", "kill_error",
                          "duration_ms", "stdout_bytes", "stderr_bytes", "stdout_cut", "stderr_cut", "cpu_seconds",
                          "cost_usd"}


async def test_the_store_refuses_a_negative_cpu_reading():
    assert DSN, "DATABASE_URL must be set"
    _, rows, _, _ = await _run("postgres", [[["burn1"]]])
    [row] = rows
    store = Persistence.postgres(DSN, create_schema=False).workspace_store()
    bad = CommandRecord(command_id=str(uuid.uuid4()), workspace_id=row.workspace_id, tenant_id=row.tenant_id,
                        project_id=PROJECT, run_id=row.run_id, tool_call_id=None, argv=["x"], exit_code=0,
                        killed=None, started_at=now(), ended_at=now(), stdout_bytes=0, stderr_bytes=0,
                        stdout_kept=0, stderr_kept=0, cpu_usec=-1)
    with pytest.raises(psycopg.errors.CheckViolation):
        await asyncio.to_thread(store.record_command, bad)


def test_migration_0023_adds_only_the_cpu_column():
    sql = (REPO / "agentsdk" / "migrations" / "0023_command_cpu.sql").read_text(encoding="utf-8")
    assert "cpu_usec" in sql and "workspace_connections" not in sql and "workspace_snapshots" not in sql


# =================================================================================================
# FR-158: example 37
# =================================================================================================


EX37_NAME = "37_compute.py"
CEILING = "a plan whose children's compute passed its ceiling stopped budget_exceeded, the command that crossed it recorded"


async def example_37(store: str, provider_name: str = "fake", **options) -> dict:
    module = _example(EX37_NAME)
    checks = await module.offline(store, provider_name, tenant=tenancy(), quiet=True, **options)
    return {label: (passed, why) for label, passed, why in checks}


@pytest.mark.parametrize("store", STORES)
async def test_example_37_holds_every_check_offline(store):
    checks = await example_37(store)
    assert len(checks) >= 4 and CEILING in checks
    assert {label: why for label, (passed, why) in checks.items() if passed is not True} == {}


async def test_example_37s_ceiling_check_fails_without_the_compute_charge(monkeypatch):
    real = mgr.RunWorkspace._finished

    async def uncharged(self, *args, **kwargs):
        self._account = None
        return await real(self, *args, **kwargs)

    monkeypatch.setattr(mgr.RunWorkspace, "_finished", uncharged)
    checks = await example_37("memory")
    assert checks[CEILING][0] is False, checks[CEILING]


async def test_example_37s_ceiling_check_fails_without_the_check_before_a_command(monkeypatch):
    """F5: the ceiling child asks for two burns in one turn; only the check before a command
    stops the second."""
    import agentsdk.api as api

    monkeypatch.setattr(api._Compute, "may_run", lambda self: True)
    checks = await example_37("memory")
    assert checks[CEILING][0] is False and "started after it" in checks[CEILING][1], checks[CEILING]


async def test_example_37_passes_nothing_when_no_command_runs(monkeypatch):
    module = _example(EX37_NAME)

    async def refuses(self, handle, argv, *, timeout, stdin=None, command_id=None):
        raise mgr.WorkspaceUnavailable("no command runs")

    monkeypatch.setattr(FakeProvider, "run", refuses)
    checks = await module.offline("memory", "fake", tenant=tenancy(), quiet=True)
    assert [label for label, passed, _ in checks if passed is True] == []


def test_example_37_runs_offline_as_a_program():
    done = subprocess.run([sys.executable, str(REPO / "scripts" / EX37_NAME), "--offline"],
                          capture_output=True, text=True, timeout=600, cwd=REPO)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-2000:]
    assert "[FAIL]" not in done.stdout and done.stdout.count("[PASS]") >= 4


def test_example_37_is_listed():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert EX37_NAME in readme
    dist = (REPO / "tests" / "test_distribution.py").read_text(encoding="utf-8")
    assert f'"{EX37_NAME}"' in dist


def test_the_runtime_dependencies_are_unchanged():
    import tomllib

    declared = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    assert declared == ["httpx==0.28.1", "jsonschema==4.26.0", "psycopg[binary]==3.3.5", "psycopg_pool==3.2.6",
                        "python-dotenv==1.2.3"]


# =================================================================================================
# On a real daemon (pytest -m docker)
# =================================================================================================


BUSY = "import time\nt = time.process_time()\nwhile time.process_time() - t < {s}: pass\nprint('busy done')\n"
# The main process prints a forged line and exits at once; its detached child burns 1.5 s, forges
# again, then sleeps: the wrapper kills it and reads what it really used.
FORGER = ("import os, sys, time\n"
          "MARK = sys.argv[1]\n"
          "print(MARK + ' 1 0', flush=True)\n"
          "if os.fork() == 0:\n"
          "    os.setsid()\n"
          "    t = time.process_time()\n"
          "    while time.process_time() - t < 1.5: pass\n"
          "    sys.stdout.write('\\n' + MARK + ' 1 0\\n'); sys.stdout.flush()\n"
          "    time.sleep(60)\n"
          "    os._exit(0)\n"
          "time.sleep(2.5)\n")


async def _docker_handle(**limits):
    docker = DockerProvider()
    handle = await docker.create(WorkspaceSpec(image=EX36.IMAGE, **limits), {
        "agentsdk.workspace": str(uuid.uuid4()), "agentsdk.tenant": tenancy()})
    return docker, handle


@pytest.mark.docker
async def test_on_docker_a_busy_loop_reads_within_ten_percent():
    docker, handle = await _docker_handle()
    try:
        outcome = await docker.run(handle, ["python", "-c", BUSY.format(s=2.0)], timeout=60)
    finally:
        await docker.remove(handle)
    assert outcome.exit_code == 0 and outcome.killed is None
    assert 1.8 <= outcome.cpu_seconds <= 2.2 + 0.3, outcome.cpu_seconds  # the interpreter's start included
    assert dk.MARK not in outcome.stdout and outcome.stdout == b"busy done\n"
    assert outcome.stdout_bytes == len(b"busy done\n")


@pytest.mark.docker
async def test_on_docker_a_forged_reading_is_not_what_a_command_is_charged():
    docker, handle = await _docker_handle()
    try:
        outcome = await docker.run(handle, ["python", "-c", FORGER, dk.MARK.decode()], timeout=60)
    finally:
        await docker.remove(handle)
    assert outcome.exit_code == 0 and outcome.killed is None
    assert outcome.cpu_seconds >= 1.4, outcome.cpu_seconds
    # The forged copies stay in what the model reads: they are the command's own output.
    assert outcome.stdout.count(dk.MARK) == 2


# A detached child that never stops forging, under a main process that burns a second and exits:
# unless it is killed before the reading, its forged line follows the wrapper's.
SPAMMER = ("import os, sys, time\n"
           "line = ('\\n' + sys.argv[1] + ' 1 0\\n').encode()\n"
           "if os.fork() == 0:\n"
           "    os.setsid()\n"
           "    while True:\n"
           "        os.write(1, line)\n"
           "t = time.process_time()\n"
           "while time.process_time() - t < 1.0: pass\n")


@pytest.mark.docker
async def test_on_docker_a_process_left_forging_is_killed_before_the_reading():
    docker, handle = await _docker_handle()
    try:
        outcomes = [await docker.run(handle, ["python", "-c", SPAMMER, dk.MARK.decode()], timeout=60)
                    for _ in range(3)]
    finally:
        await docker.remove(handle)
    for outcome in outcomes:
        assert outcome.exit_code == 0 and outcome.killed is None
        assert outcome.cpu_seconds >= 0.9, [o.cpu_seconds for o in outcomes]


@pytest.mark.docker
async def test_on_docker_the_exit_code_is_the_commands_own_and_a_timeout_is_charged_its_ceiling():
    docker, handle = await _docker_handle(cpus=1.5)
    try:
        exited = await docker.run(handle, ["python", "-c", "import sys; print('x'); sys.exit(3)"], timeout=60)
        missing = await docker.run(handle, ["no-such-program"], timeout=60)
        slept = await docker.run(handle, ["python", "-c", "import time; time.sleep(60)"], timeout=2)
    finally:
        await docker.remove(handle)
    assert (exited.exit_code, exited.stdout) == (3, b"x\n")
    assert missing.exit_code == 127 and dk.MARK not in missing.stdout
    assert slept.killed == "timeout" and slept.cpu_seconds == pytest.approx(slept.duration * 1.5)


@pytest.mark.docker
async def test_on_docker_output_past_max_output_bytes_still_gives_its_reading():
    docker, handle = await _docker_handle()
    program = "import sys\nsys.stdout.write('y' * 3000000)\n"
    try:
        outcome = await docker.run(handle, ["python", "-c", program], timeout=60)
    finally:
        await docker.remove(handle)
    assert outcome.stdout_cut and outcome.stdout_bytes == 3_000_000
    assert len(outcome.stdout) == handle.spec.max_output_bytes and dk.MARK not in outcome.stdout
    assert 0 < outcome.cpu_seconds < outcome.duration


@pytest.mark.docker
@pytest.mark.parametrize("store", STORES)
async def test_on_docker_example_37_holds_every_check(store):
    checks = await example_37(store, "docker")
    assert {label: why for label, (passed, why) in checks.items() if passed is not True} == {}


@pytest.mark.docker
def test_example_37_runs_scripted_on_docker():
    done = subprocess.run([sys.executable, str(REPO / "scripts" / EX37_NAME), "--scripted"],
                          capture_output=True, text=True, timeout=900, cwd=REPO)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-2000:]
    assert "[FAIL]" not in done.stdout


# --- FR-155: AC-100's kill on cancel, one layer at a time ---------------------------------------------


CANCEL = "a cancelled command that ignores SIGTERM and left its group was gone when the plan reported cancelled"


def without_layer(monkeypatch, layer: str) -> None:
    """Takes away one layer that ends a cancelled command's container, and nothing else."""
    if layer == "provider kill":
        async def no_kill(self, handle, command_id):
            return None

        monkeypatch.setattr(DockerProvider, "kill", no_kill)
    elif layer == "command removal":
        # A command's own removals by name; the provider's kill removes by name through the CLI
        # directly, and the sweep by container id, so neither is touched. Only command containers:
        # the helpers that set up a volume or read its size remove themselves as before, since
        # their removal is no layer of a cancelled command's.
        named: set[str] = set()
        real_contained, real_rm = DockerProvider._contained, DockerProvider._rm

        async def contained(self, handle, command, argv, **kw):
            if command.name.startswith("agentsdk-cmd-"):
                named.add(command.name)
            return await real_contained(self, handle, command, argv, **kw)

        async def rm(self, *names):
            return await real_rm(self, *[name for name in names if name not in named])

        monkeypatch.setattr(DockerProvider, "_contained", contained)
        monkeypatch.setattr(DockerProvider, "_rm", rm)
    elif layer == "workspace sweep":
        async def volume_only(self, workspace_id):
            for attempt in range(dk._REMOVE_ATTEMPTS):
                code, _, _ = await self._cli("volume", "rm", "-f", dk.volume_name(workspace_id))
                if code == 0:
                    return
                await asyncio.sleep(0.5 * (attempt + 1))
            raise mgr.WorkspaceUnavailable(f"docker could not remove the workspace's volume (exit {code})")

        monkeypatch.setattr(DockerProvider, "_remove", volume_only)
    else:
        raise AssertionError(layer)


@pytest.mark.docker
@pytest.mark.parametrize("layer", ["provider kill", "command removal", "workspace sweep"])
async def test_on_docker_each_cancel_layer_alone_is_enough(monkeypatch, layer):
    """F4: with the other two layers taken away, each one alone still ends the cancelled command
    in time, so no layer is a no-op the others hide (EX36.CANCEL_ALONE, DECISION-d9a568e1)."""
    for other in EX36.CANCEL_LAYERS:
        if other != layer:
            without_layer(monkeypatch, other)
    checks, _ = await EX36.offline("memory", "docker", tenant=tenancy(), quiet=True, seed=5)
    passed, why = {label: (p, w) for label, p, w in checks}[CANCEL]
    assert EX36.CANCEL_ALONE[layer] is True and passed is True, why


# Builtins that would run inside PID 1 if the argv ran in its shell (F1): each is not found.
BUILTINS = [["eval", "printf '\n%s 1 0\n' @@agentsdk-cpu; exit 0"],
            ["exec", "python", "-c", "print('x')"], ["exit", "0"], ["cd", "/tmp"], ["-i", "true"]]


@pytest.mark.docker
async def test_on_docker_no_shell_exports_reach_the_command():
    """P5-D6: the wrapper's shell exports SHLVL and PWD; a command sees neither, as before M43."""
    docker, handle = await _docker_handle()
    try:
        outcome = await docker.run(handle, ["env"], timeout=60)
    finally:
        await docker.remove(handle)
    names = {line.split("=", 1)[0] for line in outcome.stdout.decode().splitlines() if "=" in line}
    assert outcome.exit_code == 0 and names and not names & {"SHLVL", "PWD", "OLDPWD"}, names


@pytest.mark.docker
async def test_on_docker_no_shell_builtin_runs_as_the_command():
    docker, handle = await _docker_handle()
    try:
        outcomes = [await docker.run(handle, argv, timeout=60) for argv in BUILTINS]
    finally:
        await docker.remove(handle)
    for argv, outcome in zip(BUILTINS, outcomes):
        assert (outcome.exit_code, outcome.stdout) == (127, b""), (argv, outcome)
        assert dk.MARK not in outcome.stderr


@pytest.mark.docker
async def test_on_docker_a_line_printed_just_before_a_timeout_is_never_read():
    """F2: commands that end around their timeout, so the wrapper's line is sometimes printed
    before the kill lands."""
    docker, handle = await _docker_handle()
    try:
        outcomes = [await docker.run(handle, ["sh", "-c", f"echo hi; sleep {s / 100}"], timeout=1.8)
                    for s in range(130, 170, 2)]
    finally:
        await docker.remove(handle)
    assert any(o.killed == "timeout" for o in outcomes)
    assert [o.stdout for o in outcomes if dk.MARK in o.stdout] == []


@pytest.mark.docker
@pytest.mark.parametrize("layer", ["provider kill", "command removal", "workspace sweep"])
async def test_on_docker_the_cancel_check_without_one_layer_fails_or_is_covered(monkeypatch, layer):
    """FR-155: each layer alone. A removal the check survives is declared covered by the layers
    EX36.CANCEL_LAYERS names, and the check fails once those are taken away too."""
    without_layer(monkeypatch, layer)
    checks, _ = await EX36.offline("memory", "docker", tenant=tenancy(), quiet=True, seed=5)
    passed = {label: p for label, p, _ in checks}[CANCEL]
    covered_by = EX36.CANCEL_LAYERS[layer]
    if not covered_by:
        assert passed is False
        return
    assert passed is True
    for other in covered_by:
        without_layer(monkeypatch, other)
    checks, _ = await EX36.offline("memory", "docker", tenant=tenancy(), quiet=True, seed=5)
    assert {label: p for label, p, _ in checks}[CANCEL] is False


# --- M43 round 2 (DECISION-88395b13) -------------------------------------------------------------------

BURN_2 = "import time\nt = time.process_time()\nwhile time.process_time() - t < 2: pass\n"


def _attack(signal_name: str) -> str:
    """A 2 s burn, a forged line naming 130 printed last, then a signal to PID 1 (round 1's attack)."""
    return (BURN_2 + "import os, signal\n"
            f"os.write(1, ('\\n' + {dk.MARK.decode()!r} + ' 1 130\\n').encode())\n"
            f"os.kill(1, signal.SIG{signal_name})\n")


@pytest.mark.docker
@pytest.mark.parametrize("signal_name", ["INT", "QUIT", "TERM", "HUP", "USR1", "USR2"])
async def test_on_docker_a_signal_to_pid_1_neither_stops_the_reading_nor_takes_the_exit_code(signal_name):
    docker, handle = await _docker_handle()
    try:
        outcome = await docker.run(handle, ["python", "-c", _attack(signal_name)], timeout=60)
    finally:
        await docker.remove(handle)
    assert (outcome.exit_code, outcome.killed) == (0, None), outcome
    assert outcome.cpu_seconds >= 1.8, outcome.cpu_seconds
    # The forged line, carrying no nonce, is the command's own output and stays in it.
    assert outcome.stdout.endswith(dk.MARK + b" 1 130\n")


@pytest.mark.docker
async def test_on_docker_the_trap_does_not_reach_the_command():
    """Handlers reset on exec: a command that interrupts itself still ends by its own SIGINT."""
    docker, handle = await _docker_handle()
    program = "import os, signal, time\nos.kill(os.getpid(), signal.SIGINT)\ntime.sleep(5)\nprint('survived')\n"
    try:
        outcome = await docker.run(handle, ["python", "-c", program], timeout=60)
    finally:
        await docker.remove(handle)
    # Python ends on a SIGINT it did not handle by that signal: 130, its own exit code, not PID 1's.
    assert (outcome.exit_code, outcome.killed) == (130, None), outcome
    assert b"survived" not in outcome.stdout and b"KeyboardInterrupt" in outcome.stderr


@pytest.mark.docker
async def test_on_docker_stdin_reaches_the_command_whole_after_the_nonce():
    docker, handle = await _docker_handle()
    echo = "import sys\nsys.stdout.buffer.write(sys.stdin.buffer.read())\n"
    data = b"first line\nsecond\n" + bytes(range(256)) + b"no newline at the end"
    try:
        given = await docker.run(handle, ["python", "-c", echo], timeout=60, stdin=data)
        none = await docker.run(handle, ["python", "-c", echo], timeout=60)
    finally:
        await docker.remove(handle)
    assert given.exit_code == 0 and given.stdout == data
    assert none.exit_code == 0 and none.stdout == b""


@pytest.mark.docker
async def test_on_docker_the_nonce_is_beyond_the_commands_reach(monkeypatch):
    nonce = "5eed" * 8
    monkeypatch.setattr(dk, "_nonce", lambda: nonce)
    peek = ("import sys\n"
            "for path in ('/proc/1/environ', '/proc/1/cmdline', '/proc/self/environ', '/proc/1/mem'):\n"
            "    try:\n"
            "        sys.stdout.buffer.write(open(path, 'rb').read(1 << 20))\n"
            "    except OSError as exc:\n"
            "        print(path, type(exc).__name__)\n"
            "sys.stdout.buffer.write(sys.stdin.buffer.read())\n")
    docker, handle = await _docker_handle()
    try:
        outcome = await docker.run(handle, ["python", "-c", peek], timeout=60, stdin=b"given")
    finally:
        await docker.remove(handle)
    assert outcome.exit_code == 0 and b"/proc/1/mem PermissionError" in outcome.stdout
    assert nonce.encode() not in outcome.stdout + outcome.stderr and b"given" in outcome.stdout
    assert outcome.cpu_seconds > 0


@pytest.mark.docker
async def test_on_docker_a_command_with_no_authentic_line_reports_no_exit_code_and_is_charged_its_ceiling(
        monkeypatch):
    """A wrapper stopped before its line, however: here one that never prints it."""
    docker, handle = await _docker_handle(cpus=1.5)
    # Only this command's wrapper: the workspace's own helpers ran under the real one.
    monkeypatch.setattr(dk, "WRAPPER", dk.WRAPPER.split("while kill")[0] + "exit $c")
    try:
        outcome = await docker.run(handle, ["python", "-c", BURN_2], timeout=60)
    finally:
        await docker.remove(handle)
    assert (outcome.exit_code, outcome.killed, outcome.limit) == (None, "error", None), outcome
    assert outcome.cpu_seconds == pytest.approx(outcome.duration * 1.5)
