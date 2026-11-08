"""The Claude arm's driver, run inside its container (FR-179).

It reads one request -- the prompt, the turn limit, the tools, the model and the working folder --
as a JSON line on its standard input, runs the Python claude_agent_sdk against the pinned Claude
Code CLI installed in the image, and writes one JSON line per message: the assistant's text, and
the result with its turns, usage, cost estimate and subtype. The gateway's address and key come
from the container's environment, never from the request, so no credential is in the image, on a
command line or in what this prints.

`drive()` takes the query function as an argument, so the tests run it with a scripted stand-in.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

# The CLI the image's npm install placed, pinned in package.json (DECISION-05ee16ad).
CLI_PATH = "/opt/adr12/node_modules/.bin/claude"
ENVIRONMENT = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
               "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC")


def options_for(request: dict[str, Any], environ: dict[str, str]) -> Any:
    from claude_agent_sdk import ClaudeAgentOptions

    tools = list(request["tools"])
    return ClaudeAgentOptions(
        tools=tools,
        allowed_tools=tools,
        permission_mode="dontAsk",
        setting_sources=[],
        cwd=request["cwd"],
        model=request["model"],
        max_turns=int(request["max_turns"]),
        cli_path=CLI_PATH,
        env={name: environ[name] for name in ENVIRONMENT if name in environ},
    )


async def drive(request: dict[str, Any], query: Any, options: Any, emit: Any) -> int:
    """Run the request, emitting each message as a dict; the exit status of the run."""
    try:
        async for message in query(prompt=request["prompt"], options=options):
            event = describe(message)
            if event is not None:
                emit(event)
    except Exception as exc:  # noqa: BLE001 - reported as the run's error, never a traceback with its env
        emit({"kind": "error", "error": f"{type(exc).__name__}"})
        return 1
    return 0


def describe(message: Any) -> dict[str, Any] | None:
    kind = getattr(message, "kind", None)
    if kind in ("assistant", "result"):  # a scripted stand-in names its kind
        return {"kind": kind, **{k: v for k, v in vars(message).items() if k != "kind"}}
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    if isinstance(message, AssistantMessage):
        texts = [b.text for b in message.content if isinstance(b, TextBlock) and b.text.strip()]
        return {"kind": "assistant", "text": "\n".join(texts)} if texts else None
    if isinstance(message, ResultMessage):
        return {"kind": "result", "num_turns": message.num_turns, "usage": message.usage,
                "total_cost_usd": message.total_cost_usd, "subtype": message.subtype, "is_error": message.is_error}
    return None


def main() -> int:
    request = json.loads(sys.stdin.readline())
    from claude_agent_sdk import query

    def emit(event: dict[str, Any]) -> None:
        print(json.dumps(event, default=str), flush=True)

    return asyncio.run(drive(request, query, options_for(request, dict(os.environ)), emit))


if __name__ == "__main__":
    raise SystemExit(main())
