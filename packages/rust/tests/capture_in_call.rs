//! `capture()` inside a tool on an instrumented server, end to end: the call's `agent` part, the
//! turns linked to the call, and what a failed `identify` makes of the two.

mod common;

use std::future::Future;
use std::pin::Pin;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use common::{client, connect, ingestion_says, until, Ingestion, Seen};
use lsquare::mcp::{instrument, IdentifyError, Identity, InstrumentOptions};
use lsquare::{Client, ToolCall, Turn, Usage};
use rmcp::model::{
    CallToolRequestParams, CallToolResponse, CallToolResult, ContentBlock, ProtocolVersion,
    ServerCapabilities, ServerConfig, Tool,
};
use rmcp::service::{RequestContext, RoleClient, RoleServer, RunningService};
use rmcp::{
    ClientLifecycleMode, ClientServiceExt, ErrorData as McpError, ServerHandler, ServiceExt,
};
use serde_json::{json, Map, Value};
use tokio::sync::Notify;

/// The key of the turn `7` of `conv_42` in `org_1`, as TypeScript's `turnKey` makes it.
const KEYED: &str = "k/b2a8ca3c769656a82fc92cbb6c80278318654520ff08c88d0c9008fb68a61cf8";

type Running = Pin<Box<dyn Future<Output = CallToolResult> + Send>>;
type Run = Arc<dyn Fn(String, Map<String, Value>) -> Running + Send + Sync>;

/// A server whose every tool is one function of the test's: given the tool's name and its
/// arguments, it does what an agent's tool would, `capture()` included.
#[derive(Clone)]
struct Agent {
    run: Run,
    /// It answers `get_tool`, as a server made with rmcp's macros does: its tools then take the
    /// inputs the wrapper adds.
    defined: bool,
}

fn agent<F, Fut>(run: F) -> Agent
where
    F: Fn(String, Map<String, Value>) -> Fut + Send + Sync + 'static,
    Fut: Future<Output = CallToolResult> + Send + 'static,
{
    Agent {
        run: Arc::new(move |tool, arguments| Box::pin(run(tool, arguments))),
        defined: false,
    }
}

fn done() -> CallToolResult {
    CallToolResult::success(vec![ContentBlock::text("done")])
}

fn info() -> ServerConfig {
    let mut info = ServerConfig::default();
    info.server_info.name = "agent-server".into();
    info.server_info.version = "1.0.0".into();
    info.capabilities = ServerCapabilities::builder().enable_tools().build();
    info
}

impl ServerHandler for Agent {
    fn get_info(&self) -> ServerConfig {
        info()
    }

    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        _context: RequestContext<RoleServer>,
    ) -> Result<CallToolResponse, McpError> {
        let arguments = request.arguments.unwrap_or_default();
        Ok((self.run)(request.name.to_string(), arguments).await.into())
    }

    fn get_tool(&self, name: &str) -> Option<Tool> {
        self.defined.then(|| {
            Tool::new(
                name.to_owned(),
                "A tool of the test's.",
                common::arguments(
                    json!({ "type": "object", "properties": { "who": { "type": "string" } } }),
                ),
            )
        })
    }
}

/// Calls a tool. A tool that panicked would leave its call unanswered: that is a failure here,
/// not a test that never ends.
async fn call<C: rmcp::ClientHandler>(
    mcp: &RunningService<RoleClient, C>,
    tool: &'static str,
    args: Value,
) -> Result<CallToolResult, rmcp::ServiceError> {
    tokio::time::timeout(Duration::from_secs(20), common::call(mcp, tool, args))
        .await
        .expect("the tool answers")
}

/// A result that says what a tool saw, for the test to check.
fn saw(what: impl std::fmt::Debug) -> CallToolResult {
    CallToolResult::success(vec![ContentBlock::text(format!("{what:?}"))])
}

fn said(result: &CallToolResult) -> String {
    result.content[0]
        .as_text()
        .map(|text| text.text.clone())
        .unwrap_or_default()
}

/// Waits until everything recorded so far was sent.
async fn settled(lsquare: &Client) {
    let flushed = lsquare.flush_within(Duration::from_secs(30)).await;
    assert_eq!(flushed.pending, 0, "{flushed:?}");
}

fn valid(events: &[Value]) {
    if let Some(said) = ingestion_says(events) {
        assert!(said.iter().all(|said| said == "valid"), "{said:?}");
    }
}

fn of_kind<'a>(events: &'a [Value], kind: &str) -> Vec<&'a Value> {
    events
        .iter()
        .filter(|event| event["kind"] == kind)
        .collect()
}

fn text_of(arguments: &Map<String, Value>, key: &str) -> String {
    arguments
        .get(key)
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_owned()
}

fn whole_turn() -> Turn {
    Turn::new()
        .org_id("org_1")
        .user_id("user_8")
        .conversation_id("conv_42")
        .turn_id("7")
        .question("Where is the order of ann@example.com?")
        .answer("It left the warehouse this morning.")
        .tool_call(ToolCall::new("find_order").input(json!({ "email": "ann@example.com" })))
        .tool_call(ToolCall::new("carrier").duration_ms(80))
        .usage(Usage::new().input_tokens(812).output_tokens(64))
        .metadata("plan", "pro")
}

#[tokio::test]
async fn the_first_capture_inside_a_tool_is_the_calls_agent_part() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, seen) = client(&ingestion, |options| options);
    let inside = lsquare.clone();
    let mut server = agent(move |_, _| {
        let lsquare = inside.clone();
        async move {
            lsquare.capture(whole_turn());
            done()
        }
    });
    server.defined = true;
    let mcp = connect(instrument(server, &lsquare, InstrumentOptions::new())).await;
    let result = call(&mcp, "ask", json!({ "text": "from ann@example.com" }))
        .await
        .unwrap();
    // Its conversation groups the call: no session is started for it, nor told to the agent.
    assert_eq!(result.content.len(), 1);
    settled(&lsquare).await;
    assert!(seen.codes().is_empty(), "{:?}", seen.codes());
    let events = ingestion.events();
    valid(&events);
    assert_eq!(events.len(), 1);
    let event = &events[0];
    assert_eq!(event["kind"], "tool_call");
    assert_eq!(
        (&event["orgId"], &event["userId"], &event["conversationId"]),
        (&json!("org_1"), &json!("user_8"), &json!("conv_42"))
    );
    assert_eq!(event["turnRef"], KEYED);
    assert_eq!(
        event["agent"],
        json!({
            "question": "Where is the order of [EMAIL_1]?",
            "answer": "It left the warehouse this morning.",
            "turnId": "7",
            "status": "completed",
            "toolCalls": [
                { "name": "find_order", "input": { "email": "[EMAIL_1]" } },
                { "name": "carrier", "durationMs": 80 },
            ],
            "usage": { "inputTokens": 812, "outputTokens": 64 },
        })
    );
    // One placeholder for one address, wherever in the event it shows.
    assert_eq!(event["input"], json!({ "text": "from [EMAIL_1]" }));
    assert_eq!(event["metadata"], json!({ "plan": "pro" }));
    assert!(event.get("sessionId").is_none(), "{event}");
}

#[tokio::test]
async fn a_later_capture_is_a_turn_linked_to_the_call() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, seen) = client(&ingestion, |options| options);
    let inside = lsquare.clone();
    let known: Arc<Mutex<Vec<Option<String>>>> = Arc::default();
    let ids = known.clone();
    let server = agent(move |_, _| {
        let (lsquare, ids) = (inside.clone(), ids.clone());
        async move {
            ids.lock().unwrap().push(lsquare.current_call_id());
            lsquare.capture(Turn::new().answer("the call's own"));
            lsquare.capture(Turn::new().answer("linked"));
            lsquare.capture(Turn::new().answer("linked, its call ID empty").call_id(""));
            lsquare.capture(Turn::new().answer("another call's").call_id("call_9"));
            // One whose call ID isn't valid is dropped by itself.
            lsquare.capture(Turn::new().answer("dropped").call_id("\0"));
            done()
        }
    });
    let mcp = connect(instrument(server, &lsquare, InstrumentOptions::new())).await;
    call(&mcp, "ask", json!({})).await.unwrap();
    // Outside a call there is none.
    assert_eq!(lsquare.current_call_id(), None);
    settled(&lsquare).await;
    assert_eq!(seen.codes(), ["invalid_id"]);
    assert_eq!(seen.errors()[0].paths, ["callId"]);
    let events = ingestion.events();
    valid(&events);
    let calls = of_kind(&events, "tool_call");
    assert_eq!(calls.len(), 1);
    let call_id = calls[0]["callId"].as_str().unwrap();
    assert_eq!(known.lock().unwrap()[0].as_deref(), Some(call_id));
    assert_eq!(calls[0]["agent"]["answer"], "the call's own");
    let turns: Vec<(&str, &str)> = of_kind(&events, "turn")
        .into_iter()
        .map(|turn| {
            (
                turn["answer"].as_str().unwrap(),
                turn["callId"].as_str().unwrap(),
            )
        })
        .collect();
    assert_eq!(
        turns,
        [
            ("linked", call_id),
            ("linked, its call ID empty", call_id),
            ("another call's", "call_9"),
        ]
    );
}

#[tokio::test]
async fn a_task_the_tool_spawns_is_outside_the_call() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    let inside = lsquare.clone();
    let server = agent(move |_, _| {
        let lsquare = inside.clone();
        async move {
            let call_id = lsquare.current_call_id();
            let (later, given) = (lsquare.clone(), call_id.clone().unwrap_or_default());
            let outside = tokio::spawn(async move {
                later.capture(Turn::new().answer("unlinked"));
                later.capture(Turn::new().answer("linked by hand").call_id(given));
                later.current_call_id()
            })
            .await;
            saw((call_id.is_some(), outside.ok()))
        }
    });
    let mcp = connect(instrument(server, &lsquare, InstrumentOptions::new())).await;
    let result = call(&mcp, "ask", json!({})).await.unwrap();
    // The tool has the call's ID; the task it spawned has none.
    assert_eq!(said(&result), "(true, Some(None))");
    settled(&lsquare).await;
    let events = ingestion.events();
    valid(&events);
    let calls = of_kind(&events, "tool_call");
    // Neither took the call's agent part.
    assert!(calls[0].get("agent").is_none());
    let turns = of_kind(&events, "turn");
    assert_eq!(turns.len(), 2);
    assert!(turns[0].get("callId").is_none());
    assert_eq!(turns[1]["callId"], calls[0]["callId"]);
}

#[tokio::test]
async fn the_captures_ids_come_before_identifys_and_their_metadata_is_one() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, seen) = client(&ingestion, |options| options);
    let inside = lsquare.clone();
    let server = agent(move |_, _| {
        let lsquare = inside.clone();
        async move {
            lsquare.capture(
                Turn::new()
                    .user_id("user_capture")
                    // An empty one is none: identify's stays.
                    .org_id("")
                    .turn_id("7")
                    .metadata("plan", "pro")
                    .metadata("seats", 12),
            );
            done()
        }
    });
    let options = InstrumentOptions::new().identify(|_| async {
        Ok(Identity::new()
            .org_id("org_1")
            .user_id("user_identify")
            .conversation_id("conv_42")
            .metadata("plan", "free")
            .metadata("region", "eu"))
    });
    let mcp = connect(instrument(server, &lsquare, options)).await;
    call(&mcp, "ask", json!({})).await.unwrap();
    settled(&lsquare).await;
    assert!(seen.codes().is_empty(), "{:?}", seen.codes());
    let events = ingestion.events();
    valid(&events);
    let event = &events[0];
    assert_eq!(
        (&event["orgId"], &event["userId"], &event["conversationId"]),
        (&json!("org_1"), &json!("user_capture"), &json!("conv_42"))
    );
    assert_eq!(
        event["metadata"],
        json!({ "plan": "pro", "region": "eu", "seats": 12 })
    );
    // The turn's reference needs the capture's own conversation: identify's doesn't make one.
    assert!(event.get("turnRef").is_none());
    assert_eq!(event["agent"]["turnId"], "7");
}

#[tokio::test]
async fn a_captured_id_that_is_not_valid_drops_the_calls_event() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, seen) = client(&ingestion, |options| options);
    let inside = lsquare.clone();
    let server = agent(move |_, _| {
        let lsquare = inside.clone();
        async move {
            lsquare.capture(Turn::new().org_id("o".repeat(513)).answer("agent"));
            // A turn of its own with such an ID is dropped by itself.
            lsquare.capture(Turn::new().org_id("o".repeat(513)).answer("turn"));
            done()
        }
    });
    let options = InstrumentOptions::new()
        .identify(|_| async { Ok(Identity::new().org_id("org_1").user_id("user_8")) });
    let mcp = connect(instrument(server, &lsquare, options)).await;
    call(&mcp, "ask", json!({})).await.unwrap();
    settled(&lsquare).await;
    let mut paths: Vec<Vec<String>> = seen
        .errors()
        .into_iter()
        .map(|error| {
            assert_eq!(error.code, lsquare::ErrorCode::InvalidId);
            error.paths
        })
        .collect();
    paths.sort();
    assert_eq!(
        paths,
        [vec!["ids.orgId".to_owned()], vec!["orgId".to_owned()]]
    );
    assert!(ingestion.events().is_empty());
}

/// A server whose `identify` does what the call's `who` says, and whose tool captures what its
/// `capture` says: nothing, a turn without IDs, or one with the IDs named.
fn placed(lsquare: &Client) -> impl ServerHandler {
    let inside = lsquare.clone();
    let server = agent(move |_, arguments| {
        let lsquare = inside.clone();
        async move {
            let given = text_of(&arguments, "capture");
            if given != "nothing" {
                let mut turn = Turn::new().answer("agent");
                for id in given.split(',') {
                    turn = match id {
                        "org" => turn.org_id("org_capture"),
                        "user" => turn.user_id("user_capture"),
                        "conversation" => turn.conversation_id("conv_capture"),
                        _ => turn,
                    };
                }
                lsquare.capture(turn);
            }
            done()
        }
    });
    let options = InstrumentOptions::new().identify(|input| async move {
        let who = input
            .arguments()
            .and_then(|arguments| arguments.get("who"))
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_owned();
        match who.as_str() {
            "fails" => Err(IdentifyError::new()),
            "org and user" => Ok(Identity::new().org_id("org_1").user_id("user_8")),
            "nobody" => Ok(Identity::new()),
            _ => panic!("the test names who"),
        }
    });
    instrument(server, lsquare, options)
}

/// Calls the server of `placed`, and gives the call's event with what was said of `identify`.
async fn placing(
    mcp: &common::Mcp,
    lsquare: &Client,
    ingestion: &Ingestion,
    seen: &Seen,
    who: &str,
    capture: &str,
) -> (Value, Option<String>) {
    let before = (ingestion.events().len(), seen.errors().len());
    call(
        mcp,
        "ask",
        json!({ "who": who, "capture": capture, "text": "private" }),
    )
    .await
    .unwrap();
    settled(lsquare).await;
    let events = ingestion.events();
    assert_eq!(events.len(), before.0 + 1);
    let said = seen.errors().get(before.1).map(|error| {
        assert_eq!(error.code, lsquare::ErrorCode::IdentifyFailed);
        error.message.clone()
    });
    (events[before.0].clone(), said)
}

const WHOLE: &str = "identify failed; the call was sent with capture()'s IDs.";
const LIMITED: &str = "identify failed; the call was sent as limited diagnostics.";

/// Whether a call's event went whole: with what the tool was sent.
fn whole(event: &Value) -> bool {
    event.get("input").is_some()
}

#[tokio::test]
async fn a_call_identify_cannot_place_goes_whole_when_its_capture_says_whose_it_is() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, seen) = client(&ingestion, |options| options);
    let mcp = connect(placed(&lsquare)).await;
    let ask = |who: &'static str, capture: &'static str| {
        placing(&mcp, &lsquare, &ingestion, &seen, who, capture)
    };
    // Before identify ever answered, a userId is what is needed.
    let (event, said) = ask("fails", "nothing").await;
    assert!(!whole(&event), "{event}");
    assert_eq!(said.as_deref(), Some(LIMITED));
    let (event, said) = ask("fails", "org").await;
    assert!(!whole(&event), "{event}");
    assert_eq!(said.as_deref(), Some(LIMITED));
    // Limited diagnostics name nobody, and keep nothing the agent said.
    assert_eq!(event["identity"], "failed");
    assert!(event.get("orgId").is_none() && event.get("agent").is_none());
    let (event, said) = ask("fails", "user").await;
    assert!(whole(&event), "{event}");
    assert_eq!(said.as_deref(), Some(WHOLE));
    assert_eq!(event["userId"], "user_capture");
    assert_eq!(event["agent"]["answer"], "agent");

    // Once identify answered with an org and a user, both are needed.
    let (event, said) = ask("org and user", "nothing").await;
    assert!(whole(&event) && said.is_none());
    let (event, said) = ask("fails", "user").await;
    assert!(!whole(&event), "{event}");
    assert_eq!(said.as_deref(), Some(LIMITED));
    let (event, said) = ask("fails", "org,user,conversation").await;
    assert!(whole(&event), "{event}");
    assert_eq!(said.as_deref(), Some(WHOLE));
    assert_eq!(
        (&event["orgId"], &event["userId"], &event["conversationId"]),
        (
            &json!("org_capture"),
            &json!("user_capture"),
            &json!("conv_capture")
        )
    );

    // Once it answered with no ID at all, none is needed: but a capture is, so a tool that
    // gave nothing never has its call sent whole on identify's failure.
    let (event, said) = ask("nobody", "nothing").await;
    assert!(whole(&event) && said.is_none());
    let (event, said) = ask("fails", "nothing").await;
    assert!(!whole(&event), "{event}");
    assert_eq!(said.as_deref(), Some(LIMITED));
    let (event, said) = ask("fails", "none of the IDs").await;
    assert!(whole(&event), "{event}");
    assert_eq!(said.as_deref(), Some(WHOLE));
    valid(&ingestion.events());
}

#[tokio::test]
async fn an_answer_counts_from_when_identify_returns_not_from_when_its_call_ends() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, seen) = client(&ingestion, |options| options);
    let inside = lsquare.clone();
    let release = Arc::new(Notify::new());
    let held = release.clone();
    let server = agent(move |tool, _| {
        let (lsquare, held) = (inside.clone(), held.clone());
        async move {
            if tool == "long" {
                held.notified().await;
            } else {
                lsquare.capture(Turn::new().user_id("user_capture"));
            }
            done()
        }
    });
    let answered = Arc::new(AtomicUsize::new(0));
    let count = answered.clone();
    let options = InstrumentOptions::new().identify(move |input| {
        let count = count.clone();
        async move {
            if input.tool_name() == "long" {
                count.fetch_add(1, Ordering::SeqCst);
                Ok(Identity::new().org_id("org_1").user_id("user_8"))
            } else {
                Err(IdentifyError::new())
            }
        }
    });
    let mcp = Arc::new(connect(instrument(server, &lsquare, options)).await);
    // The first call's identify answers with an org and a user while its tool still runs.
    let long = tokio::spawn({
        let mcp = mcp.clone();
        async move { call(&mcp, "long", json!({})).await.unwrap() }
    });
    until(|| answered.load(Ordering::SeqCst) == 1).await;
    tokio::time::sleep(Duration::from_millis(50)).await;
    // A second call's identify fails meanwhile, and its capture gives a user alone: that is no
    // longer all the client's identify returns.
    call(&mcp, "short", json!({ "text": "private" }))
        .await
        .unwrap();
    until(|| ingestion.events().len() == 1).await;
    let event = &ingestion.events()[0];
    assert!(!whole(event), "{event}");
    assert_eq!(
        seen.errors()[0].message,
        "identify failed; the call was sent as limited diagnostics."
    );
    release.notify_one();
    long.await.unwrap();
    settled(&lsquare).await;
}

#[tokio::test]
async fn a_capture_on_another_client_is_a_turn_of_its_own() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    let (other, _) = client(&ingestion, |options| options);
    let (elsewhere, same) = (other.clone(), lsquare.clone());
    let server = agent(move |_, _| {
        let (other, lsquare) = (elsewhere.clone(), same.clone());
        async move {
            let ids = (
                other.current_call_id(),
                // A clone of the client is that client.
                lsquare.clone().current_call_id().is_some(),
            );
            other.capture(Turn::new().answer("the other client's"));
            // A client made inside the tool logs that it has no key, there and then: what its
            // logger captures is no part of the call.
            let logged = lsquare.clone();
            lsquare::Client::new(lsquare::Options::new().dry_run(false).logger(move |_, _| {
                logged.capture(Turn::new().answer("from a logger"));
            }));
            lsquare
                .clone()
                .capture(Turn::new().answer("the call's own"));
            saw(ids)
        }
    });
    let mcp = connect(instrument(server, &lsquare, InstrumentOptions::new())).await;
    let result = call(&mcp, "ask", json!({})).await.unwrap();
    assert_eq!(said(&result), "(None, true)");
    settled(&lsquare).await;
    settled(&other).await;
    let events = ingestion.events();
    valid(&events);
    assert_eq!(events.len(), 2);
    assert_eq!(
        of_kind(&events, "tool_call")[0]["agent"]["answer"],
        "the call's own"
    );
    let turn = of_kind(&events, "turn")[0];
    assert_eq!(turn["answer"], "the other client's");
    assert!(turn.get("callId").is_none());
}

/// A handler that does its work before it gives its future: a `call_tool` written without
/// `async`.
#[derive(Clone)]
struct Eager {
    lsquare: Client,
}

impl ServerHandler for Eager {
    fn get_info(&self) -> ServerConfig {
        info()
    }

    fn call_tool(
        &self,
        _request: CallToolRequestParams,
        _context: RequestContext<RoleServer>,
    ) -> impl Future<Output = Result<CallToolResponse, McpError>> + Send + '_ {
        self.lsquare
            .capture(Turn::new().answer("before the future"));
        let call_id = self.lsquare.current_call_id();
        async move { Ok(saw(call_id.is_some()).into()) }
    }
}

#[tokio::test]
async fn a_handler_that_captures_before_it_gives_its_future_is_inside_the_call() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    let server = Eager {
        lsquare: lsquare.clone(),
    };
    let mcp = connect(instrument(server, &lsquare, InstrumentOptions::new())).await;
    let result = call(&mcp, "ask", json!({})).await.unwrap();
    assert_eq!(said(&result), "true");
    settled(&lsquare).await;
    let events = ingestion.events();
    assert_eq!(events.len(), 1);
    assert_eq!(events[0]["agent"]["answer"], "before the future");
}

#[tokio::test]
async fn a_cancelled_call_keeps_what_was_captured() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    let inside = lsquare.clone();
    let release = Arc::new(Notify::new());
    let held = release.clone();
    let server = agent(move |_, _| {
        let (lsquare, held) = (inside.clone(), held.clone());
        async move {
            lsquare.capture(Turn::new().answer("before it was cancelled"));
            held.notified().await;
            lsquare.capture(Turn::new().answer("after it was cancelled"));
            done()
        }
    });
    let mcp = connect(instrument(server, &lsquare, InstrumentOptions::new())).await;
    // Dropping the client's call cancels the request; the tool goes on to its end.
    let _ = tokio::time::timeout(
        Duration::from_millis(100),
        mcp.call_tool(CallToolRequestParams::new("ask")),
    )
    .await;
    tokio::time::sleep(Duration::from_millis(50)).await;
    release.notify_one();
    until(|| ingestion.events().len() == 2).await;
    let events = ingestion.events();
    valid(&events);
    let call = of_kind(&events, "tool_call")[0];
    assert_eq!(call["error"]["category"], "timeout");
    assert_eq!(call["agent"]["answer"], "before it was cancelled");
    let turn = of_kind(&events, "turn")[0];
    assert_eq!(turn["answer"], "after it was cancelled");
    assert_eq!(turn["callId"], call["callId"]);
}

#[tokio::test]
async fn wrapped_twice_a_capture_is_the_recording_wrappers() {
    let ingestion = Ingestion::accepting().await;
    let (outer, _) = client(&ingestion, |options| options);
    let (inner, _) = client(&ingestion, |options| options);
    let (out, within) = (outer.clone(), inner.clone());
    let make = move || {
        let (outer, inner) = (out.clone(), within.clone());
        agent(move |_, _| {
            let (outer, inner) = (outer.clone(), inner.clone());
            async move {
                // The wrapper further in records the call: the outer one's client has none.
                let ids = (outer.current_call_id(), inner.current_call_id());
                outer.capture(Turn::new().answer("on the outer wrapper's client"));
                inner.capture(Turn::new().answer("on the inner wrapper's client"));
                CallToolResult::success(vec![ContentBlock::text(format!(
                    "{} {}",
                    ids.0.is_some(),
                    ids.1.is_some()
                ))])
            }
        })
    };
    let twice = instrument(
        instrument(make(), &inner, InstrumentOptions::new()),
        &outer,
        InstrumentOptions::new(),
    );
    let mcp = connect(twice).await;
    let result = call(&mcp, "ask", json!({})).await.unwrap();
    assert_eq!(result.content[0].as_text().unwrap().text, "false true");
    settled(&outer).await;
    settled(&inner).await;
    let events = ingestion.events();
    valid(&events);
    assert_eq!(events.len(), 2);
    assert_eq!(
        of_kind(&events, "tool_call")[0]["agent"]["answer"],
        "on the inner wrapper's client"
    );
    let turn = of_kind(&events, "turn")[0];
    assert_eq!(turn["answer"], "on the outer wrapper's client");
    assert!(turn.get("callId").is_none());

    // With the inner wrapper's client shut down, nobody records the call: a capture on the
    // outer one's is a turn of its own, never a part of a call that is recorded nowhere.
    inner.shutdown().await;
    let result = call(&mcp, "ask", json!({})).await.unwrap();
    assert_eq!(result.content[0].as_text().unwrap().text, "false false");
    settled(&outer).await;
    let events = ingestion.events();
    assert_eq!(events.len(), 3);
    assert_eq!(events[2]["kind"], "turn");
    assert_eq!(events[2]["answer"], "on the outer wrapper's client");
    assert!(events[2].get("callId").is_none());
}

/// A handler between two wrappers: it captures on a client, lets the runtime go round once, as
/// forwarding middleware that awaits something does, and passes the call on.
#[derive(Clone)]
struct Between<H> {
    lsquare: Client,
    inner: H,
}

impl<H: ServerHandler> ServerHandler for Between<H> {
    fn get_info(&self) -> ServerConfig {
        info()
    }

    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        context: RequestContext<RoleServer>,
    ) -> Result<CallToolResponse, McpError> {
        self.lsquare.capture(
            Turn::new()
                .answer("before the inner wrapper")
                .user_id("user_capture"),
        );
        self.lsquare
            .capture(Turn::new().answer("a turn, before it too"));
        tokio::task::yield_now().await;
        self.inner.call_tool(request, context).await
    }
}

#[tokio::test]
async fn a_call_taken_over_by_a_wrapper_further_in_keeps_its_id_and_its_capture() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, seen) = client(&ingestion, |options| options);
    // The outer wrapper's identify answers late, and with no ID: the call is the inner
    // wrapper's by then, and its answer is nobody's rule.
    let outer = InstrumentOptions::new().identify(|_| async {
        tokio::time::sleep(Duration::from_millis(150)).await;
        Ok(Identity::new())
    });
    let inner = InstrumentOptions::new().identify(|input| async move {
        match input.tool_name() {
            "first" => Ok(Identity::new().org_id("org_1").user_id("user_8")),
            _ => Err(IdentifyError::new()),
        }
    });
    let inside = lsquare.clone();
    let tools = agent(move |_, _| {
        let lsquare = inside.clone();
        async move {
            // Not the first of its call: a turn of its own, whatever IDs it gives.
            lsquare.capture(Turn::new().org_id("org_capture").user_id("user_capture"));
            tokio::time::sleep(Duration::from_millis(300)).await;
            done()
        }
    });
    let between = Between {
        lsquare: lsquare.clone(),
        inner: instrument(tools, &lsquare, inner),
    };
    let mcp = connect(instrument(between, &lsquare, outer)).await;
    call(&mcp, "first", json!({})).await.unwrap();
    settled(&lsquare).await;
    let events = ingestion.events();
    valid(&events);
    assert_eq!(events.len(), 3, "{events:?}");
    // Both wrappers have the same client: what was captured before the inner one took the
    // call is the call's agent part, its IDs the call's, and the turns' link is to the event
    // that was recorded.
    let recorded = of_kind(&events, "tool_call")[0];
    assert_eq!(recorded["agent"]["answer"], "before the inner wrapper");
    assert_eq!(
        (&recorded["orgId"], &recorded["userId"]),
        (&json!("org_1"), &json!("user_capture"))
    );
    let turns = of_kind(&events, "turn");
    assert_eq!(turns[0]["answer"], "a turn, before it too");
    assert!(turns
        .iter()
        .all(|turn| turn["callId"] == recorded["callId"]));
    assert!(seen.codes().is_empty(), "{:?}", seen.codes());
    // The outer identify's answer without IDs came once the call was no longer its wrapper's:
    // a call that fails now still needs the org and the user the inner one last gave, and its
    // agent part gives the user alone (the org is on a turn of its own).
    tokio::time::sleep(Duration::from_millis(100)).await;
    call(&mcp, "second", json!({ "text": "private" }))
        .await
        .unwrap();
    settled(&lsquare).await;
    let failed = of_kind(&ingestion.events(), "tool_call")[1].clone();
    assert!(!whole(&failed), "{failed}");
    assert_eq!(
        seen.errors()[0].message,
        "identify failed; the call was sent as limited diagnostics."
    );
}

/// A handler that passes a call on twice, as middleware that retries does.
#[derive(Clone)]
struct Twice<H> {
    inner: H,
}

impl<H: ServerHandler> ServerHandler for Twice<H> {
    fn get_info(&self) -> ServerConfig {
        info()
    }

    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        context: RequestContext<RoleServer>,
    ) -> Result<CallToolResponse, McpError> {
        let _ = self.inner.call_tool(request.clone(), context.clone()).await;
        self.inner.call_tool(request, context).await
    }
}

#[tokio::test]
async fn a_call_passed_on_twice_is_two_events_with_ids_of_their_own() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    let inner = instrument(
        agent(|_, _| async { done() }),
        &lsquare,
        InstrumentOptions::new(),
    );
    let mcp = connect(instrument(
        Twice { inner },
        &lsquare,
        InstrumentOptions::new(),
    ))
    .await;
    call(&mcp, "ask", json!({})).await.unwrap();
    settled(&lsquare).await;
    let events = ingestion.events();
    valid(&events);
    assert_eq!(events.len(), 2, "{events:?}");
    assert_ne!(events[0]["eventId"], events[1]["eventId"]);
}

#[tokio::test]
async fn a_call_taken_over_on_another_client_takes_none_of_the_outer_ones_capture() {
    let ingestion = Ingestion::accepting().await;
    let (outer, _) = client(&ingestion, |options| options);
    let (inner, _) = client(&ingestion, |options| options);
    let between = Between {
        lsquare: outer.clone(),
        inner: instrument(
            agent(|_, _| async { done() }),
            &inner,
            InstrumentOptions::new(),
        ),
    };
    let mcp = connect(instrument(between, &outer, InstrumentOptions::new())).await;
    call(&mcp, "ask", json!({})).await.unwrap();
    settled(&outer).await;
    settled(&inner).await;
    let events = ingestion.events();
    valid(&events);
    assert_eq!(events.len(), 2, "{events:?}");
    // The inner wrapper's client records the call, without what the outer one's captured.
    let recorded = of_kind(&events, "tool_call")[0];
    assert!(recorded.get("agent").is_none(), "{recorded}");
    // The turn the outer client captured keeps the call's ID, which the event has too.
    let turn = of_kind(&events, "turn")[0];
    assert_eq!(turn["callId"], recorded["callId"]);
}

#[tokio::test]
async fn a_capture_from_identify_is_a_turn_of_its_own() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    let inside = lsquare.clone();
    let options = InstrumentOptions::new().identify(move |_| {
        let lsquare = inside.clone();
        async move {
            assert_eq!(lsquare.current_call_id(), None);
            lsquare.capture(Turn::new().answer("from identify"));
            Ok(Identity::new().user_id("user_8"))
        }
    });
    let mcp = connect(instrument(
        agent(|_, _| async { done() }),
        &lsquare,
        options,
    ))
    .await;
    call(&mcp, "ask", json!({})).await.unwrap();
    settled(&lsquare).await;
    let events = ingestion.events();
    assert_eq!(events.len(), 2);
    assert!(of_kind(&events, "tool_call")[0].get("agent").is_none());
    assert!(of_kind(&events, "turn")[0].get("callId").is_none());
}

#[tokio::test]
async fn a_dry_run_counts_the_tools_of_a_calls_agent_part() {
    let seen = Seen::default();
    let lsquare = Client::new(seen.options().dry_run(true));
    let inside = lsquare.clone();
    let server = agent(move |_, _| {
        let lsquare = inside.clone();
        async move {
            lsquare.capture(whole_turn());
            done()
        }
    });
    let mcp = connect(instrument(server, &lsquare, InstrumentOptions::new())).await;
    call(&mcp, "ask", json!({})).await.unwrap();
    settled(&lsquare).await;
    // After the line that says what the wrapper records.
    let lines = seen.lines();
    assert_eq!(lines.len(), 2, "{lines:?}");
    let summary: Value =
        serde_json::from_str(lines[1].strip_prefix("[lsquare dry-run] ").unwrap()).unwrap();
    assert_eq!(summary["kind"], "tool_call");
    assert_eq!(summary["eventId"], "the call");
    assert_eq!(summary["toolCalls"], 2);
    let fields = summary["fields"].as_array().unwrap();
    for field in ["agent", "turnRef", "orgId", "userId", "conversationId"] {
        assert!(fields.contains(&json!(field)), "{field}: {fields:?}");
    }
}

/// A client on the protocol version that has no sessions, connected to `server` in memory.
async fn modern<H: ServerHandler>(server: H) -> RunningService<RoleClient, common::App> {
    let (server_transport, client_transport) = tokio::io::duplex(64 * 1024);
    tokio::spawn(async move {
        if let Ok(running) = server.serve(server_transport).await {
            let _ = running.waiting().await;
        }
    });
    common::App
        .serve_with_lifecycle(
            client_transport,
            ClientLifecycleMode::Discover {
                preferred_versions: vec![ProtocolVersion::V_2026_07_28],
            },
        )
        .await
        .expect("the MCP client connects")
}

#[tokio::test]
async fn a_captured_conversation_groups_the_call_in_place_of_a_session() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    let inside = lsquare.clone();
    let mut server = agent(move |_, arguments| {
        let lsquare = inside.clone();
        async move {
            match text_of(&arguments, "who").as_str() {
                "in a conversation" => lsquare.capture(Turn::new().conversation_id("conv_42")),
                "without one" => lsquare.capture(Turn::new().conversation_id("")),
                _ => {}
            }
            done()
        }
    });
    server.defined = true;
    let mcp = modern(instrument(server, &lsquare, InstrumentOptions::new())).await;
    let told = |result: &CallToolResult| {
        result
            .content
            .last()
            .and_then(|block| block.as_text())
            .is_some_and(|text| text.text.starts_with("Task ID: "))
    };
    // No capture, or one without a conversation: the call is given a session, told to the agent.
    for who in ["nobody", "without one"] {
        assert!(told(
            &call(&mcp, "ask", json!({ "who": who })).await.unwrap()
        ));
    }
    assert!(!told(
        &call(&mcp, "ask", json!({ "who": "in a conversation" }))
            .await
            .unwrap()
    ));
    // A session the agent sent is the call's all the same.
    let sent = "ses_abcdefghijklmnopqrstuv";
    assert!(!told(
        &call(
            &mcp,
            "ask",
            json!({ "who": "in a conversation", "session_id": sent })
        )
        .await
        .unwrap()
    ));
    settled(&lsquare).await;
    let events = ingestion.events();
    valid(&events);
    let sessions: Vec<Option<&str>> = events
        .iter()
        .map(|event| event.get("sessionSource").and_then(Value::as_str))
        .collect();
    assert_eq!(
        sessions,
        [Some("issued"), Some("issued"), None, Some("sent")]
    );
    assert!(events[2].get("sessionId").is_none());
    assert_eq!(events[2]["conversationId"], "conv_42");
    assert_eq!(events[3]["sessionId"], sent);
}
