"""The deep review's last data findings on each line: what a capture() inside the tool reads, and a
resource block's uri, a pydantic URL on the 1.x lines."""

from __future__ import annotations

import importlib
from typing import Any

import pytest

from lines import FAST, MAJOR, client, connect, high, low, needs_line, tool
from lsquare.mcp import instrument

if FAST:
    # At the module's level: fastmcp resolves the tool's return annotation from here.
    from fastmcp.tools import ToolResult

pytestmark = [needs_line, pytest.mark.anyio]
OFF = {"sessions": False, "intent": False, "missing_tools": False}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# parity-2: a capture() inside the tool reads its data when it is called, as any capture() does.


async def test_a_capture_inside_the_tool_reads_its_data_before_it_returns() -> None:
    lsquare, events = client()
    app = high()

    def answer(question: str) -> str:
        calls = [{"name": "search", "durationMs": 5}]
        usage = {"inputTokens": 1, "outputTokens": 1}
        metadata = {"turn": "1"}
        lsquare.capture({"question": question, "conversationId": "c", "turnId": 1, "toolCalls": calls, "usage": usage, "metadata": metadata})
        # The tool reuses its objects for its next turn, then empties them, before it returns.
        calls[:] = [{"name": "fetch", "durationMs": 7}]
        usage["inputTokens"] = 99
        metadata["turn"] = "2"
        lsquare.capture({"question": "again", "conversationId": "c", "turnId": 2, "toolCalls": calls, "usage": usage, "metadata": metadata})
        usage["inputTokens"] = 999
        calls.clear()
        metadata.clear()
        return "ok"

    tool(app, answer)
    instrument(app, lsquare, extras=OFF)
    async with connect(app) as connection:
        await connection.call("answer", {"question": "why?"})
    await lsquare.aflush(timeout_ms=5_000)
    [call] = events.calls()
    [turn] = events.turns()
    assert call["agent"]["toolCalls"] == [{"name": "search", "durationMs": 5}]
    assert call["agent"]["usage"] == {"inputTokens": 1, "outputTokens": 1}
    assert call["metadata"] == {"turn": "1"}
    assert call["conversationId"] == "c" and call["turnRef"].startswith("k/")
    assert turn["toolCalls"] == [{"name": "fetch", "durationMs": 7}]
    assert turn["usage"] == {"inputTokens": 99, "outputTokens": 1} and turn["metadata"] == {"turn": "2"}


async def test_a_capture_inside_the_tool_that_couldnt_be_read_then_drops_the_call_never_reading_it_later() -> None:
    errors: list[str] = []
    lsquare, events = client(on_error=lambda error, event=None: errors.append(error.code))
    app = high()
    returned = False

    class Flaky(list):  # type: ignore[type-arg]
        """Raises until the tool returns, then reads differently."""

        def __getitem__(self, index: Any) -> Any:
            if not returned:
                raise RuntimeError("not yet")
            return {"name": "late", "durationMs": 999}

    def answer(question: str) -> str:
        nonlocal returned
        lsquare.capture({"question": question, "conversationId": "c", "toolCalls": Flaky([{}])})
        returned = True
        return "ok"

    tool(app, answer)
    instrument(app, lsquare, extras=OFF)
    async with connect(app) as connection:
        reply = await connection.call("answer", {"question": "why?"})
    await lsquare.aflush(timeout_ms=5_000)
    assert reply.texts == ["ok"]
    assert events.calls() == [] and errors == ["redaction_failed"]


# parity-3: a capture() whose ID can't be read isn't the call's, never sent without its ID.


async def test_a_capture_inside_the_tool_whose_id_cant_be_read_is_dropped_not_sent_without_it() -> None:
    class Guarded(dict):  # type: ignore[type-arg]
        """Its userId can't be read, however it's asked for: capture() reads each key once, by `get`."""

        def get(self, key: Any, default: Any = None) -> Any:
            if key == "userId":
                raise ValueError("no")
            return super().get(key, default)

        def __getitem__(self, key: Any) -> Any:
            if key == "userId":
                raise ValueError("no")
            return super().__getitem__(key)

        def __contains__(self, key: object) -> bool:
            if key == "userId":
                raise ValueError("no")
            return super().__contains__(key)

    errors: list[str] = []
    lsquare, events = client(on_error=lambda error, event=None: errors.append(error.code))
    app = high()

    def answer(question: str) -> str:
        lsquare.capture(Guarded(userId="u1", conversationId="c", question="private"))
        return "ok"

    tool(app, answer)
    instrument(app, lsquare, extras=OFF)
    async with connect(app) as connection:
        await connection.call("answer", {"question": "why?"})
    await lsquare.aflush(timeout_ms=5_000)
    [call] = events.calls()
    assert "agent" not in call and "userId" not in call and "conversationId" not in call
    assert errors == ["internal_error"]


# parity-7: a resource link's and an embedded resource's uri, a pydantic URL on the 1.x lines.


def _blocks() -> list[Any]:
    # The line's own types: on the 1.x lines a resource's uri is a pydantic AnyUrl.
    types: Any = importlib.import_module("mcp.types" if MAJOR == 1 else "mcp_types")
    return [
        types.ResourceLink(type="resource_link", uri="file:///docs/readme.md", name="readme", mimeType="text/markdown"),
        types.EmbeddedResource(type="resource", resource=types.TextResourceContents(uri="file:///a.txt", mimeType="text/plain", text="hello")),
        types.EmbeddedResource(type="resource", resource=types.BlobResourceContents(uri="file:///b.bin", mimeType="application/octet-stream", blob="aGVsbG8=")),
    ]


EXPECTED = [
    {"type": "resource_link", "uri": "file:///docs/readme.md", "name": "readme", "mimeType": "text/markdown"},
    {"type": "resource", "uri": "file:///a.txt", "mimeType": "text/plain", "text": "hello"},
    {"type": "resource", "uri": "file:///b.bin", "mimeType": "application/octet-stream", "bytes": 5},
]


async def test_resource_blocks_keep_their_uri_on_a_high_level_server() -> None:
    lsquare, events = client()
    app = high()
    if FAST:

        def docs() -> ToolResult:
            return ToolResult(content=_blocks())
    else:

        def docs() -> list:  # type: ignore[misc,type-arg]
            return _blocks()

    tool(app, docs, name="docs")
    instrument(app, lsquare, extras=OFF)
    async with connect(app) as connection:
        await connection.call("docs", {})
    await lsquare.aflush(timeout_ms=5_000)
    [event] = events.calls()
    assert event["result"]["content"] == EXPECTED and "dropped" not in event


async def test_resource_blocks_keep_their_uri_on_a_low_level_server() -> None:
    lsquare, events = client()

    async def call(name: str, arguments: dict[str, Any]) -> Any:
        return {"content": [block.model_dump(mode="json", by_alias=True, exclude_none=True) for block in _blocks()]}

    app = low("docs", [{"name": "docs", "inputSchema": {"type": "object"}}], call)
    instrument(app, lsquare, extras=OFF)
    async with connect(app) as connection:
        await connection.call("docs", {})
    await lsquare.aflush(timeout_ms=5_000)
    [event] = events.calls()
    assert event["result"]["content"] == EXPECTED and "dropped" not in event
