"""The event's types, as types.ts defines them (shared spec, section 2).

Events are plain dicts with the event's own camelCase keys, so the shared cases and the ingestion read
them as they are. These types describe them; the pipeline builds them as ``dict[str, Any]``.
"""

from __future__ import annotations

from typing import Dict, List, Literal, TypedDict, Union

Json = Union[str, int, float, bool, None, List["Json"], Dict[str, "Json"]]

#: Built-in redaction patterns.
PatternName = Literal["email", "phone", "card", "secret", "ip"]

#: Why a value was removed from an event, recorded in the event's ``dropped``.
DroppedReason = Literal["wrong_type", "not_json", "missing_name", "unsafe_integer", "empty", "too_large"]

#: One limited-diagnostics category, when ``identify`` failed.
ErrorCategory = Literal["thrown", "tool_error", "protocol", "timeout"]

#: Why building an event dropped it.
BuildError = Literal["invalid_input", "invalid_id", "too_large"]

Status = Literal["completed", "error"]
SessionSource = Literal["sent", "issued", "protocol"]
Round = Literal["input_required", "continuation"]


class _SdkInfo(TypedDict):
    name: str
    version: str
    language: Literal["js", "python"]


class SdkInfo(_SdkInfo, total=False):
    #: The runtime and its version, such as ``python/3.12``.
    runtime: str


class Named(TypedDict, total=False):
    name: str
    version: str


class ToolCall(TypedDict, total=False):
    name: str
    input: Json
    resultChars: float
    resultIsError: bool
    errorMessage: str
    durationMs: float


class Usage(TypedDict, total=False):
    inputTokens: float
    outputTokens: float
    costUsd: float


class TurnError(TypedDict, total=False):
    message: str
    type: str


class TurnFields(TypedDict, total=False):
    """A turn's own fields: those of a ``turn`` event, and of a call's ``agent`` part."""

    question: str
    answer: str
    turnId: str
    status: Status
    error: TurnError
    #: Absent means not recorded or all invalid; ``[]`` means no tools were called.
    toolCalls: List[ToolCall]
    usage: Usage
    latency: Json
    verification: Json
    client: Union[str, Named]
    apiKeyId: str
    surface: str


class EventBase(TypedDict, total=False):
    eventId: str
    schemaVersion: Literal[2]
    capturedAt: str
    source: Literal["live"]
    sdk: SdkInfo
    orgId: str
    userId: str
    conversationId: str
    metadata: Dict[str, Union[str, float, bool, None]]
    #: Replacements made by built-in redaction, per pattern.
    redactions: Dict[str, int]
    #: Values the SDK removed: path to reason. Paths never contain keys from the app's data.
    dropped: Dict[str, DroppedReason]
    #: Fields shortened, or left out for size (those are then absent).
    truncated: List[str]


class TurnEvent(EventBase, TurnFields, total=False):
    """From ``capture()`` outside an instrumented call."""

    kind: Literal["turn"]
    toolName: str
    callId: str


class CallError(TypedDict, total=False):
    message: str
    type: str
    code: Union[float, str]
    category: ErrorCategory


class Task(TypedDict):
    id: str
    status: str


class ToolCallEvent(EventBase, total=False):
    """One ``tools/call``, from the wrapper."""

    kind: Literal["tool_call"]
    callId: str
    turnRef: str
    toolName: str
    server: Named
    client: Named
    protocolVersion: str
    sessionId: str
    sessionSource: SessionSource
    input: Json
    #: MCP's result, with files reduced to their size and text shortened.
    result: Dict[str, Json]
    status: Status
    error: CallError
    durationMs: float
    round: Round
    task: Task
    intent: str
    extra: Literal["missing_tools"]
    #: What ``capture()`` sent from inside the call before the tool returned.
    agent: TurnFields
    #: ``identify`` failed: only the fields every event needs are kept.
    identity: Literal["failed"]


LSquareEvent = Union[TurnEvent, ToolCallEvent]

__all__ = [
    "BuildError",
    "CallError",
    "DroppedReason",
    "ErrorCategory",
    "EventBase",
    "Json",
    "LSquareEvent",
    "Named",
    "PatternName",
    "Round",
    "SdkInfo",
    "SessionSource",
    "Status",
    "Task",
    "ToolCall",
    "ToolCallEvent",
    "TurnError",
    "TurnEvent",
    "TurnFields",
    "Usage",
]
