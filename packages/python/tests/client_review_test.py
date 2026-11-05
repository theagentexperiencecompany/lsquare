"""One test per finding of Codex's review of the client, 29 October 2026."""

from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import gc
import os
import subprocess
import sys
import textwrap
import threading
import time
import weakref
from typing import Any

import pytest

from lsquare import LSquare
from lsquare._client import CallScope
from lsquare._sending import Reply

from helpers import FakeTransport, Ingestion, Recorder, ack, eventually

KEY = "lsq_test_key"


def client(transport: FakeTransport | None = None, recorder: Recorder | None = None, **options: Any) -> LSquare:
    recorder = recorder or Recorder()
    options.setdefault("logger", recorder)
    options.setdefault("on_error", recorder.on_error)
    options.setdefault("_random", lambda: 0.0)
    return LSquare(api_key=KEY, endpoint="http://localhost:1", _transport=transport or FakeTransport(), **options)


def turn(n: int = 0, **extra: Any) -> dict[str, Any]:
    return {"conversationId": f"conv-{n}", "turnId": n, "question": "q", **extra}


def run(code: str, timeout: float = 20) -> subprocess.CompletedProcess[str]:
    # The environment's own Python, so the child imports the lsquare installed there: the wheel in CI.
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)], capture_output=True, text=True, timeout=timeout)


def test_1_an_abort_listener_that_waits_on_a_capture_cannot_deadlock_the_client() -> None:
    holder: list[LSquare] = []
    captured = threading.Event()

    def capture_and_say_so() -> None:
        holder[0].capture(turn(2))
        captured.set()

    def listener() -> None:
        worker = threading.Thread(target=capture_and_say_so)
        worker.start()
        worker.join(3)

    def before_send(event: dict[str, Any], signal: Any) -> Any:
        signal.add_listener(listener)
        signal.wait(5)
        return None

    lsquare = client(before_send=before_send, before_send_timeout_ms=100)
    holder.append(lsquare)
    lsquare.capture(turn(1))
    assert captured.wait(3)


def test_2_the_exit_flush_keeps_to_its_deadline_with_a_stalled_ingestion() -> None:
    server = Ingestion()
    stall = threading.Event()
    server.script.extend([lambda: stall.wait(10)] * 3)
    try:
        started = time.monotonic()
        done = run(
            f"""
            from lsquare import LSquare
            lsquare = LSquare(api_key="lsq_test", endpoint="{server.url}", flush_timeout_ms=300)
            lsquare.capture({{"conversationId": "c", "turnId": 1}})
            """
        )
        assert done.returncode == 0, done.stderr
        assert time.monotonic() - started < 5
    finally:
        stall.set()
        server.close()


def test_2_a_hook_that_never_returns_does_not_hold_the_exit() -> None:
    started = time.monotonic()
    done = run(
        """
        import threading
        from lsquare import LSquare
        lsquare = LSquare(api_key="lsq_test", endpoint="http://localhost:1", flush_timeout_ms=200,
                          before_send=lambda event: threading.Event().wait(60))
        lsquare.capture({"conversationId": "c", "turnId": 1})
        """
    )
    assert done.returncode == 0, done.stderr
    assert time.monotonic() - started < 5


def test_3_aflush_and_ashutdown_wait_on_trio() -> None:
    trio = pytest.importorskip("trio")
    gate = threading.Event()

    def respond(events: list[dict[str, Any]]) -> Reply:
        gate.wait(5)
        return Reply(200, None, ack(*[event["eventId"] for event in events]))

    transport = FakeTransport(respond)
    lsquare = client(transport)
    outcomes: list[Any] = []

    async def main() -> None:
        lsquare.capture(turn(1))
        await trio.sleep(0.05)
        threading.Timer(0.2, gate.set).start()
        outcomes.append(await lsquare.aflush(timeout_ms=3_000))
        lsquare.capture(turn(2))
        outcomes.append(await lsquare.ashutdown(timeout_ms=3_000))

    trio.run(main)
    assert outcomes[0].sent == 1
    assert outcomes[1].failed == 0
    assert len(transport.events) == 2


def test_3_an_async_before_send_captured_under_trio_is_refused_not_run_elsewhere() -> None:
    trio = pytest.importorskip("trio")
    recorder = Recorder()
    ran: list[bool] = []

    async def before_send(event: dict[str, Any], signal: Any) -> dict[str, Any]:
        ran.append(True)
        return event

    lsquare = client(recorder=recorder, before_send=before_send)

    async def main() -> None:
        lsquare.capture(turn())
        await trio.sleep(0.2)

    trio.run(main)
    assert recorder.wait_for(lambda: "hook_failed" in recorder.codes())
    assert ran == []
    assert recorder.wait_for(lambda: any("needs asyncio" in line for _, line in recorder.lines))


def test_3_async_callbacks_under_trio_run_on_the_private_loop_where_async_hooks_are_refused() -> None:
    # The exception to callbacks running as hooks do: a diagnostic isn't lost for want of asyncio.
    trio = pytest.importorskip("trio")
    ran: list[bool] = []
    seen: dict[str, tuple[asyncio.AbstractEventLoop, threading.Thread]] = {}
    done = threading.Event()

    def where(kind: str) -> None:
        seen[kind] = (asyncio.get_running_loop(), threading.current_thread())
        if len(seen) == 2:
            done.set()

    async def before_send(event: dict[str, Any], signal: Any) -> dict[str, Any]:
        ran.append(True)
        return event

    async def on_error(error: Any) -> None:
        if error.code == "hook_failed":
            where("on_error")

    class Logger:
        async def warn(self, message: str) -> None:
            if "needs asyncio" in message:
                where("logger")

    lsquare = client(before_send=before_send, on_error=on_error, logger=Logger())
    trio_thread: list[threading.Thread] = []

    async def main() -> None:
        trio_thread.append(threading.current_thread())
        lsquare.capture(turn())
        # Both run while trio does, so not once it has returned.
        assert await trio.to_thread.run_sync(done.wait, 2)

    try:
        trio.run(main)
        assert ran == []
        private = lsquare._private_loop.get()
        assert {kind: loop is private and thread is not trio_thread[0] for kind, (loop, thread) in seen.items()} == {"on_error": True, "logger": True}
    finally:
        lsquare.shutdown(timeout_ms=0)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="no fork here")
def test_4_a_child_forked_while_the_tls_lock_is_held_can_still_take_it() -> None:
    done = run(
        """
        import os
        from lsquare import _sending
        _sending._tls_lock.acquire()
        pid = os.fork()
        if pid == 0:
            os._exit(0 if _sending._tls_lock.acquire(timeout=1) else 3)
        _, status = os.waitpid(pid, 0)
        print(os.waitstatus_to_exitcode(status))
        """
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "0"


def test_5_a_shut_down_client_leaves_no_thread_and_can_be_collected() -> None:
    def lsquare_threads() -> int:
        return sum(1 for thread in threading.enumerate() if thread.name.startswith("lsquare-"))

    before = lsquare_threads()
    references = []
    for n in range(5):
        lsquare = LSquare(dry_run=True, logger=Recorder())
        lsquare.capture(turn(n))
        lsquare.shutdown()
        references.append(weakref.ref(lsquare))
        del lsquare
    assert eventually(lambda: lsquare_threads() <= before, timeout=5)
    gc.collect()
    assert eventually(lambda: (gc.collect(), all(reference() is None for reference in references))[1], timeout=5)


def test_6_a_cancelled_ashutdown_still_drops_what_is_left() -> None:
    started = threading.Event()
    aborted = threading.Event()

    def respond(events: list[dict[str, Any]]) -> Reply:
        started.set()
        time.sleep(1)
        return Reply(200, None, ack(*[event["eventId"] for event in events]))

    transport = FakeTransport(respond)
    lsquare = client(transport)

    async def main() -> None:
        lsquare.capture(turn())
        assert await asyncio.get_running_loop().run_in_executor(None, started.wait, 2)
        task = asyncio.ensure_future(lsquare.ashutdown(timeout_ms=5_000))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert lsquare._closed and not lsquare._live and not lsquare._waits
    del aborted


def test_7_model_and_dataclass_fields_are_read_only_when_visited() -> None:
    reads: list[str] = []
    names = [f"f{n}" for n in range(150)]
    Wide = dataclasses.make_dataclass("Wide", names)

    class Counted(Wide):  # type: ignore[valid-type,misc]
        def __getattribute__(self, name: str) -> Any:
            if name.startswith("f") and name[1:].isdigit():
                reads.append(name)
            return super().__getattribute__(name)

    transport = FakeTransport()
    lsquare = client(transport)
    lsquare.capture(turn(verification=Counted(*range(150))))
    lsquare.flush()
    assert eventually(lambda: len(transport.events) == 1)
    assert len(transport.events[0]["verification"]) == 100
    assert len(reads) <= 101


def test_8_a_call_recorded_from_a_worker_thread_keeps_its_loop() -> None:
    seen: list[Any] = []

    async def before_send(event: dict[str, Any], signal: Any) -> dict[str, Any]:
        seen.append(asyncio.get_running_loop())
        return event

    transport = FakeTransport()
    lsquare = client(transport, before_send=before_send)
    internal = lsquare._lsquare_internal()

    async def main() -> None:
        server_loop = asyncio.get_running_loop()
        internal.calls().set(CallScope(call_id="11111111-1111-4111-8111-111111111111", loop=server_loop))
        record = {
            "callId": "11111111-1111-4111-8111-111111111111", "capturedAt": "2026-10-29T00:00:00.000Z",
            "toolName": "t", "status": "completed", "durationMs": 1, "ids": {}, "diagnostics": False,
        }
        await asyncio.to_thread(internal.record, record)
        await lsquare.aflush()
        assert seen == [server_loop]

    asyncio.run(main())


def test_9_an_async_internal_callback_runs_outside_the_call() -> None:
    lsquare = client()
    internal = lsquare._lsquare_internal()

    async def callback() -> Any:
        await asyncio.sleep(0)
        return lsquare.current_call_id()

    async def main() -> None:
        internal.calls().set(CallScope(call_id="call-9"))
        assert await internal.callback(callback) is None
        assert lsquare.current_call_id() == "call-9"

    asyncio.run(main())


def test_10_an_error_callback_keeps_the_reporting_context_and_loop() -> None:
    tenant: contextvars.ContextVar[str] = contextvars.ContextVar("tenant", default="none")
    seen: list[Any] = []
    done = threading.Event()

    async def on_error(error: Any, event: Any = None) -> None:
        seen.append((error.code, tenant.get(), asyncio.get_running_loop()))
        done.set()

    lsquare = client(on_error=on_error)

    async def main() -> None:
        tenant.set("acme")
        lsquare.capture({"conversationId": "x" * 600, "turnId": 1})
        assert await asyncio.get_running_loop().run_in_executor(None, done.wait, 3)
        assert seen == [("invalid_id", "acme", asyncio.get_running_loop())]

    asyncio.run(main())


def test_11_concurrent_captures_make_one_quota_check_per_interval() -> None:
    now = [1_800_000_000_000.0]
    clock = [1_000.0]
    resets = "2027-01-15T09:00:00.000Z"

    def respond(events: list[dict[str, Any]]) -> Reply:
        return Reply(200, None, ack(rejected=[{"eventId": e["eventId"], "reason": "quota_exceeded", "resetsAt": resets} for e in events]))

    transport = FakeTransport(respond)
    recorder = Recorder()
    lsquare = client(transport, recorder, _now=lambda: now[0], _clock=lambda: clock[0] + time.monotonic() * 0)
    lsquare.capture(turn(0))
    lsquare.flush()
    assert recorder.wait_for(lambda: "quota_exceeded" in recorder.codes())
    clock[0] += 16 * 60  # past the 15-minute check interval
    barrier = threading.Barrier(8)

    def work(n: int) -> None:
        barrier.wait()
        lsquare.capture(turn(n))

    threads = [threading.Thread(target=work, args=(n,)) for n in range(1, 9)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    lsquare.flush(timeout_ms=1_000)
    assert len(transport.requests) == 2


# -- Codex's second round ------------------------------------------------------------------------


def test_r2_1_a_trickling_reply_cannot_outlast_the_deadline_without_the_watchdog() -> None:
    import socket as _socket

    from lsquare._sending import Abort, Exchange, NetworkError, Resolver
    from lsquare._threads import start_thread

    listener = _socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    stop = threading.Event()

    def trickle() -> None:
        connection, _ = listener.accept()
        connection.recv(65536)
        for byte in b"HTTP/1.1 200 OK\r\nx-slow: " + b"a" * 200:
            if stop.is_set():
                break
            try:
                connection.send(bytes([byte]))
            except OSError:
                break
            time.sleep(0.02)
        connection.close()

    threading.Thread(target=trickle, daemon=True).start()
    # A scheduler that never runs anything: no watchdog.
    exchange = Exchange(f"http://127.0.0.1:{port}/v1/turns", KEY, "ua", Resolver(start_thread), lambda delay, fn: (lambda: None))
    started = time.monotonic()
    with pytest.raises(NetworkError):
        exchange.post(b'{"events":[]}', 0.3, Abort())
    assert time.monotonic() - started < 1.0
    stop.set()
    listener.close()


def test_r2_2_ashutdown_cancelled_while_waiting_for_calls_still_closes() -> None:
    import concurrent.futures

    lsquare = client()
    lsquare._lsquare_internal().track("call-r2", concurrent.futures.Future())

    async def main() -> None:
        task = asyncio.ensure_future(lsquare.ashutdown(timeout_ms=5_000))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert lsquare._closed


def test_r2_3_shutdown_lets_a_hook_on_the_private_loop_finish_its_cleanup() -> None:
    released = threading.Event()

    async def before_send(event: dict[str, Any], signal: Any) -> Any:
        try:
            await asyncio.sleep(10)
        finally:
            await asyncio.sleep(0.04)
            released.set()
        return event

    lsquare = client(before_send=before_send)
    lsquare.capture(turn())
    time.sleep(0.1)
    lsquare.shutdown(timeout_ms=100)
    assert released.wait(3)


def test_r2_4_an_abort_listener_that_captures_cannot_feed_itself() -> None:
    holder: list[LSquare] = []
    runs: list[int] = []

    def before_send(event: dict[str, Any], signal: Any) -> Any:
        runs.append(1)
        signal.add_listener(lambda: holder[0].capture(turn(len(runs))))
        signal.wait(1)
        return None

    lsquare = client(before_send=before_send, before_send_timeout_ms=50)
    holder.append(lsquare)
    lsquare.capture(turn())
    time.sleep(1.5)
    # The first hook, and at most a couple more from its listeners before the depth limit.
    assert len(runs) <= 3


def test_r2_5_pending_after_flush_runs_on_the_capturing_loop() -> None:
    gate = threading.Event()
    seen: list[Any] = []
    done = threading.Event()

    def respond(events: list[dict[str, Any]]) -> Reply:
        gate.wait(5)
        return Reply(200, None, ack(*[event["eventId"] for event in events]))

    async def on_error(error: Any, event: Any = None) -> None:
        if error.code == "pending_after_flush":
            seen.append(asyncio.get_running_loop())
            done.set()

    lsquare = client(FakeTransport(respond), on_error=on_error)

    async def main() -> None:
        lsquare.capture(turn())
        await asyncio.to_thread(lsquare.flush, 100)
        assert await asyncio.get_running_loop().run_in_executor(None, done.wait, 3)
        assert seen == [asyncio.get_running_loop()]
        gate.set()

    asyncio.run(main())


@pytest.mark.parametrize("handler", ["on_error", "logger"])
@pytest.mark.parametrize("state", ["stopped", "closed"])
def test_r3_4_an_async_callback_whose_origin_loop_has_stopped_or_closed_runs_on_the_private_loop(state: str, handler: str) -> None:
    # Codex's third review: on its origin's loop only while that loop runs. The report about an
    # event captured on a loop that has since stopped, or closed, still runs, on the private loop.
    gate = threading.Event()
    seen: list[asyncio.AbstractEventLoop] = []
    done = threading.Event()

    def respond(events: list[dict[str, Any]]) -> Reply:
        gate.wait(5)
        return Reply(200, None, ack(*[event["eventId"] for event in events]))

    async def where() -> None:
        seen.append(asyncio.get_running_loop())
        done.set()

    async def on_error(error: Any, event: Any = None) -> None:
        if error.code == "pending_after_flush":
            await where()

    class Logger:
        def info(self, message: str) -> None:
            pass

        async def warn(self, message: str) -> None:
            if "pending_after_flush" in message:
                await where()

    # Without on_error, the report goes to the logger.
    options: dict[str, Any] = {"on_error": on_error} if handler == "on_error" else {"on_error": None, "logger": Logger()}
    lsquare = client(FakeTransport(respond), **options)
    loop = asyncio.new_event_loop()

    async def capture() -> None:
        lsquare.capture(turn())

    try:
        loop.run_until_complete(capture())
        if state == "closed":
            loop.close()
        lsquare.flush(timeout_ms=100)
        assert done.wait(3)
        assert seen == [lsquare._private_loop.get()]
    finally:
        gate.set()
        lsquare.shutdown(timeout_ms=0)
        loop.close()
