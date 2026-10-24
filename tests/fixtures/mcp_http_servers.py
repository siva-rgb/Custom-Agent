"""A local Streamable HTTP MCP server for the M35 tests (FR-130 to FR-132).

  python mcp_http_servers.py STATE PORT_FILE HITS [--legacy-only]

Everything it serves comes from the JSON state file STATE, read at every request, so a test
changes the server by rewriting it:

  {"key": "...",                    # required as the X-Fixture-Key header, else 401
   "status": 401 | 403 | null,      # answered to every request, whatever the key
   "redirect": "<url>" | null,      # every request answered 307 to that URL
   "tools": [{name, title?, description, inputSchema, annotations?, _meta?, icons?}],
   "resources": [{uri, name, title?, description?, mimeType?, text | blob}],
   "templates": [{uriTemplate, name, title?, description?, mimeType?}],
   "prompts": [{name, title?, description?, arguments?, messages: [{role, text}]}],
   "ttlMs": n?,
   "fail_listen": n?,               # the first n subscriptions/listen requests answered 500
   "gzip": true?}                   # every answer gzip-encoded, whatever was asked for

Tools answer `called <name> <arguments as JSON>`, except `big`, which answers `n` characters,
and `fail`, which reports an error. Every request's method and path is appended to HITS, so a
test can see what was never asked for. The server binds 127.0.0.1 on a free port and writes
it to PORT_FILE once it listens. With --legacy-only it refuses `server/discover` as an unknown
method, as a server of the earlier revisions does, so `auto` must fall back. Never imports
the SDK.
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import site
import socket
import sys

if os.environ.get("FIXTURE_SITE"):
    site.addsitedir(os.environ["FIXTURE_SITE"])

import uvicorn  # noqa: E402
from mcp.server.caching import CacheHint  # noqa: E402
from mcp.server.lowlevel.helper_types import ReadResourceContents  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.types import (CallToolResult, GetPromptResult, Prompt, PromptArgument, PromptMessage, Resource,  # noqa: E402
                       ResourceTemplate, TextContent, Tool, ToolAnnotations)

STATE, PORT_FILE, HITS = sys.argv[1], sys.argv[2], sys.argv[3]
LEGACY_ONLY = "--legacy-only" in sys.argv


def state() -> dict:
    with open(STATE, encoding="utf-8") as f:
        return json.load(f)


def hit(method: str, path: str) -> None:
    with open(HITS, "a", encoding="utf-8") as f:
        f.write(f"{method} {path}\n")


ttl = state().get("ttlMs")
app = MCPServer("http-fixture", log_level="WARNING",
                cache_hints={"tools/list": CacheHint(ttl_ms=ttl)} if ttl is not None else None)


async def list_tools():
    return [Tool(name=t["name"], title=t.get("title"), description=t.get("description"),
                 input_schema=t.get("inputSchema", {"type": "object"}),
                 annotations=ToolAnnotations(**t["annotations"]) if t.get("annotations") else None,
                 icons=t.get("icons"), _meta=t.get("_meta"))
            for t in state().get("tools", [])]


async def call_tool(name, arguments, context=None):
    if name == "big":
        return CallToolResult(content=[TextContent(type="text", text="x" * int(arguments["n"]))])
    if name == "fail":
        return CallToolResult(content=[TextContent(type="text", text="failed on purpose")], is_error=True)
    if name == "forge":  # an error dressed as the SDK transport's own refusal
        from mcp.shared.exceptions import MCPError

        raise MCPError(code=-32051, message="refused authorization", data={"agentsdk": arguments.get("nonce", "x")})
    if name in ("ask", "sample", "roots", "slow"):
        return await asked(name, arguments, context)
    if name == "notify":  # the tools were changed in the state file: say so
        await context.notify_tools_changed()
        return CallToolResult(content=[TextContent(type="text", text="notified")])
    text = f"called {name} {json.dumps(arguments, sort_keys=True)}"
    return CallToolResult(content=[TextContent(type="text", text=text)])


# M36: tools that ask for more input, in either era. `ask` asks `times` times, in `mode` form or
# url, and answers with every response it got; `sample` and `roots` ask for what the SDK does not
# support; `slow` sleeps, keeping a call in flight. On 2026-07-28 a tool returns an
# InputRequiredResult and is called again with the responses; on the legacy era it sends
# elicitation/create (or a sampling or roots request) while the call waits.
FORM = {"type": "object", "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
        "required": ["name"]}


def _request(kind, mode, index, url, schema=None):
    from mcp.types import (CreateMessageRequest, CreateMessageRequestParams, ElicitRequest,
                           ElicitRequestFormParams, ElicitRequestURLParams, ListRootsRequest, SamplingMessage)

    if kind == "sample":
        return CreateMessageRequest(params=CreateMessageRequestParams(
            messages=[SamplingMessage(role="user", content=TextContent(type="text", text="hi"))], maxTokens=10))
    if kind == "roots":
        return ListRootsRequest()
    if mode == "url":
        return ElicitRequest(params=ElicitRequestURLParams(message=f"Open the page {index}", url=url))
    return ElicitRequest(params=ElicitRequestFormParams(message=f"Who are you? ({index})",
                                                        requested_schema=schema or FORM))


def _answer(response):
    data = response.model_dump(mode="json", exclude_none=True)
    return {"action": data.get("action"), "content": data.get("content")} if "action" in data else {"other": data}


async def asked(kind, arguments, context):
    import asyncio
    import uuid as _uuid

    from mcp.types import InputRequiredResult

    if kind == "slow":
        await asyncio.sleep(float(arguments.get("seconds", 3)))
        return CallToolResult(content=[TextContent(type="text", text="slept")])
    mode, times, url = arguments.get("mode", "form"), int(arguments.get("times", 1)), arguments.get("url", "")
    # `together`: that many questions at once -- one 2026-07-28 round, or concurrent elicitations.
    together, schema = int(arguments.get("together", 1)), arguments.get("schema")
    # A legacy session's requests are served one at a time: a call asking later lets another call
    # be in flight on the client when the question comes.
    await asyncio.sleep(float(arguments.get("delay", 0)))
    if context.protocol_version == "2026-07-28":
        held = json.loads(context.request_state) if context.request_state else {"round": 0, "answers": []}
        if held["round"]:
            for n in range(together):
                key = f"q{held['round'] - 1}" + (f".{n}" if together > 1 else "")
                response = (context.input_responses or {}).get(key)
                held["answers"].append(None if response is None else _answer(response))
        if held["round"] < times:
            request = {f"q{held['round']}" + (f".{n}" if together > 1 else ""): _request(kind, mode, n, url, schema)
                       for n in range(together)}
            held["round"] += 1
            return InputRequiredResult(input_requests=request, request_state=json.dumps(held))
        answers = held["answers"]
    else:
        answers, session = [], context.session
        for index in range(times):
            try:
                if kind == "sample":
                    from mcp.types import SamplingMessage

                    response = await session.create_message(
                        [SamplingMessage(role="user", content=TextContent(type="text", text="hi"))], max_tokens=10,
                        related_request_id=context.request_id)
                elif kind == "roots":
                    response = await session.list_roots()
                elif mode == "url":
                    response = await session.elicit_url(f"Open the page {index}", url, str(_uuid.uuid4()),
                                                        related_request_id=context.request_id)
                elif together > 1:
                    many = await asyncio.gather(*(session.elicit_form(f"Who are you? ({n})", schema or FORM,
                                                                      related_request_id=context.request_id)
                                                  for n in range(together)))
                    answers.extend(_answer(r) for r in many)
                    continue
                else:
                    # M37: `schema` may be a list, one schema a request, so one call can ask big then small.
                    one = schema[index] if isinstance(schema, list) else schema
                    response = await session.elicit_form(f"Who are you? ({index})", one or FORM,
                                                         related_request_id=context.request_id)
                answers.append(_answer(response))
            except Exception as exc:  # the client refused: say so, as a tool would
                answers.append({"refused": str(exc)})
    return CallToolResult(content=[TextContent(type="text", text="answered " + json.dumps(answers, sort_keys=True))])


async def list_resources():
    return [Resource(uri=r["uri"], name=r["name"], title=r.get("title"), description=r.get("description"),
                     mime_type=r.get("mimeType"))
            for r in state().get("resources", [])]


async def list_resource_templates():
    return [ResourceTemplate(uri_template=t["uriTemplate"], name=t["name"], title=t.get("title"),
                             description=t.get("description"), mime_type=t.get("mimeType"))
            for t in state().get("templates", [])]


async def read_resource(uri, context=None):
    if str(uri) == "ask://form":  # M36: a resource that asks for input as it is read
        answered = await asked("ask", {}, context)
        if not isinstance(answered, CallToolResult):
            return answered  # the 2026-07-28 round: an InputRequiredResult
        return [ReadResourceContents(content=answered.content[0].text, mime_type="text/plain")]
    for r in state().get("resources", []):
        if r["uri"] == str(uri):
            content = base64.b64decode(r["blob"]) if "blob" in r else r["text"]
            return [ReadResourceContents(content=content, mime_type=r.get("mimeType"))]
    from mcp.server.mcpserver.exceptions import ResourceNotFoundError

    raise ResourceNotFoundError(f"no resource {uri}")


async def list_prompts():
    return [Prompt(name=p["name"], title=p.get("title"), description=p.get("description"),
                   arguments=[PromptArgument(**a) for a in p.get("arguments", [])] or None)
            for p in state().get("prompts", [])]


async def get_prompt(name, arguments=None, context=None):
    for p in state().get("prompts", []):
        if p["name"] == name:
            filled = {k: v for k, v in (arguments or {}).items()}
            return GetPromptResult(description=p.get("description"), messages=[
                PromptMessage(role=m["role"], content=TextContent(type="text", text=m["text"].format(**filled)))
                for m in p["messages"]])
    raise ValueError(f"no prompt {name}")


app.list_tools, app.call_tool = list_tools, call_tool
app.list_resources, app.list_resource_templates, app.read_resource = list_resources, list_resource_templates, read_resource
app.list_prompts, app.get_prompt = list_prompts, get_prompt
inner = app.streamable_http_app(host="127.0.0.1")


async def plain(send, status, headers=()):
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain"), *headers]})
    await send({"type": "http.response.body", "body": b"refused"})


async def served(scope, receive, send):
    if scope["type"] != "http":
        return await inner(scope, receive, send)
    hit(scope["method"], scope["path"])
    now = state()
    if now.get("redirect"):
        return await plain(send, 307, [(b"location", now["redirect"].encode())])
    if now.get("status"):
        return await plain(send, int(now["status"]), [
            (b"www-authenticate", b'Bearer resource_metadata="' + now.get("metadata", "").encode() + b'"')])
    headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
    if headers.get("x-fixture-key") != now.get("key"):
        return await plain(send, 401)
    if scope["method"] != "POST":
        return await inner(dict(scope, path="/mcp", raw_path=b"/mcp"), receive, send)
    # The body is read once, here, and handed on as it arrived.
    chunks, more = [], True
    while more:
        message = await receive()
        chunks.append(message.get("body", b""))
        more = message.get("more_body", False)
    body = b"".join(chunks)
    try:
        request = json.loads(body or b"{}")
    except ValueError:
        request = {}
    method = request.get("method") if isinstance(request, dict) else None
    if LEGACY_ONLY and method == "server/discover":
        payload = json.dumps({"jsonrpc": "2.0", "id": request.get("id"),
                              "error": {"code": -32601, "message": "Method not found"}}).encode()
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        return await send({"type": "http.response.body", "body": payload})
    if method == "subscriptions/listen" and now.get("fail_listen", 0) > 0:
        now["fail_listen"] -= 1  # a passing failure, as a server under load may give
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(now, f)
        return await plain(send, 500)
    sent = {"done": False}

    async def replay():
        if not sent["done"]:
            sent["done"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    if not now.get("gzip"):
        return await inner(dict(scope, path="/mcp", raw_path=b"/mcp"), replay, send)
    # "gzip": every answer compressed, whatever the client asked for.
    start, parts = {}, []

    async def compressed(message):
        if message["type"] == "http.response.start":
            start.update(message)
        elif message["type"] == "http.response.body":
            parts.append(message.get("body", b""))
            if not message.get("more_body"):
                data = gzip.compress(b"".join(parts))
                headers = [(k, v) for k, v in start.get("headers", []) if k.lower() not in (b"content-length",)]
                await send({**start, "headers": headers + [(b"content-encoding", b"gzip"),
                                                         (b"content-length", str(len(data)).encode())]})
                await send({"type": "http.response.body", "body": data})

    return await inner(dict(scope, path="/mcp", raw_path=b"/mcp"), replay, compressed)


listener = socket.socket()
listener.bind(("127.0.0.1", 0))
port = listener.getsockname()[1]
config = uvicorn.Config(served, log_level="critical", lifespan="on")


class Announced(uvicorn.Server):
    async def startup(self, sockets=None):
        await super().startup(sockets)
        with open(PORT_FILE + ".tmp", "w", encoding="utf-8") as f:
            f.write(str(port))
        os.replace(PORT_FILE + ".tmp", PORT_FILE)


Announced(config).run(sockets=[listener])
