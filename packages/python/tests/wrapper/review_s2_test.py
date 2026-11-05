"""Codex's first review of the wrapper: one test per finding."""

from __future__ import annotations

import asyncio
import contextvars
import re
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

from helpers import Recorder
from lines import FAST, MAJOR, client, connect, high, needs_line, only, tool
from lsquare._client import CallScope
from lsquare.mcp import IdentifyInput, instrument
from lsquare.mcp._boundary import tapped

pytestmark = [needs_line, pytest.mark.anyio]
ID_LINE = re.compile(r"^Task ID: ses_[A-Za-z0-9_-]{22}\. ")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def search(query: str) -> str:
    return f"found {query}"


@only("fastmcp3", "fastmcp4")
async def test_an_injection_subclass_that_resolves_names_its_own_way_leaves_calls_as_sent() -> None:
    from fastmcp.server.middleware.tool_injection import ToolInjectionMiddleware
    from fastmcp.tools import Tool

    def order(x: int, intent: str = "") -> str:
        return f"{x}:{intent}"

    class Resolving(ToolInjectionMiddleware):
        def get_injected_tool(self, name: str) -> Any:
            return Tool.from_function(order) if name == "order" else None

    def plain_order(x: int) -> str:
        return "plain"

    lsquare, _ = client()
    app = high()
    tool(app, plain_order, name="order")
    app.add_middleware(Resolving(tools=[]))
    instrument(app, lsquare)
    async with connect(app) as connection:
        reply = await connection.call("order", {"x": 1, "intent": "mine"})
    assert not reply.is_error and reply.texts[0] in ("1:mine", "plain")


async def test_a_subclass_of_a_high_level_server_is_recognised() -> None:
    lsquare, events = client()
    base = type(high())

    class Mine(base):  # type: ignore[valid-type, misc]
        pass

    app = Mine("mine")
    tool(app, search)
    assert instrument(app, lsquare) is app
    async with connect(app) as connection:
        await connection.call("search", {"query": "a"})
    await lsquare.aflush(timeout_ms=5_000)
    assert [event["toolName"] for event in events.calls()] == ["search"]


async def test_a_call_that_raises_keeps_the_session_its_agent_sent() -> None:
    lsquare, events = client()
    app = high()

    def boom(x: int) -> str:
        raise ValueError("no")

    tool(app, boom)
    instrument(app, lsquare)
    session = "ses_" + "b" * 22
    async with connect(app) as connection:
        await connection.call("boom", {"x": 1, "session_id": session})
    await lsquare.aflush(timeout_ms=5_000)
    [event] = events.calls()
    assert event["sessionId"] == session and event["sessionSource"] == "sent"


async def test_a_sync_function_returning_an_awaitable_runs_until_it_settles_and_a_closed_generator_closes() -> None:
    lsquare, _ = client()
    var = lsquare._lsquare_internal().calls()

    async def later() -> str:
        raise ValueError("late")

    def returns_awaitable() -> Any:
        return later()

    scope = CallScope(call_id="c1")
    token = var.set(scope)
    try:
        # fastmcp awaits what a sync tool returns (`finishes`): mcp 1.x and 2.x convert it as it is.
        pending = tapped(returns_awaitable, lambda: var, finishes=True)()
        assert scope.thrown is None
        with pytest.raises(ValueError):
            await pending
    finally:
        var.reset(token)
    assert scope.thrown is not None and isinstance(scope.thrown[0], ValueError)

    seen: list[contextvars.Context] = []

    def stream() -> Iterator[int]:
        # Work started inside a step carries the run's boundary with it.
        seen.append(contextvars.copy_context())
        yield 1
        yield 2

    scope = CallScope(call_id="c2")
    token = var.set(scope)
    try:
        generator = tapped(stream, lambda: var)()
        assert next(generator) == 1
        generator.close()
    finally:
        var.reset(token)
    # Closed: a capture from work the run started is a turn of its own, not the call's agent part.
    seen[0].run(lsquare.capture, {"question": "after close", "answer": "x"})
    assert scope.agent is None and scope.thrown is None


@only("fastmcp4")
async def test_fastmcps_own_error_result_is_the_tools_error_with_the_id_line() -> None:
    from fastmcp.tools.base import ToolResult

    lsquare, events = client()
    app = high()

    def failing():  # type: ignore[no-untyped-def]
        return ToolResult(content="out of stock", is_error=True)

    tool(app, failing)
    instrument(app, lsquare)
    async with connect(app) as connection:
        reply = await connection.call("failing", {})
    await lsquare.aflush(timeout_ms=5_000)
    assert reply.is_error and reply.texts[0] == "out of stock" and ID_LINE.match(reply.texts[-1])
    [event] = events.calls()
    assert event["error"]["category"] == "tool_error"


async def test_a_fallback_build_no_thread_takes_finishes_the_call_and_aborts_identify() -> None:
    recorder = Recorder()
    lsquare, events = client(on_error=recorder.on_error)
    release = threading.Event()
    aborted = threading.Event()

    def slow(call: IdentifyInput) -> Any:
        call.signal.add_listener(aborted.set)
        release.wait(5)
        return {"userId": "u"}

    app = high()
    tool(app, search)
    instrument(app, lsquare, identify=slow, extras={"sessions": False, "intent": False, "missing_tools": False})
    lsquare._builds.submit = lambda job: False  # type: ignore[method-assign]
    loop_calls = lsquare._lsquare_internal().call_on_loop
    lsquare._lsquare_internal().call_on_loop = lambda origin, fn: False  # type: ignore[method-assign]
    try:
        async with connect(app) as connection:
            await connection.call("search", {"query": "a"})
        assert aborted.wait(3)
        assert recorder.wait_for(lambda: any("No thread could build" in error.message for error in recorder.errors))
        deadline = time.monotonic() + 2
        while lsquare._finishing_calls and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert not lsquare._finishing_calls and events.calls() == []
    finally:
        release.set()
        lsquare._lsquare_internal().call_on_loop = loop_calls  # type: ignore[method-assign]


@only("mcp2")
async def test_middleware_that_replaces_the_get_more_tools_reply_costs_it_the_id_line() -> None:
    async def other(ctx: Any, call_next: Any) -> Any:
        result = await call_next(ctx)
        return {**result} if ctx.method == "tools/call" else result

    lsquare, _ = client()
    app = high()
    tool(app, search)
    app._lowlevel_server.middleware.append(other)
    instrument(app, lsquare)
    async with connect(app) as connection:
        reply = await connection.call("get_more_tools", {"need": "refunds"})
    assert reply.texts[0].startswith("Noted.") and not ID_LINE.match(reply.texts[-1])


@only("fastmcp3", "fastmcp4")
async def test_a_server_whose_only_tool_is_disabled_gets_no_get_more_tools() -> None:
    lsquare, _ = client()
    app = high()
    tool(app, search)
    app.disable(names={"search"})
    instrument(app, lsquare)
    async with connect(app) as connection:
        names = await connection.names()
        reply = await connection.call("get_more_tools", {"need": "x"})
    assert names == []
    assert reply.is_error and not reply.texts[0].startswith("Noted.")


async def test_identify_aborted_before_its_awaitable_starts_never_starts_and_none_runs_after_shutdown() -> None:
    lsquare, _ = client()
    internal = lsquare._lsquare_internal()
    started: list[str] = []
    gate = threading.Event()

    async def work() -> Any:
        started.append("ran")
        return {"userId": "u"}

    def returns_coroutine(call: Any) -> Any:
        gate.wait(2)
        return work()

    origin = internal.origin()
    run = internal.identify(returns_coroutine, lambda signal: signal, origin)
    assert run is not None
    run.abort()
    gate.set()
    with pytest.raises((asyncio.CancelledError, BaseException)):
        run.future.result(timeout=2)
    await asyncio.sleep(0.05)
    assert started == [] and internal.identifying["active"] == 0

    # A closed origin loop fails the hook rather than moving it to another loop.
    closed = asyncio.new_event_loop()
    closed.close()
    gone = internal.identify(work_identify, lambda signal: signal, _origin(origin, closed))
    assert gone is not None
    with pytest.raises(RuntimeError):
        gone.future.result(timeout=2)

    await lsquare.ashutdown(timeout_ms=100)
    assert internal.identify(returns_coroutine, lambda signal: signal, origin) is None


async def work_identify(call: Any) -> Any:
    return {"userId": "u"}


def _origin(origin: Any, loop: asyncio.AbstractEventLoop) -> Any:
    from lsquare._client import Origin

    return Origin(contextvars.copy_context(), origin.depth, loop, False)


# -- Codex's second review ---------------------------------------------------------------------------


async def test_an_identity_that_comes_after_the_deadline_never_counts_however_late_the_build_runs() -> None:
    lsquare, events = client()
    internal = lsquare._lsquare_internal()

    async def stubborn(call: IdentifyInput) -> Any:
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            return {"userId": "too-late"}
        return None

    app = high()
    tool(app, search)
    instrument(app, lsquare, identify=stubborn, extras={"sessions": False, "intent": False, "missing_tools": False})
    busy = threading.Event()

    def occupy() -> None:
        busy.wait(2.5)

    # The build worker is busy past identify's deadline.
    lsquare._builds.submit(occupy)
    try:
        async with connect(app) as connection:
            await connection.call("search", {"query": "a"})
        await lsquare.aflush(timeout_ms=5_000)
    finally:
        busy.set()
    await lsquare.aflush(timeout_ms=5_000)
    [event] = events.calls()
    assert event["identity"] == "failed"
    assert internal.identified["keys"] is None


async def test_a_get_more_tools_registered_while_visibility_is_read_is_the_developers(monkeypatch: pytest.MonkeyPatch) -> None:
    from lsquare.mcp import _registry

    lsquare, _ = client()
    app = high()
    tool(app, search)

    def get_more_tools(need: str) -> str:
        return f"mine: {need}"

    async def visible(self: Any) -> bool:
        tool(app, get_more_tools)
        return True

    monkeypatch.setattr(_registry.Registry, "visible", visible)
    instrument(app, lsquare)
    async with connect(app) as connection:
        reply = await connection.call("get_more_tools", {"need": "x"})
    assert reply.texts[0] == "mine: x"


async def test_a_call_cancelled_while_visibility_is_read_is_recorded_as_timeout() -> None:
    import anyio

    from lsquare.mcp._call import NO_EXTRAS, Extras, State, on_call, read_options

    lsquare, events = client()
    internal = lsquare._lsquare_internal()
    started = anyio.Event()

    class Registry:
        kind = "fastmcp"

        def owns_missing_tools(self) -> bool:
            return False

        def offers_missing_tools(self) -> bool:
            return True

        async def visible(self) -> bool:
            started.set()
            await anyio.sleep(30)
            return True

    class Dispatch:
        request = None
        tool_name = "get_more_tools"
        arguments = {"need": "x"}
        context = None

        def client(self) -> Any:
            return None

        def version(self) -> Any:
            return "2025-11-25"

        def continued(self) -> bool:
            return False

        def read(self, result: Any, key: str) -> Any:
            return None

        def jsonrpc_code(self, error: BaseException) -> int | None:
            return None

        def jsonrpc_message(self, error: BaseException) -> str | None:
            return None

        def recorded(self, result: Any) -> Any:
            return result

    settings = read_options(internal, None, True, True, None, None, False)
    state = State(internal=internal, settings=settings, line="v2", low=object(), high=object(), registry=Registry(), high_extras=Extras(missing_tools=True))  # type: ignore[arg-type]
    assert state.low_extras == NO_EXTRAS
    cancelled: list[bool] = []

    async def run() -> None:
        try:
            await on_call(state, Dispatch())  # type: ignore[arg-type]
        except anyio.get_cancelled_exc_class():
            cancelled.append(True)
            raise

    async with anyio.create_task_group() as group:
        group.start_soon(run)
        await started.wait()
        group.cancel_scope.cancel()
    await lsquare.aflush(timeout_ms=3_000)
    assert cancelled == [True]
    [event] = events.calls()
    assert event["toolName"] == "get_more_tools" and event["error"]["category"] == "timeout"


async def test_identify_admitted_as_shutdown_happens_is_refused_and_holds_no_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    from lsquare import _client

    lsquare, _ = client()
    internal = lsquare._lsquare_internal()
    enter = _client._enter_callback

    def shutting_down(*args: Any) -> None:
        lsquare.shutdown(timeout_ms=0)
        enter(*args)

    monkeypatch.setattr(_client, "_enter_callback", shutting_down)
    assert internal.identify(lambda call: {"userId": "u"}, lambda signal: signal, internal.origin()) is None
    assert internal.identifying["active"] == 0 and not lsquare._identify_runs


# -- Codex's third review ----------------------------------------------------------------------------


@only("mcp2", "fastmcp4")
async def test_a_handler_replaced_while_visibility_is_read_gets_the_call_as_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    import mcp_types

    from lsquare.mcp import _registry

    lsquare, _ = client()
    app = high()
    tool(app, search)
    low = app._mcp_server if FAST else app._lowlevel_server
    seen: list[dict[str, Any]] = []

    async def replacement(ctx: Any, params: Any) -> Any:
        seen.append(dict(params.arguments or {}))
        return mcp_types.CallToolResult(content=[mcp_types.TextContent(type="text", text="replaced")])

    async def visible(self: Any) -> bool:
        low.add_request_handler("tools/call", mcp_types.CallToolRequestParams, replacement)
        return True

    monkeypatch.setattr(_registry.Registry, "visible", visible)
    instrument(app, lsquare)
    session = "ses_" + "c" * 22
    async with connect(app) as connection:
        reply = await connection.call("get_more_tools", {"need": "x", "session_id": session})
    assert reply.texts == ["replaced"]
    assert seen == [{"need": "x", "session_id": session}]


async def test_a_sync_identify_aborted_before_a_thread_takes_it_never_runs() -> None:
    lsquare, _ = client()
    internal = lsquare._lsquare_internal()
    ran: list[bool] = []
    submitted: list[Any] = []
    submit = lsquare._identify_hooks.submit
    lsquare._identify_hooks.submit = lambda job: submitted.append(job) or True  # type: ignore[method-assign, func-returns-value]
    try:
        run = internal.identify(lambda call: ran.append(True), lambda signal: signal, internal.origin())
        assert run is not None
        lsquare.shutdown(timeout_ms=0)
        [job] = submitted
        job()
    finally:
        lsquare._identify_hooks.submit = submit  # type: ignore[method-assign]
    assert ran == [] and run.signal.aborted
    assert run.future.done() and internal.identifying["active"] == 0
