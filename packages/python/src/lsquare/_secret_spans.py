"""Finds the values under secret key names in JSON held in a string, as secret-spans.ts does: a text
block, an error quoting a response, a stringified argument. They are found by scanning, not by
parsing the string: it may be cut, or be JSON inside prose or a code fence, and rewriting it would
change what isn't a secret. Only the spans of the values are found; everything else stays as it was.

Its work is linear: each piece of text between two unescaped quotes is read a fixed number of times
per level of nesting, and nesting stops at `_MAX_NESTING`. It runs on UTF-16 code units (`_values.to_units`), as
JavaScript does, so both find the same spans.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Final, NamedTuple

from ._values import to_units

# How many times a string may hold JSON in a string. Past this, a string still holding a key is
# replaced whole.
_MAX_NESTING: Final = 4
_WHOLE_PLACEHOLDER: Final = re.compile(r"\[(?:EMAIL|PHONE|CARD|SECRET|IP)_[0-9]{1,9}\]")
# The longest whole placeholder: `[SECRET_` + 9 digits + `]`.
_LONGEST_PLACEHOLDER: Final = 18
_MAX_CODE_POINT: Final = 0x10FFFF
_HEX2: Final = re.compile(r"[0-9A-Fa-f]{2}")
_HEX4: Final = re.compile(r"[0-9A-Fa-f]{4}")
_HEX8: Final = re.compile(r"[0-9A-Fa-f]{8}")
_JSON_NUMBER: Final = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_JSON_STRING: Final = re.compile(r'''"[^"\\\x00-\x1f]*(?:\\(?:["\\/bfnrt]|u[0-9A-Fa-f]{4})[^"\\\x00-\x1f]*)*"''')
# What text ending in isn't a key, unless it starts an entry of an object: it is code or a sentence
# before a string, as in `token == ":"` or `createToken(":", a)`.
_NOT_A_KEY_END: Final = " \t=([+?,.<|&"
# What a sentence or code can have after a closing quote, before a space or a line's end.
_SENTENCE_ENDS: Final = ".;)'`"
# A name is matched by its end, so only this much of a long key is read.
_KEY_READ: Final = 256
_LITERALS: Final = ("true", "false", "null")
_QUOTE_OR_BACKSLASH: Final = re.compile(r'["\\]')
_STRUCTURE: Final = re.compile(r'["{}\[\]]')
_BARE_END: Final = re.compile(r"[\n\r,}\]]")
_NOT_SPACE: Final = re.compile(r"[^ \t\n\r]")
_SPACE: Final = " \t\n\r"
_BARE_ENDS: Final = "\n\r,}]"
_NOT_BLANK: Final = re.compile(r"[^ \t]")
# A key at the nesting limit: a quote, whitespace and a colon, each as it is or encoded any number of
# times.
_ENCODED_KEY: Final = re.compile(r'(?:"|\\u0022)(?:[ \t\r\n]|\\+[nrt]|\\+u00(?:20|09|0[aAdD]))*(?::|\\+u003[aA])')
_FOLLOWS_COMMA: Final = '"{[}]-0123456789'
_LINE_END: Final = re.compile(r"[\n\r]")
# JSON's escapes, then those printed text and YAML have that JSON doesn't, besides those that take digits.
_ESCAPED: Final[dict[str, str]] = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    " ": " ",
    "0": "\0",
    "a": "\x07",
    "e": "\x1b",
    "v": "\v",
    "N": "\x85",
    "_": "\xa0",
    "L": "\u2028",
    "P": "\u2029",
}
_CLOSER: Final[dict[str, str]] = {"{": "}", "[": "]"}
_OPENER: Final[dict[str, str]] = {"}": "{", "]": "["}


# How much scanning one event may do: one for each quote, backslash or bracket read, each JSON value
# checked, and each 16 characters decoded. So a crafted string can't hold up the host app, where
# ordinary text never comes near it. Past this, a string holding a quote and a colon is replaced
# whole, as nothing unread may be sent.
SCAN_WORK: Final = 1_000_000


class ScanWork:
    """What's left of `SCAN_WORK` for an event."""

    __slots__ = ("left",)

    def __init__(self) -> None:
        self.left: int = SCAN_WORK


class _OverWork(Exception):
    pass


class SecretSpan(NamedTuple):
    """A value to replace. `start` and `end` are positions in the string given. `identity` is what
    makes it "the same value" for numbering; None: it couldn't be read whole, so it is numbered on
    its own. `quote` is what goes either side of its placeholder: nothing inside a string's quotes,
    otherwise a quote at its level of nesting."""

    start: int
    end: int
    identity: str | None
    quote: str


class _View(NamedTuple):
    """A string to scan, and where each of its positions is in the string given. That string itself
    has no map."""

    text: str
    map: list[int] | None


class _Reading:
    """What one call of `secret_spans` shares."""

    __slots__ = ("is_secret_key", "spans", "work")

    def __init__(self, is_secret_key: Callable[[str], bool], work: ScanWork) -> None:
        self.is_secret_key = is_secret_key
        self.spans: list[SecretSpan] = []
        self.work = work


def may_hold_key(value: str) -> bool:
    """Whether a string could hold a key and a value at all: the cheap check before scanning it. It
    needs a quote and a colon, as they are or encoded any number of times."""
    return ('"' in value or "u0022" in value) and (":" in value or "u003a" in value or "u003A" in value)


def secret_spans(value: str, is_secret_key: Callable[[str], bool], work: ScanWork) -> list[SecretSpan]:
    """The values under secret key names in `value`, in order and not overlapping. `value` is in
    UTF-16 code units."""
    reading = _Reading(is_secret_key, work)
    try:
        _read(_View(value, None), 0, False, reading)
    except _OverWork:
        return [SecretSpan(0, len(value), None, "")]
    return _merged(reading.spans)


def _spend(work: ScanWork, amount: int = 1) -> None:
    work.left -= amount
    if work.left < 0:
        raise _OverWork


def _read(view: _View, depth: int, printed: bool, reading: _Reading) -> None:
    """Python's repr and Node's util.inspect double JSON's backslashes and leave its quotes, so `\\\\"`
    reads as the end of a string. Decoded once, it's JSON again. So a string that isn't JSON and holds
    a backslash is read a second time decoded, once for each chain of nesting, which keeps the work
    linear. JSON as it is doesn't need that, and could come out wrong: its `\\\\"` does end a string."""
    _scan(view, depth, printed, reading)
    if printed or depth == _MAX_NESTING or "\\" not in view.text or is_valid_json(view.text, reading.work):
        return
    decoded = _decode(view, 0, len(view.text), reading.work)
    if len(decoded.text) < len(view.text):
        _read(decoded, depth + 1, True, reading)


def is_valid_json(text: str, work: ScanWork) -> bool:
    """Whether the text is one JSON value. Checked here, not by `json`, so that the two languages agree
    at any depth, and deep nesting can't run out of stack."""
    opened: list[str] = []
    index: int | None = _skip_space(text, 0)
    while True:
        assert index is not None
        _spend(work)
        char = text[index : index + 1]
        end: int | None
        if char and char in _CLOSER:
            inner = _skip_space(text, index + 1)
            if text[inner : inner + 1] != _CLOSER[char]:
                opened.append(char)
                index = _json_key_end(text, inner) if char == "{" else inner
                if index is None:
                    return False
                continue
            end = inner + 1
        else:
            end = _json_scalar_end(text, index)
        if end is None:
            return False
        index = _skip_space(text, end)
        while opened and text[index : index + 1] == _CLOSER[opened[-1]]:
            opened.pop()
            index = _skip_space(text, index + 1)
        if not opened:
            return index == len(text)
        if text[index : index + 1] != ",":
            return False
        index = _skip_space(text, index + 1)
        if opened[-1] == "{":
            index = _json_key_end(text, index)
        if index is None:
            return False


def _json_key_end(text: str, at: int) -> int | None:
    """Where the value after the key at `at` starts."""
    found = _JSON_STRING.match(text, at)
    if found is None:
        return None
    colon = _skip_space(text, found.end())
    return _skip_space(text, colon + 1) if text[colon : colon + 1] == ":" else None


def _json_scalar_end(text: str, at: int) -> int | None:
    found = _JSON_STRING.match(text, at)
    if found is not None:
        return found.end()
    for literal in _LITERALS:
        if text.startswith(literal, at):
            return at + len(literal)
    end = _json_number_end(text, at)
    return end if end > at else None


def _json_number_end(text: str, at: int) -> int:
    found = _JSON_NUMBER.match(text, at)
    return at if found is None else found.end()


def _merged(spans: list[SecretSpan]) -> list[SecretSpan]:
    spans.sort(key=lambda span: (span.start, -span.end))
    out: list[SecretSpan] = []
    for span in spans:
        if not out or span.start >= out[-1].end:
            out.append(span)
        elif span.end > out[-1].end:
            out[-1] = SecretSpan(out[-1].start, span.end, None, out[-1].quote)
    return out


def _scan(view: _View, depth: int, printed: bool, reading: _Reading) -> None:
    """Every piece of text before an unescaped quote is a candidate, whether it really is a string or
    lies between two of them. A key is one that a colon follows. So a stray quote in prose can't put
    the scan out of step, and a value holding quotes needs no special case."""
    text = view.text
    start = 0
    while True:
        _spend(reading.work)
        end, escaped = _lex(text, start)
        # A key is read too: hiding a value in JSON used as a key is worth two such keys coming out the same.
        if escaped:
            _nest(view, start, end, depth, printed, reading)
        if end >= len(text):
            return
        colon = _skip_space(text, end + 1)
        if text[colon : colon + 1] == ":" and _is_key(view, start, end, colon, escaped, reading):
            resume = _read_value(view, _skip_space(text, colon + 1), depth - 1 if printed else depth, _starts_entry(text, start), reading)
            if resume is None:
                return
            start = resume
        else:
            start = end + 1


def _is_key(view: _View, start: int, end: int, colon: int, escaped: bool, reading: _Reading) -> bool:
    """A key with a secret key name. What's before a quote can be code or a sentence, as in
    `cond ? "token" : "x"`, but not where an object has a key: there, JSON's keys are read as they
    are."""
    key = _body(view, start, end, escaped, reading.work)
    if not reading.is_secret_key(key[-_KEY_READ:]):
        return False
    text = view.text
    before = _before_key(text, start)
    char = text[before] if before >= 0 else ""
    if char == "?" and (before == start - 2 or colon > end + 1):
        return False
    if text[end - 1 : end] not in _NOT_A_KEY_END or char in ("{", ",", "\n", "\r"):
        return True
    return before < 0 and start > 0 and text[start - 1] == '"'


def _starts_entry(text: str, start: int) -> bool:
    """Whether a value that isn't a quoted string can follow this key. Not when the key comes straight
    after a word or an operator, where it is code or a sentence quoting something: `case "token":
    return x`, `x == "token": y`, `"users"."token"::text`. A line number, a timestamp or a colour code
    isn't a word: a word is two letters."""
    index = _before_key(text, start)
    char = text[index] if index >= 0 else ""
    previous = text[index - 1] if index >= 1 else ""
    if char == "=" or (_is_letter(char) and _is_letter(previous)):
        return False
    # `-> "Token":`, not a quoted line's `> "token":`.
    if char == ">":
        return previous not in ("-", "=")
    # `Failed. "token":` and `"users"."token":`, not a list's `1. "token":` or an elided `..."token":`.
    return char != "." or not (_is_letter(previous) or previous == '"')


def _before_key(text: str, start: int) -> int:
    """Where what's before the quote opening the piece at `start` is, past its escapes, spaces and
    tabs. -1: nothing is, or no quote opens it, as in a string cut in its key."""
    if start == 0 or text[start - 1] != '"':
        return -1
    index = start - 2
    while index >= 0 and text[index] in " \t\\":
        index -= 1
    return index


def _nest(view: _View, start: int, end: int, depth: int, printed: bool, reading: _Reading) -> None:
    """JSON in a string: its quotes, colons and spaces are all encoded, so it is scanned decoded."""
    inner = _decode(view, start, end, reading.work)
    # With nothing decoded, it holds no more than it shows: a Windows path, a regular expression.
    if len(inner.text) == end - start or not may_hold_key(inner.text):
        return
    if depth < _MAX_NESTING:
        _read(inner, depth + 1, printed, reading)
    elif _ENCODED_KEY.search(inner.text) is not None:
        reading.spans.append(SecretSpan(_position(view, start), _position(view, end), None, ""))


def _read_value(view: _View, at: int, level: int, entry: bool, reading: _Reading) -> int | None:
    """Adds the span of the value at `at`, if it can hold a secret. Where scanning goes on; None: it
    ran to the end of the text."""
    text = view.text
    spans = reading.spans
    work = reading.work
    if at >= len(text):
        return at
    first = text[at]
    if first == '"':
        # Replaced already, whatever follows it: a hook's event is read a second time.
        whole = _lex(text, at + 1)[0]
        line = _LINE_END.search(text, at + 1, whole)
        if line is not None:
            whole = line.start()
        if _is_placeholder(view, at + 1, whole):
            return whole + 1 if whole < len(text) else None
        end, escaped = _value_end(text, at + 1, False, work)
        closed = end < len(text)
        if end > at + 1:
            identity = _body(view, at + 1, end, escaped, work) if closed else None
            spans.append(SecretSpan(_position(view, at + 1), _position(view, end), identity, ""))
        return end + 1 if closed else None
    # Only a quoted string is a value wherever its key is: not `Post "https://x/token": timeout`, or
    # `case "token": return x`.
    if not entry:
        return at
    quote = "\\" * (2**level - 1) + '"'
    if first in _CLOSER:
        close = _container_end(text, at, work)
        if close is None:
            spans.append(SecretSpan(_position(view, at), _position(view, len(text)), None, quote))
            return None
        if _ends_token(text, close):
            if _skip_space(text, at + 1) == close - 1:
                return close
            spans.append(SecretSpan(_position(view, at), _position(view, close), text[at:close], quote))
            return close
        return _bare_value(view, at, close, quote, spans)
    for literal in _LITERALS:
        if text.startswith(literal, at) and _ends_token(text, at + len(literal)):
            return at + len(literal)
    number = _json_number_end(text, at)
    if number > at and _ends_token(text, number):
        spans.append(SecretSpan(_position(view, at), _position(view, number), text[at:number], quote))
        return number
    return _bare_value(view, at, at, quote, spans)


def _bare_value(view: _View, at: int, start: int, quote: str, spans: list[SecretSpan]) -> int:
    """A value that isn't JSON, from `at`, read on from `start` up to the end of its line or the next
    comma or closing bracket."""
    text = view.text
    found = _BARE_END.search(text, start)
    resume = len(text) if found is None else found.start()
    end = resume
    while end > at and text[end - 1] in _SPACE:
        end -= 1
    if end > at:
        spans.append(SecretSpan(_position(view, at), _position(view, end), text[at:end], quote))
    return resume


def _value_end(text: str, start: int, in_container: bool, work: ScanWork) -> tuple[int, bool]:
    """Where a string value ends: at an unescaped quote that something able to follow a string follows.
    So a value with a quote left unescaped in it, as hand-built JSON has, is read whole, up to a line
    that starts as JSON's do."""
    escaped = False
    index = start
    while True:
        _spend(work)
        end, more = _lex(text, index)
        escaped = escaped or more
        line = _entry_line_end(text, index, end) if index > start else None
        if line is not None:
            return line, escaped
        if end >= len(text):
            return len(text), escaped
        if _ends_string(text, end + 1, in_container):
            return end, escaped
        index = end + 1


def _entry_line_end(text: str, start: int, end: int) -> int | None:
    """The end of the first line in the range whose next line starts with a quote or a closing
    bracket."""
    index = start
    while True:
        found = _LINE_END.search(text, index, end)
        if found is None:
            return None
        index = _skip_space(text, found.start() + 1)
        if text[index : index + 1] in ('"', "}", "]"):
            return found.start()


def _ends_token(text: str, at: int) -> bool:
    """Whether what's at `at`, after spaces and tabs, can follow a number, a literal or a container."""
    found = _NOT_BLANK.search(text, at)
    return found is None or text[found.start()] in _BARE_ENDS or _is_line_end(text, found.start())


def _ends_string(text: str, at: int, in_container: bool) -> bool:
    """Whether what's at `at`, after spaces and tabs, can follow a string: the text's or a line's end,
    a comma that a value or a line's end follows, a closing bracket with no word straight after it,
    what ends a sentence, or inside a container, a key's colon."""
    index = _skip_blank(text, at)
    if index >= len(text) or _is_line_end(text, index):
        return True
    char = text[index]
    if char == ",":
        return _follows_comma(text, _skip_blank(text, index + 1))
    if char in "}]":
        return not _is_word(text[index + 1 : index + 2])
    return (in_container and char == ":") or _ends_sentence(text, index)


def _ends_sentence(text: str, at: int) -> bool:
    index = at
    while index < len(text) and text[index] in _SENTENCE_ENDS:
        index += 1
    return index > at and (index >= len(text) or text[index] in _SPACE or _is_line_end(text, index))


def _follows_comma(text: str, at: int) -> bool:
    if at >= len(text) or _is_line_end(text, at) or text[at] in _FOLLOWS_COMMA:
        return True
    return any(text.startswith(literal, at) for literal in _LITERALS)


def _is_line_end(text: str, at: int) -> bool:
    """A line's end, written as it is or as printed JSON has it."""
    char = text[at]
    if char in "\n\r":
        return True
    return char == "\\" and text[at + 1 : at + 2] in ("n", "r")


def _is_letter(char: str) -> bool:
    return char.isascii() and char.isalpha()


def _is_word(char: str) -> bool:
    return char.isascii() and char.isalnum()


def _container_end(text: str, at: int, work: ScanWork) -> int | None:
    """Where a container ends, past its closing bracket. None: its brackets don't match, or it never
    closes."""
    opened: list[str] = []
    index = at
    while True:
        found = _STRUCTURE.search(text, index)
        if found is None:
            return None
        index = found.start()
        char = text[index]
        if char == '"':
            index = _value_end(text, index + 1, True, work)[0] + 1
            continue
        _spend(work)
        if char in _CLOSER:
            opened.append(char)
        else:
            if not opened or opened.pop() != _OPENER[char]:
                return None
            if not opened:
                return index + 1
        index += 1


def _lex(text: str, start: int) -> tuple[int, bool]:
    """The next unescaped quote from `start`, or the end of the text, and whether a backslash came
    before it."""
    escaped = False
    index = start
    while True:
        found = _QUOTE_OR_BACKSLASH.search(text, index)
        if found is None:
            return len(text), escaped
        index = found.start()
        if text[index] == '"':
            return index, escaped
        escaped = True
        index += 2


def _body(view: _View, start: int, end: int, escaped: bool, work: ScanWork) -> str:
    return _decode(view, start, end, work).text if escaped else view.text[start:end]


def _decode(view: _View, start: int, end: int, work: ScanWork) -> _View:
    """Escapes decoded, keeping where each character came from: JSON's, and those printed text and
    YAML have, including YAML's folded lines. Anything else stays as it is, so a truncated escape or a
    Windows path does no harm. JSON itself holds none of the others."""
    text = view.text
    out: list[str] = []
    positions: list[int] = []
    index = start
    _spend(work, 1 + ((end - start) >> 4))
    while index < end:
        found = text.find("\\", index, end)
        stop = end if found == -1 else found
        if stop > index:
            out.append(text[index:stop])
            positions.extend(range(index, stop) if view.map is None else view.map[index:stop])
        if found == -1:
            break
        _spend(work)
        origin = _position(view, found)
        following = text[found + 1] if found + 1 < end else ""
        chars = _ESCAPED.get(following)
        index = found + 2
        if chars is not None:
            pass
        elif following == "u" and found + 6 <= end and _HEX4.fullmatch(text, found + 2, found + 6) is not None:
            chars = chr(int(text[found + 2 : found + 6], 16))
            index = found + 6
        elif following == "x" and found + 4 <= end and _HEX2.fullmatch(text, found + 2, found + 4) is not None:
            chars = chr(int(text[found + 2 : found + 4], 16))
            index = found + 4
        elif (
            following == "U"
            and found + 10 <= end
            and _HEX8.fullmatch(text, found + 2, found + 10) is not None
            and int(text[found + 2 : found + 10], 16) <= _MAX_CODE_POINT
        ):
            chars = to_units(chr(int(text[found + 2 : found + 10], 16)))
            index = found + 10
        elif following in ("\n", "\r"):
            chars = ""
            index = _fold_end(text, found + 1, end)
        else:
            chars = "\\"
            index = found + 1
        out.append(chars)
        positions.extend([origin] * len(chars))
    positions.append(_position(view, end))
    return _View("".join(out), positions)


def _fold_end(text: str, at: int, end: int) -> int:
    """Past the line's end at `at` and the next line's indentation."""
    index = at + (2 if text[at] == "\r" and at + 1 < end and text[at + 1] == "\n" else 1)
    while index < end and text[index] in " \t":
        index += 1
    return index


def _is_placeholder(view: _View, start: int, end: int) -> bool:
    """Spelled out as a placeholder in the string given, so that `reserve` has seen its number."""
    if end - start > _LONGEST_PLACEHOLDER or _position(view, end) - _position(view, start) != end - start:
        return False
    return _WHOLE_PLACEHOLDER.fullmatch(view.text, start, end) is not None


def _position(view: _View, index: int) -> int:
    return index if view.map is None else view.map[index]


def _skip_space(text: str, start: int) -> int:
    found = _NOT_SPACE.search(text, start)
    return len(text) if found is None else found.start()


def _skip_blank(text: str, start: int) -> int:
    found = _NOT_BLANK.search(text, start)
    return len(text) if found is None else found.start()
