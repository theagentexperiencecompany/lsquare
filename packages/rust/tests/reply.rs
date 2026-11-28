//! A reply that waits for its call's event, end to end: `flush_before_reply`, `flush()` inside a
//! tool, and a call's sending handed to the host (`wait_until`). The ingestion holds its answers
//! until the test lets them go, so what the reply waits for is seen.

mod common;

use std::future::Future;
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use common::{client, connect, ingestion_says, until, Ingestion, Reply};
use lsquare::mcp::{instrument, Identity, InstrumentOptions};
use lsquare::{Client, FlushResult, Turn};
use rmcp::model::{
    CallToolRequestParams, CallToolResponse, CallToolResult, ContentBlock, ServerCapabilities,
    ServerConfig,
};
use rmcp::service::{RequestContext, RoleClient, RoleServer, RunningService};
use rmcp::{ErrorData as McpError, ServerHandler};
use serde_json::{json, Map, Value};
use tokio::sync::Notify;
use tokio::task::JoinHandle;

type Running = Pin<Box<dyn Future<Output = CallToolResult> + Send>>;
type Run = Arc<dyn Fn(String, Map<String, Value>) -> Running + Send + Sync>;

/// A server whose every tool is one function of the test's.
#[derive(Clone)]
struct Agent {
    run: Run,
}

fn agent<F, Fut>(run: F) -> Agent
where
    F: Fn(String, Map<String, Value>) -> Fut + Send + Sync + 'static,
    Fut: Future<Output = CallToolResult> + Send + 'static,
{
    Agent {
        run: Arc::new(move |tool, arguments| Box::pin(run(tool, arguments))),
    }
}

/// A server whose tools answer at once.
fn quick() -> Agent {
    agent(|_, _| async { done() })
}

fn done() -> CallToolResult {
    CallToolResult::success(vec![ContentBlock::text("done")])
}

fn info() -> ServerConfig {
    let mut info = ServerConfig::default();
    info.server_info.name = "reply-server".into();
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
}

type Mcp = Arc<RunningService<RoleClient, common::App>>;
type Calling = JoinHandle<Result<CallToolResult, rmcp::ServiceError>>;

async fn serve<H: ServerHandler>(server: H) -> Mcp {
    Arc::new(connect(server).await)
}

/// Calls a tool on a task of its own, so the test sees whether the reply has come.
fn calling(mcp: &Mcp, tool: &'static str) -> Calling {
    let mcp = mcp.clone();
    tokio::spawn(async move { common::call(&mcp, tool, json!({ "text": "hello" })).await })
}

/// The reply, which must come now: within a few seconds.
async fn replied(calling: Calling) -> CallToolResult {
    tokio::time::timeout(Duration::from_secs(10), calling)
        .await
        .expect("the reply comes")
        .expect("the call's task ends")
        .expect("the tool answers")
}

/// An ingestion that holds every answer until the test lets one go.
async fn held() -> (Ingestion, Arc<Notify>) {
    let gate = Arc::new(Notify::new());
    let holding = gate.clone();
    let ingestion =
        Ingestion::start(move |_, received| Reply::accept(received).when(&holding)).await;
    (ingestion, gate)
}

/// Lets the ingestion answer everything from now on.
fn release(gate: &Arc<Notify>) -> JoinHandle<()> {
    let gate = gate.clone();
    tokio::spawn(async move {
        loop {
            gate.notify_one();
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    })
}

/// Ends a release: the ingestion holds its answers again, with none let go of left over.
async fn hold(releasing: JoinHandle<()>, gate: &Arc<Notify>) {
    releasing.abort();
    let _ = releasing.await;
    let _ = tokio::time::timeout(Duration::from_millis(5), gate.notified()).await;
}

/// An ingestion that answers for a call's event at once, and holds what it is sent of turns.
async fn holding_turns() -> (Ingestion, Arc<Notify>) {
    let gate = Arc::new(Notify::new());
    let holding = gate.clone();
    let ingestion = Ingestion::start(move |_, received| {
        let turn = received.events.iter().any(|event| event["kind"] == "turn");
        if turn {
            Reply::accept(received).when(&holding)
        } else {
            Reply::accept(received)
        }
    })
    .await;
    (ingestion, gate)
}

/// A while in which a reply that doesn't wait would have come.
async fn a_while() {
    tokio::time::sleep(Duration::from_millis(200)).await;
}

/// What is still on its way, without waiting for any of it.
async fn pending(lsquare: &Client) -> usize {
    lsquare.flush_within(Duration::ZERO).await.pending
}

fn waiting() -> InstrumentOptions {
    InstrumentOptions::new().flush_before_reply(true)
}

fn valid(events: &[Value]) {
    if let Some(said) = ingestion_says(events) {
        assert!(said.iter().all(|said| said == "valid"), "{said:?}");
    }
}

#[tokio::test]
async fn a_reply_waits_for_its_calls_event_to_be_sent() {
    let (ingestion, gate) = held().await;
    let (lsquare, seen) = client(&ingestion, |options| {
        options.flush_timeout(Duration::from_secs(20))
    });
    let mcp = serve(instrument(quick(), &lsquare, waiting())).await;
    let call = calling(&mcp, "ask");
    // The event is with the ingestion, which hasn't said it has it: the reply waits.
    until(|| ingestion.requests().len() == 1).await;
    a_while().await;
    assert!(!call.is_finished());
    gate.notify_one();
    let result = replied(call).await;
    assert_eq!(result.content.len(), 1);
    // Nothing is left on its way as the reply comes.
    assert_eq!(
        lsquare.flush_within(Duration::ZERO).await,
        FlushResult::default()
    );
    assert!(seen.codes().is_empty(), "{:?}", seen.codes());
    valid(&ingestion.events());
}

#[tokio::test]
async fn without_the_option_a_reply_does_not_wait() {
    let (ingestion, gate) = held().await;
    let (lsquare, _) = client(&ingestion, |options| options);
    let mcp = serve(instrument(quick(), &lsquare, InstrumentOptions::new())).await;
    replied(calling(&mcp, "ask")).await;
    assert_eq!(pending(&lsquare).await, 1);
    let releasing = release(&gate);
    assert_eq!(lsquare.flush().await.sent, 1);
    hold(releasing, &gate).await;
}

#[tokio::test]
async fn a_reply_goes_at_the_deadline_whatever_is_left() {
    let (ingestion, gate) = held().await;
    let (lsquare, _) = client(&ingestion, |options| {
        options.flush_timeout(Duration::from_millis(300))
    });
    let mcp = serve(instrument(quick(), &lsquare, waiting())).await;
    let started = Instant::now();
    replied(calling(&mcp, "ask")).await;
    let took = started.elapsed();
    assert!(
        took >= Duration::from_millis(280) && took < Duration::from_millis(1_500),
        "{took:?}"
    );
    // The event goes on by itself, and is sent if the process lives on.
    assert_eq!(pending(&lsquare).await, 1);
    let releasing = release(&gate);
    assert_eq!(lsquare.flush_within(Duration::from_secs(10)).await.sent, 1);
    hold(releasing, &gate).await;
    assert_eq!(ingestion.events().len(), 1);
}

#[tokio::test]
async fn with_no_time_to_wait_a_reply_waits_for_nothing() {
    let (ingestion, gate) = held().await;
    let (lsquare, _) = client(&ingestion, |options| options.flush_timeout(Duration::ZERO));
    let mcp = serve(instrument(quick(), &lsquare, waiting())).await;
    replied(calling(&mcp, "ask")).await;
    assert_eq!(pending(&lsquare).await, 1);
    release(&gate);
    until(|| ingestion.events().len() == 1).await;
}

#[tokio::test]
async fn a_reply_waits_for_the_turns_captured_inside_its_call() {
    let (ingestion, gate) = held().await;
    let (lsquare, _) = client(&ingestion, |options| {
        options.flush_timeout(Duration::from_secs(20))
    });
    let inside = lsquare.clone();
    let server = agent(move |_, _| {
        let lsquare = inside.clone();
        async move {
            lsquare.capture(Turn::new().answer("the call's own"));
            lsquare.capture(Turn::new().answer("a turn"));
            lsquare.capture(Turn::new().answer("another"));
            // One that is dropped as it is captured holds nothing up.
            lsquare.capture(Turn::new().org_id("\0"));
            done()
        }
    });
    let mcp = serve(instrument(server, &lsquare, waiting())).await;
    let call = calling(&mcp, "ask");
    // One request at a time: the first turn went alone, and the rest wait behind it.
    until(|| ingestion.requests().len() == 1).await;
    a_while().await;
    assert!(!call.is_finished());
    // With the first answered, the others go, and the reply still waits for them.
    gate.notify_one();
    until(|| ingestion.requests().len() == 2).await;
    a_while().await;
    assert!(!call.is_finished());
    let releasing = release(&gate);
    replied(call).await;
    hold(releasing, &gate).await;
    let events = ingestion.events();
    valid(&events);
    assert_eq!(events.len(), 3, "{events:?}");
    assert_eq!(pending(&lsquare).await, 0);
}

#[tokio::test]
async fn a_tool_that_flushes_makes_its_own_reply_wait() {
    let (ingestion, gate) = held().await;
    let (lsquare, _) = client(&ingestion, |options| {
        options.flush_timeout(Duration::from_secs(20))
    });
    let inside = lsquare.clone();
    let server = agent(move |tool, _| {
        let lsquare = inside.clone();
        async move {
            match tool.as_str() {
                // It doesn't wait on itself: its own event isn't on its way yet.
                "flushes" => {
                    lsquare.flush().await;
                }
                // Only counting what is on its way asks for nothing.
                "counts" => {
                    lsquare.flush_within(Duration::ZERO).await;
                }
                // On a task of its own it is outside the call.
                _ => {
                    let outside = lsquare.clone();
                    let brief = Duration::from_millis(50);
                    let _ = tokio::spawn(async move { outside.flush_within(brief).await }).await;
                }
            }
            done()
        }
    });
    // No option: the tool asks.
    let mcp = serve(instrument(server, &lsquare, InstrumentOptions::new())).await;
    for tool in ["counts", "spawns"] {
        replied(calling(&mcp, tool)).await;
    }
    assert_eq!(pending(&lsquare).await, 2);
    let releasing = release(&gate);
    assert_eq!(lsquare.flush().await.sent, 2);
    hold(releasing, &gate).await;
    a_while().await;
    let call = calling(&mcp, "flushes");
    until(|| ingestion.requests().len() == 3).await;
    a_while().await;
    assert!(!call.is_finished());
    let releasing = release(&gate);
    replied(call).await;
    hold(releasing, &gate).await;
    assert_eq!(pending(&lsquare).await, 0);
}

#[tokio::test]
async fn a_reply_does_not_wait_for_an_event_that_is_not_sent() {
    // In dry run nothing is sent: the event has ended once it is built.
    let seen = common::Seen::default();
    let lsquare = Client::new(
        seen.options()
            .dry_run(true)
            .flush_timeout(Duration::from_secs(20)),
    );
    let mcp = serve(instrument(quick(), &lsquare, waiting())).await;
    replied(calling(&mcp, "ask")).await;
    assert_eq!(pending(&lsquare).await, 0);
    // Its line is written by the thread the logger has, in its own time.
    until(|| {
        seen.lines()
            .iter()
            .any(|line| line.contains("\"kind\":\"tool_call\""))
    })
    .await;
}

#[tokio::test]
async fn identify_gets_as_long_as_the_reply_waits() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, seen) = client(&ingestion, |options| {
        options.flush_timeout(Duration::from_millis(150))
    });
    let options = waiting().identify(|_| async {
        tokio::time::sleep(Duration::from_secs(30)).await;
        Ok(Identity::new().user_id("user_8"))
    });
    let mcp = serve(instrument(quick(), &lsquare, options)).await;
    let started = Instant::now();
    replied(calling(&mcp, "ask")).await;
    // Its time is the reply's, not its usual second: the event is built as that runs out.
    until(|| ingestion.events().len() == 1).await;
    let took = started.elapsed();
    assert!(took < Duration::from_millis(800), "{took:?}");
    assert_eq!(ingestion.events()[0]["identity"], "failed");
    lsquare.flush().await;
    assert_eq!(
        seen.errors()[0].message,
        "identify took longer than its deadline; the call was sent as limited diagnostics."
    );
}

#[tokio::test]
async fn a_flush_from_identify_does_not_wait_for_its_own_call() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, seen) = client(&ingestion, |options| options);
    let inside = lsquare.clone();
    let options = InstrumentOptions::new().identify(move |_| {
        let lsquare = inside.clone();
        async move {
            // By now the tool has answered, and the call's event waits for this answer.
            tokio::time::sleep(Duration::from_millis(50)).await;
            lsquare.flush().await;
            Ok(Identity::new().user_id("user_8"))
        }
    });
    let mcp = serve(instrument(quick(), &lsquare, options)).await;
    replied(calling(&mcp, "ask")).await;
    until(|| ingestion.events().len() == 1).await;
    lsquare.flush().await;
    assert!(seen.codes().is_empty(), "{:?}", seen.codes());
    assert_eq!(ingestion.events()[0]["userId"], "user_8");
}

/// A handler in front of an instrumented one that says when the instrumented one has answered:
/// what the server did, whatever its client still waits for.
#[derive(Clone)]
struct Watched<H> {
    inner: H,
    answered: Arc<AtomicBool>,
}

impl<H: ServerHandler> ServerHandler for Watched<H> {
    fn get_info(&self) -> ServerConfig {
        info()
    }

    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        context: RequestContext<RoleServer>,
    ) -> Result<CallToolResponse, McpError> {
        let answer = self.inner.call_tool(request, context).await;
        self.answered.store(true, Ordering::SeqCst);
        answer
    }
}

#[tokio::test]
async fn a_request_that_is_cancelled_has_no_reply_to_hold_back() {
    let (ingestion, gate) = held().await;
    let (lsquare, _) = client(&ingestion, |options| {
        options.flush_timeout(Duration::from_secs(20))
    });
    let answered = Arc::new(AtomicBool::new(false));
    let server = Watched {
        inner: instrument(quick(), &lsquare, waiting()),
        answered: answered.clone(),
    };
    let mcp = serve(server).await;
    let call = calling(&mcp, "ask");
    until(|| ingestion.requests().len() == 1).await;
    a_while().await;
    assert!(!answered.load(Ordering::SeqCst));
    // The client gives the call up, which cancels the request: the server stops waiting, with
    // the ingestion still holding its answer.
    call.abort();
    until(|| answered.load(Ordering::SeqCst)).await;
    assert_eq!(pending(&lsquare).await, 1);
    // The event is the one the tool's answer made, sent all the same.
    let releasing = release(&gate);
    assert_eq!(lsquare.flush().await.sent, 1);
    hold(releasing, &gate).await;
    let events = ingestion.events();
    assert_eq!(events.len(), 1);
    assert_eq!(events[0]["status"], "completed");
}

#[tokio::test]
async fn an_error_waits_like_a_result_and_comes_as_it_was() {
    let (ingestion, gate) = held().await;
    let (lsquare, _) = client(&ingestion, |options| {
        options.flush_timeout(Duration::from_secs(20))
    });
    let server = agent(|_, _| async {
        CallToolResult::error(vec![ContentBlock::text("the tool's own error")])
    });
    let mcp = serve(instrument(server, &lsquare, waiting())).await;
    let call = calling(&mcp, "ask");
    until(|| ingestion.requests().len() == 1).await;
    a_while().await;
    assert!(!call.is_finished());
    gate.notify_one();
    let result = replied(call).await;
    assert_eq!(result.is_error, Some(true));
    assert_eq!(
        result.content[0].as_text().map(|text| text.text.as_str()),
        Some("the tool's own error")
    );
    assert_eq!(ingestion.events()[0]["status"], "error");
}

#[tokio::test]
async fn of_two_wrappers_the_one_that_records_the_call_decides() {
    let (ingestion, gate) = held().await;
    let (lsquare, _) = client(&ingestion, |options| {
        options.flush_timeout(Duration::from_secs(20))
    });
    // The outer one would wait, but the inner one records the call, and doesn't.
    let outer_only = instrument(
        instrument(quick(), &lsquare, InstrumentOptions::new()),
        &lsquare,
        waiting(),
    );
    let mcp = serve(outer_only).await;
    replied(calling(&mcp, "ask")).await;
    assert_eq!(pending(&lsquare).await, 1);
    let releasing = release(&gate);
    assert_eq!(lsquare.flush().await.sent, 1);
    hold(releasing, &gate).await;
    a_while().await;
    // The inner one waits, whatever the outer one says.
    let inner_only = instrument(
        instrument(quick(), &lsquare, waiting()),
        &lsquare,
        InstrumentOptions::new(),
    );
    let mcp = serve(inner_only).await;
    let call = calling(&mcp, "ask");
    until(|| ingestion.requests().len() == 2).await;
    a_while().await;
    assert!(!call.is_finished());
    let releasing = release(&gate);
    replied(call).await;
    hold(releasing, &gate).await;
}

/// A handler between two wrappers that flushes, and captures a turn, before it passes the call
/// on.
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
        match request.name.as_ref() {
            "flushes" => {
                self.lsquare.flush().await;
            }
            "captures" => {
                self.lsquare.capture(Turn::new().answer("the call's own"));
                self.lsquare
                    .capture(Turn::new().answer("a turn, before the hand-off"));
            }
            _ => {}
        }
        self.inner.call_tool(request, context).await
    }
}

#[tokio::test]
async fn a_flush_before_a_wrapper_further_in_takes_the_call_still_counts() {
    let (ingestion, gate) = held().await;
    let (lsquare, _) = client(&ingestion, |options| {
        options.flush_timeout(Duration::from_secs(20))
    });
    let between = Between {
        lsquare: lsquare.clone(),
        inner: instrument(quick(), &lsquare, InstrumentOptions::new()),
    };
    let mcp = serve(instrument(between, &lsquare, InstrumentOptions::new())).await;
    // Neither wrapper is told to wait, and without a flush neither does.
    replied(calling(&mcp, "ask")).await;
    assert_eq!(pending(&lsquare).await, 1);
    let releasing = release(&gate);
    assert_eq!(lsquare.flush().await.sent, 1);
    hold(releasing, &gate).await;
    a_while().await;
    let call = calling(&mcp, "flushes");
    until(|| ingestion.requests().len() == 2).await;
    a_while().await;
    assert!(!call.is_finished());
    let releasing = release(&gate);
    replied(call).await;
    hold(releasing, &gate).await;
}

#[tokio::test]
async fn an_event_that_retries_after_the_deadline_does_not_hold_the_reply() {
    // The first answer asks for another try in a second, which is past the reply's time.
    let ingestion = Ingestion::start(|count, received| {
        if count == 0 {
            Reply::status(429).header("Retry-After", "1")
        } else {
            Reply::accept(received)
        }
    })
    .await;
    let (lsquare, _) = client(&ingestion, |options| {
        options.flush_timeout(Duration::from_millis(400))
    });
    let mcp = serve(instrument(quick(), &lsquare, waiting())).await;
    let started = Instant::now();
    replied(calling(&mcp, "ask")).await;
    let took = started.elapsed();
    assert!(took < Duration::from_millis(350), "{took:?}");
    // It is sent on that later try, if the process lives on.
    until(|| ingestion.events().len() == 2).await;
    assert_eq!(lsquare.flush().await.pending, 0);
}

type Handed = Arc<Mutex<Vec<Pin<Box<dyn Future<Output = ()> + Send>>>>>;

#[tokio::test]
async fn a_calls_sending_is_handed_to_the_host_and_the_reply_does_not_wait() {
    let (ingestion, gate) = holding_turns().await;
    let (lsquare, seen) = client(&ingestion, |options| {
        options
            .flush_timeout(Duration::from_secs(20))
            .max_concurrent_requests(4)
    });
    let handed: Handed = Arc::default();
    let host = handed.clone();
    let inside = lsquare.clone();
    let server = agent(move |_, _| {
        let lsquare = inside.clone();
        async move {
            lsquare.capture(Turn::new().answer("the call's own"));
            lsquare.capture(Turn::new().answer("a turn"));
            done()
        }
    });
    // With both, the host's is used, and that is said once.
    let options = waiting().wait_until(move |sending| host.lock().unwrap().push(sending));
    let mcp = serve(instrument(server, &lsquare, options)).await;
    replied(calling(&mcp, "ask")).await;
    // The host was handed one thing for the call, which settles once its event and the turn
    // captured inside it were sent: the ingestion has the call's event, and holds the turn.
    until(|| ingestion.requests().len() == 2).await;
    let sending = {
        let mut handed = handed.lock().unwrap();
        assert_eq!(handed.len(), 1);
        handed.remove(0)
    };
    let kept = tokio::spawn(sending);
    a_while().await;
    assert!(!kept.is_finished());
    let releasing = release(&gate);
    tokio::time::timeout(Duration::from_secs(10), kept)
        .await
        .expect("what the host was handed settles")
        .unwrap();
    hold(releasing, &gate).await;
    assert_eq!(pending(&lsquare).await, 0);
    assert_eq!(ingestion.events().len(), 2);
    assert_eq!(
        seen.lines(),
        ["[lsquare] Both wait_until and flush_before_reply are set: wait_until is used, and flush_before_reply ignored."]
    );
}

#[tokio::test]
async fn a_host_that_refuses_what_it_is_handed_changes_nothing_for_the_call() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, seen) = client(&ingestion, |options| options);
    let asked = Arc::new(AtomicUsize::new(0));
    let count = asked.clone();
    let options = InstrumentOptions::new().wait_until(move |_| {
        count.fetch_add(1, Ordering::SeqCst);
        panic!("the host has no request to keep alive");
    });
    let mcp = serve(instrument(quick(), &lsquare, options)).await;
    let result = replied(calling(&mcp, "ask")).await;
    assert_eq!(result.content.len(), 1);
    assert_eq!(lsquare.flush().await.pending, 0);
    assert_eq!(asked.load(Ordering::SeqCst), 1);
    assert_eq!(ingestion.events().len(), 1);
    assert!(seen.codes().is_empty(), "{:?}", seen.codes());
}

#[tokio::test]
async fn a_tool_that_flushes_makes_its_reply_wait_though_its_sending_is_handed_over() {
    let (ingestion, gate) = held().await;
    let (lsquare, _) = client(&ingestion, |options| {
        options.flush_timeout(Duration::from_secs(20))
    });
    let handed: Handed = Arc::default();
    let host = handed.clone();
    let inside = lsquare.clone();
    let server = agent(move |_, _| {
        let lsquare = inside.clone();
        async move {
            lsquare.flush().await;
            done()
        }
    });
    let options =
        InstrumentOptions::new().wait_until(move |sending| host.lock().unwrap().push(sending));
    let mcp = serve(instrument(server, &lsquare, options)).await;
    let call = calling(&mcp, "ask");
    until(|| ingestion.requests().len() == 1).await;
    a_while().await;
    assert!(!call.is_finished());
    assert_eq!(handed.lock().unwrap().len(), 1);
    let releasing = release(&gate);
    replied(call).await;
    hold(releasing, &gate).await;
}

#[tokio::test]
async fn a_reply_waits_for_a_turn_though_its_calls_event_was_sent() {
    let (ingestion, gate) = holding_turns().await;
    let (lsquare, _) = client(&ingestion, |options| {
        options
            .flush_timeout(Duration::from_secs(20))
            .max_concurrent_requests(4)
    });
    let inside = lsquare.clone();
    let server = agent(move |_, _| {
        let lsquare = inside.clone();
        async move {
            lsquare.capture(Turn::new().answer("the call's own"));
            lsquare.capture(Turn::new().answer("a turn"));
            // Its request is under way before the call's event exists.
            tokio::time::sleep(Duration::from_millis(100)).await;
            done()
        }
    });
    let mcp = serve(instrument(server, &lsquare, waiting())).await;
    let call = calling(&mcp, "ask");
    until(|| ingestion.requests().len() == 2).await;
    a_while().await;
    assert!(!call.is_finished());
    assert_eq!(pending(&lsquare).await, 1);
    let releasing = release(&gate);
    replied(call).await;
    hold(releasing, &gate).await;
    assert_eq!(pending(&lsquare).await, 0);
}

#[tokio::test]
async fn a_reply_waits_for_its_turns_though_its_own_event_found_no_room() {
    let (ingestion, gate) = held().await;
    let (lsquare, seen) = client(&ingestion, |options| {
        options
            .flush_timeout(Duration::from_secs(20))
            .max_queue_events(1)
    });
    let inside = lsquare.clone();
    let server = agent(move |_, _| {
        let lsquare = inside.clone();
        async move {
            lsquare.capture(Turn::new().answer("the call's own"));
            lsquare.capture(Turn::new().answer("a turn"));
            // The turn is with the ingestion by the time the tool returns: the queue's one
            // place is its own, and can't be given up.
            tokio::time::sleep(Duration::from_millis(100)).await;
            done()
        }
    });
    let mcp = serve(instrument(server, &lsquare, waiting())).await;
    let call = calling(&mcp, "ask");
    until(|| ingestion.requests().len() == 1).await;
    a_while().await;
    assert!(!call.is_finished());
    let releasing = release(&gate);
    replied(call).await;
    hold(releasing, &gate).await;
    lsquare.flush().await;
    assert_eq!(seen.codes(), ["queue_full"]);
    assert_eq!(ingestion.events().len(), 1);
    assert_eq!(ingestion.events()[0]["kind"], "turn");
}

#[tokio::test]
async fn with_no_time_to_wait_identify_keeps_its_second() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, seen) = client(&ingestion, |options| options.flush_timeout(Duration::ZERO));
    let options = waiting().identify(|_| async {
        tokio::time::sleep(Duration::from_millis(100)).await;
        Ok(Identity::new().user_id("user_8"))
    });
    let mcp = serve(instrument(quick(), &lsquare, options)).await;
    replied(calling(&mcp, "ask")).await;
    until(|| ingestion.events().len() == 1).await;
    assert_eq!(ingestion.events()[0]["userId"], "user_8");
    assert!(seen.codes().is_empty(), "{:?}", seen.codes());
}

#[tokio::test]
async fn a_reply_does_not_wait_for_on_error_to_be_told() {
    let ingestion = Ingestion::accepting().await;
    let told = Arc::new(AtomicUsize::new(0));
    let count = told.clone();
    let lsquare = Client::new(
        lsquare::Options::new()
            .api_key("lsq_test_key".to_owned())
            .endpoint(ingestion.url.clone())
            .dry_run(false)
            .flush_timeout(Duration::from_secs(20))
            .on_error(move |_, _| {
                // A handler that takes its time.
                std::thread::sleep(Duration::from_millis(1_500));
                count.fetch_add(1, Ordering::SeqCst);
            }),
    );
    let mcp = serve(instrument(quick(), &lsquare, waiting())).await;
    // Something for `on_error`, which is busy with it while the call is made.
    lsquare.capture(Turn::new().org_id("\0"));
    tokio::time::sleep(Duration::from_millis(50)).await;
    let started = Instant::now();
    replied(calling(&mcp, "ask")).await;
    let took = started.elapsed();
    assert!(took < Duration::from_millis(1_000), "{took:?}");
    assert_eq!(told.load(Ordering::SeqCst), 0);
    assert_eq!(ingestion.events().len(), 1);
}

#[tokio::test]
async fn what_a_host_is_handed_settles_once_on_error_was_told_too() {
    // The ingestion refuses the event: `on_error` has something to be told.
    let ingestion = Ingestion::start(|_, received| {
        let rejected: Vec<Value> = received
            .ids()
            .iter()
            .map(|id| json!({ "eventId": id, "reason": "invalid_field" }))
            .collect();
        Reply::json(json!({ "accepted": [], "duplicates": [], "rejected": rejected }))
    })
    .await;
    let told = Arc::new(AtomicUsize::new(0));
    let count = told.clone();
    let lsquare = Client::new(
        lsquare::Options::new()
            .api_key("lsq_test_key".to_owned())
            .endpoint(ingestion.url.clone())
            .dry_run(false)
            .on_error(move |_, _| {
                std::thread::sleep(Duration::from_millis(300));
                count.fetch_add(1, Ordering::SeqCst);
            }),
    );
    let handed: Handed = Arc::default();
    let host = handed.clone();
    let options =
        InstrumentOptions::new().wait_until(move |sending| host.lock().unwrap().push(sending));
    let mcp = serve(instrument(quick(), &lsquare, options)).await;
    replied(calling(&mcp, "ask")).await;
    let sending = handed.lock().unwrap().remove(0);
    tokio::time::timeout(Duration::from_secs(10), sending)
        .await
        .expect("what the host was handed settles");
    // The host may stop the process now: the handler has been told.
    assert_eq!(told.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn a_flush_on_another_client_before_the_call_is_taken_over_asks_nothing() {
    let (ingestion, gate) = held().await;
    let (lsquare, _) = client(&ingestion, |options| {
        options.flush_timeout(Duration::from_secs(20))
    });
    let (other, _) = client(&ingestion, |options| {
        options.flush_timeout(Duration::from_millis(50))
    });
    // The handler between the two wrappers flushes the outer one's client: the call is the
    // inner one's, on another client, whose reply that doesn't make wait.
    let between = Between {
        lsquare: other.clone(),
        inner: instrument(quick(), &lsquare, InstrumentOptions::new()),
    };
    let mcp = serve(instrument(between, &other, InstrumentOptions::new())).await;
    replied(calling(&mcp, "flushes")).await;
    assert_eq!(pending(&lsquare).await, 1);
    let releasing = release(&gate);
    assert_eq!(lsquare.flush().await.sent, 1);
    hold(releasing, &gate).await;
}

#[tokio::test]
async fn a_wait_too_long_to_count_is_a_long_one() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, seen) = client(&ingestion, |options| options.flush_timeout(Duration::MAX));
    let mcp = serve(instrument(quick(), &lsquare, waiting())).await;
    replied(calling(&mcp, "ask")).await;
    assert_eq!(pending(&lsquare).await, 0);
    assert_eq!(
        lsquare.flush_within(Duration::MAX).await,
        FlushResult::default()
    );
    assert_eq!(
        lsquare.shutdown_within(Duration::MAX).await,
        FlushResult::default()
    );
    assert!(seen.codes().is_empty(), "{:?}", seen.codes());
}

#[tokio::test]
async fn identify_is_in_time_within_the_replys_wait_and_late_past_it() {
    let ingestion = Ingestion::accepting().await;
    let (lsquare, seen) = client(&ingestion, |options| {
        options.flush_timeout(Duration::from_millis(300))
    });
    let options = waiting().identify(|input| async move {
        // Both within its usual second: only the reply's wait tells them apart.
        let takes = if input.tool_name() == "soon" { 60 } else { 650 };
        tokio::time::sleep(Duration::from_millis(takes)).await;
        Ok(Identity::new().user_id("user_8"))
    });
    let mcp = serve(instrument(quick(), &lsquare, options)).await;
    replied(calling(&mcp, "soon")).await;
    replied(calling(&mcp, "late")).await;
    until(|| ingestion.events().len() == 2).await;
    lsquare.flush().await;
    let events = ingestion.events();
    assert_eq!(events[0]["userId"], "user_8");
    assert_eq!(events[1]["identity"], "failed");
    assert_eq!(seen.codes(), ["identify_failed"]);
}

/// A server whose tools fail: one refuses the call, as the MCP SDK's errors do, one panics.
#[derive(Clone)]
struct Failing;

impl ServerHandler for Failing {
    fn get_info(&self) -> ServerConfig {
        info()
    }

    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        _context: RequestContext<RoleServer>,
    ) -> Result<CallToolResponse, McpError> {
        if request.name == "panics" {
            panic!("the tool's own panic");
        }
        Err(McpError::invalid_params("the tool's own refusal", None))
    }
}

/// A future that gives its panic as a value, to be looked at.
struct Caught<F>(Pin<Box<F>>);

impl<F: Future> Future for Caught<F> {
    type Output = Result<F::Output, String>;

    fn poll(
        mut self: Pin<&mut Self>,
        context: &mut std::task::Context<'_>,
    ) -> std::task::Poll<Self::Output> {
        let polled = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            self.0.as_mut().poll(context)
        }));
        match polled {
            Ok(std::task::Poll::Pending) => std::task::Poll::Pending,
            Ok(std::task::Poll::Ready(output)) => std::task::Poll::Ready(Ok(output)),
            Err(panic) => std::task::Poll::Ready(Err(panic
                .downcast_ref::<&str>()
                .map(|text| (*text).to_owned())
                .unwrap_or_default())),
        }
    }
}

/// A handler in front of an instrumented one that answers with how that one ended: its error's
/// message, or what it panicked with.
#[derive(Clone)]
struct Ended<H> {
    inner: H,
}

impl<H: ServerHandler> ServerHandler for Ended<H> {
    fn get_info(&self) -> ServerConfig {
        info()
    }

    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        context: RequestContext<RoleServer>,
    ) -> Result<CallToolResponse, McpError> {
        let how = match Caught(Box::pin(self.inner.call_tool(request, context))).await {
            Ok(Ok(_)) => "answered".to_owned(),
            Ok(Err(error)) => format!("refused: {}", error.message),
            Err(panic) => format!("panicked: {panic}"),
        };
        Ok(CallToolResult::success(vec![ContentBlock::text(how)]).into())
    }
}

#[tokio::test]
async fn a_tools_refusal_and_its_panic_wait_too_and_go_on_as_they_were() {
    let (ingestion, gate) = held().await;
    let (lsquare, _) = client(&ingestion, |options| {
        options.flush_timeout(Duration::from_secs(20))
    });
    let server = Ended {
        inner: instrument(Failing, &lsquare, waiting()),
    };
    let mcp = serve(server).await;
    for (tool, how, category) in [
        ("refuses", "refused: the tool's own refusal", "protocol"),
        ("panics", "panicked: the tool's own panic", "thrown"),
    ] {
        let before = ingestion.requests().len();
        let call = calling(&mcp, tool);
        until(|| ingestion.requests().len() == before + 1).await;
        a_while().await;
        assert!(!call.is_finished(), "{tool}");
        let releasing = release(&gate);
        let result = replied(call).await;
        hold(releasing, &gate).await;
        let said = result.content[0].as_text().map(|text| text.text.clone());
        assert_eq!(said.as_deref(), Some(how));
        let event = ingestion.events().pop().unwrap();
        assert_eq!(event["error"]["category"], category, "{tool}");
    }
}

#[tokio::test]
async fn a_turn_captured_before_a_wrapper_further_in_takes_the_call_is_waited_for() {
    let (ingestion, gate) = holding_turns().await;
    let (lsquare, _) = client(&ingestion, |options| {
        options
            .flush_timeout(Duration::from_secs(20))
            .max_concurrent_requests(4)
    });
    let between = Between {
        lsquare: lsquare.clone(),
        inner: instrument(quick(), &lsquare, waiting()),
    };
    let mcp = serve(instrument(between, &lsquare, InstrumentOptions::new())).await;
    let call = calling(&mcp, "captures");
    // The call's event is answered for at once; the turn the handler between the two wrappers
    // captured is held, and the reply waits for it.
    until(|| ingestion.requests().len() == 2).await;
    a_while().await;
    assert!(!call.is_finished());
    let releasing = release(&gate);
    replied(call).await;
    hold(releasing, &gate).await;
    assert_eq!(pending(&lsquare).await, 0);
    let events = ingestion.events();
    assert_eq!(events.len(), 2);
    let recorded = events
        .iter()
        .find(|event| event["kind"] == "tool_call")
        .unwrap();
    assert_eq!(recorded["agent"]["answer"], "the call's own");
}

#[tokio::test]
async fn the_wait_is_the_outermost_wrappers_and_holds_up_nothing_between_the_two() {
    let (ingestion, gate) = held().await;
    let (lsquare, _) = client(&ingestion, |options| {
        options.flush_timeout(Duration::from_secs(20))
    });
    // A handler between two wrappers that says when the inner one, which records the call and
    // is the one told to wait, has answered it.
    let answered = Arc::new(AtomicBool::new(false));
    let between = Watched {
        inner: instrument(quick(), &lsquare, waiting()),
        answered: answered.clone(),
    };
    let mcp = serve(instrument(between, &lsquare, InstrumentOptions::new())).await;
    let call = calling(&mcp, "ask");
    until(|| ingestion.requests().len() == 1).await;
    // It has the tool's answer as soon as the tool gave it: a timeout of its own, or a retry,
    // sees nothing of the wait. The reply to the client is what waits.
    until(|| answered.load(Ordering::SeqCst)).await;
    a_while().await;
    assert!(!call.is_finished());
    let releasing = release(&gate);
    replied(call).await;
    hold(releasing, &gate).await;
    assert_eq!(pending(&lsquare).await, 0);
}

/// A handler between two wrappers that gives the inner call a moment, then gives it up and
/// answers in its place.
#[derive(Clone)]
struct Impatient<H> {
    inner: H,
}

impl<H: ServerHandler> ServerHandler for Impatient<H> {
    fn get_info(&self) -> ServerConfig {
        info()
    }

    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        context: RequestContext<RoleServer>,
    ) -> Result<CallToolResponse, McpError> {
        let inner = self.inner.call_tool(request, context);
        match tokio::time::timeout(Duration::from_millis(50), inner).await {
            Ok(answer) => answer,
            Err(_) => Ok(CallToolResult::success(vec![ContentBlock::text("in its place")]).into()),
        }
    }
}

#[tokio::test]
async fn a_call_given_up_between_two_wrappers_is_still_waited_for_by_the_reply() {
    let (ingestion, gate) = held().await;
    let (lsquare, _) = client(&ingestion, |options| {
        options.flush_timeout(Duration::from_secs(20))
    });
    let slow = agent(|_, _| async {
        tokio::time::sleep(Duration::from_secs(5)).await;
        done()
    });
    let between = Impatient {
        inner: instrument(slow, &lsquare, waiting()),
    };
    let mcp = serve(instrument(between, &lsquare, InstrumentOptions::new())).await;
    let call = calling(&mcp, "ask");
    // The inner wrapper records the call as it is let go of, and the event is with the
    // ingestion: the answer given in the tool's place waits for it like the tool's own.
    until(|| ingestion.requests().len() == 1).await;
    a_while().await;
    assert!(!call.is_finished());
    let releasing = release(&gate);
    let result = replied(call).await;
    hold(releasing, &gate).await;
    assert_eq!(
        result.content[0].as_text().map(|text| text.text.as_str()),
        Some("in its place")
    );
    let events = ingestion.events();
    assert_eq!(events.len(), 1);
    assert_eq!(events[0]["error"]["category"], "timeout");
}

/// Wakes the thread that waits for a future.
struct Unpark(std::thread::Thread);

impl std::task::Wake for Unpark {
    fn wake(self: Arc<Self>) {
        self.0.unpark();
    }
}

/// Waits for a future on this thread, with no runtime at all: what a host's own executor does
/// with what it is handed.
fn block_on<F: Future>(future: F) -> F::Output {
    let mut future = Box::pin(future);
    let waker = std::task::Waker::from(Arc::new(Unpark(std::thread::current())));
    let mut context = std::task::Context::from_waker(&waker);
    loop {
        match future.as_mut().poll(&mut context) {
            std::task::Poll::Ready(output) => return output,
            std::task::Poll::Pending => std::thread::park_timeout(Duration::from_millis(20)),
        }
    }
}

#[tokio::test]
async fn what_a_host_is_handed_can_be_waited_for_without_tokio() {
    let (ingestion, gate) = held().await;
    let (lsquare, seen) = client(&ingestion, |options| options);
    let handed: Handed = Arc::default();
    let host = handed.clone();
    let options =
        InstrumentOptions::new().wait_until(move |sending| host.lock().unwrap().push(sending));
    let mcp = serve(instrument(quick(), &lsquare, options)).await;
    replied(calling(&mcp, "ask")).await;
    let sending = handed.lock().unwrap().remove(0);
    // The host waits on a thread of its own, which has no Tokio runtime and no timers.
    let waited = Arc::new(AtomicBool::new(false));
    let done = waited.clone();
    let host = std::thread::spawn(move || {
        block_on(sending);
        done.store(true, Ordering::SeqCst);
    });
    until(|| ingestion.requests().len() == 1).await;
    a_while().await;
    assert!(!waited.load(Ordering::SeqCst));
    let releasing = release(&gate);
    until(|| waited.load(Ordering::SeqCst)).await;
    hold(releasing, &gate).await;
    host.join().unwrap();
    assert_eq!(pending(&lsquare).await, 0);
    assert_eq!(ingestion.events().len(), 1);
    // Nothing of the client ran on the host's thread, which it would have said.
    assert!(seen.lines().is_empty(), "{:?}", seen.lines());
}
