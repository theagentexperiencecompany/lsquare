"""Records every `tools/call` of an MCP server (Python spec, section 4), ported from
packages/lsquare/src/mcp/index.ts.

`instrument(server, lsquare)` takes a server of the official MCP SDK, 1.x (low-level `Server`,
`FastMCP`) or 2.x (low-level `Server`, `MCPServer`), or of fastmcp 3.x or 4.x, and hooks the SDK's
protocol dispatch, outside everything else. No MCP SDK is imported here: servers are recognised by
their shape.

Adapted, through index.ts, from AgentCat's TypeScript SDK (MIT, © 2025 AgentCat, Inc.; see
THIRD_PARTY_NOTICES): recognising a server by its shape.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any, Literal, TypeVar, cast

from .. import _jsjson
from .._client import Internal
from . import _run, _v1, _v2
from ._call import NO_EXTRAS, Extras, IdentifyInput, State, read_options, warn_serverless
from ._extras import INTENT, MISSING_TOOLS, SESSION_ID
from ._read import attr
from ._registry import Kind, Registry

#: Marks a low-level server already instrumented, by any copy of the SDK.
INSTRUMENTED = "__lsquare_instrumented__"

T = TypeVar("T")


def instrument(
    server: T,
    lsquare: Any,
    *,
    identify: Callable[[IdentifyInput], Any] | None = None,
    inputs: bool | Callable[[str], bool] = True,
    results: bool | Callable[[str], bool] = True,
    extras: Literal[False] | dict[str, bool] | None = None,
    wait_until: Callable[[Any], Any] | None = None,
    flush_before_reply: bool = False,
) -> T:
    """Records every `tools/call` of `server` through `lsquare`. Returns the server it was given.
    Never raises: a server it doesn't recognise, or a disabled client, leaves the server as it is,
    with a warning. Instrumenting a server twice does nothing the second time. Nothing it does can
    make a tool call fail."""
    try:
        internal = _internal_of(lsquare)
        if internal is None:
            _warn("[lsquare] instrument() needs an LSquare client: the server is left as it is.")
            return server
        if internal.mode == "disabled":
            internal.warn_once("instrument-disabled", "[lsquare] No api_key: instrument() leaves servers as they are.")
            return server
        found = _v2.recognise(server)
        line = "v2"
        if found is None:
            found = _v1.recognise(server)
            line = "v1"
        if found is None:
            internal.warn_once("instrument-unknown", "[lsquare] instrument() doesn't recognise this server: it is left as it is.")
            return server
        low, high, kind = found
        if attr(low, INSTRUMENTED) is not None:
            return server
        # Tracking calls needs the client's context variable: from here, for this client.
        internal.calls()
        settings = read_options(internal, identify, inputs, results, extras, wait_until, flush_before_reply)
        state = State(
            internal=internal,
            settings=settings,
            line=line,
            low=low,
            high=high,
            registry=Registry(high, cast(Kind, kind)) if high is not None and kind is not None else None,
            # A low-level server's tool definitions exist only in its own tools/list handler.
            high_extras=settings.extras if high is not None else NO_EXTRAS,
            low_extras=settings.explicit,
        )
        setattr(low, INSTRUMENTED, state)
        if line == "v2":
            _v2.arm(state)
        else:
            _v1.arm(state)
        _run.wrap_stdio(state)
        warn_serverless(settings, internal)
        if internal.mode == "dry-run":
            internal.log("info", f"[lsquare dry-run] {_jsjson.dumps(_dry_run(state))}")
    except Exception:
        # Instrumenting must never break the app: whatever was installed passes calls through.
        pass
    return server


def _internal_of(client: Any) -> Internal | None:
    """The client's internal API, by its shape: never `isinstance`, so two copies of the SDK still meet."""
    try:
        get = getattr(client, "_lsquare_internal", None)
        internal = get() if callable(get) else None
        return internal if callable(getattr(internal, "record", None)) else None
    except Exception:
        return None


def _warn(message: str) -> None:
    try:
        print(message, file=sys.stderr)
    except Exception:
        pass


def _dry_run(state: State) -> dict[str, Any]:
    """What dry run says of a server: what it records, and what the extras can change."""
    name = attr(state.low, "name")
    line: dict[str, Any] = {
        "server": name if isinstance(name, str) else "unnamed",
        "level": "high" if state.high is not None else "low",
        "records": "every tools/call",
    }
    if state.high is None:
        low = state.low_extras
        if not low.any():
            line["extras"] = (
                'off on a low-level server; extras={"sessions": True, "intent": True, "missing_tools": True} turns them on, '
                "for a server whose instances list and serve their own tools, the same to every request"
            )
            return line
        on = [name for name, flag in ((SESSION_ID, low.sessions), (INTENT, low.intent), (MISSING_TOOLS, low.missing_tools and state.settings.inputs_mode != "off")) if flag]
        low_line: dict[str, Any] = {
            "on": on,
            "from": "the server's own tool list: every page, read before anything changes and again after it changes; a call waits at most 1 second for it",
            "for": "instances that list the same tools to every request they serve",
        }
        if MISSING_TOOLS in on and state.settings.inputs_mode == "asked":
            low_line[MISSING_TOOLS] = "only where the inputs function answers true for it"
        if state.line == "v2":
            low_line["2026-07-28"] = "requests on 2026-07-28 or later are passed on unchanged: their change notifications go where the wrapper can't see them"
        line["extras"] = low_line
        return line
    extras = state.high_extras
    if not extras.any():
        line["extras"] = "off"
        return line
    # One read of the registry for every tool it names.
    read = state.registry.read() if state.registry is not None else None
    names = (read.names() if read is not None else None) or []
    owners = [read.ownership(name) if read is not None else None for name in names]

    def can(name: str) -> str:
        count = sum(1 for owner in owners if owner is not None and owner.readable and name not in owner.declared)
        return f"{count} of the {len(names)} tools registered so far"

    described: dict[str, str] = {}
    if extras.sessions:
        described[SESSION_ID] = can(SESSION_ID)
    if extras.intent:
        described[INTENT] = can(INTENT)
    if extras.sessions or extras.intent:
        described["later"] = "tools registered later follow the same rules, and a tool whose listed schema can't take an input is listed as it is"
    if extras.missing_tools:
        described[MISSING_TOOLS] = (
            "not added: the server has its own"
            if MISSING_TOOLS in names
            else "not added: its input wouldn't be recorded"
            if state.settings.inputs_mode == "off"
            else "added while the server has an enabled tool of its own, and the inputs function answers true for it"
            if state.settings.inputs_mode == "asked"
            else "added while the server has an enabled tool of its own"
        )
    if extras.sessions:
        described["idLine"] = "added to a task's first result when its call can't be grouped by its protocol session or a conversationId"
    line["extras"] = described
    return line


__all__ = ["Extras", "IdentifyInput", "instrument"]
