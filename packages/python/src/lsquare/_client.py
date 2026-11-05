"""The client (sdk-0.2-python-spec.md section 3), ported from packages/lsquare/src/client.ts.

The pipeline is TypeScript's, step for step: an event is built on the capturing thread, queued,
passed through `before_send` if there is one, redacted, then sent in batches, retried, held back
after `quota_exceeded`, and finished exactly once. Where TypeScript has one thread, microtasks and
timers, this has a lock and the client's own threads (`_threads.py`): every step that changes the
client's state runs under `self._lock`, and deferred steps and timers run on its scheduler.
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import contextvars
import copy
import inspect
import itertools
import os
import random
import sys
import threading
import time
import weakref
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Literal, Protocol

from . import _event, _jsjson, _normalize, _redact, _url
from ._runtime import StderrLogger, is_pyodide, logger_method, runtime_name
from ._sending import Abort, Exchange, NetworkError, Reply, Resolver
from ._threads import Interrupted, Pool, PrivateLoop, Scheduler, acknowledging, exiting, interrupts, schedule_on, start_thread
from ._version import __version__

SDK_NAME = "lsquare"
#: The package's own version, as events and the User-Agent give it: one source, which the build reads too.
SDK_VERSION = __version__
DEFAULT_ENDPOINT = "https://ingest.lsquare.io"

Mode = Literal["disabled", "dry-run", "send"]
Outcome = Literal["sent", "skipped", "failed"]
#: `queued`: ready to send, waiting for a request slot.
Stage = Literal["waiting", "hook", "ready", "queued", "sending", "retry"]

#: What `capture()` reads of its input inside a call, once each.
CAPTURE_FIELDS = (
    "conversationId", "orgId", "userId", "toolName", "question", "turnId", "answer", "status", "error", "toolCalls",
    "usage", "latency", "verification", "client", "apiKeyId", "surface", "metadata", "callId",
)

DEFAULTS = {
    "flush_timeout_ms": 2_000,
    "max_concurrent_hooks": 16,
    "max_concurrent_requests": 4,
    "max_queue_events": 1_000,
    "max_queue_bytes": 10 * 1024 * 1024,
    "max_event_bytes": 256 * 1024,
    "max_attempts": 3,
    "request_timeout_ms": 10_000,
}
#: The ingestion's limits for one request.
MAX_BATCH_EVENTS = 100
MAX_BATCH_BYTES = 1024 * 1024
#: The ingestion answers 413 to a batch whose acknowledgement could pass this.
MAX_ACK_BYTES = 60 * 1024
ACK_OVERHEAD = len('{"accepted":[],"duplicates":[],"rejected":[]}')
#: As long as any `resetsAt`.
LONGEST_RESETS_AT = "2000-01-01T00:00:00.000Z"
BATCH_OVERHEAD = len('{"events":[]}')
#: While a daily limit holds, one event is sent this often to check whether it was raised.
QUOTA_PROBE_MS = 15 * 60_000
RETRY_BASE_MS = 250
RETRY_CAP_MS = 5_000
RETRY_AFTER_CAP_MS = 30_000
ERROR_LOG_INTERVAL_MS = 60_000
MAX_CALLBACK_DEPTH = 2
#: At interpreter exit, how long the lines the exit flush handed to the callback thread may take to
#: be written before the interpreter finalizes.
EXIT_LOG_GRACE_S = 0.5
#: How long flush(), shutdown() and what the wrapper hands to `wait_until` wait at most, past their
#: own wait, for the reports and log lines handed to the callback thread before they return to have
#: run (an async handler's, to have started): a slow sync handler can't hold them back longer, and
#: one that hasn't returned holds back only the first of them (see `LSquare._delivered`).
DELIVERED_GRACE_S = 0.5
CALLBACKS_PER_SECOND = 100
#: The hosts plain http may go to, as `url.hostname` writes them: an IPv6 one in brackets.
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
REJECTION_REASONS = {
    "missing_field", "invalid_field", "invalid_conversation_id", "too_large", "unsupported_schema_version",
    "quota_exceeded",
}
#: The longest wait any timer takes, as TypeScript's (about 24.8 days).
MAX_TIMER_MS = 2_147_483_647
#: How many of a call's turns (see `CallScope.turns`) its sending waits for at once: a turn captured
#: while that many are still being sent goes as usual, only not waited for. A long-running tool
#: capturing many turns while the ingestion is slow holds no more than this.
MAX_CALL_TURNS = 100

#: Callback nesting, shared by every client so loops between two clients are cut too: the app's
#: own code runs at depth 0, an `on_error` or logger call delivered at depth n runs at n, and so
#: does everything it starts. Events remember the depth they were captured at.
_depth: contextvars.ContextVar[int] = contextvars.ContextVar("lsquare_callback_depth", default=0)

#: The email-like `userId` warning is given once per process.
_warned_contact_user_id = False


def current_depth() -> int:
    return _depth.get()


@dataclass
class FlushResult:
    sent: int = 0
    #: Completed without sending: dry run, or `before_send` returned `None`.
    skipped: int = 0
    failed: int = 0
    pending: int = 0


@dataclass(frozen=True)
class LSquareError:
    """Why an event was dropped, failed or is pending, or why sending is held back. Never content."""

    code: str
    message: str
    event_id: str | None = None
    paths: tuple[str, ...] | None = None
    status: int | None = None
    resets_at: str | None = None
    count: int | None = None
    suppressed: int | None = None


class Logger(Protocol):
    def info(self, message: str) -> Any: ...


class _Gate:
    """A one-shot event whose `set()` never blocks: it releases a lock and takes none. Ending a
    wait, on one thread, then can't be held up by another thread (or a signal handler's call on
    this one) stopped inside `threading.Event.wait()`, which holds that Event's own lock on its way
    in and out. Set once, it stays set; any number of threads may wait."""

    __slots__ = ("_lock", "_set")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._lock.acquire()
        self._set = False

    def is_set(self) -> bool:
        return self._set

    def set(self) -> None:
        if self._set:
            return
        self._set = True
        try:
            self._lock.release()
        except RuntimeError:
            # Set twice at once: already open.
            pass

    def wait(self, timeout: float | None = None) -> bool:
        if self._set:
            return True
        if self._lock.acquire(timeout=-1 if timeout is None else max(0.0, timeout)):
            # Open again for the next waiter.
            try:
                self._lock.release()
            except RuntimeError:
                pass
        return self._set


class Signal:
    """Given to hooks: aborted at their deadline. `wait(timeout)` blocks until then (sync hooks)."""

    def __init__(self) -> None:
        #: A gate: aborting never blocks, even with this thread stopped inside wait().
        self._event = _Gate()
        #: Reentrant: shutdown() from a signal handler or finalizer can abort it from inside
        #: add_listener, on the same thread. Its hook's guard too: the hook's return is marked and
        #: its abort decided under it, and no other lock is taken under it, so marking never waits
        #: for the client's lock, nor for anything that lock is held across.
        self._lock = threading.RLock()
        self._listeners: list[Callable[[], Any]] = []
        #: Its hook returned or raised: from then on it is never aborted.
        self._returned = False

    @property
    def aborted(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)

    def add_listener(self, listener: Callable[[], Any]) -> None:
        with self._lock:
            if not self._event.is_set():
                self._listeners.append(listener)
                if not self._event.is_set():
                    return
                # Aborted in between, by a call back in on this thread: run here, unless that
                # abort took it.
                try:
                    self._listeners.remove(listener)
                except ValueError:
                    return
        _quietly(listener)

    def _mark_returned(self) -> None:
        """Its hook returned or raised: marked right away, where the hook ran, its task's
        done-callbacks and the client's step for its result coming only later."""
        with self._lock:
            self._returned = True

    def _while_running(self, publish: Callable[[], None]) -> bool:
        """Runs `publish` while its hook runs, neither returned nor aborted, under the lock that
        marks either: what the client does once one is marked (lets go of what `publish` put in
        place, uses it to cancel the hook) never comes before `publish`. True when it was aborted
        first: nothing was put in place, for the caller to stop the hook itself."""
        with self._lock:
            if self._returned:
                return False
            if self._event.is_set():
                return True
            publish()
            return False

    def _abort(self) -> list[Callable[[], Any]] | None:
        """Sets the flag; returns the listeners, which the client runs on its signal thread, away
        from its lock and its scheduler, and never behind a slow `on_error` or logger. None, and
        nothing aborted, once its hook has returned: there is nothing left to stop."""
        with self._lock:
            if self._returned:
                return None
            if self._event.is_set():
                return []
            self._event.set()
            listeners, self._listeners = self._listeners, []
        return listeners


def _run_all(listeners: list[Callable[[], Any]], depth: int) -> None:
    _depth.set(depth)
    for listener in listeners:
        _quietly(listener)


def _quietly(fn: Callable[[], Any]) -> None:
    try:
        swallow(fn())
    except BaseException as error:  # noqa: BLE001
        if interrupts(error):
            raise


def _enter_callback(depth: int, calls: contextvars.ContextVar[CallScope | None]) -> None:
    """In a context about to run the app's callback: that depth, and no current call."""
    _depth.set(depth)
    calls.set(None)


def _enqueue_from(client: LSquare, call: Mapping[str, Any], origin: Origin, diagnostic: LSquareError | None = None) -> None:
    """Run in a copy of the origin's context: queued as if from the call's own thread."""
    _depth.set(origin.depth)
    client._enqueue("tool_call", call, origin.loop, origin.other_loop, diagnostic=diagnostic)


@dataclass(eq=False)
class CallScope:
    """A `tools/call` in progress on an instrumented server, as `capture()` and `flush()` inside it
    see it. Lives in the client's own ContextVar: one client's calls never mix with another's. Once
    the call's event is built or dropped, the wrapper lets go of what it set here (`agent`,
    `thrown`, `tool_result`, `on_tool_entry`, `wrapper`), for good (`released`): a context that
    still holds the scope, as a turn captured inside the call does until it is sent, keeps only the
    call's ID and its boundary."""

    call_id: str
    #: The event loop running the call, for async hooks of captures made inside it.
    loop: asyncio.AbstractEventLoop | None = None
    #: The tool has returned: a `capture()` from now on is a turn of its own.
    returned: bool = False
    #: The first `capture()` before the tool returned: its IDs, and its agent part as it read it
    #: then (`_event.Captured`).
    agent: dict[str, Any] | None = None
    #: `flush()` was called inside the call: its event is sent before the reply.
    flush_requested: bool = False
    #: The turns captured inside the call before the tool returned and not yet sent or dropped, at
    #: most MAX_CALL_TURNS at once: the call's sending waits for them with its event. The client's
    #: own, filled and emptied by it alone, under its lock.
    turns: set[_Entry] = field(default_factory=set, repr=False)
    #: The tool's own function ran (high-level servers): an `isError` result without it is the SDK's.
    invoked: bool = False
    #: What the tool's own function raised, which the SDK turned into an `isError` result.
    thrown: tuple[BaseException] | None = None
    #: What the tool's own function returned, to tell its `isError` results from the SDK's.
    tool_result: tuple[Any] | None = None
    #: Run once when the tool's own function has been entered: `identify` starts then.
    on_tool_entry: Callable[[], None] | None = None
    #: The wrapper's own record of the call, for the layers it hooks inside the SDK.
    wrapper: Any = None
    #: The wrapper has let go of the above: a run of the tool's own function ending later (from a
    #: task the tool left running) still closes its boundary, and records nothing here.
    released: bool = False
    #: A run of the tool's own function has returned (high-level servers): outside a run the tool
    #: has returned, so a `capture()` from middleware after it is a turn of its own. Inside a later
    #: run (a round) it is inside again (see `mcp._boundary.RunScope`).
    ran: bool = False


@dataclass(frozen=True, eq=False)
class Origin:
    """Where a call ran, captured on its own thread: an event built later, elsewhere, is queued as
    if from there, and work sent back to it runs on its loop."""

    context: contextvars.Context
    depth: int
    #: The asyncio loop running the call.
    loop: asyncio.AbstractEventLoop | None
    #: A loop other than asyncio's (trio): async hooks can't run for it.
    other_loop: bool
    #: trio's token for that loop, to send work back to it from another thread.
    trio_token: Any = None


@dataclass(eq=False)
class HookRun:
    """A hook the wrapper started (`identify`): its outcome, and a way to stop it at its deadline."""

    #: The hook's return value, or what it raised. Shared: never cancelled by a waiter.
    future: concurrent.futures.Future[Any]
    signal: Signal
    abort: Callable[[], None]
    #: Fails it once the call's loop it was handed to has closed under it (parity-5): that loop
    #: never runs it on, nor settles it, so its slot comes back. Never raises.
    reclaim: Callable[[], None] = lambda: None


@dataclass(eq=False)
class _Batch:
    live: int
    abort: Abort
    #: Sent while a daily limit holds, to check whether it still does.
    probe: bool
    #: The hold-back it checks: its answer ends that one only.
    holdback: _Holdback | None
    #: The client's generation when it went out: a reply after a fork or shutdown is ignored.
    generation: int


@dataclass(eq=False)
class _Entry:
    seq: int
    depth: int
    #: The capturing context: the event's hook, requests, reports and logs run in a copy of it.
    context: contextvars.Context | None
    #: Monotonic seconds at capture, for deadlines.
    created_at: float
    event_id: str
    kind: str
    event: dict[str, Any] | None
    draft: Any
    bytes: int
    stage: Stage
    redacted: bool
    #: The loop an async `before_send` runs on.
    loop: asyncio.AbstractEventLoop | None
    #: Captured on a loop that isn't asyncio's (trio): an async hook can't run for it.
    other_loop: bool = False
    attempts: int = 0
    #: Monotonic seconds of the next attempt while waiting to retry.
    retry_at: float | None = None
    outcome: Outcome | None = None
    #: Finished while its hook was still running: keeps its slot and bytes until the hook settles.
    zombie: bool = False
    waiters: list[Callable[[], None]] | None = None
    #: Its hook's signal, also the hook's guard: once the hook has returned, nothing aborts it.
    #: This and `hook_cancel` are let go of once the hook has settled, or the event is dropped.
    hook_signal: Signal | None = None
    hook_cancel: Callable[[], None] | None = None
    batch: _Batch | None = None
    timer: Callable[[], None] | None = None
    ignored_fields: dict[str, int] = field(default_factory=dict)
    ignored_tool_call_fields: dict[str, int] = field(default_factory=dict)
    #: Its `before_send` run, from the hook's start until its slot comes back (see _HookRun).
    run: _HookRun | None = None
    #: Its hook returned in time: its deadline then waits for the finalization ceiling (see _expire).
    ceiling: bool = False


@dataclass(eq=False)
class _HookRun:
    """One `before_send` run, from its start until its slot comes back (py-runtime-2). Each step is
    decided under `lock`, its signal's own, the hook's guard, under which no other lock is taken: the
    hook's return is marked there, on the hook's thread or its loop, never under the client's lock,
    and the client's deadline, its finalization ceiling, shutdown() and a closed loop decide there
    what of it is still running.

    `state`: "starting" (the hook not yet called, its event not yet copied), "running" (the hook
    runs, or its awaitable does), "returned" (it returned or raised, at `returned_at`, its result
    not yet handed on), "queued" (its result waits for a worker to read it, or, with none to take
    it, for its failure to be published: let go of by any terminal path), "processing" (a worker
    reads and rebuilds it, until the result is published under the client's lock), "done" (its
    result published or let go of, and its slot back)."""

    lock: Any
    state: str = "starting"
    #: When the hook returned or raised, on the client's clock, read under `lock`: whether it was in
    #: time is decided by this, never by how long what follows takes.
    returned_at: float | None = None
    #: The event to copy for the hook, until a worker takes it; then what the hook returned, until a
    #: worker reads it. Emptied on a terminal path: neither is kept for a run let go of.
    source: list[Any] = field(default_factory=list)
    output: list[Any] = field(default_factory=list)
    #: The app's loop an async hook was handed to (parity-5), until the run is done.
    loop: asyncio.AbstractEventLoop | None = None

    def mark_returned(self, signal: Signal, clock: Callable[[], float]) -> None:
        """The hook returned or raised: marked where it ran, at once, the time read under the lock,
        so a deadline decided under it before this is never beaten by a return marked after."""
        with self.lock:
            signal._returned = True
            if self.returned_at is None:
                self.returned_at = clock()
            if self.state in ("starting", "running"):
                self.state = "returned"


@dataclass(eq=False)
class _Wait:
    deadline: float
    pending: set[_Entry]
    ended: _Gate = field(default_factory=_Gate)
    on_end: list[Callable[[], None]] = field(default_factory=list)

    def end(self) -> None:
        if self.ended.is_set():
            return
        self.ended.set()
        for callback in self.on_end:
            _quietly(callback)


@dataclass(eq=False)
class _FinishingCall:
    """A call that has replied, whose event the wrapper is still building."""

    done: concurrent.futures.Future[None]
    #: Its event's ID, which a flush reports it by while it is unbuilt.
    call_id: str
    #: Where the call ran: a flush reports it from there while it is unbuilt, its context, depth
    #: and loop, as it reports a queued event from where that was captured.
    origin: Origin
    finished: bool = False
    #: Its event, once queued: kept so a flush counts it even after it leaves the queue.
    entry: _Entry | None = None
    #: Takes the call's deferred build and lets go of it, unbuilt: shutdown() ends it this way (see
    #: _shutdown_finish). None for a wrapper that gives none.
    discard: Callable[[], None] | None = None


@dataclass(eq=False)
class _Holdback:
    """Holding back after `quota_exceeded`, until `until` (epoch ms) or a check finds the limit raised."""

    until: float
    resets_at: str
    #: Monotonic seconds after which the next event captured becomes the check.
    next_check: float
    #: The check's event; a bare object while one is reserved but not yet built.
    check: _Entry | object | None = None
    dropped: int = 0


class _RateLimit:
    """A burst of 100, then 100 a second, per client: a loop the depth can't see dies out. And at
    most 100 held at once, from admission until the handler returns (an async one until it
    settles, or its loop closes under it), with event copies of at most `max_bytes` between them: a
    handler slower than what it's given can't build a backlog on the callback thread. One past the
    rate or the 100 is dropped and counted; one past the bytes goes without its copy."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        #: Reentrant: a signal handler or finalizer that reports or logs can run while this thread
        #: holds it.
        self._lock = threading.RLock()
        self._tokens = float(CALLBACKS_PER_SECOND)
        self._last = clock()
        #: Admitted and not yet released, and the bytes of the event copies they hold.
        self.held = 0
        self.held_bytes = 0
        self.suppressed = 0

    def take(self, size: int | None = None, max_bytes: int = 0) -> _Slot | None:
        """Admits one, with an event copy of `size` bytes (None: it has none), and hands it the
        count of those dropped before it; None when it's dropped, and counted. Past `max_bytes`
        held it holds no copy, though one alone always fits."""
        with self._lock:
            now = self._clock()
            # A clock that went back (one a test injects) refills nothing, rather than owing tokens,
            # and dropping everything, for as long as it went back.
            self._tokens = min(CALLBACKS_PER_SECOND, self._tokens + max(0.0, now - self._last) * CALLBACKS_PER_SECOND)
            self._last = now
            if self._tokens < 1 or self.held >= CALLBACKS_PER_SECOND:
                self.suppressed += 1
                return None
            if size is not None and self.held_bytes > 0 and self.held_bytes + size > max_bytes:
                size = None
            self._tokens -= 1
            self.held += 1
            self.held_bytes += size or 0
            slot = _Slot(self, self.suppressed, size)
            self.suppressed = 0
            return slot

    def release(self, slot: _Slot, refused: bool) -> None:
        with self._lock:
            if slot.released:
                return
            slot.released = True
            self.held -= 1
            self.held_bytes -= slot.size or 0
            if refused:
                self.suppressed += slot.suppressed


class _Slot:
    """A report's or log line's place in its `_RateLimit`, from admission until its handler
    returns, or settles if it's async. Released once, whichever path gets there first."""

    __slots__ = ("_limit", "released", "size", "suppressed")

    def __init__(self, limit: _RateLimit, suppressed: int, size: int | None) -> None:
        self._limit = limit
        #: How many were dropped before it was admitted: it carries the count.
        self.suppressed = suppressed
        #: The bytes of the event copy it holds; None when it holds none.
        self.size = size
        self.released = False

    def release(self, refused: bool = False) -> None:
        """`refused`: it was never delivered, so the count it carried goes to the next one."""
        self._limit.release(self, refused)


class Internal:
    """What the wrapper (`lsquare.mcp`) uses. It keeps no state of its own: what it needs is here."""

    def __init__(self, client: LSquare) -> None:
        self._client = client
        self.mode: Mode = client._mode
        self.flush_timeout_ms: float = client._flush_timeout_ms
        self.max_concurrent_hooks: int = client._max_concurrent_hooks

    @property
    def identifying(self) -> dict[str, int]:
        """`identify` hooks running now, for their limit. The client's, so a fork resets it."""
        return self._client._identifying

    @property
    def identified(self) -> dict[str, tuple[str, ...] | None]:
        """The ID fields the last successful `identify` returned."""
        return self._client._identified

    @property
    def closed(self) -> bool:
        """`shutdown()` has run: identify isn't called from here, and every event is dropped,
        which is reported once."""
        return self._client._closed

    def origin(self) -> Origin:
        """The calling thread's context, callback depth and loop, as an event queued here would see them."""
        loop = _running_loop()
        other = loop is None and _other_loop_running()
        token = None
        if other:
            try:
                token = sys.modules["trio"].lowlevel.current_trio_token()
            except Exception:
                token = None
        return Origin(contextvars.copy_context(), current_depth(), loop, other, token)

    def record(
        self,
        call: Mapping[str, Any],
        loop: asyncio.AbstractEventLoop | None = None,
        origin: Origin | None = None,
        diagnostic: LSquareError | None = None,
    ) -> None:
        """Queues a call's event. With an `origin`, as if from there: its context, depth and loop,
        whatever thread this runs on. Else its async hooks run on `loop`, else the call scope's,
        else the running one. `diagnostic`, what the call reports of its `identify`, is reported
        unless the event is refused for `shutdown()` (parity-25): decided under the lock that
        admits it, as the client's other reports about it are, so a shutdown landing between
        building the call's record and queuing it can't make it claim the call was sent."""
        client = self._client
        try:
            if origin is not None:
                origin.context.copy().run(_enqueue_from, client, call, origin, diagnostic)
                return
            if loop is None:
                scope = client._calls.get()
                loop = scope.loop if scope is not None else None
            client._enqueue("tool_call", call, loop, diagnostic=diagnostic)
        except Exception:
            client._report(LSquareError("internal_error", "Recording a tool call failed unexpectedly; its event was dropped."))

    def identify(self, hook: Callable[..., Any], make_input: Callable[[Signal], Any], origin: Origin) -> HookRun | None:
        """Starts `identify` as a hook: a sync one on its own threads, an async one as a task on the
        call's loop (never on trio), in a copy of the call's context, one level deeper and outside
        the call. `None` past its limit, or after `shutdown()`, when it isn't called. Its slot is
        held until it settles; its deadline or `shutdown()` aborts it while it runs, never once it
        has returned. While it runs it holds its own copy of the context, never `origin`, and
        `make_input` only until the hook is called with what it made."""
        client = self._client
        signal = Signal()
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        #: Reentrant: shutdown() from a signal handler can abort the hook on a thread inside one of
        #: these sections. No other lock is taken under it.
        guard = threading.RLock()
        state: dict[str, Any] = {"settled": False, "cancel": None, "aborted": False, "generation": None, "loop": None}
        context = origin.context.copy()
        depth = origin.depth + 1
        call_loop, other_loop = origin.loop, origin.other_loop
        context.run(_enter_callback, depth, client._calls)
        #: Taken out as the hook is called, so only the hook holds its input from then on.
        inputs = [make_input]
        run: HookRun

        def invoke() -> Any:
            # The input is made inline, held by no name here: a hook that deletes it lets it go
            # (on Python 3.10 a call's argument stays on its caller's stack until it returns).
            return hook(inputs.pop()(signal))

        def returned() -> None:
            # The hook returned or raised: from here nothing aborts its signal, though an async one's
            # task settles only a loop iteration later. Marked under the signal's own lock, as the
            # abort is decided.
            signal._mark_returned()

        def settle(error: BaseException | None, out: Any) -> None:
            returned()
            # One that never ran (refused, aborted first) won't take it.
            inputs.clear()
            with client._lock:
                if state["settled"]:
                    return
                state["settled"] = True
                client._identify_runs.discard(run)
                if state["generation"] == client._generation:
                    client._identifying["active"] -= 1
            try:
                if error is not None:
                    future.set_exception(error)
                else:
                    future.set_result(out)
            except concurrent.futures.InvalidStateError:
                pass

        def abort() -> None:
            try:
                listeners = signal._abort()
                if listeners is None:
                    # It answered: there is nothing left to stop, and its signal never fires.
                    return
                with guard:
                    state["aborted"] = True
                    cancel = state["cancel"]
                if listeners:
                    run_in = context.copy()
                    client._signal(lambda: run_in.run(_run_all, listeners, depth))
                if cancel is not None:
                    _quietly(cancel)
            finally:
                # Its cancel can't reach a loop that has closed under it: settled here instead.
                _quietly(reclaim)

        def reclaim() -> None:
            # Handed to the call's loop, which has closed without running its tasks to their end
            # (`run_until_complete()`, then `close()`): nothing there runs it on, nor settles it, so
            # it fails, and its slot comes back. One on a loop that has only stopped keeps its slot:
            # that loop may run again. Settled once: a no-op once it has.
            with guard:
                handed = state["loop"]
            if handed is not None and handed.is_closed():
                settle(RuntimeError("The call's event loop closed before identify settled."), None)

        def run_async(make: Callable[[], Any], made: Any = None) -> None:
            with guard:
                aborted = state["aborted"]
            if aborted:
                # Aborted before its task could start: it never does.
                if inspect.iscoroutine(made):
                    made.close()
                settle(asyncio.CancelledError(), None)
                return
            loop: asyncio.AbstractEventLoop | PrivateLoop | None = call_loop
            if call_loop is not None and call_loop.is_closed():
                # The call's loop is gone: the hook fails, rather than moving to another loop.
                settle(RuntimeError("The call's event loop is closed."), None)
                return
            if loop is None:
                if other_loop:
                    client._warn_once("async-identify-loop", "[lsquare] An async identify needs asyncio; it isn't run on other event loops. Use a sync one there.")
                    settle(RuntimeError("not asyncio"), None)
                    return
                # If its thread can't start, the hook fails, whichever call started it.
                loop = client._private_loop
            else:
                # The app's: should it close without running the hook on, the slot comes back.
                with guard:
                    state["loop"] = call_loop
            try:
                cancel = schedule_on(loop, make, context, settle)
            except Exception as error:
                settle(error, None)
                return
            with guard:
                state["cancel"] = cancel
                aborted = state["aborted"]
            if aborted:
                _quietly(cancel)

        run = HookRun(future, signal, abort, reclaim)
        # Those held by a loop that has closed under them fail first, and give their slots back,
        # outside the lock: settling runs the wrapper's callbacks.
        with client._lock:
            held = list(client._identify_runs)
        for other in held:
            _quietly(other.reclaim)
        # Admitted and registered at once, so a shutdown either refuses it or aborts it.
        with client._lock:
            if client._closed or client._identifying["active"] >= client._max_concurrent_hooks:
                return None
            client._identifying["active"] += 1
            state["generation"] = client._generation
            client._identify_runs.add(run)
        if inspect.iscoroutinefunction(hook):
            run_async(lambda: _watched(invoke, returned))
            return run

        def run_sync() -> None:
            with guard:
                aborted = state["aborted"]
            if aborted:
                # Aborted (its deadline, or shutdown) before a thread took it: it never runs.
                settle(asyncio.CancelledError(), None)
                return
            try:
                out = context.run(invoke)
            except BaseException as error:  # noqa: BLE001 - the hook's failure
                settle(error, None)
                return
            if inspect.isawaitable(out):
                run_async(lambda: _watched(lambda: out, returned), out)
                return
            settle(None, out)

        if not client._identify_hooks.submit(run_sync):
            settle(RuntimeError("No thread can start for identify."), None)
        return run

    def call_on_loop(self, origin: Origin, fn: Callable[[], None]) -> bool:
        """Runs `fn` on the origin's loop thread, in a copy of its context. False when that loop
        can't take it; true doesn't promise it runs, as the loop may stop first."""
        context = origin.context.copy()
        try:
            if origin.loop is not None:
                if origin.loop.is_closed():
                    return False
                origin.loop.call_soon_threadsafe(context.run, fn)
                return True
            if origin.trio_token is not None:
                origin.trio_token.run_sync_soon(context.run, fn)
                return True
        except Exception:
            return False
        return False

    def call_later(self, delay_s: float, fn: Callable[[], None], refused: Callable[[], None] | None = None) -> Callable[[], None]:
        """Runs `fn` on one of the client's worker threads after `delay_s`; when no worker can take
        it, runs `refused` instead, on the scheduler: it must be quick. Returns a cancel."""
        client = self._client

        def dispatch() -> None:
            if not client._builds.submit(fn) and refused is not None:
                _quietly(refused)

        return client._scheduler.call_later(max(0.0, delay_s), dispatch)

    def timer(self, delay_s: float, fn: Callable[[], None]) -> Callable[[], None]:
        """Runs a quick `fn` on the scheduler after `delay_s`; returns a cancel."""
        return self._client._scheduler.call_later(max(0.0, delay_s), lambda: _quietly(fn))

    async def wait(self, future: concurrent.futures.Future[Any], timeout_s: float) -> None:
        """Waits up to `timeout_s` for `future` without blocking the loop. Cancelling the wait ends it
        at once and never cancels `future`, which others may share."""
        proxy: concurrent.futures.Future[None] = concurrent.futures.Future()
        future.add_done_callback(lambda _: _resolve(proxy))
        if timeout_s <= 0 or proxy.done():
            return
        if _running_loop() is not None:
            try:
                await asyncio.wait_for(asyncio.wrap_future(proxy), timeout_s)
            except asyncio.TimeoutError:
                pass
            return
        if _other_loop_running():
            await _on_trio(lambda wake: proxy.add_done_callback(lambda _: wake()), proxy.done, timeout_s)
            return
        concurrent.futures.wait([proxy], timeout=timeout_s)

    def calls(self) -> contextvars.ContextVar[CallScope | None]:
        return self._client._calls

    def settled(self, event_id: str, timeout_ms: float, turns: Iterable[_Entry] = ()) -> concurrent.futures.Future[None]:
        """Done once the event with this ID, and each of a call's `turns` (see `CallScope`) still
        queued, is sent or dropped, or after `timeout_ms`."""
        return self._client._settled(event_id, timeout_ms, turns)

    def turns(self, scope: CallScope) -> frozenset[_Entry]:
        """The call's turns still being sent (see `CallScope`), read under the lock that fills and
        empties them: what waits for them then holds these, never the call."""
        with self._client._lock:
            return frozenset(scope.turns)

    def delivered(self) -> concurrent.futures.Future[None]:
        """Done once the reports and log lines raised so far have reached `on_error` or the logger,
        an async one having started (see `LSquare._delivered`)."""
        return self._client._delivered()

    def track(
        self,
        call_id: str,
        finishing: concurrent.futures.Future[None],
        origin: Origin | None = None,
        discard: Callable[[], None] | None = None,
    ) -> None:
        """A call whose event is still being built: flush() and shutdown() wait for it and count it.
        `origin`: where the call ran, by default the calling thread (see `origin()`). `discard`
        takes its deferred build and lets go of it unbuilt, `finishing` done: shutdown() calls it
        for one still unbuilt as it ends, before it stops the client's threads (py-mcp-19), and
        this calls it at once, the shutdown reported, for one tracked once the client is closed."""
        self._client._track(call_id, finishing, origin, discard)

    def flush(self, timeout_ms: float) -> FlushResult:
        return self._client.flush(timeout_ms=timeout_ms)

    async def aflush(self, timeout_ms: float) -> FlushResult:
        return await self._client.aflush(timeout_ms=timeout_ms)

    def callback(self, run: Callable[[], Any]) -> Any:
        """Runs the app's callback (`identify`) as a callback: one level deeper, outside any call."""
        client = self._client

        def deeper() -> Any:
            _depth.set(current_depth() + 1)
            client._calls.set(None)
            return run()

        context = contextvars.copy_context()
        out = context.run(deeper)
        if inspect.isawaitable(out) and _running_loop() is not None:
            # Run as a task made in the isolated context, so the callback's own awaits never see
            # the call either.
            return context.run(asyncio.ensure_future, out)
        return out

    def log(self, level: str, message: str) -> None:
        self._client._log(level, message)

    def warn_once(self, key: str, message: str) -> None:
        self._client._warn_once(key, message)

    def report(self, error: LSquareError) -> None:
        self._client._report(error)

    def valid_id(self, value: Any) -> bool:
        return _id_ok(value)

    def turn_id(self, value: Any) -> str | None:
        """A turnId as events keep it: a non-empty string or a safe integer, as a string, within limits."""
        return _turn_id(value)

    def turn_key(self, org_id: str | None, conversation_id: str, turn_id: str) -> str:
        return str(_normalize.turn_key(org_id, conversation_id, turn_id))


class LSquare:
    """LSquare's client: `capture()` records an agent's turn; `lsquare.mcp.instrument()` records tool calls.

    Never raises. Without an `api_key`, or with an invalid endpoint (outside dry run), it's disabled.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        endpoint: str | None = None,
        dry_run: bool | None = None,
        flush_timeout_ms: float | None = None,
        before_send: Callable[..., Any] | None = None,
        before_send_timeout_ms: float | None = None,
        max_concurrent_hooks: int | None = None,
        max_concurrent_requests: int | None = None,
        redact: Literal[False] | Mapping[str, bool] | None = None,
        redact_keys: Iterable[str] | None = None,
        on_error: Callable[..., Any] | None = None,
        logger: Any = None,
        max_queue_events: int | None = None,
        max_queue_bytes: int | None = None,
        max_event_bytes: int | None = None,
        _transport: Callable[[bytes, float, Abort], Reply] | None = None,
        _max_attempts: int | None = None,
        _request_timeout_ms: float | None = None,
        _clock: Callable[[], float] | None = None,
        _now: Callable[[], float] | None = None,
        _random: Callable[[], float] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._mode: Mode = "disabled"
        self._clock: Callable[[], float] = _clock or time.monotonic
        #: Wall clock, epoch milliseconds: `resetsAt`, UTC midnight, a dated `Retry-After`.
        self._now: Callable[[], float] = _now or (lambda: time.time() * 1000)
        self._random: Callable[[], float] = _random or random.random
        self._logger: Any = StderrLogger()
        #: The app's own logger: its calls are callbacks, run at a depth.
        self._custom_logger = False
        self._on_error: Callable[..., Any] | None = None
        self._on_error_takes_event = True
        self._before_send: Callable[..., Any] | None = None
        self._before_send_takes_signal = True
        self._before_send_is_async = False
        self._flush_timeout_ms: float = DEFAULTS["flush_timeout_ms"]
        self._before_send_timeout_ms: float = DEFAULTS["flush_timeout_ms"]
        self._max_concurrent_hooks: int = DEFAULTS["max_concurrent_hooks"]
        self._max_concurrent_requests: int = DEFAULTS["max_concurrent_requests"]
        self._max_queue_events: int = DEFAULTS["max_queue_events"]
        self._max_queue_bytes: int = DEFAULTS["max_queue_bytes"]
        self._max_event_bytes: int = DEFAULTS["max_event_bytes"]
        self._max_attempts: int = DEFAULTS["max_attempts"]
        self._request_timeout_ms: float = DEFAULTS["request_timeout_ms"]
        self._transport = _transport
        self._exchange: Exchange | None = None
        self._context: Any = None
        self._calls: contextvars.ContextVar[CallScope | None] = contextvars.ContextVar("lsquare_call", default=None)
        self._internal_api: Internal | None = None
        #: The keys warned once, each set in one step: see _warn_once.
        self._warned: dict[str, object] = {}
        #: Set by the exit flush while it runs, and by a sync shutdown() for its own timeout: a
        #: request then takes only what's left of it, and one no sending thread can take is sent by
        #: the scheduler's thread, or the thread that took the scheduler over (see _resume_at_exit),
        #: instead.
        self._exit_deadline: float | None = None
        #: Set by the client's own exit flush (see _exit_flush) while it runs, and by nothing else:
        #: the one phase in which a hook's copy, or what it returned, may be handled on the thread
        #: that got to it when no hook thread can take it (see _at_exit). A sync shutdown() sets
        #: `_exit_deadline`, never this.
        self._exit_flushing = False
        #: What the constructor has made of the client's infrastructure (see _infrastructure).
        self._made: set[str] = set()
        self._reset_state()
        try:
            if logger is not None:
                self._logger = logger
                self._custom_logger = True
            if callable(on_error):
                self._on_error = on_error
                self._on_error_takes_event = _accepts(on_error, 2)
            self._flush_timeout_ms = _wait_time(flush_timeout_ms, DEFAULTS["flush_timeout_ms"])
            # When flush() doesn't wait (0), a hook still gets the default time.
            self._before_send_timeout_ms = _duration(
                before_send_timeout_ms, self._flush_timeout_ms or DEFAULTS["flush_timeout_ms"]
            )
            self._max_concurrent_hooks = _positive_int(max_concurrent_hooks, DEFAULTS["max_concurrent_hooks"])
            self._max_concurrent_requests = _positive_int(max_concurrent_requests, DEFAULTS["max_concurrent_requests"])
            self._max_queue_events = _positive_int(max_queue_events, DEFAULTS["max_queue_events"])
            self._max_queue_bytes = _positive_int(max_queue_bytes, DEFAULTS["max_queue_bytes"])
            self._max_event_bytes = _positive_int(max_event_bytes, DEFAULTS["max_event_bytes"])
            self._max_attempts = _positive_int(_max_attempts, DEFAULTS["max_attempts"])
            self._request_timeout_ms = _duration(_request_timeout_ms, DEFAULTS["request_timeout_ms"])
            if callable(before_send):
                self._before_send = before_send
                self._before_send_takes_signal = _accepts(before_send, 2)
                self._before_send_is_async = inspect.iscoroutinefunction(before_send)
            # First: what follows may log, and an iterable of key names that raises ends here.
            self._infrastructure()
            self._context = _event.Context(
                sdk={"name": SDK_NAME, "version": SDK_VERSION, "language": "python", "runtime": runtime_name()},
                patterns=_redact.resolve_patterns(redact),
                redact_keys=[],
                max_event_bytes=self._max_event_bytes,
            )
            if is_pyodide():
                # Written at once: no thread may start here, the callback thread included. Before the
                # key names are read, since an iterable of them that raises would log through one.
                # In a copy of the context, so the depth a custom logger runs at stays its own.
                contextvars.copy_context().run(
                    self._write_log, "warn", "[lsquare] Pyodide (such as Cloudflare Python Workers) has no threads or sockets: capture is disabled.", 1
                )
                return
            keys = _redact.read_keys(redact_keys)
            if keys is not None:
                self._context = replace(self._context, redact_keys=keys[0])
            if keys is None:
                # Never sent without the masking the developer asked for.
                self._log("warn", "[lsquare] redact_keys must be a list of key names: capture is disabled.")
                return
            if keys[1]:
                self._log("warn", "[lsquare] Part of redact_keys isn't a key name: it is ignored.")
            key = api_key.strip() if isinstance(api_key, str) else ""
            dry = dry_run if isinstance(dry_run, bool) else os.environ.get("LSQUARE_DRY_RUN", "") in ("1", "true")
            if dry:
                self._mode = "dry-run"
                self._arm_exit()
                return
            if not key:
                self._log("warn", "[lsquare] No api_key: capture is disabled.")
                return
            resolved = _resolve_endpoint(endpoint or os.environ.get("LSQUARE_ENDPOINT") or DEFAULT_ENDPOINT)
            if isinstance(resolved, _Problem):
                self._log("warn", f"[lsquare] {resolved.problem}: capture is disabled.")
                return
            self._mode = "send"
            self._url = resolved.url
            self._exchange = Exchange(
                resolved.url,
                key,
                f"lsquare-python/{SDK_VERSION} ({runtime_name()})",
                self._resolver,
                self._scheduler.call_later,
                self._clock,
            )
            self._arm_exit()
        except Exception:
            self._mode = "disabled"
            try:
                # An option that failed before it was made (one whose read raised): the warning
                # needs it, and so does shutdown().
                self._infrastructure()
            except Exception:
                pass
            self._log("warn", "[lsquare] Invalid options: capture is disabled.")

    def __repr__(self) -> str:
        # Never shows the key or queued content.
        return f"<LSquare {self._mode}>"

    # -- State -----------------------------------------------------------------------------------

    def _reset_state(self) -> None:
        self._live: dict[int, _Entry] = {}
        self._live_bytes = 0
        self._zombies = 0
        self._zombie_bytes = 0
        #: The zombies, watched until their hook settles: one whose async hook an app's loop held
        #: when that loop closed, running or with its settling queued, gives its slot back then
        #: (see _reclaim_hooks).
        self._hung: set[_Entry] = set()
        self._active_hooks = 0
        self._active_requests = 0
        #: Events waiting for a request slot, in the order they became ready.
        self._queued: dict[_Entry, None] = {}
        self._pump_scheduled = False
        #: The pump armed after no sending thread could take a batch (see _send_batch), while it
        #: waits, and the delay of the next one. shutdown() drops it with the scheduler's timers,
        #: and nothing is queued after it; a fork resets both.
        self._repump: Callable[[], None] | None = None
        self._repump_ms: float = RETRY_BASE_MS
        self._holdback: _Holdback | None = None
        self._waits: set[_Wait] = set()
        self._seq = 0
        self._closed = False
        self._reported_closed = False
        self._report_limit = _RateLimit(self._clock)
        self._log_limit = _RateLimit(self._clock)
        #: Async loggers and `on_error`s scheduled on a loop and not finished yet, each with that loop
        #: and what lets go of it if the loop closes first (see _retire_closed), and their lock.
        self._callback_futures: dict[concurrent.futures.Future[Any], tuple[asyncio.AbstractEventLoop, Callable[[], None]]] = {}
        self._futures_lock = threading.RLock()
        #: Of those, the ones that haven't started yet, each done once it has, with the loop it was
        #: handed to (None: the private loop) and when (monotonic). Under `_futures_lock`.
        self._unstarted: dict[concurrent.futures.Future[None], tuple[asyncio.AbstractEventLoop | None, float]] = {}
        #: `_delivered()`'s turns on the callback thread still to come, each with when it was
        #: handed over (monotonic). Under `_futures_lock`.
        self._barriers: list[tuple[float, concurrent.futures.Future[None]]] = []
        self._last_error_log = float("-inf")
        self._suppressed_errors = 0
        self._finishing_calls: dict[str, _FinishingCall] = {}
        #: `identify` hooks running now, and the ID fields the last successful one returned.
        self._identifying: dict[str, int] = {"active": 0}
        self._identified: dict[str, tuple[str, ...] | None] = {"keys": None}
        #: The `identify` hooks still running, for shutdown() to abort.
        self._identify_runs: set[HookRun] = set()
        self._generation = getattr(self, "_generation", 0) + 1

    def _pools(self) -> None:
        self._scheduler = Scheduler(self._clock)
        self._senders = Pool("sender", self._max_concurrent_requests)
        self._hooks = Pool("hook", self._max_concurrent_hooks)
        #: `identify`'s own, so stuck `before_send` hooks never keep it from starting.
        self._identify_hooks = Pool("identify", self._max_concurrent_hooks)
        #: Builds a call's event when its loop can't (the wrapper's fallback).
        self._builds = Pool("build", 1)
        self._callbacks = Pool("callback", 1)
        #: Hooks' abort listeners: their own thread, so a slow `on_error` or logger never holds
        #: back a hook's cancellation. At most twice `max_concurrent_hooks` jobs wait there (as
        #: many as aborting every hook of both hook pools at once makes), and at least 100.
        self._signals = Pool("signal", 1, max(CALLBACKS_PER_SECOND, 2 * self._max_concurrent_hooks))
        # Held weakly: its thread stops the loop once the client is collected (see PrivateLoop).
        self._private_loop = PrivateLoop(weakref.ref(self))
        self._resolver = Resolver(start_thread, self._clock)

    def _arm_exit(self) -> None:
        reference = weakref.ref(self)

        def at_exit() -> None:
            client = reference()
            if client is not None:
                client._exit_flush()

        atexit.register(at_exit)

    def _infrastructure(self) -> None:
        """The threads' pools, then the reset a forked child gets, each made once, whichever way
        the constructor ends: a disabled client's warning starts its callback thread, whose pool
        lock a parent's thread may hold at a fork, and shutdown() stops every pool. Before
        anything logs."""
        if "pools" not in self._made:
            self._pools()
            self._made.add("pools")
        if "fork" not in self._made:
            self._arm_fork()
            self._made.add("fork")

    def _arm_fork(self) -> None:
        # Held weakly by the module's one handler (see _reset_forked): a registration of its own
        # could never be undone, and would stay for the life of the process.
        _register_forkable(self)

    def _exit_flush(self) -> None:
        """Interpreter exit: flush within the flush timeout (2 s when that is 0, as shutdown() waits),
        on the threads already running. Then
        the client goes quiet, and what it already handed to the callback and signal threads (the
        flush's own report, say), with any async logger or `on_error` that started, gets
        EXIT_LOG_GRACE_S to finish: the interpreter aborts if a daemon thread is writing to stderr as
        it finalizes. The default logger can't be caught mid-write (it writes to the descriptor); a
        custom one's lines are best effort."""
        armed = False
        try:
            if self._closed or self._mode == "disabled":
                return
            with self._lock:
                if not self._live and not self._finishing_calls:
                    return
                # Terminal, as shutdown(): 2 s when flush() doesn't wait (0), so a request path
                # that doesn't wait still lets the exit send what's queued.
                self._exit_deadline = self._clock() + (self._flush_timeout_ms or DEFAULTS["flush_timeout_ms"]) / 1000
                self._exit_flushing = True
                armed = True
            # What this resumes runs within the flush's time: one budget for both. At least 1 ms: a
            # flush given 0 doesn't report what it leaves pending, and this one did wait.
            self._resume_at_exit()
            # Without flush()'s last wait, for what it reported to be delivered: the drain below is
            # that wait, within the same grace.
            self._flush(max(1.0, (self._exit_deadline - self._clock()) * 1000))
        except BaseException:  # noqa: BLE001 - the interpreter is exiting already
            # A SystemExit or KeyboardInterrupt here (a second signal) ends the flush. Passing it
            # on from atexit would only print it: the exit goes on either way.
            pass
        finally:
            try:
                self._scheduler.give_back()
                if armed:
                    # Its deadline is its own: a shutdown() in an app's atexit handler that runs
                    # after it (one registered before the client) sends within its own timeout.
                    # Without the lock, which a stuck step may hold.
                    self._exit_deadline = None
                    self._exit_flushing = False
                until = time.monotonic() + EXIT_LOG_GRACE_S
                # Quiet from here: neither thread takes another job, each pool checking under its own
                # lock, never held while a thread starts. A daemon thread writing to stderr while the
                # interpreter finalizes aborts it.
                self._callbacks.refuse()
                self._signals.refuse()
                self._callbacks.drain(EXIT_LOG_GRACE_S)
                self._signals.drain(max(0.0, until - time.monotonic()))
                # The pools' jobs have scheduled every async callback they will: wait for those,
                # except the ones a closed loop will never end.
                self._retire_closed()
                with self._futures_lock:
                    running = list(self._callback_futures)
                if running:
                    concurrent.futures.wait(running, timeout=max(0.0, until - time.monotonic()))
            except BaseException:  # noqa: BLE001
                pass

    def _after_fork(self) -> None:
        """In a forked child: a client with nothing in it. The parent sends what it had."""
        self._lock = threading.RLock()
        self._reset_state()
        # A parent's sync shutdown() sets it too: the child's requests take their own timeout.
        self._exit_deadline = None
        self._exit_flushing = False
        self._scheduler.reset()
        self._senders.reset()
        self._hooks.reset()
        self._identify_hooks.reset()
        self._builds.reset()
        self._callbacks.reset()
        self._signals.reset()
        self._private_loop.reset()
        self._resolver.reset()
        # A new variable: the child's inherited current call belongs to the parent.
        self._calls = contextvars.ContextVar("lsquare_call", default=None)
        if self._internal_api is not None:
            self._internal_api = None

    # -- Public API ------------------------------------------------------------------------------

    def capture(self, fields: Mapping[str, Any]) -> None:
        """Sends a turn: what an agent did. Never raises and never waits on the network. Inside a
        call on an instrumented server, the first capture before the tool returns becomes the
        call's `agent` part; any other is a turn linked to the call."""
        try:
            scope = self._calls.get() if current_depth() == 0 else None
            if scope is not None and isinstance(fields, Mapping):
                # A UUID ID becomes its string now: the wrapper groups the call by it. Each key read
                # once and strictly, by the reader a capture outside a call uses: a read that raises
                # raises, as a getter's throw does in TypeScript, and the capture is dropped below
                # (internal_error) rather than sent without that field, an ID perhaps. A value that
                # can't be read as what it says it is becomes UNREADABLE (`_values.readable`).
                read = _normalize.app_ids(_jsjson.read_fields(fields, CAPTURE_FIELDS, strict=True))
                captured = _event.Captured(read)
                with self._lock:
                    # `ran` read by name: a scope another copy of the SDK made may not have it.
                    mine = not scope.returned and getattr(scope, "ran", False) is not True and scope.agent is None
                    if mine:
                        # Taken once its top-level fields are read, before they're shaped: a
                        # capture() from the app's code that shaping runs (a model's fields, a
                        # lazy object inside them) is a turn of its own. One that reading a
                        # top-level field ran came first, and may have taken it.
                        scope.agent = captured
                if mine:
                    # The call's agent part, read now, as any capture()'s data is, not when the
                    # call's event is built: what the app does with its objects afterwards changes
                    # nothing. Outside the lock, as it runs the app's code.
                    if self._mode != "disabled":
                        try:
                            captured.snapshot = _event.shape_agent(read, self._context)
                        except Exception:
                            # Final too: never read again, as what the app holds later may not be
                            # what it gave. The call's event is dropped when it is built.
                            captured.snapshot = _event.CAPTURE_FAILED
                    return
                # Left out, None and an empty one alike are absent: linked to the call. A given
                # ID, valid or not (the literal "empty" included), is kept for the checks.
                given = _normalize.id_value(read.get("callId"))
                if given is None or given == "empty":
                    read["callId"] = scope.call_id
                entry = self._enqueue("turn", read, scope.loop)
                if entry is not None:
                    self._follow(scope, entry)
                return
            self._enqueue("turn", fields)
        except Exception:
            try:
                self._report(LSquareError("internal_error", "capture() failed unexpectedly; the event was dropped."))
            except Exception:
                pass

    def current_call_id(self) -> str | None:
        """Inside a tool on an instrumented server: the call's ID, so a turn captured later elsewhere
        can pass it as `callId`. `None` outside a call."""
        try:
            scope = self._calls.get()
            return scope.call_id if scope is not None else None
        except Exception:
            return None

    def _lsquare_internal(self) -> Internal:
        """For the wrapper only."""
        if self._internal_api is None:
            self._internal_api = Internal(self)
        return self._internal_api

    def flush(self, timeout_ms: float | None = None) -> FlushResult:
        """Waits up to `timeout_ms` for the events captured before the call, except those whose next
        retry is after the deadline. `0` doesn't wait; a `timeout_ms` it can't read counts as not
        given. What is still pending when it returns is reported, unless given `0`: a call's still
        waiting for `identify` too, by the ID its event will have. Never raises. Blocks the calling
        thread: on an event loop, use `aflush()`. Given time to wait, it returns once what it
        reported, and the reports about the events it waited for, have reached `on_error` or the
        logger, waiting at most DELIVERED_GRACE_S more for that. Inside a tool call it doesn't: no
        reply waits for that."""
        result, waits = self._flush(timeout_ms)
        if waits:
            # A host may freeze the process once it returns (Lambda, Vercel), the callback thread
            # with it.
            self._wait_delivered(_running_loop())
        return result

    def _flush(self, timeout_ms: float | None) -> tuple[FlushResult, bool]:
        """`flush()` up to its last wait: the result, and whether it was given time to wait."""
        result = FlushResult()
        waits = False
        try:
            # What a closed loop left pending is let go of, whether this waits or not.
            self._retire_closed()
            if self._mode == "disabled":
                return result, waits
            if _running_loop() is not None:
                self._warn_once("sync-flush", "[lsquare] flush() blocks the event loop it's called on; use await aflush() there.")
            if self._reentered():
                timeout_ms = 0
            prepared = self._flush_prepare(timeout_ms, result)
            if prepared is None:
                return result, waits
            targets, calls, given, in_call = prepared
            # Inside a call that hasn't returned, no wait for reports: its reply hasn't gone out, so
            # the host can't freeze the process yet, and an unrelated slow on_error mustn't delay
            # it. What wait_until is given waits for them instead.
            waits = given > 0 and not in_call
            timeout_ms = given
            if calls and timeout_ms > 0 and current_depth() == 0:
                timeout_ms -= self._wait_calls(calls, timeout_ms)
            wait, unbuilt = self._flush_start(targets, calls, result, timeout_ms)
            if wait is not None:
                self._block(wait, timeout_ms)
                self._end_wait(wait)
            self._flush_count(targets, unbuilt, result, given)
        except Exception:
            pass
        return result, waits

    async def aflush(self, timeout_ms: float | None = None) -> FlushResult:
        """`flush()` for event loops: the same accounting, waiting without blocking the loop."""
        result = FlushResult()
        waits = False
        try:
            self._retire_closed()
            if self._mode == "disabled":
                return result
            prepared = self._flush_prepare(timeout_ms, result)
            if prepared is None:
                return result
            targets, calls, given, in_call = prepared
            # Inside a call, as flush().
            waits = given > 0 and not in_call
            timeout_ms = given
            if calls and timeout_ms > 0 and current_depth() == 0:
                timeout_ms -= await _await_futures([call.done for call in calls], timeout_ms, self._clock)
            wait, unbuilt = self._flush_start(targets, calls, result, timeout_ms)
            if wait is not None:
                try:
                    await _await_wait(wait, timeout_ms)
                finally:
                    self._end_wait(wait)
            self._flush_count(targets, unbuilt, result, given)
        except Exception:
            pass
        if waits:
            await self._await_delivered()
        return result

    def shutdown(self, timeout_ms: float | None = None) -> FlushResult:
        """Terminal: drops events waiting for `before_send`, waits up to `timeout_ms` for the rest,
        then drops whatever is left. Later captures are dropped. Its result counts the events
        outstanding when it was called, one sent while it waited for calls still being built
        included, and those it found as it closed. A `timeout_ms` it can't read counts as not given:
        it still shuts down. Blocks the calling thread: on an event loop, use `ashutdown()`. Given
        time to wait, it returns once what it reported has reached `on_error` or the logger, as
        `flush()` does."""
        result = FlushResult()
        started: tuple[list[_Entry], _Wait | None] | None = None
        calls: list[_FinishingCall] = []
        #: The events outstanding as it was called, counted (see _shutdown_start).
        before: list[_Entry] = []
        waits = False
        try:
            before = self._outstanding()
            self._retire_closed()
            if self._mode != "disabled" and _running_loop() is not None:
                self._warn_once("sync-shutdown", "[lsquare] shutdown() blocks the event loop it's called on; use await ashutdown() there.")
            timeout = _wait_arg(timeout_ms, self._flush_timeout_ms or DEFAULTS["flush_timeout_ms"])
            if self._reentered():
                timeout = 0
            # Disabled, it captured nothing, so it reports nothing.
            waits = timeout > 0 and self._mode != "disabled"
            if waits and not self._closed and (self._live or self._finishing_calls):
                # Terminal, and blocking: what no thread can send (in an app's atexit handler, on
                # a Python that refuses thread starts there), it sends itself, as the exit flush
                # does, within its own timeout.
                with self._lock:
                    if self._exit_deadline is None:
                        self._exit_deadline = self._clock() + timeout / 1000
                self._resume_at_exit()
            calls = list(self._finishing_calls.values())
            if calls and timeout > 0 and current_depth() == 0:
                timeout -= self._wait_calls(calls, timeout)
            started = self._shutdown_start(calls, result, timeout, before)
            if started[1] is not None:
                self._block(started[1], timeout)
        except Exception:
            pass
        finally:
            try:
                # Terminal whatever happens, an interruption included: what's left is dropped.
                self._shutdown_end(started, calls, result, before)
            finally:
                self._scheduler.give_back()
        if waits:
            # Its 'shutdown' reports too (see flush()).
            self._wait_delivered(_running_loop())
        return result

    async def ashutdown(self, timeout_ms: float | None = None) -> FlushResult:
        """`shutdown()` for event loops."""
        result = FlushResult()
        started: tuple[list[_Entry], _Wait | None] | None = None
        calls: list[_FinishingCall] = []
        before: list[_Entry] = []
        waits = False
        try:
            # Counted, as shutdown() counts them.
            before = self._outstanding()
            self._retire_closed()
            timeout = _wait_arg(timeout_ms, self._flush_timeout_ms or DEFAULTS["flush_timeout_ms"])
            waits = timeout > 0 and self._mode != "disabled"
            calls = list(self._finishing_calls.values())
            if calls and timeout > 0 and current_depth() == 0:
                timeout -= await _await_futures([call.done for call in calls], timeout, self._clock)
            started = self._shutdown_start(calls, result, timeout, before)
            if started[1] is not None:
                await _await_wait(started[1], timeout)
        except Exception:
            pass
        finally:
            # Terminal whatever happens, a cancellation included: what's left is dropped.
            self._shutdown_end(started, calls, result, before)
        if waits:
            await self._await_delivered()
        return result

    def _resume_at_exit(self) -> None:
        """The start of the exit flush, or of a sync shutdown(). What a thread start interrupted by
        a signal handler's SystemExit left waiting runs now, steps due and requests no sending
        thread took, and so do requests waiting behind a busy one that no thread can start beside:
        a request in flight may be held for longer than the exit allows. When no thread runs the
        scheduler's steps and none can start, this thread takes its place until it gives it back:
        those steps and requests, the ones the flush starts, and the retries due before its
        deadline then run here, one after another, while it waits (see _block)."""
        self._scheduler.take_over()
        self._scheduler.resume()
        for job in self._senders.resume(at_exit=True):
            self._scheduler.call_soon(job)

    def _wait_calls(self, calls: list[_FinishingCall], timeout_ms: float) -> float:
        """Waits up to `timeout_ms` for these calls' events to be built; returns the ms spent. On
        the thread that took the scheduler over, running its steps and timers meanwhile."""
        futures = [call.done for call in calls]
        if not self._scheduler.driving():
            return _wait_futures(futures, timeout_ms, self._clock)
        started = self._clock()
        for future in futures:
            future.add_done_callback(lambda _: self._scheduler.wake())
        self._scheduler.run_until(started + max(0.0, timeout_ms) / 1000, lambda: all(future.done() for future in futures))
        return min(timeout_ms, (self._clock() - started) * 1000)

    def _block(self, wait: _Wait, timeout_ms: float) -> None:
        """Waits up to `timeout_ms` for `wait` to end. On the thread that took the scheduler over,
        running its steps and timers meanwhile, as its thread would: a request sent from here, its
        outcome, its retry."""
        if not self._scheduler.driving():
            wait.ended.wait(max(0.0, timeout_ms) / 1000)
            return
        wait.on_end.append(self._scheduler.wake)
        self._scheduler.run_until(self._clock() + max(0.0, timeout_ms) / 1000, wait.ended.is_set)

    def _reentered(self) -> bool:
        """This thread is inside one of the client's steps, which a signal handler or a finalizer
        interrupted to call back in: what a flush or shutdown would wait for can't happen until that
        step resumes, so it doesn't wait. Inside the scheduler's lock too: no step runs until it's
        let go."""
        try:
            return bool(self._lock._is_owned() or self._scheduler._lock._is_owned())  # type: ignore[attr-defined]
        except Exception:
            return False

    def _shutdown_end(
        self,
        started: tuple[list[_Entry], _Wait | None] | None,
        calls: list[_FinishingCall],
        result: FlushResult,
        before: list[_Entry],
    ) -> None:
        try:
            if started is None:
                # Interrupted before it started: closed now, nothing waited for. The calls it never
                # got to read are read here, so one still unbuilt counts as failed all the same.
                started = self._shutdown_start(calls or list(self._finishing_calls.values()), result, 0, before)
            targets, wait = started
            if wait is not None:
                self._end_wait(wait)
            self._shutdown_finish(targets, result)
        except Exception:
            pass

    # -- flush and shutdown ----------------------------------------------------------------------

    def _flush_prepare(
        self, timeout_ms: float | None, result: FlushResult
    ) -> tuple[list[_Entry], list[_FinishingCall], float, bool] | None:
        """What the flush waits for, its timeout, and whether it runs inside a call that hasn't
        returned."""
        # Inside a call: its event isn't built yet, so it can't be waited for here. The wrapper sends
        # it before the reply instead, and this flush doesn't wait on itself.
        scope = self._calls.get() if current_depth() == 0 else None
        in_call = False
        if scope is not None and not scope.returned:
            scope.flush_requested = True
            in_call = True
        timeout = _wait_arg(timeout_ms, self._flush_timeout_ms)
        with self._lock:
            targets = list(self._live.values())
            calls = list(self._finishing_calls.values())
        return targets, calls, timeout, in_call

    def _flush_start(
        self, targets: list[_Entry], calls: list[_FinishingCall], result: FlushResult, timeout_ms: float
    ) -> tuple[_Wait | None, list[_FinishingCall]]:
        """The wait, if any, and the calls still unbuilt, counted as pending."""
        with self._lock:
            unbuilt, dropped = self._add_calls(targets, calls)
            result.pending += len(unbuilt)
            result.failed += dropped
            self._pump()
            self._pump_requests()
            if targets and timeout_ms > 0:
                return self._wait_for(targets, timeout_ms), unbuilt
        return None, unbuilt

    def _flush_count(self, targets: list[_Entry], unbuilt: list[_FinishingCall], result: FlushResult, timeout_ms: float) -> None:
        with self._lock:
            for entry in targets:
                if entry.outcome is not None:
                    setattr(result, entry.outcome, getattr(result, entry.outcome) + 1)
                    continue
                result.pending += 1
                # With 0 the caller chose not to wait: pending is expected, not a problem.
                if timeout_ms > 0:
                    self._report(
                        LSquareError("pending_after_flush", "Still pending when flush() returned.", event_id=entry.event_id),
                        entry.event if entry.redacted else None,
                        entry.depth,
                        entry.context,
                        entry.loop,
                        entry.bytes,
                    )
            # Pending too, by the ID its event will have: still waiting for identify. Reported from
            # where the call ran, as its event would be, not from the flush's own request.
            if timeout_ms > 0:
                for call in unbuilt:
                    origin = call.origin
                    self._report(
                        LSquareError("pending_after_flush", "Still pending when flush() returned.", event_id=call.call_id),
                        None,
                        origin.depth,
                        origin.context,
                        origin.loop,
                    )

    def _outstanding(self) -> list[_Entry]:
        with self._lock:
            return list(self._live.values())

    def _shutdown_start(
        self, calls: list[_FinishingCall], result: FlushResult, timeout_ms: float, before: list[_Entry]
    ) -> tuple[list[_Entry], _Wait | None]:
        """Counted: the events outstanding when shutdown() was called (`before`), one sent while it
        waited for calls included, then those still live as it closes, then those calls' events,
        each once. One captured and settled entirely while it waited for calls is in neither."""
        with self._lock:
            self._closed = True
            targets = list(dict.fromkeys([*before, *self._live.values()]))
            unbuilt, dropped = self._add_calls(targets, calls)
            result.failed += len(unbuilt) + dropped
            for entry in targets:
                if entry.stage in ("waiting", "hook"):
                    self._finish(entry, "failed", LSquareError("shutdown", "shutdown() dropped an event waiting for before_send."))
            self._pump_requests()
            remaining = [entry for entry in targets if entry.outcome is None]
            wait = self._wait_for(remaining, timeout_ms) if remaining and timeout_ms > 0 else None
            return targets, wait

    def _shutdown_finish(self, targets: list[_Entry], result: FlushResult) -> None:
        discards: list[Callable[[], None]] = []
        runs: list[HookRun] = []
        with self._lock:
            for entry in list(self._live.values()):
                self._finish(entry, "failed", LSquareError("shutdown", "shutdown() ended before the event was sent."))
            for entry in targets:
                outcome = entry.outcome or "failed"
                setattr(result, outcome, getattr(result, outcome) + 1)
            # Requests still in flight change nothing from here, and the threads stop when idle.
            self._generation += 1
            self._hung = set()
            runs, self._identify_runs = list(self._identify_runs), set()
            # None is admitted from here: the hooks still running hold no slot worth counting.
            self._identifying["active"] = 0
            # Calls whose events are still to be built (waiting for identify), already counted as
            # failed: their builds are taken and let go of now, before the threads that would run
            # them stop, rather than left armed on a loop or a timer, holding the call and its
            # result, to build an event that would only be dropped (py-mcp-19). Reported as
            # shutdown, once, as such a build would be: never as identify_failed (parity-25).
            discards = [call.discard for call in self._finishing_calls.values() if call.entry is None and not call.finished and call.discard is not None]
            if discards:
                self._refuse_closed()
        for discard in discards:
            _quietly(discard)
        for run in runs:
            _quietly(run.abort)
        self._scheduler.stop()
        self._senders.stop()
        self._hooks.stop()
        self._identify_hooks.stop()
        self._builds.stop()
        self._callbacks.stop()
        self._signals.stop()
        self._resolver.stop()
        self._private_loop.stop()

    def _add_calls(self, targets: list[_Entry], calls: list[_FinishingCall]) -> tuple[list[_FinishingCall], int]:
        """Adds the events these calls queued to `targets`. Returns (the calls still unbuilt, how
        many were dropped before queued)."""
        seen = set(targets)
        unbuilt: list[_FinishingCall] = []
        dropped = 0
        for call in calls:
            if call.entry is not None:
                if call.entry not in seen:
                    targets.append(call.entry)
                    seen.add(call.entry)
            elif call.finished:
                dropped += 1
            else:
                unbuilt.append(call)
        return unbuilt, dropped

    def _settled(self, event_id: str, timeout_ms: float, turns: Iterable[_Entry] = ()) -> concurrent.futures.Future[None]:
        future: concurrent.futures.Future[None] = concurrent.futures.Future()
        with self._lock:
            pending: set[_Entry] = set()
            for entry in self._live.values():
                if entry.event_id == event_id:
                    pending.add(entry)
                    break
            # Read under the lock that fills and empties a call's turns: those still queued.
            for entry in turns:
                if self._live.get(entry.seq) is entry:
                    pending.add(entry)
            if not pending:
                _resolve(future)
                return future
            cancel = self._scheduler.call_later(min(max(0.0, timeout_ms), MAX_TIMER_MS) / 1000, lambda: _resolve(future))
            for entry in pending:

                def done(entry: _Entry = entry) -> None:
                    # Under the lock, in _finish.
                    pending.discard(entry)
                    if not pending:
                        cancel()
                        _resolve(future)

                if entry.waiters is None:
                    entry.waiters = []
                entry.waiters.append(done)
        return future

    def _follow(self, scope: CallScope, entry: _Entry) -> None:
        """Before the tool returns, a turn's sending is the call's too: waited for with the call's
        event, each capture on its own (two can share an event ID), until it is sent or dropped."""
        with self._lock:
            turns = scope.turns
            if entry.outcome is not None or scope.returned or len(turns) >= MAX_CALL_TURNS:
                return
            turns.add(entry)
            if entry.waiters is None:
                entry.waiters = []
            entry.waiters.append(lambda: turns.discard(entry))

    def _delivered(self, blocked: asyncio.AbstractEventLoop | None = None) -> concurrent.futures.Future[None]:
        """Done once the callback thread has run the reports and log lines handed to it so far (it
        takes them in order), and the async handlers among them have started on their loops, up to
        their first await, not their own I/O. Done at once when there are none, from inside a
        callback (which may be running on that thread), or re-entered (see `_reentered`). A handler
        on `blocked`, the loop the caller blocks, can't start until the caller returns, and one
        handed to a loop that has stopped, or more than DELIVERED_GRACE_S ago, won't start in time:
        neither is waited for. A turn on the callback thread still to come DELIVERED_GRACE_S on
        means a sync handler hasn't returned, holding back what came after it: a later one is done
        at once rather than wait again."""
        future: concurrent.futures.Future[None] = concurrent.futures.Future()
        ran: concurrent.futures.Future[None] = concurrent.futures.Future()
        try:
            if current_depth() > 0 or self._reentered():
                _resolve(future)
                return future
            now = time.monotonic()
            with self._futures_lock:
                if self._callbacks.idle() and not self._unstarted:
                    _resolve(future)
                    return future
                self._barriers = [barrier for barrier in self._barriers if not barrier[1].done()]
                if self._barriers and now - self._barriers[0][0] >= DELIVERED_GRACE_S:
                    _resolve(future)
                    return future
                self._barriers.append((now, ran))

            def turn() -> None:
                _resolve(ran)
                # After every job handed to the callback thread before: the async handlers those
                # started are all known now.
                now = time.monotonic()
                starting: list[concurrent.futures.Future[None]] = []
                with self._futures_lock:
                    for started, (loop, since) in list(self._unstarted.items()):
                        if now - since >= DELIVERED_GRACE_S or (loop is not None and (loop.is_closed() or not loop.is_running())):
                            del self._unstarted[started]
                        elif loop is None or loop is not blocked:
                            starting.append(started)
                _all_done(starting).add_done_callback(lambda _: _resolve(future))

            if self._callbacks.idle():
                turn()
            elif not self._callbacks.submit(turn):
                _resolve(ran)
                _resolve(future)
        except Exception:
            _resolve(ran)
            _resolve(future)
        return future

    def _wait_delivered(self, blocked: asyncio.AbstractEventLoop | None = None) -> None:
        """Waits for `_delivered(blocked)` at most DELIVERED_GRACE_S. Never raises."""
        try:
            future = self._delivered(blocked)
            deadline = time.monotonic() + DELIVERED_GRACE_S
            concurrent.futures.wait([future], timeout=DELIVERED_GRACE_S)
            left = _top_up(future, deadline)
            if left > 0:
                concurrent.futures.wait([future], timeout=left)
        except Exception:
            pass

    async def _await_delivered(self) -> None:
        """`_wait_delivered()` for event loops."""
        try:
            future = self._delivered()
            deadline = time.monotonic() + DELIVERED_GRACE_S
            await _await_futures([future], DELIVERED_GRACE_S * 1000, self._clock)
            left = _top_up(future, deadline)
            if left > 0:
                await _await_futures([future], left * 1000, self._clock)
        except Exception:
            pass

    def _track(
        self,
        call_id: str,
        finishing: concurrent.futures.Future[None],
        origin: Origin | None = None,
        discard: Callable[[], None] | None = None,
    ) -> None:
        here = origin if origin is not None else self._lsquare_internal().origin()
        call = _FinishingCall(done=finishing, call_id=call_id, origin=here, discard=discard)
        # Registered first, then checked, under the lock shutdown() closes the client under and
        # reads these calls under: a shutdown() before the registration is found closed here, one
        # after it finds the call, so none escapes it. A call registered with a closed client (its
        # tool awaited ashutdown(), say) is disposed of at once, as shutdown() disposes of one
        # still unbuilt: its build taken and let go of, `finishing` done, and the shutdown reported
        # once, as its event would be. Nothing waits for it then, nor counts it pending.
        with self._lock:
            self._finishing_calls[call_id] = call
            closed = self._closed and discard is not None
            if closed:
                self._refuse_closed()

        def finished(_: Any) -> None:
            with self._lock:
                call.finished = True
                # What builds or drops the call holds it, and its result: let go of with it, as the
                # finishing future, which keeps this callback, may be held on (a reply waiting).
                call.discard = None
                if self._finishing_calls.get(call_id) is call:
                    del self._finishing_calls[call_id]

        finishing.add_done_callback(finished)
        if closed and discard is not None:
            # Outside the lock, as shutdown() runs it (see _shutdown_finish): it aborts identify.
            _quietly(discard)

    # -- The pipeline ----------------------------------------------------------------------------

    def _enqueue(
        self,
        kind: str,
        input: Any,
        loop: asyncio.AbstractEventLoop | None = None,
        other_loop: bool | None = None,
        diagnostic: LSquareError | None = None,
    ) -> _Entry | None:
        """Returns the event's entry once it is queued, None when it isn't. `diagnostic` (a call's
        `identify_failed`) is reported once, before what else is reported about the event, unless
        it is refused for shutdown(), which is reported instead (parity-25): the last check of that
        is made under the lock that admits the event."""
        global _warned_contact_user_id
        if self._mode == "disabled":
            return None
        # Captured from code nested past the limit (a before_send capturing from a hook): refused.
        if current_depth() > MAX_CALLBACK_DEPTH:
            return None
        if self._closed:
            with self._lock:
                self._refuse_closed()
            return None
        unsaid = [diagnostic] if diagnostic is not None else []

        def say() -> None:
            while unsaid:
                self._report(unsaid.pop())

        probe: tuple[_Holdback, object] | None = None
        with self._lock:
            if self._holdback is not None:
                admitted = self._admit(self._holdback)
                if admitted == "drop":
                    # Held back past the daily limit, and counted there: what the call says of
                    # identify still holds, unless shutdown() came first.
                    if not self._closed:
                        say()
                    return None
                if admitted == "check":
                    # Reserved now, under the lock, so two captures can't both become the check.
                    probe = (self._holdback, self._holdback.check)
        hook = self._before_send is not None
        try:
            if kind == "turn":
                built = _event.build_turn(input, _iso_now(self._now()), self._context, hook)
            else:
                built = _event.build_call(input, self._context, hook)
        except Exception:
            self._unbuilt(probe, say, LSquareError("redaction_failed", "Building the event failed; it was dropped, never sent unredacted."))
            return None
        if not built.ok:
            messages = {
                "invalid_input": "capture() needs a mapping; the event was dropped.",
                "invalid_id": "An ID must be a non-empty string of at most 512 bytes, without NUL; the event was dropped.",
                "too_large": f"The event is larger than {self._max_event_bytes} bytes even without its largest parts; it was dropped.",
            }
            paths = tuple(built.paths) if built.paths else None
            self._unbuilt(probe, say, LSquareError(built.code, messages.get(built.code, "The event was dropped."), paths=paths))
            return None
        draft = built.draft
        event = draft.event
        user_id = event.get("userId")
        if isinstance(user_id, str) and not _warned_contact_user_id and _redact.looks_like_contact(user_id):
            _warned_contact_user_id = True
            self._log("warn", "[lsquare] A userId looks like an email address or phone number. It is sent as given, so the person can be deleted by it: send an internal ID instead.")
        size = _event.byte_size(event) + (_event.retained_bytes(draft) if hook else 0)
        if other_loop is None:
            if loop is None:
                loop = _running_loop()
            other_loop = loop is None and _other_loop_running()
        with self._lock:
            # Building ran the app's code (getters): one that shut the client down drops this event
            # too, before it can take the room of one shutdown() is still sending, reported as one
            # captured after it, as in TypeScript.
            if self._closed:
                self._release_probe(probe)
                self._refuse_closed()
                return None
            # Not refused for shutdown(): from here what the call says of its identify is true.
            # Handed to the callback thread here, under the lock, so it comes before the event's
            # own reports, from its steps; the handler runs there, outside the lock.
            say()
            # Hooks whose loop closed under them give their slots and bytes back first (parity-5).
            self._reclaim_hooks()
            if not self._make_room(size):
                self._release_probe(probe)
                self._report(LSquareError("queue_full", "The queue is full; the event was dropped.", event_id=event["eventId"]))
                return None
            self._seq += 1
            stage: Stage = "waiting" if hook else "ready"
            entry = _Entry(
                seq=self._seq,
                depth=current_depth(),
                context=contextvars.copy_context(),
                created_at=self._clock(),
                event_id=event["eventId"],
                kind=kind,
                event=event,
                draft=draft if hook else None,
                bytes=size,
                stage=stage,
                redacted=not hook,
                loop=loop,
                other_loop=other_loop,
                ignored_fields=dict(getattr(draft, "ignored_fields", {}) or {}),
                ignored_tool_call_fields=dict(getattr(getattr(draft, "notes", None), "ignored_tool_call_fields", {}) or {}),
            )
            try:
                self._live[entry.seq] = entry
                self._live_bytes += size
                if kind == "tool_call":
                    call = self._finishing_calls.get(entry.event_id)
                    if call is not None:
                        call.entry = entry
                if probe is not None and self._holdback is probe[0] and probe[0].check is probe[1]:
                    probe[0].check = entry
                    probe[0].next_check = self._clock() + QUOTA_PROBE_MS / 1000
                if hook:
                    entry.timer = self._scheduler.call_later(self._before_send_timeout_ms / 1000, lambda: self._locked(lambda: self._deadline(entry)))
                    self._scheduler.call_soon(lambda: self._locked(self._pump))
                else:
                    self._scheduler.call_soon(lambda: self._locked(lambda: self._guard(entry, lambda: self._ready(entry))))
            except BaseException:
                # Interrupted before its steps were all scheduled (a signal handler's SystemExit,
                # say, inside a thread start): not queued at all, rather than left where nothing
                # sends it. A step already scheduled finds it finished; one that already ran here
                # (inline, as at exit) took it on, so it's left to that. The bytes are counted
                # again, wherever the interruption landed.
                if entry.stage == stage:
                    self._finish(entry, "failed")
                self._live_bytes = sum(other.bytes for other in self._live.values())
                self._release_probe(probe)
                raise
        return entry

    def _refuse_closed(self) -> None:
        """Reported once: an on_error that captures after shutdown must not feed itself. Under the lock."""
        if not self._reported_closed:
            self._reported_closed = True
            self._report(LSquareError("shutdown", "capture() was called after shutdown(); this and later events are dropped."))

    def _locked(self, step: Callable[[], None]) -> None:
        with self._lock:
            try:
                step()
            except Exception:
                pass

    def _pump(self) -> None:
        """Enforces hook deadlines and starts hooks for waiting events, within the limit."""
        try:
            if self._before_send is None:
                return
            # First, hooks whose loop closed under them fail, and give their slots back (parity-5).
            self._reclaim_hooks()
            for entry in list(self._live.values()):
                if entry.stage in ("waiting", "hook") and self._past_deadline(entry):
                    self._expire(entry)
            for entry in list(self._live.values()):
                if self._active_hooks >= self._max_concurrent_hooks:
                    break
                if entry.stage == "waiting":
                    self._start_hook(entry)
        except Exception:
            pass

    def _start_hook(self, entry: _Entry) -> None:
        """Starts `before_send` for a waiting event, under the client's lock. Nothing of the app's
        runs here, nor anything that takes long: the event is copied for the hook on a hook thread
        (a sync hook's own, or one that then hands an async hook to its loop), and what the hook
        returns is read and rebuilt there too, or for an async hook on a hook thread again, never
        on the app's loop nor under this lock (py-runtime-2, py-runtime-26). Only the result is
        published under it, once checked against the run's state and the client's generation."""
        before_send = self._before_send
        assert before_send is not None
        signal = Signal()
        run = _HookRun(signal._lock, source=[entry.event])
        entry.stage = "hook"
        self._active_hooks += 1
        entry.hook_signal = signal
        entry.run = run
        context = (entry.context or contextvars.Context()).copy()
        generation = self._generation
        takes_signal = self._before_send_takes_signal
        created_at = entry.created_at
        timeout_ms = self._before_send_timeout_ms
        clock = self._clock

        def take() -> Any:
            """The event, copied for the hook on the thread about to call it: _GONE once the event
            was dropped first (its slot then already back), _UNCOPIED when it can't be copied."""
            with run.lock:
                if run.state != "starting" or not run.source:
                    return _GONE
                source = run.source.pop()
                run.state = "running"
            try:
                return copy.deepcopy(source)
            except Exception:
                return _UNCOPIED
            finally:
                source = None

        def returned() -> None:
            # Right away, where the hook ran (its thread, or inside its task): its result is
            # published only later, and a _finish in between (shutdown(), its deadline) must
            # leave the signal of a hook that has returned alone. Under the run's own lock, never
            # the client's: another event's step can hold that one until this hook's deadline, and
            # this may run on the app's loop.
            run.mark_returned(signal, clock)

        def publish(result: Any, failed: bool = False, started: bool = True) -> None:
            self._scheduler.call_soon(lambda: self._locked(lambda: self._hook_done(entry, result, failed, generation, started, run)))

        def finalize() -> None:
            """On a hook thread: what the hook returned, read and rebuilt (the app's code, perhaps
            slow: a Mapping's getters), then published. Its draft is taken now, not when the hook
            started: an event dropped meanwhile has none, and nothing is read."""
            with run.lock:
                if run.state != "queued" or not run.output:
                    # Let go of first (shutdown(), the ceiling, its loop closing): its slot is back.
                    return
                run.state = "processing"
                out = run.output.pop()
            result: Any = _REDACTION_FAILED
            try:
                draft = entry.draft
                result = None if draft is None else _hook_result(out, draft, self._context)
            finally:
                out = draft = None
                publish(result)

        def unread() -> None:
            """No hook thread can take what the hook returned (none runs, and the system refused to
            start one): it isn't read where the hook settled, the app's loop perhaps, whose
            thread a getter would hold, or wait for. The event fails. The run stays "queued" until
            that is published: nothing of the app's runs for it, so should this hand-off be
            interrupted before then (a KeyboardInterrupt on the main thread's loop, the app
            catching it), its ceiling or shutdown() still lets go of it at once, and its slot comes
            back. Only a worker reading it, the app's code, makes a run "processing"."""
            with run.lock:
                if run.state != "queued" or not run.output:
                    return
                run.output.clear()
            publish(_UNREAD)

        def settle(error: BaseException | None, out: Any, started: bool = True, here: bool = False) -> None:
            """The hook settled: on time or not decided by when it returned, not by what follows.
            `here`: on a hook thread, which then reads the result itself."""
            returned()
            with run.lock:
                if run.state == "done":
                    # Let go of already, its slot back: nothing to hand on.
                    return
                on_time = run.returned_at is not None and (run.returned_at - created_at) * 1000 < timeout_ms
                read = error is None and out is not None and on_time
                if read:
                    run.state = "queued"
                    run.output.append(out)
            out = None
            if not read:
                if isinstance(error, _Uncopied):
                    publish(_UNCOPIED)
                else:
                    publish(None, error is not None, started)
                return
            if here:
                # On a hook thread already: read there.
                finalize()
            elif self._hooks.submit(finalize):
                return
            elif self._at_exit():
                # Only in the exit's flush, where some Pythons start no thread at all: read here,
                # on the loop it settled on, so that flush can still send it.
                finalize()
            else:
                unread()

        def in_context(fn: Callable[[], Any]) -> Callable[[], Any]:
            def deeper() -> Any:
                _depth.set(entry.depth + 1)
                return fn()

            return lambda: context.run(deeper)

        def call(event: Any) -> Any:
            return before_send(event, signal) if takes_signal else before_send(event)

        if self._before_send_is_async:

            def prepare() -> None:
                # On a hook thread: copied there, then handed to the hook's loop.
                event = take()
                if event is _GONE:
                    return
                if event is _UNCOPIED:
                    settle(_Uncopied(), None)
                    return
                self._run_async(entry, signal, in_context(lambda: _watched(lambda: call(event), returned)), context, settle)

            if self._hooks.submit(prepare):
                return
            if self._at_exit():
                # Only in the exit's flush, where some Pythons start no thread at all: copied and
                # handed over here, so that flush can still run it.
                prepare()
                return
            # Never copied here otherwise, under the client's lock, on the app's thread perhaps
            # (py-runtime-26): it never ran, as a sync hook no thread can take.
            with run.lock:
                run.source.clear()
                run.state = "running"
            settle(RuntimeError("No thread can start for before_send."), None, False)
            return

        def run_sync() -> None:
            event = take()
            if event is _GONE:
                return
            if event is _UNCOPIED:
                settle(_Uncopied(), None, here=True)
                return
            try:
                out = in_context(lambda: call(event))()
            except BaseException as error:  # noqa: BLE001
                settle(error, None, here=True)
                return
            finally:
                event = None
            if inspect.isawaitable(out):
                if signal.aborted:
                    # Its deadline or shutdown() came while its sync part ran: never started, on no
                    # loop, as identify's isn't (py-runtime-5). An abort that lands after this is
                    # still cancelled once handed over (see _run_async).
                    if inspect.iscoroutine(out):
                        out.close()
                    settle(asyncio.CancelledError(), None, here=True)
                    return
                # A sync function returning an awaitable: settled on a loop, as an async hook. One
                # refused there (a closed loop, or the private one stopped since the check above)
                # is closed, never left un-awaited.
                def settled(error: BaseException | None, result: Any) -> None:
                    if error is not None:
                        _close_unstarted(out)
                    settle(error, result)

                self._run_async(entry, signal, lambda: _watched(lambda: out, returned), context, settled)
                return
            settle(None, out, here=True)

        if not self._hooks.submit(run_sync):
            # It never ran: said so, not taken for a hook that raised. Nothing to copy any more.
            with run.lock:
                run.source.clear()
                run.state = "running"
            settle(RuntimeError("No thread can start for before_send."), None, False)

    def _at_exit(self) -> bool:
        """The client's own exit flush is under way (see _exit_flush), as the interpreter exits,
        where some Pythons (3.12.2) start no thread at all. Only there is a hook's copy, or what it
        returned, handled on the thread that got to it when no hook thread can take it (see
        _start_hook). Never for a sync shutdown(), though it sets `_exit_deadline` too and the main
        thread may have finished: a server a non-daemon thread keeps running after the main thread
        returned is still serving, and that thread may be its loop's, where a getter would block
        the loop, or wait for it forever. So it is the flag only the exit flush sets that decides,
        never `_exit_deadline`."""
        return self._exit_flushing and exiting()

    def _run_async(
        self,
        entry: _Entry,
        signal: Signal,
        make: Callable[[], Any],
        context: contextvars.Context,
        settle: Callable[[BaseException | None, Any], None],
    ) -> None:
        """An async hook runs on its event's loop, else the private loop; never on trio."""
        loop: asyncio.AbstractEventLoop | PrivateLoop | None = entry.loop
        if loop is None:
            if entry.other_loop:
                self._warn_once("async-hook-loop", "[lsquare] An async before_send needs asyncio; it isn't run on other event loops. Use a sync one there.")
                settle(RuntimeError("not asyncio"), None)
                return
            # If its thread can't start, the hook fails, whichever event's hook started it.
            loop = self._private_loop
        else:
            run = entry.run
            if run is not None:
                # The app's: should it close before the hook settles, the slot comes back (parity-5).
                with run.lock:
                    run.loop = entry.loop
        try:
            cancel = schedule_on(loop, make, context, settle)
        except Exception as error:
            # The loop is closed or gone: the event fails, nothing unprocessed is sent.
            settle(error, None)
            return

        def publish() -> None:
            entry.hook_cancel = cancel

        # Kept only while it runs: a sync hook's awaitable, started from a hook thread, can settle
        # before schedule_on() returns, its _hook_done already past, or be aborted (its deadline,
        # shutdown()) with nothing yet to cancel it by: cancelled here then.
        if signal._while_running(publish):
            _quietly(cancel)

    def _hook_done(
        self, entry: _Entry, result: Any, failed: bool, generation: int, started: bool = True, run: _HookRun | None = None
    ) -> None:
        """Publishes a hook's result, under the lock: `result` is what was made of what the hook
        returned (see _hook_result), None when it returned None or failed (`failed`). `run`: the run
        it came from, whose state says whether its slot is already back."""
        # Settled: nothing aborts or cancels it from here. Its task holds what it returned, which
        # whatever still holds this entry (a call's turns, a flush) mustn't keep, after a shutdown
        # or fork too.
        entry.hook_signal = None
        entry.hook_cancel = None
        if run is None:
            run = entry.run
        returned_at: float | None = None
        if run is not None:
            with run.lock:
                given_back = run.state == "done"
                run.state = "done"
                run.source.clear()
                run.output.clear()
                run.loop = None
                returned_at = run.returned_at
            if given_back:
                # Its slot came back already (see _finish, _reclaim_hooks): nothing to release.
                if generation == self._generation:
                    self._pump()
                return
        if generation != self._generation:
            return
        self._active_hooks -= 1
        if entry.zombie:
            # Too late: the event was already dropped. Release what the hook held, discard its result.
            entry.zombie = False
            self._hung.discard(entry)
            self._zombies -= 1
            self._zombie_bytes -= entry.bytes
            self._pump()
            return
        if entry.outcome is not None:
            self._pump()
            return
        entry.stage = "ready"
        self._clear_timer(entry)
        # When it returned, not now: the client's own reading and rebuilding never counts against
        # the hook. The deadline timer may not have run yet: checked here too.
        late = returned_at is None or (returned_at - entry.created_at) * 1000 >= self._before_send_timeout_ms
        unfinished = not late and self._past_ceiling(entry)

        def step() -> None:
            if result is _UNCOPIED:
                self._finish(entry, "failed", LSquareError("internal_error", "before_send could not be started; the event was dropped."))
            elif result is _UNREAD:
                self._finish(entry, "failed", LSquareError("hook_failed", "No thread could start to read what before_send returned; the event was dropped."))
            elif late:
                self._finish(entry, "failed", self._timeout_error())
            elif unfinished:
                self._finish(entry, "failed", self._unfinished_error())
            elif failed:
                message = "before_send raised; the event was dropped." if started else "No thread could start for before_send; the event was dropped."
                self._finish(entry, "failed", LSquareError("hook_failed", message))
            elif result is None:
                self._finish(entry, "skipped")
            elif result is _NOT_A_MAPPING:
                self._finish(entry, "failed", LSquareError("hook_failed", "before_send must return the event or None; the event was dropped."))
            elif result is _REDACTION_FAILED:
                self._finish(entry, "failed", LSquareError("redaction_failed", "Redaction failed; the event was dropped, not sent unredacted."))
            else:
                self._finish_hook(entry, result)

        self._guard(entry, step)
        self._pump()

    def _finish_hook(self, entry: _Entry, rebuilt: Any) -> None:
        """The event rebuilt from what the hook returned, and measured, on a hook thread (see
        _hook_result): only its accounting and its queuing here, under the lock."""
        entry.draft = None
        built = rebuilt.built if isinstance(rebuilt, _Rebuilt) else rebuilt
        if not built.ok:
            too_large = built.code == "too_large"
            self._finish(
                entry,
                "failed",
                LSquareError(
                    "too_large" if too_large else "hook_failed",
                    f"before_send made the event larger than {self._max_event_bytes} bytes even without its largest parts; it was dropped."
                    if too_large
                    else "before_send returned invalid IDs; the event was dropped.",
                ),
            )
            return
        event = built.draft.event
        size = rebuilt.size if isinstance(rebuilt, _Rebuilt) else _event.byte_size(event)
        self._live_bytes += size - entry.bytes
        entry.bytes = size
        entry.event = event
        entry.redacted = True
        if not self._fits_after_growth(entry):
            self._finish(entry, "failed", LSquareError("queue_full", "The event grew past the memory limits; it was dropped."))
        elif entry.outcome is None:
            self._ready(entry)

    def _ready(self, entry: _Entry) -> None:
        """Redacted and final: logged in dry run, otherwise queued for the next request."""
        if entry.outcome is not None:
            return
        if self._mode == "dry-run":
            self._log("info", _dry_run_summary(entry), entry.depth, entry.context, entry.loop)
            self._finish(entry, "skipped")
            return
        self._dispatch(entry)

    def _dispatch(self, entry: _Entry) -> None:
        """Waits its turn in the order events became ready; requests go out in a later step, so
        events ready together go together."""
        entry.stage = "queued"
        self._queued[entry] = None
        if self._pump_scheduled:
            return
        self._pump_scheduled = True

        def run() -> None:
            self._pump_scheduled = False
            self._pump_requests()

        self._scheduler.call_soon(lambda: self._locked(run))

    def _pump_requests(self) -> None:
        try:
            if self._holdback is not None and not self._holding_back():
                return
            while self._active_requests < self._max_concurrent_requests and self._queued:
                if not self._send_batch(self._take_batch(), False):
                    # Back at the head of the queue: no sending thread can take another now.
                    break
        except Exception:
            pass

    def _admit(self, holdback: _Holdback) -> str:
        if self._now() >= holdback.until:
            self._end_holdback()
            return "send"
        if holdback.check is None and self._clock() >= holdback.next_check:
            holdback.check = object()
            return "check"
        holdback.dropped += 1
        return "drop"

    def _unbuilt(self, probe: tuple[_Holdback, object] | None, say: Callable[[], None], error: LSquareError) -> None:
        """An event that couldn't be built. Building ran the app's code (getters): one that shut the
        client down meanwhile has the event refused for that, reported once as shutdown, as one
        that was built is, and neither its build's failure nor what the call said of identify is
        reported (parity-25). Decided under the lock, as for an event that was built; the reports
        are handed to the callback thread there, and their handler runs outside it."""
        with self._lock:
            self._release_probe(probe)
            if self._closed:
                self._refuse_closed()
                return
            say()
            self._report(error)

    def _release_probe(self, probe: tuple[_Holdback, object] | None) -> None:
        """A reserved check that never became an event: the next capture may be the check."""
        if probe is None:
            return
        with self._lock:
            if probe[0].check is probe[1]:
                probe[0].check = None

    def _holding_back(self) -> bool:
        holdback = self._holdback
        assert holdback is not None
        if self._now() >= holdback.until:
            self._end_holdback()
            return True
        for entry in list(self._queued):
            if entry is holdback.check:
                continue
            holdback.dropped += 1
            self._finish(entry, "failed")
        check = holdback.check
        if isinstance(check, _Entry) and check in self._queued and self._active_requests < self._max_concurrent_requests:
            del self._queued[check]
            self._send_batch([check], True)
        return False

    def _start_holdback(self, resets_at: str | None) -> bool:
        parsed = _parse_time(resets_at)
        if parsed is not None and parsed <= self._now():
            return False
        until = _reset_time(resets_at, self._now())
        at = _iso_now(until)
        holdback = self._holdback
        if holdback is not None:
            if until > holdback.until:
                holdback.until = until
                holdback.resets_at = at
            return True
        self._holdback = _Holdback(until=until, resets_at=at, next_check=self._clock() + QUOTA_PROBE_MS / 1000)
        self._report(
            LSquareError(
                "quota_exceeded",
                f"The project's daily limit was reached; events are dropped until {at[11:16]} UTC.",
                resets_at=at,
            )
        )
        return True

    def _end_holdback(self) -> None:
        holdback = self._holdback
        self._holdback = None
        if holdback is not None and holdback.dropped > 0:
            self._report(
                LSquareError(
                    "quota_dropped",
                    f"Sending resumed after the daily limit; {holdback.dropped} events were dropped.",
                    count=holdback.dropped,
                )
            )

    def _take_batch(self) -> list[_Entry]:
        batch: list[_Entry] = []
        body = BATCH_OVERHEAD
        ack = ACK_OVERHEAD
        for entry in self._queued:
            event_ack = _ack_bytes(entry.event_id)
            fits = len(batch) < MAX_BATCH_EVENTS and body + entry.bytes + 1 <= MAX_BATCH_BYTES and ack + event_ack <= MAX_ACK_BYTES
            if not fits and batch:
                break
            batch.append(entry)
            body += entry.bytes + 1
            ack += event_ack
        for entry in batch:
            del self._queued[entry]
        return batch

    def _send_batch(self, entries: list[_Entry], probe: bool) -> bool:
        """False when no sending thread could take the batch: it is queued again (see below)."""
        batch = _Batch(
            live=len(entries),
            abort=Abort(),
            probe=probe,
            holdback=self._holdback if probe else None,
            generation=self._generation,
        )
        for entry in entries:
            entry.stage = "sending"
            entry.batch = batch
            entry.attempts += 1
        self._active_requests += 1
        #: The events, final once queued: serialized by the request, on its own thread, never under
        #: the client's lock, which every capture takes (py-runtime-26). Taken there, so only the
        #: body is held from then on.
        events = [[entry.event for entry in entries]]
        context = (entries[0].context if entries and entries[0].context is not None else contextvars.Context()).copy()

        def exchange() -> None:
            reply: Reply | None = None
            body: bytes | None = None
            taken = events.pop() if events else []
            # A shutdown() since, one that interrupted this very step included (a signal handler's
            # or a finalizer's), already counted these events as failed: never posted after it, and
            # not serialized for nothing. A body that can't be made is a request that fails, retried.
            if batch.generation == self._generation and not batch.abort.aborted:
                try:
                    body = _jsjson.dumps({"events": taken}).encode("utf-8")
                except Exception:
                    body = None
            taken = []
            # The time left once serialized, which took some: the exit's deadline holds for both.
            timeout_s = self._request_timeout_ms / 1000
            if self._exit_deadline is not None:
                timeout_s = min(timeout_s, max(0.0, self._exit_deadline - self._clock()))
            # Checked again: a shutdown() may have come while it serialized.
            if body is not None and timeout_s > 0 and batch.generation == self._generation:
                try:
                    if self._transport is not None:
                        reply = self._transport(body, timeout_s, batch.abort)
                    elif self._exchange is not None:
                        reply = self._exchange.post(body, timeout_s, batch.abort)
                except NetworkError:
                    reply = None
                except Exception:
                    reply = None
            self._scheduler.call_soon(lambda: self._locked(lambda: self._sent(entries, batch, reply)))

        if self._senders.submit(lambda: context.run(exchange)):
            self._repump_ms = RETRY_BASE_MS
            return True
        if self._exit_deadline is not None:
            # Interpreter exit, or a sync shutdown(): no thread can start. Sent from the scheduler,
            # or the thread that took it over, within what's left of the deadline, with addresses
            # already resolved.
            self._scheduler.call_soon(lambda: context.run(exchange))
            return True
        # No request was made, so it isn't a network failure: back at the head of the queue, order
        # kept, its attempt not spent, as TypeScript keeps a batch that can't start on Workers. The
        # queue is pumped again later, with backoff, as a retry would be, so a start refused for a
        # moment (a container's process limit) doesn't leave it waiting for the next capture. With
        # no scheduler thread either, that pump waits for the exit flush, which sends the queue.
        self._active_requests -= 1
        for entry in entries:
            entry.stage = "queued"
            entry.batch = None
            entry.attempts -= 1
        self._queued = {**dict.fromkeys(entries), **self._queued}
        if self._repump is None:
            delay = self._repump_ms
            self._repump_ms = min(RETRY_CAP_MS, self._repump_ms * 2)

            def repump() -> None:
                self._repump = None
                self._pump_requests()

            self._repump = self._scheduler.call_later(delay / 1000, lambda: self._locked(repump))
        return False

    def _sent(self, entries: list[_Entry], batch: _Batch, reply: Reply | None) -> None:
        if batch.generation != self._generation:
            return
        self._active_requests -= 1
        try:
            for entry in entries:
                if entry.batch is batch:
                    entry.batch = None
            live = [entry for entry in entries if entry.outcome is None]
            if reply is None:
                self._retry_or_fail(live, None, None)
                return
            status = reply.status
            if 200 <= status < 300:
                acks = _acknowledgements(reply.body) if reply.body is not None else None
                over_quota: list[_Entry] = []
                resets_at: str | None = None
                accepted = 0
                for entry in live:
                    ack = acks.get(entry.event_id, "unexpected") if acks is not None else "unexpected"
                    if ack == "accepted":
                        accepted += 1
                        self._finish(entry, "sent")
                    elif ack == "unexpected":
                        self._finish(entry, "failed", LSquareError("send_failed", "Unexpected response: LSquare did not acknowledge the event.", status=status))
                    elif isinstance(ack, tuple) and ack[0] == "quota_exceeded":
                        if resets_at is None:
                            resets_at = ack[1]
                        over_quota.append(entry)
                    elif isinstance(ack, tuple):
                        self._finish(entry, "failed", LSquareError("rejected", f"LSquare rejected the event: {ack[0]}."))
                if over_quota:
                    holding = self._start_holdback(resets_at)
                    for entry in over_quota:
                        if holding and self._holdback is not None:
                            self._holdback.dropped += 1
                            self._finish(entry, "failed")
                        else:
                            self._finish(entry, "failed", LSquareError("rejected", "LSquare rejected the event: quota_exceeded."))
                elif batch.probe and accepted > 0 and self._holdback is not None and self._holdback is batch.holdback:
                    self._end_holdback()
            elif status == 429 or status >= 500:
                self._retry_or_fail(live, status, _parse_retry_after(reply.retry_after, self._now()))
            else:
                for entry in live:
                    self._finish(entry, "failed", LSquareError("send_failed", f"LSquare answered {status}; not retried.", status=status))
        except Exception:
            for entry in entries:
                if entry.outcome is None:
                    self._finish(entry, "failed", LSquareError("internal_error", "Sending failed unexpectedly."))
        finally:
            self._pump_requests()

    def _retry_or_fail(self, entries: list[_Entry], status: int | None, retry_after_ms: float | None) -> None:
        """Each event retries on its own count; one delay for the batch, so its events go together again."""
        retrying: list[_Entry] = []
        for entry in entries:
            if entry.attempts < self._max_attempts:
                retrying.append(entry)
                continue
            message = (
                f"Network error after {entry.attempts} attempts."
                if status is None
                else f"LSquare answered {status} after {entry.attempts} attempts."
            )
            self._finish(entry, "failed", LSquareError("send_failed", message, status=status))
        if not retrying:
            return
        attempts = max(entry.attempts for entry in retrying)
        backoff = self._random() * min(RETRY_CAP_MS, RETRY_BASE_MS * 2 ** (attempts - 1))
        delay = backoff if retry_after_ms is None else min(retry_after_ms, RETRY_AFTER_CAP_MS)
        for entry in retrying:
            entry.stage = "retry"
            entry.retry_at = self._clock() + delay / 1000

            def again(entry: _Entry = entry) -> None:
                entry.timer = None
                entry.retry_at = None
                if entry.outcome is None:
                    self._dispatch(entry)

            def fire(again: Callable[[], None] = again) -> None:
                self._locked(again)

            entry.timer = self._scheduler.call_later(delay / 1000, fire)
            # A flush whose deadline comes first can't see this event finish: it stops waiting for it.
            self._release(entry)

    def _past_deadline(self, entry: _Entry) -> bool:
        return (self._clock() - entry.created_at) * 1000 >= self._before_send_timeout_ms

    def _timeout_error(self) -> LSquareError:
        return LSquareError("hook_timeout", f"before_send did not finish within {self._before_send_timeout_ms:g} ms; the event was dropped.")

    def _past_ceiling(self, entry: _Entry) -> bool:
        """Past the finalization ceiling: twice the hook's deadline, which a hook that returned in
        time gets for what it returned to be read, rebuilt and published (py-runtime-2)."""
        return (self._clock() - entry.created_at) * 1000 >= 2 * self._before_send_timeout_ms

    def _unfinished_error(self) -> LSquareError:
        return LSquareError(
            "hook_failed",
            f"before_send returned in time, but what it returned wasn't processed within {2 * self._before_send_timeout_ms:g} ms; the event was dropped.",
        )

    def _deadline(self, entry: _Entry) -> None:
        """An event's hook deadline, or its finalization ceiling: first, hooks whose loop closed
        under them fail (hook_failed, not hook_timeout) and give their slots back to the events
        waiting for one (parity-5); then this one expires, unless its hook returned in time."""
        self._reclaim_hooks()
        self._expire(entry)
        # A slot given back (a closed loop's, or a returned hook's at its ceiling) goes to the next.
        self._pump()

    def _expire(self, entry: _Entry) -> None:
        """Past its deadline: an event waiting for its hook, or whose hook still runs, fails
        `hook_timeout`. One whose hook returned in time is left to be published until the
        finalization ceiling, at which it fails `hook_failed`, its signal never aborted (py-runtime-2).
        Decided under the run's lock, which the hook's return is marked under, with the time read
        there too: a return marked after this was past the deadline, and is late."""
        if entry.outcome is not None or entry.stage not in ("waiting", "hook"):
            return
        run = entry.run
        if entry.stage == "hook" and run is not None:
            with run.lock:
                returned_at = run.returned_at
                now = self._clock()
            if returned_at is not None and (returned_at - entry.created_at) * 1000 < self._before_send_timeout_ms:
                if self._past_ceiling(entry):
                    self._finish(entry, "failed", self._unfinished_error())
                elif not entry.ceiling:
                    entry.ceiling = True
                    self._clear_timer(entry)
                    until = entry.created_at + 2 * self._before_send_timeout_ms / 1000
                    entry.timer = self._scheduler.call_later(max(0.0, until - self._clock()), lambda: self._locked(lambda: self._deadline(entry)))
                return
            if returned_at is None and (now - entry.created_at) * 1000 < self._before_send_timeout_ms:
                # Not yet (a timer early on a clock a test moves): its own timer comes again.
                return
        self._finish(entry, "failed", self._timeout_error())

    def _reclaim_hooks(self) -> bool:
        """A `before_send` handed to an app's loop that then closed without running its tasks to
        their end (`run_until_complete()`, then `close()`), its task or the callback creating it
        still queued there, never settles: that loop never runs anything again. Its event fails
        `hook_failed` now, not at its deadline, whether its hook was still running or had returned
        with its settling still queued there; an expired one's slot and bytes come back now, once,
        in either case too.
        One on a loop that has only stopped is left alone: that loop may run again. Nor is one whose
        result a hook thread is reading: that thread gives the slot back as it publishes. Checked
        as an event is captured, as hooks are started and at each hook deadline. True when a slot
        came back. Never raises."""
        changed = False
        try:
            for entry in list(self._live.values()):
                run = entry.run
                if entry.stage != "hook" or run is None:
                    continue
                with run.lock:
                    stuck = run.loop is not None and run.state in ("running", "returned") and run.loop.is_closed()
                if stuck:
                    self._finish(entry, "failed", LSquareError("hook_failed", "before_send's event loop closed before it settled; the event was dropped."))
                    changed = True
            for entry in list(self._hung):
                run = entry.run
                if run is None or not entry.zombie:
                    self._hung.discard(entry)
                    continue
                with run.lock:
                    # Still running there, or returned with its settling queued there (its
                    # deadline's cancel ended it in that loop's last pass): either way, only that
                    # loop could settle it. A settling that ran there first and whose publication
                    # comes after this finds the run done, and releases nothing.
                    gone = run.loop is not None and run.state in ("running", "returned") and run.loop.is_closed()
                    if gone:
                        run.state = "done"
                        run.loop = None
                if not gone:
                    continue
                # Its slot, its count and its bytes, once: a settling after all finds it done.
                self._hung.discard(entry)
                entry.zombie = False
                self._active_hooks -= 1
                self._zombies -= 1
                self._zombie_bytes -= entry.bytes
                changed = True
        except Exception:
            pass
        return changed

    def _pinned(self, except_: _Entry | None = None) -> tuple[int, int]:
        """What eviction can't free: expired hooks still running, and events inside a running hook."""
        count = self._zombies
        size = self._zombie_bytes
        for entry in self._live.values():
            if entry.stage == "hook" and entry is not except_:
                count += 1
                size += entry.bytes
        return count, size

    def _make_room(self, size: int) -> bool:
        count, pinned = self._pinned()
        if count + 1 > self._max_queue_events or pinned + size > self._max_queue_bytes:
            return False
        return self._evict_until(
            lambda: len(self._live) + self._zombies + 1 <= self._max_queue_events
            and self._live_bytes + self._zombie_bytes + size <= self._max_queue_bytes
        )

    def _fits_after_growth(self, grown: _Entry) -> bool:
        if grown.bytes + self._pinned(grown)[1] > self._max_queue_bytes:
            return False
        return self._evict_until(lambda: self._live_bytes + self._zombie_bytes <= self._max_queue_bytes, grown)

    def _evict_until(self, fits: Callable[[], bool], keep: _Entry | None = None) -> bool:
        """Drops the oldest events until `fits()`. Events inside a hook free nothing; events with a
        request in flight go last: aborting them may not stop LSquare receiving them."""
        if fits():
            return True
        for in_flight in (False, True):
            for entry in list(self._live.values()):
                if entry is keep or entry.stage == "hook" or (entry.stage == "sending") != in_flight:
                    continue
                self._finish(
                    entry,
                    "failed",
                    LSquareError(
                        "queue_full",
                        "Dropped to stay within the memory limits; its request was aborted and may still have reached LSquare."
                        if in_flight
                        else "Dropped to stay within the memory limits.",
                    ),
                )
                if fits():
                    return True
        return False

    def _finish(self, entry: _Entry, outcome: Outcome, error: LSquareError | None = None) -> None:
        """Final state. Anything that happens to the event later (a late hook, a response) is ignored."""
        if entry.outcome is not None:
            return
        entry.outcome = outcome
        self._clear_timer(entry)
        batch = entry.batch
        entry.batch = None
        # Its request carries other events too: aborted only once none of them still needs it.
        if batch is not None:
            batch.live -= 1
            if batch.live == 0:
                batch.abort.abort()
        run = entry.run if entry.stage == "hook" else None
        idle = False
        if run is not None:
            with run.lock:
                # Nothing of the app's runs for it: not yet started, or returned with its result
                # not yet taken by a worker. Let go of now, with what it holds (py-runtime-2).
                idle = run.state in ("starting", "returned", "queued")
                if idle:
                    run.state = "done"
                    run.output.clear()
                    run.loop = None
                run.source.clear()
        if idle:
            # Its slot comes back now, once: whatever settles or publishes it later finds it done.
            self._active_hooks -= 1
            entry.hook_signal = None
            entry.hook_cancel = None
        elif entry.stage == "hook":
            entry.zombie = True
            self._zombies += 1
            self._zombie_bytes += entry.bytes
            # Watched until it settles: should its hook be on an app's loop that closes under it,
            # now or once a sync hook's awaitable is handed there, the slot comes back (parity-5).
            self._hung.add(entry)
            # Only a hook still running is aborted: one that has returned, its _hook_done still to
            # come, never sees its signal fire. Decided under the signal's lock, which its hook's
            # return marks.
            listeners = entry.hook_signal._abort() if entry.hook_signal is not None else None
            if listeners:
                # The app's callbacks: in the event's context, one level deeper, so one that
                # captures can't feed itself.
                run_in = (entry.context or contextvars.Context()).copy()
                depth = entry.depth + 1
                self._signal(lambda: run_in.run(_run_all, listeners, depth))
            if listeners is not None and entry.hook_cancel is not None:
                _quietly(entry.hook_cancel)
            # Used once: whatever still holds the entry holds neither, nor the task the cancel
            # holds, should the hook's settling never come (its loop stopped or closed first).
            entry.hook_signal = None
            entry.hook_cancel = None
        self._live.pop(entry.seq, None)
        self._queued.pop(entry, None)
        self._live_bytes -= entry.bytes
        if self._holdback is not None and self._holdback.check is entry:
            self._holdback.check = None
        # Its report goes to the callback thread before anything waiting for the event wakes, so a
        # wait that then waits for that thread (`_delivered`) ends after it.
        if error is not None:
            self._report(
                replace(error, event_id=error.event_id or entry.event_id),
                entry.event if entry.redacted else None,
                entry.depth,
                entry.context,
                entry.loop,
                entry.bytes,
            )
        waiters, entry.waiters = entry.waiters, None
        for waiter in waiters or ():
            _quietly(waiter)
        # The report took its own copy. Whatever still holds this entry holds no content from here.
        entry.event = None
        entry.draft = None
        entry.context = None
        self._release(entry)

    def _wait_for(self, entries: list[_Entry], timeout_ms: float) -> _Wait | None:
        deadline = self._clock() + timeout_ms / 1000
        wait = _Wait(deadline=deadline, pending={entry for entry in entries if not _settled_by(entry, deadline)})
        if not wait.pending:
            return None
        self._waits.add(wait)
        return wait

    def _end_wait(self, wait: _Wait) -> None:
        with self._lock:
            self._waits.discard(wait)
        wait.end()

    def _release(self, entry: _Entry) -> None:
        for wait in list(self._waits):
            if entry not in wait.pending or not _settled_by(entry, wait.deadline):
                continue
            wait.pending.discard(entry)
            if not wait.pending:
                self._waits.discard(wait)
                wait.end()

    def _guard(self, entry: _Entry, step: Callable[[], None]) -> None:
        try:
            step()
        except Exception:
            self._finish(entry, "failed", LSquareError("internal_error", "Processing failed unexpectedly; the event was dropped."))

    def _clear_timer(self, entry: _Entry) -> None:
        if entry.timer is not None:
            entry.timer()
        entry.timer = None

    # -- Reports and logs ------------------------------------------------------------------------

    def _report(
        self,
        error: LSquareError,
        event: dict[str, Any] | None = None,
        origin: int | None = None,
        context: contextvars.Context | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        size: int = 0,
    ) -> None:
        """`event` is passed only once redacted, and only as a copy; `size` is its bytes, as the
        queue counts them. `origin`, `context` and `loop` are the depth, context and loop of the
        code it's about, given together; without `origin`, the reporting code's. The handler runs
        on the callback thread, so it can't re-enter the SDK in the middle of a step. The report
        holds a slot of its rate limit until the handler returns, or settles if it's async; past
        the bytes those slots may hold, it goes without the copy."""
        slot: _Slot | None = None
        try:
            depth = (current_depth() if origin is None else origin) + 1
            if depth > MAX_CALLBACK_DEPTH:
                return
            # Only a custom handler sees the event, so only then is it copied.
            copied = self._on_error is not None and event is not None
            # First, the slots of async handlers a closed loop left pending come back.
            self._retire_closed()
            slot = self._report_limit.take(size if copied else None, self._max_queue_bytes)
            if slot is None:
                return
            delivered = replace(error, suppressed=slot.suppressed) if slot.suppressed > 0 else error
            snapshot = copy.deepcopy(event) if slot.size is not None else None
            run_in, on = _origin(origin, context, loop)
            held = slot
            if self._callback(lambda: _holding(held, run_in.run, self._deliver, delivered, snapshot, depth, on, held)):
                slot = None
        except Exception:
            pass
        finally:
            if slot is not None:
                slot.release(refused=True)

    def _deliver(
        self,
        error: LSquareError,
        event: dict[str, Any] | None,
        depth: int,
        loop: asyncio.AbstractEventLoop | None = None,
        slot: _Slot | None = None,
    ) -> bool:
        """True when an async `on_error` or logger now holds `slot` until it settles."""
        try:
            handler = self._on_error
            if handler is not None:
                _depth.set(depth)
                if self._on_error_takes_event:
                    out = handler(error, event)
                else:
                    out = handler(error)
                return self._settle_callback(out, loop, slot)
            self._suppressed_errors += error.suppressed or 0
            now = self._clock() * 1000
            # One logged later than now was logged on a clock that has gone back since (see
            # _RateLimit.take): long ago.
            if 0 <= now - self._last_error_log < ERROR_LOG_INTERVAL_MS:
                self._suppressed_errors += 1
                return False
            more = f" ({self._suppressed_errors} more since the last report)" if self._suppressed_errors > 0 else ""
            self._last_error_log = now
            self._suppressed_errors = 0
            return self._write_log("warn", f"[lsquare] {error.code}: {error.message}{more}", depth, loop, slot)
        except BaseException:  # noqa: BLE001 - on_error must never break the app
            return False

    def _log(
        self,
        level: str,
        message: str,
        origin: int | None = None,
        context: contextvars.Context | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """Logs on the callback thread, within the depth and rate limits, from `origin`, `context`
        and `loop` as `_report` does. The line holds a slot of its rate limit until the logger
        returns, or settles if it's async."""
        slot: _Slot | None = None
        try:
            depth = (current_depth() if origin is None else origin) + 1
            if depth > MAX_CALLBACK_DEPTH:
                return
            # As in _report.
            self._retire_closed()
            slot = self._log_limit.take()
            if slot is None:
                return
            suppressed = slot.suppressed
            line = f"{message} [lsquare: {suppressed} earlier log lines suppressed]" if suppressed > 0 else message
            run_in, on = _origin(origin, context, loop)
            held = slot
            if self._callback(lambda: _holding(held, run_in.run, self._write_log, level, line, depth, on, held)):
                slot = None
        except Exception:
            pass
        finally:
            if slot is not None:
                slot.release(refused=True)

    def _callback(self, job: Callable[[], None]) -> bool:
        """Hands log lines and reports to the callback thread; False when it didn't take the job.
        Once the client is quiet, at exit, nothing more reaches it: the pool checks that and takes
        the job together, under its own lock, which is never held while a thread starts."""
        return self._callbacks.submit(job)

    def _signal(self, job: Callable[[], None]) -> None:
        """Hands a hook's abort listeners, one job for all of a signal's, to the signal thread, as
        `_callback` hands the rest. Past the jobs that may wait there, behind a listener that is
        slow or never returns, later ones are dropped."""
        if not self._signals.submit(job) and self._signals.full():
            self._warn_once("signal-backlog", "[lsquare] A hook's abort listener is slow or hasn't returned: later abort listeners are dropped.")

    def _warn_once(self, key: str, message: str) -> None:
        # Marked in one step (dict.setdefault), without the client's lock: the warning a sync
        # shutdown() gives on a loop's thread, from a signal handler say, never waits for it.
        first = object()
        if self._warned.setdefault(key, first) is first:
            self._log("warn", message)

    def _write_log(
        self, level: str, message: str, depth: int, loop: asyncio.AbstractEventLoop | None = None, slot: _Slot | None = None
    ) -> bool:
        """True when an async logger now holds `slot` until it settles."""
        try:
            method = logger_method(self._logger, level)
            if not callable(method):
                return False
            if self._custom_logger:
                _depth.set(depth)
            return self._settle_callback(method(message), loop, slot)
        except BaseException as error:  # noqa: BLE001 - logging must never break the app
            # Written on the calling thread on Pyodide: a SystemExit there goes on.
            if interrupts(error):
                raise
            return False

    def _started(self, started: concurrent.futures.Future[None]) -> None:
        with self._futures_lock:
            self._unstarted.pop(started, None)

    def _callback_done(self, finished: concurrent.futures.Future[Any], slot: _Slot | None = None) -> None:
        try:
            _retrieve(finished)
        finally:
            with self._futures_lock:
                self._callback_futures.pop(finished, None)
            if slot is not None:
                slot.release()

    def _retire_closed(self) -> None:
        """Lets go of the async loggers and `on_error`s still pending on a loop that has closed: an
        app's, closed without running its tasks to their end, or the private one, closing after its
        last pass with one still queued. That loop never runs them on, nor settles their futures, so
        nothing else would give their slots back. Each gives its slot back, one never entered is
        closed, and the SDK keeps nothing of them; what one that was entered holds goes once the
        garbage collector finalizes it, never run on nor closed from here. One on a loop that has
        only stopped is kept: that loop may run again. Checked as a report or log line takes its
        slot, as a flush or shutdown starts, and at exit. Never raises."""
        try:
            if not self._callback_futures:
                return
            retired: list[Callable[[], None]] = []
            with self._futures_lock:
                for future, (loop, retire) in list(self._callback_futures.items()):
                    # Popped, not deleted: a signal handler's report, on this thread, may have
                    # retired it meanwhile.
                    if loop.is_closed() and self._callback_futures.pop(future, None) is not None:
                        retired.append(retire)
            for retire in retired:
                _quietly(retire)
        except Exception:
            pass

    def _settle_callback(self, out: Any, loop: asyncio.AbstractEventLoop | None = None, slot: _Slot | None = None) -> bool:
        """An async `on_error` or logger: run on the loop it was raised on while that one runs,
        else on the private loop, its failure retrieved. True when it was, and holds `slot` until
        it settles, or that loop closes first (see _retire_closed)."""
        if not inspect.isawaitable(out):
            return False
        coroutine = out if inspect.iscoroutine(out) else _as_coroutine(out)
        started: concurrent.futures.Future[None] = concurrent.futures.Future()
        wrapper, close = acknowledging(coroutine, started)
        handed = False
        try:
            on: asyncio.AbstractEventLoop | None = None
            if loop is None or loop.is_closed() or not loop.is_running():
                # If the private loop's thread can't start, the future fails: the slot comes back.
                future, owner = self._private_loop.run(wrapper)
            else:
                future = asyncio.run_coroutine_threadsafe(wrapper, loop)
                on = owner = loop
            handed = True
            future.add_done_callback(lambda finished: close())

            def retire() -> None:
                # Its loop closed first: it neither starts nor settles there now (see _retire_closed).
                _resolve(started)
                close()
                if slot is not None:
                    slot.release()

            # Still running when its job returned: the exit waits for these too, and, until they
            # have started, what waits for reports to be delivered (see _delivered).
            with self._futures_lock:
                self._callback_futures[future] = (owner, retire)
                self._unstarted[started] = (on, time.monotonic())
            started.add_done_callback(self._started)
            # One that ends without having started (its loop's thread never did, say) never will.
            future.add_done_callback(lambda finished: _resolve(started))
            future.add_done_callback(lambda finished: self._callback_done(finished, slot))
            return True
        except Exception:
            if not handed:
                close()
            return False


# -- Helpers -------------------------------------------------------------------------------------


#: The clients a forked child resets, each under a key of its own, never reused, with the ID of the
#: process it belongs to, and by a weak reference with no callback: nothing of the SDK's runs as a
#: client is collected, where a signal handler's SystemExit or KeyboardInterrupt would only be
#: reported, never raised (as a weakref callback's or a finalizer's is). Added to as each is made
#: (see LSquare._infrastructure), which also drops the references to clients collected since
#: (`_register_forkable`).
_forkable: dict[int, tuple[int, weakref.ref[LSquare]]] = {}
_forkable_keys = itertools.count()
_FORKABLE_PRUNED_AT = 16
#: Making a client prunes the registry once it holds more than this: twice the clients alive at
#: the last pruning, and at least _FORKABLE_PRUNED_AT, so making one costs constant time on average.
_forkable_limit = _FORKABLE_PRUNED_AT


def _register_forkable(client: LSquare) -> None:
    """Adds `client`, as this process's, then, past the limit, drops the references whose client is
    gone. Each step is one operation on the dict, and its keys are read in one copy: another
    thread, or a signal handler making a client, changes nothing being iterated, and a key once
    dead stays dead."""
    global _forkable_limit
    _forkable[next(_forkable_keys)] = (os.getpid(), weakref.ref(client))
    if len(_forkable) <= _forkable_limit:
        return
    for key in list(_forkable):
        entry = _forkable.get(key)
        if entry is not None and entry[1]() is None:
            _forkable.pop(key, None)
    _forkable_limit = max(_FORKABLE_PRUNED_AT, 2 * len(_forkable))


def _reset_forked() -> None:
    """In a forked child, the one handler registered for all clients: each still alive that the
    parent had starts empty (see LSquare._after_fork), whatever another's reset raised. The first
    failure is then raised, for the interpreter to report as it would one from a handler of its
    own. The entries are copied in one operation, before any is followed: a client a signal
    handler makes meanwhile is the child's own, and needs no reset. So is one an earlier
    `after_in_child` callback of the app's made, which is the child's, by its process: what it
    captured is kept. Each client is made the child's before it is reset, so a fork of this child
    resets it in turn, whether its reset here raised or not."""
    failure: BaseException | None = None
    pid = os.getpid()
    for key, (owner, reference) in list(_forkable.items()):
        if owner == pid:
            continue
        client = reference()
        if client is None:
            continue
        _forkable[key] = (pid, reference)
        try:
            client._after_fork()
        except BaseException as error:  # noqa: BLE001 - the next client is reset all the same
            if failure is None:
                failure = error
    if failure is not None:
        raise failure


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_forked)


def swallow(value: Any) -> None:
    """Retrieves the failure of whatever awaitable a callback returned, so none goes unnoticed."""
    try:
        if isinstance(value, (asyncio.Future, concurrent.futures.Future)):
            value.add_done_callback(_retrieve)
    except Exception:
        pass


def _holding(slot: _Slot, run: Callable[..., bool], *args: Any) -> None:
    """A report's or log line's job on the callback thread: `run(*args)` delivers it, and its slot
    is released once that returns, unless an async handler now holds it until it settles."""
    held = False
    try:
        held = run(*args)
    finally:
        if not held:
            slot.release()


def _retrieve(finished: Any) -> None:
    """Reads a finished future's failure, so it isn't reported as never retrieved. A cancelled one
    has none: `exception()` would raise, and the futures module would log that on stderr."""
    try:
        if not finished.cancelled():
            finished.exception()
    except BaseException as error:  # noqa: BLE001 - a callback's failure is the app's
        # Read in a done-callback, perhaps on the main thread's loop: a signal handler's
        # SystemExit there goes on.
        if interrupts(error):
            raise


async def _as_coroutine(awaitable: Any) -> Any:
    return await awaitable


def _close_unstarted(awaitable: Any) -> None:
    """Closes a coroutine none of which has run: one refused before it started."""
    try:
        if inspect.iscoroutine(awaitable) and inspect.getcoroutinestate(awaitable) == inspect.CORO_CREATED:
            awaitable.close()
    except Exception:
        pass


class _Uncopied(Exception):
    """The event couldn't be copied for `before_send`, which never ran."""


#: What a hook thread makes of a `before_send` run, besides None and the rebuilt event: its event
#: was dropped before the hook was called (_GONE), couldn't be copied for it (_UNCOPIED), or what it
#: returned isn't a Mapping (_NOT_A_MAPPING) or couldn't be shaped and redacted (_REDACTION_FAILED).
_GONE = object()
_UNCOPIED = object()
#: No hook thread could take what before_send returned, outside the exit's flush (see _start_hook).
_UNREAD = object()
_NOT_A_MAPPING = object()
_REDACTION_FAILED = object()


@dataclass(frozen=True, eq=False)
class _Rebuilt:
    """The event rebuilt from what `before_send` returned, and its bytes as the queue counts them."""

    built: Any
    size: int


def _hook_result(out: Any, draft: Any, context: Any) -> Any:
    """What `before_send` returned, read where no lock is held (a hook thread): the app's code runs
    as a Mapping is read, which may block, or take a lock of the app's that a capturing thread holds.
    The event rebuilt from it, and measured (`_Rebuilt`), the error building it gave, or one of the
    markers above."""
    if not isinstance(out, Mapping):
        return _NOT_A_MAPPING
    try:
        built = _event.after_hook(out, draft, context)
        if not built.ok:
            return built
        return _Rebuilt(built, _event.byte_size(built.draft.event))
    except Exception:
        return _REDACTION_FAILED


async def _watched(make: Callable[[], Any], returned: Callable[[], None]) -> Any:
    """A hook's task: awaits what `make()` gives, and calls `returned` the moment that returns or
    raises, inside the task, where its done-callbacks would run a loop iteration later. Not when
    it is closed unfinished (GeneratorExit): its task was destroyed with its loop, and the garbage
    collector finalizes it on whatever thread it runs, perhaps inside one of the SDK's locks, so it
    takes none. Its event or run is gone by then."""
    try:
        out = await make()
    except GeneratorExit:
        raise
    except (SystemExit, KeyboardInterrupt) as error:
        returned()
        if interrupts(error):
            raise
        # Off the main thread the hook's failure like any other, never out of the loop running it.
        raise Interrupted(type(error).__name__) from error
    except BaseException:
        returned()
        raise
    returned()
    return out


def _resolve(future: concurrent.futures.Future[None]) -> None:
    if not future.done():
        try:
            future.set_result(None)
        except concurrent.futures.InvalidStateError:
            pass


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _origin(
    origin: int | None, context: contextvars.Context | None, loop: asyncio.AbstractEventLoop | None
) -> tuple[contextvars.Context, asyncio.AbstractEventLoop | None]:
    """Where a report or log line runs: a copy of the context, and the loop, of the code it's
    about. Given with its depth, both are taken as they are: an empty context (which is false)
    and no loop are the event's or call's too, never replaced by the reporting code's. Without
    its depth, the reporting code's."""
    if origin is None:
        return contextvars.copy_context(), _running_loop()
    return (contextvars.Context() if context is None else context).copy(), loop


def _other_loop_running() -> bool:
    """trio (or another loop) running on this thread, which async hooks can't use."""
    trio = sys.modules.get("trio")
    if trio is None:
        return False
    try:
        trio.lowlevel.current_task()
        return True
    except Exception:
        return False


def _accepts(fn: Callable[..., Any], count: int) -> bool:
    """Whether `fn` takes `count` positional arguments (a hook without `signal`, an `on_error`
    without `event`)."""
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return True
    positional = 0
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            return True
        if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD):
            positional += 1
    return positional >= count


def _settled_by(entry: _Entry, deadline: float) -> bool:
    """Finished, or not due to try again before `deadline`."""
    return entry.outcome is not None or (entry.retry_at is not None and entry.retry_at >= deadline)


def _wait_futures(futures: list[concurrent.futures.Future[None]], timeout_ms: float, clock: Callable[[], float]) -> float:
    """Waits up to `timeout_ms` for these futures. Returns the milliseconds spent."""
    started = clock()
    concurrent.futures.wait(futures, timeout=max(0.0, timeout_ms) / 1000)
    return min(timeout_ms, (clock() - started) * 1000)


async def _offload(fn: Callable[[], Any]) -> Any:
    """Runs a blocking wait without blocking the loop: asyncio's or trio's thread offload, or
    inline when no loop runs."""
    if _running_loop() is not None:
        return await asyncio.get_running_loop().run_in_executor(None, fn)
    if _other_loop_running():
        trio = sys.modules["trio"]
        return await trio.to_thread.run_sync(fn)
    return fn()


async def _on_trio(register: Callable[[Callable[[], None]], Any], ready: Callable[[], bool], timeout_s: float) -> None:
    """Waits on trio without a thread, so the wait keeps to its timeout whatever trio's thread
    limiter holds: `register(wake)` arranges for `wake()`, from any thread, when it's time."""
    trio = sys.modules["trio"]
    event = trio.Event()
    token = trio.lowlevel.current_trio_token()

    def wake() -> None:
        try:
            token.run_sync_soon(event.set)
        except Exception:
            # The run has ended: nothing waits any more.
            pass

    register(wake)
    if ready():
        return
    with trio.move_on_after(max(0.0, timeout_s)):
        await event.wait()


def _top_up(future: concurrent.futures.Future[None], deadline: float) -> float:
    """After one wait of DELIVERED_GRACE_S for `future`: the seconds still to wait, by the monotonic
    clock the grace is measured with, so a wait woken a hair early still lasts the grace a later
    `_delivered` checks for. 0 when it's done, or when that clock stood still (a test freezing
    time), which can't hold the wait for longer than the one grace."""
    left = deadline - time.monotonic()
    return left if not future.done() and 0 < left < DELIVERED_GRACE_S else 0.0


def _all_done(futures: list[concurrent.futures.Future[None]]) -> concurrent.futures.Future[None]:
    """A future done once all of these are."""
    together: concurrent.futures.Future[None] = concurrent.futures.Future()
    left = [len(futures)]
    lock = threading.Lock()

    def one(_: Any) -> None:
        with lock:
            left[0] -= 1
            last = left[0] == 0
        if last:
            _resolve(together)

    if not futures:
        _resolve(together)
    for future in futures:
        future.add_done_callback(one)
    return together


async def _await_futures(futures: list[concurrent.futures.Future[None]], timeout_ms: float, clock: Callable[[], float]) -> float:
    if _running_loop() is None:
        if _other_loop_running():
            started = clock()
            together = _all_done(futures)
            await _on_trio(lambda wake: together.add_done_callback(lambda _: wake()), together.done, timeout_ms / 1000)
            return min(timeout_ms, (clock() - started) * 1000)
        spent: float = await _offload(lambda: _wait_futures(futures, timeout_ms, clock))
        return spent
    started = clock()
    wrapped = [asyncio.wrap_future(future) for future in futures]
    await asyncio.wait(wrapped, timeout=max(0.0, timeout_ms) / 1000)
    return min(timeout_ms, (clock() - started) * 1000)


async def _await_wait(wait: _Wait, timeout_ms: float) -> None:
    if _running_loop() is None:
        if _other_loop_running():
            await _on_trio(wait.on_end.append, wait.ended.is_set, timeout_ms / 1000)
            return
        await _offload(lambda: wait.ended.wait(max(0.0, timeout_ms) / 1000))
        return
    loop = asyncio.get_running_loop()
    done: asyncio.Future[None] = loop.create_future()

    def settle() -> None:
        if not done.done():
            done.set_result(None)

    def wake() -> None:
        try:
            loop.call_soon_threadsafe(settle)
        except RuntimeError:
            pass

    wait.on_end.append(wake)
    if wait.ended.is_set():
        return
    try:
        await asyncio.wait_for(asyncio.shield(done), timeout=max(0.0, timeout_ms) / 1000)
    except asyncio.TimeoutError:
        pass


def _ack_bytes(event_id: str) -> int:
    """What one event can add to an acknowledgement: its ID with the longest rejection, and a comma."""
    return int(_event.byte_size({"eventId": event_id, "reason": "quota_exceeded", "resetsAt": LONGEST_RESETS_AT})) + 1


def _iso_now(epoch_ms: float) -> str:
    """As JavaScript's `toISOString`: UTC, milliseconds, `Z`."""
    moment = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _parse_time(value: str | None) -> float | None:
    """An ISO 8601 time as epoch milliseconds, or None."""
    if not isinstance(value, str):
        return None
    try:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        moment = datetime.fromisoformat(text)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.timestamp() * 1000
    except Exception:
        return None


def _reset_time(resets_at: str | None, now: float) -> float:
    """When a daily limit resets: `resetsAt` if it's in the next 25 hours, else the next 00:00 UTC."""
    parsed = _parse_time(resets_at)
    if parsed is not None and now < parsed <= now + 25 * 3_600_000:
        return parsed
    today = datetime.fromtimestamp(now / 1000, tz=timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return (today + timedelta(days=1)).timestamp() * 1000


def _parse_retry_after(header: str | None, now: float) -> float | None:
    if not header:
        return None
    text = header.strip()
    if text.isdigit() and text.isascii():
        # As TypeScript's Number(): past a double's range it's infinite, which the retry caps at
        # RETRY_AFTER_CAP_MS. int() would overflow the float, or refuse 4,300 digits and more.
        return float(text) * 1000.0
    try:
        moment = parsedate_to_datetime(text)
    except Exception:
        return None
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, moment.timestamp() * 1000 - now)


def _acknowledgements(text: str) -> dict[str, Any] | None:
    """Each event counts as sent only if LSquare names it as accepted or duplicate, and not also as
    rejected. None when the body isn't an acknowledgement at all."""
    import json

    try:
        # NaN, Infinity and -Infinity aren't JSON, which TypeScript's JSON.parse refuses too.
        body = json.loads(text, parse_constant=_not_json)
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    acks: dict[str, Any] = {}
    for key in ("accepted", "duplicates"):
        listed = body.get(key)
        if isinstance(listed, list):
            for item in listed:
                if isinstance(item, str):
                    acks[item] = "accepted"
    rejected = body.get("rejected")
    if isinstance(rejected, list):
        for item in rejected:
            if not isinstance(item, dict) or not isinstance(item.get("eventId"), str):
                continue
            event_id = item["eventId"]
            if event_id in acks:
                acks[event_id] = "unexpected"
                continue
            reason = item.get("reason")
            reason = reason if isinstance(reason, str) and reason in REJECTION_REASONS else "unspecified"
            resets_at = item.get("resetsAt") if isinstance(item.get("resetsAt"), str) else None
            acks[event_id] = (reason, resets_at)
    return acks


def _not_json(constant: str) -> Any:
    raise ValueError(f"{constant} isn't JSON")


def _dry_run_summary(entry: _Entry) -> str:
    """One line per event: shape only, never content."""
    event = entry.event or {}
    kind = event.get("kind")
    event_id = str(event.get("eventId", ""))
    summary: dict[str, Any] = {
        "kind": kind,
        "eventId": "the call" if kind == "tool_call" else ("from turnId, hashed" if event_id.startswith("k/") else "random: no turnId"),
        "fields": sorted(key for key in event if key not in _normalize.SDK_FIELDS),
        "bytes": entry.bytes,
    }
    if kind == "turn" and isinstance(event.get("toolCalls"), list):
        summary["toolCalls"] = len(event["toolCalls"])
    agent = event.get("agent")
    if kind == "tool_call" and isinstance(agent, dict) and isinstance(agent.get("toolCalls"), list):
        summary["toolCalls"] = len(agent["toolCalls"])
    for key in ("truncated", "dropped", "redactions"):
        if event.get(key):
            summary[key] = event[key]
    if entry.ignored_fields:
        summary["ignoredFields"] = entry.ignored_fields
    if entry.ignored_tool_call_fields:
        summary["ignoredToolCallFields"] = entry.ignored_tool_call_fields
    return f"[lsquare dry-run] {_jsjson.dumps(summary)}"


@dataclass
class _Endpoint:
    url: str


@dataclass
class _Problem:
    problem: str


def _resolve_endpoint(raw: str) -> _Endpoint | _Problem:
    """As TypeScript's resolveEndpoint: `new URL`'s reading (see _url), then the https rule."""
    try:
        url = _url.parse(raw)
    except _url.Unsupported as error:
        if error.scheme == "https":
            return _Problem("The endpoint's host can't be an internationalized domain name")
        # No such host is local: TypeScript refuses it as not https, if it reads it at all.
        return _Problem("The endpoint must use https (http only for localhost)")
    except Exception:
        return _Problem("The endpoint is not a valid URL")
    secure = url.scheme == "https" or (url.scheme == "http" and url.host in LOCAL_HOSTS)
    if not secure:
        return _Problem("The endpoint must use https (http only for localhost)")
    origin = f"{url.scheme}://{url.host}" + (f":{url.port}" if url.port is not None else "")
    return _Endpoint(f"{origin}{url.pathname.rstrip('/')}/v1/turns")


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except Exception:
        # An int too large for a double (10**400): JavaScript's Infinity, which isn't usable either.
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def _positive(value: Any, fallback: float) -> float:
    number = _number(value)
    return number if number is not None and number > 0 else fallback


def _duration(value: Any, fallback: float) -> float:
    return min(_positive(value, fallback), MAX_TIMER_MS)


def _wait_time(value: Any, fallback: float) -> float:
    """Like `_duration`, but 0 is allowed: don't wait."""
    number = _number(value)
    return min(number, MAX_TIMER_MS) if number is not None and number >= 0 else fallback


def _wait_arg(value: Any, fallback: float) -> float:
    """A flush's or shutdown's `timeout_ms`: one that can't be read (a number whose `__float__`
    raises) counts as not given, as one that isn't a number does."""
    try:
        return _wait_time(value, fallback)
    except Exception:
        return fallback


def _positive_int(value: Any, fallback: int) -> int:
    """At least 1: a fraction such as 0.5 must not floor to a limit of 0, which would block everything."""
    return max(1, int(_positive(value, fallback)))


def _id_ok(value: Any) -> bool:
    checked = _normalize.id_value(value)
    return isinstance(checked, dict) or hasattr(checked, "value")


def _turn_id(value: Any) -> str | None:
    checked = _normalize.turn_id_value(value)
    if isinstance(checked, dict) and "value" in checked:
        return str(checked["value"])
    inner = getattr(checked, "value", None)
    return inner if isinstance(inner, str) else None


__all__ = ["CallScope", "FlushResult", "HookRun", "Internal", "LSquare", "LSquareError", "Origin", "Signal", "start_thread"]
