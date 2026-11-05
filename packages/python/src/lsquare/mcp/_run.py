"""The flush when a stdio server closes (Python spec, section 3, "flush(), shutdown() and exit").

The flush has to happen while the server's loop still runs: once `asyncio.run()` returns, pending
async hooks are cancelled. Only positively identified stdio runs flush: the instance's
`run_stdio_async()` (FastMCP 1.x, MCPServer, and fastmcp, whose `run_async()` calls it for stdio).
HTTP apps end through their lifespan, where REFERENCE.md shows `await lsquare.ashutdown()`.
"""

from __future__ import annotations

import contextvars
import functools
from typing import Any

from ._boundary import TAPPED
from ._call import State
from ._read import attr, loaded

#: The servers whose wrapped stdio run this is inside, by their marks (`State.stdio_mark`), never
#: the servers themselves: a task made during a run keeps this context after it, and would keep
#: the server and its client. Their connections are persistent, and only the outermost run
#: flushes. Another server serving from inside that run (from its lifespan, say) isn't marked by
#: it: its connections are its own, whatever fastmcp's marker says (`in_stdio`).
_STDIO: contextvars.ContextVar[tuple[object, ...]] = contextvars.ContextVar("lsquare_stdio_run", default=())


def in_stdio(state: State) -> bool:
    """Inside this server's own stdio run, as the wrapper knows of it. Only inside no wrapped run
    at all, one fastmcp says is stdio: a wrapped fastmcp run sets fastmcp's own marker too, and
    another server serving from inside that run would inherit it."""
    marked = _STDIO.get()
    if marked:
        mark = state.stdio_mark
        return any(one is mark for one in marked)
    transport = attr(attr(loaded("fastmcp.server.context"), "_current_transport"), "get")
    try:
        return callable(transport) and transport() == "stdio"
    except Exception:
        return False


def wrap_stdio(state: State) -> None:
    high = state.high
    original = attr(high, "run_stdio_async")
    if high is None or not callable(original) or attr(original, TAPPED, False) is True:
        return

    mark = state.stdio_mark

    async def run_stdio_async(*args: Any, **kwargs: Any) -> Any:
        outer = _STDIO.get()
        token = _STDIO.set((*outer, mark))
        try:
            return await original(*args, **kwargs)
        finally:
            try:
                if not outer:
                    await _flush(state)
            finally:
                _STDIO.reset(token)

    try:
        functools.update_wrapper(run_stdio_async, original)
        setattr(run_stdio_async, TAPPED, True)
        setattr(high, "run_stdio_async", run_stdio_async)
    except Exception:
        pass


async def _flush(state: State) -> None:
    """Sends what's queued, within the flush timeout, shielded from the cancellation that may be
    ending the run. Never raises: the run's own outcome is what's passed on."""
    internal = state.internal
    timeout_s = internal.flush_timeout_ms / 1000
    try:
        anyio = loaded("anyio")
        if anyio is None:
            await internal.aflush(internal.flush_timeout_ms)
            return
        with anyio.move_on_after(timeout_s, shield=True):
            await internal.aflush(internal.flush_timeout_ms)
    except Exception:
        pass
