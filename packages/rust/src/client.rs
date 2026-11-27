//! The client: it queues events, sends them to LSquare in batches, reads each event's
//! acknowledgement, and retries. A port of what `packages/lsquare/src/client.ts` does on the
//! wire, with its defaults.
//!
//! Its state is one mutex, never held across an `await` or while the app's code runs. Requests,
//! retry timers and `before_send` run as tasks spawned when there is something for them to do, on
//! the runtime of whoever gave it that; none outlives its work. A task its runtime drops gives its
//! events back to the queue. The app's own code never runs where an event is recorded or sent:
//! `before_send` has a thread of its own for each event, and `on_error` and the logger share one
//! for all they are told.
//!
//! `capture()` is in `client/capture.rs`, and what the client learns from `identify`'s answers in
//! `client/identified.rs`.

use std::collections::{BTreeMap, VecDeque};
use std::fmt;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicBool, AtomicU8, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::{Duration, Instant};

use serde_json::{Map, Value};
use tokio::runtime::Handle;
use tokio::sync::{Notify, Semaphore};

use crate::event::{after_hook, finish_call, Built, CallHead, Context, ShapedCallData};
use crate::json::stringify;
use crate::redact::{looks_like_contact, resolve_keys, Pattern, ORDER};
use crate::time::{iso, next_midnight, now_ms, parse_iso};

mod capture;
mod identified;

// The wrapper's side of both.
#[cfg(feature = "rmcp")]
pub(crate) use capture::{CallScope, Captured, CALL};
pub(crate) use identified::Identified;
#[cfg(feature = "rmcp")]
pub(crate) use identified::{both, CONVERSATION, ORG, USER};

pub(crate) const SDK_NAME: &str = "lsquare";
pub(crate) const SDK_VERSION: &str = env!("CARGO_PKG_VERSION");
/// Where events go unless the `endpoint` option or `LSQUARE_ENDPOINT` says otherwise.
pub const DEFAULT_ENDPOINT: &str = "https://ingest.lsquare.io";

pub(crate) const DEFAULT_FLUSH_TIMEOUT: Duration = Duration::from_millis(2_000);
/// At least this long, or the flush timeout when that is longer: an exiting process has time for slow requests.
pub(crate) const DEFAULT_SHUTDOWN_TIMEOUT: Duration = Duration::from_millis(5_000);
pub(crate) const DEFAULT_MAX_CONCURRENT_HOOKS: usize = 16;
pub(crate) const DEFAULT_MAX_CONCURRENT_REQUESTS: usize = 4;
pub(crate) const DEFAULT_MAX_QUEUE_EVENTS: usize = 10_000;
pub(crate) const DEFAULT_MAX_QUEUE_BYTES: usize = 10 * 1024 * 1024;
pub(crate) const DEFAULT_MAX_EVENT_BYTES: usize = 256 * 1024;
pub(crate) const DEFAULT_MAX_ATTEMPTS: u32 = 3;
pub(crate) const DEFAULT_REQUEST_TIMEOUT: Duration = Duration::from_millis(10_000);
/// The ingestion's limits for one request.
pub(crate) const MAX_BATCH_EVENTS: usize = 100;
pub(crate) const MAX_BATCH_BYTES: usize = 1024 * 1024;
/// The ingestion answers 413 to a batch whose acknowledgement could pass this.
pub(crate) const MAX_ACK_BYTES: usize = 60 * 1024;
const ACK_OVERHEAD: usize = r#"{"accepted":[],"duplicates":[],"rejected":[]}"#.len();
const BATCH_OVERHEAD: usize = r#"{"events":[]}"#.len();
/// While a daily limit holds, one batch is sent this often to check whether it was raised.
const QUOTA_PROBE: Duration = Duration::from_secs(15 * 60);
pub(crate) const RETRY_BASE_MS: u64 = 250;
pub(crate) const RETRY_CAP_MS: u64 = 5_000;
pub(crate) const RETRY_AFTER_CAP_MS: u64 = 30_000;
const ERROR_LOG_INTERVAL: Duration = Duration::from_secs(60);
/// How many reports can wait for `on_error` at once, as many as TypeScript holds callbacks: one
/// past it is dropped.
const MAX_WAITING_REPORTS: usize = 100;
/// How many servers a client remembers tool names of, how many names for each, and how long a
/// name it keeps.
const MAX_HINTED_SERVERS: usize = 32;
const MAX_TOOL_HINTS: usize = 8;
const MAX_HINT_BYTES: usize = 256;
/// How many of a server's tools a client warns of, each once.
const MAX_WARNED_TOOLS: usize = 1_000;
/// How long `flush` and `shutdown` wait, past their deadline, for `on_error` to be told of what
/// they reported themselves.
const REPORT_GRACE: Duration = Duration::from_secs(1);
pub(crate) const MAX_RESPONSE_BYTES: usize = 64 * 1024;
/// Reasons the ingestion service may give. Anything else is reported as "unspecified".
const REJECTION_REASONS: [&str; 6] = [
    "missing_field",
    "invalid_field",
    "invalid_conversation_id",
    "too_large",
    "unsupported_schema_version",
    "quota_exceeded",
];

/// An event on its way to LSquare, as [`Options::before_send`] gets it: a JSON object, with the
/// fields of a `tool_call`. The hook may change or remove what the event holds; the fields that
/// identify it (its IDs, tool, status and times) are restored afterwards, and whatever it returns
/// is shortened and redacted like the app's own data.
#[derive(Debug, Clone, PartialEq)]
pub struct Event(pub(crate) Map<String, Value>);

impl Event {
    /// `tool_call`.
    pub fn kind(&self) -> &str {
        self.0
            .get("kind")
            .and_then(Value::as_str)
            .unwrap_or_default()
    }

    /// One of its fields, such as `toolName`, `input`, `result` or `error`.
    pub fn get(&self, field: &str) -> Option<&Value> {
        self.0.get(field)
    }

    /// Changes one of its fields, or adds it.
    pub fn insert(&mut self, field: impl Into<String>, value: impl Into<Value>) -> Option<Value> {
        self.0.insert(field.into(), value.into())
    }

    /// Removes one of its fields, keeping the others in their order.
    pub fn remove(&mut self, field: &str) -> Option<Value> {
        self.0.shift_remove(field)
    }

    /// Its fields.
    pub fn as_map(&self) -> &Map<String, Value> {
        &self.0
    }

    /// Its fields, to change. `serde_json`'s own `remove` reorders an object: use `shift_remove`.
    pub fn as_map_mut(&mut self) -> &mut Map<String, Value> {
        &mut self.0
    }

    /// The event as the JSON that is sent.
    pub fn to_json(&self) -> String {
        stringify(&self.0)
    }
}

/// Why an event was dropped, failed or is still pending, or why sending is held back.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
#[non_exhaustive]
pub enum ErrorCode {
    /// An ID (`orgId`, `userId`, `conversationId`, the tool's name) that isn't a non-empty string of at most 512 bytes without NUL.
    InvalidId,
    /// The event is larger than the limit even without its largest parts.
    TooLarge,
    /// The queue is full: an event was dropped to stay within the memory limits.
    QueueFull,
    /// `before_send` panicked.
    HookFailed,
    /// `before_send` took longer than its deadline.
    HookTimeout,
    /// Building or redacting the event failed: it was dropped, never sent unredacted.
    RedactionFailed,
    /// LSquare couldn't be reached, or didn't acknowledge the event.
    SendFailed,
    /// LSquare rejected the event.
    Rejected,
    /// The project's daily limit was reached: events are dropped until `resets_at`.
    QuotaExceeded,
    /// Sending resumed after a daily limit: `count` events were dropped meanwhile.
    QuotaDropped,
    /// The event was still pending when `flush` returned.
    PendingAfterFlush,
    /// `identify` failed, took too long or returned invalid IDs: the call was sent as limited diagnostics.
    IdentifyFailed,
    /// The client was shut down before the event was sent.
    Shutdown,
    /// Something in the SDK failed unexpectedly; the event was dropped.
    InternalError,
}

impl ErrorCode {
    /// The code as the other SDKs name it, such as `queue_full`.
    pub fn as_str(self) -> &'static str {
        match self {
            ErrorCode::InvalidId => "invalid_id",
            ErrorCode::TooLarge => "too_large",
            ErrorCode::QueueFull => "queue_full",
            ErrorCode::HookFailed => "hook_failed",
            ErrorCode::HookTimeout => "hook_timeout",
            ErrorCode::RedactionFailed => "redaction_failed",
            ErrorCode::SendFailed => "send_failed",
            ErrorCode::Rejected => "rejected",
            ErrorCode::QuotaExceeded => "quota_exceeded",
            ErrorCode::QuotaDropped => "quota_dropped",
            ErrorCode::PendingAfterFlush => "pending_after_flush",
            ErrorCode::IdentifyFailed => "identify_failed",
            ErrorCode::Shutdown => "shutdown",
            ErrorCode::InternalError => "internal_error",
        }
    }
}

impl fmt::Display for ErrorCode {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.as_str())
    }
}

/// What [`Options::on_error`] is told. Nothing here is returned from the SDK's own functions: it
/// never fails a tool call, so problems are reported, not raised.
#[derive(Debug, Clone, PartialEq)]
#[non_exhaustive]
pub struct Error {
    /// What happened.
    pub code: ErrorCode,
    /// Never contains event content.
    pub message: String,
    /// The event's ID, when the error is about one event.
    pub event_id: Option<String>,
    /// The fields an `invalid_id` or `too_large` error is about.
    pub paths: Vec<String>,
    /// HTTP status, when the error comes from a response.
    pub status: Option<u16>,
    /// `quota_exceeded`: when the daily limit resets.
    pub resets_at: Option<String>,
    /// `quota_dropped`: how many events were dropped.
    pub count: Option<u64>,
}

impl Error {
    pub(crate) fn new(code: ErrorCode, message: impl Into<String>) -> Error {
        Error {
            code,
            message: message.into(),
            event_id: None,
            paths: Vec::new(),
            status: None,
            resets_at: None,
            count: None,
        }
    }

    pub(crate) fn about(mut self, event_id: &str) -> Error {
        self.event_id = Some(event_id.to_owned());
        self
    }

    fn status(mut self, status: Option<u16>) -> Error {
        self.status = status;
        self
    }
}

impl fmt::Display for Error {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}: {}", self.code, self.message)
    }
}

impl std::error::Error for Error {}

/// How serious a log line is.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Level {
    /// Dry-run lines.
    Info,
    /// Problems: a disabled client, a dropped event.
    Warn,
}

/// What `flush` and `shutdown` found of the events outstanding when they were called.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct FlushResult {
    /// Acknowledged by LSquare.
    pub sent: usize,
    /// Completed without sending: dry run, or `before_send` returned `None`.
    pub skipped: usize,
    /// Dropped: rejected, failed after its retries, or dropped for room or shutdown.
    pub failed: usize,
    /// Still on their way when the wait ended.
    pub pending: usize,
}

type BeforeSend = Arc<dyn Fn(Event) -> Option<Event> + Send + Sync>;
type OnError = Arc<dyn Fn(&Error, Option<&Event>) + Send + Sync>;
type Logger = Arc<dyn Fn(Level, &str) + Send + Sync>;

/// How a client is set up. Everything has a default; without an `api_key` (and outside dry run)
/// the client is disabled, and does nothing.
#[derive(Clone, Default)]
pub struct Options {
    api_key: Option<String>,
    endpoint: Option<String>,
    dry_run: Option<bool>,
    flush_timeout: Option<Duration>,
    before_send: Option<BeforeSend>,
    before_send_timeout: Option<Duration>,
    max_concurrent_hooks: Option<usize>,
    max_concurrent_requests: Option<usize>,
    redact_off: Vec<Pattern>,
    redact_keys: Vec<String>,
    on_error: Option<OnError>,
    logger: Option<Logger>,
    max_queue_events: Option<usize>,
    max_queue_bytes: Option<usize>,
    max_event_bytes: Option<usize>,
    max_attempts: Option<u32>,
    request_timeout: Option<Duration>,
}

impl fmt::Debug for Options {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        // Never the key.
        formatter
            .debug_struct("Options")
            .field("endpoint", &self.endpoint)
            .field("dry_run", &self.dry_run)
            .finish_non_exhaustive()
    }
}

impl Options {
    /// The defaults.
    pub fn new() -> Options {
        Options::default()
    }

    /// The LSquare project's API key. `None` leaves the client disabled, so
    /// `.api_key(std::env::var("LSQUARE_KEY").ok())` is safe where the variable isn't set.
    pub fn api_key(mut self, key: impl Into<Option<String>>) -> Options {
        self.api_key = key.into();
        self
    }

    /// Base URL of the ingestion service. Defaults to `LSQUARE_ENDPOINT`, then <https://ingest.lsquare.io>.
    pub fn endpoint(mut self, endpoint: impl Into<String>) -> Options {
        self.endpoint = Some(endpoint.into());
        self
    }

    /// Runs everything but sends nothing: one log line per event says what it would have sent.
    /// Defaults to `LSQUARE_DRY_RUN=1`.
    pub fn dry_run(mut self, dry_run: bool) -> Options {
        self.dry_run = Some(dry_run);
        self
    }

    /// How long `flush` waits by default (2 seconds). Zero: it doesn't wait. `shutdown` waits
    /// 5 seconds, or this when it is longer.
    pub fn flush_timeout(mut self, timeout: Duration) -> Options {
        self.flush_timeout = Some(timeout);
        self
    }

    /// Gets every event before it's redacted and sent: to redact more, or to drop it by returning
    /// `None`. It runs on a blocking thread, never on the tool's path; an event whose hook panics
    /// or runs past `before_send_timeout` is dropped. What it returns is shortened and redacted again.
    pub fn before_send(
        mut self,
        hook: impl Fn(Event) -> Option<Event> + Send + Sync + 'static,
    ) -> Options {
        self.before_send = Some(Arc::new(hook));
        self
    }

    /// The hook's deadline. Defaults to the flush timeout, or 2 seconds when that is zero.
    pub fn before_send_timeout(mut self, timeout: Duration) -> Options {
        self.before_send_timeout = Some(timeout);
        self
    }

    /// Hooks (`before_send`, and the wrapper's `identify`) running at once (16 by default).
    pub fn max_concurrent_hooks(mut self, count: usize) -> Options {
        self.max_concurrent_hooks = Some(count);
        self
    }

    /// Requests to LSquare in flight at once (4 by default). Other events wait, and go in batches.
    pub fn max_concurrent_requests(mut self, count: usize) -> Options {
        self.max_concurrent_requests = Some(count);
        self
    }

    /// `false` turns built-in redaction off.
    pub fn redact(mut self, redact: bool) -> Options {
        self.redact_off = if redact { Vec::new() } else { ORDER.to_vec() };
        self
    }

    /// Turns one built-in pattern off, or back on.
    pub fn redact_pattern(mut self, pattern: Pattern, redact: bool) -> Options {
        self.redact_off.retain(|off| *off != pattern);
        if !redact {
            self.redact_off.push(pattern);
        }
        self
    }

    /// More key names whose values are replaced whole, as for `password`: matched lowercased,
    /// stripped to `a-z0-9`, by their end, so `name` also covers `firstName` (and `fileName`).
    pub fn redact_keys<Name: AsRef<str>>(
        mut self,
        names: impl IntoIterator<Item = Name>,
    ) -> Options {
        let names: Vec<String> = names
            .into_iter()
            .map(|name| name.as_ref().to_owned())
            .collect();
        self.redact_keys = resolve_keys(names.iter().map(String::as_str));
        self
    }

    /// Told of every event dropped or failed, with the event when it was already redacted. Without
    /// it, problems are logged, at most one line a minute.
    ///
    /// It runs on a thread of its own, one report at a time: never on a tool's path, nor on your
    /// runtime, so a handler that takes its time holds up no reply. `flush` and `shutdown` wait
    /// for it within their own time. Up to 100 reports wait for a handler that is slow; later
    /// ones are dropped, which is logged once.
    pub fn on_error(
        mut self,
        handler: impl Fn(&Error, Option<&Event>) + Send + Sync + 'static,
    ) -> Options {
        self.on_error = Some(Arc::new(handler));
        self
    }

    /// Where the SDK's own lines go. Defaults to standard error, so a stdio server's standard
    /// output stays clean.
    pub fn logger(mut self, logger: impl Fn(Level, &str) + Send + Sync + 'static) -> Options {
        self.logger = Some(Arc::new(logger));
        self
    }

    /// Events held at once, waiting to be sent or in a request (10,000 by default). Past it, the
    /// oldest are dropped and reported as `queue_full`.
    pub fn max_queue_events(mut self, count: usize) -> Options {
        self.max_queue_events = Some(count);
        self
    }

    /// Their size together, in bytes of JSON (10 MB by default). Past it, the oldest are dropped too.
    pub fn max_queue_bytes(mut self, bytes: usize) -> Options {
        self.max_queue_bytes = Some(bytes);
        self
    }

    /// The largest event sent (256 KB, the ingestion's limit). Mostly for tests.
    pub fn max_event_bytes(mut self, bytes: usize) -> Options {
        self.max_event_bytes = Some(bytes);
        self
    }

    /// How many times an event is sent before it is given up (3). Mostly for tests.
    pub fn max_attempts(mut self, attempts: u32) -> Options {
        self.max_attempts = Some(attempts);
        self
    }

    /// How long one request may take (10 seconds). Mostly for tests.
    pub fn request_timeout(mut self, timeout: Duration) -> Options {
        self.request_timeout = Some(timeout);
        self
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Mode {
    Disabled,
    DryRun,
    Send,
}

const SENT: u8 = 1;
const SKIPPED: u8 = 2;
const FAILED: u8 = 3;

/// What `flush` follows of one event, from the call that makes it to its end.
#[derive(Debug)]
struct Tracker {
    event_id: String,
    outcome: AtomicU8,
    /// When its next attempt is due, while it waits to retry.
    retry_at: Mutex<Option<Instant>>,
}

impl Tracker {
    fn outcome(&self) -> u8 {
        self.outcome.load(Ordering::Acquire)
    }

    /// Its end, which is final: whatever comes later finds it ended. True when this was it.
    fn end(&self, outcome: u8) -> bool {
        self.outcome
            .compare_exchange(0, outcome, Ordering::AcqRel, Ordering::Acquire)
            .is_ok()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Stage {
    Queued,
    Sending,
    Retry,
}

struct Entry {
    tracker: Arc<Tracker>,
    /// The event, kept only for `on_error`: without a handler, its JSON is all that is needed.
    event: Option<Arc<Event>>,
    /// The event's JSON, as sent.
    json: Arc<str>,
    stage: Stage,
    attempts: u32,
    /// The request it is in, or the retry it waits for: an older one's end finds nothing to do.
    token: u64,
}

/// Holding back after `quota_exceeded`, until `until` (epoch ms) or a check finds the limit raised.
/// Events recorded meanwhile are dropped at once, except one every 15 minutes: `check`, sent on its
/// own to see whether the limit still holds.
struct Holdback {
    until: i64,
    next_check: Instant,
    /// The check, once it is queued.
    check: Option<u64>,
    /// A call was taken as the check, and its event is still being built.
    checking: bool,
    dropped: u64,
    id: u64,
}

#[derive(Default)]
struct State {
    entries: BTreeMap<u64, Entry>,
    /// Events ready to send, in the order they became ready.
    queued: VecDeque<u64>,
    /// What every tracked event weighs: those in `entries`, and those still being built.
    bytes: usize,
    /// Events still being built: after the tool replied, waiting for `identify` or `before_send`.
    building: Vec<Arc<Tracker>>,
    /// What those hold, of `bytes`.
    reserved: usize,
    active_requests: usize,
    seq: u64,
    tokens: u64,
    closed: bool,
    reported_closed: bool,
    holdback: Option<Holdback>,
    reporting: Reporting,
    /// An event ended since those who wait for one were last woken.
    stirred: bool,
    last_error_log: Option<Instant>,
    suppressed_errors: u64,
    warned: Vec<String>,
    /// The once-only warnings given about a server's tools, by their keys.
    warned_tools: Vec<String>,
    runtime: Option<Handle>,
}

pub(crate) struct Inner {
    mode: Mode,
    url: String,
    api_key: String,
    http: Mutex<Option<reqwest::Client>>,
    context: Context,
    before_send: Option<BeforeSend>,
    on_error: Option<OnError>,
    logger: Logger,
    flush_timeout: Duration,
    before_send_timeout: Duration,
    max_concurrent_hooks: usize,
    max_concurrent_requests: usize,
    max_queue_events: usize,
    max_queue_bytes: usize,
    max_event_bytes: usize,
    max_attempts: u32,
    request_timeout: Duration,
    state: Mutex<State>,
    /// Wakes whoever waits for events: an event ended, or one being built did.
    changed: Notify,
    /// Ends the requests and retry waits still running, at shutdown.
    stop: Notify,
    hooks: Arc<Semaphore>,
    /// How many `identify` hooks run now.
    pub(crate) identifying: std::sync::atomic::AtomicUsize,
    /// Names of tools that handlers wrapped with this client were seen to define, newest first,
    /// for each server by what it calls itself. Only names to ask about: a wrapper built for one
    /// request asks its own handler whether it defines one of them, having seen no tool itself
    /// (see `mcp.rs`).
    tool_hints: Mutex<Vec<(String, Vec<String>)>>,
    /// What `identify` last answered, on whichever server of this client.
    identified: Identified,
}

impl fmt::Debug for Inner {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        // Never the key, nor the queue's content.
        formatter
            .debug_struct("Client")
            .field("mode", &self.mode)
            .finish_non_exhaustive()
    }
}

/// Sends events to LSquare. Cheap to clone: every clone is the same client.
///
/// It never panics and never returns an error: what goes wrong is reported to
/// [`Options::on_error`], or logged. It needs a Tokio runtime to send from, the one its events are
/// recorded on. Before the process exits, `shutdown().await` sends what is still queued.
#[derive(Clone, Debug)]
pub struct Client {
    inner: Arc<Inner>,
}

/// One thing `on_error` is to be told, or the log when there is no handler.
enum Report {
    /// For `on_error`, or for the log when there is no handler.
    Error {
        error: Error,
        /// The event it is about, when it was redacted already and there was room to keep it.
        event: Option<Arc<Event>>,
        bytes: usize,
        /// For the log: how many reports weren't written since its last line.
        more: u64,
    },
    /// A line for the logger.
    Line(Level, String),
}

const BEHIND: &str = "[lsquare] on_error or the logger isn't keeping up: with 100 reports waiting, later ones are dropped.";

/// What `on_error` and the logger are still to be told. A thread of its own tells them, one
/// report at a time and never where it came up: not on a tool's path, nor on a runtime's worker,
/// where a handler that waits would hold up a reply. The thread ends when nothing waits. An event
/// shows its end at once; `flush` and `shutdown` wait for this too, within their deadline.
#[derive(Default)]
struct Reporting {
    waiting: VecDeque<Report>,
    /// What the waiting reports' events weigh: at most the queue's own limit.
    bytes: usize,
    max_bytes: usize,
    /// There is a handler: without one, reports are log lines, at most one a minute.
    handled: bool,
    /// A thread is telling them, or about to.
    running: bool,
    /// The handler has one in hand.
    telling: bool,
    /// Reports were dropped because too many waited, and that wasn't said yet.
    behind: bool,
}

impl Reporting {
    fn undelivered(&self) -> bool {
        self.telling || !self.waiting.is_empty()
    }
}

/// The thread that tells `on_error` and the logger. However it ends, the next report starts
/// another.
struct Telling {
    inner: Arc<Inner>,
    started: bool,
    /// It found nothing left to tell, and said so itself.
    done: bool,
}

impl Telling {
    fn run(mut self) {
        self.started = true;
        // Nothing captured from `on_error` or the logger is taken.
        let _telling = capture::Telling::enter();
        loop {
            let (report, behind) = {
                let mut state = self.inner.lock();
                let Some(report) = state.reporting.waiting.pop_front() else {
                    // Under the same lock as the look: a report that comes now starts a thread.
                    state.reporting.running = false;
                    self.done = true;
                    break;
                };
                if let Report::Error { bytes, .. } = &report {
                    state.reporting.bytes = state.reporting.bytes.saturating_sub(*bytes);
                }
                state.reporting.telling = true;
                // Said once, from here: where reports are dropped is no place for the logger.
                let behind = std::mem::take(&mut state.reporting.behind)
                    && !state.warned.iter().any(|warned| warned == "behind");
                if behind {
                    state.warned.push("behind".to_owned());
                }
                (report, behind)
            };
            self.inner.tell(report);
            if behind {
                self.inner.log(Level::Warn, BEHIND);
            }
            self.inner.lock().reporting.telling = false;
            self.inner.changed.notify_waiters();
        }
    }
}

impl Drop for Telling {
    fn drop(&mut self) {
        // One that said it was done leaves the rest alone: another may be telling by now. One
        // that ended some other way (it never started, or panicked) says here that none runs;
        // what waits stays, for the next thread.
        if !self.done {
            let mut state = self.inner.lock();
            state.reporting.running = false;
            state.reporting.telling = false;
        }
        if self.started {
            self.inner.changed.notify_waiters();
        }
    }
}

/// When something with `timeout` from now is due. A time too far off to count is a long way off.
fn by(timeout: Duration) -> Instant {
    let now = Instant::now();
    now.checked_add(timeout)
        .unwrap_or_else(|| now + Duration::from_secs(365 * 24 * 60 * 60))
}

/// An event once it is built and its hook has run: the event, nothing when the hook dropped it,
/// or what there is to report of one that couldn't be finished.
type Finished = Result<Option<Box<crate::event::Draft>>, Option<Box<Error>>>;

fn default_logger() -> Logger {
    Arc::new(|_, line| eprintln!("{line}"))
}

fn env(name: &str) -> Option<String> {
    std::env::var(name).ok().filter(|value| !value.is_empty())
}

/// The URL events are posted to, or what is wrong with the endpoint.
pub(crate) fn resolve_endpoint(raw: &str) -> Result<String, &'static str> {
    let url = reqwest::Url::parse(raw).map_err(|_| "The endpoint is not a valid URL")?;
    let local = matches!(url.host_str(), Some("localhost" | "127.0.0.1" | "[::1]"));
    if !(url.scheme() == "https" || (url.scheme() == "http" && local)) {
        return Err("The endpoint must use https (http only for localhost)");
    }
    Ok(format!(
        "{}{}/v1/turns",
        url.origin().ascii_serialization(),
        url.path().trim_end_matches('/')
    ))
}

/// What one event can add to an acknowledgement: its ID with the longest rejection, and a comma.
fn ack_bytes(event_id: &str) -> usize {
    r#"{"eventId":,"reason":"quota_exceeded","resetsAt":"2000-01-01T00:00:00.000Z"}"#.len()
        + stringify(event_id).len()
        + 1
}

/// A number in [0, 1), for a retry's jitter. From the standard library's own random seed, which
/// can't fail: a retry must never be what stops an event.
fn random() -> f64 {
    use std::hash::{BuildHasher, Hasher};
    let mut hasher = std::collections::hash_map::RandomState::new().build_hasher();
    hasher.write_u64(now_ms() as u64);
    (hasher.finish() >> 11) as f64 / (1u64 << 53) as f64
}

/// How long to wait before a batch's next attempt: `Retry-After` when LSquare gave one, capped, or
/// a random share of a delay that doubles with each attempt.
pub(crate) fn retry_delay(attempts: u32, retry_after_ms: Option<u64>, random: f64) -> Duration {
    let backoff = RETRY_BASE_MS
        .saturating_mul(1u64 << attempts.saturating_sub(1).min(20))
        .min(RETRY_CAP_MS);
    Duration::from_millis(match retry_after_ms {
        Some(after) => after.min(RETRY_AFTER_CAP_MS),
        None => (random * backoff as f64) as u64,
    })
}

/// `Retry-After`: seconds, or an HTTP date.
pub(crate) fn parse_retry_after(header: Option<&str>) -> Option<u64> {
    let header = header?.trim();
    if !header.is_empty() && header.bytes().all(|byte| byte.is_ascii_digit()) {
        return header
            .parse::<u64>()
            .ok()
            .map(|seconds| seconds.saturating_mul(1_000));
    }
    let date = httpdate::parse_http_date(header).ok()?;
    Some(
        date.duration_since(std::time::SystemTime::now())
            .map_or(0, |wait| wait.as_millis() as u64),
    )
}

/// What LSquare said of one event.
#[derive(Debug, PartialEq)]
pub(crate) enum Ack {
    Accepted,
    /// Named more than once, or not at all.
    Unexpected,
    Rejected {
        reason: String,
        resets_at: Option<String>,
    },
}

/// Each event counts as sent only if LSquare names it as accepted or duplicate, and not also as
/// rejected. `None` when the body isn't an acknowledgement at all.
pub(crate) fn acknowledgements(text: &str) -> Option<std::collections::HashMap<String, Ack>> {
    let body: Value = serde_json::from_str(text).ok()?;
    let body = body.as_object()?;
    let mut acks = std::collections::HashMap::new();
    for key in ["accepted", "duplicates"] {
        for id in body
            .get(key)
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
        {
            acks.insert(id.to_owned(), Ack::Accepted);
        }
    }
    for item in body
        .get("rejected")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let Some(id) = item.get("eventId").and_then(Value::as_str) else {
            continue;
        };
        if acks.contains_key(id) {
            acks.insert(id.to_owned(), Ack::Unexpected);
            continue;
        }
        let reason = item
            .get("reason")
            .and_then(Value::as_str)
            .filter(|reason| REJECTION_REASONS.contains(reason))
            .unwrap_or("unspecified");
        acks.insert(
            id.to_owned(),
            Ack::Rejected {
                reason: reason.to_owned(),
                resets_at: item
                    .get("resetsAt")
                    .and_then(Value::as_str)
                    .map(str::to_owned),
            },
        );
    }
    Some(acks)
}

/// When a daily limit resets: `resets_at` if it's in the next 25 hours, else the next 00:00 UTC.
fn reset_time(resets_at: Option<&str>, now: i64) -> i64 {
    match resets_at.and_then(parse_iso) {
        Some(parsed) if parsed > now && parsed <= now + 25 * 3_600_000 => parsed,
        _ => next_midnight(now),
    }
}

/// One line per event: shape only, never content.
fn dry_run_summary(event: &Event, bytes: usize) -> String {
    const SDK_FIELDS: [&str; 9] = [
        "eventId",
        "capturedAt",
        "source",
        "schemaVersion",
        "kind",
        "sdk",
        "redactions",
        "dropped",
        "truncated",
    ];
    let mut summary = Map::new();
    summary.insert("kind".into(), event.kind().into());
    // Where the ID came from, never the ID: a turn's is derived from its conversation and turn.
    let turn = event.kind() == "turn";
    let keyed = event
        .get("eventId")
        .and_then(Value::as_str)
        .is_some_and(|id| id.starts_with("k/"));
    summary.insert(
        "eventId".into(),
        match (turn, keyed) {
            (false, _) => "the call",
            (true, true) => "from turnId, hashed",
            (true, false) => "random: no turnId",
        }
        .into(),
    );
    let mut fields: Vec<&str> = event
        .0
        .keys()
        .map(String::as_str)
        .filter(|key| !SDK_FIELDS.contains(key))
        .collect();
    fields.sort_unstable();
    summary.insert("fields".into(), fields.into());
    summary.insert("bytes".into(), bytes.into());
    // How many tools the agent called: a turn's own, or those of a call's agent part.
    let turn_fields = if turn {
        Some(&event.0)
    } else {
        event.get("agent").and_then(Value::as_object)
    };
    if let Some(calls) = turn_fields
        .and_then(|fields| fields.get("toolCalls"))
        .and_then(Value::as_array)
    {
        summary.insert("toolCalls".into(), calls.len().into());
    }
    for key in ["truncated", "dropped", "redactions"] {
        if let Some(value) = event.get(key) {
            summary.insert(key.into(), value.clone());
        }
    }
    format!("[lsquare dry-run] {}", stringify(&summary))
}

/// The email-like `userId` warning is given once per process.
static WARNED_CONTACT_USER_ID: AtomicBool = AtomicBool::new(false);

impl Client {
    /// Never panics. Without an `api_key`, or with an invalid endpoint (outside dry run), the client is disabled.
    pub fn new(options: Options) -> Client {
        let logger = options.logger.clone().unwrap_or_else(default_logger);
        let flush_timeout = options.flush_timeout.unwrap_or(DEFAULT_FLUSH_TIMEOUT);
        let positive = |value: Option<usize>, default: usize| {
            value.filter(|value| *value > 0).unwrap_or(default)
        };
        let max_event_bytes = positive(options.max_event_bytes, DEFAULT_MAX_EVENT_BYTES);
        let max_concurrent_hooks =
            positive(options.max_concurrent_hooks, DEFAULT_MAX_CONCURRENT_HOOKS);
        let patterns: Vec<Pattern> = ORDER
            .into_iter()
            .filter(|pattern| !options.redact_off.contains(pattern))
            .collect();
        let mut sdk = Map::new();
        sdk.insert("name".into(), SDK_NAME.into());
        sdk.insert("version".into(), SDK_VERSION.into());
        sdk.insert("language".into(), "rust".into());
        let api_key = options
            .api_key
            .as_deref()
            .map(str::trim)
            .unwrap_or_default()
            .to_owned();
        let dry_run = options
            .dry_run
            .unwrap_or_else(|| matches!(env("LSQUARE_DRY_RUN").as_deref(), Some("1" | "true")));
        let mut warning = None;
        let (mode, url) = if dry_run {
            (Mode::DryRun, String::new())
        } else if api_key.is_empty() {
            warning = Some("[lsquare] No api_key: capture is disabled.".to_owned());
            (Mode::Disabled, String::new())
        } else {
            let endpoint = options
                .endpoint
                .clone()
                .filter(|endpoint| !endpoint.is_empty())
                .or_else(|| env("LSQUARE_ENDPOINT"))
                .unwrap_or_else(|| DEFAULT_ENDPOINT.to_owned());
            match resolve_endpoint(&endpoint) {
                Ok(url) => (Mode::Send, url),
                Err(problem) => {
                    warning = Some(format!("[lsquare] {problem}: capture is disabled."));
                    (Mode::Disabled, String::new())
                }
            }
        };
        let inner = Inner {
            mode,
            url,
            api_key,
            http: Mutex::new(None),
            context: Context {
                sdk: Value::Object(sdk),
                patterns,
                redact_keys: options.redact_keys.clone(),
                max_event_bytes,
            },
            before_send: options.before_send.clone(),
            on_error: options.on_error.clone(),
            logger,
            flush_timeout,
            // When flush doesn't wait (zero), a hook still gets the default time.
            before_send_timeout: options
                .before_send_timeout
                .filter(|timeout| !timeout.is_zero())
                .unwrap_or(if flush_timeout.is_zero() {
                    DEFAULT_FLUSH_TIMEOUT
                } else {
                    flush_timeout
                }),
            max_concurrent_hooks,
            max_concurrent_requests: positive(
                options.max_concurrent_requests,
                DEFAULT_MAX_CONCURRENT_REQUESTS,
            ),
            max_queue_events: positive(options.max_queue_events, DEFAULT_MAX_QUEUE_EVENTS),
            max_queue_bytes: positive(options.max_queue_bytes, DEFAULT_MAX_QUEUE_BYTES),
            max_event_bytes,
            max_attempts: options
                .max_attempts
                .filter(|attempts| *attempts > 0)
                .unwrap_or(DEFAULT_MAX_ATTEMPTS),
            request_timeout: options
                .request_timeout
                .filter(|timeout| !timeout.is_zero())
                .unwrap_or(DEFAULT_REQUEST_TIMEOUT),
            state: Mutex::new(State {
                runtime: Handle::try_current().ok(),
                reporting: Reporting {
                    max_bytes: positive(options.max_queue_bytes, DEFAULT_MAX_QUEUE_BYTES),
                    handled: options.on_error.is_some(),
                    ..Reporting::default()
                },
                ..State::default()
            }),
            changed: Notify::new(),
            stop: Notify::new(),
            hooks: Arc::new(Semaphore::new(max_concurrent_hooks)),
            identifying: std::sync::atomic::AtomicUsize::new(0),
            tool_hints: Mutex::new(Vec::new()),
            identified: Identified::default(),
        };
        let client = Client {
            inner: Arc::new(inner),
        };
        if let Some(warning) = warning {
            client.inner.log(Level::Warn, &warning);
        }
        client
    }

    /// Waits up to the flush timeout (2 seconds by default) for the events outstanding now,
    /// including calls that have replied but whose events are still being built. Events still
    /// pending when it returns are reported to `on_error`. It also waits, within the same time,
    /// until `on_error` was told of whatever failed before, and up to a second more for what it
    /// reports itself. Never fails.
    pub async fn flush(&self) -> FlushResult {
        self.flush_within(self.inner.flush_timeout).await
    }

    /// `flush`, waiting up to `timeout`. Zero doesn't wait: it only counts.
    pub async fn flush_within(&self, timeout: Duration) -> FlushResult {
        let inner = &self.inner;
        if inner.mode == Mode::Disabled {
            return FlushResult::default();
        }
        let targets = inner.lock().trackers();
        inner.wait_for(&targets, Instant::now() + timeout).await;
        let mut result = FlushResult::default();
        let mut built = None;
        let mut reported = false;
        for tracker in &targets {
            match tracker.outcome() {
                SENT => result.sent += 1,
                SKIPPED => result.skipped += 1,
                FAILED => result.failed += 1,
                _ => {
                    result.pending += 1;
                    // With zero the caller chose not to wait: pending is expected, not a problem.
                    if !timeout.is_zero() {
                        // With the event, when it is built already.
                        let (event, bytes) = built
                            .get_or_insert_with(|| inner.built_events())
                            .get(&(Arc::as_ptr(tracker) as usize))
                            .cloned()
                            .unwrap_or_default();
                        inner.lock().report(
                            Error::new(
                                ErrorCode::PendingAfterFlush,
                                "Still pending when flush() returned.",
                            )
                            .about(&tracker.event_id),
                            event,
                            bytes,
                        );
                        reported = true;
                    }
                }
            }
        }
        if reported {
            inner.after();
            inner.wait_for(&[], Instant::now() + REPORT_GRACE).await;
        }
        result
    }

    /// Ends the client: waits up to 5 seconds (or the flush timeout when that is longer) for the
    /// events outstanding, then drops whatever is left, stopping requests and retries. Later events
    /// are dropped. Call it before the process exits, and await it: nothing is sent once the
    /// runtime is gone.
    pub async fn shutdown(&self) -> FlushResult {
        self.shutdown_within(self.inner.flush_timeout.max(DEFAULT_SHUTDOWN_TIMEOUT))
            .await
    }

    /// `shutdown`, waiting up to `timeout`. Zero doesn't wait for events; `on_error` still has
    /// up to a second to be told of what was dropped.
    pub async fn shutdown_within(&self, timeout: Duration) -> FlushResult {
        let inner = &self.inner;
        if inner.mode == Mode::Disabled {
            inner.lock().closed = true;
            return FlushResult::default();
        }
        let deadline = Instant::now() + timeout;
        let mut targets = inner.lock().trackers();
        // Calls whose events are still being built get the chance to be queued first.
        let building = inner.lock().building.clone();
        inner.wait_for(&building, deadline).await;
        {
            let mut state = inner.lock();
            state.closed = true;
            for tracker in state.trackers() {
                if !targets.iter().any(|known| Arc::ptr_eq(known, &tracker)) {
                    targets.push(tracker);
                }
            }
            // One still unbuilt is dropped here, and said: its build finds the client closed.
            // Only what this ends is said: another shutdown, or its hook, may have ended it.
            for tracker in state.building.clone() {
                if state.end(&tracker, FAILED) {
                    state.report(
                        Error::new(
                            ErrorCode::Shutdown,
                            "shutdown() ended before the event was sent.",
                        )
                        .about(&tracker.event_id),
                        None,
                        0,
                    );
                }
            }
        }
        inner.after();
        inner.wait_for(&targets, deadline).await;
        {
            let mut state = inner.lock();
            let left: Vec<u64> = state.entries.keys().copied().collect();
            for seq in left {
                state.finish(
                    seq,
                    FAILED,
                    Some(Error::new(
                        ErrorCode::Shutdown,
                        "shutdown() ended before the event was sent.",
                    )),
                );
            }
        }
        // Requests and retries still running have nothing left to do.
        inner.stop.notify_waiters();
        inner.after();
        // Told of what was dropped before this returns, if the handler takes no longer than that.
        inner
            .wait_for(&[], deadline.max(Instant::now() + REPORT_GRACE))
            .await;
        let mut result = FlushResult::default();
        for tracker in &targets {
            match tracker.outcome() {
                SENT => result.sent += 1,
                SKIPPED => result.skipped += 1,
                _ => result.failed += 1,
            }
        }
        result
    }

    /// Whether events are recorded: false without an API key or with an invalid endpoint, outside dry run.
    pub fn is_enabled(&self) -> bool {
        self.inner.mode != Mode::Disabled
    }

    pub(crate) fn inner(&self) -> &Arc<Inner> {
        &self.inner
    }
}

impl State {
    /// Every event outstanding: being built, queued, in a request or waiting to retry. One still
    /// in the hands of its hook that shows its end already (shutdown dropped it) isn't.
    fn trackers(&self) -> Vec<Arc<Tracker>> {
        self.building
            .iter()
            .filter(|tracker| tracker.outcome() == 0)
            .cloned()
            .chain(self.entries.values().map(|entry| entry.tracker.clone()))
            .collect()
    }

    /// An event's end, which shows at once. True when this was it.
    fn end(&mut self, tracker: &Tracker, outcome: u8) -> bool {
        let ended = tracker.end(outcome);
        self.stirred |= ended;
        ended
    }

    /// Something for `on_error`, or for the log when there is no handler: then at most one line a
    /// minute, with how many more there were. `bytes` is what `event` weighs. Past
    /// `MAX_WAITING_REPORTS` waiting, a report is dropped; past the queue's own size in events
    /// waiting, a report goes without its event. So a handler that is slow, or never returns,
    /// holds a bounded amount.
    fn report(&mut self, error: Error, event: Option<Arc<Event>>, bytes: usize) {
        let mut more = 0;
        if !self.reporting.handled {
            let now = Instant::now();
            if self
                .last_error_log
                .is_some_and(|last| now.duration_since(last) < ERROR_LOG_INTERVAL)
            {
                self.suppressed_errors += 1;
                return;
            }
            self.last_error_log = Some(now);
            more = std::mem::take(&mut self.suppressed_errors);
        }
        let reporting = &mut self.reporting;
        if reporting.waiting.len() >= MAX_WAITING_REPORTS {
            reporting.behind = true;
            return;
        }
        let (event, bytes) = match event {
            Some(event) if reporting.bytes.saturating_add(bytes) <= reporting.max_bytes => {
                (Some(event), bytes)
            }
            _ => (None, 0),
        };
        reporting.bytes += bytes;
        reporting.waiting.push_back(Report::Error {
            error,
            event,
            bytes,
            more,
        });
    }

    /// A line for the logger, written from the thread that tells `on_error`: never where it
    /// came up. One past `MAX_WAITING_REPORTS` waiting is dropped.
    fn say(&mut self, level: Level, line: String) {
        if self.reporting.waiting.len() >= MAX_WAITING_REPORTS {
            self.reporting.behind = true;
            return;
        }
        self.reporting.waiting.push_back(Report::Line(level, line));
    }

    /// A warning given once per client, by its key.
    fn warn_once(&mut self, key: &str, line: &str) {
        if !self.warned.iter().any(|warned| warned == key) {
            self.warned.push(key.to_owned());
            self.say(Level::Warn, line.to_owned());
        }
    }

    /// A warning about one of a server's tools, given once per client, by its key: the first
    /// 1,000 such, since a server has as many tools as it likes. The other warnings don't count
    /// among them.
    fn warn_once_of_tool(&mut self, key: &str, line: &str) {
        if !self.warned_tools.iter().any(|warned| warned == key)
            && self.warned_tools.len() < MAX_WARNED_TOOLS
        {
            self.warned_tools.push(key.to_owned());
            self.say(Level::Warn, line.to_owned());
        }
    }

    /// An event's end. Anything that happens to it later (a response, a retry) finds it gone.
    fn finish(&mut self, seq: u64, outcome: u8, error: Option<Error>) {
        let Some(entry) = self.entries.remove(&seq) else {
            return;
        };
        self.queued.retain(|queued| *queued != seq);
        self.bytes = self.bytes.saturating_sub(entry.json.len());
        if let Some(holdback) = &mut self.holdback {
            if holdback.check == Some(seq) {
                holdback.check = None;
            }
        }
        // Its report waits before its end shows: whoever sees the end finds the report there.
        if let Some(error) = error {
            let bytes = entry.json.len();
            self.report(error.about(&entry.tracker.event_id), entry.event, bytes);
        }
        self.end(&entry.tracker, outcome);
    }

    /// Makes room for `bytes` more by dropping the oldest events that aren't in a request, unless
    /// even that couldn't make it fit: then nothing is dropped. An event in a request may already
    /// have reached LSquare, and one still being built has nothing to drop yet.
    fn make_room(&mut self, bytes: usize, max_events: usize, max_bytes: usize) -> bool {
        let fits = |state: &State| {
            state.entries.len() + state.building.len() < max_events
                && state.bytes + bytes <= max_bytes
        };
        if fits(self) {
            return true;
        }
        let sending = self
            .entries
            .values()
            .filter(|entry| entry.stage == Stage::Sending);
        let (held_events, held_bytes) = sending.fold(
            (self.building.len(), self.reserved),
            |(count, bytes), entry| (count + 1, bytes + entry.json.len()),
        );
        if held_events >= max_events || held_bytes + bytes > max_bytes {
            return false;
        }
        let droppable: Vec<u64> = self
            .entries
            .iter()
            .filter(|(_, entry)| entry.stage != Stage::Sending)
            .map(|(seq, _)| *seq)
            .collect();
        for seq in droppable {
            self.finish(
                seq,
                FAILED,
                Some(Error::new(
                    ErrorCode::QueueFull,
                    "Dropped to stay within the memory limits.",
                )),
            );
            if fits(self) {
                return true;
            }
        }
        false
    }
}

/// A call whose event is still being built: registered before the tool's reply leaves, so `flush`
/// waits for it, and counted in the queue's limits by what it holds. Dropped without its event
/// (its task was cancelled, or panicked), it counts as failed.
pub(crate) struct Building {
    inner: Arc<Inner>,
    tracker: Arc<Tracker>,
    reserved: usize,
    /// It checks whether a daily limit still holds: that limit's hold-back, by its ID.
    check: Option<u64>,
    /// Its event was built, and queued or ended: its end is no longer this one's to give.
    done: bool,
}

impl Drop for Building {
    fn drop(&mut self) {
        {
            let mut state = self.inner.lock();
            state
                .building
                .retain(|tracker| !Arc::ptr_eq(tracker, &self.tracker));
            state.bytes = state.bytes.saturating_sub(self.reserved);
            state.reserved = state.reserved.saturating_sub(self.reserved);
            // The check it was to be never went: the next call can be.
            if let (Some(id), Some(holdback)) = (self.check, &mut state.holdback) {
                if holdback.id == id {
                    holdback.checking = false;
                }
            }
            // Let go before its event was built (its task's runtime ended, or there was none):
            // the event is dropped, and that is said. Not when shutdown ended it: it said so.
            if !self.done && state.end(&self.tracker, FAILED) {
                state.report(
                    Error::new(
                        ErrorCode::InternalError,
                        "The Tokio runtime ended before the event was built; it was dropped.",
                    )
                    .about(&self.tracker.event_id),
                    None,
                    0,
                );
            }
        }
        self.inner.after();
        self.inner.changed.notify_waiters();
    }
}

impl Inner {
    fn lock(&self) -> MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(PoisonError::into_inner)
    }

    pub(crate) fn context(&self) -> &Context {
        &self.context
    }

    pub(crate) fn identified(&self) -> &Identified {
        &self.identified
    }

    pub(crate) fn enabled(&self) -> bool {
        self.mode != Mode::Disabled
    }

    pub(crate) fn dry_run(&self) -> bool {
        self.mode == Mode::DryRun
    }

    pub(crate) fn max_concurrent_hooks(&self) -> usize {
        self.max_concurrent_hooks
    }

    pub(crate) fn closed(&self) -> bool {
        self.lock().closed
    }

    pub(crate) fn log(&self, level: Level, line: &str) {
        let logger = self.logger.clone();
        // Wherever it is called from, the app's own thread included: nothing it captures is
        // taken.
        let _telling = capture::Telling::enter();
        // The app's logger must never break the app, nor the SDK.
        let _ = catch_unwind(AssertUnwindSafe(|| logger(level, line)));
    }

    /// A warning given once per client, by its key. Written by the thread that tells `on_error`:
    /// this is called on a tool's path.
    pub(crate) fn warn_once(self: &Arc<Self>, key: &str, line: &str) {
        self.lock().warn_once(key, line);
        self.after();
    }

    /// Remembers the name of a tool a server's handler defines: the newest first, a few for each
    /// server, and a few servers. A name that is very long isn't kept.
    pub(crate) fn hint_tool(&self, server: &str, name: &str) {
        if name.len() > MAX_HINT_BYTES || server.len() > MAX_HINT_BYTES {
            return;
        }
        let mut servers = self
            .tool_hints
            .lock()
            .unwrap_or_else(|held| held.into_inner());
        let at = match servers.iter().position(|(known, _)| known == server) {
            Some(at) => at,
            None => {
                servers.insert(0, (server.to_owned(), Vec::new()));
                servers.truncate(MAX_HINTED_SERVERS);
                0
            }
        };
        let names = &mut servers[at].1;
        match names.iter().position(|known| known == name) {
            Some(0) => {}
            // One it knows comes first again, as it is.
            Some(found) => names[..=found].rotate_right(1),
            None => {
                names.insert(0, name.to_owned());
                names.truncate(MAX_TOOL_HINTS);
            }
        }
    }

    /// The names remembered for a server, newest first.
    pub(crate) fn tool_hints(&self, server: &str) -> Vec<String> {
        self.tool_hints
            .lock()
            .unwrap_or_else(|held| held.into_inner())
            .iter()
            .find(|(known, _)| known == server)
            .map(|(_, names)| names.clone())
            .unwrap_or_default()
    }

    /// The same for a warning about one of a server's tools: the first 1,000 of them.
    pub(crate) fn warn_once_of_tool(self: &Arc<Self>, key: &str, line: &str) {
        self.lock().warn_once_of_tool(key, line);
        self.after();
    }

    /// Reports an error that belongs to no queued event.
    pub(crate) fn report(self: &Arc<Self>, error: Error) {
        self.lock().report(error, None, 0);
        self.after();
    }

    /// Once the state is free again after a step: starts telling `on_error` what waits for it,
    /// and wakes whoever waits for an event's end. Nothing of the app's runs in here.
    fn after(self: &Arc<Self>) {
        let (start, stirred) = {
            let mut state = self.lock();
            let start = !state.reporting.running && !state.reporting.waiting.is_empty();
            state.reporting.running |= start;
            (start, std::mem::take(&mut state.stirred))
        };
        // A thread that can't be started leaves its reports waiting for the next one that can:
        // they are never told where they came up.
        let started = start
            && std::thread::Builder::new()
                .name("lsquare-on-error".into())
                .spawn({
                    let telling = Telling {
                        inner: self.clone(),
                        started: false,
                        done: false,
                    };
                    move || telling.run()
                })
                .is_ok();
        if stirred || started {
            self.changed.notify_waiters();
        }
    }

    /// Tells `on_error`, or writes the log's line.
    fn tell(&self, report: Report) {
        let (error, event, more) = match report {
            Report::Line(level, line) => return self.log(level, &line),
            Report::Error {
                error, event, more, ..
            } => (error, event, more),
        };
        if let Some(handler) = &self.on_error {
            let handler = handler.clone();
            // `on_error` must never break the app, nor the SDK.
            let _ = catch_unwind(AssertUnwindSafe(|| handler(&error, event.as_deref())));
            return;
        }
        let more = if more > 0 {
            format!(" ({more} more since the last report)")
        } else {
            String::new()
        };
        self.log(
            Level::Warn,
            &format!("[lsquare] {}: {}{more}", error.code, error.message),
        );
    }

    /// The event of each one that is built, and what it weighs, by its tracker's address.
    fn built_events(&self) -> std::collections::HashMap<usize, (Option<Arc<Event>>, usize)> {
        let state = self.lock();
        state
            .entries
            .values()
            .map(|entry| {
                (
                    Arc::as_ptr(&entry.tracker) as usize,
                    (entry.event.clone(), entry.json.len()),
                )
            })
            .collect()
    }

    /// The runtime to run a task on: this one, or the last one known.
    fn runtime(&self) -> Option<Handle> {
        match Handle::try_current() {
            Ok(handle) => {
                self.lock().runtime = Some(handle.clone());
                Some(handle)
            }
            Err(_) => self.lock().runtime.clone(),
        }
    }

    /// Whether a call recorded now would be taken at all: not by a disabled client, nor a closed
    /// one, which says so once.
    pub(crate) fn recording(self: &Arc<Self>) -> bool {
        if self.mode == Mode::Disabled {
            return false;
        }
        let closed = {
            let mut state = self.lock();
            if state.closed {
                Self::refuse_closed(&mut state, false);
            }
            state.closed
        };
        self.after();
        !closed
    }

    /// Registers a call whose event is about to be built, which holds about `estimate` bytes until
    /// then. `None`: the client records nothing now, and the call's data can be let go at once.
    /// Called on the tool's own path, so nothing of the app's runs in here.
    pub(crate) fn admit(self: &Arc<Self>, call_id: &str, estimate: usize) -> Option<Building> {
        if self.mode == Mode::Disabled {
            return None;
        }
        let building = {
            let mut state = self.lock();
            match Self::admission(&mut state, false) {
                None => None,
                Some(check) => self.register(&mut state, call_id, estimate, check),
            }
        };
        self.after();
        building
    }

    /// Whether an event recorded now is taken at all: not after shutdown, which is said once,
    /// nor while a daily limit holds, unless it is the one that checks whether the limit still
    /// does: then that limit's hold-back, by its ID, which the event keeps until it is queued
    /// (`register`), or gives back (`release_check`). `turn`: it comes from `capture()`.
    fn admission(state: &mut State, turn: bool) -> Option<Option<u64>> {
        if state.closed {
            Self::refuse_closed(state, turn);
            return None;
        }
        match Self::held_back(state) {
            Admission::Drop => None,
            Admission::Send => Some(None),
            Admission::Check(holdback) => Some(Some(holdback)),
        }
    }

    /// The check an event was to be never went: the next one can be.
    fn release_check(state: &mut State, check: Option<u64>) {
        if let (Some(id), Some(holdback)) = (check, &mut state.holdback) {
            if holdback.id == id {
                holdback.checking = false;
            }
        }
    }

    /// Makes room for an admitted event that holds about `estimate` bytes until it is queued,
    /// and counts it among those being built. `None`: the queue is full, which is said.
    fn register(
        self: &Arc<Self>,
        state: &mut State,
        event_id: &str,
        estimate: usize,
        check: Option<u64>,
    ) -> Option<Building> {
        if !state.make_room(estimate, self.max_queue_events, self.max_queue_bytes) {
            Self::release_check(state, check);
            state.report(
                Error::new(
                    ErrorCode::QueueFull,
                    "The queue is full; the event was dropped.",
                )
                .about(event_id),
                None,
                0,
            );
            return None;
        }
        let tracker = Arc::new(Tracker {
            event_id: event_id.to_owned(),
            outcome: AtomicU8::new(0),
            retry_at: Mutex::new(None),
        });
        state.building.push(tracker.clone());
        state.bytes += estimate;
        state.reserved += estimate;
        Some(Building {
            inner: self.clone(),
            tracker,
            reserved: estimate,
            check,
            done: false,
        })
    }

    /// Said once: an `on_error` that records after shutdown must not feed itself.
    fn refuse_closed(state: &mut State, turn: bool) {
        if !state.reported_closed {
            state.reported_closed = true;
            let what = if turn {
                "capture() was called"
            } else {
                "A call was recorded"
            };
            state.report(
                Error::new(
                    ErrorCode::Shutdown,
                    format!("{what} after shutdown(); this and later events are dropped."),
                ),
                None,
                0,
            );
        }
    }

    /// Whether a call recorded while a daily limit holds goes ahead, becomes the check, or is dropped and counted.
    fn held_back(state: &mut State) -> Admission {
        let Some(holdback) = &mut state.holdback else {
            return Admission::Send;
        };
        if now_ms() >= holdback.until {
            Self::end_holdback(state);
            return Admission::Send;
        }
        let due = Instant::now() >= holdback.next_check;
        if let (Some(check), true) = (holdback.check, due) {
            // Unanswered for a whole interval: dropped and counted, and this event checks instead.
            holdback.check = None;
            holdback.dropped += 1;
            state.finish(check, FAILED, None);
        }
        let Some(holdback) = &mut state.holdback else {
            return Admission::Send;
        };
        if holdback.check.is_none() && !holdback.checking && due {
            // The next check's time is set once this one is queued: one that never is leaves the
            // next call to be the check.
            holdback.checking = true;
            return Admission::Check(holdback.id);
        }
        holdback.dropped += 1;
        Admission::Drop
    }

    fn end_holdback(state: &mut State) {
        if let Some(holdback) = state.holdback.take() {
            if holdback.dropped > 0 {
                let mut error = Error::new(
                    ErrorCode::QuotaDropped,
                    format!(
                        "Sending resumed after the daily limit; {} events were dropped.",
                        holdback.dropped
                    ),
                );
                error.count = Some(holdback.dropped);
                state.report(error, None, 0);
            }
        }
    }

    /// Starts holding back, or goes on. False: the limit it was told of has reset already.
    fn start_holdback(state: &mut State, resets_at: Option<&str>) -> bool {
        let now = now_ms();
        if resets_at
            .and_then(parse_iso)
            .is_some_and(|parsed| parsed <= now)
        {
            return false;
        }
        let until = reset_time(resets_at, now);
        if let Some(holdback) = &mut state.holdback {
            holdback.until = holdback.until.max(until);
            return true;
        }
        state.tokens += 1;
        state.holdback = Some(Holdback {
            until,
            next_check: Instant::now() + QUOTA_PROBE,
            check: None,
            checking: false,
            dropped: 0,
            id: state.tokens,
        });
        let at = iso(until);
        let mut error = Error::new(
            ErrorCode::QuotaExceeded,
            format!(
                "The project's daily limit was reached; events are dropped until {} UTC.",
                &at[11..16]
            ),
        );
        error.resets_at = Some(at);
        state.report(error, None, 0);
        true
    }

    /// Finishes a call's event, now that who it was for is known: redacts it, runs `before_send`,
    /// and queues it. `diagnostic` is what the call has to say of its `identify`, reported unless
    /// the event is refused for shutdown.
    pub(crate) async fn record(
        self: &Arc<Self>,
        building: Building,
        data: ShapedCallData,
        head: CallHead,
        diagnostic: Option<Error>,
    ) {
        let call_id = head.call_id.clone();
        let hook = self.before_send.clone();
        let fail = |code: ErrorCode, message: &str| {
            Some(Box::new(Error::new(code, message).about(&call_id)))
        };
        let built: Finished = {
            let context = self.context.clone();
            let hooked = hook.is_some();
            let build = move || finish_call(data, &head, &context, hooked);
            // Never sent unredacted: a failure here drops the event.
            match catch_unwind(AssertUnwindSafe(build)) {
                Err(_) => Err(fail(
                    ErrorCode::RedactionFailed,
                    "Building the event failed; it was dropped, never sent unredacted.",
                )),
                Ok(Err(refused)) => {
                    let (code, message) = match refused.code {
                        "invalid_id" => (ErrorCode::InvalidId, "An ID must be a non-empty string of at most 512 bytes, without NUL; the event was dropped.".to_owned()),
                        _ => (ErrorCode::TooLarge, format!("The event is larger than {} bytes even without its largest parts; it was dropped.", self.max_event_bytes)),
                    };
                    let mut error = Error::new(code, message).about(&call_id);
                    error.paths = refused.paths;
                    Err(Some(Box::new(error)))
                }
                Ok(Ok(draft)) => Ok(Some(draft)),
            }
        };
        let built = match (built, hook) {
            (Ok(Some(draft)), Some(hook)) => {
                let deadline = by(self.before_send_timeout);
                self.run_hook(draft, hook, &building, deadline, 0).await
            }
            (built, _) => built,
        };
        self.conclude(building, built, diagnostic);
    }

    /// An event's end once it is built and its hook has run: queued, or dropped, which is said.
    /// `diagnostic` is what a call has to say of its `identify`.
    fn conclude(
        self: &Arc<Self>,
        mut building: Building,
        built: Finished,
        diagnostic: Option<Error>,
    ) {
        let tracker = building.tracker.clone();
        // What there is to say of the call waits for `on_error` before its event can be sent, or
        // shows its end: whoever sees either finds the report waiting, and waits for it. After
        // shutdown nothing more is said: the event was dropped there, and said so.
        let queue = {
            let mut state = self.lock();
            let closed = state.closed;
            if let (Some(diagnostic), false) = (diagnostic, closed) {
                state.report(diagnostic, None, 0);
            }
            match built {
                Err(error) => {
                    if let (Some(error), false) = (error, closed) {
                        state.report(*error, None, 0);
                    }
                    state.end(&tracker, FAILED);
                    None
                }
                Ok(None) => {
                    state.end(&tracker, SKIPPED);
                    None
                }
                Ok(Some(draft)) => Some(draft),
            }
        };
        if let Some(draft) = queue {
            self.enqueue(&mut building, Event(draft.event));
        }
        building.done = true;
        drop(building);
        self.after();
        self.pump();
    }

    /// Runs `before_send` on a thread of its own, by `deadline` and within the hook limit, then
    /// checks, shortens and redacts what it returned. `Ok(None)`: the hook dropped the event.
    /// A hook past its deadline is left to end by itself: its thread belongs to no runtime, so it
    /// holds up neither the runtime's end nor the process's. `depth` is how deep in the clients'
    /// callbacks the event was captured: what the hook captures is one deeper.
    async fn run_hook(
        self: &Arc<Self>,
        draft: Box<crate::event::Draft>,
        hook: BeforeSend,
        building: &Building,
        deadline: Instant,
        depth: u8,
    ) -> Finished {
        let tracker = building.tracker.clone();
        let call_id = building.tracker.event_id.as_str();
        let fail = |code: ErrorCode, message: String| -> Finished {
            Err(Some(Box::new(Error::new(code, message).about(call_id))))
        };
        let timeout = self.before_send_timeout;
        let late = || {
            fail(
                ErrorCode::HookTimeout,
                format!(
                    "before_send did not finish within {} ms; the event was dropped.",
                    timeout.as_millis()
                ),
            )
        };
        let context = self.context.clone();
        let hooks = self.hooks.clone();
        let wanted = tracker.clone();
        let work = async move {
            // Its deadline counts the wait for a free slot too. The slot is the hook's until it
            // returns, even past its deadline: a hook that runs on still takes its thread.
            let slot = hooks.acquire_owned().await.ok()?;
            let given = Event(draft.event.clone());
            let (answer, answered) = tokio::sync::oneshot::channel();
            let run = move || {
                let _slot = slot;
                // Looked at once more where the hook is about to run: its time may have run out
                // while it waited for its turn, and shutdown may have ended its event. Then the
                // app's code isn't run for it.
                if Instant::now() >= deadline || wanted.outcome() != 0 {
                    let _ = answer.send((Hooked::NotRun, Instant::now()));
                    return;
                }
                capture::in_hook(depth);
                let returned = catch_unwind(AssertUnwindSafe(|| hook(given)));
                let returned_at = Instant::now();
                let hooked = match returned {
                    Err(_) => Hooked::Panicked,
                    Ok(None) => Hooked::Dropped,
                    Ok(Some(out)) => {
                        match catch_unwind(AssertUnwindSafe(|| after_hook(&out.0, draft, &context)))
                        {
                            Err(_) => Hooked::RedactionFailed,
                            Ok(built) => Hooked::Built(built),
                        }
                    }
                };
                let _ = answer.send((hooked, returned_at));
            };
            std::thread::Builder::new()
                .name("lsquare-before-send".into())
                .spawn(run)
                .ok()?;
            answered.await.ok()
        };
        let hooked = match tokio::time::timeout_at(deadline.into(), work).await {
            Err(_) => return late(),
            Ok(None) => {
                return fail(
                    ErrorCode::InternalError,
                    "before_send could not be started; the event was dropped.".into(),
                )
            }
            // Its answer can be there, ready, when this is next looked at, however late that is:
            // what counts is when the hook returned.
            Ok(Some((_, returned_at))) if returned_at > deadline => return late(),
            Ok(Some((hooked, _))) => hooked,
        };
        match hooked {
            // Ended by shutdown, which said so; or out of time.
            Hooked::NotRun if tracker.outcome() != 0 => Err(None),
            Hooked::NotRun => late(),
            Hooked::Panicked => fail(ErrorCode::HookFailed, "before_send panicked; the event was dropped.".into()),
            Hooked::Dropped => Ok(None),
            Hooked::RedactionFailed => fail(ErrorCode::RedactionFailed, "Redaction failed; the event was dropped, not sent unredacted.".into()),
            Hooked::Built(Ok(draft)) => Ok(Some(draft)),
            Hooked::Built(Err(refused)) => match refused.code {
                "too_large" => fail(ErrorCode::TooLarge, format!("before_send made the event larger than {} bytes even without its largest parts; it was dropped.", self.max_event_bytes)),
                _ => fail(ErrorCode::HookFailed, "before_send returned invalid IDs; the event was dropped.".into()),
            },
        }
    }

    /// Queues a finished event, or in dry run, logs what it would have sent.
    fn enqueue(self: &Arc<Self>, building: &mut Building, event: Event) {
        if let Some(user) = event.get("userId").and_then(Value::as_str) {
            if looks_like_contact(user) && !WARNED_CONTACT_USER_ID.swap(true, Ordering::Relaxed) {
                self.lock().say(Level::Warn, "[lsquare] A userId looks like an email address or phone number. It is sent as given, so the person can be deleted by it: send an internal ID instead.".to_owned());
            }
        }
        let json: Arc<str> = stringify(&event.0).into();
        // Only a handler sees the event, so only then is it kept beside its JSON.
        let handled = self.on_error.is_some();
        let tracker = building.tracker.clone();
        if self.mode == Mode::DryRun {
            // After shutdown it ended there already, and that was said.
            let mut state = self.lock();
            if !state.closed {
                state.say(Level::Info, dry_run_summary(&event, json.len()));
            }
            state.end(&tracker, SKIPPED);
            return;
        }
        let mut state = self.lock();
        // What it held while it was built gives way to what it weighs.
        state.building.retain(|known| !Arc::ptr_eq(known, &tracker));
        state.bytes = state.bytes.saturating_sub(building.reserved);
        state.reserved = state.reserved.saturating_sub(building.reserved);
        building.reserved = 0;
        if state.closed {
            // Shutdown ended it already, and said so.
            state.end(&tracker, FAILED);
            return;
        }
        if !state.make_room(json.len(), self.max_queue_events, self.max_queue_bytes) {
            state.report(
                Error::new(
                    ErrorCode::QueueFull,
                    "The queue is full; the event was dropped.",
                )
                .about(&tracker.event_id),
                handled.then(|| Arc::new(event)),
                json.len(),
            );
            state.end(&tracker, FAILED);
            return;
        }
        state.seq += 1;
        let seq = state.seq;
        state.bytes += json.len();
        state.entries.insert(
            seq,
            Entry {
                tracker,
                event: handled.then(|| Arc::new(event)),
                json,
                stage: Stage::Queued,
                attempts: 0,
                token: 0,
            },
        );
        state.queued.push_back(seq);
        // It is the check of the limit it was taken for, if that one still holds: one that
        // outlived it is an event like any other, of whatever holds now.
        if let (Some(id), Some(holdback)) = (building.check.take(), &mut state.holdback) {
            if holdback.id == id {
                holdback.check = Some(seq);
                holdback.checking = false;
                holdback.next_check = Instant::now() + QUOTA_PROBE;
            }
        }
    }

    /// Sends batches while a request slot is free: each carries the events ready now, up to the
    /// ingestion's limits. The limit on slots keeps a slow or unreachable LSquare from holding many
    /// sockets, and lets batches form under load.
    pub(crate) fn pump(self: &Arc<Self>) {
        if self.mode != Mode::Send {
            return;
        }
        let Some(runtime) = self.runtime() else {
            return;
        };
        let mut batches = Vec::new();
        {
            let mut state = self.lock();
            let mut probe = None;
            if state.holdback.is_some() {
                probe = Self::holding_back(&mut state);
            }
            let held = state.holdback.is_some();
            while state.active_requests < self.max_concurrent_requests {
                let batch = match (held, probe.take()) {
                    // While a daily limit holds, only its check goes, on its own.
                    (true, Some(check)) => vec![check],
                    (true, None) => break,
                    (false, _) => Self::take_batch(&state),
                };
                if batch.is_empty() {
                    break;
                }
                state.tokens += 1;
                let token = state.tokens;
                let mut sending = Vec::with_capacity(batch.len());
                for seq in &batch {
                    state.queued.retain(|queued| queued != seq);
                    if let Some(entry) = state.entries.get_mut(seq) {
                        entry.stage = Stage::Sending;
                        entry.attempts += 1;
                        entry.token = token;
                        sending.push((*seq, entry.json.clone()));
                    }
                }
                state.active_requests += 1;
                let holdback = state.holdback.as_ref().map(|holdback| holdback.id);
                batches.push((token, sending, holdback));
            }
        }
        // Spawned once the state is free: a task a finished runtime refuses is dropped at once,
        // and gives its events back as it goes.
        for (token, sending, holdback) in batches {
            let request = Request {
                inner: self.clone(),
                token,
                seqs: sending.iter().map(|(seq, _)| *seq).collect(),
                ended: false,
            };
            runtime.spawn(request.run(sending, holdback));
        }
        self.after();
    }

    /// While a daily limit holds, events that became ready are dropped and counted, and the check
    /// goes on its own: its sequence number, when it is ready to go.
    fn holding_back(state: &mut State) -> Option<u64> {
        let until = state.holdback.as_ref()?.until;
        if now_ms() >= until {
            Self::end_holdback(state);
            return None;
        }
        let check = state.holdback.as_ref()?.check;
        let dropped: Vec<u64> = state
            .queued
            .iter()
            .copied()
            .filter(|seq| Some(*seq) != check)
            .collect();
        for seq in dropped {
            if let Some(holdback) = &mut state.holdback {
                holdback.dropped += 1;
            }
            state.finish(seq, FAILED, None);
        }
        check.filter(|check| state.queued.contains(check))
    }

    /// The first event ready, then the others in order, within the ingestion's limits: 100 events,
    /// 1 MB of body, and an acknowledgement that stays under 60 KiB even if every event came back
    /// as `quota_exceeded`.
    fn take_batch(state: &State) -> Vec<u64> {
        let mut batch = Vec::new();
        let mut body = BATCH_OVERHEAD;
        let mut ack = ACK_OVERHEAD;
        for seq in &state.queued {
            let Some(entry) = state.entries.get(seq) else {
                continue;
            };
            let event_ack = ack_bytes(&entry.tracker.event_id);
            let fits = batch.len() < MAX_BATCH_EVENTS
                && body + entry.json.len() < MAX_BATCH_BYTES
                && ack + event_ack <= MAX_ACK_BYTES;
            // The first always goes: an event is at most 256 KB.
            if !batch.is_empty() && !fits {
                break;
            }
            batch.push(*seq);
            body += entry.json.len() + 1;
            ack += event_ack;
        }
        batch
    }

    fn http(&self) -> Option<reqwest::Client> {
        let mut http = self.http.lock().unwrap_or_else(PoisonError::into_inner);
        if http.is_none() {
            // A redirect is never followed, so the key can't go elsewhere.
            *http = reqwest::Client::builder()
                .redirect(reqwest::redirect::Policy::none())
                .user_agent(format!("lsquare-rust/{SDK_VERSION}"))
                .build()
                .ok();
        }
        http.clone()
    }

    /// One exchange: the status, `Retry-After`, and a 2xx's body when it is within 64 KB. `None`:
    /// a network error, or a redirect, which is refused.
    async fn exchange(&self, body: String) -> Option<(u16, Option<u64>, Option<String>)> {
        let http = self.http()?;
        let mut response = http
            .post(&self.url)
            .header("content-type", "application/json")
            .header("authorization", format!("Bearer {}", self.api_key))
            .body(body)
            .send()
            .await
            .ok()?;
        let status = response.status().as_u16();
        if (300..400).contains(&status) {
            return None;
        }
        let retry_after = parse_retry_after(
            response
                .headers()
                .get("retry-after")
                .and_then(|value| value.to_str().ok()),
        );
        if !(200..300).contains(&status) {
            // Only a 2xx body matters (the acknowledgement). Others are decided from status and headers.
            return Some((status, retry_after, None));
        }
        let mut bytes: Vec<u8> = Vec::new();
        loop {
            match response.chunk().await {
                Err(_) => return None,
                Ok(None) => break,
                Ok(Some(chunk)) => {
                    if bytes.len() + chunk.len() > MAX_RESPONSE_BYTES {
                        return Some((status, retry_after, None));
                    }
                    bytes.extend_from_slice(&chunk);
                }
            }
        }
        Some((
            status,
            retry_after,
            Some(String::from_utf8_lossy(&bytes).into_owned()),
        ))
    }

    /// What a request's answer means for each of its events still this request's.
    fn settle(
        self: &Arc<Self>,
        token: u64,
        seqs: &[u64],
        holdback: Option<u64>,
        reply: Option<(u16, Option<u64>, Option<String>)>,
    ) {
        let mut retry = None;
        {
            let mut state = self.lock();
            let live: Vec<u64> =
                seqs.iter()
                    .copied()
                    .filter(|seq| {
                        state.entries.get(seq).is_some_and(|entry| {
                            entry.stage == Stage::Sending && entry.token == token
                        })
                    })
                    .collect();
            match reply {
                None => retry = self.retry_or_fail(&mut state, &live, None, None),
                Some((status, _, body)) if (200..300).contains(&status) => {
                    let acks = body.as_deref().and_then(acknowledgements);
                    let mut over_quota = Vec::new();
                    let mut resets_at: Option<String> = None;
                    let mut accepted = 0;
                    for seq in live {
                        let Some(id) = state
                            .entries
                            .get(&seq)
                            .map(|entry| entry.tracker.event_id.clone())
                        else {
                            continue;
                        };
                        match acks.as_ref().and_then(|acks| acks.get(&id)) {
                            Some(Ack::Accepted) => {
                                accepted += 1;
                                state.finish(seq, SENT, None);
                            }
                            Some(Ack::Rejected { reason, resets_at: at }) if reason == "quota_exceeded" => {
                                resets_at = resets_at.or_else(|| at.clone());
                                over_quota.push(seq);
                            }
                            Some(Ack::Rejected { reason, .. }) => state.finish(seq, FAILED, Some(Error::new(ErrorCode::Rejected, format!("LSquare rejected the event: {reason}.")))),
                            _ => state.finish(seq, FAILED, Some(Error::new(ErrorCode::SendFailed, "Unexpected response: LSquare did not acknowledge the event.").status(Some(status)))),
                        }
                    }
                    if !over_quota.is_empty() {
                        // Final, never retried. Holding back, they're counted and reported once; after
                        // a late answer from a limit that has reset since, each is reported.
                        let holding = Self::start_holdback(&mut state, resets_at.as_deref());
                        for seq in over_quota {
                            match (&mut state.holdback, holding) {
                                (Some(holdback), true) => {
                                    holdback.dropped += 1;
                                    state.finish(seq, FAILED, None);
                                }
                                _ => state.finish(
                                    seq,
                                    FAILED,
                                    Some(Error::new(
                                        ErrorCode::Rejected,
                                        "LSquare rejected the event: quota_exceeded.",
                                    )),
                                ),
                            }
                        }
                    } else if accepted > 0
                        && holdback.is_some()
                        && state.holdback.as_ref().map(|holdback| holdback.id) == holdback
                    {
                        // Its check went through: the limit was raised.
                        Self::end_holdback(&mut state);
                    }
                }
                Some((status, retry_after, _)) if status == 429 || status >= 500 => {
                    retry = self.retry_or_fail(&mut state, &live, Some(status), retry_after)
                }
                Some((status, _, _)) => {
                    for seq in live {
                        state.finish(
                            seq,
                            FAILED,
                            Some(
                                Error::new(
                                    ErrorCode::SendFailed,
                                    format!("LSquare answered {status}; not retried."),
                                )
                                .status(Some(status)),
                            ),
                        );
                    }
                }
            }
        }
        if let Some((retry_token, delay, seqs)) = retry {
            let timer = Retry {
                inner: self.clone(),
                token: retry_token,
                seqs,
                fired: false,
            };
            match self.runtime() {
                Some(runtime) => {
                    runtime.spawn(timer.run(delay));
                }
                // Nowhere to wait: its events go back to the queue as it is dropped.
                None => drop(timer),
            }
        }
        self.after();
        // A retry's time is for whoever waits to see: it may be past their deadline.
        self.changed.notify_waiters();
    }

    /// A request's answer couldn't be read through: its events are dropped, never left as sending.
    fn fail_sending(self: &Arc<Self>, token: u64, seqs: &[u64]) {
        {
            let mut state = self.lock();
            for seq in seqs {
                if state
                    .entries
                    .get(seq)
                    .is_some_and(|entry| entry.stage == Stage::Sending && entry.token == token)
                {
                    state.finish(
                        *seq,
                        FAILED,
                        Some(Error::new(
                            ErrorCode::InternalError,
                            "Sending failed unexpectedly.",
                        )),
                    );
                }
            }
        }
        self.after();
    }

    /// Each event retries on its own count; one delay for the batch, so its events go out together again.
    fn retry_or_fail(
        &self,
        state: &mut State,
        live: &[u64],
        status: Option<u16>,
        retry_after_ms: Option<u64>,
    ) -> Option<(u64, Duration, Vec<u64>)> {
        let mut retrying = Vec::new();
        let mut attempts = 0;
        for seq in live {
            let Some(tried) = state.entries.get(seq).map(|entry| entry.attempts) else {
                continue;
            };
            if tried < self.max_attempts {
                retrying.push(*seq);
                attempts = attempts.max(tried);
                continue;
            }
            let message = match status {
                None => format!("Network error after {tried} attempts."),
                Some(status) => format!("LSquare answered {status} after {tried} attempts."),
            };
            state.finish(
                *seq,
                FAILED,
                Some(Error::new(ErrorCode::SendFailed, message).status(status)),
            );
        }
        if retrying.is_empty() {
            return None;
        }
        let delay = retry_delay(attempts, retry_after_ms, random());
        state.tokens += 1;
        let token = state.tokens;
        let retry_at = Instant::now() + delay;
        for seq in &retrying {
            if let Some(entry) = state.entries.get_mut(seq) {
                entry.stage = Stage::Retry;
                entry.token = token;
                // A flush whose deadline comes first can't see this event finish: it stops waiting for it.
                *entry
                    .tracker
                    .retry_at
                    .lock()
                    .unwrap_or_else(PoisonError::into_inner) = Some(retry_at);
            }
        }
        Some((token, delay, retrying))
    }

    /// Back in the queue: a retry is due, or what it waited on is gone.
    fn requeue(&self, token: u64, seqs: &[u64], from: Stage, front: bool) {
        let mut state = self.lock();
        let mut back = Vec::new();
        for seq in seqs {
            if let Some(entry) = state.entries.get_mut(seq) {
                if entry.stage == from && entry.token == token {
                    entry.stage = Stage::Queued;
                    if front {
                        // Its request never ran: the attempt doesn't count.
                        entry.attempts = entry.attempts.saturating_sub(1);
                    }
                    *entry
                        .tracker
                        .retry_at
                        .lock()
                        .unwrap_or_else(PoisonError::into_inner) = None;
                    back.push(*seq);
                }
            }
        }
        if front {
            for seq in back.into_iter().rev() {
                state.queued.push_front(seq);
            }
        } else {
            state.queued.extend(back);
        }
    }

    /// Waits until every one of `targets` has ended, or can't by `deadline`: one whose next attempt
    /// is due after it isn't waited for. And until `on_error` was told of everything reported so
    /// far, within the same time. What is ready is sent from here, at first and each time
    /// something changes: an event another runtime gave back as it ended has only this one to send
    /// it.
    async fn wait_for(self: &Arc<Self>, targets: &[Arc<Tracker>], deadline: Instant) {
        loop {
            let notified = self.changed.notified();
            tokio::pin!(notified);
            // Registered before anything is looked at: an event that ends, or comes back to the
            // queue, from here on wakes it.
            notified.as_mut().enable();
            self.pump();
            // In dry run nothing is sent, and a report still has to be started on its way.
            self.after();
            let waiting = targets.iter().any(|tracker| {
                tracker.outcome() == 0
                    && !tracker
                        .retry_at
                        .lock()
                        .unwrap_or_else(PoisonError::into_inner)
                        .is_some_and(|retry_at| retry_at > deadline)
            }) || self.lock().reporting.undelivered();
            if !waiting || Instant::now() >= deadline {
                return;
            }
            // Tokio panics making a timer on a runtime that has none: then nothing can be
            // waited for, nor sent, and that is said instead.
            let Ok(late) = catch_unwind(AssertUnwindSafe(|| {
                tokio::time::sleep_until(deadline.into())
            })) else {
                self.warn_once("no-timers", "[lsquare] This Tokio runtime has no timers, so flush() can't wait and events can't be sent from it: build it with enable_all().");
                return;
            };
            tokio::select! {
                () = &mut notified => {}
                () = late => return,
            }
        }
    }
}

enum Admission {
    Send,
    /// It checks whether the daily limit of this hold-back still holds.
    Check(u64),
    Drop,
}

enum Hooked {
    /// The hook wasn't called: its time had run out, or its event had ended already.
    NotRun,
    Panicked,
    Dropped,
    RedactionFailed,
    Built(Built),
}

/// One request. Dropped before its answer (its runtime ended, or shutdown stopped it), it gives
/// its slot back, and its events to the queue.
struct Request {
    inner: Arc<Inner>,
    token: u64,
    seqs: Vec<u64>,
    ended: bool,
}

impl Request {
    async fn run(mut self, sending: Vec<(u64, Arc<str>)>, holdback: Option<u64>) {
        let mut body = String::with_capacity(
            BATCH_OVERHEAD
                + sending
                    .iter()
                    .map(|(_, json)| json.len() + 1)
                    .sum::<usize>(),
        );
        body.push_str("{\"events\":[");
        for (index, (_, json)) in sending.iter().enumerate() {
            if index > 0 {
                body.push(',');
            }
            body.push_str(json);
        }
        body.push_str("]}");
        drop(sending);
        let inner = self.inner.clone();
        let stopped = inner.stop.notified();
        tokio::pin!(stopped);
        stopped.as_mut().enable();
        if inner.closed()
            && self
                .seqs
                .iter()
                .all(|seq| !inner.lock().entries.contains_key(seq))
        {
            self.end();
            return;
        }
        // Covers the whole exchange, including reading the response.
        let reply = tokio::select! {
            reply = tokio::time::timeout(inner.request_timeout, inner.exchange(body)) => reply.ok().flatten(),
            () = &mut stopped => {
                self.end();
                return;
            }
        };
        self.end();
        // Whatever goes wrong reading the answer, no event is left as sending.
        if catch_unwind(AssertUnwindSafe(|| {
            inner.settle(self.token, &self.seqs, holdback, reply)
        }))
        .is_err()
        {
            inner.fail_sending(self.token, &self.seqs);
        }
        inner.pump();
    }

    /// Returns the request's slot, once.
    fn end(&mut self) {
        if self.ended {
            return;
        }
        self.ended = true;
        let mut state = self.inner.lock();
        state.active_requests = state.active_requests.saturating_sub(1);
    }
}

impl Drop for Request {
    fn drop(&mut self) {
        if self.ended {
            return;
        }
        self.end();
        self.inner
            .requeue(self.token, &self.seqs, Stage::Sending, true);
        self.inner.changed.notify_waiters();
    }
}

/// A batch's wait before its next attempt. Dropped before it is due (its runtime ended), its
/// events go back to the queue at once.
struct Retry {
    inner: Arc<Inner>,
    token: u64,
    seqs: Vec<u64>,
    fired: bool,
}

impl Retry {
    async fn run(mut self, delay: Duration) {
        let inner = self.inner.clone();
        tokio::select! {
            () = tokio::time::sleep(delay) => {}
            () = inner.stop.notified() => {}
        }
        self.fire();
        inner.pump();
    }

    fn fire(&mut self) {
        if !self.fired {
            self.fired = true;
            self.inner
                .requeue(self.token, &self.seqs, Stage::Retry, false);
            self.inner.changed.notify_waiters();
        }
    }
}

impl Drop for Retry {
    fn drop(&mut self) {
        self.fire();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_client_remembers_a_few_tool_names_newest_first() {
        let client = Client::new(Options::new().dry_run(false));
        let inner = client.inner();
        assert!(inner.tool_hints("desk").is_empty());
        for index in 0..20 {
            inner.hint_tool("desk", &format!("t{index}"));
        }
        assert_eq!(
            inner.tool_hints("desk"),
            ["t19", "t18", "t17", "t16", "t15", "t14", "t13", "t12"]
        );
        // One it knows comes first again, once, and the others stay as they were.
        inner.hint_tool("desk", "t15");
        inner.hint_tool("desk", "t15");
        assert_eq!(
            inner.tool_hints("desk"),
            ["t15", "t19", "t18", "t17", "t16", "t14", "t13", "t12"]
        );
        // Each server has its own, and a name too long to be a tool's isn't kept.
        inner.hint_tool("other", "search");
        inner.hint_tool("other", &"x".repeat(MAX_HINT_BYTES + 1));
        assert_eq!(inner.tool_hints("other"), ["search"]);
        assert_eq!(inner.tool_hints("desk").len(), MAX_TOOL_HINTS);
        // Of the servers too, only so many are kept: the newest.
        for index in 0..MAX_HINTED_SERVERS {
            inner.hint_tool(&format!("server {index}"), "a");
        }
        assert!(inner.tool_hints("desk").is_empty() && inner.tool_hints("other").is_empty());
        assert_eq!(inner.tool_hints("server 0"), ["a"]);
    }

    #[test]
    fn the_endpoint_must_be_https_or_local() {
        assert_eq!(
            resolve_endpoint("https://ingest.lsquare.io").as_deref(),
            Ok("https://ingest.lsquare.io/v1/turns")
        );
        assert_eq!(
            resolve_endpoint("https://example.com/base//?x=1#y").as_deref(),
            Ok("https://example.com/base/v1/turns")
        );
        assert_eq!(
            resolve_endpoint("http://localhost:8787/").as_deref(),
            Ok("http://localhost:8787/v1/turns")
        );
        assert_eq!(
            resolve_endpoint("http://[::1]:8787").as_deref(),
            Ok("http://[::1]:8787/v1/turns")
        );
        assert!(resolve_endpoint("http://example.com").is_err());
        assert!(resolve_endpoint("not a url").is_err());
    }

    #[test]
    fn a_retry_waits_as_typescript_does() {
        assert_eq!(retry_delay(1, None, 0.999_999), Duration::from_millis(249));
        assert_eq!(retry_delay(2, None, 0.5), Duration::from_millis(250));
        assert_eq!(retry_delay(3, None, 1.0), Duration::from_millis(1_000));
        assert_eq!(
            retry_delay(9, None, 1.0),
            Duration::from_millis(RETRY_CAP_MS)
        );
        assert_eq!(
            retry_delay(1, Some(2_000), 0.0),
            Duration::from_millis(2_000)
        );
        assert_eq!(
            retry_delay(1, Some(120_000), 0.0),
            Duration::from_millis(RETRY_AFTER_CAP_MS)
        );
        assert_eq!(parse_retry_after(Some(" 7 ")), Some(7_000));
        assert_eq!(
            parse_retry_after(Some("Wed, 21 Oct 2015 07:28:00 GMT")),
            Some(0)
        );
        assert_eq!(parse_retry_after(Some("soon")), None);
        assert!((0.0..1.0).contains(&random()));
    }

    #[test]
    fn an_event_is_sent_only_if_lsquare_says_so() {
        let acks = acknowledgements(r#"{"accepted":["a"],"duplicates":["b"],"rejected":[{"eventId":"c","reason":"quota_exceeded","resetsAt":"2026-01-01T00:00:00.000Z"},{"eventId":"d","reason":"made_up"},{"eventId":"a","reason":"invalid_field"},{"index":3,"reason":"invalid_field"}]}"#).unwrap();
        assert_eq!(acks.get("a"), Some(&Ack::Unexpected));
        assert_eq!(acks.get("b"), Some(&Ack::Accepted));
        assert_eq!(
            acks.get("c"),
            Some(&Ack::Rejected {
                reason: "quota_exceeded".into(),
                resets_at: Some("2026-01-01T00:00:00.000Z".into())
            })
        );
        assert_eq!(
            acks.get("d"),
            Some(&Ack::Rejected {
                reason: "unspecified".into(),
                resets_at: None
            })
        );
        assert_eq!(acks.len(), 4);
        assert!(acknowledgements("[]").is_none() && acknowledgements("oops").is_none());
    }

    #[test]
    fn a_daily_limit_resets_when_lsquare_says_or_at_midnight() {
        let now = parse_iso("2026-10-28T10:00:00.000Z").unwrap();
        assert_eq!(
            iso(reset_time(Some("2026-10-28T12:00:00.000Z"), now)),
            "2026-10-28T12:00:00.000Z"
        );
        assert_eq!(
            iso(reset_time(Some("2026-11-28T12:00:00.000Z"), now)),
            "2026-10-29T00:00:00.000Z"
        );
        assert_eq!(iso(reset_time(None, now)), "2026-10-29T00:00:00.000Z");
    }

    #[test]
    fn a_flush_on_a_runtime_without_timers_says_so_and_does_not_panic() {
        let lines = Arc::new(Mutex::new(Vec::new()));
        let written = lines.clone();
        let client = Client::new(
            Options::new()
                .api_key("lsq_test_key".to_owned())
                .endpoint("http://127.0.0.1:9")
                .dry_run(false)
                .logger(move |_, line| written.lock().unwrap().push(line.to_owned())),
        );
        // An event still being built, so there is something to wait for.
        let _unbuilt = client
            .inner()
            .admit("call-1", 10)
            .expect("the call is taken");
        let runtime = tokio::runtime::Builder::new_current_thread()
            .build()
            .expect("a runtime without timers");
        let result = runtime.block_on(client.flush_within(Duration::from_millis(50)));
        assert_eq!(result.pending, 1);
        // The logger is told from a thread of its own.
        let deadline = Instant::now() + Duration::from_secs(5);
        let said = || {
            lines
                .lock()
                .unwrap()
                .iter()
                .any(|line: &String| line.contains("has no timers"))
        };
        while !said() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(5));
        }
        assert!(said(), "{:?}", lines.lock().unwrap());
    }

    #[test]
    fn a_check_taken_for_one_daily_limit_is_not_the_next_ones() {
        let client = Client::new(
            Options::new()
                .api_key("lsq_test_key".to_owned())
                .endpoint("http://127.0.0.1:9")
                .dry_run(false)
                .logger(|_, _| {}),
        );
        let inner = client.inner();
        let holdback = |id: u64, next_check: Instant| Holdback {
            until: now_ms() + 3_600_000,
            next_check,
            check: None,
            checking: false,
            dropped: 0,
            id,
        };
        // A check is due, and the next call is taken as it.
        inner.lock().holdback = Some(holdback(1, Instant::now()));
        let mut check = inner.admit("call-1", 10).expect("the call is taken");
        assert_eq!(check.check, Some(1));
        assert!(inner
            .lock()
            .holdback
            .as_ref()
            .is_some_and(|now| now.checking));
        // While its event is built, that limit ends and another begins, with a check of its own
        // on its way.
        let later = Instant::now() + QUOTA_PROBE;
        inner.lock().holdback = Some(Holdback {
            checking: true,
            ..holdback(2, later)
        });
        let mut event = Map::new();
        event.insert("eventId".into(), "call-1".into());
        inner.enqueue(&mut check, Event(event));
        drop(check);
        // The old check is an event like any other now: the new limit's check, and its time, are
        // as they were.
        let state = inner.lock();
        let now = state.holdback.as_ref().expect("the second limit holds");
        assert_eq!((now.id, now.check, now.checking), (2, None, true));
        assert_eq!(now.next_check, later);
        assert_eq!(state.queued.len(), 1);
        drop(state);

        // One let go before its event was built doesn't free the next limit's check either.
        inner.lock().holdback = Some(holdback(3, Instant::now()));
        let unbuilt = inner.admit("call-2", 10).expect("the call is taken");
        assert_eq!(unbuilt.check, Some(3));
        inner.lock().holdback = Some(Holdback {
            checking: true,
            ..holdback(4, later)
        });
        drop(unbuilt);
        assert!(inner
            .lock()
            .holdback
            .as_ref()
            .is_some_and(|now| now.checking));
    }
}
