//! `capture()`: a turn of an agent, sent as an event of its own, or, inside a tool call on an
//! instrumented server, as that call's `agent` part. A port of `capture()` in
//! `packages/lsquare/src/client.ts`.

use std::cell::Cell;

use super::*;
use crate::event::{shape_agent, turn::build_turn, EarlyAgent};
use crate::json::{dispose, json_size};
use crate::normalize::{id_value, is_valid_id, turn_event_id, turn_id_value, Check, Id};
use crate::turn::Turn;

/// How deep in the clients' callbacks a capture is still taken: an event captured by the app is
/// at depth 0, one its `before_send` captures at 1, one that one's `before_send` captures at 2.
/// Deeper, a hook that captures from each event it is given would never end.
const MAX_CALLBACK_DEPTH: u8 = 2;
/// The depth of the thread that tells `on_error` and the logger: nothing captured there is
/// taken, since each turn could bring the next report.
const TELLING: u8 = u8::MAX;

thread_local! {
    /// How deep in the clients' callbacks this thread runs: 0 in the app's own code. The same
    /// for every client, so two clients capturing from each other's callbacks end too.
    static CALLBACK_DEPTH: Cell<u8> = const { Cell::new(0) };
}

tokio::task_local! {
    /// The tool call this task is handling, set by the wrapper around the handler's own code.
    pub(crate) static CALL: Arc<CallScope>;
}

fn depth() -> u8 {
    // A thread that is ending has none to give: nothing of the app's own runs there.
    CALLBACK_DEPTH.try_with(Cell::get).unwrap_or(TELLING)
}

/// This thread runs `before_send` for an event captured at `depth`.
pub(super) fn in_hook(depth: u8) {
    let _ = CALLBACK_DEPTH.try_with(|own| own.set(depth.saturating_add(1)));
}

/// While it lives, this thread is telling `on_error` or the logger: nothing captured from
/// either is taken, wherever the SDK calls them from. Then the thread is what it was.
pub(super) struct Telling(Option<u8>);

impl Telling {
    pub(super) fn enter() -> Telling {
        Telling(CALLBACK_DEPTH.try_with(|own| own.replace(TELLING)).ok())
    }
}

impl Drop for Telling {
    fn drop(&mut self) {
        if let Some(before) = self.0 {
            let _ = CALLBACK_DEPTH.try_with(|own| own.set(before));
        }
    }
}

/// What `capture()` was given, let go of without recursion however it ends.
struct Given(Value);

impl Drop for Given {
    fn drop(&mut self) {
        dispose(std::mem::take(&mut self.0));
    }
}

/// The place of the event that checks whether a daily limit still holds, held from the moment an
/// event is admitted as that one: given back if it never gets to be it.
struct Claim {
    inner: Arc<Inner>,
    check: Option<u64>,
}

impl Drop for Claim {
    fn drop(&mut self) {
        if self.check.is_some() {
            let mut state = self.inner.lock();
            Inner::release_check(&mut state, self.check.take());
        }
    }
}

/// A `capture()` inside a call, as the call keeps it: its IDs, and its agent part, read then.
pub(crate) struct Captured {
    /// `orgId`, `userId`, `conversationId` and `turnId`, each as it was given where it is valid
    /// (see `captured_ids`).
    pub ids: Map<String, Value>,
    pub agent: EarlyAgent,
}

enum Slot {
    /// No `capture()` yet: the first is the call's agent part.
    Open,
    /// One is being read.
    Reading,
    Agent(Box<Captured>),
    /// One couldn't be read: the call's event is dropped, never built from what was half read.
    Failed,
    /// The tool has returned, or the call isn't this wrapper's to record after all.
    Closed,
}

/// A tool call in progress on an instrumented server, as `capture()` inside it sees it.
pub(crate) struct CallScope {
    /// The client that records the call: a capture on another is a turn of its own, unlinked.
    client: Arc<Inner>,
    call_id: String,
    slot: Mutex<Slot>,
    /// The call this one was made inside of, if any: another client's, say, whose own captures
    /// in here are still that call's.
    outer: Option<Arc<CallScope>>,
}

impl CallScope {
    /// A call that starts on this task, inside whatever call the task is handling already.
    pub(crate) fn new(client: &Arc<Inner>, call_id: String) -> CallScope {
        CallScope {
            client: client.clone(),
            call_id,
            slot: Mutex::new(Slot::Open),
            outer: CALL.try_with(Arc::clone).ok(),
        }
    }

    pub(crate) fn call_id(&self) -> &str {
        &self.call_id
    }

    /// Whether `client` records this call.
    pub(crate) fn is_of(&self, client: &Arc<Inner>) -> bool {
        Arc::ptr_eq(&self.client, client)
    }

    /// Takes over what another wrapper's scope held of the same call, as `close` gave it: the
    /// agent part captured before this wrapper took the call.
    #[allow(clippy::result_unit_err)]
    pub(crate) fn adopt(&self, captured: Option<Result<Box<Captured>, ()>>) {
        let mut slot = self.slot();
        if let (Slot::Open, Some(captured)) = (&*slot, captured) {
            *slot = captured.map_or(Slot::Failed, Slot::Agent);
        }
    }

    fn slot(&self) -> MutexGuard<'_, Slot> {
        self.slot.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// The call is over for `capture()`: the tool returned, or the call is recorded elsewhere.
    /// What was captured as its agent part, or an error when that couldn't be read.
    #[allow(clippy::result_unit_err)]
    pub(crate) fn close(&self) -> Option<Result<Box<Captured>, ()>> {
        match std::mem::replace(&mut *self.slot(), Slot::Closed) {
            Slot::Agent(captured) => Some(Ok(captured)),
            Slot::Failed | Slot::Reading => Some(Err(())),
            Slot::Open | Slot::Closed => None,
        }
    }

    /// Whether `client` records this call, still under way.
    fn open_to(&self, client: &Arc<Inner>) -> bool {
        self.is_of(client) && !matches!(*self.slot(), Slot::Closed)
    }

    /// Takes the agent part's place, if it is free.
    fn claim(&self) -> bool {
        let mut slot = self.slot();
        let free = matches!(*slot, Slot::Open);
        if free {
            *slot = Slot::Reading;
        }
        free
    }

    fn fill(&self, read: Option<Box<Captured>>) {
        let mut slot = self.slot();
        if matches!(*slot, Slot::Reading) {
            *slot = read.map_or(Slot::Failed, Slot::Agent);
        }
    }
}

/// The call this task is handling that `client` records: the innermost one still under way. A
/// call made inside it and recorded by another client leaves it this client's.
fn current_call(client: &Arc<Inner>) -> Option<Arc<CallScope>> {
    let mut scope = CALL.try_with(Arc::clone).ok();
    while let Some(candidate) = scope {
        if candidate.open_to(client) {
            return Some(candidate);
        }
        scope = candidate.outer.clone();
    }
    None
}

/// What the wrapper reads of a `capture()` inside the call: the IDs that group the call and make
/// its `turnRef`. Each is kept as it was given where it is valid, and so of a bounded size. One
/// that isn't valid is kept as a short one that isn't either (a string as a string, so that it
/// still groups the call as TypeScript's does): the call's event is dropped for it all the same.
/// The turn ID is kept as the event will have it, or not at all.
fn captured_ids(fields: &Map<String, Value>) -> Map<String, Value> {
    let mut ids = Map::new();
    for key in ["orgId", "userId", "conversationId"] {
        let kept = match fields.get(key) {
            None => continue,
            Some(Value::String(id)) if id.is_empty() || is_valid_id(id) => id.as_str().into(),
            Some(Value::String(_)) => "\0".into(),
            Some(Value::Null) => Value::Null,
            Some(_) => Value::Bool(false),
        };
        ids.insert(key.to_owned(), kept);
    }
    if let Check::Valid(turn) = turn_id_value(fields.get("turnId")) {
        ids.insert("turnId".to_owned(), turn.into());
    }
    ids
}

impl Client {
    /// Records one turn of an agent: what it was asked and answered, the tools it called, what
    /// it used.
    ///
    /// Outside a tool call it is sent as an event of its own (`turn`). Inside a tool on a server
    /// instrumented with this client, the first `capture` before the tool returns becomes that
    /// call's `agent` part, and its `org_id`, `user_id` and `conversation_id` the call's, ahead
    /// of `identify`'s; any other is a `turn` event linked to the call. "Inside" is the task the
    /// handler runs on: a task the tool spawns is outside, and links its turns with
    /// [`Client::current_call_id`].
    ///
    /// It never fails, never panics and never waits: not for the network, nor for `before_send`.
    /// What goes wrong is reported to [`Options::on_error`]. What it does before it returns is
    /// bounded by the event's limits, except for listing the keys of each JSON object it is
    /// given, which takes time in proportion to the object's size.
    ///
    /// A turn captured from `before_send` is taken up to two levels deep; one captured from
    /// `on_error` or the logger is ignored, since each could bring the next report.
    pub fn capture(&self, turn: Turn) {
        let inner = &self.inner;
        let mut given = Given(Value::Object(turn.into_fields()));
        if catch_unwind(AssertUnwindSafe(|| inner.capture(&mut given.0))).is_err() {
            inner.report(Error::new(
                ErrorCode::InternalError,
                "capture() failed unexpectedly; the event was dropped.",
            ));
        }
    }

    /// Inside a tool on a server instrumented with this client: the call's ID, so a turn
    /// captured later elsewhere, such as in a task the tool spawned, can link itself to the call
    /// with [`Turn::call_id`]. `None` outside a call.
    pub fn current_call_id(&self) -> Option<String> {
        current_call(&self.inner).map(|scope| scope.call_id.clone())
    }
}

impl Inner {
    fn capture(self: &Arc<Self>, input: &mut Value) {
        if self.mode == Mode::Disabled {
            return;
        }
        let Some(fields) = input.as_object_mut() else {
            return;
        };
        let depth = depth();
        if depth > MAX_CALLBACK_DEPTH {
            return;
        }
        // Inside a call on an instrumented server, from the app's own code: the first capture
        // before the tool returns is the call's agent part; any other is a turn linked to it.
        if let Some(call) = current_call(self).filter(|_| depth == 0) {
            if call.claim() {
                // Read now, as any capture's data is: what the app gave is let go of as this
                // returns, and the call keeps what was read of it.
                let read = catch_unwind(AssertUnwindSafe(|| {
                    let ids = captured_ids(fields);
                    let agent = shape_agent(fields, &self.context);
                    Box::new(Captured { ids, agent })
                }));
                call.fill(read.ok());
                return;
            }
            // Left out and an empty one alike are absent: linked to the call. A given ID, valid
            // or not, is kept for the checks.
            if matches!(id_value(fields.get("callId")), Id::Absent | Id::Empty) {
                fields.insert("callId".into(), call.call_id.clone().into());
            }
        }
        self.capture_turn(input, depth);
    }

    /// A turn of its own: built here and now, then queued, or handed to `before_send` first.
    fn capture_turn(self: &Arc<Self>, input: &Value, depth: u8) {
        let captured_at = iso(now_ms());
        // The hook's time counts from here, whenever a runtime gets to start it.
        let deadline = by(self.before_send_timeout);
        let admitted = {
            let mut state = self.lock();
            Self::admission(&mut state, true)
        };
        // Its place as the daily limit's check, if it was given it, is given back wherever this
        // ends before the event is registered with it.
        let mut claim = admitted.map(|check| Claim {
            inner: self.clone(),
            check,
        });
        self.after();
        let Some(claim) = &mut claim else {
            return;
        };
        let hook = self.before_send.clone();
        // Never sent unredacted: a failure here drops the event.
        let built = catch_unwind(AssertUnwindSafe(|| {
            build_turn(
                input,
                &captured_at,
                &self.context,
                hook.is_some(),
                turn_event_id,
            )
        }));
        let refused = |error: Error| {
            let mut state = self.lock();
            // After shutdown that is what is said of it, once.
            if state.closed {
                Self::refuse_closed(&mut state, true);
            } else {
                state.report(error, None, 0);
            }
            drop(state);
            self.after();
        };
        let draft = match built {
            Err(_) => {
                return refused(Error::new(
                    ErrorCode::RedactionFailed,
                    "Building the event failed; it was dropped, never sent unredacted.",
                ))
            }
            Ok(Err(refusal)) => {
                let (code, message) = match refusal.code {
                    "invalid_id" => (ErrorCode::InvalidId, "An ID must be a non-empty string of at most 512 bytes, without NUL; the event was dropped.".to_owned()),
                    "too_large" => (ErrorCode::TooLarge, format!("The event is larger than {} bytes even without its largest parts; it was dropped.", self.max_event_bytes)),
                    _ => (ErrorCode::InternalError, "capture() couldn't read the turn; the event was dropped.".to_owned()),
                };
                let mut error = Error::new(code, message);
                error.paths = refusal.paths;
                return refused(error);
            }
            Ok(Ok(draft)) => draft,
        };
        let event_id = draft
            .event
            .get("eventId")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_owned();
        // What it weighs, counted before room is made for it: one too large for the queue takes
        // no other event's place.
        let estimate = json_size(&draft.event);
        let building = {
            let mut state = self.lock();
            let check = claim.check.take();
            if state.closed {
                Self::release_check(&mut state, check);
                Self::refuse_closed(&mut state, true);
                None
            } else {
                self.register(&mut state, &event_id, estimate, check)
            }
        };
        self.after();
        let Some(building) = building else {
            return;
        };
        let Some(hook) = hook else {
            return self.conclude(building, Ok(Some(draft)), None);
        };
        match self.runtime() {
            Some(runtime) => {
                let inner = self.clone();
                // A runtime that is gone drops the task, and the event with it, which is said.
                runtime.spawn(async move {
                    let built = inner
                        .run_hook(draft, hook, &building, deadline, depth)
                        .await;
                    inner.conclude(building, built, None);
                });
            }
            None => self.conclude(
                building,
                Err(Some(Box::new(
                    Error::new(
                        ErrorCode::InternalError,
                        "capture() found no Tokio runtime to wait for before_send on; the event was dropped.",
                    )
                    .about(&event_id),
                ))),
                None,
            ),
        }
    }
}

#[cfg(test)]
mod tests {
    use std::future::Future;
    use std::task::{Context as TaskContext, Poll, Waker};

    use super::*;

    fn client() -> Client {
        Client::new(Options::new().dry_run(true).logger(|_, _| {}))
    }

    fn scope(client: &Client) -> Arc<CallScope> {
        Arc::new(CallScope::new(client.inner(), "call_1".to_owned()))
    }

    fn answer(captured: &Captured) -> String {
        format!("{:?}", captured.agent)
    }

    #[test]
    fn the_first_capture_inside_a_call_is_kept_as_its_agent_part() {
        let lsquare = client();
        let call = scope(&lsquare);
        CALL.sync_scope(call.clone(), || {
            assert_eq!(lsquare.current_call_id().as_deref(), Some("call_1"));
            lsquare.capture(
                Turn::new()
                    .org_id("org_1")
                    .turn_id("7")
                    .answer("the first")
                    .metadata("plan", "pro"),
            );
            // A later one is a turn of its own: the first stays.
            lsquare.capture(Turn::new().answer("the second"));
        });
        let captured = call.close().expect("a capture").expect("that was read");
        // Its IDs as they were given, for the call; nothing else of what the app gave.
        assert_eq!(
            Value::Object(captured.ids.clone()),
            serde_json::json!({ "orgId": "org_1", "turnId": "7" })
        );
        assert!(answer(&captured).contains("the first"));
        assert!(!answer(&captured).contains("the second"));
        // Once the call is over, there is nothing more to take, and no call to be inside of.
        assert!(call.close().is_none());
        CALL.sync_scope(call.clone(), || {
            assert_eq!(lsquare.current_call_id(), None);
            lsquare.capture(Turn::new().answer("after the tool returned"));
        });
        assert!(call.close().is_none());
    }

    #[test]
    fn a_call_is_its_own_clients_and_not_a_callbacks() {
        let (lsquare, other) = (client(), client());
        let call = scope(&lsquare);
        CALL.sync_scope(call.clone(), || {
            // Another client's capture, and one from a callback, leave the call's place free.
            assert_eq!(other.current_call_id(), None);
            other.capture(Turn::new().answer("another client's"));
            in_hook(0);
            lsquare.capture(Turn::new().answer("from a hook"));
            let _ = CALLBACK_DEPTH.try_with(|own| own.set(0));
        });
        assert!(call.close().is_none());
        // A clone of the client is the client.
        let call = scope(&lsquare);
        CALL.sync_scope(call.clone(), || {
            lsquare.clone().capture(Turn::new().answer("a clone's"));
        });
        assert!(call.close().is_some());
    }

    #[test]
    fn a_call_made_inside_another_leaves_the_outer_one_to_its_own_client() {
        let (lsquare, other) = (client(), client());
        let outer = scope(&lsquare);
        let (inner, again) = CALL.sync_scope(outer.clone(), || {
            // A call the tool makes on its way, recorded by another client.
            let inner = scope(&other);
            CALL.sync_scope(inner.clone(), || {
                assert_eq!(lsquare.current_call_id().as_deref(), Some("call_1"));
                lsquare.capture(Turn::new().answer("the outer call's"));
                other.capture(Turn::new().answer("the inner call's"));
            });
            // And one recorded by the same client: the innermost is the call.
            let again = Arc::new(CallScope::new(lsquare.inner(), "call_2".to_owned()));
            CALL.sync_scope(again.clone(), || {
                assert_eq!(lsquare.current_call_id().as_deref(), Some("call_2"));
                lsquare.capture(Turn::new().answer("the second call's"));
                // Once that one is over, what is captured is the enclosing call's again.
                again.close();
                assert_eq!(lsquare.current_call_id().as_deref(), Some("call_1"));
            });
            (inner, again)
        });
        let captured = |scope: &CallScope| scope.close().map(|read| answer(&read.expect("read")));
        assert!(captured(&outer).is_some_and(|agent| agent.contains("the outer call's")));
        assert!(captured(&inner).is_some_and(|agent| agent.contains("the inner call's")));
        assert!(captured(&again).is_none());
    }

    #[test]
    fn a_wrapper_that_takes_a_call_over_takes_what_was_captured_for_it() {
        let lsquare = client();
        let outer = scope(&lsquare);
        CALL.sync_scope(outer.clone(), || {
            lsquare.capture(Turn::new().answer("before it was taken over"));
        });
        assert!(outer.is_of(lsquare.inner()) && !outer.is_of(client().inner()));
        // The same ID, and the agent part captured so far.
        let inner = CallScope::new(lsquare.inner(), outer.call_id().to_owned());
        inner.adopt(outer.close());
        // One of its own would have come first.
        inner.adopt(None);
        CALL.sync_scope(
            Arc::new(CallScope::new(lsquare.inner(), "other".into())),
            || {},
        );
        let captured = inner.close().expect("a capture").expect("that was read");
        assert!(answer(&captured).contains("before it was taken over"));
        // What it captured itself isn't replaced by what it is handed.
        let (first, second) = (scope(&lsquare), scope(&lsquare));
        for (scope, said) in [(&first, "its own"), (&second, "the other's")] {
            CALL.sync_scope(scope.clone(), || lsquare.capture(Turn::new().answer(said)));
        }
        first.adopt(second.close());
        let captured = first.close().expect("a capture").expect("that was read");
        assert!(answer(&captured).contains("its own"));
    }

    #[test]
    fn a_calls_ids_are_kept_small_and_an_invalid_one_stays_invalid() {
        let given = serde_json::json!({
            "orgId": "o".repeat(100_000),
            "userId": "user_8",
            "conversationId": "",
            "turnId": 7,
            "answer": "not an ID",
        });
        let ids = captured_ids(given.as_object().unwrap());
        assert_eq!(
            Value::Object(ids),
            serde_json::json!({ "orgId": "\0", "userId": "user_8", "conversationId": "", "turnId": "7" })
        );
        let odd = serde_json::json!({ "orgId": 5, "userId": null, "turnId": "t".repeat(300_000) });
        assert_eq!(
            Value::Object(captured_ids(odd.as_object().unwrap())),
            serde_json::json!({ "orgId": false, "userId": null })
        );
    }

    /// Captures as it is dropped: what a tool's own guard does when its call is let go.
    struct OnDrop(Client);

    impl Drop for OnDrop {
        fn drop(&mut self) {
            self.0.capture(Turn::new().answer("as the call was let go"));
        }
    }

    #[test]
    fn a_capture_made_as_the_tools_future_is_dropped_is_still_inside_the_call() {
        let lsquare = client();
        let call = scope(&lsquare);
        let guard = OnDrop(lsquare.clone());
        let mut running = Box::pin(CALL.scope(call.clone(), async move {
            let _guard = guard;
            std::future::pending::<()>().await;
        }));
        let mut context = TaskContext::from_waker(Waker::noop());
        assert!(matches!(running.as_mut().poll(&mut context), Poll::Pending));
        // The MCP SDK lets go of the call: the wrapper records it once the future is gone.
        drop(running);
        let captured = call.close().expect("a capture").expect("that was read");
        assert!(answer(&captured).contains("as the call was let go"));
    }

    #[test]
    fn captures_nested_in_callbacks_end_and_those_of_the_reporting_thread_are_ignored() {
        std::thread::spawn(|| {
            assert_eq!(depth(), 0);
            in_hook(0);
            assert_eq!(depth(), 1);
            in_hook(MAX_CALLBACK_DEPTH);
            assert!(depth() > MAX_CALLBACK_DEPTH);
            in_hook(0);
            {
                // While `on_error` or the logger is told, and as deep as before once it was.
                let _telling = Telling::enter();
                assert_eq!(depth(), TELLING);
                in_hook(TELLING);
                assert_eq!(depth(), TELLING);
            }
            assert_eq!(depth(), 1);
        })
        .join()
        .unwrap();
    }
}
