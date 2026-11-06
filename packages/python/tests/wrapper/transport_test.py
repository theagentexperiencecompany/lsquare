"""The transports, end to end on each line, through the SDK's own clients (Python spec, sections 3
and 4): stdio through the server's own run method in a subprocess, with the close flush; Streamable
HTTP, stateful and stateless, served by uvicorn. What each call's event says of its session and
protocol version, and whether the reply carries the ID line."""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import importlib
import json
import re
import socket
import sys
import threading
import time
import urllib.parse
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from lines import FAST, LINE, MAJOR, Connection, client, high, needs_line, tool
from lsquare import LSquare
from lsquare.mcp import instrument

pytestmark = [needs_line, pytest.mark.anyio]
SERVER = Path(__file__).resolve().parent / "servers" / "stdio_app.py"
ID_LINE = re.compile(r"^Task ID: (ses_[A-Za-z0-9_-]{22})\. Send it as session_id")
MODERN = "2026-07-28"
#: Sent by hand: a version every line supports, and none of the SDK clients' default.
OLDER = "2025-03-26"
FORGED = "f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0"
modern_era = pytest.mark.skipif(MAJOR != 2, reason="mcp 1.x (and fastmcp 3.x over it) has no 2026-07-28 era to pick")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def search(query: str) -> str:
    return f"found {query}"


def issued(texts: list[str]) -> str | None:
    match = ID_LINE.match(texts[-1]) if texts else None
    return match.group(1) if match else None


def asked() -> str:
    """The version the line's client asks for on the handshake: what the server accepts, and so
    what the events should say."""
    if MAJOR == 1:
        return str(importlib.import_module("mcp.types").LATEST_PROTOCOL_VERSION)
    return str(importlib.import_module("mcp_types.version").LATEST_HANDSHAKE_VERSION)


def by_query(calls: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Events by their call's query, whatever order they were sent in."""
    return {event["input"]["query"]: event for event in calls}


# -- stdio ------------------------------------------------------------------------------------------


def written(path: Path) -> list[dict[str, Any]]:
    """What the server's client wrote down: its events and the marker, in order."""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def tool_calls(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [line for line in lines if line.get("kind") == "tool_call"]


@asynccontextmanager
async def stdio(events: Path, mode: str | None = None, **env: str) -> AsyncIterator[Connection]:
    """A connection to `servers/stdio_app.py` in a subprocess, through the line's own stdio client.
    Leaving it closes the server's stdin and waits for the process to exit (the SDKs give it two
    seconds before terminating it). `mode` picks the 2.x lines' era: "legacy" (the default here)
    or "2026-07-28"."""
    environment = {"LSQUARE_TEST_EVENTS": str(events), **env}
    era = mode or "legacy"
    if FAST:
        from fastmcp import Client as FastClient
        from fastmcp.client.transports import StdioTransport

        # keep_alive=False: fastmcp otherwise keeps the subprocess running after the context.
        transport = StdioTransport(sys.executable, [str(SERVER)], env=environment, keep_alive=False)
        options: dict[str, Any] = {"mode": era} if MAJOR == 2 else {}
        async with FastClient(transport, **options) as fast:
            yield Connection(fast)
        return
    from mcp.client.stdio import StdioServerParameters

    parameters = StdioServerParameters(command=sys.executable, args=[str(SERVER)], env=environment)
    if MAJOR == 1:
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client

        async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            yield Connection(session)
        return
    from mcp.client.client import Client

    async with Client(parameters, mode=era) as official:
        yield Connection(official)


async def test_stdio_groups_a_connections_calls_by_one_protocol_session_and_a_second_connection_gets_another(
    tmp_path: Path,
) -> None:
    sessions: list[str] = []
    for n in (1, 2):
        path = tmp_path / f"events-{n}.jsonl"
        async with stdio(path) as connection:
            first = await connection.call("search", {"query": "a"})
            second = await connection.call("search", {"query": "b"})
        # No ID line: the connection groups them.
        assert first.texts == ["found a"] and not first.is_error
        assert second.texts == ["found b"] and not second.is_error
        calls = by_query(tool_calls(written(path)))
        assert sorted(calls) == ["a", "b"]
        assert {event["sessionSource"] for event in calls.values()} == {"protocol"}
        [session_id] = {event["sessionId"] for event in calls.values()}
        assert {event["protocolVersion"] for event in calls.values()} == {asked()}
        assert all(isinstance(event.get("client"), dict) for event in calls.values())
        sessions.append(session_id)
    assert sessions[0] != sessions[1]


@modern_era
async def test_stdio_on_2026_07_28_has_no_protocol_session_so_each_first_result_is_issued_an_id(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    async with stdio(path, mode=MODERN) as connection:
        first = await connection.call("search", {"query": "a"})
        second = await connection.call("search", {"query": "b"})
    one, two = issued(first.texts), issued(second.texts)
    assert first.texts[0] == "found a" and one is not None, first.texts
    assert second.texts[0] == "found b" and two is not None and two != one, second.texts
    calls = by_query(tool_calls(written(path)))
    assert (calls["a"]["sessionId"], calls["a"]["sessionSource"]) == (one, "issued")
    assert (calls["b"]["sessionId"], calls["b"]["sessionSource"]) == (two, "issued")
    assert {event["protocolVersion"] for event in calls.values()} == {MODERN}


# The server's own sync run() (through fastmcp's run_async() on the fastmcp lines), and its
# run_stdio_async() awaited directly.
@pytest.mark.parametrize("entry", ["run", "run_stdio_async"])
async def test_stdio_close_flush_sends_an_event_still_in_an_async_before_send_before_the_run_returns(
    tmp_path: Path, entry: str
) -> None:
    path = tmp_path / "events.jsonl"
    async with stdio(path, LSQUARE_TEST_ENTRY=entry, LSQUARE_TEST_HOOK_DELAY="0.3") as connection:
        reply = await connection.call("search", {"query": "a"})
    # Closed at once: the hook was still sleeping when stdin closed, and the process has exited.
    assert reply.texts == ["found a"]
    lines = written(path)
    kinds = [line.get("kind") or line.get("marker") for line in lines]
    # Sent while the run's loop still ran, then the run returned by itself: never terminated.
    assert kinds == ["tool_call", "run returned"], lines


async def test_stdio_without_the_wrapped_run_the_same_event_is_lost_so_the_close_flush_is_what_sends_it(
    tmp_path: Path,
) -> None:
    """The control: the class's own run_stdio_async, which instrument() never wraps. The hook is
    cancelled with the loop, and the exit flush can't bring its event back."""
    path = tmp_path / "events.jsonl"
    async with stdio(path, LSQUARE_TEST_ENTRY="unwrapped", LSQUARE_TEST_HOOK_DELAY="1") as connection:
        reply = await connection.call("search", {"query": "a"})
    # (Unmarked, the run isn't known to be stdio on the mcp lines: the reply gets an ID line there.)
    assert reply.texts[0] == "found a"
    assert written(path) == [{"marker": "run returned"}]


# -- Streamable HTTP --------------------------------------------------------------------------------


def http_app(app: Any, stateless: bool) -> Any:
    """The line's own Streamable HTTP ASGI app, at /mcp."""
    if LINE == "mcp1":
        app.settings.stateless_http = stateless
        return app.streamable_http_app()
    if LINE == "mcp2":
        return app.streamable_http_app(stateless_http=stateless)
    return app.http_app(stateless_http=stateless)


@asynccontextmanager
async def serving(asgi: Any) -> AsyncIterator[str]:
    """Serves `asgi` with uvicorn on a free port, on a thread with its own loop; yields the URL."""
    uvicorn: Any = importlib.import_module("uvicorn")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(asgi, log_level="warning", lifespan="on", timeout_graceful_shutdown=2))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("uvicorn didn't start")
            await asyncio.sleep(0.02)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        await asyncio.to_thread(thread.join, 10)
        sock.close()


@asynccontextmanager
async def over_http(url: str, mode: str | None = None) -> AsyncIterator[tuple[Connection, Callable[[], str | None]]]:
    """A connection through the line's own Streamable HTTP client, and what reads the
    `mcp-session-id` the server issued it, if any. `mode` as for `stdio()`."""
    era = mode or "legacy"
    if FAST:
        from fastmcp import Client as FastClient
        from fastmcp.client.transports import StreamableHttpTransport

        transport = StreamableHttpTransport(url)
        options: dict[str, Any] = {"mode": era} if MAJOR == 2 else {}
        async with FastClient(transport, **options) as fast:
            yield Connection(fast), transport.get_session_id
        return
    from mcp.client.streamable_http import streamable_http_client

    if MAJOR == 1:
        from mcp import ClientSession

        async with streamable_http_client(url) as (read, write, session_id), ClientSession(read, write) as session:
            await session.initialize()
            yield Connection(session), session_id
        return
    from mcp.client.client import Client
    from mcp.shared._httpx_utils import create_mcp_http_client

    # mcp 2.x's transport keeps the issued ID to itself: read it off the responses.
    seen: list[str] = []

    async def record(response: Any) -> None:
        value = response.headers.get("mcp-session-id")
        if value:
            seen.append(value)

    async with create_mcp_http_client() as http_client:
        http_client.event_hooks = {"request": [], "response": [record]}
        async with Client(streamable_http_client(url, http_client=http_client), mode=era) as official:
            yield Connection(official), lambda: seen[-1] if seen else None


def counting_flushes(lsquare: LSquare, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every flush or shutdown of the client from here on, by name: the wrapper's go through
    these methods too."""
    seen: list[str] = []

    def counted(name: str) -> Callable[..., Any]:
        original = getattr(lsquare, name)
        if name.startswith("a"):

            async def waiting(*args: Any, **kwargs: Any) -> Any:
                seen.append(name)
                return await original(*args, **kwargs)

            return waiting

        def blocking(*args: Any, **kwargs: Any) -> Any:
            seen.append(name)
            return original(*args, **kwargs)

        return blocking

    for name in ("flush", "aflush", "shutdown", "ashutdown"):
        monkeypatch.setattr(lsquare, name, counted(name))
    return seen


async def settled(flushes: list[str]) -> list[str]:
    """The flushes so far, once a session the client just left has had time to end on the server."""
    await asyncio.sleep(0.5)
    return list(flushes)


async def test_stateful_http_groups_calls_by_the_session_id_the_server_issued_and_flushes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lsquare, events = client()
    aflush = lsquare.aflush
    flushes = counting_flushes(lsquare, monkeypatch)
    app = high("http")
    tool(app, search)
    instrument(app, lsquare)
    async with serving(http_app(app, stateless=False)) as url:
        # Two sessions open at once, their calls interleaved: each request is read for its own.
        async with over_http(url) as (one, one_id), over_http(url) as (two, two_id):
            replies = [
                await one.call("search", {"query": "a1"}),
                await two.call("search", {"query": "b1"}),
                await one.call("search", {"query": "a2"}),
            ]
            issued_ids = {"a": one_id(), "b": two_id()}
        # The requests ended, and with each client's DELETE its session too.
        assert await settled(flushes) == []
    await aflush(timeout_ms=5_000)
    assert issued_ids["a"] and issued_ids["b"] and issued_ids["a"] != issued_ids["b"], issued_ids
    # No ID line: the session groups them.
    assert [reply.texts for reply in replies] == [["found a1"], ["found b1"], ["found a2"]]
    calls = by_query(events.calls())
    assert sorted(calls) == ["a1", "a2", "b1"]
    for query, event in calls.items():
        assert (event["sessionId"], event["sessionSource"]) == (hashlib.sha256(str(issued_ids[query[0]]).encode()).hexdigest(), "protocol"), query
        assert event["protocolVersion"] == asked()
        # The client the session's initialize named.
        assert isinstance(event.get("client"), dict), event


async def test_stateful_http_sends_no_session_id_with_sessions_turned_off() -> None:
    lsquare, events = client()
    app = high("http")
    tool(app, search)
    instrument(app, lsquare, extras={"sessions": False})
    async with serving(http_app(app, stateless=False)) as url:
        async with over_http(url) as (one, one_id):
            await one.call("search", {"query": "a1"})
            assert one_id()
    await lsquare.aflush(timeout_ms=5_000)
    calls = events.calls()
    assert len(calls) == 1
    assert "sessionId" not in calls[0] and "sessionSource" not in calls[0], calls[0]


@modern_era
async def test_http_on_2026_07_28_has_no_protocol_session_even_on_a_stateful_server() -> None:
    lsquare, events = client()
    app = high("http")
    tool(app, search)
    instrument(app, lsquare)
    async with serving(http_app(app, stateless=False)) as url:
        async with over_http(url, mode=MODERN) as (connection, session_id):
            reply = await connection.call("search", {"query": "a"})
            issued_header = session_id()
    await lsquare.aflush(timeout_ms=5_000)
    task = issued(reply.texts)
    assert reply.texts[0] == "found a" and task is not None, reply.texts
    assert issued_header is None
    [event] = events.calls()
    assert (event["sessionId"], event["sessionSource"], event["protocolVersion"]) == (task, "issued", MODERN)
    # Named in the request's own _meta.
    assert isinstance(event.get("client"), dict), event


async def test_stateless_http_has_no_protocol_session_so_the_first_reply_carries_an_issued_id_and_flushes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lsquare, events = client()
    aflush = lsquare.aflush
    flushes = counting_flushes(lsquare, monkeypatch)
    app = high("http")
    tool(app, search)
    instrument(app, lsquare)
    async with serving(http_app(app, stateless=True)) as url:
        async with over_http(url) as (connection, session_id):
            first = await connection.call("search", {"query": "a"})
            task = issued(first.texts)
            assert task is not None, first.texts
            second = await connection.call("search", {"query": "b", "session_id": task})
            issued_header = session_id()
        # Every request ended, each with its own server session.
        assert await settled(flushes) == []
    await aflush(timeout_ms=5_000)
    assert issued_header is None
    assert first.texts[0] == "found a" and second.texts == ["found b"]
    calls = by_query(events.calls())
    assert (calls["a"]["sessionId"], calls["a"]["sessionSource"]) == (task, "issued")
    assert (calls["b"]["sessionId"], calls["b"]["sessionSource"]) == (task, "sent")
    assert {event["protocolVersion"] for event in calls.values()} == {asked()}
    # Its initialize ran in a session of its own: no call knows the client.
    assert all("client" not in event for event in calls.values())


RAW = {"content-type": "application/json", "accept": "application/json, text/event-stream"}


def post(url: str, body: dict[str, Any], headers: dict[str, str]) -> tuple[int, str, str | None]:
    """One raw POST, blocking: run it off the loop. Its status, body and `mcp-session-id`."""
    parts = urllib.parse.urlsplit(url)
    connection = http.client.HTTPConnection(parts.hostname or "127.0.0.1", parts.port, timeout=10)
    try:
        connection.request("POST", parts.path, body=json.dumps(body).encode("utf-8"), headers={**RAW, **headers})
        response = connection.getresponse()
        return response.status, response.read().decode("utf-8"), response.getheader("mcp-session-id")
    finally:
        connection.close()


def calling(query: str, id_: int = 1) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "method": "tools/call", "params": {"name": "search", "arguments": {"query": query}}}


def answer(body: str) -> dict[str, Any]:
    """The JSON-RPC response in a JSON or an SSE reply."""
    if body.lstrip().startswith("{"):
        out: dict[str, Any] = json.loads(body)
        return out
    for line in body.splitlines():
        if line.startswith("data:"):
            message: dict[str, Any] = json.loads(line[len("data:") :])
            if "id" in message:
                return message
    raise AssertionError(f"no JSON-RPC response in {body!r}")


async def test_stateless_http_never_takes_an_mcp_session_id_header_it_never_issued() -> None:
    lsquare, events = client()
    app = high("http")
    tool(app, search)
    instrument(app, lsquare)
    headers = {"mcp-protocol-version": OLDER, "mcp-session-id": FORGED}
    async with serving(http_app(app, stateless=True)) as url:
        status, body, _ = await asyncio.to_thread(post, url, calling("raw"), headers)
    await lsquare.aflush(timeout_ms=5_000)
    assert status == 200, body
    result = answer(body)["result"]
    texts = [block["text"] for block in result["content"] if block.get("type") == "text"]
    task = issued(texts)
    assert texts[0] == "found raw" and task is not None, texts
    [event] = events.calls()
    assert event["sessionId"] != FORGED
    assert (event["sessionId"], event["sessionSource"]) == (task, "issued")
    # The version the request asked for, as its header says; no initialize, no client.
    assert event["protocolVersion"] == OLDER and "client" not in event


async def test_stateless_http_records_no_version_for_a_request_that_declares_none() -> None:
    lsquare, events = client()
    app = high("http")
    tool(app, search)
    instrument(app, lsquare)
    async with serving(http_app(app, stateless=True)) as url:
        status, body, _ = await asyncio.to_thread(post, url, calling("raw"), {})
    await lsquare.aflush(timeout_ms=5_000)
    assert status == 200, body
    [event] = events.calls()
    # No initialize and no header: the MCP SDK assumes 2025-03-26 to serve it, but no client said so.
    assert "protocolVersion" not in event and "client" not in event


def handshake_then_call(url: str) -> list[tuple[int, str]]:
    """A client that initializes a stateful session by hand, then calls once declaring an older
    version than the one negotiated, and once declaring none."""
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": asked(), "capabilities": {}, "clientInfo": {"name": "raw", "version": "1"}},
    }
    status, body, session_id = post(url, initialize, {})
    assert status == 200 and session_id, body
    replies = [(status, body)]
    initialized = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    status, body, _ = post(url, initialized, {"mcp-session-id": session_id, "mcp-protocol-version": asked()})
    replies.append((status, body))
    for query, version in (("declared", OLDER), ("undeclared", None)):
        status, body, _ = post(url, calling(query, 2), {"mcp-session-id": session_id, **({"mcp-protocol-version": version} if version else {})})
        replies.append((status, body))
    return replies


async def test_stateful_http_records_the_version_a_request_declares_in_its_header_before_the_negotiated_one() -> None:
    lsquare, events = client()
    app = high("http")
    tool(app, search)
    instrument(app, lsquare)
    async with serving(http_app(app, stateless=False)) as url:
        replies = await asyncio.to_thread(handshake_then_call, url)
    await lsquare.aflush(timeout_ms=5_000)
    # The SDK accepts both calls: a header naming any version it supports.
    assert [status for status, _ in replies] == [200, 202, 200, 200], replies
    calls = by_query(events.calls())
    assert calls["undeclared"]["protocolVersion"] == asked()
    assert calls["declared"]["sessionSource"] == "protocol"
    assert calls["declared"]["protocolVersion"] == OLDER
