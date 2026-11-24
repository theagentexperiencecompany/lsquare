# Changes

What each release changes, newest first: `lsquare` on npm, `lsquare` on PyPI, and `@lsquare/node`,
the 0.1 SDK, on npm.

## `lsquare` 0.2.3 on npm, `lsquare` 0.2.0b9 on PyPI

**Before you upgrade:** each tool call's event now carries the tool's description, so events are
larger, by the description's length (up to 1,000 characters), and use more of a project's daily
bytes. `descriptions: false` (`descriptions=False`) keeps events as they were.

### Tool descriptions

- **Each `tool_call` event has `toolDescription`:** the tool's description as agents were listed it
  when the call came, cut at 1,000 characters and redacted like any text. It tells you which
  wording of a description a call was made under. `beforeSend` (`before_send`) gets it, and can
  change it or take it out. A tool without a description has none. Neither do `get_more_tools`,
  nor a limited-diagnostics event.
- **`descriptions` turns it off,** everywhere with `false` (`False`), or for chosen tools with a
  function, like `inputs` and `results`.
- **On a high-level server, it is the tool's registration,** read as the call arrives: an
  `McpServer` in TypeScript; in Python, `FastMCP`, `MCPServer`, and fastmcp serving only its own
  tools.
- **On a low-level `Server`, it is the description that instance last listed.** The wrapper reads
  each `tools/list` reply as it goes to the client, and changes nothing in it. A call that reaches
  an instance before any listing has no description, which on a stateless server built per request
  is most calls. The same goes for a server whose `tools/call` handler you replaced, and for
  fastmcp with mounted or proxied servers, or with tool transforms.
- **TypeScript, on a low-level `Server` without extras:** the wrapper now sits in front of your
  `tools/list` handler to read its replies, and passes them on as they are. It left that handler
  alone before, and still does with `descriptions: false`.
- **An event over 256 KB loses its tool's description first,** before its result: the description
  is the same on every call of the tool.

### The contract

- `@lsquare/contract` 0.1.8 checks that `toolDescription` is a string, and the shared cases cover
  it. An ingestion on an earlier contract stores it as it is, like any field it doesn't know.

## `lsquare` 0.2.2 on npm, `lsquare` 0.2.0b8 on PyPI

**Before you upgrade:** `shutdown()` can take up to 5 seconds where it took 2, and so can a Python
process that exits with events still queued. And the client can hold ten times as many events in
memory, in the same 10 MB.

### Sending

- **`shutdown()` waits up to 5 seconds by default,** or `flushTimeoutMs` (`flush_timeout_ms`) when
  that is longer. It waited the flush timeout: 2 seconds by default, and when that is `0`. It still
  returns as soon as everything is sent, and a timeout of its own, `shutdown({ timeoutMs })`
  (`shutdown(timeout_ms)`), still replaces the default. Give the process that long before a host
  kills it.
- **Python: a process that exits without `shutdown()`** sends what's still queued within 5 seconds.
  It was 2. With a `flush_timeout_ms` of your own above `0`, it waits that long, as before.
- **The client holds up to 10,000 events at once,** waiting to be sent or in a request. It held
  1,000. The other limit is unchanged: 10 MB of events, whichever comes first. Past either, the
  oldest events are dropped and reported as `queue_full`, as before. A server that makes events
  faster than they're sent for a while now loses fewer of them.

### Documentation

- The READMEs list the two queue limits among the client options: `maxQueueEvents` and
  `maxQueueBytes` (`max_queue_events`, `max_queue_bytes`).
- On Node 26.5.0, the built-in `fetch` sends the SDK's requests one at a time. Node 26.12.1 sends
  them together: the README says so beside the Node releases it asks for.

## `lsquare` 0.2.1 on npm, `lsquare` 0.2.0b7 on PyPI

**Before you upgrade:** two turn fields that the reference never listed are gone. TypeScript that
sets them stops compiling; at run time they're ignored, like any field the SDK doesn't know. And
with inputs or results off, a failed call's error text is no longer sent.

### More is kept out of events

- **A secret key's value in JSON held in a string.** A value under a secret key name
  (`"password": "…"`, and the names in `redactKeys` / `redact_keys`) is now replaced in any
  string, not only in structured data: a text block holding JSON, which is FastMCP's default
  output, JSON printed by `repr`, pprint or `util.inspect`, and Python's `{'password': 'x'}`. An
  event has a limit on this work; past it, a string holding a quote and a colon is replaced whole.
  With the secret pattern on, `beforeSend` (`before_send`) no longer sees these values; the other
  patterns still run after it.
- **Pydantic's validation errors,** which quote the input they refused as `input_value=…`: that
  input is masked, whatever the field's name.
- **URLs.** Parameters under secret names are masked, in a query or a fragment, along with a signed
  URL's `sig`, `signature` and `hmac` and an OAuth `code`. A password is masked whole, also when it
  holds `@`, `'`, `,` or `\`. When the user is a key, as in `sqs://AKIA…:secret@…`, the user and
  the password are masked together; before, only the key was, and the password was sent.
- **More shapes:** Mastercard's 2221-2720 range, Mir and 14-digit Diners cards; Slack's other
  webhook and response URLs and its government host; encoded cookies; an AWS secret in double
  quotes in JSON; and a private key block that a value runs into or that the string's end cuts. A
  card no longer matches a decimal's fraction or takes in an expiry date.
- **Error text** is recorded only with both inputs and results on, since it can quote what the
  tool was sent and what it would have returned: `error.message`, and the whole of an `isError`
  result. A thrown error's text used to be sent either way. The event still says how the call
  failed: its category, type and code. In TypeScript, an error's own name is kept as its type only
  if it reads as a class's name.
- **The protocol session ID,** which can be a live handle to the session, is sent as its SHA-256
  hash. That's the ID used for clients on protocols before 2026-07-28: a stateful HTTP server's
  `Mcp-Session-Id`, or the ID the SDK gives an SSE or stdio connection. It still groups a task's
  calls, and `extras: { sessions: false }` (`extras={"sessions": False}`) still stops it. A session
  open across the upgrade is grouped as two.
- **Python:** a task that an `inputs` or `results` function returns, and that fails, is no longer
  logged by asyncio with its exception's text.

### `get_more_tools`

- It's offered only when inputs are recorded, since its description tells the agent that what it
  writes reaches the server's developers.
- It's now marked as reaching outside the server (`openWorldHint: true`), since what the agent
  writes leaves it, and still as read-only (`readOnlyHint: true`, `destructiveHint: false`), since
  nothing on the server changes. The READMEs say how the agents tried behave with each label.
- Its reply no longer says the request was passed on, which a hook or a failed `identify` can stop.

### Fixed

- A list of strings holding a URL with a port, such as `['https://host:443','ok','ops@…']`, is no
  longer masked from the port to the next `@`.

### Removed

- Two turn fields the reference never listed, as above.
- The dry run no longer names two other spellings of the token counts among the fields it
  ignored; it counts them with the other unknown fields. Token counts go in `usage.inputTokens`
  and `usage.outputTokens`, as before.

### Documentation

- The packages' links name this repository.
- `REDACTION.md` lists what redaction still misses.
- On Node, two bugs in the built-in `fetch` let a malicious or compromised endpoint crash the
  process past the SDK's error handling. They're fixed from Node 22.23.4, 24.22.1 and 26.12.1.

## `@lsquare/node` 0.1.3

**Before you upgrade:** the two turn fields are gone, as in `lsquare` above.

- A value under a secret key name in JSON held in a string is now replaced, as in `lsquare` above,
  for the SDK's own list of key names: this SDK has no `redactKeys`, and its `beforeSend` still sees
  the event before redaction.
- The other redaction changes above: URLs, more shapes, and a URL with a port in a list of strings.
- The dry run's two other spellings of the token counts, as above.
- The package's links name this repository, and its types' example tool name is a neutral one.
- The Node `fetch` note above.
