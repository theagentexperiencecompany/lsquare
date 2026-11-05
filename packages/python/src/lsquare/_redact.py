"""Redaction (shared spec, section 2), as redact.ts does it.

The patterns are TypeScript's, character for character, compiled with `re.ASCII` (so `\\d`, `\\w`
and `\\b` are ASCII, as in JavaScript without the `u` flag), and with JavaScript's `\\s` written out
as an explicit set, since Python's is a different one. They run on the text as UTF-16 code units
(`_values.to_units`), as JavaScript's do: a bounded repeat counts an emoji as two, as there.
"""

from __future__ import annotations

import hashlib
import re
from itertools import islice
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final, cast

from ._jsjson import dumps, number
from ._secret_spans import ScanWork, SecretSpan, may_hold_key, secret_spans
from ._shape import Binary, Cut, Secret, Shaped, TooDeep, is_marked, plain
from ._types import Json, PatternName
from ._values import join, to_units, utf16_length

#: JavaScript's `\s`, written out so Python matches the same characters (its own `\s` is another set).
WS: Final = "\\t\\n\\v\\f\\r \\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff"

#: TypeScript's pattern sources, as its RegExp `source` gives them.
SOURCES: Final[dict[PatternName, str]] = {
    "email": r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+(?:@|%40)[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}",
    "phone": (
        r"\+(?=[1-9](?:[ .()-]{0,2}\d){5})"
        r"(?:\d{1,3}(?:[ .-]\(?|\()(?:\d{1,4}(?:\)[ .-]?|[ .-])(?:\d{2,4}[ .-](?:\d{2,4}(?:[ .-](?:\d{2,4}(?:[ .-]\d{2,4}\b|\b)"
        r"|\d{4,8}\b)|\b)|\d{4,8}(?:[ .-]\d{2,4}\b|\b)|\d{6,12}\b)|\d{4,8}(?:[ .-](?:\d{2,4}(?:[ .-]\d{2,4}\b|\b)|\d{4,8}\b)|\b)"
        r"|\d{6,12}(?:[ .-]\d{2,4}\b|\b)|\d{8,16}\b)|\d{3,8}[ .-](?:\d{2,4}(?:[ .-](?:\d{2,4}(?:[ .-]\d{2,4}\b|\b)|\d{4,8}\b)|\b)"
        r"|\d{4,8}(?:[ .-]\d{2,4}\b|\b)|\d{6,12}\b)|\d{5,12}(?:[ .-](?:\d{2,4}(?:[ .-]\d{2,4}\b|\b)|\d{4,8}\b)|\b)"
        r"|\d{7,16}(?:[ .-]\d{2,4}\b|\b)|\d{9,20}\b)"
        r"|\d{2,7}(?:\)[ .-]?|[ .-])(?:\d{2,4}[ .-](?:\d{2,4}(?:[ .-](?:\d{2,4}(?:[ .-]\d{2,4}\b|\b)|\d{4,8}\b)|\b)"
        r"|\d{4,8}(?:[ .-]\d{2,4}\b|\b)|\d{6,12}\b)|\d{4,8}(?:[ .-](?:\d{2,4}(?:[ .-]\d{2,4}\b|\b)|\d{4,8}\b)|\b)"
        r"|\d{6,12}(?:[ .-]\d{2,4}\b|\b)|\d{8,16}\b)|\d{4,11}[ .-](?:\d{2,4}(?:[ .-](?:\d{2,4}(?:[ .-]\d{2,4}\b|\b)|\d{4,8}\b)|\b)"
        r"|\d{4,8}(?:[ .-]\d{2,4}\b|\b)|\d{6,12}\b)|\d{6,15}(?:[ .-](?:\d{2,4}(?:[ .-]\d{2,4}\b|\b)|\d{4,8}\b)|\b)"
        r"|\d{8,19}(?:[ .-]\d{2,4}\b|\b)|\d{10,23}\b)"
        r"|\(\d{3}\) ?\d{3}-\d{4}\b|\b\d{3}-\d{3}-\d{4}\b"
    ),
    "card": r"\b[3-6]\d{3}(?:[ -]?\d){11,12}\b",
    "secret": (
        r"\b(?:lsq_[A-Za-z0-9_-]{40,}|sk-[A-Za-z0-9_-]{20,}|[rs]k_(?:live|test)_[A-Za-z0-9]{16,}|whsec_[A-Za-z0-9]{24,}"
        r"|gh[opsur]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{20,}|npm_[A-Za-z0-9]{36}"
        r"|xox[abprs]-[A-Za-z0-9-]{10,}|xapp-[A-Za-z0-9-]{10,}|hooks\.slack\.com\/services\/[A-Za-z0-9\/_-]{20,}"
        r"|(?:AKIA|ASIA)[0-9A-Z]{16}|(?:aws_secret_access_key|AWS_SECRET_ACCESS_KEY)" + "[\"']?[" + WS + "]*[:=][" + WS + "]*[\"']?"
        r"[A-Za-z0-9\/+]{40}"
        r"|AIza[0-9A-Za-z_-]{35}|ya29\.[A-Za-z0-9._-]{20,}|GOCSPX-[A-Za-z0-9_-]{20,}|phx_[A-Za-z0-9]{20,})"
        r"|(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
        r"|(?:[Bb]earer|BEARER)[" + WS + r"]+(?=[A-Za-z0-9._~+/-]{0,512}\d)[A-Za-z0-9._~+/-]{16,}=*"
        r"|\b(?:Basic|BASIC)[" + WS + r"]+(?=[A-Za-z0-9+/]{0,512}[0-9+=])[A-Za-z0-9+/]{16,}={0,2}"
        r"|(?<=:\/\/)[^" + WS + r"/:@?#]{0,256}:[^" + WS + r"/@?#]{1,256}(?=@)"
        r"|-----BEGIN [A-Z ]{0,32}PRIVATE KEY-----(?:[^-]|-(?!----))*?-----END [A-Z ]{0,32}PRIVATE KEY-----"
    ),
    "ip": (
        r"\b(?:25[0-5]|2[0-4]\d|1\d\d|\d\d?)(?:\.(?:25[0-5]|2[0-4]\d|1\d\d|\d\d?)){3}\b|(?<![0-9A-Za-z:.])(?:(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}"
        r"|(?:[0-9A-Fa-f]{1,4}:){1,7}:|(?:[0-9A-Fa-f]{1,4}:){1,6}:[0-9A-Fa-f]{1,4}|(?:[0-9A-Fa-f]{1,4}:){1,5}(?::[0-9A-Fa-f]{1,4}){1,2}"
        r"|(?:[0-9A-Fa-f]{1,4}:){1,4}(?::[0-9A-Fa-f]{1,4}){1,3}|(?:[0-9A-Fa-f]{1,4}:){1,3}(?::[0-9A-Fa-f]{1,4}){1,4}"
        r"|(?:[0-9A-Fa-f]{1,4}:){1,2}(?::[0-9A-Fa-f]{1,4}){1,5}|[0-9A-Fa-f]{1,4}:(?::[0-9A-Fa-f]{1,4}){1,6}|:(?::[0-9A-Fa-f]{1,4}){1,7})"
        r"(?![0-9A-Za-z:]|\.\d)"
    ),
}

#: 0.1's patterns, with `\s` written out. Each matches in linear time (redact.ts says how). The phone
#: pattern and the IPv4 octet are 0.1's, rewritten so that adjacent digit repeats can't split one run of
#: digits in more than one way, and they match exactly what 0.1's did. packages/lsquare/scripts/phone-pattern.mjs
#: builds the phone pattern from 0.1's pieces.
PATTERNS: Final[dict[PatternName, re.Pattern[str]]] = {name: re.compile(source, re.ASCII) for name, source in SOURCES.items()}

#: 0.1's secret key names, matched by the end of a key lowercased and stripped to `a-z0-9`.
SECRET_KEYS: Final[tuple[str, ...]] = (
    "password", "passwords", "passwd", "passphrase", "secret", "secrets", "token", "auth", "authorization",
    "apikey", "apikeys", "privatekey", "secretkey", "accesskey", "signingkey", "encryptionkey",
    "credential", "credentials", "cookie", "cookies", "connectionstring",
)  # fmt: skip

#: Secrets first, so a token's digits are never read as a card or phone number.
ORDER: Final[tuple[PatternName, ...]] = ("secret", "email", "card", "phone", "ip")

LABELS: Final[dict[PatternName, str]] = {"email": "EMAIL", "phone": "PHONE", "card": "CARD", "secret": "SECRET", "ip": "IP"}
_PATTERN_BY_LABEL: Final[dict[str, PatternName]] = {label: pattern for pattern, label in LABELS.items()}
_EXISTING_PLACEHOLDER: Final = re.compile(r"\[(EMAIL|PHONE|CARD|SECRET|IP)_(\d{1,9})\]", re.ASCII)
_WHOLE_PLACEHOLDER: Final = re.compile(r"\[(?:EMAIL|PHONE|CARD|SECRET|IP)_\d{1,9}\]", re.ASCII)

#: What a secret looks like at its start, for one that runs into a cut and so can't be matched whole.
_SECRET_START: Final = re.compile(
    r"(?:lsq_|sk-|[rs]k_(?:live|test)_|whsec_|gh[opsur]_|github_pat_|glpat-|npm_|xox[abprs]-|xapp-|hooks\.slack\.com\/services\/"
    r"|AKIA|ASIA|AIza|ya29\.|GOCSPX-|phx_|eyJ)",
    re.ASCII,
)
#: A Bearer or Basic token's scheme, just before the token the cut ends.
_AUTH_SCHEME: Final = re.compile(r"(?:[Bb]earer|BEARER|Basic|BASIC)[" + WS + r"]+\Z", re.ASCII)
_PEM_BEGIN: Final = re.compile(r"-----BEGIN [A-Z ]{0,32}(?:PRIVATE KEY-----|\Z)", re.ASCII)
#: A PEM key's closing line, whole: until one follows its BEGIN line, the block runs into the cut.
_PEM_END: Final = re.compile(r"-----END [A-Z ]{0,32}PRIVATE KEY-----", re.ASCII)
#: A run of token characters, read backwards from the end of what's kept.
_TOKEN_RUN: Final = re.compile(r"[A-Za-z0-9._~+/=-]*", re.ASCII)
#: Inside a run of token characters, a secret can start after one of these, as the patterns allow.
_SECRET_BOUNDARY: Final = re.compile(r"[.~+/=]", re.ASCII)
#: Matched values longer than this (in UTF-16 units) are numbered by a hash, so a pending event doesn't keep them.
MAX_KEPT_IDENTITY: Final = 64
#: Python only: the fewest UTF-16 units each pattern matches (what `re`'s parser gives as its least
#: width, which a test checks), so a string shorter than that is never searched for it.
SHORTEST_MATCH: Final[dict[PatternName, int]] = {"email": 6, "phone": 7, "card": 15, "secret": 2, "ip": 3}
#: Card numbers have 15 or 16 digits, the first 3 to 6: a number outside these isn't one.
_CARD_LOW: Final = 3 * 10**14
_CARD_HIGH: Final = 7 * 10**15
_NOT_KEY_CHAR: Final = re.compile(r"[^a-z0-9]", re.ASCII)
_NOT_DIGIT: Final = re.compile(r"\D", re.ASCII)
_CARD_DIGITS: Final = re.compile(r"[3-6]\d{14,15}", re.ASCII)


def normalize_key(key: str) -> str:
    return _NOT_KEY_CHAR.sub("", join(key).lower())


def resolve_patterns(option: object) -> list[PatternName]:
    """The `redact` option: False turns redaction off, a mapping of pattern names to False turns
    those off. Anything else leaves every pattern on."""
    if option is False:
        return []
    settings: Mapping[Any, Any] = option if isinstance(option, Mapping) else {}
    out: list[PatternName] = []
    for pattern in ORDER:
        try:
            off = settings.get(pattern) is False
        except Exception:
            off = False
        if not off:
            out.append(pattern)
    return out


def read_keys(option: object) -> tuple[list[str], bool] | None:
    """`redact_keys`, as the names are matched (lowercased, stripped to `a-z0-9`), and whether any
    entry wasn't a key name; None when the value isn't a list of names at all. A list, or any other
    iterable of strings (a set, a dict's keys, a generator), of which the first 1,000 entries are
    read; one string is one name. A mapping isn't read, as TypeScript doesn't read an object. An
    iterable that raises raises."""
    if option is None:
        return [], False
    if isinstance(option, str):
        items: Iterable[object] = (option,)
    elif isinstance(option, (Mapping, bytes, bytearray, memoryview)) or not isinstance(option, Iterable):
        return None
    else:
        items = option
    keys: list[str] = []
    skipped = False
    for key in islice(items, 1000):
        name = normalize_key(key) if isinstance(key, str) else ""
        if name == "":
            skipped = True
        elif name not in keys:
            keys.append(name)
    return keys, skipped


def resolve_keys(option: object) -> list[str]:
    """The names `read_keys` finds; none for a value that isn't a list of names."""
    read = read_keys(option)
    return [] if read is None else read[0]


def looks_like_contact(value: str) -> bool:
    """Whether a `userId` looks like an email address or phone number, which the SDK warns about."""
    units = to_units(join(value))
    return PATTERNS["email"].search(units) is not None or PATTERNS["phone"].search(units) is not None


def luhn(value: str) -> bool:
    digits = _NOT_DIGIT.sub("", value)
    total = 0
    for index in range(len(digits)):
        digit = ord(digits[len(digits) - 1 - index]) - 48
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return len(digits) > 0 and total % 10 == 0


@dataclass(frozen=True)
class Scope:
    """How a field is finalised."""

    #: Redact every string and key. Without it, only the strings that were cut are, and what's under
    #: secret key names in JSON a string holds.
    all: bool
    #: Keys come from the app's data: secret key names apply.
    free_form: bool
    #: Numbers that look like card numbers are redacted (tool inputs).
    cards: bool
    #: The field must be a string: a file becomes `[binary, N bytes]`.
    text: bool


JSON_FIELD: Final = Scope(all=True, free_form=True, cards=True, text=False)
TEXT_FIELD: Final = Scope(all=True, free_form=False, cards=False, text=True)
#: Free-form JSON that isn't a tool input: secret key names apply, card numbers don't.
FREE_FORM: Final = replace(JSON_FIELD, cards=False)


class Redactor:
    """Redaction of one event. Placeholders are numbered per event: the same value gets the same
    number everywhere, also across `before_send` when the same Redactor finishes the event."""

    def __init__(self, patterns: Sequence[PatternName], extra_keys: Sequence[str] = ()) -> None:
        self.patterns: list[PatternName] = list(patterns)
        self.counts: dict[PatternName, int] = {}
        #: What this SDK made of strings before `before_send`, as the hook got them, each with the
        #: tightest limit it keeps its form under after the hook: a cut, the limit it was cut at; a
        #: string whose secret values were replaced, its length before that, since placeholders may
        #: have made it longer than the limit it fitted. Only what is still in the event, after `prune`.
        self.cuts: dict[str, int] = {}
        self._used: dict[PatternName, set[int]] = {}
        self._next: dict[PatternName, int] = {}
        self._assigned: dict[str, int] = {}
        self._scan_work = ScanWork()
        self._secret_keys = "secret" in self.patterns
        self._key_names = SECRET_KEYS + tuple(extra_keys)
        #: A key with fewer characters can't end with any name: stripping it never adds one.
        self._shortest_name = min(map(len, self._key_names))

    @property
    def enabled(self) -> bool:
        return len(self.patterns) > 0

    def reserve(self, value: object) -> None:
        """Records placeholders already in the event, so new ones never reuse their numbers."""
        kind = type(value)
        if kind is int or kind is float or kind is bool or value is None:
            return
        if isinstance(value, str):
            self._reserve_text(value)
        elif isinstance(value, Cut):
            self._reserve_text(value.window)
        elif isinstance(value, (Secret, Binary, TooDeep)):
            # A secret's identity may hold placeholders, but it is sent as a new one: nothing to reserve.
            return
        elif isinstance(value, list):
            for item in value:
                if type(item) not in _LEAVES:
                    self.reserve(item)
        elif isinstance(value, dict):
            for key, item in value.items():
                self._reserve_text(key)
                if type(item) not in _LEAVES:
                    self.reserve(item)

    def _reserve_text(self, value: str) -> None:
        if "[" not in value:
            return
        for found in _EXISTING_PLACEHOLDER.finditer(value):
            pattern = _PATTERN_BY_LABEL.get(found.group(1))
            if pattern is not None:
                self._used_numbers(pattern).add(int(found.group(2)))

    def finalize(self, value: Shaped, scope: Scope) -> Json:
        """Shaped JSON to JSON: cuts redacted and closed, secrets replaced, files and deep containers
        as markers. Secret key names apply in free-form fields and in JSON held in a string even before
        the hook."""
        kind = type(value)
        # The commonest first, by exact type: what each would be found as below.
        if kind is str:
            return self.string(value) if scope.all else self._before_hook(value)  # type: ignore[arg-type]
        if kind is int or kind is float:
            return self._number(value) if scope.all and scope.cards else value  # type: ignore[arg-type,return-value]
        if isinstance(value, Cut):
            closed = self.cut_text(value.window, value.cut, value.scanned) + value.mark
            earlier = self.cuts.get(closed)
            self.cuts[closed] = min(value.limit if earlier is None else earlier, value.limit)
            return closed
        if isinstance(value, Secret):
            # Couldn't be read whole: numbered on its own, never equated with another secret.
            return self._placeholder("secret", "" if value.identity is None else value.identity, value.identity is None)
        if isinstance(value, Binary):
            return f"[binary, {value.bytes} bytes]" if scope.text else {"omitted": "binary", "bytes": value.bytes}
        if isinstance(value, TooDeep):
            return {"omitted": "depth"}
        if isinstance(value, str):
            return self.string(value) if scope.all else self._before_hook(value)
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return self._number(value) if scope.all and scope.cards else value
        if isinstance(value, list):
            if scope.all and scope.cards:
                return [self.finalize(item, scope) for item in value]
            # Numbers, booleans and None are as they are here.
            return [item if type(item) in _LEAVES else self.finalize(item, scope) for item in value]  # type: ignore[misc]
        out: dict[str, Json] = {}
        for key, item in value.items():
            name = self.text(key) if scope.all else key
            # Two keys that redact to the same placeholder must not overwrite each other.
            if scope.all and name in out:
                name = self.text(key, True)
            if scope.free_form and self._secret_keys and self.is_secret_key(key) and not isinstance(item, Secret):
                out[name] = self.secret_value(plain(item))
            else:
                out[name] = self.finalize(item, scope)
        return out

    def prune(self, event: object) -> None:
        """Keeps only what the event still holds once it has been fitted: the cuts in it, and the
        numbers of the placeholders in it with what they stand for. A pending event then holds no
        more of what redaction matched than what it still shows, and `retained_bytes` counts that."""
        strings: set[str] = set()
        present: dict[PatternName, set[int]] = {}

        def note(value: str) -> None:
            strings.add(value)
            if "[" not in value:
                return
            for found in _EXISTING_PLACEHOLDER.finditer(value):
                present.setdefault(_PATTERN_BY_LABEL[found.group(1)], set()).add(int(found.group(2)))

        def walk(value: object) -> None:
            if isinstance(value, str):
                note(value)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    walk(item)
            elif isinstance(value, Mapping):
                for key, item in value.items():
                    note(key)
                    walk(item)

        walk(event)
        for closed in [closed for closed in self.cuts if closed not in strings]:
            del self.cuts[closed]
        for key in [key for key, n in self._assigned.items() if n not in present.get(cast(PatternName, key[: key.index("\x00")]), ())]:
            del self._assigned[key]
        self._used = present

    def retained_bytes(self) -> int:
        """About what this Redactor keeps in memory beyond the event, for the queue's limits."""
        total = 0
        for key in self._assigned:
            total += utf16_length(key) * 2 + 64
        total += len(self.cuts) * 64
        for numbers in self._used.values():
            total += len(numbers) * 16
        return total

    def secret_key_matcher(self) -> Callable[[str], bool] | None:
        """For shaping: None when the `secret` pattern is off."""
        return self.is_secret_key if self._secret_keys else None

    def is_secret_key(self, key: str) -> bool:
        if len(key) < self._shortest_name:
            return False
        if key.isascii():
            lowered = key.lower()
            if lowered.isalnum():
                # Nothing to strip.
                return lowered.endswith(self._key_names)
        return normalize_key(key).endswith(self._key_names)

    def secret_value(self, value: Json) -> Json:
        """The whole value under a secret key name, whatever its shape. Values that can't hold a
        secret (None, booleans, empty values) and existing placeholders stay as they are."""
        if value is None or isinstance(value, bool) or (isinstance(value, str) and value == ""):
            return value
        if isinstance(value, str) and _WHOLE_PLACEHOLDER.fullmatch(value) is not None:
            return value
        if isinstance(value, (list, dict)) and len(value) == 0:
            return value
        return self._placeholder("secret", value if isinstance(value, str) else dumps(value), False)

    def string(self, value: str) -> str:
        """A string value: what's under secret key names in JSON it holds, then the patterns."""
        return self.text(self._without_secret_values(value))

    def _before_hook(self, value: str) -> str:
        """A string value before the hook: only what's under secret key names, which the hook never
        sees."""
        out = self._without_secret_values(value)
        # One ending with a marker is read as any other marked string after the hook.
        if out is not value and not is_marked(out):
            length = len(value)
            earlier = self.cuts.get(out)
            self.cuts[out] = min(length if earlier is None else earlier, length)
        return out

    def _without_secret_values(self, value: str) -> str:
        if not self._secret_keys or not may_hold_key(value):
            return value
        units = to_units(value)
        out, _ = self._window_without_secret_values(units, len(units))
        return value if out is units else join(out)

    def _window_without_secret_values(self, window: str, cut: int) -> tuple[str, int]:
        """Replaces what's under secret key names in JSON held in `window`, in UTF-16 code units, which
        reads past the cut at `cut`: the rewritten window and where the cut now is. A value starting
        past the cut is neither replaced nor counted. One running past it is replaced whole, and its
        placeholder ends what's kept."""
        if not self._secret_keys or not may_hold_key(window):
            return window, cut
        spans = secret_spans(window, self.is_secret_key, self._scan_work)
        if not spans:
            return window, cut
        parts: list[str] = []
        start = 0
        at = cut
        straddled: int | None = None
        for span in spans:
            if span.start >= cut:
                break
            placeholder = self._span_placeholder(span)
            parts += (window[start : span.start], placeholder)
            start = span.end
            if span.end > cut:
                straddled = sum(map(len, parts))
                break
            at += len(placeholder) - (span.end - span.start)
        parts.append(window[start:])
        return "".join(parts), at if straddled is None else straddled

    def _span_placeholder(self, span: SecretSpan) -> str:
        identity = "" if span.identity is None else span.identity
        return span.quote + self._placeholder("secret", identity, span.identity is None) + span.quote

    def text(self, value: str, fresh: bool = False) -> str:
        units = to_units(value)
        out = units
        for pattern in self.patterns:
            if len(out) < SHORTEST_MATCH[pattern] or _skip(pattern, out):
                continue

            def replace_match(found: re.Match[str], pattern: PatternName = pattern) -> str:
                match = found.group(0)
                if _rejected(pattern, match):
                    return match
                return self._placeholder(pattern, _identity(pattern, match), fresh)

            out = PATTERNS[pattern].sub(replace_match, out)
        return out if units is value else join(out)

    def cut_text(self, window: str, cut: int, scanned: bool = False) -> str:
        """Redacts what's kept of a cut string, reading `window` past the cut at `cut` (a code point
        index): values under secret key names in JSON it holds, unless `scanned`, then the patterns. A
        match that runs past the cut is masked whole: its placeholder ends what's kept.
        Matches entirely past the cut are neither replaced nor counted. Then a secret that starts
        like one and runs into the cut, or past the window, is masked too."""
        out = to_units(window)
        split = out is not window
        # Where the cut is, in UTF-16 units, as the patterns' offsets count.
        at = utf16_length(window[:cut]) if split else cut
        if not scanned:
            out, at = self._window_without_secret_values(out, at)
        for pattern in self.patterns:
            if _skip(pattern, out):
                continue
            shift = 0
            straddled: int | None = None

            def replace_match(found: re.Match[str], pattern: PatternName = pattern, at: int = at) -> str:
                nonlocal shift, straddled
                match = found.group(0)
                offset = found.start()
                if offset >= at or straddled is not None or _rejected(pattern, match):
                    return match
                placeholder = self._placeholder(pattern, _identity(pattern, match), False)
                if offset + len(match) <= at:
                    shift += len(placeholder) - len(match)
                else:
                    straddled = offset + shift + len(placeholder)
                return placeholder

            out = PATTERNS[pattern].sub(replace_match, out)
            at = straddled if straddled is not None else at + shift
        kept = self._mask_at_cut(out[:at])
        return join(kept) if split else kept

    def _mask_at_cut(self, kept: str) -> str:
        """A secret whose end was cut off: its kept part becomes the placeholder."""
        if not self._secret_keys:
            return kept
        begin = kept.rfind("-----BEGIN ")
        if begin != -1 and _PEM_BEGIN.match(kept, begin, begin + 64) is not None and _PEM_END.search(kept, begin) is None:
            return kept[:begin] + self._placeholder("secret", kept[begin:], False)
        found = _TOKEN_RUN.match(kept[::-1])
        run = len(kept) - (len(found.group(0)) if found is not None else 0)
        if run >= len(kept):
            return kept
        # The earliest place in the run where a secret could start: its beginning, or just after a
        # character that ends one in the patterns, as `=` does in `token=eyJ…`.
        starts = [run]
        starts.extend(boundary.start() + 1 for boundary in _SECRET_BOUNDARY.finditer(kept, run, len(kept) - 1))
        for start in starts:
            scheme = start == run and _AUTH_SCHEME.search(kept, max(0, start - 64), start) is not None
            if _SECRET_START.match(kept, start) is not None or scheme:
                return kept[:start] + self._placeholder("secret", kept[start:], False)
        return kept

    def _number(self, value: int | float) -> Json:
        """Card numbers stored as JSON numbers inside tool inputs."""
        if "card" not in self.patterns or not _CARD_LOW <= value < _CARD_HIGH:
            return value
        if not (isinstance(value, int) or value.is_integer()):
            return value
        digits = number(value)
        if _CARD_DIGITS.fullmatch(digits) is None or not luhn(digits):
            return value
        return self._placeholder("card", digits, False)

    def _placeholder(self, pattern: PatternName, value: str, fresh: bool) -> str:
        joined = join(value)
        # JSON escapes lone surrogates, which UTF-8 would replace: two values never share a hash for that.
        identity = (
            joined
            if utf16_length(joined) <= MAX_KEPT_IDENTITY
            else "#" + hashlib.sha256(dumps(joined).encode("utf-8")).hexdigest()
        )
        key = f"{pattern}\x00{identity}"
        n = None if fresh else self._assigned.get(key)
        if n is None:
            n = self._next_free(pattern)
            if not fresh:
                self._assigned[key] = n
        self.counts[pattern] = self.counts.get(pattern, 0) + 1
        return f"[{LABELS[pattern]}_{n}]"

    def _next_free(self, pattern: PatternName) -> int:
        """A running counter per pattern, skipping numbers already in the event: linear overall."""
        used = self._used_numbers(pattern)
        n = self._next.get(pattern, 1)
        while n in used:
            n += 1
        used.add(n)
        self._next[pattern] = n + 1
        return n

    def _used_numbers(self, pattern: PatternName) -> set[int]:
        numbers = self._used.get(pattern)
        if numbers is None:
            numbers = self._used[pattern] = set()
        return numbers


#: Python only: every alternative of the secret pattern contains one of these, so text without any
#: can't match. Python's regex engine tries each alternative at each position, which JavaScript's
#: avoids; this check costs a substring search per literal instead.
_SECRET_LITERALS: Final = (
    "lsq_", "sk-", "k_live_", "k_test_", "whsec_", "gho_", "ghp_", "ghs_", "ghu_", "ghr_", "github_pat_", "glpat-", "npm_",
    "xox", "xapp-", "hooks.slack.com/services/", "AKIA", "ASIA", "aws_secret_access_key", "AWS_SECRET_ACCESS_KEY", "AIza",
    "ya29.", "GOCSPX-", "phx_", "eyJ", "earer", "EARER", "Basic", "BASIC", "://", "-----BEGIN ",
)  # fmt: skip
_DIGIT: Final = re.compile(r"[0-9]", re.ASCII)
#: Every IPv6 alternative holds a group followed by a colon, or two colons.
_IPV6_HINT: Final = re.compile(r"[0-9A-Fa-f:]:", re.ASCII)
#: Any of them, in one search: in a short string, cheaper than a substring search per literal.
_SECRET_LITERAL: Final = re.compile("|".join(map(re.escape, _SECRET_LITERALS)))
#: Shaped values with nothing in them to reserve or, outside tool inputs, to finalize.
_LEAVES: Final = frozenset([int, float, bool, type(None)])


def _skip(pattern: PatternName, value: str) -> bool:
    """Cheap checks that let a pattern skip text it can't match: TypeScript's, and in Python, for the
    patterns its engine is slow on, a character or literal every match must contain."""
    if pattern == "email":
        return "@" not in value and "%40" not in value
    if pattern == "ip":
        # IPv4 needs a dot and a digit; IPv6 a colon after a hex digit or another colon. The
        # substring scans first: most text has neither, and they cost far less than a search.
        return (":" not in value or _IPV6_HINT.search(value) is None) and ("." not in value or _DIGIT.search(value) is None)
    if pattern in ("card", "phone"):
        return _DIGIT.search(value) is None
    if len(value) <= 64:
        return _SECRET_LITERAL.search(value) is None
    return not any(literal in value for literal in _SECRET_LITERALS)


def _rejected(pattern: PatternName, match: str) -> bool:
    if pattern == "card":
        return not luhn(match)
    # IPv6 needs three groups: `::1`, `fe80::1` and code such as `data[::10]` or `A::B` stay.
    if pattern == "ip":
        return ":" in match and len([part for part in match.split(":") if part]) < 3
    return False


def _identity(pattern: PatternName, match: str) -> str:
    """What makes two matches "the same value" for numbering."""
    if pattern == "email":
        return match.lower().replace("%40", "@")
    if pattern == "ip":
        return match.lower()
    if pattern in ("card", "phone"):
        return _NOT_DIGIT.sub("", match)
    return match


__all__ = [
    "FREE_FORM",
    "JSON_FIELD",
    "LABELS",
    "ORDER",
    "PATTERNS",
    "SECRET_KEYS",
    "SOURCES",
    "TEXT_FIELD",
    "WS",
    "Redactor",
    "Scope",
    "looks_like_contact",
    "luhn",
    "normalize_key",
    "plain",
    "read_keys",
    "resolve_keys",
    "resolve_patterns",
]
