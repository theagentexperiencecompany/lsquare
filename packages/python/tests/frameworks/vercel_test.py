"""Vercel's Python runtime, on each line (Python spec, section 3, "Serverless"). The runtime serves
the ASGI app as it is (with uvicorn, its lifespan included), so this covers the app, the VERCEL
warning, and `wait_until`'s hand-off, ported from tests/frameworks/node/mcp-handler.test.ts: each
call's sending handed to `wait_until` as an awaitable that ends once the event is sent, the reply
not waiting for it. `hosts.vercel` stands in for the runtime's per-request wrapper and
`hosts.wait_until` for `vercel.functions.wait_until`. Vercel's own execution lifetime (how long
the instance runs work after a reply) isn't covered."""

from __future__ import annotations

import asyncio
import re
import sys
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("uvicorn")

from helpers import Recorder
from lines import MAJOR, needs_line, tool

from frameworks.hosts import (
    ENDPOINT,
    Lifespan,
    calling,
    client,
    fastapi_app,
    in_process,
    issued,
    orders,
    served,
    texts,
    until_async,
    vercel,
    wait_until,
)
from lsquare.mcp import instrument

pytestmark = [needs_line, pytest.mark.anyio]
VERCEL = "This looks like Vercel"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def on_vercel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERCEL", "1")


async def test_hands_each_calls_sending_to_wait_until_the_reply_first_the_awaitable_ending_once_the_ingestion_answers() -> None:
    lsquare, ingestion = client()
    server = orders(stateless=True)

    def search(query: str) -> str:
        lsquare.capture({"question": f"Find {query}"})
        return f"3 orders for {query}"

    tool(server, search)
    instrument(server, lsquare, wait_until=wait_until)
    app, requests = vercel(fastapi_app(server, lsquare, stateless=True), ingestion)
    async with served(app, ENDPOINT) as http:
        ingestion.hold()
        reply = await http.post(calling({"query": "late"}))
        # The reply came while the event's acknowledgement is held.
        result = reply.result()
        assert texts(result)[0] == "3 orders for late"
        [collector] = requests
        assert len(collector.handed) == 1
        assert await until_async(lambda: len(ingestion.batches) == 1)
        assert ingestion.answered() == [False]
        # The runtime awaits what it was handed: the invocation hasn't ended.
        await asyncio.sleep(0.2)
        assert not collector.drained.is_set()
        ingestion.release()
        assert await asyncio.to_thread(collector.drained.wait, 5)
        assert ingestion.answered() == [True]
    # Replied, then acknowledged, and only then the invocation ended.
    timeline = ingestion.seen()
    assert timeline[:3] == ["replied", "acknowledged", "invocation ended"], timeline
    [event] = ingestion.events()
    assert event["toolName"] == "search" and event["server"]["name"] == "orders"
    assert re.fullmatch(r"python/\d+\.\d+", event["sdk"]["runtime"]) and event["sdk"]["runtime"] == f"python/{sys.version_info[0]}.{sys.version_info[1]}"
    assert event["agent"]["question"] == "Find late"
    assert (event["sessionSource"], event["sessionId"]) == ("issued", issued(texts(result)))


async def test_still_hands_the_sending_to_wait_until_when_the_tool_called_flush_the_reply_waiting_at_most_flush_timeout_ms() -> None:
    lsquare, ingestion = client(flush_timeout_ms=50)
    server = orders(stateless=True)

    async def search(query: str) -> str:
        lsquare.capture({"question": f"Find {query}"})
        await lsquare.aflush()
        return f"3 orders for {query}"

    tool(server, search)
    instrument(server, lsquare, wait_until=wait_until)
    app, requests = vercel(fastapi_app(server, lsquare, stateless=True), ingestion)
    async with served(app, ENDPOINT) as http:
        ingestion.hold()
        reply = await http.post(calling({"query": "late"}))
        # The reply gave up waiting at flush_timeout_ms, the event's acknowledgement held.
        assert texts(reply.result())[0] == "3 orders for late"
        [collector] = requests
        assert len(collector.handed) == 1
        assert await until_async(lambda: len(ingestion.batches) == 1)
        assert ingestion.answered() == [False]
        await asyncio.sleep(0.2)
        assert not collector.drained.is_set()
        ingestion.release()
        assert await asyncio.to_thread(collector.drained.wait, 5)
        assert ingestion.answered() == [True]
    timeline = ingestion.seen()
    assert timeline[:3] == ["replied", "acknowledged", "invocation ended"], timeline
    [event] = ingestion.events()
    assert event["agent"]["question"] == "Find late"


async def test_on_a_stateful_server_only_mcp_2x_hands_the_sending_to_the_request_that_made_the_call() -> None:
    """Why REFERENCE.md says to serve statelessly on Vercel: mcp 1.x (and fastmcp 3.x over it) runs
    a stateful session's handlers in the context of the request that opened the session, so
    `wait_until` finds that request's collector, long closed, and the sending isn't kept alive."""
    lsquare, ingestion = client()
    server = orders(stateless=False)
    tool(server, lambda query: f"found {query}", name="search")
    instrument(server, lsquare, wait_until=wait_until)
    app, requests = vercel(fastapi_app(server, lsquare, stateless=False), ingestion)
    async with served(app, ENDPOINT) as http:
        await http.handshake()
        reply = await http.post(calling({"query": "a"}))
        assert texts(reply.result()) == ["found a"]
        assert await until_async(lambda: len(ingestion.events()) == 1)
        await http.close()
    # initialize, notifications/initialized, tools/call, then the DELETE.
    handed = [len(collector.handed) for collector in requests[:3]]
    assert handed == ([0, 0, 1] if MAJOR == 2 else [0, 0, 0]), handed


@pytest.mark.parametrize("handed", [False, True], ids=["without", "with wait_until"])
async def test_warns_once_on_vercel_without_wait_until_and_not_with_it(handed: bool) -> None:
    recorder = Recorder()
    lsquare, ingestion = client(logger=recorder)
    server = orders(stateless=True)
    tool(server, lambda query: f"found {query}", name="search")
    instrument(server, lsquare, wait_until=wait_until if handed else None)
    app, _ = vercel(fastapi_app(server, lsquare, stateless=True), ingestion)
    async with Lifespan(app, ingestion), in_process(app, ENDPOINT) as http:
        for query in ("a", "b"):
            assert texts((await http.post(calling({"query": query}))).result())[0] == f"found {query}"
    lsquare._lsquare_internal().warn_once("test-marker", "[test] marker")
    assert recorder.wait_for(lambda: any(line == "[test] marker" for _, line in recorder.lines))
    lines: list[Any] = [line for _, line in recorder.lines if VERCEL in line]
    assert len(lines) == (0 if handed else 1), recorder.lines
