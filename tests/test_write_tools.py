"""M31 gate: write_file and edit_file (FR-118, FR-119, AC-87, NFR-30, NFR-33, NFR-14).

The first tools that change anything. Both are confined to their root as M10's file tools
are, write atomically -- a hidden temporary file beside the target, then one replace -- and
need approval by default. The approver is shown the change and its before and after hashes;
at execution the target must still be what the approver saw, or nothing is written.

It opens with M31's pre-flight: the caveats of M30's review (KNOWLEDGE-ebc4b9fd), the first
two of which let an approval gate fail open.

Windows only, with Developer Mode on (D7). Store calls made from async tests go to a worker
thread (FR-81); the persisted half needs DATABASE_URL and removes every row it writes.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import psycopg
import pytest

from agentsdk import AgentSpec, Persistence, RunConfig, RunStatus, Runner
from agentsdk.builtin_tools import edit_file_tool, glob_tool, grep_tool, list_directory_tool, write_file_tool
from agentsdk.config import normalise_database_url
from agentsdk.errors import ToolApprovalCheckFailed
from agentsdk.events import EventType
from agentsdk.hooks import HookAction, HookOutcome, RuntimeHook
from agentsdk.identity import PrincipalContext
from agentsdk.interruptions import InMemoryInterruptionStore, InterruptionDecision
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.primitives import Message, Role, ToolCall
from agentsdk.tools import ApprovalPolicy, ApprovedCall, RiskClass, Tool, ToolSpec

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the file tools are Windows-only (D11)")

REPO = Path(__file__).resolve().parent.parent
DSN = normalise_database_url(os.environ.get("DATABASE_URL"))
TENANT, PROJECT = "SYN-m31", "p-m31"
STORES = ["memory", "postgres"]
APPROVER = PrincipalContext(agent_principal="approval-ui", user_principal="alice")
TEMP = re.compile(r"\.agentsdk-[0-9a-f-]{36}\.tmp$")


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
            for table in ("run_events", "messages", "execution_manifests"):
                conn.execute(f"DELETE FROM {table} WHERE run_id = ANY(%s)", (ids,))
            conn.execute("DELETE FROM runs WHERE run_id = ANY(%s)", (ids,))


def respond(content="", calls=()):
    return ModelResponse(message=Message(role=Role.ASSISTANT, content=content, tool_calls=tuple(calls)),
                         stop_reason=StopReason.TOOL_CALLS if calls else StopReason.END_TURN, usage=Usage(5, 1, 6))


class Script:
    def __init__(self, *turns):
        self.turns, self.seen = list(turns), []

    async def send(self, request):
        self.seen.append(request)
        turn = sum(1 for m in request.messages if m.role is Role.ASSISTANT)
        if turn < len(self.turns):
            return respond(calls=[ToolCall(id=cid, name=name, arguments=args) for name, args, cid in self.turns[turn]])
        return respond("done")


async def pending(runner, run_id, count, timeout=10.0):
    deadline = time.monotonic() + timeout
    while True:
        views = await runner.interruptions(TENANT, PROJECT, run_id=run_id)
        if len(views) >= count:
            return sorted(views, key=lambda v: v.interruption.tool_call_id)
        assert time.monotonic() < deadline, f"only {len(views)} of {count} pending"
        await asyncio.sleep(0.01)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def config(**fields):
    return RunConfig(tenant_id=TENANT, project_id=PROJECT, max_turns=6, **fields)


# =================================================================================================
# M31 pre-flight: M30's review caveats (KNOWLEDGE-ebc4b9fd)
# =================================================================================================


def _recording(name, ran, **fields):
    def fn(**arguments):
        ran.append(dict(arguments))
        return "ran"
    return Tool(spec=ToolSpec(name=name, description=name, input_schema={"type": "object"}, **fields), fn=fn, )


async def test_what_runs_is_exactly_what_was_approved_even_if_a_hook_keeps_changing_its_call():
    """Caveat 1: the request held a shallow copy while the live arguments ran, so a hook
    holding its replacement could change the call after the approver saw it."""
    ran, kept = [], []

    class Keeps(RuntimeHook):
        def before_tool(self, tool_call):
            replacement = dataclasses.replace(tool_call, arguments={"x": {"nested": "approved"}})
            kept.append(replacement)
            return HookOutcome(HookAction.MODIFY, replacement=replacement)

    tool = _recording("act", ran, approval_policy=ApprovalPolicy.REQUIRE_APPROVAL)
    agent = AgentSpec(id="agent", instructions="act", preferred_model="m:x", tool_profile=("act",))
    runner = Runner({"m": Script([("act", {"x": "asked"}, "c1")])}, tools=[tool], hook=Keeps())
    handle = await runner.start(agent, "go", config())
    [view] = await pending(runner, handle.run_id, 1)
    kept[0].arguments["x"]["nested"] = "changed after approval was asked"
    kept[0].arguments["y"] = "added"
    view.interruption.request_payload["arguments"]["x"]["nested"] = "changed in the record"
    await handle.approve(view.interruption.interruption_id, principal=APPROVER)
    await handle.result()
    [stored] = await runner.interruptions(TENANT, PROJECT, run_id=handle.run_id, pending_only=False)
    assert ran == [{"x": {"nested": "approved"}}]
    assert stored.interruption.request_payload["arguments"] == {"x": {"nested": "approved"}}


@pytest.mark.parametrize("answer", [1, "yes", None, 0, [True]])
async def test_a_predicate_answering_anything_but_a_bool_fails_the_call_closed(answer):
    """Caveat 2: only the True singleton required approval, so 1 or 'yes' ran unasked."""
    ran = []
    tool = Tool(spec=ToolSpec(name="act", description="act", input_schema={"type": "object"}),
                fn=lambda **a: ran.append(a) or "ran", needs_approval=lambda arguments: answer)
    model = Script([("act", {}, "c1")])
    agent = AgentSpec(id="agent", instructions="act", preferred_model="m:x", tool_profile=("act",))
    runner = Runner({"m": model}, tools=[tool])
    result = await runner.run(agent, "go", config())
    assert result.status is RunStatus.COMPLETED and ran == []
    [message] = [m for m in model.seen[-1].messages if m.tool_results]
    assert ToolApprovalCheckFailed.__name__ in message.tool_results[0].content
    assert await runner.interruptions(TENANT, PROJECT, run_id=result.run_id, pending_only=False) == []


async def test_a_predicate_answering_false_runs_the_call_unasked():
    ran = []
    tool = Tool(spec=ToolSpec(name="act", description="act", input_schema={"type": "object"}),
                fn=lambda **a: ran.append(a) or "ran", needs_approval=lambda arguments: False)
    agent = AgentSpec(id="agent", instructions="act", preferred_model="m:x", tool_profile=("act",))
    runner = Runner({"m": Script([("act", {}, "c1")])}, tools=[tool])
    await runner.run(agent, "go", config())
    assert ran == [{}]


async def test_every_asked_interruption_emits_approval_requested_even_if_decided_at_once(monkeypatch):
    """Caveat 3: an approver deciding between recording and the run's first read left the
    interruption with no ApprovalRequested event (FR-114)."""
    ran = []
    tool = _recording("act", ran, approval_policy=ApprovalPolicy.REQUIRE_APPROVAL)
    agent = AgentSpec(id="agent", instructions="act", preferred_model="m:x", tool_profile=("act",))
    runner = Runner({"m": Script([("act", {}, "c1")])}, tools=[tool])
    real = InMemoryInterruptionStore.append_interruption

    def decided_at_once(self, item, decision=None):
        real(self, item, decision)
        from datetime import datetime, timezone
        self.append_decision(InterruptionDecision(
            decision_id=str(uuid.uuid4()), interruption_id=item.interruption_id, tenant_id=item.tenant_id,
            project_id=item.project_id, decision="approve", source="principal", principal=APPROVER,
            decided_at=datetime.now(timezone.utc)))

    monkeypatch.setattr(InMemoryInterruptionStore, "append_interruption", decided_at_once)
    result = await runner.run(agent, "go", config())
    kinds = [e.event_type for e in result.events]
    assert ran == [{}] and kinds.count(EventType.APPROVAL_REQUESTED) == 1
    assert kinds.index(EventType.APPROVAL_REQUESTED) < kinds.index(EventType.APPROVAL_DECIDED)


async def test_a_sticky_decision_whose_standing_write_fails_still_wakes_the_run(monkeypatch):
    """Caveat 4: the decision was recorded but no wake was sent, so the run found it only on
    its five-second re-read."""
    ran = []
    tool = _recording("act", ran, approval_policy=ApprovalPolicy.REQUIRE_APPROVAL)
    agent = AgentSpec(id="agent", instructions="act", preferred_model="m:x", tool_profile=("act",))
    runner = Runner({"m": Script([("act", {}, "c1")])}, tools=[tool])
    handle = await runner.start(agent, "go", config())
    [view] = await pending(runner, handle.run_id, 1)
    async for event in handle.events():
        if event.event_type is EventType.APPROVAL_REQUESTED:
            break

    def broken(self, standing):
        raise RuntimeError("the store is down")

    monkeypatch.setattr(InMemoryInterruptionStore, "append_standing", broken)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="standing"):
        await handle.approve(view.interruption.interruption_id, principal=APPROVER, sticky=True)
    await handle.result()
    assert ran == [{}] and time.monotonic() - started < 2.0


# =================================================================================================
# FR-118: the tools
# =================================================================================================


@pytest.fixture()
def root(tmp_path):
    base = tmp_path / "root"
    base.mkdir()
    (base / "docs").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside", encoding="utf-8")
    return base


async def approve(tool, arguments):
    """What the executor does for an approved call: describe, then run with that request."""
    described = await tool.describe(dict(arguments))
    approved = ApprovedCall(interruption_id=str(uuid.uuid4()), request={"arguments": dict(arguments),
                                                                         "describe": described})
    return described, await tool.fn(**arguments, approved=approved)


def no_temp_left(root):
    return not [p for p in root.rglob("*") if TEMP.search(p.name)]


def test_the_tools_declare_what_they_are_and_need_approval_by_default(root):
    for tool in (write_file_tool(root), edit_file_tool(root)):
        spec = tool.spec
        assert spec.approval_policy is ApprovalPolicy.REQUIRE_APPROVAL and spec.risk_class is RiskClass.WRITE
        assert (spec.read_only, spec.idempotent, spec.concurrency_safe) == (False, False, False)
        assert spec.input_schema["additionalProperties"] is False and "approved" not in spec.input_schema["properties"]
        assert tool.describe is not None


async def test_write_file_creates_and_replaces_a_file(root):
    tool = write_file_tool(root)
    described, result = await approve(tool, {"path": "docs/new.txt", "content": "hello\n"})
    assert described["path"] == "docs/new.txt" and described["before"] is None
    assert described["after"] == sha(b"hello\n") and described["bytes_after"] == 6
    assert (root / "docs" / "new.txt").read_bytes() == b"hello\n" and sha(b"hello\n") in result
    described, _ = await approve(tool, {"path": "docs/new.txt", "content": "bye"})
    assert described["before"] == sha(b"hello\n") and (root / "docs" / "new.txt").read_bytes() == b"bye"
    assert no_temp_left(root)


async def test_edit_file_replaces_the_one_occurrence_and_keeps_every_other_byte(root):
    target = root / "notes.txt"
    original = b"\xef\xbb\xbfline one\r\nthe river\r\nline three\r\n"
    target.write_bytes(original)
    tool = edit_file_tool(root)
    described, _ = await approve(tool, {"path": "notes.txt", "old": "river", "new": "Rhine"})
    assert described["line"] == 2 and described["before"] == sha(original)
    assert target.read_bytes() == b"\xef\xbb\xbfline one\r\nthe Rhine\r\nline three\r\n"
    assert described["after"] == sha(target.read_bytes())


@pytest.mark.parametrize("text, old", [("a b a", "a"), ("aaa", "aa"), ("nothing here", "missing")])
async def test_edit_file_refuses_an_old_text_absent_or_not_unique_overlapping_included(root, text, old):
    target = root / "f.txt"
    target.write_text(text, encoding="utf-8")
    tool = edit_file_tool(root)
    with pytest.raises(Exception, match="occurs|not found"):
        await tool.describe({"path": "f.txt", "old": old, "new": "x"})
    assert target.read_text(encoding="utf-8") == text and no_temp_left(root)


def _junction(link, target):
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)


@pytest.mark.parametrize("case", ["missing parent", "dot dot", "absolute", "device", "short name", "directory",
                                  "symlink target", "junction outside", "hard link", "non utf-8 edit",
                                  "oversized content", "oversized file"])
async def test_every_refusal_writes_nothing_leaves_no_temporary_file_and_names_no_outside_path(root, case):
    outside = root.parent / "outside"
    (root / "plain.txt").write_text("plain", encoding="utf-8")
    write, edit = write_file_tool(root, max_bytes=100), edit_file_tool(root, max_bytes=100)
    tool, arguments = write, {"path": "x.txt", "content": "new"}
    if case == "missing parent":
        arguments = {"path": "nope/x.txt", "content": "new"}
    elif case == "dot dot":
        arguments = {"path": "../outside/secret.txt", "content": "new"}
    elif case == "absolute":
        arguments = {"path": str(outside / "secret.txt"), "content": "new"}
    elif case == "device":
        arguments = {"path": "docs/NUL", "content": "new"}
    elif case == "short name":
        arguments = {"path": "LONGFI~1.TXT", "content": "new"}
    elif case == "directory":
        arguments = {"path": "docs", "content": "new"}
    elif case == "symlink target":
        os.symlink(root / "plain.txt", root / "link.txt")
        arguments = {"path": "link.txt", "content": "new"}
    elif case == "junction outside":
        _junction(root / "away", outside)
        arguments = {"path": "away/secret.txt", "content": "new"}
    elif case == "hard link":
        os.link(root / "plain.txt", root / "twin.txt")
        arguments = {"path": "twin.txt", "content": "new"}
    elif case == "non utf-8 edit":
        (root / "bin.dat").write_bytes(b"\xff\xfe\x00bad")
        tool, arguments = edit, {"path": "bin.dat", "old": "bad", "new": "good"}
    elif case == "oversized content":
        arguments = {"path": "x.txt", "content": "x" * 101}
    elif case == "oversized file":
        (root / "big.txt").write_text("y" * 150, encoding="utf-8")
        tool, arguments = edit, {"path": "big.txt", "old": "y", "new": "z"}
    before = {p: p.read_bytes() for p in root.parent.rglob("*") if p.is_file() and not p.is_symlink()}
    with pytest.raises(Exception) as refused:
        await approve(tool, arguments)
    after = {p: p.read_bytes() for p in root.parent.rglob("*") if p.is_file() and not p.is_symlink()}
    assert after == before, "nothing written"
    assert no_temp_left(root)
    assert str(outside) not in str(refused.value) and "secret" not in str(refused.value)


async def test_a_target_changed_since_its_approval_is_refused_and_left_as_it_is(root):
    target = root / "f.txt"
    target.write_text("first", encoding="utf-8")
    tool = write_file_tool(root)
    described = await tool.describe({"path": "f.txt", "content": "approved"})
    target.write_text("changed meanwhile", encoding="utf-8")
    approved = ApprovedCall(interruption_id=str(uuid.uuid4()),
                            request={"arguments": {"path": "f.txt", "content": "approved"}, "describe": described})
    with pytest.raises(Exception, match="changed since approval"):
        await tool.fn(path="f.txt", content="approved", approved=approved)
    assert target.read_text(encoding="utf-8") == "changed meanwhile" and no_temp_left(root)


async def test_a_target_held_open_is_refused_in_use(root):
    target = root / "f.txt"
    target.write_text("first", encoding="utf-8")
    tool = write_file_tool(root)
    with open(target, "rb"):
        with pytest.raises(Exception, match="in use"):
            await approve(tool, {"path": "f.txt", "content": "second"})
    assert target.read_text(encoding="utf-8") == "first" and no_temp_left(root)


async def test_a_reader_polling_the_target_sees_only_the_old_or_the_new_content(root):
    target = root / "f.txt"
    contents = [("A" * 50_000).encode(), ("B" * 70_000).encode()]
    target.write_bytes(contents[0])
    tool = write_file_tool(root)
    seen, stop = set(), threading.Event()

    def poll():
        while not stop.is_set():
            try:
                seen.add(target.read_bytes())
            except OSError:
                pass  # a failed open is no read

    reader = threading.Thread(target=poll)
    reader.start()
    written = refused = 0
    try:
        for i in range(30):
            # A reader holding the file open at that moment makes the replace fail: the
            # write is refused in use, by design (FR-118), and tried again.
            for _ in range(200):
                try:
                    await approve(tool, {"path": "f.txt", "content": contents[(i + 1) % 2].decode()})
                    written += 1
                    break
                except Exception as exc:  # noqa: BLE001
                    assert "in use" in str(exc), exc
                    refused += 1
    finally:
        stop.set()
        reader.join()
    assert written == 30 and no_temp_left(root)
    assert seen and seen <= set(contents), {len(s) for s in seen}


async def test_list_glob_and_grep_show_a_file_merely_named_like_a_temporary_one(root):
    """Edited at M32's pre-flight (M31 review follow-up 2): withholding every name of this
    shape let a model hide a file there. Only a write's own temporary file, while in
    flight, is withheld now (tests/test_approval_contention.py)."""
    lookalike = f".f.txt.agentsdk-{uuid.uuid4()}.tmp"
    (root / lookalike).write_text("needle", encoding="utf-8")
    (root / "shown.txt").write_text("needle", encoding="utf-8")
    listed = await list_directory_tool(root).fn(path=".")
    globbed = await glob_tool(root).fn(pattern="**/*")
    grepped = await grep_tool(root).fn(text="needle")
    for output in (listed, globbed, grepped):
        assert "shown.txt" in output and lookalike in output


async def test_without_an_approved_request_the_tool_writes_with_no_before_check(root):
    """An application may set approval_policy to AUTO: then no request was approved, and
    the write is made as asked."""
    tool = write_file_tool(root, approval_policy=ApprovalPolicy.AUTO)
    await tool.fn(path="f.txt", content="direct")
    assert (root / "f.txt").read_text(encoding="utf-8") == "direct" and no_temp_left(root)


@pytest.mark.parametrize("store", STORES)
async def test_an_agent_writes_a_file_only_once_a_person_approves_what_it_was_shown(store, root):
    write, edit = write_file_tool(root), edit_file_tool(root)
    (root / "plan.txt").write_text("We sail on Monday.", encoding="utf-8")
    model = Script([("write_file", {"path": "docs/a.txt", "content": "alpha"}, "c1"),
                    ("edit_file", {"path": "plan.txt", "old": "Monday", "new": "Tuesday"}, "c2")])
    agent = AgentSpec(id="agent", instructions="act", preferred_model="m:x", tool_profile=("write_file", "edit_file"))
    runner = Runner({"m": model}, tools=[write, edit],
                    persistence=Persistence.postgres(DSN, create_schema=False) if store == "postgres" else None)
    handle = await runner.start(agent, "go", config())
    first, second = await pending(runner, handle.run_id, 2)
    assert not (root / "docs" / "a.txt").exists(), "nothing is written before the decision"
    assert first.interruption.request_payload["describe"]["after"] == sha(b"alpha")
    assert second.interruption.request_payload["describe"]["line"] == 1
    await handle.approve(first.interruption.interruption_id, principal=APPROVER)
    await handle.reject(second.interruption.interruption_id, principal=APPROVER, message="keep Monday")
    result = await handle.result()
    assert result.status is RunStatus.COMPLETED
    assert (root / "docs" / "a.txt").read_text(encoding="utf-8") == "alpha"
    assert (root / "plan.txt").read_text(encoding="utf-8") == "We sail on Monday."


# =================================================================================================
# FR-119: example 25, offline on both stores
# =================================================================================================


def _example():
    import importlib.util

    spec = importlib.util.spec_from_file_location("example_25", REPO / "scripts" / "25_write_tools.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("store", STORES)
async def test_example_25_runs_offline_with_every_check_passing(store):
    checks, runs = await _example().offline(store, quiet=True)
    failed = [label for label, passed in checks if not passed]
    assert len(checks) >= 6 and not failed, failed
    if store == "postgres":
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("DELETE FROM interruption_decisions WHERE interruption_id IN (SELECT interruption_id"
                         " FROM interruptions WHERE run_id = ANY(%s::uuid[]))", (runs,))
            conn.execute("DELETE FROM interruptions WHERE run_id = ANY(%s::uuid[])", (runs,))
            conn.execute("DELETE FROM standing_decisions WHERE run_id = ANY(%s::uuid[])", (runs,))
            for table in ("run_events", "messages", "execution_manifests"):
                conn.execute(f"DELETE FROM {table} WHERE run_id = ANY(%s::uuid[])", (runs,))
            conn.execute("DELETE FROM runs WHERE run_id = ANY(%s::uuid[])", (runs,))


def test_example_25_runs_offline():
    done = subprocess.run([sys.executable, str(REPO / "scripts" / "25_write_tools.py"), "--offline"],
                          capture_output=True, text=True, timeout=180, cwd=REPO)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "[FAIL]" not in done.stdout and "[PASS]" in done.stdout, done.stdout


def test_the_store_never_hands_out_its_own_record():
    """M31 pre-flight, caveat 1's second half: no caller's object aliases a recorded
    request -- not the one it recorded, nor any it reads back, by either lookup."""
    from datetime import datetime, timedelta, timezone
    from agentsdk.outcomes import InterruptionKind, RunInterruption

    run = str(uuid.uuid4())
    store = InMemoryInterruptionStore(runs=lambda tenant, project, run_id: True)
    now = datetime.now(timezone.utc)
    item = RunInterruption(kind=InterruptionKind.TOOL_APPROVAL, run_id=run, request_payload={"arguments": {"x": "1"}},
                           tool_call_id="c", created_at=now, tenant_id=TENANT, project_id=PROJECT, top_run_id=run,
                           tool_name="t", agent_id="a", trigger="policy", expires_at=now + timedelta(hours=1))
    store.append_interruption(item)
    item.request_payload["arguments"]["x"] = "changed by the recorder"
    store.interruption(TENANT, PROJECT, item.interruption_id).interruption.request_payload["arguments"]["x"] = "one"
    store.interruptions(TENANT, PROJECT)[0].interruption.request_payload["arguments"]["x"] = "many"
    assert store.interruption(TENANT, PROJECT, item.interruption_id).interruption.request_payload == {
        "arguments": {"x": "1"}}


async def test_a_directory_target_is_refused_as_a_directory(root):
    with pytest.raises(Exception, match="directory cannot be written"):
        await approve(write_file_tool(root), {"path": "docs", "content": "x"})
