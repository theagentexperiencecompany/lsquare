"""Port of packages/lsquare/test/calls.test.ts. What the ingestion says about each event, as the
contract's validator has it, is checked in one node process, at the end
(`test_the_ingestion_accepts_every_event`)."""

from __future__ import annotations

import math
import uuid
from typing import Any

from lsquare._event import BuiltOk, Context, Draft, after_hook, build_call, build_turn
from lsquare._jsjson import dumps
from lsquare._normalize import turn_key
from lsquare._redact import resolve_patterns

from cases_test import ingestion, require_node

CONTEXT = Context(
    sdk={"name": "lsquare", "version": "0.2.0", "language": "python", "runtime": "python/3.12"},
    patterns=resolve_patterns(None),
    redact_keys=[],
    max_event_bytes=256 * 1024,
)


def prose(chars: int) -> str:
    return ("lorem ipsum " * math.ceil(chars / 12))[:chars]


def record(**over: Any) -> dict[str, Any]:
    return {
        "callId": "b3c2f0a4-1111-4111-8111-000000000001",
        "capturedAt": "2026-10-28T00:00:00.000Z",
        "toolName": "search_orders",
        "status": "completed",
        "durationMs": 12,
        "ids": {},
        "diagnostics": False,
        **over,
    }


def call(hook: bool = False, **over: Any) -> Draft:
    built = build_call(record(**over), CONTEXT, hook)
    assert isinstance(built, BuiltOk), built
    return built.draft


# A tool_call event.


def outlined() -> dict[str, Any]:
    return call(
        ids={"orgId": "org_1", "userId": "u_1", "conversationId": "conv/1"},
        turnRef=turn_key("org_1", "conv/1", "3"),
        server={"name": "orders", "version": "1.0.0", "other": "x"},
        client={"name": "claude-code", "version": "2.1"},
        protocolVersion="2026-07-28",
        sessionId="ses_abcdefghijklmnopqrstuv",
        sessionSource="issued",
        input={"value": {"q": "late orders"}},
        result={"value": {"content": [{"type": "text", "text": "3 orders"}], "isError": False}},
        round="continuation",
        task={"id": "task_1", "status": "working"},
        intent="Find the late orders of this customer",
        metadata={"plan": "pro"},
    ).event


def test_carries_the_call_its_ids_and_its_outline_with_the_call_id_as_its_event_id() -> None:
    assert outlined() == {
        "eventId": "b3c2f0a4-1111-4111-8111-000000000001",
        "schemaVersion": 2,
        "kind": "tool_call",
        "capturedAt": "2026-10-28T00:00:00.000Z",
        "source": "live",
        "sdk": CONTEXT.sdk,
        "callId": "b3c2f0a4-1111-4111-8111-000000000001",
        "toolName": "search_orders",
        "orgId": "org_1",
        "userId": "u_1",
        "conversationId": "conv/1",
        "turnRef": turn_key("org_1", "conv/1", "3"),
        "server": {"name": "orders", "version": "1.0.0"},
        "client": {"name": "claude-code", "version": "2.1"},
        "protocolVersion": "2026-07-28",
        "sessionId": "ses_abcdefghijklmnopqrstuv",
        "sessionSource": "issued",
        "status": "completed",
        "durationMs": 12,
        "round": "continuation",
        "task": {"id": "task_1", "status": "working"},
        "input": {"q": "late orders"},
        "result": {"content": [{"type": "text", "text": "3 orders"}], "isError": False},
        "intent": "Find the late orders of this customer",
        "metadata": {"plan": "pro"},
    }


def test_writes_the_event_in_typescripts_field_order() -> None:
    # Python only: the same JSON text, byte for byte, as TypeScript builds its object in this order.
    assert list(outlined()) == [
        "eventId", "schemaVersion", "kind", "capturedAt", "source", "sdk", "callId", "toolName", "orgId", "userId",
        "conversationId", "turnRef", "server", "client", "protocolVersion", "sessionId", "sessionSource", "status",
        "durationMs", "round", "input", "result", "intent", "task", "metadata",
    ]  # fmt: skip


def errors() -> list[dict[str, Any]]:
    thrown = call(status="error", error={"message": "boom for jane@acme.io", "type": "TypeError", "category": "thrown"}).event
    protocol = call(status="error", error={"message": "Unknown tool", "code": -32602, "category": "protocol"}).event
    # An isError result's text is left out with the results: the category still says what happened.
    hidden = call(status="error", error={"category": "tool_error"}).event
    return [thrown, protocol, hidden]


def test_records_errors_with_their_category_and_a_code_only_when_given_one() -> None:
    thrown, protocol, hidden = errors()
    assert thrown["error"] == {"message": "boom for [EMAIL_1]", "type": "TypeError", "category": "thrown"}
    assert protocol["error"] == {"message": "Unknown tool", "code": -32602, "category": "protocol"}
    assert hidden["error"] == {"category": "tool_error"}


AGENT = {
    "question": "How many?", "answer": "Two.", "turnId": 3, "status": "completed", "error": {"message": "none"},
    "toolCalls": [{"name": "find", "input": {"q": "x"}, "resultChars": 3}], "usage": {"inputTokens": 1, "outputTokens": 2, "costUsd": 0.1},
    "latency": {"totalMs": 9}, "verification": {"passed": True}, "client": "claude-code", "apiKeyId": "key_1", "surface": "mcp",
    "orgId": "org_1", "conversationId": "c", "toolName": "ask_docs",
}  # fmt: skip


def test_keeps_every_0_1_capture_field_in_agent_and_the_turns_ids_out_of_it() -> None:
    event = call(agent=AGENT).event
    fields = {key: value for key, value in AGENT.items() if key not in ("orgId", "conversationId", "toolName")}
    assert event["agent"] == {**fields, "turnId": "3"}


def test_refuses_a_tool_name_the_ingestion_would() -> None:
    built = build_call(record(toolName="x" * 513), CONTEXT, False)
    assert not built.ok and built.code == "invalid_id"


# Events over 256 KB.


def big() -> list[str]:
    return [f"{i} {prose(15_000)}" for i in range(30)]


def oversized_call() -> tuple[dict[str, Any], dict[str, Any]]:
    agent = {"question": prose(15_000), "answer": prose(15_000), "toolCalls": [{"name": f"t{i}", "input": {"q": prose(15_000)}} for i in range(20)]}
    event = call(
        input={"value": {"rows": big()}},
        result={"value": {"structuredContent": {"rows": [f"{i} {prose(3_900)}" for i in range(80)]}}},
        agent=agent,
    ).event
    return event, agent


def test_a_tool_call_loses_its_result_then_its_input_then_its_agent_tool_calls_then_its_agent() -> None:
    event, agent = oversized_call()
    # The result was also shortened, where the scan budget ran out, so it's named once.
    assert event["truncated"] == ["result", "input", "agent.toolCalls"]
    assert "result" not in event and "input" not in event
    assert "toolCalls" not in event["agent"]
    assert event["agent"]["question"] == agent["question"]
    assert len(dumps(event)) <= 256 * 1024


def oversized_turn() -> dict[str, Any]:
    built = build_turn(
        {"question": prose(15_000), "answer": prose(15_000), "toolCalls": [{"name": f"t{i}", "input": {"rows": [prose(15_000)]}} for i in range(20)]},
        "2026-10-28T00:00:00.000Z",
        CONTEXT,
        False,
    )
    assert isinstance(built, BuiltOk)
    return built.draft.event


def test_a_turn_loses_its_tool_calls_then_its_answer_then_its_question() -> None:
    event = oversized_turn()
    assert event["truncated"] == ["toolCalls"]
    assert "toolCalls" not in event
    assert event["answer"] == prose(15_000)


# Limited diagnostics.


def failed(hook: bool = False, **over: Any) -> Draft:
    values: dict[str, Any] = {
        "diagnostics": True,
        "status": "error",
        "ids": {"orgId": "org_1"},
        "input": {"value": {"email": "jane@acme.io"}},
        "result": {"value": {"content": [{"type": "text", "text": "no"}]}},
        "error": {"message": "Not found: jane@acme.io", "code": 404, "category": "tool_error"},
        "client": {"name": "jane's-laptop"},
        "server": {"name": "acme-orders"},
        "protocolVersion": "2026-07-28",
        "sessionId": "ses_abcdefghijklmnopqrstuv",
        "intent": "look someone up",
        "agent": {"question": "who?"},
        "metadata": {"tenant": "acme"},
        **over,
    }
    return call(hook, **values)


def test_diagnostics_keep_only_what_every_event_needs_the_outline_and_the_error_category() -> None:
    assert failed().event == {
        "eventId": "b3c2f0a4-1111-4111-8111-000000000001",
        "kind": "tool_call",
        "schemaVersion": 2,
        "capturedAt": "2026-10-28T00:00:00.000Z",
        "source": "live",
        "sdk": CONTEXT.sdk,
        "callId": "b3c2f0a4-1111-4111-8111-000000000001",
        "toolName": "search_orders",
        "status": "error",
        "durationMs": 12,
        "identity": "failed",
        "error": {"category": "tool_error"},
    }


def reduced_after_hook() -> dict[str, Any]:
    pending = call(True, diagnostics=True, ids={"orgId": "org_1"}, input={"value": {"q": 1}})
    # Before the hook, the event is whole: the hook sees it as a call.
    assert "input" in pending.event
    out = {**pending.event, "input": {"added": True}, "metadata": {"added": True}, "orgId": "other"}
    finished = after_hook(out, pending, CONTEXT)
    assert isinstance(finished, BuiltOk)
    return finished.draft.event


def test_diagnostics_are_reduced_after_before_send_so_a_hook_cannot_add_anything_back() -> None:
    assert sorted(reduced_after_hook()) == [
        "callId", "capturedAt", "durationMs", "eventId", "identity", "kind", "schemaVersion", "sdk", "source", "status", "toolName",
    ]  # fmt: skip
    assert failed().event["identity"] == "failed"


# before_send.


def test_before_send_cant_change_a_calls_identity_or_outline_but_can_change_its_content_and_the_clients_name() -> None:
    pending = call(True, ids={"orgId": "org_1", "userId": "u_1"}, client={"name": "laptop"}, input={"value": {"q": "jane@acme.io"}}, intent="x")
    # Not redacted yet: the hook sees the data, as in 0.1.
    assert pending.event["input"] == {"q": "jane@acme.io"}
    rest = {key: value for key, value in pending.event.items() if key not in ("client", "intent")}
    out = {**rest, "userId": "someone_else", "status": "error", "toolName": "other", "input": {"q": "bob@acme.io"}}
    finished = after_hook(out, pending, CONTEXT)
    assert isinstance(finished, BuiltOk)
    event = finished.draft.event
    assert (event["userId"], event["status"], event["toolName"], event["input"]) == ("u_1", "completed", "search_orders", {"q": "[EMAIL_1]"})
    assert "client" not in event and "intent" not in event
    assert event["redactions"] == {"email": 1}


def test_before_send_can_give_a_calls_agent_part_a_uuid_as_its_string_and_its_turn_id_stays() -> None:
    # Python only: the hook is the app's code, so its UUIDs are read as capture()'s are.
    given = uuid.UUID("12345678-1234-4234-8234-123456789abc")
    pending = call(True, agent={"question": "q", "turnId": "t_1"})
    before = pending.event["agent"]
    assert before["turnId"] == "t_1" and "apiKeyId" not in before
    out = {**pending.event, "agent": {**before, "apiKeyId": given, "turnId": given}}
    finished = after_hook(out, pending, CONTEXT)
    assert isinstance(finished, BuiltOk)
    assert finished.draft.event["agent"] == {**before, "apiKeyId": str(given)}
    assert finished.draft.notes.dropped == {}


def test_before_send_keeps_a_turns_ids_and_the_markers_of_what_was_cut_and_cuts_again_only_what_it_made_longer() -> None:
    pending = build_turn({"orgId": "org_1", "conversationId": "c", "turnId": 1, "question": "q", "answer": prose(17_000)}, "2026-10-28T00:00:00.000Z", CONTEXT, True)
    assert isinstance(pending, BuiltOk)
    before = pending.draft.event
    assert before["answer"] == f"{prose(16_000)}…[+1000 chars]"
    assert before["truncated"] == ["answer"]
    out = {**before, "orgId": "org_2", "question": prose(20_000), "truncated": []}
    finished = after_hook(out, pending.draft, CONTEXT)
    assert isinstance(finished, BuiltOk)
    event = finished.draft.event
    assert event["eventId"] == turn_key("org_1", "c", "1")
    assert event["orgId"] == "org_1"
    assert event["answer"] == f"{prose(16_000)}…[+1000 chars]"
    assert event["question"] == f"{prose(16_000)}…[+4000 chars]"
    assert event["truncated"] == ["answer", "question"]


def test_the_ingestion_accepts_every_event() -> None:
    require_node()
    events = [outlined(), *errors(), call(agent=AGENT).event, oversized_call()[0], oversized_turn(), failed().event, reduced_after_hook()]
    assert ingestion(events) == ["valid"] * len(events)
