# @lsquare/node

Capture each turn of your MCP server's agent and send it to LSquare: the caller's question, your
answer, and the internal tool calls behind it.

This is the 0.1 SDK, kept for the code already using it. New servers use
[`lsquare`](https://www.npmjs.com/package/lsquare), which records every tool call too; its README
says how to move from `@lsquare/node`.

- `capture()` never throws and never waits on the network.
- `flush()` never throws and waits up to 2 seconds by default.
- Common private data is redacted before anything leaves your process.
- Node 18+, no runtime dependencies, ESM and CommonJS.

## Setup

```sh
npm install @lsquare/node
```

Set `LSQUARE_KEY` to your project key. Events go to `https://ingest.lsquare.io`; set
`LSQUARE_ENDPOINT` only to send elsewhere, such as a test endpoint (https; plain http only for
localhost). Start with `LSQUARE_DRY_RUN=1`: nothing is sent, and the SDK
logs one line per event with its shape (fields, anything it dropped, redaction counts, size, and
whether the event ID comes from your `turnId`), never its content or IDs. Unset it to start sending.
The default logger writes those lines to standard error (`console.error`), never to stdout, which
carries a stdio server's MCP messages; some logging systems file standard error lines as errors.
`logger: console`, passed explicitly, writes them to stdout with `console.info`: don't pass it on a
stdio server.

```ts
import { LSquare } from '@lsquare/node'

const lsquare = new LSquare({ apiKey: process.env.LSQUARE_KEY }) // no key: disabled, never throws

// After each turn is saved
lsquare.capture({
  conversationId, turnId, orgId, toolName: 'your_tool',
  question, answer, toolCalls,
  usage: { inputTokens, outputTokens, costUsd },
  latency, verification, client, apiKeyId,
  surface: 'mcp',
})
await lsquare.flush()

// Optional, when a turn fails
lsquare.capture({ conversationId, turnId, orgId, toolName: 'your_tool', question, status: 'error', error: err })
await lsquare.flush()
```

`conversationId`, `orgId`, `toolName` and `question` are required. Leave out any optional field you
don't have. `toolCalls`, `latency`, `verification`, `client` and `error` accept any shape: values the
SDK can't use are removed and listed in the event's `dropped` field, never a compile error. A value
your code won't let it read (a getter or Proxy that throws) counts as one of the wrong type.

With a `turnId`, the event ID is made from `conversationId` and `turnId`, without `orgId`, so a turn
captured twice is stored once. Conversation IDs must then be unique across your organizations within
the project: otherwise two organizations' turns with the same conversation and turn IDs are taken
for one.

Tool calls keep only `name`, `input`, `resultChars`, `resultIsError`, `errorMessage` and
`durationMs`. A `result` is reduced to its length.

## Redaction

Common formats of emails, phone numbers, card numbers, API keys and tokens (including LSquare's
own), credentials in URLs, and IPv4 and IPv6 addresses become numbered placeholders such as
`[EMAIL_1]`. It catches common formats,
not every piece of private data. Each pattern can be turned off: `redact: { ip: false }`, or
`redact: false` for all.

Values under secret key names in tool inputs, `latency`, `verification` and `metadata` are replaced
whole, whatever they look like: `{ dbPassword: '…' }` is sent as `{ dbPassword: '[SECRET_1]' }`. A
key is secret when its name, ignoring case and punctuation, ends with `password(s)`, `passwd`,
`passphrase`, `secret(s)`, `token`, `auth`, `authorization`, `apikey(s)`, `privatekey`, `secretkey`,
`accesskey`, `signingkey`, `encryptionkey`, `credential(s)`, `cookie(s)` or `connectionstring`. This is
part of the `secret` pattern. Redact names specific to your app with `beforeSend`.

The same rule applies to JSON held in a string, such as an input or an answer:
`{"password":"hunter2"}` becomes `{"password":"[SECRET_1]"}`, and the rest of the string stays. So do
Python's `{'password': 'x'}`, as pydantic's errors quote an input, and a URL's `?token=abc`. Other
syntax (`password=hunter2`, `{password: "x"}`) isn't covered. Use `beforeSend` for those.

Identifier fields are never redacted: `conversationId`, `turnId`, `orgId`, `toolName`, `apiKeyId`,
`surface`, `client` and `status`. Put opaque IDs there, never emails, names or other personal data.
`beforeSend` can't change the first four: they are restored from the original capture.

Names, company names and usernames are not detected. Redact them with `beforeSend`, which runs before
the built-in redaction, can be async, and drops the event if it returns `null`:

```ts
new LSquare({
  apiKey: process.env.LSQUARE_KEY,
  beforeSend: async (event, { signal }) => ({ ...event, question: await scrub(event.question, { signal }) }),
})
```

`beforeSend` sees unredacted data. If it calls an external service, the data goes there. Pass
`signal` on: at the hook's deadline the SDK aborts it, and a hook that ignores it keeps its slot (one
of `maxConcurrentHooks`) until it settles.

## In your process

`capture()`, `flush()` and `shutdown()` never throw, and what the SDK does in the background is
caught: problems become `onError` reports, not uncaught exceptions or unhandled rejections. Memory
is bounded by the queue limits, dropped events are released at once, and the SDK's timers never keep
your process alive. Call them as methods: `capture` passed on its own (e.g. `.then(lsquare.capture)`)
doesn't throw, but drops the event. A request to LSquare still in flight does keep the process alive
until it ends, at most 10 seconds: in a script or a test, call `shutdown()` before exiting.

Two bugs in Node's built-in `fetch` are the exception: a malicious or compromised endpoint can crash
the process, and the SDK can't catch it
([GHSA-xjj6-f33q-929c](https://github.com/advisories/GHSA-xjj6-f33q-929c),
[GHSA-qrxm-fqhp-jwvm](https://github.com/advisories/GHSA-qrxm-fqhp-jwvm)). They're fixed from Node
22.23.4, 24.22.1 and 26.12.1; Node 18, 20 and 25 have no fixed release.

Your callbacks (`beforeSend`, `onError`, `logger`) are your code, and so are getters, `toJSON`
methods and Proxies in the data you capture. The SDK catches what they throw or reject, and gives
every promise it reads from your data a rejection handler, even if you replaced the global
`Promise` (bluebird, zone.js). It can't stop code that blocks the event loop, errors thrown from
timers or event listeners that code sets up itself, or a promise the SDK never sees (one a Proxy
trap hands only to the JavaScript engine) or can't attach a handler to (one whose `constructor`
throws). With a custom `fetch` that ignores the abort signal, the SDK still stops response bodies it
no longer reads, as far as the stream lets it.

Each event's callbacks, and its request to LSquare, run in the async context of the `capture()` call
that produced it, so your own `AsyncLocalStorage` (request, tenant, trace) is that request's. Before
Node 24, setting `beforeSend`, `onError` or a logger other than `console` makes the SDK use
`AsyncLocalStorage` itself, which adds a small cost to every promise in the process; the defaults
don't.

## Options

| Option | Default | |
|---|---|---|
| `apiKey` | | Project key. Without it, the client is disabled |
| `endpoint` | `LSQUARE_ENDPOINT`, then `https://ingest.lsquare.io` | Ingestion base URL. https only, except localhost |
| `dryRun` | `LSQUARE_DRY_RUN=1` | Run everything but send nothing |
| `flushTimeoutMs` | `2000` | How long `flush()` waits. `0`: it doesn't |
| `beforeSend` | | Redact more, or drop an event |
| `beforeSendTimeoutMs` | `flushTimeoutMs`, or `2000` when that is `0` | Hook deadline, counted from `capture()` |
| `maxConcurrentHooks` | `16` | |
| `maxConcurrentRequests` | `32` | Requests to LSquare in flight at once; other events wait their turn |
| `redact` | all on | `false`, or `{ email, phone, card, secret, ip }` |
| `onError` | logs codes, once a minute | `(error, event?) => void`, may be async. Receives a redacted copy, never the queued event. Called in a later event-loop turn; a burst of 100, then 100 a second, and `error.suppressed` counts what was dropped |
| `logger` | `console`, both levels on standard error | `{ info, warn }` |

`onError` and `logger` are best-effort: they can run after `flush()` or `shutdown()` resolves, are lost
on `process.exit()`, and on CPU-throttled serverless may only run with the next request.

`flush()` resolves to `{ sent, skipped, failed, pending }`. It doesn't wait for an event whose next
retry is due after its deadline, since that event can't finish in time: one rate-limited event
doesn't hold up every later request. `flush({ timeoutMs: 0 })` returns at once and doesn't report
what is pending. `shutdown()` is terminal: it drops events still waiting for `beforeSend`, waits up
to the flush timeout for the rest (2 seconds when `flushTimeoutMs` is `0`), then drops whatever is
left. Nothing is sent after it resolves. In both, a `timeoutMs` that can't be read (a getter that
throws) counts as not given.

## Licence

MIT
