"""The deep review's last data findings: Python's values, shortening and redaction, the scan budget,
and what a `capture()` inside a call reads. Where both SDKs must give the same event, the case is in
packages/contract/cases; these are the rest, and the Python-only rules."""

from __future__ import annotations

import dataclasses
import datetime as dt
import enum
import gc
import importlib
import math
import re
import tracemalloc
import uuid
import weakref
from collections.abc import Callable, Iterator, Mapping
from typing import Any, ClassVar, Optional

import pytest
from pydantic import AnyUrl, BaseModel, ConfigDict, Field, create_model

from helpers import FakeTransport, Recorder
from lsquare import LSquare, _jsjson, _normalize, _redact, _shape, _values
from lsquare._client import CallScope
from lsquare._event import BuiltOk, Captured, CaptureUnread, Context, ShapedAgent, after_hook, build_call, build_turn, shape_agent
from lsquare._jsjson import _ESCAPES, Budget, dumps, json_length, size_within, string
from lsquare._redact import ORDER, PATTERNS, SHORTEST_MATCH, SOURCES, _skip, resolve_patterns
from lsquare._shape import CUT_MARK, Binary, Secret, new_state, shape_json
from lsquare._values import object_keys, utf16_length

ALL = resolve_patterns(None)
SDK = {"name": "lsquare", "version": "0.2.0", "language": "python"}
AT = "2026-10-28T00:00:00.000Z"
KEY = "lsq_" + "a" * 40


def context(**over: Any) -> Context:
    values: dict[str, Any] = {"sdk": SDK, "patterns": ALL, "redact_keys": [], "max_event_bytes": 256 * 1024, **over}
    return Context(**values)


def prose(chars: int) -> str:
    """Prose: spaces keep it from looking like base64."""
    return ("lorem ipsum " * math.ceil(chars / 12))[:chars]


def turn(fields: Mapping[str, Any], **over: Any) -> dict[str, Any]:
    built = build_turn(fields, AT, context(**over), False)
    assert isinstance(built, BuiltOk), built
    return built.draft.event


def record(**over: Any) -> dict[str, Any]:
    return {
        "callId": "b3c2f0a4-1111-4111-8111-000000000001",
        "capturedAt": AT,
        "toolName": "search_orders",
        "status": "completed",
        "durationMs": 12,
        "ids": {},
        "diagnostics": False,
        **over,
    }


def call(**over: Any) -> dict[str, Any]:
    built = build_call(record(**over), context(), False)
    assert isinstance(built, BuiltOk), built
    return built.draft.event


def shaped(value: Any) -> tuple[Any, dict[str, str]]:
    """A value as a call's input, and what was dropped."""
    event = call(input={"value": {"v": value}})
    return event["input"].get("v", "<absent>"), event.get("dropped", {})


def client(transport: FakeTransport, recorder: Recorder, **options: Any) -> LSquare:
    return LSquare(api_key=KEY, endpoint="http://localhost:1", _transport=transport, logger=recorder, on_error=recorder.on_error, **options)


# py-data-6: a model's or a dataclass's fields are worked out as the walk enumerates them, each
# entry it reaches charged to the event's key allowance, those it leaves out included.


reads = 0


class Info:
    """A field's metadata, counting each read of it."""

    def __init__(self, **attributes: Any) -> None:
        self.__dict__.update(attributes)

    def __getattribute__(self, name: str) -> Any:
        global reads
        reads += 1
        return object.__getattribute__(self, name)


class Endless(dict):  # type: ignore[type-arg]
    def items(self) -> Any:
        raise AssertionError("a subclass's items() isn't read: the dict's own entries are")


def wide(fields: dict[str, Any], module: str = __name__, extra: dict[str, Any] | None = None) -> Any:
    """A pydantic model by its shape: its class's fields, model_dump, and a fields-set."""

    class Wide:
        model_fields = Endless(fields)

        def model_dump(self) -> dict[str, Any]:
            raise AssertionError("model_dump is never called")

        def __init__(self) -> None:
            self.__pydantic_fields_set__: set[str] = set()
            self.model_extra = extra

        def __getattr__(self, name: str) -> Any:
            return None

    Wide.__module__ = module
    return Wide()


def bounded(value: Any) -> tuple[Any, bool]:
    """Shapes `value` with 3 keys left to enumerate: what it gives, and whether it was too large."""
    state = new_state()
    state.keys = 3
    return shape_json(value, 16_000, state), state.too_large


class TestWideModels:
    def test_a_wide_models_fields_are_read_only_as_far_as_the_key_bound(self) -> None:
        global reads
        model = wide({f"field_{i}": Info(exclude=None, serialization_alias=None, alias=None) for i in range(10_000)})
        reads = 0
        assert bounded(model) == (_shape.NOT_JSON, True)
        # Each field's exclude, serialization_alias and alias, for the 3 the bound allows and the
        # one that shows it's past it: not all 10,000.
        assert reads == 4 * 3

    @pytest.mark.parametrize(
        "info, module",
        [
            ({"exclude": True}, __name__),
            # An MCP SDK type's field holding None, which it writes without.
            ({"exclude": None, "serialization_alias": None, "alias": None}, "mcp.types"),
            # Every field written under one name: listed once.
            ({"exclude": None, "serialization_alias": "same", "alias": None}, __name__),
        ],
        ids=["excluded", "none_in_an_mcp_type", "one_name"],
    )
    def test_a_field_it_leaves_out_counts_against_the_bound_too(self, info: dict[str, Any], module: str) -> None:
        global reads
        model = wide({f"field_{i}": Info(**info) for i in range(10_000)}, module)
        reads = 0
        assert bounded(model) == (_shape.NOT_JSON, True)
        assert reads <= 4 * 3

    def test_so_does_an_extra_field_left_out(self) -> None:
        # 10,000 extra fields holding None, in an MCP SDK type: past the bound of 3, not all read.
        assert bounded(wide({}, "mcp.types", {f"x{i}": None for i in range(10_000)})) == (_shape.NOT_JSON, True)

    def test_a_wide_dataclass_is_read_only_as_far_as_the_key_bound(self) -> None:
        Wide = dataclasses.make_dataclass("Wide", [(f"field_{i}", int, dataclasses.field(default=0)) for i in range(10_000)])
        counted = 0

        class Counted(dataclasses.Field):  # type: ignore[type-arg]
            __slots__ = ()

            def __getattribute__(self, name: str) -> Any:
                nonlocal counted
                counted += 1
                return super().__getattribute__(name)

        for field in dataclasses.fields(Wide):
            field.__class__ = Counted
        counted = 0
        assert bounded(Wide()) == (_shape.NOT_JSON, True)
        # Each field's kind and name, for the 3 the bound allows and the one past it.
        assert counted <= 4 * 3

    def test_so_is_one_with_many_class_variables(self) -> None:
        # ClassVar pseudo-fields are in __dataclass_fields__, not in dataclasses.fields: reached all the same.
        namespace: dict[str, Any] = {f"c{i}": 0 for i in range(10_000)}
        namespace["__annotations__"] = {f"c{i}": ClassVar[int] for i in range(10_000)}
        Many: Any = dataclasses.dataclass(type("Many", (), namespace))
        counted = 0

        class Counted(dataclasses.Field):  # type: ignore[type-arg]
            __slots__ = ()

            def __getattribute__(self, name: str) -> Any:
                nonlocal counted
                counted += 1
                return super().__getattribute__(name)

        for field in Many.__dataclass_fields__.values():
            field.__class__ = Counted
        counted = 0
        assert bounded(Many()) == (_shape.NOT_JSON, True)
        assert counted <= 4 * 3

    def test_its_fields_are_the_ones_dataclasses_fields_gives(self) -> None:
        @dataclasses.dataclass
        class Base:
            a: int = 1
            kind: ClassVar[str] = "base"

        @dataclasses.dataclass
        class Child(Base):
            seed: dataclasses.InitVar[int] = 0
            b: int = dataclasses.field(default=2, kw_only=True)
            c: str = "c"

        out, dropped = shaped(Child())
        assert list(out) == [field.name for field in dataclasses.fields(Child)] == ["a", "b", "c"]
        assert dropped == {}


    # Data review 1: a model the tool results share has its fields worked out once, not once a
    # result, though its keys were listed once: each visit read them all again, by name.
    @pytest.mark.parametrize("kind", ["model", "dataclass"])
    def test_one_many_tool_results_share_has_its_fields_worked_out_once(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        reached = 0
        name = "_model_pairs" if kind == "model" else "_dataclass_pairs"
        pairs = getattr(_values, name)

        def counted(*args: Any) -> Iterator[Any]:
            nonlocal reached
            for pair in pairs(*args):
                reached += 1
                yield pair

        monkeypatch.setattr(_values, name, counted)
        value: Any
        if kind == "model":
            # 2,000 fields marked exclude, and one kept.
            value = create_model("Wide", **{f"f{i}": (int, Field(default=0, exclude=True)) for i in range(2_000)}, kept=(int, 1))()  # type: ignore[call-overload]
        else:
            # 2,000 ClassVars, reached as fields are, and one field.
            namespace: dict[str, Any] = {f"c{i}": 0 for i in range(2_000)}
            namespace["__annotations__"] = {**{f"c{i}": ClassVar[int] for i in range(2_000)}, "kept": int}
            namespace["kept"] = 1
            Wide: Any = dataclasses.dataclass(type("Wide", (), namespace))
            value = Wide()
        event = turn({"toolCalls": [{"name": f"t{i}", "result": value if i % 2 else {"nested": value}} for i in range(100)]})
        assert [call["resultChars"] for call in event["toolCalls"]] == [len('{"nested":{"kept":1}}'), len('{"kept":1}')] * 50
        # Each of the 2,001 once: it was 200,100, all of them again for each of the 99 later results.
        assert reached == 2_001

    def test_its_values_are_still_read_for_each_visit(self) -> None:
        # Only the fields are kept: a value is read as the walk visits it, as any value is.
        class Counted(BaseModel):
            kept: int = 1

            def __getattribute__(self, name: str) -> Any:
                if name == "kept":
                    seen.append(name)
                return super().__getattribute__(name)

        seen: list[str] = []
        value = Counted()
        event = turn({"toolCalls": [{"name": f"t{i}", "result": value} for i in range(3)]})
        assert [call["resultChars"] for call in event["toolCalls"]] == [len('{"kept":1}')] * 3
        assert seen == ["kept"] * 3


# py-data-10: a key written twice holds what model_dump writes there last, in its first place.


class TestModelsAsModelDumpWritesThem:
    def test_an_extra_field_named_as_a_field_is_written_wins_in_the_fields_place(self) -> None:
        class Row(BaseModel):
            model_config = ConfigDict(extra="allow")
            a: int = Field(serialization_alias="b")
            c: int = 3

        row = Row.model_validate({"a": 1, "z": 9, "b": 2})
        # model_dump writes the fields, then the extra ones: a key written twice holds the last value.
        assert list(row.model_dump(by_alias=True).items()) == [("b", 2), ("c", 3), ("z", 9)]
        out, dropped = shaped(row)
        assert list(out.items()) == [("b", 2), ("c", 3), ("z", 9)]
        assert dropped == {}

    def test_a_field_left_out_as_none_replaces_nothing_written_under_its_name(self) -> None:
        class Pair(BaseModel):
            model_config = ConfigDict(extra="allow")
            a: Optional[int] = Field(default=None, serialization_alias="b")
            c: Optional[int] = Field(default=None, serialization_alias="b")

        # One of the MCP SDKs' own types, which they write without the fields holding None.
        Pair.__module__ = "mcp.types"
        for given in ({"a": 1, "c": None}, {"a": None, "c": 1}, {"a": 1, "c": 2}, {"a": None, "c": None}):
            pair = Pair(**given)
            out, _ = shaped(pair)
            assert list(out.items()) == list(pair.model_dump(by_alias=True, exclude_none=True).items()), given

    def test_reads_what_model_dump_writes_in_its_order(self) -> None:
        class Aliased(BaseModel):
            model_config = ConfigDict(extra="allow")
            a: int = Field(validation_alias="x", serialization_alias="b")

        class Twice(BaseModel):
            model_config = ConfigDict(extra="allow")
            a: int = 1
            b: int = Field(default=2, serialization_alias="a")

        class Nested(BaseModel):
            model_config = ConfigDict(extra="allow")
            inner: Optional[int] = Field(default=None, serialization_alias="z")

        for model in (
            Aliased.model_validate({"x": 1, "a": 5, "b": 6}),
            Twice(),
            Twice.model_validate({"a": 1, "zz": 0}),
            Nested.model_validate({"z": {"k": 1}}),
            Nested.model_validate({"inner": 5, "z": None}),
        ):
            out, _ = shaped(model)
            assert list(out.items()) == list(model.model_dump(by_alias=True).items()), model


# Data review 2: an object the event holds more than once is worked out once, and read as it is
# at each visit: its values, a model's extra fields, which field a key written twice holds.


class Then(dict):  # type: ignore[type-arg]
    """A tool call or a content block whose `name` or `type`, read, runs `action` first: the app
    changing an object between two visits of it."""

    def __init__(self, action: Callable[[], None], **fields: Any) -> None:
        super().__init__(**fields)
        self.action = action

    def get(self, key: str, default: Any = None) -> Any:
        if key in ("name", "type"):
            self.action()
        return super().get(key, default)


def counting(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Counts each model field reached (`_values._model_pairs`): the list's one item."""
    reached = [0]
    pairs = _values._model_pairs

    def counted(fields: dict[str, Any]) -> Iterator[Any]:
        for pair in pairs(fields):
            reached[0] += 1
            yield pair

    monkeypatch.setattr(_values, "_model_pairs", counted)
    return reached


def excluded(count: int) -> dict[str, Any]:
    """`count` fields marked exclude, for `create_model`."""
    return {f"f{i}": (int, Field(default=0, exclude=True)) for i in range(count)}


class TestAnObjectHeldAgain:
    def test_has_its_extra_fields_read_again_at_each_visit(self) -> None:
        class Row(BaseModel):
            model_config = ConfigDict(extra="allow", validate_assignment=True)
            a: int = 1

        def assign() -> None:
            setattr(row, "x", 123456789)

        row = Row.model_validate({"x": 1})
        held = row.__pydantic_extra__
        event = turn({"toolCalls": [{"name": "t0", "result": row}, Then(assign, name="t1", result=row)]})
        # An assignment pydantic validates gives the model a new dict of extra fields.
        assert row.__pydantic_extra__ is not held
        assert [call["resultChars"] for call in event["toolCalls"]] == [len('{"a":1,"x":1}'), len('{"a":1,"x":123456789}')]
        # Measured on its own, in a pass's listing: the same.
        row, listed, allowance = Row.model_validate({"x": 1}), _values.Listed(), Budget(1_000)
        assert json_length(row, allowance, listed) == 13
        assign()
        assert json_length(row, allowance, listed) == 21

    def test_has_which_field_a_key_written_twice_holds_read_again_at_each_visit(self) -> None:
        class Pair(BaseModel):
            a: Optional[int] = Field(default=None, serialization_alias="b")
            c: Optional[int] = Field(default=None, serialization_alias="b")

        # One of the MCP SDKs' own types, which they write without the fields holding None.
        Pair.__module__ = "mcp.types"

        def setting(a: Optional[int], c: Optional[int]) -> Callable[[], None]:
            def action() -> None:
                pair.a, pair.c = a, c
                written.append(dumps(pair.model_dump(by_alias=True, exclude_none=True)))

            return action

        pair = Pair(a=1)
        written = [dumps(pair.model_dump(by_alias=True, exclude_none=True))]
        steps = [setting(None, 22), setting(5, None), setting(None, None)]
        event = turn({"toolCalls": [{"name": "t0", "result": pair}, *(Then(step, name=f"t{i + 1}", result=pair) for i, step in enumerate(steps))]})
        assert written == ['{"b":1}', '{"b":22}', '{"b":5}', "{}"]
        # The field it read first holds None at the second visit: 2, as if "b" weren't there.
        assert [call["resultChars"] for call in event["toolCalls"]] == [len(text) for text in written]

    @pytest.mark.parametrize("keys", [1_000_000, 1_000], ids=["within_the_key_bound", "past_it"])
    def test_one_many_tool_calls_repeat_has_its_fields_worked_out_once(self, keys: int, monkeypatch: pytest.MonkeyPatch) -> None:
        reached = counting(monkeypatch)
        value = create_model("Call", **excluded(2_000), name=(str, "t"))()
        state, notes = new_state(), _normalize.new_notes()
        state.keys = keys
        assert _normalize._tool_calls([value] * 10, state, notes, "toolCalls") == [{"name": "t"}] * 10
        # Each of the 2,001 once. Within the bound it was 20,010, all of them again for each later
        # call; past it 21,020, each call's then read by name too, and every field reached again.
        assert reached[0] == 2_001

    def test_and_has_its_keys_charged_for_each_of_them(self) -> None:
        # 1,000 entries each, the fields left out too.
        value = create_model("Call", **excluded(998), name=(str, "t"), tool=(str, "x"))()
        state, notes = new_state(), _normalize.new_notes()
        state.keys = 2_500
        assert _normalize._tool_calls([value] * 5, state, notes, "toolCalls") == [{"name": "t"}] * 5
        # The first two's keys counted, the third's past the 500 left: the allowance used up.
        assert notes.ignored_tool_call_fields == {"tool": 2}
        assert state.keys == 0

    def test_and_has_its_values_read_again_for_each_of_them(self) -> None:
        class Call(BaseModel):
            model_config = ConfigDict(extra="allow", validate_assignment=True)
            name: str

        def change() -> None:
            repeated.name = "second"
            setattr(repeated, "durationMs", 2)

        repeated = Call.model_validate({"name": "first", "durationMs": 1})
        event = turn({"toolCalls": [repeated, Then(change, name="between"), repeated]})
        assert event["toolCalls"] == [{"name": "first", "durationMs": 1}, {"name": "between"}, {"name": "second", "durationMs": 2}]

    def test_whose_fields_couldnt_be_worked_out_has_them_all_at_its_next_visit(self) -> None:
        class Once:
            """A field's metadata whose first read raises."""

            raised = False

            def __getattr__(self, name: str) -> Any:
                if not Once.raised:
                    Once.raised = True
                    raise RuntimeError("not yet")
                return None

        plain = {"exclude": None, "serialization_alias": None, "alias": None}
        model = wide({"a": Info(**plain), "b": Once(), "c": Info(**plain)})
        listed, allowance = _values.Listed(), Budget(1_000)
        assert json_length(model, allowance, listed) == "not_json"
        # Read again from the first field, not only those reached before it raised.
        assert json_length(model, allowance, listed) == len('{"a":null,"b":null,"c":null}')

    def test_one_many_content_blocks_repeat_has_its_fields_worked_out_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reached = counting(monkeypatch)
        block = create_model("Block", **excluded(2_000), type=(str, "text"), text=(str, "hi"))()
        event = call(result={"value": {"content": [block] * 10}})
        assert event["result"]["content"] == [{"type": "text", "text": "hi"}] * 10
        # Each of the 2,002 once: it was 20,020, every field again for each block, read by name.
        assert reached[0] == 2_002

    def test_and_has_its_values_read_again_for_each_of_them_too(self) -> None:
        class Block(BaseModel):
            type: str = "text"
            text: str

        def change() -> None:
            block.text = "two"

        block = Block(text="one")
        event = call(result={"value": {"content": [block, Then(change, type="text", text="between"), block]}})
        assert [shaped["text"] for shaped in event["result"]["content"]] == ["one", "between", "two"]


# Data review 3: a class's fields are worked out once for all its objects in a pass, each object
# keeping its own values, extra fields and keys; an object's class isn't asked again at its next
# visit; a field whose key couldn't be indexed is read again.


class Unasked(dict):  # type: ignore[type-arg]
    """A tool call or a content block whose `__class__` raises once its `name` or `type` is read."""

    def __init__(self, **fields: Any) -> None:
        super().__init__(**fields)
        self.read = False

    @property  # type: ignore[misc]
    def __class__(self) -> type:
        if self.read:
            raise RuntimeError("class unreadable")
        return type(self)

    def get(self, key: str, default: Any = None) -> Any:
        if key in ("name", "type"):
            self.read = True
        return super().get(key, default)


def one_class(kind: str) -> Callable[[], Any]:
    """A class with 2,000 entries left out (fields marked exclude, or ClassVars) and `name`, `type`
    and `text`: each call makes a distinct object of it."""
    if kind == "model":
        return create_model("Wide", **excluded(2_000), name=(str, "t"), type=(str, "text"), text=(str, "hi"))  # type: ignore[no-any-return]
    namespace: dict[str, Any] = {f"c{i}": 0 for i in range(2_000)}
    namespace["__annotations__"] = {**{f"c{i}": ClassVar[int] for i in range(2_000)}, "name": str, "type": str, "text": str}
    namespace.update(name="t", type="text", text="hi")
    made: Callable[[], Any] = dataclasses.dataclass(type("Wide", (), namespace))
    return made


class TestOneClassesObjects:
    def test_a_mapping_whose_class_cant_be_read_at_its_next_visit_is_read_as_at_its_first(self) -> None:
        # Whether it's an object was worked out at its first visit: its class isn't asked again,
        # so the event isn't dropped for it. It was, raising out of the pass.
        tool = Unasked(name="t")
        event = turn({"question": "keep me", "toolCalls": [tool, tool]})
        assert (event["question"], event["toolCalls"], event.get("dropped")) == ("keep me", [{"name": "t"}] * 2, None)
        block = Unasked(type="text", text="hello")
        event = call(result={"value": {"content": [block, block]}})
        assert (event["result"]["content"], event.get("dropped")) == ([{"type": "text", "text": "hello"}] * 2, None)

    def test_and_has_its_keys_listed(self) -> None:
        held = Unasked(name="t", tool="x")
        held.read = True
        keys = object_keys(held, 10)
        assert keys is not None and keys.names == ["name", "tool"]
        listed = _values.Listed()
        held.read = False
        assert listed.source(held) is held
        held.read = True
        assert listed.source(held) is held
        keys = listed.keys(held, 10)
        assert keys is not None and keys.names == ["name", "tool"]

    @pytest.mark.parametrize("kind", ["model", "dataclass"])
    def test_has_its_fields_worked_out_once_for_distinct_objects(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        reached = 0
        name = "_model_pairs" if kind == "model" else "_dataclass_pairs"
        pairs = getattr(_values, name)

        def counted(*args: Any) -> Iterator[Any]:
            nonlocal reached
            for pair in pairs(*args):
                reached += 1
                yield pair

        monkeypatch.setattr(_values, name, counted)
        make = one_class(kind)
        state, notes = new_state(), _normalize.new_notes()
        assert _normalize._tool_calls([make() for _ in range(10)], state, notes, "toolCalls") == [{"name": "t"}] * 10
        # Each of the 2,003 once, for the 10 tool calls: it was 20,030, all of them for each.
        assert reached == 2_003
        reached = 0
        event = call(result={"value": {"content": [make() for _ in range(10)]}})
        assert event["result"]["content"] == [{"type": "text", "text": "hi"}] * 10
        # And for the 10 content blocks, in their own pass: it was 20,030 too.
        assert reached == 2_003

    def test_and_once_for_the_tool_results_measured_in_the_same_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The results have their own listing, for their keys, but the class's fields are the pass's.
        reached = counting(monkeypatch)
        Wide = create_model("Wide", **excluded(2_000), name=(str, "t"), result=(Optional[Any], None))
        calls = [Wide(result=Wide()) for _ in range(10)]
        state, notes = new_state(), _normalize.new_notes()
        assert _normalize._tool_calls(calls, state, notes, "toolCalls") == [{"name": "t", "resultChars": len('{"name":"t","result":null}')}] * 10
        # Each of the 2,002 once, for 10 tool calls and 10 results: it was 40,040.
        assert reached[0] == 2_002

    def test_holds_one_copy_of_its_fields_for_distinct_objects(self) -> None:
        Block = create_model("Block", **{f"f{i}": (int, 0) for i in range(2_000)}, type=(str, "text"), text=(str, "ok"))  # type: ignore[call-overload]
        blocks = [Block() for _ in range(100)]
        given = record(result={"value": {"content": blocks}})
        over = context()
        tracemalloc.start()
        try:
            built = build_call(given, over, False)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert isinstance(built, BuiltOk) and built.draft.event["result"]["content"] == [{"type": "text", "text": "ok"}] * 100
        # About 0.3 MB, the class's 2,002 fields once: it was 31 MB, all of them for each block.
        assert peak < 5_000_000

    def test_and_each_keeps_its_own_values_extra_fields_and_keys(self) -> None:
        class Call(BaseModel):
            model_config = ConfigDict(extra="allow")
            name: str

        first = Call.model_validate({"name": "a", "durationMs": 1})
        second = Call.model_validate({"name": "b", "resultChars": 5, "tool": "x"})
        state, notes = new_state(), _normalize.new_notes()
        shaped_calls = _normalize._tool_calls([first, second, first], state, notes, "toolCalls")
        assert shaped_calls == [{"name": "a", "durationMs": 1}, {"name": "b", "resultChars": 5}, {"name": "a", "durationMs": 1}]
        assert notes.ignored_tool_call_fields == {"tool": 1}

    def test_has_a_field_whose_key_couldnt_be_indexed_at_its_next_visit(self) -> None:
        class Alias(str):
            """A serialization alias whose hash raises once."""

            fail = False

            def __hash__(self) -> int:
                if Alias.fail:
                    Alias.fail = False
                    raise RuntimeError("not yet")
                return str.__hash__(self)

        class Row(BaseModel):
            a: int = Field(default=1, serialization_alias=Alias("x"))
            b: int = 2

        row, listed, allowance = Row(), _values.Listed(), Budget(1_000)
        assert row.model_dump(by_alias=True) == {"x": 1, "b": 2}
        Alias.fail = True
        assert json_length(row, allowance, listed) == "not_json"
        # Both fields: it was 7, `{"b":2}`, the first one reached but never indexed by its key.
        assert json_length(row, allowance, listed) == len('{"x":1,"b":2}')

    def test_is_no_object_at_a_visit_where_its_dataclass_fields_cant_be_read(self) -> None:
        @dataclasses.dataclass
        class Row:
            a: int = 1

        table, reads = Row.__dataclass_fields__, []

        def fields(self: object) -> object:
            """Raises at its first read only."""
            reads.append(1)
            if len(reads) == 1:
                raise RuntimeError("not yet")
            return table

        Row.__dataclass_fields__ = property(fields)  # type: ignore[assignment]
        row, allowance = Row(), Budget(1_000)
        # One read, then not JSON, as before the pass shared declarations: never read again at the
        # same visit, which would record what the class says later.
        assert json_length(row, allowance, _values.Listed()) == "not_json"
        assert reads == [1]
        assert json_length(row, allowance, _values.Listed()) == len('{"a":1}')

    def test_is_read_again_for_each_field_outside_the_tool_calls_and_content_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The spec's promise, no wider: the tool calls are read through the pass's listing, and
        # usage, client and error, read once each, aren't.
        reached = counting(monkeypatch)
        value = create_model("Many", **excluded(20), name=(str, "t"), message=(str, "m"), inputTokens=(int, 1))()
        event = turn({"toolCalls": [value] * 10, "usage": value, "client": value, "error": value})
        assert event["toolCalls"] == [{"name": "t"}] * 10 and event["usage"] == {"inputTokens": 1}
        assert reached[0] == 4 * 23


# py-data-11: two keys Python holds apart that are one key to JavaScript are one entry.

PAIR = chr(0xD83D) + chr(0xDE00)
EMOJI = "\U0001f600"


class TestKeysJavaScriptReadsAsOne:
    def test_are_one_entry_in_result_chars_too(self) -> None:
        value = {EMOJI: 1, PAIR: 2}
        out, _ = shaped(value)
        assert out == {EMOJI: 2}
        assert json_length(value, Budget(100)) == len(dumps(out)) == len('{"\U0001f600":2}')
        event = turn({"toolCalls": [{"name": "t", "result": value}]})
        assert event["toolCalls"] == [{"name": "t", "resultChars": 7}]

    def test_the_value_one_of_them_replaces_is_never_measured(self) -> None:
        # Not JSON, or too large to measure: JavaScript never holds it, so neither matters.
        assert json_length({EMOJI: b"x", PAIR: 2}, Budget(100)) == 7
        assert json_length({EMOJI: ["x" * 1_000] * 100, PAIR: 2}, Budget(100)) == 7
        event = turn({"toolCalls": [{"name": "t", "result": {EMOJI: b"x", "z": 3, PAIR: 2}}]})
        assert event["toolCalls"] == [{"name": "t", "resultChars": len('{"\U0001f600":2,"z":3}')}]
        assert "dropped" not in event

    def test_are_one_entry_where_python_holds_more_of_them_than_the_allowance_left(self) -> None:
        # One key of 200 UTF-16 units, its value and the object one each: 202, as in TypeScript,
        # though Python holds 300 code points of keys.
        value = {EMOJI * 100: 1, PAIR * 100: 2}
        assert json_length(value, Budget(202)) == json_length({EMOJI * 100: 2}, Budget(202)) == 106
        assert json_length(value, Budget(201)) == json_length({EMOJI * 100: 2}, Budget(201)) == "too_large"
        # 1,200,000 code points of keys, one key of 800,000 units within the 1,000,000 allowed.
        event = turn({"toolCalls": [{"name": "t", "result": {EMOJI * 400_000: 1, PAIR * 400_000: 2}}]})
        assert event["toolCalls"] == [{"name": "t", "resultChars": 400_006}]
        assert "dropped" not in event

    def test_are_told_apart_reading_at_most_twice_the_allowance_left_of_the_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        read = 0
        surrogate = _jsjson._SURROGATE

        class Counted:
            def search(self, value: str) -> Any:
                nonlocal read
                read += len(value)
                return surrogate.search(value)

        monkeypatch.setattr(_jsjson, "_SURROGATE", Counted())
        # 900 keys of 100 code points each: the 21st would take it past 2,000 read, so which are
        # one isn't known, and the mapping is too large (below).
        keys = object_keys({"é" * 99 + chr(0x100 + i): i for i in range(900)}, 1_000)
        assert keys is not None
        assert _jsjson._merged(keys, 1_000) is None
        assert read == 2_000
        assert keys.merged is None


    # Data review 1: past what may be read of the keys, too large before any value is read. Three
    # spellings of a 400,000-emoji key, the first two holding what isn't JSON: JavaScript holds
    # the last (400,006, as TypeScript measures it), which reading them as given never reached.
    def test_are_too_large_where_there_are_more_of_them_than_may_be_read(self) -> None:
        value = {PAIR * 400_000: b"x", EMOJI + PAIR * 399_999: b"y", EMOJI * 2 + PAIR * 399_998: 1}
        assert json_length({EMOJI * 400_000: 1}, Budget(1_000_000)) == 400_006
        allowance = Budget(1_000_000)
        assert json_length(value, allowance) == "too_large"
        assert allowance.remaining < 0
        event = turn({"toolCalls": [{"name": "t", "result": value}]})
        assert event["toolCalls"] == [{"name": "t"}]
        assert event["dropped"] == {"toolCalls[0].resultChars": "too_large"}

    # Data review 2: past what may be read of the keys, too large, as above, without a surrogate in
    # any of them, and the allowance used up for the tool results after it. The bound is twice
    # what's left of the allowance, so a mapping need not hold 2,000,000 code points of keys.
    def test_are_too_large_with_the_tool_results_after_them_where_none_holds_a_surrogate(self) -> None:
        # 20,002 code points of keys, none a surrogate, after a value that isn't JSON.
        keys = {"a": b"x", "\u00e9" * 10_001: 1, "\u00ea" * 10_001: 2}
        after = [{"name": "t1", "result": keys}, {"name": "t2", "result": {"x": 1}}]
        # 990,000 of the 1,000,000 used first: past the 20,000 that may be read of the keys then.
        event = turn({"toolCalls": [{"name": "t0", "result": "x" * 990_000}, *after]})
        assert event["toolCalls"] == [{"name": "t0", "resultChars": 990_000}, {"name": "t1"}, {"name": "t2"}]
        # TypeScript measures neither: the first value isn't JSON, and {"x":1} is 7 there.
        assert event["dropped"] == {"toolCalls[1].resultChars": "too_large", "toolCalls[2].resultChars": "too_large"}
        # With the whole allowance left, the keys are read, then the value that isn't JSON.
        event = turn({"toolCalls": after})
        assert event["toolCalls"] == [{"name": "t1"}, {"name": "t2", "resultChars": len('{"x":1}')}]
        assert event["dropped"] == {"toolCalls[0].resultChars": "not_json"}

    def test_and_are_never_told_apart_again_in_the_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        read = 0
        surrogate = _jsjson._SURROGATE

        class Counted:
            def search(self, value: str) -> Any:
                nonlocal read
                read += len(value)
                return surrogate.search(value)

        monkeypatch.setattr(_jsjson, "_SURROGATE", Counted())
        # A value that isn't JSON first, then three spellings of a 400-emoji key: 2,397 code points,
        # past the 1,998 that may be read of them.
        value = {"a": b"x", PAIR * 400: 1, EMOJI + PAIR * 399: 2, EMOJI * 2 + PAIR * 398: 3}
        allowance, listed = Budget(1_000), _values.Listed()
        assert [json_length(value, allowance, listed) for _ in range(5)] == ["too_large"] * 5
        # The first two keys, once: each visit read them again, and found the value that isn't JSON.
        assert read == 800 + 799
        assert allowance.remaining < 0


# py-data-12: a value under a secret key name that came out empty without being empty.


def test_a_secret_made_empty_by_the_budget_or_by_what_json_cant_hold_gets_a_number_of_its_own() -> None:
    # The budget ran out before the list's first item: what it held was never read.
    redactor = _redact.Redactor(ALL)
    state = new_state(budget=len("password") + 1, secret_keys=redactor.secret_key_matcher())
    state.secret_key = state.secret_keys
    assert shape_json({"password": ["hunter2"]}, 16_000, state) == {"password": Secret(None)}
    # Or every entry was left out, as JSON can't hold it: a key that isn't a string, bytes.
    event = call(input={"value": {"password": {1: "hunter2"}, "token": {"k": b"hunter2"}, "auth": {}, "cookies": []}})
    assert event["input"] == {"password": "[SECRET_1]", "token": "[SECRET_2]", "auth": {}, "cookies": []}
    assert event["redactions"] == {"secret": 2}


# py-data-13: a key is charged before its value is read.


class Recorded(Mapping[str, Any]):
    def __init__(self, reads: list[str], **values: Any) -> None:
        self._reads = reads
        self._values = values

    def __getitem__(self, key: str) -> Any:
        self._reads.append(key)
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


def test_reads_no_value_past_the_budget_a_mappings_getter_included() -> None:
    log: list[str] = []
    # "first" costs 6 and its value the 2 left: "second", charged before it's read, never is.
    state = new_state(budget=8)
    out = shape_json(Recorded(log, first="more than the budget", second="s"), 16_000, state)
    assert isinstance(out, dict) and list(out) == ["first"]
    assert state.scan.remaining == 0
    assert log == ["first"]
    # Nor is the next entry of each object still open when it runs out.
    log.clear()
    nested = Recorded(log, a=Recorded(log, b=Recorded(log, c="more than the budget", d="d"), e="e"), f="f")
    shape_json(nested, 16_000, new_state(budget=8))
    assert log == ["a", "b", "c"]


# py-data-14: Python's cost within the shared budget, cut without changing what is built. Counted,
# never timed.


class TestBuildingCost:
    def test_an_event_far_over_256_kb_is_written_only_until_it_is_known_to_be_over(self, monkeypatch: pytest.MonkeyPatch) -> None:
        writes = 0
        write = _jsjson._write

        def counted(*args: Any) -> None:
            nonlocal writes
            writes += 1
            write(*args)

        monkeypatch.setattr(_jsjson, "_write", counted)
        # A million numbers, all within the scan budget: about 2 MB of JSON.
        result = {"content": [{"type": "text", "text": "ok"}], "structuredContent": {"rows": [[list(range(100))] * 100] * 100}}
        event = call(result={"value": result})
        assert "result" not in event and event["truncated"] == ["result"]
        # Each write is at least a byte: about 256 KB of them, not the 2 MB.
        assert writes < 256 * 1024

    def test_a_full_budget_of_short_keys_is_matched_without_normalizing_them(self, monkeypatch: pytest.MonkeyPatch) -> None:
        counts = {"normalize_key": 0, "skip": 0}
        normalize, skip = _redact.normalize_key, _redact._skip

        def counted_normalize(key: str) -> str:
            counts["normalize_key"] += 1
            return normalize(key)

        def counted_skip(pattern: Any, value: str) -> bool:
            counts["skip"] += 1
            return skip(pattern, value)

        monkeypatch.setattr(_redact, "normalize_key", counted_normalize)
        monkeypatch.setattr(_redact, "_skip", counted_skip)
        row = {chr(97 + i // 26) + chr(97 + i % 26): i for i in range(100)}
        call(result={"value": {"content": [{"type": "text", "text": "ok"}], "structuredContent": {"rows": [[row] * 100] * 100}}})
        # ASCII letters and digits have nothing to strip, and a two-letter key is shorter than any
        # pattern's shortest match but the secret one's: before, 664,425 and 1,661,070.
        assert counts["normalize_key"] == 0
        assert counts["skip"] < 400_000

    def test_text_without_a_colon_is_never_searched_for_an_ipv6_address(self, monkeypatch: pytest.MonkeyPatch) -> None:
        searched: list[str] = []
        hint = _redact._IPV6_HINT

        class Counted:
            def search(self, value: str) -> Any:
                searched.append(value)
                return hint.search(value)

        monkeypatch.setattr(_redact, "_IPV6_HINT", Counted())
        # A full budget of prose: a substring scan each, as before the hint, not a regex search.
        event = call(intent="find the order", input={"value": {"q": [prose(16_000)] * 61}}, result={"value": {"content": [{"type": "text", "text": prose(4_000)}]}})
        assert event["truncated"] == ["result", "input"]
        assert searched == []
        # With a colon, the hint is searched, and an address found as before.
        assert call(input={"value": {"q": "from 2001:db8::8a2e:370:7334 at 12:30"}})["input"] == {"q": "from [IP_1] at 12:30"}
        assert searched == ["from 2001:db8::8a2e:370:7334 at 12:30"]

    def test_builds_what_it_built_whatever_the_values_exact_types(self) -> None:
        # The quick paths are taken by exact type: subclasses take the others, which must agree.
        class S(str):
            pass

        class I(int):  # noqa: E742
            pass

        class F(float):
            pass

        class D(dict):  # type: ignore[type-arg]
            pass

        class L(list):  # type: ignore[type-arg]
            pass

        def build(s: type, i: type, f: type, d: type, lst: type) -> dict[str, Any]:
            value = d({s("email"): s("jane@acme.io"), s("n"): lst([i(1), i(4111111111111111), f(1.5), f(2**60), s("x" * 20_000)])})
            return call(input={"value": {s("card"): i(4111111111111111), s("v"): value}}, result={"value": {"structuredContent": lst([d({s("k"): i(2)})])}})

        assert build(S, I, F, D, L) == build(str, int, float, dict, list)

    def test_the_size_it_measures_is_the_one_dumps_writes(self) -> None:
        value = {"é": ["\U0001f600", 1.5, None, True, "\ud800"], "b": "x" * 10}
        size = len(dumps(value).encode("utf-8"))
        assert size_within(value, size) == size
        assert size_within(value, size - 1) is None
        assert size_within(list(range(1_000_000)), 1_000) is None

    def test_escapes_every_character_by_javascripts_table(self) -> None:
        # Written by Python's own C escaping where there's no surrogate: the same, character for character.
        for code in range(0x10000):
            if not 0xD800 <= code <= 0xDFFF:
                text = f'a{chr(code)}"b'
                assert string(text) == '"' + text.translate(_ESCAPES) + '"', hex(code)

    def test_skips_a_pattern_only_where_it_cant_match(self) -> None:
        # A string shorter than a pattern's shortest match, or without what every match holds, isn't
        # searched. The shortest matches are what re's own parser works out.
        try:
            parser: Any = importlib.import_module("re._parser")
        except ImportError:  # Python 3.10
            parser = importlib.import_module("sre_parse")
        assert SHORTEST_MATCH == {name: parser.parse(SOURCES[name], re.ASCII).getwidth()[0] for name in ORDER}
        samples = ["user:pass@", "https://user:pass@host", "Bearer abcdefghijklmnop1", "a ::1 b", "fe80::1", "data[::10]", "1::2:3",
                   "aaaa:bbbb::cccc", "10.0.0.1", "eyJabcdefghij.abcdefghijk.abcdefghijk", "sk-" + "a" * 20, "x@y.zz", "+44 20 7946 0958",
                   "4111 1111 1111 1111", "2001:db8::8a2e:370:7334", "ab", ":", "a:", ":b", "::"]  # fmt: skip
        for text in samples:
            for name in ORDER:
                if len(text) < SHORTEST_MATCH[name] or _skip(name, text):
                    assert PATTERNS[name].search(text) is None, (name, text)


# py-data-19: a value that can't be read as what it says it is.


class Lazy:
    """A lazy proxy, as Django's SimpleLazyObject is: it takes its class from what it wraps, which it
    makes when first asked."""

    def __init__(self, make: Callable[[], Any]) -> None:
        self._make = make

    @property  # type: ignore[misc]
    def __class__(self) -> type:
        return type(self._make())


def failing() -> Any:
    raise ConnectionError("the database is down")


def interrupted() -> Any:
    raise KeyboardInterrupt


PROXIES: dict[str, Callable[[], Any]] = {"failing": lambda: Lazy(failing), "boolean": lambda: Lazy(lambda: True), "string": lambda: Lazy(lambda: "s"), "number": lambda: Lazy(lambda: 5)}


class TestValuesThatCantBeRead:
    def test_a_value_whose_class_cant_be_read_is_one_field_left_out_not_the_event(self) -> None:
        lazy = Lazy(failing)
        event = turn({"question": "q", "conversationId": "c1", "latency": {"user": lazy, "totalMs": 120}, "metadata": {"user": lazy, "plan": "pro"}})
        assert event["latency"] == {"totalMs": 120}
        assert event["metadata"] == {"plan": "pro"}
        assert event["dropped"] == {"latency": "not_json", "metadata": "not_json"}

    def test_a_proxy_passing_for_a_string_or_a_number_is_the_wrong_type_where_one_must_be(self) -> None:
        lazy = Lazy(failing)
        fields = {"question": lazy, "answer": Lazy(lambda: "a"), "usage": {"inputTokens": Lazy(lambda: 5)}, "toolCalls": [{"name": "t", "result": lazy, "resultIsError": Lazy(lambda: True)}]}
        event = turn(fields)
        assert event["dropped"] == {
            "question": "wrong_type",
            "answer": "wrong_type",
            "usage.inputTokens": "wrong_type",
            "toolCalls[0].resultChars": "not_json",
            "toolCalls[0].resultIsError": "wrong_type",
        }

    @pytest.mark.parametrize("kind", list(PROXIES))
    def test_one_nested_anywhere_is_left_out_and_its_field_kept(self, kind: str) -> None:
        proxy = PROXIES[kind]
        event = turn(
            {
                "question": "q",
                "conversationId": "c1",
                "latency": {"x": proxy(), "totalMs": 1},
                "verification": [proxy(), "ok"],
                "metadata": {"x": proxy(), "plan": "pro"},
                "toolCalls": [{"name": "t", "input": {"flag": proxy(), "q": "s"}, "result": {"flag": proxy()}}],
            }
        )
        assert event["latency"] == {"totalMs": 1}
        assert event["verification"] == [None, "ok"]
        assert event["metadata"] == {"plan": "pro"}
        assert event["toolCalls"] == [{"name": "t", "input": {"q": "s"}}]
        assert event["dropped"] == {
            "latency": "not_json",
            "verification": "not_json",
            "toolCalls[0].input": "not_json",
            "toolCalls[0].resultChars": "not_json",
            "metadata": "not_json",
        }
        event = call(
            input={"value": {"flag": proxy(), "password": proxy(), "list": [proxy()]}},
            result={"value": {"content": [{"type": "text", "text": proxy()}, proxy()], "structuredContent": {"flag": proxy()}, "isError": proxy()}},
        )
        assert event["input"] == {"list": [None]}
        assert event["result"] == {"content": [{"type": "text", "text": ""}], "structuredContent": {}}
        assert event["dropped"] == {"input": "not_json", "result.content[1]": "wrong_type", "result.structuredContent": "not_json"}

    def test_an_id_that_cant_be_read_drops_the_event_as_an_invalid_id(self) -> None:
        # An ordinary value can be left out; an ID can't: sent without it, the event could slip past a deletion block.
        for proxy in (Lazy(failing), Lazy(lambda: "c1")):
            built = build_turn({"question": "q", "conversationId": proxy}, AT, context(), False)
            assert not built.ok and built.code == "invalid_id" and built.paths == ["conversationId"]

    def test_a_uuid_whose_str_raises_is_not_json_and_the_event_is_kept(self) -> None:
        class Broken(uuid.UUID):
            def __str__(self) -> str:
                raise RuntimeError("no string")

        value = Broken("12345678-1234-4234-8234-123456789abc")
        assert shaped({"id": value, "n": 1}) == ({"n": 1}, {"input": "not_json"})
        assert json_length(value, Budget(100)) == "not_json"

    def test_an_interrupt_raised_as_a_value_is_read_is_never_swallowed(self) -> None:
        for fields in ({"question": "q", "latency": {"x": Lazy(interrupted)}}, {"question": "q", "metadata": {"x": Lazy(interrupted)}}):
            with pytest.raises(KeyboardInterrupt):
                build_turn(fields, AT, context(), False)
        with pytest.raises(KeyboardInterrupt):
            json_length({"x": Lazy(interrupted)}, Budget(100))


# parity-2: a capture() inside a call reads its data when it is called, as any capture() does.


def captured_in(lsquare: LSquare, scope: CallScope, fields: Mapping[str, Any]) -> None:
    calls = lsquare._lsquare_internal().calls()
    token = calls.set(scope)
    try:
        lsquare.capture(fields)
    finally:
        calls.reset(token)


class TestACaptureInsideACall:
    def test_is_read_when_it_is_called_not_when_the_calls_event_is_built(self) -> None:
        lsquare = client(FakeTransport(), Recorder())
        scope = CallScope(call_id="b3c2f0a4-1111-4111-8111-000000000001")
        tool_calls = [{"name": "search", "durationMs": 5}]
        usage = {"inputTokens": 1, "outputTokens": 1}
        metadata = {"turn": "1"}
        captured_in(lsquare, scope, {"conversationId": "c", "turnId": 1, "question": "first", "toolCalls": tool_calls, "usage": usage, "metadata": metadata})
        # The tool reuses its objects for its next turn, then empties them, before the call's event is built.
        tool_calls[:] = [{"name": "fetch", "durationMs": 7}]
        usage["inputTokens"] = 99
        metadata["turn"] = "2"
        tool_calls.clear()
        metadata.clear()
        event = call(agent=scope.agent)
        assert event["agent"] == {"question": "first", "turnId": "1", "status": "completed", "toolCalls": [{"name": "search", "durationMs": 5}], "usage": {"inputTokens": 1, "outputTokens": 1}}
        assert event["metadata"] == {"turn": "1"}
        lsquare.shutdown(timeout_ms=0)

    def test_that_couldnt_be_read_then_drops_the_call_and_is_never_read_again(self) -> None:
        recorder, transport = Recorder(), FakeTransport()
        lsquare = client(transport, recorder)
        returned = False

        class Flaky(list):  # type: ignore[type-arg]
            """Raises until the tool returns, then reads differently."""

            def __getitem__(self, index: Any) -> Any:
                if not returned:
                    raise RuntimeError("not yet")
                return {"name": "late", "durationMs": 999}

        scope = CallScope(call_id="b3c2f0a4-1111-4111-8111-000000000001")
        captured_in(lsquare, scope, {"conversationId": "c", "question": "q", "toolCalls": Flaky([{}])})
        returned = True
        with pytest.raises(CaptureUnread):
            build_call(record(agent=scope.agent), context(), False)
        lsquare._lsquare_internal().record(record(agent=scope.agent))
        assert lsquare.flush(timeout_ms=1_000).sent == 0
        assert transport.events == []
        assert recorder.wait_for(lambda: recorder.codes() == ["redaction_failed"])
        lsquare.shutdown(timeout_ms=0)

    def test_keeps_only_its_ids_of_what_the_app_gave(self) -> None:
        lsquare = client(FakeTransport(), Recorder())

        class Calls(list):  # type: ignore[type-arg]
            pass

        tool_calls = Calls([{"name": "search"}])
        held = weakref.ref(tool_calls)
        scope = CallScope(call_id="b3c2f0a4-1111-4111-8111-000000000001")
        captured_in(lsquare, scope, {"orgId": "o", "userId": "u", "conversationId": "c", "turnId": 7, "question": "q" * 100_000, "toolCalls": tool_calls})
        del tool_calls
        gc.collect()
        # What the wrapper reads (the IDs that group the call and make its turnRef), and what was read.
        assert held() is None
        assert dict(scope.agent or {}) == {"orgId": "o", "userId": "u", "conversationId": "c", "turnId": 7}
        assert isinstance(scope.agent, Captured) and isinstance(scope.agent.snapshot, ShapedAgent)
        lsquare.shutdown(timeout_ms=0)

    def test_a_capture_from_the_apps_code_shaping_it_runs_is_a_turn_of_its_own(self) -> None:
        transport = FakeTransport()
        lsquare = client(transport, Recorder())
        scope = CallScope(call_id="b3c2f0a4-1111-4111-8111-000000000001")
        returned = False
        ran: list[bool] = []

        class Usage(Mapping[str, Any]):
            def __getitem__(self, key: str) -> Any:
                # Its getter captures (a library logging its own use), before capture() returns.
                if not ran:
                    ran.append(returned)
                    lsquare.capture({"question": "from a getter", "conversationId": "c"})
                return {"inputTokens": 3}[key]

            def __iter__(self) -> Iterator[str]:
                return iter(["inputTokens"])

            def __len__(self) -> int:
                return 1

        captured_in(lsquare, scope, {"question": "agent", "conversationId": "c", "usage": Usage()})
        returned = True
        assert ran == [False]
        event = call(agent=scope.agent)
        assert event["agent"]["usage"] == {"inputTokens": 3}
        lsquare.flush(timeout_ms=1_000)
        assert [(event["question"], event["callId"]) for event in transport.events] == [("from a getter", scope.call_id)]
        lsquare.shutdown(timeout_ms=0)

    def test_a_capture_from_reading_a_top_level_field_comes_first_and_can_take_the_call(self) -> None:
        # Its top-level fields are read before the slot is taken (data review 1): a capture() a
        # mapping's own read makes is the call's agent part, and the one that ran it a linked turn.
        transport = FakeTransport()
        lsquare = client(transport, Recorder())
        scope = CallScope(call_id="b3c2f0a4-1111-4111-8111-000000000001")
        ran: list[str] = []

        class Fields(Mapping[str, Any]):
            def __init__(self, **fields: Any) -> None:
                self.fields = fields

            def __getitem__(self, key: str) -> Any:
                if key == "question" and not ran:
                    ran.append(key)
                    lsquare.capture({"question": "from a getter", "conversationId": "c"})
                return self.fields[key]

            def __iter__(self) -> Iterator[str]:
                return iter(self.fields)

            def __len__(self) -> int:
                return len(self.fields)

        captured_in(lsquare, scope, Fields(question="agent", conversationId="c"))
        assert ran == ["question"]
        assert call(agent=scope.agent)["agent"]["question"] == "from a getter"
        lsquare.flush(timeout_ms=1_000)
        assert [(event["question"], event["callId"]) for event in transport.events] == [("agent", scope.call_id)]
        lsquare.shutdown(timeout_ms=0)

    def test_an_interrupt_while_it_reads_is_never_swallowed(self) -> None:
        lsquare = client(FakeTransport(), Recorder())
        scope = CallScope(call_id="b3c2f0a4-1111-4111-8111-000000000001")

        class Usage(Mapping[str, Any]):
            def __getitem__(self, key: str) -> Any:
                raise KeyboardInterrupt

            def __iter__(self) -> Iterator[str]:
                return iter(["inputTokens"])

            def __len__(self) -> int:
                return 1

        with pytest.raises(KeyboardInterrupt):
            captured_in(lsquare, scope, {"question": "q", "usage": Usage()})
        # Never read, so never sent: the call is dropped when it's built.
        with pytest.raises(CaptureUnread):
            build_call(record(agent=scope.agent), context(), False)
        lsquare.shutdown(timeout_ms=0)

    @staticmethod
    def agent_call(agent: dict[str, Any], early: bool, input_chars: int) -> dict[str, Any]:
        over = {"max_event_bytes": 10 * 1024 * 1024}
        given: dict[str, Any] = dict(agent)
        if early:
            given = Captured(agent)
            given.snapshot = shape_agent(agent, context(**over))
        built = build_call(
            record(
                input={"value": {"q": [prose(16_000)] * (input_chars // 16_000)}},
                result={"value": {"content": [{"type": "text", "text": prose(5_000)}] * 30}},
                agent=given,
            ),
            context(**over),
            False,
        )
        assert isinstance(built, BuiltOk), built
        return built.draft.event

    def test_charges_what_it_read_to_the_calls_budget_in_its_place(self) -> None:
        # Shaped when capture() is called, the agent part leaves the result what it would have had.
        agent = {"question": prose(16_000), "answer": prose(20_000), "turnId": 1}
        late = self.agent_call(agent, False, 880_000)
        early = self.agent_call(agent, True, 880_000)
        texts = [block["text"] for block in early["result"]["content"]]
        assert 0 < sum(text.endswith("…[+1000 chars]") for text in texts) < 30
        assert early == late

    def test_keeps_what_it_read_where_less_was_left_for_it(self) -> None:
        agent = {"question": prose(16_000), "answer": prose(20_000), "turnId": 1}
        late = self.agent_call(agent, False, 976_000)
        early = self.agent_call(agent, True, 976_000)
        assert late["agent"]["answer"].endswith(CUT_MARK)
        assert early["agent"]["answer"] == prose(16_000) + "…[+4000 chars]"
        # The budget is used up there: no block of the result is taken, as in order.
        assert early["result"] == late["result"] == {"content": []}


# parity-3: a top-level field whose read raises drops the event, as a getter's throw does in TypeScript.


class Guarded(dict):  # type: ignore[type-arg]
    """A mapping whose userId can't be read, however it's asked for."""

    def get(self, key: Any, default: Any = None) -> Any:
        if key == "userId":
            raise ValueError("no")
        return super().get(key, default)

    def __getitem__(self, key: Any) -> Any:
        if key == "userId":
            raise ValueError("no")
        return super().__getitem__(key)

    def __contains__(self, key: object) -> bool:
        if key == "userId":
            raise ValueError("no")
        return super().__contains__(key)


class TestAFieldThatCantBeRead:
    def test_drops_a_capture_never_sending_it_without_the_field(self) -> None:
        recorder, transport = Recorder(), FakeTransport()
        lsquare = client(transport, recorder)
        lsquare.capture(Guarded(userId="u1", conversationId="c", question="private"))
        result = lsquare.flush(timeout_ms=1_000)
        assert transport.events == [] and (result.sent, result.failed) == (0, 0)
        assert recorder.wait_for(lambda: recorder.codes() == ["redaction_failed"])
        lsquare.shutdown(timeout_ms=0)

    def test_drops_the_event_when_it_is_in_before_sends_output(self) -> None:
        recorder, transport = Recorder(), FakeTransport()
        lsquare = client(transport, recorder, before_send=lambda event: Guarded(event))
        lsquare.capture({"conversationId": "c", "question": "q"})
        assert lsquare.flush(timeout_ms=1_000).failed == 1
        assert transport.events == []
        assert recorder.wait_for(lambda: recorder.codes() == ["redaction_failed"])
        lsquare.shutdown(timeout_ms=0)

    def test_inside_a_call_reports_the_capture_and_takes_none_of_it(self) -> None:
        recorder = Recorder()
        lsquare = client(FakeTransport(), recorder)
        scope = CallScope(call_id="b3c2f0a4-1111-4111-8111-000000000001")
        captured_in(lsquare, scope, Guarded(userId="u1", conversationId="c", question="private"))
        assert scope.agent is None
        assert recorder.wait_for(lambda: recorder.codes() == ["internal_error"])
        lsquare.shutdown(timeout_ms=0)

    def test_is_read_by_get_inside_a_call_as_outside_one(self) -> None:
        # The reader every capture() has: by `get`, which a dict subclass may override alone.
        class OnlyGet(dict):  # type: ignore[type-arg]
            def get(self, key: Any, default: Any = None) -> Any:
                if key == "userId":
                    raise ValueError("no")
                return super().get(key, default)

        recorder, transport = Recorder(), FakeTransport()
        lsquare = client(transport, recorder)
        lsquare.capture(OnlyGet(userId="u1", conversationId="c", question="private"))
        scope = CallScope(call_id="b3c2f0a4-1111-4111-8111-000000000001")
        captured_in(lsquare, scope, OnlyGet(userId="u1", conversationId="c", question="private"))
        assert scope.agent is None
        lsquare.flush(timeout_ms=1_000)
        assert transport.events == []
        assert recorder.wait_for(lambda: sorted(recorder.codes()) == ["internal_error", "redaction_failed"])
        lsquare.shutdown(timeout_ms=0)

    def test_a_raising_read_inside_a_field_leaves_that_field_out(self) -> None:
        # Only the top level is strict; inside usage, error or a tool call, as before (TypeScript drops the event).
        class Raising(dict):  # type: ignore[type-arg]
            def get(self, key: Any, default: Any = None) -> Any:
                raise ValueError("no")

        event = turn({"question": "q", "usage": Raising(inputTokens=1)})
        assert "usage" not in event


# parity-6: a huge marked string from before_send is read only as far as the budget allows.


def test_reads_only_what_the_budget_allows_of_a_huge_string_before_send_marked_as_cut() -> None:
    pending = build_turn({"question": "q"}, AT, context(), True)
    assert isinstance(pending, BuiltOk)
    huge = "\U0001f600 " * 10_000_000 + CUT_MARK
    tracemalloc.start()
    try:
        built = after_hook({**pending.draft.event, "answer": huge}, pending.draft, context())
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert isinstance(built, BuiltOk) and built.draft.event["answer"] == "\U0001f600 " * 8_000 + CUT_MARK
    # 80 MB as Python holds the string: a copy of it was 88 MB; what the budget reads, about 16.
    assert peak < 40_000_000


# parity-10: a non-ASCII string's JavaScript length is read once per pass, within an allowance.


class TestFileLengths:
    FILE = "A" * 4_096 + "\U0001f600" * 10_000 + "A" * 1_024

    def test_reads_a_non_ascii_strings_length_once_however_many_times_the_event_holds_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        read: list[int] = []

        def counted(value: str) -> int:
            read.append(len(value))
            return utf16_length(value)

        monkeypatch.setattr(_shape, "utf16_length", counted)
        out = shape_json([[self.FILE] * 100] * 100, 4_000, new_state())
        assert isinstance(out, list) and len(out) == 100
        # Every one the budget reached is the file's size, from its UTF-16 length: one read.
        assert out[99] == [Binary((25_120 * 3) // 4)] * 100
        assert sum(read) == len(self.FILE)

    def test_takes_the_length_of_one_past_what_may_be_read_as_its_code_points(self) -> None:
        # Python only, and on purpose unlike TypeScript: past MAX_LENGTH_READS code points in a pass,
        # a string's length is its code points, so an emoji counts once, and its size is lower.
        state = new_state()
        state.length_reads = 20_000
        second = "B" * 4_096 + "\U0001f601" * 10_000 + "B" * 1_024
        assert shape_json([self.FILE, second, self.FILE], 4_000, state) == [
            Binary((25_120 * 3) // 4),
            Binary((15_120 * 3) // 4),
            Binary((25_120 * 3) // 4),
        ]


# parity-13: a turnId's size is its UTF-8 bytes as JSON writes it (packages/contract/cases/ids.json
# has the cases).


def test_a_turn_id_just_within_256_kb_as_json_writes_it_is_kept() -> None:
    within = "\U0001f600" * (256 * 1024 // 4)
    event = turn({"conversationId": "c", "turnId": within}, max_event_bytes=1024 * 1024)
    assert event["turnId"] == within and event["eventId"].startswith("k/")
    escaped = '"' * (256 * 1024 // 2)
    assert turn({"conversationId": "c", "turnId": escaped}, max_event_bytes=1024 * 1024)["turnId"] == escaped
    assert turn({"conversationId": "c", "turnId": escaped + '"'})["dropped"] == {"turnId": "too_large"}


# parity-17: the error's category stays, whatever the hook puts in its place.


def test_keeps_the_errors_category_when_the_hook_replaces_it_with_something_else() -> None:
    def pending() -> Any:
        built = build_call(record(status="error", error={"message": "boom", "category": "thrown"}), context(), True)
        assert isinstance(built, BuiltOk)
        return built.draft

    rest = {key: value for key, value in pending().event.items() if key != "error"}
    for other in (False, "hidden", ["x"], 0):
        built = after_hook({**rest, "error": other}, pending(), context())
        assert isinstance(built, BuiltOk)
        assert built.draft.event["error"] == {"category": "thrown"}
        assert built.draft.event["dropped"] == {"beforeSend.error": "wrong_type"}


# parity-18: a short field over 16,000 characters is too_large, wherever it is: here, set by the hook.


def test_one_too_long_in_before_sends_output_is_too_large_there() -> None:
    long = "x" * 16_001

    def pending() -> Any:
        built = build_call(record(client={"name": "c"}, server={"name": "s"}, protocolVersion="2025-06-18"), context(), True)
        assert isinstance(built, BuiltOk)
        return built.draft

    draft = pending()
    out = {**draft.event, "client": {"name": long}, "server": {"name": "s", "version": long}, "protocolVersion": long, "task": {"id": "t", "status": long}}
    after = after_hook(out, draft, context())
    assert isinstance(after, BuiltOk)
    event = after.draft.event
    assert "client" not in event and event["server"] == {"name": "s"} and "protocolVersion" not in event and "task" not in event
    assert event["dropped"] == {
        "beforeSend.server.version": "too_large",
        "beforeSend.client": "too_large",
        "beforeSend.protocolVersion": "too_large",
        "beforeSend.task": "too_large",
    }
    # Of another type, left out without a word, as before: the protocol may not give one.
    draft = pending()
    after = after_hook({**draft.event, "client": 5, "server": {"version": "1"}}, draft, context())
    assert isinstance(after, BuiltOk) and "dropped" not in after.draft.event
    assert "client" not in after.draft.event and "server" not in after.draft.event


# parity-20: numbering skips a placeholder anywhere in the event, with a hook as without.


def test_numbering_skips_a_placeholder_in_a_field_never_redacted_with_a_hook_as_without() -> None:
    fields = {"userId": "[EMAIL_1]", "question": "mail jane@acme.io", "surface": "[EMAIL_2]", "toolCalls": [{"name": "[EMAIL_3]"}]}
    assert turn(fields)["question"] == "mail [EMAIL_4]"
    pending = build_turn(fields, AT, context(), True)
    assert isinstance(pending, BuiltOk)
    after = after_hook(dict(pending.draft.event), pending.draft, context())
    assert isinstance(after, BuiltOk) and after.draft.event["question"] == "mail [EMAIL_4]"
    # And in a call: its tool name, server and client, set by the hook too.
    built = build_call(record(toolName="[EMAIL_1]", input={"value": {"email": "jane@acme.io"}}), context(), True)
    assert isinstance(built, BuiltOk)
    out = {**built.draft.event, "client": {"name": "[EMAIL_2]"}, "server": {"name": "s", "version": "[EMAIL_3]"}}
    after = after_hook(out, built.draft, context())
    assert isinstance(after, BuiltOk)
    assert after.draft.event["input"] == {"email": "[EMAIL_4]"}
    assert after.draft.event["client"] == {"name": "[EMAIL_2]"}


# parity-7: a block's fields are strings, as in TypeScript, except that a pydantic URL is its string.


class Link(BaseModel):
    type: str = "resource_link"
    uri: Any
    name: Any = None


class Contents(BaseModel):
    uri: AnyUrl
    mimeType: Optional[str] = None
    text: Any = None


class Embedded(BaseModel):
    type: str = "resource"
    resource: Contents


class Text(BaseModel):
    type: str = "text"
    text: Any


class Color(enum.Enum):
    RED = "red"


def content(blocks: list[Any]) -> list[dict[str, Any]]:
    event = call(result={"value": {"content": blocks, "isError": False}})
    assert "dropped" not in event, event.get("dropped")
    out: list[dict[str, Any]] = event["result"]["content"]
    return out


def test_a_resource_blocks_pydantic_url_is_its_string() -> None:
    assert content([Link(uri=AnyUrl("file:///docs/readme.md"), name="readme"), Embedded(resource=Contents(uri=AnyUrl("file:///a.txt"), mimeType="text/plain", text="hello"))]) == [
        {"type": "resource_link", "uri": "file:///docs/readme.md", "name": "readme"},
        {"type": "resource", "uri": "file:///a.txt", "mimeType": "text/plain", "text": "hello"},
    ]


def test_a_block_field_that_isnt_a_string_or_a_pydantic_url_is_left_out_as_in_typescript() -> None:
    # A date, a UUID or an enum stand for a string as a value; not as a block's field, as TypeScript
    # drops a Date there.
    when = dt.datetime(2026, 10, 28, 1, 2, 3, 4_000, tzinfo=dt.timezone.utc)
    blocks = [Text(text=when), Text(text=Color.RED), Link(uri=AnyUrl("https://example.com/x"), name=uuid.UUID(int=1)), Link(uri=when, name=Color.RED)]
    assert content(blocks) == [
        {"type": "text", "text": ""},
        {"type": "text", "text": ""},
        {"type": "resource_link", "uri": "https://example.com/x"},
        {"type": "resource_link"},
    ]
