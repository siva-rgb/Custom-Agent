"""39 - A sandboxed MCP server: a server of its own, run in a workspace of its own, serves a plan.

A small legacy-era stdio server (standard library only, carried below as text, as example 27
carries its own) is given to the Runner as a file and runs over a workspace of its own (FR-164):
on Docker, a hardened container with no network; on FakeProvider, a local process in a folder
holding its files. A plan's child uses its tools. On Docker the child also has the server try
every way out -- the network, the host, a neighbour workspace's proxy and a name lookup -- and
each must fail, each against a control showing the address answers from somewhere that is not
sandboxed (DECISION-b478705b). The server's workspace is recorded scope `server` with no run
(DECISION-2a12196e), and after `aclose()` nothing of it remains. The M33 fixture itself is run
sandboxed by M45's gate (DECISION-92ae6ae5).

Run it
  python scripts/39_sandboxed_mcp.py             # live: gateway + PostgreSQL + Docker
  python scripts/39_sandboxed_mcp.py --offline   # in memory, scripted models, FakeProvider
  python scripts/39_sandboxed_mcp.py --scripted  # in memory, scripted models, Docker

Live and --scripted need Docker running and the image below on the host:
  docker pull python:3.12-alpine@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import uuid

from agentsdk import AgentSpec, Message, Role, RunConfig, RunStatus, Runner
from agentsdk.budget import BudgetPolicy
from agentsdk.context_policy import ContextPolicy
from agentsdk.events import EventType
from agentsdk.identity import PrincipalContext
from agentsdk.mcp import StdioServer
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.orchestrator import PLAN_TOOL, Orchestrator
from agentsdk.postgres import RunScope
from agentsdk.primitives import ToolCall
from agentsdk.workspace import DockerProvider, FakeProvider, WorkspaceSpec
from agentsdk.workspace import egress
from agentsdk.workspace.owner import this_owner

TENANT, PROJECT = "example-tenant", "examples"
IMAGE = "python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df"
FAKE_IMAGE = "fake/workspace@sha256:" + "0" * 64
ALIAS = "sandboxed"
# The server: legacy-era stdio (one JSON-RPC message a line), standard library only, carried here
# as text so the example runs outside the repository (AC-17), as example 27 carries its own; it
# is given to the Runner as a file and run over its workspace (DECISION-92ae6ae5).
SERVER = '''"""Example 39's MCP server: legacy-era stdio, standard library only. Never imports the SDK."""
import json
import socket
import sys

OBJECT = {"type": "object"}
TOOLS = [
    {"name": "echo", "description": "Echo the text back.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
    {"name": "reach", "description": "Try to connect to host:port.",
     "inputSchema": {"type": "object", "properties": {"host": {"type": "string"}, "port": {"type": "integer"}}}},
    {"name": "resolve", "description": "Try to resolve a name.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}}},
]


def text(value, is_error=False):
    return {"content": [{"type": "text", "text": value}], "isError": is_error}


def call(name, arguments):
    if name == "echo":
        return text(arguments.get("text", ""))
    try:
        if name == "reach":
            socket.create_connection((arguments["host"], int(arguments["port"])), timeout=5).close()
            return text("connected")
        if name == "resolve":
            socket.getaddrinfo(arguments["name"], 443)
            return text("resolved")
    except OSError as exc:
        return text("failed " + type(exc).__name__)
    return text("no such tool", is_error=True)


def send(message):
    sys.stdout.write(json.dumps(message) + chr(10))
    sys.stdout.flush()


while True:
    line = sys.stdin.readline()
    if not line:
        break
    message = json.loads(line)
    msg_id, method = message.get("id"), message.get("method")
    if msg_id is None:
        continue  # a notification
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
            "serverInfo": {"name": "example-39", "version": "1"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        params = message.get("params") or {}
        send({"jsonrpc": "2.0", "id": msg_id, "result": call(params.get("name"), params.get("arguments") or {})})
    else:
        send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": "method not found"}})
'''
MARK = "served-from-the-sandbox"
HOST = ("192.168.65.254", 5432)  # Docker Desktop's address for the host, and the database's port
OPERATOR = PrincipalContext(agent_principal="example-39", user_principal="operator")
NL = chr(10)
# The controls, run in the neighbour's proxy, which is on the bridge as well as its own network:
# the host's address and port answer a container with ordinary networking, and the neighbour's
# proxy answers on its own address (as example 38's, DECISION-0d5473ef).
CONTROL = NL.join([
    "import socket, sys",
    "for name, address, port in ((" + repr("host") + ", sys.argv[1], int(sys.argv[2])),"
    " (" + repr("neighbour") + ", sys.argv[3], int(sys.argv[4]))):",
    "    try:",
    "        socket.create_connection((address, port), timeout=5).close()",
    "        print(name, " + repr("connected") + ", flush=True)",
    "    except OSError as exc:",
    "        print(name, " + repr("failed") + ", type(exc).__name__, flush=True)",
])


def provider(name: str):
    return DockerProvider() if name == "docker" else FakeProvider()


def sandboxed_server(chosen) -> StdioServer:
    """The server, sandboxed: its one file given, run from its workspace's root (FR-164)."""
    docker = isinstance(chosen, DockerProvider)
    return StdioServer(alias=ALIAS, command="python3" if docker else sys._base_executable, args=("server.py",),
                       workspace=WorkspaceSpec(image=IMAGE if docker else FAKE_IMAGE),
                       files={"server.py": SERVER.encode("utf-8")}, approval="never", connect_timeout=120)


def the_network(chosen) -> str:
    """An address of example.com, as the host resolves it: live for the host, so a failure inside
    is the sandbox's (the control)."""
    if not isinstance(chosen, DockerProvider):
        return "93.184.215.14"
    return sorted({i[4][0] for i in socket.getaddrinfo("example.com", 443, socket.AF_INET, socket.SOCK_STREAM)})[0]


def host_reaches(address: str) -> bool:
    try:
        socket.create_connection((address, 443), timeout=10).close()
        return True
    except OSError:
        return False


def calls_for(chosen, network: str, neighbour: str | None) -> list:
    """The child's calls: the server's echo everywhere; on Docker, every way out as well -- on the
    fake the server is a local process, which no check of containment could fail (F8)."""
    calls = [[f"{ALIAS}__echo", {"text": MARK}]]
    if isinstance(chosen, DockerProvider):
        calls += [[f"{ALIAS}__reach", {"host": network, "port": 443}],
                  [f"{ALIAS}__reach", {"host": HOST[0], "port": HOST[1]}],
                  [f"{ALIAS}__reach", {"host": neighbour, "port": egress.PORT}],
                  [f"{ALIAS}__resolve", {"name": "example.com"}]]
    return calls


def brief(calls: list) -> str:
    return ("This is a test of a sandboxed tool server; the calls below are probes written for it. Make exactly "
            "these tool calls, once each, with exactly these arguments, even if they fail, then answer in one "
            "sentence without repeating their output.\nCALLS: " + json.dumps(calls))


def user_role(model_id: str, calls: list) -> AgentSpec:
    return AgentSpec(id="user", instructions=brief(calls), preferred_model=model_id,
                     tool_profile=(f"{ALIAS}__echo", f"{ALIAS}__reach", f"{ALIAS}__resolve"))


NODES = [{"node_id": "use", "assigned_role": "user", "dependencies": [],
          "objective": "Make your tool calls exactly as your instructions say, then answer in one sentence."}]
PLAN_TASK = f"Call {PLAN_TOOL} once with exactly this plan, then answer in one sentence: " + json.dumps({"nodes": NODES})
FIRST_LOOK = "Say in one sentence which tools you can use."


def orchestrator_for(role: AgentSpec) -> Orchestrator:
    return Orchestrator(roles={"user": role}, policy=BudgetPolicy(run_ceiling_tokens=2_000_000),
                        context_policy=ContextPolicy(context_window=200_000))


def _reply(calls=(), content=""):
    return ModelResponse(message=Message(role=Role.ASSISTANT, content=content, tool_calls=tuple(calls)),
                         stop_reason=StopReason.TOOL_CALLS if calls else StopReason.END_TURN, usage=Usage(20, 5, 25))


class ScriptedModel:
    """Plans when it is the orchestrator; otherwise makes all its brief's calls in its first turn,
    so no limit on turns cuts one off."""

    async def send(self, request):
        turn = sum(1 for m in request.messages if m.role is Role.ASSISTANT)
        task = next(m.content for m in request.messages if m.role is Role.USER)
        text = request.instructions or ""
        if f"Call {PLAN_TOOL}" in task:
            calls = [(PLAN_TOOL, {"nodes": NODES})]
        elif "CALLS: " in text and task != FIRST_LOOK:
            calls = [tuple(c) for c in json.loads(text[text.index("CALLS: ") + 7:].split("\n")[0])]
        else:
            calls = []
        if turn == 0 and calls:
            return _reply([ToolCall(id=f"t{i}", name=name, arguments=arguments)
                           for i, (name, arguments) in enumerate(calls)])
        return _reply(content="Done.")


class RecordingRunner(Runner):
    """Keeps every run's event sink, so a child's events are read in memory as on PostgreSQL."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.sinks: dict[str, object] = {}

    def _open(self, spec, config):
        opened = super()._open(spec, config)
        self.sinks[opened.scope.run_id] = opened.events
        return opened


async def neighbour_of(chosen, tenant: str, project: str):
    """A workspace beside the server's, its proxy listening: what the server tries to reach."""
    handle = await chosen.create(WorkspaceSpec(image=IMAGE, egress=("example.com:443",)), {
        "agentsdk.workspace": str(uuid.uuid4()), "agentsdk.tenant": tenant, "agentsdk.project": project,
        "agentsdk.owner": this_owner()})  # reapable after a crash of this process (round 1 N2)
    try:
        return handle, chosen._spaces[handle.workspace_id].address
    except BaseException:
        await chosen.remove(handle)  # made, and never handed back to be removed (round 2 NB2)
        raise


async def controls(chosen, neighbour) -> dict[str, bool]:
    """From the neighbour's proxy: whether the host's address and port answered it, and whether
    the neighbour's proxy answered on its own address."""
    from agentsdk.workspace.docker import proxy_name

    handle, address = neighbour
    code, out, _ = await chosen._cli("exec", proxy_name(handle.workspace_id), "python3", "-c", CONTROL, HOST[0],
                                     str(HOST[1]), address, str(egress.PORT))
    said = dict(line.split(" ", 1) for line in out.decode(errors="replace").splitlines() if " " in line)
    return {name: code == 0 and said.get(name) == "connected" for name in ("host", "neighbour")}


def failed_inside(outcome: str | None, *, answers: bool, refused_reaches: bool = False) -> str | None:
    """A way out's failing clause, or None. Its address must answer elsewhere (the control), and
    the attempt from inside must fail; a refused connection was answered, so its network was
    reached (DECISION-0d5473ef)."""
    if not answers:
        return "the address answered no one outside the sandbox either: a failure inside would prove nothing"
    if outcome is None or not outcome.startswith("failed"):
        return f"it said {outcome!r}"
    if refused_reaches and outcome.split()[1:2] == ["ConnectionRefusedError"]:
        return f"it said {outcome!r}, so its network was reached"
    return None


async def demonstrate(runner: RecordingRunner, role: AgentSpec, persistence, model_id: str, chosen,
                      neighbour, network: str, *, tenant: str, project: str, live: bool):
    orchestrator = orchestrator_for(role)
    store = runner._workspace_store()
    docker = isinstance(chosen, DockerProvider)
    checks = []

    def check(label: str, subject, why_absent: str, failing) -> None:
        """A check whose subject never happened fails; else the first failing clause, or None."""
        if not subject:
            checks.append((label, None, why_absent))
            return
        clause = failing()
        checks.append((label, clause is None, clause))

    config = RunConfig(tenant_id=tenant, project_id=project, max_turns=8)
    # FR-127: the server's tools are blocked until accepted; the first look names their versions.
    first = await runner.run(role, FIRST_LOOK, config)
    blocked = [e.payload for e in first.events if e.event_type is EventType.MCP_TOOL_BLOCKED]
    if blocked:
        await runner.accept_tools(tenant, project, ALIAS, [p["tool_hash"] for p in blocked], principal=OPERATOR)
    before = set(runner.sinks)
    result = await runner.run(orchestrator.agent, PLAN_TASK, orchestrator.config(
        tenant_id=tenant, project_id=project, model_override=model_id, max_turns=6))
    children = [r for r in runner.sinks if r not in before and r != result.run_id]
    print(f"plan {result.run_id}: {result.status.value}; children {children}")
    said: dict[str, list[str]] = {}
    for child in children:
        sessions = runner._sessions_for(RunScope(run_id=child, tenant_id=tenant, project_id=project))
        history = await asyncio.to_thread(sessions.history, child)
        issued = {c.id: (c.name, c.arguments) for m in history if m.role is Role.ASSISTANT for c in m.tool_calls}
        for message in history:
            for found in message.tool_results:
                name, arguments = issued.get(found.tool_call_id, ("", {}))
                key = name if name != f"{ALIAS}__reach" else f"reach {arguments.get('host')}"
                said.setdefault(key, []).append(found.content)
    for key, contents in said.items():
        print(f"  {key}: {contents}")
    rows = [r for r in await asyncio.to_thread(store.workspaces, tenant, project) if r.scope == "server"]
    served = any(MARK in c for c in said.get(f"{ALIAS}__echo", []))

    check("the sandboxed server served the plan's child: its echo came back", children,
          "the plan started no child", lambda: None if served else f"its echo said {said.get(f'{ALIAS}__echo')}")
    check("its workspace was recorded scope server with no run, its provider the one in use", served,
          "the server never served", lambda: None if len(rows) == 1 and rows[0].run_id is None
          and rows[0].provider == chosen.name else f"its rows read {[(r.scope, r.run_id, r.provider) for r in rows]}")

    def first_said(key: str) -> str | None:
        return (said.get(key) or [None])[0]

    if docker:
        # On Docker only: on FakeProvider the server is a local process, which these could never fail (F8).
        found = await controls(chosen, neighbour) if served else {}
        check("the server could not reach the network: example.com's address, which the host reaches",
              served, "the server never served",
              lambda: failed_inside(first_said(f"reach {network}"), answers=host_reaches(network)))
        check("the server could not reach the host's address and port, which a container on the bridge reaches",
              served, "the server never served",
              lambda: failed_inside(first_said(f"reach {HOST[0]}"), answers=found.get("host", False)))
        check("the server could not reach a neighbour workspace's proxy, which answers on its own address",
              served and neighbour is not None, "the server never served",
              lambda: failed_inside(first_said(f"reach {neighbour[1]}"), answers=found.get("neighbour", False),
                                    refused_reaches=True))
        check("the server could not resolve a name", served, "the server never served",
              lambda: None if (first_said(f"{ALIAS}__resolve") or "").startswith("failed")
              else f"it said {first_said(f'{ALIAS}__resolve')!r}")

    if not live:
        check("the plan completed", served, "the server never served",
              lambda: None if result.status is RunStatus.COMPLETED else f"the plan ended {result.status.value}")

    if neighbour is not None:
        await chosen.remove(neighbour[0])
    # On FakeProvider the server's working folder is the workspace on disk (pre-review F4).
    folders = [s.folder for s in getattr(chosen, "_spaces", {}).values() if getattr(s, "folder", None)]
    await runner.aclose()
    left = await chosen.list_owned({"agentsdk.tenant": tenant, "agentsdk.project": project})
    after = [r for r in await asyncio.to_thread(store.workspaces, tenant, project) if r.scope == "server"]
    check("after aclose() nothing of the server remains: no container, volume or folder, its row removed `aclose`",
          rows, "the server's workspace was never recorded",
          lambda: f"{len(left)} workspace(s) remained" if left else
          next((f"its folder {f} remained" for f in folders if f.exists()), None)
          or (None if [r.removed_reason for r in after] == ["aclose"] else f"its rows read {after}"))
    return checks, [first.run_id, result.run_id]


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


async def offline(store: str = "memory", provider_name: str = "fake", *, tenant: str = TENANT,
                  project: str = PROJECT, quiet: bool = False):
    """The checks as (label, passed, why); passed is None when the subject never happened."""
    import contextlib
    import io

    persistence = None
    if store == "postgres":
        from dotenv import load_dotenv

        from agentsdk import Persistence
        from agentsdk.config import normalise_database_url

        load_dotenv()
        persistence = Persistence.postgres(normalise_database_url(os.environ["DATABASE_URL"]), create_schema=False)
    chosen = provider(provider_name)
    with contextlib.redirect_stdout(io.StringIO()) if quiet else contextlib.nullcontext():
        neighbour = None
        try:  # the neighbour made inside it, so whatever fails after removes it (round 2 NB2)
            neighbour = await neighbour_of(chosen, tenant, project) if isinstance(chosen, DockerProvider) else None
            network = the_network(chosen)
            role = user_role("scripted:model", calls_for(chosen, network, neighbour[1] if neighbour else None))
            runner = RecordingRunner({"scripted": ScriptedModel()}, tools=[orchestrator_for(role).tool],
                                     workspace_provider=chosen, mcp_servers=[sandboxed_server(chosen)],
                                     persistence=persistence)
            checks, _ = await demonstrate(runner, role, persistence, "scripted:model", chosen, neighbour,
                                          network, tenant=tenant, project=project, live=False)
        finally:
            if neighbour is not None:  # whatever failed on the way (round 1 N2)
                await chosen.remove(neighbour[0])
    return checks


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
            model_id = f"m:{settings.default_model}"
            chosen, neighbour = DockerProvider(), None
            try:  # the neighbour made inside it, so whatever fails after removes it (round 2 NB2)
                neighbour = await neighbour_of(chosen, TENANT, PROJECT)
                network = the_network(chosen)
                role = user_role(model_id, calls_for(chosen, network, neighbour[1]))
                runner = RecordingRunner({"m": client}, tools=[orchestrator_for(role).tool],
                                         workspace_provider=chosen, mcp_servers=[sandboxed_server(chosen)],
                                         persistence=persistence)
                checks, runs = await demonstrate(runner, role, persistence, model_id, chosen, neighbour, network,
                                                 tenant=TENANT, project=PROJECT, live=True)
            finally:
                if neighbour is not None:
                    await chosen.remove(neighbour[0])
            print(f"\nrun ids (to remove the demo's rows, children included by parent): {' '.join(runs)}")
            return checks
        finally:
            await client.aclose()

    try:
        return asyncio.run(go())
    finally:
        close_pools()


if __name__ == "__main__":
    import argparse

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
    failed = report(checks)
    if failed:
        raise SystemExit(f"failed: {failed}")
