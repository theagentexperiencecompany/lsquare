"""Port of packages/lsquare/test/shorten.test.ts."""

from __future__ import annotations

import itertools
import math
import re
import time
from collections.abc import Iterator
from typing import Any

import pytest

from lsquare._event import BuiltOk, Context, build_call, build_turn
from lsquare._redact import TEXT_FIELD, Redactor, read_keys, resolve_keys, resolve_patterns
from lsquare._shape import CUT_MARK, Cut, binary_size, new_state, shape_json, shape_string

ALL = resolve_patterns(None)
SDK = {"name": "lsquare", "version": "0.2.0", "language": "python"}


def context(**over: Any) -> Context:
    values: dict[str, Any] = {"sdk": SDK, "patterns": ALL, "redact_keys": [], "max_event_bytes": 256 * 1024, **over}
    return Context(**values)


def prose(chars: int) -> str:
    """Prose: spaces keep it from looking like base64."""
    return ("lorem ipsum " * math.ceil(chars / 12))[:chars]


def turn(input: dict[str, Any], **over: Any) -> dict[str, Any]:
    built = build_turn(input, "2026-10-28T00:00:00.000Z", context(**over), False)
    assert isinstance(built, BuiltOk), built
    return built.draft.event


def call(over: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "callId": "b3c2f0a4-1111-4111-8111-000000000001",
        "capturedAt": "2026-10-28T00:00:00.000Z",
        "toolName": "search_orders",
        "status": "completed",
        "durationMs": 12,
        "ids": {},
        "diagnostics": False,
        **(over or {}),
    }
    built = build_call(record, context(**extra), False)
    assert isinstance(built, BuiltOk), built
    return built.draft.event


class TestCuttingStrings:
    def test_cuts_at_16000_code_points_in_a_turn_ending_with_the_exact_count_of_what_was_cut(self) -> None:
        event = turn({"answer": prose(20_000)})
        assert event["answer"] == f"{prose(16_000)}…[+4000 chars]"
        assert event["truncated"] == ["answer"]

    def test_counts_an_emoji_as_one_character_and_never_splits_one(self) -> None:
        event = turn({"answer": "\U0001f600" * 16_001})
        assert event["answer"] == "\U0001f600" * 16_000 + "…[+1 chars]"

    def test_leaves_a_string_at_the_limit_alone_and_marks_nothing(self) -> None:
        event = turn({"answer": prose(16_000)})
        assert event["answer"] == prose(16_000)
        assert "truncated" not in event

    def test_cuts_results_at_4000_inputs_at_16000_and_the_intent_at_500(self) -> None:
        event = call(
            {
                "input": {"value": {"q": prose(17_000)}},
                "result": {"value": {"content": [{"type": "text", "text": prose(5_000)}], "structuredContent": {"rows": [prose(4_500)]}}},
                "intent": prose(600),
            }
        )
        assert event["input"] == {"q": f"{prose(16_000)}…[+1000 chars]"}
        assert event["result"] == {
            "content": [{"type": "text", "text": f"{prose(4_000)}…[+1000 chars]"}],
            "structuredContent": {"rows": [f"{prose(4_000)}…[+500 chars]"]},
        }
        assert event["intent"] == f"{prose(500)}…[+100 chars]"
        assert event["truncated"] == ["intent", "input", "result"]

    def test_keeps_the_first_100_entries_of_lists_and_objects_and_stops_nesting_at_10_levels(self) -> None:
        deep: Any = "bottom"
        for _ in range(12):
            deep = {"next": deep}
        objects = {f"k{i}": i for i in range(150)}
        event = call({"input": {"value": {"list": list(range(150)), "object": objects, "deep": deep}}})
        input = event["input"]
        assert len(input["list"]) == 100
        assert len(input["object"]) == 100
        level = input["deep"]
        # input is level 1 and deep level 2, so 8 more levels of `next` fit before the marker.
        for _ in range(8):
            level = level["next"]
        assert level["next"] == {"omitted": "depth"}
        assert event["truncated"] == ["input"]


class TestTheScanBudget:
    def test_ends_a_string_with_cut_when_counting_the_rest_would_pass_it_and_that_uses_the_budget_up(self) -> None:
        state = new_state()
        cut = shape_string(prose(2_000_000), 16_000, state)
        assert isinstance(cut, Cut) and cut.cut == 16_000 and cut.mark == CUT_MARK
        # Reading toward the end until the budget ran out is charged: the next string can't read it again.
        assert state.scan.remaining == 0
        after = shape_string("short", 16_000, state)
        assert isinstance(after, Cut) and after.window == "" and after.mark == CUT_MARK

    def test_still_reads_8000_characters_past_a_cut_where_the_budget_runs_out_for_redaction(self) -> None:
        # Inside the kept part: cut where the budget ran out.
        inside = new_state(budget=10_000)
        assert shape_string(prose(100_000), 16_000, inside) == Cut(prose(18_000), 10_000, CUT_MARK, 16_000)
        assert inside.scan.remaining == 0
        # Inside what's read past a cut at the limit: all 8,000 are read, though the budget paid for 5.
        ahead = new_state(budget=16_005)
        assert shape_string(prose(100_000), 16_000, ahead) == Cut(prose(24_000), 16_000, CUT_MARK, 16_000)
        assert ahead.scan.remaining == 0
        # A rest that fits is still counted exactly.
        fits = new_state(budget=16_005)
        assert shape_string(prose(16_004), 16_000, fits) == Cut(prose(16_004), 16_000, "…[+4 chars]", 16_000)
        assert fits.scan.remaining == 1

    def test_keeps_one_event_with_100_large_strings_within_it(self) -> None:
        strings = [prose(200_000) for _ in range(100)]
        state = new_state()
        started = time.perf_counter()
        shaped = shape_json(strings, 16_000, state)
        assert time.perf_counter() - started < 1.0
        assert state.scan.remaining == 0
        assert isinstance(shaped, list)
        # Each of the first four costs its slot and all its 200,000 characters, the rest being counted.
        assert [cut.mark for cut in shaped[:4] if isinstance(cut, Cut)] == ["…[+184000 chars]"] * 4
        # The fifth's rest no longer fits: counting it uses the budget up, and no entry is visited after.
        fifth = shaped[4]
        assert isinstance(fifth, Cut) and fifth.cut == 16_000 and fifth.mark == CUT_MARK
        assert len(shaped) == 5
        assert len(call({"input": {"value": {"strings": strings}}})["input"]["strings"]) == 5

    def test_bounds_the_work_100_strings_of_2_million_characters_read_about_a_million_in_all(self) -> None:
        big = prose(2_000_000)
        started = time.perf_counter()
        shaped = shape_json([big] * 100, 16_000, new_state())
        assert time.perf_counter() - started < 0.5
        assert isinstance(shaped, list) and len(shaped) == 1

    def test_shortens_a_100_mb_text_result_quickly(self) -> None:
        big = prose(100 * 1024 * 1024)
        started = time.perf_counter()
        event = call({"result": {"value": {"content": [{"type": "text", "text": big}]}}})
        assert time.perf_counter() - started < 1.0
        assert event["result"]["content"][0]["text"] == f"{prose(4_000)}{CUT_MARK}"

    def test_shortens_a_100_mb_non_ascii_text_result_quickly_too(self) -> None:
        # Python only: a string is joined only as far as it is read.
        big = "é" * (100 * 1024 * 1024)
        started = time.perf_counter()
        event = call({"result": {"value": {"content": [{"type": "text", "text": big}]}}})
        assert time.perf_counter() - started < 1.0
        assert event["result"]["content"][0]["text"] == "é" * 4_000 + CUT_MARK


BASE64 = "QUJD" * 500


class TestFilesSentAsText:
    def test_keeps_base64_strings_and_data_uris_as_their_size_and_leaves_prose_and_code_alone(self) -> None:
        assert binary_size(BASE64) == 1_500
        assert binary_size(f"{BASE64}==") == 1_500
        assert binary_size("data:image/png;base64,QUJD") == 3
        assert binary_size("data:text/plain,hello") is None
        assert binary_size("QUJD" * 200) is None
        assert binary_size(prose(5_000)) is None
        assert binary_size(f"function f() {{ return {'a' * 5_000} }}") is None
        # Only the ends are read: base64-looking at both, with prose in the middle, is a file too.
        assert binary_size(f"{'A' * 4_096}{prose(10_000)}{'A' * 1_024}") == (15_120 * 3) // 4

    def test_counts_lengths_in_utf16_units_as_typescript_does(self) -> None:
        # Python only: the decoded size is JavaScript's, whose length counts an emoji twice.
        # Computed with TypeScript's binarySize.
        assert binary_size("A" * 4_096 + "\U0001f600" * 10 + "A" * 1_024) == 3855
        assert binary_size("data:\U0001f600;base64,QUJD") == 3
        assert binary_size("data:x;base64,\U0001f600QUJD") == 4

    def test_becomes_omitted_bytes_in_json_and_binary_n_bytes_in_a_field_that_must_be_a_string(self) -> None:
        event = call(
            {
                "input": {"value": {"file": BASE64}},
                "result": {
                    "value": {
                        "content": [
                            {"type": "text", "text": BASE64},
                            {"type": "image", "data": BASE64, "mimeType": "image/png"},
                            {"type": "resource", "resource": {"uri": "file:///a", "blob": BASE64}},
                        ]
                    }
                },
            }
        )
        assert event["input"] == {"file": {"omitted": "binary", "bytes": 1_500}}
        assert event["result"] == {
            "content": [
                {"type": "text", "text": "[binary, 1500 bytes]"},
                {"type": "image", "mimeType": "image/png", "bytes": 1_500},
                {"type": "resource", "uri": "file:///a", "bytes": 1_500},
            ]
        }
        assert turn({"answer": BASE64})["answer"] == "[binary, 1500 bytes]"

    def test_keeps_resource_links_and_embedded_text_never_unknown_block_contents(self) -> None:
        event = call(
            {
                "result": {
                    "value": {
                        "content": [
                            {"type": "resource_link", "uri": "file:///orders.csv", "name": "orders.csv", "mimeType": "text/csv", "extra": "x"},
                            {"type": "resource", "resource": {"uri": "file:///a.txt", "mimeType": "text/plain", "text": "hello"}},
                            {"type": "widget", "payload": "anything"},
                        ],
                        "isError": False,
                        "_meta": {"trace": "x"},
                    }
                }
            }
        )
        assert event["result"] == {
            "content": [
                {"type": "resource_link", "uri": "file:///orders.csv", "name": "orders.csv", "mimeType": "text/csv"},
                {"type": "resource", "uri": "file:///a.txt", "mimeType": "text/plain", "text": "hello"},
                {"type": "widget"},
            ],
            "isError": False,
        }


def pad(n: int) -> str:
    return prose(n - 1) + " "


class TestRedactionAroundACut:
    @pytest.mark.parametrize(
        ("name", "secret"),
        [
            ("a PEM key", f"-----BEGIN PRIVATE KEY-----\n{'A' * 20_000}"),
            ("a JWT", f"eyJhbGciOiJIUzI1NiJ9.{'a' * 30_000}.sig"),
            ("an sk- key", f"sk-{'a' * 30_000}"),
            ("a Bearer token", f"Bearer {'a1' * 15_000}"),
        ],
    )
    def test_still_masks_a_secret_cut_at_the_limit(self, name: str, secret: str) -> None:
        event = turn({"answer": pad(15_900) + secret})
        assert re.search(r" \[SECRET_1\]…\[(\+\d+ chars|cut)\]\Z", event["answer"]), name
        assert event["redactions"] == {"secret": 1}

    # The cut falls `kept` characters into the token, where the budget runs out inside the string
    # (10,000) or inside the 8,000 read past a cut at the limit (16,005).
    @pytest.mark.parametrize(
        ("name", "token", "kept", "masked", "pattern"),
        [
            ("a card number", "4111 1111 1111 1111", 14, "[CARD_1]", "card"),
            ("an email", "jane.doe@example.com", 14, "[EMAIL_1]", "email"),
            ("a URL's credentials", "https://alice:supersecretpassword@db.example.com/x", 20, "https://[SECRET_1]", "secret"),
            ("an AWS secret key", "aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", 41, "[SECRET_1]", "secret"),
            ("a key prefix after a hyphen", "id-sk-abcdefghijklmnopqrstuvwxyz0123", 20, "id-[SECRET_1]", "secret"),
            ("an IPv6 address", "fe80:0:0:0:202:b3ff:fe1e:8329", 20, "[IP_1]", "ip"),
        ],
    )
    def test_masks_a_token_running_into_a_cut_where_the_scan_budget_runs_out(self, name: str, token: str, kept: int, masked: str, pattern: str) -> None:
        for budget, at in ((10_000, 10_000), (16_005, 16_000)):
            state = new_state(budget=budget)
            redactor = Redactor(ALL)
            shaped = shape_string(f"{pad(at - kept)}{token} {prose(20_000)}", 16_000, state)
            assert redactor.finalize(shaped, TEXT_FIELD) == f"{pad(at - kept)}{masked}{CUT_MARK}", name
            assert redactor.counts == {pattern: 1}, name

    def test_masks_a_secret_that_runs_past_the_cut_whole_matched_with_what_follows_it(self) -> None:
        key = f"sk-{'k' * 40}"
        event = turn({"answer": f"{pad(15_980)}{key} after"})
        assert event["answer"] == f"{pad(15_980)}[SECRET_1]…[+{len(key) + 6 - 20} chars]"

    def test_neither_replaces_nor_counts_what_comes_entirely_after_the_cut(self) -> None:
        event = turn({"answer": f"{prose(16_000)} jane@acme.io and sk-{'k' * 40}"})
        assert event["answer"] == f"{prose(16_000)}…[+{1 + 12 + 5 + 43} chars]"
        assert "redactions" not in event

    def test_numbers_a_value_the_same_in_a_cut_string_and_elsewhere(self) -> None:
        event = turn({"question": "is jane@acme.io there?", "answer": f"{pad(15_990)}jane@acme.io {prose(1_000)}"})
        assert event["question"] == "is [EMAIL_1] there?"
        assert event["answer"].startswith(f"{pad(15_990)}[EMAIL_1]")

    def test_applies_secret_key_names_before_any_cut(self) -> None:
        event = call({"input": {"value": {"password": prose(20_000)}}})
        assert event["input"] == {"password": "[SECRET_1]"}

    def test_measures_the_cut_in_utf16_units_for_the_patterns(self) -> None:
        # Python only: emoji before an email that straddles the cut, which TypeScript indexes in units.
        event = turn({"answer": "\U0001f600" * 15_990 + " jane@acme.io and more"})
        # Computed with TypeScript.
        assert event["answer"] == "\U0001f600" * 15_990 + " [EMAIL_1]…[+12 chars]"


class TestRedactKeys:
    def test_replaces_values_under_matching_keys_whole_matched_by_their_end_like_the_secret_key_names(self) -> None:
        redact_keys = resolve_keys(["name", "Date-Of-Birth", "", 7, "name"])
        assert redact_keys == ["name", "dateofbirth"]
        event = call(
            {
                "input": {"value": {"firstName": "Jane", "customer_name": {"first": "J"}, "fileName": "a.csv", "dateOfBirth": "1990-01-01", "city": "Rome"}},
                "result": {"value": {"structuredContent": {"rows": [{"name": "Bob"}]}}},
            },
            redact_keys=redact_keys,
        )
        assert event["input"] == {"firstName": "[SECRET_1]", "customer_name": "[SECRET_2]", "fileName": "[SECRET_3]", "dateOfBirth": "[SECRET_4]", "city": "Rome"}
        assert event["result"] == {"structuredContent": {"rows": [{"name": "[SECRET_5]"}]}}

    def test_reads_a_set_or_any_other_iterable_of_names_and_one_string_as_one_name(self) -> None:
        def names() -> Iterator[str]:
            yield "name"
            yield "Date-Of-Birth"

        for option in ({"name", "Date-Of-Birth"}, frozenset({"name", "Date-Of-Birth"}), {"name": 1, "Date-Of-Birth": 2}.keys(), names(), ("name", "Date-Of-Birth")):
            read = read_keys(option)
            assert read is not None and sorted(read[0]) == ["dateofbirth", "name"] and read[1] is False, option
        assert read_keys("Name") == (["name"], False)
        assert read_keys(None) == ([], False)
        assert read_keys([]) == ([], False)
        endless = read_keys(f"k{n}" for n in itertools.count())
        assert endless is not None and len(endless[0]) == 1000

    def test_flags_entries_that_arent_names_and_refuses_what_isnt_a_list_of_names(self) -> None:
        assert read_keys(["name", 7, "", "--"]) == (["name"], True)
        # A mapping isn't read, as TypeScript doesn't read an object.
        for option in ({"name": True}, 42, True, b"name", lambda: ["name"]):
            assert read_keys(option) is None, option


class TestPatterns:
    def test_match_with_javascripts_whitespace_written_out(self) -> None:
        # TypeScript compares its patterns with 0.1's; redact_test compares Python's with TypeScript's.
        assert Redactor(ALL).text("Authorization: Bearer abcdefghijklmnop1234") == "Authorization: [SECRET_1]"
