"""JSON as JavaScript reads and writes it (json.ts, and the writer every size and hash uses).

`dumps` writes exactly what `JSON.stringify` writes for the same value: no spaces, non-ASCII
characters as themselves (U+2028 and U+2029 included), control characters and lone surrogates as
lowercase `\\u` escapes, keys in JavaScript's order, and numbers as `Number.prototype.toString`
writes them. Python's own `json.dumps` writes `1e-07`, `1e+16` and `\\u00e9`, so it is never used
for a size or a hash.
"""

from __future__ import annotations

import math
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from json.encoder import encode_basestring
from typing import Any, Final, Literal

from ._values import (
    BINARY_TYPES,
    MAX_SAFE_INTEGER,
    MISSING,
    NOT_JSON,
    UNCHANGED,
    Keys,
    Listed,
    as_object,
    is_finite,
    join,
    js_number,
    js_order,
    object_keys,
    readable,
    stand_in,
    text,
    utf16_length,
)

MAX_DEPTH: Final = 64


@dataclass
class Budget:
    """Work allowance shared by everything one event measures. Every array slot, key and node is
    charged before it is visited, so no input shape escapes it."""

    remaining: int


def read_fields(source: Mapping[Any, Any], keys: Iterable[str], strict: bool = False) -> dict[str, Any]:
    """Reads each of `keys` from the app's object once. A key that isn't there is left out; one
    that is None (JSON null) is kept, as JavaScript keeps null and drops undefined. A read that
    raises leaves its key out too, unless `strict`: then it raises, as a getter's throw does in
    TypeScript, and the event is dropped rather than sent without a field it was given, an ID
    perhaps. A value read that can't be read as what it says it is becomes UNREADABLE
    (`_values.readable`)."""
    out: dict[str, Any] = {}
    for key in keys:
        try:
            value = source.get(key, MISSING)
        except Exception:
            if strict:
                raise
            continue
        if value is not MISSING:
            out[key] = readable(value)
    return out


def is_plain_object(value: object) -> bool:
    """A JSON object: a mapping, a pydantic model or a dataclass instance."""
    return as_object(value) is not None


# Strings.

_ESCAPES: dict[int, str] = {code: f"\\u{code:04x}" for code in range(0x20)}
_ESCAPES.update({0x08: "\\b", 0x09: "\\t", 0x0A: "\\n", 0x0C: "\\f", 0x0D: "\\r", 0x22: '\\"', 0x5C: "\\\\"})
_WITH_SURROGATES: dict[int, str] = {**_ESCAPES, **{code: f"\\u{code:04x}" for code in range(0xD800, 0xE000)}}
_NEEDS_ESCAPE = re.compile(r'[\x00-\x1f"\\\ud800-\udfff]')
_SURROGATE = re.compile(r"[\ud800-\udfff]")


def string(value: str) -> str:
    """A string as `JSON.stringify` writes it, quotes included."""
    value = text(value)
    if _NEEDS_ESCAPE.search(value) is None:
        return f'"{value}"'
    if _SURROGATE.search(value) is not None:
        # Pairs Python holds as two code points are one character to JavaScript, written as itself.
        return '"' + join(value).translate(_WITH_SURROGATES) + '"'
    # Without surrogates, Python's own escaping (`json.dumps(value, ensure_ascii=False)`) is
    # JavaScript's, `_ESCAPES`, and is written in C.
    return encode_basestring(value)


# Numbers.


def _digits(value: float) -> tuple[str, int]:
    """The shortest digits that read back to `value` (positive, finite, not 0), and the exponent n
    such that value = 0.digits * 10**n, as ECMAScript's Number::toString defines them."""
    mantissa, _, exponent = repr(value).partition("e")
    whole, _, fraction = mantissa.partition(".")
    digits = whole + fraction
    stripped = digits.lstrip("0")
    point = len(whole) - (len(digits) - len(stripped)) + (int(exponent) if exponent else 0)
    return stripped.rstrip("0"), point


def number(value: int | float) -> str:
    """`Number.prototype.toString` for a Python number, read as JavaScript holds it."""
    held = js_number(value)
    if held is None:
        raise TypeError(f"not a number: {type(value).__name__}")
    if isinstance(held, int):
        return str(held)
    if math.isnan(held):
        return "NaN"
    if math.isinf(held):
        return "Infinity" if held > 0 else "-Infinity"
    if held == 0:
        return "0"
    sign = "-" if held < 0 else ""
    digits, n = _digits(abs(held))
    k = len(digits)
    if k <= n <= 21:
        return sign + digits + "0" * (n - k)
    if 0 < n <= 21:
        return sign + digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return sign + "0." + "0" * -n + digits
    exponent = n - 1
    mark = "+" if exponent >= 0 else "-"
    if k == 1:
        return f"{sign}{digits}e{mark}{abs(exponent)}"
    return f"{sign}{digits[0]}.{digits[1:]}e{mark}{abs(exponent)}"


def json_number(value: int | float) -> str:
    """A number as `JSON.stringify` writes it: `null` when it isn't finite."""
    held = js_number(value)
    if held is None:
        raise TypeError(f"not a number: {type(value).__name__}")
    return number(held) if is_finite(held) else "null"


# The writer.


def dumps(value: object) -> str:
    """`JSON.stringify(value)` for JSON made of dicts, lists, tuples, str, int, float, bool and None.

    Raises TypeError for anything else, and for a key that isn't a string: events hold only JSON.
    """
    parts: list[str] = []
    _write(value, parts, 0, sys.maxsize)
    return "".join(parts)


class _Over(Exception):
    """What `size_within` writes went past its limit."""


def size_within(value: object, limit: int) -> int | None:
    """The UTF-8 bytes of `dumps(value)`, or None when they're more than `limit`. Each part written
    is at least one byte, so writing stops once more than `limit` parts are written, at the end of
    the entry that passed it: an event far over the limit costs about what one at the limit does,
    not what writing all of it would."""
    parts: list[str] = []
    try:
        _write(value, parts, 0, limit)
    except _Over:
        return None
    size = len("".join(parts).encode("utf-8"))
    return size if size <= limit else None


def _write(value: object, out: list[str], depth: int, limit: int) -> None:
    kind = type(value)
    # The commonest first, by exact type: what each would be written as below.
    if kind is str:
        out.append(string(value))  # type: ignore[arg-type]
    elif kind is int and -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:  # type: ignore[operator]
        out.append(str(value))
    elif value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, str):
        out.append(string(value))
    elif isinstance(value, (int, float)):
        out.append(json_number(value))
    elif kind is dict or isinstance(value, Mapping):
        if depth > 512:
            raise ValueError("nested too deeply")
        assert isinstance(value, Mapping)
        keys = list(value)
        for key in keys:
            if not isinstance(key, str):
                raise TypeError(f"a JSON key must be a string, not {type(key).__name__}")
        out.append("{")
        first = True
        for key in js_order(keys):
            if not first:
                out.append(",")
            first = False
            out.append(string(key))
            out.append(":")
            _write(value[key], out, depth + 1, limit)
            if len(out) > limit:
                raise _Over
        out.append("}")
    elif isinstance(value, (list, tuple)):
        if depth > 512:
            raise ValueError("nested too deeply")
        out.append("[")
        for index, item in enumerate(value):
            if index:
                out.append(",")
            _write(item, out, depth + 1, limit)
            if len(out) > limit:
                raise _Over
        out.append("]")
    else:
        raise TypeError(f"not JSON: {type(value).__name__}")


# Measuring.


def _object(item: object, limit: int, listed: Listed | None) -> tuple[Mapping[Any, Any], Keys | None] | None:
    """What an object is read from, and its keys (`object_keys`), or None when it isn't one: in
    `listed`, enumerated once per object, and only its values read again at each visit. May raise
    what the mapping's own iteration raises."""
    if listed is None:
        found = as_object(item)
        return None if found is None else (found, object_keys(found, limit))
    found = listed.source(item)
    return None if found is None else (found, listed.keys(item, limit))


def _merged(keys: Keys, limit: int) -> list[Any] | None:
    """The keys to read a mapping's entries by, one per key JavaScript holds: two of Python's keys
    that are one there (a pair held as two code points, and the character it makes) are one entry,
    in the first one's place, holding the last one's value. Worked out before any value is read,
    so the one replaced is never measured; `keys.names` itself when no key holds a surrogate, as
    nearly always.

    Reads at most twice `limit` characters of the keys to tell, a cost of Python's own, apart from
    the allowance, which is charged each key JavaScript holds once, as there. Each of Python's
    spellings of a key holds at most as many code points as the key has UTF-16 units, so two that
    are one key fitting in `limit` are always read. Past that, None: which of the keys are one
    isn't known, so no value can be read, as it may be one JavaScript never holds, and the mapping
    is too large, the allowance used up, so every tool result measured after it within the same
    allowance is too large too, however small. `limit` is what's left of the allowance: after
    results that used most of it, keys far short of 2,000,000 code points are enough. A key held
    three ways or more (only one with two astral characters or more can be) can so be too large
    where TypeScript measures it, and a mapping whose keys can't fit, with every result after it,
    too large where TypeScript finds a value that isn't JSON before it reaches them, and measures
    the results after it, with or without a surrogate in any of the keys."""
    if keys.merged is not None:
        return keys.merged
    read = 0
    paired = False
    for raw in keys.names:
        if raw.isascii():
            continue
        key = text(raw)
        read += len(key)
        if read > 2 * limit:
            return None
        if _SURROGATE.search(key) is not None:
            paired = True
    if not paired:
        keys.merged = keys.names
        return keys.names
    chosen: dict[str, Any] = {}
    for raw in keys.names:
        # A dict keeps a key's first place, and holds the last value given for it.
        chosen[join(raw)] = raw
    keys.merged = list(chosen.values()) if len(chosen) < len(keys.names) else keys.names
    return keys.merged


def json_length(value: object, allowance: Budget, listed: Listed | None = None) -> int | Literal["too_large", "not_json"]:
    """Exact length of the value's `JSON.stringify` text, in code points, without keeping a copy,
    within a shared work allowance. Says why when it can't: the allowance ran out, or the value
    isn't plain JSON. `listed`: the objects already enumerated (`Listed`), shared by the measures of
    one event's tool results, so an object they all refer to is enumerated once, a model's fields
    worked out once, and its values, a model's extra fields too, read at each visit.

    Python's values follow the value rules (`_values`): models and dataclasses are objects, UUIDs,
    enums and dates stand for their JSON, and binary data, like a Buffer in JavaScript, isn't JSON.
    """
    ancestors: set[int] = set()

    def charge(cost: int) -> bool:
        allowance.remaining -= cost
        return allowance.remaining >= 0

    def measure(item: object, depth: int) -> int | None:
        try:
            return measure_value(item, depth)
        except Exception:
            # A value whose own type check or conversion raises (a lazy object whose factory
            # fails) isn't JSON, as a mapping that raises isn't.
            return None

    def measure_value(item: object, depth: int) -> int | None:
        # Stand-ins first: a UUID or an enum is measured as what it stands for, at no extra charge.
        refused = False
        for _ in range(MAX_DEPTH):
            replaced = stand_in(item)
            if replaced is UNCHANGED:
                break
            if replaced is NOT_JSON:
                refused = True
                break
            item = replaced
        else:
            refused = True
        if not charge(1) or depth >= MAX_DEPTH or refused:
            return None
        if item is None:
            return 4
        if item is True:
            return 4
        if item is False:
            return 5
        if isinstance(item, str):
            raw = text(item)
            # At least its length in UTF-16 units: one that can't fit isn't read.
            if len(raw) > allowance.remaining:
                allowance.remaining -= len(raw)
                return None
            if not charge(utf16_length(raw)):
                return None
            return len(string(raw))
        if isinstance(item, (int, float)):
            held = js_number(item)
            if held is None:
                # One that only says it is a number (a proxy).
                return None
            # Same as JSON.stringify: an infinite number, or an int too large for a double, is null.
            return len(number(held)) if is_finite(held) else 4
        if isinstance(item, BINARY_TYPES):
            return None
        if id(item) in ancestors:
            return None
        if isinstance(item, (list, tuple)):
            ancestors.add(id(item))
            total = 2 + max(0, len(item) - 1)
            for element in item:
                length = measure(element, depth + 1)
                if length is None:
                    return None
                total += length
            ancestors.discard(id(item))
            return total
        try:
            # Every key costs at least one: a mapping with more than what's left can't fit.
            found = _object(item, max(0, allowance.remaining), listed)
        except Exception:
            return None
        if found is None:
            return None
        source, keys = found
        if keys is None:
            allowance.remaining = -1
            return None
        if keys.others:
            return None
        names = _merged(keys, max(0, allowance.remaining))
        if names is None:
            # Too large, before any value is read, and the allowance used up: never told apart again,
            # and every result measured after it within the allowance too large too (`_merged`).
            allowance.remaining = -1
            return None
        ancestors.add(id(item))
        total = 2
        first = True
        for raw in names:
            try:
                element = source.get(raw, MISSING)
            except Exception:
                return None
            if element is MISSING:
                if not charge(1):
                    return None
                continue
            raw_key = text(raw)
            if len(raw_key) > allowance.remaining:
                allowance.remaining -= len(raw_key)
                return None
            key = join(raw_key)
            if not charge(utf16_length(key)):
                return None
            length = measure(element, depth + 1)
            if length is None:
                return None
            total += (0 if first else 1) + len(string(key)) + 1 + length
            first = False
        ancestors.discard(id(item))
        return total

    length = measure(value, 0)
    if allowance.remaining < 0:
        return "too_large"
    return "not_json" if length is None else length


__all__ = ["MAX_DEPTH", "Budget", "dumps", "is_plain_object", "json_length", "json_number", "number", "read_fields", "size_within", "string"]
