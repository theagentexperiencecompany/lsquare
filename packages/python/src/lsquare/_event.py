"""Building events, as event.ts does: a turn from `capture()`, a call from the wrapper, and either
again from `before_send`'s output.

`hook=True` means a `before_send` is pending: only cut strings and what's under secret key names are
redacted yet, and the event is redacted whole after the hook (`after_hook`).
"""

from __future__ import annotations

import enum
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final, Literal, TypedDict, TypeVar, Union

from ._jsjson import Budget, dumps, read_fields, size_within
from ._normalize import (
    BuiltError,
    Metadata,
    Notes,
    Reason,
    ShapedTurn,
    Valid,
    app_ids,
    apply,
    finish_metadata,
    finish_turn_fields,
    id_value,
    merge_objects,
    metadata_size,
    metadata_value,
    named,
    new_notes,
    reserve_turn_fields,
    shape_any,
    shape_field,
    shape_text,
    shape_turn,
    shape_turn_fields,
    short,
    turn_event_id,
)
from ._redact import JSON_FIELD, TEXT_FIELD, Redactor
from ._shape import (
    LIMITS,
    MAX_ENTRIES,
    MAX_KEYS,
    SCAN_BUDGET,
    Shaped,
    ShapeState,
    decoded_size,
    js_length,
    new_state,
    secret_string,
    shape_string,
    spend,
)
from ._types import ErrorCategory, Json, PatternName, Round, SdkInfo, SessionSource, Status
from ._values import MISSING, Missing, as_object, is_finite, is_safe_integer, join, js_number, pydantic_url, stand_in, text, utf16_length

T = TypeVar("T")

#: Parts left out, in order, until an event fits: biggest and least needed first.
TURN_ORDER: Final = (("toolCalls",), ("answer",), ("question",), ("verification",), ("latency",))
CALL_ORDER: Final = (("result",), ("input",), ("agent", "toolCalls"), ("agent",))


@dataclass
class Context:
    """What every event needs, besides its content."""

    sdk: SdkInfo
    patterns: Sequence[PatternName]
    redact_keys: Sequence[str]
    max_event_bytes: int


class CallIds(TypedDict, total=False):
    """Who a call was for: from `identify`, and from `capture()` inside the call, which wins."""

    orgId: str
    userId: str
    conversationId: str


class Boxed(TypedDict):
    value: Any


class _RecordErrorRequired(TypedDict):
    category: ErrorCategory


class RecordError(_RecordErrorRequired, total=False):
    message: Any
    type: Any
    code: Any


class RecordTask(TypedDict):
    id: Any
    status: Any


class _CallRecordRequired(TypedDict):
    callId: str
    capturedAt: str
    toolName: str
    status: Status
    durationMs: float
    ids: CallIds
    #: `identify` failed: sent as limited diagnostics.
    diagnostics: bool


class CallRecord(_CallRecordRequired, total=False):
    """A call, as the wrapper saw it, with event.ts's camelCase keys, so a shared case's `call` maps
    one to one. Values from the app or the client are read once, checked and shaped here: the
    wrapper passes them on as it found them."""

    turnRef: str
    server: Any
    client: Any
    protocolVersion: Any
    sessionId: str
    sessionSource: SessionSource
    #: The arguments, when inputs are recorded for this tool.
    input: Boxed
    #: MCP's result, when results are recorded for this tool.
    result: Boxed
    error: RecordError
    round: Round
    #: A task the call created: its ID and status come from the server, so they're shaped and redacted.
    task: RecordTask
    intent: Any
    extra: Literal["missing_tools"]
    #: What `capture()` sent from inside the call: from the client, a `Captured`, read and shaped then.
    agent: Mapping[str, Any]
    metadata: Any


RECORD_FIELDS: Final = (
    "callId", "capturedAt", "toolName", "status", "durationMs", "ids", "turnRef", "server", "client", "protocolVersion",
    "sessionId", "sessionSource", "input", "result", "error", "round", "task", "intent", "extra", "agent", "metadata", "diagnostics",
)  # fmt: skip
_TURN_REF: Final = re.compile(r"k/[0-9a-f]{64}", re.ASCII)
CATEGORIES: Final = frozenset(["thrown", "tool_error", "protocol", "timeout"])


@dataclass
class Draft:
    """An event on its way: built, maybe waiting for `before_send`, then finished."""

    kind: Literal["turn", "tool_call"]
    #: The event as `before_send` sees it, or as sent when there is none.
    event: dict[str, Any]
    #: Keeps numbering across `before_send`.
    redactor: Redactor
    #: Fields the hook can't change, restored from here.
    identity: dict[str, Any]
    category: ErrorCategory | None
    #: The agent part's turnId, restored after the hook: the call's `turnRef` was worked out from it.
    agent_turn_id: str | None
    diagnostics: bool
    notes: Notes
    ignored_fields: dict[str, int]


@dataclass
class BuiltOk:
    draft: Draft
    ok: Literal[True] = True


Built = Union[BuiltOk, BuiltError]


def build_turn(input: object, captured_at: str, context: Context, hook: bool) -> Built:
    """A turn from `capture()`. With a hook pending, only cut strings and what's under secret key names
    are redacted yet."""
    redactor = Redactor(context.patterns, context.redact_keys)
    state = new_state(secret_keys=redactor.secret_key_matcher())
    shaped = shape_turn(input, state, metadata_budget=context.max_event_bytes)
    if isinstance(shaped, BuiltError):
        return shaped
    ids, fields, metadata, notes = shaped.ids, shaped.fields, shaped.metadata, shaped.notes
    # As in 0.1, a turn without a status completed.
    fields.setdefault("status", "completed")
    reserve_turn_fields(fields, redactor)
    redactor.reserve(None if metadata is MISSING else metadata)
    head: dict[str, Any] = {
        "eventId": turn_event_id(ids, fields.get("turnId")),
        "schemaVersion": 2,
        "kind": "turn",
        "capturedAt": captured_at,
        "source": "live",
        "sdk": context.sdk,
    }
    identity: dict[str, Any] = {**head, **ids}
    if "turnId" in fields:
        identity["turnId"] = fields["turnId"]
    # The IDs too, which are never redacted: a placeholder anywhere in the event is skipped.
    redactor.reserve(identity)
    event = _turn_event(head, ids, fields, metadata, redactor, not hook)
    draft = Draft(
        kind="turn",
        event=event,
        redactor=redactor,
        identity=identity,
        category=None,
        agent_turn_id=None,
        diagnostics=False,
        notes=notes,
        ignored_fields=shaped.ignored_fields,
    )
    return _pending(_finish(draft, not hook, context), hook)


def retained_bytes(draft: Draft) -> int:
    """What a draft keeps beyond its event, for the queue's limits: its redaction state, and the agent
    part's turnId, which fitting may have left out of the event."""
    turn_id = draft.agent_turn_id
    return draft.redactor.retained_bytes() + (utf16_length(turn_id) * 2 if turn_id is not None else 0)


def _pending(built: Built, hook: bool) -> Built:
    """An event waiting for its hook keeps only the redaction state of what it still holds."""
    if isinstance(built, BuiltOk) and hook:
        built.draft.redactor.prune(built.draft.event)
    return built


def build_call(record: CallRecord | Mapping[str, Any], context: Context, hook: bool) -> Built:
    """A call from the wrapper."""
    # Every value read once: some come from the app (identify, capture()).
    rec = read_fields(record, RECORD_FIELDS) if isinstance(record, Mapping) else {}
    notes = new_notes()
    ids, invalid = _check_call(rec)
    if invalid:
        return BuiltError("invalid_id", invalid)
    # The error read once, its category included, for the event and for the draft.
    found = as_object(rec.get("error", MISSING))
    error = read_fields(found, ("message", "type", "code", "category")) if found is not None else None
    if "error" in rec and error is None:
        notes.dropped["error"] = "wrong_type"
    category = _category(error.get("category") if error is not None else None)
    redactor = Redactor(context.patterns, context.redact_keys)
    state = new_state(secret_keys=redactor.secret_key_matcher())
    shaped = _shape_call(rec, error, category, state, notes)
    if shaped == "too_large":
        return BuiltError("too_large", ["metadata"])
    identity = _call_identity(rec, ids, context, notes)
    _reserve_call(shaped, identity, redactor)
    event = _call_event(identity, shaped, redactor, not hook)
    agent = shaped.get("agent")
    draft = Draft(
        kind="tool_call",
        event=event,
        redactor=redactor,
        identity=identity,
        category=category,
        agent_turn_id=agent.get("turnId") if agent is not None else None,
        diagnostics=rec.get("diagnostics") is True,
        notes=notes,
        ignored_fields={},
    )
    return _pending(_finish(draft, not hook, context), hook)


@dataclass(frozen=True)
class ShapedAgent:
    """A call's agent part as `capture()` inside the call read it."""

    body: ShapedTurn
    notes: Notes
    #: Merged with identify's when the call's event is built, and what reading it noted.
    metadata: Metadata | Missing | Literal["too_large"]
    metadata_notes: Notes
    #: What shaping it read, of the scan budget and of the keys allowed: charged to the call's.
    scanned: int
    keys: int


class _Failed(enum.Enum):
    FAILED = 0


#: `capture()` inside the call couldn't read its data: the call's event is dropped when it's built,
#: as one that couldn't be built, since what the app holds then may not be what it gave.
CAPTURE_FAILED: Final = _Failed.FAILED
#: What the wrapper reads of a `capture()` inside the call, as it was given: the IDs that group the
#: call and make its turnRef.
CAPTURED_IDS: Final = ("orgId", "userId", "conversationId", "turnId")


class Captured(dict):  # type: ignore[type-arg]
    """A `capture()` from inside a call: its IDs, as the wrapper reads them, and its agent part as it
    was read there and then (`shape_agent`), or CAPTURE_FAILED. None while it's still being read.
    Nothing else of what the app gave is kept."""

    __slots__ = ("snapshot",)

    def __init__(self, fields: Mapping[str, Any]) -> None:
        super().__init__((key, fields[key]) for key in CAPTURED_IDS if key in fields)
        self.snapshot: ShapedAgent | _Failed | None = None


class CaptureUnread(Exception):
    """The call's agent part couldn't be read when `capture()` was called: the call is dropped."""


def shape_agent(fields: Mapping[str, Any], context: Context) -> ShapedAgent:
    """A call's agent part, shaped when `capture()` is called inside the call: capture() reads its
    data before it returns (0.1's rule), so what the app's objects hold then is what is sent,
    whatever the app does with them before the call's event is built. What it reads is charged to
    the call's scan budget in the agent's place (`_charge`). `fields` are capture()'s, read once."""
    redactor = Redactor(context.patterns, context.redact_keys)
    state = new_state(secret_keys=redactor.secret_key_matcher())
    notes = new_notes()
    body = shape_turn_fields(app_ids(read_fields(fields, AGENT_FIELDS)), state, notes, "agent.")
    # As for a turn of its own: one without a status completed.
    body.setdefault("status", "completed")
    metadata_notes = new_notes()
    metadata = metadata_value(read_fields(fields, ("metadata",)).get("metadata", MISSING), metadata_notes, Budget(METADATA_BUDGET))
    return ShapedAgent(body, notes, metadata, metadata_notes, SCAN_BUDGET - state.scan.remaining, MAX_KEYS - state.keys)


def _charge(state: ShapeState, early: ShapedAgent) -> None:
    """The scan budget and keys an agent part read when it was captured, charged in its place: with
    more left than it read, what's left for the result is what shaping it now would leave, and so
    was the agent part. With less, the budget ran out there: it keeps what it read, the result none."""
    state.scan.remaining = state.scan.remaining - early.scanned if state.scan.remaining > early.scanned else 0
    state.keys = max(0, state.keys - early.keys)


def _add_notes(into: Notes, source: Notes) -> None:
    """Notes taken earlier, in their place among the event's."""
    into.dropped.update(source.dropped)
    for name in source.truncated:
        into.truncated[name] = None
    for key, count in source.ignored_tool_call_fields.items():
        into.ignored_tool_call_fields[key] = into.ignored_tool_call_fields.get(key, 0) + count


def _category(value: object) -> ErrorCategory | None:
    if isinstance(value, str) and value in CATEGORIES:
        return text(value)  # type: ignore[return-value]
    return None


def _check_call(rec: Mapping[str, Any]) -> tuple[dict[str, str], list[str]]:
    """The call's IDs and outline, checked as the ingestion checks them."""
    invalid: list[str] = []
    ids: dict[str, str] = {}
    for key in ("callId", "toolName"):
        if not isinstance(id_value(rec.get(key, MISSING)), Valid):
            invalid.append(key)
    found = as_object(rec.get("ids", MISSING))
    given = read_fields(found, ("orgId", "userId", "conversationId")) if found is not None else {}
    for key in ("orgId", "userId", "conversationId"):
        checked = id_value(given.get(key, MISSING))
        if checked == "invalid":
            invalid.append(f"ids.{key}")
        elif isinstance(checked, Valid):
            ids[key] = checked.value
    if "sessionId" in rec and not isinstance(id_value(rec["sessionId"]), Valid):
        invalid.append("sessionId")
    if "turnRef" in rec:
        ref = rec["turnRef"]
        if not (isinstance(ref, str) and _TURN_REF.fullmatch(ref) is not None):
            invalid.append("turnRef")
    return ids, invalid


def after_hook(out: Mapping[str, Any], draft: Draft, context: Context) -> Built:
    """`before_send`'s output, checked like the app's input: identity restored, shaped again (strings
    already shortened keep their markers), everything redacted, fitted, and diagnostics reduced.
    Values removed from it are recorded under `beforeSend.`."""
    redactor = draft.redactor
    state = new_state(after_hook=True, cuts=redactor.cuts, secret_keys=redactor.secret_key_matcher())
    hook_notes = new_notes()
    event: dict[str, Any]
    if draft.kind == "turn":
        head_keys = ("eventId", "schemaVersion", "kind", "capturedAt", "source", "sdk")
        identity = draft.identity
        ids = {key: value for key, value in identity.items() if key not in head_keys and key != "turnId"}
        given: dict[str, Any] = dict(ids)
        if identity.get("turnId") is not None:
            given["turnId"] = identity["turnId"]
        shaped = shape_turn(out, state, metadata_budget=context.max_event_bytes, identity=given)
        if isinstance(shaped, BuiltError):
            return shaped
        shaped.fields.setdefault("status", "completed")
        reserve_turn_fields(shaped.fields, redactor)
        redactor.reserve(None if shaped.metadata is MISSING else shaped.metadata)
        redactor.reserve(identity)
        _merge_notes(hook_notes, shaped.notes)
        head = {key: identity.get(key) for key in head_keys}
        event = _turn_event(head, ids, shaped.fields, shaped.metadata, redactor, True)
    else:
        reshaped = _reshape_call(out, draft, state, hook_notes)
        if reshaped == "too_large":
            return BuiltError("too_large", ["metadata"])
        identity = dict(draft.identity)
        # The hook may remove or change what the calling app and the server called themselves.
        fixed = reshaped["fixed"]
        for key in ("client", "server", "protocolVersion"):
            identity.pop(key, None)
            if key in fixed:
                identity[key] = fixed[key]
        _reserve_call(reshaped, identity, redactor)
        event = _call_event(identity, reshaped, redactor, True)
    notes = draft.notes
    for path, reason in hook_notes.dropped.items():
        notes.dropped[f"beforeSend.{path}"] = reason
    for name in hook_notes.truncated:
        notes.truncated[name] = None
    return _finish(replace(draft, event=event, notes=notes), True, context)


def _finish(draft: Draft, final: bool, context: Context) -> Built:
    """Adds the SDK's bookkeeping, fits the event, and once it's final, reduces diagnostics."""
    # Diagnostics keep only a few small fields, whatever the full event's size.
    if final and draft.diagnostics:
        draft.event = _diagnostics(draft)
        return BuiltOk(draft)
    event = draft.event
    notes, redactor = draft.notes, draft.redactor
    if final and redactor.counts:
        event["redactions"] = dict(redactor.counts)
    if notes.dropped:
        event["dropped"] = dict(notes.dropped)
    if notes.truncated:
        event["truncated"] = list(notes.truncated)
    fits = _fit(event, context.max_event_bytes, TURN_ORDER if draft.kind == "turn" else CALL_ORDER, notes.truncated)
    # A diagnostics event waiting for its hook is reduced after it, so its size doesn't count yet.
    if not fits and not draft.diagnostics:
        return BuiltError("too_large", [])
    return BuiltOk(draft)


def _fit(event: dict[str, Any], max_bytes: int, order: tuple[tuple[str, ...], ...], truncated: dict[str, None]) -> bool:
    """Leaves parts out, in order, until the event's compact JSON fits. False if it never does. An
    event over the limit is written only until it is known to be over it (`size_within`)."""
    fits = size_within(event, max_bytes) is not None
    for path in order:
        if fits:
            return True
        parent = event if len(path) == 1 else event.get(path[0])
        key = path[-1]
        if not isinstance(parent, dict) or key not in parent:
            continue
        del parent[key]
        truncated[".".join(path)] = None
        event["truncated"] = list(truncated)
        fits = size_within(event, max_bytes) is not None
    return fits


def _diagnostics(draft: Draft) -> dict[str, Any]:
    """What a limited-diagnostics event keeps: what every event needs, and the call's outline."""
    event = draft.event
    out: dict[str, Any] = {}
    if "eventId" in event:
        out["eventId"] = event["eventId"]
    out["kind"] = "tool_call"
    out["schemaVersion"] = 2
    if "capturedAt" in event:
        out["capturedAt"] = event["capturedAt"]
    out["source"] = "live"
    for key in ("sdk", "callId", "toolName", "status", "durationMs"):
        if key in event:
            out[key] = event[key]
    out["identity"] = "failed"
    if draft.category is not None:
        out["error"] = {"category": draft.category}
    return out


def byte_size(value: object) -> int:
    """UTF-8 bytes of the value's compact JSON, as `JSON.stringify` writes it."""
    return len(dumps(value).encode("utf-8"))


def _merge_notes(into: Notes, source: Notes) -> None:
    into.dropped.update(source.dropped)
    for name in source.truncated:
        into.truncated[name] = None


def _turn_event(
    head: Mapping[str, Any],
    ids: Mapping[str, Any],
    fields: ShapedTurn,
    metadata: Metadata | Missing,
    redactor: Redactor,
    all_: bool,
) -> dict[str, Any]:
    event: dict[str, Any] = {**head, **ids, **finish_turn_fields(fields, redactor, all_)}
    if metadata is not MISSING:
        event["metadata"] = finish_metadata(metadata, redactor, all_)
    return event


# Calls.

RESULT_KEYS: Final = ("content", "structuredContent", "isError")
BLOCK_KEYS: Final = ("type", "text", "data", "mimeType", "uri", "name", "resource", "bytes")


class ShapedBlock(TypedDict, total=False):
    type: str
    text: Shaped
    uri: Shaped
    name: Shaped
    mimeType: Shaped
    bytes: Union[int, float]


class ShapedResult(TypedDict, total=False):
    content: list[ShapedBlock]
    structuredContent: Shaped
    isError: bool


class ShapedCallError(TypedDict, total=False):
    message: Shaped
    type: str
    code: Union[int, float, str]
    category: ErrorCategory


class _ShapedCallRequired(TypedDict):
    #: Fixed fields that `before_send` may change: what the calling app and the server called themselves.
    fixed: dict[str, Json]


class ShapedCall(_ShapedCallRequired, total=False):
    task: dict[str, str]
    input: Shaped
    result: ShapedResult
    error: ShapedCallError
    intent: Shaped
    agent: ShapedTurn
    metadata: Metadata


def _call_identity(rec: Mapping[str, Any], ids: Mapping[str, str], context: Context, notes: Notes) -> dict[str, Any]:
    # The call's ID and tool name passed the ID check: strings.
    call_id = join(rec["callId"])
    identity: dict[str, Any] = {"eventId": call_id, "schemaVersion": 2, "kind": "tool_call"}
    if "capturedAt" in rec:
        identity["capturedAt"] = _plain_value(rec["capturedAt"])
    identity.update({"source": "live", "sdk": context.sdk, "callId": call_id, "toolName": join(rec["toolName"]), **ids})
    if "turnRef" in rec:
        identity["turnRef"] = text(rec["turnRef"])
    _named_fields(rec, identity, notes)
    if "sessionId" in rec:
        identity["sessionId"] = join(rec["sessionId"])
    for key in ("sessionSource", "status", "durationMs", "round", "extra"):
        if key in rec:
            identity[key] = _plain_value(rec[key])
    return identity


def _named_fields(source: Mapping[str, Any], into: dict[str, Any], notes: Notes) -> None:
    """What the server and the calling app called themselves, and the protocol version, as the
    protocol (or `before_send`) gave them. One too long to keep is left out and recorded as
    too_large, as other short fields are; one of another type is left out without a word, as the
    protocol may not give one."""
    for key in ("server", "client"):
        checked = named(source.get(key, MISSING), notes, key)
        if isinstance(checked, Valid):
            into[key] = dict(checked.value)
        elif isinstance(checked, Reason) and checked.reason == "too_large":
            notes.dropped[key] = "too_large"
    protocol = short(source.get("protocolVersion", MISSING))
    if isinstance(protocol, Valid):
        into["protocolVersion"] = protocol.value
    elif isinstance(protocol, Reason) and protocol.reason == "too_large":
        notes.dropped["protocolVersion"] = "too_large"


def _plain_value(value: object) -> Any:
    """A value the wrapper set, as JSON: strings and numbers as JavaScript holds them."""
    if isinstance(value, str):
        return text(value)
    held = js_number(value)
    return value if held is None else held


def _task_value(value: object, notes: Notes, path: str) -> dict[str, str] | None:
    """A task's ID and status: short strings, both needed."""
    if value is None or value is MISSING:
        return None
    found = as_object(value)
    fields = read_fields(found, ("id", "status")) if found is not None else {}
    task_id = short(fields.get("id", MISSING))
    status = short(fields.get("status", MISSING))
    if isinstance(task_id, Valid) and task_id.value != "" and isinstance(status, Valid):
        return {"id": task_id.value, "status": status.value}
    # Both strings, the ID not empty, but one too long to keep: too_large, as other short fields are.
    strings = all(isinstance(check, Valid) or (isinstance(check, Reason) and check.reason == "too_large") for check in (task_id, status))
    empty = isinstance(task_id, Valid) and task_id.value == ""
    notes.dropped[path] = "too_large" if strings and not empty else "wrong_type"
    return None


def _error_fields(fields: Mapping[str, Any], state: ShapeState, notes: Notes, field: Callable[[str, Callable[[], Any]], Any]) -> ShapedCallError:
    """An error's message, type and code, as the app or the client gave them."""
    error: ShapedCallError = {}
    message = field("error", lambda: shape_text(fields.get("message", MISSING), LIMITS.result, state, notes, "error.message"))
    if message is not MISSING:
        error["message"] = message
    kind = apply(short(fields.get("type", MISSING)), "error.type", notes)
    if kind is not MISSING and kind != "":
        error["type"] = kind
    code = fields.get("code", MISSING)
    held = js_number(code)
    if held is not None and is_finite(held):
        error["code"] = held
    else:
        given = apply(short(code), "error.code", notes)
        if given is not MISSING:
            error["code"] = given
    return error


def _shape_call(
    rec: Mapping[str, Any],
    error: Mapping[str, Any] | None,
    category: ErrorCategory | None,
    state: ShapeState,
    notes: Notes,
) -> ShapedCall | Literal["too_large"]:
    """Shaped in a fixed order, which the scan budget makes part of the result: the intent and error
    first, as the smallest and most telling, then the input, the agent's turn and the result.
    Oversized metadata drops the event, as for a turn, unless it's diagnostics, which leave it out."""
    shaped: ShapedCall = {"fixed": {}}

    def field(name: str, run: Callable[[], T]) -> T:
        return shape_field(state, notes, name, run)

    task = _task_value(rec.get("task", MISSING), notes, "task")
    if task is not None:
        shaped["task"] = task
    if "intent" in rec:
        intent = field("intent", lambda: shape_text(rec["intent"], LIMITS.intent, state, notes, "intent"))
        if intent is not MISSING:
            shaped["intent"] = intent
    if error is not None:
        fields = _error_fields(error, state, notes, field)
        if category is not None:
            fields["category"] = category
        # Neither a message nor a category: nothing the ingestion would take as an error.
        if "message" in fields or "category" in fields:
            shaped["error"] = fields
        else:
            notes.dropped["error"] = "wrong_type"
    boxed = rec.get("input", MISSING)
    if _truthy(boxed):
        found = as_object(boxed)
        value = read_fields(found, ("value",)).get("value", MISSING) if found is not None else MISSING
        given = {} if value is None or value is MISSING else value
        shaped_input = field("input", lambda: shape_any(given, LIMITS.input, state, notes, "input", True))
        if shaped_input is not MISSING:
            shaped["input"] = shaped_input
    agent = rec.get("agent", MISSING)
    early: ShapedAgent | None = None
    if isinstance(agent, Captured):
        # Read when capture() was called. One that couldn't be read then isn't read now: what the
        # app holds now may not be what it gave.
        snapshot = agent.snapshot
        if not isinstance(snapshot, ShapedAgent):
            raise CaptureUnread()
        early = snapshot
    if early is not None:
        shaped["agent"] = early.body
        _add_notes(notes, early.notes)
        _charge(state, early)
    elif agent is not None and agent is not MISSING:
        found_agent = as_object(agent)
        if found_agent is not None:
            body = shape_turn_fields(app_ids(read_fields(found_agent, AGENT_FIELDS)), state, notes, "agent.")
            # As for a turn of its own: one without a status completed.
            body.setdefault("status", "completed")
            shaped["agent"] = body
        else:
            notes.dropped["agent"] = "wrong_type"
    boxed = rec.get("result", MISSING)
    if _truthy(boxed):
        found = as_object(boxed)
        value = read_fields(found, ("value",)).get("value", MISSING) if found is not None else MISSING
        result = field("result", lambda: _shape_result(value, state, notes, False))
        if result is not None:
            shaped["result"] = result
    # identify's metadata, merged with that of the capture() inside the call, which wins per key.
    # Each is read within the budget, and the merged result must fit it too: a value one replaces
    # doesn't count.
    identified = metadata_value(rec.get("metadata", MISSING), notes, Budget(METADATA_BUDGET))
    captured: Metadata | Missing | Literal["too_large"] = MISSING
    if early is not None:
        captured = early.metadata
        _add_notes(notes, early.metadata_notes)
    else:
        found_agent = as_object(agent) if agent is not None and agent is not MISSING else None
        if found_agent is not None:
            captured = metadata_value(read_fields(found_agent, ("metadata",)).get("metadata", MISSING), notes, Budget(METADATA_BUDGET))
    diagnostics = _truthy(rec.get("diagnostics", MISSING))
    if identified == "too_large" or captured == "too_large":
        if not diagnostics:
            return "too_large"
    elif identified is not MISSING or captured is not MISSING:
        merged = merge_objects(*(part for part in (identified, captured) if isinstance(part, dict)))
        if metadata_size(merged) > METADATA_BUDGET:
            if not diagnostics:
                return "too_large"
        else:
            shaped["metadata"] = merged
    return shaped


def _truthy(value: object) -> bool:
    """JavaScript's truthiness: an empty dict or list is true, as any object is."""
    if value is None or value is MISSING or value is False:
        return False
    if isinstance(value, str):
        return value != ""
    held = js_number(value)
    if held is not None:
        return held != 0 and held == held
    return True


def _reshape_call(out: Mapping[str, Any], draft: Draft, state: ShapeState, notes: Notes) -> ShapedCall | Literal["too_large"]:
    """`before_send`'s output for a call: shaped again by the same rules, the result's blocks
    included, so a hook can't add a file's data back. The error keeps its category, which says what
    happened when the message is left out; the agent part keeps its turnId, which the call's turnRef
    is from."""
    # A field whose read raises drops the event, as for capture()'s fields.
    source = read_fields(
        out, ("input", "result", "error", "intent", "agent", "metadata", "task", "client", "server", "protocolVersion"), strict=True
    )
    shaped: ShapedCall = {"fixed": {}}

    def field(name: str, run: Callable[[], T]) -> T:
        return shape_field(state, notes, name, run)

    _named_fields(source, shaped["fixed"], notes)
    task = _task_value(source.get("task", MISSING), notes, "task")
    if task is not None:
        shaped["task"] = task
    if source.get("intent") is not None:
        intent = field("intent", lambda: shape_text(source["intent"], LIMITS.intent, state, notes, "intent"))
        if intent is not MISSING:
            shaped["intent"] = intent
    if source.get("error") is not None:
        found = as_object(source["error"])
        if found is not None:
            error = _error_fields(read_fields(found, ("message", "type", "code")), state, notes, field)
            if draft.category is not None:
                error["category"] = draft.category
            if "message" in error or "category" in error:
                shaped["error"] = error
        else:
            notes.dropped["error"] = "wrong_type"
            if draft.category is not None:
                # Replaced by something that isn't an error: the category stays, as below.
                shaped["error"] = {"category": draft.category}
    elif draft.category is not None:
        # Removed by the hook: the category, which isn't content, stays.
        shaped["error"] = {"category": draft.category}
    if "input" in source:
        value = field("input", lambda: shape_any(source["input"], LIMITS.input, state, notes, "input", True))
        if value is not MISSING:
            shaped["input"] = value
    if source.get("agent") is not None:
        found_agent = as_object(source["agent"])
        if found_agent is not None:
            agent = shape_turn_fields(app_ids(read_fields(found_agent, AGENT_FIELDS)), state, notes, "agent.")
            agent.pop("turnId", None)
            if draft.agent_turn_id is not None:
                agent["turnId"] = draft.agent_turn_id
            shaped["agent"] = agent
        else:
            notes.dropped["agent"] = "wrong_type"
    if source.get("result") is not None:
        result = field("result", lambda: _shape_result(source["result"], state, notes, True))
        if result is not None:
            shaped["result"] = result
    metadata = metadata_value(source.get("metadata", MISSING), notes, Budget(METADATA_BUDGET))
    if metadata == "too_large":
        if not draft.diagnostics:
            return "too_large"
    elif metadata is not MISSING:
        shaped["metadata"] = metadata
    return shaped


#: Metadata's own budget, as for a turn: the whole event's size.
METADATA_BUDGET: Final = 256 * 1024

AGENT_FIELDS: Final = (
    "question", "answer", "turnId", "status", "error", "toolCalls", "usage", "latency", "verification", "client",
    "apiKeyId", "surface",
)  # fmt: skip


def _shape_result(value: object, state: ShapeState, notes: Notes, sized: bool) -> ShapedResult | None:
    """MCP's result without anything binary: files become their size, text is shortened. `sized`:
    `before_send`'s output, whose files may already be only their size."""
    found = as_object(value)
    if found is None:
        notes.dropped["result"] = "wrong_type"
        return None
    fields = read_fields(found, RESULT_KEYS)
    out: ShapedResult = {}
    content = fields.get("content", MISSING)
    if isinstance(content, (list, tuple)):
        if len(content) > MAX_ENTRIES:
            state.shortened = True
        blocks: list[ShapedBlock] = []
        for index in range(min(len(content), MAX_ENTRIES)):
            # One per block, as for any list: past the budget, no more blocks are taken.
            if not spend(state, 1):
                break
            block = _shape_block(content[index], state, sized)
            if block is not None:
                blocks.append(block)
            else:
                notes.dropped[f"result.content[{index}]"] = "wrong_type"
        out["content"] = blocks
    elif content is not MISSING:
        notes.dropped["result.content"] = "wrong_type"
    if "structuredContent" in fields:
        structured = shape_any(fields["structuredContent"], LIMITS.result, state, notes, "result.structuredContent", True)
        if structured is not MISSING:
            out["structuredContent"] = structured
    if type(fields.get("isError")) is bool:
        out["isError"] = fields["isError"]
    return out


def _shape_block(value: object, state: ShapeState, sized: bool) -> ShapedBlock | None:
    # A block the content repeats is read from what its first visit worked out, a model's fields
    # worked out once: its values are read again for each.
    found = state.listed.source(value)
    if found is None:
        return None
    block = read_fields(found, BLOCK_KEYS)
    given = block.get("type")
    kind: str | None = None
    if isinstance(given, str) and given != "" and len(given) <= 64:
        joined = join(given)
        if utf16_length(joined) <= 64:
            kind = joined
    if kind is None:
        return None

    # A block's fields are MCP's, but a secret key name or one of `redact_keys` still replaces one
    # whole, numbered by its raw value.
    def hidden(key: str) -> bool:
        return state.secret_keys is not None and state.secret_keys(key)

    def text_(item: object, key: str) -> Shaped | Missing:
        if not isinstance(item, str):
            # A pydantic URL (the resource `uri` of the MCP 1.x lines' types) is its string, as
            # pydantic writes it. Anything else that isn't a string is left out, as in TypeScript.
            if not pydantic_url(item):
                return MISSING
            item = stand_in(item)
            if not isinstance(item, str):
                return MISSING
        return secret_string(item, LIMITS.result, state) if hidden(key) else shape_string(item, LIMITS.result, state)

    def mime(item: object) -> Shaped | Missing:
        if not isinstance(item, str) or len(item) > 256:
            return MISSING
        joined = join(item)
        if utf16_length(joined) > 256:
            return MISSING
        return secret_string(joined, 256, state) if hidden("mimeType") else joined

    out: ShapedBlock = {"type": kind}

    def size_only() -> None:
        # A size this SDK worked out before the hook: kept, as nothing else of the file is.
        held = js_number(block.get("bytes"))
        if sized and held is not None and is_safe_integer(held) and held >= 0:
            out["bytes"] = held

    if kind == "text":
        written = text_(block.get("text"), "text")
        out["text"] = "" if written is MISSING else written
    elif kind in ("image", "audio"):
        mime_type = mime(block.get("mimeType"))
        if mime_type is not MISSING:
            out["mimeType"] = mime_type
        data = block.get("data")
        if isinstance(data, str):
            raw = text(data)
            out["bytes"] = decoded_size(js_length(raw, state), raw)
            state.shortened = True
        else:
            size_only()
    elif kind == "resource_link":
        for key in ("uri", "name"):
            written = text_(block.get(key), key)
            if written is not MISSING:
                out[key] = written
        mime_type = mime(block.get("mimeType"))
        if mime_type is not MISSING:
            out["mimeType"] = mime_type
    elif kind == "resource":
        # After the hook, the block is in this SDK's flat form; before it, in MCP's.
        inner = state.listed.source(block.get("resource"))
        resource: Mapping[str, Any] = (
            read_fields(inner, ("uri", "mimeType", "text", "blob")) if inner is not None else block if sized else {}
        )
        uri = text_(resource.get("uri"), "uri")
        if uri is not MISSING:
            out["uri"] = uri
        mime_type = mime(resource.get("mimeType"))
        if mime_type is not MISSING:
            out["mimeType"] = mime_type
        if isinstance(resource.get("text"), str):
            written = text_(resource["text"], "text")
            assert written is not MISSING
            out["text"] = written
        elif isinstance(resource.get("blob"), str):
            blob = text(resource["blob"])
            out["bytes"] = decoded_size(js_length(blob, state), blob)
            state.shortened = True
        else:
            size_only()
    # Unknown kinds keep only their type: their fields could hold anything.
    return out


def _reserve_call(shaped: ShapedCall, identity: Mapping[str, Any], redactor: Redactor) -> None:
    """Records placeholders already in the call's event: in every field, redacted or not (its tool
    name, IDs, client and server, a block's type), as for a turn."""
    redactor.reserve(shaped)
    redactor.reserve(identity)


def _call_event(identity: Mapping[str, Any], shaped: ShapedCall, redactor: Redactor, all_: bool) -> dict[str, Any]:
    text_scope = replace(TEXT_FIELD, all=all_)

    def text_(value: Shaped) -> Json:
        return redactor.finalize(value, text_scope)

    event: dict[str, Any] = dict(identity)
    if "input" in shaped:
        event["input"] = redactor.finalize(shaped["input"], replace(JSON_FIELD, all=all_))
    if "result" in shaped:
        result: dict[str, Any] = {}
        if "content" in shaped["result"]:
            blocks: list[dict[str, Any]] = []
            for block in shaped["result"]["content"]:
                written: dict[str, Any] = {"type": text_(block["type"])}
                for key in ("uri", "name", "mimeType", "text"):
                    if key in block:
                        written[key] = text_(block[key])
                if "bytes" in block:
                    written["bytes"] = block["bytes"]
                blocks.append(written)
            result["content"] = blocks
        if "structuredContent" in shaped["result"]:
            result["structuredContent"] = redactor.finalize(shaped["result"]["structuredContent"], replace(JSON_FIELD, all=all_))
        if "isError" in shaped["result"]:
            result["isError"] = shaped["result"]["isError"]
        event["result"] = result
    if "error" in shaped:
        source: ShapedCallError = shaped["error"]
        error: dict[str, Any] = {}
        if "message" in source:
            error["message"] = text_(source["message"])
        if "type" in source:
            error["type"] = redactor.text(source["type"]) if all_ else source["type"]
        if "code" in source:
            code = source["code"]
            error["code"] = redactor.text(code) if isinstance(code, str) and all_ else code
        if "category" in source:
            error["category"] = source["category"]
        event["error"] = error
    if "intent" in shaped:
        event["intent"] = text_(shaped["intent"])
    if "task" in shaped:
        event["task"] = {"id": text_(shaped["task"]["id"]), "status": text_(shaped["task"]["status"])}
    if "agent" in shaped:
        event["agent"] = finish_turn_fields(shaped["agent"], redactor, all_)
    if "metadata" in shaped:
        event["metadata"] = finish_metadata(shaped["metadata"], redactor, all_)
    return event


__all__ = [
    "CALL_ORDER",
    "CAPTURED_IDS",
    "CAPTURE_FAILED",
    "TURN_ORDER",
    "Built",
    "BuiltError",
    "BuiltOk",
    "CallIds",
    "CallRecord",
    "Captured",
    "CaptureUnread",
    "Context",
    "Draft",
    "ShapedAgent",
    "after_hook",
    "build_call",
    "build_turn",
    "byte_size",
    "retained_bytes",
    "shape_agent",
]
