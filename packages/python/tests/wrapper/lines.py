"""The MCP SDK line installed, and one way to build, connect to and call a server on each.

Each test environment has one line (`uv run --group mcp1 pytest tests/wrapper`, and `mcp2`, `fastmcp3`,
`fastmcp4`); tests that need one skip without it.
"""

from __future__ import annotations

import importlib.metadata
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest

from lsquare import LSquare
from lsquare._sending import Reply


def _version(dist: str) -> str | None:
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        return None


FASTMCP = _version("fastmcp")
MCP = _version("mcp")
LINE: str | None = (
    "fastmcp4" if FASTMCP and FASTMCP.startswith("4.")
    else "fastmcp3" if FASTMCP and FASTMCP.startswith("3.")
    else "mcp2" if MCP and MCP.startswith("2.")
    else "mcp1" if MCP and MCP.startswith("1.")
    else None
)  # fmt: skip
#: The official SDK's major version underneath.
MAJOR = 2 if LINE in ("mcp2", "fastmcp4") else 1
FAST = LINE in ("fastmcp3", "fastmcp4")

needs_line = pytest.mark.skipif(LINE is None, reason="no MCP SDK installed")


def only(*lines: str) -> pytest.MarkDecorator:
    return pytest.mark.skipif(LINE not in lines, reason=f"needs {' or '.join(lines)}")


class Events:
    """A fake ingestion: records every event the client sends, acknowledging each."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def __call__(self, body: bytes, timeout: float, abort: Any) -> Reply:
        batch = json.loads(body.decode("utf-8"))["events"]
        self.events.extend(batch)
        ack = {"accepted": [event["eventId"] for event in batch], "duplicates": [], "rejected": []}
        return Reply(200, None, json.dumps(ack))

    def calls(self) -> list[dict[str, Any]]:
        return [event for event in self.events if event.get("kind") == "tool_call"]

    def turns(self) -> list[dict[str, Any]]:
        return [event for event in self.events if event.get("kind") == "turn"]


def client(**options: Any) -> tuple[LSquare, Events]:
    events = Events()
    options.setdefault("api_key", "lsq_test_key")
    options.setdefault("endpoint", "http://localhost:1")
    return LSquare(_transport=events, **options), events


def high(name: str = "test") -> Any:
    """The line's high-level server."""
    if LINE == "mcp1":
        from mcp.server.fastmcp import FastMCP

        return FastMCP(name)
    if LINE == "mcp2":
        from mcp.server.mcpserver import MCPServer

        return MCPServer(name)
    from fastmcp import FastMCP as Fast

    return Fast(name)


def tool(app: Any, fn: Callable[..., Any], name: str | None = None, **options: Any) -> Any:
    """Registers `fn` as a tool, as the line's decorator does."""
    if FAST:
        return app.tool(fn, name=name, **options) if name else app.tool(fn, **options)
    return app.tool(name=name, **options)(fn) if name else app.tool(**options)(fn)


@dataclass
class Result:
    texts: list[str]
    is_error: bool
    structured: Any
    raw: Any


def _result(raw: Any) -> Result:
    content = getattr(raw, "content", None) or []
    texts = [getattr(block, "text", None) for block in content]
    is_error = getattr(raw, "is_error", None)
    if is_error is None:
        is_error = getattr(raw, "isError", False)
    structured = getattr(raw, "structured_content", None)
    if structured is None:
        structured = getattr(raw, "structuredContent", None)
    return Result([text for text in texts if isinstance(text, str)], bool(is_error), structured, raw)


def schema(listed: Any) -> dict[str, Any]:
    value = getattr(listed, "input_schema", None)
    if value is None:
        value = getattr(listed, "inputSchema", None)
    return value if isinstance(value, dict) else {}


class Connection:
    def __init__(self, session: Any) -> None:
        self.session = session

    async def tools(self) -> list[Any]:
        listed = await self.session.list_tools()
        return list(getattr(listed, "tools", listed))

    async def names(self) -> list[str]:
        return [item.name for item in await self.tools()]

    async def call(self, name: str, arguments: dict[str, Any] | None = None) -> Result:
        if type(self.session).__module__.startswith("fastmcp"):
            raw = await self.session.call_tool(name, arguments or {}, raise_on_error=False)
        else:
            raw = await self.session.call_tool(name, arguments or {})
        return _result(raw)


@asynccontextmanager
async def connect(app: Any, mode: str | None = None) -> AsyncIterator[Connection]:
    """An in-memory client session with the server, through the SDK's own client (the official
    one for a low-level server on the fastmcp lines). `mode` picks mcp 2.x's protocol era:
    "legacy" or "2026-07-28" (the SDK's default)."""
    # fastmcp's client for fastmcp's own FastMCP (and the app's subclasses); the official one for
    # anything else, a fastmcp server's low-level server included.
    official = not FAST or not any(cls.__module__ == "fastmcp.server.server" and cls.__name__ == "FastMCP" for cls in type(app).__mro__)
    if MAJOR == 1 and official:
        from mcp.shared.memory import create_connected_server_and_client_session

        low = getattr(app, "_mcp_server", app)
        async with create_connected_server_and_client_session(low) as session:
            yield Connection(session)
        return
    if MAJOR == 2 and official:
        from mcp.client.client import Client

        async with (Client(app, mode=mode) if mode else Client(app)) as session:
            yield Connection(session)
        return
    from fastmcp import Client as FastClient

    # fastmcp 4.x's client picks the era as the official 2.x one does; 3.x's has no such option.
    async with (FastClient(app, mode=mode) if mode and MAJOR == 2 else FastClient(app)) as session:
        yield Connection(session)


def low(name: str, tools: list[dict[str, Any]], call: Callable[..., Any]) -> Any:
    """A low-level server listing `tools` (wire dicts) and answering calls with `call(name,
    arguments)`, which returns content blocks as dicts, or a whole result dict."""
    if MAJOR == 1:
        import mcp.types as types
        from mcp.server.lowlevel import Server

        server: Any = Server(name)

        @server.list_tools()
        async def list_tools() -> list[Any]:
            return [types.Tool.model_validate(each) for each in tools]

        @server.call_tool()
        async def call_tool(tool_name: str, arguments: dict[str, Any]) -> Any:
            out = await call(tool_name, arguments)
            if isinstance(out, dict):
                return types.CallToolResult.model_validate(out)
            return [types.TextContent.model_validate(block) for block in out]

        return server
    import mcp_types
    from mcp.server.lowlevel import Server as Low

    async def on_list_tools(ctx: Any, params: Any) -> Any:
        return mcp_types.ListToolsResult.model_validate({"tools": tools})

    async def on_call_tool(ctx: Any, params: Any) -> Any:
        out = await call(params.name, params.arguments or {})
        return mcp_types.CallToolResult.model_validate(out if isinstance(out, dict) else {"content": out})

    return Low(name, on_list_tools=on_list_tools, on_call_tool=on_call_tool)
