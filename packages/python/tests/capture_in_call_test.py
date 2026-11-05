"""capture() inside a call, where three groups of fixes meet: each top-level field is read once
(py-runtime-13) and strictly (parity-3), through the reader a capture outside a call uses, the
agent part is shaped as capture() is called (parity-2), and a later capture whose callId is left
out, None or empty is linked to the call (parity-9), as is one after a run returned (py-mcp-8).
Core only: no MCP SDK, so every Python the core runs on runs it."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

import pytest

from helpers import FakeTransport, Recorder
from lsquare import LSquare
from lsquare._client import CallScope
from lsquare._event import Captured, ShapedAgent

KEY = "lsq_" + "a" * 40
CALL_ID = "b3c2f0a4-1111-4111-8111-000000000001"
AT = "2026-10-31T00:00:00.000Z"


def client(transport: FakeTransport, recorder: Recorder) -> LSquare:
    return LSquare(api_key=KEY, endpoint="http://localhost:1", _transport=transport, logger=recorder, on_error=recorder.on_error)


def inside(lsquare: LSquare, scope: CallScope, fields: Mapping[str, Any]) -> None:
    calls = lsquare._lsquare_internal().calls()
    token = calls.set(scope)
    try:
        lsquare.capture(fields)
    finally:
        calls.reset(token)


def call_record(agent: Any) -> dict[str, Any]:
    """The call's record, as the wrapper builds it once the tool returns."""
    return {"callId": CALL_ID, "capturedAt": AT, "toolName": "ask", "status": "completed", "durationMs": 1, "ids": {}, "diagnostics": False, "agent": agent}


class ReadOnce(Mapping[str, Any]):
    """Keeps the ABC's `__contains__`, which reads the value through `__getitem__`. Each value is
    there for one read: a second raises, and not as `KeyError`, since the key is there, as a lazy
    proxy's might. A key that isn't there is `KeyError`, read or not."""

    def __init__(self, **data: Any) -> None:
        self.data = data
        self.reads: dict[str, int] = {}

    def __getitem__(self, key: str) -> Any:
        if key not in self.data:
            raise KeyError(key)
        self.reads[key] = self.reads.get(key, 0) + 1
        if self.reads[key] > 1:
            raise RuntimeError(f"{key} was read already")
        return self.data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(list(self.data))

    def __len__(self) -> int:
        return len(self.data)


class Unreadable(Mapping[str, Any]):
    """One key is there, but its read raises: `__contains__` (the ABC's) and `get` raise too."""

    def __init__(self, unreadable: str, **data: Any) -> None:
        self.unreadable = unreadable
        self.data = data

    def __getitem__(self, key: str) -> Any:
        if key == self.unreadable:
            raise ValueError(f"{key} can't be read")
        return self.data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(list(self.data))

    def __len__(self) -> int:
        return len(self.data)


def test_each_field_is_read_once_inside_a_call_as_outside_one_so_a_second_read_that_would_raise_never_happens() -> None:
    transport, recorder = FakeTransport(), Recorder()
    lsquare = client(transport, recorder)
    scope = CallScope(call_id=CALL_ID)
    agent = ReadOnce(userId="u1", conversationId="c", question="agent", answer="a")
    later = ReadOnce(userId="u1", conversationId="c", question="later", callId=None)
    outside = ReadOnce(userId="u1", conversationId="c", question="outside")
    inside(lsquare, scope, agent)
    inside(lsquare, scope, later)
    lsquare.capture(outside)
    for fields in (agent, later, outside):
        assert fields.reads and set(fields.reads.values()) == {1}, fields.reads
    # The agent part: its IDs for the wrapper, and what was read then, never the app's mapping.
    captured = scope.agent
    assert isinstance(captured, Captured) and dict(captured) == {"userId": "u1", "conversationId": "c"}
    assert isinstance(captured.snapshot, ShapedAgent) and captured.snapshot.body["question"] == "agent"
    lsquare._lsquare_internal().record(call_record(captured))
    lsquare.flush(timeout_ms=2_000)
    by_question = {event.get("question", event.get("agent", {}).get("question")): event for event in transport.events}
    assert by_question["agent"]["kind"] == "tool_call" and by_question["agent"]["agent"]["answer"] == "a"
    assert by_question["later"]["callId"] == CALL_ID and by_question["later"]["userId"] == "u1"
    assert "callId" not in by_question["outside"] and by_question["outside"]["userId"] == "u1"
    assert recorder.codes() == []
    lsquare.shutdown(timeout_ms=0)


def test_a_capture_whose_user_id_cant_be_read_is_dropped_inside_a_call_as_outside_never_sent_without_it() -> None:
    transport, recorder = FakeTransport(), Recorder()
    lsquare = client(transport, recorder)
    scope = CallScope(call_id=CALL_ID)
    inside(lsquare, scope, Unreadable("userId", userId="u1", conversationId="c", question="private"))
    # Dropped before the slot is taken: the call goes without it, and a later capture is the agent.
    assert scope.agent is None
    assert recorder.wait_for(lambda: recorder.codes() == ["internal_error"])
    inside(lsquare, scope, {"userId": "u2", "conversationId": "c", "question": "agent"})
    assert dict(scope.agent or {}) == {"userId": "u2", "conversationId": "c"}
    # As a later capture: dropped too, never sent linked to the call without its userId.
    inside(lsquare, scope, Unreadable("userId", userId="u1", conversationId="c", question="private", callId=None))
    # One whose callId can't be read isn't linked to the call in its place: it's dropped.
    inside(lsquare, scope, Unreadable("callId", userId="u1", conversationId="c", question="private", callId="call-elsewhere"))
    lsquare.capture(Unreadable("userId", userId="u1", conversationId="c", question="private"))
    lsquare._lsquare_internal().record(call_record(scope.agent))
    lsquare.flush(timeout_ms=2_000)
    [event] = transport.events
    assert event["kind"] == "tool_call" and event["agent"]["question"] == "agent"
    # Inside the call each is capture()'s own failure; outside, the turn is dropped as it's built.
    assert recorder.wait_for(lambda: sorted(recorder.codes()) == ["internal_error"] * 3 + ["redaction_failed"])
    lsquare.shutdown(timeout_ms=0)


@pytest.mark.parametrize(
    ("given", "linked"),
    [
        pytest.param({}, CALL_ID, id="left-out"),
        pytest.param({"callId": None}, CALL_ID, id="none"),
        pytest.param({"callId": ""}, CALL_ID, id="empty"),
        pytest.param({"callId": "empty"}, "empty", id="literal-empty"),
        pytest.param({"callId": "call-elsewhere"}, "call-elsewhere", id="given"),
    ],
)
def test_a_later_capture_is_linked_to_the_call_unless_it_gives_a_call_id(given: dict[str, Any], linked: str) -> None:
    transport, recorder = FakeTransport(), Recorder()
    lsquare = client(transport, recorder)
    scope = CallScope(call_id=CALL_ID)
    inside(lsquare, scope, {"conversationId": "c", "question": "agent"})
    later = ReadOnce(conversationId="c", question="later", **given)
    inside(lsquare, scope, later)
    assert set(later.reads.values()) == {1}
    lsquare.flush(timeout_ms=2_000)
    [event] = transport.events
    assert event["question"] == "later" and event["callId"] == linked and "dropped" not in event
    assert recorder.codes() == []
    lsquare.shutdown(timeout_ms=0)


def test_a_later_capture_whose_call_id_isnt_a_string_is_kept_for_the_checks_and_dropped() -> None:
    transport, recorder = FakeTransport(), Recorder()
    lsquare = client(transport, recorder)
    scope = CallScope(call_id=CALL_ID)
    inside(lsquare, scope, {"conversationId": "c", "question": "agent"})
    inside(lsquare, scope, {"conversationId": "c", "question": "later", "callId": 42})
    lsquare.flush(timeout_ms=2_000)
    assert transport.events == []
    assert recorder.wait_for(lambda: recorder.codes() == ["invalid_id"])
    lsquare.shutdown(timeout_ms=0)


def test_after_a_run_returned_a_capture_with_call_id_none_is_a_turn_linked_to_the_call_read_once() -> None:
    transport, recorder = FakeTransport(), Recorder()
    lsquare = client(transport, recorder)
    scope = CallScope(call_id=CALL_ID)
    # Middleware after the tool's function returned: outside the run, though the call goes on.
    scope.ran = True
    after = ReadOnce(conversationId="c", question="after", callId=None)
    inside(lsquare, scope, after)
    assert scope.agent is None and set(after.reads.values()) == {1}
    lsquare.flush(timeout_ms=2_000)
    [event] = transport.events
    assert event["question"] == "after" and event["callId"] == CALL_ID
    lsquare.shutdown(timeout_ms=0)
