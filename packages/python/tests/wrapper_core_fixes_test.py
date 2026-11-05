"""The tap on a tool's own function, without an MCP SDK or anyio, so every Python the core runs on
runs it: a closed generator's return value (py-mcp-21), a tool object tapped for two clients at
once (py-mcp-7), a generator a function returns (py-mcp-10), a function called from inside a run
(py-mcp-18), and a capture after a run returned (py-mcp-8). Then Codex's first review of those fixes:
the wrapper's locks in a forked child, and a context a run left behind."""

from __future__ import annotations

import contextvars
import gc
import os
import signal
import subprocess
import sys
import textwrap
import threading
import weakref
from collections.abc import Generator, Iterator
from typing import Any

import pytest

from helpers import FakeTransport
from lsquare import LSquare
from lsquare._client import CallScope
from lsquare.mcp._boundary import tap, tapped


def make_client() -> tuple[LSquare, FakeTransport]:
    transport = FakeTransport()
    return LSquare(api_key="k", endpoint="http://localhost:1", _transport=transport), transport


class In:
    """Runs a test's steps inside a call: its scope set in the client's variable."""

    def __init__(self, lsquare: LSquare, call_id: str = "call") -> None:
        self.var = lsquare._lsquare_internal().calls()
        self.scope = CallScope(call_id=call_id)

    def __enter__(self) -> CallScope:
        self.token = self.var.set(self.scope)
        return self.scope

    def __exit__(self, *_: Any) -> None:
        self.var.reset(self.token)


# -- py-mcp-21 ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["cleanup-result", 0])
def test_a_tapped_generator_closed_returns_what_the_generator_returned(value: Any) -> None:
    lsquare, _ = make_client()
    var = lsquare._lsquare_internal().calls()

    def cleaning() -> Generator[int, None, Any]:
        try:
            yield 1
            yield 2
        except GeneratorExit:
            # What close() returns, on Python 3.13+; a falsy value too.
            return value
        return None

    original = cleaning()
    next(original)
    # Typed as returning None, which it does before 3.13.
    close: Any = original.close
    expected = close()
    assert expected == (value if sys.version_info >= (3, 13) else None)
    for in_call in (False, True):
        scope = CallScope(call_id="c")
        token = var.set(scope) if in_call else None
        try:
            wrapped = tapped(cleaning, lambda: var)()
            next(wrapped)
            closer: Any = wrapped.close
            assert closer() == expected
        finally:
            if token is not None:
                var.reset(token)
        assert scope.thrown is None and scope.invoked is in_call


def test_a_tapped_generator_that_yields_as_it_closes_still_raises_as_the_original() -> None:
    lsquare, _ = make_client()

    def stubborn() -> Iterator[int]:
        try:
            yield 1
        except GeneratorExit:
            yield 2

    with In(lsquare) as scope:
        wrapped = tapped(stubborn, lambda: lsquare._lsquare_internal().calls())()
        next(wrapped)
        with pytest.raises(RuntimeError, match="ignored GeneratorExit"):
            wrapped.close()
    assert scope.thrown is not None and isinstance(scope.thrown[0], RuntimeError)


# -- py-mcp-7 -----------------------------------------------------------------------------------------


def test_a_function_tapped_for_two_clients_records_on_the_call_of_each_and_keeps_neither() -> None:
    a, _ = make_client()
    b, _ = make_client()

    class Tool:
        fn: Any = None

    shared = Tool()
    shared.fn = lambda: "ok"
    tap(shared, a._lsquare_internal().calls)
    tap(shared, b._lsquare_internal().calls)
    for each in (a, b):
        with In(each) as scope:
            assert shared.fn() == "ok"
        assert scope.invoked and scope.tool_result == ("ok",)

    class Owner:
        def calls(self) -> Any:
            return None

    owner = Owner()
    tap(shared, owner.calls)
    gone = weakref.ref(owner)
    del owner
    gc.collect()
    assert gone() is None


def test_two_clients_tapping_one_tool_object_at_once_both_keep_it() -> None:
    # The second tap reads `fn` while the first is still installing its wrapper: it must find that
    # wrapper, and add its client to it, not wrap the original over it. The order is the test's
    # own: b starts only once a is installing, and a installs only once b has read `fn`.
    a, _ = make_client()
    b, _ = make_client()
    a_installing = threading.Event()
    read_by_b = threading.Event()

    class Tool:
        def __init__(self) -> None:
            self._fn: Any = lambda: "ok"
            self.installs: list[str] = []

        @property
        def fn(self) -> Any:
            if threading.current_thread().name == "b":
                read_by_b.set()
            return self._fn

        @fn.setter
        def fn(self, value: Any) -> None:
            if threading.current_thread().name == "a" and not self.installs:
                a_installing.set()
                # Thread a installs only once b has read `fn` too: both started on the original.
                assert read_by_b.wait(10)
            self.installs.append(threading.current_thread().name)
            self._fn = value

    def tap_b() -> None:
        assert a_installing.wait(10)
        tap(shared, b._lsquare_internal().calls)

    shared = Tool()
    threads = [threading.Thread(target=tap, args=(shared, a._lsquare_internal().calls), name="a"), threading.Thread(target=tap_b, name="b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert a_installing.is_set() and read_by_b.is_set()
    assert shared.installs == ["a"]
    for each in (a, b):
        with In(each) as scope:
            assert shared.fn() == "ok"
        assert scope.invoked and scope.tool_result == ("ok",), each


# -- py-mcp-10 ----------------------------------------------------------------------------------------


def test_a_function_returning_a_generator_keeps_its_run_until_the_generator_ends() -> None:
    lsquare, transport = make_client()

    def body() -> Iterator[int]:
        lsquare.capture({"question": "in body", "answer": "a"})
        yield 1
        raise ValueError("generator body failed")

    def factory() -> Iterator[int]:
        return body()

    with In(lsquare) as scope:
        value = tapped(factory, lambda: lsquare._lsquare_internal().calls())()
        # The SDK gets a generator, named as the tool's, and nothing has run yet.
        assert type(value).__name__ == "generator" and value.__name__ == "body"
        assert scope.tool_result is None and scope.thrown is None and scope.agent is None
        with pytest.raises(ValueError):
            list(value)
    assert scope.thrown is not None and str(scope.thrown[0]) == "generator body failed"
    from lsquare._event import Captured, ShapedAgent

    # Read when it was captured: the scope keeps its IDs and what it read, not the app's objects.
    agent = scope.agent
    assert isinstance(agent, Captured) and isinstance(agent.snapshot, ShapedAgent) and agent.snapshot.body["question"] == "in body"
    lsquare.flush(timeout_ms=3_000)
    assert transport.events == []


def test_an_async_generator_a_function_returns_is_carried_only_where_the_sdk_iterates_it() -> None:
    # fastmcp iterates one (`finishes`); mcp 1.x and 2.x convert it as an object.
    import asyncio

    lsquare, _ = make_client()
    calls = lsquare._lsquare_internal().calls

    async def stream() -> Any:
        yield 1
        raise ValueError("async generator body failed")

    async def drain(value: Any) -> None:
        async for _ in value:
            pass

    with In(lsquare) as scope:
        value = tapped(lambda: stream(), lambda: calls(), finishes=True)()
        assert value.__name__ == "stream" and scope.tool_result is None
        with pytest.raises(ValueError):
            asyncio.run(drain(value))
    assert scope.thrown is not None and str(scope.thrown[0]) == "async generator body failed"
    with In(lsquare) as scope:
        value = tapped(lambda: stream(), lambda: calls())()
    assert scope.tool_result == (value,)
    asyncio.run(value.aclose())


# -- py-mcp-18 ----------------------------------------------------------------------------------------


def test_a_function_called_from_inside_another_run_records_nothing_on_its_call() -> None:
    lsquare, _ = make_client()
    calls = lsquare._lsquare_internal().calls

    def inner() -> None:
        raise ValueError("inner secret failure")

    inner_tapped = tapped(inner, lambda: calls())

    def outer() -> str:
        try:
            inner_tapped()
        except ValueError:
            pass
        return "outer's own"

    with In(lsquare) as scope:
        assert tapped(outer, lambda: calls())() == "outer's own"
    assert scope.thrown is None and scope.tool_result == ("outer's own",)


def test_a_run_that_returns_after_one_that_raised_is_the_calls_outcome() -> None:
    lsquare, _ = make_client()
    attempts: list[int] = []

    def flaky() -> str:
        attempts.append(1)
        if len(attempts) == 1:
            raise ConnectionError("first attempt failed")
        return "second attempt"

    wrapped = tapped(flaky, lambda: lsquare._lsquare_internal().calls())
    with In(lsquare) as scope:
        with pytest.raises(ConnectionError):
            wrapped()
        assert wrapped() == "second attempt"
    assert scope.thrown is None and scope.tool_result == ("second attempt",)


# -- py-mcp-8 -----------------------------------------------------------------------------------------


def test_a_capture_after_a_run_returned_is_a_turn_outside_any_run_and_inside_a_later_one() -> None:
    lsquare, transport = make_client()
    calls = lsquare._lsquare_internal().calls
    with In(lsquare, "r1") as scope:
        assert tapped(lambda: "done", lambda: calls())() == "done"
        # Middleware after the tool returned, in the call's scope: a turn of its own.
        lsquare.capture({"question": "middleware, after", "answer": "a"})
        assert scope.agent is None

        def later_round() -> str:
            lsquare.capture({"question": "round two", "answer": "b"})
            return "again"

        assert tapped(later_round, lambda: calls())() == "again"
    lsquare.flush(timeout_ms=3_000)
    from lsquare._event import Captured, ShapedAgent

    agent = scope.agent
    assert isinstance(agent, Captured) and isinstance(agent.snapshot, ShapedAgent) and agent.snapshot.body["question"] == "round two"
    assert [(event["question"], event["callId"]) for event in transport.events] == [("middleware, after", "r1")]


# -- Codex's first review, 31 October 2026 --------------------------------------------------------


@pytest.mark.skipif(not hasattr(os, "fork"), reason="no fork here")
@pytest.mark.parametrize("lock", ["tapping", "watching"])
def test_a_child_forked_while_another_thread_holds_a_wrapper_lock_takes_it_as_its_own(lock: str) -> None:
    # Review 2: a child has only the thread that forked, so a lock another thread held then stayed
    # held, and the child's next tap (or transport watch) waited forever. Here the tap's lock is
    # held by a real tap(), waiting in the tool's own `fn` setter.
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                f"""
                import contextlib, os, signal, sys, threading, types
                from lsquare.mcp import _boundary, _v1

                holding, release = threading.Event(), threading.Event()

                class Waiting:
                    def __init__(self):
                        self._fn = lambda: "ok"

                    @property
                    def fn(self):
                        return self._fn

                    @fn.setter
                    def fn(self, value):
                        holding.set()
                        release.wait()
                        self._fn = value

                def hold_watching():
                    with _v1._WATCHING:
                        holding.set()
                        release.wait()

                if {lock!r} == "tapping":
                    thread = threading.Thread(target=_boundary.tap, args=(Waiting(), lambda: None))
                else:
                    thread = threading.Thread(target=hold_watching)
                thread.start()
                assert holding.wait(10)
                pid = os.fork()
                if pid == 0:
                    # A child that waits forever ends here: SIGALRM's own action.
                    signal.alarm(5)
                    if {lock!r} == "tapping":
                        plain = types.SimpleNamespace(fn=lambda: "ok")
                        _boundary.tap(plain, lambda: None)
                        ok = getattr(plain.fn, "__lsquare_tapped__", False) is True
                    else:
                        module = types.ModuleType("mcp.server.streamable_http")

                        class StreamableHTTPServerTransport:
                            @contextlib.asynccontextmanager
                            async def connect(self):
                                yield None, None

                        module.StreamableHTTPServerTransport = StreamableHTTPServerTransport
                        sys.modules[module.__name__] = module
                        _v1.watch_transports()
                        ok = getattr(StreamableHTTPServerTransport.__dict__["connect"], "__lsquare_watches__", None) is _v1._OPENED
                    os._exit(0 if ok else 3)
                _, status = os.waitpid(pid, 0)
                release.set()
                thread.join(10)
                print("child", os.waitstatus_to_exitcode(status))
                """
            ),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    # Before: "child -14", the child's watchdog alarm.
    assert done.stdout.split() == ["child", "0"], done.stdout + done.stderr


def test_a_context_copied_inside_a_run_runs_no_tool_of_the_call_after_that_run_returned() -> None:
    # Review 6: a task or thread a tool starts inherits its run. Once the run returned, the call
    # goes on (its middleware, say, still running), and a tool function run from that copy then
    # claimed a run of its own on the call, its raise recorded as the call's.
    lsquare, _ = make_client()
    calls = lsquare._lsquare_internal().calls
    left: list[contextvars.Context] = []

    def audit() -> str:
        raise ValueError("audit failed")

    audit_tapped = tapped(audit, lambda: calls())

    def outer() -> str:
        left.append(contextvars.copy_context())
        return "outer's own"

    with In(lsquare) as scope:
        assert tapped(outer, lambda: calls())() == "outer's own"
        with pytest.raises(ValueError):
            left[0].run(audit_tapped)
        # A retry, from the call's own context, is a run of the call's again.
        with pytest.raises(ValueError):
            audit_tapped()
        assert scope.thrown is not None and str(scope.thrown[0]) == "audit failed"
        assert tapped(outer, lambda: calls())() == "outer's own"
        with pytest.raises(ValueError):
            left[1].run(audit_tapped)
    assert scope.thrown is None and scope.tool_result == ("outer's own",)


# -- Codex's second review, 31 October 2026 -------------------------------------------------------
# The wrapper's other weak containers ran a callback of the standard library's as what they held was
# collected (WeakSet's, WeakMethod's, WeakKeyDictionary's): a signal handler's exit raised there was
# only reported, and the process went on.


def run(code: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    # The tests' folder is its argument, for the helpers.
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code), os.path.dirname(__file__)], capture_output=True, text=True, timeout=timeout)


#: In a subprocess: `held()` makes what the wrapper keeps for an object (`thing`), the way it does.
HOLDERS = """
    import asyncio, contextlib, gc, signal, sys, weakref
    sys.path.insert(0, sys.argv[1])
    from lsquare import LSquare
    from lsquare.mcp import _boundary, _call, _v1

    class Quiet:
        def info(self, message): pass
        def warn(self, message): pass

    class Stream:
        pass

    class Session:
        pass

    def state_of(client):
        internal = client._lsquare_internal()
        settings = _call.read_options(internal, None, True, True, None, None, False)
        return _call.State(internal=internal, settings=settings, line="v1", low=object(), high=None, registry=None)

    client = LSquare(logger=Quiet())
    assert client._callbacks.drain(5)
    state = state_of(client)
    tools = []

    def held(which):
        if which == "opened":
            # mcp 1.x's Streamable HTTP transport, as the wrapper wraps it: it notes the read stream.
            stream = Stream()

            @contextlib.asynccontextmanager
            async def connect():
                yield stream, None

            async def opens():
                async with _v1._watched_transport(connect)() as streams:
                    pass

            asyncio.run(opens())
            assert stream in _v1._OPENED
            return stream
        if which == "clients":
            # A tool tapped for a client, by its own bound method: the tool outlives it.
            owner = LSquare(logger=Quiet())
            assert owner._callbacks.drain(5)
            tools.append(_boundary.tapped(lambda: "ok", owner._lsquare_internal().calls))
            return owner
        session = Session()
        if which == "made":
            assert _call.made_session(state, session) is not None
        else:
            assert _call.http_session(state, session, lambda: "s-1") == "s-1"
        return session
"""

KINDS = ["opened", "clients", "made", "http"]


@pytest.mark.skipif(not hasattr(signal, "SIGUSR1"), reason="no SIGUSR1 here")
@pytest.mark.parametrize("which", KINDS)
def test_a_signal_handler_s_exit_while_what_the_wrapper_holds_weakly_is_collected_ends_the_process(which: str) -> None:
    # A real signal in the first Python code to run as the object is collected: there is none. Each
    # is held as the wrapper holds it: a read stream mcp 1.x's watched transport opened, a tapped
    # function's client, a session made for a connection, an HTTP session's kept ID.
    done = run(
        HOLDERS
        + f"""
    unraisable = []
    sys.unraisablehook = lambda raised: unraisable.append(type(raised.exc_value).__name__)

    def exits(signum, frame):
        sys.exit(23)

    signal.signal(signal.SIGUSR1, exits)
    thing = held({which!r})
    gone = weakref.ref(thing)
    gc.collect()
    ran = []

    def profile(frame, event, arg):
        if event == "call" and not ran:
            ran.append(frame.f_code.co_name)
            signal.raise_signal(signal.SIGUSR1)

    sys.setprofile(profile)
    del thing
    gc.collect()
    sys.setprofile(None)
    print("collected", gone() is None, "ran", ran, "unraisable", unraisable, flush=True)
    signal.raise_signal(signal.SIGUSR1)
    print("still running", flush=True)
    """
    )
    # Before: "ran ['_remove']" (opened), "['_cb']" (clients), "['remove']" (made, http), each with
    # "unraisable ['SystemExit']", and the process went on until the second signal.
    assert done.stdout.splitlines() == ["collected True ran [] unraisable []"], done.stdout + done.stderr
    assert done.returncode == 23, done.stderr


@pytest.mark.skipif(not hasattr(signal, "SIGUSR1"), reason="no SIGUSR1 here")
@pytest.mark.parametrize("which", KINDS)
def test_a_signal_handler_adding_to_what_the_wrapper_holds_weakly_while_it_changes_loses_nothing(which: str) -> None:
    # A real signal at every bytecode of the table's own addition (one that prunes the references
    # of what was collected, where the table prunes), up to 200, each handler adding another object
    # the same way: every object added keeps what was kept for it. Before, on `clients`: a handler's
    # tap was lost, the addition it interrupted storing its own copy of the clients over it.
    done = run(
        HOLDERS
        + f"""
    which = {which!r}
    fired, made, handling = [], [], [False]
    # The table's own step: a set's or a function's clients' `add`, a session table's `setdefault`.
    step = "setdefault" if which in ("made", "http") else "add"

    class Owner:
        def calls(self):
            return None

    tool = type("Tool", (), {{}})()
    tool.fn = lambda: "ok"
    owners = []

    def keep(thing, value):
        # As the wrapper adds each: a stream to the watched set, a client to a tapped function, an
        # ID to a state's table.
        if which == "opened":
            _v1._OPENED.add(thing)
        elif which == "clients":
            _boundary.tap(tool, thing.calls)
        else:
            table = state.made_sessions if which == "made" else state.http_sessions
            assert table.setdefault(thing, value) == value

    def kept(thing, value):
        if which == "opened":
            return thing in _v1._OPENED
        if which == "clients":
            return _boundary.watches(tool, thing.calls)
        table = state.made_sessions if which == "made" else state.http_sessions
        return table.get(thing) == value

    def new():
        return Owner() if which == "clients" else Session()

    def adds(signum, frame):
        handling[0] = True
        try:
            thing = new()
            owners.append(thing)
            keep(thing, f"handler-{{len(made)}}")
            made.append(thing)
        finally:
            handling[0] = False

    def inside(frame):
        while frame is not None:
            if frame.f_code.co_name == step and frame.f_code.co_filename != "<string>":
                return True
            frame = frame.f_back
        return False

    def tracer(frame, event, arg):
        if handling[0] or not inside(frame):
            return None
        frame.f_trace_opcodes = True
        return each

    def each(frame, event, arg):
        # (Set again here: Python 3.14 starts the opcode events only then.)
        frame.f_trace_opcodes = True
        if not handling[0] and len(fired) < 200:
            fired.append(event)
            signal.raise_signal(signal.SIGUSR1)
        return each

    # Collected objects' references first: as many as the table holds before it prunes. Each
    # collected at once, and objects kept in their place, so none of them has its id reused.
    first = new()
    owners.append(first)
    keep(first, "first")
    gone = [new() for _ in range(15)]
    for thing in gone:
        keep(thing, "gone")
    del gone, thing
    gc.collect()
    spacers = [new() for _ in range(100)]
    signal.signal(signal.SIGUSR1, adds)
    outer = new()
    owners.append(outer)
    sys.settrace(tracer)
    keep(outer, "outer")
    sys.settrace(None)
    lost = [index for index, thing in enumerate(made) if not kept(thing, f"handler-{{index}}")]
    print("fired", len(fired) > 1, len(made) == len(fired), "first", kept(first, "first"), "outer", kept(outer, "outer"), "lost", lost, flush=True)
    """
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["fired", "True", "True", "first", "True", "outer", "True", "lost", "[]"], done.stdout + done.stderr


@pytest.mark.parametrize("which", KINDS)
def test_what_the_wrapper_holds_weakly_keeps_nothing_alive_and_prunes_what_was_collected(which: str) -> None:
    # A guard for the references with no callback: what was collected stays only until later
    # additions prune it, a few dozen entries at most, never one for each object held.
    from lsquare.mcp import _boundary, _call, _v1

    lsquare, _ = make_client()
    internal = lsquare._lsquare_internal()
    state = _call.State(internal=internal, settings=_call.read_options(internal, None, True, True, None, None, False), line="v1", low=object(), high=None, registry=None)

    class Thing:
        def calls(self) -> Any:
            return None

    class Tool:
        fn: Any = staticmethod(lambda: "ok")

    tool = Tool()
    tap(tool, internal.calls)
    gone: list[weakref.ref[Thing]] = []
    # Each object made takes the place of the last one collected, so no id is reused here.
    spacers: list[Thing] = []
    for index in range(200):
        thing = Thing()
        gone.append(weakref.ref(thing))
        if which == "opened":
            _v1._OPENED.add(thing)
            assert thing in _v1._OPENED
        elif which == "clients":
            tap(tool, thing.calls)
            assert _boundary.watches(tool, thing.calls)
        elif which == "made":
            assert _call.made_session(state, thing) is not None
        else:
            assert _call.http_session(state, thing, lambda: f"s-{index}") == f"s-{index}"
        # Collected now (it is in no cycle): what is made next takes its place.
        del thing
        spacers.append(Thing())
    gc.collect()
    assert [reference for reference in gone if reference() is not None] == []
    if which == "opened":
        table: Any = _v1._OPENED
    elif which == "clients":
        table = getattr(tool.fn, "__lsquare_clients__")._held
        # The client the tool was tapped for first is still held.
        assert _boundary.watches(tool, internal.calls)
    else:
        table = state.made_sessions if which == "made" else state.http_sessions
    assert len(table) < 40


@pytest.mark.parametrize("which", ["opened", "made", "http"])
def test_an_object_that_takes_a_collected_one_s_id_is_not_taken_for_it(which: str) -> None:
    # A guard: the tables are keyed by id(), which a collected object's successor may reuse, and a
    # reference counts only for the object it still refers to.
    from lsquare.mcp import _call, _v1

    lsquare, _ = make_client()
    internal = lsquare._lsquare_internal()
    state = _call.State(internal=internal, settings=_call.read_options(internal, None, True, True, None, None, False), line="v1", low=object(), high=None, registry=None)

    class Thing:
        pass

    numbers = iter(range(10**9))

    def keep(thing: Thing) -> Any:
        if which == "opened":
            _v1._OPENED.add(thing)
            return True
        if which == "made":
            return _call.made_session(state, thing)
        return _call.http_session(state, thing, lambda: f"s-{next(numbers)}")

    def known(thing: Thing) -> Any:
        if which == "opened":
            return thing in _v1._OPENED
        table = state.made_sessions if which == "made" else state.http_sessions
        return table.get(thing)

    kept: list[Thing] = []
    # Many, so some of their successors take their ids, whatever else the allocator serves.
    firsts = [Thing() for _ in range(1000)]
    before = {id(thing): keep(thing) for thing in firsts}
    del firsts
    gc.collect()
    successors = []
    for _ in range(2000):
        candidate = Thing()
        kept.append(candidate)
        if id(candidate) in before:
            successors.append(candidate)
    if not successors:
        pytest.skip("no object took a collected one's id")
    for successor in successors:
        assert not known(successor)
        after = keep(successor)
        assert known(successor) == after
        if which != "opened":
            assert after != before[id(successor)]


@pytest.mark.parametrize("which", KINDS)
def test_threads_adding_to_what_the_wrapper_holds_weakly_at_once_each_keep_theirs(which: str) -> None:
    # A guard: threads switching as often as the interpreter lets them, each adding objects (and
    # dropping half, so the tables prune and ids are reused), then all at once one object with a
    # value of each's: every object kept is found with its value, and the shared one with one value.
    from lsquare.mcp import _boundary, _call, _v1

    lsquare, _ = make_client()
    internal = lsquare._lsquare_internal()
    state = _call.State(internal=internal, settings=_call.read_options(internal, None, True, True, None, None, False), line="v1", low=object(), high=None, registry=None)

    class Thing:
        def calls(self) -> Any:
            return None

    class Tool:
        fn: Any = staticmethod(lambda: "ok")

    tool = Tool()
    tap(tool, internal.calls)
    table: Any = state.made_sessions if which == "made" else state.http_sessions

    def add(thing: Thing, value: str) -> str:
        if which == "opened":
            _v1._OPENED.add(thing)
            return value
        if which == "clients":
            tap(tool, thing.calls)
            return value
        result: str = table.setdefault(thing, value)
        return result

    def kept(thing: Thing, value: str) -> bool:
        if which == "opened":
            return thing in _v1._OPENED
        if which == "clients":
            return _boundary.watches(tool, thing.calls)
        return bool(table.get(thing) == value)

    shared = Thing()
    barrier = threading.Barrier(8)
    results: dict[int, Any] = {}
    failures: list[str] = []

    def work(index: int) -> None:
        try:
            mine: list[tuple[Thing, str]] = []
            for step in range(150):
                thing = Thing()
                value = f"{index}-{step}"
                if add(thing, value) != value:
                    failures.append(f"{value} was given another")
                if step % 2:
                    mine.append((thing, value))
            barrier.wait(10)
            results[index] = add(shared, f"shared-{index}")
            failures.extend(value for thing, value in mine if not kept(thing, value))
        except BaseException as error:  # noqa: BLE001 - the test's own report
            failures.append(repr(error))
            barrier.abort()

    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        threads = [threading.Thread(target=work, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
    finally:
        sys.setswitchinterval(interval)
    assert failures == []
    assert len(results) == 8
    if which in ("made", "http"):
        assert len(set(results.values())) == 1 and table.get(shared) in results.values()


def test_pruning_drops_a_dead_reference_in_one_step_as_the_standard_librarys_weakref_does() -> None:
    # A guard: no check-then-pop, in which a reader would see a live entry stored meanwhile gone.
    import _weakref

    from lsquare.mcp import _weak

    assert _weak._remove_dead is getattr(_weakref, "_remove_dead_weakref")
