package lsquaremcp

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"regexp"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/auth"
	"github.com/modelcontextprotocol/go-sdk/jsonrpc"
	"github.com/modelcontextprotocol/go-sdk/mcp"

	lsquare "github.com/theagentexperiencecompany/lsquare/packages/go"
)

const (
	legacy = "2025-06-18"
	modern = "2026-07-28"
)

type weatherOutput struct {
	Forecast string `json:"forecast"`
}

type ownerInput struct {
	SessionID string `json:"session_id" jsonschema:"the checkout session"`
	Item      string `json:"item"`
}

// seen is what the tools' own code was given.
type seen struct {
	mu        sync.Mutex
	arguments []string
	inputs    []any
}

func (s *seen) note(request *mcp.CallToolRequest, input any) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.arguments = append(s.arguments, string(request.Params.Arguments))
	s.inputs = append(s.inputs, input)
}

func (s *seen) last() (string, any) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.arguments) == 0 {
		return "", nil
	}
	return s.arguments[len(s.arguments)-1], s.inputs[len(s.inputs)-1]
}

// shared is a result one tool returns every time, with room at the end of its content.
var shared = func() *mcp.CallToolResult {
	content := make([]mcp.Content, 1, 8)
	content[0] = &mcp.TextContent{Text: "always the same"}
	return &mcp.CallToolResult{Content: content}
}()

// addShop adds a server's tools, as any app adds them: with the MCP SDK.
func addShop(server *mcp.Server, saw *seen) {
	mcp.AddTool(server, &mcp.Tool{Name: "weather", Description: "The weather in a city."}, func(ctx context.Context, request *mcp.CallToolRequest, input weatherInput) (*mcp.CallToolResult, weatherOutput, error) {
		saw.note(request, input)
		switch input.City {
		case "nowhere":
			return nil, weatherOutput{}, errors.New("no such city")
		case "protocol":
			return nil, weatherOutput{}, &jsonrpc.Error{Code: -32000, Message: "not today"}
		case "silent":
			return &mcp.CallToolResult{}, weatherOutput{}, nil
		}
		return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: "sunny in " + input.City}}}, weatherOutput{Forecast: "sunny"}, nil
	})
	// A tool with a session_id of its own.
	mcp.AddTool(server, &mcp.Tool{Name: "checkout"}, func(ctx context.Context, request *mcp.CallToolRequest, input ownerInput) (*mcp.CallToolResult, any, error) {
		saw.note(request, input)
		return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: "paid in " + input.SessionID}}}, nil, nil
	})
	// A tool that takes any key.
	mcp.AddTool(server, &mcp.Tool{Name: "anything"}, func(ctx context.Context, request *mcp.CallToolRequest, input map[string]any) (*mcp.CallToolResult, any, error) {
		saw.note(request, input)
		return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: "ok"}}}, nil, nil
	})
	mcp.AddTool(server, &mcp.Tool{Name: "same"}, func(ctx context.Context, request *mcp.CallToolRequest, input struct{}) (*mcp.CallToolResult, any, error) {
		return shared, nil, nil
	})
	// Two tools whose handlers read the arguments themselves, with a schema written by hand: one
	// that refuses the inputs it doesn't name, and one that doesn't say.
	echo := func(ctx context.Context, request *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		saw.note(request, nil)
		return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: string(request.Params.Arguments)}}}, nil
	}
	server.AddTool(&mcp.Tool{Name: "closed", InputSchema: json.RawMessage(`{"type":"object","properties":{"q":{"type":"string"}},"additionalProperties":false}`)}, echo)
	server.AddTool(&mcp.Tool{Name: "open", InputSchema: json.RawMessage(`{"type":"object","properties":{"q":{"type":"string"}}}`)}, echo)
}

type warnings struct {
	mu    sync.Mutex
	lines []string
}

func (w *warnings) Info(message string) {}
func (w *warnings) Warn(message string) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.lines = append(w.lines, message)
}
func (w *warnings) all() string {
	w.mu.Lock()
	defer w.mu.Unlock()
	return strings.Join(w.lines, "\n")
}

// shop is a server with its tools, instrumented, a client on the given protocol version, and
// what the tools' own code saw.
func shop(t *testing.T, protocol string, options *Options) (*harness, *seen, *warnings) {
	t.Helper()
	saw, logged := &seen{}, &warnings{}
	h := newHarnessWith(t, lsquare.Options{Logger: logged}, &mcp.ClientSessionOptions{ProtocolVersion: protocol}, func(h *harness) {
		addShop(h.server, saw)
		Instrument(h.server, h.client, options)
	})
	return h, saw, logged
}

// unwrapped is the same server without the wrapper.
func unwrapped(t *testing.T, protocol string, saw *seen) *harness {
	t.Helper()
	return newHarnessWith(t, lsquare.Options{}, &mcp.ClientSessionOptions{ProtocolVersion: protocol}, func(h *harness) {
		addShop(h.server, saw)
	})
}

func raw(arguments string) json.RawMessage { return json.RawMessage(arguments) }

// latest is the event of the call just made: each is sent before the next call is made, so
// events arrive in the order of their calls.
func (h *harness) latest() map[string]any {
	h.t.Helper()
	events := h.events()
	if len(events) == 0 {
		h.t.Fatal("no event")
	}
	return events[len(events)-1]
}

func asJSON(value any) string {
	out, _ := json.Marshal(value)
	return string(out)
}

func texts(result *mcp.CallToolResult) []string {
	out := []string{}
	for _, block := range result.Content {
		if text, ok := block.(*mcp.TextContent); ok {
			out = append(out, text.Text)
		}
	}
	return out
}

var taskLine = regexp.MustCompile(`^Task ID: (ses_[A-Za-z0-9_-]{22})\. Send it as session_id on later calls for this task, and pass it to any subagent working on it\.$`)

// issued is the session ID a result's last block gives, or "".
func issued(result *mcp.CallToolResult) string {
	blocks := texts(result)
	if len(blocks) == 0 {
		return ""
	}
	if found := taskLine.FindStringSubmatch(blocks[len(blocks)-1]); found != nil {
		return found[1]
	}
	return ""
}

func listed(t *testing.T, h *harness) map[string]string {
	t.Helper()
	tools, err := h.session.ListTools(context.Background(), nil)
	if err != nil {
		t.Fatal(err)
	}
	out := map[string]string{}
	for _, tool := range tools.Tools {
		schema, _ := json.Marshal(tool.InputSchema)
		out[tool.Name] = string(schema)
	}
	return out
}

func TestToolsAreListedWithTheInputs(t *testing.T) {
	h, _, logged := shop(t, modern, nil)
	tools := listed(t, h)
	var weather struct {
		Properties map[string]struct{ Type, Description string } `json:"properties"`
		Required   []string                                      `json:"required"`
		Additional *bool                                         `json:"additionalProperties"`
	}
	if err := json.Unmarshal([]byte(tools["weather"]), &weather); err != nil {
		t.Fatal(err)
	}
	if len(weather.Properties) != 4 || weather.Properties["session_id"].Description != sessionDescription || weather.Properties["intent"].Description != intentDescription ||
		weather.Properties["session_id"].Type != "string" || weather.Properties["city"].Description != "the city" || len(weather.Required) != 1 || weather.Additional == nil || *weather.Additional {
		t.Errorf("weather as listed: %s", tools["weather"])
	}
	// A tool with a session_id of its own keeps it, and gets the other input.
	if !strings.Contains(tools["checkout"], `"the checkout session"`) || strings.Contains(tools["checkout"], sessionDescription) || !strings.Contains(tools["checkout"], intentDescription) {
		t.Errorf("checkout as listed: %s", tools["checkout"])
	}
	// A schema written by hand that refuses what it doesn't name is as good as an inferred one.
	if !strings.Contains(tools["closed"], sessionDescription) || !strings.Contains(tools["closed"], intentDescription) {
		t.Errorf("closed as listed: %s", tools["closed"])
	}
	// A tool that takes any key, and one whose schema doesn't say, are as they were.
	for _, name := range []string{"anything", "open"} {
		if strings.Contains(tools[name], "session_id") || strings.Contains(tools[name], "intent") {
			t.Errorf("%s as listed: %s", name, tools[name])
		}
	}
	if !strings.Contains(tools["get_more_tools"], needDescription) || !strings.Contains(tools["get_more_tools"], sessionDescription) || strings.Contains(tools["get_more_tools"], intentDescription) {
		t.Errorf("get_more_tools as listed: %s", tools["get_more_tools"])
	}
	if len(tools) != 7 {
		t.Errorf("%d tools listed: %v", len(tools), tools)
	}
	// Listed again, the same: get_more_tools is the server's own tool by now, and listed once.
	if again := listed(t, h); len(again) != 7 || again["weather"] != tools["weather"] || again["get_more_tools"] != tools["get_more_tools"] {
		t.Errorf("listed again: %v", again)
	}
	said := logged.all()
	for _, want := range []string{
		`[lsquare] Tool "checkout" has its own session_id input: it is left as it is.`,
		`[lsquare] Tool "open" gets no session_id or intent: its input schema can't be read safely (it doesn't say "additionalProperties": false), so any input could be its own.`,
		`[lsquare] Tool "anything" gets no session_id or intent: its input schema can't be read safely (` + unreadableListing + `), so any input could be its own.`,
	} {
		if !strings.Contains(said, want) {
			t.Errorf("no warning %q in:\n%s", want, said)
		}
	}
	if strings.Contains(said, `"weather"`) || strings.Contains(said, `"same"`) || strings.Count(said, `"open"`) != 1 {
		t.Errorf("warnings:\n%s", said)
	}
}

type orderedInput struct {
	Zebra  string `json:"zebra"`
	Apple  string `json:"apple,omitempty"`
	Nested struct {
		Yak string `json:"yak"`
		Bee string `json:"bee"`
	} `json:"nested"`
}

// The server's own tools are never changed: what a client is listed is a copy, the same as the
// server's but for two properties more at the end of its own.
func TestTheListingIsACopyWithTheInputsAfterTheToolsOwn(t *testing.T) {
	var inner []string
	var registered []*mcp.Tool
	h := newHarnessWith(t, lsquare.Options{}, nil, func(h *harness) {
		// Inside the wrapper's: it sees the server's own answer.
		h.server.AddReceivingMiddleware(func(next mcp.MethodHandler) mcp.MethodHandler {
			return func(ctx context.Context, method string, request mcp.Request) (mcp.Result, error) {
				result, err := next(ctx, method, request)
				if page, ok := result.(*mcp.ListToolsResult); ok {
					for _, tool := range page.Tools {
						if tool.Name == "ordered" {
							inner = append(inner, asJSON(tool.InputSchema))
							registered = append(registered, tool)
						}
					}
				}
				return result, err
			}
		})
		mcp.AddTool(h.server, &mcp.Tool{Name: "ordered"}, func(ctx context.Context, request *mcp.CallToolRequest, input orderedInput) (*mcp.CallToolResult, any, error) {
			return nil, nil, nil
		})
		Instrument(h.server, h.client, nil)
	})
	first, second := listed(t, h)["ordered"], listed(t, h)["ordered"]
	if len(inner) != 2 || inner[0] != inner[1] || registered[0] != registered[1] || strings.Contains(inner[1], "session_id") {
		t.Fatalf("the server's own tool changed: %v", inner)
	}
	plain := inner[0]
	if !strings.Contains(plain, `"zebra":{"type":"string"},"apple":{"type":"string"},"nested":{"type":"object","properties":{"yak":{"type":"string"},"bee":{"type":"string"}}`) {
		t.Fatalf("the SDK's own listing: %s", plain)
	}
	// A client decodes the schema into a map, which has no order: compared as the server wrote it.
	var wrote string
	h2 := newHarnessWith(t, lsquare.Options{}, nil, func(h *harness) {
		mcp.AddTool(h.server, &mcp.Tool{Name: "ordered"}, func(ctx context.Context, request *mcp.CallToolRequest, input orderedInput) (*mcp.CallToolResult, any, error) {
			return nil, nil, nil
		})
		Instrument(h.server, h.client, nil)
		// Outside the wrapper's: it sees what clients are sent.
		h.server.AddReceivingMiddleware(func(next mcp.MethodHandler) mcp.MethodHandler {
			return func(ctx context.Context, method string, request mcp.Request) (mcp.Result, error) {
				result, err := next(ctx, method, request)
				if page, ok := result.(*mcp.ListToolsResult); ok {
					for _, tool := range page.Tools {
						if tool.Name == "ordered" {
							wrote = asJSON(tool.InputSchema)
						}
					}
				}
				return result, err
			}
		})
	})
	listed(t, h2)
	last := strings.LastIndex(plain, `},"required"`)
	want := plain[:last] + `,"session_id":{"type":"string","description":` + quoted(sessionDescription) + `},"intent":{"type":"string","description":` + quoted(intentDescription) + `}` + plain[last:]
	if wrote != want {
		t.Errorf("listed:\n%s\nwant:\n%s", wrote, want)
	}
	if first != second || !strings.Contains(first, "session_id") {
		t.Errorf("listed twice: %s / %s", first, second)
	}
}

func TestTheInputsNeverReachTheTool(t *testing.T) {
	sent := "ses_" + strings.Repeat("s", 22)
	h, saw, _ := shop(t, modern, nil)
	result, err := h.call("weather", raw(`{"city":"Rome","session_id":"`+sent+`","intent":"plan a trip for jane@example.com"}`))
	if err != nil || result.IsError {
		t.Fatalf("%v %v", err, texts(result))
	}
	arguments, input := saw.last()
	if arguments != `{"city":"Rome"}` || input != (weatherInput{City: "Rome"}) {
		t.Errorf("the tool saw %s and %+v", arguments, input)
	}
	// The agent sent the ID, so the result is the tool's, and nothing more.
	if got := texts(result); len(got) != 1 || got[0] != "sunny in Rome" {
		t.Errorf("the result: %v", got)
	}
	event := h.one()
	if event["sessionId"] != sent || event["sessionSource"] != "sent" || event["intent"] != "plan a trip for [EMAIL_1]" {
		t.Errorf("session %v (%v), intent %v", event["sessionId"], event["sessionSource"], event["intent"])
	}
	if got := asJSON(event["input"]); got != `{"city":"Rome"}` {
		t.Errorf("recorded input: %s", got)
	}
	// The tool's description, from the listing the wrapper read to know the tool.
	if event["toolDescription"] != "The weather in a city." {
		t.Errorf("description: %v", event["toolDescription"])
	}
}

func TestACallWithoutASessionIsGivenOne(t *testing.T) {
	h, saw, _ := shop(t, modern, nil)
	result, err := h.call("weather", raw(`{"city":"Rome"}`))
	if err != nil {
		t.Fatal(err)
	}
	id := issued(result)
	blocks := texts(result)
	if id == "" || len(blocks) != 2 || blocks[0] != "sunny in Rome" {
		t.Fatalf("the result: %v", blocks)
	}
	if structured, _ := json.Marshal(result.StructuredContent); string(structured) != `{"forecast":"sunny"}` {
		t.Errorf("structured content: %s", structured)
	}
	if arguments, _ := saw.last(); arguments != `{"city":"Rome"}` {
		t.Errorf("the tool saw %s", arguments)
	}
	first := h.latest()
	if first["sessionId"] != id || first["sessionSource"] != "issued" {
		t.Errorf("the first call's session: %v (%v)", first["sessionId"], first["sessionSource"])
	}
	// The recorded result is the tool's own, without the line.
	if recorded := asJSON(first["result"]); strings.Contains(recorded, "Task ID") || !strings.Contains(recorded, "sunny in Rome") {
		t.Errorf("recorded result: %s", recorded)
	}
	// Sent back, it groups the next call, which gets no other.
	again, err := h.call("weather", raw(`{"city":"Milan","session_id":"`+id+`"}`))
	if err != nil || issued(again) != "" || len(texts(again)) != 1 {
		t.Fatalf("the second result: %v %v", err, texts(again))
	}
	if second := h.latest(); second["sessionId"] != id || second["sessionSource"] != "sent" {
		t.Errorf("the second call's session: %v (%v)", second["sessionId"], second["sessionSource"])
	}
}

// Whatever an agent sends as one of the inputs is taken out before the SDK checks the call: it
// is never why a call is refused.
func TestAnyOtherValueOfAnInputIsTakenOutAndIgnored(t *testing.T) {
	h, saw, _ := shop(t, modern, nil)
	for _, arguments := range []string{
		`{"city":"Rome","session_id":null,"intent":null}`,
		`{"session_id":42,"city":"Rome","intent":["why"]}`,
		`{"city":"Rome","session_id":"not an ID","intent":""}`,
		`{"city":"Rome","session_id":{"a":1},"session_id":"ses_short"}`,
	} {
		result, err := h.call("weather", raw(arguments))
		if err != nil || result.IsError {
			t.Fatalf("%s was refused: %v %v", arguments, err, texts(result))
		}
		if got, _ := saw.last(); got != `{"city":"Rome"}` {
			t.Errorf("%s: the tool saw %s", arguments, got)
		}
		if issued(result) == "" {
			t.Errorf("%s: no session was given", arguments)
		}
		if event := h.latest(); event["sessionSource"] != "issued" || event["intent"] != nil || asJSON(event["input"]) != `{"city":"Rome"}` {
			t.Errorf("%s: session source %v, intent %v, input %s", arguments, event["sessionSource"], event["intent"], asJSON(event["input"]))
		}
	}
	// An input of the tool's own of the wrong type is the SDK's to refuse, as ever.
	if result, err := h.call("weather", raw(`{"city":"Rome","days":"three","intent":"why"}`)); err != nil || !result.IsError || issued(result) != "" {
		t.Errorf("%v %v", err, texts(result))
	}
	if event := h.latest(); event["intent"] != "why" || asJSON(event["input"]) != `{"city":"Rome","days":"three"}` || at(event, "error", "category") != "tool_error" {
		t.Errorf("refused: intent %v, input %s, error %v", event["intent"], asJSON(event["input"]), event["error"])
	}
}

func TestOnAConnectionWithASessionCallsAreGroupedByIt(t *testing.T) {
	h, _, _ := shop(t, legacy, nil)
	sent := "ses_" + strings.Repeat("t", 22)
	connection := ""
	for i, call := range []struct{ tool, arguments, source string }{
		{"weather", `{"city":"Rome","intent":"why"}`, "protocol"},
		{"open", `{}`, "protocol"},
		{"anything", `{"session_id":"` + sent + `"}`, "protocol"},
		{"weather", `{"city":"Rome","session_id":"` + sent + `"}`, "sent"},
	} {
		result, err := h.call(call.tool, raw(call.arguments))
		if err != nil || result.IsError || issued(result) != "" {
			t.Fatalf("%s: %v %v", call.tool, err, texts(result))
		}
		event := h.latest()
		id, _ := event["sessionId"].(string)
		if i == 0 {
			connection = id
			if !regexp.MustCompile(`^[0-9a-f]{64}$`).MatchString(connection) {
				t.Fatalf("the connection's session: %q", connection)
			}
		}
		want := connection
		if call.source == "sent" {
			want = sent
		}
		if id != want || event["sessionSource"] != call.source {
			t.Errorf("call %d: session %v (%v), want %s (%s)", i, id, event["sessionSource"], want, call.source)
		}
		// A tool the wrapper leaves alone keeps a session_id of its own in its arguments.
		if call.tool == "anything" && asJSON(event["input"]) != call.arguments {
			t.Errorf("recorded input of the tool left alone: %s", asJSON(event["input"]))
		}
	}
	// Another connection is another session.
	other, _, _ := shop(t, legacy, nil)
	if _, err := other.call("weather", raw(`{"city":"Rome"}`)); err != nil {
		t.Fatal(err)
	}
	if id := other.one()["sessionId"]; id == connection || id == nil {
		t.Errorf("another connection's session: %v", id)
	}
}

func TestAToolKeepsAnInputOfItsOwn(t *testing.T) {
	h, saw, _ := shop(t, modern, nil)
	result, err := h.call("checkout", raw(`{"session_id":"cs_live_42","item":"book","intent":"buy it"}`))
	if err != nil || result.IsError {
		t.Fatalf("%v %v", err, texts(result))
	}
	arguments, input := saw.last()
	if arguments != `{"session_id":"cs_live_42","item":"book"}` || input != (ownerInput{SessionID: "cs_live_42", Item: "book"}) {
		t.Errorf("the tool saw %s and %+v", arguments, input)
	}
	// Its session_id is its own: the call has no session of the wrapper's, and gets none.
	if got := texts(result); len(got) != 1 || got[0] != "paid in cs_live_42" {
		t.Errorf("the result: %v", got)
	}
	event := h.one()
	if event["sessionId"] != nil || event["intent"] != "buy it" || asJSON(event["input"]) != `{"item":"book","session_id":"cs_live_42"}` {
		t.Errorf("session %v, intent %v, input %s", event["sessionId"], event["intent"], asJSON(event["input"]))
	}
}

// A tool the wrapper leaves alone gets its calls exactly as they came.
func TestAToolThatIsLeftAloneGetsItsCallsAsTheyCame(t *testing.T) {
	h, saw, _ := shop(t, modern, nil)
	for _, tool := range []string{"anything", "open"} {
		sent := `{"intent":"mine","q":"x","session_id":"ses_` + strings.Repeat("m", 22) + `"}`
		result, err := h.call(tool, raw(sent))
		if err != nil || result.IsError || issued(result) != "" {
			t.Fatalf("%s: %v %v", tool, err, texts(result))
		}
		if got, _ := saw.last(); got != sent {
			t.Errorf("%s saw %s", tool, got)
		}
		if event := h.latest(); event["intent"] != nil || event["sessionId"] != nil || asJSON(event["input"]) != sent {
			t.Errorf("%s: intent %v, session %v, input %s", tool, event["intent"], event["sessionId"], asJSON(event["input"]))
		}
	}
	// With OpenSchemas, a schema that doesn't say is taken at its word.
	trusting, saw, _ := shop(t, modern, &Options{Extras: Extras{OpenSchemas: true}})
	if tools := listed(t, trusting); !strings.Contains(tools["open"], sessionDescription) || strings.Contains(tools["anything"], "session_id") {
		t.Errorf("listed: %s / %s", tools["open"], tools["anything"])
	}
	result, err := trusting.call("open", raw(`{"q":"x","intent":"why"}`))
	if err != nil || issued(result) == "" {
		t.Fatalf("%v %v", err, texts(result))
	}
	if got, _ := saw.last(); got != `{"q":"x"}` {
		t.Errorf("open saw %s", got)
	}
}

func TestOnlyAResultThatSaysSomethingGetsASession(t *testing.T) {
	h, _, _ := shop(t, modern, nil)
	// An error result: the tool's own, or the SDK's for arguments it refused.
	for _, arguments := range []string{`{"city":"nowhere"}`, `{"town":"Rome"}`} {
		failed, err := h.call("weather", raw(arguments))
		if err != nil || !failed.IsError || issued(failed) != "" || len(texts(failed)) != 1 {
			t.Errorf("%s: %v %v", arguments, err, texts(failed))
		}
		if event := h.latest(); event["sessionId"] != nil || at(event, "error", "category") != "tool_error" {
			t.Errorf("%s: session %v, error %v", arguments, event["sessionId"], event["error"])
		}
	}
	// An error of the protocol's is no result.
	if _, err := h.call("weather", raw(`{"city":"protocol"}`)); err == nil || !strings.Contains(err.Error(), "not today") {
		t.Errorf("a protocol error: %v", err)
	}
	if event := h.latest(); event["sessionId"] != nil {
		t.Errorf("a protocol error: session %v", event["sessionId"])
	}
	// A result with nothing in it stays empty: a client may read its first block as the value.
	if silent, err := h.call("same", raw(`{}`)); err != nil || issued(silent) == "" {
		t.Errorf("a result with content: %v %v", err, texts(silent))
	}
	h.latest()
}

func TestTheToolsResultIsNeverChanged(t *testing.T) {
	h, _, _ := shop(t, modern, nil)
	for range 3 {
		result, err := h.call("same", raw(`{}`))
		if err != nil || issued(result) == "" || len(texts(result)) != 2 {
			t.Fatalf("%v %v", err, texts(result))
		}
	}
	if len(shared.Content) != 1 || len(shared.Content[:2]) != 2 || shared.Content[:2][1] != nil {
		t.Errorf("the tool's own result was written to: %v", shared.Content[:2])
	}
}

func TestGetMoreTools(t *testing.T) {
	h, _, _ := shop(t, modern, nil)
	check := func(event map[string]any) {
		t.Helper()
		if event["extra"] != "missing_tools" || event["toolName"] != "get_more_tools" || event["toolDescription"] != nil {
			t.Errorf("extra %v, description %v", event["extra"], event["toolDescription"])
		}
	}
	// Called before any listing, by an agent that was listed it elsewhere.
	noted, err := h.call("get_more_tools", raw(`{"need":"a way to refund bob@example.com"}`))
	if err != nil || noted.IsError || texts(noted)[0] != missingToolsAnswer || issued(noted) == "" {
		t.Fatalf("%v %v", err, texts(noted))
	}
	event := h.latest()
	check(event)
	if got := asJSON(event["input"]); got != `{"need":"a way to refund [EMAIL_1]"}` || event["sessionSource"] != "issued" {
		t.Errorf("the request: %s, %v", got, event["sessionSource"])
	}
	refused, err := h.call("get_more_tools", raw(`{"need":7}`))
	if err != nil || !refused.IsError || texts(refused)[0] != missingToolsNoNeed || issued(refused) != "" {
		t.Fatalf("%v %v", err, texts(refused))
	}
	event = h.latest()
	check(event)
	if event["status"] != "error" || at(event, "error", "category") != "protocol" || event["sessionId"] != nil {
		t.Errorf("the refusal: %v %v %v", event["status"], event["error"], event["sessionId"])
	}
	sent := "ses_" + strings.Repeat("m", 22)
	if again, err := h.call("get_more_tools", raw(`{"need":"x","session_id":"`+sent+`"}`)); err != nil || issued(again) != "" {
		t.Fatalf("%v %v", err, texts(again))
	}
	event = h.latest()
	check(event)
	if got := asJSON(event["input"]); got != `{"need":"x"}` || event["sessionId"] != sent || event["sessionSource"] != "sent" {
		t.Errorf("with a session: %s, %v", got, event["sessionId"])
	}
	// Whatever else an agent sends as its session_id is ignored, as for any tool.
	if odd, err := h.call("get_more_tools", raw(`{"need":"x","session_id":37}`)); err != nil || odd.IsError || issued(odd) == "" {
		t.Fatalf("%v %v", err, texts(odd))
	}
	h.latest()
}

// A server's own get_more_tools is the server's, whenever and however it was added.
func TestTheServersOwnGetMoreTools(t *testing.T) {
	theirs := func(ctx context.Context, request *mcp.CallToolRequest, input struct{}) (*mcp.CallToolResult, any, error) {
		return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: "the server's own"}}}, nil, nil
	}
	for name, build := range map[string]func(h *harness){
		"added before the wrapper": func(h *harness) {
			mcp.AddTool(h.server, &mcp.Tool{Name: "get_more_tools"}, theirs)
			addShop(h.server, &seen{})
			Instrument(h.server, h.client, nil)
		},
		"added after the wrapper": func(h *harness) {
			Instrument(h.server, h.client, nil)
			addShop(h.server, &seen{})
			mcp.AddTool(h.server, &mcp.Tool{Name: "get_more_tools"}, theirs)
		},
	} {
		logged := &warnings{}
		h := newHarnessWith(t, lsquare.Options{Logger: logged}, nil, build)
		for range 2 {
			result, err := h.call("get_more_tools", raw(`{}`))
			if err != nil || texts(result)[0] != "the server's own" {
				t.Errorf("%s: %v %v", name, err, result)
			}
			if event := h.latest(); event["extra"] != nil {
				t.Errorf("%s: extra %v", name, event["extra"])
			}
			listed(t, h)
		}
		if !strings.Contains(logged.all(), "[lsquare] The server has its own get_more_tools tool: it is left as it is, and LSquare's isn't added.") {
			t.Errorf("%s: warnings:\n%s", name, logged.all())
		}
	}
	// Added while the server runs, it takes the place of LSquare's.
	h, _, _ := shop(t, modern, nil)
	if tools := listed(t, h); !strings.Contains(tools["get_more_tools"], needDescription) {
		t.Fatalf("listed: %v", tools)
	}
	mcp.AddTool(h.server, &mcp.Tool{Name: "get_more_tools"}, theirs)
	if tools := listed(t, h); strings.Contains(tools["get_more_tools"], needDescription) || len(tools) != 7 {
		t.Errorf("listed after the server added its own: %v", tools)
	}
	result, err := h.call("get_more_tools", raw(`{}`))
	if err != nil || texts(result)[0] != "the server's own" {
		t.Errorf("%v %v", err, result)
	}
	if event := h.latest(); event["extra"] != nil {
		t.Errorf("extra %v", event["extra"])
	}
}

func TestEachExtraCanBeTurnedOff(t *testing.T) {
	t.Run("sessions", func(t *testing.T) {
		h, _, _ := shop(t, legacy, &Options{Extras: Extras{DisableSessions: true}})
		tools := listed(t, h)
		if strings.Contains(tools["weather"], "session_id") || !strings.Contains(tools["weather"], intentDescription) || strings.Contains(tools["get_more_tools"], "session_id") {
			t.Errorf("listed: %s / %s", tools["weather"], tools["get_more_tools"])
		}
		result, err := h.call("weather", raw(`{"city":"Rome","intent":"why"}`))
		if err != nil || issued(result) != "" {
			t.Fatalf("%v %v", err, texts(result))
		}
		if event := h.one(); event["sessionId"] != nil || event["sessionSource"] != nil || event["intent"] != "why" {
			t.Errorf("session %v, intent %v", event["sessionId"], event["intent"])
		}
	})
	t.Run("intent", func(t *testing.T) {
		h, _, _ := shop(t, modern, &Options{Extras: Extras{DisableIntent: true}})
		tools := listed(t, h)
		if strings.Contains(tools["weather"], intentDescription) || !strings.Contains(tools["weather"], sessionDescription) {
			t.Errorf("listed: %s", tools["weather"])
		}
		// An intent nobody listed is an argument like any other, which this tool refuses.
		if result, err := h.call("weather", raw(`{"city":"Rome","intent":"why"}`)); err != nil || !result.IsError {
			t.Errorf("%v %v", err, texts(result))
		}
	})
	t.Run("get_more_tools", func(t *testing.T) {
		h, _, _ := shop(t, modern, &Options{Extras: Extras{DisableMissingTools: true}})
		if tools := listed(t, h); tools["get_more_tools"] != "" || len(tools) != 6 {
			t.Errorf("listed: %v", tools)
		}
		if _, err := h.call("get_more_tools", raw(`{"need":"x"}`)); err == nil {
			t.Error("get_more_tools answered")
		}
	})
	t.Run("all", func(t *testing.T) {
		h, saw, _ := shop(t, legacy, &Options{Extras: Extras{DisableSessions: true, DisableIntent: true, DisableMissingTools: true}})
		plain := unwrapped(t, legacy, &seen{})
		if got, want := listed(t, h), listed(t, plain); len(got) != len(want) || got["weather"] != want["weather"] || got["checkout"] != want["checkout"] {
			t.Errorf("listed %v, want %v", got, want)
		}
		sent := `{"city":"Rome"}`
		if _, err := h.call("weather", raw(sent)); err != nil {
			t.Fatal(err)
		}
		if arguments, _ := saw.last(); arguments != sent {
			t.Errorf("the tool saw %s", arguments)
		}
		if event := h.one(); event["sessionId"] != nil {
			t.Errorf("session %v", event["sessionId"])
		}
	})
	t.Run("inputs off for get_more_tools", func(t *testing.T) {
		h, _, logged := shop(t, modern, &Options{Inputs: func(tool string) bool { return tool != "get_more_tools" }})
		if tools := listed(t, h); tools["get_more_tools"] != "" {
			t.Errorf("listed: %v", tools)
		}
		if !strings.Contains(logged.all(), "[lsquare] get_more_tools isn't offered: inputs are off for it, so what an agent asks for wouldn't be recorded.") {
			t.Errorf("warnings:\n%s", logged.all())
		}
	})
}

func sameInput(a, b any) bool {
	left, _ := json.Marshal(a)
	right, _ := json.Marshal(b)
	return string(left) == string(right)
}

// The same calls, on the same server with and without the wrapper: the tool sees the same
// arguments and the same input, and the client gets the same result, but for the one line more.
func TestACallEndsAsItDoesWithoutTheWrapper(t *testing.T) {
	sent := "ses_" + strings.Repeat("o", 22)
	for _, protocol := range []string{legacy, modern} {
		wrapped, sawWrapped, _ := shop(t, protocol, nil)
		plainSaw := &seen{}
		plain := unwrapped(t, protocol, plainSaw)
		for _, call := range []struct{ tool, business, extras string }{
			{"weather", `"city":"Rome"`, `,"session_id":"` + sent + `","intent":"why"`},
			{"weather", `"city":"Rome","days":3`, `,"intent":"why"`},
			{"weather", `"city":"nowhere"`, `,"session_id":null`},
			{"weather", `"days":"three"`, `,"intent":7`},
			{"weather", `"city":"Rome","other":1`, ``},
			{"weather", `"city":"silent"`, `,"intent":"why"`},
			{"checkout", `"session_id":"cs_1","item":"book"`, `,"intent":"why"`},
			{"checkout", `"item":"book"`, ``},
			{"anything", `"session_id":"x","intent":7`, ``},
			{"open", `"intent":"real","session_id":1`, ``},
			{"closed", `"q":"x"`, `,"intent":"why","session_id":"` + sent + `"`},
			{"closed", `"q":1e3,  "q":"last"`, ``},
			{"same", ``, ``},
		} {
			with, withErr := wrapped.call(call.tool, raw(`{`+call.business+call.extras+`}`))
			without, withoutErr := plain.call(call.tool, raw(`{`+call.business+`}`))
			name := protocol + " " + call.tool + " {" + call.business + "}"
			if (withErr == nil) != (withoutErr == nil) {
				t.Errorf("%s: error %v, want %v", name, withErr, withoutErr)
				continue
			}
			if withErr != nil {
				continue
			}
			got, want := texts(with), texts(without)
			if issued(with) != "" {
				got = got[:len(got)-1]
			}
			if strings.Join(got, "|") != strings.Join(want, "|") || with.IsError != without.IsError {
				t.Errorf("%s: result %v (error %v), want %v (error %v)", name, got, with.IsError, want, without.IsError)
			}
			gotStructured, _ := json.Marshal(with.StructuredContent)
			wantStructured, _ := json.Marshal(without.StructuredContent)
			if string(gotStructured) != string(wantStructured) {
				t.Errorf("%s: structured %s, want %s", name, gotStructured, wantStructured)
			}
			gotArguments, gotInput := sawWrapped.last()
			wantArguments, wantInput := plainSaw.last()
			if gotArguments != wantArguments || !sameInput(gotInput, wantInput) {
				t.Errorf("%s: the tool saw %s %+v, want %s %+v", name, gotArguments, gotInput, wantArguments, wantInput)
			}
		}
	}
}

// With a client that is disabled, the server is as it was: no input, no line, no tool more.
func TestWithADisabledClientTheServerIsLeftAlone(t *testing.T) {
	server := mcp.NewServer(&mcp.Implementation{Name: "plain", Version: "1"}, nil)
	addShop(server, &seen{})
	Instrument(server, lsquare.New(lsquare.Options{DryRun: lsquare.DryRunOff}), nil)
	ctx := context.Background()
	serverTransport, clientTransport := mcp.NewInMemoryTransports()
	serving, err := server.Connect(ctx, serverTransport, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = serving.Close() }()
	session, err := mcp.NewClient(&mcp.Implementation{Name: "test", Version: "1"}, nil).Connect(ctx, clientTransport, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = session.Close() }()
	tools, err := session.ListTools(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	for _, tool := range tools.Tools {
		schema, _ := json.Marshal(tool.InputSchema)
		if tool.Name == "get_more_tools" || (tool.Name == "weather" && strings.Contains(string(schema), "session_id")) {
			t.Errorf("%s: %s", tool.Name, schema)
		}
	}
	result, err := session.CallTool(ctx, &mcp.CallToolParams{Name: "weather", Arguments: raw(`{"city":"Rome"}`)})
	if err != nil || len(texts(result)) != 1 {
		t.Errorf("%v %v", err, texts(result))
	}
}

func TestIdentifySeesTheArgumentsWithoutTheInputs(t *testing.T) {
	asked := make(chan Call, 1)
	h, _, _ := shop(t, modern, &Options{Identify: func(ctx context.Context, call Call) (*Identity, error) {
		asked <- call
		return &Identity{UserID: "u_1"}, nil
	}})
	if _, err := h.call("weather", raw(`{"city":"Rome","intent":"why","session_id":null}`)); err != nil {
		t.Fatal(err)
	}
	if call := <-asked; string(call.Arguments) != `{"city":"Rome"}` {
		t.Errorf("Identify saw %s", call.Arguments)
	}
	if event := h.one(); event["intent"] != "why" || event["userId"] != "u_1" {
		t.Errorf("intent %v, user %v", event["intent"], event["userId"])
	}
}

// After Shutdown agents still have the listing they were given, so the inputs are still taken
// out of their calls; nothing is recorded, issued or offered any more.
func TestAfterShutdownTheInputsAreStillTakenOut(t *testing.T) {
	h, saw, _ := shop(t, modern, nil)
	if tools := listed(t, h); !strings.Contains(tools["weather"], "session_id") || tools["get_more_tools"] == "" {
		t.Fatalf("listed: %v", tools)
	}
	h.client.Shutdown(context.Background())
	for _, arguments := range []string{`{"city":"Rome","intent":"why","session_id":"ses_` + strings.Repeat("a", 22) + `"}`, `{"city":"Rome","intent":null}`} {
		result, err := h.call("weather", raw(arguments))
		if err != nil || result.IsError || len(texts(result)) != 1 {
			t.Fatalf("%s: %v %v", arguments, err, texts(result))
		}
		if got, _ := saw.last(); got != `{"city":"Rome"}` {
			t.Errorf("the tool saw %s", got)
		}
	}
	// What an agent asks for would no longer be recorded, so the tool no longer takes it.
	if _, err := h.call("get_more_tools", raw(`{"need":"x"}`)); err == nil || !strings.Contains(err.Error(), `unknown tool "get_more_tools"`) {
		t.Errorf("get_more_tools after Shutdown: %v", err)
	}
	if tools := listed(t, h); strings.Contains(tools["weather"], "session_id") {
		t.Errorf("listed after Shutdown: %s", tools["weather"])
	}
}

func TestTheFirstCallsOfAConnectionShareItsSession(t *testing.T) {
	h, _, _ := shop(t, legacy, nil)
	var calls sync.WaitGroup
	for range 20 {
		calls.Add(1)
		go func() {
			defer calls.Done()
			if _, err := h.call("weather", raw(`{"city":"Rome","intent":"why"}`)); err != nil {
				t.Error(err)
			}
		}()
	}
	calls.Wait()
	events := h.events()
	if len(events) != 20 {
		t.Fatalf("%d events", len(events))
	}
	for _, event := range events {
		if event["sessionId"] != events[0]["sessionId"] || event["sessionSource"] != "protocol" || event["intent"] != "why" {
			t.Fatalf("sessions differ: %v and %v", event["sessionId"], events[0]["sessionId"])
		}
	}
}

// A call can come before any listing: on a new process, or a server built for one request, from
// an agent that was listed the tool elsewhere. The wrapper then lists the tools itself, once.
func TestACallBeforeAnyListing(t *testing.T) {
	var lists atomic.Int32
	saw := &seen{}
	h := newHarnessWith(t, lsquare.Options{}, &mcp.ClientSessionOptions{ProtocolVersion: modern}, func(h *harness) {
		// Inside the wrapper's: it sees the listing the wrapper asks for.
		h.server.AddReceivingMiddleware(func(next mcp.MethodHandler) mcp.MethodHandler {
			return func(ctx context.Context, method string, request mcp.Request) (mcp.Result, error) {
				if method == "tools/list" {
					lists.Add(1)
				}
				return next(ctx, method, request)
			}
		})
		addShop(h.server, saw)
		Instrument(h.server, h.client, nil)
	})
	result, err := h.call("weather", raw(`{"city":"Rome","intent":"why","session_id":null}`))
	if err != nil || result.IsError || issued(result) == "" {
		t.Fatalf("%v %v", err, texts(result))
	}
	if got, _ := saw.last(); got != `{"city":"Rome"}` {
		t.Errorf("the tool saw %s", got)
	}
	if event := h.latest(); event["intent"] != "why" || event["toolDescription"] != "The weather in a city." {
		t.Errorf("intent %v, description %v", event["intent"], event["toolDescription"])
	}
	// Once: the next calls, to any of the tools it listed, ask for nothing.
	for _, tool := range []string{"weather", "checkout", "same", "weather"} {
		if _, err := h.call(tool, raw(`{"city":"Rome","item":"x"}`)); err != nil {
			t.Fatal(err)
		}
	}
	if lists.Load() != 1 {
		t.Errorf("the wrapper listed the tools %d times", lists.Load())
	}
	// A tool the server doesn't have is looked for once, too.
	for range 3 {
		if _, err := h.call("no_such_tool", raw(`{}`)); err == nil {
			t.Fatal("an unknown tool answered")
		}
	}
	if lists.Load() != 1 {
		t.Errorf("the wrapper listed the tools %d times for an unknown tool", lists.Load())
	}
}

// A listing the wrapper asks for itself can fail, as any request: the call goes on as it came,
// and the wrapper doesn't ask again for a while.
func TestAListingOfTheWrappersOwnThatFails(t *testing.T) {
	var lists atomic.Int32
	saw := &seen{}
	h := newHarnessWith(t, lsquare.Options{}, &mcp.ClientSessionOptions{ProtocolVersion: modern}, func(h *harness) {
		h.server.AddReceivingMiddleware(func(next mcp.MethodHandler) mcp.MethodHandler {
			return func(ctx context.Context, method string, request mcp.Request) (mcp.Result, error) {
				if method == "tools/list" {
					lists.Add(1)
					return nil, errors.New("not now")
				}
				return next(ctx, method, request)
			}
		})
		addShop(h.server, saw)
		Instrument(h.server, h.client, nil)
	})
	for range 3 {
		result, err := h.call("weather", raw(`{"city":"Rome"}`))
		if err != nil || result.IsError || issued(result) != "" {
			t.Fatalf("%v %v", err, texts(result))
		}
	}
	if got, _ := saw.last(); got != `{"city":"Rome"}` {
		t.Errorf("the tool saw %s", got)
	}
	if lists.Load() != 1 {
		t.Errorf("the wrapper asked %d times", lists.Load())
	}
}

// A tool added again under the same name is read anew from the next listing.
func TestAToolAddedAgainUnderTheSameName(t *testing.T) {
	h, _, _ := shop(t, modern, nil)
	if tools := listed(t, h); !strings.Contains(tools["weather"], intentDescription) {
		t.Fatalf("listed: %s", tools["weather"])
	}
	type own struct {
		Intent int `json:"intent"`
	}
	var got own
	mcp.AddTool(h.server, &mcp.Tool{Name: "weather"}, func(ctx context.Context, request *mcp.CallToolRequest, input own) (*mcp.CallToolResult, any, error) {
		got = input
		return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: string(request.Params.Arguments)}}}, nil, nil
	})
	if tools := listed(t, h); strings.Contains(tools["weather"], intentDescription) || !strings.Contains(tools["weather"], sessionDescription) {
		t.Fatalf("listed after the tool was added again: %s", tools["weather"])
	}
	result, err := h.call("weather", raw(`{"intent":7}`))
	if err != nil || result.IsError || got.Intent != 7 || texts(result)[0] != `{"intent":7}` {
		t.Fatalf("%v %v, the tool saw %+v", err, texts(result), got)
	}
	if event := h.latest(); event["intent"] != nil || asJSON(event["input"]) != `{"intent":7}` {
		t.Errorf("recorded intent %v, input %s", event["intent"], asJSON(event["input"]))
	}
}

// A server that lists its tools in pages has the inputs on every page, and its get_more_tools.
func TestAListingInSeveralPages(t *testing.T) {
	saw := &seen{}
	h := newHarnessWith(t, lsquare.Options{}, &mcp.ClientSessionOptions{ProtocolVersion: modern}, func(h *harness) {
		h.server = mcp.NewServer(&mcp.Implementation{Name: "orders", Version: "1.2.3"}, &mcp.ServerOptions{PageSize: 2})
		addShop(h.server, saw)
		Instrument(h.server, h.client, nil)
	})
	tools := map[string]string{}
	pages := 0
	for cursor := ""; ; pages++ {
		page, err := h.session.ListTools(context.Background(), &mcp.ListToolsParams{Cursor: cursor})
		if err != nil {
			t.Fatal(err)
		}
		for _, tool := range page.Tools {
			if _, twice := tools[tool.Name]; twice {
				t.Errorf("%s listed twice", tool.Name)
			}
			tools[tool.Name] = asJSON(tool.InputSchema)
		}
		if cursor = page.NextCursor; cursor == "" {
			break
		}
	}
	if pages < 2 || !strings.Contains(tools["weather"], "session_id") || !strings.Contains(tools["closed"], "session_id") || !strings.Contains(tools["checkout"], "intent") {
		t.Errorf("%d pages: %v", pages+1, tools)
	}
	if tools["get_more_tools"] == "" || len(tools) != 7 {
		t.Errorf("no get_more_tools at the end of the listing: %v", tools)
	}
	if result, err := h.call("get_more_tools", raw(`{"need":"x"}`)); err != nil || texts(result)[0] != missingToolsAnswer {
		t.Errorf("%v %v", err, result)
	}
	h.latest()
}

// A call cancelled before the tool returned is a timeout, whatever the tool then returned.
func TestACancelledCallGetsNoSession(t *testing.T) {
	h := newHarness(t, nil, lsquare.Options{})
	answer := func(cancel func()) mcp.MethodHandler {
		return func(ctx context.Context, method string, request mcp.Request) (mcp.Result, error) {
			if method == "tools/list" {
				return &mcp.ListToolsResult{Tools: []*mcp.Tool{{Name: "search_orders", InputSchema: json.RawMessage(`{"type":"object","additionalProperties":false}`)}}}, nil
			}
			cancel()
			return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: "done"}}}, nil
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	result, err := newInstrumented(bridgeOf(h.client), nil).middleware(answer(cancel))(ctx, "tools/call", callRequest(`{}`))
	if got, _ := result.(*mcp.CallToolResult); err != nil || got == nil || issued(got) != "" {
		t.Fatalf("%v %v", err, result)
	}
	if event := h.latest(); event["sessionId"] != nil || at(event, "error", "category") != "timeout" {
		t.Errorf("session %v, error %v", event["sessionId"], event["error"])
	}
	// The same call, not cancelled, is given one.
	result, _ = newInstrumented(bridgeOf(h.client), nil).middleware(answer(func() {}))(context.Background(), "tools/call", callRequest(`{}`))
	if got, _ := result.(*mcp.CallToolResult); got == nil || issued(got) == "" {
		t.Errorf("a call that wasn't cancelled: %v", result)
	}
	h.latest()
}

// What the tool's code does with the request it was given changes nothing in the event, and
// neither holds more of the arguments than it has.
func TestTheEventsArgumentsAreItsOwn(t *testing.T) {
	big := strings.Repeat("lorem ipsum ", 30_000)
	h := newHarnessWith(t, lsquare.Options{}, nil, func(h *harness) {
		h.server.AddTool(&mcp.Tool{Name: "scribbles", InputSchema: json.RawMessage(`{"type":"object","properties":{"city":{"type":"string"}},"additionalProperties":false}`)}, func(ctx context.Context, request *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
			if cap(request.Params.Arguments) != len(request.Params.Arguments) {
				t.Errorf("the tool's arguments hold %d bytes for %d", cap(request.Params.Arguments), len(request.Params.Arguments))
			}
			for i := range request.Params.Arguments {
				request.Params.Arguments[i] = '#'
			}
			return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: "ok"}}}, nil
		})
		Instrument(h.server, h.client, nil)
	})
	if _, err := h.call("scribbles", raw(`{"city":"Rome","intent":"why"}`)); err != nil {
		t.Fatal(err)
	}
	if event := h.latest(); asJSON(event["input"]) != `{"city":"Rome"}` || event["intent"] != "why" {
		t.Errorf("recorded input %s, intent %v", asJSON(event["input"]), event["intent"])
	}
	// Arguments too large to copy before the call is admitted are copied as the tool returns,
	// as they are without the extras: what the tool wrote over them is what there is to record.
	if _, err := h.call("scribbles", raw(`{"city":"`+big+`","intent":"why"}`)); err != nil {
		t.Fatal(err)
	}
	if event := h.latest(); event["intent"] != "why" || strings.Contains(asJSON(event["input"]), "lorem") {
		t.Errorf("recorded input %.60s, intent %v", asJSON(event["input"]), event["intent"])
	}
	// A long value that is taken out leaves nothing of its size behind.
	took := takeInputs([]byte(`{"session_id":"`+big+`","x":7}`), true, true)
	if string(took.arguments) != `{"x":7}` || cap(took.arguments) != len(took.arguments) {
		t.Errorf("kept %d bytes for %q", cap(took.arguments), took.arguments)
	}
}

// BeforeSend sees why the call was made and which task it belongs to, and may change or clear
// the first.
func TestBeforeSendSeesTheNewFields(t *testing.T) {
	var saw []lsquare.Event
	var mu sync.Mutex
	edit := ""
	h := newHarnessWith(t, lsquare.Options{BeforeSend: func(ctx context.Context, event *lsquare.Event) (*lsquare.Event, error) {
		mu.Lock()
		defer mu.Unlock()
		saw = append(saw, *event)
		switch edit {
		case "clear":
			event.Intent = ""
		case "change":
			event.Intent = "edited for bob@example.com"
			event.SessionID, event.SessionSource, event.Extra = "other", "sent", "missing_tools"
		}
		return event, nil
	}}, nil, func(h *harness) {
		addShop(h.server, &seen{})
		Instrument(h.server, h.client, nil)
	})
	set := func(to string) {
		mu.Lock()
		edit = to
		mu.Unlock()
	}
	result, err := h.call("weather", raw(`{"city":"Rome","intent":"why"}`))
	if err != nil {
		t.Fatal(err)
	}
	id := issued(result)
	event := h.latest()
	mu.Lock()
	first := saw[0]
	mu.Unlock()
	if first.Intent != "why" || first.SessionID != id || first.SessionSource != "issued" || first.Extra != "" {
		t.Errorf("the hook saw intent %q, session %q (%s), extra %q", first.Intent, first.SessionID, first.SessionSource, first.Extra)
	}
	// Returned as it came, the event keeps them.
	if event["intent"] != "why" || event["sessionId"] != id || event["sessionSource"] != "issued" {
		t.Errorf("sent intent %v, session %v (%v)", event["intent"], event["sessionId"], event["sessionSource"])
	}
	set("clear")
	if _, err := h.call("weather", raw(`{"city":"Rome","intent":"why"}`)); err != nil {
		t.Fatal(err)
	}
	if event := h.latest(); event["intent"] != nil || event["sessionSource"] != "issued" {
		t.Errorf("cleared: intent %v, session source %v", event["intent"], event["sessionSource"])
	}
	set("change")
	if _, err := h.call("weather", raw(`{"city":"Rome","intent":"why"}`)); err != nil {
		t.Fatal(err)
	}
	// Which task the call belongs to isn't the hook's to change.
	if event := h.latest(); event["intent"] != "edited for [EMAIL_1]" || event["sessionSource"] != "issued" || event["sessionId"] == "other" || event["extra"] != nil {
		t.Errorf("changed: intent %v, session %v (%v), extra %v", event["intent"], event["sessionId"], event["sessionSource"], event["extra"])
	}
	set("")
	if _, err := h.call("get_more_tools", raw(`{"need":"x"}`)); err != nil {
		t.Fatal(err)
	}
	h.latest()
	mu.Lock()
	last := saw[len(saw)-1]
	mu.Unlock()
	if last.Extra != "missing_tools" {
		t.Errorf("the hook saw extra %q", last.Extra)
	}
}

// get_more_tools takes a request only while it would be recorded.
func TestGetMoreToolsOnlyAnswersWhatIsRecorded(t *testing.T) {
	var mu sync.Mutex
	on := true
	h, _, _ := shop(t, modern, &Options{Inputs: func(tool string) bool {
		mu.Lock()
		defer mu.Unlock()
		return on || tool != "get_more_tools"
	}})
	if result, err := h.call("get_more_tools", raw(`{"need":"x"}`)); err != nil || texts(result)[0] != missingToolsAnswer {
		t.Fatalf("%v %v", err, result)
	}
	h.latest()
	mu.Lock()
	on = false
	mu.Unlock()
	if _, err := h.call("get_more_tools", raw(`{"need":"x"}`)); err == nil || !strings.Contains(err.Error(), `unknown tool "get_more_tools"`) {
		t.Errorf("with its inputs off: %v", err)
	}
	if event := h.latest(); event["extra"] != nil || event["input"] != nil || at(event, "error", "category") != "protocol" {
		t.Errorf("with its inputs off: extra %v, input %v, error %v", event["extra"], event["input"], event["error"])
	}
}

// Over HTTP: a server that keeps sessions groups a client's calls by its session, whose ID never
// leaves as it is, and one that keeps none gives each task an ID.
func TestSessionsOverHTTP(t *testing.T) {
	own := "ses_" + strings.Repeat("h", 22)
	connect := func(t *testing.T, options *mcp.StreamableHTTPOptions) (*harness, *mcp.ClientSession) {
		t.Helper()
		h := newHarnessWith(t, lsquare.Options{}, nil, func(h *harness) {
			// The server's session IDs happen to look like the wrapper's.
			h.server = mcp.NewServer(&mcp.Implementation{Name: "orders", Version: "1.2.3"}, &mcp.ServerOptions{GetSessionID: func() string { return own }})
			addShop(h.server, &seen{})
			Instrument(h.server, h.client, nil)
		})
		web := httptest.NewServer(mcp.NewStreamableHTTPHandler(func(*http.Request) *mcp.Server { return h.server }, options))
		t.Cleanup(web.Close)
		session, err := mcp.NewClient(&mcp.Implementation{Name: "test-agent", Version: "0.9"}, nil).Connect(context.Background(), &mcp.StreamableClientTransport{Endpoint: web.URL}, &mcp.ClientSessionOptions{ProtocolVersion: legacy})
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = session.Close() })
		return h, session
	}
	call := func(t *testing.T, session *mcp.ClientSession, arguments string, refused bool) *mcp.CallToolResult {
		t.Helper()
		result, err := session.CallTool(context.Background(), &mcp.CallToolParams{Name: "weather", Arguments: raw(arguments)})
		if err != nil || result.IsError != refused {
			t.Fatalf("%s: %v %v", arguments, err, result)
		}
		return result
	}
	t.Run("a server that keeps sessions", func(t *testing.T) {
		h, session := connect(t, nil)
		if session.ID() != own {
			t.Fatalf("the session's ID: %q", session.ID())
		}
		other := "ses_" + strings.Repeat("o", 22)
		for _, step := range []struct {
			arguments, id, source string
			refused               bool
		}{
			{`{"city":"Rome"}`, sessionHash(own), "protocol", false},
			// Sent back, the session's own ID is still that session, recorded as its hash.
			{`{"city":"Rome","session_id":"` + own + `"}`, sessionHash(own), "protocol", false},
			// Also on a call the SDK refuses.
			{`{"town":"Rome","session_id":"` + own + `"}`, sessionHash(own), "protocol", true},
			{`{"city":"Rome","session_id":"` + other + `"}`, other, "sent", false},
		} {
			if result := call(t, session, step.arguments, step.refused); issued(result) != "" {
				t.Errorf("%s: a session was issued", step.arguments)
			}
			if event := h.latest(); event["sessionId"] != step.id || event["sessionSource"] != step.source {
				t.Errorf("%s: session %v (%v), want %s (%s)", step.arguments, event["sessionId"], event["sessionSource"], step.id, step.source)
			}
		}
		for _, event := range h.events() {
			if strings.Contains(asJSON(event), own) {
				t.Errorf("the session's own ID left: %s", asJSON(event))
			}
		}
		// A tool the wrapper leaves alone gets it as it was sent, and the event's input has its
		// hash, wherever the arguments repeat it.
		kept, err := session.CallTool(context.Background(), &mcp.CallToolParams{Name: "open", Arguments: raw(`{"session_id":"` + own + `","q":"about ` + own + `"}`)})
		if err != nil || !strings.Contains(texts(kept)[0], own) {
			t.Fatalf("%v %v", err, kept)
		}
		if event := h.latest(); at(event, "input", "session_id") != sessionHash(own) || strings.Contains(asJSON(event["input"]), own) || event["sessionSource"] != "protocol" {
			t.Errorf("recorded input %s", asJSON(event["input"]))
		}
	})
	t.Run("a server that keeps none", func(t *testing.T) {
		h, session := connect(t, &mcp.StreamableHTTPOptions{Stateless: true})
		result := call(t, session, `{"city":"Rome"}`, false)
		id := issued(result)
		if id == "" {
			t.Fatalf("no session was issued: %v", texts(result))
		}
		if event := h.latest(); event["sessionId"] != id || event["sessionSource"] != "issued" {
			t.Errorf("session %v (%v)", event["sessionId"], event["sessionSource"])
		}
		if again := call(t, session, `{"city":"Rome","session_id":"`+id+`"}`, false); issued(again) != "" {
			t.Errorf("a second session was issued: %v", texts(again))
		}
		if event := h.latest(); event["sessionId"] != id || event["sessionSource"] != "sent" {
			t.Errorf("session %v (%v)", event["sessionId"], event["sessionSource"])
		}
	})
}

// A server built for each request, as a stateless HTTP server often is: every one is new to the
// wrapper, and every call still has its inputs taken out and its task.
func TestAServerBuiltForEachRequest(t *testing.T) {
	saw := &seen{}
	var h *harness
	h = newHarnessWith(t, lsquare.Options{}, nil, func(h *harness) {})
	web := httptest.NewServer(mcp.NewStreamableHTTPHandler(func(*http.Request) *mcp.Server {
		server := mcp.NewServer(&mcp.Implementation{Name: "orders", Version: "1.2.3"}, nil)
		addShop(server, saw)
		Instrument(server, h.client, nil)
		return server
	}, &mcp.StreamableHTTPOptions{Stateless: true}))
	t.Cleanup(web.Close)
	session, err := mcp.NewClient(&mcp.Implementation{Name: "test-agent", Version: "0.9"}, nil).Connect(context.Background(), &mcp.StreamableClientTransport{Endpoint: web.URL}, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = session.Close() })
	tools, err := session.ListTools(context.Background(), nil)
	if err != nil {
		t.Fatal(err)
	}
	names := map[string]bool{}
	for _, tool := range tools.Tools {
		names[tool.Name] = true
	}
	if !names["get_more_tools"] || len(names) != 7 {
		t.Errorf("listed: %v", names)
	}
	first, err := session.CallTool(context.Background(), &mcp.CallToolParams{Name: "weather", Arguments: raw(`{"city":"Rome","intent":"why"}`)})
	if err != nil || first.IsError || issued(first) == "" {
		t.Fatalf("%v %v", err, first)
	}
	if got, _ := saw.last(); got != `{"city":"Rome"}` {
		t.Errorf("the tool saw %s", got)
	}
	if event := h.latest(); event["intent"] != "why" || event["sessionSource"] != "issued" {
		t.Errorf("intent %v, session source %v", event["intent"], event["sessionSource"])
	}
	noted, err := session.CallTool(context.Background(), &mcp.CallToolParams{Name: "get_more_tools", Arguments: raw(`{"need":"x","session_id":"` + issued(first) + `"}`)})
	if err != nil || noted.IsError || texts(noted)[0] != missingToolsAnswer {
		t.Fatalf("%v %v", err, noted)
	}
	if event := h.latest(); event["extra"] != "missing_tools" || event["sessionId"] != issued(first) {
		t.Errorf("extra %v, session %v", event["extra"], event["sessionId"])
	}
}

// A server's own get_more_tools is the one that answers even where its listings hide it: the
// wrapper never takes a call the server has a tool for.
func TestGetMoreToolsNeverTakesTheServersOwn(t *testing.T) {
	h := newHarnessWith(t, lsquare.Options{}, nil, func(h *harness) {
		// Inside the wrapper's: no listing shows the server's own get_more_tools.
		h.server.AddReceivingMiddleware(func(next mcp.MethodHandler) mcp.MethodHandler {
			return func(ctx context.Context, method string, request mcp.Request) (mcp.Result, error) {
				result, err := next(ctx, method, request)
				if page, ok := result.(*mcp.ListToolsResult); ok {
					shown := *page
					shown.Tools = nil
					for _, tool := range page.Tools {
						if tool.Name != "get_more_tools" {
							shown.Tools = append(shown.Tools, tool)
						}
					}
					return &shown, err
				}
				return result, err
			}
		})
		addShop(h.server, &seen{})
		mcp.AddTool(h.server, &mcp.Tool{Name: "get_more_tools"}, func(ctx context.Context, request *mcp.CallToolRequest, input map[string]any) (*mcp.CallToolResult, any, error) {
			return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: "the server's own, with " + string(request.Params.Arguments)}}}, nil, nil
		})
		Instrument(h.server, h.client, nil)
	})
	listed(t, h)
	sent := `{"need":"x","session_id":"ses_` + strings.Repeat("g", 22) + `"}`
	for range 2 {
		result, err := h.call("get_more_tools", raw(sent))
		if err != nil || texts(result)[0] != "the server's own, with "+sent || issued(result) != "" {
			t.Fatalf("%v %v", err, texts(result))
		}
		if event := h.latest(); event["extra"] != nil || event["sessionId"] != nil || asJSON(event["input"]) != sent {
			t.Errorf("extra %v, session %v, input %s", event["extra"], event["sessionId"], asJSON(event["input"]))
		}
	}
}

// The wrapper's answer to get_more_tools is written as the SDK writes a tool's own result.
func TestGetMoreToolsAnswersAsATool(t *testing.T) {
	for _, protocol := range []string{legacy, modern} {
		var mu sync.Mutex
		replies := map[string]string{}
		h := newHarnessWith(t, lsquare.Options{}, &mcp.ClientSessionOptions{ProtocolVersion: protocol}, func(h *harness) {
			addShop(h.server, &seen{})
			Instrument(h.server, h.client, &Options{Extras: Extras{DisableSessions: true}})
			// Outside the wrapper's: it sees what clients are sent.
			h.server.AddReceivingMiddleware(func(next mcp.MethodHandler) mcp.MethodHandler {
				return func(ctx context.Context, method string, request mcp.Request) (mcp.Result, error) {
					result, err := next(ctx, method, request)
					if call, ok := request.(*mcp.CallToolRequest); ok && err == nil {
						mu.Lock()
						replies[call.Params.Name+" "+string(call.Params.Arguments)] = asJSON(result)
						mu.Unlock()
					}
					return result, err
				}
			})
		})
		for _, arguments := range []string{`{"need":"x"}`, `{}`} {
			if _, err := h.call("get_more_tools", raw(arguments)); err != nil {
				t.Fatal(err)
			}
		}
		if _, err := h.call("weather", raw(`{"city":"Rome"}`)); err != nil {
			t.Fatal(err)
		}
		mu.Lock()
		noted, refused, own := replies[`get_more_tools {"need":"x"}`], replies[`get_more_tools {}`], replies[`weather {"city":"Rome"}`]
		mu.Unlock()
		// As a tool of the server's own says it: with the mark of a last result where the
		// protocol has one, and without it where it doesn't.
		marked := strings.Contains(own, `"resultType":"complete"`)
		if marked != (protocol == modern) {
			t.Fatalf("%s: the server's own result: %s", protocol, own)
		}
		for name, reply := range map[string]string{"noted": noted, "refused": refused} {
			if strings.Contains(reply, `"resultType":"complete"`) != marked {
				t.Errorf("%s: %s is written %s, and the server's own %s", protocol, name, reply, own)
			}
		}
		if !strings.Contains(noted, missingToolsAnswer) || !strings.Contains(refused, `"isError":true`) {
			t.Errorf("%s: %s / %s", protocol, noted, refused)
		}
		h.events()
	}
}

// A server whose listing waits, or can't answer while a call runs, doesn't hold a call: the call
// goes on as it came after a second, and the listing is read when it comes.
func TestAListingThatWaitsDoesNotHoldACall(t *testing.T) {
	release := make(chan struct{})
	saw := &seen{}
	h := newHarnessWith(t, lsquare.Options{}, &mcp.ClientSessionOptions{ProtocolVersion: modern}, func(h *harness) {
		h.server.AddReceivingMiddleware(func(next mcp.MethodHandler) mcp.MethodHandler {
			return func(ctx context.Context, method string, request mcp.Request) (mcp.Result, error) {
				if method == "tools/list" {
					<-release
				}
				return next(ctx, method, request)
			}
		})
		addShop(h.server, saw)
		Instrument(h.server, h.client, nil)
	})
	began := time.Now()
	result, err := h.call("weather", raw(`{"city":"Rome"}`))
	if err != nil || result.IsError || issued(result) != "" || texts(result)[0] != "sunny in Rome" {
		t.Fatalf("%v %v", err, texts(result))
	}
	if waited := time.Since(began); waited < listWait || waited > 5*listWait {
		t.Errorf("the call waited %v", waited)
	}
	// A second call doesn't start a second listing, and doesn't wait for the first one again:
	// the second it had is over.
	began = time.Now()
	if _, err := h.call("weather", raw(`{"city":"Rome"}`)); err != nil {
		t.Fatal(err)
	}
	if waited := time.Since(began); waited > listWait/2 {
		t.Errorf("the second call waited %v", waited)
	}
	close(release)
	// Once the listing is read, calls have their task.
	deadline := time.Now().Add(5 * time.Second)
	for {
		result, err := h.call("weather", raw(`{"city":"Rome","intent":"why"}`))
		if err == nil && !result.IsError && issued(result) != "" {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("the listing was never read: %v %v", err, result)
		}
		time.Sleep(20 * time.Millisecond)
	}
	if got, _ := saw.last(); got != `{"city":"Rome"}` {
		t.Errorf("the tool saw %s", got)
	}
	h.events()
}

// A tool the server no longer lists is let go of, and a tool whose schema can't be shown with
// the inputs is left alone altogether: nothing is taken out of its calls, and no agent is told
// to send it an ID.
func TestTheCatalogFollowsTheListing(t *testing.T) {
	h := newHarness(t, nil, lsquare.Options{})
	w := newInstrumented(bridgeOf(h.client), nil)
	recursive := json.RawMessage(`{"type":"object","properties":{"child":{"$ref":"#"}},"additionalProperties":false}`)
	closed := json.RawMessage(`{"type":"object","additionalProperties":false}`)
	tools := []*mcp.Tool{{Name: "a", InputSchema: closed}, {Name: "b", InputSchema: closed}, {Name: "tree", InputSchema: recursive}}
	var arguments string
	handler := w.middleware(func(ctx context.Context, method string, request mcp.Request) (mcp.Result, error) {
		if method == "tools/list" {
			return &mcp.ListToolsResult{Tools: tools}, nil
		}
		arguments = string(request.(*mcp.CallToolRequest).Params.Arguments)
		return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: "done"}}}, nil
	})
	list := func() map[string]string {
		result, err := handler(context.Background(), "tools/list", &mcp.ListToolsRequest{})
		if err != nil {
			t.Fatal(err)
		}
		out := map[string]string{}
		for _, tool := range result.(*mcp.ListToolsResult).Tools {
			out[tool.Name] = asJSON(tool.InputSchema)
		}
		return out
	}
	call := func(name, sent string) *mcp.CallToolResult {
		result, err := handler(context.Background(), "tools/call", &mcp.CallToolRequest{Params: &mcp.CallToolParamsRaw{Name: name, Arguments: raw(sent)}})
		if err != nil {
			t.Fatal(err)
		}
		return result.(*mcp.CallToolResult)
	}
	if listed := list(); !strings.Contains(listed["a"], "session_id") || listed["tree"] != string(recursive) || listed["get_more_tools"] == "" {
		t.Errorf("listed: %v", listed)
	}
	sent := `{"child":{},"session_id":"x","intent":"why"}`
	if result := call("tree", sent); arguments != sent || issued(result) != "" {
		t.Errorf("the tool whose schema can't be shown with the inputs saw %s, and got %v", arguments, texts(result))
	}
	if result := call("b", `{"intent":"why"}`); arguments != `{}` || issued(result) == "" {
		t.Errorf("b saw %s, and got %v", arguments, texts(result))
	}
	// Removed: the next whole listing lets it go, and its calls are the server's to answer.
	tools = tools[:1]
	if listed := list(); listed["b"] != "" {
		t.Errorf("listed: %v", listed)
	}
	w.mu.Lock()
	known := len(w.catalog.plans)
	w.mu.Unlock()
	if known != 1 {
		t.Errorf("the catalog holds %d tools", known)
	}
	if result := call("b", `{"intent":"why"}`); arguments != `{"intent":"why"}` || issued(result) != "" {
		t.Errorf("after it was removed, b saw %s, and got %v", arguments, texts(result))
	}
	h.events()
}

// served is the wrapper's middleware over a server stood in for by two functions: what it lists,
// and how it answers a call.
type served struct {
	t       *testing.T
	wrapper *instrumented
	handler mcp.MethodHandler
}

func serve(t *testing.T, h *harness, options *Options, list func(request *mcp.ListToolsRequest) (*mcp.ListToolsResult, error), call func(request *mcp.CallToolRequest) (*mcp.CallToolResult, error)) *served {
	w := newInstrumented(bridgeOf(h.client), options)
	return &served{t: t, wrapper: w, handler: w.middleware(func(ctx context.Context, method string, request mcp.Request) (mcp.Result, error) {
		if method == "tools/list" {
			// As the SDK returns it: a nil result is no result.
			page, err := list(request.(*mcp.ListToolsRequest))
			if page == nil {
				return nil, err
			}
			return page, err
		}
		result, err := call(request.(*mcp.CallToolRequest))
		if result == nil {
			return nil, err
		}
		return result, err
	})}
}

func (s *served) list(cursor string) *mcp.ListToolsResult {
	s.t.Helper()
	result, err := s.handler(context.Background(), "tools/list", &mcp.ListToolsRequest{Params: &mcp.ListToolsParams{Cursor: cursor}})
	if err != nil {
		s.t.Fatal(err)
	}
	return result.(*mcp.ListToolsResult)
}

func (s *served) names() []string {
	s.t.Helper()
	var out []string
	for cursor := ""; ; {
		page := s.list(cursor)
		for _, tool := range page.Tools {
			out = append(out, tool.Name)
		}
		if cursor = page.NextCursor; cursor == "" {
			return out
		}
	}
}

func (s *served) call(request *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	result, err := s.handler(context.Background(), "tools/call", request)
	typed, _ := result.(*mcp.CallToolResult)
	return typed, err
}

func (s *served) known() []string {
	s.wrapper.mu.Lock()
	defer s.wrapper.mu.Unlock()
	var out []string
	for name := range s.wrapper.catalog.plans {
		out = append(out, name)
	}
	return out
}

var closedSchema = json.RawMessage(`{"type":"object","additionalProperties":false}`)

func echoed(request *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: string(request.Params.Arguments)}}}, nil
}

// The connection's own ID is kept out of the event wherever a call's texts repeat it, and
// nothing else of the event changes for it: no key, no number, nothing of the JSON.
func TestTheConnectionsIDIsMaskedInTextsOnly(t *testing.T) {
	for _, own := range []string{"word", "1", `a"b`} {
		for _, options := range []*Options{nil, {Extras: Extras{DisableSessions: true}}} {
			h := newHarnessWith(t, lsquare.Options{}, nil, func(h *harness) {
				h.server = mcp.NewServer(&mcp.Implementation{Name: "orders", Version: "1.2.3"}, &mcp.ServerOptions{GetSessionID: func() string { return own }})
				addShop(h.server, &seen{})
				Instrument(h.server, h.client, options)
			})
			web := httptest.NewServer(mcp.NewStreamableHTTPHandler(func(*http.Request) *mcp.Server { return h.server }, nil))
			t.Cleanup(web.Close)
			session, err := mcp.NewClient(&mcp.Implementation{Name: "test-agent", Version: "0.9"}, nil).Connect(context.Background(), &mcp.StreamableClientTransport{Endpoint: web.URL}, &mcp.ClientSessionOptions{ProtocolVersion: legacy})
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { _ = session.Close() })
			quotedID, _ := json.Marshal(own)
			// A secret under a key that holds the ID, a key that holds it, a number that is it,
			// and two texts that repeat it, one of them with an escape for its first character.
			escaped := fmt.Sprintf(`"\u%04x%s`, []rune(own)[0], string(quotedID[2:]))
			sent := `{"password":"hunter2","wordy":1,"n":1,"said":` + string(quotedID) + `,"note":` + escaped + `}`
			result, err := session.CallTool(context.Background(), &mcp.CallToolParams{Name: "open", Arguments: raw(sent)})
			if err != nil || texts(result)[0] != sent {
				t.Fatalf("%q: %v %v", own, err, result)
			}
			input, _ := h.latest()["input"].(map[string]any)
			hash := sessionHash(own)
			if input["password"] != "[SECRET_1]" || input["wordy"] != float64(1) || input["n"] != float64(1) || input["said"] != hash || input["note"] != hash || len(input) != 5 {
				t.Errorf("%q: recorded input %s", own, asJSON(input))
			}
			// And in why the call was made.
			if _, err := session.CallTool(context.Background(), &mcp.CallToolParams{Name: "closed", Arguments: raw(`{"intent":` + quoted("for "+own) + `}`)}); err != nil {
				t.Fatal(err)
			}
			if event := h.latest(); options == nil && event["intent"] != "for "+hash {
				t.Errorf("%q: intent %v", own, event["intent"])
			}
		}
	}
}

// What the server's middleware does to the request the wrapper lists the tools with, the call
// never sees: it is the wrapper's own, with copies of what says who is asking.
func TestTheWrappersListingIsARequestOfItsOwn(t *testing.T) {
	h := newHarness(t, nil, lsquare.Options{})
	var listed *mcp.ListToolsRequest
	s := serve(t, h, nil, func(request *mcp.ListToolsRequest) (*mcp.ListToolsResult, error) {
		listed = request
		request.Extra.TokenInfo.UserID = "list"
		request.Extra.TokenInfo.Scopes[0] = "list"
		request.Extra.TokenInfo.Extra["tenant"] = "list"
		request.Extra.Header.Set("X-Route", "list")
		request.Params.Meta["tenant"].(map[string]any)["id"] = "list"
		return &mcp.ListToolsResult{Tools: []*mcp.Tool{{Name: "search_orders", InputSchema: closedSchema}}}, nil
	}, func(request *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		who := fmt.Sprint(request.Extra.TokenInfo.UserID, request.Extra.TokenInfo.Scopes, request.Extra.TokenInfo.Extra["tenant"], request.Extra.Header.Get("X-Route"), request.Params.Meta["tenant"], request.Params.Meta["progressToken"])
		return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: who}}}, nil
	})
	request := callRequest(`{}`)
	request.Extra = &mcp.RequestExtra{TokenInfo: &auth.TokenInfo{UserID: "call", Scopes: []string{"call"}, Extra: map[string]any{"tenant": "call"}}, Header: http.Header{"X-Route": {"call"}}}
	request.Params.Meta = mcp.Meta{"tenant": map[string]any{"id": "call"}, "progressToken": "p1"}
	result, err := s.call(request)
	if err != nil || texts(result)[0] != "call[call]callcallmap[id:call]p1" {
		t.Fatalf("the tool saw %v (%v)", texts(result), err)
	}
	// It was asked as the same caller, without what belongs to the call's own reply.
	if listed == nil || listed.Params.Meta["tenant"] == nil || listed.Params.Meta["progressToken"] != nil || listed.Extra.Header.Get("X-Route") != "list" {
		t.Errorf("the listing was asked with %+v", listed)
	}
	h.events()
}

// The catalog is the server's tools as last listed in full: a listing that ends after a newer
// one is dropped, the same tool listed with another schema is read again, and a tool that is
// gone is let go, also where the server lists in pages.
func TestTheCatalogIsTheLastWholeListing(t *testing.T) {
	h := newHarness(t, nil, lsquare.Options{})
	intentOwner := json.RawMessage(`{"type":"object","properties":{"intent":{"type":"integer"}},"required":["intent"],"additionalProperties":false}`)
	tool := &mcp.Tool{Name: "a", InputSchema: closedSchema}
	tools := []*mcp.Tool{tool, {Name: "b", InputSchema: closedSchema}, {Name: "c", InputSchema: closedSchema}}
	var mu sync.Mutex
	held := make(chan struct{})
	holding := false
	s := serve(t, h, nil, func(request *mcp.ListToolsRequest) (*mcp.ListToolsResult, error) {
		mu.Lock()
		current, wait := append([]*mcp.Tool(nil), tools...), holding
		mu.Unlock()
		if wait && request.Extra == nil && request.Session == nil && request.Params.Meta == nil && request.Params.Cursor == "held" {
			<-held
		}
		// Two tools a page.
		from := 0
		for i, tool := range current {
			if tool.Name == request.Params.Cursor {
				from = i
			}
		}
		page := &mcp.ListToolsResult{Tools: current[from:min(from+2, len(current))]}
		if from+2 < len(current) {
			page.NextCursor = current[from+2].Name
		}
		return page, nil
	}, echoed)
	if got := s.names(); strings.Join(got, ",") != "a,b,c,get_more_tools" {
		t.Fatalf("listed %v", got)
	}
	// The same tool, listed with another schema: an intent of its own.
	mu.Lock()
	tool.InputSchema = intentOwner
	mu.Unlock()
	s.names()
	if result, err := s.call(&mcp.CallToolRequest{Params: &mcp.CallToolParamsRaw{Name: "a", Arguments: raw(`{"intent":7}`)}}); err != nil || texts(result)[0] != `{"intent":7}` {
		t.Errorf("after its schema changed, the tool saw %v (%v)", texts(result), err)
	}
	// Removed: the next traversal, in pages, lets it go.
	mu.Lock()
	tools = tools[:2]
	mu.Unlock()
	if got := s.names(); strings.Join(got, ",") != "a,b,get_more_tools" {
		t.Errorf("listed %v", got)
	}
	known := s.known()
	if len(known) != 2 {
		t.Errorf("the catalog holds %v", known)
	}
	// A server that had its own get_more_tools, and removed it, gets LSquare's.
	mu.Lock()
	tools = []*mcp.Tool{tool, {Name: "b", InputSchema: closedSchema}, {Name: "get_more_tools", InputSchema: closedSchema}}
	mu.Unlock()
	if got := s.names(); strings.Join(got, ",") != "a,b,get_more_tools" {
		t.Errorf("with the server's own get_more_tools, listed %v", got)
	}
	mu.Lock()
	tools = tools[:2]
	mu.Unlock()
	if got := s.names(); strings.Join(got, ",") != "a,b,get_more_tools" {
		t.Errorf("once the server's own is gone, listed %v", got)
	}
	h.events()
}

// The wrapper answers get_more_tools only for what the SDK itself says of a tool the server
// doesn't have: an error with anything of the app's in it is the app's answer.
func TestGetMoreToolsLeavesTheAppsOwnErrors(t *testing.T) {
	h := newHarness(t, nil, lsquare.Options{})
	var answerWith error
	s := serve(t, h, nil, func(request *mcp.ListToolsRequest) (*mcp.ListToolsResult, error) {
		return &mcp.ListToolsResult{Tools: []*mcp.Tool{{Name: "a", InputSchema: closedSchema}}}, nil
	}, func(request *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		return nil, answerWith
	})
	request := func() *mcp.CallToolRequest {
		return &mcp.CallToolRequest{Params: &mcp.CallToolParamsRaw{Name: "get_more_tools", Arguments: raw(`{"need":"x"}`)}}
	}
	missing := `unknown tool "get_more_tools"`
	for name, err := range map[string]error{
		"with data of its own": &jsonrpc.Error{Code: jsonrpc.CodeInvalidParams, Message: missing, Data: json.RawMessage(`{"why":"not for you"}`)},
		"another code":         &jsonrpc.Error{Code: jsonrpc.CodeInvalidRequest, Message: missing},
		"another message":      &jsonrpc.Error{Code: jsonrpc.CodeInvalidParams, Message: "unknown tool"},
		"wrapped":              fmt.Errorf("denied: %w", &jsonrpc.Error{Code: jsonrpc.CodeInvalidParams, Message: missing}),
	} {
		answerWith = err
		if result, got := s.call(request()); got != err || result != nil {
			t.Errorf("%s: %v %v", name, result, got)
		}
	}
	answerWith = &jsonrpc.Error{Code: jsonrpc.CodeInvalidParams, Message: missing}
	if result, err := s.call(request()); err != nil || texts(result)[0] != missingToolsAnswer {
		t.Errorf("the SDK's own answer: %v %v", result, err)
	}
	h.events()
}

// After Shutdown a server built for a request still has the inputs taken out of its calls: the
// agents that send them were listed them before.
func TestAfterShutdownANewServerStillTakesTheInputsOut(t *testing.T) {
	h := newHarness(t, nil, lsquare.Options{})
	h.client.Shutdown(context.Background())
	s := serve(t, h, nil, func(request *mcp.ListToolsRequest) (*mcp.ListToolsResult, error) {
		return &mcp.ListToolsResult{Tools: []*mcp.Tool{{Name: "a", InputSchema: closedSchema}}}, nil
	}, echoed)
	result, err := s.call(&mcp.CallToolRequest{Params: &mcp.CallToolParamsRaw{Name: "a", Arguments: raw(`{"intent":"why","session_id":null}`)}})
	if err != nil || len(texts(result)) != 1 || texts(result)[0] != `{}` {
		t.Errorf("%v %v", result, err)
	}
}
