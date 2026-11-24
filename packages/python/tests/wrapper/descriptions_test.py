"""The tool's description on each call's event: a registered tool's own on a high-level server,
the one its instance last listed on a low-level one, the `descriptions` option, and what never
has one."""

from __future__ import annotations

from typing import Any, cast

import pytest

from helpers import Recorder
from lines import FAST, MAJOR, client, connect, high, low, needs_line, only, tool
from lsquare import LSquare
from lsquare.mcp import INSTRUMENTED, instrument

pytestmark = [needs_line, pytest.mark.anyio]
OFF = {"sessions": False, "intent": False, "missing_tools": False}
ALL = {"sessions": True, "intent": True, "missing_tools": True}
PLAIN = {"type": "object", "properties": {"id": {"type": "string"}}}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def flush(lsquare: LSquare) -> None:
    await lsquare.aflush(timeout_ms=5_000)


def search(query: str) -> str:
    return f"found {query}"


def lookup(order_id: str) -> str:
    """Look an order up by its ID."""
    return f"order {order_id}"


def bare(query: str) -> str:
    return query


def described(events: Any) -> list[tuple[str, str | None]]:
    return [(event["toolName"], event.get("toolDescription")) for event in events.calls()]


def connected(server: Any) -> Any:
    # mcp 2.x: the legacy era, where a low-level server's extras apply.
    return connect(server, mode="legacy") if MAJOR == 2 else connect(server)


async def ok(name: str, arguments: dict[str, Any]) -> Any:
    return [{"type": "text", "text": f"ok {name}"}]


# A high-level server: the registered tool's own.


async def test_a_high_level_servers_call_carries_its_tools_registered_description_before_any_list() -> None:
    lsquare, events = client()
    app = high("orders")
    tool(app, search, description="Search the orders by a query.")
    tool(app, lookup)
    tool(app, bare)
    instrument(app, lsquare, extras=OFF)
    async with connect(app) as connection:
        found = await connection.call("search", {"query": "shoes"})
        await connection.call("lookup", {"order_id": "7"})
        await connection.call("bare", {"query": "x"})
        await connection.call("nope", {})
    await flush(lsquare)
    assert found.texts == ["found shoes"]
    assert described(events) == [
        ("search", "Search the orders by a query."),
        # A docstring is the description, as the SDK lists it.
        ("lookup", "Look an order up by its ID."),
        # Neither: none.
        ("bare", None),
        # Nor a tool the server doesn't have.
        ("nope", None),
    ]


async def test_the_extras_change_nothing_of_it_and_get_more_tools_has_none() -> None:
    lsquare, events = client()
    app = high()
    tool(app, search, description="Search the orders by a query.")
    instrument(app, lsquare)
    async with connect(app) as connection:
        listed = {item.name: item for item in await connection.tools()}
        await connection.call("search", {"query": "shoes", "intent": "find shoes"})
        noted = await connection.call("get_more_tools", {"need": "issue refunds"})
    await flush(lsquare)
    # The listing is the developer's own, as before.
    assert listed["search"].description == "Search the orders by a query."
    assert noted.texts[0].startswith("Noted.")
    first, second = events.calls()
    assert first["toolDescription"] == "Search the orders by a query." and first["intent"] == "find shoes"
    assert second["extra"] == "missing_tools" and "toolDescription" not in second


async def test_a_description_longer_than_1000_characters_is_cut_and_one_with_an_email_is_redacted() -> None:
    lsquare, events = client()
    app = high()
    tool(app, search, description="lorem ipsum " * 100)
    tool(app, bare, description="Ask jane@acme.io before you use this.")
    instrument(app, lsquare, extras=OFF)
    async with connect(app) as connection:
        await connection.call("search", {"query": "shoes"})
        await connection.call("bare", {"query": "x"})
    await flush(lsquare)
    first, second = events.calls()
    assert first["toolDescription"] == ("lorem ipsum " * 100)[:1_000] + "…[+200 chars]"
    assert first["truncated"] == ["toolDescription"]
    assert second["toolDescription"] == "Ask [EMAIL_1] before you use this."


# The option.


async def test_descriptions_false_leaves_it_out_of_every_call() -> None:
    lsquare, events = client()
    app = high()
    tool(app, search, description="Search the orders by a query.")
    instrument(app, lsquare, extras=OFF, descriptions=False)
    async with connect(app) as connection:
        await connection.tools()
        await connection.call("search", {"query": "shoes"})
    await flush(lsquare)
    assert described(events) == [("search", None)]


async def test_a_descriptions_function_is_asked_once_a_call_and_decides_for_each_tool() -> None:
    lsquare, events = client()
    app = high()
    tool(app, search, description="Search the orders by a query.")
    tool(app, lookup)
    asked: list[str] = []

    def descriptions(name: str) -> bool:
        asked.append(name)
        return name != "search"

    instrument(app, lsquare, extras=OFF, descriptions=descriptions)
    async with connect(app) as connection:
        await connection.call("search", {"query": "shoes"})
        await connection.call("lookup", {"order_id": "7"})
    await flush(lsquare)
    assert asked == ["search", "lookup"]
    assert described(events) == [("search", None), ("lookup", "Look an order up by its ID.")]


async def test_a_descriptions_function_that_raises_or_is_async_records_none_and_says_so_once() -> None:
    recorder = Recorder()
    lsquare, events = client(logger=recorder, on_error=recorder.on_error)
    app = high()
    tool(app, search, description="Search the orders by a query.")
    tool(app, lookup)

    def raises(name: str) -> bool:
        raise RuntimeError("no policy")

    instrument(app, lsquare, extras=OFF, descriptions=raises)

    other, others = client(logger=recorder)
    second = high()
    tool(second, search, description="Search the orders by a query.")

    async def later(name: str) -> bool:
        return True

    instrument(second, other, extras=OFF, descriptions=cast(Any, later))
    async with connect(app) as connection:
        found = await connection.call("search", {"query": "shoes"})
        await connection.call("lookup", {"order_id": "7"})
    async with connect(second) as connection:
        await connection.call("search", {"query": "hats"})
    await flush(lsquare)
    await flush(other)
    assert found.texts == ["found shoes"]
    assert described(events) == [("search", None), ("lookup", None)]
    assert described(others) == [("search", None)]
    # The calls are recorded all the same, with their inputs.
    assert [event["input"] for event in events.calls()] == [{"query": "shoes"}, {"order_id": "7"}]
    said = [line for _, line in recorder.lines if "The descriptions function raised or returned an awaitable" in line]
    assert said == ["[lsquare] The descriptions function raised or returned an awaitable: descriptions aren't recorded for the calls it answered that way."] * 2


# A low-level server: as its instance last listed it.


async def test_a_low_level_servers_call_carries_the_description_its_instance_last_listed() -> None:
    lsquare, events = client()
    listing: list[dict[str, Any]] = [
        {"name": "orders", "description": "Look orders up.", "inputSchema": PLAIN},
        {"name": "bare", "inputSchema": PLAIN},
    ]
    server = low("plain", listing, ok)
    instrument(server, lsquare)
    async with connect(server) as connection:
        # Before any list: the instance has shown no one its tools yet.
        await connection.call("orders", {"id": "1"})
        listed = {item.name: item for item in await connection.tools()}
        await connection.call("orders", {"id": "2"})
        await connection.call("bare", {"id": "3"})
        # Changed, but not listed again yet: agents still have the one before.
        listing[0] = {"name": "orders", "description": "Look orders up by their ID.", "inputSchema": PLAIN}
        await connection.call("orders", {"id": "4"})
        await connection.tools()
        await connection.call("orders", {"id": "5"})
        # Listed without one: it has none from then on.
        listing[0] = {"name": "orders", "inputSchema": PLAIN}
        await connection.tools()
        await connection.call("orders", {"id": "6"})
    await flush(lsquare)
    # The listing goes to the client as it came.
    assert listed["orders"].description == "Look orders up." and listed["bare"].description is None
    assert described(events) == [
        ("orders", None),
        ("orders", "Look orders up."),
        ("bare", None),
        ("orders", "Look orders up."),
        ("orders", "Look orders up by their ID."),
        ("orders", None),
    ]


async def test_a_low_level_server_with_descriptions_off_has_none_listed_or_not() -> None:
    lsquare, events = client()
    server = low("plain", [{"name": "orders", "description": "Look orders up.", "inputSchema": PLAIN}], ok)
    instrument(server, lsquare, descriptions=False)
    async with connect(server) as connection:
        await connection.tools()
        await connection.call("orders", {"id": "1"})
    await flush(lsquare)
    assert described(events) == [("orders", None)]


async def test_with_the_extras_on_the_wrappers_own_read_of_the_list_notes_them_before_the_first_call() -> None:
    lsquare, events = client()
    server = low("plain", [{"name": "orders", "description": "Look orders up.", "inputSchema": PLAIN}], ok)
    instrument(server, lsquare, extras=ALL)
    async with connected(server) as connection:
        # No client has listed: the call waits for the wrapper's read, which lists every page.
        await connection.call("orders", {"id": "1", "intent": "check an order"})
    await flush(lsquare)
    [event] = events.calls()
    assert event["toolDescription"] == "Look orders up." and event["intent"] == "check an order"


async def test_another_list_handler_forgets_what_the_one_before_listed() -> None:
    lsquare, events = client()
    server = low("plain", [{"name": "orders", "description": "Look orders up.", "inputSchema": PLAIN}], ok)
    instrument(server, lsquare)
    replaced = [{"name": "orders", "description": "Find orders.", "inputSchema": PLAIN}]
    async with connect(server) as connection:
        await connection.tools()
        await connection.call("orders", {"id": "1"})
        if MAJOR == 1:
            import mcp.types as types

            async def other() -> list[Any]:
                return [types.Tool.model_validate(each) for each in replaced]

            server.list_tools()(other)
        else:
            import mcp_types

            async def another(ctx: Any, params: Any) -> Any:
                return mcp_types.ListToolsResult.model_validate({"tools": replaced})

            server.add_request_handler("tools/list", server._request_handlers["tools/list"].params_type, another)
        # Not listed by the new handler yet: what the old one said isn't its tool's.
        await connection.call("orders", {"id": "2"})
        await connection.tools()
        await connection.call("orders", {"id": "3"})
    await flush(lsquare)
    assert described(events) == [("orders", "Look orders up."), ("orders", None), ("orders", "Find orders.")]


@only("mcp1", "fastmcp3")
async def test_mcp1_the_sdks_own_listing_during_an_instances_first_call_counts() -> None:
    lsquare, events = client()
    during: list[dict[str, str]] = []

    async def call(name: str, arguments: dict[str, Any]) -> Any:
        # mcp 1.x listed the tools itself to check the arguments, before this ran: no client has.
        during.append(dict(getattr(server, INSTRUMENTED).lists.described))
        return [{"type": "text", "text": "ok"}]

    server = low("plain", [{"name": "orders", "description": "Look orders up.", "inputSchema": PLAIN}], call)
    instrument(server, lsquare)
    async with connect(server) as connection:
        await connection.call("orders", {"id": "1"})
        await connection.call("orders", {"id": "2"})
    await flush(lsquare)
    assert during[0] == {"orders": "Look orders up."}
    # Read as the call arrives: the first one came before that listing.
    assert described(events) == [("orders", None), ("orders", "Look orders up.")]


# fastmcp: tools the registry can't speak for.


@only("fastmcp3", "fastmcp4")
async def test_fastmcp_a_mounted_servers_tool_has_the_description_it_was_last_listed_with() -> None:
    assert FAST
    lsquare, events = client()
    app = high("main")
    tool(app, search, description="Search the orders by a query.")
    inner = high("inner")
    tool(inner, lookup)
    app.mount(inner, namespace="inner")
    instrument(app, lsquare, extras=OFF)
    async with connect(app) as connection:
        # Another provider: the registry no longer says which tool a name runs, the server's own
        # included. Before any list, the instance has shown no one its tools.
        await connection.call("inner_lookup", {"order_id": "7"})
        names = await connection.names()
        await connection.call("search", {"query": "hats"})
        again = await connection.call("inner_lookup", {"order_id": "8"})
    await flush(lsquare)
    assert sorted(names) == ["inner_lookup", "search"] and again.texts == ["order 8"]
    assert described(events) == [
        ("inner_lookup", None),
        ("search", "Search the orders by a query."),
        ("inner_lookup", "Look an order up by its ID."),
    ]
