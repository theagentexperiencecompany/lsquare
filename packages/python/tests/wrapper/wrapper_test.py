"""instrument() on each line: what a call records, its error category, the options, and that
nothing the wrapper does changes a reply or makes a call fail."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import pytest

from helpers import Recorder
from lines import FAST, LINE, MAJOR, client, connect, high, low, needs_line, only, schema, tool
from lsquare import LSquare
from lsquare.mcp import instrument

pytestmark = [needs_line, pytest.mark.anyio]
OFF = {"sessions": False, "intent": False, "missing_tools": False}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def flush(lsquare: LSquare) -> None:
    await lsquare.aflush(timeout_ms=5_000)


def search(query: str) -> str:
    return f"found {query}"


async def asearch(query: str) -> str:
    await asyncio.sleep(0)
    return f"found {query}"


def boom(x: int) -> str:
    raise ValueError("it broke")


async def test_records_a_call_with_its_input_result_server_client_and_version() -> None:
    lsquare, events = client()
    app = high("orders")
    tool(app, search)
    tool(app, asearch)
    instrument(app, lsquare, extras=OFF)
    async with connect(app) as connection:
        sync = await connection.call("search", {"query": "shoes"})
        asynchronous = await connection.call("asearch", {"query": "hats"})
    await flush(lsquare)
    assert sync.texts == ["found shoes"] and asynchronous.texts == ["found hats"]
    first, second = events.calls()
    assert first["toolName"] == "search" and second["toolName"] == "asearch"
    for event, query in ((first, "shoes"), (second, "hats")):
        assert event["status"] == "completed"
        assert event["input"] == {"query": query}
        assert "result" in event
        assert event["server"]["name"] == "orders"
        assert isinstance(event["client"]["name"], str)
        assert isinstance(event["protocolVersion"], str)
        assert event["sdk"]["language"] == "python"
        assert "sessionId" not in event and "intent" not in event


async def test_a_capture_inside_the_tool_is_its_agent_part_and_one_after_it_is_a_turn_of_its_own() -> None:
    lsquare, events = client()
    app = high()
    seen: dict[str, Any] = {}

    def answer(question: str) -> str:
        lsquare.capture({"question": question, "answer": "42", "conversationId": "conv-1", "userId": "u1"})
        lsquare.capture({"question": "again", "answer": "43", "conversationId": "conv-1"})
        seen["call"] = lsquare.current_call_id()
        return "42"

    tool(app, answer)
    instrument(app, lsquare, extras=OFF)
    async with connect(app) as connection:
        await connection.call("answer", {"question": "why?"})
    await flush(lsquare)
    [call] = events.calls()
    [turn] = events.turns()
    assert call["agent"]["question"] == "why?"
    assert call["conversationId"] == "conv-1" and call["userId"] == "u1"
    assert seen["call"] is not None
    assert turn["callId"] == call["callId"] == call["eventId"]


async def test_tells_a_raise_from_the_sdk_refusing_the_call() -> None:
    lsquare, events = client()
    app = high()
    tool(app, boom)
    instrument(app, lsquare, extras=OFF)
    async with connect(app) as connection:
        raised = await connection.call("boom", {"x": 1})
        refused = await connection.call("boom", {"x": "not a number"})
        unknown = await connection.call("nope", {})
    await flush(lsquare)
    assert raised.is_error and refused.is_error and unknown.is_error
    by_input = {str(event.get("input")): event for event in events.calls()}
    thrown = by_input[str({"x": 1})]["error"]
    assert thrown["category"] == "thrown" and thrown["message"] == "it broke" and thrown["type"] == "ValueError"
    assert by_input[str({"x": "not a number"})]["error"]["category"] == "protocol"
    assert by_input[str({})]["error"]["category"] == "protocol"


async def test_a_low_level_servers_handler_is_the_tool() -> None:
    lsquare, events = client()

    async def call(name: str, arguments: dict[str, Any]) -> Any:
        if name == "fails":
            return {"content": [{"type": "text", "text": "no such order"}], "isError": True}
        return [{"type": "text", "text": f"ok {arguments}"}]

    listing = [{"name": "orders", "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}}}, {"name": "fails", "inputSchema": {"type": "object"}}]
    server = low("plain", listing, call)
    # The extras stay off on a low-level server unless passed as True (lowlevel_test.py).
    instrument(server, lsquare)
    async with connect(server) as connection:
        listed = await connection.tools()
        ok = await connection.call("orders", {"id": "o1"})
        failed = await connection.call("fails", {})
    await flush(lsquare)
    assert [item.name for item in listed] == ["orders", "fails"]
    assert "intent" not in (schema(listed[0]).get("properties") or {})
    assert ok.texts == ["ok {'id': 'o1'}"] and failed.is_error
    completed, error = events.calls()
    assert completed["status"] == "completed" and completed["input"] == {"id": "o1"}
    assert error["error"]["category"] == "tool_error" and error["error"]["message"] == "no such order"


async def test_records_no_inputs_or_results_where_told_not_to() -> None:
    lsquare, events = client()
    app = high()
    tool(app, search)
    tool(app, boom)
    instrument(app, lsquare, extras=OFF, inputs=False, results=lambda name: name != "search")
    async with connect(app) as connection:
        await connection.call("search", {"query": "secret"})
    await flush(lsquare)
    [event] = events.calls()
    assert "input" not in event and "result" not in event


async def test_records_no_inputs_or_results_for_a_call_its_function_raises_on_or_is_async_and_says_so_once() -> None:
    recorder = Recorder()
    lsquare, events = client(logger=recorder, on_error=recorder.on_error)
    app = high()
    tool(app, search)
    tool(app, asearch)
    policies = {"search": True}

    async def results(name: str) -> bool:
        return False

    # No entry for asearch: the lookup raises. An async results isn't allowed by the types, but an
    # app may still pass one.
    instrument(app, lsquare, extras=OFF, inputs=lambda name: policies[name], results=cast(Any, results))
    async with connect(app) as connection:
        replies = [await connection.call("asearch", {"query": "a"}), await connection.call("asearch", {"query": "b"})]
        await connection.call("search", {"query": "kept"})
    await flush(lsquare)
    assert [reply.texts for reply in replies] == [["found a"], ["found b"]]
    *hidden, kept = events.calls()
    assert len(hidden) == 2
    assert all("input" not in event and "result" not in event for event in hidden)
    assert kept["input"] == {"query": "kept"} and "result" not in kept
    assert recorder.errors == []
    expected = [
        "[lsquare] The inputs function raised or returned an awaitable: inputs aren't recorded for the calls it answered that way.",
        "[lsquare] The results function raised or returned an awaitable: results aren't recorded for the calls it answered that way.",
    ]
    assert recorder.wait_for(lambda: sorted(line for _, line in recorder.lines) == expected)


#: Caller text that no redaction pattern matches.
PRIVATE = "pin 9876 my-private-note"


async def _ok(name: str, arguments: dict[str, Any]) -> Any:
    return [{"type": "text", "text": "ok"}]


# mcp 2.x checks a request's params inside the middleware chain and answers with its own "Invalid
# request parameters": pydantic's text quotes the values it refused, which inputs=False keeps out.
# (fastmcp 4.x's own check refuses a 2026-07-28 request without _meta before the chain: no event.)
@only("mcp2", "fastmcp4")
@pytest.mark.parametrize(("server", "mode"), [("high", "legacy"), ("high", "2026-07-28"), ("low", "legacy"), ("low", "2026-07-28")])
async def test_params_that_dont_validate_record_the_message_the_client_got(server: str, mode: str) -> None:
    if LINE == "fastmcp4" and server == "high" and mode == "2026-07-28":
        pytest.skip("fastmcp 4.x refuses it before the middleware chain")
    lsquare, events = client()
    if server == "high":
        app = high()
        tool(app, search)
    else:
        app = low("plain", [{"name": "search", "inputSchema": {"type": "object"}}], _ok)
    instrument(app, lsquare, extras=OFF, inputs=False, results=False)
    async with connect(app, mode) as connection:
        with pytest.raises(Exception, match="^Invalid request parameters$"):
            await connection.session.session._dispatcher.send_raw_request("tools/call", {"name": "search", "arguments": PRIVATE})
    await flush(lsquare)
    [event] = events.calls()
    assert event["error"] == {"message": "Invalid request parameters", "type": "ValidationError", "code": -32602, "category": "protocol"}
    assert "input" not in event and PRIVATE not in json.dumps(event)


@only("mcp2", "fastmcp4")
async def test_a_low_level_handlers_own_pydantic_error_records_the_message_the_client_got() -> None:
    import pydantic

    class Order(pydantic.BaseModel):
        quantity: int

    async def call(name: str, arguments: dict[str, Any]) -> Any:
        Order.model_validate({"quantity": arguments.get("q")})
        return []

    lsquare, events = client()
    app = low("plain", [{"name": "search", "inputSchema": {"type": "object"}}], call)
    instrument(app, lsquare, inputs=False, results=False)
    async with connect(app, "legacy") as connection:
        with pytest.raises(Exception, match="^Invalid request parameters$"):
            await connection.call("search", {"q": PRIVATE})
    await flush(lsquare)
    [event] = events.calls()
    assert event["error"]["message"] == "Invalid request parameters" and event["error"]["code"] == -32602
    assert PRIVATE not in json.dumps(event)


async def test_covers_tools_registered_later_and_a_second_instrument_changes_nothing() -> None:
    lsquare, events = client()
    app = high()
    instrument(app, lsquare, extras=OFF)
    instrument(app, lsquare, extras=OFF)
    tool(app, search)
    async with connect(app) as connection:
        await connection.call("search", {"query": "late"})
    await flush(lsquare)
    assert [event["toolName"] for event in events.calls()] == ["search"]


async def test_gives_the_same_replies_and_listings_with_and_without_instrument_extras_off() -> None:
    async def run(instrumented: bool) -> list[Any]:
        lsquare, _ = client()
        app = high()
        tool(app, search)
        tool(app, boom)
        if instrumented:
            instrument(app, lsquare, extras=OFF)
        async with connect(app) as connection:
            listed = [(item.name, schema(item)) for item in await connection.tools()]
            replies = [await connection.call(*args) for args in (("search", {"query": "a"}), ("boom", {"x": 1}), ("boom", {}), ("nope", {}))]
        return [listed, [(reply.texts, reply.is_error, reply.structured) for reply in replies]]

    assert await run(True) == await run(False)


async def test_never_makes_a_call_fail_whatever_part_of_the_wrapper_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from lsquare.mcp import _call, _registry

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("wrapper bug")

    async def reply(arguments: dict[str, Any], instrumented: bool) -> tuple[str, bool]:
        lsquare, _ = client()
        app = high()
        tool(app, search)
        if instrumented:
            instrument(app, lsquare)
        async with connect(app) as connection:
            result = await connection.call("search", arguments)
        # With the extras on, the reply may also carry the ID line: the tool's own text comes first.
        return result.texts[0], result.is_error

    for target, name in (
        (_call, "build_record"),
        (_call, "outcome_of"),
        (_call, "group"),
        (_call, "start_identify"),
        (_registry.Read, "ownership"),
        (_registry.Registry, "read"),
        (_registry.Read, "local"),
        (_call, "tap_tools"),
    ):
        # A definition that can't be read keeps its arguments as sent: as without the wrapper.
        arguments = {"query": "x", "intent": "why"} if name in ("ownership", "read") else {"query": "x"}
        with monkeypatch.context() as patch:
            patch.setattr(target, name, broken)
            got = await reply(arguments, True)
        assert got == await reply(arguments, False), name


async def test_leaves_the_server_as_it_is_without_a_key_or_when_it_isnt_one() -> None:
    recorder = Recorder()
    lsquare = LSquare(logger=recorder)
    app = high()
    assert instrument(app, lsquare) is app
    assert instrument(object(), LSquare(api_key="k", logger=recorder, _transport=client()[1])) is not None
    assert recorder.wait_for(lambda: any("No api_key: instrument()" in line for _, line in recorder.lines))
    assert recorder.wait_for(lambda: any("doesn't recognise this server" in line for _, line in recorder.lines))


async def test_says_in_dry_run_what_it_records_and_what_the_extras_can_change() -> None:
    recorder = Recorder()
    lsquare = LSquare(dry_run=True, logger=recorder)
    app = high("shop")
    tool(app, search)
    instrument(app, lsquare)
    assert recorder.wait_for(lambda: any('"records":"every tools/call"' in line for _, line in recorder.lines))
    line = next(line for _, line in recorder.lines if "every tools/call" in line)
    assert '"server":"shop"' in line and '"level":"high"' in line and "1 of the 1 tools registered so far" in line


@only("mcp1", "fastmcp3")
async def test_a_later_decorator_is_wrapped_again_on_1x() -> None:
    lsquare, events = client()

    async def call(name: str, arguments: dict[str, Any]) -> Any:
        return [{"type": "text", "text": "first"}]

    server = low("plain", [{"name": "t", "inputSchema": {"type": "object"}}], call) if LINE == "mcp1" else None
    if server is None:
        pytest.skip("low-level only")
    instrument(server, lsquare)

    @server.call_tool()
    async def again(name: str, arguments: dict[str, Any]) -> Any:
        import mcp.types as types

        return [types.TextContent(type="text", text="second")]

    async with connect(server) as connection:
        reply = await connection.call("t", {})
    await flush(lsquare)
    assert reply.texts == ["second"]
    assert [event["toolName"] for event in events.calls()] == ["t"]


async def test_records_a_cancelled_call_as_timeout_and_passes_the_cancellation_on() -> None:
    import anyio

    lsquare, events = client()
    app = high()
    started = anyio.Event()

    async def slow() -> str:
        started.set()
        await anyio.sleep(10)
        return "late"

    tool(app, slow)
    instrument(app, lsquare, extras=OFF)
    async with connect(app) as connection:
        async with anyio.create_task_group() as group:
            group.start_soon(connection.call, "slow", {})
            await started.wait()
            await anyio.sleep(0.05)
            group.cancel_scope.cancel()
        # The client cancelled its own request: the server's handler is cancelled in turn on 1.x
        # and fastmcp; the in-process 2.x client may let it run on, so give it time.
        await anyio.sleep(0.3)
    await flush(lsquare)
    calls = events.calls()
    if calls:
        assert calls[0]["status"] == "error" and calls[0]["error"]["category"] == "timeout"
    elif MAJOR == 1 and not FAST:
        pytest.fail("a cancelled call made no event")
