package lsquaremcp

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"
	"weak"

	"github.com/modelcontextprotocol/go-sdk/auth"
	"github.com/modelcontextprotocol/go-sdk/jsonrpc"
	"github.com/modelcontextprotocol/go-sdk/mcp"

	lsquare "github.com/theagentexperiencecompany/lsquare/packages/go"
	"github.com/theagentexperiencecompany/lsquare/packages/go/internal/bridge"
	"github.com/theagentexperiencecompany/lsquare/packages/go/internal/contracttest"
	"github.com/theagentexperiencecompany/lsquare/packages/go/internal/jsonv"
)

func bridgeOf(client *lsquare.Client) bridge.Client { return bridge.Of(client) }

// harness is an MCP server with a few tools, instrumented, a client connected to it in memory,
// and a stand-in for LSquare's ingestion that keeps the events it is sent.
type harness struct {
	t       *testing.T
	server  *mcp.Server
	client  *lsquare.Client
	session *mcp.ClientSession
	mu      sync.Mutex
	raw     [][]byte
	errors  []*lsquare.Error
}

type searchInput struct {
	Query string `json:"query" jsonschema:"what to search for"`
	Token string `json:"token,omitempty"`
}

type searchOutput struct {
	Count int    `json:"count"`
	Owner string `json:"owner"`
}

var searchTool = &mcp.Tool{Name: "search_orders", Description: "Searches the orders of a customer. Ask ops@example.com for access."}

func addTools(server *mcp.Server) {
	mcp.AddTool(server, searchTool, func(ctx context.Context, request *mcp.CallToolRequest, input searchInput) (*mcp.CallToolResult, searchOutput, error) {
		switch input.Query {
		case "fail":
			return nil, searchOutput{}, errors.New("no orders for " + input.Query + " at alice@example.com")
		case "slow":
			<-ctx.Done()
			return nil, searchOutput{}, ctx.Err()
		}
		return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: "3 orders for " + input.Query}}}, searchOutput{Count: 3, Owner: "bob@example.com"}, nil
	})
	mcp.AddTool(server, &mcp.Tool{Name: "files"}, func(ctx context.Context, request *mcp.CallToolRequest, input struct{}) (*mcp.CallToolResult, any, error) {
		return &mcp.CallToolResult{Content: []mcp.Content{
			&mcp.ImageContent{MIMEType: "image/png", Data: make([]byte, 2048)},
			&mcp.AudioContent{MIMEType: "audio/wav", Data: make([]byte, 100)},
			&mcp.ResourceLink{URI: "file:///orders.csv", Name: "orders.csv", MIMEType: "text/csv"},
			&mcp.EmbeddedResource{Resource: &mcp.ResourceContents{URI: "file:///a.txt", MIMEType: "text/plain", Text: "written by carol@example.com"}},
			&mcp.EmbeddedResource{Resource: &mcp.ResourceContents{URI: "file:///a.bin", Blob: make([]byte, 512)}},
		}}, nil, nil
	})
	server.AddTool(&mcp.Tool{Name: "raw", InputSchema: map[string]any{"type": "object"}}, func(ctx context.Context, request *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		var arguments struct{ Mode string }
		_ = json.Unmarshal(request.Params.Arguments, &arguments)
		switch arguments.Mode {
		case "error":
			return nil, fmt.Errorf("lookup failed: %w", io.ErrUnexpectedEOF)
		case "protocol":
			return nil, &jsonrpc.Error{Code: -32000, Message: "not today"}
		}
		return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: "raw"}}}, nil
	})
}

func newHarness(t *testing.T, options *Options, before lsquare.Options) *harness {
	t.Helper()
	return newHarnessOn(t, options, before, nil)
}

// newHarnessOn is newHarness with the client's own choice of protocol version.
func newHarnessOn(t *testing.T, options *Options, before lsquare.Options, session *mcp.ClientSessionOptions) *harness {
	t.Helper()
	return newHarnessWith(t, before, session, func(h *harness) {
		addTools(h.server)
		Instrument(h.server, h.client, options)
	})
}

// newHarnessWith is a harness whose server build sets up: it adds the tools and instruments it.
func newHarnessWith(t *testing.T, before lsquare.Options, session *mcp.ClientSessionOptions, build func(h *harness)) *harness {
	t.Helper()
	h := &harness{t: t}
	ingestion := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		var envelope struct {
			Events []json.RawMessage `json:"events"`
		}
		_ = json.Unmarshal(body, &envelope)
		ids := []string{}
		h.mu.Lock()
		for _, event := range envelope.Events {
			h.raw = append(h.raw, event)
			var head struct {
				EventID string `json:"eventId"`
			}
			_ = json.Unmarshal(event, &head)
			ids = append(ids, head.EventID)
		}
		h.mu.Unlock()
		_ = json.NewEncoder(w).Encode(map[string]any{"accepted": ids, "duplicates": []string{}, "rejected": []any{}})
	}))
	t.Cleanup(ingestion.Close)
	before.APIKey, before.Endpoint, before.DryRun = "lsq_test", ingestion.URL, lsquare.DryRunOff
	before.OnError = func(err *lsquare.Error, event *lsquare.Event) {
		h.mu.Lock()
		h.errors = append(h.errors, err)
		h.mu.Unlock()
	}
	h.client = lsquare.New(before)
	h.server = mcp.NewServer(&mcp.Implementation{Name: "orders", Version: "1.2.3"}, nil)
	build(h)

	serverTransport, clientTransport := mcp.NewInMemoryTransports()
	ctx := context.Background()
	serverSession, err := h.server.Connect(ctx, serverTransport, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = serverSession.Close() })
	h.session, err = mcp.NewClient(&mcp.Implementation{Name: "test-agent", Version: "0.9"}, nil).Connect(ctx, clientTransport, session)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = h.session.Close() })
	// Whatever the test got from the wrapper passes the ingestion's checks.
	t.Cleanup(func() {
		h.mu.Lock()
		events := append([][]byte(nil), h.raw...)
		h.mu.Unlock()
		if t.Failed() || len(events) == 0 {
			return
		}
		for i, verdict := range contracttest.Ingestion(t, events) {
			if verdict != "valid" {
				t.Errorf("the ingestion rejects an event as %s: %s", verdict, events[i])
			}
		}
	})
	return h
}

func (h *harness) call(tool string, arguments any) (*mcp.CallToolResult, error) {
	return h.session.CallTool(context.Background(), &mcp.CallToolParams{Name: tool, Arguments: arguments})
}

// events flushes the client and returns the events sent so far.
func (h *harness) events() []map[string]any {
	h.t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if result := h.client.Flush(ctx); result.Pending != 0 {
		h.t.Fatalf("flush left %+v", result)
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	out := make([]map[string]any, len(h.raw))
	for i, raw := range h.raw {
		if err := json.Unmarshal(raw, &out[i]); err != nil {
			h.t.Fatal(err)
		}
	}
	return out
}

func (h *harness) one() map[string]any {
	h.t.Helper()
	events := h.events()
	if len(events) != 1 {
		h.t.Fatalf("%d events, want 1: %v", len(events), events)
	}
	return events[0]
}

func (h *harness) reported() []string {
	h.mu.Lock()
	defer h.mu.Unlock()
	out := make([]string, len(h.errors))
	for i, err := range h.errors {
		out[i] = string(err.Code) + ": " + err.Message
	}
	return out
}

func keys(event map[string]any) string {
	names := make([]string, 0, len(event))
	for name := range event {
		names = append(names, name)
	}
	sort.Strings(names)
	return strings.Join(names, ",")
}

func at(value any, path ...any) any {
	for _, step := range path {
		switch s := step.(type) {
		case string:
			object, _ := value.(map[string]any)
			value = object[s]
		case int:
			list, _ := value.([]any)
			if s >= len(list) {
				return nil
			}
			value = list[s]
		}
	}
	return value
}

func TestACallBecomesOneEvent(t *testing.T) {
	type contextKey struct{}
	var asked Call
	h := newHarness(t, &Options{
		Tools:      []*mcp.Tool{searchTool},
		ServerInfo: &mcp.Implementation{Name: "orders", Version: "1.2.3"},
		Identify: func(ctx context.Context, call Call) (*Identity, error) {
			asked = call
			return &Identity{OrgID: "org_1", UserID: "user_1", ConversationID: "conv_1", Metadata: map[string]any{"plan": "pro", "seats": 3}}, nil
		},
	}, lsquare.Options{})
	result, err := h.call("search_orders", map[string]any{"query": "alice@example.com", "token": "hunter2"})
	if err != nil || result.IsError {
		t.Fatalf("the call failed: %v %+v", err, result)
	}
	event := h.one()
	if got, want := keys(event), "callId,capturedAt,client,conversationId,durationMs,eventId,input,kind,metadata,orgId,protocolVersion,redactions,result,schemaVersion,sdk,server,sessionId,sessionSource,source,status,toolDescription,toolName,userId"; got != want {
		t.Errorf("the event's fields are %s, want %s", got, want)
	}
	for path, want := range map[string]any{
		"kind": "tool_call", "toolName": "search_orders", "status": "completed", "source": "live", "schemaVersion": float64(2),
		"orgId": "org_1", "userId": "user_1", "conversationId": "conv_1",
		"toolDescription": "Searches the orders of a customer. Ask [EMAIL_3] for access.",
	} {
		if got := event[path]; got != want {
			t.Errorf("%s is %v, want %v", path, got, want)
		}
	}
	if event["eventId"] != event["callId"] || len(fmt.Sprint(event["callId"])) != 36 {
		t.Errorf("the IDs are %v and %v", event["eventId"], event["callId"])
	}
	if _, err := time.Parse("2006-01-02T15:04:05.000Z", fmt.Sprint(event["capturedAt"])); err != nil {
		t.Errorf("capturedAt is %v", event["capturedAt"])
	}
	if duration, ok := event["durationMs"].(float64); !ok || duration < 0 || duration != float64(int(duration)) {
		t.Errorf("durationMs is %v", event["durationMs"])
	}
	for _, check := range []struct {
		path []any
		want any
	}{
		{[]any{"sdk", "language"}, "go"},
		{[]any{"sdk", "name"}, "lsquare"},
		{[]any{"client", "name"}, "test-agent"},
		{[]any{"client", "version"}, "0.9"},
		{[]any{"server", "name"}, "orders"},
		{[]any{"server", "version"}, "1.2.3"},
		{[]any{"input", "query"}, "[EMAIL_1]"},
		{[]any{"input", "token"}, "[SECRET_1]"},
		{[]any{"result", "content", 0, "type"}, "text"},
		{[]any{"result", "content", 0, "text"}, "3 orders for [EMAIL_1]"},
		{[]any{"result", "structuredContent", "count"}, float64(3)},
		{[]any{"result", "structuredContent", "owner"}, "[EMAIL_2]"},
		{[]any{"metadata", "plan"}, "pro"},
		{[]any{"metadata", "seats"}, float64(3)},
		{[]any{"redactions", "email"}, float64(4)},
		{[]any{"redactions", "secret"}, float64(1)},
	} {
		if got := at(event, check.path...); got != check.want {
			t.Errorf("%v is %v, want %v", check.path, got, check.want)
		}
	}
	if version, _ := event["protocolVersion"].(string); len(version) != len("2025-06-18") {
		t.Errorf("protocolVersion is %v", event["protocolVersion"])
	}
	if _, has := at(event, "result").(map[string]any)["isError"]; has {
		t.Error("a result that isn't an error says isError")
	}
	if asked.ToolName != "search_orders" || !strings.Contains(string(asked.Arguments), `"query":"alice@example.com"`) {
		t.Errorf("Identify was asked about %+v", asked)
	}
	if reported := h.reported(); len(reported) != 0 {
		t.Errorf("reported %v", reported)
	}
	_ = contextKey{}
}

func TestHowACallFailed(t *testing.T) {
	for _, test := range []struct {
		name      string
		tool      string
		arguments any
		category  string
		message   any
		code      any
		kind      any
		result    bool
	}{
		{"an error the tool returns, which the SDK turns into a result", "search_orders", map[string]any{"query": "fail"}, "tool_error", "no orders for fail at [EMAIL_1]", nil, nil, true},
		{"arguments the SDK refuses", "search_orders", map[string]any{"query": 7}, "tool_error", `validating "arguments": validating root: validating /properties/query: type: 7 has type "integer", want "string"`, nil, nil, true},
		{"a tool that isn't there", "nothing", map[string]any{}, "protocol", `unknown tool "nothing"`, float64(-32602), nil, false},
		{"an error a raw handler returns", "raw", map[string]any{"mode": "error"}, "thrown", "lookup failed: unexpected EOF", nil, "fmt.wrapError", false},
		{"a JSON-RPC error a handler returns", "raw", map[string]any{"mode": "protocol"}, "protocol", "not today", float64(-32000), nil, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			h := newHarness(t, nil, lsquare.Options{})
			_, _ = h.call(test.tool, test.arguments)
			event := h.one()
			failure, _ := event["error"].(map[string]any)
			if event["status"] != "error" || failure["category"] != test.category || failure["message"] != test.message || failure["code"] != test.code || failure["type"] != test.kind {
				t.Errorf("status %v, error %v", event["status"], failure)
			}
			if _, has := event["result"]; has != test.result {
				t.Errorf("the result: %v", event["result"])
			}
			if test.result && at(event, "result", "isError") != true {
				t.Errorf("the result doesn't say isError: %v", event["result"])
			}
		})
	}
}

// A failed call's text can quote its arguments: with either inputs or results off, neither the
// error's message nor the result is recorded.
func TestAFailedCallsTextNeedsBothInputsAndResults(t *testing.T) {
	for name, options := range map[string]*Options{"inputs off": {Inputs: Off}, "results off": {Results: Off}} {
		t.Run(name, func(t *testing.T) {
			h := newHarness(t, options, lsquare.Options{})
			_, _ = h.call("search_orders", map[string]any{"query": "fail"})
			_, _ = h.call("nothing", map[string]any{"query": "warehouse-canary"})
			for _, event := range h.events() {
				failure, _ := event["error"].(map[string]any)
				if _, has := failure["message"]; has || failure["category"] == nil {
					t.Errorf("the error is %v", failure)
				}
				if _, has := event["result"]; has {
					t.Errorf("the result is recorded: %v", event["result"])
				}
				// The arguments themselves are there only where inputs are recorded.
				if _, has := event["input"]; has != (name == "results off") {
					t.Errorf("the input: %v", event["input"])
				}
				without := map[string]any{}
				for key, value := range event {
					if key != "input" {
						without[key] = value
					}
				}
				if text := fmt.Sprint(without); strings.Contains(text, "canary") || strings.Contains(text, "no orders") {
					t.Errorf("the event quotes the call: %v", event)
				}
			}
		})
	}
}

func TestACancelledCallIsATimeout(t *testing.T) {
	h := newHarness(t, nil, lsquare.Options{})
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	_, _ = h.session.CallTool(ctx, &mcp.CallToolParams{Name: "search_orders", Arguments: map[string]any{"query": "slow"}})
	var event map[string]any
	for deadline := time.Now().Add(3 * time.Second); time.Now().Before(deadline); time.Sleep(10 * time.Millisecond) {
		if events := h.events(); len(events) == 1 {
			event = events[0]
			break
		}
	}
	failure, _ := event["error"].(map[string]any)
	if event["status"] != "error" || failure["category"] != "timeout" || failure["message"] != "The request was cancelled before the tool finished." {
		t.Errorf("status %v, error %v", event["status"], failure)
	}
	if _, has := event["result"]; has {
		t.Errorf("a cancelled call has a result: %v", event["result"])
	}
}

func TestFilesAreOnlyTheirSize(t *testing.T) {
	h := newHarness(t, nil, lsquare.Options{})
	if _, err := h.call("files", map[string]any{}); err != nil {
		t.Fatal(err)
	}
	event := h.one()
	content, _ := json.Marshal(at(event, "result", "content"))
	want := `[{"bytes":2048,"mimeType":"image/png","type":"image"},{"bytes":100,"mimeType":"audio/wav","type":"audio"},` +
		`{"mimeType":"text/csv","name":"orders.csv","type":"resource_link","uri":"file:///orders.csv"},` +
		`{"mimeType":"text/plain","text":"written by [EMAIL_1]","type":"resource","uri":"file:///a.txt"},{"bytes":512,"type":"resource","uri":"file:///a.bin"}]`
	if string(content) != want {
		t.Errorf("the content is\n%s\nwant\n%s", content, want)
	}
	if truncated, _ := event["truncated"].([]any); len(truncated) != 1 || truncated[0] != "result" {
		t.Errorf("truncated is %v", event["truncated"])
	}
}

func TestWhatIsRecordedIsChosenPerTool(t *testing.T) {
	h := newHarness(t, &Options{
		Inputs:       func(tool string) bool { return tool != "search_orders" },
		Results:      Off,
		Descriptions: func(tool string) bool { panic("no") },
		Tools:        []*mcp.Tool{searchTool},
	}, lsquare.Options{Logger: quiet{}})
	_, _ = h.call("search_orders", map[string]any{"query": "x"})
	_, _ = h.call("raw", map[string]any{"mode": "ok"})
	events := h.events()
	if len(events) != 2 {
		t.Fatalf("%d events", len(events))
	}
	for _, event := range events {
		_, hasInput := event["input"]
		_, hasResult := event["result"]
		_, hasDescription := event["toolDescription"]
		if hasInput != (event["toolName"] == "raw") || hasResult || hasDescription {
			t.Errorf("%v has input %v, result %v, description %v", event["toolName"], hasInput, hasResult, hasDescription)
		}
	}
}

type quiet struct{}

func (quiet) Info(string) {}
func (quiet) Warn(string) {}

func TestADescriptionIsTheOneLastListed(t *testing.T) {
	// With the extras off the wrapper never lists the tools itself: before any listing, and
	// without Options.Tools, a call has no description.
	h := newHarness(t, &Options{Extras: Extras{DisableSessions: true, DisableIntent: true, DisableMissingTools: true}}, lsquare.Options{})
	_, _ = h.call("search_orders", map[string]any{"query": "x"})
	if _, err := h.session.ListTools(context.Background(), nil); err != nil {
		t.Fatal(err)
	}
	_, _ = h.call("search_orders", map[string]any{"query": "x"})
	_, _ = h.call("files", map[string]any{})
	events := h.events()
	if len(events) != 3 {
		t.Fatalf("%d events", len(events))
	}
	described := 0
	for _, event := range events {
		if description, has := event["toolDescription"]; has {
			described++
			if event["toolName"] != "search_orders" || !strings.HasPrefix(fmt.Sprint(description), "Searches the orders") {
				t.Errorf("%v is described as %v", event["toolName"], description)
			}
		}
	}
	if described != 1 {
		t.Errorf("%d calls have a description, want the one after the listing of the tool that has one", described)
	}
}

func TestIdentifyFailingSendsLimitedDiagnostics(t *testing.T) {
	for _, test := range []struct {
		name     string
		identify func(ctx context.Context, call Call) (*Identity, error)
		why      string
	}{
		{"an error", func(context.Context, Call) (*Identity, error) { return nil, errors.New("no session") }, "returned an error"},
		{"a panic", func(context.Context, Call) (*Identity, error) { panic("no") }, "panicked"},
		{"an invalid ID", func(context.Context, Call) (*Identity, error) { return &Identity{UserID: "a\x00b"}, nil }, "returned an invalid userId"},
		{"too slow", func(ctx context.Context, call Call) (*Identity, error) {
			<-ctx.Done()
			return &Identity{UserID: "late"}, nil
		}, "took longer than its deadline"},
	} {
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()
			h := newHarness(t, &Options{Identify: test.identify, Tools: []*mcp.Tool{searchTool}}, lsquare.Options{})
			_, _ = h.call("search_orders", map[string]any{"query": "fail"})
			event := h.one()
			if got, want := keys(event), "callId,capturedAt,durationMs,error,eventId,identity,kind,schemaVersion,sdk,source,status,toolName"; got != want {
				t.Errorf("the event's fields are %s, want %s", got, want)
			}
			failure, _ := json.Marshal(event["error"])
			if event["identity"] != "failed" || event["status"] != "error" || string(failure) != `{"category":"tool_error"}` {
				t.Errorf("identity %v, status %v, error %s", event["identity"], event["status"], failure)
			}
			want := "identify_failed: Identify " + test.why + "; the call was sent as limited diagnostics."
			if reported := h.reported(); len(reported) != 1 || reported[0] != want {
				t.Errorf("reported %v, want %q", reported, want)
			}
		})
	}
}

func TestNothingIsWaitedForBeforeTheReply(t *testing.T) {
	release := make(chan struct{})
	h := newHarness(t, &Options{Identify: func(ctx context.Context, call Call) (*Identity, error) {
		<-release
		return &Identity{UserID: "user_1"}, nil
	}}, lsquare.Options{})
	started := time.Now()
	if _, err := h.call("search_orders", map[string]any{"query": "x"}); err != nil {
		t.Fatal(err)
	}
	if took := time.Since(started); took > 500*time.Millisecond {
		t.Errorf("the reply waited %v for Identify", took)
	}
	close(release)
	if event := h.one(); event["userId"] != "user_1" {
		t.Errorf("the event is %v", event)
	}
}

// A client on a protocol version with an initialize: the event has the version the server
// negotiated, not the one the client asked for, and the server's name as it answered with it.
func TestAConnectionThatInitializes(t *testing.T) {
	for asked, want := range map[string]string{"2025-06-18": "2025-06-18", "2025-03-26": "2025-03-26", "2024-01-01": ""} {
		t.Run(asked, func(t *testing.T) {
			h := newHarnessOn(t, nil, lsquare.Options{}, &mcp.ClientSessionOptions{ProtocolVersion: asked})
			_, _ = h.call("search_orders", map[string]any{"query": "x"})
			event := h.one()
			negotiated := h.session.InitializeResult().ProtocolVersion
			if want == "" {
				// One the server doesn't have: it answers with another, which is the one recorded.
				want = negotiated
				if negotiated == asked {
					t.Fatalf("the server took %s", asked)
				}
			}
			if event["protocolVersion"] != want || negotiated != want {
				t.Errorf("protocolVersion is %v, the server negotiated %s, want %s", event["protocolVersion"], negotiated, want)
			}
			if at(event, "server", "name") != "orders" || at(event, "server", "version") != "1.2.3" || at(event, "client", "name") != "test-agent" {
				t.Errorf("server %v, client %v", event["server"], event["client"])
			}
		})
	}
}

// The first Instrument of a server is the one that counts: a second one, with other options or
// another client, neither records the call again nor brings back what the first left out.
func TestInstrumentingTwiceChangesNothing(t *testing.T) {
	h := newHarness(t, &Options{Inputs: Off, Results: Off}, lsquare.Options{})
	Instrument(h.server, h.client, nil)
	other := lsquare.New(lsquare.Options{DryRun: lsquare.DryRunOn, Logger: quiet{}})
	Instrument(h.server, other, nil)
	_, _ = h.call("search_orders", map[string]any{"query": "x"})
	event := h.one()
	if _, has := event["input"]; has {
		t.Errorf("the input is recorded: %v", event["input"])
	}
	if _, has := event["result"]; has {
		t.Errorf("the result is recorded: %v", event["result"])
	}
}

// With a client that records nothing, the server is left as it is, and can be instrumented later.
func TestADisabledClientLeavesTheServerAlone(t *testing.T) {
	t.Setenv("LSQUARE_DRY_RUN", "")
	h := newHarness(t, nil, lsquare.Options{})
	server := mcp.NewServer(&mcp.Implementation{Name: "other"}, nil)
	Instrument(server, lsquare.New(lsquare.Options{Logger: quiet{}}), nil)
	serversMu.Lock()
	marked := servers[weak.Make(server)]
	serversMu.Unlock()
	if marked {
		t.Error("a disabled client took the server")
	}
	_ = h
}

// A tool that panics: the panic goes on as it would without the wrapper, and the call is recorded.
func TestAPanicInTheToolGoesOn(t *testing.T) {
	h := newHarness(t, nil, lsquare.Options{})
	handler := newInstrumented(bridgeOf(h.client), nil).middleware(func(ctx context.Context, method string, request mcp.Request) (mcp.Result, error) {
		panic("the tool's own")
	})
	recovered := func() (value any) {
		defer func() { value = recover() }()
		_, _ = handler(context.Background(), "tools/call", &mcp.CallToolRequest{Params: &mcp.CallToolParamsRaw{Name: "search_orders", Arguments: json.RawMessage(`{"query":"x"}`)}})
		return nil
	}()
	if recovered != "the tool's own" {
		t.Fatalf("the panic was %v", recovered)
	}
	event := h.one()
	failure, _ := json.Marshal(event["error"])
	if event["status"] != "error" || string(failure) != `{"category":"thrown","type":"panic"}` {
		t.Errorf("status %v, error %s", event["status"], failure)
	}
}

func TestBeforeSendGetsTheCall(t *testing.T) {
	var seen *lsquare.Event
	h := newHarness(t, &Options{Tools: []*mcp.Tool{searchTool}, ServerInfo: &mcp.Implementation{Name: "orders"}}, lsquare.Options{BeforeSend: func(ctx context.Context, event *lsquare.Event) (*lsquare.Event, error) {
		copied := *event
		seen = &copied
		event.ToolDescription = ""
		event.Client = &lsquare.Named{Name: "renamed"}
		return event, nil
	}})
	_, _ = h.call("search_orders", map[string]any{"query": "alice@example.com"})
	event := h.one()
	if seen == nil || seen.ToolName != "search_orders" || seen.Kind != "tool_call" || seen.Client.Name != "test-agent" || seen.Server.Name != "orders" ||
		string(seen.Input) != `{"query":"alice@example.com"}` || *seen.Result.Content[0].Text != "3 orders for alice@example.com" ||
		string(seen.Result.StructuredContent) != `{"count":3,"owner":"bob@example.com"}` || !strings.HasPrefix(seen.ToolDescription, "Searches") {
		t.Errorf("the hook got %+v", seen)
	}
	if _, has := event["toolDescription"]; has || at(event, "client", "name") != "renamed" || at(event, "input", "query") != "[EMAIL_1]" {
		t.Errorf("the event is %v", event)
	}
}

// direct runs one tools/call through the wrapper's middleware with a handler of the test's own,
// for what a real server and client can't be made to do.
func direct(h *harness, options *Options, request *mcp.CallToolRequest, next func() (mcp.Result, error)) (mcp.Result, error) {
	handler := newInstrumented(bridgeOf(h.client), options).middleware(func(ctx context.Context, method string, request mcp.Request) (mcp.Result, error) {
		if method != "tools/call" {
			// The wrapper's own look at the server's tools: this one lists none.
			return &mcp.ListToolsResult{}, nil
		}
		return next()
	})
	return handler(context.Background(), "tools/call", request)
}

func callRequest(arguments string) *mcp.CallToolRequest {
	return &mcp.CallToolRequest{Params: &mcp.CallToolParamsRaw{Name: "search_orders", Arguments: json.RawMessage(arguments)}}
}

// once writes its JSON a single time, as a value read from a stream would.
type once struct{ written bool }

func (o *once) MarshalJSON() ([]byte, error) {
	if o.written {
		return nil, errors.New("already written")
	}
	o.written = true
	return []byte(`{"n":1}`), nil
}

// Recording never runs the app's own marshalling: the reply is the first, and only, to do it.
func TestStructuredContentIsNeverMarshalledTwice(t *testing.T) {
	h := newHarness(t, nil, lsquare.Options{})
	stream := &once{}
	result, err := direct(h, nil, callRequest(`{}`), func() (mcp.Result, error) {
		return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: "ok"}}, StructuredContent: map[string]any{"stream": stream}}, nil
	})
	if err != nil || stream.written {
		t.Fatalf("the wrapper wrote the content itself: %v, written %v", err, stream.written)
	}
	if _, err := json.Marshal(result.(*mcp.CallToolResult).StructuredContent); err != nil {
		t.Fatalf("the reply can't be written any more: %v", err)
	}
	event := h.one()
	if _, has := at(event, "result").(map[string]any)["structuredContent"]; has || at(event, "dropped", "result.structuredContent") != "not_json" || at(event, "result", "content", 0, "text") != "ok" {
		t.Errorf("result %v, dropped %v", event["result"], event["dropped"])
	}
}

// Content that only holds data is recorded, whatever its Go type; a block that is nil, or of a
// kind a tool's result doesn't have, keeps only a type.
func TestPlainStructuredContentAndOddBlocks(t *testing.T) {
	type order struct {
		ID    string            `json:"id"`
		Total float64           `json:"total"`
		Tags  []string          `json:"tags"`
		Notes map[string]string `json:"notes"`
		Owner *struct {
			Email string `json:"email"`
		} `json:"owner"`
		hidden int
	}
	h := newHarness(t, nil, lsquare.Options{})
	value := order{ID: "ord_1", Total: 12.5, Tags: []string{"a"}, Notes: map[string]string{"2": "x"}, Owner: &struct {
		Email string `json:"email"`
	}{"dana@example.com"}}
	_, _ = direct(h, nil, callRequest(`{}`), func() (mcp.Result, error) {
		return &mcp.CallToolResult{Content: []mcp.Content{(*mcp.TextContent)(nil), &mcp.ToolUseContent{ID: "t", Name: "n", Input: map[string]any{"secret": "s"}}, nil}, StructuredContent: []order{value}}, nil
	})
	event := h.one()
	structured, _ := json.Marshal(at(event, "result", "structuredContent"))
	if want := `[{"id":"ord_1","notes":{"2":"x"},"owner":{"email":"[EMAIL_1]"},"tags":["a"],"total":12.5}]`; string(structured) != want {
		t.Errorf("structuredContent is %s, want %s", structured, want)
	}
	content, _ := json.Marshal(at(event, "result", "content"))
	if want := `[{"type":"unknown"},{"type":"tool_use"},{"type":"unknown"}]`; string(content) != want {
		t.Errorf("content is %s, want %s", content, want)
	}
}

// Identify gets copies: what it does with them the tool never sees.
func TestIdentifyGetsACopyOfTheCall(t *testing.T) {
	token := &auth.TokenInfo{UserID: "user_1", Scopes: []string{"read"}, Extra: map[string]any{"org": "org_1"}}
	header := http.Header{"X-Tenant": {"acme"}}
	request := callRequest(`{"query":"x"}`)
	request.Extra = &mcp.RequestExtra{TokenInfo: token, Header: header}
	asked := make(chan Call, 1)
	h := newHarness(t, nil, lsquare.Options{})
	_, _ = direct(h, &Options{Identify: func(ctx context.Context, call Call) (*Identity, error) {
		id := &Identity{UserID: call.TokenInfo.UserID, OrgID: call.TokenInfo.Extra["org"].(string), Metadata: map[string]any{"tenant": call.Header.Get("X-Tenant")}}
		call.TokenInfo.UserID, call.TokenInfo.Scopes[0], call.TokenInfo.Extra["org"] = "changed", "changed", "changed"
		call.Header.Set("X-Tenant", "changed")
		call.Arguments[2] = 'X'
		asked <- call
		return id, nil
	}}, request, func() (mcp.Result, error) { return &mcp.CallToolResult{}, nil })
	<-asked
	if token.UserID != "user_1" || token.Scopes[0] != "read" || token.Extra["org"] != "org_1" || header.Get("X-Tenant") != "acme" || string(request.Params.Arguments) != `{"query":"x"}` {
		t.Errorf("Identify changed the tool's own: %+v %v %s", token, header, request.Params.Arguments)
	}
	event := h.one()
	if event["userId"] != "user_1" || event["orgId"] != "org_1" || at(event, "metadata", "tenant") != "acme" {
		t.Errorf("the event is %v", event)
	}
}

type coded struct{ error }

// An error's type is recorded by its name, and one without a name not at all: what Go prints for
// it holds its fields and their tags. A JSON-RPC error wrapped in another is recorded as the
// client gets it: the whole text, and the wrapped error's code.
func TestErrorsAreRecordedByNameAndAsTheClientGetsThem(t *testing.T) {
	h := newHarness(t, nil, lsquare.Options{})
	unnamed := struct {
		error
		Canary string `json:"private-canary"`
	}{errors.New("lookup failed"), "x"}
	for _, test := range []struct {
		err      error
		category string
		message  string
		kind     any
		code     any
	}{
		{unnamed, "thrown", "lookup failed", nil, nil},
		{coded{errors.New("no rows")}, "thrown", "no rows", "lsquaremcp.coded", nil},
		{&coded{errors.New("no rows")}, "thrown", "no rows", "lsquaremcp.coded", nil},
		{fmt.Errorf("outer: %w", &jsonrpc.Error{Code: -32010, Message: "inner"}), "protocol", "outer: " + (&jsonrpc.Error{Code: -32010, Message: "inner"}).Error(), nil, float64(-32010)},
	} {
		_, _ = direct(h, nil, callRequest(`{}`), func() (mcp.Result, error) { return nil, test.err })
	}
	events := h.events()
	if len(events) != 4 {
		t.Fatalf("%d events", len(events))
	}
	seen := map[string]bool{}
	for _, event := range events {
		failure, _ := event["error"].(map[string]any)
		seen[fmt.Sprintf("%v|%v|%v|%v", failure["category"], failure["message"], failure["type"], failure["code"])] = true
		if strings.Contains(fmt.Sprint(event), "canary") {
			t.Errorf("an event holds the unnamed type's tag: %v", event)
		}
	}
	for _, want := range []string{
		"thrown|lookup failed|<nil>|<nil>", "thrown|no rows|lsquaremcp.coded|<nil>",
		"protocol|outer: " + (&jsonrpc.Error{Code: -32010, Message: "inner"}).Error() + "|<nil>|-32010",
	} {
		if !seen[want] {
			t.Errorf("no event with %q among %v", want, seen)
		}
	}
}

// A listing is what agents see: a tool it shows without a description has none from then on,
// whatever it was registered with.
func TestAListingOverrulesWhatWasRegistered(t *testing.T) {
	h := newHarness(t, nil, lsquare.Options{})
	w := newInstrumented(bridgeOf(h.client), &Options{Tools: []*mcp.Tool{searchTool, {Name: "files", Description: "Lists files."}}})
	if w.description("search_orders") == "" || w.description("files") != "Lists files." {
		t.Fatalf("before any listing: %q and %q", w.description("search_orders"), w.description("files"))
	}
	w.describe([]*mcp.Tool{{Name: "search_orders", Description: "Finds orders."}, {Name: "files"}})
	if w.description("search_orders") != "Finds orders." || w.description("files") != "" {
		t.Errorf("after the listing: %q and %q", w.description("search_orders"), w.description("files"))
	}
}

// After Shutdown the wrapper reads nothing of a call, and asks Identify nothing: the calls run
// unrecorded, which is reported once.
func TestAfterShutdownNothingIsRead(t *testing.T) {
	asked := false
	h := newHarness(t, &Options{Identify: func(context.Context, Call) (*Identity, error) { asked = true; return nil, nil }}, lsquare.Options{})
	h.client.Shutdown(context.Background())
	for i := 0; i < 3; i++ {
		if result, err := h.call("search_orders", map[string]any{"query": "x"}); err != nil || result.IsError {
			t.Fatalf("the call failed: %v", err)
		}
	}
	// The flush first: what was reported has reached OnError once it returns.
	events := h.events()
	if reported := h.reported(); asked || len(events) != 0 || len(reported) != 1 || !strings.HasPrefix(reported[0], "shutdown: ") {
		t.Errorf("Identify asked: %v; events %d; reported %v", asked, len(events), reported)
	}
}

// Identify's second is counted from when the tool returned: an answer after it doesn't count,
// even one that is there before anyone looks for it.
func TestIdentifysAnswerAfterItsDeadlineDoesntCount(t *testing.T) {
	gated := func(deadline time.Time) *running {
		return &running{identified: make(chan identified, 1), gate: &gate{deadline: deadline}, deadline: deadline}
	}
	call := gated(time.Now().Add(-time.Millisecond))
	call.gate.returned()
	call.identified <- identified{ids: nil, at: time.Now()}
	if who := call.who(); who == nil || who.why != "took longer than its deadline" {
		t.Errorf("a late answer counted: %+v", who)
	}
	call = gated(time.Now().Add(50 * time.Millisecond))
	call.identified <- identified{at: time.Now()}
	if who := call.who(); who == nil || who.why != "" {
		t.Errorf("an answer in time didn't count: %+v", who)
	}
	// Still running at its deadline: late, without waiting for it any longer.
	call = gated(time.Now().Add(10 * time.Millisecond))
	if who := call.who(); who == nil || who.why != "took longer than its deadline" {
		t.Errorf("an Identify that never answered counted: %+v", who)
	}
	// Back in time, its answer still being read as the deadline passes: it counts.
	call = gated(time.Now().Add(20 * time.Millisecond))
	at, _ := call.gate.returned()
	go func(answers chan identified) {
		time.Sleep(80 * time.Millisecond)
		answers <- identified{at: at}
	}(call.identified)
	if who := call.who(); who == nil || who.why != "" {
		t.Errorf("an answer that was back in time didn't count: %+v", who)
	}
}

// appends writes its own text, by the method Go's JSON calls from Go 1.27 on.
type appends struct{ calls *int }

func (a appends) AppendText(b []byte) ([]byte, error) {
	*a.calls++
	return append(b, "x"...), nil
}

// zeroes says whether it is zero, which a field tagged omitzero is asked.
type zeroes struct{ calls *int }

func (z zeroes) IsZero() bool {
	*z.calls++
	return false
}

type inner struct {
	Stream *once `json:"stream"`
}

// Content that writes its own JSON, by any method Go's JSON calls in one version or another, is
// never asked to: wherever it sits, the content is left out, and the reply is the first to write it.
func TestNoMethodOfTheContentsIsCalled(t *testing.T) {
	h := newHarness(t, nil, lsquare.Options{})
	calls := 0
	stream := &once{}
	for name, content := range map[string]any{
		"an embedded struct without a name of its own": struct {
			inner
			ID string `json:"id"`
		}{inner{stream}, "ord_1"},
		"a text appender": map[string]any{"a": appends{&calls}},
		"a field asked whether it is zero": struct {
			Z zeroes `json:"z,omitzero"`
		}{zeroes{&calls}},
		"a key that writes itself": map[appends]string{{&calls}: "v"},
	} {
		_, _ = direct(h, nil, callRequest(`{}`), func() (mcp.Result, error) {
			return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: name}}, StructuredContent: content}, nil
		})
	}
	if calls != 0 || stream.written {
		t.Errorf("the wrapper ran the content's own code: %d calls, written %v", calls, stream.written)
	}
	events := h.events()
	if len(events) != 4 {
		t.Fatalf("%d events", len(events))
	}
	for _, event := range events {
		if _, has := at(event, "result").(map[string]any)["structuredContent"]; has || at(event, "dropped", "result.structuredContent") != "not_json" {
			t.Errorf("%v: result %v, dropped %v", at(event, "result", "content", 0, "text"), event["result"], event["dropped"])
		}
	}
}

// A field Go's JSON never writes is never read either: the app may be changing it meanwhile.
func TestAFieldLeftOutOfTheJSONIsNotRead(t *testing.T) {
	h := newHarness(t, nil, lsquare.Options{})
	calls := 0
	content := struct {
		ID    string  `json:"id"`
		Cache appends `json:"-"`
		note  appends
	}{"ord_1", appends{&calls}, appends{&calls}}
	_, _ = direct(h, nil, callRequest(`{}`), func() (mcp.Result, error) {
		return &mcp.CallToolResult{StructuredContent: content}, nil
	})
	event := h.one()
	if structured, _ := json.Marshal(at(event, "result", "structuredContent")); string(structured) != `{"id":"ord_1"}` || calls != 0 {
		t.Errorf("structuredContent is %s, after %d calls", structured, calls)
	}
}

// counted is an error that counts how often it is asked for its text.
type counted struct{ asked *int }

func (c counted) Error() string {
	*c.asked++
	return "attempt " + fmt.Sprint(*c.asked)
}

// An error is asked for its text once, and only when the text is recorded; a JSON-RPC error is
// read by its fields, as the MCP SDK writes it.
func TestAnErrorIsAskedForItsTextOnce(t *testing.T) {
	h := newHarness(t, nil, lsquare.Options{})
	off, on := 0, 0
	_, _ = direct(h, &Options{Inputs: Off}, callRequest(`{}`), func() (mcp.Result, error) { return nil, counted{&off} })
	_, _ = direct(h, nil, callRequest(`{}`), func() (mcp.Result, error) { return nil, counted{&on} })
	_, _ = direct(h, nil, callRequest(`{}`), func() (mcp.Result, error) {
		return nil, &jsonrpc.Error{Code: 99_999_999_999_999_999, Message: "not today"}
	})
	_, _ = direct(h, nil, callRequest(`{}`), func() (mcp.Result, error) { return nil, (*jsonrpc.Error)(nil) })
	if off != 0 || on != 1 {
		t.Errorf("asked %d times with the text off, %d times with it on", off, on)
	}
	events := h.events()
	if len(events) != 4 {
		t.Fatalf("%d events", len(events))
	}
	seen := map[string]bool{}
	for _, event := range events {
		failure, _ := json.Marshal(event["error"])
		seen[string(failure)] = true
	}
	for _, want := range []string{
		`{"category":"thrown","type":"lsquaremcp.counted"}`,
		`{"category":"thrown","message":"attempt 1","type":"lsquaremcp.counted"}`,
		// A code JavaScript can't hold exactly is written as JavaScript reads it: the ingestion
		// measures the event as it writes it back.
		`{"category":"protocol","code":100000000000000000,"message":"not today"}`,
		`{"category":"thrown"}`,
	} {
		if !seen[want] {
			t.Errorf("no event with the error %s among %v", want, seen)
		}
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	for _, raw := range h.raw {
		if strings.Contains(string(raw), `"code":`) && !strings.Contains(string(raw), `"code":100000000000000000`) {
			t.Errorf("the code is sent as %s", raw)
		}
	}
}

// An error result's text is its first text block among its first 1,000, as in the other SDKs.
func TestAnErrorResultsTextIsLookedForInItsFirstBlocks(t *testing.T) {
	h := newHarness(t, nil, lsquare.Options{})
	blocks := make([]mcp.Content, 0, 1_001)
	for i := 0; i < 1_000; i++ {
		blocks = append(blocks, &mcp.ImageContent{MIMEType: "image/png"})
	}
	blocks = append(blocks, &mcp.TextContent{Text: "internal note"})
	_, _ = direct(h, nil, callRequest(`{}`), func() (mcp.Result, error) {
		return &mcp.CallToolResult{IsError: true, Content: blocks}, nil
	})
	_, _ = direct(h, nil, callRequest(`{}`), func() (mcp.Result, error) {
		return &mcp.CallToolResult{IsError: true, Content: blocks[998:]}, nil
	})
	events := h.events()
	if len(events) != 2 {
		t.Fatalf("%d events", len(events))
	}
	messages := map[any]bool{}
	for _, event := range events {
		messages[at(event, "error", "message")] = true
	}
	if !messages[nil] || !messages["internal note"] || len(messages) != 2 {
		t.Errorf("the messages are %v", messages)
	}
}

// What a call holds until its event is built counts against the queue's limits, measured before
// anything of it is kept: a result too large for the queue is refused, and the reply is untouched.
func TestAResultLargerThanTheQueueIsRefused(t *testing.T) {
	h := newHarness(t, nil, lsquare.Options{MaxQueueBytes: 4_000})
	large := map[string]any{"rows": []any{strings.Repeat("x", 3_000), map[string]string{"more": strings.Repeat("y", 3_000)}}}
	result, err := direct(h, nil, callRequest(`{}`), func() (mcp.Result, error) {
		return &mcp.CallToolResult{StructuredContent: large}, nil
	})
	if err != nil || len(result.(*mcp.CallToolResult).StructuredContent.(map[string]any)["rows"].([]any)) != 2 {
		t.Fatalf("the reply changed: %v, %v", result, err)
	}
	_, _ = direct(h, nil, callRequest(`{}`), func() (mcp.Result, error) {
		return &mcp.CallToolResult{StructuredContent: map[string]any{"rows": 2}}, nil
	})
	event := h.one()
	if at(event, "result", "structuredContent", "rows") != float64(2) {
		t.Errorf("the event is %v", event)
	}
	if reported := h.reported(); len(reported) != 1 || !strings.HasPrefix(reported[0], "queue_full: ") {
		t.Errorf("reported %v", reported)
	}
}

// A server instrumented from several goroutines at once is instrumented once.
func TestInstrumentingAtOnceRecordsEachCallOnce(t *testing.T) {
	h := newHarness(t, nil, lsquare.Options{})
	server := mcp.NewServer(&mcp.Implementation{Name: "orders", Version: "1.2.3"}, nil)
	addTools(server)
	var wg sync.WaitGroup
	for i := 0; i < 16; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			Instrument(server, h.client, nil)
		}()
	}
	wg.Wait()
	serverTransport, clientTransport := mcp.NewInMemoryTransports()
	serverSession, err := server.Connect(context.Background(), serverTransport, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer serverSession.Close()
	session, err := mcp.NewClient(&mcp.Implementation{Name: "test-agent", Version: "0.9"}, nil).Connect(context.Background(), clientTransport, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer session.Close()
	if _, err := session.CallTool(context.Background(), &mcp.CallToolParams{Name: "raw", Arguments: map[string]any{}}); err != nil {
		t.Fatal(err)
	}
	h.one()
}

// Identify is stopped a second after the tool returned, whatever else is still going on.
func TestIdentifyIsStoppedASecondAfterTheTool(t *testing.T) {
	stopped := make(chan time.Duration, 1)
	h := newHarness(t, nil, lsquare.Options{})
	var returned time.Time
	_, _ = direct(h, &Options{Identify: func(ctx context.Context, call Call) (*Identity, error) {
		<-ctx.Done()
		stopped <- time.Since(returned)
		return nil, ctx.Err()
	}}, callRequest(`{}`), func() (mcp.Result, error) {
		returned = time.Now()
		return &mcp.CallToolResult{}, nil
	})
	select {
	case after := <-stopped:
		if after < 900*time.Millisecond || after > 1500*time.Millisecond {
			t.Errorf("Identify was stopped %v after the tool returned", after)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("Identify was never stopped")
	}
	if event := h.one(); event["identity"] != "failed" {
		t.Errorf("the event is %v", event)
	}
}

type cached struct {
	Cache map[string]string
}

type based struct {
	ID string `json:"id"`
}

// A struct's fields are read as Go's JSON writes them: an embedded struct's as its own, and
// through a pointer that is nil, none. Where two fields would take one name, which of them is
// written is the encoder's to say, so nothing of the struct is read: the one left out may be a
// map the app is changing meanwhile.
func TestAStructsFieldsAreTheOnesItsJSONHas(t *testing.T) {
	h := newHarness(t, nil, lsquare.Options{})
	shadowed := struct {
		cached
		Cache int
	}{cached{map[string]string{"k": "v"}}, 7}
	promoted := struct {
		based
		*cached
		Total int `json:"total"`
	}{based{"ord_1"}, nil, 3}
	walk := measure{fields: map[reflect.Type]*written{}}
	if walk.passive(reflect.ValueOf(shadowed), 0) {
		t.Error("a struct with two fields of one name was read")
	}
	for name, content := range map[string]any{"shadowed": shadowed, "promoted": promoted} {
		_, _ = direct(h, nil, callRequest(`{}`), func() (mcp.Result, error) {
			return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: name}}, StructuredContent: content}, nil
		})
	}
	for _, event := range h.events() {
		structured, _ := json.Marshal(at(event, "result", "structuredContent"))
		want := `{"id":"ord_1","total":3}`
		if at(event, "result", "content", 0, "text") == "shadowed" {
			want = "null"
		}
		if string(structured) != want {
			t.Errorf("%v: structuredContent is %s, want %s", at(event, "result", "content", 0, "text"), structured, want)
		}
	}
}

// What is measured of plain content is never less than what writing it takes: room is taken for
// it before it is written.
func TestPlainContentIsNeverLargerThanItsMeasure(t *testing.T) {
	type row struct {
		Name  string            `json:"name"`
		Tags  []string          `json:"tags,omitempty"`
		Notes map[string]string `json:"notes"`
		Raw   json.RawMessage   `json:"raw"`
		When  time.Time         `json:"when"`
		N     json.Number       `json:"n"`
		Blob  []byte            `json:"blob"`
		Ratio float64
		Empty struct{}
		Next  *row
	}
	for name, value := range map[string]any{
		"control characters":  strings.Repeat("\x00", 1_000),
		"what HTML escapes":   strings.Repeat("<&>", 500),
		"line separators":     strings.Repeat("\u2028\u2029", 300),
		"bytes that aren't":   strings.Repeat("\xff\xfe", 400),
		"quotes":              strings.Repeat(`"\`, 700),
		"several bytes each":  strings.Repeat("界😀é", 400),
		"empty structs":       make([]struct{}, 3_000),
		"empty maps":          make([]map[string]int, 2_000),
		"numbers":             []any{-1.7976931348623157e308, int64(-9223372036854775808), uint64(18446744073709551615), float32(1e-45), true, nil},
		"keys to escape":      map[string]any{"<\x01>": 1, "\xff": map[string]string{"\u2028": "\x02"}},
		"an array of bytes":   [4]byte{255, 254, 253, 252},
		"rows":                []row{{Name: "a\tb", Tags: []string{"x"}, Notes: map[string]string{"k": "v"}, Raw: json.RawMessage(`{"a": 1}`), When: time.Now(), N: "12.5", Blob: make([]byte, 1_001), Next: &row{}}},
		"nothing":             nil,
		"a pointer to a text": new(string),
	} {
		walk := measure{fields: map[reflect.Type]*written{}}
		if !walk.passive(reflect.ValueOf(value), 0) {
			t.Errorf("%s: not plain", name)
			continue
		}
		written, err := json.Marshal(value)
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		if int64(len(written)) > walk.size {
			t.Errorf("%s: measured %d bytes, written in %d", name, walk.size, len(written))
		}
	}
}

// The arguments recorded are the ones the client sent, though the tool rewrites its own in place.
func TestTheArgumentsRecordedAreTheOnesSent(t *testing.T) {
	h := newHarness(t, nil, lsquare.Options{})
	request := callRequest(`{"value":"old"}`)
	_, _ = direct(h, nil, request, func() (mcp.Result, error) {
		copy(request.Params.Arguments, `{"value":"new"}`)
		return &mcp.CallToolResult{}, nil
	})
	if event := h.one(); at(event, "input", "value") != "old" {
		t.Errorf("the input is %v", event["input"])
	}
}

// The calling app is read from the request's metadata as it is there: a value of the app's own
// isn't written as JSON to be read, which would be the app's code to run.
func TestTheCallingAppIsReadWithoutWritingIt(t *testing.T) {
	h := newHarness(t, nil, lsquare.Options{})
	stream := &once{}
	for _, info := range []any{stream, map[string]any{"name": "an-agent", "version": "2.0"}, &mcp.Implementation{Name: "typed-agent"}} {
		request := callRequest(`{}`)
		request.Params.Meta = mcp.Meta{mcp.MetaKeyClientInfo: info}
		_, _ = direct(h, nil, request, func() (mcp.Result, error) { return &mcp.CallToolResult{}, nil })
	}
	if stream.written {
		t.Error("the wrapper wrote the metadata's own JSON")
	}
	clients := map[string]bool{}
	for _, event := range h.events() {
		client, _ := json.Marshal(event["client"])
		clients[string(client)] = true
	}
	for _, want := range []string{"null", `{"name":"an-agent","version":"2.0"}`, `{"name":"typed-agent"}`} {
		if !clients[want] {
			t.Errorf("no event with the client %s among %v", want, clients)
		}
	}
}

// An error result's text counts against the queue's limits wherever it is among the blocks.
func TestAnErrorResultsTextCountsWhereverItIs(t *testing.T) {
	h := newHarness(t, nil, lsquare.Options{MaxQueueBytes: 4_000})
	blocks := make([]mcp.Content, 0, 151)
	for i := 0; i < 150; i++ {
		blocks = append(blocks, &mcp.ImageContent{})
	}
	blocks = append(blocks, &mcp.TextContent{Text: strings.Repeat("x", 6_000)})
	_, _ = direct(h, nil, callRequest(`{}`), func() (mcp.Result, error) {
		return &mcp.CallToolResult{IsError: true, Content: blocks}, nil
	})
	if events := h.events(); len(events) != 0 {
		t.Errorf("%d events", len(events))
	}
	if reported := h.reported(); len(reported) != 1 || !strings.HasPrefix(reported[0], "queue_full: ") {
		t.Errorf("reported %v", reported)
	}
}

// Only struct tags every Go version reads the same way are read. With any other (a name in
// quotes, the string option, an option a later encoding/json knows), which fields are written
// is the encoder's to say, so nothing of the struct is read.
func TestAStructWithATagOfItsOwnKindIsLeftAlone(t *testing.T) {
	hidden := cached{map[string]string{"k": strings.Repeat("z", 100)}}
	type embeddedResult struct {
		Total int
		Cache map[string]string
	}
	for name, value := range map[string]any{
		"a name in quotes": struct {
			cached
			Cache int `json:"'Cache'"`
		}{hidden, 7},
		"a name that isn't plain": struct {
			cached
			Cache int `json:"🌐"`
		}{hidden, 7},
		"an option of a later encoder": struct {
			Base  embeddedResult `json:",embed"`
			Cache int
		}{embeddedResult{5, hidden.Cache}, 7},
		"the string option": struct {
			Value string `json:"v,string"`
		}{"x"},
	} {
		walk := measure{fields: map[reflect.Type]*written{}}
		if walk.passive(reflect.ValueOf(value), 0) {
			t.Errorf("%s: read", name)
		}
	}
	walk := measure{fields: map[reflect.Type]*written{}}
	plain := struct {
		ID    string `json:"order.id,omitempty"`
		Total int    `json:",omitzero"`
		Skip  cached `json:"-"`
	}{"ord_1", 3, hidden}
	if !walk.passive(reflect.ValueOf(plain), 0) {
		t.Error("a struct with plain tags wasn't read")
	}
}

// JSON held inside plain content, and bytes, are measured as they are written: escaped again,
// and in base64. Ordinary rows aren't measured at several times their size.
func TestWhatIsWrittenInsidePlainContentIsMeasured(t *testing.T) {
	type row struct {
		ID     int    `json:"id"`
		Title  string `json:"title"`
		Active bool   `json:"active"`
	}
	rows := make([]row, 1_800)
	for i := range rows {
		rows[i] = row{i, fmt.Sprintf("Title %d", i), true}
	}
	html := json.RawMessage(`"` + strings.Repeat("<b>invoice</b> & \u2028", 256) + `"`)
	for name, value := range map[string]any{
		"JSON that HTML escaping lengthens": map[string]json.RawMessage{"html": html},
		"JSON that is none":                 make([]json.RawMessage, 100),
		"bytes":                             struct{ Data []byte }{make([]byte, 300_001)},
		"rows":                              rows,
		"a table of tabs and line ends":     map[string]string{"table": strings.Repeat("a\tb\tc\n", 50_000)},
		"an optional time":                  struct{ When, Never *time.Time }{When: new(time.Time)},
	} {
		walk := measure{fields: map[reflect.Type]*written{}}
		if !walk.passive(reflect.ValueOf(value), 0) {
			t.Errorf("%s: not plain", name)
			continue
		}
		written, err := json.Marshal(value)
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		if size := int64(len(written)); size > walk.size || walk.size > 2*size+64 {
			t.Errorf("%s: measured %d bytes, written in %d", name, walk.size, size)
		}
	}
}

// An error result's text that isn't valid UTF-8 is held twice once made valid, as the message
// and as its block: room is taken for both.
func TestAnErrorTextMadeValidIsCountedTwice(t *testing.T) {
	text := strings.Repeat("\xff", 2_048)
	result := &mcp.CallToolResult{IsError: true, Content: []mcp.Content{&mcp.TextContent{Text: text}}}
	h := newHarness(t, nil, lsquare.Options{})
	how := newInstrumented(bridgeOf(h.client), nil).outcomeOf(&running{inputs: true, results: true}, result, nil, false)
	if held := int64(how.text) + planResult(result).size; held < 2*3*2_048 {
		t.Errorf("room for %d bytes, for two texts of %d", held, 3*2_048)
	}
}

// The calling app is the one the MCP SDK says it is, also where the request's metadata is odd.
func TestTheCallingAppIsTheOneTheSDKReads(t *testing.T) {
	h := newHarnessOn(t, nil, lsquare.Options{}, &mcp.ClientSessionOptions{ProtocolVersion: "2025-06-18"})
	var session *mcp.ServerSession
	for s := range h.server.Sessions() {
		session = s
		break
	}
	if session == nil {
		t.Fatal("no session")
	}
	for _, info := range []any{nil, map[string]any{}, map[string]any{"name": "agent"}, map[string]any{"name": 5}, map[string]any{"name": "agent", "version": 2}, &mcp.Implementation{Name: "typed", Version: "1"},
		map[string]any{"name": "agent", "title": 5}, map[string]any{"name": "agent", "icons": "x"}, map[string]any{"name": "agent", "title": "An agent", "icons": []any{}}, "bare", 7.0, []any{"x"},
		map[string]any{"name": "agent", "icons": []any{"x"}}, map[string]any{"name": "agent", "icons": []any{map[string]any{"src": 5}}},
		map[string]any{"name": "agent", "icons": []any{map[string]any{"src": "https://example.com/i.png", "sizes": []any{1.0}}}},
		map[string]any{"name": "agent", "icons": []any{map[string]any{"src": "https://example.com/i.png", "sizes": []any{"48x48"}, "theme": "dark"}}}} {
		request := callRequest(`{}`)
		request.Session = session
		if info != nil {
			request.Params.Meta = mcp.Meta{mcp.MetaKeyClientInfo: info}
		}
		want, _ := json.Marshal(named(request.ClientInfo()))
		got, _ := json.Marshal(clientOf(request))
		if string(got) != string(want) {
			t.Errorf("%v: the wrapper reads %s, the SDK %s", info, got, want)
		}
	}
}

// A text with a byte that isn't UTF-8 is measured as what it becomes, not as three times itself.
func TestATextIsMeasuredAsWhatItBecomes(t *testing.T) {
	for text, want := range map[string]int{
		"plain":                                 5,
		"a\xffb":                                5,
		strings.Repeat("x", 1_000) + "\xff\xfe": 1_006,
		"\xe7\x95":                              6,
		"界":                                     3,
	} {
		if got := textSize(text); got != want || got != len(jsonv.ValidString(text)) {
			t.Errorf("%q is measured %d, want %d", text, got, want)
		}
	}
}

// Of a result's blocks past the hundredth, only that there are some is noted: nothing of them
// is measured, or kept.
func TestBlocksPastTheHundredthAreOnlyCounted(t *testing.T) {
	h := newHarness(t, nil, lsquare.Options{MaxQueueBytes: 128 << 10})
	blocks := make([]mcp.Content, 0, 102)
	for i := 0; i < 100; i++ {
		blocks = append(blocks, &mcp.TextContent{Text: fmt.Sprintf("hit %d", i)})
	}
	blocks = append(blocks, &mcp.TextContent{Text: strings.Repeat("appendix ", 16_000)}, &mcp.TextContent{Text: "more"})
	result := &mcp.CallToolResult{Content: blocks}
	if plan := planResult(result); plan.size > 2_000 {
		t.Errorf("the blocks are measured %d bytes", plan.size)
	}
	_, _ = direct(h, nil, callRequest(`{}`), func() (mcp.Result, error) { return result, nil })
	event := h.one()
	content, _ := at(event, "result", "content").([]any)
	if len(content) != 100 || at(event, "result", "content", 99, "text") != "hit 99" || fmt.Sprint(event["truncated"]) != "[result]" {
		t.Errorf("%d blocks, truncated %v, reported %v", len(content), event["truncated"], h.reported())
	}
}

// The tools the options named are read for their descriptions, and let go.
func TestTheOptionsToolsAreNotKept(t *testing.T) {
	h := newHarness(t, nil, lsquare.Options{})
	w := newInstrumented(bridgeOf(h.client), &Options{Tools: []*mcp.Tool{searchTool}, ServerInfo: &mcp.Implementation{Name: "orders"}})
	if w.options.Tools != nil || w.options.ServerInfo != nil || w.description("search_orders") == "" || w.server == nil {
		t.Errorf("tools %v, server info %v, description %q", w.options.Tools, w.options.ServerInfo, w.description("search_orders"))
	}
}

// instructed is a server made with instructions, instrumented with the harness's client, and a
// client connected to it on the given protocol version ("" for the Go client's own).
func instructed(t *testing.T, h *harness, options *Options, protocol string) *mcp.ClientSession {
	t.Helper()
	server := mcp.NewServer(&mcp.Implementation{Name: "orders", Version: "1.2.3"}, &mcp.ServerOptions{Instructions: "Search before you refund. Ask ops@example.com when unsure."})
	addTools(server)
	Instrument(server, h.client, options)
	serverTransport, clientTransport := mcp.NewInMemoryTransports()
	serverSession, err := server.Connect(context.Background(), serverTransport, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = serverSession.Close() })
	var sessionOptions *mcp.ClientSessionOptions
	if protocol != "" {
		sessionOptions = &mcp.ClientSessionOptions{ProtocolVersion: protocol}
	}
	session, err := mcp.NewClient(&mcp.Implementation{Name: "test-agent", Version: "0.9"}, nil).Connect(context.Background(), clientTransport, sessionOptions)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = session.Close() })
	return session
}

// A call carries the server's instructions as the server gives them to clients, redacted like
// any text: learned from its answer to initialize, or to server/discover, which a client on the
// newer protocol asks in its place.
func TestACallCarriesTheServersInstructions(t *testing.T) {
	for name, protocol := range map[string]string{"initialize": "2025-06-18", "server/discover": ""} {
		t.Run(name, func(t *testing.T) {
			h := newHarness(t, nil, lsquare.Options{})
			session := instructed(t, h, nil, protocol)
			if _, err := session.CallTool(context.Background(), &mcp.CallToolParams{Name: "raw", Arguments: map[string]any{}}); err != nil {
				t.Fatal(err)
			}
			event := h.one()
			if event["serverInstructions"] != "Search before you refund. Ask [EMAIL_1] when unsure." {
				t.Errorf("serverInstructions is %v", event["serverInstructions"])
			}
		})
	}
}

// On a connection that was given no instructions, a call's are the ones the options gave, or
// without them the ones the server last gave anyone; Off records none.
func TestInstructionsFromTheOptionsAndTheSwitch(t *testing.T) {
	h := newHarness(t, nil, lsquare.Options{})
	run := func(options *Options, before func(w *instrumented)) {
		w := newInstrumented(bridgeOf(h.client), options)
		if before != nil {
			before(w)
		}
		_, _ = w.middleware(func(context.Context, string, mcp.Request) (mcp.Result, error) {
			return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: "ok"}}}, nil
		})(context.Background(), "tools/call", callRequest(`{}`))
	}
	discover := func(instructions string) func(*instrumented) {
		return func(w *instrumented) {
			_, _ = w.middleware(func(context.Context, string, mcp.Request) (mcp.Result, error) {
				return &mcp.DiscoverResult{Instructions: instructions}, nil
			})(context.Background(), "server/discover", &mcp.ServerRequest[*mcp.DiscoverParams]{})
		}
	}
	run(nil, nil)
	run(nil, discover("as answered"))
	run(nil, discover(""))
	run(&Options{ServerInstructions: "as made"}, nil)
	run(&Options{ServerInstructions: "as made"}, discover("as answered"))
	run(&Options{ServerInstructions: "as made"}, discover(""))
	run(&Options{ServerInstructions: "as made", Instructions: Off}, nil)
	seen := map[string]int{}
	for _, event := range h.events() {
		seen[fmt.Sprint(event["serverInstructions"])]++
	}
	if seen["<nil>"] != 3 || seen["as made"] != 3 || seen["as answered"] != 1 || len(seen) != 3 {
		t.Errorf("the instructions recorded are %v", seen)
	}
}

// A call sent as limited diagnostics has no instructions, as it has no description.
func TestLimitedDiagnosticsHaveNoInstructions(t *testing.T) {
	h := newHarness(t, nil, lsquare.Options{})
	_, _ = direct(h, &Options{ServerInstructions: "as made", Tools: []*mcp.Tool{searchTool},
		Identify: func(context.Context, Call) (*Identity, error) { return nil, errors.New("no") }}, callRequest(`{}`),
		func() (mcp.Result, error) { return &mcp.CallToolResult{}, nil })
	event := h.one()
	if _, has := event["serverInstructions"]; has || event["identity"] != "failed" {
		t.Errorf("the event is %v", event)
	}
}

// BeforeSend gets the instructions, and what it makes of them is redacted and cut like the rest.
func TestBeforeSendGetsTheInstructions(t *testing.T) {
	var got string
	h := newHarness(t, nil, lsquare.Options{BeforeSend: func(ctx context.Context, event *lsquare.Event) (*lsquare.Event, error) {
		got = event.ServerInstructions
		event.ServerInstructions = "Ask zoe@example.com. " + strings.Repeat("x", 2_100)
		return event, nil
	}})
	_, _ = direct(h, &Options{ServerInstructions: "as made"}, callRequest(`{}`), func() (mcp.Result, error) { return &mcp.CallToolResult{}, nil })
	event := h.one()
	text, _ := event["serverInstructions"].(string)
	if got != "as made" || !strings.HasPrefix(text, "Ask [EMAIL_1]. xxx") || !strings.HasSuffix(text, "chars]") || fmt.Sprint(event["truncated"]) != "[serverInstructions]" {
		t.Errorf("the hook got %q; the event has %.40q… (truncated %v)", got, text, event["truncated"])
	}
}

// The instructions a call carries are the ones its own connection was given, where clients are
// given different ones: another connection's answer changes nothing for it.
func TestEachConnectionHasTheInstructionsItWasGiven(t *testing.T) {
	h := newHarness(t, nil, lsquare.Options{})
	server := mcp.NewServer(&mcp.Implementation{Name: "orders", Version: "1.2.3"}, &mcp.ServerOptions{Instructions: "For everyone."})
	addTools(server)
	// Added before Instrument, so it runs inside it: the wrapper sees the reply as the client gets it.
	server.AddReceivingMiddleware(func(next mcp.MethodHandler) mcp.MethodHandler {
		return func(ctx context.Context, method string, request mcp.Request) (mcp.Result, error) {
			result, err := next(ctx, method, request)
			if answer, ok := result.(*mcp.InitializeResult); ok && err == nil {
				if params, ok := request.GetParams().(*mcp.InitializeParams); ok && params.ClientInfo != nil {
					answer.Instructions = "Instructions for " + params.ClientInfo.Name + "."
				}
			}
			return result, err
		}
	})
	Instrument(server, h.client, nil)
	connect := func(name string) *mcp.ClientSession {
		serverTransport, clientTransport := mcp.NewInMemoryTransports()
		serverSession, err := server.Connect(context.Background(), serverTransport, nil)
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = serverSession.Close() })
		session, err := mcp.NewClient(&mcp.Implementation{Name: name, Version: "1"}, nil).Connect(context.Background(), clientTransport, &mcp.ClientSessionOptions{ProtocolVersion: "2025-06-18"})
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = session.Close() })
		return session
	}
	alpha, beta := connect("alpha"), connect("beta")
	for _, session := range []*mcp.ClientSession{alpha, beta, alpha} {
		if _, err := session.CallTool(context.Background(), &mcp.CallToolParams{Name: "raw", Arguments: map[string]any{}}); err != nil {
			t.Fatal(err)
		}
	}
	events := h.events()
	if len(events) != 3 {
		t.Fatalf("%d events", len(events))
	}
	for _, event := range events {
		if want := "Instructions for " + fmt.Sprint(at(event, "client", "name")) + "."; event["serverInstructions"] != want {
			t.Errorf("a call of %v carries %q", at(event, "client", "name"), event["serverInstructions"])
		}
	}
}

// JSON a result holds already is recorded the same by its value and by a pointer to it, at the
// size it has; with none there, the client is sent null, and so is the event.
func TestJSONAlreadyWrittenIsTheSameByValueAndByPointer(t *testing.T) {
	raw := json.RawMessage(`{"html":"` + strings.Repeat("<b>invoice</b> & ", 2_000) + `"}`)
	var none json.RawMessage
	for _, content := range []any{raw, &raw} {
		if plan := planResult(&mcp.CallToolResult{StructuredContent: content}); plan.size != int64(len(raw)) {
			t.Errorf("%T: measured %d bytes, for %d", content, plan.size, len(raw))
		}
	}
	h := newHarness(t, nil, lsquare.Options{MaxQueueBytes: 128 << 10})
	for _, content := range []any{raw, &raw, none, &none, (*json.RawMessage)(nil)} {
		_, _ = direct(h, nil, callRequest(`{}`), func() (mcp.Result, error) {
			return &mcp.CallToolResult{StructuredContent: content}, nil
		})
	}
	events := h.events()
	if len(events) != 5 || len(h.reported()) != 0 {
		t.Fatalf("%d events, reported %v", len(events), h.reported())
	}
	html, null := 0, 0
	for _, event := range events {
		result, _ := event["result"].(map[string]any)
		structured, has := result["structuredContent"]
		switch {
		case has && structured == nil:
			null++
		case at(event, "result", "structuredContent", "html") != nil:
			html++
		}
	}
	if html != 2 || null != 3 {
		t.Errorf("%d events with the content, %d with null", html, null)
	}
}

// Identify is judged in time or late once, by the same rule for the event and for what the
// client learns from it: in time while the tool runs, then before its deadline, never at it, and
// never once the event has given up on it, whenever its goroutine got round to saying so.
func TestIdentifysGateDecidesOnce(t *testing.T) {
	closed := func(g *gate, deadline time.Time) *gate {
		g.mu.Lock()
		g.closeLocked(deadline)
		g.mu.Unlock()
		return g
	}
	if _, inTime := (&gate{}).returned(); !inTime {
		t.Error("an answer while the tool ran was late")
	}
	// Its deadline passes first: the answer that comes after is late.
	g := closed(&gate{}, time.Now().Add(time.Hour))
	if !g.expire() {
		t.Error("the deadline passed without an answer, and the event kept waiting")
	}
	if _, inTime := g.returned(); inTime {
		t.Error("an answer after the event gave up on it counted")
	}
	// It answers first: the deadline passing finds it in time.
	g = closed(&gate{}, time.Now().Add(time.Hour))
	if _, inTime := g.returned(); !inTime || g.expire() {
		t.Error("an answer before the deadline was late")
	}
	// At its deadline: late.
	g = closed(&gate{}, time.Now())
	if _, inTime := g.returned(); inTime {
		t.Error("an answer at its deadline was in time")
	}
}

// What a failed call needs of its capture is fixed once the tool has returned and Identify has
// failed: a later answer, of another call, applies to later calls only.
func TestAFailedCallsNeedsAreFixedWhenItIsDecided(t *testing.T) {
	history := []string{"orgId", "userId"}
	keys := func() []string { return history }
	// Failed while the tool ran: fixed as it returns, by what was known then.
	g := &gate{keys: keys}
	g.fail()
	history = []string{}
	g.mu.Lock()
	g.closeLocked(time.Now().Add(time.Hour))
	g.mu.Unlock()
	history = []string{"orgId", "userId", "conversationId"}
	if got := g.requiredKeys(); len(got) != 0 {
		t.Errorf("failed while the tool ran: needs %v", got)
	}
	// Failed after it returned: fixed then.
	history = []string{"orgId", "userId"}
	g = &gate{keys: keys}
	g.mu.Lock()
	g.closeLocked(time.Now().Add(time.Hour))
	g.mu.Unlock()
	history = []string{"userId"}
	g.fail()
	history = []string{}
	if got := g.requiredKeys(); len(got) != 1 || got[0] != "userId" {
		t.Errorf("failed after the return: needs %v", got)
	}
	// Late: fixed as its deadline passes.
	g = &gate{keys: keys}
	g.mu.Lock()
	g.closeLocked(time.Now().Add(time.Hour))
	g.mu.Unlock()
	history = []string{"orgId"}
	g.expire()
	history = nil
	if got := g.requiredKeys(); len(got) != 1 || got[0] != "orgId" {
		t.Errorf("late: needs %v", got)
	}
}
