"""Workspaces: where an agent's commands run (Phase 5, P5-D1 to P5-D15).

A run whose agent has a `WorkspaceSpec` is given a workspace the first time one of its
workspace tools needs it (FR-138). The `Runner`'s `WorkspaceManager` is the only caller of its
`WorkspaceProvider`; `FakeProvider` keeps the provider contract in process for tests and
offline examples, and runs no host process and no model-written code (FR-139).

Imports nothing outside the standard library and the SDK (FR-137, NFR-40).
"""

from __future__ import annotations

import asyncio
import base64
import io
import math
import posixpath
import re
import secrets
import shutil
import tarfile
import tempfile
import time
import uuid
import weakref
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from ..errors import WorkspaceUnavailable
from ..primitives import unstorable_reason
from ..tools import ApprovalPolicy
from .egress import MAX_CONNECTIONS, USER, Proxy, client_hello, parse_egress

__all__ = [
    "CommandOutcome",
    "DockerProvider",
    "FakeCommand",
    "FakeFiles",
    "FakeProvider",
    "SnapshotHandle",
    "SnapshotRef",
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
# FR-168 (M46): what copying a workspace into a snapshot, or a snapshot into a new workspace, may
# take; a command or write asked while a snapshot is taken waits for it as long (DECISION-bafa7be8).
SNAPSHOT_SECONDS = 600.0
# How many times a provider tries a removal, waiting longer each time, before it raises.
_REMOVE_ATTEMPTS = 4


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
    # FR-159 (M44): the `host:port` pairs it may reach, through a proxy of its own; none by default.
    egress: tuple[str, ...] = ()
    # FR-169 (M46): the snapshot, of the same tenancy, it starts as a copy of; none by default.
    from_snapshot: str | None = None

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
        parse_egress(self.egress)
        if self.from_snapshot is not None and (not isinstance(self.from_snapshot, str) or not self.from_snapshot):
            raise ValueError("WorkspaceSpec.from_snapshot must name a snapshot, as SnapshotRef.snapshot_id does")

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
            # Named only when set, so a spec without it records what it did before (NFR-49).
            **({"egress": list(self.egress)} if self.egress else {}),
            **({"from_snapshot": self.from_snapshot} if self.from_snapshot is not None else {}),
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


# FR-165 (M45, pre-review F2): what a sandboxed server's start and close may take beyond an MCP
# server's own -- its slot within connect_timeout apart, which its caller adds. A start: the
# workspace's creation and its files' import, SETUP_SECONDS each; its record, 30 s (the manager's
# _REMOVE_SECONDS); and a launch's two docker calls, 60 s each (docker's _CLI_SECONDS). A close: the
# removal and its egress drain, 30 s each, and its record, 30 s. Tested against those constants.
SERVER_START_SECONDS = 2 * SETUP_SECONDS + 30.0 + 2 * 60.0
SERVER_CLOSE_SECONDS = 3 * 30.0


@dataclass(frozen=True)
class ServerLaunch:
    """FR-164: how a sandboxed MCP server is started over its workspace -- the program, its
    arguments, its environment (the values its program needs, never recorded) and its working
    directory -- which the MCP client starts over stdio as it starts any server."""

    command: str
    args: tuple[str, ...]
    env: Mapping[str, str] | None = field(default=None, repr=False)
    cwd: str | None = None


@dataclass(frozen=True)
class WorkspaceHandle:
    """A provider's reference to one workspace. `workspace_id` names it everywhere."""

    workspace_id: str
    spec: WorkspaceSpec
    labels: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SnapshotHandle:
    """FR-168 (M46): a provider's reference to one snapshot, labelled as its workspace was, with
    `agentsdk.snapshot` naming it and `agentsdk.of` its workspace. `size_bytes` is its measure when
    it was taken, and None when it was listed: a provider cannot always say it again."""

    snapshot_id: str
    labels: Mapping[str, str] = field(default_factory=dict)
    size_bytes: int | None = None


@dataclass(frozen=True)
class SnapshotRef:
    """FR-168: what `Runner.snapshot_workspace` returns. `WorkspaceSpec(from_snapshot=snapshot_id)`
    starts a workspace of the same tenant and project from it."""

    snapshot_id: str
    workspace_id: str
    tenant_id: str
    project_id: str
    size_bytes: int


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
    cancellation the manager records that as `kill_error` (DECISION-d4ede901).

    NFR-39 (M45 round 3, DECISION-8e21d1ff): with a sandboxed MCP server, the server's engine keeps
    the provider until its server's workspace is removed, after its `Runner` is gone. So a provider
    keeps nothing that still waits on a run once that run's loop is closed -- `FakeProvider` holds
    a running command's task and kill weakly -- or a dropped `Runner` is never collected."""

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

    # FR-168, FR-169 (M46): `snapshot` copies a workspace, between commands, into a snapshot
    # labelled `labels`, measured as `size` counts; `create` with a spec's `from_snapshot` starts the
    # workspace as that snapshot's copy, and raises `WorkspaceUnavailable` when there is none. A
    # snapshot is never one of `list_owned`'s workspaces: `list_snapshots` names them.
    async def snapshot(self, handle: WorkspaceHandle, labels: Mapping[str, str]) -> SnapshotHandle: ...

    async def remove_snapshot(self, snapshot_id: str) -> None: ...

    async def list_snapshots(self, labels: Mapping[str, str]) -> list[SnapshotHandle]: ...


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


def server_files(env: Any, cwd: Any, spec: Any, files: Any) -> Mapping[str, bytes]:
    """FR-164 (DECISION-b478705b, DECISION-2a12196e): a sandboxed MCP server's files, checked and
    frozen. A workspace's server takes its environment from its spec alone and runs from its
    workspace's root; it lives exactly as long as its server, so it is neither kept nor a plan's;
    its files are clean relative paths to bytes, and given only with a workspace."""
    if files is None or not isinstance(files, Mapping):
        raise ValueError("files must map paths to bytes")
    if spec is None:
        if files:
            raise ValueError("files are given to a sandboxed server only: give it a workspace too")
        return MappingProxyType({})
    if not isinstance(spec, WorkspaceSpec):
        raise ValueError("workspace must be a WorkspaceSpec")
    if env or cwd is not None:
        raise ValueError("a sandboxed server's environment is its WorkspaceSpec.env alone, and it runs from its"
                         " workspace's root: give no env or cwd of its own")
    if spec.keep or spec.scope != "run":
        raise ValueError("a server's workspace lives exactly as long as its server: keep and scope plan are refused")
    if spec.from_snapshot is not None:
        # DECISION-bafa7be8: snapshots are of run and plan workspaces only (M46 pre-review F2).
        raise ValueError("a server's workspace starts from its files, never from a snapshot")
    for path, data in files.items():
        parts = path.split("/") if isinstance(path, str) else []
        if (not parts or any(p in ("", ".", "..") for p in parts) or "\\" in path or ":" in path
                or "\x00" in path or unstorable_reason(path)):
            raise ValueError(f"files: {path!r} is not a clean relative path")
        if not isinstance(data, bytes):
            raise ValueError(f"files: {path!r} must be bytes")
    # Pre-review F5: one tree, as the transfer filter reads it -- no path a folder of another, and
    # none two that only case tells apart (a host's folder may fold them into one file).
    folders = {"/".join(path.split("/")[:i]) for path in files for i in range(1, path.count("/") + 1)}
    clashes = sorted(folders & set(files))
    if clashes:
        raise ValueError(f"files: {clashes[0]!r} is a file and also another file's folder")
    if len({path.casefold() for path in files}) != len(files):
        raise ValueError("files: two paths differ only in case")
    return MappingProxyType(dict(files))


class FakeFiles:
    """FR-139: a fake workspace's tree: directories, regular files, and symbolic links, which a
    command can make but nothing reads through -- an export packs a link as `docker cp` does, so
    the transfer filter refuses it on both providers (M42). Writes from a command that was killed
    are discarded: a kill is total (P5-D5)."""

    def __init__(self) -> None:
        self._files: dict[str, bytes] = {}
        self._dirs: set[str] = set()
        self._links: dict[str, str] = {}

    def copy(self) -> FakeFiles:
        """FR-168: the tree as it is now, links kept as links."""
        copied = FakeFiles()
        copied._files, copied._dirs, copied._links = dict(self._files), set(self._dirs), dict(self._links)
        return copied

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
                 keep: int, egress: _FakeEgress | None = None) -> None:
        self.argv, self.stdin, self.env, self.files = list(argv), stdin, dict(env), files
        self._keep = keep
        self._egress = egress
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

    async def connect(self, host: str, port: int, *, sni: str | None = None, token: str | None = None) -> bytes:
        """FR-163: an HTTPS connection through the workspace's proxy, under the very rules of the
        Docker proxy (`egress.Proxy`): what the allowed site sent back, or OSError when refused. `sni`
        defaults to `host`, `token` to this command's own. With no `egress` there is no network."""
        if self._egress is None:
            raise OSError("the workspace has no egress: no network")
        return await self._egress.connect(host, port, host if sni is None else sni,
                                          self._egress.token if token is None else token)

    @property
    def egress_token(self) -> str | None:
        """This command's token, as its HTTPS_PROXY carries it on Docker; None with no egress."""
        return None if self._egress is None else self._egress.token

    async def proxy(self, data: bytes) -> bytes:
        """FR-163: `data` sent as-is to the workspace's proxy, as a command on Docker can write any bytes
        to its socket; what the proxy answered. With no `egress` there is no network."""
        if self._egress is None:
            raise OSError("the workspace has no egress: no network")
        return await self._egress.send(data)

    async def resolve_name(self, name: str) -> list[str]:
        """No name resolves inside a workspace (P5-D20)."""
        raise OSError(f"no name resolves inside a workspace: {name}")

    async def connect_address(self, address: str, port: int) -> bytes:
        """No direct connection leaves a workspace: only the proxy does (P5-D19)."""
        raise OSError(f"no direct connection leaves a workspace: {address}:{port}")

    def cpu(self, seconds: float) -> None:
        """FR-156: the CPU-seconds this command reads as having used, 0 unless declared. A killed
        command is charged its ceiling whatever it declared, as on Docker."""
        self._cpu = seconds

    def limit(self, name: str) -> None:
        """Ends the command as a container's limit would: killed, `killed="limit"`, and the
        outcome's `limit` is `name`."""
        raise _Killed(name)


FakeCommandFn = Callable[[FakeCommand], Awaitable[int]]


class _Sink:
    """A stream writer that keeps what it is given: the fake proxy's client and upstream ends."""

    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


class _FakeEgress:
    """One command's way out on the fake: its token, and its workspace's `Proxy`."""

    def __init__(self, proxy: Proxy, token: str) -> None:
        self.proxy, self.token = proxy, token

    async def send(self, data: bytes) -> bytes:
        """`data` as a client's first bytes to the proxy; what it wrote back before it closed."""
        reader = asyncio.StreamReader()
        reader.feed_data(data)
        sink = _Sink()
        await self.proxy.handle(reader, sink)
        return bytes(sink.data)

    async def connect(self, host: str, port: int, sni: str, token: str) -> bytes:
        auth = base64.b64encode(f"{USER}:{token}".encode()).decode()
        head = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\nProxy-Authorization: Basic {auth}\r\n\r\n"
        answer = await self.send(head.encode() + client_hello(sni))
        if not answer.startswith(b"HTTP/1.1 200"):
            raise ConnectionRefusedError(f"the egress proxy refused {host}:{port}")
        body = answer.partition(b"\r\n\r\n")[2]
        if not body:
            raise ConnectionResetError(f"the egress proxy closed the tunnel to {host}:{port}")
        return body


def _written(folder: Path, files: Mapping[str, bytes]) -> None:
    """A fake workspace's files, written under `folder` (each path already a workspace path)."""
    for path, data in files.items():
        target = folder.joinpath(*path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


class _FakeWorkspace:
    def __init__(self, handle: WorkspaceHandle) -> None:
        self.handle = handle
        self.files = FakeFiles()
        # NFR-39 (M45 round 3, DECISION-8e21d1ff): a sandboxed server's engine keeps this provider,
        # and a run awaits what is kept here; each is held by its command's own call alone.
        self.running: weakref.WeakValueDictionary[str, asyncio.Task[Any]] = weakref.WeakValueDictionary()
        self.kills: weakref.WeakValueDictionary[str, asyncio.Event] = weakref.WeakValueDictionary()
        self.proxy: Proxy | None = None  # FR-160: only with egress
        self.tokens: dict[str, str] = {}  # each command's token, to its command id
        self.watcher: Callable[[dict], None] | None = None
        # FR-165 (M45): a sandboxed server's working directory, made from its files at its first
        # start and kept across a restart; removed with the workspace (DECISION-b478705b).
        self.folder: Path | None = None


class FakeProvider:
    """FR-139: a provider in process. Commands are Python callables registered by name, dispatched
    on `argv[0]`; an unregistered name exits 127. Every limit a container enforces can be
    exercised: a timeout and a kill end the command totally -- one that ignores its cancellation
    is abandoned, and whatever it writes afterwards is discarded -- and `FakeCommand.limit` ends
    it as an exhausted limit would. No host process is started."""

    name = "fake"

    def __init__(self, commands: Mapping[str, FakeCommandFn] | None = None, *,
                 resolve: Mapping[str, list[str] | Callable[[], list[str]]] | None = None,
                 sites: Mapping[tuple[str, int], bytes] | None = None,
                 max_connections: int = MAX_CONNECTIONS) -> None:
        self._commands = dict(commands or {})
        self._spaces: dict[str, _FakeWorkspace] = {}
        # FR-163: the names the fake's proxy resolves, and what each allowed site sends back. A
        # name's addresses may be a callable, asked at each resolution: a name that rebinds
        # (M47, DECISION-18bd3f65).
        self._resolve = {name: addresses if callable(addresses) else list(addresses)
                         for name, addresses in (resolve or {}).items()}
        self._sites = dict(sites or {})
        self._max_connections = max_connections
        # FR-168 (M46): each snapshot's handle and its copy of the tree, in memory.
        self._snapshots: dict[str, tuple[SnapshotHandle, FakeFiles]] = {}

    def _proxy(self, space: _FakeWorkspace) -> Proxy:
        async def resolve(host: str, port: int) -> list[str]:
            found = self._resolve.get(host, [])
            return list(found() if callable(found) else found)

        async def connect(address: str, port: int):
            # A site is found by the name a fixed answer gives its address.
            host = next((name for name, found in self._resolve.items()
                         if not callable(found) and address in found), None)
            site = self._sites.get((host, port))
            if site is None:
                raise ConnectionRefusedError(f"nothing answers at {address}:{port}")
            reader = asyncio.StreamReader()
            reader.feed_data(site)
            reader.feed_eof()
            return reader, _Sink()

        def reported(report: dict) -> None:
            report = dict(report)
            token = report.pop("token", None)
            if report["kind"] == "connection":
                # The token names its command; none, or another's, names no command (DECISION-88dea779).
                report["command_id"] = space.tokens.get(token) if token else None
            if space.watcher is not None:
                space.watcher(report)

        return Proxy(space.handle.spec.egress, report=reported, resolve=resolve, connect=connect,
                     max_connections=self._max_connections)

    def watch_egress(self, handle: WorkspaceHandle, callback: Callable[[dict], None]) -> None:
        """FR-162: `callback` gets each report of the workspace's proxy, its token made a command id."""
        self._space(handle).watcher = callback

    def kill_proxy(self, handle: WorkspaceHandle) -> None:
        """Ends the workspace's proxy as a dead one ends on Docker: it is gone, and reported `ended`
        (F2, DECISION-26009e04)."""
        space = self._space(handle)
        space.proxy = None
        if space.watcher is not None:
            space.watcher({"kind": "ended", "at": datetime.now(timezone.utc).isoformat()})

    def _space(self, handle: WorkspaceHandle) -> _FakeWorkspace:
        space = self._spaces.get(handle.workspace_id)
        if space is None:
            raise WorkspaceUnavailable(f"workspace {handle.workspace_id} does not exist")
        return space

    async def create(self, spec: WorkspaceSpec, labels: Mapping[str, str]) -> WorkspaceHandle:
        workspace_id = labels.get("agentsdk.workspace") or str(uuid.uuid4())
        handle = WorkspaceHandle(workspace_id=workspace_id, spec=spec, labels=MappingProxyType(dict(labels)))
        snapshot = None
        if spec.from_snapshot is not None:
            snapshot = self._snapshots.get(spec.from_snapshot)
            if snapshot is None:
                raise WorkspaceUnavailable(f"snapshot {spec.from_snapshot} does not exist")
        space = self._spaces[workspace_id] = _FakeWorkspace(handle)
        if snapshot is not None:
            space.files = snapshot[1].copy()
        if spec.egress:
            space.proxy = self._proxy(space)
        return handle

    async def run(self, handle: WorkspaceHandle, argv: list[str], *, timeout: float, stdin: bytes | None = None,
                  command_id: str | None = None) -> CommandOutcome:
        space = self._space(handle)
        command_id = command_id or str(uuid.uuid4())
        fn = self._commands.get(argv[0]) if argv else None
        files = _CommandFiles(space.files)
        egress, token = None, None
        if space.proxy is not None:
            token = secrets.token_hex(16)
            space.tokens[token] = command_id
            egress = _FakeEgress(space.proxy, token)
        command = FakeCommand(argv, stdin, handle.spec.env, files, handle.spec.max_output_bytes, egress)
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
            space.tokens.pop(token, None)  # F4: it names its command only while it runs
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
        if space.proxy is not None:
            # Stopped as Docker's is, so its last reports come first (DECISION-e510a5e3).
            proxy, space.proxy = space.proxy, None
            await proxy.close()
        if space.folder is not None:
            # A server's process may still be ending, and on Windows its working folder cannot go
            # until it has: tried as Docker's removal is, and one left stays listed for the reap
            # (M45 round 4 NB2).
            for attempt in range(_REMOVE_ATTEMPTS):
                await asyncio.to_thread(shutil.rmtree, space.folder, True)
                if not space.folder.exists():
                    break
                await asyncio.sleep(0.5 * (attempt + 1))
            else:
                raise WorkspaceUnavailable("the server's folder could not be removed: its process still runs in it")
        self._spaces.pop(handle.workspace_id, None)

    async def server_launch(self, handle: WorkspaceHandle, command: str, args: list[str]) -> ServerLaunch:
        """FR-165 (DECISION-b478705b): a sandboxed server here is a local process -- no isolation,
        the same records and lifetime -- run from a folder holding its workspace's files, made at
        its first start and kept across a restart, with its spec's env alone."""
        space = self._space(handle)
        if space.folder is None:
            files = dict(space.files._files)
            space.folder = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix="agentsdk-server-"))
            await asyncio.to_thread(_written, space.folder, files)
        return ServerLaunch(command, tuple(args), dict(handle.spec.env), str(space.folder))

    async def list_owned(self, labels: Mapping[str, str]) -> list[WorkspaceHandle]:
        return [s.handle for s in self._spaces.values()
                if all(s.handle.labels.get(k) == v for k, v in labels.items())]

    async def snapshot(self, handle: WorkspaceHandle, labels: Mapping[str, str]) -> SnapshotHandle:
        files = self._space(handle).files.copy()
        taken = SnapshotHandle(labels["agentsdk.snapshot"], MappingProxyType(dict(labels)), files.size())
        self._snapshots[taken.snapshot_id] = (taken, files)
        return taken

    async def remove_snapshot(self, snapshot_id: str) -> None:
        self._snapshots.pop(snapshot_id, None)

    async def list_snapshots(self, labels: Mapping[str, str]) -> list[SnapshotHandle]:
        return [taken for taken, _ in self._snapshots.values()
                if all(taken.labels.get(k) == v for k, v in labels.items())]


from .docker import DockerProvider, read_tar  # noqa: E402 - it builds on the names above (FR-142)
