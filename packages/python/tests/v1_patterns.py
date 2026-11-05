"""0.1's phone and ip patterns, which 0.2 rewrites, and the inputs their matches are compared on.

TypeScript's test/v1-patterns.ts has the same inputs. The patterns are copied from
packages/node/src/redact.ts rather than read from it, so that a 0.1.x release with the rewrite can't
turn the comparison into one of the rewrite with itself.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Iterator
from typing import Final

V1: Final[dict[str, re.Pattern[str]]] = {
    "phone": re.compile(r"\+[1-9]\d{0,2}[ .-]?\(?\d{1,4}\)?(?:[ .-]?\d{2,4}){2,4}\b|\(\d{3}\) ?\d{3}-\d{4}\b|\b\d{3}-\d{3}-\d{4}\b", re.ASCII),
    "ip": re.compile(
        r"\b(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}\b|(?<![0-9A-Za-z:.])(?:(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}"
        r"|(?:[0-9A-Fa-f]{1,4}:){1,7}:|(?:[0-9A-Fa-f]{1,4}:){1,6}:[0-9A-Fa-f]{1,4}|(?:[0-9A-Fa-f]{1,4}:){1,5}(?::[0-9A-Fa-f]{1,4}){1,2}"
        r"|(?:[0-9A-Fa-f]{1,4}:){1,4}(?::[0-9A-Fa-f]{1,4}){1,3}|(?:[0-9A-Fa-f]{1,4}:){1,3}(?::[0-9A-Fa-f]{1,4}){1,4}"
        r"|(?:[0-9A-Fa-f]{1,4}:){1,2}(?::[0-9A-Fa-f]{1,4}){1,5}|[0-9A-Fa-f]{1,4}:(?::[0-9A-Fa-f]{1,4}){1,6}|:(?::[0-9A-Fa-f]{1,4}){1,7})"
        r"(?![0-9A-Za-z:]|\.\d)",
        re.ASCII,
    ),
}

#: The digits 0.1's international phone pieces take: country code, area code, four groups.
_FEWEST: Final = (1, 1, 2, 2, 2, 2)
_MOST: Final = (3, 4, 4, 4, 4, 4)
#: Characters between two runs of digits, and the pieces each may follow. `.` and `-` match as ` `
#: does, and _EXPLICIT has a few.
_BETWEEN: Final = {" ": (0, 1, 2, 3, 4), "(": (0,), " (": (0,), ")": (1,), ") ": (1,)}


def _numbers(text: str, starts: list[int], runs: int) -> Iterator[str]:
    """Phone numbers: `text`, then a run of digits that starts at one of the pieces `starts`, and so
    on up to six runs (`runs` counts this one), each after a character that may follow a piece the
    one before can end at. Each run is any length up to one more than the pieces left can take, and
    each string ends after a run, or after a run and a letter."""
    longest = max(sum(_MOST[start:]) for start in starts)
    for length in range(1, longest + 2):
        run = text + "1" * length
        yield run
        yield run + "a"
        if runs == 6:
            continue
        ends: set[int] = set()
        for start in starts:
            fewest = most = 0
            for end in range(start, len(_MOST)):
                fewest += _FEWEST[end]
                most += _MOST[end]
                if fewest <= length <= most:
                    ends.add(end)
        for between, after in _BETWEEN.items():
            following = [end + 1 for end in ends if end < len(_MOST) - 1 and end in after]
            if following:
                yield from _numbers(run + between, following, runs + 1)


#: Forms the generated inputs leave out: other separators, zeros, and what can border a number.
_EXPLICIT: Final = (
    "+44 20 7946 0958", "+44.20.7946.0958", "+44-20-7946-0958", "+1-555-123-4567", "+1.555.123.4567",
    "+1 (555) 123-4567", "+1-(555)-123-4567", "+1.(555).123.4567", "+1 555--123 4567", "+1..555.123.4567",
    "+1-555-123-4567-", "+1.555.123.4567.", "+1 555 123 4567-8", "+1 (555)-123.4567", "+1(555)123-4567",
    "+44 (0)20 7946 0958", "+44 020 7946 0958", "+0 20 7946 0958", "+01 234 5678", "+1 000 000 0000", "+100000000000",
    "+1_555_123_4567", "_+15551234567", "+15551234567_", "+1 555 123_4567", "a+15551234567", "1+15551234567",
    "+44 20 7946 0958é", "é+44 20 7946 0958", "+44 20 7946 0958٣", "+٤٤ 20 7946 0958",
    "+44 20 7946 ٠9 58", "+44 20 7946 0958\U0001f600", "+44 20 7946 0958", "+44​20 7946 0958",
    "(555) 123-4567", "(555)123-4567", "555-123-4567", "1555-123-4567", "555-123-45678", "(555) 123-4567_", "_555-123-4567",
    "192.168.1.20", "192.168.01.1", "192.168.001.001", "010.001.000.001", "00.0.0.0", "0.0.0.0", "255.255.255.255",
    "1.2.3.4.", "1.2.3.4-5", "10.0.0.1.5", "10.0.0.1_", "_10.0.0.1", "10.0.0.1é", "é10.0.0.1", "10.0.0.١",
    "١٠.0.0.1", "10-0-0-1", "10.0.0.1-", "-10.0.0.1", "10.0.0.1:8080", "v1.2.3.4", "1.2.3.4a",
)  # fmt: skip


def inputs() -> Iterator[str]:
    """The inputs: phone numbers, every short string after a `+`, IPv4 addresses, and _EXPLICIT."""
    yield from _numbers("+", [0], 1)
    for length in range(6):
        for word in itertools.product("10 ()a#", repeat=length):
            yield "+" + "".join(word)
    octets = ["0", "1", "01", "11", "19", "25", "99", "100", "199", "200", "249", "250", "255", "256", "300", "1111"]
    for a, b in itertools.product(octets, repeat=2):
        for end in ["", "a", " ", ".", ".1", ":"]:
            yield f"{a}.{b}.{b}.{a}{end}"
            yield f" {a}.{a}.{b}.{b}.{a}{end}"
    yield from _EXPLICIT


def first_difference(pattern: re.Pattern[str], reference: re.Pattern[str]) -> str | None:
    """The first input on which the two patterns' matches differ, if any."""
    for text in inputs():
        if [match.span() for match in pattern.finditer(text)] != [match.span() for match in reference.finditer(text)]:
            return text
    return None
