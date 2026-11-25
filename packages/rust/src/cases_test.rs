//! The shared cases in `packages/contract/cases`: TypeScript's `cases.test.ts` and Python's
//! `cases_test.py` pass the same files. For a new language, identity with them is measured, and
//! the guarantees are required (see `README.md`, "The shared cases"):
//!
//! - every event passes the ingestion's checks, the contract's `validate.ts`, run through Node;
//! - every string is within its field's limit;
//! - nothing the expected event redacted shows in this SDK's;
//! - and each case gives the expected event, except those listed in `DIFFERENT`, each with its reason.
//!
//! The contract is found through one setting, `LSQUARE_CONTRACT_DIR` (`../contract` by default).

use std::collections::{BTreeMap, BTreeSet};
use std::io::Write;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::LazyLock;

use regex::Regex;
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

use crate::event::{
    after_hook, build_call, finish_call, turn::build_turn, Built, CallData, CallHead, CallShaper,
    Context, ResultIn,
};
use crate::json::stringify;
use crate::redact::{resolve_keys, resolve_patterns};
use crate::testing::{difference, expand, parse, text, to_value, Held, Js};

const CAPTURED_AT: &str = "2026-10-28T00:00:00.000Z";
const FILES: [&str; 5] = [
    "calls.json",
    "ids.json",
    "numbers.json",
    "redaction.json",
    "shortening.json",
];

/// The cases whose event isn't the expected one, and why. Each still passes every other check.
/// A case that isn't listed and differs fails the test; so does a listed one that no longer differs.
const DIFFERENT: [(&str, &str, &str); 5] = [
    (
        "ids.json",
        "a turnId is measured as JSON writes it: 50,000 lone surrogates are 300,000 bytes, escaped",
        "A Rust string can't hold a lone surrogate, and serde_json refuses one: here each is U+FFFD, three bytes where JSON's escape takes six, so this turnId fits and is kept.",
    ),
    (
        "numbers.json",
        "numbers are written as JavaScript writes them: shortest digits, no .0, exponents only below 1e-6 or from 1e21, -0 as 0",
        "serde_json refuses a number too large for a double, where JavaScript reads infinity: the case's number is given as null, which is no result to measure. The other numbers are written as JavaScript writes them.",
    ),
    (
        "numbers.json",
        "an integer past 2^53 is the nearest double, and one too large for a double isn't JSON",
        "The number too large for a double is given as null (see above), which is JSON, so nothing is dropped. The integer past 2^53 is the nearest double here too.",
    ),
    (
        "redaction.json",
        "secrets that differ only in a lone surrogate differ",
        "Both lone surrogates are U+FFFD in Rust, so the two secrets are one value and share a number.",
    ),
    (
        "shortening.json",
        "a lone surrogate is one character",
        "The lone surrogates are U+FFFD in Rust: one character each, as counted, but another character.",
    ),
];

pub(crate) fn contract_dir() -> PathBuf {
    match std::env::var_os("LSQUARE_CONTRACT_DIR") {
        Some(dir) => PathBuf::from(dir),
        None => PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../contract"),
    }
}

pub(crate) fn sdk() -> Value {
    serde_json::json!({ "name": "lsquare", "version": env!("CARGO_PKG_VERSION"), "language": "rust" })
}

struct Case {
    file: &'static str,
    name: String,
    case: Js,
}

fn cases() -> Vec<Case> {
    let dir = contract_dir().join("cases");
    let mut found: Vec<String> = std::fs::read_dir(&dir)
        .unwrap_or_else(|error| panic!("the shared cases aren't at {}: {error}", dir.display()))
        .filter_map(|entry| entry.ok()?.file_name().into_string().ok())
        .filter(|name| name.ends_with(".json"))
        .collect();
    found.sort();
    assert_eq!(found, FILES, "the files both SDKs read");
    let mut all = Vec::new();
    for file in FILES {
        let Js::Array(list) = parse(&std::fs::read_to_string(dir.join(file)).expect("a case file"))
        else {
            panic!("{file} isn't a list")
        };
        for case in list {
            let name = case
                .get("name")
                .and_then(Js::as_text)
                .expect("a case's name");
            all.push(Case { file, name, case });
        }
    }
    all
}

/// A turn's event ID: its `k/` key with a conversation and a turnId, or else random.
fn turn_event_id(ids: &Map<String, Value>, turn_id: Option<&str>) -> String {
    match (ids.get("conversationId").and_then(Value::as_str), turn_id) {
        (Some(conversation), Some(turn)) => {
            let org = ids.get("orgId").map_or("null".to_owned(), stringify);
            let json = format!("[{org},{},{}]", stringify(conversation), stringify(turn));
            let digest: String = Sha256::digest(json.as_bytes())
                .iter()
                .map(|byte| format!("{byte:02x}"))
                .collect();
            format!("k/{digest}")
        }
        _ => uuid::Uuid::new_v4().to_string(),
    }
}

/// What a case gave: its event, or why it was dropped.
pub(crate) struct Outcome {
    pub(crate) built: Result<Map<String, Value>, String>,
    /// A field `before_send` didn't get as the case says it must.
    hook_seen: Option<String>,
}

fn context(case: &Js, held: &mut Held) -> Context {
    let options = case
        .get("options")
        .map(|options| to_value(&expand(options), held));
    let redact = options.as_ref().and_then(|options| options.get("redact"));
    let keys = match options
        .as_ref()
        .and_then(|options| options.get("redactKeys"))
    {
        Some(Value::String(name)) => resolve_keys([name.as_str()]),
        Some(Value::Array(names)) => resolve_keys(names.iter().filter_map(Value::as_str)),
        _ => Vec::new(),
    };
    Context {
        sdk: sdk(),
        patterns: resolve_patterns(redact),
        redact_keys: keys,
        max_event_bytes: 256 * 1024,
    }
}

/// What a case's call was, around its data, with the defaults the README gives.
fn head_of(call: &Map<String, Value>) -> CallHead {
    let text = |key: &str, default: &str| {
        call.get(key)
            .map_or(Some(default.to_owned()), |value| {
                value.as_str().map(str::to_owned)
            })
            .unwrap_or_default()
    };
    CallHead {
        call_id: text("callId", "case-call"),
        captured_at: CAPTURED_AT.into(),
        tool_name: text("toolName", "search_orders"),
        status: text("status", "completed"),
        duration_ms: call.get("durationMs").cloned().unwrap_or(Value::from(1)),
        ids: Some(
            call.get("ids")
                .cloned()
                .unwrap_or_else(|| Value::Object(Map::new())),
        ),
        turn_ref: call.get("turnRef").cloned(),
        server: call.get("server").cloned(),
        client: call.get("client").cloned(),
        protocol_version: call.get("protocolVersion").cloned(),
        session_id: call.get("sessionId").cloned(),
        session_source: call
            .get("sessionSource")
            .and_then(Value::as_str)
            .map(str::to_owned),
        round: call.get("round").and_then(Value::as_str).map(str::to_owned),
        extra: call.get("extra").and_then(Value::as_str).map(str::to_owned),
        metadata: call.get("metadata").cloned(),
        diagnostics: call.get("diagnostics") == Some(&Value::Bool(true)),
    }
}

pub(crate) fn build(case: &Js) -> Outcome {
    let mut held = Held::default();
    let context = context(case, &mut held);
    let hook = case.get("hook");
    let mut hook_seen = None;
    let built: Built = if let Some(turn) = case.get("turn") {
        let input = to_value(&expand(turn), &mut held);
        build_turn(&input, CAPTURED_AT, &context, hook.is_some(), turn_event_id)
    } else {
        let call = to_value(
            &expand(case.get("call").expect("a turn or a call")),
            &mut held,
        );
        let call = call.as_object().expect("a call's fields");
        let head = head_of(call);
        let data = CallData {
            tool_description: call.get("toolDescription"),
            server_instructions: call.get("serverInstructions"),
            input: call.get("input"),
            result: call.get("result").map(ResultIn::Json),
            error: call.get("error"),
            task: call.get("task"),
            intent: call.get("intent"),
            agent: call.get("agent"),
        };
        build_call(&data, &head, &context, hook.is_some())
    };
    let built = match (built, hook) {
        (Ok(draft), Some(hook)) => {
            // A `before_send` that returns the event as it got it.
            let given = draft.event.clone();
            if let Some(Js::Object(seen)) = hook.get("seen").map(expand) {
                let got = parse(&stringify(&given));
                for (field, expected) in seen {
                    let field = text(&field);
                    let found = match got.get(&field) {
                        Some(actual) => difference(actual, &expected, &format!("hook.{field}")),
                        None => Some(format!("hook.{field}: missing")),
                    };
                    hook_seen = hook_seen.or(found);
                }
            }
            after_hook(&given, draft, &context)
        }
        (built, _) => built,
    };
    Outcome {
        built: built
            .map(|draft| draft.event)
            .map_err(|refused| refused.code.to_owned()),
        hook_seen,
    }
}

static UUID: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\A[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\z")
        .expect("UUID pattern")
});

/// Where the event differs from the one the case expects, with what the README leaves out of the
/// comparison: its time, its SDK, and a random ID.
fn compare(event: &Map<String, Value>, expected: &Js) -> Option<String> {
    let mut actual = event.clone();
    actual.shift_remove("sdk");
    if actual.shift_remove("capturedAt") != Some(Value::from(CAPTURED_AT)) {
        return Some("$.capturedAt isn't the time given".into());
    }
    let mut expected = expand(expected);
    if expected.get("eventId").and_then(Js::as_text).as_deref() == Some("$uuid") {
        let id = actual
            .get("eventId")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_owned();
        if !UUID.is_match(&id) {
            return Some(format!("$.eventId: {id:?} isn't a random UUID"));
        }
        if let Js::Object(entries) = &mut expected {
            for (key, value) in entries {
                if text(key) == "eventId" {
                    *value = Js::string(&id);
                }
            }
        }
    }
    // As written, which is what leaves the app: `-0` goes as `0`.
    difference(&parse(&stringify(&actual)), &expected, "$")
}

// The limits: what's kept of a string, without its cut marker and without what redaction put in
// place of its values, is within its field's limit. A placeholder can be longer than what it
// replaces, so the string as sent can be longer than that, in every SDK.

static MARKER: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"…\[(?:\+[0-9]{1,16} chars|cut)\]\z").expect("marker pattern"));
static PLACEHOLDER: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r#"(?:\\*["'])?\[(?:EMAIL|PHONE|CARD|SECRET|IP)_[0-9]{1,9}\](?:\\*["'])?"#)
        .expect("placeholder pattern")
});

fn kept_length(text: &str) -> usize {
    let body = MARKER
        .find(text)
        .map_or(text, |marker| &text[..marker.start()]);
    PLACEHOLDER.replace_all(body, "").chars().count()
}

fn check_strings(value: &Value, limit: usize, path: &str, over: &mut Vec<String>) {
    match value {
        Value::String(text) => {
            let kept = kept_length(text);
            if kept > limit {
                over.push(format!("{path}: {kept} characters kept, over {limit}"));
            }
        }
        Value::Array(items) => items.iter().enumerate().for_each(|(index, item)| {
            check_strings(item, limit, &format!("{path}[{index}]"), over)
        }),
        Value::Object(entries) => entries.values().enumerate().for_each(|(index, item)| {
            check_strings(item, limit, &format!("{path}.#{index}"), over)
        }),
        _ => {}
    }
}

fn turn_limits(fields: &Map<String, Value>, path: &str, over: &mut Vec<String>) {
    for key in [
        "question",
        "answer",
        "error",
        "toolCalls",
        "latency",
        "verification",
    ] {
        if let Some(value) = fields.get(key) {
            check_strings(value, 16_000, &format!("{path}{key}"), over);
        }
    }
}

/// Every string over its field's limit: none, for an event within them.
fn over_limits(event: &Map<String, Value>) -> Vec<String> {
    let mut over = Vec::new();
    if event.get("kind") == Some(&Value::from("turn")) {
        turn_limits(event, "", &mut over);
    } else {
        for (key, limit) in [
            ("input", 16_000),
            ("result", 4_000),
            ("error", 4_000),
            ("intent", 500),
            ("toolDescription", 1_000),
            ("serverInstructions", 2_000),
        ] {
            if let Some(value) = event.get(key) {
                check_strings(value, limit, key, &mut over);
            }
        }
        if let Some(Value::Object(agent)) = event.get("agent") {
            turn_limits(agent, "agent.", &mut over);
        }
    }
    let bytes = stringify(event).len();
    if bytes > 256 * 1024 {
        over.push(format!("the event is {bytes} bytes"));
    }
    over
}

// Redaction: nothing the expected event replaced shows here. Where both events have a string, this
// SDK's may be cut shorter or have more of it replaced, never less: what it shows between its
// placeholders must be found, in order, in what the expected string shows between its own. A value
// the expected event replaced whole must be replaced whole. The paths redaction leaves alone
// (REDACTION.md) aren't compared.

const NEVER_REDACTED: [&str; 28] = [
    "eventId",
    "schemaVersion",
    "kind",
    "capturedAt",
    "source",
    "sdk",
    "callId",
    "toolName",
    "orgId",
    "userId",
    "conversationId",
    "turnId",
    "turnRef",
    "sessionId",
    "sessionSource",
    "apiKeyId",
    "surface",
    "client",
    "server",
    "protocolVersion",
    "status",
    "durationMs",
    "round",
    "extra",
    "identity",
    "redactions",
    "dropped",
    "truncated",
];

/// What a string shows: its text between placeholders, without its cut marker.
fn shown(text: &str) -> Vec<String> {
    let body = MARKER
        .find(text)
        .map_or(text, |marker| &text[..marker.start()]);
    PLACEHOLDER
        .split(body)
        .filter(|piece| !piece.is_empty())
        .map(str::to_owned)
        .collect()
}

fn shows_no_more(actual: &str, expected: &str) -> bool {
    let expected = shown(expected);
    let (mut piece, mut from) = (0, 0);
    for wanted in shown(actual) {
        loop {
            let Some(text) = expected.get(piece) else {
                return false;
            };
            match text[from..].find(&wanted) {
                Some(found) => {
                    from += found + wanted.len();
                    break;
                }
                None => {
                    piece += 1;
                    from = 0;
                }
            }
        }
    }
    true
}

fn is_placeholder(value: &Value) -> bool {
    value.as_str().is_some_and(|text| {
        PLACEHOLDER
            .find(text)
            .is_some_and(|found| found.start() == 0 && found.end() == text.len())
    })
}

fn leaks(actual: &Value, expected: &Value, path: &str, found: &mut Vec<String>) {
    match (actual, expected) {
        (Value::String(actual), Value::String(expected)) => {
            if !shows_no_more(actual, expected) {
                found.push(format!(
                    "{path}: shows text the expected event replaced or cut"
                ));
            }
        }
        (Value::Array(actual), Value::Array(expected)) => {
            for (index, (actual, expected)) in actual.iter().zip(expected).enumerate() {
                leaks(actual, expected, &format!("{path}[{index}]"), found);
            }
        }
        (Value::Object(actual), Value::Object(expected)) => {
            for (index, (key, value)) in actual.iter().enumerate() {
                if path == "$" && NEVER_REDACTED.contains(&key.as_str()) {
                    continue;
                }
                // The same key, or the one in its place when a key was replaced.
                let other = expected.get(key).map(|other| (key, other)).or_else(|| {
                    expected
                        .iter()
                        .nth(index)
                        .filter(|(name, _)| !actual.contains_key(*name))
                });
                let Some((name, other)) = other else { continue };
                if !shows_no_more(key, name) {
                    found.push(format!(
                        "{path}: a key shows text the expected event replaced"
                    ));
                }
                leaks(value, other, &format!("{path}.{key}"), found);
            }
        }
        // Replaced whole there, so replaced whole here.
        (actual, expected) if is_placeholder(expected) && !is_placeholder(actual) => {
            found.push(format!("{path}: not replaced whole"))
        }
        _ => {}
    }
}

// The ingestion's checks.

const VALIDATE: &str = r"
import { readFileSync } from 'node:fs'
const { validateEvent } = await import(process.argv[1])
const lines = readFileSync(0, 'utf8').split('\n').filter(Boolean)
process.stdout.write(JSON.stringify(lines.map((line) => {
  const result = validateEvent(JSON.parse(line))
  return result.ok ? 'valid' : result.reason
})))
";

/// As `cases_test.py` runs TypeScript on the same cases: each event's JSON text, or the code it is dropped with.
pub(crate) const TYPESCRIPT: &str = r"
import { readFileSync } from 'node:fs'
const [src, file, sdk] = process.argv.slice(1)
const { afterHook, buildCall, buildTurn } = await import(`${src}/event.ts`)
const { resolveKeys, resolvePatterns } = await import(`${src}/redact.ts`)
const CAPTURED_AT = '2026-10-28T00:00:00.000Z'
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
  const hook = test.hook !== undefined
  let built
  if (test.turn) built = buildTurn(expand(test.turn), CAPTURED_AT, context, hook)
  else {
    const { input, result, ...rest } = expand(test.call)
    const record = { callId: 'case-call', capturedAt: CAPTURED_AT, toolName: 'search_orders', status: 'completed', durationMs: 1, ids: {}, diagnostics: false, ...rest,
      ...(input === undefined ? {} : { input: { value: input } }), ...(result === undefined ? {} : { result: { value: result } }) }
    built = buildCall(record, context, hook)
  }
  if (hook && built.ok) built = afterHook(structuredClone(built.draft.event), built.draft, context)
  if (!built.ok) return built.code
  const event = built.draft.event
  if (test.event?.eventId === '$uuid') event.eventId = '$uuid'
  return JSON.stringify(event)
})
process.stdout.write(JSON.stringify(written))
";

fn node_available() -> bool {
    Command::new("node")
        .arg("--version")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .is_ok_and(|status| status.success())
}

/// Whether Node's checks can be skipped: only away from CI, where a machine may lack Node.
pub(crate) fn skip_without_node(what: &str) -> bool {
    if node_available() {
        return false;
    }
    assert!(
        std::env::var_os("CI").is_none(),
        "node isn't installed: {what} can't be checked"
    );
    eprintln!("skipped: node isn't installed, so {what} wasn't checked");
    true
}

pub(crate) fn file_url(path: &std::path::Path) -> String {
    let path = path
        .canonicalize()
        .unwrap_or_else(|error| panic!("{}: {error}", path.display()));
    format!("file://{}", path.display())
}

/// Runs a script with tsx, from the contract's folder, where `npm ci` at the repository's root put it.
pub(crate) fn node(script: &str, args: &[String], input: &str) -> String {
    let mut child = Command::new("node")
        .args(["--import", "tsx", "--input-type=module", "-e", script])
        .args(args)
        .current_dir(contract_dir())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("node starts");
    let mut stdin = child.stdin.take().expect("node's input");
    let input = input.to_owned();
    let writer = std::thread::spawn(move || stdin.write_all(input.as_bytes()));
    let output = child.wait_with_output().expect("node runs");
    let _ = writer.join();
    assert!(
        output.status.success(),
        "node failed (is tsx installed? run `npm ci` at the repository's root):\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).expect("node writes UTF-8")
}

/// What the ingestion says of each event, given this SDK's JSON: `valid`, or why it's rejected.
pub(crate) fn ingestion(events: &[String]) -> Vec<String> {
    let validator = file_url(&contract_dir().join("src/validate.ts"));
    let out = node(VALIDATE, &[validator], &(events.join("\n") + "\n"));
    let results: Vec<String> = serde_json::from_str(&out).expect("the validator's answers");
    assert_eq!(results.len(), events.len());
    results
}

#[test]
fn every_case_gives_its_event_or_its_error() {
    let mut differences: BTreeMap<(String, String), String> = BTreeMap::new();
    let mut identical = 0;
    let mut total = 0;
    for Case { file, name, case } in cases() {
        total += 1;
        let outcome = build(&case);
        let found = match (
            &outcome.built,
            case.get("error").and_then(Js::as_text),
            case.get("event"),
        ) {
            (Err(code), Some(expected), _) => {
                (*code != expected).then(|| format!("dropped as {code}, not {expected}"))
            }
            (Err(code), None, _) => Some(format!("dropped as {code}")),
            (Ok(_), Some(expected), _) => Some(format!("gave an event, not {expected}")),
            (Ok(event), None, Some(expected)) => outcome
                .hook_seen
                .clone()
                .or_else(|| compare(event, expected)),
            (Ok(_), None, None) => Some("the case expects neither an event nor an error".into()),
        };
        match found {
            None => identical += 1,
            Some(found) => {
                differences.insert((file.to_owned(), name), found);
            }
        }
    }
    println!("{identical} of {total} cases give the expected event or error");
    let listed: BTreeSet<(String, String)> = DIFFERENT
        .iter()
        .map(|(file, name, _)| ((*file).to_owned(), (*name).to_owned()))
        .collect();
    let unlisted: Vec<String> = differences
        .iter()
        .filter(|(case, _)| !listed.contains(case))
        .map(|((file, name), found)| format!("{file}: {name}\n    {found}"))
        .collect();
    let stale: Vec<&(String, String)> = listed
        .iter()
        .filter(|case| !differences.contains_key(case))
        .collect();
    assert!(
        unlisted.is_empty(),
        "{} cases differ from the expected event and aren't listed in DIFFERENT:\n{}",
        unlisted.len(),
        unlisted.join("\n")
    );
    assert!(
        stale.is_empty(),
        "listed in DIFFERENT, but they give the expected event now: {stale:?}"
    );
}

/// The wrapper reads a call in two steps (`CallShaper`): its intent and its arguments before the
/// tool takes them, the rest once it has answered. For every case's call the wrapper could make,
/// that gives the event reading it all at once gives, byte for byte. (One kind of call isn't
/// among them, and differs: one whose scan budget runs out between its error and its arguments,
/// which the two read in the other order.)
#[test]
fn a_call_read_in_two_steps_gives_the_same_event() {
    // And calls the shared cases don't have: an intent beside an error, each cut or refused.
    let more = r#"[
        { "name": "an intent and an error's message, both cut", "call": { "intent": { "$prose": 600 }, "status": "error", "error": { "category": "tool_error", "message": { "$prose": 20000 } }, "input": { "q": { "$prose": 17000 } }, "result": { "content": [{ "type": "text", "text": { "$prose": 5000 } }] } } },
        { "name": "an intent that is cut and an error that isn't an object", "call": { "intent": { "$prose": 600 }, "status": "error", "error": "broken", "input": { "q": 1 } } },
        { "name": "an intent that isn't text and an error that is cut", "call": { "intent": 5, "status": "error", "error": { "category": "protocol", "message": { "$prose": 20000 } } } },
        { "name": "an intent far past the scan budget and an error's message, both cut", "call": { "intent": { "$prose": 1200000 }, "status": "error", "error": { "category": "protocol", "message": { "$prose": 1200000 } }, "input": { "q": { "$prose": 1200000 } }, "result": { "content": [{ "type": "text", "text": { "$prose": 1200000 } }] } } },
        { "name": "an intent with a secret and an error with one", "call": { "intent": "Write to ann@example.com.", "status": "error", "error": { "category": "thrown", "message": "No account for bob@example.com." }, "input": { "to": "cat@example.com" } } }
    ]"#;
    let Js::Array(more) = parse(more) else {
        panic!("the extra cases are a list")
    };
    let more = more.into_iter().map(|case| Case {
        file: "cases_test.rs",
        name: case
            .get("name")
            .and_then(Js::as_text)
            .expect("a case's name"),
        case,
    });
    let mut compared = 0;
    for Case { file, name, case } in cases().into_iter().chain(more) {
        let Some(call) = case.get("call") else {
            continue;
        };
        let mut held = Held::default();
        let call = to_value(&expand(call), &mut held);
        let call = call.as_object().expect("a call's fields");
        // What the wrapper never gives: an agent's turn, arguments that aren't an object.
        let arguments = match call.get("input") {
            None => None,
            Some(Value::Object(arguments)) => Some(arguments),
            Some(_) => continue,
        };
        if case.get("hook").is_some() || call.contains_key("agent") {
            continue;
        }
        let context = context(&case, &mut held);
        let head = head_of(call);
        let rest = |input, intent| CallData {
            tool_description: call.get("toolDescription"),
            server_instructions: call.get("serverInstructions"),
            input,
            result: call.get("result").map(ResultIn::Json),
            error: call.get("error"),
            task: call.get("task"),
            intent,
            agent: None,
        };
        let at_once = build_call(
            &rest(call.get("input"), call.get("intent")),
            &head,
            &context,
            false,
        );
        let mut shaper = CallShaper::new(&context);
        // The intent first, then the arguments, as the wrapper reads them.
        if let Some(intent) = call.get("intent") {
            shaper.intent(intent);
        }
        if call.contains_key("input") {
            shaper.arguments(arguments);
        }
        let in_steps = finish_call(shaper.finish(&rest(None, None)), &head, &context, false);
        let written = |built: Built| {
            built
                .map(|draft| stringify(&draft.event))
                .map_err(|refused| refused.code)
        };
        assert_eq!(written(in_steps), written(at_once), "{file}: {name}");
        compared += 1;
    }
    assert!(compared > 300, "{compared}");
    println!("{compared} calls give the same event read in two steps");
}

#[test]
fn every_string_is_within_its_limit() {
    let mut over = Vec::new();
    for Case { file, name, case } in cases() {
        if let Ok(event) = build(&case).built {
            for found in over_limits(&event) {
                over.push(format!("{file}: {name}\n    {found}"));
            }
        }
    }
    assert!(over.is_empty(), "{}", over.join("\n"));
}

#[test]
fn nothing_the_expected_event_redacted_shows() {
    let mut found = Vec::new();
    let mut checked = 0;
    for Case { file, name, case } in cases() {
        let (Ok(event), Some(expected)) = (build(&case).built, case.get("event")) else {
            continue;
        };
        // What a Rust app would hold of the expected event: a lone surrogate is U+FFFD on both sides.
        let expected = to_value(&expand(expected), &mut Held::default());
        checked += 1;
        let mut leaked = Vec::new();
        leaks(&Value::Object(event), &expected, "$", &mut leaked);
        found.extend(
            leaked
                .into_iter()
                .map(|leak| format!("{file}: {name}\n    {leak}")),
        );
    }
    assert!(found.is_empty(), "{}", found.join("\n"));
    println!("none of {checked} events shows what the expected one redacted");
}

#[test]
fn the_leak_check_tells_a_leak() {
    assert!(shows_no_more(
        "From [EMAIL_1] to bob",
        "From [EMAIL_1] to bob"
    ));
    assert!(shows_no_more(
        "From [EMAIL_1] to [EMAIL_2]",
        "From [EMAIL_1] to bob"
    ));
    assert!(shows_no_more(
        "From [EMAIL_1]…[+9 chars]",
        "From [EMAIL_1] to bob"
    ));
    // Part of a value shown, a value shown whole, and text shown past the expected cut.
    assert!(!shows_no_more(
        "From lice@example.com to bob",
        "From [EMAIL_1] to bob"
    ));
    assert!(!shows_no_more(
        "{\"pin\": 1234}",
        "{\"pin\": \"[SECRET_1]\"}"
    ));
    assert!(!shows_no_more("abcdef", "abc…[+3 chars]"));
    let mut found = Vec::new();
    leaks(
        &serde_json::json!({ "input": { "pin": 1234, "a@b.co": 1 } }),
        &serde_json::json!({ "input": { "pin": "[SECRET_1]", "[EMAIL_1]": 1 } }),
        "$",
        &mut found,
    );
    assert_eq!(found.len(), 2, "{found:?}");
}

#[test]
fn the_ingestion_accepts_each_event() {
    if skip_without_node("the contract's validator (validate.ts)") {
        return;
    }
    let mut names = Vec::new();
    let mut events = Vec::new();
    for Case { file, name, case } in cases() {
        if let Ok(event) = build(&case).built {
            names.push(format!("{file}: {name}"));
            events.push(stringify(&event));
        }
    }
    let rejected: Vec<String> = names
        .iter()
        .zip(ingestion(&events))
        .filter(|(_, result)| result != "valid")
        .map(|(name, result)| format!("{name}: {result}"))
        .collect();
    assert!(
        rejected.is_empty(),
        "the ingestion would reject {} of {} events:\n{}",
        rejected.len(),
        events.len(),
        rejected.join("\n")
    );
    println!("the ingestion accepts all {} events", events.len());
}

/// Measured, not required: how many cases this SDK writes character for character as TypeScript
/// does, key order included. Run with `-- --nocapture` to see the count and the cases that differ.
#[test]
fn typescript_writes_the_same_bytes() {
    let source = std::env::var_os("LSQUARE_TYPESCRIPT_SRC")
        .map_or_else(|| contract_dir().join("../lsquare/src"), PathBuf::from);
    if !source.join("event.ts").exists() {
        eprintln!(
            "skipped: the TypeScript SDK's source isn't at {}",
            source.display()
        );
        return;
    }
    if skip_without_node("identity with TypeScript's output") {
        return;
    }
    let dir = contract_dir().join("cases");
    let all = cases();
    let (mut same, mut total) = (0, 0);
    let mut differing = Vec::new();
    for file in FILES {
        let out = node(
            TYPESCRIPT,
            &[
                file_url(&source),
                dir.join(file)
                    .canonicalize()
                    .expect("a case file")
                    .display()
                    .to_string(),
                stringify(&sdk()),
            ],
            "",
        );
        let written: Vec<String> = serde_json::from_str(&out).expect("TypeScript's events");
        let ours = all.iter().filter(|case| case.file == file);
        for (case, theirs) in ours.zip(written) {
            total += 1;
            let mine = match build(&case.case).built {
                Err(code) => code,
                Ok(mut event) => {
                    if case
                        .case
                        .get("event")
                        .and_then(|event| event.get("eventId"))
                        .and_then(Js::as_text)
                        .as_deref()
                        == Some("$uuid")
                    {
                        event.insert("eventId".into(), "$uuid".into());
                    }
                    stringify(&event)
                }
            };
            if mine == theirs {
                same += 1;
            } else {
                differing.push(format!("{file}: {}", case.name));
            }
        }
    }
    println!("{same} of {total} cases are written byte for byte as TypeScript writes them");
    for name in &differing {
        println!("  differs: {name}");
    }
}
