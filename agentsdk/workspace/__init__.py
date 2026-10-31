"""Workspaces: where an agent's commands run (Phase 5, P5-D1 to P5-D15).

A run whose agent has a `WorkspaceSpec` is given a workspace the first time one of its
workspace tools needs it (FR-138). The `Runner`'s `WorkspaceManager` is the only caller of its
`WorkspaceProvider`; `FakeProvider` keeps the provider contract in process for tests and
offline examples, and runs no host process and no model-written code (FR-139).

Imports nothing outside the standard library and the SDK (FR-137, NFR-40).
"""

from __future__ import annotations

import asyncio
import io
import math
import posixpath
import re
import tarfile
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from ..errors import WorkspaceUnavailable
from ..primitives import unstorable_reason
from ..tools import ApprovalPolicy

__all__ = [
    "CommandOutcome",
    "DockerProvider",
    "FakeCommand",
    "FakeFiles",
    "FakeProvider",
    "WorkspaceHandle",
    "WorkspaceProvider",
    "WorkspaceSpec",
    "charged",
]

_MiB, _GiB = 2**20, 2**30
_DIGEST = re.compile(r"[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Each limit's default and fixed maximum (FR-137). None can be switched off.
_LIMITS: dict[str, tuple[float, float, type]] = {
    "memory_bytes": (512 * _MiB, 16 * _GiB, int),
    "cpus": (1.0, 16.0, float),
    "pids": (128, 4096, int),
    "tmp_bytes": (64 * _MiB, 1 * _GiB, int),
    "max_workspace_bytes": (1 * _GiB, 64 * _GiB, int),
    "max_command_seconds": (120, 3600, float),
    "max_output_bytes": (1 * _MiB, 16 * _MiB, int),
}
SCOPES = ("run", "plan")
# FR-138, FR-148: what a workspace's setup -- reaping, the slot wait, creation -- may take before a
# call fails WorkspaceUnavailable; every workspace tool's deadline is past it, so a command that
# starts ends by its own timeout (DECISION-e799700b).
SETUP_SECONDS = 120.0
# What reading a workspace's size may take; a command's first use reads it first when it is stale,
# so the tools' deadline is past it too (DECISION-4689af02).
MEASURE_SECONDS = 120.0


def _limit(name: str, value: Any) -> float:
    _, maximum, kind = _LIMITS[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"WorkspaceSpec.{name} must be a number, got {type(value).__name__}")
    if kind is int and not isinstance(value, int):
        raise ValueError(f"WorkspaceSpec.{name} must be a whole number, got {value!r}")
    if not math.isfinite(value) or value <= 0 or value > maximum:
        raise ValueError(f"WorkspaceSpec.{name} must be over 0 and at most {maximum}, got {value!r}")
    if name == "cpus" and (value < 0.01 or abs(value * 100 - round(value * 100)) > 1e-9):
        # Docker reads `--cpus` to the nanocpu: a value in scientific notation gave no limit at
        # all, and one past two decimals failed every create (DECISION-d4ede901).
        raise ValueError(f"WorkspaceSpec.cpus must be from 0.01 in whole hundredths, got {value!r}")
    return value


@dataclass(frozen=True)
class WorkspaceSpec:
    """FR-137: what a run's workspace is made from and bound by. Frozen; `env`'s values are never
    shown -- not by `repr`, `record()`, a row, an event or the manifest (NFR-45)."""

    image: str
    memory_bytes: int = _LIMITS["memory_bytes"][0]
    cpus: float = _LIMITS["cpus"][0]
    pids: int = _LIMITS["pids"][0]
    tmp_bytes: int = _LIMITS["tmp_bytes"][0]
    max_workspace_bytes: int = _LIMITS["max_workspace_bytes"][0]
    max_command_seconds: float = _LIMITS["max_command_seconds"][0]
    max_output_bytes: int = _LIMITS["max_output_bytes"][0]
    env: Mapping[str, str] = field(default_factory=dict, repr=False)
    scope: str = "run"
    keep: bool = False
    run_command_approval: ApprovalPolicy = ApprovalPolicy.REQUIRE_APPROVAL

    def __post_init__(self) -> None:
        # P5-D6: by digest, never a tag, so what runs is what was recorded; the provider never
        # pulls, so a run never reaches the network to start one.
        if not isinstance(self.image, str) or not _DIGEST.fullmatch(self.image):
            raise ValueError("WorkspaceSpec.image must be pinned by digest: <name>@sha256:<64 lowercase hex>")
        for name in _LIMITS:
            _limit(name, getattr(self, name))
        if self.cpus != round(self.cpus, 2):
            # Kept as docker receives it, so the record names the limit in force (DECISION-f3e99c07).
            object.__setattr__(self, "cpus", round(self.cpus, 2))
        if not isinstance(self.env, Mapping):
            raise ValueError(f"WorkspaceSpec.env must map names to values, got {type(self.env).__name__}")
        env = {}
        for name, value in self.env.items():
            if not isinstance(name, str) or not _ENV_NAME.fullmatch(name):
                raise ValueError(f"WorkspaceSpec.env has an invalid name: {name!r}")
            if not isinstance(value, str):
                raise ValueError(f"WorkspaceSpec.env[{name!r}] must be a str, got {type(value).__name__}")
            if unstorable_reason(value) is not None or "\x00" in value:
                # Named, never shown: the value itself is the secret (NFR-45).
                raise ValueError(f"WorkspaceSpec.env[{name!r}] is a value no store or process can take")
            env[name] = value
        object.__setattr__(self, "env", MappingProxyType(env))
        if self.scope not in SCOPES:
            raise ValueError(f"WorkspaceSpec.scope must be one of {SCOPES}, got {self.scope!r}")
        if not isinstance(self.keep, bool):
            raise ValueError(f"WorkspaceSpec.keep must be a bool, got {type(self.keep).__name__}")
        if not isinstance(self.run_command_approval, ApprovalPolicy):
            raise ValueError("WorkspaceSpec.run_command_approval must be an ApprovalPolicy")

    def record(self) -> dict[str, Any]:
        """What the manifest and the `workspaces` row record: the image, the limits in force, the
        scope, and the env's names -- never its values (FR-140)."""
        return {
            "image": self.image,
            **{name: getattr(self, name) for name in _LIMITS},
            "env": sorted(self.env),
            "scope": self.scope,
            "keep": self.keep,
            "run_command_approval": self.run_command_approval.value,
        }


@dataclass(frozen=True)
class CommandOutcome:
    """FR-138: how one command ended. `exit_code` is None when it was killed; `killed` says why:
    None, "timeout", "cancelled" or "limit", and `limit` names the bound that ended it when the
    provider can say, "memory_bytes" on Docker (DECISION-21e7f6fb). Each stream is kept up to the
    spec's `max_output_bytes` and marked cut past it, with the bytes it produced counted."""

    exit_code: int | None
    stdout: bytes
    stderr: bytes
    stdout_cut: bool
    stderr_cut: bool
    stdout_bytes: int
    stderr_bytes: int
    duration: float
    killed: str | None = None
    limit: str | None = None
    # FR-156 (M43): the CPU-seconds it is charged, as `charged` decides.
    cpu_seconds: float = 0.0


def charged(reading: float | None, killed: str | None, duration: float, cpus: float) -> float:
    """FR-156, NFR-48: what a command is charged. Its ceiling is the most it could have used, its
    wall time times its `cpus` limit: a command killed, by any cause, is charged that, as is one
    whose reading is absent, not a finite number, negative or past it. Otherwise its reading."""
    ceiling = max(0.0, duration) * cpus
    if killed is not None or reading is None or not math.isfinite(reading) or not 0 <= reading <= ceiling:
        return ceiling
    return reading


@dataclass(frozen=True)
class WorkspaceHandle:
    """A provider's reference to one workspace. `workspace_id` names it everywhere."""

    workspace_id: str
    spec: WorkspaceSpec
    labels: Mapping[str, str] = field(default_factory=dict)


@runtime_checkable
class WorkspaceProvider(Protocol):
    """FR-138, P5-D2: what the `WorkspaceManager` asks of a provider. Every method is a
    coroutine. `run` never raises for what a command does -- its exit, a timeout, a kill, a limit
    are its `CommandOutcome` -- and raises `WorkspaceUnavailable` when the workspace cannot run
    anything. `import_` takes a tar stream of regular files and directories only, and raises
    `ValueError` for anything else (P5-D8); `export` returns one, and raises
    `FileNotFoundError` for a missing path. `list` names a folder's direct entries -- (name, kind,
    size), kind "file", "dir", "link" or "other", sorted by name, at most `limit` of them -- and
    how many there are, transferring no file's contents; it raises `FileNotFoundError` for a
    missing path, `NotADirectoryError` for anything but a folder (DECISION-0004d2dc),
    `PermissionError` for a folder that cannot be read, and `WorkspaceUnavailable` otherwise.
    `size` counts as `du` does. `kill` raises when the command may still be running; on a run's
    cancellation the manager records that as `kill_error` (DECISION-d4ede901)."""

    name: str

    async def create(self, spec: WorkspaceSpec, labels: Mapping[str, str]) -> WorkspaceHandle: ...

    async def run(self, handle: WorkspaceHandle, argv: list[str], *, timeout: float, stdin: bytes | None = None,
                  command_id: str | None = None) -> CommandOutcome: ...

    async def kill(self, handle: WorkspaceHandle, command_id: str) -> None: ...

    async def size(self, handle: WorkspaceHandle) -> int: ...

    async def list(self, handle: WorkspaceHandle, path: str, *,
                   limit: int = 1000) -> tuple[list[tuple[str, str, int]], int]: ...

    async def export(self, handle: WorkspaceHandle, path: str) -> bytes: ...

    async def import_(self, handle: WorkspaceHandle, data: bytes) -> None: ...

    async def remove(self, handle: WorkspaceHandle) -> None: ...

    async def list_owned(self, labels: Mapping[str, str]) -> list[WorkspaceHandle]: ...


# =================================================================================================
# FakeProvider (FR-139)
# =================================================================================================


_BLOCK = 4096  # what `du` counts in, on the file systems Docker's volumes live on


def normal_path(path: str) -> str:
    """A workspace path as the fake keeps it: relative, `/`-separated, normalised. Anything that
    would leave the workspace raises ValueError (the tools refuse it earlier, at step 2)."""
    if not isinstance(path, str) or "\x00" in path or "\\" in path or path.startswith("/"):
        raise ValueError(f"not a workspace path: {path!r}")
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValueError(f"not a workspace path: {path!r}")
    return "/".join(parts)


class FakeFiles:
    """FR-139: a fake workspace's tree: directories, regular files, and symbolic links, which a
    command can make but nothing reads through -- an export packs a link as `docker cp` does, so
    the transfer filter refuses it on both providers (M42). Writes from a command that was killed
    are discarded: a kill is total (P5-D5)."""

    def __init__(self) -> None:
        self._files: dict[str, bytes] = {}
        self._dirs: set[str] = set()
        self._links: dict[str, str] = {}

    def size(self) -> int:
        """As `du` counts (DECISION-d4ede901): each file in whole 4 KiB blocks and each folder one
        block, the root apart, so a bound tuned on the fake holds on Docker."""
        return _BLOCK * (len(self._dirs) + sum(-(-len(data) // _BLOCK) for data in self._files.values()))

    def read(self, path: str) -> bytes:
        key = normal_path(path)
        if key not in self._files:
            raise FileNotFoundError(path)
        return self._files[key]

    def _parents(self, key: str) -> None:
        parent = posixpath.dirname(key)
        while parent:
            if parent in self._files or parent in self._links:
                raise NotADirectoryError(parent)
            self._dirs.add(parent)
            parent = posixpath.dirname(parent)

    def write(self, path: str, data: bytes | str) -> None:
        key = normal_path(path)
        if not key or key in self._dirs:
            raise IsADirectoryError(path)
        if key in self._links:
            # A command writing through a link: the fake has no tree outside it to write into.
            # (An import replaces the link instead, as on Docker: see untar.)
            raise PermissionError(f"{path} is a link")
        self._parents(key)
        self._files[key] = data.encode("utf-8") if isinstance(data, str) else bytes(data)

    def link(self, path: str, target: str) -> None:
        """A symbolic link at `path` to `target`, which may name anything, inside or out."""
        key = normal_path(path)
        if not key or key in self._dirs or key in self._files or key in self._links:
            raise FileExistsError(path)
        self._parents(key)
        self._links[key] = target

    def exists(self, path: str) -> bool:
        key = normal_path(path)
        return key == "" or key in self._files or key in self._dirs or key in self._links

    def is_dir(self, path: str) -> bool:
        key = normal_path(path)
        return key == "" or key in self._dirs

    def listdir(self, path: str = "") -> list[tuple[str, str, int]]:
        """(name, "file", "dir" or "link", size) of each direct entry, sorted by name."""
        key = normal_path(path)
        if not self.is_dir(key):
            raise (NotADirectoryError if key in self._files or key in self._links else FileNotFoundError)(path)
        prefix = f"{key}/" if key else ""
        found: dict[str, tuple[str, int]] = {}
        for name, data in self._files.items():
            if name.startswith(prefix) and "/" not in name[len(prefix):]:
                found[name[len(prefix):]] = ("file", len(data))
        for name in self._dirs:
            if name.startswith(prefix) and name != key and "/" not in name[len(prefix):]:
                found[name[len(prefix):]] = ("dir", 0)
        for name in self._links:
            if name.startswith(prefix) and "/" not in name[len(prefix):]:
                found[name[len(prefix):]] = ("link", 0)
        return [(name, kind, size) for name, (kind, size) in sorted(found.items())]

    def remove(self, path: str) -> None:
        key = normal_path(path)
        if key in self._files:
            del self._files[key]
            return
        if key in self._links:
            del self._links[key]
            return
        raise FileNotFoundError(path)

    def tar(self, path: str) -> bytes:
        key = normal_path(path)
        if not self.exists(key):
            raise FileNotFoundError(path)
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            if key in self._files:
                self._add(tar, posixpath.basename(key), self._files[key])
            elif key in self._links:
                self._add_link(tar, posixpath.basename(key), self._links[key])
            else:
                base = f"{key}/" if key else ""
                directory = tarfile.TarInfo(".")
                directory.type, directory.mode = tarfile.DIRTYPE, 0o755
                tar.addfile(directory)
                for name in sorted(n for n in self._dirs if n.startswith(base)):
                    info = tarfile.TarInfo("./" + name[len(base):])
                    info.type, info.mode = tarfile.DIRTYPE, 0o755
                    tar.addfile(info)
                for name, data in sorted(self._files.items()):
                    if name.startswith(base):
                        self._add(tar, "./" + name[len(base):], data)
                for name, target in sorted(self._links.items()):
                    if name.startswith(base):
                        self._add_link(tar, "./" + name[len(base):], target)
        return buffer.getvalue()

    @staticmethod
    def _add(tar: tarfile.TarFile, name: str, data: bytes) -> None:
        info = tarfile.TarInfo(name)
        info.size, info.mode, info.uid, info.gid = len(data), 0o644, 65534, 65534
        tar.addfile(info, io.BytesIO(data))

    @staticmethod
    def _add_link(tar: tarfile.TarFile, name: str, target: str) -> None:
        info = tarfile.TarInfo(name)
        info.type, info.linkname, info.mode, info.uid, info.gid = tarfile.SYMTYPE, target, 0o777, 65534, 65534
        tar.addfile(info)

    def untar(self, data: bytes) -> None:
        """Regular files and directories only (P5-D8); anything else refuses the whole stream,
        before anything is written."""
        staged: list[tuple[str, bytes | None]] = []
        try:
            with tarfile.open(fileobj=io.BytesIO(data)) as tar:
                for member in tar.getmembers():
                    if not (member.isfile() or member.isdir()):
                        raise ValueError(f"not a regular file or directory: {member.name!r}")
                    key = normal_path(member.name)
                    staged.append((key, tar.extractfile(member).read() if member.isfile() else None))
        except tarfile.TarError as exc:
            raise ValueError(f"not a tar stream: {type(exc).__name__}") from None
        for key, content in staged:
            # Nothing may turn a file into a folder or a folder into a file (pre-review D9).
            if (content is None and key in self._files) or (content is not None and key in self._dirs):
                raise ValueError(f"{key!r} is already a {'file' if content is None else 'folder'}")
            # Nothing lands under a link, as Docker refuses it (M42 pre-review F5).
            parent = posixpath.dirname(key)
            while parent:
                if parent in self._links:
                    raise ValueError(f"{key!r} is under {parent!r}, a link")
                parent = posixpath.dirname(parent)
        for key, content in staged:
            if content is None:
                if key:
                    self._links.pop(key, None)
                    self._dirs.add(key)
            else:
                # A link it lands on is replaced by the file, as extracting does on Docker.
                self._links.pop(key, None)
                self.write(key, content)


class _Killed(BaseException):
    """Raised inside a fake command that went over a limit."""

    def __init__(self, limit: str) -> None:
        self.limit = limit


class _CommandFiles:
    """One command's view of its workspace's files. Once the command is killed its writes go
    nowhere -- through this view, however the command holds it (pre-review D6)."""

    def __init__(self, tree: FakeFiles) -> None:
        self._tree, self.alive = tree, True

    def size(self) -> int:
        return self._tree.size()

    def read(self, path: str) -> bytes:
        return self._tree.read(path)

    def exists(self, path: str) -> bool:
        return self._tree.exists(path)

    def is_dir(self, path: str) -> bool:
        return self._tree.is_dir(path)

    def listdir(self, path: str = "") -> list[tuple[str, str, int]]:
        return self._tree.listdir(path)

    def write(self, path: str, data: bytes | str) -> None:
        if self.alive:
            self._tree.write(path, data)

    def link(self, path: str, target: str) -> None:
        if self.alive:
            self._tree.link(path, target)

    def remove(self, path: str) -> None:
        if self.alive:
            self._tree.remove(path)


class FakeCommand:
    """What a fake command sees: its argv, stdin and environment, the workspace's files, and its
    two output streams, each kept up to the spec's `max_output_bytes` and counted past it."""

    def __init__(self, argv: list[str], stdin: bytes | None, env: Mapping[str, str], files: _CommandFiles,
                 keep: int) -> None:
        self.argv, self.stdin, self.env, self.files = list(argv), stdin, dict(env), files
        self._keep = keep
        self._out, self._err = bytearray(), bytearray()
        self.out_bytes = self.err_bytes = 0
        self._cpu = 0.0

    def _write(self, buffer: bytearray, data: bytes | str) -> int:
        data = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        room = self._keep - len(buffer)
        if room > 0:
            buffer += data[:room]
        return len(data)

    def out(self, data: bytes | str) -> None:
        self.out_bytes += self._write(self._out, data)

    def err(self, data: bytes | str) -> None:
        self.err_bytes += self._write(self._err, data)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    def cpu(self, seconds: float) -> None:
        """FR-156: the CPU-seconds this command reads as having used, 0 unless declared. A killed
        command is charged its ceiling whatever it declared, as on Docker."""
        self._cpu = seconds

    def limit(self, name: str) -> None:
        """Ends the command as a container's limit would: killed, `killed="limit"`, and the
        outcome's `limit` is `name`."""
        raise _Killed(name)


FakeCommandFn = Callable[[FakeCommand], Awaitable[int]]


class _FakeWorkspace:
    def __init__(self, handle: WorkspaceHandle) -> None:
        self.handle = handle
        self.files = FakeFiles()
        self.running: dict[str, asyncio.Task[Any]] = {}
        self.kills: dict[str, asyncio.Event] = {}


class FakeProvider:
    """FR-139: a provider in process. Commands are Python callables registered by name, dispatched
    on `argv[0]`; an unregistered name exits 127. Every limit a container enforces can be
    exercised: a timeout and a kill end the command totally -- one that ignores its cancellation
    is abandoned, and whatever it writes afterwards is discarded -- and `FakeCommand.limit` ends
    it as an exhausted limit would. No host process is started."""

    name = "fake"

    def __init__(self, commands: Mapping[str, FakeCommandFn] | None = None) -> None:
        self._commands = dict(commands or {})
        self._spaces: dict[str, _FakeWorkspace] = {}

    def _space(self, handle: WorkspaceHandle) -> _FakeWorkspace:
        space = self._spaces.get(handle.workspace_id)
        if space is None:
            raise WorkspaceUnavailable(f"workspace {handle.workspace_id} does not exist")
        return space

    async def create(self, spec: WorkspaceSpec, labels: Mapping[str, str]) -> WorkspaceHandle:
        workspace_id = labels.get("agentsdk.workspace") or str(uuid.uuid4())
        handle = WorkspaceHandle(workspace_id=workspace_id, spec=spec, labels=MappingProxyType(dict(labels)))
        self._spaces[workspace_id] = _FakeWorkspace(handle)
        return handle

    async def run(self, handle: WorkspaceHandle, argv: list[str], *, timeout: float, stdin: bytes | None = None,
                  command_id: str | None = None) -> CommandOutcome:
        space = self._space(handle)
        command_id = command_id or str(uuid.uuid4())
        fn = self._commands.get(argv[0]) if argv else None
        files = _CommandFiles(space.files)
        command = FakeCommand(argv, stdin, handle.spec.env, files, handle.spec.max_output_bytes)
        started = time.monotonic()
        if fn is None:
            command.err(f"{argv[0] if argv else ''}: command not found\n")
            return self._outcome(command, 127, started, cpus=handle.spec.cpus)
        task = asyncio.ensure_future(fn(command))
        space.running[command_id] = task
        space.kills[command_id] = kill = asyncio.Event()
        killing = asyncio.ensure_future(kill.wait())
        killed: str | None = None
        limit: str | None = None
        exit_code: int | None = None
        try:
            # A kill returns at once, even from a command that ignores its cancellation.
            done, _ = await asyncio.wait({task, killing}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            done.discard(killing)
            if kill.is_set():
                killed = "cancelled"
            elif not done:
                killed = "timeout"
            else:
                try:
                    exit_code = int(task.result())
                except _Killed as exc:
                    killed, limit = "limit", exc.limit
                except asyncio.CancelledError:
                    killed = "cancelled"
                except Exception as exc:  # noqa: BLE001 - a command that raised exits 1, as a process would
                    command.err(f"{type(exc).__name__}: {exc}\n")
                    exit_code = 1
        finally:
            killing.cancel()
            space.running.pop(command_id, None)
            space.kills.pop(command_id, None)
            if not task.done():
                # Total: the command is cancelled and abandoned, and whatever it does afterwards
                # reaches no workspace.
                files.alive = False
                task.cancel()
                task.add_done_callback(lambda t: t.cancelled() or t.exception())
        return self._outcome(command, None if killed else exit_code, started, killed, limit, cpus=handle.spec.cpus)

    @staticmethod
    def _outcome(command: FakeCommand, exit_code: int | None, started: float, killed: str | None = None,
                 limit: str | None = None, *, cpus: float):
        duration = time.monotonic() - started
        return CommandOutcome(
            exit_code=exit_code, stdout=bytes(command._out), stderr=bytes(command._err),
            stdout_cut=command.out_bytes > len(command._out), stderr_cut=command.err_bytes > len(command._err),
            stdout_bytes=command.out_bytes, stderr_bytes=command.err_bytes, duration=duration,
            # What it declared, which no wall time bounds since nothing ran; killed, the ceiling.
            killed=killed, limit=limit,
            cpu_seconds=command._cpu if killed is None else charged(None, killed, duration, cpus))

    async def kill(self, handle: WorkspaceHandle, command_id: str) -> None:
        space = self._spaces.get(handle.workspace_id)
        if space is None:
            return
        task, kill = space.running.get(command_id), space.kills.get(command_id)
        if task is not None and kill is not None and not task.done():
            kill.set()
            task.cancel()

    async def size(self, handle: WorkspaceHandle) -> int:
        return self._space(handle).files.size()

    async def list(self, handle: WorkspaceHandle, path: str, *,
                   limit: int = 1000) -> tuple[list[tuple[str, str, int]], int]:
        found = self._space(handle).files.listdir(path)
        return found[:limit], len(found)

    async def export(self, handle: WorkspaceHandle, path: str) -> bytes:
        # Refused as DockerProvider refuses it: past 16 MiB or 10,000 members (M41 pre-review).
        data = self._space(handle).files.tar(path)
        read_tar(data)
        return data

    async def import_(self, handle: WorkspaceHandle, data: bytes) -> None:
        # Checked as DockerProvider checks it, a duplicate member or a file under a file
        # included, before anything is written (M41 pre-review).
        read_tar(data)
        self._space(handle).files.untar(data)

    async def remove(self, handle: WorkspaceHandle) -> None:
        space = self._spaces.get(handle.workspace_id)
        if space is None:
            return
        # Killed while the workspace can still be found, so removal ends what runs in it (M38 review).
        for command_id in list(space.running):
            await self.kill(handle, command_id)
        self._spaces.pop(handle.workspace_id, None)

    async def list_owned(self, labels: Mapping[str, str]) -> list[WorkspaceHandle]:
        return [s.handle for s in self._spaces.values()
                if all(s.handle.labels.get(k) == v for k, v in labels.items())]


from .docker import DockerProvider, read_tar  # noqa: E402 - it builds on the names above (FR-142)
