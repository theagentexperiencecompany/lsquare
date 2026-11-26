# lsquare for Go

Record every tool call your MCP server handles in LSquare, and your agent's turns. A first beta,
for servers built with the official Go MCP SDK
([`github.com/modelcontextprotocol/go-sdk`](https://github.com/modelcontextprotocol/go-sdk)).
LSquare is an invite-only beta, so [request access](https://lsquare.io) to get an API key.

- One line per MCP server. Recording never fails a tool call, never panics into your app, and
  doesn't make the reply wait on `Identify`, redaction or the network unless you ask it to
  ([`FlushBeforeReply`](#client-options), or `Flush` inside a tool). (The one exception is an
  error whose methods answer differently each time they are called: see [below](#mcp-servers).)
- Three [extras](#the-extras), on by default: calls grouped into tasks, why each call was made,
  and what agents looked for and didn't find. They are the one thing that changes what your
  server shows its callers ([what they change](#what-the-defaults-change-in-your-server)).
- `Capture` records an agent's turn, outside MCP, and never waits on the network
  ([agent turns](#agent-turns)).
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

   	lsquaremcp.Instrument(server, client, &lsquaremcp.Options{ServerInfo: info})

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

   The reply is your tool's result, followed by one line that gives the agent a task ID to send
   back on its later calls: one of [the extras](#the-extras). The server's terminal shows one
   line for the call's event: its fields and size, never its content or IDs.

   ```
   [lsquare dry-run] {"kind":"tool_call","eventId":"the call","fields":["callId","durationMs","input","protocolVersion","result","server","sessionId","sessionSource","status","toolDescription","toolName"],"bytes":646}
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
version, the IDs `Identify` returns, and, with the extras, the task the call belongs to and why it
was made. Results are
sent by default, each string cut at 4,000 characters and files kept as their size only; turn inputs
or results off everywhere, or for chosen tools, with `Inputs` and `Results`. An event that can't
be kept is dropped and reported to `OnError`, as when the queue is full. After `Shutdown`, calls
run unrecorded, which is reported once.

The tool runs unchanged, and its reply or error goes back as it came, but for what
[the extras](#what-the-defaults-change-in-your-server) do. Before the tool, the wrapper
reads the call (its tool, the calling app, the protocol version), copies its arguments when they
are recorded (up to 256 KB; larger ones are read as the tool returns), copies them all for
`Identify` when you set one, and asks your `Inputs`, `Results`, `Descriptions` and `Instructions`
functions, which must answer at once. As the tool returns, it reads how the call ended and notes what the result holds. The event
is built from there, in a goroutine of its own, so the reply doesn't wait for `Identify`, redaction
or the network, unless it is asked to ([inside a tool](#inside-a-tool)). A call the SDK refuses before the middleware, such as one whose JSON it can't
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

### What the defaults change in your server

Three extras are on by default, so calls are grouped into tasks, each call says why it was made,
and agents can tell you what they couldn't find. They change what your server shows its callers:

- most tools gain two optional inputs, `session_id` and `intent`, which the wrapper takes out of
  each call before the SDK checks it, so your code never sees them
  ([which tools](#which-tools-get-the-inputs));
- some results gain one text block at the end, giving the agent a task ID;
- the server gains a tool, `get_more_tools`, which the wrapper answers itself.

Clients and scanners that flag changed tool definitions may warn your server's users once when you
add the SDK. Each extra can be turned off by itself:

```go
lsquaremcp.Instrument(server, client, &lsquaremcp.Options{
	Extras: lsquaremcp.Extras{
		DisableSessions:     true, // no session_id, no task ID line, no grouping by connection
		DisableIntent:       true, // no intent
		DisableMissingTools: true, // no get_more_tools
	}, // leave any of them out to keep it on
})
```

With all three off, the wrapper never changes a listing, a call's arguments or a result, and never
asks your server for its tools. [The extras](#the-extras) says exactly what each one does.

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
| `FlushBeforeReply` | `false` | Each reply waits for its call's event to be sent, up to the client's `FlushTimeout` ([below](#client-options)) |
| `Extras` | all on | `lsquaremcp.Extras{DisableSessions, DisableIntent, DisableMissingTools}` turns each of [the extras](#the-extras) off; `OpenSchemas` gives the inputs to more tools ([below](#which-tools-get-the-inputs)) |

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
is left out too.) One exception: a turn captured inside the tool that gives each ID `Identify`
last returned in time, on any server instrumented with the same client (a `UserID`, before it
has returned any), keeps the call's event whole, with the capture's IDs
([below](#inside-a-tool)). A call with no such capture is limited diagnostics, whatever
`Identify` returned before.

### Tool descriptions

Each event carries the tool's description as agents were listed it (`toolDescription`, cut at
1,000 characters), so you can tell which wording a call was made under. It is the one the server
last listed: the wrapper reads each `tools/list` reply as it goes to the client, and changes
nothing of a tool's description in it. The Go MCP SDK doesn't say which tools a server has, so
for a call to a tool it hasn't seen listed the wrapper lists the tools itself
([below](#how-the-wrapper-knows-your-tools)). With the extras all off it doesn't, and such a call
has no description unless you pass your tools as `Tools`.

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

## The extras

### `session_id`: calls grouped into tasks

A tool that gets the inputs takes an optional `session_id`, described to the agent as: "The ID of
the task this call belongs to. Leave it out on your first call. On every later call for the same
task, send the ID an earlier result gave you, and pass it to any subagent working on that task."
The event's `sessionId` is the task, and its `sessionSource` says where the ID came from:

- **`sent`**: the call came with a valid ID (`ses_` and 22 letters, digits, `-` or `_`). Any
  other value is taken out and ignored.
- **`protocol`**: the call came from a client on a protocol version before 2026-07-28, and is
  grouped by its connection: a Streamable HTTP server's own session, recorded as a hash of its
  ID, or one ID per stdio, SSE or in-memory connection. Every tool's calls are grouped this way,
  the ones that don't get the inputs included, unless the first turn captured inside the call
  gives its conversation ([inside a tool](#inside-a-tool)), which groups it instead. A stateless server has no connection to group
  by; neither has an HTTP server run with `MCPGODEBUG=allowsessionsinstateless=1`, where a
  session's ID can be one the client chose.
- **`issued`**: any other call to a tool that gets `session_id`, and whose first turn captured
  inside it gives no conversation, gets a new ID, in a text block at
  the end of the result: "Task ID: ses_…. Send it as session_id on later calls for this task,
  and pass it to any subagent working on it." The block goes at the end, so code that reads the
  first block still gets your tool's output, and the recorded result is your tool's, without it.
  A result marked as an error never gets one (it can be the SDK's own, for arguments it
  refused), and neither does a result with no content, a JSON-RPC error, a cancelled call, a
  panic, or a round that asks the client for more.

Clients that read only `structuredContent` don't see the text block, so they never learn an ID to
send back; their calls are still grouped by their connection where there is one. Agents decide
for themselves whether to send the ID back:
[what agents do with them](../lsquare/README.md#what-agents-do-with-them) says what the ones
checked so far did, on the TypeScript and Python SDKs.

### `intent`: why each call was made

Described as: "In one sentence, why you're calling this tool and what the user is trying to do.
No personal data or secrets." When it's a non-empty string it's recorded, redacted and cut at 500
characters, whether or not the call's inputs are; anything else sent as `intent` is taken out and
ignored. It stays optional, so strict clients never refuse a call that leaves it out.

### `get_more_tools`: what agents looked for

A server with at least one tool of its own gets one more, `get_more_tools`, marked as read-only,
since nothing on your server changes, and as reaching outside the server, since what the agent
writes leaves it, with a required `need` input (and the optional `session_id`): "Call this when
none of the available tools fits what you need, or one fits only in part. It doesn't return new
tools: it tells the server's developers what to add." The wrapper answers it ("Noted. No other
tools are available right now.") and records the request as a `tool_call` event with
`extra: "missing_tools"`, which is queued and sent like any other event. With `Inputs` off for it, the server doesn't get the tool, since the
request wouldn't be recorded; where that changes later, or after `Shutdown`, the tool answers as
the SDK does for a tool the server doesn't have. Clients that cap the number of tools across
servers count it.

Nothing is added to your server: the wrapper shows the tool at the end of your server's tool
list, and answers its calls. A server with its own `get_more_tools` keeps it. Where your
server's listing has one, LSquare's isn't shown; and a call to `get_more_tools` always goes to
your server first, so a tool of yours under that name answers it, listed or not. The wrapper
answers only a call your server answers exactly as the SDK answers a call to a tool it doesn't
have: an error of your own, with another code, another message or data of its own, goes back as
it is.

### Which tools get the inputs

The wrapper can't read a tool's code, only the schema your server lists for it. So a tool gets
the inputs only where that schema is enough to know the tool isn't using them itself:

- **Its schema refuses the inputs it doesn't name**: it says `"additionalProperties": false`.
  That is what the SDK infers for a tool added with `mcp.AddTool` and a struct for its input, so
  those tools get the inputs with nothing to do. A schema written by hand gets them when it says
  so too.
- **Its schema is a plain object of named properties.** One with a combinator, a pattern, a
  limit on the number of properties or a reference to itself is left alone, since one more input
  could change what it accepts; so is one whose input is a map, which takes any key.
- **A tool that already has an input called `session_id` or `intent` keeps it**, untouched, and
  gets only the other.

A tool that is left alone is exactly as it was: its listing, its calls and its results. It is
logged once, with the reason, and its calls are still recorded, and grouped by their connection.

The schema is taken at its word. A tool whose own code takes an input that its listed schema
refuses, which a handler added with `server.AddTool` is free to do, no longer gets a
`session_id` or an `intent` it was reading that way: list them in its schema, and it keeps them.

For the tools with a schema written by hand that doesn't say `"additionalProperties": false`, you
can add that line to the schema, or tell the wrapper to take every such schema at its word:

```go
lsquaremcp.Instrument(server, client, &lsquaremcp.Options{Extras: lsquaremcp.Extras{OpenSchemas: true}})
```

Do that only if none of those tools reads an input its schema doesn't list: with `OpenSchemas`,
a tool that reads a `session_id` or an `intent` of its own without listing it no longer gets it.

### How the wrapper knows your tools

The Go MCP SDK doesn't say what tools a server has, so the wrapper learns them from the tool
lists your server sends, as they pass. Three things follow:

- **A call can come before the wrapper has seen a tool list**: on a new process, or on a server
  built for each request, from an agent that was listed the tools earlier. The wrapper then
  reads your server's whole tool list itself, once per server, in a request of its own. A call
  waits for it one second at most, and otherwise goes on as it came; if it fails, or takes more
  than 10 seconds, the wrapper doesn't ask again for a minute. That request runs the MCP
  middleware you added before `Instrument`, like any other, with copies of the call's headers,
  token and metadata: add yours after `Instrument` if it shouldn't. It runs while the call that
  started it waits, so on a server whose tool listing waits for something its own middleware
  holds for as long as a tool call runs, neither can go on: turn the extras off there.
- **A tool is known by its name, as it was last listed.** If your server replaces a tool with
  another under the same name while it runs, the wrapper follows the old tool's schema until the
  new one is listed, which happens as soon as a client asks for the tool list again. And if your
  server lists a tool under different schemas to different callers, the wrapper goes by the one
  it listed last, to anyone: turn `session_id` and `intent` off on such a server.
- **Where your own MCP middleware sits matters.** Middleware you added before `Instrument` sees a
  call without the two inputs, and the result before its task ID line; middleware you add after
  sees both.

## Agent turns

An agent that isn't behind an MCP server, or the agent of your app, records its turns with
`Capture`:

```go
client.Capture(ctx, lsquare.Turn{
	OrgID: orgID, UserID: userID, ConversationID: conversationID, TurnID: turnID,
	Question: question, Answer: answer,
	ToolCalls: []lsquare.ToolCall{{Name: "search_orders", Input: input, DurationMs: lsquare.Ptr(41.5)}},
	Usage:     &lsquare.Usage{InputTokens: lsquare.Ptr(812), OutputTokens: lsquare.Ptr(240), CostUSD: lsquare.Ptr(0.004)},
})
```

Every field is optional, and one left empty, a nil pointer, slice or map, is left out of the
event; `lsquare.Ptr` makes the pointer for an optional number. A turn is sent as a `turn` event,
with the client's other events: `Capture` reads it as it is called, so what you change afterwards
changes nothing, and never waits on the network. It is shortened and redacted by the same rules
as a tool call ([what leaves your process](#what-leaves-your-process)). A turn still over 256 KB
loses whole parts, in this order: its tool calls, its answer, its question, `Verification`, then
`Latency`.

`BeforeSend` gets it, with `Event.Kind` `"turn"` and the turn in `Event.Turn`: change the turn's
content there, its status and error included, and its metadata in `Event.Metadata`, where
`Event.Turn.Metadata` is nil. Its IDs stay the event's.

- **IDs.** `OrgID`, `UserID` and `ConversationID` are the same as `Identify`'s: internal IDs, at
  most 512 bytes each. A turn whose ID isn't valid is dropped and reported as `invalid_id`.
- **Duplicates.** A turn with a `ConversationID` and a `TurnID` gets the same event ID every time
  it is captured with them and the same `OrgID`, so a retry or a re-import is recognised as a
  duplicate. Without both, its ID is random; so is it with a `TurnID` that isn't valid UTF-8 or
  is over 256 KB as JSON text, which is left out and noted in `dropped` while the rest of the
  turn is sent (an `OrgID`, `UserID` or `ConversationID` that isn't valid drops the whole turn).
- **Tool calls.** `ToolCalls` nil says nothing of them; an empty slice says the agent called
  none. A tool call's `Input` is JSON, and its result is never recorded, only its size
  (`ResultChars`).
- **JSON of your own.** `Latency` and `Verification` are JSON as you'd have them in the
  dashboard; JSON that isn't valid is left out, and noted in `dropped`.
- **`CallID`** links a turn to a tool call of an instrumented server, by the call's event ID: for
  a turn captured after the call, in a background job say. Inside the tool,
  `client.CurrentCallID(ctx)` gives it.
- **Callbacks.** A turn captured from `BeforeSend` with the context it was given counts as one of
  the clients' own events: past two levels of that, a capture is ignored, so a hook that captures
  a turn for each event it is given stops, also when it captures on another client. A turn
  captured from `OnError` or the `Logger`, as they run, is ignored, as each would make another
  report or line.
- **Shutdown.** Once `Shutdown` has begun, a turn is dropped, which is reported once.

### Inside a tool

When a tool on an instrumented server runs an agent, `Capture` with the tool's own `ctx` adds the
agent's turn to the call's event:

```go
mcp.AddTool(server, ask, func(ctx context.Context, request *mcp.CallToolRequest, input askInput) (*mcp.CallToolResult, any, error) {
	answer, usage := runAgent(ctx, input.Question)
	client.Capture(ctx, lsquare.Turn{ConversationID: conversationID, TurnID: turnID, Question: input.Question, Answer: answer, Usage: usage})
	return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: answer}}}, nil, nil
})
```

- **The first capture before the tool returns is the call's `agent` part**, read and shortened
  as it is called, as the call's event will hold it: what the call keeps of a long answer is
  what the event sends. Its `OrgID`, `UserID` and `ConversationID` fill the call's, each winning
  over `Identify`'s, and its metadata is merged with `Identify`'s, winning key by key. With a
  `ConversationID` and a `TurnID`, the call's event has a `turnRef` to the turn. A capture from
  another goroutine that is still being read as the tool returns is too late: it is a turn of
  its own, and none of this applies to it (nor, if `Identify` fails, does the call's reduction
  to limited diagnostics: finish such a capture before the tool returns).
- **Its `ConversationID` groups the call** into a task: the call is given no session of its
  connection's and no task ID line; a `session_id` the agent sent still names its session. A
  `ConversationID` that isn't a valid ID still does, and then drops the call's event as
  `invalid_id`.
- **Its `turnRef`** needs a valid `ConversationID` and `TurnID` of the capture's own (one
  `Identify` returns doesn't count) and, if the call has one, a valid `OrgID`. A call sent as
  limited diagnostics has neither the agent part nor the reference.
- **`BeforeSend` gets the agent part** in `Event.Agent` and the reference in `Event.TurnRef`. It
  may change what the agent says, or remove it by setting it to nil, but not its turn ID or the
  reference.
- **Any other capture is a turn of its own**, linked to the call by `callId` unless it gives a
  `CallID` of its own: a second one before the tool returns, and one after it, from work the
  tool left running with its `ctx`.
- **`Flush` with the tool's `ctx`** sends what else is queued, and makes the reply wait for the
  call's own event, and for the turns captured before it returned ([client
  options](#client-options)).
- **Only the client the server is instrumented with** sees the call: a capture on another client
  is a turn of that client's, not linked to the call, and so is one made with a context that
  isn't the tool's (one `Identify` or `BeforeSend` was given, or a new `context.Background()`).
- **The call ends as the wrapper's middleware gets the reply**: a typed tool's structured output
  is written for the reply before then, and middleware you added before `Instrument` runs before
  then too, so a capture from either still counts as before the tool returned. So does one from
  a handler the tool runs itself with its `ctx`: the call is one call, the outer tool's. And on
  a protocol version before 2026-07-28, where the SDK runs a tool again in the same call once the
  client has answered what it asked for, both runs are the same call: a capture from work the
  first run left going can be the call's agent part.

## What leaves your process

- Strings in a call's `input` are cut at 16,000 characters, in its result at 4,000, the server's
  instructions at 2,000, a tool's description at 1,000 and a call's `intent` at 500; a cut string ends with `…[+N chars]`, or `…[cut]` once an event's work
  budget for reading strings is spent. Lists and objects keep their first 100 entries, and nesting
  stops at 10 levels. Images, audio and binary resources are kept as their size only, and so is a
  file sent as text: a `data:` URI in base64, or 1,000 characters or more of nothing but base64.
- An event still over 256 KB loses parts, in this order: the server's instructions, its tool's
  description, then its result, then its input, then its agent part's tool calls, then the
  agent part. The event's `truncated` field names what was shortened or left out. Metadata is
  never shortened: an event whose metadata is too large for it is dropped, and reported as
  `too_large`.
- Common formats of emails, phone numbers, card numbers, API keys and tokens, credentials in URLs,
  and IP addresses become numbered placeholders such as `[EMAIL_1]`, in inputs, results, errors,
  intents, tool descriptions and server instructions alike. Each pattern can be turned off: `SkipRedaction: []lsquare.Pattern{lsquare.IP}`,
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
  rest; only what it returns is sent. A Streamable HTTP session's ID, which can be a live handle
  to the session, is sent as its SHA-256 hash only: as the event's `sessionId`, and in a call's
  `input` and `intent` wherever a text there repeats it. What a tool returns is the tool's.
- An `intent` is recorded whether or not the call's inputs are: `DisableIntent` is what stops it.
  What an agent writes as `need` to `get_more_tools` is that tool's input, recorded like any
  other: with `Inputs` off for it, the server doesn't get the tool.
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
| `FlushTimeout` | 2 s | How long `Flush` waits when its context has no deadline. Negative: it doesn't. `Shutdown` waits up to 5 s, or this when it is longer. Also how long a reply waits for its event, with `FlushBeforeReply` or a `Flush` inside the tool |
| `BeforeSend` | | `func(ctx, *Event) (*Event, error)`: redact more, or drop an event |
| `BeforeSendTimeout` | `FlushTimeout`, or 2 s when that is negative | Hook deadline |
| `MaxConcurrentHooks` | 16 | `BeforeSend` calls running at once, and `Identify` calls, each |
| `MaxConcurrentRequests` | 4 | Requests in flight at once; other events wait, and go together. One unanswered after 10 seconds (`RequestTimeout`) is given up, and no longer counts |
| `MaxQueueEvents` | 10,000 | Events held at once, waiting to be sent or in a request, with the calls whose events are still being built. Past it, the oldest are dropped and reported as `queue_full`: those not yet in a request first, and never one inside `BeforeSend` |
| `MaxQueueBytes` | 10 MB | Their size together: an event's JSON, and the arguments, result and captured turn (as already shortened) of a call still being built. Past it, the same. A call that holds more than this by itself isn't recorded |
| `SkipRedaction` | none | Patterns to turn off: `lsquare.Email`, `Phone`, `Card`, `Secret`, `IP` |
| `RedactKeys` | | More key names whose values are replaced whole |
| `OnError` | logs codes, once a minute | `func(*Error, *Event)`. Receives a redacted copy of the event, or `nil`. About 100 reports a second at most (100 at once, then 100 a second), and 100 waiting for it; `Error.Suppressed` counts the ones left out |
| `Logger` | standard error | `Info(string)` and `Warn(string)` |
| `HTTPClient` | `http.Client{}` | Used through a copy: redirects are never followed, whatever its own policy, and its cookie jar isn't used |

Events go to LSquare in batches of at most 100, and one that fails with a network error, a 429 or
a 5xx is sent again, three times in all (`MaxAttempts`). Call `client.Shutdown(ctx)` before your process exits: it
sends what's queued, waiting until `ctx`'s deadline, or 5 seconds (`FlushTimeout`, when that is
longer), and drops events still waiting for `BeforeSend` or inside it. At its deadline `Flush`
stops waiting for events, then gives `OnError` and the `Logger` up to 50 ms more to be told what
is still pending, and returns.

On a host that stops your process once a request is served, such as a serverless function, an
event still queued when the reply goes is lost. Make each reply wait for its call's event with
`FlushBeforeReply`:

```go
lsquaremcp.Instrument(server, client, &lsquaremcp.Options{FlushBeforeReply: true})
```

The reply then waits, from when the tool returns and up to the client's `FlushTimeout` (2 seconds
by default), for the call's event to be built and then sent or dropped, and for the turns
captured inside the call before it returned that are still on their way (100 at most at once).
`Identify`'s second is cut to that wait when it is shorter. The reply goes at the deadline, or
when the request is cancelled, whatever is left: an event still on its way is sent if the
process lives on, and one whose retry falls past the deadline doesn't hold the reply. A negative
`FlushTimeout` waits for nothing, and leaves `Identify` its second. A tool that panics is waited
for the same way before the panic goes on. A tool can make its own reply wait so:
`client.Flush(ctx)` inside the tool, with the tool's `ctx`, sends what else is queued, and the
reply waits for the call's own event in the same way, within the same `FlushTimeout`; it doesn't
wait for `OnError` or the `Logger` to be told of what wasn't sent, as an ordinary `Flush` does.
That holds until the call's dispatch ends: after it, with a `ctx` kept, `Flush` is an ordinary
one, and a `Flush` from `Identify`, `BeforeSend`, `OnError` or the `Logger` marks no call.

## Not yet supported

This is a first beta. The TypeScript and Python SDKs also have these, and this one doesn't yet:

- **Other MCP libraries**, such as `mark3labs/mcp-go`.
- **Tasks**: a call that creates a task is recorded without its `task`.

## Differences from the other SDKs

For the same input, this SDK builds the event the TypeScript SDK builds, field for field, apart
from `sdk`. (An event is measured with its `sdk`, which isn't the same length in each SDK, so
right at the 256 KB limit one SDK can keep a part another leaves out.) What differs comes from Go
and its MCP SDK:

- The server's instructions come from the server's answers to `initialize` and `server/discover`,
  or from `ServerInstructions` ([above](#server-instructions)), where the other SDKs read them
  from the server as each call arrives.
- The other SDKs read each tool's own definition to know which get the `session_id` and
  `intent` inputs. The Go MCP SDK doesn't show it, so this one goes by the schema the server
  lists, only where that schema refuses the inputs it doesn't name, and learns the tools from the
  server's listings ([above](#which-tools-get-the-inputs)).
- A result marked as an error never gets a task ID, where the TypeScript SDK gives one on a
  tool's own error result: here it can't be told from the SDK's refusal of the arguments. A
  result with no content gets none either.
- A call to `get_more_tools` goes to the server first, which says it has no such tool, before
  the wrapper answers it: the MCP middleware added before `Instrument` sees that call fail.
- A tool added with `mcp.AddTool` that returns an error other than a `*jsonrpc.Error`, or whose
  arguments are refused, is a `tool_error`, where the TypeScript SDK says `thrown` and `protocol`
  ([above](#how-a-call-failed)).
- Structured content is recorded only when it is JSON already or plain data
  ([above](#mcp-servers)).
- A Go string can't hold half of a surrogate pair, which JSON can write as `\ud800`. Go reads one
  as U+FFFD, as its JSON decoder does, where the other SDKs keep it. Two secrets that differ only
  there then share one placeholder number, and such a string takes fewer bytes.
- A turn's text left empty is left out, where the other SDKs keep an empty string they are
  given; and a turn's `TurnID` is text, where they also take a number that is a safe integer
  (7 and "7" are the same turn in all of them).
- There is no `waitUntil`, which hands a call's sending to a JavaScript host such as Cloudflare
  Workers: `FlushBeforeReply` is the way to keep a reply until its event is sent.
- A call still being built counts towards `MaxQueueBytes` with what it holds (its arguments,
  its result, and the turn captured inside it, already shortened), where the other SDKs count
  only queued events.
- A failed `Identify` keeps the call whole only when a turn was captured inside it, where the
  other SDKs also do so when `Identify` last returned no ID at all.
- A call is one call from the wrapper's middleware to its reply: on a protocol version before
  2026-07-28, the runs of a tool the SDK makes in one call share its agent part and its `Flush`
  ([inside a tool](#inside-a-tool)), where the other SDKs give each run its own, though all of
  them record one call.
- Metadata given as a Go map, by `Identify`, `Capture` or `BeforeSend`, is read in its keys'
  sorted order, where the other SDKs read an object in the order its keys were set: two values
  that are redacted can be numbered the other way round (`[EMAIL_1]`, `[EMAIL_2]`).
- Binary data in a tool's structured content is recorded as the base64 text the client gets, cut
  like any string, or as its size from 1,000 characters on. The other SDKs leave a byte buffer
  out.
- An empty `requestState` on a call can't be told from none, so such a call isn't marked as a
  continuation.

## Licence

MIT. `lsquaremcp` includes code adapted from AgentCat's TypeScript SDK, also MIT: see
`THIRD_PARTY_NOTICES`.
