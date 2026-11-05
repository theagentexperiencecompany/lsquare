"""identify, and the waits for a call's event: flush_before_reply, flush() inside a tool and
wait_until, on each line."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import threading
import time
import uuid
import weakref
from collections.abc import Callable
from typing import Any

import pytest

from helpers import FakeTransport, Held, Recorder, acollected
from lines import MAJOR, client, connect, high, low, needs_line, tool
from lsquare import LSquare
from lsquare._sending import Reply
from lsquare._normalize import turn_key
from lsquare.mcp import IdentifyInput, instrument

pytestmark = [needs_line, pytest.mark.anyio]
OFF = {"sessions": False, "intent": False, "missing_tools": False}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def search(query: str) -> str:
    return f"found {query}"


async def one_call(lsquare: LSquare, arguments: dict[str, Any] | None = None, **options: Any) -> Any:
    app = high()
    tool(app, search)
    instrument(app, lsquare, **{"extras": OFF, **options})
    async with connect(app) as connection:
        reply = await connection.call("search", arguments or {"query": "a"})
    await lsquare.aflush(timeout_ms=5_000)
    return reply


async def test_gives_the_ids_identify_returns_and_capture_inside_the_call_wins() -> None:
    lsquare, events = client()
    got: list[IdentifyInput] = []
    aborted_then: list[bool] = []

    def identify(call: IdentifyInput) -> Any:
        got.append(call)
        aborted_then.append(call.signal.aborted)
        return {"orgId": "org-1", "userId": "user-1", "conversationId": "conv-1", "metadata": {"plan": "pro"}}

    app = high()

    def answering(query: str) -> str:
        lsquare.capture({"question": query, "answer": "x", "userId": "user-2"})
        return "ok"

    tool(app, answering)
    instrument(app, lsquare, identify=identify)
    async with connect(app) as connection:
        await connection.call("answering", {"query": "q", "intent": "why", "session_id": "ses_" + "a" * 22})
    await lsquare.aflush(timeout_ms=5_000)
    [event] = events.calls()
    assert (event["orgId"], event["userId"], event["conversationId"]) == ("org-1", "user-2", "conv-1")
    assert event["metadata"] == {"plan": "pro"}
    [call] = got
    assert call.tool_name == "answering" and call.args == {"query": "q"}
    # It answered in time: its signal never fires, when the event is built or after.
    assert call.context is not None and aborted_then == [False] and not call.signal.aborted


async def test_sends_limited_diagnostics_when_identify_fails_unless_capture_supplied_the_ids() -> None:
    recorder = Recorder()
    lsquare, events = client(on_error=recorder.on_error)

    def failing(call: IdentifyInput) -> Any:
        raise RuntimeError("no auth")

    await one_call(lsquare, identify=failing)
    [event] = events.calls()
    assert event["identity"] == "failed" and "input" not in event and "result" not in event
    assert recorder.wait_for(lambda: "identify_failed" in recorder.codes())

    lsquare, events = client()
    app = high()

    def supplying(query: str) -> str:
        lsquare.capture({"question": query, "answer": "x", "userId": "user-9"})
        return "ok"

    tool(app, supplying)
    instrument(app, lsquare, identify=failing, extras=OFF)
    async with connect(app) as connection:
        await connection.call("supplying", {"query": "q"})
    await lsquare.aflush(timeout_ms=5_000)
    [event] = events.calls()
    assert "identity" not in event and event["userId"] == "user-9"


async def test_takes_a_uuid_as_its_string_from_identify_and_from_capture_inside_the_call() -> None:
    # Converted where they come in: a UUID conversationId groups the call, as its string would, and
    # the call's turnRef is the string's.
    lsquare, events = client()
    org, user, conversation = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    app = high()

    def answering(query: str) -> str:
        lsquare.capture({"question": query, "answer": "x", "conversationId": conversation, "turnId": 7})
        return "ok"

    def alone(query: str) -> str:
        return "ok"

    tool(app, answering)
    tool(app, alone)
    instrument(app, lsquare, identify=lambda call: {"orgId": org, "userId": user})
    async with connect(app) as connection:
        grouped = await connection.call("answering", {"query": "q"})
        ungrouped = await connection.call("alone", {"query": "q"})
    await lsquare.aflush(timeout_ms=5_000)
    event = {event["toolName"]: event for event in events.calls()}["answering"]
    assert (event["orgId"], event["userId"], event["conversationId"]) == (str(org), str(user), str(conversation))
    assert event["turnRef"] == turn_key(str(org), str(conversation), "7") and "identity" not in event
    assert not any(text.startswith("Task ID: ") for text in grouped.texts), grouped.texts
    assert any(text.startswith("Task ID: ") for text in ungrouped.texts), ungrouped.texts


async def test_treats_an_invalid_id_as_a_failure() -> None:
    lsquare, events = client()
    await one_call(lsquare, identify=lambda call: {"userId": 42})
    [event] = events.calls()
    assert event["identity"] == "failed"


async def test_skips_identify_past_its_limit_and_sends_the_call_as_limited_diagnostics() -> None:
    lsquare, events = client(max_concurrent_hooks=1)
    release = threading.Event()
    entered = threading.Event()

    def blocking(call: IdentifyInput) -> Any:
        entered.set()
        release.wait(5)
        return {"userId": "u"}

    app = high()
    tool(app, search)
    instrument(app, lsquare, identify=blocking, extras=OFF)
    try:
        async with connect(app) as connection:
            await connection.call("search", {"query": "first"})
            assert entered.wait(2)
            await connection.call("search", {"query": "second"})
        await lsquare.aflush(timeout_ms=2_500)
    finally:
        release.set()
    # The first waited past its deadline; the second was never called.
    assert len(events.calls()) == 2
    assert all(event["identity"] == "failed" for event in events.calls())


async def test_waits_for_a_slow_identify_without_delaying_the_reply_then_builds_the_event_on_the_calls_loop() -> None:
    lsquare, events = client()
    threads: list[str] = []
    original = None

    def slow(call: IdentifyInput) -> Any:
        time.sleep(0.3)
        return {"userId": "slow-user"}

    app = high()
    tool(app, search)
    instrument(app, lsquare, identify=slow, extras=OFF)
    internal = lsquare._lsquare_internal()
    original = internal.record

    def recording(call: Any, loop: Any = None, origin: Any = None, diagnostic: Any = None) -> None:
        threads.append(threading.current_thread().name)
        original(call, loop=loop, origin=origin, diagnostic=diagnostic)

    internal.record = recording  # type: ignore[method-assign]
    async with connect(app) as connection:
        started = time.monotonic()
        await connection.call("search", {"query": "a"})
        replied = time.monotonic() - started
    await lsquare.aflush(timeout_ms=5_000)
    assert replied < 0.25
    [event] = events.calls()
    assert event["userId"] == "slow-user"
    assert threads == [threading.main_thread().name]


async def test_sends_limited_diagnostics_when_identify_misses_its_deadline_and_aborts_its_signal() -> None:
    lsquare, events = client()
    aborted = threading.Event()

    def late(call: IdentifyInput) -> Any:
        call.signal.add_listener(aborted.set)
        time.sleep(1.4)
        return {"userId": "too-late"}

    await one_call(lsquare, identify=late)
    [event] = events.calls()
    assert event["identity"] == "failed"
    assert aborted.wait(2)


@pytest.mark.parametrize("how", ["sync", "async", "awaitable"])
async def test_never_aborts_the_signal_of_an_identify_that_answered(how: str) -> None:
    lsquare, events = client()
    signals: list[Any] = []

    async def answer() -> Any:
        return {"userId": "user-1"}

    def answering(call: IdentifyInput) -> Any:
        signals.append(call.signal)
        return answer() if how == "awaitable" else {"userId": "user-1"}

    async def answering_async(call: IdentifyInput) -> Any:
        return answering(call)

    await one_call(lsquare, identify=answering_async if how == "async" else answering)
    await one_call(lsquare, identify=answering_async if how == "async" else answering)
    # Past the deadline as well: an abort left to it would have happened by now.
    await asyncio.sleep(1.2)
    assert [event["userId"] for event in events.calls()] == ["user-1", "user-1"]
    assert [signal.aborted for signal in signals] == [False, False]


async def test_an_async_identify_that_answered_is_not_aborted_by_the_shutdown_it_set_off() -> None:
    lsquare, _ = client()
    signals: list[Any] = []

    async def identify(call: IdentifyInput) -> Any:
        signals.append(call.signal)
        # Runs on the loop before the hook's task has settled: shutdown() still finds it registered.
        asyncio.get_running_loop().call_soon(lambda: lsquare.shutdown(timeout_ms=0))
        return {"userId": "user-1"}

    await one_call(lsquare, identify=identify)
    await asyncio.sleep(0.05)
    assert len(signals) == 1 and not signals[0].aborted


async def test_runs_an_async_identify_on_the_calls_loop() -> None:
    lsquare, events = client()
    loops: list[Any] = []

    async def identify(call: IdentifyInput) -> Any:
        loops.append(asyncio.get_running_loop())
        await asyncio.sleep(0.01)
        return {"orgId": "org-a"}

    app = high()
    tool(app, search)
    instrument(app, lsquare, identify=identify, extras=OFF)
    async with connect(app) as connection:
        await connection.call("search", {"query": "a"})
        here = asyncio.get_running_loop()
    await lsquare.aflush(timeout_ms=5_000)
    [event] = events.calls()
    assert event["orgId"] == "org-a"
    assert loops == [here]


async def test_knows_the_ids_identify_returned_last_as_soon_as_it_succeeds() -> None:
    lsquare, events = client()
    calls = {"n": 0}

    def identify(call: IdentifyInput) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            return {"orgId": "o", "userId": "u"}
        raise RuntimeError("down")

    app = high()

    def capturing(query: str) -> str:
        if query == "second":
            lsquare.capture({"question": "q", "answer": "a", "userId": "u2"})
        return "ok"

    tool(app, capturing)
    instrument(app, lsquare, identify=identify, extras=OFF)
    async with connect(app) as connection:
        await connection.call("capturing", {"query": "first"})
        await asyncio.sleep(0.1)
        await connection.call("capturing", {"query": "second"})
    await lsquare.aflush(timeout_ms=5_000)
    first, second = events.calls()
    assert first["orgId"] == "o"
    # capture() gave a userId but not the orgId identify last gave: still diagnostics.
    assert second["identity"] == "failed"


async def test_sends_the_event_before_the_reply_with_flush_before_reply_or_after_flush_inside_the_tool() -> None:
    for how in ("option", "flush"):
        lsquare, events = client()
        app = high()

        async def flushing(query: str) -> str:
            if how == "flush":
                await lsquare.aflush()
            return "ok"

        tool(app, flushing)
        instrument(app, lsquare, extras=OFF, flush_before_reply=how == "option")
        async with connect(app) as connection:
            await connection.call("flushing", {"query": "a"})
            assert len(events.calls()) == 1, how


async def test_hands_each_calls_sending_to_wait_until() -> None:
    lsquare, events = client()
    handed: list[Any] = []
    app = high()
    tool(app, search)
    instrument(app, lsquare, extras=OFF, wait_until=handed.append)
    async with connect(app) as connection:
        await connection.call("search", {"query": "a"})
        assert len(handed) == 1 and asyncio.iscoroutine(handed[0])
        await handed[0]
        assert len(events.calls()) == 1


def held_client(**options: Any) -> tuple[LSquare, Held]:
    held = Held()
    return LSquare(api_key="lsq_test_key", endpoint="http://localhost:1", _transport=held, **options), held


async def until(check: Callable[[], bool]) -> None:
    """Waits until `check` holds, at most 5 s."""
    for _ in range(500):
        if check():
            return
        await asyncio.sleep(0.01)
    assert check()


def raising() -> Exception:
    """What a low-level server's handler raises that reaches the wrapper on this line, rather than
    becoming an isError result."""
    if MAJOR == 1:
        from mcp.shared.exceptions import UrlElicitationRequiredError

        error: Exception = UrlElicitationRequiredError([])
        return error
    return ValueError("lookup failed")


TOOLS = [{"name": "lookup", "inputSchema": {"type": "object"}}]


async def test_flush_inside_the_tool_with_wait_until_replies_within_its_timeout_and_still_hands_the_rest_over() -> None:
    lsquare, held = held_client(flush_timeout_ms=50)
    handed: list[Any] = []
    app = high()

    async def noted(query: str) -> str:
        lsquare.capture({"question": query})
        await lsquare.aflush()
        return "ok"

    tool(app, noted)
    instrument(app, lsquare, extras=OFF, wait_until=handed.append)
    async with connect(app) as connection:
        await connection.call("noted", {"query": "a"})
    # The reply gave up waiting at flush_timeout_ms, the ingestion still holding its answer.
    assert held.acknowledged("tool_call") == [] and len(handed) == 1
    task = asyncio.ensure_future(handed[0])
    await asyncio.sleep(0.05)
    assert not task.done()
    held.release()
    await asyncio.wait_for(task, 5)
    [event] = held.acknowledged("tool_call")
    assert event["agent"]["question"] == "a"
    await lsquare.ashutdown()


@pytest.mark.parametrize("how", ["option", "flush"])
async def test_a_call_that_raises_waits_as_one_that_returns_with_flush_before_reply_or_flush_inside_the_tool(how: str) -> None:
    lsquare, held = held_client()

    async def lookup(name: str, arguments: dict[str, Any]) -> Any:
        if how == "flush":
            await lsquare.aflush(timeout_ms=0)
        raise raising()

    server = low("lookup", TOOLS, lookup)
    instrument(server, lsquare, extras=OFF, flush_before_reply=how == "option")
    async with connect(server) as connection:

        async def call() -> int:
            with pytest.raises(Exception):
                await connection.call("lookup", {})
            # The error came only once the event was acknowledged.
            return len(held.acknowledged("tool_call"))

        task = asyncio.ensure_future(call())
        assert await asyncio.to_thread(held.entered.wait, 5)
        await asyncio.sleep(0.05)
        assert not task.done()
        held.release()
        assert await asyncio.wait_for(task, 5) == 1
    await lsquare.ashutdown()


async def test_a_call_that_raises_hands_its_sending_to_wait_until() -> None:
    lsquare, held = held_client()
    handed: list[Any] = []

    async def lookup(name: str, arguments: dict[str, Any]) -> Any:
        raise raising()

    server = low("lookup", TOOLS, lookup)
    instrument(server, lsquare, extras=OFF, wait_until=handed.append)
    async with connect(server) as connection:
        with pytest.raises(Exception):
            await connection.call("lookup", {})
    assert len(handed) == 1
    held.release()
    await asyncio.wait_for(handed[0], 5)
    [event] = held.acknowledged("tool_call")
    assert event["status"] == "error"
    await lsquare.ashutdown()


async def test_a_cancelled_call_hands_its_sending_to_wait_until() -> None:
    import anyio

    lsquare, held = held_client()
    handed: list[Any] = []
    app = high()
    started = anyio.Event()

    async def slow(query: str) -> str:
        started.set()
        await anyio.sleep(30)
        return "late"

    tool(app, slow)
    instrument(app, lsquare, extras=OFF, wait_until=handed.append)
    async with connect(app) as connection:
        async with anyio.create_task_group() as group:
            group.start_soon(connection.call, "slow", {"query": "a"})
            await started.wait()
            group.cancel_scope.cancel()
    # The client gave up on the call: the server cancels the tool once it hears so, or, as mcp 1.x
    # clients don't say, once the connection closes.
    await until(lambda: len(handed) == 1)
    held.release()
    await asyncio.wait_for(handed[0], 5)
    [event] = held.acknowledged("tool_call")
    assert event["error"]["category"] == "timeout"
    await lsquare.ashutdown()


class Big(dict[str, str]):
    """A tool's result a weak reference can follow."""


async def test_what_wait_until_is_given_lets_go_of_the_tools_result_with_the_reply() -> None:
    # It keeps the call's ID and the turns still being sent, nothing else of the call: a result
    # it doesn't record isn't held for as long as the ingestion takes, outside the queue's bound.
    lsquare, held = held_client()
    handed: list[Any] = []
    results: list[weakref.ref[Big]] = []
    app = high()

    def large(query: str) -> dict[str, str]:
        out = Big(text="x" * 1_000_000)
        results.append(weakref.ref(out))
        return out

    tool(app, large)
    instrument(app, lsquare, extras=OFF, results=False, wait_until=handed.append)
    async with connect(app) as connection:
        await connection.call("large", {"query": "a"})
    waited = asyncio.ensure_future(handed[0])
    assert await asyncio.to_thread(held.entered.wait, 5)
    assert await acollected(results[0])
    assert not waited.done()
    held.release()
    await asyncio.wait_for(waited, 5)
    assert len(held.acknowledged("tool_call")) == 1
    await lsquare.ashutdown()


async def test_what_wait_until_is_given_lets_go_of_what_before_send_returned_for_a_turn_already_sent() -> None:
    # It still waits for the second turn, and holds the first one's handle: that handle keeps
    # nothing of its hook, whose result isn't held for as long as the second turn takes.
    returned: dict[str, weakref.ref[Big]] = {}

    async def before_send(event: dict[str, Any]) -> Any:
        out = Big(event)
        question = event.get("question")
        if isinstance(question, str):
            returned[question] = weakref.ref(out)
        return out

    lsquare, held = held_client(before_send=before_send)
    handed: list[Any] = []
    app = high()

    async def turns(query: str) -> str:
        lsquare.capture({"question": "agent part"})
        lsquare.capture({"conversationId": "c1", "turnId": 1, "question": "first"})
        await until(lambda: len(held.batches) == 1)
        lsquare.capture({"conversationId": "c1", "turnId": 2, "question": "second"})
        await until(lambda: len(held.batches) == 2)
        return "ok"

    tool(app, turns)
    instrument(app, lsquare, extras=OFF, wait_until=handed.append)
    async with connect(app) as connection:
        await connection.call("turns", {"query": "a"})
    waited = asyncio.ensure_future(handed[0])
    await asyncio.sleep(0)
    held.release(0)
    # The first turn is sent: the second and the call's event are left.
    await until(lambda: lsquare.flush(timeout_ms=0).pending == 2)
    assert await acollected(returned["first"])
    assert not waited.done()
    held.release()
    await asyncio.wait_for(waited, 5)
    assert [event["question"] for event in held.acknowledged("turn")] == ["first", "second"]
    await lsquare.ashutdown()


#: An app's own variable, set inside a tool.
APP_VAR: contextvars.ContextVar[str | None] = contextvars.ContextVar("app_var", default=None)


@pytest.mark.parametrize("how", ["built", "built_after_identify", "dropped", "failed"])
async def test_a_turn_captured_inside_the_call_and_still_being_sent_keeps_the_calls_id_not_its_result(
    monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    # The turn runs its hook and its sending in its capture's context, the app's variables in it,
    # and the call's scope too: once the call's event is built, or dropped for good, that scope
    # keeps the call's ID, not what the tool returned, outside the queue's bound.
    from lsquare.mcp import _call

    seen: list[str | None] = []

    def before_send(event: dict[str, Any]) -> Any:
        if event["kind"] == "turn":
            seen.append(APP_VAR.get())
        return event

    recorder = Recorder()
    lsquare, held = held_client(before_send=before_send, on_error=recorder.on_error)
    results: list[weakref.ref[Big]] = []
    scopes: list[Any] = []
    go = threading.Event()
    options: dict[str, Any] = {}
    if how != "built":

        def identify(call: IdentifyInput) -> Any:
            go.wait(5)
            return {"userId": "u-1"}

        options["identify"] = identify
    if how == "failed":

        def broken(*args: Any) -> Any:
            raise RuntimeError("broken")

        monkeypatch.setattr(_call, "hand_off", broken)
    app = high()

    async def large(query: str) -> dict[str, str]:
        APP_VAR.set("set in the tool")
        scopes.append(lsquare._lsquare_internal().calls().get())
        lsquare.capture({"question": "agent part"})
        lsquare.capture({"conversationId": "c1", "turnId": 1, "question": "a turn of its own"})
        out = Big(text="x" * 1_000_000)
        results.append(weakref.ref(out))
        return out

    tool(app, large)
    instrument(app, lsquare, extras=OFF, results=False, **options)
    internal = lsquare._lsquare_internal()
    if how == "dropped":
        # No thread takes the build at identify's deadline: the event is dropped.
        monkeypatch.setattr(lsquare._builds, "submit", lambda job: False)
        monkeypatch.setattr(internal, "call_on_loop", lambda origin, fn: False)
    try:
        async with connect(app) as connection:
            await connection.call("large", {"query": "a"})
            if how == "built_after_identify":
                # The reply didn't wait: the event is built once identify answers, its agent part in it.
                go.set()
                await until(lambda: len(held.batches) == 2)
            elif how == "dropped":
                assert await asyncio.to_thread(recorder.wait_for, lambda: any("No thread could build" in error.message for error in recorder.errors))
            assert await acollected(results[0])
            # The turn is still being sent.
            assert lsquare.flush(timeout_ms=0).pending >= 1
            # What the turn's context holds: the call's scope, the wrapper's own parts let go of.
            [scope] = scopes
            assert (scope.agent, scope.thrown, scope.tool_result, scope.on_tool_entry, scope.wrapper) == (None,) * 5
            assert scope.returned and scope.invoked
        held.release()
        await lsquare.aflush(timeout_ms=5_000)
        assert seen == ["set in the tool"]
        [captured] = held.acknowledged("turn")
        assert captured["callId"] == scope.call_id
        calls = held.acknowledged("tool_call")
        if how in ("built", "built_after_identify"):
            [event] = calls
            assert event["callId"] == scope.call_id and event["agent"]["question"] == "agent part"
            assert event.get("userId") == (None if how == "built" else "u-1")
        else:
            assert calls == []
    finally:
        go.set()
        await lsquare.ashutdown()


async def test_an_identify_still_running_once_the_calls_event_is_built_holds_neither_the_record_nor_the_result() -> None:
    # Past its deadline the event goes without it; identify runs on. What reacts to its settling
    # holds identify's own state, not the call's record, with what the layers inside the SDK noted
    # there of the result for the reply's ID line (fastmcp's and the MCP SDK 2.x middleware's).
    from lsquare.mcp._call import Running

    lsquare, held = held_client(flush_timeout_ms=100)
    go = threading.Event()
    running = threading.Event()
    # Set as identify returns: what's checked while it runs on must be checked before then.
    finished = threading.Event()

    def identify(call: IdentifyInput) -> Any:
        running.set()
        go.wait(10)
        finished.set()
        return {"userId": "u-1"}

    records: list[weakref.ref[Running]] = []
    app = high()

    def large(query: str) -> dict[str, str]:
        scope = lsquare._lsquare_internal().calls().get()
        assert scope is not None and isinstance(scope.wrapper, Running)
        records.append(weakref.ref(scope.wrapper))
        return {"text": "x" * 1_000}

    tool(app, large)
    instrument(app, lsquare, extras=OFF, results=False, identify=identify, flush_before_reply=True)
    try:
        async with connect(app) as connection:
            await connection.call("large", {"query": "a"})
        assert await asyncio.to_thread(held.entered.wait, 5)
        assert running.is_set() and not go.is_set()
        assert await acollected(records[0])
        assert not finished.is_set()
    finally:
        go.set()
        held.release()
        await lsquare.ashutdown()


@pytest.mark.parametrize("how", ["wait_until", "flush_before_reply"])
async def test_waits_for_the_turns_captured_inside_the_call_with_its_event(how: str) -> None:
    lsquare, held = held_client(flush_timeout_ms=10_000)
    handed: list[Any] = []
    app = high()

    async def turns(query: str) -> str:
        lsquare.capture({"question": "agent part"})
        lsquare.capture({"conversationId": "c1", "turnId": 2, "question": "a turn of its own"})
        # The turn goes first, in a batch of its own.
        await until(lambda: len(held.batches) == 1)
        return "ok"

    tool(app, turns)
    options: dict[str, Any] = {"wait_until": handed.append} if how == "wait_until" else {"flush_before_reply": True}
    instrument(app, lsquare, extras=OFF, **options)
    async with connect(app) as connection:
        reply = asyncio.ensure_future(connection.call("turns", {"query": "a"}))
        await until(lambda: len(held.batches) == 2)
        assert held.kinds() == [["turn"], ["tool_call"]]
        if how == "wait_until":
            await asyncio.wait_for(reply, 5)
            # What waits: the coroutine wait_until was handed, or the reply itself.
            waited = asyncio.ensure_future(handed[0])
        else:
            waited = reply
        held.release(1)
        # The call's event is sent: only the turn is left.
        await until(lambda: lsquare.flush(timeout_ms=0).pending == 1)
        await asyncio.sleep(0.05)
        assert not waited.done()
        held.release(0)
        await asyncio.wait_for(waited, 5)
        assert held.answered == [True, True]
    await lsquare.ashutdown()


async def test_waits_for_each_turn_captured_inside_the_call_on_its_own_two_that_share_an_event_id_included() -> None:
    skip = threading.Event()

    def before_send(event: dict[str, Any]) -> Any:
        # The first turn's hook holds until it is skipped; the rest go at once.
        if event.get("question") == "first":
            skip.wait(5)
            return None
        return event

    lsquare, held = held_client(before_send=before_send)
    handed: list[Any] = []
    app = high()

    async def turns(query: str) -> str:
        lsquare.capture({"question": "agent part"})
        # The same conversationId and turnId: one event ID for both.
        lsquare.capture({"conversationId": "c1", "turnId": 2, "question": "first"})
        lsquare.capture({"conversationId": "c1", "turnId": 2, "question": "second"})
        await until(lambda: len(held.batches) == 1)
        return "ok"

    tool(app, turns)
    instrument(app, lsquare, extras=OFF, wait_until=handed.append)
    async with connect(app) as connection:
        await connection.call("turns", {"query": "a"})
    await until(lambda: len(held.batches) == 2)
    assert [[event.get("question", event["kind"]) for event in batch] for batch in held.batches] == [["second"], ["tool_call"]]
    waited = asyncio.ensure_future(handed[0])
    # Its first step says what it waits for, both turns still queued.
    await asyncio.sleep(0)
    skip.set()
    held.release(1)
    # The first turn skipped, the call's event sent: only the second turn is left.
    await until(lambda: lsquare.flush(timeout_ms=0).pending == 1)
    await asyncio.sleep(0.05)
    assert not waited.done()
    held.release(0)
    await asyncio.wait_for(waited, 5)
    assert held.answered == [True, True]
    await lsquare.ashutdown()


async def test_waits_for_identify_up_to_a_second_in_the_background_whatever_flush_timeout_ms_is() -> None:
    # Neither wait_until nor a reply that waits: flush_timeout_ms bounds neither the sending nor identify.
    lsquare, events = client(flush_timeout_ms=0)

    async def identify(call: IdentifyInput) -> Any:
        await asyncio.sleep(0.05)
        return {"userId": "u-1"}

    await one_call(lsquare, identify=identify)
    [event] = events.calls()
    assert event["userId"] == "u-1"


async def test_a_reply_wait_cancelled_midway_ends_at_once_and_cancels_nothing_shared() -> None:
    from lsquare.mcp._call import sent_by

    lsquare, _ = client()
    internal = lsquare._lsquare_internal()
    shared: concurrent.futures.Future[None] = concurrent.futures.Future()
    task = asyncio.ensure_future(sent_by(internal, "call-id", shared, time.monotonic() + 10))
    await asyncio.sleep(0.05)
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - started < 0.5
    assert not shared.cancelled() and not shared.done()


# What wait_until is given ends once what was reported about the call has reached on_error: the
# host may freeze the process then, the callback thread with it.


def slow_on_error(codes: list[str]) -> Callable[..., None]:
    """A sync on_error that takes a moment, as one writing to a file or a socket does: what waited
    for its report would end first without waiting for it, and it ends well within the grace."""

    def on_error(error: Any) -> None:
        time.sleep(0.1)
        codes.append(error.code)

    return on_error


async def test_what_wait_until_is_given_ends_once_the_calls_failure_has_reached_on_error() -> None:
    codes: list[str] = []
    lsquare = LSquare(api_key="lsq_test_key", endpoint="http://localhost:1", _transport=FakeTransport(lambda events: Reply(400, None, "")), on_error=slow_on_error(codes))
    handed: list[Any] = []
    app = high()
    tool(app, search)
    instrument(app, lsquare, extras=OFF, wait_until=handed.append)
    async with connect(app) as connection:
        await connection.call("search", {"query": "a"})
        await asyncio.wait_for(handed[0], 5)
        assert codes == ["send_failed"]
    await lsquare.ashutdown()


async def test_what_wait_until_is_given_ends_once_a_call_refused_as_its_event_was_queued_was_reported() -> None:
    codes: list[str] = []
    lsquare, _ = client(max_queue_bytes=64, on_error=slow_on_error(codes))
    handed: list[Any] = []
    app = high()
    tool(app, search)
    instrument(app, lsquare, extras=OFF, wait_until=handed.append)
    async with connect(app) as connection:
        await connection.call("search", {"query": "a"})
        await asyncio.wait_for(handed[0], 5)
        assert codes == ["queue_full"]
    await lsquare.ashutdown()


async def test_wait_until_is_given_what_ends_once_a_failure_to_record_the_call_was_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    from lsquare.mcp import _call

    def broken(*args: Any) -> Any:
        raise RuntimeError("broken")

    monkeypatch.setattr(_call, "hand_off", broken)
    codes: list[str] = []
    lsquare, _ = client(on_error=slow_on_error(codes))
    handed: list[Any] = []
    app = high()
    tool(app, search)
    instrument(app, lsquare, extras=OFF, wait_until=handed.append)
    async with connect(app) as connection:
        await connection.call("search", {"query": "a"})
        assert len(handed) == 1
        await asyncio.wait_for(handed[0], 5)
        assert codes == ["internal_error"]
    await lsquare.ashutdown()


@pytest.mark.parametrize("how", ["flush_before_reply", "aflush", "flush", "aflush_and_wait_until", "flush_and_wait_until"])
async def test_a_reply_held_for_its_event_never_waits_for_a_busy_callback_thread(monkeypatch: pytest.MonkeyPatch, how: str) -> None:
    from lsquare import _client
    from lsquare.mcp import _call

    # A grace long enough to notice: a reply that waited for the stuck handler would fail the test.
    monkeypatch.setattr(_client, "DELIVERED_GRACE_S", 30.0)
    monkeypatch.setattr(_call, "DELIVERED_GRACE_S", 30.0)
    gate = threading.Event()
    lsquare, events = client(on_error=lambda error: gate.wait(10))
    handed: list[Any] = []
    app = high()

    async def flushes_on_the_loop(query: str) -> str:
        await lsquare.aflush()
        return "ok"

    def flushes(query: str) -> str:
        # Blocking: on a line that runs a sync tool on the loop, the loop too.
        lsquare.flush()
        return "ok"

    tools: dict[str, Callable[[str], Any]] = {"flush_before_reply": search, "aflush": flushes_on_the_loop, "flush": flushes}
    tool(app, tools[how.removesuffix("_and_wait_until")], name="noted")
    options: dict[str, Any] = (
        {"flush_before_reply": True} if how == "flush_before_reply" else {"wait_until": handed.append} if how.endswith("wait_until") else {}
    )
    instrument(app, lsquare, extras=OFF, **options)
    try:
        # A report about something else, whose handler doesn't return.
        lsquare.capture({"conversationId": 7, "question": "q"})
        async with connect(app) as connection:
            started = time.monotonic()
            await asyncio.wait_for(connection.call("noted", {"query": "a"}), 20)
            # Timed as well: a flush() blocking the loop holds wait_for's own timeout back too.
            assert time.monotonic() - started < 5
            assert len(events.calls()) == 1
    finally:
        gate.set()
        for each in handed:
            each.close()
    await lsquare.ashutdown(timeout_ms=0)


@pytest.mark.parametrize("anyio_backend", ["trio"])
async def test_on_trio_a_sync_identify_runs_and_an_async_one_is_refused_with_one_warning(anyio_backend: str) -> None:
    pytest.importorskip("trio")
    recorder = Recorder()
    for identify in (lambda call: {"userId": "sync"}, _async_identify):
        lsquare, events = client(logger=recorder)
        app = high()
        tool(app, search)
        instrument(app, lsquare, identify=identify, extras=OFF)
        try:
            async with connect(app) as connection:
                await connection.call("search", {"query": "a"})
        except Exception as error:  # an SDK client that needs asyncio
            pytest.skip(f"this line's in-memory client doesn't run on trio: {error!r}")
        await lsquare.aflush(timeout_ms=5_000)
        [event] = events.calls()
        if identify is _async_identify:
            assert event["identity"] == "failed"
        else:
            assert event["userId"] == "sync"
    assert recorder.wait_for(lambda: sum("async identify needs asyncio" in line for _, line in recorder.lines) == 1)


async def _async_identify(call: IdentifyInput) -> Any:
    return {"userId": "async"}
