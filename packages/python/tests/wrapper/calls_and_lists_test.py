"""Regression tests for the deep review's TypeScript findings on calls, identify and low-level
snapshots, as they hold in Python too (31 October): an identify settling past its deadline while its
timer is held up (ts-mcp-5), or while the wrapper reads how the call ended (found reviewing these
fixes), flush_timeout_ms=0 with a flush() inside the tool (ts-core-28), and waits for a low-level
server's snapshot kept until the list next changes (ts-mcp-16, py-mcp-6), or for as long as a
listing that ignores its cancellation runs (found reviewing these fixes). Ported from
packages/lsquare/test/mcp-calls-and-lists.test.ts; the tests of the read of how the call ended
are Python's own, where a sync identify settles on a thread of its own while that read runs."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import gc
import sys
import threading
import time
import weakref
from collections.abc import Callable
from typing import Any, cast

import pytest

from helpers import Recorder, acollected
from lines import client, connect, high, low, needs_line, only, tool
from lsquare.mcp import IdentifyInput, _call, _lists, instrument

pytestmark = [needs_line, pytest.mark.anyio]
OFF = {"sessions": False, "intent": False, "missing_tools": False}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def search(query: str) -> str:
    return f"found {query}"


async def test_an_identify_that_settles_past_its_deadline_is_refused_though_its_timer_has_not_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_call, "IDENTIFY_WAIT_S", 0.2)
    lsquare, events = client()
    answer = threading.Event()
    returned: list[float] = []

    def identify(call: IdentifyInput) -> Any:
        answer.wait(5)
        returned.append(time.monotonic())
        return {"orgId": "org-1", "userId": "u-1"}

    app = high()
    tool(app, search)
    instrument(app, lsquare, extras=OFF, identify=identify)
    # The scheduler, which runs identify's deadline timer, is held up (a step stuck behind a lock,
    # say) from just after the tool returns until identify has answered.
    gate = threading.Event()

    def held() -> None:
        gate.wait(5)

    async with connect(app) as connection:
        await connection.call("search", {"query": "a"})
        replied = time.monotonic()
        lsquare._scheduler.call_soon(held)
        await asyncio.sleep(0.5)  # past the deadline, 0.2 s after the tool returned
        answer.set()  # identify answers late, before its timer could run
        for _ in range(200):
            if returned:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        gate.set()
    await lsquare.aflush(timeout_ms=5_000)
    [event] = events.calls()
    assert returned and returned[0] - replied > 0.2
    assert "userId" not in event and event.get("identity") == "failed", event
    # What it returned teaches nothing: the ID fields last known stay as they were.
    assert lsquare._lsquare_internal().identified.get("keys") is None
    await lsquare.ashutdown()


class _Judged:
    """Holds one of the wrapper's reads of the app's code as the call ends (`hold()`): past
    identify's allowance, identify answers, and the read goes on once identify's own settling has
    judged that answer on time or late, on identify's thread."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_call, "IDENTIFY_WAIT_S", 0.1)
        self.answer = threading.Event()
        self.decided = threading.Event()
        self.held = 0
        real = _call.on_time

        def on_time(identify: Any) -> bool:
            try:
                return real(identify)
            finally:
                self.decided.set()

        monkeypatch.setattr(_call, "on_time", on_time)

    def identify(self, call: IdentifyInput) -> Any:
        self.answer.wait(5)
        return {"orgId": "org-1", "userId": "u-1"}

    def hold(self) -> None:
        self.held += 1
        time.sleep(0.3)  # past the allowance: the tool returned before this read started
        self.answer.set()
        assert self.decided.wait(5)

    def check(self, lsquare: Any, events: Any) -> None:
        assert self.held == 1
        [event] = events.calls()
        assert "userId" not in event and event.get("identity") == "failed", event
        # What it returned teaches nothing: the ID fields last known stay as they were.
        assert lsquare._lsquare_internal().identified.get("keys") is None


# identify's deadline was set only once the wrapper had read how the call ended, which runs the
# app's code: while that blocked, an identify settling past its allowance found no deadline yet,
# and counted as on time.
async def test_an_identify_that_settles_past_its_deadline_while_the_tools_error_is_read_is_late(monkeypatch: pytest.MonkeyPatch) -> None:
    judged = _Judged(monkeypatch)
    lsquare, events = client()
    calls = lsquare._lsquare_internal().calls()

    class Refused(Exception):
        def __str__(self) -> str:
            # The SDK reads it too, inside the call, as it turns the raise into an error result:
            # only the wrapper's read, outside the call once the tool has returned, is held.
            if calls.get() is None and not judged.held:
                judged.hold()
            return "refused"

    async def refuse(query: str) -> str:
        raise Refused()

    app = high()
    tool(app, refuse)
    instrument(app, lsquare, extras=OFF, identify=judged.identify)
    async with connect(app) as connection:
        reply = await connection.call("refuse", {"query": "a"})
    await lsquare.aflush(timeout_ms=5_000)
    assert reply.is_error
    judged.check(lsquare, events)
    await lsquare.ashutdown()


# The same with a low-level handler's own result, which the mcp 1.x lines' wrapper reads as it is.
@only("mcp1", "fastmcp3")
async def test_an_identify_that_settles_past_its_deadline_while_the_result_is_read_is_late(monkeypatch: pytest.MonkeyPatch) -> None:
    import mcp.types as types

    judged = _Judged(monkeypatch)
    lsquare, events = client()

    class Slow(dict[str, Any]):
        """A result whose `resultType`, the first field the wrapper reads, is held."""

        def get(self, key: str, default: Any = None) -> Any:
            if key == "resultType" and not judged.held:
                judged.hold()
            return super().get(key, default)

        def model_dump(self, **options: Any) -> dict[str, Any]:
            return dict(self)

    async def unused(name: str, arguments: dict[str, Any]) -> Any:
        return []

    app = low("test", [{"name": "t", "inputSchema": {"type": "object"}}], unused)

    async def handler(request: Any) -> Any:
        return Slow(content=[{"type": "text", "text": "ok"}], isError=False)

    app.request_handlers[types.CallToolRequest] = handler
    instrument(app, lsquare, extras=OFF, identify=judged.identify)
    async with connect(app) as connection:
        reply = await connection.call("t", {})
    await lsquare.aflush(timeout_ms=5_000)
    assert reply.texts == ["ok"]
    judged.check(lsquare, events)
    await lsquare.ashutdown()


WAYS = ["without wait_until", "with wait_until", "with flush_before_reply"]


def _noted(lsquare: Any, how: str, identify: Callable[..., Any]) -> tuple[Any, list[Any]]:
    """A high-level server whose tool captures, then flushes, a common pattern: the
    reply then waits for the call's sending, which flush_timeout_ms=0 makes no wait at all."""
    app = high()

    async def noted(query: str) -> str:
        lsquare.capture({"question": f"find {query}"})
        await lsquare.aflush()
        return f"noted {query}"

    tool(app, noted)
    handed: list[Any] = []
    options: dict[str, Any] = {"extras": OFF, "identify": identify}
    if how == "with wait_until":
        options["wait_until"] = handed.append
    if how == "with flush_before_reply":
        options["flush_before_reply"] = True
    instrument(app, lsquare, **options)
    return app, handed


# 0 waits for nothing: the reply doesn't wait for identify, and identify keeps its second.
@pytest.mark.parametrize("how", WAYS)
async def test_flush_inside_the_tool_with_flush_timeout_ms_0_still_gives_identify_its_second(how: str) -> None:
    recorder = Recorder()
    lsquare, events = client(flush_timeout_ms=0, on_error=recorder.on_error)
    answer: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

    async def identify(call: IdentifyInput) -> Any:
        return await answer

    app, handed = _noted(lsquare, how, identify)
    async with connect(app) as connection:
        reply = await connection.call("noted", {"query": "a"})
    # The reply came while identify still ran; it answers now, well within its second.
    assert reply.texts == ["noted a"] and not answer.done()
    answer.set_result({"userId": "u-1"})
    for awaitable in handed:
        await awaitable
    assert len(handed) == (1 if how == "with wait_until" else 0)
    await lsquare.aflush(timeout_ms=5_000)
    [event] = events.calls()
    assert event.get("userId") == "u-1", event
    assert event["agent"]["question"] == "find a"
    assert recorder.errors == []
    await lsquare.ashutdown()


# The other half: its second, and no more. Past it, the call goes without it, at its deadline
# rather than wait_until's 30 seconds.
@pytest.mark.parametrize("how", WAYS)
async def test_flush_inside_the_tool_with_flush_timeout_ms_0_holds_identify_to_its_second(monkeypatch: pytest.MonkeyPatch, how: str) -> None:
    monkeypatch.setattr(_call, "IDENTIFY_WAIT_S", 0.2)
    recorder = Recorder()
    lsquare, events = client(flush_timeout_ms=0, on_error=recorder.on_error)
    answer: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

    async def identify(call: IdentifyInput) -> Any:
        return await answer

    app, handed = _noted(lsquare, how, identify)
    try:
        async with connect(app) as connection:
            reply = await connection.call("noted", {"query": "a"})
        assert reply.texts == ["noted a"]
        for awaitable in handed:
            await asyncio.wait_for(awaitable, 5)
        await lsquare.aflush(timeout_ms=5_000)
        [event] = events.calls()
        assert "userId" not in event and event.get("identity") == "failed", event
        assert recorder.wait_for(lambda: [error.message for error in recorder.errors] == ["identify took longer than its deadline; the call was sent as limited diagnostics."], 2)
    finally:
        if not answer.done():
            answer.set_result(None)
    await lsquare.ashutdown()


def _futures() -> int:
    """Futures alive now, after a full collection: what a wait leaves behind, counted exactly."""
    gc.collect()
    return sum(1 for item in gc.get_objects() if isinstance(item, concurrent.futures.Future))


def _reader(page: Any) -> Any:
    return lambda: _lists.Reader(page, contextvars.copy_context())


# Each wait once added a callback to a future the instance kept until the list next changed: one a
# list that fails, or keeps timing out, and never sends list_changed may never have.
async def test_waits_keep_nothing_once_they_end_reads_failing_on_and_on() -> None:
    lsquare, _ = client()
    internal = lsquare._lsquare_internal()
    lists = _lists.Lists()

    async def failing(cursor: str | None) -> Any:
        raise RuntimeError("down")

    async def waits(count: int) -> int:
        woken = 0
        for _ in range(count):
            woken += await _lists.wait_once(lists, internal, _reader(failing), _lists.Wait())
            # The pause lifted, as by its minute passing.
            lists.paused_until = 0.0
        return woken

    await waits(1)
    before = _futures()
    assert await waits(500) == 500
    await asyncio.sleep(0.05)
    assert _futures() - before < 50
    await lsquare.ashutdown()


async def test_waits_keep_nothing_once_they_end_reads_timing_out_with_the_list_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_lists, "WAIT_S", 0.05)
    monkeypatch.setattr(_lists, "GIVE_UP_S", 0.2)
    lsquare, _ = client()
    internal = lsquare._lsquare_internal()
    lists = _lists.Lists()

    async def hanging(cursor: str | None) -> Any:
        await asyncio.sleep(3600)

    # Calls in the first second (here 0.05 s) of a read that hangs: none is woken. The read is
    # given up at its deadline, its task cancelled, and its pause lifted.
    async def cycle() -> list[bool]:
        woken = await asyncio.gather(*(_lists.wait_once(lists, internal, _reader(hanging), _lists.Wait()) for _ in range(250)))
        for _ in range(100):
            if lists.reading is None and not lists.tasks:
                break
            await asyncio.sleep(0.01)
        lists.paused_until = 0.0
        return list(woken)

    before = _futures()
    for _ in range(2):
        assert set(await cycle()) == {False}
    await asyncio.sleep(0.05)
    assert _futures() - before < 50
    await lsquare.ashutdown()


class _Watched:
    """The client's internals as the snapshot's waits see them, keeping a weak reference to each
    future a wait waits on, and holding each read's give-up until the test runs it."""

    def __init__(self, internal: Any) -> None:
        self.internal = internal
        self.waited: list[weakref.ref[concurrent.futures.Future[None]]] = []
        self.give_ups: list[Callable[[], None]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.internal, name)

    async def wait(self, future: concurrent.futures.Future[None], timeout_s: float) -> None:
        self.waited.append(weakref.ref(future))
        await self.internal.wait(future, timeout_s)

    def timer(self, delay_s: float, fn: Callable[[], None]) -> Callable[[], None]:
        self.give_ups.append(fn)
        return lambda: None


# A read kept each wait's waker until it was over, and after: a listing that won't stop when the
# read is given up keeps the read, so every wait that ever ended on it stayed alive with it.
async def test_waits_on_a_read_whose_listing_ignores_its_cancellation_keep_nothing_once_they_end(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_lists, "WAIT_S", 0.05)
    lsquare, _ = client()
    watched = _Watched(lsquare._lsquare_internal())
    lists = _lists.Lists()
    release = asyncio.Event()
    cancelled: list[int] = []

    async def stubborn(cursor: str | None) -> Any:
        while True:
            try:
                await release.wait()
                break
            except asyncio.CancelledError:
                cancelled.append(1)  # ignored: the listing runs on
        raise RuntimeError("let go")

    def waits(count: int) -> Any:
        return asyncio.gather(*(_lists.wait_once(lists, cast(Any, watched), _reader(stubborn), _lists.Wait()) for _ in range(count)))

    try:
        # 100 calls in the first second (here 0.05 s) of the read: each times out, none woken.
        assert set(await waits(100)) == {False}
        reading = lists.reading
        assert reading is not None and not reading.over and len(watched.waited) == 100
        assert await acollected(*watched.waited)
        # 100 more, with all the time they need, are woken as the read is given up, its task asked
        # to cancel, which it ignores.
        monkeypatch.setattr(_lists, "WAIT_S", 30.0)
        waiting = waits(100)
        for _ in range(500):
            if len(watched.waited) == 200:
                break
            await asyncio.sleep(0.01)
        [give_up] = watched.give_ups
        give_up()
        assert set(await asyncio.wait_for(waiting, 5)) == {True}
        for _ in range(500):
            if cancelled:
                break
            await asyncio.sleep(0.01)
        assert reading.expired and cancelled and not lists.waiting
        assert await acollected(*watched.waited)
        assert lists.tasks, "the listing still runs"
    finally:
        release.set()
    for _ in range(500):
        if not lists.tasks:
            break
        await asyncio.sleep(0.01)
    assert not lists.tasks
    await lsquare.ashutdown()


async def test_a_wait_still_wakes_when_the_list_changes_meanwhile() -> None:
    lsquare, _ = client()
    internal = lsquare._lsquare_internal()
    lists = _lists.Lists()

    async def hanging(cursor: str | None) -> Any:
        await asyncio.sleep(3600)

    waiting = asyncio.ensure_future(_lists.wait_once(lists, internal, _reader(hanging), _lists.Wait()))
    await asyncio.sleep(0.05)
    _lists.mark(lists, True)
    assert await asyncio.wait_for(waiting, 1) is True
    await lsquare.ashutdown()


# The control: a wait whose read is over before the wait waits on it is woken all the same.
async def test_a_wait_on_a_read_already_over_as_it_starts_waiting_returns_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_lists, "WAIT_S", 30.0)
    # The read can't start (no loop it knows of): it fails, and is over, before the wait waits.
    monkeypatch.setattr(_lists, "_asyncio_loop", lambda: None)
    monkeypatch.delitem(sys.modules, "trio", raising=False)
    lsquare, _ = client()
    lists = _lists.Lists()

    async def unused(cursor: str | None) -> Any:
        return {"tools": []}

    wait = _lists.Wait()
    assert await asyncio.wait_for(_lists.wait_once(lists, lsquare._lsquare_internal(), _reader(unused), wait), 5) is True
    [reading] = wait.started
    assert reading.over and lists.paused_until > 0
    await lsquare.ashutdown()
