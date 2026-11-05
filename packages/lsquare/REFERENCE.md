# lsquare: reference

Everything the [README](README.md) leaves out, for when you need it: the details of each part
of the SDK, the extras on every kind of server, and serverless hosts.

## MCP servers

### The calling app

The calling app is the name the MCP client gives itself. A client on a protocol version before
2026-07-28 gives it only in `initialize`, so on a stateless HTTP server, where `initialize` and each
tool call are served by separate server instances, its calls are recorded without the calling app.
That covers SDK 2.x's `createMcpHandler` for those clients, Cloudflare's `createMcpHandler`,
Vercel's `mcp-handler`, and SDK 1.x without a `sessionIdGenerator`. Their protocol version comes
from the request's `MCP-Protocol-Version` header, when the client sends it. `identify` gets the HTTP
request and can return its `User-Agent` in `metadata`, but that is a separate value the client
doesn't vouch for, often naming its HTTP library, and it isn't recorded as the calling app.

### Agent details from inside a tool

When a tool runs an agent, `capture()` inside it adds the agent's details to the call's event:

```ts
lsquare.capture({ question, answer, toolCalls, usage: { inputTokens, outputTokens, costUsd } })
```

- Call it on the client you passed to `instrument()`. A capture on another client goes as an event
  of its own, not linked to the call.
- The first `capture()` before the tool returns becomes the call's `agent` part, and its
  `orgId`, `userId` and `conversationId` fill the call's: an `orgId` there that isn't a string
  drops the call's event as `invalid_id`, as it would anywhere. Any other capture, before or after
  the tool returns, goes as its own `turn` event, linked by `callId` (the call's, unless you pass
  one: left out, `null` and `''` don't count), and one of those whose `orgId` isn't a string drops
  only that turn; `lsquare.currentCallId()` gives you the ID inside a tool, for a capture made
  later elsewhere. One made before the tool returns
  is waited for with the call's event, by `waitUntil` and by a reply that waits, up to 100 of a
  call's still being sent at once. The wrapper sees an async tool's return one promise reaction
  after it (a low-level handler's, through the MCP SDK's own async wrapper), so a capture the tool
  queued as a microtask just before returning still counts as before it.
- Its data is read when you call it, though the call's event is built once the tool returns: what
  the tool changes afterwards, or reuses for its next turn, changes nothing. Its top-level fields
  are read first, then the call's `agent` part is taken, then what they hold is shaped: a capture
  your data's own code makes as it's shaped (a getter inside its `usage` or `toolCalls`, a
  `toJSON`) is a turn of its own. One made by a getter of the object you pass comes first, so it
  can become the call's `agent` part, and this capture a turn linked to the call. If a getter
  throws as it's read, the call's event is dropped (`redaction_failed`), never read again; if a
  top-level field throws, that capture is (`internal_error`), and the call goes without it.
- A tool that runs another registered tool's handler itself is one call, the outer tool's: the
  other tool records nothing of its own on it, its throw included. So does work your tool left
  going, a promise continuation it chained, that runs another tool's handler once your tool has
  returned while the call goes on.
- `flush()` inside a tool doesn't wait on itself: it sends everything else that's queued, and the
  call's own event is sent before its reply, within `flushTimeoutMs`. If the ingestion doesn't
  answer by then, the reply goes anyway; with `waitUntil`, the rest of the sending is still
  handed to it. With a `flushTimeoutMs` of 0 the reply waits for nothing, and `identify` keeps its
  second.

### Who the call is for: `identify`

```ts
instrument(server, lsquare, {
  identify: async ({ toolName, args, context, signal }) => ({ orgId, userId, conversationId, metadata }),
})
```

`identify` gets the tool name, the arguments as the client sent them (without the extras' inputs)
and the MCP SDK's request context (auth info, the HTTP request), and returns the IDs you know. It
runs alongside the tool, never before it, so it adds no latency; the event waits for it at most a
second after the tool returns. At most 16 run at once (the client's `maxConcurrentHooks`); past
that, it isn't called, nor after `shutdown()`. It gets an `AbortSignal` that fires at its deadline if it is still running
then; once it has answered, it never fires. An answer that comes past its deadline counts as late
even when the event loop was too busy to notice in time. As with any `AbortSignal`, an abort
listener's error is Node's to report, as an uncaught exception, so a listener must not throw.

IDs from a `capture()` inside the call win over `identify`'s. If `identify` throws, takes longer,
is skipped at the limit or returns IDs that aren't valid, the call keeps its full event only when a
`capture()` inside it gave every ID field `identify` returned the last time it succeeded (a
`userId`, before it ever has). Otherwise it is sent as limited diagnostics: its ID, the tool's name,
its status, duration and error category, and nothing else. That keeps your call
counts and error rates complete, but these events aren't anonymous (their time and `callId` can be
matched with other records), and they can't be deleted by person. Each failure is reported to
`onError`, as `identify_failed`, once the client has accepted the call's event: a call whose event
`shutdown()` drops (one made after it, one in flight then whose `identify` fails afterwards, or one
your own code shuts the client down from as its event is built) is reported only as the client's
one `shutdown`, since it isn't sent, nor is that event's build failing then reported.

## The extras

Each extra can be turned off by itself (`extras: { intent: false }`), and `extras: false` turns all
three off. Any other value that isn't an object, a list or a function included, is ignored, with one
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
  a stateful HTTP server's `Mcp-Session-Id`, an SSE connection's own ID, or one ID per stdio
  connection, `serveStdio()` included. A stateless server has none.
- Any other call gets a new ID, in a text block at the end of the tool's own result, when the
  result has a `content` list: "Task ID:
  ses_…. Send it as session_id on later calls for this task, and pass it to any subagent working on
  it." The block goes at the end, so code that reads the first block still gets your tool's output,
  and the recorded result is your tool's, without it. Thrown errors, results the SDK made instead
  (refused arguments, say), task acknowledgements and `input_required` rounds never get one.
- Rounds of a multi-round call aren't linked to each other: each is grouped like any call.
- Clients that read only `structuredContent` don't see the text block, so they never learn an ID
  to send back; their calls are still grouped by protocol session or `conversationId`.
- Agents decide for themselves whether to send the ID back: [what agents do with
  them](README.md#what-agents-do-with-them) says what the ones checked so far did.

### `intent`: why each call was made

Every tool gets an optional `intent` input, described as: "In one sentence, why you're calling this
tool and what the user is trying to do. No personal data or secrets." When it's a non-empty string
it's recorded, redacted and cut at 500 characters, whether or not the call's inputs are; anything
else sent as `intent` is taken out and ignored. It stays optional, so strict clients never refuse a
call that leaves it out.

### `get_more_tools`: what agents looked for

A server with at least one enabled tool of its own gets one more tool, `get_more_tools`, marked as
not read-only, since what the agent writes leaves the server, with a required `need` input (and the
optional `session_id`): "Call this when none of the available tools fits what you need, or one fits
only in part. It doesn't return new tools: it tells the server's developers what to add." The
wrapper answers it ("Noted. No other tools are available right now.") and records the request as a
`tool_call` event with `extra: "missing_tools"`. With `inputs` off for it, the server doesn't get
the tool, since the request wouldn't be recorded. It's added to the last page of a paged list.
Clients that cap the number of tools across servers count it. A server that registers its own
`get_more_tools`, even a disabled one, keeps it, and doesn't get LSquare's.

### Which tools get them

On a high-level server the wrapper reads each tool's own definition, at every listing and call, so
servers built per request with different tools per customer are each read on their own.

- A tool that already has an input called `session_id` or `intent` keeps it, untouched, and gets
  only the other. Each case is logged once.
- Only zod schemas (zod 3 and 4, full and mini) are read, and only plain objects that drop keys they
  don't declare (refined or not), or that refuse them (not refined). Raw shapes count, and a tool
  without an input schema gets the extras. Any other schema, such as a preprocess, a transform,
  `lazy`, a union, an intersection, a record, an object that keeps unknown keys, a refined object
  that refuses them or a schema from another library, could read or need any key, so that tool is
  left without the extras, logged once, and grouped only by its protocol session or
  `conversationId`.
- A tool's listing shows an input only where its JSON Schema can take one more optional property
  without changing what it accepts: not, for instance, with a combinator, `const`, `enum`, a limit on
  the number of properties, or a recursive schema. The inputs are still taken out of its calls.

### Low-level servers

A low-level `Server` has no tool definitions the wrapper can read, so the extras are off. Turn each
on by passing it as `true`:

```ts
instrument(server, lsquare, { extras: { sessions: true, intent: true, missingTools: true } })
```

Do this only for a server whose instances list and serve their own tools, and list the same tools
to every request they serve, such as a stdio server, or a stateful one with an instance per session.
One request's listing decides another's calls, so don't turn them on for a server built per request,
or for one instance that lists different tools to different users.

- The wrapper calls your `tools/list` handler itself, every page, before changing anything (at most
  100 pages and 10,000 tools: a longer list leaves the extras off), and keeps what it lists until your server sends `notifications/tools/list_changed`, you replace the handler,
  or a client's listing shows a change. A tool removed from a list of several pages may go unnoticed
  until then. Each page gets a signal of its own, which a change to the list aborts only while that
  page is being read.
- A tool gets the inputs only when its listed schema is a plain object of named properties
  (`additionalProperties` absent or `false`) that doesn't name them. A low-level handler can read any
  key, so by turning the extras on you let the listing speak for the tool: an input it doesn't name
  is taken out of the call.
- A call waits for that list at most 1 second after the wrapper started reading it. Past that, the
  call is passed on unchanged, and a strict tool may then refuse an `intent` an agent sends from an
  earlier listing. A read is given up after 10 seconds, and after one fails, none starts for 60
  seconds unless your server sends `list_changed`.

### A replaced `tools/call` handler

A high-level server whose `tools/call` handler you replace is treated as a low-level one: only the
extras passed as `true` stay on, read from its tool list as above. The wrapper notices a replacement
made after `instrument()`. One made before can't be told from McpServer's own handler, so call
`instrument()` first, or turn the extras off. A handler set after `instrument()` that calls the one
it replaced, such as middleware, is still one call: each `tools/call` is recorded once, with one
`identify`.

## Serverless hosts and frameworks

By default a call's event is sent in the background after the reply. On a host that stops your
process or its CPU once the reply is sent, pass one of:

- `waitUntil`: each call's sending (its event, and the turns captured inside it before the tool
  returned) is handed to it, however the call ends, without delaying the reply. On Cloudflare
  Workers, with Cloudflare's Agents SDK, instrument each server inside the factory, where the
  request's `ctx` is at hand:

  ```ts
  import { McpServer } from '@modelcontextprotocol/server'  // SDK 2.x, which createMcpHandler serves
  import { createMcpHandler } from 'agents/mcp/server'

  export default {
    fetch(request, env, ctx) {
      return createMcpHandler(() => {
        const server = new McpServer({ name: 'orders', version: '1.0.0' })
        // register tools...
        return instrument(server, lsquare, { waitUntil: ctx.waitUntil.bind(ctx) })
      })(request, env, ctx)
    },
  }
  ```

  With Vercel's `mcp-handler`, `waitUntil` from `@vercel/functions` finds the current request
  itself:

  ```ts
  import { waitUntil } from '@vercel/functions'
  import { createMcpHandler } from 'mcp-handler'

  const handler = createMcpHandler((server) => {
    // register tools...
    instrument(server, lsquare, { waitUntil })
  })
  export { handler as GET, handler as POST }
  ```

  A server built once and reused across requests needs a function that finds the current request's
  `waitUntil`, for instance through `AsyncLocalStorage`:

  ```ts
  import { AsyncLocalStorage } from 'node:async_hooks'

  const requests = new AsyncLocalStorage<ExecutionContext>()
  instrument(server, lsquare, { waitUntil: (promise) => requests.getStore()?.waitUntil(promise) })

  export default {
    fetch: (request, env, ctx) => requests.run(ctx, () => handle(request, env, ctx)),
  }
  ```

- `flushBeforeReply: true`, for hosts without `waitUntil` that stop the CPU after the reply, such as
  AWS Lambda or Cloud Run without always-on CPU: each call waits for its event to be sent, up to
  `flushTimeoutMs` (2 seconds by default, `identify` included; with 0 the reply doesn't wait, and
  `identify` keeps its second), before replying or passing on the error its handler threw. Every
  call then takes as long as sending its event, usually one short request, longer.

If both are set, `waitUntil` is used, with one warning. Where neither is set, the wrapper warns once
on hosts it recognises: AWS Lambda, Cloud Run, Vercel and Cloudflare Workers.

A server in a Durable Object, such as Cloudflare's `McpAgent`, needs neither: Durable Objects stay
active while requests or other I/O are pending. The wrapper can't tell a Durable Object from a plain
Worker, so it still warns there; pass the object's own `waitUntil` to silence it:

```ts
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'  // McpAgent serves SDK 1.x only
import { McpAgent } from 'agents/mcp'

export class OrdersAgent extends McpAgent {
  server = new McpServer({ name: 'orders', version: '1.0.0' })
  async init() {
    // register tools...
    instrument(this.server, lsquare, { waitUntil: this.ctx.waitUntil.bind(this.ctx) })
  }
}
```

On a plain Worker, a client at the top of the file is shared by every request, yet Workers runs
nothing of one request from another. So each event's `beforeSend` and request start in the request
that captured it, while that request waits (`waitUntil`, or an awaited `flush()`), and the
events of requests that ended go with the next request sent. A report about an event reaches a
custom `onError` or `logger` only from that event's own request, and does so before what that
request waits on settles: the promise the wrapper hands to `waitUntil`, and `flush()` or
`shutdown()` handed to `ctx.waitUntil` or awaited. An async handler is started by then, but its
own I/O isn't waited for: hand what it starts to the request's `waitUntil` to keep it.

When a stdio server's client shuts it (closing its stdin), or the server closes, the wrapper flushes
what's queued, within `flushTimeoutMs`, and the process waits for that; a call still running then
is flushed once it ends. Servers on other transports, HTTP included, don't flush when they close:
stop them with `shutdown()`, as below. A client that kills the process outright still loses what's
left, so stop cleanly on `SIGTERM`:

```ts
process.on('SIGTERM', async () => {
  await lsquare.shutdown()
  process.exit(0)
})
```

Frameworks that hand you the official SDK's server are covered through it, and tested: Cloudflare's
`McpAgent` and `createMcpHandler` (`agents` 0.31), and Vercel's `mcp-handler` (2.3). Frameworks with their own
server class, such as FastMCP for TypeScript and Mastra, haven't been tried yet.

### Hooks on Workers

On Workers, a `beforeSend` hook can also outlive its own request, which takes the hook with it when
it ends: such a hook never settles. So there, unlike elsewhere, `maxConcurrentHooks` and the memory
limits stop counting a `beforeSend` hook that hasn't settled once its event is finished (at its
deadline, or before) and the hook's beat, a timer in its own request, has been still for 5 seconds;
the next capture, flush or hook that runs then drops it. The beat stops when the hook's request
ends, or by itself about a minute past the hook's deadline, even if the hook is still running in a
request that is alive or in a Durable Object (the SDK can't tell one from a plain Worker, and a
timer kept would keep the object awake). Such a hook may still hold its copy of the event until it
settles.

An async `onError` or custom logger can be cut off the same way, and at most 100 reports, and
100 log lines, are held at once until their handlers settle. So on Workers one that hasn't settled
gives its place back once its own beat has been still for 5 seconds: when its request has ended,
or about a minute after it started.

## Agent turns outside MCP

```ts
const lsquare = new LSquare({ apiKey: process.env.LSQUARE_KEY }) // no key: disabled, never throws

// After an agent turn
lsquare.capture({
  orgId, userId, conversationId, turnId,
  question, answer, toolCalls,
  usage: { inputTokens, outputTokens, costUsd },
})
```

Every field is optional. A turn with a `conversationId` and a `turnId` gets the same event ID every
time it's sent, so a retry or a re-import is recognised as a duplicate.

## Sending

Events go in batches of up to 100, at most 4 requests at once. If your project reaches its daily
limit, the SDK stops sending until the limit resets (00:00 UTC), checks every 15 minutes whether it
was raised, and reports through `onError` when it starts dropping events and how many it dropped.
