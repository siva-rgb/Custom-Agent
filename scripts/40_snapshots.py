"""40 - Snapshots: a workspace built up, snapshotted, changed, and a second one started from it.

A run builds a workspace -- a file, a folder, a link -- and pauses between commands; the
application snapshots it (FR-168); the run then changes it and ends, its workspace kept. A second
run, its spec naming the snapshot (FR-169), reads the state at the snapshot: the first file as it
was, no file written after, the link still a link. Each check stands against a control: the kept
workspace does hold the change; the same snapshot fails another tenancy's workspace and, once
released, this one's. After the release and `aclose()`, nothing of either workspace or the
snapshot remains (DECISION-bafa7be8).

The application takes snapshots; no tool does (P5-D22), so every mode uses a scripted model.

Run it
  python scripts/40_snapshots.py             # PostgreSQL + Docker
  python scripts/40_snapshots.py --offline   # in memory, FakeProvider
  python scripts/40_snapshots.py --scripted  # in memory, Docker

Docker modes need Docker running and the image below on the host:
  docker pull python:3.12-alpine@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid

from agentsdk import AgentSpec, Message, Role, RunConfig, RunStatus, Runner
from agentsdk.events import EventType
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.postgres import RunScope
from agentsdk.primitives import ToolCall
from agentsdk.tools import ApprovalPolicy
from agentsdk.workspace import DockerProvider, FakeProvider, WorkspaceSpec
from agentsdk.workspace.tools import one_file

PROJECT = "examples"
IMAGE = "python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df"
FAKE_IMAGE = "fake/workspace@sha256:" + "0" * 64
TOOLS = ("run_command", "read_workspace_file", "write_workspace_file", "list_workspace")


async def _mkdir_b(cmd):
    cmd.files.write("dir/b.txt", b"two")
    return 0


async def _link(cmd):
    cmd.files.link(cmd.argv[3], cmd.argv[2])  # ln -s TARGET PATH
    return 0


def provider(name: str):
    return DockerProvider() if name == "docker" else FakeProvider(commands={"sh": _mkdir_b, "ln": _link})


BUILT = [
    [["write_workspace_file", {"path": "a.txt", "content": "one"}],
     ["run_command", {"argv": ["sh", "-c", "mkdir -p dir && printf two > dir/b.txt"]}],
     ["run_command", {"argv": ["ln", "-s", "a.txt", "l"]}]],
    "pause",
    [["write_workspace_file", {"path": "a.txt", "content": "changed"}],
     ["write_workspace_file", {"path": "c.txt", "content": "later"}]],
]
READ_BACK = [[["read_workspace_file", {"path": "a.txt"}], ["read_workspace_file", {"path": "dir/b.txt"}],
              ["list_workspace", {"path": "."}]]]


class ScriptedModel:
    """The task holds a JSON list of turns; a turn "pause" holds back the next one until `go`,
    telling `paused`: the application's moment between commands."""

    def __init__(self):
        self.paused, self.go = asyncio.Event(), asyncio.Event()

    async def send(self, request):
        text = request.messages[0].content
        written = json.loads(text[text.index("["):].split("\n")[0])
        turns = [t for t in written if t != "pause"]
        paused_before = {i - written[:i].count("pause") for i, t in enumerate(written) if t == "pause"}
        turn = sum(1 for m in request.messages if m.role is Role.ASSISTANT)
        if turn in paused_before:
            self.paused.set()
            await self.go.wait()
        calls = [ToolCall(id=f"t{turn}-{i}", name=name, arguments=args)
                 for i, (name, args) in enumerate(turns[turn])] if turn < len(turns) else []
        return ModelResponse(message=Message(role=Role.ASSISTANT, content="" if calls else "done",
                                             tool_calls=tuple(calls)),
                             stop_reason=StopReason.TOOL_CALLS if calls else StopReason.END_TURN,
                             usage=Usage(5, 1, 6))


def agent(spec: WorkspaceSpec) -> AgentSpec:
    return AgentSpec(id="builder", instructions="use the workspace", preferred_model="scripted:model",
                     tool_profile=TOOLS, workspace=spec)


async def demonstrate(store_name: str, provider_name: str, tenant: str):
    """The checks as (label, passed, why); passed is None when the subject never happened."""
    persistence = None
    if store_name == "postgres":
        from dotenv import load_dotenv

        from agentsdk import Persistence
        from agentsdk.config import normalise_database_url

        load_dotenv()
        persistence = Persistence.postgres(normalise_database_url(os.environ["DATABASE_URL"]), create_schema=False)
    chosen, model = provider(provider_name), ScriptedModel()
    image = IMAGE if provider_name == "docker" else FAKE_IMAGE
    runner = Runner({"scripted": model}, workspace_provider=chosen, persistence=persistence)
    checks = []

    def check(label, subject, missing, verdict):
        if not subject:
            checks.append((label, None, missing))
            return
        why = verdict()
        checks.append((label, why is None, why))

    def spec(**fields):
        return WorkspaceSpec(image=image, run_command_approval=ApprovalPolicy.AUTO, **fields)

    async def said(result):
        sessions = runner._sessions_for(RunScope(run_id=result.run_id, tenant_id=result_tenant[result.run_id],
                                                 project_id=PROJECT))
        history = await asyncio.to_thread(sessions.history, result.run_id)
        return {r.tool_call_id: r for m in history for r in m.tool_results}

    def made(result):
        return [e.payload["workspace_id"] for e in result.events if e.event_type is EventType.WORKSPACE_CREATED]

    result_tenant = {}

    async def run(workspace, turns, owner=tenant):
        result = await runner.run(agent(workspace), json.dumps(turns), RunConfig(tenant_id=owner, project_id=PROJECT))
        result_tenant[result.run_id] = owner
        return result

    ref, kept_id = None, None
    try:
        handle = await runner.start(agent(spec(keep=True)), json.dumps(BUILT),
                                    RunConfig(tenant_id=tenant, project_id=PROJECT))
        await asyncio.wait_for(model.paused.wait(), 300)
        owned = await chosen.list_owned({"agentsdk.tenant": tenant, "agentsdk.project": PROJECT})
        kept_id = owned[0].workspace_id if owned else None
        ref = await runner.snapshot_workspace(tenant, PROJECT, kept_id) if kept_id else None
        model.go.set()
        built = await handle.result()
        result_tenant[built.run_id] = tenant
        print(f"snapshot {ref.snapshot_id if ref else None} of workspace {kept_id}: "
              f"{ref.size_bytes if ref else '-'} B; the run ended {built.status.value}")

        # The control: the kept workspace does hold what was written after the snapshot.
        now_held = None
        if kept_id and built.status is RunStatus.COMPLETED:
            [kept] = [h for h in await chosen.list_owned({"agentsdk.workspace": kept_id})]
            try:
                now_held = one_file(await chosen.export(kept, "a.txt"), "a.txt").decode()
            except Exception as exc:  # noqa: BLE001 - a failed check, never a crash (M46 pre-review)
                now_held = f"unreadable ({type(exc).__name__})"
        check("the kept workspace was changed after the snapshot (the control)", now_held is not None,
              "the building run never ended with its workspace kept",
              lambda: None if now_held == "changed" else f"it reads {now_held!r}")

        restored = await run(spec(from_snapshot=ref.snapshot_id), READ_BACK) if ref else None
        got = await said(restored) if restored else {}
        listing = got["t0-2"].content if "t0-2" in got else ""
        check("a second workspace started from the snapshot reads the state at the snapshot", restored,
              "no snapshot was taken",
              lambda: None if (got["t0-0"].content, got["t0-1"].content) == ("one", "two")
              and "c.txt" not in listing else f"it read {got['t0-0'].content!r}, listing {listing!r}")
        check("the link is still a link", restored, "no snapshot was taken",
              lambda: None if "l\tlink" in listing else f"the listing reads {listing!r}")
        check("the second workspace is a workspace of its own", restored, "no snapshot was taken",
              lambda: None if made(restored) and made(restored)[0] != kept_id else f"it made {made(restored)}")

        # Refused for its tenancy, not for any failure (M47 round 1 F1, DECISION-ee543c57): the other
        # tenancy starts a workspace of its own (the control), and cannot release the snapshot either --
        # the same lookup, answered by a value, not by an error's text.
        other = tenant + "-other"
        elsewhere = await run(spec(from_snapshot=ref.snapshot_id), READ_BACK, owner=other) if ref else None
        plain = await run(spec(), READ_BACK, owner=other) if ref else None
        theirs = await runner.release_snapshot(other, PROJECT, ref.snapshot_id) if ref else None
        check("another tenancy's workspace cannot start from it", elsewhere and restored, "no snapshot was taken",
              lambda: (None if not made(elsewhere) and made(restored) else f"it made {made(elsewhere)}")
              or (None if made(plain) else "the other tenancy could start no workspace at all")
              or (None if theirs is False else f"the other tenancy's release answered {theirs}"))

        released = await runner.release_snapshot(tenant, PROJECT, ref.snapshot_id) if ref else False
        after = await run(spec(from_snapshot=ref.snapshot_id), READ_BACK) if released else None
        listed = await chosen.list_snapshots({"agentsdk.tenant": tenant, "agentsdk.project": PROJECT}) if released \
            else []
        check("once released, it starts no workspace", after and restored, "the snapshot was never released",
              lambda: (None if not made(after) and made(restored) else f"it made {made(after)}")
              or (None if not listed else f"{len(listed)} snapshot(s) still listed after the release"))
    finally:
        model.go.set()
        await runner.aclose()
    left = await chosen.list_owned({"agentsdk.tenant": tenant, "agentsdk.project": PROJECT})
    snapshots = await chosen.list_snapshots({"agentsdk.tenant": tenant, "agentsdk.project": PROJECT})
    store = runner._workspace_store()
    rows = await asyncio.to_thread(store.snapshots, tenant, PROJECT)
    check("after the release and aclose() nothing remains, the snapshot's row removed `released`", ref,
          "no snapshot was taken",
          lambda: f"{len(left)} workspace(s) remained" if left else f"{len(snapshots)} snapshot(s) remained"
          if snapshots else None if [r.removed_reason for r in rows] == ["released"] else f"its rows read {rows}")
    return checks, list(result_tenant)


def report(checks) -> list[str]:
    """A check whose subject never happened fails, in every mode."""
    failed = []
    for label, passed, why in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {'' if passed is not None else 'not exercised: '}{label}"
              + (f" -- {why}" if why and not passed else ""))
        if not passed:
            failed.append(label)
    return failed


def docker_ready() -> str | None:
    import subprocess

    try:
        if subprocess.run(["docker", "version"], capture_output=True, timeout=30).returncode != 0:
            return "docker not running"
        if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, timeout=30).returncode != 0:
            return "the example image is not on this host (see the docstring to pull it)"
    except (OSError, subprocess.TimeoutExpired):
        return "docker not running"
    return None


async def offline(store: str = "memory", provider_name: str = "fake", *, tenant: str | None = None):
    checks, _ = await demonstrate(store, provider_name, tenant or f"example-{uuid.uuid4().hex[:8]}")
    return checks


if __name__ == "__main__":
    import argparse

    sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="in memory, FakeProvider")
    mode.add_argument("--scripted", action="store_true", help="in memory, Docker")
    args = parser.parse_args()
    if not args.offline and (why := docker_ready()) is not None:
        print(f"[SKIP] {why}")
        raise SystemExit(0)
    if args.offline or args.scripted:
        checks = asyncio.run(offline(provider_name="fake" if args.offline else "docker"))
    else:
        from agentsdk.postgres import close_pools

        try:
            checks, runs = asyncio.run(demonstrate("postgres", "docker", f"example-{uuid.uuid4().hex[:8]}"))
        finally:
            close_pools()
        print(f"\nrun ids (to remove the demo's rows, children included by parent): {' '.join(runs)}")
    failed = report(checks)
    if failed:
        raise SystemExit(f"failed: {failed}")
