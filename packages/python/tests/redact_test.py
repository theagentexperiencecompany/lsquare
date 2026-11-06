"""Port of packages/lsquare/test/redact.test.ts."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from typing import Any

import pytest

from lsquare._event import BuiltOk, Context, build_turn
from lsquare._jsjson import dumps
from lsquare._redact import PATTERNS, SOURCES, WS, Redactor, luhn, resolve_patterns
from lsquare._types import PatternName

from cases_test import REPO, node_available
from v1_patterns import V1, first_difference, inputs

ALL = resolve_patterns(None)
SDK = {"name": "lsquare", "version": "0.2.0", "language": "python"}


def redact(body: dict[str, Any], patterns: list[PatternName] = ALL, redact_keys: list[str] | None = None) -> tuple[dict[str, Any], dict[str, int]]:
    """Runs a turn through the whole pipeline and returns its fields, without the SDK's own."""
    context = Context(sdk=SDK, patterns=patterns, redact_keys=redact_keys or [], max_event_bytes=256 * 1024)  # type: ignore[arg-type]
    built = build_turn(body, "2026-10-28T00:00:00.000Z", context, False)
    assert isinstance(built, BuiltOk), built
    value = dict(built.draft.event)
    for key in ("eventId", "schemaVersion", "kind", "capturedAt", "source", "sdk", "dropped", "truncated"):
        value.pop(key, None)
    counts = value.pop("redactions", {})
    if "status" not in body:
        value.pop("status", None)
    return value, counts


class TestPatterns:
    @pytest.mark.parametrize(
        ("pattern", "text", "expected"),
        [
            ("email", "write to jane.doe@acme.io today", "write to [EMAIL_1] today"),
            ("phone", "call +44 20 7946 0958", "call [PHONE_1]"),
            ("phone", "or (555) 123-4567", "or [PHONE_1]"),
            ("card", "card 4111 1111 1111 1111", "card [CARD_1]"),
            ("secret", "key sk-abcdefghijklmnopqrstuvwx", "key [SECRET_1]"),
            ("secret", f"mine is lsq_{'Ab9_-' * 8}xyz, careful", "mine is [SECRET_1], careful"),
            ("secret", "Authorization: Bearer abcdefghijklmnop1234", "Authorization: [SECRET_1]"),
            ("secret", "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U", "jwt [SECRET_1]"),
            ("secret", "k -----BEGIN RSA PRIVATE KEY-----\nMIIEow\nabc\n-----END RSA PRIVATE KEY----- end", "k [SECRET_1] end"),
            ("secret", f"token ya29.a0AfH6SM{'b' * 40}", "token [SECRET_1]"),
            ("secret", f"client GOCSPX-{'c' * 28}", "client [SECRET_1]"),
            ("secret", f"oauth gho_{'d' * 36}", "oauth [SECRET_1]"),
            ("secret", f"app ghs_{'e' * 36}", "app [SECRET_1]"),
            ("secret", "sts ASIAABCDEFGHIJKLMNOP", "sts [SECRET_1]"),
            ("secret", f"aws_secret_access_key = {'f' * 40}", "[SECRET_1]"),
            ("secret", f"stripe sk_test_{'g' * 24}", "stripe [SECRET_1]"),
            ("secret", f"hook whsec_{'h' * 32}", "hook [SECRET_1]"),
            ("secret", f"slack xapp-1-A0{'i' * 20}", "slack [SECRET_1]"),
            ("secret", f"https://hooks.slack.com/services/T0AAAA/B0BBBB/{'j' * 24}", "https://[SECRET_1]"),
            ("secret", f"npm npm_{'k' * 36}", "npm [SECRET_1]"),
            ("secret", f"gitlab glpat-{'l' * 20}", "gitlab [SECRET_1]"),
            ("secret", "authorization: bearer abcdefghijklmnop1234", "authorization: [SECRET_1]"),
            ("secret", "Authorization: Basic YWRtaW46aHVudGVyMi1zZWNyZXQ=", "Authorization: [SECRET_1]"),
            ("secret", "postgres://admin:hunter2@db.internal/app", "postgres://[SECRET_1]@db.internal/app"),
            ("secret", "REDIS_URL=redis://:s3cretPassw0rd@localhost:6379", "REDIS_URL=redis://[SECRET_1]@localhost:6379"),
            ("email", f"write to {'a.' * 40}jane@acme.io", "write to [EMAIL_1]"),
            ("email", "GET /users?email=jane.doe%40acme.io", "GET /users?email=[EMAIL_1]"),
            ("ip", "from 192.168.1.20", "from [IP_1]"),
            ("ip", "from 2001:db8:85a3::8a2e:370:7334 today", "from [IP_1] today"),
            ("ip", "gateway 2001:db8::1.", "gateway [IP_1]."),
        ],
    )
    def test_pattern(self, pattern: str, text: str, expected: str) -> None:
        value, counts = redact({"question": text})
        assert value["question"] == expected
        assert counts == {pattern: 1}

    def test_leaves_ordinary_text_alone(self) -> None:
        text = (
            "ref Rk8QwE3rTu6IoP1aSd5F7h on 2026-03-02, 41208 tokens, 3 sessions, t=1187, "
            "stamp 1790000000123, total 0.2750399999999999, phone-like 5551234567, card-like 4111111111111112, "
            "at 10:30:45, ratio 3:2:1, Foo::Bar and std::vector, a :: b, basic statistics, Basic plan, "
            "the bearer of bad news, see https://example.com:8080/path and ws://host:9000/feed, 100%, "
            # Code and prose the newer rules must leave alone.
            "rows = data[::10], xs[0::2], A::B.new, Ab::Cd, BAD:: ok, loopback ::1 and fe80::1, "
            "Basic Authentication/Authorization guide, the bearer responsibilities are clear, "
            "https://example.com:8080?x=a@b"
        )
        value, counts = redact({"answer": text})
        assert value["answer"] == text
        assert counts == {}

    def test_checks_cards_with_luhn(self) -> None:
        assert luhn("4111111111111111")
        assert not luhn("4111111111111112")

    def test_phone_and_ip_rewritten_match_exactly_what_0_1s_did(self) -> None:
        # Here in Python's engine; TypeScript's shorten.test.ts compares them in JavaScript's.
        assert first_difference(PATTERNS["phone"], V1["phone"]) is None
        assert first_difference(PATTERNS["ip"], V1["ip"]) is None
        assert sum(1 for _ in inputs()) == 144_994


class TestNumbering:
    def test_gives_the_same_value_the_same_number_across_fields_and_distinct_values_distinct_numbers(self) -> None:
        value, _ = redact({"question": "Is jane@acme.io the same as bob@acme.io?", "answer": "JANE@acme.io is not bob@acme.io"})
        assert value["question"] == "Is [EMAIL_1] the same as [EMAIL_2]?"
        assert value["answer"] == "[EMAIL_1] is not [EMAIL_2]"

    def test_gives_url_encoded_and_plain_forms_of_one_email_the_same_number(self) -> None:
        value, _ = redact({"question": "a%40b%40example.com is a%40b@example.com"})
        assert value["question"] == "[EMAIL_1] is [EMAIL_1]"

    def test_skips_numbers_of_placeholders_already_in_the_event(self) -> None:
        value, _ = redact({"question": "Earlier I wrote [EMAIL_1], now jane@acme.io"})
        assert value["question"] == "Earlier I wrote [EMAIL_1], now [EMAIL_2]"

    def test_redacts_object_keys_without_collisions(self) -> None:
        value, _ = redact({"verification": {"jane@acme.io": 1, "JANE@ACME.IO": 2, "[EMAIL_1]": 3}})
        assert value["verification"] == {"[EMAIL_2]": 1, "[EMAIL_3]": 2, "[EMAIL_1]": 3}


class TestScope:
    def test_never_changes_identifiers(self) -> None:
        body = {
            "conversationId": "conv1",
            "orgId": "org_1",
            "toolName": "ask_docs",
            "apiKeyId": "key-jane@acme.io",
            "client": {"name": "agent jane@acme.io"},
            "question": "jane@acme.io",
        }
        value, _ = redact(body)
        assert value == {**body, "question": "[EMAIL_1]"}

    def test_redacts_tool_inputs_and_error_messages_but_not_tool_names(self) -> None:
        value, _ = redact(
            {"toolCalls": [{"name": "lookup_jane@acme.io", "input": {"email": "jane@acme.io", "card": 4111111111111111}, "errorMessage": "no jane@acme.io"}]}
        )
        assert value["toolCalls"] == [{"name": "lookup_jane@acme.io", "input": {"email": "[EMAIL_1]", "card": "[CARD_1]"}, "errorMessage": "no [EMAIL_1]"}]

    def test_never_changes_numbers_outside_tool_inputs(self) -> None:
        value, _ = redact({"usage": {"inputTokens": 4111111111111111}, "latency": {"ms": 4111111111111111}})
        assert value == {"usage": {"inputTokens": 4111111111111111}, "latency": {"ms": 4111111111111111}}

    def test_can_be_turned_off_entirely_or_per_pattern(self) -> None:
        text = "jane@acme.io from 10.0.0.1"
        assert redact({"question": text}, resolve_patterns(False))[0]["question"] == text
        assert redact({"question": text}, resolve_patterns({"ip": False}))[0]["question"] == "[EMAIL_1] from 10.0.0.1"


# Security review finding 1: a secret in structured data has no recognisable format, but its key
# name gives it away.
def in_input(input: Any) -> tuple[dict[str, Any], dict[str, int]]:
    return redact({"toolCalls": [{"name": "tool", "input": input}]})


def input_of(value: dict[str, Any]) -> Any:
    return value["toolCalls"][0]["input"]


class TestSecretKeyNames:
    def test_catches_the_security_reviews_reproduction(self) -> None:
        secret = "aB3/" * 10
        value, _ = redact({"question": f"AWS_SECRET_ACCESS_KEY={secret}", "toolCalls": [{"name": "test", "input": {"AWS_SECRET_ACCESS_KEY": secret}}]})
        assert secret not in dumps(value)

    @pytest.mark.parametrize(
        "key",
        [
            "password", "dbPassword", "DB_PASSWORD", "new-password", "passwords", "passwd", "passphrase",
            "secret", "client_secret", "clientSecret", "secrets", "token", "accessToken", "refresh_token",
            "GITHUB_TOKEN", "x-csrf-token", "auth", "basicAuth", "oauth", "Authorization", "Proxy-Authorization",
            "apiKey", "api_key", "X-Api-Key", "apiKeys", "privateKey", "private_key", "secretKey",
            "AWS_SECRET_ACCESS_KEY", "signingKey", "encryptionKey", "credential", "credentials",
            "Cookie", "Set-Cookie", "cookies", "connectionString",
        ],
    )  # fmt: skip
    def test_replaces_the_value_under(self, key: str) -> None:
        value, counts = in_input({key: "opaque-value-123", "note": "kept"})
        assert input_of(value) == {key: "[SECRET_1]", "note": "kept"}
        assert counts == {"secret": 1}

    @pytest.mark.parametrize(
        "key",
        [
            "apiKeyId", "keyId", "key", "sortKey", "accessKeyId", "maxTokens", "tokenCount", "inputTokens", "token_type",
            "tokenizer", "author", "authorId", "sessionId", "secretQuestion", "passwordMinLength", "cookieConsent",
        ],
    )  # fmt: skip
    def test_leaves_the_value_alone_under(self, key: str) -> None:
        value, counts = in_input({key: "opaque-value-123"})
        assert input_of(value) == {key: "opaque-value-123"}
        assert counts == {}

    def test_replaces_whole_values_of_any_shape_at_any_depth_keeping_values_that_hold_nothing(self) -> None:
        value, counts = in_input(
            {
                "db": {"host": "db.internal", "password": 123456},
                "headers": [{"Authorization": "opaque"}],
                "credentials": {"user": "jane", "pass": "hunter2"},
                "secrets": ["a", "b"],
                "token": None,
                "apiKey": True,
                "passwd": "",
                "auth": {},
                "cookies": [],
                "secret": "[SECRET_7]",
            }
        )
        assert input_of(value) == {
            "db": {"host": "db.internal", "password": "[SECRET_1]"},
            "headers": [{"Authorization": "[SECRET_2]"}],
            "credentials": "[SECRET_3]",
            "secrets": "[SECRET_4]",
            "token": None,
            "apiKey": True,
            "passwd": "",
            "auth": {},
            "cookies": [],
            "secret": "[SECRET_7]",
        }
        assert counts == {"secret": 4}

    def test_gives_a_secret_the_same_number_under_any_key_and_in_text(self) -> None:
        key = "sk-abcdefghijklmnopqrstuvwx"
        value, _ = redact(
            {
                "question": f"use {key}",
                "toolCalls": [{"name": "a", "input": {"apiKey": key, "token": "other-opaque"}}, {"name": "b", "input": {"openai_api_key": key}}],
            }
        )
        assert value["question"] == "use [SECRET_1]"
        assert value["toolCalls"] == [
            {"name": "a", "input": {"apiKey": "[SECRET_1]", "token": "[SECRET_2]"}},
            {"name": "b", "input": {"openai_api_key": "[SECRET_1]"}},
        ]

    def test_applies_in_latency_verification_and_metadata_never_to_the_fields_the_sdk_defines(self) -> None:
        body = {
            "apiKeyId": "key_1",
            "usage": {"inputTokens": 10},
            "error": {"message": "failed", "type": "AuthError"},
            "latency": {"token": "a1"},
            "verification": {"checks": [{"secret": "b2"}]},
            "metadata": {"sessionToken": "c3", "plan": "pro"},
        }
        value, counts = redact(body)
        assert value == {
            **body,
            "latency": {"token": "[SECRET_1]"},
            "verification": {"checks": [{"secret": "[SECRET_2]"}]},
            "metadata": {"sessionToken": "[SECRET_3]", "plan": "pro"},
        }
        assert counts == {"secret": 3}

    def test_is_part_of_the_secret_pattern(self) -> None:
        body = {"toolCalls": [{"name": "tool", "input": {"password": "hunter2"}}]}
        assert redact(body, resolve_patterns({"secret": False}))[0] == body
        assert redact(body, resolve_patterns(False))[0] == body
        assert input_of(redact(body, resolve_patterns({"email": False}))[0]) == {"password": "[SECRET_1]"}


class TestLongSecrets:
    def test_redacts_a_jwt_longer_than_8_kb(self) -> None:
        jwt = f"eyJhbGciOiJIUzI1NiJ9.eyJ{'a' * 9400}.{'b' * 43}"
        assert len(jwt) == 9468
        value, counts = redact({"question": f"token {jwt} end"})
        assert value["question"] == "token [SECRET_1] end"
        assert counts == {"secret": 1}

    def test_redacts_a_pem_block_longer_than_8_kb(self) -> None:
        pem = f"-----BEGIN PRIVATE KEY-----\n{'A' * 12000}\n-----END PRIVATE KEY-----"
        assert redact({"question": pem})[0]["question"] == "[SECRET_1]"


def pem(label: str) -> str:
    return f"-----BEGIN {label}PRIVATE KEY-----\n{'ABCD' * 16}\n-----END {label}PRIVATE KEY-----"


class TestATokenCutOffAfterAWrittenLineEnd:
    def test_is_masked_the_escapes_letter_isnt_part_of_the_token(self) -> None:
        for escape in ("\\n", "\\r", "\\t"):
            assert Redactor(ALL).cut_text(f"a{escape}sk-abcdefgh", 14) == f"a{escape}[SECRET_1]"


class TestAPemKeyCutOff:
    def test_is_masked_whole_when_the_cut_falls_anywhere_in_its_end_line(self) -> None:
        for label in ("", "RSA ", "OPENSSH "):
            key = pem(label)
            for cut in range(key.rindex("-----END "), len(key)):
                assert Redactor(ALL).cut_text(key[:cut], cut) == "[SECRET_1]", (label, cut)

    def test_is_masked_after_a_whole_one_and_a_whole_closing_line_still_closes_it(self) -> None:
        key = pem("")
        assert Redactor(ALL).cut_text(f"{key} and {key[:-5]}", len(key) * 2) == "[SECRET_1] and [SECRET_2]"
        # The pattern can't match this block (its body holds -----), but its whole END line closes it.
        closed = "-----BEGIN PRIVATE KEY-----\nAB-----CD\n-----END PRIVATE KEY-----\nthen prose"
        assert Redactor(ALL).cut_text(closed, len(closed)) == closed


class TestJavaScriptsRules:
    """Python only: the rules TypeScript gets from JavaScript itself."""

    def test_patterns_are_typescripts_with_javascripts_whitespace_written_out(self) -> None:
        if not node_available():
            pytest.skip("node isn't installed: the patterns weren't compared with TypeScript's")
        script = (
            "const { PATTERNS } = await import(process.argv[1]);"
            "process.stdout.write(JSON.stringify(Object.fromEntries(Object.entries(PATTERNS).map(([k, v]) => [k, v.source]))))"
        )
        source = (REPO / "packages" / "lsquare" / "src" / "redact.ts").as_uri()
        run = subprocess.run(["node", "--import", "tsx", "--input-type=module", "-e", script, source], cwd=REPO, capture_output=True, timeout=60)
        assert run.returncode == 0, run.stderr.decode("utf-8", "replace")
        assert json.loads(run.stdout) == SOURCES

    def test_digits_and_word_boundaries_are_ascii(self) -> None:
        assert Redactor(ALL).text("call +٤٤ ٢٠ ٧٩٤٦ ٠٩٥٨") == (
            "call +٤٤ ٢٠ ٧٩٤٦ ٠٩٥٨"
        )
        assert Redactor(ALL).text("caféjane@acme.io") == "café[EMAIL_1]"

    def test_whitespace_is_javascripts(self) -> None:
        assert all(pattern.flags & 256 for pattern in PATTERNS.values())  # re.ASCII
        assert WS.startswith("\\t\\n\\v\\f\\r ")
        assert Redactor(ALL).text("Authorization: Bearer abcdefghijklmnop1234") == "Authorization: [SECRET_1]"
        assert Redactor(ALL).text("Authorization: Bearer\u0085abcdefghijklmnop1234") == "Authorization: Bearer\u0085abcdefghijklmnop1234"
        assert Redactor(ALL).text("Authorization: Bearer﻿abcdefghijklmnop1234") == "Authorization: [SECRET_1]"

    def test_bounded_repeats_count_utf16_units_as_in_javascript(self) -> None:
        # A URL's user name is at most 256 units: 128 emoji are 256, 129 are 258.
        fits = "redis://" + "\U0001f600" * 128 + ":pw@host"
        too_long = "redis://" + "\U0001f600" * 129 + ":pw@host"
        assert Redactor(ALL).text(fits) == "redis://[SECRET_1]@host"
        assert Redactor(ALL).text(too_long) == too_long

    def test_a_pair_held_as_two_code_points_is_redacted_as_one_character(self) -> None:
        pair = "\ud83d" + "\ude00"
        assert Redactor(ALL).text("redis://" + pair * 128 + ":pw@host") == "redis://[SECRET_1]@host"


def timed(text: str, patterns: list[PatternName] = ALL) -> float:
    """Smallest of three timings, to reduce noise."""
    best = float("inf")
    for _ in range(3):
        started = time.perf_counter()
        Redactor(patterns).text(text)
        best = min(best, time.perf_counter() - started)
    return best


class TestPerformance:
    """TypeScript's timings, with Python's regex engine given more room: what matters is that no
    pattern is worse than linear."""

    def test_numbers_10000_distinct_emails_in_linear_time(self) -> None:
        text = " ".join(f"u{i}@acme.io" for i in range(10_000))
        redactor = Redactor(ALL)
        started = time.perf_counter()
        out = redactor.text(text)
        assert time.perf_counter() - started < 1.0
        assert redactor.counts == {"email": 10_000}
        assert out.endswith("[EMAIL_10000]")

    def test_scales_linearly_4x_the_input_takes_well_under_16x_the_time(self) -> None:
        generators: dict[str, Callable[[int], str]] = {
            "distinctEmails": lambda n: " ".join(f"u{i}@acme.io" for i in range(n // 12)),
            "emailNoDomain": lambda n: "a" * (n - 1) + "@",
            "jwtStarts": lambda n: "eyJ" * (n // 3),
            "jwtMissingSegments": lambda n: ".eyJaaaaaaaaaaaa" * (n // 16),
            "pemNoEnd": lambda n: "-----BEGIN PRIVATE KEY-----" * (n // 27),
            "pemLongBodyNoEnd": lambda n: "-----BEGIN PRIVATE KEY-----" + "A" * n,
            "cardStarts": lambda n: "4111 " * (n // 5),
            "ipv6Compressed": lambda n: " 1::" * (n // 4),
            "ipv6GroupsBadEnd": lambda n: (" " + "aaaa:" * 7 + "g") * (n // 37),
            "urlCredentialsNoAt": lambda n: "://a:" * (n // 5),
            "basicLetters": lambda n: ("Basic " + "a" * 600 + " ") * (n // 607),
            "encodedEmailNoDomain": lambda n: " a" + "%40a" * (n // 4),
        }
        for name, generate in generators.items():
            small = timed(generate(64 * 1024))
            large = timed(generate(256 * 1024))
            assert large / max(small, 0.0005) < 10, name

    def test_stays_fast_on_long_adversarial_strings(self) -> None:
        size = 256 * 1024
        inputs = [
            "a" * (size - 1) + "@",
            "eyJ" * (size // 3),
            "sk-" * (size // 3),
            "-----END PRIVATE KEY-----" + "-----BEGIN PRIVATE KEY-----" * (size // 27),
            "4" * size,
            "1." * (size // 2),
            "+1 " * (size // 3),
            "Bearer " * (size // 7),
        ]
        for text in inputs:
            started = time.perf_counter()
            Redactor(ALL).text(text)
            assert time.perf_counter() - started < 1.0, text[:20]

    def test_doesnt_try_every_split_of_a_run_of_digits_after_a_plus(self) -> None:
        # 0.1's phone pattern did, hundreds of splits for each + before failing at the letter, which took
        # 10 to 45 times plain text's time. Without them, under 4.
        size = 256 * 1024

        def fill(unit: str) -> str:
            return (unit * (size // len(unit) + 1))[:size]

        plain = max(timed(fill("+1 "), ["phone"]), 0.0005)
        for unit in ("+1" + "1" * 12 + "a", "+1" + "1" * 17 + "a", "+11 " + "1" * 16 + "a"):
            assert timed(fill(unit), ["phone"]) / plain < 10, unit
