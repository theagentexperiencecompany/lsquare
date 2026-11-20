"""The hook lifecycle fixes of the deep review's last Python and parity findings, 31 October 2026:
one section per finding, each test failing before its fix unless it says it's a guard. The
MCP-dependent tests are in wrapper/hooks_fixes_wrapper_test.py; TypeScript's side is in
packages/lsquare/test/hooks-fixes.test.ts. No sleep orders anything here: events, gates, an
injected clock and the scheduler's own wake do, and nothing asserts how long anything took."""

from __future__ import annotations

import asyncio
import contextvars
import copy
import gc
import inspect
import json
import linecache
import subprocess
import sys
import textwrap
import threading
import types
import weakref
from collections.abc import Callable, Coroutine, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from lsquare import LSquare, _client, _event, _jsjson, _threads
from lsquare._threads import PrivateLoop, schedule_on

from helpers import FakeTransport, Recorder, collected, eventually

KEY = "lsq_test_key"


class Clock:
    """The client's clock, moved by the test: its scheduler's timers come due as it moves, once
    woken (see `moved`)."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def client(transport: Any = None, recorder: Recorder | None = None, **options: Any) -> LSquare:
    recorder = recorder or Recorder()
    options.setdefault("logger", recorder)
    options.setdefault("on_error", recorder.on_error)
    options.setdefault("_random", lambda: 0.0)
    return LSquare(api_key=KEY, endpoint="http://localhost:1", _transport=transport or FakeTransport(), **options)


def moved(lsquare: LSquare, clock: Clock, to: float) -> None:
    """Moves the client's clock `to` seconds past its start and has the scheduler look again."""
    clock.now = 1_000.0 + to
    lsquare._scheduler.wake()


def delivered(lsquare: LSquare, transport: FakeTransport, count: int) -> bool:
    """Flushes, then waits for `count` events to have reached the transport: one may have been sent
    before the flush began, which then doesn't count it."""
    return lsquare.flush(timeout_ms=5_000).pending == 0 and eventually(lambda: len(transport.events) == count)


def turn(n: int = 0, **extra: Any) -> dict[str, Any]:
    return {"conversationId": f"conv-{n}", "turnId": n, "question": f"question {n}", **extra}


def owned(lsquare: LSquare) -> bool:
    """Whether the calling thread holds the client's lock."""
    return bool(lsquare._lock._is_owned())  # type: ignore[attr-defined]


class Gated(Mapping[str, Any]):
    """What a hook returns: a Mapping whose first read waits for `gate`, noting whether the thread
    reading it held the client's lock, and which thread that was."""

    def __init__(self, data: Mapping[str, Any], lsquare: LSquare, gate: threading.Event, entered: threading.Event) -> None:
        self._data = dict(data)
        self._lsquare = lsquare
        self._gate = gate
        self._entered = entered
        self.locked: list[bool] = []
        self.threads: list[str] = []

    def __getitem__(self, key: str) -> Any:
        if not self._entered.is_set():
            self.locked.append(owned(self._lsquare))
            self.threads.append(threading.current_thread().name)
            self._entered.set()
            self._gate.wait(10)
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


def codes(recorder: Recorder) -> list[str]:
    return recorder.codes()


# -- py-runtime-2: what before_send returned is read off the client's lock, its deadline by when it returned ----


def test_what_before_send_returned_is_read_off_the_client_lock_and_an_unrelated_capture_goes_on() -> None:
    # A Mapping whose read blocks (a lazy proxy, an app lock): before, it was read on the scheduler
    # under the client's lock, which every capture, flush and deadline needs.
    gate, entered = threading.Event(), threading.Event()
    returned: list[Gated] = []
    transport = FakeTransport()
    holder: list[LSquare] = []

    def before_send(event: dict[str, Any]) -> Any:
        if event["conversationId"] != "conv-1":
            return event
        returned.append(Gated(event, holder[0], gate, entered))
        return returned[0]

    lsquare = client(transport, before_send=before_send, before_send_timeout_ms=60_000)
    holder.append(lsquare)
    try:
        lsquare.capture(turn(1))
        assert entered.wait(5)
        done = threading.Event()

        def capture() -> None:
            lsquare.capture(turn(2))
            done.set()

        threading.Thread(target=capture, daemon=True).start()
        # Returned while the read is still blocked: it takes the lock only to queue its event.
        assert done.wait(5)
        assert returned[0].locked == [False]
    finally:
        gate.set()
    assert delivered(lsquare, transport, 2)
    assert sorted(event["conversationId"] for event in transport.events) == ["conv-1", "conv-2"]
    lsquare.shutdown(timeout_ms=0)


def test_a_hook_that_returned_in_time_is_on_time_however_late_the_client_takes_its_result() -> None:
    # Its deadline is when it returned, not when the scheduler, held up meanwhile by another step,
    # takes its result: before, that was decided only then, and the event failed hook_timeout.
    clock = Clock()
    recorder = Recorder()
    transport = FakeTransport()
    entered, go = threading.Event(), threading.Event()

    def before_send(event: dict[str, Any]) -> Any:
        entered.set()
        go.wait(5)
        # Returns half way to its deadline.
        clock.now = 1_000.5
        return event

    lsquare = client(transport, recorder, before_send=before_send, before_send_timeout_ms=1_000, _clock=clock)
    lsquare.capture(turn(1))
    assert entered.wait(5)
    blocking, unblock = threading.Event(), threading.Event()

    def block() -> None:
        blocking.set()
        unblock.wait(5)

    lsquare._scheduler.call_soon(block)
    assert blocking.wait(5)
    go.set()
    # Its result is on its way to the scheduler, which is still held up.
    assert eventually(lambda: len(lsquare._scheduler._soon) >= 1)
    moved(lsquare, clock, 1.5)
    unblock.set()
    assert delivered(lsquare, transport, 1)
    assert "hook_timeout" not in codes(recorder)
    assert len(transport.events) == 1
    lsquare.shutdown(timeout_ms=0)


def test_an_on_time_return_survives_its_deadline_while_the_client_still_reads_it() -> None:
    # Guard (the skeptic's deadline_b): the client's own rebuilding, here held past the deadline,
    # never counts against the hook. TypeScript decides it the same way, as the hook settles.
    clock = Clock()
    recorder = Recorder()
    transport = FakeTransport()
    gate, entered = threading.Event(), threading.Event()
    real = _event.after_hook

    def slow(out: Any, draft: Any, context: Any) -> Any:
        entered.set()
        gate.wait(5)
        return real(out, draft, context)

    lsquare = client(transport, recorder, before_send=lambda event: event, before_send_timeout_ms=1_000, _clock=clock)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(_event, "after_hook", slow)
        lsquare.capture(turn(1))
        assert entered.wait(5)
        moved(lsquare, clock, 1.5)
        # The deadline has come and gone (its step, then the timer for the ceiling): once the
        # scheduler has run what was due, the read ends.
        ran = threading.Event()
        lsquare._scheduler.call_later(0, ran.set)
        assert ran.wait(5)
        gate.set()
        assert delivered(lsquare, transport, 1)
    assert codes(recorder) == []
    lsquare.shutdown(timeout_ms=0)


def test_an_on_time_result_never_published_fails_at_the_ceiling_without_aborting_and_keeps_its_slot_until_read() -> None:
    # The read never ends (a blocking Mapping): at twice its deadline the event fails hook_failed,
    # said to be unfinished processing, not a late hook; the hook returned, so its signal isn't
    # aborted. The thread still reading keeps its slot and its event's bytes until it is done, and
    # what it reads then can't bring the event back.
    clock = Clock()
    recorder = Recorder()
    transport = FakeTransport()
    gate, entered = threading.Event(), threading.Event()
    signals: list[Any] = []
    holder: list[LSquare] = []

    def before_send(event: dict[str, Any], signal: Any) -> Any:
        signals.append(signal)
        return Gated(event, holder[0], gate, entered)

    lsquare = client(transport, recorder, before_send=before_send, before_send_timeout_ms=1_000, max_concurrent_hooks=1, _clock=clock)
    holder.append(lsquare)
    try:
        lsquare.capture(turn(1))
        assert entered.wait(5)
        (entry,) = list(lsquare._live.values())
        moved(lsquare, clock, 1.5)
        # Past its deadline it is left to be read: the ceiling comes next.
        assert eventually(lambda: getattr(entry, "ceiling", None) is True)
        assert entry.outcome is None
        moved(lsquare, clock, 2.0)
        assert recorder.wait_for(lambda: "hook_failed" in codes(recorder))
        assert codes(recorder) == ["hook_failed"]
        assert "wasn't processed within 2000 ms" in recorder.errors[0].message
        assert signals[0].aborted is False
        with lsquare._lock:
            assert (lsquare._active_hooks, lsquare._zombies, lsquare._zombie_bytes > 0) == (1, 1, True)
    finally:
        gate.set()
    assert eventually(lambda: (lsquare._active_hooks, lsquare._zombies, lsquare._zombie_bytes) == (0, 0, 0))
    assert lsquare.flush(timeout_ms=1_000).sent == 0
    assert transport.events == [] and codes(recorder) == ["hook_failed"]
    lsquare.shutdown(timeout_ms=0)


def test_an_event_expired_while_its_sync_before_send_is_stuck_keeps_nothing_of_it_with_the_hook() -> None:
    # Guard: the draft (the event before redaction) is taken for the hook's result only once that
    # is read, not held by the hook's closures from its start.
    clock = Clock()
    entered, go = threading.Event(), threading.Event()

    def before_send(event: dict[str, Any]) -> Any:
        entered.set()
        go.wait(10)
        return event

    lsquare = client(before_send=before_send, before_send_timeout_ms=1_000, _clock=clock)
    try:
        lsquare.capture(turn(1))
        assert entered.wait(5)
        (entry,) = list(lsquare._live.values())
        draft = weakref.ref(entry.draft)
        del entry
        moved(lsquare, clock, 1.5)
        assert eventually(lambda: not lsquare._live)
        assert collected(draft)
    finally:
        go.set()
    lsquare.shutdown(timeout_ms=0)


def test_an_async_before_send_s_copy_and_rebuild_run_on_a_hook_thread_never_on_the_app_s_loop() -> None:
    # Guard: the copy the hook is given and the rebuild of what it returns cost the app's loop
    # nothing (the skeptic's loop-cost probe), though the hook itself runs there.
    threads: dict[str, list[str]] = {"copy": [], "rebuild": [], "hook": []}
    real_copy = copy.deepcopy
    real_after = _event.after_hook

    def copying(value: Any, memo: Any = None) -> Any:
        if isinstance(value, dict) and "eventId" in value:
            threads["copy"].append(threading.current_thread().name)
        return real_copy(value, memo)

    def rebuilding(out: Any, draft: Any, context: Any) -> Any:
        threads["rebuild"].append(threading.current_thread().name)
        return real_after(out, draft, context)

    async def before_send(event: dict[str, Any]) -> Any:
        threads["hook"].append(threading.current_thread().name)
        return event

    transport = FakeTransport()
    lsquare = client(transport, before_send=before_send)

    async def main() -> None:
        lsquare.capture(turn(1))
        await lsquare.aflush(timeout_ms=5_000)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(copy, "deepcopy", copying)
        patch.setattr(_event, "after_hook", rebuilding)
        here: list[str] = []

        def run() -> None:
            here.append(threading.current_thread().name)
            asyncio.run(main())

        thread = threading.Thread(target=run, name="app-loop", daemon=True)
        thread.start()
        thread.join(10)
    assert threads["hook"] == ["app-loop"]
    assert threads["copy"] and threads["rebuild"]
    assert all(name.startswith("lsquare-hook") for name in threads["copy"] + threads["rebuild"]), threads
    assert len(transport.events) == 1
    lsquare.shutdown(timeout_ms=0)


# -- py-runtime-26: a hook's copy, its result's rebuild and a batch's serialization take no client lock ---------


def test_a_hook_s_copy_its_result_s_rebuild_and_a_batch_s_serialization_take_no_client_lock() -> None:
    seen: dict[str, list[bool]] = {"copy": [], "rebuild": [], "serialize": []}
    holder: list[LSquare] = []
    real_copy = copy.deepcopy
    real_after = _event.after_hook
    real_dumps = _jsjson.dumps

    def copying(value: Any, memo: Any = None) -> Any:
        if holder and isinstance(value, dict) and "eventId" in value:
            seen["copy"].append(owned(holder[0]))
        return real_copy(value, memo)

    def rebuilding(out: Any, draft: Any, context: Any) -> Any:
        seen["rebuild"].append(owned(holder[0]))
        return real_after(out, draft, context)

    def serializing(value: Any) -> str:
        if isinstance(value, dict) and "events" in value:
            seen["serialize"].append(owned(holder[0]))
        return str(real_dumps(value))

    transport = FakeTransport()
    lsquare = client(transport, before_send=lambda event: event)
    holder.append(lsquare)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(copy, "deepcopy", copying)
        patch.setattr(_event, "after_hook", rebuilding)
        patch.setattr(_jsjson, "dumps", serializing)
        lsquare.capture(turn(1))
        assert delivered(lsquare, transport, 1)
    assert seen == {"copy": [False], "rebuild": [False], "serialize": [False]}
    lsquare.shutdown(timeout_ms=0)


def test_a_request_at_exit_gets_the_time_left_once_its_batch_is_serialized() -> None:
    # Guard: serializing takes some of the exit's time (here 1.5 s of 2 on the client's clock), and
    # the request gets only what is left, never the whole deadline as before serializing.
    clock = Clock()
    timeouts: list[float] = []
    real_dumps = _jsjson.dumps

    def serializing(value: Any) -> str:
        if isinstance(value, dict) and "events" in value:
            clock.now += 1.5
        return str(real_dumps(value))

    def transport(body: bytes, timeout: float, abort: Any) -> Any:
        timeouts.append(timeout)
        return FakeTransport()(body, timeout, abort)

    lsquare = client(transport, _clock=clock)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(_jsjson, "dumps", serializing)
        with lsquare._lock:
            lsquare._exit_deadline = clock() + 2.0
        lsquare.capture(turn(1))
        assert eventually(lambda: len(timeouts) == 1)
    assert timeouts[0] == pytest.approx(0.5)
    lsquare.shutdown(timeout_ms=0)


def test_a_shutdown_while_a_batch_is_serialized_posts_nothing() -> None:
    # Guard: shutdown() and the client's generation are checked again once the batch is serialized,
    # outside the lock now, before anything is posted.
    posted: list[bytes] = []
    real_dumps = _jsjson.dumps
    holder: list[LSquare] = []
    results: list[Any] = []

    def serializing(value: Any) -> str:
        if isinstance(value, dict) and "events" in value and not results:
            results.append(holder[0].shutdown(timeout_ms=0))
        return str(real_dumps(value))

    def transport(body: bytes, timeout: float, abort: Any) -> Any:
        posted.append(body)
        return FakeTransport()(body, timeout, abort)

    lsquare = client(transport)
    holder.append(lsquare)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(_jsjson, "dumps", serializing)
        lsquare.capture(turn(1))
        assert eventually(lambda: bool(results))
        assert lsquare._senders.drain(5)
    assert posted == []
    assert results[0].failed == 1


# -- py-runtime-5: a late awaitable never starts; a stopped private loop takes no hook --------------------------


def body_noted(ran: list[str]) -> Coroutine[Any, Any, None]:
    async def body() -> None:
        ran.append("started")
        await asyncio.sleep(0)

    return body()


def test_an_awaitable_a_sync_before_send_returns_past_its_deadline_never_starts() -> None:
    clock = Clock()
    recorder = Recorder()
    entered, go = threading.Event(), threading.Event()
    ran: list[str] = []
    made: list[Coroutine[Any, Any, None]] = []

    def before_send(event: dict[str, Any]) -> Any:
        entered.set()
        go.wait(5)
        made.append(body_noted(ran))
        return made[0]

    lsquare = client(recorder=recorder, before_send=before_send, before_send_timeout_ms=1_000, max_concurrent_hooks=1, _clock=clock)
    lsquare.capture(turn(1))
    assert entered.wait(5)
    moved(lsquare, clock, 1.5)
    assert recorder.wait_for(lambda: codes(recorder) == ["hook_timeout"])
    go.set()
    assert eventually(lambda: bool(made) and inspect.getcoroutinestate(made[0]) == inspect.CORO_CLOSED)
    assert eventually(lambda: (lsquare._active_hooks, lsquare._zombies) == (0, 0))
    assert ran == []
    assert lsquare._private_loop._current is None
    lsquare.shutdown(timeout_ms=0)


def test_an_awaitable_a_sync_before_send_returns_after_shutdown_starts_no_loop() -> None:
    entered, go = threading.Event(), threading.Event()
    ran: list[str] = []
    made: list[Coroutine[Any, Any, None]] = []

    def before_send(event: dict[str, Any]) -> Any:
        entered.set()
        go.wait(5)
        made.append(body_noted(ran))
        return made[0]

    lsquare = client(before_send=before_send, before_send_timeout_ms=60_000)
    lsquare.capture(turn(1))
    assert entered.wait(5)
    lsquare.shutdown(timeout_ms=0)
    go.set()
    assert eventually(lambda: bool(made) and inspect.getcoroutinestate(made[0]) == inspect.CORO_CLOSED)
    assert ran == []
    assert lsquare._private_loop._current is None


def test_a_stopped_private_loop_refuses_a_hook_and_starts_no_thread_for_it() -> None:
    private = PrivateLoop()
    private.stop()
    made: list[int] = []
    settled: list[Any] = []

    def make() -> Any:
        made.append(1)
        return asyncio.sleep(0)

    with pytest.raises(RuntimeError):
        schedule_on(private, make, contextvars.Context(), lambda error, out: settled.append(error))
    assert (made, private._current) == ([], None)


class Landing:
    """Runs `nested()` once, at the `at`-th line run in `code` itself, on the thread it traces:
    where a signal handler's call back in can land."""

    def __init__(self, code: types.CodeType, at: int, nested: Callable[[], Any]) -> None:
        self.code, self.at, self.nested = code, at, nested
        self.lines = 0
        self.fired = False
        #: The source of the line it landed on.
        self.line = ""

    def run(self, fn: Callable[[], Any]) -> Any:
        sys.settrace(self._call)
        try:
            return fn()
        finally:
            sys.settrace(None)

    def _call(self, frame: types.FrameType, event: str, arg: Any) -> Any:
        return self._line if event == "call" and frame.f_code is self.code else None

    def _line(self, frame: types.FrameType, event: str, arg: Any) -> Any:
        if event == "line":
            self.lines += 1
            if self.lines == self.at and not self.fired:
                self.fired = True
                self.line = linecache.getline(frame.f_code.co_filename, frame.f_lineno).strip()
                self.nested()
        return self._line


def test_a_stop_called_back_in_anywhere_in_a_hook_s_hand_off_to_the_private_loop_refuses_it() -> None:
    # shutdown() from a signal handler, landing at each line of the hand-off, the loop's making
    # included: the hook is never created there, its `done` hears why, and the loop made for it
    # stops and closes rather than run on with no one to stop it.
    code = PrivateLoop.hand.__code__
    here = threading.current_thread()
    at = 0
    while True:
        at += 1
        private = PrivateLoop()
        made: list[int] = []
        settled: list[Any] = []
        loops: list[asyncio.AbstractEventLoop] = []
        real_new = asyncio.new_event_loop

        def new_loop() -> asyncio.AbstractEventLoop:
            # Every loop in the process is made through this, and a client an earlier test left can
            # make one at any moment: only those made on this thread, by the hand-off.
            loop = real_new()
            if threading.current_thread() is here:
                loops.append(loop)
            return loop

        def make() -> Any:
            made.append(1)
            return asyncio.sleep(0)

        landing = Landing(code, at, private.stop)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(asyncio, "new_event_loop", new_loop)
            try:
                landing.run(lambda: schedule_on(private, make, contextvars.Context(), lambda error, out: settled.append(error)))
            except RuntimeError:
                settled.append("refused")
        if not landing.fired or landing.line == "return out":
            # Past the loop's thread start: handed over before the stop, a hook like any other,
            # which shutdown() then cancels where it runs.
            private.stop()
            break
        assert eventually(lambda: all(loop.is_closed() for loop in loops)), at
        assert made == [], at
        assert len(settled) == 1, at


def test_a_callback_handed_over_after_stop_never_lands_on_a_loop_winding_down(monkeypatch: pytest.MonkeyPatch) -> None:
    # An async on_error or logger after shutdown() runs on a loop made around the stop, which runs
    # one pass, winds down and closes. The first one's loop is held between its wind-down and its
    # close (a thread switch there): before, it was still the published loop, and the second
    # callback, queued on it then, never ran, its future never settled.
    # Only this PrivateLoop's loops are held: one left by another test can wind down meanwhile.
    real = _threads._wind_down
    wound, go = threading.Event(), threading.Event()
    mine: set[asyncio.AbstractEventLoop] = set()

    def held(loop: asyncio.AbstractEventLoop, cancel: bool = False) -> None:
        real(loop, cancel)
        if loop in mine and not wound.is_set():
            wound.set()
            go.wait(10)

    monkeypatch.setattr(_threads, "_wind_down", held)
    private = PrivateLoop()
    begin = private._begin

    def begun(loop: asyncio.AbstractEventLoop, start: Any) -> None:
        mine.add(loop)
        begin(loop, start)

    monkeypatch.setattr(private, "_begin", begun)
    private.stop()

    async def report(name: str) -> str:
        await asyncio.sleep(0)
        return name

    first, first_loop = private.run(report("first"))
    try:
        assert first.result(5) == "first"
        assert wound.wait(5)
        assert not first_loop.is_closed()
        second, second_loop = private.run(report("second"))
    finally:
        go.set()
    assert second.result(5) == "second"
    assert second_loop is not first_loop
    assert eventually(lambda: first_loop.is_closed() and second_loop.is_closed())
    assert private._current is None


# -- py-runtime-10: a dropped client's private loop stops, from that loop's own thread -----------------------------


def test_a_client_dropped_without_shutdown_stops_its_private_loop_and_its_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_threads, "IDLE_S", 0.05)

    async def before_send(event: dict[str, Any]) -> Any:
        return event

    transport = FakeTransport()
    lsquare = client(transport, before_send=before_send)
    lsquare.capture(turn(1))
    assert delivered(lsquare, transport, 1)
    current = lsquare._private_loop._current
    assert current is not None
    loop = current[0]
    owner = weakref.ref(lsquare)
    del lsquare, current
    assert collected(owner)
    assert eventually(loop.is_closed)


def test_a_dropped_client_s_private_loop_cancels_what_a_hook_left_running_there(monkeypatch: pytest.MonkeyPatch) -> None:
    # A hook's child task, left awaiting: before, the loop's wind-down gathered it forever, keeping
    # the loop and its thread.
    monkeypatch.setattr(_threads, "IDLE_S", 0.05)
    cancelled = threading.Event()
    children: list[asyncio.Task[None]] = []

    async def child() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def before_send(event: dict[str, Any]) -> Any:
        children.append(asyncio.ensure_future(child()))
        return event

    transport = FakeTransport()
    lsquare = client(transport, before_send=before_send)
    lsquare.capture(turn(1))
    assert delivered(lsquare, transport, 1)
    current = lsquare._private_loop._current
    assert current is not None
    loop = current[0]
    owner = weakref.ref(lsquare)
    del lsquare, current
    assert collected(owner)
    assert cancelled.wait(5)
    assert eventually(loop.is_closed)


def test_a_live_idle_client_keeps_its_private_loop_until_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    # Guard: what an async hook made on the loop may be bound to it, so the loop never idles out
    # while its client lives; its own thread checks the client, again and again, until shutdown().
    monkeypatch.setattr(_threads, "IDLE_S", 0.02)

    async def before_send(event: dict[str, Any]) -> Any:
        return event

    transport = FakeTransport()
    lsquare = client(transport, before_send=before_send)
    checks: list[int] = []
    real = lsquare._private_loop._owner
    assert real is not None

    def owner() -> Any:
        checks.append(1)
        return real()

    lsquare._private_loop._owner = owner
    lsquare.capture(turn(1))
    assert delivered(lsquare, transport, 1)
    current = lsquare._private_loop._current
    assert current is not None
    assert eventually(lambda: len(checks) >= 3)
    assert current[0].is_running() and not current[0].is_closed()
    lsquare.shutdown(timeout_ms=0)
    assert eventually(current[0].is_closed)


def test_the_client_registers_no_garbage_collection_time_code() -> None:
    # Guard: no finalizer, nor a weak reference with a callback, to the client: code run at
    # collection time can swallow a signal handler's SystemExit on the main thread.
    transport = FakeTransport()
    lsquare = client(transport, before_send=lambda event: event)
    lsquare.capture(turn(1))
    assert delivered(lsquare, transport, 1)
    refs = weakref.getweakrefs(lsquare)
    # The private loop's owner and the exit's reference (one object, as Python shares a plain
    # weak reference): callback-free. The fork registry's own entry (a WeakValueDictionary's) only
    # drops itself from that dictionary.
    assert any(ref.__callback__ is None for ref in refs)
    assert all(ref.__callback__ is None or isinstance(ref, weakref.KeyedRef) for ref in refs)
    assert not any(finalizer.peek() is not None and finalizer.peek()[0] is lsquare for finalizer in list(weakref.finalize._registry))  # type: ignore[attr-defined]
    lsquare.shutdown(timeout_ms=0)


# -- parity-5: a hook on a loop the app closed fails hook_failed, its slot back once ---------------------------


def on_closed_loop(lsquare: LSquare, wait_started: threading.Event | None = None) -> asyncio.AbstractEventLoop:
    """Captures an event on an app's loop that then closes without running its tasks to their end
    (`run_until_complete()`, then `close()`): its hook's task pending there (`wait_started`), or
    the callback creating it still queued."""
    loop = asyncio.new_event_loop()

    async def main() -> None:
        lsquare.capture(turn(1))
        if wait_started is not None:
            while not wait_started.is_set():
                await asyncio.sleep(0)

    if wait_started is None:
        # Under the lock, so the hook is handed to the loop only once it has stopped.
        with lsquare._lock:
            loop.run_until_complete(main())
        assert eventually(lambda: len(getattr(loop, "_ready", ())) > 0)
    else:
        loop.run_until_complete(main())
    loop.close()
    return loop


@pytest.mark.parametrize("when", ["before its task was created", "while its task is pending"])
def test_an_async_before_send_whose_loop_the_app_closed_fails_hook_failed_and_the_next_hook_runs_at_once(when: str) -> None:
    # Before, it held its slot until its deadline (a minute here), and the next event waited for it.
    recorder = Recorder()
    transport = FakeTransport()
    started = threading.Event()
    hooked: list[str] = []

    async def before_send(event: dict[str, Any]) -> Any:
        hooked.append(event["conversationId"])
        if event["conversationId"] == "conv-1":
            started.set()
            await asyncio.Event().wait()
        return event

    lsquare = client(transport, recorder, before_send=before_send, before_send_timeout_ms=60_000, max_concurrent_hooks=1)
    on_closed_loop(lsquare, started if when == "while its task is pending" else None)
    lsquare.capture(turn(2))
    assert delivered(lsquare, transport, 1)
    assert [event["conversationId"] for event in transport.events] == ["conv-2"]
    assert codes(recorder) == ["hook_failed"]
    assert "event loop closed" in recorder.errors[0].message
    with lsquare._lock:
        assert (lsquare._active_hooks, lsquare._zombies, lsquare._zombie_bytes) == (0, 0, 0)
    lsquare.shutdown(timeout_ms=0)


def test_an_async_before_send_that_expired_then_lost_its_loop_gives_its_slot_back_once() -> None:
    # Past its deadline it fails hook_timeout and is kept as a zombie; its loop closing afterwards
    # gives its slot back, once: a settling that came after all would release nothing twice.
    clock = Clock()
    recorder = Recorder()
    transport = FakeTransport()
    started = threading.Event()
    loop = asyncio.new_event_loop()

    async def before_send(event: dict[str, Any]) -> Any:
        if event["conversationId"] == "conv-1":
            started.set()
            await asyncio.Event().wait()
        return event

    async def main() -> None:
        lsquare.capture(turn(1))
        while not started.is_set():
            await asyncio.sleep(0)

    lsquare = client(transport, recorder, before_send=before_send, before_send_timeout_ms=1_000, max_concurrent_hooks=1, _clock=clock)
    loop.run_until_complete(main())
    (entry,) = list(lsquare._live.values())
    moved(lsquare, clock, 1.5)
    assert recorder.wait_for(lambda: codes(recorder) == ["hook_timeout"])
    with lsquare._lock:
        assert (lsquare._active_hooks, lsquare._zombies) == (1, 1)
    loop.close()
    lsquare.capture(turn(2))
    assert delivered(lsquare, transport, 1)
    with lsquare._lock:
        assert (lsquare._active_hooks, lsquare._zombies, lsquare._zombie_bytes) == (0, 0, 0)
        # A settling after all, as from a loop that ran it: nothing released twice.
        lsquare._hook_done(entry, None, False, lsquare._generation)
        assert (lsquare._active_hooks, lsquare._zombies, lsquare._zombie_bytes) == (0, 0, 0)
    assert codes(recorder) == ["hook_timeout"]
    lsquare.shutdown(timeout_ms=0)


def test_an_async_before_send_that_expired_then_returned_as_its_loop_stopped_gives_its_slot_back_once_that_loop_closes() -> None:
    # Past its deadline it fails hook_timeout and is kept as a zombie. Its deadline's cancel ends
    # it in the loop's last pass: it has returned, its settling still queued on the loop, which the
    # app then closes. Before, only a zombie still running was reclaimed: this one kept its slot for
    # good, and the next event's hook never ran.
    clock = Clock()
    recorder = Recorder()
    transport = FakeTransport()
    started = threading.Event()
    hooked: list[str] = []
    loop = asyncio.new_event_loop()

    async def before_send(event: dict[str, Any]) -> Any:
        hooked.append(event["conversationId"])
        if event["conversationId"] == "conv-1":
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # Its cleanup done, in the pass the app's loop stops at.
                asyncio.get_running_loop().stop()
                raise
        return event

    async def main() -> None:
        lsquare.capture(turn(1))
        while not started.is_set():
            await asyncio.sleep(0)

    lsquare = client(transport, recorder, before_send=before_send, before_send_timeout_ms=1_000, max_concurrent_hooks=1, _clock=clock)
    loop.run_until_complete(main())
    (entry,) = list(lsquare._live.values())
    run = entry.run
    assert run is not None
    moved(lsquare, clock, 1.5)
    assert recorder.wait_for(lambda: codes(recorder) == ["hook_timeout"])
    with lsquare._lock:
        # Expired under this lock, its cancel queued on the loop there.
        assert entry.zombie
    # Should that cancel never come, the loop stops all the same, and the state below says so.
    loop.call_later(5, loop.stop)
    loop.run_forever()
    assert run.state == "returned"
    with lsquare._lock:
        assert (lsquare._active_hooks, lsquare._zombies) == (1, 1)
    loop.close()
    lsquare.capture(turn(2))
    assert delivered(lsquare, transport, 1)
    assert [event["conversationId"] for event in transport.events] == ["conv-2"]
    assert hooked == ["conv-1", "conv-2"]
    with lsquare._lock:
        assert (lsquare._active_hooks, lsquare._zombies, lsquare._zombie_bytes) == (0, 0, 0)
        # A settling after all, as from a loop that ran it: nothing released twice.
        lsquare._hook_done(entry, None, False, lsquare._generation, run=run)
        assert (lsquare._active_hooks, lsquare._zombies, lsquare._zombie_bytes) == (0, 0, 0)
    assert codes(recorder) == ["hook_timeout"]
    lsquare.shutdown(timeout_ms=0)


def test_a_hook_that_returned_in_time_keeps_its_slot_while_a_hook_thread_reads_it_though_its_loop_closed() -> None:
    # Guard: closing the hook's loop doesn't reclaim what a hook thread is reading: that thread
    # publishes the event, sent, and gives the slot back, once.
    recorder = Recorder()
    transport = FakeTransport()
    gate, entered = threading.Event(), threading.Event()
    holder: list[LSquare] = []

    async def before_send(event: dict[str, Any]) -> Any:
        if event["conversationId"] == "conv-1":
            return Gated(event, holder[0], gate, entered)
        return event

    lsquare = client(transport, recorder, before_send=before_send, before_send_timeout_ms=60_000, max_concurrent_hooks=1)
    holder.append(lsquare)
    try:
        on_closed_loop(lsquare, entered)
        captured = threading.Event()

        def capture() -> None:
            lsquare.capture(turn(2))
            captured.set()

        threading.Thread(target=capture, daemon=True).start()
        assert captured.wait(5)
        with lsquare._lock:
            assert lsquare._active_hooks == 1
    finally:
        gate.set()
    assert delivered(lsquare, transport, 2)
    assert sorted(event["conversationId"] for event in transport.events) == ["conv-1", "conv-2"]
    assert codes(recorder) == []
    with lsquare._lock:
        assert (lsquare._active_hooks, lsquare._zombies) == (0, 0)
    lsquare.shutdown(timeout_ms=0)


def test_an_async_before_send_on_a_loop_that_has_only_stopped_keeps_its_slot_until_that_loop_runs_it() -> None:
    # Guard: a stopped loop may run again.
    recorder = Recorder()
    transport = FakeTransport()
    loop = asyncio.new_event_loop()

    async def before_send(event: dict[str, Any]) -> Any:
        return event

    async def main() -> None:
        lsquare.capture(turn(1))

    lsquare = client(transport, recorder, before_send=before_send, before_send_timeout_ms=60_000, max_concurrent_hooks=1)
    try:
        with lsquare._lock:
            loop.run_until_complete(main())
        assert eventually(lambda: len(getattr(loop, "_ready", ())) > 0)
        lsquare.capture(turn(2))
        with lsquare._lock:
            assert lsquare._active_hooks == 1
            assert [entry.stage for entry in lsquare._live.values()] == ["hook", "waiting"]
        loop.run_until_complete(asyncio.sleep(0))
        assert delivered(lsquare, transport, 2)
        assert codes(recorder) == []
    finally:
        loop.close()
        lsquare.shutdown(timeout_ms=0)


def test_an_async_identify_whose_loop_the_app_closed_fails_and_gives_its_slot_to_the_next() -> None:
    lsquare = client(max_concurrent_hooks=1)
    internal = lsquare._lsquare_internal()

    started = threading.Event()

    async def identify(given: Any) -> Any:
        started.set()
        await asyncio.Event().wait()

    loop = asyncio.new_event_loop()
    runs: list[Any] = []

    async def main() -> None:
        runs.append(internal.identify(identify, lambda signal: None, internal.origin()))
        # Its task pending there as the loop stops.
        while not started.is_set():
            await asyncio.sleep(0)

    loop.run_until_complete(main())
    loop.close()
    assert runs[0] is not None
    # The next admission finds the slot held by a loop that has closed: it fails, and gives it back.
    second = internal.identify(lambda given: {"userId": "u"}, lambda signal: None, internal.origin())
    assert second is not None
    error = runs[0].future.exception(5)
    assert isinstance(error, RuntimeError) and "closed" in str(error)
    assert second.future.result(5) == {"userId": "u"}
    assert eventually(lambda: internal.identifying["active"] == 0)
    lsquare.shutdown(timeout_ms=0)


def test_an_async_identify_whose_loop_the_app_closed_settles_when_it_is_aborted() -> None:
    lsquare = client()
    internal = lsquare._lsquare_internal()

    started = threading.Event()

    async def identify(given: Any) -> Any:
        started.set()
        await asyncio.Event().wait()

    loop = asyncio.new_event_loop()
    runs: list[Any] = []

    async def main() -> None:
        runs.append(internal.identify(identify, lambda signal: None, internal.origin()))
        # Its task pending there as the loop stops.
        while not started.is_set():
            await asyncio.sleep(0)

    loop.run_until_complete(main())
    loop.close()
    runs[0].abort()
    assert isinstance(runs[0].future.exception(5), RuntimeError)
    assert internal.identifying["active"] == 0
    lsquare.shutdown(timeout_ms=0)


# -- parity-25: a call's identify diagnostic is reported as its event is admitted -------------------------------


def call_record(call_id: str) -> dict[str, Any]:
    return {
        "callId": call_id,
        "capturedAt": "2026-10-31T00:00:00.000Z",
        "toolName": "search",
        "status": "completed",
        "durationMs": 1,
        "ids": {"userId": "u_1"},
        "diagnostics": True,
    }


def test_a_call_s_identify_diagnostic_is_reported_as_its_event_is_admitted_never_once_shutdown_refuses_it(monkeypatch: pytest.MonkeyPatch) -> None:
    # Building the event runs the app's code (its values' getters): a shutdown() landing there,
    # after the first check, still refuses the event, under the lock that admits it, and then the
    # call's identify_failed, which would claim the call was sent, isn't reported.
    recorder = Recorder()
    transport = FakeTransport()
    lsquare = client(transport, recorder)
    internal = lsquare._lsquare_internal()
    diagnostic = _client.LSquareError("identify_failed", "identify threw; the call was sent as limited diagnostics.")
    internal.record(call_record("11111111-1111-4111-8111-111111111111"), diagnostic=diagnostic)
    assert delivered(lsquare, transport, 1)
    real = _event.build_call
    second = call_record("22222222-2222-4222-8222-222222222222")

    def building(record: Any, *args: Any) -> Any:
        # This record's build only: another client's, meanwhile, would shut this one down before
        # the record is captured, which would then never meet a shutdown during its build.
        if record is second:
            lsquare.shutdown(timeout_ms=0)
        return real(record, *args)

    monkeypatch.setattr(_event, "build_call", building)
    internal.record(second, diagnostic=diagnostic)
    future = internal.delivered()
    future.result(5)
    assert codes(recorder) == ["identify_failed", "shutdown"]
    assert len(transport.events) == 1


# -- Codex's first review of these fixes, 1 November 2026 ------------------------------------------------------
#
# Finding 2: what an async before_send returned was read on the loop it settled on, the app's
# perhaps, whenever no hook thread could take it; and its copy was made where it was started, under
# the client's lock, whenever none could take that. Only at interpreter exit now.


def refusing_hook_threads(monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    """While the returned event is set, the system refuses to start a hook thread, as it may in
    normal operation (a process or memory limit): those already running still take jobs."""
    refusing = threading.Event()
    real = _threads.start_thread

    def starting(target: Callable[[], None], name: str) -> None:
        if refusing.is_set() and name.startswith("lsquare-hook"):
            raise RuntimeError("can't start new thread")
        real(target, name)

    monkeypatch.setattr(_threads, "start_thread", starting)
    return refusing


class Reads(Mapping[str, Any]):
    """What a hook returns: notes the thread each read of it runs on."""

    def __init__(self, data: Mapping[str, Any]) -> None:
        self._data = dict(data)
        self.threads: list[str] = []

    def __getitem__(self, key: str) -> Any:
        self.threads.append(threading.current_thread().name)
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


@pytest.mark.parametrize("main_thread", ["running", "finished"])
def test_what_an_async_before_send_returned_isnt_read_on_the_app_s_loop_when_no_hook_thread_can_start(main_thread: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # The hook's copy is made on a hook thread, which then idles out while the hook awaits; as it
    # returns, none runs and none can start. Before, what it returned was read right there, on the
    # app's loop (MainThread), where a getter blocks the loop, or deadlocks it waiting for it.
    # "finished": as for a server a non-daemon thread keeps running once the main thread has
    # returned, the interpreter exiting but no exit flush under way: still never read there.
    if main_thread == "finished":
        monkeypatch.setattr(_client, "exiting", lambda: True, raising=False)
    monkeypatch.setattr(_threads, "IDLE_S", 0.05)
    refusing = refusing_hook_threads(monkeypatch)
    recorder = Recorder()
    transport = FakeTransport()
    returned: list[Reads] = []
    holder: list[LSquare] = []

    async def before_send(event: dict[str, Any]) -> Any:
        if event["conversationId"] != "conv-1":
            return event
        # Polled, never timed: until no hook thread runs.
        for _ in range(2_000):
            if holder[0]._hooks.idle() and holder[0]._hooks._workers == 0:
                break
            await asyncio.sleep(0.005)
        refusing.set()
        returned.append(Reads(event))
        return returned[0]

    lsquare = client(transport, recorder, before_send=before_send, max_concurrent_hooks=1)
    holder.append(lsquare)
    flushed: list[Any] = []

    async def main() -> None:
        lsquare.capture(turn(1))
        flushed.append(await lsquare.aflush(timeout_ms=5_000))
        # Its slot came back: the next one runs, once a hook thread can start again.
        refusing.clear()
        lsquare.capture(turn(2))
        flushed.append(await lsquare.aflush(timeout_ms=5_000))

    asyncio.run(main())
    assert returned and returned[0].threads == []
    assert [(result.failed, result.sent) for result in flushed] == [(1, 0), (0, 1)]
    assert recorder.wait_for(lambda: len(recorder.errors) >= 1)
    assert codes(recorder) == ["hook_failed"]
    assert recorder.errors[0].message == "No thread could start to read what before_send returned; the event was dropped."
    assert [event["conversationId"] for event in transport.events] == ["conv-2"]
    lsquare.shutdown(timeout_ms=0)


def test_an_async_before_send_no_hook_thread_can_start_for_never_runs_and_isnt_copied_where_it_was_started(monkeypatch: pytest.MonkeyPatch) -> None:
    # Audit of the same fallback for the copy the hook is given: before, it was made where the hook
    # was started, under the client's lock, on the capturing thread here, and the hook ran.
    refusing = refusing_hook_threads(monkeypatch)
    refusing.set()
    recorder = Recorder()
    transport = FakeTransport()
    called: list[str] = []
    copies: list[str] = []
    real_copy = copy.deepcopy
    # Every copy in the process is made through this, another client's too (a report's, on its
    # scheduler's thread): only this test's events, by their mark. Never by thread: a copy made on
    # the wrong one is what this looks for.
    mark = "copied-on-a-hook-thread"

    def copying(value: Any, memo: Any = None) -> Any:
        if isinstance(value, dict) and "eventId" in value and value.get("orgId") == mark:
            copies.append(threading.current_thread().name)
        return real_copy(value, memo)

    async def before_send(event: dict[str, Any]) -> Any:
        called.append(event["conversationId"])
        return event

    lsquare = client(transport, recorder, before_send=before_send, max_concurrent_hooks=1)
    flushed: list[Any] = []

    async def main() -> None:
        lsquare.capture(turn(1, orgId=mark))
        flushed.append(await lsquare.aflush(timeout_ms=5_000))
        refusing.clear()
        lsquare.capture(turn(2, orgId=mark))
        flushed.append(await lsquare.aflush(timeout_ms=5_000))

    monkeypatch.setattr(copy, "deepcopy", copying)
    asyncio.run(main())
    monkeypatch.setattr(copy, "deepcopy", real_copy)
    assert copies and all(name.startswith("lsquare-hook") for name in copies), copies
    assert called == ["conv-2"]
    # The first fails as soon as its hook is refused, which the scheduler's thread can do before
    # aflush() starts, whose counts leave out what has already ended: that failure is read from its
    # report. The second's hook runs on this loop, which runs nothing until aflush() waits: counted.
    assert (flushed[0].sent, flushed[0].pending) == (0, 0)
    assert (flushed[1].failed, flushed[1].sent) == (0, 1)
    assert recorder.wait_for(lambda: len(recorder.errors) >= 1)
    assert codes(recorder) == ["hook_failed"]
    assert recorder.errors[0].message == "No thread could start for before_send; the event was dropped."
    assert [event["conversationId"] for event in transport.events] == ["conv-2"]
    assert not lsquare._live
    lsquare.shutdown(timeout_ms=0)


def test_in_the_exit_flush_an_async_before_send_no_hook_thread_can_take_still_runs_and_its_result_is_read() -> None:
    # Guard of the fallback kept for the exit's flush alone, where some Pythons (3.12.2) start no
    # thread at all: there what a hook returned is read, and a waiting hook copied and handed over,
    # on the thread that got to it, so that flush still sends both. Hook thread starts are refused
    # from the exit on, on every Python here; the private loop runs from before it. One slot: "a"
    # returns once the exit's flush is under way, then "b", waiting for that slot, starts there.
    tests = str(Path(__file__).resolve().parent)
    code = f"""
        import asyncio, atexit, sys, threading
        sys.path.insert(0, {tests!r})
        from helpers import FakeTransport
        from lsquare import LSquare, _threads

        _threads.IDLE_S = 0.05
        transport = FakeTransport()
        # Registered first, so it runs last: after the client's exit flush.
        atexit.register(lambda: print("sent", sorted(event["conversationId"] for event in transport.events), flush=True))
        holder = []
        started = threading.Event()

        async def before_send(event):
            lsquare = holder[0]
            if event["conversationId"] == "a":
                started.set()
                # Polled, never timed: until the exit's flush is under way and no hook thread runs.
                for _ in range(4000):
                    if lsquare._exit_deadline is not None and lsquare._hooks.idle() and lsquare._hooks._workers == 0:
                        break
                    await asyncio.sleep(0.005)
            return dict(event)

        class Quiet:
            def info(self, message): pass
            def warn(self, message): pass

        lsquare = LSquare(api_key="lsq_test", endpoint="http://localhost:1", _transport=transport, before_send=before_send,
                          max_concurrent_hooks=1, flush_timeout_ms=10_000, logger=Quiet(),
                          on_error=lambda error, event=None: print(error.code, flush=True))
        holder.append(lsquare)
        lsquare.capture({{"conversationId": "a", "turnId": 1}})
        lsquare.capture({{"conversationId": "b", "turnId": 2}})
        # Before the exit: "a" copied on a hook thread and running on the private loop.
        assert started.wait(10)
        real = _threads.start_thread

        def refusing(target, name):
            if name.startswith("lsquare-hook"):
                raise RuntimeError("can't start new thread")
            real(target, name)

        def at_exit():
            _threads.start_thread = refusing

        # Registered after the client: it runs before the client's exit flush.
        atexit.register(at_exit)
        """
    done = subprocess.run([sys.executable, "-c", textwrap.dedent(code)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["sent ['a', 'b']"], done.stdout + done.stderr


# Finding 5 (Python's side; TypeScript's is in hooks-fixes.test.ts): a build that failed after the
# app's code it runs shut the client down still reported the call's identify_failed, and its own
# failure, rather than the shutdown that refuses the event, as a build that succeeds does.


class ShutsDown(Mapping[str, Any]):
    """A call's IDs: reading userId runs `then` first (the app's code: a getter)."""

    def __init__(self, then: Callable[[], None]) -> None:
        self._then = then

    def __getitem__(self, key: str) -> Any:
        if key != "userId":
            raise KeyError(key)
        self._then()
        # Past the 512 bytes an ID may have: the build fails, invalid_id.
        return "u" * 600

    def __iter__(self) -> Iterator[str]:
        return iter(["userId"])

    def __len__(self) -> int:
        return 1


def failing_build(kind: str, lsquare: LSquare, monkeypatch: pytest.MonkeyPatch, shut: bool) -> dict[str, Any]:
    """A call record whose build fails, `kind` "raises" (an unexpected failure: redaction_failed) or
    "invalid" (invalid_id), after a shutdown() it runs when `shut`."""
    record = call_record("44444444-4444-4444-8444-444444444444")

    def then() -> None:
        if shut:
            lsquare.shutdown(timeout_ms=0)

    if kind == "invalid":
        record["ids"] = ShutsDown(then)
        return record
    real = _event.build_call

    def building(given: Any, *args: Any) -> Any:
        # This record's build only: another client's, meanwhile, would shut this one down first.
        if given is not record:
            return real(given, *args)
        then()
        raise RuntimeError("the build failed")

    monkeypatch.setattr(_event, "build_call", building)
    return record


@pytest.mark.parametrize("kind", ["raises", "invalid"])
def test_a_build_that_fails_after_the_app_code_it_runs_shut_the_client_down_reports_only_the_shutdown(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder()
    transport = FakeTransport()
    lsquare = client(transport, recorder)
    internal = lsquare._lsquare_internal()
    diagnostic = _client.LSquareError("identify_failed", "identify threw; the call was sent as limited diagnostics.")
    internal.record(failing_build(kind, lsquare, monkeypatch, shut=True), diagnostic=diagnostic)
    internal.delivered().result(5)
    assert codes(recorder) == ["shutdown"]
    assert transport.events == []


@pytest.mark.parametrize(("kind", "code"), [("raises", "redaction_failed"), ("invalid", "invalid_id")])
def test_a_build_that_fails_while_the_client_is_open_still_reports_the_call_s_diagnostic_then_its_failure(kind: str, code: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # Guard: an ordinary failure keeps its reports, the diagnostic first.
    recorder = Recorder()
    transport = FakeTransport()
    lsquare = client(transport, recorder)
    internal = lsquare._lsquare_internal()
    diagnostic = _client.LSquareError("identify_failed", "identify threw; the call was sent as limited diagnostics.")
    internal.record(failing_build(kind, lsquare, monkeypatch, shut=False), diagnostic=diagnostic)
    internal.delivered().result(5)
    assert codes(recorder) == ["identify_failed", code]
    assert transport.events == []
    lsquare.shutdown(timeout_ms=0)


# -- Codex's second review of these fixes, 1 November 2026 -----------------------------------------------------
#
# Finding 1: the exit's fallback (a hook copied, or what it returned read, on the thread that got to
# it) was decided by `_exit_deadline is not None and exiting()`. A sync shutdown() sets that deadline
# too, and once the main thread has returned a server on a non-daemon thread still serves: another
# thread's shutdown(), waiting for a call, then had what a hook returned read on the server's loop,
# where a getter blocks it. Only the client's own exit flush, which alone sets its flag, does that now.

EXIT_PHASE = """
import asyncio, concurrent.futures, json, sys, threading
from collections.abc import Mapping

sys.path.insert(0, sys.argv[1])
from helpers import FakeTransport
from lsquare import LSquare

# "prepare": no hook thread takes the hook's start; "finalize": none takes what it returned.
where = sys.argv[2]
transport = FakeTransport()
errors, reads, called = [], [], []
armed, refusing, ready = threading.Event(), threading.Event(), threading.Event()
state = {}


class Reads(Mapping):
    # What the hook returns: notes the thread each read of it runs on.
    def __init__(self, data):
        self._data = dict(data)

    def __getitem__(self, key):
        reads.append(threading.current_thread().name)
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)


async def before_send(event):
    if event["conversationId"] == "warm":
        raise RuntimeError("warm-up")
    called.append(threading.current_thread().name)
    if where == "finalize":
        refusing.set()
    return Reads(event)


class Quiet:
    def info(self, message): pass
    def warn(self, message): pass


lsquare = LSquare(api_key="lsq_test", endpoint="http://localhost:1", _transport=transport, before_send=before_send,
                  flush_timeout_ms=10_000, logger=Quiet(), on_error=lambda error, event=None: errors.append([error.code, error.message]))
internal = lsquare._lsquare_internal()
# No hook thread takes a job once refusing: none runs, and the system refuses to start one (as
# 3.12.2 does by then anyway).
real_submit = lsquare._hooks.submit
lsquare._hooks.submit = lambda job: False if refusing.is_set() else real_submit(job)
call = concurrent.futures.Future()
real_wait = lsquare._wait_calls


def wait_calls(calls, timeout_ms):
    # shutdown() waits for the call, its exit deadline set: the server captures now.
    state["main_alive"] = threading.main_thread().is_alive()
    armed.set()
    state["loop"].call_soon_threadsafe(state["waiting"].set_result, None)
    return real_wait(calls, timeout_ms)


lsquare._wait_calls = wait_calls
real_done = lsquare._hook_done


def hook_done(*args, **kwargs):
    try:
        return real_done(*args, **kwargs)
    finally:
        # The hook's result is published: the call is done, and shutdown() goes on.
        if armed.is_set() and not call.done():
            call.set_result(None)


lsquare._hook_done = hook_done


async def serve_main():
    loop = asyncio.get_running_loop()
    state["loop"] = loop
    state["waiting"], state["ended"] = loop.create_future(), loop.create_future()
    # Before the main thread returns: a hook thread and the callback thread, idle from then on.
    lsquare.capture({"conversationId": "warm", "turnId": 0})
    await lsquare.aflush(timeout_ms=5_000)
    await asyncio.wrap_future(internal.delivered())
    errors.clear()
    internal.track("call-1", call, internal.origin())
    ready.set()
    await state["waiting"]
    if where == "prepare":
        refusing.set()
    lsquare.capture({"conversationId": "a", "turnId": 1})
    await state["ended"]


def shutting():
    ready.wait(30)
    # The main thread has returned; this non-daemon server still serves.
    threading.main_thread().join()
    result = lsquare.shutdown(timeout_ms=10_000)
    print(json.dumps({"main_alive": state["main_alive"], "called": called, "reads": reads, "errors": errors,
                      "sent": [event["conversationId"] for event in transport.events], "failed": result.failed}), flush=True)
    state["loop"].call_soon_threadsafe(state["ended"].set_result, None)


threading.Thread(target=lambda: asyncio.run(serve_main()), name="server").start()
threading.Thread(target=shutting, name="shutting").start()
ready.wait(30)
"""


@pytest.mark.parametrize("where", ["prepare", "finalize"])
def test_a_sync_shutdown_on_another_thread_once_the_main_thread_returned_never_has_a_hook_handled_on_the_server_s_loop(where: str) -> None:
    # Before: the hook was copied and called there ("prepare"), and what it returned read on the
    # server's thread (both), the event sent.
    tests = str(Path(__file__).resolve().parent)
    done = subprocess.run([sys.executable, "-c", EXIT_PHASE, tests, where], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    [line] = done.stdout.splitlines()
    out = json.loads(line)
    message = {
        "prepare": "No thread could start for before_send; the event was dropped.",
        "finalize": "No thread could start to read what before_send returned; the event was dropped.",
    }[where]
    # As exiting() sees it: the main thread had finished when shutdown() waited.
    assert out["main_alive"] is False
    assert out["reads"] == []
    assert out["called"] == ([] if where == "prepare" else ["server"])
    assert out["errors"] == [["hook_failed", message]]
    assert out["sent"] == []


# Finding 3: a refusal's hand-off made the run "processing", which only a worker reading it should
# be, before it was published: interrupted in between (a KeyboardInterrupt on the main thread's
# loop, caught by the app), that run was never let go of: its deadline's ceiling kept it as a
# zombie, a closed loop doesn't reclaim one being read, and with one slot the next hook never ran.
# It stays "queued" until published now, which every terminal path lets go of. The interruption
# lands where the interpreter runs a signal handler, a Python function's start: at each call into
# the SDK's own code the hand-off makes, in turn, each round a fresh client and loop.

INTERRUPTED_REFUSAL = """
import asyncio, sys, threading
sys.path.insert(0, sys.argv[1])
from helpers import FakeTransport
from lsquare import LSquare, _client, _threads

OURS = (_client.__file__, _threads.__file__)
state = {"target": 0, "seen": 0, "fired": False, "at": "", "inside": None}


class Clock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now


def land(frame):
    # The target-th call this round: interrupted at its start, as by Ctrl-C.
    if not state["fired"]:
        if state["seen"] == state["target"]:
            state["fired"] = True
            state["at"] = frame.f_code.co_name
            raise KeyboardInterrupt
        state["seen"] += 1


def local(frame, event, arg):
    if event == "return" and frame is state["inside"]:
        state["inside"] = None
    return local


def tracer(frame, event, arg):
    if event != "call":
        return None
    code = frame.f_code
    if state["inside"] is None:
        if code.co_name == "unread" and code.co_filename == _client.__file__:
            state["inside"] = frame
            land(frame)
            return local
        return None
    if code.co_filename in OURS:
        land(frame)
    return None


def one_round():
    clock = Clock()
    transport = FakeTransport()
    errors, hooked = [], []
    refusing = threading.Event()

    async def before_send(event):
        hooked.append(event["conversationId"])
        if event["conversationId"] == "conv-1":
            # As it returns, no hook thread can take what it returned.
            refusing.set()
        return dict(event)

    lsquare = LSquare(api_key="lsq_test", endpoint="http://localhost:1", _transport=transport, before_send=before_send,
                      before_send_timeout_ms=1_000, max_concurrent_hooks=1, _clock=clock,
                      on_error=lambda error, event=None: errors.append(error.message))
    real_submit = lsquare._hooks.submit
    lsquare._hooks.submit = lambda job: False if refusing.is_set() else real_submit(job)
    loop = asyncio.new_event_loop()
    caught = False

    async def first():
        lsquare.capture({"conversationId": "conv-1", "turnId": 1})
        # Uninterrupted (the last round), it ends once the event has failed.
        await lsquare.aflush(timeout_ms=5_000)

    state["inside"] = None
    sys.settrace(tracer)
    try:
        loop.run_until_complete(first())
    except KeyboardInterrupt:
        caught = True
    finally:
        sys.settrace(None)
        # The app closes its interrupted loop, as asyncio.run() would: its tasks cancelled first.
        tasks = asyncio.all_tasks(loop)
        for task in tasks:
            task.cancel()
        if tasks:
            loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        loop.close()
    fired = state["fired"]
    assert caught == fired, (caught, fired)
    refusing.clear()
    # Past the finalization ceiling: the event fails, its slot back, whether or not its hand-off
    # was published.
    clock.now = 1_002.5
    lsquare._scheduler.wake()

    async def second():
        lsquare.capture({"conversationId": "conv-2", "turnId": 2})
        return await lsquare.aflush(timeout_ms=5_000)

    flushed = asyncio.run(second())
    lsquare._lsquare_internal().delivered().result(5)
    with lsquare._lock:
        books = (lsquare._active_hooks, lsquare._zombies, lsquare._zombie_bytes)
    lsquare.shutdown(timeout_ms=0)
    at = state["at"]
    assert hooked == ["conv-1", "conv-2"], (at, hooked, books)
    assert (flushed.sent, flushed.pending) == (1, 0), (at, flushed, books)
    assert books == (0, 0, 0), (at, books)
    assert [event["conversationId"] for event in transport.events] == ["conv-2"]
    assert len(errors) == 1 and errors[0] in (
        "No thread could start to read what before_send returned; the event was dropped.",
        "before_send returned in time, but what it returned wasn't processed within 2000 ms; the event was dropped.",
    ), (at, errors)
    return fired


swept = []
while one_round():
    swept.append(state["at"])
    state.update(target=state["target"] + 1, seen=0, fired=False, at="")
print("swept", len(swept), *swept, sep="|", flush=True)
"""


def test_a_keyboard_interrupt_anywhere_in_a_refused_hook_s_hand_off_never_keeps_its_slot() -> None:
    # Before: interrupted as the hand-off called publish(), its run "processing", the next hook
    # never ran and its flush found it pending.
    tests = str(Path(__file__).resolve().parent)
    done = subprocess.run([sys.executable, "-c", INTERRUPTED_REFUSAL, tests], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr[-6_000:]
    fields = done.stdout.strip().split("|")
    assert fields[0] == "swept", done.stdout
    # The hand-off's start, its publication's, and the scheduler's taking of it, at least.
    assert int(fields[1]) >= 3 and {"unread", "publish", "call_soon"} <= set(fields[2:]), done.stdout
