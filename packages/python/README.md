# lsquare

Record every tool call your MCP server handles, and the agent turns behind them, in LSquare.
Pre-release: LSquare is an invite-only beta, so [request access](https://lsquare.io) to get an API
key. During the beta, LSquare sends you a weekly report on your tool calls (the most used, failing
and slowest tools, missing tools and top intents), and exports on request.

- One line per MCP server. Nothing the SDK does can make a tool call fail, and it never raises.
- `capture()` never waits on the network, and `flush()` waits up to 2 seconds by default, then up
  to half a second more for a slow `on_error` or logger.
- Common private data is redacted, and long values shortened, before anything leaves your process.
- No runtime dependencies. Python 3.10 and later; the same events as LSquare's TypeScript SDK.
- Events are stored in the United States. If you serve customers in the EU, take that into account.

## Quickstart

A server with one tool, served over HTTP on your machine, and one call to it with `curl`.

1. **Install** the MCP Python SDK (2.x here; 1.x and fastmcp work too, [below](#mcp-servers)), then
   `lsquare`, which is a pre-release until launch: `--pre` on its own line, so it doesn't also pick
   up pre-releases of the SDK.

   ```sh
   pip install mcp
   ```

   ```sh
   pip install --pre lsquare
   ```

2. **Instrument the server**, in `server.py`:

   ```python
   import os

   from lsquare import LSquare
   from lsquare.mcp import instrument
   from mcp.server.mcpserver import MCPServer

   lsquare = LSquare(api_key=os.environ.get("LSQUARE_KEY"))

   mcp = MCPServer("weather")


   # Text results only: see structuredContent under session_id, in REFERENCE.md.
   @mcp.tool(structured_output=False)
   def get_forecast(city: str) -> str:
       """Gives tomorrow's forecast for a city."""
       return f"Tomorrow in {city}: sunny, 21°C."


   instrument(mcp, lsquare)

   if __name__ == "__main__":
       mcp.run(transport="streamable-http", stateless_http=True)
   ```

   On mcp 1.x: `from mcp.server.fastmcp import FastMCP`, `mcp = FastMCP("weather",
   stateless_http=True)` and `mcp.run(transport="streamable-http")`.

3. **See the first event, in dry run**, where nothing is sent:

   ```sh
   LSQUARE_DRY_RUN=1 python server.py
   ```

   In another terminal, call the tool:

   ```sh
   curl -s http://127.0.0.1:8000/mcp -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_forecast","arguments":{"city":"Paris"}}}'
   ```

   The reply is your tool's result, then a text block with the call's task ID, "Task ID: ses_…"
   ([`session_id`](REFERENCE.md#session_id-calls-grouped-into-tasks), in the reference). The
   server's terminal shows a line for what `instrument()` did, and one for the call's event: its
   fields and size, never its content or IDs.

   ```
   [lsquare dry-run] {"server":"weather","level":"high","records":"every tools/call","extras":{…}}
   [lsquare dry-run] {"kind":"tool_call","eventId":"the call","fields":["callId","durationMs","input","result","server","sessionId","sessionSource","status","toolName"],"bytes":571}
   ```

4. **Send it.** Stop the server, start it again with your project key instead of dry run, and call
   the tool again:

   ```sh
   LSQUARE_KEY=lsq_your_key python server.py
   ```

   Nothing is printed when LSquare accepts an event. A problem is logged as one `[lsquare]` line,
   such as `[lsquare] send_failed: LSquare answered 401; not retried.` for a key it doesn't know.

Without a key, and outside dry run, the client is disabled: `capture()` does nothing and
`instrument()` leaves your servers exactly as they are, with one warning. Over stdio, the dry-run
lines go to the server's standard error, which MCP clients keep in their logs. Before deploying,
read [Serverless hosts and shutting
down](REFERENCE.md#serverless-hosts-and-shutting-down): hosts that stop your process once it has
replied, and sending what's still queued when it ends (on `SIGTERM`, a synchronous server just
exits, `sys.exit(0)`, and never calls the SDK from the handler). An agent that isn't behind an MCP
server records its turns
with `capture()`: [agent turns outside MCP](REFERENCE.md#agent-turns-outside-mcp).

## MCP servers

```python
import os
from lsquare import LSquare
from lsquare.mcp import instrument
from mcp.server.fastmcp import FastMCP

lsquare = LSquare(api_key=os.environ.get("LSQUARE_KEY"))   # once, at module level

mcp = FastMCP("orders")

@mcp.tool()
def search_orders(customer_id: str) -> str: ...

instrument(mcp, lsquare)                                     # the one line per server
```

`instrument()` takes a server of the official MCP Python SDK, 1.x (`FastMCP`, or a low-level
`Server`) or 2.x (`MCPServer`, or a low-level `Server`), or of fastmcp 3.x or 4.x (`FastMCP`). It
returns the server it was given, and instrumenting a server twice does nothing the second time.
Tools registered after it are covered too. Instrument a server before running it: a client that
listed its tools before keeps a copy without the extras (below) until it lists them again. On mcp
2.x, instrument after adding your own middleware to the server: replacing its `middleware` list
afterwards removes the wrapper.

Each call that reaches the server's `tools/call` dispatch becomes one `tool_call` event: the tool's
name, its arguments (`input`), its result, how it ended (`status`, and an `error` with its
`category`: `thrown`, `tool_error`, `protocol` or `timeout`), how long it took, the calling app and
protocol version, and the IDs below. Results are recorded as the SDK sends them, each string cut at
4,000 characters; turn inputs or results off everywhere, or for chosen tools, with `inputs` and
`results`. A failed call's text, in `error.message` and in an `isError` result, can quote what the
tool was sent and what it would have returned, so it's recorded only with both on. The event still
says how the call failed: `category`, `type` and `code`. A cancelled call's fixed sentence stays.
A call the client cancelled is a `timeout`, also when your tool caught the cancellation
and returned. fastmcp's `call_tool()`, called from your own Python code, never reaches the dispatch
and makes no event; called inside a call (from a tool or a task it started, from middleware, or
from a fastmcp 4.x extension's interceptor), it leaves that call's record alone too.

### What the defaults change in your server

On a high-level server three extras are on by default, so calls are grouped into tasks, each call
says why it was made, and agents can tell you what they couldn't find. They change what your server
shows its callers:

- every tool gains two optional inputs, `session_id` and `intent`, which the wrapper takes out of
  each call before the SDK checks it, so your code never sees them;
- some results gain one text block at the end, giving the agent a task ID;
- the server gains a tool, `get_more_tools`, which the wrapper answers itself.

Clients and scanners that flag changed tool definitions may warn your server's users once when you
add the SDK. Each extra can be turned off by itself, in your one `instrument()` call:

```python
instrument(mcp, lsquare, extras={
    "sessions": False,        # no session_id, no task ID line
    "intent": False,          # no intent
    "missing_tools": False,   # no get_more_tools
})                            # leave any of them out to keep it on
```

`extras=False` turns all three off at once. With all three off, the wrapper never changes
arguments, results or errors. On a low-level `Server`
they're off ([in the reference](REFERENCE.md#low-level-servers)), whose
[extras](REFERENCE.md#the-extras) section says exactly what each one does.

### `instrument()` options

| Option | Default | |
|---|---|---|
| `identify` | | `(call) -> {"orgId", "userId", "conversationId", "metadata"}` or `None`, sync or async ([how it works](REFERENCE.md#who-the-call-is-for-identify)) |
| `inputs` | `True` | `False`, or `(tool_name) -> bool`. Off for `get_more_tools`, the server doesn't get that tool |
| `results` | `True` | `False`, or `(tool_name) -> bool` |
| `extras` | all on (high-level), off (low-level) | `{"sessions", "intent", "missing_tools"}` to booleans; one left out keeps its default. `False`: all off. On a low-level server only `True` turns one on ([in the reference](REFERENCE.md#low-level-servers)) |
| `wait_until` | | A callable taking an awaitable: each call's sending is handed to it ([serverless hosts](REFERENCE.md#serverless-hosts-and-shutting-down)) |
| `flush_before_reply` | `False` | Wait for each call's event to be sent, up to `flush_timeout_ms`, before replying |

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
  it asked for the payment-processor logs that would explain why. Those runs were before the tool
  stopped being marked read-only. Run again on 9 November 2026 (Claude Code 2.1.321, Codex CLI
  0.167.0), Claude Code still called it in every run where no tool fitted (16 of 16) and never
  otherwise. Codex tried to in all 16, and refused each call itself: it asks the person before a
  tool that isn't marked read-only, and these runs had approvals off. Used interactively, Codex asks
  the person before each call.

## What leaves your process

- Python values become JSON as the TypeScript SDK's would: pydantic models by their fields (as
  `model_dump(by_alias=True)` names them, with no serializer run), dataclasses by their fields, UUIDs
  and pydantic URLs as strings, enums as their values, aware datetimes in UTC. Bytes, naive
  datetimes, `Decimal`, sets and other objects aren't JSON and are left out; a field with nothing
  JSON left in it is named in the event's `dropped`. So is a value that can't be read as what it
  says it is (a lazy object whose factory fails, a proxy passing for a string or a number): only
  that value is left out, not the whole event, unless it's an ID, which drops the event as
  `invalid_id`. A field of what `capture()` is given whose read raises drops the event.
  `capture()` reads its data before it returns, inside a tool call too: what your code does with
  its objects afterwards changes nothing. A call's arguments and result are read when its event is
  built, at the reply, or up to a second later while `identify` runs.
- Strings in a call's `input` and `agent`, and in a turn, are cut at 16,000 characters, results at
  4,000 and `intent` at 500; a cut string ends with `…[+N chars]`, or `…[cut]` once an event's work
  budget for reading strings is spent. That budget also bounds how long building an event holds
  your loop or thread: about a quarter of a second on a current laptop for one that uses all of
  it, and up to twice that for a call with a `capture()` inside it, whose agent part is read with a
  budget of its own. Lists and objects keep their first 100 entries, and nesting stops at 10
  levels. Images, audio, binary resources and files sent as base64 text are kept as their size
  only.
- An event still over 256 KB loses parts, biggest and least needed first: a call its result, then its
  input, then its agent's tool calls, then its agent part; a turn its tool calls, then its answer,
  question, verification and latency. The event's `truncated` field names what was shortened or left
  out.
- Common formats of emails, phone numbers, card numbers, API keys and tokens, credentials in URLs,
  and IP addresses become numbered placeholders such as `[EMAIL_1]`, in inputs, results, errors,
  intents and agent details alike. Each pattern can be turned off: `redact={"ip": False}`, or
  `redact=False` for all.
- Values under secret key names (`password`, `token`, `apiKey` and the like) are replaced whole, in
  structured data and in JSON held in a string, such as a text block, an input or an error message:
  `{"password":"hunter2"}` becomes `{"password":"[SECRET_1]"}`, and the rest of the string stays.
  So do Python's `{'password': 'x'}`, as pydantic's errors quote an input, and a URL's `?token=abc`.
  `redact_keys=["name", "address"]` (a list or a set) adds your own: matched on their ASCII letters and digits only, ignoring case, by the end
  of the key, so `name` also covers `firstName` and `fileName`; a narrower name such as `fullname`
  avoids that. Free text, such as a name inside a sentence, isn't caught, nor is other syntax
  (`password=hunter2`, `{password: "x"}`).
- `before_send(event, signal)` sees every event before the patterns above are applied, and can
  change it or drop it by returning `None`; it may be sync or async. Values under secret key names
  are already replaced, and a string cut for length has had its kept part redacted. What it returns
  is shaped and redacted again, on one of the client's threads, never on your event loop nor while
  other captures wait (with no thread to take it, the system refusing to start one, the event is
  dropped as `hook_failed`). It is on time if it returns before `before_send_timeout_ms`, however long
  that reading takes, up to twice that deadline, after which the event is dropped as `hook_failed`.
  `event["kind"]` tells a `turn` from a `tool_call`.
- The wrapper never records HTTP headers, auth info, environment variables or anything else from the
  request context by itself, with three exceptions: the calling app, MCP's own
  `MCP-Protocol-Version` header, as the call's protocol version, and, for a client on a protocol
  version before 2026-07-28, a SHA-256 hash of a stateful HTTP server's `Mcp-Session-Id`, as
  `sessionId`. The hash groups a task's calls, and `extras={"sessions": False}` stops it. The wrapper
  doesn't record the ID itself, but what an agent or a tool writes can hold it: an argument under `inputs`,
  `intent`, or a result or error text under `results` (with `inputs`, for a failed call). So can an ID an
  agent sends on another connection, if your IDs have the SDK's own `ses_` format.
  `identify` can read the rest; only what it returns is sent, its IDs and `metadata`, shaped and
  redacted like the rest.
- IDs (`orgId`, `userId`, `conversationId`, `turnId`, `sessionId`, `toolName`) are never redacted.
  Each must be at most 512 bytes, except `turnId`, which may be longer (up to 256 KB as JSON writes
  it), or a safe integer.

## Privacy checklist

- Turn results off for tools that return customer records: `results=lambda tool: tool != "get_customer"`.
- Use `redact_keys` for personal data in inputs and results, whether structured or JSON in a text
  block, and `before_send` for anything finer. To omit sensitive inputs or results, use `inputs` and
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
| `api_key` | | Project key. Without it, the client is disabled. Never read from the environment by the SDK |
| `endpoint` | `LSQUARE_ENDPOINT`, then `https://ingest.lsquare.io` | https only, except localhost. Read as TypeScript's `new URL` reads it, but an internationalized host (non-ASCII, or `xn--`) disables the client |
| `dry_run` | `LSQUARE_DRY_RUN=1` | Run everything but send nothing |
| `flush_timeout_ms` | `2000` | How long `flush()` waits for events, then up to half a second more for its reports to reach a slow `on_error` or logger. `0`: it doesn't wait (`shutdown()` and the exit flush still wait up to 2 s) |
| `before_send` | | Redact more, or drop an event |
| `before_send_timeout_ms` | `flush_timeout_ms`, or `2000` when that is `0` | Hook deadline |
| `max_concurrent_hooks` | `16` | `before_send` hooks running at once, and `identify` hooks, each |
| `max_concurrent_requests` | `4` | Requests in flight at once; other events wait, and go together |
| `redact` | all on | `False`, or `{"email", "phone", "card", "secret", "ip"}` to booleans |
| `redact_keys` | | More key names whose values are replaced whole |
| `on_error` | logs codes, once a minute | `(error, event=None)`. Receives a redacted copy, or `None` while earlier reports' copies hold `max_queue_bytes`; may be async |
| `logger` | standard error | A `logging.Logger`, or any object with `info` and `warn` or `warning`. Never stdout |

Errors given to `on_error` are an `LSquareError` with `code`, `message`, `event_id`, `paths`,
`status`, `resets_at` and `suppressed`. `flush()` returns a `FlushResult` with `sent`, `skipped`,
`failed` and `pending`. Its counts cover the work still outstanding when it starts: events already
finished aren't counted, so zero counts don't mean nothing was sent. `shutdown()`'s counts add
what is still outstanding as it closes, after waiting for calls still being built. It drops an
event still waiting for `before_send`, as failed, reported as `shutdown`: with a hook, call
`flush()` first (`await lsquare.aflush()` on an event loop), with a timeout longer than the hook
takes. The client's threads often send an event before `flush()` is called, so the same code can
report lower counts than in TypeScript, where nothing is sent until the event loop is free.

## Compatibility

- Python 3.10 to 3.14. `lsquare.mcp` works with mcp 1.x and 2.x, and fastmcp 3.x and 4.x, and
  imports none of them itself.
- Sending needs no event loop. Async hooks (`before_send`, `identify`) need asyncio; on trio,
  through anyio, only sync ones run. An async `on_error` or logger runs on the asyncio loop it
  comes from while that loop runs, and otherwise, on trio too, on a loop of the client's own. An
  async hook left on a loop your app closes without running its tasks to their end
  (`run_until_complete()`, then `close()`) fails its event (`hook_failed`, or `hook_timeout` once
  past its deadline) and gives its place back, whether it was still running or had just returned. The client's own loop runs until `shutdown()`, or, for a client you drop without it, until
  its thread finds the client gone (within 30 s), cancelling what is left on it.
- Linking a `capture()` to its call uses a context variable of the client's own.
- The wrapper recognises a server by its shape and relies on parts of the MCP SDKs that aren't
  public API. A new SDK release can change them: the server keeps working, but its tool calls stop
  being recorded. LSquare tests new SDK releases weekly.

## Reference

[REFERENCE.md](REFERENCE.md), beside this README in the source distribution, has the rest:

- [the calling app](REFERENCE.md#the-calling-app), [agent details from inside a
  tool](REFERENCE.md#agent-details-from-inside-a-tool) and [who the call is for, with
  `identify`](REFERENCE.md#who-the-call-is-for-identify);
- [each extra in detail](REFERENCE.md#the-extras), [which tools get
  them](REFERENCE.md#which-tools-get-them), [low-level servers](REFERENCE.md#low-level-servers) and
  [a replaced `tools/call` handler](REFERENCE.md#a-replaced-toolscall-handler);
- [serverless hosts and shutting down](REFERENCE.md#serverless-hosts-and-shutting-down);
- [agent turns outside MCP](REFERENCE.md#agent-turns-outside-mcp), with `capture()`;
- [sending](REFERENCE.md#sending).

## Licence

MIT. `lsquare.mcp` includes code adapted from AgentCat's TypeScript SDK, also MIT: see
`THIRD_PARTY_NOTICES`.
