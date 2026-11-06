"""JSON in a text block, as a real server sends it: a tool that returns a dict, on each MCP SDK line.
What it becomes in general is in the shared cases (packages/contract/cases/redaction.json)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from lines import client, connect, high, needs_line, tool
from lsquare.mcp import instrument

pytestmark = [needs_line, pytest.mark.anyio]
OFF = {"sessions": False, "intent": False, "missing_tools": False}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def lookup(query: str) -> dict[str, str]:
    return {"db_password": "hunter2", "plan": query}


async def test_a_dict_result_hides_what_is_under_secret_key_names_and_the_client_still_gets_it() -> None:
    lsquare, events = client()
    app = high()
    tool(app, lookup)
    instrument(app, lsquare, extras=OFF)
    async with connect(app) as connection:
        reply = await connection.call("lookup", {"query": "pro"})
    await lsquare.aflush(timeout_ms=5_000)
    assert any("hunter2" in text for text in reply.texts)
    [event] = events.calls()
    assert "hunter2" not in json.dumps(event)
    blocks = event["result"]["content"]
    assert any("[SECRET_1]" in block["text"] and "pro" in block["text"] for block in blocks)
    # Every copy of the value gets one number: its text, and its structured twin where the line gives one.
    assert "[SECRET_2]" not in json.dumps(event)
    await lsquare.ashutdown()


def _needs_a_field(must: str, password: str = "", note: str = "") -> str:
    return "ok"


def _takes_a_number(password: int) -> str:
    return "ok"


@pytest.mark.parametrize(
    ("function", "arguments"),
    [
        (_needs_a_field, {"password": "hunter2-canary", "note": "short"}),
        (_needs_a_field, {"password": "hunter2-canary", "note": "a note long enough for pydantic to cut the input short"}),
        (_needs_a_field, {"note": "a note long enough for pydantic to cut the input short", "password": "hunter2-canary"}),
        (_takes_a_number, {"password": "hunter2-canary"}),
    ],
)
async def test_a_refusal_quoting_the_arguments_keeps_no_secret(function: Any, arguments: dict[str, str]) -> None:
    lsquare, events = client()
    app = high()
    tool(app, function)
    instrument(app, lsquare, extras=OFF)
    async with connect(app) as connection:
        reply = await connection.call(function.__name__, arguments)
    assert reply.is_error
    await lsquare.aflush(timeout_ms=5_000)
    [event] = events.calls()
    assert event["status"] == "error" and "hunter2-canary" not in json.dumps(event)
    await lsquare.ashutdown()
