"""DockerProvider (FR-142 to FR-144, M39): workspaces in Docker, driven through the `docker` CLI.

A workspace is a volume, mounted `volume-nocopy` at `/workspace` and owned by 65534 (P5-D4). Each
command is its own container over it, hardened as P5-D6 fixes and never configurable down:
`docker create`, `docker start -a -i`, `docker inspect`, `docker rm -f` -- so a kill by name works
from the moment the container exists, the exit code is the command's own, and the container is
gone when the command ends (DECISION-504c7f04). Files cross only as tar streams (P5-D8): the host
builds what goes in, filters what comes out, and never opens a path inside a volume.

Every `docker` call is an `asyncio` subprocess with an argument list, never a shell; the CLI is
needed only when a `Runner` is given this provider (NFR-40). Imports only the standard library.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import posixpath
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import time
import uuid
import weakref
from collections.abc import AsyncIterator, Awaitable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from ..errors import WorkspaceUnavailable
# A container's removal, once begun, is never abandoned: finished even when the caller is
# cancelled meanwhile, then that cancel raised (one helper, DECISION-ed9b1bba).
from ..handle import through
from . import _REMOVE_ATTEMPTS, CommandOutcome, ServerLaunch, WorkspaceHandle, WorkspaceSpec, charged, egress, normal_path

MOUNT = "/workspace"
UID = 65534
MAX_TRANSFER_BYTES = 16 * 2**20  # FR-143
MAX_TRANSFER_MEMBERS = 10_000
_CLI_SECONDS = 60.0  # any docker call that is not a command
_MEASURE_SECONDS = 60.0
_READ = 64 * 1024
# FR-156, P5-D17: every container's PID 1. It runs the argv and keeps its exit code, kills every
# other process until none is left -- PID 1 is the one kill -1 never reaches -- then prints the
# container's CPU reading as its last line and exits with the command's own code. The argv runs
# through `env`, as a program looked up on PATH, as docker ran it before: a shell builtin such as
# eval, exec or exit would run inside PID 1 and could print a reading and leave before the wrapper's
# (M43 pre-review F1, DECISION-0883f7bd). The shell's own exports, SHLVL and PWD, are unset first,
# so a command sees the environment it saw before M43: only what WorkspaceSpec names (P5-D6).
# Nothing the command does stops the wrapper or speaks for it (M43 round 1, DECISION-88395b13): it
# ignores every signal a command could send it -- ash keeps a SIGINT handler, so an untrapped SIGINT
# ended PID 1 before its line -- and handlers reset on exec, so the command keeps its own. Its line
# carries the command's nonce, read from stdin's first line into a variable no command can read
# (/proc/1/environ and cmdline are readable, /proc/1/mem is not). After the argv, builtins only: a
# fork bomb at `pids` leaves nothing to start a process with.
MARK = b"@@agentsdk-cpu"
WRAPPER = (
    "trap : INT QUIT TERM HUP USR1 USR2; unset SHLVL PWD; read -r n; "
    'env -- "$@"; c=$?; '
    "while kill -9 -1 2>/dev/null; do :; done; "
    'u=; while read -r k v; do [ "$k" = usage_usec ] && u=$v; done < /sys/fs/cgroup/cpu.stat; '
    "printf '\\n%s %s %s %s\\n' " + MARK.decode() + ' "$n" "$u" "$c"; exit $c'
)
TAIL_BYTES = 4096  # what is kept of stdout's end, apart from max_output_bytes, to read the line from


def _nonce() -> str:
    """A command's own 128-bit nonce, which only its wrapper is given (DECISION-88395b13)."""
    return secrets.token_hex(16)
# What `tarfile.data_filter` resolves member names against: a host path that is never created,
# opened or written; the filter only computes where a member would land (P5-D8).
_NOWHERE = os.path.abspath(os.path.join(os.sep, "agentsdk-workspace-filter-nowhere"))
# DECISION-504c7f04: names the CLI itself reads, which a WorkspaceSpec.env must not set, since its
# values reach the container through the CLI's own environment (FR-142).
_CLI_NAMES = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "USERPROFILE", "HOME", "HOMEDRIVE", "HOMEPATH",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "TEMP", "TMP", "HTTP_PROXY", "HTTPS_PROXY",
    "NO_PROXY", "ALL_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR",
    # The Go runtime the CLI is built on (pre-review P11).
    "GODEBUG", "GOTRACEBACK", "GOMAXPROCS", "GOGC", "GOMEMLIMIT",
})
# Windows refuses a command line of 32,767 characters or more (pre-review P4); the CLI's own path
# is allowed for apart from the arguments.
_COMMAND_LINE = 32_767 - 512
# FR-146 via DECISION-0004d2dc: one folder's direct entries, read in a container hardened as a
# command is. $1 is the path under /workspace, $2 the most to describe; each entry is printed as
# kind, size and name separated by tabs and ended by a NUL, then the count. Exit 20: no such path;
# 21: not a folder (a link to one included); 22: the folder cannot be read; 23: an entry cannot.
_LIST = r"""d="/workspace/$1"
if [ ! -e "$d" ] && [ ! -L "$d" ]; then exit 20; fi
if [ -L "$d" ] || [ ! -d "$d" ]; then exit 21; fi
if [ ! -r "$d" ]; then exit 22; fi
cd -- "$d" || exit 22
n=0
for f in * .[!.]* ..?*; do
  if [ ! -e "$f" ] && [ ! -L "$f" ]; then continue; fi
  n=$((n + 1))
  if [ "$n" -gt "$2" ]; then continue; fi
  if [ -L "$f" ]; then k=link; s=0
  elif [ -d "$f" ]; then k=dir; s=0
  elif [ -f "$f" ]; then k=file; s=$(stat -c %s -- "$f") || exit 23
  else k=other; s=0; fi
  printf '%s\t%s\t%s\0' "$k" "$s" "$f"
done
printf 'total\t%s\t\0' "$n"
"""


def env_refusal(env: Mapping[str, str]) -> str | None:
    """Why `env` cannot be given through the docker CLI, naming the variable, never its value; or
    None. The CLI's environment is case-insensitive on Windows, so two names differing only in
    case would reach the container as one (DECISION-504c7f04)."""
    seen: dict[str, str] = {}
    for name in env:
        folded = name.upper()
        if folded.startswith("DOCKER_") or folded in _CLI_NAMES:
            return f"WorkspaceSpec.env names {name!r}, which the docker CLI itself reads"
        if folded in seen:
            return f"WorkspaceSpec.env names {seen[folded]!r} and {name!r}, which differ only in case"
        seen[folded] = name
    return None


def hardening(spec: WorkspaceSpec, network: str = "none") -> list[str]:
    """P5-D6: every command container's limits, exactly these; nothing a model or a spec can reach
    adds to them or takes one away. With egress, the workspace's own internal network replaces
    `none`, and nothing else changes (FR-160)."""
    return [
        "--network", network, "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only",
        "--tmpfs", f"/tmp:size={spec.tmp_bytes}", "--user", f"{UID}:{UID}", "--pids-limit", str(spec.pids),
        "--memory", str(spec.memory_bytes), "--memory-swap", str(spec.memory_bytes), "--cpus", f"{spec.cpus:.2f}",
        # The daemon keeps nothing of a command's output: it is read only through the attached
        # client, kept up to max_output_bytes (DECISION-d4ede901).
        "--log-driver", "none",
    ]


PROXY_LIMITS = {"--pids-limit": "32", "--memory": str(64 * 2**20), "--memory-swap": str(64 * 2**20), "--cpus": "0.50"}


def proxy_hardening(spec: WorkspaceSpec, network: str) -> list[str]:
    """FR-160: the proxy's container is hardened as a command's is, on the workspace's network, with
    fixed limits of its own (DECISION-88dea779)."""
    args = hardening(spec, network)
    for flag, value in PROXY_LIMITS.items():
        args[args.index(flag) + 1] = value
    return args


def volume_name(workspace_id: str) -> str:
    return f"agentsdk-{workspace_id}"


def engine_refusal(version: str) -> str | None:
    """F1: an egress network's gateway is isolated, which needs Docker Engine 28; why this daemon
    cannot give one, or None (DECISION-26009e04)."""
    try:
        major = int(version.split(".", 1)[0])
    except ValueError:
        major = 0
    if major >= 28:
        return None
    return f"egress needs Docker Engine 28 or later, for a network whose gateway is isolated; this daemon is {version!r}"


def ready_refusal(line: bytes) -> str | None:
    """F6: why the proxy's first line is not a python3 at least 3.8 listening, or None."""
    try:
        ready = json.loads(line)
        version = tuple(ready["python"]) if isinstance(ready, dict) and ready.get("kind") == "listening" else ()
    except (ValueError, KeyError, TypeError):
        version = ()
    if len(version) == 2 and all(isinstance(n, int) for n in version) and version >= egress.PYTHON:
        return None
    return f"the image cannot run the egress proxy: it needs python3, {egress.PYTHON[0]}.{egress.PYTHON[1]} or later"


def network_name(workspace_id: str) -> str:
    return f"agentsdk-net-{workspace_id}"


def proxy_name(workspace_id: str) -> str:
    return f"agentsdk-proxy-{workspace_id}"


def _egress_labels(handle: WorkspaceHandle) -> list[str]:
    """The proxy's and the network's labels: the workspace's, but for its spec, which its volume
    carries; with it the allow list would ride twice on the proxy's line (F5, DECISION-a7a1b37b)."""
    return _labels({key: value for key, value in handle.labels.items() if key != "agentsdk.spec"})


def proxy_args(handle: WorkspaceHandle, subnet: str, max_connections: int = egress.MAX_CONNECTIONS) -> list[str]:
    """FR-160: the docker call creating a workspace's proxy, its allow list, subnet and connection cap
    its config (FR-161, DECISION-f7114b8b)."""
    workspace_id = handle.workspace_id
    config = json.dumps({"allow": list(handle.spec.egress), "port": egress.PORT, "subnet": subnet,
                         "max_connections": max_connections})
    return ["create", "--pull", "never", "--name", proxy_name(workspace_id), *_egress_labels(handle),
            *proxy_hardening(handle.spec, network_name(workspace_id)), "--entrypoint=", handle.spec.image,
            "python3", "-c", egress.SOURCE, config]


def _labels(labels: Mapping[str, str]) -> list[str]:
    return [arg for key, value in sorted(labels.items()) for arg in ("--label", f"{key}={value}")]


def _mount(handle: WorkspaceHandle) -> list[str]:
    # P5-D4: a volume, never a bind mount; volume-nocopy keeps the root's owner.
    return ["--mount", f"type=volume,src={volume_name(handle.workspace_id)},dst={MOUNT},volume-nocopy"]


def container_args(handle: WorkspaceHandle, name: str, argv: list[str], env_names: list[str], network: str = "none",
                   assigned: tuple[str, ...] = ()) -> list[str]:
    """FR-142: `docker create` for one command. The image is by digest and never pulled; the
    entrypoint is cleared so `argv` runs as given, under the wrapper and no other shell (FR-156);
    each env value is named only (`-e NAME`) and given in the CLI's own environment, so none is on
    a command line. With egress, the command joins its workspace's network and is `assigned` its
    proxy URL as `-e NAME=VALUE`: the CLI reads HTTPS_PROXY from its own environment, so it cannot
    go there, and its token names the command only (FR-160, DECISION-88dea779)."""
    return [
        "create", "--pull", "never", "--name", name, "-i", *_labels(handle.labels), *hardening(handle.spec, network),
        "-w", MOUNT, *_mount(handle), *[arg for env in env_names for arg in ("-e", env)],
        *[arg for env in assigned for arg in ("-e", env)],
        "--entrypoint=", handle.spec.image, "sh", "-c", WRAPPER, "sh", *argv,
    ]


SERVER_ROLE = "agentsdk.role"  # FR-165: a sandboxed server's container, found by it on a restart


def server_args(handle: WorkspaceHandle, name: str, command: str, args: list[str], env_names: list[str],
                network: str = "none", assigned: tuple[str, ...] = ()) -> list[str]:
    """FR-164, FR-165: `docker run -i` for a sandboxed MCP server -- attached, its stdio the
    protocol, hardened as a command is over its workspace's volume, labelled as its workspace is
    and as a server. No wrapper: the server's stdin is the protocol's from its first byte, and a
    server is never charged. The image's entrypoint is cleared, as a command's is, so `command`
    runs as given and nothing else writes to the protocol (M45 pre-review F1). Env values by name
    only, as a command's (FR-142)."""
    return [
        "run", "-i", "--pull", "never", "--name", name, *_labels({**handle.labels, SERVER_ROLE: "server"}),
        *hardening(handle.spec, network), "-w", MOUNT, *_mount(handle),
        *[arg for env in env_names for arg in ("-e", env)], *[arg for env in assigned for arg in ("-e", env)],
        "--entrypoint=", handle.spec.image, command, *args,
    ]


def reading(tail: bytes, nonce: bytes) -> tuple[int | None, int, int] | None:
    """FR-156: the wrapper's line, when `tail` ends with one carrying the command's `nonce`: its
    `usage_usec` (None when the container gave none), the exit code it names, and its length. Only
    the last line counts, and only with the nonce, so any copy the command printed is its own output."""
    line = re.compile(rb"\n" + re.escape(MARK) + rb" " + re.escape(nonce) + rb" ([0-9]*) ([0-9]{1,3})\n\Z")
    found = line.search(tail)
    if found is None:
        return None
    return (int(found[1]) if found[1] else None), int(found[2]), len(found[0])


def _kind(member: tarfile.TarInfo) -> str:
    if member.issym():
        return "symlink"
    if member.islnk():
        return "hard link"
    if member.isfifo():
        return "FIFO"
    if member.ischr():
        return "character device"
    if member.isblk():
        return "block device"
    return "member"


def read_tar(data: bytes) -> list[tarfile.TarInfo]:
    """FR-143: a transfer's members, refusing the whole stream for anything but regular files and
    directories at normalised relative paths, over 16 MiB, or over 10,000 members. Raises
    ValueError naming the member; nothing is extracted, here or anywhere on the host."""
    if len(data) > MAX_TRANSFER_BYTES:
        raise ValueError(f"the transfer is over {MAX_TRANSFER_BYTES} B")
    members: list[tarfile.TarInfo] = []
    kinds: dict[str, str] = {}  # each path's kind, its parents' included
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r|") as tar:
            for member in tar:
                if len(members) == MAX_TRANSFER_MEMBERS:
                    raise ValueError(f"the transfer holds over {MAX_TRANSFER_MEMBERS} members")
                if not (member.isfile() or member.isdir()) or member.issparse():
                    kind = "sparse file" if member.issparse() else _kind(member)
                    raise ValueError(f"{kind} {member.name!r} is not a regular file or directory")
                try:
                    tarfile.data_filter(member, _NOWHERE)
                    key = normal_path(member.name)
                except (tarfile.FilterError, ValueError) as exc:
                    raise ValueError(f"{member.name!r} is not a path inside the workspace: "
                                     f"{type(exc).__name__}") from None
                # Its size as stored is what it would become (pre-review P7), counted with the rest.
                total += member.size if member.isfile() else 0
                if total > MAX_TRANSFER_BYTES:
                    raise ValueError(f"the transfer is over {MAX_TRANSFER_BYTES} B")
                # One meaning for each path: never twice, never a file that is also a folder.
                kind = "file" if member.isfile() else "dir"
                if key and (kinds.get(key) == "file" or (kind == "file" and key in kinds)):
                    raise ValueError(f"{member.name!r} appears in the transfer twice, or as a file and a folder")
                parent = posixpath.dirname(key)
                while parent:
                    if kinds.setdefault(parent, "dir") == "file":
                        raise ValueError(f"{member.name!r} is under {parent!r}, a file in the same transfer")
                    parent = posixpath.dirname(parent)
                if key:
                    kinds[key] = kind
                members.append(member)
    except tarfile.TarError as exc:
        raise ValueError(f"not a tar stream: {type(exc).__name__}") from None
    return members


def import_tar(data: bytes) -> bytes:
    """FR-143: what goes into a workspace, rebuilt by the SDK from a checked stream -- each
    directory and regular file under `workspace/`, every parent given, all owned by 65534."""
    read_tar(data)
    files: dict[str, bytes] = {}
    dirs: set[str] = set()
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for member in tar.getmembers():
            key = normal_path(member.name)
            if member.isdir():
                if key:
                    dirs.add(key)
            else:
                files[key] = tar.extractfile(member).read()
    for key in list(files) + list(dirs):
        parent = posixpath.dirname(key)
        while parent:
            dirs.add(parent)
            parent = posixpath.dirname(parent)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as out:
        for key in sorted(dirs):
            info = tarfile.TarInfo(f"workspace/{key}")
            info.type, info.mode, info.uid, info.gid = tarfile.DIRTYPE, 0o755, UID, UID
            out.addfile(info)
        for key, content in sorted(files.items()):
            info = tarfile.TarInfo(f"workspace/{key}")
            info.size, info.mode, info.uid, info.gid = len(content), 0o644, UID, UID
            out.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def _root_tar() -> bytes:
    """The volume's root, as a `workspace/` entry owned by 65534: copied with `docker cp -a` into
    `/` of a container over the volume, it owns the root without running any process there."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo("workspace")
        info.type, info.mode, info.uid, info.gid = tarfile.DIRTYPE, 0o755, UID, UID
        tar.addfile(info)
    return buffer.getvalue()


class _Kept:
    """One output stream: read to its end, kept up to `keep` bytes, every byte counted (FR-144)."""

    def __init__(self, keep: int) -> None:
        self.keep, self.data, self.count = keep, bytearray(), 0
        self.tail = b""  # the stream's last TAIL_BYTES, whatever `keep` is (FR-156)

    async def drain(self, stream: asyncio.StreamReader) -> None:
        while chunk := await stream.read(_READ):
            self.count += len(chunk)
            room = self.keep - len(self.data)
            if room > 0:
                self.data += chunk[:room]
            self.tail = (self.tail + chunk)[-TAIL_BYTES:]

    def take_reading(self, nonce: bytes) -> tuple[bool, int | None]:
        """The wrapper's line, taken off what the command printed: whether a line carries the
        command's nonce, and its reading (None when the container gave none). The exit code the line
        names is the container's by construction, which is the one reported."""
        found = reading(self.tail, nonce)
        if found is None:
            return False, None
        usec, _, length = found
        self.count -= length
        del self.data[self.count:]
        return True, usec


@dataclass
class _Command:
    name: str
    kill: asyncio.Event = field(default_factory=asyncio.Event)
    network: str = "none"  # FR-160: its workspace's network, with egress
    assigned: tuple[str, ...] = ()  # and its proxy URL


@dataclass
class _Space:
    handle: WorkspaceHandle
    baseline: int = 0  # what the empty volume's root itself uses, which no command wrote
    # NFR-39 (M45 round 3, DECISION-8e21d1ff): a sandboxed server's engine keeps this provider, and a
    # run awaits each command's kill; each is held by its command's own call alone.
    running: weakref.WeakValueDictionary[str, _Command] = field(default_factory=weakref.WeakValueDictionary)
    # FR-160 (M44), with egress: the network commands join, the proxy's address on it, its attached
    # process and the task reading its reports, each command's token, and who is told of them.
    network: str = "none"
    address: str | None = None
    proxy: asyncio.subprocess.Process | None = None
    reader: asyncio.Task[None] | None = None
    tokens: dict[str, str] = field(default_factory=dict)
    watcher: Any = None
    stopping: bool = False  # its proxy asked to stop, so its end is no death
    noise: asyncio.Task[None] | None = None  # its stderr, drained so the client never stalls (F7)


@dataclass(frozen=True)
class _Ended:
    exit_code: int | None
    stdout: _Kept
    stderr: _Kept
    killed: str | None
    cpu_usec: int | None = None  # the wrapper's reading, when it gave one (FR-156)


class DockerProvider:
    """FR-142: workspaces as Docker volumes, each command a hardened container over one."""

    name = "docker"

    def __init__(self, docker: str = "docker", *, max_connections: int = egress.MAX_CONNECTIONS) -> None:
        self._cli_name = docker
        self._spaces: dict[str, _Space] = {}
        self._max_connections = max_connections  # each proxy's, as FakeProvider's (DECISION-f7114b8b)

    # --- the CLI ---------------------------------------------------------------------------------

    def _docker(self) -> str:
        found = shutil.which(self._cli_name)
        if found is None:
            raise WorkspaceUnavailable("the docker CLI was not found")
        return found

    @staticmethod
    def _env(spec_env: Mapping[str, str] | None = None) -> dict[str, str]:
        """The CLI's environment: the host's, for the CLI alone, plus the spec's values, which
        reach a container only by the names `-e` gives (FR-142, NFR-45)."""
        env = dict(os.environ)
        if spec_env:
            folded = {name.upper() for name in spec_env}
            env = {k: v for k, v in env.items() if k.upper() not in folded}
            env.update(spec_env)
        return env

    async def _spawn(self, args: list[str], *, env: dict[str, str] | None = None) -> asyncio.subprocess.Process:
        if _too_long(args):
            raise ValueError("the docker command line would pass the 32,767 characters Windows allows")
        try:
            return await asyncio.create_subprocess_exec(
                self._docker(), *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=env if env is not None else self._env())
        except WorkspaceUnavailable:
            raise
        except OSError as exc:
            raise WorkspaceUnavailable(f"the docker CLI could not be started: {type(exc).__name__}") from None

    async def _cli(self, *args: str, stdin: bytes | None = None, env: dict[str, str] | None = None,
                   timeout: float = _CLI_SECONDS, keep: int = 2**20) -> tuple[int, bytes, bytes]:
        """One docker call that is not a command: its exit code and output, kept up to `keep`."""
        process = await self._spawn(list(args), env=env)
        out, err = _Kept(keep), _Kept(keep)

        async def talk() -> int:
            async def feed() -> None:
                with contextlib.suppress(OSError):
                    if stdin:
                        process.stdin.write(stdin)
                        await process.stdin.drain()
                    process.stdin.close()

            await asyncio.gather(feed(), out.drain(process.stdout), err.drain(process.stderr))
            return await process.wait()

        try:
            code = await asyncio.wait_for(talk(), timeout)
        except BaseException:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await through(process.wait())
            raise
        return code, bytes(out.data), bytes(err.data)

    async def _rm(self, *names: str) -> None:
        # -v: with any volume the image declares, which no one else could ever remove (M39 review).
        if names:
            with contextlib.suppress(Exception):
                await self._cli("rm", "-fv", *names)

    def _space(self, handle: WorkspaceHandle) -> _Space:
        space = self._spaces.get(handle.workspace_id)
        if space is None:
            raise WorkspaceUnavailable(f"workspace {handle.workspace_id} does not exist")
        return space

    # --- workspaces ------------------------------------------------------------------------------

    async def create(self, spec: WorkspaceSpec, labels: Mapping[str, str]) -> WorkspaceHandle:
        refusal = env_refusal(spec.env)
        if refusal is not None:
            raise WorkspaceUnavailable(refusal)
        workspace_id = labels.get("agentsdk.workspace") or str(uuid.uuid4())
        record = {k: v for k, v in spec.record().items() if k not in ("env", "run_command_approval")}
        labels = {**labels, "agentsdk.workspace": workspace_id, "agentsdk.spec": json.dumps(record, sort_keys=True)}
        handle = WorkspaceHandle(workspace_id=workspace_id, spec=spec, labels=MappingProxyType(labels))
        # The daemon first, so an unreachable one is not reported as a missing image (pre-review P6).
        code, _, _ = await self._cli("version", "--format", "{{.Server.Version}}")
        if code != 0:
            raise WorkspaceUnavailable(f"the docker daemon could not be reached (exit {code})")
        code, _, _ = await self._cli("image", "inspect", "--format", "{{.Id}}", spec.image)
        if code != 0:
            raise WorkspaceUnavailable("the image is not on this host, and a provider never pulls it")
        code, _, _ = await self._cli("volume", "create", *_labels(labels), volume_name(workspace_id))
        if code != 0:
            raise WorkspaceUnavailable(f"docker could not create the workspace's volume (exit {code})")
        space = _Space(handle)
        try:
            async with self._helper(handle) as helper:
                code, _, _ = await self._cli("cp", "-a", "-", f"{helper}:/", stdin=_root_tar())
            if code != 0:
                raise WorkspaceUnavailable(f"docker could not prepare the workspace (exit {code})")
            self._spaces[workspace_id] = space
            try:
                space.baseline = await self._du(handle)
            except Exception as exc:  # noqa: BLE001 - named by class only
                raise WorkspaceUnavailable(f"the image cannot measure a workspace: {type(exc).__name__}") from None
            if spec.egress:
                await self._start_egress(handle, space)
        except BaseException:
            self._spaces.pop(workspace_id, None)
            await through(self._discard(space, workspace_id))
            raise
        return handle

    # --- egress (FR-160 to FR-162) -----------------------------------------------------------------

    async def _start_egress(self, handle: WorkspaceHandle, space: _Space) -> None:
        """The workspace's internal network, its gateway isolated, and its proxy, on it and on the
        bridge, listening on that network alone, started attached so its reports reach the SDK
        alone; ready once it says it is listening under a python3 it can run on."""
        workspace_id, labels = handle.workspace_id, _egress_labels(handle)
        network, name = network_name(workspace_id), proxy_name(workspace_id)
        code, out, _ = await self._cli("version", "--format", "{{.Server.Version}}")
        refusal = engine_refusal(out.decode().strip() if code == 0 else "")
        if refusal is not None:
            raise WorkspaceUnavailable(refusal)
        # F1: no gateway address, so no command reaches the host through it (DECISION-26009e04).
        code, _, _ = await self._cli("network", "create", "--internal", "-o",
                                     "com.docker.network.bridge.gateway_mode_ipv4=isolated", *labels, network)
        if code != 0:
            raise WorkspaceUnavailable(f"docker could not create the workspace's network (exit {code})")
        space.network = network
        code, out, _ = await self._cli("network", "inspect", "--format", "{{(index .IPAM.Config 0).Subnet}}", network)
        subnet = out.decode().strip()
        if code != 0 or not subnet:
            raise WorkspaceUnavailable("the workspace's network has no subnet")
        args = proxy_args(handle, subnet, self._max_connections)
        if _too_long(args):
            # F5: refused as the workspace's, never a raw ValueError (DECISION-a7a1b37b).
            raise WorkspaceUnavailable("the workspace's proxy would pass the docker command line's bound: "
                                       "its egress list is too long")
        code, _, _ = await self._cli(*args)
        if code != 0:
            raise WorkspaceUnavailable(f"docker could not create the workspace's proxy (exit {code})")
        code, _, _ = await self._cli("network", "connect", "bridge", name)
        if code != 0:
            raise WorkspaceUnavailable(f"docker could not connect the workspace's proxy (exit {code})")
        space.proxy = await self._spawn(["start", "-a", name])
        space.noise = asyncio.ensure_future(_Kept(4096).drain(space.proxy.stderr))
        try:
            first = await asyncio.wait_for(space.proxy.stdout.readline(), _CLI_SECONDS)
        except asyncio.TimeoutError:
            first = b""
        refusal = ready_refusal(first)
        if refusal is not None:
            raise WorkspaceUnavailable(refusal)
        code, out, _ = await self._cli("inspect", "--format", "{{json .NetworkSettings.Networks}}", name)
        try:
            space.address = json.loads(out)[network]["IPAddress"] if code == 0 else None
        except (ValueError, KeyError, TypeError):
            space.address = None
        if not space.address:
            raise WorkspaceUnavailable("the workspace's proxy has no address on its network")
        space.reader = asyncio.ensure_future(self._read_reports(space))

    async def _read_reports(self, space: _Space) -> None:
        """FR-162: the proxy's own reports, a JSON line each, its tokens made command ids; what is not
        one is not a report. Its output ending when no stop was asked is its death: reported `ended`
        (F2, DECISION-26009e04)."""
        while line := await space.proxy.stdout.readline():
            try:
                report = json.loads(line)
            except ValueError:
                continue
            if not isinstance(report, dict) or report.get("kind") not in ("connection", "end", "limit"):
                continue
            token = report.pop("token", None)
            if report["kind"] == "connection":
                report["command_id"] = space.tokens.get(token) if isinstance(token, str) else None
            if space.watcher is not None:
                with contextlib.suppress(Exception):
                    space.watcher(report)
        if not space.stopping and space.watcher is not None:
            with contextlib.suppress(Exception):
                space.watcher({"kind": "ended", "at": egress._now()})

    def watch_egress(self, handle: WorkspaceHandle, callback: Any) -> None:
        """FR-162: `callback` gets each report of the workspace's proxy, its token made a command id."""
        self._space(handle).watcher = callback

    async def _stop_egress(self, space: _Space) -> None:
        """The proxy asked to stop, so each open tunnel reports its bytes, and its reports read to the
        end; its container, and the network, go with the workspace's sweep."""
        if space.proxy is None:
            return
        space.stopping = True
        await self._cli("stop", "-t", "5", proxy_name(space.handle.workspace_id))
        if space.reader is not None:
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(asyncio.shield(space.reader), _CLI_SECONDS)
            space.reader.cancel()
        if space.proxy.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                space.proxy.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(space.proxy.wait(), _CLI_SECONDS)
        if space.noise is not None:
            space.noise.cancel()  # it writes nothing: it only reads to the stream's end

    @contextlib.asynccontextmanager
    async def _helper(self, handle: WorkspaceHandle) -> AsyncIterator[str]:
        """A container over the volume that is created and never started: what `docker cp` reads
        from and writes into (FR-143)."""
        name = f"agentsdk-xfer-{uuid.uuid4()}"
        try:
            code, _, _ = await self._cli("create", "--pull", "never", "--name", name, *_labels(handle.labels),
                                         "--network", "none", *_mount(handle), "--entrypoint=", handle.spec.image,
                                         "true")
            if code != 0:
                raise WorkspaceUnavailable(f"docker could not reach the workspace (exit {code})")
            yield name
        finally:
            await through(self._rm(name))

    async def _alive(self, name: str) -> bool:
        code, _, _ = await self._cli("inspect", "--format", "{{.Id}}", name)
        return code == 0

    # --- commands --------------------------------------------------------------------------------

    async def server_launch(self, handle: WorkspaceHandle, command: str, args: list[str]) -> ServerLaunch:
        """FR-165: a sandboxed server's container, started by the MCP client as `docker run -i`.
        One from before -- a crash's restart (DECISION-b478705b) -- is removed first, whether or
        not it heeded its stdin's end. With egress it joins its workspace's network with a proxy
        URL whose token names no command, so its connections are recorded with none
        (DECISION-61aed587)."""
        space = self._space(handle)
        code, out, _ = await self._cli("ps", "-aq", "--filter", f"label=agentsdk.workspace={handle.workspace_id}",
                                       "--filter", f"label={SERVER_ROLE}=server")
        if code == 0 and out.split():
            await self._rm(*out.decode().split())
        assigned: tuple[str, ...] = ()
        if space.address is not None:
            url = f"http://{egress.USER}:{secrets.token_hex(16)}@{space.address}:{egress.PORT}"
            assigned = (f"HTTPS_PROXY={url}", f"https_proxy={url}")
        args_ = server_args(handle, f"agentsdk-srv-{uuid.uuid4()}", command, list(args), sorted(handle.spec.env),
                            space.network, assigned)
        if _too_long(args_):
            raise WorkspaceUnavailable("the server's docker command line would pass the 32,767 characters Windows allows")
        return ServerLaunch(self._docker(), tuple(args_), self._env(handle.spec.env), None)

    async def run(self, handle: WorkspaceHandle, argv: list[str], *, timeout: float, stdin: bytes | None = None,
                  command_id: str | None = None) -> CommandOutcome:
        space = self._space(handle)
        started = time.monotonic()
        if not argv or not argv[0]:
            message = b"command not found\n"
            return CommandOutcome(exit_code=127, stdout=b"", stderr=message, stdout_cut=False, stderr_cut=False,
                                  stdout_bytes=0, stderr_bytes=len(message), duration=0.0)
        command_id = command_id or str(uuid.uuid4())
        command = _Command(f"agentsdk-cmd-{uuid.uuid4()}")
        assigned: tuple[str, ...] = ()
        token = None
        if space.address is not None:
            # FR-160: its own token, which names it in the proxy's reports (DECISION-88dea779).
            token = secrets.token_hex(16)
            space.tokens[token] = command_id
            url = f"http://{egress.USER}:{token}@{space.address}:{egress.PORT}"
            assigned = (f"HTTPS_PROXY={url}", f"https_proxy={url}")
        command.network, command.assigned = space.network, assigned
        if _too_long(container_args(handle, command.name, list(argv), sorted(handle.spec.env), space.network,
                                    assigned)):
            # As exec reports E2BIG: the model's own arguments, never docker's absence (pre-review P4).
            space.tokens.pop(token, None)
            message = b"argument list too long: the command line would pass the 32,767 characters Windows allows\n"
            return CommandOutcome(exit_code=126, stdout=b"", stderr=message, stdout_cut=False, stderr_cut=False,
                                  stdout_bytes=0, stderr_bytes=len(message), duration=time.monotonic() - started)
        space.running[command_id] = command
        try:
            ended = await self._contained(handle, command, list(argv), timeout=timeout, stdin=stdin,
                                          env=handle.spec.env, keep=handle.spec.max_output_bytes)
        finally:
            space.running.pop(command_id, None)
            # F4: a token names its command only while it runs (DECISION-26009e04).
            space.tokens.pop(token, None)
        out, err = ended.stdout, ended.stderr
        duration = time.monotonic() - started
        return CommandOutcome(
            exit_code=ended.exit_code, stdout=bytes(out.data), stderr=bytes(err.data),
            stdout_cut=out.count > len(out.data), stderr_cut=err.count > len(err.data), stdout_bytes=out.count,
            stderr_bytes=err.count, duration=duration, killed=ended.killed,
            # Docker ends a command at a limit only when it runs out of memory; a fork past
            # `pids` fails inside it, and is not told (DECISION-21e7f6fb).
            limit="memory_bytes" if ended.killed == "limit" else None,
            cpu_seconds=charged(None if ended.cpu_usec is None else ended.cpu_usec / 1_000_000, ended.killed,
                                duration, handle.spec.cpus))

    async def _contained(self, handle: WorkspaceHandle, command: _Command, argv: list[str], *, timeout: float,
                         stdin: bytes | None, env: Mapping[str, str], keep: int) -> _Ended:
        """One container's life: created, started attached, inspected, removed -- removed whatever
        ends it, a cancellation included, before this returns or raises."""
        started = time.monotonic()
        out, err = _Kept(keep), _Kept(keep)
        process: asyncio.subprocess.Process | None = None
        nonce = _nonce().encode()
        try:
            code, _, _ = await self._cli(*container_args(handle, command.name, argv, sorted(env), command.network,
                                                        command.assigned), env=self._env(env))
            if command.kill.is_set():
                return _Ended(None, out, err, "cancelled")
            if code != 0:
                raise WorkspaceUnavailable(f"docker could not create the command's container (exit {code})")
            process = await self._spawn(["start", "-a", "-i", command.name])

            async def feed() -> None:
                # The nonce is stdin's first line, which the wrapper reads; the command gets the rest.
                with contextlib.suppress(OSError):
                    process.stdin.write(nonce + b"\n")
                    if stdin:
                        process.stdin.write(stdin)
                    await process.stdin.drain()
                    process.stdin.close()

            talking = asyncio.ensure_future(asyncio.gather(
                feed(), out.drain(process.stdout), err.drain(process.stderr)))
            waiting = asyncio.ensure_future(process.wait())
            killing = asyncio.ensure_future(command.kill.wait())
            try:
                await asyncio.wait({waiting, killing}, timeout=max(0.0, timeout - (time.monotonic() - started)),
                                   return_when=asyncio.FIRST_COMPLETED)
            finally:
                killing.cancel()
            killed = "cancelled" if command.kill.is_set() else None if waiting.done() else "timeout"
            if killed is not None:
                await self._rm(command.name)  # the only total kill (P5-D5)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(waiting), _CLI_SECONDS)
            with contextlib.suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(talking, _CLI_SECONDS)
            if killed is not None:
                # The wrapper may have printed its line just before the kill: the model never
                # reads it, and the command is charged its ceiling all the same (F2).
                out.take_reading(nonce)
                return _Ended(None, out, err, killed)
            code, state, _ = await self._cli("inspect", "--format", "{{json .State}}", command.name)
            if code != 0:
                if command.kill.is_set():
                    return _Ended(None, out, err, "cancelled")
                raise WorkspaceUnavailable("the command's container could not be inspected")
            state = json.loads(state)
            exit_code = int(state["ExitCode"])
            found, usec = out.take_reading(nonce)
            # OOMKilled says any process in the container was killed for memory: the command was
            # ended at its bound only when its own process went by SIGKILL; otherwise it ended on
            # its own, and keeps its exit code (M42 pre-review F6, DECISION-4689af02).
            if state.get("OOMKilled") and exit_code == 137:
                return _Ended(None, out, err, "limit")
            if not found:
                # No line carrying its nonce, so the wrapper did not finish: the container's exit code
                # may be PID 1's, not the command's, so none is reported (DECISION-88395b13).
                return _Ended(None, out, err, "error")
            return _Ended(exit_code, out, err, None, usec)
        finally:
            await through(self._cleaned(command.name, process))

    async def _cleaned(self, name: str, process: asyncio.subprocess.Process | None) -> None:
        """A command's container removed, then its `docker start -a` client killed and waited for,
        bounded: one step under `through`, so a cancel held during the removal never skips the
        kill (DECISION-1e35a472)."""
        await self._rm(name)
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.wait(), _CLI_SECONDS)

    async def kill(self, handle: WorkspaceHandle, command_id: str) -> None:
        space = self._spaces.get(handle.workspace_id)
        command = None if space is None else space.running.get(command_id)
        if command is None:
            return
        command.kill.set()
        code, _, _ = await self._cli("rm", "-fv", command.name)
        # By state, not by docker's message: a container already gone was killed (FR-148). One the
        # daemon cannot say is gone is not taken as gone (M40 round 1, DECISION-f3e99c07).
        if code != 0:
            if await self._alive(command.name):
                raise WorkspaceUnavailable(f"docker could not remove the command's container (exit {code})")
            code, _, _ = await self._cli("version", "--format", "{{.Server.Version}}")
            if code != 0:
                raise WorkspaceUnavailable("the docker daemon could not say whether the command's container is gone")

    # --- the measured bound (FR-144) -------------------------------------------------------------

    async def _du(self, handle: WorkspaceHandle) -> int:
        """What the volume uses on disk, in bytes, read by `du -sk` in a container hardened as a
        command is (DECISION-504c7f04). Raises when it cannot be read: the manager then refuses
        every later command and write (fail closed)."""
        ended = await self._contained(handle, _Command(f"agentsdk-du-{uuid.uuid4()}"), ["du", "-sk", MOUNT],
                                      timeout=_MEASURE_SECONDS, stdin=None, env={}, keep=4096)
        if ended.killed is not None or ended.exit_code != 0:
            raise WorkspaceUnavailable(f"du ended with exit code {ended.exit_code}, killed {ended.killed}")
        try:
            return int(bytes(ended.stdout.data).split()[0]) * 1024
        except (IndexError, ValueError):
            raise WorkspaceUnavailable("du printed no size") from None

    async def size(self, handle: WorkspaceHandle) -> int:
        space = self._space(handle)
        return max(0, await self._du(handle) - space.baseline)

    # --- listing (DECISION-0004d2dc) -------------------------------------------------------------

    async def list(self, handle: WorkspaceHandle, path: str, *,
                   limit: int = 1000) -> tuple[list[tuple[str, str, int]], int]:
        """A folder's direct entries, read by `sh` and `stat` in a container hardened as a command
        is: links, FIFOs and devices listed as what they are, and no file's contents transferred."""
        key = normal_path(path)
        self._space(handle)
        ended = await self._contained(handle, _Command(f"agentsdk-ls-{uuid.uuid4()}"),
                                      ["sh", "-c", _LIST, "sh", key, str(int(limit))],
                                      timeout=_CLI_SECONDS, stdin=None, env={}, keep=8 * 2**20)
        if ended.exit_code == 20:
            raise FileNotFoundError(path)
        if ended.exit_code == 21:
            raise NotADirectoryError(path)
        if ended.exit_code in (22, 23):
            raise PermissionError(path)  # the folder, or one of its entries, cannot be read
        out = bytes(ended.stdout.data)
        if ended.killed is not None or ended.exit_code != 0 or ended.stdout.count > len(out):
            raise WorkspaceUnavailable(f"the listing ended with exit code {ended.exit_code}, killed {ended.killed}")
        records = [r.decode("utf-8", errors="replace").split("\t", 2) for r in out.split(b"\0") if r]
        if not records or records[-1][0] != "total":
            raise WorkspaceUnavailable("the listing printed no count")
        found = sorted((name, kind, int(size)) for kind, size, name in records[:-1])
        return found, int(records[-1][1])

    # --- transfer (FR-143) -----------------------------------------------------------------------

    async def export(self, handle: WorkspaceHandle, path: str) -> bytes:
        key = normal_path(path)
        self._space(handle)
        async with self._helper(handle) as helper:
            source = f"{helper}:{MOUNT}/{key}" if key else f"{helper}:{MOUNT}/."
            process = await self._spawn(["cp", source, "-"])
            process.stdin.close()
            data, noise = bytearray(), _Kept(4096)

            async def read() -> None:
                # Bounded while it is read: past the bound the copy is ended, never finished (FR-143).
                while chunk := await process.stdout.read(_READ):
                    data.extend(chunk)
                    if len(data) > MAX_TRANSFER_BYTES:
                        with contextlib.suppress(ProcessLookupError):
                            process.kill()
                        return

            try:
                await asyncio.wait_for(asyncio.gather(read(), noise.drain(process.stderr)), _CLI_SECONDS * 2)
            finally:
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                await through(process.wait())
            if len(data) > MAX_TRANSFER_BYTES:
                raise ValueError(f"the transfer is over {MAX_TRANSFER_BYTES} B")
            if process.returncode != 0:
                # By state, not by docker's message: the helper still exists, so the path does not.
                if await self._alive(helper):
                    raise FileNotFoundError(path)
                raise WorkspaceUnavailable("docker could not read the workspace")
        read_tar(bytes(data))
        return bytes(data)

    async def import_(self, handle: WorkspaceHandle, data: bytes) -> None:
        archive = import_tar(data)  # checked and rebuilt before docker sees it
        self._space(handle)
        async with self._helper(handle) as helper:
            code, _, _ = await self._cli("cp", "-a", "-", f"{helper}:/", stdin=archive)
            if code != 0:
                if await self._alive(helper):
                    raise ValueError("the files could not be written into the workspace")
                raise WorkspaceUnavailable("docker could not write the workspace")

    # --- removal and listing ---------------------------------------------------------------------

    async def remove(self, handle: WorkspaceHandle) -> None:
        space = self._spaces.pop(handle.workspace_id, None)
        if space is not None:
            for command in list(space.running.values()):
                command.kill.set()
            await through(self._discard(space, handle.workspace_id))
            return
        await self._remove(handle.workspace_id)

    async def _discard(self, space: _Space, workspace_id: str) -> None:
        """Its proxy stopped, then everything it holds removed: one step under `through`, so a
        cancel held while the proxy stops never skips the removal (DECISION-1e35a472)."""
        await self._stop_egress(space)
        await self._remove(workspace_id)

    async def _remove(self, workspace_id: str) -> None:
        """Every container over the workspace, by its label -- its proxy's too -- then its volume and
        its network. A container created while this runs still holds the volume, so the sweep is
        repeated a few times."""
        for attempt in range(_REMOVE_ATTEMPTS):
            code, out, _ = await self._cli("ps", "-aq", "--filter", f"label=agentsdk.workspace={workspace_id}")
            if code == 0:
                await self._rm(*out.decode().split())
            code, _, _ = await self._cli("volume", "rm", "-f", volume_name(workspace_id))
            if code == 0 and await self._network_gone(workspace_id):
                return
            await asyncio.sleep(0.5 * (attempt + 1))
        raise WorkspaceUnavailable(f"docker could not remove the workspace's volume or network (exit {code})")

    async def _network_gone(self, workspace_id: str) -> bool:
        """FR-160: the workspace's network removed, or never made."""
        code, out, _ = await self._cli("network", "ls", "-q", "--filter", f"label=agentsdk.workspace={workspace_id}")
        if code != 0:
            return False
        found = out.decode().split()
        if found:
            code, _, _ = await self._cli("network", "rm", *found)
            return code == 0
        return True

    async def list_owned(self, labels: Mapping[str, str]) -> list[WorkspaceHandle]:
        filters = [arg for key, value in sorted(labels.items()) for arg in ("--filter", f"label={key}={value}")]
        code, out, _ = await self._cli("volume", "ls", "-q", "--filter", "label=agentsdk.workspace", *filters)
        if code != 0:
            raise WorkspaceUnavailable(f"docker could not list volumes (exit {code})")
        names = out.decode().split()
        # A labelled container whose volume is gone is listed too, by its own labels, so reaping
        # and aclose() remove it (NFR-44; M41 pre-review, DECISION-b1498111).
        code, out, _ = await self._cli("ps", "-aq", "--filter", "label=agentsdk.workspace", *filters)
        if code != 0:
            raise WorkspaceUnavailable(f"docker could not list containers (exit {code})")
        containers = out.decode().split()
        orphans: dict[str, dict[str, str]] = {}
        for start in range(0, len(containers), 100):
            # A container removed meanwhile fails the call, and the others are still printed.
            _, out, _ = await self._cli("inspect", *containers[start:start + 100], keep=16 * 2**20)
            try:
                inspected = json.loads(out) if out.strip() else []
            except ValueError:
                continue  # found again by the next listing
            for container in inspected:
                found = dict((container.get("Config") or {}).get("Labels") or {})
                workspace_id = found.get("agentsdk.workspace", "")
                if workspace_id and volume_name(workspace_id) not in names:
                    orphans.setdefault(workspace_id, found)
        # And a labelled network whose volume is gone, so it goes too (FR-160).
        code, out, _ = await self._cli("network", "ls", "-q", "--filter", "label=agentsdk.workspace", *filters)
        if code != 0:
            raise WorkspaceUnavailable(f"docker could not list networks (exit {code})")
        networks = out.decode().split()
        for start in range(0, len(networks), 100):
            _, out, _ = await self._cli("network", "inspect", *networks[start:start + 100], keep=16 * 2**20)
            try:
                inspected = json.loads(out) if out.strip() else []
            except ValueError:
                continue
            for network in inspected:
                found = dict(network.get("Labels") or {})
                workspace_id = found.get("agentsdk.workspace", "")
                if workspace_id and volume_name(workspace_id) not in names:
                    orphans.setdefault(workspace_id, found)
        volumes: list[dict[str, Any]] = []
        for start in range(0, len(names), 100):  # each call well inside a command line (pre-review P4)
            code, out, _ = await self._cli("volume", "inspect", *names[start:start + 100], keep=16 * 2**20)
            if code != 0:
                raise WorkspaceUnavailable(f"docker could not inspect volumes (exit {code})")
            volumes += json.loads(out)
        handles = []
        for volume in volumes:
            found = dict(volume.get("Labels") or {})
            known = self._spaces.get(found.get("agentsdk.workspace", ""))
            if known is not None:
                handles.append(known.handle)
                continue
            handles.append(WorkspaceHandle(workspace_id=found["agentsdk.workspace"], spec=_recorded(found),
                                           labels=MappingProxyType(found)))
        for workspace_id, found in orphans.items():
            handles.append(WorkspaceHandle(workspace_id=workspace_id, spec=_recorded(found),
                                           labels=MappingProxyType(found)))
        return sorted(handles, key=lambda h: h.workspace_id)


# A listed workspace whose labels name no spec this SDK can read: enough to remove it (FR-151).
_UNKNOWN_SPEC = WorkspaceSpec(image="unknown/workspace@sha256:" + "0" * 64)


def _recorded(labels: Mapping[str, str]) -> WorkspaceSpec:
    """The spec a volume's labels record, or `_UNKNOWN_SPEC`: one foreign, old or damaged label
    never fails a listing (pre-review P5)."""
    try:
        record = json.loads(labels.get("agentsdk.spec") or "")
        fields = {k: v for k, v in record.items() if k in WorkspaceSpec.__dataclass_fields__}
        if "egress" in fields:
            fields["egress"] = tuple(fields["egress"])
        return WorkspaceSpec(**fields)
    except Exception:  # noqa: BLE001 - whatever a label holds
        return _UNKNOWN_SPEC


def _too_long(args: list[str]) -> bool:
    return sys.platform == "win32" and len(subprocess.list2cmdline(args)) > _COMMAND_LINE
