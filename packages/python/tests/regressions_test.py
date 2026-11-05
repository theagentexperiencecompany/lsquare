"""Port of the event parts of packages/lsquare/test/regressions.test.ts: sentinels, work budgets and
result lengths. The client's parts (re-entrant callbacks, responses, rate limits) are the client's
tests; json.ts's `toJson` isn't used by the event pipeline, so it has no Python counterpart."""

from __future__ import annotations

import math
import time
from collections.abc import Iterator
from typing import Any

import pytest

from lsquare._event import BuiltOk, Context, build_call, build_turn
from lsquare._jsjson import Budget, dumps, json_length
from lsquare._normalize import RESULT_MEASURE_LIMIT

BASE = {"conversationId": "conv1", "orgId": "org_1", "toolName": "ask_docs", "question": "q"}
CONTEXT = Context(sdk={"name": "lsquare", "version": "0.2.0", "language": "python"}, patterns=[], redact_keys=[], max_event_bytes=256 * 1024)


def turn(input: dict[str, Any]) -> Any:
    return build_turn(input, "2026-10-28T00:00:00.000Z", CONTEXT, False)


class TestSentinels:
    def test_keeps_the_string_too_large_as_data(self) -> None:
        result = turn({**BASE, "verification": "too_large", "latency": "too_large", "toolCalls": [{"name": "x", "input": "too_large"}]})
        assert isinstance(result, BuiltOk)
        event = result.draft.event
        assert event["verification"] == "too_large"
        assert event["toolCalls"] == [{"name": "x", "input": "too_large"}]


class TestWorkBudgets:
    def test_reads_only_the_first_100_tool_calls_valid_or_not(self) -> None:
        calls = [{"tool": "x"} for _ in range(100_000)]
        started = time.perf_counter()
        result = turn({**BASE, "toolCalls": calls})
        assert time.perf_counter() - started < 0.2
        assert isinstance(result, BuiltOk)
        assert len(result.draft.notes.dropped) == 100

    def test_shares_one_measuring_allowance_across_all_tool_results(self) -> None:
        # Counted, not timed: measuring one element takes a few microseconds in Python, so the time
        # says more about the machine than about the allowance. Each result alone would fit in it.
        big = Counted([0] * 900_000)
        result = turn({**BASE, "toolCalls": [{"name": "x", "result": big} for _ in range(20)]})
        # Every element read is charged one, and a result stops at the first charge that can't be
        # paid: one allowance for the whole event, not one per result (which would read 18 million).
        assert big.reads <= RESULT_MEASURE_LIMIT
        assert isinstance(result, BuiltOk)
        assert result.draft.event["toolCalls"][0]["resultChars"] == len(dumps([0] * 900_000))
        assert result.draft.notes.dropped == {f"toolCalls[{index}].resultChars": "too_large" for index in range(1, 20)}

    def test_enumerates_a_result_the_tool_calls_share_once_which_is_only_measured(self) -> None:
        # Its first value isn't JSON: each measure stops there, with nearly all its allowance left,
        # so without the per-pass list its keys would be enumerated again at every tool call.
        result = Listed({"0": object(), **{f"k{i}": i for i in range(200)}})
        built = turn({**BASE, "toolCalls": [{"name": "lookup", "result": result} for _ in range(100)]})
        assert isinstance(built, BuiltOk)
        assert built.draft.notes.dropped["toolCalls[99].resultChars"] == "not_json"
        assert result.lists == 1
        # The next event enumerates it again.
        turn({**BASE, "toolCalls": [{"name": "lookup", "result": result}]})
        assert result.lists == 2


class TestListsPastTheScanBudget:
    """As TypeScript's: a result's content blocks and the tool calls in toolCalls are list items,
    charged one each, so past the budget they take no more entries."""

    @staticmethod
    def call(**over: Any) -> dict[str, Any]:
        record: dict[str, Any] = {
            "callId": "call-1",
            "capturedAt": "2026-10-28T00:00:00.000Z",
            "toolName": "search_orders",
            "status": "completed",
            "durationMs": 1,
            "ids": {},
            "diagnostics": False,
            **over,
        }
        built = build_call(record, CONTEXT, False)
        assert isinstance(built, BuiltOk), built
        return built.draft.event

    def test_a_calls_result_content_and_agent_tool_calls(self) -> None:
        tool_calls = [{"name": f"tool_{i}", "input": {"a": 1}} for i in range(100)]
        content = [{"type": "text", "text": f"block {i}"} for i in range(100)]
        sent = self.call(input={"value": {"big": prose(2_400_000)}}, agent={"toolCalls": tool_calls}, result={"value": {"content": content}})
        assert sent["agent"] == {"status": "completed"}
        assert sent["result"] == {"content": []}
        assert sent["truncated"] == ["input", "agent", "result"]

    def test_keeps_the_blocks_and_tool_calls_read_before_it_ran_out(self) -> None:
        content = [{"type": "text", "text": prose(1_200_000)}, {"type": "text", "text": "after"}]
        assert len(self.call(result={"value": {"content": content}})["result"]["content"]) == 1
        built = turn({**BASE, "toolCalls": [{"name": "a", "input": {"s": prose(2_000_000)}}, {"name": "b"}]})
        assert isinstance(built, BuiltOk)
        assert [call["name"] for call in built.draft.event["toolCalls"]] == ["a"]

    def test_a_turn_whose_tool_calls_took_none_leaves_them_out_sent_empty_they_would_say_none_was_called(self) -> None:
        built = turn({**BASE, "question": prose(2_000_000), "toolCalls": [{"name": "lookup"}]})
        assert isinstance(built, BuiltOk)
        assert "toolCalls" not in built.draft.event
        assert built.draft.event["truncated"] == ["question", "toolCalls"]


def prose(chars: int) -> str:
    """Prose: spaces keep it from looking like base64."""
    return ("lorem ipsum " * math.ceil(chars / 12))[:chars]


class Listed(dict[str, Any]):
    """A mapping that counts how often its keys are enumerated."""

    lists = 0

    def __iter__(self) -> Iterator[str]:
        self.lists += 1
        return super().__iter__()


class Counted(list[int]):
    """A list that counts the elements read from it, as measuring reads an array."""

    reads = 0

    def __iter__(self) -> Iterator[int]:
        for element in super().__iter__():
            self.reads += 1
            yield element


class TestJsonLength:
    @pytest.mark.parametrize("value", [{"a": "\n"}, ["\\", "\x00"], {'k"ey': "é\t"}, [1, None, True, -0.0, 1e21], "plain"])
    def test_matches_json_stringify_escapes_included(self, value: Any) -> None:
        # JSON.stringify's text is what the writer writes (jsjson_test checks it against node).
        assert json_length(value, Budget(1_000_000)) == len(dumps(value))

    def test_measures_numbers_as_javascript_writes_them(self) -> None:
        assert json_length([1e-7, 1e21, 0.1 + 0.2, 2**53 + 1, math.inf], Budget(100)) == len("[1e-7,1e+21,0.30000000000000004,9007199254740992,null]")
