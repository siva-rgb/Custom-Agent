"""18 - Context policy and compaction: an agent sees only its tools, and a long run compacts.

What it shows
  * a run carrying a ContextPolicy is sent only the tools its agent may execute: three
    are registered, the reader's profile names one, and one is all it sees (FR-77)
  * reading long pages pushes the prompt past 0.75 of the policy's context window, and
    the history compacts: the older turns are replaced by one summary (FR-78)
  * the compaction is recorded: a ContextCompacted event names the artifact holding
    what was replaced, with the token counts before and after, and that artifact reads
    back against its content hash
  * the summary keeps the pages' taint: they came from outside, and so does the summary

The window is set small on purpose, so the compaction happens within a few cheap turns.
In the live run the reader is the gateway's model and everything is stored in PostgreSQL.

Run it
  python scripts/18_context.py            # live: gateway + PostgreSQL (DATABASE_URL in .env)
  python scripts/18_context.py --offline  # in memory, a scripted model: no database, no network
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from agentsdk import AgentSpec, ContextPolicy, Message, Role, RunConfig, RunStatus, Runner
from agentsdk.events import EventType
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.primitives import ToolCall
from agentsdk.tools import ResultProvenance, Tool, ToolSpec

TENANT, PROJECT = "example-tenant", "examples"
PAGES = 5
POLICY = ContextPolicy(context_window=2000, keep_recent_turns=1)


def _key(n: int) -> str:
    return f"k{n * 7919 % 1000:03d}"


async def read_page(n: int, key: str) -> str:
    """A long page from outside the deployment, so its result is tainted. Each page
    gives the key to the next, so pages are read one per turn: a model reading them all
    at once would make one turn, and a turn is never split by a compaction."""
    expected = "start" if n == 1 else _key(n - 1)
    if key != expected:
        raise ValueError(f"wrong key for page {n}: read page {n - 1} first, which ends with this page's key")
    body = " ".join(f"Page {n}, line {line}: the archive records item {n * 100 + line}." for line in range(40))
    follow = f" To read page {n + 1}, use key {_key(n)}." if n < PAGES else " This is the last page."
    return body + follow


async def send_email(to: str) -> str:
    return f"sent to {to}"


async def delete_archive() -> str:
    return "deleted"


TOOLS = [
    Tool(spec=ToolSpec(name="read_page", description="Read one page of the archive, with the key the page before it gave.", input_schema={
        "type": "object", "properties": {"n": {"type": "integer"}, "key": {"type": "string"}}, "required": ["n", "key"]},
        result_provenance=ResultProvenance.external()), fn=read_page),
    Tool(spec=ToolSpec(name="send_email", description="Send an email.", input_schema={
        "type": "object", "properties": {"to": {"type": "string"}}, "required": ["to"]}), fn=send_email),
    Tool(spec=ToolSpec(name="delete_archive", description="Delete the archive.", input_schema={"type": "object"}),
         fn=delete_archive),
]
READER = AgentSpec(
    id="reader",
    instructions=(
        f"Read the archive with read_page, one page per turn, from page 1 to page {PAGES}: page 1 "
        f"takes the key 'start', and each page ends with the key for the next. "
        f"After page {PAGES}, answer in one sentence with the item number on page {PAGES}, line 0."
    ),
    tool_profile=("read_page",),
)


class Watching:
    """The model client, with the tool names of every request the reader made kept, to
    show what it was sent. A compaction's summarising call is sent no tools at all."""

    def __init__(self, client):
        self.client, self.sent = client, []

    async def send(self, request):
        if not (request.instructions or "").startswith("You compact"):
            self.sent.append(sorted(schema["function"]["name"] for schema in request.tools))
        return await self.client.send(request)


class ScriptedReader:
    """Offline: reads page after page, answers, and summarises when asked to."""

    def __init__(self):
        self.turn = 0

    async def send(self, request):
        size = sum(len(m.content or "") + sum(len(r.content or "") for r in m.tool_results) for m in request.messages)
        usage = Usage(size // 4, 5, size // 4 + 5)
        if (request.instructions or "").startswith("You compact"):
            return ModelResponse(message=Message(role=Role.ASSISTANT, content=(
                "The reader has read the earlier pages of the archive; each lists forty items.")),
                stop_reason=StopReason.END_TURN, usage=usage)
        self.turn += 1
        if self.turn <= PAGES:
            key = "start" if self.turn == 1 else _key(self.turn - 1)
            call = ToolCall(id=f"read-{self.turn}", name="read_page", arguments={"n": self.turn, "key": key})
            return ModelResponse(message=Message(role=Role.ASSISTANT, tool_calls=(call,)),
                                 stop_reason=StopReason.TOOL_CALLS, usage=usage)
        return ModelResponse(message=Message(role=Role.ASSISTANT, content=f"Page {PAGES}, line 0 records item {PAGES * 100}."),
                             stop_reason=StopReason.END_TURN, usage=usage)


async def demonstrate(runner, watching, model, artifacts):
    result = await runner.run(READER, "Read the archive.", RunConfig(
        tenant_id=TENANT, project_id=PROJECT, model_override=model, max_turns=PAGES + 4, context_policy=POLICY))
    registered = sorted(tool.spec.name for tool in TOOLS)
    print(f"registered tools: {registered}")
    print(f"tools the reader was sent, every turn: {sorted(set(map(tuple, watching.sent)))}")
    compacted = [e.payload for e in result.events if e.event_type is EventType.CONTEXT_COMPACTED]
    checks = [
        ("the reader was sent only the tool its profile names", {tuple(s) for s in watching.sent} == {("read_page",)}),
        ("the run compacted at least once", bool(compacted)),
    ]
    if compacted:
        first = compacted[0]
        print(f"ContextCompacted: turn {first['turn']}, {first['replaced_messages']} messages replaced, "
              f"about {first['tokens_before']} tokens before and {first['tokens_after']} after "
              f"(window {first['context_window']}, compacting at {first['compact_at']})")
        print(f"  the replaced turns are artifact {first['artifact']}")
        print(f"  the summary's labels: {first['summary_provenance']}")
        artifact_id = first["artifact"].rsplit(":", 1)[-1]
        ref = await artifacts.metadata(artifact_id)
        replaced = json.loads(await artifacts.get(artifact_id))  # get() verifies the content hash
        print(f"  read back: {ref.size} bytes, hash {ref.content_hash[:16]}..., {len(replaced)} messages")
        checks += [
            ("the prompt was smaller after the compaction", first["tokens_after"] < first["tokens_before"]),
            ("the artifact reads back against its hash and holds what was replaced",
             ref.content_hash == first["content_hash"] and len(replaced) == first["replaced_messages"]),
            ("the summary keeps the pages' taint", first["summary_provenance"]["trust_zone"] == "untrusted"),
        ]
    print(f"answer: {result.output!r}")
    checks.append(("the run completed", result.status is RunStatus.COMPLETED))
    return checks


async def offline():
    watching = Watching(ScriptedReader())
    runner = Runner({"reader": watching}, tools=TOOLS)
    # In memory there is no Persistence to hand out the store; this is the Runner's own.
    return await demonstrate(runner, watching, "reader:scripted", runner._artifacts_for(TENANT, PROJECT))


def live():
    from dotenv import load_dotenv

    from agentsdk import Persistence
    from agentsdk.config import Settings, normalise_database_url
    from agentsdk.postgres import close_pools
    from agentsdk.providers import OpenAICompatibleModelClient

    load_dotenv()
    persistence = Persistence.postgres(normalise_database_url(os.environ["DATABASE_URL"]))
    settings = Settings.from_env(load_dotfile=False)

    async def go():
        client = OpenAICompatibleModelClient(base_url=settings.base_url, api_key=settings.api_key, model=settings.default_model)
        try:
            watching = Watching(client)
            runner = Runner({"reader": watching}, tools=TOOLS, persistence=persistence)
            return await demonstrate(runner, watching, f"reader:{settings.default_model}",
                                     persistence.artifact_store(TENANT, PROJECT))
        finally:
            await client.aclose()

    try:
        return asyncio.run(go())
    finally:
        close_pools()


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="run in memory with a scripted model")
    checks = asyncio.run(offline()) if parser.parse_args().offline else live()
    for label, passed in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {label}")
    failed = [label for label, passed in checks if not passed]
    if failed:
        raise SystemExit(f"failed: {failed}")
