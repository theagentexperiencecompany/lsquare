package lsquare

import (
	"context"
	"encoding/json"
	"math"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/theagentexperiencecompany/lsquare/packages/go/internal/bridge"
	"github.com/theagentexperiencecompany/lsquare/packages/go/internal/contracttest"
	"github.com/theagentexperiencecompany/lsquare/packages/go/internal/core"
	"github.com/theagentexperiencecompany/lsquare/packages/go/internal/jsonv"
)

// sent is every event the ingestion got, each as the JSON it was sent as.
func (in *ingestion) sent() [][]byte {
	var out [][]byte
	for _, event := range in.events() {
		raw, _ := json.Marshal(event)
		out = append(out, raw)
	}
	return out
}

// valid fails the test for an event the ingestion would refuse.
func valid(t *testing.T, in *ingestion) {
	t.Helper()
	events := in.sent()
	for i, verdict := range contracttest.Ingestion(t, events) {
		if verdict != "valid" {
			t.Errorf("the ingestion refuses an event as %s: %s", verdict, events[i])
		}
	}
}

var fullTurn = Turn{
	OrgID: "org_1", UserID: "user_1", ConversationID: "conv_1", TurnID: "7",
	Question: "Where is the order of alice@example.com?", Answer: "It ships tomorrow.",
	Status: StatusCompleted,
	ToolCalls: []ToolCall{
		{Name: "search_orders", Input: json.RawMessage(`{"customer":"alice@example.com"}`), ResultChars: Ptr(120), ResultIsError: Ptr(false), DurationMs: Ptr(41.5)},
		{Name: "refund", ErrorMessage: "not allowed", ResultIsError: Ptr(true)},
	},
	Usage:   &Usage{InputTokens: Ptr(812), OutputTokens: Ptr(240), CostUSD: Ptr(0.004)},
	Latency: json.RawMessage(`{"firstTokenMs":320}`),
	Client:  "web", ClientVersion: "3.1",
	APIKeyID: "key_9", Surface: "chat",
	Metadata: map[string]any{"plan": "pro", "seats": 3},
}

func TestCaptureSendsATurn(t *testing.T) {
	in := newIngestion(t)
	c := New(Options{APIKey: "lsq_test", Endpoint: in.URL, DryRun: DryRunOff})
	c.Capture(context.Background(), fullTurn)
	flush(t, c)
	events := in.events()
	if len(events) != 1 {
		t.Fatalf("%d events", len(events))
	}
	event := events[0]
	org := "org_1"
	for key, want := range map[string]any{
		"kind": "turn", "eventId": core.TurnKey(&org, "conv_1", "7"), "schemaVersion": float64(2), "source": "live",
		"orgId": "org_1", "userId": "user_1", "conversationId": "conv_1", "turnId": "7",
		"question": "Where is the order of [EMAIL_1]?", "answer": "It ships tomorrow.", "status": "completed",
		"apiKeyId": "key_9", "surface": "chat",
	} {
		if event[key] != want {
			t.Errorf("%s is %v, want %v", key, event[key], want)
		}
	}
	for key, want := range map[string]string{
		// The same address, so the same placeholder.
		"toolCalls": `[{"durationMs":41.5,"input":{"customer":"[EMAIL_1]"},"name":"search_orders","resultChars":120,"resultIsError":false},{"errorMessage":"not allowed","name":"refund","resultIsError":true}]`,
		"usage":     `{"costUsd":0.004,"inputTokens":812,"outputTokens":240}`,
		"latency":   `{"firstTokenMs":320}`,
		"client":    `{"name":"web","version":"3.1"}`,
		"metadata":  `{"plan":"pro","seats":3}`,
	} {
		if got, _ := json.Marshal(event[key]); string(got) != want {
			t.Errorf("%s is %s, want %s", key, got, want)
		}
	}
	valid(t, in)
}

// A turn is what the other SDKs' capture() takes, with the same names: what isn't set isn't
// there, and an empty list of tool calls says the agent called none.
func TestATurnIsWhatTheOtherSDKsTake(t *testing.T) {
	for _, test := range []struct {
		name string
		turn Turn
		want string
	}{
		{"nothing", Turn{}, `{}`},
		{"no tool called", Turn{ToolCalls: []ToolCall{}}, `{"toolCalls":[]}`},
		{"a client without a version", Turn{Client: "slack"}, `{"client":"slack"}`},
		{"a version without a client", Turn{ClientVersion: "1"}, `{}`},
		{"an error without a message", Turn{Status: StatusError, Error: &TurnError{}}, `{"status":"error","error":{"message":""}}`},
		{"zero numbers", Turn{Usage: &Usage{InputTokens: Ptr(0), CostUSD: Ptr(0.0)}}, `{"usage":{"inputTokens":0,"costUsd":0}}`},
		{"an empty usage", Turn{Usage: &Usage{}}, `{"usage":{}}`},
		{"IDs, in the order the builder reads them", Turn{TurnID: "1", CallID: "c", ToolName: "t", ConversationID: "v", UserID: "u", OrgID: "o"},
			`{"orgId":"o","userId":"u","conversationId":"v","toolName":"t","callId":"c","turnId":"1"}`},
		{"text that isn't UTF-8", Turn{Answer: "caf\xe9"}, `{"answer":"caf` + "�" + `"}`},
	} {
		if got := string(jsonv.Marshal(turnInput(&test.turn))); got != test.want {
			t.Errorf("%s: %s, want %s", test.name, got, test.want)
		}
	}
}

func TestATurnWithoutATurnIDHasARandomID(t *testing.T) {
	in := newIngestion(t)
	c := New(Options{APIKey: "lsq_test", Endpoint: in.URL, DryRun: DryRunOff})
	for range 2 {
		c.Capture(context.Background(), Turn{ConversationID: "conv_1", Question: "hi"})
		c.Capture(context.Background(), Turn{ConversationID: "conv_1", TurnID: "1"})
	}
	flush(t, c)
	events := in.events()
	if len(events) != 4 {
		t.Fatalf("%d events", len(events))
	}
	random, keyed := map[any]bool{}, map[any]bool{}
	for _, event := range events {
		if event["turnId"] == nil {
			random[event["eventId"]] = true
		} else {
			keyed[event["eventId"]] = true
		}
	}
	// The same turn twice is one event ID, sent twice, which the ingestion keeps once.
	if len(random) != 2 || len(keyed) != 1 || keyed[core.TurnKey(nil, "conv_1", "1")] != true {
		t.Errorf("event IDs: random %v, keyed %v", random, keyed)
	}
	valid(t, in)
}

// What the app does with what it gave Capture afterwards changes nothing.
func TestCaptureReadsTheTurnAsItIsCalled(t *testing.T) {
	in := newIngestion(t)
	c := New(Options{APIKey: "lsq_test", Endpoint: in.URL, DryRun: DryRunOff})
	turn := Turn{
		Metadata:  map[string]any{"plan": "pro"},
		ToolCalls: []ToolCall{{Name: "search", Input: json.RawMessage(`{"q":"before"}`), DurationMs: Ptr(1.0)}},
	}
	c.Capture(context.Background(), turn)
	turn.Metadata["plan"] = "free"
	copy(turn.ToolCalls[0].Input, `{"q":"after!"}`)
	*turn.ToolCalls[0].DurationMs = 99
	turn.ToolCalls[0].Name = "changed"
	flush(t, c)
	event := in.events()[0]
	if got, _ := json.Marshal(event["toolCalls"]); string(got) != `[{"durationMs":1,"input":{"q":"before"},"name":"search"}]` {
		t.Errorf("tool calls: %s", got)
	}
	if got, _ := json.Marshal(event["metadata"]); string(got) != `{"plan":"pro"}` {
		t.Errorf("metadata: %s", got)
	}
}

func TestATurnWithAnInvalidIDIsDropped(t *testing.T) {
	in := newIngestion(t)
	var p problems
	c := New(Options{APIKey: "lsq_test", Endpoint: in.URL, DryRun: DryRunOff, OnError: p.onError})
	c.Capture(context.Background(), Turn{UserID: "a\x00b", Question: "hi"})
	c.Capture(context.Background(), Turn{OrgID: strings.Repeat("o", 513)})
	c.Capture(context.Background(), Turn{ConversationID: "caf\xe9"})
	flush(t, c)
	if len(in.events()) != 0 {
		t.Errorf("sent %v", in.events())
	}
	if codes := p.codes(); strings.Join(codes, ",") != "invalid_id,invalid_id,invalid_id" {
		t.Errorf("reported %v", codes)
	}
}

// BeforeSend gets a turn's event with its turn, and may change what the turn says, not who or
// which turn it is.
func TestBeforeSendSeesATurnAndMayChangeIt(t *testing.T) {
	in := newIngestion(t)
	var mu sync.Mutex
	var saw []Event
	var turns []Turn
	c := New(Options{APIKey: "lsq_test", Endpoint: in.URL, DryRun: DryRunOff, BeforeSend: func(ctx context.Context, event *Event) (*Event, error) {
		mu.Lock()
		saw, turns = append(saw, *event), append(turns, *event.Turn)
		mu.Unlock()
		if event.Turn.Answer == "drop" {
			return nil, nil
		}
		event.Turn.Question = ""
		event.Turn.Answer = "Edited for bob@example.com."
		event.Turn.Status = StatusError
		event.Turn.Error = &TurnError{Message: "late", Type: "Timeout"}
		event.Turn.ToolCalls = nil
		event.Turn.OrgID, event.Turn.TurnID, event.OrgID, event.EventID = "other", "99", "other", "other"
		event.Metadata["plan"] = "edited"
		return event, nil
	}})
	c.Capture(context.Background(), fullTurn)
	c.Capture(context.Background(), Turn{Answer: "drop"})
	flush(t, c)
	// Hooks run side by side: the turn that isn't dropped.
	mu.Lock()
	first, turn := saw[0], turns[0]
	if turn.Answer == "drop" {
		first, turn = saw[1], turns[1]
	}
	mu.Unlock()
	if first.Kind != "turn" || first.Client != nil || first.Input != nil || first.Result != nil || turn.Client != "web" || turn.ClientVersion != "3.1" || turn.TurnID != "7" || turn.OrgID != "org_1" ||
		turn.Question != "Where is the order of alice@example.com?" || len(turn.ToolCalls) != 2 || *turn.Usage.InputTokens != 812 || string(turn.Latency) != `{"firstTokenMs":320}` || turn.Verification != nil {
		t.Errorf("the hook saw %+v and %+v", first, turn)
	}
	events := in.events()
	if len(events) != 1 {
		t.Fatalf("%d events", len(events))
	}
	event := events[0]
	org := "org_1"
	if event["question"] != nil || event["answer"] != "Edited for [EMAIL_1]." || event["status"] != "error" || event["toolCalls"] != nil ||
		event["orgId"] != "org_1" || event["turnId"] != "7" || event["eventId"] != core.TurnKey(&org, "conv_1", "7") || at(event, "metadata", "plan") != "edited" {
		t.Errorf("sent %v", event)
	}
	if got, _ := json.Marshal(event["error"]); string(got) != `{"message":"late","type":"Timeout"}` {
		t.Errorf("error: %s", got)
	}
	valid(t, in)
}

// A BeforeSend that captures a turn for each event it is given stops: a capture is refused past
// two levels of the client's own callbacks.
func TestCapturesFromHooksStop(t *testing.T) {
	in := newIngestion(t)
	var c *Client
	c = New(Options{APIKey: "lsq_test", Endpoint: in.URL, DryRun: DryRunOff, BeforeSend: func(ctx context.Context, event *Event) (*Event, error) {
		c.Capture(ctx, Turn{Question: "from the hook"})
		return event, nil
	}})
	c.Capture(context.Background(), Turn{Question: "from the app"})
	flush(t, c)
	flush(t, c)
	flush(t, c)
	if events := in.events(); len(events) != 3 {
		t.Errorf("%d events", len(events))
	}
	// Also two clients whose hooks capture on each other.
	var hooks atomic.Int32
	var a, b *Client
	hook := func(other **Client) func(context.Context, *Event) (*Event, error) {
		return func(ctx context.Context, event *Event) (*Event, error) {
			hooks.Add(1)
			(*other).Capture(ctx, Turn{Question: "from the other's hook"})
			return event, nil
		}
	}
	a = New(Options{APIKey: "lsq_test", Endpoint: in.URL, DryRun: DryRunOff, BeforeSend: hook(&b)})
	b = New(Options{APIKey: "lsq_test", Endpoint: in.URL, DryRun: DryRunOff, BeforeSend: hook(&a)})
	a.Capture(context.Background(), Turn{Question: "from the app"})
	for range 3 {
		flush(t, a)
		flush(t, b)
	}
	if hooks.Load() != 3 {
		t.Errorf("the two hooks ran %d times", hooks.Load())
	}
}

// A turn captured from OnError or the Logger is never recorded: each would make another report,
// or line, and its handler another turn.
func TestATurnCapturedFromOnErrorOrTheLoggerIsIgnored(t *testing.T) {
	in := newIngestion(t)
	var reports atomic.Int32
	var c *Client
	c = New(Options{APIKey: "lsq_test", Endpoint: in.URL, DryRun: DryRunOff, OnError: func(err *Error, event *Event) {
		reports.Add(1)
		// Slower than the reports' rate limit fills again.
		time.Sleep(15 * time.Millisecond)
		c.Capture(context.Background(), Turn{UserID: "a\x00b"})
	}})
	c.Capture(context.Background(), Turn{UserID: "a\x00b"})
	time.Sleep(200 * time.Millisecond)
	flush(t, c)
	if reports.Load() != 1 || len(in.events()) != 0 {
		t.Errorf("%d reports, %d events", reports.Load(), len(in.events()))
	}
	t.Setenv("LSQUARE_DRY_RUN", "1")
	var log capturing
	dry := New(Options{Logger: &log})
	log.client = dry
	dry.Capture(context.Background(), Turn{Question: "hi"})
	flush(t, dry)
	time.Sleep(50 * time.Millisecond)
	flush(t, dry)
	if lines := log.lines.Load(); lines != 1 {
		t.Errorf("%d lines", lines)
	}
}

// capturing is a Logger that captures a turn for each line it is given.
type capturing struct {
	client *Client
	lines  atomic.Int32
}

func (l *capturing) Info(message string) {
	l.lines.Add(1)
	l.client.Capture(context.Background(), Turn{Question: message})
}
func (l *capturing) Warn(message string) { l.Info(message) }

// Once Shutdown has begun, no turn is taken: what it waits for is the calls that had replied.
func TestATurnIsNotTakenOnceShutdownBegins(t *testing.T) {
	in := newIngestion(t)
	var p problems
	c := New(Options{APIKey: "lsq_test", Endpoint: in.URL, DryRun: DryRunOff, OnError: p.onError})
	done, _, ok := bridge.Of(c).Track("a call", 10)
	if !ok {
		t.Fatal("the call wasn't tracked")
	}
	ended := make(chan FlushResult)
	go func() { ended <- c.Shutdown(context.Background()) }()
	for !c.ending() {
		time.Sleep(time.Millisecond)
	}
	c.Capture(context.Background(), fullTurn)
	done()
	<-ended
	if len(in.events()) != 0 || strings.Join(p.codes(), ",") != "shutdown" {
		t.Errorf("sent %d events, reported %v", len(in.events()), p.codes())
	}
}

// A turn the client doesn't take is never read: it costs nothing.
func TestATurnTheClientDoesntTakeIsNeverRead(t *testing.T) {
	c := New(Options{APIKey: "lsq_test", Endpoint: "http://localhost:1", DryRun: DryRunOff})
	c.Shutdown(context.Background())
	calls := make([]ToolCall, 1_000)
	for i := range calls {
		calls[i] = ToolCall{Name: "search", Input: json.RawMessage(`{"q":"` + strings.Repeat("x", 100) + `"}`)}
	}
	turn := Turn{ToolCalls: calls, Metadata: map[string]any{"a": 1, "b": 2}}
	if allocations := testing.AllocsPerRun(20, func() { c.Capture(context.Background(), turn) }); allocations > 20 {
		t.Errorf("%v allocations for a turn refused", allocations)
	}
}

func TestTheEdgesOfATurn(t *testing.T) {
	in := newIngestion(t)
	c := New(Options{APIKey: "lsq_test", Endpoint: in.URL, DryRun: DryRunOff})
	calls := make([]ToolCall, 150)
	for i := range calls {
		calls[i] = ToolCall{Name: "search"}
	}
	// The 101st and those after it are never read.
	calls[100].Input = json.RawMessage(`[` + strings.Repeat(`1,`, 100_000) + `1]`)
	c.Capture(context.Background(), Turn{Question: "many", ToolCalls: calls})
	c.Capture(context.Background(), Turn{Question: "json", Verification: json.RawMessage(`{"passed":`), Latency: json.RawMessage(`null`)})
	c.Capture(context.Background(), Turn{Question: "id", ConversationID: "conv", TurnID: "a\xff"})
	c.Capture(context.Background(), Turn{Question: "id", ConversationID: "conv", TurnID: "a\uFFFD"})
	flush(t, c)
	byQuestion := map[string][]map[string]any{}
	for _, event := range in.events() {
		question, _ := event["question"].(string)
		byQuestion[question] = append(byQuestion[question], event)
	}
	if many := byQuestion["many"][0]; len(many["toolCalls"].([]any)) != 100 || asJSON(many["truncated"]) != `["toolCalls"]` {
		t.Errorf("150 tool calls: %d kept, truncated %v", len(many["toolCalls"].([]any)), many["truncated"])
	}
	// JSON that isn't is noted; a JSON null says nothing.
	if j := byQuestion["json"][0]; j["verification"] != nil || j["latency"] != nil || asJSON(j["dropped"]) != `{"verification":"not_json"}` {
		t.Errorf("JSON: %v", j)
	}
	// A turn ID that isn't text is left out, and the turn's ID is random: never another turn's.
	ids := byQuestion["id"]
	if len(ids) != 2 || ids[0]["eventId"] == ids[1]["eventId"] {
		t.Fatalf("turn IDs: %v", ids)
	}
	for _, event := range ids {
		if event["turnId"] == "a\uFFFD" {
			if event["eventId"] != core.TurnKey(nil, "conv", "a\uFFFD") {
				t.Errorf("a valid turn ID: %v", event)
			}
		} else if event["turnId"] != nil || asJSON(event["dropped"]) != `{"turnId":"wrong_type"}` || strings.HasPrefix(event["eventId"].(string), "k/") {
			t.Errorf("a turn ID that isn't text: %v", event)
		}
	}
	valid(t, in)
}

func asJSON(value any) string {
	out, _ := json.Marshal(value)
	return string(out)
}

// A named client stays named through a BeforeSend that changes nothing, also once its version
// was left out for its size.
func TestANamedClientStaysNamed(t *testing.T) {
	for _, hook := range []func(context.Context, *Event) (*Event, error){nil, func(ctx context.Context, event *Event) (*Event, error) { return event, nil }} {
		in := newIngestion(t)
		c := New(Options{APIKey: "lsq_test", Endpoint: in.URL, DryRun: DryRunOff, BeforeSend: hook})
		c.Capture(context.Background(), Turn{Client: "web", ClientVersion: strings.Repeat("v", 16_001)})
		c.Capture(context.Background(), Turn{Client: "slack"})
		flush(t, c)
		clients := map[string]bool{}
		for _, event := range in.events() {
			clients[asJSON(event["client"])] = true
		}
		if !clients[`{"name":"web"}`] || !clients[`"slack"`] || len(clients) != 2 {
			t.Errorf("hook %v: clients %v", hook != nil, clients)
		}
	}
}

func TestLargeCountsStayLarge(t *testing.T) {
	if intOf(float64(math.MaxInt)) != math.MaxInt || intOf(-float64(math.MaxInt)-1e6) != math.MinInt || intOf(812) != 812 {
		t.Errorf("%d %d %d", intOf(float64(math.MaxInt)), intOf(-float64(math.MaxInt)-1e6), intOf(812))
	}
}

func TestDryRunLabelsATurn(t *testing.T) {
	var log lines
	t.Setenv("LSQUARE_DRY_RUN", "1")
	c := New(Options{Logger: &log})
	c.Capture(context.Background(), fullTurn)
	c.Capture(context.Background(), Turn{Question: "hi"})
	c.Capture(context.Background(), Turn{ToolCalls: []ToolCall{}})
	flush(t, c)
	info, warns := log.all()
	if len(info) != 3 || len(warns) != 0 {
		t.Fatalf("logged %v and %v", info, warns)
	}
	for i, want := range []string{
		`[lsquare dry-run] {"kind":"turn","eventId":"from turnId, hashed","fields":["answer","apiKeyId","client","conversationId","latency","metadata","orgId","question","status","surface","toolCalls","turnId","usage","userId"],"bytes":`,
		`[lsquare dry-run] {"kind":"turn","eventId":"random: no turnId","fields":["question","status"],"bytes":`,
		`[lsquare dry-run] {"kind":"turn","eventId":"random: no turnId","fields":["status","toolCalls"],"bytes":`,
	} {
		if !strings.HasPrefix(info[i], want) {
			t.Errorf("logged %s", info[i])
		}
	}
	if !strings.Contains(info[0], `,"toolCalls":2,`) || !strings.Contains(info[2], `,"toolCalls":0`) || strings.Contains(info[1], "toolCalls") || strings.Contains(info[0], "alice") {
		t.Errorf("logged %v", info)
	}
}

func TestCaptureOnADisabledClientDoesNothing(t *testing.T) {
	t.Setenv("LSQUARE_DRY_RUN", "")
	var log lines
	c := New(Options{Logger: &log})
	c.Capture(context.Background(), fullTurn)
	//nolint:staticcheck // A nil context is taken as context.Background().
	c.Capture(nil, fullTurn)
	var none *Client
	none.Capture(context.Background(), fullTurn)
	if result := flush(t, c); result != (FlushResult{}) {
		t.Errorf("flush gave %+v", result)
	}
	if info, warns := log.all(); len(info) != 0 || len(warns) != 1 {
		t.Errorf("logged %v and %v", info, warns)
	}
}

func TestCaptureAfterShutdownIsReportedOnce(t *testing.T) {
	in := newIngestion(t)
	var p problems
	c := New(Options{APIKey: "lsq_test", Endpoint: in.URL, DryRun: DryRunOff, OnError: p.onError})
	c.Shutdown(context.Background())
	c.Capture(context.Background(), fullTurn)
	c.Capture(context.Background(), fullTurn)
	flush(t, c)
	if len(in.events()) != 0 || strings.Join(p.codes(), ",") != "shutdown" {
		t.Errorf("sent %d events, reported %v", len(in.events()), p.codes())
	}
}

// Identify's history keeps the answer that returned last, whatever order they are noted in.
func TestIdentifysHistoryKeepsTheLastToReturn(t *testing.T) {
	c := New(Options{APIKey: "lsq_test", Endpoint: "http://localhost:1", DryRun: DryRunOff})
	b := bridge.Of(c)
	if b.IdentifiedKeys() != nil {
		t.Fatal("history before any answer")
	}
	now := time.Now()
	b.Identified([]string{"orgId", "userId"}, now)
	b.Identified([]string{"userId"}, now.Add(-time.Millisecond))
	if got := strings.Join(b.IdentifiedKeys(), ","); got != "orgId,userId" {
		t.Errorf("history %s", got)
	}
	b.Identified(nil, now.Add(time.Millisecond))
	if got := b.IdentifiedKeys(); got == nil || len(got) != 0 {
		t.Errorf("an empty answer: %v", got)
	}
}

// A Flush marks a tool's call only from the app's own code: never from a callback of a client's.
func TestOnlyTheAppsOwnFlushMarksACall(t *testing.T) {
	c := New(Options{APIKey: "lsq_test", Endpoint: "http://localhost:1", DryRun: DryRunOff})
	other := New(Options{APIKey: "lsq_test", Endpoint: "http://localhost:1", DryRun: DryRunOff})
	for _, test := range []struct {
		name  string
		ctx   func(*bridge.Call) context.Context
		marks bool
	}{
		{"the tool's own", func(call *bridge.Call) context.Context { return bridge.WithCall(context.Background(), c.owner, call) }, true},
		{"another client's callback", func(call *bridge.Call) context.Context {
			return bridge.WithCall(bridge.MarkCallback(context.Background(), other), c.owner, call)
		}, false},
		{"another client's call", func(call *bridge.Call) context.Context {
			return bridge.WithCall(context.Background(), other.owner, call)
		}, false},
	} {
		call := bridge.NewCall("a call")
		ctx, cancel := context.WithTimeout(test.ctx(call), time.Second)
		c.Flush(ctx)
		cancel()
		if got := call.Close().Flushed; got != test.marks {
			t.Errorf("%s: marked %v", test.name, got)
		}
	}
	// An OnError or a Logger that was given a tool's context it kept, running as a report is
	// delivered on this goroutine: no call's reply is made to wait for a reporter.
	call := bridge.NewCall("a call")
	handling.Store(goroutineID(), struct{}{})
	handlers.Add(1)
	ctx, cancel := context.WithTimeout(bridge.WithCall(context.Background(), c.owner, call), time.Second)
	c.Flush(ctx)
	cancel()
	handlers.Add(-1)
	handling.Delete(goroutineID())
	if call.Close().Flushed {
		t.Error("a reporter's Flush marked the call")
	}
}

// panicking is a context whose cancellation methods panic.
type panicking struct{ context.Context }

func (panicking) Done() <-chan struct{} { panic("Done") }
func (panicking) Err() error            { panic("Err") }

// A context whose methods panic, given to the waits, neither leaves the client's lock unlocked
// nor ends the process: the waits run to their deadline instead.
func TestAContextThatPanicsLeavesTheClientWhole(t *testing.T) {
	c := New(Options{APIKey: "lsq_test", Endpoint: "http://localhost:1", DryRun: DryRunOff, FlushTimeout: 60 * time.Millisecond})
	b := bridge.Of(c)
	release, tracked, ok := b.Track("a call", 10)
	if !ok {
		t.Fatal("the call wasn't tracked")
	}
	started := time.Now()
	b.Settle(panicking{context.Background()}, tracked, nil, started)
	if waited := time.Since(started); waited < 50*time.Millisecond {
		t.Errorf("the reply waited %v for a build still under way", waited)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Millisecond)
	defer cancel()
	result := c.Flush(panicking{ctx})
	if result.Pending != 1 {
		t.Errorf("flush: %+v", result)
	}
	release()
	// The lock is the client's as it was: taken and given back.
	c.mu.Lock()
	c.mu.Unlock()
	c.Shutdown(panicking{ctx})
}
