//! What this SDK must hold in common with the contract (`packages/contract`), read from its
//! source so that a change there fails here: the wire limits, the secret key names, and what a
//! limited-diagnostics event may carry. And, where the TypeScript SDK's source is beside it, its
//! defaults.

use std::collections::{BTreeMap, BTreeSet};

use regex::Regex;

use crate::cases_test::contract_dir;
use crate::client;
use crate::event::{finish_call, shape_call, CallData, CallHead, Context};
use crate::normalize::MAX_ID_BYTES;
use crate::redact::{ORDER, SECRET_KEYS};
use crate::shape::{
    LIMIT_AGENT, LIMIT_DESCRIPTION, LIMIT_INPUT, LIMIT_INSTRUCTIONS, LIMIT_INTENT, LIMIT_RESULT,
    LOOKAHEAD, MAX_ENTRIES, MAX_LEVELS, SCAN_BUDGET,
};

fn read(path: &str) -> String {
    let path = contract_dir().join(path);
    std::fs::read_to_string(&path).unwrap_or_else(|error| panic!("{}: {error}", path.display()))
}

/// `1024 * 1024`, `10_000`, `60 * 1024`.
fn product(expression: &str) -> u64 {
    expression
        .split('*')
        .map(|factor| {
            factor
                .trim()
                .replace('_', "")
                .parse::<u64>()
                .unwrap_or_else(|_| panic!("not a number: {expression}"))
        })
        .product()
}

/// Every `NAME = number` or `name: number` in a TypeScript source, numbers written as products.
fn constants(source: &str) -> BTreeMap<String, u64> {
    let pattern = Regex::new(r"(?m)^\s*(?:export )?(?:const )?([A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*([0-9][0-9_]*(?:\s*\*\s*[0-9][0-9_]*)*),?\s*$").unwrap();
    pattern
        .captures_iter(source)
        .map(|found| (found[1].to_owned(), product(&found[2])))
        .collect()
}

/// The quoted names after `after`, up to `until`.
fn names(source: &str, after: &str, until: &str) -> BTreeSet<String> {
    let start = source
        .find(after)
        .unwrap_or_else(|| panic!("{after:?} isn't in the source"));
    let end = start + source[start..].find(until).expect("the list's end");
    Regex::new(r"'([^']+)'")
        .unwrap()
        .captures_iter(&source[start..end])
        .map(|found| found[1].to_owned())
        .collect()
}

#[test]
fn the_wire_limits_are_the_contracts() {
    let limits = constants(&read("src/limits.ts"));
    assert_eq!(limits["MAX_BODY_BYTES"], client::MAX_BATCH_BYTES as u64);
    assert_eq!(limits["MAX_EVENTS"], client::MAX_BATCH_EVENTS as u64);
    assert_eq!(
        limits["MAX_EVENT_BYTES"],
        client::DEFAULT_MAX_EVENT_BYTES as u64
    );
    assert_eq!(limits["MAX_EVENT_ID_BYTES"], MAX_ID_BYTES as u64);
    assert_eq!(limits["MAX_ID_BYTES"], MAX_ID_BYTES as u64);
    assert_eq!(limits["MAX_ACK_BYTES"], client::MAX_ACK_BYTES as u64);
    // The ingestion asks for a wait the client honours whole.
    assert!(limits["RETRY_AFTER_SECONDS"] * 1_000 <= client::RETRY_AFTER_CAP_MS);
    // The deepest event this SDK writes: its values nest ten levels, under at most four of its own
    // (the event, `agent`, `toolCalls`, a tool call) and a marker's.
    assert!((MAX_LEVELS + 6) as u64 <= limits["MAX_EVENT_DEPTH"]);
    assert_eq!(
        constants(&read("src/limits.ts")).len(),
        8,
        "a limit this test doesn't know of"
    );
}

#[test]
fn the_secret_key_names_are_redaction_mds() {
    let document = read("REDACTION.md");
    let start = document
        .find("const SECRET_KEYS = [")
        .expect("REDACTION.md lists the secret key names");
    let end = start + document[start..].find(']').expect("the list's end");
    let listed: Vec<String> = Regex::new(r"'([a-z0-9]+)'")
        .unwrap()
        .captures_iter(&document[start..end])
        .map(|found| found[1].to_owned())
        .collect();
    assert_eq!(listed, SECRET_KEYS, "in the same order");
}

#[test]
fn the_names_the_ingestion_accepts_are_the_ones_this_sdk_writes() {
    let validator = read("src/validate.ts");
    let patterns: BTreeSet<String> = ORDER
        .iter()
        .map(|pattern| pattern.name().to_owned())
        .collect();
    assert_eq!(
        names(&validator, "export type PatternName =", "\n"),
        patterns
    );
    assert!(
        names(&validator, "export type SdkLanguage =", "\n").contains("rust"),
        "the contract accepts `sdk.language: rust`"
    );
    // Every reason this SDK gives for removing a value is one the ingestion knows.
    let reasons = names(&validator, "export type DroppedReason =", "\n");
    let source: String = ["src/normalize.rs", "src/event.rs", "src/shape.rs"]
        .iter()
        .map(|file| {
            std::fs::read_to_string(std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join(file))
                .unwrap()
        })
        .collect();
    let given: BTreeSet<String> = Regex::new(r#"(?:drop\([^;]*?,\s*|Dropped\()"([a-z_]+)"\)"#)
        .unwrap()
        .captures_iter(&source)
        .map(|found| found[1].to_owned())
        .collect();
    assert!(given.len() >= 5, "{given:?}");
    assert!(
        given.is_subset(&reasons),
        "{given:?} aren't all among {reasons:?}"
    );
}

#[test]
fn a_limited_diagnostics_event_keeps_only_what_the_contract_allows() {
    let validator = read("src/validate.ts");
    let allowed = names(&validator, "const DIAGNOSTIC_FIELDS = new Set(", "]");
    let categories = names(&validator, "const ERROR_CATEGORIES = new Set(", "]");
    let context = Context {
        sdk: serde_json::json!({ "name": "lsquare", "version": "0", "language": "rust" }),
        patterns: ORDER.to_vec(),
        redact_keys: Vec::new(),
        max_event_bytes: 256 * 1024,
    };
    for category in &categories {
        let failure = serde_json::json!({ "message": "it broke for ann@example.com", "type": "Oops", "code": 7, "category": category });
        let input = serde_json::json!({ "who": "ann@example.com" });
        let description = serde_json::json!("Finds orders.");
        let instructions = serde_json::json!("Ask before you cancel an order.");
        let data = CallData {
            tool_description: Some(&description),
            server_instructions: Some(&instructions),
            input: Some(&input),
            error: Some(&failure),
            ..CallData::default()
        };
        let head = CallHead {
            call_id: "call-1".into(),
            captured_at: "2026-10-28T00:00:00.000Z".into(),
            tool_name: "search".into(),
            status: "error".into(),
            duration_ms: 3.into(),
            server: Some(serde_json::json!({ "name": "server", "version": "1" })),
            client: Some(serde_json::json!({ "name": "client", "version": "1" })),
            protocol_version: Some("2026-07-28".into()),
            metadata: Some(serde_json::json!({ "plan": "pro" })),
            diagnostics: true,
            ..CallHead::default()
        };
        let event = finish_call(shape_call(&data, &context), &head, &context, false)
            .expect("an event")
            .event;
        let fields: BTreeSet<String> = event.keys().cloned().collect();
        assert!(
            fields.is_subset(&allowed),
            "{fields:?} aren't all among {allowed:?}"
        );
        // Everything the contract allows of a call that failed is there, and of its error only the category.
        assert_eq!(fields, allowed);
        assert_eq!(event["error"], serde_json::json!({ "category": category }));
        assert_eq!(event["identity"], "failed");
    }
    assert_eq!(categories.len(), 4);
}

#[test]
fn the_servers_instructions_are_the_first_part_left_out_to_make_an_event_fit() {
    let description = serde_json::json!("Finds orders. ".repeat(20));
    let instructions = serde_json::json!("Ask before you cancel an order. ".repeat(20));
    let input = serde_json::json!({ "query": "lorem ipsum ".repeat(40) });
    let data = CallData {
        tool_description: Some(&description),
        server_instructions: Some(&instructions),
        input: Some(&input),
        ..CallData::default()
    };
    let head = CallHead {
        call_id: "call-1".into(),
        captured_at: "2026-10-28T00:00:00.000Z".into(),
        tool_name: "search".into(),
        status: "completed".into(),
        duration_ms: 3.into(),
        ..CallHead::default()
    };
    let built = |max_event_bytes: usize| {
        let context = Context {
            sdk: serde_json::json!({ "name": "lsquare", "version": "0", "language": "rust" }),
            patterns: ORDER.to_vec(),
            redact_keys: Vec::new(),
            max_event_bytes,
        };
        finish_call(shape_call(&data, &context), &head, &context, false)
            .expect("an event")
            .event
    };
    let size =
        |event: &serde_json::Map<String, serde_json::Value>| crate::json::stringify(event).len();
    let whole = built(256 * 1024);
    assert!(whole.contains_key("serverInstructions") && whole.contains_key("toolDescription"));
    // One byte short: the instructions go, and nothing else.
    let fitted = built(size(&whole) - 1);
    assert!(!fitted.contains_key("serverInstructions"));
    assert!(fitted.contains_key("toolDescription") && fitted.contains_key("input"));
    assert_eq!(
        fitted["truncated"],
        serde_json::json!(["serverInstructions"])
    );
    // Shorter still: the tool's description goes next, before anything only this call has.
    let fitted = built(size(&fitted) - 1);
    assert!(!fitted.contains_key("toolDescription") && fitted.contains_key("input"));
    assert_eq!(
        fitted["truncated"],
        serde_json::json!(["serverInstructions", "toolDescription"])
    );
}

#[test]
fn the_limits_and_defaults_are_typescripts() {
    let source = contract_dir().join("../lsquare/src");
    if !source.join("client.ts").exists() {
        eprintln!(
            "skipped: the TypeScript SDK's source isn't at {}",
            source.display()
        );
        return;
    }
    let read = |file: &str| std::fs::read_to_string(source.join(file)).unwrap();
    let shape = read("shape.ts");
    let shape_constants = constants(&shape);
    assert_eq!(shape_constants["SCAN_BUDGET"], SCAN_BUDGET as u64);
    assert_eq!(shape_constants["MAX_ENTRIES"], MAX_ENTRIES as u64);
    assert_eq!(shape_constants["MAX_LEVELS"], MAX_LEVELS as u64);
    assert_eq!(shape_constants["LOOKAHEAD"], LOOKAHEAD as u64);
    let limits = Regex::new(r"LIMITS = \{ input: ([0-9_]+), agent: ([0-9_]+), result: ([0-9_]+), intent: ([0-9_]+), description: ([0-9_]+), instructions: ([0-9_]+) \}").unwrap().captures(&shape).expect("shape.ts's LIMITS");
    let limits: Vec<u64> = (1..=6).map(|index| product(&limits[index])).collect();
    assert_eq!(
        limits,
        [
            LIMIT_INPUT as u64,
            LIMIT_AGENT as u64,
            LIMIT_RESULT as u64,
            LIMIT_INTENT as u64,
            LIMIT_DESCRIPTION as u64,
            LIMIT_INSTRUCTIONS as u64
        ]
    );

    let defaults = constants(&read("client.ts"));
    assert_eq!(
        defaults["flushTimeoutMs"],
        client::DEFAULT_FLUSH_TIMEOUT.as_millis() as u64
    );
    assert_eq!(
        defaults["shutdownTimeoutMs"],
        client::DEFAULT_SHUTDOWN_TIMEOUT.as_millis() as u64
    );
    assert_eq!(
        defaults["maxConcurrentHooks"],
        client::DEFAULT_MAX_CONCURRENT_HOOKS as u64
    );
    assert_eq!(
        defaults["maxConcurrentRequests"],
        client::DEFAULT_MAX_CONCURRENT_REQUESTS as u64
    );
    assert_eq!(
        defaults["maxQueueEvents"],
        client::DEFAULT_MAX_QUEUE_EVENTS as u64
    );
    assert_eq!(
        defaults["maxQueueBytes"],
        client::DEFAULT_MAX_QUEUE_BYTES as u64
    );
    assert_eq!(
        defaults["maxEventBytes"],
        client::DEFAULT_MAX_EVENT_BYTES as u64
    );
    assert_eq!(
        defaults["maxAttempts"],
        u64::from(client::DEFAULT_MAX_ATTEMPTS)
    );
    assert_eq!(
        defaults["requestTimeoutMs"],
        client::DEFAULT_REQUEST_TIMEOUT.as_millis() as u64
    );
    assert_eq!(
        defaults["MAX_BATCH_EVENTS"],
        client::MAX_BATCH_EVENTS as u64
    );
    assert_eq!(defaults["MAX_BATCH_BYTES"], client::MAX_BATCH_BYTES as u64);
    assert_eq!(defaults["RETRY_BASE_MS"], client::RETRY_BASE_MS);
    assert_eq!(defaults["RETRY_CAP_MS"], client::RETRY_CAP_MS);
    assert_eq!(defaults["RETRY_AFTER_CAP_MS"], client::RETRY_AFTER_CAP_MS);
    assert_eq!(
        defaults["MAX_RESPONSE_BYTES"],
        client::MAX_RESPONSE_BYTES as u64
    );
    assert!(read("client.ts").contains(&format!(
        "DEFAULT_ENDPOINT = '{}'",
        client::DEFAULT_ENDPOINT
    )));
}
