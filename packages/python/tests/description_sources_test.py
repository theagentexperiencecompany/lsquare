"""Where the wrapper gets a tool's description from, without an MCP SDK: the registry's read of
it, and what an instance keeps of the lists it gave (`_lists.describe`). On each SDK line, with
real servers: tests/wrapper/descriptions_test.py."""

from __future__ import annotations

import asyncio
import contextvars
from types import SimpleNamespace
from typing import Any

import pytest

from helpers import FakeTransport
from lsquare import LSquare
from lsquare.mcp import _call, _lists
from lsquare.mcp._registry import Read


class Loud(str):
    """A str whose own code must never run where the wrapper compares or keeps it."""

    def __eq__(self, other: object) -> bool:
        raise AssertionError("compared by its own __eq__")

    def __hash__(self) -> int:
        raise AssertionError("hashed by its own __hash__")


def registry(tools: dict[str, Any]) -> Read:
    # FastMCP 1.x and MCPServer: one registry, `_tool_manager._tools`, by name.
    return Read(SimpleNamespace(_tool_manager=SimpleNamespace(_tools=tools)), "fastmcp1")


def test_the_registry_gives_a_registered_tools_own_description_when_it_is_a_string_that_isnt_empty() -> None:
    read = registry({
        "search": SimpleNamespace(description="Search the orders."),
        "empty": SimpleNamespace(description=""),
        "none": SimpleNamespace(description=None),
        "number": SimpleNamespace(description=7),
        "without": SimpleNamespace(),
        "loud": SimpleNamespace(description=Loud("Find orders.")),
    })  # fmt: skip
    assert read.description("search") == "Search the orders."
    for name in ("empty", "none", "number", "without", "unregistered"):
        assert read.description(name) is None, name
    # A str subclass is read as its text: none of its own code runs.
    found = read.description("loud")
    assert found == "Find orders." and type(found) is str


def test_the_registry_gives_none_where_it_cant_tell_or_a_read_raises() -> None:
    class Raises:
        @property
        def description(self) -> str:
            raise RuntimeError("no")

    assert registry({"search": Raises()}).description("search") is None
    # No registry to read at all.
    assert Read(SimpleNamespace(), "fastmcp1").description("search") is None


def test_registrations_of_one_name_must_agree_on_the_description() -> None:
    read = registry({})
    # As fastmcp's versions of one tool: every registration a call of the name can run.
    same = [SimpleNamespace(description="Search."), SimpleNamespace(description="Search.")]
    other = [SimpleNamespace(description="Search."), SimpleNamespace(description="Search, newest first.")]
    one_without = [SimpleNamespace(description="Search."), SimpleNamespace(description=None)]
    for found, expected in ((same, "Search."), (other, None), (one_without, None)):
        read.tools = lambda name, found=found: found  # type: ignore[method-assign,misc]
        assert read.description("search") == expected


def test_an_instance_keeps_each_tools_description_as_it_last_listed_it() -> None:
    lists = _lists.Lists(describes=True)
    _lists.describe(lists, "orders", "Look orders up.")
    _lists.describe(lists, "refunds", "Issue a refund.")
    assert _lists.described(lists, "orders") == "Look orders up."
    _lists.describe(lists, "orders", "Look orders up by ID.")
    assert _lists.described(lists, "orders") == "Look orders up by ID."
    # Listed without one, or with something that isn't one: it has none from then on.
    for nothing in (None, "", 7, ["Look orders up."], object()):
        _lists.describe(lists, "orders", "Look orders up.")
        _lists.describe(lists, "orders", nothing)
        assert _lists.described(lists, "orders") is None, nothing
    assert _lists.described(lists, "refunds") == "Issue a refund."
    assert _lists.described(lists, "unknown") is None and _lists.described(lists, 7) is None
    # Another handler: nothing the one before listed is kept.
    _lists.forget(lists)
    assert lists.described == {} and _lists.described(lists, "refunds") is None


def test_a_name_or_a_description_of_a_str_subclass_is_kept_as_plain_text() -> None:
    lists = _lists.Lists(describes=True)
    _lists.describe(lists, Loud("orders"), Loud("Look orders up."))
    assert lists.described == {"orders": "Look orders up."}
    assert all(type(key) is str and type(value) is str for key, value in lists.described.items())
    assert _lists.described(lists, Loud("orders")) == "Look orders up."
    # A name that isn't a string names no tool.
    _lists.describe(lists, 7, "A number.")
    _lists.describe(lists, None, "Nothing.")
    assert lists.described == {"orders": "Look orders up."}


def test_with_descriptions_off_nothing_is_noted() -> None:
    lists = _lists.Lists()
    _lists.describe(lists, "orders", "Look orders up.")
    assert lists.described == {} and _lists.described(lists, "orders") is None


def test_no_more_names_are_kept_than_a_list_may_have(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_lists, "MAX_TOOLS", 2)
    lists = _lists.Lists(describes=True)
    for name in ("a", "b", "c"):
        _lists.describe(lists, name, f"Tool {name}.")
    assert lists.described == {"a": "Tool a.", "b": "Tool b."}
    # One already kept is still updated, and one let go makes room.
    _lists.describe(lists, "a", "Tool a, again.")
    _lists.describe(lists, "b", None)
    _lists.describe(lists, "c", "Tool c.")
    assert lists.described == {"a": "Tool a, again.", "c": "Tool c."}


def state(lsquare: LSquare, descriptions: Any = True) -> _call.State:
    internal = lsquare._lsquare_internal()
    settings = _call.read_options(internal, None, True, True, None, None, False, descriptions)
    return _call.State(
        internal=internal, settings=settings, line="v1", low=object(), high=None, registry=None,
        lists=_lists.Lists(describes=settings.descriptions_mode != "off"),
    )  # fmt: skip


def test_a_listing_is_noted_as_it_is_and_never_fails_for_it() -> None:
    lsquare = LSquare(api_key="lsq_test_key", endpoint="http://localhost:1", _transport=FakeTransport())
    try:
        made = state(lsquare)
        tools = [
            {"name": "orders", "description": "Look orders up."},
            {"name": "bare"},
            {"description": "No name."},
            "not a tool",
        ]
        before = [dict(tool) if isinstance(tool, dict) else tool for tool in tools]

        def name_of(tool: Any) -> Any:
            return tool["name"]  # Raises for the last two: the rest of the page is unaffected by it.

        _call.note_listed(made, tools, lambda tool: tool.get("name") if isinstance(tool, dict) else None, lambda tool: tool.get("description"))
        assert made.lists.described == {"orders": "Look orders up."} and tools == before
        assert _call.description_of(made, "orders") == "Look orders up."
        assert _call.description_of(made, "bare") is None
        # A reader that raises stops the noting, never the listing.
        _call.note_listed(made, [{"name": "refunds", "description": "Issue one."}, {}], name_of, lambda tool: tool["description"])
        assert made.lists.described == {"orders": "Look orders up.", "refunds": "Issue one."}
        # Not a list: nothing to note.
        _call.note_listed(made, None, name_of, name_of)
        _call.note_listed(made, {"orders": {}}, name_of, name_of)
        assert set(made.lists.described) == {"orders", "refunds"}

        # Off: no tool's description is read at all.
        off = state(lsquare, descriptions=False)
        assert off.settings.descriptions_mode == "off" and off.settings.descriptions("orders") is False

        def unread(tool: Any) -> Any:
            raise AssertionError("read with descriptions off")

        _call.note_listed(off, tools, unread, unread)
        assert off.lists.described == {} and _call.description_of(off, "orders") is None
    finally:
        lsquare.shutdown(timeout_ms=0)


def test_the_option_is_read_as_inputs_and_results_are() -> None:
    lsquare = LSquare(api_key="lsq_test_key", endpoint="http://localhost:1", _transport=FakeTransport())
    try:
        assert state(lsquare).settings.descriptions_mode == "on"
        assert state(lsquare).settings.descriptions("any") is True
        asked = state(lsquare, descriptions=lambda name: name == "orders").settings
        assert asked.descriptions_mode == "asked"
        assert asked.descriptions("orders") is True and asked.descriptions("refunds") is False
        # Anything but False keeps them on, as for inputs and results.
        assert state(lsquare, descriptions=None).settings.descriptions("any") is True
    finally:
        lsquare.shutdown(timeout_ms=0)


def test_the_wrappers_own_read_of_a_list_notes_what_it_lists_and_a_description_it_cant_read_fails_nothing() -> None:
    class Unreadable(dict[str, Any]):
        def __contains__(self, key: object) -> bool:
            if key == "description":
                raise RuntimeError("no")
            return super().__contains__(key)

    pages: dict[str | None, dict[str, Any]] = {
        None: {"tools": [{"name": "orders", "description": "Look orders up.", "inputSchema": {"type": "object"}}, {"name": "bare", "inputSchema": {"type": "object"}}], "nextCursor": "2"},
        "2": {"tools": [Unreadable(name="odd", inputSchema={"type": "object"}), {"name": "refunds", "description": "Issue a refund.", "inputSchema": {"type": "object"}}]},
    }  # fmt: skip

    async def page(cursor: str | None) -> Any:
        return pages[cursor]

    noted: list[tuple[str, Any]] = []
    reader = _lists.Reader(page, contextvars.copy_context(), None, lambda name, description: noted.append((name, description)))
    snapshot = asyncio.run(_lists._collect(reader))
    assert list(snapshot.tools) == ["orders", "bare", "odd", "refunds"]
    assert [name for name, _ in noted] == ["orders", "bare", "refunds"]
    assert noted[0][1] == "Look orders up." and noted[2][1] == "Issue a refund."
    # The tool listed without one: told so, whatever stands for "absent".
    lists = _lists.Lists(describes=True)
    _lists.describe(lists, "bare", "An older one.")
    for name, description in noted:
        _lists.describe(lists, name, description)
    assert lists.described == {"orders": "Look orders up.", "refunds": "Issue a refund."}
    # Without the callback (descriptions off), no description is read: the same snapshot.
    plain = asyncio.run(_lists._collect(_lists.Reader(page, contextvars.copy_context())))
    assert list(plain.tools) == list(snapshot.tools)
