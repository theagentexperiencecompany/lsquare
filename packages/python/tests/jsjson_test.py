"""The JavaScript-compatible writer and JSON measuring (Python only): `dumps` writes exactly what
`JSON.stringify` writes, checked against node itself where it is installed."""

from __future__ import annotations

import json
import math
import random
import struct
import subprocess
from typing import Any

import pytest

from lsquare._jsjson import Budget, dumps, json_length, number, string
from lsquare._values import is_array_index, join, js_order, to_units, utf16_length

from cases_test import node_available


def node(script: str, data: str) -> Any:
    """Runs a script under node with `data` on stdin, and parses what it prints as JSON."""
    if not node_available():
        pytest.skip("node isn't installed: this comparison with JSON.stringify wasn't run")
    run = subprocess.run(["node", "-e", script], input=data.encode("utf-8"), capture_output=True, timeout=60)
    assert run.returncode == 0, run.stderr.decode("utf-8", "replace")
    return json.loads(run.stdout)


class TestStrings:
    def test_escapes_what_json_stringify_escapes_and_writes_the_rest_as_itself(self) -> None:
        assert string('"\\') == '"\\"\\\\"'
        assert string("\b\f\n\r\t") == '"\\b\\f\\n\\r\\t"'
        assert string("\x00\x01\x1f") == '"\\u0000\\u0001\\u001f"'
        assert string("\x7f\u2028\u2029é\U0001f600") == '"\x7f\u2028\u2029é\U0001f600"'
        assert string("/") == '"/"'

    def test_writes_lone_surrogates_as_lowercase_escapes(self) -> None:
        assert string("a\ud800b\udfffc") == '"a\\ud800b\\udfffc"'
        assert string("\udc00\ud800") == '"\\udc00\\ud800"'

    def test_writes_a_pair_python_holds_as_two_code_points_as_the_character_it_is(self) -> None:
        assert string("\ud83d" + "\ude00") == '"\U0001f600"'
        assert string("\ud83d\ud83d\ude00") == '"\\ud83d\U0001f600"'

    def test_matches_json_stringify_for_every_ascii_character_and_the_edges(self) -> None:
        samples = [chr(code) for code in range(0x80)] + ["\u2028", "\u2029", "\ud800", "\udbff", "\udc00", "\udfff", "é", "\U0001f600"]
        samples.append("".join(samples))
        # Sent to node as UTF-16 code units, so lone surrogates arrive as they are.
        units = [[ord(unit) for unit in to_units(sample)] for sample in samples]
        written = node(
            "const units = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
            "process.stdout.write(JSON.stringify(units.map((u) => JSON.stringify(String.fromCharCode(...u)))))",
            json.dumps(units),
        )
        assert [string(sample) for sample in samples] == written


class TestNumbers:
    @pytest.mark.parametrize(
        ("value", "written"),
        [
            (0, "0"),
            (-0.0, "0"),
            (1.0, "1"),
            (-1.5, "-1.5"),
            (1e-7, "1e-7"),
            (1.5e-7, "1.5e-7"),
            (0.000001, "0.000001"),
            (1e21, "1e+21"),
            (1.5e21, "1.5e+21"),
            (1e20, "100000000000000000000"),
            (1e16, "10000000000000000"),
            (123e18, "123000000000000000000"),
            (0.1 + 0.2, "0.30000000000000004"),
            (5e-324, "5e-324"),
            (1.7976931348623157e308, "1.7976931348623157e+308"),
            (2**53 - 1, "9007199254740991"),
            (2**53 + 1, "9007199254740992"),
            (-(2**53) - 3, "-9007199254740996"),
            (10**21, "1e+21"),
            (10**400, "Infinity"),
            (-(10**400), "-Infinity"),
            (math.inf, "Infinity"),
            (math.nan, "NaN"),
        ],
    )
    def test_writes_numbers_as_number_prototype_to_string(self, value: int | float, written: str) -> None:
        assert number(value) == written

    def test_json_writes_what_isnt_finite_as_null(self) -> None:
        assert dumps([math.inf, -math.inf, math.nan, 10**400]) == "[null,null,null,null]"

    def test_refuses_what_isnt_a_number(self) -> None:
        with pytest.raises(TypeError):
            number(True)

    def test_matches_javascript_for_many_doubles(self) -> None:
        rng = random.Random(28)
        values = [rng.uniform(-1e3, 1e3) for _ in range(300)]
        values += [struct.unpack("<d", rng.getrandbits(64).to_bytes(8, "little"))[0] for _ in range(1200)]
        values += [10.0**exponent for exponent in range(-30, 30)] + [1.5 * 10.0**exponent for exponent in range(-30, 30)]
        values += [rng.random() * 10.0**rng.randint(-330, 308) for _ in range(500)]
        values = [value for value in values if math.isfinite(value)]
        written = node(
            "const hexes = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
            "process.stdout.write(JSON.stringify(hexes.map((h) => String(Buffer.from(h, 'hex').readDoubleLE(0)))))",
            json.dumps([struct.pack("<d", value).hex() for value in values]),
        )
        assert [number(value) for value in values] == written


class TestDumps:
    def test_writes_compact_json_with_keys_in_javascripts_order(self) -> None:
        value = {"b": [1, {"x": None}], "10": True, "2": "two", "01": False}
        assert dumps(value) == '{"2":"two","10":true,"b":[1,{"x":null}],"01":false}'

    def test_matches_json_stringify(self) -> None:
        value = {"b": "é\n\U0001f600", "10": [1.5, -0.0, 1e21, 1e-7, None], "2": {"z": "\u2028"}, "": [], "a": {}}
        written = node("process.stdout.write(JSON.stringify(JSON.stringify(JSON.parse(require('fs').readFileSync(0, 'utf8')))))", json.dumps(value))
        assert dumps(value) == written

    def test_writes_tuples_as_arrays(self) -> None:
        assert dumps((1, "a")) == '[1,"a"]'

    def test_refuses_what_isnt_json(self) -> None:
        for value in [{1: "a"}, {"a": {1, 2}}, [b"x"], object()]:
            with pytest.raises(TypeError):
                dumps(value)


class TestStringRules:
    def test_joins_pairs_and_keeps_lone_surrogates(self) -> None:
        assert join("\ud83d" + "\ude00") == "\U0001f600"
        assert len(join("a\ud83d\ud83d\ude00\ude00")) == 4
        assert join("\ude00\ud83d") == "\ude00\ud83d"

    def test_counts_utf16_units(self) -> None:
        assert utf16_length("a\U0001f600é") == 4
        assert utf16_length("\ud83d" + "\ude00") == 2
        assert utf16_length("\ud800") == 1

    def test_splits_into_units_and_back(self) -> None:
        assert to_units("a\U0001f600") == "a\ud83d\ude00"
        assert join(to_units("a\U0001f600\ud800")) == "a\U0001f600\ud800"

    def test_knows_javascripts_array_indexes(self) -> None:
        for key in ["0", "7", "10", "4294967294"]:
            assert is_array_index(key)
        for key in ["", "01", "-1", "1.5", "4294967295", "99999999999", " 1", "\u0661", "1a", "+1"]:
            assert not is_array_index(key)
        assert js_order(["b", "10", "2", "a", "0", "4294967295"]) == ["0", "2", "10", "b", "a", "4294967295"]


class TestJsonLength:
    @pytest.mark.parametrize(
        "value",
        [{"a": "\n"}, ["\\", "\x00"], {'k"ey': "é\t"}, [1, None, True, -0.0, 1e21], "plain", {"x": "\U0001f600"}, {"2": 1, "b": [[]]}],
    )
    def test_matches_the_writer_in_code_points(self, value: Any) -> None:
        assert json_length(value, Budget(1_000_000)) == len(dumps(value))

    def test_counts_a_lone_surrogate_as_its_escape(self) -> None:
        assert json_length("\ud800", Budget(100)) == 8

    def test_says_why_it_cannot(self) -> None:
        assert json_length({1, 2}, Budget(100)) == "not_json"
        assert json_length(b"raw", Budget(100)) == "not_json"
        assert json_length([0] * 200, Budget(100)) == "too_large"
        assert json_length("x" * 200, Budget(100)) == "too_large"
        assert json_length({f"k{i}": 0 for i in range(200)}, Budget(100)) == "too_large"
        circular: list[Any] = []
        circular.append(circular)
        assert json_length(circular, Budget(100)) == "not_json"
        assert json_length({1: "a"}, Budget(100)) == "not_json"

    def test_shares_the_allowance(self) -> None:
        allowance = Budget(10)
        assert json_length([0] * 20, allowance) == "too_large"
        assert json_length([0], allowance) == "too_large"

    def test_writes_what_isnt_finite_as_null(self) -> None:
        assert json_length([math.inf, 10**400], Budget(100)) == len("[null,null]")
