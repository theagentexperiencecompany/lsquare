//! `capture()`, end to end: a turn sent as an event of its own, and, inside a tool on an
//! instrumented server, as that call's `agent` part.

mod common;

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use common::{client, ingestion_says, until, Ingestion, Reply};
use lsquare::{Client, ErrorCode, FlushResult, Options, ToolCall, Turn, TurnStatus, Usage};
use serde_json::{json, Value};

/// The key of the turn `7` of `conv_42` in `org_1`, as TypeScript's `turnKey` makes it.
const KEYED: &str = "k/b2a8ca3c769656a82fc92cbb6c80278318654520ff08c88d0c9008fb68a61cf8";
/// The same turn without an org.
const KEYED_WITHOUT_ORG: &str =
    "k/aedc04c550d1000a5a316affc13bcbcb68cf65228fce299e57b8b9f88b9b2318";

fn sent(count: usize) -> FlushResult {
    FlushResult {
        sent: count,
        skipped: 0,
        failed: 0,
        pending: 0,
    }
}

/// Every event the ingestion got passes the contract's checks.
fn valid(events: &[Value]) {
    if let Some(said) = ingestion_says(events) {
        assert!(said.iter().all(|said| said == "valid"), "{said:?}");
    }
}

fn whole_turn() -> Turn {
    Turn::new()
        .org_id("org_1")
        .user_id("user_8")
        .conversation_id("conv_42")
        .turn_id("7")
        .tool_name("ask")
        .question("Where is the order of ann@example.com?")
        .answer("It left the warehouse this morning.")
        .tool_call(
            ToolCall::new("find_order")
                .input(json!({ "email": "ann@example.com", "password": "hunter2" }))
                .result_chars(240)
                .result_is_error(false)
                .duration_ms(120),
        )
        .usage(
            Usage::new()
                .input_tokens(812)
                .output_tokens(64)
                .cost_usd(0.0031),
        )
        .latency(json!({ "totalMs": 950 }))
        .verification(json!({ "grounded": true }))
        .client("web")
        .client_version("2.4.0")
        .api_key_id("key_5")
        .surface("support")
        .metadata("plan", "pro")
        .metadata("seats", 12)
}

#[tokio::test]
async fn a_turn_is_sent_as_an_event_of_its_own() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, seen) = client(&ingestion, |options| options);
    lsquare.capture(whole_turn());
    assert_eq!(lsquare.flush().await, sent(1));
    assert!(seen.codes().is_empty(), "{:?}", seen.codes());
    let events = ingestion.events();
    valid(&events);
    let mut event = events[0].clone();
    let fields = event.as_object_mut().unwrap();
    assert!(fields.remove("capturedAt").is_some_and(|at| at.is_string()));
    assert_eq!(
        fields.remove("sdk").unwrap()["language"],
        json!("rust"),
        "the event says which SDK made it"
    );
    assert_eq!(
        event,
        json!({
            "eventId": KEYED,
            "schemaVersion": 2,
            "kind": "turn",
            "source": "live",
            "orgId": "org_1",
            "userId": "user_8",
            "conversationId": "conv_42",
            "toolName": "ask",
            "question": "Where is the order of [EMAIL_1]?",
            "answer": "It left the warehouse this morning.",
            "turnId": "7",
            "status": "completed",
            "toolCalls": [{
                "name": "find_order",
                "input": { "email": "[EMAIL_1]", "password": "[SECRET_1]" },
                "resultChars": 240,
                "resultIsError": false,
                "durationMs": 120,
            }],
            "usage": { "inputTokens": 812, "outputTokens": 64, "costUsd": 0.0031 },
            "latency": { "totalMs": 950 },
            "verification": { "grounded": true },
            "client": { "name": "web", "version": "2.4.0" },
            "apiKeyId": "key_5",
            "surface": "support",
            "metadata": { "plan": "pro", "seats": 12 },
            "redactions": { "email": 2, "secret": 1 },
        })
    );
}

#[tokio::test]
async fn a_turn_with_a_conversation_and_a_turn_id_is_one_event_however_often_it_is_captured() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    let keyed = || Turn::new().conversation_id("conv_42").turn_id("7");
    lsquare.capture(keyed().answer("first"));
    lsquare.capture(keyed().answer("again"));
    // Without one of the two, each capture is an event of its own.
    lsquare.capture(Turn::new().conversation_id("conv_42"));
    lsquare.capture(Turn::new().conversation_id("conv_42"));
    lsquare.capture(Turn::new());
    assert_eq!(lsquare.flush().await.pending, 0);
    let events = ingestion.events();
    valid(&events);
    let ids: Vec<&str> = events
        .iter()
        .map(|event| event["eventId"].as_str().unwrap())
        .collect();
    assert_eq!(ids[0], KEYED_WITHOUT_ORG);
    assert_eq!(ids[1], KEYED_WITHOUT_ORG);
    assert!(ids[2..].iter().all(|id| !id.starts_with("k/")), "{ids:?}");
    assert_ne!(ids[2], ids[3]);
    // A turn with nothing in it is still a turn that completed.
    assert_eq!(events[4]["status"], json!("completed"));
    assert_eq!(events[4]["kind"], json!("turn"));
}

#[tokio::test]
async fn a_turn_that_failed_says_how() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    lsquare.capture(
        Turn::new()
            .status(TurnStatus::Error)
            .error("The carrier didn't answer bob@example.com")
            .error_type("Timeout")
            .tool_calls([]),
    );
    assert_eq!(lsquare.flush().await, sent(1));
    let events = ingestion.events();
    valid(&events);
    assert_eq!(events[0]["status"], json!("error"));
    assert_eq!(
        events[0]["error"],
        json!({ "message": "The carrier didn't answer [EMAIL_1]", "type": "Timeout" })
    );
    // An empty list says the agent called no tools.
    assert_eq!(events[0]["toolCalls"], json!([]));
}

#[tokio::test]
async fn a_turn_with_an_id_that_is_not_valid_is_dropped_and_that_is_said() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, seen) = client(&ingestion, |options| options);
    lsquare.capture(Turn::new().org_id("o".repeat(513)).user_id("with\0nul"));
    lsquare.capture(Turn::new().metadata("big", "x".repeat(300 * 1024)));
    // An empty ID is none: the turn goes without it, and says so.
    lsquare.capture(Turn::new().user_id("").answer("fine"));
    assert_eq!(lsquare.flush().await, sent(1));
    let errors = seen.errors();
    assert_eq!(
        errors
            .iter()
            .map(|error| (error.code, error.paths.clone()))
            .collect::<Vec<_>>(),
        [
            (
                ErrorCode::InvalidId,
                vec!["orgId".to_owned(), "userId".to_owned()]
            ),
            (ErrorCode::TooLarge, vec!["metadata".to_owned()]),
        ]
    );
    let events = ingestion.events();
    assert_eq!(events.len(), 1);
    assert_eq!(events[0]["dropped"], json!({ "userId": "empty" }));
    assert!(events[0].get("userId").is_none());
}

#[tokio::test]
async fn a_disabled_client_captures_nothing() {
    let seen = common::Seen::default();
    let lsquare = Client::new(seen.options().dry_run(false));
    assert!(!lsquare.is_enabled());
    lsquare.capture(whole_turn());
    assert_eq!(lsquare.current_call_id(), None);
    assert_eq!(lsquare.flush().await, FlushResult::default());
    assert!(seen.codes().is_empty());
}

#[tokio::test]
async fn a_turn_captured_after_shutdown_is_dropped_and_that_is_said_once() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, seen) = client(&ingestion, |options| options);
    lsquare.shutdown().await;
    lsquare.capture(Turn::new().answer("late"));
    lsquare.capture(Turn::new().answer("later"));
    lsquare.flush().await;
    let errors = seen.errors();
    assert_eq!(errors.len(), 1, "{errors:?}");
    assert_eq!(errors[0].code, ErrorCode::Shutdown);
    assert_eq!(
        errors[0].message,
        "capture() was called after shutdown(); this and later events are dropped."
    );
    assert!(ingestion.events().is_empty());
}

#[tokio::test]
async fn a_dry_run_says_what_a_turn_would_have_sent_and_where_its_id_comes_from() {
    let seen = common::Seen::default();
    let lsquare = Client::new(seen.options().dry_run(true));
    lsquare.capture(
        Turn::new()
            .conversation_id("conv_42")
            .turn_id("7")
            .question("ann@example.com?")
            .tool_call(ToolCall::new("find_order"))
            .tool_call(ToolCall::new("carrier")),
    );
    lsquare.capture(Turn::new().answer("no turn ID"));
    lsquare.flush().await;
    let lines = seen.lines();
    assert_eq!(lines.len(), 2, "{lines:?}");
    let summary = |line: &str| -> Value {
        serde_json::from_str(
            line.strip_prefix("[lsquare dry-run] ")
                .expect("a dry-run line"),
        )
        .expect("its summary")
    };
    let keyed = summary(&lines[0]);
    assert_eq!(keyed["kind"], json!("turn"));
    assert_eq!(keyed["eventId"], json!("from turnId, hashed"));
    assert_eq!(
        keyed["fields"],
        json!([
            "conversationId",
            "question",
            "status",
            "toolCalls",
            "turnId"
        ])
    );
    assert_eq!(keyed["toolCalls"], json!(2));
    assert_eq!(keyed["redactions"], json!({ "email": 1 }));
    // Never the content, nor the ID.
    assert!(!lines[0].contains("ann@") && !lines[0].contains("k/"));
    let random = summary(&lines[1]);
    assert_eq!(random["eventId"], json!("random: no turnId"));
    assert!(random.get("toolCalls").is_none());
}

#[tokio::test]
async fn before_send_gets_a_turn_and_may_change_or_drop_it() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, seen) = client(&ingestion, |options| {
        options.before_send(|mut event| {
            assert_eq!(event.kind(), "turn");
            let answer = event.get("answer").and_then(Value::as_str).unwrap_or("");
            if answer == "drop me" {
                return None;
            }
            // Not yet redacted where it wasn't cut: the hook can still read it.
            assert!(event
                .get("question")
                .is_none_or(|question| question == "mail ann@example.com"));
            event.insert("answer", format!("{answer}, checked by cat@example.com"));
            // Its identity isn't the hook's to change.
            event.insert("eventId", "mine");
            event.insert("userId", "someone_else");
            Some(event)
        })
    });
    lsquare.capture(
        Turn::new()
            .user_id("user_8")
            .question("mail ann@example.com")
            .answer("sent"),
    );
    lsquare.capture(Turn::new().answer("drop me"));
    assert_eq!(
        lsquare.flush().await,
        FlushResult {
            sent: 1,
            skipped: 1,
            failed: 0,
            pending: 0
        }
    );
    assert!(seen.codes().is_empty(), "{:?}", seen.codes());
    let events = ingestion.events();
    valid(&events);
    assert_eq!(events.len(), 1);
    assert_eq!(events[0]["userId"], json!("user_8"));
    assert_ne!(events[0]["eventId"], json!("mine"));
    assert_eq!(events[0]["question"], json!("mail [EMAIL_1]"));
    assert_eq!(events[0]["answer"], json!("sent, checked by [EMAIL_2]"));
}

#[tokio::test]
async fn a_hook_that_takes_too_long_drops_the_turn() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, seen) = client(&ingestion, |options| {
        options
            .before_send_timeout(Duration::from_millis(50))
            .before_send(|event| {
                std::thread::sleep(Duration::from_millis(400));
                Some(event)
            })
    });
    lsquare.capture(Turn::new().answer("slow"));
    let flushed = lsquare.flush().await;
    assert_eq!(flushed.failed, 1, "{flushed:?}");
    assert_eq!(seen.codes(), ["hook_timeout"]);
    assert!(ingestion.events().is_empty());
}

#[test]
fn a_hook_whose_time_ran_out_before_the_runtime_could_start_it_is_not_run() {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    let seen = common::Seen::default();
    let ran = Arc::new(AtomicUsize::new(0));
    let count = ran.clone();
    let lsquare = runtime.block_on(async {
        Client::new(
            seen.options()
                .dry_run(true)
                .before_send_timeout(Duration::from_millis(40))
                .before_send(move |event| {
                    count.fetch_add(1, Ordering::SeqCst);
                    Some(event)
                }),
        )
    });
    runtime.block_on(async {
        lsquare.capture(Turn::new().answer("held up"));
        // The runtime's one thread is busy past the hook's time, which counts from the capture.
        std::thread::sleep(Duration::from_millis(120));
        let flushed = lsquare.flush().await;
        assert_eq!(flushed.failed, 1, "{flushed:?}");
    });
    assert_eq!(seen.codes(), ["hook_timeout"]);
    assert_eq!(ran.load(Ordering::SeqCst), 0);
}

#[tokio::test]
async fn a_hook_time_too_long_to_count_is_a_long_one() {
    let ingestion = Ingestion::accepting().await;
    for hooked in [false, true] {
        let (lsquare, seen) = client(&ingestion, |options| {
            let options = options.before_send_timeout(Duration::MAX);
            if hooked {
                options.before_send(Some)
            } else {
                options
            }
        });
        lsquare.capture(Turn::new().answer("fine"));
        assert_eq!(lsquare.flush().await, sent(1), "hooked: {hooked}");
        assert!(seen.codes().is_empty(), "{:?}", seen.codes());
    }
}

#[test]
fn a_hook_whose_turn_comes_after_its_time_is_not_run() {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    let seen = common::Seen::default();
    let ran = Arc::new(AtomicUsize::new(0));
    let count = ran.clone();
    let lsquare = runtime.block_on(async {
        Client::new(
            seen.options()
                .dry_run(true)
                .max_concurrent_hooks(1)
                .before_send_timeout(Duration::from_millis(100))
                .before_send(move |event| {
                    count.fetch_add(1, Ordering::SeqCst);
                    std::thread::sleep(Duration::from_millis(150));
                    Some(event)
                }),
        )
    });
    runtime.block_on(async {
        lsquare.capture(Turn::new().answer("the first"));
        lsquare.capture(Turn::new().answer("the second"));
        // Both are on their way: the first has the only hook slot, the second waits for it.
        tokio::time::sleep(Duration::from_millis(20)).await;
        // The runtime's one thread is busy while the first hook ends and the second's time
        // runs out: when it is free again, the slot is too.
        std::thread::sleep(Duration::from_millis(250));
        let flushed = lsquare.flush().await;
        assert_eq!(flushed.failed, 2, "{flushed:?}");
    });
    assert_eq!(seen.codes(), ["hook_timeout", "hook_timeout"]);
    assert_eq!(ran.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn a_hook_that_captures_from_every_event_ends_two_levels_deep() {
    let ingestion = Ingestion::accepting().await;
    let slot: Arc<Mutex<Option<Client>>> = Arc::default();
    let inside = slot.clone();
    let hooked = Arc::new(AtomicUsize::new(0));
    let count = hooked.clone();
    let (lsquare, seen) = client(&ingestion, move |options| {
        options.before_send(move |event| {
            let depth = count.fetch_add(1, Ordering::SeqCst);
            if let Some(lsquare) = inside.lock().unwrap().clone() {
                lsquare.capture(Turn::new().answer(format!("from the hook, {depth}")));
            }
            Some(event)
        })
    });
    *slot.lock().unwrap() = Some(lsquare.clone());
    lsquare.capture(Turn::new().answer("the app's own"));
    // The app's, the one its hook captured, and the one that one's hook captured: the third
    // hook's capture is ignored.
    until(|| ingestion.events().len() == 3).await;
    tokio::time::sleep(Duration::from_millis(200)).await;
    lsquare.flush().await;
    assert_eq!(ingestion.events().len(), 3);
    assert_eq!(hooked.load(Ordering::SeqCst), 3);
    assert!(seen.codes().is_empty(), "{:?}", seen.codes());
    *slot.lock().unwrap() = None;
}

#[tokio::test]
async fn two_clients_capturing_from_each_others_hooks_end_too() {
    let ingestion = Ingestion::accepting().await;
    let slots: [Arc<Mutex<Option<Client>>>; 2] = [Arc::default(), Arc::default()];
    let make = |other: Arc<Mutex<Option<Client>>>| {
        client(&ingestion, move |options| {
            options.before_send(move |event| {
                if let Some(other) = other.lock().unwrap().clone() {
                    other.capture(Turn::new().answer("from the other's hook"));
                }
                Some(event)
            })
        })
    };
    let (first, _) = make(slots[1].clone());
    let (second, _) = make(slots[0].clone());
    *slots[0].lock().unwrap() = Some(first.clone());
    *slots[1].lock().unwrap() = Some(second.clone());
    first.capture(Turn::new().answer("the app's own"));
    until(|| ingestion.events().len() == 3).await;
    tokio::time::sleep(Duration::from_millis(200)).await;
    first.flush().await;
    second.flush().await;
    assert_eq!(ingestion.events().len(), 3);
    for slot in &slots {
        *slot.lock().unwrap() = None;
    }
}

#[tokio::test]
async fn a_turn_captured_from_on_error_or_the_logger_is_ignored() {
    let ingestion = Ingestion::accepting().await;
    let slot: Arc<Mutex<Option<Client>>> = Arc::default();
    let (in_handler, in_logger) = (slot.clone(), slot.clone());
    let told = Arc::new(AtomicUsize::new(0));
    let count = told.clone();
    let lsquare = Client::new(
        Options::new()
            .api_key("lsq_test_key".to_owned())
            .endpoint(ingestion.url.clone())
            .on_error(move |_, _| {
                count.fetch_add(1, Ordering::SeqCst);
                if let Some(lsquare) = in_handler.lock().unwrap().clone() {
                    // Each of these would fail too, and bring the next report.
                    lsquare.capture(Turn::new().org_id("\0"));
                    lsquare.capture(Turn::new().answer("from on_error"));
                }
            })
            .logger(move |_, _| {
                if let Some(lsquare) = in_logger.lock().unwrap().clone() {
                    lsquare.capture(Turn::new().answer("from the logger"));
                }
            }),
    );
    *slot.lock().unwrap() = Some(lsquare.clone());
    lsquare.capture(Turn::new().org_id("\0"));
    lsquare.flush().await;
    tokio::time::sleep(Duration::from_millis(200)).await;
    lsquare.flush().await;
    assert_eq!(told.load(Ordering::SeqCst), 1);
    assert!(ingestion.events().is_empty());
    *slot.lock().unwrap() = None;
}

#[tokio::test]
async fn a_turn_captured_from_the_logger_is_ignored_wherever_the_logger_is_called() {
    // In dry run every event is a line for the logger: one that captured from each line would
    // never end.
    let slot: Arc<Mutex<Option<Client>>> = Arc::default();
    let inside = slot.clone();
    let lines = Arc::new(AtomicUsize::new(0));
    let count = lines.clone();
    let logger = move |_: lsquare::Level, _: &str| {
        count.fetch_add(1, Ordering::SeqCst);
        if let Some(lsquare) = inside.lock().unwrap().clone() {
            lsquare.capture(Turn::new().answer("from the logger"));
        }
    };
    let lsquare = Client::new(Options::new().dry_run(true).logger(logger.clone()));
    *slot.lock().unwrap() = Some(lsquare.clone());
    lsquare.capture(Turn::new().answer("the app's own"));
    lsquare.flush().await;
    tokio::time::sleep(Duration::from_millis(200)).await;
    lsquare.flush().await;
    assert_eq!(lines.load(Ordering::SeqCst), 1);
    // The line a client writes as it is made comes on the app's own thread: no capture from
    // there is taken either, and the thread captures as before afterwards.
    let keyless = Client::new(Options::new().dry_run(false).logger(logger));
    assert!(!keyless.is_enabled());
    assert_eq!(lines.load(Ordering::SeqCst), 2);
    lsquare.capture(Turn::new().answer("the app's own again"));
    lsquare.flush().await;
    tokio::time::sleep(Duration::from_millis(200)).await;
    lsquare.flush().await;
    assert_eq!(lines.load(Ordering::SeqCst), 3);
    *slot.lock().unwrap() = None;
}

#[tokio::test]
async fn what_a_turn_gives_wrongly_is_left_out_and_noted() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, seen) = client(&ingestion, |options| options);
    lsquare.capture(
        Turn::new()
            .usage(Usage::new().input_tokens(3).cost_usd(f64::NAN))
            .metadata("nested", json!({ "a": 1 }))
            .metadata("plan", "pro")
            .turn_id(""),
    );
    assert_eq!(lsquare.flush().await, sent(1));
    assert!(seen.codes().is_empty());
    let events = ingestion.events();
    valid(&events);
    assert_eq!(events[0]["usage"], json!({ "inputTokens": 3 }));
    assert_eq!(events[0]["metadata"], json!({ "plan": "pro" }));
    assert_eq!(
        events[0]["dropped"],
        json!({ "turnId": "empty", "usage.costUsd": "wrong_type", "metadata": "not_json" })
    );
}

#[test]
fn a_value_nested_deeper_than_the_stack_is_let_go_of_safely() {
    // Built without recursion, as it must be let go of: `serde_json` itself would overflow the
    // stack dropping it.
    let mut deep = json!("the innermost");
    for _ in 0..200_000 {
        deep = Value::Array(vec![deep]);
    }
    let seen = common::Seen::default();
    let lsquare = Client::new(seen.options().dry_run(true));
    lsquare.capture(Turn::new().answer("kept").latency(deep));
    // And a list as wide as that one was deep, which takes no more room to let go of.
    lsquare.capture(Turn::new().latency(Value::Array(vec![Value::Null; 2_000_000])));
    // A disabled client lets go of what it is given the same way.
    let mut deep = json!({});
    for _ in 0..200_000 {
        let mut outer = serde_json::Map::new();
        outer.insert("in".to_owned(), deep);
        deep = Value::Object(outer);
    }
    Client::new(Options::new().dry_run(false).logger(|_, _| {}))
        .capture(Turn::new().verification(deep));
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    runtime.block_on(lsquare.flush());
    assert_eq!(seen.lines().len(), 2, "{:?}", seen.lines());
    assert!(seen.codes().is_empty());
}

#[tokio::test]
async fn a_turn_captured_off_the_runtime_is_sent_from_the_one_the_client_knows() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, seen) = client(&ingestion, |options| options);
    let off = lsquare.clone();
    std::thread::spawn(move || off.capture(Turn::new().answer("from a plain thread")))
        .join()
        .unwrap();
    until(|| ingestion.events().len() == 1).await;
    assert!(seen.codes().is_empty());
    assert_eq!(
        ingestion.events()[0]["answer"],
        json!("from a plain thread")
    );
}

#[test]
fn a_turn_captured_where_there_is_no_runtime_waits_for_one_and_never_panics() {
    let seen = common::Seen::default();
    // Made off any runtime, with a hook: there is nowhere to wait for it.
    let hooked = Client::new(
        seen.options()
            .api_key("lsq_test_key".to_owned())
            .endpoint("http://127.0.0.1:9")
            .before_send(Some),
    );
    hooked.capture(Turn::new().answer("nowhere to run the hook"));
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    let flushed = runtime.block_on(hooked.flush_within(Duration::from_millis(200)));
    assert_eq!(flushed, FlushResult::default());
    assert_eq!(seen.codes(), ["internal_error"]);
    assert_eq!(
        seen.errors()[0].message,
        "capture() found no Tokio runtime to wait for before_send on; the event was dropped."
    );
    // Without a hook it is queued, and sent by the first runtime that flushes.
    let ingestion = runtime.block_on(Ingestion::accepting());
    let plain = Client::new(
        Options::new()
            .api_key("lsq_test_key".to_owned())
            .endpoint(ingestion.url.clone()),
    );
    plain.capture(Turn::new().answer("queued"));
    assert_eq!(runtime.block_on(plain.flush()), sent(1));
}

#[tokio::test]
async fn a_full_queue_drops_a_turn_and_says_so() {
    // Nothing is acknowledged until the test lets it: the queue stays as it is.
    let gate = Arc::new(tokio::sync::Notify::new());
    let held = gate.clone();
    let ingestion = Ingestion::start(move |_, received| Reply::accept(received).when(&held)).await;
    let (lsquare, seen) = client(&ingestion, |options| options.max_queue_events(2));
    for index in 0..4 {
        lsquare.capture(Turn::new().answer(format!("turn {index}")));
        if index == 0 {
            // The first is on its way, and can't give its place up.
            until(|| ingestion.requests().len() == 1).await;
        }
    }
    for _ in 0..4 {
        gate.notify_one();
    }
    gate.notify_waiters();
    let flushed = lsquare.flush_within(Duration::from_secs(5)).await;
    assert_eq!(flushed.sent, 2, "{flushed:?}");
    // The second and third each gave way to the one after: the oldest not yet in a request.
    assert_eq!(seen.codes(), ["queue_full", "queue_full"]);
    let answers: Vec<Value> = ingestion
        .events()
        .iter()
        .map(|event| event["answer"].clone())
        .collect();
    assert_eq!(answers, [json!("turn 0"), json!("turn 3")]);
}

#[test]
fn a_turn_too_large_for_the_queue_takes_no_other_events_place() {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    let ingestion = runtime.block_on(Ingestion::accepting());
    let seen = common::Seen::default();
    // Made and used off any runtime: what is captured waits in the queue.
    let lsquare = Client::new(
        seen.options()
            .api_key("lsq_test_key".to_owned())
            .endpoint(ingestion.url.clone())
            .max_queue_events(1)
            .max_queue_bytes(512),
    );
    lsquare.capture(Turn::new().answer("small"));
    lsquare.capture(Turn::new().answer("lorem ipsum ".repeat(100)));
    let flushed = runtime.block_on(lsquare.flush());
    assert_eq!(flushed.sent, 1, "{flushed:?}");
    assert_eq!(seen.codes(), ["queue_full"]);
    assert_eq!(ingestion.events()[0]["answer"], json!("small"));
}
