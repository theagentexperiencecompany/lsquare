// Package lsquaremcp records every tool call of a server built with the official Go MCP SDK
// (github.com/modelcontextprotocol/go-sdk).
//
//	server := mcp.NewServer(&mcp.Implementation{Name: "orders", Version: "1.0.0"}, nil)
//	client := lsquare.New(lsquare.Options{APIKey: os.Getenv("LSQUARE_KEY")})
//	defer client.Shutdown(context.Background())
//	lsquaremcp.Instrument(server, client, &lsquaremcp.Options{
//		Identify: func(ctx context.Context, call lsquaremcp.Call) (*lsquaremcp.Identity, error) {
//			if call.TokenInfo == nil {
//				return nil, nil
//			}
//			return &lsquaremcp.Identity{UserID: call.TokenInfo.UserID}, nil
//		},
//	})
//
// Each call that reaches the middleware becomes one event: the tool and its description, the
// server's instructions, the arguments, the result, how it ended and how long it took, and who
// it was for. The tool runs
// unchanged, and its reply or error goes back as it came, but for what the extras do (see Extras):
// a tool whose listed schema refuses the inputs it doesn't name takes two more, session_id and
// intent, which never reach its handler, and its result may end with a line giving the agent a
// task ID. Before the tool, the wrapper reads the
// call (its tool, the calling app, the protocol version), copies its arguments when they are
// recorded, and asks the Choice functions; as the tool returns, it reads how the call ended and
// notes what the result holds, without waiting on anything; the event is built from there, in a
// goroutine of its own, which may end before or after the reply reaches the client. Whatever
// fails in recording only drops the event.
//
// To read a failed call, the wrapper reads a *jsonrpc.Error by its fields; of any other error it
// calls the Error method once (when its text is recorded) and errors.As, as the MCP SDK itself
// does afterwards to write the reply. A result's structured content is recorded when it is JSON
// already (json.RawMessage, which mcp.AddTool leaves there) or plain data: structs with plain
// json tags, maps with string keys, slices and plain values, up to 10,000 values and 32 levels,
// of types with no MarshalJSON, MarshalText or other method encoding/json calls. Content that
// writes its own JSON would have to write it a second time for the event, which is the app's
// code to run, so it is left out, and the event says so in its dropped field. (Go's own
// json.RawMessage, json.Number and time.Time, and pointers to them, are written: their methods
// are Go's.) Plain data is written once more as the tool returns, before the reply: that takes
// about as long as the MCP SDK then takes to write it.
package lsquaremcp

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"maps"
	"math"
	"net/http"
	"os"
	"reflect"
	"regexp"
	"runtime"
	"slices"
	"strings"
	"sync"
	"time"
	"unicode/utf8"
	"weak"

	"github.com/modelcontextprotocol/go-sdk/auth"
	"github.com/modelcontextprotocol/go-sdk/jsonrpc"
	"github.com/modelcontextprotocol/go-sdk/mcp"

	lsquare "github.com/theagentexperiencecompany/lsquare/packages/go"
	"github.com/theagentexperiencecompany/lsquare/packages/go/internal/bridge"
	"github.com/theagentexperiencecompany/lsquare/packages/go/internal/core"
	"github.com/theagentexperiencecompany/lsquare/packages/go/internal/jsonv"
)

// How long a call's event waits for Identify after the tool returns.
const identifyWait = time.Second

// The most tools whose listed description is kept.
const maxTools = 10_000

// Options says what is recorded. The zero value records everything it can.
type Options struct {
	// Identify says who a call is for. The event waits for it up to a second after the tool
	// returns. It runs alongside the tool, in its own goroutine, and doesn't delay the reply,
	// unless the reply waits for the call's event (FlushBeforeReply, or Flush inside the tool):
	// the reply then waits for it too, and its second is cut to the client's FlushTimeout when
	// that is shorter. If it returns an error, panics, answers later than that or returns an
	// invalid ID, the call is sent as limited diagnostics (its tool's name, status, duration and
	// error category, with none of its content and no IDs of Identify's), unless a turn captured
	// inside the call gives every ID field Identify last returned in time (a UserID before any),
	// and the client reports identify_failed.
	//
	// ctx is the context the call reached Instrument's middleware with: it has what MCP
	// middleware added after Instrument put in it (that middleware runs first), and not what
	// middleware added before did, which only the tool sees. It is cancelled a second after the
	// tool returns at the latest (sooner, once nothing waits for the answer), not when the
	// request ends. Its values are the request's own, shared with the tool: read them, don't
	// change them. On an HTTP session that outlives one request, the MCP SDK gives every call the
	// context of the request that opened the session, so what your HTTP middleware put in it is
	// that request's. Over Streamable HTTP, read who is calling from call.TokenInfo and
	// call.Header, which are the current request's; over the older SSE transport both are nil
	// (see Call).
	Identify func(ctx context.Context, call Call) (*Identity, error)

	// Inputs says which tools' arguments are recorded: nil, every tool's.
	Inputs Choice
	// Results says which tools' results are recorded: nil, every tool's. Strings are cut at 4,000
	// characters, and files are only their size. A failed call's result and error message are
	// recorded only when both its inputs and its results are, since an error can quote either.
	Results Choice
	// Descriptions says which tools' descriptions are recorded with their calls, cut at 1,000
	// characters: nil, every tool's.
	Descriptions Choice
	// Instructions says which tools' calls the server's instructions are recorded with, cut at
	// 2,000 characters: nil, every tool's; Off, none. It is asked like the others, with the
	// tool's name and before the tool runs, though the instructions are the server's and the same
	// for every tool: Off is the choice most servers have to make.
	Instructions Choice

	// Tools are the server's tools, as registered: a call's description comes from here until the
	// server has listed that tool to a client, and from that listing afterwards. Without it, a
	// call that comes before any listing has no description.
	Tools []*mcp.Tool
	// ServerInfo is what the server calls itself, as given to mcp.NewServer. Without it, it is
	// learned from the first initialize the server answers, which a client on protocol
	// 2026-07-28 or later never sends.
	ServerInfo *mcp.Implementation
	// ServerInstructions is what the server tells clients about using it, as given to
	// mcp.NewServer in ServerOptions.Instructions. The MCP SDK doesn't say what a server's
	// instructions are, so a call's are the ones its own connection was given, in the server's
	// answer to initialize or server/discover as it passed the middleware; on a connection given
	// none, which each request of a stateless server is, they are these. Without this, they are
	// the ones the server last gave any client, and a call that comes before any has none: a
	// server that tells different clients different things should set it, or turn Instructions
	// off.
	ServerInstructions string

	// FlushBeforeReply makes each call's reply wait for the call's event to be sent, up to the
	// client's FlushTimeout, and for the turns captured inside the call: for a host that stops
	// the process once it has replied. A tool that calls Flush makes its own reply wait so.
	FlushBeforeReply bool

	// Extras turns off, each by itself, what the wrapper adds to the server's tools: the
	// session_id and intent inputs, and the get_more_tools tool. The zero value leaves all three
	// on, for the tools whose listed schema refuses the inputs it doesn't name.
	Extras Extras
}

// Choice says which tools something is recorded for: a function that is asked once per call,
// before the tool runs, so it must answer at once (the call waits for it). Nil records it for
// every tool, Off for none. If it panics, it isn't recorded for that call.
type Choice func(toolName string) bool

// Off is the Choice that records it for no tool.
func Off(toolName string) bool { return false }

// Call is the call Identify is asked about. Its arguments, header and token are copies, which
// the tool running alongside doesn't share, with one exception: the values inside
// TokenInfo.Extra are the token's own.
//
// Header and TokenInfo are what the MCP SDK gives the tool for this request. Over Streamable
// HTTP that is the current request's. Over the older SSE transport (mcp.NewSSEHandler) the SDK
// gives neither, so both are nil there, and only the context has the token of the request that
// opened the connection.
type Call struct {
	ToolName string
	// Arguments are the arguments as the client sent them, before the SDK checked them: a copy
	// of them all, whatever their size, made before the tool runs. They are without the
	// session_id and intent the wrapper takes out of a call, as the tool gets them.
	Arguments json.RawMessage
	// Header is the HTTP request's header, over Streamable HTTP; nil on other transports, the
	// older SSE one included.
	Header http.Header
	// TokenInfo is the request's verified token, over Streamable HTTP, if the server checks one;
	// nil on other transports, the older SSE one included. Its Scopes and its
	// Extra map are copies; a value inside Extra (a list of roles, say) is the one the tool
	// has, so read it and don't change it.
	TokenInfo *auth.TokenInfo
	// SessionID is the ID of the connection the call came on, when it has one.
	SessionID string
}

// Identity is who a call is for. Every field is optional. Only these are sent, never anything
// else Identify was given.
type Identity struct {
	// OrgID, UserID and ConversationID must be internal IDs, not email addresses: events can be
	// deleted by them. Each is at most 512 bytes.
	OrgID          string
	UserID         string
	ConversationID string
	// Metadata is recorded with the event: strings, numbers, booleans and nil.
	Metadata map[string]any
}

type instrumented struct {
	client  bridge.Client
	options Options

	mu sync.Mutex
	// Each tool's description as the server last listed it.
	listed map[string]string
	// Each tool's description as registered (Options.Tools), until a listing says otherwise.
	registered map[string]string
	server     *jsonv.Object
	// The server's instructions for a call on a connection that wasn't given any: the ones
	// Options said, or without them the ones the server last gave any client.
	instructions string
	// Options said what the instructions are: no answer changes them for other connections.
	seeded bool

	// What each connection was told.
	connections *connections

	extras Extras
	// The tools the server has listed: under mu.
	catalog catalog
}

// connections is what each connection was told as it was set up: the protocol version its
// initialize negotiated, and the instructions it was given. It is on its own so that a
// connection's cleanup holds nothing else: a cleanup that reached the wrapper would reach
// Identify, and a connection Identify holds on to would then never be collected.
type connections struct {
	mu sync.Mutex
	of map[weak.Pointer[mcp.ServerSession]]*told
}

type told struct {
	version string
	// The instructions the connection was given, and whether it was given any answer that has
	// them: an answer without instructions says there are none.
	instructions string
	answered     bool
	// The ID the wrapper gave the connection, to group its calls by: made on its first call.
	id string
}

// note changes what is known of a connection.
func (c *connections) note(session *mcp.ServerSession, change func(*told)) {
	key := weak.Make(session)
	c.mu.Lock()
	defer c.mu.Unlock()
	known := c.of[key]
	if known == nil {
		known = &told{}
		c.of[key] = known
		// Forgotten when the connection is: nothing here keeps it alive.
		runtime.AddCleanup(session, func(key weak.Pointer[mcp.ServerSession]) {
			c.mu.Lock()
			delete(c.of, key)
			c.mu.Unlock()
		}, key)
	}
	change(known)
}

func (c *connections) get(session *mcp.ServerSession) (told, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if known := c.of[weak.Make(session)]; known != nil {
		return *known, true
	}
	return told{}, false
}

// The servers already instrumented: the first Instrument of a server is the one that counts.
var (
	serversMu sync.Mutex
	servers   = map[weak.Pointer[mcp.Server]]bool{}
)

// Instrument records every tools/call of server with client, through a receiving middleware it
// adds to the server.
//
// The middleware sees a call as it reaches its place among the server's: middleware added before
// Instrument runs inside it, so its changes to the reply are recorded; middleware added after
// runs outside it, so what it adds to the context reaches Identify, and a call it refuses is
// never recorded.
//
// The first Instrument of a server decides what is recorded, and with which client: another one
// changes nothing. With a client that is disabled (no API key, outside a dry run), the server is
// left as it is.
func Instrument(server *mcp.Server, client *lsquare.Client, options *Options) {
	if server == nil || client == nil || bridge.Of == nil {
		return
	}
	bridged := bridge.Of(client)
	if bridged == nil || !bridged.Enabled() {
		return
	}
	key := weak.Make(server)
	// Held until the middleware is in place: another Instrument of the same server returns only
	// once the first one's is.
	serversMu.Lock()
	defer serversMu.Unlock()
	if servers[key] {
		return
	}
	server.AddReceivingMiddleware(newInstrumented(bridged, options).middleware)
	servers[key] = true
	runtime.AddCleanup(server, func(key weak.Pointer[mcp.Server]) {
		serversMu.Lock()
		delete(servers, key)
		serversMu.Unlock()
	}, key)
}

func newInstrumented(client bridge.Client, options *Options) *instrumented {
	w := &instrumented{client: client, listed: map[string]string{}, registered: map[string]string{}, connections: &connections{of: map[weak.Pointer[mcp.ServerSession]]*told{}}}
	if options != nil {
		w.options = *options
	}
	w.extras = w.options.Extras
	for _, tool := range w.options.Tools {
		if tool != nil && tool.Name != "" && tool.Description != "" {
			w.registered[tool.Name] = tool.Description
		}
	}
	w.server = named(w.options.ServerInfo)
	// Made valid UTF-8 once, here, and not on every call.
	w.instructions, w.seeded = jsonv.ValidString(w.options.ServerInstructions), w.options.ServerInstructions != ""
	// Read, and let go: a tool the server later drops, with its schemas, isn't kept alive here.
	w.options.Tools, w.options.ServerInfo, w.options.ServerInstructions = nil, nil, ""
	return w
}

func named(info *mcp.Implementation) *jsonv.Object {
	if info == nil || info.Name == "" {
		return nil
	}
	out := jsonv.NewObject()
	out.Set("name", jsonv.ValidString(info.Name))
	if info.Version != "" {
		out.Set("version", jsonv.ValidString(info.Version))
	}
	return out
}

func (w *instrumented) middleware(next mcp.MethodHandler) mcp.MethodHandler {
	return func(ctx context.Context, method string, request mcp.Request) (mcp.Result, error) {
		switch method {
		case "tools/call":
			return w.onCall(ctx, next, method, request)
		case "tools/list":
			return w.onList(ctx, next, method, request)
		case "initialize":
			result, err := next(ctx, method, request)
			if err == nil {
				w.quietly(func() { w.noteInitialize(request, result) })
			}
			return result, err
		case "server/discover":
			result, err := next(ctx, method, request)
			if err == nil {
				w.quietly(func() { w.noteDiscover(request, result) })
			}
			return result, err
		}
		return next(ctx, method, request)
	}
}

// quietly runs a step of the wrapper's own: a bug in it must never reach the app's call. It is
// reported, since the call's event is then lost.
func (w *instrumented) quietly(step func()) {
	defer func() {
		if recover() != nil {
			func() {
				defer func() { _ = recover() }()
				w.client.Report(bridge.Report{Code: string(lsquare.CodeInternal), Message: "Recording a tool call failed unexpectedly; its event was dropped."})
			}()
		}
	}()
	step()
}

// describe notes the description of each tool the server lists: the listing is what
// agents see, so it also overrules what the tool was registered with.
func (w *instrumented) describe(tools []*mcp.Tool) {
	w.mu.Lock()
	defer w.mu.Unlock()
	for _, tool := range tools {
		if tool == nil || tool.Name == "" {
			continue
		}
		delete(w.registered, tool.Name)
		if tool.Description == "" {
			delete(w.listed, tool.Name)
		} else if _, known := w.listed[tool.Name]; known || len(w.listed) < maxTools {
			w.listed[tool.Name] = tool.Description
		}
	}
}

// noteInitialize notes what the server called itself and the protocol version it negotiated for
// this connection.
func (w *instrumented) noteInitialize(request mcp.Request, result mcp.Result) {
	answer, ok := result.(*mcp.InitializeResult)
	if !ok || answer == nil {
		return
	}
	w.mu.Lock()
	if w.server == nil {
		w.server = named(answer.ServerInfo)
	}
	w.mu.Unlock()
	w.noteInstructions(request, answer.Instructions)
	if session, ok := request.GetSession().(*mcp.ServerSession); ok && session != nil && answer.ProtocolVersion != "" {
		w.connections.note(session, func(known *told) { known.version = answer.ProtocolVersion })
	}
}

// noteDiscover notes the instructions the server gives a client that asks what it is, which a
// client on protocol 2026-07-28 or later does in place of initialize.
func (w *instrumented) noteDiscover(request mcp.Request, result mcp.Result) {
	if answer, ok := result.(*mcp.DiscoverResult); ok && answer != nil {
		w.noteInstructions(request, answer.Instructions)
	}
}

// noteInstructions notes the instructions a connection was just given: they are that
// connection's from here, whatever another one is given. For calls on connections that are given
// none (each request of a stateless server is one), they are also the server's, unless Options
// said what those are.
func (w *instrumented) noteInstructions(request mcp.Request, instructions string) {
	instructions = jsonv.ValidString(instructions)
	if session, ok := request.GetSession().(*mcp.ServerSession); ok && session != nil {
		w.connections.note(session, func(known *told) { known.instructions, known.answered = instructions, true })
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	if !w.seeded {
		w.instructions = instructions
	}
}

// serverInstructions is what the server told the call's client about using it: what its
// connection was given, else the server's own.
func (w *instrumented) serverInstructions(request *mcp.CallToolRequest) string {
	if request.Session != nil {
		if known, ok := w.connections.get(request.Session); ok && known.answered {
			return known.instructions
		}
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.instructions
}

// description is the tool's description as agents are listed it now: the one the server last
// listed, else the one it was registered with.
func (w *instrumented) description(toolName string) string {
	w.mu.Lock()
	defer w.mu.Unlock()
	if listed, ok := w.listed[toolName]; ok {
		return listed
	}
	return w.registered[toolName]
}

// protocolVersion is the version the call is made under: the request's own, where the protocol
// puts it there; otherwise the one the connection's initialize negotiated, which can be older
// than the one the client asked for.
func (w *instrumented) protocolVersion(request *mcp.CallToolRequest) string {
	version := request.ProtocolVersion()
	session := request.Session
	if session == nil {
		return version
	}
	asked := ""
	if params := session.InitializeParams(); params != nil {
		asked = params.ProtocolVersion
	}
	if version != asked {
		return version
	}
	if known, ok := w.connections.get(session); ok && known.version != "" {
		return known.version
	}
	return version
}

// choose asks a Choice about a tool, once per call. One that panics records nothing for that
// call, and one warning per option says so.
func (w *instrumented) choose(choice Choice, option, toolName string) (recorded bool) {
	if choice == nil {
		return true
	}
	defer func() {
		if recover() != nil {
			recorded = false
			w.client.WarnOnce(option+"-panicked", "[lsquare] The "+option+" function panicked: "+strings.ToLower(option)+" aren't recorded for the calls it answered that way.")
		}
	}()
	return choice(toolName)
}

// running is a call, from its arrival.
type running struct {
	callID     string
	capturedAt string
	started    time.Time
	toolName   string
	// What the client sent: a copy taken as the call arrived, or, when they are too large to
	// copy before the call is admitted, the request's own bytes until then.
	arguments []byte
	// The arguments are already a copy.
	copied  bool
	inputs  bool
	results bool
	// The tool's description as agents were listed it when the call arrived.
	description string
	// The server's instructions as it gave them to clients when the call arrived.
	instructions string
	client       *jsonv.Object
	protocol     string
	// The call brings what an earlier round asked for.
	continued bool
	// What Identify answers, or why it didn't; nil when there is no Identify.
	identified chan identified
	// Whether Identify answered in time, decided once for the event and the client alike.
	gate *gate
	// Stops Identify, at its deadline or once nothing waits for it.
	cancel context.CancelFunc
	// When Identify's answer stops counting: a second after the tool returned.
	deadline time.Time
	// Stops Identify at that deadline, whatever the building of the event is doing by then.
	expiry *time.Timer

	// What the tool leaves to the wrapper, as its listing says: nil for a tool that is left
	// alone. The arguments above are the call's without it.
	takes *plan
	// A session ID the agent sent, in the right format, and why it made the call.
	sent, intent string
	// The ID the transport gave the call's connection, if it gave one, and the ID its calls are
	// grouped by, if they are: see connection.
	transport, connection string
	// The session the call belongs to, once settled.
	session *session
	settled bool
	// The wrapper answered the call itself (get_more_tools), and refused its arguments.
	answered, refused bool
	// The request was cancelled before the tool returned.
	cancelled bool
	// When the tool returned, and whether the reply waits for the call's event: the tool called
	// Flush, or Options.FlushBeforeReply. The turns captured inside the call besides its agent
	// part, which it waits for too, and the call as the client tracks it until its event is built.
	endedAt time.Time
	waits   bool
	turns   []any
	tracked bridge.Tracked
	// The first turn captured inside the call before the tool returned, as Capture read it: the
	// call's agent part. Its conversation, when it gives one, groups the call.
	agent *bridge.Agent
}

// conversation is the conversation the call's agent part gave: "" for none.
func (call *running) conversation() string {
	if call.agent == nil {
		return ""
	}
	value, _ := call.agent.IDs.Get("conversationId")
	text, _ := value.(string)
	return text
}

// gate is where Identify's answer is found in time or late, once: for the call's event and for
// what the client learns from it alike. A failed Identify (or a late one) is decided there too,
// with what the client then required of the call's capture: fixed once both the tool has returned
// and Identify has failed, whatever comes after. Identify's goroutine holds it, not the call.
type gate struct {
	mu sync.Mutex
	// Identify's deadline, set as the tool returns: zero while it runs.
	deadline time.Time
	// When Identify returned: zero until then.
	at time.Time
	// Its deadline passed with no answer: none counts from then on.
	expired bool
	// Identify failed, or was late.
	failed bool
	// The ID fields the client required of a failed call's capture (see bridge.Client.Identified),
	// once frozen; keys reads them.
	frozen   bool
	required []string
	keys     func() []string
}

// returned notes that Identify returned, now, and reports when, and whether that was in time:
// before its deadline, or while the tool still ran, which is before it, and not once its deadline
// has passed without it.
func (g *gate) returned() (at time.Time, inTime bool) {
	g.mu.Lock()
	defer g.mu.Unlock()
	at = time.Now()
	g.at = at
	inTime = !g.expired && (g.deadline.IsZero() || at.Before(g.deadline))
	if !inTime {
		g.failLocked()
	}
	return at, inTime
}

// fail notes that Identify failed.
func (g *gate) fail() {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.failLocked()
}

func (g *gate) failLocked() {
	g.failed = true
	g.freezeLocked()
}

// close sets Identify's deadline as the tool returns. Called with mu held.
func (g *gate) closeLocked(deadline time.Time) {
	g.deadline = deadline
	g.freezeLocked()
}

// freezeLocked fixes what a failed call needs, once the tool has returned and Identify failed.
func (g *gate) freezeLocked() {
	if g.failed && !g.deadline.IsZero() && !g.frozen {
		g.frozen = true
		if g.keys != nil {
			g.required = g.keys()
		}
	}
}

// expire is Identify's deadline passing, as the event waits for it. False: it had returned in
// time, and only the reading of its answer is left. True: none of its answers counts any more.
func (g *gate) expire() bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	if !g.at.IsZero() && !g.expired && g.at.Before(g.deadline) {
		return false
	}
	g.expired = true
	g.failLocked()
	return true
}

// requiredKeys is what the client required of the failed call's capture: nil before any Identify
// answered in time.
func (g *gate) requiredKeys() []string {
	g.mu.Lock()
	defer g.mu.Unlock()
	if !g.frozen && g.keys != nil {
		return g.keys()
	}
	return g.required
}

// session is the session a call is grouped into, and where its ID came from: the agent sent it,
// the wrapper issued it with this call, or it is the connection's.
type session struct{ id, source string }

// stop ends Identify: at its deadline, or once nothing waits for its answer.
func (call *running) stop() {
	if call.expiry != nil {
		call.expiry.Stop()
	}
	if call.cancel != nil {
		call.cancel()
	}
}

type identified struct {
	ids      *jsonv.Object
	metadata *jsonv.Object
	// Why it failed; "" when it didn't.
	why string
	// When Identify returned, before anything was made of its answer.
	at time.Time
}

// onCall is one tools/call. The tool runs unchanged, and its reply goes back as it came, but for
// what the extras do for a tool whose listing says they can: session_id and intent are taken out
// of the call before the SDK checks it, in a copy of the request, and the reply may end with a
// line that gives the agent a task ID. The event is built after the tool returns. If the tool
// panics, the panic goes on as it would have.
func (w *instrumented) onCall(ctx context.Context, next mcp.MethodHandler, method string, request mcp.Request) (mcp.Result, error) {
	arrived := time.Now()
	typed, _ := request.(*mcp.CallToolRequest)
	if typed == nil || typed.Params == nil {
		return next(ctx, method, request)
	}
	// Also after Shutdown: a tool that was listed with the inputs still gets calls with them.
	took, forward, takes := w.prepare(ctx, next, typed)
	// After Shutdown nothing is recorded, so nothing else is read, and nothing is answered.
	if w.client.Closed() {
		return next(ctx, method, forward)
	}
	// A call to get_more_tools, which the wrapper may answer: until the server has said it has
	// no such tool, the call is the server's, as it came.
	answers := takes != nil && takes.answers
	if answers {
		takes = nil
	}
	var call *running
	w.quietly(func() { call = w.start(ctx, typed, arrived, took, takes) })
	if call == nil {
		return next(ctx, method, forward)
	}
	// The tool is given the call: a Capture from its code, on this client, finds it there.
	scope := bridge.NewCall(call.callID)
	tool := w.client.WithCall(ctx, scope)
	dispatch := func() (mcp.Result, error) { return next(tool, method, forward) }
	if answers && call.inputs {
		dispatch = func() (mcp.Result, error) {
			// The server first: a tool of its own under that name, listed or not, is the one
			// that answers. Only what the SDK says of a tool it doesn't have is the wrapper's to
			// answer, and only where what the agent asks for is recorded.
			result, err := next(tool, method, forward)
			if !unknownTool(err) {
				return result, err
			}
			sessions := !w.extras.DisableSessions
			need := takeInputs(typed.Params.Arguments, sessions, false)
			reply := answer(need.arguments, typed.Session)
			call.takes, call.sent = &plan{read: true, sessions: sessions, answers: true}, need.sessionID
			call.arguments, call.copied = bytes.Clone(need.arguments), true
			call.answered, call.refused = true, reply.IsError
			return reply, nil
		}
	}
	var (
		result   mcp.Result
		err      error
		lasted   time.Duration
		ended    bool
		finished bool
	)
	// The call ends when the tool returns, or panics: how long it took, whether it was cancelled
	// by then, and Identify's second are all counted from here, before anything of the result is
	// read or added to.
	end := func() {
		// The return and Identify's deadline are one step for Identify's goroutine, which judges
		// its answer by them: an answer before it is in time; one after, by the deadline.
		call.gate.mu.Lock()
		returned := time.Now()
		// A capture from now on is a turn of its own, as is one still being read.
		closed := scope.Close()
		call.agent, call.turns = closed.Agent, closed.Turns
		// A reply that waits for its event waits for Identify too: Identify's second is cut to
		// what the reply waits, when that is shorter, so that the event can be sent in time.
		call.waits, call.endedAt = closed.Flushed || w.options.FlushBeforeReply, returned
		wait := identifyWait
		if timeout := w.client.FlushTimeout(); call.waits && timeout > 0 && timeout < wait {
			wait = timeout
		}
		call.deadline = returned.Add(wait)
		call.gate.closeLocked(call.deadline)
		call.gate.mu.Unlock()
		lasted, ended = returned.Sub(call.started), true
		if call.cancel != nil {
			call.expiry = time.AfterFunc(time.Until(call.deadline), call.cancel)
		}
		// The request's own context, which may be the app's: whatever it does, the call ends.
		func() {
			defer func() { _ = recover() }()
			call.cancelled = ctx.Err() != nil
		}()
	}
	defer func() {
		// A tool that panicked: the panic goes on, once the call is recorded as it does, and its
		// event sent if the reply was to wait for it.
		panicked := !ended
		if panicked {
			end()
		}
		if !finished {
			w.quietly(func() { w.finish(call, result, err, panicked, lasted) })
			if panicked && call.waits {
				w.quietly(func() { w.client.Settle(ctx, call.tracked, call.turns, call.endedAt) })
			}
		}
	}()
	result, err = dispatch()
	end()
	reply := result
	w.quietly(func() {
		if issued := w.issue(call, result, err); issued != nil {
			reply = issued
		}
	})
	// The tool's own result: the task ID line, if the reply has one, isn't the tool's.
	finished = true
	w.quietly(func() { w.finish(call, result, err, false, lasted) })
	if call.waits {
		// The call's event is sent before the reply, and the turns captured inside it.
		w.quietly(func() { w.client.Settle(ctx, call.tracked, call.turns, call.endedAt) })
	}
	return reply, err
}

// prepare takes what the call's tool leaves to the wrapper out of its arguments, and returns the
// request to pass on: the call's own, unless something was taken out, and then a copy whose
// arguments are its own. When the wrapper hasn't seen the server's whole tool listing, it reads
// it first (see discover). Whatever goes wrong, the call goes on as it came.
func (w *instrumented) prepare(ctx context.Context, next mcp.MethodHandler, request *mcp.CallToolRequest) (took taken, forward mcp.Request, takes *plan) {
	unchanged := func() { took, forward, takes = taken{arguments: request.Params.Arguments}, request, nil }
	unchanged()
	if !w.extras.any() {
		return
	}
	defer func() {
		if recover() != nil {
			unchanged()
		}
	}()
	name := request.Params.Name
	entry := w.planOf(name)
	if entry == nil {
		// Also after Shutdown: an agent that was listed the inputs still sends them.
		w.discover(ctx, next, request.Session, request.Extra, request.Params.Meta)
		entry = w.planOf(name)
	}
	if entry == nil && name == missingTools && w.offers() {
		// The wrapper's own, which it answers if the server has none: the call goes to the
		// server as it came.
		takes = &plan{read: true, answers: true}
		return
	}
	if entry == nil || (!entry.sessions && !entry.intents) {
		return
	}
	takes = entry
	if took = takeInputs(request.Params.Arguments, entry.sessions, entry.intents); took.cut {
		forward = withArguments(request, took.arguments)
	}
	return
}

// unknownTool reports whether err is what the MCP SDK answers a call to a tool the server
// doesn't have.
func unknownTool(err error) bool {
	wire, ok := err.(*jsonrpc.Error)
	// As the SDK writes it, with nothing more: an error with data of its own is the app's.
	return ok && wire != nil && wire.Code == jsonrpc.CodeInvalidParams && len(wire.Data) == 0 && wire.Message == `unknown tool "`+missingTools+`"`
}

// clientNamedSessions reports whether the MCP SDK was told to take a client's word for its
// session's ID on a server that keeps no sessions (MCPGODEBUG=allowsessionsinstateless=1, read
// as the SDK reads it): such an ID is whatever the client sent, so calls aren't grouped by it.
var clientNamedSessions = func() bool {
	value := ""
	for setting := range strings.SplitSeq(os.Getenv("MCPGODEBUG"), ",") {
		if key, is, ok := strings.Cut(setting, "="); ok && strings.TrimSpace(key) == "allowsessionsinstateless" {
			value = strings.TrimSpace(is)
		}
	}
	return value == "1"
}()

// connection is what the transport says of the call's connection. transport is the ID it gave
// the connection, if it gave one: a Streamable HTTP session's. grouped is the ID the connection's
// calls are grouped by: that one, when the server issued it, or, on a connection that has none
// (stdio, the older SSE transport, an in-memory one), an ID the wrapper gives it with its first
// call. Both are empty on protocol 2026-07-28 and later, which has no sessions, and grouped is
// over HTTP on a server that keeps none.
func (w *instrumented) connection(request *mcp.CallToolRequest, version string) (transport, grouped string) {
	if sessionless(version) || request.Session == nil {
		return "", ""
	}
	overHTTP := request.Extra != nil && request.Extra.Header != nil
	if transport = request.Session.ID(); transport != "" {
		// A request can't say whether its server keeps sessions: where the SDK takes a client's
		// word for the ID, no HTTP session's is trusted.
		if overHTTP && clientNamedSessions {
			return transport, ""
		}
		return transport, transport
	}
	if overHTTP {
		return "", ""
	}
	w.connections.note(request.Session, func(known *told) {
		if known.id == "" {
			known.id = core.NewUUID()
		}
		grouped = known.id
	})
	return "", grouped
}

// settle decides which session the call belongs to: the one whose ID the agent sent; else its
// connection's, as a hash of its ID, which can be a live handle to the session. The connection's
// own ID, sent back, is still that session, and never leaves as it is.
func (w *instrumented) settle(call *running) {
	if call.settled {
		return
	}
	call.settled = true
	if w.extras.DisableSessions {
		return
	}
	sent := call.sent
	if sent != "" && sent == call.transport {
		sent = ""
	}
	switch {
	case sent != "":
		call.session = &session{sent, "sent"}
	case call.conversation() != "":
		// The turn captured inside the call says which conversation it is: that groups it, and
		// it is given no session of its own.
	case call.connection != "":
		call.session = &session{sessionHash(call.connection), "protocol"}
	}
}

// issue gives the agent a new session ID for a call that has no session, in a text block at the
// end of a copy of the result: the result itself is never changed. Nil: the reply is the result.
// Only a tool that leaves session_id to the wrapper gets one, and only on a result that says
// something and isn't an error: an error result can be the SDK's own, for arguments it refused,
// which nothing tells from the tool's. A cancelled call and a round that asks the client for
// more get none either.
func (w *instrumented) issue(call *running, result mcp.Result, err error) mcp.Result {
	w.settle(call)
	if w.extras.DisableSessions || call.session != nil || call.conversation() != "" || err != nil || call.cancelled || call.takes == nil || !call.takes.sessions {
		return nil
	}
	// Shut down while the tool ran: the call's event is lost, and so would its task be.
	if w.client.Ended() {
		return nil
	}
	typed, _ := result.(*mcp.CallToolResult)
	if typed == nil || typed.IsError || len(typed.Content) == 0 || typed.NeedsInput() || typed.InputRequests != nil {
		return nil
	}
	id := newSessionID()
	reply := *typed
	// A list of its own: the tool's may have room at its end, which the tool still owns.
	reply.Content = slices.Concat(typed.Content, []mcp.Content{&mcp.TextContent{Text: idLine(id)}})
	// The SDK adds to a reply's metadata as it sends it.
	reply.Meta = maps.Clone(typed.Meta)
	call.session = &session{id, "issued"}
	return &reply
}

// start reads the call as it arrives. took is its arguments without what its tool leaves to the
// wrapper, which is what the event and Identify get.
func (w *instrumented) start(ctx context.Context, typed *mcp.CallToolRequest, now time.Time, took taken, takes *plan) *running {
	toolName := typed.Params.Name
	call := &running{
		gate:       &gate{keys: w.client.IdentifiedKeys},
		callID:     core.NewUUID(),
		capturedAt: now.UTC().Format("2006-01-02T15:04:05.000Z"),
		started:    now,
		toolName:   toolName,
		inputs:     w.choose(w.options.Inputs, "Inputs", toolName),
		results:    w.choose(w.options.Results, "Results", toolName),
		// Present, even empty: the client answered an earlier round. (An empty requestState can't
		// be told from none here: the MCP SDK reads both as "".)
		continued: typed.Params.InputResponses != nil || typed.Params.RequestState != "",
		protocol:  w.protocolVersion(typed),
		client:    clientOf(typed),
		takes:     takes,
		sent:      took.sessionID,
		intent:    took.intent,
	}
	// The connection's own ID is known whatever the extras: it is kept out of the event either
	// way. Calls are grouped by it only with sessions on.
	transport, grouped := w.connection(typed, call.protocol)
	call.transport = transport
	if !w.extras.DisableSessions {
		call.connection = grouped
	}
	if w.choose(w.options.Descriptions, "Descriptions", toolName) {
		call.description = w.description(toolName)
	}
	if w.choose(w.options.Instructions, "Instructions", toolName) {
		call.instructions = w.serverInstructions(typed)
	}
	if call.inputs {
		// As the client sent them: the tool may change its own. Arguments larger than an event can
		// hold aren't copied before the call is admitted, as the tool returns.
		// A copy of the event's own: the request passed on is the app's to change.
		switch {
		case len(took.arguments) <= core.DefaultMaxEventBytes:
			call.arguments, call.copied = bytes.Clone(took.arguments), true
		case took.cut:
			// Too large to copy before the call is admitted: the copy that was passed on is
			// copied then, as the tool left it.
			call.arguments = took.arguments
		default:
			call.arguments = typed.Params.Arguments
		}
	}
	w.identify(ctx, call, typed, took.arguments)
	return call
}

// clientOf is what the calling app calls itself: from the request's own metadata, where the
// protocol puts it there, else from the connection's initialize. It reads what the MCP SDK's
// ClientInfo reads, without writing the metadata as JSON to read it back, as that does: a value
// there that isn't plain data is left alone.
func clientOf(request *mcp.CallToolRequest) *jsonv.Object {
	if meta := request.Params.GetMeta(); meta != nil {
		switch info := meta[mcp.MetaKeyClientInfo].(type) {
		case nil:
		case *mcp.Implementation:
			return named(info)
		case map[string]any:
			// As decoded from the wire. One whose name or version isn't text is no client info,
			// for the MCP SDK either: the connection's is used.
			name, nameIsText := textOr(info["name"])
			version, versionIsText := textOr(info["version"])
			plain := nameIsText && versionIsText
			for _, key := range []string{"title", "description", "websiteUrl"} {
				_, isText := textOr(info[key])
				plain = plain && isText
			}
			if icons, has := info["icons"]; has && icons != nil {
				plain = plain && iconsOr(icons)
			}
			if plain {
				return named(&mcp.Implementation{Name: name, Version: version})
			}
		case string, bool, float64, json.Number, []any:
			// From the wire, and no client info: the connection's is used.
		default:
			return nil
		}
	}
	if request.Session != nil {
		if params := request.Session.InitializeParams(); params != nil {
			return named(params.ClientInfo)
		}
	}
	return nil
}

// identify starts Identify, alongside the tool, with a copy of the call: nothing the tool holds.
func (w *instrumented) identify(ctx context.Context, call *running, request *mcp.CallToolRequest, arguments []byte) {
	hook := w.options.Identify
	if hook == nil {
		return
	}
	answers := make(chan identified, 1)
	gate := call.gate
	client := w.client
	call.identified = answers
	release, ok := w.client.AcquireHook()
	if !ok {
		gate.fail()
		answers <- identified{why: "was skipped at the hook limit"}
		return
	}
	// The slot comes back whatever fails before Identify's goroutine has it.
	started := false
	defer func() {
		if !started {
			release()
		}
	}()
	input := Call{ToolName: call.toolName, Arguments: bytes.Clone(arguments)}
	if request.Session != nil {
		input.SessionID = request.Session.ID()
	}
	if extra := request.Extra; extra != nil {
		input.Header = extra.Header.Clone()
		if token := extra.TokenInfo; token != nil {
			copied := *token
			copied.Scopes = slices.Clone(token.Scopes)
			copied.Extra = maps.Clone(token.Extra)
			input.TokenInfo = &copied
		}
	}
	// The request's values, without its end: Identify may outlive the reply, up to its deadline.
	// Marked as a callback's, so a Flush given it doesn't wait on this very call.
	identifyContext, cancel := context.WithCancel(w.client.CallbackContext(context.WithoutCancel(ctx)))
	call.cancel = cancel
	started = true
	// The goroutine holds the answer's channel, not the call: a call that was refused keeps
	// nothing alive while its Identify is still running.
	go func() {
		answer := identified{why: "panicked"}
		var at time.Time
		// However it ends, its slot comes back and its answer is given, with when it came. A
		// failure is noted first: what the call then needs of its capture is fixed from there.
		defer func() {
			_ = recover()
			if at.IsZero() {
				at, _ = gate.returned()
			}
			if answer.why != "" {
				gate.fail()
			}
			answer.at = at
			release()
			answers <- answer
		}()
		identity, err := hook(identifyContext, input)
		// Its time is its own: reading its answer isn't counted against it, and whoever waits
		// for the answer knows from here that it is on its way.
		at, inTime := gate.returned()
		if err != nil {
			answer = identified{why: "returned an error"}
			return
		}
		answer = checkIDs(identity)
		if answer.why != "" {
			return
		}
		// In time: the ID fields it returned are what a call whose Identify fails needs from a
		// capture, from now on, on any server of the client, unless one returned since. Late, it
		// teaches nothing: the call's event goes without it.
		if inTime {
			client.Identified(answer.ids.Keys(), at)
		}
		if identity != nil && identity.Metadata != nil {
			// Read now, each value on its own: what Identify does with the map afterwards changes
			// nothing, and a value that isn't JSON doesn't take the others with it.
			answer.metadata = jsonv.ObjectFromMap(identity.Metadata)
		}
	}()
}

// checkIDs checks the IDs Identify returned, as the ingestion checks them.
func checkIDs(identity *Identity) identified {
	answer := identified{ids: jsonv.NewObject()}
	if identity == nil {
		return answer
	}
	for _, id := range []struct{ key, value string }{{"orgId", identity.OrgID}, {"userId", identity.UserID}, {"conversationId", identity.ConversationID}} {
		if id.value == "" {
			continue
		}
		if !core.ValidID(id.value) {
			return identified{why: "returned an invalid " + id.key}
		}
		answer.ids.Set(id.key, id.value)
	}
	return answer
}

// outcome is how the call ended, read as it ended.
type outcome struct {
	status string
	// The error's category, message, type and code; nil when the call completed.
	failure *jsonv.Object
	round   string
	// How long the error's text is: held until the event is built.
	text int
	// Whether the result is recorded.
	recorded bool
	result   *snapshot
}

// finish reads how the call ended, and hands the rest to a goroutine of its own: nothing below
// waits on Identify or the network before the reply goes out.
func (w *instrumented) finish(call *running, result mcp.Result, err error, panicked bool, took time.Duration) {
	// Whatever fails here, Identify is told to stop: nothing would wait for its answer.
	handed := false
	defer func() {
		if !handed {
			call.stop()
		}
	}()
	// Also when the tool panicked, or the reply couldn't be read: the session a call belongs to
	// doesn't depend on how it ended.
	w.settle(call)
	typed, _ := result.(*mcp.CallToolResult)
	how := w.outcomeOf(call, typed, err, panicked)
	// Room is taken before anything of the call is kept, and tracked before the reply: a Flush
	// right after it waits for this call's event.
	// The captured turn as the event will hold it, already shaped.
	held := int64(len(call.arguments)) + int64(len(call.intent)) + int64(how.text) + int64(call.agent.Bytes())
	var plan resultPlan
	if how.recorded {
		plan = planResult(typed)
		held += plan.size
	}
	done, tracked, ok := w.client.Track(call.callID, saturate(held))
	if !ok {
		return
	}
	call.tracked = tracked
	// Given back whatever fails from here, by this function until the builder has it.
	defer func() {
		if !handed {
			done()
		}
	}()
	// The request's bytes are the app's after the reply.
	if !call.copied {
		call.arguments, call.copied = bytes.Clone(call.arguments), true
	}

	if how.recorded {
		how.result = takeResult(typed, plan)
	}
	handed = true
	go func() {
		defer done()
		defer call.stop()
		w.quietly(func() { w.build(call, how, took) })
	}()
}

// errorText reports whether a failed call's text is recorded. It can quote what the tool would
// have returned, and what it was sent: a validator's message does.
func errorText(call *running) bool { return call.results && call.inputs }

// What a Go type's name looks like. An unnamed type's, such as a struct written in place, holds
// its fields and their tags, which are the app's.
var typeName = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_.]{0,127}$`)

// errorType is the name of an error's type, for an error whose type has one.
func errorType(err error) (string, bool) {
	kind := reflect.TypeOf(err)
	for kind != nil && kind.Kind() == reflect.Pointer {
		kind = kind.Elem()
	}
	if kind == nil || kind.Name() == "" {
		return "", false
	}
	name := kind.String()
	return name, typeName.MatchString(name)
}

// outcomeOf says how the call ended: a cancellation or a reconnect later changes nothing.
//
// A request cancelled before the end is "timeout". An error the handler returned is "protocol"
// when it is, or wraps, a JSON-RPC error, which the client gets with its code, and "thrown"
// otherwise. A result marked as an error is "tool_error": the SDK makes the same result of a
// tool's own error and of arguments it refused, so the two can't be told apart here.
//
// The error is read as the MCP SDK reads it to write its reply: a JSON-RPC error by its fields,
// any other by errors.As and, when its text is recorded, one call of Error.
func (w *instrumented) outcomeOf(call *running, result *mcp.CallToolResult, err error, panicked bool) outcome {
	how := outcome{status: "completed"}
	fail := func(category string) *jsonv.Object {
		how.status = "error"
		how.failure = jsonv.NewObject()
		how.failure.Set("category", category)
		return how.failure
	}
	if result != nil && result.NeedsInput() {
		how.round = "input_required"
	} else if call.continued {
		how.round = "continuation"
	}
	if call.cancelled {
		// The SDK's own sentence, which quotes nothing.
		fail("timeout").Set("message", "The request was cancelled before the tool finished.")
		return how
	}
	if panicked {
		fail("thrown").Set("type", "panic")
		return how
	}
	if err != nil {
		message := func(text string) {
			if errorText(call) {
				// As it is, until the call is admitted: see build.
				how.failure.Set("message", text)
				how.text = textSize(text)
			}
		}
		if wire, plain := err.(*jsonrpc.Error); plain {
			if wire == nil {
				// A nil pointer as an error: nothing of it can be read.
				fail("thrown")
				return how
			}
			fail("protocol")
			message(wire.Message)
			how.failure.Set("code", jsonv.Int(wire.Code))
			return how
		}
		var wrapped *jsonrpc.Error
		if errors.As(err, &wrapped) && wrapped != nil {
			fail("protocol")
			if errorText(call) {
				// As the client gets it: the whole error's text, with the wrapped error's code.
				message(err.Error())
			}
			how.failure.Set("code", jsonv.Int(wrapped.Code))
			return how
		}
		fail("thrown")
		if errorText(call) {
			message(err.Error())
		}
		if name, ok := errorType(err); ok {
			how.failure.Set("type", name)
		}
		return how
	}
	if result == nil {
		return how
	}
	how.recorded = call.results
	if result.IsError {
		category := "tool_error"
		if call.refused {
			// The wrapper's own get_more_tools refuses arguments as the SDK does.
			category = "protocol"
		}
		failure := fail(category)
		how.recorded = errorText(call)
		if how.recorded {
			if text, index, ok := firstText(result.Content); ok {
				failure.Set("message", text)
				if index >= maxBlocks || !utf8.ValidString(text) {
					// Past the blocks the result's own measure counts, or made valid UTF-8 for the
					// message apart from the block's own copy.
					how.text = textSize(text)
				}
			}
		}
	}
	return how
}

// firstText is the text of an error result: its first text block, among its first 1,000.
func firstText(content []mcp.Content) (string, int, bool) {
	for index, block := range content {
		if index >= 1_000 {
			break
		}
		if text, ok := block.(*mcp.TextContent); ok && text != nil {
			return text.Text, index, true
		}
	}
	return "", 0, false
}

// iconsOr reports whether a value is a list of icons as the wire gives one: objects whose src,
// mimeType and theme are text and whose sizes are a list of texts.
func iconsOr(value any) bool {
	list, ok := value.([]any)
	if !ok {
		return false
	}
	for _, entry := range list {
		icon, ok := entry.(map[string]any)
		if !ok {
			return false
		}
		for _, key := range []string{"src", "mimeType", "theme"} {
			if _, isText := textOr(icon[key]); !isText {
				return false
			}
		}
		if sizes, has := icon["sizes"]; has && sizes != nil {
			texts, ok := sizes.([]any)
			if !ok {
				return false
			}
			for _, size := range texts {
				if _, isText := size.(string); !isText {
					return false
				}
			}
		}
	}
	return true
}

// textOr is a value that is text, or absent: "" then. False for anything else.
func textOr(value any) (string, bool) {
	if value == nil {
		return "", true
	}
	text, ok := value.(string)
	return text, ok
}

// textSize is what a text holds once it is valid UTF-8: each byte that isn't becomes U+FFFD,
// which is three.
func textSize(text string) int {
	if utf8.ValidString(text) {
		return len(text)
	}
	size := int64(len(text))
	for i := 0; i < len(text); {
		r, width := utf8.DecodeRuneInString(text[i:])
		if r == utf8.RuneError && width == 1 {
			size += 2
		}
		i += width
	}
	return saturate(size)
}

// saturate is a size as an int, at most the largest one.
func saturate(size int64) int {
	if size > math.MaxInt {
		return math.MaxInt
	}
	return int(size)
}

// snapshot is a result as the client gets it, taken before the reply: its blocks, with the size
// of each file in place of its data, and its structured content as JSON.
type snapshot struct {
	blocks []any
	// The structured content's JSON; nil when the result has none.
	structured []byte
	// The structured content couldn't be taken without running the app's own code again.
	opaque  bool
	isError bool
}

// The blocks of a result that are read: as many as an event keeps. One more is noted, empty, so
// the event knows there were more.
const maxBlocks = core.MaxEntries

// resultPlan is what taking a result will hold, known before anything of it is kept.
type resultPlan struct {
	// At most how many bytes the snapshot keeps alive or copies.
	size int64
	// The structured content can be written as JSON without running any code of the app's.
	passive bool
}

// planResult measures a result without keeping anything of it.
func planResult(result *mcp.CallToolResult) resultPlan {
	var plan resultPlan
	text := func(values ...string) {
		for _, value := range values {
			plan.size += int64(textSize(value))
		}
	}
	for index, block := range result.Content {
		if index >= maxBlocks {
			break
		}
		switch b := block.(type) {
		case *mcp.TextContent:
			if b != nil {
				text(b.Text)
			}
		case *mcp.ImageContent:
			if b != nil {
				text(b.MIMEType)
			}
		case *mcp.AudioContent:
			if b != nil {
				text(b.MIMEType)
			}
		case *mcp.ResourceLink:
			if b != nil {
				text(b.URI, b.Name, b.MIMEType)
			}
		case *mcp.EmbeddedResource:
			if b != nil && b.Resource != nil {
				text(b.Resource.URI, b.Resource.MIMEType, b.Resource.Text)
			}
		}
	}
	switch structured := result.StructuredContent.(type) {
	case nil:
	case json.RawMessage:
		plan.size += int64(len(structured))
	case *json.RawMessage:
		if structured != nil {
			plan.size += int64(len(*structured))
		}
	default:
		walk := measure{fields: map[reflect.Type]*written{}}
		if walk.passive(reflect.ValueOf(structured), 0) {
			plan.passive = true
			plan.size += walk.size
		}
	}
	return plan
}

// takeResult reads the result now: what the app does with it after the reply changes nothing.
// It runs none of the app's code: a second run of a MarshalJSON could give another answer than
// the one the client gets, or none.
func takeResult(result *mcp.CallToolResult, plan resultPlan) *snapshot {
	taken := &snapshot{isError: result.IsError, blocks: make([]any, 0, min(len(result.Content), maxBlocks+1))}
	for index, block := range result.Content {
		if index >= maxBlocks {
			// In place of all the others: nothing of them is read, or kept.
			taken.blocks = append(taken.blocks, jsonv.NewObject())
			break
		}
		taken.blocks = append(taken.blocks, blockValue(block))
	}
	switch structured := result.StructuredContent.(type) {
	case nil:
	case json.RawMessage:
		// What mcp.AddTool leaves there: the JSON the client gets.
		taken.structured = rawCopy(structured)
	case *json.RawMessage:
		// The same, by its pointer: none is written as null, as for the value.
		if structured == nil {
			taken.structured = rawCopy(nil)
		} else {
			taken.structured = rawCopy(*structured)
		}
	default:
		taken.opaque = true
		if plan.passive {
			if encoded, err := json.Marshal(structured); err == nil {
				taken.structured, taken.opaque = encoded, false
			}
		}
	}
	return taken
}

// rawCopy is JSON the result holds already, copied: with none there, the client is sent null.
func rawCopy(raw json.RawMessage) []byte {
	if len(raw) == 0 {
		return []byte("null")
	}
	return bytes.Clone(raw)
}

var (
	rawMessage = reflect.TypeFor[json.RawMessage]()
	jsonNumber = reflect.TypeFor[json.Number]()
	timeTime   = reflect.TypeFor[time.Time]()
)

// The methods encoding/json calls on a value to write it, in one Go version or another:
// MarshalJSON and MarshalText in all, MarshalJSONTo and AppendText from Go 1.27, and IsZero for a
// field tagged omitzero.
var marshalHooks = [...]string{"MarshalJSON", "MarshalJSONTo", "MarshalText", "AppendText", "IsZero"}

// hooked reports whether a type, or its pointer, has a method encoding/json may call.
func hooked(kind reflect.Type) bool {
	has := func(t reflect.Type) bool {
		for _, name := range marshalHooks {
			if _, ok := t.MethodByName(name); ok {
				return true
			}
		}
		return false
	}
	if has(kind) {
		return true
	}
	if kind.Kind() == reflect.Pointer || kind.Kind() == reflect.Interface {
		return false
	}
	return has(reflect.PointerTo(kind))
}

// written is the fields of a struct type that encoding/json writes, each by its path through
// the structs embedded in it. ok is false for a type whose fields can't be told from its
// declaration alone: two of them, at any depth, would be written under one name, and which one
// wins is the encoder's to decide.
type written struct {
	ok     bool
	fields [][]int
	// What their names take in the JSON, with their quotes and colons.
	names int64
}

// A field's name in the JSON that every encoder reads the same way, and that needs no escaping.
var plainName = regexp.MustCompile(`^[A-Za-z0-9_.-]*$`)

// writtenFields reads a struct type's fields as encoding/json does: an exported field under its
// name or its tag's, not one tagged "-"; an embedded struct's fields as the outer struct's own.
// Only tags every Go version reads the same way are read: a name of letters, digits, "_", "."
// and "-", with omitempty or omitzero. With any other tag (a quoted name, string, an option
// only a later encoding/json knows), which fields are written is the encoder's to say.
func writtenFields(kind reflect.Type) *written {
	out := &written{}
	names := map[string]bool{}
	var read func(kind reflect.Type, path []int, depth int) bool
	read = func(kind reflect.Type, path []int, depth int) bool {
		if depth > 8 {
			return false
		}
		for i := 0; i < kind.NumField(); i++ {
			field := kind.Field(i)
			embedded := field.Type
			if embedded.Kind() == reflect.Pointer {
				embedded = embedded.Elem()
			}
			if field.Anonymous {
				if !field.IsExported() && embedded.Kind() != reflect.Struct {
					continue
				}
			} else if !field.IsExported() {
				continue
			}
			tag := field.Tag.Get("json")
			if tag == "-" {
				continue
			}
			name, options, _ := strings.Cut(tag, ",")
			if !plainName.MatchString(name) {
				return false
			}
			for options != "" {
				var option string
				option, options, _ = strings.Cut(options, ",")
				if option != "omitempty" && option != "omitzero" {
					return false
				}
			}
			index := append(slices.Clone(path), i)
			if field.Anonymous && name == "" && embedded.Kind() == reflect.Struct {
				if !read(embedded, index, depth+1) {
					return false
				}
				continue
			}
			if name == "" {
				name = field.Name
			}
			if names[name] {
				return false
			}
			names[name] = true
			out.fields = append(out.fields, index)
			// Its quotes, its colon and a comma.
			out.names += int64(len(name)) + 4
		}
		return true
	}
	out.ok = read(kind, nil, 0)
	return out
}

// measure walks a value to see whether it is passive, and how large its JSON is at most.
type measure struct {
	seen   int
	size   int64
	fields map[reflect.Type]*written
}

// passive reports whether a value can be written as JSON without running any code of the app's:
// it is made of structs, maps with string keys, slices, arrays, strings, numbers, booleans and
// nil, none of a type with a method encoding/json may call (json.RawMessage, json.Number and
// time.Time apart, whose methods are Go's). Of a struct it reads the fields encoding/json
// writes and no other: a field left out of the JSON may be one the app is changing meanwhile.
// It looks at no more than 10,000 values, 32 levels deep: past that the answer is no.
func (m *measure) passive(value reflect.Value, depth int) bool {
	if m.seen++; m.seen > 10_000 || depth > 32 {
		return false
	}
	// A comma, or a bracket's other half.
	m.size++
	if !value.IsValid() {
		m.size += 4
		return true
	}
	kind := value.Type()
	if kind.Kind() == reflect.Pointer && (kind.Elem() == rawMessage || kind.Elem() == jsonNumber || kind.Elem() == timeTime) {
		// A pointer to one of Go's own: an optional timestamp, say.
		if value.IsNil() {
			m.size += 4
			return true
		}
		value, kind = value.Elem(), kind.Elem()
	}
	switch kind {
	case rawMessage:
		m.size += rawSize(value.Bytes())
		return true
	case jsonNumber:
		m.size += int64(value.Len()) + 2
		return true
	case timeTime:
		m.size += 40
		return true
	}
	if hooked(kind) {
		return false
	}
	switch value.Kind() {
	case reflect.Bool:
		m.size += 5
		return true
	case reflect.Int8, reflect.Uint8:
		m.size += 4
		return true
	case reflect.Int16, reflect.Uint16:
		m.size += 6
		return true
	case reflect.Int32, reflect.Uint32:
		m.size += 11
		return true
	case reflect.Int, reflect.Int64, reflect.Uint, reflect.Uint64, reflect.Uintptr:
		m.size += 20
		return true
	case reflect.Float32, reflect.Float64:
		// A float's longest: its digits, point, exponent and signs.
		m.size += 25
		return true
	case reflect.String:
		m.size += jsonStringSize(value.String())
		return true
	case reflect.Pointer, reflect.Interface:
		if value.IsNil() {
			m.size += 4
			return true
		}
		return m.passive(value.Elem(), depth+1)
	case reflect.Slice, reflect.Array:
		if value.Kind() == reflect.Slice && value.IsNil() {
			m.size += 4
			return true
		}
		m.size += 2
		if element := kind.Elem(); element.Kind() == reflect.Uint8 {
			if hooked(element) {
				return false
			}
			// A slice of bytes is written as base64 text; an array of them as numbers.
			if value.Kind() == reflect.Slice {
				m.size += (int64(value.Len()) + 2) / 3 * 4
			} else {
				m.size += int64(value.Len()) * 4
			}
			return true
		}
		for i := 0; i < value.Len(); i++ {
			if !m.passive(value.Index(i), depth+1) {
				return false
			}
		}
		return true
	case reflect.Map:
		if kind.Key().Kind() != reflect.String || hooked(kind.Key()) {
			return false
		}
		if value.IsNil() {
			m.size += 4
			return true
		}
		m.size += 2
		for iter := value.MapRange(); iter.Next(); {
			m.size += jsonStringSize(iter.Key().String()) + 1
			if !m.passive(iter.Value(), depth+1) {
				return false
			}
		}
		return true
	case reflect.Struct:
		fields := m.fields[kind]
		if fields == nil {
			fields = writtenFields(kind)
			m.fields[kind] = fields
		}
		if !fields.ok {
			return false
		}
		m.size += 2 + fields.names
		for _, index := range fields.fields {
			// Through a nil pointer to an embedded struct there is no field: nothing is written.
			field, err := value.FieldByIndexErr(index)
			if err != nil {
				continue
			}
			if !m.passive(field, depth+1) {
				return false
			}
		}
		return true
	}
	return false
}

// rawSize is the most JSON takes once encoding/json has written it inside other JSON: it
// escapes <, > and & and the line separators there too, and writes none as null.
func rawSize(raw []byte) int64 {
	if len(raw) == 0 {
		return 4
	}
	size := int64(len(raw))
	for i := 0; i < len(raw); i++ {
		switch c := raw[i]; {
		case c == '<', c == '>', c == '&':
			size += 5
		case c == 0xe2 && i+2 < len(raw) && raw[i+1] == 0x80 && (raw[i+2] == 0xa8 || raw[i+2] == 0xa9):
			size += 3
		}
	}
	return size
}

// jsonStringSize is the most a string takes as JSON, with its quotes: what encoding/json
// escapes takes six bytes (a control character, <, > and &, a line separator, a byte that isn't
// UTF-8), or two where there is a short escape (a quote, a backslash, a tab, a line's end).
func jsonStringSize(text string) int64 {
	size := int64(len(text)) + 2
	for i := 0; i < len(text); {
		c := text[i]
		if c < utf8.RuneSelf {
			switch {
			case c == '"', c == '\\', c == '\n', c == '\r', c == '\t', c == '\b', c == '\f':
				size++
			case c < 0x20, c == '<', c == '>', c == '&':
				size += 5
			}
			i++
			continue
		}
		r, width := utf8.DecodeRuneInString(text[i:])
		switch {
		case r == utf8.RuneError && width == 1:
			size += 5
		case r == 0x2028 || r == 0x2029:
			size += 3
		}
		i += width
	}
	return size
}

func blockValue(block mcp.Content) *jsonv.Object {
	out := jsonv.NewObject()
	file := func(kind, mimeType string, data []byte) {
		out.Set("type", kind)
		out.Set("mimeType", jsonv.ValidString(mimeType))
		out.Set("data", jsonv.Blob{Bytes: int64(len(data))})
	}
	text := func(key, value string) {
		if value != "" {
			out.Set(key, jsonv.ValidString(value))
		}
	}
	// Another kind, or a block that is nil, keeps only a type: its fields could hold anything.
	out.Set("type", "unknown")
	switch b := block.(type) {
	case *mcp.TextContent:
		if b != nil {
			out.Set("type", "text")
			out.Set("text", jsonv.ValidString(b.Text))
		}
	case *mcp.ImageContent:
		if b != nil {
			file("image", b.MIMEType, b.Data)
		}
	case *mcp.AudioContent:
		if b != nil {
			file("audio", b.MIMEType, b.Data)
		}
	case *mcp.ResourceLink:
		if b != nil {
			out.Set("type", "resource_link")
			text("uri", b.URI)
			text("name", b.Name)
			text("mimeType", b.MIMEType)
		}
	case *mcp.EmbeddedResource:
		if b != nil {
			out.Set("type", "resource")
			if b.Resource != nil {
				resource := jsonv.NewObject()
				resource.Set("uri", jsonv.ValidString(b.Resource.URI))
				if b.Resource.MIMEType != "" {
					resource.Set("mimeType", jsonv.ValidString(b.Resource.MIMEType))
				}
				if b.Resource.Text != "" {
					resource.Set("text", jsonv.ValidString(b.Resource.Text))
				} else if b.Resource.Blob != nil {
					resource.Set("blob", jsonv.Blob{Bytes: int64(len(b.Resource.Blob))})
				}
				out.Set("resource", resource)
			}
		}
	case *mcp.ToolUseContent:
		out.Set("type", "tool_use")
	case *mcp.ToolResultContent:
		out.Set("type", "tool_result")
	}
	return out
}

// value is the snapshot as the result's JSON, its structured content read now.
func (s *snapshot) value() *jsonv.Object {
	out := jsonv.NewObject()
	out.Set("content", s.blocks)
	if s.opaque {
		out.Set("structuredContent", jsonv.NotJSON{})
	} else if s.structured != nil {
		structured, err := jsonv.Decode(s.structured)
		if err != nil {
			structured = jsonv.NotJSON{}
		}
		out.Set("structuredContent", structured)
	}
	if s.isError {
		out.Set("isError", true)
	}
	return out
}

// who waits for Identify's answer, until its deadline: one that came later doesn't count, though
// it came before anyone looked.
func (call *running) who() *identified {
	if call.identified == nil {
		return nil
	}
	late := &identified{why: "took longer than its deadline"}
	timer := time.NewTimer(time.Until(call.deadline))
	defer timer.Stop()
	var answer identified
	select {
	case answer = <-call.identified:
	case <-timer.C:
		if call.gate.expire() {
			return late
		}
		// Back in time: only the reading of its answer is left, which is the SDK's own work.
		answer = <-call.identified
	}
	// At its deadline it is late, as in the other SDKs.
	if !answer.at.Before(call.deadline) {
		return late
	}
	return &answer
}

// build waits for Identify, at most a second after the tool returned, builds the call's record
// and hands it to the client.
func (w *instrumented) build(call *running, how outcome, took time.Duration) {
	who := call.who()
	call.stop()
	record := jsonv.NewObject()
	record.Set("callId", call.callID)
	record.Set("capturedAt", call.capturedAt)
	record.Set("toolName", jsonv.ValidString(call.toolName))
	record.Set("status", how.status)
	record.Set("durationMs", jsonv.Int(int64(math.Round(float64(max(took, 0))/float64(time.Millisecond)))))
	var diagnostic *bridge.Report
	ids := jsonv.NewObject()
	record.Set("diagnostics", false)
	// The IDs: Identify's, then the capture's inside the call, which win, each by itself. One the
	// capture gives that isn't valid drops the event, as invalid_id.
	captured := jsonv.NewObject()
	if call.agent != nil {
		captured = call.agent.IDs
	}
	if who != nil {
		if who.why != "" {
			// Limited diagnostics, unless a capture inside the call gave each ID Identify last
			// returned in time, on any server of the client (a userId before any answer).
			needed := call.gate.requiredKeys()
			if needed == nil {
				needed = []string{"userId"}
			}
			supplied := call.agent != nil
			for _, key := range needed {
				value, _ := captured.Get(key)
				text, isText := value.(string)
				supplied = supplied && isText && core.ValidID(text)
			}
			sent := "as limited diagnostics"
			if supplied {
				sent = "with Capture's IDs"
			}
			record.Set("diagnostics", !supplied)
			diagnostic = &bridge.Report{Code: string(lsquare.CodeIdentifyFailed), Message: "Identify " + who.why + "; the call was sent " + sent + "."}
		} else {
			ids = who.ids
			if who.metadata != nil {
				record.Set("metadata", who.metadata)
			}
		}
	}
	for i := 0; i < captured.Len(); i++ {
		key, value := captured.At(i)
		ids.Set(key, value)
	}
	record.Set("ids", ids)
	if call.agent != nil {
		record.Set("agent", call.agent.Shaped)
		// The turn's reference, from its conversation and turn ID and the call's org.
		var org *string
		if value, has := ids.Get("orgId"); has {
			text, _ := value.(string)
			org = &text
		}
		conversation, _ := captured.Get("conversationId")
		text, _ := conversation.(string)
		turnID := call.agent.Shaped.TurnID()
		if ref, ok := core.TurnRef(org, text, turnID); ok {
			record.Set("turnRef", ref)
		}
	}
	w.mu.Lock()
	server := w.server
	w.mu.Unlock()
	if server != nil {
		record.Set("server", server)
	}
	if call.client != nil {
		record.Set("client", call.client)
	}
	if call.protocol != "" {
		record.Set("protocolVersion", jsonv.ValidString(call.protocol))
	}
	if call.session != nil {
		record.Set("sessionId", call.session.id)
		record.Set("sessionSource", call.session.source)
	}
	// Recorded whether or not inputs are: turning the extra off is what stops it.
	if call.intent != "" {
		record.Set("intent", jsonv.ValidString(masked(call.intent, call.transport).(string)))
	}
	// The wrapper's own tool has the wrapper's description, which says nothing of the server.
	if call.description != "" && !call.answered {
		record.Set("toolDescription", jsonv.ValidString(call.description))
	}
	if call.instructions != "" {
		// Valid UTF-8 already: made so once, as they were noted.
		record.Set("serverInstructions", call.instructions)
	}
	if call.answered {
		record.Set("extra", "missing_tools")
	}
	if call.inputs {
		holder := jsonv.NewObject()
		if len(bytes.TrimSpace(call.arguments)) > 0 {
			arguments, err := jsonv.Decode(call.arguments)
			if err != nil {
				arguments = jsonv.NotJSON{}
			}
			holder.Set("value", masked(arguments, call.transport))
		}
		record.Set("input", holder)
	}
	if how.failure != nil {
		if message, ok := how.failure.Get("message"); ok {
			if text, isText := message.(string); isText {
				how.failure.Set("message", jsonv.ValidString(text))
			}
		}
		record.Set("error", how.failure)
	}
	if how.round != "" {
		record.Set("round", how.round)
	}
	if how.result != nil {
		holder := jsonv.NewObject()
		holder.Set("value", how.result.value())
		record.Set("result", holder)
	}
	w.client.Record(record, diagnostic)
}

// masked is a call's input, or its intent, without the ID of the connection it came on: that ID
// is a live handle to the session, so wherever a text repeats it, the event has its hash, as it
// does for the session. Only what a text says changes: no key, and nothing of the JSON.
func masked(value jsonv.Value, id string) jsonv.Value {
	if id == "" {
		return value
	}
	switch v := value.(type) {
	case string:
		if strings.Contains(v, id) {
			return strings.ReplaceAll(v, id, sessionHash(id))
		}
	case []jsonv.Value:
		for i, item := range v {
			v[i] = masked(item, id)
		}
	case *jsonv.Object:
		for _, key := range slices.Clone(v.Keys()) {
			item, _ := v.Get(key)
			v.Set(key, masked(item, id))
		}
	}
	return value
}
