"""The extras on a high-level server, end to end on each line: the listing, the inputs taken out
of calls, the ID line and grouping, and get_more_tools."""

from __future__ import annotations

import asyncio
import copy
import gc
import re
from typing import Any

import pytest

from helpers import Recorder
from lines import FAST, MAJOR, client, connect, high, needs_line, schema, tool
from lsquare.mcp import instrument

pytestmark = [needs_line, pytest.mark.anyio]
ID_LINE = re.compile(r"^Task ID: (ses_[A-Za-z0-9_-]{22})\. Send it as session_id")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def search(query: str) -> str:
    return f"found {query}"


def own_intent(query: str, intent: str = "none") -> str:
    return f"{query}/{intent}"


def boom(x: int) -> str:
    raise ValueError("it broke")


def properties(listed: Any) -> list[str]:
    return sorted(schema(listed).get("properties") or {})


def issued(texts: list[str]) -> str | None:
    match = ID_LINE.match(texts[-1]) if texts else None
    return match.group(1) if match else None


async def test_lists_both_inputs_on_every_tool_and_adds_get_more_tools_on_a_copy() -> None:
    lsquare, _ = client()
    app = high()
    tool(app, search)
    instrument(app, lsquare)
    async with connect(app) as connection:
        listed = {item.name: item for item in await connection.tools()}
        again = {item.name: item for item in await connection.tools()}
    assert properties(listed["search"]) == ["intent", "query", "session_id"]
    assert properties(again["search"]) == ["intent", "query", "session_id"]
    assert properties(listed["get_more_tools"]) == ["need", "session_id"]
    assert schema(listed["get_more_tools"])["required"] == ["need"]


async def test_takes_them_out_before_the_sdk_checks_the_arguments_and_groups_later_calls_by_the_id() -> None:
    lsquare, events = client()
    app = high()
    seen: list[dict[str, Any]] = []

    def strict(query: str) -> str:
        seen.append({"query": query})
        return f"found {query}"

    tool(app, strict)
    instrument(app, lsquare)
    async with connect(app) as connection:
        first = await connection.call("strict", {"query": "a", "intent": "find a thing"})
        session_id = issued(first.texts)
        assert session_id is not None, first.texts
        second = await connection.call("strict", {"query": "b", "session_id": session_id, "intent": "again"})
    await lsquare.aflush(timeout_ms=5_000)
    assert first.texts[0] == "found a" and not first.is_error
    assert second.texts == ["found b"] and not second.is_error
    assert seen == [{"query": "a"}, {"query": "b"}]
    one, two = events.calls()
    assert one["input"] == {"query": "a"} and one["intent"] == "find a thing"
    assert one["sessionId"] == session_id and one["sessionSource"] == "issued"
    assert two["sessionId"] == session_id and two["sessionSource"] == "sent" and two["intent"] == "again"


async def test_removes_a_malformed_session_id_and_an_intent_that_isnt_a_non_empty_string_and_ignores_them() -> None:
    lsquare, events = client()
    app = high()
    tool(app, search)
    instrument(app, lsquare)
    async with connect(app) as connection:
        reply = await connection.call("search", {"query": "a", "session_id": "not-an-id", "intent": ""})
    await lsquare.aflush(timeout_ms=5_000)
    assert reply.texts[0] == "found a" and not reply.is_error
    [event] = events.calls()
    assert event["input"] == {"query": "a"}
    assert "intent" not in event and event["sessionSource"] == "issued"


async def test_records_intent_whether_or_not_inputs_are() -> None:
    lsquare, events = client()
    app = high()
    tool(app, search)
    instrument(app, lsquare, inputs=False)
    async with connect(app) as connection:
        await connection.call("search", {"query": "a", "intent": "to see"})
    await lsquare.aflush(timeout_ms=5_000)
    [event] = events.calls()
    assert "input" not in event and event["intent"] == "to see"


async def test_gives_no_id_line_to_a_raise_or_a_call_the_sdk_refused() -> None:
    lsquare, events = client()
    app = high()
    tool(app, boom)
    instrument(app, lsquare)
    async with connect(app) as connection:
        raised = await connection.call("boom", {"x": 1})
        refused = await connection.call("boom", {"x": "nan"})
        unknown = await connection.call("nope", {})
    await lsquare.aflush(timeout_ms=5_000)
    for reply in (raised, refused, unknown):
        assert reply.is_error and issued(reply.texts) is None
    assert all("sessionId" not in event for event in events.calls())


async def test_leaves_an_input_a_tool_declares_as_its_own_and_says_so_once() -> None:
    recorder = Recorder()
    lsquare, events = client(logger=recorder)
    app = high()
    tool(app, own_intent)
    instrument(app, lsquare)
    async with connect(app) as connection:
        listed = {item.name: item for item in await connection.tools()}
        reply = await connection.call("own_intent", {"query": "a", "intent": "mine"})
        await connection.call("own_intent", {"query": "b", "intent": "mine too"})
    await lsquare.aflush(timeout_ms=5_000)
    assert properties(listed["own_intent"]) == ["intent", "query", "session_id"]
    assert reply.texts[0] == "a/mine"
    # The tool's own input: passed on, recorded as an input, never as the extra.
    assert all(event["input"]["intent"].startswith("mine") and "intent" not in event for event in events.calls())
    assert recorder.wait_for(lambda: sum("has its own intent input" in line for _, line in recorder.lines) == 1)


async def test_turns_each_extra_off_by_itself() -> None:
    for off, absent in (("sessions", "session_id"), ("intent", "intent"), ("missing_tools", None)):
        lsquare, _ = client()
        app = high()
        tool(app, search)
        instrument(app, lsquare, extras={off: False})
        async with connect(app) as connection:
            listed = {item.name: item for item in await connection.tools()}
            reply = await connection.call("search", {"query": "a"})
        if absent is not None:
            assert absent not in properties(listed["search"]), off
            assert "get_more_tools" in listed, off
        else:
            assert "get_more_tools" not in listed
        assert (issued(reply.texts) is None) == (off == "sessions"), off


async def test_answers_get_more_tools_itself_and_records_the_need_under_the_inputs_option() -> None:
    lsquare, events = client()
    app = high()
    tool(app, search)
    instrument(app, lsquare)
    async with connect(app) as connection:
        noted = await connection.call("get_more_tools", {"need": "issue refunds"})
        refused = await connection.call("get_more_tools", {})
    await lsquare.aflush(timeout_ms=5_000)
    assert noted.texts[0].startswith("Noted.") and not noted.is_error
    assert refused.is_error and "need is required" in refused.texts[0] and issued(refused.texts) is None
    first, second = events.calls()
    assert first["extra"] == "missing_tools" and first["input"] == {"need": "issue refunds"} and first["status"] == "completed"
    assert second["error"]["category"] == "protocol"


@pytest.mark.parametrize(("answers", "noted"), [([True, False], True), ([False, True], False)])
async def test_asks_the_inputs_function_once_a_call(answers: list[bool], noted: bool) -> None:
    lsquare, events = client()
    app = high()
    tool(app, search)
    left: list[bool] = []
    instrument(app, lsquare, inputs=lambda name: left.pop(0) if left else True)
    async with connect(app) as connection:
        # The client's own listings, which it can make around a call, answer true.
        await connection.tools()
        left[:] = answers
        try:
            reply = await connection.call("get_more_tools", {"need": "issue refunds"})
        except Exception:
            reply = None
    await lsquare.aflush(timeout_ms=5_000)
    assert (reply is not None and bool(reply.texts) and reply.texts[0].startswith("Noted.")) is noted
    [event] = events.calls()
    if noted:
        assert event["extra"] == "missing_tools" and event["input"] == {"need": "issue refunds"}
    else:
        assert "extra" not in event


async def test_keeps_a_warning_of_its_own_for_an_inputs_function_that_doesnt_know_get_more_tools() -> None:
    recorder = Recorder()
    lsquare, _ = client(logger=recorder)
    app = high()
    tool(app, search)
    tool(app, own_intent)
    policy = {"search": True}
    instrument(app, lsquare, inputs=lambda name: policy[name])
    async with connect(app) as connection:
        await connection.tools()
        assert recorder.wait_for(lambda: sum("when asked about get_more_tools" in line for _, line in recorder.lines) == 1)
        assert not any("recorded for the calls" in line for _, line in recorder.lines)
        await connection.call("own_intent", {"query": "a"})
    assert recorder.wait_for(lambda: sum("recorded for the calls" in line for _, line in recorder.lines) == 1)


async def test_a_failing_task_from_the_inputs_function_is_never_logged_with_its_text() -> None:
    seen: list[dict[str, Any]] = []
    asyncio.get_running_loop().set_exception_handler(lambda _, context: seen.append(context))

    async def fail() -> bool:
        raise RuntimeError("the private note")

    lsquare, _ = client()
    app = high()
    tool(app, search)
    inputs: Any = lambda name: asyncio.create_task(fail())
    instrument(app, lsquare, inputs=inputs)
    async with connect(app) as connection:
        await connection.tools()
    await asyncio.sleep(0)
    gc.collect()
    await asyncio.sleep(0)
    assert seen == []


async def test_still_warns_of_the_servers_own_get_more_tools_with_inputs_off() -> None:
    def get_more_tools(need: str) -> str:
        return need

    recorder = Recorder()
    lsquare, _ = client(logger=recorder)
    app = high()
    tool(app, get_more_tools)
    instrument(app, lsquare, inputs=False)
    async with connect(app) as connection:
        await connection.tools()
        await connection.call("get_more_tools", {"need": "x"})
    assert recorder.wait_for(lambda: sum("has its own get_more_tools" in line for _, line in recorder.lines) == 1)


@pytest.mark.parametrize("inputs", [False, lambda name: name != "get_more_tools"])
async def test_doesnt_offer_get_more_tools_where_its_need_wouldnt_be_recorded_and_says_so_once(inputs: Any) -> None:
    recorder = Recorder()
    lsquare, events = client(logger=recorder)
    app = high()
    tool(app, search)
    instrument(app, lsquare, inputs=inputs)
    async with connect(app) as connection:
        names = [item.name for item in await connection.tools()]
        await connection.tools()
        assert recorder.wait_for(lambda: sum("get_more_tools isn't offered" in line for _, line in recorder.lines) == 1)
        try:
            reply = await connection.call("get_more_tools", {"need": "issue refunds"})
        except Exception:
            reply = None
    await lsquare.aflush(timeout_ms=5_000)
    assert sum("get_more_tools isn't offered" in line for _, line in recorder.lines) == 1
    assert names == ["search"]
    assert reply is None or (reply.is_error and not reply.texts[0].startswith("Noted."))
    assert events.calls()
    assert all("extra" not in event and "issue refunds" not in str(event) for event in events.calls())


async def test_get_more_tools_gives_way_to_a_tool_of_that_name_and_needs_a_tool_of_the_servers_own() -> None:
    lsquare, events = client()
    app = high()

    def get_more_tools(need: str) -> str:
        return f"mine: {need}"

    tool(app, get_more_tools)
    tool(app, search)
    instrument(app, lsquare)
    async with connect(app) as connection:
        names = await connection.names()
        reply = await connection.call("get_more_tools", {"need": "x"})
    assert names.count("get_more_tools") == 1
    assert reply.texts[0] == "mine: x"

    empty = high()
    instrument(empty, client()[0])
    async with connect(empty) as connection:
        assert "get_more_tools" not in await connection.names()


async def test_changes_no_definition_or_listing_in_place() -> None:
    lsquare, _ = client()
    app = high()
    tool(app, search)
    instrument(app, lsquare)

    async def definition() -> Any:
        if FAST:
            return copy.deepcopy((await app.get_tool("search")).parameters)
        return copy.deepcopy(app._tool_manager._tools["search"].parameters)

    before = await definition()
    async with connect(app) as connection:
        await connection.tools()
        await connection.call("search", {"query": "a", "intent": "b"})
    assert await definition() == before
    assert "intent" not in (before.get("properties") or {})


async def test_a_tool_returning_its_own_error_result_gets_the_id_line() -> None:
    if FAST:
        pytest.skip("fastmcp makes every error result itself")
    lsquare, events = client()
    app = high()
    if MAJOR == 1:
        import mcp.types as types

        # No return annotation: the SDK then passes the result on as it is.
        def failing():  # type: ignore[no-untyped-def]
            return types.CallToolResult(content=[types.TextContent(type="text", text="out of stock")], isError=True)

    elif MAJOR == 2:
        import mcp_types

        def failing():  # type: ignore[no-untyped-def]
            return mcp_types.CallToolResult(content=[mcp_types.TextContent(type="text", text="out of stock")], is_error=True)

    else:
        raise AssertionError("unreachable")

    tool(app, failing)
    instrument(app, lsquare)
    async with connect(app) as connection:
        reply = await connection.call("failing", {})
    await lsquare.aflush(timeout_ms=5_000)
    assert reply.is_error and reply.texts[0] == "out of stock"
    assert issued(reply.texts) is not None
    [event] = events.calls()
    assert event["error"]["category"] == "tool_error" and event["sessionSource"] == "issued"
