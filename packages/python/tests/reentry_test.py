"""A signal handler or a finalizer calling back into the SDK in the middle of its own code, and the
SystemExit or KeyboardInterrupt a handler raises there.

A signal handler is a nested call on the main thread between two bytecodes, a finalizer one on any
thread. So these tests make that call from inside the SDK's own step: from a patched method, or at
one line found by a trace function (`Landing`), each line in turn. Scenarios that could deadlock
run on a daemon thread joined with a bound, so a deadlock fails the test instead of hanging the
suite. The tests count what happened; none measures how long anything took. Those about SystemExit
and KeyboardInterrupt run on the main thread, as pytest runs them: where a signal handler raises."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import gc
import json
import linecache
import socket
import sys
import threading
import time
import types
from collections.abc import Callable, Iterator
from typing import Any, cast

import pytest

from lsquare import LSquare
from lsquare import _client, _sending, _threads
from lsquare._client import Signal, _Wait, swallow
from lsquare._sending import Abort, Exchange, NetworkError, Reply, Resolver
from lsquare._threads import Pool, PrivateLoop, Scheduler, schedule_on, start_thread
from lsquare.mcp import _call, _lists

from helpers import FakeTransport, Ingestion, Recorder, ack, eventually

KEY = "lsq_test"
TURN = {"conversationId": "c", "question": "q", "answer": "a"}
JOIN_S = 5.0
#: What a patched getaddrinfo answers at once (a TEST-NET-1 address), so no lookup runs.
ADDRESS = (socket.AF_INET, socket.SOCK_STREAM, 6, ("192.0.2.1", 443))


def on_own_thread(fn: Callable[[], Any], client: LSquare | None = None, timeout: float = JOIN_S) -> Any:
    """Runs `fn` on a daemon thread named "scenario", joined for `timeout` seconds; returns what it
    returned, raises what it raised. A thread still running fails the test: a deadlock."""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["out"] = fn()
        except BaseException as error:  # noqa: BLE001 - raised again on the test's thread
            box["error"] = error

    thread = threading.Thread(target=run, name="scenario", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive() and client is not None:
        # Wedged: the exit flush returns at once for a closed client, whatever locks are held.
        client._closed = True
    assert not thread.is_alive(), "deadlocked, or waited out a timeout it can't use"
    if "error" in box:
        raise box["error"]
    return box.get("out")


def reenter_once(owner: Any, name: str, nested: Callable[[], None]) -> list[int]:
    """The first call of `owner.name` on the scenario thread runs `nested()` first, as a signal
    handler or finalizer would."""
    real = getattr(owner, name)
    fired: list[int] = []

    def patched(*args: Any, **kwargs: Any) -> Any:
        if not fired and threading.current_thread().name == "scenario":
            fired.append(1)
            nested()
        return real(*args, **kwargs)

    setattr(owner, name, patched)
    return fired


class Landing:
    """Runs `nested()` once, on the thread it traces, at the `at`-th line run inside a call of
    `code` and of whatever that calls (only the lines `counts` takes, when given): where a signal
    handler can run. A sweep runs one landing for each line in turn, until one no longer fires.

    `raises`: `nested` raises, so the line event a `with` statement's line gets again on its way
    out is skipped. It comes before the `__exit__` call, outside the block, and raising there leaves
    the lock held, where a signal handler, run only once that call has returned, can't."""

    def __init__(
        self,
        code: types.CodeType,
        at: int,
        nested: Callable[[], Any],
        counts: Callable[[types.FrameType], bool] | None = None,
        raises: bool = False,
    ) -> None:
        self.code = code
        self.at = at
        self.nested = nested
        self.counts = counts
        self.raises = raises
        self.lines = 0
        self.fired = False
        self._inside = 0
        #: The lines each frame has run, to tell a `with` line coming back.
        self._seen: dict[types.FrameType, set[int]] = {}

    def run(self, fn: Callable[[], Any]) -> Any:
        sys.settrace(self._call)
        try:
            return fn()
        finally:
            sys.settrace(None)

    def _call(self, frame: types.FrameType, event: str, arg: Any) -> Any:
        if event != "call":
            return None
        if frame.f_code is self.code:
            self._inside += 1
            return self._line
        return self._line if self._inside > 0 else None

    def _line(self, frame: types.FrameType, event: str, arg: Any) -> Any:
        if event == "line" and (self.counts is None or self.counts(frame)) and self._lands(frame):
            self.lines += 1
            if self.lines == self.at and not self.fired:
                self.fired = True
                self.nested()
        elif event == "return" and frame.f_code is self.code:
            self._inside -= 1
        return self._line

    def _lands(self, frame: types.FrameType) -> bool:
        if not self.raises:
            return True
        seen = self._seen.setdefault(frame, set())
        again = frame.f_lineno in seen
        seen.add(frame.f_lineno)
        return not (again and linecache.getline(frame.f_code.co_filename, frame.f_lineno).lstrip().startswith(("with ", "async with ")))


def every_line() -> Iterator[int]:
    """1, 2, 3, and so on: a sweep stops once its landing no longer fires."""
    at = 0
    while True:
        at += 1
        yield at


def nested_code(function: Callable[..., Any], name: str) -> types.CodeType:
    """The code of the function `name` defined inside `function`."""
    for constant in function.__code__.co_consts:
        if isinstance(constant, types.CodeType) and constant.co_name == name:
            return constant
    raise LookupError(name)


def raising(error: type[BaseException], *args: Any) -> Callable[..., Any]:
    def raise_it(*_: Any, **__: Any) -> Any:
        raise error(*args)

    return raise_it


def stalled() -> tuple[FakeTransport, threading.Event, threading.Event]:
    """A transport whose requests hang until released; `started` is set once one is in flight."""
    release = threading.Event()
    started = threading.Event()

    def respond(events: list[dict[str, Any]]) -> Reply:
        started.set()
        release.wait(30)
        return Reply(200, None, ack(*[event["eventId"] for event in events]))

    return FakeTransport(respond), started, release


def make(transport: FakeTransport, recorder: Recorder | None = None) -> LSquare:
    recorder = recorder or Recorder()
    return LSquare(api_key=KEY, endpoint="https://ingest.example", _transport=transport, logger=recorder, on_error=recorder.on_error)


def interrupted_start(monkeypatch: pytest.MonkeyPatch, name: str, after: bool) -> Callable[[Callable[[], None], str], None]:
    """Makes the first start of a thread called `name` raise SystemExit, as a signal handler's
    would, inside the start: before the thread begins, or after. Returns that start_thread."""
    real = start_thread
    fired: list[int] = []

    def start(target: Callable[[], None], thread_name: str) -> None:
        if fired or thread_name != name:
            real(target, thread_name)
            return
        fired.append(1)
        if after:
            real(target, thread_name)
        raise SystemExit(3)

    monkeypatch.setattr(_threads, "start_thread", start)
    return start


@pytest.fixture
def ingestion() -> Iterator[Ingestion]:
    server = Ingestion()
    yield server
    server.close()


@pytest.fixture
def no_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(ADDRESS[0], ADDRESS[1], ADDRESS[2], "", ADDRESS[3])])


# -- SystemExit and KeyboardInterrupt are passed on, on the main thread --------------------------------
#
# One test for each place the SDK catches BaseException on a path the main thread can take: after
# its own cleanup, it lets those two through there.


def test_a_listener_s_keyboard_interrupt_is_passed_on_on_the_main_thread_and_swallowed_elsewhere() -> None:
    signal = Signal()
    signal._abort()
    with pytest.raises(KeyboardInterrupt):
        signal.add_listener(raising(KeyboardInterrupt))
    # On another thread it's the listener's failure, as any other.
    on_own_thread(lambda: signal.add_listener(raising(KeyboardInterrupt)))


def test_a_system_exit_creating_a_hook_s_task_on_the_main_thread_s_loop_is_passed_on() -> None:
    settled: list[Any] = []

    async def main() -> None:
        schedule_on(asyncio.get_running_loop(), raising(SystemExit, 3), contextvars.copy_context(), lambda error, out: settled.append(error))
        for _ in range(10):
            await asyncio.sleep(0)

    with pytest.raises(SystemExit):
        asyncio.run(main())
    # Reported as the hook's failure first.
    assert [type(error) for error in settled] == [SystemExit]


def test_a_system_exit_settling_a_hook_on_the_main_thread_s_loop_is_passed_on_and_settles_it_once() -> None:
    settled: list[tuple[Any, Any]] = []

    def done(error: BaseException | None, out: Any) -> None:
        settled.append((error, out))
        if len(settled) == 1:
            raise SystemExit(3)  # a signal handler's, landing as the hook settles

    async def hook() -> str:
        return "out"

    async def main() -> None:
        schedule_on(asyncio.get_running_loop(), hook, contextvars.copy_context(), done)
        for _ in range(10):
            await asyncio.sleep(0)

    with pytest.raises(SystemExit):
        asyncio.run(main())
    assert settled == [(None, "out")]


def test_a_system_exit_in_a_step_run_inline_on_the_main_thread_is_passed_on(monkeypatch: pytest.MonkeyPatch) -> None:
    # Interpreter exit: no thread starts, so the scheduler's steps run on the calling thread.
    monkeypatch.setattr(_threads, "start_thread", raising(RuntimeError, "can't create new thread at interpreter shutdown"))
    scheduler = Scheduler()
    with pytest.raises(SystemExit):
        scheduler.call_soon(raising(SystemExit, 3))
    ran: list[int] = []
    scheduler.call_soon(lambda: ran.append(1))
    assert ran == [1]


def list_read(page: Callable[[str | None], Any]) -> tuple[_lists.Lists, _lists.Reading, _lists.Reader]:
    lists = _lists.Lists()
    reader = _lists.Reader(page=page, context=contextvars.copy_context())
    return lists, _lists.Reading(generation=0, started=time.monotonic(), reader=reader), reader


async def exits(cursor: str | None) -> Any:
    raise SystemExit(3)


def test_a_system_exit_in_a_detached_list_read_on_the_main_thread_s_loop_is_passed_on_once_the_read_is_over() -> None:
    lists, reading, reader = list_read(exits)

    async def main() -> None:
        asyncio.ensure_future(_lists._run(lists, reading, reader))
        for _ in range(10):
            await asyncio.sleep(0)

    with pytest.raises(SystemExit):
        asyncio.run(main())
    assert reading.over and reading.done.done()


def test_a_system_exit_in_a_list_read_on_trio_s_main_thread_ends_the_run() -> None:
    trio: Any = pytest.importorskip("trio")
    lists, reading, reader = list_read(exits)

    async def main() -> None:
        trio.lowlevel.spawn_system_task(_lists._run_trio, lists, reading, reader, trio.CancelScope())
        for _ in range(10):
            await trio.lowlevel.checkpoint()

    def inside(error: BaseException | None) -> Iterator[BaseException]:
        if error is not None:
            yield error
            for each in getattr(error, "exceptions", ()):
                yield from inside(each)
            yield from inside(error.__cause__)

    # A system task that raises ends the run, as trio's internal error around what it raised.
    with pytest.raises(trio.TrioInternalError) as raised:
        trio.run(main)
    assert any(isinstance(error, SystemExit) for error in inside(raised.value))
    assert reading.over and reading.done.done()


def test_a_keyboard_interrupt_reading_identify_s_outcome_is_passed_on_on_the_main_thread() -> None:
    class Interrupted(concurrent.futures.Future[Any]):
        def exception(self, timeout: float | None = None) -> BaseException | None:
            raise KeyboardInterrupt

    internal = cast(Any, None)
    with pytest.raises(KeyboardInterrupt):
        _call.check_identity(internal, Interrupted())
    # Elsewhere, read as an identify that threw.
    assert on_own_thread(lambda: _call.check_identity(internal, Interrupted())) == (False, {}, None, "threw")


def test_a_system_exit_retrieving_a_callback_s_failure_on_the_main_thread_s_loop_is_passed_on() -> None:
    class Interrupting(asyncio.Future[Any]):
        def exception(self) -> BaseException | None:
            raise SystemExit(3)

    async def main() -> None:
        future: asyncio.Future[Any] = Interrupting()
        swallow(future)
        future.set_result(None)
        for _ in range(10):
            await asyncio.sleep(0)

    with pytest.raises(SystemExit):
        asyncio.run(main())


def test_on_pyodide_a_logger_s_keyboard_interrupt_on_the_main_thread_is_passed_on(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pyodide has no threads: the client writes its warning on the constructing thread.
    monkeypatch.setattr(_client, "is_pyodide", lambda: True)

    class Interrupting:
        def info(self, message: str) -> None:
            pass

        def warn(self, message: str) -> None:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        LSquare(api_key=KEY, logger=Interrupting())
    # The warning ran one level deeper in a context of its own: this thread's depth is as it was.
    assert _client.current_depth() == 0


def test_on_pyodide_the_warning_runs_one_level_deeper_and_leaves_the_constructing_thread_s_depth_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_client, "is_pyodide", lambda: True)
    depths: list[int] = []

    class Recording:
        def info(self, message: str) -> None:
            pass

        def warn(self, message: str) -> None:
            depths.append(_client.current_depth())

    def construct() -> int:
        LSquare(api_key=KEY, logger=Recording())
        return _client.current_depth()

    # In a copy, so a depth left behind can't reach the tests that follow.
    assert contextvars.copy_context().run(construct) == 0
    assert depths == [1]


def test_a_keyboard_interrupt_connecting_in_turn_on_the_main_thread_is_passed_on_not_taken_for_a_failed_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # How green sockets connect; at exit, inline, that runs on the main thread.
    made: list[int] = []

    def interrupted(*args: Any) -> Any:
        made.append(1)
        raise KeyboardInterrupt

    monkeypatch.setattr(_sending, "_DeadlineSocket", interrupted)
    exchange = Exchange("http://ingest.test/v1/turns", KEY, "ua", Resolver(start_thread), lambda delay, fn: (lambda: None))
    addresses: list[_sending.Address] = [ADDRESS, (socket.AF_INET, socket.SOCK_STREAM, 6, ("192.0.2.2", 443))]
    with pytest.raises(KeyboardInterrupt):
        exchange._connect_in_turn(addresses, lambda: 5.0, time.monotonic() + 5)
    assert made == [1]


# -- Thread starts interrupted --------------------------------------------------------------------------
#
# A signal handler's SystemExit raised while a thread starts (Thread.start waits for the new thread, and
# the handler runs inside that wait): before the thread begins, and right after.


def test_a_scheduler_start_interrupted_leaves_no_thread_marked_running(monkeypatch: pytest.MonkeyPatch) -> None:
    for after in (False, True):
        interrupted_start(monkeypatch, "lsquare-scheduler", after)
        scheduler = Scheduler()
        first = threading.Event()
        second = threading.Event()
        try:
            with pytest.raises(SystemExit):
                scheduler.call_soon(first.set)
            # The next step finds a thread running, or starts one: both run.
            scheduler.call_soon(second.set)
            assert first.wait(5) and second.wait(5), after
        finally:
            scheduler.stop()


def test_a_worker_start_interrupted_leaves_the_worker_count_as_it_was(monkeypatch: pytest.MonkeyPatch) -> None:
    for after in (False, True):
        interrupted_start(monkeypatch, "lsquare-test", after)
        pool = Pool("test", 1)
        first = threading.Event()
        second = threading.Event()
        try:
            with pytest.raises(SystemExit):
                pool.submit(first.set)
            # The job stayed queued: the worker left, or the next to start, takes both.
            pool.submit(second.set)
            assert first.wait(5) and second.wait(5), after
            assert pool.drain(5)
        finally:
            pool.stop()


def test_jobs_an_interrupted_start_left_waiting_get_a_worker_or_are_handed_back(monkeypatch: pytest.MonkeyPatch) -> None:
    interrupted_start(monkeypatch, "lsquare-test", after=False)
    pool = Pool("test", 1)
    ran = threading.Event()
    try:
        with pytest.raises(SystemExit):
            pool.submit(ran.set)
        assert pool.resume() == [] and ran.wait(5)
    finally:
        pool.stop()
    # With no thread able to start (interpreter exit), they're handed back.
    interrupted_start(monkeypatch, "lsquare-test", after=False)
    pool = Pool("test", 1)
    with pytest.raises(SystemExit):
        pool.submit(ran.set)
    monkeypatch.setattr(_threads, "start_thread", raising(RuntimeError, "can't create new thread at interpreter shutdown"))
    assert pool.resume() == [ran.set] and pool.resume() == []


def test_a_resolver_start_interrupted_leaves_no_thread_marked_running(monkeypatch: pytest.MonkeyPatch, no_lookup: None) -> None:
    for after in (False, True):
        resolver = Resolver(interrupted_start(monkeypatch, "lsquare-resolver", after))
        try:
            with pytest.raises(SystemExit):
                resolver.resolve("ingest.test", 443, 5)
            assert resolver.resolve("ingest.test", 443, 5) == [ADDRESS], after
        finally:
            resolver.stop()


def test_a_call_back_in_anywhere_in_starting_the_scheduler_starts_one_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    # A signal handler's call between the check that no thread runs and the claim, among others.
    starts: list[str] = []
    here = threading.current_thread()

    def counting(target: Callable[[], None], name: str) -> None:
        # Every thread in the process starts through this, and a client an earlier test left can
        # start one at any moment: only this thread's count.
        if threading.current_thread() is here:
            starts.append(name)
        start_thread(target, name)

    monkeypatch.setattr(_threads, "start_thread", counting)
    for at in every_line():
        scheduler = Scheduler()
        starts.clear()
        landing = Landing(Scheduler._ensure_running.__code__, at, lambda: scheduler.call_soon(lambda: None))
        try:
            landing.run(lambda: scheduler.call_soon(lambda: None))
        finally:
            scheduler.stop()
        assert starts == ["lsquare-scheduler"], at
        if not landing.fired:
            break


# -- The private loop ------------------------------------------------------------------------------------


def test_a_stop_from_inside_get_reaches_the_loop_get_publishes(monkeypatch: pytest.MonkeyPatch) -> None:
    holder = PrivateLoop()

    def start(target: Callable[[], None], name: str) -> None:
        holder.stop()  # shutdown() from a signal handler, while get() starts the loop
        start_thread(target, name)

    monkeypatch.setattr(_threads, "start_thread", start)
    loop = on_own_thread(holder.get)
    # The stop reached it: its thread runs it, stops and closes it.
    assert eventually(loop.is_closed)


def test_get_hands_out_the_loop_without_waiting_for_its_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    # The loop's thread is held up: get() doesn't wait for it, as it would under the client's lock.
    go = threading.Event()

    def held(target: Callable[[], None], name: str) -> None:
        def later() -> None:
            go.wait(10)
            target()

        start_thread(later, name)

    monkeypatch.setattr(_threads, "start_thread", held)
    holder = PrivateLoop()
    try:
        loop = on_own_thread(holder.get, timeout=3.0)
        future = asyncio.run_coroutine_threadsafe(asyncio.sleep(0, "ran"), loop)
        go.set()
        assert future.result(5) == "ran"
    finally:
        go.set()
        holder.stop()


def test_a_loop_start_interrupted_after_its_thread_began_withdraws_that_loop_and_its_thread_closes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made: list[asyncio.AbstractEventLoop] = []
    real = asyncio.new_event_loop

    def recorded() -> asyncio.AbstractEventLoop:
        made.append(real())
        return made[-1]

    monkeypatch.setattr(asyncio, "new_event_loop", recorded)
    interrupted_start(monkeypatch, "lsquare-loop", after=True)
    holder = PrivateLoop()
    try:
        with pytest.raises(SystemExit):
            holder.get()
        assert eventually(made[0].is_closed)
        # The next one is another loop, which runs.
        loop = holder.get()
        assert loop is not made[0]
        assert asyncio.run_coroutine_threadsafe(asyncio.sleep(0, "ran"), loop).result(5) == "ran"
    finally:
        holder.stop()


def test_a_loop_start_that_fails_fails_what_every_caller_handed_that_loop_and_each_gives_back_what_it_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One identify starts the private loop. While that start is under way, other threads hand the
    # loop work: another identify, an event's before_send, an async on_error. Then the start fails,
    # as when the OS refuses a thread. Each of them fails, rather than waiting on a loop nothing will
    # run, and gives back its place.
    async def hook(given: Any) -> str:
        return "ran"

    async def before_send(event: Any, signal: Any) -> Any:
        return event

    codes: list[str] = []

    async def on_error(error: Any, event: Any = None) -> None:
        codes.append(error.code)

    transport = FakeTransport()
    client = LSquare(api_key=KEY, endpoint="https://ingest.example", _transport=transport, logger=Recorder(),
                     on_error=on_error, before_send=before_send, before_send_timeout_ms=60_000)
    internal = client._lsquare_internal()
    others: list[Any] = []

    def start(target: Callable[[], None], name: str) -> None:
        if name != "lsquare-loop" or others:
            start_thread(target, name)
            return
        others.append(on_own_thread(lambda: internal.identify(hook, lambda signal: None, internal.origin())))
        client.capture(dict(TURN))
        assert eventually(lambda: any(entry.hook_cancel is not None for entry in list(client._live.values())))
        # An invalid_input report, for the async on_error.
        client.capture(["not", "a", "mapping"])  # type: ignore[arg-type]
        assert eventually(lambda: len(client._callback_futures) == 1)
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(_threads, "start_thread", start)
    try:
        first = internal.identify(hook, lambda signal: None, internal.origin())
        assert first is not None and others[0] is not None
        assert isinstance(first.future.exception(timeout=5), RuntimeError)
        assert isinstance(others[0].future.exception(timeout=5), RuntimeError)
        assert (client._identifying["active"], len(client._identify_runs)) == (0, 0)
        # The event fails, unsent, and its report reaches on_error on a loop started since.
        assert eventually(lambda: "hook_failed" in codes)
        assert (transport.events, len(client._live), client._active_hooks, client._zombies) == ([], 0, 0, 0)
        assert eventually(lambda: (len(client._callback_futures), client._report_limit.held) == (0, 0))
        # The next start makes another loop, which runs.
        later = internal.identify(hook, lambda signal: None, internal.origin())
        assert later is not None and later.future.result(timeout=5) == "ran"
    finally:
        client.shutdown(timeout_ms=0)


def test_a_loop_start_interrupted_before_its_thread_began_fails_what_was_handed_to_it_and_passes_the_interruption_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holder = PrivateLoop()
    failed: list[BaseException] = []
    fired: list[int] = []

    def start(target: Callable[[], None], name: str) -> None:
        if fired:
            start_thread(target, name)
            return
        fired.append(1)
        # Another thread hands the loop work meanwhile.
        on_own_thread(lambda: holder.hand(lambda loop: loop.call_soon_threadsafe(lambda: None), failed.append))
        raise SystemExit(3)

    monkeypatch.setattr(_threads, "start_thread", start)
    try:
        with pytest.raises(SystemExit):
            holder.hand(lambda loop: loop.call_soon_threadsafe(lambda: None), failed.append)
        # Both fail with an error of their own: the interruption is the starting thread's alone.
        assert [type(error) for error in failed] == [RuntimeError, RuntimeError]
        loop = holder.get()
        assert asyncio.run_coroutine_threadsafe(asyncio.sleep(0, "ran"), loop).result(5) == "ran"
    finally:
        holder.stop()


# -- Locks a call back in takes again -----------------------------------------------------------------------
#
# Each sweep makes the nested call at every line of the step in turn, on the scenario thread. A wait
# the nested call would end gets a short timeout, so a landing after it still comes.


def test_shutdown_from_inside_a_flush_s_wait_ends_it_wherever_it_lands() -> None:
    # threading.Event holds its own lock on its way into and out of wait(): ending the wait there,
    # from the same thread, never returned.
    code = type(_Wait(deadline=0.0, pending=set()).ended).wait.__code__
    for at in every_line():
        transport, started, release = stalled()
        client = make(transport)
        landing = Landing(code, at, lambda: client.shutdown(timeout_ms=0))
        try:
            client.capture(dict(TURN))
            assert started.wait(5)
            result = on_own_thread(lambda: landing.run(lambda: client.flush(timeout_ms=500)), client=client)
            assert result.failed + result.pending == 1, at
        finally:
            release.set()
        client.shutdown(timeout_ms=0)
        if not landing.fired:
            break


def test_an_abort_from_inside_a_hook_s_wait_ends_it_wherever_it_lands() -> None:
    code = type(Signal()._event).wait.__code__
    for at in every_line():
        signal = Signal()
        landing = Landing(code, at, signal._abort)
        on_own_thread(lambda: landing.run(lambda: signal.wait(0.5)))
        assert signal.aborted or not landing.fired, at
        if not landing.fired:
            break


def test_an_abort_from_inside_add_listener_runs_the_listener_once_wherever_it_lands() -> None:
    for at in every_line():
        signal = Signal()
        ran: list[int] = []

        def abort() -> None:
            # shutdown() aborting the hook, then running the listeners it took, as the client does.
            for listener in signal._abort() or []:
                listener()

        landing = Landing(Signal.add_listener.__code__, at, abort)
        on_own_thread(lambda: landing.run(lambda: signal.add_listener(lambda: ran.append(1))))
        assert ran == ([1] if landing.fired else []), at
        if not landing.fired:
            break


def test_an_abort_from_inside_attach_shuts_the_socket_wherever_it_lands() -> None:
    for at in every_line():
        abort = Abort()
        left, right = socket.socketpair()
        right.settimeout(5)
        try:
            landing = Landing(Abort._attach.__code__, at, abort.abort)
            on_own_thread(lambda: landing.run(lambda: abort._attach(left)))
            if landing.fired:
                # Shut down: its peer reads the end of the stream.
                assert right.recv(1) == b"", at
        finally:
            left.close()
            right.close()
        if not landing.fired:
            break


def test_a_stop_from_inside_resolve_wherever_it_lands_returns(no_lookup: None) -> None:
    for at in every_line():
        resolver = Resolver(start_thread)
        landing = Landing(Resolver.resolve.__code__, at, resolver.stop)
        try:
            on_own_thread(lambda: landing.run(lambda: resolver.resolve("ingest.test", 443, 0.5)))
        except NetworkError:
            # Stopped before its lookup was taken: failed in its time, as the deadline says.
            pass
        if not landing.fired:
            break


def test_a_cancel_from_inside_a_hook_task_s_creation_wherever_it_lands_settles_it_once() -> None:
    code = nested_code(schedule_on, "create")
    for at in every_line():
        cancels: list[Callable[[], None]] = []
        settled: list[Any] = []

        async def hook() -> str:
            return "out"

        async def main() -> None:
            cancels.append(schedule_on(asyncio.get_running_loop(), hook, contextvars.copy_context(), lambda error, out: settled.append(out)))
            for _ in range(10):
                await asyncio.sleep(0)

        landing = Landing(code, at, lambda: cancels[0]())
        on_own_thread(lambda: landing.run(lambda: asyncio.run(main())))
        assert len(settled) == 1, at
        if not landing.fired:
            break


def test_shutdown_from_inside_identify_s_start_wherever_it_lands_returns() -> None:
    # shutdown() aborts the hook, which takes the guard its start holds.
    code = nested_code(_client.Internal.identify, "run_async")

    async def identify(given: Any) -> None:
        return None

    for at in every_line():
        client = make(FakeTransport())
        internal = client._lsquare_internal()
        landing = Landing(code, at, lambda: client.shutdown(timeout_ms=0))
        run = on_own_thread(lambda: landing.run(lambda: internal.identify(identify, lambda signal: None, internal.origin())), client=client)
        assert run is not None and client._closed == landing.fired, at
        client.shutdown(timeout_ms=0)
        # A loop got after the shutdown (one landing inside new_event_loop(), say) stops too.
        client._private_loop.stop()
        if not landing.fired:
            break


# -- A finalizer on another thread ----------------------------------------------------------------------


def test_a_hook_task_destroyed_with_its_loop_is_collected_without_taking_the_client_s_lock() -> None:
    # The app closed its loop with the hook's task still pending (run_until_complete, then close).
    # Once its event has expired, the collector finalizes that task on whatever thread collects:
    # here one that another thread, holding the client's lock, waits on, as a capture holding it
    # waits for the scheduler's lock that a collection can run inside. If finalizing it took the
    # client's lock, neither would ever go on.
    started = threading.Event()
    closed = threading.Event()

    async def before_send(event: Any, signal: Any) -> Any:
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            closed.set()
        return event

    client = LSquare(api_key=KEY, endpoint="https://ingest.example", _transport=FakeTransport(), logger=Recorder(),
                     before_send=before_send, before_send_timeout_ms=50)
    # Nothing is collected until this test collects it, on the thread it chooses.
    gc.disable()
    try:
        loop = asyncio.new_event_loop()

        async def main() -> None:
            client.capture(dict(TURN))
            for _ in range(500):
                if started.is_set():
                    return
                await asyncio.sleep(0.01)

        loop.run_until_complete(main())
        loop.close()
        assert started.is_set()
        # Past its deadline it has expired: its task is unreachable, and still not finalized.
        assert eventually(lambda: not client._live)
        assert not closed.is_set()
        holding = threading.Event()
        collected = threading.Event()
        waited: list[bool] = []

        def hold() -> None:
            with client._lock:
                holding.set()
                waited.append(collected.wait(JOIN_S))

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        assert holding.wait(JOIN_S)

        def collect() -> None:
            gc.collect()
            collected.set()

        on_own_thread(collect, timeout=2 * JOIN_S)
        holder.join(JOIN_S)
        assert waited == [True] and closed.is_set()
    finally:
        gc.enable()
        client.shutdown(timeout_ms=0)


# -- shutdown() and flush() called back in --------------------------------------------------------------


def test_shutdown_called_back_inside_a_report_hand_off_while_another_thread_captures_returns() -> None:
    # The hand-off held a lock of its own that another thread's report, under the client's lock,
    # then waited for, while the shutdown waited for the client's lock.
    transport, started, release = stalled()
    client = make(transport)
    try:
        client.capture(dict(TURN))
        assert started.wait(5)

        def another_request_thread() -> None:
            # A busy server: its next capture reports "called after shutdown()" under the lock.
            if eventually(lambda: client._closed):
                client.capture(dict(TURN))

        results: list[Any] = []

        def nested() -> None:
            threading.Thread(target=another_request_thread, daemon=True).start()
            results.append(client.shutdown(timeout_ms=300))

        fired = reenter_once(client._callbacks, "submit", nested)
        on_own_thread(lambda: client.capture(cast(Any, "not a mapping")), client=client)
        assert fired and results[0].failed == 1
    finally:
        release.set()


def test_shutdown_called_back_inside_a_client_step_drops_what_is_left_at_once() -> None:
    # Nothing it would wait for can happen until the step it interrupted resumes.
    transport, started, release = stalled()
    client = make(transport)
    try:
        client.capture(dict(TURN))
        assert started.wait(5)
        results: list[Any] = []
        fired = reenter_once(client._scheduler, "call_soon", lambda: results.append(client.shutdown(timeout_ms=60_000)))
        on_own_thread(lambda: client.capture(dict(TURN)), client=client)
        assert fired and client._closed and results[0].failed == 2
    finally:
        release.set()


def test_flush_called_back_inside_a_client_step_counts_what_is_left_as_pending_at_once() -> None:
    transport, started, release = stalled()
    client = make(transport)
    try:
        client.capture(dict(TURN))
        assert started.wait(5)
        results: list[Any] = []
        fired = reenter_once(client._scheduler, "call_soon", lambda: results.append(client.flush(timeout_ms=60_000)))
        on_own_thread(lambda: client.capture(dict(TURN)), client=client)
        assert fired and results[0].pending == 2 and not client._closed
    finally:
        release.set()
    client.shutdown(timeout_ms=0)


def test_a_request_built_as_a_shutdown_lands_is_never_posted(ingestion: Ingestion, monkeypatch: pytest.MonkeyPatch) -> None:
    # The shutdown lands (a finalizer's, on the scheduler's thread, inside the step) before the
    # batch's events are marked as sending, so it can't abort the request: its generation stops it.
    client = LSquare(api_key=KEY, endpoint=ingestion.url)
    results: list[Any] = []
    real_batch = _client._Batch

    def batch(*args: Any, **kwargs: Any) -> Any:
        if not results:
            results.append(client.shutdown(timeout_ms=0))
        return real_batch(*args, **kwargs)

    monkeypatch.setattr(_client, "_Batch", batch)
    submitted = threading.Event()
    real_submit = client._senders.submit

    def submit(job: Callable[[], None]) -> bool:
        taken = real_submit(job)
        submitted.set()
        return taken

    monkeypatch.setattr(client._senders, "submit", submit)
    client.capture(dict(TURN))
    assert submitted.wait(5)
    # What the sending thread was handed has run.
    assert client._senders.drain(5)
    assert results[0].failed == 1
    assert ingestion.requests == []


# -- Interrupted captures ---------------------------------------------------------------------------------


def test_a_capture_interrupted_anywhere_in_its_hand_off_is_sent_or_never_queued() -> None:
    # A SystemExit at each line of the hand-off, the scheduler's start under it included: the event
    # is sent, or never queued, never left where nothing sends it.
    code = LSquare._enqueue.__code__
    files = {_threads.__file__, threading.__file__}

    def counts(frame: types.FrameType) -> bool:
        return frame.f_code is code or frame.f_code.co_filename in files

    for at in every_line():
        client = make(FakeTransport())
        landing = Landing(code, at, raising(SystemExit, 3), counts, raises=True)
        try:
            landing.run(lambda: client.capture(dict(TURN)))
        except SystemExit:
            assert landing.fired, at
        result = client.flush(timeout_ms=5_000)
        assert (result.pending, result.failed, len(client._live), client._live_bytes) == (0, 0, 0, 0), (at, result)
        client.shutdown(timeout_ms=0)
        if not landing.fired:
            break


def test_the_exit_flush_sends_a_request_whose_sending_thread_s_start_was_interrupted(monkeypatch: pytest.MonkeyPatch) -> None:
    # No scheduler thread can start, as at interpreter exit: the capture's steps run inline, and its
    # request's sending thread starts under the client's lock, where the SystemExit lands.
    fired: list[int] = []

    def start(target: Callable[[], None], name: str) -> None:
        if name == "lsquare-scheduler":
            raise RuntimeError("can't create new thread at interpreter shutdown")
        if name == "lsquare-sender" and not fired:
            fired.append(1)
            raise SystemExit(3)
        start_thread(target, name)

    monkeypatch.setattr(_threads, "start_thread", start)
    transport = FakeTransport()
    client = make(transport)
    with pytest.raises(SystemExit):
        client.capture(dict(TURN))
    client._exit_flush()
    assert len(transport.events) == 1


def test_the_exit_flush_runs_a_step_left_waiting_by_an_interrupted_scheduler_start(monkeypatch: pytest.MonkeyPatch) -> None:
    # The main thread starts the scheduler from a timer the wrapper sets, outside the client's lock.
    # Another thread's capture comes meanwhile: it finds a start under way and leaves its step to
    # it. Then the start is interrupted.
    transport = FakeTransport()
    client = make(transport)
    fired: list[int] = []

    def start(target: Callable[[], None], name: str) -> None:
        if name == "lsquare-scheduler" and not fired:
            fired.append(1)
            other = threading.Thread(target=lambda: client.capture(dict(TURN)))
            other.start()
            other.join(5)
            raise SystemExit(3)
        start_thread(target, name)

    monkeypatch.setattr(_threads, "start_thread", start)
    with pytest.raises(SystemExit):
        client._lsquare_internal().timer(60, lambda: None)
    client._exit_flush()
    assert len(transport.events) == 1


def test_the_exit_flush_spends_one_timeout_on_the_requests_it_resumes_and_its_flush(monkeypatch: pytest.MonkeyPatch) -> None:
    # A request an interrupted start left waiting is sent from the exiting thread, as no thread can
    # start at exit, and takes all the time it's given; a call's event still waits for identify. The
    # flush after that request gets what it left of the timeout, not a timeout of its own. The
    # request's time is added to the client's clock rather than waited out.
    budget_ms = 3_000.0
    taken = [0.0]
    exiting: list[int] = []
    fired: list[int] = []
    posted: list[bytes] = []

    def start(target: Callable[[], None], name: str) -> None:
        if exiting or name == "lsquare-scheduler":
            raise RuntimeError("can't create new thread at interpreter shutdown")
        if name == "lsquare-sender" and not fired:
            fired.append(1)
            raise SystemExit(3)
        start_thread(target, name)

    def slow(body: bytes, timeout: float, abort: Abort) -> Reply:
        taken[0] += timeout
        posted.append(body)
        return Reply(200, None, ack(*[event["eventId"] for event in json.loads(body)["events"]]))

    def clock() -> float:
        return time.monotonic() + taken[0]

    monkeypatch.setattr(_threads, "start_thread", start)
    client = LSquare(api_key=KEY, endpoint="https://ingest.example", _transport=slow, logger=Recorder(),
                     flush_timeout_ms=budget_ms, _clock=clock)
    try:
        with pytest.raises(SystemExit):
            client.capture(dict(TURN))
        client._track("call", concurrent.futures.Future())
        exiting.append(1)
        began = clock()
        client._exit_flush()
        spent_ms = (clock() - began) * 1000
        assert len(posted) == 1
        # Twice the timeout, before: the request's, then the flush's own wait for the call.
        assert spent_ms < budget_ms + 500, spent_ms
    finally:
        client.shutdown(timeout_ms=0)


# -- Quiet, warnings and the gate ----------------------------------------------------------------------------


def test_a_pool_that_refuses_takes_no_more_jobs_and_runs_the_ones_it_took() -> None:
    pool = Pool("test", 1)
    gate = threading.Event()
    ran: list[str] = []

    def taken() -> None:
        gate.wait(5)
        ran.append("taken")

    try:
        assert pool.submit(taken)
        pool.refuse()
        assert not pool.submit(lambda: ran.append("refused"))
        gate.set()
        assert pool.drain(5) and ran == ["taken"]
        # After a fork, the child's pool takes jobs again.
        pool.reset()
        assert pool.submit(lambda: ran.append("after a fork")) and pool.drain(5)
        assert ran == ["taken", "after a fork"]
    finally:
        gate.set()
        pool.stop()


def test_a_warning_given_once_never_waits_for_the_client_s_lock() -> None:
    recorder = Recorder()
    client = make(FakeTransport(), recorder)
    held = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with client._lock:
            held.set()
            release.wait(10)

    threading.Thread(target=hold, daemon=True).start()
    assert held.wait(5)
    try:
        on_own_thread(lambda: client._warn_once("test", "[lsquare] once"))
        on_own_thread(lambda: client._warn_once("test", "[lsquare] once"))
    finally:
        release.set()
    assert recorder.wait_for(lambda: ("warn", "[lsquare] once") in recorder.lines)
    assert client._callbacks.drain(5)
    assert recorder.lines.count(("warn", "[lsquare] once")) == 1
    client.shutdown(timeout_ms=0)


def test_a_gate_opens_once_set_and_times_out_until_then() -> None:
    from lsquare._client import _Gate

    gate = _Gate()
    assert not gate.is_set() and not gate.wait(0.01)
    gate.set()
    assert gate.is_set() and gate.wait(0) and gate.wait(None) and gate.wait(1)


def test_a_gate_set_twice_stays_set_and_opens_every_waiter() -> None:
    from lsquare._client import _Gate

    gate = _Gate()
    woke: list[bool] = []
    lock = threading.Lock()

    def waiter() -> None:
        opened = gate.wait(5)
        with lock:
            woke.append(opened)

    waiters = [threading.Thread(target=waiter, daemon=True) for _ in range(3)]
    for thread in waiters:
        thread.start()
    gate.set()
    gate.set()
    for thread in waiters:
        thread.join(5)
    assert woke == [True, True, True]
    assert gate.is_set() and gate.wait(0)
