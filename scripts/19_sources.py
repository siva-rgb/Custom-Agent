"""19 - Source versions and the scoped resource cache: a page is fetched once, then served.

What it shows
  * every fetch inside a run records an immutable source version: the URL asked for and
    the one that answered, when, and the content as an artifact that reads back against
    its hash (FR-89)
  * a second fetch of the same page in the same run, and a fetch in a second run of the
    same project, are served from the cache: no request, the same version (FR-90, FR-91)
  * once the version is older than the cache's freshness, the next fetch goes out again
    and records a new version naming the old one as its prior
  * a second tenant fetching the same page at PROJECT scope gets no hit: evidence never
    crosses tenancy (NFR-25)
  * each fetch says what happened in a SourceFetched or SourceServed event

The cache's clock is the fetch tool's test seam, moved forward by hand so the expiry
shows without waiting an hour. In the live run the model is the gateway's, the page is
https://example.com/ and everything is stored in PostgreSQL.

Run it
  python scripts/19_sources.py            # live: gateway + PostgreSQL (DATABASE_URL in .env)
  python scripts/19_sources.py --offline  # in memory, a local page and a scripted model
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

from agentsdk import AgentSpec, Message, Role, RunConfig, RunStatus, Runner
from agentsdk.builtin_tools import fetch_tool
from agentsdk.events import EventType
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.postgres import RunScope
from agentsdk.primitives import ToolCall

TENANT, PROJECT, OTHER_TENANT = "example-tenant", "examples", "example-other-tenant"
FRESHNESS = 600


class Clock:
    def __init__(self):
        self.now = datetime.now(timezone.utc)

    def __call__(self):
        return self.now


def reader(url: str) -> AgentSpec:
    return AgentSpec(
        id="reader",
        instructions=(f"Check that a page is stable. Call fetch_url on {url}. After you have its result, "
                      f"call fetch_url on {url} a second time, in a new turn. Only after both results, "
                      "answer in one sentence with the page's title and whether the two fetches matched. "
                      "Never answer after a single fetch."),
        tool_profile=("fetch_url",),
    )


class ScriptedReader:
    """Offline: fetches the page named in its instructions twice, then answers."""

    async def send(self, request):
        url = request.instructions.split("fetch_url on ", 1)[1].split(". ", 1)[0]
        done = sum(len(m.tool_results) for m in request.messages)
        if done < 2:
            call = ToolCall(id=f"fetch-{done + 1}", name="fetch_url", arguments={"url": url})
            return ModelResponse(message=Message(role=Role.ASSISTANT, tool_calls=(call,)),
                                 stop_reason=StopReason.TOOL_CALLS, usage=Usage(20, 5, 25))
        return ModelResponse(message=Message(role=Role.ASSISTANT, content="The page is titled Example Domain."),
                             stop_reason=StopReason.END_TURN, usage=Usage(20, 5, 25))


async def demonstrate(runner, clock, model, url, requests):
    """`requests()` counts the requests the page has had, where that can be seen."""
    store = runner._evidence_store()
    checks, versions = [], {}

    async def run(label, tenant):
        result = await runner.run(reader(url), "Fetch the page.", RunConfig(
            tenant_id=tenant, project_id=PROJECT, model_override=model, max_turns=6))
        sessions = runner._sessions_for(RunScope(run_id=result.run_id, tenant_id=tenant, project_id=PROJECT))
        history = await asyncio.to_thread(sessions.history, result.run_id)
        sources = [r.provenance.source_uri_or_hash for m in history for r in m.tool_results]
        events = [(e.event_type.value, e.payload) for e in result.events
                  if e.event_type in (EventType.SOURCE_FETCHED, EventType.SOURCE_SERVED)]
        print(f"\n{label} (run {result.run_id}, tenant {tenant}): {result.status.value}")
        for kind, payload in events:
            age = f", {payload['age_seconds']:.0f} s old" if "age_seconds" in payload else ""
            copied = ", a copy" if payload.get("copied") else ""
            print(f"  {kind}: {payload['source_uri']} ({payload['cache_scope']}{age}{copied})")
        for uri in dict.fromkeys(sources):
            if uri.startswith("urn:agentsdk:source:"):
                v = await asyncio.to_thread(store.get, tenant, PROJECT, uri.rsplit(":", 1)[1])
                versions[uri] = v
                print(f"  version {v.source_version_id}: {v.canonical_uri} -> {v.final_uri}, "
                      f"retrieved {v.retrieval_time:%H:%M:%S}, hash {v.content_hash[:12]}..., prior {v.prior_version}")
        print(f"  answer: {result.output!r}; requests to the page so far: {requests()}")
        return result, sources, events

    first, sources, events = await run("Run 1: the page fetched twice", TENANT)
    kinds = [kind for kind, _ in events]
    checks += [
        ("run 1 completed", first.status is RunStatus.COMPLETED),
        ("run 1 fetched once and was then served", kinds == ["SourceFetched", "SourceServed"]),
        ("both fetches name one source version", len(sources) == 2 and len(set(sources)) == 1),
    ]
    original = sources[0] if sources else None
    v = versions.get(original)
    if v is not None:
        content = await runner._artifacts_for(TENANT, PROJECT).get(v.artifact_id)  # verified against its hash
        print(f"\nthe version's content reads back: {len(content)} bytes, starting {content[:40]!r}")

    second, sources2, events2 = await run("Run 2: the same project, a new run", TENANT)
    checks += [("run 2 was served the same version, with no request",
                [k for k, _ in events2] == ["SourceServed", "SourceServed"] and set(sources2) == {original})]

    clock.now += timedelta(seconds=FRESHNESS)
    third, sources3, events3 = await run(f"Run 3: {FRESHNESS} s later, past freshness", TENANT)
    refreshed = versions.get(sources3[0]) if sources3 else None
    checks += [("run 3 fetched again and recorded a new version naming the old one",
                [k for k, _ in events3][:1] == ["SourceFetched"] and refreshed is not None
                and refreshed.prior_version == (v.source_version_id if v else None))]

    other, sources4, events4 = await run("Run 4: another tenant, the same page", OTHER_TENANT)
    checks += [("another tenant got no hit and a version of its own",
                [k for k, _ in events4][:1] == ["SourceFetched"] and original not in sources4)]
    return checks, [r.run_id for r in (first, second, third, other)]


PAGE = b"<html><head><title>Example Domain</title></head><body>An example page.</body></html>"


async def offline():
    hits = []

    async def serve(reader_, writer):
        await reader_.readuntil(b"\r\n\r\n")
        hits.append(1)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                     + f"Content-Length: {len(PAGE)}\r\nConnection: close\r\n\r\n".encode() + PAGE)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async def resolve(host, port_):
        return ["93.184.215.14"]  # a public address, which the dial below maps to the local page

    clock = Clock()
    fetch = fetch_tool(["example.com"], freshness_seconds=FRESHNESS, _clock=clock,
                       _resolve=resolve, _connect=lambda ip, port_: ("127.0.0.1", port))
    runner = Runner({"reader": ScriptedReader()}, tools=[fetch])
    try:
        checks, _ = await demonstrate(runner, clock, "reader:scripted", "http://example.com/", lambda: len(hits))
    finally:
        server.close()
        await server.wait_closed()
    checks.append(("the page was requested three times: runs 1 and 3, and the other tenant's run 4",
                   len(hits) == 3))
    return checks


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
            clock = Clock()
            fetch = fetch_tool(["example.com"], freshness_seconds=FRESHNESS, _clock=clock)
            runner = Runner({"reader": client}, tools=[fetch], persistence=persistence)
            checks, run_ids = await demonstrate(runner, clock, f"reader:{settings.default_model}", "https://example.com/",
                                                lambda: "not counted live")
            print(f"\nrun ids (to remove the demo's rows): {' '.join(run_ids)}")
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
    parser.add_argument("--offline", action="store_true", help="run in memory with a local page and a scripted model")
    checks = asyncio.run(offline()) if parser.parse_args().offline else live()
    for label, passed in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {label}")
    failed = [label for label, passed in checks if not passed]
    if failed:
        raise SystemExit(f"failed: {failed}")
