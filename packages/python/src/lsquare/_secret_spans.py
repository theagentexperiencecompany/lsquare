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
# What _container_end() gives for a container with a string in other quotes: it's not JSON.
_NOT_JSON: Final = -1
_DIGITS: Final = "0123456789"
_QUOTE_OR_BACKSLASH: Final = re.compile(r'["\\]')
_STRUCTURE: Final = re.compile(r'''['"`{}\[\]]''')
_BARE_END: Final = re.compile(r"[\n\r,}\]]")
_NOT_SPACE: Final = re.compile(r"[^ \t\n\r]")
_SPACE: Final = " \t\n\r"
_BARE_ENDS: Final = "\n\r,}]"
_NOT_BLANK: Final = re.compile(r"[^ \t]")
# A key at the nesting limit: a quote, whitespace and a colon, each as it is or encoded any number of
# times.
_ENCODED_KEY: Final = re.compile(r'(?:"|\\u0022)(?:[ \t\r\n]|\\+[nrt]|\\+u00(?:20|09|0[aAdD]))*(?::|\\+u003[aA])')
_FOLLOWS_COMMA: Final = '"\'`{[}]-0123456789'
# Parameters that hold secrets only in a URL: a signed URL's, and an OAuth or Azure Functions `code`.
_URL_ONLY_NAMES: Final = re.compile(r"(?:^|[^A-Za-z0-9])(?:sig|signature|hmac)\Z|^(?:code|code_verifier)\Z", re.IGNORECASE | re.ASCII)
_QUERY_NAME: Final = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-.~%+[]")
# What a query value stops at: where a URL ends in text, or the next parameter.
_QUERY_VALUE_END: Final = re.compile(r"""[ \t\r\n\f\v&#"'<>`)\]}\\]""")
# A query value that is a template, not a value, up to where a value ends: `{token}`, `{{token}}`, `$TOKEN`, `${TOKEN}`.
_QUERY_TEMPLATE: Final = re.compile(r"\{[A-Za-z0-9_.-]+\}|\{\{ ?[A-Za-z0-9_.-]+ ?\}\}|\$\{[A-Za-z0-9_.-]+\}|\$[A-Z][A-Z0-9_]*")
_INPUT_VALUE: Final = "input_value="
# Where pydantic's errors have it: straight after the error's type, which a custom error can spell any way.
_PYDANTIC_VALUE: Final = re.compile(r"\[type=[^\r\n]{1,128}?, input_value=")
_INPUT_TYPE: Final = ", input_type="
# What's after `, input_type=` where it does end a value: the type's name and the bracket that closes the error's.
_INPUT_TYPE_END: Final = re.compile(r"""[^ \t\r\n'"=]*\]""")
# How far past its start a value printed by pydantic is looked for its end: it cuts long ones to well under this.
_INPUT_READ: Final = 1024
# A line's end, as text printed more than once has it: any number of backslashes, then `n` or `r`.
_WRITTEN_LINE_END: Final = re.compile(r"(?<!\\)\\+[nr]")
_PEM_START: Final = "-----BEGIN "
# A private key block, or one the string's end cuts.
_PEM_BLOCK: Final = re.compile(r"-----BEGIN [A-Z ]{0,32}PRIVATE KEY-----(?:[^-]|-(?!----))*?(?:-----END [A-Z ]{0,32}PRIVATE KEY-----|(?:-----[A-Z ]{0,48}-{0,4})?\Z)")
# How many times a URL held in a URL is read: a redirect chain encodes it again each time.
_MAX_PERCENT: Final = 3
_PERCENT_EQUALS: Final = re.compile(r"%(?:25)*3[Dd]")
_PERCENT_BYTE: Final = re.compile(r"%([0-9A-Fa-f]{2})")
# How far past a closing quote a run of what ends a sentence is read.
_SENTENCE_READ: Final = 5
# How far past a Python string's first quote a quote that can end it is looked for: each string reads
# this at most, so the work stays linear.
_QUOTE_READ: Final = 256
_BARE_WORD_END: Final = re.compile(r"[\n\r,}\] \t]")
_CONTAINER_NAME: Final = re.compile(r"""[^ \t\r\n'"`,{}()\[\]]+""")
_PYTHON_STRUCTURE: Final = re.compile(r"""['"`{}()\[\]]""")
_PYTHON_LITERALS: Final = ("None", "True", "False", "true", "false", "null")
# util.inspect's mark on a container something inside it refers back to.
_INSPECT_REF: Final = re.compile(r"<ref \*[0-9]{1,9}> ")
_STRING_STOP: Final[dict[str, re.Pattern[str]]] = {
    "'": re.compile(r"['\\\n\r]"),
    '"': re.compile(r'["\\\n\r]'),
    "`": re.compile(r"[`\\\n\r]"),
}
_LINE_END: Final = re.compile(r"[\n\r]")
# JSON's escapes, then those printed text and YAML have that JSON doesn't, besides those that take digits.
_ESCAPED: Final[dict[str, str]] = {
    '"': '"',
    "'": "'",
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


# How much scanning one event may do. A unit is roughly one quote or bracket read, one backslash or
# percent sign decoded, one JSON value checked, one key looked at, or 16 characters decoded or of a parameter's name. So a crafted string
# can't hold up the host app for long, where ordinary text doesn't come near it. Past this, a string
# `may_hold_key` lets through is replaced whole, as nothing unread may be sent.
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

    __slots__ = ("is_secret_key", "lines_in", "spans", "work")

    def __init__(self, is_secret_key: Callable[[str], bool], work: ScanWork) -> None:
        self.is_secret_key = is_secret_key
        self.spans: list[SecretSpan] = []
        self.work = work
        # A decoded view where a container didn't close with its line ends as escapes: the rest are
        # read with them as line ends.
        self.lines_in: _View | None = None


def may_hold_key(value: str) -> bool:
    """Whether a string could hold a secret value at all: the cheap check before scanning it. It
    needs a quote and a colon or `=>`, as they are or encoded any number of times; or an `=`, as it is or
    percent-encoded, and what can start a URL's parameter; or pydantic's `input_value=`."""
    if ('"' in value or "'" in value or "u0022" in value) and (":" in value or "=>" in value or "u003a" in value or "u003A" in value):
        return True
    if "%" in value and _PERCENT_EQUALS.search(value) is not None:
        return True
    return "=" in value and ("?" in value or "&" in value or "#" in value or _INPUT_VALUE in value)


def secret_spans(value: str, is_secret_key: Callable[[str], bool], work: ScanWork) -> list[SecretSpan]:
    """The values under secret key names in `value`, in order and not overlapping. `value` is in
    UTF-16 code units."""
    reading = _Reading(is_secret_key, work)
    try:
        _read(_View(value, None), 0, False, reading)
    except _OverWork:
        return [SecretSpan(0, len(value), None, "")]
    return _with_whole_keys(value, _merged(reading.spans))


def _with_whole_keys(text: str, spans: list[SecretSpan]) -> list[SecretSpan]:
    """A value can stop at a private key block's first line, which would leave the block without the
    BEGIN its pattern looks for. So a block that a value runs into goes whole."""
    if not spans or _PEM_START not in text:
        return spans
    out = list(spans)
    index = 0
    for block in _PEM_BLOCK.finditer(text):
        while index < len(spans) and spans[index].end <= block.start():
            index += 1
        if index < len(spans) and spans[index].start < block.end():
            out.append(SecretSpan(block.start(), block.end(), None, ""))
    return spans if len(out) == len(spans) else _merged(out)


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
    _scan_single_quoted(view, reading)
    _scan_input_values(view, reading)
    _scan_queries(view, reading)
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
        arrow = text.startswith("=>", colon)
        if (arrow or (text[colon : colon + 1] == ":" and text[colon + 1 : colon + 2] != ":")) and _is_key(view, start, end, colon, escaped, reading):
            resume = _read_value(view, _skip_space(text, colon + (2 if arrow else 1)), depth - 1 if printed else depth, _starts_entry(text, start), reading)
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


def _scan_single_quoted(view: _View, reading: _Reading) -> None:
    """Python's repr of a dict, which is how pydantic's errors quote an input, and other text with
    single-quoted keys: `{'password': 'x', 'note': "it's"}`, and `Map { 'password' => 'x' }`. Any two
    quotes in a row can hold a key, whatever is between them, so an apostrophe can't put the scan out
    of step. Only one that opens an entry is a key, so a sentence's `the 'token': 'x'` isn't, and a
    SQL cast's `'token'::text` isn't either."""
    text = view.text
    start = text.find("'")
    while start != -1:
        close = text.find("'", start + 1)
        if close == -1:
            return
        after = _skip_blank(text, close + 1)
        arrow = text.startswith("=>", after)
        if (arrow or (text[after : after + 1] == ":" and text[after + 1 : after + 2] != ":")) and _opens_entry(text, start):
            _spend(reading.work)
            if reading.is_secret_key(text[max(start + 1, close - _KEY_READ) : close]):
                resume = _read_python_value(view, _skip_space(text, after + (2 if arrow else 1)), reading)
                if resume is None:
                    return
                start = text.find("'", resume)
                continue
        start = close


def _opens_entry(text: str, start: int) -> bool:
    """Whether the quote at `start`, or the `b` or `u` before it, comes straight after `{`, `(` or
    `,`, or at the start of a line or the text."""
    index = start - 1
    if index >= 0 and text[index] in "bu":
        index -= 1
    while index >= 0 and text[index] in " \t":
        index -= 1
    return index < 0 or text[index] in "{(,\n\r"


def _python_quote_at(text: str, at: int) -> int | None:
    """Where a string in Python's quotes starts at or after a `b` or `u` at `at`, or None: there is
    no such string."""
    quote_at = at + 1 if text[at : at + 1] in ("b", "u") else at
    return quote_at if text[quote_at : quote_at + 1] in ("'", '"', "`") else None


def _read_python_value(view: _View, at: int, reading: _Reading) -> int | None:
    """Adds the span of the value at `at`, as Python or Node's util.inspect writes one. Where scanning
    goes on; None: it ran to the end of the text."""
    text = view.text
    spans = reading.spans
    work = reading.work
    if at >= len(text):
        return at
    quote_at = _python_quote_at(text, at)
    if quote_at is not None:
        return _read_python_string(view, quote_at, reading)
    # An escaped quote is read decoded, where it is a string's: `\\\\"it's\\\\"`, in JSON holding a repr.
    if text[at] == "\\":
        return at
    bracket = _python_bracket_at(text, at)
    if bracket is not None:
        close = _python_containers_end(view, bracket, reading)
        if close is None:
            spans.append(SecretSpan(_position(view, at), _position(view, len(text)), None, "'"))
            return None
        if (bracket == at and _skip_space(text, at + 1) == close - 1) or _is_placeholder(view, at, close):
            return close
        if not _ends_token(text, close):
            return _bare_value(view, at, close, "'", spans)
        spans.append(SecretSpan(_position(view, at), _position(view, close), text[at:close], "'"))
        return close
    # What util.inspect and repr write in angle brackets: `<Buffer 68 69>` holds a value's bytes.
    if text[at] == "<":
        angle = text.find(">", at, at + _KEY_READ)
        if angle != -1 and _ends_token(text, angle + 1):
            spans.append(SecretSpan(_position(view, at), _position(view, angle + 1), text[at : angle + 1], "'"))
            return angle + 1
    for literal in _PYTHON_LITERALS:
        if text.startswith(literal, at) and _ends_token(text, at + len(literal)):
            return at + len(literal)
    number = _json_number_end(text, at)
    if number > at and _ends_token(text, number):
        spans.append(SecretSpan(_position(view, at), _position(view, number), text[at:number], "'"))
        return number
    # One word, as `{'token': token}` has. More than that is a sentence: `, 'password': explains it.`
    limit = min(at + _KEY_READ, len(text))
    found = _BARE_WORD_END.search(text, at, limit)
    end = limit if found is None else found.start()
    if end < limit and not _ends_token(text, end):
        return at
    return _bare_value(view, at, at, "'", spans) if end > at else at


def _read_python_string(view: _View, quote_at: int, reading: _Reading) -> int:
    """Adds the span of the string whose opening quote is at `quote_at`, and of what continues it:
    pprint writes a long value as adjacent strings, and util.inspect joins them with `+`. Where
    scanning goes on."""
    text = view.text
    work = reading.work
    first = text[quote_at]
    end, escaped = _python_string_end(view, quote_at + 1, first, False, work)
    last = end
    last_quote = first
    while text[last : last + 1] == last_quote:
        following = _skip_space(text, last + 1)
        if text[following : following + 1] == "+":
            following = _skip_space(text, following + 1)
        piece = _python_quote_at(text, following)
        if piece is None:
            break
        quote = text[piece]
        ended = _python_string_end(view, piece + 1, quote, True, work)[0]
        if text[ended : ended + 1] == quote and text[_skip_blank(text, ended + 1) : _skip_blank(text, ended + 1) + 1] == ":":
            break
        last = ended
        last_quote = quote
    closed = text[last : last + 1] == last_quote
    if last == end and _is_placeholder(view, quote_at + 1, end):
        return end + 1 if closed else end
    # Pieces with different quotes: their placeholder takes one kind, so that they stay balanced.
    if closed and last_quote != first:
        reading.spans.append(SecretSpan(_position(view, quote_at), _position(view, last + 1), None, "'"))
        return last + 1
    if last > quote_at + 1:
        identity = _body(view, quote_at + 1, end, escaped, work) if closed and last == end else None
        reading.spans.append(SecretSpan(_position(view, quote_at + 1), _position(view, last), identity, ""))
    return last + 1 if closed else last


def _python_string_end(view: _View, start: int, quote: str, in_container: bool, work: ScanWork, lines: bool = False) -> tuple[int, bool]:
    """Where a string in Python's quotes ends: a quote that something able to follow it follows.
    Without one soon after its first quote, on its line, that first quote, so that a sentence after it
    stays; without a quote, its line's end, which repr never has in a string. A line end that was an
    escape, in text read decoded, is one only if no quote follows, or with `lines`."""
    text = view.text
    pattern = _STRING_STOP[quote]
    escaped = False
    first_quote: int | None = None
    first_line: int | None = None
    index = start
    while True:
        found = pattern.search(text, index)
        if found is None:
            if first_quote is not None:
                return first_quote, escaped
            return (len(text) if first_line is None else first_line), escaped
        index = found.start()
        if first_quote is not None and index > first_quote + _QUOTE_READ:
            return first_quote, escaped
        char = text[index]
        if char == "\\":
            escaped = True
            index += 2
        elif char in "\n\r":
            if lines or not _from_escape(view, index):
                return (index if first_quote is None else first_quote), escaped
            if first_line is None:
                first_line = index
            index += 1
        else:
            _spend(work)
            if _ends_python_string(text, index + 1, in_container):
                return index, escaped
            if first_quote is None:
                first_quote = index
            index += 1


def _from_escape(view: _View, at: int) -> bool:
    """Whether what's at `at` in a decoded view was written as an escape."""
    return view.map is not None and _position(view, at + 1) - _position(view, at) != 1


def _ends_python_string(text: str, at: int, in_container: bool) -> bool:
    """Not a colon outside a container, which is the next key's quote in a repr pydantic cut short:
    `{'token': 'abc...i_key': b'x'}`."""
    index = _skip_blank(text, at)
    if index >= len(text) or _is_line_end(text, index) or _WRITTEN_LINE_END.match(text, index) is not None:
        return True
    char = text[index]
    if char in ",}])":
        return True
    if char == "+":
        return _python_quote_at(text, _skip_space(text, index + 1)) is not None
    # A key's end, and `<class 'list'>`.
    if in_container and (char in ":>" or text.startswith("=>", index)):
        return True
    # One run of what ends a sentence, read at most a few characters on: a long run isn't one.
    for end in range(index, index + _SENTENCE_READ):
        if end >= len(text) or text[end] in _SPACE:
            return end > index
        if text[end] not in _SENTENCE_ENDS:
            return False
    return False


def _python_bracket_at(text: str, at: int) -> int | None:
    """The bracket of the container at `at`, which may have any name: `[1, 2]`, `OrderedDict(a='b')`,
    `Foo { a: 'b' }`, `$e { a: 'b' }`, `f.<locals>.Foo(a='b')`."""
    ref = _INSPECT_REF.match(text, at)
    start = at if ref is None else ref.end()
    found = _CONTAINER_NAME.match(text, start, start + _KEY_READ)
    index = start if found is None else found.end()
    if index > start and text[index : index + 1] == " ":
        index += 1
    return index if text[index : index + 1] in ("{", "[", "(") else None


def _python_containers_end(view: _View, at: int, reading: _Reading) -> int | None:
    """Where the container whose first bracket is at `at` ends, past what's written before its
    contents: `Set(2) { 'a' }`, `Foo(2) [Map] { 'a' => 'b' }`, `[Object: null prototype] { a: 'b' }`,
    `Resp[int](a=1)`."""
    text = view.text
    close = _python_container_read(view, at, reading)
    while close is not None:
        following = close + 1 if text[close : close + 1] == " " else close
        if text[following : following + 1] not in ("{", "[", "("):
            break
        close = _python_container_read(view, following, reading)
    return close


def _python_container_read(view: _View, at: int, reading: _Reading) -> int | None:
    """A decoded view's line ends were escapes. A container that doesn't close is read again with them
    as line ends, as in text that has them as they are, and so is every later one in that view, so that
    many of them don't each read to the far end first."""
    work = reading.work
    if view.map is None:
        return _python_container_end(view, at, False, work)
    if reading.lines_in is view:
        return _python_container_end(view, at, True, work)
    close = _python_container_end(view, at, False, work)
    if close is not None:
        return close
    reading.lines_in = view
    return _python_container_end(view, at, True, work)


def _python_container_end(view: _View, at: int, lines: bool, work: ScanWork) -> int | None:
    """Where a container in Python's syntax ends, past its closing bracket. None: its brackets don't
    match, or it never closes."""
    text = view.text
    opened: list[str] = []
    index = at
    while True:
        found = _PYTHON_STRUCTURE.search(text, index)
        if found is None:
            return None
        index = found.start()
        char = text[index]
        if char in ("'", '"', "`"):
            _spend(work)
            index = _escaped_string_end(text, index, char) if char != "`" and text[index - 1 : index] == "\\" else _python_string_end(view, index + 1, char, True, work, lines)[0] + 1
            continue
        _spend(work)
        if char in "{[(":
            opened.append(char)
        else:
            if not opened or opened.pop() != {"}": "{", "]": "[", ")": "("}[char]:
                return None
            if not opened:
                return index + 1
        index += 1


def _escaped_string_end(text: str, at: int, quote: str) -> int:
    """Past a string whose opening quote has a backslash before it, as JSON holding a repr has: its
    closing quote has one too."""
    found = text.find("\\" + quote, at + 1)
    return len(text) if found == -1 else found + 2


def _scan_input_values(view: _View, reading: _Reading) -> None:
    """Pydantic's errors quote what they refused: `Input should be a valid string [type=string_type,
    input_value=123, input_type=int]`. It can be cut anywhere, list its key on another line or not at
    all, and the event's input has it anyway, so it always goes, up to `, input_type=` and the type's
    name. Without that soon after it, it was cut: up to its line's end, however that's written."""
    text = view.text
    # Each looked for only once it's behind: a search per value would read to a far one every time.
    kind = -1
    line = -1
    written = -1
    found = _PYDANTIC_VALUE.search(text)
    while found is not None:
        _spend(reading.work)
        start = found.end()
        if kind < start:
            kind = _input_type_at(text, start)
        if line < start:
            found_line = _LINE_END.search(text, start)
            line = len(text) if found_line is None else found_line.start()
        end = kind
        if kind > line or kind - start > _INPUT_READ:
            if written < start:
                found_written = _WRITTEN_LINE_END.search(text, start)
                written = len(text) if found_written is None else found_written.start()
            end = min(line, written)
        if end > start and not _is_placeholder(view, start, end):
            reading.spans.append(SecretSpan(_position(view, start), _position(view, end), _repr_text(text[start:end]), ""))
        found = _PYDANTIC_VALUE.search(text, end)


def _input_type_at(text: str, start: int) -> int:
    """The next `, input_type=` that ends a value; a value can hold those words. Past the text's end if
    none does."""
    at = text.find(_INPUT_TYPE, start)
    while at != -1:
        if _INPUT_TYPE_END.match(text, at + len(_INPUT_TYPE)) is not None:
            return at
        at = text.find(_INPUT_TYPE, at + 1)
    return len(text) + 1


def _repr_text(value: str) -> str | None:
    """What a value printed by repr holds, so a string shares its number with the same value
    elsewhere. None: pydantic cut it."""
    if "..." in value:
        return None
    first = value[0]
    return value[1:-1] if len(value) > 1 and first in ("'", '"') and value.endswith(first) else value


def _scan_queries(view: _View, reading: _Reading) -> None:
    """A URL's parameters: `?token=abc&api_key=def`, a fragment's `#access_token=abc`, and a URL held
    in a parameter, percent-encoded once or more. Their names follow the same rule as keys', and a few
    go only here: a signed URL's `sig`, an OAuth `code`."""
    current = view
    level = 0
    while True:
        _scan_parameters(current, reading)
        if level == _MAX_PERCENT or _PERCENT_EQUALS.search(current.text) is None:
            return
        current = _percent_decoded(current, reading.work)
        level += 1


def _scan_parameters(view: _View, reading: _Reading) -> None:
    text = view.text
    equals = text.find("=")
    while equals != -1:
        limit = max(0, equals - _KEY_READ)
        start = equals
        while start > limit and text[start - 1] in _QUERY_NAME:
            start -= 1
        resume = equals + 1
        if start < equals and text[start] != "]" and _starts_parameter(text, start):
            _spend(reading.work, 1 + ((equals - start) >> 4))
            name = _parameter_name(text[start:equals])
            if reading.is_secret_key(name) or _URL_ONLY_NAMES.search(name) is not None:
                resume = _read_query_value(view, resume, reading)
        equals = text.find("=", resume)


def _parameter_name(name: str) -> str:
    """A name as it was before encoding, without PHP's and Rails' `[]` at its end."""
    decoded = _PERCENT_BYTE.sub(lambda found: chr(int(found.group(1), 16)), name) if "%" in name else name
    end = len(decoded)
    while end > 0 and decoded[end - 1] == "]":
        opening = end - 2
        while opening >= 0 and decoded[opening] in _DIGITS:
            opening -= 1
        if opening < 0 or decoded[opening] != "[":
            break
        end = opening
    return decoded[:end]


def _starts_parameter(text: str, start: int) -> bool:
    """After `?`, `&`, `#`, a hash route's `#/`, `#!` or `#!/`, or `&` as HTML writes it. Not code's
    `&&`."""
    if start == 0:
        return False
    char = text[start - 1]
    if char in "?#":
        return True
    if char == "&":
        return text[start - 2 : start - 1] != "&" if start >= 2 else True
    if char == ";":
        return start >= 5 and text.startswith("&amp", start - 5)
    if char in "/!" and start >= 2:
        return text[start - 2] == "#" or (char == "/" and start >= 3 and text.startswith("#!", start - 3))
    return False


def _read_query_value(view: _View, at: int, reading: _Reading) -> int:
    """Adds the span of the parameter's value at `at`. Where scanning goes on."""
    text = view.text
    found = _QUERY_VALUE_END.search(text, at)
    end = len(text) if found is None else found.start()
    # A placeholder's bracket ends a value, so it is looked for by its own shape.
    if text.startswith("[", at):
        close = text.find("]", at, at + _LONGEST_PLACEHOLDER)
        if close != -1 and _is_placeholder(view, at, close + 1):
            return close + 1
    template = _QUERY_TEMPLATE.match(text, at, at + _KEY_READ)
    if template is not None and (template.end() >= len(text) or _QUERY_VALUE_END.match(text, template.end()) is not None):
        return template.end()
    if end > at:
        reading.spans.append(SecretSpan(_position(view, at), _position(view, end), text[at:end], ""))
    return end


def _percent_decoded(view: _View, work: ScanWork) -> _View:
    """`%XX` for an ASCII character decoded, keeping where each character came from. Anything else
    stays as it is."""
    text = view.text
    out: list[str] = []
    positions: list[int] = []
    _spend(work, 1 + (len(text) >> 4))
    index = 0
    while True:
        found = text.find("%", index)
        stop = len(text) if found == -1 else found
        if stop > index:
            out.append(text[index:stop])
            positions.extend(_position(view, at) for at in range(index, stop))
        if found == -1:
            break
        _spend(work)
        byte = int(text[found + 1 : found + 3], 16) if _HEX2.fullmatch(text, found + 1, found + 3) is not None else 0x100
        if byte < 0x80:
            out.append(chr(byte))
            index = found + 3
        else:
            out.append("%")
            index = found + 1
        positions.append(_position(view, found))
    positions.append(_position(view, len(text)))
    return _View("".join(out), positions)


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
        # Replaced already, whatever follows it: a string a hook changed is read a second time.
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
    # Quoted any other way: `"password": 'x'`, as a repr has for a key with an apostrophe.
    if _python_quote_at(text, at) is not None:
        return _read_python_value(view, at, reading)
    # Only a quoted string is a value wherever its key is: not `Post "https://x/token": timeout`, or
    # `case "token": return x`.
    if not entry:
        return at
    quote = "\\" * (2**level - 1) + '"'
    if first in _CLOSER:
        close = _container_end(text, at, work)
        if close == _NOT_JSON:
            return _read_python_value(view, at, reading)
        if close is None:
            spans.append(SecretSpan(_position(view, at), _position(view, len(text)), None, quote))
            return None
        if _is_placeholder(view, at, close):
            return close
        if _ends_token(text, close):
            if _skip_space(text, at + 1) == close - 1:
                return close
            spans.append(SecretSpan(_position(view, at), _position(view, close), text[at:close], quote))
            return close
        # What's written before a container's contents: `[Object: null prototype] { a: 'b' }`.
        contents = close + 1 if text[close : close + 1] == " " else close
        if text[contents : contents + 1] in ("{", "[", "("):
            return _read_python_value(view, at, reading)
        return _bare_value(view, at, close, quote, spans)
    # A container with a name: `Foo(a='b')`, `Set(2) { 'a' }`.
    if _python_bracket_at(text, at) is not None:
        return _read_python_value(view, at, reading)
    for literal in _PYTHON_LITERALS:
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
    what ends a sentence, or inside a container, a key's colon or arrow."""
    index = _skip_blank(text, at)
    if index >= len(text) or _is_line_end(text, index):
        return True
    char = text[index]
    if char == ",":
        return _follows_comma(text, _skip_blank(text, index + 1))
    if char in "}]":
        return not _is_word(text[index + 1 : index + 2])
    return (in_container and (char == ":" or text.startswith("=>", index))) or _ends_sentence(text, index)


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
        if char in ("'", "`"):
            return _NOT_JSON
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
