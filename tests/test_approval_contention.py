"""M32 gate: approvals inside orchestration, and the exit signal (FR-120, FR-121, AC-88, NFR-30,
NFR-31, NFR-22, NFR-33).

A call requiring approval inside a plan's child is recorded against the child, with its
top-level run and node, and is asked for on the top-level run's stream too, where the
top-level handle decides it, at any depth. The child keeps its node's reservation and its
concurrency slot while it waits; other nodes keep running; cancelling the top-level run
decides every pending interruption beneath it cancel. Example 26 is the exit signal: a plan
of writers contending for approvals, run ten times on both stores.

It opens with M32's pre-flight: the follow-ups of M31's review (KNOWLEDGE-86b899ad).

Windows only for the write tools (D11). Store calls from async tests go to a worker thread
(FR-81); the persisted half needs DATABASE_URL and removes every row it writes.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psycopg
import pytest

from agentsdk import AgentSpec, BudgetPolicy, ContextPolicy, Persistence, RunConfig, RunStatus, Runner, SchedulerLimits
from agentsdk.builtin_tools import (
    _fs,
    _list,
    _Root,
    edit_file_tool,
    glob_tool,
    grep_tool,
    list_directory_tool,
    write_file_tool,
)
from agentsdk.config import normalise_database_url
from agentsdk.errors import ToolApprovalCheckFailed
from agentsdk.events import EventType
from agentsdk.identity import PrincipalContext
from agentsdk.interruptions import InMemoryInterruptionStore
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.orchestrator import PLAN_TOOL, Orchestrator
from agentsdk.primitives import Message, Role, ToolCall
from agentsdk.scope import ToolScope
from agentsdk.subagents import Briefing, SubagentPool
from agentsdk.tools import ApprovalPolicy, ApprovedCall, Tool, ToolSpec

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the write tools are Windows-only (D11)")

REPO = Path(__file__).resolve().parent.parent
DSN = normalise_database_url(os.environ.get("DATABASE_URL"))
TENANT, PROJECT = "SYN-m32", "p-m32"
STORES = ["memory", "postgres"]
APPROVER = PrincipalContext(agent_principal="approval-ui", user_principal="alice")


@pytest.fixture(autouse=True)
def remove_what_the_test_wrote():
    yield
    if DSN:
        with psycopg.connect(DSN, autocommit=True) as conn:
            ids = [r[0] for r in conn.execute("SELECT run_id FROM runs WHERE tenant_id = %s", (TENANT,))]
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


def respond(content="", calls=()):
    return ModelResponse(message=Message(role=Role.ASSISTANT, content=content, tool_calls=tuple(calls)),
                         stop_reason=StopReason.TOOL_CALLS if calls else StopReason.END_TURN, usage=Usage(5, 1, 6))


class Turns:
    """A child: its task holds a JSON list of turns, each a list of [tool, arguments]."""

    async def send(self, request):
        text = request.messages[0].content
        turns = json.loads(text[text.index("["):].split("\n")[0])
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


def writer_node(node_id, turns):
    return {"node_id": node_id, "objective": json.dumps(turns), "assigned_role": "writer", "dependencies": []}


def orchestrated(store, root, nodes, slots=8):
    writer = AgentSpec(id="writer", instructions="write", preferred_model="w:w", tool_profile=("write_file", "edit_file"))
    orchestrator = Orchestrator(roles={"writer": writer}, policy=BudgetPolicy(run_ceiling_tokens=1_000_000),
                                limits=SchedulerLimits(max_concurrent_subagents=slots),
                                context_policy=ContextPolicy(context_window=1_000_000))
    runner = Runner({"planner": Planner(nodes), "w": Turns()},
                    tools=[orchestrator.tool, write_file_tool(root), edit_file_tool(root)],
                    persistence=Persistence.postgres(DSN, create_schema=False) if store == "postgres" else None)
    return orchestrator, runner


async def start(orchestrator, runner, **fields):
    return await runner.start(orchestrator.agent, "go", orchestrator.config(
        tenant_id=TENANT, project_id=PROJECT, model_override="planner:p", max_turns=6, **fields))


async def pending(runner, run_id, count, timeout=15.0):
    deadline = time.monotonic() + timeout
    while True:
        views = await runner.interruptions(TENANT, PROJECT, run_id=run_id)
        if len(views) >= count:
            return views
        assert time.monotonic() < deadline, f"only {len(views)} of {count} pending"
        await asyncio.sleep(0.01)


async def until(handle, wanted, timeout=15.0):
    """The first event on the handle's stream for which `wanted` holds. It fails, rather than
    hangs on the run's hour-long approval_timeout, when none comes (M32 round 1, C4)."""

    async def first():
        async for event in handle.events():
            if wanted(event):
                return event
        pytest.fail("the stream ended without the event")

    try:
        return await asyncio.wait_for(first(), timeout)
    except asyncio.TimeoutError:
        pytest.fail(f"no such event on the stream within {timeout} s")


def asked(event):
    return event.event_type is EventType.APPROVAL_REQUESTED


# =================================================================================================
# M32 pre-flight: M31's review follow-ups (KNOWLEDGE-86b899ad)
# =================================================================================================


async def test_an_edit_with_a_huge_old_text_is_refused_in_linear_time(tmp_path):
    """Follow-up 1: counting overlapping occurrences tested every position, so a 1 MB file and
    a 500,000-character old held a file-pool thread for 15 s, before anyone was asked."""
    (tmp_path / "a.txt").write_text("a" * 1_000_000, encoding="utf-8")
    tool = edit_file_tool(tmp_path, max_bytes=2_000_000)
    assert tool.spec.input_schema["properties"]["old"]["maxLength"] == 10_000
    with pytest.raises(Exception, match="more than once"):
        await tool.describe({"path": "a.txt", "old": "a" * 9_999, "new": "b"})
    with pytest.raises(Exception, match="at most 10000"):
        await tool.describe({"path": "a.txt", "old": "a" * 500_000, "new": "b"})
    # Within the cap, a near miss at every position, in its middle (a scan checks the first
    # and last characters before the rest): a scan of every position compares about 5,000
    # characters three million times; a linear search does not.
    (tmp_path / "big.txt").write_text("a" * 3_000_000, encoding="utf-8")
    wide = edit_file_tool(tmp_path, max_bytes=4_000_000)
    started = time.monotonic()
    with pytest.raises(Exception, match="not found"):
        await wide.describe({"path": "big.txt", "old": "a" * 5_000 + "b" + "a" * 4_999, "new": "c"})
    assert time.monotonic() - started < 0.5, time.monotonic() - started


async def test_only_a_write_in_flight_hides_its_temporary_name(tmp_path, monkeypatch):
    """Follow-up 2: every name of the temporary shape was withheld, so a model could hide a
    file there. Now only this process's own temporary files, while they exist, are."""
    lookalike = f".x.txt.agentsdk-{uuid.uuid4()}.tmp"
    (tmp_path / lookalike).write_text("needle", encoding="utf-8")
    for output in (await list_directory_tool(tmp_path).fn(path="."), await glob_tool(tmp_path).fn(pattern="*"),
                   await grep_tool(tmp_path).fn(text="needle")):
        assert lookalike in output, output
    tool = write_file_tool(tmp_path, approval_policy=ApprovalPolicy.AUTO)
    with pytest.raises(Exception, match="reserved"):
        await tool.fn(path=lookalike, content="x")
    root, shown = _Root(tmp_path), []
    real = type(_fs).replace

    def peek(self, source, target):
        # Written and closed, about to replace its target: only the in-flight rule hides
        # it now (while it was open, sharing nothing, a listing could not open it either).
        on_disk = [n for n in os.listdir(tmp_path) if n.startswith(".f.txt.agentsdk-")]
        shown.append((on_disk, _list(root, 1000, None, ".")))
        return real(self, source, target)

    monkeypatch.setattr(type(_fs), "replace", peek)
    await tool.fn(path="f.txt", content="written")
    [(on_disk, listed)] = shown
    assert on_disk and on_disk[0] not in listed, "a write in flight hides its own temporary file"


async def test_a_stale_orphan_of_a_target_is_swept_when_that_target_is_next_written(tmp_path):
    """Follow-up 2: a crash leaves a temporary file nothing removed. A write sweeps its own
    target's orphans older than a minute, and leaves a fresh one, possibly another
    process's write in flight."""
    stale, fresh = (tmp_path / f".f.txt.agentsdk-{uuid.uuid4()}.tmp" for _ in range(2))
    stale.write_text("stale", encoding="utf-8")
    fresh.write_text("fresh", encoding="utf-8")
    old = time.time() - 3600
    os.utime(stale, (old, old))
    await write_file_tool(tmp_path, approval_policy=ApprovalPolicy.AUTO).fn(path="f.txt", content="x")
    assert not stale.exists() and fresh.exists()


async def test_the_sweep_leaves_a_stale_temporary_file_of_another_target(tmp_path):
    """M32 round 1, C8: the sweep matched its target's prefix only, so writing f.txt removed
    the temporary file of a target named f.txt.agentsdk-<uuid>.tmp -- which starts the same."""
    sibling = f"f.txt.agentsdk-{uuid.uuid4()}.tmp"
    theirs = tmp_path / f".{sibling}.agentsdk-{uuid.uuid4()}.tmp"
    ours = tmp_path / f".f.txt.agentsdk-{uuid.uuid4()}.tmp"
    old = time.time() - 3600
    for path in (theirs, ours):
        path.write_text("stale", encoding="utf-8")
        os.utime(path, (old, old))
    await write_file_tool(tmp_path, approval_policy=ApprovalPolicy.AUTO).fn(path="f.txt", content="x")
    assert theirs.exists() and not ours.exists()


async def test_the_sweep_matches_its_target_whatever_the_case(tmp_path):
    """M32 round 2 caveat: names on this filesystem ignore case, so an orphan left by a write
    to F.TXT is f.txt's orphan too."""
    orphan = tmp_path / f".F.TXT.agentsdk-{uuid.uuid4()}.tmp"
    orphan.write_text("stale", encoding="utf-8")
    old = time.time() - 3600
    os.utime(orphan, (old, old))
    tool = write_file_tool(tmp_path, approval_policy=ApprovalPolicy.AUTO)
    await tool.fn(path="f.txt", content="x")
    assert not orphan.exists()
    # So a name the sweep would match is reserved in any case: a model cannot plant one.
    with pytest.raises(Exception, match="reserved"):
        await tool.fn(path=f".F.TXT.AGENTSDK-{str(uuid.uuid4()).upper()}.TMP", content="x")


async def test_decide_wakes_a_waiting_run_at_once_sticky_or_not(tmp_path):
    """Follow-up 4: removing a wake left every test passing, only slower, because the
    five-second re-read masked it. Each decision must reach its run well inside that."""
    for sticky in (False, True):
        ran = []
        tool = Tool(spec=ToolSpec(name="act", description="act", input_schema={"type": "object"},
                                  approval_policy=ApprovalPolicy.REQUIRE_APPROVAL), fn=lambda **a: ran.append(a) or "ok")
        agent = AgentSpec(id="agent", instructions="act", preferred_model="m:x", tool_profile=("act",))

        class One:
            async def send(self, request):
                if not any(m.role is Role.TOOL for m in request.messages):
                    return respond(calls=[ToolCall(id="c1", name="act", arguments={})])
                return respond("done")

        runner = Runner({"m": One()}, tools=[tool])
        handle = await runner.start(agent, "go", RunConfig(tenant_id=TENANT, project_id=PROJECT))
        [view] = await pending(runner, handle.run_id, 1)
        await until(handle, asked)
        await asyncio.sleep(0.2)  # the run is waiting: only a wake reaches it now
        started = time.monotonic()
        await handle.approve(view.interruption.interruption_id, principal=APPROVER, sticky=sticky)
        await handle.result()
        assert ran == [{}] and time.monotonic() - started < 1.0, (sticky, time.monotonic() - started)


async def test_a_non_bool_predicate_fails_by_assertion_not_by_a_wait():
    """M31 review: this mutant was killed by a hang. With a short timeout, a call wrongly
    asked about would expire; the assertion that nothing was asked catches it."""
    tool = Tool(spec=ToolSpec(name="act", description="act", input_schema={"type": "object"}),
                fn=lambda **a: "ran", needs_approval=lambda arguments: "yes")
    agent = AgentSpec(id="agent", instructions="act", preferred_model="m:x", tool_profile=("act",))

    class One:
        async def send(self, request):
            if not any(m.role is Role.TOOL for m in request.messages):
                return respond(calls=[ToolCall(id="c1", name="act", arguments={})])
            return respond("done")

    runner = Runner({"m": One()}, tools=[tool])
    result = await runner.run(agent, "go", RunConfig(tenant_id=TENANT, project_id=PROJECT, approval_timeout=0.3))
    assert await runner.interruptions(TENANT, PROJECT, run_id=result.run_id, pending_only=False) == []


def test_the_readme_says_any_reader_can_block_a_replace():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "any other program reading it" in readme


# =================================================================================================
# FR-120: approvals inside orchestration
# =================================================================================================


@pytest.mark.parametrize("store", STORES)
async def test_a_childs_approval_is_asked_on_the_top_level_run_and_decided_by_its_handle(store, tmp_path):
    nodes = [writer_node("waits", [[["write_file", {"path": "a.txt", "content": "A"}]]]),
             writer_node("free", [[["edit_file", {"path": "missing.txt", "old": "x", "new": "y"}]]])]
    orchestrator, runner = orchestrated(store, tmp_path, nodes)
    handle = await start(orchestrator, runner)
    [view] = await pending(runner, handle.run_id, 1)
    item = view.interruption
    assert item.run_id != handle.run_id and item.top_run_id == handle.run_id and item.node_id == "waits"
    # The child's own stream, held while the child is live (C5: its task_id was unpinned).
    own = runner._live[item.run_id][0]
    on_top = await until(handle, asked)
    assert on_top.task_id == "waits" and on_top.payload["interruption"] == item.interruption_id
    # The other node finished while this one waited.
    await until(handle, lambda e: e.event_type is EventType.PLAN_NODE_FINISHED and e.task_id == "free")
    assert not (tmp_path / "a.txt").exists()
    await handle.approve(item.interruption_id, principal=APPROVER)
    result = await handle.result()
    assert result.status is RunStatus.COMPLETED and (tmp_path / "a.txt").read_text(encoding="utf-8") == "A"
    decided = [e for e in result.events if e.event_type is EventType.APPROVAL_DECIDED]
    assert [e.task_id for e in decided] == ["waits"]
    approvals = (EventType.APPROVAL_REQUESTED, EventType.APPROVAL_DECIDED)
    assert [(e.event_type, e.task_id, e.payload["interruption"]) for e in own.events() if e.event_type in approvals] \
        == [(kind, "waits", item.interruption_id) for kind in approvals]


async def test_a_run_two_levels_down_is_listed_asked_and_decided_from_the_top(tmp_path):
    ran = []
    act = Tool(spec=ToolSpec(name="act", description="act", input_schema={"type": "object"},
                             approval_policy=ApprovalPolicy.REQUIRE_APPROVAL), fn=lambda **a: ran.append(a) or "ok")
    deep = AgentSpec(id="deep", instructions="act", preferred_model="m:x", tool_profile=("act",))

    async def delegate(*, scope: ToolScope) -> str:
        result = await SubagentPool(scope._runner).spawn(
            parent=scope, briefing=Briefing(objective="act once", assigned_role="deep"), agent=deep, lease=None,
            depth=2, node_id="deep-node")
        return result.status.value

    class Model:
        async def send(self, request):
            text = request.messages[0].content
            done = any(m.role is Role.TOOL for m in request.messages)
            name = "act" if "act once" in text else "delegate"
            return respond("done") if done else respond(calls=[ToolCall(id="c1", name=name, arguments={})])

    top_agent = AgentSpec(id="top", instructions="delegate", preferred_model="m:x", tool_profile=("delegate",))
    runner = Runner({"m": Model()}, tools=[act, Tool(spec=ToolSpec(
        name="delegate", description="delegate", input_schema={"type": "object"}), fn=delegate)])
    handle = await runner.start(top_agent, "go", RunConfig(tenant_id=TENANT, project_id=PROJECT,
                                                           approval_timeout=30))
    [view] = await pending(runner, handle.run_id, 1)
    assert view.interruption.run_id != handle.run_id and view.interruption.top_run_id == handle.run_id
    assert (await until(handle, asked)).payload["interruption"] == view.interruption.interruption_id
    # The child inherited the parent's approval_timeout.
    waited = (view.interruption.expires_at - view.interruption.created_at).total_seconds()
    assert abs(waited - 30) < 1, waited
    await handle.approve(view.interruption.interruption_id, principal=APPROVER)
    await handle.result()
    assert ran == [{}]


@pytest.mark.parametrize("store", STORES)
async def test_cancelling_the_top_level_run_decides_cancel_for_every_pending_interruption_beneath_it(store, tmp_path):
    nodes = [writer_node(f"n{i}", [[["write_file", {"path": f"{i}.txt", "content": str(i)}]]]) for i in range(3)]
    orchestrator, runner = orchestrated(store, tmp_path, nodes)
    handle = await start(orchestrator, runner)
    await pending(runner, handle.run_id, 3)
    handle.cancel("stop")
    result = await handle.result()
    assert result.status is RunStatus.CANCELLED
    views = await runner.interruptions(TENANT, PROJECT, run_id=result.run_id, pending_only=False)
    assert len(views) == 3 and {v.decision.source for v in views} == {"cancel"}
    assert not list(tmp_path.glob("*.txt"))


async def test_waiting_children_hold_their_slots_and_the_plan_settles_once_they_are_decided(tmp_path):
    nodes = [writer_node(f"n{i}", [[["write_file", {"path": f"{i}.txt", "content": str(i)}]]]) for i in range(3)]
    orchestrator, runner = orchestrated("memory", tmp_path, nodes, slots=2)
    handle = await start(orchestrator, runner)
    first = await pending(runner, handle.run_id, 2)
    await asyncio.sleep(0.3)
    assert len(await runner.interruptions(TENANT, PROJECT, run_id=handle.run_id)) == 2, "the third waits for a slot"
    for view in first:
        await handle.approve(view.interruption.interruption_id, principal=APPROVER)
    [third] = [v for v in await pending(runner, handle.run_id, 1)]
    await handle.approve(third.interruption.interruption_id, principal=APPROVER)
    result = await handle.result()
    assert result.status is RunStatus.COMPLETED and len(list(tmp_path.glob("*.txt"))) == 3


async def test_a_critic_run_never_records_an_interruption(tmp_path):
    """M32 round 1, C7: this asserted a critic's empty tool profile and never ran one. Now a
    gated node's critic runs, and its model asks for write_file, a tool needing approval,
    before giving its verdict: nothing is asked, and nothing is written."""
    critics = _module("test_critic.py")
    verdict = critics.Critic({"approve": critics.approved})

    class Trying:
        tried = 0

        async def send(self, request):
            if not any(m.role is Role.ASSISTANT for m in request.messages):
                Trying.tried += 1
                return respond(calls=[ToolCall(id="w", name="write_file", arguments={"path": "x.txt", "content": "x"})])
            return await verdict.send(request)

    plan = {"nodes": [critics.node("n", [critics.READ, critics.doubted("The Rhine reaches the North Sea.")],
                                   "approve")]}
    async with critics.Pages() as pages:
        orchestrator, runner = critics.setup(pages, plan, critic=Trying())
        runner._registry.register(write_file_tool(tmp_path))
        # A short approval_timeout: a critic wrongly asked about would expire, and the
        # assertions below fail, rather than the test waiting out the default hour.
        result = await runner.run(orchestrator.agent, "go", orchestrator.config(
            tenant_id=TENANT, project_id=PROJECT, model_override="planner:p", max_turns=8, approval_timeout=1))
    [judged] = [e for e in result.events if e.event_type is EventType.CRITIC_VERDICT]
    assert Trying.tried == 1 and judged.payload["critic_run"]
    assert await runner.interruptions(TENANT, PROJECT, run_id=result.run_id, pending_only=False) == []
    assert not (tmp_path / "x.txt").exists()


# =================================================================================================
# FR-121, AC-88: the exit signal, ten times on both stores
# =================================================================================================


def _module(name, folder="tests"):
    import importlib.util

    spec = importlib.util.spec_from_file_location(Path(name).stem, REPO / folder / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _example():
    return _module("26_approval_contention.py", "scripts")


@pytest.mark.parametrize("store", STORES)
async def test_the_exit_signal_holds_ten_times_with_shuffled_starts_and_random_delays(store):
    example = _example()
    for seed in range(10):
        checks, runs = await example.offline(store, seed=seed, quiet=True, tenant=TENANT, project=f"{PROJECT}-{seed}")
        failed = [label for label, passed in checks if not passed]
        assert failed == [], f"seed {seed}: {failed}"


RACE = "exactly one of the two simultaneous decisions was recorded, and the other refused"
# Edited at M33 (FR-122): the rejection check names b.txt, and a new shared check sees a
# skipped path; listed for review.
REJECTED = "b.txt was rejected by a person, and its reason reached its writer"
EVERY_PATH = "every scripted path but late.txt was decided by a person or a standing decision"
ORDER = "every writer's calls ran in its issue order, and some writer ran more than one"
SCREEN = "every decision the approval screen made was recorded as it made it"
SOURCES = "the sticky approval decided the writer's second edit, a person rejected b.txt, and the late call expired"


@pytest.mark.parametrize("fault, expected", [
    # M32 round 1, C1: the contention check passed when no race happened at all.
    ("no race", [RACE]),
    # M32 round 2, F1: with nobody rejecting b.txt, the rejection check passed over nothing.
    ("b never decided", [REJECTED, EVERY_PATH, SOURCES]),
    # F1: a rejection too late raised InterruptionExpired, which the screen swallowed.
    ("b decided too late", [SCREEN, REJECTED, EVERY_PATH, SOURCES]),
    # Round 2: issue order held vacuously when no writer ran more than one call.
    ("no writer runs twice", [ORDER, "the folder holds exactly what the approved calls wrote",
                              "base.txt's edits ran in turn order, the second needing the first's text",
                              "e.txt's two writes in one turn ran in issue order: the second found the file changed",
                              SOURCES]),
])
async def test_the_exit_signal_fails_when_what_it_checks_does_not_happen(monkeypatch, fault, expected):
    example = _example()
    if fault == "no race":
        async def alone(runner, tenant, project, interruption_id):
            await runner.decide(tenant, project, interruption_id, approve=True, principal=example.APPROVERS[0])
            return {}

        monkeypatch.setattr(example, "decide_twice", alone)
    elif fault == "no writer runs twice":
        monkeypatch.setattr(example, "WRITERS", {name: turns for name, turns in example.WRITERS.items()
                                                 if name not in ("sticky-base", "ordered-e")})
    elif fault == "b never decided":
        monkeypatch.setattr(example, "UNDECIDED", {"late.txt", "b.txt"})
    else:
        monkeypatch.setattr(example, "DELAYED", {"b.txt": example.TIMEOUT + 1})
    checks, _ = await example.offline("memory", seed=0, quiet=True, tenant=TENANT, project=PROJECT)
    assert [label for label, passed in checks if not passed] == expected


def test_example_26_runs_offline():
    done = subprocess.run([sys.executable, str(REPO / "scripts" / "26_approval_contention.py"), "--offline"],
                          capture_output=True, text=True, timeout=240, cwd=REPO)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "[FAIL]" not in done.stdout and "[PASS]" in done.stdout, done.stdout


def test_a_second_test_session_is_refused_while_one_runs(tmp_path):
    """M32 round 1: two sessions at once on the shared database broke AC-44 at M31 and M32.
    A session started outside this one -- not inheriting its variable -- stops at once."""
    (tmp_path / "conftest.py").write_bytes((REPO / "tests" / "conftest.py").read_bytes())
    (tmp_path / "pytest.ini").write_text("[pytest]\nasyncio_mode = auto\n")
    (tmp_path / "test_inner.py").write_text("def test_nothing():\n    pass\n")
    env = {name: value for name, value in os.environ.items() if name != "AGENTSDK_TEST_SESSION"}
    done = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", str(tmp_path)],
                          cwd=tmp_path, env={**env, "PYTHONPATH": str(REPO)}, capture_output=True, text=True,
                          timeout=120)
    output = done.stdout + done.stderr
    if DSN:
        output = output.replace(DSN, "<dsn>")
    assert done.returncode == 3 and "another test session" in output and "passed" not in output, output[-2000:]


def test_no_new_runtime_dependency():
    """NFR-33: Phase 4's first increment adds no runtime dependency."""
    import tomllib

    declared = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    assert sorted(declared) == ["httpx==0.28.1", "jsonschema==4.26.0", "psycopg[binary]==3.3.5",
                                "psycopg_pool==3.2.6", "python-dotenv==1.2.3"]
