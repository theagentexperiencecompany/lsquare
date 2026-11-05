"""Python's values (Python spec, section 2, "Values"): each row of the table, as the walk visits it."""

from __future__ import annotations

import asyncio
import concurrent.futures
import dataclasses
import datetime as dt
import enum
import inspect
import uuid
from collections import OrderedDict
from collections.abc import Iterator, Mapping
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, RootModel, computed_field

from lsquare._event import BuiltOk, Context, build_call, build_turn
from lsquare._jsjson import Budget, dumps, json_length
from lsquare._redact import resolve_patterns
from lsquare._shape import MAX_KEYS, NOT_JSON, new_state, shape_json

CONTEXT = Context(
    sdk={"name": "lsquare", "version": "0.2.0", "language": "python"},
    patterns=resolve_patterns(None),
    redact_keys=[],
    max_event_bytes=256 * 1024,
)


def call(input: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "callId": "call-1",
        "capturedAt": "2026-10-28T00:00:00.000Z",
        "toolName": "search_orders",
        "status": "completed",
        "durationMs": 1,
        "ids": {},
        "diagnostics": False,
        "input": {"value": input},
    }
    built = build_call(record, CONTEXT, False)
    assert isinstance(built, BuiltOk)
    return built.draft.event


def shaped(value: Any) -> tuple[Any, dict[str, str]]:
    """A value as the input of a call, and what was dropped."""
    event = call({"v": value})
    return event["input"].get("v", "<absent>"), event.get("dropped", {})


class TestMappings:
    def test_dicts_and_other_mappings_are_objects_read_in_javascripts_order(self) -> None:
        for value in [{"b": 1, "10": 2, "2": 3}, OrderedDict([("b", 1), ("10", 2), ("2", 3)]), MappingProxyType({"b": 1, "10": 2, "2": 3})]:
            out, dropped = shaped(value)
            assert list(out) == ["2", "10", "b"]
            assert out == {"2": 3, "10": 2, "b": 1}
            assert dropped == {}

    def test_a_mapping_of_its_own_kind(self) -> None:
        class Settings(Mapping[str, int]):
            def __getitem__(self, key: str) -> int:
                return {"a": 1, "0": 2}[key]

            def __iter__(self) -> Iterator[str]:
                return iter(["a", "0"])

            def __len__(self) -> int:
                return 2

        assert list(shaped(Settings())[0]) == ["0", "a"]

    def test_an_entry_whose_key_isnt_a_string_is_dropped_as_not_json(self) -> None:
        out, dropped = shaped({1: "a", "b": 2, (1, 2): 3})
        assert out == {"b": 2}
        assert dropped == {"input": "not_json"}

    def test_a_mapping_that_raises_while_read_is_not_json(self) -> None:
        class Broken(Mapping[str, int]):
            def __getitem__(self, key: str) -> int:
                raise RuntimeError("no")

            def __iter__(self) -> Iterator[str]:
                raise RuntimeError("no")

            def __len__(self) -> int:
                return 1

        out, dropped = shaped(Broken())
        assert out == "<absent>"
        assert dropped == {"input": "not_json"}

    def test_keys_past_the_enumeration_bound_are_too_large(self) -> None:
        state = new_state()
        state.keys = 10
        assert shape_json({f"k{i}": i for i in range(11)}, 16_000, state) is NOT_JSON
        assert state.too_large
        out, dropped = shaped({"0": 1})
        assert out == {"0": 1} and dropped == {}

    def test_an_endless_mapping_costs_no_more_than_the_bound(self) -> None:
        class Endless(Mapping[str, int]):
            def __getitem__(self, key: str) -> int:
                return 0

            def __iter__(self) -> Iterator[str]:
                count = 0
                while True:
                    count += 1
                    yield f"k{count}"

            def __len__(self) -> int:
                return 2**62

        out, dropped = shaped(Endless())
        assert out == "<absent>"
        assert dropped == {"input": "too_large"}
        assert MAX_KEYS == 1_000_000

    def test_a_defaultdict_isnt_changed_by_reading_it(self) -> None:
        from collections import defaultdict

        value: defaultdict[str, int] = defaultdict(int, {"a": 1})
        event = call(value)
        assert event["input"] == {"a": 1}
        assert dict(value) == {"a": 1}


class TestSequences:
    def test_lists_and_tuples_are_arrays(self) -> None:
        assert shaped([1, "a", None])[0] == [1, "a", None]
        assert shaped((1, (2, 3)))[0] == [1, [2, 3]]

    def test_sets_and_other_iterables_are_not_json(self) -> None:
        for value in [{1, 2}, frozenset([1]), range(3), iter([1])]:
            assert shaped(value) == ("<absent>", {"input": "not_json"})


class TestScalars:
    def test_strings_booleans_and_none_are_as_they_are(self) -> None:
        assert shaped("text") == ("text", {})
        assert shaped(True) == (True, {})
        assert shaped(None) == (None, {})

    def test_a_bool_is_not_an_int(self) -> None:
        out, _ = shaped([True, 1])
        assert out[0] is True and type(out[1]) is int
        built = build_turn({"conversationId": "c", "turnId": True}, "2026-10-28T00:00:00.000Z", CONTEXT, False)
        assert isinstance(built, BuiltOk)
        assert built.draft.notes.dropped == {"turnId": "wrong_type"}

    def test_a_str_subclass_is_its_characters(self) -> None:
        class Color(str, enum.Enum):
            RED = "red"

        out, _ = shaped({Color.RED: Color.RED})
        assert out == {"red": "red"} and type(next(iter(out))) is str and type(out["red"]) is str

    def test_ints_are_exact_within_2_53_and_the_nearest_double_beyond(self) -> None:
        out, dropped = shaped([2**53 - 1, 2**53 + 1, -(2**53) - 3, 10**20 + 1])
        assert out == [2**53 - 1, 9007199254740992.0, -9007199254740996.0, 1e20]
        assert dumps(out) == "[9007199254740991,9007199254740992,-9007199254740996,100000000000000000000]"
        assert dropped == {}

    def test_an_int_too_large_for_a_double_is_not_json(self) -> None:
        assert shaped(10**400) == ("<absent>", {"input": "not_json"})
        assert shaped([10**400]) == ([None], {"input": "not_json"})

    def test_nan_and_infinities_are_not_json_and_negative_zero_is_written_0(self) -> None:
        assert shaped([float("nan"), float("inf"), -float("inf")]) == ([None, None, None], {"input": "not_json"})
        event = call({"z": -0.0})
        assert dumps(event["input"]) == '{"z":0}'

    def test_an_int_enum_is_its_number(self) -> None:
        class Level(enum.IntEnum):
            HIGH = 3

        out, _ = shaped(Level.HIGH)
        assert out == 3 and type(out) is int


class TestBinary:
    def test_bytes_bytearray_and_memoryview_are_not_json(self) -> None:
        # As TypeScript drops a Buffer or a typed array: binary data isn't JSON.
        out, dropped = shaped({"a": b"abc", "b": bytearray(4), "c": memoryview(b"xy"), "d": 1})
        assert out == {"d": 1}
        assert set(dropped.values()) == {"not_json"}

    def test_a_released_memoryview_is_not_json(self) -> None:
        view = memoryview(b"xy")
        view.release()
        assert shaped(view) == ("<absent>", {"input": "not_json"})


class Address(BaseModel):
    street: str
    city: str = Field(serialization_alias="town")


class Customer(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: str = Field(alias="fullName")
    secret_note: str = Field(default="hidden", exclude=True)
    address: Address
    tags: list[str] = []

    @computed_field  # type: ignore[prop-decorator]
    @property
    def shout(self) -> str:
        raise AssertionError("computed fields aren't run")

    def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("model_dump is never called")


class TestPydantic:
    def test_a_model_is_read_field_by_field_as_model_dump_by_alias_names_them(self) -> None:
        customer = Customer.model_validate({"fullName": "Jane", "address": {"street": "Via Roma", "city": "Rome"}, "tags": ["vip"], "0": "extra first", "plan": "pro"})
        out, dropped = shaped(customer)
        assert out == {"0": "extra first", "fullName": "Jane", "address": {"street": "Via Roma", "town": "Rome"}, "tags": ["vip"], "plan": "pro"}
        assert list(out) == ["0", "fullName", "address", "tags", "plan"]
        assert dropped == {}

    def test_a_models_fields_follow_every_rule(self) -> None:
        class Login(BaseModel):
            user: str
            password: str

        out, _ = shaped(Login(user="jane@acme.io", password="hunter2"))
        assert out == {"user": "[EMAIL_1]", "password": "[SECRET_1]"}

    def test_a_root_model_is_its_root(self) -> None:
        assert shaped(RootModel[list[int]]([1, 2]))[0] == [1, 2]

    def test_a_long_chain_of_root_models_is_not_json_rather_than_too_deep_for_python(self) -> None:
        value: Any = 1
        for _ in range(2_000):
            value = RootModel[Any](value)
        assert shaped(value) == ("<absent>", {"input": "not_json"})
        assert json_length(value, Budget(100)) == "not_json"

    def test_a_model_class_is_not_a_model(self) -> None:
        assert shaped(Customer) == ("<absent>", {"input": "not_json"})

    def test_a_cyclic_model_is_cut_where_it_repeats(self) -> None:
        class Node(BaseModel):
            name: str
            next: Any = None

        first = Node(name="a")
        first.next = first
        out, dropped = shaped(first)
        assert out == {"name": "a"}
        assert dropped == {"input": "not_json"}


@dataclasses.dataclass
class Point:
    x: int
    y: int
    label: str = "p"


class TestDataclasses:
    def test_a_dataclass_is_read_by_its_fields(self) -> None:
        assert shaped(Point(1, 2)) == ({"x": 1, "y": 2, "label": "p"}, {})

    def test_a_dataclass_type_is_not_json(self) -> None:
        assert shaped(Point) == ("<absent>", {"input": "not_json"})


class TestStandIns:
    def test_a_uuid_is_its_string(self) -> None:
        value = uuid.UUID("12345678-1234-4234-8234-123456789abc")
        assert shaped(value) == ("12345678-1234-4234-8234-123456789abc", {})

    def test_an_enum_is_its_value_read_as_any_value(self) -> None:
        class Plan(enum.Enum):
            PRO = {"tier": 2, "contact": "jane@acme.io"}

        assert shaped(Plan.PRO) == ({"tier": 2, "contact": "[EMAIL_1]"}, {})

    def test_an_aware_datetime_is_utc_with_milliseconds_and_z(self) -> None:
        rome = dt.timezone(dt.timedelta(hours=2))
        value = dt.datetime(2026, 10, 28, 12, 30, 5, 123987, tzinfo=rome)
        assert shaped(value) == ("2026-10-28T10:30:05.123Z", {})
        assert shaped(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)) == ("2026-01-01T00:00:00.000Z", {})

    def test_a_date_is_yyyy_mm_dd(self) -> None:
        assert shaped(dt.date(2026, 10, 28)) == ("2026-10-28", {})

    def test_a_naive_datetime_a_time_and_a_decimal_are_not_json(self) -> None:
        for value in [dt.datetime(2026, 10, 28), dt.time(12, 0), Decimal("1.10")]:
            assert shaped(value) == ("<absent>", {"input": "not_json"})


class TestOtherObjects:
    def test_coroutines_futures_and_generators_are_not_json_and_left_untouched(self) -> None:
        async def work() -> int:
            return 7

        coroutine = work()
        generator = (n for n in range(3))
        future: concurrent.futures.Future[int] = concurrent.futures.Future()
        try:
            for value in [coroutine, generator, future, object(), lambda: 1]:
                assert shaped(value) == ("<absent>", {"input": "not_json"})
            assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CREATED
            assert next(generator) == 0
            assert not future.done()
        finally:
            assert asyncio.run(coroutine) == 7

    def test_an_asyncio_future_is_left_untouched(self) -> None:
        async def check() -> None:
            future = asyncio.get_running_loop().create_future()
            assert shaped(future) == ("<absent>", {"input": "not_json"})
            assert not future.done()
            future.set_result(1)
            assert await future == 1

        asyncio.run(check())


class TestCapture:
    def test_capture_needs_a_mapping(self) -> None:
        for value in [Customer.model_construct(), Point(1, 2), [("question", "q")], "q", None]:
            built = build_turn(value, "2026-10-28T00:00:00.000Z", CONTEXT, False)
            assert not built.ok and built.code == "invalid_input"

    def test_structured_fields_may_be_models_or_dataclasses(self) -> None:
        @dataclasses.dataclass
        class Usage:
            inputTokens: int
            outputTokens: int

        built = build_turn({"usage": Usage(10, 2), "client": {"name": "cursor"}}, "2026-10-28T00:00:00.000Z", CONTEXT, False)
        assert isinstance(built, BuiltOk)
        assert built.draft.event["usage"] == {"inputTokens": 10, "outputTokens": 2}

    def test_an_exception_as_error_is_its_message_and_class_name(self) -> None:
        built = build_turn({"error": TypeError("boom")}, "2026-10-28T00:00:00.000Z", CONTEXT, False)
        assert isinstance(built, BuiltOk)
        assert built.draft.event["error"] == {"message": "boom", "type": "TypeError"}

    def test_metadata_keeps_strings_numbers_booleans_and_none(self) -> None:
        class Tier(str, enum.Enum):
            PRO = "pro"

        class Kind(enum.Enum):
            A = "a"

        built = build_turn({"metadata": {"tier": Tier.PRO, "n": 2**53 + 1, "ok": True, "kind": Kind.A, "big": 10**400}}, "2026-10-28T00:00:00.000Z", CONTEXT, False)
        assert isinstance(built, BuiltOk)
        assert built.draft.event["metadata"] == {"tier": "pro", "n": 9007199254740992.0, "ok": True}
        assert built.draft.event["dropped"] == {"metadata": "not_json"}


class TestPairs:
    def test_a_pair_held_as_two_code_points_is_one_character_in_values_and_keys(self) -> None:
        pair = "\ud83d" + "\ude00"
        out, _ = shaped({pair: pair})
        assert out == {"\U0001f600": "\U0001f600"}

    def test_and_in_lengths(self) -> None:
        assert json_length({"\ud83d" + "\ude00": 1}, Budget(100)) == len('{"\U0001f600":1}')
