"""41 - Increment 2's exit signal: egress, a hostile sandboxed server, a snapshot and compute, at once.

Phase 5 increment 2's exit signal (P5-D24, FR-171). An orchestrated plan runs four children at
once, in a shuffled start order:

  egress    allowed one host, tries every other way out: an IP literal, an unlisted host, a name
            resolving privately, a name that rebinds between a private and a public address,
            another site behind the allowed one's address by SNI, plain HTTP, a second request,
            a direct connection, the host, a name lookup, a neighbour's network and its gateway
  server    uses a sandboxed MCP server -- this example's own, carried below -- that tries the
            network, the host's database port, a neighbour workspace and a name lookup; it is
            killed with its Runner
  snapshot  builds a workspace holding a link and a file past a later spec's bound; between its
            commands the operator snapshots it, and afterwards starts workspaces from the snapshot:
            in its own tenancy within bounds, in another tenancy, and under a bound below its size
  burn      burns CPU until its compute ceiling stops it

An operator approves every command (`run_command` needs approval), and takes the snapshot while
the snapshot child's second command waits for approval: between commands by construction
(DECISION-33856cec). Then `Runner.aclose()`.

Every check names its subject and has a witness, independent of the property, that the subject
happened (KNOWLEDGE-1afcf6b2); a subject that never happened is a failure, in every mode
(DECISION-a7d6a1f5). Checks read the store, the provider and the commands' and the server's own
output -- never an error's text (DECISION-94bfee7c). What a fake cannot fail -- a direct
connection, the host, a name lookup, a neighbour, the gateway, and the server's containment --
is checked on Docker only, each against a control showing the address answers from outside the
sandbox. The rebinding name is a public one on Docker, which the checks see rebind from a
container, and one the fake's resolver rotates (DECISION-18bd3f65).

Run it
  python scripts/41_increment2_contention.py             # live: gateway + PostgreSQL + Docker
  python scripts/41_increment2_contention.py --offline   # in memory, scripted children, FakeProvider
  python scripts/41_increment2_contention.py --scripted  # in memory, scripted children, Docker

Live and --scripted need Docker running, the image below on the host, and the internet (example.com
and rbndr.us):
  docker pull python:3.12-alpine@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import base64
import contextlib
import ipaddress
import json
import os
import random
import socket
import subprocess
import sys
import uuid
from decimal import Decimal

from agentsdk import AgentSpec, Message, Role, RunConfig, Runner
from agentsdk.budget import BudgetPolicy
from agentsdk.context_policy import ContextPolicy
from agentsdk.events import EventType
from agentsdk.identity import PrincipalContext
from agentsdk.mcp import StdioServer
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.orchestrator import PLAN_TOOL, Orchestrator
from agentsdk.postgres import RunScope
from agentsdk.primitives import ToolCall
from agentsdk.registry import ModelCapabilities, ModelEntry, ModelPricing, ModelRegistry
from agentsdk.scheduler import SchedulerLimits
from agentsdk.workspace import DockerProvider, FakeProvider, WorkspaceSpec
from agentsdk.workspace import egress
from agentsdk.workspace.owner import this_owner

TENANT = "example-tenant"
ELSEWHERE = "-elsewhere"  # another tenancy: the tenant with this added
IMAGE = "python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df"
FAKE_IMAGE = "fake/workspace@sha256:" + "0" * 64
MiB = 2**20
NL = chr(10)
OPERATOR = PrincipalContext(agent_principal="example-41", user_principal="operator")
HOST = ("192.168.65.254", 5432)  # Docker Desktop's address for the host, and the database's port

# --- egress -------------------------------------------------------------------------------------------

# A public name (rbndr.us) answering 127.0.0.1 or 1.1.1.1 at random, with no caching.
REBIND = "7f000001.01010101.rbndr.us"
REBIND_TRIES = 12  # a second apart: a resolver may hold an answer for one (M47 Docker run)
REBIND_LOOKUPS = 60  # at most, from a container on the bridge, until it gives two answers: an upstream
# resolver held one answer through 12 lookups (M47 live run)
# The fake's rotation: loopback, then example.com's fake address.
REBIND_ANSWERS = (["127.0.0.1"], ["93.184.215.14"])
ALLOW = ("example.com:443", "localtest.me:443", f"{REBIND}:443")
# Inside the workspace: every way out, each printed as `<attempt> <outcome>` (as example 38's).
EGRESS = NL.join([
    "import os, socket, ssl, sys, time, urllib.parse, urllib.request",
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
    f"for _ in range({REBIND_TRIES}):",
    f"    attempt('rebind', lambda: raw(connect('{REBIND}:443'), hello('{REBIND}')))",
    "    time.sleep(1.1)",
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
# The controls, run in the neighbour's proxy, which is on the bridge as well as its own network:
# the host's address and port answer a container with ordinary networking, the neighbour's proxy
# answers on its own address, and the rebinding name rebinds (DECISION-0d5473ef, DECISION-18bd3f65).
CONTROL = NL.join([
    "import socket, sys, time",
    "for name, address, port in (('host', sys.argv[1], int(sys.argv[2])), ('neighbour', sys.argv[3], int(sys.argv[4]))):",
    "    try:",
    "        socket.create_connection((address, port), timeout=5).close()",
    "        print(name, 'connected', flush=True)",
    "    except OSError as exc:",
    "        print(name, 'failed', type(exc).__name__, flush=True)",
    "seen = set()",
    "for _ in range(int(sys.argv[6])):",
    "    try:",
    "        seen.update(i[4][0] for i in socket.getaddrinfo(sys.argv[5], 443, socket.AF_INET, socket.SOCK_STREAM))",
    "    except OSError:",
    "        pass",
    "    if len(seen) > 1:",
    "        break",
    "    time.sleep(1.1)",
    "print('rebinds', ','.join(sorted(seen)) or 'none', flush=True)",
])

# --- the snapshot child, the burner -------------------------------------------------------------------

BIG = 3 * MiB  # a file past BELOW, the bound of a later spec
BELOW = 1 * MiB
BUILD = NL.join([
    "import os",
    "open('a.txt', 'w').write('one')",
    "os.makedirs('dir', exist_ok=True)",
    "open('dir/b.txt', 'w').write('two')",
    "os.symlink('a.txt', 'l')",
    f"open('big.bin', 'wb').write(os.urandom({BIG}))",
    "print('built', sorted(os.listdir('.')))",
    "print('nonce', os.urandom(4).hex())",
])
# The second command passes on the nonce the build printed, so no model can ask for both in one
# response: a batch's approvals are all settled before any of its commands runs, so there would be
# no moment between them (M47 live run).
# It changes the workspace, so a restore that reads the state at the snapshot is no copy of the live
# tree (M47 pre-review F1).
NONCE = "NONCE"
AFTER = NL.join(["import sys", "open('a.txt', 'w').write('changed')", "open('later.txt', 'w').write('later')",
                 "print('after', sys.argv[1], open('a.txt').read())"])
PRICE = Decimal("0.05")  # a CPU-second
BURN = 1.0  # CPU-seconds each command burns
SLACK = 1.0  # what the interpreter's start may add to a reading on Docker
RESERVED = "0.12"  # the burner's ceiling: its third burn crosses it
BURN_SRC = NL.join(["import time", "t = time.process_time()", f"while time.process_time() - t < {BURN}: pass",
                    "print('burnt')"])
ARGV = {"egress": ["python", "-c", EGRESS, "neighbour", "targets"], "build": ["python", "-c", BUILD],
        "after": ["python", "-c", AFTER, NONCE], "burn": ["python", "-c", BURN_SRC]}


def runs_probe(argv, name: str) -> bool:
    """Whether `argv` runs probe `name`: `python -c` with the same program, however it is spaced (a
    live model drops spaces after commas, M42 live run), and as many arguments as its own."""
    probe = ARGV[name]
    if (not isinstance(argv, list) or len(argv) != len(probe) or argv[:2] != probe[:2]
            or not all(isinstance(a, str) for a in argv[2:])):
        return False
    try:
        return ast.dump(ast.parse(argv[2])) == ast.dump(ast.parse(probe[2]))
    except Exception:  # noqa: BLE001 - a program that cannot be read is not the probe (M42 pre-review F1)
        return False


# --- the hostile server -------------------------------------------------------------------------------

ALIAS = "hostile"
MARK = "served-from-the-sandbox"
# Legacy-era stdio (one JSON-RPC message a line), standard library only, carried here as text so the
# example runs outside the repository (AC-17); its own, not example 39's (DECISION-33856cec).
SERVER = '''"""Example 41's hostile MCP server: legacy-era stdio, standard library only. Never imports the SDK."""
import json
import socket
import sys

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
            "serverInfo": {"name": "example-41", "version": "1"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        params = message.get("params") or {}
        send({"jsonrpc": "2.0", "id": msg_id, "result": call(params.get("name"), params.get("arguments") or {})})
    else:
        send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": "method not found"}})
'''
SERVER_TOOLS = (f"{ALIAS}__echo", f"{ALIAS}__reach", f"{ALIAS}__resolve")
FIRST_LOOK = "Say in one sentence which tools you can use."


def sandboxed_server(chosen) -> StdioServer:
    """The server, sandboxed: its one file given, run from its workspace's root (FR-164)."""
    docker = isinstance(chosen, DockerProvider)
    return StdioServer(alias=ALIAS, command="python3" if docker else sys._base_executable, args=("server.py",),
                       workspace=WorkspaceSpec(image=IMAGE if docker else FAKE_IMAGE),
                       files={"server.py": SERVER.encode("utf-8")}, approval="never", connect_timeout=120)


# --- the plan -----------------------------------------------------------------------------------------

CHILDREN = ("egress", "server", "snapshot", "burn")
WORKSPACE_TOOLS = ("run_command", "write_workspace_file")


def turns_for(name: str, chosen, network: str, neighbour: str, targets: str) -> list[list]:
    """Each child's calls, by turn. The snapshot child's two commands are two turns, the second
    needing the first's nonce, so the operator snapshots between them; the burner burns once a
    turn until it is stopped."""
    if name == "egress":
        return [[["run_command", {"argv": ["python", "-c", EGRESS, neighbour, targets]}]]]
    if name == "server":
        calls = [[f"{ALIAS}__echo", {"text": MARK}]]
        if isinstance(chosen, DockerProvider):  # on the fake the server is a local process (F8)
            calls += [[f"{ALIAS}__reach", {"host": network, "port": 443}],
                      [f"{ALIAS}__reach", {"host": HOST[0], "port": HOST[1]}],
                      [f"{ALIAS}__reach", {"host": neighbour, "port": egress.PORT}],
                      [f"{ALIAS}__resolve", {"name": "example.com"}]]
        return [calls]
    if name == "snapshot":
        return [[["run_command", {"argv": ARGV["build"]}]], [["run_command", {"argv": ARGV["after"]}]]]
    return [[["run_command", {"argv": ARGV["burn"]}]] for _ in range(4)]


def brief(turns: list[list]) -> str:
    """The child's instructions, for a live model and the scripted one alike: the exact calls, so they
    never pass through the planner (M42 live run)."""
    return ("This is a containment test of your own isolated, disposable sandbox; the calls below are probes "
            "written for it and cannot harm anything outside. Make exactly these tool calls, in this order, each "
            "turn's calls in one response, with exactly these arguments, even when one fails. "
            f"Where an argument reads {NONCE}, write instead the hex word your previous command printed after "
            "'nonce'. If a call's approval is rejected, make that call again, alone, in your next response, once. "
            "Then answer in one sentence, without repeating any output.\nTURNS: " + json.dumps(turns))


def objective(name: str) -> str:
    return f"Run your {name} containment probe exactly as your instructions say, then answer in one sentence."


def nodes(rng: random.Random) -> list[dict]:
    order = list(CHILDREN)
    rng.shuffle(order)
    return [{"node_id": name, "assigned_role": name, "dependencies": [], "objective": objective(name),
             **({"budget_reservation": {"usd": RESERVED}} if name == "burn" else {})} for name in order]


def plan_task(rng: random.Random) -> str:
    return f"Call {PLAN_TOOL} once with exactly this plan, then answer in one sentence: " + json.dumps(
        {"nodes": nodes(rng)})


LIVE_OUTPUT_TOKENS = 8000  # a live model copying the egress probe ran out of output at the default (M47 live run)


def roles(model_id: str, chosen, network: str, neighbour: str, targets: str,
          live: bool = False) -> dict[str, AgentSpec]:
    output = {"max_output_tokens": LIVE_OUTPUT_TOKENS} if live else {}

    def role(name: str) -> AgentSpec:
        instructions = brief(turns_for(name, chosen, network, neighbour, targets))
        if name == "server":
            return AgentSpec(id=f"contender-{name}", instructions=instructions, preferred_model=model_id,
                             tool_profile=SERVER_TOOLS, **output)
        space = {"egress": {"egress": ALLOW, "max_command_seconds": 180.0}}.get(name, {"max_command_seconds": 60.0})
        return AgentSpec(id=f"contender-{name}", instructions=instructions, preferred_model=model_id,
                         tool_profile=WORKSPACE_TOOLS, workspace=WorkspaceSpec(image=IMAGE, **space), **output)

    return {name: role(name) for name in CHILDREN}


def orchestrator_for(model_id: str, chosen, network: str, neighbour: str, targets: str,
                     live: bool = False) -> Orchestrator:
    return Orchestrator(roles=roles(model_id, chosen, network, neighbour, targets, live),
                        policy=BudgetPolicy(run_ceiling_usd=Decimal("10.00"), cpu_second_usd=PRICE),
                        limits=SchedulerLimits(max_concurrent_subagents=len(CHILDREN) + 1),
                        context_policy=ContextPolicy(context_window=200_000))


# --- FakeProvider's stand-in for the image's python (--offline) ---------------------------------------

FAKE_SITE = b"<!doctype html><title>Example Domain</title>"
CRLF = b"\r\n"


class Rotor:
    """The fake's rebinding name: each resolution gives the next of its answers, every one kept."""

    def __init__(self, answers) -> None:
        self.answers, self.given = tuple(answers), []

    def __call__(self) -> list[str]:
        found = list(self.answers[len(self.given) % len(self.answers)])
        self.given.append(found)
        return found


async def _egress(cmd) -> None:
    """The egress probe's attempts through the fake's proxy -- the very `egress.Proxy` the container
    runs -- as example 38's fake makes them, the rebinding name's among them."""
    auth = b"Proxy-Authorization: Basic " + base64.b64encode(f"{egress.USER}:{cmd.egress_token}".encode())

    def connect(target: str) -> bytes:
        return f"CONNECT {target} HTTP/1.1".encode() + CRLF + f"Host: {target}".encode() + CRLF + auth + CRLF + CRLF

    async def raw(first: bytes, then: bytes = b"") -> str:
        status, _, back = (await cmd.proxy(first + then)).partition(CRLF + CRLF)
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
        *[("rebind", lambda: raw(connect(f"{REBIND}:443"), egress.client_hello(REBIND)))] * REBIND_TRIES,
        ("direct", lambda: cmd.connect_address(cmd.argv[4].split(",")[0], 443)),
        ("host_direct", lambda: cmd.connect_address(HOST[0], HOST[1])),
        ("dns", lambda: cmd.resolve_name("example.com")),
        ("neighbour", lambda: cmd.connect_address(cmd.argv[3], egress.PORT)),
        ("gateway", lambda: asyncio.sleep(0, result="none_open")),
    ]
    for name, call in attempts:
        try:
            cmd.out(f"{name} {await call()}\n")
        except OSError as exc:
            cmd.out(f"{name} failed {type(exc).__name__}\n")


async def python(cmd) -> int:
    """The fake runs no model-written code (FR-139): it gives each probe's effect, and refuses any
    other program."""
    probe = next((name for name in ARGV if runs_probe(cmd.argv, name)), None)
    if probe == "egress":
        await _egress(cmd)
    elif probe == "build":
        cmd.files.write("a.txt", b"one")
        cmd.files.write("dir/b.txt", b"two")
        cmd.files.link("l", "a.txt")
        cmd.files.write("big.bin", b"x" * BIG)
        cmd.out(f"built\nnonce {os.urandom(4).hex()}\n")
    elif probe == "after":
        cmd.files.write("a.txt", b"changed")
        cmd.files.write("later.txt", b"later")
        cmd.out(f"after {cmd.argv[3]} {cmd.files.read('a.txt').decode()}\n")
    elif probe == "burn":
        cmd.cpu(BURN)
        cmd.out("burnt\n")
    else:
        cmd.err("the fake runs only this example's probes\n")
        return 1
    return 0


def provider(name: str):
    if name == "docker":
        return DockerProvider()
    return FakeProvider(commands={"python": python}, sites={("example.com", 443): FAKE_SITE},
                        resolve={"example.com": ["93.184.215.14"], "localtest.me": ["127.0.0.1"],
                                 REBIND: Rotor(REBIND_ANSWERS)})


# --- scripted models ----------------------------------------------------------------------------------


def _reply(calls=(), content=""):
    return ModelResponse(message=Message(role=Role.ASSISTANT, content=content, tool_calls=tuple(calls)),
                         stop_reason=StopReason.TOOL_CALLS if calls else StopReason.END_TURN, usage=Usage(20, 5, 25))


class ScriptedPlanner:
    async def send(self, request):
        if any(m.role is Role.TOOL for m in request.messages):
            return _reply(content="The plan ran.")
        task = next(m.content for m in request.messages if m.role is Role.USER)
        return _reply([ToolCall(id="plan", name=PLAN_TOOL, arguments=json.loads(task[task.index("{"):]))])


def _tool_names(request) -> set[str]:
    return {t.get("name") or (t.get("function") or {}).get("name") for t in request.tools}


class ScriptedChild:
    """Makes its brief's calls, a turn at a time, after a random delay. With `quote`, each turn also
    writes the last result it read, verbatim, into a later call's arguments -- a file of its
    workspace, or the server's echo: a model quoting earlier results (DECISION-a00773b0)."""

    def __init__(self, rng: random.Random, delay: float, quote: bool = False) -> None:
        self.rng, self.delay, self.quote = rng, delay, quote

    async def send(self, request):
        await asyncio.sleep(self.rng.random() * self.delay)
        text = request.instructions or ""
        task = next(m.content for m in request.messages if m.role is Role.USER)
        if task == FIRST_LOOK or "TURNS: " not in text:
            return _reply(content="Done.")
        turns = json.loads(text[text.index("TURNS: ") + 7:].split("\n")[0])
        turn = sum(1 for m in request.messages if m.role is Role.ASSISTANT)
        read = [r for m in request.messages for r in m.tool_results if r.tool_call_id.startswith("t")]
        nonce = next((line.split()[1] for r in reversed(read) for line in stdout_of(r.content).splitlines()
                      if line.startswith("nonce ") and len(line.split()) == 2), NONCE)
        made = [ToolCall(id=f"t{turn}-{i}", name=name, arguments=json.loads(json.dumps(arguments).replace(
                    json.dumps(NONCE), json.dumps(nonce))))
                for i, (name, arguments) in enumerate(turns[turn])] if turn < len(turns) else []
        if self.quote and read and (made or turn + 2 <= 4):
            if f"{ALIAS}__echo" in _tool_names(request):
                made.append(ToolCall(id=f"q{turn}", name=f"{ALIAS}__echo", arguments={"text": read[-1].content}))
            else:
                made.append(ToolCall(id=f"q{turn}", name="write_workspace_file",
                                     arguments={"path": f"quoted-{turn}.txt", "content": read[-1].content}))
        return _reply(made) if made else _reply(content="Done.")


READ_BACK = [[["read_workspace_file", {"path": "a.txt"}], ["read_workspace_file", {"path": "dir/b.txt"}],
              ["list_workspace", {"path": "."}]]]


class ScriptedReader:
    """The operator's own reads of a restored workspace, the same in every mode: its task is the
    calls, as JSON."""

    async def send(self, request):
        turns = json.loads(request.messages[0].content)
        turn = sum(1 for m in request.messages if m.role is Role.ASSISTANT)
        if turn < len(turns):
            return _reply([ToolCall(id=f"t{turn}-{i}", name=name, arguments=arguments)
                           for i, (name, arguments) in enumerate(turns[turn])])
        return _reply(content="done")


# Models this cheap leave compute to decide when the burner stops.
SCRIPTED_PRICING = ModelPricing(input=Decimal("0.0000001"), output=Decimal("0.0000001"))


def scripted_registry() -> ModelRegistry:
    return ModelRegistry([ModelEntry(provider=provider_name, model_id=model, model_version="1",
                                     adapter_version="scripted/1",
                                     capabilities=ModelCapabilities(max_context_tokens=200_000,
                                                                    pricing=SCRIPTED_PRICING))
                          for provider_name, model in (("planner", "p"), ("child", "m"), ("reader", "r"))])


class RecordingRunner(Runner):
    """Keeps every run's event sink, so a child's events are read in memory as on PostgreSQL (as
    examples 36 to 39 do)."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.sinks: dict[str, object] = {}

    def _open(self, spec, config):
        opened = super()._open(spec, config)
        self.sinks[opened.scope.run_id] = opened.events
        return opened


# --- the operator -------------------------------------------------------------------------------------


async def operator(runner: Runner, chosen, tenant: str, project: str, top: str, rng: random.Random,
                   seen: dict) -> None:
    """Approves every command of the plan in a shuffled order. While the snapshot child's second
    command waits for approval -- between its commands -- it snapshots that child's workspace. A
    model may ask for both in one response (M47 live run), and a response's approvals are all
    settled before any of its commands runs: so a second command asked before the first has run is
    refused, and the child, as its brief says, asks for it again alone."""
    decided: set[str] = set()
    store = runner._workspace_store()
    while True:
        views = [v for v in await runner.interruptions(tenant, project, run_id=top)
                 if v.interruption.interruption_id not in decided]
        rng.shuffle(views)
        for view in views:
            argv = (view.interruption.request_payload.get("arguments") or {}).get("argv")
            decided.add(view.interruption.interruption_id)
            if runs_probe(argv, "after") and seen["snapshot"] is None and seen["snapshot_error"] is None:
                # A command's row is written as it ends: with no build's yet, this was asked with it.
                ran = await asyncio.to_thread(store.commands, tenant, project, view.interruption.run_id)
                if not any(runs_probe(c.argv, "build") for c in ran):
                    seen["refused"] += 1
                    try:
                        await runner.decide(tenant, project, view.interruption.interruption_id, approve=False,
                                            principal=OPERATOR)
                    except Exception:  # noqa: BLE001 - judged by the checks
                        pass
                    continue
            if runs_probe(argv, "after") and seen["snapshot"] is None and seen["snapshot_error"] is None:
                try:
                    [held] = await chosen.list_owned({"agentsdk.run": view.interruption.run_id})
                    seen["snapshot"] = await runner.snapshot_workspace(tenant, project, held.workspace_id)
                    seen["snapshotted"] = held.workspace_id
                except Exception as exc:  # noqa: BLE001 - judged by the checks
                    seen["snapshot_error"] = type(exc).__name__
            try:
                await runner.decide(tenant, project, view.interruption.interruption_id, approve=True,
                                    principal=OPERATOR)
            except Exception:  # noqa: BLE001 - judged by the checks
                continue
        await asyncio.sleep(0.05)


# --- reading what happened ----------------------------------------------------------------------------


async def events_of(runner: RecordingRunner, persistence, run_ids) -> dict[str, list[tuple[str, dict]]]:
    """Each run's (event type, payload) in order: from PostgreSQL, or in memory from the sinks."""
    if persistence is None:
        return {r: [(e.event_type.value, e.payload) for e in runner.sinks[r].events()] for r in run_ids}
    import psycopg

    from agentsdk.config import normalise_database_url

    def read() -> dict:
        found = {str(r): [] for r in run_ids}
        with psycopg.connect(normalise_database_url(os.environ["DATABASE_URL"])) as conn:
            for run_id, kind, payload in conn.execute(
                    "SELECT run_id, event_type, payload FROM run_events WHERE run_id = ANY(%s::uuid[])"
                    " ORDER BY run_id, sequence_no", (list(run_ids),)).fetchall():
                found[str(run_id)].append((kind, payload))
        return found

    return await asyncio.to_thread(read)


def stdout_of(content: str) -> str:
    """A command's stdout, from what the model read of it (workspace.tools.describe)."""
    if "--- stdout ---\n" not in content:
        return ""
    return content.split("--- stdout ---\n", 1)[1].split("\n--- stderr ---", 1)[0]


def made(result) -> list[str]:
    return [e.payload["workspace_id"] for e in result.events if e.event_type is EventType.WORKSPACE_CREATED]


def _docker(*args: str) -> list[str] | None:
    try:
        done = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.split() if done.returncode == 0 else None


async def remains(chosen, tenant: str) -> list[str] | None:
    """Every container, volume and network labelled with the tenant, on Docker; on the fake, its
    listing. None when the listing failed."""
    if not isinstance(chosen, DockerProvider):
        return [h.workspace_id for h in await chosen.list_owned({"agentsdk.tenant": tenant})]
    found = []
    for listing in (["ps", "-aq"], ["volume", "ls", "-q"], ["network", "ls", "-q"]):
        names = await asyncio.to_thread(_docker, *listing, "--filter", f"label=agentsdk.tenant={tenant}")
        if names is None:
            return None
        found += names
    return found


def direct_targets(chosen) -> list[str]:
    """Where the direct attempt dials: every address the allowed name has, as the host resolves it,
    so the address the proxy reached is among them (as example 38's). On the fake, its own."""
    if not isinstance(chosen, DockerProvider):
        return ["93.184.215.14"]
    return sorted({info[4][0] for info in socket.getaddrinfo("example.com", 443, socket.AF_INET, socket.SOCK_STREAM)})


def host_reaches(address: str) -> bool:
    """The control for a check of the network: the host, unisolated, reaches the address."""
    try:
        socket.create_connection((address, 443), timeout=10).close()
        return True
    except OSError:
        return False


async def neighbour_of(chosen, tenant: str, project: str):
    """A workspace beside the plan's, its proxy listening: what the egress child and the server try
    to reach. On the fake there is no network, so an address stands in."""
    if not isinstance(chosen, DockerProvider):
        return None, "10.99.0.2"
    handle = await chosen.create(WorkspaceSpec(image=IMAGE, egress=("example.com:443",)), {
        "agentsdk.workspace": str(uuid.uuid4()), "agentsdk.tenant": tenant, "agentsdk.project": project,
        "agentsdk.owner": this_owner()})  # reapable after a crash of this process
    try:
        return handle, chosen._spaces[handle.workspace_id].address
    except BaseException:
        await chosen.remove(handle)
        raise


async def controls(chosen, neighbour) -> dict:
    """From the neighbour's proxy: whether the host's address and port answered it, whether the
    neighbour's proxy answered on its own address, and what the rebinding name resolved to. On the
    fake, what its rotor gave."""
    if not isinstance(chosen, DockerProvider):
        return {"rebinds": {a for given in chosen._resolve[REBIND].given for a in given}}
    from agentsdk.workspace.docker import proxy_name

    handle, address = neighbour
    code, out, _ = await chosen._cli("exec", proxy_name(handle.workspace_id), "python3", "-c", CONTROL, HOST[0],
                                     str(HOST[1]), address, str(egress.PORT), REBIND, str(REBIND_LOOKUPS),
                                     timeout=180)
    said = dict(line.split(" ", 1) for line in out.decode(errors="replace").splitlines() if " " in line)
    rebinds = set() if said.get("rebinds", "none") == "none" else set(said["rebinds"].split(","))
    return {"host": code == 0 and said.get("host") == "connected",
            "neighbour": code == 0 and said.get("neighbour") == "connected", "rebinds": rebinds}


def rebound(addresses: set[str]) -> bool:
    """The witness that the name rebinds: it answered a private address and a global one."""
    found = [ipaddress.ip_address(a).is_global for a in addresses]
    return True in found and False in found


def failed_inside(outcome: str | None, *, answers: bool, refused_reaches: bool = False) -> str | None:
    """A way out's failing clause, or None. Its address must answer elsewhere (the control), and the
    attempt from inside must fail; a refused connection was answered, so its network was reached
    (DECISION-0d5473ef)."""
    if not answers:
        return "the address answered no one outside the sandbox either: a failure inside would prove nothing"
    if outcome is None or not outcome.startswith("failed"):
        return f"it said {outcome!r}"
    if refused_reaches and outcome.split()[1:2] == ["ConnectionRefusedError"]:
        return f"it said {outcome!r}, so its network was reached"
    return None


# --- the demonstration --------------------------------------------------------------------------------


async def demonstrate(runner: RecordingRunner, orchestrator: Orchestrator, chosen, *, model_id: str, planner_id: str,
                      tenant: str, project: str, persistence, rng: random.Random, neighbour, network: str,
                      targets: list[str], live: bool, say=print):
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

    # FR-127: the server's tools are blocked until accepted; a first look names their versions.
    server_role = roles(model_id, chosen, network, neighbour[1], ",".join(targets), live)["server"]
    first = await runner.run(server_role, FIRST_LOOK, RunConfig(tenant_id=tenant, project_id=project, max_turns=2))
    blocked = [e.payload for e in first.events if e.event_type is EventType.MCP_TOOL_BLOCKED]
    if blocked:
        await runner.accept_tools(tenant, project, ALIAS, [p["tool_hash"] for p in blocked], principal=OPERATOR)

    # --- the plan -----------------------------------------------------------------------------------
    config = orchestrator.config(tenant_id=tenant, project_id=project, model_override=planner_id, max_turns=6,
                                 approval_timeout=300)
    before = set(runner.sinks)
    handle = await runner.start(orchestrator.agent, plan_task(rng), config)
    seen = {"snapshot": None, "snapshotted": None, "snapshot_error": None, "refused": 0}
    helper = asyncio.ensure_future(operator(runner, chosen, tenant, project, handle.run_id, rng, seen))
    try:
        result = await handle.result()
    finally:
        helper.cancel()
    say(f"plan {result.run_id}: {result.status.value}")
    opened = [r for r in runner.sinks if r not in before and r != result.run_id]
    events = await events_of(runner, persistence, opened)
    children = {}
    for run_id, found in events.items():
        started = next((p for kind, p in found if kind == EventType.RUN_STARTED.value), {})
        if str(started.get("parent_run_id")) == str(result.run_id):
            children[str(started.get("agent_spec_id", "")).removeprefix("contender-")] = (run_id, found)
    say(f"  children {({name: run_id for name, (run_id, _) in children.items()})}")

    histories, issued, returned = {}, {}, {}
    for name, (run_id, _) in children.items():
        sessions = runner._sessions_for(RunScope(run_id=run_id, tenant_id=tenant, project_id=project))
        histories[name] = await asyncio.to_thread(sessions.history, run_id)
        for m in histories[name]:
            if m.role is Role.ASSISTANT:
                issued.update({(name, c.id): (c.name, dict(c.arguments)) for c in m.tool_calls})
            for r in m.tool_results:
                returned[(name, r.tool_call_id)] = r.content
    commands = await asyncio.to_thread(store.commands, tenant, project)
    rows = await asyncio.to_thread(store.connections, tenant, project)

    # --- egress ---------------------------------------------------------------------------------------
    # Every run of the probe is judged on its own rows and output: a live model may run it again (M47
    # live run), and a run cut short still says what it reached (as M42 pre-review F2 found).
    egress_run = children.get("egress", (None, []))[0]
    probes = {c.command_id for c in commands if runs_probe(c.argv, "egress")}
    by_call = {c.tool_call_id: c for c in commands if c.run_id == egress_run and c.command_id in probes}
    runs = []  # (outcomes by attempt, the command's row, its connection rows)
    for (name, call_id), (tool, arguments) in issued.items():
        if name == "egress" and call_id in by_call and runs_probe(arguments.get("argv"), "egress"):
            out: dict[str, list[str]] = {}
            for line in stdout_of(returned.get((name, call_id), "")).splitlines():
                attempt, _, outcome = line.partition(" ")
                out.setdefault(attempt, []).append(outcome)
            command = by_call[call_id]
            runs.append((out, command, [r for r in rows if r.command_id == command.command_id]))
    ended = [run for run in runs if run[1].exit_code == 0 and "gateway" in run[0]]

    def said(out: dict, attempt: str) -> str:
        return (out.get(attempt) or [""])[0]

    def every(clause, among) -> str | None:
        """The first failing clause over the runs `among`, naming the run when there are several."""
        for i, (out, command, own) in enumerate(among):
            why = clause(out, [r for r in own if r.host != REBIND])
            if why:
                return why if len(among) == 1 else f"run {i + 1} of {len(among)}: {why}"
        return None

    probed = bool(ended)
    found = await controls(chosen, neighbour)  # the server's checks read them too, whatever egress did
    for row in rows:
        say(f"  {row.reason:15} {row.host}:{row.port} -> {row.address}")

    def fetched(out: dict, plain: list) -> str | None:
        allowed = [r for r in plain if r.allowed]
        return ((None if said(out, "allowed").isdigit() and int(said(out, "allowed")) > 0 else
                 f"the fetch said {said(out, 'allowed')!r}")
                or (None if len(allowed) == 1 and allowed[0].host == "example.com" and allowed[0].address else
                    f"its rows read {[(r.host, r.address, r.command_id) for r in allowed]}"))

    check("the allowed pair was fetched through the proxy, recorded allowed with its address, by the command "
          "that made it", probed, "the egress probe never ran to its end",
          lambda: every(fetched, ended)
          or every(lambda out, plain: next((f"{r.host} was allowed" for r in plain
                                            if r.allowed and r.host != "example.com"), None), runs))

    def refusals(out: dict, plain: list) -> str | None:
        refused = sorted(r.reason for r in plain if not r.allowed)
        return None if refused == REFUSALS else f"the refusals recorded were {refused}"

    check("an IP literal, an unlisted host, a name resolving privately, another site by SNI, plain HTTP and a "
          "second request were each refused and recorded by reason", probed, "the egress probe never ran to its end",
          lambda: every(refusals, ended)
          or next((f"{r.reason} was recorded with command {r.command_id}" for r in rows
                   if r.command_id not in probes), None))

    rebinding = [r for _, _, own in runs for r in own if r.host == REBIND]
    tried = sum(len(out.get("rebind", [])) for out, _, _ in runs)

    def never_private() -> str | None:
        if not rebound(found.get("rebinds", set())):
            return f"the name never rebound: it answered {sorted(found.get('rebinds', set()))}"
        if len(rebinding) != tried:
            return f"{len(rebinding)} row(s) for {tried} attempt(s)"
        # The attack needs the proxy handed both answers: a check of the public one, then a dial.
        if not ({r.allowed for r in rebinding} == {True, False}):
            return f"the proxy was never handed both answers: {sorted({r.reason for r in rebinding})}"
        # An allowed attempt reached what answers at the global address it checked -- nothing
        # answers on loopback inside the proxy's container -- so it never resolved again (FR-161).
        return next((f"an attempt was recorded {r.reason} at {r.address}, {r.bytes_in} byte(s) back"
                     for r in rebinding
                     if not ((not r.allowed and r.reason == "not_global")
                             or (r.allowed and r.address and ipaddress.ip_address(r.address).is_global
                                 and (r.bytes_in or 0) > 0))), None)

    check("a rebinding name was never reached at a private address: each attempt refused not_global or recorded "
          "at a global one", probed and tried, "the rebinding name was never tried", never_private)

    if docker:
        # On Docker only: the fake has no network, so there these can never fail (F8, DECISION-26009e04).
        def direct(out: dict, plain: list) -> str | None:
            allowed = [r for r in plain if r.allowed]
            reached = allowed[0].address if len(allowed) == 1 else None
            if not reached:
                return "no allowed connection recorded the address the proxy reached"
            if reached not in targets:
                return f"the proxy reached {reached}, which the probe did not dial ({','.join(targets)})"
            if not host_reaches(reached):
                return f"the host could not reach {reached} either: a failure inside would prove nothing"
            if set(said(out, "direct").split(",")) != {"failed"}:
                return f"direct: {said(out, 'direct')}"
            return None

        check("a direct connection to the address the proxy reached, the host directly and a name lookup failed "
              "inside the workspace", probed, "the egress probe never ran to its end",
              lambda: every(direct, ended)
              or (None if found.get("host") else "the host's address and port answered no container on an "
                  "ordinary network either: a failure inside would prove nothing")
              or every(lambda out, plain: next((f"{name}: {said(out, name)}" for name in ("host_direct", "dns")
                                                if name in out and not said(out, name).startswith("failed")), None),
                       runs))
        check("a neighbour workspace's network was unreachable", probed, "the egress probe never ran to its end",
              lambda: every(lambda out, plain: failed_inside(said(out, "neighbour"), answers=found.get("neighbour", False),
                                                             refused_reaches=True), ended))
        check("no host listener was open on the workspace's network, its gateway isolated", probed,
              "the egress probe never scanned its network",
              lambda: every(lambda out, plain: None if said(out, "gateway") == "none_open"
                            else f"the gateway: {said(out, 'gateway')}", ended))

    connection_events = [p for _, (_, found_events) in children.items() for kind, p in found_events
                         if kind == EventType.WORKSPACE_CONNECTION.value]
    check("each connection's row has its WorkspaceConnection event, and none names a token", probed and rows,
          "no connection was recorded",
          lambda: (None if sorted(p["connection_id"] for p in connection_events) == sorted(r.connection_id for r in rows)
                   else f"{len(connection_events)} event(s) for {len(rows)} row(s)")
          or next((f"an event names {key}" for p in connection_events for key in p if "token" in key), None))

    # --- the server ---------------------------------------------------------------------------------
    server_said: dict[str, list[str]] = {}
    for (name, call_id), (tool, arguments) in issued.items():
        # A quote goes to the echo alone, and only reach and resolve calls are judged by their
        # arguments: no call is set aside by its id, which is the model's (pre-review F3).
        if name == "server":
            key = tool if tool != f"{ALIAS}__reach" else f"reach {arguments.get('host')}"
            server_said.setdefault(key, []).append(returned.get((name, call_id), ""))
    served = any(MARK in c for c in server_said.get(f"{ALIAS}__echo", []))
    server_rows = [r for r in await asyncio.to_thread(store.workspaces, tenant, project) if r.scope == "server"]
    listed = {h.workspace_id for h in await chosen.list_owned({"agentsdk.tenant": tenant, "agentsdk.project": project})}
    check("the sandboxed server served the plan's child: its echo came back", server_said.get(f"{ALIAS}__echo"),
          "the server child never called its echo",
          lambda: None if served else f"its echo said {server_said.get(f'{ALIAS}__echo')}")

    def first_said(key: str) -> str | None:
        return (server_said.get(key) or [None])[0]

    if docker:
        check("the sandboxed server reached nothing: the network, the host's database port, a neighbour and a name "
              "lookup, each against a control", served, "the server never served",
              lambda: failed_inside(first_said(f"reach {network}"), answers=host_reaches(network))
              or failed_inside(first_said(f"reach {HOST[0]}"), answers=found.get("host", False))
              or failed_inside(first_said(f"reach {neighbour[1]}"), answers=found.get("neighbour", False),
                               refused_reaches=True)
              or (None if (first_said(f"{ALIAS}__resolve") or "").startswith("failed")
                  else f"its lookup said {first_said(f'{ALIAS}__resolve')!r}"))

    # --- the snapshot --------------------------------------------------------------------------------
    ref = seen["snapshot"]
    say(f"  snapshot {ref.snapshot_id if ref else seen['snapshot_error']}: "
        f"{ref.size_bytes if ref else '-'} B of workspace {seen['snapshotted']}")
    recorded = {s.snapshot_id: s for s in await asyncio.to_thread(store.snapshots, tenant, project)}
    check("a snapshot taken between the child's commands was measured past the later bound and recorded", ref,
          f"no snapshot was taken ({seen['snapshot_error'] or 'the second command was never asked'})",
          lambda: (None if ref.size_bytes >= BIG > BELOW else f"it measured {ref.size_bytes} B")
          or (None if ref.snapshot_id in recorded and recorded[ref.snapshot_id].size_bytes == ref.size_bytes
              and recorded[ref.snapshot_id].workspace_id == seen["snapshotted"] else f"its rows read {recorded}"))

    def reader(**fields) -> AgentSpec:
        return AgentSpec(id="restorer", instructions="Read back as asked.", preferred_model="reader:r",
                         tool_profile=("read_workspace_file", "list_workspace"),
                         workspace=WorkspaceSpec(image=IMAGE, **fields))

    async def restore(owner: str, **fields):
        fields.setdefault("from_snapshot", ref.snapshot_id)
        return await runner.run(reader(**{k: v for k, v in fields.items() if v is not None}), json.dumps(READ_BACK),
                                RunConfig(tenant_id=owner, project_id=project))

    restored = await restore(tenant) if ref else None
    elsewhere = await restore(tenant + ELSEWHERE) if ref else None
    # Refused for its tenancy, not for any failure (round 1 F1, DECISION-ee543c57): the other tenancy
    # starts a workspace of its own (the control), and cannot release the snapshot -- the same lookup,
    # answered by a value, not by an error's text.
    plain = await restore(tenant + ELSEWHERE, from_snapshot=None) if ref else None
    theirs = await runner.release_snapshot(tenant + ELSEWHERE, project, ref.snapshot_id) if ref else None
    below = await restore(tenant, max_workspace_bytes=BELOW) if ref else None
    got = {}
    if restored is not None:
        sessions = runner._sessions_for(RunScope(run_id=restored.run_id, tenant_id=tenant, project_id=project))
        got = {r.tool_call_id: r.content for m in await asyncio.to_thread(sessions.history, restored.run_id)
               for r in m.tool_results}
    listing = got.get("t0-2", "")
    # Its subject: the workspace changed after the snapshot -- the second command, started after the
    # snapshot was recorded, read back the a.txt it rewrote (pre-review F1).
    snapshot_run = children.get("snapshot", (None, []))[0]
    taken_at = recorded[ref.snapshot_id].created_at if ref and ref.snapshot_id in recorded else None
    after_rows = {c.tool_call_id: c for c in commands if c.run_id == snapshot_run and runs_probe(c.argv, "after")}
    changed = taken_at is not None and any(
        name == "snapshot" and call_id in after_rows and after_rows[call_id].started_at >= taken_at
        and any(line.split()[:1] == ["after"] and line.split()[-1:] == ["changed"]
                for line in stdout_of(returned.get((name, call_id), "")).splitlines())
        for (name, call_id) in issued)
    check("restored in its own tenancy within bounds, it reads the state at the snapshot, the link still a link",
          restored and changed, "no snapshot was taken" if not restored else
          "the workspace was never changed after its snapshot",
          lambda: (None if (got.get("t0-0"), got.get("t0-1")) == ("one", "two") else
                   f"it read {got.get('t0-0')!r}, {got.get('t0-1')!r}")
          or (None if "l\tlink" in listing and "big.bin" in listing and "later.txt" not in listing else
              f"its listing reads {listing!r}")
          or (None if made(restored) and made(restored)[0] != seen["snapshotted"] else f"it made {made(restored)}"))
    check("another tenancy's workspace could not start from the snapshot", elsewhere and restored,
          "no snapshot was taken",
          lambda: (None if not made(elsewhere) and made(restored) else f"it made {made(elsewhere)}")
          or (None if made(plain) else "the other tenancy could start no workspace at all")
          or (None if theirs is False else f"the other tenancy's release answered {theirs}"))
    check("a workspace whose bound is below the snapshot's size could not start from it", below and restored,
          "no snapshot was taken",
          lambda: None if not made(below) and made(restored) and ref.size_bytes > BELOW else f"it made {made(below)}")

    # --- compute ---------------------------------------------------------------------------------------
    governor = config.budget_lease.governor
    burn_run, burn_events = children.get("burn", (None, []))
    burn_rows = {c.command_id: c for c in commands if c.run_id == burn_run}
    reserved = governor.lease("burn").reservation.usd if "burn" in children else None
    running, crossing, after = Decimal(0), None, []
    for kind, p in burn_events:
        if kind == EventType.WORKSPACE_COMMAND_STARTED.value and crossing is not None:
            after.append(p["command_id"])
        if kind in (EventType.MODEL_CALLED.value, EventType.WORKSPACE_COMMAND_ENDED.value) and p.get("cost_usd"):
            spent_before, running = running, running + Decimal(p["cost_usd"])
            if kind == EventType.WORKSPACE_COMMAND_ENDED.value and spent_before < reserved <= running:
                crossing = p
    burns = [p for kind, p in burn_events if kind == EventType.WORKSPACE_COMMAND_ENDED.value
             and p["command_id"] in burn_rows and runs_probe(burn_rows[p["command_id"]].argv, "burn")
             and p.get("exit_code") == 0]
    terminal = next((p for kind, p in reversed(burn_events) if kind in (EventType.RUN_FAILED.value,
                                                                        EventType.RUN_COMPLETED.value)), {})
    costs = [Decimal(p["cost_usd"]) for p in burns if p.get("cost_usd") is not None]
    say(f"  burn: reserved {reserved}, spent {running}, {len(burns)} burn(s); ended {terminal.get('reason')}")
    check("the burning child was stopped within one command's cost of its ceiling, no command started after",
          burns, "the burner never burned",
          lambda: ("no command's cost crossed its ceiling" if crossing is None else None)
          or (f"{len(after)} command(s) started after it" if after else None)
          or (None if terminal.get("reason") == "budget_exceeded" else f"the child ended {terminal}")
          or (None if governor.lease("burn").spent.usd <= reserved + max(costs, default=Decimal(0)) else
              f"it spent {governor.lease('burn').spent.usd} against {reserved}"))
    check("every command of the burning child was charged at least its CPU reading", burns,
          "the burner never burned",
          lambda: next((f"command {p['command_id']} read {burn_rows[p['command_id']].cpu_usec} us and was charged "
                        f"{p.get('cost_usd')}" for p in burns
                        if burn_rows[p["command_id"]].cpu_usec is None or p.get("cost_usd") is None
                        or Decimal(p["cost_usd"]) < Decimal(burn_rows[p["command_id"]].cpu_usec) / 1_000_000 * PRICE),
                       None)
          or next((f"a burn of {BURN} s read {p['cpu_seconds']} s" for p in burns
                   if not 0.9 * BURN <= p["cpu_seconds"] <= BURN + SLACK), None))

    # --- closing --------------------------------------------------------------------------------------
    if neighbour is not None and neighbour[0] is not None:
        await chosen.remove(neighbour[0])
    folders = [s.folder for s in getattr(chosen, "_spaces", {}).values() if getattr(s, "folder", None)]
    await runner.aclose()
    left = await remains(chosen, tenant)
    after_rows = [r for r in await asyncio.to_thread(store.workspaces, tenant, project) if r.scope == "server"]
    check("the server was killed with its Runner: listed before aclose(), nothing of it after, its row removed aclose",
          served and server_rows and server_rows[0].workspace_id in listed, "the server's workspace was never seen",
          lambda: ("the daemon could not be listed" if left is None else None)
          or (f"{len(left)} object(s) of the tenancy remained" if left else None)
          or next((f"its folder {f} remained" for f in folders if f.exists()), None)
          or (None if [r.removed_reason for r in after_rows] == ["aclose"] else f"its rows read {after_rows}"))
    snapshots = await chosen.list_snapshots({"agentsdk.tenant": tenant, "agentsdk.project": project})
    rows_after = await asyncio.to_thread(store.snapshots, tenant, project)
    check("after aclose() nothing of the snapshot remains, its row removed aclose", ref, "no snapshot was taken",
          lambda: f"{len(snapshots)} snapshot(s) remained" if snapshots else
          None if [r.removed_reason for r in rows_after] == ["aclose"] else f"its rows read {rows_after}")
    # Its subject: something of the example was made -- a command's workspace or the server's.
    check("after aclose() no workspace, network, proxy or volume of the example remains", commands or server_rows,
          "no workspace was ever made", lambda: "the daemon could not be listed" if left is None
          else f"{len(left)} remained" if left else None)
    return checks, [first.run_id, result.run_id] + [r.run_id for r in (restored, elsewhere, plain, below) if r]


# --- running it ---------------------------------------------------------------------------------------


def report(checks) -> list[str]:
    """A check whose subject never happened fails in every mode (DECISION-21e7f6fb)."""
    failed = []
    for label, passed, why in checks:
        if passed is None:
            print(f"[FAIL] not exercised: {label} ({why})")
        else:
            print(f"[{'PASS' if passed else 'FAIL'}] {label}" + ("" if passed else f" -- {why}"))
        if not passed:
            failed.append(label)
    return failed


def docker_ready() -> str | None:
    """Why this example cannot use Docker here, or None."""
    try:
        if subprocess.run(["docker", "version"], capture_output=True, timeout=30).returncode != 0:
            return "docker not running"
        if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, timeout=30).returncode != 0:
            return "the example image is not on this host (see the docstring to pull it)"
    except (OSError, subprocess.TimeoutExpired):
        return "docker not running"
    return None


def the_network(chosen) -> str:
    """An address of example.com, as the host resolves it: what the server tries to reach."""
    return direct_targets(chosen)[0]


async def offline(store: str = "memory", provider_name: str = "fake", *, seed: int | None = None,
                  tenant: str = TENANT, project: str | None = None, quote: bool = False, quiet: bool = False):
    """The exit signal with scripted children; the tests call it, ten times on Docker. A project of
    its own each time, so one repetition's records never meet another's."""
    rng = random.Random(seed)
    persistence = None
    if store == "postgres":
        from dotenv import load_dotenv

        from agentsdk import Persistence
        from agentsdk.config import normalise_database_url

        load_dotenv()
        persistence = Persistence.postgres(normalise_database_url(os.environ["DATABASE_URL"]), create_schema=False)
    chosen = provider(provider_name)
    project = project or f"contention2-{uuid.uuid4().hex[:8]}"
    delay = 0.5 if provider_name == "docker" else 0.01
    say = (lambda *a: None) if quiet else print
    neighbour = runner = None
    try:  # the neighbour and the Runner made inside it, so whatever fails after removes them
        neighbour = await neighbour_of(chosen, tenant, project)
        network, targets = the_network(chosen), direct_targets(chosen)
        orchestrator = orchestrator_for("child:m", chosen, network, neighbour[1], ",".join(targets))
        runner = RecordingRunner({"planner": ScriptedPlanner(), "child": ScriptedChild(rng, delay, quote),
                                  "reader": ScriptedReader()},
                                 tools=[orchestrator.tool], workspace_provider=chosen, persistence=persistence,
                                 mcp_servers=[sandboxed_server(chosen)], model_registry=scripted_registry(),
                                 max_concurrent_workspaces=len(CHILDREN) + 4)
        checks, runs = await demonstrate(runner, orchestrator, chosen, model_id="child:m", planner_id="planner:p",
                                         tenant=tenant,
                                         project=project, persistence=persistence, rng=rng, neighbour=neighbour,
                                         network=network, targets=targets, live=False, say=say)
    finally:
        await _left_nothing(chosen, runner, neighbour)
    return checks, runs


async def _left_nothing(chosen, runner, neighbour) -> None:
    """However the demonstration ended -- passing, failing, raising or cancelled -- its Runner is
    closed, which removes its workspaces, its server and its snapshots, and the neighbour goes
    (M47 pre-review F2). Both may already be gone."""
    try:
        if runner is not None:
            await runner.aclose()
    finally:
        if neighbour is not None and neighbour[0] is not None:
            with contextlib.suppress(Exception):
                await chosen.remove(neighbour[0])


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
        docker, neighbour, runner = DockerProvider(), None, None
        project = f"contention2-{uuid.uuid4().hex[:8]}"
        try:
            model_id = f"m:{settings.default_model}"
            neighbour = await neighbour_of(docker, TENANT, project)
            network, targets = the_network(docker), direct_targets(docker)
            orchestrator = orchestrator_for(model_id, docker, network, neighbour[1], ",".join(targets), live=True)
            runner = RecordingRunner({"m": client, "reader": ScriptedReader()}, tools=[orchestrator.tool],
                                     workspace_provider=docker, persistence=persistence,
                                     mcp_servers=[sandboxed_server(docker)],
                                     max_concurrent_workspaces=len(CHILDREN) + 4)
            checks, runs = await demonstrate(runner, orchestrator, docker, model_id=model_id, planner_id=model_id,
                                             tenant=TENANT,
                                             project=project, persistence=persistence, rng=random.Random(),
                                             neighbour=neighbour, network=network, targets=targets, live=True)
            print(f"\nrun ids (to remove the demo's rows, children included by parent): {' '.join(runs)}")
            return checks
        finally:
            try:
                await _left_nothing(docker, runner, neighbour)
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
    mode.add_argument("--offline", action="store_true", help="in memory, scripted children, FakeProvider")
    mode.add_argument("--scripted", action="store_true", help="in memory, scripted children, Docker")
    args = parser.parse_args()
    if not args.offline and (why := docker_ready()) is not None:
        print(f"[SKIP] {why}")
        raise SystemExit(0)
    if args.offline or args.scripted:
        checks, _ = asyncio.run(offline(provider_name="fake" if args.offline else "docker"))
    else:
        checks = live()
    failed = report(checks)
    if failed:
        raise SystemExit(f"failed: {failed}")
