"""Port of packages/lsquare/test/turns.test.ts. JavaScript-only values are replaced by Python's
counterparts: a set for a Map, a Decimal for a BigInt, an aware datetime for a Date."""

from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from decimal import Decimal
from typing import Any

import pytest

from lsquare._event import BuiltOk, Context, build_turn
from lsquare._normalize import turn_key
from lsquare._redact import resolve_patterns

BASE = {"conversationId": "conv1", "orgId": "org_1", "toolName": "ask_docs", "question": "How many orders?"}
CONTEXT = Context(
    sdk={"name": "lsquare", "version": "0.2.0", "language": "python"},
    patterns=resolve_patterns(None),
    redact_keys=[],
    max_event_bytes=256 * 1024,
)
RANDOM_ID = re.compile(r"[0-9a-f-]{36}")


def build(input: Any) -> Any:
    return build_turn(input, "2026-10-28T00:00:00.000Z", CONTEXT, False)


def ok(input: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, int], dict[str, int]]:
    built = build(input)
    assert isinstance(built, BuiltOk), f"expected ok, got {built.code}"
    draft = built.draft
    return draft.event, draft.notes.dropped, draft.ignored_fields, draft.notes.ignored_tool_call_fields


class TestFields:
    def test_needs_none_an_empty_turn_is_still_a_turn_with_a_random_id(self) -> None:
        body = ok({})[0]
        assert {key: body[key] for key in ("kind", "schemaVersion", "source", "status")} == {
            "kind": "turn",
            "schemaVersion": 2,
            "source": "live",
            "status": "completed",
        }
        assert RANDOM_ID.fullmatch(body["eventId"])

    def test_refuses_anything_but_a_mapping(self) -> None:
        class Turn:
            pass

        for input in [None, "question", [BASE], Turn()]:
            built = build(input)
            assert not built.ok and built.code == "invalid_input"

    def test_accepts_a_conversation_id_containing_a_slash_which_0_1_refused(self) -> None:
        assert ok({**BASE, "conversationId": "a/b"})[0]["conversationId"] == "a/b"

    def test_defaults_status_to_completed_as_0_1_did(self) -> None:
        assert ok(BASE)[0]["status"] == "completed"

    def test_gives_a_0_1_input_the_same_turn_fields(self) -> None:
        input = {
            **BASE,
            "turnId": 3,
            "answer": "Two.",
            "toolCalls": [{"name": "find", "input": {"q": "x"}, "result": "abc", "durationMs": 4}],
            "usage": {"inputTokens": 10, "outputTokens": 5, "costUsd": 0.01},
            "latency": {"totalMs": 900},
            "verification": {"passed": True},
            "client": {"name": "claude-code", "version": "2.1"},
            "apiKeyId": "key_1",
            "surface": "mcp",
            "metadata": {"plan": "pro"},
        }
        body = ok(input)[0]
        fields = {key: value for key, value in body.items() if key not in ("eventId", "schemaVersion", "kind", "capturedAt", "source", "sdk")}
        assert fields == {
            **input,
            "turnId": "3",
            "status": "completed",
            "toolCalls": [{"name": "find", "input": {"q": "x"}, "resultChars": 3, "durationMs": 4}],
        }


class TestIds:
    @pytest.mark.parametrize(
        ("field", "value"),
        [("orgId", 7), ("userId", "a\x00b"), ("conversationId", "x" * 513), ("toolName", "é" * 257), ("callId", {})],
    )
    def test_drops_the_event_for_an_id_that_is_not_a_string_has_a_nul_or_is_over_512_bytes(self, field: str, value: Any) -> None:
        built = build({**BASE, field: value})
        assert not built.ok and built.code == "invalid_id" and built.paths == [field]

    def test_keeps_ids_of_exactly_512_bytes(self) -> None:
        assert len(ok({**BASE, "conversationId": "x" * 512, "userId": "é" * 256})[0]["userId"]) == 256

    def test_measures_ids_as_textencoder_does_a_lone_surrogate_being_3_bytes(self) -> None:
        # Python only: TypeScript checks the ingestion's limit with TextEncoder, which writes U+FFFD.
        assert ok({**BASE, "userId": "\ud800" * 170 + "xx"})[0]["userId"] == "\ud800" * 170 + "xx"
        assert not build({**BASE, "userId": "\ud800" * 171}).ok
        assert ok({**BASE, "userId": ("\ud83d" + "\ude00") * 128})[0]["userId"] == "\U0001f600" * 128
        assert not build({**BASE, "userId": ("\ud83d" + "\ude00") * 129}).ok

    def test_counts_an_empty_one_as_absent_and_says_so(self) -> None:
        body, dropped, _, _ = ok({**BASE, "orgId": "", "userId": ""})
        assert "orgId" not in body and "userId" not in body
        assert dropped == {"orgId": "empty", "userId": "empty"}

    def test_sends_user_id_and_call_id_as_given_never_redacted(self) -> None:
        body = ok({**BASE, "userId": "jane@acme.io", "callId": "call-1"})[0]
        assert body["userId"] == "jane@acme.io" and body["callId"] == "call-1"

    def test_takes_a_uuid_as_its_string_in_every_id_an_app_gives(self) -> None:
        # Python only: the event is the one the string makes, its key included.
        given = uuid.UUID("12345678-1234-4234-8234-123456789ABC")
        text = "12345678-1234-4234-8234-123456789abc"
        keys = ("orgId", "userId", "conversationId", "turnId", "callId", "apiKeyId")
        as_uuids, dropped, _, _ = ok({**BASE, **{key: given for key in keys}})
        assert as_uuids == ok({**BASE, **{key: text for key in keys}})[0] and dropped == {}
        assert as_uuids["eventId"] == turn_key(text, text, text)

        class Shouting(uuid.UUID):
            def __str__(self) -> str:
                return super().__str__().upper()

        # Its canonical string, whatever a subclass writes.
        assert ok({**BASE, "userId": Shouting(text)})[0]["userId"] == text

    def test_keeps_a_uuid_tool_name_invalid_and_a_uuids_hex_a_different_id(self) -> None:
        given = uuid.uuid4()
        built = build({**BASE, "toolName": given})
        assert not built.ok and built.code == "invalid_id" and built.paths == ["toolName"]
        assert ok({**BASE, "conversationId": given.hex, "turnId": 1})[0]["eventId"] != ok({**BASE, "conversationId": given, "turnId": 1})[0]["eventId"]


class TestTurnId:
    @pytest.mark.parametrize(
        ("turn_id", "kept", "reason"),
        [
            ("t_1", "t_1", None),
            (42, "42", None),
            ("", None, "empty"),
            (1.5, None, "unsafe_integer"),
            (2**60, None, "unsafe_integer"),
            ({}, None, "wrong_type"),
            # Python only: what JavaScript would hold.
            (42.0, "42", None),
            (-0.0, "0", None),
            (True, None, "wrong_type"),
            (2**53 - 1, "9007199254740991", None),
            (2**53, None, "unsafe_integer"),
        ],
    )
    def test_turn_id(self, turn_id: Any, kept: str | None, reason: str | None) -> None:
        body, dropped, _, _ = ok({**BASE, "turnId": turn_id})
        assert body.get("turnId") == kept
        assert dropped.get("turnId") == reason


class TestEventIds:
    def test_are_k_and_the_sha256_of_org_conversation_and_turn_as_compact_json(self) -> None:
        # Computed with Python's json.dumps(separators=(",", ":"), ensure_ascii=False) and hashlib.
        assert turn_key("org_1", "conv1", "7") == "k/3f8e87b789e2e017772841b8a6b6be39f9ad5b3384435e43ff6be84ac541c1cc"
        assert turn_key(None, "conv/1", "t_1") == "k/808ca52684483385c951e9c815da6d29511a8743454656ae124ef951942cf6d0"
        assert turn_key("org_é", "c\U0001f600", "x") == "k/7667cae4d178cedaf66c9c9afd64b6397aa564fcfc08f6083d0af90633e6783d"

    def test_are_equal_for_turn_id_7_and_the_string_7_and_differ_for_two_orgs(self) -> None:
        assert ok({**BASE, "turnId": 7})[0]["eventId"] == ok({**BASE, "turnId": "7"})[0]["eventId"]
        assert ok({**BASE, "turnId": 7})[0]["eventId"] == turn_key("org_1", "conv1", "7")
        assert ok({**BASE, "orgId": "org_2", "turnId": 7})[0]["eventId"] != ok({**BASE, "turnId": 7})[0]["eventId"]

    def test_are_random_without_a_conversation_or_a_turn_id_and_hash_long_turn_ids(self) -> None:
        assert RANDOM_ID.fullmatch(ok(BASE)[0]["eventId"])
        assert RANDOM_ID.fullmatch(ok({"turnId": 1})[0]["eventId"])
        assert re.fullmatch(r"k/[0-9a-f]{64}", ok({**BASE, "turnId": "x" * 10_000})[0]["eventId"])

    def test_hash_a_lone_surrogate_as_its_json_escape(self) -> None:
        # Python only: the writer escapes it, as JSON.stringify does; UTF-8 can't write it.
        assert turn_key(None, "c", "\ud800") != turn_key(None, "c", "\ud801")


class TestToolCalls:
    def test_keeps_allowed_fields_and_reduces_a_result_to_its_length_in_code_points(self) -> None:
        body, _, _, ignored = ok(
            {
                **BASE,
                "toolCalls": [
                    {"name": "search_docs", "input": {"q": "x"}, "result": "abc\U0001f600", "resultIsError": False, "extra": 1},
                    {"name": "lookup", "input": {}, "result": {"rows": [1, 2]}, "resultChars": 3},
                ],
            }
        )
        assert body["toolCalls"] == [
            {"name": "search_docs", "input": {"q": "x"}, "resultChars": 4, "resultIsError": False},
            {"name": "lookup", "input": {}, "resultChars": 3},
        ]
        assert ignored == {"result": 2, "(other)": 1}

    def test_is_absent_not_empty_when_every_item_is_invalid(self) -> None:
        body, dropped, _, ignored = ok({**BASE, "toolCalls": [{"tool": "search_docs", "args": {}}, "x"]})
        assert "toolCalls" not in body
        assert dropped == {"toolCalls[0]": "missing_name", "toolCalls[1]": "wrong_type"}
        assert ignored == {"tool": 1, "args": 1}

    def test_keeps_an_empty_array_no_tools_were_called(self) -> None:
        assert ok({**BASE, "toolCalls": []})[0]["toolCalls"] == []

    def test_keeps_an_explicit_null_input(self) -> None:
        assert ok({**BASE, "toolCalls": [{"name": "q", "input": None}]})[0]["toolCalls"] == [{"name": "q", "input": None}]

    def test_measures_non_string_results_within_a_work_limit_and_says_why_when_it_cannot(self) -> None:
        body, dropped, _, _ = ok(
            {
                **BASE,
                "toolCalls": [
                    {"name": "map", "result": {1, 2}},
                    {"name": "big", "result": [0] * 1_100_000},
                    {"name": "small", "result": [1]},
                ],
            }
        )
        # The allowance is shared: once a big result uses it up, later results aren't measured either.
        assert body["toolCalls"] == [{"name": "map"}, {"name": "big"}, {"name": "small"}]
        assert dropped == {
            "toolCalls[0].resultChars": "not_json",
            "toolCalls[1].resultChars": "too_large",
            "toolCalls[2].resultChars": "too_large",
        }

    def test_keeps_the_first_100(self) -> None:
        body = ok({**BASE, "toolCalls": [{"name": f"t{i}"} for i in range(150)]})[0]
        assert len(body["toolCalls"]) == 100
        assert body["truncated"] == ["toolCalls"]

    def test_drops_a_non_array(self) -> None:
        body, dropped, _, _ = ok({**BASE, "toolCalls": {"name": "x"}})
        assert "toolCalls" not in body
        assert dropped["toolCalls"] == "wrong_type"

    def test_marks_an_input_with_unencodable_parts_without_exposing_keys(self) -> None:
        body, dropped, _, _ = ok({**BASE, "toolCalls": [{"name": "q", "input": {"jane@acme.io": Decimal(1), "ok": 1}}]})
        assert body["toolCalls"][0]["input"] == {"ok": 1}
        assert dropped == {"toolCalls[0].input": "not_json"}


class TestFreeFormJsonFields:
    def test_drops_a_set_instead_of_turning_it_into_an_empty_object(self) -> None:
        body, dropped, _, _ = ok({**BASE, "verification": {("a", 1)}})
        assert "verification" not in body
        assert dropped["verification"] == "not_json"

    def test_keeps_the_encodable_rest_of_a_lossy_value(self) -> None:
        circular: dict[str, Any] = {"totalMs": 10}
        circular["self"] = circular
        body, dropped, _, _ = ok({**BASE, "latency": circular})
        assert body["latency"] == {"totalMs": 10}
        assert dropped["latency"] == "not_json"

    def test_marks_unencodable_array_items_as_lossy(self) -> None:
        body, dropped, _, _ = ok({**BASE, "verification": [1, object()]})
        assert body["verification"] == [1, None]
        assert dropped["verification"] == "not_json"

    def test_drops_short_fields_over_16000_characters_instead_of_the_event(self) -> None:
        long = "x" * 16_001
        body, dropped, _, _ = ok({**BASE, "apiKeyId": long, "surface": long, "client": long, "error": {"message": "m", "type": long}})
        assert "apiKeyId" not in body
        assert body["error"] == {"message": "m"}
        assert dropped == {"apiKeyId": "too_large", "surface": "too_large", "client": "too_large", "error.type": "too_large"}

    def test_drops_binary_data_as_not_json(self) -> None:
        # As turns.test.ts does with a Buffer and a Uint8Array.
        body, dropped, _, _ = ok({**BASE, "latency": b"raw", "verification": {"at": 1, "bytes": bytearray(4)}})
        assert "latency" not in body
        assert body["verification"] == {"at": 1}
        assert dropped == {"latency": "not_json", "verification": "not_json"}

    def test_writes_an_aware_datetime_as_date_to_json_does(self) -> None:
        date = dt.datetime(2026, 10, 14, 10, 0, tzinfo=dt.timezone.utc)
        assert ok({**BASE, "verification": {"at": date}})[0]["verification"] == {"at": "2026-10-14T10:00:00.000Z"}

    def test_never_copies_a_proto_key(self) -> None:
        value = json.loads('{"__proto__": {"polluted": true}, "a": 1}')
        body, dropped, _, _ = ok({**BASE, "verification": value})
        assert body["verification"] == {"a": 1}
        assert dropped["verification"] == "not_json"


class TestOtherFields:
    def test_normalises_errors(self) -> None:
        assert ok({**BASE, "error": TypeError("boom")})[0]["error"] == {"message": "boom", "type": "TypeError"}
        assert ok({**BASE, "error": "boom"})[0]["error"] == {"message": "boom"}
        assert ok({**BASE, "error": {"message": "boom", "type": "Timeout"}})[0]["error"] == {"message": "boom", "type": "Timeout"}
        assert ok({**BASE, "error": 42})[1]["error"] == "wrong_type"

    def test_accepts_client_as_a_string_or_name_and_version(self) -> None:
        assert ok({**BASE, "client": "claude-code"})[0]["client"] == "claude-code"
        assert ok({**BASE, "client": {"name": "cursor", "version": "2.1", "x": 1}})[0]["client"] == {"name": "cursor", "version": "2.1"}
        assert ok({**BASE, "client": 7})[1]["client"] == "wrong_type"

    def test_keeps_valid_usage_fields_and_records_invalid_ones(self) -> None:
        body, dropped, _, _ = ok({**BASE, "usage": {"inputTokens": 10, "costUsd": "free"}})
        assert body["usage"] == {"inputTokens": 10}
        assert dropped == {"usage.costUsd": "wrong_type"}

    def test_keeps_primitive_metadata_only_and_drops_the_event_when_metadata_alone_is_too_big(self) -> None:
        body, dropped, _, _ = ok({**BASE, "metadata": {"a": 1, "b": "x", "c": {"nested": True}}})
        assert body["metadata"] == {"a": 1, "b": "x"}
        assert dropped["metadata"] == "not_json"
        built = build({**BASE, "metadata": {"a": "x" * 300_000}})
        assert not built.ok and built.code == "too_large"

    def test_names_unknown_fields_only_when_they_are_known_aliases(self) -> None:
        assert ok({**BASE, "foo": 1, "tool": 2, "jane@acme.io": 3})[2] == {"tool": 1, "(other)": 2}
