# lsquare

Record every tool call your MCP server handles, and the agent turns behind them, in LSquare.
Pre-release: LSquare is an invite-only beta, so [request access](https://lsquare.io) to get an API
key. During the beta, LSquare sends you a weekly report on your tool calls (the most used, failing
and slowest tools, missing tools and top intents), and exports on request.

- One line per MCP server. Nothing the SDK does can make a tool call fail, and it never throws.
- `capture()` never waits on the network, and `flush()` waits up to 2 seconds by default.
- Common private data is redacted, and long values shortened, before anything leaves your process.
- No runtime dependencies. Node 18+, Cloudflare Workers and Bun; ESM and CommonJS.
- Events are stored in the United States. If you serve customers in the EU, take that into account.

## Quickstart

A server with one tool, served over HTTP on your machine, and one call to it with `curl`.

1. **Install** `lsquare` and the MCP TypeScript SDK (2.x here; 1.x works too, [below](#mcp-servers)).
   This example needs Node 20 or later, as SDK 2.x does; `lsquare` itself runs on Node 18:

   ```sh
   npm install lsquare @modelcontextprotocol/server @modelcontextprotocol/node zod
   ```

2. **Instrument the server**, in `server.mjs`:

   ```js
   import { createServer } from 'node:http'
   import { createMcpHandler, McpServer } from '@modelcontextprotocol/server'
   import { toNodeHandler } from '@modelcontextprotocol/node'
   import { z } from 'zod'
   import { LSquare } from 'lsquare'
   import { instrument } from 'lsquare/mcp'

   const lsquare = new LSquare({ apiKey: process.env.LSQUARE_KEY })

   // A new server for each request, as createMcpHandler serves it; instrument each one.
   function buildServer() {
     const server = new McpServer({ name: 'weather', version: '1.0.0' })
     server.registerTool(
       'get_forecast',
       { description: "Gives tomorrow's forecast for a city.", inputSchema: z.object({ city: z.string() }) },
       async ({ city }) => ({ content: [{ type: 'text', text: `Tomorrow in ${city}: sunny, 21°C.` }] }),
     )
     return instrument(server, lsquare)
   }

   createServer(toNodeHandler(createMcpHandler(buildServer))).listen(3000, () => {
     console.error('Listening on http://localhost:3000/mcp')
   })
   ```

3. **See the first event, in dry run**, where nothing is sent:

   ```sh
   LSQUARE_DRY_RUN=1 node server.mjs
   ```

   In another terminal, call the tool:

   ```sh
   curl -s http://localhost:3000/mcp -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_forecast","arguments":{"city":"Paris"}}}'
   ```

   The reply is your tool's result, then a text block with the call's task ID, "Task ID: ses_…"
   ([`session_id`](REFERENCE.md#session_id-calls-grouped-into-tasks), in the reference). The
   server's terminal shows a line for what `instrument()` did, and one for the call's event: its
   fields and size, never its content or IDs.

   ```
   [lsquare dry-run] {"server":"weather","level":"high","records":"every tools/call","extras":{…}}
   [lsquare dry-run] {"kind":"tool_call","eventId":"the call","fields":["callId","durationMs","input","result","server","sessionId","sessionSource","status","toolName"],"bytes":550}
   ```

4. **Send it.** Stop the server, start it again with your project key instead of dry run, and call
   the tool again:

   ```sh
   LSQUARE_KEY=lsq_your_key node server.mjs
   ```

   Nothing is printed when LSquare accepts an event. A problem is logged as one `[lsquare]` line,
   such as `[lsquare] send_failed: LSquare answered 401; not retried.` for a key it doesn't know.

Without a key, and outside dry run, the client is disabled: `capture()` does nothing and
`instrument()` leaves your servers exactly as they are, with one warning. Over stdio, the dry-run
lines go to the server's standard error, which MCP clients keep in their logs. Before deploying to
a host that stops your process once it has replied, read [Serverless hosts and
frameworks](REFERENCE.md#serverless-hosts-and-frameworks). An agent that isn't behind an MCP server records its
turns with `capture()`: [agent turns outside MCP](REFERENCE.md#agent-turns-outside-mcp).

## MCP servers

```ts
import { LSquare } from 'lsquare'
import { instrument } from 'lsquare/mcp'

const lsquare = new LSquare({ apiKey: process.env.LSQUARE_KEY })  // once, at the top of the file

const server = new McpServer({ name: 'orders', version: '1.0.0' })
server.registerTool('search_orders', { /* ... */ }, searchOrders)
instrument(server, lsquare)                                         // the one line per server
```

`instrument()` takes a high-level `McpServer` or a low-level `Server`, from the MCP TypeScript SDK
1.x (`@modelcontextprotocol/sdk`) or 2.x (`@modelcontextprotocol/server`). It returns the server it
was given, so it can be returned from a factory, and instrumenting a server twice does nothing the
second time. Tools registered after it are covered too. Instrument a server before connecting it:
a client that listed its tools before keeps a copy without the extras (below) until it lists them
again.

Each call that reaches the server's `tools/call` handler becomes one `tool_call` event: the tool's
name, its arguments (`input`), its result, how it ended (`status`, and an `error` with its
`category`: `thrown`, `tool_error`, `protocol` or `timeout`), how long it took, the calling app and
protocol version, and the IDs below. Results are sent by default, each string cut at 4,000
characters; turn inputs or results off everywhere, or for chosen tools, with `inputs` and `results`.
A failed call's text, in `error.message` and in an `isError` result, can quote what the tool was
sent and what it would have returned, so it's recorded only with both on. The event still says how
the call failed: `category`, `type` and `code`. A cancelled call's fixed sentence stays.
A call the SDK's HTTP layer refuses before dispatch, such as one on SDK 2.x whose `Mcp-Param`
headers don't match its arguments, never reaches the handler and makes no event.

### What the defaults change in your server

On a high-level `McpServer` three extras are on by default, so calls are grouped into tasks, each
call says why it was made, and agents can tell you what they couldn't find. They change what your
server shows its callers:

- every tool gains two optional inputs, `session_id` and `intent`, which the wrapper takes out of
  each call before the SDK checks it, so your code never sees them;
- some results gain one text block at the end, giving the agent a task ID;
- the server gains a tool, `get_more_tools`, which the wrapper answers itself.

Clients and scanners that flag changed tool definitions may warn your server's users once when you
add the SDK. Each extra can be turned off by itself, in your one `instrument()` call (a second call
on the same server changes nothing):

```ts
instrument(server, lsquare, {
  extras: {
    sessions: false,      // no session_id, no task ID line
    intent: false,        // no intent
    missingTools: false,  // no get_more_tools
  },                      // leave any of them out to keep it on
})
```

`extras: false` turns all three off at once. With all three off, the wrapper never changes
arguments, results or errors, and adds no waiting before your handler runs. On a low-level `Server`
they're off unless you turn them on ([in the reference](REFERENCE.md#low-level-servers)), whose
[extras](REFERENCE.md#the-extras) section says exactly what each one does.

### `instrument()` options

| Option | Default | |
|---|---|---|
| `identify` | | `({ toolName, args, context, signal }) => ({ orgId, userId, conversationId, metadata })` ([how it works](REFERENCE.md#who-the-call-is-for-identify)) |
| `inputs` | `true` | `false`, or `(toolName) => boolean`. Off for `get_more_tools`, the server doesn't get that tool |
| `results` | `true` | `false`, or `(toolName) => boolean` |
| `extras` | all on (high-level), all off (low-level) | `{ sessions, intent, missingTools }`; one left out keeps its default. `false`: all off |
| `waitUntil` | | The current request's `waitUntil`: each call's sending is handed to it ([serverless hosts](REFERENCE.md#serverless-hosts-and-frameworks)) |
| `flushBeforeReply` | `false` | Wait for each call's event to be sent, up to `flushTimeoutMs`, before replying |

In dry run, `instrument()` logs one line per server saying what the extras can change.

## The extras

How each extra works, which tools get them, and the extras on low-level servers are in the
[reference](REFERENCE.md#the-extras). Here is what agents did with them.

### What agents do with them

Checked on 29 October 2026 with Claude Code 2.1.312 (Claude Opus 5.5) and Codex CLI 0.165.0
(gpt-6-astra, high reasoning effort), each doing the same multi-step tasks on a sample shop server
with the default extras: on TypeScript (MCP SDK 2.x) and on Python (mcp 1.x), over stdio and over
stateless HTTP. Each task ran once on each of those, 26 runs per agent, so these say what each agent
did, not how often it would. Codex ran headless (`codex exec`, a new process for each turn of a
conversation). Cursor, Claude desktop and ChatGPT aren't checked yet, and nothing here is about them.

- **`intent`**: both agents filled it in on every call to the server's own tools (94 of 94 for
  Claude Code, 74 of 74 for Codex), and neither put the task's personal data in it, though one
  task's prompt gave a customer's name, email address and phone number.
- **`session_id`, over stateless HTTP**: once the model had seen a task's ID line, every later call
  it made for that task sent the ID back (Claude Code 24 of 24, Codex 22 of 22). Separately,
  subagents' calls sent their parent's ID (7 of 7, 6 of 6). When the conversation moved on to an
  unrelated task, neither sent the earlier task's ID.
- **Parallel first calls**: over HTTP, Claude Code often started a task with several calls at once,
  before any result had come back, so each got its own ID and the task was split across them (7 of
  16 tasks, counting an unrelated follow-up as a task of its own). Codex made one call first and
  sent its ID on the rest, and no task was split over HTTP.
- **Protocol versions**: Codex used 2025-06-18 everywhere; Claude Code used 2026-07-28 over HTTP on
  the TypeScript server and 2025-11-25 otherwise (over stdio, and against mcp 1.x, which has no
  2026-07-28). So over stdio neither got an ID line: their calls were grouped by the stdio
  connection, which puts everything sent over it together, whatever the task. Claude Code's
  subagents used their parent's connection; each of Codex's opened its own, so over stdio its task
  with subagents was split by connection. On a stateful HTTP server, a client on a version before
  2026-07-28 is grouped the same way, by its `Mcp-Session-Id`, without an ID line; these runs didn't
  try one.
- **`structuredContent`**: for a tool with an output schema, Claude Code showed the model only the
  structured result, so the ID line never reached it (0 of 2 sent back). Codex showed the text
  blocks as well and sent the ID back (2 of 2).
- **`get_more_tools`**: both called it in every run where no tool fitted, or one fitted only in
  part (8 of 8 each). Claude Code never called it otherwise. Codex did in 3 of the 18 runs where the
  tools fitted: each time, after confirming a customer had been charged twice and before answering,
  it asked for the payment-processor logs that would explain why. These runs were before the tool
  stopped being marked read-only, which a client may treat differently.

## What leaves your process

- Strings in a call's `input` and `agent`, and in a turn, are cut at 16,000 characters, results at
  4,000 and `intent` at 500; a cut string ends with `…[+N chars]`, or `…[cut]` once an event's work
  budget for reading strings is spent. Lists and objects keep their first
  100 entries, and nesting stops at 10 levels. Images, audio, binary resources and files sent as
  base64 text are kept as their size only.
- `capture()` reads its data before it returns, inside a tool call too: what your code does with its
  objects afterwards changes nothing. A getter that throws as it's read drops that event; inside a
  call, a top-level one drops that capture and the call goes without it, and one nested deeper
  drops the call's event. A call's arguments and result are read when its event is built.
- An event still over 256 KB loses parts, biggest and least needed first: a call its result, then its
  input, then its agent's tool calls, then its agent part; a turn its tool calls, then its answer,
  question, verification and latency. The event's `truncated` field names what was shortened or left out.
- Common formats of emails, phone numbers, card numbers, API keys and tokens, credentials in URLs,
  and IP addresses become numbered placeholders such as `[EMAIL_1]`, in inputs, results, errors,
  intents and agent details alike. Each pattern can be turned off: `redact: { ip: false }`, or
  `redact: false` for all.
- Values under secret key names (`password`, `token`, `apiKey` and the like) are replaced whole, in
  structured data and in JSON held in a string, such as a text block, an input or an error message:
  `{"password":"hunter2"}` becomes `{"password":"[SECRET_1]"}`, and the rest of the string stays.
  `redactKeys: ['name', 'address']` (an array or a Set) adds your own: matched on their ASCII letters and digits only, ignoring case, by the end
  of the key, so `name` also covers `firstName` and `fileName`; a narrower name such as `fullname`
  avoids that. Free text, such as a name inside a sentence, isn't caught, nor is other syntax
  (`password=hunter2`, Python's `{'password': 'x'}`).
- `beforeSend(event, { signal })` sees every event before the patterns above are applied, and can
  change it or drop it by returning `null`. Values under secret key names are already replaced, and
  a string cut for length has had its kept part redacted. What it returns is shaped and redacted
  again; it is on time if it settles before `beforeSendTimeoutMs`, however long that takes.
  `event.kind` tells a `turn` from a `tool_call`.
- The wrapper never records HTTP headers, auth info, environment variables or anything else from the
  request context by itself, with three exceptions: the calling app, MCP's own
  `MCP-Protocol-Version` header, as the call's protocol version, and, for a client on a protocol
  version before 2026-07-28, a SHA-256 hash of a stateful HTTP server's `Mcp-Session-Id` or an SSE
  connection's ID, as `sessionId`. The hash groups a task's calls, and `extras: { sessions: false }` stops it. The wrapper
  doesn't record the ID itself, but what an agent or a tool writes can hold it: an argument under `inputs`,
  `intent`, or a result or error text under `results` (with `inputs`, for a failed call). So can an ID an
  agent sends on another connection, if your IDs have the SDK's own `ses_` format. `identify` can read
  the rest; only what it returns is sent, its IDs and `metadata`, shaped and redacted like the rest.
- IDs (`orgId`, `userId`, `conversationId`, `turnId`, `sessionId`, `toolName`) are never redacted.
  Each must be at most 512 bytes, except `turnId`, which may be longer (up to 256 KB as JSON writes
  it), or a safe integer.

## Privacy checklist

- Turn results off for tools that return customer records: `results: (tool) => tool !== 'get_customer'`.
- Use `redactKeys` for personal data in inputs and results, whether structured or JSON in a text
  block, and `beforeSend` for anything finer. To omit sensitive inputs or results, use `inputs` and
  `results`.
- Send `userId`, as an internal ID rather than an email address or phone number: LSquare deletes a
  person's events by it, and finds only events that carry it. A `userId` that looks like an email
  address or phone number is still sent as given, since masking it would stop deletion, and the SDK
  warns once. Return it from [`identify`](REFERENCE.md#who-the-call-is-for-identify), or pass it to
  `capture()`.
- Keep retention short. It's set per project: ask for a short one, especially if you handle personal
  data, since events without a `userId`, and names written inside results, go only with retention.

## Client options

| Option | Default | |
|---|---|---|
| `apiKey` | | Project key. Without it, the client is disabled |
| `endpoint` | `LSQUARE_ENDPOINT`, then `https://ingest.lsquare.io` | https only, except localhost |
| `dryRun` | `LSQUARE_DRY_RUN=1` | Run everything but send nothing |
| `flushTimeoutMs` | `2000` | How long `flush()` waits. `0`: it doesn't |
| `beforeSend` | | Redact more, or drop an event |
| `beforeSendTimeoutMs` | `flushTimeoutMs`, or `2000` when that is `0` | Hook deadline |
| `maxConcurrentHooks` | `16` | `beforeSend` hooks running at once, and `identify` hooks, each ([on Workers](REFERENCE.md#hooks-on-workers)) |
| `maxConcurrentRequests` | `4` | Requests in flight at once; other events wait, and go together |
| `redact` | all on | `false`, or `{ email, phone, card, secret, ip }` |
| `redactKeys` | | More key names whose values are replaced whole |
| `onError` | logs codes, once a minute | `(error, event?) => void`. Receives a redacted copy, or none while earlier reports' copies hold `maxQueueBytes` |
| `logger` | standard error | `{ info, warn }`. Don't pass `console` on a stdio server: its `info` writes to stdout |

In a script or a test, call `await lsquare.shutdown()` before exiting.

## Compatibility

- The core runs on Node 18 and later, Cloudflare Workers (with the `nodejs_compat` compatibility
  flag) and Bun. `lsquare/mcp` runs where the MCP SDK does: SDK 1.x from Node 18, SDK 2.x from Node
  20.
- Linking a `capture()` to its call uses `AsyncLocalStorage`, on from the first `instrument()`.
  Before Node 24 it slows promises across the process a little: about 2× in a benchmark of nothing
  but promises, little in code that waits on I/O.
- The wrapper recognises a server by its shape and relies on parts of the MCP SDKs that aren't
  public API. A new SDK release can change them: the server keeps working, but its tool calls stop
  being recorded. LSquare tests new SDK releases weekly, and its daily checks watch for projects
  whose tool calls stop arriving.
- Loading the SDK twice, once with `import` and once with `require`, still links each capture to its
  call, and a server instrumented from both copies is wrapped once.
- Moving from `@lsquare/node` (0.1): change the package name in the install and import lines. Events
  then carry `sdk.name: "lsquare"`.

## Reference

[REFERENCE.md](REFERENCE.md), beside this README in the package, has the rest:

- [the calling app](REFERENCE.md#the-calling-app), [agent details from inside a
  tool](REFERENCE.md#agent-details-from-inside-a-tool) and [who the call is for, with
  `identify`](REFERENCE.md#who-the-call-is-for-identify);
- [each extra in detail](REFERENCE.md#the-extras), [which tools get
  them](REFERENCE.md#which-tools-get-them), [low-level servers](REFERENCE.md#low-level-servers) and
  [a replaced `tools/call` handler](REFERENCE.md#a-replaced-toolscall-handler);
- [serverless hosts and frameworks](REFERENCE.md#serverless-hosts-and-frameworks);
- [agent turns outside MCP](REFERENCE.md#agent-turns-outside-mcp), with `capture()`;
- [sending](REFERENCE.md#sending).

## Licence

MIT. `lsquare/mcp` includes code adapted from AgentCat's TypeScript SDK, also MIT: see
`THIRD_PARTY_NOTICES`.
