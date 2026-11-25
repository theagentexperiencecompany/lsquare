//! The client, end to end: what it sends LSquare's ingestion, and what it makes of the answers.

mod common;

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use common::{call, client, connect, until, Ingestion, Reply, Tools};
use lsquare::mcp::{instrument, InstrumentOptions};
use lsquare::{ErrorCode, FlushResult, Pattern};
use serde_json::json;
use tokio::sync::Notify;

async fn echo(mcp: &common::Mcp, count: usize) {
    for index in 0..count {
        call(mcp, "echo", json!({ "text": format!("call {index}") }))
            .await
            .unwrap();
    }
}

#[tokio::test]
async fn an_event_is_sent_only_if_lsquare_acknowledges_it() {
    // A first request is held, so the four calls after it go together: accepted, duplicate,
    // rejected, and not named at all.
    let gate = Arc::new(Notify::new());
    let held = gate.clone();
    let ingestion = Ingestion::start(move |count, received| {
        let ids = received.ids();
        if count == 0 {
            return Reply::accept(received).when(&held);
        }
        Reply::json(json!({ "accepted": [ids[0]], "duplicates": [ids[1]], "rejected": [{ "eventId": ids[2], "reason": "invalid_field" }] }))
    })
    .await;
    let (lsquare, seen) = client(&ingestion, |options| options.max_concurrent_requests(1));
    let mcp = connect(instrument(
        Tools::default(),
        &lsquare,
        InstrumentOptions::new(),
    ))
    .await;
    call(&mcp, "echo", json!({ "text": "first" }))
        .await
        .unwrap();
    until(|| ingestion.requests().len() == 1).await;
    echo(&mcp, 4).await;
    gate.notify_one();
    assert_eq!(
        lsquare.flush().await,
        FlushResult {
            sent: 3,
            skipped: 0,
            failed: 2,
            pending: 0
        }
    );
    // Ready together, they went together.
    assert_eq!(ingestion.requests().len(), 2);
    let errors = seen.errors();
    assert_eq!(errors.len(), 2);
    assert_eq!(
        (errors[0].code, errors[0].message.as_str()),
        (
            ErrorCode::Rejected,
            "LSquare rejected the event: invalid_field."
        )
    );
    assert_eq!(
        (errors[1].code, errors[1].status),
        (ErrorCode::SendFailed, Some(200))
    );
    assert_eq!(
        errors[1].message,
        "Unexpected response: LSquare did not acknowledge the event."
    );
    let ids = ingestion.requests()[1].ids();
    assert_eq!(errors[0].event_id.as_deref(), Some(ids[2].as_str()));
    // The handler gets the event as it was sent: redacted.
    let reported = seen.errors.lock().unwrap()[0].1.clone().unwrap();
    assert_eq!(reported["input"], json!({ "text": "call 2" }));
}

#[tokio::test]
async fn a_batch_holds_at_most_100_events() {
    // One request at a time, and the first is held, so the queue fills behind it.
    let gate = Arc::new(Notify::new());
    let held = gate.clone();
    let ingestion = Ingestion::start(move |count, received| {
        if count == 0 {
            Reply::accept(received).when(&held)
        } else {
            Reply::accept(received)
        }
    })
    .await;
    let (lsquare, _) = client(&ingestion, |options| options.max_concurrent_requests(1));
    let mcp = connect(instrument(
        Tools::default(),
        &lsquare,
        InstrumentOptions::new(),
    ))
    .await;
    echo(&mcp, 1).await;
    until(|| ingestion.requests().len() == 1).await;
    echo(&mcp, 250).await;
    gate.notify_one();
    assert_eq!(
        lsquare.flush_within(Duration::from_secs(10)).await.sent,
        251
    );
    let sizes: Vec<usize> = ingestion
        .requests()
        .iter()
        .map(|request| request.events.len())
        .collect();
    assert_eq!(sizes, [1, 100, 100, 50]);
    // In the order they were recorded.
    let texts: Vec<String> = ingestion
        .events()
        .iter()
        .skip(1)
        .map(|event| event["input"]["text"].as_str().unwrap().to_owned())
        .collect();
    assert_eq!(
        texts,
        (0..250)
            .map(|index| format!("call {index}"))
            .collect::<Vec<_>>()
    );
}

#[tokio::test]
async fn no_more_requests_are_in_flight_than_the_limit() {
    // Every answer is held until the test lets it go.
    let gate = Arc::new(Notify::new());
    let held = gate.clone();
    let ingestion = Ingestion::start(move |_, received| Reply::accept(received).when(&held)).await;
    let (lsquare, seen) = client(&ingestion, |options| options.max_concurrent_requests(4));
    let mcp = connect(instrument(
        Tools::default(),
        &lsquare,
        InstrumentOptions::new(),
    ))
    .await;
    for in_flight in 1..=4 {
        echo(&mcp, 1).await;
        until(|| ingestion.requests().len() == in_flight).await;
    }
    // Four requests are out: what is recorded now waits for one to end, and goes together.
    echo(&mcp, 6).await;
    tokio::time::sleep(Duration::from_millis(200)).await;
    assert_eq!(ingestion.requests().len(), 4);
    let flushing = tokio::spawn({
        let lsquare = lsquare.clone();
        async move { lsquare.flush_within(Duration::from_secs(10)).await }
    });
    while !flushing.is_finished() {
        gate.notify_waiters();
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    assert_eq!(flushing.await.unwrap().sent, 10);
    let sizes: Vec<usize> = ingestion
        .requests()
        .iter()
        .map(|request| request.events.len())
        .collect();
    assert_eq!(sizes, [1, 1, 1, 1, 6]);
    assert!(seen.codes().is_empty());
}

#[tokio::test]
async fn a_server_error_is_retried_and_then_given_up() {
    let ingestion = Ingestion::start(|count, received| {
        if count < 2 {
            Reply::status(503)
        } else {
            Reply::accept(received)
        }
    })
    .await;
    let (lsquare, seen) = client(&ingestion, |options| options);
    let mcp = connect(instrument(
        Tools::default(),
        &lsquare,
        InstrumentOptions::new(),
    ))
    .await;
    echo(&mcp, 1).await;
    assert_eq!(lsquare.flush().await.sent, 1);
    assert_eq!(ingestion.requests().len(), 3);
    // The same event each time, by its ID: a retry can't be stored twice.
    assert!(ingestion
        .requests()
        .iter()
        .all(|request| request.ids() == ingestion.requests()[0].ids()));
    assert!(seen.codes().is_empty());

    let down = Ingestion::start(|_, _| Reply::status(500)).await;
    let (lsquare, seen) = client(&down, |options| options);
    let mcp = connect(instrument(
        Tools::default(),
        &lsquare,
        InstrumentOptions::new(),
    ))
    .await;
    echo(&mcp, 1).await;
    assert_eq!(lsquare.flush().await.failed, 1);
    assert_eq!(down.requests().len(), 3);
    assert_eq!(
        seen.errors()[0].message,
        "LSquare answered 500 after 3 attempts."
    );
    assert_eq!(
        (seen.errors()[0].code, seen.errors()[0].status),
        (ErrorCode::SendFailed, Some(500))
    );
}

#[tokio::test]
async fn retry_after_is_waited_for() {
    let ingestion = Ingestion::start(|count, received| {
        if count == 0 {
            Reply::status(429).header("retry-after", "1")
        } else {
            Reply::accept(received)
        }
    })
    .await;
    let (lsquare, _) = client(&ingestion, |options| options);
    let mcp = connect(instrument(
        Tools::default(),
        &lsquare,
        InstrumentOptions::new(),
    ))
    .await;
    echo(&mcp, 1).await;
    // A flush whose deadline comes before the retry doesn't wait for it.
    let started = Instant::now();
    assert_eq!(
        lsquare
            .flush_within(Duration::from_millis(600))
            .await
            .pending,
        1
    );
    assert!(started.elapsed() < Duration::from_millis(500));
    assert_eq!(lsquare.flush_within(Duration::from_secs(3)).await.sent, 1);
    let requests = ingestion.requests();
    let waited = requests[1].at.duration_since(requests[0].at);
    assert!(
        waited >= Duration::from_millis(950) && waited < Duration::from_millis(2_500),
        "{waited:?}"
    );
}

#[tokio::test]
async fn a_client_error_or_a_redirect_is_not_followed() {
    let refusing = Ingestion::start(|_, _| Reply::status(401)).await;
    let (lsquare, seen) = client(&refusing, |options| options);
    let mcp = connect(instrument(
        Tools::default(),
        &lsquare,
        InstrumentOptions::new(),
    ))
    .await;
    echo(&mcp, 1).await;
    assert_eq!(lsquare.flush().await.failed, 1);
    assert_eq!(refusing.requests().len(), 1);
    assert_eq!(
        seen.errors()[0].message,
        "LSquare answered 401; not retried."
    );

    // A redirect is refused, so the key never goes elsewhere: retried as a network error.
    let elsewhere = Ingestion::accepting().await;
    let target = format!("{}/v1/turns", elsewhere.url);
    let redirecting =
        Ingestion::start(move |_, _| Reply::status(307).header("location", target.clone())).await;
    let (lsquare, seen) = client(&redirecting, |options| options.max_attempts(2));
    let mcp = connect(instrument(
        Tools::default(),
        &lsquare,
        InstrumentOptions::new(),
    ))
    .await;
    echo(&mcp, 1).await;
    assert_eq!(lsquare.flush().await.failed, 1);
    assert_eq!(redirecting.requests().len(), 2);
    assert!(elsewhere.requests().is_empty());
    assert_eq!(seen.errors()[0].message, "Network error after 2 attempts.");
}

#[tokio::test]
async fn flush_reports_what_is_still_pending_and_shutdown_drops_it() {
    let slow =
        Ingestion::start(|_, received| Reply::accept(received).after(Duration::from_secs(20)))
            .await;
    let (lsquare, seen) = client(&slow, |options| options);
    let mcp = connect(instrument(
        Tools::default(),
        &lsquare,
        InstrumentOptions::new(),
    ))
    .await;
    echo(&mcp, 2).await;
    let started = Instant::now();
    assert_eq!(
        lsquare.flush_within(Duration::from_millis(200)).await,
        FlushResult {
            sent: 0,
            skipped: 0,
            failed: 0,
            pending: 2
        }
    );
    assert!(started.elapsed() < Duration::from_secs(2));
    assert_eq!(seen.codes(), ["pending_after_flush", "pending_after_flush"]);
    // Zero doesn't wait, and doesn't report.
    assert_eq!(lsquare.flush_within(Duration::ZERO).await.pending, 2);
    assert_eq!(seen.codes().len(), 2);

    assert_eq!(
        lsquare.shutdown_within(Duration::from_millis(200)).await,
        FlushResult {
            sent: 0,
            skipped: 0,
            failed: 2,
            pending: 0
        }
    );
    assert_eq!(seen.codes()[2..], ["shutdown", "shutdown"]);
    assert_eq!(
        seen.errors()[2].message,
        "shutdown() ended before the event was sent."
    );
    // Later calls still work, and are dropped, which is said once.
    echo(&mcp, 2).await;
    assert_eq!(lsquare.flush().await, FlushResult::default());
    until(|| seen.codes().len() == 5).await;
    assert_eq!(seen.codes()[4..], ["shutdown"]);
    assert_eq!(
        seen.errors()[4].message,
        "A call was recorded after shutdown(); this and later events are dropped."
    );
}

#[tokio::test]
async fn shutdown_sends_what_is_queued() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, seen) = client(&ingestion, |options| options);
    let mcp = connect(instrument(
        Tools::default(),
        &lsquare,
        InstrumentOptions::new(),
    ))
    .await;
    echo(&mcp, 3).await;
    // It counts what was still on its way when it was called: all three, or fewer if some were
    // sent already.
    let result = lsquare.shutdown().await;
    assert_eq!((result.failed, result.pending, result.skipped), (0, 0, 0));
    assert!((1..=3).contains(&result.sent), "{result:?}");
    assert_eq!(ingestion.events().len(), 3);
    assert!(seen.codes().is_empty());
}

#[tokio::test]
async fn the_queue_drops_its_oldest_events_past_its_limits() {
    let slow =
        Ingestion::start(|_, received| Reply::accept(received).after(Duration::from_millis(300)))
            .await;
    // One request at a time: the first event is in it, the others wait.
    let (lsquare, seen) = client(&slow, |options| {
        options.max_concurrent_requests(1).max_queue_events(3)
    });
    let mcp = connect(instrument(
        Tools::default(),
        &lsquare,
        InstrumentOptions::new(),
    ))
    .await;
    echo(&mcp, 1).await;
    until(|| slow.requests().len() == 1).await;
    echo(&mcp, 4).await;
    lsquare.flush_within(Duration::from_secs(5)).await;
    // Of the four that waited, the two oldest gave way; the one in the request was never dropped.
    assert_eq!(seen.codes(), ["queue_full", "queue_full"]);
    assert_eq!(
        seen.errors()[0].message,
        "Dropped to stay within the memory limits."
    );
    let texts: Vec<String> = slow
        .events()
        .iter()
        .map(|event| event["input"]["text"].as_str().unwrap().to_owned())
        .collect();
    assert_eq!(texts, ["call 0", "call 2", "call 3"]);
}

#[tokio::test]
async fn before_send_is_given_the_event_as_it_is_sent() {
    let ingestion = Ingestion::accepting().await;
    let given = Arc::new(std::sync::Mutex::new(None));
    let kept = given.clone();
    let (lsquare, seen) = client(&ingestion, |options| {
        options.before_send(move |event| {
            *kept.lock().unwrap() = event.get("input").map(ToString::to_string);
            Some(event)
        })
    });
    let mcp = connect(instrument(
        Tools::default(),
        &lsquare,
        InstrumentOptions::new(),
    ))
    .await;
    // An integer past 2^53, and keys that JavaScript puts first.
    let arguments =
        json!({ "text": "hello", "n": 9007199254740993u64, "10": 1, "2": -9007199254740993i64 });
    call(&mcp, "echo", arguments).await.unwrap();
    assert_eq!(lsquare.flush().await.sent, 1);
    // The hook read what the other SDKs' hooks read, and what was sent: not what arrived.
    let sent = r#"{"2":-9007199254740992,"10":1,"text":"hello","n":9007199254740992}"#;
    assert_eq!(given.lock().unwrap().as_deref(), Some(sent));
    assert_eq!(ingestion.events()[0]["input"].to_string(), sent);
    assert!(seen.codes().is_empty());
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn before_send_can_change_an_event_or_drop_it() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, seen) = client(&ingestion, |options| {
        options.before_send(|mut event| {
            let text = event.get("input").and_then(|input| input["text"].as_str()).unwrap_or_default().to_owned();
            match text.as_str() {
                "drop me" => return None,
                "panic" => panic!("the hook panicked"),
                _ => {}
            }
            // The hook sees the arguments before the patterns run, and what it adds is redacted too.
            assert!(text.contains("ann@example.com") || text == "plain");
            event.insert("input", json!({ "text": format!("{text} and bob@example.com"), "note": "word ".repeat(4_000) }));
            // What identifies the event is restored.
            event.insert("toolName", "renamed");
            event.remove("status");
            event.insert("made_up", true);
            Some(event)
        })
    });
    let mcp = connect(instrument(
        Tools::default(),
        &lsquare,
        InstrumentOptions::new(),
    ))
    .await;
    for text in ["to ann@example.com", "drop me", "panic", "plain"] {
        call(&mcp, "echo", json!({ "text": text })).await.unwrap();
    }
    // Each ends in its own time; a flush counts only those still on their way when it is called.
    until(|| ingestion.events().len() == 2 && seen.codes().len() == 1).await;
    lsquare.flush().await;
    assert_eq!(seen.codes(), ["hook_failed"]);
    assert_eq!(
        seen.errors()[0].message,
        "before_send panicked; the event was dropped."
    );
    let mut events = ingestion.events();
    events.sort_by_key(|event| {
        event["input"]["text"]
            .as_str()
            .map(|text| text.starts_with("plain"))
    });
    assert_eq!(events[0]["input"]["text"], "to [EMAIL_1] and [EMAIL_2]");
    assert_eq!(events[0]["toolName"], "echo");
    assert_eq!(events[0]["status"], "completed");
    assert!(events[0].get("made_up").is_none());
    assert!(events[0]["input"]["note"]
        .as_str()
        .unwrap()
        .ends_with("…[+4000 chars]"));
    assert_eq!(events[0]["truncated"], json!(["input"]));
    assert_eq!(events[1]["input"]["text"], "plain and [EMAIL_1]");
    if let Some(says) = common::ingestion_says(&events) {
        assert_eq!(says, ["valid", "valid"]);
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_hook_past_its_deadline_drops_its_event() {
    let ingestion = Ingestion::accepting().await;
    let running = Arc::new(AtomicUsize::new(0));
    let most = Arc::new(AtomicUsize::new(0));
    let (now, peak) = (running.clone(), most.clone());
    let (lsquare, seen) = client(&ingestion, |options| {
        options
            .before_send_timeout(Duration::from_millis(150))
            .max_concurrent_hooks(2)
            .before_send(move |event| {
                peak.fetch_max(now.fetch_add(1, Ordering::SeqCst) + 1, Ordering::SeqCst);
                std::thread::sleep(Duration::from_millis(400));
                now.fetch_sub(1, Ordering::SeqCst);
                Some(event)
            })
    });
    let mcp = connect(instrument(
        Tools::default(),
        &lsquare,
        InstrumentOptions::new(),
    ))
    .await;
    echo(&mcp, 3).await;
    assert_eq!(lsquare.flush().await.failed, 3);
    assert_eq!(seen.codes(), ["hook_timeout"; 3]);
    assert_eq!(
        seen.errors()[0].message,
        "before_send did not finish within 150 ms; the event was dropped."
    );
    assert!(ingestion.requests().is_empty());
    // Never more at once than the limit, even past their deadline.
    until(|| running.load(Ordering::SeqCst) == 0).await;
    assert_eq!(most.load(Ordering::SeqCst), 2);
}

#[tokio::test]
async fn redaction_follows_the_options() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| {
        options
            .redact_pattern(Pattern::Email, false)
            .redact_keys(["Customer-Name"])
    });
    let mcp = connect(instrument(
        Tools::default(),
        &lsquare,
        InstrumentOptions::new(),
    ))
    .await;
    call(&mcp, "echo", json!({ "text": "ann@example.com at 10.0.0.1", "customerName": "Ann", "nested": { "customer_name": "Bob" } })).await.unwrap();
    let (off, _) = client(&ingestion, |options| options.redact(false));
    let unredacted = connect(instrument(Tools::default(), &off, InstrumentOptions::new())).await;
    call(
        &unredacted,
        "echo",
        json!({ "text": "ann@example.com", "password": "hunter2" }),
    )
    .await
    .unwrap();
    lsquare.flush().await;
    off.flush().await;
    let events = ingestion.events();
    assert_eq!(
        events[0]["input"],
        json!({ "text": "ann@example.com at [IP_1]", "customerName": "[SECRET_1]", "nested": { "customer_name": "[SECRET_2]" } })
    );
    assert_eq!(events[0]["redactions"], json!({ "secret": 2, "ip": 2 }));
    assert_eq!(
        events[1]["input"],
        json!({ "text": "ann@example.com", "password": "hunter2" })
    );
    assert!(events[1].get("redactions").is_none());
}

#[tokio::test]
async fn a_daily_limit_holds_events_back() {
    let resets_at = "2999-01-01T00:00:00.000Z";
    let ingestion = Ingestion::start(move |_, received| {
        let rejected: Vec<_> = received
            .ids()
            .into_iter()
            .map(|id| json!({ "eventId": id, "reason": "quota_exceeded", "resetsAt": resets_at }))
            .collect();
        Reply::json(json!({ "accepted": [], "duplicates": [], "rejected": rejected }))
    })
    .await;
    let (lsquare, seen) = client(&ingestion, |options| options);
    let mcp = connect(instrument(
        Tools::default(),
        &lsquare,
        InstrumentOptions::new(),
    ))
    .await;
    echo(&mcp, 1).await;
    assert_eq!(lsquare.flush().await.failed, 1);
    // Reported once, with when it resets: a time more than 25 hours away means the next midnight.
    assert_eq!(seen.codes(), ["quota_exceeded"]);
    let reset = seen.errors()[0].resets_at.clone().unwrap();
    assert!(
        reset.ends_with("T00:00:00.000Z") && reset.as_str() < resets_at,
        "{reset}"
    );
    assert!(seen.errors()[0]
        .message
        .starts_with("The project's daily limit was reached; events are dropped until 00:00 UTC."));
    // Meanwhile nothing more is sent, nor reported one by one.
    echo(&mcp, 5).await;
    assert_eq!(lsquare.flush().await, FlushResult::default());
    assert_eq!(ingestion.requests().len(), 1);
    assert_eq!(seen.codes(), ["quota_exceeded"]);
}

#[tokio::test]
async fn an_event_that_cannot_fit_drops_nothing_else() {
    let gate = Arc::new(Notify::new());
    let held = gate.clone();
    let ingestion = Ingestion::start(move |count, received| {
        if count == 0 {
            Reply::accept(received).when(&held)
        } else {
            Reply::accept(received)
        }
    })
    .await;
    let (lsquare, seen) = client(&ingestion, |options| {
        options.max_concurrent_requests(1).max_queue_bytes(4_000)
    });
    let mcp = connect(instrument(
        Tools::default(),
        &lsquare,
        InstrumentOptions::new(),
    ))
    .await;
    echo(&mcp, 1).await;
    until(|| ingestion.requests().len() == 1).await;
    echo(&mcp, 2).await;
    // Larger than the whole queue may be: dropping the two that wait wouldn't make room for it.
    call(&mcp, "echo", json!({ "text": "lorem ipsum ".repeat(600) }))
        .await
        .unwrap();
    until(|| seen.codes().len() == 1).await;
    gate.notify_one();
    lsquare.flush().await;
    until(|| ingestion.events().len() == 3).await;
    assert_eq!(seen.codes(), ["queue_full"]);
    assert_eq!(
        seen.errors()[0].message,
        "The queue is full; the event was dropped."
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_flush_waits_until_on_error_was_told() {
    let ingestion = Ingestion::start(|_, _| Reply::status(400)).await;
    let hold = common::Hold::default();
    let held = hold.clone();
    let (lsquare, _) = client(&ingestion, |options| {
        options.on_error(move |_, _| held.wait())
    });
    let mcp = connect(instrument(
        Tools::default(),
        &lsquare,
        InstrumentOptions::new(),
    ))
    .await;
    echo(&mcp, 1).await;
    // LSquare refused the event and `on_error` is being told. The event has ended, so a flush
    // that starts now has no event to count, but it doesn't return before the handler has.
    until(|| hold.entered() == 1).await;
    let flushing = tokio::spawn({
        let lsquare = lsquare.clone();
        async move { lsquare.flush_within(Duration::from_secs(5)).await }
    });
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert!(!flushing.is_finished());
    hold.release();
    assert_eq!(flushing.await.unwrap(), FlushResult::default());
    // One that holds on past the flush's time doesn't hold the flush: it returns at its deadline.
    let stuck = common::Hold::default();
    let held = stuck.clone();
    let (lsquare, _) = client(&ingestion, |options| {
        options.on_error(move |_, _| held.wait())
    });
    let mcp = connect(instrument(
        Tools::default(),
        &lsquare,
        InstrumentOptions::new(),
    ))
    .await;
    echo(&mcp, 1).await;
    until(|| stuck.entered() == 1).await;
    let started = Instant::now();
    lsquare.flush_within(Duration::from_millis(200)).await;
    assert!(started.elapsed() < Duration::from_secs(2));
    stuck.release();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn shutdown_counts_an_event_still_being_built_once() {
    let ingestion = Ingestion::accepting().await;
    let hold = common::Hold::default();
    let held = hold.clone();
    let (lsquare, seen) = client(&ingestion, |options| {
        options
            .before_send_timeout(Duration::from_secs(10))
            .before_send(move |_| {
                held.wait();
                None
            })
    });
    let mcp = connect(instrument(
        Tools::default(),
        &lsquare,
        InstrumentOptions::new(),
    ))
    .await;
    echo(&mcp, 1).await;
    until(|| hold.entered() == 1).await;
    // Two at once: the event is dropped, counted and reported by one of them.
    let (first, second) = tokio::join!(
        lsquare.shutdown_within(Duration::ZERO),
        lsquare.shutdown_within(Duration::ZERO)
    );
    assert_eq!(
        first,
        FlushResult {
            sent: 0,
            skipped: 0,
            failed: 1,
            pending: 0
        }
    );
    assert_eq!(second, FlushResult::default());
    assert_eq!(seen.codes(), ["shutdown"]);
    assert_eq!(
        seen.errors()[0].message,
        "shutdown() ended before the event was sent."
    );
    // It was counted: no later flush counts it again.
    assert_eq!(lsquare.flush().await, FlushResult::default());
    // Nor does what its hook answers in the end change what was said of it.
    hold.release();
    tokio::time::sleep(Duration::from_millis(200)).await;
    assert_eq!(lsquare.flush().await, FlushResult::default());
    assert_eq!(seen.codes(), ["shutdown"]);
    assert!(ingestion.requests().is_empty());
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_handler_that_holds_on_is_owed_a_bounded_number_of_reports() {
    let ingestion = Ingestion::start(|_, _| Reply::status(400)).await;
    let hold = common::Hold::default();
    let held = hold.clone();
    let (lsquare, seen) = client(&ingestion, |options| {
        options.on_error(move |_, _| held.wait())
    });
    let mcp = connect(instrument(
        Tools::default(),
        &lsquare,
        InstrumentOptions::new(),
    ))
    .await;
    // The handler holds the first report, and 130 more events fail behind it.
    echo(&mcp, 1).await;
    until(|| hold.entered() == 1).await;
    echo(&mcp, 130).await;
    until(|| ingestion.events().len() == 131).await;
    // Shutdown gives the handler a second past its own time, no more.
    let started = Instant::now();
    lsquare.shutdown_within(Duration::ZERO).await;
    assert!(started.elapsed() < Duration::from_secs(3));
    // A hundred reports waited behind the one in hand; the others were let go, which is said.
    hold.release();
    until(|| hold.entered() == 101).await;
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert_eq!(hold.entered(), 101);
    assert!(seen
        .lines()
        .iter()
        .any(|line| line.contains("isn't keeping up")));
}

#[tokio::test]
async fn a_hook_that_returns_late_is_late_whenever_its_answer_is_seen() {
    let ingestion = Ingestion::accepting().await;
    let started = Arc::new(AtomicUsize::new(0));
    let entered = started.clone();
    let (lsquare, seen) = client(&ingestion, |options| {
        options
            .before_send_timeout(Duration::from_millis(100))
            .before_send(move |event| {
                entered.fetch_add(1, Ordering::SeqCst);
                std::thread::sleep(Duration::from_millis(300));
                Some(event)
            })
    });
    let mcp = connect(instrument(
        Tools::default(),
        &lsquare,
        InstrumentOptions::new(),
    ))
    .await;
    echo(&mcp, 1).await;
    until(|| started.load(Ordering::SeqCst) == 1).await;
    // This runtime's one thread is held until the hook has returned, past its deadline: when
    // the runtime next looks, the answer is there, ready, and it is late all the same.
    std::thread::sleep(Duration::from_millis(600));
    assert_eq!(lsquare.flush().await.failed, 1);
    assert_eq!(seen.codes(), ["hook_timeout"]);
    assert!(ingestion.requests().is_empty());
}

fn runtime() -> tokio::runtime::Runtime {
    tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .unwrap()
}

#[test]
fn a_request_gives_its_events_back_when_its_runtime_ends() {
    // The ingestion has a runtime of its own, alive throughout. Its first answer never comes.
    let outside = runtime();
    let ingestion = outside.block_on(Ingestion::start(|count, received| {
        Reply::accept(received).after(Duration::from_secs(if count == 0 { 60 } else { 0 }))
    }));
    let first = runtime();
    let (lsquare, seen) = first.block_on(async {
        let (lsquare, seen) = client(&ingestion, |options| options);
        let mcp = connect(instrument(
            Tools::default(),
            &lsquare,
            InstrumentOptions::new(),
        ))
        .await;
        echo(&mcp, 1).await;
        until(|| ingestion.requests().len() == 1).await;
        (lsquare, seen)
    });
    // The runtime ends with the request in flight: the event goes back to the queue, and the next
    // runtime that asks sends it.
    drop(first);
    let second = runtime();
    assert_eq!(second.block_on(lsquare.flush()).sent, 1);
    let requests = ingestion.requests();
    assert_eq!(requests.len(), 2);
    assert_eq!(requests[0].ids(), requests[1].ids());
    assert!(seen.codes().is_empty(), "{:?}", seen.errors());
}

#[test]
fn a_flush_already_waiting_sends_what_a_dying_runtime_gives_back() {
    let outside = runtime();
    let ingestion = outside.block_on(Ingestion::start(|count, received| {
        Reply::accept(received).after(Duration::from_secs(if count == 0 { 60 } else { 0 }))
    }));
    let first = runtime();
    let lsquare = first.block_on(async {
        let (lsquare, _) = client(&ingestion, |options| options);
        let mcp = connect(instrument(
            Tools::default(),
            &lsquare,
            InstrumentOptions::new(),
        ))
        .await;
        echo(&mcp, 1).await;
        until(|| ingestion.requests().len() == 1).await;
        lsquare
    });
    // The flush is waiting on another runtime when the first one ends: it sends the event itself.
    let second = runtime();
    let flushing = second.spawn({
        let lsquare = lsquare.clone();
        async move { lsquare.flush_within(Duration::from_secs(5)).await }
    });
    std::thread::sleep(Duration::from_millis(200));
    drop(first);
    assert_eq!(second.block_on(flushing).unwrap().sent, 1);
    assert_eq!(ingestion.requests().len(), 2);
}

#[test]
fn an_event_whose_runtime_ends_before_it_is_built_is_reported() {
    let outside = runtime();
    let ingestion = outside.block_on(Ingestion::accepting());
    let hold = common::Hold::default();
    let held = hold.clone();
    let working = runtime();
    let (lsquare, seen) = working.block_on(async {
        let (lsquare, seen) = client(&ingestion, |options| {
            options
                .before_send_timeout(Duration::from_secs(30))
                .before_send(move |event| {
                    held.wait();
                    Some(event)
                })
        });
        let mcp = connect(instrument(
            Tools::default(),
            &lsquare,
            InstrumentOptions::new(),
        ))
        .await;
        echo(&mcp, 1).await;
        until(|| hold.entered() == 1).await;
        (lsquare, seen)
    });
    // The runtime ends with the event in its hook's hands: nothing is left to build it on.
    drop(working);
    hold.release();
    // It is dropped, and `on_error` is told, on its own thread, by the time a flush returns.
    let second = runtime();
    assert_eq!(second.block_on(lsquare.flush()), FlushResult::default());
    assert_eq!(seen.codes(), ["internal_error"]);
    assert_eq!(
        seen.errors()[0].message,
        "The Tokio runtime ended before the event was built; it was dropped."
    );
    assert!(ingestion.requests().is_empty());
}

#[test]
fn a_hook_that_never_returns_holds_up_neither_shutdown_nor_the_runtimes_end() {
    let outside = runtime();
    let ingestion = outside.block_on(Ingestion::accepting());
    let working = runtime();
    let (lsquare, seen) = working.block_on(async {
        let (lsquare, seen) = client(&ingestion, |options| {
            options
                .before_send_timeout(Duration::from_millis(100))
                .before_send(|event| {
                    std::thread::sleep(Duration::from_secs(30));
                    Some(event)
                })
        });
        let mcp = connect(instrument(
            Tools::default(),
            &lsquare,
            InstrumentOptions::new(),
        ))
        .await;
        echo(&mcp, 1).await;
        assert_eq!(lsquare.shutdown().await.failed, 1);
        (lsquare, seen)
    });
    assert_eq!(seen.codes(), ["hook_timeout"]);
    // The hook still sleeps on a thread of its own, which belongs to no runtime.
    let started = Instant::now();
    drop(working);
    drop(lsquare);
    assert!(
        started.elapsed() < Duration::from_secs(5),
        "{:?}",
        started.elapsed()
    );
}
