"""The two arms of the sandbox tasks (FR-179, DECISION-d0269633).

This SDK's arm runs the general preset (FR-174) with the task's files and its allowed host as
egress, `keep=True`, tenant `adr12` and project `comparison`. An approver refuses every export at
once, and each refusal is recorded by the Runner. After the run the kept workspace is read back
through the provider's `export` -- FR-143's checked tar, parsed in memory, so the host never opens
a workspace path (NFR-43) -- and then released by its recorded id.

The Claude arm runs the Python `claude_agent_sdk` driving the pinned Claude Code CLI, both inside a
disposable container made from the image `evals/adr12/Dockerfile` builds. The task's files are
copied in, the driver is given the prompt, tools and turn limit on its standard input, the working
folder is read back through the same checked tar, and the container is removed. The gateway's key
reaches only the container's environment: `docker create -e NAME` names it, and the value travels
in the docker client's own environment, never on a command line or in the image, and the arm
redacts it from every error and answer it records. That client is allowed `create` with exactly
`--pull never`, `cp`, `start`, `image inspect`, a listing of the comparison's own labels and `rm`
of its own containers, each call's output bounded: nothing here pulls or builds an image (AC-108).
A container's removal survives a cancellation, and a closing sweep removes any the labels still
name.

`Verifier` checks a code task's module in a fresh container of the workspace image with no
network, on visible and hidden cases, and `fetch_witness` fetches the page the fetch tasks are
judged against (DECISION-b944d3ab).

Nothing under agentsdk imports this module (NFR-51).
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import tarfile
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from agentsdk import Runner
from agentsdk.budget import BUDGET_EXCEEDED, BudgetPolicy
from agentsdk.events import EventType
from agentsdk.identity import PrincipalContext
from agentsdk.model import Usage
from agentsdk.presets import GENERAL_VERSION, general_agent
from agentsdk.version import __version__ as AGENTSDK_VERSION
from agentsdk.workspace import normal_path
from agentsdk.workspace.docker import MAX_TRANSFER_BYTES, read_tar

from .arms import RunOutcome, _estimate, _normalised_usage, cli_pin
from .fetch_proof import fetch_calls, named_hosts  # noqa: F401 - named_hosts is part of this module's surface
from .sandbox import VERIFIER, SandboxTask, ceilings, verification_input, verification_result

TENANT, PROJECT = "adr12", "comparison"
APPROVER = PrincipalContext(agent_principal="adr12-approver", user_principal="adr12")
# FR-179: the Claude arm's tools on a sandbox task.
CLAUDE_SANDBOX_TOOLS = ("Read", "Glob", "Grep", "Bash", "Write", "Edit")
CLAUDE_WORKDIR = "/work"
# The container's own user, created by the Dockerfile; the copied files are given to it.
CLAUDE_UID = 1000
CLAUDE_LABEL = "adr12.claude-arm"
CLAUDE_IMAGE = "adr12-claude:cli-" + "2.1.267"
# The variables the driver reads; only their names reach a command line.
CLAUDE_KEY_VARIABLE = "ANTHROPIC_AUTH_TOKEN"
# What one container run may take before it is removed: the longest task's turns, generously.
CONTAINER_SECONDS = 1800
SETUP = {
    "agentsdk": "the general preset (FR-174) in a workspace of its own, egress by allow list",
    "claude-agent-sdk": "the Claude Agent SDK and the pinned CLI in a disposable container, ordinary networking",
}


@dataclass
class SandboxOutcome(RunOutcome):
    """A sandbox run: what RunOutcome holds, the workspace read back, and its records."""

    files: Mapping[str, bytes] = field(default_factory=dict)
    exports_refused: int | None = None
    run_id: str | None = None
    workspace_id: str | None = None
    # The hosts the arm shows the run reached (DECISION-7830ca10): this SDK's arm from its
    # workspace's allowed connections, the Claude arm from its fetch tool calls.
    fetched: frozenset[str] = frozenset()
    # Round-1 F1: what showed each fetch, kept in the row so a credit can be checked afterwards: this
    # SDK's arm's connections by id, the Claude arm's calls as it gave them (fetch_proof).
    fetch_evidence: tuple[str, ...] = ()


def read_back(data: bytes) -> dict[str, bytes]:
    """FR-143: a workspace's files from its checked tar, in memory. read_tar refuses anything but
    regular files and directories at normalised relative paths, within the transfer bounds; then
    each regular file's bytes are read from the stream. Nothing is extracted to the host."""
    read_tar(data)
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r|") as tar:
        for member in tar:
            if not member.isfile():
                continue
            key = normal_path(member.name)
            handle = tar.extractfile(member)
            files[key] = handle.read() if handle is not None else b""
    return files


class PresetArm:
    """This SDK's arm on a sandbox task: the general preset, its exports refused."""

    name = "agentsdk"
    wire_format = "OpenAI-compatible chat completions"

    def __init__(self, *, client_factory: Any, model: str, registry: Any, image: str, provider: Any,
                 persistence: Any = None) -> None:
        self._client_factory = client_factory
        self._model = model
        self._registry = registry
        self._image = image
        self._provider = provider
        self._persistence = persistence

    @property
    def version(self) -> str:
        return f"agentsdk {AGENTSDK_VERSION}, preset {GENERAL_VERSION}"

    @property
    def image(self) -> str:
        return self._image

    def agent_for(self, task: SandboxTask):
        """The general agent FR-179 names: the task's files, its egress, keep=True, the budget rule."""
        usd, tokens = ceilings(task.kind)
        return general_agent(
            model=f"model:{self._model}", image=self._image, registry=self._registry,
            budget=BudgetPolicy(run_ceiling_usd=Decimal(usd), run_ceiling_tokens=tokens),
            egress=task.egress, files=dict(task.files), keep=True, max_turns=task.max_turns,
        )

    async def run_sandbox(self, task: SandboxTask) -> SandboxOutcome:
        agent = self.agent_for(task)
        usd, tokens = ceilings(task.kind)
        client = self._client_factory()
        runner = Runner({"model": client}, model_registry=self._registry, workspace_provider=self._provider,
                        persistence=self._persistence)
        started = time.perf_counter()
        result, error, files, refused, workspace_id = None, None, {}, None, None
        fetched: frozenset[str] = frozenset()
        evidence: tuple[str, ...] = ()
        wall_ms = 0.0
        try:
            try:
                handle = await runner.start(agent.spec, task.prompt, agent.config(TENANT, PROJECT))
            except Exception as exc:  # noqa: BLE001 - a run refused at its start is a failed row
                return SandboxOutcome(answer=None, status="failed", turns=0, usage=Usage(),
                                      wall_ms=(time.perf_counter() - started) * 1000,
                                      error=f"{type(exc).__name__}: {exc}", budget_usd=usd, budget_tokens=tokens)
            refusing = asyncio.ensure_future(_refuse_every_export(runner, handle.run_id))
            try:
                result = await handle.result()
            finally:
                # Pre-review F1: the approver's end never replaces the run's own.
                refusing.cancel()
                await asyncio.gather(refusing, return_exceptions=True)
            wall_ms = (time.perf_counter() - started) * 1000
            try:
                refused = await _refusals(runner, result.run_id)
            except Exception:  # noqa: BLE001 - the count is a measure; a measure not read is unavailable
                refused = None
            made = [e.payload["workspace_id"] for e in result.events if e.event_type is EventType.WORKSPACE_CREATED]
            workspace_id = made[0] if made else None
            if workspace_id is not None:
                try:
                    files = await self._read_kept(workspace_id)
                except Exception as exc:  # noqa: BLE001 - a workspace that cannot be read is a failed row
                    error = f"read back: {type(exc).__name__}: {exc}"
                # Pre-review F6: a release that fails is the row's error; aclose() still removes it.
                try:
                    await runner.release_workspace(TENANT, PROJECT, workspace_id)
                except Exception as exc:  # noqa: BLE001
                    error = error or f"release: {type(exc).__name__}: {exc}"
                # DECISION-7830ca10: the evidence of a fetch is the workspace's recorded egress, read
                # after the release, which settles the proxy's last reports.
                try:
                    reached = await reached_connections(runner, result.run_id)
                    fetched = frozenset(row.host for row in reached)
                    # Round-1 F1: the connections that counted, to be checked against the store.
                    evidence = tuple(f"connection {row.connection_id} to {row.host}:{row.port}, allowed"
                                     for row in reached)
                except Exception as exc:  # noqa: BLE001 - evidence not read is no evidence
                    error = error or f"connections: {type(exc).__name__}: {exc}"
        finally:
            try:
                await runner.aclose()
            finally:
                closer = getattr(client, "aclose", None)
                if closer is not None:
                    await closer()
        turns = sum(1 for e in result.events if e.event_type is EventType.MODEL_CALLED)
        # FR-179: a run its budget stopped is reported as a terminal status of its own.
        return SandboxOutcome(
            answer=result.output, status=BUDGET_EXCEEDED if result.error == BUDGET_EXCEEDED else result.status.value,
            turns=turns, usage=result.usage, wall_ms=wall_ms, error=error or result.error, files=files,
            exports_refused=refused, run_id=result.run_id, workspace_id=workspace_id, budget_usd=usd,
            budget_tokens=tokens, fetched=fetched, fetch_evidence=evidence,
        )

    async def _read_kept(self, workspace_id: str) -> dict[str, bytes]:
        owned = await self._provider.list_owned({"agentsdk.workspace": workspace_id})
        if len(owned) != 1:
            raise LookupError(f"{len(owned)} workspaces hold the id {workspace_id}")
        return read_back(await self._provider.export(owned[0], ""))


async def _refuse_every_export(runner: Runner, run_id: str) -> None:
    """The approver: every request the run makes is refused at once, and the Runner records it.

    Pre-review F1: it outlives any one failure. A read or a refusal that raises is tried again on
    the next pass, and a request is only set aside once its refusal is recorded, so no request is
    left waiting for the approval timeout."""
    decided: set[str] = set()
    while True:
        try:
            views = await runner.interruptions(TENANT, PROJECT, run_id=run_id)
        except Exception:  # noqa: BLE001 - read again on the next pass
            views = []
        for view in views:
            item = view.interruption
            if item.interruption_id in decided:
                continue
            try:
                await runner.decide(TENANT, PROJECT, item.interruption_id, approve=False, principal=APPROVER,
                                    message="the comparison refuses every export")
                decided.add(item.interruption_id)
            except Exception:  # noqa: BLE001 - tried again on the next pass; a decided one leaves the list
                pass
        await asyncio.sleep(0.02)


async def reached_connections(runner: Runner, run_id: str) -> list[Any]:
    """The connections a run's workspace made through its proxy and was allowed (FR-162)."""
    store = runner._workspace_store()
    rows = await asyncio.to_thread(store.connections, TENANT, PROJECT, run_id)
    return [row for row in rows if row.allowed and row.run_id == run_id]


async def reached_hosts(runner: Runner, run_id: str) -> frozenset[str]:
    """The hosts a run's workspace connected to through its proxy and was allowed (FR-162)."""
    return frozenset(row.host for row in await reached_connections(runner, run_id))


async def _refusals(runner: Runner, run_id: str) -> int:
    views = await runner.interruptions(TENANT, PROJECT, run_id=run_id, pending_only=False)
    return sum(1 for v in views if v.decision is not None and v.decision.decision != "approve")


# --- the Claude arm --------------------------------------------------------------------------------


CONTAINER_PREFIXES = ("adr12-claude-", "adr12-verify-")
VERIFIER_LABEL = "adr12.verifier"
# Pre-review F3: what one docker call may hand back. A copy out is bounded as FR-143 bounds a
# transfer, with room for the tar's own headers; the driver's lines are bounded on their own.
COPY_LIMIT = MAX_TRANSFER_BYTES + 1024 * 1024
OUTPUT_LIMIT = 4 * 1024 * 1024
_CHUNK = 64 * 1024


class DockerCli:
    """The docker client the comparison drives, allowed only what it needs (AC-108): create with
    `--pull never`, cp, start, image inspect, a listing of its own labels, and rm of its own
    containers. Nothing it accepts pulls or builds an image."""

    ALLOWED = ("create", "cp", "start", "rm", "image", "ps")

    def __init__(self, executable: str = "docker") -> None:
        self._executable = executable

    @classmethod
    def checked(cls, argv: list[str]) -> list[str]:
        if not argv or argv[0] not in cls.ALLOWED or (argv[0] == "image" and argv[1:2] != ["inspect"]):
            raise ValueError(f"the comparison never runs docker {' '.join(argv[:2])}")
        # Pre-review F7: exactly --pull never, and only the comparison's own containers removed.
        if argv[0] == "create" and (argv.count("--pull") != 1 or argv[argv.index("--pull") + 1:][:1] != ["never"]
                                    or any(a.startswith("--pull=") for a in argv)):
            raise ValueError("docker create must say --pull never")
        if argv[0] == "rm" and (argv[1:2] != ["-f"] or len(argv) < 3
                                or not all(name.startswith(CONTAINER_PREFIXES) for name in argv[2:])):
            raise ValueError("the comparison removes only its own containers")
        if argv[0] == "ps" and argv[1:4] != ["-a", "--format", "{{.Names}}"] or argv[0] == "ps" and not (
                len(argv) == 6 and argv[4] == "--filter"
                and argv[5] in (f"label={CLAUDE_LABEL}=1", f"label={VERIFIER_LABEL}=1")):
            raise ValueError("the comparison lists only its own containers")
        return argv

    async def __call__(self, argv: list[str], *, stdin: bytes | None = None, env: Mapping[str, str] | None = None,
                       timeout: float = 120, limit: int = OUTPUT_LIMIT) -> tuple[int, bytes, bytes]:
        """Runs one docker call; its output past `limit` bytes ends the call with ValueError."""
        argv = self.checked(list(argv))
        process = await asyncio.create_subprocess_exec(
            self._executable, *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env={**os.environ, **(env or {})})
        out, err = bytearray(), bytearray()

        async def feed() -> None:
            if stdin:
                process.stdin.write(stdin)
                await process.stdin.drain()
            process.stdin.close()

        async def read(stream: Any, into: bytearray, bound: int) -> None:
            while chunk := await stream.read(_CHUNK):
                into.extend(chunk)
                if len(into) > bound:
                    raise ValueError(f"docker {argv[0]} wrote over {bound} B")

        steps = [asyncio.ensure_future(feed()), asyncio.ensure_future(read(process.stdout, out, limit)),
                 asyncio.ensure_future(read(process.stderr, err, 64 * 1024))]
        try:
            await asyncio.wait_for(asyncio.gather(*steps), timeout)
            await process.wait()
        except BaseException:
            for step in steps:
                step.cancel()
            await asyncio.gather(*steps, return_exceptions=True)
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            # A pipe left unread stays paused and never sees the end of a killed process, so its
            # wait would never return: read both to their end, keeping nothing, then wait.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.gather(_discard(process.stdout), _discard(process.stderr),
                                                      process.wait()), 30)
            raise
        return process.returncode, bytes(out), bytes(err)


async def _discard(stream: Any) -> None:
    while await stream.read(_CHUNK):
        pass


async def sweep(docker: Any) -> list[str]:
    """Pre-review F4: removes every container the comparison's labels name, whatever left it."""
    removed: list[str] = []
    for label in (f"label={CLAUDE_LABEL}=1", f"label={VERIFIER_LABEL}=1"):
        code, out, _ = await docker(["ps", "-a", "--format", "{{.Names}}", "--filter", label])
        names = [n for n in out.decode().split() if n.startswith(CONTAINER_PREFIXES)] if code == 0 else []
        if names:
            await docker(["rm", "-f", *names])
            removed += names
    return removed


async def _remove(docker: Any, name: str, *, created: bool) -> None:
    """Pre-review F4: removes one container. When its create may still be under way in the daemon
    (cancelled or failed), the removal is tried again after a pause, so a late create is caught."""
    for attempt in range(3 if not created else 1):
        with contextlib.suppress(Exception):
            await docker(["rm", "-f", name])
        if created:
            return
        await asyncio.sleep(1.0)


class Verifier:
    """The harness's verification of a code task (DECISION-b944d3ab): the files read back and a
    pristine verifier, in a fresh container of the workspace image with no network, no
    capabilities and bounded resources. Its result names a nonce no workspace code sees."""

    def __init__(self, *, image: str, docker: Any = None) -> None:
        self._image = image
        self._docker = docker if docker is not None else DockerCli()

    def create_argv(self, name: str) -> list[str]:
        return ["create", "--pull", "never", "-i", "--name", name, "--label", f"{VERIFIER_LABEL}=1",
                "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--pids-limit", "256", "--memory", "512m", "-e", "PYTHONDONTWRITEBYTECODE=1", "-w", "/work",
                "--entrypoint", "python", self._image, "-I", "/verify/verify.py"]

    async def verify(self, task: SandboxTask, files: Mapping[str, bytes]) -> str:
        """`pass`, or what failed, or why the verification could not run."""
        nonce = uuid.uuid4().hex
        name = f"adr12-verify-{uuid.uuid4().hex[:12]}"
        created = False
        payload = {**{f"work/{p}": d for p, d in files.items()}, "verify/verify.py": VERIFIER.encode(),
                   **{f"verify/{p}": d for p, d in (task.verification_files or {}).items()}}
        try:
            code, _, err = await self._docker(self.create_argv(name))
            if code != 0:
                return f"no verification: docker create failed: {_said(err)}"
            created = True
            code, _, err = await self._docker(["cp", "-", f"{name}:/"], stdin=files_into_work(payload, uid=0))
            if code != 0:
                return f"no verification: docker cp failed: {_said(err)}"
            code, out, _ = await self._docker(["start", "-a", "-i", name], stdin=verification_input(task, nonce),
                                              timeout=300)
            return verification_result(out, nonce)
        except Exception as exc:  # noqa: BLE001 - an unverified task is a failed check, never a crash
            return f"no verification: {type(exc).__name__}"
        finally:
            await asyncio.shield(_remove(self._docker, name, created=created))


def files_into_work(files: Mapping[str, bytes], uid: int = CLAUDE_UID) -> bytes:
    """Files as a tar for `docker cp -`, each owned by `uid`: the Claude container's user by default."""
    buffer = io.BytesIO()
    made: set[str] = set()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path in sorted(files):
            key = normal_path(path)
            parts = key.split("/")
            for depth in range(1, len(parts)):
                folder = "/".join(parts[:depth])
                if folder not in made:
                    made.add(folder)
                    info = tarfile.TarInfo(folder)
                    info.type, info.mode, info.uid, info.gid = tarfile.DIRTYPE, 0o755, uid, uid
                    tar.addfile(info)
            info = tarfile.TarInfo(key)
            info.size, info.mode, info.uid, info.gid = len(files[path]), 0o644, uid, uid
            tar.addfile(info, io.BytesIO(files[path]))
    return buffer.getvalue()


class ClaudeContainerArm:
    """The Claude Agent SDK and the pinned CLI, run in a disposable container per task."""

    name = "claude-agent-sdk"
    wire_format = "Anthropic Messages through the Claude Code CLI"

    def __init__(self, *, image: str, model: str, base_url: str, auth_token: str, docker: Any = None,
                 command: tuple[str, ...] = ()) -> None:
        self._image = image
        self._model = model
        self._base_url = (base_url or "").rstrip("/")
        self._auth_token = auth_token
        self._docker = docker if docker is not None else DockerCli()
        # The image's entrypoint runs the driver; a test may name another program in its place.
        self._command = tuple(command)

    @property
    def version(self) -> str:
        return f"claude-agent-sdk 0.2.152 with Claude Code CLI {cli_pin()}, in {self._image}"

    @property
    def image(self) -> str:
        return self._image

    def request_for(self, task: SandboxTask) -> dict[str, Any]:
        """What the driver is given on its standard input: no credential, which is in its environment."""
        return {"prompt": task.prompt, "max_turns": task.max_turns, "tools": list(CLAUDE_SANDBOX_TOOLS),
                "model": self._model, "cwd": CLAUDE_WORKDIR}

    def create_argv(self, name: str) -> list[str]:
        return ["create", "--pull", "never", "-i", "--name", name, "--label", f"{CLAUDE_LABEL}=1",
                "-e", CLAUDE_KEY_VARIABLE, "-e", f"ANTHROPIC_BASE_URL={self._base_url}",
                "-e", f"ANTHROPIC_MODEL={self._model}", "-e", f"ANTHROPIC_DEFAULT_HAIKU_MODEL={self._model}",
                "-e", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1", "-w", CLAUDE_WORKDIR,
                *(["--entrypoint", self._command[0]] if self._command else []), self._image, *self._command[1:]]

    async def run_sandbox(self, task: SandboxTask) -> SandboxOutcome:
        name = f"adr12-claude-{uuid.uuid4().hex[:12]}"
        secret = {CLAUDE_KEY_VARIABLE: self._auth_token}
        started = time.perf_counter()
        events: list[dict[str, Any]] = []
        files: dict[str, bytes] = {}
        error: str | None = None
        created = False
        try:
            code, _, err = await self._docker(self.create_argv(name), env=secret)
            if code != 0:
                raise RuntimeError(f"docker create failed: {_said(err)}")
            created = True
            code, _, err = await self._docker(["cp", "-", f"{name}:{CLAUDE_WORKDIR}"], stdin=files_into_work(task.files))
            if code != 0:
                raise RuntimeError(f"docker cp into the container failed: {_said(err)}")
            request = (json.dumps(self.request_for(task)) + "\n").encode()
            code, out, err = await self._docker(["start", "-a", "-i", name], stdin=request,
                                                timeout=CONTAINER_SECONDS)
            events = _events(out)
            if code != 0 and not events:
                error = f"the driver exited {code}: {_said(err)}"
            code, tar, err = await self._docker(["cp", f"{name}:{CLAUDE_WORKDIR}/.", "-"], limit=COPY_LIMIT)
            if code != 0:
                raise RuntimeError(f"docker cp out of the container failed: {_said(err)}")
            files = read_back(tar)
        except Exception as exc:  # noqa: BLE001 - a failed run is a row, not a crashed comparison
            # Pre-review F4: a timeout has no message of its own; its type is the message then.
            error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        finally:
            # Pre-review F4: a cancellation does not stop the removal, nor a create still under way.
            await asyncio.shield(_remove(self._docker, name, created=created))
        wall_ms = (time.perf_counter() - started) * 1000
        result = next((e for e in reversed(events) if e.get("kind") == "result"), None)
        answers = [e.get("text") for e in events if e.get("kind") == "assistant" and e.get("text")]
        error = error or next((f"the driver reported {e.get('error')}" for e in events if e.get("kind") == "error"), None)
        if result is None and error is None:
            error = "the driver reported no result"
        answer = answers[-1] if answers else None
        calls = fetch_calls(events, task.egress)
        return SandboxOutcome(
            fetched=frozenset(name for reached, _ in calls for name in reached),
            fetch_evidence=tuple(self._redacted(said) or "" for _, said in calls),
            answer=self._redacted(answer),
            status=_status(result, error),
            turns=int((result or {}).get("num_turns") or 0),
            usage=_normalised_usage((result or {}).get("usage")) if result and result.get("usage") else Usage(),
            wall_ms=wall_ms,
            sdk_cost_estimate=_estimate((result or {}).get("total_cost_usd")),
            error=self._redacted(error),
            files=files,
        )

    def _redacted(self, text: str | None) -> str | None:
        """Pre-review F5: the key is in the container's environment, so a program the run starts can
        print it; no text the arm records carries it."""
        if text is None or not self._auth_token:
            return text
        return text.replace(self._auth_token, "[redacted]")


def _events(out: bytes) -> list[dict[str, Any]]:
    """The driver's JSON lines; anything else on its output is not an event."""
    found = []
    for line in out.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line.startswith("{"):
            with contextlib.suppress(ValueError):
                value = json.loads(line)
                if isinstance(value, dict):
                    found.append(value)
    return found


def _status(result: Mapping[str, Any] | None, error: str | None) -> str:
    if result is None:
        return "failed"
    subtype = str(result.get("subtype") or "")
    if "max_turns" in subtype:
        return "max_turns_exceeded"
    # The run's own end; a read-back that failed after it is the row's error, not its status.
    return "failed" if result.get("is_error") else "completed"


def _said(err: bytes) -> str:
    text = " ".join(err.decode("utf-8", "replace").split())
    return text[:200] if text else "no message"


def fetch_witness(url: str = "https://example.com/", opener: Any = None) -> tuple[bytes, str]:
    """DECISION-b944d3ab: the page body the harness fetches itself, once per comparison, and when;
    the fetch tasks are judged against it on both arms."""
    import urllib.request
    from datetime import datetime, timezone

    request = urllib.request.Request(url, headers={"User-Agent": "adr12-comparison"})
    if opener is None:
        # Verified against certifi's bundle: the Windows store Python reads here lacks the root
        # example.com's certificate chains to (M50 live run 4); verification itself stays on.
        import ssl

        import certifi

        context = ssl.create_default_context(cafile=certifi.where())

        def opener(request: Any, timeout: float) -> Any:
            return urllib.request.urlopen(request, timeout=timeout, context=context)

    with opener(request, timeout=30) as response:
        body = response.read(MAX_TRANSFER_BYTES + 1)
    if len(body) > MAX_TRANSFER_BYTES:
        raise ValueError("the witness page is over the transfer bound")
    return body, datetime.now(timezone.utc).isoformat(timespec="seconds")
