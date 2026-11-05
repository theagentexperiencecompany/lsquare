"""mcp 2.x and fastmcp 4.x (Python spec, section 4): the low-level server's `middleware`.

One middleware goes in at index 0, outside the SDK's OpenTelemetry middleware, its
`RequestStateBoundary` and the developer's own, so the extras' inputs are out before anything
inside sees the arguments. A second one goes at the end, next to the handler, and records the
result the handler's step returned: the ID line needs the outer one to get that same object back.
It also marks that step, each time a middleware calls it: a tool's function run outside it, by a
middleware around the handler, isn't the call's tool. The list is read on every request, so
registering handlers doesn't disturb it.
"""

from __future__ import annotations

import contextvars
import dataclasses
from collections.abc import Callable, Mapping
from typing import Any, cast

from . import _fastmcp
from .._client import _enter_callback, current_depth
from ._boundary import HANDLING, Phase
from ._call import Running, State, high_mode, http_session, listed, low_listed, low_on, made_session, on_call, snapshot, sse_message, tap_tools, unchanged
from ._lists import Reader, mark
from ._extras import missing_tools_reply
from ._read import attr, cancelled, in_mro, item, loaded, wire
from ._run import in_stdio

LOWLEVEL = "mcp.server.lowlevel.server"
CLIENT_INFO_KEY = "io.modelcontextprotocol/clientInfo"
PROTOCOL_VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
INVALID_PARAMS = -32602
#: What the dispatcher sends with it for params that don't validate: never pydantic's text, which
#: quotes the values it refused.
INVALID_PARAMS_MESSAGE = "Invalid request parameters"


def is_low(value: Any) -> bool:
    """An mcp 2.x low-level `Server`, or a subclass such as fastmcp's: a `middleware` list."""
    return (
        in_mro(value, LOWLEVEL, "Server")
        and isinstance(attr(value, "middleware"), list)
        and isinstance(attr(value, "_request_handlers"), dict)
    )


def recognise(server: Any) -> tuple[Any, Any, str | None] | None:
    """(low-level server, high-level server or None, registry kind) for an mcp 2.x line server,
    the app's own subclasses included."""
    if in_mro(server, "mcp.server.mcpserver.server", "MCPServer"):
        low = attr(server, "_lowlevel_server")
        if is_low(low) and attr(server, "_tool_manager") is not None:
            return low, server, "mcpserver"
        return None
    if in_mro(server, "fastmcp.server.server", "FastMCP"):
        low = attr(server, "_mcp_server")
        if is_low(low) and attr(server, "_local_provider") is not None:
            return low, server, "fastmcp"
    if is_low(server):
        return server, None, None
    return None


def arm(state: State) -> None:
    low = state.low
    state.call_entry = _wrap_entry(state)
    state.list_entry = _list_entry(state)
    state.inner = Inner(state)
    low.middleware.insert(0, Outer(state))
    low.middleware.append(state.inner)
    if state.registry is not None and state.registry.kind == "fastmcp":
        _fastmcp.wrap_call_tool(state)


#: Marks the wrapper's own handler in an entry.
ANSWERS = "__lsquare_answers__"


def _wrap_entry(state: State) -> Any:
    """The tools/call entry wrapped so it answers the wrapper's own get_more_tools before the SDK
    looks the tool up, on a high-level server or a low-level one with the extra on. Returns the
    entry now in place; one already wrapped is left as it is."""
    handlers = attr(state.low, "_request_handlers")
    entry = handlers.get("tools/call") if isinstance(handlers, dict) else None
    original = attr(entry, "handler")
    if not callable(original) or attr(original, ANSWERS, False) is True:
        return entry
    if state.high is None and not state.low_extras.missing_tools:
        return entry
    internal = state.internal

    async def handler(ctx: Any, params: Any) -> Any:
        try:
            scope = internal.calls().get()
            running = scope.wrapper if scope is not None else None
            if isinstance(running, Running) and running.extras.answer:
                model = attr(loaded("mcp_types"), "CallToolResult")
                return model.model_validate(missing_tools_reply(item(attr(params, "arguments"), "need")))
        except Exception:
            pass
        return await original(ctx, params)

    try:
        setattr(handler, ANSWERS, True)
        wrapped = dataclasses.replace(cast(Any, entry), handler=handler)
        handlers["tools/call"] = wrapped
        return wrapped
    except Exception:
        return entry


def _entry(state: State) -> Any:
    handlers = attr(state.low, "_request_handlers")
    return handlers.get("tools/call") if isinstance(handlers, dict) else None


def _check_replaced(state: State) -> None:
    """A tools/call entry other than the one found at instrument() (`add_request_handler`) is a
    replacement: the server is read as low-level from then on. On a low-level server, or once
    replaced, a new entry is wrapped again, so get_more_tools can still be answered."""
    if state.high is not None and not state.replaced and _entry(state) is not state.call_entry:
        state.replaced = True
    if not high_mode(state) and state.low_extras.missing_tools:
        _wrap_entry(state)


def _list_entry(state: State) -> Any:
    handlers = attr(state.low, "_request_handlers")
    return handlers.get("tools/list") if isinstance(handlers, dict) else None


def _check_list_entry(state: State) -> None:
    """Another tools/list entry: a snapshot read from the one before is out of date."""
    entry = _list_entry(state)
    if entry is not state.list_entry:
        state.list_entry = entry
        mark(state.lists, False)


def _watch(state: State, ctx: Any) -> None:
    """Marks the snapshot out of date whenever the server sends notifications/tools/list_changed,
    before sending it on as it came: through this request's own channel, which every request-
    scoped helper ends in, and the connection's, for those sent outside a request. Each watched
    on the instance, never the class; the connection's once."""
    if not state.low_extras.any():
        return
    session = attr(ctx, "session")
    for channel in (attr(session, "_request_outbound"), attr(attr(session, "_connection"), "outbound")):
        notify = attr(channel, "notify")
        if not callable(notify) or attr(notify, WATCHED, False) is True:
            continue
        try:
            setattr(channel, "notify", _watched(state, notify))
        except Exception:
            pass


#: Marks a channel's notify already watched.
WATCHED = "__lsquare_watched__"


def _watched(state: State, notify: Callable[..., Any]) -> Callable[..., Any]:
    lists = state.lists

    async def watched(method: Any, *args: Any, **kwargs: Any) -> Any:
        if method == "notifications/tools/list_changed":
            mark(lists, True)
        return await notify(method, *args, **kwargs)

    setattr(watched, WATCHED, True)
    return watched


def _inner_present(state: State) -> bool:
    """The inner middleware is in the list as a call arrives, so in the chain composed for it."""
    middleware = attr(state.low, "middleware")
    try:
        return isinstance(middleware, list) and any(each is state.inner for each in middleware)
    except Exception:
        return False


def _inner_last(state: State) -> bool:
    """Whether the inner middleware is still last. If not, it's moved back for later calls: this
    one's chain is already composed."""
    middleware = attr(state.low, "middleware")
    if not isinstance(middleware, list):
        return False
    if middleware and middleware[-1] is state.inner:
        return True
    try:
        while state.inner in middleware:
            middleware.remove(state.inner)
        middleware.append(state.inner)
    except Exception:
        pass
    return False


class Outer:
    """The wrapper, at index 0."""

    def __init__(self, state: State) -> None:
        self.state = state

    async def __call__(self, ctx: Any, call_next: Callable[[Any], Any]) -> Any:
        method = attr(ctx, "method")
        if method == "tools/call":
            return await self._call(ctx, call_next)
        if method == "tools/list":
            return await self._list(ctx, call_next)
        return await call_next(ctx)

    async def _call(self, ctx: Any, call_next: Callable[[Any], Any]) -> Any:
        state = self.state
        try:
            _check_replaced(state)
            _check_list_entry(state)
            _watch(state, ctx)
            gated = _inner_present(state)
            dispatch = Call(state, ctx, call_next, misplaced=not _inner_last(state), gated=gated)
        except Exception:
            return await call_next(ctx)
        return await on_call(state, dispatch, tap_tools)

    async def _list(self, ctx: Any, call_next: Callable[[Any], Any]) -> Any:
        state = self.state
        try:
            _check_list_entry(state)
            _watch(state, ctx)
        except Exception:
            pass
        listing = await call_next(ctx)
        try:
            # Checked after the await too: the handler may have been replaced meanwhile.
            _check_replaced(state)
            if not high_mode(state) and state.low_extras.any():
                dispatch = Call(state, ctx, call_next, misplaced=False)
                if not low_on(state, dispatch):
                    return listing
                return await _with_low_extras(state, listing, ctx, dispatch)
            return _with_extras(state, listing)
        except Exception as error:
            if cancelled(error):
                raise
            return listing


class Inner:
    """Next to the handler: marks its step while it runs, and only then (`HANDLING`), and records
    the result it returned, before any middleware can replace it. Each time a middleware calls it
    is an attempt of its own: what an earlier one recorded isn't this one's. On a low-level server
    (or one whose handler was replaced) the handler is the tool: its return is the tool's, and a
    `capture()` from middleware after it is a turn of its own."""

    def __init__(self, state: State) -> None:
        self.state = state

    async def __call__(self, ctx: Any, call_next: Callable[[Any], Any]) -> Any:
        running: Running | None = None
        token = None
        phase: Phase | None = None
        tool = False
        try:
            if attr(ctx, "method") == "tools/call":
                scope = self.state.internal.calls().get()
                wrapper = scope.wrapper if scope is not None else None
                if isinstance(wrapper, Running) and scope is wrapper.scope:
                    running = wrapper
                    running.inner_result = running.inner_snapshot = None
                    tool = not high_mode(self.state)
                    if not scope.released:
                        scope.invoked = False
                        scope.thrown = None
                        scope.tool_result = None
                        if tool:
                            scope.ran = False
                    phase = Phase(scope)
                    token = HANDLING.set(phase)
        except Exception:
            running = None
        try:
            result = await call_next(ctx)
        finally:
            if phase is not None:
                # A task this step left running keeps it, ended: what it runs later isn't the call's.
                phase.ended = True
            if token is not None:
                try:
                    HANDLING.reset(token)
                except Exception:
                    pass
            if tool and running is not None:
                try:
                    if not running.scope.released:
                        running.scope.ran = True
                except Exception:
                    pass
        try:
            if running is not None:
                running.inner_result = result
                running.inner_snapshot = snapshot(result)
        except Exception:
            pass
        return result


async def _with_low_extras(state: State, listing: Any, ctx: Any, dispatch: Call) -> Any:
    tools = item(listing, "tools")
    if not isinstance(tools, list):
        return listing
    shown = await low_listed(
        state,
        tools,
        item(listing, "nextCursor") is None,
        item(attr(ctx, "params"), "cursor") is None,
        dispatch.reader,
        lambda tool: item(tool, "name"),
        lambda tool: item(tool, "inputSchema"),
        lambda tool, schema: {**tool, "inputSchema": schema},
        lambda definition: definition,
        lambda: _check_list_entry(state),
    )
    if shown is None:
        return listing
    return {**listing, "tools": shown}


def _with_extras(state: State, listing: Any) -> Any:
    tools = item(listing, "tools")
    if not isinstance(tools, list):
        return listing
    shown = listed(
        state,
        tools,
        item(listing, "nextCursor") is None,
        lambda tool: item(tool, "name"),
        lambda tool: item(tool, "inputSchema"),
        lambda tool, schema: {**tool, "inputSchema": schema},
        lambda definition: definition,
    )
    if shown is None:
        return listing
    return {**listing, "tools": shown}


class Call:
    """A `tools/call` through the outer middleware: the wire params in, the wire result out."""

    def __init__(self, state: State, ctx: Any, call_next: Callable[[Any], Any], misplaced: bool, gated: bool = False) -> None:
        self.state = state
        self.request = ctx
        self.call_next = call_next
        self.misplaced = misplaced
        #: The call's chain has the inner middleware, which marks the handler's step.
        self.gated = gated
        params = attr(ctx, "params")
        self.tool_name = item(params, "name")
        self.arguments = item(params, "arguments")
        self.context = ctx

    def with_arguments(self, arguments: dict[str, Any]) -> Any:
        ctx = self.request
        return dataclasses.replace(ctx, params={**ctx.params, "arguments": arguments})

    async def call(self, request: Any) -> Any:
        return await self.call_next(request)

    def refresh(self) -> None:
        _check_replaced(self.state)
        _check_list_entry(self.state)

    def reader(self) -> Reader | None:
        """The instance's own tools/list entry, called directly (not through middleware) with a
        copy of this request's context marked as a listing, without its `_meta`, request state or
        SSE callbacks, in a copy of this request's context variables."""
        entry = _list_entry(self.state)
        handler = attr(entry, "handler")
        params_type = attr(entry, "params_type")
        if not callable(handler) or params_type is None:
            return None
        ctx = self.request
        run_in = contextvars.copy_context()
        run_in.run(_enter_callback, current_depth() + 1, self.state.internal.calls())

        async def page(cursor: str | None) -> Any:
            params: dict[str, Any] = {"cursor": cursor} if cursor is not None else {}
            changes: dict[str, Any] = {"method": "tools/list", "params": params, "meta": None, "close_sse_stream": None, "close_standalone_sse_stream": None}
            listing = dataclasses.replace(ctx, **{name: value for name, value in changes.items() if hasattr(ctx, name)})
            return await handler(listing, params_type.model_validate(params, by_name=False))

        state = self.state
        return Reader(page, run_in, lambda: _list_entry(state) is entry)

    async def answer(self, request: Any, reply: dict[str, Any]) -> Any:
        # Through the chain: the handler entry's wrapper answers it, so the SDK shapes the reply's
        # envelope for the request's version, as for any tool.
        return await self.call_next(request)

    def client(self) -> Any:
        """On 2026-07-28 the request's `_meta`; else what the client sent in `initialize`."""
        sent = item(item(attr(self.request, "params"), "_meta"), CLIENT_INFO_KEY)
        if sent is not None:
            return sent
        return attr(attr(attr(self.request, "session"), "client_params"), "client_info")

    def version(self) -> Any:
        """The request's own version (in `_meta` on 2026-07-28), then its `mcp-protocol-version`
        header on HTTP, then the one the connection's `initialize` negotiated, as the shared spec
        orders them. A connection that never ran it (the legacy stateless path) holds only the
        version the SDK assumes, which no client declared: none, as on 1.x."""
        sent = item(item(attr(self.request, "params"), "_meta"), PROTOCOL_VERSION_KEY)
        if isinstance(sent, str):
            return sent
        headers = attr(attr(self.request, "request"), "headers")
        get = attr(headers, "get")
        try:
            header = get("mcp-protocol-version") if callable(get) else None
        except Exception:
            header = None
        if isinstance(header, str):
            return header
        if attr(attr(self.request, "session"), "client_params") is None:
            return None
        return attr(self.request, "protocol_version")

    def session(self, version: Any) -> str | None:
        """The transport's issued session ID; over SSE, which issues the connection none, one made
        for the connection once it ran `initialize` there (the request's own handshake, never
        another request's envelope: a stateless connection never runs it), decided at its first
        call and kept; none on other HTTP. On stdio, one made for the connection, which outlives
        the per-request session proxies."""
        connection = attr(attr(self.request, "session"), "_connection")
        issued = attr(connection, "session_id")
        if isinstance(issued, str) and issued != "":
            return issued
        request = attr(self.request, "request")
        if attr(request, "headers") is not None:
            if attr(attr(self.request, "session"), "client_params") is None:
                return None
            state = self.state
            return http_session(state, connection, lambda: made_session(state, connection) if sse_message(request) else None)
        if in_stdio(self.state):
            return made_session(self.state, connection)
        return None

    def continued(self) -> bool:
        params = attr(self.request, "params")
        return isinstance(item(params, "inputResponses"), Mapping) or isinstance(item(params, "requestState"), str)

    def read(self, result: Any, key: str) -> Any:
        # The wire dict the handler's step returns, or a model a middleware inside the wrapper's
        # returned in its place (the SDK dumps it by alias once the chain returns): read by alias.
        return wire(result, key)

    def refusal(self, result: Any) -> BaseException | None:
        """A middleware inside the wrapper's returned an `ErrorData`: once the chain returns, the
        SDK raises it as `MCPError`, which the client gets as a JSON-RPC error."""
        try:
            error_data = attr(loaded("mcp_types"), "ErrorData")
            if isinstance(error_data, type) and isinstance(result, error_data):
                error = attr(loaded("mcp.shared.exceptions"), "MCPError").from_error_data(result)
                return error if isinstance(error, BaseException) else None
        except Exception:
            pass
        return None

    def cancel_requested(self) -> bool:
        """The request's own channel (the one `_watch` wraps) was told of the client's
        notifications/cancelled, which mcp 2.x then never answers. Its dispatcher cancels the
        handler's scope for it too, unless built with `peer_cancel_mode="signal"`, which lets the
        handler run on."""
        flag = attr(attr(attr(self.request, "session"), "_request_outbound"), "cancel_requested")
        is_set = attr(flag, "is_set")
        try:
            return callable(is_set) and is_set() is True
        except Exception:
            return False

    def with_block(self, result: Any, text: str) -> Any:
        return {**result, "content": [*result["content"], {"type": "text", "text": text}]}

    def recorded(self, result: Any) -> Any:
        return result

    def provenance(self, running: Running, result: Any) -> bool:
        if self.misplaced or running.inner_result is not result or not unchanged(running.inner_snapshot, result):
            return False
        # The wrapper's own get_more_tools is answered at the handler entry, before fastmcp.
        registry = self.state.registry
        if registry is not None and registry.kind == "fastmcp" and not running.extras.answer:
            return _fastmcp.provenance(running)
        return True

    def jsonrpc_code(self, error: BaseException) -> int | None:
        if in_mro(error, "mcp.shared.exceptions", "MCPError"):
            code = attr(error, "code")
            return code if isinstance(code, int) and not isinstance(code, bool) else None
        # The dispatcher answers a request whose params don't validate with Invalid params.
        if in_mro(error, "pydantic_core._pydantic_core", "ValidationError"):
            return INVALID_PARAMS
        return None

    def jsonrpc_message(self, error: BaseException) -> str | None:
        if in_mro(error, "mcp.shared.exceptions", "MCPError"):
            return None
        return INVALID_PARAMS_MESSAGE if in_mro(error, "pydantic_core._pydantic_core", "ValidationError") else None


__all__ = ["Inner", "Outer", "arm", "is_low", "recognise"]
