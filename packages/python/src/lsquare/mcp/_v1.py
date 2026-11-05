"""mcp 1.x and fastmcp 3.x (Python spec, section 4): the low-level server's `request_handlers`.

The instance's dict becomes a subclass whose `__setitem__` (and `update()`, `setdefault()` and
`|=`) wraps the `CallToolRequest` and `ListToolsRequest` entries as they're set, so a later
`@server.call_tool()` or `@server.list_tools()` is wrapped again, as TypeScript re-arms. The
request context comes from the SDK's `request_ctx`.

Adapted, through packages/lsquare/src/mcp/index.ts, from AgentCat's TypeScript SDK (MIT, © 2025
AgentCat, Inc.; see THIRD_PARTY_NOTICES): recognising a server by its shape, hooking the handler
map and re-arming when handlers are set again.
"""

from __future__ import annotations

import contextlib
import contextvars
import dataclasses
import functools
import os
import threading
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

from . import _fastmcp
from .._client import _enter_callback, current_depth
from ._call import Running, State, high_mode, http_session, listed, low_listed, made_session, on_call, sse_message, tap_tools
from ._lists import Reader, mark
from ._read import attr, cancelled, in_mro, loaded, wire
from ._run import in_stdio
from ._weak import IdentitySet

LOWLEVEL = "mcp.server.lowlevel.server"
TYPES = "mcp.types"
#: Marks the wrapper's own handlers: what they wrap.
WRAPS = "__lsquare_wraps__"
#: On an HTTP transport's method this copy wrapped: the read streams it notes (`watch_transports`).
WATCHES = "__lsquare_watches__"


def is_low(value: Any) -> bool:
    """An mcp 1.x low-level `Server`, or a subclass such as fastmcp's: a `request_handlers` dict."""
    return in_mro(value, LOWLEVEL, "Server") and isinstance(attr(value, "request_handlers"), dict)


def recognise(server: Any) -> tuple[Any, Any, str | None] | None:
    """(low-level server, high-level server or None, registry kind) for an mcp 1.x line server,
    the app's own subclasses included."""
    low = attr(server, "_mcp_server")
    if is_low(low):
        if in_mro(server, "mcp.server.fastmcp.server", "FastMCP") and attr(server, "_tool_manager") is not None:
            return low, server, "fastmcp1"
        if in_mro(server, "fastmcp.server.server", "FastMCP") and attr(server, "_local_provider") is not None:
            return low, server, "fastmcp"
        return None
    if is_low(server):
        return server, None, None
    return None


def _request_class(key: Any, name: str) -> bool:
    return isinstance(key, type) and key.__name__ == name and key.__module__ == TYPES


class Handlers(dict[Any, Any]):
    """The low-level server's handlers, keyed by request class: setting the tools/call or
    tools/list entry wraps it."""

    def __init__(self, contents: Mapping[Any, Any], state: State) -> None:
        super().__init__()
        self._lsquare_state = state
        for key, value in contents.items():
            dict.__setitem__(self, key, _entry(state, key, value, initial=True))

    def __setitem__(self, key: Any, value: Any) -> None:
        try:
            value = _entry(self._lsquare_state, key, value, initial=False)
        except Exception:
            pass
        dict.__setitem__(self, key, value)

    def update(self, *args: Any, **kwargs: Any) -> None:
        for key, value in dict(*args, **kwargs).items():
            self[key] = value

    def setdefault(self, key: Any, default: Any = None) -> Any:
        if key not in self:
            self[key] = default
        return self[key]

    def __ior__(self, other: Any) -> Handlers:  # type: ignore[misc]
        # dict's own `|=` merges in C, past __setitem__: an entry replaced this way is wrapped too.
        self.update(other)
        return self


def arm(state: State) -> None:
    """Puts the wrapper in front of the tools/call and tools/list handlers, now and whenever
    they're set again."""
    low = state.low
    handlers = low.request_handlers
    wrapped = Handlers(handlers, state)
    low.request_handlers = wrapped
    # The experimental task handlers keep the dict they were made with: they write to this one too.
    experimental = attr(low, "_experimental_handlers")
    if experimental is not None and attr(experimental, "_request_handlers") is handlers:
        try:
            experimental._request_handlers = wrapped
        except Exception:
            pass
    if state.registry is not None and state.registry.kind == "fastmcp":
        _fastmcp.wrap_call_tool(state)
    watch_transports()


def _entry(state: State, key: Any, value: Any, initial: bool) -> Any:
    if not callable(value) or attr(value, WRAPS) is not None:
        return value
    if _request_class(key, "CallToolRequest"):
        # The high-level classes set theirs in __init__, before instrument(): a later one is the app's.
        if not initial and state.high is not None:
            state.replaced = True
        return _wrap(value, lambda original: _call_handler(state, original))
    if _request_class(key, "ListToolsRequest"):
        # Another tools/list handler: a snapshot read from the one before is out of date.
        if not initial:
            mark(state.lists, False)
        return _wrap(value, lambda original: _list_handler(state, original))
    return value


def _wrap(original: Callable[..., Any], make: Callable[[Callable[..., Any]], Callable[..., Any]]) -> Callable[..., Any]:
    handler = make(original)
    try:
        functools.update_wrapper(handler, original)
    except Exception:
        pass
    setattr(handler, WRAPS, original)
    return handler


def _call_handler(state: State, original: Callable[..., Any]) -> Callable[..., Any]:
    async def handler(request: Any) -> Any:
        try:
            dispatch = Call(state, original, request)
            context = dispatch.context
            nested = _nested(state, context)
            if not nested:
                _watch(state, context)
        except Exception:
            return await original(request)
        if nested:
            return await original(request)
        # While it runs, a wrapper entered inside it for the same request passes it through.
        key = id(context)
        added = context is not None and key not in state.dispatching
        if added:
            state.dispatching[key] = context
        try:
            return await on_call(state, dispatch, tap_tools)
        finally:
            if added:
                state.dispatching.pop(key, None)

    return handler


def _nested(state: State, context: Any) -> bool:
    """This server's wrapper is already recording this request's call, further out: a tools/call
    handler set later that calls the wrapped one (middleware) enters the wrapper again, in the
    outer call's scope, for the same request context. Another request, such as one a tool makes
    through a client, has a context of its own."""
    if context is None or state.dispatching.get(id(context)) is not context:
        return False
    scope = state.internal.calls().get()
    running = scope.wrapper if scope is not None else None
    return isinstance(running, Running) and running.context is context


def _list_handler(state: State, original: Callable[..., Any]) -> Callable[..., Any]:
    async def handler(request: Any) -> Any:
        listing = await original(request)
        # The output-schema cache fill (a None request) keeps the developer's own tools.
        if request is None:
            return listing
        try:
            context = _request_context()
            _watch(state, context)
            if not high_mode(state) and state.low_extras.any():
                return await _with_low_extras(state, listing, request, context)
            return _with_extras(state, listing)
        except Exception as error:
            if cancelled(error):
                raise
            return listing

    return handler


def _watch(state: State, context: Any) -> None:
    """Marks the snapshot out of date whenever the server sends notifications/tools/list_changed
    on this session, before sending it on as it came. Each session the wrapper sees is watched
    from then on, on the instance; never the class."""
    if not state.low_extras.any():
        return
    session = attr(context, "session")
    send = attr(session, "send_notification")
    if not callable(send) or attr(send, WRAPS) is not None:
        return
    lists = state.lists

    async def send_notification(notification: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            if wire(_unroot(notification)[0], "method") == "notifications/tools/list_changed":
                mark(lists, True)
        except Exception:
            pass
        return await send(notification, *args, **kwargs)

    try:
        setattr(send_notification, WRAPS, send)
        setattr(session, "send_notification", send_notification)
    except Exception:
        pass


def reader(state: State, context: Any) -> Reader | None:
    """How a snapshot read gets the list: the instance's own tools/list handler (what the wrapper
    wraps), called with requests built from the dict's own key class, in a copy of the starting
    request's context whose `request_ctx` holds no `_meta`, task metadata or SSE callbacks."""
    handlers = attr(state.low, "request_handlers")
    if not isinstance(handlers, dict):
        return None
    key = next((each for each in handlers if _request_class(each, "ListToolsRequest")), None)
    entry = handlers.get(key) if key is not None else None
    handler = attr(entry, WRAPS) or entry
    variable = attr(loaded(LOWLEVEL), "request_ctx")
    if key is None or not callable(handler) or variable is None:
        return None
    copy = context
    if dataclasses.is_dataclass(context) and not isinstance(context, type):
        experimental = attr(context, "experimental")
        changes: dict[str, Any] = {"meta": None, "close_sse_stream": None, "close_standalone_sse_stream": None}
        if dataclasses.is_dataclass(experimental) and not isinstance(experimental, type):
            changes["experimental"] = dataclasses.replace(experimental, task_metadata=None)
        copy = dataclasses.replace(context, **{name: value for name, value in changes.items() if hasattr(context, name)})
    run_in = contextvars.copy_context()
    run_in.run(variable.set, copy)
    run_in.run(_enter_callback, current_depth() + 1, state.internal.calls())

    async def page(cursor: str | None) -> Any:
        request = key.model_validate({"method": "tools/list", "params": {"cursor": cursor} if cursor is not None else None})
        return await handler(request)

    return Reader(page, run_in, lambda: handlers.get(key) is entry)


async def _with_low_extras(state: State, listing: Any, request: Any, context: Any) -> Any:
    root, rooted = _unroot(listing)
    tools = attr(root, "tools")
    if not isinstance(tools, list):
        return listing
    tool_class = attr(loaded(TYPES), "Tool")
    shown = await low_listed(
        state,
        tools,
        attr(root, "nextCursor") is None,
        attr(attr(request, "params"), "cursor") is None,
        lambda: reader(state, context),
        lambda tool: wire(tool, "name"),
        lambda tool: wire(tool, "inputSchema"),
        lambda tool, schema: tool.model_copy(update={"inputSchema": schema}),
        lambda definition: tool_class.model_validate(definition),
    )
    if shown is None:
        return listing
    copy = root.model_copy(update={"tools": shown})
    return type(listing)(copy) if rooted else copy


def _unroot(value: Any) -> tuple[Any, bool]:
    """A `ServerResult`'s own result, and whether it was wrapped in one."""
    if getattr(type(value), "__pydantic_root_model__", False) is True:
        return attr(value, "root"), True
    return value, False


def _with_extras(state: State, listing: Any) -> Any:
    root, rooted = _unroot(listing)
    tools = attr(root, "tools")
    if not isinstance(tools, list):
        return listing
    tool_class = attr(loaded(TYPES), "Tool")
    shown = listed(
        state,
        tools,
        attr(root, "nextCursor") is None,
        lambda tool: wire(tool, "name"),
        lambda tool: wire(tool, "inputSchema"),
        lambda tool, schema: tool.model_copy(update={"inputSchema": schema}),
        lambda definition: tool_class.model_validate(definition),
    )
    if shown is None:
        return listing
    copy = root.model_copy(update={"tools": shown})
    return type(listing)(copy) if rooted else copy


class Call:
    """A `tools/call` through the dict entry."""

    def __init__(self, state: State, original: Callable[..., Any], request: Any) -> None:
        self.state = state
        self.original = original
        self.request = request
        params = attr(request, "params")
        self.tool_name = attr(params, "name")
        self.arguments = attr(params, "arguments")
        self.context = _request_context()

    def with_arguments(self, arguments: dict[str, Any]) -> Any:
        params = self.request.params
        return self.request.model_copy(update={"params": params.model_copy(update={"arguments": arguments})})

    async def call(self, request: Any) -> Any:
        return await self.original(request)

    def refresh(self) -> None:
        # The handlers dict notes a replacement as it's written.
        pass

    def reader(self) -> Reader | None:
        return reader(self.state, self.context)

    async def answer(self, request: Any, reply: dict[str, Any]) -> Any:
        types = loaded(TYPES)
        return types.ServerResult(types.CallToolResult.model_validate(reply))  # type: ignore[union-attr]

    def client(self) -> Any:
        params = attr(attr(self.context, "session"), "client_params")
        return attr(params, "clientInfo")

    def version(self) -> Any:
        """On HTTP the request's `mcp-protocol-version` header; otherwise the version the session
        negotiated, worked out as the SDK does."""
        header = _header(self.context, "mcp-protocol-version")
        if isinstance(header, str):
            return header
        params = attr(attr(self.context, "session"), "client_params")
        if params is None:
            return None
        requested = attr(params, "protocolVersion")
        supported = attr(loaded("mcp.shared.version"), "SUPPORTED_PROTOCOL_VERSIONS")
        latest = attr(loaded(TYPES), "LATEST_PROTOCOL_VERSION")
        if isinstance(supported, (list, tuple)) and requested in supported:
            return requested
        return latest if isinstance(latest, str) else None

    def session(self, version: Any) -> str | None:
        """On HTTP, only for a session that ran its own `initialize` (a stateless one never does):
        the `mcp-session-id` header, by which Streamable HTTP routes a stateful session's
        requests; over SSE, which routes by its `session_id` query parameter and never checks that
        header, one made for the session. A session the Streamable HTTP transport opened is its
        own (`streamable`), whatever query its requests add; another is SSE's when its request
        carries SSE's query parameter. Decided at the session's first call and kept, so a request
        can't move its session to another. On stdio, one made for the session."""
        session = attr(self.context, "session")
        if _headers(self.context) is not None:
            if attr(session, "client_params") is None:
                return None

            def choose() -> str | None:
                if not streamable(session) and sse_message(attr(self.context, "request")):
                    return made_session(self.state, session)
                header = _header(self.context, "mcp-session-id")
                return header if isinstance(header, str) else None

            return http_session(self.state, session, choose)
        if in_stdio(self.state):
            return made_session(self.state, session)
        return None

    def continued(self) -> bool:
        return False

    def read(self, result: Any, key: str) -> Any:
        return wire(_unroot(result)[0], key)

    def with_block(self, result: Any, text: str) -> Any:
        root, rooted = _unroot(result)
        block = attr(loaded(TYPES), "TextContent")(type="text", text=text)
        copy = root.model_copy(update={"content": [*wire(root, "content"), block]})
        return type(result)(copy) if rooted else copy

    def recorded(self, result: Any) -> Any:
        return _unroot(result)[0]

    def refusal(self, result: Any) -> BaseException | None:
        """A handler returned an `ErrorData`: mcp 1.x responds with it as a JSON-RPC error."""
        if not in_mro(result, TYPES, "ErrorData"):
            return None
        try:
            error = attr(loaded("mcp.shared.exceptions"), "McpError")(result)
            return error if isinstance(error, BaseException) else None
        except Exception:
            return None

    def provenance(self, running: Running, result: Any) -> bool:
        # The wrapper's own get_more_tools is answered here, outside everything: nothing replaced it.
        if running.extras.answer:
            return True
        registry = self.state.registry
        if registry is not None and registry.kind == "fastmcp":
            return _fastmcp.provenance(running)
        # FastMCP 1.x has no middleware: the function's boundary is enough.
        return True

    def jsonrpc_code(self, error: BaseException) -> int | None:
        if not in_mro(error, "mcp.shared.exceptions", "McpError"):
            return None
        code = attr(attr(error, "error"), "code")
        return code if isinstance(code, int) and not isinstance(code, bool) else None

    def jsonrpc_message(self, error: BaseException) -> str | None:
        # mcp 1.x sends an McpError's own message.
        return None


def _request_context() -> Any:
    variable = attr(loaded(LOWLEVEL), "request_ctx")
    try:
        return variable.get() if variable is not None else None
    except LookupError:
        return None
    except Exception:
        return None


#: mcp 1.x's Streamable HTTP transport, as `watch_transports()` wraps it: its module, class and the
#: method that opens a connection's streams, yielding the read stream its `ServerSession` reads.
_STREAMABLE = ("mcp.server.streamable_http", "StreamableHTTPServerTransport", "connect")
#: The read streams it opened since, by identity and held weakly, by references with no callback
#: (see _weak): nothing runs as a stream is collected.
_OPENED = IdentitySet()
_WATCHING = threading.Lock()
#: How far down a method's `__wrapped__` chain this copy looks for its own wrap.
_WRAPS_SEEN = 32


def _reset_after_fork() -> None:
    # A forked child has only the thread that forked: a lock another thread held then stays held,
    # with no one to release it. Replaced, never acquired.
    global _WATCHING
    _WATCHING = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)


def _watched_here(method: Any) -> bool:
    """`method` is this copy's wrap, or wraps it (through `__wrapped__`): another copy of the SDK
    loaded in the process wraps the transport over this one's, and each copy then wraps it once,
    not again each time it looks."""
    for _ in range(_WRAPS_SEEN):
        if method is None:
            return False
        if attr(method, WATCHES) is _OPENED:
            return True
        method = attr(method, "__wrapped__")
    return False


def watch_transports() -> None:
    """Wraps mcp 1.x's Streamable HTTP transport, once the app has imported it, process-wide and
    once, so a `ServerSession` it opened is known as its own: that transport checks its requests'
    `mcp-session-id` header, and never their query parameters, which SSE routes its requests by.
    A request can carry either, so only the transport can say which one it checked. The wrap only
    notes the read stream: what the transport does is unchanged. Each copy of the SDK loaded in
    the process wraps it once, and finds its own wrap under another copy's. Never imports it, nor
    raises."""
    module, name, method = _STREAMABLE
    try:
        cls = attr(loaded(module), name)
        if not isinstance(cls, type) or _watched_here(attr(cls, method)):
            return
        with _WATCHING:
            original = cls.__dict__.get(method)
            if not callable(original) or _watched_here(original):
                return
            watched = _watched_transport(original)
            functools.update_wrapper(watched, original)
            # This copy's: another copy of the SDK wraps it once too, over or under it.
            setattr(watched, WATCHES, _OPENED)
            setattr(cls, method, watched)
    except Exception:
        pass


def _watched_transport(original: Callable[..., Any]) -> Callable[..., Any]:
    @contextlib.asynccontextmanager
    async def watched(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        async with original(*args, **kwargs) as streams:
            try:
                _OPENED.add(streams[0])
            except Exception:
                pass
            yield streams

    return watched


def streamable(session: Any) -> bool:
    """A session mcp 1.x's Streamable HTTP transport opened since it was watched, by the read
    stream it reads."""
    watch_transports()
    try:
        read = attr(session, "_read_stream")
        return read is not None and read in _OPENED
    except Exception:
        return False


def _headers(context: Any) -> Any:
    """The HTTP request's headers: present only on HTTP transports."""
    return attr(attr(context, "request"), "headers")


def _header(context: Any, name: str) -> Any:
    headers = _headers(context)
    get = attr(headers, "get")
    try:
        return get(name) if callable(get) else None
    except Exception:
        return None


__all__ = ["Handlers", "arm", "is_low", "recognise"]
