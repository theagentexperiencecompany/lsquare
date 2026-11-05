"""What decides the ID line and which tools get the extras, where middleware, transforms and
providers can change what a call runs and returns: the identity checks, fastmcp's known tool set,
and a tools/call handler replaced after instrument()."""

from __future__ import annotations

import re
from typing import Any

import pytest

from lines import FAST, MAJOR, client, connect, high, needs_line, only, schema, tool
from lsquare.mcp import instrument

pytestmark = [needs_line, pytest.mark.anyio]
ID_LINE = re.compile(r"^Task ID: ses_[A-Za-z0-9_-]{22}\. ")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def search(query: str) -> str:
    return f"found {query}"


def has_id_line(texts: list[str]) -> bool:
    return bool(texts) and ID_LINE.match(texts[-1]) is not None


def properties(listed: Any) -> list[str]:
    return sorted(schema(listed).get("properties") or {})


async def first_reply(app: Any) -> list[str]:
    async with connect(app) as connection:
        return (await connection.call("search", {"query": "a"})).texts


@only("mcp2")
async def test_mcp2_a_middleware_that_returns_another_result_or_replaces_its_content_costs_the_id_line() -> None:
    async def passing(ctx: Any, call_next: Any) -> Any:
        return await call_next(ctx)

    async def other(ctx: Any, call_next: Any) -> Any:
        result = await call_next(ctx)
        return {**result} if ctx.method == "tools/call" else result

    async def content(ctx: Any, call_next: Any) -> Any:
        result = await call_next(ctx)
        if ctx.method == "tools/call":
            result["content"] = [{"type": "text", "text": "cached"}]
        return result

    for middleware, expected in ((passing, True), (other, False), (content, False)):
        lsquare, _ = client()
        app = high()
        tool(app, search)
        app._lowlevel_server.middleware.append(middleware)
        instrument(app, lsquare)
        assert has_id_line(await first_reply(app)) is expected, middleware.__name__


@only("mcp2", "fastmcp4")
async def test_middleware_appended_after_instrument_costs_that_call_its_id_line_only() -> None:
    lsquare, _ = client()
    app = high()
    tool(app, search)
    instrument(app, lsquare)
    low = app._lowlevel_server if not FAST else app._mcp_server

    async def later(ctx: Any, call_next: Any) -> Any:
        return await call_next(ctx)

    low.middleware.append(later)
    async with connect(app) as connection:
        first = await connection.call("search", {"query": "a"})
        second = await connection.call("search", {"query": "b"})
    assert not has_id_line(first.texts)
    assert has_id_line(second.texts)


@only("fastmcp3", "fastmcp4")
async def test_fastmcp_a_middleware_that_returns_another_tool_result_costs_the_id_line() -> None:
    from fastmcp.server.middleware import Middleware
    from fastmcp.tools.base import ToolResult

    class Passing(Middleware):
        async def on_call_tool(self, context: Any, call_next: Any) -> Any:
            return await call_next(context)

    class Other(Middleware):
        async def on_call_tool(self, context: Any, call_next: Any) -> Any:
            result = await call_next(context)
            return ToolResult(content=result.content, structured_content=result.structured_content)

    for middleware, expected in ((Passing(), True), (Other(), False)):
        lsquare, _ = client()
        app = high()
        tool(app, search)
        app.add_middleware(middleware)
        instrument(app, lsquare)
        assert has_id_line(await first_reply(app)) is expected, type(middleware).__name__


@only("fastmcp3", "fastmcp4")
async def test_fastmcp_tools_get_the_extras_only_while_the_tool_set_is_known() -> None:
    from fastmcp.server.middleware.tool_injection import ToolInjectionMiddleware
    from fastmcp.server.transforms import Transform
    from fastmcp.server.transforms.visibility import Visibility
    from fastmcp.tools import Tool

    class Narrower(Visibility):
        pass

    class Passthrough(Transform):
        """Changes nothing, but could: the wrapper can't tell."""

    def provider_transform(app: Any) -> None:
        app._local_provider.add_transform(Passthrough())

    def visibility_subclass(app: Any) -> None:
        app._transforms.append(Narrower(False, names={"nothing"}))

    def injected(app: Any) -> None:
        def search(query: str) -> str:
            return "injected"

        app.add_middleware(ToolInjectionMiddleware(tools=[Tool.from_function(search)]))

    def versions(app: Any) -> None:
        def search(query: str, intent: str = "") -> str:
            return "v2"

        app.tool(search, version="2")

    for change in (provider_transform, visibility_subclass, injected, versions):
        lsquare, _ = client()
        app = high()
        app.tool(search, version="1") if change is versions else tool(app, search)
        change(app)
        instrument(app, lsquare)
        async with connect(app) as connection:
            listed = {item.name: item for item in await connection.tools()}
        # Nothing added (version 2 declares intent itself).
        assert "session_id" not in properties(listed["search"]), change.__name__
        # An unknown set gets no get_more_tools either; an injected name or disagreeing versions
        # leave only that tool alone.
        assert ("get_more_tools" in listed) == (change in (injected, versions)), change.__name__

    # fastmcp's own disable() adds a plain Visibility: the tool set stays known.
    lsquare, _ = client()
    app = high()
    tool(app, search)

    def hidden(query: str) -> str:
        return "hidden"

    tool(app, hidden)
    app.disable(names={"hidden"})
    instrument(app, lsquare)
    async with connect(app) as connection:
        listed = {item.name: item for item in await connection.tools()}
    assert "hidden" not in listed and "intent" in properties(listed["search"])


@only("fastmcp3", "fastmcp4")
async def test_fastmcp_a_name_that_is_a_str_subclass_counts_while_it_compares_and_hashes_as_a_str() -> None:
    from fastmcp.tools import Tool

    class Name(str):
        """Compares and hashes as the string it spells."""

    class Hashed(str):
        """Hashes its own way: an index by name would split what fastmcp finds by equality."""

        def __hash__(self) -> int:
            return str.__hash__(self) ^ 1

    def other(query: str) -> str:
        return f"other {query}"

    # pydantic's model_copy doesn't validate: the registration keeps the subclass.
    lsquare, _ = client()
    app = high()
    tool(app, other)
    app.add_tool(Tool.from_function(search, name="search").model_copy(update={"name": Name("search")}))
    instrument(app, lsquare)
    async with connect(app) as connection:
        listed = {item.name: item for item in await connection.tools()}
        reply = await connection.call("search", {"query": "a", "intent": "why"})
    assert "intent" in properties(listed["search"]) and "get_more_tools" in listed
    assert reply.texts[0] == "found a"

    # fastmcp runs version 2, which declares intent. An index by name would find version 1 alone,
    # which doesn't, and take version 2's input out: the tool set isn't known, so no tool gets
    # the extras.
    def first(query: str) -> str:
        return f"found {query}"

    def second(query: str, intent: str = "") -> str:
        return f"{query}|{intent}"

    lsquare, _ = client()
    app = high()
    tool(app, other)
    tool(app, first, name="search", version="1")
    tool(app, second, name="search", version="2")
    components = app._local_provider._components
    for key, component in list(components.items()):
        if str(component.version) == "2":
            components[key] = component.model_copy(update={"name": Hashed("search")})
    instrument(app, lsquare)
    async with connect(app) as connection:
        listed = {item.name: item for item in await connection.tools()}
        reply = await connection.call("search", {"query": "q", "intent": "mine"})
    assert reply.texts[0] == "q|mine"
    assert "session_id" not in properties(listed["other"]) and "get_more_tools" not in listed


@only("fastmcp3", "fastmcp4")
async def test_fastmcp_a_call_taps_its_tool_while_the_tool_set_isnt_known() -> None:
    from fastmcp.server.transforms import Transform

    from lsquare.mcp._boundary import TAPPED

    class Passthrough(Transform):
        """Changes nothing, but could: the wrapper can't tell."""

    class Hashed(str):
        def __hash__(self) -> int:
            return str.__hash__(self) ^ 1

    def transformed(app: Any) -> None:
        app._local_provider.add_transform(Passthrough())

    def hashed(app: Any) -> None:
        components = app._local_provider._components
        for key, component in list(components.items()):
            components[key] = component.model_copy(update={"name": Hashed(component.name)})

    for change in (transformed, hashed):
        lsquare, _ = client()
        app = high()
        tool(app, search)
        change(app)
        instrument(app, lsquare)
        async with connect(app) as connection:
            reply = await connection.call("search", {"query": "a"})
        # Its own function is still watched, as every local registration of its name is.
        [registered] = app._local_provider._components.values()
        assert reply.texts[0] == "found a" and getattr(registered.fn, TAPPED, False) is True, change.__name__


async def test_a_tool_redefined_between_listings_is_read_again() -> None:
    lsquare, _ = client()
    app = high()
    tool(app, search)
    instrument(app, lsquare)

    def redefined(query: str, intent: str = "") -> str:
        return f"{query}|{intent}"

    async with connect(app) as connection:
        first = {item.name: item for item in await connection.tools()}
        if FAST:
            app._local_provider.remove_tool("search")
        else:
            del app._tool_manager._tools["search"]
        tool(app, redefined, name="search")
        second = {item.name: item for item in await connection.tools()}
        reply = await connection.call("search", {"query": "q", "intent": "mine"})
    # Nothing read for the first listing is kept: the new definition's intent is its own, listed
    # as it declares it and passed to it.
    assert schema(second["search"])["properties"]["intent"] != schema(first["search"])["properties"]["intent"]
    assert reply.texts[0] == "q|mine"


async def test_a_tool_the_apps_inputs_function_replaces_is_the_one_watched() -> None:
    lsquare, events = client()
    app = high()
    tool(app, search)

    def broken(query: str) -> str:
        raise ValueError("it broke")

    replaced: list[str] = []

    def inputs(name: str) -> bool:
        # The app's own code, run as the call arrives: the call runs the tool it puts in place.
        if not replaced:
            replaced.append(name)
            if FAST:
                app._local_provider.remove_tool("search")
            else:
                del app._tool_manager._tools["search"]
            tool(app, broken, name="search")
        return True

    instrument(app, lsquare, inputs=inputs)
    async with connect(app) as connection:
        reply = await connection.call("search", {"query": "a"})
    await lsquare.aflush(timeout_ms=5_000)
    [event] = events.calls()
    # Its raise is the tool's own, not the SDK refusing the call: the replacement is the one watched.
    assert reply.is_error and replaced == ["search"]
    assert event["error"]["category"] == "thrown" and event["error"]["type"] == "ValueError"


async def test_a_tools_call_handler_replaced_after_instrument_reads_the_server_as_low_level() -> None:
    lsquare, events = client()
    app = high()

    # No output schema: the replacement answers with content only.
    def search(query):  # type: ignore[no-untyped-def]
        return f"found {query}"

    tool(app, search)
    instrument(app, lsquare)
    seen: list[dict[str, Any]] = []

    if MAJOR == 1:
        import mcp.types as types

        low = app._mcp_server

        @low.call_tool(validate_input=False)
        async def replacement(name: str, arguments: dict[str, Any]) -> Any:
            seen.append(arguments)
            return [types.TextContent(type="text", text="replaced")]

    else:
        import mcp_types

        low = app._mcp_server if FAST else app._lowlevel_server

        async def handler(ctx: Any, params: Any) -> Any:
            seen.append(dict(params.arguments or {}))
            return mcp_types.CallToolResult(content=[mcp_types.TextContent(type="text", text="replaced")])

        low.add_request_handler("tools/call", mcp_types.CallToolRequestParams, handler)

    async with connect(app) as connection:
        listed = {item.name: item for item in await connection.tools()}
        reply = await connection.call("search", {"query": "a", "intent": "why"})
    await lsquare.aflush(timeout_ms=5_000)
    assert reply.texts == ["replaced"]
    assert seen == [{"query": "a", "intent": "why"}]
    assert "intent" not in properties(listed["search"]) and "get_more_tools" not in listed
    [event] = events.calls()
    assert event["input"] == {"query": "a", "intent": "why"} and "intent" not in event
