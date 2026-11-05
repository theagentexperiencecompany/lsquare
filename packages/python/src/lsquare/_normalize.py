"""Reading `capture()`'s fields and a call's agent part into shaped form, as normalize.ts does.

`None` is JSON's null and `MISSING` JavaScript's undefined: a field that isn't there. TypeScript
tells them apart in places (an explicit null tool input is kept, a null `sessionId` is invalid), and
so does this module.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from functools import partial
from typing import Any, Final, Generic, Literal, TypedDict, TypeVar, Union, cast

from ._jsjson import Budget, dumps, json_length, number, read_fields, string
from ._redact import FREE_FORM, JSON_FIELD, TEXT_FIELD, Redactor
from ._shape import LIMITS, MAX_ENTRIES, NOT_JSON, Shaped, ShapeState, advance, shape_json, shape_string, spend
from ._types import BuildError, DroppedReason, Json
from ._values import (
    MISSING,
    Keys,
    Listed,
    Missing,
    as_object,
    is_finite,
    is_safe_integer,
    join,
    joined_prefix,
    js_number,
    js_order,
    object_keys,
    text,
    utf16_length,
)

T = TypeVar("T")

#: The ingestion's limit for IDs, and for event IDs.
MAX_ID_BYTES: Final = 512
#: Short fields kept as they are (`surface`, `apiKeyId`, names, types): longer ones are dropped as too_large.
MAX_SHORT_CHARS: Final = 16_000
#: A larger turnId couldn't fit in an event: dropped as too_large before it's hashed. Its UTF-8
#: bytes as the event writes it, in JSON without its quotes.
MAX_TURN_ID_BYTES: Final = 256 * 1024
#: Work allowed to measure the non-string tool results of one event, which are never sent.
RESULT_MEASURE_LIMIT: Final = 1_000_000
#: Unknown top-level fields counted for the dry run; the rest are ignored without looking.
MAX_IGNORED_FIELDS: Final = 1_000

#: Fields that identify the event: checked, never shaped or redacted, and restored after `before_send`.
ID_FIELDS: Final = ("orgId", "userId", "conversationId", "toolName", "callId")
TURN_FIELDS: Final = (
    "question", "answer", "turnId", "status", "error", "toolCalls", "usage", "latency", "verification", "client",
    "apiKeyId", "surface",
)  # fmt: skip
INPUT_FIELDS: Final = (*ID_FIELDS, *TURN_FIELDS, "metadata")
_INPUT_FIELD_SET: Final = frozenset(INPUT_FIELDS)
TOOL_CALL_FIELDS: Final = ("name", "input", "resultChars", "resultIsError", "errorMessage", "durationMs")
_TOOL_CALL_FIELD_SET: Final = frozenset(TOOL_CALL_FIELDS)
SDK_FIELDS: Final = frozenset(["eventId", "capturedAt", "source", "schemaVersion", "kind", "sdk", "redactions", "dropped", "truncated"])

#: Field names likely to be used instead of ours. Only these are reported by name: any other key
#: could be data, so it is counted as "(other)".
KNOWN_ALIASES: Final = frozenset([
    "tool", "tool_name", "toolName", "args", "arguments", "params", "parameters", "result", "output", "response",
    "isError", "is_error", "error", "duration", "duration_ms", "latency", "latencyMs", "id", "type",
    "sessionId", "session_id", "user_id", "user", "messages", "content", "role", "createdAt", "timestamp",
    "inputTokens", "outputTokens", "cost", "costUsd",
])  # fmt: skip

Ids = dict[str, str]
Metadata = dict[str, Union[str, int, float, bool, None]]


@dataclass(frozen=True)
class Valid(Generic[T]):
    """A value that passed its check (TypeScript's `{ value }`)."""

    value: T


@dataclass(frozen=True)
class Reason:
    """Why a value was removed (TypeScript's `{ reason }`)."""

    reason: DroppedReason


Check = Union[Valid[T], Reason, None]


@dataclass
class BuiltError:
    """An event that was dropped while it was built, and why."""

    code: BuildError
    paths: list[str]
    ok: Literal[False] = False


class ShapedToolCall(TypedDict, total=False):
    name: str
    input: Shaped
    resultChars: Union[int, float]
    resultIsError: bool
    errorMessage: Shaped
    durationMs: Union[int, float]


class ShapedError(TypedDict, total=False):
    message: Shaped
    type: str


class ShapedTurn(TypedDict, total=False):
    """Shaped turn fields: strings still `Cut`s until finished with a Redactor."""

    question: Shaped
    answer: Shaped
    turnId: str
    status: Literal["completed", "error"]
    error: ShapedError
    toolCalls: list[ShapedToolCall]
    usage: dict[str, Union[int, float]]
    latency: Shaped
    verification: Shaped
    client: Union[str, dict[str, str]]
    apiKeyId: str
    surface: str


@dataclass
class Notes:
    """Where shaping reports what it removed and shortened, for one event."""

    dropped: dict[str, DroppedReason] = field(default_factory=dict)
    #: Top-level fields shortened, in the order they were (a dict as an ordered set).
    truncated: dict[str, None] = field(default_factory=dict)
    ignored_tool_call_fields: dict[str, int] = field(default_factory=dict)


def new_notes() -> Notes:
    return Notes()


def shape_field(state: ShapeState, notes: Notes, name: str, run: Callable[[], T]) -> T:
    """Shapes one field, recording it as truncated if anything in it was shortened."""
    before = state.shortened
    state.shortened = False
    try:
        return run()
    finally:
        if state.shortened:
            notes.truncated[name] = None
        state.shortened = before or state.shortened


def shape_any(value: object, limit: int, state: ShapeState, notes: Notes, path: str, free_form: bool = True) -> Shaped | Missing:
    """A JSON field shaped at `limit`. Not JSON: dropped as not_json; partly not JSON: kept, and
    marked. `free_form`: its keys come from the app's data, so secret key names apply. A mapping
    with more keys than an event may enumerate is left out as too_large (Python only)."""
    lossy, too_large = state.lossy, state.too_large
    state.lossy = False
    state.too_large = False
    state.secret_key = state.secret_keys if free_form else None
    try:
        shaped = shape_json(value, limit, state)
        if shaped is NOT_JSON or state.lossy:
            notes.dropped[path] = "too_large" if state.too_large else "not_json"
        return MISSING if shaped is NOT_JSON else shaped
    finally:
        state.lossy = lossy or state.lossy
        state.too_large = too_large
        state.secret_key = None


def longer_than(value: str, limit: int) -> bool:
    """Whether a string has more than `limit` code points, reading at most twice that."""
    raw = text(value)
    if len(raw) <= limit:
        return False
    joined, whole = joined_prefix(raw, limit + 1)
    return not whole or len(joined) > limit


def json_bytes_over(value: str, limit: int) -> bool:
    """Whether a string takes more than `limit` UTF-8 bytes written as JSON, without its quotes, as
    the event's size counts them: an escaped character, a lone surrogate among them, counts as its
    escape. One of more than `limit` code points is, unread: each is at least one byte."""
    raw = text(value)
    return len(raw) > limit or len(string(raw).encode("utf-8")) - 2 > limit


def shape_text(value: object, limit: int, state: ShapeState, notes: Notes, path: str) -> Shaped | Missing:
    """A field that must be a string, shaped at `limit`."""
    if value is None or value is MISSING:
        return MISSING
    if not isinstance(value, str):
        notes.dropped[path] = "wrong_type"
        return MISSING
    return shape_string(value, limit, state)


def id_value(value: object) -> Valid[str] | Literal["empty", "invalid"] | None:
    """An ID: absent, a valid string, or invalid (the event is then dropped: sent without it, it could
    slip past a deletion block). `''` counts as absent. Its size is the ingestion's: UTF-8 bytes as
    TextEncoder counts them, a lone surrogate being 3."""
    if value is None or value is MISSING:
        return None
    if not isinstance(value, str):
        return "invalid"
    raw = text(value)
    if raw == "":
        return "empty"
    # Each of Python's code points is at least one UTF-8 byte of the joined string.
    if len(raw) > MAX_ID_BYTES:
        return "invalid"
    joined = join(raw)
    if "\x00" in joined or len(joined.encode("utf-8", "surrogatepass")) > MAX_ID_BYTES:
        return "invalid"
    return Valid(joined)


#: The IDs an app gives, where a `uuid.UUID` stands for its string. `toolName` is a name, not one.
APP_ID_FIELDS: Final = ("orgId", "userId", "conversationId", "turnId", "callId", "apiKeyId")


def app_id(value: object) -> object:
    """A `uuid.UUID` given as an app's ID is its canonical string, lowercase with hyphens, so the
    event is the one that string makes. Anything else is left as it is, for the ID checks."""
    try:
        if isinstance(value, uuid.UUID):
            return uuid.UUID.__str__(value)
    except Exception:
        pass
    return value


def app_ids(fields: dict[str, Any]) -> dict[str, Any]:
    """`fields`, read from the app, with each UUID among its IDs as its string."""
    for key in APP_ID_FIELDS:
        if key in fields:
            fields[key] = app_id(fields[key])
    return fields


def read_ids(source: Mapping[str, Any], notes: Notes, prefix: str = "") -> tuple[Ids, list[str]]:
    """Reads the IDs; the list names those that would drop the event."""
    ids: Ids = {}
    invalid: list[str] = []
    for key in ID_FIELDS:
        checked = id_value(source.get(key, MISSING))
        if checked == "invalid":
            invalid.append(f"{prefix}{key}")
        elif checked == "empty":
            notes.dropped[f"{prefix}{key}"] = "empty"
        elif isinstance(checked, Valid):
            ids[key] = checked.value
    return ids, invalid


def turn_key(org_id: str | None, conversation_id: str, turn_id: str) -> str:
    """`k/` and the SHA-256 of `[orgId, conversationId, turnId]` as compact JSON, a missing org as
    null: the same for a turn whichever SDK sends it, and different for two orgs' turns."""
    written = dumps([None if org_id is None else join(org_id), join(conversation_id), join(turn_id)])
    return "k/" + hashlib.sha256(written.encode("utf-8")).hexdigest()


def turn_event_id(ids: Mapping[str, str], turn_id: str | None) -> str:
    """A turn's event ID: its `k/` key with a conversation and a turnId, or else random."""
    conversation = ids.get("conversationId")
    if conversation is not None and turn_id is not None:
        return turn_key(ids.get("orgId"), conversation, turn_id)
    return str(uuid.uuid4())


def turn_id_value(value: object) -> Check[str]:
    if value is None or value is MISSING:
        return None
    if isinstance(value, str):
        raw = text(value)
        if raw == "":
            return Reason("empty")
        return Reason("too_large") if json_bytes_over(raw, MAX_TURN_ID_BYTES) else Valid(join(raw))
    # Python's True isn't 1 here: a boolean is no number to JavaScript.
    held = js_number(value)
    if held is not None:
        return Valid(number(held)) if is_safe_integer(held) else Reason("unsafe_integer")
    return Reason("wrong_type")


def apply(check: Check[T], path: str, notes: Notes) -> T | Missing:
    """The checked value, or MISSING with its reason noted."""
    if check is None:
        return MISSING
    if isinstance(check, Valid):
        return check.value
    notes.dropped[path] = check.reason
    return MISSING


def shape_turn_fields(source: Mapping[str, Any], state: ShapeState, notes: Notes, prefix: str = "") -> ShapedTurn:
    """Reads a turn's own fields (a `turn` event's, or a call's `agent`) into shaped form."""
    body: ShapedTurn = {}

    def at(path: str) -> str:
        return f"{prefix}{path}"

    # A call's agent part is one field for `truncated`; a turn's fields are each their own.
    def field_(name: str, run: Callable[[], T]) -> T:
        return shape_field(state, notes, prefix[:-1] if prefix else name, run)

    limit = LIMITS.agent

    turn_id = apply(turn_id_value(source.get("turnId", MISSING)), at("turnId"), notes)
    if turn_id is not MISSING:
        body["turnId"] = turn_id
    status_ = apply(status(source.get("status", MISSING)), at("status"), notes)
    if status_ is not MISSING:
        body["status"] = status_
    question = field_("question", lambda: shape_text(source.get("question", MISSING), limit, state, notes, at("question")))
    if question is not MISSING:
        body["question"] = question
    answer = field_("answer", lambda: shape_text(source.get("answer", MISSING), limit, state, notes, at("answer")))
    if answer is not MISSING:
        body["answer"] = answer
    error = field_("error", lambda: _error_value(source.get("error", MISSING), state, notes, at("error")))
    if error is not MISSING:
        body["error"] = error
    calls = field_("toolCalls", lambda: _tool_calls(source.get("toolCalls", MISSING), state, notes, at("toolCalls")))
    if calls is not MISSING:
        body["toolCalls"] = calls
    usage = _usage_value(source.get("usage", MISSING), notes, at("usage"))
    if usage is not MISSING:
        body["usage"] = usage
    for key in ("latency", "verification"):
        item = source.get(key, MISSING)
        if item is None or item is MISSING:
            continue
        shaped = field_(key, lambda: shape_any(item, limit, state, notes, at(key), True))
        if shaped is not MISSING:
            body[key] = shaped
    client_ = apply(client(source.get("client", MISSING), notes, at("client")), at("client"), notes)
    if client_ is not MISSING:
        body["client"] = client_
    api_key_id = apply(short(source.get("apiKeyId", MISSING)), at("apiKeyId"), notes)
    if api_key_id is not MISSING:
        body["apiKeyId"] = api_key_id
    surface = apply(short(source.get("surface", MISSING)), at("surface"), notes)
    if surface is not MISSING:
        body["surface"] = surface
    return body


def finish_turn_fields(body: ShapedTurn, redactor: Redactor, all_: bool) -> dict[str, Any]:
    """Shaped turn fields to JSON. `all_`: redact everything; otherwise only what was cut."""
    text_scope = replace(TEXT_FIELD, all=all_)

    def text_(value: Shaped) -> Json:
        return redactor.finalize(value, text_scope)

    out: dict[str, Any] = {}
    if "question" in body:
        out["question"] = text_(body["question"])
    if "answer" in body:
        out["answer"] = text_(body["answer"])
    if "turnId" in body:
        out["turnId"] = body["turnId"]
    if "status" in body:
        out["status"] = body["status"]
    if "error" in body:
        error: dict[str, Any] = {"message": text_(body["error"]["message"])}
        if "type" in body["error"]:
            kind = body["error"]["type"]
            error["type"] = redactor.text(kind) if all_ else kind
        out["error"] = error
    if "toolCalls" in body:
        calls: list[dict[str, Any]] = []
        for call in body["toolCalls"]:
            finished: dict[str, Any] = {key: item for key, item in call.items() if key not in ("input", "errorMessage")}
            if "input" in call:
                finished["input"] = redactor.finalize(call["input"], replace(JSON_FIELD, all=all_))
            if "errorMessage" in call:
                finished["errorMessage"] = text_(call["errorMessage"])
            calls.append(finished)
        out["toolCalls"] = calls
    if "usage" in body:
        out["usage"] = body["usage"]
    if "latency" in body:
        out["latency"] = redactor.finalize(body["latency"], replace(FREE_FORM, all=all_))
    if "verification" in body:
        out["verification"] = redactor.finalize(body["verification"], replace(FREE_FORM, all=all_))
    for key in ("client", "apiKeyId", "surface"):
        if key in body:
            out[key] = body[key]
    return out


def reserve_turn_fields(body: ShapedTurn, redactor: Redactor) -> None:
    """Records placeholders already in shaped turn fields: in every one, redacted or not (a tool
    call's name, `surface`, `client`), as 0.1's rule says a placeholder anywhere in the event is
    skipped, and as `prune` finds them with a hook pending."""
    redactor.reserve(body)


@dataclass
class ShapedTurnEvent:
    ids: Ids
    fields: ShapedTurn
    notes: Notes
    #: Unknown top-level fields: known aliases by name, everything else as "(other)".
    ignored_fields: dict[str, int]
    metadata: Metadata | Missing = MISSING
    ok: Literal[True] = True


TurnResult = Union[ShapedTurnEvent, BuiltError]


def shape_turn(raw: object, state: ShapeState, *, metadata_budget: int, identity: Mapping[str, Any] | None = None) -> TurnResult:
    """Turns `capture()` input (or `before_send`'s output for a turn, with `identity`) into a shaped
    turn. Never raises on bad shapes. Only an invalid ID or oversized metadata drops it. What
    `capture()` is given must be a mapping."""
    notes = new_notes()
    is_mapping = isinstance(raw, Mapping)
    if not is_mapping and identity is None:
        return BuiltError("invalid_input", [])
    # A field whose read raises drops the event, as a getter's throw does in TypeScript: sent
    # without it, an ID perhaps, it could slip past a deletion block.
    source: dict[str, Any] = app_ids(read_fields(raw, INPUT_FIELDS, strict=True)) if isinstance(raw, Mapping) else {}
    ids: Ids
    if identity is not None:
        ids = {key: value for key, value in identity.items() if key != "turnId"}
        source["turnId"] = identity.get("turnId", MISSING)
    else:
        ids, invalid = read_ids(source, notes)
        if invalid:
            return BuiltError("invalid_id", invalid)
    fields = shape_turn_fields(source, state, notes)
    metadata = metadata_value(source.get("metadata", MISSING), notes, Budget(metadata_budget))
    if metadata == "too_large":
        return BuiltError("too_large", ["metadata"])

    ignored: dict[str, int] = {}
    if isinstance(raw, Mapping) and identity is None:
        _count_unknown(partial(object_keys, raw), lambda key: key in _INPUT_FIELD_SET or key in SDK_FIELDS, ignored, state)
    return ShapedTurnEvent(ids=ids, fields=fields, notes=notes, ignored_fields=ignored, metadata=metadata)


def _count_unknown(listing: Callable[[int], Keys | None], known: Callable[[str], bool], counts: dict[str, int], state: ShapeState) -> None:
    """Counts the first 1,000 keys, in JavaScript's order, that aren't fields: known aliases by name.
    `listing` gives them within what's left of the event's key allowance, which is charged them
    each time, enumerated or not; past it, nothing is counted."""
    try:
        keys = listing(state.keys)
    except Exception:
        return
    if keys is None:
        state.keys = 0
        return
    state.keys -= keys.total
    seen = 0
    for raw in keys.names:
        seen += 1
        if seen > MAX_IGNORED_FIELDS:
            return
        key = text(raw)
        if not known(key):
            _count(counts, key)
    for _ in range(keys.others):
        seen += 1
        if seen > MAX_IGNORED_FIELDS:
            return
        _count(counts, "(other)")


def _count(counts: dict[str, int], key: str) -> None:
    name = key if key in KNOWN_ALIASES else "(other)"
    counts[name] = counts.get(name, 0) + 1


def short(value: object) -> Check[str]:
    """A short field kept as it is: at most 16,000 code points."""
    if value is None or value is MISSING:
        return None
    if not isinstance(value, str):
        return Reason("wrong_type")
    return Reason("too_large") if longer_than(value, MAX_SHORT_CHARS) else Valid(join(value))


def boolean(value: object) -> Check[bool]:
    if value is None or value is MISSING:
        return None
    # By type: bool can't be subclassed, so only a proxy passes isinstance for one.
    return Valid(value) if type(value) is bool else Reason("wrong_type")


def number_value(value: object) -> Check[Union[int, float]]:
    """normalize.ts's `number`: a finite number, as JavaScript holds it."""
    if value is None or value is MISSING:
        return None
    held = js_number(value)
    return Valid(held) if held is not None and is_finite(held) else Reason("wrong_type")


def status(value: object) -> Check[Literal["completed", "error"]]:
    if value is None or value is MISSING:
        return None
    if isinstance(value, str):
        if value == "completed":
            return Valid("completed")
        if value == "error":
            return Valid("error")
    return Reason("wrong_type")


def _error_value(value: object, state: ShapeState, notes: Notes, path: str) -> ShapedError | Missing:
    if value is None or value is MISSING:
        return MISSING
    if isinstance(value, str):
        return {"message": shape_string(value, LIMITS.agent, state)}
    if isinstance(value, BaseException):
        # Python's counterpart of an Error: its message, and its class's name as JavaScript's `name`.
        try:
            source: dict[str, Any] = {"message": str(value), "name": type(value).__name__}
        except Exception:
            notes.dropped[path] = "wrong_type"
            return MISSING
    else:
        found = as_object(value)
        if found is None:
            notes.dropped[path] = "wrong_type"
            return MISSING
        source = read_fields(found, ("message", "type", "name"))
    message = source.get("message")
    if not isinstance(message, str):
        notes.dropped[path] = "wrong_type"
        return MISSING
    out: ShapedError = {"message": shape_string(message, LIMITS.agent, state)}
    given = source.get("type", MISSING)
    kind = short(given if isinstance(given, str) else source.get("name", MISSING))
    if isinstance(kind, Valid):
        if kind.value != "":
            out["type"] = kind.value
    elif isinstance(kind, Reason) and kind.reason == "too_large":
        # Too long to keep, as other short fields are; one of another type is ignored, as in 0.1.
        notes.dropped[f"{path}.type"] = "too_large"
    return out


def client(value: object, notes: Notes | None = None, path: str = "client") -> Check[Union[str, dict[str, str]]]:
    """0.1's client: a non-empty string, or `{ name, version? }`."""
    if value is None or value is MISSING:
        return None
    if isinstance(value, str) and value != "":
        return Reason("too_large") if longer_than(value, MAX_SHORT_CHARS) else Valid(join(value))
    if as_object(value) is None:
        return Reason("wrong_type")
    named_ = named(value, notes, path)
    if isinstance(named_, Valid):
        return Valid(named_.value)
    return named_


def named(value: object, notes: Notes | None = None, path: str = "") -> Check[dict[str, str]]:
    """`{ name, version? }`, as clients and servers name themselves. A version too long to keep is
    left out, and with `notes`, recorded as too_large at `path.version`; one of another type is
    ignored."""
    found = as_object(value)
    if found is None:
        return Reason("wrong_type")
    fields = read_fields(found, ("name", "version"))
    name = fields.get("name")
    if not isinstance(name, str) or name == "":
        return Reason("wrong_type")
    if longer_than(name, MAX_SHORT_CHARS):
        return Reason("too_large")
    out = {"name": join(name)}
    version = fields.get("version")
    if isinstance(version, str):
        if not longer_than(version, MAX_SHORT_CHARS):
            out["version"] = join(version)
        elif notes is not None:
            notes.dropped[f"{path}.version"] = "too_large"
    return Valid(out)


def _tool_calls(value: object, state: ShapeState, notes: Notes, path: str) -> list[ShapedToolCall] | Missing:
    if value is None or value is MISSING:
        return MISSING
    if not isinstance(value, (list, tuple)):
        notes.dropped[path] = "wrong_type"
        return MISSING
    out: list[ShapedToolCall] = []
    allowance = Budget(RESULT_MEASURE_LIMIT)
    # A result's mapping the tool calls share is enumerated once, as in TypeScript. Its own, as its
    # keys are listed within the measuring allowance, but a class's fields are worked out once for
    # the pass: the same as the tool calls'.
    listed = Listed(state.listed)
    if len(value) > MAX_ENTRIES:
        state.shortened = True
    for index in range(min(len(value), MAX_ENTRIES)):
        # One per item, as for any list: past the budget, no more tool calls are taken.
        if not spend(state, 1):
            break
        at = f"{path}[{index}]"
        given = value[index]
        # A tool call the list repeats is enumerated once, a model's fields worked out once: its
        # values are read again, and its keys charged again, for each.
        item = state.listed.source(given)
        if item is None:
            notes.dropped[at] = "wrong_type"
            continue
        # Counted before validation: field names an app uses instead of ours show up in the dry run.
        _count_unknown(partial(state.listed.keys, given), lambda key: key in _TOOL_CALL_FIELD_SET, notes.ignored_tool_call_fields, state)
        fields = read_fields(item, (*TOOL_CALL_FIELDS, "result"))
        name = short(fields.get("name", MISSING))
        if not isinstance(name, Valid) or name.value == "":
            # A call needs its name: one too long to keep drops it as too_large.
            notes.dropped[at] = "too_large" if isinstance(name, Reason) and name.reason == "too_large" else "missing_name"
            continue
        call: ShapedToolCall = {"name": name.value}
        if "input" in fields:
            # An explicit null input is kept: the tool was called with none.
            given = fields["input"]
            shaped = None if given is None else shape_any(given, LIMITS.input, state, notes, f"{at}.input", True)
            if shaped is not MISSING:
                call["input"] = shaped
        result_chars = apply(number_value(fields.get("resultChars", MISSING)), f"{at}.resultChars", notes)
        if result_chars is not MISSING:
            call["resultChars"] = result_chars
        is_error = apply(boolean(fields.get("resultIsError", MISSING)), f"{at}.resultIsError", notes)
        if is_error is not MISSING:
            call["resultIsError"] = is_error
        message = shape_text(fields.get("errorMessage", MISSING), LIMITS.agent, state, notes, f"{at}.errorMessage")
        if message is not MISSING:
            call["errorMessage"] = message
        duration = apply(number_value(fields.get("durationMs", MISSING)), f"{at}.durationMs", notes)
        if duration is not MISSING:
            call["durationMs"] = duration
        # Results never leave: keep only their length, in code points for a string.
        result = fields.get("result")
        if "resultChars" not in call and result is not None:
            length = _result_length(result, allowance) if isinstance(result, str) else json_length(result, allowance, listed)
            if isinstance(length, int):
                call["resultChars"] = length
            else:
                notes.dropped[f"{at}.resultChars"] = length
        out.append(call)
    # Every item invalid means unknown, not "no tools were called".
    return MISSING if len(out) == 0 and len(value) > 0 else out


def _result_length(value: str, allowance: Budget) -> int | Literal["too_large"]:
    count = max(0, allowance.remaining) + 1
    joined, whole = joined_prefix(value, count)
    end, passed = advance(joined, 0, count)
    allowance.remaining -= passed
    return passed if end == len(joined) and whole and allowance.remaining >= 0 else "too_large"


def _usage_value(value: object, notes: Notes, path: str) -> dict[str, Union[int, float]] | Missing:
    if value is None or value is MISSING:
        return MISSING
    found = as_object(value)
    if found is None:
        notes.dropped[path] = "wrong_type"
        return MISSING
    fields = read_fields(found, ("inputTokens", "outputTokens", "costUsd"))
    usage: dict[str, Union[int, float]] = {}
    for key in ("inputTokens", "outputTokens", "costUsd"):
        held = apply(number_value(fields.get(key, MISSING)), f"{path}.{key}", notes)
        if held is not MISSING:
            usage[key] = held
    return usage if usage else MISSING


def metadata_value(value: object, notes: Notes, budget: Budget) -> Metadata | Missing | Literal["too_large"]:
    """0.1's metadata: string, number, boolean and None values only, never shortened. Each entry
    costs one, and a kept one its key's and string's length in UTF-16 units, as in TypeScript."""
    if value is None or value is MISSING:
        return MISSING
    found = as_object(value)
    if found is None:
        notes.dropped["metadata"] = "wrong_type"
        return MISSING
    try:
        # Every entry costs at least one: a mapping with more than what's left is too large.
        keys = object_keys(found, max(0, budget.remaining))
    except Exception:
        notes.dropped["metadata"] = "not_json"
        return MISSING
    if keys is None:
        return "too_large"
    out: Metadata = {}
    lossy = keys.others > 0
    for raw in keys.names:
        try:
            item = found.get(raw, MISSING)
        except Exception:
            item = NOT_JSON
        budget.remaining -= 1
        if budget.remaining < 0:
            return "too_large"
        if item is MISSING:
            continue
        key_raw = text(raw)
        primitive, held = _primitive(item)
        if primitive and key_raw != "__proto__":
            size = len(key_raw) + (len(held) if isinstance(held, str) else 1)
            # At least their UTF-16 lengths: what can't fit isn't joined.
            if size > budget.remaining:
                return "too_large"
            key = join(key_raw)
            if isinstance(held, str):
                held = join(held)
            budget.remaining -= utf16_length(key) + (utf16_length(held) if isinstance(held, str) else 1)
            if budget.remaining < 0:
                return "too_large"
            out[key] = held
        else:
            lossy = True
    if lossy:
        notes.dropped["metadata"] = "not_json"
    return out if out else MISSING


def _primitive(value: object) -> tuple[bool, Union[str, int, float, bool, None]]:
    try:
        # By type: bool can't be subclassed, so only a proxy passes isinstance for one.
        if value is None or type(value) is bool:
            return True, value
        if isinstance(value, str):
            return True, text(value)
        held = js_number(value)
    except Exception:
        # A value whose own type check raises (a lazy object whose factory fails) isn't kept.
        return False, None
    if held is not None and is_finite(held):
        return True, held
    return False, None


def metadata_size(metadata: Metadata) -> int:
    """Metadata's size as its budget counts it: one per entry, its key, and a string's length."""
    size = 0
    for key, value in metadata.items():
        size += 1 + utf16_length(key) + (utf16_length(value) if isinstance(value, str) else 1)
    return size


def finish_metadata(metadata: Metadata, redactor: Redactor, all_: bool) -> Json:
    """Metadata to JSON: secret key names apply even before the hook; with `all_`, everything is redacted."""
    return redactor.finalize(cast(Shaped, metadata), replace(FREE_FORM, all=all_))


def merge_objects(*parts: Mapping[str, T]) -> dict[str, T]:
    """`{ ...a, ...b }` in JavaScript: later parts win per key, and keys are in JavaScript's order."""
    merged: dict[str, T] = {}
    for part in parts:
        merged.update(part)
    return {key: merged[key] for key in js_order(list(merged))}


__all__ = [
    "APP_ID_FIELDS",
    "ID_FIELDS",
    "INPUT_FIELDS",
    "KNOWN_ALIASES",
    "MAX_ID_BYTES",
    "MAX_SHORT_CHARS",
    "MAX_TURN_ID_BYTES",
    "SDK_FIELDS",
    "TOOL_CALL_FIELDS",
    "TURN_FIELDS",
    "BuiltError",
    "Check",
    "Ids",
    "Metadata",
    "Notes",
    "Reason",
    "ShapedTurn",
    "ShapedTurnEvent",
    "TurnResult",
    "Valid",
    "app_id",
    "app_ids",
    "apply",
    "boolean",
    "client",
    "finish_metadata",
    "finish_turn_fields",
    "id_value",
    "json_bytes_over",
    "longer_than",
    "merge_objects",
    "metadata_size",
    "metadata_value",
    "named",
    "new_notes",
    "number_value",
    "read_ids",
    "reserve_turn_fields",
    "shape_any",
    "shape_field",
    "shape_text",
    "shape_turn",
    "shape_turn_fields",
    "short",
    "status",
    "turn_event_id",
    "turn_id_value",
    "turn_key",
]
