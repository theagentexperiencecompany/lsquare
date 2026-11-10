"""The runtime and parity fixes of the deep review's last Python findings, 31 October 2026: one
section per finding, each test failing before its fix unless it says it's a guard. The subprocess
tests are in runtime_process_fixes_test.py; TypeScript's side is in
packages/lsquare/test/runtime-fixes.test.ts. No sleep orders anything here: events, held
transports, an injected clock and the scheduler's own wake do."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import random
import shutil
import socket
import subprocess
import threading
from collections.abc import Callable, Coroutine, Iterator, Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from lsquare import LSquare, _client, _url
from lsquare._client import DEFAULTS, RETRY_AFTER_CAP_MS, CallScope, FlushResult
from lsquare._sending import Abort, Exchange, NetworkError, Reply, Resolver
from lsquare._threads import Interrupted, start_thread

from helpers import FakeTransport, Held, Ingestion, Recorder, ack, eventually

REPO = Path(__file__).resolve().parents[3]
KEY = "lsq_test_key"


def client(transport: Any = None, recorder: Recorder | None = None, **options: Any) -> LSquare:
    recorder = recorder or Recorder()
    options.setdefault("logger", recorder)
    options.setdefault("on_error", recorder.on_error)
    options.setdefault("_random", lambda: 0.0)
    return LSquare(api_key=KEY, endpoint="http://localhost:1", _transport=transport or FakeTransport(), **options)


def turn(n: int = 0, **extra: Any) -> dict[str, Any]:
    return {"conversationId": f"conv-{n}", "turnId": n, "question": f"question {n}", **extra}


# -- py-runtime-4: an app callback's SystemExit or KeyboardInterrupt off the main thread ----------------
#
# A failure like any other there: asyncio raises those two out of the loop a task runs on, so the
# SDK's task raises an ordinary exception in their place, and the loop goes on.


def on_a_thread_of_its_own(main: Callable[[], Coroutine[Any, Any, Any]]) -> tuple[str, Any]:
    """`asyncio.run(main())` on a thread of its own: ("returned", what it returned), or ("raised",
    the type of what came out of asyncio.run), never raised here."""
    out: list[tuple[str, Any]] = []

    def run() -> None:
        try:
            out.append(("returned", asyncio.run(main())))
        except BaseException as error:  # noqa: BLE001 - the outcome under test
            out.append(("raised", type(error)))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(10)
    assert out, "the loop's thread is still running"
    return out[0]


@pytest.mark.parametrize("interruption", [SystemExit, KeyboardInterrupt])
def test_an_async_hook_s_interruption_on_an_app_s_loop_off_the_main_thread_fails_the_event_and_the_loop_goes_on(
    interruption: type[BaseException],
) -> None:
    recorder = Recorder()

    async def before_send(event: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0)
        raise interruption

    lsquare = client(recorder=recorder, before_send=before_send)

    async def main() -> FlushResult:
        lsquare.capture(turn(1))
        return await lsquare.aflush(timeout_ms=5_000)

    outcome, result = on_a_thread_of_its_own(main)
    assert outcome == "returned", result
    assert (result.sent, result.failed) == (0, 1)
    assert recorder.wait_for(lambda: recorder.codes() == ["hook_failed"])
    lsquare.shutdown(timeout_ms=0)


@pytest.mark.parametrize("interruption", [SystemExit, KeyboardInterrupt])
def test_an_async_on_error_s_interruption_on_an_app_s_loop_off_the_main_thread_settles_it_and_the_loop_goes_on(
    interruption: type[BaseException],
) -> None:
    started: list[str] = []

    async def on_error(error: Any) -> None:
        started.append(error.code)
        await asyncio.sleep(0)
        raise interruption

    lsquare = client(on_error=on_error)

    async def main() -> str:
        # Reported from this loop, which runs the async on_error: aflush() returns once it started.
        lsquare.capture(cast(Any, ["not", "a", "mapping"]))
        await lsquare.aflush(timeout_ms=5_000)
        running = list(lsquare._callback_futures)
        if running:
            await asyncio.wait([asyncio.wrap_future(future) for future in running], timeout=5)
        return "settled" if not lsquare._callback_futures else "pending"

    assert on_a_thread_of_its_own(main) == ("returned", "settled")
    assert started == ["invalid_input"]
    assert lsquare._report_limit.held == 0
    lsquare.shutdown(timeout_ms=0)


@pytest.mark.parametrize("interruption", [SystemExit, KeyboardInterrupt])
@pytest.mark.parametrize("callback", ["before_send", "on_error"])
def test_an_async_callback_s_interruption_on_the_main_thread_s_loop_still_goes_on_through(
    callback: str, interruption: type[BaseException]
) -> None:
    # A guard: there a signal handler's can't be told from the app's, so it is passed on, as before.
    async def raising(*args: Any) -> Any:
        await asyncio.sleep(0)
        raise interruption

    options: dict[str, Any] = {callback: raising}
    lsquare = client(**options)

    async def main() -> None:
        lsquare.capture(turn(1) if callback == "before_send" else cast(Any, ["not", "a", "mapping"]))
        await lsquare.aflush(timeout_ms=5_000)
        running = list(lsquare._callback_futures)
        if running:
            await asyncio.wait([asyncio.wrap_future(future) for future in running], timeout=5)

    with pytest.raises(interruption):
        asyncio.run(main())
    lsquare.shutdown(timeout_ms=0)


@pytest.fixture
def thread_deaths(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, type[BaseException]]]:
    """The SDK threads that ended on an exception during the test, with its type."""
    died: list[tuple[str, type[BaseException]]] = []

    def hook(args: Any) -> None:
        if args.thread is not None and args.thread.name.startswith("lsquare-"):
            died.append((args.thread.name, args.exc_type))

    monkeypatch.setattr(threading, "excepthook", hook)
    return died


@pytest.mark.parametrize("interruption", [SystemExit, KeyboardInterrupt])
def test_an_async_hook_s_interruption_on_the_private_loop_fails_the_event_and_that_loop_runs_the_next_hook(
    interruption: type[BaseException], thread_deaths: list[tuple[str, type[BaseException]]]
) -> None:
    loops: list[asyncio.AbstractEventLoop] = []
    threads: list[threading.Thread] = []

    async def before_send(event: dict[str, Any]) -> dict[str, Any]:
        loops.append(asyncio.get_running_loop())
        threads.append(threading.current_thread())
        if event["question"] == "question 1":
            raise interruption
        return event

    recorder = Recorder()
    transport = FakeTransport()
    # Captured outside any loop: the hook runs on the private one.
    lsquare = client(transport, recorder, before_send=before_send)
    lsquare.capture(turn(1))
    assert recorder.wait_for(lambda: recorder.codes() == ["hook_failed"])
    lsquare.capture(turn(2))
    lsquare.flush(timeout_ms=5_000)
    assert eventually(lambda: [event["question"] for event in transport.events] == ["question 2"])
    lsquare.shutdown(timeout_ms=1_000)
    for thread in threads:
        thread.join(5)
    # Before, the first hook's interruption ended the loop's thread, and the next hook got a new one.
    assert thread_deaths == []
    assert len(loops) == 2 and loops[0] is loops[1]


@pytest.mark.parametrize("interruption", [SystemExit, KeyboardInterrupt])
def test_an_async_on_error_s_interruption_on_the_private_loop_settles_it_and_leaves_that_loop_running(
    interruption: type[BaseException], thread_deaths: list[tuple[str, type[BaseException]]]
) -> None:
    threads: list[threading.Thread] = []

    async def on_error(error: Any) -> None:
        threads.append(threading.current_thread())
        await asyncio.sleep(0)
        raise interruption

    lsquare = client(on_error=on_error)
    # Reported outside any loop: the on_error runs on the private one.
    lsquare.capture(cast(Any, ["not", "a", "mapping"]))
    lsquare.flush(timeout_ms=1_000)
    assert eventually(lambda: bool(threads))
    concurrent.futures.wait(list(lsquare._callback_futures), timeout=5)
    assert eventually(lambda: lsquare._report_limit.held == 0)
    current = lsquare._private_loop._current
    assert current is not None and not current[0].is_closed()
    lsquare.shutdown(timeout_ms=1_000)
    threads[0].join(5)
    assert thread_deaths == []


# The same for an async `identify` and an async logger (checker's fixes, 31 October 2026): each goes
# through the SDK's task as a hook or an on_error does, and is pinned on its own, so a later path of
# its own that doesn't contain the two shows here.


class Interrupting:
    """A logger whose async `warn` raises `interruption` once it has started."""

    def __init__(self, interruption: type[BaseException]) -> None:
        self.interruption = interruption
        self.started: list[tuple[str, threading.Thread]] = []

    def info(self, message: str) -> None:
        pass

    async def warn(self, message: str) -> None:
        self.started.append((message, threading.current_thread()))
        await asyncio.sleep(0)
        raise self.interruption


def identify_once(lsquare: LSquare, hook: Callable[[Any], Any]) -> _client.HookRun:
    """An `identify` started as the wrapper starts one, from here: on this thread's loop, if any."""
    internal = lsquare._lsquare_internal()
    run = internal.identify(hook, lambda signal: {"toolName": "ask"}, internal.origin())
    assert run is not None
    return run


@pytest.mark.parametrize("interruption", [SystemExit, KeyboardInterrupt])
def test_an_async_identify_s_interruption_on_an_app_s_loop_off_the_main_thread_fails_its_run_and_the_loop_goes_on(
    interruption: type[BaseException], thread_deaths: list[tuple[str, type[BaseException]]]
) -> None:
    lsquare = client()

    async def identify(hook_input: Any) -> Any:
        await asyncio.sleep(0)
        raise interruption

    async def main() -> tuple[str, str]:
        # Started from this loop: the hook runs on it.
        run = identify_once(lsquare, identify)
        try:
            await asyncio.wait_for(asyncio.wrap_future(run.future), 5)
        except Exception as error:  # Interrupted: ordinary, here
            return type(error).__name__, type(error.__cause__).__name__
        return "returned", ""

    assert on_a_thread_of_its_own(main) == ("returned", ("Interrupted", interruption.__name__))
    assert lsquare._identifying["active"] == 0
    lsquare.shutdown(timeout_ms=0)
    assert thread_deaths == []


@pytest.mark.parametrize("interruption", [SystemExit, KeyboardInterrupt])
def test_an_async_identify_s_interruption_on_the_private_loop_fails_its_run_and_that_loop_runs_the_next_one(
    interruption: type[BaseException], thread_deaths: list[tuple[str, type[BaseException]]]
) -> None:
    loops: list[asyncio.AbstractEventLoop] = []
    threads: list[threading.Thread] = []

    async def identify(hook_input: Any) -> Any:
        loops.append(asyncio.get_running_loop())
        threads.append(threading.current_thread())
        if len(loops) == 1:
            raise interruption
        return {"userId": "u"}

    lsquare = client()
    # Started outside any loop: the hook runs on the private one.
    first = identify_once(lsquare, identify)
    # Read without raising: before, the run's future held the interruption itself, which result()
    # would raise on this, the main, thread.
    error = first.future.exception(timeout=5)
    assert isinstance(error, Interrupted) and isinstance(error.__cause__, interruption)
    assert identify_once(lsquare, identify).future.result(timeout=5) == {"userId": "u"}
    assert lsquare._identifying["active"] == 0
    lsquare.shutdown(timeout_ms=1_000)
    threads[0].join(5)
    # Before, the first's interruption ended the loop's thread, and the second got a new one.
    assert thread_deaths == []
    assert len(loops) == 2 and loops[0] is loops[1]


@pytest.mark.parametrize("interruption", [SystemExit, KeyboardInterrupt])
def test_an_async_logger_s_interruption_on_an_app_s_loop_off_the_main_thread_settles_it_and_the_loop_goes_on(
    interruption: type[BaseException], thread_deaths: list[tuple[str, type[BaseException]]]
) -> None:
    logger = Interrupting(interruption)
    # No on_error: the report is logged, by the async logger.
    lsquare = client(logger=logger, on_error=None)

    async def main() -> str:
        # Reported from this loop, which runs the logger's warn: aflush() returns once it started.
        lsquare.capture(cast(Any, ["not", "a", "mapping"]))
        await lsquare.aflush(timeout_ms=5_000)
        running = list(lsquare._callback_futures)
        if running:
            await asyncio.wait([asyncio.wrap_future(future) for future in running], timeout=5)
        return "settled" if not lsquare._callback_futures else "pending"

    assert on_a_thread_of_its_own(main) == ("returned", "settled")
    assert [message.split(":")[0] for message, _ in logger.started] == ["[lsquare] invalid_input"]
    assert lsquare._report_limit.held == 0
    lsquare.shutdown(timeout_ms=0)
    assert thread_deaths == []


@pytest.mark.parametrize("interruption", [SystemExit, KeyboardInterrupt])
def test_an_async_logger_s_interruption_on_the_private_loop_settles_it_and_leaves_that_loop_running(
    interruption: type[BaseException], thread_deaths: list[tuple[str, type[BaseException]]]
) -> None:
    logger = Interrupting(interruption)
    lsquare = client(logger=logger, on_error=None)
    # Reported outside any loop: the logger's warn runs on the private one.
    lsquare.capture(cast(Any, ["not", "a", "mapping"]))
    lsquare.flush(timeout_ms=1_000)
    assert eventually(lambda: bool(logger.started))
    concurrent.futures.wait(list(lsquare._callback_futures), timeout=5)
    assert eventually(lambda: lsquare._report_limit.held == 0)
    current = lsquare._private_loop._current
    assert current is not None and not current[0].is_closed()
    lsquare.shutdown(timeout_ms=1_000)
    logger.started[0][1].join(5)
    assert thread_deaths == []


# -- py-runtime-13: a capture inside a call reads each field once ---------------------------------------


class Consumed(Mapping[str, Any]):
    """Keeps the ABC's `__contains__`, which reads the value: each read counted, and each value
    there for one read only, as a lazy proxy's might be."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = dict(data)
        self.reads: dict[str, int] = {}

    def __getitem__(self, key: str) -> Any:
        self.reads[key] = self.reads.get(key, 0) + 1
        return self.data.pop(key)

    def __iter__(self) -> Iterator[str]:
        return iter(list(self.data))

    def __len__(self) -> int:
        return len(self.data)


def test_a_capture_inside_a_call_reads_each_field_once_as_one_outside_a_call_does() -> None:
    lsquare = client()
    calls = lsquare._lsquare_internal().calls()
    scope = CallScope(call_id="call-1")
    fields = Consumed({"conversationId": "conv-1", "question": "q?"})
    token = calls.set(scope)
    try:
        lsquare.capture(fields)
    finally:
        calls.reset(token)
    # Each key it reads, once: a second read of either would find nothing.
    assert max(fields.reads.values()) == 1 and fields.reads["conversationId"] == fields.reads["question"] == 1
    from lsquare._event import Captured, ShapedAgent

    # The scope keeps the capture's IDs, and its agent part as it was read then.
    agent = scope.agent
    assert isinstance(agent, Captured) and dict(agent) == {"conversationId": "conv-1"}
    assert isinstance(agent.snapshot, ShapedAgent) and agent.snapshot.body["question"] == "q?"
    lsquare.shutdown(timeout_ms=0)


# -- py-runtime-16: shutdown() counts what was outstanding when it was called ---------------------------


def waited_on(future: concurrent.futures.Future[None]) -> bool:
    """A sync wait (concurrent.futures.wait) or an async one (wrap_future) has started on it, beside
    the client's own done-callback."""
    return bool(future._waiters) or len(future._done_callbacks) > 1  # type: ignore[attr-defined]


def shutting_down(lsquare: LSquare, form: str) -> tuple[threading.Thread, list[FlushResult]]:
    results: list[FlushResult] = []

    def run() -> None:
        results.append(lsquare.shutdown(timeout_ms=5_000) if form == "sync" else asyncio.run(lsquare.ashutdown(timeout_ms=5_000)))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, results


@pytest.mark.parametrize("form", ["sync", "async"])
def test_shutdown_counts_an_event_outstanding_when_it_was_called_though_it_was_sent_while_it_waited_for_a_call(form: str) -> None:
    held = Held()
    lsquare = client(held)
    # A call on an instrumented server whose event is still being built.
    finishing: concurrent.futures.Future[None] = concurrent.futures.Future()
    lsquare._lsquare_internal().track("call", finishing)
    lsquare.capture(turn(1))
    assert held.entered.wait(5)
    thread, results = shutting_down(lsquare, form)
    assert eventually(lambda: waited_on(finishing))
    # Sent while shutdown() waits for the call, which then ends with no event recorded.
    held.release(0)
    assert eventually(lambda: not lsquare._live)
    finishing.set_result(None)
    thread.join(5)
    assert results and (results[0].sent, results[0].failed) == (1, 1)


@pytest.mark.parametrize("form", ["sync", "async"])
def test_shutdown_still_waits_for_and_counts_a_capture_made_while_it_waited_for_a_call(form: str) -> None:
    # A guard: what it finds live as it closes is counted, as before.
    held = Held()
    recorder = Recorder()
    lsquare = client(held, recorder)
    finishing: concurrent.futures.Future[None] = concurrent.futures.Future()
    lsquare._lsquare_internal().track("call", finishing)
    thread, results = shutting_down(lsquare, form)
    assert eventually(lambda: waited_on(finishing))
    lsquare.capture(turn(2))
    assert held.entered.wait(5)
    finishing.set_result(None)
    # Answered only once it has closed, and so found the capture live.
    assert eventually(lambda: lsquare._closed)
    held.release()
    thread.join(5)
    assert results and (results[0].sent, results[0].failed) == (1, 1)
    assert held.answered == [True]
    assert "shutdown" not in recorder.codes()


# -- py-runtime-18 and py-runtime-19: an acknowledgement read as TypeScript's fetch reads it ------------


class RawServer:
    """A local server answering each connection with the next of `replies`, raw bytes, once the
    request is in, then closing it. Its URL is the ingestion's /v1/turns."""

    def __init__(self, replies: list[bytes]) -> None:
        self.replies = list(replies)
        self.requests = 0
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.url = f"http://127.0.0.1:{self.listener.getsockname()[1]}"
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                connection, _ = self.listener.accept()
            except OSError:
                return
            with connection:
                received = b""
                while b"\r\n\r\n" not in received:
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    received += chunk
                head, _, body = received.partition(b"\r\n\r\n")
                length = next((int(line.split(b":")[1]) for line in head.split(b"\r\n") if line.lower().startswith(b"content-length:")), 0)
                while len(body) < length:
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    body += chunk
                self.requests += 1
                reply = self.replies.pop(0) if self.replies else self.replies_left_over(body)
                connection.sendall(reply)

    @staticmethod
    def replies_left_over(body: bytes) -> bytes:
        ids = [event["eventId"] for event in json.loads(body)["events"]]
        payload = ack(*ids).encode()
        return b"HTTP/1.1 200 OK\r\ncontent-length: %d\r\n\r\n%s" % (len(payload), payload)

    def close(self) -> None:
        self.listener.close()


def answer(body: bytes, length: int | None = None, head: bytes = b"") -> bytes:
    """An HTTP 200 with this body, its Content-Length `length` (the body's own by default)."""
    declared = len(body) if length is None else length
    return b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: %d\r\n%s\r\n%s" % (declared, head, body)


@pytest.fixture
def raw_server() -> Iterator[Callable[[list[bytes]], RawServer]]:
    made: list[RawServer] = []

    def make(replies: list[bytes]) -> RawServer:
        made.append(RawServer(replies))
        return made[-1]

    yield make
    for server in made:
        server.close()


def exchange(url: str) -> Exchange:
    return Exchange(url + "/v1/turns", KEY, "ua", Resolver(start_thread), lambda delay, fn: (lambda: None))


def test_a_reply_that_ends_before_its_content_length_is_a_network_error(raw_server: Callable[[list[bytes]], RawServer]) -> None:
    server = raw_server([answer(b'{"accepted":["e1"]}', length=100)])
    with pytest.raises(NetworkError):
        exchange(server.url).post(b'{"events":[]}', 5.0, Abort())


def test_a_reply_that_ends_before_its_content_length_is_retried_as_typescript_retries_it(
    raw_server: Callable[[list[bytes]], RawServer],
) -> None:
    server = raw_server([answer(b'{"accepted":[', length=100)])
    recorder = Recorder()
    lsquare = LSquare(api_key=KEY, endpoint=server.url, logger=recorder, on_error=recorder.on_error, _random=lambda: 0.0)
    lsquare.capture(turn(1))
    lsquare.flush(timeout_ms=5_000)
    # Sent by its retry: nothing left, nothing reported. (A flush counts only what is still
    # outstanding as it starts, which the sending threads may already have sent.)
    assert not lsquare._live and server.requests == 2
    assert recorder.codes() == []
    lsquare.shutdown(timeout_ms=0)


@pytest.mark.parametrize(
    "reply",
    [
        answer(b'{"accepted":["e1"]}'),
        b'HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n13\r\n{"accepted":["e1"]}\r\n0\r\n\r\n',
        b'HTTP/1.1 200 OK\r\nconnection: close\r\n\r\n{"accepted":["e1"]}',
    ],
    ids=["content-length", "chunked", "until-closed"],
)
def test_a_complete_reply_is_read_whole(raw_server: Callable[[list[bytes]], RawServer], reply: bytes) -> None:
    # A guard: a body as long as it says, chunked, or ending with its connection, is the acknowledgement.
    server = raw_server([reply])
    out = exchange(server.url).post(b'{"events":[]}', 5.0, Abort())
    assert (out.status, out.body) == (200, '{"accepted":["e1"]}')


def test_a_bodyless_reply_and_one_past_64_kb_are_read_as_before(raw_server: Callable[[list[bytes]], RawServer]) -> None:
    # A guard: a 204 has no body to end early, and a body past the cap is cut there, unread.
    server = raw_server([b"HTTP/1.1 204 No Content\r\n\r\n", answer(b"x" * (65 * 1024))])
    first = exchange(server.url).post(b'{"events":[]}', 5.0, Abort())
    second = exchange(server.url).post(b'{"events":[]}', 5.0, Abort())
    assert (first.status, first.body, second.status, second.body) == (204, "", 200, None)


def test_an_acknowledgement_s_leading_byte_order_mark_is_dropped_as_typescript_s_text_decoder_drops_it(
    raw_server: Callable[[list[bytes]], RawServer],
) -> None:
    server = raw_server([answer(b'\xef\xbb\xbf{"accepted":["e1"]}')])
    out = exchange(server.url).post(b'{"events":[]}', 5.0, Abort())
    assert (out.status, out.body) == (200, '{"accepted":["e1"]}')


@pytest.mark.parametrize(
    ("prefix", "sent"),
    [(b"\xef\xbb\xbf", 1), (b"\xef\xbb\xbf\xef\xbb\xbf", 0), (b"\xef\xbb", 0)],
    ids=["one byte order mark", "two (a guard)", "a partial one (a guard)"],
)
def test_only_one_whole_leading_byte_order_mark_is_dropped_as_in_typescript(
    raw_server: Callable[[list[bytes]], RawServer], prefix: bytes, sent: int
) -> None:
    # TextDecoder drops one; a second, or a partial one (U+FFFD), isn't JSON in either language.
    def ack_with_prefix(body: bytes) -> bytes:
        ids = [event["eventId"] for event in json.loads(body)["events"]]
        return answer(prefix + ack(*ids).encode())

    server = raw_server([])
    server.replies_left_over = ack_with_prefix  # type: ignore[method-assign]
    recorder = Recorder()
    lsquare = LSquare(api_key=KEY, endpoint=server.url, logger=recorder, on_error=recorder.on_error)
    lsquare.capture(turn(1))
    lsquare.flush(timeout_ms=5_000)
    assert not lsquare._live and server.requests == 1
    assert recorder.wait_for(lambda: recorder.codes() == ([] if sent else ["send_failed"]))
    lsquare.shutdown(timeout_ms=0)


# -- py-runtime-28: options and timeouts past a double's range -------------------------------------------

_NUMBERS = {
    "flush_timeout_ms": "_flush_timeout_ms",
    "before_send_timeout_ms": "_before_send_timeout_ms",
    "max_concurrent_hooks": "_max_concurrent_hooks",
    "max_concurrent_requests": "_max_concurrent_requests",
    "max_queue_events": "_max_queue_events",
    "max_queue_bytes": "_max_queue_bytes",
    "max_event_bytes": "_max_event_bytes",
    "_max_attempts": "_max_attempts",
    "_request_timeout_ms": "_request_timeout_ms",
}


def test_an_option_too_large_for_a_double_falls_back_to_its_default_as_in_typescript() -> None:
    # JavaScript reads it as Infinity, which isn't a usable value: the default applies, and the client sends.
    recorder = Recorder()
    huge: dict[str, Any] = {option: 10**400 for option in _NUMBERS}
    lsquare = LSquare(api_key=KEY, endpoint="http://localhost:1", _transport=FakeTransport(), logger=recorder, **huge)
    assert repr(lsquare) == "<LSquare send>"
    defaults = {**DEFAULTS, "before_send_timeout_ms": DEFAULTS["flush_timeout_ms"]}
    assert {option: getattr(lsquare, attribute) for option, attribute in _NUMBERS.items()} == {
        option: defaults[option.lstrip("_")] for option in _NUMBERS
    }
    assert lsquare.shutdown(timeout_ms=1_000) == FlushResult()
    assert recorder.lines == []


class Unreadable:
    """Callable, but any attribute read raises: inspect can't tell what it takes, or whether it's async."""

    def __call__(self, *args: Any) -> Any:
        return args[0]

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(name)


@pytest.mark.parametrize("option", ["before_send", "on_error"])
def test_an_option_whose_read_fails_disables_the_client_with_its_warning_and_shutdown_never_raises(option: str) -> None:
    # It fails before the client's threads were made, which the warning and shutdown() need.
    recorder = Recorder()
    unreadable: dict[str, Any] = {option: Unreadable()}
    lsquare = LSquare(api_key=KEY, endpoint="http://localhost:1", logger=recorder, **unreadable)
    assert repr(lsquare) == "<LSquare disabled>"
    assert recorder.wait_for(lambda: ("warn", "[lsquare] Invalid options: capture is disabled.") in recorder.lines)
    lsquare.capture(turn(1))
    assert lsquare.flush(timeout_ms=0) == FlushResult()
    assert asyncio.run(lsquare.aflush(timeout_ms=0)) == FlushResult()
    assert lsquare.shutdown(timeout_ms=0) == FlushResult()
    assert asyncio.run(lsquare.ashutdown(timeout_ms=0)) == FlushResult()


@pytest.mark.parametrize("form", ["flush", "aflush", "shutdown", "ashutdown"])
def test_a_timeout_too_large_for_a_double_waits_the_default(form: str) -> None:
    # A guard, from the TypeScript group's branch on: read as not given, so it waits the default
    # for the event in flight, sends it and reports nothing.
    held = Held()
    recorder = Recorder()
    lsquare = client(held, recorder)
    lsquare.capture(turn(1))
    assert held.entered.wait(5)

    def release_once_waiting() -> None:
        eventually(lambda: bool(lsquare._waits))
        held.release()

    threading.Thread(target=release_once_waiting, daemon=True).start()
    timeout = 10**400
    if form == "flush":
        result = lsquare.flush(timeout_ms=timeout)
    elif form == "aflush":
        result = asyncio.run(lsquare.aflush(timeout_ms=timeout))
    elif form == "shutdown":
        result = lsquare.shutdown(timeout_ms=timeout)
    else:
        result = asyncio.run(lsquare.ashutdown(timeout_ms=timeout))
    assert (result.sent, result.failed, result.pending) == (1, 0, 0)
    assert recorder.codes() == []
    lsquare.shutdown(timeout_ms=0)


# -- parity-14: a Retry-After too large for a double -----------------------------------------------------


@pytest.mark.parametrize("retry_after", ["9" * 400, "9" * 5_000], ids=["400 digits", "5000 digits"])
def test_a_retry_after_too_large_for_a_double_waits_the_cap_as_in_typescript(retry_after: str) -> None:
    # TypeScript's Number() makes it Infinity, capped at 30 s: retried then, not failed at once.
    now = [1_000.0]
    replies = [Reply(503, retry_after, "")]

    def respond(events: list[dict[str, Any]]) -> Reply:
        return replies.pop() if replies else Reply(200, None, ack(*[event["eventId"] for event in events]))

    transport = FakeTransport(respond)
    recorder = Recorder()
    lsquare = client(transport, recorder, _clock=lambda: now[0])
    lsquare.capture(turn(1))
    assert eventually(lambda: [entry.stage for entry in list(lsquare._live.values())] == ["retry"])
    (entry,) = lsquare._live.values()
    assert entry.retry_at == now[0] + RETRY_AFTER_CAP_MS / 1000
    assert len(transport.requests) == 1
    # The client's clock reaches the cap: its scheduler, woken, sends the retry.
    now[0] += RETRY_AFTER_CAP_MS / 1000
    lsquare._scheduler.wake()
    assert eventually(lambda: entry.outcome == "sent")
    assert len(transport.requests) == 2
    assert recorder.codes() == []
    lsquare.shutdown(timeout_ms=0)


# -- parity-15: NaN and the infinities aren't JSON -------------------------------------------------------


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_an_acknowledgement_holding_nan_or_an_infinity_is_not_json_as_in_typescript(constant: str) -> None:
    # TypeScript's JSON.parse refuses it, and a 2xx body that isn't JSON fails its events.
    transport = FakeTransport(lambda events: Reply(200, None, '{"accepted":["%s"],"extra":%s}' % (events[0]["eventId"], constant)))
    recorder = Recorder()
    lsquare = client(transport, recorder)
    lsquare.capture(turn(1))
    lsquare.flush(timeout_ms=5_000)
    assert recorder.wait_for(lambda: recorder.codes() == ["send_failed"])
    assert len(transport.requests) == 1
    lsquare.shutdown(timeout_ms=0)


def test_an_acknowledgement_holding_a_number_too_large_for_a_double_is_still_json() -> None:
    # A guard: 1e400 is JSON, read as an infinite number in both languages.
    transport = FakeTransport(lambda events: Reply(200, None, '{"accepted":["%s"],"extra":1e400}' % events[0]["eventId"]))
    recorder = Recorder()
    lsquare = client(transport, recorder)
    lsquare.capture(turn(1))
    lsquare.flush(timeout_ms=5_000)
    assert not lsquare._live and len(transport.requests) == 1
    assert recorder.codes() == []
    lsquare.shutdown(timeout_ms=0)


# -- parity-16: an endpoint read as TypeScript's `new URL` reads it ------------------------------------


def endpoint_outcome(raw: str) -> str:
    """What the client makes of an endpoint: the URL it posts to, or which refusal."""
    resolved = _client._resolve_endpoint(raw)
    if isinstance(resolved, _client._Endpoint):
        return resolved.url
    if "internationalized" in resolved.problem:
        return "international"
    try:
        _url.parse(raw)
    except _url.Unsupported:
        # Refused as not https: an internationalized host on another scheme.
        return "international"
    except Exception:
        pass
    if "not a valid URL" in resolved.problem:
        return "invalid"
    assert "must use https" in resolved.problem, resolved.problem
    return "https"


#: Each endpoint, and what the client makes of it: what TypeScript's makes of it too (see
#: runtime-fixes.test.ts), but where it says `international`.
ENDPOINTS = [
    ("https://ingest.example.com", "https://ingest.example.com/v1/turns"),
    ("https://ingest.example/base/", "https://ingest.example/base/v1/turns"),
    # The path, percent-encoded as UTF-8 where the standard encodes it; a backslash is a slash.
    ('https://ingest.example/é a"^%41', "https://ingest.example/%C3%A9%20a%22%5E%41/v1/turns"),
    ("https://ingest.example/日本", "https://ingest.example/%E6%97%A5%E6%9C%AC/v1/turns"),
    ("https://ingest.example/a\\b", "https://ingest.example/a/b/v1/turns"),
    ("https://ingest.example\\x", "https://ingest.example/x/v1/turns"),
    ("https://ingest.example\\@elsewhere.example/", "https://ingest.example/@elsewhere.example/v1/turns"),
    ("https://ingest.example/x|[]{}`<>\x7f", "https://ingest.example/x|[]%7B%7D%60%3C%3E%7F/v1/turns"),
    ("https://ingest.example/~!$&()*+,;=:@'", "https://ingest.example/~!$&()*+,;=:@'/v1/turns"),
    ("https://ingest.example/a%zz/%41/", "https://ingest.example/a%zz/%41/v1/turns"),
    ("https://ingest.example/\ud800", "https://ingest.example/%EF%BF%BD/v1/turns"),
    ("https://ingest.example/a?b#c", "https://ingest.example/a/v1/turns"),
    # Dot segments, `%2e` included.
    ("https://ingest.example/a/../b", "https://ingest.example/b/v1/turns"),
    ("https://ingest.example/a/./b/.", "https://ingest.example/a/b/v1/turns"),
    ("https://ingest.example/a/%2e%2E/b", "https://ingest.example/b/v1/turns"),
    ("https://ingest.example/../..", "https://ingest.example/v1/turns"),
    # C0 controls and the space trimmed, not a no-break space; tabs and newlines removed anywhere.
    (" \x00https://ingest.example/x\x1f ", "https://ingest.example/x/v1/turns"),
    ("\u00a0https://ingest.example/", "invalid"),
    ("https://ingest.example/\u00a0", "https://ingest.example/%C2%A0/v1/turns"),
    ("https://ingest.ex\tam\nple/x\r", "https://ingest.example/x/v1/turns"),
    # Hosts: lowercased and percent-decoded, a forbidden code point refused.
    ("https://INGEST.Example/", "https://ingest.example/v1/turns"),
    ("https://ingest%2Eexample/", "https://ingest.example/v1/turns"),
    ("https://ingest%41.example/", "https://ingesta.example/v1/turns"),
    ("https://user:secret@ingest.example/", "https://ingest.example/v1/turns"),
    ("https://ingest example/", "invalid"),
    ("https://ingest^example/", "invalid"),
    ("https://ingest|example/", "invalid"),
    ("https://ingest<example/", "invalid"),
    ("https://ingest%20example/", "invalid"),
    ("https://ingest%00example/", "invalid"),
    ("https://ingest%zz/", "invalid"),
    ("https://%ff.example/", "invalid"),
    ("https://%c0%af.example/", "invalid"),
    ("https://\ufffd.example/", "invalid"),
    ("https://user@/x", "invalid"),
    # Internationalized: refused in Python, which has no UTS #46 tables.
    ("https://bücher.example/", "international"),
    ("https://xn--bcher-kva.example/", "international"),
    ("https://a\u200db.example/", "international"),
    ("https://a\u00a0b.example/", "international"),
    ("http://bücher.example/", "international"),
    # ... but not one that no mapping makes valid: a space stays a space, and a bad port is a bad port.
    ("https://a b\u00e9.example/", "invalid"),
    ("https://bücher.example:99999/", "invalid"),
    ("https://a<\u0338b.example/", "international"),
    # IPv4, in each form the standard reads, and nothing else.
    ("http://127.1:8787/", "http://127.0.0.1:8787/v1/turns"),
    ("http://0x7f.0.0.1/", "http://127.0.0.1/v1/turns"),
    ("http://0177.0.0.1/", "http://127.0.0.1/v1/turns"),
    ("http://2130706433/", "http://127.0.0.1/v1/turns"),
    ("https://0x/", "https://0.0.0.0/v1/turns"),
    ("https://127.0.0.999/", "invalid"),
    ("https://1.2.3.4.5/", "invalid"),
    ("https://example.123/", "invalid"),
    ("https://09/", "invalid"),
    ("https://4294967296/", "invalid"),
    # IPv6: canonical, without a zone.
    ("http://[0:0::1]:8787/", "http://[::1]:8787/v1/turns"),
    ("https://[::FFFF:1.2.3.4]/", "https://[::ffff:102:304]/v1/turns"),
    ("https://[1:0:0:2:0:0:0:3]/", "https://[1:0:0:2::3]/v1/turns"),
    ("https://[fe80::1%25eth0]/", "invalid"),
    ("https://[1::2::3]/", "invalid"),
    ("https://[::1.2.3.04]/", "invalid"),
    ("http://localhost./", "https"),
    # Ports.
    ("https://ingest.example:443/", "https://ingest.example/v1/turns"),
    ("https://ingest.example:0443/", "https://ingest.example/v1/turns"),
    ("https://ingest.example:8443/", "https://ingest.example:8443/v1/turns"),
    # Port 0 kept, as any but the default: fetch tries it, and can't connect.
    ("https://ingest.example:0/", "https://ingest.example:0/v1/turns"),
    ("http://localhost:00/", "http://localhost:0/v1/turns"),
    ("https://ingest.example:65536/", "invalid"),
    ("https://ingest.example:8a/", "invalid"),
    # Schemes, and their slashes.
    ("HTTPS://ingest.example/", "https://ingest.example/v1/turns"),
    ("https:ingest.example/x", "https://ingest.example/x/v1/turns"),
    ("https:\\\\\\ingest.example/x", "https://ingest.example/x/v1/turns"),
    ("http://ingest.example/", "https"),
    ("mailto:x", "https"),
    ("ftp://%zz/", "invalid"),
    ("not a url", "invalid"),
]


@pytest.mark.parametrize(("raw", "expected"), ENDPOINTS, ids=[json.dumps(raw) for raw, _ in ENDPOINTS])
def test_an_endpoint_is_read_as_typescript_s_new_url_reads_it(raw: str, expected: str) -> None:
    assert endpoint_outcome(raw) == expected


def test_an_endpoint_s_path_is_posted_to_as_typescript_writes_it(ingestion: Ingestion) -> None:
    # http.client can't write a space or non-ASCII in a path: before, nothing was ever sent.
    lsquare = LSquare(api_key=KEY, endpoint=ingestion.url + '/é a"^%41\\b/../c', logger=Recorder())
    lsquare.capture(turn(1))
    lsquare.flush(timeout_ms=5_000)
    assert not lsquare._live
    assert [request["path"] for request in ingestion.requests] == ["/%C3%A9%20a%22%5E%41/c/v1/turns"]
    lsquare.shutdown(timeout_ms=0)


class Asking(Resolver):
    """Records the host and port of each lookup; then looks it up, or fails it without a lookup."""

    def __init__(self, look_up: bool = False) -> None:
        super().__init__(start_thread)
        self.look_up = look_up
        self.asked: list[tuple[str, int]] = []

    def resolve(self, host: str, port: int, timeout: float) -> list[Any]:
        self.asked.append((host, port))
        if not self.look_up:
            raise NetworkError("not looked up")
        return super().resolve(host, port, timeout)


def no_timer(delay: float, fn: Callable[[], None]) -> Callable[[], None]:
    return lambda: None


@pytest.mark.parametrize(
    ("endpoint", "connection"),
    [
        ("https://ingest.example:0/", ("ingest.example", 0, "/v1/turns")),
        ("http://localhost:00/base", ("localhost", 0, "/base/v1/turns")),
        ("https://ingest.example/", ("ingest.example", 443, "/v1/turns")),
        ("http://localhost/", ("localhost", 80, "/v1/turns")),
        ("https://ingest.example:8443/x", ("ingest.example", 8443, "/x/v1/turns")),
        ("http://[0:0::1]:8787/", ("::1", 8787, "/v1/turns")),
    ],
)
def test_the_exchange_connects_to_the_endpoint_s_host_and_port_as_fetch_does_port_0_included(
    endpoint: str, connection: tuple[str, int, str]
) -> None:
    # Before, port 0 was taken for no port, and the request went to 443 or 80.
    resolved = _client._resolve_endpoint(endpoint)
    assert isinstance(resolved, _client._Endpoint)
    resolver = Asking()
    exchange = Exchange(resolved.url, KEY, "ua", resolver, no_timer)
    with pytest.raises(NetworkError):
        exchange.post(b'{"events":[]}', 5.0, Abort())
    assert (*resolver.asked[0], exchange._path) == connection and len(resolver.asked) == 1


def test_an_endpoint_on_port_0_fails_its_event_as_a_network_error_as_typescript_s_fetch_does() -> None:
    recorder = Recorder()
    lsquare = LSquare(api_key=KEY, endpoint="http://127.0.0.1:0/", logger=recorder, on_error=recorder.on_error, _max_attempts=1)
    assert lsquare._exchange is not None
    resolver = Asking(look_up=True)
    lsquare._exchange._resolver = resolver
    lsquare.capture(turn(1))
    # Port 0 fails at once, often before flush() starts, whose counts leave out what has already
    # ended: the failure is read from its report, not from the counts.
    result = lsquare.flush(timeout_ms=5_000)
    assert (result.sent, result.pending) == (0, 0)
    assert recorder.wait_for(lambda: recorder.codes() == ["send_failed"])
    assert resolver.asked == [("127.0.0.1", 0)]
    assert not lsquare._live
    lsquare.shutdown(timeout_ms=0)


@pytest.mark.parametrize(
    ("endpoint", "warning"),
    [
        ("https://ingest example/", "[lsquare] The endpoint is not a valid URL: capture is disabled."),
        ("https://[fe80::1%25eth0]/", "[lsquare] The endpoint is not a valid URL: capture is disabled."),
        ("https://bücher.example/", "[lsquare] The endpoint's host can't be an internationalized domain name: capture is disabled."),
    ],
)
def test_an_endpoint_that_can_never_send_disables_the_client_with_a_warning(endpoint: str, warning: str) -> None:
    recorder = Recorder()
    lsquare = LSquare(api_key=KEY, endpoint=endpoint, logger=recorder)
    assert repr(lsquare) == "<LSquare disabled>"
    assert recorder.wait_for(lambda: recorder.lines == [("warn", warning)])


@pytest.fixture
def ingestion() -> Iterator[Ingestion]:
    server = Ingestion()
    yield server
    server.close()


_HOSTS = [
    "ingest.example", "INGEST.Example", "localhost", "LOCALHOST", "localhost.", "127.0.0.1", "127.1", "0x7f.1",
    "0177.0.0.1", "2130706433", "0x7F000001", "127.0.0.999", "1.2.3.4.5", "1.2.3.4.", "example.123", "foo.0x", "0x",
    "09", "08.1", "0.0x", "4294967295", "4294967296", "0x100000000", "1.0x1000000", "1.2.65535", "1.2.65536",
    "9" * 30, "0x" + "f" * 30, "[::1]", "[0:0::1]", "[::ffff:1.2.3.4]", "[::1.2.3.4]", "[fe80::1%25eth0]", "[1::2::3]",
    "[::1.2.3.04]", "[1:2:3:4:5:6:7:8]", "[1:2:3:4:5:6:7:8:9]", "[1:2:3:4:5:6:7::]", "[::]", "[0:0:1:0:0:0:1:0]",
    "[1:0:0:2:0:0:0:3]", "[ABCD::EF]", "[:1]", "[1:]", "[::1.2.3]", "[::1.2.3.4.5]", "[::256.1.1.1]", "[12345::]",
    "[::1]x", "[", "]", "a]b", "a b", "a%20b", "a%00b", "a^b", "a|b", "a<b", 'a"b', "a{b}", "a_b", "a..b", ".a",
    "a*b", "a!b", "a$b", "a&b", "a'b", "a(b)", "a+b", "a,b", "a;b", "a=b", "a`b", "a~b", "%ff.example",
    "%c0%af.example", "\ufffd.example", "a\u200db.example", "a\u00a0b.example", "bücher.example", "xn--zz.example",
    "xn--bcher-kva.example", "XN--abc", "a<\u0338b.example", "a/\u0338b", "ingest%2Eexample", "ingest%41.example",
    "%41%42", "%zz", "a%", "a%2", "%25", "%5B::1%5D", "\u0130.example", "a\x7fb", "a%7Fb", "a%09b", "a%0Ab", "%2e",
    ".", "..", "",
]
_PORTS = ["", ":", ":0", ":00", ":443", ":0443", ":80", ":8787", ":65535", ":65536", ":8a", ": 80", ":-1", ":" + "0" * 30 + "443", ":99999999999"]
_PATHS = [
    "", "/", "/base", "/base/", "//", "///x//", "/a/../b", "/a/./b/.", "/a/%2e%2E/b", "/%2e", "/..", "/a/..", "/a/.%2E",
    '/é a"^%41', "/日本", "/a\\b", "\\x", "\\@elsewhere.example/", "/x|[]{}`<>\x7f", "/~!$&()*+,;=:@'", "/a%zz/%41/",
    "/\u00a0", "/a?b", "/a#b", "?x", "#y", "/\ud800", "/\ud83d\ude00", "/\udc00\ud800", "/a\tb\nc", "/\x00\x1f",
    "/%2F", "/a/b/../../../c",
]
_SCHEMES = ["https", "http", "HTTPS", "Https", "ws", "wss", "ftp", "file", "mailto", "foo", "h-t.t+p", "1http", "ht tp"]
_SEPARATORS = ["://", ":", ":/", ":\\\\", ":///", ":\\/", ":/\\", "://///"]
_CREDENTIALS = ["", "user@", "u:p@", "@", "a@b@", "us%40er@", "u@:", ":@"]
_ENDS = ["", " ", "\u00a0", "\t", "\x00", "\x1f", "\n", "\u3000", "\ufeff"]
_ALPHABET = list("abcxn-.:/\\@%[]?# \t^|{}<>\"'2ef0") + ["é", "\u00a0", "\u200d", "\ufffd", "%2e", "%41", "%ff", "::", "xn--"]


def endpoint_corpus(count: int = 2_000) -> list[str]:
    """Every host, port, path, scheme, separator, credential and end above in an endpoint of its
    own, then `count` drawn at random from them and from the alphabet, by a fixed seed."""
    seeded = random.Random(16)
    corpus = [f"https://{host}/" for host in _HOSTS] + [f"http://{host}/" for host in _HOSTS]
    corpus += [f"https://ingest.example{port}/x" for port in _PORTS] + [f"http://localhost{port}/x" for port in _PORTS]
    corpus += [f"https://ingest.example{path}" for path in _PATHS]
    corpus += [f"{scheme}{separator}ingest.example/x" for scheme in _SCHEMES for separator in _SEPARATORS]
    corpus += [f"{scheme}{separator}%zz/" for scheme in _SCHEMES for separator in _SEPARATORS]
    corpus += [f"https://{credentials}ingest.example/x" for credentials in _CREDENTIALS]
    corpus += [f"{start}https://ingest.example/x{end}" for start in _ENDS for end in _ENDS]
    for _ in range(count):
        corpus.append(
            seeded.choice(_ENDS) + seeded.choice(_SCHEMES) + seeded.choice(_SEPARATORS) + seeded.choice(_CREDENTIALS)
            + seeded.choice(_HOSTS) + seeded.choice(_PORTS) + seeded.choice(_PATHS) + seeded.choice(_ENDS)
        )
        corpus.append("https://" + "".join(seeded.choice(_ALPHABET) for _ in range(seeded.randint(1, 16))))
    # Falsy endpoints fall back to LSQUARE_ENDPOINT, then the default: not this check's.
    return [raw for raw in corpus if raw]


_TYPESCRIPT_ENDPOINTS = """
import { readFileSync } from 'node:fs'
const { LSquare } = await import(process.argv[1])
const endpoints = JSON.parse(readFileSync(0, 'utf8'))
// What fetch connects to and asks for: the host, an IPv6 one without its brackets, the port, the
// scheme's default only when there is none (0 is tried), and the path.
const reading = (url) => {
  const parsed = new URL(url)
  const port = parsed.port === '' ? (parsed.protocol === 'https:' ? 443 : 80) : Number(parsed.port)
  return [parsed.hostname.replace(/^\\[(.*)\\]$/, '$1'), port, parsed.pathname + parsed.search]
}
const out = []
const reads = []
for (const endpoint of endpoints) {
  const warnings = []
  const urls = []
  const fetch = async (url, init) => {
    urls.push(String(url))
    const ids = JSON.parse(String(init.body)).events.map((event) => event.eventId)
    return new Response(JSON.stringify({ accepted: ids }), { status: 200 })
  }
  const client = new LSquare({ apiKey: 'lsq_test', endpoint, fetch, onError() {}, logger: { info() {}, warn: (line) => warnings.push(line) } })
  client.capture({ conversationId: 'c', turnId: 1 })
  await client.flush({ timeoutMs: 1000 })
  // A disabled client's warning is logged in a later turn, which its flush() doesn't wait for.
  await new Promise((resolve) => setImmediate(() => setImmediate(resolve)))
  const warning = warnings.join(' ')
  out.push(warning.includes('not a valid URL') ? 'invalid' : warning.includes('must use https') ? 'https' : urls[0] ?? `none: ${warning}`)
  reads.push(urls[0] === undefined ? null : reading(urls[0]))
}
process.stdout.write(JSON.stringify({ out, reads }))
"""


def typescript_endpoints(endpoints: list[str]) -> tuple[list[str], list[list[Any] | None]]:
    """What TypeScript's own client makes of each endpoint, from its source, in one node process:
    the URL it posts to, `invalid` or `https`; and for each it posts to, the host, port and path
    its fetch connects to and asks for."""
    tsx = REPO / "node_modules" / "tsx" / "package.json"
    assert tsx.exists(), f"tsx is missing from {tsx.parent.parent}: run npm install at the repo root"
    source = (REPO / "packages" / "lsquare" / "src" / "index.ts").as_uri()
    run = subprocess.run(
        ["node", "--import", "tsx", "--input-type=module", "-e", _TYPESCRIPT_ENDPOINTS, source],
        cwd=REPO,
        input=json.dumps(endpoints, ensure_ascii=True).encode("ascii"),
        capture_output=True,
        timeout=300,
    )
    assert run.returncode == 0, run.stderr.decode("utf-8", "replace")
    made: dict[str, Any] = json.loads(run.stdout)
    assert len(made["out"]) == len(made["reads"]) == len(endpoints)
    return made["out"], made["reads"]


def test_every_endpoint_of_a_seeded_corpus_is_read_as_typescript_s_client_reads_it() -> None:
    """Python only: TypeScript's client, given the same thousands of endpoints, posts to the same
    URL or refuses them the same way, but for the internationalized hosts Python refuses."""
    if shutil.which("node") is None:
        pytest.skip("node isn't installed: TypeScript's client wasn't run on these endpoints")
    corpus = endpoint_corpus() + [raw for raw, _ in ENDPOINTS]
    theirs, readings = typescript_endpoints(corpus)
    ours = [endpoint_outcome(raw) for raw in corpus]
    differ = {raw: (mine, typescript) for raw, mine, typescript in zip(corpus, ours, theirs) if mine != typescript and mine != "international"}
    assert differ == {}
    # And for each URL both post to, the Exchange connects to and asks for what fetch does. Before,
    # port 0 went to 443 or 80.
    resolver = Resolver(start_thread)
    connections: dict[str, tuple[list[Any], list[Any]]] = {}
    for raw, mine, typescript, reading in zip(corpus, ours, theirs, readings):
        if reading is not None and mine == typescript:
            exchange = Exchange(mine, KEY, "ua", resolver, no_timer)
            connections[raw] = ([exchange._host, exchange._port, exchange._path], reading)
    assert {raw: pair for raw, pair in connections.items() if pair[0] != pair[1]} == {}
    assert sum(1 for mine, _ in connections.values() if mine[1] == 0) >= 4
    # The table's rows are TypeScript's outcomes too, but the internationalized ones.
    assert {raw: typescript for (raw, expected), typescript in zip(ENDPOINTS, theirs[-len(ENDPOINTS) :]) if expected != typescript} == {
        raw: typescript for (raw, expected), typescript in zip(ENDPOINTS, theirs[-len(ENDPOINTS) :]) if expected == "international"
    }
