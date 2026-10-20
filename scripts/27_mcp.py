"""27 - Native MCP: an agent uses the tools of an MCP server.

The SDK connects to MCP servers itself (FR-123 to FR-126). Here a small glossary server, in
this same file, is started over stdio. Its tools reach the agent as `glossary__<name>`,
only because the agent's profile names `glossary__*`; one tool is filtered out by the
server's `allow` list; a tool that fails reaches the model as an error; and every result in
the run's history is labelled untrusted MCP data, whatever the server says (NFR-35). The
manifest records the server's command and the names, never the values, of what it was given.

Compare example 06, which bridges a server's tools by hand.

Run it
  python scripts/27_mcp.py            # live: gateway + PostgreSQL
  python scripts/27_mcp.py --offline  # in memory, scripted model
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

SERVER_NAME = "glossary"


def serve() -> None:
    """The demo MCP server. Runs in a child process; never imports the SDK."""
    from mcp.server.mcpserver import MCPServer

    app = MCPServer(SERVER_NAME, log_level="WARNING")
    glossary = {
        "agent": "a program that uses a model to decide which tools to call",
        "tool": "a function an agent may call, described by a JSON Schema",
    }

    @app.tool()
    def define(word: str) -> str:
        """Define a word from the glossary."""
        return glossary.get(word.lower(), f"no definition for {word!r}")

    @app.tool()
    def lookup_everything() -> str:
        """Fails: the glossary has no such index."""
        raise ValueError("the glossary has no index of everything")

    @app.tool()
    def forget(word: str) -> str:
        """Remove a word. Left out by the client's allow list."""
        return glossary.pop(word, "") and "forgotten"

    app.run()  # stdio


if "--serve" in sys.argv:  # before any SDK import: the server process needs none
    serve()
    raise SystemExit(0)

from agentsdk import AgentSpec, Message, Role, RunConfig, RunStatus, Runner  # noqa: E402
from agentsdk.mcp import StdioServer  # noqa: E402
from agentsdk.model import ModelResponse, StopReason, Usage  # noqa: E402
from agentsdk.postgres import RunScope  # noqa: E402
from agentsdk.primitives import InstructionAuthority, Origin, ToolCall, TrustZone  # noqa: E402

TENANT, PROJECT = "example-tenant", "examples"
TASK = ("Define the word 'agent' with glossary__define, then call glossary__lookup_everything once, "
        "then answer in one sentence.")


def glossary_server() -> StdioServer:
    # Approval is P4-D20's default; this example shows the client, so it asks for none.
    # The python that runs this file starts the server (on Windows, prefer the real
    # interpreter to a venv launcher, so the Job Object holds the server's children).
    return StdioServer(alias=SERVER_NAME, command=sys.executable, args=(os.path.abspath(__file__), "--serve"),
                       allow=("define", "lookup_everything"), approval="never")


class ScriptedModel:
    """Offline: defines a word, calls the failing tool, then the filtered one, then answers."""

    def __init__(self) -> None:
        self.requests = []

    async def send(self, request):
        self.requests.append(request)
        turn = sum(1 for m in request.messages if m.role is Role.ASSISTANT)
        calls = [[ToolCall(id="t0", name="glossary__define", arguments={"word": "agent"})],
                 [ToolCall(id="t1", name="glossary__lookup_everything", arguments={})],
                 [ToolCall(id="t2", name="glossary__forget", arguments={"word": "agent"})]]
        if turn < len(calls):
            return ModelResponse(message=Message(role=Role.ASSISTANT, tool_calls=tuple(calls[turn])),
                                 stop_reason=StopReason.TOOL_CALLS, usage=Usage(20, 5, 25))
        return ModelResponse(message=Message(role=Role.ASSISTANT, content="An agent decides which tools to call."),
                             stop_reason=StopReason.END_TURN, usage=Usage(20, 5, 25))


async def demonstrate(runner: Runner, model_id: str, requests: list | None, *, live: bool, persisted: bool):
    agent = AgentSpec(id="glossary-reader", instructions="Use the glossary tools as asked.",
                      preferred_model=model_id, tool_profile=("glossary__*",))
    async with runner:
        result = await runner.run(agent, TASK, RunConfig(tenant_id=TENANT, project_id=PROJECT, max_turns=6))
    sessions = runner._sessions_for(RunScope(run_id=result.run_id, tenant_id=TENANT, project_id=PROJECT))
    history = await asyncio.to_thread(sessions.history, result.run_id)
    issued = {c.id: c.name for m in history if m.role is Role.ASSISTANT for c in m.tool_calls}
    results = [r for m in history for r in m.tool_results]
    mcp = [r for r in results if issued.get(r.tool_call_id, "").startswith("glossary__")
           and not r.content.startswith("ToolNotFound")]

    print(f"run {result.run_id}: {result.status.value}")
    for r in results:
        print(f"  {issued.get(r.tool_call_id)} -> {'error: ' if r.is_error else ''}{r.content[:80]}"
              f"  [{r.provenance.origin.value}, {r.provenance.trust_zone.value}]")

    checks = [
        ("the run completed", result.status is RunStatus.COMPLETED),
        ("every MCP result in the history is untrusted, data-only MCP content, from mcp://glossary/",
         bool(mcp) and all(r.provenance.origin is Origin.MCP_RESOURCE and r.provenance.trust_zone is TrustZone.UNTRUSTED
                           and r.provenance.instruction_authority is InstructionAuthority.DATA_ONLY
                           and (r.provenance.source_uri_or_hash or "").startswith(("mcp://glossary/", "urn:"))
                           for r in mcp)),
    ]
    if requests is not None:
        offered = {t["function"]["name"] for t in requests[0].tools}
        by_id = {r.tool_call_id: r for r in results}
        checks += [
            ("the tools were offered by qualified name, and the filtered one was not",
             offered == {"glossary__define", "glossary__lookup_everything"}),
            ("a definition came back from the server",
             by_id["t0"].content == "a program that uses a model to decide which tools to call"),
            ("the failing tool reached the model as an error result", by_id["t1"].is_error),
            ("a call to the filtered tool was refused as unknown", by_id["t2"].content.startswith("ToolNotFound")),
        ]
    if persisted:
        from agentsdk.config import normalise_database_url

        import psycopg

        with psycopg.connect(normalise_database_url(os.environ["DATABASE_URL"])) as conn:
            [[servers]] = conn.execute("SELECT mcp_servers FROM execution_manifests WHERE run_id = %s",
                                       (result.run_id,)).fetchall()
        print(f"  manifest: {json.dumps(servers)}")
        checks.append(("the manifest records the server's command and its allow list",
                       len(servers) == 1 and servers[0]["command"] == sys.executable
                       and servers[0]["allow"] == ["define", "lookup_everything"]))
    return checks, [result.run_id]


async def offline(store: str = "memory"):
    persistence = None
    if store == "postgres":
        from dotenv import load_dotenv

        from agentsdk import Persistence
        from agentsdk.config import normalise_database_url

        load_dotenv()
        persistence = Persistence.postgres(normalise_database_url(os.environ["DATABASE_URL"]), create_schema=False)
    model = ScriptedModel()
    runner = Runner({"scripted": model}, mcp_servers=[glossary_server()], persistence=persistence)
    return await demonstrate(runner, "scripted:model", model.requests, live=False, persisted=store == "postgres")


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
        client = OpenAICompatibleModelClient(base_url=settings.base_url, api_key=settings.api_key,
                                             model=settings.default_model)
        try:
            runner = Runner({"m": client}, mcp_servers=[glossary_server()], persistence=persistence)
            checks, runs = await demonstrate(runner, f"m:{settings.default_model}", None, live=True, persisted=True)
            print(f"\nrun ids (to remove the demo's rows): {' '.join(runs)}")
            return checks
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
    checks = asyncio.run(offline())[0] if parser.parse_args().offline else live()
    for label, passed in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {label}")
    failed = [label for label, passed in checks if not passed]
    if failed:
        raise SystemExit(f"failed: {failed}")
