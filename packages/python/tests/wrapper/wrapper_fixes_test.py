"""The deep review's last wrapper findings, on each line: what a call's own tool is and what it
returned (py-mcp-3, 5, 7, 8, 9, 10, 18), what a middleware returns in place of a result
(py-mcp-4), handlers replaced with `|=` (py-mcp-12), SSE's sessions (py-mcp-16), the stdio marker
(py-mcp-20), and the call's parity with TypeScript (parity-8, 9, 12, 21). Then Codex's first review
of those fixes: two copies of the SDK watching one transport, a stdio run's marker, a task or thread
the tool left running, and fastmcp 4.x's extension interceptors."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import hashlib
import gc
import re
import subprocess
import sys
import textwrap
import threading
import weakref
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from helpers import Recorder
from lines import FAST, LINE, MAJOR, client, connect, high, needs_line, only, tool
from lsquare.mcp import IdentifyInput, instrument

pytestmark = [needs_line, pytest.mark.anyio]
ID_LINE = re.compile(r"^Task ID: (ses_[A-Za-z0-9_-]{22})\. ")
OFF = {"sessions": False, "intent": False, "missing_tools": False}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def search(query: str) -> str:
    return f"found {query}"


def own_error(text: str) -> Any:
    """An isError result the tool returns itself, as the line builds one."""
    if FAST:
        from fastmcp.tools.base import ToolResult

        return ToolResult(content=text, is_error=True)
    if MAJOR == 1:
        import mcp.types as types

        return types.CallToolResult(content=[types.TextContent(type="text", text=text)], isError=True)
    import mcp_types

    return mcp_types.CallToolResult(content=[mcp_types.TextContent(type="text", text=text)], is_error=True)


def no_output(**options: Any) -> dict[str, Any]:
    """Registration options for a tool returning a result object as it is: no output schema."""
    return {"output_schema": None, **options} if FAST else options


def errors(events: Any) -> list[tuple[str, Any]]:
    return [(event["toolName"], event.get("error")) for event in events.calls()]


def lowlevel(app: Any) -> Any:
    """The mcp 2.x low-level server under the line's high-level one."""
    return app._mcp_server if FAST else app._lowlevel_server


def name_of(ctx: Any) -> Any:
    params = ctx.params
    return params.get("name") if isinstance(params, dict) else getattr(params, "name", None)


# -- py-mcp-3: what fastmcp's core step returned, for tools the wrapper can't watch ------------------


@only("fastmcp3", "fastmcp4")
async def test_a_mounted_or_proxied_tools_errors_are_its_own_and_fastmcps_refusals_protocol() -> None:
    from fastmcp import FastMCP
    from fastmcp.exceptions import ToolError
    from fastmcp.server import create_proxy

    lsquare, events = client()
    parent, child, backend = high("parent"), FastMCP("child"), FastMCP("backend")

    def local_boom(x: int) -> str:
        raise ValueError("local body raise")

    def boom(x: int) -> str:
        raise ValueError("mounted body raise")

    def says_no(x: int) -> str:
        raise ToolError("the tool says no")

    def own(x: int) -> Any:
        return own_error("its own error")

    def remote(x: int) -> str:
        raise ValueError("backend body raise")

    tool(parent, local_boom)
    tool(child, boom)
    tool(child, says_no)
    tool(child, own, **no_output())
    tool(backend, remote)
    parent.mount(child, namespace="sub")
    parent.mount(create_proxy(backend), namespace="px")
    instrument(parent, lsquare)
    async with connect(parent) as connection:
        for name, x in (("local_boom", 1), ("sub_boom", 1), ("sub_says_no", 1), ("sub_own", 1), ("px_remote", 1), ("sub_boom", "not a number"), ("nope", 1)):
            assert (await connection.call(name, {"x": x})).is_error
    await lsquare.aflush(timeout_ms=5_000)
    assert [(name, error["category"]) for name, error in errors(events)] == [
        ("local_boom", "thrown"),
        # Their functions run unseen: their own errors, whatever raised them.
        ("sub_boom", "tool_error"),
        ("sub_says_no", "tool_error"),
        ("sub_own", "tool_error"),
        ("px_remote", "tool_error"),
        # fastmcp refused them before any tool ran.
        ("sub_boom", "protocol"),
        ("nope", "protocol"),
    ]


@only("fastmcp3")
async def test_a_mounted_tools_output_the_sdk_refuses_is_protocol_as_a_local_ones() -> None:
    # mcp 1.x's low-level server checks the structured output after fastmcp's chain returned.
    from fastmcp import FastMCP
    from fastmcp.tools.base import ToolResult

    lsquare, events = client()
    parent, child = high("parent"), FastMCP("child")
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}

    def wrong(x: int) -> Any:
        return ToolResult(content="wrong", structured_content={"n": "not an int"})

    tool(parent, wrong, name="local_wrong", output_schema=schema)
    tool(child, wrong, output_schema=schema)
    parent.mount(child, namespace="sub")
    instrument(parent, lsquare)
    async with connect(parent) as connection:
        for name in ("local_wrong", "sub_wrong"):
            assert (await connection.call(name, {"x": 1})).is_error
    await lsquare.aflush(timeout_ms=5_000)
    assert [(name, error["category"]) for name, error in errors(events)] == [("local_wrong", "protocol"), ("sub_wrong", "protocol")]


@only("fastmcp3", "fastmcp4")
@pytest.mark.parametrize("how", ["replaced", "in_place"])
async def test_a_success_middleware_makes_an_error_is_protocol_however_it_does_it(how: str) -> None:
    # What the core step returned is read as it returned: a result changed in place afterwards
    # isn't taken for the tool's own error.
    from fastmcp import FastMCP
    from fastmcp.server.middleware import Middleware
    from fastmcp.tools.base import ToolResult

    lsquare, events = client()
    parent, child = high("parent"), FastMCP("child")

    def fine(x: int) -> Any:
        return ToolResult(content="fine")

    tool(parent, fine, name="local_fine", **no_output())
    tool(child, fine, **no_output())
    parent.mount(child, namespace="sub")

    class Errs(Middleware):
        async def on_call_tool(self, context: Any, call_next: Any) -> Any:
            result = await call_next(context)
            if how == "replaced":
                return ToolResult(content="fine", is_error=True)
            result.is_error = True
            return result

    parent.add_middleware(Errs())
    instrument(parent, lsquare)
    async with connect(parent) as connection:
        for name in ("local_fine", "sub_fine"):
            reply = await connection.call(name, {"x": 1})
            assert reply.is_error and reply.texts == ["fine"]
    await lsquare.aflush(timeout_ms=5_000)
    assert errors(events) == [(name, {"message": "fine", "category": "protocol"}) for name in ("local_fine", "sub_fine")]


@only("fastmcp3", "fastmcp4")
@pytest.mark.parametrize("order", ["succeeds_then_raises", "raises_then_errs"])
async def test_a_call_retried_by_middleware_is_its_last_core_steps(order: str) -> None:
    # Each core step's evidence is its own: what an earlier attempt returned, or raised, isn't it.
    from fastmcp import FastMCP
    from fastmcp.server.middleware import Middleware

    lsquare, events = client()
    parent, child = high("parent"), FastMCP("child")
    attempts: list[str] = []

    def flaky(x: int) -> Any:
        attempts.append("run")
        first = len(attempts) % 2 == 1
        if order == "succeeds_then_raises":
            if first:
                return "first attempt fine"
            raise ValueError("second attempt failed")
        if first:
            raise ConnectionError("first attempt failed")
        return own_error("second attempt says no")

    tool(parent, flaky, name="local_flaky", **no_output())
    tool(child, flaky, **no_output())
    parent.mount(child, namespace="sub")

    class Twice(Middleware):
        async def on_call_tool(self, context: Any, call_next: Any) -> Any:
            try:
                await call_next(context)
            except Exception:
                pass
            return await call_next(context)

    parent.add_middleware(Twice())
    instrument(parent, lsquare)
    async with connect(parent) as connection:
        for name in ("local_flaky", "sub_flaky"):
            assert (await connection.call(name, {"x": 1})).is_error
    await lsquare.aflush(timeout_ms=5_000)
    got = [(name, error["category"]) for name, error in errors(events)]
    if order == "succeeds_then_raises":
        # The local function's raise is seen; the mounted one's is its own error, never the first success.
        assert got == [("local_flaky", "thrown"), ("sub_flaky", "tool_error")]
    else:
        assert got == [("local_flaky", "tool_error"), ("sub_flaky", "tool_error")]


@only("fastmcp3", "fastmcp4")
async def test_a_retry_fastmcp_refuses_is_protocol_whatever_the_first_attempts_function_raised() -> None:
    # The second core step refuses the arguments the middleware passes it: no function runs, and
    # the first attempt's raise isn't this one's.
    from fastmcp.server.middleware import Middleware

    lsquare, events = client()
    app = high()

    def boom(x: int) -> str:
        raise ValueError("first attempt failed")

    class Retry(Middleware):
        async def on_call_tool(self, context: Any, call_next: Any) -> Any:
            try:
                return await call_next(context)
            except Exception:
                message = context.message.model_copy(update={"arguments": {"x": "not a number"}})
                return await call_next(context.copy(message=message))

    tool(app, boom)
    app.add_middleware(Retry())
    instrument(app, lsquare)
    async with connect(app) as connection:
        assert (await connection.call("boom", {"x": 1})).is_error
    await lsquare.aflush(timeout_ms=5_000)
    [event] = events.calls()
    assert event["error"]["category"] == "protocol"


@only("fastmcp3", "fastmcp4")
async def test_a_mounted_server_instrumented_with_the_same_client_records_its_tools_raise_on_the_call() -> None:
    # The mounted server's call_tool(), made by the call's core step, runs the call's own tool.
    from fastmcp import FastMCP

    lsquare, events = client()
    parent, child = high("parent"), FastMCP("child")

    def boom(x: int) -> str:
        raise ValueError("mounted body raise")

    tool(child, boom)
    parent.mount(child, namespace="sub")
    instrument(child, lsquare)
    instrument(parent, lsquare)
    async with connect(child) as connection:
        await connection.call("boom", {"x": 1})
    async with connect(parent) as connection:
        await connection.call("sub_boom", {"x": 1})
    await lsquare.aflush(timeout_ms=5_000)
    thrown = {"message": "mounted body raise", "type": "ValueError", "category": "thrown"}
    assert errors(events) == [("boom", thrown), ("sub_boom", thrown)]


# -- py-mcp-4: a model or ErrorData a middleware returns in the handler's place ----------------------


@only("mcp2", "fastmcp4")
@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
async def test_a_middleware_refusing_with_a_model_records_the_refusal(mode: str) -> None:
    import mcp_types

    async def policy(ctx: Any, call_next: Any) -> Any:
        if ctx.method == "tools/call":
            return mcp_types.CallToolResult(is_error=True, content=[mcp_types.TextContent(type="text", text="denied by policy")])
        return await call_next(ctx)

    lsquare, events = client()
    app = high()
    tool(app, search)
    lowlevel(app).middleware.append(policy)
    instrument(app, lsquare)
    async with connect(app, mode) as connection:
        got = await connection.call("search", {"query": "a"})
    assert got.is_error and got.texts == ["denied by policy"]
    await lsquare.aflush(timeout_ms=5_000)
    [event] = events.calls()
    assert (event["status"], event["error"]) == ("error", {"message": "denied by policy", "category": "protocol"})


@only("mcp2", "fastmcp4")
@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
async def test_a_middleware_refusing_with_error_data_records_the_json_rpc_error_the_client_gets(mode: str) -> None:
    import mcp_types
    from mcp.shared.exceptions import MCPError

    async def policy(ctx: Any, call_next: Any) -> Any:
        if ctx.method == "tools/call":
            return mcp_types.ErrorData(code=-32001, message="denied by policy")
        return await call_next(ctx)

    lsquare, events = client()
    app = high()
    tool(app, search)
    lowlevel(app).middleware.append(policy)
    instrument(app, lsquare)
    async with connect(app, mode) as connection:
        # The reply goes on unchanged: the client gets the JSON-RPC error.
        with pytest.raises(MCPError, match="denied by policy") as raised:
            await connection.session.call_tool("search", {"query": "a"})
    assert raised.value.error.code == -32001
    await lsquare.aflush(timeout_ms=5_000)
    [event] = events.calls()
    assert event["status"] == "error" and "result" not in event
    assert event["error"] == {"message": "denied by policy", "type": "MCPError", "code": -32001, "category": "protocol"}


@only("mcp2", "fastmcp4")
async def test_a_middleware_answering_with_an_input_required_model_records_the_round() -> None:
    import mcp_types

    answered: list[int] = []

    async def asking(ctx: Any, call_next: Any) -> Any:
        if ctx.method == "tools/call" and not answered:
            answered.append(1)
            return mcp_types.InputRequiredResult(request_state="state-1", input_requests={})
        return await call_next(ctx)

    lsquare, events = client()
    app = high()
    tool(app, search)
    lowlevel(app).middleware.append(asking)
    instrument(app, lsquare)
    async with connect(app, "2026-07-28") as connection:
        await connection.call("search", {"query": "a"})
    await lsquare.aflush(timeout_ms=5_000)
    assert events.calls()[0].get("round") == "input_required"


@only("mcp1", "fastmcp3")
async def test_a_replaced_tools_call_handler_answering_with_error_data_records_the_json_rpc_error() -> None:
    import mcp.types as types
    from mcp.shared.exceptions import McpError

    lsquare, events = client()
    app = high()
    tool(app, search)
    instrument(app, lsquare)
    low = app._mcp_server

    async def refusing(request: Any) -> Any:
        # mcp 1.x responds with an ErrorData a handler returns as a JSON-RPC error.
        return types.ErrorData(code=-32001, message="denied by policy")

    low.request_handlers[types.CallToolRequest] = refusing
    async with connect(low) as connection:
        with pytest.raises(McpError, match="denied by policy"):
            await connection.session.call_tool("search", {"query": "a"})
    await lsquare.aflush(timeout_ms=5_000)
    [event] = events.calls()
    assert (event["status"], event["error"]["category"], event["error"]["code"]) == ("error", "protocol", -32001)
    assert "result" not in event


# -- py-mcp-5: what a sync tool returns, on lines that never await it --------------------------------


@only("mcp1", "mcp2")
async def test_a_sync_tools_awaitable_value_reaches_the_sdk_as_it_is() -> None:
    class Answer(dict):  # type: ignore[type-arg]
        def __await__(self) -> Any:
            yield from ()
            return self

    def answer():  # type: ignore[no-untyped-def]
        return Answer(answer="ok")

    replies = []
    for instrumented in (False, True):
        lsquare, events = client()
        app = high()
        tool(app, answer, structured_output=False)
        if instrumented:
            instrument(app, lsquare, extras=OFF)
        async with connect(app) as connection:
            replies.append((await connection.call("answer", {})).texts)
        await lsquare.aflush(timeout_ms=5_000)
    assert replies[1] == replies[0] == ['{\n  "answer": "ok"\n}']
    [event] = events.calls()
    assert event["status"] == "completed"


# -- py-mcp-7: one tool object on two servers, each with its own client ------------------------------


async def test_a_tool_object_on_two_servers_records_each_call_on_its_own_client() -> None:
    def boom(x: int) -> str:
        raise ValueError("body failure")

    if FAST:
        from fastmcp.tools import Tool
    elif MAJOR == 1:
        from mcp.server.fastmcp.tools import Tool
    else:
        from mcp.server.mcpserver.tools import Tool
    shared = [Tool.from_function(search), Tool.from_function(boom)]
    recorded = []
    for name in ("a", "b"):
        lsquare, events = client()
        if FAST:
            app = high(name)
            for each in shared:
                app.add_tool(each)
        else:
            app = type(high(name))(name, tools=shared)
        instrument(app, lsquare)
        async with connect(app) as connection:
            found = await connection.call("search", {"query": "x"})
            await connection.call("boom", {"x": 1})
        await lsquare.aflush(timeout_ms=5_000)
        assert ID_LINE.match(found.texts[-1])
        recorded.append([(event.get("sessionSource"), (event.get("error") or {}).get("category")) for event in events.calls()])
    assert recorded == [[("issued", None), (None, "thrown")]] * 2


# -- py-mcp-8 and py-mcp-10: the tool's return is the line, a generator's end included ----------------


@only("fastmcp3", "fastmcp4", "mcp2")
async def test_a_capture_from_middleware_after_the_tool_returned_is_a_turn_and_the_call_keeps_its_id_line() -> None:
    lsquare, events = client()
    app = high()
    tool(app, search)
    if FAST:
        from fastmcp.server.middleware import Middleware

        class After(Middleware):
            async def on_call_tool(self, context: Any, call_next: Any) -> Any:
                result = await call_next(context)
                lsquare.capture({"conversationId": "conv-after", "question": "after", "answer": "mw"})
                return result

        app.add_middleware(After())
    else:

        async def after(ctx: Any, call_next: Any) -> Any:
            result = await call_next(ctx)
            if ctx.method == "tools/call":
                lsquare.capture({"conversationId": "conv-after", "question": "after", "answer": "mw"})
            return result

        app._lowlevel_server.middleware.append(after)
    instrument(app, lsquare)
    async with connect(app) as connection:
        reply = await connection.call("search", {"query": "x"})
    await lsquare.aflush(timeout_ms=5_000)
    assert ID_LINE.match(reply.texts[-1])
    [call] = events.calls()
    [turn] = events.turns()
    assert "agent" not in call and call["sessionSource"] == "issued"
    assert turn["callId"] == call["callId"] and turn["conversationId"] == "conv-after"


@only("fastmcp3", "fastmcp4")
async def test_a_capture_from_middleware_after_a_mounted_tool_returned_is_a_turn_as_after_a_local_one() -> None:
    # The wrapper can't watch the mounted tool's function: the end of the core step that ran it
    # is its return.
    from fastmcp import FastMCP
    from fastmcp.server.middleware import Middleware

    lsquare, events = client()
    parent, child = high("parent"), FastMCP("child")
    tool(parent, search, name="local_search")
    tool(child, search)
    parent.mount(child, namespace="sub")

    class After(Middleware):
        async def on_call_tool(self, context: Any, call_next: Any) -> Any:
            result = await call_next(context)
            lsquare.capture({"conversationId": "conv-after", "question": f"after {context.message.name}", "answer": "mw"})
            return result

    parent.add_middleware(After())
    instrument(parent, lsquare)
    async with connect(parent) as connection:
        for name in ("local_search", "sub_search"):
            await connection.call(name, {"query": "x"})
    await lsquare.aflush(timeout_ms=5_000)
    calls = events.calls()
    # Neither call takes the capture, nor its conversation.
    assert [(event["toolName"], "agent" in event, "conversationId" in event) for event in calls] == [("local_search", False, False), ("sub_search", False, False)]
    turns = {turn["question"]: turn["callId"] for turn in events.turns()}
    assert turns == {"after local_search": calls[0]["callId"], "after sub_search": calls[1]["callId"]}


@only("fastmcp3", "fastmcp4")
async def test_a_capture_from_an_instrumented_mounted_servers_middleware_after_its_tool_is_a_turn() -> None:
    # The mounted server's own chain runs the tool through its core step: its middleware's capture
    # after its call_next is after the tool's return too, also for a tool it can't watch either
    # (one it mounts itself).
    from fastmcp import FastMCP
    from fastmcp.server.middleware import Middleware

    lsquare, events = client()
    parent, child, grandchild = high("parent"), FastMCP("child"), FastMCP("grandchild")
    tool(grandchild, search)

    class ChildAfter(Middleware):
        async def on_call_tool(self, context: Any, call_next: Any) -> Any:
            result = await call_next(context)
            lsquare.capture({"conversationId": "conv-child", "question": "child, after", "answer": "mw"})
            return result

    child.add_middleware(ChildAfter())
    child.mount(grandchild, namespace="gc")
    parent.mount(child, namespace="sub")
    instrument(child, lsquare)
    instrument(parent, lsquare)
    async with connect(parent) as connection:
        assert (await connection.call("sub_gc_search", {"query": "x"})).texts == ["found x"]
    await lsquare.aflush(timeout_ms=5_000)
    [call] = events.calls()
    assert "agent" not in call and "conversationId" not in call
    assert [(turn["question"], turn["callId"]) for turn in events.turns()] == [("child, after", call["callId"])]


@only("fastmcp3", "fastmcp4")
async def test_a_capture_in_a_mounted_tools_body_run_again_by_a_retry_is_the_calls_agent_part() -> None:
    # A retry runs the tool again: inside its second run, a capture is inside the call again.
    from fastmcp import FastMCP
    from fastmcp.server.middleware import Middleware

    lsquare, events = client()
    parent, child = high("parent"), FastMCP("child")
    attempts: list[int] = []

    def flaky(x: int) -> str:
        attempts.append(x)
        if len(attempts) == 1:
            raise ConnectionError("first attempt failed")
        lsquare.capture({"conversationId": "conv-body", "question": "in the second run", "answer": "a"})
        return "second attempt"

    class Retry(Middleware):
        async def on_call_tool(self, context: Any, call_next: Any) -> Any:
            try:
                return await call_next(context)
            except Exception:
                return await call_next(context)

    tool(child, flaky)
    parent.mount(child, namespace="sub")
    parent.add_middleware(Retry())
    instrument(parent, lsquare)
    async with connect(parent) as connection:
        reply = await connection.call("sub_flaky", {"x": 1})
    await lsquare.aflush(timeout_ms=5_000)
    assert len(attempts) == 2 and reply.texts == ["second attempt"]
    [call] = events.calls()
    assert call["agent"]["question"] == "in the second run" and call["conversationId"] == "conv-body"
    assert events.turns() == []


@only("mcp2")
async def test_on_a_low_level_server_a_capture_from_middleware_after_the_handler_returned_is_a_turn() -> None:
    # On a low-level server the handler is the tool: its return is the line.
    from lines import low

    lsquare, events = client()

    attempts: list[str] = []

    async def answer(name: str, arguments: dict[str, Any]) -> Any:
        attempts.append(arguments["query"])
        if attempts.count("again") == 2:
            lsquare.capture({"conversationId": "conv-handler", "question": "in the second attempt", "answer": "h"})
        return [{"type": "text", "text": "ok"}]

    server = low("low", [{"name": "search", "inputSchema": {"type": "object"}}], answer)

    async def after(ctx: Any, call_next: Any) -> Any:
        if ctx.method != "tools/call":
            return await call_next(ctx)
        result = await call_next(ctx)
        lsquare.capture({"conversationId": "conv-after", "question": f"after {ctx.params['arguments']['query']}", "answer": "mw"})
        if ctx.params["arguments"]["query"] == "again":
            # The handler's second attempt: inside it, inside the call again.
            result = await call_next(ctx)
        return result

    server.middleware.append(after)
    instrument(server, lsquare)
    async with connect(server) as connection:
        for query in ("once", "again"):
            await connection.call("search", {"query": query})
    await lsquare.aflush(timeout_ms=5_000)
    assert attempts == ["once", "again", "again"]
    once, again = events.calls()
    assert "agent" not in once and "conversationId" not in once
    assert again["agent"]["question"] == "in the second attempt"
    assert sorted((turn["question"], turn["callId"]) for turn in events.turns()) == [("after again", again["callId"]), ("after once", once["callId"])]


async def test_a_capture_in_the_body_of_a_generator_a_function_returns_is_the_calls_agent_part() -> None:
    # The SDK iterates the generator after the function returned: its run lasts until then.
    lsquare, events = client()
    app = high()

    def body(n: int) -> Iterator[int]:
        lsquare.capture({"conversationId": "conv-body", "question": "in body", "answer": "a"})
        yield n

    def numbers(n: int) -> Any:
        return body(n)

    tool(app, numbers, **({"output_schema": None} if FAST else {"structured_output": False}))
    instrument(app, lsquare)
    async with connect(app) as connection:
        reply = await connection.call("numbers", {"n": 1})
    await lsquare.aflush(timeout_ms=5_000)
    [call] = events.calls()
    assert not any(ID_LINE.match(text) for text in reply.texts)
    assert call["agent"]["question"] == "in body" and call["conversationId"] == "conv-body"
    assert events.turns() == []


@pytest.mark.parametrize("kind", ["sync", "async", "decorated"])
async def test_a_function_returning_a_generator_that_raises_is_thrown(kind: str) -> None:
    lsquare, events = client()
    app = high()

    def body(n: int) -> Any:
        yield n
        raise ValueError("generator body failed")

    def sync_factory(n: int) -> Any:
        return body(n)

    async def async_factory(n: int) -> Any:
        return body(n)

    @functools.wraps(body)
    def decorated(*args: Any, **kwargs: Any) -> Any:
        return body(*args, **kwargs)

    fns: dict[str, Callable[..., Any]] = {"sync": sync_factory, "async": async_factory, "decorated": decorated}
    tool(app, fns[kind], name="gen", **({"output_schema": None} if FAST else {"structured_output": False}))
    instrument(app, lsquare)
    async with connect(app) as connection:
        reply = await connection.call("gen", {"n": 1})
    await lsquare.aflush(timeout_ms=5_000)
    assert reply.is_error
    [event] = events.calls()
    assert (event["error"]["category"], event["error"]["type"]) == ("thrown", "ValueError")


@only("fastmcp3", "fastmcp4")
async def test_a_function_returning_an_async_generator_fastmcp_iterates_is_thrown_when_it_raises() -> None:
    lsquare, events = client()
    app = high()

    async def stream(n: int) -> Any:
        yield n
        raise ValueError("async generator body failed")

    def factory(n: int) -> Any:
        return stream(n)

    tool(app, factory, output_schema=None)
    instrument(app, lsquare)
    async with connect(app) as connection:
        reply = await connection.call("factory", {"n": 1})
    await lsquare.aflush(timeout_ms=5_000)
    assert reply.is_error
    [event] = events.calls()
    assert (event["error"]["category"], event["error"]["type"]) == ("thrown", "ValueError")


# -- py-mcp-9 and py-mcp-18: other calls inside a call record nothing on it ---------------------------


@only("fastmcp3", "fastmcp4")
@pytest.mark.parametrize("audit_raises", [False, True])
@pytest.mark.parametrize("target", ["other", "same"])
async def test_a_call_tool_from_middleware_after_the_tool_leaves_the_calls_result_its_own(target: str, audit_raises: bool) -> None:
    from fastmcp.server.middleware import Middleware

    lsquare, events = client()
    outer, other = high("outer"), high("other")

    def refuse() -> Any:
        return own_error("refuse says no")

    def audit() -> str:
        if audit_raises:
            raise ValueError("audit failed")
        return "audited"

    tool(outer, refuse, **no_output())
    tool(outer, audit)
    tool(other, audit)
    nested = other if target == "other" else outer

    class Audits(Middleware):
        async def on_call_tool(self, context: Any, call_next: Any) -> Any:
            result = await call_next(context)
            if context.message.name == "refuse":
                try:
                    await nested.call_tool("audit", {})
                except Exception:
                    pass
            return result

    outer.add_middleware(Audits())
    instrument(outer, lsquare)
    instrument(other, lsquare)
    async with connect(outer) as connection:
        # The audit's own call taps it.
        await connection.call("audit", {})
        reply = await connection.call("refuse", {})
    await lsquare.aflush(timeout_ms=5_000)
    # Neither its result's provenance nor its outcome are the nested call's.
    assert reply.is_error and ID_LINE.match(reply.texts[-1])
    event = events.calls()[-1]
    assert event["error"] == {"message": "refuse says no", "category": "tool_error"} and event["sessionSource"] == "issued"


@only("fastmcp3", "fastmcp4")
@pytest.mark.parametrize("audit_raises", [False, True])
@pytest.mark.parametrize("when", ["before", "after"])
async def test_a_call_tool_without_middleware_from_middleware_isnt_the_calls_core_step(when: str, audit_raises: bool) -> None:
    # call_tool(run_middleware=False) on the call's own server, as the core step's own call is:
    # only the call_next fastmcp composed makes the call's core step.
    from fastmcp.server.middleware import Middleware

    lsquare, events = client()
    app = high()

    def refuse() -> Any:
        return own_error("refuse says no")

    def audit() -> str:
        if audit_raises:
            raise ValueError("audit failed")
        return "audited"

    tool(app, refuse, **no_output())
    tool(app, audit)

    class Audits(Middleware):
        async def on_call_tool(self, context: Any, call_next: Any) -> Any:
            auditing = context.message.name == "refuse"
            if auditing and when == "before":
                with contextlib.suppress(Exception):
                    await app.call_tool("audit", {}, run_middleware=False)
            result = await call_next(context)
            if auditing and when == "after":
                with contextlib.suppress(Exception):
                    await app.call_tool("audit", {}, run_middleware=False)
            return result

    app.add_middleware(Audits())
    instrument(app, lsquare)
    async with connect(app) as connection:
        # The audit's own call taps it.
        await connection.call("audit", {})
        reply = await connection.call("refuse", {})
    await lsquare.aflush(timeout_ms=5_000)
    assert reply.is_error and reply.texts[0] == "refuse says no" and ID_LINE.match(reply.texts[-1])
    event = events.calls()[-1]
    assert event["error"] == {"message": "refuse says no", "category": "tool_error"} and event["sessionSource"] == "issued"


@only("fastmcp3", "fastmcp4")
@pytest.mark.parametrize("audit_raises", [False, True])
@pytest.mark.parametrize("run_middleware", [True, False])
async def test_a_mounted_servers_middleware_calling_its_own_server_after_its_tool_is_nested(run_middleware: bool, audit_raises: bool) -> None:
    # The mounted server's chain runs the call's tool through its own core step: its middleware's
    # call_tool() of that server, after its call_next, is nested, whatever it runs.
    from fastmcp import FastMCP
    from fastmcp.server.middleware import Middleware

    lsquare, events = client()
    parent, child = high("parent"), FastMCP("child")

    def refuse() -> Any:
        return own_error("refuse says no")

    def audit() -> str:
        if audit_raises:
            raise ValueError("audit failed")
        return "audited"

    tool(child, refuse, **no_output())
    tool(child, audit)

    class Audits(Middleware):
        async def on_call_tool(self, context: Any, call_next: Any) -> Any:
            result = await call_next(context)
            if context.message.name == "refuse":
                with contextlib.suppress(Exception):
                    await child.call_tool("audit", {}, run_middleware=run_middleware)
            return result

    child.add_middleware(Audits())
    parent.mount(child, namespace="sub")
    instrument(child, lsquare)
    instrument(parent, lsquare)
    async with connect(child) as connection:
        # Their own calls tap them.
        await connection.call("audit", {})
        await connection.call("refuse", {})
    async with connect(parent) as connection:
        reply = await connection.call("sub_refuse", {})
    await lsquare.aflush(timeout_ms=5_000)
    assert reply.is_error and reply.texts == ["refuse says no"]
    event = events.calls()[-1]
    assert (event["toolName"], event["error"]) == ("sub_refuse", {"message": "refuse says no", "category": "tool_error"})


@only("fastmcp4")
@pytest.mark.parametrize("when", ["before", "after"])
async def test_a_call_tool_from_mcp2_middleware_isnt_read_as_the_calls_own_steps(when: str) -> None:
    # Outside the handler's step: it can neither stand in for the call's chain nor its core step.
    from fastmcp.server.middleware import Middleware
    from fastmcp.tools.base import ToolResult

    lsquare, events = client()
    app = high()

    class Replaces(Middleware):
        async def on_call_tool(self, context: Any, call_next: Any) -> Any:
            result = await call_next(context)
            return ToolResult(content="replaced by middleware") if context.message.name == "ok" else result

    async def auditing(ctx: Any, call_next: Any) -> Any:
        if when == "before" and ctx.method == "tools/call" and name_of(ctx) == "ok":
            await app.call_tool("audit", {})
        result = await call_next(ctx)
        if when == "after" and ctx.method == "tools/call" and name_of(ctx) == "ok":
            await app.call_tool("audit", {})
        return result

    def ok() -> str:
        return "fine"

    def audit() -> str:
        return "audited"

    app._mcp_server.middleware.append(auditing)
    app.add_middleware(Replaces())
    tool(app, ok, output_schema=None)
    tool(app, audit)
    instrument(app, lsquare)
    async with connect(app) as connection:
        await connection.call("audit", {})
        reply = await connection.call("ok", {})
    await lsquare.aflush(timeout_ms=5_000)
    # A result fastmcp's middleware replaced gets no ID line, whatever else ran.
    assert reply.texts == ["replaced by middleware"]
    assert "sessionSource" not in events.calls()[-1]


@only("mcp2", "fastmcp4")
@pytest.mark.parametrize("audit_raises", [True, False])
@pytest.mark.parametrize("when", ["before", "after"])
async def test_a_call_tool_from_mcp2_middleware_around_the_tool_leaves_the_calls_outcome_its_own(when: str, audit_raises: bool) -> None:
    lsquare, events = client()
    holder: dict[str, Any] = {}

    async def audited(ctx: Any) -> None:
        if ctx.method == "tools/call" and name_of(ctx) == "refuse":
            try:
                await holder["app"].call_tool("audit", {})
            except Exception:
                pass

    async def auditing(ctx: Any, call_next: Any) -> Any:
        if when == "before":
            await audited(ctx)
        result = await call_next(ctx)
        if when == "after":
            await audited(ctx)
        return result

    if FAST:
        app = high()
        app._mcp_server.middleware.append(auditing)
    else:
        from mcp.server.mcpserver import MCPServer

        app = MCPServer("test", middleware=[auditing])
    holder["app"] = app

    def refuse() -> Any:
        return own_error("refuse says no")

    def audit() -> str:
        if audit_raises:
            raise ValueError("audit failed")
        return "audited"

    tool(app, refuse, **no_output())
    tool(app, audit)
    instrument(app, lsquare)
    async with connect(app) as connection:
        await connection.call("audit", {})
        reply = await connection.call("refuse", {})
    await lsquare.aflush(timeout_ms=5_000)
    assert reply.is_error and ID_LINE.match(reply.texts[-1])
    event = events.calls()[-1]
    assert event["error"] == {"message": "refuse says no", "category": "tool_error"} and event["sessionSource"] == "issued"


@only("mcp2", "fastmcp4")
async def test_a_middleware_calling_the_handler_again_records_its_last_attempt() -> None:
    # Each call_next is a handler attempt of its own: a first raise isn't the retry's.
    lsquare, events = client()
    attempts: list[int] = []

    async def retrying(ctx: Any, call_next: Any) -> Any:
        if ctx.method != "tools/call":
            return await call_next(ctx)
        await call_next(ctx)
        return await call_next(ctx)

    if FAST:
        app = high()
        app._mcp_server.middleware.append(retrying)
    else:
        from mcp.server.mcpserver import MCPServer

        app = MCPServer("test", middleware=[retrying])

    def flaky() -> Any:
        attempts.append(1)
        if len(attempts) == 1:
            raise ValueError("first attempt failed")
        return own_error("second attempt says no")

    tool(app, flaky, **no_output())
    instrument(app, lsquare)
    async with connect(app) as connection:
        reply = await connection.call("flaky", {})
    await lsquare.aflush(timeout_ms=5_000)
    assert len(attempts) == 2 and reply.texts[0] == "second attempt says no" and ID_LINE.match(reply.texts[-1])
    [event] = events.calls()
    assert event["error"] == {"message": "second attempt says no", "category": "tool_error"}


async def test_a_tool_calling_another_through_the_server_keeps_its_own_outcome() -> None:
    lsquare, events = client()
    app = high()

    def inner():  # type: ignore[no-untyped-def]
        raise ValueError("inner secret failure")

    async def outer():  # type: ignore[no-untyped-def]
        try:
            await app.call_tool("inner", {})
        except Exception:
            pass
        return own_error("outer says no")

    tool(app, inner)
    tool(app, outer, **no_output())
    instrument(app, lsquare)
    async with connect(app) as connection:
        # inner's own call taps it.
        await connection.call("inner", {})
        reply = await connection.call("outer", {})
    await lsquare.aflush(timeout_ms=5_000)
    assert reply.is_error and reply.texts[0] == "outer says no" and ID_LINE.match(reply.texts[-1])
    event = events.calls()[1]
    assert event["toolName"] == "outer" and event["error"] == {"message": "outer says no", "category": "tool_error"}


@only("fastmcp3", "fastmcp4")
async def test_a_tool_calling_a_mounted_servers_tool_from_its_body_keeps_its_own_outcome() -> None:
    # From inside the tool's own function, the mounted server's call_tool() is nested: neither its
    # chain nor its core step is the call's, whatever it runs.
    from fastmcp import FastMCP

    lsquare, events = client()
    parent, child = high("parent"), FastMCP("child")

    def inner() -> str:
        raise ValueError("inner secret failure")

    async def outer() -> Any:
        with contextlib.suppress(Exception):
            await child.call_tool("inner", {})
        return own_error("outer says no")

    tool(child, inner)
    tool(parent, outer, **no_output())
    parent.mount(child, namespace="sub")
    instrument(child, lsquare)
    instrument(parent, lsquare)
    async with connect(child) as connection:
        # inner's own call taps it.
        await connection.call("inner", {})
    async with connect(parent) as connection:
        reply = await connection.call("outer", {})
    await lsquare.aflush(timeout_ms=5_000)
    assert reply.is_error and reply.texts[0] == "outer says no"
    event = events.calls()[-1]
    assert event["toolName"] == "outer" and event["error"] == {"message": "outer says no", "category": "tool_error"}


@only("fastmcp3", "fastmcp4")
async def test_a_tool_run_again_by_a_retry_middleware_records_its_last_runs_outcome() -> None:
    from fastmcp.server.middleware import Middleware

    lsquare, events = client()
    app = high()
    attempts: list[int] = []

    def flaky(x: int) -> Any:
        attempts.append(x)
        if len(attempts) == 1:
            raise ConnectionError("first attempt failed")
        return own_error("tool says no")

    class Retry(Middleware):
        async def on_call_tool(self, context: Any, call_next: Any) -> Any:
            try:
                return await call_next(context)
            except Exception:
                return await call_next(context)

    tool(app, flaky, **no_output())
    app.add_middleware(Retry())
    instrument(app, lsquare)
    async with connect(app) as connection:
        reply = await connection.call("flaky", {"x": 1})
    await lsquare.aflush(timeout_ms=5_000)
    assert len(attempts) == 2 and reply.is_error and reply.texts[0] == "tool says no" and ID_LINE.match(reply.texts[-1])
    [event] = events.calls()
    assert event["error"] == {"message": "tool says no", "category": "tool_error"} and event["sessionSource"] == "issued"


# -- py-mcp-12: `|=` on mcp 1.x's handlers --------------------------------------------------------------


@only("mcp1", "fastmcp3")
async def test_a_tools_call_handler_replaced_with_an_in_place_merge_is_wrapped_as_one_set_by_index() -> None:
    import mcp.types as types

    from lines import schema

    lsquare, events = client()
    app = high()
    tool(app, search)
    instrument(app, lsquare)
    low = app._mcp_server

    async def replacement(request: Any) -> Any:
        return types.ServerResult(types.CallToolResult(content=[types.TextContent(type="text", text="replaced")], structuredContent={"result": "replaced"}))

    handlers = low.request_handlers
    handlers |= {types.CallToolRequest: replacement}
    assert low.request_handlers is handlers
    async with connect(low) as connection:
        listed = {item.name: sorted(schema(item).get("properties") or {}) for item in await connection.tools()}
        reply = await connection.call("search", {"query": "a"})
    await lsquare.aflush(timeout_ms=5_000)
    assert reply.texts == ["replaced"]
    # Replaced: read as a low-level server's, its extras off, as after `[]=`.
    assert listed == {"search": ["query"]}
    assert len(events.calls()) == 1


# -- py-mcp-16: SSE's sessions ------------------------------------------------------------------------


def sse_app(app: Any) -> Any:
    """The line's own SSE ASGI app, at /sse."""
    return app.http_app(transport="sse") if FAST else app.sse_app()


async def test_sse_groups_a_connections_calls_by_one_protocol_session_and_never_takes_an_mcp_session_id_header() -> None:
    from mcp.client.session import ClientSession
    from mcp.client.sse import sse_client

    from transport_test import serving

    lsquare, events = client()
    app = high("sse")
    tool(app, search)
    instrument(app, lsquare)
    replies: list[list[str]] = []
    async with serving(sse_app(app)) as url:
        url = url.removesuffix("/mcp") + "/sse"
        # The second connection sends a header SSE never issued, nor checks.
        for headers in (None, {"mcp-session-id": "spoofed-by-the-client"}):
            async with sse_client(url, headers=headers) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                for query in ("a", "b"):
                    result = await session.call_tool("search", {"query": f"{query}{len(replies)}"})
                    replies.append([getattr(block, "text", "") for block in result.content])
    await lsquare.aflush(timeout_ms=5_000)
    calls = events.calls()
    assert [len(reply) for reply in replies] == [1, 1, 1, 1], replies
    assert [event["sessionSource"] for event in calls] == ["protocol"] * 4
    sessions = [event["sessionId"] for event in calls]
    assert "spoofed-by-the-client" not in sessions
    assert sessions[0] == sessions[1] and sessions[2] == sessions[3] and sessions[0] != sessions[2]


@only("mcp1", "fastmcp3")
@pytest.mark.parametrize("order", [("plain", "added", "removed"), ("added", "plain", "added")])
async def test_a_stateful_http_session_keeps_its_id_whatever_query_a_request_adds(order: tuple[str, ...]) -> None:
    # SSE's query parameter, sent on a Streamable HTTP session, can't make it two sessions, nor
    # move it off its issued ID, also when its first call sends it: the transport that opened the
    # session says which it is, not the request.
    import http.client
    import json
    import urllib.parse

    from transport_test import RAW, asked, calling, http_app, serving

    def post(url: str, body: dict[str, Any], headers: dict[str, str]) -> tuple[int, str, str | None]:
        parts = urllib.parse.urlsplit(url)
        connection = http.client.HTTPConnection(parts.hostname or "127.0.0.1", parts.port, timeout=10)
        try:
            target = parts.path + (f"?{parts.query}" if parts.query else "")
            connection.request("POST", target, body=json.dumps(body).encode("utf-8"), headers={**RAW, **headers})
            response = connection.getresponse()
            return response.status, response.read().decode("utf-8"), response.getheader("mcp-session-id")
        finally:
            connection.close()

    lsquare, events = client()
    app = high("http")
    tool(app, search)
    instrument(app, lsquare)

    def session(url: str) -> str:
        initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": asked(), "capabilities": {}, "clientInfo": {"name": "raw", "version": "1"}}}
        status, body, session_id = post(url, initialize, {})
        assert status == 200 and session_id, body
        headers = {"mcp-session-id": session_id, "mcp-protocol-version": asked()}
        post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, headers)
        for index, query in enumerate(order):
            target = url + "?session_id=0123456789abcdef0123456789abcdef" if query == "added" else url
            status, body, _ = post(target, calling(query, index + 2), headers)
            assert status == 200, body
        return session_id

    async with serving(http_app(app, stateless=False)) as url:
        issued = await asyncio.to_thread(session, url)
    await lsquare.aflush(timeout_ms=5_000)
    assert [(event["input"]["query"], event["sessionId"], event["sessionSource"]) for event in events.calls()] == [
        (query, hashlib.sha256(issued.encode()).hexdigest(), "protocol") for query in order
    ]


# -- py-mcp-20: the stdio marker is the server's own --------------------------------------------------


async def test_a_server_serving_from_inside_another_servers_stdio_run_is_not_counted_as_stdio() -> None:
    lsquare, events = client()
    outer, inner = high("outer"), high("inner")
    tool(outer, search)
    tool(inner, search)
    seen: dict[str, Any] = {}

    async def run_stdio_async(*args: Any, **kwargs: Any) -> None:
        # Standing in for the run: serves both, in memory, from inside it (from its lifespan, say),
        # to a client on a protocol version with sessions.
        for name, app in (("outer", outer), ("inner", inner)):
            async with connect(app, "legacy") as connection:
                seen[name] = (await connection.call("search", {"query": name})).texts

    outer.run_stdio_async = run_stdio_async
    instrument(outer, lsquare)
    instrument(inner, lsquare)
    await outer.run_stdio_async()
    await lsquare.aflush(timeout_ms=5_000)
    calls = {event["input"]["query"]: event for event in events.calls()}
    # The outer server's own run: a persistent connection, its result unchanged.
    assert calls["outer"]["sessionSource"] == "protocol" and len(seen["outer"]) == 1
    # The inner one's connection isn't that run's: an issued ID, given in the reply.
    match = ID_LINE.match(seen["inner"][-1])
    assert match is not None, seen["inner"]
    assert (calls["inner"]["sessionSource"], calls["inner"]["sessionId"]) == ("issued", match.group(1))


@only("fastmcp3", "fastmcp4")
async def test_inside_fastmcps_own_stdio_run_another_servers_calls_arent_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    # fastmcp's real run, which marks its own transport "stdio" around its lifespan: a server
    # served from that lifespan inherits fastmcp's marker, never the wrapper's.
    import anyio
    import fastmcp.server.context as fastmcp_context
    import fastmcp.server.mixins.transport as transport
    from fastmcp import FastMCP

    lsquare, events = client()
    inner = high("inner")
    tool(inner, search)
    seen: dict[str, Any] = {}

    @contextlib.asynccontextmanager
    async def lifespan(server: Any) -> Any:
        seen["transport"] = fastmcp_context._current_transport.get()
        async with connect(inner._mcp_server, "legacy" if MAJOR == 2 else None) as connection:
            seen["inner"] = (await connection.call("search", {"query": "inner"})).texts
        yield {}

    outer = FastMCP("outer", lifespan=lifespan)
    tool(outer, search)

    @contextlib.asynccontextmanager
    async def closed_stdio() -> Any:
        # stdin already at its end: the run serves nothing of its own, and ends.
        send_in, receive_in = anyio.create_memory_object_stream[Any](0)
        send_out, receive_out = anyio.create_memory_object_stream[Any](10)
        await send_in.aclose()
        yield receive_in, send_out

    monkeypatch.setattr(transport, "stdio_server", closed_stdio)
    instrument(outer, lsquare)
    instrument(inner, lsquare)
    await outer.run_stdio_async(show_banner=False)
    await lsquare.aflush(timeout_ms=5_000)
    assert seen["transport"] == "stdio"
    [call] = events.calls()
    match = ID_LINE.match(seen["inner"][-1])
    assert match is not None, seen["inner"]
    assert (call["sessionSource"], call["sessionId"]) == ("issued", match.group(1))


# -- parity-8: a last success without IDs needs none from capture() -----------------------------------


@pytest.mark.parametrize("anonymous", [None, {"userId": "", "orgId": None}])
async def test_a_failure_after_a_success_without_ids_is_sent_in_full_as_in_typescript(anonymous: Any) -> None:
    recorder = Recorder()
    lsquare, events = client(on_error=recorder.on_error)
    count = [0]

    def identify(call: IdentifyInput) -> Any:
        count[0] += 1
        if count[0] == 1:
            return anonymous
        raise RuntimeError("down")

    app = high()
    tool(app, search)
    instrument(app, lsquare, identify=identify, extras=OFF)
    async with connect(app) as connection:
        await connection.call("search", {"query": "one"})
        await connection.call("search", {"query": "two"})
    await lsquare.aflush(timeout_ms=5_000)
    _, second = events.calls()
    assert lsquare._identified["keys"] == ()
    assert "identity" not in second and second["input"] == {"query": "two"} and "result" in second
    assert recorder.wait_for(lambda: "identify_failed" in recorder.codes())
    [failed] = [error for error in recorder.errors if error.code == "identify_failed"]
    assert failed.message == "identify threw; the call was sent with capture()'s IDs."


# -- parity-9: a later capture() inside a call whose callId is absent is linked to it ------------------


async def test_a_later_capture_with_a_null_or_empty_call_id_is_linked_to_its_call() -> None:
    lsquare, events = client()
    seen: list[Any] = []
    app = high()

    def ask(query: str) -> str:
        seen.append(lsquare.current_call_id())
        lsquare.capture({"question": "first"})
        lsquare.capture({"question": "omitted"})
        lsquare.capture({"question": "none", "callId": None})
        lsquare.capture({"question": "empty", "callId": ""})
        lsquare.capture({"question": "literal", "callId": "empty"})
        lsquare.capture({"question": "other", "callId": "call-elsewhere"})
        return "ok"

    tool(app, ask)
    instrument(app, lsquare, extras=OFF)
    async with connect(app) as connection:
        await connection.call("ask", {"query": "a"})
    await lsquare.aflush(timeout_ms=5_000)
    turns = {event["question"]: event for event in events.turns()}
    for question in ("omitted", "none", "empty"):
        assert turns[question]["callId"] == seen[0] and "dropped" not in turns[question], question
    assert turns["literal"]["callId"] == "empty"
    assert turns["other"]["callId"] == "call-elsewhere"


# -- parity-12: a cancellation the tool handles is still the call's timeout ---------------------------


async def test_a_cancelled_call_whose_tool_catches_the_cancellation_is_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import anyio

    from lsquare.mcp import _call
    from lsquare.mcp._lists import Wait as Base

    waits: list[Any] = []

    class Wait(Base):
        def __init__(self) -> None:
            super().__init__()
            self.marked = False
            waits.append(self)

        def cancelled(self) -> None:
            self.marked = True
            super().cancelled()

    monkeypatch.setattr(_call, "Wait", Wait)
    lsquare, events = client()
    app = high()
    started = anyio.Event()
    handled: list[str] = []

    async def slow(query: str) -> str:
        started.set()
        try:
            await anyio.sleep_forever()
        except anyio.get_cancelled_exc_class():
            handled.append("cancelled")
            return "cleaned up"
        return "never"

    tool(app, slow)
    instrument(app, lsquare, extras=OFF)
    async with connect(app) as connection:
        async with anyio.create_task_group() as group:
            group.start_soon(connection.call, "slow", {"query": "a"})
            await started.wait()
            group.cancel_scope.cancel()
    # The server cancels the tool once it hears so, or, as mcp 1.x's in-memory client doesn't
    # say, once the connection closes.
    await lsquare.aflush(timeout_ms=5_000)
    [event] = events.calls()
    assert handled == ["cancelled"]
    assert event["status"] == "error" and event["error"]["category"] == "timeout" and "result" not in event
    # The call's own snapshot wait knows it was cancelled, as for a raised cancellation.
    assert [wait.marked for wait in waits] == [True]


@pytest.mark.parametrize("anyio_backend", ["trio"])
async def test_on_trio_a_cancelled_call_whose_tool_catches_the_cancellation_is_a_timeout(anyio_backend: str) -> None:
    import anyio

    lsquare, events = client()
    app = high()
    started = anyio.Event()
    handled: list[str] = []

    async def slow(query: str) -> str:
        started.set()
        try:
            await anyio.sleep_forever()
        except anyio.get_cancelled_exc_class():
            handled.append("cancelled")
            return "cleaned up"
        return "never"

    tool(app, slow)
    instrument(app, lsquare, extras=OFF)
    # The official client, at the low level, as fastmcp's starts asyncio tasks.
    server = app._mcp_server if FAST else app
    async with connect(server, mode="legacy") if MAJOR == 2 else connect(server) as connection:
        async with anyio.create_task_group() as group:
            group.start_soon(connection.call, "slow", {"query": "a"})
            await started.wait()
            group.cancel_scope.cancel()
    await lsquare.aflush(timeout_ms=5_000)
    [event] = events.calls()
    assert handled == ["cancelled"]
    assert event["status"] == "error" and event["error"]["category"] == "timeout" and "result" not in event


@only("mcp2", "fastmcp4")
async def test_a_cancelled_call_whose_handler_runs_on_in_mcp2s_signal_mode_is_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    # mcp 2.x's peer_cancel_mode="signal" (a dispatcher the app builds itself) only sets the
    # request's cancel_requested and lets the handler finish; the result is never answered.
    import anyio
    from mcp.shared import jsonrpc_dispatcher

    init = jsonrpc_dispatcher.JSONRPCDispatcher.__init__
    notified = jsonrpc_dispatcher.JSONRPCDispatcher._dispatch_notification
    told = threading.Event()

    def signal_mode(self: Any, *args: Any, **kwargs: Any) -> None:
        init(self, *args, **{**kwargs, "peer_cancel_mode": "signal"})

    def spy(self: Any, msg: Any, *args: Any, **kwargs: Any) -> Any:
        out = notified(self, msg, *args, **kwargs)
        if msg.method == "notifications/cancelled":
            told.set()
        return out

    monkeypatch.setattr(jsonrpc_dispatcher.JSONRPCDispatcher, "__init__", signal_mode)
    monkeypatch.setattr(jsonrpc_dispatcher.JSONRPCDispatcher, "_dispatch_notification", spy)
    lsquare, events = client()
    app = high()
    started = anyio.Event()
    finished = anyio.Event()

    async def slow(query: str) -> str:
        started.set()
        await anyio.to_thread.run_sync(told.wait, 10)
        finished.set()
        return "finished anyway"

    tool(app, slow)
    instrument(app, lsquare, extras=OFF)
    # The legacy era: JSON-RPC over memory streams, through the dispatcher patched above.
    async with connect(app, mode="legacy") as connection:
        async with anyio.create_task_group() as group:
            group.start_soon(connection.call, "slow", {"query": "a"})
            await started.wait()
            group.cancel_scope.cancel()
        # Ran on to its end, the connection still open: nothing but the cancel can make it a timeout.
        await finished.wait()
    await lsquare.aflush(timeout_ms=5_000)
    [event] = events.calls()
    assert told.is_set()
    assert event["status"] == "error" and event["error"]["category"] == "timeout" and "result" not in event


# -- parity-21: a non-string orgId from capture() inside the call is invalid_id ------------------------


async def test_a_non_string_org_id_from_capture_inside_the_call_is_invalid_id() -> None:
    recorder = Recorder()
    lsquare, events = client(on_error=recorder.on_error)
    app = high()

    def answering(query: str) -> str:
        lsquare.capture({"question": query, "conversationId": "c1", "turnId": "t1", "orgId": 5})
        return "ok"

    tool(app, answering)
    instrument(app, lsquare, extras=OFF)
    async with connect(app) as connection:
        reply = await connection.call("answering", {"query": "q"})
    await lsquare.aflush(timeout_ms=5_000)
    assert reply.texts == ["ok"] and events.calls() == []
    assert recorder.wait_for(lambda: recorder.errors != [])
    [error] = recorder.errors
    assert (error.code, error.paths) == ("invalid_id", ("ids.orgId",))


async def test_a_non_string_org_id_from_a_later_capture_drops_that_turn_not_the_call() -> None:
    # As REFERENCE.md says, and as in TypeScript: the turn's own orgId, at its top level.
    recorder = Recorder()
    lsquare, events = client(on_error=recorder.on_error)
    app = high()

    def answering(query: str) -> str:
        lsquare.capture({"question": "first"})
        lsquare.capture({"question": "second", "orgId": 5})
        return "ok"

    tool(app, answering)
    instrument(app, lsquare, extras=OFF)
    async with connect(app) as connection:
        await connection.call("answering", {"query": "q"})
    await lsquare.aflush(timeout_ms=5_000)
    assert [call["agent"]["question"] for call in events.calls()] == ["first"] and events.turns() == []
    assert recorder.wait_for(lambda: recorder.errors != [])
    [error] = recorder.errors
    assert (error.code, error.paths) == ("invalid_id", ("orgId",))


# -- py-mcp-3 (docs): which raises of a mounted tool's body read as fastmcp's refusals ---------------


@only("fastmcp3", "fastmcp4")
async def test_a_mounted_tools_body_raising_a_refusal_fastmcp_passes_on_as_it_is_reads_as_one() -> None:
    # The wrapper can't watch the body: an exception fastmcp refuses calls with is a refusal when it
    # reaches the core step as it is. fastmcp wraps a body's NotFoundError and DisabledError (and,
    # on 4.x, pydantic's ValidationError) in a ToolError, which is the tool's.
    import pydantic
    from fastmcp import FastMCP
    from fastmcp.exceptions import AuthorizationError, DisabledError, NotFoundError, ValidationError

    lsquare, events = client()
    parent, child = high("parent"), FastMCP("child")

    def auth(x: int) -> str:
        raise AuthorizationError("body says unauthorized")

    def invalid(x: int) -> str:
        raise ValidationError("body says invalid")

    def pydantic_invalid(x: int) -> str:
        return str(pydantic.TypeAdapter(int).validate_python("not a number"))

    def disabled(x: int) -> str:
        raise DisabledError("body says disabled")

    def missing(x: int) -> str:
        raise NotFoundError("body says missing")

    def plain(x: int) -> str:
        raise ValueError("body plain")

    names = ("auth", "invalid", "pydantic_invalid", "disabled", "missing", "plain")
    for fn in (auth, invalid, pydantic_invalid, disabled, missing, plain):
        tool(child, fn)
    parent.mount(child, namespace="sub")
    instrument(parent, lsquare)
    async with connect(parent) as connection:
        for name in names:
            await connection.call(f"sub_{name}", {"x": 1})
    await lsquare.aflush(timeout_ms=5_000)
    pydantic_category = "protocol" if MAJOR == 1 else "tool_error"
    assert [(name, error["category"]) for name, error in errors(events)] == [
        ("sub_auth", "protocol"),
        ("sub_invalid", "protocol"),
        ("sub_pydantic_invalid", pydantic_category),
        ("sub_disabled", "tool_error"),
        ("sub_missing", "tool_error"),
        ("sub_plain", "tool_error"),
    ]


# Codex's first review of the wrapper and runtime fixes, 31 October 2026. ------------------------------


def run_python(code: str) -> subprocess.CompletedProcess[str]:
    # The line's own Python, importing the lsquare it imports.
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)], capture_output=True, text=True, timeout=60)


# -- Review 1: two copies of the SDK watching mcp 1.x's Streamable HTTP transport ---------------------


@only("mcp1", "fastmcp3")
def test_two_copies_of_the_sdk_watching_the_streamable_http_transport_wrap_it_once_each() -> None:
    # Each copy looked only at the outermost wrap for its own, so each wrapped the other's again
    # every time it looked: after 400 rounds a connection raised RecursionError.
    done = run_python(
        """
        import importlib.util, os, sys
        import anyio
        import lsquare.mcp._v1 as first
        from mcp.server.streamable_http import StreamableHTTPServerTransport as Transport

        root = os.path.dirname(sys.modules["lsquare"].__file__)
        spec = importlib.util.spec_from_file_location("lsquare_copy", os.path.join(root, "__init__.py"), submodule_search_locations=[root])
        copy = importlib.util.module_from_spec(spec)
        sys.modules["lsquare_copy"] = copy
        spec.loader.exec_module(copy)
        import lsquare_copy.mcp._v1 as second

        assert first._OPENED is not second._OPENED
        for _ in range(400):
            first.watch_transports()
            second.watch_transports()
        layers, method = 0, Transport.__dict__["connect"]
        while getattr(method, "__lsquare_watches__", None) is not None:
            layers += 1
            method = method.__wrapped__
        print("layers", layers, flush=True)

        async def connect() -> None:
            async with Transport(mcp_session_id=None).connect() as (read, write):
                print("noted", read in first._OPENED, read in second._OPENED, flush=True)

        anyio.run(connect)
        """
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.split() == ["layers", "2", "noted", "True", "True"], done.stdout + done.stderr


# -- Review 5: a stdio run's marker keeps nothing of the server ---------------------------------------


async def test_a_task_started_during_a_stdio_run_keeps_neither_the_server_nor_its_client_after_it() -> None:
    # The marker was the server's State, which holds the server and its client: a task made during
    # the run inherited it, and kept both for as long as it lived.
    lsquare, _ = client()
    app = high("stdio")
    tool(app, search)
    started = asyncio.Event()
    released = asyncio.Event()

    async def waiting() -> None:
        started.set()
        await released.wait()

    async def run_stdio_async(*args: Any, **kwargs: Any) -> None:
        # Standing in for the run: a task of the app's that outlives it.
        tasks.append(asyncio.ensure_future(waiting()))
        await started.wait()

    tasks: list[asyncio.Task[None]] = []
    app.run_stdio_async = run_stdio_async
    instrument(app, lsquare)
    await app.run_stdio_async()
    server = weakref.ref(app)
    owner = weakref.ref(lsquare)
    del app, lsquare, run_stdio_async
    gc.collect()
    try:
        assert not tasks[0].done()
        assert server() is None and owner() is None
    finally:
        released.set()
        await tasks[0]


# -- Review 6: a task or thread the tool left running is never the call's ----------------------------


@only("fastmcp3", "fastmcp4")
@pytest.mark.parametrize("run_middleware", [True, False])
@pytest.mark.parametrize("target", ["other", "same"])
async def test_a_task_the_tool_left_running_calling_a_tool_after_it_returned_records_nothing_on_the_call(target: str, run_middleware: bool) -> None:
    # The task inherited the tool's run and its core step. Once the tool returned, its run no longer
    # counted as running, so the task's call_tool() of another server qualified as the call's own.
    from fastmcp.server.middleware import Middleware

    lsquare, events = client()
    outer, other = high("outer"), high("other")
    released = asyncio.Event()
    tasks: list[asyncio.Task[None]] = []
    audits: list[int] = []
    nested = other if target == "other" else outer

    async def later() -> None:
        await released.wait()
        with contextlib.suppress(Exception):
            await nested.call_tool("audit", {}, run_middleware=run_middleware)

    async def refuse() -> Any:
        tasks.append(asyncio.ensure_future(later()))
        return own_error("refuse says no")

    def audit() -> str:
        audits.append(1)
        raise ValueError("audit failed")

    tool(outer, refuse, **no_output())
    tool(outer, audit)
    tool(other, audit)

    class Releases(Middleware):
        async def on_call_tool(self, context: Any, call_next: Any) -> Any:
            result = await call_next(context)
            if context.message.name == "refuse":
                # The tool has returned, and the call hasn't ended: the task runs now.
                released.set()
                await tasks[0]
            return result

    outer.add_middleware(Releases())
    instrument(outer, lsquare)
    instrument(other, lsquare)
    async with connect(other) as connection:
        # The audit's own calls tap it.
        await connection.call("audit", {})
    async with connect(outer) as connection:
        await connection.call("audit", {})
        reply = await connection.call("refuse", {})
    await lsquare.aflush(timeout_ms=5_000)
    # The task's audit ran, after the tool returned.
    assert tasks[0].done() and len(audits) == 3
    assert reply.is_error and reply.texts[0] == "refuse says no" and ID_LINE.match(reply.texts[-1])
    event = events.calls()[-1]
    assert event["toolName"] == "refuse"
    assert event["error"] == {"message": "refuse says no", "category": "tool_error"} and event["sessionSource"] == "issued"


@only("fastmcp3", "fastmcp4")
async def test_a_task_a_mounted_tool_the_wrapper_cant_watch_left_running_records_nothing_on_the_call() -> None:
    # No run to inherit here: the task held the parent's core step, which, once ended, still let
    # another server's call_tool() be the call's tool running there.
    from fastmcp import FastMCP
    from fastmcp.server.middleware import Middleware

    lsquare, events = client()
    parent, child, other = high("parent"), FastMCP("child"), high("other")
    released = asyncio.Event()
    tasks: list[asyncio.Task[None]] = []
    audits: list[int] = []

    async def later() -> None:
        await released.wait()
        with contextlib.suppress(Exception):
            await other.call_tool("audit", {})

    async def refuse() -> Any:
        tasks.append(asyncio.ensure_future(later()))
        return own_error("refuse says no")

    def audit() -> str:
        audits.append(1)
        raise ValueError("audit failed")

    tool(child, refuse, **no_output())
    tool(other, audit)

    class Releases(Middleware):
        async def on_call_tool(self, context: Any, call_next: Any) -> Any:
            result = await call_next(context)
            if context.message.name == "sub_refuse":
                released.set()
                await tasks[0]
            return result

    parent.add_middleware(Releases())
    parent.mount(child, namespace="sub")
    instrument(parent, lsquare)
    instrument(other, lsquare)
    async with connect(other) as connection:
        await connection.call("audit", {})
    async with connect(parent) as connection:
        reply = await connection.call("sub_refuse", {})
    await lsquare.aflush(timeout_ms=5_000)
    assert tasks[0].done() and len(audits) == 2
    # (A tool of a server whose tools can't all be read gets no ID line.)
    assert reply.is_error and reply.texts == ["refuse says no"]
    event = events.calls()[-1]
    assert (event["toolName"], event["error"]) == ("sub_refuse", {"message": "refuse says no", "category": "tool_error"})


@only("mcp2", "fastmcp4")
@pytest.mark.parametrize("target", ["other", "same"])
async def test_a_task_the_tool_left_running_released_by_mcp2_middleware_records_nothing_on_the_call(target: str) -> None:
    # The task inherited the handler's step too: outside it, its tool isn't the call's.
    lsquare, events = client()
    released = asyncio.Event()
    tasks: list[asyncio.Task[None]] = []
    audits: list[int] = []
    holder: dict[str, Any] = {}

    async def later() -> None:
        await released.wait()
        with contextlib.suppress(Exception):
            await holder["nested"].call_tool("audit", {})

    async def refuse() -> Any:
        tasks.append(asyncio.ensure_future(later()))
        return own_error("refuse says no")

    def audit() -> str:
        audits.append(1)
        raise ValueError("audit failed")

    async def releasing(ctx: Any, call_next: Any) -> Any:
        result = await call_next(ctx)
        if ctx.method == "tools/call" and name_of(ctx) == "refuse":
            released.set()
            await tasks[0]
        return result

    if FAST:
        app = high()
        app._mcp_server.middleware.append(releasing)
    else:
        from mcp.server.mcpserver import MCPServer

        app = MCPServer("test", middleware=[releasing])
    other = high("other")
    holder["nested"] = other if target == "other" else app
    tool(app, refuse, **no_output())
    tool(app, audit)
    tool(other, audit)
    instrument(app, lsquare)
    instrument(other, lsquare)
    async with connect(other) as connection:
        await connection.call("audit", {})
    async with connect(app) as connection:
        await connection.call("audit", {})
        reply = await connection.call("refuse", {})
    await lsquare.aflush(timeout_ms=5_000)
    assert tasks[0].done() and len(audits) == 3
    assert reply.is_error and reply.texts[0] == "refuse says no" and ID_LINE.match(reply.texts[-1])
    event = events.calls()[-1]
    assert event["toolName"] == "refuse"
    assert event["error"] == {"message": "refuse says no", "category": "tool_error"} and event["sessionSource"] == "issued"


@only("mcp2", "fastmcp4")
async def test_a_task_a_low_level_handler_left_running_records_nothing_on_the_call_after_its_step() -> None:
    # On a low-level server the handler is the tool, unwatched: the task holds its handler's step,
    # which ended with it. The other server is mcp 2.x's own, on either line.
    from mcp.server.mcpserver import MCPServer

    from lines import low

    lsquare, events = client()
    released = asyncio.Event()
    tasks: list[asyncio.Task[None]] = []
    audits: list[int] = []
    other = MCPServer("other")

    async def later() -> None:
        await released.wait()
        with contextlib.suppress(Exception):
            await other.call_tool("audit", {})

    async def answer(name: str, arguments: dict[str, Any]) -> Any:
        tasks.append(asyncio.ensure_future(later()))
        return {"content": [{"type": "text", "text": "handler says no"}], "isError": True}

    def audit() -> str:
        audits.append(1)
        raise ValueError("audit failed")

    server = low("low", [{"name": "refuse", "inputSchema": {"type": "object"}}], answer)

    async def releasing(ctx: Any, call_next: Any) -> Any:
        result = await call_next(ctx)
        if ctx.method == "tools/call":
            released.set()
            await tasks[0]
        return result

    server.middleware.append(releasing)
    other.tool()(audit)
    instrument(server, lsquare)
    instrument(other, lsquare)
    async with connect(other) as connection:
        await connection.call("audit", {})
    async with connect(server) as connection:
        reply = await connection.call("refuse", {})
    await lsquare.aflush(timeout_ms=5_000)
    assert tasks[0].done() and len(audits) == 2 and reply.is_error and reply.texts[0] == "handler says no"
    event = events.calls()[-1]
    assert (event["toolName"], event["error"]) == ("refuse", {"message": "handler says no", "category": "tool_error"})


@pytest.mark.parametrize("target", ["other", "same"])
async def test_a_thread_the_tool_left_running_calling_a_tool_after_it_returned_records_nothing_on_the_call(target: str) -> None:
    # On every line, a thread can run between the tool's return and the call's end (FastMCP 1.x has
    # no middleware, so nothing the app awaits can): here one started with the tool's context runs
    # another tool through a server while the SDK converts the tool's result, inside the call's
    # core step on fastmcp.
    lsquare, events = client()
    app, other = high(), high("other")
    nested = other if target == "other" else app
    release = threading.Event()
    threads: list[threading.Thread] = []
    audits: list[int] = []

    def later() -> None:
        assert release.wait(10)
        with contextlib.suppress(Exception):
            asyncio.run(nested.call_tool("audit", {}))

    class Answer(list):  # type: ignore[type-arg]
        def __iter__(self) -> Any:
            # Converting the result: the tool has returned, and the call goes on.
            release.set()
            threads[0].join(10)
            return super().__iter__()

    def refuse():  # type: ignore[no-untyped-def]
        context = contextvars.copy_context()
        threads.append(threading.Thread(target=context.run, args=(later,)))
        threads[0].start()
        return Answer(["refused"])

    def audit() -> str:
        audits.append(1)
        raise ValueError("audit failed")

    tool(app, refuse)
    tool(app, audit)
    tool(other, audit)
    instrument(app, lsquare)
    instrument(other, lsquare)
    async with connect(other) as connection:
        await connection.call("audit", {})
    async with connect(app) as connection:
        await connection.call("audit", {})
        reply = await connection.call("refuse", {})
    await lsquare.aflush(timeout_ms=5_000)
    assert not threads[0].is_alive() and len(audits) == 3
    # fastmcp writes a list of plain values as one JSON text.
    assert not reply.is_error and reply.texts[0] in ("refused", '["refused"]') and ID_LINE.match(reply.texts[-1])
    event = events.calls()[-1]
    assert event["toolName"] == "refuse" and "error" not in event and event["sessionSource"] == "issued"


@only("fastmcp3", "fastmcp4")
async def test_a_middleware_running_call_next_in_a_task_of_its_own_still_runs_the_calls_tool() -> None:
    # A guard: a task made inside the call's chain step, while that step runs (asyncio.wait_for
    # makes one, before 3.12), runs its core step.
    from fastmcp.server.middleware import Middleware

    lsquare, events = client()
    app = high()

    def refuse() -> Any:
        return own_error("refuse says no")

    class InATask(Middleware):
        async def on_call_tool(self, context: Any, call_next: Any) -> Any:
            return await asyncio.ensure_future(call_next(context))

    tool(app, refuse, **no_output())
    app.add_middleware(InATask())
    instrument(app, lsquare)
    async with connect(app) as connection:
        reply = await connection.call("refuse", {})
    await lsquare.aflush(timeout_ms=5_000)
    assert reply.is_error and reply.texts[0] == "refuse says no" and ID_LINE.match(reply.texts[-1])
    [event] = events.calls()
    assert event["error"] == {"message": "refuse says no", "category": "tool_error"} and event["sessionSource"] == "issued"


@only("mcp2", "fastmcp4")
async def test_an_mcp2_middleware_running_call_next_in_a_task_of_its_own_still_runs_the_calls_tool() -> None:
    # A guard, as above, for the handler's step.
    lsquare, events = client()

    async def in_a_task(ctx: Any, call_next: Any) -> Any:
        return await asyncio.ensure_future(call_next(ctx))

    if FAST:
        app = high()
        app._mcp_server.middleware.append(in_a_task)
    else:
        from mcp.server.mcpserver import MCPServer

        app = MCPServer("test", middleware=[in_a_task])

    def refuse() -> Any:
        return own_error("refuse says no")

    tool(app, refuse, **no_output())
    instrument(app, lsquare)
    async with connect(app) as connection:
        reply = await connection.call("refuse", {})
    await lsquare.aflush(timeout_ms=5_000)
    assert reply.is_error and reply.texts[0] == "refuse says no" and ID_LINE.match(reply.texts[-1])
    [event] = events.calls()
    assert event["error"] == {"message": "refuse says no", "category": "tool_error"} and event["sessionSource"] == "issued"


# -- Review 7: fastmcp 4.x's extension interceptors ---------------------------------------------------


def _interceptor(around: Callable[[Any, Any], Any]) -> Any:
    """A fastmcp 4.x extension whose `intercept_tool_call` is `around(params, call_next)`."""
    from fastmcp.server.extensions import ServerExtension

    class Audits(ServerExtension):
        identifier = "io.lsquare.test/audits"

        async def intercept_tool_call(self, params: Any, context: Any, call_next: Any) -> Any:
            return await around(params, call_next)

    return Audits()


@only("fastmcp4")
@pytest.mark.parametrize("audit_raises", [False, True])
@pytest.mark.parametrize("when", ["before", "after"])
async def test_an_extensions_own_call_tool_without_middleware_isnt_the_calls_core_step(when: str, audit_raises: bool) -> None:
    # The interceptors sit between fastmcp's middleware and the tool: the call_next fastmcp's
    # middleware ends with runs them, then the core step. Only that last one is the core step.
    lsquare, events = client()
    app = high()

    def refuse() -> Any:
        return own_error("refuse says no")

    def audit() -> str:
        if audit_raises:
            raise ValueError("audit failed")
        return "audited"

    async def around(params: Any, call_next: Any) -> Any:
        auditing = params.name == "refuse"
        if auditing and when == "before":
            with contextlib.suppress(Exception):
                await app.call_tool("audit", {}, run_middleware=False)
        result = await call_next()
        if auditing and when == "after":
            with contextlib.suppress(Exception):
                await app.call_tool("audit", {}, run_middleware=False)
        return result

    tool(app, refuse, **no_output())
    tool(app, audit)
    app.add_extension(_interceptor(around))
    instrument(app, lsquare)
    async with connect(app) as connection:
        # The audit's own call taps it.
        await connection.call("audit", {})
        reply = await connection.call("refuse", {})
    await lsquare.aflush(timeout_ms=5_000)
    assert reply.is_error and reply.texts[0] == "refuse says no" and ID_LINE.match(reply.texts[-1])
    event = events.calls()[-1]
    assert event["error"] == {"message": "refuse says no", "category": "tool_error"} and event["sessionSource"] == "issued"


@only("fastmcp4")
@pytest.mark.parametrize("order", ["raises_then_errs", "errs_then_raises"])
async def test_an_extension_retrying_the_tool_records_its_last_core_step(order: str) -> None:
    # A guard: each call of the interceptor's continuation is a core step of its own.
    lsquare, events = client()
    app = high()
    attempts: list[int] = []

    def flaky() -> Any:
        attempts.append(1)
        if (len(attempts) == 1) == (order == "raises_then_errs"):
            raise ValueError("the attempt raised")
        return own_error("the attempt says no")

    async def around(params: Any, call_next: Any) -> Any:
        try:
            await call_next()
        except Exception:
            pass
        return await call_next()

    tool(app, flaky, **no_output())
    app.add_extension(_interceptor(around))
    instrument(app, lsquare)
    async with connect(app) as connection:
        reply = await connection.call("flaky", {})
    await lsquare.aflush(timeout_ms=5_000)
    assert len(attempts) == 2 and reply.is_error
    [event] = events.calls()
    if order == "raises_then_errs":
        assert reply.texts[0] == "the attempt says no" and ID_LINE.match(reply.texts[-1])
        assert event["error"] == {"message": "the attempt says no", "category": "tool_error"}
    else:
        assert event["error"]["category"] == "thrown" and event["error"]["message"] == "the attempt raised"


__all__ = ["LINE"]
