"""Exit and fork, in subprocesses: `atexit` sends what's queued, with no thread able to start too,
and leaves no SDK thread writing to stderr as the interpreter finalizes; a forked child starts empty
and the parent sends its own events once; Pyodide is detected before any thread starts."""

from __future__ import annotations

import asyncio
import concurrent.futures
import io
import os
import subprocess
import sys
import textwrap
import threading
import time
import types
from typing import Any

import pytest

from lsquare import LSquare, LSquareError, _threads
from lsquare import _client as lsquare_client
from lsquare._runtime import StderrLogger
from lsquare._sending import Reply
from lsquare._threads import Pool, Scheduler

from helpers import FakeTransport, Ingestion, Recorder, ack, eventually, run_until



@pytest.fixture
def ingestion() -> Any:
    server = Ingestion()
    yield server
    server.close()


def run(code: str, timeout: float = 20) -> subprocess.CompletedProcess[str]:
    # The environment's own Python, so the child imports the lsquare installed there: the wheel in CI.
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)], capture_output=True, text=True, timeout=timeout)


def test_a_script_that_captures_and_exits_sends_its_events(ingestion: Ingestion) -> None:
    done = run(
        f"""
        from lsquare import LSquare
        lsquare = LSquare(api_key="lsq_test", endpoint="{ingestion.url}")
        for n in range(3):
            lsquare.capture({{"conversationId": "exit", "turnId": n}})
        """
    )
    assert done.returncode == 0, done.stderr
    assert len(ingestion.events()) == 3


def test_a_script_exiting_right_after_its_first_capture_still_sends_it(ingestion: Ingestion) -> None:
    # No sending thread has started when atexit runs: some Pythons (3.12.2) refuse to start one there.
    done = run(
        f"""
        from lsquare import LSquare
        LSquare(api_key="lsq_test", endpoint="{ingestion.url}").capture({{"conversationId": "now", "turnId": 1}})
        """
    )
    assert done.returncode == 0, done.stderr
    assert len(ingestion.events()) == 1


# Once the main thread has finished, some Pythons (3.12.2, not 3.10 or 3.14) refuse to start
# threads: in atexit handlers, and in non-daemon threads still running. These tests refuse them the
# same way on every version, from `_threads.start_thread`; CI's 3.12 is a later patch.

#: What such a Python raises.
REFUSED = "can't create new thread at interpreter shutdown"


@pytest.mark.parametrize("mode", ["thread", "atexit", "retry", "busy", "shutdown"])
def test_a_capture_once_the_main_thread_has_finished_is_sent(ingestion: Ingestion, mode: str) -> None:
    # From a non-daemon thread, and from an app's atexit handler (it runs before the client's);
    # one answered 503 first, so its retry is due during the exit flush; one queued behind a
    # request still in flight; and one an app's atexit handler sends with shutdown().
    if mode == "retry":
        ingestion.script.append((503, {}, b"{}"))
    if mode == "busy":
        # The first request is answered only once the second has arrived.
        ingestion.script.append(lambda: eventually(lambda: len(ingestion.requests) >= 2, timeout=10))
    done = run(
        f"""
        import atexit, threading, time
        from lsquare import LSquare, _threads
        start_thread = _threads.start_thread

        def refusing(target, name):
            if not threading.main_thread().is_alive():
                raise RuntimeError("{REFUSED}")
            start_thread(target, name)

        _threads.start_thread = refusing
        lsquare = LSquare(api_key="lsq_test", endpoint="{ingestion.url}", _random=lambda: 0.2)

        def late():
            lsquare.capture({{"conversationId": "late", "turnId": 2}})

        mode = "{mode}"
        if mode == "thread":
            threading.Thread(target=lambda: (threading.main_thread().join(), late())).start()
        elif mode == "shutdown":
            atexit.register(lambda: (late(), print("sent", lsquare.shutdown().sent, flush=True)))
        else:
            if mode == "busy":
                lsquare.capture({{"conversationId": "early", "turnId": 1}})
                for _ in range(500):
                    if lsquare._senders._busy:
                        break
                    time.sleep(0.01)
            atexit.register(late)
        """
    )
    assert done.returncode == 0, done.stderr
    sent = [event["conversationId"] for event in ingestion.events()]
    expected = {"retry": ["late", "late"], "busy": ["early", "late"]}.get(mode, ["late"])
    assert sorted(sent) == expected, (mode, done.stdout)
    if mode == "shutdown":
        assert "sent 1" in done.stdout, done.stdout


@pytest.mark.parametrize("starts", ["allowed", "refused"])
def test_a_shutdown_in_an_atexit_handler_that_runs_after_the_exit_flush_sends_within_its_own_timeout(ingestion: Ingestion, starts: str) -> None:
    # Registered before the client, the app's handler runs after the client's own exit flush (atexit
    # runs handlers last in, first out), once that flush's deadline has passed: the first request
    # is answered only once the second has arrived, so the flush waits for it until then.
    ingestion.script.append(lambda: eventually(lambda: len(ingestion.requests) >= 2, timeout=10))
    done = run(
        f"""
        import atexit, threading
        from lsquare import LSquare, _threads
        start_thread = _threads.start_thread

        def refusing(target, name):
            if not threading.main_thread().is_alive():
                raise RuntimeError("{REFUSED}")
            start_thread(target, name)

        if "{starts}" == "refused":
            _threads.start_thread = refusing

        def late():
            lsquare.capture({{"conversationId": "late", "turnId": 2}})
            result = lsquare.shutdown(timeout_ms=5000)
            print("sent", result.sent, "failed", result.failed, flush=True)

        atexit.register(late)
        lsquare = LSquare(api_key="lsq_test", endpoint="{ingestion.url}", flush_timeout_ms=300)
        lsquare.capture({{"conversationId": "early", "turnId": 1}})
        """
    )
    assert done.returncode == 0, done.stderr
    assert "sent 2 failed 0" in done.stdout, (done.stdout, done.stderr)
    # The first may be sent twice: posted only once the exit flush began, its request took that
    # flush's timeout and gave up before its answer came, then was sent again.
    sent = [event["conversationId"] for event in ingestion.events()]
    assert set(sent) == {"early", "late"} and sent.count("late") == 1, sent


# A SystemExit raised inside the SDK's own code, as a SIGTERM handler's `sys.exit(0)` raises it
# wherever the main thread is: the process exits, and the exit flush sends what came before.


def test_a_system_exit_inside_a_capture_s_hand_off_exits_and_leaves_nothing_unsent(ingestion: Ingestion) -> None:
    # It lands with the event queued and its first step not yet scheduled. Once the exit flush is
    # done, nothing is left in the queue: it was sent, or never queued.
    done = run(
        f"""
        import atexit
        from lsquare import LSquare
        atexit.register(lambda: print("left", len(lsquare._live), flush=True))
        lsquare = LSquare(api_key="lsq_test", endpoint="{ingestion.url}", on_error=lambda error, event=None: print(error.code, flush=True))
        for n in range(3):
            lsquare.capture({{"conversationId": "before", "turnId": n}})
        call_soon = lsquare._scheduler.call_soon
        fired = []

        def interrupted(fn):
            if not fired:
                fired.append(1)
                raise SystemExit(0)
            call_soon(fn)

        lsquare._scheduler.call_soon = interrupted
        lsquare.capture({{"conversationId": "interrupted", "turnId": 1}})
        print("not reached", flush=True)
        """
    )
    assert done.returncode == 0, done.stderr
    assert "not reached" not in done.stdout and "pending_after_flush" not in done.stdout, done.stdout
    assert "left 0" in done.stdout, done.stdout
    assert [event["conversationId"] for event in ingestion.events()].count("before") == 3


def test_a_system_exit_starting_the_scheduler_under_the_client_s_lock_exits_and_another_thread_s_capture_is_sent(
    ingestion: Ingestion,
) -> None:
    # It lands inside the scheduler's thread start, in a capture, under the client's lock, while
    # another request thread waits for that lock to capture too; the app lets that request finish
    # before it exits.
    done = run(
        f"""
        import threading, time
        from lsquare import LSquare, _threads
        _threads.IDLE_S = 0.05
        lsquare = LSquare(api_key="lsq_test", endpoint="{ingestion.url}", on_error=lambda error, event=None: print(error.code, flush=True))
        lsquare.capture({{"conversationId": "before", "turnId": 1}})
        # Sent, before the flush or during it: the flush counts it only in the second case.
        result = lsquare.flush(timeout_ms=5000)
        assert (result.failed, result.pending, len(lsquare._live)) == (0, 0, 0)
        # The scheduler's thread goes idle and exits: the next capture starts another.
        for _ in range(500):
            if not any(thread.name == "lsquare-scheduler" for thread in threading.enumerate()):
                break
            time.sleep(0.01)
        other = threading.Thread(target=lambda: lsquare.capture({{"conversationId": "other", "turnId": 2}}))
        start_thread = _threads.start_thread
        fired = []

        def interrupted(target, name):
            if name == "lsquare-scheduler" and not fired:
                fired.append(1)
                other.start()
                raise SystemExit(0)
            start_thread(target, name)

        _threads.start_thread = interrupted
        try:
            lsquare.capture({{"conversationId": "interrupted", "turnId": 3}})
        finally:
            other.join()
        print("not reached", flush=True)
        """
    )
    assert done.returncode == 0, done.stderr
    assert "not reached" not in done.stdout and "pending_after_flush" not in done.stdout, done.stdout
    assert sorted(event["conversationId"] for event in ingestion.events()) == ["before", "other"]


def test_a_system_exit_as_a_hook_settles_on_the_main_thread_s_loop_ends_the_loop_and_what_came_before_is_sent(
    ingestion: Ingestion,
) -> None:
    done = run(
        f"""
        import asyncio, sys
        from lsquare import LSquare
        hooked = []

        async def before_send(event):
            hooked.append(event["conversationId"])
            return event

        # The interrupted hook returned in time, but what it returned is never read: it fails at its
        # finalization ceiling (twice its deadline), well within the exit's flush.
        lsquare = LSquare(api_key="lsq_test", endpoint="{ingestion.url}", before_send=before_send, before_send_timeout_ms=200,
                          on_error=lambda error, event=None: print(error.code, flush=True))

        async def main():
            for n in range(3):
                lsquare.capture({{"conversationId": "before", "turnId": n}})
            assert (await lsquare.aflush(timeout_ms=5000)).sent == 3
            submit = lsquare._hooks.submit
            fired = []

            def interrupted(fn):
                # Called from the hook's settle, on this loop's thread, handing its result to a
                # hook thread to read.
                if not fired and sys._getframe(1).f_code.co_name == "settle":
                    fired.append(1)
                    raise SystemExit(0)
                return submit(fn)

            lsquare._hooks.submit = interrupted
            lsquare.capture({{"conversationId": "interrupted", "turnId": 1}})
            for _ in range(500):
                await asyncio.sleep(0.01)
            print("not reached", flush=True)

        asyncio.run(main())
        """
    )
    assert done.returncode == 0, done.stderr
    assert "not reached" not in done.stdout and "pending_after_flush" not in done.stdout, done.stdout
    # Reported as unfinished processing, never as a late hook, once: the report reaches on_error
    # wherever the callback thread can start at exit. A Python that refuses thread starts then
    # (3.12.2) may drop it, never report anything else.
    if refuses_thread_starts_at_exit():
        assert set(done.stdout.split()) <= {"hook_failed"}, done.stdout
    else:
        assert done.stdout.split() == ["hook_failed"], done.stdout
    assert [event["conversationId"] for event in ingestion.events()].count("before") == 3


def refuses_thread_starts_at_exit() -> bool:
    """Whether this Python refuses to start a thread once the main thread has finished, in an
    `atexit` handler, as 3.12.2 does and 3.10 and 3.14 don't: asked of the interpreter itself."""
    done = run(
        """
        import atexit, threading

        def start():
            try:
                threading.Thread(target=lambda: None).start()
            except RuntimeError:
                print("refused", flush=True)

        atexit.register(start)
        """
    )
    assert done.returncode == 0, done.stderr
    return "refused" in done.stdout


def test_the_exit_flush_writes_its_own_report_before_the_interpreter_finalizes() -> None:
    # A daemon thread writing to stderr while the interpreter finalizes aborts it ("could not
    # acquire lock for <stderr> at interpreter shutdown"): a 2 MB log line keeps the callback thread
    # writing as the exit flush, held up by a stalled ingestion, ends.
    server = Ingestion()
    stall = threading.Event()
    server.script.extend([lambda: stall.wait(10)] * 3)
    try:
        done = run(
            f"""
            import sys
            from lsquare import LSquare

            class Loud:
                def info(self, message):
                    sys.stderr.write(message + "\\n")

                def warn(self, message):
                    sys.stderr.write(message + "x" * 2_000_000 + "\\n")

            lsquare = LSquare(api_key="lsq_test", endpoint="{server.url}", flush_timeout_ms=300, logger=Loud())
            lsquare.capture({{"conversationId": "c", "turnId": 1}})
            """
        )
        assert done.returncode == 0, done.stderr[:500]
        assert "pending_after_flush" in done.stderr and "x" * 2_000_000 in done.stderr
    finally:
        stall.set()
        server.close()


def test_the_exit_flush_waits_for_its_report_to_be_written_then_nothing_more_is_logged() -> None:
    lines: list[str] = []

    class Slow:
        def info(self, message: str) -> None:
            lines.append(message)

        def warn(self, message: str) -> None:
            time.sleep(0.2)
            lines.append(message)

    stall = threading.Event()

    def respond(events: list[dict[str, Any]]) -> Reply:
        stall.wait(5)
        return Reply(200, None, ack(*[event["eventId"] for event in events]))

    lsquare = LSquare(api_key="lsq_test", endpoint="http://localhost:1", logger=Slow(), flush_timeout_ms=200, _transport=FakeTransport(respond))
    try:
        lsquare.capture({"conversationId": "c", "turnId": 1})
        lsquare._exit_flush()
        # Written before the exit handler returned, slow logger and all.
        assert any("pending_after_flush" in line for line in lines), lines
        lsquare._log("warn", "[lsquare] late")
        lsquare._report(LSquareError("late", "reported after the exit flush"))
        time.sleep(0.3)
        assert not any("late" in line for line in lines), lines
    finally:
        stall.set()


def test_after_the_exit_flush_an_aborted_hook_s_listener_and_its_report_are_not_run() -> None:
    # Quiet from the exit flush on, the signal thread included: a listener could still be running,
    # writing, as the interpreter finalizes.
    started = threading.Event()
    signals: list[Any] = []
    ran: list[str] = []
    recorder = Recorder()

    def before_send(event: Any, signal: Any) -> Any:
        signal.add_listener(lambda: ran.append("listener"))
        signals.append(signal)
        started.set()
        signal.wait(5)
        return event

    lsquare = LSquare(api_key="lsq_test", endpoint="http://localhost:1", before_send=before_send, before_send_timeout_ms=60_000,
                      logger=recorder, on_error=recorder.on_error, _transport=FakeTransport())
    # Nothing queued yet: the exit flush only makes the client quiet.
    lsquare._exit_flush()
    lsquare.capture({"conversationId": "c", "turnId": 1})
    assert started.wait(5)
    lsquare.shutdown(timeout_ms=0)
    assert signals[0].aborted
    assert lsquare._signals.drain(5) and lsquare._callbacks.drain(5)
    assert ran == [] and recorder.errors == []


def test_the_exit_flush_waits_for_an_async_logger_and_runs_nothing_handed_over_after_it() -> None:
    lines: list[str] = []

    class Async:
        async def info(self, message: str) -> None:
            lines.append(message)

        async def warn(self, message: str) -> None:
            await asyncio.sleep(0.2)
            lines.append(message)

    stall = threading.Event()

    def respond(events: list[dict[str, Any]]) -> Reply:
        stall.wait(5)
        return Reply(200, None, ack(*[event["eventId"] for event in events]))

    lsquare = LSquare(api_key="lsq_test", endpoint="http://localhost:1", logger=Async(), flush_timeout_ms=200, _transport=FakeTransport(respond))
    try:
        lsquare.capture({"conversationId": "c", "turnId": 1})
        lsquare._exit_flush()
        # Its coroutine ran on a loop after its job returned; the exit waited for it all the same.
        assert any("pending_after_flush" in line for line in lines), lines
        # What abort listeners, logs and reports go through: nothing handed over now runs.
        ran = threading.Event()
        lsquare._callback(ran.set)
        assert not ran.wait(0.3)
    finally:
        stall.set()


def test_a_cancelled_async_logger_is_let_go_without_a_traceback(capfd: Any) -> None:
    class Cancelled:
        async def info(self, message: str) -> None:
            raise asyncio.CancelledError()

        async def warn(self, message: str) -> None:
            raise asyncio.CancelledError()

    lsquare = LSquare(api_key="lsq_test", endpoint="http://localhost:1", logger=Cancelled(), _transport=FakeTransport())
    lsquare._log("warn", "[lsquare] cancelled")
    lsquare._exit_flush()
    assert lsquare._callback_futures == {}
    assert "exception calling callback" not in capfd.readouterr().err


def test_the_exit_waits_for_no_async_logger_left_pending_by_a_loop_the_app_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    # A grace long enough to notice: an exit that waited for the logger, which that loop will never
    # run on, would fail the test.
    monkeypatch.setattr(lsquare_client, "EXIT_LOG_GRACE_S", 30.0)
    started = threading.Event()

    class Pending:
        def info(self, message: str) -> None:
            pass

        async def warn(self, message: str) -> None:
            started.set()
            await asyncio.get_running_loop().create_future()

    lsquare = LSquare(api_key="lsq_test", endpoint="http://localhost:1", logger=Pending(), _transport=FakeTransport())
    loop = asyncio.new_event_loop()
    run_until(loop, lambda: lsquare._log("warn", "[lsquare] pending"), started.is_set)
    loop.close()
    exiting = time.monotonic()
    lsquare._exit_flush()
    assert time.monotonic() - exiting < 10
    assert (lsquare._callback_futures, lsquare._log_limit.held) == ({}, 0)


def test_the_default_logger_writes_to_stderrs_descriptor_or_to_a_replaced_stderr(capfd: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    StderrLogger().warn("[lsquare] to the descriptor")
    assert capfd.readouterr().err == "[lsquare] to the descriptor\n"
    replaced = io.StringIO()
    monkeypatch.setattr(sys, "stderr", replaced)
    StderrLogger().warn("[lsquare] to the stream")
    assert replaced.getvalue() == "[lsquare] to the stream\n"


def test_a_pool_drains_once_its_jobs_are_done_and_gives_up_at_its_timeout() -> None:
    pool = Pool("test", 1)
    release = threading.Event()

    def job() -> None:
        release.wait(5)

    pool.submit(job)
    assert pool.drain(0.1) is False
    release.set()
    assert pool.drain(5) is True
    assert pool.drain(0) is True


def test_a_burst_runs_at_once_beside_a_worker_that_is_idle() -> None:
    # Each idle worker takes one job: a burst beyond them starts workers of its own, instead of
    # queueing behind the one idle worker, which hasn't even woken yet.
    pool = Pool("test", 4)
    warm = threading.Event()
    pool.submit(warm.set)
    assert warm.wait(1)

    def idle() -> int:
        with pool._lock:
            return pool._idle

    assert eventually(lambda: idle() == 1)
    barrier = threading.Barrier(4, timeout=2)
    met: list[bool] = []

    def job() -> None:
        try:
            barrier.wait()
            met.append(True)
        except threading.BrokenBarrierError:
            met.append(False)

    for _ in range(4):
        pool.submit(job)
    assert pool.drain(5)
    assert met == [True] * 4


def test_an_idle_client_goes_quiet_at_exit_too() -> None:
    recorder = Recorder()
    lsquare = LSquare(api_key="lsq_test", endpoint="http://localhost:1", logger=recorder, _transport=FakeTransport())
    lsquare._exit_flush()
    lsquare._log("warn", "[lsquare] late")
    time.sleep(0.2)
    assert not any("late" in line for _, line in recorder.lines)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="no fork here")
def test_a_forked_child_starts_empty_and_nothing_is_sent_twice(ingestion: Ingestion) -> None:
    done = run(
        f"""
        import os, threading
        from lsquare import LSquare
        gate = threading.Event()
        lsquare = LSquare(api_key="lsq_test", endpoint="{ingestion.url}")
        lsquare.capture({{"conversationId": "parent", "turnId": 1}})
        pid = os.fork()
        if pid == 0:
            lsquare.capture({{"conversationId": "child", "turnId": 2}})
            result = lsquare.flush(timeout_ms=5000)
            os._exit(0 if (result.failed, result.pending) == (0, 0) else 3)
        _, status = os.waitpid(pid, 0)
        print("child", os.waitstatus_to_exitcode(status))
        result = lsquare.flush(timeout_ms=5000)
        print("parent", result.failed + result.pending)
        """
    )
    assert done.returncode == 0, done.stderr
    assert "child 0" in done.stdout and "parent 0" in done.stdout
    ids = sorted(event["conversationId"] for event in ingestion.events())
    assert ids == ["child", "parent"]


@pytest.mark.skipif(not hasattr(os, "fork"), reason="no fork here")
def test_a_child_forked_after_a_shutdown_gives_its_requests_their_own_timeout(ingestion: Ingestion) -> None:
    # A sync shutdown() sends within a deadline of its own; the child's client, emptied by the fork,
    # isn't held to its parent's. The clock stands in for the child running past that deadline.
    done = run(
        f"""
        import os
        from lsquare import LSquare
        now = [1_000.0]
        lsquare = LSquare(api_key="lsq_test", endpoint="{ingestion.url}", _clock=lambda: now[0])
        lsquare.capture({{"conversationId": "parent", "turnId": 1}})
        print("parent", lsquare.shutdown(timeout_ms=5000).sent, flush=True)
        pid = os.fork()
        if pid == 0:
            now[0] += 60
            lsquare.capture({{"conversationId": "child", "turnId": 2}})
            result = lsquare.flush(timeout_ms=5000)
            os._exit(0 if (result.sent, result.pending, result.failed) == (1, 0, 0) else 3)
        _, status = os.waitpid(pid, 0)
        print("child", os.waitstatus_to_exitcode(status))
        """
    )
    assert done.returncode == 0, done.stderr
    assert "parent 1" in done.stdout and "child 0" in done.stdout, done.stdout
    assert sorted(event["conversationId"] for event in ingestion.events()) == ["child", "parent"]


# -- No thread able to start, in this process ----------------------------------------------------


def refuse_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    """From now on no SDK thread starts, as once the main thread has finished on some Pythons."""

    def refused(target: Any, name: str) -> None:
        raise RuntimeError(REFUSED)

    monkeypatch.setattr(_threads, "start_thread", refused)


def answers(*statuses: int) -> FakeTransport:
    """A transport answering these statuses in turn, then acknowledging every event."""
    left = list(statuses)

    def respond(events: list[dict[str, Any]]) -> Reply:
        status = left.pop(0) if left else 200
        return Reply(status, None, ack(*[event["eventId"] for event in events]) if status == 200 else "{}")

    return FakeTransport(respond)


def joined(fn: Any, timeout: float = 10.0) -> Any:
    """Runs `fn` on a daemon thread joined within `timeout` seconds: one still running fails the
    test, rather than hanging the suite."""
    box: dict[str, Any] = {}
    thread = threading.Thread(target=lambda: box.update(out=fn()), daemon=True)
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), "still waiting"
    return box.get("out")


def test_a_batch_no_thread_can_send_stays_queued_its_attempt_unspent_and_the_exit_flush_sends_it(monkeypatch: pytest.MonkeyPatch) -> None:
    # A capture once no thread can start: its steps run inline, and no sending thread takes it.
    refuse_starts(monkeypatch)
    transport = FakeTransport()
    lsquare = LSquare(api_key="lsq_test", endpoint="http://localhost:1", logger=Recorder(), _transport=transport)
    lsquare.capture({"conversationId": "c", "turnId": 1})
    (entry,) = lsquare._live.values()
    # No request was made: not a network failure, so no attempt spent and no retry timer.
    assert (entry.stage, entry.attempts, entry.retry_at, lsquare._active_requests) == ("queued", 0, None, 0)
    lsquare._exit_flush()
    assert len(transport.events) == 1 and not lsquare._live


def test_the_exit_flush_sends_a_retry_due_before_its_deadline_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    # Answered 503: its retry's timer, 50 ms on, has no scheduler thread to fire it. The exiting
    # thread, which took the scheduler's place, does.
    refuse_starts(monkeypatch)
    transport = answers(503)
    lsquare = LSquare(api_key="lsq_test", endpoint="http://localhost:1", logger=Recorder(), flush_timeout_ms=5_000,
                      _random=lambda: 0.2, _transport=transport)
    lsquare.capture({"conversationId": "c", "turnId": 1})
    lsquare._exit_flush()
    assert len(transport.requests) == 2 and not lsquare._live


def test_a_shutdown_no_thread_can_help_sends_and_retries_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    # An app's own atexit handler capturing, then shutting the client down.
    refuse_starts(monkeypatch)
    transport = answers(503)
    lsquare = LSquare(api_key="lsq_test", endpoint="http://localhost:1", logger=Recorder(), _random=lambda: 0.2, _transport=transport)
    lsquare.capture({"conversationId": "c", "turnId": 1})
    result = lsquare.shutdown(timeout_ms=5_000)
    assert (result.sent, result.failed, len(transport.requests)) == (1, 0, 2)


@pytest.mark.parametrize("scheduler", ["running", "gone"])
def test_at_exit_a_batch_and_its_retry_do_not_wait_behind_a_request_in_flight(monkeypatch: pytest.MonkeyPatch, scheduler: str) -> None:
    # One request is held in flight, then no thread can start: the next batch would wait behind it
    # in the sending pool, and so would that batch's retry. Both are sent beside it instead, by the
    # scheduler's thread, or, once that has gone, by the exiting thread. The retry's 200 lets the
    # held request go.
    if scheduler == "gone":
        monkeypatch.setattr(_threads, "IDLE_S", 0.05)
    in_flight = threading.Event()
    release = threading.Event()
    seen: list[str] = []

    def respond(events: list[dict[str, Any]]) -> Reply:
        seen.append(events[0]["conversationId"])
        if seen[-1] == "first":
            in_flight.set()
            release.wait(10)
        elif seen.count("second") == 1:
            return Reply(503, None, "{}")
        else:
            release.set()
        return Reply(200, None, ack(*[event["eventId"] for event in events]))

    lsquare = LSquare(api_key="lsq_test", endpoint="http://localhost:1", logger=Recorder(), flush_timeout_ms=5_000,
                      _random=lambda: 0.0, _transport=FakeTransport(respond))
    try:
        lsquare.capture({"conversationId": "first", "turnId": 1})
        assert in_flight.wait(5)
        if scheduler == "gone":
            # Idle, its thread exits: from here nothing runs the client's steps but the caller.
            assert eventually(lambda: not lsquare._scheduler._owner)
        refuse_starts(monkeypatch)
        lsquare.capture({"conversationId": "second", "turnId": 2})
        lsquare._exit_flush()
        assert seen == ["first", "second", "second"]
        assert release.is_set() and not lsquare._live
    finally:
        release.set()


def test_the_exit_flush_sends_batch_after_batch_in_order_without_nesting_deeper(monkeypatch: pytest.MonkeyPatch) -> None:
    # Sent inline, each request's outcome sent the next request from inside it, a stack frame
    # deeper each time: a long queue at exit could reach the recursion limit. Each capture's batch,
    # refused, went back to the head of the queue: the order is the capture's.
    depths: list[int] = []

    def respond(events: list[dict[str, Any]]) -> Reply:
        frame: types.FrameType | None = sys._getframe()
        depth = 0
        while frame is not None:
            frame, depth = frame.f_back, depth + 1
        depths.append(depth)
        return Reply(200, None, ack(*[event["eventId"] for event in events]))

    refuse_starts(monkeypatch)
    transport = FakeTransport(respond)
    lsquare = LSquare(api_key="lsq_test", endpoint="http://localhost:1", logger=Recorder(), _transport=transport)
    for n in range(300):
        lsquare.capture({"conversationId": "c", "turnId": n})
    lsquare._exit_flush()
    # Three batches of 100, each sent from the same depth.
    assert len(depths) == 3 and len(set(depths)) == 1, depths
    assert [event["turnId"] for event in transport.events] == [str(n) for n in range(300)]
    assert not lsquare._live


def test_a_clock_that_stands_still_does_not_hold_a_shutdown_no_thread_can_help(monkeypatch: pytest.MonkeyPatch) -> None:
    # The client's clock decides when timers come due, and stands still here: the exiting thread's
    # wait is bounded on the monotonic clock as well. The retry, 125 ms on, never comes due.
    refuse_starts(monkeypatch)
    transport = FakeTransport(lambda events: Reply(503, None, "{}"))
    lsquare = LSquare(api_key="lsq_test", endpoint="http://localhost:1", logger=Recorder(), _clock=lambda: 1_000.0,
                      _random=lambda: 0.5, _transport=transport)
    lsquare.capture({"conversationId": "c", "turnId": 1})
    result = joined(lambda: lsquare.shutdown(timeout_ms=200))
    assert (result.sent, result.failed, len(transport.requests)) == (0, 1, 1)


def test_the_exit_flush_goes_on_once_a_call_s_event_is_built_on_another_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    # The exiting thread runs the scheduler's steps while it waits for the call: the call's
    # future, finished on another thread, wakes it, with nothing scheduled.
    refuse_starts(monkeypatch)
    lsquare = LSquare(api_key="lsq_test", endpoint="http://localhost:1", logger=Recorder(), flush_timeout_ms=10_000,
                      _transport=FakeTransport())
    built: concurrent.futures.Future[None] = concurrent.futures.Future()
    lsquare._track("call", built)
    condition = lsquare._scheduler._lock
    real_wait = condition.wait
    waiting = threading.Event()

    def wait(timeout: float | None = None) -> bool:
        waiting.set()
        return real_wait(timeout)

    def build() -> None:
        waiting.wait(5)
        built.set_result(None)

    monkeypatch.setattr(condition, "wait", wait)
    builder = threading.Thread(target=build, daemon=True)
    builder.start()
    began = time.monotonic()
    lsquare._exit_flush()
    builder.join(5)
    assert built.done() and time.monotonic() - began < 5


# -- The thread that takes the scheduler over, and the pools at exit --------------------------------


def test_a_thread_that_takes_the_scheduler_over_runs_what_comes_due_before_its_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    scheduler = Scheduler()
    try:
        # A thread can start: that one runs the steps.
        assert not scheduler.take_over() and not scheduler.driving()
    finally:
        scheduler.stop()
    refuse_starts(monkeypatch)
    scheduler = Scheduler()
    assert scheduler.take_over() and scheduler.driving()
    ran: list[tuple[str, str]] = []

    def note(what: str) -> None:
        ran.append((what, threading.current_thread().name))

    def due() -> None:
        note("due")
        scheduler.call_soon(lambda: note("its step"))

    other: list[bool] = []

    def from_another_thread() -> None:
        other.append(scheduler.driving())
        scheduler.call_soon(lambda: note("other's"))

    scheduler.call_later(0.0, due)
    scheduler.call_later(60.0, lambda: note("later"))
    elsewhere = threading.Thread(target=from_another_thread, name="other")
    elsewhere.start()
    elsewhere.join(5)
    # Scheduled on another thread, it waits for this one rather than running there, inline.
    assert other == [False] and ran == []
    scheduler.run_until(scheduler._clock() + 0.05, lambda: False)
    here = threading.current_thread().name
    assert ran == [("other's", here), ("due", here), ("its step", here)]
    scheduler.give_back()
    assert not scheduler.driving()
    # Given back, with no thread able to start: inline, where it's scheduled, as before.
    scheduler.call_soon(lambda: note("after"))
    assert ran[-1] == ("after", here)


def test_run_until_reads_done_under_the_lock_a_wake_takes(monkeypatch: pytest.MonkeyPatch) -> None:
    # The wait is ended on another thread just after the exiting thread read done(), before it
    # waits: the wake that follows still reaches it.
    refuse_starts(monkeypatch)
    scheduler = Scheduler()
    assert scheduler.take_over()
    ended = threading.Event()

    def end() -> None:
        ended.set()
        scheduler.wake()

    reads: list[bool] = []

    def done() -> bool:
        reads.append(ended.is_set())
        if len(reads) == 1:
            threading.Thread(target=end, daemon=True).start()
            assert eventually(ended.is_set)
            return False
        return reads[-1]

    began = time.monotonic()
    scheduler.run_until(scheduler._clock() + 10, done)
    scheduler.give_back()
    assert reads == [False, True] and time.monotonic() - began < 5


def test_at_exit_a_pool_hands_back_jobs_behind_a_busy_worker_and_refuses_later_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = Pool("test", 2)
    busy = threading.Event()
    release = threading.Event()
    def held() -> None:
        busy.set()
        release.wait(10)

    try:
        assert pool.submit(held)
        assert busy.wait(5)
        refuse_starts(monkeypatch)

        def behind() -> None:
            pass

        # While the process runs, a job waits behind the busy worker, for it to take once done.
        assert pool.submit(behind)
        assert pool.resume() == []
        # At exit that worker may be held past the end: the job comes back, and a later one is
        # refused.
        assert pool.resume(at_exit=True) == [behind]
        assert not pool.submit(lambda: None) and pool.resume(at_exit=True) == []
        release.set()

        def idle() -> int:
            with pool._lock:
                return pool._idle

        assert eventually(lambda: idle() == 1)
        # An idle worker still takes one.
        ran = threading.Event()
        assert pool.submit(ran.set) and ran.wait(5)
    finally:
        release.set()
        pool.stop()


def test_pyodide_is_detected_before_any_thread_starts() -> None:
    done = run(
        """
        import sys, threading
        sys.platform = "emscripten"
        from lsquare import LSquare
        before = threading.active_count()
        lsquare = LSquare(api_key="lsq_test")
        lsquare.capture({"conversationId": "c", "turnId": 1})
        print(lsquare._mode, threading.active_count() - before)
        """
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.split()[:2] == ["disabled", "0"]
