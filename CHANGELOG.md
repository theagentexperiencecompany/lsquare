# Changes

What each release changes, newest first: `lsquare` on npm, `lsquare` on PyPI, and `@lsquare/node`,
the 0.1 SDK, on npm.

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
