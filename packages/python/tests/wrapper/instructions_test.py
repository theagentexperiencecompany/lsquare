"""The server's instructions on each call's event: what the server gives clients in `initialize`,
read from the instance as the call arrives, the `instructions` option, and what never has them."""

from __future__ import annotations

from typing import Any

import pytest

from helpers import Recorder
from lines import FAST, MAJOR, client, connect, high, low, needs_line, only, tool
from lsquare import LSquare
from lsquare.mcp import IdentifyInput, instrument

pytestmark = [needs_line, pytest.mark.anyio]
OFF = {"sessions": False, "intent": False, "missing_tools": False}
PLAIN = {"type": "object", "properties": {"id": {"type": "string"}}}
INSTRUCTIONS = "Search the orders before you refund one. Never refund an order twice."


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def flush(lsquare: LSquare) -> None:
    await lsquare.aflush(timeout_ms=5_000)


def search(query: str) -> str:
    """Search the orders by a query."""
    return f"found {query}"


async def ok(name: str, arguments: dict[str, Any]) -> Any:
    return [{"type": "text", "text": f"ok {name}"}]


def instructed(events: Any) -> list[tuple[str, str | None]]:
    return [(event["toolName"], event.get("serverInstructions")) for event in events.calls()]


def lowlevel(app: Any) -> Any:
    """The low-level server every class answers `initialize` from: where the instructions are."""
    for name in ("_mcp_server", "_lowlevel_server"):
        found = getattr(app, name, None)
        if found is not None:
            return found
    return app


async def given(connection: Any) -> str | None:
    """The instructions the client's own handshake, or its discovery of the server, gave it."""
    session = connection.session
    found: str | None
    if MAJOR == 2:
        # The official client's session keeps them from either; fastmcp's client wraps one.
        found = (session.session if type(session).__module__.startswith("fastmcp") else session).instructions
    elif type(session).__module__.startswith("fastmcp"):
        found = session.initialize_result.instructions
    else:
        # mcp 1.x's client keeps none of the reply: asked again, the session answers as it did.
        found = (await session.initialize()).instructions
    return found


# What every server's calls carry.


async def test_a_high_level_servers_calls_carry_the_instructions_its_clients_are_given() -> None:
    lsquare, events = client()
    app = high("orders", instructions=INSTRUCTIONS)
    tool(app, search)
    instrument(app, lsquare, extras=OFF)
    async with connect(app) as connection:
        told = await given(connection)
        found = await connection.call("search", {"query": "shoes"})
        await connection.call("nope", {})
    await flush(lsquare)
    assert found.texts == ["found shoes"]
    # The handshake is the server's own, as before, and the events say what it said.
    assert told == INSTRUCTIONS
    # A call of a tool the server doesn't have was made under them too.
    assert instructed(events) == [("search", INSTRUCTIONS), ("nope", INSTRUCTIONS)]
    first = events.calls()[0]
    assert "truncated" not in first and "dropped" not in first and "redactions" not in first
    # Right after the tool's description, as TypeScript writes it.
    keys = list(first)
    assert keys[keys.index("toolDescription") : keys.index("toolDescription") + 2] == ["toolDescription", "serverInstructions"]


async def test_a_low_level_servers_calls_carry_them_from_the_first_before_any_listing() -> None:
    lsquare, events = client()
    server = low("plain", [{"name": "orders", "description": "Look orders up.", "inputSchema": PLAIN}], ok, instructions=INSTRUCTIONS)
    instrument(server, lsquare)
    async with connect(server) as connection:
        told = await given(connection)
        # No list yet: the tool's description isn't known, the server's instructions are.
        await connection.call("orders", {"id": "1"})
        await connection.tools()
        await connection.call("orders", {"id": "2"})
    await flush(lsquare)
    assert told == INSTRUCTIONS
    first, second = events.calls()
    assert first["serverInstructions"] == INSTRUCTIONS and "toolDescription" not in first
    assert second["serverInstructions"] == INSTRUCTIONS and second["toolDescription"] == "Look orders up."


@only("mcp2", "fastmcp4")
@pytest.mark.parametrize("mode", ["legacy", None])
async def test_mcp2_in_either_era_the_calls_carry_what_the_handshake_or_the_discovery_gave(mode: str | None) -> None:
    # Legacy: `initialize` gives them. The client's default: `server/discover` does, on 2026-07-28.
    lsquare, events = client()
    app = high("orders", instructions=INSTRUCTIONS)
    tool(app, search)
    instrument(app, lsquare, extras=OFF)
    async with connect(app, mode=mode) as connection:
        told = await given(connection)
        await connection.call("search", {"query": "shoes"})
    await flush(lsquare)
    assert told == INSTRUCTIONS
    assert instructed(events) == [("search", INSTRUCTIONS)]
    [event] = events.calls()
    assert (event["protocolVersion"] >= "2026-07-28") is (mode is None)


async def test_get_more_tools_carries_them_too_as_they_are_the_servers_not_a_tools() -> None:
    lsquare, events = client()
    app = high("orders", instructions=INSTRUCTIONS)
    tool(app, search)
    instrument(app, lsquare)
    async with connect(app) as connection:
        await connection.call("search", {"query": "shoes", "intent": "find shoes"})
        noted = await connection.call("get_more_tools", {"need": "issue refunds"})
    await flush(lsquare)
    assert noted.texts[0].startswith("Noted.")
    first, second = events.calls()
    assert first["serverInstructions"] == INSTRUCTIONS and first["toolDescription"] == "Search the orders by a query."
    assert second["extra"] == "missing_tools" and "toolDescription" not in second
    assert second["serverInstructions"] == INSTRUCTIONS


# What has none.


@pytest.mark.parametrize("held", [None, "", 42, ["Search first."]])
async def test_a_server_without_instructions_or_with_ones_that_arent_text_has_none(held: Any) -> None:
    lsquare, events = client()
    app = high("orders")
    tool(app, search)
    instrument(app, lsquare, extras=OFF)
    server = low("plain", [{"name": "orders", "inputSchema": PLAIN}], ok)
    instrument(server, lsquare)
    for each, name, arguments in ((app, "search", {"query": "shoes"}), (server, "orders", {"id": "1"})):
        holder = lowlevel(each)
        # The SDK's own handshake refuses instructions that aren't text: set once it is over, as
        # an app can, on the instance the wrapper reads.
        holder.instructions = held if held is None or isinstance(held, str) else None
        async with connect(each) as connection:
            holder.instructions = held
            await connection.call(name, arguments)
    await flush(lsquare)
    assert instructed(events) == [("search", None), ("orders", None)]
    # Left out without a word: the server has none to give.
    assert all("dropped" not in event for event in events.calls())


async def test_limited_diagnostics_never_carry_them() -> None:
    recorder = Recorder()
    lsquare, events = client(on_error=recorder.on_error)
    app = high("orders", instructions=INSTRUCTIONS)
    tool(app, search)

    def failing(call: IdentifyInput) -> Any:
        raise RuntimeError("no auth")

    instrument(app, lsquare, extras=OFF, identify=failing)
    async with connect(app) as connection:
        await connection.call("search", {"query": "shoes"})
    await flush(lsquare)
    [event] = events.calls()
    assert event["identity"] == "failed"
    assert "serverInstructions" not in event and "toolDescription" not in event


# The option.


async def test_instructions_false_leaves_them_out_of_every_call_and_reads_none() -> None:
    lsquare, events = client()
    server = low("plain", [{"name": "orders", "inputSchema": PLAIN}], ok, instructions=INSTRUCTIONS)
    reads = counting(server)
    instrument(server, lsquare, instructions=False)
    app = high("orders", instructions=INSTRUCTIONS)
    tool(app, search)
    instrument(app, lsquare, extras=OFF, instructions=False)
    async with connect(server) as connection:
        told = await given(connection)
        before = len(reads)
        await connection.call("orders", {"id": "1"})
        await connection.call("orders", {"id": "2"})
        during = len(reads) - before
    async with connect(app) as connection:
        await connection.call("search", {"query": "shoes"})
    await flush(lsquare)
    # The clients are given them as before: only the events are without.
    assert told == INSTRUCTIONS and during == 0
    assert instructed(events) == [("orders", None), ("orders", None), ("search", None)]
    # The tool's description is another option's.
    assert events.calls()[2]["toolDescription"] == "Search the orders by a query."


async def test_they_are_read_once_a_call_as_the_instance_holds_them_then() -> None:
    lsquare, events = client()
    server = low("plain", [{"name": "orders", "inputSchema": PLAIN}], ok, instructions=INSTRUCTIONS)
    reads = counting(server)
    instrument(server, lsquare)
    async with connect(server) as connection:
        before = len(reads)
        await connection.call("orders", {"id": "1"})
        once = len(reads) - before
        # Changed on the instance: what its next clients are given, and what its next calls carry.
        server.instructions = "Look an order up before you change it."
        await connection.call("orders", {"id": "2"})
        server.instructions = None
        await connection.call("orders", {"id": "3"})
    await flush(lsquare)
    assert once == 1
    assert instructed(events) == [("orders", INSTRUCTIONS), ("orders", "Look an order up before you change it."), ("orders", None)]


@pytest.mark.parametrize("on", [True, None, 0, "no", lambda tool_name: False])
async def test_only_false_turns_them_off(on: Any) -> None:
    lsquare, events = client()
    app = high("orders", instructions=INSTRUCTIONS)
    tool(app, search)
    instrument(app, lsquare, extras=OFF, instructions=on)
    async with connect(app) as connection:
        await connection.call("search", {"query": "shoes"})
    await flush(lsquare)
    assert instructed(events) == [("search", INSTRUCTIONS)]


def counting(server: Any) -> list[str | None]:
    """Makes `server.instructions` note each read of it, and gives the notes."""
    reads: list[str | None] = []
    held = {"value": server.__dict__.pop("instructions")}

    def get(self: Any) -> Any:
        reads.append(held["value"])
        return held["value"]

    def put(self: Any, value: Any) -> None:
        held["value"] = value

    server.__class__ = type(type(server).__name__, (type(server),), {"instructions": property(get, put), "__module__": type(server).__module__})
    return reads


@only("fastmcp3", "fastmcp4")
async def test_fastmcp_instructions_set_on_the_server_after_it_was_made_are_the_ones_carried() -> None:
    assert FAST
    lsquare, events = client()
    app = high("orders")
    tool(app, search)
    instrument(app, lsquare, extras=OFF)
    # fastmcp's own setter, after instrument(): read as each call arrives.
    app.instructions = INSTRUCTIONS
    async with connect(app) as connection:
        told = await given(connection)
        await connection.call("search", {"query": "shoes"})
        app.instructions = "Ask before you refund."
        await connection.call("search", {"query": "hats"})
    await flush(lsquare)
    assert told == INSTRUCTIONS
    assert instructed(events) == [("search", INSTRUCTIONS), ("search", "Ask before you refund.")]


# Shaped as the event's other text.


async def test_instructions_longer_than_2000_characters_are_cut_and_ones_with_an_email_are_redacted() -> None:
    lsquare, events = client()
    app = high("orders", instructions="lorem ipsum " * 250)
    tool(app, search)
    instrument(app, lsquare, extras=OFF)
    other, others = client()
    second = high("support", instructions="Ask jane@acme.io before you refund an order.")
    tool(second, search, description="Search the orders. Questions go to jane@acme.io.")
    instrument(second, other, extras=OFF)
    async with connect(app) as connection:
        told = await given(connection)
        await connection.call("search", {"query": "shoes"})
    async with connect(second) as connection:
        await connection.call("search", {"query": "hats"})
    await flush(lsquare)
    await flush(other)
    # The client is given them whole: only the event's copy is cut.
    assert told == "lorem ipsum " * 250
    [event] = events.calls()
    assert event["serverInstructions"] == ("lorem ipsum " * 250)[:2_000] + "…[+1000 chars]"
    assert event["truncated"] == ["serverInstructions"]
    [event] = others.calls()
    # The same address as in the tool's description: the same placeholder.
    assert event["toolDescription"] == "Search the orders. Questions go to [EMAIL_1]."
    assert event["serverInstructions"] == "Ask [EMAIL_1] before you refund an order."
    assert event["redactions"] == {"email": 2}


async def test_before_send_sees_them_and_can_change_them_or_take_them_out() -> None:
    seen: list[Any] = []

    def before_send(event: dict[str, Any]) -> Any:
        seen.append(event.get("serverInstructions"))
        query = event["input"]["query"]
        if query == "changed":
            return {**event, "serverInstructions": "Ask ops@acme.io first."}
        if query == "removed":
            return {key: value for key, value in event.items() if key != "serverInstructions"}
        return event

    lsquare, events = client(before_send=before_send)
    app = high("orders", instructions="Ask jane@acme.io before you refund an order.")
    tool(app, search)
    instrument(app, lsquare, extras=OFF)
    async with connect(app) as connection:
        for query in ("kept", "changed", "removed"):
            await connection.call("search", {"query": query})
    await flush(lsquare)
    # Not redacted yet: the hook sees the text.
    assert seen == ["Ask jane@acme.io before you refund an order."] * 3
    by_query = {event["input"]["query"]: event.get("serverInstructions") for event in events.calls()}
    assert by_query == {"kept": "Ask [EMAIL_1] before you refund an order.", "changed": "Ask [EMAIL_1] first.", "removed": None}
