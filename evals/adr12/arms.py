"""The two arms of the comparison (FR-61).

Both run the same task, with the same model through the same gateway, the same turn limit and
read-only tools over their own copy of the fixture folder. Each arm takes its runner as an
argument -- a model client factory here, a query function there -- so the whole task set runs
offline with scripted stand-ins (AC-48), and the live command passes the real ones.

Nothing under agentsdk imports this module, and this module is never in the wheel (NFR-20).
"""

from __future__ import annotations

import json
import pathlib
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from agentsdk import AgentSpec, RunConfig, Runner
from agentsdk.builtin_tools import glob_tool, grep_tool, read_file_tool
from agentsdk.model import Usage
from agentsdk.version import __version__ as AGENTSDK_VERSION

HERE = pathlib.Path(__file__).parent
# FR-61: the Claude arm's tools, and this SDK's Tier 1 file tools answer the same questions.
CLAUDE_TOOLS = ["Read", "Glob", "Grep"]
INSTRUCTIONS = (
    "You answer questions about the files in your working folder. Use your tools to read them, "
    "and answer with what the files say and nothing else. If a request needs a tool you do not "
    "have, say plainly that you cannot do it rather than pretending you did."
)


def cli_pin() -> str:
    """The Claude Code CLI version pinned beside this file (DECISION-05ee16ad)."""
    return json.loads((HERE / "package.json").read_text(encoding="utf-8"))["dependencies"]["@anthropic-ai/claude-code"]


@dataclass
class RunOutcome:
    """One run of one task on one arm, before the harness checks and costs it."""

    answer: str | None
    status: str
    turns: int
    usage: Usage
    wall_ms: float
    # Only the Claude arm estimates a cost of its own; FR-62 records it separately.
    sdk_cost_estimate: Decimal | None = None
    error: str | None = None


class ThisSdkArm:
    """This SDK with its M10 file tools, confined to the run's folder."""

    name = "agentsdk"
    wire_format = "OpenAI-compatible chat completions"

    def __init__(self, *, client_factory, model: str | None = None, model_registry: Any = None) -> None:
        self._client_factory = client_factory
        self._model = model
        self._registry = model_registry

    @property
    def version(self) -> str:
        return f"agentsdk {AGENTSDK_VERSION}"

    def tools_for(self, root: pathlib.Path) -> list[Any]:
        """Read, glob and grep, rooted at this run's folder: the same reach as Read, Glob and Grep."""
        return [read_file_tool(root), glob_tool(root), grep_tool(root)]

    async def run(self, task: Any, root: pathlib.Path) -> RunOutcome:
        tools = self.tools_for(root)
        options: dict[str, Any] = {"tools": tools}
        if self._registry is not None:
            options["model_registry"] = self._registry
        client = self._client_factory()
        runner = Runner({"model": client}, **options)
        spec = AgentSpec(
            id="adr12",
            instructions=INSTRUCTIONS,
            tool_profile=tuple(tool.spec.name for tool in tools),
            preferred_model=f"model:{self._model}" if self._model else None,
        )
        config = RunConfig(tenant_id="adr12", project_id="comparison", max_turns=task.max_turns)
        started = time.perf_counter()
        try:
            result = await runner.run(spec, task.prompt, config)
        finally:
            closer = getattr(client, "aclose", None)
            if closer is not None:
                await closer()
        wall_ms = (time.perf_counter() - started) * 1000
        turns = sum(1 for event in result.events if event.event_type.value == "ModelCalled")
        return RunOutcome(
            answer=result.output,
            status=result.status.value,
            turns=turns,
            usage=result.usage,
            wall_ms=wall_ms,
            sdk_cost_estimate=None,
            error=result.error,
        )


class ClaudeAgentSdkArm:
    """The raw Claude Agent SDK, driving the pinned Claude Code CLI against the same gateway."""

    name = "claude-agent-sdk"
    wire_format = "Anthropic Messages through the Claude Code CLI"

    def __init__(self, *, cli_path: Any, model: str, base_url: str, auth_token: str, query_fn: Any = None) -> None:
        self._cli_path = pathlib.Path(cli_path)
        self._model = model
        self._base_url = (base_url or "").rstrip("/")
        self._auth_token = auth_token
        self._query_fn = query_fn

    @property
    def version(self) -> str:
        from importlib.metadata import PackageNotFoundError, version as installed

        try:
            sdk = installed("claude-agent-sdk")
        except PackageNotFoundError:  # pragma: no cover - the extra is not installed
            sdk = "not installed"
        return f"claude-agent-sdk {sdk} with Claude Code CLI {cli_pin()}"

    def options_for(self, task: Any, root: pathlib.Path) -> Any:
        """Exactly FR-61's configuration; the gate asserts every field of it."""
        from claude_agent_sdk import ClaudeAgentOptions

        return ClaudeAgentOptions(
            tools=list(CLAUDE_TOOLS),
            allowed_tools=list(CLAUDE_TOOLS),
            permission_mode="dontAsk",
            setting_sources=[],
            cwd=str(root),
            model=self._model,
            max_turns=task.max_turns,
            cli_path=str(self._cli_path),
            env={
                "ANTHROPIC_BASE_URL": self._base_url,
                "ANTHROPIC_AUTH_TOKEN": self._auth_token,
                "ANTHROPIC_MODEL": self._model,
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": self._model,
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            },
        )

    async def run(self, task: Any, root: pathlib.Path) -> RunOutcome:
        query = self._query_fn if self._query_fn is not None else _live_query()
        options = self.options_for(task, root)
        answer: str | None = None
        result: Any = None
        started = time.perf_counter()
        error: str | None = None
        try:
            async for message in query(prompt=task.prompt, options=options):
                kind, text = _describe(message)
                if kind == "assistant" and text:
                    answer = text
                elif kind == "result":
                    result = message
        except Exception as exc:  # noqa: BLE001 - a failed run is a row, not a crashed comparison
            error = f"{type(exc).__name__}: {exc}"
        wall_ms = (time.perf_counter() - started) * 1000
        return RunOutcome(
            answer=answer,
            status=_status_of(result, error),
            turns=int(getattr(result, "num_turns", 0) or 0),
            usage=_normalised_usage(getattr(result, "usage", None)),
            wall_ms=wall_ms,
            sdk_cost_estimate=_estimate(getattr(result, "total_cost_usd", None)),
            error=error,
        )


def _live_query() -> Any:
    from claude_agent_sdk import query

    return query


def _describe(message: Any) -> tuple[str | None, str | None]:
    """The kind and text of one message, from the SDK's types or a scripted stand-in."""
    kind = getattr(message, "kind", None)
    if kind in ("assistant", "result"):
        return kind, getattr(message, "text", None)
    try:
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
    except ImportError:  # pragma: no cover - the extra is not installed
        return None, None
    if isinstance(message, AssistantMessage):
        texts = [block.text for block in message.content if isinstance(block, TextBlock) and block.text.strip()]
        return "assistant", "\n".join(texts) if texts else None
    if isinstance(message, ResultMessage):
        return "result", None
    return None, None


def _status_of(result: Any, error: str | None) -> str:
    if error is not None or result is None:
        return "failed"
    subtype = str(getattr(result, "subtype", "") or "")
    if "max_turns" in subtype:
        return "max_turns_exceeded"
    return "failed" if getattr(result, "is_error", False) else "completed"


def _normalised_usage(raw: Any) -> Usage:
    """FR-62: the Claude arm's prompt count is input + cache read + cache creation, so both arms
    count the same tokens. A missing field counts as zero here and is reported as unavailable by
    the harness when the whole usage is missing."""
    if raw is None:
        return Usage()
    read = _count(raw, "cache_read_input_tokens")
    written = _count(raw, "cache_creation_input_tokens")
    prompt = _count(raw, "input_tokens") + read + written
    completion = _count(raw, "output_tokens")
    return Usage(prompt, completion, prompt + completion, read, written)


def _count(raw: Any, field: str) -> int:
    value = raw.get(field) if isinstance(raw, dict) else getattr(raw, field, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _estimate(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 - an estimate that cannot be read is no estimate
        return None
