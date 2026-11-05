"""The wrapper's side of the hook lifecycle fixes (31 October 2026), on each line: identify's result
checked once (parity-4), its diagnostic reported only for a call the client admits (parity-25), and
the deferred build's place: the call's loop at identify's deadline, a trampoline that keeps nothing
there, and shutdown()'s disposal (py-mcp-19). Each fails before its fix unless it says it's a
guard. Events, gates and the loop's own turns order everything; nothing asserts a time."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import signal
import subprocess
import sys
import threading
import weakref
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from helpers import Recorder, collected, eventually
from lines import client, connect, high, needs_line, tool
from lsquare import LSquare
from lsquare.mcp import IdentifyInput, _call, instrument

pytestmark = [needs_line]
OFF = {"sessions": False, "intent": False, "missing_tools": False}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def search(query: str) -> str:
    return f"found {query}"


def recorded() -> tuple[LSquare, Any, Recorder]:
    recorder = Recorder()
    lsquare, events = client(on_error=recorder.on_error, logger=recorder)
    return lsquare, events, recorder


async def delivered(lsquare: LSquare) -> None:
    """Until the reports raised so far have reached on_error."""
    await asyncio.wrap_future(lsquare._lsquare_internal().delivered())


async def until(check: Any) -> bool:
    """Lets the loop run until `check()` holds (5 s at most)."""
    for _ in range(5_000):
        if check():
            return True
        await asyncio.sleep(0.001)
    return bool(check())


# -- parity-4: identify's result is read and checked once ------------------------------------------------------


@pytest.mark.anyio
async def test_the_ids_are_identifys_as_it_settled_never_read_again_at_the_build() -> None:
    lsquare, events = client()
    shared = {"orgId": "org-1", "userId": "user-a"}

    async def changing(query: str) -> str:
        # identify has settled, and its result been checked: the ID fields last known are set.
        assert await until(lambda: lsquare._identified["keys"] is not None)
        shared["userId"] = "user-b"
        del shared["orgId"]
        return "found"

    app = high()
    tool(app, changing, name="search")
    instrument(app, lsquare, identify=lambda given: shared, extras=OFF)
    async with connect(app) as connection:
        await connection.call("search", {"query": "a"})
    await lsquare.aflush(timeout_ms=5_000)
    [event] = events.calls()
    assert (event.get("orgId"), event.get("userId")) == ("org-1", "user-a")
    assert lsquare._identified["keys"] == ("orgId", "userId")
    lsquare.shutdown(timeout_ms=0)


class Counting(Mapping[str, Any]):
    """identify's result: each read counted, the first one held until `go`."""

    def __init__(self, data: Mapping[str, Any], first: threading.Event, go: threading.Event) -> None:
        self._data = dict(data)
        self._first = first
        self._go = go
        self.reads: list[str] = []
        self.lock = threading.Lock()

    def __getitem__(self, key: str) -> Any:
        with self.lock:
            self.reads.append(key)
            first = len(self.reads) == 1
        if first:
            self._first.set()
            self._go.wait(10)
        return self._data[key]

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


@pytest.mark.anyio
async def test_a_build_that_finds_identify_s_result_being_checked_goes_on_from_that_check_without_reading_it_or_blocking_the_loop() -> None:
    # identify's settling claims the check and is held inside the app's getter. The tool returns:
    # its build doesn't read the object again, nor wait there, on the call's loop: the reply goes
    # out, and the event is built from that one check once it is published.
    lsquare, events = client()
    first, go, entered = threading.Event(), threading.Event(), threading.Event()
    result = Counting({"orgId": "org-1", "userId": "user-a"}, first, go)

    def identify(given: IdentifyInput) -> Any:
        # Answers once the tool runs, so its settling, on this hook thread, makes the check.
        entered.wait(10)
        return result

    async def waiting(query: str) -> str:
        entered.set()
        assert await until(first.is_set)
        return "found"

    app = high()
    tool(app, waiting, name="search")
    instrument(app, lsquare, identify=identify, extras=OFF)
    try:
        async with connect(app) as connection:
            await connection.call("search", {"query": "a"})
            # Replied, the check still held: the build didn't wait for it on this loop.
            assert not go.is_set()
            go.set()
            await lsquare.aflush(timeout_ms=5_000)
    finally:
        go.set()
        entered.set()
    [event] = events.calls()
    assert (event.get("orgId"), event.get("userId")) == ("org-1", "user-a")
    assert sorted(result.reads) == ["orgId", "userId"]
    lsquare.shutdown(timeout_ms=0)


def test_a_build_that_found_identify_s_check_under_way_is_built_once_it_is_published_though_the_call_s_loop_has_stopped() -> None:
    # As above, the check held in the app's getter as the tool returns; then the call's loop stops,
    # still open, as after run_until_complete(), and the check is published. The build goes to that
    # loop, and a client thread makes it LOOP_GRACE_S later, as the loop hasn't. Before, nothing
    # did: no timer was armed for it (identify had settled as the tool returned), flush() counted it
    # pending, and shutdown() dropped it.
    lsquare, events = client()
    first, go, entered = threading.Event(), threading.Event(), threading.Event()
    result = Counting({"orgId": "org-1", "userId": "user-a"}, first, go)
    builds: list[str] = []
    internal = lsquare._lsquare_internal()
    original = internal.record

    def recording(call: Any, loop: Any = None, origin: Any = None, diagnostic: Any = None) -> None:
        builds.append(threading.current_thread().name)
        if diagnostic is None:
            original(call, loop=loop, origin=origin)
        else:
            original(call, loop=loop, origin=origin, diagnostic=diagnostic)

    internal.record = recording  # type: ignore[method-assign]

    def identify(given: IdentifyInput) -> Any:
        entered.wait(10)
        return result

    async def waiting(query: str) -> str:
        entered.set()
        assert await until(first.is_set)
        return "found"

    app = high()
    tool(app, waiting, name="search")
    instrument(app, lsquare, identify=identify, extras=OFF)
    loop = asyncio.new_event_loop()

    async def go_call() -> None:
        async with connect(app) as connection:
            await connection.call("search", {"query": "a"})

    try:
        loop.run_until_complete(go_call())
        # Replied, the loop stopped, the check still held: nothing built yet.
        assert builds == []
        go.set()
        assert lsquare.flush(timeout_ms=5_000).pending == 0
        [event] = events.calls()
        assert (event.get("orgId"), event.get("userId")) == ("org-1", "user-a")
        assert sorted(result.reads) == ["orgId", "userId"]
        assert builds and builds[0] != threading.main_thread().name
        # Run again: what the check queued there finds the call taken, and builds nothing.
        loop.run_until_complete(asyncio.sleep(0))
        loop.run_until_complete(asyncio.sleep(0))
        assert len(builds) == 1
    finally:
        go.set()
        entered.set()
        loop.close()
        lsquare.shutdown(timeout_ms=0)


# -- parity-25: identify_failed only for a call the client admits ----------------------------------------------


@pytest.mark.anyio
async def test_calls_after_shutdown_report_only_the_shutdown_and_identify_isnt_called() -> None:
    lsquare, events, recorder = recorded()
    called: list[str] = []

    def identify(given: IdentifyInput) -> Any:
        called.append(given.tool_name)
        return {"userId": "u"}

    app = high()
    tool(app, search)
    instrument(app, lsquare, identify=identify, extras=OFF)
    async with connect(app) as connection:
        await connection.call("search", {"query": "a"})
        await lsquare.aflush(timeout_ms=5_000)
        await lsquare.ashutdown()
        for _ in range(3):
            await connection.call("search", {"query": "b"})
    await delivered(lsquare)
    assert recorder.codes() == ["shutdown"]
    assert called == ["search"]
    assert len(events.calls()) == 1


@pytest.mark.anyio
async def test_a_call_in_flight_at_shutdown_reports_only_the_shutdown_never_identify_failed() -> None:
    lsquare, events, recorder = recorded()
    release = threading.Event()

    def identify(given: IdentifyInput) -> Any:
        release.wait(10)
        raise RuntimeError("down")

    app = high()
    tool(app, search)
    instrument(app, lsquare, identify=identify, extras=OFF)
    try:
        async with connect(app) as connection:
            await connection.call("search", {"query": "a"})
            result = await lsquare.ashutdown(timeout_ms=0)
    finally:
        release.set()
    await delivered(lsquare)
    assert result.failed == 1
    assert recorder.codes() == ["shutdown"]
    assert events.calls() == []


@pytest.mark.anyio
async def test_an_async_identify_that_shutdown_cancels_mid_call_reports_only_the_shutdown_never_identify_failed() -> None:
    # The skeptic's case: an async identify still running as ashutdown() ends is cancelled there.
    # Before, its settling built the call's event all the same, reporting identify_failed
    # ("identify threw; the call was sent as limited diagnostics.") before the client refused that
    # event for the shutdown.
    lsquare, events, recorder = recorded()
    ended = threading.Event()
    builds: list[concurrent.futures.Future[None]] = []
    internal = lsquare._lsquare_internal()
    original = internal.track

    def tracking(call_id: str, finishing: concurrent.futures.Future[None], *rest: Any) -> None:
        builds.append(finishing)
        original(call_id, finishing, *rest)

    internal.track = tracking  # type: ignore[method-assign,assignment]

    async def identify(given: IdentifyInput) -> Any:
        try:
            await asyncio.Event().wait()
        finally:
            ended.set()
        return {"userId": "u"}

    app = high()
    tool(app, search)
    instrument(app, lsquare, identify=identify, extras=OFF)
    async with connect(app) as connection:
        await connection.call("search", {"query": "a"})
        result = await lsquare.ashutdown(timeout_ms=50)
        # identify cancelled by it, and whatever its settling queues on this loop run: its task's
        # done-callbacks a turn later, the build they queue a turn after that.
        assert await until(ended.is_set)
        for _ in range(3):
            await asyncio.sleep(0)
        assert await until(lambda: len(builds) == 1 and builds[0].done())
    await delivered(lsquare)
    assert result.failed == 1
    assert recorder.codes() == ["shutdown"]
    assert events.calls() == []


@pytest.mark.anyio
async def test_a_shutdown_landing_between_the_call_s_record_and_its_admission_reports_only_the_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    # Decided where the client admits the event, under its lock: a check of the client's state in
    # the build, before queuing, would still claim the call was sent.
    lsquare, events, recorder = recorded()

    def identify(given: IdentifyInput) -> Any:
        raise RuntimeError("down")

    real = _call.build_record

    def building(*args: Any) -> Any:
        out = real(*args)
        lsquare.shutdown(timeout_ms=0)
        return out

    monkeypatch.setattr(_call, "build_record", building)
    app = high()
    tool(app, search)
    instrument(app, lsquare, identify=identify, extras=OFF)
    async with connect(app) as connection:
        await connection.call("search", {"query": "a"})
    await delivered(lsquare)
    assert recorder.codes() == ["shutdown"]
    assert events.calls() == []


@pytest.mark.anyio
async def test_an_identify_that_fails_for_a_call_the_client_admits_is_still_reported() -> None:
    # Guard: a genuine failure, the call accepted, says so.
    lsquare, events, recorder = recorded()

    def identify(given: IdentifyInput) -> Any:
        raise RuntimeError("down")

    app = high()
    tool(app, search)
    instrument(app, lsquare, identify=identify, extras=OFF)
    async with connect(app) as connection:
        await connection.call("search", {"query": "a"})
    await lsquare.aflush(timeout_ms=5_000)
    await delivered(lsquare)
    assert recorder.codes() == ["identify_failed"]
    assert "identify threw" in recorder.errors[0].message
    assert len(events.calls()) == 1
    lsquare.shutdown(timeout_ms=0)


# -- py-mcp-19: the deferred build's place, and what holds the call meanwhile ---------------------------------------


@pytest.mark.anyio
async def test_at_identify_s_deadline_the_event_is_built_on_the_call_s_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    # The loop is free (awaiting the flush), and the client threads' fallback far off: the loop
    # builds it, at the deadline.
    monkeypatch.setattr(_call, "IDENTIFY_WAIT_S", 0.2)
    monkeypatch.setattr(_call, "LOOP_GRACE_S", 5.0, raising=False)
    lsquare, events = client()
    release = threading.Event()
    threads: list[str] = []
    internal = lsquare._lsquare_internal()
    original = internal.record

    def recording(call: Any, loop: Any = None, origin: Any = None, diagnostic: Any = None) -> None:
        threads.append(threading.current_thread().name)
        # Passed on only when given, as a wrapper that gives none calls it.
        if diagnostic is None:
            original(call, loop=loop, origin=origin)
        else:
            original(call, loop=loop, origin=origin, diagnostic=diagnostic)

    internal.record = recording  # type: ignore[method-assign]
    app = high()
    tool(app, search)
    instrument(app, lsquare, identify=lambda given: (release.wait(10), {"userId": "late"})[1], extras=OFF)
    try:
        async with connect(app) as connection:
            await connection.call("search", {"query": "a"})
        await lsquare.aflush(timeout_ms=5_000)
    finally:
        release.set()
    [event] = events.calls()
    assert event["identity"] == "failed"
    assert threads == [threading.main_thread().name]
    lsquare.shutdown(timeout_ms=0)


def held_call(monkeypatch: pytest.MonkeyPatch) -> list[weakref.ref[Any]]:
    """Each call handed off, held weakly: once its event is built or dropped, nothing should keep it."""
    held: list[weakref.ref[Any]] = []
    original = _call.hand_off

    def spying(state: Any, call: Any, *rest: Any) -> Any:
        held.append(weakref.ref(call))
        return original(state, call, *rest)

    monkeypatch.setattr(_call, "hand_off", spying)
    return held


def test_a_loop_stopped_at_identify_s_deadline_keeps_nothing_of_the_call_and_runs_nothing_when_it_runs_again(monkeypatch: pytest.MonkeyPatch) -> None:
    # Guard (the skeptic's retention test): the deadline queues the build on the call's loop,
    # stopped here, still open, as after run_until_complete(). The client threads' fallback builds
    # it; what the loop still has queued then holds nothing of the call, its result included, and
    # runs nothing once the loop runs again.
    monkeypatch.setattr(_call, "IDENTIFY_WAIT_S", 0.1)
    monkeypatch.setattr(_call, "LOOP_GRACE_S", 0.05, raising=False)
    held = held_call(monkeypatch)
    release = threading.Event()
    lsquare, events = client()
    builds: list[str] = []
    internal = lsquare._lsquare_internal()
    original = internal.record

    def recording(call: Any, loop: Any = None, origin: Any = None, diagnostic: Any = None) -> None:
        builds.append(threading.current_thread().name)
        # Passed on only when given, as a wrapper that gives none calls it.
        if diagnostic is None:
            original(call, loop=loop, origin=origin)
        else:
            original(call, loop=loop, origin=origin, diagnostic=diagnostic)

    internal.record = recording  # type: ignore[method-assign]
    app = high()
    tool(app, search)
    instrument(app, lsquare, identify=lambda given: (release.wait(10), {"userId": "u"})[1], extras=OFF)
    loop = asyncio.new_event_loop()

    async def go() -> None:
        async with connect(app) as connection:
            await connection.call("search", {"query": "a"})

    try:
        loop.run_until_complete(go())
        # Stopped, not closed: the fallback builds it on the client's threads.
        assert eventually(lambda: len(builds) == 1)
        assert held and collected(*held)
        # Run again: what the deadline queued there finds the call taken, and does nothing.
        loop.run_until_complete(asyncio.sleep(0))
        loop.run_until_complete(asyncio.sleep(0))
        assert len(builds) == 1
        lsquare.flush(timeout_ms=5_000)
        assert len(events.calls()) == 1
    finally:
        release.set()
        loop.close()
        lsquare.shutdown(timeout_ms=0)


def test_shutdown_takes_a_build_still_waiting_for_identify_and_keeps_nothing_of_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    # identify lets go of its input and waits; the tool returns and its loop stops. shutdown() counts
    # the call as failed, reports the shutdown, and takes its deferred build: before, that build
    # stayed armed on identify's settling, holding the call (its result too) and its place among
    # the calls being built.
    held = held_call(monkeypatch)
    release = threading.Event()
    lsquare, events, recorder = recorded()

    def identify(given: IdentifyInput) -> Any:
        del given
        release.wait(10)
        return {"userId": "u"}

    app = high()
    tool(app, search)
    instrument(app, lsquare, identify=identify, extras=OFF)
    loop = asyncio.new_event_loop()

    async def go() -> None:
        async with connect(app) as connection:
            await connection.call("search", {"query": "a"})

    try:
        loop.run_until_complete(go())
        result = lsquare.shutdown(timeout_ms=0)
        assert result.failed == 1
        assert held and collected(*held)
        assert lsquare._finishing_calls == {}
        future: concurrent.futures.Future[None] = lsquare._lsquare_internal().delivered()
        concurrent.futures.wait([future], timeout=5)
        assert recorder.codes() == ["shutdown"]
        assert events.calls() == []
    finally:
        release.set()
        loop.close()


# -- Codex's first review of these fixes, 1 November 2026 ------------------------------------------------------
#
# Finding 1: shutdown() from a signal handler deadlocked on a thread stopped inside the claim of a
# call's build, or inside `keep`, both under a lock that wasn't reentrant; identify's own lock had
# the same flaw. A real SIGUSR1 is raised at each line of those, in turn, its handler shutting the
# client down there; the child's faulthandler ends a hang, with the stacks.

SWEEP = """
import asyncio, faulthandler, signal, sys, threading, time
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, sys.argv[1] + "/wrapper")
faulthandler.dump_traceback_later(60, exit=True)
from helpers import FakeTransport
from lines import client, connect, high, tool
from lsquare import LSquare
from lsquare.mcp import _call, instrument

where = sys.argv[2]
OFF = {"sessions": False, "intent": False, "missing_tools": False}
if where == "claim":
    # Built on this loop, at identify's deadline: the claim runs on the main thread.
    _call.IDENTIFY_WAIT_S = 0.05
    _call.LOOP_GRACE_S = 30.0
traced = {"keep": [_call.hand_off], "claim": [_call.hand_off], "identify": [_call.on_time]}[where]
names = {"keep": {"keep"}, "claim": {"claim"}, "identify": {"on_time"}}[where]
state = {"target": 0, "seen": 0, "fired": False, "client": None}

def handler(signum, frame):
    state["client"].shutdown(timeout_ms=0)

signal.signal(signal.SIGUSR1, handler)

def local(frame, event, arg):
    # The target-th line run in those functions, this round: the handler runs inside raise_signal.
    if event == "line" and not state["fired"]:
        if state["seen"] == state["target"]:
            state["fired"] = True
            signal.raise_signal(signal.SIGUSR1)
        state["seen"] += 1
    return local

def tracer(frame, event, arg):
    if event == "call" and frame.f_code.co_name in names and frame.f_code.co_filename == _call.__file__:
        return local
    return None

async def search(query: str) -> str:
    return "found"

async def hook(given):
    await asyncio.Event().wait()

def call_round():
    release = threading.Event()
    lsquare, events = client()
    state["client"] = lsquare
    built = []
    internal = lsquare._lsquare_internal()
    original = internal.track

    def tracking(call_id, finishing, *rest):
        built.append(finishing)
        original(call_id, finishing, *rest)

    internal.track = tracking
    app = high()
    tool(app, search)
    instrument(app, lsquare, identify=lambda given: (release.wait(10), {"userId": "u"})[1], extras=OFF)

    async def main():
        async with connect(app) as connection:
            sys.settrace(tracer)
            try:
                await connection.call("search", {"query": "a"})
                if where == "claim":
                    await asyncio.wait_for(asyncio.wrap_future(built[0]), 10)
            finally:
                sys.settrace(None)

    asyncio.run(main())
    release.set()
    fired = state["fired"]
    lsquare.shutdown(timeout_ms=0)
    assert built and built[0].done(), "unbuilt"
    if fired:
        # Taken by the shutdown, or built and refused there: never sent.
        assert events.calls() == [], events.calls()
        assert lsquare._finishing_calls == {}
    return fired

def identify_round():
    # identify's lock held on this thread, in on_time, as the handler's shutdown() aborts an identify
    # whose loop closed: settled there, its settling takes that lock again.
    lsquare = LSquare(api_key="lsq_test", endpoint="http://localhost:1", _transport=FakeTransport())
    state["client"] = lsquare
    internal = lsquare._lsquare_internal()
    loop = asyncio.new_event_loop()

    async def start():
        return internal.identify(hook, lambda signal: None, internal.origin())

    run = loop.run_until_complete(start())
    loop.close()
    identify = _call.Identifying()
    identify.run = run
    identify.deadline = time.monotonic() + 60
    judged = []
    run.future.add_done_callback(lambda future: judged.append(_call.on_time(identify)))
    sys.settrace(tracer)
    try:
        _call.on_time(identify)
    finally:
        sys.settrace(None)
    fired = state["fired"]
    lsquare.shutdown(timeout_ms=0)
    if fired:
        assert run.future.done() and judged, (run.future.done(), judged)
    return fired

swept = 0
while (identify_round if where == "identify" else call_round)():
    swept += 1
    state.update(target=state["target"] + 1, seen=0, fired=False)
print("swept", swept, flush=True)
"""


@pytest.mark.skipif(not hasattr(signal, "SIGUSR1"), reason="no SIGUSR1 here")
@pytest.mark.parametrize("where", ["keep", "claim", "identify"])
def test_shutdown_from_a_signal_handler_anywhere_in_a_build_s_claim_its_timers_registration_or_identify_s_lock_never_deadlocks(where: str) -> None:
    # Before: the child hung at the first line inside the lock (keep: its second, claim: its
    # second, on_time: its second), the handler's shutdown() waiting for its own thread.
    tests = str(Path(__file__).resolve().parent.parent)
    done = subprocess.run([sys.executable, "-c", SWEEP, tests, where], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr[-6_000:]
    lines = done.stdout.split()
    assert lines[:1] == ["swept"], done.stdout + done.stderr[-6_000:]
    # Every line of each: keep runs twice per call, claim and on_time once.
    assert int(lines[1]) >= {"keep": 4, "claim": 5, "identify": 4}[where], done.stdout


# Finding 3: a build that found a check of identify's result under way left `again` with that check,
# which keeps its waiters for as long as it reads the app's object: the call, and what the tool
# returned, stayed alive past shutdown(), until the check ended.


def test_shutdown_lets_go_of_a_call_whose_build_waits_for_a_check_of_identify_s_result_still_under_way(monkeypatch: pytest.MonkeyPatch) -> None:
    held = held_call(monkeypatch)
    lsquare, events, recorder = recorded()
    first, go, entered = threading.Event(), threading.Event(), threading.Event()
    result = Counting({"orgId": "org-1", "userId": "user-a"}, first, go)

    def identify(given: IdentifyInput) -> Any:
        # Answers once the tool runs, so its settling, on this hook thread, makes the check.
        entered.wait(10)
        return result

    async def waiting(query: str) -> str:
        entered.set()
        assert await until(first.is_set)
        return "found"

    app = high()
    tool(app, waiting, name="search")
    instrument(app, lsquare, identify=identify, extras=OFF)
    loop = asyncio.new_event_loop()

    async def go_call() -> None:
        async with connect(app) as connection:
            await connection.call("search", {"query": "a"})

    try:
        loop.run_until_complete(go_call())
        # Replied; the check is still held in the app's getter, the build waiting for it.
        shut = lsquare.shutdown(timeout_ms=0)
        assert shut.failed == 1
        assert lsquare._finishing_calls == {}
        assert held and collected(*held)
        assert not go.is_set()
    finally:
        go.set()
        entered.set()
        loop.close()
    future: concurrent.futures.Future[None] = lsquare._lsquare_internal().delivered()
    concurrent.futures.wait([future], timeout=5)
    assert recorder.codes() == ["shutdown"]
    assert events.calls() == []


# Finding 4: the hand-off exposed shutdown()'s discard (through track()) before it filled what the
# loop and identify's settling hold: a shutdown() in between emptied nothing, and the hand-off then
# filled them, identify's future keeping the discarded call until identify settled.


def test_a_shutdown_as_soon_as_the_build_is_tracked_keeps_nothing_of_the_call_while_identify_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    held = held_call(monkeypatch)
    release = threading.Event()
    lsquare, events, recorder = recorded()
    internal = lsquare._lsquare_internal()
    original = internal.track
    tracked, shut = threading.Event(), threading.Event()
    results: list[Any] = []

    def tracking(*args: Any) -> None:
        original(*args)
        # Discard exposed: the hand-off goes on only once shutdown() has run, on another thread.
        tracked.set()
        shut.wait(10)

    internal.track = tracking  # type: ignore[method-assign,assignment]

    def shutting() -> None:
        tracked.wait(10)
        try:
            results.append(lsquare.shutdown(timeout_ms=0))
        finally:
            shut.set()

    threading.Thread(target=shutting, daemon=True).start()

    def identify(given: IdentifyInput) -> Any:
        del given
        release.wait(10)
        return {"userId": "u"}

    app = high()
    tool(app, search)
    instrument(app, lsquare, identify=identify, extras=OFF)
    loop = asyncio.new_event_loop()

    async def go() -> None:
        async with connect(app) as connection:
            await connection.call("search", {"query": "a"})

    try:
        loop.run_until_complete(go())
        assert shut.is_set() and results and results[0].failed == 1
        assert lsquare._finishing_calls == {}
        # identify still runs: nothing it, the loop or a timer holds keeps the call.
        assert held and collected(*held)
        assert not release.is_set()
    finally:
        release.set()
        tracked.set()
        shut.set()
        loop.close()
    future: concurrent.futures.Future[None] = lsquare._lsquare_internal().delivered()
    concurrent.futures.wait([future], timeout=5)
    assert recorder.codes() == ["shutdown"]
    assert events.calls() == []


# -- Codex's second review of these fixes, 1 November 2026 -----------------------------------------------------
#
# Finding 1 (the core's side is in hooks_fixes_test.py): once the main thread has returned, a server
# on a non-daemon thread still serves. Another thread's sync shutdown(), waiting for a call whose
# build awaits identify, set the exit deadline, and the exit's fallback then ran the call's
# before_send on the server's loop: copied and called there when no hook thread could take its
# start ("prepare"), what it returned read there when none could take that ("finalize").

EXIT_PHASE = """
import asyncio, concurrent.futures, json, sys, threading, types
from collections.abc import Mapping
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, sys.argv[1] + "/wrapper")
from lines import client, connect, high, tool
from lsquare.mcp import _call, instrument

where = sys.argv[2]
OFF = {"sessions": False, "intent": False, "missing_tools": False}
# identify settles only once shutdown() waits: the build waits for it, never its deadline.
_call.IDENTIFY_WAIT_S = 60.0
_call.LOOP_GRACE_S = 60.0
errors, reads, called = [], [], []
armed, refusing, ready, published, release = (threading.Event() for _ in range(5))
state = {}


class Reads(Mapping):
    # What before_send returns: notes the thread each read of it runs on.
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
    if event.get("conversationId") == "warm":
        raise RuntimeError("warm-up")
    called.append(threading.current_thread().name)
    if where == "finalize":
        refusing.set()
    return Reads(event)


class Quiet:
    def info(self, message): pass
    def warn(self, message): pass


lsquare, events = client(before_send=before_send, flush_timeout_ms=10_000, logger=Quiet(),
                         on_error=lambda error, event=None: errors.append([error.code, error.message]))
internal = lsquare._lsquare_internal()
# No hook thread takes a job once refusing: none runs, and the system refuses to start one (as
# 3.12.2 does by then anyway).
real_submit = lsquare._hooks.submit
lsquare._hooks.submit = lambda job: False if refusing.is_set() else real_submit(job)
real_wait = lsquare._wait_calls


def wait_calls(calls, timeout_ms):
    # shutdown() waits for the call, its exit deadline set; it goes on once the call's build is
    # done and its event's hook result published, so that hook settles while it waits.
    state["main_alive"] = threading.main_thread().is_alive()
    gate = concurrent.futures.Future()

    def check(_=None):
        if published.is_set() and all(call.done.done() for call in calls):
            try:
                gate.set_result(None)
            except concurrent.futures.InvalidStateError:
                pass

    state["check"] = check
    armed.set()
    for call in calls:
        call.done.add_done_callback(check)
    state["loop"].call_soon_threadsafe(state["waiting"].set_result, None)
    return real_wait([types.SimpleNamespace(done=gate)], timeout_ms)


lsquare._wait_calls = wait_calls
real_done = lsquare._hook_done


def hook_done(*args, **kwargs):
    try:
        return real_done(*args, **kwargs)
    finally:
        if armed.is_set():
            published.set()
            state["check"]()


lsquare._hook_done = hook_done


def identify(given):
    release.wait(30)
    return {"userId": "u"}


async def search(query: str) -> str:
    return "found"


app = high()
tool(app, search)
instrument(app, lsquare, identify=identify, extras=OFF)


async def serve_main():
    loop = asyncio.get_running_loop()
    state["loop"] = loop
    state["waiting"], state["ended"] = loop.create_future(), loop.create_future()
    # Before the main thread returns: a hook thread and the callback thread, idle from then on.
    lsquare.capture({"conversationId": "warm", "turnId": 0})
    await lsquare.aflush(timeout_ms=5_000)
    await asyncio.wrap_future(internal.delivered())
    errors.clear()
    async with connect(app) as connection:
        await connection.call("search", {"query": "a"})
        # Replied; its build waits for identify.
        ready.set()
        await state["waiting"]
        if where == "prepare":
            refusing.set()
        release.set()
        await state["ended"]


def shutting():
    ready.wait(30)
    # The main thread has returned; this non-daemon server still serves.
    threading.main_thread().join()
    result = lsquare.shutdown(timeout_ms=10_000)
    print(json.dumps({"main_alive": state.get("main_alive"), "called": called, "reads": reads, "errors": errors,
                      "sent": len(events.calls()), "failed": result.failed}), flush=True)
    state["loop"].call_soon_threadsafe(state["ended"].set_result, None)


threading.Thread(target=lambda: asyncio.run(serve_main()), name="server").start()
threading.Thread(target=shutting, name="shutting").start()
ready.wait(30)
"""


@pytest.mark.parametrize("where", ["prepare", "finalize"])
def test_a_sync_shutdown_waiting_for_a_call_once_the_main_thread_returned_never_has_its_hook_handled_on_the_server_s_loop(where: str) -> None:
    # Before: on every line, the hook was copied and called on the server's thread ("prepare"), and
    # what it returned read there (both), the call's event sent.
    tests = str(Path(__file__).resolve().parent.parent)
    done = subprocess.run([sys.executable, "-c", EXIT_PHASE, tests, where], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr[-6_000:]
    [line] = done.stdout.splitlines()
    out = json.loads(line)
    message = {
        "prepare": "No thread could start for before_send; the event was dropped.",
        "finalize": "No thread could start to read what before_send returned; the event was dropped.",
    }[where]
    assert out["main_alive"] is False
    assert out["reads"] == []
    assert out["called"] == ([] if where == "prepare" else ["server"])
    assert out["errors"] == [["hook_failed", message]]
    assert out["sent"] == 0


# Finding 2: a call handed off once the client had closed (its tool awaited ashutdown()) was
# registered all the same, after shutdown() had disposed of the calls it found. With identify's
# result still being checked (its getter held), its build found that check under way and armed
# nothing: the reply went out, but the call stayed among those being built, flush() counted it
# pending and it was kept, until the getter returned. Registered first, then checked: disposed of
# at once, its future done and the shutdown reported, with nothing armed for it.


@pytest.mark.anyio
async def test_a_call_handed_off_after_its_tool_shut_the_client_down_is_disposed_of_at_once_though_identify_s_check_is_held(monkeypatch: pytest.MonkeyPatch) -> None:
    held = held_call(monkeypatch)
    lsquare, events, recorder = recorded()
    first, go, entered = threading.Event(), threading.Event(), threading.Event()
    result = Counting({"orgId": "org-1", "userId": "user-a"}, first, go)

    def identify(given: IdentifyInput) -> Any:
        # Answers once the tool runs, so its settling, on this hook thread, makes the check.
        entered.wait(10)
        return result

    async def shutting(query: str) -> str:
        entered.set()
        # The check is held inside the app's getter: then the tool shuts the client down.
        assert await until(first.is_set)
        await lsquare.ashutdown(timeout_ms=0)
        return "found"

    app = high()
    tool(app, shutting, name="search")
    instrument(app, lsquare, identify=identify, extras=OFF)
    try:
        async with connect(app) as connection:
            await connection.call("search", {"query": "a"})
            # Replied, the getter still held: nothing of the call is kept, nor waited for.
            assert lsquare._finishing_calls == {}
            flushed = await lsquare.aflush(timeout_ms=0)
            assert flushed.pending == 0
            assert held and collected(*held)
            assert not go.is_set()
    finally:
        go.set()
        entered.set()
    await delivered(lsquare)
    assert recorder.codes() == ["shutdown"]
    assert events.calls() == []
