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


# The tool's description.

DESCRIPTION = "Search a customer's orders by status and date. Returns at most 50."


def described() -> dict[str, Any]:
    return call(input={"value": {"q": "late orders"}}, intent="find late orders", task={"id": "task_1", "status": "working"}, toolDescription=DESCRIPTION).event


def test_a_call_carries_its_tools_description_as_given_after_its_intent() -> None:
    event = described()
    assert event["toolDescription"] == DESCRIPTION
    assert "truncated" not in event and "dropped" not in event
    # Python only: TypeScript's field order (see test_writes_the_event_in_typescripts_field_order).
    keys = list(event)
    assert keys[keys.index("intent") : keys.index("intent") + 3] == ["intent", "toolDescription", "task"]
    assert "toolDescription" not in call().event


def cut_description() -> dict[str, Any]:
    return call(toolDescription=prose(1_500)).event


def test_a_description_is_cut_at_1000_characters_and_named_as_truncated() -> None:
    event = cut_description()
    assert event["toolDescription"] == f"{prose(1_000)}…[+500 chars]"
    assert event["truncated"] == ["toolDescription"]
    whole = call(toolDescription=prose(1_000)).event
    assert whole["toolDescription"] == prose(1_000) and "truncated" not in whole


def test_a_description_that_is_not_a_string_is_dropped_as_wrong_type() -> None:
    event = call(toolDescription=["Search orders"]).event
    assert "toolDescription" not in event
    assert event["dropped"] == {"toolDescription": "wrong_type"}
    assert "toolDescription" not in call(toolDescription=None).event
    assert "dropped" not in call(toolDescription=None).event


def test_a_description_is_redacted_as_the_calls_other_text_is() -> None:
    event = call(toolDescription="Ask jane@acme.io for access, then search.", input={"value": {"to": "jane@acme.io"}}).event
    assert event["toolDescription"] == "Ask [EMAIL_1] for access, then search."
    # The same address, so the same placeholder, counted where it was replaced.
    assert event["input"] == {"to": "[EMAIL_1]"}
    assert event["redactions"] == {"email": 2}


def test_limited_diagnostics_never_carry_the_description() -> None:
    assert "toolDescription" not in failed(toolDescription=DESCRIPTION).event
    assert failed(toolDescription=DESCRIPTION).event == failed().event
    # Nor one a hook adds, or leaves in.
    pending = failed(True, toolDescription=DESCRIPTION)
    assert pending.event["toolDescription"] == DESCRIPTION
    finished = after_hook({**pending.event, "toolDescription": "added"}, pending, CONTEXT)
    assert isinstance(finished, BuiltOk)
    assert "toolDescription" not in finished.draft.event and finished.draft.event["identity"] == "failed"


def test_before_send_sees_the_description_and_can_change_it_or_remove_it() -> None:
    pending = call(True, toolDescription="Ask jane@acme.io for access.", input={"value": {"q": 1}})
    # Not redacted yet: the hook sees the text.
    assert pending.event["toolDescription"] == "Ask jane@acme.io for access."
    kept = after_hook(dict(pending.event), pending, CONTEXT)
    assert isinstance(kept, BuiltOk) and kept.draft.event["toolDescription"] == "Ask [EMAIL_1] for access."

    pending = call(True, toolDescription=DESCRIPTION)
    changed = after_hook({**pending.event, "toolDescription": prose(1_200)}, pending, CONTEXT)
    assert isinstance(changed, BuiltOk)
    assert changed.draft.event["toolDescription"] == f"{prose(1_000)}…[+200 chars]"
    assert changed.draft.event["truncated"] == ["toolDescription"]

    pending = call(True, toolDescription=DESCRIPTION)
    removed = after_hook({key: value for key, value in pending.event.items() if key != "toolDescription"}, pending, CONTEXT)
    assert isinstance(removed, BuiltOk) and "toolDescription" not in removed.draft.event

    pending = call(True, toolDescription=DESCRIPTION)
    wrong = after_hook({**pending.event, "toolDescription": 7}, pending, CONTEXT)
    assert isinstance(wrong, BuiltOk) and "toolDescription" not in wrong.draft.event
    assert wrong.draft.event["dropped"] == {"beforeSend.toolDescription": "wrong_type"}


def test_the_description_is_shaped_last_so_it_never_takes_scan_budget_from_the_calls_own_data() -> None:
    # A description far past the budget: the input and the result are read first, whole.
    rows = {"rows": [prose(900) for _ in range(40)]}
    result = {"content": [{"type": "text", "text": prose(3_000)}]}
    without = call(input={"value": rows}, result={"value": result}).event
    with_one = call(input={"value": rows}, result={"value": result}, toolDescription=prose(1_200_000)).event
    assert with_one["input"] == without["input"] == rows
    assert with_one["result"] == without["result"] == result
    assert "truncated" not in without
    # Counting what was cut would use the budget up: marked as cut, without a count.
    assert with_one["toolDescription"] == f"{prose(1_000)}…[cut]"
    assert with_one["truncated"] == ["toolDescription"]

    # A result that uses the budget up leaves none for the description: everything else is as it
    # would be without one.
    spent = {"content": [{"type": "text", "text": prose(1_200_000)}]}
    without = call(input={"value": rows}, result={"value": spent}).event
    with_one = call(input={"value": rows}, result={"value": spent}, toolDescription=DESCRIPTION).event
    assert with_one["toolDescription"] == "…[cut]"
    assert with_one["truncated"] == ["result", "toolDescription"] and without["truncated"] == ["result"]
    rest = {key: value for key, value in with_one.items() if key not in ("toolDescription", "truncated")}
    assert rest == {key: value for key, value in without.items() if key != "truncated"}


def test_an_event_too_large_loses_its_description_first() -> None:
    def built(max_bytes: int, **over: Any) -> dict[str, Any]:
        context = Context(sdk=CONTEXT.sdk, patterns=CONTEXT.patterns, redact_keys=[], max_event_bytes=max_bytes)
        made = build_call(record(**over), context, False)
        assert isinstance(made, BuiltOk), made
        return made.draft.event

    values: dict[str, Any] = {
        "input": {"value": {"q": prose(600)}},
        "result": {"value": {"content": [{"type": "text", "text": prose(600)}]}},
        "agent": {"question": prose(600)},
        "toolDescription": prose(600),
    }
    whole = built(256 * 1024, **values)
    size = len(dumps(whole).encode("utf-8"))
    assert size > 2_400 and "truncated" not in whole
    # Just under its size: the description is left out, and nothing that only this call has.
    event = built(size - 1, **values)
    assert event["truncated"] == ["toolDescription"] and "toolDescription" not in event
    rest = {key: value for key, value in whole.items() if key != "toolDescription"}
    assert {key: value for key, value in event.items() if key != "truncated"} == rest
    # Smaller still: then the result, the input and the agent's turn, in the order they always went.
    event = built(1_100, **values)
    assert event["truncated"] == ["toolDescription", "result", "input"]
    assert event["agent"] == whole["agent"] and len(dumps(event).encode("utf-8")) <= 1_100
    # An event that fitted without a description is never dropped for having one.
    alone = len(dumps(built(256 * 1024)).encode("utf-8"))
    event = built(alone + 40, toolDescription=prose(600))
    assert event["truncated"] == ["toolDescription"] and "toolDescription" not in event


def test_the_ingestion_accepts_every_event() -> None:
    require_node()
    events = [
        outlined(), *errors(), call(agent=AGENT).event, oversized_call()[0], oversized_turn(), failed().event, reduced_after_hook(),
        described(), cut_description(), failed(toolDescription=DESCRIPTION).event,
    ]  # fmt: skip
    assert ingestion(events) == ["valid"] * len(events)
