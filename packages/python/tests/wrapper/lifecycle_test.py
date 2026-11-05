"""Regression tests for the deep review's TypeScript findings on options and handlers that Python
shares (31 October): `extras=False` turning every extra on (ts-mcp-17), and a tools/call handler set
after instrument() that calls the wrapped one recorded twice (ts-mcp-13: the mcp 1.x lines, whose
hook is the handlers dict; on 2.x the hook is middleware outside any handler). Ported from
packages/lsquare/test/mcp-lifecycle.test.ts."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import pytest

from helpers import Recorder
from lines import client, connect, high, needs_line, only, schema, tool
from lsquare.mcp import instrument

pytestmark = [needs_line, pytest.mark.anyio]
OFF = {"sessions": False, "intent": False, "missing_tools": False}
ID_LINE = re.compile(r"^Task ID: (ses_[A-Za-z0-9_-]{22})\. Send it as session_id")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def search(query: str) -> str:
    return f"found {query}"


def properties(listed: Any) -> list[str]:
    return sorted(schema(listed).get("properties") or {})


async def test_extras_false_turns_every_extra_off() -> None:
    recorder = Recorder()
    lsquare, events = client(logger=recorder)
    app = high()
    tool(app, search)
    instrument(app, lsquare, extras=False)
    async with connect(app) as connection:
        listed = {item.name: item for item in await connection.tools()}
        reply = await connection.call("search", {"query": "a"})
    await lsquare.aflush(timeout_ms=5_000)
    assert sorted(listed) == ["search"]
    assert properties(listed["search"]) == ["query"]
    assert reply.texts == ["found a"]
    [event] = events.calls()
    assert "sessionId" not in event
    assert [line for _, line in recorder.lines if "extras" in line] == []


def _function_with_extras_off() -> Any:
    def extras() -> None:
        pass

    for key in OFF:
        setattr(extras, key, False)
    return extras


# A list or a function is no mapping either, whatever it holds: none of it is read.
@pytest.mark.parametrize("value", [lambda: 0, lambda: [], _function_with_extras_off], ids=["0", "a list", "a function with the extras off on it"])
async def test_extras_that_is_neither_false_nor_a_mapping_keeps_the_defaults_with_one_warning(value: Callable[[], Any]) -> None:
    recorder = Recorder()
    lsquare, _ = client(logger=recorder)
    app = high()
    tool(app, search)
    instrument(app, lsquare, extras=value())
    async with connect(app) as connection:
        listed = {item.name: item for item in await connection.tools()}
        reply = await connection.call("search", {"query": "a"})
    assert "get_more_tools" in listed
    assert properties(listed["search"]) == ["intent", "query", "session_id"]
    assert ID_LINE.match(reply.texts[-1])
    assert recorder.wait_for(lambda: sum("extras is neither False nor a mapping" in line for _, line in recorder.lines) == 1)


def _delegate(app: Any) -> list[int]:
    """Middleware, as an app or a framework adds it after instrument(): it takes the current
    tools/call entry of the low-level server's handlers and sets one that calls it."""
    import mcp.types as types

    low = getattr(app, "_mcp_server", app)
    inner = low.request_handlers[types.CallToolRequest]
    through: list[int] = []

    async def middleware(request: Any) -> Any:
        through.append(1)
        return await inner(request)

    low.request_handlers[types.CallToolRequest] = middleware
    return through


@only("mcp1", "fastmcp3")
async def test_a_handler_that_calls_the_one_it_replaced_records_each_call_once_with_one_identify() -> None:
    lsquare, events = client()
    identifies: list[int] = []

    def identify(_: Any) -> Any:
        identifies.append(1)
        return {"userId": "u-1"}

    app = high()
    tool(app, search)
    instrument(app, lsquare, extras=OFF, identify=identify)
    through = _delegate(app)
    async with connect(app) as connection:
        reply = await connection.call("search", {"query": "a"})
    await lsquare.aflush(timeout_ms=5_000)
    assert reply.texts == ["found a"]
    assert through == [1]
    calls = events.calls()
    assert len(calls) == 1, calls
    assert calls[0]["toolName"] == "search" and calls[0]["userId"] == "u-1"
    assert identifies == [1]


# The control: the nested request comes inside the outer call's scope, with a context of its own.
@only("mcp1")
async def test_a_tool_that_calls_another_through_a_client_while_it_runs_is_two_calls() -> None:
    from mcp.shared.memory import create_connected_server_and_client_session

    lsquare, events = client()
    app = high()
    sessions: list[Any] = []

    async def outer(query: str) -> str:
        await sessions[0].call_tool("search", {"query": query})
        return f"outer {query}"

    tool(app, search)
    tool(app, outer)
    instrument(app, lsquare, extras=OFF)
    async with create_connected_server_and_client_session(app._mcp_server) as session:
        sessions.append(session)
        reply = await session.call_tool("outer", {"query": "a"})
    await lsquare.aflush(timeout_ms=5_000)
    assert [block.text for block in reply.content] == ["outer a"]
    assert sorted(event["toolName"] for event in events.calls()) == ["outer", "search"]
