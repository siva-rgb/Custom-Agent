"""38 - Egress by allow list: one host reachable, every other way out refused and recorded.

A plan's child runs in a workspace allowed `example.com:443` (and `localtest.me:443`, a public name
that resolves to 127.0.0.1). Its workspace gets a network of its own and a proxy on it (FR-160):
HTTPS `CONNECT` to a listed pair only, every address the name resolves to global, the ClientHello's
SNI equal to the host (FR-161). The child fetches https://example.com through it, then tries every
other way out: an IP literal, an unlisted host, the host's database port, the listed name that
resolves to loopback, a ClientHello naming another host, plain HTTP, a second request on one
connection, a direct connection, the host directly, a name lookup, a neighbour workspace's proxy, and
every port it tries on its own network's gateway, which is isolated (DECISION-26009e04). The checks a
fake cannot fail -- the direct connection, the host, the name lookup, the neighbour and the gateway --
run on Docker only, each against a control: the address the proxy reached answers the host, and the
host's address and the neighbour's proxy answer the neighbour's proxy, on the bridge as well; a
refused connection to the neighbour counts as reaching it (DECISION-0d5473ef).

Every connection the proxy decides is a `workspace_connections` row and a `WorkspaceConnection`
event, from the proxy's own report, named to the command by the token in its HTTPS_PROXY -- a token
that is never recorded (FR-162). When the plan ends, its workspace's network and proxy go with it.

Run it
  python scripts/38_egress.py             # live: gateway + PostgreSQL + Docker (and example.com)
  python scripts/38_egress.py --offline   # in memory, scripted models, FakeProvider
  python scripts/38_egress.py --scripted  # in memory, scripted models, Docker (and example.com)

Live and --scripted need Docker running, the image below on the host, and the internet:
  docker pull python:3.12-alpine@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import socket
import sys
import uuid

from agentsdk import AgentSpec, Message, Role, RunStatus, Runner
from agentsdk.budget import BudgetPolicy
from agentsdk.context_policy import ContextPolicy
from agentsdk.events import EventType
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.orchestrator import PLAN_TOOL, Orchestrator
from agentsdk.postgres import RunScope
from agentsdk.primitives import ToolCall
from agentsdk.tools import ApprovalPolicy
from agentsdk.workspace import DockerProvider, FakeProvider, WorkspaceSpec
from agentsdk.workspace import egress

TENANT, PROJECT = "example-tenant", "examples"
IMAGE = "python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df"
ALLOW = ("example.com:443", "localtest.me:443")
NL = chr(10)
# Inside the workspace: every way out, each printed as `<attempt> <outcome>`.
PROBE = NL.join([
    "import os, socket, ssl, sys, urllib.parse, urllib.request",
    # Read in the container, where the SDK sets it: not a setting of this example's own.
    "proxy = urllib.parse.urlsplit(os.getenv('HTTPS_PROXY'))",
    "auth = 'Proxy-Authorization: Basic ' + __import__('base64').b64encode(",
    "    (proxy.username + ':' + proxy.password).encode()).decode()",
    "def raw(first, then=b''):",
    "    s = socket.create_connection((proxy.hostname, proxy.port), timeout=15)",
    "    s.sendall(first)",
    "    status = s.recv(4096).split(b'\\r\\n')[0].decode()",
    "    if then and status.startswith('HTTP/1.1 200'):",
    "        s.sendall(then)",
    "        try:",
    "            back = s.recv(4096)",
    "        except OSError:",
    "            back = b''",
    "        status += ' then ' + ('closed' if not back else 'answered')",
    "    s.close()",
    "    return status",
    "def connect(target):",
    "    return ('CONNECT ' + target + ' HTTP/1.1\\r\\nHost: ' + target + '\\r\\n' + auth + '\\r\\n\\r\\n').encode()",
    "def hello(name):",
    "    a, b = ssl.MemoryBIO(), ssl.MemoryBIO()",
    "    t = ssl.create_default_context().wrap_bio(a, b, server_hostname=name)",
    "    try:",
    "        t.do_handshake()",
    "    except ssl.SSLWantReadError:",
    "        pass",
    "    return b.read()",
    "def attempt(name, call):",
    "    try:",
    "        print(name, call(), flush=True)",
    "    except Exception as exc:",
    "        print(name, 'failed', type(exc).__name__, flush=True)",
    "attempt('allowed', lambda: len(urllib.request.urlopen('https://example.com/', timeout=20).read()))",
    "attempt('ip_literal', lambda: raw(connect('93.184.215.14:443')))",
    "attempt('not_listed', lambda: raw(connect('example.org:443')))",
    "attempt('host_port', lambda: raw(connect('host.docker.internal:5432')))",
    "attempt('not_global', lambda: raw(connect('localtest.me:443'), hello('localtest.me')))",
    "attempt('sni_mismatch', lambda: raw(connect('example.com:443'), hello('example.org')))",
    "attempt('not_connect', lambda: raw(b'GET http://example.com/ HTTP/1.1\\r\\nHost: example.com\\r\\n'",
    "                                   + auth.encode() + b'\\r\\n\\r\\n'))",
    "attempt('second_request', lambda: raw(connect('example.com:443'), connect('example.org:443')))",
    "def dial(address):",
    "    try:",
    "        socket.create_connection((address, 443), timeout=5).close()",
    "        return 'connected'",
    "    except OSError:",
    "        return 'failed'",
    "attempt('direct', lambda: ','.join(dial(a) for a in sys.argv[2].split(',')))",
    "attempt('host_direct', lambda: socket.create_connection(('192.168.65.254', 5432), timeout=5) and 'connected')",
    "attempt('dns', lambda: socket.getaddrinfo('example.com', 443))",
    "attempt('neighbour', lambda: socket.create_connection((sys.argv[1], 3128), timeout=5) and 'connected')",
    "def scan():",
    "    prefix = proxy.hostname.rsplit('.', 1)[0]",
    "    opened = []",
    "    for last in range(1, 9):",
    "        for port in (111, 22, 2375, 5432):",
    "            s = socket.socket()",
    "            s.settimeout(1)",
    "            try:",
    "                s.connect((prefix + '.' + str(last), port))",
    "                opened.append(prefix + '.' + str(last) + ':' + str(port))",
    "            except OSError:",
    "                pass",
    "            finally:",
    "                s.close()",
    "    return 'open ' + ','.join(opened) if opened else 'none_open'",
    "attempt('gateway', scan)",
])
REFUSALS = sorted(["ip_literal", "not_listed", "not_listed", "not_global", "sni_mismatch", "not_connect",
                   "second_request"])
FAILS_INSIDE = ("direct", "host_direct", "dns", "neighbour")
# N5's controls (DECISION-0d5473ef), run in the neighbour's proxy, which is on the bridge as well as
# its own network: the host's address and port answer a container with ordinary networking, and the
# neighbour's proxy answers on its own address -- so neither failure inside is a dead address's.
CONTROL = NL.join([
    "import socket, sys",
    "for name, address, port in (('host', '192.168.65.254', 5432), ('neighbour', sys.argv[1], int(sys.argv[2]))):",
    "    try:",
    "        socket.create_connection((address, port), timeout=5).close()",
    "        print(name, 'connected', flush=True)",
    "    except OSError as exc:",
    "        print(name, 'failed', type(exc).__name__, flush=True)",
])


# --- FakeProvider's stand-in for the probe (--offline) -----------------------------------------------

FAKE_RESOLVE = {"example.com": ["93.184.215.14"], "localtest.me": ["127.0.0.1"]}
FAKE_SITE = b"<!doctype html><title>Example Domain</title>"
CRLF = b"\r\n"


async def python(cmd) -> int:
    """The fake runs no model-written code (FR-139): it makes this example's probe's attempts through
    the fake's proxy -- the very `egress.Proxy` the container runs -- and refuses any other program."""
    if cmd.argv[:3] != ["python", "-c", PROBE] or len(cmd.argv) != 5:
        cmd.err("the fake runs only this example's probe\n")
        return 1
    auth = b"Proxy-Authorization: Basic " + base64.b64encode(f"{egress.USER}:{cmd.egress_token}".encode())

    def connect(target: str) -> bytes:
        return f"CONNECT {target} HTTP/1.1".encode() + CRLF + f"Host: {target}".encode() + CRLF + auth + CRLF + CRLF

    async def raw(first: bytes, then: bytes = b"") -> str:
        answer = await cmd.proxy(first + then)
        status, _, back = answer.partition(CRLF + CRLF)
        line = status.split(CRLF)[0].decode()
        return line + (" then " + ("closed" if not back else "answered") if then and line.startswith(
            "HTTP/1.1 200") else "")

    async def fetched() -> int:
        return len(await cmd.connect("example.com", 443))

    attempts = [
        ("allowed", fetched),
        ("ip_literal", lambda: raw(connect("93.184.215.14:443"))),
        ("not_listed", lambda: raw(connect("example.org:443"))),
        ("host_port", lambda: raw(connect("host.docker.internal:5432"))),
        ("not_global", lambda: raw(connect("localtest.me:443"), egress.client_hello("localtest.me"))),
        ("sni_mismatch", lambda: raw(connect("example.com:443"), egress.client_hello("example.org"))),
        ("not_connect", lambda: raw(b"GET http://example.com/ HTTP/1.1" + CRLF + b"Host: example.com" + CRLF
                                    + auth + CRLF + CRLF)),
        ("second_request", lambda: raw(connect("example.com:443"), connect("example.org:443"))),
        ("direct", lambda: cmd.connect_address(cmd.argv[4].split(",")[0], 443)),
        ("host_direct", lambda: cmd.connect_address("192.168.65.254", 5432)),
        ("dns", lambda: cmd.resolve_name("example.com")),
        ("neighbour", lambda: cmd.connect_address(cmd.argv[3], egress.PORT)),
        ("gateway", lambda: asyncio.sleep(0, result="none_open")),
    ]
    for name, call in attempts:
        try:
            cmd.out(f"{name} {await call()}\n")
        except OSError as exc:
            cmd.out(f"{name} failed {type(exc).__name__}\n")
    return 0


def provider(name: str):
    if name == "docker":
        return DockerProvider()
    return FakeProvider(commands={"python": python}, resolve=FAKE_RESOLVE, sites={("example.com", 443): FAKE_SITE})


def argv(neighbour: str, targets: str = "") -> list[str]:
    return ["python", "-c", PROBE, neighbour, targets]


def direct_targets(chosen) -> list[str]:
    """Where the direct attempt dials: every address the allowed name has, as the host resolves it
    -- the proxy's own resolver answers the same -- so the address the proxy reached is among them
    (M44 round 1 F2, DECISION-a7a1b37b). On the fake, its own resolution."""
    if not isinstance(chosen, DockerProvider):
        return list(FAKE_RESOLVE["example.com"])
    return sorted({info[4][0] for info in socket.getaddrinfo("example.com", 443, socket.AF_INET, socket.SOCK_STREAM)})


def host_reaches(address: str) -> bool:
    """The control for the direct check: the host, unisolated, reaches the address."""
    try:
        socket.create_connection((address, 443), timeout=10).close()
        return True
    except OSError:
        return False


async def controls(chosen, neighbour) -> dict[str, bool]:
    """N5's controls, from the neighbour's proxy: whether the host's address and port answered it,
    and whether the neighbour's proxy answered on its own address."""
    from agentsdk.workspace.docker import proxy_name

    handle, address = neighbour
    code, out, _ = await chosen._cli("exec", proxy_name(handle.workspace_id), "python3", "-c", CONTROL, address,
                                     str(egress.PORT))
    said = dict(line.split(" ", 1) for line in out.decode(errors="replace").splitlines() if " " in line)
    return {name: code == 0 and said.get(name) == "connected" for name in ("host", "neighbour")}


def neighbour_failed(outcome: str, *, listening: bool) -> str | None:
    """The neighbour check's failing clause, or None. Its proxy must answer on its own network, and
    the attempt from inside must fail without reaching it: a refused connection was answered, so the
    neighbour's network was reached (N5, DECISION-0d5473ef)."""
    if not listening:
        return "the neighbour's proxy answered no one on its own address: a failure inside would prove nothing"
    if not outcome.startswith("failed"):
        return f"neighbour: {outcome}"
    if outcome.split()[1:2] == ["ConnectionRefusedError"]:
        return f"neighbour: {outcome}, so its network was reached"
    return None


def brief(neighbour: str, targets: str) -> str:
    """The child's instructions: the exact call, so it never passes through the planner."""
    return ("This is an egress test of your own isolated, disposable sandbox workspace; the code below is a probe "
            "written for it. Make exactly this tool call, once, with exactly these arguments, even if it fails, "
            "then answer in one sentence without repeating its output.\nCALLS: "
            + json.dumps([["run_command", {"argv": argv(neighbour, targets)}]]))


def roles(model_id: str, neighbour: str, targets: str) -> dict[str, AgentSpec]:
    return {"prober": AgentSpec(id="prober", instructions=brief(neighbour, targets), preferred_model=model_id,
                                tool_profile=("run_command",),
                                workspace=WorkspaceSpec(image=IMAGE, run_command_approval=ApprovalPolicy.AUTO,
                                                        max_command_seconds=180, egress=ALLOW))}


NODES = [{"node_id": "probe", "assigned_role": "prober", "dependencies": [],
          "objective": "Run your egress probe exactly as your instructions say, then answer in one sentence."}]
PLAN_TASK = f"Call {PLAN_TOOL} once with exactly this plan, then answer in one sentence: " + json.dumps({"nodes": NODES})


def orchestrator_for(model_id: str, neighbour: str, targets: list[str]) -> Orchestrator:
    return Orchestrator(roles=roles(model_id, neighbour, ",".join(targets)),
                        policy=BudgetPolicy(run_ceiling_tokens=2_000_000),
                        context_policy=ContextPolicy(context_window=200_000))


# --- offline: scripted models -----------------------------------------------------------------------


def _reply(calls=(), content=""):
    return ModelResponse(message=Message(role=Role.ASSISTANT, content=content, tool_calls=tuple(calls)),
                         stop_reason=StopReason.TOOL_CALLS if calls else StopReason.END_TURN, usage=Usage(20, 5, 25))


class ScriptedModel:
    """Plans when it is the orchestrator; otherwise makes its brief's call."""

    async def send(self, request):
        turn = sum(1 for m in request.messages if m.role is Role.ASSISTANT)
        task = next(m.content for m in request.messages if m.role is Role.USER)
        text = request.instructions or ""
        if f"Call {PLAN_TOOL}" in task:
            calls = [(PLAN_TOOL, {"nodes": NODES})]
        elif "CALLS: " in text:
            calls = [tuple(c) for c in json.loads(text[text.index("CALLS: ") + 7:].split("\n")[0])]
        else:
            calls = []
        if turn < len(calls):
            name, arguments = calls[turn]
            return _reply([ToolCall(id=f"t{turn}", name=name, arguments=arguments)])
        return _reply(content="Done.")


class RecordingRunner(Runner):
    """Keeps every run's event sink, so a child's events are read in memory as on PostgreSQL (as
    examples 36 and 37 do)."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.sinks: dict[str, object] = {}

    def _open(self, spec, config):
        opened = super()._open(spec, config)
        self.sinks[opened.scope.run_id] = opened.events
        return opened


# --- the demonstration ------------------------------------------------------------------------------


async def events_of(runner: RecordingRunner, persistence, run_ids) -> list[tuple[str, str, dict]]:
    if persistence is None:
        return [(r, e.event_type.value, e.payload) for r in run_ids for e in runner.sinks[r].events()]
    import psycopg

    from agentsdk.config import normalise_database_url

    def read() -> list:
        with psycopg.connect(normalise_database_url(os.environ["DATABASE_URL"])) as conn:
            return [(str(r), t, p) for r, t, p in conn.execute(
                "SELECT run_id, event_type, payload FROM run_events WHERE run_id = ANY(%s::uuid[])"
                " ORDER BY run_id, sequence_no", (list(run_ids),)).fetchall()]

    return await asyncio.to_thread(read)


def stdout_of(content: str) -> str:
    """A command's stdout, from what the model read of it (workspace.tools.describe)."""
    if "--- stdout ---\n" not in content:
        return ""
    return content.split("--- stdout ---\n", 1)[1].split("\n--- stderr ---", 1)[0]


async def neighbour_of(chosen, tenant: str, project: str):
    """A workspace beside the plan's, allowed the same pairs: its proxy's address is what the probe
    tries to reach. On the fake there is no network, so an address stands in."""
    handle = await chosen.create(WorkspaceSpec(image=IMAGE, egress=ALLOW), {
        "agentsdk.workspace": str(uuid.uuid4()), "agentsdk.tenant": tenant, "agentsdk.project": project})
    address = chosen._spaces[handle.workspace_id].address if isinstance(chosen, DockerProvider) else "10.99.0.2"
    return handle, address


async def demonstrate(runner: RecordingRunner, orchestrator: Orchestrator, persistence, model_id: str, chosen,
                      neighbour, targets: list[str], *, tenant: str, project: str, live: bool):
    store = runner._workspace_store()
    checks = []

    def check(label: str, subject, why_absent: str, failing) -> None:
        """A check whose subject never happened fails; else the first failing clause, or None."""
        if not subject:
            checks.append((label, None, why_absent))
            return
        clause = failing()
        checks.append((label, clause is None, clause))

    before = set(runner.sinks)
    result = await runner.run(orchestrator.agent, PLAN_TASK, orchestrator.config(
        tenant_id=tenant, project_id=project, model_override=model_id, max_turns=6))
    children = [r for r in runner.sinks if r not in before and r != result.run_id]
    print(f"plan {result.run_id}: {result.status.value}; children {children}")
    commands = [c for c in await asyncio.to_thread(store.commands, tenant, project) if c.argv[:3] == argv("")[:3]]
    rows = await asyncio.to_thread(store.connections, tenant, project)
    events = [(r, kind, p) for r, kind, p in await events_of(runner, persistence, children)
              if kind == EventType.WORKSPACE_CONNECTION.value]
    out = {}
    for child in children:
        sessions = runner._sessions_for(RunScope(run_id=child, tenant_id=tenant, project_id=project))
        for message in await asyncio.to_thread(sessions.history, child):
            for found in message.tool_results:
                for line in stdout_of(found.content).splitlines():
                    name, _, outcome = line.partition(" ")
                    out.setdefault(name, outcome)
    probed = [c for c in commands if c.exit_code == 0] and "allowed" in out
    probe_ids = {c.command_id for c in commands}
    for row in rows:
        print(f"  {row.reason:15} {row.host}:{row.port} -> {row.address} command={row.command_id} "
              f"bytes={row.bytes_out}/{row.bytes_in}")
    for name, outcome in out.items():
        print(f"  {name}: {outcome}")

    allowed = [r for r in rows if r.allowed]
    check("the allowed pair was fetched through the proxy, recorded allowed with its address and bytes, by the "
          "command that made it", probed, "the probe never ran to its end",
          lambda: (None if out.get("allowed", "").isdigit() and int(out["allowed"]) > 0 else
                   f"the fetch said {out.get('allowed')}")
          or (None if len(allowed) == 1 and allowed[0].host == "example.com" and allowed[0].address
              and (allowed[0].bytes_in or 0) > 0 and allowed[0].command_id in probe_ids else
              f"its rows read {[(r.host, r.address, r.bytes_in, r.command_id) for r in allowed]}"))

    refused = sorted(r.reason for r in rows if not r.allowed)
    check("an IP literal, an unlisted host, the host's port, a name resolving to a private address, a ClientHello "
          "naming another host, plain HTTP and a second request were each refused and recorded by reason", probed,
          "the probe never ran to its end",
          lambda: (None if refused == REFUSALS else f"the refusals recorded were {refused}")
          or next((f"{r.reason} was recorded with command {r.command_id}" for r in rows
                   if r.command_id not in probe_ids), None))

    if isinstance(chosen, DockerProvider):
        # On Docker only: FakeProvider has no network, so there these can never fail (F8,
        # DECISION-26009e04).
        def direct() -> str | None:
            # F2: the very address the proxy reached, live for the host, failed from inside
            # (DECISION-a7a1b37b): so the failure is the isolation's, never a dead address's.
            reached = allowed[0].address if len(allowed) == 1 else None
            if not reached:
                return "no allowed connection recorded the address the proxy reached"
            if reached not in targets:
                return f"the proxy reached {reached}, which the probe did not dial ({','.join(targets)})"
            if not host_reaches(reached):
                return f"the host could not reach {reached} either: a failure inside would prove nothing"
            if set(out.get("direct", "").split(",")) != {"failed"}:
                return f"direct: {out.get('direct')}"
            return None

        found = await controls(chosen, neighbour) if probed and neighbour is not None else {}
        check("a direct connection to the address the proxy reached (which the host reaches), the host directly "
              "and a name lookup failed inside the workspace", probed, "the probe never ran to its end",
              lambda: direct()
              or (None if found.get("host") else "the host's address and port answered no container on an "
                  "ordinary network either: a failure inside would prove nothing")
              or next((f"{name}: {out.get(name)}" for name in FAILS_INSIDE[1:3]
                       if not out.get(name, "").startswith("failed")), None))
        check("a neighbour workspace's proxy was unreachable", probed and neighbour is not None,
              "the probe never ran to its end",
              lambda: neighbour_failed(out.get("neighbour", ""), listening=found.get("neighbour", False)))
        check("no host listener was open anywhere on the workspace's network, its gateway isolated",
              probed and "gateway" in out, "the probe never scanned its network",
              lambda: None if out.get("gateway") == "none_open" else f"the gateway: {out.get('gateway')}")

    check("each connection's row has its WorkspaceConnection event, and none names a token", probed and rows,
          "no connection was recorded",
          lambda: (None if sorted(p["connection_id"] for _, _, p in events) == sorted(r.connection_id for r in rows)
                   else f"{len(events)} event(s) for {len(rows)} row(s)")
          or next((f"an event names {key}" for _, _, p in events for key in p if "token" in key), None))

    if not live:
        check("the plan completed", probed, "the probe never ran to its end",
              lambda: None if result.status is RunStatus.COMPLETED else f"the plan ended {result.status.value}")

    await chosen.remove(neighbour[0])
    await runner.aclose()
    left = await chosen.list_owned({"agentsdk.tenant": tenant, "agentsdk.project": project})
    check("after the plan and aclose() no network, proxy or volume of the example remains", probed,
          "the probe never ran to its end", lambda: f"{len(left)} workspace(s) remained" if left else None)
    return checks, [result.run_id]


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
        neighbour = await neighbour_of(chosen, tenant, project)
        targets = direct_targets(chosen)
        orchestrator = orchestrator_for("scripted:model", neighbour[1], targets)
        runner = RecordingRunner({"scripted": ScriptedModel()}, tools=[orchestrator.tool], workspace_provider=chosen,
                                 persistence=persistence)
        checks, _ = await demonstrate(runner, orchestrator, persistence, "scripted:model", chosen, neighbour,
                                      targets, tenant=tenant, project=project, live=False)
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
            docker = DockerProvider()
            model_id = f"m:{settings.default_model}"
            neighbour = await neighbour_of(docker, TENANT, PROJECT)
            targets = direct_targets(docker)
            orchestrator = orchestrator_for(model_id, neighbour[1], targets)
            runner = RecordingRunner({"m": client}, tools=[orchestrator.tool], workspace_provider=docker,
                                     persistence=persistence)
            checks, runs = await demonstrate(runner, orchestrator, persistence, model_id, docker, neighbour,
                                             targets, tenant=TENANT, project=PROJECT, live=True)
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
    failed = report(checks)
    if failed:
        raise SystemExit(f"failed: {failed}")
