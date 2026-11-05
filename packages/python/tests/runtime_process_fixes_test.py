"""The runtime fixes of runtime_fixes_test.py that need a process of their own: the exit flush with
`flush_timeout_ms=0`, the SDK's threads under Python 3.14's thread context inheritance, a
disabled client forked while its callback thread's lock is held, and the one fork handler every
client shares. Each fails before its fix unless it says it's a guard; none orders anything by
sleeping."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest


def run(code: str, *flags: str, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    # The environment's own Python, so the child imports the lsquare installed there: the wheel in
    # CI. The tests' folder is its argument, for the helpers.
    return subprocess.run(
        [sys.executable, *flags, "-c", textwrap.dedent(code), os.path.dirname(__file__)], capture_output=True, text=True, timeout=timeout
    )


# -- py-runtime-9: the exit flush waits 2 s when flush() doesn't wait ------------------------------------


def test_with_flush_timeout_ms_0_the_exit_flush_still_sends_what_is_queued_as_shutdown_would() -> None:
    # One request at a time: the first is held until the exit flush has set its deadline, so the
    # second, queued behind it, goes out under that deadline. With 0 it went with no time to send.
    done = run(
        """
        import atexit, json, sys, threading
        sys.path.insert(0, sys.argv[1])
        from lsquare import LSquare
        from lsquare._sending import Reply
        from helpers import ack

        sent, release, entered = [], threading.Event(), threading.Event()

        def transport(body, timeout, abort):
            events = json.loads(body)["events"]
            entered.set()
            if events[0]["turnId"] == "1":
                release.wait(10)
            sent.append(events[0]["turnId"])
            return Reply(200, None, ack(*[event["eventId"] for event in events]))

        # Registered before the client's exit flush, so it runs after it.
        atexit.register(lambda: print("sent", *sent, flush=True))
        lsquare = LSquare(api_key="lsq_test", endpoint="http://localhost:1", _transport=transport, flush_timeout_ms=0,
                          max_concurrent_requests=1)
        resume = lsquare._resume_at_exit

        def resume_at_exit():
            # The exit flush, its deadline set: the first request answers now.
            release.set()
            resume()

        lsquare._resume_at_exit = resume_at_exit
        lsquare.capture({"conversationId": "c", "turnId": 1})
        assert entered.wait(10)
        lsquare.capture({"conversationId": "c", "turnId": 2})
        """
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "sent 1 2", done.stdout + done.stderr


# -- py-runtime-14: the SDK's threads keep no request's context when threads inherit contexts ------------


@pytest.mark.skipif(sys.version_info < (3, 14), reason="Python 3.14 lets threads inherit contexts")
def test_the_sdk_s_threads_hold_no_request_s_context_when_threads_inherit_contexts() -> None:
    # With inheritance on (free-threaded 3.14's default), a Thread started without a context of its
    # own keeps a copy of the starting thread's for its whole life: the scheduler's, which idles for
    # 30 s, here started by the capture inside the request.
    done = run(
        """
        import contextvars, gc, sys, weakref
        sys.path.insert(0, sys.argv[1])
        from helpers import FakeTransport
        from lsquare import LSquare

        class Request:
            pass

        current = contextvars.ContextVar("current_request")
        transport = FakeTransport()
        lsquare = LSquare(api_key="lsq_test", endpoint="http://localhost:1", _transport=transport)
        request = Request()
        reference = weakref.ref(request)
        token = current.set(request)
        lsquare.capture({"conversationId": "c", "turnId": 1})
        lsquare.flush(timeout_ms=5000)
        assert len(transport.events) == 1
        current.reset(token)
        del request, token
        gc.collect()
        print("kept" if reference() is not None else "let go")
        """,
        "-X",
        "thread_inherit_context=1",
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "let go"


# -- py-runtime-15: every client is reset in a forked child, a disabled one included --------------------


@pytest.mark.skipif(not hasattr(os, "fork"), reason="no fork here")
@pytest.mark.parametrize(
    "options",
    ["", 'api_key="lsq_test", endpoint="http://ingest.example.com"', 'api_key="lsq_test", before_send=Unreadable()'],
    ids=["no key", "an insecure endpoint", "an option whose read fails"],
)
def test_a_disabled_client_forked_while_its_callback_thread_s_lock_is_held_shuts_down_in_the_child(options: str) -> None:
    # A disabled client's warning starts its callback thread; a thread of the parent's holds that
    # pool's lock at the fork, as the worker does for a moment as it takes or ends a job. The child,
    # which has no such thread, then waited on it forever.
    done = run(
        f"""
        import os, signal, threading, time
        from lsquare import LSquare

        class Quiet:
            def info(self, message): pass
            def warn(self, message): pass

        class Unreadable:
            def __call__(self, *args): return args[0]
            def __getattr__(self, name): raise RuntimeError(name)

        lsquare = LSquare({options + ", " if options else ""}logger=Quiet())
        assert repr(lsquare) == "<LSquare disabled>"
        assert lsquare._callbacks.drain(5)
        held, release = threading.Event(), threading.Event()

        def hold():
            with lsquare._callbacks._lock:
                held.set()
                release.wait()

        threading.Thread(target=hold, daemon=True).start()
        held.wait()
        pid = os.fork()
        if pid == 0:
            try:
                lsquare.shutdown(timeout_ms=0)
            except BaseException:
                os._exit(1)
            os._exit(0)
        release.set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            finished, status = os.waitpid(pid, os.WNOHANG)
            if finished:
                print("child", os.waitstatus_to_exitcode(status))
                break
            time.sleep(0.01)
        else:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            print("child hung")
        """
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "child 0", done.stdout + done.stderr


# Checker's fixes, 31 October 2026: a registration can't be undone, so one per client stayed for the
# life of the process (about 600 bytes each), a disabled client's too, and every fork ran them all.


@pytest.mark.skipif(not hasattr(os, "register_at_fork"), reason="no fork here")
def test_clients_register_no_fork_handler_of_their_own_and_a_collected_one_leaves_nothing_behind() -> None:
    done = run(
        """
        import gc, os, time
        registered = []
        register = os.register_at_fork

        def counting(**handlers):
            registered.append(handlers)
            register(**handlers)

        os.register_at_fork = counting
        from lsquare import LSquare, _client

        class Quiet:
            def info(self, message): pass
            def warn(self, message): pass

        at_import = len(registered)
        kinds = [{}, {"api_key": "lsq_test", "endpoint": "http://ingest.example.com"}, {"dry_run": True},
                 {"api_key": "lsq_test", "endpoint": "http://localhost:1"}]
        for options in kinds:
            for _ in range(50):
                lsquare = LSquare(logger=Quiet(), **options)
                # A disabled client's warning job holds it until it has run.
                assert lsquare._callbacks.drain(5)
                lsquare.shutdown(timeout_ms=0)
        print("registered", len(registered) - at_import, flush=True)
        del lsquare

        def alive():
            return sum(1 for _, reference in list(_client._forkable.values()) if reference() is not None)

        # Until each pool's thread, stopped by shutdown(), has let go of its last job.
        deadline = time.monotonic() + 10
        gc.collect()
        while alive() and time.monotonic() < deadline:
            time.sleep(0.01)
            gc.collect()
        # Held by references with no callback, which making clients drops once they're dead: a
        # few dozen at most are kept, never one for each client made.
        callbacks = sum(1 for _, reference in list(_client._forkable.values()) if reference.__callback__ is not None)
        print("held", alive(), "callbacks", callbacks, "kept", len(_client._forkable) < 50, flush=True)
        """
    )
    # Before: 200 more, one for each client, disabled (no key, an insecure endpoint) or not.
    assert done.stdout.split()[:2] == ["registered", "0"], done.stdout + done.stderr
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["registered", "0", "held", "0", "callbacks", "0", "kept", "True"], done.stdout + done.stderr


# Codex's first review, 31 October 2026: the registry was a WeakValueDictionary, whose callback runs as
# a client is collected (a signal handler's exit raised there is only reported), and whose values()
# on Python 3.10 to 3.12 iterate the live dict (a client made meanwhile ends the iteration).


@pytest.mark.parametrize("kind", ["enabled", "disabled"])
def test_a_signal_handler_s_exit_while_a_client_is_collected_ends_the_process(kind: str) -> None:
    # The signal arrives in the first Python code to run as the client is collected: there is none.
    done = run(
        f"""
        import gc, signal, sys, weakref
        sys.path.insert(0, sys.argv[1])
        from helpers import FakeTransport
        from lsquare import LSquare

        class Quiet:
            def info(self, message): pass
            def warn(self, message): pass

        unraisable = []
        sys.unraisablehook = lambda raised: unraisable.append(type(raised.exc_value).__name__)

        def exits(signum, frame):
            sys.exit(23)

        signal.signal(signal.SIGUSR1, exits)
        if {kind!r} == "enabled":
            lsquare = LSquare(api_key="lsq_test", endpoint="http://localhost:1", _transport=FakeTransport(), logger=Quiet())
        else:
            lsquare = LSquare(logger=Quiet())
            assert lsquare._callbacks.drain(5)
        gone = weakref.ref(lsquare)
        gc.collect()
        ran = []

        def profile(frame, event, arg):
            if event == "call" and not ran:
                ran.append(frame.f_code.co_name)
                signal.raise_signal(signal.SIGUSR1)

        sys.setprofile(profile)
        del lsquare
        gc.collect()
        sys.setprofile(None)
        print("collected", gone() is None, "ran", ran, "unraisable", unraisable, flush=True)
        signal.raise_signal(signal.SIGUSR1)
        print("still running", flush=True)
        """
    )
    # Before: "ran ['remove'] unraisable ['SystemExit']", and the process went on.
    assert done.stdout.splitlines() == ["collected True ran [] unraisable []"], done.stdout + done.stderr
    assert done.returncode == 23, done.stderr


@pytest.mark.skipif(not hasattr(os, "fork"), reason="no fork here")
def test_clients_a_signal_handler_makes_while_a_fork_resets_the_clients_stop_no_client_s_reset() -> None:
    # A real signal at every step of the reset's own code, each making a client. Before, on 3.10 to
    # 3.12: "dictionary changed size during iteration", raised before any client was reset.
    done = run(
        """
        import os, signal, sys
        resetting, fired, made = [], [], []

        def profile(frame, event, arg):
            if frame.f_code is reset:
                if event == "call":
                    resetting.append(True)
                elif event == "return":
                    resetting.clear()
                    return
            if resetting and len(fired) < 40:
                fired.append(event)
                signal.raise_signal(signal.SIGUSR1)

        # Before lsquare's own handlers: the child's profile is set before they run.
        os.register_at_fork(after_in_child=lambda: sys.setprofile(profile))
        sys.path.insert(0, sys.argv[1])
        from helpers import FakeTransport
        from lsquare import LSquare, _client
        reset = _client._reset_forked.__code__

        class Quiet:
            def info(self, message): pass
            def warn(self, message): pass

        def makes(signum, frame):
            made.append(LSquare(api_key="lsq_test", endpoint="http://localhost:1", _transport=FakeTransport(), logger=Quiet()))

        clients = [LSquare(api_key="lsq_test", endpoint="http://localhost:1", _transport=FakeTransport(), logger=Quiet()) for _ in range(2)]
        before = [client._generation for client in clients]
        signal.signal(signal.SIGUSR1, makes)
        pid = os.fork()
        if pid == 0:
            sys.setprofile(None)
            reset_all = all(client._generation == generation + 1 for client, generation in zip(clients, before))
            os._exit(0 if reset_all and len(made) == len(fired) > 1 else 3)
        _, status = os.waitpid(pid, 0)
        print("child", os.waitstatus_to_exitcode(status))
        """
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["child", "0"], done.stdout + done.stderr
    assert "changed size" not in done.stderr, done.stderr


@pytest.mark.skipif(not hasattr(os, "fork"), reason="no fork here")
def test_a_fork_resets_every_live_client_though_one_s_reset_raises() -> None:
    # A guard: with one handler for all, a client whose reset raises keeps no other from its own,
    # as separate handlers didn't, and the interpreter still reports what it raised.
    done = run(
        """
        import gc, json, os, sys, threading, weakref
        sys.path.insert(0, sys.argv[1])
        from helpers import Held
        from lsquare import LSquare

        class Quiet:
            def info(self, message): pass
            def warn(self, message): pass

        def broken():
            raise RuntimeError("reset failed")

        disabled = LSquare(logger=Quiet())
        failing = LSquare(logger=Quiet())
        failing._after_fork = broken
        gone = LSquare(logger=Quiet())
        reference = weakref.ref(gone)
        del gone
        gc.collect()
        assert reference() is None
        held = Held()
        sending = LSquare(api_key="lsq_test", endpoint="http://localhost:1", _transport=held, logger=Quiet())
        sending.capture({"conversationId": "c", "turnId": 1})
        assert held.entered.wait(10) and sending._live
        before = {"disabled": disabled._generation, "failing": failing._generation, "sending": sending._generation}
        pid = os.fork()
        if pid == 0:
            after = {"disabled": disabled._generation, "failing": failing._generation, "sending": sending._generation}
            reset = sorted(name for name in after if after[name] == before[name] + 1)
            os._exit(0 if reset == ["disabled", "sending"] and not sending._live else 3)
        _, status = os.waitpid(pid, 0)
        print("child", os.waitstatus_to_exitcode(status))
        held.release()
        print("parent", sending.shutdown(timeout_ms=5000).sent)
        """
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["child", "0", "parent", "1"], done.stdout + done.stderr
    assert "RuntimeError: reset failed" in done.stderr


# Codex's second review, 31 October 2026: the one fork handler reset every client alive in the child,
# also one an app's own `after_in_child` callback, run before it, had made there: what that client
# had captured in the child was dropped.


@pytest.mark.skipif(not hasattr(os, "fork"), reason="no fork here")
def test_a_client_an_earlier_after_in_child_callback_made_keeps_what_it_captured_and_the_next_fork_resets_it() -> None:
    done = run(
        """
        import os, sys
        sys.path.insert(0, sys.argv[1])
        made = []

        class Quiet:
            def info(self, message): pass
            def warn(self, message): pass

        def early():
            # The app's, registered before lsquare is imported, so it runs before lsquare's own:
            # in the first child, a fresh client that captures an event.
            if made:
                return
            from helpers import Held
            from lsquare import LSquare
            fresh = LSquare(api_key="lsq_test", endpoint="http://localhost:1", _transport=Held(), logger=Quiet())
            fresh.capture({"conversationId": "c", "turnId": 1})
            made.append((fresh, fresh._generation, len(fresh._live)))

        os.register_at_fork(after_in_child=early)
        from lsquare import LSquare
        inherited = LSquare(logger=Quiet())
        assert inherited._callbacks.drain(5)
        before = inherited._generation

        def report(*values):
            print(*values, flush=True)

        pid = os.fork()
        if pid == 0:
            fresh, generation, live = made[0]
            report("child", inherited._generation - before, fresh._generation - generation, live, len(fresh._live))
            grandchild = os.fork()
            if grandchild == 0:
                # Now an inherited one: reset like the others.
                report("grandchild", inherited._generation - before, fresh._generation - generation, len(fresh._live))
                os._exit(0)
            _, status = os.waitpid(grandchild, 0)
            os._exit(os.waitstatus_to_exitcode(status))
        _, status = os.waitpid(pid, 0)
        report("exit", os.waitstatus_to_exitcode(status))
        """
    )
    assert done.returncode == 0, done.stderr
    # Before: "child 1 1 1 0", the fresh client reset and its event dropped.
    assert done.stdout.splitlines() == ["child 1 0 1 1", "grandchild 2 1 0", "exit 0"], done.stdout + done.stderr


@pytest.mark.skipif(not hasattr(os, "fork"), reason="no fork here")
def test_a_grandchild_given_a_pid_its_grandparent_had_still_resets_the_clients_it_inherited() -> None:
    # A guard for each client being made the child's as it is reset, before the reset runs: a
    # descendant may be given the PID of an ancestor that has exited (the system reuses them), and a
    # client still marked as that ancestor's would then pass for one the descendant made itself.
    # The grandchild's PID is the test's own here: a hook run before lsquare's gives the
    # grandparent's. One client's reset raises in the child: it is still the child's.
    done = run(
        """
        import os, sys
        grandparent = os.getpid()
        resets, posing = [], []

        def pose():
            # The app's, registered before lsquare's: in the grandchild, the grandparent's PID.
            if posing:
                os.getpid = lambda: grandparent

        os.register_at_fork(after_in_child=pose)
        sys.path.insert(0, sys.argv[1])
        from lsquare import LSquare

        class Quiet:
            def info(self, message): pass
            def warn(self, message): pass

        def broken():
            resets.append("failing")
            raise RuntimeError("reset failed")

        plain = LSquare(logger=Quiet())
        failing = LSquare(logger=Quiet())
        failing._after_fork = broken
        for client in (plain, failing):
            assert client._callbacks.drain(5)
        before = plain._generation
        pid = os.fork()
        if pid == 0:
            child = (plain._generation - before, len(resets))
            posing.append(True)
            grandchild = os.fork()
            if grandchild == 0:
                ok = os.getpid() == grandparent and (plain._generation - before, len(resets)) == (2, 2)
                os._exit(0 if ok else 3)
            _, status = os.waitpid(grandchild, 0)
            os._exit(os.waitstatus_to_exitcode(status) if child == (1, 1) else 4)
        _, status = os.waitpid(pid, 0)
        print("child", os.waitstatus_to_exitcode(status))
        """
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["child", "0"], done.stdout + done.stderr
