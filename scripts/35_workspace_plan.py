"""35 - A plan in workspaces: children share a workspace, files leave only as artifacts.

An orchestrated plan runs three children. Two analysts share one plan workspace
(`WorkspaceSpec(scope="plan")`): the first writes data.csv, the second runs a program over it and
exports the summary it wrote. A writer, in a workspace of its own, writes and exports its notes.
`export_workspace_file` is the only way a file leaves a workspace (FR-150): each export is an
artifact of the run that made it, as untrusted as anything a command produced, naming the
workspace, the path and the last command that ran before it. When the plan's top-level run ends,
every workspace it used is removed (FR-151).

Then an archivist's run keeps its workspace (`keep=True`): it outlives the run until the operator
releases it with `Runner.release_workspace`. `Runner.aclose()` leaves nothing the Runner created.

Run it
  python scripts/35_workspace_plan.py             # live: gateway + PostgreSQL + Docker
  python scripts/35_workspace_plan.py --offline   # in memory, scripted models, FakeProvider
  python scripts/35_workspace_plan.py --scripted  # in memory, scripted models, Docker

Live and --scripted need Docker running and the image below on the host:
  docker pull python:3.12-alpine@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import os
import sys

from agentsdk import AgentSpec, Message, Role, RunConfig, RunStatus, Runner
from agentsdk.budget import BudgetPolicy
from agentsdk.context_policy import ContextPolicy
from agentsdk.events import EventType
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.orchestrator import PLAN_TOOL, Orchestrator
from agentsdk.postgres import RunScope
from agentsdk.primitives import InstructionAuthority, TaintFlag, ToolCall, TrustZone
from agentsdk.tools import ApprovalPolicy
from agentsdk.workspace import DockerProvider, FakeProvider, WorkspaceSpec

TENANT, PROJECT = "example-tenant", "examples"
IMAGE = "python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df"
DATA = "item,n\napples,3\npears,4\nplums,5\n"
PROGRAM = ("import csv\n"
           "total = sum(int(row['n']) for row in csv.DictReader(open('data.csv')))\n"
           "open('summary.txt', 'w').write(f'total {total}\\n')\n")
SUMMARY = "total 12\n"
NOTES = "# Notes\n\nThe fruit counts are summed in summary.txt.\n"
TOOLS = ("run_command", "read_workspace_file", "write_workspace_file", "list_workspace", "export_workspace_file")
ARGV = ["python", "-c", PROGRAM]
NODES = [
    {"node_id": "data", "assigned_role": "analyst", "dependencies": [],
     "objective": f"Write data.csv in your workspace with exactly this content:\n{DATA}Then answer in one sentence."},
    {"node_id": "stats", "assigned_role": "analyst", "dependencies": ["data"],
     "objective": f"In your workspace, run the command with argv {json.dumps(ARGV)}, which writes summary.txt; "
                  "then export summary.txt with mime_type text/plain. Then answer in one sentence."},
    {"node_id": "notes", "assigned_role": "writer", "dependencies": [],
     "objective": f"Write notes.md in your workspace with exactly this content:\n{NOTES}"
                  "then export notes.md with mime_type text/markdown. Then answer in one sentence."},
]
PLAN_TASK = f"Call {PLAN_TOOL} once with exactly this plan, then answer in one sentence: " + json.dumps({"nodes": NODES})
ARCHIVE_TASK = "Write archive.txt in your workspace containing the word kept, then answer in one sentence."


# --- FakeProvider's stand-in for the image's python (--offline) ------------------------------------


async def python(cmd) -> int:
    """The fake runs no model-written code (FR-139): it gives this example's program's effect
    only, and refuses any other."""
    if cmd.argv != ARGV:
        cmd.err("the fake runs only this example's program\n")
        return 1
    total = sum(int(row["n"]) for row in csv.DictReader(io.StringIO(cmd.files.read("data.csv").decode())))
    cmd.files.write("summary.txt", f"total {total}\n")
    return 0


def provider(name: str):
    return DockerProvider() if name == "docker" else FakeProvider(commands={"python": python})


def roles(model_id: str) -> dict[str, AgentSpec]:
    def role(agent_id: str, **space) -> AgentSpec:
        return AgentSpec(id=agent_id, instructions="Use your workspace tools exactly as asked.", preferred_model=model_id,
                         tool_profile=TOOLS,
                         workspace=WorkspaceSpec(image=IMAGE, run_command_approval=ApprovalPolicy.AUTO, **space))

    return {"analyst": role("analyst", scope="plan"), "writer": role("writer"), "archivist": role("archivist", keep=True)}


# --- offline: scripted models -----------------------------------------------------------------------


def _reply(calls=(), content=""):
    return ModelResponse(message=Message(role=Role.ASSISTANT, content=content, tool_calls=tuple(calls)),
                         stop_reason=StopReason.TOOL_CALLS if calls else StopReason.END_TURN, usage=Usage(20, 5, 25))


class ScriptedModel:
    """Plans when it is the orchestrator; otherwise does what its node's objective says, one call a turn."""

    def _calls(self, task: str):
        if task.startswith(f"Call {PLAN_TOOL}"):
            return [(PLAN_TOOL, {"nodes": NODES})]
        if "data.csv in your workspace" in task:
            return [("write_workspace_file", {"path": "data.csv", "content": DATA})]
        if "writes summary.txt" in task:
            return [("run_command", {"argv": ARGV}),
                    ("export_workspace_file", {"path": "summary.txt", "mime_type": "text/plain"})]
        if "notes.md" in task:
            return [("write_workspace_file", {"path": "notes.md", "content": NOTES}),
                    ("export_workspace_file", {"path": "notes.md", "mime_type": "text/markdown"})]
        if "archive.txt" in task:
            return [("write_workspace_file", {"path": "archive.txt", "content": "kept\n"})]
        return []

    async def send(self, request):
        task = next(m.content for m in request.messages if m.role is Role.USER)
        calls = self._calls(task[task.find("Call "):] if f"Call {PLAN_TOOL}" in task else task)
        turn = sum(1 for m in request.messages if m.role is Role.ASSISTANT)
        if turn < len(calls):
            name, arguments = calls[turn]
            return _reply([ToolCall(id=f"t{turn}", name=name, arguments=arguments)])
        return _reply(content="Done.")


# --- the demonstration ------------------------------------------------------------------------------


async def _exports(runner: Runner, tenant: str, project: str, run_ids) -> list:
    """Each export a run made, as (run id, its call's arguments, its artifact's metadata, content)."""
    found, store = [], runner._artifacts_for(tenant, project)
    for run_id in run_ids:
        sessions = runner._sessions_for(RunScope(run_id=run_id, tenant_id=tenant, project_id=project))
        history = await asyncio.to_thread(sessions.history, run_id)
        issued = {c.id: c for m in history if m.role is Role.ASSISTANT for c in m.tool_calls}
        for message in history:
            for result in message.tool_results:
                call = issued.get(result.tool_call_id)
                if call is None or call.name != "export_workspace_file" or result.is_error:
                    continue
                ref = await store.metadata(result.content.strip())
                found.append((run_id, call.arguments, ref, await store.get(ref.artifact_id)))
    return found


async def demonstrate(runner: Runner, orchestrator: Orchestrator, model_id: str, chosen, *, tenant: str, project: str,
                      live: bool):
    store = runner._workspace_store()

    # --- the plan -----------------------------------------------------------------------------------
    result = await runner.run(orchestrator.agent, PLAN_TASK, orchestrator.config(
        tenant_id=tenant, project_id=project, model_override=model_id, max_turns=6))
    print(f"plan {result.run_id}: {result.status.value}")
    spaces = [w for w in await asyncio.to_thread(store.workspaces, tenant, project)
              if w.run_id == result.run_id or w.scope == "run"]
    commands = [c for c in await asyncio.to_thread(store.commands, tenant, project)
                if c.workspace_id in {w.workspace_id for w in spaces}]
    for w in spaces:
        print(f"  workspace {w.workspace_id} scope={w.scope} run={w.run_id} removed={w.removed_reason}")
    plan_spaces = [w for w in spaces if w.scope == "plan"]
    own_spaces = [w for w in spaces if w.scope == "run"]
    children = {c.run_id for c in commands} | {w.run_id for w in own_spaces}
    exports = await _exports(runner, tenant, project, sorted(children))
    for run_id, arguments, ref, data in exports:
        print(f"  {run_id} exported {arguments['path']} as {ref.artifact_id} ({ref.mime_type}): {data!r}")
        print(f"    source: {ref.provenance.source_uri_or_hash}")
    by_path = {arguments["path"]: (run_id, ref, data) for run_id, arguments, ref, data in exports}
    stats_command = [c for c in commands if c.argv == ARGV]
    removed = [e.payload for e in result.events if e.event_type is EventType.WORKSPACE_REMOVED]
    kinds = [e.event_type for e in result.events]
    left = await chosen.list_owned({"agentsdk.tenant": tenant, "agentsdk.project": project})

    def provenance_holds(run_id, ref, path, command_id) -> bool:
        # A child's own workspace, or else its plan's.
        p, space = ref.provenance, ([w for w in own_spaces if w.run_id == run_id] or plan_spaces or [None])[0]
        source = f"workspace://{space.workspace_id if space else '?'}/{path}"
        return (ref.source_run == run_id and p.trust_zone is TrustZone.UNTRUSTED
                and p.instruction_authority is InstructionAuthority.DATA_ONLY
                and {TaintFlag.EXTERNAL_CONTENT, TaintFlag.EXECUTABLE_CONTENT} <= set(p.taint_flags)
                and p.source_uri_or_hash.startswith(source)
                and (p.source_uri_or_hash.endswith(f"?command={command_id}") if command_id
                     else "?command=" not in p.source_uri_or_hash))

    checks = [
        ("the analysts shared one plan workspace, recorded under the top-level run; the writer had its own",
         None if not spaces else (len(plan_spaces) == 1 and plan_spaces[0].run_id == result.run_id
                                  and len(own_spaces) == 1 and own_spaces[0].run_id != result.run_id)),
        ("the summary exported by one analyst totals the file the other analyst wrote",
         None if "summary.txt" not in by_path else by_path["summary.txt"][2].decode() == SUMMARY),
        ("every export is an artifact of its run: untrusted, data-only, naming its workspace, path and last command",
         None if not exports else (
             ("summary.txt" not in by_path or (stats_command and provenance_holds(
                 by_path["summary.txt"][0], by_path["summary.txt"][1], "summary.txt", stats_command[-1].command_id)))
             and ("notes.md" not in by_path or provenance_holds(
                 by_path["notes.md"][0], by_path["notes.md"][1], "notes.md", None)))),
        ("every workspace of the plan was removed when the plan ended, and none remains",
         None if not spaces else (all(w.removed_reason == "run_ended" for w in spaces) and left == []
                                  and len(removed) == 1 and removed[0]["workspace_id"] == plan_spaces[0].workspace_id
                                  and kinds.index(EventType.WORKSPACE_REMOVED) < len(kinds) - 1)),
    ]
    if not live:
        checks.append(("the plan completed, and both files were exported",
                       result.status is RunStatus.COMPLETED and set(by_path) == {"summary.txt", "notes.md"}))

    # --- a kept workspace ---------------------------------------------------------------------------
    kept = await runner.run(roles(model_id)["archivist"], ARCHIVE_TASK,
                            RunConfig(tenant_id=tenant, project_id=project, max_turns=4))
    [space] = [w for w in await asyncio.to_thread(store.workspaces, tenant, project, kept.run_id)] or [None]
    standing = await chosen.list_owned({"agentsdk.run": kept.run_id})
    print(f"archive {kept.run_id}: {kept.status.value}; its workspace stands: {[h.workspace_id for h in standing]}")
    released = space is not None and await runner.release_workspace(tenant, project, space.workspace_id)
    after = await asyncio.to_thread(store.workspaces, tenant, project, kept.run_id)
    checks += [
        ("a kept workspace outlived its run",
         None if space is None else (space.removed_reason is None and [h.workspace_id for h in standing]
                                     == [space.workspace_id])),
        ("released, it was removed and recorded released",
         None if space is None else (released is True and [w.removed_reason for w in after] == ["released"]
                                     and await chosen.list_owned({"agentsdk.run": kept.run_id}) == [])),
    ]

    # --- closing --------------------------------------------------------------------------------------
    await runner.aclose()
    checks.append(("after aclose() nothing the Runner created remains",
                   await chosen.list_owned({"agentsdk.tenant": tenant, "agentsdk.project": project}) == []))
    return checks, [result.run_id, kept.run_id]


def report(checks, live: bool) -> list[str]:
    failed = []
    for label, passed in checks:
        if passed is None:
            print(f"[{'SKIP' if live else 'FAIL'}] not exercised: {label}")
            if not live:
                failed.append(label)
            continue
        print(f"[{'PASS' if passed else 'FAIL'}] {label}")
        if not passed:
            failed.append(label)
    return failed


def docker_ready() -> str | None:
    """Why this example cannot use Docker here, or None."""
    import subprocess

    try:
        if subprocess.run(["docker", "version"], capture_output=True, timeout=30).returncode != 0:
            return "docker not running"
        if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, timeout=30).returncode != 0:
            return "the example image is not on this host (see the docstring to pull it)"
    except (OSError, subprocess.TimeoutExpired):
        return "docker not running"
    return None


def orchestrator_for(model_id: str) -> Orchestrator:
    team = roles(model_id)
    return Orchestrator(roles={"analyst": team["analyst"], "writer": team["writer"]},
                        policy=BudgetPolicy(run_ceiling_tokens=2_000_000),
                        context_policy=ContextPolicy(context_window=200_000))


async def offline(persistence=None, provider_name: str = "fake", *, tenant: str = TENANT, project: str = PROJECT):
    chosen = provider(provider_name)
    orchestrator = orchestrator_for("scripted:model")
    runner = Runner({"scripted": ScriptedModel()}, tools=[orchestrator.tool], workspace_provider=chosen,
                    persistence=persistence)
    checks, _ = await demonstrate(runner, orchestrator, "scripted:model", chosen, tenant=tenant, project=project,
                                  live=False)
    return [(label, passed is True) for label, passed in checks]


def live():
    from dotenv import load_dotenv

    from agentsdk import Persistence
    from agentsdk.config import Settings, normalise_database_url
    from agentsdk.postgres import close_pools
    from agentsdk.providers import OpenAICompatibleModelClient

    load_dotenv()
    persistence = Persistence.postgres(normalise_database_url(os.environ["DATABASE_URL"]))
    settings = Settings.from_env(load_dotfile=False)

    async def go():
        client = OpenAICompatibleModelClient(base_url=settings.base_url, api_key=settings.api_key,
                                             model=settings.default_model)
        try:
            docker = DockerProvider()
            model_id = f"m:{settings.default_model}"
            orchestrator = orchestrator_for(model_id)
            runner = Runner({"m": client}, tools=[orchestrator.tool], workspace_provider=docker,
                            persistence=persistence)
            checks, runs = await demonstrate(runner, orchestrator, model_id, docker, tenant=TENANT, project=PROJECT,
                                             live=True)
            print(f"\nrun ids (to remove the demo's rows, children included by parent): {' '.join(runs)}")
            return checks
        finally:
            await client.aclose()

    try:
        return asyncio.run(go())
    finally:
        close_pools()


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="in memory, scripted models, FakeProvider")
    mode.add_argument("--scripted", action="store_true", help="in memory, scripted models, Docker")
    args = parser.parse_args()
    if not args.offline and (why := docker_ready()) is not None:
        print(f"[SKIP] {why}")
        raise SystemExit(0)
    if args.offline or args.scripted:
        checks = asyncio.run(offline(provider_name="fake" if args.offline else "docker"))
    else:
        checks = live()
    failed = report(checks, live=not (args.offline or args.scripted))
    if failed:
        raise SystemExit(f"failed: {failed}")
