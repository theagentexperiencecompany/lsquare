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

use std::any::Any;
use std::borrow::Cow;
use std::collections::HashMap;
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
    SubscribeRequestParams, SubscriptionFilter, Tool, UnsubscribeRequestParams, UpdateTaskParams,
};
use rmcp::service::{
    MaybeSendFuture, NotificationContext, RequestContext, RoleServer, SubscriptionContext,
};
use rmcp::{ErrorData as McpError, ServerHandler};
use serde_json::{Map, Value};
use tokio::runtime::Handle;
use tokio::task::JoinHandle;

use crate::client::{Client, Error, ErrorCode, Inner, Level};
use crate::event::{BlockIn, CallData, CallHead, CallShaper, ResourceIn, ResultIn, TypedResult};
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

    /// The arguments as the client sent them.
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
}

impl Default for InstrumentOptions {
    fn default() -> InstrumentOptions {
        InstrumentOptions {
            identify: None,
            inputs: Choice::All,
            results: Choice::All,
            descriptions: Choice::All,
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
}

struct Shared {
    client: Arc<Inner>,
    options: InstrumentOptions,
    /// The description of each tool this instance last listed.
    listed: Mutex<HashMap<String, String>>,
    /// What the server calls itself, read as it was instrumented.
    server: Option<Value>,
}

/// A [`ServerHandler`] whose tool calls are recorded. Made by [`instrument`]; serve it where the
/// handler was served.
pub struct Instrumented<H> {
    handler: H,
    shared: Arc<Shared>,
}

impl<H: Clone> Clone for Instrumented<H> {
    fn clone(&self) -> Instrumented<H> {
        Instrumented {
            handler: self.handler.clone(),
            shared: self.shared.clone(),
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
/// the tool, its description, its arguments, its result (text shortened, files as sizes), how it
/// ended and how long it took, the calling app and the protocol version, and the IDs `identify`
/// gives. Never panics.
///
/// A server built for each session or request (as `StreamableHttpService` builds them) is
/// instrumented each time, with the same client. A handler instrumented twice, one wrapper inside
/// the other, is recorded once, by the inner one and with its options.
pub fn instrument<H: ServerHandler>(
    handler: H,
    client: &Client,
    options: InstrumentOptions,
) -> Instrumented<H> {
    let inner = client.inner().clone();
    // Its name and version, read here once: the app's `get_info` never runs on a call's path.
    let server = if inner.enabled() {
        catch_unwind(AssertUnwindSafe(|| {
            let info = handler.get_info().server_info;
            named(&info.name, &info.version)
        }))
        .ok()
    } else {
        None
    };
    if inner.dry_run() {
        let name = server
            .as_ref()
            .and_then(|server| server.get("name").and_then(Value::as_str));
        let mut line = Map::new();
        line.insert("server".into(), name.unwrap_or("unnamed").into());
        line.insert("records".into(), "every tools/call".into());
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
            server,
        }),
    }
}

/// A call a wrapper has taken to record.
struct Taken {
    id: RequestId,
    tool_name: String,
    /// A wrapper further in, on the same handler, took the same call: it is that one's to record.
    further_in: AtomicBool,
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

    /// The tool's description as agents are listed it now: the registered tool's, or else the one
    /// this instance last listed, so a tool no page has shown yet has none. A `get_tool` that
    /// panics gives none, and the call is recorded without.
    fn description(&self, tool_name: &str) -> Option<String> {
        let registered =
            catch_unwind(AssertUnwindSafe(|| self.handler.get_tool(tool_name))).ok()?;
        match registered {
            Some(tool) => tool.description.map(Cow::into_owned),
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
    fn start(&self, request: &CallToolRequestParams, context: &RequestContext<RoleServer>) -> Call {
        let options = &self.shared.options;
        let tool_name = request.name.to_string();
        let inputs = self.chooses(&options.inputs, "inputs", &tool_name);
        let results = self.chooses(&options.results, "results", &tool_name);
        let description = if self.chooses(&options.descriptions, "descriptions", &tool_name) {
            self.description(&tool_name).map(Value::String)
        } else {
            None
        };
        let mut shaper = CallShaper::new(self.shared.client.context());
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
        let _ = TAKEN.try_with(|outer| {
            if outer.id == context.id && outer.tool_name == request.name.as_ref() {
                outer.further_in.store(true, Ordering::Release);
            }
        });
        if !client.recording() {
            return self.handler.call_tool(request, context).await;
        }
        let taken = Arc::new(Taken {
            id: context.id.clone(),
            tool_name: request.name.to_string(),
            further_in: AtomicBool::new(false),
        });
        // Whatever fails in here, the call goes on as it would without it.
        let call = catch_unwind(AssertUnwindSafe(|| self.start(&request, &context))).ok();
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
        // A handler can panic making its future, before it is ever polled.
        let outcome = match catch_unwind(AssertUnwindSafe(|| {
            self.handler.call_tool(request, context)
        })) {
            Ok(answering) => {
                let mut answering = CatchUnwind(Box::pin(TAKEN.scope(taken.clone(), answering)));
                // `identify` starts once the handler was polled for the first time: never
                // before the tool, and not at all when a wrapper further in took the call. The
                // handler is polled when this is, no more: what it does, it does as it would
                // without the wrapper.
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
        };
        if let Some(call) = unanswered.call.take() {
            if taken.further_in() {
                drop(call);
            } else if catch_unwind(AssertUnwindSafe(|| self.finish(call, Some(&outcome)))).is_err()
            {
                client.report(Error::new(
                    ErrorCode::InternalError,
                    "Recording a tool call failed unexpectedly; its event was dropped.",
                ));
            }
        }
        match outcome {
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
    fn finish(&self, call: Call, outcome: Option<&Outcome>) {
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
                        let mut returned = failed("tool_error");
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
        let failure = failure.map(Value::Object);
        let data = CallData {
            tool_description: call.description.as_ref(),
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
            return;
        };
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
            session_id: None,
            session_source: None,
            round: round.map(str::to_owned),
            extra: None,
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
        let listed = self.handler.list_tools(request, context).await;
        if let Ok(page) = &listed {
            // Unnoted, these tools' calls carry the description last noted, or none.
            let _ = catch_unwind(AssertUnwindSafe(|| self.note(page)));
        }
        listed
    }

    fn get_tool(&self, name: &str) -> Option<Tool> {
        self.handler.get_tool(name)
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
