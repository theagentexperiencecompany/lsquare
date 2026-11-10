"""The extras' rules that don't depend on a call: which of their inputs a tool's listed schema
leaves to the wrapper, the listed copy that shows them, get_more_tools, and new session IDs.

Ported from packages/lsquare/src/mcp/extras.ts, with the same names, texts and behaviour. Reading a
tool's own definition, which TypeScript does from zod, is `_ownership`'s, from pydantic.
Parts adapted from AgentCat's TypeScript SDK (MIT, © 2025 AgentCat, Inc.; see THIRD_PARTY_NOTICES):
adding the extras' inputs to a listed tool's schema.

Schemas are JSON-like values: any `Mapping` is an object, a `list` or `tuple` an array. Nothing here
changes the schema it's given, and nothing raises on it.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from .._values import text

SESSION_ID: Final = "session_id"
INTENT: Final = "intent"
MISSING_TOOLS: Final = "get_more_tools"

#: A session ID an agent may send back: `ses_` and 22 letters, digits, `-` or `_`.
SESSION_FORMAT: Final = re.compile(r"^ses_[A-Za-z0-9_-]{22}\Z", re.ASCII)
_ALPHABET: Final = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"

_DESCRIPTIONS: Final[Mapping[str, str]] = {
    SESSION_ID: (
        "The ID of the task this call belongs to. Leave it out on your first call. On every later call for the same "
        "task, send the ID an earlier result gave you, and pass it to any subagent working on that task."
    ),
    INTENT: "In one sentence, why you're calling this tool and what the user is trying to do. No personal data or secrets.",
}
_MISSING_TOOLS_DESCRIPTION: Final = (
    "Call this when none of the available tools fits what you need, or one fits only in part. It doesn't return new "
    "tools: it tells the server's developers what to add."
)
_NEED_DESCRIPTION: Final = "What you needed to do and couldn't, in one or two sentences. No personal data or secrets."
_REPLY: Final = "Noted. No other tools are available right now."
_NO_NEED: Final = (
    "Invalid arguments for tool get_more_tools: need is required, as a string saying what you needed to do and couldn't."
)

#: Keys a listed schema's root may hold for inputs to be added to it: the object's own, and those that only describe.
ROOT_KEYS: Final = frozenset([
    "type", "properties", "required", "additionalProperties", "$schema", "$id", "$comment", "$defs", "definitions",
    "title", "description", "default", "examples", "deprecated",
])  # fmt: skip
#: How much of a listed schema is read to find its references, at most.
WALK_NODES: Final = 10_000
WALK_DEPTH: Final = 64

_ABSENT: Final[Any] = object()


def new_session_id() -> str:
    """A new session ID: `ses_` and 22 random base64url characters."""
    # 64 characters: each byte's low 6 bits pick one, evenly.
    return "ses_" + "".join(_ALPHABET[byte & 63] for byte in secrets.token_bytes(22))


def id_block(id: str) -> dict[str, str]:
    """The text block that gives the agent its task's ID, at the end of a result."""
    return {
        "type": "text",
        "text": f"Task ID: {id}. Send it as session_id on later calls for this task, and pass it to any subagent working on it.",
    }


def missing_tools_tool(sessions: bool) -> dict[str, Any]:
    """get_more_tools as listed: it takes `need`, and `session_id` when sessions are on. It changes
    nothing on the server, so it's read-only, and its `need` leaves the server."""
    properties: dict[str, Any] = {"need": {"type": "string", "description": _NEED_DESCRIPTION}}
    if sessions:
        properties[SESSION_ID] = {"type": "string", "description": _DESCRIPTIONS[SESSION_ID]}
    return {
        "name": MISSING_TOOLS,
        "description": _MISSING_TOOLS_DESCRIPTION,
        "inputSchema": {"type": "object", "properties": properties, "required": ["need"]},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True},
    }


def missing_tools_reply(need: object) -> dict[str, Any]:
    """The wrapper's reply to get_more_tools: an isError result, as the MCP SDKs give, without a string `need`."""
    if not isinstance(need, str):
        return {"content": [{"type": "text", "text": _NO_NEED}], "isError": True}
    return {"content": [{"type": "text", "text": _REPLY}]}


@dataclass(frozen=True)
class Ownership:
    """What a tool's definition shows of the extras' inputs: the names it declares (`declared`, only
    `session_id` and `intent`), when it can be read. When it can't (`why` says why, to finish "its
    input schema can't be read safely (...)"), both are assumed to be the tool's. Read as the
    definition is now, never cached."""

    readable: bool
    declared: frozenset[str] = frozenset()
    why: str = ""

    @classmethod
    def of(cls, names: Iterable[str]) -> Ownership:
        """A readable definition that declares those of `names` that are extras' inputs."""
        return cls(True, frozenset(name for name in names if name == SESSION_ID or name == INTENT))

    @classmethod
    def unreadable(cls, why: str) -> Ownership:
        return cls(False, frozenset(), why)

    def owns(self, name: str) -> bool:
        """The tool may read this input: it declares it, or its definition can't be read."""
        return not self.readable or name in self.declared


#: A definition that couldn't be read: reading it raised.
COULD_NOT_READ: Final = Ownership.unreadable("it could not be read")
_NOT_NAMED_PROPERTIES: Final = Ownership.unreadable("its listed input schema is more than an object of named properties")


def _get(value: Mapping[Any, Any], key: str) -> Any:
    """One entry of a schema object, `_ABSENT` when it has none. A read that raises raises on,
    failing the whole reading: an absent entry is the permissive case, so a failed read must never
    pass for one."""
    return value[key] if key in value else _ABSENT


def _is_text(value: object, expected: str) -> bool:
    return isinstance(value, str) and text(value) == expected


def _is_array(value: object) -> bool:
    return isinstance(value, (list, tuple))


def _roots_only(schema: Mapping[Any, Any]) -> bool:
    for key in schema:
        if not isinstance(key, str) or text(key) not in ROOT_KEYS:
            return False
    return True


def listed_ownership(schema: object) -> Ownership:
    """What a listed JSON Schema says of the extras' inputs, on a low-level server, where the
    listing speaks for the tool: readable only when its root holds only `type: "object"`,
    `properties`, a `required` of names, `additionalProperties` absent or false, and the keys that
    only describe. `declared` holds only the extras' inputs it names, in `properties` or `required`.
    A read that raises, or a field of the wrong kind, makes it unreadable.
    """
    try:
        if not isinstance(schema, Mapping) or not _roots_only(schema):
            return _NOT_NAMED_PROPERTIES
        if not _is_text(_get(schema, "type"), "object"):
            return _NOT_NAMED_PROPERTIES
        properties = _get(schema, "properties")
        if properties is not _ABSENT and not isinstance(properties, Mapping):
            return _NOT_NAMED_PROPERTIES
        required = _get(schema, "required")
        if required is not _ABSENT and (not _is_array(required) or len(required) > 10_000):
            return _NOT_NAMED_PROPERTIES
        additional = _get(schema, "additionalProperties")
        if additional is not _ABSENT and additional is not False:
            return _NOT_NAMED_PROPERTIES
        names: set[str] = set()
        for name in required if required is not _ABSENT else ():
            if not isinstance(name, str):
                return _NOT_NAMED_PROPERTIES
            names.add(text(name))
        if properties is not _ABSENT:
            names.update(name for name in (SESSION_ID, INTENT) if name in properties)
        return Ownership.of(names)
    except Exception:
        return _NOT_NAMED_PROPERTIES


def same_ownership(a: Ownership, b: Ownership) -> bool:
    """Two readings agree on the extras' inputs: both unreadable, or both declaring the same of them."""
    if not a.readable or not b.readable:
        return a.readable == b.readable
    return all((name in a.declared) == (name in b.declared) for name in (SESSION_ID, INTENT))


def advertise(schema: object, names: Sequence[str]) -> dict[str, Any] | None:
    """A copy of a tool's listed input schema with `names` added as optional string properties,
    each only where the schema takes it as one more without changing what it accepts; None when
    none can be added. The root may hold only `type: "object"`, `properties`, a `required` without
    the name, `additionalProperties` absent or false, and keys that only describe. Every reference
    in it must point into its `$defs`, `definitions` or one of its own properties: one to the root,
    as a recursive schema has, would show the inputs at nested levels too, where they aren't removed.
    """
    try:
        if not isinstance(schema, Mapping) or len(names) == 0 or not _roots_only(schema):
            return None
        if not _is_text(_get(schema, "type"), "object"):
            return None
        properties = _get(schema, "properties")
        if properties is not _ABSENT and not isinstance(properties, Mapping):
            return None
        required = _get(schema, "required")
        if required is not _ABSENT and not _is_array(required):
            return None
        additional = _get(schema, "additionalProperties")
        if additional is not _ABSENT and additional is not False:
            return None

        def taken(name: str) -> bool:
            if properties is not _ABSENT and name in properties:
                return True
            return required is not _ABSENT and any(_is_text(entry, name) for entry in required)

        adding = [name for name in names if not taken(name)]
        if not adding or not _local_references(schema, adding):
            return None
        added: dict[Any, Any] = dict(properties) if properties is not _ABSENT else {}
        for name in adding:
            entry = {"type": "string"}
            description = _DESCRIPTIONS.get(name)
            if description is not None:
                entry["description"] = description
            added[name] = entry
        return {**schema, "properties": added}
    except Exception:
        return None


def _local_references(root: Mapping[Any, Any], adding: Sequence[str]) -> bool:
    """Every `$ref` in the schema points into its `$defs`, `definitions` or a property it already
    has, and it has no dynamic references. Each entry read counts against the budget, before its
    value is read, so a wide object or list costs no more than a deep one.
    """
    stack: list[tuple[Any, int]] = [(root, 0)]
    budget = WALK_NODES
    while stack:
        node, depth = stack.pop()
        if depth > WALK_DEPTH:
            return False
        if isinstance(node, Mapping):
            if "$dynamicRef" in node or "$recursiveRef" in node:
                return False
            if "$ref" in node and not _local_reference(node["$ref"], adding):
                return False
            # Iteration hands out keys one at a time, so the budget stops the walk before it reads them all.
            for key in node:
                budget -= 1
                if budget < 0:
                    return False
                value = node[key]
                if isinstance(value, Mapping) or _is_array(value):
                    stack.append((value, depth + 1))
        else:
            for value in node:
                budget -= 1
                if budget < 0:
                    return False
                if isinstance(value, Mapping) or _is_array(value):
                    stack.append((value, depth + 1))
    return True


def _local_reference(ref: object, adding: Sequence[str]) -> bool:
    if not isinstance(ref, str):
        return False
    ref = text(ref)
    # Percent-encoding could spell any name: such a reference counts as unknown.
    if "%" in ref:
        return False
    if ref.startswith("#/$defs/") or ref.startswith("#/definitions/"):
        return True
    if not ref.startswith("#/properties/"):
        return False
    name = ref[len("#/properties/") :].split("/")[0].replace("~1", "/").replace("~0", "~")
    return name not in adding
