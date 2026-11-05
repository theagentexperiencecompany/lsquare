"""Checks the wheel installed alone, with pip, in a fresh venv (spec section 7: on Python 3.10 mcp
2.x and fastmcp 4.x resolve older versions of their own dependencies, and the wheel must install
cleanly with what resolves there). Run with that venv's Python:

    <venv>/bin/python scripts/clean_install.py core|mcp2|fastmcp4

- core: the wheel alone imports, declares no dependency, and records a turn in dry run;
- mcp2, fastmcp4: an instrumented server answers one in-memory call, recorded as a tool call.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import sys
import threading
from typing import Any

import lsquare
from lsquare import LSquare
from lsquare._sending import Reply
from lsquare.mcp import instrument


def fail(message: str) -> None:
    print(f"FAIL {message}")
    raise SystemExit(1)


class Lines:
    """A logger that keeps its lines and says when one comes: lines arrive on the client's own thread."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.came = threading.Event()

    def info(self, message: str) -> None:
        self.lines.append(message)
        self.came.set()

    def warn(self, message: str) -> None:
        self.lines.append(message)
        self.came.set()


def recording() -> tuple[LSquare, list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []

    def transport(body: bytes, timeout: float, abort: Any) -> Reply:
        batch = json.loads(body)["events"]
        events.extend(batch)
        return Reply(200, None, json.dumps({"accepted": [event["eventId"] for event in batch], "duplicates": [], "rejected": []}))

    return LSquare(api_key="lsq_clean_install", endpoint="http://localhost:1", _transport=transport), events


def core() -> None:
    if importlib.metadata.requires("lsquare"):
        fail(f"the wheel declares dependencies: {importlib.metadata.requires('lsquare')}")
    logger = Lines()
    client = LSquare(dry_run=True, logger=logger)
    client.capture({"conversationId": "c", "turnId": 1, "question": "q", "answer": "a"})
    client.shutdown(timeout_ms=2_000)
    logger.came.wait(5)
    if not any(line.startswith("[lsquare dry-run]") and '"kind":"turn"' in line for line in logger.lines):
        fail(f"no dry-run line for the turn: {logger.lines}")


async def mcp2() -> None:
    from mcp.client.client import Client
    from mcp.server.mcpserver import MCPServer

    client, events = recording()
    app = MCPServer("clean")

    @app.tool()
    def search(query: str) -> str:
        return f"found {query}"

    instrument(app, client)
    async with Client(app) as session:
        reply = await session.call_tool("search", {"query": "a", "intent": "clean install"})
    await client.ashutdown(timeout_ms=5_000)
    check_call(events, reply)


async def fastmcp4() -> None:
    from fastmcp import Client, FastMCP

    client, events = recording()
    app = FastMCP("clean")

    @app.tool
    def search(query: str) -> str:
        return f"found {query}"

    instrument(app, client)
    async with Client(app) as session:
        reply = await session.call_tool("search", {"query": "a", "intent": "clean install"}, raise_on_error=False)
    await client.ashutdown(timeout_ms=5_000)
    check_call(events, reply)


def check_call(events: list[dict[str, Any]], reply: Any) -> None:
    """The tool answered as itself, and the call was recorded as completed, without the extras."""
    texts = [getattr(block, "text", None) for block in getattr(reply, "content", None) or []]
    if getattr(reply, "is_error", None) or not texts or texts[0] != "found a":
        fail(f"the tool didn't answer as itself: {reply}")
    calls = [event for event in events if event.get("kind") == "tool_call"]
    if len(calls) != 1 or calls[0].get("toolName") != "search" or calls[0].get("input") != {"query": "a"}:
        fail(f"the call wasn't recorded as expected: {calls}")
    if calls[0].get("status") != "completed" or calls[0].get("intent") != "clean install":
        fail(f"the call's event isn't a completed call with its intent: {calls[0]}")


def main() -> None:
    case = sys.argv[1] if len(sys.argv) == 2 else ""
    if "site-packages" not in (lsquare.__file__ or ""):
        fail(f"lsquare imports from {lsquare.__file__}, not an installed wheel")
    if case == "core":
        core()
    elif case == "mcp2":
        asyncio.run(mcp2())
    elif case == "fastmcp4":
        asyncio.run(fastmcp4())
    else:
        print("Usage: python scripts/clean_install.py core|mcp2|fastmcp4")
        raise SystemExit(2)
    versions = {name: importlib.metadata.version(name) for name in ("lsquare", "mcp", "fastmcp", "pydantic") if _installed(name)}
    print(f"ok   {case} on Python {sys.version.split()[0]}: {versions}")


def _installed(name: str) -> bool:
    try:
        importlib.metadata.version(name)
        return True
    except importlib.metadata.PackageNotFoundError:
        return False


if __name__ == "__main__":
    main()
