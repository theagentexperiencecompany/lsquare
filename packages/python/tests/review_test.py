"""Port of the event parts of packages/lsquare/test/review.test.ts: Codex's review of the 0.2 core,
numbered as in the review. The client's parts (the queue's memory limit) are the client's tests."""

from __future__ import annotations

import asyncio
import inspect
import math
import re
import time
from collections.abc import Iterator, Mapping
from typing import Any

import pytest

from lsquare._event import BuiltOk, Context, Draft, after_hook, build_call, build_turn
from lsquare._jsjson import dumps
from lsquare._redact import resolve_patterns
from lsquare._shape import binary_size, decoded_size, new_state, shape_json

from cases_test import ingestion, require_node

CONTEXT = Context(
    sdk={"name": "lsquare", "version": "0.2.0", "language": "python"},
    patterns=resolve_patterns(None),
    redact_keys=[],
    max_event_bytes=256 * 1024,
)
RANDOM_ID = re.compile(r"[0-9a-f-]{36}")


def prose(chars: int) -> str:
    return ("lorem ipsum " * math.ceil(chars / 12))[:chars]


def record(**over: Any) -> dict[str, Any]:
    return {"callId": "call-1", "capturedAt": "2026-10-28T00:00:00.000Z", "toolName": "search_orders", "status": "completed", "durationMs": 1, "ids": {}, "diagnostics": False, **over}


def draft(built: Any) -> Draft:
    assert isinstance(built, BuiltOk), built
    return built.draft


def call(hook: bool = False, context: Context = CONTEXT, **over: Any) -> Draft:
    return draft(build_call(record(**over), context, hook))


def turn(input: dict[str, Any], hook: bool = False) -> Draft:
    return draft(build_turn(input, "2026-10-28T00:00:00.000Z", CONTEXT, hook))


def hooked(pending: Draft, out: dict[str, Any]) -> Draft:
    return draft(after_hook(out, pending, CONTEXT))


class Test1ASecretCutAtTheLimit:
    @pytest.mark.parametrize(
        ("before", "secret"),
        [
            ("token=", f"eyJhbGciOiJIUzI1NiJ9.{'a' * 30_000}.sig"),
            ("https://hooks.example/", f"sk-{'k' * 30_000}"),
            ("value.", f"ghp_{'g' * 30_000}"),
        ],
        ids=["after token=", "after a slash", "after a dot"],
    )
    def test_is_masked(self, before: str, secret: str) -> None:
        answer = turn({"answer": f"{prose(15_899)} {before}{secret}"}).event["answer"]
        assert f"{before}[SECRET_1]…" in answer
        assert secret[:12] not in answer

    def test_but_not_after_a_letter_or_digit_where_no_pattern_lets_one_start(self) -> None:
        answer = turn({"answer": f"{prose(15_899)} risk-{'a' * 30_000}"}).event["answer"]
        assert "risk-aaa" in answer


class Test2FieldsThatAreContentNotIdentifiers:
    def test_redacts_a_tasks_id_and_status_and_a_blocks_type_and_mime_type(self) -> None:
        event = call(
            task={"id": "task for jane@acme.io", "status": "waiting on sk-abcdefghijklmnopqrstuvwxyz"},
            result={"value": {"content": [{"type": "image", "mimeType": "image/png; jane@acme.io", "data": "QUJD"}, {"type": "x jane@acme.io"}]}},
        ).event
        assert event["task"] == {"id": "task for [EMAIL_1]", "status": "waiting on [SECRET_1]"}
        assert event["result"] == {"content": [{"type": "image", "mimeType": "image/png; [EMAIL_1]", "bytes": 3}, {"type": "x [EMAIL_1]"}]}

    def test_lets_before_send_change_or_remove_a_task(self) -> None:
        pending = call(True, task={"id": "task_1", "status": "working"})
        rest = {key: value for key, value in pending.event.items() if key != "task"}
        assert "task" not in hooked(pending, rest).event


class Test3CoroutinesInACallsValues:
    """TypeScript silences promises; Python leaves coroutines untouched, never closed or awaited."""

    def test_are_never_awaited_or_closed(self) -> None:
        async def value() -> str:
            return "late"

        values = [value() for _ in range(12)]
        try:
            built = build_call(record(ids={"orgId": values[0]}), CONTEXT, False)
            assert not built.ok and built.code == "invalid_id"
            call(
                server=values[1],
                client=values[2],
                intent=values[3],
                error={"message": values[4], "type": values[5], "category": "thrown"},
                agent={"question": values[6], "error": values[7], "toolCalls": values[8], "latency": values[9]},
                metadata=values[10],
                task=values[11],
            )
            assert all(inspect.getcoroutinestate(coroutine) == inspect.CORO_CREATED for coroutine in values)
        finally:

            async def finish() -> list[str]:
                return [await coroutine for coroutine in values]

            assert asyncio.run(finish()) == ["late"] * 12


class Test4WorkOutsideTheBudget:
    def test_is_bounded_base64_padding_and_a_data_uris_header(self) -> None:
        equals = "=" * 2_000_000
        started = time.perf_counter()
        assert decoded_size(len(equals), equals) == ((2_000_000 - 2) * 3) // 4
        assert binary_size(f"data:{'x' * 2_000_000},AAAA") is None
        assert time.perf_counter() - started < 0.05

    def test_a_turn_id_too_long_to_fit_in_an_event_is_dropped_before_it_is_hashed(self) -> None:
        built = turn({"conversationId": "c", "turnId": "x" * 300_000})
        assert built.notes.dropped == {"turnId": "too_large"}
        assert RANDOM_ID.fullmatch(built.event["eventId"])


class Test5AnEventWaitingForBeforeSend:
    @staticmethod
    def emails(i: int) -> str:
        return " ".join(f"u{i}_{j}@acme.io" for j in range(1_400))

    def test_keeps_nothing_of_what_redaction_matched_in_content_that_fitting_left_out(self) -> None:
        pending = call(True, input={"value": {"texts": [self.emails(i) for i in range(40)]}})
        assert "input" not in pending.event
        assert pending.redactor.retained_bytes() < 1_000

    def test_counts_what_it_keeps_for_what_it_still_holds(self) -> None:
        # Fifteen cut strings of distinct emails fit in the event: their numbering is kept, and counted.
        pending = call(True, input={"value": {"texts": [self.emails(i) for i in range(15)]}})
        assert "input" in pending.event
        assert pending.redactor.retained_bytes() > len(dumps(pending.event))


class Test7BeforeSendOutputForACall:
    def test_cant_add_a_files_data_back_and_keeps_the_sizes_this_sdk_worked_out(self) -> None:
        pending = call(True, result={"value": {"content": [{"type": "image", "data": "QUJD", "mimeType": "image/png"}]}})
        event = pending.event
        content = [*event["result"]["content"], {"type": "audio", "data": "QUJDREVG", "mimeType": "audio/wav"}]
        out = hooked(pending, {**event, "result": {**event["result"], "content": content}}).event
        assert out["result"] == {"content": [{"type": "image", "mimeType": "image/png", "bytes": 3}, {"type": "audio", "mimeType": "audio/wav", "bytes": 6}]}

    def test_keeps_the_error_valid_its_category_stays_when_the_hook_empties_or_removes_it(self) -> None:
        for event in self.hooked_errors():
            assert event["error"]["category"] == "thrown"
        pending = call(True, status="error", error={"message": "boom", "category": "thrown"})
        assert pending.event["error"] == {"message": "boom", "category": "thrown"}

    @staticmethod
    def hooked_errors() -> list[dict[str, Any]]:
        pending = call(True, status="error", error={"message": "boom", "category": "thrown"})
        rest = {key: value for key, value in pending.event.items() if key != "error"}
        outs = [{**rest, "error": {}}, rest, {**rest, "error": {"message": "m", "category": "protocol"}}]
        return [hooked(call(True, status="error", error={"message": "boom", "category": "thrown"}), out).event for out in outs]


class Test10AStringThisSdkCutAfterBeforeSend:
    def test_keeps_its_exact_count_when_the_hook_left_it_alone_though_redaction_made_it_longer(self) -> None:
        pending = turn({"answer": "a@b.co " * 2_400}, True)
        before = pending.event["answer"]
        assert before.endswith("…[+800 chars]")
        assert hooked(pending, {**pending.event}).event["answer"] == before

    def test_is_cut_again_when_the_hook_moves_it_to_a_field_with_a_tighter_limit(self) -> None:
        pending = call(True, input={"value": {"q": prose(17_000)}})
        cut = pending.event["input"]["q"]
        out = hooked(pending, {**pending.event, "intent": cut, "result": {"content": [{"type": "text", "text": cut}]}}).event
        assert out["intent"] == f"{prose(500)}…[+16500 chars]"
        assert out["result"]["content"][0]["text"] == f"{prose(4_000)}…[+13000 chars]"
        assert out["input"]["q"] == cut

    def test_is_recognised_only_while_it_is_in_the_event_the_hook_got(self) -> None:
        rows = [f"{i} {prose(16_500)}" for i in range(30)]
        pending = call(True, input={"value": {"rows": rows}})
        assert "input" not in pending.event
        cut = f"{rows[0][:16_000]}…[+502 chars]"
        out = hooked(pending, {**pending.event, "intent": cut}).event
        assert out["intent"] == f"{cut[:500]}…[+16002 chars]"

    def test_adds_what_was_cut_before_to_the_count_when_the_hook_made_it_longer(self) -> None:
        pending = turn({"answer": prose(17_000)}, True)
        before = pending.event["answer"]
        longer = f"{before[: -len('…[+1000 chars]')]}{prose(500)}…[+1000 chars]"
        assert hooked(pending, {**pending.event, "answer": longer}).event["answer"] == f"{prose(16_000)}…[+1500 chars]"


class Test11ValuesUnderSecretKeyNames:
    def test_are_replaced_before_before_send_sees_them_and_a_short_one_is_numbered_like_the_same_secret_elsewhere(self) -> None:
        key = f"sk-{'k' * 40}"
        pending = call(True, input={"value": {"password": prose(20_000), "apiKey": key, "note": f"uses {key}"}})
        assert pending.event["input"] == {"password": "[SECRET_1]", "apiKey": "[SECRET_2]", "note": f"uses {key}"}
        assert hooked(pending, {**pending.event}).event["input"] == {"password": "[SECRET_1]", "apiKey": "[SECRET_2]", "note": "uses [SECRET_2]"}

    def test_a_long_one_which_cant_be_read_whole_gets_a_number_of_its_own_never_shared(self) -> None:
        long = prose(20_000)
        event = call(input={"value": {"password": long, "token": long, "note": long}}).event
        assert event["input"]["password"] == "[SECRET_1]"
        assert event["input"]["token"] == "[SECRET_2]"

    def test_in_metadata_too(self) -> None:
        pending = turn({"metadata": {"sessionToken": "abc", "plan": "pro"}}, True)
        assert pending.event["metadata"] == {"sessionToken": "[SECRET_1]", "plan": "pro"}


class Test12ACallsIds:
    @pytest.mark.parametrize(
        ("over", "path"),
        [
            ({"ids": {"userId": "a\x00b"}}, "ids.userId"),
            ({"ids": {"orgId": 7}}, "ids.orgId"),
            ({"sessionId": "x" * 513}, "sessionId"),
            ({"turnRef": "k/short"}, "turnRef"),
            # Python only: a null where TypeScript reads it as present.
            ({"sessionId": None}, "sessionId"),
        ],
    )
    def test_are_checked_as_the_ingestion_checks_them(self, over: dict[str, Any], path: str) -> None:
        built = build_call(record(**over), CONTEXT, False)
        assert not built.ok and built.code == "invalid_id" and built.paths == [path]

    def test_never_let_ids_replace_the_call_id(self) -> None:
        event = call(ids={"callId": "other", "orgId": "org_1"}).event
        assert (event["eventId"], event["callId"], event["orgId"]) == ("call-1", "call-1", "org_1")

    def test_keep_the_agent_parts_turn_id_which_the_turn_ref_was_worked_out_from_across_before_send(self) -> None:
        pending = call(True, turnRef=f"k/{'a' * 64}", agent={"question": "q", "turnId": 3})
        event = pending.event
        out = hooked(pending, {**event, "agent": {**event["agent"], "turnId": "99"}}).event
        assert out["agent"]["turnId"] == "3"


class Test13LimitedDiagnostics:
    @staticmethod
    def events() -> list[dict[str, Any]]:
        big = {"tenant": "x" * 262_000}
        direct = call(diagnostics=True, metadata=big).event
        pending = call(True, diagnostics=True, input={"value": {"rows": [prose(15_000) for _ in range(30)]}})
        out = hooked(pending, {**pending.event, "metadata": big}).event
        return [direct, out]

    def test_are_reduced_before_any_size_check_so_oversized_content_never_drops_them(self) -> None:
        for event in self.events():
            assert event["identity"] == "failed"


class Test14OversizedMetadataOnACall:
    def test_drops_the_event_as_on_a_turn_before_and_after_before_send(self) -> None:
        big = {"tenant": "x" * 300_000}
        built = build_call(record(metadata=big), CONTEXT, False)
        assert not built.ok and built.code == "too_large"
        pending = call(True)
        after = after_hook({**pending.event, "metadata": big}, pending, CONTEXT)
        assert not after.ok and after.code == "too_large"


class Test15LengthsInCodePoints:
    def test_for_short_fields_and_for_the_length_of_a_result_that_is_not_a_string(self) -> None:
        # JavaScript's length of 8,001 emoji is 16,002: they're kept, being 8,001 characters.
        assert turn({"client": "\U0001f600" * 8_001}).event["client"] == "\U0001f600" * 8_001
        assert turn({"client": "\U0001f600" * 16_001}).notes.dropped == {"client": "too_large"}
        assert turn({"toolCalls": [{"name": "x", "result": {"x": "\U0001f600"}}]}).event["toolCalls"] == [{"name": "x", "resultChars": 9}]


class Test16Coverage:
    @staticmethod
    def call_without_agent() -> dict[str, Any]:
        agent = {"question": prose(15_000), "answer": prose(15_000), "verification": {"rows": [prose(15_000) for _ in range(16)]}}
        return call(input={"value": {"q": "x"}}, agent=agent, intent="kept").event

    @staticmethod
    def turn_without_answer() -> dict[str, Any]:
        tool_calls = [{"name": "t", "input": {"rows": [prose(15_000) for _ in range(16)]}}]
        return turn(
            {"question": prose(15_000), "answer": prose(15_000), "verification": {"rows": [prose(15_000) for _ in range(18)]}, "toolCalls": tool_calls}
        ).event

    def test_a_call_loses_its_whole_agent_last_after_its_tool_calls(self) -> None:
        event = self.call_without_agent()
        # The input goes before the agent part, however small.
        assert event["truncated"] == ["input", "agent"]
        assert "agent" not in event and "input" not in event
        assert event["intent"] == "kept"

    def test_a_turn_loses_its_answer_then_its_question_once_its_tool_calls_are_gone(self) -> None:
        assert self.turn_without_answer()["truncated"] == ["toolCalls", "answer", "question", "verification"]

    def test_every_array_slot_costs_the_budget(self) -> None:
        state = new_state(budget=20)
        started = time.perf_counter()
        shaped = shape_json([None] * 1_000_000, 16_000, state)
        assert isinstance(shaped, list) and len(shaped) == 20
        assert time.perf_counter() - started < 0.02

    def test_objects_are_read_in_javascripts_order_integer_like_keys_first_which_numbering_follows(self) -> None:
        event = call(input={"value": {"b": "b@acme.io", "10": "ten@acme.io", "2": "two@acme.io"}}).event
        assert event["input"] == {"2": "[EMAIL_1]", "10": "[EMAIL_2]", "b": "[EMAIL_3]"}


# Codex's check of the fixes, numbered as in that review.


class TestCheck1TwoDifferentSecrets:
    def test_never_share_a_number_because_shortening_made_them_look_alike(self) -> None:
        event = call(input={"value": {"password": "a" * 2_000, "token": "b" * 2_000, "secret": "a" * 2_000}}).event
        assert event["input"] == {"password": "[SECRET_1]", "token": "[SECRET_2]", "secret": "[SECRET_1]"}

    def test_in_objects_count_only_when_nothing_in_them_was_cut_or_turned_into_a_size(self) -> None:
        event = call(input={"value": {"auth": {"file": "QUJD" * 500}, "credentials": {"file": "QUJE" * 500}}}).event
        assert event["input"] == {"auth": "[SECRET_1]", "credentials": "[SECRET_2]"}


class TestCheck2SecretsThatDifferOnlyInALoneSurrogate:
    def test_get_different_numbers(self) -> None:
        z = "z" * 65
        event = call(input={"value": {"password": f"{z}\ud800", "token": f"{z}\ud801", "secret": f"{z}\ud800"}}).event
        assert event["input"] == {"password": "[SECRET_1]", "token": "[SECRET_2]", "secret": "[SECRET_1]"}


class TestCheck4ErrorCategory:
    def test_is_read_once(self) -> None:
        reads = 0

        class Error(Mapping[str, Any]):
            def __getitem__(self, key: str) -> Any:
                nonlocal reads
                if key == "category":
                    reads += 1
                    return object()
                if key == "message":
                    return "m"
                raise KeyError(key)

            def __iter__(self) -> Iterator[str]:
                return iter(["message", "category"])

            def __len__(self) -> int:
                return 2

        event = call(status="error", error=Error()).event
        assert event["error"] == {"message": "m"}
        assert reads == 1


class TestCheck5RedactKeysInResultBlocks:
    def test_replace_a_blocks_name_uri_mime_type_or_text_before_the_hook_too(self) -> None:
        context = Context(sdk=CONTEXT.sdk, patterns=CONTEXT.patterns, redact_keys=["name"], max_event_bytes=CONTEXT.max_event_bytes)
        masked = call(True, context, result={"value": {"content": [{"type": "resource_link", "uri": "file:///a", "name": "Jane Doe"}]}})
        assert masked.event["result"] == {"content": [{"type": "resource_link", "uri": "file:///a", "name": "[SECRET_1]"}]}


class TestCheck6AStringMarkedAsCut:
    def test_is_only_recognised_if_the_sdk_cut_it_not_from_a_field_it_never_cuts(self) -> None:
        forged = f"{prose(15_000)}…[cut]"
        pending = call(True, status="error", error={"message": "m", "type": forged, "category": "thrown"})
        out = hooked(pending, {**pending.event, "result": {"content": [{"type": "text", "text": forged}]}}).event
        assert out["result"]["content"][0]["text"] == f"{prose(4_000)}…[cut]"


class TestCheck7AMarkedStringAfterTheHook:
    def test_is_read_once_and_the_budget_charged_for_it(self) -> None:
        pending = turn({"question": "q"}, True)
        answer = f"{'x ' * 2_000_000}…[cut]"
        # TypeScript counts the characters read; here, the time it takes and where the budget ran out.
        started = time.perf_counter()
        out = hooked(pending, {**pending.event, "answer": answer}).event
        assert time.perf_counter() - started < 0.5
        assert out["answer"] == f"{'x ' * 8_000}…[cut]"


class TestSecondCheck2RedactKeysInResultBlocks:
    def test_number_each_value_by_its_raw_value_before_it_could_look_like_a_file_or_be_cut(self) -> None:
        blocks = [{"type": "text", "text": "a" * 2_000}, {"type": "text", "text": "b" * 2_000}, {"type": "text", "text": "a" * 2_000}, {"type": "text", "text": prose(5_000)}, {"type": "text", "text": prose(5_000)}]
        context = Context(sdk=CONTEXT.sdk, patterns=CONTEXT.patterns, redact_keys=["text"], max_event_bytes=CONTEXT.max_event_bytes)
        pending = call(True, context, result={"value": {"content": blocks}})

        def texts(event: dict[str, Any]) -> list[str]:
            return [block["text"] for block in event["result"]["content"]]

        # Too long to read whole: a number each, never shared.
        assert texts(pending.event) == ["[SECRET_1]", "[SECRET_2]", "[SECRET_1]", "[SECRET_3]", "[SECRET_4]"]
        assert texts(draft(after_hook({**pending.event}, pending, context)).event) == ["[SECRET_1]", "[SECRET_2]", "[SECRET_1]", "[SECRET_3]", "[SECRET_4]"]


def test_the_ingestion_accepts_every_event() -> None:
    require_node()
    events = [
        *Test7BeforeSendOutputForACall.hooked_errors(),
        *Test13LimitedDiagnostics.events(),
        Test16Coverage.call_without_agent(),
        Test16Coverage.turn_without_answer(),
    ]
    assert ingestion(events) == ["valid"] * len(events)
