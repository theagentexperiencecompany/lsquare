"""The tap on a high-level tool's own function (Python spec, section 4, "The tool's own result").

Each tool's function is replaced, at its first call, by a wrapper that keeps what it is (a
coroutine function or not, a generator or not) and its signature through `__wrapped__`, so the SDK
calls it as it would the function. Each run gets its own boundary: entering, returning, raising,
and the value returned are recorded on the call's scope, and a `capture()` in the body is inside
the call until that run ends. Generators keep it open until exhausted or closed, as does a
generator the function returns, which the SDK iterates after it.

Only the call's own tool records a run: a function called from inside a run (another tool, run
through the server's `call_tool()`) or from a task or thread a run started, even once it returned,
from a nested `call_tool()` (fastmcp's, see _fastmcp), or on mcp 2.x outside the handler's step (by
a middleware around it, or a task a step left running) isn't the call's, and records nothing on
it.

Adapted, through packages/lsquare/src/mcp/index.ts, from AgentCat's TypeScript SDK (MIT, © 2025
AgentCat, Inc.; see THIRD_PARTY_NOTICES): the tap on each tool's own function.
"""

from __future__ import annotations

import contextvars
import dataclasses
import functools
import inspect
import itertools
import os
import threading
from collections.abc import AsyncGenerator, Callable, Generator, Iterator
from typing import Any

from .._client import CallScope
from . import _weak
from ._read import attr

#: Marks a function already tapped, by any copy of the SDK.
TAPPED = "__lsquare_tapped__"
#: On a function this copy tapped: the clients it was tapped for (`_Clients`).
CLIENTS = "__lsquare_clients__"

Calls = Callable[[], "contextvars.ContextVar[CallScope | None]"]

#: The call whose nested `call_tool()` runs here (fastmcp's, see _fastmcp): the functions it runs
#: aren't the call's tool, and record nothing on it. The call's scope, never its record.
NESTED: contextvars.ContextVar[CallScope | None] = contextvars.ContextVar("lsquare_nested_call", default=None)


class Phase:
    """mcp 2.x: one of a call's handler steps, set by the wrapper's inner middleware around each
    `call_next` it makes (a middleware may call it more than once). It ends with that `call_next`:
    a task made inside the step keeps it, and what that task runs later is outside it. The call's
    scope, never its record."""

    __slots__ = ("scope", "ended")

    def __init__(self, scope: CallScope) -> None:
        self.scope = scope
        self.ended = False


#: mcp 2.x: the handler's step running here (`Phase`). For a call whose chain has the wrapper's
#: inner middleware, a function run outside it (by a middleware around the handler, or by a task
#: the step left running, once it ended) isn't the call's tool.
HANDLING: contextvars.ContextVar[Phase | None] = contextvars.ContextVar("lsquare_handling_call", default=None)

#: Taps one function at a time, and registers its clients: two servers whose clients tap one tool
#: object at once both get it. Reentrant: replacing a tool's `fn` may run its model's validators.
_TAPPING = threading.RLock()


def _reset_after_fork() -> None:
    # A forked child has only the thread that forked: the lock another thread held then (one
    # waiting in a tool's own `fn` setter, say) stays held, with no one to release it. Replaced,
    # never acquired.
    global _TAPPING
    _TAPPING = threading.RLock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)


class _Clients:
    """Each client a function was tapped for, by its call variable: one tool object can sit on two
    servers instrumented with different clients. Held weakly where it can be (a client's own is a
    bound method, see _weak.method), by references with no callback, so a tool shared with the
    servers of clients made and dropped keeps none of them, and nothing runs as one is collected.
    Each client is held under a key of its own, never reused: adding one, and dropping one whose
    client is gone, is one operation on the dict, and a run reads it in one copy, so another
    thread, or a signal handler tapping it for another client meanwhile, loses no client. Added to
    under `_TAPPING`."""

    __slots__ = ("_held",)

    def __init__(self, calls: Calls) -> None:
        self._held: dict[int, Callable[[], Calls | None]] = {}
        self.add(calls)

    def holds(self, calls: Calls) -> bool:
        return any(one() == calls for one in list(self._held.values()))

    def add(self, calls: Calls) -> None:
        """Under `_TAPPING`: adds `calls`, and drops the clients gone since."""
        held = self._held
        for key in list(held):
            one = held.get(key)
            if one is not None and one() is None:
                held.pop(key, None)
        if not self.holds(calls):
            held[next(_CLIENT_KEYS)] = _weak.method(calls)

    def each(self) -> Iterator[Calls]:
        for one in list(self._held.values()):
            calls = one()
            if calls is not None:
                yield calls


#: The keys `_Clients` holds its clients under, across every function: never reused.
_CLIENT_KEYS = itertools.count()


class RunScope(CallScope):
    """One run of the tool's own function, as `capture()` sees it inside: the call's scope, read
    and written through, except its boundary, which closes when this run returns or the call ends.
    (SDK 2.x's legacy multi-round shim runs a function once per round, all in one call.) Only a
    context inside the run, or one copied there (a task or thread the tool started), holds it: a
    function called from either isn't the call's tool, whether the run has returned or not."""

    def __init__(self, call: CallScope) -> None:  # noqa: D107 - the dataclass's fields are the call's
        object.__setattr__(self, "_call", call)
        object.__setattr__(self, "_ended", False)


def _through(name: str) -> property:
    def get(self: RunScope) -> Any:
        return getattr(object.__getattribute__(self, "_call"), name)

    def put(self: RunScope, value: Any) -> None:
        setattr(object.__getattribute__(self, "_call"), name, value)

    return property(get, put)


def _returned_get(self: RunScope) -> bool:
    return bool(object.__getattribute__(self, "_ended") or object.__getattribute__(self, "_call").returned)


def _returned_put(self: RunScope, value: bool) -> None:
    if value:
        object.__setattr__(self, "_ended", True)
        # The tool returned: outside a run, a capture() from now on is a turn of its own.
        try:
            object.__getattribute__(self, "_call").ran = True
        except Exception:
            pass


def _ran_get(self: RunScope) -> bool:
    # A run's line is its own return: inside a later run (a round), a capture() is inside again.
    return False


def _ran_put(self: RunScope, value: bool) -> None:
    object.__getattribute__(self, "_call").ran = value


for _field in dataclasses.fields(CallScope):
    if _field.name not in ("returned", "ran"):
        setattr(RunScope, _field.name, _through(_field.name))
setattr(RunScope, "returned", property(_returned_get, _returned_put))  # noqa: B010
setattr(RunScope, "ran", property(_ran_get, _ran_put))  # noqa: B010


def tap(tool: Any, calls: Calls, finishes: bool = False) -> None:
    """Wraps the tool's `fn` in place, once: one this copy tapped for another client reads this
    client's calls too. A tool whose function can't be replaced is left as it is: a raise then
    reads as the tool's own error. `finishes`: the SDK awaits what a sync function returns, and
    iterates an async generator a function returns (fastmcp)."""
    try:
        fn = attr(tool, "fn")
        clients = attr(fn, CLIENTS)
        if isinstance(clients, _Clients) and clients.holds(calls):
            return
        # Read again under the lock: another thread may have tapped it, or added its client, since.
        with _TAPPING:
            fn = attr(tool, "fn")
            if not callable(fn):
                return
            if attr(fn, TAPPED, False) is True:
                # Tapped by this copy for another client: it reads this client's calls too.
                # Another copy's tap is left as it is.
                clients = attr(fn, CLIENTS)
                if isinstance(clients, _Clients):
                    clients.add(calls)
                return
            setattr(tool, "fn", tapped(fn, calls, finishes))
    except Exception:
        pass


def watches(tool: Any, calls: Calls) -> bool:
    """The tool's `fn` is tapped by this copy of the SDK, for this client."""
    try:
        clients = attr(attr(tool, "fn"), CLIENTS)
        return isinstance(clients, _Clients) and clients.holds(calls)
    except Exception:
        return False


def is_async(fn: Any) -> bool:
    """As the MCP SDKs tell a coroutine function: through `functools.partial`, and a callable
    object's `__call__`."""
    try:
        while isinstance(fn, functools.partial):
            fn = fn.func
        return inspect.iscoroutinefunction(fn) or (callable(fn) and inspect.iscoroutinefunction(attr(fn, "__call__")))
    except Exception:
        return False


def tapped(fn: Callable[..., Any], calls: Calls, finishes: bool = False) -> Callable[..., Any]:
    """The tool's own function, watched. Called exactly once per call to the wrapper: inside its
    run's scope, or, if entering it fails, as it is. `finishes`: as for `tap()`."""
    clients = _Clients(calls)
    if inspect.isasyncgenfunction(fn):
        wrapper: Callable[..., Any] = _async_generator(fn, _starts(clients))
    elif inspect.isgeneratorfunction(fn):
        wrapper = _generator(fn, _starts(clients))
    elif is_async(fn):
        wrapper = _coroutine(fn, clients, finishes)
    else:
        wrapper = _function(fn, clients, finishes)
    try:
        functools.update_wrapper(wrapper, fn)
    except Exception:
        pass
    setattr(wrapper, CLIENTS, clients)
    setattr(wrapper, TAPPED, True)
    return wrapper


class _Run:
    """One run's boundary on the call's scope."""

    __slots__ = ("scope", "run", "var")

    def __init__(self, scope: CallScope, run: RunScope, var: contextvars.ContextVar[CallScope | None]) -> None:
        self.scope = scope
        self.run = run
        self.var = var

    def entered(self) -> None:
        try:
            on_entry = self.scope.on_tool_entry
            if on_entry is not None:
                on_entry()
        except Exception:
            # identify then waits for the call's end.
            pass

    def done(self, value: Any) -> None:
        try:
            scope = self.scope
            # The last run's own outcome is the call's: what an earlier run recorded (one a retry
            # ran again) isn't this run's.
            scope.thrown = None
            scope.tool_result = (value,)
            # Read after the write, so a let_go() on another thread in between is seen: a run that
            # ends once the call let go (from a task the tool left running) keeps nothing here.
            if scope.released:
                scope.tool_result = None
            self.run.returned = True
        except Exception:
            pass

    def failed(self, error: BaseException) -> None:
        try:
            scope = self.scope
            scope.tool_result = None
            scope.thrown = (error,)
            if scope.released:
                scope.thrown = None
            self.run.returned = True
        except Exception:
            pass

    def closed(self) -> None:
        """A generator closed before it finished: its boundary closes, with no value."""
        try:
            self.run.returned = True
        except Exception:
            pass

    def enter(self) -> contextvars.Token[CallScope | None] | None:
        try:
            return self.var.set(self.run)
        except Exception:
            return None

    def leave(self, token: contextvars.Token[CallScope | None] | None) -> None:
        if token is not None:
            try:
                self.var.reset(token)
            except Exception:
                pass


def _call_of(scope: CallScope) -> CallScope:
    """The call's own scope, for a run's."""
    if isinstance(scope, RunScope):
        call: CallScope = object.__getattribute__(scope, "_call")
        return call
    return scope


def outside_handler(call: CallScope) -> bool:
    """mcp 2.x: the call's chain has the wrapper's inner middleware, and none of its handler's steps
    is running here: a middleware around it is, or a task a step left running, which ended (see
    `HANDLING`)."""
    if attr(call.wrapper, "gated", False) is not True:
        return False
    phase = HANDLING.get()
    return phase is None or phase.scope is not call or phase.ended


def _begin(clients: _Clients) -> _Run | None:
    """A run on the call in a client's variable, its evidence the run's own from here. None outside
    a call, and for a function that isn't the call's tool: called from inside a run of a tool's own
    function or from a task or thread that run started (whether it has returned or not), by a
    nested `call_tool()`, or outside the handler's step (an app's `call_tool()`, from mcp 2.x
    middleware), none of which makes an event or is the call's."""
    try:
        each = list(clients.each())
    except Exception:
        return None
    for calls in each:
        try:
            var = calls()
            scope = var.get()
            if scope is None or isinstance(scope, RunScope):
                continue
            call = _call_of(scope)
            if NESTED.get() is call or outside_handler(call):
                continue
            if not call.released:
                call.thrown = None
                call.tool_result = None
            call.invoked = True
            return _Run(call, RunScope(call), var)
        except Exception:
            continue
    return None


def _starts(clients: _Clients) -> Callable[[], _Run | None]:
    def start() -> _Run | None:
        run = _begin(clients)
        if run is not None:
            run.entered()
        return run

    return start


def _finish(run: _Run, value: Any, finishes: bool) -> Any:
    """What a run returned, as the SDK gets it: a generator carries the run on until it's
    exhausted or closed, as every line iterates one after the function returned; an async
    generator too where the SDK iterates it (fastmcp). Any other value ends the run."""
    try:
        if inspect.isgenerator(value):
            return _carried(_generator(lambda: value, lambda: run)(), value)
        if finishes and inspect.isasyncgen(value):
            return _carried(_async_generator(lambda: value, lambda: run)(), value)
    except Exception:
        pass
    run.done(value)
    return value


def _carried(carrier: Any, value: Any) -> Any:
    """The generator that carries the run, named as the one it passes on."""
    for name in ("__name__", "__qualname__"):
        try:
            setattr(carrier, name, getattr(value, name))
        except Exception:
            pass
    return carrier


def _function(fn: Callable[..., Any], clients: _Clients, finishes: bool) -> Callable[..., Any]:
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        run = _begin(clients)
        if run is None:
            return fn(*args, **kwargs)
        run.entered()
        token = run.enter()
        try:
            value = fn(*args, **kwargs)
        except BaseException as error:
            run.failed(error)
            raise
        finally:
            run.leave(token)
        if finishes and inspect.isawaitable(value):
            # A sync function returning an awaitable, which fastmcp awaits: the run lasts until
            # that settles. mcp 1.x and 2.x convert it as it is: it is the value.
            return _follow(run, value)
        return _finish(run, value, finishes)

    return wrapper


async def _follow(run: _Run, awaitable: Any) -> Any:
    token = run.enter()
    try:
        value = await awaitable
    except BaseException as error:
        run.failed(error)
        raise
    finally:
        run.leave(token)
    return _finish(run, value, True)


def _coroutine(fn: Callable[..., Any], clients: _Clients, finishes: bool) -> Callable[..., Any]:
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        run = _begin(clients)
        if run is None:
            return await fn(*args, **kwargs)
        run.entered()
        token = run.enter()
        try:
            value = await fn(*args, **kwargs)
        except BaseException as error:
            run.failed(error)
            raise
        finally:
            run.leave(token)
        return _finish(run, value, finishes)

    return wrapper


def _generator(fn: Callable[..., Any], start: Callable[[], _Run | None]) -> Callable[..., Any]:
    """A generator function's wrapper, whose run starts with its first step (`start`), or a
    generator a run returned, passed on through that run."""

    def wrapper(*args: Any, **kwargs: Any) -> Generator[Any, Any, Any]:
        run = start()

        def step(method: Callable[..., Any], *arg: Any) -> Any:
            token = run.enter() if run is not None else None
            try:
                return method(*arg)
            finally:
                if run is not None:
                    run.leave(token)

        closing: list[Any] = []
        try:
            # Making the generator runs none of its body.
            generator = fn(*args, **kwargs)
            value = yield from _delegate(generator, step, closing)
        except GeneratorExit:
            if run is not None:
                run.closed()
            if closing and closing[0] is not None:
                # What the generator returned as it closed, which close() returns (Python 3.13+).
                return closing[0]
            raise
        except BaseException as error:
            if run is not None:
                run.failed(error)
            raise
        if run is not None:
            run.done(value)
        return value

    return wrapper


def _delegate(generator: Generator[Any, Any, Any], step: Callable[..., Any], closing: list[Any]) -> Generator[Any, Any, Any]:
    """`yield from generator`, each of its steps run through `step`: `send`, `throw` and `close`
    passed on, as PEP 380 says. What `close()` returned goes in `closing`, which `yield from`
    would drop."""
    try:
        item = step(generator.send, None)
    except StopIteration as stop:
        return stop.value
    while True:
        try:
            sent = yield item
        except GeneratorExit:
            closing.append(step(generator.close))
            raise
        except BaseException as error:
            try:
                item = step(generator.throw, error)
            except StopIteration as stop:
                return stop.value
        else:
            try:
                item = step(generator.send, sent)
            except StopIteration as stop:
                return stop.value


def _async_generator(fn: Callable[..., Any], start: Callable[[], _Run | None]) -> Callable[..., Any]:
    """As `_generator`, for async generators."""

    async def wrapper(*args: Any, **kwargs: Any) -> AsyncGenerator[Any, Any]:
        run = start()

        async def step(method: Callable[..., Any], *arg: Any) -> Any:
            token = run.enter() if run is not None else None
            try:
                return await method(*arg)
            finally:
                if run is not None:
                    run.leave(token)

        generator = fn(*args, **kwargs)
        try:
            try:
                item = await step(generator.asend, None)
            except StopAsyncIteration:
                if run is not None:
                    run.done(None)
                return
            while True:
                try:
                    sent = yield item
                except GeneratorExit:
                    await step(generator.aclose)
                    raise
                except BaseException as error:
                    try:
                        item = await step(generator.athrow, error)
                    except StopAsyncIteration:
                        break
                else:
                    try:
                        item = await step(generator.asend, sent)
                    except StopAsyncIteration:
                        break
        except GeneratorExit:
            if run is not None:
                run.closed()
            raise
        except BaseException as error:
            if run is not None:
                run.failed(error)
            raise
        if run is not None:
            run.done(None)

    return wrapper
