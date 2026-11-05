"""Shortening (shared spec, section 2), as shape.ts does it: values from the app become event JSON
within the limits, before redaction. Strings over a field's limit become `Cut`s, which keep what
redaction needs to see past the cut; binary-looking strings become `Binary`. Redaction (`_redact`)
turns both into JSON.

Every count is in code points of the string as JavaScript reads it (`_values.join`), so both SDKs
cut in the same places. Indexes into a `Cut` are code points too, where TypeScript's are UTF-16.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Dict, Final, List, Union

from ._jsjson import MAX_DEPTH, dumps, number
from ._values import (
    BINARY_TYPES,
    MAX_SAFE_INTEGER,
    MISSING,
    NOT_JSON,
    UNCHANGED,
    Listed,
    NotJson,
    as_object,
    binary_length,
    is_finite,
    join,
    joined_prefix,
    js_number,
    object_keys,
    stand_in,
    text,
    utf16_length,
)

#: Characters (code points) read per event, plus one per value visited, across all its fields.
SCAN_BUDGET: Final = 1_000_000


class LIMITS:
    input: Final = 16_000
    agent: Final = 16_000
    result: Final = 4_000
    intent: Final = 500


MAX_ENTRIES: Final = 100
MAX_LEVELS: Final = 10
#: Redaction reads this far past a cut, so a secret that runs into it is still recognised.
LOOKAHEAD: Final = 8_000
BINARY_MIN: Final = 1_000
BINARY_HEAD: Final = 4_096
BINARY_TAIL: Final = 1_024
DATA_HEADER_MAX: Final = 1_024
CUT_MARK: Final = "…[cut]"
#: Python only: keys enumerated per event, across its mappings, to read each in JavaScript's order.
MAX_KEYS: Final = 1_000_000
#: Python only: code points read per shaping pass to find JavaScript's length of the non-ASCII
#: strings a file's size is worked out from (`js_length`), apart from the scan budget: TypeScript
#: knows a string's length without reading it.
MAX_LENGTH_READS: Final = 16 * SCAN_BUDGET
#: A marker this SDK writes, at the end of a string it cut.
MARKER: Final = re.compile(r"…\[(?:\+(\d{1,16}) chars|cut)\]\Z", re.ASCII)
#: A placeholder from redaction, kept as it is under a secret key name.
WHOLE_PLACEHOLDER: Final = re.compile(r"\[(?:EMAIL|PHONE|CARD|SECRET|IP)_\d{1,9}\]", re.ASCII)
_BASE64_CHAR: Final = re.compile(r"[A-Za-z0-9+/\-_=]*", re.ASCII)


@dataclass
class Scan:
    remaining: int


@dataclass(frozen=True)
class Cut:
    """A shortened string: `window` is what was kept plus what redaction reads past `cut` (a code
    point index), cut for a field whose limit is `limit`."""

    window: str
    cut: int
    mark: str
    limit: int
    #: Already read for values under secret key names, with the lookahead this one no longer has.
    scanned: bool = False


@dataclass(frozen=True)
class Binary:
    """A string, or Python bytes, that held a file: only its size is kept."""

    bytes: int


@dataclass(frozen=True)
class TooDeep:
    """A container below the nesting limit."""


@dataclass(frozen=True)
class Secret:
    """A value under a secret key name, replaced whole before anything else sees it, `before_send`
    included. `identity` tells two equal secrets apart from different ones, for numbering: the raw
    value, or None when it couldn't be read whole, so it is numbered on its own."""

    identity: str | None


Shaped = Union[str, int, float, bool, None, Cut, Binary, TooDeep, Secret, List["Shaped"], Dict[str, "Shaped"]]


@dataclass
class ShapeState:
    scan: Scan
    #: Something was cut, left out at a limit, or replaced by a size.
    shortened: bool = False
    #: Something couldn't be encoded as JSON and was removed (0.1's `not_json`).
    lossy: bool = False
    #: Shaping `before_send`'s output: a string this SDK cut keeps its marker.
    after_hook: bool = False
    #: The strings this SDK cut or rewrote before `before_send`, as the hook got them (`Redactor.cuts`).
    cuts: Mapping[str, int] | None = None
    #: This event's secret key names; None when the `secret` pattern is off.
    secret_keys: Callable[[str], bool] | None = None
    #: The same, while shaping a field whose keys come from the app's data; otherwise None.
    secret_key: Callable[[str], bool] | None = None
    #: Python only: keys still allowed to be enumerated for this event (MAX_KEYS).
    keys: int = MAX_KEYS
    #: Python only: a mapping had more keys than that, and was left out.
    too_large: bool = False
    #: Python only: the longest string in `cuts`, so a longer one is never joined to look it up.
    longest_cut: int = 0
    #: Python only: code points still allowed to be read to find a file's JavaScript length
    #: (MAX_LENGTH_READS), and each length found, by the string's identity (`js_length`).
    length_reads: int = MAX_LENGTH_READS
    lengths: dict[int, tuple[str, int]] = field(default_factory=dict)
    #: The objects this pass reads by their fields' names, or counts the keys of: a tool call, a
    #: content block. Each worked out once, a class's fields once for all its objects, and its
    #: values read at each visit (`Listed`). TypeScript's is shared with measuring the tool results,
    #: which here have their own, as their keys are enumerated within the measuring allowance rather
    #: than this pass's bound on keys, sharing only the classes' fields.
    listed: Listed = field(default_factory=Listed)


def new_state(
    *,
    after_hook: bool = False,
    budget: int | None = None,
    cuts: Mapping[str, int] | None = None,
    secret_keys: Callable[[str], bool] | None = None,
) -> ShapeState:
    return ShapeState(
        scan=Scan(SCAN_BUDGET if budget is None else budget),
        after_hook=after_hook,
        cuts=cuts,
        secret_keys=secret_keys,
        longest_cut=max(map(len, cuts), default=0) if cuts else 0,
    )


def advance(text: str, start: int, count: int) -> tuple[int, int]:
    """Advances `count` code points from index `start` of a joined string: the index reached and
    how many code points were passed. A lone surrogate counts as one, as in JavaScript's count."""
    end = min(len(text), start + max(0, count))
    return end, max(0, end - start)


def decoded_size(length: int, text: str) -> int:
    """Bytes a base64 payload of `length` characters decodes to; `text` ends with its padding (at most 2)."""
    padding = 0
    while padding < 2 and padding < length and padding < len(text) and text[len(text) - 1 - padding] == "=":
        padding += 1
    return ((length - padding) * 3) // 4


def js_length(value: str, state: ShapeState | None) -> int:
    """JavaScript's `length` of a string a file's size is worked out from. An ASCII string's is known
    at once. Any other's is read, which TypeScript needn't do: once per shaping pass however many
    times the event holds the string, and at most MAX_LENGTH_READS code points per pass. A string
    longer than what's left of that is taken as its code points: an astral character in it counts
    once, where JavaScript counts two, so its size can be lower than TypeScript's there."""
    if value.isascii():
        return len(value)
    if state is None:
        return utf16_length(value)
    held = state.lengths.get(id(value))
    if held is not None and held[0] is value:
        return held[1]
    if len(value) > state.length_reads:
        return len(value)
    state.length_reads -= len(value)
    length = utf16_length(value)
    # Kept with the string, so its identity isn't another's while the pass lasts.
    state.lengths[id(value)] = (value, length)
    return length


def binary_size(value: str, state: ShapeState | None = None) -> int | None:
    """The decoded size of a string that holds a file: any `data:` URI with `;base64,`, or a string of
    at least 1,000 characters whose first 4 KB and last 1 KB are only base64 characters. A heuristic:
    the rest isn't read. Bounded work, not charged to the scan budget.

    Lengths are JavaScript's (UTF-16 units), as TypeScript measures them. Base64 is ASCII, so the
    ends read here hold the same characters as JavaScript's slices whenever either is base64. A
    non-ASCII string's length is read within the pass's allowance (`js_length`).
    """
    raw = text(value)
    if len(raw) >= 5 and raw[:5].lower() == "data:":
        header = raw[: DATA_HEADER_MAX + 1]
        comma = header.find(",", 5)
        if comma != -1:
            units = utf16_length(header[:comma])
            if units <= DATA_HEADER_MAX and header[5:comma].lower().endswith(";base64"):
                return decoded_size(js_length(raw, state) - units - 1, raw)
    if len(raw) < BINARY_MIN:
        return None
    if _BASE64_CHAR.fullmatch(raw, 0, BINARY_HEAD) is None:
        return None
    if len(raw) > BINARY_HEAD and _BASE64_CHAR.fullmatch(raw, len(raw) - BINARY_TAIL) is None:
        return None
    return decoded_size(js_length(raw, state), raw)


def shape_string(value: str, limit: int, state: ShapeState) -> str | Cut | Binary:
    """Shortens one string to `limit` code points. Within the limit it stays a string (charged its
    length); past it, a `Cut` keeping the first `limit` code points plus `LOOKAHEAD` more for
    redaction, marked `…[+N chars]` with the exact count of the rest when that fits in the budget,
    which is charged for counting it. When it doesn't fit, counting it would use the budget up, so it
    is: the string ends `…[cut]`, and so does every string after it. Once the budget runs out,
    strings are cut where it ran out.

    Only what is read is joined (`_values.joined_prefix`): at most what's left of the budget.
    """
    scan = state.scan
    raw = text(value)
    if raw == "":
        return raw
    if scan.remaining <= 0:
        state.shortened = True
        return Cut("", 0, CUT_MARK, limit)
    size = binary_size(raw, state)
    if size is not None:
        state.shortened = True
        return Binary(size)

    # After before_send: a string this SDK cut, as the hook got it, is kept as it is where its limit
    # is no tighter than the one it was cut at. Any other marked string is shortened as usual,
    # adding the count in its marker to what is cut now.
    earlier: str | None = None
    if state.after_hook:
        # Only the end is searched: a regex anchored at the end would still scan a long string.
        tail = raw[-32:]
        marked = MARKER.search(tail)
        if marked is not None:
            # Where the body ends, not the body: slicing copies, and a hook's string can be huge.
            body_end = len(raw) - len(tail) + marked.start()
            cut_at = None
            if state.cuts and len(raw) <= 2 * state.longest_cut + 2:
                cut_at = state.cuts.get(join(raw))
            if cut_at is not None and cut_at <= limit:
                state.shortened = True
                # No longer than a string this SDK cut.
                whole = join(raw[:body_end])
                end, passed = advance(whole, 0, scan.remaining)
                if end == len(whole):
                    scan.remaining -= passed
                    return Cut(whole, len(whole), marked.group(0), limit, True)
                scan.remaining = 0
                return Cut(whole[:end], end, CUT_MARK, limit, True)
            earlier = marked.group(0)
            # Only what joined_prefix reads below, plus one character, so it can still tell that
            # the body goes on: its verdict, whole or not, is the same as on the whole body.
            raw = raw[: min(body_end, 2 * (scan.remaining + LOOKAHEAD + 2) + 3)]
        elif state.cuts and len(raw) <= 2 * state.longest_cut + 2:
            # A string whose secret values were replaced before the hook, as the hook got it, is kept
            # whole where its length before that fits: placeholders may have made it longer than that.
            whole = join(raw)
            before = state.cuts.get(whole)
            if before is not None and before <= limit and before <= scan.remaining:
                scan.remaining -= before
                return whole

    # Never more than what's left of the budget is read, plus one: a string longer than that
    # can't be counted, so where it ends doesn't matter.
    # Plus what redaction reads past a cut, which it reads also where the budget runs out.
    joined, whole_string = joined_prefix(raw, scan.remaining + LOOKAHEAD + 2)
    readable = min(limit, scan.remaining)
    cut, kept = advance(joined, 0, readable)
    if cut == len(joined) and whole_string:
        scan.remaining -= kept
        return joined if earlier is None else Cut(joined, cut, earlier, limit)
    state.shortened = True
    # Redaction reads LOOKAHEAD past every cut, also where the budget runs out, so a token running
    # into the cut is matched whole. The budget runs out once, so that reads at most LOOKAHEAD more.
    end, ahead = advance(joined, cut, LOOKAHEAD)
    if kept < limit or kept + ahead > scan.remaining:
        # The budget ran out inside this string, or inside what redaction reads past the cut.
        scan.remaining = 0
        return Cut(joined[:end], cut, CUT_MARK, limit)
    scan.remaining -= kept + ahead
    rest: int | None = ahead
    if not (end == len(joined) and whole_string):
        last, counted = advance(joined, end, scan.remaining + 1)
        if last == len(joined) and whole_string and counted <= scan.remaining:
            scan.remaining -= counted
            rest = ahead + counted
        else:
            scan.remaining = 0
            rest = None
    return Cut(joined[:end], cut, _combine(rest, earlier), limit)


def is_marked(value: str) -> bool:
    """Whether a string ends with a marker this SDK writes."""
    return MARKER.search(value[-32:]) is not None


def _combine(rest: int | None, earlier: str | None) -> str:
    """The marker for `rest` characters cut now (None: not counted), plus a marker from before."""
    if rest is None:
        return CUT_MARK
    if earlier is None:
        return f"…[+{rest} chars]"
    found = MARKER.search(earlier)
    before = found.group(1) if found is not None else None
    # Added as JavaScript adds them: in doubles, which a 16-digit count from a forged marker rounds.
    return CUT_MARK if before is None else f"…[+{number(float(rest) + float(before))} chars]"


def plain(value: Shaped) -> Any:
    """Shaped JSON as plain JSON, without redaction: a value's identity under a secret key, and
    output with redaction off."""
    if isinstance(value, Cut):
        return value.window[: value.cut] + value.mark
    if isinstance(value, Binary):
        return {"omitted": "binary", "bytes": value.bytes}
    if isinstance(value, TooDeep):
        return {"omitted": "depth"}
    if isinstance(value, Secret):
        return "[SECRET]"
    if isinstance(value, list):
        return [plain(item) for item in value]
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    return value


def _secret_value(value: object, limit: int, state: ShapeState, ancestors: set[int], level: int) -> Shaped | NotJson:
    """A value under a secret key name, read within the limits but never shortened: its identity is
    the raw value. A string is read up to the field's limit; anything else is converted, and counts
    only if nothing in it was cut, left out or turned into a size. Values that can't hold a secret
    (None, booleans, values empty as given) and placeholders stay as they are."""
    try:
        # By type, not isinstance: bool can't be subclassed, so only a proxy passes for one.
        if value is None or type(value) is bool:
            return value
        if isinstance(value, str):
            return "" if value == "" else secret_string(value, limit, state)
    except Exception:
        # As in `_convert`: a value whose own type check raises isn't JSON.
        return NOT_JSON
    shortened, lossy, secret_key = state.shortened, state.lossy, state.secret_key
    state.shortened = False
    state.lossy = False
    # Read as a whole, as 0.1 did: secret key names inside it don't matter.
    state.secret_key = None
    converted = _convert(value, limit, state, ancestors, level)
    exact = not state.shortened and not state.lossy
    state.shortened, state.lossy, state.secret_key = shortened, lossy, secret_key
    if converted is NOT_JSON:
        return NOT_JSON
    # Empty as it was given, not made empty by the budget or by leaving out what JSON can't hold.
    if exact and isinstance(converted, (list, dict)) and len(converted) == 0:
        return converted
    return Secret(dumps(plain(converted)) if exact else None)


def secret_string(value: str, limit: int, state: ShapeState) -> Shaped:
    """A string under a secret key name: its raw value, read up to `limit`, or a number of its own."""
    raw = text(value)
    if raw == "" or (len(raw) <= 20 and WHOLE_PLACEHOLDER.fullmatch(raw) is not None):
        return raw
    count = min(limit, state.scan.remaining)
    joined, whole = joined_prefix(raw, count + 1)
    end, passed = advance(joined, 0, count)
    state.scan.remaining -= passed
    return Secret(joined if end == len(joined) and whole else None)


def shape_json(value: object, limit: int, state: ShapeState) -> Shaped | NotJson:
    """Converts a value from the app into shaped JSON, without raising and without reading more than
    the limits allow: lists and objects keep their first 100 entries, nesting stops at 10 levels, and
    strings are shortened to `limit`. Objects are read in JavaScript's order (integer-like keys
    first, ascending, then the others as they were added). Under a secret key name, when
    `state.secret_key` is set, a value becomes a `Secret`. Parts JSON can't encode are removed and
    the state marked lossy; a value that is itself not encodable gives NOT_JSON. Python's own values
    follow the value rules (`_values`)."""
    return _convert(value, limit, state, set(), 0)


def spend(state: ShapeState, cost: int) -> bool:
    """Charges `cost` to the scan budget. Past it, False: the budget is used up, and the value
    marked shortened."""
    if state.scan.remaining < cost:
        state.scan.remaining = 0
        state.shortened = True
        return False
    state.scan.remaining -= cost
    return True


def _key_cost(raw: str, remaining: int) -> tuple[int, str | None]:
    """A key's charge, its code points up to what's left (TypeScript's `advance(key, 0, remaining)`),
    and the key joined when it can be read whole."""
    if len(raw) > 2 * remaining + 2:
        return remaining, None
    key = join(raw)
    return min(len(key), remaining), key


#: Exact types that are never a stand-in: read as they are.
_PLAIN_CONTAINERS: Final = frozenset([dict, list, tuple])


def _convert(value: object, limit: int, state: ShapeState, ancestors: set[int], level: int) -> Shaped | NotJson:
    try:
        kind = type(value)
        # The commonest first, by exact type: what each would become below.
        if kind is str:
            return shape_string(value, limit, state)  # type: ignore[arg-type]
        if kind is int and -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:  # type: ignore[operator]
            return value  # type: ignore[return-value]
        if kind is float:
            return value if math.isfinite(value) else NOT_JSON  # type: ignore[arg-type,return-value]
        return _convert_value(value, limit, state, ancestors, level)
    except Exception:
        # A value whose own type check or reading raises (a lazy object whose factory fails, a list
        # whose items can't be read): it isn't JSON, and only it is left out, as a mapping that raises.
        return NOT_JSON


def _convert_value(value: object, limit: int, state: ShapeState, ancestors: set[int], level: int) -> Shaped | NotJson:
    if value is None:
        return None
    # By type, not isinstance: bool can't be subclassed, so only a proxy (Django's
    # SimpleLazyObject of True) passes for one, and it would be kept as itself. It isn't JSON.
    if type(value) is bool:
        return value
    if isinstance(value, str):
        return shape_string(value, limit, state)
    if isinstance(value, (int, float)):
        held = js_number(value)
        return held if held is not None and is_finite(held) else NOT_JSON
    if isinstance(value, BINARY_TYPES):
        # Binary data isn't JSON, as TypeScript drops a Buffer or a typed array (shape.ts).
        return NOT_JSON
    if id(value) in ancestors:
        return NOT_JSON

    kind = type(value)
    replaced = UNCHANGED if kind in _PLAIN_CONTAINERS else stand_in(value)
    if replaced is not UNCHANGED:
        # A chain of stand-ins (root models of root models) is bounded as JSON's nesting is.
        if replaced is NOT_JSON or len(ancestors) >= MAX_DEPTH:
            return NOT_JSON
        # Like toJSON in JavaScript: read in its place, at the same level.
        ancestors.add(id(value))
        try:
            return _convert(replaced, limit, state, ancestors, level)
        finally:
            ancestors.discard(id(value))

    is_list = isinstance(value, (list, tuple))
    source = None if is_list else as_object(value)
    if not is_list and source is None:
        return NOT_JSON
    if level >= MAX_LEVELS:
        state.shortened = True
        return TooDeep()

    ancestors.add(id(value))
    try:
        if is_list:
            assert isinstance(value, (list, tuple))
            items: list[Shaped] = []
            if len(value) > MAX_ENTRIES:
                state.shortened = True
            scan = state.scan
            for index in range(min(len(value), MAX_ENTRIES)):
                # Each slot costs one (`spend`, inlined).
                if scan.remaining < 1:
                    scan.remaining = 0
                    state.shortened = True
                    break
                scan.remaining -= 1
                item = value[index]
                if type(item) is int and -MAX_SAFE_INTEGER <= item <= MAX_SAFE_INTEGER:
                    # What `_convert` makes of it.
                    items.append(item)
                    continue
                converted = _convert(item, limit, state, ancestors, level + 1)
                if converted is NOT_JSON:
                    state.lossy = True
                    items.append(None)
                else:
                    items.append(converted)
            return items
        assert source is not None
        return _convert_object(source, limit, state, ancestors, level)
    finally:
        ancestors.discard(id(value))


def _convert_object(source: Mapping[Any, Any], limit: int, state: ShapeState, ancestors: set[int], level: int) -> Shaped | NotJson:
    try:
        keys = object_keys(source, state.keys)
    except Exception:
        return NOT_JSON
    if keys is None:
        # More keys than this event may enumerate: left out, and said so.
        state.keys = 0
        state.too_large = True
        return NOT_JSON
    state.keys -= keys.total
    if keys.others:
        # A key JSON can't have.
        state.lossy = True
    out: dict[str, Shaped] = {}
    kept = 0
    scan = state.scan
    # Restored by `_secret_value` before it returns, so the same for every entry.
    secret_key = state.secret_key
    for raw in keys.names:
        if kept >= MAX_ENTRIES:
            state.shortened = True
            break
        # A key is read whole, never cut: it costs its length, and one that doesn't fit ends the
        # object. Charged before its value is read, so no getter runs past the budget.
        if type(raw) is str and raw.isascii():
            # Its code points are its length, joined as it is (`_key_cost`).
            key: str | None = raw
            cost = len(raw)
        else:
            cost, key = _key_cost(text(raw), scan.remaining)
        if not spend(state, 1 + cost):
            break
        try:
            item = source.get(raw, MISSING)
        except Exception:
            state.lossy = True
            continue
        if item is MISSING:
            continue
        assert key is not None
        if key == "__proto__":
            state.lossy = True
            continue
        hidden = secret_key is not None and secret_key(key)
        if not hidden and type(item) is int and -MAX_SAFE_INTEGER <= item <= MAX_SAFE_INTEGER:
            # What `_convert` makes of it.
            out[key] = item
            kept += 1
            continue
        converted = (
            _secret_value(item, limit, state, ancestors, level + 1) if hidden else _convert(item, limit, state, ancestors, level + 1)
        )
        if converted is NOT_JSON:
            state.lossy = True
            continue
        out[key] = converted
        kept += 1
    return out


__all__ = [
    "BINARY_HEAD",
    "BINARY_MIN",
    "BINARY_TAIL",
    "CUT_MARK",
    "LIMITS",
    "LOOKAHEAD",
    "MARKER",
    "MAX_ENTRIES",
    "MAX_KEYS",
    "MAX_LENGTH_READS",
    "MAX_LEVELS",
    "NOT_JSON",
    "SCAN_BUDGET",
    "Binary",
    "Cut",
    "Scan",
    "Secret",
    "ShapeState",
    "Shaped",
    "TooDeep",
    "advance",
    "binary_size",
    "decoded_size",
    "is_marked",
    "js_length",
    "new_state",
    "plain",
    "secret_string",
    "shape_json",
    "shape_string",
    "spend",
]
