"""Egress by allow list (FR-159 to FR-162, M44; P5-D19, P5-D20).

A workspace whose spec names `egress` reaches out only through a proxy of its own: HTTPS `CONNECT`
to a listed `host:port`, every address the name resolves to global, the TLS ClientHello's SNI equal to
the host. This module is that proxy, and it is the very program the proxy container runs, as
`python3 -c SOURCE CONFIG` in the workspace's own image: so it imports the standard library alone
(NFR-46), and nothing of the SDK. `FakeProvider` applies the same `Proxy` in process (FR-163).

Each decision, and each tunnel's end, is reported as one JSON line on the proxy's stdout, which only
the SDK reads (FR-162). A command is known by the token in its HTTPS_PROXY, the URL's password; the
provider maps it to the command, and no token is ever recorded (DECISION-88dea779).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import json
import re
import signal
import socket
import ssl
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

MAX_ENTRIES = 32
MAX_TUNNELS = 32
IDLE_SECONDS = 60.0
MAX_CONNECTIONS = 10_000
MAX_WAITING = 64  # connections waiting on their request head at once (DECISION-26009e04)
PYTHON = (3, 8)  # the oldest python3 the program runs on
PORT = 3128
USER = "agentsdk"  # the proxy URL's user; the token is its password (KNOWLEDGE-bd42619c)
HEAD_BYTES = 8192
RECORD_BYTES = 5 + 16384  # one TLS record, the most a ClientHello is read for
READ_SECONDS = 10.0
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,64}")
_METHOD = re.compile(rb"[A-Z]")
FORBIDDEN = b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
ESTABLISHED = b"HTTP/1.1 200 Connection established\r\n\r\n"


# --- the spec (FR-159) ---------------------------------------------------------------------------------


def _is_name(host: str) -> bool:
    """An exact DNS name: lower case, no trailing dot, no wildcard, and no IP literal in any form."""
    if not host or len(host) > 253:
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    try:
        # Hex, octal and short forms too: what inet_aton reads, a resolver reads as an address (F7).
        socket.inet_aton(host)
        return False
    except OSError:
        pass
    labels = host.split(".")
    # A numeric last label is no top-level domain: `1.2.3` would resolve as an address.
    return all(_LABEL.fullmatch(label) for label in labels) and not labels[-1].isdigit()


def _pair(entry: str) -> tuple[str, int]:
    host, _, port = entry.rpartition(":")
    return host, int(port)


def parse_egress(value: object) -> tuple[str, ...]:
    """FR-159: `WorkspaceSpec.egress`, a tuple of at most 32 distinct `"host:port"`; anything else
    raises ValueError."""
    if not isinstance(value, tuple):
        raise ValueError(f"WorkspaceSpec.egress must be a tuple of 'host:port' strings, got {type(value).__name__}")
    if len(value) > MAX_ENTRIES:
        raise ValueError(f"WorkspaceSpec.egress holds at most {MAX_ENTRIES} entries, got {len(value)}")
    for entry in value:
        if not isinstance(entry, str):
            raise ValueError(f"WorkspaceSpec.egress entries must be 'host:port' strings, got {type(entry).__name__}")
        host, sep, port = entry.rpartition(":")
        # F6: five digits at most before int(), which refuses past 4,300 (DECISION-a7a1b37b).
        if not sep or not (port.isascii() and port.isdigit()) or len(port) > 5 or port != str(int(port)) \
                or not 1 <= int(port) <= 65535:
            raise ValueError(f"WorkspaceSpec.egress entry {entry!r} needs a port from 1 to 65535")
        if not _is_name(host):
            raise ValueError(f"WorkspaceSpec.egress entry {entry!r} must name an exact, lower-case DNS name: "
                             "no IP literal, wildcard or trailing dot")
    if len(set(value)) != len(value):
        raise ValueError("WorkspaceSpec.egress names an entry twice")
    return value


# --- reading what a client sends --------------------------------------------------------------------


def _clean(text: str, limit: int = 255) -> str:
    """What a client named, safe to record: printable ASCII, cut to `limit`."""
    return "".join(c if " " < c < "\x7f" else "?" for c in text)[:limit]


def _token(headers: dict[str, str]) -> str | None:
    value = headers.get("proxy-authorization", "")
    scheme, _, encoded = value.partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        user, _, token = base64.b64decode(encoded.strip(), validate=True).decode("ascii").partition(":")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    return token if user == USER and _TOKEN.fullmatch(token) else None


def sni_of(data: bytes) -> str | None:
    """The server name a TLS ClientHello carries, when `data` begins with one whole record holding
    it; otherwise None. One naming more than one server, in one server_name extension or two, gives
    them all, comma-joined, which no host equals (round 5 N4, DECISION-0d5473ef)."""
    try:
        if len(data) < 5 or data[0] != 0x16:
            return None
        body = data[5:5 + int.from_bytes(data[3:5], "big")]
        if len(body) < 4 or body[0] != 1:
            return None
        hello = body[4:4 + int.from_bytes(body[1:4], "big")]
        at = 2 + 32  # version, random
        at += 1 + hello[at]  # session id
        at += 2 + int.from_bytes(hello[at:at + 2], "big")  # cipher suites
        at += 1 + hello[at]  # compression methods
        end = at + 2 + int.from_bytes(hello[at:at + 2], "big")
        at += 2
        if end > len(hello):
            return None
        names = []
        while at + 4 <= end:
            kind, size = int.from_bytes(hello[at:at + 2], "big"), int.from_bytes(hello[at + 2:at + 4], "big")
            if kind == 0:
                entry = hello[at + 4:at + 4 + size]
                if len(entry) < 5:
                    return None
                listed = entry[2:2 + int.from_bytes(entry[0:2], "big")]
                while listed:  # every name in its list, not only the first
                    if len(listed) < 3 or listed[0] != 0:
                        return None
                    name = listed[3:3 + int.from_bytes(listed[1:3], "big")]
                    if not name:
                        return None
                    names.append(name.decode("ascii").lower())
                    listed = listed[3 + len(name):]
            at += 4 + size
        return ",".join(names) if names else None
    except (IndexError, UnicodeDecodeError):
        return None


def client_hello(sni: str | None) -> bytes:
    """A real TLS ClientHello naming `sni` (or none), as a client would send it first."""
    context = ssl.create_default_context()
    if sni is None:
        context.check_hostname = False
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls = context.wrap_bio(incoming, outgoing, server_hostname=sni)
    try:
        tls.do_handshake()
    except ssl.SSLWantReadError:
        pass
    return outgoing.read()


def _global(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    mapped = getattr(ip, "ipv4_mapped", None)
    return (mapped or ip).is_global


async def _resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(info[4][0] for info in infos))


async def _connect(address: str, port: int):
    return await asyncio.wait_for(asyncio.open_connection(address, port), READ_SECONDS)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- the proxy (FR-161) -------------------------------------------------------------------------------


class Proxy:
    """FR-161: HTTPS CONNECT to a listed pair only, under its bounds (DECISION-88dea779)."""

    def __init__(self, allow, *, report, resolve=None, connect=None, is_global=None, max_tunnels: int = MAX_TUNNELS,
                 idle_seconds: float = IDLE_SECONDS, max_connections: int = MAX_CONNECTIONS,
                 max_waiting: int = MAX_WAITING) -> None:
        self.allow = frozenset(_pair(entry) for entry in parse_egress(tuple(allow)))
        self.report, self.resolve = report, resolve or _resolve
        self.connect, self.is_global = connect or _connect, is_global or _global
        self.max_tunnels, self.idle_seconds, self.max_connections = max_tunnels, idle_seconds, max_connections
        self.max_waiting = max_waiting
        self.connections = self.at_limit = self.tunnels = self.waiting = 0
        self.handlers: set = set()
        # Past the limit, the count reported at the first refusal, then at most once a second, and
        # when the proxy stops: lines, queue and writes bounded (DECISION-e510a5e3).
        self.limit_reported = 0
        self.limit_timer = None

    async def serve(self, host: str, port: int):
        return await asyncio.start_server(self.handle, host, port)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self.handlers.add(task)
        state: dict = {"seen": None}
        try:
            await self._handle(reader, writer, state)
        except ConnectionError:
            self._undecided(state, "client_reset")  # F3: reset by its client before its decision
        except asyncio.CancelledError:
            self._undecided(state, "stopped")  # the proxy stopped before its decision (DECISION-e510a5e3)
            raise
        except Exception:  # noqa: BLE001 - one client's failure is never the proxy's, and is recorded
            self._undecided(state, "proxy_error")
        finally:
            self.handlers.discard(task)
            writer.close()

    def _undecided(self, state: dict, reason: str) -> None:
        """A counted connection that ended before its decision is reported, so every one is (FR-162)."""
        seen = state["seen"]
        if seen is not None and seen["reason"] is None:
            seen["reason"] = reason
            self.report(seen)

    async def close(self) -> None:
        """Every open tunnel ended, each reporting its bytes: the proxy's last reports."""
        for task in list(self.handlers):
            task.cancel()
        await asyncio.gather(*self.handlers, return_exceptions=True)
        if self.limit_timer is not None:
            self.limit_timer.cancel()
        self._report_limit()

    def _limited(self) -> None:
        self.at_limit += 1
        if not self.limit_reported:
            self._report_limit()
        elif self.limit_timer is None:
            self.limit_timer = asyncio.get_running_loop().call_later(1.0, self._report_limit)

    def _report_limit(self) -> None:
        self.limit_timer = None
        if self.at_limit > self.limit_reported:
            self.limit_reported = self.at_limit
            self.report({"kind": "limit", "count": self.at_limit, "at": _now()})

    async def _handle(self, reader, writer, state: dict) -> None:
        if self.connections >= self.max_connections:
            self._limited()
            writer.write(FORBIDDEN)
            return
        self.connections += 1
        seen = state["seen"] = {"kind": "connection", "id": str(uuid.uuid4()), "token": None, "host": None,
                                "port": None, "address": None, "allowed": False, "reason": None, "at": _now()}

        def refuse(reason: str, answer: bytes | None = FORBIDDEN) -> None:
            seen["reason"] = reason
            self.report(seen)
            if answer:
                writer.write(answer)

        if self.waiting >= self.max_waiting:
            # F2: heads that never end cannot pile up past the proxy's memory (DECISION-26009e04).
            return refuse("busy")
        self.waiting += 1
        try:
            head, rest = await self._read_head(reader)
        finally:
            self.waiting -= 1
        request = _request(head)
        if request is None:
            return refuse("malformed")
        method, target, headers = request
        seen["token"] = _token(headers)
        if method != "CONNECT":
            return refuse("not_connect")
        host, _, port = target.rpartition(":")
        if not host or not (port.isascii() and port.isdigit()) or len(port) > 5 or port != str(int(port)) \
                or not 1 <= int(port) <= 65535:
            # F6: five digits at most before int() (DECISION-a7a1b37b); N8: written as a port is, with
            # no leading zero (DECISION-7a3a2ae2).
            return refuse("malformed")
        seen["host"], seen["port"] = _clean(host), int(port)
        try:
            ipaddress.ip_address(host.strip("[]"))
            return refuse("ip_literal")
        except ValueError:
            pass
        if (host, int(port)) not in self.allow:
            return refuse("not_listed")
        if self.tunnels >= self.max_tunnels:
            return refuse("busy")
        self.tunnels += 1
        try:
            try:
                addresses = await asyncio.wait_for(self.resolve(host, int(port)), READ_SECONDS)
            except (OSError, asyncio.TimeoutError, UnicodeError):
                addresses = []
            if not addresses:
                return refuse("unresolved")
            if not all(self.is_global(address) for address in addresses):
                return refuse("not_global")
            writer.write(ESTABLISHED)
            await writer.drain()
            hello = await self._read_hello(reader, rest)
            if hello.startswith(b"CONNECT "):
                return refuse("second_request", None)
            if not hello or hello[0] != 0x16:
                return refuse("not_tls", None)
            sni = sni_of(hello)
            if sni is None:
                return refuse("no_sni", None)
            if sni != host:
                return refuse("sni_mismatch", None)
            # The address it checked, never resolving again (FR-161).
            seen["address"] = addresses[0]
            try:
                up_reader, up_writer = await self.connect(addresses[0], int(port))
            except (OSError, asyncio.TimeoutError):
                return refuse("upstream_failed", None)
            seen["allowed"], seen["reason"] = True, "allowed"
            self.report(seen)
            counts = [0, 0]
            try:
                await self._tunnel(reader, writer, up_reader, up_writer, hello, counts)
            finally:
                # Ended however it ends -- the proxy stopping with its workspace included (FR-162).
                self.report({"kind": "end", "id": seen["id"], "bytes_out": counts[0], "bytes_in": counts[1],
                             "at": _now()})
        finally:
            self.tunnels -= 1

    async def _read_head(self, reader) -> tuple[bytes | None, bytes]:
        """The request head, at most HEAD_BYTES, and whatever followed it; None when there is none."""
        data = b""
        deadline = asyncio.get_running_loop().time() + READ_SECONDS
        while b"\r\n\r\n" not in data:
            if data and not _METHOD.match(data):
                return None, b""
            if len(data) > HEAD_BYTES:
                return None, b""
            part = await _read_by(reader, HEAD_BYTES + 1, deadline)
            if not part:
                return None, b""
            data += part
        head, _, rest = data.partition(b"\r\n\r\n")
        return (head if len(head) <= HEAD_BYTES else None), rest

    async def _read_hello(self, reader, data: bytes) -> bytes:
        """The tunnel's first bytes: one whole TLS record when they begin one, else what came."""
        deadline = asyncio.get_running_loop().time() + READ_SECONDS
        while len(data) < 5 or (data[0] == 0x16 and len(data) < min(
                RECORD_BYTES, 5 + int.from_bytes(data[3:5], "big"))):
            if data and data[0] != 0x16 and len(data) >= 8:
                break
            part = await _read_by(reader, RECORD_BYTES, deadline)
            if not part:
                break
            data += part
        return data[:RECORD_BYTES] if data[:1] == b"\x16" else data

    async def _tunnel(self, reader, writer, up_reader, up_writer, first: bytes, counts: list[int]) -> None:
        """Bytes both ways, counted into `counts`, until either side ends or neither moves for
        `idle_seconds`."""
        loop = asyncio.get_running_loop()
        last = [loop.time()]

        async def send(sink, data: bytes, index: int) -> None:
            counts[index] += len(data)
            last[0] = loop.time()
            sink.write(data)
            await sink.drain()

        async def pipe(source, sink, index: int, start: bytes = b"") -> None:
            if start:
                await send(sink, start, index)
            while data := await source.read(65536):
                await send(sink, data, index)

        async def idle() -> None:
            while loop.time() - last[0] < self.idle_seconds:
                await asyncio.sleep(max(0.01, self.idle_seconds - (loop.time() - last[0])))

        tasks = [asyncio.ensure_future(pipe(reader, up_writer, 0, first)),
                 asyncio.ensure_future(pipe(up_reader, writer, 1)),
                 asyncio.ensure_future(idle())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            up_writer.close()


async def _read_by(reader, size: int, deadline: float) -> bytes:
    """Up to `size` bytes, or nothing once `deadline` has passed. Python 3.8's asyncio has no
    `timeout()`; a reset by the client still raises (F3, F6)."""
    left = deadline - asyncio.get_running_loop().time()
    if left <= 0:
        return b""
    try:
        return await asyncio.wait_for(reader.read(size), left)
    except asyncio.TimeoutError:
        return b""


def _own_address(subnet: str) -> str:
    """This host's address on `subnet`: the one the proxy listens on, so only that network reaches
    it (F5). Read from the route to the subnet, sending nothing."""
    network = ipaddress.ip_network(subnet)
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((str(network.network_address + 1), 9))
        address = probe.getsockname()[0]
    finally:
        probe.close()
    if ipaddress.ip_address(address) not in network:
        raise OSError(f"no address of this host is on {subnet}")
    return address


def _request(head: bytes | None) -> tuple[str, str, dict[str, str]] | None:
    if head is None:
        return None
    try:
        lines = head.decode("latin-1").split("\r\n")
    except UnicodeDecodeError:
        return None
    parts = lines[0].split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        return None
    headers = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if not sep:
            return None
        headers[name.strip().lower()] = value.strip()
    return parts[0], parts[1], headers


# --- the program --------------------------------------------------------------------------------------

SOURCE = Path(globals()["__file__"]).read_text(encoding="utf-8") if "__file__" in globals() else None


def _print(line: dict) -> None:
    print(json.dumps(line, separators=(",", ":")), flush=True)


async def _main(config: dict) -> None:
    proxy = Proxy(config["allow"], report=_print, max_tunnels=config.get("max_tunnels", MAX_TUNNELS),
                  idle_seconds=config.get("idle_seconds", IDLE_SECONDS),
                  max_connections=config.get("max_connections", MAX_CONNECTIONS))
    # Its workspace's network alone: nothing on the bridge reaches it (F5).
    address = _own_address(config["subnet"]) if config.get("subnet") else "0.0.0.0"
    server = await proxy.serve(address, config.get("port", PORT))
    stop = asyncio.Event()
    try:
        # Stopped with its workspace, it ends each open tunnel so each reports its bytes (FR-162).
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
    except (NotImplementedError, RuntimeError):
        pass  # no signal handlers here: it is killed instead
    _print({"kind": "listening", "address": address, "port": server.sockets[0].getsockname()[1],
            "python": list(sys.version_info[:2]), "at": _now()})
    await stop.wait()
    server.close()
    await proxy.close()


if __name__ == "__main__":
    asyncio.run(_main(json.loads(sys.argv[1])))
