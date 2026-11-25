# lsquare

Record every tool call your MCP server handles in LSquare. A first beta for Rust: it records tool
calls on servers built with [`rmcp`](https://crates.io/crates/rmcp), the official Rust MCP SDK, and
nothing else yet ([below](#not-yet-supported)). LSquare is an invite-only beta, so
[request access](https://lsquare.io) to get an API key.

- One line per MCP server. Nothing the SDK does can make a tool call fail: it never panics, and
  the handler's answer, error or panic reaches the client as it would without it.
- A tool's reply never waits on the network, and `flush()` waits up to 2 seconds by default.
- Common private data is redacted, and long values shortened, before anything leaves your process.
- Tokio, with timers and IO as `#[tokio::main]` enables them, and `rmcp` 3.11. Rust 1.88 or later.
- Events are stored in the United States. If you serve customers in the EU, take that into account.

## Quickstart

A server with one tool, served over HTTP on your machine, and one call to it with `curl`. It is
[`examples/weather.rs`](examples/weather.rs), which `cargo run --example weather` runs from this
folder.

1. **Add** `lsquare` beside `rmcp`:

   ```sh
   cargo add lsquare
   ```

   ```sh
   cargo add rmcp@~3.11 --features server,macros,transport-streamable-http-server
   ```

   `lsquare` is a beta: its only version on crates.io is a pre-release, which `cargo add` picks
   while there is no stable one. A requirement written by hand needs the version in full, as
   `cargo add` writes it (`lsquare = "0.2.0-beta.1"`): one such as `"0.2"` doesn't match a
   pre-release.

2. **Instrument the server:**

   ```rust
   use lsquare::mcp::{instrument, InstrumentOptions};

   let lsquare = lsquare::Client::new(lsquare::Options::new().api_key(std::env::var("LSQUARE_KEY").ok()));

   // A new server for each session, as rmcp serves them over HTTP; instrument each one.
   let recorder = lsquare.clone();
   let service = StreamableHttpService::new(
       move || Ok(instrument(Weather, &recorder, InstrumentOptions::new())),
       LocalSessionManager::default().into(),
       StreamableHttpServerConfig::default().with_legacy_session_mode(false).with_json_response(true),
   );
   ```

3. **See the first event, in dry run**, where nothing is sent:

   ```sh
   LSQUARE_DRY_RUN=1 cargo run --example weather
   ```

   In another terminal, call the tool:

   ```sh
   curl -s http://127.0.0.1:3000/mcp -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_forecast","arguments":{"city":"Lisbon"}}}'
   ```

   The reply is your tool's result. The server's terminal shows a line for what `instrument()`
   did, and one for the call's event: its fields and size, never its content or IDs.

   ```
   [lsquare dry-run] {"server":"weather","records":"every tools/call"}
   [lsquare dry-run] {"kind":"tool_call","eventId":"the call","fields":["callId","durationMs","input","protocolVersion","result","server","status","toolDescription","toolName"],"bytes":580}
   ```

4. **Send it.** Stop the server, start it again with your project key instead of dry run, and call
   the tool again:

   ```sh
   LSQUARE_KEY=lsq_your_key cargo run --example weather
   ```

   Nothing is printed when LSquare accepts an event. A problem is logged as one `[lsquare]` line,
   such as `[lsquare] send_failed: LSquare answered 401; not retried.` for a key it doesn't know.

Without a key, and outside dry run, the client is disabled: `instrument()` passes every call
straight to your handler, with one warning when the client is made. The SDK's lines go to standard
error, so a stdio server's standard output stays clean.

**Before the process exits, `lsquare.shutdown().await`.** Events are sent from tasks on your Tokio
runtime: what is still queued when the runtime ends is lost. For a stdio server:

```rust
let running = instrument(Weather, &lsquare, InstrumentOptions::new()).serve(rmcp::transport::stdio()).await?;
running.waiting().await?;
lsquare.shutdown().await;
```

## MCP servers

`instrument(handler, &client, options)` takes any `rmcp::ServerHandler` and returns one, so it goes
wherever the handler went: `.serve(transport)`, or the factory of a `StreamableHttpService`. It
records `call_tool`, reads the descriptions `list_tools` gives, and passes every other method on
to your handler unchanged. A handler instrumented twice, one wrapper inside the other, is recorded
once, by the inner one and with its options, also after that one's client was shut down; another
tool your handler calls on its way is an event of its own. Each `tools/call` becomes one
`tool_call` event:

| Field | What it holds |
| --- | --- |
| `toolName`, `status`, `durationMs` | The tool, `completed` or `error`, and how long the handler took, on a monotonic clock |
| `toolDescription` | The tool's description as agents were listed it: the registered tool's (`get_tool`, which `#[tool_handler]` writes for you), or else the one this instance last listed. Cut at 1,000 characters |
| `serverInstructions` | The instructions your server gives clients (`get_info().instructions`), on every call whichever the tool. Read when `instrument()` is called, not on each call: your `get_info` never runs on a call's path. Cut at 2,000 characters |
| `input` | The arguments. Strings are cut at 16,000 characters |
| `result` | MCP's result as your handler returned it: text cut at 4,000 characters, images, audio and blobs as their size in bytes |
| `error` | For a failed call, its `category`: `protocol` (the handler returned an `ErrorData`; its `code` is kept), `tool_error` (an `isError` result), `timeout` (the request was cancelled), `thrown` (the handler panicked). Its message is kept only when both inputs and results are recorded for the tool, since it can quote either |
| `client`, `protocolVersion`, `server` | The calling app as it named itself, the protocol version, and your server's name and version. A client that gave no name has none: `rmcp` fills in its own name for a request that came without one, which is left out |
| `orgId`, `userId`, `conversationId`, `metadata` | What `identify` returns |
| `task`, `round` | A task the call created, and whether the call asked for input or brought it |

### `instrument()` options

```rust
let options = InstrumentOptions::new()
    .identify(|input| async move {
        // The MCP SDK's request context: over HTTP, its extensions hold the request's parts.
        let account = lookup(input.context()).await?;
        Ok(Identity::new().org_id(account.org).user_id(account.user))
    })
    .inputs_for(|tool| tool != "upload_document")
    .results(false);
```

| Option | Default | What it does |
| --- | --- | --- |
| `identify` | none | Who the call is for. It starts once the tool has, never before it, and is waited for at most a second after the tool returns. If it fails (any error converts with `?`), takes longer, panics or returns an invalid ID, the call is sent as limited diagnostics (its tool, status, duration and error category, nothing that could name anyone) and `on_error` is told (`identify_failed`). It gets its own copy of the arguments, the only copy the SDK makes of a call: without `identify`, arguments and results are read where they are |
| `inputs`, `inputs_for` | everywhere | Record arguments: everywhere, nowhere, or for the tools a function returns true for |
| `results`, `results_for` | everywhere | Record results, the same way |
| `descriptions`, `descriptions_for` | everywhere | Record each call's tool description, the same way |
| `instructions` | on | Record the server's instructions on each call. `instructions(false)` turns it off |

## What leaves your process

The same as from the [TypeScript](../lsquare/README.md#what-leaves-your-process) and Python SDKs,
by the same rules ([`packages/contract/REDACTION.md`](../contract/REDACTION.md)): emails, phone
numbers, card numbers, known formats of API keys and tokens, credentials in URLs, private keys and
IP addresses are replaced by placeholders such as `[EMAIL_1]`, numbered per event; a value under a
key named like a secret (`password`, `apiKey`, `Authorization`, and your own `redact_keys`) is
replaced whole, also in JSON held in a string. `before_send` runs before the patterns and can
redact more or drop an event; what it returns is shortened and redacted again.

What a call holds (its arguments, its result, its error) is shortened and redacted into the same
bytes the TypeScript SDK writes for it: a test compares the two SDKs on generated inputs. Two
things follow from that:

- **Objects are read in JavaScript's order:** keys that are whole numbers (`"2"`, `"10"`) first,
  ascending, then the others in the order the client sent them. So placeholders are numbered as in
  the other SDKs, and of an object of more than 100 entries the same 100 are kept.
- **Numbers are read and written as JavaScript reads and writes them.** An integer past 2^53 is
  the nearest double: `9007199254740993` is sent as `9007199254740992`, as the other SDKs send it.

`before_send` reads the event in that order and with those numbers, as it will be sent.
`identify` is given the arguments as your handler gets them: in the order they came, and exact.

What `serde_json` can't read never reaches the SDK: a number too large for a double, or a lone
surrogate.

`lsquare` turns on two features of `serde_json`: `preserve_order`, to know the order keys came
in, and `float_roundtrip`, to read every number as the double JavaScript reads. Cargo unifies
features, so they are on for all of your build: every `serde_json::Map` keeps insertion order
rather than sorting its keys, and numbers with a fraction are read exactly, which takes about
twice as long for those numbers. If your build turns on `arbitrary_precision`, numbers are still
written as JavaScript writes them; one too large for a double is then sent as `null`.

## Client options

`lsquare::Options::new()`, then:

| Option | Default | What it does |
| --- | --- | --- |
| `api_key` | none | The project's key. Without it, outside dry run, the client is disabled |
| `endpoint` | `LSQUARE_ENDPOINT`, then `https://ingest.lsquare.io` | The ingestion service. `https`, or `http` for localhost |
| `dry_run` | `LSQUARE_DRY_RUN=1` | Runs everything, sends nothing, and logs one line per event |
| `flush_timeout` | 2 s | How long `flush()` waits. `shutdown()` waits 5 s, or this when longer. Both give `on_error` up to a second more for what they report themselves |
| `before_send` | none | Gets every event before the patterns run; returns it, changed or not, or `None` to drop it. Runs on a thread of its own, never on the tool's path nor on your runtime |
| `before_send_timeout` | the flush timeout | An event whose hook returns later is dropped (`hook_timeout`) |
| `max_concurrent_hooks` | 16 | `before_send` hooks, and `identify` hooks, running at once |
| `max_concurrent_requests` | 4 | Requests to LSquare in flight; other events wait, and go in batches of up to 100 |
| `redact`, `redact_pattern` | all on | `redact(false)` turns built-in redaction off; `redact_pattern(Pattern::Ip, false)` one pattern |
| `redact_keys` | none | More key names whose values are replaced whole, matched by their end as `password` is |
| `on_error` | logs, at most a line a minute | Told of every event dropped or failed, with its code and, when it was redacted already, the event. Runs on a thread of its own, one report at a time: never on a tool's path nor on your runtime, so a slow handler holds up no reply. `flush()` and `shutdown()` wait for it within their own time. Up to 100 reports wait for a slow handler; later ones are dropped, which is logged once |
| `logger` | standard error | Where the SDK's own lines go. But for the lines `Client::new` and `instrument()` write as you call them, it is called from the thread `on_error` has, never on a tool's path |
| `max_queue_events`, `max_queue_bytes` | 10,000 and 10 MB | Past either, the oldest events not yet in a request are dropped (`queue_full`) |

An event is sent up to 3 times: a network error, a 429 or a 5xx is retried after a random share of
a delay that doubles (250 ms, 500 ms, capped at 5 s), or after `Retry-After`, up to 30 s. Any other
status isn't retried, and a redirect is never followed, so the key can't go elsewhere. When the
project's daily limit is reached, the client reports it once (`quota_exceeded`), drops events until
the limit resets, and checks every 15 minutes whether it was raised.

## Not yet supported

This beta is the client and the `rmcp` wrapper. Compared with the TypeScript and Python SDKs:

- **The extras:** the `session_id` and `intent` inputs and the `get_more_tools` tool. So events
  carry no `sessionId` and no `intent`, and calls aren't grouped into tasks.
- **`capture()` and `turn` events:** agent turns, inside a tool call or outside MCP.
- **Other Rust MCP libraries.** Only `rmcp`, and only 3.11: `lsquare` forwards each method of
  rmcp's `ServerHandler` by hand, so it accepts no later rmcp until a release that forwards what
  that one adds. A test compares the two lists.
- **`flushBeforeReply` and `waitUntil`**, for hosts that stop a process once it has replied.
- **`rmcp`'s `local` feature** (handlers that aren't `Send`): the crate builds with it, but
  `identify` must still be `Send`, and its tests don't run that way.
- **Calls `rmcp` refuses before your handler** (a malformed request, a missing header) aren't
  recorded: the wrapper sees what reaches `call_tool`.

## Licence

MIT, in [LICENSE](LICENSE).
