//! The inputs the wrapper adds to a server's tools, `session_id` and `intent`, end to end: a real
//! rmcp client calls a real rmcp server, in memory and over HTTP.

mod common;

use std::sync::{Arc, Mutex};
use std::time::Duration;

use common::{arguments, call, client, connect, ingestion_says, until, Ingestion};
use lsquare::mcp::{instrument, IdentifyError, InstrumentOptions};
use rmcp::handler::server::wrapper::Parameters;
use rmcp::model::{
    CallToolRequestParams, CallToolResponse, CallToolResult, ClientConfig, ContentBlock,
    ListToolsResult, PaginatedRequestParams, ProtocolVersion, ServerCapabilities, ServerConfig,
    Tool,
};
use rmcp::service::{RequestContext, RoleClient, RoleServer, RunningService};
use rmcp::transport::streamable_http_server::session::local::LocalSessionManager;
use rmcp::transport::{StreamableHttpServerConfig, StreamableHttpService};
use rmcp::{
    tool, tool_handler, tool_router, ClientLifecycleMode, ClientServiceExt, ErrorData as McpError,
    ServerHandler, ServiceExt,
};
use serde_json::{json, Value};

/// How the two inputs are described to agents: TypeScript's words (a test in `src/extras.rs`
/// compares the SDK's with TypeScript's source).
const SESSION_DESCRIPTION: &str = "The ID of the task this call belongs to. Leave it out on your first call. On every later call for the same task, send the ID an earlier result gave you, and pass it to any subagent working on that task.";
const INTENT_DESCRIPTION: &str = "In one sentence, why you're calling this tool and what the user is trying to do. No personal data or secrets.";
/// A session ID as an agent sends one back.
const SENT: &str = "ses_abcdefghijklmnopqrstuv";

/// What each call brought, as the handler got it: the tool, and its arguments as JSON text, so
/// their order shows.
type Got = Arc<Mutex<Vec<(String, String)>>>;

/// A server whose tools each say something else of the two inputs.
#[derive(Clone, Default)]
struct Desk {
    got: Got,
    /// In place of its own tools, this many, named `t0` onwards, each with this schema.
    many: Option<(usize, Value)>,
    /// `waits` says here that it has started, and goes on once it is released.
    entered: Arc<tokio::sync::Notify>,
    release: Arc<tokio::sync::Notify>,
}

fn plain() -> Value {
    json!({ "type": "object", "properties": { "q": { "type": "string" } }, "required": ["q"] })
}

/// Each tool: its name, the schema it is listed with, and the one `get_tool` gives (none for a
/// tool the handler gives no definition for).
fn catalog() -> Vec<(&'static str, Value, Option<Value>)> {
    let own_session =
        json!({ "type": "object", "properties": { "session_id": { "type": "number" } } });
    let own_intent = json!({ "type": "object", "properties": { "q": { "type": "string" } }, "required": ["intent"] });
    let open = json!({ "type": "object", "additionalProperties": true });
    let either =
        json!({ "type": "object", "anyOf": [{ "required": ["a"] }, { "required": ["b"] }] });
    let with_intent = json!({ "type": "object", "properties": { "intent": { "type": "string" } } });
    vec![
        ("search", plain(), Some(plain())),
        ("own_session", own_session.clone(), Some(own_session)),
        ("own_intent", own_intent.clone(), Some(own_intent)),
        ("open", open.clone(), Some(open)),
        ("either", either.clone(), Some(either)),
        // Listed with an input of its own that its definition doesn't have.
        ("moved", with_intent, Some(plain())),
        ("ghost", plain(), None),
        ("phantom", plain(), None),
        ("fails", plain(), Some(plain())),
        ("refuses", plain(), Some(plain())),
        ("silent", plain(), Some(plain())),
        ("data", plain(), Some(plain())),
        ("waits", plain(), Some(plain())),
    ]
}

impl ServerHandler for Desk {
    fn get_info(&self) -> ServerConfig {
        let mut info = ServerConfig::default();
        info.server_info.name = "desk".into();
        info.server_info.version = "1.0.0".into();
        info.capabilities = ServerCapabilities::builder().enable_tools().build();
        info
    }

    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        _context: RequestContext<RoleServer>,
    ) -> Result<CallToolResponse, McpError> {
        let sent = request.arguments.as_ref().map_or_else(
            || "none".to_owned(),
            |sent| Value::Object(sent.clone()).to_string(),
        );
        self.got
            .lock()
            .unwrap()
            .push((request.name.to_string(), sent));
        match request.name.as_ref() {
            "fails" => Ok(CallToolResult::error(vec![ContentBlock::text("it failed")]).into()),
            "refuses" => Err(McpError::invalid_params("bad arguments", None)),
            "silent" => Ok(CallToolResult::success(Vec::new()).into()),
            "data" => {
                let mut result = CallToolResult::success(Vec::new());
                result.structured_content = Some(json!({ "rows": 2 }));
                Ok(result.into())
            }
            "waits" => {
                self.entered.notify_one();
                self.release.notified().await;
                Ok(CallToolResult::success(vec![ContentBlock::text("done")]).into())
            }
            _ => Ok(CallToolResult::success(vec![ContentBlock::text("done")]).into()),
        }
    }

    async fn list_tools(
        &self,
        _request: Option<PaginatedRequestParams>,
        _context: RequestContext<RoleServer>,
    ) -> Result<ListToolsResult, McpError> {
        if let Some((count, schema)) = &self.many {
            return Ok(ListToolsResult::with_all_items(
                (0..*count)
                    .map(|index| {
                        Tool::new(format!("t{index}"), "A tool.", arguments(schema.clone()))
                    })
                    .collect(),
            ));
        }
        Ok(ListToolsResult::with_all_items(
            catalog()
                .into_iter()
                .map(|(name, listed, _)| Tool::new(name, "A tool.", arguments(listed)))
                .collect(),
        ))
    }

    fn get_tool(&self, name: &str) -> Option<Tool> {
        if let Some((_, schema)) = &self.many {
            return Some(Tool::new(
                name.to_owned(),
                "A tool.",
                arguments(schema.clone()),
            ));
        }
        catalog()
            .into_iter()
            .find(|(tool, _, _)| *tool == name)
            .and_then(|(name, _, defined)| {
                defined.map(|schema| Tool::new(name, "A tool.", arguments(schema)))
            })
    }
}

/// An MCP client on a protocol version that has sessions.
#[derive(Clone, Default)]
struct Legacy;

impl rmcp::ClientHandler for Legacy {
    fn get_info(&self) -> ClientConfig {
        let mut info = ClientConfig::default();
        info.protocol_version = ProtocolVersion::V_2025_06_18;
        info.client_info.name = "legacy-app".into();
        info.client_info.version = "1.0.0".into();
        info
    }
}

/// A client connected to `server` in memory, and the task serving it, which ends once the
/// connection has closed.
async fn serve<H: ServerHandler, C: rmcp::ClientHandler>(
    server: H,
    app: C,
) -> (RunningService<RoleClient, C>, tokio::task::JoinHandle<()>) {
    let (server_transport, client_transport) = tokio::io::duplex(64 * 1024);
    let serving = tokio::spawn(async move {
        if let Ok(running) = server.serve(server_transport).await {
            let _ = running.waiting().await;
        }
    });
    let mcp = app
        .serve(client_transport)
        .await
        .expect("the MCP client connects");
    (mcp, serving)
}

/// A client on the protocol version that has no sessions, connected to `server` in memory: it
/// never sends `initialize`. (`connect` is a client on a version that has them.)
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

/// Waits until everything recorded so far was sent: longer than `flush` waits by default, for a
/// machine that is slow.
async fn settled(lsquare: &lsquare::Client) {
    let flushed = lsquare.flush_within(Duration::from_secs(30)).await;
    assert_eq!(flushed.pending, 0, "{flushed:?}");
}

/// The text of each block of a result.
fn texts(result: &CallToolResult) -> Vec<String> {
    result
        .content
        .iter()
        .map(|block| {
            block
                .as_text()
                .map(|text| text.text.clone())
                .unwrap_or_default()
        })
        .collect()
}

/// The ID a result's last block gives the agent.
fn told(result: &CallToolResult) -> Option<String> {
    let last = texts(result).pop()?;
    let id = last.strip_prefix("Task ID: ")?.strip_suffix(
        ". Send it as session_id on later calls for this task, and pass it to any subagent working on it.",
    )?;
    Some(id.to_owned())
}

fn schema_of(tools: &[Tool], name: &str) -> Value {
    let tool = tools
        .iter()
        .find(|tool| tool.name == name)
        .expect("the tool is listed");
    Value::Object((*tool.input_schema).clone())
}

fn the_two() -> (Value, Value) {
    (
        json!({ "type": "string", "description": SESSION_DESCRIPTION }),
        json!({ "type": "string", "description": INTENT_DESCRIPTION }),
    )
}

#[tokio::test]
async fn listed_tools_get_the_inputs_they_can_take() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, seen) = client(&ingestion, |options| options);
    let server = instrument(Desk::default(), &lsquare, InstrumentOptions::new());
    let direct = server.clone();
    let mcp = connect(server).await;
    let tools = mcp.list_all_tools().await.unwrap();
    let (session_id, intent) = the_two();

    // Both, after the tool's own, optional: `required` is as it was.
    let search = schema_of(&tools, "search");
    assert_eq!(
        search,
        json!({ "type": "object", "properties": { "q": { "type": "string" }, "session_id": session_id, "intent": intent }, "required": ["q"] })
    );
    assert_eq!(
        search["properties"]
            .as_object()
            .unwrap()
            .keys()
            .collect::<Vec<_>>(),
        ["q", "session_id", "intent"]
    );
    // A tool with one of its own keeps it, and gets the other.
    assert_eq!(
        schema_of(&tools, "own_session"),
        json!({ "type": "object", "properties": { "session_id": { "type": "number" }, "intent": intent } })
    );
    assert_eq!(
        schema_of(&tools, "own_intent"),
        json!({ "type": "object", "properties": { "q": { "type": "string" }, "session_id": session_id }, "required": ["intent"] })
    );
    // Listed as they came: a schema that could take any input, one the definition disagrees
    // with, and the tools the handler gives no definition for.
    for (name, listed, _) in catalog() {
        if ["open", "either", "moved", "ghost", "phantom"].contains(&name) {
            assert_eq!(schema_of(&tools, name), listed, "{name}");
        }
    }
    // Everything else of the page is the handler's: the same tools in the same order, each as
    // the handler lists it once the inputs that were added are taken away again.
    let bare = connect(Desk::default()).await;
    let theirs = serde_json::to_value(bare.list_tools(None).await.unwrap()).unwrap();
    let mut shown = serde_json::to_value(mcp.list_tools(None).await.unwrap()).unwrap();
    let mut added = 0;
    for (tool, original) in shown["tools"]
        .as_array_mut()
        .unwrap()
        .iter_mut()
        .zip(theirs["tools"].as_array().unwrap())
    {
        let Some(properties) = tool["inputSchema"]
            .get_mut("properties")
            .and_then(Value::as_object_mut)
        else {
            continue;
        };
        for (name, input) in [("session_id", &session_id), ("intent", &intent)] {
            let own = original["inputSchema"]["properties"].get(name).is_some();
            if !own && properties.get(name) == Some(input) {
                properties.shift_remove(name);
                added += 1;
            }
        }
    }
    assert_eq!(shown, theirs);
    assert_eq!(shown["tools"].as_array().unwrap().len(), catalog().len());
    // Two on each of the six plain tools, one on each of the two with an input of their own.
    assert_eq!(added, 14);

    // `get_tool` gives the handler's own definition.
    assert_eq!(
        Value::Object((*direct.get_tool("search").unwrap().input_schema).clone()),
        plain()
    );

    // Each tool left alone is said once, however often it is listed.
    mcp.list_all_tools().await.unwrap();
    until(|| seen.lines().len() >= 6).await;
    settled(&lsquare).await;
    let mut lines = seen.lines();
    lines.sort();
    assert_eq!(
        lines,
        [
            r#"[lsquare] Tool "either" gets no session_id or intent: its input schema can't be read safely (it isn't just an object of named properties), so any input could be its own."#,
            r#"[lsquare] Tool "ghost" gets no session_id or intent: the handler's get_tool gives no definition for it. rmcp's #[tool_handler] writes get_tool; a handler written by hand needs its own."#,
            r#"[lsquare] Tool "open" gets no session_id or intent: its input schema can't be read safely (it isn't just an object of named properties), so any input could be its own."#,
            r#"[lsquare] Tool "own_intent" has its own intent input: it is left as it is."#,
            r#"[lsquare] Tool "own_session" has its own session_id input: it is left as it is."#,
            r#"[lsquare] Tool "phantom" gets no session_id or intent: the handler's get_tool gives no definition for it. rmcp's #[tool_handler] writes get_tool; a handler written by hand needs its own."#,
        ]
    );
}

#[tokio::test]
async fn the_inputs_are_taken_out_before_the_handler_sees_the_call() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    let desk = Desk::default();
    let mcp = connect(instrument(desk.clone(), &lsquare, InstrumentOptions::new())).await;

    let result = call(
        &mcp,
        "search",
        json!({ "session_id": SENT, "q": "rust", "intent": "Find the docs the user asked for.", "page": 2, "sort": "new" }),
    )
    .await
    .unwrap();
    // The reply is the tool's own: an agent that sent the task's ID isn't told one.
    assert_eq!(texts(&result), ["done"]);
    // The other arguments, in the order they came.
    assert_eq!(
        *desk.got.lock().unwrap(),
        [(
            "search".to_owned(),
            r#"{"q":"rust","page":2,"sort":"new"}"#.to_owned()
        )]
    );
    settled(&lsquare).await;
    assert_eq!(ingestion.events().len(), 1);
    let event = &ingestion.events()[0];
    assert_eq!(
        event["input"].to_string(),
        r#"{"q":"rust","page":2,"sort":"new"}"#
    );
    assert_eq!(event["intent"], "Find the docs the user asked for.");
    assert_eq!(event["sessionId"], SENT);
    assert_eq!(event["sessionSource"], "sent");
    if let Some(answers) = ingestion_says(&ingestion.events()) {
        assert_eq!(answers, ["valid"]);
    }
}

#[tokio::test]
async fn a_call_without_a_session_is_given_one() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    let desk = Desk::default();
    let mcp = modern(instrument(desk.clone(), &lsquare, InstrumentOptions::new())).await;

    let first = call(&mcp, "search", json!({ "q": "a" })).await.unwrap();
    assert_eq!(texts(&first).len(), 2);
    assert_eq!(texts(&first)[0], "done");
    let id = told(&first).expect("the result ends with the task's ID");
    assert!(id.starts_with("ses_") && id.len() == 26, "{id}");
    // Sent back, it is the session, and no other is given.
    let second = call(&mcp, "search", json!({ "q": "b", "session_id": id }))
        .await
        .unwrap();
    assert_eq!(texts(&second), ["done"]);
    // One that isn't an ID the wrapper gives is taken out all the same, and a new one given.
    for other in [
        json!(7),
        json!("mine"),
        json!(null),
        json!(format!("{SENT}x")),
    ] {
        let again = call(&mcp, "search", json!({ "q": "c", "session_id": other }))
            .await
            .unwrap();
        let another = told(&again).expect("a new ID");
        assert_ne!(another, id);
    }
    assert!(desk
        .got
        .lock()
        .unwrap()
        .iter()
        .all(|(_, sent)| !sent.contains("session_id")));

    settled(&lsquare).await;
    assert_eq!(ingestion.events().len(), 6);
    let events = ingestion.events();
    assert_eq!(events[0]["sessionId"], id.as_str());
    assert_eq!(events[0]["sessionSource"], "issued");
    // The result recorded is the tool's own, without the block that tells the ID.
    assert_eq!(events[0]["result"]["content"].as_array().unwrap().len(), 1);
    assert!(!events[0]["result"].to_string().contains("Task ID"));
    assert_eq!(events[1]["sessionId"], id.as_str());
    assert_eq!(events[1]["sessionSource"], "sent");
    for event in &events[2..] {
        assert_eq!(event["sessionSource"], "issued");
        assert_ne!(event["sessionId"], id.as_str());
    }
    if let Some(answers) = ingestion_says(&events) {
        assert!(
            answers.iter().all(|answer| answer == "valid"),
            "{answers:?}"
        );
    }
}

#[tokio::test]
async fn a_tool_keeps_an_input_of_its_own() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    let desk = Desk::default();
    let mcp = modern(instrument(desk.clone(), &lsquare, InstrumentOptions::new())).await;

    // Its own `session_id` reaches it, and is no session: none is given either.
    let result = call(
        &mcp,
        "own_session",
        json!({ "session_id": 12, "intent": "To count." }),
    )
    .await
    .unwrap();
    assert_eq!(texts(&result), ["done"]);
    // Its own `intent` reaches it, and isn't recorded as the agent's.
    let result = call(
        &mcp,
        "own_intent",
        json!({ "intent": 3, "session_id": SENT }),
    )
    .await
    .unwrap();
    assert_eq!(texts(&result), ["done"]);
    assert_eq!(
        *desk.got.lock().unwrap(),
        [
            ("own_session".to_owned(), r#"{"session_id":12}"#.to_owned()),
            ("own_intent".to_owned(), r#"{"intent":3}"#.to_owned()),
        ]
    );
    settled(&lsquare).await;
    assert_eq!(ingestion.events().len(), 2);
    let events = ingestion.events();
    assert_eq!(events[0]["intent"], "To count.");
    assert!(events[0].get("sessionId").is_none() && events[0].get("sessionSource").is_none());
    assert_eq!(events[0]["input"], json!({ "session_id": 12 }));
    assert!(events[1].get("intent").is_none());
    assert_eq!(events[1]["sessionId"], SENT);
    assert_eq!(events[1]["sessionSource"], "sent");
    assert_eq!(events[1]["input"], json!({ "intent": 3 }));
}

#[tokio::test]
async fn a_tool_whose_inputs_cannot_be_told_is_called_as_it_is() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    let desk = Desk::default();
    let mcp = modern(instrument(desk.clone(), &lsquare, InstrumentOptions::new())).await;
    let sent = json!({ "session_id": SENT, "q": "x", "intent": "Why." });

    for tool in ["open", "either", "ghost"] {
        let result = call(&mcp, tool, sent.clone()).await.unwrap();
        assert_eq!(texts(&result), ["done"], "{tool}");
    }
    // A tool listed otherwise than it is defined is called by its definition.
    let result = call(&mcp, "moved", sent.clone()).await.unwrap();
    assert_eq!(texts(&result), ["done"]);
    let got = desk.got.lock().unwrap().clone();
    for (tool, arguments) in &got[..3] {
        assert_eq!(arguments, &sent.to_string(), "{tool}");
    }
    assert_eq!(got[3], ("moved".to_owned(), r#"{"q":"x"}"#.to_owned()));

    settled(&lsquare).await;
    assert_eq!(ingestion.events().len(), 4);
    let events = ingestion.events();
    for event in &events[..3] {
        assert!(event.get("sessionId").is_none(), "{event}");
        assert!(event.get("intent").is_none(), "{event}");
    }
    assert_eq!(events[3]["sessionId"], SENT);
    assert_eq!(events[3]["intent"], "Why.");
}

#[tokio::test]
async fn a_call_that_fails_is_given_no_session() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    let desk = Desk::default();
    let mcp = modern(instrument(desk.clone(), &lsquare, InstrumentOptions::new())).await;

    let failed = call(
        &mcp,
        "fails",
        json!({ "q": "x", "intent": "To see it fail." }),
    )
    .await
    .unwrap();
    assert_eq!(failed.is_error, Some(true));
    assert_eq!(texts(&failed), ["it failed"]);
    assert!(call(&mcp, "refuses", json!({ "q": "x" })).await.is_err());
    // One it was sent stays its session.
    let failed = call(&mcp, "fails", json!({ "q": "x", "session_id": SENT }))
        .await
        .unwrap();
    assert_eq!(texts(&failed), ["it failed"]);
    assert!(
        call(&mcp, "refuses", json!({ "q": "x", "session_id": SENT }))
            .await
            .is_err()
    );
    assert!(desk
        .got
        .lock()
        .unwrap()
        .iter()
        .all(|(_, sent)| sent == r#"{"q":"x"}"#));

    settled(&lsquare).await;
    assert_eq!(ingestion.events().len(), 4);
    let events = ingestion.events();
    assert_eq!(events[0]["status"], "error");
    assert_eq!(events[0]["intent"], "To see it fail.");
    for event in &events[..2] {
        assert!(event.get("sessionId").is_none() && event.get("sessionSource").is_none());
    }
    for event in &events[2..] {
        assert_eq!(event["sessionId"], SENT);
        assert_eq!(event["sessionSource"], "sent");
    }
}

#[tokio::test]
async fn a_connection_with_a_protocol_session_is_grouped_by_it() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    let desk = Desk::default();
    let server = instrument(desk.clone(), &lsquare, InstrumentOptions::new());
    let (mcp, _) = serve(server.clone(), Legacy).await;
    let (other, _) = serve(server.clone(), Legacy).await;

    // No ID is told: the connection is the session. Even a tool left alone is grouped by it.
    for tool in ["search", "open", "fails"] {
        let result = call(&mcp, tool, json!({ "q": "x" })).await.unwrap();
        assert_eq!(texts(&result).len(), 1, "{tool}");
    }
    // An ID the agent sends still says which task it is.
    call(&mcp, "search", json!({ "q": "x", "session_id": SENT }))
        .await
        .unwrap();
    // Another connection, served by a copy of the wrapper, is another session.
    call(&other, "search", json!({ "q": "x" })).await.unwrap();

    settled(&lsquare).await;
    assert_eq!(ingestion.events().len(), 5);
    let events = ingestion.events();
    let session = events[0]["sessionId"].as_str().unwrap().to_owned();
    assert_eq!(session.len(), 64);
    assert!(session.bytes().all(|byte| byte.is_ascii_hexdigit()));
    assert_eq!(events[0]["protocolVersion"], "2025-06-18");
    for event in &events[..3] {
        assert_eq!(event["sessionId"], session.as_str());
        assert_eq!(event["sessionSource"], "protocol");
    }
    assert_eq!(events[3]["sessionId"], SENT);
    assert_eq!(events[3]["sessionSource"], "sent");
    assert_eq!(events[4]["sessionSource"], "protocol");
    assert_ne!(events[4]["sessionId"], session.as_str());
    if let Some(answers) = ingestion_says(&events) {
        assert!(
            answers.iter().all(|answer| answer == "valid"),
            "{answers:?}"
        );
    }
}

#[tokio::test]
async fn one_wrapper_serving_two_connections_tells_them_apart() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    let shared = Arc::new(instrument(
        Desk::default(),
        &lsquare,
        InstrumentOptions::new(),
    ));

    let (first, _) = serve(shared.clone(), Legacy).await;
    let (second, _) = serve(shared.clone(), Legacy).await;
    // Each connection is its own session, whichever wrapper serves it: no ID is told.
    for mcp in [&first, &second, &first] {
        let result = call(mcp, "search", json!({ "q": "x" })).await.unwrap();
        assert_eq!(texts(&result), ["done"]);
    }

    settled(&lsquare).await;
    let events = ingestion.events();
    assert_eq!(events.len(), 3);
    for event in &events {
        assert_eq!(event["sessionSource"], "protocol");
    }
    assert_ne!(events[0]["sessionId"], events[1]["sessionId"]);
    assert_eq!(events[0]["sessionId"], events[2]["sessionId"]);
}

#[tokio::test]
async fn a_request_on_a_version_without_sessions_has_no_protocol_session() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    let (mcp, _) = serve(
        instrument(Desk::default(), &lsquare, InstrumentOptions::new()),
        Legacy,
    )
    .await;
    // The connection has a session, and this request says it is on a version that has none.
    let mut meta = rmcp::model::RequestMetaObject::default();
    meta.set_protocol_version(ProtocolVersion::V_2026_07_28);
    meta.set_client_capabilities(rmcp::model::ClientCapabilities::default());
    let mut request =
        CallToolRequestParams::new("search").with_arguments(arguments(json!({ "q": "x" })));
    request.meta = Some(meta);
    let result = mcp.call_tool(request).await.unwrap();
    assert!(told(&result).is_some());
    settled(&lsquare).await;
    let events = ingestion.events();
    assert_eq!(events.len(), 1);
    assert_eq!(events[0]["protocolVersion"], "2026-07-28");
    assert_eq!(events[0]["sessionSource"], "issued");
}

#[tokio::test]
async fn a_wrapper_keeps_an_id_for_a_thousand_open_connections() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    let shared = Arc::new(instrument(
        Desk::default(),
        &lsquare,
        InstrumentOptions::new(),
    ));
    let mut open = Vec::new();
    for _ in 0..1_000 {
        open.push(serve(shared.clone(), Legacy).await);
    }
    // Each of them is its own session; one more has none of its own, and is given IDs.
    for (mcp, _) in &open {
        let result = call(mcp, "search", json!({ "q": "x" })).await.unwrap();
        assert_eq!(texts(&result), ["done"]);
    }
    let (extra, _) = serve(shared.clone(), Legacy).await;
    let result = call(&extra, "search", json!({ "q": "x" })).await.unwrap();
    assert!(told(&result).is_some());
    // Once they have closed, the wrapper has room again.
    for (mcp, serving) in open {
        mcp.cancel().await.unwrap();
        serving.await.unwrap();
    }
    let (next, _) = serve(shared.clone(), Legacy).await;
    let result = call(&next, "search", json!({ "q": "x" })).await.unwrap();
    assert_eq!(texts(&result), ["done"]);

    settled(&lsquare).await;
    let events = ingestion.events();
    assert_eq!(events.len(), 1_002);
    let mut sessions: Vec<&str> = events[..1_000]
        .iter()
        .map(|event| {
            assert_eq!(event["sessionSource"], "protocol");
            event["sessionId"].as_str().unwrap()
        })
        .collect();
    sessions.sort_unstable();
    sessions.dedup();
    assert_eq!(sessions.len(), 1_000);
    assert_eq!(events[1_000]["sessionSource"], "issued");
    assert_eq!(events[1_001]["sessionSource"], "protocol");
    assert!(!sessions.contains(&events[1_001]["sessionId"].as_str().unwrap()));
}

#[tokio::test]
async fn a_connection_that_sends_initialize_again_is_the_session_it_was() {
    use tokio::io::{AsyncBufReadExt, AsyncWriteExt};
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    let shared = Arc::new(instrument(
        Desk::default(),
        &lsquare,
        InstrumentOptions::new(),
    ));
    // A connection written by hand, one JSON-RPC message to a line.
    let (server_transport, client_transport) = tokio::io::duplex(64 * 1024);
    let server = shared.clone();
    tokio::spawn(async move {
        if let Ok(running) = server.serve(server_transport).await {
            let _ = running.waiting().await;
        }
    });
    let (reading, mut writing) = tokio::io::split(client_transport);
    let mut reading = tokio::io::BufReader::new(reading).lines();
    let mut ask = async |message: Value, answered: bool| -> Value {
        writing
            .write_all(format!("{message}\n").as_bytes())
            .await
            .unwrap();
        if !answered {
            return Value::Null;
        }
        serde_json::from_str(&reading.next_line().await.unwrap().unwrap()).unwrap()
    };
    let search = |id: usize| json!({ "jsonrpc": "2.0", "id": id, "method": "tools/call", "params": { "name": "search", "arguments": { "q": "x" } } });
    let hello = |id: usize| {
        let mut message = initialize();
        message["id"] = id.into();
        message
    };
    assert_eq!(
        ask(hello(1), true).await["result"]["protocolVersion"],
        "2025-06-18"
    );
    ask(
        json!({ "jsonrpc": "2.0", "method": "notifications/initialized" }),
        false,
    )
    .await;
    assert_eq!(said(&ask(search(2), true).await), ["done"]);
    // `initialize` a thousand times more: the connection stays the one session, and takes no
    // other connection's place.
    for id in 3..1_003 {
        assert!(ask(hello(id), true).await.get("result").is_some());
    }
    assert_eq!(said(&ask(search(1_003), true).await), ["done"]);
    let (other, _) = serve(shared.clone(), Legacy).await;
    let result = call(&other, "search", json!({ "q": "x" })).await.unwrap();
    assert_eq!(texts(&result), ["done"]);

    settled(&lsquare).await;
    let events = ingestion.events();
    assert_eq!(events.len(), 3);
    for event in &events {
        assert_eq!(event["sessionSource"], "protocol", "{event}");
    }
    assert_eq!(events[0]["sessionId"], events[1]["sessionId"]);
    assert_ne!(events[0]["sessionId"], events[2]["sessionId"]);
}

#[tokio::test]
async fn a_client_on_a_protocol_without_sessions_is_never_given_anothers() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    let shared = Arc::new(instrument(
        Desk::default(),
        &lsquare,
        InstrumentOptions::new(),
    ));

    // One connection has a session; a client that never sends `initialize` calls the same wrapper.
    let (first, _) = serve(shared.clone(), Legacy).await;
    call(&first, "search", json!({ "q": "x" })).await.unwrap();
    let other = modern(shared.clone()).await;
    let result = call(&other, "search", json!({ "q": "x" })).await.unwrap();
    assert!(told(&result).is_some());

    settled(&lsquare).await;
    let events = ingestion.events();
    assert_eq!(events.len(), 2);
    assert_eq!(events[0]["sessionSource"], "protocol");
    assert_eq!(events[1]["protocolVersion"], "2026-07-28");
    assert_eq!(events[1]["sessionSource"], "issued");
}

#[tokio::test]
async fn a_call_whose_event_is_dropped_is_still_given_its_session() {
    let ingestion = Ingestion::accepting().await;
    // No room for any event.
    let (lsquare, seen) = client(&ingestion, |options| options.max_queue_bytes(1));
    let mcp = modern(instrument(
        Desk::default(),
        &lsquare,
        InstrumentOptions::new(),
    ))
    .await;
    let result = call(&mcp, "search", json!({ "q": "x" })).await.unwrap();
    // The calls that follow can still be told to belong together.
    assert!(told(&result).is_some());
    until(|| seen.codes().contains(&"queue_full".to_owned())).await;
    settled(&lsquare).await;
    assert!(ingestion.events().is_empty());
}

#[tokio::test]
async fn a_long_listing_is_read_within_bounds() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, seen) = client(&ingestion, |options| options);
    let listing = |count: usize, schema: Value| {
        let desk = Desk {
            many: Some((count, schema)),
            ..Desk::default()
        };
        let server = instrument(desk, &lsquare, InstrumentOptions::new());
        async move { connect(server).await.list_all_tools().await.unwrap() }
    };
    let (session_id, intent) = the_two();
    let with_both = json!({ "type": "object", "properties": { "q": { "type": "string" }, "session_id": session_id, "intent": intent }, "required": ["q"] });
    let all = |tools: &[Tool], schema: &Value| {
        tools.iter().enumerate().all(|(index, tool)| {
            tool.name == format!("t{index}") && schema_of(tools, &tool.name) == *schema
        })
    };

    // 10,000 tools are each given the inputs; a page of more is listed as it came.
    let tools = listing(10_000, plain()).await;
    assert!(tools.len() == 10_000 && all(&tools, &with_both));
    let tools = listing(10_001, plain()).await;
    assert!(tools.len() == 10_001 && all(&tools, &plain()));
    assert!(seen.lines().is_empty());
    // A client warns of the first 1,000 tools it leaves alone, no more. One call at a time, so
    // each warning is written before the next: no more than 100 wait at once.
    let open = json!({ "type": "object", "additionalProperties": true });
    let desk = Desk {
        many: Some((1_100, open)),
        ..Desk::default()
    };
    let mcp = connect(instrument(desk, &lsquare, InstrumentOptions::new())).await;
    for index in 0..1_100usize {
        let name = format!("t{index}");
        mcp.call_tool(CallToolRequestParams::new(name))
            .await
            .unwrap();
        if index % 50 == 49 {
            until(|| seen.lines().len() >= (index + 1).min(1_000)).await;
        }
    }
    settled(&lsquare).await;
    let lines = seen.lines();
    assert_eq!(lines.len(), 1_000);
    assert!(lines[0].starts_with(r#"[lsquare] Tool "t0" gets no session_id or intent"#));
    assert!(lines[999].starts_with(r#"[lsquare] Tool "t999" gets no session_id or intent"#));
    // Any other warning is still given.
    let mcp = connect(instrument(
        Desk::default(),
        &lsquare,
        InstrumentOptions::new().inputs_for(|_| panic!("the inputs function panicked")),
    ))
    .await;
    call(&mcp, "search", json!({ "q": "x" })).await.unwrap();
    until(|| seen.lines().len() > 1_000).await;
    settled(&lsquare).await;
    let lines = seen.lines();
    assert_eq!(lines.len(), 1_001);
    assert!(lines[1_000].starts_with("[lsquare] The inputs function panicked"));
}

#[tokio::test]
async fn a_wrapper_serves_the_next_connection_once_the_last_has_closed() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    let shared = Arc::new(instrument(
        Desk::default(),
        &lsquare,
        InstrumentOptions::new(),
    ));

    let (first, serving) = serve(shared.clone(), Legacy).await;
    call(&first, "search", json!({ "q": "x" })).await.unwrap();
    first.cancel().await.unwrap();
    serving.await.unwrap();
    let (second, _) = serve(shared.clone(), Legacy).await;
    let result = call(&second, "search", json!({ "q": "x" })).await.unwrap();
    assert_eq!(texts(&result), ["done"]);

    settled(&lsquare).await;
    assert_eq!(ingestion.events().len(), 2);
    let events = ingestion.events();
    assert_eq!(events[0]["sessionSource"], "protocol");
    assert_eq!(events[1]["sessionSource"], "protocol");
    assert_ne!(events[0]["sessionId"], events[1]["sessionId"]);
}

#[tokio::test]
async fn the_options_turn_each_input_off() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    let sent = json!({ "q": "x", "session_id": SENT, "intent": "Why." });
    let (session_id, intent) = the_two();

    // Neither: the server is listed and called as it is.
    let desk = Desk::default();
    let mcp = modern(instrument(
        desk.clone(),
        &lsquare,
        InstrumentOptions::new().extras(false),
    ))
    .await;
    assert_eq!(
        schema_of(&mcp.list_all_tools().await.unwrap(), "search"),
        plain()
    );
    let result = call(&mcp, "search", sent.clone()).await.unwrap();
    assert_eq!(texts(&result), ["done"]);
    assert_eq!(desk.got.lock().unwrap()[0].1, sent.to_string());

    // Only `intent`.
    let desk = Desk::default();
    let mcp = modern(instrument(
        desk.clone(),
        &lsquare,
        InstrumentOptions::new().sessions(false),
    ))
    .await;
    assert_eq!(
        schema_of(&mcp.list_all_tools().await.unwrap(), "search")["properties"],
        json!({ "q": { "type": "string" }, "intent": intent })
    );
    let result = call(&mcp, "search", sent.clone()).await.unwrap();
    assert_eq!(texts(&result), ["done"]);
    assert_eq!(
        desk.got.lock().unwrap()[0].1,
        json!({ "q": "x", "session_id": SENT }).to_string()
    );

    // Only `session_id`.
    let desk = Desk::default();
    let mcp = modern(instrument(
        desk.clone(),
        &lsquare,
        InstrumentOptions::new().intent(false),
    ))
    .await;
    assert_eq!(
        schema_of(&mcp.list_all_tools().await.unwrap(), "search")["properties"],
        json!({ "q": { "type": "string" }, "session_id": session_id })
    );
    let result = call(&mcp, "search", json!({ "q": "x", "intent": "Why." }))
        .await
        .unwrap();
    assert!(told(&result).is_some());
    assert_eq!(
        desk.got.lock().unwrap()[0].1,
        json!({ "q": "x", "intent": "Why." }).to_string()
    );

    settled(&lsquare).await;
    assert_eq!(ingestion.events().len(), 3);
    let events = ingestion.events();
    assert!(events[0].get("sessionId").is_none() && events[0].get("intent").is_none());
    assert_eq!(events[0]["input"], sent);
    assert!(events[1].get("sessionId").is_none());
    assert_eq!(events[1]["intent"], "Why.");
    assert_eq!(events[2]["sessionSource"], "issued");
    assert!(events[2].get("intent").is_none());
}

#[tokio::test]
async fn a_client_without_a_key_changes_nothing() {
    let lsquare = lsquare::Client::new(lsquare::Options::new().dry_run(false));
    let desk = Desk::default();
    let (mcp, _) = serve(
        instrument(desk.clone(), &lsquare, InstrumentOptions::new()),
        Legacy,
    )
    .await;
    assert_eq!(
        schema_of(&mcp.list_all_tools().await.unwrap(), "search"),
        plain()
    );
    let sent = json!({ "q": "x", "session_id": SENT, "intent": "Why." });
    let result = call(&mcp, "search", sent.clone()).await.unwrap();
    assert_eq!(texts(&result), ["done"]);
    assert_eq!(desk.got.lock().unwrap()[0].1, sent.to_string());
}

#[tokio::test]
async fn a_client_that_was_shut_down_still_takes_the_inputs_out() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    let desk = Desk::default();
    // On a connection with no session of its own, where a recorded call would be given an ID.
    let mcp = modern(instrument(desk.clone(), &lsquare, InstrumentOptions::new())).await;
    let listed = mcp.list_all_tools().await.unwrap();
    lsquare.shutdown().await;

    // Agents were listed the inputs, and go on sending them: the tool still never sees them.
    let result = call(
        &mcp,
        "search",
        json!({ "q": "x", "intent": "Why.", "session_id": 4 }),
    )
    .await
    .unwrap();
    // Nothing records the call any more, so no session is started for it.
    assert_eq!(texts(&result), ["done"]);
    assert_eq!(desk.got.lock().unwrap()[0].1, r#"{"q":"x"}"#);
    // And the listing stays what it was.
    assert_eq!(
        schema_of(&mcp.list_all_tools().await.unwrap(), "search"),
        schema_of(&listed, "search")
    );
    assert!(ingestion.events().is_empty());
}

#[tokio::test]
async fn a_client_shut_down_while_a_tool_runs_starts_no_session() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    let desk = Desk::default();
    let mcp = modern(instrument(desk.clone(), &lsquare, InstrumentOptions::new())).await;
    let (result, ()) = tokio::join!(call(&mcp, "waits", json!({ "q": "x" })), async {
        desk.entered.notified().await;
        lsquare.shutdown().await;
        desk.release.notify_one();
    });
    // The call was taken to be recorded, and nothing records it now: no ID is told for it.
    assert_eq!(texts(&result.unwrap()), ["done"]);
    assert!(ingestion.events().is_empty());
}

#[tokio::test]
async fn a_result_with_nothing_in_it_is_left_empty() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    let mcp = modern(instrument(
        Desk::default(),
        &lsquare,
        InstrumentOptions::new(),
    ))
    .await;

    // A client that reads a result's first block as the tool's value still reads none.
    let silent = call(&mcp, "silent", json!({ "q": "x" })).await.unwrap();
    assert!(silent.content.is_empty());
    silent.into_typed::<()>().unwrap();
    // With structured content, that is the value: the ID is told beside it.
    let data = call(&mcp, "data", json!({ "q": "x" })).await.unwrap();
    assert_eq!(texts(&data).len(), 1);
    let id = told(&data).expect("the result ends with the task's ID");
    assert_eq!(data.into_typed::<Value>().unwrap(), json!({ "rows": 2 }));

    settled(&lsquare).await;
    let events = ingestion.events();
    assert_eq!(events.len(), 2);
    assert!(events[0].get("sessionId").is_none() && events[0].get("sessionSource").is_none());
    assert_eq!(events[1]["sessionId"], id.as_str());
    assert_eq!(events[1]["sessionSource"], "issued");
}

/// A server with a tool of its own, which passes every other call on to an instrumented server.
struct Front {
    desk: lsquare::mcp::Instrumented<Desk>,
    got: Got,
}

impl ServerHandler for Front {
    fn get_info(&self) -> ServerConfig {
        let mut info = self.desk.get_info();
        info.server_info.name = "front".into();
        info
    }

    /// The server it passes calls on to hears of each connection too.
    async fn initialize(
        &self,
        request: rmcp::model::InitializeRequestParams,
        context: RequestContext<RoleServer>,
    ) -> Result<rmcp::model::InitializeResult, McpError> {
        self.desk.initialize(request, context).await
    }

    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        context: RequestContext<RoleServer>,
    ) -> Result<CallToolResponse, McpError> {
        if request.name != "local" {
            return self.desk.call_tool(request, context).await;
        }
        let sent = Value::Object(request.arguments.unwrap_or_default()).to_string();
        self.got.lock().unwrap().push(("local".to_owned(), sent));
        Ok(CallToolResult::success(vec![ContentBlock::text("here")]).into())
    }

    async fn list_tools(
        &self,
        request: Option<PaginatedRequestParams>,
        context: RequestContext<RoleServer>,
    ) -> Result<ListToolsResult, McpError> {
        let mut page = self.desk.list_tools(request, context).await?;
        page.tools
            .push(Tool::new("local", "A tool.", arguments(plain())));
        Ok(page)
    }

    fn get_tool(&self, name: &str) -> Option<Tool> {
        // The instrumented server first, then its own.
        self.desk.get_tool(name).or_else(|| {
            (name == "local").then(|| Tool::new("local", "A tool.", arguments(plain())))
        })
    }
}

#[tokio::test]
async fn a_server_that_passes_calls_on_to_an_instrumented_one_adds_them_to_its_own_tools() {
    let (inner_ingestion, outer_ingestion) =
        (Ingestion::accepting().await, Ingestion::accepting().await);
    let (inner, _) = client(&inner_ingestion, |options| options);
    let (outer, _) = client(&outer_ingestion, |options| options);
    let desk = Desk::default();
    let got = Got::default();
    let front = Front {
        desk: instrument(desk.clone(), &inner, InstrumentOptions::new()),
        got: got.clone(),
    };
    let mcp = modern(instrument(front, &outer, InstrumentOptions::new())).await;
    let once = modern(instrument(
        Desk::default(),
        &inner,
        InstrumentOptions::new(),
    ))
    .await;

    // Its own tool gets the inputs from the outer wrapper; the others have theirs from the inner.
    let (tools, alone) = (
        mcp.list_all_tools().await.unwrap(),
        once.list_all_tools().await.unwrap(),
    );
    assert_eq!(schema_of(&tools, "local"), schema_of(&alone, "search"));
    for (name, _, _) in catalog() {
        assert_eq!(schema_of(&tools, name), schema_of(&alone, name), "{name}");
    }
    let sent = json!({ "q": "x", "intent": "Why." });
    for (tool, said) in [("local", "here"), ("search", "done")] {
        let result = call(&mcp, tool, sent.clone()).await.unwrap();
        // One ID, told once, whichever wrapper has the tool.
        assert_eq!(texts(&result).len(), 2, "{tool}");
        assert_eq!(texts(&result)[0], said);
        assert!(told(&result).is_some());
    }
    assert_eq!(got.lock().unwrap()[0].1, r#"{"q":"x"}"#);
    assert_eq!(desk.got.lock().unwrap()[0].1, r#"{"q":"x"}"#);
    // Each call is recorded once, by the wrapper nearest its tool.
    settled(&outer).await;
    settled(&inner).await;
    let (outside, inside) = (outer_ingestion.events(), inner_ingestion.events());
    assert_eq!((outside.len(), inside.len()), (1, 1));
    for (event, tool) in [(&outside[0], "local"), (&inside[0], "search")] {
        assert_eq!(event["toolName"], tool);
        assert_eq!(event["intent"], "Why.");
        assert_eq!(event["sessionSource"], "issued");
    }

    // On a connection that is a session, it is one session whichever wrapper records a call:
    // the inner one, which has an ID of its own for the connection, takes the outer one's.
    let front = Front {
        desk: instrument(Desk::default(), &inner, InstrumentOptions::new()),
        got: Got::default(),
    };
    let (mcp, _) = serve(instrument(front, &outer, InstrumentOptions::new()), Legacy).await;
    for tool in ["local", "search"] {
        let result = call(&mcp, tool, json!({ "q": "x" })).await.unwrap();
        assert_eq!(texts(&result).len(), 1, "{tool}");
    }
    settled(&outer).await;
    settled(&inner).await;
    let (outside, inside) = (outer_ingestion.events(), inner_ingestion.events());
    assert_eq!((outside.len(), inside.len()), (2, 2));
    assert_eq!(outside[1]["sessionSource"], "protocol");
    assert_eq!(inside[1]["sessionSource"], "protocol");
    assert_eq!(outside[1]["sessionId"], inside[1]["sessionId"]);
}

#[tokio::test]
async fn a_connection_that_never_sent_initialize_is_never_given_anothers_session() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    let shared = Arc::new(instrument(
        Desk::default(),
        &lsquare,
        InstrumentOptions::new(),
    ));
    // One connection is a session.
    let (first, _) = serve(shared.clone(), Legacy).await;
    call(&first, "search", json!({ "q": "x" })).await.unwrap();
    // Another is served without the handshake, as a server that keeps no sessions serves each
    // request, and its client says it is on the same protocol version.
    let (server_transport, client_transport) = tokio::io::duplex(64 * 1024);
    let said = rmcp::ClientHandler::get_info(&Legacy);
    let _serving = rmcp::service::serve_directly::<RoleServer, _, _, _, _>(
        shared.clone(),
        server_transport,
        Some(said),
    );
    let other =
        rmcp::service::serve_directly::<RoleClient, _, _, _, _>(Legacy, client_transport, None);
    let result = call(&other, "search", json!({ "q": "x" })).await.unwrap();
    assert!(told(&result).is_some());
    // And the first is still the session it was.
    let result = call(&first, "search", json!({ "q": "x" })).await.unwrap();
    assert_eq!(texts(&result), ["done"]);

    settled(&lsquare).await;
    let events = ingestion.events();
    assert_eq!(events.len(), 3);
    assert_eq!(events[0]["sessionSource"], "protocol");
    assert_eq!(events[1]["protocolVersion"], "2025-06-18");
    assert_eq!(events[1]["sessionSource"], "issued");
    assert_eq!(events[2]["sessionSource"], "protocol");
    assert_eq!(events[2]["sessionId"], events[0]["sessionId"]);
}

#[tokio::test]
async fn wrapped_twice_the_inner_wrapper_adds_the_inputs_once() {
    let (inner_ingestion, outer_ingestion) =
        (Ingestion::accepting().await, Ingestion::accepting().await);
    let (inner, _) = client(&inner_ingestion, |options| options);
    let (outer, _) = client(&outer_ingestion, |options| options);
    let desk = Desk::default();
    let once = modern(instrument(desk.clone(), &inner, InstrumentOptions::new())).await;
    let twice = modern(instrument(
        instrument(desk.clone(), &inner, InstrumentOptions::new()),
        &outer,
        InstrumentOptions::new(),
    ))
    .await;
    // Listed as one wrapper lists it.
    let (one, two) = (
        once.list_all_tools().await.unwrap(),
        twice.list_all_tools().await.unwrap(),
    );
    for (name, _, _) in catalog() {
        assert_eq!(schema_of(&two, name), schema_of(&one, name), "{name}");
    }
    let result = call(&twice, "search", json!({ "q": "x", "intent": "Why." }))
        .await
        .unwrap();
    // One ID, told once.
    assert_eq!(texts(&result).len(), 2);
    let id = told(&result).unwrap();
    assert_eq!(desk.got.lock().unwrap()[0].1, r#"{"q":"x"}"#);
    settled(&inner).await;
    settled(&outer).await;
    assert_eq!(inner_ingestion.events().len(), 1);
    assert!(outer_ingestion.events().is_empty());
    let event = &inner_ingestion.events()[0];
    assert_eq!(event["sessionId"], id.as_str());
    assert_eq!(event["sessionSource"], "issued");
    assert_eq!(event["intent"], "Why.");

    // The inner wrapper's options are the ones that count: with its extras off, the outer adds none.
    let desk = Desk::default();
    let mcp = modern(instrument(
        instrument(desk.clone(), &inner, InstrumentOptions::new().extras(false)),
        &outer,
        InstrumentOptions::new(),
    ))
    .await;
    assert_eq!(
        schema_of(&mcp.list_all_tools().await.unwrap(), "search"),
        plain()
    );
    let sent = json!({ "q": "x", "intent": "Why." });
    let result = call(&mcp, "search", sent.clone()).await.unwrap();
    assert_eq!(texts(&result), ["done"]);
    assert_eq!(desk.got.lock().unwrap()[0].1, sent.to_string());

    // A wrapper further in that records nothing (no key) leaves them to the outer one.
    let desk = Desk::default();
    let nothing = lsquare::Client::new(lsquare::Options::new().dry_run(false));
    let mcp = modern(instrument(
        instrument(desk.clone(), &nothing, InstrumentOptions::new()),
        &outer,
        InstrumentOptions::new(),
    ))
    .await;
    assert_eq!(
        schema_of(&mcp.list_all_tools().await.unwrap(), "search"),
        schema_of(&one, "search")
    );
    let result = call(&mcp, "search", sent).await.unwrap();
    assert!(told(&result).is_some());
    assert_eq!(desk.got.lock().unwrap()[0].1, r#"{"q":"x"}"#);
}

#[tokio::test]
async fn the_intent_is_recorded_as_any_other_text() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, seen) = client(&ingestion, |options| options);
    let desk = Desk::default();
    // Whether or not arguments are recorded.
    let mcp = modern(instrument(
        desk.clone(),
        &lsquare,
        InstrumentOptions::new().inputs(false),
    ))
    .await;
    call(
        &mcp,
        "search",
        json!({ "q": "x", "intent": "To write to ann@example.com about it." }),
    )
    .await
    .unwrap();
    // Cut at 500 characters, and a secret that crosses the cut never shows in part.
    let long = format!("{}{}", "why ".repeat(123), "ann@example.com and more");
    call(&mcp, "search", json!({ "q": "x", "intent": long }))
        .await
        .unwrap();
    call(
        &mcp,
        "search",
        json!({ "q": "x", "intent": "a".repeat(600) }),
    )
    .await
    .unwrap();
    // What isn't a sentence is taken out and not recorded.
    for other in [json!(""), json!(5), json!(null), json!(["why"])] {
        call(&mcp, "search", json!({ "q": "x", "intent": other }))
            .await
            .unwrap();
    }
    assert!(desk
        .got
        .lock()
        .unwrap()
        .iter()
        .all(|(_, sent)| sent == r#"{"q":"x"}"#));
    settled(&lsquare).await;
    assert_eq!(ingestion.events().len(), 7);
    let events = ingestion.events();
    assert!(events[0].get("input").is_none());
    assert_eq!(events[0]["intent"], "To write to [EMAIL_1] about it.");
    assert_eq!(
        events[1]["intent"],
        format!("{}[EMAIL_1]\u{2026}[+16 chars]", "why ".repeat(123))
    );
    assert_eq!(
        events[2]["intent"],
        format!("{}\u{2026}[+100 chars]", "a".repeat(500))
    );
    assert_eq!(events[2]["truncated"], json!(["intent"]));
    for event in &events[3..] {
        assert!(event.get("intent").is_none(), "{event}");
    }
    if let Some(answers) = ingestion_says(&events) {
        assert!(
            answers.iter().all(|answer| answer == "valid"),
            "{answers:?}"
        );
    }

    // A call nobody could be named for is sent as limited diagnostics: without what the agent said.
    let mcp = modern(instrument(
        Desk::default(),
        &lsquare,
        InstrumentOptions::new().identify(|_| async { Err(IdentifyError::new()) }),
    ))
    .await;
    call(
        &mcp,
        "search",
        json!({ "q": "x", "intent": "Why.", "session_id": SENT }),
    )
    .await
    .unwrap();
    settled(&lsquare).await;
    assert_eq!(ingestion.events().len(), 8);
    let event = ingestion.events().pop().unwrap();
    assert_eq!(event["identity"], "failed");
    for field in ["intent", "input", "sessionId", "sessionSource"] {
        assert!(event.get(field).is_none(), "{field}: {event}");
    }
    until(|| seen.codes().contains(&"identify_failed".to_owned())).await;
}

#[tokio::test]
async fn identify_is_given_the_arguments_the_tool_gets() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    let given: Arc<Mutex<Vec<String>>> = Arc::default();
    let saw = given.clone();
    let mcp = modern(instrument(
        Desk::default(),
        &lsquare,
        InstrumentOptions::new().identify(move |input| {
            let saw = saw.clone();
            async move {
                saw.lock().unwrap().push(
                    input
                        .arguments()
                        .map(|sent| Value::Object(sent.clone()).to_string())
                        .unwrap_or_default(),
                );
                Ok(lsquare::mcp::Identity::new().user_id("user_1"))
            }
        }),
    ))
    .await;
    call(
        &mcp,
        "search",
        json!({ "q": "x", "intent": "Why.", "session_id": SENT }),
    )
    .await
    .unwrap();
    settled(&lsquare).await;
    assert_eq!(ingestion.events().len(), 1);
    assert_eq!(*given.lock().unwrap(), [r#"{"q":"x"}"#]);
    let event = &ingestion.events()[0];
    assert_eq!(event["userId"], "user_1");
    assert_eq!(event["sessionId"], SENT);
}

// A server made with rmcp's macros, whose tool refuses inputs it doesn't know.

#[derive(Clone)]
struct Strict;

#[derive(serde::Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
struct Lookup {
    /// What to look up.
    word: String,
}

#[tool_router]
impl Strict {
    #[tool(description = "Looks a word up.")]
    async fn look_up(&self, Parameters(Lookup { word }): Parameters<Lookup>) -> String {
        format!("{word}: a word.")
    }

    #[tool(description = "Says the time.")]
    async fn now(&self) -> String {
        "Noon.".to_owned()
    }
}

#[tool_handler(name = "strict", version = "1.0.0")]
impl ServerHandler for Strict {}

#[tokio::test]
async fn a_server_made_with_rmcps_macros_gets_the_inputs() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, seen) = client(&ingestion, |options| options);
    let sent = json!({ "word": "desk", "intent": "To spell it.", "session_id": SENT });

    // Without the wrapper, the tool refuses them.
    let bare = modern(Strict).await;
    let unanswered = call(&bare, "look_up", json!({})).await.unwrap();
    let refused = call(&bare, "look_up", sent.clone()).await.unwrap();
    assert_eq!(refused.is_error, Some(true));
    assert_eq!(
        texts(&refused),
        ["failed to deserialize parameters: unknown field `intent`, expected `word`"]
    );

    let mcp = modern(instrument(Strict, &lsquare, InstrumentOptions::new())).await;
    let tools = mcp.list_all_tools().await.unwrap();
    let (session_id, intent) = the_two();
    for name in ["look_up", "now"] {
        let schema = schema_of(&tools, name);
        assert_eq!(schema["properties"]["session_id"], session_id, "{schema}");
        assert_eq!(schema["properties"]["intent"], intent, "{schema}");
    }
    assert!(schema_of(&tools, "look_up")["properties"]
        .get("word")
        .is_some());
    assert_eq!(schema_of(&tools, "look_up")["required"], json!(["word"]));

    let result = call(&mcp, "look_up", sent).await.unwrap();
    assert_eq!(texts(&result), ["desk: a word."]);
    let result = call(&mcp, "now", json!({ "intent": "To greet the user." }))
        .await
        .unwrap();
    assert_eq!(texts(&result)[0], "Noon.");
    assert!(told(&result).is_some());
    // A required input left out is answered by rmcp, as an error result: no session starts on it.
    let missing = call(&mcp, "look_up", json!({ "intent": "To spell it." }))
        .await
        .unwrap();
    assert_eq!(missing.is_error, Some(true));
    assert_eq!(
        texts(&missing),
        ["failed to deserialize parameters: missing field `word`"]
    );
    // The whole of it is what rmcp answers without the wrapper.
    assert_eq!(missing, unanswered);

    settled(&lsquare).await;
    assert_eq!(ingestion.events().len(), 3);
    let events = ingestion.events();
    assert_eq!(events[0]["input"], json!({ "word": "desk" }));
    assert_eq!(events[0]["intent"], "To spell it.");
    assert_eq!(events[0]["sessionSource"], "sent");
    assert_eq!(events[1]["sessionSource"], "issued");
    assert_eq!(events[2]["status"], "error");
    assert!(events[2].get("sessionId").is_none());
    assert!(seen.lines().is_empty(), "{:?}", seen.lines());
}

// Over HTTP, as rmcp serves it.

/// A server on a local port, built by `factory` as rmcp asks for one: its URL.
async fn http<S: ServerHandler>(
    factory: impl Fn() -> S + Send + Sync + 'static,
    config: StreamableHttpServerConfig,
) -> String {
    let service = StreamableHttpService::new(
        move || Ok(factory()),
        LocalSessionManager::default().into(),
        config,
    );
    let router = axum::Router::new().nest_service("/mcp", service);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}/mcp", listener.local_addr().unwrap());
    tokio::spawn(async move {
        let _ = axum::serve(listener, router).await;
    });
    url
}

/// Posts one JSON-RPC message as a client on protocol 2025-06-18: the response's session header,
/// and its JSON, read from the body or from the event stream's first message.
async fn post(url: &str, session: Option<&str>, message: Value) -> (Option<String>, Value) {
    let mut request = reqwest::Client::new()
        .post(url)
        .header("content-type", "application/json")
        .header("accept", "application/json, text/event-stream")
        .header("mcp-protocol-version", "2025-06-18");
    if let Some(session) = session {
        request = request.header("mcp-session-id", session);
    }
    let response = request.body(message.to_string()).send().await.unwrap();
    let session = response
        .headers()
        .get("mcp-session-id")
        .and_then(|value| value.to_str().ok())
        .map(str::to_owned);
    let body = response.text().await.unwrap();
    let json = serde_json::from_str(&body).ok().or_else(|| {
        body.lines()
            .filter_map(|line| line.strip_prefix("data:"))
            .find_map(|data| serde_json::from_str(data.trim()).ok())
    });
    (session, json.unwrap_or(Value::Null))
}

fn initialize() -> Value {
    json!({ "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": { "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": { "name": "http-app", "version": "1.0.0" } } })
}

fn search(id: u32) -> Value {
    json!({ "jsonrpc": "2.0", "id": id, "method": "tools/call", "params": { "name": "search", "arguments": { "q": format!("q{id}") } } })
}

/// The texts of a `tools/call` response's content.
fn said(response: &Value) -> Vec<String> {
    response["result"]["content"]
        .as_array()
        .map(|blocks| {
            blocks
                .iter()
                .map(|block| block["text"].as_str().unwrap_or_default().to_owned())
                .collect()
        })
        .unwrap_or_default()
}

#[tokio::test]
async fn over_http_each_session_is_grouped_by_its_connection() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    // One wrapper, copied for each session.
    let server = instrument(Desk::default(), &lsquare, InstrumentOptions::new());
    let url = http(
        move || server.clone(),
        StreamableHttpServerConfig::default(),
    )
    .await;

    let mut sessions = Vec::new();
    for _ in 0..2 {
        let (session, answer) = post(&url, None, initialize()).await;
        assert_eq!(
            answer["result"]["protocolVersion"], "2025-06-18",
            "{answer}"
        );
        let session = session.expect("the server starts a session");
        post(
            &url,
            Some(&session),
            json!({ "jsonrpc": "2.0", "method": "notifications/initialized" }),
        )
        .await;
        sessions.push(session);
    }
    for (id, session) in [(2, &sessions[0]), (3, &sessions[0]), (4, &sessions[1])] {
        let (_, answer) = post(&url, Some(session), search(id)).await;
        assert_eq!(said(&answer), ["done"], "{answer}");
    }
    settled(&lsquare).await;
    assert_eq!(ingestion.events().len(), 3);
    let events = ingestion.events();
    for event in &events {
        assert_eq!(event["sessionSource"], "protocol", "{event}");
        // Never the transport's own ID, which is a handle to the session.
        assert!(!sessions
            .iter()
            .any(|session| event["sessionId"] == session.as_str()));
    }
    assert_eq!(events[0]["sessionId"], events[1]["sessionId"]);
    assert_ne!(events[0]["sessionId"], events[2]["sessionId"]);
}

#[tokio::test]
async fn over_http_without_sessions_each_call_is_given_an_id() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    let stateless = || {
        StreamableHttpServerConfig::default()
            .with_legacy_session_mode(false)
            .with_json_response(true)
    };
    // A wrapper for each request, and one shared by them all: neither serves one connection.
    let server = instrument(Desk::default(), &lsquare, InstrumentOptions::new());
    let copied = http(move || server.clone(), stateless()).await;
    let server = Arc::new(instrument(
        Desk::default(),
        &lsquare,
        InstrumentOptions::new(),
    ));
    let shared = http(move || server.clone(), stateless()).await;

    let mut told = Vec::new();
    let mut asked = 0;
    for url in [&copied, &shared] {
        let (session, answer) = post(url, None, initialize()).await;
        assert!(session.is_none());
        assert_eq!(
            answer["result"]["protocolVersion"], "2025-06-18",
            "{answer}"
        );
        for id in [2, 3] {
            asked += 1;
            let (_, answer) = post(url, None, search(asked)).await;
            let texts = said(&answer);
            assert_eq!(texts.len(), 2, "{answer} to {id}");
            let id = texts[1]
                .strip_prefix("Task ID: ")
                .and_then(|rest| rest.split_once(". Send it as session_id"))
                .map(|(id, _)| id.to_owned())
                .unwrap_or_else(|| panic!("{answer}"));
            told.push((format!("q{asked}"), id));
        }
    }
    settled(&lsquare).await;
    let events = ingestion.events();
    assert_eq!(events.len(), 4);
    // Each call's event carries the ID its own reply told, and no two calls share one.
    for (asked, id) in &told {
        let event = events
            .iter()
            .find(|event| event["input"]["q"] == asked.as_str())
            .unwrap_or_else(|| panic!("no event for {asked}"));
        assert_eq!(event["sessionSource"], "issued", "{event}");
        assert_eq!(event["sessionId"], id.as_str());
        assert_eq!(event["protocolVersion"], "2025-06-18");
    }
    let mut ids: Vec<&String> = told.iter().map(|(_, id)| id).collect();
    ids.sort();
    ids.dedup();
    assert_eq!(ids.len(), 4);
}
