"""FR-146, FR-147 (M38): the workspace tools (P5-D9, P5-D10).

Never registered in a Runner's ToolRegistry: a run with a workspace adds to its view, each turn,
the ones its agent's `tool_profile` names, bound to that run's workspace (FR-146). Every result
and every error after a tool ran carries `TrustZone.UNTRUSTED`, `InstructionAuthority.DATA_ONLY`,
`EXTERNAL_CONTENT` and `EXECUTABLE_CONTENT` (NFR-42): what a command prints, or a file holds, is
whatever the model's code made of it.
"""

from __future__ import annotations

import io
import posixpath
import tarfile
from collections.abc import Iterable
from typing import Any

from ..primitives import InstructionAuthority, Origin, TaintFlag, TrustZone
from ..scope import ToolScope
from ..tools import ApprovalPolicy, ResultProvenance, RiskClass, Tool, ToolSpec
from . import CommandOutcome, WorkspaceSpec

NAMES = ("run_command", "read_workspace_file", "write_workspace_file", "list_workspace")
# Every name a workspace tool has or will have: none may be a registered tool's (M38 pre-flight).
RESERVED = (*NAMES, "export_workspace_file")
MAX_ENTRIES = 1000

UNTRUSTED = ResultProvenance(
    origin=Origin.INTERNAL_TOOL, trust_zone=TrustZone.UNTRUSTED, instruction_authority=InstructionAuthority.DATA_ONLY,
    taint_flags=frozenset({TaintFlag.EXTERNAL_CONTENT, TaintFlag.EXECUTABLE_CONTENT}))

# A workspace path, refused at step 2 when it could leave /workspace: relative, `/`-separated, no
# `..` segment, no drive, no backslash, and no control character at all -- a newline would hide a
# `..` from `.`, and `$` admits one at the end, so the pattern ends at `\Z` (FR-146; pre-review D1).
PATH = {
    "type": "string",
    "minLength": 1,
    "maxLength": 4096,
    "pattern": "^(?![A-Za-z]:)(?!/)(?!(?:.*/)?\\.\\.(?:/|\\Z))[^\\\\\\x00-\\x1f\\x7f]+\\Z",
}
# No argument may hold a NUL, which no process can be given (pre-review D10).
ARGUMENT = {"type": "string", "maxLength": 65536, "pattern": "^[^\\x00]*\\Z"}


def _spec(name: str, description: str, properties: dict[str, Any], required: list[str], risk: RiskClass,
          approval: ApprovalPolicy, timeout: float) -> ToolSpec:
    return ToolSpec(
        name=name, description=description,
        input_schema={"type": "object", "properties": properties, "required": required,
                      "additionalProperties": False},
        risk_class=risk, read_only=risk is RiskClass.READ_ONLY, idempotent=risk is RiskClass.READ_ONLY,
        approval_policy=approval, timeout_seconds=timeout, result_provenance=UNTRUSTED)


def specs(spec: WorkspaceSpec) -> dict[str, ToolSpec]:
    # The executor's deadline is past the command's own, so a command ends by its timeout and
    # reports it, rather than being abandoned by the executor (FR-148).
    deadline = spec.max_command_seconds + 30.0
    return {
        "run_command": _spec(
            "run_command",
            "Run a command in your workspace, a sandbox with no network. argv is the program and its "
            "arguments, run without a shell unless you name one. Returns the exit code and the output.",
            {"argv": {"type": "array", "items": ARGUMENT, "minItems": 1,
                      "maxItems": 4096},
             "stdin": {"type": "string", "maxLength": 1048576},
             "timeout_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": spec.max_command_seconds}},
            ["argv"], RiskClass.DESTRUCTIVE, spec.run_command_approval, deadline),
        "read_workspace_file": _spec(
            "read_workspace_file", "Read a text file in your workspace.", {"path": PATH}, ["path"],
            RiskClass.READ_ONLY, ApprovalPolicy.AUTO, deadline),
        "write_workspace_file": _spec(
            "write_workspace_file", "Create or replace a file in your workspace, with its parent folders.",
            {"path": PATH, "content": {"type": "string", "maxLength": 16 * 1024 * 1024}}, ["path", "content"],
            RiskClass.WRITE, ApprovalPolicy.AUTO, deadline),
        "list_workspace": _spec(
            "list_workspace", "List a folder of your workspace: names, kinds and sizes.",
            {"path": PATH}, [], RiskClass.READ_ONLY, ApprovalPolicy.AUTO, deadline),
    }


def describe(outcome: CommandOutcome) -> str:
    """What the model reads of a command: how it ended, then each stream in a labelled section,
    marked when cut."""
    lines = [f"exit code: {'none' if outcome.exit_code is None else outcome.exit_code}"]
    if outcome.killed is not None:
        lines.append(f"killed: {outcome.killed}")
    lines.append(f"duration: {outcome.duration:.2f} s")
    for label, data, cut, produced in (("stdout", outcome.stdout, outcome.stdout_cut, outcome.stdout_bytes),
                                       ("stderr", outcome.stderr, outcome.stderr_cut, outcome.stderr_bytes)):
        lines.append(f"--- {label} ---")
        lines.append(data.decode("utf-8", errors="replace"))
        if cut:
            lines.append(f"[{label} cut: {len(data)} of {produced} B kept]")
    return "\n".join(lines)


def one_file(data: bytes, path: str) -> bytes:
    """The regular file a path's export holds. A link, a FIFO or a device is refused by name
    (P5-D8); nothing is written to the host."""
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        found = [m for m in tar.getmembers() if m.name.removeprefix("./") not in ("", ".")]
        if len(found) != 1 or found[0].isdir():
            raise IsADirectoryError(f"{path} is a folder")
        member = found[0]
        if not member.isfile():
            raise ValueError(f"{path} is not a regular file")
        return tar.extractfile(member).read()


def entries(data: bytes) -> list[tuple[str, str, int]]:
    """A folder export's direct entries: (name, kind, size). Anything but a regular file or a
    folder is listed as what it is, never followed."""
    found: dict[str, tuple[str, int]] = {}
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for m in tar.getmembers():
            name = m.name.removeprefix("./").rstrip("/")
            if not name or name == "." or "/" in name:
                continue
            kind = "dir" if m.isdir() else "file" if m.isfile() else "link" if m.issym() or m.islnk() else "other"
            found[name] = (kind, m.size if m.isfile() else 0)
    return [(n, k, s) for n, (k, s) in sorted(found.items())]


def one_file_tar(path: str, content: bytes) -> bytes:
    """FR-143: the tar the SDK builds itself: one regular file, owned by 65534."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo(posixpath.normpath(path))
        info.size, info.mode, info.uid, info.gid = len(content), 0o644, 65534, 65534
        tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def workspace_tools(binding: Any, spec: WorkspaceSpec, names: Iterable[str]) -> list[Tool]:
    """The workspace tools `names` lists, bound to one run's workspace through `binding` -- the
    manager's handle on that run, which creates the workspace at its first need (FR-138)."""
    wanted = set(names)
    declared = specs(spec)

    async def run_command(argv: list[str], scope: ToolScope, stdin: str | None = None,
                          timeout_seconds: float | None = None) -> str:
        outcome = await binding.run(list(argv), stdin=None if stdin is None else stdin.encode("utf-8"),
                                    timeout=spec.max_command_seconds if timeout_seconds is None else timeout_seconds,
                                    tool_call_id=scope.tool_call_id)
        return describe(outcome)

    async def read_workspace_file(path: str) -> str:
        return one_file(await binding.export(path), path).decode("utf-8", errors="replace")

    async def write_workspace_file(path: str, content: str) -> str:
        data = content.encode("utf-8")
        await binding.import_(one_file_tar(path, data))
        return f"wrote {len(data)} B to {posixpath.normpath(path)}"

    async def list_workspace(path: str = ".") -> str:
        found = entries(await binding.export(path))
        shown = found[:MAX_ENTRIES]
        lines = [f"{name}\t{kind}\t{size}" for name, kind, size in shown]
        if len(found) > len(shown):
            lines.append(f"[{len(found) - len(shown)} more entries not shown]")
        return "\n".join(lines) if lines else "(empty)"

    functions = {"run_command": run_command, "read_workspace_file": read_workspace_file,
                 "write_workspace_file": write_workspace_file, "list_workspace": list_workspace}
    return [Tool(spec=declared[name], fn=functions[name]) for name in NAMES if name in wanted]
