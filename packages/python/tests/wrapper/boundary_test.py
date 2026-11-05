"""The tap on a tool's own function, and the call flow itself, without an MCP SDK: each kind of
function kept as it is, each run's boundary, generators passing send/throw/close through, a
cancelled call recorded as `timeout` with the cancellation passed on unchanged, and what a call's
end waits for: `wait_until` handed every call's sending first, a cancellation or an exit never
waited for."""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import sys
import threading
import time
import weakref
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import pytest

from helpers import Held, acollected
from lsquare import LSquare
from lsquare._client import CallScope
from lsquare._sending import Reply
from lsquare.mcp._boundary import TAPPED, RunScope, is_async, tapped
from lsquare.mcp._call import NO_EXTRAS, Running, Settings, State, on_call, read_options

pytest.importorskip("anyio")
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def make_client() -> tuple[LSquare, list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []

    def transport(body: bytes, timeout: float, abort: Any) -> Reply:
        batch = json.loads(body)["events"]
        events.extend(batch)
        return Reply(200, None, json.dumps({"accepted": [event["eventId"] for event in batch], "duplicates": [], "rejected": []}))

    return LSquare(api_key="k", endpoint="http://localhost:1", _transport=transport), events


def plain(a: int, b: str = "x") -> str:
    return f"{a}{b}"


async def coroutine(a: int) -> int:
    await asyncio.sleep(0)
    return a + 1


def generator(a: int) -> Iterator[int]:
    yield a


async def async_generator(a: int) -> AsyncIterator[int]:
    yield a


class Callable_:
    async def __call__(self, a: int) -> int:
        return a


async def test_keeps_each_kind_of_function_and_its_signature() -> None:
    lsquare, _ = make_client()
    calls = lsquare._lsquare_internal().calls
    for fn in (plain, coroutine, generator, async_generator, functools.partial(coroutine, 1), Callable_()):
        wrapped = tapped(fn, calls)
        assert getattr(wrapped, TAPPED) is True
        assert inspect.signature(wrapped) == inspect.signature(fn)
        assert is_async(wrapped) == is_async(fn)
        assert inspect.isgeneratorfunction(wrapped) == inspect.isgeneratorfunction(fn)
        assert inspect.isasyncgenfunction(wrapped) == inspect.isasyncgenfunction(fn)
    assert tapped(plain, calls)(2, b="y") == "2y"
    assert await tapped(coroutine, calls)(2) == 3


async def test_records_each_runs_entry_return_and_raise_and_closes_its_boundary_when_it_returns() -> None:
    lsquare, events = make_client()
    var = lsquare._lsquare_internal().calls()
    scope = CallScope(call_id="call-1")
    entered: list[bool] = []
    scope.on_tool_entry = lambda: entered.append(True)

    def capturing() -> str:
        lsquare.capture({"question": "inside", "answer": "a"})
        return "done"

    token = var.set(scope)
    try:
        assert tapped(capturing, lambda: var)() == "done"
        # The run returned: a capture now is a turn of its own, though the call hasn't ended.
        lsquare.capture({"question": "after", "answer": "b"})
        with pytest.raises(ValueError):
            tapped(_raises, lambda: var)()
    finally:
        var.reset(token)
    await lsquare.aflush(timeout_ms=3_000)
    assert entered == [True, True] and scope.invoked
    from lsquare._event import Captured, ShapedAgent

    # Read when it was captured: the scope keeps its IDs and what it read, not the app's objects.
    agent = scope.agent
    assert isinstance(agent, Captured) and isinstance(agent.snapshot, ShapedAgent) and agent.snapshot.body["question"] == "inside"
    # The last run's own outcome is the call's: the raise replaced what the first run returned.
    assert scope.tool_result is None
    assert scope.thrown is not None and isinstance(scope.thrown[0], ValueError)
    assert not scope.returned
    assert [event["question"] for event in events] == ["after"]


def _raises() -> None:
    raise ValueError("no")


async def test_a_run_scope_reads_and_writes_through_except_its_boundary() -> None:
    call = CallScope(call_id="c")
    run = RunScope(call)
    run.agent = {"question": "q"}
    run.flush_requested = True
    run.returned = True
    assert call.agent == {"question": "q"} and call.flush_requested
    assert run.returned and not call.returned and run.call_id == "c"
    call.returned = True
    assert RunScope(call).returned


async def test_a_generator_tool_captures_inside_its_call_and_passes_send_throw_and_close_on() -> None:
    lsquare, _ = make_client()
    var = lsquare._lsquare_internal().calls()
    scope = CallScope(call_id="call-g")
    closed: list[bool] = []

    def chatty() -> Iterator[Any]:
        try:
            got = yield "first"
            lsquare.capture({"question": "in body", "answer": str(got)})
            try:
                yield "second"
            except KeyError:
                yield "caught"
            yield "last"
        finally:
            closed.append(True)

    token = var.set(scope)
    try:
        wrapped = tapped(chatty, lambda: var)()
        assert next(wrapped) == "first"
        assert wrapped.send("hello") == "second"
        assert wrapped.throw(KeyError("k")) == "caught"
        wrapped.close()
    finally:
        var.reset(token)
    assert closed == [True]
    from lsquare._event import Captured, ShapedAgent

    agent = scope.agent
    assert isinstance(agent, Captured) and isinstance(agent.snapshot, ShapedAgent) and agent.snapshot.body["answer"] == "hello"


async def test_an_async_generator_tool_passes_asend_athrow_and_aclose_on() -> None:
    lsquare, _ = make_client()
    var = lsquare._lsquare_internal().calls()
    closed: list[bool] = []

    async def stream() -> AsyncIterator[Any]:
        try:
            got = yield 1
            try:
                yield got
            except KeyError:
                yield "caught"
        finally:
            closed.append(True)

    token = var.set(CallScope(call_id="call-a"))
    try:
        wrapped = tapped(stream, lambda: var)()
        assert await wrapped.__anext__() == 1
        assert await wrapped.asend("two") == "two"
        assert await wrapped.athrow(KeyError("k")) == "caught"
        await wrapped.aclose()
    finally:
        var.reset(token)
    assert closed == [True]


class _Slow:
    """A dispatch whose handler never finishes on its own."""

    request = None
    tool_name = "slow"
    arguments: dict[str, Any] = {}
    context = None

    def __init__(self, started: Any) -> None:
        self.started = started

    def with_arguments(self, arguments: dict[str, Any]) -> Any:
        return None

    async def call(self, request: Any) -> Any:
        import anyio

        self.started.set()
        await anyio.sleep(30)

    async def answer(self, request: Any, reply: dict[str, Any]) -> Any:
        return reply

    def client(self) -> Any:
        return None

    def version(self) -> Any:
        return "2025-11-25"

    def session(self, version: Any) -> str | None:
        return None

    def continued(self) -> bool:
        return False

    def read(self, result: Any, key: str) -> Any:
        return None

    def with_block(self, result: Any, text: str) -> Any:
        return result

    def recorded(self, result: Any) -> Any:
        return result

    def provenance(self, running: Running, result: Any) -> bool:
        return False

    def jsonrpc_code(self, error: BaseException) -> int | None:
        return None

    def jsonrpc_message(self, error: BaseException) -> str | None:
        return None

    def refresh(self) -> None:
        pass

    def reader(self) -> Any:
        return None


def _state(lsquare: LSquare, wait_until: Callable[[Any], Any] | None = None, flush_before_reply: bool = False) -> State:
    internal = lsquare._lsquare_internal()
    settings: Settings = read_options(internal, None, True, True, None, wait_until, flush_before_reply)
    return State(internal=internal, settings=settings, line="v1", low=object(), high=None, registry=None, high_extras=NO_EXTRAS)


@pytest.mark.parametrize("anyio_backend", ["asyncio", "trio"])
async def test_a_cancelled_call_is_recorded_as_timeout_and_the_cancellation_passed_on(anyio_backend: str) -> None:
    import anyio

    if anyio_backend == "trio":
        pytest.importorskip("trio")
    lsquare, events = make_client()
    started = anyio.Event()
    finished: list[str] = []

    async def run() -> None:
        try:
            await on_call(_state(lsquare), _Slow(started))
        except anyio.get_cancelled_exc_class():
            finished.append("cancelled")
            raise

    async with anyio.create_task_group() as group:
        group.start_soon(run)
        await started.wait()
        group.cancel_scope.cancel()
    await lsquare.aflush(timeout_ms=3_000)
    assert finished == ["cancelled"]
    [event] = events
    assert event["status"] == "error" and event["error"]["category"] == "timeout"


class _Ended(_Slow):
    """A dispatch whose handler gives `result`, after the tool's own function returned `value`."""

    def __init__(self, calls: Any, value: Any, result: Any) -> None:
        self.calls = calls
        self.value = value
        self.result = result

    async def call(self, request: Any) -> Any:
        scope = self.calls.get()
        assert isinstance(scope, CallScope)
        scope.invoked = True
        scope.tool_result = (self.value,)
        return self.result

    def read(self, result: Any, key: str) -> Any:
        return result.get(key) if isinstance(result, dict) else None


async def test_an_error_result_the_sdk_made_from_the_tools_own_is_protocol_not_tool_error() -> None:
    lsquare, events = make_client()
    calls = lsquare._lsquare_internal().calls()
    state = _state(lsquare)
    state.high = object()
    own = {"content": [{"type": "text", "text": "out of stock"}], "isError": True}
    made = {"content": [{"type": "text", "text": "Error executing tool: output validation failed"}], "isError": True}
    await on_call(state, _Ended(calls, own, dict(own)))
    await on_call(state, _Ended(calls, own, made))
    await lsquare.aflush(timeout_ms=3_000)
    passed_on, replaced = events
    assert passed_on["error"]["category"] == "tool_error"
    assert replaced["error"]["category"] == "protocol"


def _held(**options: Any) -> tuple[LSquare, Held]:
    held = Held()
    return LSquare(api_key="k", endpoint="http://localhost:1", _transport=held, **options), held


@pytest.mark.parametrize("anyio_backend", ["asyncio", "trio"])
async def test_a_cancelled_call_hands_its_sending_to_wait_until_and_passes_the_cancellation_on(anyio_backend: str) -> None:
    import anyio

    if anyio_backend == "trio":
        pytest.importorskip("trio")
    lsquare, held = _held()
    handed: list[Any] = []
    started = anyio.Event()
    finished: list[str] = []

    async def run() -> None:
        try:
            await on_call(_state(lsquare, wait_until=handed.append), _Slow(started))
        except anyio.get_cancelled_exc_class():
            finished.append("cancelled")
            raise

    async with anyio.create_task_group() as group:
        group.start_soon(run)
        await started.wait()
        group.cancel_scope.cancel()
    assert finished == ["cancelled"] and len(handed) == 1
    held.release()
    await handed[0]
    [event] = held.acknowledged("tool_call")
    assert event["error"]["category"] == "timeout"
    await lsquare.ashutdown()


async def test_a_cancelled_call_never_waits_for_its_event_even_when_its_reply_would() -> None:
    lsquare, held = _held(flush_timeout_ms=30_000)
    started = asyncio.Event()
    task = asyncio.ensure_future(on_call(_state(lsquare, flush_before_reply=True), _Slow(started)))
    await started.wait()
    # A plain asyncio cancel is delivered once: a wait after it would hold the task until the
    # ingestion answers.
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=5)
    assert task in done and task.cancelled()
    held.release()
    await lsquare.ashutdown()


class _Exits(_Slow):
    """A dispatch whose handler calls flush(), so the reply would wait, then raises `exit`."""

    def __init__(self, lsquare: LSquare, exit: BaseException) -> None:
        self.lsquare = lsquare
        self.exit = exit

    async def call(self, request: Any) -> Any:
        await self.lsquare.aflush(timeout_ms=0)
        raise self.exit


@pytest.mark.parametrize("exit", [SystemExit(3), KeyboardInterrupt()], ids=["SystemExit", "KeyboardInterrupt"])
async def test_an_exit_is_passed_on_at_once_and_its_sending_still_handed_to_wait_until(exit: BaseException) -> None:
    lsquare, held = _held(flush_timeout_ms=10_000)
    handed: list[Any] = []
    started = time.monotonic()
    with pytest.raises(type(exit)):
        await on_call(_state(lsquare, wait_until=handed.append), _Exits(lsquare, exit))
    # Not held up for the event, the ingestion holding its answer.
    assert time.monotonic() - started < 5
    assert len(handed) == 1
    held.release()
    await asyncio.wait_for(handed[0], 5)
    assert len(held.acknowledged("tool_call")) == 1
    await lsquare.ashutdown()


class _Flushing(_Slow):
    """A dispatch whose handler calls flush(), so the reply waits, then returns or raises."""

    def __init__(self, lsquare: LSquare, raises: bool) -> None:
        self.lsquare = lsquare
        self.raises = raises

    async def call(self, request: Any) -> Any:
        await self.lsquare.aflush(timeout_ms=0)
        if self.raises:
            raise ValueError("lookup failed")
        return None


@pytest.mark.parametrize("raises", [False, True], ids=["returns", "raises"])
async def test_a_reply_wait_cancelled_midway_leaves_the_sending_with_wait_until(raises: bool) -> None:
    lsquare, held = _held(flush_timeout_ms=30_000)
    handed: list[Any] = []
    task = asyncio.ensure_future(on_call(_state(lsquare, wait_until=handed.append), _Flushing(lsquare, raises)))
    # The tool has ended and the reply waits on the ingestion, which holds its answer.
    assert await asyncio.to_thread(held.entered.wait, 5)
    assert not task.done()
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=5)
    assert task in done and task.cancelled()
    assert len(handed) == 1
    held.release()
    await asyncio.wait_for(handed[0], 5)
    assert len(held.acknowledged("tool_call")) == 1
    await lsquare.ashutdown()


class Big(dict[str, str]):
    """What a run returns, or raises with, which a weak reference can follow."""


class _Late(Exception):
    def __init__(self, big: Big) -> None:
        super().__init__("raised late")
        self.big = big


class _Detaching(_Slow):
    """A dispatch whose tool's own function, tapped, captures the call's agent part and a turn of
    its own, and leaves a task running that calls it again once `go` is set: that run returns, or
    raises, holding 1 MB."""

    def __init__(self, lsquare: LSquare, raises: bool) -> None:
        self.lsquare = lsquare
        self.raises = raises
        self.go = asyncio.Event()
        self.late: asyncio.Future[None] | None = None
        self.big: list[weakref.ref[Big]] = []
        #: Each run's scope, as capture() sees it inside.
        self.scopes: list[Any] = []
        self.fn = tapped(self.own, lsquare._lsquare_internal().calls)

    async def own(self, late: bool) -> Any:
        self.scopes.append(self.lsquare._lsquare_internal().calls().get())
        if late:
            big = Big(text="x" * 1_000_000)
            self.big.append(weakref.ref(big))
            if self.raises:
                raise _Late(big)
            return big
        self.lsquare.capture({"question": "agent part"})
        self.lsquare.capture({"conversationId": "c1", "turnId": 1, "question": "a turn of its own"})

        async def later() -> None:
            await self.go.wait()
            try:
                await self.fn(True)
            except _Late:
                pass

        self.late = asyncio.ensure_future(later())
        return "ok"

    async def call(self, request: Any) -> Any:
        return await self.fn(False)


@pytest.mark.parametrize("raises", [False, True], ids=["returns", "raises"])
async def test_a_run_ending_after_the_call_let_go_closes_its_boundary_and_records_nothing(raises: bool) -> None:
    # A task the tool left running calls its own function again once the call's event is built,
    # while a turn captured inside the call, its context holding the call's scope, is still being
    # sent: what that run returns or raises isn't put back on the scope, outside the queue's bound.
    lsquare, held = _held()
    dispatch = _Detaching(lsquare, raises)
    try:
        assert await on_call(_state(lsquare), dispatch) == "ok"
        dispatch.go.set()
        assert dispatch.late is not None
        await asyncio.wait_for(dispatch.late, 5)
        first, late = dispatch.scopes
        scope = object.__getattribute__(first, "_call")
        assert await acollected(dispatch.big[0])
        assert (scope.tool_result, scope.thrown) == (None, None) and scope.released
        # The late run's boundary closed all the same.
        assert object.__getattribute__(late, "_ended")
        assert lsquare.flush(timeout_ms=0).pending >= 1
        held.release()
        await lsquare.aflush(timeout_ms=5_000)
        [captured] = held.acknowledged("turn")
        [event] = held.acknowledged("tool_call")
        assert captured["callId"] == event["callId"] == scope.call_id
        assert event["agent"]["question"] == "agent part"
    finally:
        held.release()
        await lsquare.ashutdown()


class _Answers(_Slow):
    """A dispatch whose handler gives 1 MB, which the layers inside the SDK (fastmcp's, MCP SDK
    2.x's middleware) note on the call's record as they pass it on."""

    def __init__(self, lsquare: LSquare) -> None:
        self.lsquare = lsquare
        self.big: list[weakref.ref[Big]] = []
        self.calls: list[Running] = []

    async def call(self, request: Any) -> Any:
        scope = self.lsquare._lsquare_internal().calls().get()
        assert scope is not None and isinstance(scope.wrapper, Running)
        running = scope.wrapper
        self.calls.append(running)
        big = Big(text="x" * 1_000_000)
        self.big.append(weakref.ref(big))
        running.inner_result = running.core_result = running.chain_result = big
        return big


@pytest.mark.parametrize("kind", ["sync", "async"])
async def test_an_identify_still_running_once_the_calls_event_is_built_keeps_nothing_of_its_result(kind: str) -> None:
    # Past its deadline the event goes without it. identify runs on, ignoring its abort, its
    # future keeping its callbacks: those hold the call's record, holding nothing of its result
    # from then on, nor the build, with the result it was given.
    lsquare, held = _held(flush_timeout_ms=100)
    go = threading.Event()
    # Set as identify returns: what's checked while it runs on must be checked before then.
    finished = threading.Event()

    def blocking(given: Any) -> Any:
        go.wait(10)
        finished.set()
        return {"userId": "u-1"}

    async def running_on(given: Any) -> Any:
        while not go.is_set():
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                pass
        finished.set()
        return {"userId": "u-1"}

    internal = lsquare._lsquare_internal()
    settings = read_options(internal, blocking if kind == "sync" else running_on, True, False, None, None, True)
    state = State(internal=internal, settings=settings, line="v1", low=object(), high=None, registry=None, high_extras=NO_EXTRAS)
    dispatch = _Answers(lsquare)
    try:
        await on_call(state, dispatch)
        assert await asyncio.to_thread(held.entered.wait, 5)
        [running] = dispatch.calls
        assert running.identify.run is not None and not running.identify.run.future.done()
        assert await acollected(dispatch.big[0])
        assert not finished.is_set()
        held.release()
        await lsquare.aflush(timeout_ms=5_000)
        [event] = held.acknowledged("tool_call")
        assert event["identity"] == "failed" and "userId" not in event
    finally:
        go.set()
        held.release()
        await lsquare.ashutdown()


class _Given(_Slow):
    """A dispatch whose call comes with 1 MB of arguments and a 1 MB request context, and whose
    handler notes the call's record: the test follows each by a weak reference only."""

    context: Any = None

    def __init__(self, lsquare: LSquare) -> None:
        self.lsquare = lsquare
        self.arguments = {"query": Big(text="x" * 1_000_000)}
        self.context = Big(text="y" * 1_000_000)
        self.given = [weakref.ref(self.arguments["query"]), weakref.ref(self.context)]
        self.record: list[weakref.ref[Running]] = []

    async def call(self, request: Any) -> Any:
        scope = self.lsquare._lsquare_internal().calls().get()
        assert scope is not None and isinstance(scope.wrapper, Running)
        self.record.append(weakref.ref(scope.wrapper))
        return "ok"


@pytest.mark.parametrize("given", ["kept", "deleted"])
@pytest.mark.parametrize("kind", ["sync", "async"])
async def test_an_identify_still_running_once_the_calls_event_is_sent_holds_only_what_it_keeps_itself(kind: str, given: str) -> None:
    # Past its deadline the event goes without it, and is sent. identify runs on, ignoring its
    # abort: what reacts to its settling, its deadline and its input's making hold neither the
    # call's record nor what the call came with. What it was given is its own, kept or deleted.
    lsquare, held = _held(flush_timeout_ms=100)
    go = threading.Event()
    inputs: list[Any] = []
    running = threading.Event()
    # Set as identify returns: what's checked while it runs on must be checked before then.
    finished = threading.Event()

    def blocking(call: Any) -> Any:
        if given == "kept":
            inputs.append(call)
        del call
        running.set()
        go.wait(10)
        finished.set()
        return {"userId": "u-1"}

    async def running_on(call: Any) -> Any:
        if given == "kept":
            inputs.append(call)
        del call
        running.set()
        while not go.is_set():
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                pass
        finished.set()
        return {"userId": "u-1"}

    internal = lsquare._lsquare_internal()
    settings = read_options(internal, blocking if kind == "sync" else running_on, True, False, None, None, True)
    state = State(internal=internal, settings=settings, line="v1", low=object(), high=None, registry=None, high_extras=NO_EXTRAS)
    dispatch = _Given(lsquare)
    try:
        assert await on_call(state, dispatch) == "ok"
        # The test's own references go: only weak ones are left.
        dispatch.arguments, dispatch.context = {}, None
        assert await asyncio.to_thread(running.wait, 5)
        held.release()
        await lsquare.aflush(timeout_ms=5_000)
        [event] = held.acknowledged("tool_call")
        # Limited diagnostics: the arguments aren't in it.
        assert event["identity"] == "failed" and "input" not in event
        # Python 3.10 keeps a call's argument on its caller's stack until it returns, whatever the
        # function does with it: a sync identify's input is kept then.
        let_go = given == "deleted" and (kind == "async" or sys.version_info >= (3, 11))
        # What goes is waited for first: what stays has then had the time to go, and didn't.
        assert await acollected(dispatch.record[0], *(dispatch.given if let_go else []))
        assert not go.is_set() and not finished.is_set()
        assert [ref() is not None for ref in dispatch.given] == [not let_go] * 2
    finally:
        go.set()
        held.release()
        await lsquare.ashutdown()
