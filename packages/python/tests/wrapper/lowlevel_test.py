"""The extras on a low-level server (Python spec, section 5, "Low-level servers"), ported from
packages/lsquare/test/mcp-low.test.ts: off unless passed as True, the snapshot read from the
server's own list, every page, read again when something says it changed, its bounds, its wait,
give-up and pause, and a high-level server whose tools/call handler was replaced."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from helpers import Recorder
from lines import LINE, MAJOR, client, connect, high, needs_line, only, schema, tool
from lsquare.mcp import _lists, instrument

if MAJOR == 1 and LINE is not None:
    import mcp.types as mcp1_types
if MAJOR == 2 and LINE is not None:
    import mcp_types

pytestmark = [needs_line, pytest.mark.anyio]
ID_LINE = re.compile(r"^Task ID: (ses_[A-Za-z0-9_-]{22})\. ")
ALL = {"sessions": True, "intent": True, "missing_tools": True}
PLAIN = {"type": "object", "properties": {"id": {"type": "string"}}}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Plan:
    """A low-level server's list, by cursor, and what its handlers saw."""

    def __init__(self, pages: dict[str | None, tuple[list[dict[str, Any]], str | None]]) -> None:
        self.pages = pages
        self.listings: list[dict[str, Any]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.list_delay = 0.0
        #: A delay for the next listing only (the wrapper's read, which comes first).
        self.list_delay_once = 0.0
        self.list_error: Exception | None = None
        #: Run inside a call with the SDK's request context (1.x) or ctx (2.x).
        self.during_call: Callable[[Any], Awaitable[None]] | None = None
        self.result: dict[str, Any] | None = None

    def tools(self) -> int:
        return sum(len(tools) for tools, _ in self.pages.values())


def build(plan: Plan) -> Any:
    if MAJOR == 1:
        from mcp.server.lowlevel import Server

        server: Any = Server("low")

        async def list_tools(request: mcp1_types.ListToolsRequest) -> mcp1_types.ListToolsResult:
            context = server.request_context
            # None: the SDK filling its output-schema cache, before a call.
            cursor = request.params.cursor if request is not None and request.params is not None else None
            plan.listings.append({
                "cursor": cursor, "meta": context.meta, "task": context.experimental.task_metadata,
                "sse": context.close_sse_stream,
            })
            await _delay(plan)
            _fail(plan)
            tools, following = plan.pages[cursor]
            return mcp1_types.ListToolsResult(tools=[mcp1_types.Tool.model_validate(each) for each in tools], nextCursor=following)

        server.list_tools()(list_tools)

        async def call_tool(name: str, arguments: dict[str, Any]) -> Any:
            plan.calls.append((name, dict(arguments)))
            if plan.during_call is not None:
                await plan.during_call(server.request_context)
            if plan.result is not None:
                return mcp1_types.CallToolResult.model_validate(plan.result)
            return [mcp1_types.TextContent(type="text", text=f"ok {name}")]

        server.call_tool(validate_input=False)(call_tool)
        return server

    from mcp.server.lowlevel import Server as Low

    async def on_list_tools(ctx: Any, params: Any) -> Any:
        cursor = getattr(params, "cursor", None)
        plan.listings.append({"cursor": cursor, "method": ctx.method, "meta": ctx.meta, "sse": ctx.close_sse_stream})
        await _delay(plan)
        _fail(plan)
        tools, following = plan.pages[cursor]
        return mcp_types.ListToolsResult.model_validate({"tools": tools, "nextCursor": following})

    async def on_call_tool(ctx: Any, params: Any) -> Any:
        plan.calls.append((params.name, dict(params.arguments or {})))
        if plan.during_call is not None:
            await plan.during_call(ctx)
        if plan.result is not None:
            return mcp_types.CallToolResult.model_validate(plan.result)
        return mcp_types.CallToolResult(content=[mcp_types.TextContent(type="text", text=f"ok {params.name}")])

    return Low("low", on_list_tools=on_list_tools, on_call_tool=on_call_tool)


async def _delay(plan: Plan) -> None:
    delay, plan.list_delay_once = plan.list_delay + plan.list_delay_once, 0.0
    if delay:
        await asyncio.sleep(delay)


def _fail(plan: Plan) -> None:
    """The next listing fails (the wrapper's read, which comes first); later ones work."""
    error, plan.list_error = plan.list_error, None
    if error is not None:
        raise error


def connected(server: Any) -> Any:
    # mcp 2.x: the legacy era, where the extras apply.
    return connect(server, mode="legacy") if MAJOR == 2 else connect(server)


def one_page(*names: str, input_schema: dict[str, Any] | None = None) -> dict[str | None, tuple[list[dict[str, Any]], str | None]]:
    return {None: ([{"name": name, "inputSchema": input_schema or PLAIN} for name in names], None)}


def properties(listed: Any) -> list[str]:
    return sorted(schema(listed).get("properties") or {})


def issued(texts: list[str]) -> str | None:
    match = ID_LINE.match(texts[-1]) if texts else None
    return match.group(1) if match else None


async def test_stays_off_unless_each_extra_is_passed_as_true() -> None:
    for extras in (None, {"sessions": False}, {"intent": "yes"}):
        plan = Plan(one_page("orders"))
        server = build(plan)
        instrument(server, client()[0], extras=extras)  # type: ignore[arg-type]
        async with connected(server) as connection:
            listed = await connection.tools()
            reply = await connection.call("orders", {"id": "1", "intent": "why"})
        assert [item.name for item in listed] == ["orders"] and properties(listed[0]) == ["id"]
        assert plan.calls == [("orders", {"id": "1", "intent": "why"})] and issued(reply.texts) is None
        assert plan.listings == [plan.listings[0]], "no read of its own"


async def test_turned_on_lists_the_inputs_and_get_more_tools_takes_them_out_and_gives_the_first_result_an_id() -> None:
    lsquare, events = client()
    plan = Plan(one_page("orders"))
    server = build(plan)
    instrument(server, lsquare, extras=ALL)
    async with connected(server) as connection:
        listed = {item.name: item for item in await connection.tools()}
        first = await connection.call("orders", {"id": "1", "intent": "find an order"})
        session_id = issued(first.texts)
        second = await connection.call("orders", {"id": "2", "session_id": session_id})
        noted = await connection.call("get_more_tools", {"need": "refunds"})
        refused = await connection.call("get_more_tools", {})
    await lsquare.aflush(timeout_ms=5_000)
    assert properties(listed["orders"]) == ["id", "intent", "session_id"] and "get_more_tools" in listed
    assert plan.calls == [("orders", {"id": "1"}), ("orders", {"id": "2"})]
    assert session_id is not None and second.texts == ["ok orders"]
    assert noted.texts[0].startswith("Noted.") and refused.is_error and issued(refused.texts) is None
    one, two, three, four = events.calls()
    assert one["intent"] == "find an order" and one["sessionSource"] == "issued"
    assert two["sessionId"] == session_id and two["sessionSource"] == "sent"
    assert three["extra"] == "missing_tools" and four["error"]["category"] == "protocol"


async def test_turns_each_on_by_itself() -> None:
    for on, name in (("sessions", "session_id"), ("intent", "intent")):
        plan = Plan(one_page("orders"))
        server = build(plan)
        instrument(server, client()[0], extras={on: True})
        async with connected(server) as connection:
            listed = {item.name: item for item in await connection.tools()}
        assert properties(listed["orders"]) == sorted(["id", name]) and "get_more_tools" not in listed, on
    plan = Plan(one_page("orders"))
    server = build(plan)
    instrument(server, client()[0], extras={"missing_tools": True})
    async with connected(server) as connection:
        listed = {item.name: item for item in await connection.tools()}
    assert properties(listed["orders"]) == ["id"] and "get_more_tools" in listed


async def test_reads_every_page_before_a_call_with_a_copy_of_the_request_context_marked_as_a_listing() -> None:
    plan = Plan({
        None: ([{"name": "a", "inputSchema": PLAIN}], "page-2"),
        "page-2": ([{"name": "b", "inputSchema": PLAIN}], "page-3"),
        "page-3": ([{"name": "c", "inputSchema": PLAIN}], None),
    })
    server = build(plan)
    instrument(server, client()[0], extras=ALL)
    async with connected(server) as connection:
        await connection.call("c", {"id": "1", "intent": "last page"})
    assert plan.calls == [("c", {"id": "1"})]
    # The wrapper's read comes first; the client may list once more after the call, for its own checks.
    read = plan.listings[:3]
    assert [listing["cursor"] for listing in read] == [None, "page-2", "page-3"]
    for listing in read:
        assert listing["meta"] is None and listing["sse"] is None
        assert listing.get("method", "tools/list") == "tools/list"
        assert listing.get("task") is None


async def test_reads_the_list_again_after_list_changed_and_after_the_tools_list_handler_is_replaced() -> None:
    plan = Plan(one_page("orders"))
    server = build(plan)

    async def changes(context: Any) -> None:
        plan.pages = one_page("orders", input_schema={"type": "object", "properties": {"id": {"type": "string"}, "intent": {"type": "string"}}})
        await context.session.send_tool_list_changed()

    instrument(server, client()[0], extras=ALL)
    async with connected(server) as connection:
        await connection.call("orders", {"id": "1", "intent": "a"})
        plan.during_call = changes
        await connection.call("orders", {"id": "2", "intent": "b"})
        plan.during_call = None
        # The tool now declares intent: it's its own, and stays in the call.
        await connection.call("orders", {"id": "3", "intent": "c"})
    assert plan.calls == [("orders", {"id": "1"}), ("orders", {"id": "2"}), ("orders", {"id": "3", "intent": "c"})]


@only("mcp2")
async def test_mcp2_a_notification_sent_on_the_requests_own_channel_is_seen() -> None:
    plan = Plan(one_page("orders"))
    server = build(plan)

    async def raw(ctx: Any) -> None:
        plan.pages = one_page("orders", input_schema={"type": "object", "properties": {"intent": {"type": "string"}}})
        await ctx.session._request_outbound.notify("notifications/tools/list_changed", None)

    instrument(server, client()[0], extras=ALL)
    async with connected(server) as connection:
        plan.during_call = raw
        await connection.call("orders", {"id": "1"})
        plan.during_call = None
        await connection.call("orders", {"id": "2", "intent": "mine"})
    assert plan.calls[-1] == ("orders", {"id": "2", "intent": "mine"})


async def test_leaves_alone_a_tool_whose_listed_schema_says_more_than_named_properties() -> None:
    plan = Plan(one_page("open", input_schema={"type": "object", "properties": {"id": {"type": "string"}}, "additionalProperties": True}))
    server = build(plan)
    instrument(server, client()[0], extras=ALL)
    async with connected(server) as connection:
        listed = {item.name: item for item in await connection.tools()}
        await connection.call("open", {"id": "1", "intent": "kept"})
    assert properties(listed["open"]) == ["id"]
    assert plan.calls == [("open", {"id": "1", "intent": "kept"})]


async def test_a_page_that_disagrees_with_the_snapshot_gets_nothing_and_is_read_again() -> None:
    plan = Plan(one_page("orders"))
    server = build(plan)
    instrument(server, client()[0], extras=ALL)
    async with connected(server) as connection:
        await connection.call("orders", {"id": "1"})
        # A tool added without list_changed: the next listing shows it.
        plan.pages = one_page("orders", "refunds")
        disagreeing = {item.name: item for item in await connection.tools()}
        agreeing = {item.name: item for item in await connection.tools()}
    # The tool that agrees keeps its inputs; the one the snapshot lacks, and get_more_tools, wait.
    assert "intent" in properties(disagreeing["orders"]) and "intent" not in properties(disagreeing["refunds"])
    assert "get_more_tools" not in disagreeing
    assert "intent" in properties(agreeing["refunds"]) and "get_more_tools" in agreeing


async def test_waits_at_most_a_second_for_a_slow_read_then_passes_the_call_on_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_lists, "WAIT_S", 0.2)
    plan = Plan(one_page("orders"))
    plan.list_delay_once = 0.6
    server = build(plan)
    instrument(server, client()[0], extras=ALL)
    async with connected(server) as connection:
        started = asyncio.get_running_loop().time()
        await connection.call("orders", {"id": "1", "intent": "early"})
        waited = asyncio.get_running_loop().time() - started
        await asyncio.sleep(0.6)
        await connection.call("orders", {"id": "2", "intent": "later"})
    assert waited < 0.5
    assert plan.calls == [("orders", {"id": "1", "intent": "early"}), ("orders", {"id": "2"})]


async def test_gives_up_a_read_at_its_deadline_whatever_its_handler_does_and_pauses_until_list_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_lists, "WAIT_S", 0.1)
    monkeypatch.setattr(_lists, "GIVE_UP_S", 0.3)
    plan = Plan(one_page("orders"))
    stubborn = asyncio.Event()

    async def changes(context: Any) -> None:
        await context.session.send_tool_list_changed()

    server = build(plan)
    original = plan.pages

    # The first listing ignores cancellation and finishes long after its deadline.
    async def slow_list(*args: Any) -> None:
        try:
            await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            await asyncio.sleep(0.5)

    plan.list_delay = 0
    instrument(server, client()[0], extras=ALL)
    real_collect = _lists._collect

    async def collect(reader: Any) -> Any:
        if not stubborn.is_set():
            stubborn.set()
            await slow_list()
        return await real_collect(reader)

    monkeypatch.setattr(_lists, "_collect", collect)
    async with connected(server) as connection:
        await connection.call("orders", {"id": "1", "intent": "a"})
        await asyncio.sleep(0.5)
        # Given up: paused, so this call goes as sent without a new read.
        listings = len(plan.listings)
        await connection.call("orders", {"id": "2", "intent": "b"})
        assert len(plan.listings) == listings
        plan.during_call = changes
        await connection.call("orders", {"id": "3", "intent": "c"})
        plan.during_call = None
        await connection.call("orders", {"id": "4", "intent": "d"})
    assert plan.pages is original
    assert [arguments for _, arguments in plan.calls] == [{"id": "1", "intent": "a"}, {"id": "2", "intent": "b"}, {"id": "3", "intent": "c"}, {"id": "4"}]


async def test_fails_a_read_that_breaks_its_bounds_passes_the_call_on_and_pauses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_lists, "MAX_TOOLS", 2)
    cases: list[tuple[str, Plan]] = [
        ("throws", Plan(one_page("orders"))),
        ("cursor twice", Plan({None: ([{"name": "a", "inputSchema": PLAIN}], "x"), "x": ([{"name": "b", "inputSchema": PLAIN}], "x")})),
        ("duplicate name", Plan({None: ([{"name": "a", "inputSchema": PLAIN}], "x"), "x": ([{"name": "a", "inputSchema": PLAIN}], None)})),
        ("too many tools", Plan(one_page("a", "b", "c"))),
        ("just enough tools", Plan(one_page("a", "b"))),
    ]
    cases[0][1].list_error = RuntimeError("no list")
    for label, plan in cases:
        server = build(plan)
        instrument(server, client()[0], extras=ALL)
        async with connected(server) as connection:
            await connection.call("a" if label != "throws" else "orders", {"id": "1", "intent": "x"})
            listings = len(plan.listings)
            await connection.call("a" if label != "throws" else "orders", {"id": "2", "intent": "y"})
        stripped = "intent" not in plan.calls[0][1]
        assert stripped == (label == "just enough tools"), label
        if label != "just enough tools":
            assert len(plan.listings) == listings, f"{label}: paused, no new read"


@only("mcp1")
async def test_mcp1_an_error_result_on_a_low_level_server_gets_no_id_line() -> None:
    plan = Plan(one_page("orders"))
    plan.result = {"content": [{"type": "text", "text": "no such order"}], "isError": True}
    server = build(plan)
    instrument(server, client()[0], extras=ALL)
    async with connected(server) as connection:
        reply = await connection.call("orders", {"id": "1"})
    assert reply.is_error and issued(reply.texts) is None


@only("mcp2")
async def test_mcp2_a_low_level_tools_error_result_is_its_own_and_gets_the_id_line() -> None:
    plan = Plan(one_page("orders"))
    plan.result = {"content": [{"type": "text", "text": "no such order"}], "isError": True}
    server = build(plan)
    instrument(server, client()[0], extras=ALL)
    async with connected(server) as connection:
        reply = await connection.call("orders", {"id": "1"})
    assert reply.is_error and issued(reply.texts) is not None


@only("mcp2")
async def test_mcp2_a_request_on_2026_07_28_is_passed_on_unchanged() -> None:
    plan = Plan(one_page("orders"))
    server = build(plan)
    instrument(server, client()[0], extras=ALL)
    async with connect(server, mode="2026-07-28") as connection:
        listed = {item.name: item for item in await connection.tools()}
        reply = await connection.call("orders", {"id": "1", "intent": "as sent"})
    assert list(listed) == ["orders"] and properties(listed["orders"]) == ["id"]
    assert plan.calls == [("orders", {"id": "1", "intent": "as sent"})] and issued(reply.texts) is None


async def test_a_high_level_server_whose_handler_was_replaced_follows_the_low_level_rules_with_only_the_true_extras() -> None:
    lsquare, _ = client()
    app = high()

    def orders(id: str):  # type: ignore[no-untyped-def]
        return f"order {id}"

    tool(app, orders)
    instrument(app, lsquare, extras={"intent": True})
    seen: list[dict[str, Any]] = []
    if MAJOR == 1:
        low = app._mcp_server

        @low.call_tool(validate_input=False)
        async def replacement(name: str, arguments: dict[str, Any]) -> Any:
            seen.append(dict(arguments))
            return [mcp1_types.TextContent(type="text", text="replaced")]

    else:
        from lines import FAST

        low = app._mcp_server if FAST else app._lowlevel_server

        async def handler(ctx: Any, params: Any) -> Any:
            seen.append(dict(params.arguments or {}))
            return mcp_types.CallToolResult(content=[mcp_types.TextContent(type="text", text="replaced")])

        low.add_request_handler("tools/call", mcp_types.CallToolRequestParams, handler)
    async with connected(app) as connection:
        listed = {item.name: item for item in await connection.tools()}
        reply = await connection.call("orders", {"id": "1", "intent": "why", "session_id": "ses_" + "d" * 22})
    assert properties(listed["orders"]) == ["id", "intent"] and "get_more_tools" not in listed
    assert seen == [{"id": "1", "session_id": "ses_" + "d" * 22}] and reply.texts == ["replaced"]


async def test_says_in_dry_run_whether_the_extras_are_on_and_where_they_come_from() -> None:
    from lsquare import LSquare

    for extras, expected in ((None, "turns them on"), (ALL, "the server's own tool list")):
        recorder = Recorder()
        lsquare = LSquare(dry_run=True, logger=recorder)
        instrument(build(Plan(one_page("orders"))), lsquare, extras=extras)
        assert recorder.wait_for(lambda: any(expected in line for _, line in recorder.lines)), extras


# -- The snapshot itself ------------------------------------------------------------------------------


def _reader(pages: Callable[[str | None], Awaitable[Any]]) -> _lists.Reader:
    import contextvars

    return _lists.Reader(pages, contextvars.copy_context())


async def test_a_read_overtaken_by_a_change_is_discarded_and_wakes_its_waiters() -> None:
    lsquare, _ = client()
    internal = lsquare._lsquare_internal()
    lists = _lists.Lists()
    release = asyncio.Event()

    async def page(cursor: str | None) -> Any:
        await release.wait()
        return {"tools": [{"name": "a", "inputSchema": PLAIN}]}

    wait = _lists.Wait()
    waiting = asyncio.ensure_future(_lists.wait_once(lists, internal, lambda: _reader(page), wait))
    await asyncio.sleep(0.05)
    _lists.mark(lists, True)
    assert await asyncio.wait_for(waiting, 1) is True
    release.set()
    await asyncio.sleep(0.05)
    assert _lists.fresh(lists) is None
    assert await _lists.with_snapshot(lists, internal, lambda: _reader(page), _lists.Wait()) is not None


async def test_a_read_that_fails_after_its_starting_call_was_cancelled_is_discarded_without_the_pause() -> None:
    lsquare, _ = client()
    internal = lsquare._lsquare_internal()
    lists = _lists.Lists()
    fail = asyncio.Event()

    async def page(cursor: str | None) -> Any:
        await fail.wait()
        raise RuntimeError("the cancelled request's channel is closed")

    wait = _lists.Wait()
    await _lists.wait_once(lists, internal, lambda: _reader(page), wait) if False else None
    task = asyncio.ensure_future(_lists.wait_once(lists, internal, lambda: _reader(page), wait))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    wait.cancelled()
    fail.set()
    await asyncio.sleep(0.05)
    assert lists.paused_until == 0.0


@pytest.mark.parametrize("anyio_backend", ["trio"])
async def test_on_trio_the_read_runs_as_a_system_task_and_a_wait_keeps_its_bound(anyio_backend: str) -> None:
    trio = pytest.importorskip("trio")
    lsquare, _ = client()
    internal = lsquare._lsquare_internal()
    lists = _lists.Lists()

    async def page(cursor: str | None) -> Any:
        await trio.sleep(0.05)
        return {"tools": [{"name": "a", "inputSchema": PLAIN}]}

    snapshot = await _lists.with_snapshot(lists, internal, lambda: _reader(page), _lists.Wait())
    assert snapshot is not None and list(snapshot.tools) == ["a"]

    async def never(cursor: str | None) -> Any:
        await trio.sleep(30)

    _lists.mark(lists, False)
    started = trio.current_time()
    assert await _lists.with_snapshot(lists, internal, lambda: _reader(never), _lists.Wait()) is None
    assert trio.current_time() - started < 1.5


# -- Codex's first review of step 3 -------------------------------------------------------------------


@pytest.mark.parametrize("anyio_backend", ["trio"])
async def test_on_trio_a_listing_that_raises_anything_fails_the_read_not_the_run(anyio_backend: str) -> None:
    trio = pytest.importorskip("trio")
    lsquare, _ = client()
    internal = lsquare._lsquare_internal()
    lists = _lists.Lists()

    class Odd(BaseException):
        pass

    async def page(cursor: str | None) -> Any:
        await trio.sleep(0)
        raise Odd()

    assert await _lists.with_snapshot(lists, internal, lambda: _reader(page), _lists.Wait()) is None
    await trio.sleep(0.05)
    assert lists.paused_until > 0 and not lists.tasks


@pytest.mark.parametrize("anyio_backend", ["trio"])
async def test_on_trio_the_read_runs_in_the_copied_context(anyio_backend: str) -> None:
    import contextvars

    trio = pytest.importorskip("trio")
    lsquare, _ = client()
    internal = lsquare._lsquare_internal()
    variable: contextvars.ContextVar[str] = contextvars.ContextVar("probe", default="unset")
    seen: list[str] = []

    async def page(cursor: str | None) -> Any:
        seen.append(variable.get())
        return {"tools": []}

    copied = contextvars.copy_context()
    copied.run(variable.set, "the request's")
    reader = _lists.Reader(page, copied)
    await _lists.with_snapshot(_lists.Lists(), internal, lambda: reader, _lists.Wait())
    assert seen == ["the request's"]


@only("mcp2")
async def test_mcp2_a_list_handler_replaced_during_the_read_is_read_again_before_the_call() -> None:
    plan = Plan(one_page("orders"))
    plan.list_delay_once = 0.3
    server = build(plan)
    instrument(server, client()[0], extras=ALL)
    entry = server._request_handlers["tools/list"]

    async def declaring(ctx: Any, params: Any) -> Any:
        return mcp_types.ListToolsResult.model_validate({"tools": [{"name": "orders", "inputSchema": {"type": "object", "properties": {"intent": {"type": "string"}}}}]})

    async def replace_soon() -> None:
        await asyncio.sleep(0.1)
        server.add_request_handler("tools/list", entry.params_type, declaring)

    async with connected(server) as connection:
        replacing = asyncio.ensure_future(replace_soon())
        await connection.call("orders", {"id": "1", "intent": "the tool's own"})
        await replacing
    assert plan.calls == [("orders", {"id": "1", "intent": "the tool's own"})]


async def test_a_call_cancelled_while_waiting_to_reply_still_counts_as_the_reads_starter(monkeypatch: pytest.MonkeyPatch) -> None:
    from lsquare.mcp import _call

    lsquare, _ = client()
    internal = lsquare._lsquare_internal()
    fail = asyncio.Event()

    async def failing(cursor: str | None) -> Any:
        await fail.wait()
        raise RuntimeError("the cancelled request's channel is closed")

    class Dispatch:
        request = None
        tool_name = "orders"
        arguments = {"id": "1"}
        context = None

        def reader(self) -> Any:
            return _reader(failing)

        def refresh(self) -> None:
            pass

        def version(self) -> Any:
            return "2025-11-25"

        def client(self) -> Any:
            return None

        def continued(self) -> bool:
            return False

        def session(self, version: Any) -> Any:
            return None

        async def call(self, request: Any) -> Any:
            return {"content": [{"type": "text", "text": "ok"}]}

        def read(self, result: Any, key: str) -> Any:
            return result.get(key) if isinstance(result, dict) else None

        def with_block(self, result: Any, text: str) -> Any:
            return result

        def recorded(self, result: Any) -> Any:
            return result

        def provenance(self, running: Any, result: Any) -> bool:
            return True

        def jsonrpc_code(self, error: BaseException) -> Any:
            return None

        def jsonrpc_message(self, error: BaseException) -> str | None:
            return None

    async def cancelled_wait(*args: Any) -> None:
        raise asyncio.CancelledError()

    monkeypatch.setattr(_lists, "WAIT_S", 0.05)
    monkeypatch.setattr(_call, "sent_by", cancelled_wait)
    settings = _call.read_options(internal, None, True, True, None, None, True)
    state = _call.State(internal=internal, settings=settings, line="v1", low=object(), high=None, registry=None, low_extras=_call.Extras(True, True, True))
    with pytest.raises(asyncio.CancelledError):
        await _call.on_call(state, Dispatch())  # type: ignore[arg-type]
    fail.set()
    await asyncio.sleep(0.05)
    assert state.lists.paused_until == 0.0


async def test_a_read_has_one_outcome_whether_it_finishes_or_expires_first() -> None:
    snapshot = _lists.Snapshot({}, False)
    lists = _lists.Lists()
    reading = _lists.Reading(generation=0, started=0.0)
    lists.reading = reading
    _lists._finish(lists, reading, snapshot)
    _lists._expire(lists, reading)
    assert _lists.fresh(lists) is snapshot and lists.paused_until == 0.0 and not reading.expired

    lists = _lists.Lists()
    reading = _lists.Reading(generation=0, started=0.0)
    lists.reading = reading
    _lists._expire(lists, reading)
    _lists._finish(lists, reading, snapshot)
    assert _lists.fresh(lists) is None and lists.paused_until > 0 and reading.expired


@only("mcp2")
async def test_mcp2_an_old_read_that_fails_after_its_handler_was_replaced_pauses_nothing() -> None:
    plan = Plan(one_page("orders"))
    plan.list_delay_once = 0.3
    plan.list_error = RuntimeError("the old handler fails late")
    server = build(plan)
    instrument(server, client()[0], extras=ALL)
    entry = server._request_handlers["tools/list"]

    async def working(ctx: Any, params: Any) -> Any:
        return mcp_types.ListToolsResult.model_validate({"tools": [{"name": "orders", "inputSchema": PLAIN}]})

    async def replace_soon() -> None:
        await asyncio.sleep(0.1)
        server.add_request_handler("tools/list", entry.params_type, working)

    async with connected(server) as connection:
        replacing = asyncio.ensure_future(replace_soon())
        await connection.call("orders", {"id": "1", "intent": "first"})
        await replacing
        await asyncio.sleep(0.4)
        await connection.call("orders", {"id": "2", "intent": "second"})
    # The working handler was read: no pause from the old one's failure.
    assert plan.calls[-1] == ("orders", {"id": "2"})
