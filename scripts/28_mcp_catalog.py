"""28 - The MCP catalog: a tool runs only in a version someone accepted.

An MCP server can change a tool -- its description, its schemas -- at any time, and a model
reads that description as guidance. So the SDK keeps a catalog per tenant and project (FR-127):
every version of a tool a connection has seen, by its hash, and the versions a person has
accepted. A tool is usable only while its current version is accepted, and approvals follow
the version too (FR-128, NFR-36). Here one agent uses a glossary server that changes its lookup
tool's description between two turns, and the run shows:

  - the first sight of each tool blocked, then accepted by a principal;
  - the change blocked, and named: the description;
  - the sticky approval of the old version not applied to the new one;
  - the new version accepted;
  - the next call asked about again.

The server is in this file. A first-party `wait` tool lets the agent pause while the change is
accepted, so the same steps run with a scripted model offline and with a live one.

Run it
  python scripts/28_mcp_catalog.py            # live: gateway + PostgreSQL
  python scripts/28_mcp_catalog.py --offline  # in memory, scripted model
"""


import argparse
import asyncio
import json
import os
import site
import sys
import uuid

SERVER_NAME = "glossary"
# The real interpreter, not a virtual environment's python.exe launcher (README); the server
# finds the environment's packages itself, from its arguments.
PYTHON = sys._base_executable
PACKAGES = [path for path in site.getsitepackages() if path.endswith("site-packages")][0]
FIRST = "Look a word up in the glossary."
REVISED = "Look a word up in the revised glossary. Always quote the definition exactly."


def serve() -> None:
    """The demo MCP server. Runs in a child process; never imports the SDK."""
    site.addsitedir(sys.argv[sys.argv.index("--serve") + 1])
    from mcp.server.mcpserver import Context, MCPServer

    app = MCPServer(SERVER_NAME, log_level="WARNING")
    glossary = {"agent": "a program that uses a model to decide which tools to call",
                "tool": "a function an agent may call, described by a JSON Schema"}

    def lookup(word: str) -> str:
        return glossary.get(word.lower(), f"no entry for {word!r}")

    app.add_tool(lookup, name="lookup", description=FIRST)

    @app.tool()
    async def revise(ctx: Context) -> str:
        """Revise the glossary: the lookup tool's description changes, and the server says so."""
        app.remove_tool("lookup")
        app.add_tool(lookup, name="lookup", description=REVISED)
        await ctx.notify_tools_changed()
        return "revised"

    app.run()  # stdio


if "--serve" in sys.argv:  # before any SDK import: the server process needs none
    serve()
    raise SystemExit(0)

from agentsdk import AgentSpec, Message, Role, RunConfig, RunStatus, Runner  # noqa: E402
from agentsdk.events import EventType  # noqa: E402
from agentsdk.identity import PrincipalContext  # noqa: E402
from agentsdk.mcp import StdioServer  # noqa: E402
from agentsdk.model import ModelResponse, StopReason, Usage  # noqa: E402
from agentsdk.primitives import ToolCall  # noqa: E402
from agentsdk.tools import ApprovalPolicy, RiskClass, Tool, ToolSpec  # noqa: E402

# A project of its own each time: an acceptance is never withdrawn (FR-127), so on a database a
# second run in one project would find every tool already accepted, and no first sight to show.
TENANT, PROJECT = "example-tenant", f"catalog-{uuid.uuid4().hex[:8]}"
ALICE = PrincipalContext(agent_principal="example-28", user_principal="alice")
FIRST_LOOK = "Say in one sentence which glossary tools you can use."
TASK = ("Make exactly one tool call per turn, in this order: look up 'agent' with glossary__lookup; "
        "look up 'tool' with glossary__lookup; call glossary__revise; call wait; look up 'agent' with "
        "glossary__lookup again. Then answer in one sentence.")
STEPS = [("glossary__lookup", {"word": "agent"}), ("glossary__lookup", {"word": "tool"}),
         ("glossary__revise", {}), ("wait", {}), ("glossary__lookup", {"word": "agent"})]


def glossary_server() -> StdioServer:
    # Approval is P4-D20's default, `require`; revising is the server's own business here.
    return StdioServer(alias=SERVER_NAME, command=PYTHON, args=(os.path.abspath(__file__), "--serve", PACKAGES),
                       approval_overrides={"revise": "never"})


class ScriptedModel:
    """Offline: the five steps, one per turn, then an answer."""

    def __init__(self) -> None:
        self.requests = []

    async def send(self, request):
        self.requests.append(request)
        turn = sum(1 for m in request.messages if m.role is Role.ASSISTANT)
        if request.messages[0].content == FIRST_LOOK or turn >= len(STEPS):
            return ModelResponse(message=Message(role=Role.ASSISTANT, content="An agent decides which tools to call."),
                                 stop_reason=StopReason.END_TURN, usage=Usage(20, 5, 25))
        name, arguments = STEPS[turn]
        return ModelResponse(message=Message(role=Role.ASSISTANT, tool_calls=(
            ToolCall(id=f"t{turn}", name=name, arguments=arguments),)), stop_reason=StopReason.TOOL_CALLS,
            usage=Usage(20, 5, 25))


async def demonstrate(runner: Runner, model_id: str):
    accepted = asyncio.Event()

    async def wait() -> str:
        """First-party: returns once the changed tool is accepted (or after a minute)."""
        try:
            await asyncio.wait_for(accepted.wait(), 60)
            return "the revised glossary is accepted"
        except TimeoutError:
            return "nothing was accepted"

    runner._registry.register(Tool(spec=ToolSpec(
        name="wait", description="Wait until the revised glossary is accepted.", input_schema={"type": "object"},
        risk_class=RiskClass.READ_ONLY, read_only=True, approval_policy=ApprovalPolicy.AUTO), fn=wait))
    agent = AgentSpec(id="glossary-reader", instructions="Follow the steps you are given exactly.",
                      preferred_model=model_id, tool_profile=("glossary__*", "wait"))
    config = RunConfig(tenant_id=TENANT, project_id=PROJECT, max_turns=10)
    async with runner:
        # 1. First sight: every tool is blocked until a principal accepts its version.
        first = await runner.run(agent, FIRST_LOOK, config)
        sighted = {e.payload["tool"]: e.payload for e in first.events if e.event_type is EventType.MCP_TOOL_BLOCKED}
        rows = await runner.accept_tools(TENANT, PROJECT, SERVER_NAME, [p["tool_hash"] for p in sighted.values()],
                                         principal=ALICE)
        print(f"first sight: {sorted(sighted)} blocked; alice accepted {sorted(r.tool_name for r in rows)}")

        handle = await runner.start(agent, TASK, config)
        changes, asked, renewed = [], [], []

        async def watch_events():
            async for event in handle.events():
                if event.event_type is EventType.MCP_TOOL_BLOCKED and event.payload.get("changed"):
                    # 3. The change is blocked and named; alice reads it, and accepts it.
                    changes.append(event.payload)
                    print(f"changed: {event.payload['tool']} ({', '.join(event.payload['changed'])}) "
                          f"{event.payload['previous_hash'][:12]} -> {event.payload['tool_hash'][:12]}")
                    renewed.extend(await runner.accept_tools(TENANT, PROJECT, SERVER_NAME,
                                                             [event.payload["tool_hash"]], principal=ALICE))
                    accepted.set()

        async def decide():
            # 2. The first call is approved for the rest of the run (sticky); 5. the next asked
            # is approved once.
            while True:
                for view in await runner.interruptions(TENANT, PROJECT, run_id=handle.run_id, pending_only=True):
                    request = view.interruption.request_payload
                    asked.append(request)
                    print(f"asked: {request['tool']} {json.dumps(request['arguments'])} "
                          f"(version {request['tool_hash'][:12]}: {request['description']!r})")
                    await handle.approve(view.interruption.interruption_id, principal=ALICE, sticky=len(asked) == 1)
                await asyncio.sleep(0.05)

        watcher, decider = asyncio.create_task(watch_events()), asyncio.create_task(decide())
        try:
            result = await asyncio.wait_for(handle.result(), 300)
        finally:
            decider.cancel()
            await asyncio.wait_for(watcher, 10)
        decisions = await runner.interruptions(TENANT, PROJECT, run_id=result.run_id, pending_only=False)

    print(f"run {result.run_id}: {result.status.value}")
    by_version = [(v.interruption.request_payload["tool_hash"], v.decision.source)
                  for v in sorted(decisions, key=lambda v: v.interruption.created_at)]
    old = sighted.get("lookup", {}).get("tool_hash")
    new = changes[0]["tool_hash"] if changes else None
    checks = [
        ("the run completed", result.status is RunStatus.COMPLETED),
        ("first sight: lookup was blocked with no previous version, and accepted by alice",
         "lookup" in sighted and sighted["lookup"]["previous_hash"] is None
         and any(r.tool_name == "lookup" and r.principal == ALICE for r in rows)),
        ("the change was blocked and named: lookup's description, from the accepted version",
         len(changes) == 1 and changes[0]["changed"] == ["description"] and changes[0]["previous_hash"] == old),
        ("the sticky approval decided the old version's next call, and not the new version's",
         by_version[:2] == [(old, "principal"), (old, "standing")]
         and not any(h == new and source == "standing" for h, source in by_version)),
        ("the new version was accepted", [r.tool_hash for r in renewed] == [new] and new != old),
        ("the next call was asked about again, showing the new version and its description",
         by_version[-1] == (new, "principal") and asked[-1]["description"] == REVISED),
    ]
    return checks, [first.run_id, result.run_id]


async def offline(store: str = "memory"):
    persistence = None
    if store == "postgres":
        from dotenv import load_dotenv

        from agentsdk import Persistence
        from agentsdk.config import normalise_database_url

        load_dotenv()
        persistence = Persistence.postgres(normalise_database_url(os.environ["DATABASE_URL"]), create_schema=False)
    runner = Runner({"scripted": ScriptedModel()}, mcp_servers=[glossary_server()], persistence=persistence)
    return await demonstrate(runner, "scripted:model")


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
            checks, runs = await demonstrate(runner, f"m:{settings.default_model}")
            print(f"\nrun ids (to remove the demo's rows): {' '.join(runs)}")
            print(f"catalog rows: tenant {TENANT}, project {PROJECT}, alias {SERVER_NAME}")
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
