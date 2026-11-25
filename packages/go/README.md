# lsquare for Go

Record every tool call your MCP server handles in LSquare. A first beta, for servers built with
the official Go MCP SDK ([`github.com/modelcontextprotocol/go-sdk`](https://github.com/modelcontextprotocol/go-sdk)).
LSquare is an invite-only beta, so [request access](https://lsquare.io) to get an API key.

- One line per MCP server. Recording never fails a tool call or changes its reply, never makes
  the reply wait on `Identify`, redaction or the network, and never panics into your app. (The
  one exception is an error whose methods answer differently each time they are called: see
  [below](#mcp-servers).)
- `Flush` waits up to 2 seconds by default: for events to be sent, then for `OnError` and the
  `Logger` to have been told of what wasn't, which get 50 ms more if the time has run out.
- Common private data is redacted, and long values shortened, before anything leaves your process.
- The root package uses only Go's standard library. Go 1.25 or later, which the MCP SDK needs.
- Events are stored in the United States. If you serve customers in the EU, take that into account.

It sends the same events as the [TypeScript](../lsquare) and [Python](../python) SDKs, redacted
and shortened by the same rules. It doesn't do everything they do yet: see
[not yet supported](#not-yet-supported).

## Quickstart

A server with one tool, served over HTTP on your machine, and one call to it with `curl`.

1. **Install** `lsquare` and the MCP Go SDK, in your module (in a new directory, `go mod init
   example.com/weather` makes one first):

   ```sh
   go get github.com/theagentexperiencecompany/lsquare/packages/go github.com/modelcontextprotocol/go-sdk
   ```

2. **Instrument the server**, in `main.go`:

   ```go
   package main

   import (
   	"context"
   	"log"
   	"net/http"
   	"os"

   	"github.com/modelcontextprotocol/go-sdk/mcp"

   	lsquare "github.com/theagentexperiencecompany/lsquare/packages/go"
   	"github.com/theagentexperiencecompany/lsquare/packages/go/lsquaremcp"
   )

   type forecastInput struct {
   	City string `json:"city"`
   }

   func main() {
   	client := lsquare.New(lsquare.Options{APIKey: os.Getenv("LSQUARE_KEY")})

   	info := &mcp.Implementation{Name: "weather", Version: "1.0.0"}
   	forecast := &mcp.Tool{Name: "get_forecast", Description: "Gives tomorrow's forecast for a city."}
   	server := mcp.NewServer(info, nil)
   	mcp.AddTool(server, forecast, func(ctx context.Context, request *mcp.CallToolRequest, input forecastInput) (*mcp.CallToolResult, any, error) {
   		return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: "Tomorrow in " + input.City + ": sunny, 21°C."}}}, nil, nil
   	})

   	lsquaremcp.Instrument(server, client, &lsquaremcp.Options{Tools: []*mcp.Tool{forecast}, ServerInfo: info})

   	handler := mcp.NewStreamableHTTPHandler(func(*http.Request) *mcp.Server { return server }, &mcp.StreamableHTTPOptions{Stateless: true})
   	http.Handle("/mcp", handler)
   	log.Print("Listening on http://localhost:3000/mcp")
   	log.Fatal(http.ListenAndServe("localhost:3000", nil))
   }
   ```

3. **See the first event, in dry run**, where nothing is sent:

   ```sh
   LSQUARE_DRY_RUN=1 go run .
   ```

   In another terminal, call the tool:

   ```sh
   curl -s http://localhost:3000/mcp -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_forecast","arguments":{"city":"Paris"}}}'
   ```

   The reply is your tool's result, unchanged. The server's terminal shows one line for the call's
   event: its fields and size, never its content or IDs.

   ```
   [lsquare dry-run] {"kind":"tool_call","eventId":"the call","fields":["callId","durationMs","input","protocolVersion","result","server","status","toolDescription","toolName"],"bytes":580}
   ```

4. **Send it.** Stop the server, start it again with your project key instead of dry run, and call
   the tool again:

   ```sh
   LSQUARE_KEY=lsq_your_key go run .
   ```

   Nothing is printed when LSquare accepts an event. A problem is logged as one `[lsquare]` line,
   such as `[lsquare] send_failed: LSquare answered 401; not retried.` for a key it doesn't know.

Without a key, and outside dry run, the client is disabled: nothing is recorded, your server is
left exactly as it is, and one warning says so. The dry-run lines and warnings go to standard
error, so a stdio server's standard output stays clean. [`examples/server`](examples/server) is
this server, with a shutdown that waits for the calls in flight before it sends what is queued.

## MCP servers

```go
client := lsquare.New(lsquare.Options{APIKey: os.Getenv("LSQUARE_KEY")}) // once, for the process
defer client.Shutdown(context.Background())                               // sends what is queued

server := mcp.NewServer(&mcp.Implementation{Name: "orders", Version: "1.0.0"}, nil)
mcp.AddTool(server, searchOrders, search)
lsquaremcp.Instrument(server, client, nil) // the one line per server
```

`Instrument` adds a receiving middleware to the server. Tools added later are covered too. The
first `Instrument` of a server decides what is recorded and with which client, so instrumenting it
twice records each call once. With a disabled client, the server is left exactly as it is.

Where it sits among your own MCP middleware matters. Middleware you added before `Instrument` runs
inside it, so what it changes in a reply is recorded. Middleware you add after runs outside it: what
it puts in the context reaches `Identify`, and a call it refuses is never recorded.

Each `tools/call` that reaches the middleware becomes one `tool_call` event: the tool's name and
description, the server's instructions, the call's arguments (`input`), its result, how it ended
(`status`, and an `error` with its `category`), how long it took, the calling app and protocol
version, and the IDs `Identify` returns. Results are
sent by default, each string cut at 4,000 characters and files kept as their size only; turn inputs
or results off everywhere, or for chosen tools, with `Inputs` and `Results`. An event that can't
be kept is dropped and reported to `OnError`, as when the queue is full. After `Shutdown`, calls
run unrecorded, which is reported once.

The tool runs unchanged, and its reply or error goes back as it came. Before the tool, the wrapper
reads the call (its tool, the calling app, the protocol version), copies its arguments when they
are recorded (up to 256 KB; larger ones are read as the tool returns), copies them all for
`Identify` when you set one, and asks your `Inputs`, `Results`, `Descriptions` and `Instructions`
functions, which must answer at once. As the tool returns, it reads how the call ended and notes what the result holds. The event
is built from there, in a goroutine of its own, so the reply never waits for `Identify`, redaction
or the network. A call the SDK refuses before the middleware, such as one whose JSON it can't
read, makes no event.

To read a call, the wrapper runs as little of your code as it can:

- **Structured content** is recorded when it is JSON already, as `mcp.AddTool` leaves it, or plain
  data: structs, maps with string keys, slices and plain values, up to 10,000 values and 32
  levels. Anything else is left out, and the event's `dropped` says so: content with a type that
  writes its own JSON (a method named `MarshalJSON`, `MarshalText`, `AppendText`, `MarshalJSONTo`
  or `IsZero`, which Go's JSON calls; Go's own `time.Time`, `json.Number` and `json.RawMessage`
  are recorded, and pointers to them), a map whose keys aren't strings, a struct in which two
  fields share a JSON name, or one with a `json` tag beyond a plain name with `omitempty` or
  `omitzero`. Writing those for the event would run your code a second time, or read a field
  Go's JSON leaves out. Plain data is written once more as the tool returns, which takes about
  as long as the MCP SDK then takes to write it for the reply.
- **An error** that is a `*jsonrpc.Error` is read by its fields. Any other is asked for its text
  once (`Error()`), only when the text is recorded, and is passed to `errors.As`, which calls its
  `As` and `Unwrap` methods; the MCP SDK then does the same to write the reply. An error whose
  methods answer differently each time they are called gives the event one answer and the client
  the next.

### Options

| Option | Default | |
|---|---|---|
| `Identify` | | `func(ctx, call) (*Identity, error)`: who the call is for ([below](#who-the-call-is-for)) |
| `Inputs` | every tool | `lsquaremcp.Off` for none, or your own `func(toolName string) bool` |
| `Results` | every tool | The same |
| `Descriptions` | every tool | The same |
| `Instructions` | every tool | `lsquaremcp.Off` leaves the server's instructions out of its calls' events; a function of your own chooses per tool, as for the others ([below](#server-instructions)) |
| `Tools` | | The tools you registered, for their descriptions ([below](#tool-descriptions)) |
| `ServerInfo` | | What you gave `mcp.NewServer`, for the event's `server`. Without it, the server's name is learned from the first `initialize` it answers, which a client on protocol 2026-07-28 never sends |
| `ServerInstructions` | | What you gave `mcp.NewServer` in `ServerOptions.Instructions`, for the event's `serverInstructions` ([below](#server-instructions)) |

### How a call failed

| `error.category` | When |
|---|---|
| `timeout` | The request was cancelled before the tool finished |
| `protocol` | The client got a JSON-RPC error, with its `code`: the handler returned a `*jsonrpc.Error`, or (a handler added with `server.AddTool`) an error that wraps one; or the tool is unknown |
| `thrown` | A handler added with `server.AddTool` returned any other error, or a handler panicked. `error.type` is the name of the error's Go type, when it has one, and `panic` for a panic |
| `tool_error` | The result is marked as an error (`IsError`) |

A tool added with `mcp.AddTool` that returns an error other than a `*jsonrpc.Error` gives
`tool_error`, since the Go SDK turns that error into a result marked as one. It does the same with
arguments it refuses, and the two can't be told apart from the middleware, so both are
`tool_error` here, where the TypeScript SDK says `thrown` and `protocol`.

A failed call's text, in `error.message` and in its result, can quote what the tool was sent and
what it would have returned, so it's recorded only with both `Inputs` and `Results` on. The event
still says how the call failed: `category`, `type` and `code`. A cancelled call's message is the
SDK's own sentence, which quotes nothing, so it is there whatever `Inputs` and `Results` say.

### Who the call is for

```go
lsquaremcp.Instrument(server, client, &lsquaremcp.Options{
	Identify: func(ctx context.Context, call lsquaremcp.Call) (*lsquaremcp.Identity, error) {
		token := call.TokenInfo // the request's verified token, if your server checks one
		if token == nil {
			return nil, nil // nobody to name: the call is recorded without IDs
		}
		org, _ := token.Extra["org_id"].(string)
		return &lsquaremcp.Identity{OrgID: org, UserID: token.UserID}, nil
	},
})
```

`Identify` runs alongside the tool, in its own goroutine, and gets the call: its tool, arguments,
HTTP header, verified token and the ID of its connection. The event waits for it up to a second
after the tool returns. Only what it returns is sent: `OrgID`, `UserID`, `ConversationID` and
`Metadata` (strings, numbers, booleans and `nil`).

- **Read who is calling from `call.TokenInfo` and `call.Header`**, which over Streamable HTTP are
  the current request's. `ctx` is the context the call reached `Instrument`'s middleware with (MCP
  middleware you added before `Instrument` runs after it, so what that adds reaches the tool
  only). On an HTTP session that outlives one request, the MCP SDK gives every call the context
  of the request that opened the session: what your HTTP middleware put in it is that first
  request's.
- **Over the older SSE transport (`mcp.NewSSEHandler`) there is no per-request header or token**:
  the MCP SDK gives the tool neither, so `call.Header` and `call.TokenInfo` are nil, and only
  `ctx` has the token of the request that opened the connection. To tell callers apart request
  by request, serve Streamable HTTP.
- **What it is given is a copy**, so changing it changes nothing for the tool: the arguments, the
  header, the token and its scopes. Two things are shared, to read and not to change: the values
  inside `TokenInfo.Extra` (a list of roles, say), and whatever `ctx` carries.
- **`ctx` ends a second after the tool returns at the latest**, and not when the request does:
  sooner, once nothing waits for the answer any more.

If it returns an error, panics, takes longer or returns an ID that isn't valid, the call is sent as
limited diagnostics: its tool's name, status, duration and error category, with none of the call's
content, no error message, and no `OrgID`, `UserID` or `ConversationID`. `OnError` gets
`identify_failed`. (`BeforeSend` still gets the whole event first; what it changes in the content
is left out too.)

### Tool descriptions

Each event carries the tool's description as agents were listed it (`toolDescription`, cut at
1,000 characters), so you can tell which wording a call was made under. It is the one the server
last listed: the wrapper reads each `tools/list` reply as it goes to the client, and changes
nothing in it. The Go MCP SDK doesn't say which tools a server has, so a call that comes before any
listing has no description unless you pass your tools as `Tools`.

The last listing is the server's, not the caller's: a server that lists a tool under different
descriptions to different callers records the one it listed last, to anyone. Turn `Descriptions`
off for such a tool. Listed descriptions are kept for 10,000 tools at most.

### Server instructions

Each event carries the instructions the server gives clients (`serverInstructions`, cut at 2,000
characters), so you can tell which wording of them a call was made under. A server made without
instructions has none, and neither does a call sent as limited diagnostics.

The Go MCP SDK doesn't say what a server's instructions are, so the wrapper learns them in two ways:

```go
options := &mcp.ServerOptions{Instructions: "Search before you refund."}
server := mcp.NewServer(info, options)
lsquaremcp.Instrument(server, client, &lsquaremcp.Options{ServerInfo: info, ServerInstructions: options.Instructions})
```

- **From the server's own answers** to `initialize` and `server/discover`, read as they go to the
  client and left unchanged. A call carries the instructions its own connection was given.
- **From `ServerInstructions`,** what you gave `mcp.NewServer`, for a call on a connection that was
  given none. On a stateless server every request is such a connection, so set it there: with it,
  every call has the instructions from the first one.

Without `ServerInstructions`, a call on a connection that was given none carries the ones the
server last gave any client, and none before the server has answered one. So a server that tells
different clients different things, from middleware of its own, should set `ServerInstructions`
or turn `Instructions` off, or one client's wording can be recorded on another's call.

As for replies to calls, the wrapper sees an answer as it passes its place among your MCP
middleware: what middleware added before `Instrument` changes in the instructions is recorded,
what middleware added after it changes is not.

`Instructions: lsquaremcp.Off` records none.

## What leaves your process

- Strings in a call's `input` are cut at 16,000 characters, in its result at 4,000, the server's
  instructions at 2,000 and a tool's description at 1,000; a cut string ends with `…[+N chars]`, or `…[cut]` once an event's work
  budget for reading strings is spent. Lists and objects keep their first 100 entries, and nesting
  stops at 10 levels. Images, audio and binary resources are kept as their size only, and so is a
  file sent as text: a `data:` URI in base64, or 1,000 characters or more of nothing but base64.
- An event still over 256 KB loses parts, in this order: the server's instructions, its tool's
  description, then its result, then its input. The event's `truncated` field names what was shortened or left out. Metadata is
  never shortened: an event whose metadata is too large for it is dropped, and reported as
  `too_large`.
- Common formats of emails, phone numbers, card numbers, API keys and tokens, credentials in URLs,
  and IP addresses become numbered placeholders such as `[EMAIL_1]`, in inputs, results, errors,
  tool descriptions and server instructions alike. Each pattern can be turned off: `SkipRedaction: []lsquare.Pattern{lsquare.IP}`,
  or `lsquare.AllPatterns()` for all.
- Values under secret key names (`password`, `token`, `apiKey` and the like) are replaced whole, in
  structured data and in JSON held in a string, such as a text block or an error message:
  `{"password":"hunter2"}` becomes `{"password":"[SECRET_1]"}`, and the rest of the string stays.
  `RedactKeys: []string{"name", "address"}` adds your own: matched on their ASCII letters and digits
  only, ignoring case, by the end of the key, so `name` also covers `firstName` and `fileName`.
- `BeforeSend` sees every event the client takes, before the patterns above are applied, and can
  change it or drop it by returning `nil`. (An event dropped while it waits for the hook, at the
  hook's deadline or for room in the queue, is never shown to it.) Values under secret key names are already replaced, and
  a string cut for length has had its kept part redacted. What it returns is shortened and
  redacted again, and is what is sent: an input, result, description or instructions it adds are
  sent even where `Inputs`, `Results`, `Descriptions` or `Instructions` left them out.
- The wrapper never records HTTP headers, tokens, environment variables or anything else of the
  request by itself, except the calling app and the protocol version. `Identify` can read the
  rest; only what it returns is sent.
- IDs (`OrgID`, `UserID`, `ConversationID`, the tool's name) are never redacted. Each must be at
  most 512 bytes. Send internal IDs, not email addresses: LSquare deletes a person's events by
  their `UserID`.

The rules are the ones the TypeScript and Python SDKs follow
([REDACTION.md](../contract/REDACTION.md)). This SDK's tests run every shared case of the three
SDKs, and compare its events with the TypeScript SDK's on generated inputs.

## Client options

| Option | Default | |
|---|---|---|
| `APIKey` | | Project key. Without it, the client is disabled |
| `Endpoint` | `LSQUARE_ENDPOINT`, then `https://ingest.lsquare.io` | https only, except localhost |
| `DryRun` | `LSQUARE_DRY_RUN` (`1` or `true`) | `lsquare.DryRunOn`: run everything but send nothing, with or without a key. `lsquare.DryRunOff`: send, whatever the environment says |
| `FlushTimeout` | 2 s | How long `Flush` waits when its context has no deadline. Negative: it doesn't. `Shutdown` waits up to 5 s, or this when it is longer |
| `BeforeSend` | | `func(ctx, *Event) (*Event, error)`: redact more, or drop an event |
| `BeforeSendTimeout` | `FlushTimeout`, or 2 s when that is negative | Hook deadline |
| `MaxConcurrentHooks` | 16 | `BeforeSend` calls running at once, and `Identify` calls, each |
| `MaxConcurrentRequests` | 4 | Requests in flight at once; other events wait, and go together. One unanswered after 10 seconds (`RequestTimeout`) is given up, and no longer counts |
| `MaxQueueEvents` | 10,000 | Events held at once, waiting to be sent or in a request, with the calls whose events are still being built. Past it, the oldest are dropped and reported as `queue_full`: those not yet in a request first, and never one inside `BeforeSend` |
| `MaxQueueBytes` | 10 MB | Their size together: an event's JSON, and the arguments and result of a call still being built. Past it, the same. A call that holds more than this by itself isn't recorded |
| `SkipRedaction` | none | Patterns to turn off: `lsquare.Email`, `Phone`, `Card`, `Secret`, `IP` |
| `RedactKeys` | | More key names whose values are replaced whole |
| `OnError` | logs codes, once a minute | `func(*Error, *Event)`. Receives a redacted copy of the event, or `nil`. About 100 reports a second at most (100 at once, then 100 a second), and 100 waiting for it; `Error.Suppressed` counts the ones left out |
| `Logger` | standard error | `Info(string)` and `Warn(string)` |
| `HTTPClient` | `http.Client{}` | Used through a copy: redirects are never followed, whatever its own policy, and its cookie jar isn't used |

Events go to LSquare in batches of at most 100, and one that fails with a network error, a 429 or
a 5xx is sent again, three times in all (`MaxAttempts`). Call `client.Shutdown(ctx)` before your process exits: it
sends what's queued, waiting until `ctx`'s deadline, or 5 seconds (`FlushTimeout`, when that is
longer), and drops events still waiting for `BeforeSend` or inside it. On a host that stops your
process once a request is served, wrap your HTTP handler and call `client.Flush(ctx)` after it
has served each request: the call's event is built once the tool has returned, and `Flush` waits
for it. At its deadline `Flush` stops waiting for events, then gives `OnError` and the `Logger` up
to 50 ms more to be told what is still pending, and returns.

## Not yet supported

This is a first beta. The TypeScript and Python SDKs also have these, and this one doesn't yet:

- **The extras**: `session_id` and `intent` inputs, and the `get_more_tools` tool. So calls aren't
  grouped into tasks, and an event has no `sessionId`, `intent` or `extra`.
- **`capture()` and turn events**: an agent's turns, outside MCP or from inside a tool, so a call's
  event has no `agent` part or `turnRef`.
- **Other MCP libraries**, such as `mark3labs/mcp-go`.
- **Tasks**: a call that creates a task is recorded without its `task`.
- **Sending handed to the host**: `flushBeforeReply` and `waitUntil`. Call `Flush` yourself where
  a host needs it, as [above](#client-options).

## Differences from the other SDKs

For the same input, this SDK builds the event the TypeScript SDK builds, field for field, apart
from `sdk`. (An event is measured with its `sdk`, which isn't the same length in each SDK, so
right at the 256 KB limit one SDK can keep a part another leaves out.) What differs comes from Go
and its MCP SDK:

- The server's instructions come from the server's answers to `initialize` and `server/discover`,
  or from `ServerInstructions` ([above](#server-instructions)), where the other SDKs read them
  from the server as each call arrives.
- A tool added with `mcp.AddTool` that returns an error other than a `*jsonrpc.Error`, or whose
  arguments are refused, is a `tool_error`, where the TypeScript SDK says `thrown` and `protocol`
  ([above](#how-a-call-failed)).
- Structured content is recorded only when it is JSON already or plain data
  ([above](#mcp-servers)).
- A Go string can't hold half of a surrogate pair, which JSON can write as `\ud800`. Go reads one
  as U+FFFD, as its JSON decoder does, where the other SDKs keep it. Two secrets that differ only
  there then share one placeholder number, and such a string takes fewer bytes.
- Binary data in a tool's structured content is recorded as the base64 text the client gets, cut
  like any string, or as its size from 1,000 characters on. The other SDKs leave a byte buffer
  out.
- An empty `requestState` on a call can't be told from none, so such a call isn't marked as a
  continuation.

## Licence

MIT.
