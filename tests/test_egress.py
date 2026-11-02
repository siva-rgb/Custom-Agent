"""M44 gate: egress by allow list (FR-159 to FR-163, AC-102, NFR-46, NFR-47, NFR-49).

Written before the implementation, against FR-159 to FR-163 as the owner settled them in the
pre-flight (DECISION-88dea779) and the probe after it (KNOWLEDGE-bd42619c):
  * a per-command token in each command's HTTPS_PROXY, as the URL's password
    (`http://agentsdk:<token>@<proxy>:<port>`), which the proxy reports and the provider maps to the
    command; no token is ever recorded;
  * at most 10,000 connections in a proxy's life, then one `connection_limit` row carrying the count;
  * the proxy hardened as a command is, 64 MiB, 32 pids, 0.50 cpus, its CPU not charged;
  * at most 32 tunnels open at once (`busy` past that), 60 s idle, bytes each way on the row.

The surface these tests pin, in `agentsdk.workspace.egress` (standard library only, and the very
program the proxy container runs):
  * `parse_egress(value) -> tuple[str, ...]`, raising ValueError; `WorkspaceSpec.egress`.
  * `Proxy(allow, *, report, resolve=..., connect=..., is_global=..., max_tunnels=32,
    idle_seconds=60.0, max_connections=10_000)`: `handle(reader, writer)` serves one client and
    `serve(host, port)` listens; each decision and each tunnel's end is `report(dict)`.
  * `client_hello(sni) -> bytes` and `sni_of(data) -> str | None`.
  * `SOURCE`, the module's own text, run as `python3 -c SOURCE CONFIG`.
On `FakeProvider(resolve=..., sites=...)`, `FakeCommand.connect(host, port, sni=None)` meets the same
rules; `resolve_name` and `connect_address` fail, as from a command on Docker.

Tests marked `docker` run in the `docker` gate (DECISION-88dea779). The persisted half removes every
row it writes, by its own tenancies.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import importlib.util
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

from agentsdk import AgentSpec, Persistence, RunConfig, RunStatus, Runner
from agentsdk.config import normalise_database_url
from agentsdk.events import EventType
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.postgres import RunScope
from agentsdk.primitives import Message, Role, ToolCall
from agentsdk.tools import ApprovalPolicy
from agentsdk.workspace import FakeProvider, WorkspaceHandle, WorkspaceSpec
from agentsdk.workspace import egress as eg
from agentsdk.workspace import manager as mgr

REPO = Path(__file__).resolve().parent.parent
DSN = normalise_database_url(os.environ.get("DATABASE_URL"))
PREFIX = "SYN-m44-"
PROJECT = "p-m44"
STORES = ["memory", "postgres"]
FAKE_IMAGE = "fake/workspace@sha256:" + "0" * 64
TENANTS: list[str] = []
CRLF = b"\r\n"


def _docker_leftovers(tenant: str) -> None:
    """Removes what a test left on the daemon for its own tenancy: never anything else."""
    for kind, listing in (("container", ["ps", "-aq"]), ("volume", ["volume", "ls", "-q"]),
                          ("network", ["network", "ls", "-q"])):
        found = subprocess.run(["docker", *listing, "--filter", f"label=agentsdk.tenant={tenant}"],
                               capture_output=True, text=True, timeout=60).stdout.split()
        if found:
            subprocess.run(["docker", kind, "rm", "-f", *found] if kind != "network" else
                           ["docker", "network", "rm", *found], capture_output=True, timeout=120)


@pytest.fixture(autouse=True)
def remove_what_the_test_wrote():
    TENANTS.clear()
    DIALLED.clear()
    yield
    if TENANTS and subprocess.run(["docker", "version"], capture_output=True).returncode == 0:
        for tenant in TENANTS:
            _docker_leftovers(tenant)
    if DSN and TENANTS:
        with psycopg.connect(DSN, autocommit=True) as conn:
            ids = [r[0] for r in conn.execute("SELECT run_id FROM runs WHERE tenant_id = ANY(%s)", (TENANTS,))]
            if conn.execute("SELECT to_regclass('workspace_connections')").fetchone()[0] is not None:
                conn.execute("DELETE FROM workspace_connections WHERE tenant_id = ANY(%s)", (TENANTS,))
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
# FR-159: the spec
# =================================================================================================


@pytest.mark.parametrize("value", [
    ("1.2.3.4:443",), ("[::1]:443",), ("::1:443",),                  # IP literals
    ("*.example.com:443",), ("exa*mple.com:443",),                  # wildcards
    ("example.com:0",), ("example.com:65536",), ("example.com:-1",),  # ports
    ("example.com",), ("example.com:",), ("example.com:https",),    # no port
    ("Example.com:443",), ("example.com.:443",),                    # not lower case, trailing dot
    ("exa mple.com:443",), ("-example.com:443",), ("example..com:443",), (":443",),
    ("1.2.3:443",), ("example.123:443",),                              # a numeric last label resolves as an address
    ("a" * 64 + ".com:443",), (".".join(["a" * 63] * 4) + ":443",),  # label and name too long
    tuple(f"h{i}.example.com:443" for i in range(33)),              # more than 32
    "example.com:443",                                               # not a tuple
    ("example.com:443", "example.com:443"),                          # repeated
    (443,),
])
def test_each_refused_egress_value_raises_value_error(value):
    with pytest.raises(ValueError, match="egress"):
        WorkspaceSpec(image=FAKE_IMAGE, egress=value)


def test_a_valid_egress_is_kept_and_recorded():
    allowed = tuple(f"h{i}.example.com:{i + 1}" for i in range(31)) + ("pypi.org:443",)
    spec = WorkspaceSpec(image=FAKE_IMAGE, egress=allowed)
    assert spec.egress == allowed and eg.parse_egress(allowed) == allowed
    assert spec.record()["egress"] == list(allowed)


def test_a_spec_without_egress_records_what_it_did_before():
    """NFR-49: no egress key, so its workspaces row, its manifest and its plan key are unchanged."""
    spec = WorkspaceSpec(image=FAKE_IMAGE)
    assert spec.egress == () and "egress" not in spec.record()
    with_egress = WorkspaceSpec(image=FAKE_IMAGE, egress=("example.com:443",))
    assert mgr._spec_key(spec) != mgr._spec_key(with_egress)
    assert mgr._spec_key(with_egress) != mgr._spec_key(WorkspaceSpec(image=FAKE_IMAGE, egress=("example.org:443",)))


# =================================================================================================
# FR-161: the proxy's rules, in process
# =================================================================================================


def _head(target: str, token: str | None = "tok", method: str = "CONNECT", extra: bytes = b"") -> bytes:
    lines = [f"{method} {target} HTTP/1.1".encode(), f"Host: {target}".encode()]
    if token is not None:
        lines.append(b"Proxy-Authorization: Basic " + base64.b64encode(f"agentsdk:{token}".encode()))
    return CRLF.join(lines) + CRLF + CRLF + extra


class Upstream:
    """A local server standing in for the allowed host: it echoes what it gets."""

    def __init__(self) -> None:
        self.server = None
        self.connections = 0

    async def __aenter__(self):
        async def echo(reader, writer):
            self.connections += 1
            try:
                while data := await reader.read(65536):
                    writer.write(data)
                    await writer.drain()
            finally:
                writer.close()

        self.server = await asyncio.start_server(echo, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()


DIALLED: list[tuple[str, int]] = []  # where a test's proxy connected upstream


def _proxy(reports: list, upstream: Upstream | None, *, addresses=("93.184.215.14",), **limits) -> eg.Proxy:
    async def resolve(host, port):
        return list(addresses)

    async def connect(address, port):
        assert upstream is not None, "no upstream in this test"
        DIALLED.append((address, port))
        return await asyncio.open_connection("127.0.0.1", upstream.port)

    return eg.Proxy(("example.com:443",), report=reports.append, resolve=resolve, connect=connect, **limits)


async def _serve(proxy: eg.Proxy):
    server = await proxy.serve("127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _ask(port: int, head: bytes, then: bytes | None = None, read_back: bool = True):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(head)
    await writer.drain()
    status = await asyncio.wait_for(reader.readuntil(CRLF + CRLF), 5)
    back = b""
    if then is not None and status.startswith(b"HTTP/1.1 200"):
        writer.write(then)
        await writer.drain()
        if read_back:
            try:
                back = await asyncio.wait_for(reader.readexactly(len(then)), 5)
            except (asyncio.IncompleteReadError, ConnectionError):
                back = b""
    writer.close()
    return status, back


async def _settle(reports: list, count: int) -> None:
    for _ in range(200):
        if len(reports) >= count:
            return
        await asyncio.sleep(0.02)


def _decisions(reports):
    return [r for r in reports if r["kind"] == "connection"]


async def test_the_allowed_pair_connects_and_its_end_reports_the_bytes_each_way():
    reports: list = []
    async with Upstream() as upstream:
        server, port = await _serve(_proxy(reports, upstream))
        hello = eg.client_hello("example.com")
        status, back = await _ask(port, _head("example.com:443", token="t-1"), hello)
        await _settle(reports, 2)
        server.close()
    assert status.startswith(b"HTTP/1.1 200") and back == hello
    [decision] = _decisions(reports)
    assert decision["allowed"] is True and decision["reason"] == "allowed"
    # The address it checked, never the name again (FR-161).
    assert DIALLED == [("93.184.215.14", 443)]
    assert (decision["host"], decision["port"], decision["address"], decision["token"]) == (
        "example.com", 443, "93.184.215.14", "t-1")
    [end] = [r for r in reports if r["kind"] == "end"]
    assert end["id"] == decision["id"] and end["bytes_out"] == len(hello) and end["bytes_in"] == len(hello)


@pytest.mark.parametrize("head, reason", [
    (_head("93.184.215.14:443"), "ip_literal"),
    (_head("[2606:2800:220:1::]:443"), "ip_literal"),
    (_head("example.org:443"), "not_listed"),
    (_head("example.com:80"), "not_listed"),
    (_head("EXAMPLE.com:443"), "not_listed"),
    (_head("example.com.:443"), "not_listed"),
    (_head("http://example.com/", method="GET"), "not_connect"),
    (b"CONNECT\r\n\r\n", "malformed"),
    (b"\x16\x03\x01\x00\x05hello", "malformed"),
    (b"CONNECT example.com:443 HTTP/1.1" + CRLF + b"X-Pad: " + b"x" * 9000 + CRLF + CRLF, "malformed"),
])
async def test_each_refused_request_is_answered_403_and_reported_by_reason(head, reason):
    reports: list = []
    server, port = await _serve(_proxy(reports, None))
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(head)
    await writer.drain()
    answer = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    await _settle(reports, 1)
    server.close()
    assert answer.startswith(b"HTTP/1.1 403")
    [decision] = _decisions(reports)
    assert (decision["allowed"], decision["reason"]) == (False, reason)


@pytest.mark.parametrize("addresses, reason", [
    (("127.0.0.1",), "not_global"), (("10.0.0.5",), "not_global"), (("192.168.65.254",), "not_global"),
    (("93.184.215.14", "172.17.0.1"), "not_global"), (("::1",), "not_global"), (("fd00::1",), "not_global"),
    (("169.254.169.254",), "not_global"), ((), "unresolved"),
])
async def test_a_listed_name_resolving_anywhere_not_global_is_refused(addresses, reason):
    reports: list = []
    async with Upstream() as upstream:
        server, port = await _serve(_proxy(reports, upstream, addresses=addresses))
        status, _ = await _ask(port, _head("example.com:443"), eg.client_hello("example.com"))
        await _settle(reports, 1)
        server.close()
    assert status.startswith(b"HTTP/1.1 403") and upstream.connections == 0
    [decision] = _decisions(reports)
    assert (decision["allowed"], decision["reason"]) == (False, reason)


@pytest.mark.parametrize("then, reason", [
    (eg.client_hello("other.example"), "sni_mismatch"),
    (eg.client_hello(None), "no_sni"),
    (b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n", "not_tls"),
    (_head("example.com:443"), "second_request"),
    (b"\x16\x03\x01\x00\x10" + b"\x00" * 16, "no_sni"),
])
async def test_a_tunnel_whose_client_hello_does_not_name_the_host_is_closed_before_any_upstream(then, reason):
    reports: list = []
    async with Upstream() as upstream:
        server, port = await _serve(_proxy(reports, upstream))
        status, back = await _ask(port, _head("example.com:443"), then)
        await _settle(reports, 1)
        server.close()
    assert status.startswith(b"HTTP/1.1 200") and back == b"" and upstream.connections == 0
    [decision] = _decisions(reports)
    assert (decision["allowed"], decision["reason"]) == (False, reason)


async def test_bytes_sent_with_the_request_count_as_the_tunnels_first():
    """A client that sends its ClientHello right behind CONNECT is read the same: the hello is checked."""
    reports: list = []
    async with Upstream() as upstream:
        server, port = await _serve(_proxy(reports, upstream))
        status, _ = await _ask(port, _head("example.com:443", extra=eg.client_hello("other.example")))
        await _settle(reports, 1)
        server.close()
    assert [d["reason"] for d in _decisions(reports)] == ["sni_mismatch"] and upstream.connections == 0


async def test_a_missing_or_malformed_token_is_reported_as_none_and_the_rules_still_apply():
    reports: list = []
    async with Upstream() as upstream:
        server, port = await _serve(_proxy(reports, upstream))
        await _ask(port, _head("example.com:443", token=None), eg.client_hello("example.com"))
        for credentials in (b"!!", base64.b64encode(b"someone:tok"), base64.b64encode(b"agentsdk:bad token!"),
                            base64.b64encode(b"agentsdk:"), base64.b64encode(b"agentsdk:" + b"t" * 65)):
            bad = _head("example.com:443", token=None).replace(
                b"\r\n\r\n", b"\r\nProxy-Authorization: Basic " + credentials + b"\r\n\r\n")
            await _ask(port, bad, eg.client_hello("example.com"))
        await _ask(port, _head("example.com:443", token="good_Token-1"), eg.client_hello("example.com"))
        await _settle(reports, 7)
        server.close()
    assert [(d["allowed"], d["token"]) for d in _decisions(reports)] == [(True, None)] * 6 + [(True, "good_Token-1")]


async def test_past_32_open_tunnels_a_request_is_refused_busy():
    reports: list = []
    async with Upstream() as upstream:
        server, port = await _serve(_proxy(reports, upstream, max_tunnels=2))
        held = []
        for _ in range(2):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(_head("example.com:443") + eg.client_hello("example.com"))
            await writer.drain()
            await reader.readuntil(CRLF + CRLF)
            held.append(writer)
        await _settle(reports, 2)
        status, _ = await _ask(port, _head("example.com:443"))
        await _settle(reports, 3)
        for writer in held:
            writer.close()
        server.close()
    assert status.startswith(b"HTTP/1.1 403")
    assert [d["reason"] for d in _decisions(reports)] == ["allowed", "allowed", "busy"]
    assert eg.Proxy(("example.com:443",), report=print).max_tunnels == 32


async def test_a_tunnel_idle_both_ways_is_closed_and_its_end_reported():
    reports: list = []
    async with Upstream() as upstream:
        server, port = await _serve(_proxy(reports, upstream, idle_seconds=0.3))
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        hello = eg.client_hello("example.com")
        writer.write(_head("example.com:443") + hello)
        await writer.drain()
        await reader.readuntil(CRLF + CRLF)
        await reader.readexactly(len(hello))
        closed = await asyncio.wait_for(reader.read(), 5)
        await _settle(reports, 2)
        server.close()
    assert closed == b"" and [r["kind"] for r in reports] == ["connection", "end"]
    assert eg.Proxy(("example.com:443",), report=print).idle_seconds == 60.0


async def test_past_the_connection_limit_every_connection_is_refused_and_counted():
    reports: list = []
    async with Upstream() as upstream:
        proxy = _proxy(reports, upstream, max_connections=2)
        server, port = await _serve(proxy)
        for _ in range(5):
            await _ask(port, _head("example.org:443"))
        await _settle(reports, 3)
        await proxy.close()  # round 2: the final count when it stops (DECISION-e510a5e3)
        server.close()
    assert [d["reason"] for d in _decisions(reports)] == ["not_listed", "not_listed"]
    limits = [r for r in reports if r["kind"] == "limit"]
    assert [r["count"] for r in limits] == [1, 3], limits
    assert eg.Proxy(("example.com:443",), report=print).max_connections == 10_000


def test_the_sni_is_read_from_a_client_hello_and_from_nothing_else():
    assert eg.sni_of(eg.client_hello("example.com")) == "example.com"
    assert eg.sni_of(eg.client_hello(None)) is None
    for junk in (b"", b"\x16", b"\x16\x03\x01\xff\xff" + b"\x01" * 50, b"\x17\x03\x03\x00\x01x", os.urandom(300)):
        assert eg.sni_of(junk) is None


def test_the_proxy_is_standard_library_only_and_fits_a_command_line():
    """NFR-46: it runs in the workspace's image with nothing installed; it goes as `python3 -c`."""
    tree = ast.parse(eg.SOURCE)
    imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    imported |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                 and node.level == 0}
    assert all(name in sys.stdlib_module_names or name == "__future__" for name in imported), imported
    assert not any(isinstance(node, ast.ImportFrom) and node.level for node in ast.walk(tree))
    assert len(subprocess.list2cmdline(["python3", "-c", eg.SOURCE, "{}"])) < 24_000
    assert eg.SOURCE == (REPO / "agentsdk" / "workspace" / "egress.py").read_text(encoding="utf-8")


def test_the_proxy_program_runs_on_its_own_and_reports_as_json_lines():
    config = json.dumps({"allow": ["example.com:443"], "port": 0})
    process = subprocess.Popen([sys.executable, "-I", "-c", eg.SOURCE, config], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    try:
        ready = json.loads(process.stdout.readline())
        assert ready["kind"] == "listening" and ready["port"] > 0

        async def ask():
            return await _ask(ready["port"], _head("example.org:443"))

        status, _ = asyncio.run(ask())
        decision = json.loads(process.stdout.readline())
    finally:
        process.kill()
        process.wait(10)
    assert status.startswith(b"HTTP/1.1 403") and decision["reason"] == "not_listed"


# =================================================================================================
# FR-160, FR-162 on FakeProvider: the same rules, recorded on both stores
# =================================================================================================


SITE = b"<html>example</html>"
RESOLVE = {"example.com": ["93.184.215.14"], "private.example.com": ["10.1.2.3"]}


async def _reach(cmd):
    """What a hostile command tries, each attempt printed with its outcome."""
    for attempt, call in (
            ("allowed", lambda: cmd.connect("example.com", 443)),
            ("other port", lambda: cmd.connect("example.com", 80)),
            ("unlisted", lambda: cmd.connect("example.org", 443)),
            ("ip literal", lambda: cmd.connect("93.184.215.14", 443)),
            ("private", lambda: cmd.connect("private.example.com", 443)),
            ("sni", lambda: cmd.connect("example.com", 443, sni="other.example")),
            ("resolve", lambda: cmd.resolve_name("example.com")),
            ("direct", lambda: cmd.connect_address("93.184.215.14", 443))):
        try:
            got = await call()
            cmd.out(f"{attempt} ok {len(got) if isinstance(got, (bytes, list)) else got}\n")
        except OSError as exc:
            cmd.out(f"{attempt} failed {type(exc).__name__}\n")
    return 0


def respond(content="", calls=()):
    return ModelResponse(message=Message(role=Role.ASSISTANT, content=content, tool_calls=tuple(calls)),
                         stop_reason=StopReason.TOOL_CALLS if calls else StopReason.END_TURN, usage=Usage(5, 1, 6))


class Turns:
    """Each turn makes the run_command calls its task lists, then answers."""

    async def send(self, request):
        turns = json.loads(request.messages[0].content)
        turn = sum(1 for m in request.messages if m.role is Role.ASSISTANT)
        if turn < len(turns):
            return respond(calls=[ToolCall(id=f"t{turn}-{i}", name="run_command", arguments={"argv": argv})
                                  for i, argv in enumerate(turns[turn])])
        return respond("done")


async def _egress_run(store: str, turns: list, egress=("example.com:443", "private.example.com:443"), **fake):
    tenant = tenancy()
    persistence = Persistence.postgres(DSN, create_schema=False) if store == "postgres" else None
    provider = FakeProvider(commands={"reach": _reach}, resolve=RESOLVE, sites={("example.com", 443): SITE}, **fake)
    runner = Runner({"m": Turns()}, workspace_provider=provider, persistence=persistence)
    agent = AgentSpec(id="reacher", instructions="x", preferred_model="m:x", tool_profile=("run_command",),
                      workspace=WorkspaceSpec(image=FAKE_IMAGE, run_command_approval=ApprovalPolicy.AUTO,
                                              egress=egress))
    result = await runner.run(agent, json.dumps(turns), RunConfig(tenant_id=tenant, project_id=PROJECT, max_turns=6))
    store_ = runner._workspace_store()
    rows = await asyncio.to_thread(store_.connections, tenant, PROJECT)
    commands = await asyncio.to_thread(store_.commands, tenant, PROJECT)
    sessions = runner._sessions_for(RunScope(run_id=result.run_id, tenant_id=tenant, project_id=PROJECT))
    OUTPUTS.append([line for m in await asyncio.to_thread(sessions.history, result.run_id)
                    for r in m.tool_results for line in r.content.splitlines()])
    await runner.aclose()
    return result, rows, commands


OUTPUTS: list[list[str]] = []  # each fake run's command output, as the model read it


def _connection_events(result) -> list[dict]:
    return [e.payload for e in result.events if e.event_type is EventType.WORKSPACE_CONNECTION]


@pytest.mark.parametrize("store", STORES)
async def test_on_the_fake_every_way_out_but_the_allowed_pair_fails_each_recorded_by_reason(store):
    result, rows, commands = await _egress_run(store, [[["reach"]]])
    assert result.status is RunStatus.COMPLETED, result.error
    [command] = commands
    by_reason = sorted((r.host, r.port, r.allowed, r.reason) for r in rows)
    assert by_reason == sorted([
        ("example.com", 443, True, "allowed"), ("example.com", 80, False, "not_listed"),
        ("example.org", 443, False, "not_listed"), ("93.184.215.14", 443, False, "ip_literal"),
        ("private.example.com", 443, False, "not_global"), ("example.com", 443, False, "sni_mismatch")])
    assert all(r.command_id == command.command_id and r.run_id == result.run_id for r in rows)
    [allowed] = [r for r in rows if r.allowed]
    assert allowed.address == "93.184.215.14" and allowed.bytes_in == len(SITE) and allowed.bytes_out > 0
    events = _connection_events(result)
    assert sorted((e["host"], e["reason"]) for e in events) == sorted((r.host, r.reason) for r in rows)
    assert all("token" not in e for e in events)


async def test_on_the_fake_a_workspace_without_egress_reaches_nothing_and_records_no_connection():
    result, rows, _ = await _egress_run("memory", [[["reach"]]], egress=())
    assert rows == [] and _connection_events(result) == []
    # No proxy at all, not a proxy that refuses: the attempt fails as on --network none, an OSError
    # and not a ConnectionRefusedError.
    assert [line for line in OUTPUTS[-1] if line.startswith("allowed")] == ["allowed failed OSError"], OUTPUTS[-1]


async def test_connections_are_recorded_before_the_run_reports_its_end(monkeypatch):
    """Recorded by a task of their own, drained before the workspace's removal is recorded (FR-162)."""
    from agentsdk.workspace import records

    real = records.InMemoryWorkspaceStore.record_connection

    def slow(self, record):
        import time
        time.sleep(0.2)
        return real(self, record)

    monkeypatch.setattr(records.InMemoryWorkspaceStore, "record_connection", slow)
    result, rows, _ = await _egress_run("memory", [[["reach"]]])
    assert result.status is RunStatus.COMPLETED and len(rows) == 6


async def test_on_the_fake_past_the_limit_one_row_carries_the_count():
    result, rows, _ = await _egress_run("memory", [[["reach"]], [["reach"]]], max_connections=4)
    limits = [r for r in rows if r.reason == "connection_limit"]
    assert len(limits) == 1 and limits[0].count == 8 and not limits[0].allowed
    assert len(rows) == 5 and len([e for e in _connection_events(result) if e["reason"] == "connection_limit"]) == 1


async def test_on_the_fake_a_connection_with_an_unknown_token_is_recorded_with_no_command():
    async def stranger(cmd):
        await cmd.connect("example.com", 443, token="not-a-command's")
        return 0

    tenant = tenancy()
    provider = FakeProvider(commands={"stranger": stranger}, resolve=RESOLVE, sites={("example.com", 443): SITE})
    runner = Runner({"m": Turns()}, workspace_provider=provider)
    agent = AgentSpec(id="s", instructions="x", preferred_model="m:x", tool_profile=("run_command",),
                      workspace=WorkspaceSpec(image=FAKE_IMAGE, run_command_approval=ApprovalPolicy.AUTO,
                                              egress=("example.com:443",)))
    result = await runner.run(agent, json.dumps([[["stranger"]]]), RunConfig(tenant_id=tenant, project_id=PROJECT,
                                                                             max_turns=3))
    rows = await asyncio.to_thread(runner._workspace_store().connections, tenant, PROJECT)
    await runner.aclose()
    assert [(r.allowed, r.command_id, r.run_id) for r in rows] == [(True, None, result.run_id)]


async def test_on_the_fake_a_command_can_send_its_proxy_anything_and_meets_the_same_rules():
    """FR-163: raw bytes to the workspace's proxy, as a command on Docker can write to its socket."""
    async def raw(cmd):
        auth = b"Proxy-Authorization: Basic " + base64.b64encode(b"agentsdk:" + cmd.egress_token.encode())
        for name, data in (("get", b"GET http://example.com/ HTTP/1.1" + CRLF + auth + CRLF + CRLF),
                           ("twice", _head("example.com:443", token=cmd.egress_token) * 2)):
            cmd.out(f"{name} {(await cmd.proxy(data)).split(CRLF)[0].decode()}\n")
        return 0

    tenant = tenancy()
    provider = FakeProvider(commands={"raw": raw}, resolve=RESOLVE, sites={("example.com", 443): SITE})
    runner = Runner({"m": Turns()}, workspace_provider=provider)
    agent = AgentSpec(id="r", instructions="x", preferred_model="m:x", tool_profile=("run_command",),
                      workspace=WorkspaceSpec(image=FAKE_IMAGE, run_command_approval=ApprovalPolicy.AUTO,
                                              egress=("example.com:443",)))
    await runner.run(agent, json.dumps([[["raw"]]]), RunConfig(tenant_id=tenant, project_id=PROJECT, max_turns=3))
    rows = await asyncio.to_thread(runner._workspace_store().connections, tenant, PROJECT)
    commands = await asyncio.to_thread(runner._workspace_store().commands, tenant, PROJECT)
    await runner.aclose()
    assert sorted(r.reason for r in rows) == ["not_connect", "second_request"]
    assert {r.command_id for r in rows} == {commands[0].command_id}


def test_the_store_refuses_a_connection_row_against_another_tenancys_workspace():
    from agentsdk.workspace.records import ConnectionRecord, now

    store = Persistence.postgres(DSN, create_schema=False).workspace_store()
    with pytest.raises(ValueError):
        store.record_connection(ConnectionRecord(
            connection_id=str(uuid.uuid4()), workspace_id=str(uuid.uuid4()), tenant_id=tenancy(), project_id=PROJECT,
            run_id=str(uuid.uuid4()), command_id=None, host="example.com", port=443, address=None, allowed=False,
            reason="not_listed", at=now()))


def test_migration_0024_adds_the_connections_table_only():
    sql = (REPO / "agentsdk" / "migrations" / "0024_workspace_connections.sql").read_text(encoding="utf-8")
    assert "workspace_connections" in sql and "workspace_snapshots" not in sql
    assert "token" not in sql.lower()


def test_the_runtime_dependencies_are_unchanged():
    import tomllib

    declared = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    assert declared == ["httpx==0.28.1", "jsonschema==4.26.0", "psycopg[binary]==3.3.5", "psycopg_pool==3.2.6",
                        "python-dotenv==1.2.3"]


async def test_closing_the_proxy_ends_each_open_tunnel_and_reports_its_bytes():
    """The proxy stopped with its workspace: an open tunnel still reports its bytes (FR-162)."""
    reports: list = []
    async with Upstream() as upstream:
        proxy = _proxy(reports, upstream)
        server, port = await _serve(proxy)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        hello = eg.client_hello("example.com")
        writer.write(_head("example.com:443") + hello)
        await writer.drain()
        await reader.readuntil(CRLF + CRLF)
        await reader.readexactly(len(hello))
        server.close()
        await proxy.close()
        writer.close()
    [end] = [r for r in reports if r["kind"] == "end"]
    assert end["bytes_out"] == len(hello) and end["bytes_in"] == len(hello)


# =================================================================================================
# On a real daemon (the docker gate)
# =================================================================================================


from agentsdk.workspace import DockerProvider  # noqa: E402
from agentsdk.workspace import docker as dk  # noqa: E402
from agentsdk.workspace import owner as ow  # noqa: E402

IMAGE = "python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df"
NO_PYTHON = "redis@sha256:e7723ff73d963f5cc6d9c4643ea3d989527a402a319239054e9472a7fb9219a2"
ALLOW = ("example.com:443", "localtest.me:443")
NL = chr(10)
# Inside the workspace: every way out a command can try, each printed as `<attempt> <outcome>`.
PROBE = NL.join([
    "import os, socket, ssl, sys, urllib.parse, urllib.request",
    "proxy = urllib.parse.urlsplit(os.environ['HTTPS_PROXY'])",
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
    "    ctx = ssl.create_default_context()",
    "    a, b = ssl.MemoryBIO(), ssl.MemoryBIO()",
    "    t = ctx.wrap_bio(a, b, server_hostname=name)",
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
    "for target in sys.argv[1:2]:",
    "    host, port = target.rsplit(':', 1)",
    "    attempt('neighbour ' + target, lambda: socket.create_connection((host, int(port)), timeout=5) and 'connected')",
])


async def _docker_space(tenant: str, egress=ALLOW, image=IMAGE, owner: str | None = None):
    docker = DockerProvider()
    labels = {"agentsdk.workspace": str(uuid.uuid4()), "agentsdk.tenant": tenant, "agentsdk.project": PROJECT}
    if owner is not None:
        labels["agentsdk.owner"] = owner
    return docker, await docker.create(WorkspaceSpec(image=image, egress=egress), labels)


def _left(tenant: str) -> dict[str, list[str]]:
    found = {}
    for kind, listing in (("containers", ["ps", "-aq"]), ("volumes", ["volume", "ls", "-q"]),
                          ("networks", ["network", "ls", "-q"])):
        found[kind] = subprocess.run(["docker", *listing, "--filter", f"label=agentsdk.tenant={tenant}"],
                                     capture_output=True, text=True, timeout=60).stdout.split()
    return found


def _outcomes(stdout: bytes) -> dict[str, str]:
    return {line.split(" ", 1)[0] if not line.startswith("neighbour") else line.rsplit(" ", 2)[0]:
            line.split(" ", 1)[1] if not line.startswith("neighbour") else line.rsplit(" ", 2)[1:]
            for line in stdout.decode().splitlines() if line.strip()}


@pytest.mark.docker
async def test_on_docker_only_the_allowed_pair_leaves_and_every_other_way_is_refused_by_reason():
    tenant = tenancy()
    docker, handle = await _docker_space(tenant)
    neighbour_docker, neighbour = await _docker_space(tenant)
    reports: list = []
    docker.watch_egress(handle, reports.append)
    other = neighbour_docker._spaces[neighbour.workspace_id].address
    try:
        outcome = await docker.run(handle, ["python", "-c", PROBE, f"{other}:{eg.PORT}",
                                            ",".join(eg_addresses("example.com"))], timeout=120,
                                   command_id="cmd-probe")
    finally:
        await docker.remove(handle)
        await neighbour_docker.remove(neighbour)
    out = _outcomes(outcome.stdout)
    assert outcome.exit_code == 0, outcome
    assert out["allowed"].isdigit() and int(out["allowed"]) > 0, out
    for attempt in ("ip_literal", "not_listed", "host_port", "not_global", "not_connect"):
        assert out[attempt].startswith("HTTP/1.1 403"), (attempt, out)
    for attempt in ("sni_mismatch", "second_request"):
        assert out[attempt] == "HTTP/1.1 200 Connection established then closed", (attempt, out)
    for attempt in ("host_direct", "dns"):
        assert out[attempt].startswith("failed"), (attempt, out)
    assert set(out["direct"].split(",")) == {"failed"}, out
    [neighbour_attempt] = [v for k, v in out.items() if k.startswith("neighbour")]
    assert neighbour_attempt[0] == "failed", out
    decisions = [r for r in reports if r["kind"] == "connection"]
    reasons = sorted(r["reason"] for r in decisions)
    assert reasons == sorted(["allowed", "ip_literal", "not_listed", "not_listed", "not_global", "sni_mismatch",
                              "not_connect", "second_request"]), reasons
    assert all(r["command_id"] == "cmd-probe" for r in decisions)
    assert [r for r in reports if r["kind"] == "end" and r["bytes_in"] > 0]
    # Removed, not dead: its stop is no death (F2).
    assert not [r for r in reports if r["kind"] == "ended"], reports
    assert _left(tenant) == {"containers": [], "volumes": [], "networks": []}


@pytest.mark.docker
async def test_on_docker_the_proxy_is_hardened_as_a_command_with_its_own_fixed_limits():
    tenant = tenancy()
    docker, handle = await _docker_space(tenant)
    try:
        inspected = json.loads(subprocess.run(["docker", "inspect", dk.proxy_name(handle.workspace_id)],
                                              capture_output=True, text=True, timeout=60).stdout)[0]
        network = json.loads(subprocess.run(["docker", "network", "inspect", dk.network_name(handle.workspace_id)],
                                             capture_output=True, text=True, timeout=60).stdout)[0]
    finally:
        await docker.remove(handle)
    host = inspected["HostConfig"]
    assert host["CapDrop"] == ["ALL"] and host["ReadonlyRootfs"] is True and "no-new-privileges" in host["SecurityOpt"]
    assert (host["PidsLimit"], host["Memory"], host["MemorySwap"], host["NanoCpus"]) == (
        32, 64 * 2**20, 64 * 2**20, 500_000_000)
    assert host["LogConfig"]["Type"] == "none" and inspected["Config"]["User"] == "65534:65534"
    assert set(inspected["NetworkSettings"]["Networks"]) == {"bridge", dk.network_name(handle.workspace_id)}
    assert network["Internal"] is True and network["Labels"]["agentsdk.tenant"] == tenant
    assert inspected["Config"]["Labels"]["agentsdk.workspace"] == handle.workspace_id


@pytest.mark.docker
async def test_on_docker_a_workspace_without_egress_keeps_no_network_and_no_proxy():
    tenant = tenancy()
    docker, handle = await _docker_space(tenant, egress=())
    program = NL.join([
        "import os, socket",
        "print(os.environ.get('HTTPS_PROXY'))",
        "try:",
        "    socket.create_connection(('1.1.1.1', 443), timeout=5); print('connected')",
        "except OSError as exc:",
        "    print('failed')",
    ])
    try:
        outcome = await docker.run(handle, ["python", "-c", program], timeout=60)
        networks = _left(tenant)["networks"]
    finally:
        await docker.remove(handle)
    # --network none, unchanged (NFR-49): no proxy URL, no network, nothing reached.
    assert outcome.stdout == b"None\nfailed\n" and networks == []


@pytest.mark.docker
async def test_on_docker_an_image_without_python3_fails_the_workspace_and_leaves_nothing():
    tenant = tenancy()
    with pytest.raises(mgr.WorkspaceUnavailable, match="python3"):
        await _docker_space(tenant, image=NO_PYTHON)
    assert _left(tenant) == {"containers": [], "volumes": [], "networks": []}


@pytest.mark.docker
async def test_on_docker_a_tunnel_open_when_its_workspace_goes_reports_its_bytes():
    tenant = tenancy()
    docker, handle = await _docker_space(tenant)
    reports: list = []
    docker.watch_egress(handle, reports.append)
    holder = NL.join([
        "import ssl, socket, os, time, urllib.parse",
        "import urllib.request",
        "r = urllib.request.urlopen('https://example.com/', timeout=20)",
        "print('open', flush=True)",
        "time.sleep(60)",
    ])
    running = asyncio.ensure_future(docker.run(handle, ["python", "-c", holder], timeout=120))
    for _ in range(300):
        if any(r["kind"] == "connection" for r in reports):
            break
        await asyncio.sleep(0.1)
    await asyncio.sleep(1)
    await docker.remove(handle)
    await asyncio.gather(running, return_exceptions=True)
    ends = [r for r in reports if r["kind"] == "end"]
    assert ends and ends[0]["bytes_out"] > 0 and ends[0]["bytes_in"] > 0, reports
    assert _left(tenant) == {"containers": [], "volumes": [], "networks": []}


class OneCall:
    """Runs one command, the probe's own argv, then answers."""

    def __init__(self, argv) -> None:
        self.argv = argv

    async def send(self, request):
        if any(m.role is Role.TOOL for m in request.messages):
            return respond("done")
        return respond(calls=[ToolCall(id="t0", name="run_command", arguments={"argv": self.argv})])


def _docker_runner(store: str, argv=("python", "-c", "print(1)")):
    persistence = Persistence.postgres(DSN, create_schema=False) if store == "postgres" else None
    return Runner({"m": OneCall(list(argv))}, workspace_provider=DockerProvider(), persistence=persistence)


def _agent(keep: bool = False) -> AgentSpec:
    return AgentSpec(id="egress", instructions="x", preferred_model="m:x", tool_profile=("run_command",),
                     workspace=WorkspaceSpec(image=IMAGE, run_command_approval=ApprovalPolicy.AUTO, egress=ALLOW,
                                             keep=keep))


@pytest.mark.docker
@pytest.mark.parametrize("store", STORES)
async def test_on_docker_no_network_or_proxy_remains_after_the_runs_end(store):
    tenant = tenancy()
    fetch = "import urllib.request; print(len(urllib.request.urlopen('https://example.com/', timeout=20).read()))"
    runner = _docker_runner(store, ("python", "-c", fetch))
    result = await runner.run(_agent(), "go", RunConfig(tenant_id=tenant, project_id=PROJECT, max_turns=3))
    left = _left(tenant)
    rows = await asyncio.to_thread(runner._workspace_store().connections, tenant, PROJECT)
    await runner.aclose()
    assert result.status is RunStatus.COMPLETED, result.error
    assert left == {"containers": [], "volumes": [], "networks": []}
    [row] = rows
    assert row.allowed and row.host == "example.com" and row.command_id is not None and row.bytes_in > 0


@pytest.mark.docker
async def test_on_docker_no_network_or_proxy_remains_after_release_or_aclose():
    tenant = tenancy()
    runner = _docker_runner("memory")
    kept = await runner.run(_agent(keep=True), "go", RunConfig(tenant_id=tenant, project_id=PROJECT, max_turns=3))
    [space] = await asyncio.to_thread(runner._workspace_store().workspaces, tenant, PROJECT, kept.run_id)
    standing = _left(tenant)
    assert await runner.release_workspace(tenant, PROJECT, space.workspace_id) is True
    released = _left(tenant)
    await runner.run(_agent(keep=True), "go", RunConfig(tenant_id=tenant, project_id=PROJECT, max_turns=3))
    before_close = _left(tenant)
    await runner.aclose()
    assert standing["networks"] and standing["containers"], standing
    assert released == {"containers": [], "volumes": [], "networks": []}
    assert before_close["networks"] and _left(tenant) == {"containers": [], "volumes": [], "networks": []}


@pytest.mark.docker
async def test_on_docker_a_dead_runners_network_and_proxy_are_reaped():
    tenant = tenancy()
    host, pid, start = ow.this_owner().rsplit(":", 2)
    _, dead = await _docker_space(tenant, owner=f"{host}:{pid}:{int(start) + 1}")
    assert _left(tenant)["networks"]
    runner = _docker_runner("memory")
    await runner._workspaces.reap()
    await runner.aclose()
    assert _left(tenant) == {"containers": [], "volumes": [], "networks": []}, dead.workspace_id


@pytest.mark.docker
async def test_on_docker_a_network_left_without_its_volume_is_still_listed_and_removed():
    tenant = tenancy()
    docker, handle = await _docker_space(tenant)
    await docker._stop_egress(docker._spaces[handle.workspace_id])
    subprocess.run(["docker", "rm", "-f", dk.proxy_name(handle.workspace_id)], capture_output=True, timeout=60)
    subprocess.run(["docker", "volume", "rm", "-f", dk.volume_name(handle.workspace_id)], capture_output=True,
                   timeout=60)
    listed = await DockerProvider().list_owned({"agentsdk.tenant": tenant})
    assert [h.workspace_id for h in listed] == [handle.workspace_id]
    await DockerProvider().remove(listed[0])
    assert _left(tenant) == {"containers": [], "volumes": [], "networks": []}


# =================================================================================================
# FR-163: example 38
# =================================================================================================


EX38_NAME = "38_egress.py"
REFUSED = ("an IP literal, an unlisted host, the host's port, a name resolving to a private address, a ClientHello "
           "naming another host, plain HTTP and a second request were each refused and recorded by reason")


def _example(name: str):
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py").replace("-", "_"), REPO / "scripts" / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def example_38(store: str, provider_name: str = "fake") -> dict:
    checks = await _example(EX38_NAME).offline(store, provider_name, tenant=tenancy(), quiet=True)
    return {label: (passed, why) for label, passed, why in checks}


@pytest.mark.parametrize("store", STORES)
async def test_example_38_holds_every_check_offline(store):
    checks = await example_38(store)
    assert len(checks) >= 5 and REFUSED in checks
    # F8: what a fake cannot fail is checked on Docker only (DECISION-26009e04).
    assert not [label for label in checks if "neighbour" in label or "direct connection" in label
                or "host listener" in label], list(checks)
    assert {label: why for label, (passed, why) in checks.items() if passed is not True} == {}


async def test_example_38s_refusal_check_fails_without_the_proxys_address_check(monkeypatch):
    monkeypatch.setattr(eg, "_global", lambda address: True)
    checks = await example_38("memory")
    assert checks[REFUSED][0] is False, checks[REFUSED]


async def test_example_38_passes_nothing_when_no_command_runs(monkeypatch):
    async def refuses(self, handle, argv, *, timeout, stdin=None, command_id=None):
        raise mgr.WorkspaceUnavailable("no command runs")

    monkeypatch.setattr(FakeProvider, "run", refuses)
    checks = await example_38("memory")
    assert [label for label, (passed, _) in checks.items() if passed is True] == []


def test_example_38_runs_offline_as_a_program():
    done = subprocess.run([sys.executable, str(REPO / "scripts" / EX38_NAME), "--offline"],
                          capture_output=True, text=True, timeout=600, cwd=REPO)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-2000:]
    assert "[FAIL]" not in done.stdout and done.stdout.count("[PASS]") >= 5


def test_example_38_is_listed():
    assert EX38_NAME in (REPO / "README.md").read_text(encoding="utf-8")
    assert f'"{EX38_NAME}"' in (REPO / "tests" / "test_distribution.py").read_text(encoding="utf-8")


@pytest.mark.docker
@pytest.mark.parametrize("store", STORES)
async def test_on_docker_example_38_holds_every_check(store):
    checks = await example_38(store, "docker")
    assert {label: why for label, (passed, why) in checks.items() if passed is not True} == {}


@pytest.mark.docker
def test_example_38_runs_scripted_on_docker():
    done = subprocess.run([sys.executable, str(REPO / "scripts" / EX38_NAME), "--scripted"],
                          capture_output=True, text=True, timeout=900, cwd=REPO)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-2000:]
    assert "[FAIL]" not in done.stdout


async def test_in_a_plan_workspace_each_connection_is_its_own_runs():
    """FR-152, FR-162: two runs of one plan share a workspace and its proxy; each connection is
    recorded under the run whose command made it, not the run that created the workspace."""
    async def one(cmd):
        await cmd.connect("example.com", 443)
        return 0

    tenant, top = tenancy(), str(uuid.uuid4())
    from agentsdk.workspace.records import InMemoryWorkspaceStore

    store = InMemoryWorkspaceStore()
    manager = mgr.WorkspaceManager(FakeProvider(commands={"one": one}, resolve=RESOLVE,
                                                sites={("example.com", 443): SITE}), lambda: store, 4)
    spec = WorkspaceSpec(image=FAKE_IMAGE, scope="plan", egress=("example.com:443",))

    async def call(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def emit(kind, payload):
        return None

    runs = [str(uuid.uuid4()), str(uuid.uuid4())]
    bindings = [manager.for_run(spec, tenant, PROJECT, run_id, emit, call, plan_run_id=top) for run_id in runs]
    for binding in bindings:
        await binding.run(["one"], stdin=None, timeout=5, tool_call_id=None)
    await manager.end_plans(top, emit, call)
    rows = await asyncio.to_thread(manager.store().connections, tenant, PROJECT)
    commands = await asyncio.to_thread(manager.store().commands, tenant, PROJECT)
    by_command = {c.command_id: c.run_id for c in commands}
    assert sorted(r.run_id for r in rows) == sorted(runs)
    assert all(by_command[r.command_id] == r.run_id for r in rows)
    await manager.aclose()


@pytest.mark.docker
async def test_on_docker_a_proxy_stopped_under_an_open_tunnel_reports_its_bytes():
    """The proxy stopped while its client still holds a tunnel open -- as when its container goes
    before the command's: the tunnel's end is still reported, with its bytes (FR-162)."""
    tenant = tenancy()
    docker, handle = await _docker_space(tenant)
    reports: list = []
    docker.watch_egress(handle, reports.append)
    # A TLS session through the proxy, handshake done and then idle: the tunnel stays open.
    holder = NL.join([
        "import os, socket, ssl, time, urllib.parse, base64",
        "p = urllib.parse.urlsplit(os.getenv('HTTPS_PROXY'))",
        "s = socket.create_connection((p.hostname, p.port), timeout=20)",
        "auth = base64.b64encode((p.username + ':' + p.password).encode()).decode()",
        "s.sendall(('CONNECT example.com:443 HTTP/1.1\\r\\nHost: example.com:443\\r\\n'",
        "           'Proxy-Authorization: Basic ' + auth + '\\r\\n\\r\\n').encode())",
        "s.recv(4096)",
        "t = ssl.create_default_context().wrap_socket(s, server_hostname='example.com')",
        "print('open', flush=True)",
        # Well past the wait for the proxy's last reports, so only its stop can end the tunnel.
        "time.sleep(600)",
    ])
    running = asyncio.ensure_future(docker.run(handle, ["python", "-c", holder], timeout=300))
    try:
        for _ in range(300):
            if any(r["kind"] == "connection" for r in reports):
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(1)
        assert not [r for r in reports if r["kind"] == "end"], "the tunnel ended before the proxy stopped"
        stopping = asyncio.get_running_loop().time()
        await docker._stop_egress(docker._spaces[handle.workspace_id])
        stopped_in = asyncio.get_running_loop().time() - stopping
        ends = [r for r in reports if r["kind"] == "end"]
        # Ended by the stop itself, in seconds -- not by the tunnel's 60 s idle limit.
        assert stopped_in < 20, stopped_in
    finally:
        await docker.remove(handle)
        await asyncio.gather(running, return_exceptions=True)
    assert ends and ends[0]["bytes_out"] > 0 and ends[0]["bytes_in"] > 0, reports


# =================================================================================================
# M44 pre-review repairs (DECISION-26009e04)
# =================================================================================================


import socket  # noqa: E402
import struct  # noqa: E402


@pytest.mark.parametrize("value", ["0x5db8d70e:443", "0x7f000001:443", "0x7f.0x1:443", "017700000001:443",
                                   "0x7f.1:443"])
def test_f7_an_address_in_any_form_a_parser_reads_is_refused(value):
    with pytest.raises(ValueError, match="egress"):
        WorkspaceSpec(image=FAKE_IMAGE, egress=(value,))


def _reset(writer) -> None:
    """Closes with an RST, as a client that resets its connection."""
    sock = writer.get_extra_info("socket")
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    writer.transport.abort()


@pytest.mark.parametrize("stage", ["partial head", "after the head", "after part of the hello"])
async def test_f3_a_connection_its_client_resets_is_reported_client_reset(stage):
    """The reset reaches the proxy as ConnectionResetError on Linux, where it runs; the reader here
    raises it at each stage, on every platform alike."""
    from agentsdk.workspace import _Sink

    reports: list = []
    upstream_dials: list = []

    async def connect(address, port):
        upstream_dials.append(address)
        raise AssertionError("no upstream for a reset connection")

    proxy = eg.Proxy(("example.com:443",), report=reports.append, connect=connect,
                     resolve=lambda host, port: asyncio.sleep(0, result=["93.184.215.14"]))
    reader = asyncio.StreamReader()
    if stage == "partial head":
        reader.feed_data(b"CONNECT example.com:443 HTTP/1.1" + CRLF + b"Host: exa")
    else:
        reader.feed_data(_head("example.com:443"))
        if stage == "after part of the hello":
            reader.feed_data(eg.client_hello("example.com")[:20])
    reader.set_exception(ConnectionResetError("reset by its client"))
    await proxy.handle(reader, _Sink())
    assert [(d["allowed"], d["reason"]) for d in _decisions(reports)] == [(False, "client_reset")], reports
    assert upstream_dials == []


async def test_f3_a_failure_of_the_proxys_own_is_reported_proxy_error():
    reports: list = []

    async def broken(host, port):
        raise RuntimeError("the proxy's own failure")

    proxy = eg.Proxy(("example.com:443",), report=reports.append, resolve=broken)
    server, port = await _serve(proxy)
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(_head("example.com:443"))
    await writer.drain()
    await asyncio.wait_for(reader.read(), 5)
    writer.close()
    await _settle(reports, 1)
    server.close()
    assert [(d["allowed"], d["reason"]) for d in _decisions(reports)] == [(False, "proxy_error")]


async def test_f2_past_64_connections_waiting_on_their_head_a_new_one_is_refused_busy():
    reports: list = []
    proxy = _proxy(reports, None, max_waiting=2)
    server, port = await _serve(proxy)
    waiting = []
    for _ in range(2):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"CONNECT example.com:443 HTTP/1.1\r\nX-Pad: " + b"x" * 4000)
        await writer.drain()
        waiting.append(writer)
    await asyncio.sleep(0.3)
    status, _ = await _ask(port, _head("example.org:443"))
    await _settle(reports, 1)
    for writer in waiting:
        writer.close()
    server.close()
    assert status.startswith(b"HTTP/1.1 403")
    assert [d["reason"] for d in _decisions(reports)][:1] == ["busy"]
    assert eg.Proxy(("example.com:443",), report=print).max_waiting == 64


def test_f6_the_proxy_runs_on_python_3_8_and_names_its_version_when_listening():
    tree = ast.parse(eg.SOURCE, feature_version=(3, 8))
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.Attribute) and node.attr == "timeout"
                and isinstance(node.value, ast.Name) and node.value.id == "asyncio"]
    assert dk.ready_refusal(b'{"kind": "listening", "python": [3, 8], "port": 3128}') is None
    for line in (b'{"kind": "listening", "python": [3, 7], "port": 3128}', b'{"kind": "listening", "port": 3128}',
                 b"", b"Traceback (most recent call last):", b'{"kind": "connection"}', b"[1, 2]"):
        assert "python3" in dk.ready_refusal(line), line


def test_f6_and_f5_the_program_listens_on_the_address_it_was_given_a_subnet_for():
    config = json.dumps({"allow": ["example.com:443"], "port": 0, "subnet": "127.0.0.0/8"})
    process = subprocess.Popen([sys.executable, "-I", "-c", eg.SOURCE, config], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    try:
        ready = json.loads(process.stdout.readline())
    finally:
        process.kill()
        process.wait(10)
    assert ready["kind"] == "listening" and ready["address"] == "127.0.0.1"
    assert ready["python"] == list(sys.version_info[:2])


@pytest.mark.parametrize("version, refused", [("27.5.1", True), ("28.0.0", False), ("29.6.2", False),
                                              ("", True), ("garbage", True), ("28.0.0-rc.1", False)])
def test_f1_an_engine_older_than_28_is_refused_by_version(version, refused):
    refusal = dk.engine_refusal(version)
    assert (refusal is not None) is refused
    if refused:
        assert "28" in refusal


async def test_f4_a_finished_commands_token_names_no_command():
    async def leaves(cmd):
        cmd.files.write("token", cmd.egress_token)
        await cmd.connect("example.com", 443)
        return 0

    async def borrows(cmd):
        await cmd.connect("example.org", 443, token=cmd.files.read("token").decode())
        return 0

    tenant = tenancy()
    provider = FakeProvider(commands={"leaves": leaves, "borrows": borrows}, resolve=RESOLVE,
                            sites={("example.com", 443): SITE})
    runner = Runner({"m": Turns()}, workspace_provider=provider)
    agent = AgentSpec(id="t", instructions="x", preferred_model="m:x", tool_profile=("run_command",),
                      workspace=WorkspaceSpec(image=FAKE_IMAGE, run_command_approval=ApprovalPolicy.AUTO,
                                              egress=("example.com:443",)))
    await runner.run(agent, json.dumps([[["leaves"]], [["borrows"]]]),
                     RunConfig(tenant_id=tenant, project_id=PROJECT, max_turns=4))
    rows = await asyncio.to_thread(runner._workspace_store().connections, tenant, PROJECT)
    commands = await asyncio.to_thread(runner._workspace_store().commands, tenant, PROJECT)
    await runner.aclose()
    first = next(c.command_id for c in commands if c.argv == ["leaves"])
    assert sorted((r.reason, r.command_id) for r in rows) == sorted([("allowed", first), ("not_listed", None)])


async def test_f2_a_proxy_that_dies_is_recorded_and_its_workspace_refuses_later_commands():
    async def fetch(cmd):
        await cmd.connect("example.com", 443)
        return 0

    tenant = tenancy()
    provider = FakeProvider(commands={"fetch": fetch}, resolve=RESOLVE, sites={("example.com", 443): SITE})

    class KillsBetween(Turns):
        async def send(self, request):
            if sum(1 for m in request.messages if m.role is Role.ASSISTANT) == 1:
                for handle in await provider.list_owned({"agentsdk.tenant": tenant}):
                    provider.kill_proxy(handle)
                await asyncio.sleep(0.1)
            return await super().send(request)

    runner = Runner({"m": KillsBetween()}, workspace_provider=provider)
    agent = AgentSpec(id="d", instructions="x", preferred_model="m:x", tool_profile=("run_command",),
                      workspace=WorkspaceSpec(image=FAKE_IMAGE, run_command_approval=ApprovalPolicy.AUTO,
                                              egress=("example.com:443",)))
    result = await runner.run(agent, json.dumps([[["fetch"]], [["fetch"]]]),
                              RunConfig(tenant_id=tenant, project_id=PROJECT, max_turns=4))
    rows = await asyncio.to_thread(runner._workspace_store().connections, tenant, PROJECT)
    commands = await asyncio.to_thread(runner._workspace_store().commands, tenant, PROJECT)
    await runner.aclose()
    assert sorted(r.reason for r in rows) == ["allowed", "proxy_ended"]
    assert [e.payload["reason"] for e in result.events if e.event_type is EventType.WORKSPACE_CONNECTION] == [
        "allowed", "proxy_ended"]
    assert len(commands) == 1, "no command starts once its workspace's proxy is gone"
    refused = [e.payload for e in result.events if e.event_type is EventType.TOOL_CALLED][-1]
    # A workspace's failure reaches the model as ToolExecutionError, as every one does (README, M42).
    assert refused["is_error"] and refused.get("error_type") == "ToolExecutionError", refused


@pytest.mark.docker
async def test_on_docker_f1_nothing_on_the_workspaces_network_but_its_proxy_answers_a_command():
    """F1: with its gateway isolated the network holds no address of the host -- the first container,
    its proxy, may take .1. The first eight addresses: no host listener open anywhere (rpcbind, ssh,
    the docker API, the database), and the proxy's port only at the proxy's own address."""
    tenant = tenancy()
    docker, handle = await _docker_space(tenant)
    space = docker._spaces[handle.workspace_id]
    prefix = space.address.rsplit(".", 1)[0]
    scan = NL.join([
        "import socket, sys",
        "for last in range(1, 9):",
        "    address = sys.argv[1] + '.' + str(last)",
        "    for port in (111, 22, 2375, 5432, 3128):",
        "        s = socket.socket(); s.settimeout(1)",
        "        try:",
        "            s.connect((address, port)); print(address, port, 'OPEN')",
        "        except OSError:",
        "            pass",
        "        finally:",
        "            s.close()",
        "print('scanned')",
    ])
    try:
        outcome = await docker.run(handle, ["python", "-c", scan, prefix], timeout=300)
        options = json.loads(subprocess.run(["docker", "network", "inspect", dk.network_name(handle.workspace_id)],
                                            capture_output=True, text=True, timeout=60).stdout)[0]["Options"]
    finally:
        await docker.remove(handle)
    lines = outcome.stdout.decode().splitlines()
    assert lines[-1:] == ["scanned"], outcome
    assert [line for line in lines if "OPEN" in line] == [f"{space.address} 3128 OPEN"], lines
    assert options.get("com.docker.network.bridge.gateway_mode_ipv4") == "isolated"


@pytest.mark.docker
async def test_on_docker_f5_a_container_on_the_bridge_cannot_reach_the_proxy():
    tenant = tenancy()
    docker, handle = await _docker_space(tenant)
    bridge_address = json.loads(subprocess.run(
        ["docker", "inspect", "--format", "{{json .NetworkSettings.Networks}}", dk.proxy_name(handle.workspace_id)],
        capture_output=True, text=True, timeout=60).stdout)["bridge"]["IPAddress"]
    reports: list = []
    docker.watch_egress(handle, reports.append)
    attempt = NL.join([
        "import socket, sys",
        "try:",
        "    s = socket.create_connection((sys.argv[1], 3128), timeout=5); print('connected')",
        "except OSError as exc:",
        "    print('failed', type(exc).__name__)",
    ])
    try:
        done = subprocess.run(["docker", "run", "--rm", "--label", f"agentsdk.tenant={tenant}", "--entrypoint=", IMAGE,
                               "python", "-c", attempt, bridge_address], capture_output=True, text=True, timeout=120)
    finally:
        await docker.remove(handle)
    assert done.stdout.startswith("failed"), done.stdout + done.stderr
    assert reports == []


@pytest.mark.docker
async def test_on_docker_f4_a_finished_commands_token_names_no_command():
    tenant = tenancy()
    docker, handle = await _docker_space(tenant)
    reports: list = []
    docker.watch_egress(handle, reports.append)
    keep = "import os; open('token', 'w').write(os.getenv('HTTPS_PROXY'))"
    borrow = NL.join([
        "import base64, socket, urllib.parse",
        "p = urllib.parse.urlsplit(open('token').read())",
        "auth = base64.b64encode((p.username + ':' + p.password).encode()).decode()",
        "s = socket.create_connection((p.hostname, p.port), timeout=15)",
        "s.sendall(('CONNECT example.org:443 HTTP/1.1\\r\\nHost: example.org:443\\r\\n'",
        "           'Proxy-Authorization: Basic ' + auth + '\\r\\n\\r\\n').encode())",
        "print(s.recv(4096).split(b'\\r\\n')[0])",
    ])
    try:
        await docker.run(handle, ["python", "-c", keep], timeout=60, command_id="cmd-a")
        await docker.run(handle, ["python", "-c", borrow], timeout=60, command_id="cmd-b")
    finally:
        await docker.remove(handle)
    [decision] = [r for r in reports if r["kind"] == "connection"]
    assert (decision["reason"], decision["command_id"]) == ("not_listed", None)


@pytest.mark.docker
async def test_on_docker_f2_a_proxy_that_dies_is_reported_ended():
    tenant = tenancy()
    docker, handle = await _docker_space(tenant)
    reports: list = []
    docker.watch_egress(handle, reports.append)
    try:
        subprocess.run(["docker", "kill", dk.proxy_name(handle.workspace_id)], capture_output=True, timeout=60)
        for _ in range(100):
            if reports:
                break
            await asyncio.sleep(0.1)
    finally:
        await docker.remove(handle)
    assert [r["kind"] for r in reports] == ["ended"], reports


# =================================================================================================
# M44 round 1 repairs (DECISION-a7a1b37b)
# =================================================================================================


class _Recording(Runner):
    """Keeps every run's event sink, so events written after a run's end are seen too."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.sinks: dict[str, object] = {}

    def _open(self, spec, config):
        opened = super()._open(spec, config)
        self.sinks[opened.scope.run_id] = opened.events
        return opened


def _slow_rows(monkeypatch, seconds: float) -> None:
    from agentsdk.workspace import records

    real = records.InMemoryWorkspaceStore.record_connection

    def slow(self, record):
        import time
        time.sleep(seconds)
        return real(self, record)

    monkeypatch.setattr(records.InMemoryWorkspaceStore, "record_connection", slow)


async def test_r1_f1_a_report_backlog_past_the_bound_never_hangs_the_run(monkeypatch):
    """F1: the recorder writes through RunControl.store, which absorbs a cancel so its write lands;
    so it is never stopped by cancel. A backlog past the bound ends the run all the same."""
    monkeypatch.setattr(mgr, "_REMOVE_SECONDS", 0.5)
    _slow_rows(monkeypatch, 0.2)  # 12 connections, 2.4 s of writing against a 0.5 s bound
    run = asyncio.ensure_future(_egress_run("memory", [[["reach"], ["reach"]]]))
    done, _ = await asyncio.wait({run}, timeout=20)
    if not done:
        run.cancel()
    assert done, "the run never ended: its workspace's removal waited on its recorder forever"
    result, rows, _ = run.result()
    assert result.status is RunStatus.COMPLETED and 0 < len(rows) <= 12


async def test_r1_f1_a_backlog_within_the_bound_is_recorded_whole_and_the_recorder_ends(monkeypatch):
    _slow_rows(monkeypatch, 0.05)
    tenant = tenancy()
    provider = FakeProvider(commands={"reach": _reach}, resolve=RESOLVE, sites={("example.com", 443): SITE})
    runner = Runner({"m": Turns()}, workspace_provider=provider)
    agent = AgentSpec(id="r", instructions="x", preferred_model="m:x", tool_profile=("run_command",),
                      workspace=WorkspaceSpec(image=FAKE_IMAGE, run_command_approval=ApprovalPolicy.AUTO,
                                              egress=("example.com:443", "private.example.com:443"), keep=True))
    await runner.run(agent, json.dumps([[["reach"]]]), RunConfig(tenant_id=tenant, project_id=PROJECT, max_turns=4))
    [space] = list(runner._workspaces.spaces)
    await runner.aclose()
    rows = await asyncio.to_thread(runner._workspace_store().connections, tenant, PROJECT)
    assert len(rows) == 6 and space.recorder.done() and not space.recorder.cancelled()


async def test_r1_f1_a_recorder_mid_write_when_the_bound_passes_is_told_to_stop_never_cancelled(monkeypatch):
    """The pattern itself: a task writing through RunControl.store stops at its marker, or after its
    current write once told to, and the wait on it is bounded either way."""
    from agentsdk.handle import RunControl

    monkeypatch.setattr(mgr, "_REMOVE_SECONDS", 0.3)
    _slow_rows(monkeypatch, 0.5)
    control = RunControl()

    async def emit(*args, **kwargs):
        return None

    space = mgr._Space(WorkspaceSpec(image=FAKE_IMAGE, egress=("example.com:443",)), tenancy(), PROJECT, "run")
    space.reports = asyncio.Queue()
    space.owner = mgr._Binding("run", emit, control.store)
    from agentsdk.workspace.records import InMemoryWorkspaceStore

    rows = InMemoryWorkspaceStore()
    manager = mgr.WorkspaceManager(FakeProvider(), lambda: rows, 1)
    handle = WorkspaceHandle(workspace_id="w", spec=space.spec, labels={})
    space.recorder = asyncio.ensure_future(manager.book(space, handle))
    for i in range(10):
        space.reports.put_nowait({"kind": "connection", "id": str(i), "host": "example.com", "port": 443,
                                  "allowed": True, "reason": "allowed", "at": None})
    booked = asyncio.ensure_future(manager._booked(space))
    done, _ = await asyncio.wait({booked}, timeout=5)
    assert done, "_booked never returned"
    await asyncio.wait({space.recorder}, timeout=2)
    assert space.recorder.done() and not space.recorder.cancelled()


async def test_r1_f3_a_kept_workspaces_connection_after_its_run_ended_is_a_row_with_no_event():
    tenant = tenancy()
    provider = FakeProvider(commands={"reach": _reach}, resolve=RESOLVE, sites={("example.com", 443): SITE})
    runner = _Recording({"m": Turns()}, workspace_provider=provider)
    agent = AgentSpec(id="k", instructions="x", preferred_model="m:x", tool_profile=("run_command",),
                      workspace=WorkspaceSpec(image=FAKE_IMAGE, run_command_approval=ApprovalPolicy.AUTO,
                                              egress=("example.com:443",), keep=True))
    result = await runner.run(agent, json.dumps([[["reach"]]]),
                              RunConfig(tenant_id=tenant, project_id=PROJECT, max_turns=4))
    during = len(await asyncio.to_thread(runner._workspace_store().connections, tenant, PROJECT))
    # The kept workspace's proxy reports after its run's terminal event: its death, here.
    for handle in await provider.list_owned({"agentsdk.tenant": tenant}):
        provider.kill_proxy(handle)
    for _ in range(50):
        rows = await asyncio.to_thread(runner._workspace_store().connections, tenant, PROJECT)
        if len(rows) > during:
            break
        await asyncio.sleep(0.05)
    events = [e.event_type for e in runner.sinks[result.run_id].events()]
    await runner.aclose()
    assert [r.reason for r in rows if r.reason == "proxy_ended"] == ["proxy_ended"]
    terminal = events.index(EventType.RUN_COMPLETED)
    assert terminal == len(events) - 1, f"events after the run's terminal event: {events[terminal + 1:]}"
    assert events.count(EventType.WORKSPACE_CONNECTION) == during


async def test_r1_f3_connections_drained_at_removal_still_have_their_events(monkeypatch):
    """Removal drains before the terminal event: those reports keep their events."""
    _slow_rows(monkeypatch, 0.1)
    result, rows, _ = await _egress_run("memory", [[["reach"]]])
    assert len(rows) == 6 and len(_connection_events(result)) == 6


async def test_r1_f4_the_limit_event_marks_the_limit_reached_and_the_row_carries_the_count():
    result, rows, _ = await _egress_run("memory", [[["reach"]], [["reach"]]], max_connections=4)
    [limit] = [r for r in rows if r.reason == "connection_limit"]
    [event] = [e for e in _connection_events(result) if e["reason"] == "connection_limit"]
    assert limit.count == 8 and "count" not in event, event


def _maximal_egress() -> tuple[str, ...]:
    labels = ["a" * 63, "b" * 63, "c" * 63]
    return tuple(".".join([*labels, f"d{i:02d}" + "e" * 58]) + ":65535" for i in range(32))


def test_r1_f5_a_maximal_egress_spec_fits_the_proxys_command_line_with_room():
    egress = _maximal_egress()
    assert len(egress) == eg.MAX_ENTRIES and all(len(e.rpartition(":")[0]) == 253 for e in egress)
    spec = WorkspaceSpec(image=IMAGE, egress=egress)
    labels = {"agentsdk.workspace": str(uuid.uuid4()), "agentsdk.tenant": "t" * 64, "agentsdk.project": "p" * 64,
              "agentsdk.spec": json.dumps(spec.record())}
    handle = WorkspaceHandle(workspace_id=labels["agentsdk.workspace"], spec=spec, labels=labels)
    args = dk.proxy_args(handle, "172.30.0.0/16")
    assert not any(a.startswith("agentsdk.spec=") for a in args), "the spec label rides on the proxy's line"
    import subprocess as sp
    assert len(sp.list2cmdline(["docker", *args])) < 32_767 - 2048


async def test_r1_f5_a_proxy_line_past_the_bound_is_workspace_unavailable(monkeypatch):
    calls: list = []

    async def cli(self, *args, **kwargs):
        calls.append(args[:2])
        if args[:2] == ("version", "--format"):
            return 0, b"29.6.2", b""
        if args[:3] == ("network", "inspect", "--format"):
            return 0, b"172.30.0.0/16", b""
        return 0, b"", b""

    monkeypatch.setattr(dk.DockerProvider, "_cli", cli)
    monkeypatch.setattr(dk, "_too_long", lambda args: "python3" in args)
    provider = dk.DockerProvider()
    spec = WorkspaceSpec(image=IMAGE, egress=_maximal_egress())
    handle = WorkspaceHandle(workspace_id=str(uuid.uuid4()), spec=spec, labels={})
    space = dk._Space(handle=handle)
    with pytest.raises(mgr.WorkspaceUnavailable, match="command line"):
        await provider._start_egress(handle, space)
    assert ("create", "--pull") not in calls


def test_r1_f6_a_port_of_more_than_five_digits_is_refused_by_name():
    with pytest.raises(ValueError, match="port from 1 to 65535"):
        eg.parse_egress(("example.com:" + "4" * 4301,))


async def test_r1_f6_a_request_naming_a_port_of_4301_digits_is_malformed():
    reports: list = []
    server, port = await _serve(_proxy(reports, None))
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    # One line, so the head stays inside HEAD_BYTES and reaches the port's own check.
    head = b"CONNECT example.com:" + b"4" * 4301 + b" HTTP/1.1" + CRLF + CRLF
    assert len(head) < eg.HEAD_BYTES
    writer.write(head)
    await writer.drain()
    answer = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    await _settle(reports, 1)
    server.close()
    assert answer.startswith(b"HTTP/1.1 403")
    assert [r["reason"] for r in reports if r["kind"] == "connection"] == ["malformed"], reports


async def test_r1_f7_a_kept_workspace_forgets_each_command_once_its_reports_are_booked():
    tenant = tenancy()
    provider = FakeProvider(commands={"reach": _reach}, resolve=RESOLVE, sites={("example.com", 443): SITE})
    runner = Runner({"m": Turns()}, workspace_provider=provider)
    agent = AgentSpec(id="f", instructions="x", preferred_model="m:x", tool_profile=("run_command",),
                      workspace=WorkspaceSpec(image=FAKE_IMAGE, run_command_approval=ApprovalPolicy.AUTO,
                                              egress=("example.com:443",), keep=True))
    await runner.run(agent, json.dumps([[["reach"]], [["reach"]], [["reach"]]]),
                     RunConfig(tenant_id=tenant, project_id=PROJECT, max_turns=6))
    [space] = list(runner._workspaces.spaces)
    for _ in range(50):
        if not space.commands and space.reports.empty():
            break
        await asyncio.sleep(0.02)
    left = dict(space.commands)
    rows = await asyncio.to_thread(runner._workspace_store().connections, tenant, PROJECT)
    commands = await asyncio.to_thread(runner._workspace_store().commands, tenant, PROJECT)
    await runner.aclose()
    assert left == {}, "a finished command is still held by its workspace"
    # Forgotten only after its reports: every row still names the command that made it.
    assert {r.command_id for r in rows} == {c.command_id for c in commands}


@pytest.mark.docker
async def test_on_docker_r1_f7_a_proxy_writing_past_a_pipes_buffer_to_stderr_still_reports(monkeypatch):
    """F7: the proxy's stderr is drained. 256 KiB written there first would fill an undrained pipe
    and stall the docker client, and its ready line with it."""
    future = "from __future__ import annotations" + NL
    assert eg.SOURCE.count(future) == 1
    monkeypatch.setattr(eg, "SOURCE", eg.SOURCE.replace(future, future + "import sys" + NL
                                                         + "sys.stderr.write('x' * 262144)" + NL
                                                         + "sys.stderr.flush()" + NL))
    tenant = tenancy()
    docker, handle = await _docker_space(tenant)
    reports: list = []
    docker.watch_egress(handle, reports.append)
    try:
        outcome = await docker.run(handle, ["python", "-c", NL.join([
            "import urllib.request",
            "print(len(urllib.request.urlopen('https://example.com/', timeout=20).read()))"])], timeout=120)
    finally:
        await docker.remove(handle)
    assert outcome.exit_code == 0, outcome
    assert [r["reason"] for r in reports if r["kind"] == "connection"] == ["allowed"], reports


def _host_reaches(address: str) -> bool:
    import socket
    try:
        socket.create_connection((address, 443), timeout=10).close()
        return True
    except OSError:
        return False


def _bridge_reaches(tenant: str, address: str) -> str:
    """A container with no isolation at all, on the default bridge: the control for a direct check."""
    program = NL.join([
        "import socket",
        "try:",
        f"    socket.create_connection(({address!r}, 443), timeout=10); print('connected')",
        "except OSError as exc:",
        "    print('failed', type(exc).__name__)"])
    done = subprocess.run(["docker", "run", "--rm", "--pull", "never", "--network", "bridge",
                           "--label", f"agentsdk.tenant={tenant}", "--entrypoint", "python", IMAGE, "-c", program],
                          capture_output=True, text=True, timeout=120)
    return done.stdout.strip()


@pytest.mark.docker
async def test_on_docker_r1_f2_the_direct_attempt_dials_the_address_the_proxy_reached_which_is_live():
    """F2: the direct attempt dials the very address the allowed connection reached through the
    proxy, and a container with no isolation reaches it: the failure inside is the isolation's."""
    tenant = tenancy()
    docker, handle = await _docker_space(tenant)
    neighbour_docker, neighbour = await _docker_space(tenant)
    reports: list = []
    docker.watch_egress(handle, reports.append)
    other = neighbour_docker._spaces[neighbour.workspace_id].address
    targets = eg_addresses("example.com")
    try:
        outcome = await docker.run(handle, ["python", "-c", PROBE, f"{other}:{eg.PORT}", ",".join(targets)],
                                   timeout=120)
    finally:
        await docker.remove(handle)
        await neighbour_docker.remove(neighbour)
    out = _outcomes(outcome.stdout)
    [allowed] = [r for r in reports if r["kind"] == "connection" and r["allowed"]]
    assert allowed["address"] in targets, (allowed["address"], targets)
    assert out["direct"] and all(part == "failed" for part in out["direct"].split(",")), out["direct"]
    assert _bridge_reaches(tenant, allowed["address"]) == "connected"
    assert _host_reaches(allowed["address"])


def eg_addresses(name: str) -> list[str]:
    """Every IPv4 address `name` resolves to on the host, as the examples dial them."""
    import socket
    return sorted({info[4][0] for info in socket.getaddrinfo(name, 443, socket.AF_INET, socket.SOCK_STREAM)})


@pytest.mark.docker
async def test_on_docker_r1_f2_example_38s_direct_check_fails_on_a_network_that_is_not_isolated(monkeypatch):
    """The check itself, made to fail: the workspace's network an ordinary one, with a gateway out."""
    real = dk.DockerProvider._cli

    async def ordinary(self, *args, **kwargs):
        if args[:2] == ("network", "create"):
            args = tuple(a for a in args if a not in ("--internal", "-o")
                         and not a.startswith("com.docker.network.bridge.gateway_mode"))
        return await real(self, *args, **kwargs)

    monkeypatch.setattr(dk.DockerProvider, "_cli", ordinary)
    checks = await example_38("memory", "docker")
    [direct] = [label for label in checks if "direct connection" in label]
    # Failed by the direct attempt itself, not only by the host's port, which also opens here.
    assert checks[direct][0] is False and checks[direct][1].startswith("direct: "), checks[direct]


@pytest.mark.docker
@pytest.mark.parametrize("without", ["the address dialled", "the host's control"])
async def test_on_docker_r1_f2_example_38s_direct_check_fails_without_its_control(monkeypatch, without):
    """No pass from a dead or a different address: the direct check fails when the address the proxy
    reached was not dialled, and when the host cannot reach it either."""
    module = _example(EX38_NAME)
    if without == "the address dialled":
        monkeypatch.setattr(module, "direct_targets", lambda chosen: ["192.0.2.1"])  # TEST-NET-1
    else:
        monkeypatch.setattr(module, "host_reaches", lambda address: False)
    checks = {label: (passed, why) for label, passed, why in
              await module.offline("memory", "docker", tenant=tenancy(), quiet=True)}
    [direct] = [label for label in checks if "direct connection" in label]
    assert checks[direct][0] is False, checks[direct]


# =================================================================================================
# M44 round 2 repairs (DECISION-e510a5e3)
# =================================================================================================


async def test_r2_reports_left_past_the_bound_are_counted_in_one_unrecorded_row(monkeypatch):
    """Every connection recorded individually, or counted in the one `unrecorded` row."""
    from agentsdk.handle import RunControl
    from agentsdk.workspace.records import InMemoryWorkspaceStore

    monkeypatch.setattr(mgr, "_REMOVE_SECONDS", 0.3)
    _slow_rows(monkeypatch, 0.2)

    async def emit(*args, **kwargs):
        return None

    tenant = tenancy()
    space = mgr._Space(WorkspaceSpec(image=FAKE_IMAGE, egress=("example.com:443",)), tenant, PROJECT, "run")
    space.reports = asyncio.Queue()
    space.owner = mgr._Binding("run", emit, RunControl().store)
    from agentsdk.workspace.records import WorkspaceRecord, now

    rows = InMemoryWorkspaceStore()
    rows.record_workspace(WorkspaceRecord(workspace_id="w", tenant_id=tenant, project_id=PROJECT, run_id="run",
                                          scope="run", provider="fake", image=FAKE_IMAGE, limits={},
                                          created_at=now()))
    manager = mgr.WorkspaceManager(FakeProvider(), lambda: rows, 1)
    handle = WorkspaceHandle(workspace_id="w", spec=space.spec, labels={})
    space.recorder = asyncio.ensure_future(manager.book(space, handle))
    for i in range(20):
        space.reports.put_nowait({"kind": "connection", "id": str(uuid.uuid4()), "host": "example.com",
                                  "port": 443, "allowed": True, "reason": "allowed", "at": None})
        space.reports.put_nowait({"kind": "end", "id": "x", "bytes_out": 1, "bytes_in": 1})
    space.reports.put_nowait({"kind": "limit", "count": 5, "at": None})  # 5 refusals no row counts yet
    await manager._booked(space)
    await asyncio.wait({space.recorder}, timeout=3)
    written = rows.connections(tenant, PROJECT)
    [unrecorded] = [r for r in written if r.reason == "unrecorded"]
    recorded = [r for r in written if r.reason != "unrecorded"]
    # A tunnel's end is no connection: only the connections left are counted.
    assert 0 < len(recorded) < 20 and unrecorded.count == 20 - len(recorded) + 5, (len(recorded), unrecorded.count)
    assert unrecorded.run_id == "run" and not unrecorded.allowed and space.recorder.done()


async def test_r2_the_store_takes_an_unrecorded_row_with_its_count():
    from agentsdk.workspace.records import ConnectionRecord, now

    result, rows, _ = await _egress_run("postgres", [[["reach"]]])
    tenant, workspace_id, run_id = rows[0].tenant_id, rows[0].workspace_id, result.run_id
    before = {r.connection_id for r in rows}

    def write_and_read() -> list:
        store = Persistence.postgres(DSN, create_schema=False).workspace_store()
        for reason, count in (("unrecorded", 7), ("connection_limit", 3)):
            store.record_connection(ConnectionRecord(
                connection_id=str(uuid.uuid4()), workspace_id=workspace_id, tenant_id=tenant, project_id=PROJECT,
                run_id=run_id, command_id=None, host=None, port=None, address=None, allowed=False, reason=reason,
                at=now(), count=count))
        with pytest.raises(Exception):  # a count on any other row is still refused
            store.record_connection(ConnectionRecord(
                connection_id=str(uuid.uuid4()), workspace_id=workspace_id, tenant_id=tenant, project_id=PROJECT,
                run_id=run_id, command_id=None, host="example.com", port=443, address=None, allowed=False,
                reason="not_listed", at=now(), count=2))
        return store.connections(tenant, PROJECT)

    written = await asyncio.to_thread(write_and_read)
    assert sorted((r.reason, r.count) for r in written if r.connection_id not in before) == [
        ("connection_limit", 3), ("unrecorded", 7)]


async def test_r2_past_the_limit_the_count_is_reported_at_most_once_a_second_and_when_the_proxy_stops():
    reports: list = []
    proxy = _proxy(reports, None, max_connections=1)
    server, port = await _serve(proxy)
    for _ in range(40):
        await _ask(port, _head("example.org:443"))
    await asyncio.sleep(0.2)
    within = [r["count"] for r in reports if r["kind"] == "limit"]
    await asyncio.sleep(1.2)
    later = [r["count"] for r in reports if r["kind"] == "limit"]
    for _ in range(5):
        await _ask(port, _head("example.org:443"))
    await proxy.close()
    server.close()
    final = [r["count"] for r in reports if r["kind"] == "limit"]
    assert within == [1], within  # the first refusal at once, and nothing more within the second
    assert later == [1, 39], later  # then the count, once the second is up
    assert final[-1] == 44 and len(final) <= 4, final  # and the final count when the proxy stops


async def test_r2_a_connection_undecided_when_the_proxy_stops_is_reported_stopped():
    reports: list = []
    proxy = _proxy(reports, None)
    server, port = await _serve(proxy)
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"CONNECT exam")  # a head never finished
    await writer.drain()
    for _ in range(50):
        if proxy.connections:
            break
        await asyncio.sleep(0.02)
    await proxy.close()
    writer.close()
    server.close()
    assert [(r["kind"], r["reason"]) for r in reports] == [("connection", "stopped")], reports


async def test_r2_on_the_fake_a_workspaces_removal_stops_its_proxy_and_the_last_count_is_recorded():
    result, rows, _ = await _egress_run("memory", [[["reach"]], [["reach"]]], max_connections=4)
    [limit] = [r for r in rows if r.reason == "connection_limit"]
    assert limit.count == 8


async def test_r2_a_connection_of_no_command_in_a_plan_workspace_is_the_top_level_runs():
    async def stranger(cmd):
        await cmd.connect("example.com", 443, token="not-a-command's")
        return 0

    tenant, top = tenancy(), str(uuid.uuid4())
    from agentsdk.workspace.records import InMemoryWorkspaceStore

    store = InMemoryWorkspaceStore()
    manager = mgr.WorkspaceManager(FakeProvider(commands={"stranger": stranger}, resolve=RESOLVE,
                                                sites={("example.com", 443): SITE}), lambda: store, 4)
    spec = WorkspaceSpec(image=FAKE_IMAGE, scope="plan", egress=("example.com:443",))

    async def call(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    emitted: dict[str, list] = {"top": [], "child": []}

    def emitter(name):
        async def emit(kind, payload):
            emitted[name].append(kind)
        return emit

    child = str(uuid.uuid4())
    binding = manager.for_run(spec, tenant, PROJECT, child, emitter("child"), call, plan_run_id=top,
                              top=(emitter("top"), call))
    await binding.run(["stranger"], stdin=None, timeout=5, tool_call_id=None)
    await binding.end("run_ended", emitter("child"))
    await manager.end_plans(top, emitter("top"), call)
    rows = await asyncio.to_thread(store.connections, tenant, PROJECT)
    await manager.aclose()
    assert [(r.run_id, r.command_id) for r in rows] == [(top, None)]
    assert emitted["top"].count(EventType.WORKSPACE_CONNECTION) == 1
    assert EventType.WORKSPACE_CONNECTION not in emitted["child"]


async def test_r2_on_a_runner_a_plan_childs_connection_of_no_command_is_the_top_level_runs():
    """Through the Runner: the child's connection with no command is the plan's top-level run's,
    row and event both, and no event lands in the child's stream."""
    ex = _example(EX38_NAME)

    async def stranger(cmd):
        await cmd.connect("example.com", 443, token="nobody's")
        return 0

    tenant = tenancy()
    provider = FakeProvider(commands={"stranger": stranger}, resolve=RESOLVE, sites={("example.com", 443): SITE})
    role = AgentSpec(id="prober", instructions="CALLS: " + json.dumps([["run_command", {"argv": ["stranger"]}]]),
                     preferred_model="scripted:model", tool_profile=("run_command",),
                     workspace=WorkspaceSpec(image=FAKE_IMAGE, run_command_approval=ApprovalPolicy.AUTO,
                                             scope="plan", egress=("example.com:443",)))
    orchestrator = ex.Orchestrator(roles={"prober": role}, policy=ex.BudgetPolicy(run_ceiling_tokens=2_000_000),
                                   context_policy=ex.ContextPolicy(context_window=200_000))
    runner = ex.RecordingRunner({"scripted": ex.ScriptedModel()}, tools=[orchestrator.tool],
                                workspace_provider=provider)
    result = await runner.run(orchestrator.agent, ex.PLAN_TASK, orchestrator.config(
        tenant_id=tenant, project_id=PROJECT, model_override="scripted:model", max_turns=6))
    rows = await asyncio.to_thread(runner._workspace_store().connections, tenant, PROJECT)
    children = [r for r in runner.sinks if r != result.run_id]
    await runner.aclose()
    assert result.status is RunStatus.COMPLETED and children, result.error
    assert [(r.run_id, r.command_id) for r in rows] == [(result.run_id, None)]
    top_events = [e.event_type for e in runner.sinks[result.run_id].events()]
    assert top_events.count(EventType.WORKSPACE_CONNECTION) == 1
    assert top_events.index(EventType.WORKSPACE_CONNECTION) < top_events.index(EventType.RUN_COMPLETED)
    assert not [c for c in children
                if EventType.WORKSPACE_CONNECTION in [e.event_type for e in runner.sinks[c].events()]]


async def test_r2_a_kept_plan_workspace_reports_after_its_top_level_run_ended_as_rows_alone():
    tenant, top = tenancy(), str(uuid.uuid4())
    from agentsdk.workspace.records import InMemoryWorkspaceStore

    store = InMemoryWorkspaceStore()
    provider = FakeProvider(commands={"reach": _reach}, resolve=RESOLVE, sites={("example.com", 443): SITE})
    manager = mgr.WorkspaceManager(provider, lambda: store, 4)
    spec = WorkspaceSpec(image=FAKE_IMAGE, scope="plan", keep=True, egress=("example.com:443",))

    async def call(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    emitted: list = []

    async def top_emit(kind, payload):
        emitted.append(kind)

    async def child_emit(kind, payload):
        return None

    binding = manager.for_run(spec, tenant, PROJECT, str(uuid.uuid4()), child_emit, call, plan_run_id=top,
                              top=(top_emit, call))
    await binding.ensure()
    await binding.end("run_ended", child_emit)
    await manager.end_plans(top, top_emit, call)  # kept: it lives on
    before = len(emitted)
    for handle in await provider.list_owned({"agentsdk.tenant": tenant}):
        provider.kill_proxy(handle)
    for _ in range(50):
        rows = await asyncio.to_thread(store.connections, tenant, PROJECT)
        if rows:
            break
        await asyncio.sleep(0.02)
    await manager.aclose()
    assert [(r.reason, r.run_id) for r in rows] == [("proxy_ended", top)]
    assert emitted[before:].count(EventType.WORKSPACE_CONNECTION) == 0, emitted


async def test_r2_a_cancelled_end_still_ends_its_runs_events(monkeypatch):
    """A run's end cancelled during its workspace's removal: its connections are rows alone from
    then on, as on every other path to its terminal event."""
    async def cancelled(self, space, reason, emit, call):
        raise asyncio.CancelledError()

    monkeypatch.setattr(mgr.WorkspaceManager, "hold_or_remove", cancelled)

    async def call(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def emit(kind, payload):
        return None

    manager = mgr.WorkspaceManager(FakeProvider(), lambda: None, 1)
    binding = manager.for_run(WorkspaceSpec(image=FAKE_IMAGE, egress=("example.com:443",)), tenancy(), PROJECT,
                              str(uuid.uuid4()), emit, call)
    with pytest.raises(asyncio.CancelledError):
        await binding.end("run_ended", emit)
    assert binding._binding.gate.enter() is False  # its run ended: no event gets in (DECISION-5c68ddfb)


def test_r2_the_readme_declares_the_direct_checks_dependency_on_the_cdn():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "DECISION-e510a5e3" in readme and "CDN" in readme


@pytest.mark.docker
async def test_on_docker_r2_a_proxy_stopped_under_an_undecided_connection_reports_it_stopped():
    tenant = tenancy()
    docker, handle = await _docker_space(tenant)
    reports: list = []
    docker.watch_egress(handle, reports.append)
    holder = NL.join([
        "import os, socket, time, urllib.parse",
        "p = urllib.parse.urlsplit(os.environ['HTTPS_PROXY'])",
        "s = socket.create_connection((p.hostname, p.port), timeout=10)",
        "s.sendall(b'CONNECT exam')",
        "print('sent', flush=True)",
        "time.sleep(600)",
    ])
    running = asyncio.ensure_future(docker.run(handle, ["python", "-c", holder], timeout=900))
    await asyncio.sleep(5)
    await docker.remove(handle)
    await asyncio.wait({running}, timeout=60)
    assert [r["reason"] for r in reports if r["kind"] == "connection"] == ["stopped"], reports


@pytest.mark.docker
async def test_on_docker_r1_f2_with_no_egress_the_attempt_dials_a_live_address():
    """NFR-49's Docker check dials 1.1.1.1:443: a container with no isolation reaches it."""
    assert _bridge_reaches(tenancy(), "1.1.1.1") == "connected"


# =================================================================================================
# M44 rounds 3 and 4 repairs (DECISION-f7114b8b)
# =================================================================================================


def _connection(host: str = "example.com") -> dict:
    return {"kind": "connection", "id": str(uuid.uuid4()), "host": host, "port": 443, "allowed": True,
            "reason": "allowed", "at": None}


def _booking(tenant: str, run_id: str = "run"):
    """A workspace's recorder over an in-memory store holding its workspace, as the manager starts one."""
    from agentsdk.handle import RunControl
    from agentsdk.workspace.records import InMemoryWorkspaceStore, WorkspaceRecord, now

    emitted: list = []

    async def emit(kind, payload):
        emitted.append((kind, payload))

    space = mgr._Space(WorkspaceSpec(image=FAKE_IMAGE, egress=("example.com:443",)), tenant, PROJECT, run_id)
    space.reports = asyncio.Queue()
    space.owner = mgr._Binding(run_id, emit, RunControl().store)
    rows = InMemoryWorkspaceStore()
    rows.record_workspace(WorkspaceRecord(workspace_id="w", tenant_id=tenant, project_id=PROJECT, run_id=run_id,
                                          scope="run", provider="fake", image=FAKE_IMAGE, limits={},
                                          created_at=now()))
    manager = mgr.WorkspaceManager(FakeProvider(), lambda: rows, 1)
    handle = WorkspaceHandle(workspace_id="w", spec=space.spec, labels={})
    space.recorder = asyncio.ensure_future(manager.book(space, handle))
    return manager, space, handle, rows, emitted


def _refusing(monkeypatch, refused: set[int], method: str = "record_connection") -> None:
    """The store refuses the writes numbered in `refused`, counting from 1."""
    from agentsdk.workspace import records

    real, calls = getattr(records.InMemoryWorkspaceStore, method), [0]

    def write(self, *args, **kwargs):
        calls[0] += 1
        if calls[0] in refused:
            raise RuntimeError("the store refused it")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(records.InMemoryWorkspaceStore, method, write)


async def test_r3_a_connection_whose_row_the_store_refused_is_counted_on_the_unrecorded_row(monkeypatch):
    """Round 4: a row that failed to write was neither recorded nor counted."""
    _refusing(monkeypatch, {2, 3})
    tenant = tenancy()
    manager, space, handle, rows, _ = _booking(tenant)
    for _ in range(6):
        space.reports.put_nowait(_connection())
    await manager._booked(space)
    written = rows.connections(tenant, PROJECT)
    [unrecorded] = [r for r in written if r.reason == "unrecorded"]
    assert len([r for r in written if r.reason == "allowed"]) == 4 and unrecorded.count == 2
    assert unrecorded.run_id == "run" and not unrecorded.allowed


async def test_r3_refusals_at_the_limit_a_refused_update_left_uncounted_are_counted_once(monkeypatch):
    """The limit row's update refused: what it does not count is on the unrecorded row, and a later
    update that lands is never counted twice."""
    _refusing(monkeypatch, {1}, "update_connection")
    tenant = tenancy()
    manager, space, handle, rows, _ = _booking(tenant)
    for count in (3, 7):
        space.reports.put_nowait({"kind": "limit", "count": count, "at": None})
    await manager._booked(space)
    written = {r.reason: r.count for r in rows.connections(tenant, PROJECT)}
    assert written == {"connection_limit": 3, "unrecorded": 4}, written

    tenant = tenancy()
    manager, space, handle, rows, _ = _booking(tenant)
    _refusing(monkeypatch, {1}, "update_connection")  # refuses the update to 7, not the one to 9
    for count in (3, 7, 9):
        space.reports.put_nowait({"kind": "limit", "count": count, "at": None})
    await manager._booked(space)
    written = {r.reason: r.count for r in rows.connections(tenant, PROJECT)}
    assert written == {"connection_limit": 9}, written


async def test_r3_the_report_taken_when_told_to_stop_is_counted():
    """Round 3: the report the recorder took as it saw it was told to stop is counted with the rest."""
    tenant = tenancy()
    manager, space, handle, rows, _ = _booking(tenant)
    space.stop_booking = True  # told to before it took any
    for _ in range(3):
        space.reports.put_nowait(_connection())
    await asyncio.wait({space.recorder}, timeout=5)
    assert [r.count for r in rows.connections(tenant, PROJECT)] == [3]


async def test_r3_past_the_bound_a_queued_tunnel_end_still_writes_its_bytes(monkeypatch):
    """Round 3 B: past the bound, a queued end left its allowed row with no bytes."""
    monkeypatch.setattr(mgr, "_REMOVE_SECONDS", 0.3)
    _slow_rows(monkeypatch, 0.2)
    tenant = tenancy()
    manager, space, handle, rows, _ = _booking(tenant)
    first = _connection()
    space.reports.put_nowait(first)
    for _ in range(19):
        space.reports.put_nowait(_connection())
    space.reports.put_nowait({"kind": "end", "id": first["id"], "bytes_out": 11, "bytes_in": 22})
    await manager._booked(space)
    await asyncio.wait({space.recorder}, timeout=5)
    written = {r.connection_id: r for r in rows.connections(tenant, PROJECT)}
    assert space.recorder.done() and any(r.reason == "unrecorded" for r in written.values())
    assert (written[first["id"]].bytes_out, written[first["id"]].bytes_in) == (11, 22)


async def test_r3_past_the_bound_a_queued_proxy_death_still_writes_its_row(monkeypatch):
    """Round 3 B: past the bound, a queued `ended` wrote no proxy_ended row."""
    monkeypatch.setattr(mgr, "_REMOVE_SECONDS", 0.3)
    _slow_rows(monkeypatch, 0.2)
    tenant = tenancy()
    manager, space, handle, rows, _ = _booking(tenant)
    for _ in range(20):
        space.reports.put_nowait(_connection())
    space.reports.put_nowait({"kind": "ended", "at": None})
    await manager._booked(space)
    await asyncio.wait({space.recorder}, timeout=8)
    reasons = [r.reason for r in rows.connections(tenant, PROJECT)]
    assert space.recorder.done() and space.proxy_ended
    assert reasons.count("proxy_ended") == 1 and reasons.count("unrecorded") == 1, reasons


async def test_r3_reports_after_the_recorder_ended_are_counted_on_one_unrecorded_row():
    """Round 3 B: a report queued behind the marker, or arriving after the recorder ended, was lost."""
    tenant = tenancy()
    manager, space, handle, rows, _ = _booking(tenant)
    space.reports.put_nowait(_connection())
    space.reports.put_nowait(mgr._STOP)
    space.reports.put_nowait(_connection())  # behind the marker
    await manager._booked(space)
    assert space.booked
    counts = [r.count for r in rows.connections(tenant, PROJECT) if r.reason == "unrecorded"]
    assert counts == [1], counts
    for _ in range(2):
        manager.arrived(space, handle, _connection())  # after it ended
    await asyncio.wait({space.late}, timeout=5)
    manager.arrived(space, handle, {"kind": "limit", "count": 4, "at": None})
    await asyncio.wait({space.late}, timeout=5)
    written = rows.connections(tenant, PROJECT)
    assert [r.count for r in written if r.reason == "unrecorded"] == [7]  # one row, its count updated
    assert len([r for r in written if r.reason == "allowed"]) == 1


async def test_r3_on_the_fake_a_report_after_its_workspaces_removal_is_counted():
    tenant = tenancy()
    from agentsdk.workspace.records import InMemoryWorkspaceStore

    store = InMemoryWorkspaceStore()
    provider = FakeProvider(commands={"reach": _reach}, resolve=RESOLVE, sites={("example.com", 443): SITE})
    manager = mgr.WorkspaceManager(provider, lambda: store, 4)

    async def call(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def emit(kind, payload):
        return None

    binding = manager.for_run(WorkspaceSpec(image=FAKE_IMAGE, egress=("example.com:443",)), tenant, PROJECT,
                              str(uuid.uuid4()), emit, call)
    handle = await binding.ensure()
    watcher = provider._space(handle).watcher
    await binding.end("run_ended", emit)
    watcher(_connection())  # the provider's reader, still reading after the removal
    await asyncio.wait({binding._space.late}, timeout=5)
    await manager.aclose()
    rows = await asyncio.to_thread(store.connections, tenant, PROJECT)
    assert [(r.reason, r.count) for r in rows] == [("unrecorded", 1)]


async def test_r3_a_cancelled_end_waits_for_the_event_being_written(monkeypatch):
    """Round 3 A: the cancelled end marked its run ended without the booking lock."""
    async def cancelled(self, space, reason, emit, call):
        raise asyncio.CancelledError()

    monkeypatch.setattr(mgr.WorkspaceManager, "hold_or_remove", cancelled)

    async def call(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def emit(kind, payload):
        return None

    manager = mgr.WorkspaceManager(FakeProvider(), lambda: None, 1)
    binding = manager.for_run(WorkspaceSpec(image=FAKE_IMAGE, egress=("example.com:443",)), tenancy(), PROJECT,
                              str(uuid.uuid4()), emit, call)
    space = binding._space
    await space.booking.acquire()  # an event being written
    ending = asyncio.ensure_future(binding.end("run_ended", emit))
    await asyncio.sleep(0.1)
    waited = not ending.done()
    space.booking.release()
    with pytest.raises(asyncio.CancelledError):
        await ending
    assert waited and binding._binding.gate.enter() is False


async def test_r3_a_cancelled_end_books_what_was_reported_with_its_event(monkeypatch):
    async def cancelled(self, space, reason, emit, call):
        raise asyncio.CancelledError()

    monkeypatch.setattr(mgr.WorkspaceManager, "hold_or_remove", cancelled)
    tenant = tenancy()
    manager, space, handle, rows, emitted = _booking(tenant)
    binding = manager.for_run(space.spec, tenant, PROJECT, "run", space.owner.emit, space.owner.call)
    binding._space, binding._binding = space, space.owner
    space.reports.put_nowait(_connection())
    with pytest.raises(asyncio.CancelledError):
        await binding.end("run_ended", space.owner.emit)
    assert [k for k, _ in emitted] == [EventType.WORKSPACE_CONNECTION]
    space.reports.put_nowait(mgr._STOP)
    await asyncio.wait({space.recorder}, timeout=5)


class _Hangs:
    """Runs one command, then waits until it is cancelled."""

    def __init__(self) -> None:
        self.waiting = asyncio.Event()

    async def send(self, request):
        if not any(m.role is Role.ASSISTANT for m in request.messages):
            return respond(calls=[ToolCall(id="t0", name="run_command", arguments={"argv": ["reach"]})])
        self.waiting.set()
        await asyncio.Event().wait()


async def test_r3_cancelled_again_while_its_workspace_ends_the_run_still_ends_cancelled(monkeypatch):
    """Round 3 A, its older half: `_end_workspace` let a second cancel through `_cancelled`, which
    then wrote no RunCancelled."""
    real = mgr.RunWorkspace.end

    async def end_then_cancelled(self, reason, emit):
        await real(self, reason, emit)
        raise asyncio.CancelledError()  # a cancel landing as the removal ends

    monkeypatch.setattr(mgr.RunWorkspace, "end", end_then_cancelled)
    model = _Hangs()
    provider = FakeProvider(commands={"reach": _reach}, resolve=RESOLVE, sites={("example.com", 443): SITE})
    runner = Runner({"m": model}, workspace_provider=provider)
    agent = AgentSpec(id="hangs", instructions="x", preferred_model="m:x", tool_profile=("run_command",),
                      workspace=WorkspaceSpec(image=FAKE_IMAGE, run_command_approval=ApprovalPolicy.AUTO,
                                              egress=("example.com:443",)))
    handle = await runner.start(agent, "go", RunConfig(tenant_id=tenancy(), project_id=PROJECT, max_turns=4))
    await asyncio.wait_for(model.waiting.wait(), 30)
    handle.cancel()
    try:
        result = await asyncio.wait_for(handle.result(), 30)
    finally:
        await runner.aclose()
    kinds_ = [e.event_type for e in result.events]
    assert result.status is RunStatus.CANCELLED and kinds_[-1] is EventType.RUN_CANCELLED, kinds_[-3:]
    assert EventType.WORKSPACE_REMOVED in kinds_


def test_r3_docker_passes_its_connection_cap_to_the_proxy():
    """Round 3: the cap was fixed at 10,000 on Docker."""
    handle = WorkspaceHandle(workspace_id=str(uuid.uuid4()), spec=WorkspaceSpec(image=IMAGE, egress=ALLOW),
                             labels={})
    assert json.loads(dk.proxy_args(handle, "10.0.0.0/24")[-1])["max_connections"] == eg.MAX_CONNECTIONS
    assert json.loads(dk.proxy_args(handle, "10.0.0.0/24", max_connections=3)[-1])["max_connections"] == 3
    assert DockerProvider(max_connections=3)._max_connections == 3


@pytest.mark.docker
async def test_on_docker_r3_past_a_lowered_cap_the_final_count_is_reported():
    tenant = tenancy()
    docker = DockerProvider(max_connections=1)
    labels = {"agentsdk.workspace": str(uuid.uuid4()), "agentsdk.tenant": tenant, "agentsdk.project": PROJECT}
    handle = await docker.create(WorkspaceSpec(image=IMAGE, egress=ALLOW), labels)
    reports: list = []
    docker.watch_egress(handle, reports.append)
    asker = NL.join([
        "import os, socket, urllib.parse",
        "p = urllib.parse.urlsplit(os.environ['HTTPS_PROXY'])",
        "for _ in range(4):",
        "    s = socket.create_connection((p.hostname, p.port), timeout=10)",
        "    s.sendall(b'CONNECT example.com:443 HTTP/1.1\\r\\nHost: example.com:443\\r\\n\\r\\n')",
        "    print(s.recv(64)[:12], flush=True)",
        "    s.close()",
    ])
    try:
        outcome = await docker.run(handle, ["python", "-c", asker], timeout=120)
    finally:
        await docker.remove(handle)
    assert outcome.exit_code == 0, outcome.stderr
    assert len([r for r in reports if r["kind"] == "connection"]) == 1, reports
    assert [r["count"] for r in reports if r["kind"] == "limit"][-1] == 3, reports


# =================================================================================================
# M44 round 5 repairs (DECISION-0d5473ef, DECISION-5c68ddfb, DECISION-7a3a2ae2)
# =================================================================================================


class _SlowWrite:
    """An event write on its worker thread, as a store's: `seconds` inside it, noting when it began
    and when it landed, so a test sees what landed before what."""

    def __init__(self, seconds: float) -> None:
        import threading

        self.seconds, self.began, self.landed = seconds, threading.Event(), []

    def __call__(self, kind, payload):
        import time

        self.began.set()
        time.sleep(self.seconds)
        self.landed.append((kind, time.monotonic()))


def _gated_booking(tenant: str, write, store=None):
    """`_booking`, its run's events written as the Runner writes them: through RunControl.store, on
    a worker thread (DECISION-5c68ddfb); and the run's binding, to end."""
    from agentsdk.handle import RunControl, StoredEmit

    manager, space, handle, rows, _ = _booking(tenant)
    space.owner = mgr._Binding("run", StoredEmit(store or RunControl().store, write), RunControl().store)
    binding = manager.for_run(space.spec, tenant, PROJECT, "run", space.owner.emit, space.owner.call)
    binding._space, binding._binding = space, space.owner
    return manager, space, handle, rows, binding


async def _removed(self, space, reason, emit, call):
    return None  # its removal done: the run's end goes straight on to settle


async def _stopped(space) -> None:
    space.reports.put_nowait(mgr._STOP)
    await asyncio.wait({space.recorder}, timeout=10)


async def test_r5_b1_cancelled_again_as_it_ends_the_event_being_written_still_lands_first(monkeypatch):
    """Round 5 B1: a second cancel during settled() skipped the wait for the booking lock, so a
    WorkspaceConnection being written landed after RunCancelled. A real write on its worker thread
    and a real second cancel, arriving while settled() waits."""
    import time

    monkeypatch.setattr(mgr.WorkspaceManager, "hold_or_remove", _removed)
    tenant = tenancy()
    write = _SlowWrite(1.5)
    manager, space, handle, rows, binding = _gated_booking(tenant, write)
    space.reports.put_nowait(_connection())
    assert await asyncio.to_thread(write.began.wait, 5)
    ending = asyncio.ensure_future(binding.end("run_ended", space.owner.emit))
    await asyncio.sleep(0.2)
    assert not ending.done()  # settled() is waiting for the event
    ending.cancel()  # cancelled again
    await asyncio.wait({ending}, timeout=10)
    terminal = time.monotonic()  # the run's terminal event is written once its end returns
    assert ending.done() and ending.cancelled()
    assert [kind for kind, _ in write.landed] == [EventType.WORKSPACE_CONNECTION], write.landed
    assert write.landed[0][1] <= terminal
    await _stopped(space)


async def test_r5_b1_through_the_runner_cancelled_twice_nothing_follows_run_cancelled(monkeypatch):
    """Round 5 B1 by the reviewer's route: a run cancelled while its connection's event is being
    written, then cancelled again as its workspace ends. Its workspace is kept: a removal waits for
    the recorder, and with it the event, before settled() is reached (pre-review, M44 round 5)."""
    import threading
    import time

    from agentsdk.events import InMemoryEventSink

    real, began, order = InMemoryEventSink.emit, threading.Event(), []

    def slow(self, event_type, payload=None, **identifiers):
        if event_type is EventType.WORKSPACE_CONNECTION:
            began.set()
            time.sleep(1.5)
        event = real(self, event_type, payload, **identifiers)
        order.append(event_type)
        return event

    monkeypatch.setattr(InMemoryEventSink, "emit", slow)

    async def one(cmd):
        await cmd.connect("example.com", 443)
        return 0

    model = _Hangs()
    provider = FakeProvider(commands={"reach": one}, resolve=RESOLVE, sites={("example.com", 443): SITE})
    runner = Runner({"m": model}, workspace_provider=provider)
    agent = AgentSpec(id="hangs", instructions="x", preferred_model="m:x", tool_profile=("run_command",),
                      workspace=WorkspaceSpec(image=FAKE_IMAGE, run_command_approval=ApprovalPolicy.AUTO,
                                              egress=("example.com:443",), keep=True))
    handle = await runner.start(agent, "go", RunConfig(tenant_id=tenancy(), project_id=PROJECT, max_turns=4))
    try:
        assert await asyncio.to_thread(began.wait, 30)
        handle.cancel()
        await asyncio.sleep(0.3)  # its workspace removed, settled() waiting for the event
        handle._control.task.cancel()  # cancelled again
        result = await asyncio.wait_for(handle.result(), 30)
        for _ in range(100):  # the event, if it could still land, has had its time
            if EventType.WORKSPACE_CONNECTION in order:
                break
            await asyncio.sleep(0.05)
    finally:
        await runner.aclose()
    assert result.status is RunStatus.CANCELLED
    assert order[-1] is EventType.RUN_CANCELLED and EventType.WORKSPACE_CONNECTION in order, order[-4:]


async def test_r5_n1_past_the_bound_an_event_write_not_yet_begun_is_shut_out(monkeypatch):
    """Round 5 N1: settled() waited without a bound for an event that had not begun. Past the bound
    a write still waiting for its thread is shut out, its row standing alone (DECISION-5c68ddfb)."""
    monkeypatch.setattr(mgr, "_REMOVE_SECONDS", 0.3)
    monkeypatch.setattr(mgr.WorkspaceManager, "hold_or_remove", _removed)
    released = asyncio.Event()

    async def queued(fn, *args, **kwargs):  # a write waiting for a worker thread
        await released.wait()
        return await asyncio.to_thread(fn, *args, **kwargs)

    tenant = tenancy()
    write = _SlowWrite(0)
    manager, space, handle, rows, binding = _gated_booking(tenant, write, store=queued)
    space.reports.put_nowait(_connection())
    for _ in range(100):
        if space.booking.locked():
            break
        await asyncio.sleep(0.01)
    assert space.booking.locked()  # its row written, its event waiting
    ending = asyncio.ensure_future(binding.end("run_ended", space.owner.emit))
    await asyncio.wait({ending}, timeout=5)
    assert ending.done() and ending.exception() is None
    released.set()
    await _stopped(space)
    assert write.landed == [] and not write.began.is_set()
    assert [r.reason for r in rows.connections(tenant, PROJECT)] == ["allowed"]


async def test_r5_n1_past_the_bound_an_event_write_inside_its_transaction_is_waited_for(monkeypatch):
    import time

    monkeypatch.setattr(mgr, "_REMOVE_SECONDS", 0.2)
    monkeypatch.setattr(mgr.WorkspaceManager, "hold_or_remove", _removed)
    tenant = tenancy()
    write = _SlowWrite(1.0)
    manager, space, handle, rows, binding = _gated_booking(tenant, write)
    space.reports.put_nowait(_connection())
    assert await asyncio.to_thread(write.began.wait, 5)
    ending = asyncio.ensure_future(binding.end("run_ended", space.owner.emit))
    await asyncio.wait({ending}, timeout=10)
    terminal = time.monotonic()
    assert ending.done() and write.landed and write.landed[0][1] <= terminal, write.landed
    await _stopped(space)


async def test_r5_n1_after_its_runs_end_a_plain_emitters_connection_is_its_row_alone(monkeypatch):
    """The gate, which replaced the `ended` flag, holds for an emitter that is not a StoredEmit too:
    a report booked after the run's end writes its row and no event (F3, DECISION-5c68ddfb)."""
    monkeypatch.setattr(mgr.WorkspaceManager, "hold_or_remove", _removed)
    tenant = tenancy()
    manager, space, handle, rows, emitted = _booking(tenant)
    binding = manager.for_run(space.spec, tenant, PROJECT, "run", space.owner.emit, space.owner.call)
    binding._space, binding._binding = space, space.owner
    space.reports.put_nowait(_connection())
    await binding.end("run_ended", space.owner.emit)
    space.reports.put_nowait(_connection())  # after its run's end
    await _stopped(space)
    assert len([r for r in rows.connections(tenant, PROJECT) if r.reason == "allowed"]) == 2
    assert [kind for kind, _ in emitted] == [EventType.WORKSPACE_CONNECTION]


async def test_r5_n1_what_was_reported_before_its_runs_end_keeps_its_events(monkeypatch):
    """The barrier, deterministically: reports queued, unbooked, as the run ends are booked with their
    events before its gate shuts (F3, DECISION-a7a1b37b). The round 1 test hit this only by timing."""
    monkeypatch.setattr(mgr.WorkspaceManager, "hold_or_remove", _removed)
    _slow_rows(monkeypatch, 0.05)
    tenant = tenancy()
    manager, space, handle, rows, emitted = _booking(tenant)
    binding = manager.for_run(space.spec, tenant, PROJECT, "run", space.owner.emit, space.owner.call)
    binding._space, binding._binding = space, space.owner
    for _ in range(3):
        space.reports.put_nowait(_connection())
    await binding.end("run_ended", space.owner.emit)  # at once: none booked yet
    assert [kind for kind, _ in emitted] == [EventType.WORKSPACE_CONNECTION] * 3
    await _stopped(space)


async def test_r6_through_keeps_a_held_cancel_when_its_work_then_fails():
    """Round 6 (1): a cancel held while the work ran was lost when the work then raised."""
    from agentsdk.handle import through

    entered = asyncio.Event()

    async def fails():
        entered.set()
        await asyncio.sleep(0.2)
        raise RuntimeError("the work failed")

    calling = asyncio.ensure_future(through(fails()))
    await entered.wait()
    calling.cancel()  # held until the work ends
    await asyncio.wait({calling}, timeout=5)
    assert calling.done() and calling.cancelled()
    try:
        calling.result()
    except asyncio.CancelledError as raised:
        assert isinstance(raised.__cause__, RuntimeError)  # neither is lost

    plain = asyncio.ensure_future(through(fails()))  # with no cancel, the failure is raised as it was
    await asyncio.wait({plain}, timeout=5)
    assert isinstance(plain.exception(), RuntimeError)


def test_r7_the_workspace_modules_hold_a_cancel_through_the_one_helper():
    """Round 7 F-R7-1: docker._through and manager._completed were copies of through() as it was
    before round 6's repair; both are gone (DECISION-ed9b1bba)."""
    from agentsdk import handle

    assert dk.through is handle.through and mgr.through is handle.through
    assert not hasattr(dk, "_through") and not hasattr(mgr, "_completed")


async def test_r7_a_command_cancelled_while_its_docker_client_outlives_its_kill_ends_cancelled(monkeypatch):
    """Round 7 F-R7-1, at the call site reproduced: a cancel held while the cleanup waits, bounded,
    on a docker client that outlived its kill was lost when that wait timed out."""
    monkeypatch.setattr(dk, "_CLI_SECONDS", 0.3)
    final_wait = asyncio.Event()

    class Client:  # a `docker start -a` that never exits, its kill ignored
        def __init__(self) -> None:
            self.returncode, self.waits = None, 0
            self.stdin = type("In", (), {"write": lambda s, d: None, "close": lambda s: None,
                                         "drain": staticmethod(lambda: asyncio.sleep(0))})()
            self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
            self.stdout.feed_eof()
            self.stderr.feed_eof()

        def kill(self) -> None:
            pass

        async def wait(self):
            self.waits += 1
            if self.waits == 2:  # the cleanup's bounded wait, after the kill
                final_wait.set()
            await asyncio.Event().wait()

    async def cli(self, *args, **kwargs):
        return 0, b"", b""

    async def spawn(self, args, *, env=None):
        return Client()

    monkeypatch.setattr(DockerProvider, "_cli", cli)
    monkeypatch.setattr(DockerProvider, "_spawn", spawn)
    docker = DockerProvider()
    handle = WorkspaceHandle(workspace_id=str(uuid.uuid4()), spec=WorkspaceSpec(image=IMAGE), labels={})
    running = asyncio.ensure_future(docker._contained(handle, dk._Command("c"), ["true"], timeout=0.1, stdin=None,
                                                      env={}, keep=1024))
    await asyncio.wait_for(final_wait.wait(), 10)
    running.cancel()  # held through that wait, which then times out
    await asyncio.wait({running}, timeout=10)
    assert running.done() and running.cancelled()


class _Lingering:
    """A `docker start -a` client that never exits; whether it was killed is kept."""

    def __init__(self) -> None:
        self.returncode, self.killed = None, False
        self.stdin = type("In", (), {"write": lambda s, d: None, "close": lambda s: None,
                                     "drain": staticmethod(lambda: asyncio.sleep(0))})()
        self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        self.stdout.feed_eof()
        self.stderr.feed_eof()

    def kill(self) -> None:
        self.killed = True

    async def wait(self):
        await asyncio.Event().wait()


async def test_r8_a_command_cancelled_while_its_container_is_removed_still_has_its_client_killed(monkeypatch):
    """Round 8 sweep: the cleanup's first step held the cancel and raised it as it ended, so the
    client's kill below never ran (DECISION-1e35a472)."""
    monkeypatch.setattr(dk, "_CLI_SECONDS", 0.2)
    client, removals, removing = _Lingering(), [], asyncio.Event()

    async def cli(self, *args, **kwargs):
        if args[:1] == ("rm",):
            removals.append(args)
            if len(removals) == 2:  # the cleanup's removal, after the body's own
                removing.set()
                await asyncio.sleep(0.3)
        return 0, b"", b""

    async def spawn(self, args, *, env=None):
        return client

    monkeypatch.setattr(DockerProvider, "_cli", cli)
    monkeypatch.setattr(DockerProvider, "_spawn", spawn)
    handle = WorkspaceHandle(workspace_id=str(uuid.uuid4()), spec=WorkspaceSpec(image=IMAGE), labels={})
    running = asyncio.ensure_future(DockerProvider()._contained(handle, dk._Command("c"), ["true"], timeout=0.1,
                                                                stdin=None, env={}, keep=1024))
    await asyncio.wait_for(removing.wait(), 10)
    running.cancel()
    await asyncio.wait({running}, timeout=10)
    assert running.done() and running.cancelled() and client.killed


def _discarding(monkeypatch, fails: bool = False):
    """`_stop_egress` slow and noted, `_remove` noted, and failing if asked: what a provider's
    discard did."""
    stopping, removed = asyncio.Event(), []

    async def stop(self, space):
        stopping.set()
        await asyncio.sleep(0.3)

    async def remove(self, workspace_id):
        removed.append(workspace_id)
        if fails:
            raise mgr.WorkspaceUnavailable("docker could not remove the workspace's volume or network")

    monkeypatch.setattr(DockerProvider, "_stop_egress", stop)
    monkeypatch.setattr(DockerProvider, "_remove", remove)
    return stopping, removed


@pytest.mark.parametrize("fails", [False, True])
async def test_r8_a_workspace_removal_cancelled_while_its_proxy_stops_still_removes_it(monkeypatch, fails):
    """And when the removal itself then fails, the call still ends cancelled: the held cancel wins
    over the work's failure (round 6, DECISION-5445c8d0), here in the docker module."""
    stopping, removed = _discarding(monkeypatch, fails)
    docker = DockerProvider()
    handle = WorkspaceHandle(workspace_id=str(uuid.uuid4()), spec=WorkspaceSpec(image=IMAGE, egress=ALLOW),
                             labels={})
    docker._spaces[handle.workspace_id] = dk._Space(handle)
    removing = asyncio.ensure_future(docker.remove(handle))
    await asyncio.wait_for(stopping.wait(), 10)
    removing.cancel()
    await asyncio.wait({removing}, timeout=10)
    assert removing.done() and removing.cancelled() and removed == [handle.workspace_id]


async def test_r8_a_failed_creation_cancelled_while_its_proxy_stops_still_removes_it(monkeypatch):
    import contextlib as cl

    stopping, removed = _discarding(monkeypatch)

    async def cli(self, *args, **kwargs):
        return (1 if args[:1] == ("cp",) else 0), b"", b""

    @cl.asynccontextmanager
    async def helper(self, handle):
        yield "helper"

    monkeypatch.setattr(DockerProvider, "_cli", cli)
    monkeypatch.setattr(DockerProvider, "_helper", helper)
    workspace_id = str(uuid.uuid4())
    creating = asyncio.ensure_future(DockerProvider().create(
        WorkspaceSpec(image=IMAGE), {"agentsdk.workspace": workspace_id, "agentsdk.tenant": tenancy()}))
    await asyncio.wait_for(stopping.wait(), 10)
    creating.cancel()
    await asyncio.wait({creating}, timeout=10)
    assert creating.done() and creating.cancelled() and removed == [workspace_id]


async def test_r8_a_kept_workspace_cancelled_while_its_creation_finishes_is_still_marked_kept():
    space = mgr._Space(WorkspaceSpec(image=FAKE_IMAGE, keep=True), tenancy(), PROJECT, "run")
    space.handle = WorkspaceHandle(workspace_id=str(uuid.uuid4()), spec=space.spec, labels={})
    space.creating = asyncio.ensure_future(asyncio.sleep(0.3, result=space.handle))
    holding = asyncio.ensure_future(mgr.WorkspaceManager(FakeProvider(), lambda: None, 1).hold_or_remove(
        space, "run_ended", None, None))
    await asyncio.sleep(0.05)
    holding.cancel()
    await asyncio.wait({holding}, timeout=5)
    assert holding.done() and holding.cancelled() and space.kept


async def test_r8_nb1_a_command_whose_cancelled_recording_fails_still_ends_cancelled(monkeypatch):
    """Round 8 NB-1: no behavioural test guarded the manager's sites. Cancelled, then cancelled again
    while it is killed and recorded, and that recording failing: the call ends cancelled, the
    failure kept as the cancel's cause, never escaping in its place (FR-148)."""
    from agentsdk.workspace.records import InMemoryWorkspaceStore

    entered = asyncio.Event()

    async def failing(self, *args, **kwargs):
        entered.set()
        await asyncio.sleep(0.3)
        raise RuntimeError("the recording failed")

    monkeypatch.setattr(mgr.RunWorkspace, "_cancelled", failing)

    async def hangs(cmd):
        await asyncio.Event().wait()

    async def call(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def emit(kind, payload):
        return None

    store = InMemoryWorkspaceStore()
    manager = mgr.WorkspaceManager(FakeProvider(commands={"hang": hangs}), lambda: store, 4)
    binding = manager.for_run(WorkspaceSpec(image=FAKE_IMAGE), tenancy(), PROJECT, str(uuid.uuid4()), emit, call)
    running = asyncio.ensure_future(binding.run(["hang"], stdin=None, timeout=30, tool_call_id=None))
    try:
        for _ in range(100):
            if binding._space.handle is not None:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)
        running.cancel()
        await asyncio.wait_for(entered.wait(), 5)
        running.cancel()  # cancelled again, while it is recorded
        await asyncio.wait({running}, timeout=5)
        assert running.done() and running.cancelled()
    finally:
        await manager.aclose()


def test_r5_n1_a_gate_shut_lets_no_write_in_and_says_whether_one_is_inside():
    gate = mgr._Gate()
    assert not gate.busy() and gate.enter() and gate.busy()
    gate.shut()
    assert gate.busy()  # the one inside finishes
    gate.leave()
    assert gate.enter() is False and not gate.busy()


async def test_r5_b2_a_failed_run_cancelled_as_its_workspace_is_removed_still_ends_failed(monkeypatch):
    """Round 5 B2: the failed path let a cancel during its workspace's removal escape, so the run
    wrote no terminal event. Its failure came first: it ends failed, its workspace removed."""
    real = FakeProvider.remove
    removing = asyncio.Event()

    async def slow(self, handle):
        removing.set()
        await asyncio.sleep(1.0)
        return await real(self, handle)

    monkeypatch.setattr(FakeProvider, "remove", slow)

    class Breaks:
        async def send(self, request):
            if not any(m.role is Role.ASSISTANT for m in request.messages):
                return respond(calls=[ToolCall(id="t0", name="run_command", arguments={"argv": ["reach"]})])
            raise RuntimeError("the model broke")

    provider = FakeProvider(commands={"reach": _reach}, resolve=RESOLVE, sites={("example.com", 443): SITE})
    runner = Runner({"m": Breaks()}, workspace_provider=provider)
    agent = AgentSpec(id="breaks", instructions="x", preferred_model="m:x", tool_profile=("run_command",),
                      workspace=WorkspaceSpec(image=FAKE_IMAGE, run_command_approval=ApprovalPolicy.AUTO,
                                              egress=("example.com:443",)))
    handle = await runner.start(agent, "go", RunConfig(tenant_id=tenancy(), project_id=PROJECT, max_turns=4))
    try:
        await asyncio.wait_for(removing.wait(), 30)
        handle._control.task.cancel()  # cancelled during the failed run's removal
        result = await asyncio.wait_for(handle.result(), 30)
    finally:
        await runner.aclose()
    kinds_ = [e.event_type for e in result.events]
    assert result.status is RunStatus.FAILED and kinds_[-1] is EventType.RUN_FAILED, kinds_[-3:]
    assert EventType.WORKSPACE_REMOVED in kinds_


async def test_r5_b2_a_cancel_out_of_its_own_workspaces_end_still_ends_its_plans(monkeypatch):
    """Round 5 B2: `_end_workspace` skipped `end_plans` when its run's own `end` raised a cancel,
    leaking the plan's network and proxy until the Runner closed."""
    from agentsdk.handle import RunControl

    runner = Runner({"m": Turns()}, workspace_provider=FakeProvider())
    ended: list = []

    class Cancelled:
        async def end(self, reason, emit):
            raise asyncio.CancelledError()

    async def end_plans(run_id, emit, call):
        ended.append(run_id)

    runner._workspace_runs["r"] = Cancelled()
    monkeypatch.setattr(runner._workspaces, "end_plans", end_plans)
    with pytest.raises(asyncio.CancelledError):
        await runner._end_workspace("r", None, RunControl())
    assert ended == ["r"]
    await runner.aclose()


async def test_r5_b2_a_cancel_while_its_workspace_ends_is_held_until_its_plans_end_too(monkeypatch):
    """The cancel lands on the caller while the steps run: both finish, then it is raised."""
    from agentsdk.handle import RunControl

    runner = Runner({"m": Turns()}, workspace_provider=FakeProvider())
    ended: list = []
    entered = asyncio.Event()

    class Slow:
        async def end(self, reason, emit):
            entered.set()
            await asyncio.sleep(0.5)
            ended.append("own")

    async def end_plans(run_id, emit, call):
        ended.append("plans")

    runner._workspace_runs["r"] = Slow()
    monkeypatch.setattr(runner._workspaces, "end_plans", end_plans)
    ending = asyncio.ensure_future(runner._end_workspace("r", None, RunControl()))
    await entered.wait()
    ending.cancel()
    await asyncio.wait({ending}, timeout=5)
    assert ending.done() and ending.cancelled() and ended == ["own", "plans"]
    await runner.aclose()


async def test_r5_n3_a_proxy_death_whose_row_the_store_refused_has_its_event_and_is_counted(monkeypatch):
    """Round 5 N3: only a refused connection was counted; a refused proxy_ended row was lost."""
    _refusing(monkeypatch, {1})
    tenant = tenancy()
    manager, space, handle, rows, emitted = _booking(tenant)
    space.reports.put_nowait({"kind": "ended", "at": None})
    await manager._booked(space)
    assert [(r.reason, r.count) for r in rows.connections(tenant, PROJECT)] == [("unrecorded", 1)]
    assert space.proxy_ended and [p["reason"] for _, p in emitted] == ["proxy_ended"], emitted


async def test_r5_n2_aclose_ends_a_late_count_waiting_on_its_store_past_the_bound(monkeypatch):
    """Round 5 N2: a late pass waiting on a store that never answered outlived aclose()."""
    monkeypatch.setattr(mgr, "_REMOVE_SECONDS", 0.3)
    tenant = tenancy()
    manager, space, handle, rows, _ = _booking(tenant)
    await manager._booked(space)
    never = asyncio.Event()

    async def hangs(fn, *args, **kwargs):  # short of its thread, so a cancel reaches it
        await never.wait()

    space.owner.call = hangs
    manager.arrived(space, handle, _connection())
    late = space.late
    closing = asyncio.ensure_future(manager.aclose())
    await asyncio.wait({closing}, timeout=5)
    assert closing.done() and late.done()


async def test_r5_n2_a_late_count_whose_cancel_its_store_absorbed_stops_after_its_current_write(monkeypatch):
    """RunControl.store absorbs a cancel and uncancels it once its write lands, so a cancelled late
    pass carried on through every tunnel end queued; told to stop, it ends after the current one."""
    import time

    from agentsdk.handle import RunControl
    from agentsdk.workspace import records

    monkeypatch.setattr(mgr, "_REMOVE_SECONDS", 0.4)
    real, writes = records.InMemoryWorkspaceStore.update_connection, []

    def slow(self, *args, **kwargs):
        time.sleep(0.5)
        writes.append(args)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(records.InMemoryWorkspaceStore, "update_connection", slow)
    tenant = tenancy()
    manager, space, handle, rows, _ = _booking(tenant)
    await manager._booked(space)
    space.owner.call = RunControl().store
    for _ in range(5):
        space.reports.put_nowait({"kind": "end", "id": str(uuid.uuid4()), "bytes_out": 1, "bytes_in": 1})
    manager.arrived(space, handle, {"kind": "end", "id": str(uuid.uuid4()), "bytes_out": 1, "bytes_in": 1})
    late = space.late
    await manager.aclose()
    assert late.done() and len(writes) == 1, len(writes)


async def test_r5_n1_the_runner_writes_connection_events_through_a_gated_emitter():
    """DECISION-5c68ddfb holds only if the Runner hands its run's binding a StoredEmit writing to
    that run's own sink."""
    from agentsdk.handle import StoredEmit

    model = _Hangs()
    provider = FakeProvider(commands={"reach": _reach}, resolve=RESOLVE, sites={("example.com", 443): SITE})
    runner = Runner({"m": model}, workspace_provider=provider)
    agent = AgentSpec(id="hangs", instructions="x", preferred_model="m:x", tool_profile=("run_command",),
                      workspace=WorkspaceSpec(image=FAKE_IMAGE, run_command_approval=ApprovalPolicy.AUTO,
                                              egress=("example.com:443",)))
    handle = await runner.start(agent, "go", RunConfig(tenant_id=tenancy(), project_id=PROJECT, max_turns=4))
    try:
        await asyncio.wait_for(model.waiting.wait(), 30)
        [workspace] = runner._workspace_runs.values()
        emit = workspace._binding.emit
        assert isinstance(emit, StoredEmit) and emit.write.__self__.scope[2] == str(handle.run_id)
        handle.cancel()
        result = await asyncio.wait_for(handle.result(), 30)
    finally:
        await runner.aclose()
    assert _connection_events(result)  # and they were written through it


async def test_r5_n1_past_the_bound_a_plain_emitter_inside_its_write_is_waited_for_too(monkeypatch):
    """An emitter that is not the Runner's StoredEmit is gated where it is called: one already
    writing is waited for past the bound, as one inside its transaction is."""
    monkeypatch.setattr(mgr, "_REMOVE_SECONDS", 0.2)
    monkeypatch.setattr(mgr.WorkspaceManager, "hold_or_remove", _removed)
    tenant = tenancy()
    manager, space, handle, rows, _ = _booking(tenant)
    began, landed = asyncio.Event(), []

    async def slow(kind, payload):
        began.set()
        await asyncio.sleep(1.0)
        landed.append(kind)

    space.owner.emit = slow
    binding = manager.for_run(space.spec, tenant, PROJECT, "run", slow, space.owner.call)
    binding._space, binding._binding = space, space.owner
    space.reports.put_nowait(_connection())
    await asyncio.wait_for(began.wait(), 5)
    ending = asyncio.ensure_future(binding.end("run_ended", slow))
    await asyncio.wait({ending}, timeout=10)
    assert ending.done() and landed == [EventType.WORKSPACE_CONNECTION]
    await _stopped(space)


async def test_r5_n6_an_unrecorded_row_written_then_refused_is_updated_never_written_twice(monkeypatch):
    """Round 5 N6: an insert that committed and then raised left the manager writing a second row."""
    from agentsdk.workspace import records

    real, raised = records.InMemoryWorkspaceStore.record_connection, []

    def committed_then_raised(self, record):
        real(self, record)
        if record.reason == "unrecorded" and not raised:
            raised.append(record.connection_id)
            raise RuntimeError("the connection dropped after its commit")

    monkeypatch.setattr(records.InMemoryWorkspaceStore, "record_connection", committed_then_raised)
    tenant = tenancy()
    manager, space, handle, rows, _ = _booking(tenant)
    await manager._booked(space)
    for _ in range(2):
        manager.arrived(space, handle, _connection())
        await asyncio.wait({space.late}, timeout=5)
    assert raised and [(r.reason, r.count) for r in rows.connections(tenant, PROJECT)] == [("unrecorded", 2)]


@pytest.mark.parametrize("store", STORES)
async def test_r5_n6_the_store_refuses_a_second_unrecorded_row_for_a_workspace(store):
    """Migration 0025's unique index, and the in-memory store's twin of it."""
    from agentsdk.workspace.records import ConnectionRecord, now

    tenant = tenancy()
    persistence = Persistence.postgres(DSN, create_schema=False) if store == "postgres" else None
    provider = FakeProvider(commands={"reach": _reach}, resolve=RESOLVE, sites={("example.com", 443): SITE})
    runner = Runner({"m": Turns()}, workspace_provider=provider, persistence=persistence)
    agent = AgentSpec(id="reacher", instructions="x", preferred_model="m:x", tool_profile=("run_command",),
                      workspace=WorkspaceSpec(image=FAKE_IMAGE, run_command_approval=ApprovalPolicy.AUTO,
                                              egress=("example.com:443",)))
    try:
        result = await runner.run(agent, json.dumps([[["reach"]]]),
                                  RunConfig(tenant_id=tenant, project_id=PROJECT, max_turns=6))
        rows = runner._workspace_store()
        [workspace] = await asyncio.to_thread(rows.workspaces, tenant, PROJECT)

        def unrecorded(count: int) -> ConnectionRecord:
            return ConnectionRecord(
                connection_id=str(uuid.uuid4()), workspace_id=workspace.workspace_id, tenant_id=tenant,
                project_id=PROJECT, run_id=result.run_id, command_id=None, host=None, port=None,
                address=None, allowed=False, reason="unrecorded", at=now(), count=count)

        await asyncio.to_thread(rows.record_connection, unrecorded(1))
        with pytest.raises(Exception):
            await asyncio.to_thread(rows.record_connection, unrecorded(2))
        written = await asyncio.to_thread(rows.connections, tenant, PROJECT)
        assert [r.count for r in written if r.reason == "unrecorded"] == [1]
    finally:
        await runner.aclose()


def test_r5_n6_migration_0025_adds_the_one_unrecorded_row_index():
    from agentsdk import migrate

    versions = [version for version, _ in migrate.discover()]
    assert versions[-1] == "0025" and "0024" in versions
    text = (REPO / "agentsdk" / "migrations" / "0025_workspace_connections_unrecorded.sql").read_text(encoding="utf-8")
    assert "CREATE UNIQUE INDEX" in text and "WHERE reason = 'unrecorded'" in text


def _hello(*server_names: list) -> bytes:
    """A ClientHello with one server_name extension per list given, each naming its servers."""
    extensions = b""
    for names in server_names:
        entries = b"".join(b"\x00" + len(n).to_bytes(2, "big") + n.encode() for n in names)
        data = len(entries).to_bytes(2, "big") + entries
        extensions += b"\x00\x00" + len(data).to_bytes(2, "big") + data
    body = (b"\x03\x03" + bytes(32) + b"\x00" + b"\x00\x02\x13\x01" + b"\x01\x00"
            + len(extensions).to_bytes(2, "big") + extensions)
    handshake = b"\x01" + len(body).to_bytes(3, "big") + body
    return b"\x16\x03\x01" + len(handshake).to_bytes(2, "big") + handshake


@pytest.mark.parametrize("names", [
    (["example.com", "evil.example"],),            # two names in one extension
    (["evil.example", "example.com"],),
    (["example.com"], ["evil.example"]),           # a second server_name extension
    (["example.com"], ["example.com"]),
])
def test_r5_n4_a_client_hello_naming_more_than_one_server_is_not_read_as_the_host(names):
    """Round 5 N4: only the first name was read, so a second could front another server."""
    assert eg.sni_of(_hello(["example.com"])) == "example.com"  # the builder's control
    assert eg.sni_of(_hello(*names)) != "example.com"


@pytest.mark.parametrize("names", [(["example.com", "evil.example"],), (["example.com"], ["evil.example"])])
async def test_r5_n4_the_proxy_refuses_a_client_hello_naming_more_than_one_server(names):
    reports: list = []
    async with Upstream() as upstream:
        server, port = await _serve(_proxy(reports, upstream))
        status, back = await _ask(port, _head("example.com:443"), _hello(*names))
        await _settle(reports, 1)
        server.close()
    assert status.startswith(b"HTTP/1.1 200") and back == b"" and upstream.connections == 0
    assert [(d["allowed"], d["reason"]) for d in _decisions(reports)] == [(False, "sni_mismatch")]


@pytest.mark.parametrize("target", ["example.com:0443", "example.com:00443", "example.com:+443"])
async def test_r5_n8_a_connect_port_written_with_a_leading_zero_is_malformed(target):
    """Round 5 N8: CONNECT example.com:00443 matched the listed example.com:443."""
    reports: list = []
    server, port = await _serve(_proxy(reports, None))
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(_head(target))
    await writer.drain()
    answer = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    await _settle(reports, 1)
    server.close()
    assert answer.startswith(b"HTTP/1.1 403")
    assert [(d["allowed"], d["reason"]) for d in _decisions(reports)] == [(False, "malformed")]


async def test_r5_n7_the_recorders_last_pass_and_a_late_one_overlapping_count_each_report_once(monkeypatch):
    """Round 5 N7: no test ran the two counting passes at once."""
    tenant = tenancy()
    manager, space, handle, rows, _ = _booking(tenant)
    _slow_rows(monkeypatch, 0.3)  # the unrecorded row's first write is slow
    space.reports.put_nowait(mgr._STOP)
    space.reports.put_nowait(_connection())  # behind the marker: the recorder's last pass counts it
    for _ in range(100):
        if space.booked:
            break
        await asyncio.sleep(0.01)
    for _ in range(3):
        manager.arrived(space, handle, _connection())
    assert space.late is not None and not space.recorder.done()  # both passes under way
    await asyncio.wait({space.recorder, space.late}, timeout=10)
    if manager._late:
        await asyncio.wait(set(manager._late), timeout=10)
    assert [(r.reason, r.count) for r in rows.connections(tenant, PROJECT)] == [("unrecorded", 4)]


@pytest.mark.parametrize("outcome, failing", [
    ("failed TimeoutError", False), ("failed OSError", False),
    ("failed ConnectionRefusedError", True), ("connected", True), ("", True),
])
def test_r5_n5_example_38s_neighbour_check_counts_a_refused_connection_as_reached(outcome, failing):
    """Round 5 N5: a refused connection proves the neighbour's network was reached."""
    clause = _example(EX38_NAME).neighbour_failed(outcome, listening=True)
    assert (clause is not None) is failing, clause
    assert _example(EX38_NAME).neighbour_failed("failed TimeoutError", listening=False) is not None


@pytest.mark.docker
@pytest.mark.parametrize("control", ["host", "neighbour"])
async def test_on_docker_r5_n5_example_38s_checks_fail_without_their_controls(monkeypatch, control):
    """Round 5 N5: host_direct passed on any host with no listener at that address; the neighbour
    check passed whether or not the neighbour's proxy listened."""
    module = _example(EX38_NAME)
    real = module.controls

    async def without(chosen, neighbour):
        found = await real(chosen, neighbour)
        assert found == {"host": True, "neighbour": True}, found  # both answer, as they should
        return {**found, control: False}

    monkeypatch.setattr(module, "controls", without)
    checks = {label: (passed, why) for label, passed, why in
              await module.offline("memory", "docker", tenant=tenancy(), quiet=True)}
    [check] = [label for label in checks if ("neighbour" in label if control == "neighbour"
                                             else "direct connection" in label)]
    assert checks[check][0] is False, checks[check]
