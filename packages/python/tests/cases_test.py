"""The shared cases in packages/contract/cases (shared spec, section 9): TypeScript's cases.test.ts
passes the same files.

Every event is also checked by the ingestion's checks, as the contract has them
(packages/contract/src/validate.ts), given the Python writer's output: one node process per file,
through tsx from the repo's node_modules. Only that check is skipped when node isn't installed.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from lsquare._event import BuiltOk, Context, build_call, build_turn
from lsquare._jsjson import dumps
from lsquare._redact import resolve_keys, resolve_patterns
from lsquare._values import join, js_order

REPO = Path(__file__).resolve().parents[3]
CASES = REPO / "packages" / "contract" / "cases"
VALIDATOR = REPO / "packages" / "contract" / "src" / "validate.ts"
CAPTURED_AT = "2026-10-28T00:00:00.000Z"
SDK = {"name": "lsquare", "version": "0.2.0", "language": "python"}
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


# Reading the files as JavaScript reads them.


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """An object in JavaScript's order: integer-like keys first, ascending, then the others."""
    merged = dict(pairs)
    return {key: merged[key] for key in js_order(list(merged))}


def _int(literal: str) -> int | float:
    # JSON.parse reads -0 as negative zero; Python's parser as 0.
    return -0.0 if literal == "-0" else int(literal)


def load(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = json.loads(path.read_text("utf-8"), object_pairs_hook=_object, parse_int=_int)
    return cases


def expand(value: Any) -> Any:
    """Expands the compact forms the README describes."""
    if isinstance(value, list):
        return [expand(item) for item in value]
    if not isinstance(value, dict):
        return value
    if "$prose" in value:
        count = value["$prose"]
        return ("lorem ipsum " * math.ceil(count / 12))[:count]
    if "$repeat" in value:
        repeated, times = value["$repeat"]
        return repeated * times
    if "$concat" in value:
        # Joined as Python joins strings: a pair split across parts stays two code points here.
        return "".join(expand(part) for part in value["$concat"])
    if "$range" in value:
        return list(range(value["$range"]))
    if "$keys" in value:
        return {f"k{index}": index for index in range(value["$keys"])}
    return {key: expand(item) for key, item in value.items()}


def context(case: dict[str, Any]) -> Context:
    options = case.get("options") or {}
    return Context(
        sdk=SDK,  # type: ignore[arg-type]
        patterns=resolve_patterns(options.get("redact")),
        redact_keys=resolve_keys(options.get("redactKeys")),
        max_event_bytes=256 * 1024,
    )


def build(case: dict[str, Any]) -> Any:
    if "turn" in case:
        return build_turn(expand(case["turn"]), CAPTURED_AT, context(case), False)
    call = expand(case["call"])
    record: dict[str, Any] = {
        "callId": "case-call",
        "capturedAt": CAPTURED_AT,
        "toolName": "search_orders",
        "status": "completed",
        "durationMs": 1,
        "ids": {},
        "diagnostics": False,
    }
    record.update({key: item for key, item in call.items() if key not in ("input", "result")})
    if "input" in call:
        record["input"] = {"value": call["input"]}
    if "result" in call:
        record["result"] = {"value": call["result"]}
    return build_call(record, context(case), False)


# Comparing as JavaScript's toEqual does.


def _double(value: int | float) -> float:
    try:
        return float(value)
    except OverflowError:
        return math.inf if value > 0 else -math.inf


def _units(value: str) -> str:
    """A string as JavaScript compares it: two that differ only in how a pair is held are equal."""
    return join(value)


def difference(actual: Any, expected: Any, path: str = "$") -> str | None:
    """Where two values differ, or None. Strict as JavaScript is: True isn't 1, and 1 is 1.0."""
    found = f"{path}: {actual!r:.200} != {expected!r:.200}"
    if isinstance(actual, bool) or isinstance(expected, bool):
        return None if type(actual) is bool and type(expected) is bool and actual == expected else found
    if actual is None or expected is None:
        return None if actual is None and expected is None else found
    if isinstance(actual, (int, float)) or isinstance(expected, (int, float)):
        if not (isinstance(actual, (int, float)) and isinstance(expected, (int, float))):
            return found
        a, b = _double(actual), _double(expected)
        if math.isnan(a) and math.isnan(b):
            return None
        # Object.is: 0 and -0 differ.
        return None if a == b and math.copysign(1, a) == math.copysign(1, b) else found
    if isinstance(actual, str) or isinstance(expected, str):
        return None if isinstance(actual, str) and isinstance(expected, str) and _units(actual) == _units(expected) else found
    if isinstance(actual, list) or isinstance(expected, list):
        if not (isinstance(actual, list) and isinstance(expected, list)):
            return found
        if len(actual) != len(expected):
            return f"{path}: {len(actual)} items != {len(expected)} items"
        for index, (left, right) in enumerate(zip(actual, expected)):
            inner = difference(left, right, f"{path}[{index}]")
            if inner is not None:
                return inner
        return None
    if isinstance(actual, dict) and isinstance(expected, dict):
        left = {_units(key): key for key in actual}
        right = {_units(key): key for key in expected}
        if set(left) != set(right):
            return f"{path}: keys {sorted(left)!r:.300} != {sorted(right)!r:.300}"
        for key in right:
            inner = difference(actual[left[key]], expected[right[key]], f"{path}.{key}")
            if inner is not None:
                return inner
        return None
    return found


def check(case: dict[str, Any], built: Any) -> None:
    if "error" in case:
        assert not built.ok, f"expected {case['error']}, got an event"
        assert built.code == case["error"]
        return
    assert isinstance(built, BuiltOk), f"dropped as {built.code}"
    event = dict(built.draft.event)
    event.pop("sdk")
    assert event.pop("capturedAt") == CAPTURED_AT
    expected = expand(case["event"])
    if expected["eventId"] == "$uuid":
        assert isinstance(event["eventId"], str) and UUID.fullmatch(event["eventId"]), event["eventId"]
        expected["eventId"] = event["eventId"]
    found = difference(event, expected)
    assert found is None, found


# The ingestion's checks: the contract's validator.


def node_available() -> bool:
    return shutil.which("node") is not None


_VALIDATE = """
import { readFileSync } from 'node:fs'
const { validateEvent } = await import(process.argv[1])
const lines = readFileSync(0, 'utf8').split('\\n').filter(Boolean)
process.stdout.write(JSON.stringify(lines.map((line) => {
  const result = validateEvent(JSON.parse(line))
  return result.ok ? 'valid' : result.reason
})))
"""


def ingestion(events: list[dict[str, Any]]) -> list[str]:
    """What the ingestion says about each event, given the Python writer's JSON: `valid` or its
    rejection reason. One node process for all of them. Fails (never skips) without tsx."""
    tsx = REPO / "node_modules" / "tsx" / "package.json"
    assert tsx.exists(), f"tsx is missing from {tsx.parent.parent}: run npm install at the repo root"
    lines = "\n".join(dumps(event) for event in events) + "\n"
    run = subprocess.run(
        ["node", "--import", "tsx", "--input-type=module", "-e", _VALIDATE, VALIDATOR.as_uri()],
        cwd=REPO,
        input=lines.encode("utf-8"),
        capture_output=True,
        timeout=120,
    )
    assert run.returncode == 0, run.stderr.decode("utf-8", "replace")
    results: list[str] = json.loads(run.stdout)
    assert len(results) == len(events)
    return results


def require_node() -> None:
    if not node_available():
        pytest.skip("node isn't installed: the contract's validator (validate.ts) wasn't run on these events")


# The tests.

FILES = sorted(path.name for path in CASES.glob("*.json"))


@cache
def cases(file: str) -> list[dict[str, Any]]:
    return load(CASES / file)


def _params() -> list[Any]:
    return [pytest.param(file, index, id=f"{file}: {case['name']}") for file in FILES for index, case in enumerate(cases(file))]


@pytest.mark.parametrize(("file", "index"), _params())
def test_case(file: str, index: int) -> None:
    case = cases(file)[index]
    check(case, build(case))


@pytest.mark.parametrize("file", FILES)
def test_the_ingestion_accepts_each_event(file: str) -> None:
    require_node()
    named: list[tuple[str, dict[str, Any]]] = []
    for case in cases(file):
        if "event" not in case:
            continue
        built = build(case)
        assert isinstance(built, BuiltOk), case["name"]
        named.append((case["name"], built.draft.event))
    results = ingestion([event for _, event in named])
    rejected = {name: result for (name, _), result in zip(named, results) if result != "valid"}
    assert rejected == {}


_TYPESCRIPT = """
import { readFileSync } from 'node:fs'
const [src, file, sdk] = process.argv.slice(1)
const { buildCall, buildTurn } = await import(`${src}/event.ts`)
const { resolveKeys, resolvePatterns } = await import(`${src}/redact.ts`)
const CAPTURED_AT = '2026-10-28T00:00:00.000Z'
// As cases.test.ts expands and builds them.
function expand(value) {
  if (Array.isArray(value)) return value.map(expand)
  if (typeof value !== 'object' || value === null) return value
  if ('$prose' in value) return 'lorem ipsum '.repeat(Math.ceil(value.$prose / 12)).slice(0, value.$prose)
  if ('$repeat' in value) return value.$repeat[0].repeat(value.$repeat[1])
  if ('$concat' in value) return value.$concat.map(expand).join('')
  if ('$range' in value) return Array.from({ length: value.$range }, (_, i) => i)
  if ('$keys' in value) return Object.fromEntries(Array.from({ length: value.$keys }, (_, i) => [`k${i}`, i]))
  return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, expand(item)]))
}
const written = JSON.parse(readFileSync(file, 'utf8')).map((test) => {
  const context = { sdk: JSON.parse(sdk), patterns: resolvePatterns(test.options?.redact), redactKeys: resolveKeys(test.options?.redactKeys), maxEventBytes: 256 * 1024 }
  let built
  if (test.turn) built = buildTurn(expand(test.turn), CAPTURED_AT, context, false)
  else {
    const { input, result, ...rest } = expand(test.call)
    const record = { callId: 'case-call', capturedAt: CAPTURED_AT, toolName: 'search_orders', status: 'completed', durationMs: 1, ids: {}, diagnostics: false, ...rest,
      ...(input === undefined ? {} : { input: { value: input } }), ...(result === undefined ? {} : { result: { value: result } }) }
    built = buildCall(record, context, false)
  }
  if (!built.ok) return built.code
  const event = built.draft.event
  if (test.event?.eventId === '$uuid') event.eventId = '$uuid'
  return JSON.stringify(event)
})
process.stdout.write(JSON.stringify(written))
"""


@pytest.mark.parametrize("file", FILES)
def test_typescript_writes_the_same_bytes(file: str) -> None:
    """Python only: TypeScript builds the same cases, and each event's JSON text is the same,
    character for character, key order included."""
    require_node()
    source = (REPO / "packages" / "lsquare" / "src").as_uri()
    run = subprocess.run(
        ["node", "--import", "tsx", "--input-type=module", "-e", _TYPESCRIPT, source, str(CASES / file), json.dumps(SDK)],
        cwd=REPO,
        capture_output=True,
        timeout=120,
    )
    assert run.returncode == 0, run.stderr.decode("utf-8", "replace")
    written: list[str] = json.loads(run.stdout)
    for case, theirs in zip(cases(file), written):
        built = build(case)
        if not isinstance(built, BuiltOk):
            assert theirs == built.code, case["name"]
            continue
        event = dict(built.draft.event)
        if case["event"]["eventId"] == "$uuid":
            event["eventId"] = "$uuid"
        assert dumps(event) == theirs, case["name"]


class _Random:
    """Random inputs in the shared cases' form: non-ASCII text, pairs split across `$concat` parts,
    secrets and markers, integer-like keys, long strings, numbers JavaScript writes its own way."""

    ATOMS = ["a", "é", "\U0001f600", " ", "\n", " ", "\ud800", "\udc00", "jane@acme.io", "+44 20 7946 0958", "4111 1111 1111 1111",
             "sk-abcdefghijklmnopqrstuvwx", "Bearer abcdefghijklmnop1234", "10.0.0.1", "[EMAIL_1]", "…[cut]", "…[+12 chars]", "QUJD",
             "eyJhbGciOiJIUzI1NiJ9.", "-----BEGIN PRIVATE KEY-----\n", "redis://u:p@h", "١", "\x00", '"', "\\", "=", "data:x;base64,"]  # fmt: skip
    KEYS = ["a", "b", "0", "2", "10", "01", "-1", "password", "apiKey", "firstName", "name", "text", "__proto__", "é", "\U0001f600", "4294967295", "4294967294"]
    NUMBERS = [0, -0.0, 1, -7, 1.5, 1e-7, 1e21, 0.1, 2**53 + 1, 4111111111111111, 1e300, 5e-324, 123456789.123, 7.0]

    def __init__(self, seed: int) -> None:
        import random

        self.rng = random.Random(seed)

    def text(self) -> Any:
        rng = self.rng
        parts: list[Any] = [rng.choice(self.ATOMS) for _ in range(rng.randint(0, 6))]
        if rng.random() < 0.15:
            parts.append({"$repeat": [rng.choice(self.ATOMS), rng.choice([10, 999, 1000, 4500, 16_001, 20_000])]})
        if rng.random() < 0.1:
            parts.append({"$prose": rng.choice([500, 4_001, 16_000, 16_001, 40_000])})
        if rng.random() < 0.15:
            parts += ["\ud83d", "\ude00"]
        return {"$concat": parts}

    def value(self, depth: int = 0) -> Any:
        rng = self.rng
        roll = rng.random()
        if depth > 11 or roll < 0.3:
            return self.text()
        if roll < 0.45:
            return rng.choice(self.NUMBERS)
        if roll < 0.5:
            return rng.choice([None, True, False])
        if roll < 0.7:
            return [self.value(depth + 1) for _ in range(rng.randint(0, 4))] if rng.random() < 0.9 else {"$range": rng.choice([5, 101])}
        if rng.random() < 0.1:
            return {"$keys": rng.choice([3, 150])}
        return {rng.choice(self.KEYS): self.value(depth + 1) for _ in range(rng.randint(0, 5))}

    def turn(self) -> dict[str, Any]:
        rng = self.rng
        makers: dict[str, Any] = {
            "question": self.text,
            "answer": self.text,
            "turnId": lambda: rng.choice(["t", 7, "", 1.5]),
            "conversationId": lambda: rng.choice(["c", "c/\U0001f600"]),
            "orgId": lambda: "org",
            "error": lambda: rng.choice([self.text(), {"message": self.text(), "type": "T"}]),
            "latency": self.value,
            "verification": self.value,
            "metadata": lambda: {rng.choice(self.KEYS): rng.choice([self.text(), 1, True, None, [1]]) for _ in range(3)},
            "toolCalls": lambda: [{"name": rng.choice(["t", ""]), "input": self.value(), "result": self.value(), "errorMessage": self.text()} for _ in range(rng.randint(0, 3))],
            "client": lambda: rng.choice([self.text(), {"name": "c", "version": "1"}]),
            "usage": lambda: {"inputTokens": rng.choice(self.NUMBERS), "costUsd": rng.choice(["x", 0.5])},
        }
        return {key: make() for key, make in makers.items() if rng.random() < 0.4}

    def call(self) -> dict[str, Any]:
        rng = self.rng
        out: dict[str, Any] = {}
        if rng.random() < 0.7:
            out["input"] = self.value()
        if rng.random() < 0.6:
            blocks = []
            for _ in range(rng.randint(0, 4)):
                kind = rng.choice(["text", "image", "resource", "resource_link", "other", ""])
                block = {"type": kind, "text": self.text(), "data": rng.choice(["QUJD", self.text()]), "mimeType": self.text(), "uri": self.text(), "name": self.text()}
                if kind == "resource":
                    block["resource"] = {"uri": self.text(), "text": self.text()} if rng.random() < 0.5 else {"uri": "u", "blob": "QUJD"}
                blocks.append(block)
            out["result"] = {"content": blocks, "structuredContent": self.value(), "isError": rng.choice([True, False, "no"])}
        for key, chance, make in [
            ("intent", 0.3, self.text),
            ("error", 0.3, lambda: {"message": self.text(), "type": "T", "code": rng.choice([1, "x", True, -32602]), "category": rng.choice(["thrown", "nope"])}),
            ("agent", 0.3, self.turn),
            ("task", 0.2, lambda: {"id": self.text(), "status": self.text()}),
            ("metadata", 0.2, lambda: {rng.choice(self.KEYS): rng.choice([self.text(), 2]) for _ in range(2)}),
            ("diagnostics", 0.1, lambda: True),
        ]:
            if rng.random() < chance:
                out[key] = make()
        return out

    def cases(self, count: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for index in range(count):
            case: dict[str, Any] = {"name": f"random {index}"}
            if self.rng.random() < 0.5:
                case["turn"] = self.turn()
            else:
                case["call"] = self.call()
            if self.rng.random() < 0.3:
                case["options"] = {"redactKeys": ["name", "text"]} if self.rng.random() < 0.5 else {"redact": {"email": False}}
            out.append(case)
        return out


def test_random_inputs_give_typescripts_bytes(tmp_path: Path) -> None:
    """Python only: random inputs, built by both SDKs, give the same JSON text."""
    require_node()
    path = tmp_path / "random.json"
    path.write_text(json.dumps(_Random(28).cases(300), ensure_ascii=True), "utf-8")
    source = (REPO / "packages" / "lsquare" / "src").as_uri()
    run = subprocess.run(
        ["node", "--import", "tsx", "--input-type=module", "-e", _TYPESCRIPT, source, str(path), json.dumps(SDK)],
        cwd=REPO,
        capture_output=True,
        timeout=300,
    )
    assert run.returncode == 0, run.stderr.decode("utf-8", "replace")
    random_id = re.compile(r'"eventId":"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"')
    for case, theirs in zip(load(path), json.loads(run.stdout)):
        built = build(case)
        ours = dumps(built.draft.event) if isinstance(built, BuiltOk) else built.code
        assert random_id.sub('"eventId":"$uuid"', ours) == random_id.sub('"eventId":"$uuid"', theirs), case["name"]


def test_every_file_has_cases() -> None:
    assert FILES == ["calls.json", "ids.json", "numbers.json", "redaction.json", "shortening.json"]
    assert all(cases(file) for file in FILES)
    assert sum(len(cases(file)) for file in FILES) == len(_params())


def test_the_comparison_is_strict() -> None:
    assert difference(True, 1) is not None
    assert difference(1, True) is not None
    assert difference(1, 1.0) is None
    assert difference(0.0, -0.0) is not None
    assert difference(2**53 + 1, 2**53) is None  # as doubles, which JavaScript compares
    assert difference("\ud83d" + "\ude00", "\U0001f600") is None
    assert difference({"a": 1}, {"a": 1, "b": 2}) is not None
    assert difference([1], [1, None]) is not None
    assert difference(None, False) is not None


def test_the_files_are_read_in_javascripts_order() -> None:
    assert list(load_text('{"b": 1, "10": 2, "2": 3, "01": 4}')) == ["2", "10", "b", "01"]
    assert math.copysign(1, load_text('{"z": -0}')["z"]) == -1


def load_text(written: str) -> Any:
    return json.loads(written, object_pairs_hook=_object, parse_int=_int)
