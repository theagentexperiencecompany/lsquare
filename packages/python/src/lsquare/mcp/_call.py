"""One `tools/call`, whatever the SDK line (Python spec, section 4), ported from `onCall()` and what
it uses in packages/lsquare/src/mcp/index.ts.

The tool runs unchanged, inside the call's scope, and its reply or error goes back as it came. The
event is built when the call ends, without awaiting anything, or, while `identify` still runs,
later on the call's own loop. If anything in recording fails, the call goes on untouched; the
app's handler is only ever called once. What differs by line (how a request is copied, where the
client and session are read, what a result looks like) is the `Dispatch` each line hands in.

Adapted, through index.ts, from AgentCat's TypeScript SDK (MIT, © 2025 AgentCat, Inc.; see
THIRD_PARTY_NOTICES): reading the client's info, detecting multi-round calls, and taking the
extras' inputs out of a call.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import json
import math
import os
import re
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
from typing import Any, Protocol

from .._client import DELIVERED_GRACE_S, CallScope, HookRun, Internal, LSquareError, Origin, Signal
from .._normalize import app_ids
from .._threads import interrupts
from .._values import as_object
from ._extras import (
    INTENT,
    MISSING_TOOLS,
    SESSION_FORMAT,
    SESSION_ID,
    Ownership,
    advertise,
    listed_ownership,
    same_ownership,
    id_block,
    missing_tools_reply,
    missing_tools_tool,
    new_session_id,
)
from ._boundary import tap, watches
from ._lists import Lists, Reader, Snapshot, Wait, fresh, mark, wait_once, with_snapshot
from ._read import attr, cancelled, close, item, scope_cancelled, wire
from ._registry import Read, Registry
from ._weak import ByIdentity

#: How long a call's event waits for `identify` after the tool returns.
IDENTIFY_WAIT_S = 1.0
#: Past identify's deadline, how long the call's loop gets to run the build handed to it before a
#: client thread builds the event instead (a loop stopped or blocked): scheduling time for the build,
#: never more time for identify, whose deadline stays where `returned()` fixed it.
LOOP_GRACE_S = 0.1
#: How long `wait_until` keeps the request alive for a call's event, at most.
WAIT_UNTIL_S = 30.0
#: The first protocol version without protocol sessions.
SESSIONLESS_PROTOCOL = "2026-07-28"
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z", re.ASCII)


@dataclass(frozen=True)
class Extras:
    sessions: bool = False
    intent: bool = False
    missing_tools: bool = False

    def any(self) -> bool:
        return self.sessions or self.intent or self.missing_tools


NO_EXTRAS = Extras()


@dataclass(frozen=True)
class IdentifyInput:
    """What `identify` gets: the tool's name, its arguments as the client sent them (without the
    extras' inputs), the SDK's request context, and a signal aborted at the hook's deadline."""

    tool_name: str
    args: Any
    context: Any
    signal: Signal


@dataclass
class Settings:
    identify: Callable[..., Any] | None
    inputs: Callable[[str], bool]
    results: Callable[[str], bool]
    wait_until: Callable[[Any], Any] | None
    flush_before_reply: bool
    #: Each extra on unless passed as False: a high-level server's defaults.
    extras: Extras
    #: Each extra passed as True: a low-level server's.
    explicit: Extras


@dataclass(eq=False)
class State:
    """What an instrumented server keeps, on its low-level server: the wrapper has no state of its own."""

    internal: Internal
    settings: Settings
    #: "v1" (mcp 1.x, fastmcp 3.x) or "v2" (mcp 2.x, fastmcp 4.x).
    line: str
    low: Any
    high: Any
    registry: Registry | None
    #: The app replaced the high-level class's tools/call handler: read as a low-level server.
    replaced: bool = False
    #: The extras while the high-level class's own tools/call handler is in place: none on a low-level server.
    high_extras: Extras = NO_EXTRAS
    #: The extras passed as True: what a low-level server will get (step 3).
    low_extras: Extras = NO_EXTRAS
    #: mcp 2.x: the tools/call entry found at instrument(), and the inner middleware.
    call_entry: Any = None
    inner: Any = None
    #: mcp 2.x: the tools/list entry the snapshot is read from, to notice it replaced.
    list_entry: Any = None
    #: A low-level server's snapshot of its own tool list.
    lists: Lists = dataclass_field(default_factory=Lists)
    #: Sessions made for connections that have none of their own (stdio), by connection. Each
    #: table is by identity, through references with no callback (see _weak): nothing runs as a
    #: connection or session is collected, and none of its own code (`__eq__`, `__hash__`) runs.
    made_sessions: ByIdentity[str] = dataclass_field(default_factory=ByIdentity)
    #: The protocol session each HTTP session or connection that ran `initialize` was given at its
    #: first call, kept for as long as it lasts: a later request on it can't change it.
    http_sessions: ByIdentity[str] = dataclass_field(default_factory=ByIdentity)
    #: fastmcp: this copy wrapped the instance's `call_tool()`, so each call knows what its core
    #: steps did (`Running.core`).
    core_watched: bool = False
    #: mcp 1.x: the request contexts whose calls the wrapper is running, by id(): a handler set
    #: later that calls the wrapped one (middleware) enters the wrapper again for the same request,
    #: and that call is the outer one's, recorded once there (see _v1._nested()).
    dispatching: dict[int, Any] = dataclass_field(default_factory=dict)
    lock: threading.Lock = dataclass_field(default_factory=threading.Lock)
    #: Marks this server's stdio runs in the contexts inside them (`_run._STDIO`): an object of no
    #: use but its identity, so a context a run leaves behind (a task's) keeps nothing of the server.
    stdio_mark: object = dataclass_field(default_factory=object)


def tap_tools(state: State, tool_name: str, read: Read | None = None) -> bool:
    """Taps the functions a call of `tool_name` may run: every local registration of it. `read`:
    the call's read of the registry, from the same step; without it, the registry is read now.
    True when the call is watched: every function it can run is tapped for this client, or a tool
    set that's known has no tool of that name (the SDK refuses the call), so an `isError` result
    whose function wasn't entered is the SDK's. A tool from another fastmcp provider (a mounted
    server, a proxy, OpenAPI, an injected tool), or one called by its hashed name, isn't watched:
    its function can't be tapped, and may have run unseen."""
    registry = state.registry
    if registry is None:
        return False
    source = read if read is not None else registry.read()
    fastmcp = source.kind == "fastmcp"
    local = source.local(tool_name)
    calls = state.internal.calls
    for tool in local:
        tap(tool, calls, fastmcp)
    if not local:
        return source.tools(tool_name) == [] and not (fastmcp and hashed_name(tool_name))
    return all(watches(tool, calls) for tool in local)


def hashed_name(name: str) -> bool:
    """fastmcp's `<hash>_<local name>` form, which it resolves through its providers when no tool
    has that name: 12 lowercase hex digits and an underscore, then the name."""
    return len(name) > 13 and name[12] == "_" and all(c in "0123456789abcdef" for c in name[:12])


def call_read(state: State) -> Read | None:
    """One read of the registry for a call's step, its extras' inputs taken out and its functions
    tapped. None without the high-level handler in place, or when it can't be read: each then
    reads it itself."""
    registry = state.registry
    if registry is None or not high_mode(state):
        return None
    try:
        return registry.read()
    except Exception:
        return None


def high_mode(state: State) -> bool:
    """The high-level class's own tools/call handler is in place, so the tools' definitions decide."""
    return state.high is not None and not state.replaced


def high_on(state: State) -> bool:
    return high_mode(state) and state.high_extras.any()


# -- Options ----------------------------------------------------------------------------------------


def read_options(
    internal: Internal,
    identify: Any,
    inputs: Any,
    results: Any,
    extras: Any,
    wait_until: Any,
    flush_before_reply: Any,
) -> Settings:
    # A function that raises, or answers with an awaitable (which can't be waited for), records
    # nothing for that call: the field stays out of its event, and one warning per option says so.
    def choice(key: str, value: Any) -> Callable[[str], bool]:
        def decide(tool_name: str) -> bool:
            if not callable(value):
                return value is not False
            try:
                answer = value(tool_name)
                if not inspect.isawaitable(answer):
                    return answer is not False
                close(answer)
            except Exception:
                pass
            internal.warn_once(f"{key}-unanswered", f"[lsquare] The {key} function raised or returned an awaitable: {key} aren't recorded for the calls it answered that way.")
            return False

        return decide

    flush = flush_before_reply is True
    if callable(wait_until) and flush:
        internal.warn_once("flush-ignored", "[lsquare] Both wait_until and flush_before_reply are set: wait_until is used, and flush_before_reply ignored.")

    # Each extra left out keeps its level's default: on for a high-level server, off for a low-level
    # one. False turns them all off. Any other value that isn't a mapping is ignored, with a warning.
    off = extras is False
    if not off and extras is not None and not isinstance(extras, Mapping):
        internal.warn_once("extras-ignored", "[lsquare] extras is neither False nor a mapping: it is ignored, and each extra keeps its default.")

    def extra(key: str) -> bool:
        return not off and item(extras, key) is not False

    def explicit(key: str) -> bool:
        return not off and item(extras, key) is True

    return Settings(
        identify=identify if callable(identify) else None,
        inputs=choice("inputs", inputs),
        results=choice("results", results),
        wait_until=wait_until if callable(wait_until) else None,
        flush_before_reply=flush and not callable(wait_until),
        extras=Extras(extra("sessions"), extra("intent"), extra("missing_tools")),
        explicit=Extras(explicit("sessions"), explicit("intent"), explicit("missing_tools")),
    )


def serverless_host() -> str | None:
    """Hosts known to stop work after the reply, with TypeScript's wording."""
    try:
        env = os.environ
        if env.get("AWS_LAMBDA_FUNCTION_NAME"):
            return "AWS Lambda"
        if env.get("K_SERVICE"):
            return "Cloud Run (unless its CPU is always allocated)"
        if env.get("VERCEL"):
            return "Vercel"
    except Exception:
        pass
    return None


def warn_serverless(settings: Settings, internal: Internal) -> None:
    if settings.wait_until is not None or settings.flush_before_reply:
        return
    host = serverless_host()
    if host is None:
        return
    internal.warn_once(
        "serverless",
        f"[lsquare] This looks like {host}, which can stop work once a reply is sent: pass wait_until, or flush_before_reply=True, to instrument(), or events may be lost.",
    )


# -- What the line hands in -------------------------------------------------------------------------


class Dispatch(Protocol):
    """One `tools/call` as a line sees it. A line may also have, read by name when present:
    `gated` (mcp 2.x: the call's chain has the wrapper's inner middleware, which marks the
    handler's step), `refusal(result)` (the exception the SDK sends a result that is a JSON-RPC
    error as) and `cancel_requested()` (the client cancelled the request)."""

    #: The request as it came: passed on as it is unless the extras take something out.
    request: Any
    tool_name: Any
    arguments: Any
    #: The SDK's request context, given to `identify`.
    context: Any

    def with_arguments(self, arguments: dict[str, Any]) -> Any:
        """A copy of the request with these arguments; the request itself is never changed."""

    async def call(self, request: Any) -> Any:
        """Calls the app's handler, or what comes next, with `request`."""

    async def answer(self, request: Any, reply: dict[str, Any]) -> Any:
        """Answers get_more_tools with `reply`, in the line's own form, as the SDK would send it."""

    def client(self) -> Any: ...

    def version(self) -> Any: ...

    def session(self, version: Any) -> str | None:
        """The session the transport established, for a version that has them."""

    def continued(self) -> bool: ...

    def read(self, result: Any, key: str) -> Any:
        """A wire field of the result."""

    def with_block(self, result: Any, text: str) -> Any:
        """A copy of the result with one more text block."""

    def recorded(self, result: Any) -> Any:
        """The result as the event records it."""

    def provenance(self, running: Running, result: Any) -> bool:
        """Every layer that could replace the result passed the SDK's own through unchanged."""

    def refresh(self) -> None:
        """Notices a tools/call handler replaced since the call arrived."""

    def reader(self) -> Reader | None:
        """How a snapshot read started by this call gets the list: the instance's own tools/list
        handler, with a copy of this request's context marked as a listing."""

    def jsonrpc_code(self, error: BaseException) -> int | None:
        """The JSON-RPC code an exception becomes, if it's one the SDK sends as a JSON-RPC error."""

    def jsonrpc_message(self, error: BaseException) -> str | None:
        """The message the SDK sends with that code when it isn't the exception's own text, which
        then never leaves the process; None when it sends that text."""


@dataclass(eq=False)
class Prepared:
    """What the extras take out of a call before the SDK checks it, and whether the wrapper answers it."""

    #: The request passed on: a copy without the extras' inputs, or the one that came.
    request: Any
    #: The arguments passed on, as `identify` and the event see them.
    arguments: Any
    #: Sessions apply to this call: the extra is on, at the server's level now.
    grouping: bool = False
    #: The tool leaves `session_id` to the wrapper.
    sessions: bool = False
    #: A session ID in the right format, sent by the agent.
    sent: str | None = None
    intent: str | None = None
    #: A call to the wrapper's own get_more_tools.
    answer: bool = False
    #: The handler is the tool (a low-level server): its result is the tool's own.
    own: bool = False


@dataclass(eq=False)
class Identifying:
    """A call's `identify`: its run and its deadline, apart from the call's record. What reacts to
    its settling or its deadline, past the call's event perhaps, holds this: never the record, with
    the call's arguments, its request and what the tool returned."""

    started: bool = False
    run: HookRun | None = None
    #: Why identify gave nothing, decided before it ran (skipped, couldn't start).
    failed: str | None = None
    #: identify's deadline passed: whatever it gives later is ignored.
    late: bool = False
    #: identify settled in time: decided once, by the first of its settling, its deadline and the build.
    on_time: bool = False
    #: Its deadline (monotonic seconds), fixed under `lock` with the time the tool returned, before
    #: the wrapper reads how the call ended (the app's code: its result, its error), and never
    #: changed: an answer that settles past it is late, though the timer that marks it hasn't run
    #: yet (the scheduler was held up meanwhile), or that read is still running.
    deadline: float | None = None
    #: Reentrant: shutdown() from a signal handler on a thread inside one of its sections can settle
    #: identify there (its loop closed: see HookRun.reclaim), and its settling takes it again (see
    #: start_identify's `learned`). Nothing under it runs the app's code or waits for a thread.
    lock: threading.RLock = dataclass_field(default_factory=threading.RLock)
    #: What identify returned, read and checked once (parity-4): None until a contender (its
    #: settling, or the build) claims the check, CHECKING while that one reads the app's object,
    #: then the check itself. Whoever comes second uses that check, never a read of its own.
    checked: Any = None
    #: What waits for that check while another thread makes it: each run once it is published.
    on_checked: list[Callable[[], None]] = dataclass_field(default_factory=list)


#: identify's result is being read and checked by another thread (see Identifying.checked).
CHECKING = object()
#: The build found that check under way: it goes on once the check is published.
PENDING = object()


@dataclass(eq=False)
class Running:
    scope: CallScope
    started: float
    captured_at: str
    tool_name: str
    args: Any
    inputs: bool
    results: bool
    client: Any
    protocol_version: Any
    continued: bool
    protocol_session: str | None
    extras: Prepared
    #: Where the call arrived: identify and late builds run as if from there.
    origin: Origin
    context: Any
    #: The server the call came to: on fastmcp, its `call_tool()` records this call's steps.
    owner: State | None = None
    #: An `isError` result whose function wasn't entered is the SDK's (see `tap_tools`).
    watched: bool = True
    #: mcp 2.x: a function runs as the call's tool only inside the handler's step (`_boundary.HANDLING`).
    gated: bool = False
    #: fastmcp: what the call's last core step did, as it ended (`_fastmcp.Core`).
    core: Any = None
    session: tuple[str, str] | None = None
    identify: Identifying = dataclass_field(default_factory=Identifying)
    # What the layers inside the SDK saw, for the ID line's provenance.
    inner_result: Any = None
    inner_snapshot: Any = None
    #: Middleware was appended after the inner one when the call arrived.
    inner_misplaced: bool = False
    core_result: Any = None
    core_snapshot: Any = None
    chain_result: Any = None


@dataclass(eq=False)
class Outcome:
    status: str = "completed"
    error: dict[str, Any] | None = None
    round: str | None = None
    task: dict[str, Any] | None = None
    #: The result is recorded.
    result: bool = False


# -- The extras' inputs -----------------------------------------------------------------------------


def note(state: State, extras: Extras, tool_name: str, owner: Ownership) -> None:
    """A tool the extras leave alone, or one with an input of its own of their names, is logged
    once per client."""
    internal = state.internal
    label = json.dumps(tool_name, ensure_ascii=False)
    if not owner.readable and (extras.sessions or extras.intent):
        internal.warn_once(
            f"extras-unreadable/{tool_name}",
            f"[lsquare] Tool {label} gets no session_id or intent: its input schema can't be read safely ({owner.why}), so any input could be its own.",
        )
    for on, name in ((extras.sessions, SESSION_ID), (extras.intent, INTENT)):
        if on and owner.readable and name in owner.declared:
            internal.warn_once(f"extras-own/{name}/{tool_name}", f"[lsquare] Tool {label} has its own {name} input: it is left as it is.")


def own_missing_tools(state: State) -> None:
    state.internal.warn_once(
        "extras-own/get_more_tools", f"[lsquare] The server has its own {MISSING_TOOLS} tool: it is left as it is, and LSquare's isn't added."
    )


def ownership_of(state: State, tool_name: str, read: Read | None = None) -> Ownership | None:
    """What a registered tool's definition, as the server holds it now, says of the extras'
    inputs; None for a name the server hasn't registered. `read`: the listing's or the call's one
    read of the registry; without it, the registry is read now."""
    registry = state.registry
    if registry is None:
        return None
    owner = (read if read is not None else registry).ownership(tool_name)
    if owner is not None:
        note(state, state.high_extras, tool_name, owner)
    return owner


def offers_missing_tools(state: State, read: Read | None = None) -> bool:
    registry = state.registry
    if registry is None:
        return False
    source = read if read is not None else registry
    if source.owns_missing_tools():
        own_missing_tools(state)
        return False
    return source.offers_missing_tools()


async def answers_missing_tools(state: State, dispatch: Dispatch) -> bool:
    """The call is the wrapper's own get_more_tools: offered, and the server shows a tool of its
    own now (an enabled one, on fastmcp)."""
    def offered() -> bool:
        # The line notices a handler replaced meanwhile (mcp 2.x compares its entry).
        refresh = attr(dispatch, "refresh")
        if callable(refresh):
            refresh()
        return high_on(state) and state.high_extras.missing_tools and dispatch.tool_name == MISSING_TOOLS and offers_missing_tools(state)

    try:
        if not offered() or state.registry is None or not await state.registry.visible():
            return False
        # Checked again after the await: a tool of that name, or a new handler, may have come meanwhile.
        return offered()
    except Exception:
        return False


def prepare(state: State, dispatch: Dispatch, answers: bool = False, read: Read | None = None) -> Prepared:
    """Takes the extras' inputs out of a call to a tool whose current definition leaves them to
    the wrapper, valid or not, into a copy of the request. A tool that declares one of them, or
    whose definition can't be read, keeps its arguments as sent. `answers`: the call is the
    wrapper's own get_more_tools. `read`: the call's read of the registry (`call_read`)."""
    none = Prepared(request=dispatch.request, arguments=dispatch.arguments, own=not high_mode(state))
    try:
        if not high_on(state):
            return none
        extras = state.high_extras
        # Even a tool the extras leave alone is grouped by its protocol session.
        none.grouping = extras.sessions
        tool_name = dispatch.tool_name
        if not isinstance(tool_name, str):
            return none
        if answers:
            return strip(none, extras, None, dispatch)
        owner = ownership_of(state, tool_name, read)
        return strip(none, extras, owner, dispatch) if owner is not None and owner.readable else none
    except Exception:
        return none


def low_on(state: State, dispatch: Dispatch) -> bool:
    """A low-level server's extras apply to this request, or those of a server whose handler was
    replaced: the snapshot decides. On mcp 2.x, a request on 2026-07-28 or later gets none of them:
    change notifications reach only its listen streams there, which the wrapper can't see."""
    if high_mode(state) or not state.low_extras.any():
        return False
    if state.line == "v2":
        version = dispatch.version()
        if isinstance(version, str) and _DATE.match(version) and version >= SESSIONLESS_PROTOCOL:
            return False
    return True


def prepare_low(state: State, dispatch: Dispatch, snapshot: Snapshot | None) -> Prepared:
    """The same for a low-level server, or one whose handler was replaced, from its snapshot, or
    without one: the call is passed on as it came."""
    extras = state.low_extras
    none = Prepared(request=dispatch.request, arguments=dispatch.arguments, grouping=extras.sessions, own=True)
    try:
        tool_name = dispatch.tool_name
        if snapshot is None or not isinstance(tool_name, str):
            return none
        if snapshot.own_missing_tools and extras.missing_tools:
            own_missing_tools(state)
        if extras.missing_tools and tool_name == MISSING_TOOLS and snapshot.tools and not snapshot.own_missing_tools:
            return strip(none, extras, None, dispatch)
        owner = snapshot.tools.get(tool_name)
        if owner is not None:
            note(state, extras, tool_name, owner)
        return strip(none, extras, owner, dispatch) if owner is not None and owner.readable else none
    except Exception:
        return none


def strip(none: Prepared, extras: Extras, owner: Ownership | None, dispatch: Dispatch) -> Prepared:
    """Takes out the inputs `owner` leaves to the wrapper, or, for get_more_tools (no owner), its
    session_id, into a copy of the request. Raises on a request that can't be read: the caller then
    passes the call on as it came."""
    answer = owner is None
    sessions = extras.sessions and (owner is None or SESSION_ID not in owner.declared)
    intents = not answer and extras.intent and owner is not None and INTENT not in owner.declared
    prepared = Prepared(request=none.request, arguments=none.arguments, grouping=none.grouping, sessions=sessions, answer=answer, own=none.own)
    args = none.arguments
    if not isinstance(args, Mapping):
        return prepared
    removed: list[str] = []
    if sessions and SESSION_ID in args:
        removed.append(SESSION_ID)
        sent = args[SESSION_ID]
        if isinstance(sent, str) and SESSION_FORMAT.match(sent):
            prepared.sent = sent
    if intents and INTENT in args:
        removed.append(INTENT)
        intent = args[INTENT]
        if isinstance(intent, str) and intent != "":
            prepared.intent = intent
    if removed:
        copy = {key: value for key, value in args.items() if key not in removed}
        prepared.request = dispatch.with_arguments(copy)
        prepared.arguments = copy
    return prepared


def listed(
    state: State,
    tools: list[Any],
    last: bool,
    name_of: Callable[[Any], Any],
    schema_of: Callable[[Any], Any],
    with_schema: Callable[[Any, dict[str, Any]], Any],
    missing_tool: Callable[[dict[str, Any]], Any],
) -> list[Any] | None:
    """A copy of a listing's tools with the extras' inputs each one's definition leaves to the
    wrapper, where its listed schema can take them, and get_more_tools on the last page; None when
    nothing changes. The developer's definitions are never changed."""
    if not high_on(state):
        return None
    extras = state.high_extras
    # One read of the registry, as it is now, for the whole listing: one pass, not one per tool.
    read = state.registry.read() if state.registry is not None else None
    changed = False
    shown: list[Any] = []
    for tool in tools:
        copy = tool
        name = name_of(tool)
        if isinstance(name, str):
            owner = ownership_of(state, name, read)
            if owner is not None and owner.readable:
                names = [input_ for input_, on in ((SESSION_ID, extras.sessions), (INTENT, extras.intent)) if on and input_ not in owner.declared]
                schema = advertise(schema_of(tool), names)
                if schema is not None:
                    copy = with_schema(tool, schema)
                    changed = True
        shown.append(copy)
    # Added to the last page only, never next to a tool of that name, and only to a listing that
    # shows a tool of the server's own (a disabled one isn't listed).
    names = [name_of(tool) for tool in shown]
    if extras.missing_tools and last and MISSING_TOOLS not in names and any(name != MISSING_TOOLS for name in names) and offers_missing_tools(state, read):
        shown.append(missing_tool(missing_tools_tool(extras.sessions)))
        changed = True
    return shown if changed else None


async def low_listed(
    state: State,
    tools: list[Any],
    last: bool,
    first: bool,
    reader: Callable[[], Reader | None],
    name_of: Callable[[Any], Any],
    schema_of: Callable[[Any], Any],
    with_schema: Callable[[Any, dict[str, Any]], Any],
    missing_tool: Callable[[dict[str, Any]], Any],
    refresh: Callable[[], None] | None = None,
) -> list[Any] | None:
    """A low-level server's page with the extras, where the snapshot and the page agree: a tool
    gets an input only when both show it doesn't declare it. A page that disagrees, by a tool
    missing from the snapshot, another reading of a schema, or a list in one page with other
    names, marks the snapshot out of date, and gets neither inputs on those tools nor
    get_more_tools. None when nothing changes, or without the snapshot in time."""
    wait = Wait()
    try:
        snapshot = await with_snapshot(state.lists, state.internal, reader, wait)
    except BaseException as error:
        if cancelled(error):
            wait.cancelled()
        raise
    if refresh is not None:
        # A tools/list handler replaced during the wait: the snapshot no longer speaks for it.
        refresh()
        snapshot = fresh(state.lists)
    extras = state.low_extras
    if snapshot is None:
        return None
    names: set[str] = set()
    changed = False
    disagrees = False
    shown: list[Any] = []
    for tool in tools:
        name = name_of(tool)
        if not isinstance(name, str) or name in names:
            disagrees = True
            shown.append(tool)
            continue
        names.add(name)
        known = snapshot.tools.get(name)
        listed_ = listed_ownership(schema_of(tool))
        if known is None or not same_ownership(known, listed_):
            disagrees = True
            shown.append(tool)
            continue
        note(state, extras, name, listed_)
        copy = tool
        if listed_.readable:
            inputs = [input_ for input_, on in ((SESSION_ID, extras.sessions), (INTENT, extras.intent)) if on and input_ not in listed_.declared]
            schema = advertise(schema_of(tool), inputs)
            if schema is not None:
                copy = with_schema(tool, schema)
                changed = True
        shown.append(copy)
    if first and last and (len(names) != len(snapshot.tools) or any(name not in names for name in snapshot.tools)):
        disagrees = True
    if disagrees:
        mark(state.lists, False)
    if snapshot.own_missing_tools and extras.missing_tools:
        own_missing_tools(state)
    if extras.missing_tools and last and not disagrees and snapshot.tools and not snapshot.own_missing_tools and MISSING_TOOLS not in names:
        shown.append(missing_tool(missing_tools_tool(extras.sessions)))
        changed = True
    return shown if changed else None


# -- The call ---------------------------------------------------------------------------------------


async def on_call(state: State, dispatch: Dispatch, tap: Callable[[State, str, Read | None], bool | None] | None = None) -> Any:
    """Runs one `tools/call` through the app's handler, recording it."""
    arrived = (time.monotonic(), _iso_now())
    # A low-level server waits for its snapshot first, at most a second; a high-level one only
    # checks, for get_more_tools, that it shows a tool. Cancelled meanwhile, the call is still
    # recorded, as one cancelled before it ran.
    early: BaseException | None = None
    wait = Wait()
    low = low_on(state, dispatch)
    answers = False
    refresh = attr(dispatch, "refresh")
    try:
        if low:
            # The line checks its tools/list handler at each turn: replaced meanwhile, the snapshot
            # is out of date and read again, within the same second.
            while True:
                if callable(refresh):
                    refresh()
                if fresh(state.lists) is not None or not await wait_once(state.lists, state.internal, dispatch.reader, wait):
                    break
        else:
            answers = await answers_missing_tools(state, dispatch)
    except BaseException as error:  # noqa: BLE001 - a cancellation is recorded, then raised again
        if cancelled(error) or not isinstance(error, Exception):
            early = error
    # The app's inputs and results functions run first, before the registry is read below: what
    # they change of it is what that read finds.
    recorded = recording(state, dispatch)
    # Then the extras' inputs come out, in the same step as the handler is called, so the snapshot
    # they're taken by can't go stale before it runs.
    if low and callable(refresh):
        try:
            refresh()
        except Exception:
            pass
    # The registry is read once for the extras' inputs and the functions tapped, both in this step,
    # with neither an await nor any of the app's code between them.
    read = None if low or answers else call_read(state)
    prepared = prepare_low(state, dispatch, fresh(state.lists)) if low else prepare(state, dispatch, answers, read)
    passed = prepared.request

    async def handler(request: Any) -> Any:
        if prepared.answer:
            return await dispatch.answer(request, missing_tools_reply(item(prepared.arguments, "need")))
        return await dispatch.call(request)

    running: Running | None = None
    try:
        calls = state.internal.calls()
        if recorded is not None:
            running = start(state, prepared, dispatch, arrived, *recorded)
            if tap is not None and high_mode(state) and not prepared.answer:
                running.watched = tap(state, running.tool_name, read) is not False
    except Exception:
        running = None
    # Let go before the handler is awaited: a call still running keeps nothing of the registry.
    del read
    if running is None:
        if early is not None:
            raise early
        return await handler(passed)
    call = running
    call.scope.wrapper = call
    # identify starts once the tool's own function has been entered. On a low-level server, or one
    # whose handler was replaced, the handler is the tool, and a tool whose function can't be
    # watched can't say when it's entered: it starts now, and runs elsewhere.
    call.scope.on_tool_entry = lambda: start_identify(state, call)
    if not high_mode(state) or not call.watched:
        start_identify(state, call)

    # The app's handler is called exactly once: in the call's scope, or, if entering it fails, as it is.
    token = None
    try:
        token = calls.set(call.scope)
    except Exception:
        token = None
    result: Any = None
    thrown: BaseException | None = early
    try:
        if early is None:
            result = await handler(passed)
    except BaseException as error:  # noqa: BLE001 - recorded, then raised again unchanged
        thrown = error
    finally:
        if token is not None:
            try:
                calls.reset(token)
            except Exception:
                pass

    reply = result
    waits: Waits | None = None
    try:
        ended = returned(state, call)
        # The SDK answered without running the tool: identify starts now.
        start_identify(state, call)
        if thrown is not None and cancelled(thrown):
            # A read this call started, and fails from here, may fail for the cancellation.
            wait.cancelled()
        outcome = outcome_of(state, call, dispatch, result, thrown)
        if (outcome.error or {}).get("category") == "timeout":
            # Cancelled, also when the tool caught it and returned: still the call, for a read it started.
            wait.cancelled()
        # mcp 1.x's low-level decorator makes raises and refusals isError too: no ID line on errors.
        error_ids = not (state.line == "v1" and not high_mode(state))
        grouped = group(call, outcome, result, dispatch, raised=thrown is not None, error_ids=error_ids)
        if thrown is None:
            reply = grouped
        waits = hand_off(state, call, outcome, result, ended, dispatch)
    except Exception:
        _report(state, "Recording a tool call failed unexpectedly; its event was dropped.")
        let_go(call.scope)
        # Nothing to send: what is handed over ends once that report has reached on_error.
        if state.settings.wait_until is not None:
            hand_over(state.settings.wait_until, delivered_by(state.internal))
    if waits is not None:
        wait_until = state.settings.wait_until
        # Handed over first, however the call ended: one that raised, was cancelled or interrupted,
        # or whose reply waits. Handing over awaits nothing.
        if wait_until is not None:
            hand_over(wait_until, handed(state.internal, call.scope.call_id, waits))
        # flush() inside the tool, or flush_before_reply: the reply, or the error raised, waits for
        # the same sending. A cancellation or an exit is passed on at once: a cancelled scope
        # cancels every await in it, and an exit isn't held up.
        if waits.reply_waits and (thrown is None or isinstance(thrown, Exception)):
            try:
                await sent_by(state.internal, call.scope.call_id, waits.built, waits.reply_by, waits.turns)
            except BaseException as error:
                # Cancelled while waiting to reply: still the call, for a read it started.
                if cancelled(error):
                    wait.cancelled()
                raise
    if thrown is not None:
        raise thrown
    return reply


def recording(state: State, dispatch: Dispatch) -> tuple[bool, bool] | None:
    """Whether a call's inputs and its result are recorded, as the app's `inputs` and `results`
    answer for its tool. None when they can't be asked: the call then goes on unrecorded."""
    try:
        tool_name = dispatch.tool_name if isinstance(dispatch.tool_name, str) else ""
        settings = state.settings
        return settings.inputs(tool_name), settings.results(tool_name)
    except Exception:
        return None


def start(state: State, prepared: Prepared, dispatch: Dispatch, arrived: tuple[float, str], inputs: bool, results: bool) -> Running:
    tool_name = dispatch.tool_name if isinstance(dispatch.tool_name, str) else ""
    loop: asyncio.AbstractEventLoop | None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    scope = CallScope(call_id=str(uuid.uuid4()), loop=loop)
    version = dispatch.version()
    return Running(
        scope=scope,
        started=arrived[0],
        captured_at=arrived[1],
        tool_name=tool_name,
        args=prepared.arguments,
        inputs=inputs,
        results=results,
        client=dispatch.client(),
        protocol_version=version,
        continued=dispatch.continued(),
        protocol_session=protocol_session(state, dispatch, version) if prepared.grouping else None,
        extras=prepared,
        origin=state.internal.origin(),
        context=dispatch.context,
        owner=state,
        gated=attr(dispatch, "gated", False) is True,
    )


def protocol_session(state: State, dispatch: Dispatch, version: Any) -> str | None:
    """The session the transport established: none from 2026-07-28 on, which has no sessions; a
    version that isn't known counts as older."""
    if isinstance(version, str) and _DATE.match(version) and version >= SESSIONLESS_PROTOCOL:
        return None
    try:
        session = dispatch.session(version)
    except Exception:
        return None
    return session if session is not None and state.internal.valid_id(session) else None


def sse_message(request: Any) -> bool:
    """An HTTP request with SSE's `session_id` query parameter, as its message POSTs carry: that
    transport routes them by it, and never checks `mcp-session-id`. Untrusted on its own (a
    client can add it to any request): read only for a session no transport vouches for (on mcp
    1.x, one Streamable HTTP didn't open: `_v1.streamable`; on 2.x, one with no issued ID), and
    `http_session()` pins what it decides to the session."""
    try:
        get = attr(attr(request, "query_params"), "get")
        return callable(get) and isinstance(get("session_id"), str)
    except Exception:
        return False


def http_session(state: State, session: Any, choose: Callable[[], str | None]) -> str | None:
    """The protocol session of an HTTP session (or connection) that ran `initialize`: what
    `choose` gives at its first call, kept for as long as it lasts, so a later request on it
    (one that adds or drops a query parameter, say) can't change which session it is."""
    if session is None:
        return None
    try:
        with state.lock:
            kept = state.http_sessions.get(session)
        if kept is not None:
            return kept
        chosen = choose()
        if chosen is None:
            return None
        with state.lock:
            return state.http_sessions.setdefault(session, chosen)
    except TypeError:
        # Not weakly referenceable: nothing can be kept for it.
        return None


def made_session(state: State, connection: Any) -> str | None:
    """A session made for a connection that has none of its own, as long as it lasts."""
    if connection is None:
        return None
    try:
        with state.lock:
            made = state.made_sessions.get(connection)
            if made is None:
                made = state.made_sessions.setdefault(connection, str(uuid.uuid4()))
            return made
    except TypeError:
        # Not weakly referenceable: no session can be kept for it.
        return None


def group(call: Running, outcome: Outcome, result: Any, dispatch: Dispatch, raised: bool = False, error_ids: bool = True) -> Any:
    """The call's session, decided when the tool returns: the ID the agent sent; else none when
    the tool called capture() with a conversationId, which groups it; else, for a client on a
    protocol version with sessions, the transport's; else a new one, given to the agent in a text
    block at the end of a copy of the tool's own final result. A call that raised is grouped too,
    and gets no new one. Returns the reply."""
    extras = call.extras
    if not extras.grouping:
        return result
    if extras.sent is not None:
        call.session = (extras.sent, "sent")
        return result
    agent = call.scope.agent
    conversation_id = agent.get("conversationId") if isinstance(agent, dict) else None
    if isinstance(conversation_id, str) and conversation_id != "":
        return result
    if call.protocol_session is not None:
        call.session = (call.protocol_session, "protocol")
        return result
    if raised or not extras.sessions:
        return result
    # Only the tool's own final result: not a raise, a result the SDK made, a task or a round.
    if outcome.task is not None or outcome.round == "input_required":
        return result
    if outcome.status == "error" and (not error_ids or (outcome.error or {}).get("category") != "tool_error"):
        return result
    # On a high-level server, the result must be the one the SDK made from the tool's (or the
    # wrapper's own get_more_tools) answer, passed on unchanged.
    if not extras.own:
        if not extras.answer and call.scope.tool_result is None:
            return result
        if not dispatch.provenance(call, result):
            return result
    try:
        content = dispatch.read(result, "content")
        if not isinstance(content, list):
            return result
        session_id = new_session_id()
        reply = dispatch.with_block(result, id_block(session_id)["text"])
        call.session = (session_id, "issued")
        return reply
    except Exception:
        # A result that can't be copied goes as it is, without an ID.
        return result


# -- identify ---------------------------------------------------------------------------------------


def start_identify(state: State, call: Running) -> None:
    """Starts `identify`, once, within its limit: past it, the hook isn't called. It runs as a
    callback, outside the call. The ID fields it returns become the last ones known as soon as it
    succeeds. Never raises."""
    identify = call.identify
    try:
        with identify.lock:
            if identify.started:
                return
            identify.started = True
        hook = state.settings.identify
        if hook is None:
            return
        internal = state.internal
        # What its input is made of, not the call's record: the client lets go of this once the
        # input is made.
        tool_name, args, context = call.tool_name, call.args, call.context
        run = internal.identify(hook, lambda signal: IdentifyInput(tool_name, args, context, signal), call.origin)
        if run is None:
            # After shutdown() it isn't called, and the call's event is dropped, which the client
            # reports once, as shutdown: never as a hook limit it didn't reach (parity-25).
            if not getattr(internal, "closed", False):
                identify.failed = "skipped at the hook limit"
            return
        identify.run = run

        # Kept for as long as identify runs, past the call's event perhaps: it holds the client's
        # books and identify's own state, never the call nor the server.
        def learned(future: concurrent.futures.Future[Any]) -> None:
            try:
                # Past its deadline the call has already gone without it: its IDs teach nothing.
                if not on_time(identify):
                    return
                # Read and checked once, here or by the build, whichever claims it first; the ID
                # fields last known are taken from that check (see checked_once).
                checked_once(internal, identify, future)
            except Exception:
                pass

        run.future.add_done_callback(learned)
    except Exception:
        identify.failed = "could not be started"


def check_identity(internal: Internal, future: concurrent.futures.Future[Any]) -> tuple[bool, dict[str, str], Any, str]:
    """(ok, ids, metadata, why) from what identify gave."""
    try:
        error = future.exception()
    except BaseException as caught:  # noqa: BLE001 - cancelled
        # Read on the call's loop, perhaps the main thread's: a signal handler's SystemExit goes on.
        if interrupts(caught):
            raise
        return (False, {}, None, "threw")
    if error is not None:
        return (False, {}, None, "threw")
    try:
        value = future.result()
        if value is None:
            return (True, {}, None, "")
        fields = as_object(value)
        if fields is None:
            return (False, {}, None, "returned something other than an object")
        read: dict[str, Any] = {}
        for key in ("orgId", "userId", "conversationId", "metadata"):
            try:
                read[key] = fields[key] if key in fields else None
            except Exception:
                return (False, {}, None, "threw reading its result")
        app_ids(read)
        ids: dict[str, str] = {}
        for key in ("orgId", "userId", "conversationId"):
            item_ = read[key]
            if item_ is None or item_ == "":
                continue
            if not internal.valid_id(item_):
                return (False, {}, None, f"returned an invalid {key}")
            ids[key] = item_
        return (True, ids, read["metadata"], "")
    except Exception:
        return (False, {}, None, "could not be checked")


def checked_once(
    internal: Internal,
    identify: Identifying,
    future: concurrent.futures.Future[Any],
    waiting: Callable[[], None] | None = None,
) -> tuple[bool, dict[str, str], Any, str] | None:
    """identify's result, read and checked once whoever asks (parity-4): its settling and the build
    are its contenders. The first claims the check under identify's lock, reads the app's object
    outside it (its getters, which may be slow), then publishes the check, and the ID fields it
    gives become the last ones known. Another that asks meanwhile gets None and, given `waiting`,
    has that run once the check is published, rather than wait for it there (the call's loop, say)
    or read the object again: changing the object identify returned changes neither the event nor
    the ID fields last known. Asked only once identify has settled in time (see on_time)."""
    with identify.lock:
        checked = identify.checked
        if checked is CHECKING:
            if waiting is not None:
                identify.on_checked.append(waiting)
            return None
        if checked is not None:
            return checked  # type: ignore[no-any-return]
        identify.checked = CHECKING
    made: tuple[bool, dict[str, str], Any, str] = (False, {}, None, "could not be checked")
    try:
        made = check_identity(internal, future)
    finally:
        with identify.lock:
            identify.checked = made
            then, identify.on_checked = identify.on_checked, []
        if made[0]:
            try:
                internal.identified["keys"] = tuple(made[1])
            except Exception:
                pass
        for fn in then:
            try:
                fn()
            except Exception:
                pass
    return made


def on_time(identify: Identifying) -> bool:
    """Whether identify settled in time, decided once and for all by whichever comes first: its
    settling, its deadline, or the build. A late answer then changes nothing, whenever it comes."""
    run = identify.run
    with identify.lock:
        if not identify.late and not identify.on_time:
            # Checked against the clock too (0.1's rule for hooks): past its deadline it is late,
            # whether or not the timer that marks it has run.
            deadline = identify.deadline
            if run is not None and run.future.done() and (deadline is None or time.monotonic() < deadline):
                identify.on_time = True
            else:
                identify.late = True
        return identify.on_time


def identified(state: State, call: Running, waiting: Callable[[], None] | None = None) -> Any:
    """What identify gave by now: None without identify; a failure when it hadn't settled in time;
    PENDING while another thread checks what it gave, `waiting` then run once that check is
    published (see checked_once)."""
    identify = call.identify
    if identify.failed is not None:
        return (False, {}, None, identify.failed)
    run = identify.run
    if run is None:
        return None
    if not on_time(identify):
        return (False, {}, None, "took longer than its deadline")
    checked = checked_once(state.internal, identify, run.future, waiting)
    return PENDING if checked is None else checked


# -- The event --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Waits:
    """What a call's reply and `wait_until` wait for, decided once, when the call ended."""

    #: Done once the call's event is queued or dropped.
    built: concurrent.futures.Future[None]
    #: The turns captured inside the call and still being sent when the tool returned (the client's
    #: handles, see `CallScope.turns`): waited for with its event. Nothing here holds the call
    #: itself, so its result isn't kept for as long as the sending takes.
    turns: frozenset[Any]
    #: The reply waits for the sending, until `reply_by`: flush() inside the tool, or
    #: `flush_before_reply` without `wait_until`.
    reply_waits: bool
    #: Monotonic seconds.
    reply_by: float
    #: Until when the sending is waited for: `wait_until`'s 30 seconds, or without it the reply's.
    send_by: float


@dataclass(frozen=True)
class Returned:
    """What is decided as the tool returns, before the wrapper reads how the call ended."""

    #: When the tool returned (monotonic seconds).
    finished: float
    #: The reply waits for the call's sending: flush() inside the tool, or `flush_before_reply`
    #: without `wait_until`.
    reply_waits: bool
    #: identify's deadline (monotonic seconds), as `Identifying.deadline` holds it.
    identify_by: float


def returned(state: State, call: Running) -> Returned:
    """The tool has returned: when, whether the reply waits, and identify's deadline, fixed before
    the wrapper reads how the call ended. That read runs the app's code (a result's fields, an
    error's message), which may take any time: an identify that settles meanwhile, past this
    deadline, is late."""
    internal = state.internal
    settings = state.settings
    call.scope.returned = True
    # flush() inside the tool promises its event before the reply, whatever the options. Read once:
    # a flush() from a thread the tool left running can't change it between here and the reply.
    reply_waits = call.scope.flush_requested or (settings.wait_until is None and settings.flush_before_reply)
    # identify waits as long as the reply does, its second at most. A flush_timeout_ms of 0 waits
    # for nothing: the reply doesn't, and identify keeps its second, the event going once it answers.
    limit_s = internal.flush_timeout_ms / 1000 if reply_waits and internal.flush_timeout_ms > 0 else WAIT_UNTIL_S
    identify = call.identify
    # The time and the deadline together, under the lock on_time() decides under: a settling judged
    # before this was before the tool returned, one judged after has the deadline.
    with identify.lock:
        finished = time.monotonic()
        identify_by = identify.deadline = finished + min(IDENTIFY_WAIT_S, limit_s)
    return Returned(finished, reply_waits, identify_by)


def hand_off(state: State, call: Running, outcome: Outcome, result: Any, ended: Returned, dispatch: Dispatch) -> Waits:
    """Builds the call's event now if identify has settled or isn't set; otherwise on the call's
    loop, when identify settles or at its deadline, whichever comes first, once; a client thread
    builds it instead if that loop can't take it, or hasn't run it LOOP_GRACE_S past that deadline
    (py-mcp-19), or LOOP_GRACE_S past the publication of a check of identify's result that the
    build found under way (parity-4). What is queued on the loop, kept by identify's future and
    kept by that check is a trampoline holding only a list the build's taking empties, so none of
    them keeps anything of the call once it is taken, and a loop stopped with it queued runs nothing
    once it runs again. shutdown() takes a build still to come and lets go of it (see `discard`),
    from any thread, or from a signal handler on this one: the taking takes no lock. So does the
    registration itself (`track`) once the client is closed, and nothing is armed for it then.
    Never awaits. Returns what the reply and `wait_until` wait for."""
    internal = state.internal
    settings = state.settings
    finished, reply_waits, identify_by = ended.finished, ended.reply_waits, ended.identify_by
    reply_by = finished + internal.flush_timeout_ms / 1000
    # With wait_until, the sending is handed to it whether or not the reply waits too: what the
    # reply doesn't wait for goes on after it, up to WAIT_UNTIL_S.
    send_by = reply_by if reply_waits and settings.wait_until is None else finished + WAIT_UNTIL_S
    origin = internal.origin()
    built: concurrent.futures.Future[None] = concurrent.futures.Future()
    waits = Waits(built, internal.turns(call.scope), reply_waits, reply_by, send_by)
    #: Who took the call: set once, in one atomic step (a fresh token's setdefault), never under a
    #: lock, so a shutdown() a signal handler makes on a thread stopped anywhere in a claim, or in
    #: `keep`, neither waits for that thread nor takes the call a second time.
    taken: dict[str, object] = {}
    #: The timers' cancels, each popped, and called, once: by the claim, or by `keep` if the claim
    #: came first.
    cancels: list[Callable[[], None]] = []
    #: What identify's settling runs, until the call is taken: its future keeps its callbacks for
    #: as long as identify runs, past its deadline perhaps, and they mustn't keep the call, nor
    #: what the tool returned.
    on_settled: list[Callable[[], None]] = []
    #: What the call's loop runs, until the call is taken: the same for a loop that stops with it
    #: queued, then runs again, or never does.
    on_loop: list[Callable[[], None]] = []
    #: What the publication of a check of identify's result, which the build found under way on
    #: another thread, runs, until the call is taken: that check keeps its waiters for as long as
    #: it reads the app's object (a getter that blocks, perhaps), past shutdown() too.
    on_published: list[Callable[[], None]] = []

    def cancel_timers() -> None:
        while True:
            try:
                cancel = cancels.pop()
            except IndexError:
                return
            try:
                cancel()
            except Exception:
                pass

    def claim() -> bool:
        """The first of the build, its fallback, the drop and shutdown()'s discard to come takes the
        call; the rest do nothing. It empties what the loop, identify and the timers hold."""
        token = object()
        if taken.setdefault("by", token) is not token:
            return False
        on_settled.clear()
        on_loop.clear()
        on_published.clear()
        cancel_timers()
        return True

    def keep(cancel: Callable[[], None]) -> None:
        """A timer's cancel, for the claim to call: kept first, then checked, so a claim made
        meanwhile, from another thread or a signal handler on this one, either finds it kept or is
        found here, and the timer is cancelled, once, at once: none armed after the claim keeps the
        call until it fires."""
        cancels.append(cancel)
        if taken:
            cancel_timers()

    identify = call.identify

    def abort_identify() -> None:
        run = identify.run
        if run is not None:
            try:
                run.abort()
            except Exception:
                pass

    def again() -> None:
        # The check of identify's result another thread was making is published: built from it, on
        # the call's loop, as at identify's settling. The timers may be spent by then (the
        # deadline's fallback found the check under way too), or were never armed (identify had
        # settled as the tool returned): a client thread builds it LOOP_GRACE_S from now, should
        # that loop have stopped or be blocked.
        for fn in list(on_settled):
            fn()
        if not taken:
            # Taken already (built, or let go of by shutdown()): no timer, nor a thread for one.
            keep(internal.call_later(LOOP_GRACE_S, build, drop))

    def published() -> None:
        for fn in list(on_published):
            fn()

    def build() -> None:
        try:
            identity = identified(state, call, published)
        except Exception:
            identity = (False, {}, None, "could not be checked")
        if identity is PENDING:
            # That check runs on another thread, perhaps the app's own code: never waited for here,
            # on the call's loop perhaps. Built once it is published (`again`).
            return
        if not claim():
            return
        try:
            record, diagnostic = build_record(state, call, outcome, result, finished, dispatch, identity)
            internal.record(record, origin=origin, diagnostic=diagnostic)
        except Exception:
            _report(state, "Recording a tool call failed unexpectedly; its event was dropped.")
        finally:
            let_go(call.scope)
            _done(built)
            abort_identify()

    def drop() -> None:
        """No thread could take the build: the event is dropped, and said so."""
        if not claim():
            return
        _report(state, "No thread could build a tool call's event; it was dropped.")
        let_go(call.scope)
        _done(built)
        abort_identify()

    def discard() -> None:
        """shutdown() ends a build still to come: its event, already counted as failed there, is
        never built, and what holds the call lets go of it now."""
        if not claim():
            return
        let_go(call.scope)
        _done(built)
        abort_identify()

    def run_on_loop() -> None:
        for fn in list(on_loop):
            fn()

    def build_soon() -> None:
        if not internal.call_on_loop(origin, run_on_loop):
            internal.call_later(0, build, drop)

    def settled(_: Any) -> None:
        for fn in list(on_settled):
            fn()

    # Filled before shutdown() can take the call (`discard`, exposed by track()): a claim then
    # empties them, and nothing registered after it, below, holds more than an emptied list or a
    # timer `keep` cancels.
    on_settled.append(build_soon)
    on_loop.append(build)
    on_published.append(again)
    # A flush that finds it unbuilt reports it from here, the call's own context and loop. A client
    # closed by now (the tool awaited ashutdown(), or a shutdown() came during this registration)
    # takes it there and then, through `discard`: its future done, the shutdown reported.
    internal.track(call.scope.call_id, built, origin, discard)
    if taken:
        # Disposed of: nothing to build, nor to wait for, and no timer or callback armed for it.
        return waits
    run = identify.run
    if run is None or run.future.done():
        build()
        return waits
    run.future.add_done_callback(settled)
    left = identify_by - time.monotonic()

    # identify's deadline, on the scheduler whatever the build threads do: it is late from here,
    # its run aborted, and the build goes to the call's loop, as at its settling.
    def at_deadline() -> None:
        on_time(identify)
        abort_identify()
        build_soon()

    keep(internal.timer(left, at_deadline))
    # The fallback, for a loop that took the build but hasn't run it by then (stopped, blocked).
    keep(internal.call_later(left + LOOP_GRACE_S, build, drop))
    return waits


def let_go(scope: CallScope) -> None:
    """Once the call's event is built, or dropped for good. A turn captured inside the call keeps
    its scope (in its capture's context) until that turn is sent: from here, the scope keeps the
    call's ID and its boundary, not what the tool returned or raised, nor the wrapper's record of
    the call. Never raises."""
    try:
        # Marked first: a run that records after this, on any thread, sees it (see _boundary._Run).
        scope.released = True
        scope.agent = None
        scope.thrown = None
        scope.tool_result = None
        scope.on_tool_entry = None
        running, scope.wrapper = scope.wrapper, None
        if isinstance(running, Running):
            # What the layers inside the SDK saw of the result, for the reply's ID line: let go
            # of with the event, whatever still holds the record (a reply still waiting).
            running.inner_result = running.inner_snapshot = None
            running.core_result = running.core_snapshot = running.chain_result = None
            running.core = None
    except Exception:
        pass


def build_record(
    state: State,
    call: Running,
    outcome: Outcome,
    result: Any,
    finished: float,
    dispatch: Dispatch,
    identity: tuple[bool, dict[str, str], Any, str] | None,
) -> tuple[dict[str, Any], LSquareError | None]:
    """The call's record, and what it reports of identify (identify_failed), which the client
    reports as it admits the event, never for one it refuses for shutdown() (parity-25)."""
    internal = state.internal
    scope = call.scope
    diagnostic: LSquareError | None = None
    agent = scope.agent
    record: dict[str, Any] = {
        "callId": scope.call_id,
        "capturedAt": call.captured_at,
        "toolName": call.tool_name,
        "status": outcome.status,
        # As JavaScript's Math.round: halves up.
        "durationMs": max(0, math.floor((finished - call.started) * 1000 + 0.5)),
        "ids": {},
        "diagnostics": False,
    }
    # The IDs: identify's, then capture()'s inside the call, which win.
    captured: dict[str, str] = {}
    for key in ("orgId", "userId", "conversationId"):
        value = agent.get(key) if isinstance(agent, dict) else None
        if value is not None and value != "":
            captured[key] = value
    record["ids"] = {**(identity[1] if identity is not None and identity[0] else {}), **captured}
    if identity is not None and not identity[0]:
        # Diagnostics, unless capture() supplied every ID identify last returned (a userId before any).
        # A last success that returned no IDs needs none: only "never succeeded" (None) needs a
        # userId, as TypeScript's `??` reads it.
        keys = internal.identified["keys"]
        needed = ("userId",) if keys is None else keys
        supplied = all(internal.valid_id(captured.get(key)) for key in needed)
        record["diagnostics"] = not supplied
        how = "with capture()'s IDs" if supplied else "as limited diagnostics"
        diagnostic = LSquareError("identify_failed", f"identify {identity[3]}; the call was sent {how}.")
    elif identity is not None and identity[2] is not None:
        record["metadata"] = identity[2]
    if isinstance(agent, dict):
        record["agent"] = agent
        # The turn's reference, from the turnId the event keeps: one it drops makes none.
        turn = internal.turn_id(agent.get("turnId"))
        conversation_id = captured.get("conversationId")
        # An orgId that isn't valid makes none either: the event is then dropped for it, as invalid_id.
        org_id = record["ids"].get("orgId")
        if turn is not None and conversation_id is not None and internal.valid_id(conversation_id) and (org_id is None or internal.valid_id(org_id)):
            record["turnRef"] = internal.turn_key(org_id, conversation_id, turn)
    server = server_info(state)
    if server is not None:
        record["server"] = server
    if call.client is not None:
        record["client"] = call.client
    if call.protocol_version is not None:
        record["protocolVersion"] = call.protocol_version
    if call.session is not None:
        record["sessionId"], record["sessionSource"] = call.session
    # Recorded whether or not inputs are: turning the extra off is what stops it.
    if call.extras.intent is not None:
        record["intent"] = call.extras.intent
    if call.extras.answer:
        record["extra"] = "missing_tools"
    if call.inputs:
        record["input"] = {"value": call.args}
    if outcome.error is not None:
        record["error"] = outcome.error
    if outcome.round is not None:
        record["round"] = outcome.round
    if outcome.task is not None:
        record["task"] = outcome.task
    if outcome.result:
        record["result"] = {"value": dispatch.recorded(result)}
    return record, diagnostic


def server_info(state: State) -> Any:
    """The server's name and version, as it gives them in `initialize`."""
    low = state.low
    info = attr(low, "server_info")
    if info is not None and not callable(info):
        return info
    out: dict[str, Any] = {}
    for key in ("name", "version", "title"):
        value = attr(low, key)
        if isinstance(value, str):
            out[key] = value
    return out or None


def outcome_of(state: State, call: Running, dispatch: Dispatch, result: Any, thrown: BaseException | None) -> Outcome:
    """How the call ended, read as it ended. A raise is `protocol` when the SDK sends it as a
    JSON-RPC error, `thrown` otherwise. An isError result is `thrown` when the tool's own function
    raised (the SDK turned it into the result), `protocol` when the SDK made it (the tool never
    ran, or its output failed the SDK's checks), and `tool_error` when the tool returned it. A
    cancelled call is `timeout`. An isError result's message is its first text block, which is
    result text: left out when results aren't recorded."""
    scope = call.scope
    outcome = Outcome()
    if thrown is None:
        # A JSON-RPC error returned in place of a result goes to the client as if it were raised.
        thrown = refusal_of(dispatch, result)
    if thrown is None and dispatch.read(result, "resultType") == "input_required":
        outcome.round = "input_required"
    elif call.continued:
        outcome.round = "continuation"
    # Cancelled, also when the handler then returned (a tool that caught the cancellation, a sync
    # one that ran on in its worker thread): the client never gets that result. TypeScript reads
    # the request's aborted signal the same way.
    if (thrown is not None and cancelled(thrown)) or cancel_requested(dispatch) or scope_cancelled():
        outcome.status = "error"
        outcome.error = {"message": "The request was cancelled before the tool finished.", "category": "timeout"}
        return outcome
    if thrown is not None:
        outcome.status = "error"
        code = dispatch.jsonrpc_code(thrown)
        # The message the client got: mcp 2.x sends its own for params pydantic refused, whose text
        # quotes the values it refused, arguments included. That text is never read.
        sent = dispatch.jsonrpc_message(thrown) if code is not None else None
        outcome.error = error_of(thrown, "protocol" if code is not None else "thrown", code, sent)
        return outcome
    if result is None:
        return outcome
    task = dispatch.read(result, "task")
    task_id = wire(task, "taskId")
    if isinstance(task_id, str):
        outcome.task = {"id": task_id, "status": wire(task, "status")}
    else:
        outcome.result = call.results
    if dispatch.read(result, "isError") is True:
        outcome.status = "error"
        if scope.thrown is not None:
            outcome.error = error_of(scope.thrown[0], "thrown", None)
        else:
            # The tool's own error result, and the SDK sent it on: not one the SDK made when it
            # couldn't convert or validate what the tool returned.
            content = dispatch.read(result, "content")
            value = scope.tool_result[0] if scope.tool_result is not None else None
            returned = wire(value, "isError") is True and _same_content(wire(value, "content"), content)
            # The wrapper's own get_more_tools refuses arguments as the SDKs do, at either level.
            protocol = call.extras.answer or (high_mode(state) and made_by_sdk(state, call, returned, content))
            error: dict[str, Any] = {"category": "protocol" if protocol else "tool_error"}
            if call.results:
                text = first_text(dispatch.read(result, "content"))
                if text is not None:
                    error["message"] = text
            outcome.error = error
    return outcome


def made_by_sdk(state: State, call: Running, returned: bool, content: Any) -> bool:
    """Whether an `isError` result, with no raise recorded in the tool's body, is one the SDK (or
    middleware) made, not the tool's own (`returned`: the function's last run returned that
    result). On fastmcp, what the call's last core step did, as it ended, decides first: nothing
    (middleware answered), a refusal, or a success that something made an error afterwards is the
    SDK's. A function that ran is then the tool's if it returned that result; one that can't be
    watched (another provider's tool) may have run unseen, so its error is its own."""
    scope = call.scope
    if state.core_watched and state.registry is not None and state.registry.kind == "fastmcp":
        core = call.core
        if core is None or core.kind == "refused":
            return True
        if core.kind == "raised":
            # A watched function's raise is `thrown`: this one raised past it, or for one unseen.
            return scope.invoked or call.watched
        if not core.kept(content):
            return True
    if scope.invoked:
        return not returned
    return call.watched


def refusal_of(dispatch: Dispatch, result: Any) -> BaseException | None:
    """The exception a result that is a JSON-RPC error (`ErrorData`) is sent as, where the line
    knows one. Never raises."""
    refusal = attr(dispatch, "refusal")
    try:
        found = refusal(result) if callable(refusal) else None
        return found if isinstance(found, BaseException) else None
    except Exception:
        return None


def cancel_requested(dispatch: Dispatch) -> bool:
    """The line's own record that the client sent notifications/cancelled for this request, where it
    keeps one apart from the cancel scope (mcp 2.x). Never raises."""
    requested = attr(dispatch, "cancel_requested")
    try:
        return callable(requested) and requested() is True
    except Exception:
        return False


def error_of(error: BaseException, category: str, code: int | None, message: str | None = None) -> dict[str, Any]:
    """`message`, when given, is recorded instead of the exception's text."""
    details: dict[str, Any] = {"category": category}
    try:
        details["message"] = str(error) if message is None else message
    except Exception:
        details["message"] = "Unknown error"
    try:
        details["type"] = type(error).__name__
    except Exception:
        pass
    if category == "protocol" and code is not None:
        details["code"] = code
    return details


def _same_content(returned: Any, sent: Any) -> bool:
    """The result sent has the content the tool returned: as many blocks, and the same first text."""
    try:
        return isinstance(returned, list) and isinstance(sent, list) and len(returned) == len(sent) and first_text(returned) == first_text(sent)
    except Exception:
        return False


def first_text(content: Any) -> str | None:
    try:
        if not isinstance(content, list):
            return None
        for block in content[:1_000]:
            if wire(block, "type") == "text":
                text = wire(block, "text")
                return text if isinstance(text, str) else None
    except Exception:
        pass
    return None


# -- Provenance -------------------------------------------------------------------------------------

#: A result with more top-level fields than this can't be snapshotted: no ID line.
SNAPSHOT_FIELDS = 100


def snapshot(value: Any) -> tuple[tuple[tuple[Any, Any], ...], int | None] | None:
    """What a layer could change of a result without replacing it, read cheaply: its top-level
    fields, holding their values to compare by identity, and its content list's length. None when
    it can't be read within bounds."""
    try:
        source = value if isinstance(value, Mapping) else attr(value, "__dict__")
        if not isinstance(source, Mapping):
            return None
        entries: list[tuple[Any, Any]] = []
        for count, entry in enumerate(source.items()):
            if count >= SNAPSHOT_FIELDS:
                return None
            entries.append(entry)
        content = next((entry for key, entry in entries if key == "content"), None)
        return (tuple(entries), len(content) if isinstance(content, list) else None)
    except Exception:
        return None


def unchanged(before: tuple[tuple[tuple[Any, Any], ...], int | None] | None, value: Any) -> bool:
    """The result still has the fields the snapshot holds, the same objects, and as much content."""
    now = snapshot(value)
    if before is None or now is None or before[1] != now[1] or len(before[0]) != len(now[0]):
        return False
    return all(a[0] == b[0] and a[1] is b[1] for a, b in zip(before[0], now[0]))


# -- Waiting for the event --------------------------------------------------------------------------


async def sent_by(
    internal: Internal, call_id: str, built: concurrent.futures.Future[None], deadline: float, turns: frozenset[Any] = frozenset()
) -> None:
    """Waits for the call's event to be built, then for it and `turns` (see `Waits`) to be sent or
    given up, until `deadline` (monotonic seconds). Cancelling it ends it at once."""
    try:
        await internal.wait(built, deadline - time.monotonic())
        left = deadline - time.monotonic()
        if left <= 0:
            return
        await internal.wait(internal.settled(call_id, left * 1000, turns), left)
    except Exception:
        pass


async def handed(internal: Internal, call_id: str, waits: Waits) -> None:
    """What `wait_until` is given: the call's sending (see `sent_by`), then what was reported about
    the call (its event refused or failed, identify failing) reaching on_error or the logger, since
    the host may freeze the process once it ends. The reply never waits for that."""
    await sent_by(internal, call_id, waits.built, waits.send_by, waits.turns)
    await delivered_by(internal)


async def delivered_by(internal: Internal) -> None:
    """Waits, at most DELIVERED_GRACE_S, for the reports and log lines raised so far to reach
    on_error or the logger. Cancelling it ends it at once."""
    try:
        await internal.wait(internal.delivered(), DELIVERED_GRACE_S)
    except Exception:
        pass


def hand_over(wait_until: Callable[[Any], Any], awaitable: Awaitable[None]) -> None:
    """Gives the host's `wait_until` the call's sending. One that refuses it costs nothing:
    sending carries on in the background."""
    try:
        out = wait_until(awaitable)
    except Exception:
        close(awaitable)
        return
    try:
        if asyncio.iscoroutine(out):
            asyncio.get_running_loop().create_task(out).add_done_callback(lambda task: task.cancelled() or task.exception())
    except Exception:
        close(out)


def _done(future: concurrent.futures.Future[None]) -> None:
    try:
        if not future.done():
            future.set_result(None)
    except concurrent.futures.InvalidStateError:
        pass


def _report(state: State, message: str) -> None:
    try:
        state.internal.report(LSquareError("internal_error", message))
    except Exception:
        pass


def _iso_now() -> str:
    moment = datetime.now(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"
