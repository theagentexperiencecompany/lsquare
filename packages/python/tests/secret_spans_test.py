"""Values under secret key names in JSON held in a string. What they become is in the shared cases
(packages/contract/cases/redaction.json); this is what those can't say: that the work is linear, that
every span is sound on any input, and that TypeScript finds the same on inputs nobody chose."""

from __future__ import annotations

import json
import random
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from lsquare._jsjson import dumps
from lsquare._redact import Redactor, resolve_keys, resolve_patterns
from lsquare._secret_spans import SCAN_WORK, ScanWork, is_valid_json, secret_spans
from lsquare._values import join, to_units, utf16_length

REPO = Path(__file__).resolve().parents[3]
PATTERNS = resolve_patterns(None)


def is_secret(key: str) -> bool:
    return key.lower().endswith("password")


def unlimited() -> ScanWork:
    work = ScanWork()
    work.left = 10**18
    return work


def timed(text: str) -> float:
    best = float("inf")
    for _ in range(3):
        started = time.perf_counter()
        secret_spans(text, is_secret, unlimited())
        best = min(best, time.perf_counter() - started)
    return best


def encoded(text: str, times: int) -> str:
    for _ in range(times):
        text = '{"a":' + json.dumps(text) + "}"
    return text


LINEAR: list[tuple[str, Callable[[int], str]]] = [
    ("quotes", lambda n: '"' * n),
    ("backslashes", lambda n: "\\" * n),
    ("escaped quotes", lambda n: '\\"' * n),
    ("a quote, then backslashes", lambda n: '"' + "\\" * n),
    ("one backslash at the end", lambda n: '"a"' * n + "\\"),
    ("strings holding quotes", lambda n: '"\\"",' * n),
    ("keys", lambda n: '"a":' * n),
    ("secret keys", lambda n: '"password":' * n),
    ("secret values", lambda n: '"password":"x",' * n),
    ("open brackets under a secret key", lambda n: '"password":' + "[" * n),
    ("open brackets, many times", lambda n: '"password":[' * n),
    ("a long payload encoded four times", lambda n: encoded('{"password":"' + "x" * n + '"}', 4)),
    ("a long payload encoded six times", lambda n: encoded('{"a":"' + "x" * n + '"}', 6)),
    ("printed keys", lambda n: '\\\\"x":' * n),
    ("keys full of escapes", lambda n: '"\\\\\\\\\\\\\\\\x":' * n),
    ("escapes surviving every decoding", lambda n: "\\" * 16 * n + "x"),
    ("commas after a quote", lambda n: '"password":"' + '",' * n),
    ("keys after a question mark", lambda n: "[" + "1," * n + '?"password":1,' * (n // 100)),
    ("blank lines in a value holding a quote", lambda n: '"password":"a" b' + "\n" * n + "c"),
    ("long keys", lambda n: '{"' + "x" * n + '":1,"' + "y" * n + '":2}'),
    ("quotes past the nesting limit", lambda n: encoded('"' + " " * n, 5)),
    ("backslashes past the nesting limit", lambda n: encoded('"' + "\\" * n, 5)),
]


@pytest.mark.parametrize(("name", "build"), LINEAR, ids=[name for name, _ in LINEAR])
def test_it_is_linear(name: str, build: Callable[[int], str]) -> None:
    # A few tries: other work on the machine can slow one measurement.
    ratio = float("inf")
    for _ in range(5):
        ratio = min(ratio, timed(build(40_000)) / max(timed(build(10_000)), 0.0005))
        if ratio < 10:
            break
    assert ratio < 10


PIECES = [
    '"', "\\", '\\"', "\\\\", '\\\\\\"', "\\u0022", "\\u003a", "\\\\u0022", "\\u0070", "\\n", "\\ud83d\\ude00", "\\ud83d", ":", ",", "{", "}", "[", "]",
    "password", "token", '"password":', '"token": ', '"a":', '{"secret":"', "abc", "12", "true", "null", " ", "\n", "\r", "\t", "\u0085",
    "\u00a0", "\U0001f600", "\ud83d", "\u00e9", "[SECRET_1]", "[SECRET_2", "sk-abcdefghijklmnopqrstuvwxyz", "bob@example.com", '\\\\"', "\\/", "\t",
]  # fmt: skip


def texts(seed: int, count: int, longest: int) -> list[str]:
    chosen = random.Random(seed)
    return ["".join(chosen.choices(PIECES, k=chosen.randrange(longest))) for _ in range(count)]


def test_spans_are_in_order_not_overlapping_and_inside_the_string_on_any_input() -> None:
    for text in texts(1, 3000, 30):
        units = to_units(text)
        at = 0
        for span in secret_spans(units, is_secret, unlimited()):
            assert at <= span.start < span.end <= len(units), text
            at = span.end


def test_a_string_with_no_key_comes_back_as_it_was() -> None:
    redactor = Redactor(PATTERNS)
    for text in ("no quotes: here", '"no colon"', "C:\\Users\\me", '{"a":"b"}'):
        assert redactor.string(text) == text


SECRET_NAMES = ["password", "dbPassword", "api_key", "token", "client_secret", "Authorization", "cookies"]
PLAIN_NAMES = ["id", "name", "note", "maxTokens", "author", "body", "url", "tokenCount", "é", "a b"]
STRING_PIECES = ["abc", " ", "\n", "\t", '"', "\\", "\\\\", "/", ",", "}", "]", "{", "[", "é", "\U0001f600", " ", "12", "true", "null"]
LAYOUTS = [(", ", ": "), (",", ":"), (" , ", " : ")]


def document(chosen: random.Random, depth: int = 0) -> object:
    """A JSON value: nested containers, every scalar, and strings that hold JSON themselves."""
    kind = chosen.randrange(9 if depth < 4 else 6)
    if kind == 0:
        return chosen.choice([0, 1.5, -3, 1e30, 12345])
    if kind == 1:
        return chosen.choice([True, False, None])
    if kind <= 3:
        # A colon only near the top: past the nesting limit a quote and a colon read as a key.
        return "".join(chosen.choices(STRING_PIECES + [":"] * (depth < 2), k=chosen.randrange(8)))
    if kind == 4:
        return "https://example.com:8080/a?b=c"
    if kind == 5:
        return "" if chosen.randrange(2) else []
    if kind == 6:
        return [document(chosen, depth + 1) for _ in range(chosen.randrange(4))]
    if kind == 7:
        names = chosen.choices(SECRET_NAMES + PLAIN_NAMES, k=chosen.randrange(5))
        out = {name: document(chosen, depth + 1) for name in names}
        if depth < 3 and chosen.randrange(4) == 0:
            out[json.dumps({"note": document(chosen, depth + 1), chosen.choice(SECRET_NAMES): "s3cr3t", "id": 1})] = document(chosen, depth + 1)
        return out
    return json.dumps(document(chosen, depth + 1), ensure_ascii=bool(chosen.randrange(2)))


def masked(value: object) -> object:
    """What parsing the JSON would give: every value under a secret key name replaced, in structured
    data and in strings holding JSON. The nesting limit is left out: `document` stays within it."""
    if isinstance(value, list):
        return [masked(item) for item in value]
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            kept = item is None or isinstance(item, bool) or item in ("", [], {})
            secret = key in SECRET_NAMES and not kept
            out[masked(key)] = "[SECRET]" if secret else masked(item)
        return out
    if isinstance(value, str) and value[:1] in ('{', '[', '"'):
        try:
            inner = json.loads(value)
        except ValueError:
            return value
        hidden = masked(inner)
        return value if hidden == inner else json.dumps(hidden, ensure_ascii=value.isascii())
    return value


@pytest.mark.parametrize("seed", [4, 5, 6])
def test_on_valid_json_it_gives_what_parsing_would(seed: int) -> None:
    chosen = random.Random(seed)
    for _ in range(3000):
        value = {name: document(chosen) for name in chosen.choices(SECRET_NAMES + PLAIN_NAMES, k=chosen.randrange(1, 5))}
        indent = chosen.choice([None, None, 2])
        layout = chosen.choice(LAYOUTS) if indent is None else None
        text = json.dumps(value, indent=indent, separators=layout, ensure_ascii=bool(chosen.randrange(2)))
        expected = json.dumps(masked(value), indent=indent, separators=layout, ensure_ascii=text.isascii())
        got = re.sub(r"\[SECRET_[0-9]+\]", "[SECRET]", Redactor(["secret"]).string(text))
        assert got == expected, text


_TYPESCRIPT = """
import { readFileSync } from 'node:fs'
const src = process.argv[1]
const { Redactor, resolveKeys, resolvePatterns } = await import(`${src}/redact.ts`)
const patterns = resolvePatterns(undefined)
const keys = resolveKeys(['address'])
const out = JSON.parse(readFileSync(0, 'utf8')).map((text) => {
  const whole = new Redactor(patterns, keys)
  const cut = new Redactor(patterns, keys)
  return [whole.string(text[1]), whole.counts, cut.cutText(text[1], text[0]), cut.counts]
})
process.stdout.write(JSON.stringify(out))
"""


@pytest.mark.parametrize("seed", [2, 3])
def test_typescript_finds_the_same_on_inputs_nobody_chose(seed: int) -> None:
    if shutil.which("node") is None:
        pytest.skip("node isn't installed: the TypeScript SDK wasn't run on these inputs")
    assert (REPO / "node_modules" / "tsx" / "package.json").exists(), "tsx is missing: run npm install at the repo root"
    keys = resolve_keys(["address"])
    chosen = random.Random(seed)
    # A cut anywhere: as a code point index here, and as the same place in UTF-16 units there.
    documents = [json.dumps(document(chosen), indent=chosen.choice([None, 2])) for _ in range(500)]
    given = [(text, chosen.randrange(len(text) + 1)) for text in texts(seed, 2000, 40) + documents]
    inputs = [(utf16_length(join(text)[:cut]), join(text)) for text, cut in given]
    run = subprocess.run(
        ["node", "--import", "tsx", "--input-type=module", "-e", _TYPESCRIPT, (REPO / "packages" / "lsquare" / "src").as_uri()],
        cwd=REPO,
        input=dumps(inputs).encode("utf-8"),
        capture_output=True,
        timeout=120,
    )
    assert run.returncode == 0, run.stderr.decode("utf-8", "replace")
    theirs = json.loads(run.stdout)
    for (text, cut), other in zip(given, theirs):
        whole = Redactor(PATTERNS, keys)
        part = Redactor(PATTERNS, keys)
        joined = join(text)
        ours = [join(whole.string(joined)), whole.counts, join(part.cut_text(joined, cut)), part.counts]
        assert ours == [join(other[0]), other[1], join(other[2]), other[3]], repr(text)


# Texts and the work each takes: the same numbers are in secret-spans.test.ts.
_WORK_SAMPLES = [
    ('{"password":"x"}', 4),
    ('{"a":"b","c":"d\\n"}', 14),
    ('{"a":"{\\"password\\":\\"x\\"}"}', 17),
    ("'{\\\"a\\\": 1}'", 14),
    ('plain "quoted": text', 3),
    ('"password":["a",{"b":1}]', 9),
    ('\\\\"x":\\\\"x":\\\\"x":', 39),
    ('"a\\q":1', 10),
]


def test_it_counts_its_work_as_the_typescript_sdk_does() -> None:
    used = []
    for text, _ in _WORK_SAMPLES:
        work = ScanWork()
        secret_spans(text, is_secret, work)
        used.append(SCAN_WORK - work.left)
    assert used == [count for _, count in _WORK_SAMPLES]


def test_it_reads_to_the_last_unit_of_its_work_and_replaces_the_string_whole_past_it() -> None:
    assert SCAN_WORK == 1_000_000
    for text, count in _WORK_SAMPLES:
        read = secret_spans(text, is_secret, unlimited())
        work = ScanWork()
        work.left = count
        assert secret_spans(text, is_secret, work) == read
        work.left = count - 1
        assert secret_spans(text, is_secret, work) == [(0, len(text), None, "")]


def test_a_string_is_replaced_whole_once_the_event_has_used_its_work_and_nothing_unread_is_sent() -> None:
    redactor = Redactor(["secret"])
    crafted = ('":"{\\{' + "\\" * 7) * 20_000
    assert redactor.string(crafted) == "[SECRET_1]"
    assert redactor.string('{"note":"not a secret"}') == "[SECRET_2]"
    assert redactor.string("no quote and colon here") == "no quote and colon here"
    assert redactor.counts == {"secret": 2}


def test_it_has_work_to_spare_on_the_largest_ordinary_event() -> None:
    nested = json.dumps({"a": json.dumps({f"k{index}": f'value "{index}"\n' for index in range(2000)})})
    work = ScanWork()
    for _ in range(1_000_000 // len(nested) + 1):
        secret_spans(nested, is_secret, work)
    assert work.left > 0


def _parses(text: str) -> bool:
    def refuse(constant: str) -> None:
        raise ValueError(constant)

    try:
        json.loads(text, parse_constant=refuse, parse_int=str, parse_float=str)
    except ValueError:
        return False
    return True


_JSON_SAMPLES = [
    "",
    " ",
    "null",
    "nul",
    "true",
    "truex",
    " false ",
    "0",
    "-0",
    "01",
    "-",
    "1.",
    "1.5",
    ".5",
    "1e5",
    "1E+5",
    "1e",
    "+1",
    "NaN",
    "Infinity",
    '""',
    '"',
    '"a',
    '"\\"',
    '"\\\\"',
    '"\\n"',
    '"\\x"',
    '"\\u12"',
    '"\\u1234"',
    '"\\ud800"',
    '"\n"',
    '"\t"',
    '"\u007f"',
    '"\ud800"',
    "[]",
    "[ ]",
    "[1]",
    "[1,]",
    "[,1]",
    "[1 2]",
    "[1,2",
    "{}",
    "{ }",
    '{"a":1}',
    '{"a":1,}',
    '{"a"}',
    '{"a":}',
    "{a:1}",
    "{1:1}",
    '{"a":1 "b":2}',
    '{"a":1]',
    "[1}",
    "{}{}",
    "[] x",
    "\f[]",
    "\u00a0[]",
    "\ufeff[]",
    "[\n1\r,\t2 ]",
    "[" + "1" * 5000 + "]",
    '{"a":[{"b":[{"c":null}]}],"d":"e"}',
    '{a":1}',
    '"\u0000"',
    '"\u001f"',
    '"\\n\t"',
    '"\\uzzzz"',
    '"\\uABCD"',
    '"\\uabcd"',
    '"\\u123"',
    '"\\u\u0661\u0662\u0663\u0664"',
    '"\\/\\b\\f\\r\\t"',
    '"\\ "',
    "{]",
    "[}",
    "[[1] ]",
    '{"a"x1}',
    '{"a" :1}',
    "1e-5",
]


@pytest.mark.parametrize("text", _JSON_SAMPLES)
def test_is_valid_json_agrees_with_the_json_module(text: str) -> None:
    assert is_valid_json(text, unlimited()) == _parses(text)


def test_is_valid_json_agrees_with_the_json_module_on_inputs_nobody_chose() -> None:
    pieces = ["{", "}", "[", "]", ":", ",", '"', "\\", "\\n", "\\u12", "\\u1234", "a", "1", "-", "0", ".", "e", "E", "+"]
    pieces += ["true", "false", "null", "NaN", " ", "\n", "\t", "\f", "\u0001", "\u007f", "\ud800", "\U0001f600"]
    pieces += ['""', "{}", "[]", '"a":', '"b"', "1e5", "01", "-0", "1.", ".5"]
    chosen = random.Random(11)
    valid = 0
    for _ in range(40_000):
        text = "".join(chosen.choice(pieces) for _ in range(chosen.randrange(9)))
        assert is_valid_json(text, unlimited()) == _parses(text), repr(text)
        valid += _parses(text)
    assert valid > 500


def test_is_valid_json_takes_any_depth() -> None:
    assert is_valid_json("[" * 200_000 + "]" * 200_000, unlimited())
    assert is_valid_json('{"a":' * 100_000 + "1" + "}" * 100_000, unlimited())
    assert not is_valid_json("[" * 200_000, unlimited())
