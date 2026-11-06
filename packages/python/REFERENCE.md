# lsquare: reference

Everything the [README](README.md) leaves out, for when you need it: the details of each part
of the SDK, the extras on every kind of server, and serverless hosts.

## MCP servers

### The calling app

The calling app is the name the MCP client gives itself. A client on a protocol version before
2026-07-28 gives it only in `initialize`, so on a stateless HTTP server its calls are recorded
without the calling app; their protocol version comes from the request's `MCP-Protocol-Version`
header, and a request without one is recorded without a version.

### Agent details from inside a tool

When a tool runs an agent, `capture()` inside it adds the agent's details to the call's event. It
takes the event's own keys:

```python
lsquare.capture({"question": question, "answer": answer, "toolCalls": tool_calls,
                 "usage": {"inputTokens": 812, "outputTokens": 240, "costUsd": 0.004}})
```

- Call it on the client you passed to `instrument()`. A capture on another client goes as an event
  of its own, not linked to the call.
- The first `capture()` before the tool returns becomes the call's `agent` part (one from
  middleware after your tool returned is a turn of its own, as is one from mcp 2.x middleware
  after a low-level server's handler returned), and its `orgId`, `userId` and `conversationId`
  fill the call's: an `orgId` there that isn't a string drops the call's event as `invalid_id`,
  as it would anywhere. Any other capture, before or after the tool returns, goes as its own
  `turn` event, linked by `callId` (the call's, unless you pass one: left out, `None` and `""`
  don't count), and one of those whose `orgId` isn't a string drops only that turn;
  `lsquare.current_call_id()` gives you the ID inside a tool, for a capture made later elsewhere.
  One made before the tool returns is waited for with the call's event, by `wait_until` and by a
  reply that waits, up to 100 of a call's still being sent at once.
- Its data is read when you call it, each field once, though the call's event is built once the
  tool returns: what the tool changes afterwards, or reuses for its next turn, changes nothing.
  Its top-level fields are read first, then the call's `agent` part is taken, then what they hold
  is shaped: a capture your data's own code makes as it's shaped (a lazy object or a mapping's
  `get` inside its `usage` or `toolCalls`) is a turn of its own. One made as a top-level field is
  read (your mapping's own `get`, a lazy object given as a field) comes first, so it can become
  the call's `agent` part, and this capture a turn linked to the call. A value of it that can't
  be read is left out, as anywhere, unless it's an ID, which is then invalid, as above; if shaping
  it fails outright (a tool-call list whose items can't be read), the call's event is dropped
  (`redaction_failed`), never read again. If a top-level field of it can't be read, that capture
  is dropped (`internal_error`), and the call goes without it.
- A sync tool's `capture()` is inside its call on every line, though mcp 2.x and fastmcp run sync
  tools in a worker thread. A thread your tool starts itself with `threading.Thread` doesn't carry
  the call by default (Python 3.14 can make it), and `ThreadPoolExecutor.submit` never does: pass
  `callId` there yourself.
- A tool that runs another tool (through the server's `call_tool()`, or by calling its function)
  is one call, the outer tool's: the other tool records nothing of its own on it, its raise
  included. So does a task or thread your tool started, also once your tool has returned and the
  call goes on (middleware awaiting that task, say), and a `call_tool()` from middleware or from a
  fastmcp 4.x extension's `intercept_tool_call`, before or after its `call_next`.
- `flush()` inside a tool doesn't wait on itself: it sends everything else that's queued, and the
  call's own event is sent before its reply, within `flush_timeout_ms`; with `wait_until`, the
  rest of the sending is still handed to it. With a `flush_timeout_ms` of 0 the reply waits for
  nothing, and `identify` keeps its second. It doesn't wait for reports to reach `on_error`, so
  an unrelated slow one never delays the reply. In an async tool use `await lsquare.aflush()`.

### Who the call is for: `identify`

```python
def identify(call):
    # call.tool_name, call.args, call.context, call.signal
    return {"orgId": ..., "userId": ..., "conversationId": ..., "metadata": {...}}

instrument(mcp, lsquare, identify=identify)
```

`identify` gets the tool name, the arguments as the client sent them (without the extras' inputs)
and the MCP SDK's request context (the session, the HTTP request), and returns the IDs you know, or
`None`. It may be a sync function, run on the client's own threads, or an async one, run on the
server's event loop; an async one needs asyncio, and on trio only a sync one runs. It runs alongside
the tool, never before it, so it adds no latency; the event waits for it at most a second after the
tool returns. At most 16 run at once (the client's `max_concurrent_hooks`); past that, it isn't
called, nor after `shutdown()`. Its `signal` is aborted at its deadline, or by `shutdown()`, if it is still running then
(`signal.aborted`, `signal.wait(timeout)`, `signal.add_listener(fn)`); once it has answered, it
never is. An answer that comes past its deadline counts as late even when the SDK's scheduler was
too busy to notice in time, or the SDK was still reading what the tool returned or raised. What it
returns is read and checked once, as it settles (or by the event's build, if that comes first):
changing that object afterwards changes neither the event nor the ID fields last known. An async
one left on a loop your app closes without running its tasks to their end (`run_until_complete()`,
then `close()`) fails, and gives its place among the 16 back, as the next one starts. At its
deadline, or once a check of what it returned that was still reading it (slow getters) ends, the
call's event is built on the call's own loop; a client thread builds it 0.1 s later if that loop
has stopped or is blocked, and that loop keeps nothing of the call meanwhile.

IDs from a `capture()` inside the call win over `identify`'s. If `identify` raises, takes longer,
is skipped at the limit or returns IDs that aren't valid strings or UUIDs, the call keeps its full event only
when a `capture()` inside it gave every ID field `identify` returned the last time it succeeded (a
`userId`, before it ever has). Otherwise it is sent as limited diagnostics: its ID, the tool's name,
its status, duration and error category, and nothing else. That keeps your call counts and error
rates complete, but these events aren't anonymous (their time and `callId` can be matched with other
records), and they can't be deleted by person. Each failure is reported to `on_error`, as
`identify_failed`, once the client has accepted the call's event: a call whose event `shutdown()`
drops (one made after it, one in flight then, still waiting for `identify`, one whose own tool shut
the client down, let go of as it returns, nothing kept for it nor counted pending though `identify`
still runs, or one your own code shuts the client down from as its event is built) is reported only
as the client's one `shutdown`, since it isn't sent, nor is that event's build failing then reported.

On stateful HTTP, mcp 1.x and fastmcp 3.x run a session's handlers in the context of the request
that opened the session, so context variables your ASGI middleware sets (auth, tenants) hold the
first request's values there. Read the request from `call.context` (`call.context.request`) instead.

## The extras

Each extra can be turned off by itself (`extras={"intent": False}`), and `extras=False` turns all
three off. Any other value that isn't a mapping, a list or a function included, is ignored, with one
warning, and each extra keeps its default.

### `session_id`: calls grouped into tasks

Every tool gets an optional `session_id` input, described to the agent as: "The ID of the task this
call belongs to. Leave it out on your first call. On every later call for the same task, send the ID
an earlier result gave you, and pass it to any subagent working on that task."

- A call with a valid ID (`ses_` and 22 letters, digits, `-` or `_`) is recorded with it. Any other
  value is taken out and ignored. The transport's own ID, sent back on its own connection on a protocol
  version before 2026-07-28, is recorded as its hash; a server whose session IDs have this format should keep them from agents, as one sent
  back any other way is recorded as it is.
- A call whose tool called `capture()` with a `conversationId` is grouped by that.
- A call from a client on a protocol version before 2026-07-28 is grouped by its protocol session:
  a stateful HTTP server's `Mcp-Session-Id`, one ID per SSE connection (an `mcp-session-id` header
  sent over SSE is ignored), or one ID per stdio connection of a server run by its own
  `run_stdio_async()` (or fastmcp's `run()`); another server you serve from inside that run is
  not stdio. A stateless server has none, and neither has a low-level server you drive with your
  own `stdio_server()`.
- Any other call gets a new ID, in a text block at the end of the tool's own result: "Task ID:
  ses_…. Send it as session_id on later calls for this task, and pass it to any subagent working on
  it." The block goes at the end, so code that reads the first block still gets your tool's output,
  and the recorded result is your tool's, without it. Raised errors, results the SDK made instead
  (refused arguments, an output it couldn't convert), task acknowledgements and `input_required`
  rounds never get one.
- A result some middleware replaced gets no ID line: middleware that let the tool run and returned
  another result, or another `content` list (fastmcp's middleware included). One that edits a
  content block in place isn't noticed.
- Clients that read only `structuredContent` don't see the text block, so they never learn an ID
  to send back; their calls are still grouped by protocol session or `conversationId`. All four
  Python SDK lines give a tool with an ordinary return annotation, such as `-> str`, an output
  schema by default, and its results a `structuredContent`; Claude Code then shows its model only
  that ([in the README](README.md#what-agents-do-with-them)). For a tool that returns a string,
  like the quickstart's, `structured_output=False` on mcp's `tool()`, or `output_schema=None` on
  fastmcp's, prevents it.
- Agents decide for themselves whether to send the ID back: [what agents do with
  them](README.md#what-agents-do-with-them) says what the ones checked so far did.

### `intent`: why each call was made

Every tool gets an optional `intent` input, described as: "In one sentence, why you're calling this
tool and what the user is trying to do. No personal data or secrets." When it's a non-empty string
it's recorded, redacted and cut at 500 characters, whether or not the call's inputs are; anything
else sent as `intent` is taken out and ignored. It stays optional, so strict clients never refuse a
call that leaves it out.

### `get_more_tools`: what agents looked for

A server with at least one tool of its own gets one more tool, `get_more_tools`, marked as not
read-only, since what the agent writes leaves the server, with a required `need` input (and the
optional `session_id`): "Call this when none of the available tools fits what you need, or one fits
only in part. It doesn't return new tools: it tells the server's developers what to add." The
wrapper answers it ("Noted. No other tools are available right now.") and records the request as a
`tool_call` event with `extra: "missing_tools"`. With `inputs` off for it, the server doesn't get
the tool, since the request wouldn't be recorded. It's added to the last page of a paged list. A
server that registers its own `get_more_tools` keeps it, and doesn't get LSquare's.

### Which tools get them

The wrapper reads each tool's definition as the server holds it, at every listing and call.

- A tool that already has an input called `session_id` or `intent` (by name, alias or validation
  alias) keeps it, untouched, and gets only the other. Each case is logged once.
- A tool gets the extras only when the SDK's validator for its arguments drops or refuses keys it
  doesn't declare, and nothing in it can read them: a plain pydantic model of fields (the one the
  SDK makes from your function's signature, or yours with `extra` set to `ignore` or `forbid`), or,
  on fastmcp, its function's own signature without `**kwargs`. A model with a model validator, its
  own `__init__` or `model_validate`, extra keys allowed, or a custom schema, could read any key, so
  that tool is left without the extras, logged once, and grouped only by its protocol session or
  `conversationId`.
- On fastmcp only plain `FunctionTool`s are read, and only while the server's tools are all its
  own: tools from mounted servers, proxies, OpenAPI providers or transforms (other than fastmcp's
  own enable and disable), tools injected by `ToolInjectionMiddleware`, and versions of a tool that
  disagree about these inputs leave those calls exactly as sent. The wrapper can't see those tools'
  functions run, so their errors, raised or returned, are recorded as the tool's own
  (`tool_error`), except fastmcp's refusals (an unknown tool, refused arguments), and errors made
  after the tool returned a success (by middleware, or an output the SDK refused), which are
  `protocol`. A tool of theirs whose body raises fastmcp's `AuthorizationError` or
  `ValidationError` (on fastmcp 3.x, pydantic's `ValidationError` too) is recorded as a refusal,
  since fastmcp passes those on as they are. A mounted server's own middleware runs unseen unless
  you instrument that server with the same client too: a `capture()` from it then counts as
  before the tool returned. A server with more than 10,000
  registered components (tools, resources and prompts together) gets none of the extras. Middleware
  of yours that answers a call with a tool of its own sees the arguments without the extras.
- A tool's listing shows an input only where its JSON Schema can take one more optional property
  without changing what it accepts: not, for instance, with a combinator, `const`, `enum`, a limit on
  the number of properties, or a recursive schema. The inputs are still taken out of its calls.

### Low-level servers

A low-level `Server` has no tool definitions the wrapper can read, so the extras are off. Turn each
on by passing it as `True`:

```python
instrument(server, lsquare, extras={"sessions": True, "intent": True, "missing_tools": True})
```

Do this only for a server whose instances list and serve their own tools, and list the same tools
to every request they serve, such as a stdio server, or a stateful one with an instance per session.
One request's listing decides another's calls, so don't turn them on for a server built per request,
or for one instance that lists different tools to different users.

- The wrapper calls your `tools/list` handler itself, every page, before changing anything (at most
  100 pages and 10,000 tools: a longer list leaves the extras off), in a copy of the context of the
  request that started the read, marked as a listing; its session and send functions are still that
  request's. It keeps what it lists until your server sends `notifications/tools/list_changed`, you
  replace the handler, or a client's listing shows a change. A tool removed from a list of several
  pages may go unnoticed until then. On mcp 1.x a session's notifications are seen from its first
  `tools/list` or `tools/call`; a change sent before that is caught when a client next lists.
- A tool gets the inputs only when its listed schema is a plain object of named properties
  (`additionalProperties` absent or `False`) that doesn't name them. A low-level handler can read any
  key, so by turning the extras on you let the listing speak for the tool: an input it doesn't name
  is taken out of the call.
- A call waits for that list at most 1 second after the wrapper started reading it. Past that, the
  call is passed on unchanged, and a strict tool may then refuse an `intent` an agent sends from an
  earlier listing. A read is given up after 10 seconds, whatever your handler does, and after one
  fails, none starts for 60 seconds unless your server sends `list_changed`.
- On mcp 2.x, a request on the 2026-07-28 protocol is passed on unchanged: change notifications go
  only to streams the wrapper can't see there.
- On mcp 1.x, the SDK turns a raised error, a failed input check and a failed output check alike
  into an `isError` result before the wrapper sees it: all are recorded as `tool_error`, and none
  gets the task ID line.

### A replaced `tools/call` handler

A high-level server whose `tools/call` handler you replace after `instrument()` (a later
`@server.call_tool()`, or a write to `request_handlers`, `|=` included, on mcp 1.x;
`add_request_handler("tools/call", ...)` on 2.x) is treated as a low-level one from then on: only the extras passed as `True` stay on, read from its tool list as
above, its next listing included. One replaced before `instrument()` can't be told from the SDK's own
handler, so call `instrument()` first, or turn the extras off. A handler set after `instrument()`
that calls the one it replaced, such as middleware, is still one call: each `tools/call` is recorded
once, with one `identify` (on mcp 1.x and fastmcp 3.x, where such a handler takes the same request
context; on 2.x the wrapper is middleware itself, outside any handler).

## Serverless hosts and shutting down

By default a call's event is sent by the client's own threads after the reply. On a host that stops
your process or its CPU once the reply is sent, pass one of:

- `wait_until`: a callable taking an awaitable, such as Vercel's ([below](#vercel)). Each call's
  sending (its event, and the turns captured inside it before the tool returned) is handed to it
  as a coroutine that ends once they're sent or given up, without delaying the reply, however the
  call ends, a cancelled one included. The client's threads send the event either way: the
  coroutine only waits for that, so a host may start it after the reply.
- `flush_before_reply=True`, for hosts that stop the CPU after the reply, such as AWS Lambda or
  Cloud Run without always-on CPU: each call waits for its event to be sent, up to
  `flush_timeout_ms` (2 seconds by default, `identify` included; with 0 the reply doesn't wait, and
  `identify` keeps its second), before replying or raising its
  handler's error again (a cancellation isn't held up), so each reply
  waits for one request to the ingestion, usually a short one. Past `flush_timeout_ms` the reply
  goes anyway, and the event is sent only if the process keeps running.

If both are set, `wait_until` is used, with one warning. Where neither is set, the wrapper warns once
per client on hosts it recognises: AWS Lambda, Cloud Run and Vercel. Cloudflare Python Workers have
no threads or sockets: the client stays disabled there, with one warning.

### FastAPI and Starlette

Mount the server's Streamable HTTP app, run its session manager from your app's lifespan (FastAPI
and Starlette don't run a mounted app's own), and shut the client down once the server has stopped:

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI

mcp_app = mcp.streamable_http_app()        # fastmcp: mcp_app = mcp.http_app()

@asynccontextmanager
async def lifespan(app):
    async with mcp.session_manager.run():   # fastmcp: async with mcp_app.lifespan(app):
        yield
    await lsquare.ashutdown()               # sends what's queued, within flush_timeout_ms

api = FastAPI(lifespan=lifespan)
api.mount("/orders", mcp_app)               # the MCP endpoint is /orders/mcp
```

For a stateless server pass `stateless_http=True`: to `FastMCP()` on mcp 1.x, to
`streamable_http_app()` on mcp 2.x, to `http_app()` on fastmcp. Neither a request nor an HTTP
session ending flushes: the lifespan's `ashutdown()` is what sends the events still queued when the
server stops. With `before_send`, `await lsquare.aflush()` before it: `ashutdown()` drops an event
still waiting for the hook, as `shutdown`.

### AWS Lambda, with Mangum

Mangum runs your app's lifespan around every invocation, so the recipe above breaks on the second
warm invocation: a session manager runs only once (mcp 1.x and 2.x refuse to start it again), and a
client shut down in the first invocation's lifespan drops the events of every later one. On Lambda,
keep the client at module level and never shut it down, serve statelessly with a new session
manager for each invocation, and pass `flush_before_reply=True`, so each call's event is sent while
its invocation still runs. On fastmcp, `http_app()` makes a new session manager each time its
lifespan runs, so Mangum can run it as it is:

```python
from mangum import Mangum

lsquare = LSquare(api_key=os.environ.get("LSQUARE_KEY"))   # at module level, never shut down
mcp = FastMCP("orders")                                      # fastmcp 3.x or 4.x
# register tools...
instrument(mcp, lsquare, flush_before_reply=True)
handler = Mangum(mcp.http_app(stateless_http=True, json_response=True))   # the endpoint is /mcp
```

On mcp 2.x each `streamable_http_app()` makes a new session manager, so make the app for each
invocation, with Mangum's lifespan off. mcp also refuses a `Host` header other than localhost's
(421) unless you name it, as for API Gateway:

```python
from mangum import Mangum
from mcp.server.transport_security import TransportSecuritySettings

security = TransportSecuritySettings(allowed_hosts=["abc123.execute-api.eu-west-1.amazonaws.com"])
mcp = MCPServer("orders")
# register tools...
instrument(mcp, lsquare, flush_before_reply=True)

async def app(scope, receive, send):
    asgi = mcp.streamable_http_app(stateless_http=True, json_response=True, transport_security=security)
    async with mcp.session_manager.run():
        await asgi(scope, receive, send)

handler = Mangum(app, lifespan="off")
```

On mcp 1.x, `FastMCP` keeps the one session manager it made, so build the server for each
invocation (the wrapper warns once per client, not once per server):

```python
def build():                                # security as above
    mcp = FastMCP("orders", stateless_http=True, json_response=True, transport_security=security)
    mcp.tool()(search_orders)
    return instrument(mcp, lsquare, flush_before_reply=True)

async def app(scope, receive, send):
    mcp = build()
    asgi = mcp.streamable_http_app()
    async with mcp.session_manager.run():
        await asgi(scope, receive, send)

handler = Mangum(app, lifespan="off")
```

### Vercel

Vercel's Python runtime serves your ASGI app as it is, and runs its lifespan as a server does, so
the [FastAPI recipe](#fastapi-and-starlette) works there. Pass the `wait_until` of Vercel's `vercel`
package: the invocation then keeps running after the reply until each call's event is sent.

```python
from vercel.functions import wait_until

instrument(mcp, lsquare, wait_until=wait_until)
```

Serve statelessly (`stateless_http=True`). `wait_until` finds the current request through a context
variable, and mcp 1.x and fastmcp 3.x run a stateful session's calls in the context of the request
that opened the session: the hand-off goes to that request, long finished, so it's refused and
nothing keeps the invocation running for the event. LSquare's tests check the app, the warning
and the hand-off against a stand-in for Vercel's runtime, not Vercel itself: how long an instance
keeps running work after a reply is up to Vercel, within the function's maximum duration. Vercel
gives cleanup at shutdown at most 500 ms, so there it's `wait_until`, not the lifespan's
`ashutdown()`, that sees each event sent.

### Sending what's queued when the process ends

- **A stdio server run by its own `run_stdio_async()`** (or `mcp.run()`, or fastmcp's `run()`)
  flushes when its client closes it, within `flush_timeout_ms`, before the event loop ends.
- **A low-level server you drive with `stdio_server()`**: await `lsquare.ashutdown()` after
  `server.run()` returns.
- **An HTTP app** (the SDK's own runner, or mounted in Starlette, FastAPI or any ASGI server): call
  `await lsquare.ashutdown()` in its lifespan's shutdown ([above](#fastapi-and-starlette)), except
  on Lambda, where Mangum runs the lifespan for each invocation ([above](#aws-lambda-with-mangum)).
  Neither a request nor an HTTP session ending flushes.
- **A script ending with `asyncio.run()`**: `await lsquare.ashutdown()` before it returns, after
  `await lsquare.aflush()` if you set `before_send`: `ashutdown()` drops an event still waiting for
  the hook, and pending async hooks are cancelled once the loop ends.
- At interpreter exit, an `atexit` handler sends what's still queued, within `flush_timeout_ms`
  (2 s when that is `0`, as `shutdown()` waits, so an exit with events queued can take 2 s), then
  waits up to half a second more for the SDK's own log lines to be written. On a Python that
  refuses to start threads once the main thread has finished (3.12.2 does), it sends from the
  exiting thread what no thread can, a retry due before the timeout included; so does a sync
  `shutdown()` in an `atexit` handler of your own, but not `ashutdown()`. On such a Python a server
  kept running by a non-daemon thread after the main thread returns can't start the SDK's threads
  again once they have gone idle: its events wait in the queue until the exit sends them, except
  that with `before_send` an event whose hook no thread can run, or whose async hook's result no
  thread can read, is dropped as `hook_failed` ("No thread could start for before_send", "No
  thread could start to read what before_send returned"), never sent unprocessed nor read on your
  event loop; so is one whenever the system refuses to start a thread, a sync `shutdown()` from
  another thread waiting for that server's calls included. Only the client's own exit flush runs
  such a hook, or reads what it returned, on the exiting thread; a `shutdown()` of yours, in an
  `atexit` handler too, drops that event as `hook_failed`. Keep the main thread alive there,
  joining the server's thread. An `atexit` handler you register before creating the client runs
  after its exit flush; a sync `shutdown()` there still sends within its own timeout.
  It doesn't run on `os._exit()`, `SIGKILL` or a signal's default action. So on a synchronous
  server, make `SIGTERM` exit, and let the exit send what's queued:

  ```python
  import signal, sys
  signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
  ```

  That is best effort: a capture the signal interrupts may be lost, and the interpreter first
  waits for your own non-daemon threads, which can hold up reaching the exit's flush; once there,
  it takes at most `flush_timeout_ms`, plus up to half a second for the SDK's log lines and
  callbacks. On an asyncio server, call `await lsquare.ashutdown()` in your framework's shutdown
  or lifespan hook instead ([above](#fastapi-and-starlette)).
- Don't call `shutdown()`, `flush()` or `capture()` inside a signal handler. Python runs the
  handler on the main thread between two steps of whatever runs there, the SDK's own code
  included, so the call can interrupt the SDK in the middle of one of its own steps; the SDK
  doesn't promise to finish it then, or to return promptly. The handler's `SystemExit`, or a
  `KeyboardInterrupt`, raised on the main thread goes on through the SDK, never swallowed, also
  as a client, or a session or stream the MCP wrapper keeps an ID for, is collected (no code of
  the SDK's runs then); so does one your own hooks, logger or `on_error` raise there. Raised on another thread, an async hook's
  or callback's on a loop running there or on the SDK's private loop included, it is that hook's or
  callback's failure, like any other exception: the loop goes on.

`flush()` and `shutdown()` block the calling thread; on an event loop use `await lsquare.aflush()`
and `await lsquare.ashutdown()`. Given time to wait, they return once what they reported, and the
reports about the events they waited for, have reached `on_error` or the logger (an async one
started), waiting up to half a second more for that, and not again while a sync handler stays
stuck (under a test that freezes the monotonic clock, each waits its half second); inside a tool
call they don't wait for this. `wait_until`'s coroutine ends the same way. An async handler's
own I/O isn't waited for.
`shutdown()` counts a call whose event still waits for `identify` as failed, lets go of that call
and its result as it ends, and reports it once, as `shutdown`. It drops an event still waiting for
`before_send` the same way, failed and reported as `shutdown`, unless a `flush()` first (`aflush()`
on an event loop) outlasts the hook.
After `os.fork()`, the child starts with an empty client, a disabled one included, and the parent
sends what it had. A client made in the child is the child's, also one your own `after_in_child`
callback makes before the SDK's runs: it keeps what it captures there. The MCP wrapper's own locks
start free in the child too, whatever thread of the parent's held them.

## Agent turns outside MCP

```python
lsquare = LSquare(api_key=os.environ.get("LSQUARE_KEY"))  # no key: disabled, never raises

# After an agent turn
lsquare.capture({
    "orgId": org_id, "userId": user_id, "conversationId": conversation_id, "turnId": turn_id,
    "question": question, "answer": answer, "toolCalls": tool_calls,
    "usage": {"inputTokens": 812, "outputTokens": 240, "costUsd": 0.004},
})
```

Every field is optional. A turn with a `conversationId` and a `turnId` gets the same event ID every
time it's sent, so a retry or a re-import is recognised as a duplicate. IDs (`orgId`, `userId`,
`conversationId`, `turnId`, `callId`, `apiKeyId`) are strings, or a `uuid.UUID`, sent as its string:
lowercase with hyphens, so the event is the one `str(uuid)` would make, and the UUID's `.hex` or an
uppercase string is a different ID. `turnId` may also be a safe integer.

## Sending

Events go in batches of up to 100, at most 4 requests at once, from the client's own daemon
threads; nothing waits on the network in your code. If your project reaches its daily limit, the
SDK stops sending until the limit resets (00:00 UTC), checks every 15 minutes whether it was raised,
and reports through `on_error` when it starts dropping events and how many it dropped.
