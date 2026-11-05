"""Sends a Python client's events to a real ingestion, for LSquare's end-to-end checks against its
ingestion, which run a copy of it with a venv holding the wheel and mcp 1.x. The key comes from
LSQUARE_KEY, never the command line.

    LSQUARE_KEY=... <venv>/bin/python send.py --endpoint <url> --org <orgId> --conversation <id>

A FastMCP server (mcp 1.x), instrumented with the extras and an `identify`, answers five calls
in memory: a search with an email and an intent (its reply gives a task ID), the same search with
that ID sent back, a tool that captures its agent's turn, one that raises, and get_more_tools. Then
a turn of its own. Prints one JSON line: the task ID, the reply's texts, what the flush counted,
and the SDK's errors. Exits 1 unless delivery is acknowledged: every event held at a gate until
`aflush()` has counted it, then all of them sent, none failed or pending, no errors reported.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import threading
from typing import Any

from lsquare import LSquare, LSquareError
from lsquare.mcp import instrument
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

ID_LINE = re.compile(r"^Task ID: (ses_[A-Za-z0-9_-]{22})\. ")
#: The five calls below (ask's turn is its agent part) and the turn of its own: what the flush must count as sent.
EVENTS = 6


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--org", required=True)
    parser.add_argument("--conversation", required=True)
    args = parser.parse_args()
    key = os.environ.get("LSQUARE_KEY", "")
    if not re.fullmatch(r"lsq_[A-Za-z0-9_-]{43}", key):
        print("LSQUARE_KEY must hold a project key", file=sys.stderr)
        return 2

    errors: list[LSquareError] = []
    # Each event waits here until aflush() has counted it: the flush then proves its delivery.
    gate = threading.Event()

    def before_send(event: dict[str, Any]) -> dict[str, Any]:
        # Closed until released, however long the calls take: an event let through early could be
        # sent before the flush counts it.
        gate.wait()
        return event

    lsquare = LSquare(
        api_key=key,
        endpoint=args.endpoint,
        before_send=before_send,
        # Long enough for the gate: the events wait at it while the session runs.
        before_send_timeout_ms=30_000,
        on_error=lambda error, event=None: errors.append(error),
        logger=_Quiet(),
    )
    app = FastMCP("orders-python")

    @app.tool()
    def search(q: str) -> str:
        return f"3 orders for {q}"

    @app.tool()
    def ask(question: str) -> str:
        lsquare.capture({"conversationId": args.conversation, "turnId": 1, "question": question, "answer": "On its way."})
        return "On its way."

    @app.tool()
    def boom() -> str:
        raise ValueError("Lookup failed for bob@example.com")

    instrument(app, lsquare, identify=lambda call: {"orgId": args.org, "userId": f"user_{args.org}"})
    try:
        return await _send(args, app, lsquare, gate, errors)
    finally:
        # Whatever happened, nothing is left waiting at the gate.
        gate.set()


async def _send(args: argparse.Namespace, app: FastMCP, lsquare: LSquare, gate: threading.Event, errors: list[LSquareError]) -> int:
    texts: list[list[str]] = []
    async with create_connected_server_and_client_session(app._mcp_server) as session:

        async def call(name: str, arguments: dict[str, Any]) -> list[str]:
            result = await session.call_tool(name, arguments)
            out = [block.text for block in result.content if getattr(block, "type", None) == "text"]
            texts.append(out)
            return out

        first = await call("search", {"q": "carol@example.com", "intent": "Find the orders of carol@example.com"})
        match = ID_LINE.match(first[-1]) if first else None
        task_id = match.group(1) if match else None
        await call("search", {"q": "late", "session_id": task_id})
        await call("ask", {"question": "Where is my order?"})
        await call("boom", {})
        await call("get_more_tools", {"need": "Refund an order"})
    lsquare.capture({"orgId": args.org, "conversationId": args.conversation, "turnId": 2, "question": "A turn of its own"})
    # Released only once aflush() has taken its snapshot of what's queued, before its first await.
    asyncio.get_running_loop().call_soon(gate.set)
    flushed = await lsquare.aflush(timeout_ms=15_000)
    await lsquare.ashutdown()
    report = {
        "taskId": task_id,
        "texts": texts,
        "flushed": {"sent": flushed.sent, "failed": flushed.failed, "pending": flushed.pending},
        "errors": [error.code for error in errors],
    }
    print(json.dumps(report))
    delivered = flushed.sent == EVENTS and flushed.failed == 0 and flushed.pending == 0 and not errors
    return 0 if delivered and task_id is not None else 1


class _Quiet:
    def info(self, message: str) -> None:
        pass

    def warn(self, message: str) -> None:
        print(message, file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
