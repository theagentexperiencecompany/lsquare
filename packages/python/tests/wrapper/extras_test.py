"""The extras' rules that don't depend on a call (Python spec, section 5; shared spec, section 4):
the listed copy that shows session_id and intent, a listed schema's ownership, session IDs and
get_more_tools. Ported from what packages/lsquare/test/mcp-extras.test.ts and mcp-low.test.ts
check of extras.ts."""

from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Iterator, Mapping
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest

from lsquare.mcp._call import group, session_hash
from lsquare.mcp._extras import (
    COULD_NOT_READ,
    INTENT,
    MISSING_TOOLS,
    ROOT_KEYS,
    SESSION_FORMAT,
    SESSION_ID,
    Ownership,
    advertise,
    id_block,
    listed_ownership,
    missing_tools_reply,
    missing_tools_tool,
    new_session_id,
    same_ownership,
)

BOTH = [SESSION_ID, INTENT]
REPLY = "Noted. No other tools are available right now."
SESSION_DESCRIPTION = (
    "The ID of the task this call belongs to. Leave it out on your first call. On every later call for the same task, "
    "send the ID an earlier result gave you, and pass it to any subagent working on that task."
)
INTENT_DESCRIPTION = "In one sentence, why you're calling this tool and what the user is trying to do. No personal data or secrets."


def schema(**more: Any) -> dict[str, Any]:
    return {"type": "object", "properties": {"q": {"type": "string"}}, **more}


def deep(levels: int) -> dict[str, Any]:
    return {} if levels == 0 else {"nested": deep(levels - 1)}


class Lazy(Mapping[str, Any]):
    """An object whose values are read one at a time, counting each read."""

    def __init__(self, size: int) -> None:
        self.size = size
        self.reads = 0

    def __iter__(self) -> Iterator[str]:
        return (f"k{index}" for index in range(self.size))

    def __len__(self) -> int:
        return self.size

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and re.fullmatch(r"k\d+", key) is not None and int(key[1:]) < self.size

    def __getitem__(self, key: str) -> Any:
        if key not in self:
            raise KeyError(key)
        self.reads += 1
        return 1


class Raising(Mapping[str, Any]):
    """A schema whose `properties` can't be read."""

    def __iter__(self) -> Iterator[str]:
        return iter(["type", "properties"])

    def __len__(self) -> int:
        return 2

    def __getitem__(self, key: str) -> Any:
        if key == "properties":
            raise RuntimeError("no")
        if key == "type":
            return "object"
        raise KeyError(key)


# advertise: the root


def test_adds_both_as_optional_strings_with_their_descriptions_on_a_copy() -> None:
    given = schema(required=["q"], additionalProperties=False, title="t", description="d", **{"$defs": {}})
    before = copy.deepcopy(given)
    listed = advertise(given, BOTH)
    assert listed == {
        **given,
        "properties": {
            "q": {"type": "string"},
            "session_id": {"type": "string", "description": SESSION_DESCRIPTION},
            "intent": {"type": "string", "description": INTENT_DESCRIPTION},
        },
    }
    assert given == before
    assert listed is not None and listed is not given and listed["properties"] is not given["properties"]
    # The keys keep their order, with properties where it was.
    assert list(listed) == list(given)


def test_adds_properties_to_a_root_without_any_and_reads_any_mapping() -> None:
    assert advertise({"type": "object"}, [SESSION_ID]) == {
        "type": "object",
        "properties": {"session_id": {"type": "string", "description": SESSION_DESCRIPTION}},
    }
    listed = advertise(MappingProxyType(schema()), [INTENT])
    assert isinstance(listed, dict) and list(listed["properties"]) == ["q", "intent"]


def test_every_key_that_only_describes_is_allowed_at_the_root() -> None:
    describing: dict[str, Any] = {key: {} for key in ROOT_KEYS - {"type", "properties", "required", "additionalProperties"}}
    assert advertise(schema(**describing), BOTH) is not None


@pytest.mark.parametrize(
    "more",
    [
        {"maxProperties": 3},
        {"const": {"q": "a"}},
        {"enum": [{"q": "a"}]},
        {"dependentRequired": {"q": ["q"]}},
        {"additionalProperties": True},
        {"additionalProperties": {}},
        {"additionalProperties": 0},
        {"oneOf": []},
        {"$ref": "#/$defs/x"},
        {"type": "string"},
        {"type": ["object"]},
        {"properties": []},
        {"properties": None},
        {"required": "q"},
        {"required": None},
    ],
)
def test_lists_nothing_more_where_the_root_could_change_what_it_accepts(more: dict[str, Any]) -> None:
    assert advertise(schema(**more), BOTH) is None


def test_leaves_out_an_input_the_schema_already_has_or_requires() -> None:
    listed = advertise(schema(required=["q", "session_id"]), BOTH)
    assert listed is not None and list(listed["properties"]) == ["q", "intent"]
    listed = advertise({"type": "object", "properties": {"intent": {"type": "number"}}}, BOTH)
    assert listed is not None and listed["properties"]["intent"] == {"type": "number"}
    assert list(listed["properties"]) == ["intent", "session_id"]
    assert advertise(schema(required=("session_id", "intent")), BOTH) is None
    assert advertise({"type": "object", "properties": {"session_id": {}, "intent": {}}}, BOTH) is None


def test_lists_nothing_for_what_is_not_a_schema_or_no_names() -> None:
    for given in [None, [], "object", 1, [schema()], (schema(),)]:
        assert advertise(given, BOTH) is None
    assert advertise(schema(), []) is None
    assert advertise({1: "x", "type": "object"}, BOTH) is None


def test_lists_nothing_when_reading_the_schema_raises() -> None:
    assert advertise(Raising(), BOTH) is None


# advertise: references and the budget


@pytest.mark.parametrize(
    "ref",
    [
        "#",
        "#/",
        "#/properties/session_id",
        "#/properties/session_id/x",
        "#/properties/session%5Fid",
        "#/$defs/session%5Fid",
        "other.json#/properties/q",
        "#anchor",
        1,
        None,
    ],
)
def test_refuses_a_reference_that_could_reach_the_root_or_an_input_being_added(ref: Any) -> None:
    assert advertise(schema(**{"$defs": {"x": {"$ref": ref}}}), [SESSION_ID]) is None


@pytest.mark.parametrize("ref", ["#/$defs/y", "#/definitions/y", "#/properties/q", "#/properties/q/items", "#/properties/intent"])
def test_takes_a_reference_into_its_definitions_or_its_own_properties(ref: str) -> None:
    assert advertise(schema(**{"$defs": {"x": {"$ref": ref}, "y": {}}, "definitions": {"y": {}}}), [SESSION_ID]) is not None


def test_reads_an_escaped_property_name_as_the_name_it_spells() -> None:
    assert advertise({"type": "object", "properties": {"a/b": {}, "c": {"$ref": "#/properties/a~1b"}}}, [SESSION_ID]) is not None
    assert advertise(schema(**{"$defs": {"x": {"$ref": "#/properties/session_id"}}}), [INTENT]) is not None
    assert advertise(schema(**{"$defs": {"x": {"$ref": "#/properties/session_id"}}}), BOTH) is None


def test_refuses_dynamic_references() -> None:
    assert advertise(schema(**{"$defs": {"x": {"$dynamicRef": "#meta"}}}), [SESSION_ID]) is None
    assert advertise(schema(**{"$defs": {"x": {"$recursiveRef": "#"}}}), [SESSION_ID]) is None
    assert advertise(schema(**{"$defs": {"x": [{"$ref": "#"}]}}), [SESSION_ID]) is None


def test_lists_a_reused_schema_that_refers_to_its_own_properties() -> None:
    reused = {"type": "object", "properties": {"from": {"type": "string"}, "to": {"$ref": "#/properties/from"}}}
    listed = advertise(reused, BOTH)
    assert listed is not None and list(listed["properties"]) == ["from", "to", "session_id", "intent"]
    assert listed["properties"]["to"] == {"$ref": "#/properties/from"}


def test_reads_at_most_its_budget_of_entries_however_wide_or_deep_the_schema() -> None:
    examples = Lazy(20_001)
    assert advertise(schema(examples=examples), [SESSION_ID]) is None
    assert examples.reads <= 10_000
    assert advertise(schema(examples=[1] * 20_001), [SESSION_ID]) is None
    assert advertise(schema(examples=(1,) * 20_001), [SESSION_ID]) is None
    assert advertise(schema(default=deep(70)), [SESSION_ID]) is None
    assert advertise(schema(default=deep(10)), [SESSION_ID]) is not None
    assert advertise(schema(examples=[{"a": 1}] * 100), [SESSION_ID]) is not None
    small = Lazy(10)
    assert advertise(schema(examples=small), [SESSION_ID]) is not None and small.reads == 10


def test_ends_on_a_schema_that_holds_itself() -> None:
    looped: dict[str, Any] = schema()
    looped["default"] = looped
    assert advertise(looped, [SESSION_ID]) is None


# listed_ownership and same_ownership


def read(given: object) -> list[str] | str:
    owner = listed_ownership(given)
    return sorted(owner.declared) if owner.readable else "unreadable"


def test_reads_named_properties_and_required_names_and_nothing_more() -> None:
    assert read({"type": "object"}) == []
    assert read(
        {"type": "object", "properties": {"q": {}, "intent": {}}, "required": ["session_id"], "additionalProperties": False, "title": "t", "$defs": {}}
    ) == ["intent", "session_id"]
    assert read({"type": "object", "required": ("q", "intent")}) == ["intent"]
    assert read(MappingProxyType({"type": "object", "properties": {"session_id": {}}})) == ["session_id"]


@pytest.mark.parametrize(
    "given",
    [
        None,
        [],
        {"type": "string"},
        {"properties": {}},
        {"type": "object", "properties": []},
        {"type": "object", "properties": None},
        {"type": "object", "required": "q"},
        {"type": "object", "required": [1]},
        {"type": "object", "required": ["q"] * 10_001},
        {"type": "object", "additionalProperties": True},
        {"type": "object", "additionalProperties": {}},
        {"type": "object", "oneOf": []},
        {"type": "object", "$ref": "#/$defs/x"},
        Raising(),
    ],
)
def test_reads_anything_else_as_unreadable(given: object) -> None:
    owner = listed_ownership(given)
    assert owner == Ownership(False, frozenset(), "its listed input schema is more than an object of named properties")


def test_same_ownership_compares_only_the_extras() -> None:
    unreadable = Ownership.unreadable("x")
    assert same_ownership(unreadable, COULD_NOT_READ)
    assert not same_ownership(unreadable, Ownership.of([]))
    assert not same_ownership(Ownership.of([]), unreadable)
    assert same_ownership(Ownership.of(["q", SESSION_ID]), Ownership.of([SESSION_ID]))
    assert not same_ownership(Ownership.of([SESSION_ID]), Ownership.of([INTENT]))
    assert same_ownership(Ownership.of(BOTH), Ownership.of([INTENT, SESSION_ID]))


def test_ownership_keeps_only_the_extras_and_owns_everything_when_unreadable() -> None:
    owner = Ownership.of(["q", "intent", "need"])
    assert owner == Ownership(True, frozenset({"intent"}))
    assert owner.owns(INTENT) and not owner.owns(SESSION_ID)
    assert COULD_NOT_READ.owns(SESSION_ID) and COULD_NOT_READ.owns(INTENT)
    assert COULD_NOT_READ == Ownership(False, frozenset(), "it could not be read")


# Session IDs, the ID line and get_more_tools


def test_session_ids_have_the_format_and_spread_over_the_whole_alphabet() -> None:
    ids = [new_session_id() for _ in range(2_000)]
    assert all(SESSION_FORMAT.match(value) for value in ids)
    assert len(set(ids)) == len(ids)
    assert set("".join(value[4:] for value in ids)) == set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("ses_AbCdEfGhIjKlMnOpQrStUv", True),
        ("ses_-_0123456789abcdefghij", True),
        ("ses_AbCdEfGhIjKlMnOpQrStUv\n", False),
        ("ses_AbCdEfGhIjKlMnOpQrStU", False),
        ("ses_AbCdEfGhIjKlMnOpQrStUvW", False),
        ("SES_AbCdEfGhIjKlMnOpQrStUv", False),
        (" ses_AbCdEfGhIjKlMnOpQrStUv", False),
        ("ses_AbCdEfGhIjKlMnOpQrStUé", False),
        ("ses_AbCdEfGhIjKlMnOpQrStUａ", False),
        ("ses_AbCdEfGhIjKlMnOpQrSt=v", False),
    ],
)
def test_session_format(value: str, valid: bool) -> None:
    assert (SESSION_FORMAT.match(value) is not None) is valid
    assert (SESSION_FORMAT.search(value) is not None) is valid


def test_id_block() -> None:
    assert id_block("ses_AbCdEfGhIjKlMnOpQrStUv") == {
        "type": "text",
        "text": "Task ID: ses_AbCdEfGhIjKlMnOpQrStUv. Send it as session_id on later calls for this task, and pass it to any subagent working on it.",
    }


def test_a_protocol_sessions_hash_is_the_typescript_sdks() -> None:
    # The same value is in packages/lsquare/test/mcp-extras.test.ts.
    assert session_hash("issued-by-server") == "00df196023a72b80c4512e3ae6f12a72fe24e65973fa7780d8ccbd5b64fe2fc9"
    # A lone surrogate is written as U+FFFD, a pair as one character.
    assert session_hash("\ud800") == hashlib.sha256("\ufffd".encode()).hexdigest()
    assert session_hash("\ud83d\ude00") == hashlib.sha256("\U0001f600".encode()).hexdigest()


def test_the_transports_own_id_sent_back_is_still_that_session_under_its_hash() -> None:
    id = "ses_AbCdEfGhIjKlMnOpQrStUv"
    call: Any = SimpleNamespace(extras=SimpleNamespace(grouping=True, sent=id), protocol_session=id, scope=SimpleNamespace(agent=None), session=None)
    group(call, None, "result", None)  # type: ignore[arg-type]
    assert call.session == (session_hash(id), "protocol")
    call.protocol_session = "another"
    group(call, None, "result", None)  # type: ignore[arg-type]
    assert call.session == (id, "sent")


def test_get_more_tools_listing() -> None:
    need = {"type": "string", "description": "What you needed to do and couldn't, in one or two sentences. No personal data or secrets."}
    expected = {
        "name": MISSING_TOOLS,
        "description": (
            "Call this when none of the available tools fits what you need, or one fits only in part. It doesn't return "
            "new tools: it tells the server's developers what to add."
        ),
        "inputSchema": {"type": "object", "properties": {"need": need}, "required": ["need"]},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
    }
    assert MISSING_TOOLS == "get_more_tools"
    assert missing_tools_tool(False) == expected
    with_sessions = missing_tools_tool(True)
    assert with_sessions["inputSchema"]["properties"] == {"need": need, "session_id": {"type": "string", "description": SESSION_DESCRIPTION}}
    # Each listing is its own copy.
    with_sessions["inputSchema"]["required"].append("x")
    assert missing_tools_tool(True)["inputSchema"]["required"] == ["need"]


def test_get_more_tools_replies() -> None:
    assert missing_tools_reply("Cancel an order") == {"content": [{"type": "text", "text": REPLY}]}
    assert missing_tools_reply("") == {"content": [{"type": "text", "text": REPLY}]}
    for need in [3, None, ["x"], {"need": "x"}, b"x"]:
        reply = missing_tools_reply(need)
        assert reply == {
            "content": [
                {
                    "type": "text",
                    "text": "Invalid arguments for tool get_more_tools: need is required, as a string saying what you needed to do and couldn't.",
                }
            ],
            "isError": True,
        }
