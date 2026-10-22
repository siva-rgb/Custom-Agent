"""30 - MCP input requests: a server asks a person, and the call waits for the answer.

An MCP server can ask for more input while a call runs: a form to fill in, or a URL to open
(elicitation). The SDK records each request as an interruption of the run, as it does an
approval, and the call waits -- past its call_timeout, up to the run's approval_timeout -- until
the application answers with `Runner.respond`: accept, decline or cancel (FR-133). Here an
onboarding server's two tools ask:

  - `register` asks for a form, which a scripted person accepts with their name and team;
  - `connect_calendar` asks the person to open a URL, which they decline.

The run shows each request recorded with what it asked -- the schema, the full URL -- the answer
reaching the server, events that name the request and never the response, and the URL never
opened by the SDK: it points at a listener this script watches.

Run it
  python scripts/30_mcp_input.py            # live: gateway + PostgreSQL
  python scripts/30_mcp_input.py --offline  # in memory, scripted model
"""


import argparse
import asyncio
import json
import os
import site
import socket
import sys
import threading
import uuid

SERVER_NAME = "intake"
PYTHON = sys._base_executable
PACKAGES = [path for path in site.getsitepackages() if path.endswith("site-packages")][0]
FORM = {"type": "object", "properties": {"name": {"type": "string"}, "team": {"type": "string"}},
        "required": ["name", "team"]}


def serve() -> None:
    """The demo MCP server, in a child process: two tools that ask for input. Never imports the SDK."""
    site.addsitedir(sys.argv[sys.argv.index("--serve") + 1])
    consent = sys.argv[sys.argv.index("--consent") + 1]
    from mcp.server.mcpserver import Context, MCPServer
    from mcp.types import ElicitRequest, ElicitRequestFormParams, ElicitRequestURLParams, InputRequiredResult

    app = MCPServer(SERVER_NAME, log_level="WARNING")

    @app.tool()
    async def register(ctx: Context) -> str | InputRequiredResult:
        """Register the person for onboarding: asks for their name and team."""
        answer = (ctx.input_responses or {}).get("who")
        if answer is None:
            return InputRequiredResult(input_requests={"who": ElicitRequest(params=ElicitRequestFormParams(
                message="Who is joining, and which team?", requested_schema=FORM))}, request_state="asked")
        if answer.action != "accept":
            return f"registration {answer.action}d"
        return f"registered {answer.content['name']} to {answer.content['team']}"

    @app.tool()
    async def connect_calendar(ctx: Context) -> str | InputRequiredResult:
        """Connect the person's calendar: asks them to open a consent page."""
        answer = (ctx.input_responses or {}).get("consent")
        if answer is None:
            return InputRequiredResult(input_requests={"consent": ElicitRequest(params=ElicitRequestURLParams(
                message="Open this page to connect your calendar.", url=consent))}, request_state="asked")
        return "calendar connected" if answer.action == "accept" else f"calendar not connected ({answer.action})"

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

TENANT, PROJECT = "example-tenant", f"input-{uuid.uuid4().hex[:8]}"
ALICE = PrincipalContext(agent_principal="example-30", user_principal="alice")
TASK = ("Make exactly one tool call per turn: first call intake__register, then call intake__connect_calendar. "
        "Then say in one sentence what happened.")
STEPS = [("intake__register", {}), ("intake__connect_calendar", {})]


class ScriptedModel:
    """Offline: the two calls, one per turn, then an answer."""

    async def send(self, request):
        turn = sum(1 for m in request.messages if m.role is Role.ASSISTANT)
        if turn >= len(STEPS):
            return ModelResponse(message=Message(role=Role.ASSISTANT, content="Ada is registered; no calendar."),
                                 stop_reason=StopReason.END_TURN, usage=Usage(20, 5, 25))
        name, arguments = STEPS[turn]
        return ModelResponse(message=Message(role=Role.ASSISTANT, tool_calls=(
            ToolCall(id=f"t{turn}", name=name, arguments=arguments),)), stop_reason=StopReason.TOOL_CALLS,
            usage=Usage(20, 5, 25))


class Watched:
    """A loopback listener counting every connection: the consent URL points here."""

    def __init__(self) -> None:
        self.hits = 0
        self._socket = socket.socket()
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen()
        self._socket.settimeout(0.2)
        self.url = f"http://127.0.0.1:{self._socket.getsockname()[1]}/consent?state={uuid.uuid4().hex}"
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self._socket.accept()
            except OSError:
                continue
            self.hits += 1
            connection.close()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(2)
        self._socket.close()


async def demonstrate(model, model_id: str, persistence=None):
    watched = Watched()
    server = StdioServer(alias=SERVER_NAME, command=PYTHON,
                         args=(os.path.abspath(__file__), "--serve", PACKAGES, "--consent", watched.url),
                         approval="never")
    runner = Runner({"m": model}, mcp_servers=[server], persistence=persistence)
    agent = AgentSpec(id="onboarder", instructions="Follow the steps you are given exactly.", preferred_model=model_id,
                      tool_profile=("intake__*",))
    asked, answers = [], []
    try:
        async with runner:
            # The server's tools are usable once a principal accepts their versions (FR-127).
            first = await runner.run(agent, "Say which tools you have.", RunConfig(tenant_id=TENANT, project_id=PROJECT))
            hashes = [e.payload["tool_hash"] for e in first.events if e.event_type is EventType.MCP_TOOL_BLOCKED]
            await runner.accept_tools(TENANT, PROJECT, SERVER_NAME, hashes, principal=ALICE)
            handle = await runner.start(agent, TASK, RunConfig(tenant_id=TENANT, project_id=PROJECT, max_turns=6))

            async def person():
                # The scripted person: fills in the form, declines the URL.
                while True:
                    for view in await runner.interruptions(TENANT, PROJECT, run_id=handle.run_id):
                        request = view.interruption.request_payload
                        if view.interruption.kind.value == "tool_approval":
                            continue
                        asked.append(view)
                        if request["mode"] == "form":
                            print(f"form: {request['message']!r} {json.dumps(request['requested_schema'])}")
                            answers.append(await handle.respond(view.interruption.interruption_id, action="accept",
                                                                content={"name": "Ada", "team": "analytics"},
                                                                principal=ALICE))
                        else:
                            print(f"url: {request['message']!r} {request['url']} -- declined, not opened")
                            answers.append(await handle.respond(view.interruption.interruption_id, action="decline",
                                                                principal=ALICE))
                    await asyncio.sleep(0.05)

            answering = asyncio.create_task(person())
            try:
                result = await asyncio.wait_for(handle.result(), 300)
            finally:
                answering.cancel()
            tools = {r.tool_call_id: r.content for m in await asyncio.to_thread(
                runner._sessions_for(__import__("agentsdk.postgres", fromlist=["RunScope"]).RunScope(
                    run_id=result.run_id, tenant_id=TENANT, project_id=PROJECT)).history, result.run_id)
                for r in m.tool_results}
    finally:
        watched.close()
    print(f"run {result.run_id}: {result.status.value}; tool results {list(tools.values())}")
    requested = [e for e in result.events if e.event_type is EventType.INPUT_REQUESTED]
    answered = [e for e in result.events if e.event_type is EventType.INPUT_ANSWERED]
    form = next((v for v in asked if v.interruption.request_payload["mode"] == "form"), None)
    url = next((v for v in asked if v.interruption.request_payload["mode"] == "url"), None)
    checks = [
        ("the run completed", result.status is RunStatus.COMPLETED),
        ("the form request was recorded with its schema, and the person's answer reached the server",
         form is not None and form.interruption.request_payload["requested_schema"] == FORM
         and any(a.decision == "accept" and a.response == {"name": "Ada", "team": "analytics"} for a in answers)
         and "registered Ada to analytics" in tools.values()),
        ("the url request was recorded with the full URL and declined, and the server was told",
         url is not None and url.interruption.request_payload["url"] == watched.url
         and any(a.decision == "decline" for a in answers)
         and any("calendar not connected (decline)" in str(v) for v in tools.values())),
        ("the events name each request and its answer, and never the response",
         len(requested) == 2 and len(answered) == 2
         and not any("analytics" in json.dumps(e.payload) for e in requested + answered)),
        ("the SDK never opened the URL", watched.hits == 0),
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
    return await demonstrate(ScriptedModel(), "m:scripted", persistence)


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
            checks, runs = await demonstrate(client, f"m:{settings.default_model}", persistence)
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
