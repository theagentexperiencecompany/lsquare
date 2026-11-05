"""The wrapper's core flows on trio, through anyio, on each line whose in-memory client runs there:
a call recorded with the extras, identify (sync, as on trio only sync hooks run), the ID line and
grouping, and a low-level server's snapshot read as a trio system task. The transport tests run on
asyncio only; these say trio works end to end."""

from __future__ import annotations

import re
from typing import Any

import pytest

from lines import FAST, MAJOR, client, connect, high, low, needs_line, schema, tool
from lsquare.mcp import IdentifyInput, instrument

pytestmark = [needs_line, pytest.mark.anyio]
ID_LINE = re.compile(r"^Task ID: (ses_[A-Za-z0-9_-]{22})\. ")


@pytest.fixture
def anyio_backend() -> str:
    pytest.importorskip("trio")
    return "trio"


def search(query: str) -> str:
    return f"found {query}"


async def connected(app: Any) -> Any:
    try:
        return connect(app, mode="legacy") if MAJOR == 2 else connect(app)
    except Exception as error:  # pragma: no cover - a client that needs asyncio
        pytest.skip(f"this line's in-memory client doesn't run on trio: {error!r}")


async def test_on_trio_a_call_is_recorded_with_the_extras_and_identify() -> None:
    lsquare, events = client()
    app = high()
    tool(app, search)

    def identify(call: IdentifyInput) -> Any:
        return {"orgId": "org-trio", "userId": "user-trio"}

    instrument(app, lsquare, identify=identify)
    # fastmcp's own client starts asyncio tasks: its server is driven by the official client here.
    async with await connected(app._mcp_server if FAST else app) as connection:
        listed = {item.name: item for item in await connection.tools()}
        first = await connection.call("search", {"query": "a", "intent": "on trio"})
        match = ID_LINE.match(first.texts[-1])
        assert match is not None, first.texts
        second = await connection.call("search", {"query": "b", "session_id": match.group(1)})
    await lsquare.aflush(timeout_ms=5_000)
    assert "intent" in (schema(listed["search"]).get("properties") or {})
    assert first.texts[0] == "found a" and second.texts == ["found b"]
    one, two = events.calls()
    assert one["orgId"] == "org-trio" and one["userId"] == "user-trio" and one["intent"] == "on trio"
    assert one["input"] == {"query": "a"} and one["sessionSource"] == "issued"
    assert two["sessionId"] == match.group(1) and two["sessionSource"] == "sent"


async def test_on_trio_a_low_level_servers_snapshot_is_read_and_used() -> None:
    lsquare, events = client()
    seen: list[dict[str, Any]] = []

    async def call(name: str, arguments: dict[str, Any]) -> Any:
        seen.append(dict(arguments))
        return [{"type": "text", "text": "ok"}]

    server = low("plain", [{"name": "orders", "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}}}], call)
    instrument(server, lsquare, extras={"sessions": True, "intent": True, "missing_tools": True})
    async with await connected(server) as connection:
        listed = {item.name: item for item in await connection.tools()}
        await connection.call("orders", {"id": "1", "intent": "from the snapshot"})
    await lsquare.aflush(timeout_ms=5_000)
    assert "get_more_tools" in listed and seen == [{"id": "1"}]
    [event] = events.calls()
    assert event["intent"] == "from the snapshot"
