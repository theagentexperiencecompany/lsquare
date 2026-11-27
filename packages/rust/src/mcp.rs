//! The wrapper for the official Rust MCP SDK, [`rmcp`]: one `tool_call` event per `tools/call`.
//!
//! ```no_run
//! # use rmcp::{ServerHandler, ServiceExt};
//! # #[derive(Clone)] struct Weather;
//! # impl ServerHandler for Weather {}
//! # async fn run() -> Result<(), Box<dyn std::error::Error>> {
//! use lsquare::mcp::{instrument, InstrumentOptions};
//!
//! let lsquare = lsquare::Client::new(lsquare::Options::new().api_key(std::env::var("LSQUARE_KEY").ok()));
//! let server = instrument(Weather, &lsquare, InstrumentOptions::new());
//! let running = server.serve(rmcp::transport::stdio()).await?;
//! running.waiting().await?;
//! lsquare.shutdown().await;
//! # Ok(()) }
//! ```
//!
//! [`Instrumented`] is a [`ServerHandler`] itself. It records `call_tool`, notes the descriptions
//! `list_tools` gives, and passes every other method of this version of `rmcp` on to the handler
//! it wraps, unchanged. Nothing it does can make a tool call fail: the handler's answer, error or
//! panic reaches the client as it would without it.
//!
//! It also adds two optional inputs to the tools it can, `session_id` and `intent` (see
//! [`InstrumentOptions::extras`]): `list_tools` shows them, `call_tool` takes them out before the
//! handler sees the call, and a call that came without a session gets one, told to the agent in a
//! text block at the end of the tool's result. And it adds one tool, `get_more_tools`, which it
//! answers itself: an agent calls it to say what it needed and couldn't do.

use std::any::Any;
use std::borrow::Cow;
use std::cell::Cell;
use std::collections::{HashMap, HashSet};
use std::fmt;
use std::future::Future;
use std::panic::{catch_unwind, resume_unwind, AssertUnwindSafe};
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, PoisonError};
use std::task::{Context as TaskContext, Poll};
use std::time::{Duration, Instant};

#[allow(deprecated)]
use rmcp::model::{
    CallToolRequestParams, CallToolResponse, CallToolResult, CancelTaskParams,
    CancelledNotificationParam, ClientCapabilities, CompleteRequestParams, CompleteResult,
    ContentBlock, CustomNotification, CustomRequest, CustomResult, DiscoverResult,
    GetPromptRequestParams, GetPromptResponse, GetSkillRequestParams, GetSkillResult,
    GetTaskParams, GetTaskResult, Implementation, InitializeRequestParams, InitializeResult,
    ListPromptsResult, ListResourceTemplatesResult, ListResourcesResult, ListSkillsResult,
    ListToolsResult, PaginatedRequestParams, ProgressNotificationParam, ProtocolVersion,
    ReadResourceDirectoryRequestParams, ReadResourceDirectoryResult, ReadResourceRequestParams,
    ReadResourceResponse, RequestId, ResourceContents, ServerConfig, SetLevelRequestParams,
    SubscribeRequestParams, SubscriptionFilter, Tool, ToolAnnotations, UnsubscribeRequestParams,
    UpdateTaskParams,
};
use rmcp::service::{
    MaybeSendFuture, NotificationContext, Peer, RequestContext, RoleServer, SubscriptionContext,
};
use rmcp::{ErrorData as McpError, ServerHandler};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use tokio::runtime::Handle;
use tokio::task::JoinHandle;

use crate::client::{Client, Error, ErrorCode, Inner, Level};
use crate::event::{BlockIn, CallData, CallHead, CallShaper, ResourceIn, ResultIn, TypedResult};
use crate::extras::{
    advertise, id_block, is_session_id, listed_ownership, missing_tools_reply,
    missing_tools_schema, new_session_id, Ownership, INTENT, MISSING_TOOLS,
    MISSING_TOOLS_DESCRIPTION, SESSION_ID,
};
use crate::json::stringify;
use crate::normalize::is_valid_id;
use crate::shape::MAX_ENTRIES;
use crate::time::{iso, now_ms};

/// How long a call's event waits for `identify` after the tool returns.
const IDENTIFY_WAIT: Duration = Duration::from_millis(1_000);
/// How many listed tools' descriptions an instance keeps.
const MAX_LISTED: usize = 10_000;

/// The IDs `identify` knows for a call, and optionally metadata. Each is optional: a call with
/// none is recorded without.
#[derive(Debug, Default, Clone, PartialEq)]
pub struct Identity {
    org_id: Option<String>,
    user_id: Option<String>,
    conversation_id: Option<String>,
    metadata: Option<Map<String, Value>>,
}

impl Identity {
    /// No IDs.
    pub fn new() -> Identity {
        Identity::default()
    }

    /// The customer organisation the call was made for.
    pub fn org_id(mut self, id: impl Into<String>) -> Identity {
        self.org_id = Some(id.into());
        self
    }

    /// The person the call was made for. An internal ID, not an email address: events can be deleted by it.
    pub fn user_id(mut self, id: impl Into<String>) -> Identity {
        self.user_id = Some(id.into());
        self
    }

    /// The conversation the call belongs to.
    pub fn conversation_id(mut self, id: impl Into<String>) -> Identity {
        self.conversation_id = Some(id.into());
        self
    }

    /// One entry of the event's `metadata`: a string, a number, a boolean or null.
    pub fn metadata(mut self, key: impl Into<String>, value: impl Into<Value>) -> Identity {
        self.metadata
            .get_or_insert_with(Map::new)
            .insert(key.into(), value.into());
        self
    }
}

/// `identify` couldn't tell who the call was for. The call is then sent as limited diagnostics:
/// its tool, status, duration and error category, and nothing that could name anyone. Any error
/// converts into it, so `?` works inside `identify`.
pub struct IdentifyError(());

impl IdentifyError {
    /// An identification that failed.
    pub fn new() -> IdentifyError {
        IdentifyError(())
    }
}

impl Default for IdentifyError {
    fn default() -> IdentifyError {
        IdentifyError::new()
    }
}

impl fmt::Debug for IdentifyError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("IdentifyError")
    }
}

// Not an `Error` itself, so that every error converts into it. What the error says is never recorded.
impl<E: std::error::Error> From<E> for IdentifyError {
    fn from(_: E) -> IdentifyError {
        IdentifyError::new()
    }
}

/// What `identify` is given: the call, and the MCP SDK's request context, with what it knows of
/// the client (its extensions hold the HTTP request's parts on a streamable HTTP server).
pub struct IdentifyInput {
    tool_name: String,
    arguments: Arc<Value>,
    context: RequestContext<RoleServer>,
}

impl IdentifyInput {
    /// The tool that was called.
    pub fn tool_name(&self) -> &str {
        &self.tool_name
    }

    /// The arguments as the tool gets them: as the client sent them, without the `session_id`
    /// and `intent` the wrapper added to the tool's inputs.
    pub fn arguments(&self) -> Option<&Map<String, Value>> {
        self.arguments.as_object()
    }

    /// The MCP SDK's request context. Only the IDs returned are sent.
    pub fn context(&self) -> &RequestContext<RoleServer> {
        &self.context
    }
}

type IdentifyFuture = Pin<Box<dyn Future<Output = Result<Identity, IdentifyError>> + Send>>;
type Identify = Arc<dyn Fn(IdentifyInput) -> IdentifyFuture + Send + Sync>;
type PerTool = Arc<dyn Fn(&str) -> bool + Send + Sync>;

/// Everywhere, nowhere, or for the tools a function answers true for.
#[derive(Clone)]
enum Choice {
    All,
    None,
    PerTool(PerTool),
}

impl Choice {
    fn of(all: bool) -> Choice {
        if all {
            Choice::All
        } else {
            Choice::None
        }
    }
}

/// What the wrapper records, and how it learns who a call was for.
#[derive(Clone)]
pub struct InstrumentOptions {
    identify: Option<Identify>,
    inputs: Choice,
    results: Choice,
    descriptions: Choice,
    instructions: bool,
    sessions: bool,
    intent: bool,
    missing_tools: bool,
}

impl Default for InstrumentOptions {
    fn default() -> InstrumentOptions {
        InstrumentOptions {
            identify: None,
            inputs: Choice::All,
            results: Choice::All,
            descriptions: Choice::All,
            instructions: true,
            sessions: true,
            intent: true,
            missing_tools: true,
        }
    }
}

impl fmt::Debug for InstrumentOptions {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("InstrumentOptions")
            .field("identify", &self.identify.is_some())
            .finish_non_exhaustive()
    }
}

impl InstrumentOptions {
    /// Everything recorded, no `identify`.
    pub fn new() -> InstrumentOptions {
        InstrumentOptions::default()
    }

    /// Who the call is for. It runs alongside the tool, never before it, and is waited for at most
    /// a second after the tool returns; one still running then is stopped at its next `await`. If
    /// it fails, takes too long, panics or returns an invalid ID, the call is sent as limited
    /// diagnostics and `on_error` is told (`identify_failed`).
    pub fn identify<F, Fut>(mut self, identify: F) -> InstrumentOptions
    where
        F: Fn(IdentifyInput) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<Identity, IdentifyError>> + Send + 'static,
    {
        self.identify = Some(Arc::new(move |input| Box::pin(identify(input))));
        self
    }

    /// Record arguments: everywhere (the default) or nowhere.
    pub fn inputs(mut self, record: bool) -> InstrumentOptions {
        self.inputs = Choice::of(record);
        self
    }

    /// Record arguments for the tools this returns true for.
    pub fn inputs_for(
        mut self,
        record: impl Fn(&str) -> bool + Send + Sync + 'static,
    ) -> InstrumentOptions {
        self.inputs = Choice::PerTool(Arc::new(record));
        self
    }

    /// Record results: everywhere (the default) or nowhere. Strings are cut at 4,000 characters,
    /// and files reduced to their size.
    pub fn results(mut self, record: bool) -> InstrumentOptions {
        self.results = Choice::of(record);
        self
    }

    /// Record results for the tools this returns true for.
    pub fn results_for(
        mut self,
        record: impl Fn(&str) -> bool + Send + Sync + 'static,
    ) -> InstrumentOptions {
        self.results = Choice::PerTool(Arc::new(record));
        self
    }

    /// Record each call's tool description, as agents were listed it: everywhere (the default) or
    /// nowhere. Cut at 1,000 characters.
    pub fn descriptions(mut self, record: bool) -> InstrumentOptions {
        self.descriptions = Choice::of(record);
        self
    }

    /// Record descriptions for the tools this returns true for.
    pub fn descriptions_for(
        mut self,
        record: impl Fn(&str) -> bool + Send + Sync + 'static,
    ) -> InstrumentOptions {
        self.descriptions = Choice::PerTool(Arc::new(record));
        self
    }

    /// Record the server's instructions on each call, as the server gives them to clients (the
    /// default), or not. Cut at 2,000 characters. They are the server's, whichever the tool: there
    /// is no choice per tool.
    pub fn instructions(mut self, record: bool) -> InstrumentOptions {
        self.instructions = record;
        self
    }

    /// What the wrapper adds to the server's tools, all of it (the default) or none: the
    /// `session_id` and `intent` inputs, and the `get_more_tools` tool.
    /// [`sessions`](Self::sessions), [`intent`](Self::intent) and
    /// [`missing_tools`](Self::missing_tools) choose one by one.
    ///
    /// A tool gets an input when the handler's `get_tool` gives its definition (a server made with
    /// rmcp's `#[tool_handler]` does), its input schema is an object of named properties
    /// (`type: "object"`, `additionalProperties` absent or false, no `anyOf` and the like), and it
    /// has no input of that name itself. `call_tool` then takes the input out of the call before
    /// the handler sees it, and `list_tools` shows it where the schema the tool is listed with
    /// says the same of the two names. A tool that gets neither is listed and called as it is.
    pub fn extras(mut self, add: bool) -> InstrumentOptions {
        self.sessions = add;
        self.intent = add;
        self.missing_tools = add;
        self
    }

    /// Group each task's calls into a session (the default), or not. The agent sends the task's
    /// ID as `session_id`; a call without one, on a connection that has no session of its own, is
    /// given a new ID in a text block at the end of the tool's result.
    pub fn sessions(mut self, add: bool) -> InstrumentOptions {
        self.sessions = add;
        self
    }

    /// Ask the agent why it calls each tool (the default), or not: an optional `intent` input,
    /// recorded on the call's event whether or not arguments are, cut at 500 characters.
    pub fn intent(mut self, add: bool) -> InstrumentOptions {
        self.intent = add;
        self
    }

    /// Give agents a tool to say what they needed and couldn't do (the default), or not:
    /// `get_more_tools`, with a required `need`. The wrapper lists it after the server's own
    /// tools and answers it itself ("Noted. No other tools are available right now."); the call
    /// is recorded as any other, marked `extra: "missing_tools"`.
    ///
    /// It is there while the handler's `get_tool` gives a definition for a tool of its own and
    /// none for one of that name, the `inputs` option records its arguments, and the client
    /// hasn't been shut down. Otherwise nothing is listed, and a call to that name goes to the
    /// handler like any other.
    pub fn missing_tools(mut self, add: bool) -> InstrumentOptions {
        self.missing_tools = add;
        self
    }
}

struct Shared {
    client: Arc<Inner>,
    options: InstrumentOptions,
    /// The description of each tool this instance last listed.
    listed: Mutex<HashMap<String, String>>,
    /// What the server calls itself, read as it was instrumented.
    server: Option<Value>,
    /// The same as one text: what the client keeps this server's tool names by.
    server_key: String,
    /// The instructions it gives clients, read then too, when they are recorded and it has some.
    instructions: Option<Value>,
    /// The tools whose warnings about the extras' inputs were given already.
    noted: Mutex<HashSet<String>>,
}

/// A connection that started with `initialize` through a wrapper: what stands for a protocol
/// session, where the protocol has them. An ID of its own, and its peer, which its calls are
/// known by.
struct Connection {
    id: String,
    peer: Peer<RoleServer>,
}

/// How many open connections a wrapper keeps an ID for: those past it have no session of their
/// own, as on a server without sessions.
const MAX_CONNECTIONS: usize = 1_000;

/// Whether two peers are one connection's. The copies of a `Peer` that rmcp hands out on one
/// connection share what its client said of itself as it connected, the very same value; two
/// connections never do, whatever their clients said.
fn same_connection(one: &Peer<RoleServer>, other: &Peer<RoleServer>) -> bool {
    matches!(
        (one.peer_info(), other.peer_info()),
        (Some(one), Some(other)) if Arc::ptr_eq(&one, &other)
    )
}

/// A [`ServerHandler`] whose tool calls are recorded. Made by [`instrument`]; serve it where the
/// handler was served.
pub struct Instrumented<H> {
    handler: H,
    shared: Arc<Shared>,
    /// The connections it serves that started with `initialize`.
    connections: Mutex<Vec<Connection>>,
}

impl<H: Clone> Clone for Instrumented<H> {
    /// A copy that records as this one does. It serves connections of its own: those this one
    /// serves stay with it.
    fn clone(&self) -> Instrumented<H> {
        Instrumented {
            handler: self.handler.clone(),
            shared: self.shared.clone(),
            connections: Mutex::new(Vec::new()),
        }
    }
}

impl<H: fmt::Debug> fmt::Debug for Instrumented<H> {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("Instrumented")
            .field("handler", &self.handler)
            .finish_non_exhaustive()
    }
}

impl<H> Instrumented<H> {
    /// The handler it wraps.
    pub fn get_ref(&self) -> &H {
        &self.handler
    }

    /// The handler it wraps, back.
    pub fn into_inner(self) -> H {
        self.handler
    }
}

/// Records every tool call `handler` answers. Each `tools/call` becomes one `tool_call` event:
/// the tool, its description, the server's instructions, the call's arguments, its result (text
/// shortened, files as sizes), how it ended and how long it took, the calling app and the protocol
/// version, and the IDs `identify` gives. Never panics.
///
/// The server's name, version and instructions are read here, from `get_info`, once: a handler
/// whose `get_info` answers differently later is recorded with what it answered now.
///
/// A server built for each session or request (as `StreamableHttpService` builds them) is
/// instrumented each time, with the same client, or instrumented once and cloned. A handler
/// instrumented twice, one wrapper inside the other, is recorded once, by the inner one and with
/// its options.
///
/// Unless [`InstrumentOptions::extras`] turns them off, the tools it can add them to get two
/// optional inputs, `session_id` and `intent`, which never reach the handler.
pub fn instrument<H: ServerHandler>(
    handler: H,
    client: &Client,
    options: InstrumentOptions,
) -> Instrumented<H> {
    let inner = client.inner().clone();
    // Its name and version, and the instructions it gives clients, read here once: the app's
    // `get_info` never runs on a call's path. Only instructions that aren't empty, which is all a
    // client is ever sent.
    let (server, instructions) = if inner.enabled() {
        catch_unwind(AssertUnwindSafe(|| {
            let info = handler.get_info();
            let instructions = info
                .instructions
                .filter(|instructions| options.instructions && !instructions.is_empty())
                .map(Value::String);
            (
                Some(named(&info.server_info.name, &info.server_info.version)),
                instructions,
            )
        }))
        .unwrap_or((None, None))
    } else {
        (None, None)
    };
    if inner.dry_run() {
        let name = server
            .as_ref()
            .and_then(|server| server.get("name").and_then(Value::as_str));
        let mut line = Map::new();
        line.insert("server".into(), name.unwrap_or("unnamed").into());
        line.insert("records".into(), "every tools/call".into());
        line.insert("extras".into(), extras_line(&options));
        inner.log(
            Level::Info,
            &format!("[lsquare dry-run] {}", stringify(&line)),
        );
    }
    Instrumented {
        handler,
        shared: Arc::new(Shared {
            client: inner,
            options,
            listed: Mutex::new(HashMap::new()),
            server_key: server.as_ref().map(Value::to_string).unwrap_or_default(),
            server,
            instructions,
            noted: Mutex::new(HashSet::new()),
        }),
        connections: Mutex::new(Vec::new()),
    }
}

/// What dry run says of the extras: which are on, and for which tools.
fn extras_line(options: &InstrumentOptions) -> Value {
    let on: Vec<Value> = [
        (options.sessions, SESSION_ID),
        (options.intent, INTENT),
        (options.missing_tools, MISSING_TOOLS),
    ]
    .into_iter()
    .filter(|(on, _)| *on)
    .map(|(_, name)| name.into())
    .collect();
    if on.is_empty() {
        return "off".into();
    }
    let mut line = Map::new();
    line.insert("on".into(), on.into());
    if options.sessions || options.intent {
        line.insert(
            "for".into(),
            "tools the handler's get_tool gives a definition for, whose input schema is an object of named properties without an input of that name".into(),
        );
    }
    if options.missing_tools {
        line.insert(
            MISSING_TOOLS.into(),
            "listed and answered while the handler's get_tool gives a definition for a tool of its own and none of that name, and inputs are recorded for it".into(),
        );
    }
    Value::Object(line)
}

/// A call a wrapper has taken to record.
struct Taken {
    id: RequestId,
    tool_name: String,
    /// A wrapper further in, on the same handler, took the same call: it is that one's to record.
    further_in: AtomicBool,
    /// The session of the connection the call came on, as this wrapper knows it: a wrapper
    /// further in records the call with it, so one connection is one session whichever wrapper
    /// records its calls.
    connection: Option<String>,
}

impl Taken {
    fn further_in(&self) -> bool {
        self.further_in.load(Ordering::Acquire)
    }
}

tokio::task_local! {
    /// Set around a handler's call by the wrapper recording it. A wrapper further in says there
    /// that it took the same call, and the outer one then leaves the call to it. A different call
    /// the handler makes on its way (another tool of an instrumented server) is its own event.
    static TAKEN: Arc<Taken>;
}

thread_local! {
    /// Raised by a wrapper's `get_tool` as it passes the question on. A wrapper that asks the
    /// handler it wraps and finds this raised has another wrapper further in, which records the
    /// handler's calls: the extras' inputs are that one's to show and to take out.
    static FURTHER_IN: Cell<bool> = const { Cell::new(false) };
}

/// What the handler says of a tool now.
struct Registered {
    /// Its definition; none for a name the handler has no tool for, or a handler that doesn't
    /// answer `get_tool`.
    tool: Option<Tool>,
    /// A wrapper further in passed the question on.
    further_in: bool,
}

/// What the extras change of a listed page.
#[derive(Default)]
struct Listed {
    /// The tools shown with the extras' inputs: their place in the page, and their schema.
    schemas: Vec<(usize, Arc<Map<String, Value>>)>,
    /// The wrapper's own `get_more_tools`, to go after the page's tools.
    more: Option<Tool>,
}

/// What the extras took out of a call, and what they may add to its reply.
#[derive(Default)]
struct Extra {
    /// Sessions apply to this call: the option is on, and no wrapper further in has the call.
    grouping: bool,
    /// The tool leaves `session_id` to the wrapper: a call without a session can be given one.
    sessions: bool,
    /// A session ID in the right format, sent by the agent.
    sent: Option<String>,
    /// Why the call was made, as the agent said it: a string that isn't empty.
    intent: Option<Value>,
}

/// `get_more_tools` as it is listed, with `session_id` where sessions are on: TypeScript's
/// definition. It changes nothing on the server, so it is read-only, and what the agent writes
/// leaves the server.
fn missing_tools_tool(sessions: bool) -> Tool {
    Tool::new(
        MISSING_TOOLS,
        MISSING_TOOLS_DESCRIPTION,
        missing_tools_schema(sessions),
    )
    .with_annotations(
        ToolAnnotations::new()
            .read_only(true)
            .destructive(false)
            .open_world(true),
    )
}

/// The wrapper's reply to a call of `get_more_tools`: TypeScript's, an error result without a
/// `need` that is a string.
fn missing_tools_result(arguments: Option<&Map<String, Value>>) -> CallToolResult {
    let (text, refused) = missing_tools_reply(arguments);
    let content = vec![ContentBlock::text(text)];
    if refused {
        return CallToolResult::error(content);
    }
    // As TypeScript's reply: no `isError` on one that isn't.
    let mut reply = CallToolResult::success(content);
    reply.is_error = None;
    reply
}

/// What is known of a call as it arrives, before it is read.
struct Arrival {
    /// What the handler said of the tool, if it was asked already.
    looked: Option<Option<Registered>>,
    /// What the extras took out of the call.
    extra: Extra,
    /// Its connection's session.
    protocol_session: Option<String>,
    /// What the `inputs` option said of the tool, if it was asked already: it is asked once.
    inputs: Option<bool>,
    /// A call to the wrapper's own `get_more_tools`, which the wrapper answers.
    answer: bool,
}

/// Whether a protocol version has no sessions: 2026-07-28 and later. One that isn't a date
/// counts as older, as in TypeScript.
fn sessionless(version: &str) -> bool {
    let date = version.len() == 10
        && version.bytes().enumerate().all(|(index, byte)| {
            if index == 4 || index == 7 {
                byte == b'-'
            } else {
                byte.is_ascii_digit()
            }
        });
    date && version >= ProtocolVersion::V_2026_07_28.as_str()
}

/// A connection's ID as events carry it: grouping only needs the same value each time.
fn session_hash(id: &str) -> String {
    Sha256::digest(id.as_bytes())
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

/// A future that gives a panic of the one it wraps as an error, to be resumed once it's recorded.
struct CatchUnwind<F>(Pin<Box<F>>);

impl<F: Future> Future for CatchUnwind<F> {
    type Output = Result<F::Output, Box<dyn Any + Send>>;

    fn poll(mut self: Pin<&mut Self>, context: &mut TaskContext<'_>) -> Poll<Self::Output> {
        match catch_unwind(AssertUnwindSafe(|| self.0.as_mut().poll(context))) {
            Ok(Poll::Pending) => Poll::Pending,
            Ok(Poll::Ready(output)) => Poll::Ready(Ok(output)),
            Err(panic) => Poll::Ready(Err(panic)),
        }
    }
}

/// How a handler's call ended: its answer, or its panic.
type Outcome = Result<Result<CallToolResponse, McpError>, Box<dyn Any + Send>>;

/// A call the handler hasn't answered yet. Dropped with its call still there, the call was let go
/// before its answer: it is recorded then, as cancelled, unless a wrapper further in took it.
struct Unanswered<'a, H: ServerHandler> {
    wrapper: &'a Instrumented<H>,
    call: Option<Call>,
    taken: Arc<Taken>,
}

impl<H: ServerHandler> Drop for Unanswered<'_, H> {
    fn drop(&mut self) {
        if let Some(call) = self.call.take() {
            if !self.taken.further_in() {
                let _ = catch_unwind(AssertUnwindSafe(|| self.wrapper.finish(call, None)));
            }
        }
    }
}

/// What `identify` gave, and when.
type Identified = (Result<Identity, IdentifyError>, Instant);

/// A running `identify`: stopped if the call it is for goes away.
struct Identifying(JoinHandle<Identified>);

impl Drop for Identifying {
    fn drop(&mut self) {
        self.0.abort();
    }
}

/// One of the client's hook slots, held while an `identify` runs.
struct HookSlot(Arc<Inner>);

impl Drop for HookSlot {
    fn drop(&mut self) {
        self.0.identifying.fetch_sub(1, Ordering::AcqRel);
    }
}

enum Identification {
    /// What it will be given, read as the call arrived: it starts once the tool has.
    Ready(Box<IdentifyInput>),
    Running(Identifying),
    /// Not started: `max_concurrent_hooks` were running.
    Skipped,
}

/// A call under way: what was read of it as it arrived.
struct Call {
    call_id: String,
    captured_at: String,
    started: Instant,
    tool_name: String,
    inputs: bool,
    results: bool,
    description: Option<Value>,
    /// Its arguments, read already when they are recorded, and the rest to come.
    shaper: CallShaper,
    /// It brings what an earlier round asked for.
    continued: bool,
    supports_tasks: bool,
    /// The protocol version it is served under has input rounds (2026-07-28 on).
    input_rounds: bool,
    client: Option<Value>,
    protocol_version: Option<Value>,
    cancelled: Box<dyn Fn() -> bool + Send + Sync>,
    identification: Option<Identification>,
    /// What the extras took out of it.
    extra: Extra,
    /// The connection's own session, where the protocol version has sessions and sessions apply.
    protocol_session: Option<String>,
    /// A call to the wrapper's own `get_more_tools`.
    answer: bool,
}

fn named(name: &str, version: &str) -> Value {
    let mut out = Map::new();
    out.insert("name".into(), name.into());
    out.insert("version".into(), version.into());
    Value::Object(out)
}

fn failed(category: &str) -> Map<String, Value> {
    let mut out = Map::new();
    out.insert("category".into(), category.into());
    out
}

/// The calling app, as it named itself: in the request's own metadata, or in `initialize`. Where a
/// request comes without either (a stateless HTTP server's), rmcp fills in its own name and
/// version, which name no client: those are left out. So is a client that sent just those in
/// `initialize`, as rmcp's own client does unless it is given a name: the two can't be told apart.
fn calling_app(context: &RequestContext<RoleServer>) -> Option<Value> {
    let info = context.meta.client_info().or_else(|| {
        context
            .client_info()
            .filter(|info| *info != Implementation::default())
    })?;
    Some(named(&info.name, &info.version))
}

/// What a panic said, as `std` prints it.
fn panic_text(panic: &(dyn Any + Send)) -> String {
    match (panic.downcast_ref::<&str>(), panic.downcast_ref::<String>()) {
        (Some(text), _) => (*text).to_owned(),
        (_, Some(text)) => text.clone(),
        _ => "Unknown error".to_owned(),
    }
}

impl<H: ServerHandler> Instrumented<H> {
    /// Asks one of the options' functions about a tool. One that panics records nothing for that
    /// call, and one warning per option says so.
    fn chooses(&self, choice: &Choice, option: &str, tool_name: &str) -> bool {
        match choice {
            Choice::All => true,
            Choice::None => false,
            Choice::PerTool(ask) => catch_unwind(AssertUnwindSafe(|| ask(tool_name))).unwrap_or_else(|_| {
                self.shared.client.warn_once(&format!("{option}-unanswered"), &format!("[lsquare] The {option} function panicked: {option} aren't recorded for the calls it answered that way."));
                false
            }),
        }
    }

    /// What the handler says of a tool now, asked once for a call or a listed tool: its
    /// definition, and whether a wrapper further in passed the question on. `None` when its
    /// `get_tool` panicked.
    fn registered(&self, tool_name: &str) -> Option<Registered> {
        // Lowered, then read once the handler has answered: `get_tool` isn't async and is given
        // no request, so nothing else looks a tool up on this thread in between.
        let _ = FURTHER_IN.try_with(|raised| raised.set(false));
        let tool = catch_unwind(AssertUnwindSafe(|| self.handler.get_tool(tool_name)));
        let further_in = FURTHER_IN
            .try_with(|raised| raised.replace(false))
            .unwrap_or(false);
        let tool = tool.ok()?;
        // A name to ask this server's handlers about (see `answers()`): the definition's own.
        if let (Some(defined), false) = (&tool, further_in) {
            self.shared
                .client
                .hint_tool(&self.shared.server_key, &defined.name);
        }
        Some(Registered { tool, further_in })
    }

    /// Whether the handler has no tool named `get_more_tools`, given what its `get_tool` said of
    /// that name. One it has is its own, and stays: logged once per client.
    fn name_is_free(&self, looked: Option<&Registered>) -> bool {
        match looked {
            Some(Registered { tool: None, .. }) => true,
            Some(_) => {
                self.shared.client.warn_once("extras-own/get_more_tools", "[lsquare] The server has its own get_more_tools tool: it is left as it is, and LSquare's isn't added.");
                false
            }
            // Its `get_tool` panicked: nothing is known.
            None => false,
        }
    }

    /// Whether the `inputs` option records `get_more_tools`' arguments, asked as a page is
    /// listed. Where it doesn't, the tool isn't offered, since what it tells agents wouldn't be
    /// true: logged once per client, and a function that panics is said so in words of its own.
    fn lists_need(&self) -> bool {
        let recorded = match &self.shared.options.inputs {
            Choice::All => true,
            Choice::None => false,
            Choice::PerTool(ask) => match catch_unwind(AssertUnwindSafe(|| ask(MISSING_TOOLS))) {
                Ok(recorded) => recorded,
                Err(_) => {
                    self.shared.client.warn_once("inputs-unanswered/get_more_tools", "[lsquare] The inputs function panicked when asked about get_more_tools: it isn't listed.");
                    return false;
                }
            },
        };
        if !recorded {
            self.shared.client.warn_once("extras-no-inputs/get_more_tools", "[lsquare] get_more_tools isn't offered: inputs are off for it, so what an agent asks for wouldn't be recorded.");
        }
        recorded
    }

    /// Whether a call to `get_more_tools` is the wrapper's to answer, and what the `inputs`
    /// option said of it if it was asked, which it is once per call. It is the wrapper's while
    /// the extra is on, the client hasn't been shut down, the handler has no tool of that name
    /// and defines one of its own, and the call's arguments are recorded.
    ///
    /// The handler's own tool is one this client has seen a handler of this server define, asked
    /// of this handler again: a wrapper built for one request has seen no tool itself, and never
    /// lists the server's tools to find one. `looked` is what the handler said of the name.
    fn answers(&self, looked: Option<&Registered>) -> (bool, Option<bool>) {
        let options = &self.shared.options;
        let client = &self.shared.client;
        if !options.missing_tools || client.closed() || !self.name_is_free(looked) {
            return (false, None);
        }
        let hints = client.tool_hints(&self.shared.server_key);
        let defines = hints.iter().any(|name| {
            self.registered(name)
                .is_some_and(|registered| registered.tool.is_some() && !registered.further_in)
        });
        if !defines {
            return (false, None);
        }
        // Why it isn't offered was said as it wasn't listed.
        let inputs = self.chooses(&options.inputs, "inputs", MISSING_TOOLS);
        (inputs, Some(inputs))
    }

    /// What the wrapper takes out of a call to its own `get_more_tools`: the `session_id`, when
    /// sessions are on. Anything else the agent sent stays, and is recorded with the need.
    fn prepare_answer(&self, request: &mut CallToolRequestParams) -> Extra {
        let sessions = self.shared.options.sessions;
        let mut extra = Extra {
            grouping: sessions,
            sessions,
            ..Extra::default()
        };
        if sessions {
            if let Some(Value::String(id)) = request
                .arguments
                .as_mut()
                .and_then(|arguments| arguments.shift_remove(SESSION_ID))
            {
                extra.sent = Some(id).filter(|id| is_session_id(id));
            }
        }
        extra
    }

    /// The tool's description as agents are listed it now: the registered tool's, or else the one
    /// this instance last listed, so a tool no page has shown yet has none. A `get_tool` that
    /// panics gives none, and the call is recorded without.
    fn description(&self, tool_name: &str, registered: Option<&Registered>) -> Option<String> {
        match &registered?.tool {
            Some(tool) => tool.description.as_deref().map(str::to_owned),
            None => self
                .shared
                .listed
                .lock()
                .unwrap_or_else(PoisonError::into_inner)
                .get(tool_name)
                .cloned(),
        }
        .filter(|description| !description.is_empty())
    }

    /// A tool the extras leave alone, or one with an input of its own of their names, is logged
    /// once per client.
    fn note_ownership(&self, tool_name: &str, ownership: Ownership) {
        let options = &self.shared.options;
        let own = |name: &str, on: bool| on && ownership.declares(name);
        let (session_id, intent) = (
            own(SESSION_ID, options.sessions),
            own(INTENT, options.intent),
        );
        if ownership != Ownership::Unreadable && !session_id && !intent {
            return;
        }
        if !self.first_note(tool_name) {
            return;
        }
        let client = &self.shared.client;
        let label = Value::from(tool_name).to_string();
        if ownership == Ownership::Unreadable {
            client.warn_once_of_tool(&format!("extras-unreadable/{tool_name}"), &format!("[lsquare] Tool {label} gets no session_id or intent: its input schema can't be read safely (it isn't just an object of named properties), so any input could be its own."));
        }
        for (own, name) in [(session_id, SESSION_ID), (intent, INTENT)] {
            if own {
                client.warn_once_of_tool(
                    &format!("extras-own/{name}/{tool_name}"),
                    &format!(
                        "[lsquare] Tool {label} has its own {name} input: it is left as it is."
                    ),
                );
            }
        }
    }

    /// A listed tool the handler gives no definition for is logged once per client.
    fn note_undefined(&self, tool_name: &str) {
        if !self.first_note(tool_name) {
            return;
        }
        self.shared.client.warn_once_of_tool(&format!("extras-undefined/{tool_name}"), &format!("[lsquare] Tool {} gets no session_id or intent: the handler's get_tool gives no definition for it. rmcp's #[tool_handler] writes get_tool; a handler written by hand needs its own.", Value::from(tool_name)));
    }

    /// Whether this wrapper has said nothing of a tool yet, and can still: it remembers the
    /// first 10,000, so the client isn't asked again on every call.
    fn first_note(&self, tool_name: &str) -> bool {
        let mut noted = self
            .shared
            .noted
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        if noted.contains(tool_name) || noted.len() >= MAX_LISTED {
            return false;
        }
        noted.insert(tool_name.to_owned());
        true
    }

    /// Takes the extras' inputs out of a call to a tool whose definition, as the handler gives it
    /// now, leaves them to the wrapper, valid or not. A tool that declares one of them keeps it,
    /// and one whose schema can't be read, or that the handler gives no definition for, keeps its
    /// arguments as sent.
    fn prepare(
        &self,
        request: &mut CallToolRequestParams,
        registered: Option<&Registered>,
    ) -> Extra {
        let options = &self.shared.options;
        let mut extra = Extra::default();
        if registered.is_some_and(|registered| registered.further_in) {
            return extra;
        }
        // Even a tool the extras leave alone is grouped by its connection's session.
        extra.grouping = options.sessions;
        let Some(tool) = registered.and_then(|registered| registered.tool.as_ref()) else {
            return extra;
        };
        let ownership = listed_ownership(&tool.input_schema);
        self.note_ownership(&request.name, ownership);
        if ownership == Ownership::Unreadable {
            return extra;
        }
        extra.sessions = options.sessions && !ownership.declares(SESSION_ID);
        let intents = options.intent && !ownership.declares(INTENT);
        let Some(arguments) = request.arguments.as_mut() else {
            return extra;
        };
        // The other arguments keep their order.
        if extra.sessions {
            if let Some(Value::String(id)) = arguments.shift_remove(SESSION_ID) {
                extra.sent = Some(id).filter(|id| is_session_id(id));
            }
        }
        if intents {
            extra.intent = arguments
                .shift_remove(INTENT)
                .filter(|intent| intent.as_str().is_some_and(|text| !text.is_empty()));
        }
        extra
    }

    /// What the extras change of a listed page: the tools that get the inputs, by their place in
    /// the page, each with the schema it is shown with, and the wrapper's own `get_more_tools`
    /// where it is added.
    ///
    /// A tool gets the inputs when the handler gives a definition for it now that says of them
    /// what the listing says, where the listed schema takes them. `get_more_tools` goes on a last
    /// page that lists a tool the handler defines and none of that name, while the handler has
    /// none either, its arguments are recorded and the client hasn't been shut down.
    fn advertised(&self, page: &ListToolsResult) -> Listed {
        let options = &self.shared.options;
        let client = &self.shared.client;
        let mut listed = Listed::default();
        let inputs = options.sessions || options.intent;
        let more = options.missing_tools && page.next_cursor.is_none();
        if !client.enabled() || !(inputs || more) || page.tools.len() > MAX_LISTED {
            return listed;
        }
        // A tool on the page that the handler defines, and not through a wrapper further in.
        let mut defines = false;
        // A tool on the page named `get_more_tools`.
        let mut named = false;
        for (index, tool) in page.tools.iter().enumerate() {
            let own = tool.name == MISSING_TOOLS;
            named |= own;
            // With the inputs off, one tool the handler defines is all that is looked for.
            if !inputs && defines {
                continue;
            }
            let Some(registered) = self.registered(&tool.name) else {
                continue;
            };
            // A wrapper further in has this tool, and showed its own already.
            if registered.further_in {
                continue;
            }
            let Some(current) = registered.tool else {
                // Nothing is said of a `get_more_tools` without a definition: it is a wrapper's
                // further in, which gave it its inputs, where it isn't the server's own.
                if inputs && !own {
                    self.note_undefined(&tool.name);
                }
                continue;
            };
            defines = true;
            if !inputs {
                continue;
            }
            let ownership = listed_ownership(&current.input_schema);
            self.note_ownership(&tool.name, ownership);
            if ownership == Ownership::Unreadable
                || ownership != listed_ownership(&tool.input_schema)
            {
                continue;
            }
            let adding: Vec<&str> = [(options.sessions, SESSION_ID), (options.intent, INTENT)]
                .into_iter()
                .filter(|(on, name)| *on && !ownership.declares(name))
                .map(|(_, name)| name)
                .collect();
            if let Some(schema) = advertise(&tool.input_schema, &adding) {
                listed.schemas.push((index, Arc::new(schema)));
            }
        }
        // Whose the name is comes first, so a server with its own is told, with inputs off too.
        let free = more && self.name_is_free(self.registered(MISSING_TOOLS).as_ref());
        // Whether the client was shut down comes last, after the app's `inputs` function ran.
        if free && !named && defines && self.lists_need() && !client.closed() {
            listed.more = Some(missing_tools_tool(options.sessions));
        }
        listed
    }

    /// Notes the connection an `initialize` starts, with an ID of its own for as long as it is
    /// open.
    fn bind(&self, context: &RequestContext<RoleServer>) {
        if !self.shared.client.enabled() || !self.shared.options.sessions {
            return;
        }
        let mut connections = self
            .connections
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        // Those that have closed are forgotten.
        connections.retain(|connection| !connection.peer.is_transport_closed());
        // One that sends `initialize` again is the session it was.
        let known = connections
            .iter()
            .any(|connection| same_connection(&connection.peer, &context.peer));
        if !known && connections.len() < MAX_CONNECTIONS {
            connections.push(Connection {
                id: uuid::Uuid::new_v4().to_string(),
                peer: context.peer.clone(),
            });
        }
    }

    /// The session of the connection a call came on: where the protocol version has sessions, and
    /// the connection started with an `initialize` through this wrapper. Any other call has none:
    /// one to a server built for each request (a stateless HTTP server's), or from a client that
    /// never sent `initialize`.
    fn protocol_session(
        &self,
        version: Option<&ProtocolVersion>,
        peer: &Peer<RoleServer>,
    ) -> Option<String> {
        let mut connections = self
            .connections
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        // Those that have closed are forgotten here too, whatever this call's version: nothing
        // of a client is kept past the next call.
        connections.retain(|connection| !connection.peer.is_transport_closed());
        if version.is_some_and(|version| sessionless(version.as_str())) {
            return None;
        }
        connections
            .iter()
            .find(|connection| same_connection(&connection.peer, peer))
            .map(|connection| connection.id.clone())
    }

    /// Notes the description of each tool a page lists.
    fn note(&self, listed: &ListToolsResult) {
        if matches!(self.shared.options.descriptions, Choice::None) || !self.shared.client.enabled()
        {
            return;
        }
        let mut described = self
            .shared
            .listed
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        for tool in &listed.tools {
            match tool
                .description
                .as_deref()
                .filter(|description| !description.is_empty())
            {
                None => {
                    described.remove(tool.name.as_ref());
                }
                Some(description) => {
                    if described.len() < MAX_LISTED || described.contains_key(tool.name.as_ref()) {
                        described.insert(tool.name.to_string(), description.to_owned());
                    }
                }
            }
        }
    }

    /// Reads the call as it arrives, its arguments where they are, and what `identify` will be
    /// given.
    fn start(
        &self,
        request: &CallToolRequestParams,
        context: &RequestContext<RoleServer>,
        arrival: Arrival,
    ) -> Call {
        let Arrival {
            looked,
            mut extra,
            protocol_session,
            inputs,
            answer,
        } = arrival;
        let options = &self.shared.options;
        let tool_name = request.name.to_string();
        let inputs = inputs.unwrap_or_else(|| self.chooses(&options.inputs, "inputs", &tool_name));
        let results = self.chooses(&options.results, "results", &tool_name);
        // The wrapper's own tool has none: its text is LSquare's, the same on every server.
        let description =
            if !answer && self.chooses(&options.descriptions, "descriptions", &tool_name) {
                let registered = looked.unwrap_or_else(|| self.registered(&tool_name));
                self.description(&tool_name, registered.as_ref())
                    .map(Value::String)
            } else {
                None
            };
        let mut shaper = CallShaper::new(self.shared.client.context());
        // The intent first, whether or not arguments are recorded. As it came, it is let go here:
        // only what was read of it waits for the tool.
        if let Some(intent) = extra.intent.take() {
            shaper.intent(&intent);
        }
        if inputs {
            shaper.arguments(request.arguments.as_ref());
        }
        let cancellation = context.ct.clone();
        let identification = options.identify.as_ref().map(|_| {
            Identification::Ready(Box::new(IdentifyInput {
                tool_name: tool_name.clone(),
                // Its own copy of the arguments: the tool takes the call's.
                arguments: Arc::new(request.arguments.clone().map_or(Value::Null, Value::Object)),
                context: context.clone(),
            }))
        });
        let protocol_version = context.protocol_version();
        Call {
            call_id: uuid::Uuid::new_v4().to_string(),
            captured_at: iso(now_ms()),
            started: Instant::now(),
            tool_name,
            inputs,
            results,
            description,
            shaper,
            continued: request.input_responses.is_some() || request.request_state.is_some(),
            supports_tasks: context
                .client_capabilities()
                .is_some_and(|capabilities| capabilities.supports_tasks()),
            input_rounds: protocol_version
                .as_ref()
                .is_some_and(|version| version.as_str() >= ProtocolVersion::V_2026_07_28.as_str()),
            client: calling_app(context),
            protocol_version: protocol_version.map(|version| Value::String(version.to_string())),
            cancelled: Box::new(move || cancellation.is_cancelled()),
            identification,
            extra,
            protocol_session,
            answer,
        }
    }

    /// Starts `identify` alongside the tool, which has just been polled for the first time: a
    /// wrapper further in has taken the call by then, and this one leaves it its `identify` too.
    fn identify(&self, call: &mut Call, taken: &Taken) {
        if taken.further_in() {
            return;
        }
        let (Some(identify), Some(Identification::Ready(input))) =
            (&self.shared.options.identify, call.identification.take())
        else {
            return;
        };
        let client = &self.shared.client;
        // Nowhere to run it: the call is sent as limited diagnostics.
        let Ok(runtime) = Handle::try_current() else {
            call.identification = Some(Identification::Ready(input));
            return;
        };
        if client.identifying.fetch_add(1, Ordering::AcqRel) >= client.max_concurrent_hooks() {
            client.identifying.fetch_sub(1, Ordering::AcqRel);
            call.identification = Some(Identification::Skipped);
            return;
        }
        let slot = HookSlot(client.clone());
        let identify = identify.clone();
        call.identification = Some(Identification::Running(Identifying(runtime.spawn(
            async move {
                let _slot = slot;
                let identity = identify(*input).await;
                (identity, Instant::now())
            },
        ))));
    }

    async fn record(
        &self,
        request: CallToolRequestParams,
        context: RequestContext<RoleServer>,
    ) -> Result<CallToolResponse, McpError> {
        let client = &self.shared.client;
        // A client that never records (it has no key) leaves the call as it is.
        if !client.enabled() {
            return self.handler.call_tool(request, context).await;
        }
        // Wrapped twice, the call is recorded once: here, further in, with these options. The
        // outer wrapper is told so even when this client has been shut down, and records nothing.
        let handed = TAKEN
            .try_with(|outer| {
                if outer.id == context.id && outer.tool_name == request.name.as_ref() {
                    outer.further_in.store(true, Ordering::Release);
                    outer.connection.clone()
                } else {
                    None
                }
            })
            .ok()
            .flatten();
        // The extras' inputs are taken out whether or not the call is recorded: a client that was
        // shut down still serves tools listed with them.
        let options = &self.shared.options;
        let mut request = request;
        // What the handler says of the tool, asked once: for the inputs, for the wrapper's own
        // tool, which is the wrapper's only while the handler has none of that name, and so the
        // client learns that this server's handlers define their tools.
        let with_inputs = options.sessions || options.intent;
        let looked = (with_inputs || options.missing_tools).then(|| {
            catch_unwind(AssertUnwindSafe(|| self.registered(&request.name)))
                .ok()
                .flatten()
        });
        // A call to the wrapper's own tool is answered here, while it is offered; one that isn't
        // goes to the handler like any other. The `inputs` option is asked of it once.
        let (answer, inputs) = if request.name == MISSING_TOOLS {
            let of_the_name = looked.as_ref().and_then(Option::as_ref);
            catch_unwind(AssertUnwindSafe(|| self.answers(of_the_name))).unwrap_or((false, None))
        } else {
            (false, None)
        };
        let mut extra = match &looked {
            Some(registered) if with_inputs && !answer => catch_unwind(AssertUnwindSafe(|| {
                self.prepare(&mut request, registered.as_ref())
            }))
            .unwrap_or_default(),
            _ => Extra::default(),
        };
        if !client.recording() {
            return self.handler.call_tool(request, context).await;
        }
        // Its own tool's `session_id` is taken out only now that the call is the wrapper's: one
        // that went to the handler above went as it came.
        if answer {
            extra = catch_unwind(AssertUnwindSafe(|| self.prepare_answer(&mut request)))
                .unwrap_or_default();
        }
        // The connection's session: the one a wrapper further out found, or else this one's own.
        // Looked up for a call a wrapper further in will record too: it is given it there.
        let connection = handed.or_else(|| {
            if !options.sessions {
                return None;
            }
            catch_unwind(AssertUnwindSafe(|| {
                self.protocol_session(context.protocol_version().as_ref(), &context.peer)
            }))
            .ok()
            .flatten()
        });
        let taken = Arc::new(Taken {
            id: context.id.clone(),
            tool_name: request.name.to_string(),
            further_in: AtomicBool::new(false),
            connection: connection.clone(),
        });
        // Whatever fails in here, the call goes on as it would without it.
        let arrival = Arrival {
            protocol_session: connection.filter(|_| extra.grouping),
            looked,
            extra,
            inputs,
            answer,
        };
        let call = catch_unwind(AssertUnwindSafe(|| self.start(&request, &context, arrival))).ok();
        // Should the MCP SDK let go of the call before the handler answers (its connection
        // closed), it is recorded as it goes.
        let mut unanswered = Unanswered {
            wrapper: self,
            call,
            taken: taken.clone(),
        };
        let identify = |unanswered: &mut Unanswered<'_, H>| {
            if let Some(call) = &mut unanswered.call {
                let _ = catch_unwind(AssertUnwindSafe(|| self.identify(call, &taken)));
            }
        };
        let outcome = if answer {
            // The wrapper's own tool: the handler isn't called, and `identify` starts as it
            // would with a tool that ran.
            identify(&mut unanswered);
            let reply = missing_tools_result(request.arguments.as_ref());
            Ok(Ok(CallToolResponse::Complete(reply)))
        } else {
            // A handler can panic making its future, before it is ever polled.
            match catch_unwind(AssertUnwindSafe(|| {
                self.handler.call_tool(request, context)
            })) {
                Ok(answering) => {
                    let mut answering =
                        CatchUnwind(Box::pin(TAKEN.scope(taken.clone(), answering)));
                    // `identify` starts once the handler was polled for the first time: never
                    // before the tool, and not at all when a wrapper further in took the call.
                    // The handler is polled when this is, no more: what it does, it does as it
                    // would without the wrapper.
                    let mut started = false;
                    std::future::poll_fn(|context| {
                        let answer = Pin::new(&mut answering).poll(context);
                        if !std::mem::replace(&mut started, true) {
                            identify(&mut unanswered);
                        }
                        answer
                    })
                    .await
                }
                Err(panic) => {
                    identify(&mut unanswered);
                    Err(panic)
                }
            }
        };
        // A new session's ID, to tell the agent.
        let mut issued = None;
        if let Some(call) = unanswered.call.take() {
            if taken.further_in() {
                drop(call);
            } else {
                match catch_unwind(AssertUnwindSafe(|| self.finish(call, Some(&outcome)))) {
                    Ok(id) => issued = id,
                    Err(_) => client.report(Error::new(
                        ErrorCode::InternalError,
                        "Recording a tool call failed unexpectedly; its event was dropped.",
                    )),
                }
            }
        }
        match outcome {
            // At the end of the tool's own result, which was read for the event already. Not
            // once the client was shut down, whenever that was since the call came: it records
            // none of the calls that would follow.
            Ok(Ok(CallToolResponse::Complete(mut result))) => {
                if let Some(id) = issued.filter(|_| client.recording()) {
                    result.content.push(ContentBlock::text(id_block(&id)));
                }
                Ok(CallToolResponse::Complete(result))
            }
            Ok(answer) => answer,
            Err(panic) => resume_unwind(panic),
        }
    }

    /// Reads how the call ended, shapes its data while the result is still here, and leaves the
    /// rest (waiting for `identify`, redacting, queueing) to a task of its own, so the reply isn't
    /// held back. Nothing of the app's runs in here.
    ///
    /// `outcome` is the handler's answer, or its panic; `None` when the call was let go before
    /// either, which is recorded as a cancelled one.
    ///
    /// Returns the ID of the session it started for the call, if it did: the reply tells the
    /// agent.
    fn finish(&self, call: Call, outcome: Option<&Outcome>) -> Option<String> {
        let client = &self.shared.client;
        let finished = Instant::now();
        // A failed call's text can quote what the tool would have returned, and what it was sent:
        // it is kept only when both are recorded.
        let error_text = call.inputs && call.results;
        let mut status = "completed";
        let mut failure: Option<Map<String, Value>> = None;
        let mut task: Option<Value> = None;
        let mut result: Option<ResultIn> = None;
        let empty = Value::Object(Map::new());
        let mut unknown: Vec<Option<Value>> = Vec::new();
        let round = match outcome {
            Some(Ok(Ok(CallToolResponse::InputRequired(_)))) => Some("input_required"),
            _ if call.continued => Some("continuation"),
            _ => None,
        };
        let protocol = |error: &McpError| {
            let mut failure = failed("protocol");
            if error_text {
                failure.insert("message".into(), error.message.to_string().into());
            }
            failure.insert("type".into(), "ErrorData".into());
            failure.insert("code".into(), error.code.0.into());
            failure
        };
        let answered = outcome.filter(|_| !(call.cancelled)());
        if let Some(outcome) = answered {
            match outcome {
                Err(panic) => {
                    status = "error";
                    let mut thrown = failed("thrown");
                    if error_text {
                        thrown.insert("message".into(), panic_text(panic.as_ref()).into());
                    }
                    thrown.insert("type".into(), "panic".into());
                    failure = Some(thrown);
                }
                Ok(Err(error)) => {
                    status = "error";
                    failure = Some(protocol(error));
                }
                // The MCP SDK answers these two with an error of its own: the client didn't say it
                // can take a task, or its protocol version has no input rounds.
                Ok(Ok(CallToolResponse::Task(_))) if !call.supports_tasks => {
                    status = "error";
                    failure = Some(protocol(&McpError::missing_required_client_capability(
                        ClientCapabilities::builder().enable_tasks().build(),
                    )));
                }
                Ok(Ok(CallToolResponse::InputRequired(_))) if !call.input_rounds => {
                    status = "error";
                    failure = Some(protocol(&McpError::invalid_request("InputRequiredResult requires negotiated protocol version 2026-07-28 or newer", None)));
                }
                Ok(Ok(CallToolResponse::Task(created))) => {
                    let mut fields = Map::new();
                    fields.insert("id".into(), created.task.task_id.clone().into());
                    fields.insert(
                        "status".into(),
                        serde_json::to_value(created.task.status).unwrap_or(Value::Null),
                    );
                    task = Some(Value::Object(fields));
                }
                Ok(Ok(CallToolResponse::Complete(complete))) => {
                    let is_error = complete.is_error == Some(true);
                    if is_error {
                        status = "error";
                        // The wrapper's own tool refuses arguments as the MCP SDKs do.
                        let mut returned = failed(if call.answer {
                            "protocol"
                        } else {
                            "tool_error"
                        });
                        if error_text {
                            if let Some(text) = first_text(complete) {
                                returned.insert("message".into(), text.into());
                            }
                        }
                        failure = Some(returned);
                    }
                    if call.results && (!is_error || error_text) {
                        result = Some(typed(complete, &mut unknown));
                    }
                }
                Ok(Ok(CallToolResponse::InputRequired(_))) => {
                    if call.results {
                        result = Some(ResultIn::Json(&empty));
                    }
                }
                // A kind of answer this version doesn't know: recorded as completed, without it.
                #[allow(unreachable_patterns)]
                Ok(Ok(_)) => {}
            }
        } else {
            status = "error";
            let mut cancelled = failed("timeout");
            cancelled.insert(
                "message".into(),
                "The request was cancelled before the tool finished.".into(),
            );
            failure = Some(cancelled);
        }
        let round = if status == "error" && round == Some("input_required") {
            None
        } else {
            round
        };
        // The call's session: the ID the agent sent; else, where the protocol has sessions, the
        // connection's; else a new one, for a tool that leaves `session_id` to the wrapper, told
        // at the end of a result that is the tool's own: a completed one. Not a task, a round
        // that asks for input, or an error, which the MCP SDK can have made without the tool. Nor
        // a result with nothing in it: a client that reads its first block as the tool's value
        // (rmcp's `into_typed`) would read the ID.
        let own_result = matches!(
            answered,
            Some(Ok(Ok(CallToolResponse::Complete(complete))))
                if complete.is_error != Some(true)
                    && !(complete.content.is_empty() && complete.structured_content.is_none())
        );
        let mut issued = None;
        let session = if !call.extra.grouping {
            None
        } else if let Some(sent) = call.extra.sent {
            Some((sent, "sent"))
        } else if let Some(connection) = call.protocol_session {
            Some((session_hash(&connection), "protocol"))
        } else if call.extra.sessions && own_result {
            let id = new_session_id();
            issued = Some(id.clone());
            Some((id, "issued"))
        } else {
            None
        };
        let failure = failure.map(Value::Object);
        let data = CallData {
            tool_description: call.description.as_ref(),
            // The server's, whichever tool is called.
            server_instructions: self.shared.instructions.as_ref(),
            input: None,
            result,
            error: failure.as_ref(),
            task: task.as_ref(),
            intent: None,
            agent: None,
        };
        let shaped = call.shaper.finish(&data);
        // Registered before the reply leaves, so a flush from here on waits for this event.
        let Some(building) = client.admit(&call.call_id, shaped.held_bytes()) else {
            // An event dropped for want of room still leaves the calls that follow their ID.
            return issued;
        };
        let (session_id, session_source) = session.unzip();
        let head = CallHead {
            call_id: call.call_id,
            captured_at: call.captured_at,
            tool_name: call.tool_name,
            status: status.into(),
            duration_ms: Value::from(
                (finished.duration_since(call.started).as_secs_f64() * 1_000.0).round() as u64,
            ),
            ids: None,
            turn_ref: None,
            server: self.shared.server.clone(),
            client: call.client,
            protocol_version: call.protocol_version,
            session_id: session_id.map(Value::String),
            session_source: session_source.map(str::to_owned),
            round: round.map(str::to_owned),
            extra: call.answer.then(|| "missing_tools".to_owned()),
            metadata: None,
            diagnostics: false,
        };
        let identification = call.identification;
        let client = client.clone();
        let rest = async move {
            let mut head = head;
            let diagnostic = match identified(identification, finished + IDENTIFY_WAIT).await {
                Ok(None) => None,
                Ok(Some(identity)) => {
                    let mut ids = Map::new();
                    for (key, id) in [
                        ("orgId", identity.org_id),
                        ("userId", identity.user_id),
                        ("conversationId", identity.conversation_id),
                    ] {
                        if let Some(id) = id {
                            ids.insert(key.into(), id.into());
                        }
                    }
                    head.ids = Some(Value::Object(ids));
                    head.metadata = identity.metadata.map(Value::Object);
                    None
                }
                Err(why) => {
                    head.diagnostics = true;
                    Some(Error::new(
                        ErrorCode::IdentifyFailed,
                        format!("identify {why}; the call was sent as limited diagnostics."),
                    ))
                }
            };
            client.record(building, shaped, head, diagnostic).await;
        };
        match Handle::try_current() {
            Ok(runtime) => {
                runtime.spawn(rest);
            }
            // Nowhere to finish it: the event is dropped, and counts as failed.
            Err(_) => drop(rest),
        }
        issued
    }
}

/// What `identify` gave, waited for until `deadline`: its IDs, none when there is no `identify`,
/// or why it failed. One still running at the deadline is stopped, and one that answered past it
/// is late, whenever its answer is seen.
async fn identified(
    identification: Option<Identification>,
    deadline: Instant,
) -> Result<Option<Identity>, String> {
    let mut running = match identification {
        None => return Ok(None),
        Some(Identification::Ready(_)) => return Err("could not be started".into()),
        Some(Identification::Skipped) => return Err("was skipped at the hook limit".into()),
        Some(Identification::Running(running)) => running,
    };
    let late = || Err("took longer than its deadline".to_owned());
    let identity = match tokio::time::timeout_at(deadline.into(), &mut running.0).await {
        Err(_) => return late(),
        Ok(Err(stopped)) => {
            return Err(if stopped.is_panic() {
                "panicked"
            } else {
                "was stopped"
            }
            .into())
        }
        Ok(Ok((_, answered))) if answered > deadline => return late(),
        Ok(Ok((Err(_), _))) => return Err("failed".into()),
        Ok(Ok((Ok(identity), _))) => identity,
    };
    let mut checked = identity;
    for (name, id) in [
        ("org_id", &mut checked.org_id),
        ("user_id", &mut checked.user_id),
        ("conversation_id", &mut checked.conversation_id),
    ] {
        // An empty ID is none.
        if id.as_deref() == Some("") {
            *id = None;
        }
        if id.as_deref().is_some_and(|id| !is_valid_id(id)) {
            return Err(format!("returned an invalid {name}"));
        }
    }
    Ok(Some(checked))
}

/// An isError result's message: its first text block.
fn first_text(result: &CallToolResult) -> Option<&str> {
    result
        .content
        .iter()
        .take(1_000)
        .find_map(|block| match block {
            ContentBlock::Text(text) => Some(text.text.as_str()),
            _ => None,
        })
}

/// The MCP SDK's result, read where it is: its text and files are borrowed, not copied. A kind of
/// block this version doesn't know is read from its JSON, kept in `unknown`.
fn typed<'a>(result: &'a CallToolResult, unknown: &'a mut Vec<Option<Value>>) -> ResultIn<'a> {
    let first = || result.content.iter().take(MAX_ENTRIES);
    *unknown = first()
        .map(|block| match block {
            ContentBlock::Text(_)
            | ContentBlock::Image(_)
            | ContentBlock::Audio(_)
            | ContentBlock::Resource(_)
            | ContentBlock::ResourceLink(_) => None,
            #[allow(unreachable_patterns)]
            other => serde_json::to_value(other).ok(),
        })
        .collect();
    let unknown: &'a [Option<Value>] = unknown;
    let blocks = first()
        .zip(unknown)
        .map(|(block, json)| match block {
            ContentBlock::Text(text) => BlockIn {
                kind: Some("text"),
                text: Some(&text.text),
                ..BlockIn::default()
            },
            ContentBlock::Image(image) => BlockIn {
                kind: Some("image"),
                data: Some(&image.data),
                mime_type: Some(&image.mime_type),
                ..BlockIn::default()
            },
            ContentBlock::Audio(audio) => BlockIn {
                kind: Some("audio"),
                data: Some(&audio.data),
                mime_type: Some(&audio.mime_type),
                ..BlockIn::default()
            },
            ContentBlock::Resource(embedded) => {
                let resource = match &embedded.resource {
                    ResourceContents::TextResourceContents {
                        uri,
                        mime_type,
                        text,
                        ..
                    } => ResourceIn {
                        uri: Some(uri),
                        mime_type: mime_type.as_deref(),
                        text: Some(text),
                        blob: None,
                    },
                    ResourceContents::BlobResourceContents {
                        uri,
                        mime_type,
                        blob,
                        ..
                    } => ResourceIn {
                        uri: Some(uri),
                        mime_type: mime_type.as_deref(),
                        text: None,
                        blob: Some(blob),
                    },
                    #[allow(unreachable_patterns)]
                    _ => ResourceIn::default(),
                };
                BlockIn {
                    kind: Some("resource"),
                    resource: Some(resource),
                    ..BlockIn::default()
                }
            }
            ContentBlock::ResourceLink(link) => BlockIn {
                kind: Some("resource_link"),
                uri: Some(&link.uri),
                name: Some(&link.name),
                mime_type: link.mime_type.as_deref(),
                ..BlockIn::default()
            },
            #[allow(unreachable_patterns)]
            _ => json
                .as_ref()
                .and_then(BlockIn::from_json)
                .unwrap_or_default(),
        })
        .collect();
    ResultIn::Typed(TypedResult {
        blocks,
        total: result.content.len(),
        structured: result.structured_content.as_ref(),
        is_error: result.is_error,
    })
}

// Every method of rmcp's `ServerHandler`, passed on to the handler: a method left out would answer
// with rmcp's default in place of the handler's own. `tests/forwarding.rs` checks the list against
// the version of rmcp in use.
#[allow(deprecated)]
impl<H: ServerHandler> ServerHandler for Instrumented<H> {
    fn ping(
        &self,
        context: RequestContext<RoleServer>,
    ) -> impl Future<Output = Result<(), McpError>> + MaybeSendFuture + '_ {
        self.handler.ping(context)
    }

    fn initialize(
        &self,
        request: InitializeRequestParams,
        context: RequestContext<RoleServer>,
    ) -> impl Future<Output = Result<InitializeResult, McpError>> + MaybeSendFuture + '_ {
        // A connection starts here.
        let _ = catch_unwind(AssertUnwindSafe(|| self.bind(&context)));
        self.handler.initialize(request, context)
    }

    fn negotiate_initialize(
        &self,
        request: &InitializeRequestParams,
    ) -> Result<InitializeResult, McpError> {
        self.handler.negotiate_initialize(request)
    }

    fn supported_protocol_versions(&self) -> Cow<'static, [ProtocolVersion]> {
        self.handler.supported_protocol_versions()
    }

    fn discover(
        &self,
        context: RequestContext<RoleServer>,
    ) -> impl Future<Output = Result<DiscoverResult, McpError>> + MaybeSendFuture + '_ {
        self.handler.discover(context)
    }

    fn complete(
        &self,
        request: CompleteRequestParams,
        context: RequestContext<RoleServer>,
    ) -> impl Future<Output = Result<CompleteResult, McpError>> + MaybeSendFuture + '_ {
        self.handler.complete(request, context)
    }

    fn set_level(
        &self,
        request: SetLevelRequestParams,
        context: RequestContext<RoleServer>,
    ) -> impl Future<Output = Result<(), McpError>> + MaybeSendFuture + '_ {
        self.handler.set_level(request, context)
    }

    fn get_prompt(
        &self,
        request: GetPromptRequestParams,
        context: RequestContext<RoleServer>,
    ) -> impl Future<Output = Result<GetPromptResponse, McpError>> + MaybeSendFuture + '_ {
        self.handler.get_prompt(request, context)
    }

    fn list_prompts(
        &self,
        request: Option<PaginatedRequestParams>,
        context: RequestContext<RoleServer>,
    ) -> impl Future<Output = Result<ListPromptsResult, McpError>> + MaybeSendFuture + '_ {
        self.handler.list_prompts(request, context)
    }

    fn list_resources(
        &self,
        request: Option<PaginatedRequestParams>,
        context: RequestContext<RoleServer>,
    ) -> impl Future<Output = Result<ListResourcesResult, McpError>> + MaybeSendFuture + '_ {
        self.handler.list_resources(request, context)
    }

    fn list_resource_templates(
        &self,
        request: Option<PaginatedRequestParams>,
        context: RequestContext<RoleServer>,
    ) -> impl Future<Output = Result<ListResourceTemplatesResult, McpError>> + MaybeSendFuture + '_
    {
        self.handler.list_resource_templates(request, context)
    }

    fn read_resource(
        &self,
        request: ReadResourceRequestParams,
        context: RequestContext<RoleServer>,
    ) -> impl Future<Output = Result<ReadResourceResponse, McpError>> + MaybeSendFuture + '_ {
        self.handler.read_resource(request, context)
    }

    fn accepted_subscription_filter(
        &self,
        requested: &SubscriptionFilter,
    ) -> Option<SubscriptionFilter> {
        self.handler.accepted_subscription_filter(requested)
    }

    fn listen(
        &self,
        context: SubscriptionContext,
    ) -> impl Future<Output = Result<(), McpError>> + MaybeSendFuture + '_ {
        self.handler.listen(context)
    }

    fn subscribe(
        &self,
        request: SubscribeRequestParams,
        context: RequestContext<RoleServer>,
    ) -> impl Future<Output = Result<(), McpError>> + MaybeSendFuture + '_ {
        self.handler.subscribe(request, context)
    }

    fn unsubscribe(
        &self,
        request: UnsubscribeRequestParams,
        context: RequestContext<RoleServer>,
    ) -> impl Future<Output = Result<(), McpError>> + MaybeSendFuture + '_ {
        self.handler.unsubscribe(request, context)
    }

    fn call_tool(
        &self,
        request: CallToolRequestParams,
        context: RequestContext<RoleServer>,
    ) -> impl Future<Output = Result<CallToolResponse, McpError>> + MaybeSendFuture + '_ {
        self.record(request, context)
    }

    async fn list_tools(
        &self,
        request: Option<PaginatedRequestParams>,
        context: RequestContext<RoleServer>,
    ) -> Result<ListToolsResult, McpError> {
        let mut listed = self.handler.list_tools(request, context).await;
        if let Ok(page) = &mut listed {
            // Unnoted, these tools' calls carry the description last noted, or none.
            let _ = catch_unwind(AssertUnwindSafe(|| self.note(page)));
            // A page that can't be read is listed as it came.
            let listed =
                catch_unwind(AssertUnwindSafe(|| self.advertised(page))).unwrap_or_default();
            for (index, schema) in listed.schemas {
                if let Some(tool) = page.tools.get_mut(index) {
                    tool.input_schema = schema;
                }
            }
            page.tools.extend(listed.more);
        }
        listed
    }

    /// The handler's own definition, without the inputs the wrapper adds to the listing.
    fn get_tool(&self, name: &str) -> Option<Tool> {
        let tool = self.handler.get_tool(name);
        // A wrapper further out, asking through here, learns that this one has the tool and
        // records its calls. One it doesn't have may be the asker's own: a router that asks here
        // first, then looks among its own tools.
        if tool.is_some() && self.shared.client.enabled() {
            let _ = FURTHER_IN.try_with(|raised| raised.set(true));
        }
        tool
    }

    fn on_custom_request(
        &self,
        request: CustomRequest,
        context: RequestContext<RoleServer>,
    ) -> impl Future<Output = Result<CustomResult, McpError>> + MaybeSendFuture + '_ {
        self.handler.on_custom_request(request, context)
    }

    fn on_cancelled(
        &self,
        notification: CancelledNotificationParam,
        context: NotificationContext<RoleServer>,
    ) -> impl Future<Output = ()> + MaybeSendFuture + '_ {
        self.handler.on_cancelled(notification, context)
    }

    fn on_progress(
        &self,
        notification: ProgressNotificationParam,
        context: NotificationContext<RoleServer>,
    ) -> impl Future<Output = ()> + MaybeSendFuture + '_ {
        self.handler.on_progress(notification, context)
    }

    fn on_initialized(
        &self,
        context: NotificationContext<RoleServer>,
    ) -> impl Future<Output = ()> + MaybeSendFuture + '_ {
        self.handler.on_initialized(context)
    }

    fn on_roots_list_changed(
        &self,
        context: NotificationContext<RoleServer>,
    ) -> impl Future<Output = ()> + MaybeSendFuture + '_ {
        self.handler.on_roots_list_changed(context)
    }

    fn on_custom_notification(
        &self,
        notification: CustomNotification,
        context: NotificationContext<RoleServer>,
    ) -> impl Future<Output = ()> + MaybeSendFuture + '_ {
        self.handler.on_custom_notification(notification, context)
    }

    fn get_info(&self) -> ServerConfig {
        self.handler.get_info()
    }

    fn get_task(
        &self,
        request: GetTaskParams,
        context: RequestContext<RoleServer>,
    ) -> impl Future<Output = Result<GetTaskResult, McpError>> + MaybeSendFuture + '_ {
        self.handler.get_task(request, context)
    }

    fn update_task(
        &self,
        request: UpdateTaskParams,
        context: RequestContext<RoleServer>,
    ) -> impl Future<Output = Result<(), McpError>> + MaybeSendFuture + '_ {
        self.handler.update_task(request, context)
    }

    fn cancel_task(
        &self,
        request: CancelTaskParams,
        context: RequestContext<RoleServer>,
    ) -> impl Future<Output = Result<(), McpError>> + MaybeSendFuture + '_ {
        self.handler.cancel_task(request, context)
    }

    fn list_skills(
        &self,
        request: Option<PaginatedRequestParams>,
        context: RequestContext<RoleServer>,
    ) -> impl Future<Output = Result<ListSkillsResult, McpError>> + MaybeSendFuture + '_ {
        self.handler.list_skills(request, context)
    }

    fn get_skill(
        &self,
        request: GetSkillRequestParams,
        context: RequestContext<RoleServer>,
    ) -> impl Future<Output = Result<GetSkillResult, McpError>> + MaybeSendFuture + '_ {
        self.handler.get_skill(request, context)
    }

    fn read_resource_directory(
        &self,
        request: ReadResourceDirectoryRequestParams,
        context: RequestContext<RoleServer>,
    ) -> impl Future<Output = Result<ReadResourceDirectoryResult, McpError>> + MaybeSendFuture + '_
    {
        self.handler.read_resource_directory(request, context)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_protocol_version_has_no_sessions_from_2026_07_28_on() {
        for version in ["2026-07-28", "2026-12-01", "2027-01-01"] {
            assert!(sessionless(version), "{version}");
        }
        // One that isn't a date counts as older, as in TypeScript.
        for version in [
            "2025-11-25",
            "2024-11-05",
            "2026-07-27",
            "draft",
            "2026-07-28x",
            "2026/07/28",
            "",
        ] {
            assert!(!sessionless(version), "{version}");
        }
        // SHA-256, in hex.
        assert_eq!(
            session_hash("abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    const TYPESCRIPT: &str = "import { readFileSync } from 'node:fs'
const { missingToolsTool, missingToolsReply } = await import(process.argv[1])
const calls = JSON.parse(readFileSync(0, 'utf8'))
process.stdout.write(JSON.stringify({
  tools: [missingToolsTool(true), missingToolsTool(false)],
  replies: calls.map((sent) => missingToolsReply(sent === null ? undefined : sent.need)),
}))
";

    /// `get_more_tools` as TypeScript's own functions give it: the tool as it is listed, and the
    /// reply to each kind of call.
    #[test]
    fn typescript_lists_and_answers_get_more_tools_the_same() {
        use crate::cases_test::{contract_dir, file_url, node, skip_without_node};
        let source = std::env::var_os("LSQUARE_TYPESCRIPT_SRC")
            .map_or_else(
                || contract_dir().join("../lsquare/src"),
                std::path::PathBuf::from,
            )
            .join("mcp/extras.ts");
        if !source.exists() {
            assert!(
                std::env::var_os("CI").is_none(),
                "the TypeScript SDK's source isn't at {}",
                source.display()
            );
            eprintln!(
                "skipped: the TypeScript SDK's source isn't at {}",
                source.display()
            );
            return;
        }
        if skip_without_node("that TypeScript lists and answers get_more_tools the same") {
            return;
        }
        // No arguments at all, then arguments with each kind of `need`.
        let calls = serde_json::json!([
            null, {}, { "need": "A refund." }, { "need": "" }, { "need": 5 }, { "need": null },
            { "need": ["a"] }, { "need": { "a": 1 } }, { "need": true }, { "other": "x" }
        ]);
        let out = node(TYPESCRIPT, &[file_url(&source)], &calls.to_string());
        let theirs: Value = serde_json::from_str(&out).expect("TypeScript's answers");
        for (sessions, tool) in [true, false]
            .into_iter()
            .zip(theirs["tools"].as_array().unwrap())
        {
            let mine = serde_json::to_value(missing_tools_tool(sessions)).unwrap();
            // Character for character: the order of the fields is what agents are shown.
            assert_eq!(mine.to_string(), tool.to_string(), "sessions: {sessions}");
        }
        let replies = theirs["replies"].as_array().unwrap();
        assert_eq!(replies.len(), calls.as_array().unwrap().len());
        for (sent, theirs) in calls.as_array().unwrap().iter().zip(replies) {
            let mine = serde_json::to_value(missing_tools_result(sent.as_object())).unwrap();
            // What the reply says: rmcp adds `resultType` for the protocol versions that have it.
            assert_eq!(mine["content"], theirs["content"], "{sent}");
            assert_eq!(mine.get("isError"), theirs.get("isError"), "{sent}");
        }
    }
}
