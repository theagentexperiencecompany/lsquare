"""Where the wrapper gets a server's instructions from, without an MCP SDK: its low-level
server's `instructions`, read without ever raising, and the `instructions` option. On each SDK
line, with real servers: tests/wrapper/instructions_test.py."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from helpers import FakeTransport
from lsquare import LSquare
from lsquare.mcp import _call, _lists


class Loud(str):
    """A str whose own code must never run where the wrapper reads or keeps it."""

    def __eq__(self, other: object) -> bool:
        raise AssertionError("compared by its own __eq__")

    def __hash__(self) -> int:
        raise AssertionError("hashed by its own __hash__")

    def __str__(self) -> str:
        raise AssertionError("written by its own __str__")


def state(lsquare: LSquare, low: Any, **options: Any) -> _call.State:
    internal = lsquare._lsquare_internal()
    settings = _call.read_options(internal, None, True, True, None, None, False, **options)
    return _call.State(internal=internal, settings=settings, line="v1", low=low, high=None, registry=None, lists=_lists.Lists())


def client() -> LSquare:
    return LSquare(api_key="lsq_test_key", endpoint="http://localhost:1", _transport=FakeTransport())


def test_a_servers_instructions_are_its_low_level_servers_own_when_text_that_isnt_empty() -> None:
    lsquare = client()
    try:
        held = SimpleNamespace(instructions="Search before you refund.")
        made = state(lsquare, held)
        assert _call.instructions_of(made) == "Search before you refund."
        # Read as the instance holds them now, never kept.
        held.instructions = "Ask before you refund."
        assert _call.instructions_of(made) == "Ask before you refund."
        for nothing in (None, "", 7, ["Search first."], b"Search first.", object()):
            held.instructions = nothing
            assert _call.instructions_of(made) is None, nothing
        # A server that has no such attribute at all.
        assert _call.instructions_of(state(lsquare, SimpleNamespace())) is None
        assert _call.instructions_of(state(lsquare, None)) is None
    finally:
        lsquare.shutdown(timeout_ms=0)


def test_a_str_subclass_is_read_as_plain_text_and_a_read_that_raises_gives_none() -> None:
    class Raises:
        @property
        def instructions(self) -> str:
            raise RuntimeError("no")

    lsquare = client()
    try:
        found = _call.instructions_of(state(lsquare, SimpleNamespace(instructions=Loud("Search first."))))
        assert type(found) is str and found == "Search first."
        assert _call.instructions_of(state(lsquare, Raises())) is None
    finally:
        lsquare.shutdown(timeout_ms=0)


def test_the_option_is_a_plain_switch_that_only_false_turns_off() -> None:
    lsquare = client()
    try:
        low = SimpleNamespace(instructions="Search first.")
        assert state(lsquare, low).settings.instructions is True
        assert state(lsquare, low, instructions=False).settings.instructions is False
        asked: list[str] = []

        def never(tool_name: str) -> bool:
            asked.append(tool_name)
            return False

        # Not a function of the tool, as the instructions aren't a tool's: one given is never asked.
        for on in (True, None, 0, "", "no", never):
            assert state(lsquare, low, instructions=on).settings.instructions is True, on
        assert asked == []
        # The other options are read as before, whatever this one is.
        off = state(lsquare, low, instructions=False).settings
        assert off.descriptions("any") is True and off.inputs("any") is True and off.results("any") is True
    finally:
        lsquare.shutdown(timeout_ms=0)
