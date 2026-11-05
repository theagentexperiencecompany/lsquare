"""The client's pipeline (ported from client.test.ts, sending.test.ts and host-safety.test.ts),
with a fake transport: modes, batching, acknowledgements, retries, hold-back, hooks, callbacks,
limits, flush and shutdown, threads, and the call scope the wrapper uses."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import functools
import gc
import inspect
import itertools
import subprocess
import sys
import textwrap
import threading
import time
import weakref
from collections.abc import Callable, Iterator
from typing import Any, cast

import pytest

from lsquare import LSquare, _threads
from lsquare import _client as lsquare_client
from lsquare._client import CallScope
from lsquare._sending import Reply

from helpers import FakeTransport, Held, Recorder, ack, acollected, collected, eventually, network_error, run_until

KEY = "lsq_test_key"


def client(transport: FakeTransport | None = None, recorder: Recorder | None = None, **options: Any) -> LSquare:
    recorder = recorder or Recorder()
    options.setdefault("logger", recorder)
    options.setdefault("on_error", recorder.on_error)
    options.setdefault("_random", lambda: 0.0)
    return LSquare(api_key=KEY, endpoint="http://localhost:1", _transport=transport or FakeTransport(), **options)


def turn(n: int = 0, **extra: Any) -> dict[str, Any]:
    return {"conversationId": f"conv-{n}", "turnId": n, "question": f"question {n}", "answer": "an answer", **extra}


# -- Modes ---------------------------------------------------------------------------------------


def test_without_a_key_the_client_is_disabled_and_says_so_once() -> None:
    recorder = Recorder()
    lsquare = LSquare(logger=recorder)
    lsquare.capture(turn())
    assert lsquare.flush() == lsquare.flush()
    assert recorder.wait_for(lambda: any("No api_key" in line for _, line in recorder.lines))
    assert len([line for _, line in recorder.lines if "No api_key" in line]) == 1


def test_an_insecure_endpoint_disables_the_client() -> None:
    recorder = Recorder()
    transport = FakeTransport()
    lsquare = LSquare(api_key=KEY, endpoint="http://ingest.example.com", logger=recorder, _transport=transport)
    lsquare.capture(turn())
    lsquare.flush(timeout_ms=200)
    assert transport.requests == []
    assert recorder.wait_for(lambda: any("must use https" in line for _, line in recorder.lines))


def test_dry_run_logs_one_line_per_event_without_content_and_sends_nothing() -> None:
    recorder = Recorder()
    transport = FakeTransport()
    lsquare = LSquare(dry_run=True, logger=recorder, _transport=transport)
    lsquare.capture(turn(1, question="secret question"))
    result = lsquare.flush()
    assert (result.failed, result.pending) == (0, 0)
    assert recorder.wait_for(lambda: any("[lsquare dry-run]" in line for _, line in recorder.lines))
    line = next(line for _, line in recorder.lines if "[lsquare dry-run]" in line)
    assert "secret question" not in line and "conv-1" not in line
    assert '"eventId":"from turnId, hashed"' in line
    assert transport.requests == []


CUSTOMER: dict[str, Any] = {"toolCalls": [{"name": "lookup", "input": {"customerName": "Jane Roe", "address": "1 Main St"}}]}


def test_redact_keys_given_as_a_set_mask_those_keys_with_no_warning() -> None:
    transport = FakeTransport()
    recorder = Recorder()
    lsquare = client(transport, recorder, redact_keys={"name", "address"})
    lsquare.capture(turn(**CUSTOMER))
    result = lsquare.flush(timeout_ms=5_000)
    assert (result.failed, result.pending) == (0, 0)
    assert eventually(lambda: len(transport.events) == 1)
    [event] = transport.events
    assert event["toolCalls"][0]["input"] == {"customerName": "[SECRET_1]", "address": "[SECRET_2]"}
    assert not any("redact_keys" in line for _, line in recorder.lines)


def test_entries_of_redact_keys_that_arent_key_names_warn_once_and_the_rest_mask() -> None:
    transport = FakeTransport()
    recorder = Recorder()
    lsquare = client(transport, recorder, redact_keys=cast(Any, ["name", 7]))
    lsquare.capture(turn(**CUSTOMER))
    result = lsquare.flush(timeout_ms=5_000)
    assert (result.failed, result.pending) == (0, 0)
    assert eventually(lambda: len(transport.events) == 1)
    [event] = transport.events
    assert event["toolCalls"][0]["input"] == {"customerName": "[SECRET_1]", "address": "1 Main St"}
    assert recorder.wait_for(lambda: any("Part of redact_keys isn't a key name" in line for _, line in recorder.lines))
    assert len([line for _, line in recorder.lines if "redact_keys" in line]) == 1


@pytest.mark.parametrize("redact_keys", [{"name": True}, 42])
def test_redact_keys_that_are_no_list_of_names_disable_the_client_and_say_so(redact_keys: Any) -> None:
    transport = FakeTransport()
    recorder = Recorder()
    lsquare = client(transport, recorder, redact_keys=redact_keys)
    lsquare.capture(turn(**CUSTOMER))
    lsquare.flush(timeout_ms=200)
    assert repr(lsquare) == "<LSquare disabled>" and transport.requests == []
    assert recorder.wait_for(lambda: any("redact_keys must be a list of key names: capture is disabled" in line for _, line in recorder.lines))


def test_redact_keys_whose_iteration_raises_disable_the_client() -> None:
    class Failing:
        def __iter__(self) -> Iterator[str]:
            raise RuntimeError("the configuration failed")

    recorder = Recorder()
    lsquare = client(FakeTransport(), recorder, redact_keys=cast(Any, Failing()))
    assert repr(lsquare) == "<LSquare disabled>"
    assert recorder.wait_for(lambda: any("Invalid options: capture is disabled" in line for _, line in recorder.lines))


def test_on_pyodide_redact_keys_that_raise_are_never_read_and_no_thread_starts() -> None:
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                """
                import sys, threading
                sys.platform = "emscripten"
                from lsquare import LSquare

                class Failing:
                    def __iter__(self):
                        raise RuntimeError("the configuration failed")

                before = threading.active_count()
                lsquare = LSquare(api_key="lsq_test", redact_keys=Failing())
                print(lsquare._mode, threading.active_count() - before)
                """
            ),
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.split()[:2] == ["disabled", "0"]
    assert "Pyodide" in done.stderr and "Invalid options" not in done.stderr


def test_repr_never_shows_the_key() -> None:
    assert KEY not in repr(client())


# -- Sending ---------------------------------------------------------------------------------------


def test_events_waiting_together_go_in_one_request_and_count_as_sent() -> None:
    # Held, as TypeScript's test holds its requests: the client's threads would otherwise send each
    # event as soon as it's queued, as many at once as there are slots.
    started = threading.Event()
    release = threading.Event()

    def respond(events: list[dict[str, Any]]) -> Reply:
        started.set()
        release.wait(5)
        return Reply(200, None, ack(*[event["eventId"] for event in events]))

    transport = FakeTransport(respond)
    lsquare = client(transport, max_concurrent_requests=1)
    lsquare.capture(turn(0))
    assert started.wait(5)
    for n in (1, 2):
        lsquare.capture(turn(n))
    assert eventually(lambda: sorted(entry.stage for entry in lsquare._live.values()) == ["queued", "queued", "sending"])
    release.set()
    result = lsquare.flush()
    assert (result.failed, result.pending) == (0, 0)
    assert [len(request) for request in transport.requests] == [1, 2]
    first = transport.events[0]
    assert first["kind"] == "turn" and first["sdk"]["language"] == "python"
    assert first["sdk"]["runtime"].startswith(("python/", "pypy/"))


def test_an_event_counts_as_sent_only_when_acknowledged() -> None:
    recorder = Recorder()
    transport = FakeTransport(lambda events: Reply(200, None, ack()))
    lsquare = client(transport, recorder)
    lsquare.capture(turn())
    lsquare.flush()
    assert recorder.wait_for(lambda: "send_failed" in recorder.codes())


def test_accepted_and_rejected_at_once_is_a_failure() -> None:
    recorder = Recorder()

    def respond(events: list[dict[str, Any]]) -> Reply:
        event_id = events[0]["eventId"]
        return Reply(200, None, ack(event_id, rejected=[{"eventId": event_id, "reason": "invalid_field"}]))

    lsquare = client(FakeTransport(respond), recorder)
    lsquare.capture(turn())
    lsquare.flush()
    assert recorder.wait_for(lambda: "send_failed" in recorder.codes())


def test_a_rejection_reason_outside_the_list_is_unspecified() -> None:
    recorder = Recorder()

    def respond(events: list[dict[str, Any]]) -> Reply:
        return Reply(200, None, ack(rejected=[{"eventId": events[0]["eventId"], "reason": "made-up <script>"}]))

    lsquare = client(FakeTransport(respond), recorder)
    lsquare.capture(turn())
    lsquare.flush()
    assert recorder.wait_for(lambda: "rejected" in recorder.codes())
    message = next(error.message for error in recorder.errors if error.code == "rejected")
    assert "unspecified" in message and "<script>" not in message


def test_network_errors_and_5xx_are_retried_then_sent() -> None:
    answers = [network_error, lambda events: Reply(503, None, ""), None]

    def respond(events: list[dict[str, Any]]) -> Any:
        step = answers.pop(0)
        return step(events) if step is not None else Reply(200, None, ack(*[event["eventId"] for event in events]))

    transport = FakeTransport(respond)
    recorder = Recorder()
    lsquare = client(transport, recorder)
    lsquare.capture(turn())
    lsquare.flush(timeout_ms=5_000)
    assert eventually(lambda: len(transport.requests) == 3)
    assert lsquare.flush().pending == 0 and recorder.codes() == []


def test_a_4xx_is_not_retried() -> None:
    recorder = Recorder()
    transport = FakeTransport(lambda events: Reply(401, None, ""))
    lsquare = client(transport, recorder)
    lsquare.capture(turn())
    lsquare.flush()
    assert len(transport.requests) == 1
    assert recorder.wait_for(lambda: any(getattr(error, "status", None) == 401 for error in recorder.errors))


def test_retries_stop_after_the_last_attempt() -> None:
    recorder = Recorder()
    transport = FakeTransport(lambda events: Reply(500, None, ""))
    lsquare = client(transport, recorder)
    lsquare.capture(turn())
    lsquare.flush(timeout_ms=5_000)
    assert recorder.wait_for(lambda: "send_failed" in recorder.codes())
    assert len(transport.requests) == 3
    assert recorder.wait_for(lambda: any("after 3 attempts" in error.message for error in recorder.errors))


def test_retry_after_is_honoured_and_a_retry_due_after_the_deadline_is_not_waited_for() -> None:
    transport = FakeTransport(lambda events: Reply(503, "30", ""))
    lsquare = client(transport)
    lsquare.capture(turn())
    started = time.monotonic()
    result = lsquare.flush(timeout_ms=1_000)
    assert time.monotonic() - started < 0.9
    assert result.pending == 1


def test_a_retry_waiting_out_its_delay_holds_no_sending_slot() -> None:
    first = threading.Event()

    def respond(events: list[dict[str, Any]]) -> Reply:
        if events[0]["conversationId"] == "conv-0" and not first.is_set():
            first.set()
            return Reply(503, "30", "")
        return Reply(200, None, ack(*[event["eventId"] for event in events]))

    transport = FakeTransport(respond)
    lsquare = client(transport, max_concurrent_requests=1)
    lsquare.capture(turn(0))
    assert eventually(first.is_set)
    lsquare.capture(turn(1))
    lsquare.flush(timeout_ms=1_000)
    assert eventually(lambda: any(event["conversationId"] == "conv-1" for event in transport.events))


# -- Daily limit -----------------------------------------------------------------------------------


def test_quota_exceeded_holds_back_until_the_reset_then_resumes() -> None:
    recorder = Recorder()
    now = [1_800_000_000_000.0]
    resets = "2027-01-15T09:00:00.000Z"  # an hour after "now"

    def respond(events: list[dict[str, Any]]) -> Reply:
        if now[0] < 1_800_000_000_000.0 + 1:
            return Reply(200, None, ack(rejected=[{"eventId": e["eventId"], "reason": "quota_exceeded", "resetsAt": resets} for e in events]))
        return Reply(200, None, ack(*[event["eventId"] for event in events]))

    transport = FakeTransport(respond)
    lsquare = client(transport, recorder, _now=lambda: now[0])
    lsquare.capture(turn(0))
    lsquare.flush()
    assert recorder.wait_for(lambda: "quota_exceeded" in recorder.codes())
    lsquare.capture(turn(1))
    lsquare.capture(turn(2))
    assert lsquare.flush().sent == 0
    assert len(transport.requests) == 1
    # After the limit resets, sending resumes and the dropped events are reported once.
    now[0] = 1_800_000_000_000.0 + 25 * 3_600_000
    lsquare.capture(turn(3))
    lsquare.flush()
    assert eventually(lambda: any(event["conversationId"] == "conv-3" for event in transport.events))
    assert recorder.wait_for(lambda: "quota_dropped" in recorder.codes())


# -- Hooks -----------------------------------------------------------------------------------------


def test_before_send_can_change_content_but_not_identity() -> None:
    transport = FakeTransport()

    def before_send(event: dict[str, Any], signal: Any) -> dict[str, Any]:
        return {**event, "question": "[removed]", "conversationId": "someone-else"}

    lsquare = client(transport, before_send=before_send)
    lsquare.capture(turn(7))
    lsquare.flush()
    assert eventually(lambda: len(transport.events) == 1)
    event = transport.events[0]
    assert event["question"] == "[removed]" and event["conversationId"] == "conv-7"


def test_a_burst_of_slow_hooks_after_earlier_traffic_runs_at_once() -> None:
    # With a hook thread idle from earlier traffic, the burst used to queue behind it: 8 hooks of
    # 300 ms one after another pass the 2 s deadline.
    transport = FakeTransport()
    recorder = Recorder()

    def before_send(event: dict[str, Any]) -> dict[str, Any]:
        time.sleep(0.3)
        return event

    def idle() -> int:
        with lsquare._hooks._lock:
            return lsquare._hooks._idle

    lsquare = client(transport, recorder, before_send=before_send)
    lsquare.capture({"conversationId": "warm", "turnId": 0})
    assert eventually(lambda: len(transport.events) == 1 and idle() == 1)
    for n in range(8):
        lsquare.capture({"conversationId": "c", "turnId": n})
    lsquare.flush(timeout_ms=5_000)
    assert eventually(lambda: len(transport.events) == 9)
    assert recorder.errors == []


def test_before_send_returning_none_skips_and_raising_fails() -> None:
    recorder = Recorder()
    calls = []

    def before_send(event: dict[str, Any]) -> Any:
        calls.append(event)
        if len(calls) == 1:
            return None
        raise ValueError("no")

    transport = FakeTransport()
    lsquare = client(transport, recorder, before_send=before_send)
    lsquare.capture(turn(1))
    lsquare.capture(turn(2))
    result = lsquare.flush()
    assert (result.skipped, result.failed) == (1, 1)
    assert transport.requests == []
    assert recorder.wait_for(lambda: "hook_failed" in recorder.codes())


def test_an_event_whose_before_send_no_thread_can_run_is_dropped_and_said_so_not_that_it_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    # No hook thread alive and none able to start (once the main thread has finished, on some
    # Pythons): the event is dropped, never sent unprocessed.
    start_thread = _threads.start_thread

    def start(target: Callable[[], None], name: str) -> None:
        if name == "lsquare-hook":
            raise RuntimeError("can't start new thread")
        start_thread(target, name)

    monkeypatch.setattr(_threads, "start_thread", start)
    recorder = Recorder()
    transport = FakeTransport()
    ran: list[dict[str, Any]] = []

    def before_send(event: dict[str, Any]) -> dict[str, Any]:
        ran.append(event)
        return event

    lsquare = client(transport, recorder, before_send=before_send)
    lsquare.capture(turn())
    assert recorder.wait_for(lambda: recorder.errors != [])
    assert [(error.code, error.message) for error in recorder.errors] == [
        ("hook_failed", "No thread could start for before_send; the event was dropped.")
    ]
    assert (ran, transport.requests, lsquare.flush().pending) == ([], [], 0)
    lsquare.shutdown(timeout_ms=0)


def test_a_hook_past_its_deadline_fails_and_keeps_its_slot_until_it_returns() -> None:
    recorder = Recorder()
    release = threading.Event()
    started = []

    def before_send(event: dict[str, Any], signal: Any) -> dict[str, Any]:
        started.append(event["conversationId"])
        release.wait(5)
        return event

    lsquare = client(recorder=recorder, before_send=before_send, before_send_timeout_ms=100, max_concurrent_hooks=1)
    lsquare.capture(turn(1))
    lsquare.capture(turn(2))
    assert recorder.wait_for(lambda: recorder.codes().count("hook_timeout") == 2, timeout=3)
    # The second event's hook never started: the first one kept the only slot.
    assert started == ["conv-1"]
    release.set()


def test_the_hook_signal_is_aborted_at_the_deadline() -> None:
    aborted = threading.Event()

    def before_send(event: dict[str, Any], signal: Any) -> Any:
        signal.wait(5)
        if signal.aborted:
            aborted.set()
        return None

    lsquare = client(before_send=before_send, before_send_timeout_ms=100)
    lsquare.capture(turn())
    assert eventually(aborted.is_set, timeout=3)


def test_shutdown_aborts_a_hook_still_running_never_one_that_has_returned() -> None:
    signals: dict[str, Any] = {}
    returned_on: list[threading.Thread] = []

    def before_send(event: dict[str, Any], signal: Any) -> Any:
        signals[event["conversationId"]] = signal
        if event["conversationId"] == "conv-2":
            signal.wait(5)
            return None
        returned_on.append(threading.current_thread())
        return event

    lsquare = client(before_send=before_send)
    # What the first hook hands the scheduler once it has returned is held there, as behind a
    # backlog: shutdown() then finds its event still in the hook.
    real = lsquare._scheduler.call_soon
    handed = threading.Event()

    def call_soon(fn: Any) -> None:
        if threading.current_thread() in returned_on:
            handed.set()
            return
        real(fn)

    lsquare._scheduler.call_soon = call_soon  # type: ignore[method-assign]
    lsquare.capture(turn(1))
    lsquare.capture(turn(2))
    assert handed.wait(3) and eventually(lambda: "conv-2" in signals)
    lsquare.shutdown(timeout_ms=0)
    assert not signals["conv-1"].aborted and signals["conv-2"].aborted


def test_an_async_hook_that_has_returned_is_not_aborted_by_the_shutdown_it_set_off() -> None:
    signals: list[Any] = []
    holder: list[LSquare] = []
    shut = threading.Event()

    def shutting() -> None:
        holder[0].shutdown(timeout_ms=0)
        shut.set()

    async def before_send(event: dict[str, Any], signal: Any) -> dict[str, Any]:
        signals.append(signal)
        # Runs on the loop before the hook's task has settled.
        asyncio.get_running_loop().call_soon(shutting)
        return event

    async def main() -> None:
        holder.append(client(before_send=before_send))
        holder[0].capture(turn())
        for _ in range(300):
            if shut.is_set():
                break
            await asyncio.sleep(0.01)

    asyncio.run(main())
    assert shut.is_set() and len(signals) == 1 and not signals[0].aborted


def test_a_hook_that_returns_while_a_step_holds_the_client_s_lock_goes_on_and_is_never_aborted() -> None:
    # Another event's step can hold the client's lock until this hook's deadline (reading what its
    # own hook returned, say). The hook's return is marked where it ran, without that lock: its loop
    # goes on, and the deadline the step's pump then finds leaves the hook's signal alone.
    now = [0.0]
    started = threading.Event()
    listened: list[str] = []
    hooks: list[tuple[asyncio.AbstractEventLoop, asyncio.Future[None], Any]] = []

    async def before_send(event: dict[str, Any], signal: Any) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        signal.add_listener(lambda: listened.append("aborted"))
        hooks.append((loop, future, signal))
        started.set()
        await future
        return event

    lsquare = client(before_send=before_send, before_send_timeout_ms=1000, _clock=lambda: now[0])
    try:
        lsquare.capture(turn())
        assert started.wait(5)
        loop, future, signal = hooks[0]
        with lsquare._lock:
            # The hook returns while this thread holds the lock; the loop runs what comes next.
            settle([(loop, future)])
            after = asyncio.run_coroutine_threadsafe(asyncio.sleep(0, "ran"), loop)
            try:
                went_on = after.result(2) == "ran"
            except concurrent.futures.TimeoutError:
                went_on = False
            # Then its deadline passes, and this step's pump finds it.
            now[0] += 2
            lsquare._pump()
        assert went_on
        assert lsquare._signals.drain(5)
        assert not signal.aborted and listened == []
    finally:
        lsquare.shutdown(timeout_ms=0)


def test_an_async_before_send_runs_on_the_capturing_loop() -> None:
    transport = FakeTransport()
    seen: list[Any] = []

    async def before_send(event: dict[str, Any], signal: Any) -> dict[str, Any]:
        seen.append(asyncio.get_running_loop())
        await asyncio.sleep(0)
        return {**event, "answer": "changed"}

    async def main() -> None:
        lsquare = client(transport, before_send=before_send)
        lsquare.capture(turn())
        result = await lsquare.aflush()
        assert result.sent == 1
        assert seen == [asyncio.get_running_loop()]

    asyncio.run(main())
    assert transport.events[0]["answer"] == "changed"


def test_an_async_before_send_captured_outside_any_loop_runs_on_the_private_loop() -> None:
    transport = FakeTransport()

    async def before_send(event: dict[str, Any], signal: Any) -> dict[str, Any]:
        await asyncio.sleep(0)
        return event

    lsquare = client(transport, before_send=before_send)
    lsquare.capture(turn())
    lsquare.flush()
    assert eventually(lambda: len(transport.events) == 1)


class Returned(dict[str, Any]):
    """What a hook returned, which a weak reference can follow."""


@pytest.mark.parametrize("where", ["capturing_loop", "private_loop"])
def test_an_event_still_being_sent_keeps_nothing_of_what_its_async_before_send_returned(where: str) -> None:
    # The event is built again from it: its task, and the result it holds, are let go of once it settles.
    held = Held()
    returned: list[weakref.ref[Returned]] = []

    async def before_send(event: dict[str, Any]) -> Any:
        out = Returned(event, padding="x" * 1_000_000)
        returned.append(weakref.ref(out))
        return out

    lsquare = LSquare(api_key=KEY, endpoint="http://localhost:1", _transport=held, before_send=before_send, logger=Recorder())
    try:
        if where == "capturing_loop":

            async def main() -> None:
                lsquare.capture(turn())
                assert await asyncio.to_thread(held.entered.wait, 5)
                assert await acollected(returned[0])
                assert lsquare.flush(timeout_ms=0).pending == 1
                held.release()
                assert (await lsquare.aflush(timeout_ms=5_000)).pending == 0

            asyncio.run(main())
        else:
            lsquare.capture(turn())
            assert held.entered.wait(5)
            assert collected(returned[0])
            assert lsquare.flush(timeout_ms=0).pending == 1
            held.release()
            assert lsquare.flush(timeout_ms=5_000).pending == 0
        [event] = held.acknowledged("turn")
        assert event["conversationId"] == "conv-0"
    finally:
        lsquare.shutdown(timeout_ms=0)


@pytest.mark.parametrize("where", ["capturing_loop", "private_loop"])
def test_a_sync_hooks_awaitable_that_settles_before_its_cancel_is_kept_leaves_nothing_on_its_event(
    monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    # A sync before_send returning an awaitable is started from a hook thread: its task can settle,
    # and the client take its result, before schedule_on() returns there. The hook thread then keeps
    # no cancel for it, nor its task or what it returned, for as long as the event is sent.
    held = Held()
    returned: list[weakref.ref[Returned]] = []

    async def rebuilt(event: dict[str, Any]) -> Any:
        out = Returned(event, padding="x" * 1_000_000)
        returned.append(weakref.ref(out))
        return out

    def before_send(event: dict[str, Any]) -> Any:
        return rebuilt(event)

    schedule_on = _threads.schedule_on

    def returning_late(*args: Any) -> Callable[[], None]:
        cancel = schedule_on(*args)
        # Once the event built from the hook's result has reached the ingestion.
        held.entered.wait(5)
        return cancel

    monkeypatch.setattr(lsquare_client, "schedule_on", returning_late)
    lsquare = LSquare(api_key=KEY, endpoint="http://localhost:1", _transport=held, before_send=before_send, logger=Recorder())

    def check() -> None:
        # The hook thread is done with it.
        assert lsquare._hooks.drain(5)
        [entry] = lsquare._live.values()
        assert collected(returned[0])
        assert (entry.hook_signal, entry.hook_cancel) == (None, None)

    try:
        if where == "capturing_loop":

            async def main() -> None:
                lsquare.capture(turn())
                assert await asyncio.to_thread(held.entered.wait, 5)
                await asyncio.to_thread(check)
                held.release()
                assert (await lsquare.aflush(timeout_ms=5_000)).pending == 0

            asyncio.run(main())
        else:
            lsquare.capture(turn())
            assert held.entered.wait(5)
            check()
            held.release()
            assert lsquare.flush(timeout_ms=5_000).pending == 0
        [event] = held.acknowledged("turn")
        assert event["conversationId"] == "conv-0"
    finally:
        lsquare.shutdown(timeout_ms=0)


def test_a_sync_hooks_awaitable_aborted_before_its_cancel_is_kept_is_cancelled_all_the_same(monkeypatch: pytest.MonkeyPatch) -> None:
    # shutdown() aborts the hook while the hook thread is still to keep its cancel: it cancels the
    # task itself then, rather than leave it running with its event's copy.
    started = threading.Event()
    go = threading.Event()
    cancelled = threading.Event()

    async def waiting(event: dict[str, Any]) -> Any:
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return event

    def before_send(event: dict[str, Any]) -> Any:
        return waiting(event)

    schedule_on = _threads.schedule_on

    def returning_late(*args: Any) -> Callable[[], None]:
        cancel = schedule_on(*args)
        go.wait(5)
        return cancel

    monkeypatch.setattr(lsquare_client, "schedule_on", returning_late)
    lsquare = client(before_send=before_send)
    lsquare.capture(turn())
    assert started.wait(5)
    [entry] = lsquare._live.values()
    # Nothing to cancel it by yet: the hook thread hasn't kept its cancel.
    lsquare.shutdown(timeout_ms=0)
    go.set()
    assert cancelled.wait(5)
    assert lsquare._hooks.drain(5)
    assert entry.hook_cancel is None


def test_a_hook_that_settles_after_shutdown_leaves_nothing_of_what_it_returned_on_its_event() -> None:
    # shutdown() drops the event and cancels its hook, which returns all the same. The client no
    # longer counts it, and what still holds the event (a call's turns, what wait_until was given)
    # keeps nothing of the hook.
    started = threading.Event()
    returned: list[weakref.ref[Returned]] = []

    async def before_send(event: dict[str, Any]) -> Any:
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            pass
        out = Returned(event, padding="x" * 1_000_000)
        returned.append(weakref.ref(out))
        return out

    lsquare = client(before_send=before_send)
    lsquare.capture(turn())
    assert started.wait(5)
    [entry] = lsquare._live.values()
    lsquare.shutdown(timeout_ms=0)
    assert eventually(lambda: bool(returned) and (entry.hook_signal, entry.hook_cancel) == (None, None))
    assert eventually(lambda: (gc.collect(), returned[0]() is None)[1])


def test_a_hook_whose_loop_closes_under_it_leaves_its_task_with_the_loop_not_on_its_event() -> None:
    # The app closes its loop with the hook's task still pending, so it never settles: once its
    # event is dropped (shutdown(), or its deadline), what still holds the event holds neither
    # the task's cancel nor the task, with what its hook holds.
    started = threading.Event()
    held: list[weakref.ref[Returned]] = []

    async def before_send(event: dict[str, Any]) -> Any:
        out = Returned(event, padding="x" * 1_000_000)
        held.append(weakref.ref(out))
        started.set()
        await asyncio.Event().wait()
        return out

    lsquare = client(before_send=before_send)
    loop = asyncio.new_event_loop()

    async def main() -> None:
        lsquare.capture(turn())
        assert await asyncio.to_thread(started.wait, 5)

    try:
        loop.run_until_complete(main())
        [entry] = lsquare._live.values()
        # Its cancel is queued on the loop, which closes without running it.
        lsquare.shutdown(timeout_ms=0)
    finally:
        loop.close()
    assert (entry.hook_signal, entry.hook_cancel) == (None, None)
    assert eventually(lambda: (gc.collect(), held[0]() is None)[1])


def test_the_cancel_schedule_on_returns_keeps_nothing_of_a_task_that_settled() -> None:
    # Whatever keeps it (an event, an identify run) keeps neither the task nor what it returned.
    loop = _threads.PrivateLoop()
    settled: concurrent.futures.Future[BaseException | None] = concurrent.futures.Future()
    returned: list[weakref.ref[Returned]] = []

    async def hook() -> Any:
        out = Returned(padding="x" * 1_000_000)
        returned.append(weakref.ref(out))
        return out

    try:
        cancel = _threads.schedule_on(loop, hook, contextvars.Context(), lambda error, out: settled.set_result(error))
        assert settled.result(5) is None
        # Once the step that settled it has returned.
        asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop.get()).result(5)
        assert collected(returned[0])
        # Settled: there is nothing to cancel.
        cancel()
    finally:
        loop.stop()


def test_an_async_hook_whose_loop_closes_fails_its_event() -> None:
    recorder = Recorder()

    async def before_send(event: dict[str, Any], signal: Any) -> dict[str, Any]:
        await asyncio.sleep(10)
        return event

    lsquare = client(recorder=recorder, before_send=before_send, before_send_timeout_ms=5_000)

    async def main() -> None:
        lsquare.capture(turn())
        await asyncio.sleep(0.05)

    asyncio.run(main())
    assert recorder.wait_for(lambda: "hook_failed" in recorder.codes())
    assert lsquare.flush(timeout_ms=0).failed == 0


def test_hooks_run_in_the_capturing_context() -> None:
    tenant: contextvars.ContextVar[str] = contextvars.ContextVar("tenant", default="none")
    seen = []

    def before_send(event: dict[str, Any]) -> dict[str, Any]:
        seen.append(tenant.get())
        return event

    lsquare = client(before_send=before_send)
    tenant.set("acme")
    lsquare.capture(turn())
    tenant.set("other")
    lsquare.flush()
    assert seen == ["acme"]


# -- Callbacks ---------------------------------------------------------------------------------------


def test_on_error_gets_a_redacted_copy_and_can_capture_without_looping_forever() -> None:
    recorder = Recorder()
    transport = FakeTransport(lambda events: Reply(400, None, ""))
    holder: list[LSquare] = []

    def on_error(error: Any, event: Any = None) -> None:
        recorder.on_error(error, event)
        holder[0].capture(turn(99, answer="from on_error"))

    lsquare = client(transport, recorder, on_error=on_error)
    holder.append(lsquare)
    lsquare.capture(turn(1, answer="mail me at someone@example.com"))
    lsquare.flush()
    assert recorder.wait_for(lambda: len(recorder.errors) >= 1)
    time.sleep(0.3)
    # Depth-limited: the chain stops after a couple of levels.
    assert len(recorder.errors) <= 3
    redacted = [event for event in recorder.events if event is not None]
    assert redacted and "someone@example.com" not in str(redacted[0])


def test_an_on_error_with_one_argument_is_called_with_the_error_only() -> None:
    got: list[Any] = []
    done = threading.Event()

    def on_error(error: Any) -> None:
        got.append(error)
        done.set()

    lsquare = client(FakeTransport(lambda events: Reply(400, None, "")), on_error=on_error)
    lsquare.capture(turn())
    lsquare.flush()
    assert done.wait(3) and got[0].code == "send_failed"


def test_callbacks_that_raise_never_break_anything() -> None:
    class Broken:
        def info(self, message: str) -> None:
            raise RuntimeError("logger")

        def warn(self, message: str) -> None:
            raise RuntimeError("logger")

    def on_error(error: Any, event: Any = None) -> None:
        raise RuntimeError("on_error")

    transport = FakeTransport(lambda events: Reply(400, None, ""))
    lsquare = client(transport, logger=Broken(), on_error=on_error)
    lsquare.capture(turn())
    lsquare.flush()
    assert eventually(lambda: len(transport.requests) == 1)
    lsquare.capture(turn(2))
    lsquare.flush()
    assert eventually(lambda: len(transport.requests) == 2)


# -- Callbacks held at once ----------------------------------------------------------------------------
#
# At most 100 reports and 100 log lines wait for the callback thread or run (an async handler until it
# settles), their event copies within max_queue_bytes. These count: the app's callbacks block on events
# the test releases, and where a clock is given, the rate limit is kept out of the way or frozen.

#: Varied words, so nothing is shortened: an event of over half of 4,096 bytes.
LONG_ANSWER = " ".join(f"word{n}" for n in range(400))


def unauthorized(events: list[dict[str, Any]]) -> Reply:
    return Reply(401, None, None)


def stepping() -> Callable[[], float]:
    """A clock a second further on at every reading: the rate limit always has room."""
    ticks = itertools.count()
    return lambda: float(next(ticks))


def settle(waiting: list[tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]]) -> None:
    """Lets these async handlers finish, on their own loops."""

    def resolve(future: asyncio.Future[None]) -> None:
        if not future.done():
            future.set_result(None)

    for loop, future in waiting:
        loop.call_soon_threadsafe(resolve, future)


def test_reports_over_the_rate_limit_are_counted_on_the_next_one_delivered() -> None:
    now = [0.0]
    stall = threading.Event()
    recorder = Recorder()

    def respond(events: list[dict[str, Any]]) -> Reply:
        stall.wait(10)
        return Reply(200, None, ack(*[event["eventId"] for event in events]))

    lsquare = client(FakeTransport(respond), recorder, _clock=lambda: now[0])
    try:
        for n in range(150):
            lsquare.capture(turn(n))
        lsquare.flush(timeout_ms=20)
        assert recorder.wait_for(lambda: len(recorder.errors) == 100)
        # A second on: the rate allows 100 more.
        now[0] += 1
        lsquare.capture(["not", "a", "mapping"])  # type: ignore[arg-type]
        assert recorder.wait_for(lambda: len(recorder.errors) == 101)
        assert recorder.codes().count("pending_after_flush") == 100
        assert (recorder.errors[-1].code, recorder.errors[-1].suppressed) == ("invalid_input", 50)
    finally:
        stall.set()
        lsquare.shutdown(timeout_ms=0)


def test_the_count_a_report_carried_survives_when_the_callback_thread_refuses_it() -> None:
    now = [0.0]
    recorder = Recorder()
    lsquare = client(recorder=recorder, _clock=lambda: now[0])
    try:
        for _ in range(150):
            lsquare.capture(["not", "a", "mapping"])  # type: ignore[arg-type]
        assert recorder.wait_for(lambda: len(recorder.errors) == 100)
        assert lsquare._callbacks.drain(5)
        now[0] += 1
        # As at interpreter exit, when no thread can start: this report, carrying the 50, isn't delivered.
        submit = lsquare._callbacks.submit
        lsquare._callbacks.submit = lambda job: False  # type: ignore[method-assign]
        lsquare.capture(["refused"])  # type: ignore[arg-type]
        lsquare._callbacks.submit = submit  # type: ignore[method-assign]
        lsquare.capture(["not", "a", "mapping"])  # type: ignore[arg-type]
        assert recorder.wait_for(lambda: len(recorder.errors) == 101)
        assert recorder.errors[-1].suppressed == 50
        assert lsquare._callbacks.drain(5)
        assert (lsquare._report_limit.held, lsquare._report_limit.held_bytes) == (0, 0)
    finally:
        lsquare.shutdown(timeout_ms=0)


def test_a_stuck_on_error_holds_at_most_100_reports_and_counts_the_rest() -> None:
    gate = threading.Event()
    got: list[Any] = []

    def on_error(error: Any, event: Any = None) -> None:
        gate.wait(10)
        got.append(error)

    lsquare = client(FakeTransport(unauthorized), on_error=on_error, _clock=stepping())
    try:
        for n in range(150):
            lsquare.capture(turn(n))
        lsquare.flush(timeout_ms=5000)
        gate.set()
        assert lsquare._callbacks.drain(5)
        lsquare.capture(turn(150))
        lsquare.flush(timeout_ms=5000)
        assert lsquare._callbacks.drain(5)
        # 100 held (one running, 99 waiting); the other 50 counted on the next one delivered.
        assert len(got) == 101 and got[-1].suppressed == 50
        assert len(got) + sum(error.suppressed or 0 for error in got) == 151
        assert (lsquare._report_limit.held, lsquare._report_limit.held_bytes) == (0, 0)
    finally:
        gate.set()
        lsquare.shutdown(timeout_ms=0)


def test_past_the_bytes_the_held_copies_may_take_a_report_goes_without_its_event() -> None:
    gate = threading.Event()
    got: list[tuple[Any, Any]] = []

    def on_error(error: Any, event: Any = None) -> None:
        gate.wait(10)
        got.append((error, event))

    lsquare = client(FakeTransport(unauthorized), on_error=on_error, max_queue_bytes=4096, _clock=stepping())
    try:
        for n in range(5):
            lsquare.capture(turn(n, answer=LONG_ANSWER))
            lsquare.flush(timeout_ms=5000)
        gate.set()
        assert lsquare._callbacks.drain(5)
        # The first one's copy, over half of 4,096 bytes, took what the others would have.
        assert [event is not None for _, event in got] == [True, False, False, False, False]
        assert got[0][1]["answer"] == LONG_ANSWER
        assert (lsquare._report_limit.held, lsquare._report_limit.held_bytes) == (0, 0)
        # Given back: a copy as large fits again.
        lsquare.capture(turn(5, answer=LONG_ANSWER))
        lsquare.flush(timeout_ms=5000)
        assert lsquare._callbacks.drain(5)
        assert len(got) == 6 and got[-1][1] is not None
    finally:
        gate.set()
        lsquare.shutdown(timeout_ms=0)


def test_pending_after_flush_copies_count_against_the_bytes_held() -> None:
    gate = threading.Event()
    stall = threading.Event()
    got: list[Any] = []

    def on_error(error: Any, event: Any = None) -> None:
        gate.wait(10)
        if error.code == "pending_after_flush":
            got.append(event)

    def respond(events: list[dict[str, Any]]) -> Reply:
        stall.wait(10)
        return Reply(401, None, None)

    lsquare = client(FakeTransport(respond), on_error=on_error, max_queue_bytes=4096, _clock=stepping())
    try:
        lsquare.capture(turn(answer=LONG_ANSWER))
        # Each flush reports the same event as pending, with a copy while the bytes allow.
        for _ in range(5):
            lsquare.flush(timeout_ms=1)
        gate.set()
        assert lsquare._callbacks.drain(5)
        assert [event is not None for event in got] == [True, False, False, False, False]
    finally:
        gate.set()
        stall.set()
        lsquare.shutdown(timeout_ms=0)


def test_a_stuck_logger_holds_at_most_100_lines_and_the_next_notes_the_rest() -> None:
    gate = threading.Event()
    lines: list[str] = []

    class Stuck:
        def info(self, message: str) -> None:
            gate.wait(10)
            lines.append(message)

        def warn(self, message: str) -> None:
            lines.append(message)

    lsquare = LSquare(dry_run=True, logger=Stuck(), _clock=stepping())
    try:
        for n in range(150):
            lsquare.capture(turn(n))
        lsquare.flush(timeout_ms=5000)
        gate.set()
        assert lsquare._callbacks.drain(5)
        lsquare.capture(turn(150))
        lsquare.flush(timeout_ms=5000)
        assert lsquare._callbacks.drain(5)
        assert len(lines) == 101
        assert lines[-1].endswith("[lsquare: 50 earlier log lines suppressed]")
        assert lsquare._log_limit.held == 0
    finally:
        gate.set()
        lsquare.shutdown(timeout_ms=0)


def test_an_async_on_error_that_never_settles_holds_its_slot_and_one_that_settles_gives_it_back() -> None:
    started: list[Any] = []
    waiting: list[tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]] = []

    async def on_error(error: Any, event: Any = None) -> None:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        started.append(error)
        waiting.append((loop, future))
        await future

    lsquare = client(FakeTransport(unauthorized), on_error=on_error, _clock=stepping())
    try:
        for n in range(101):
            lsquare.capture(turn(n))
        lsquare.flush(timeout_ms=5000)
        assert lsquare._callbacks.drain(5)
        # Every handler the callback thread handed to the private loop has started by then.
        asyncio.run_coroutine_threadsafe(asyncio.sleep(0), lsquare._private_loop.get()).result(5)
        assert len(started) == 100
        settle(waiting[:1])
        assert eventually(lambda: lsquare._report_limit.held == 99)
        lsquare.capture(turn(101))
        lsquare.flush(timeout_ms=5000)
        assert eventually(lambda: len(started) == 101)
        assert started[-1].suppressed == 1
    finally:
        settle(waiting)
        lsquare.shutdown(timeout_ms=0)


def test_a_report_logged_by_an_async_logger_holds_its_slot_until_the_logger_settles() -> None:
    now = [0.0]
    started: list[str] = []
    waiting: list[tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]] = []

    class Slow:
        def info(self, message: str) -> None:
            pass

        async def warn(self, message: str) -> None:
            loop = asyncio.get_running_loop()
            future: asyncio.Future[None] = loop.create_future()
            started.append(message)
            waiting.append((loop, future))
            await future

    # No on_error: each report is logged, at most once a minute.
    lsquare = client(logger=Slow(), on_error=None, _clock=lambda: now[0])
    try:
        for _ in range(101):
            now[0] += 61
            lsquare.capture(["not", "a", "mapping"])  # type: ignore[arg-type]
            assert lsquare._callbacks.drain(5)
        asyncio.run_coroutine_threadsafe(asyncio.sleep(0), lsquare._private_loop.get()).result(5)
        assert len(started) == 100
        settle(waiting)
        assert eventually(lambda: lsquare._report_limit.held == 0)
        now[0] += 61
        lsquare.capture(["not", "a", "mapping"])  # type: ignore[arg-type]
        assert eventually(lambda: len(started) == 101)
        assert started[-1].endswith("(1 more since the last report)")
    finally:
        settle(waiting)
        lsquare.shutdown(timeout_ms=0)


class Payload(dict[str, Any]):
    """What an async handler holds while it runs, which a weak reference can follow."""


@pytest.mark.parametrize("handler", ["logger", "on_error"])
def test_async_handlers_pending_on_loops_the_app_closed_give_their_slots_back_and_the_next_is_delivered(handler: str) -> None:
    # The app runs each loop until its handlers have started, then closes it without running its
    # tasks to their end (no asyncio.run): nothing settles them now. Their 100 slots, kept, would
    # drop every later report, or log line.
    loops = [asyncio.new_event_loop() for _ in range(4)]
    got: list[Any] = []

    async def handling(*args: Any) -> None:
        got.append(args[0])
        running = asyncio.get_running_loop()
        if running in loops:
            await running.create_future()

    class Logger:
        info = staticmethod(handling)

        def warn(self, message: str) -> None:
            pass

    # Reports to on_error, or, in a dry run, a line per event to the logger.
    if handler == "logger":
        lsquare = LSquare(dry_run=True, logger=Logger(), _clock=stepping())
        limit = lsquare._log_limit
    else:
        lsquare = client(on_error=handling, _clock=stepping())
        limit = lsquare._report_limit

    def fields(n: int) -> Any:
        return turn(n) if handler == "logger" else ["not", "a", "mapping"]

    def step(first: int) -> None:
        for n in range(first, first + 25):
            lsquare.capture(fields(n))

    try:
        for n, loop in enumerate(loops):
            # Returns once its 25 have started, before the next loop runs.
            run_until(loop, functools.partial(step, 25 * n), lambda: len(got) == 25 * (n + 1))
        assert (len(got), limit.held) == (100, 100)
        for loop in loops:
            loop.close()
        # Outside any loop: on the private one, where it ends at once.
        lsquare.capture(fields(100))
        assert eventually(lambda: len(got) == 101)
        if handler == "on_error":
            assert got[-1].suppressed is None
        else:
            assert "suppressed" not in got[-1]
        assert eventually(lambda: (len(lsquare._callback_futures), limit.held) == (0, 0))
    finally:
        lsquare.shutdown(timeout_ms=0)
        for loop in loops:
            loop.close()


def test_an_async_on_error_pending_on_a_loop_that_has_only_stopped_keeps_its_slot_until_that_loop_runs_it_on() -> None:
    # A stopped loop may run again, the handler on with it: neither a flush nor a later report
    # takes its slot.
    loop = asyncio.new_event_loop()
    waiting: list[asyncio.Future[None]] = []
    got: list[str] = []

    async def on_error(error: Any, event: Any = None) -> None:
        got.append(error.code)
        if asyncio.get_running_loop() is loop:
            future: asyncio.Future[None] = loop.create_future()
            waiting.append(future)
            await future

    lsquare = client(on_error=on_error, _clock=stepping())
    try:
        run_until(loop, lambda: lsquare.capture(["not", "a", "mapping"]), lambda: bool(waiting))  # type: ignore[arg-type]
        assert len(lsquare._callback_futures) == 1
        kept = next(iter(lsquare._callback_futures))
        lsquare.flush(timeout_ms=0)
        lsquare.capture(REFUSED)
        assert eventually(lambda: got == ["invalid_input", "invalid_id"])
        # The later one, on the private loop, ends at once and gives its own back.
        assert eventually(lambda: (list(lsquare._callback_futures), lsquare._report_limit.held) == ([kept], 1))

        async def run_on() -> None:
            waiting[0].set_result(None)
            deadline = time.monotonic() + 5
            while lsquare._report_limit.held and time.monotonic() < deadline:
                await asyncio.sleep(0.01)

        loop.run_until_complete(run_on())
        assert kept.done()
        assert (len(lsquare._callback_futures), lsquare._report_limit.held) == (0, 0)
    finally:
        lsquare.shutdown(timeout_ms=0)
        loop.close()


@pytest.mark.parametrize("let_go_by", ["flush", "aflush", "shutdown", "ashutdown"])
@pytest.mark.parametrize("handler", ["logger", "on_error"])
def test_what_an_async_handler_pending_on_a_loop_the_app_closed_holds_is_let_go_and_never_closed_from_here(handler: str, let_go_by: str) -> None:
    # Its coroutine, its 1 MB and the event copy go once the SDK lets go of them, which each of
    # these does as it starts, given no time to wait: the garbage collector finalizes the coroutine
    # then, as it would the app's own tasks that loop left pending. The SDK doesn't close it: that
    # would run its cleanup on the flushing thread, outside its loop.
    loop = asyncio.new_event_loop()
    coroutines: list[weakref.ref[Any]] = []
    payloads: list[weakref.ref[Payload]] = []

    async def handling(*args: Any) -> None:
        payload = Payload(padding="x" * 1_000_000)
        payloads.append(weakref.ref(payload))
        await loop.create_future()

    def handle(*args: Any) -> Any:
        coroutine = handling(*args)
        coroutines.append(weakref.ref(coroutine))
        return coroutine

    class Logger:
        def info(self, message: str) -> None:
            pass

        warn = staticmethod(handle)

    # A report: to on_error, with its event's copy, or without one to the logger.
    lsquare = client(failing(), logger=Logger(), on_error=None) if handler == "logger" else client(failing(), on_error=handle)
    try:
        run_until(loop, lambda: lsquare.capture(turn(answer=LONG_ANSWER)), lambda: bool(payloads))
        assert lsquare._report_limit.held == 1
        assert (lsquare._report_limit.held_bytes > 0) is (handler == "on_error")
        loop.close()
        # Without the garbage collector until the test lets it: what the SDK does itself shows.
        gc.disable()
        try:
            if let_go_by.startswith("a"):
                asyncio.run(getattr(lsquare, let_go_by)(timeout_ms=0))
            else:
                getattr(lsquare, let_go_by)(timeout_ms=0)
            assert (len(lsquare._callback_futures), lsquare._report_limit.held, lsquare._report_limit.held_bytes) == (0, 0, 0)
            assert inspect.getcoroutinestate(cast(Any, coroutines[0]())) == inspect.CORO_SUSPENDED
        finally:
            gc.enable()
        assert collected(coroutines[0], payloads[0])
    finally:
        lsquare.shutdown(timeout_ms=0)
        loop.close()


def test_an_async_on_error_whose_loop_the_app_closed_before_running_it_is_closed_not_left_never_awaited() -> None:
    made: list[Any] = []

    async def handler(error: Any) -> None:
        pass

    def on_error(error: Any) -> Any:
        made.append(handler(error))
        return made[-1]

    lsquare = client(on_error=on_error)
    loop = asyncio.new_event_loop()
    runs = threading.Thread(target=loop.run_forever)
    runs.start()
    gate = threading.Event()

    async def capture_then_hold() -> None:
        lsquare.capture(["not", "a", "mapping"])  # type: ignore[arg-type]
        # Busy until the test lets go: its handler, handed to this loop, can't start.
        gate.wait(5)

    asyncio.run_coroutine_threadsafe(capture_then_hold(), loop)
    try:
        assert eventually(lambda: bool(lsquare._unstarted))
        # The loop stops before running it, and the app closes it: it never starts.
        loop.call_soon_threadsafe(loop.stop)
    finally:
        gate.set()
        runs.join(5)
    loop.close()
    try:
        lsquare.flush(timeout_ms=0)
        assert len(made) == 1 and inspect.getcoroutinestate(made[0]) == inspect.CORO_CLOSED
        assert (len(lsquare._callback_futures), len(lsquare._unstarted), lsquare._report_limit.held) == (0, 0, 0)
    finally:
        lsquare.shutdown(timeout_ms=0)
    # The task the closed loop left, collected here: asyncio logs it as destroyed pending, in this test.
    gc.collect()


def test_a_clock_that_goes_back_still_lets_a_report_and_a_log_line_through() -> None:
    # As TypeScript's under an app's fake timers: the rate limits refill nothing while it is back,
    # rather than owe tokens, and drop everything, until it catches up.
    now = [600.0]
    recorder = Recorder()
    lsquare = LSquare(dry_run=True, logger=recorder, on_error=recorder.on_error, _clock=lambda: now[0])
    try:
        now[0] = 0.0
        lsquare.capture(turn())
        lsquare.capture({"conversationId": 7, "question": "q"})
        assert recorder.wait_for(lambda: recorder.codes() == ["invalid_id"])
        assert recorder.wait_for(lambda: any(line.startswith("[lsquare dry-run] ") for _, line in recorder.lines))
    finally:
        lsquare.shutdown(timeout_ms=0)


def test_after_the_clock_goes_back_the_default_on_error_logs_the_next_report_as_though_its_minute_had_passed() -> None:
    now = [600.0]
    recorder = Recorder()
    lsquare = client(recorder=recorder, on_error=None, _clock=lambda: now[0])

    def warned() -> list[str]:
        return [line for level, line in recorder.lines if level == "warn" and line.startswith("[lsquare] invalid_id:")]

    try:
        lsquare.capture({"conversationId": 7, "question": "q"})
        assert recorder.wait_for(lambda: len(warned()) == 1)
        # Logged at 600 s on that clock, then less than a minute before on it.
        now[0] = 0.0
        lsquare.capture({"conversationId": 8, "question": "q"})
        assert recorder.wait_for(lambda: len(warned()) == 2)
        assert not warned()[1].endswith("since the last report)")
    finally:
        lsquare.shutdown(timeout_ms=0)


def test_an_abort_listener_runs_at_its_deadline_while_on_error_is_stuck() -> None:
    gate = threading.Event()
    stuck = threading.Event()
    listened = threading.Event()

    def on_error(error: Any, event: Any = None) -> None:
        stuck.set()
        gate.wait(10)

    def before_send(event: dict[str, Any], signal: Any) -> Any:
        if event["turnId"] == "1":
            raise ValueError("so that on_error runs")
        signal.add_listener(listened.set)
        signal.wait(5)
        return event

    lsquare = client(on_error=on_error, before_send=before_send, before_send_timeout_ms=50)
    try:
        lsquare.capture(turn(1))
        assert stuck.wait(5)
        lsquare.capture(turn(2))
        assert listened.wait(5)
    finally:
        gate.set()
        lsquare.shutdown(timeout_ms=0)


def test_shutdown_aborting_more_than_100_hooks_runs_every_listener_while_on_error_is_stuck() -> None:
    gate = threading.Event()
    started = threading.Semaphore(0)
    lock = threading.Lock()
    ran = [0]
    recorder = Recorder()

    def on_error(error: Any, event: Any = None) -> None:
        gate.wait(10)

    def listener() -> None:
        with lock:
            ran[0] += 1

    def before_send(event: dict[str, Any], signal: Any) -> Any:
        signal.add_listener(listener)
        started.release()
        signal.wait(10)
        return event

    lsquare = client(recorder=recorder, on_error=on_error, before_send=before_send, max_concurrent_hooks=150,
                     before_send_timeout_ms=60_000)
    try:
        for n in range(150):
            lsquare.capture(turn(n))
        for _ in range(150):
            assert started.acquire(timeout=5)
        lsquare.shutdown(timeout_ms=0)
        assert eventually(lambda: ran[0] == 150)
        gate.set()
        assert lsquare._callbacks.drain(5)
        assert not any("abort listener" in line for _, line in recorder.lines)
    finally:
        gate.set()


def test_shutdown_aborting_every_hook_of_both_kinds_at_once_runs_every_listener() -> None:
    # 150 before_send hooks and 150 identify hooks (max_concurrent_hooks=150), all aborted by one
    # shutdown() while the first listener holds the signal thread: 299 jobs wait there, which room
    # for twice max_concurrent_hooks takes.
    gate = threading.Event()
    started = threading.Semaphore(0)
    lock = threading.Lock()
    ran = [0]
    recorder = Recorder()

    def listener() -> None:
        with lock:
            ran[0] += 1
            first = ran[0] == 1
        if first:
            gate.wait(10)

    def hook(signal: Any) -> None:
        signal.add_listener(listener)
        started.release()
        signal.wait(10)

    def before_send(event: dict[str, Any], signal: Any) -> Any:
        hook(signal)
        return event

    lsquare = client(recorder=recorder, before_send=before_send, max_concurrent_hooks=150, before_send_timeout_ms=60_000)
    internal = lsquare._lsquare_internal()
    try:
        for n in range(150):
            lsquare.capture(turn(n))
            assert internal.identify(hook, lambda signal: signal, internal.origin()) is not None
        for _ in range(300):
            assert started.acquire(timeout=5)
        lsquare.shutdown(timeout_ms=0)
        gate.set()
        assert eventually(lambda: ran[0] == 300)
        assert not any("abort listener" in line for _, line in recorder.lines)
    finally:
        gate.set()


def test_abort_listeners_behind_one_that_never_returns_are_dropped_past_the_bound_with_one_warning() -> None:
    forever = threading.Event()
    started = threading.Semaphore(0)
    returned = threading.Semaphore(0)
    lock = threading.Lock()
    hooks = [0]
    ran = [0]
    now = [0.0]
    recorder = Recorder()

    def quick() -> None:
        with lock:
            ran[0] += 1

    def before_send(event: dict[str, Any], signal: Any) -> Any:
        with lock:
            hooks[0] += 1
            first = hooks[0] == 1
        signal.add_listener((lambda: forever.wait(10)) if first else quick)
        started.release()
        signal.wait(5)
        returned.release()
        return event

    # The clock moves only when the test moves it: every hook of a round starts before its deadline,
    # however long a slow machine takes to start it.
    lsquare = client(recorder=recorder, before_send=before_send, before_send_timeout_ms=1000, _clock=lambda: now[0])
    try:
        # 10 rounds of 16 hooks (the default limit), each aborted at its deadline: 159 listeners
        # after the one that hangs, and 100 may wait behind it.
        for round_ in range(10):
            for n in range(16):
                lsquare.capture(turn(round_ * 16 + n))
            for _ in range(16):
                assert started.acquire(timeout=5)
            # All 16 have started: past their deadline, which the scheduler finds once woken.
            now[0] += 2
            lsquare._scheduler.call_soon(lambda: None)
            for _ in range(16):
                assert returned.acquire(timeout=5)
        assert recorder.wait_for(lambda: any("abort listener" in line for _, line in recorder.lines))
        forever.set()
        assert lsquare._signals.drain(5)
        # The 100 waiting ran, and whatever ran before the one that hung: at most its round.
        assert 100 <= ran[0] <= 115
        assert len([line for _, line in recorder.lines if "abort listener" in line]) == 1
    finally:
        forever.set()
        lsquare.shutdown(timeout_ms=0)


# -- Limits ------------------------------------------------------------------------------------------


def test_the_queue_drops_the_oldest_events_past_its_limit() -> None:
    recorder = Recorder()
    gate = threading.Event()

    def respond(events: list[dict[str, Any]]) -> Reply:
        gate.wait(5)
        return Reply(200, None, ack(*[event["eventId"] for event in events]))

    lsquare = client(FakeTransport(respond), recorder, max_queue_events=2, max_concurrent_requests=1)
    for n in range(4):
        lsquare.capture(turn(n))
    assert recorder.wait_for(lambda: "queue_full" in recorder.codes())
    gate.set()


def test_an_event_too_large_is_dropped_and_reported() -> None:
    recorder = Recorder()
    lsquare = client(recorder=recorder, max_event_bytes=300)
    lsquare.capture({"conversationId": "c" * 500, "turnId": 1})
    assert recorder.wait_for(lambda: "too_large" in recorder.codes())


def test_capture_given_anything_but_a_mapping_is_invalid_input() -> None:
    recorder = Recorder()
    lsquare = client(recorder=recorder)
    lsquare.capture(["not", "a", "mapping"])  # type: ignore[arg-type]
    assert recorder.wait_for(lambda: "invalid_input" in recorder.codes())


# -- flush and shutdown -------------------------------------------------------------------------------


def test_flush_reports_pending_events_and_flush_zero_does_not() -> None:
    recorder = Recorder()
    gate = threading.Event()

    def respond(events: list[dict[str, Any]]) -> Reply:
        gate.wait(5)
        return Reply(200, None, ack(*[event["eventId"] for event in events]))

    transport = FakeTransport(respond)
    lsquare = client(transport, recorder)
    lsquare.capture(turn())
    assert lsquare.flush(timeout_ms=0).pending == 1
    assert lsquare.flush(timeout_ms=100).pending == 1
    assert recorder.wait_for(lambda: recorder.codes().count("pending_after_flush") == 1)
    gate.set()
    lsquare.flush()
    assert eventually(lambda: len(transport.events) == 1)


def test_a_sync_flush_on_an_event_loop_warns_once() -> None:
    recorder = Recorder()
    lsquare = client(recorder=recorder)

    async def main() -> None:
        lsquare.flush(timeout_ms=0)
        lsquare.flush(timeout_ms=0)

    asyncio.run(main())
    assert recorder.wait_for(lambda: any("aflush()" in line for _, line in recorder.lines))
    assert len([line for _, line in recorder.lines if "aflush()" in line]) == 1


def test_a_sync_shutdown_on_an_event_loop_warns_once() -> None:
    recorder = Recorder()
    lsquare = client(recorder=recorder)

    async def main() -> None:
        lsquare.shutdown(timeout_ms=0)
        lsquare.shutdown(timeout_ms=0)

    asyncio.run(main())
    assert recorder.wait_for(lambda: any("ashutdown()" in line for _, line in recorder.lines))
    assert lsquare._callbacks.drain(5)
    assert len([line for _, line in recorder.lines if "ashutdown()" in line]) == 1


def test_shutdown_is_terminal_and_reports_a_later_capture_once() -> None:
    recorder = Recorder()
    transport = FakeTransport()
    lsquare = client(transport, recorder)
    lsquare.capture(turn(1))
    lsquare.shutdown()
    assert len(transport.events) == 1
    lsquare.capture(turn(2))
    lsquare.capture(turn(3))
    assert recorder.wait_for(lambda: "shutdown" in recorder.codes())
    time.sleep(0.1)
    assert recorder.codes().count("shutdown") == 1
    assert len(transport.events) == 1


class _ShutsDownWhenRead(dict):  # type: ignore[type-arg]
    """A capture input whose question, read as its event is built, shuts the client down."""

    def __init__(self, fields: dict[str, Any], lsquare: LSquare, timeout_ms: float) -> None:
        super().__init__(fields)
        self.lsquare = lsquare
        self.timeout_ms = timeout_ms
        self.results: list[Any] = []

    def __getitem__(self, key: str) -> Any:
        if key == "question":
            self.results.append(self.lsquare.shutdown(timeout_ms=self.timeout_ms))
        return dict.__getitem__(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        if key == "question":
            self.results.append(self.lsquare.shutdown(timeout_ms=self.timeout_ms))
        return dict.get(self, key, default)


def test_a_capture_whose_build_shuts_the_client_down_is_dropped_and_reported_once_as_in_typescript() -> None:
    recorder, held = Recorder(), Held()
    lsquare = LSquare(api_key=KEY, endpoint="http://localhost:1", _transport=held, on_error=recorder.on_error)
    lsquare.capture(_ShutsDownWhenRead(turn(), lsquare, 0))
    assert recorder.wait_for(lambda: recorder.codes() == ["shutdown"], 2)
    lsquare.capture(turn(2))
    held.release()
    time.sleep(0.1)
    assert recorder.codes() == ["shutdown"]
    assert held.batches == []


def test_a_capture_whose_build_shuts_the_client_down_takes_no_room_from_an_event_the_shutdown_is_sending() -> None:
    recorder, held = Recorder(), Held()
    lsquare = LSquare(api_key=KEY, endpoint="http://localhost:1", _transport=held, on_error=recorder.on_error, max_queue_events=1)
    lsquare.capture(turn(1))
    releasing = threading.Thread(target=_release_once_entered, args=(held,))
    releasing.start()
    captured = _ShutsDownWhenRead(turn(2), lsquare, 2_000)
    lsquare.capture(captured)
    releasing.join()
    assert [result.sent for result in captured.results] == [1]
    assert [[event["turnId"] for event in batch] for batch in held.batches] == [["1"]]
    assert recorder.wait_for(lambda: recorder.codes() == ["shutdown"], 2)


def test_ashutdown_aborts_a_request_in_flight() -> None:
    started = threading.Event()

    def respond(events: list[dict[str, Any]]) -> Reply:
        started.set()
        time.sleep(1)
        return Reply(200, None, ack(*[event["eventId"] for event in events]))

    lsquare = client(FakeTransport(respond))

    async def main() -> None:
        lsquare.capture(turn())
        await asyncio.sleep(0)
        assert started.wait(2)
        result = await lsquare.ashutdown(timeout_ms=100)
        assert result.failed == 1

    asyncio.run(main())


# -- What flush and shutdown reported, or waited for, reaches on_error before they return -----------
# A host may freeze the process once they return (Lambda, Vercel), the callback thread with it.


def slow_on_error(codes: list[str]) -> Callable[..., None]:
    """A sync on_error that takes a moment, as one writing to a file or a socket does: what waited
    for its report would end first without waiting for it, and it ends well within the grace."""

    def on_error(error: Any, event: Any = None) -> None:
        time.sleep(0.1)
        codes.append(error.code)

    return on_error


def failing() -> FakeTransport:
    return FakeTransport(lambda events: Reply(400, None, ""))


def stuck(gate: threading.Event) -> Callable[..., None]:
    """A sync on_error that doesn't return until the test lets it."""

    def on_error(error: Any, event: Any = None) -> None:
        gate.wait(10)

    return on_error


#: Refused as it's captured, so reported at once: a conversationId that isn't a string.
REFUSED = {"conversationId": 7, "question": "q"}


def test_flush_returns_once_the_failure_of_what_it_waited_for_and_a_report_before_it_reached_on_error() -> None:
    codes: list[str] = []
    lsquare = client(failing(), on_error=slow_on_error(codes))
    lsquare.capture(REFUSED)
    lsquare.capture(turn())
    lsquare.flush(timeout_ms=2_000)
    assert codes == ["invalid_id", "send_failed"]
    lsquare.shutdown(timeout_ms=0)


def test_aflush_returns_once_the_failure_of_what_it_waited_for_reached_on_error() -> None:
    codes: list[str] = []
    lsquare = client(failing(), on_error=slow_on_error(codes))

    async def main() -> None:
        lsquare.capture(turn())
        await lsquare.aflush(timeout_ms=2_000)
        assert codes == ["send_failed"]

    asyncio.run(main())
    lsquare.shutdown(timeout_ms=0)


def test_flush_returns_once_what_it_found_still_pending_was_reported() -> None:
    codes: list[str] = []
    held = Held()
    lsquare = LSquare(api_key=KEY, endpoint="http://localhost:1", _transport=held, on_error=slow_on_error(codes))
    lsquare.capture(turn())
    assert lsquare.flush(timeout_ms=50).pending == 1
    assert codes == ["pending_after_flush"]
    held.release()
    lsquare.shutdown(timeout_ms=0)


@pytest.mark.parametrize("how", ["sync", "async"])
def test_shutdown_returns_once_what_it_dropped_was_reported(how: str) -> None:
    codes: list[str] = []
    held = Held()
    lsquare = LSquare(api_key=KEY, endpoint="http://localhost:1", _transport=held, on_error=slow_on_error(codes))
    lsquare.capture(turn())
    assert held.entered.wait(5)
    if how == "sync":
        assert lsquare.shutdown(timeout_ms=50).failed == 1
    else:
        assert asyncio.run(lsquare.ashutdown(timeout_ms=50)).failed == 1
    assert codes == ["shutdown"]
    held.release()


def test_flush_returns_once_an_async_on_error_has_started_though_its_loop_was_busy() -> None:
    started = threading.Event()

    async def on_error(error: Any) -> None:
        started.set()
        await asyncio.sleep(0)

    lsquare = client(failing(), on_error=on_error)
    # The loop it runs on (the private one: no loop runs this thread) is busy a moment, with the app's
    # other work, say: the callback thread has only handed it over.
    lsquare._private_loop.get().call_soon_threadsafe(time.sleep, 0.2)
    lsquare.capture(turn())
    lsquare.flush(timeout_ms=2_000)
    assert started.is_set()
    lsquare.shutdown(timeout_ms=0)


def on_own_loop(factory: Callable[..., Any]) -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    """An app's loop, running on a thread of its own, with this task factory."""
    loop = asyncio.new_event_loop()
    loop.set_task_factory(factory)
    runs = threading.Thread(target=loop.run_forever)
    runs.start()
    return loop, runs


def capture_on(loop: asyncio.AbstractEventLoop, lsquare: LSquare, fields: dict[str, Any]) -> None:
    """Captures on the loop's thread, as a callback there: its reports' async handlers run on it."""
    captured: concurrent.futures.Future[None] = concurrent.futures.Future()

    def capture() -> None:
        lsquare.capture(fields)
        captured.set_result(None)

    loop.call_soon_threadsafe(capture)
    captured.result(5)


def test_flush_returns_once_an_async_on_error_has_started_whatever_its_loops_task_factory_runs_first(monkeypatch: pytest.MonkeyPatch) -> None:
    # A grace long enough to notice: a flush that took the factory's own step for the handler's
    # would return before the test lets the factory go on.
    monkeypatch.setattr(lsquare_client, "DELIVERED_GRACE_S", 30.0)
    started = threading.Event()
    gate = threading.Event()

    async def on_error(error: Any) -> None:
        started.set()

    def factory(loop: asyncio.AbstractEventLoop, coroutine: Any, **options: Any) -> asyncio.Task[Any]:
        # A valid factory: each task runs the coroutine it was given, once a step of its own is done.
        async def first() -> Any:
            await loop.run_in_executor(None, gate.wait, 5)
            return await coroutine

        return asyncio.Task(first(), loop=loop, **options)

    lsquare = client(failing(), on_error=on_error)
    loop, runs = on_own_loop(factory)
    flushed: list[bool] = []

    def flush() -> None:
        lsquare.flush(timeout_ms=2_000)
        flushed.append(started.is_set())

    flushing = threading.Thread(target=flush)
    try:
        capture_on(loop, lsquare, turn())
        flushing.start()
        # Waiting, its handler held by the factory's step, until the test lets that go.
        flushing.join(0.3)
        gate.set()
        flushing.join(10)
        assert flushed == [True]
    finally:
        gate.set()
        lsquare.shutdown(timeout_ms=0)
        loop.call_soon_threadsafe(loop.stop)
        runs.join(5)
        loop.close()


@pytest.mark.parametrize("when", ["before_its_first_step", "in_the_factorys_own_step"])
def test_an_async_on_error_cancelled_before_it_was_entered_is_closed_not_left_never_awaited(when: str) -> None:
    made: list[Any] = []

    async def handler(error: Any) -> None:
        pass

    def on_error(error: Any) -> Any:
        made.append(handler(error))
        return made[-1]

    gate = threading.Event()
    stepping = threading.Event()

    def factory(loop: asyncio.AbstractEventLoop, coroutine: Any, **options: Any) -> asyncio.Task[Any]:
        if when == "before_its_first_step":
            # Cancelled before it runs at all, as an app cancelling its tasks to stop would.
            task = asyncio.Task(coroutine, loop=loop, **options)
            task.cancel()
            return task

        async def first() -> Any:
            stepping.set()
            await loop.run_in_executor(None, gate.wait, 5)
            return await coroutine

        return asyncio.Task(first(), loop=loop, **options)

    lsquare = client(failing(), on_error=on_error)
    loop, runs = on_own_loop(factory)
    try:
        capture_on(loop, lsquare, turn())
        if when == "in_the_factorys_own_step":
            assert stepping.wait(5)
            # The app cancels its tasks to stop, the factory's own step still under way.
            loop.call_soon_threadsafe(lambda: [task.cancel() for task in asyncio.all_tasks(loop)])
        assert eventually(lambda: len(made) == 1 and inspect.getcoroutinestate(made[0]) == inspect.CORO_CLOSED)
        # Its place comes back.
        assert eventually(lambda: (len(lsquare._callback_futures), lsquare._report_limit.held) == (0, 0))
    finally:
        gate.set()
        lsquare.shutdown(timeout_ms=0)
        loop.call_soon_threadsafe(loop.stop)
        runs.join(5)
        loop.close()


def test_an_async_on_error_whose_loop_never_starts_is_closed_not_left_never_awaited(monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[Any] = []

    async def handler(error: Any) -> None:
        pass

    def on_error(error: Any) -> Any:
        made.append(handler(error))
        return made[-1]

    start_thread = _threads.start_thread

    def start(target: Callable[[], None], name: str) -> None:
        if name == "lsquare-loop":
            raise RuntimeError("can't start new thread")
        start_thread(target, name)

    monkeypatch.setattr(_threads, "start_thread", start)
    lsquare = client(failing(), on_error=on_error)
    try:
        lsquare.capture(turn())
        assert eventually(lambda: len(made) == 1 and inspect.getcoroutinestate(made[0]) == inspect.CORO_CLOSED)
        assert eventually(lambda: (len(lsquare._callback_futures), lsquare._report_limit.held) == (0, 0))
    finally:
        lsquare.shutdown(timeout_ms=0)


def test_an_async_on_error_whose_loop_stopped_before_it_started_holds_back_no_later_flush(monkeypatch: pytest.MonkeyPatch) -> None:
    # A grace long enough to notice: a flush that waited for the handler that never starts would
    # fail the test.
    monkeypatch.setattr(lsquare_client, "DELIVERED_GRACE_S", 30.0)
    started: list[str] = []

    async def on_error(error: Any, event: Any = None) -> None:
        started.append(str((event or {}).get("conversationId")))

    lsquare = client(failing(), on_error=on_error)
    loop = asyncio.new_event_loop()
    runs = threading.Thread(target=loop.run_forever)
    runs.start()
    gate = threading.Event()

    async def capture_then_hold() -> None:
        lsquare.capture(turn(1))
        # Busy until the test lets go: its handler, handed to this loop, can't start.
        gate.wait(5)

    asyncio.run_coroutine_threadsafe(capture_then_hold(), loop)
    try:
        assert eventually(lambda: bool(lsquare._unstarted))
        # The loop stops before running it: it never starts.
        loop.call_soon_threadsafe(loop.stop)
    finally:
        gate.set()
        runs.join(5)
    waiting = time.monotonic()
    lsquare.flush(timeout_ms=1_000)
    assert time.monotonic() - waiting < 5
    # A later handler, on a loop busy a moment, is still waited for.
    lsquare._private_loop.get().call_soon_threadsafe(time.sleep, 0.2)
    lsquare.capture(turn(2))
    lsquare.flush(timeout_ms=2_000)
    assert started == ["conv-2"]
    lsquare.shutdown(timeout_ms=0)
    # Its own, run at last, so the loop closes with nothing pending.
    loop.run_until_complete(asyncio.sleep(0.01))
    loop.close()


def test_a_wait_woken_as_an_event_finishes_finds_its_report_already_with_the_callback_thread() -> None:
    # Whatever waits for the event (the wrapper's wait_until) then waits for that thread.
    gate = threading.Event()
    answer = threading.Event()

    def respond(events: list[dict[str, Any]]) -> Reply:
        answer.wait(5)
        return Reply(400, None, "")

    lsquare = client(FakeTransport(respond), on_error=stuck(gate))
    try:
        assert lsquare._callbacks.drain(5)
        lsquare.capture(turn())
        [entry] = list(lsquare._live.values())
        busy: list[bool] = []
        # Called on the thread that finishes the event, as its waiter wakes the wait.
        lsquare._lsquare_internal().settled(entry.event_id, 5_000).add_done_callback(lambda _: busy.append(not lsquare._callbacks.idle()))
        answer.set()
        assert eventually(lambda: busy != [])
        assert busy == [True]
    finally:
        gate.set()
        lsquare.shutdown(timeout_ms=0)


def test_flush_reports_what_is_still_pending_though_waiting_for_calls_took_all_its_time() -> None:
    recorder = Recorder()
    held = Held()
    lsquare = LSquare(api_key=KEY, endpoint="http://localhost:1", _transport=held, on_error=recorder.on_error)
    # A call on an instrumented server whose event is never built.
    lsquare._lsquare_internal().track("call", concurrent.futures.Future())
    lsquare.capture(turn())
    assert lsquare.flush(timeout_ms=50).pending == 2
    # The unbuilt call too, by the ID its event will have.
    assert recorder.wait_for(lambda: recorder.codes() == ["pending_after_flush", "pending_after_flush"], 2)
    assert recorder.errors[1].event_id == "call"
    held.release()
    lsquare.shutdown(timeout_ms=0)


def test_flush_reports_each_call_still_waiting_for_identify_that_it_counts_as_pending_by_its_call_id() -> None:
    recorder = Recorder()
    held = Held()
    lsquare = LSquare(api_key=KEY, endpoint="http://localhost:1", _transport=held, on_error=recorder.on_error)
    try:
        # Two calls on an instrumented server whose events are never built (identify still running).
        lsquare._lsquare_internal().track("call-1", concurrent.futures.Future())
        lsquare._lsquare_internal().track("call-2", concurrent.futures.Future())
        lsquare.capture(turn())
        assert lsquare.flush(timeout_ms=50).pending == 3
        assert recorder.wait_for(lambda: recorder.codes().count("pending_after_flush") == 3, 2)
        assert {"call-1", "call-2"} <= {error.event_id for error in recorder.errors}
    finally:
        held.release()
        lsquare.shutdown(timeout_ms=0)


def test_aflush_reports_an_unbuilt_call_alone_and_with_0_nothing() -> None:
    recorder = Recorder()
    lsquare = LSquare(api_key=KEY, endpoint="http://localhost:1", _transport=Held(), on_error=recorder.on_error)
    try:
        lsquare._lsquare_internal().track("call-1", concurrent.futures.Future())

        async def main() -> None:
            assert (await lsquare.aflush(timeout_ms=0)).pending == 1
            assert (await lsquare.aflush(timeout_ms=50)).pending == 1

        asyncio.run(main())
        # Reports reach on_error in order, through one thread: the second flush's comes once.
        assert recorder.wait_for(lambda: len(recorder.errors) >= 1, 2)
        assert [(error.code, error.event_id) for error in recorder.errors] == [("pending_after_flush", "call-1")]
    finally:
        lsquare.shutdown(timeout_ms=0)


_request: contextvars.ContextVar[str] = contextvars.ContextVar("request", default="none")


@pytest.mark.parametrize("how", ["sync", "async"])
def test_flush_reports_an_unbuilt_call_with_the_context_and_depth_it_was_tracked_with(how: str) -> None:
    recorder = Recorder()
    seen: list[tuple[str, str, int]] = []

    def on_error(error: Any) -> None:
        seen.append((error.event_id, _request.get(), lsquare_client.current_depth()))
        recorder.on_error(error)

    held = Held()
    lsquare = LSquare(api_key=KEY, endpoint="http://localhost:1", _transport=held, on_error=on_error)
    internal = lsquare._lsquare_internal()

    # Request A: a call that replied, its event waiting for identify, and a turn it captured.
    def request_a() -> str:
        _request.set("A")
        internal.track("call-A", concurrent.futures.Future())
        lsquare.capture(turn())
        [entry] = lsquare._live.values()
        return entry.event_id

    turn_id = contextvars.copy_context().run(request_a)

    # Request B flushes, from inside a callback of its own, one level deep.
    def request_b() -> Any:
        _request.set("B")
        lsquare_client._depth.set(1)
        if how == "sync":
            return lsquare.flush(timeout_ms=50)
        return asyncio.run(lsquare.aflush(timeout_ms=50))

    try:
        assert contextvars.copy_context().run(request_b).pending == 2
        assert recorder.wait_for(lambda: len(recorder.errors) == 2, 2)
        # Both as from request A, one level deeper than it ran: the queued turn's, and the call's.
        assert sorted(seen) == sorted([(turn_id, "A", 1), ("call-A", "A", 1)])
    finally:
        held.release()
        lsquare.shutdown(timeout_ms=0)


@pytest.mark.parametrize("how", ["sync", "async"])
def test_flush_runs_an_async_on_error_for_an_unbuilt_call_on_the_loop_it_was_tracked_on(how: str) -> None:
    seen: list[asyncio.AbstractEventLoop] = []
    done = threading.Event()

    async def on_error(error: Any) -> None:
        if error.event_id == "call-A":
            seen.append(asyncio.get_running_loop())
            done.set()

    lsquare = LSquare(api_key=KEY, endpoint="http://localhost:1", _transport=Held(), on_error=on_error)
    internal = lsquare._lsquare_internal()
    # The call's loop, still running when another flushes.
    call_loop = asyncio.new_event_loop()
    running = threading.Thread(target=call_loop.run_forever)
    running.start()

    async def tracked() -> None:
        internal.track("call-A", concurrent.futures.Future())

    async def flushed() -> None:
        assert (await lsquare.aflush(timeout_ms=20)).pending == 1

    try:
        asyncio.run_coroutine_threadsafe(tracked(), call_loop).result(2)
        if how == "sync":
            assert lsquare.flush(timeout_ms=20).pending == 1
        else:
            asyncio.run(flushed())
        assert done.wait(2)
        assert seen == [call_loop]
    finally:
        call_loop.call_soon_threadsafe(call_loop.stop)
        running.join(2)
        call_loop.close()
        lsquare.shutdown(timeout_ms=0)


@pytest.mark.parametrize("how", ["sync", "async"])
def test_flush_reports_what_was_captured_in_an_empty_context_from_that_context(how: str) -> None:
    # An empty context is false, and still the one the event and the call came from.
    seen: list[tuple[str, str]] = []
    done = threading.Event()

    def on_error(error: Any) -> None:
        seen.append((error.event_id, _request.get()))
        if len(seen) == 2:
            done.set()

    held = Held()
    lsquare = LSquare(api_key=KEY, endpoint="http://localhost:1", _transport=held, on_error=on_error)
    internal = lsquare._lsquare_internal()

    def request_a() -> str:
        assert len(contextvars.copy_context()) == 0
        internal.track("call-A", concurrent.futures.Future())
        lsquare.capture(turn())
        [entry] = lsquare._live.values()
        return entry.event_id

    turn_id = contextvars.Context().run(request_a)

    def request_b() -> Any:
        _request.set("B")
        if how == "sync":
            return lsquare.flush(timeout_ms=50)
        return asyncio.run(lsquare.aflush(timeout_ms=50))

    try:
        assert contextvars.copy_context().run(request_b).pending == 2
        assert done.wait(2)
        assert sorted(seen) == sorted([(turn_id, "none"), ("call-A", "none")])
    finally:
        held.release()
        lsquare.shutdown(timeout_ms=0)


def test_aflush_runs_an_async_on_error_for_what_was_captured_outside_a_loop_off_the_flushing_loop() -> None:
    # No loop where the event and the call came from: their async on_error runs on the private
    # loop, never on the loop of the request that flushes, which runs on until both have run.
    seen: dict[str, asyncio.AbstractEventLoop] = {}
    done = threading.Event()

    async def on_error(error: Any) -> None:
        seen[error.event_id] = asyncio.get_running_loop()
        if len(seen) == 2:
            done.set()

    held = Held()
    lsquare = LSquare(api_key=KEY, endpoint="http://localhost:1", _transport=held, on_error=on_error)
    lsquare._lsquare_internal().track("call-A", concurrent.futures.Future())
    lsquare.capture(turn())

    async def request_b() -> asyncio.AbstractEventLoop:
        assert (await lsquare.aflush(timeout_ms=50)).pending == 2
        assert await asyncio.get_running_loop().run_in_executor(None, done.wait, 2)
        return asyncio.get_running_loop()

    try:
        flushing = asyncio.run(request_b())
        assert len(seen) == 2 and flushing not in seen.values()
    finally:
        held.release()
        lsquare.shutdown(timeout_ms=0)


def test_a_line_logged_for_an_empty_context_and_no_loop_keeps_both() -> None:
    # As a dry run's line for an event is: the event's context and loop, though the code that logs
    # it has a context and a loop of its own.
    seen: list[tuple[str, asyncio.AbstractEventLoop]] = []
    done = threading.Event()

    class Logger:
        async def info(self, message: str) -> None:
            seen.append((_request.get(), asyncio.get_running_loop()))
            done.set()

    lsquare = client(logger=Logger())

    async def logging() -> asyncio.AbstractEventLoop:
        _request.set("B")
        lsquare._log("info", "a line", 0, contextvars.Context(), None)
        assert await asyncio.get_running_loop().run_in_executor(None, done.wait, 2)
        return asyncio.get_running_loop()

    try:
        loop = asyncio.run(logging())
        assert len(seen) == 1 and seen[0][0] == "none" and seen[0][1] is not loop
    finally:
        lsquare.shutdown(timeout_ms=0)


def _release_once_entered(held: Held) -> None:
    """Lets the held batches answer once one has reached the transport (within 2 s)."""
    held.entered.wait(2)
    held.release()


class _Unreadable(int):
    """A number whose value can't be read: its `__float__` raises."""

    def __float__(self) -> float:
        raise RuntimeError("bad option")


def test_shutdown_with_an_unreadable_timeout_waits_as_by_default_and_closes() -> None:
    recorder, held = Recorder(), Held()
    lsquare = LSquare(api_key=KEY, endpoint="http://localhost:1", _transport=held, on_error=recorder.on_error, flush_timeout_ms=2_000)
    lsquare.capture(turn(1))
    releasing = threading.Thread(target=_release_once_entered, args=(held,))
    releasing.start()
    assert lsquare.shutdown(timeout_ms=_Unreadable(5)).sent == 1
    releasing.join()
    lsquare.capture(turn(2))
    assert recorder.wait_for(lambda: recorder.codes() == ["shutdown"], 2)
    assert [[event["turnId"] for event in batch] for batch in held.batches] == [["1"]]


def test_ashutdown_with_an_unreadable_timeout_waits_as_by_default_and_closes() -> None:
    recorder, held = Recorder(), Held()
    lsquare = LSquare(api_key=KEY, endpoint="http://localhost:1", _transport=held, on_error=recorder.on_error, flush_timeout_ms=2_000)

    async def main() -> None:
        lsquare.capture(turn(1))
        releasing = threading.Thread(target=_release_once_entered, args=(held,))
        releasing.start()
        assert (await lsquare.ashutdown(timeout_ms=_Unreadable(5))).sent == 1
        releasing.join()

    asyncio.run(main())
    lsquare.capture(turn(2))
    assert recorder.wait_for(lambda: recorder.codes() == ["shutdown"], 2)
    assert [[event["turnId"] for event in batch] for batch in held.batches] == [["1"]]


@pytest.mark.parametrize("how", ["sync", "async"])
def test_shutdown_with_an_unreadable_timeout_counts_an_unbuilt_call_as_failed(how: str) -> None:
    lsquare = LSquare(api_key=KEY, endpoint="http://localhost:1", _transport=Held(), flush_timeout_ms=20)
    lsquare._lsquare_internal().track("call-1", concurrent.futures.Future())
    result = lsquare.shutdown(timeout_ms=_Unreadable(5)) if how == "sync" else asyncio.run(lsquare.ashutdown(timeout_ms=_Unreadable(5)))
    assert result.failed == 1


@pytest.mark.parametrize("how", ["sync", "async"])
def test_a_shutdown_that_fails_before_it_starts_still_counts_an_unbuilt_call_as_failed(how: str) -> None:
    lsquare = LSquare(api_key=KEY, endpoint="http://localhost:1", _transport=Held())
    lsquare._lsquare_internal().track("call-1", concurrent.futures.Future())

    def failing() -> None:
        raise RuntimeError("unexpected")

    # Its first step: what it does after, the calls it reads included, never runs.
    lsquare._retire_closed = failing  # type: ignore[method-assign]
    result = lsquare.shutdown(timeout_ms=0) if how == "sync" else asyncio.run(lsquare.ashutdown(timeout_ms=0))
    assert result.failed == 1
    assert lsquare._closed


@pytest.mark.parametrize("how", ["sync", "async"])
def test_flush_with_an_unreadable_timeout_waits_its_default_then_counts_and_reports_what_is_pending(how: str) -> None:
    recorder, held = Recorder(), Held()
    lsquare = LSquare(api_key=KEY, endpoint="http://localhost:1", _transport=held, on_error=recorder.on_error, flush_timeout_ms=20)
    try:
        lsquare.capture(turn())
        result = lsquare.flush(timeout_ms=_Unreadable(5)) if how == "sync" else asyncio.run(lsquare.aflush(timeout_ms=_Unreadable(5)))
        assert result.pending == 1
        assert recorder.wait_for(lambda: recorder.codes() == ["pending_after_flush"], 2)
    finally:
        held.release()
        lsquare.shutdown(timeout_ms=0)


def test_a_stuck_sync_on_error_holds_back_one_flush_its_grace_not_every_flush() -> None:
    gate = threading.Event()
    lsquare = client(on_error=stuck(gate))
    try:
        lsquare.capture(REFUSED)
        for _ in range(5):
            assert lsquare.flush(timeout_ms=1_000).pending == 0
        # The first waited its grace behind it; the others found that one still waiting, and didn't.
        assert len(lsquare._barriers) == 1
    finally:
        gate.set()
        lsquare.shutdown(timeout_ms=0)


class Frozen:
    """The time module as a test that freezes time sees it (freezegun's `freeze_time`): its
    monotonic clock stands still, while waits still take real time."""

    def __init__(self) -> None:
        self.now = time.monotonic()

    def monotonic(self) -> float:
        return self.now

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)


@pytest.mark.parametrize("how", ["sync", "async"])
def test_a_frozen_clock_holds_flush_and_shutdown_back_a_grace_each_not_for_as_long_as_on_error_takes(
    monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    gate = threading.Event()
    lsquare = client(on_error=stuck(gate))
    monkeypatch.setattr(lsquare_client, "time", Frozen())
    try:
        lsquare.capture(REFUSED)
        started = time.monotonic()
        if how == "sync":
            lsquare.flush(timeout_ms=1_000)
            lsquare.shutdown(timeout_ms=1_000)
        else:

            async def main() -> None:
                await lsquare.aflush(timeout_ms=1_000)
                await lsquare.ashutdown(timeout_ms=1_000)

            asyncio.run(main())
        # The handler holds on for 10 s.
        assert time.monotonic() - started < 5
    finally:
        gate.set()


def test_a_flush_or_shutdown_given_no_time_waits_for_no_report(monkeypatch: pytest.MonkeyPatch) -> None:
    # A grace long enough to notice: a wait for the stuck handler would fail the test.
    monkeypatch.setattr(lsquare_client, "DELIVERED_GRACE_S", 30.0)
    gate = threading.Event()
    lsquare = client(on_error=stuck(gate))
    try:
        lsquare.capture(REFUSED)
        started = time.monotonic()
        lsquare.flush(timeout_ms=0)
        lsquare.shutdown(timeout_ms=0)
        assert time.monotonic() - started < 5
    finally:
        gate.set()


def test_a_sync_flush_on_a_loop_waits_for_no_async_on_error_that_loop_would_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lsquare_client, "DELIVERED_GRACE_S", 30.0)
    ran: list[str] = []

    async def on_error(error: Any) -> None:
        ran.append(error.code)

    lsquare = client(failing(), on_error=on_error)

    async def main() -> None:
        lsquare.capture(turn())
        started = time.monotonic()
        # Blocking the loop the handler is due on: it can't start until this returns.
        lsquare.flush(timeout_ms=2_000)
        assert time.monotonic() - started < 5
        assert ran == []
        await lsquare.aflush(timeout_ms=2_000)
        assert ran == ["send_failed"]

    asyncio.run(main())
    lsquare.shutdown(timeout_ms=0)


def test_the_exit_flush_leaves_waiting_for_what_it_reported_to_its_own_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    # The same grace for both: flush()'s last wait would come on top of it.
    waited: list[Any] = []
    held = Held()
    lsquare = LSquare(api_key=KEY, endpoint="http://localhost:1", _transport=held, flush_timeout_ms=100, on_error=Recorder().on_error)
    monkeypatch.setattr(lsquare, "_wait_delivered", lambda blocked=None: waited.append(blocked))
    lsquare.capture(turn())
    lsquare._exit_flush()
    held.release()
    assert waited == []


# -- Threads -----------------------------------------------------------------------------------------


def test_many_threads_capturing_at_once_send_each_event_exactly_once() -> None:
    transport = FakeTransport()
    lsquare = client(transport)

    def work(base: int) -> None:
        for n in range(25):
            lsquare.capture(turn(base * 100 + n))

    threads = [threading.Thread(target=work, args=(base,)) for base in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    result = lsquare.flush(timeout_ms=5_000)
    assert (result.failed, result.pending) == (0, 0)
    assert eventually(lambda: len(transport.events) == 200)
    ids = [event["eventId"] for event in transport.events]
    assert len(ids) == len(set(ids)) == 200


def test_the_sdk_threads_start_in_an_empty_context() -> None:
    marker: contextvars.ContextVar[str] = contextvars.ContextVar("marker", default="empty")
    seen: list[str] = []

    def respond(events: list[dict[str, Any]]) -> Reply:
        return Reply(200, None, ack(*[event["eventId"] for event in events]))

    lsquare = client(FakeTransport(respond))
    marker.set("request-1")

    def peek() -> None:
        seen.append(marker.get())

    lsquare._scheduler.call_soon(peek)
    assert eventually(lambda: bool(seen))
    assert seen == ["empty"]


def test_a_sending_thread_refused_for_a_while_sends_once_one_starts_with_no_capture_after(monkeypatch: pytest.MonkeyPatch) -> None:
    # A thread start can fail while the process runs too (a container's process limit). No request
    # was made, so no attempt is spent: the batch waits at the head of the queue, which is pumped
    # again on its own, with backoff, until a sending thread starts.
    monkeypatch.setattr(lsquare_client, "RETRY_BASE_MS", 10)
    start_thread = _threads.start_thread
    refusing = threading.Event()
    refusing.set()
    refused: list[str] = []

    def start(target: Callable[[], None], name: str) -> None:
        if name == "lsquare-sender" and refusing.is_set():
            refused.append(name)
            raise RuntimeError("can't start new thread")
        start_thread(target, name)

    monkeypatch.setattr(_threads, "start_thread", start)
    transport = FakeTransport()
    recorder = Recorder()
    lsquare = client(transport, recorder)
    lsquare.capture(turn())
    assert eventually(lambda: len(refused) >= 2)
    with lsquare._lock:
        (entry,) = lsquare._live.values()
        assert (entry.stage, entry.attempts, transport.requests) == ("queued", 0, [])
    refusing.clear()
    assert eventually(lambda: len(transport.events) == 1)
    assert eventually(lambda: not lsquare._live)
    assert entry.attempts == 1 and recorder.errors == []
    lsquare.shutdown(timeout_ms=0)


def test_a_stopped_private_loop_runs_what_is_still_queued_before_it_closes_within_its_passes() -> None:
    # What each pass queues runs in the next, a chain of them to its end; one that queues itself
    # again, forever, holds the close for a bounded number of passes only.
    private = _threads.PrivateLoop()
    loop = private.get()
    ran: list[int] = []
    again: list[int] = []

    def chain(n: int) -> None:
        ran.append(n)
        if n < 5:
            loop.call_soon(chain, n + 1)

    def forever() -> None:
        again.append(1)
        loop.call_soon(forever)

    def last_pass() -> None:
        loop.call_soon(chain, 0)
        loop.call_soon(forever)
        # This pass is the loop's last: what it queued is left for after it.
        loop.stop()

    loop.call_soon_threadsafe(last_pass)
    assert eventually(loop.is_closed)
    assert ran == [0, 1, 2, 3, 4, 5]
    assert 0 < len(again) <= _threads.WIND_DOWN_PASSES


def test_an_async_on_error_handed_to_the_private_loop_in_its_last_pass_gives_its_slot_back_once_that_loop_closes() -> None:
    # A callback that queues itself again, forever, takes every pass the stopped loop runs before it
    # closes. A handler handed over during the last is left queued, never started, its future never
    # settled: the loop it belongs to has closed, so its slot comes back all the same.
    made: list[Any] = []

    async def handler(error: Any) -> None:
        pass

    def on_error(error: Any) -> Any:
        made.append(handler(error))
        return made[-1]

    lsquare = client(on_error=on_error)
    loop = lsquare._private_loop.get()
    runs: list[int] = []
    handed = threading.Event()

    def forever() -> None:
        runs.append(1)
        if len(runs) == _threads.WIND_DOWN_PASSES:
            # The last pass, which ends once the handler has been handed over, to run after it.
            handed.wait(5)
        loop.call_soon(forever)

    def last_pass() -> None:
        loop.call_soon(forever)
        loop.stop()

    try:
        loop.call_soon_threadsafe(last_pass)
        assert eventually(lambda: len(runs) == _threads.WIND_DOWN_PASSES)
        lsquare.capture(["not", "a", "mapping"])  # type: ignore[arg-type]
        assert eventually(lambda: bool(lsquare._unstarted))
        handed.set()
        assert eventually(loop.is_closed)
        lsquare.flush(timeout_ms=0)
        assert len(made) == 1 and inspect.getcoroutinestate(made[0]) == inspect.CORO_CLOSED
        assert (len(lsquare._callback_futures), len(lsquare._unstarted), lsquare._report_limit.held) == (0, 0, 0)
    finally:
        handed.set()
        lsquare.shutdown(timeout_ms=0)


def ending_in_the_private_loops_last_pass(lsquare: LSquare, future: asyncio.Future[None]) -> None:
    """Lets what awaits `future` on the private loop end in that loop's last pass: its step is
    queued, then shutdown() queues the loop's stop behind it, before the pass runs. Its task's
    done-callbacks are queued then, for a pass after the last."""
    loop = future.get_loop()
    queued = threading.Event()
    stopped = threading.Event()

    def resume() -> None:
        future.set_result(None)
        queued.set()
        # The pass ends once shutdown() has queued the loop's stop.
        stopped.wait(5)

    loop.call_soon_threadsafe(resume)
    assert queued.wait(5)
    lsquare.shutdown(timeout_ms=0)
    stopped.set()
    assert eventually(loop.is_closed)


@pytest.mark.parametrize("handler", ["logger", "on_error"])
def test_an_async_handler_ending_in_the_private_loops_last_pass_settles_and_keeps_nothing(handler: str) -> None:
    # run_coroutine_threadsafe settles the handler's future from a done-callback of its task: run
    # before the loop closes, the future settles and gives the report's slot back, rather than
    # keep the task, with what it returned, for the exit to wait on.
    waiting: list[asyncio.Future[None]] = []
    returned: list[weakref.ref[Returned]] = []

    async def handling(*args: Any) -> Any:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        waiting.append(future)
        await future
        out = Returned(padding="x" * 1_000_000)
        returned.append(weakref.ref(out))
        return out

    class Logger:
        def info(self, message: str) -> None:
            pass

        warn = staticmethod(handling)

    # A report: to on_error, or without one to the logger.
    lsquare = client(logger=Logger(), on_error=None) if handler == "logger" else client(on_error=handling)
    lsquare.capture(["not", "a", "mapping"])  # type: ignore[arg-type]
    assert eventually(lambda: bool(waiting))
    ending_in_the_private_loops_last_pass(lsquare, waiting[0])
    assert eventually(lambda: (len(lsquare._callback_futures), lsquare._report_limit.held) == (0, 0))
    assert eventually(lambda: (gc.collect(), returned[0]() is None)[1])


def test_an_async_hook_ending_in_the_private_loops_last_pass_settles_and_keeps_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    # shutdown() drops its event and cancels it, too late: it has returned in the same pass. Its
    # settling, a done-callback of its task, still reaches the client.
    waiting: list[asyncio.Future[None]] = []
    returned: list[weakref.ref[Returned]] = []
    settled: list[bool] = []

    async def before_send(event: dict[str, Any]) -> Any:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        waiting.append(future)
        await future
        out = Returned(event, padding="x" * 1_000_000)
        returned.append(weakref.ref(out))
        return out

    lsquare = client(before_send=before_send)
    hook_done = lsquare._hook_done

    def noting(*args: Any) -> None:
        settled.append(True)
        hook_done(*args)

    monkeypatch.setattr(lsquare, "_hook_done", noting)
    lsquare.capture(turn())
    assert eventually(lambda: bool(waiting))
    ending_in_the_private_loops_last_pass(lsquare, waiting[0])
    assert eventually(lambda: settled == [True])
    assert eventually(lambda: (gc.collect(), returned[0]() is None)[1])


def test_an_async_identify_ending_in_the_private_loops_last_pass_settles_and_keeps_nothing() -> None:
    # Started outside any loop, it runs on the private one. shutdown() aborts it, too late: it has
    # returned in the same pass, and its run's future settles with what it gave.
    waiting: list[asyncio.Future[None]] = []
    returned: list[weakref.ref[Returned]] = []

    async def identify(given: Any) -> Any:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        waiting.append(future)
        await future
        out = Returned(padding="x" * 1_000_000)
        returned.append(weakref.ref(out))
        return out

    lsquare = client()
    internal = lsquare._lsquare_internal()
    run = internal.identify(identify, lambda signal: None, internal.origin())
    assert run is not None
    assert eventually(lambda: bool(waiting))
    ending_in_the_private_loops_last_pass(lsquare, waiting[0])
    assert isinstance(run.future.result(5), Returned)
    del run
    assert eventually(lambda: (gc.collect(), returned[0]() is None)[1])


# -- The call scope the wrapper uses ------------------------------------------------------------------


def test_inside_a_call_the_first_capture_is_its_agent_and_later_ones_are_linked_turns() -> None:
    transport = FakeTransport()
    lsquare = client(transport)
    internal = lsquare._lsquare_internal()
    scope = CallScope(call_id="call-1")
    token = internal.calls().set(scope)
    try:
        assert lsquare.current_call_id() == "call-1"
        lsquare.capture(turn(1, question="first"))
        lsquare.capture(turn(2, question="second"))
    finally:
        internal.calls().reset(token)
    assert lsquare.current_call_id() is None
    from lsquare._event import Captured, ShapedAgent

    # Read when it was captured: the scope keeps its IDs and what it read, not the app's objects.
    agent = scope.agent
    assert isinstance(agent, Captured) and isinstance(agent.snapshot, ShapedAgent)
    assert agent.snapshot.body["question"] == "first"
    lsquare.flush()
    assert [event["callId"] for event in transport.events] == ["call-1"]


def test_holds_at_most_100_of_the_turns_captured_inside_a_call_that_are_still_being_sent_each_until_it_is() -> None:
    held = Held()
    lsquare = LSquare(api_key=KEY, endpoint="http://localhost:1", _transport=held, logger=Recorder())
    var = lsquare._lsquare_internal().calls()
    scope = CallScope(call_id="call-5")
    token = var.set(scope)
    try:
        lsquare.capture(turn(0, question="the agent part"))
        for n in range(1, 102):
            lsquare.capture(turn(n))
    finally:
        var.reset(token)
    # The 101st goes as usual, only not waited for with the call's event.
    assert len(scope.turns) == 100
    held.release()
    # Each is sent, before this flush or while it waits: let go of once it is.
    result = lsquare.flush(timeout_ms=5_000)
    assert (result.pending, result.failed, len(held.acknowledged("turn"))) == (0, 0, 101)
    assert scope.turns == set()
    lsquare.shutdown(timeout_ms=0)


def test_concurrent_first_captures_in_one_call_make_one_agent() -> None:
    transport = FakeTransport()
    lsquare = client(transport)
    scope = CallScope(call_id="call-2")
    var = lsquare._lsquare_internal().calls()
    barrier = threading.Barrier(8)

    def work(n: int) -> None:
        context = contextvars.copy_context()

        def inside() -> None:
            var.set(scope)
            barrier.wait()
            lsquare.capture(turn(n))

        context.run(inside)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    lsquare.flush()
    assert scope.agent is not None
    assert len(transport.events) == 7


def test_flush_inside_a_call_marks_it_and_does_not_wait_on_itself() -> None:
    lsquare = client()
    scope = CallScope(call_id="call-3")
    var = lsquare._lsquare_internal().calls()
    token = var.set(scope)
    try:
        started = time.monotonic()
        lsquare.flush()
        assert time.monotonic() - started < 1
    finally:
        var.reset(token)
    assert scope.flush_requested


def test_internal_callback_runs_outside_the_call_one_level_deeper() -> None:
    lsquare = client()
    internal = lsquare._lsquare_internal()
    scope = CallScope(call_id="call-4")
    token = internal.calls().set(scope)
    try:
        assert internal.callback(lambda: lsquare.current_call_id()) is None
    finally:
        internal.calls().reset(token)


def test_record_builds_a_tool_call_event() -> None:
    transport = FakeTransport()
    lsquare = client(transport)
    lsquare._lsquare_internal().record(
        {
            "callId": "11111111-1111-4111-8111-111111111111",
            "capturedAt": "2026-10-29T00:00:00.000Z",
            "toolName": "search_orders",
            "status": "completed",
            "durationMs": 3,
            "ids": {},
            "diagnostics": False,
        }
    )
    lsquare.flush()
    assert eventually(lambda: len(transport.events) == 1)
    event = transport.events[0]
    assert event["kind"] == "tool_call" and event["toolName"] == "search_orders"


@pytest.mark.parametrize("value", ["", "x" * 600, 12])
def test_internal_valid_id(value: Any) -> None:
    assert client()._lsquare_internal().valid_id(value) is False
