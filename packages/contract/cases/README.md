# Shared test cases

Each file is a list of cases that say "this input gives this event". The TypeScript SDK
(`packages/lsquare/test/cases.test.ts`) and the Python SDK must both pass every one, so the two
turn the same input into the same event: the same cuts, IDs, placeholders and numbers (spec
section 2, "Same in every language").

A case has a `name`, an input, and either the `event` it gives or the `error` code that drops it:

- `turn`: what `capture()` gets, outside an instrumented call.
- `call`: what the wrapper hands the client for one `tools/call`. Fields left out default to
  `callId` and event ID `"case-call"`, `toolName` `"search_orders"`, `status` `"completed"`,
  `durationMs` 1, no IDs, and not diagnostics. `input` is the call's arguments and `result` MCP's
  result, as the client got them.
- `options`, optional: `redact` and `redactKeys`, as the client options.

Every event is built with `capturedAt` `"2026-10-28T00:00:00.000Z"` and `sdk` left out of the
comparison, since its `language` and `runtime` differ. `"$uuid"` as an `eventId` means a random
UUID. Everything else must match exactly, objects compared regardless of key order.

Long strings are written compactly, anywhere in a case, input or expected:

- `{"$prose": n}`: `"lorem ipsum "` repeated, cut to `n` characters
- `{"$repeat": [text, n]}`: `text` `n` times
- `{"$concat": [part, ...]}`: the parts joined, each a string or one of these
- `{"$range": n}`: the list `[0, 1, ..., n - 1]`
- `{"$keys": n}`: the object `{"k0": 0, "k1": 1, ..., "k<n-1>": n - 1}`

Non-ASCII characters are written as `\u` escapes, including lone surrogates and the pairs of
characters outside the Basic Multilingual Plane. Characters are counted as code points. A pair whose
halves are in separate `$concat` parts is one character, as in JavaScript: a Python runner holds it
as two code points, and the SDK joins them.

Numbers are JavaScript's: an integer past 2^53 is read as the nearest double, and one too large for
a double as infinite. A Python runner keeps such integers exact, so the SDK's own rule makes them
JavaScript numbers.

Objects are read in JavaScript's order: integer-like keys (`"2"`, `"10"`) first, ascending, then the
others in the order written. A JSON parser that keeps the file's order must reorder them first.
