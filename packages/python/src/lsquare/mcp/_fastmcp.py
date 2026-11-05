"""fastmcp 3.x and 4.x, over either line (Python spec, section 4, "The tool's own result").

fastmcp's middleware runs inside the protocol dispatch, on `ToolResult`s: it can let the tool run
and return another result. So the instance's `call_tool()` is wrapped: inside a call it records
what the core step (`run_middleware=False`: lookup and run) returned or raised, as it returned, and
the ID line needs the result the middleware chain returns (`run_middleware=True`) to be that
object, unchanged. Outside a call, as when the app calls `call_tool()` from Python, it passes
straight through.

Inside one, only the call's own steps are recorded: its server's first chain and each core step
that chain runs (a retry runs it again). A core step is the `call_tool()` made by the `call_next`
fastmcp composed at the chain's end, which the wrapper marks: on fastmcp 4.x, the one it composes
its extensions' `tools/call` interceptors around (the instance's `_compose_tool_call_interceptors()`
is wrapped), and otherwise the one its `_run_middleware()` runs the chain's middleware around
(wrapped too). A middleware's or an interceptor's own `call_tool(run_middleware=False)` isn't one.
A mounted server's `call_tool()`, which the core step makes to run the call's tool, is a chain of
that server's, whose own core step runs the tool, and that tool's function records on the call as
the call's own. Any other `call_tool()`, of any server, is a nested call: from fastmcp middleware
or an interceptor (a mounted server's included), from inside a tool's function or a task or thread
it started, from a task a step started once that step has ended, or on fastmcp 4.x from mcp 2.x
middleware outside the handler's step. It records nothing, and the functions it runs record
nothing on the call (see also `_boundary._begin`).

The end of the core step that runs the tool is the tool's return, as a watched function's return
is (`_boundary.RunScope`): a `capture()` after it, from middleware, is a turn of its own, also for
a tool the wrapper can't watch (a mounted server's).
"""

from __future__ import annotations

import contextvars
import functools
import weakref
from dataclasses import dataclass
from typing import Any

from .._client import CallScope
from ._boundary import NESTED, TAPPED, RunScope, outside_handler
from ._call import Running, State, first_text, snapshot, unchanged
from ._read import attr, in_mro, wire

#: The call's server's own chain and core steps; a mounted server's chain and core steps, made from
#: a core step to run the call's tool there; anything else, nested.
CHAIN, CORE, WITHIN, WITHIN_CORE, OTHER = "chain", "core", "within", "within_core", "nested"


class _Step:
    """One `call_tool()` running inside a call: which of the call's steps it is, on which server.
    It holds the call's scope, not its record, and the server weakly: a task the tool leaves
    running keeps this context, and the scope keeps only the call's ID and boundary once the call
    lets go (`let_go`). Its authority ends with it (`ended`): a task made inside it that calls a
    tool later, while the call goes on, is nested."""

    __slots__ = ("scope", "kind", "_server", "outer", "dispatched", "composed", "ended")

    def __init__(self, scope: CallScope, kind: str, server: State, outer: _Step | None) -> None:
        self.scope = scope
        self.kind = kind
        self._server = weakref.ref(server)
        #: The step this one runs inside, on the same call.
        self.outer = outer
        #: A chain step: fastmcp marked the `call_next` that makes its core step, through the wrapped
        #: `_compose_tool_call_interceptors()` or `_run_middleware()`, so its core step is only the
        #: one that marked `call_next` makes (`_CORE_NEXT`).
        self.dispatched = False
        #: fastmcp 4.x: the mark is on the `call_next` its extensions' interceptors are composed
        #: around, inside them, and not on the whole chain `_run_middleware()` gets.
        self.composed = False
        #: This `call_tool()` has returned or raised.
        self.ended = False

    def on(self, state: State) -> bool:
        """This step, or one it runs inside, is a `call_tool()` of `state`'s server."""
        step: _Step | None = self
        while step is not None:
            if step._server() is state:
                return True
            step = step.outer
        return False

    def of(self, state: State) -> bool:
        return self._server() is state


#: The `call_tool()` running here inside a call (`_Step`).
_STEP: contextvars.ContextVar[_Step | None] = contextvars.ContextVar("lsquare_fastmcp_step", default=None)
#: The chain step whose `call_next`, the one fastmcp composed at the end of its middleware (and,
#: on fastmcp 4.x, of its extensions' interceptors), runs here: the `call_tool(run_middleware=False)`
#: it makes is that chain's core step.
_CORE_NEXT: contextvars.ContextVar[_Step | None] = contextvars.ContextVar("lsquare_fastmcp_core_next", default=None)


@dataclass(frozen=True)
class Core:
    """What one core step of the call did, read as it returned or raised: never read again from its
    result, which middleware may change in place afterwards. `refused`: fastmcp refused the call
    before any tool ran; `raised`: anything else it raised; `returned`: a result."""

    kind: str
    is_error: bool = False
    #: The result's content, as `_call._same_content` compares it: how many blocks, the first text.
    length: int | None = None
    first: str | None = None

    def kept(self, content: Any) -> bool:
        """The call's final result is this core step's `isError` result, its content as it was."""
        try:
            return (
                self.kind == "returned"
                and self.is_error
                and isinstance(content, list)
                and self.length == len(content)
                and self.first == first_text(content)
            )
        except Exception:
            return False


def returned_core(result: Any) -> Core:
    content = wire(result, "content")
    return Core("returned", wire(result, "isError") is True, len(content) if isinstance(content, list) else None, first_text(content))


def _step(state: State, running: Running, here: CallScope, middleware: bool) -> tuple[str, _Step | None]:
    """Which step of `running` a `call_tool()` on `state`'s server is, and the step it runs
    inside. `here`: the scope in the client's variable, a run's inside a tool's function and in
    the tasks and threads that run started."""
    scope = running.scope
    if NESTED.get() is scope or outside_handler(scope) or isinstance(here, RunScope):
        return OTHER, None
    outer = _STEP.get()
    if outer is not None and outer.scope is scope and outer.ended:
        # A context a step left behind (a task made inside it): that step is over, and nothing run
        # from here is one of the call's.
        return OTHER, None
    current = outer if outer is not None and outer.scope is scope else None
    if current is None:
        # The call's first: its server's chain, as its handler calls it.
        if running.owner is not state:
            return OTHER, None
        return (CHAIN if middleware else CORE), None
    if current.kind in (CHAIN, WITHIN):
        # Inside a chain, only the call_next fastmcp composed at its end runs that server's core
        # step. A middleware's own call_tool(), of any server, is nested.
        if middleware or not current.of(state):
            return OTHER, current
        if current.dispatched and _CORE_NEXT.get() is not current:
            return OTHER, current
        return (CORE if current.kind == CHAIN else WITHIN_CORE), current
    if current.kind in (CORE, WITHIN_CORE) and not current.on(state):
        # From a core step, another server's: the call's tool, where a provider runs it there (a
        # mounted server's), through that server's own chain.
        return (WITHIN if middleware else WITHIN_CORE), current
    return OTHER, current


def refused(error: BaseException) -> bool:
    """What fastmcp raises when it refuses a call before any tool runs: an unknown, disabled or
    unauthorized tool, or arguments that fail its check. A tool's own body raising one of these
    can't be told apart, when its function isn't watched."""
    return (
        in_mro(error, "fastmcp.exceptions", "NotFoundError")
        or in_mro(error, "fastmcp.exceptions", "DisabledError")
        or in_mro(error, "fastmcp.exceptions", "ValidationError")
        or in_mro(error, "fastmcp.exceptions", "AuthorizationError")
        or in_mro(error, "pydantic_core._pydantic_core", "ValidationError")
    )


def _attempt(running: Running, step: str) -> None:
    """A core step starts: what an earlier one did, and what the functions it ran recorded, isn't
    this one's, and the tool hasn't returned (a retry runs it again)."""
    if step == CORE:
        running.core = None
        running.core_result = running.core_snapshot = None
    scope = running.scope
    if not scope.released:
        scope.invoked = False
        scope.thrown = None
        scope.tool_result = None
        scope.ran = False


def _ended(running: Running) -> None:
    """A core step ended, returning or raising: the tool's run, whoever's tool it is, has too."""
    scope = running.scope
    if not scope.released:
        scope.ran = True


def _wrap_compose(state: State) -> None:
    """fastmcp 4.x: wraps the instance's `_compose_tool_call_interceptors()`, which nests its
    extensions' `tools/call` interceptors around the `call_next` that makes the core step: inside
    one of the call's chain steps on this server, that `call_next` is marked while it runs
    (`_CORE_NEXT`), under every interceptor. A `call_tool()` an interceptor makes, before or after
    its own continuation, isn't the core step."""
    high = state.high
    original = attr(high, "_compose_tool_call_interceptors")
    if not callable(original) or attr(original, TAPPED, False) is True:
        return

    def _compose_tool_call_interceptors(*args: Any, **kwargs: Any) -> Any:
        try:
            step = _STEP.get()
            key = "call_next" if "call_next" in kwargs else (0 if args else None)
            if step is not None and step.kind in (CHAIN, WITHIN) and step.of(state) and not step.ended and key is not None:
                call_next = kwargs[key] if isinstance(key, str) else args[key]
                marked = _marked(call_next, step)
                if isinstance(key, str):
                    kwargs[key] = marked
                else:
                    args = (marked, *args[1:])
                step.dispatched = step.composed = True
        except Exception:
            pass
        return original(*args, **kwargs)

    try:
        functools.update_wrapper(_compose_tool_call_interceptors, original)
        setattr(_compose_tool_call_interceptors, TAPPED, True)
        setattr(high, "_compose_tool_call_interceptors", _compose_tool_call_interceptors)
    except Exception:
        pass


def _wrap_dispatch(state: State) -> None:
    """Wraps the instance's `_run_middleware()`, by which fastmcp runs a chain's middleware around
    the `call_next` it composed: inside one of the call's chain steps on this server, that
    `call_next` is marked while it runs (`_CORE_NEXT`), unless the mark is already on the one the
    extensions' interceptors are composed around (fastmcp 4.x, `_wrap_compose`)."""
    high = state.high
    original = attr(high, "_run_middleware")
    if not callable(original) or attr(original, TAPPED, False) is True:
        return

    async def _run_middleware(*args: Any, **kwargs: Any) -> Any:
        try:
            step = _STEP.get()
            context = kwargs["context"] if "context" in kwargs else (args[0] if args else None)
            key = "call_next" if "call_next" in kwargs else (1 if len(args) > 1 else None)
            if (
                step is not None
                and step.kind in (CHAIN, WITHIN)
                and step.of(state)
                and not step.composed
                and key is not None
                and attr(context, "method") == "tools/call"
            ):
                call_next = kwargs[key] if isinstance(key, str) else args[key]
                marked = _marked(call_next, step)
                if isinstance(key, str):
                    kwargs[key] = marked
                else:
                    args = (*args[:key], marked, *args[key + 1 :])
                step.dispatched = True
        except Exception:
            pass
        return await original(*args, **kwargs)

    try:
        functools.update_wrapper(_run_middleware, original)
        setattr(_run_middleware, TAPPED, True)
        setattr(high, "_run_middleware", _run_middleware)
    except Exception:
        pass


def _marked(call_next: Any, step: _Step) -> Any:
    async def core_next(*args: Any, **kwargs: Any) -> Any:
        token = _CORE_NEXT.set(step)
        try:
            return await call_next(*args, **kwargs)
        finally:
            _CORE_NEXT.reset(token)

    return core_next


def wrap_call_tool(state: State) -> None:
    high = state.high
    original = attr(high, "call_tool")
    if not callable(original) or attr(original, TAPPED, False) is True:
        return
    internal = state.internal

    async def call_tool(*args: Any, **kwargs: Any) -> Any:
        running: Running | None = None
        step = OTHER
        here: _Step | None = None
        tokens: tuple[Any, Any] | None = None
        try:
            scope = internal.calls().get()
            wrapper = scope.wrapper if scope is not None else None
            if scope is not None and isinstance(wrapper, Running):
                running = wrapper
                step, outer = _step(state, running, scope, kwargs.get("run_middleware", True) is not False)
                here = _Step(running.scope, step, state, outer)
                tokens = (_STEP.set(here), NESTED.set(running.scope) if step == OTHER else None)
                if step in (CORE, WITHIN_CORE):
                    _attempt(running, step)
        except Exception:
            running = None
        try:
            result = await original(*args, **kwargs)
        except BaseException as error:
            if running is not None and step in (CORE, WITHIN_CORE):
                try:
                    if step == CORE:
                        running.core = Core("refused" if refused(error) else "raised")
                    _ended(running)
                except Exception:
                    pass
            raise
        finally:
            if here is not None:
                # A task made inside this step keeps it, ended: what that task calls later is nested.
                here.ended = True
            if tokens is not None:
                try:
                    if tokens[1] is not None:
                        NESTED.reset(tokens[1])
                    _STEP.reset(tokens[0])
                except Exception:
                    pass
        try:
            if running is not None:
                if step == CORE:
                    running.core = returned_core(result)
                    running.core_result = result
                    running.core_snapshot = snapshot(result)
                elif step == CHAIN:
                    running.chain_result = result
                if step in (CORE, WITHIN_CORE):
                    _ended(running)
        except Exception:
            pass
        return result

    try:
        functools.update_wrapper(call_tool, original)
        setattr(call_tool, TAPPED, True)
        setattr(high, "call_tool", call_tool)
        state.core_watched = True
    except Exception:
        return
    _wrap_compose(state)
    _wrap_dispatch(state)


def provenance(running: Running) -> bool:
    """The result fastmcp's middleware chain returned is the core step's, unchanged."""
    core = running.core_result
    return core is not None and running.chain_result is core and unchanged(running.core_snapshot, core)
