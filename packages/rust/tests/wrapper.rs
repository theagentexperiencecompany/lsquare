//! The wrapper, end to end: a real rmcp client calls a real rmcp server in memory, and the events
//! reach a stand-in for LSquare's ingestion.

mod common;

use std::future::Future;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;

use common::{call, client, connect, ingestion_says, until, Ingestion, Tools, ECHO_DESCRIPTION};
use lsquare::mcp::{instrument, IdentifyError, Identity, InstrumentOptions};
use lsquare::ErrorCode;
use rmcp::model::CallToolRequestParams;
use serde_json::{json, Value};

#[tokio::test]
async fn a_call_becomes_one_tool_call_event() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, seen) = client(&ingestion, |options| options);
    let mcp = connect(instrument(
        Tools::default(),
        &lsquare,
        InstrumentOptions::new(),
    ))
    .await;
    mcp.list_all_tools().await.unwrap();

    let result = call(&mcp, "echo", json!({ "text": "hello", "2": 1, "10": 2 }))
        .await
        .unwrap();
    assert_eq!(result.content[0].as_text().unwrap().text, "hello");
    assert_eq!(lsquare.flush().await.sent, 1);

    let events = ingestion.events();
    assert_eq!(events.len(), 1);
    let event = &events[0];
    assert_eq!(event["kind"], "tool_call");
    assert_eq!(event["schemaVersion"], 2);
    assert_eq!(event["source"], "live");
    assert_eq!(
        event["sdk"],
        json!({ "name": "lsquare", "version": env!("CARGO_PKG_VERSION"), "language": "rust" })
    );
    assert_eq!(event["eventId"], event["callId"]);
    assert_eq!(event["eventId"].as_str().unwrap().len(), 36);
    assert_eq!(event["toolName"], "echo");
    assert_eq!(event["toolDescription"], ECHO_DESCRIPTION);
    assert_eq!(event["status"], "completed");
    assert!(event["durationMs"].is_u64());
    assert_eq!(
        event["server"],
        json!({ "name": "test-server", "version": "1.2.3" })
    );
    assert_eq!(
        event["client"],
        json!({ "name": "test-app", "version": "4.5.6" })
    );
    assert!(event["protocolVersion"].as_str().unwrap().starts_with("20"));
    // The arguments in JavaScript's order, as the other SDKs record them: whole numbers first.
    assert_eq!(
        event["input"].to_string(),
        r#"{"2":1,"10":2,"text":"hello"}"#
    );
    assert_eq!(
        event["result"],
        json!({ "content": [{ "type": "text", "text": "hello" }], "isError": false })
    );
    assert!(event.get("error").is_none() && event.get("userId").is_none());
    assert!(seen.codes().is_empty(), "{:?}", seen.errors());
    assert_eq!(ingestion.requests()[0].path, "/v1/turns");
    assert_eq!(
        ingestion.requests()[0].authorization.as_deref(),
        Some("Bearer lsq_test_key")
    );
    assert_eq!(
        ingestion.requests()[0].content_type.as_deref(),
        Some("application/json")
    );
    if let Some(says) = ingestion_says(&events) {
        assert_eq!(says, ["valid"]);
    }
}

#[tokio::test]
async fn a_client_that_never_named_itself_has_no_name() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    // rmcp's default client says it is rmcp itself, as rmcp does for a request that came with no
    // name at all: neither names the calling app.
    let mcp = common::connect_as(
        instrument(Tools::default(), &lsquare, InstrumentOptions::new()),
        (),
    )
    .await;
    call(&mcp, "echo", json!({ "text": "hello" }))
        .await
        .unwrap();
    lsquare.flush().await;
    assert!(ingestion.events()[0].get("client").is_none());
    assert!(ingestion.events()[0]["protocolVersion"].is_string());
}

#[tokio::test]
async fn the_description_is_the_registered_one_or_else_the_one_last_listed() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    let listed = connect(instrument(
        Tools::default(),
        &lsquare,
        InstrumentOptions::new(),
    ))
    .await;
    // No page has shown the tool yet: no description.
    call(&listed, "echo", json!({})).await.unwrap();
    listed.list_all_tools().await.unwrap();
    call(&listed, "echo", json!({})).await.unwrap();
    let registered = connect(instrument(
        Tools {
            registered: true,
            ..Tools::default()
        },
        &lsquare,
        InstrumentOptions::new(),
    ))
    .await;
    call(&registered, "echo", json!({})).await.unwrap();
    let off = connect(instrument(
        Tools {
            registered: true,
            ..Tools::default()
        },
        &lsquare,
        InstrumentOptions::new().descriptions(false),
    ))
    .await;
    call(&off, "echo", json!({})).await.unwrap();
    lsquare.flush().await;
    let descriptions: Vec<Value> = ingestion
        .events()
        .iter()
        .map(|event| event.get("toolDescription").cloned().unwrap_or(Value::Null))
        .collect();
    assert_eq!(
        descriptions,
        [
            Value::Null,
            json!(ECHO_DESCRIPTION),
            json!("The registered description."),
            Value::Null
        ]
    );
}

#[tokio::test]
async fn inputs_and_results_are_recorded_where_the_options_say() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    let options = InstrumentOptions::new()
        .inputs(false)
        .results_for(|tool| tool != "echo");
    let mcp = connect(instrument(Tools::default(), &lsquare, options)).await;
    call(&mcp, "echo", json!({ "text": "secret plans" }))
        .await
        .unwrap();
    call(&mcp, "image", json!({})).await.unwrap();
    lsquare.flush().await;
    let events = ingestion.events();
    assert!(events[0].get("input").is_none() && events[0].get("result").is_none());
    assert!(!events[0].to_string().contains("secret plans"));
    assert!(events[1].get("input").is_none());
    // A file is its size, never its data.
    assert_eq!(
        events[1]["result"]["content"],
        json!([{ "type": "image", "mimeType": "image/png", "bytes": 4 }, { "type": "text", "text": "a picture" }])
    );
    assert_eq!(events[1]["truncated"], json!(["result"]));
}

#[tokio::test]
async fn results_are_shortened_and_redacted() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    let mcp = connect(instrument(
        Tools::default(),
        &lsquare,
        InstrumentOptions::new(),
    ))
    .await;
    call(&mcp, "long", json!({})).await.unwrap();
    call(
        &mcp,
        "structured",
        json!({ "note": "from ann@example.com, card 4111 1111 1111 1111", "apiKey": "abc123" }),
    )
    .await
    .unwrap();
    lsquare.flush().await;
    let events = ingestion.events();
    let text = events[0]["result"]["content"][0]["text"].as_str().unwrap();
    assert!(
        text.ends_with("…[+2000 chars]")
            && text.chars().count() == 4_000 + "…[+2000 chars]".chars().count()
    );
    assert_eq!(events[0]["truncated"], json!(["result"]));
    assert_eq!(
        events[1]["input"],
        json!({ "note": "from [EMAIL_1], card [CARD_1]", "apiKey": "[SECRET_1]" })
    );
    assert_eq!(
        events[1]["result"]["structuredContent"],
        json!({ "password": "[SECRET_2]", "count": 2 })
    );
    assert_eq!(
        events[1]["redactions"],
        json!({ "secret": 2, "email": 1, "card": 1 })
    );
    if let Some(says) = ingestion_says(&events) {
        assert_eq!(says, ["valid", "valid"]);
    }
}

#[tokio::test]
async fn a_failed_call_says_how_it_failed() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    let mcp = connect(instrument(
        Tools::default(),
        &lsquare,
        InstrumentOptions::new(),
    ))
    .await;
    assert!(call(&mcp, "refused", json!({})).await.is_err());
    assert_eq!(
        call(&mcp, "tool_error", json!({})).await.unwrap().is_error,
        Some(true)
    );
    // Without both inputs and results, a failure's text isn't kept: it can quote either.
    let quiet = connect(instrument(
        Tools::default(),
        &lsquare,
        InstrumentOptions::new().results(false),
    ))
    .await;
    assert!(call(&quiet, "refused", json!({})).await.is_err());
    call(&quiet, "tool_error", json!({})).await.unwrap();
    lsquare.flush().await;
    let events = ingestion.events();
    assert_eq!(events[0]["status"], "error");
    assert_eq!(
        events[0]["error"],
        json!({ "message": "bad arguments", "type": "ErrorData", "code": -32602, "category": "protocol" })
    );
    assert!(events[0].get("result").is_none());
    assert_eq!(events[1]["status"], "error");
    assert_eq!(
        events[1]["error"],
        json!({ "message": "it broke for [EMAIL_1]", "category": "tool_error" })
    );
    assert_eq!(
        events[1]["result"],
        json!({ "content": [{ "type": "text", "text": "it broke for [EMAIL_1]" }], "isError": true })
    );
    assert_eq!(
        events[2]["error"],
        json!({ "type": "ErrorData", "code": -32602, "category": "protocol" })
    );
    assert_eq!(events[3]["error"], json!({ "category": "tool_error" }));
    assert!(events[3].get("result").is_none());
    if let Some(says) = ingestion_says(&events) {
        assert_eq!(says, ["valid"; 4]);
    }
}

#[tokio::test]
async fn a_cancelled_call_is_a_timeout() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    let mcp = connect(instrument(
        Tools::default(),
        &lsquare,
        InstrumentOptions::new(),
    ))
    .await;
    // Dropping the client's call cancels the request.
    let _ = tokio::time::timeout(
        Duration::from_millis(100),
        mcp.call_tool(CallToolRequestParams::new("slow")),
    )
    .await;
    until(|| ingestion.events().len() == 1).await;
    let event = &ingestion.events()[0];
    assert_eq!(event["status"], "error");
    assert_eq!(
        event["error"],
        json!({ "category": "timeout", "message": "The request was cancelled before the tool finished." })
    );
}

#[tokio::test]
async fn a_call_let_go_before_its_answer_is_recorded_as_cancelled() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    let server = instrument(Tools::default(), &lsquare, InstrumentOptions::new());
    let (server_transport, client_transport) = tokio::io::duplex(64 * 1024);
    let serving = tokio::spawn(async move {
        use rmcp::ServiceExt;
        if let Ok(running) = server.serve(server_transport).await {
            let _ = running.waiting().await;
        }
    });
    let mcp = {
        use rmcp::ServiceExt;
        common::App.serve(client_transport).await.unwrap()
    };
    let calling =
        tokio::spawn(
            async move { call(&mcp, "slow", json!({ "text": "to ann@example.com" })).await },
        );
    tokio::time::sleep(Duration::from_millis(100)).await;
    // The server's side ends with the call still running.
    serving.abort();
    calling.abort();
    until(|| ingestion.events().len() == 1).await;
    let event = &ingestion.events()[0];
    assert_eq!(event["toolName"], "slow");
    assert_eq!(event["status"], "error");
    assert_eq!(event["error"]["category"], "timeout");
    assert_eq!(event["input"], json!({ "text": "to [EMAIL_1]" }));
}

#[tokio::test]
async fn a_tool_that_panics_is_recorded_and_still_panics() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    let mcp = connect(instrument(
        Tools::default(),
        &lsquare,
        InstrumentOptions::new(),
    ))
    .await;
    // What the MCP SDK makes of a panicking handler is its own business: the event is ours.
    let _ = tokio::time::timeout(Duration::from_millis(500), call(&mcp, "panic", json!({}))).await;
    until(|| ingestion.events().len() == 1).await;
    let event = &ingestion.events()[0];
    assert_eq!(event["status"], "error");
    assert_eq!(
        event["error"],
        json!({ "message": "the tool panicked", "type": "panic", "category": "thrown" })
    );
}

#[tokio::test]
async fn identify_says_who_a_call_was_for() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, seen) = client(&ingestion, |options| options);
    let options = InstrumentOptions::new().identify(|input| async move {
        assert_eq!(input.tool_name(), "echo");
        let user = input
            .arguments()
            .and_then(|arguments| arguments.get("text"))
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_owned();
        assert!(input.context().client_info().is_some());
        Ok(Identity::new()
            .org_id("org_1")
            .user_id(user)
            .conversation_id("")
            .metadata("plan", "pro")
            .metadata("seats", 3))
    });
    let mcp = connect(instrument(Tools::default(), &lsquare, options)).await;
    call(&mcp, "echo", json!({ "text": "user_42" }))
        .await
        .unwrap();
    assert_eq!(lsquare.flush().await.sent, 1);
    let event = &ingestion.events()[0];
    assert_eq!(event["orgId"], "org_1");
    assert_eq!(event["userId"], "user_42");
    assert!(event.get("conversationId").is_none());
    assert_eq!(event["metadata"], json!({ "plan": "pro", "seats": 3 }));
    assert!(event.get("identity").is_none());
    assert!(seen.codes().is_empty());
}

#[tokio::test]
async fn a_call_identify_cannot_place_is_sent_as_limited_diagnostics() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, seen) = client(&ingestion, |options| options);
    let calls = Arc::new(AtomicUsize::new(0));
    let counted = calls.clone();
    let options = InstrumentOptions::new().identify(move |_| {
        let nth = counted.fetch_add(1, Ordering::SeqCst);
        async move {
            match nth {
                0 => Err(std::io::Error::other("the lookup failed"))?,
                1 => Ok(Identity::new().user_id("x".repeat(600))),
                2 => panic!("identify panicked"),
                _ => {
                    tokio::time::sleep(Duration::from_secs(30)).await;
                    Ok::<_, IdentifyError>(Identity::new().user_id("too_late"))
                }
            }
        }
    });
    let mcp = connect(instrument(Tools::default(), &lsquare, options)).await;
    for _ in 0..3 {
        call(&mcp, "echo", json!({ "text": "ann@example.com" }))
            .await
            .unwrap();
    }
    call(&mcp, "tool_error", json!({ "text": "ann@example.com" }))
        .await
        .unwrap();
    // The last one waits its second for identify. A flush counts only what is still on its way,
    // so the events themselves are waited for.
    lsquare.flush_within(Duration::from_secs(3)).await;
    until(|| ingestion.events().len() == 4).await;
    let events = ingestion.events();
    assert_eq!(events.len(), 4);
    for event in &events {
        let mut fields: Vec<&str> = event
            .as_object()
            .unwrap()
            .keys()
            .map(String::as_str)
            .collect();
        fields.sort_unstable();
        let expected: &[&str] = if event["status"] == "error" {
            &[
                "callId",
                "capturedAt",
                "durationMs",
                "error",
                "eventId",
                "identity",
                "kind",
                "schemaVersion",
                "sdk",
                "source",
                "status",
                "toolName",
            ]
        } else {
            &[
                "callId",
                "capturedAt",
                "durationMs",
                "eventId",
                "identity",
                "kind",
                "schemaVersion",
                "sdk",
                "source",
                "status",
                "toolName",
            ]
        };
        assert_eq!(fields, expected);
        assert_eq!(event["identity"], "failed");
        assert!(!event.to_string().contains("example.com"));
    }
    assert_eq!(
        events
            .iter()
            .find(|event| event["status"] == "error")
            .unwrap()["error"],
        json!({ "category": "tool_error" })
    );
    assert_eq!(seen.codes(), ["identify_failed"; 4]);
    let mut reasons: Vec<String> = seen
        .errors()
        .into_iter()
        .map(|error| error.message)
        .collect();
    reasons.sort();
    assert_eq!(
        reasons,
        [
            "identify failed; the call was sent as limited diagnostics.",
            "identify panicked; the call was sent as limited diagnostics.",
            "identify returned an invalid user_id; the call was sent as limited diagnostics.",
            "identify took longer than its deadline; the call was sent as limited diagnostics.",
        ]
    );
    if let Some(says) = ingestion_says(&events) {
        assert_eq!(says, ["valid"; 4]);
    }
}

#[tokio::test]
async fn every_other_method_reaches_the_handler() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    let mcp = connect(instrument(
        Tools::default(),
        &lsquare,
        InstrumentOptions::new(),
    ))
    .await;
    let prompts = mcp.list_all_prompts().await.unwrap();
    assert_eq!(prompts[0].name, "the-handlers-own");
    assert_eq!(
        mcp.peer_info().unwrap().server_info.clone().unwrap().name,
        "test-server"
    );
    assert_eq!(mcp.list_all_tools().await.unwrap().len(), 2);
}

#[tokio::test]
async fn dry_run_sends_nothing_and_says_what_it_would_send() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, seen) = client(&ingestion, |options| options.dry_run(true));
    let mcp = connect(instrument(
        Tools::default(),
        &lsquare,
        InstrumentOptions::new(),
    ))
    .await;
    call(&mcp, "echo", json!({ "text": "to ann@example.com" }))
        .await
        .unwrap();
    until(|| seen.lines().len() == 2).await;
    assert_eq!(lsquare.flush().await.sent, 0);
    assert!(ingestion.requests().is_empty());
    let lines = seen.lines();
    assert_eq!(
        lines[0],
        r#"[lsquare dry-run] {"server":"test-server","records":"every tools/call"}"#
    );
    let summary: Value =
        serde_json::from_str(lines[1].strip_prefix("[lsquare dry-run] ").unwrap()).unwrap();
    assert_eq!(summary["kind"], "tool_call");
    assert_eq!(summary["eventId"], "the call");
    assert_eq!(
        summary["fields"],
        json!([
            "callId",
            "client",
            "durationMs",
            "input",
            "protocolVersion",
            "result",
            "server",
            "status",
            "toolName"
        ])
    );
    assert_eq!(
        summary.as_object().unwrap().keys().collect::<Vec<_>>(),
        ["kind", "eventId", "fields", "bytes", "redactions"]
    );
    assert_eq!(summary["redactions"], json!({ "email": 2 }));
    assert!(summary["bytes"].as_u64().unwrap() > 100);
    assert!(!lines[1].contains("example.com"));
}

#[tokio::test]
async fn a_disabled_client_records_nothing() {
    let seen = common::Seen::default();
    let lsquare = lsquare::Client::new(seen.options());
    assert!(!lsquare.is_enabled());
    assert_eq!(seen.lines(), ["[lsquare] No api_key: capture is disabled."]);
    let mcp = connect(instrument(
        Tools::default(),
        &lsquare,
        InstrumentOptions::new().identify(|_| async { panic!("never asked") }),
    ))
    .await;
    assert_eq!(
        call(&mcp, "echo", json!({ "text": "hi" }))
            .await
            .unwrap()
            .content[0]
            .as_text()
            .unwrap()
            .text,
        "hi"
    );
    assert_eq!(lsquare.flush().await, lsquare::FlushResult::default());
    assert!(seen.codes().is_empty());
    let _ = ErrorCode::Shutdown;
}

#[tokio::test]
async fn a_handler_wrapped_twice_is_recorded_once_by_the_inner_wrapper() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    // The inner one records no inputs; the outer one, with the defaults, must not record them either.
    let inner = instrument(
        Tools::default(),
        &lsquare,
        InstrumentOptions::new().inputs(false),
    );
    let mcp = connect(instrument(inner, &lsquare, InstrumentOptions::new())).await;
    call(&mcp, "echo", json!({ "text": "secret plans" }))
        .await
        .unwrap();
    call(&mcp, "refused", json!({ "text": "secret plans" }))
        .await
        .unwrap_err();
    lsquare.flush().await;
    until(|| ingestion.events().len() == 2).await;
    tokio::time::sleep(Duration::from_millis(100)).await;
    let events = ingestion.events();
    assert_eq!(events.len(), 2);
    assert!(events.iter().all(|event| event.get("input").is_none()));
    // The tool said it back, and results are recorded: once.
    assert_eq!(events[0]["result"]["content"][0]["text"], "secret plans");
    assert_eq!(events[1]["error"]["category"], "protocol");
}

/// A handler that lets go of the call it passed on, once it has started.
struct LetsGo<H>(H);

impl<H: rmcp::ServerHandler> rmcp::ServerHandler for LetsGo<H> {
    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        context: rmcp::service::RequestContext<rmcp::service::RoleServer>,
    ) -> Result<rmcp::model::CallToolResponse, rmcp::ErrorData> {
        let mut passed_on = Box::pin(self.0.call_tool(request, context));
        let _ = std::future::poll_fn(|context| {
            std::task::Poll::Ready(passed_on.as_mut().poll(context))
        })
        .await;
        drop(passed_on);
        Ok(
            rmcp::model::CallToolResult::success(vec![rmcp::model::ContentBlock::text("let go")])
                .into(),
        )
    }
}

#[tokio::test]
async fn a_call_wrapped_twice_and_let_go_is_recorded_once_by_the_inner_wrapper() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    let inner = instrument(
        Tools::default(),
        &lsquare,
        InstrumentOptions::new().inputs(false),
    );
    // The tool never answers: the call is polled once, then dropped, wrappers and all.
    let mcp = connect(LetsGo(instrument(
        inner,
        &lsquare,
        InstrumentOptions::new(),
    )))
    .await;
    call(&mcp, "never", json!({ "text": "secret plans" }))
        .await
        .unwrap();
    lsquare.flush().await;
    until(|| ingestion.events().len() == 1).await;
    tokio::time::sleep(Duration::from_millis(100)).await;
    let events = ingestion.events();
    assert_eq!(events.len(), 1);
    assert!(events[0].get("input").is_none());
    assert_eq!(events[0]["error"]["category"], "timeout");
}

/// A handler that counts the calls that got past their first wait.
struct Counts(Arc<AtomicUsize>);

impl rmcp::ServerHandler for Counts {
    async fn call_tool(
        &self,
        _request: CallToolRequestParams,
        _context: rmcp::service::RequestContext<rmcp::service::RoleServer>,
    ) -> Result<rmcp::model::CallToolResponse, rmcp::ErrorData> {
        tokio::task::yield_now().await;
        self.0.fetch_add(1, Ordering::SeqCst);
        Ok(
            rmcp::model::CallToolResult::success(vec![rmcp::model::ContentBlock::text("done")])
                .into(),
        )
    }
}

#[tokio::test]
async fn the_handler_is_polled_only_when_the_wrapper_is() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    let past = Arc::new(AtomicUsize::new(0));
    // Polled once and let go, a call never gets past its first wait: with the wrapper as without.
    let mcp = connect(LetsGo(instrument(
        Counts(past.clone()),
        &lsquare,
        InstrumentOptions::new().identify(|_| async { Ok(Identity::new().user_id("user_42")) }),
    )))
    .await;
    call(&mcp, "anything", json!({})).await.unwrap();
    lsquare.flush().await;
    assert_eq!(past.load(Ordering::SeqCst), 0);
}

/// A handler with a tool of its own, which calls another tool of the server it holds.
struct Relay<H>(H);

impl<H: rmcp::ServerHandler> rmcp::ServerHandler for Relay<H> {
    async fn call_tool(
        &self,
        mut request: CallToolRequestParams,
        context: rmcp::service::RequestContext<rmcp::service::RoleServer>,
    ) -> Result<rmcp::model::CallToolResponse, rmcp::ErrorData> {
        if request.name == "relay" {
            request.name = "echo".into();
        }
        self.0.call_tool(request, context).await
    }
}

#[tokio::test]
async fn a_tool_that_calls_another_instrumented_tool_gives_two_events() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    let inner = instrument(Tools::default(), &lsquare, InstrumentOptions::new());
    let mcp = connect(instrument(Relay(inner), &lsquare, InstrumentOptions::new())).await;
    call(&mcp, "relay", json!({ "text": "hello" }))
        .await
        .unwrap();
    lsquare.flush().await;
    until(|| ingestion.events().len() == 2).await;
    let mut tools: Vec<String> = ingestion
        .events()
        .iter()
        .map(|event| event["toolName"].as_str().unwrap().to_owned())
        .collect();
    tools.sort();
    // Not the same call twice: the outer tool's, and the one it made on its way.
    assert_eq!(tools, ["echo", "relay"]);
}

#[tokio::test]
async fn a_wrapper_further_in_keeps_the_call_after_its_client_was_shut_down() {
    let ingestion = Ingestion::accepting().await;
    let (further_in, _) = client(&ingestion, |options| options);
    let (outer, _) = client(&ingestion, |options| options);
    let inner = instrument(
        Tools::default(),
        &further_in,
        InstrumentOptions::new().inputs(false),
    );
    let mcp = connect(instrument(inner, &outer, InstrumentOptions::new())).await;
    further_in.shutdown().await;
    call(&mcp, "echo", json!({ "text": "secret plans" }))
        .await
        .unwrap();
    // The inner wrapper's options still decide: nothing is recorded with the outer one's.
    assert_eq!(outer.flush().await, lsquare::FlushResult::default());
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert!(ingestion.events().is_empty());

    // A client without a key never recorded anything: its wrapper is no wrapper at all, as in
    // TypeScript, and the outer one records the call.
    let disabled = lsquare::Client::new(common::Seen::default().options());
    let inner = instrument(
        Tools::default(),
        &disabled,
        InstrumentOptions::new().inputs(false),
    );
    let mcp = connect(instrument(inner, &outer, InstrumentOptions::new())).await;
    call(&mcp, "echo", json!({ "text": "hello" }))
        .await
        .unwrap();
    assert_eq!(outer.flush().await.sent, 1);
    assert_eq!(ingestion.events()[0]["input"], json!({ "text": "hello" }));
}

#[tokio::test]
async fn wrapped_twice_only_the_inner_wrapper_asks_who_the_call_is_for() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, seen) = client(&ingestion, |options| options);
    let asked = Arc::new(AtomicUsize::new(0));
    let counted = asked.clone();
    let inner = instrument(
        Tools::default(),
        &lsquare,
        InstrumentOptions::new().identify(|_| async { Ok(Identity::new().user_id("inner")) }),
    );
    let outer = InstrumentOptions::new().identify(move |_| {
        counted.fetch_add(1, Ordering::SeqCst);
        async { Ok(Identity::new().user_id("outer")) }
    });
    let mcp = connect(instrument(inner, &lsquare, outer)).await;
    // The tool takes a while, so a lookup started alongside it has the time to run.
    call(&mcp, "pause", json!({})).await.unwrap();
    assert_eq!(lsquare.flush().await.sent, 1);
    let event = &ingestion.events()[0];
    assert_eq!(event["userId"], "inner");
    assert!(event.get("identity").is_none());
    // The outer wrapper's lookup never ran: it would have been for nothing.
    assert_eq!(asked.load(Ordering::SeqCst), 0);
    assert!(seen.codes().is_empty());
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_flush_waits_until_on_error_was_told_of_identify() {
    let ingestion = Ingestion::accepting().await;
    let hold = common::Hold::default();
    let held = hold.clone();
    let (lsquare, _) = client(&ingestion, |options| {
        options.on_error(move |_, _| held.wait())
    });
    let options = InstrumentOptions::new()
        .identify(|_| async { Err(std::io::Error::other("the lookup failed").into()) });
    let mcp = connect(instrument(Tools::default(), &lsquare, options)).await;
    call(&mcp, "echo", json!({ "text": "hello" }))
        .await
        .unwrap();
    until(|| hold.entered() == 1).await;
    // The event goes meanwhile, as limited diagnostics; a flush still waits for the handler.
    let flushing = tokio::spawn({
        let lsquare = lsquare.clone();
        async move { lsquare.flush_within(Duration::from_secs(5)).await }
    });
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert!(!flushing.is_finished());
    hold.release();
    flushing.await.unwrap();
    until(|| ingestion.events().len() == 1).await;
    assert_eq!(ingestion.events()[0]["identity"], "failed");
}

#[tokio::test]
async fn a_get_tool_that_panics_costs_only_the_description() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, seen) = client(&ingestion, |options| options);
    let server = Tools {
        get_tool_panics: true,
        ..Tools::default()
    };
    let mcp = connect(instrument(server, &lsquare, InstrumentOptions::new())).await;
    assert_eq!(
        call(&mcp, "echo", json!({ "text": "hello" }))
            .await
            .unwrap()
            .content[0]
            .as_text()
            .unwrap()
            .text,
        "hello"
    );
    assert_eq!(lsquare.flush().await.sent, 1);
    let event = &ingestion.events()[0];
    assert_eq!(event["input"], json!({ "text": "hello" }));
    assert!(event.get("toolDescription").is_none());
    assert!(seen.codes().is_empty());
}

#[tokio::test]
async fn an_identify_that_answers_past_its_deadline_is_late_whenever_it_is_seen() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, seen) = client(&ingestion, |options| options);
    // On this one thread, identify holds everything up past its deadline, so its answer is there,
    // ready, by the time anything looks: it is late all the same.
    let options = InstrumentOptions::new().identify(|_| async {
        std::thread::sleep(Duration::from_millis(1_200));
        Ok(Identity::new().user_id("user_42"))
    });
    let mcp = connect(instrument(Tools::default(), &lsquare, options)).await;
    call(&mcp, "echo", json!({ "text": "for ann@example.com" }))
        .await
        .unwrap();
    lsquare.flush().await;
    until(|| ingestion.events().len() == 1).await;
    let event = &ingestion.events()[0];
    assert_eq!(event["identity"], "failed");
    assert!(event.get("userId").is_none() && event.get("input").is_none());
    assert_eq!(
        seen.errors()[0].message,
        "identify took longer than its deadline; the call was sent as limited diagnostics."
    );
}

/// A handler whose `call_tool` panics before it has a future to give.
struct Eager;

impl rmcp::ServerHandler for Eager {
    #[allow(clippy::manual_async_fn)]
    fn call_tool(
        &self,
        request: CallToolRequestParams,
        _context: rmcp::service::RequestContext<rmcp::service::RoleServer>,
    ) -> impl std::future::Future<Output = Result<rmcp::model::CallToolResponse, rmcp::ErrorData>>
           + rmcp::service::MaybeSendFuture
           + '_ {
        assert!(request.name != "eager", "before there is a future");
        std::future::ready(Err(rmcp::ErrorData::invalid_params("no such tool", None)))
    }
}

#[tokio::test]
async fn a_handler_that_panics_before_its_future_is_recorded_as_one_that_panicked() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    let mcp = connect(instrument(Eager, &lsquare, InstrumentOptions::new())).await;
    let answer =
        tokio::time::timeout(Duration::from_millis(500), call(&mcp, "eager", json!({}))).await;
    // The panic went on to the MCP SDK: the client got no result from it.
    assert!(!matches!(answer, Ok(Ok(_))));
    until(|| ingestion.events().len() == 1).await;
    let event = &ingestion.events()[0];
    assert_eq!(
        event["error"],
        json!({ "message": "before there is a future", "type": "panic", "category": "thrown" })
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn on_error_never_runs_before_the_reply_has_left() {
    on_error_waits_for_the_reply().await;
}

/// On a runtime with one thread, a handler that ran on it would hold up the reply it waits for.
#[tokio::test]
async fn on_error_holds_up_no_reply_on_a_runtime_with_one_thread() {
    on_error_waits_for_the_reply().await;
}

async fn on_error_waits_for_the_reply() {
    let gate = Arc::new(tokio::sync::Notify::new());
    let held = gate.clone();
    let ingestion =
        Ingestion::start(move |_, received| common::Reply::accept(received).when(&held)).await;
    // The handler waits to be told the reply arrived: on the reply's own path, that never comes.
    let (replied, reply_seen) = std::sync::mpsc::channel::<()>();
    let reply_seen = std::sync::Mutex::new(reply_seen);
    let waited = Arc::new(std::sync::Mutex::new(None));
    let outcome = waited.clone();
    let (lsquare, _) = client(&ingestion, |options| {
        options.max_queue_events(1).on_error(move |error, _| {
            let told = reply_seen
                .lock()
                .unwrap()
                .recv_timeout(Duration::from_secs(3))
                .is_ok();
            *outcome.lock().unwrap() = Some((error.code, told));
        })
    });
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
    // The queue's one place is taken by the event in the request: this call's is refused.
    call(&mcp, "echo", json!({ "text": "second" }))
        .await
        .unwrap();
    replied.send(()).unwrap();
    until(|| waited.lock().unwrap().is_some()).await;
    assert_eq!(*waited.lock().unwrap(), Some((ErrorCode::QueueFull, true)));
    gate.notify_one();
}

/// The logger is the app's code too: a warning that comes up on a tool's path is written from
/// elsewhere, so a logger that takes its time holds up no reply.
#[tokio::test]
async fn the_logger_holds_up_no_reply() {
    let ingestion = Ingestion::accepting().await;
    let hold = common::Hold::default();
    let held = hold.clone();
    let (lsquare, _) = client(&ingestion, |options| {
        options.logger(move |_, _| held.wait())
    });
    // The choice panics as the call arrives, which is warned of once.
    let options = InstrumentOptions::new().inputs_for(|_| panic!("the choice panicked"));
    let mcp = connect(instrument(Tools::default(), &lsquare, options)).await;
    // By the clock: on this runtime's one thread, a logger called here would hold up the very
    // timer that could say so.
    let started = std::time::Instant::now();
    let answer = call(&mcp, "echo", json!({ "text": "hello" }))
        .await
        .unwrap();
    assert!(
        started.elapsed() < Duration::from_secs(2),
        "the reply waited for the logger: {:?}",
        started.elapsed()
    );
    assert_eq!(answer.content[0].as_text().unwrap().text, "hello");
    until(|| hold.entered() == 1).await;
    hold.release();
    lsquare.flush().await;
    assert!(ingestion.events()[0].get("input").is_none());
}

/// rmcp's streamable HTTP server takes a `ServerHandler`: the wrapper must be one.
#[test]
fn the_wrapper_is_a_server_handler() {
    fn serves<H: rmcp::ServerHandler + Clone>(_: &H) {}
    let lsquare = lsquare::Client::new(lsquare::Options::new().logger(|_, _| {}));
    serves(&instrument(
        Tools::default(),
        &lsquare,
        InstrumentOptions::new(),
    ));
}
