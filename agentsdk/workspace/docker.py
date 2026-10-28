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
import shutil
import subprocess
import sys
import tarfile
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from ..errors import WorkspaceUnavailable
from . import CommandOutcome, WorkspaceHandle, WorkspaceSpec, normal_path

MOUNT = "/workspace"
UID = 65534
MAX_TRANSFER_BYTES = 16 * 2**20  # FR-143
MAX_TRANSFER_MEMBERS = 10_000
_CLI_SECONDS = 60.0  # any docker call that is not a command
_MEASURE_SECONDS = 60.0
_REMOVE_ATTEMPTS = 4
_READ = 64 * 1024
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
# 21: not a folder (a link to one included); 23: an entry could not be read.
_LIST = r"""d="/workspace/$1"
if [ ! -e "$d" ] && [ ! -L "$d" ]; then exit 20; fi
if [ -L "$d" ] || [ ! -d "$d" ]; then exit 21; fi
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


def hardening(spec: WorkspaceSpec) -> list[str]:
    """P5-D6: every command container's limits, exactly these; nothing a model or a spec can reach
    adds to them or takes one away."""
    return [
        "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only",
        "--tmpfs", f"/tmp:size={spec.tmp_bytes}", "--user", f"{UID}:{UID}", "--pids-limit", str(spec.pids),
        "--memory", str(spec.memory_bytes), "--memory-swap", str(spec.memory_bytes), "--cpus", repr(float(spec.cpus)),
    ]


def volume_name(workspace_id: str) -> str:
    return f"agentsdk-{workspace_id}"


def _labels(labels: Mapping[str, str]) -> list[str]:
    return [arg for key, value in sorted(labels.items()) for arg in ("--label", f"{key}={value}")]


def _mount(handle: WorkspaceHandle) -> list[str]:
    # P5-D4: a volume, never a bind mount; volume-nocopy keeps the root's owner.
    return ["--mount", f"type=volume,src={volume_name(handle.workspace_id)},dst={MOUNT},volume-nocopy"]


def container_args(handle: WorkspaceHandle, name: str, argv: list[str], env_names: list[str]) -> list[str]:
    """FR-142: `docker create` for one command. The image is by digest and never pulled; the
    entrypoint is cleared so `argv` runs as given, without a shell; each env value is named only
    (`-e NAME`) and given in the CLI's own environment, so none is on a command line."""
    return [
        "create", "--pull", "never", "--name", name, "-i", *_labels(handle.labels), *hardening(handle.spec),
        "-w", MOUNT, *_mount(handle), *[arg for env in env_names for arg in ("-e", env)],
        "--entrypoint=", handle.spec.image, *argv,
    ]


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


async def _through(awaitable: Awaitable[Any]) -> Any:
    """Finishes `awaitable` even when the caller is cancelled meanwhile, then re-raises that
    cancellation: a container's removal, once begun, is never abandoned."""
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError()
    return task.result()


class _Kept:
    """One output stream: read to its end, kept up to `keep` bytes, every byte counted (FR-144)."""

    def __init__(self, keep: int) -> None:
        self.keep, self.data, self.count = keep, bytearray(), 0

    async def drain(self, stream: asyncio.StreamReader) -> None:
        while chunk := await stream.read(_READ):
            self.count += len(chunk)
            room = self.keep - len(self.data)
            if room > 0:
                self.data += chunk[:room]


@dataclass
class _Command:
    name: str
    kill: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class _Space:
    handle: WorkspaceHandle
    baseline: int = 0  # what the empty volume's root itself uses, which no command wrote
    running: dict[str, _Command] = field(default_factory=dict)


@dataclass(frozen=True)
class _Ended:
    exit_code: int | None
    stdout: _Kept
    stderr: _Kept
    killed: str | None


class DockerProvider:
    """FR-142: workspaces as Docker volumes, each command a hardened container over one."""

    name = "docker"

    def __init__(self, docker: str = "docker") -> None:
        self._cli_name = docker
        self._spaces: dict[str, _Space] = {}

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
                await _through(process.wait())
            raise
        return code, bytes(out.data), bytes(err.data)

    async def _rm(self, *names: str) -> None:
        if names:
            with contextlib.suppress(Exception):
                await self._cli("rm", "-f", *names)

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
        except BaseException:
            self._spaces.pop(workspace_id, None)
            await _through(self._remove(workspace_id))
            raise
        return handle

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
            await _through(self._rm(name))

    async def _alive(self, name: str) -> bool:
        code, _, _ = await self._cli("inspect", "--format", "{{.Id}}", name)
        return code == 0

    # --- commands --------------------------------------------------------------------------------

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
        if _too_long(container_args(handle, command.name, list(argv), sorted(handle.spec.env))):
            # As exec reports E2BIG: the model's own arguments, never docker's absence (pre-review P4).
            message = b"argument list too long: the command line would pass the 32,767 characters Windows allows\n"
            return CommandOutcome(exit_code=126, stdout=b"", stderr=message, stdout_cut=False, stderr_cut=False,
                                  stdout_bytes=0, stderr_bytes=len(message), duration=time.monotonic() - started)
        space.running[command_id] = command
        try:
            ended = await self._contained(handle, command, list(argv), timeout=timeout, stdin=stdin,
                                          env=handle.spec.env, keep=handle.spec.max_output_bytes)
        finally:
            space.running.pop(command_id, None)
        out, err = ended.stdout, ended.stderr
        return CommandOutcome(
            exit_code=ended.exit_code, stdout=bytes(out.data), stderr=bytes(err.data),
            stdout_cut=out.count > len(out.data), stderr_cut=err.count > len(err.data), stdout_bytes=out.count,
            stderr_bytes=err.count, duration=time.monotonic() - started, killed=ended.killed)

    async def _contained(self, handle: WorkspaceHandle, command: _Command, argv: list[str], *, timeout: float,
                         stdin: bytes | None, env: Mapping[str, str], keep: int) -> _Ended:
        """One container's life: created, started attached, inspected, removed -- removed whatever
        ends it, a cancellation included, before this returns or raises."""
        started = time.monotonic()
        out, err = _Kept(keep), _Kept(keep)
        process: asyncio.subprocess.Process | None = None
        try:
            code, _, _ = await self._cli(*container_args(handle, command.name, argv, sorted(env)),
                                         env=self._env(env))
            if command.kill.is_set():
                return _Ended(None, out, err, "cancelled")
            if code != 0:
                raise WorkspaceUnavailable(f"docker could not create the command's container (exit {code})")
            process = await self._spawn(["start", "-a", "-i", command.name])

            async def feed() -> None:
                with contextlib.suppress(OSError):
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
                return _Ended(None, out, err, killed)
            code, state, _ = await self._cli("inspect", "--format", "{{json .State}}", command.name)
            if code != 0:
                if command.kill.is_set():
                    return _Ended(None, out, err, "cancelled")
                raise WorkspaceUnavailable("the command's container could not be inspected")
            state = json.loads(state)
            if state.get("OOMKilled"):
                return _Ended(None, out, err, "limit")
            return _Ended(int(state["ExitCode"]), out, err, None)
        finally:
            await _through(self._rm(command.name))
            if process is not None and process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                with contextlib.suppress(Exception):
                    await _through(asyncio.wait_for(process.wait(), _CLI_SECONDS))

    async def kill(self, handle: WorkspaceHandle, command_id: str) -> None:
        space = self._spaces.get(handle.workspace_id)
        command = None if space is None else space.running.get(command_id)
        if command is None:
            return
        command.kill.set()
        await self._rm(command.name)

    # --- the measured bound (FR-144) -------------------------------------------------------------

    async def _du(self, handle: WorkspaceHandle) -> int:
        """What the volume uses on disk, in bytes, read by `du -sk` in a container hardened as a
        command is (DECISION-504c7f04). Raises when it cannot be read: the manager then refuses
        every later command and write (fail closed)."""
        ended = await self._contained(handle, _Command(f"agentsdk-du-{uuid.uuid4()}"), ["du", "-sk", MOUNT],
                                      timeout=_MEASURE_SECONDS, stdin=None, env={}, keep=4096)
        if ended.killed is not None or ended.exit_code != 0:
            raise RuntimeError(f"du ended with exit code {ended.exit_code}, killed {ended.killed}")
        try:
            return int(bytes(ended.stdout.data).split()[0]) * 1024
        except (IndexError, ValueError):
            raise RuntimeError("du printed no size") from None

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
        out = bytes(ended.stdout.data)
        if ended.killed is not None or ended.exit_code != 0 or ended.stdout.count > len(out):
            raise RuntimeError(f"the listing ended with exit code {ended.exit_code}, killed {ended.killed}")
        records = [r.decode("utf-8", errors="replace").split("\t", 2) for r in out.split(b"\0") if r]
        if not records or records[-1][0] != "total":
            raise RuntimeError("the listing printed no count")
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
                await _through(process.wait())
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
        await self._remove(handle.workspace_id)

    async def _remove(self, workspace_id: str) -> None:
        """Every container over the workspace, by its label, then its volume. A container created
        while this runs still holds the volume, so the sweep is repeated a few times."""
        for attempt in range(_REMOVE_ATTEMPTS):
            code, out, _ = await self._cli("ps", "-aq", "--filter", f"label=agentsdk.workspace={workspace_id}")
            if code == 0:
                await self._rm(*out.decode().split())
            code, _, _ = await self._cli("volume", "rm", "-f", volume_name(workspace_id))
            if code == 0:
                return
            await asyncio.sleep(0.5 * (attempt + 1))
        raise WorkspaceUnavailable(f"docker could not remove the workspace's volume (exit {code})")

    async def list_owned(self, labels: Mapping[str, str]) -> list[WorkspaceHandle]:
        filters = [arg for key, value in sorted(labels.items()) for arg in ("--filter", f"label={key}={value}")]
        code, out, _ = await self._cli("volume", "ls", "-q", "--filter", "label=agentsdk.workspace", *filters)
        if code != 0:
            raise WorkspaceUnavailable(f"docker could not list volumes (exit {code})")
        names = out.decode().split()
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
        return sorted(handles, key=lambda h: h.workspace_id)


# A listed workspace whose labels name no spec this SDK can read: enough to remove it (FR-151).
_UNKNOWN_SPEC = WorkspaceSpec(image="unknown/workspace@sha256:" + "0" * 64)


def _recorded(labels: Mapping[str, str]) -> WorkspaceSpec:
    """The spec a volume's labels record, or `_UNKNOWN_SPEC`: one foreign, old or damaged label
    never fails a listing (pre-review P5)."""
    try:
        record = json.loads(labels.get("agentsdk.spec") or "")
        return WorkspaceSpec(**{k: v for k, v in record.items() if k in WorkspaceSpec.__dataclass_fields__})
    except Exception:  # noqa: BLE001 - whatever a label holds
        return _UNKNOWN_SPEC


def _too_long(args: list[str]) -> bool:
    return sys.platform == "win32" and len(subprocess.list2cmdline(args)) > _COMMAND_LINE
