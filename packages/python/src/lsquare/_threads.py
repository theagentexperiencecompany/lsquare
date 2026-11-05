"""The client's own threads (spec section 3): all daemons, started lazily, each in an empty context
so none holds on to the request that happened to start it.

- `Scheduler`: one thread for the client's deferred steps and timers, Python's counterpart of
  TypeScript's microtasks, `setImmediate` and `setTimeout`. Hook deadlines and retry delays run
  here, so they fire even while every hook thread is stuck, and a retry waiting out its delay
  holds no sending thread.
- `Pool`: up to `size` worker threads for blocking work (requests, sync hooks, callbacks).
- `PrivateLoop`: an asyncio loop on its own thread, for async hooks captured outside any loop.

Every thread exits once it has been idle for `IDLE_S`, or at once when its owner is stopped
(`shutdown()`), and starts again if more work comes: a client nobody uses holds no thread, and
nothing a finished job referenced stays reachable. The private loop is the exception: it runs until
`shutdown()`, or, once its client is collected, until its own thread notices (within `IDLE_S`),
since what an async hook made on it may be bound to it for the hook's next run. Some Pythons (3.12.2, not 3.10 or 3.14) refuse
to start threads once the main thread has finished, in `atexit` handlers and in non-daemon threads
still running: `submit` then says so instead of running the job, for the caller to decide, and the
exiting thread can take the scheduler thread's place (`Scheduler.take_over`).

A signal handler runs on the main thread between bytecodes, inside a thread start's wait too, and
a finalizer on any thread: either can call back into the SDK there, and a handler can raise
SystemExit. So no lock here is held while a thread starts, the ones a call back in can take again
are reentrant, and a thread start reserves its place with a token of its own, placed in one atomic
step: a start that raises or is interrupted takes its token back, unless the thread it started
took over first, and a thread that finds its token taken back runs only if there is room for it.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import functools
import heapq
import itertools
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Coroutine, Sized
from typing import Any, TypeVar

#: How long an idle thread waits for work before it exits.
IDLE_S = 30.0
#: How many passes a stopped private loop runs at most before it closes (see _wind_down): a
#: callback that queues itself again can't hold it open.
WIND_DOWN_PASSES = 100

T = TypeVar("T")


def start_thread(target: Callable[[], None], name: str) -> None:
    """Starts a daemon thread running `target` in a fresh, empty context. Raises if it can't."""
    context = contextvars.Context()
    if sys.version_info >= (3, 14):
        # Given as the thread's own: a Python that lets threads inherit contexts (-X
        # thread_inherit_context=1, free-threaded builds' default) would otherwise keep a copy of
        # the starting thread's on the Thread object, a request in it, for the thread's life.
        thread = threading.Thread(target=target, name=name, daemon=True, context=context)
    else:
        thread = threading.Thread(target=lambda: context.run(target), name=name, daemon=True)
    thread.start()


def acknowledging(
    coroutine: Coroutine[Any, Any, Any], started: concurrent.futures.Future[None]
) -> tuple[Coroutine[Any, Any, Any], Callable[[], None]]:
    """`coroutine`, wrapped to resolve `started` once it has run its first step, up to its first
    await: acknowledged from inside, as it is entered, so a step the loop's task factory runs first
    is never taken for it. Returns the wrapper, to hand to the loop in its place, and what to call
    once nothing will run it on: its future is done, the wrapper couldn't be handed over, or the
    loop it was handed to has closed. A `coroutine` that was never entered (a cancellation came
    first, or the loop closed before running it) is closed then, never left un-awaited. One that
    was is left as it is, done or as its closed loop left it, for the garbage collector: closing it
    there would run its cleanup on the calling thread, outside its loop."""
    entered: list[bool] = []

    async def enter() -> Any:
        entered.append(True)
        try:
            # Run once this step ends, the coroutine's first with it.
            asyncio.get_running_loop().call_soon(_resolved, started)
        except Exception:
            _resolved(started)
        try:
            return await coroutine
        except (SystemExit, KeyboardInterrupt) as error:
            if interrupts(error):
                raise
            # Off the main thread a failure like any other: never out of the loop running it.
            raise Interrupted(type(error).__name__) from error

    wrapper = enter()

    def close() -> None:
        # Never entered: none of it has run, nor will now.
        if not entered:
            wrapper.close()
            coroutine.close()

    return wrapper, close


def _resolved(future: concurrent.futures.Future[None]) -> None:
    try:
        if not future.done():
            future.set_result(None)
    except concurrent.futures.InvalidStateError:
        pass


def interrupts(error: BaseException) -> bool:
    """SystemExit or KeyboardInterrupt on the main thread: a signal handler's way, or the app's, of
    ending or interrupting the process. The SDK passes it on after its own cleanup, never swallows
    it; an app callback's can't be told from a handler's, so it is passed on too. On another thread
    it stays a failure like any other."""
    return isinstance(error, (SystemExit, KeyboardInterrupt)) and threading.current_thread() is threading.main_thread()


def exiting() -> bool:
    """Whether the main thread has finished: the interpreter is exiting (its `atexit` handlers run,
    or non-daemon threads still do), where some Pythons (3.12.2) refuse every thread start (see
    LSquare._at_exit)."""
    try:
        return not threading.main_thread().is_alive()
    except Exception:
        return False


class Interrupted(Exception):
    """What a task running an app's coroutine (an async hook, logger or `on_error`) raises in place
    of a SystemExit or KeyboardInterrupt the coroutine raised off the main thread, where either is a
    failure like any other (see interrupts): asyncio raises those two out of the loop the task runs
    on, which would end an app's loop on a thread of its own, or the private one, with it. The
    original is its `__cause__`."""


class _Driver:
    """The owner token of a thread that took the scheduler over (see Scheduler.take_over)."""

    __slots__ = ("ident",)

    def __init__(self) -> None:
        self.ident = threading.get_ident()


class Scheduler:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Condition()
        self._soon: deque[Callable[[], None]] = deque()
        self._timers: list[tuple[float, int, list[Callable[[], None] | None]]] = []
        self._ids = itertools.count()
        #: The token of the start or thread that owns the thread, while one runs or starts, or of
        #: the thread that took it over (`take_over`). Placed with setdefault, one step: a call
        #: back in between a check and a set (a signal handler's, on this thread) can't start a
        #: second thread.
        self._owner: dict[str, object] = {}
        self._stopping = False

    def call_soon(self, fn: Callable[[], None]) -> None:
        """Runs `fn` on the scheduler's thread, or the thread that took it over. With neither, and
        no thread to start (interpreter exit), on the calling thread instead, with whatever else is
        due now."""
        with self._lock:
            self._soon.append(fn)
            self._lock.notify()
        if not self._ensure_running():
            self._drain_inline()

    def call_later(self, delay_s: float, fn: Callable[[], None]) -> Callable[[], None]:
        """Runs `fn` after `delay_s` seconds. Returns a function that cancels it."""
        slot: list[Callable[[], None] | None] = [fn]
        with self._lock:
            heapq.heappush(self._timers, (self._clock() + max(0.0, delay_s), next(self._ids), slot))
            self._lock.notify()
        self._ensure_running()

        def cancel() -> None:
            slot[0] = None

        return cancel

    def resume(self) -> None:
        """Runs what's due now if no thread will, as after an interrupted start; with no thread to
        start (interpreter exit), on the calling thread."""
        with self._lock:
            due = bool(self._soon)
        if due and not self._ensure_running():
            self._drain_inline()

    def take_over(self) -> bool:
        """Interpreter exit, with no thread running the steps and none able to start: the calling
        thread takes that thread's place, until `give_back()`, and True. Meanwhile a step or timer
        scheduled from any thread waits for it to run them in `run_until()`, rather than running
        inline where it was scheduled, which nests (a request sent inline schedules its outcome,
        which sends the next request) and leaves timers no thread fires. False, having taken
        nothing, when a thread runs them or one could start: that one does."""
        if self._ensure_running():
            return False
        driver = _Driver()
        return self._owner.setdefault("thread", driver) is driver

    def driving(self) -> bool:
        """The calling thread took the scheduler over (see take_over)."""
        owner = self._owner.get("thread")
        return isinstance(owner, _Driver) and owner.ident == threading.get_ident()

    def run_until(self, until: float, done: Callable[[], bool]) -> None:
        """On the thread that took the scheduler over: runs the steps, and the timers as they come
        due, until `done()` or `until` on the scheduler's clock, and never longer than that on the
        monotonic clock, so a clock that stands still can't hold it. `done()` is read under the
        lock `wake()` takes: whatever ends the wait from another thread then calls `wake()`, and
        isn't missed. On any other thread it returns at once, having run nothing."""
        if not self.driving():
            return
        limit = time.monotonic() + max(0.0, until - self._clock())
        while True:
            fn: Callable[[], None] | None = None
            with self._lock:
                if done():
                    return
                now = self._clock()
                left = min(until - now, limit - time.monotonic())
                if left <= 0:
                    return
                if self._soon:
                    fn = self._soon.popleft()
                else:
                    while self._timers and self._timers[0][2][0] is None:
                        heapq.heappop(self._timers)
                    if not self._timers or self._timers[0][0] > now:
                        self._lock.wait(min(left, self._timers[0][0] - now) if self._timers else left)
                        continue
                    _, _, slot = heapq.heappop(self._timers)
                    # None if cancelled since: a cancel takes no lock.
                    fn, slot[0] = slot[0], None
            if fn is not None:
                _run(fn)
            # Nothing a finished step referenced stays reachable from here.
            fn = None

    def wake(self) -> None:
        """Has `run_until()` read its `done()` again."""
        with self._lock:
            self._lock.notify_all()

    def give_back(self) -> None:
        """Ends this thread's `take_over()`, if it took the scheduler over. What is still due stays
        queued: the next step scheduled runs it with its own."""
        with self._lock:
            if self.driving():
                del self._owner["thread"]

    def stop(self) -> None:
        """Pending timers are dropped; the thread exits once nothing is due now."""
        with self._lock:
            self._stopping = True
            self._timers = []
            self._lock.notify_all()

    def reset(self) -> None:
        """After a fork: no thread, nothing pending."""
        self._lock = threading.Condition()
        self._soon = deque()
        self._timers = []
        self._owner = {}
        self._stopping = False

    def _ensure_running(self) -> bool:
        token = object()
        try:
            if self._owner.setdefault("thread", token) is not token:
                return True
            start_thread(lambda: self._run(token), "lsquare-scheduler")
            return True
        except BaseException as error:  # noqa: BLE001 - an interruption is passed on below
            with self._lock:
                taken_back = self._owner.get("thread") is token
                if taken_back:
                    del self._owner["thread"]
            if not isinstance(error, Exception):
                raise
            return not taken_back

    def _drain_inline(self) -> None:
        while True:
            with self._lock:
                if not self._soon:
                    return
                fn = self._soon.popleft()
            _run(fn)
            del fn

    def _run(self, token: object) -> None:
        own = object()
        with self._lock:
            if self._owner.get("thread") is token:
                self._owner["thread"] = own
            elif self._owner.setdefault("thread", own) is not own:
                # Its start took its token back, and another thread runs.
                return
        while True:
            fn: Callable[[], None] | None = None
            with self._lock:
                while fn is None:
                    if self._soon:
                        fn = self._soon.popleft()
                        break
                    now = self._clock()
                    while self._timers and self._timers[0][2][0] is None:
                        heapq.heappop(self._timers)
                    if self._timers and self._timers[0][0] <= now:
                        _, _, slot = heapq.heappop(self._timers)
                        fn, slot[0] = slot[0], None
                        continue
                    if self._stopping and not self._timers:
                        self._owner.pop("thread", None)
                        return
                    wait = self._timers[0][0] - now if self._timers else IDLE_S
                    if not self._lock.wait(wait) and not self._soon and not self._timers:
                        # Idle for IDLE_S: the thread exits, and the next call starts another.
                        self._owner.pop("thread", None)
                        return
            _run(fn)
            # Nothing a finished step referenced stays reachable from this thread.
            fn = None


def _run(fn: Callable[[], None]) -> None:
    try:
        fn()
    except BaseException as error:  # noqa: BLE001 - a background step must never kill its thread
        # Run inline on the main thread (interpreter exit): a signal handler's SystemExit goes on.
        if interrupts(error):
            raise


class Pool:
    """Up to `size` daemon workers taking jobs in order; at most `max_waiting` jobs wait, if given."""

    def __init__(self, name: str, size: int, max_waiting: int | None = None) -> None:
        self._name = name
        self._size = max(1, size)
        self._max_waiting = max_waiting
        self._lock = threading.Condition()
        self._jobs: deque[Callable[[], None]] = deque()
        self._workers = 0
        #: The tokens of workers starting, not yet running: each counts as one.
        self._starting: set[object] = set()
        self._idle = 0
        #: Jobs running now.
        self._busy = 0
        self._stopping = False
        #: Set at interpreter exit, once the client is quiet: no job is taken from then on.
        self._refusing = False
        #: Set at interpreter exit (see resume): from then on busy workers don't count as taking a
        #: job.
        self._at_exit = False

    def submit(self, job: Callable[[], None]) -> bool:
        """Queues `job`. False, with the job not queued, when no worker runs and none can start
        (interpreter exit), once the pool refuses jobs, or when it's full: the caller decides what
        happens instead. At exit (see resume), also when no idle worker will take it and none can
        start for it. Checked and queued together, under the pool's lock, which is never held
        while a thread starts."""
        token = object()
        try:
            with self._lock:
                if self._refusing or self._full():
                    return False
                self._jobs.append(job)
                if self._idle > 0:
                    self._lock.notify()
                # Each idle worker takes one job, and one notified but not yet awake still counts as
                # idle: a burst starts a worker for every job beyond them, up to the limit.
                if len(self._jobs) <= self._idle:
                    return True
                if self._workers + len(self._starting) >= self._size:
                    if not self._behind_busy():
                        return True
                    self._jobs.remove(job)
                    return False
                self._starting.add(token)
            start_thread(lambda: self._work(token), f"lsquare-{self._name}")
            return True
        except BaseException as error:  # noqa: BLE001 - an interruption is passed on below
            with self._lock:
                self._starting.discard(token)
                if not isinstance(error, Exception):
                    # Interrupted (a signal handler's SystemExit, say): the job stays queued, for
                    # a worker left or the next one to start.
                    raise
                if self._workers + len(self._starting) > 0 and not self._behind_busy():
                    # A running worker will take it: at exit, an idle or starting one.
                    return True
                try:
                    self._jobs.remove(job)
                except ValueError:
                    return True
            return False

    def resume(self, at_exit: bool = False) -> list[Callable[[], None]]:
        """Starts a worker for jobs left waiting with none to take them, as after an interrupted
        start. With none able to start (interpreter exit), takes them out and returns them, for the
        caller to run another way. With `at_exit`, from now on busy workers don't count as taking
        a job, as one may be held up for longer than the exit allows: a job no idle or starting
        worker will take, with none able to start for it, is handed back here if it waits already,
        and refused by `submit` if it comes later."""
        token = object()
        try:
            with self._lock:
                if at_exit:
                    self._at_exit = True
                if not self._jobs or (self._workers + len(self._starting) > 0 and not self._behind_busy()):
                    return []
                if self._workers + len(self._starting) >= self._size:
                    return self._hand_back()
                self._starting.add(token)
            start_thread(lambda: self._work(token), f"lsquare-{self._name}")
            return []
        except BaseException as error:  # noqa: BLE001 - an interruption is passed on below
            with self._lock:
                self._starting.discard(token)
                if not isinstance(error, Exception):
                    raise
                if self._workers + len(self._starting) > 0 and not self._behind_busy():
                    return []
                return self._hand_back()

    def _behind_busy(self) -> bool:
        """At exit: more jobs wait than the idle and starting workers will take."""
        return self._at_exit and len(self._jobs) > self._idle + len(self._starting)

    def _hand_back(self) -> list[Callable[[], None]]:
        """Takes out, in order, the jobs beyond those the idle and starting workers will take."""
        jobs = list(self._jobs)[self._idle + len(self._starting):]
        for _ in jobs:
            self._jobs.pop()
        return jobs

    def refuse(self) -> None:
        """From now on `submit` takes no job (interpreter exit, once the client is quiet). What it
        took already still runs, and `drain` waits for that."""
        with self._lock:
            self._refusing = True

    def idle(self) -> bool:
        """No job waits or runs: each one taken so far has returned."""
        with self._lock:
            return not self._jobs and not self._busy

    def full(self) -> bool:
        """Whether `max_waiting` jobs wait."""
        with self._lock:
            return self._full()

    def _full(self) -> bool:
        return self._max_waiting is not None and len(self._jobs) >= self._max_waiting

    def drain(self, timeout: float) -> bool:
        """Waits up to `timeout` seconds until no job is queued or running; True when none is."""
        deadline = time.monotonic() + timeout
        with self._lock:
            while self._jobs or self._busy:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._lock.wait(left)
            return True

    def stop(self) -> None:
        """Idle workers exit now; busy ones once their job returns."""
        with self._lock:
            self._stopping = True
            self._lock.notify_all()

    def reset(self) -> None:
        self._lock = threading.Condition()
        self._jobs = deque()
        self._workers = 0
        self._starting = set()
        self._idle = 0
        self._busy = 0
        self._stopping = False
        self._refusing = False
        self._at_exit = False

    def _work(self, token: object) -> None:
        with self._lock:
            if token in self._starting:
                self._starting.discard(token)
            elif not self._jobs or self._workers + len(self._starting) >= self._size:
                # Its start took its token back: it works only if a job waits and there is room.
                return
            self._workers += 1
        while True:
            with self._lock:
                while not self._jobs:
                    if self._stopping:
                        self._workers -= 1
                        return
                    self._idle += 1
                    woke = self._lock.wait(IDLE_S)
                    self._idle -= 1
                    if not woke and not self._jobs:
                        self._workers -= 1
                        return
                job = self._jobs.popleft()
                self._busy += 1
            _run(job)
            job = None  # type: ignore[assignment]
            with self._lock:
                self._busy -= 1
                # drain() waits on the same condition.
                self._lock.notify_all()


class _Start:
    """A private loop's thread start. Until that thread takes the loop over, each piece of work
    handed to the loop leaves here what to call if it never runs."""

    __slots__ = ("failures", "state")

    def __init__(self) -> None:
        self.failures: list[Callable[[BaseException], None]] = []
        #: "starting"; then "running" once its thread took the loop over, or "failed" once its start
        #: did, whichever came first.
        self.state = "starting"


class PrivateLoop:
    """An asyncio event loop on a daemon thread, started on first use, run until stop() (the
    client's shutdown()) or, once its owner is collected, until its own thread notices.

    `owner` is a weak reference to the client, without a callback: the loop's thread checks it
    every IDLE_S, on that thread, and stops the loop once it is dead, cancelling the tasks still
    pending there (a hook's child task, say), which no one would end otherwise (py-runtime-10).
    Nothing of this runs at garbage-collection time, where a signal handler's SystemExit would be
    swallowed. It never idles out while its owner lives: what an async hook made on it (a
    connection pool, say) may be bound to it, for the hook's next run.

    stop() is terminal for hooks (py-runtime-5): from then on, until a fork's reset, no hook is
    handed to it, nor created on it, refused under the same lock as handing one over, and on the
    loop's thread as one would be created, so a stop() called back in anywhere inside a hand-off
    (a signal handler's shutdown(), landing as the loop is made) refuses it too. An async callback
    (`on_error`, a logger) handed over after it still runs: on a loop that stops itself after its
    first pass, running what it was given to its end, then closes. Such a loop takes work only
    until its thread takes it over, withdrawn then under the lock that hands work over: a callback
    after that gets a loop of its own, never one winding down, whose close would leave it unrun."""

    def __init__(self, owner: Callable[[], Any] | None = None) -> None:
        #: Reentrant, and held only to read, publish or hand work to the loop, never while its
        #: thread starts: a stop() from a signal handler or finalizer can run on a thread that is
        #: inside hand().
        self._lock = threading.RLock()
        #: The published loop and its start, read together in one step, so a stop() called back in
        #: anywhere in between can't pair one loop with another's start.
        self._current: tuple[asyncio.AbstractEventLoop, _Start] | None = None
        self._owner = owner
        #: stop() has run: hooks are refused, and a loop started from here stops after one pass.
        self._stopped = False

    @property
    def refusing(self) -> bool:
        """Whether hooks are refused (see stop())."""
        return self._stopped

    def hand(
        self, give: Callable[[asyncio.AbstractEventLoop], T], failed: Callable[[BaseException], None], hook: bool = False
    ) -> T:
        """`give(loop)` hands work to the loop, started if it isn't; returns what `give` returned.
        The loop is published before its thread starts, which nothing waits for: what is handed to
        it runs once the thread does, and a stop() from now on reaches it. If that thread never
        starts, each `failed(error)` noted since the loop was made runs instead, once, on the thread
        whose start failed, so every caller that handed the loop work hears of it, not only the one
        that started it; that thread raises only what interrupted its start (a signal handler's
        SystemExit). Raises what `give` raises: a loop that closed since, say. A `hook` is refused
        once stop() has run (RuntimeError), checked under the lock that hands it over."""
        made: tuple[asyncio.AbstractEventLoop, _Start] | None = None
        try:
            with self._lock:
                if hook and self._stopped:
                    raise RuntimeError("The client has shut down: no hook runs on its event loop.")
                current = self._current
                if current is None or current[0].is_closed():
                    current = made = (asyncio.new_event_loop(), _Start())
                    self._current = made
                loop, start = current
                # Handed over and noted in one step: a start that fails finds both or neither.
                out = give(loop)
                if start.state == "starting":
                    start.failures.append(failed)
        finally:
            if made is not None:
                self._begin(*made)
        return out

    def run(self, coroutine: Coroutine[Any, Any, Any]) -> tuple[concurrent.futures.Future[Any], asyncio.AbstractEventLoop]:
        """`asyncio.run_coroutine_threadsafe` on the loop, started if it isn't: its future, and the
        loop. If its thread never starts, the coroutine is closed and the future fails."""
        handed: list[concurrent.futures.Future[Any]] = []

        def give(loop: asyncio.AbstractEventLoop) -> tuple[concurrent.futures.Future[Any], asyncio.AbstractEventLoop]:
            handed.append(asyncio.run_coroutine_threadsafe(coroutine, loop))
            return handed[0], loop

        def failed(error: BaseException) -> None:
            coroutine.close()
            if handed[0].set_running_or_notify_cancel():
                handed[0].set_exception(error)

        return self.hand(give, failed)

    def get(self) -> asyncio.AbstractEventLoop:
        """The loop, started if it isn't (see hand). Raises if its thread can't start."""
        failure: list[BaseException] = []
        loop = self.hand(lambda loop: loop, failure.append)
        if failure:
            raise failure[0]
        return loop

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            current, self._current = self._current, None
        if current is not None and not current[0].is_closed():
            try:
                current[0].call_soon_threadsafe(current[0].stop)
            except RuntimeError:
                pass

    def reset(self) -> None:
        self._lock = threading.RLock()
        self._current = None
        self._stopped = False

    def _begin(self, loop: asyncio.AbstractEventLoop, start: _Start) -> None:
        try:
            start_thread(lambda: self._take_over(loop, start), "lsquare-loop")
        except BaseException as error:  # noqa: BLE001 - an interruption is passed on below
            with self._lock:
                running = start.state == "running"
                if not running:
                    start.state = "failed"
                failures, start.failures = start.failures, []
                # Withdrawn, so no one else hands it work.
                if self._current is not None and self._current[1] is start:
                    self._current = None
            if running:
                # Interrupted after its thread took it over (a signal handler's SystemExit): that
                # thread finishes what it was given, then stops and closes it.
                try:
                    loop.call_soon_threadsafe(loop.stop)
                except RuntimeError:
                    pass
            else:
                # No thread will ever run it: closed, with what was handed to it, and each of those
                # fails instead, giving back what it holds.
                loop.close()
                cause = error if isinstance(error, Exception) else RuntimeError("The SDK's event loop thread didn't start.")
                for fail in failures:
                    _run(functools.partial(fail, cause))
            if not isinstance(error, Exception):
                raise

    def _take_over(self, loop: asyncio.AbstractEventLoop, start: _Start) -> None:
        with self._lock:
            if start.state != "starting":
                # Its start failed first, and failed what was handed to the loop.
                return
            start.state = "running"
            start.failures = []
            stopped = self._stopped
            if stopped and self._current is not None and self._current[1] is start:
                # Withdrawn under the lock that hands work over, before its one pass: what was
                # handed to it is queued already, and runs; what comes later gets a loop of its
                # own, never this one as it winds down and closes, where it would never run.
                self._current = None
        if stopped:
            # Made around a stop() (one called back in as it was made, or a callback's after it):
            # one pass, then it winds down and closes.
            loop.call_soon(loop.stop)
        _run_loop(loop, self._owner)


def _run_loop(loop: asyncio.AbstractEventLoop, owner: Callable[[], Any] | None = None) -> None:
    asyncio.set_event_loop(loop)
    #: Its owner was found collected: what is left on the loop is cancelled as it winds down.
    gone: list[bool] = []
    watch: list[asyncio.TimerHandle] = []

    def check() -> None:
        # On the loop's own thread, never the main one: nothing here can swallow a signal
        # handler's SystemExit. IDLE_S read each time, as the pools read it.
        watch.clear()
        if owner is not None and owner() is None:
            gone.append(True)
            loop.stop()
            return
        watch.append(loop.call_later(IDLE_S, check))

    if owner is not None:
        watch.append(loop.call_later(IDLE_S, check))
    try:
        loop.run_forever()
    finally:
        try:
            for handle in watch:
                handle.cancel()
            watch.clear()
            _wind_down(loop, cancel=bool(gone))
        finally:
            loop.close()


def _wind_down(loop: asyncio.AbstractEventLoop, cancel: bool = False) -> None:
    """Runs what a stopped private loop still has before it closes: its tasks, to their end, and
    the callbacks queued, with those they queue in turn, at most WIND_DOWN_PASSES passes. A task's
    done-callbacks run a pass after it ends, perhaps the loop's last: a hook's settling, and
    `run_coroutine_threadsafe`'s, which settles the future an async logger or `on_error` holds its
    slot by. Closed without them, the future never settles, and keeps the task, with what it
    returned."""
    # asyncio's own loops queue them here; another (an app's loop policy) gets every pass.
    ready = getattr(loop, "_ready", None)
    #: With `cancel` (its owner collected): how long the cancelled tasks get to end, in all.
    until = time.monotonic() + IDLE_S
    for _ in range(WIND_DOWN_PASSES):
        pending = asyncio.all_tasks(loop)
        if pending and cancel:
            # Nothing will ever end them now: a hook's child task left awaiting, say. Cancelled,
            # each pass, and given until IDLE_S from here in all; one that resists longer, or blocks
            # the loop, is left as it is when the loop closes.
            left = until - time.monotonic()
            if left <= 0:
                return
            for task in pending:
                task.cancel()
            loop.run_until_complete(asyncio.wait(pending, timeout=left))
            continue
        if pending:
            # Each hook was already cancelled once, at its deadline or by shutdown(): not again, so
            # its own cleanup runs, given the time it takes. A daemon thread: a hook that never ends
            # only keeps this one.
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            continue
        if isinstance(ready, Sized) and not ready:
            return
        # Stopped before it runs: one pass, over what is queued now.
        loop.stop()
        loop.run_forever()


def schedule_on(loop: asyncio.AbstractEventLoop | PrivateLoop, make: Callable[[], Any], context: contextvars.Context,
                done: Callable[[BaseException | None, Any], None]) -> Callable[[], None]:
    """Runs the coroutine `make()` returns as a task on `loop`, created inside `context` (so the
    task runs in it, on Python 3.10 too), and calls `done(error, result)` once it settles. Returns a
    function that cancels the task, and that holds nothing of the task once it has settled, nor what
    it returned, wherever it is kept. Raises if the loop is closed. On the private loop, if its
    thread can't start, `done(error, None)` is called instead; once that loop has been stopped
    (shutdown()), it raises, and a hook handed over as it was stopped is never created on it, its
    `done` called with that failure instead (see PrivateLoop)."""
    state: dict[str, Any] = {"task": None, "cancelled": False}
    #: Reentrant: a signal handler on the loop's thread can cancel the task from inside create().
    lock = threading.RLock()
    private = loop if isinstance(loop, PrivateLoop) else None

    def create() -> None:
        if private is not None and private.refusing:
            # Stopped after it was handed over (a stop() called back in during the hand-off): it
            # never starts, as one handed over after the stop wouldn't.
            done(RuntimeError("The client has shut down: no hook runs on its event loop."), None)
            return
        try:
            coroutine = make()
            task = context.run(asyncio.ensure_future, coroutine)
        except BaseException as error:  # noqa: BLE001 - reported as the hook's failure
            done(error, None)
            if interrupts(error):
                raise
            return
        with lock:
            state["task"] = task
            cancel_now = state["cancelled"]
        if cancel_now:
            task.cancel()

        def settled(finished: asyncio.Future[Any]) -> None:
            # Nothing left to cancel.
            with lock:
                state["task"] = None
            called = False
            try:
                if finished.cancelled():
                    called = True
                    done(asyncio.CancelledError(), None)
                    return
                error = finished.exception()
                result = None if error is not None else finished.result()
                called = True
                done(error, result)
            except BaseException as caught:  # noqa: BLE001
                # Settled once: `done` isn't called again for what interrupted it.
                if not called:
                    done(caught, None)
                if interrupts(caught):
                    raise

        task.add_done_callback(settled)

    if isinstance(loop, PrivateLoop):
        loop.hand(lambda running: running.call_soon_threadsafe(create), lambda error: done(error, None), hook=True)
    else:
        loop.call_soon_threadsafe(create)

    def cancel() -> None:
        with lock:
            state["cancelled"] = True
            task = state["task"]
        if task is not None:
            try:
                task.get_loop().call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass

    return cancel
