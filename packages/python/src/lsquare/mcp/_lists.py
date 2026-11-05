"""A low-level server's tool list as the extras read it (Python spec, section 5, "Low-level
servers"), ported from packages/lsquare/src/mcp/lists.ts: one snapshot per instance, read from its
own tools/list handler, every page, and kept until something says the list changed.

The read runs as a task of its own on the loop of the request that starts it (an asyncio task, or
a trio system task), so cancelling a waiting call never stops it. A coordinator, not the task,
decides when it's given up: at 10 seconds the reading expires on the client's scheduler, its
waiters wake, the pause starts and the task is asked to cancel; whatever it gives later is ignored.
Calls wait through concurrent futures, from any thread or loop.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .._client import Internal
from .._threads import interrupts
from ._extras import MISSING_TOOLS, Ownership, listed_ownership
from ._read import loaded

MAX_PAGES = 100
MAX_TOOLS = 10_000
#: How long a call waits for the snapshot, from the start of the read it waits on.
WAIT_S = 1.0
#: A read still running after this is given up.
GIVE_UP_S = 10.0
#: After a read fails or is given up, none starts for this long, unless the server sends list_changed.
PAUSE_S = 60.0
#: Reads overtaken within one wait before the list counts as changing all the time.
ROUNDS = 10


@dataclass(frozen=True)
class Snapshot:
    #: Each tool's ownership of the extras' inputs, by name.
    tools: Mapping[str, Ownership]
    #: The server has a tool of its own called get_more_tools.
    own_missing_tools: bool


@dataclass(frozen=True)
class Reader:
    """How a read gets the list: one page by its cursor, run in `context`, a copy of the starting
    request's (its own call variable cleared, one callback level deeper)."""

    page: Callable[[str | None], Awaitable[Any]]
    context: contextvars.Context
    #: Whether the handler it reads is still the server's: a read of a replaced one is overtaken.
    current: Callable[[], bool] | None = None


@dataclass(eq=False)
class Reading:
    #: The generation it was started at: a later one discards it.
    generation: int
    started: float
    #: Done once the read is over: finished, failed, expired or discarded.
    done: concurrent.futures.Future[None] = field(default_factory=concurrent.futures.Future)
    #: The waits waiting on it now, under the lists' lock: each taken out as that wait ends, and
    #: all as the read is over, to wake. A read is kept for as long as its task runs, which a
    #: listing that ignores its cancellation may make forever: it keeps none of its waits.
    waiters: set[concurrent.futures.Future[None]] = field(default_factory=set)
    #: Given up at its deadline: its result is ignored whenever it comes.
    expired: bool = False
    #: Finished, failed or expired: the one outcome, claimed under the lists' lock.
    over: bool = False
    #: The request that started it was cancelled: a failure then isn't the list's.
    starter_cancelled: bool = False
    #: Asks the task to stop, from any thread, without waiting for it.
    cancel: Callable[[], None] | None = None
    give_up: Callable[[], None] | None = None
    reader: Reader | None = None


@dataclass(eq=False)
class Lists:
    """What an instance keeps of its tool list."""

    #: Bumped by everything that says the list changed.
    generation: int = 0
    installed: tuple[int, Snapshot] | None = None
    reading: Reading | None = None
    #: No read starts before this (monotonic seconds).
    paused_until: float = 0.0
    #: The requests waiting now, each woken at the next mark, as it discards the read they wait on:
    #: each wait's own, taken out as that wait ends, so none outlives it (see wait_once()).
    waiting: set[concurrent.futures.Future[None]] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)
    #: Read tasks still running, abandoned ones included, kept until they end.
    tasks: set[Any] = field(default_factory=set)


def mark(lists: Lists, list_changed: bool) -> None:
    """Something says the list changed: the snapshot and any read running are out of date. Only
    the server's own list_changed lifts a pause, as a failed read may have failed for what it
    changed."""
    with lists.lock:
        lists.generation += 1
        if list_changed:
            lists.paused_until = 0.0
        woken = _take_waiting(lists)
        reading = lists.reading
    for waiting in woken:
        _resolve(waiting)
    if reading is not None and reading.cancel is not None:
        _quietly(reading.cancel)


def fresh(lists: Lists) -> Snapshot | None:
    """The snapshot, if it's of the current generation."""
    installed = lists.installed
    return installed[1] if installed is not None and installed[0] == lists.generation else None


@dataclass(eq=False)
class Wait:
    """How long a request has waited for the snapshot, how many reads, and which it started."""

    #: 1 second after the start of the first read it waited on (monotonic seconds).
    deadline: float | None = None
    rounds: int = 0
    started: list[Reading] = field(default_factory=list)

    def cancelled(self) -> None:
        """The request that waited was cancelled: the reads it started know it."""
        for reading in self.started:
            reading.starter_cancelled = True


async def wait_once(lists: Lists, internal: Internal, reader: Callable[[], Reader | None], wait: Wait) -> bool:
    """Waits once for a snapshot of the current generation: joins its read, or starts one with
    the context of the request that needs it, until the read ends, the list changes or the
    request's deadline, 1 second after the start of the first read it waited on, passes. True when
    it's worth checking fresh() again. The caller checks fresh() itself, in the step that uses it."""
    either: concurrent.futures.Future[None] = concurrent.futures.Future()
    with lists.lock:
        now = time.monotonic()
        if fresh(lists) is not None or wait.rounds >= ROUNDS:
            return False
        if wait.deadline is not None and now >= wait.deadline:
            return False
        reading = lists.reading
        how: Reader | None = None
        if reading is None or reading.generation != lists.generation:
            if now < lists.paused_until:
                return False
            how = reader()
            if how is None:
                return False
            reading = Reading(generation=lists.generation, started=now, reader=how)
            lists.reading = reading
            wait.started.append(reading)
            spawn = True
        else:
            spawn = False
        wait.rounds += 1
        if wait.deadline is None:
            wait.deadline = reading.started + WAIT_S
        deadline = wait.deadline
        # In before a read starts: its handler may say the list changed the moment it runs. Taken
        # out as the wait ends: a future kept by the instance for every wait would keep each wait's
        # callback on it until the list next changes, which a list that fails, or keeps timing
        # out, may never do.
        lists.waiting.add(either)
    try:
        if spawn and how is not None:
            _start(lists, internal, reading, how)
        # Past the first second of a slow read, a request doesn't wait at all.
        left = deadline - time.monotonic()
        if left <= 0:
            return False
        # Woken as the read is over: one over already has woken its waits, so this one wakes now.
        with lists.lock:
            over = reading.over
            if not over:
                reading.waiters.add(either)
        if over:
            _resolve(either)
        await internal.wait(either, left)
        return either.done()
    finally:
        with lists.lock:
            lists.waiting.discard(either)
            reading.waiters.discard(either)


async def with_snapshot(lists: Lists, internal: Internal, reader: Callable[[], Reader | None], wait: Wait) -> Snapshot | None:
    """Waits for the snapshot as wait_once() does; returns it, or None, right after the last check."""
    while fresh(lists) is None and await wait_once(lists, internal, reader, wait):
        pass
    return fresh(lists)


def _start(lists: Lists, internal: Internal, reading: Reading, reader: Reader) -> None:
    """Runs the read as a task of its own, and arms its give-up. A read that can't start fails."""
    reading.give_up = internal.timer(GIVE_UP_S, lambda: _expire(lists, reading))
    try:
        asyncio_loop = _asyncio_loop()
        if asyncio_loop is not None:
            task = reader.context.run(asyncio_loop.create_task, _run(lists, reading, reader))
            loop = asyncio_loop

            def cancel_task() -> None:
                loop.call_soon_threadsafe(task.cancel)

            reading.cancel = cancel_task
            _keep(lists, task)
            return
        trio = loaded("trio")
        if trio is not None:
            token = trio.lowlevel.current_trio_token()
            scope = trio.CancelScope()

            def cancel_scope() -> None:
                token.run_sync_soon(scope.cancel)

            reading.cancel = cancel_scope
            # A system task starts in the context it's given, not the spawner's.
            task = trio.lowlevel.spawn_system_task(_run_trio, lists, reading, reader, scope, context=reader.context.copy())
            _keep(lists, task)
            return
        raise RuntimeError("No event loop to read the tool list on.")
    except Exception:
        _finish(lists, reading, None)


async def _run(lists: Lists, reading: Reading, reader: Reader) -> None:
    """Reads the snapshot and installs it if nothing changed meanwhile. Never raises, except a
    SystemExit or KeyboardInterrupt on the main thread (a signal handler's, or the list handler's
    own), passed on once the read is over."""
    snapshot: Snapshot | None = None
    try:
        snapshot = await _collect(reader)
    except BaseException as error:  # noqa: BLE001 - a detached read; its failure is the list's, or not
        if interrupts(error):
            _finish(lists, reading, None)
            raise
        snapshot = None
    _finish(lists, reading, snapshot)


async def _run_trio(lists: Lists, reading: Reading, reader: Reader, scope: Any) -> None:
    """The same as a trio system task: nothing it raises may reach trio, whatever it is, except a
    cancellation of the whole run, which trio needs back, and a SystemExit or KeyboardInterrupt on
    the main thread, which ends the run (as trio's TrioInternalError around it) instead of being
    swallowed."""
    trio = loaded("trio")
    snapshot: Snapshot | None = None
    try:
        with scope:
            snapshot = await _collect(reader)
    except BaseException as error:  # noqa: BLE001 - a system task that raised would end the run
        cancelled = getattr(trio, "Cancelled", None)
        if (isinstance(cancelled, type) and isinstance(error, cancelled)) or interrupts(error):
            raise
        snapshot = None
    finally:
        _finish(lists, reading, snapshot)
        # trio's tasks have no done callback: the task lets go of itself.
        try:
            current = trio.lowlevel.current_task() if trio is not None else None
            with lists.lock:
                lists.tasks.discard(current)
        except Exception:
            pass


def _finish(lists: Lists, reading: Reading, snapshot: Snapshot | None) -> None:
    stale = _stale(reading)
    woken: list[concurrent.futures.Future[None]] = []
    with lists.lock:
        # The read's one outcome: its own end, or its expiry, whichever claims it first.
        if not reading.over:
            reading.over = True
            if stale:
                woken = _overtaken(lists, reading)
            elif snapshot is None:
                _failed(lists, reading)
            elif lists.generation == reading.generation:
                lists.installed = (reading.generation, snapshot)
        woken += _take_waiters(reading)
        if lists.reading is reading:
            lists.reading = None
    for waiting in woken:
        _resolve(waiting)
    if reading.give_up is not None:
        _quietly(reading.give_up)
    _resolve(reading.done)


def _expire(lists: Lists, reading: Reading) -> None:
    """The read is given up: its waiters wake, reads pause, and its task is asked to stop."""
    stale = _stale(reading)
    woken: list[concurrent.futures.Future[None]] = []
    with lists.lock:
        if reading.over:
            return
        reading.over = True
        reading.expired = True
        if stale:
            woken = _overtaken(lists, reading)
        else:
            _failed(lists, reading)
        woken += _take_waiters(reading)
        if lists.reading is reading:
            lists.reading = None
    for waiting in woken:
        _resolve(waiting)
    _resolve(reading.done)
    if reading.cancel is not None:
        _quietly(reading.cancel)


def _stale(reading: Reading) -> bool:
    """The handler the read used was replaced meanwhile: it says nothing of the list now."""
    current = reading.reader.current if reading.reader is not None else None
    try:
        return current is not None and not current()
    except Exception:
        return True


def _overtaken(lists: Lists, reading: Reading) -> list[concurrent.futures.Future[None]]:
    """A read of a replaced handler: as if the list changed, so it installs nothing and pauses
    nothing, and its waiters read again. Returns the futures to resolve after the lock. Called under
    the lock."""
    if lists.generation != reading.generation:
        return []
    lists.generation += 1
    return _take_waiting(lists)


def _take_waiting(lists: Lists) -> list[concurrent.futures.Future[None]]:
    """The waits waiting now, taken out, to wake after the lock. Called under the lock."""
    woken = list(lists.waiting)
    lists.waiting.clear()
    return woken


def _take_waiters(reading: Reading) -> list[concurrent.futures.Future[None]]:
    """The waits waiting on a read that is now over, taken out, to wake after the lock. Called
    under the lock."""
    woken = list(reading.waiters)
    reading.waiters.clear()
    return woken


def _failed(lists: Lists, reading: Reading) -> None:
    """A failed read pauses new ones for a minute. One overtaken meanwhile, or whose starting
    request was cancelled (sending through that request's functions can fail then), is only
    discarded. Called under the lock."""
    if lists.generation != reading.generation or reading.starter_cancelled:
        return
    lists.paused_until = time.monotonic() + PAUSE_S


def _keep(lists: Lists, task: Any) -> None:
    """Holds the task until it ends, even once its reading is discarded."""
    with lists.lock:
        lists.tasks.add(task)

    def ended(_: Any = None) -> None:
        with lists.lock:
            lists.tasks.discard(task)

    add = getattr(task, "add_done_callback", None)
    if callable(add):
        add(ended)


async def _collect(reader: Reader) -> Snapshot:
    """Every page of the instance's own list. Anything off fails the read."""
    tools: dict[str, Ownership] = {}
    cursors: set[str] = set()
    own_missing_tools = False
    cursor: str | None = None
    for _ in range(MAX_PAGES):
        listed = await reader.page(cursor)
        # Read so that a throw fails the read: a cursor that can't be read is no last page.
        entries = strict(listed, "tools")
        if not isinstance(entries, list):
            raise ValueError("A page has no list of tools.")
        if len(tools) + len(entries) > MAX_TOOLS:
            raise ValueError("The list has too many tools.")
        for tool in entries:
            name = strict(tool, "name")
            if not isinstance(name, str) or name in tools:
                raise ValueError("A tool has no name, or the name of another.")
            tools[name] = listed_ownership(strict(tool, "inputSchema"))
            if name == MISSING_TOOLS:
                own_missing_tools = True
        following = strict(listed, "nextCursor")
        if following is None or following is _ABSENT:
            return Snapshot(tools, own_missing_tools)
        if not isinstance(following, str) or following in cursors:
            raise ValueError("A cursor came back twice.")
        cursors.add(following)
        cursor = following
    raise ValueError("The list has too many pages.")


_ABSENT = object()


def strict(value: Any, key: str) -> Any:
    """A wire field of a page or a tool, letting a raise through: a dict's key, or a pydantic
    model's field by its name or alias (mcp 2.x's types are snake_case, aliased). A `ServerResult`
    is read through its root."""
    if getattr(type(value), "__pydantic_root_model__", False) is True:
        value = value.root
    if isinstance(value, Mapping):
        return value[key] if key in value else _ABSENT
    fields = getattr(type(value), "model_fields", None)
    if isinstance(fields, dict):
        for name, info in fields.items():
            if name == key or getattr(info, "alias", None) == key:
                return getattr(value, name)
    return _ABSENT


def _asyncio_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _resolve(future: concurrent.futures.Future[None]) -> None:
    try:
        if not future.done():
            future.set_result(None)
    except concurrent.futures.InvalidStateError:
        pass


def _quietly(fn: Callable[[], Any]) -> None:
    try:
        fn()
    except Exception:
        pass
