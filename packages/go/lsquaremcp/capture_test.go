package lsquaremcp

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	lsquare "github.com/theagentexperiencecompany/lsquare/packages/go"
	"github.com/theagentexperiencecompany/lsquare/packages/go/internal/core"
)

type askInput struct {
	Question string `json:"question,omitempty"`
}

// agentServer is a server with one tool, ask, which runs capture with the client and the tool's
// own context, and replies with the call's ID as the tool saw it.
func agentServer(t *testing.T, protocol string, options *Options, before lsquare.Options, capture func(client *lsquare.Client, ctx context.Context)) *harness {
	t.Helper()
	return newHarnessWith(t, before, &mcp.ClientSessionOptions{ProtocolVersion: protocol}, func(h *harness) {
		mcp.AddTool(h.server, &mcp.Tool{Name: "ask", Description: "Asks the agent."}, func(ctx context.Context, request *mcp.CallToolRequest, input askInput) (*mcp.CallToolResult, any, error) {
			capture(h.client, ctx)
			return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: h.client.CurrentCallID(ctx)}}}, nil, nil
		})
		Instrument(h.server, h.client, options)
	})
}

// byKind splits events into the call's and the turns.
func byKind(events []map[string]any) (calls, turns []map[string]any) {
	for _, event := range events {
		if event["kind"] == "turn" {
			turns = append(turns, event)
		} else {
			calls = append(calls, event)
		}
	}
	return calls, turns
}

func TestTheFirstTurnCapturedInsideACallIsItsAgent(t *testing.T) {
	var later sync.WaitGroup
	var kept context.Context
	h := agentServer(t, modern, nil, lsquare.Options{}, func(client *lsquare.Client, ctx context.Context) {
		client.Capture(ctx, lsquare.Turn{
			OrgID: "org_1", UserID: "user_1", ConversationID: "conv_1", TurnID: "3",
			Question: "Where is my order?", Answer: "It ships tomorrow.",
			ToolCalls: []lsquare.ToolCall{{Name: "search_orders", DurationMs: lsquare.Ptr(12.0)}},
			Metadata:  map[string]any{"plan": "pro"},
		})
		// A second, before the tool returns: a turn of its own, linked to the call.
		client.Capture(ctx, lsquare.Turn{Question: "a follow-up"})
		kept = ctx
		later.Add(1)
	})
	result, err := h.call("ask", raw(`{"question":"where"}`))
	if err != nil || result.IsError {
		t.Fatalf("%v %v", err, result)
	}
	callID := texts(result)[0]
	// After the tool returned, from work it left: a turn of its own too.
	go func() {
		defer later.Done()
		h.client.Capture(kept, lsquare.Turn{Question: "from a background job"})
	}()
	later.Wait()
	calls, turns := byKind(h.events())
	if len(calls) != 1 || len(turns) != 2 {
		t.Fatalf("%d calls, %d turns", len(calls), len(turns))
	}
	event := calls[0]
	if event["callId"] != callID || callID == "" {
		t.Errorf("the tool saw call %q, the event is %v", callID, event["callId"])
	}
	if event["orgId"] != "org_1" || event["userId"] != "user_1" || event["conversationId"] != "conv_1" || at(event, "metadata", "plan") != "pro" {
		t.Errorf("IDs and metadata: %v", event)
	}
	org := "org_1"
	if want := coreTurnKey(&org, "conv_1", "3"); event["turnRef"] != want {
		t.Errorf("turnRef %v, want %s", event["turnRef"], want)
	}
	if got := asJSON(event["agent"]); got != `{"answer":"It ships tomorrow.","question":"Where is my order?","status":"completed","toolCalls":[{"durationMs":12,"name":"search_orders"}],"turnId":"3"}` {
		t.Errorf("agent: %s", got)
	}
	// The conversation groups the call: it is given no task ID.
	if issued(result) != "" || event["sessionId"] != nil {
		t.Errorf("a session: %v, %v", texts(result), event["sessionId"])
	}
	for _, turn := range turns {
		if turn["callId"] != callID {
			t.Errorf("a turn isn't linked to the call: %v", turn)
		}
	}
}

func coreTurnKey(org *string, conversation, turn string) string {
	ref, _ := core.TurnRef(org, conversation, turn)
	return ref
}

// The capture's IDs win over Identify's, each by itself, and its metadata wins key by key.
func TestACapturesIDsWinOverIdentifys(t *testing.T) {
	h := agentServer(t, modern, &Options{Identify: func(ctx context.Context, call Call) (*Identity, error) {
		return &Identity{OrgID: "org_identify", UserID: "user_identify", ConversationID: "conv_identify", Metadata: map[string]any{"plan": "free", "region": "eu"}}, nil
	}}, lsquare.Options{}, func(client *lsquare.Client, ctx context.Context) {
		client.Capture(ctx, lsquare.Turn{UserID: "user_capture", Metadata: map[string]any{"plan": "pro"}, TurnID: "1"})
	})
	if _, err := h.call("ask", raw(`{}`)); err != nil {
		t.Fatal(err)
	}
	event := h.one()
	if event["orgId"] != "org_identify" || event["userId"] != "user_capture" || event["conversationId"] != "conv_identify" ||
		asJSON(event["metadata"]) != `{"plan":"pro","region":"eu"}` {
		t.Errorf("IDs and metadata: %v", event)
	}
	// Identify's conversation isn't the turn's: no reference without the capture's own.
	if event["turnRef"] != nil {
		t.Errorf("turnRef %v", event["turnRef"])
	}
}

// A failed Identify sends the call as limited diagnostics, unless the capture inside it gave each
// ID Identify last returned: a userId, before any answer.
func TestAFailedIdentifyAndTheCapturesIDs(t *testing.T) {
	var mu sync.Mutex
	fail := false
	identify := func(ctx context.Context, call Call) (*Identity, error) {
		mu.Lock()
		defer mu.Unlock()
		if fail {
			return nil, errors.New("down")
		}
		return &Identity{OrgID: "org_1", UserID: "user_1"}, nil
	}
	turn := lsquare.Turn{}
	h := agentServer(t, modern, &Options{Identify: identify}, lsquare.Options{}, func(client *lsquare.Client, ctx context.Context) {
		mu.Lock()
		captured := turn
		mu.Unlock()
		client.Capture(ctx, captured)
	})
	set := func(failing bool, captured lsquare.Turn) {
		mu.Lock()
		fail, turn = failing, captured
		mu.Unlock()
	}
	// Before any answer, a userId is enough.
	set(true, lsquare.Turn{UserID: "user_2", Answer: "kept"})
	if _, err := h.call("ask", raw(`{}`)); err != nil {
		t.Fatal(err)
	}
	if event := h.latest(); event["userId"] != "user_2" || at(event, "agent", "answer") != "kept" {
		t.Errorf("before any answer, with a userId: %v", event)
	}
	// Once Identify has answered with an org and a user, a capture needs both.
	set(false, lsquare.Turn{})
	if _, err := h.call("ask", raw(`{}`)); err != nil {
		t.Fatal(err)
	}
	h.latest()
	set(true, lsquare.Turn{UserID: "user_2", Answer: "dropped"})
	if _, err := h.call("ask", raw(`{}`)); err != nil {
		t.Fatal(err)
	}
	if event := h.latest(); event["userId"] != nil || event["agent"] != nil {
		t.Errorf("without the org: %v", event)
	}
	set(true, lsquare.Turn{OrgID: "org_2", UserID: "user_2", Answer: "kept"})
	if _, err := h.call("ask", raw(`{}`)); err != nil {
		t.Fatal(err)
	}
	if event := h.latest(); event["orgId"] != "org_2" || at(event, "agent", "answer") != "kept" {
		t.Errorf("with both: %v", event)
	}
	reported := strings.Join(h.reported(), "\n")
	if !strings.Contains(reported, "the call was sent with Capture's IDs.") || !strings.Contains(reported, "the call was sent as limited diagnostics.") {
		t.Errorf("reported:\n%s", reported)
	}
}

// A turn captured inside a call on a connection with a session groups the call by its
// conversation, instead of the connection's session.
func TestACapturedConversationGroupsTheCall(t *testing.T) {
	h := agentServer(t, legacy, nil, lsquare.Options{}, func(client *lsquare.Client, ctx context.Context) {
		client.Capture(ctx, lsquare.Turn{ConversationID: "conv_1"})
	})
	if _, err := h.call("ask", raw(`{}`)); err != nil {
		t.Fatal(err)
	}
	if event := h.one(); event["sessionId"] != nil || event["conversationId"] != "conv_1" {
		t.Errorf("session %v, conversation %v", event["sessionId"], event["conversationId"])
	}
	// One the agent sends still wins.
	sent := "ses_" + strings.Repeat("c", 22)
	h2 := agentServer(t, legacy, nil, lsquare.Options{}, func(client *lsquare.Client, ctx context.Context) {
		client.Capture(ctx, lsquare.Turn{ConversationID: "conv_1"})
	})
	if _, err := h2.call("ask", raw(`{"session_id":"`+sent+`"}`)); err != nil {
		t.Fatal(err)
	}
	if event := h2.one(); event["sessionId"] != sent || event["sessionSource"] != "sent" {
		t.Errorf("session %v (%v)", event["sessionId"], event["sessionSource"])
	}
}

// An empty capture is still the call's agent part; one on another client is a turn of that
// client's, not linked to the call; and outside a call, there is no call ID.
func TestOtherCapturesInsideACall(t *testing.T) {
	ingestion := newHarness(t, nil, lsquare.Options{})
	other := ingestion.client
	h := agentServer(t, modern, nil, lsquare.Options{}, func(client *lsquare.Client, ctx context.Context) {
		client.Capture(ctx, lsquare.Turn{})
		other.Capture(ctx, lsquare.Turn{Question: "on another client"})
		if other.CurrentCallID(ctx) != "" {
			t.Error("another client sees the call")
		}
	})
	if _, err := h.call("ask", raw(`{}`)); err != nil {
		t.Fatal(err)
	}
	if event := h.one(); asJSON(event["agent"]) != `{"status":"completed"}` {
		t.Errorf("agent %s", asJSON(event["agent"]))
	}
	if turn := ingestion.one(); turn["kind"] != "turn" || turn["callId"] != nil {
		t.Errorf("the other client's turn: %v", turn)
	}
	if h.client.CurrentCallID(context.Background()) != "" {
		t.Error("a call ID outside a call")
	}
}

// Identify and BeforeSend run outside the call: a capture from them is a turn of its own.
func TestCapturesFromCallbacksAreNotTheCalls(t *testing.T) {
	var h *harness
	h = newHarnessWith(t, lsquare.Options{BeforeSend: func(ctx context.Context, event *lsquare.Event) (*lsquare.Event, error) {
		if event.Kind == "tool_call" {
			h.client.Capture(ctx, lsquare.Turn{Question: "from BeforeSend"})
		}
		return event, nil
	}}, nil, func(h2 *harness) {
		mcp.AddTool(h2.server, &mcp.Tool{Name: "ask"}, func(ctx context.Context, request *mcp.CallToolRequest, input askInput) (*mcp.CallToolResult, any, error) {
			return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: "ok"}}}, nil, nil
		})
		Instrument(h2.server, h2.client, &Options{Identify: func(ctx context.Context, call Call) (*Identity, error) {
			h.client.Capture(ctx, lsquare.Turn{Question: "from Identify"})
			return nil, nil
		}})
	})
	if _, err := h.call("ask", raw(`{}`)); err != nil {
		t.Fatal(err)
	}
	h.events()
	calls, turns := byKind(h.events())
	if len(calls) != 1 || calls[0]["agent"] != nil || len(turns) != 2 {
		t.Fatalf("%d calls (agent %v), %d turns", len(calls), calls[0]["agent"], len(turns))
	}
	for _, turn := range turns {
		if turn["callId"] != nil {
			t.Errorf("a turn from a callback is linked: %v", turn)
		}
	}
}

// What Identify last returned in time is the client's, whichever server it answered on, and a
// failed Identify keeps the call whole only with a capture that gives each of those IDs.
func TestIdentifysHistoryIsTheClients(t *testing.T) {
	var mu sync.Mutex
	fail := false
	identify := func(ctx context.Context, call Call) (*Identity, error) {
		mu.Lock()
		defer mu.Unlock()
		if fail {
			return nil, errors.New("down")
		}
		return &Identity{OrgID: "org_1", UserID: "user_1"}, nil
	}
	turn := lsquare.Turn{UserID: "user_2", Answer: "private"}
	first := agentServer(t, modern, &Options{Identify: identify}, lsquare.Options{}, func(client *lsquare.Client, ctx context.Context) {})
	if _, err := first.call("ask", raw(`{}`)); err != nil {
		t.Fatal(err)
	}
	first.events()
	// Another server, instrumented with the same client.
	second := newHarnessWith(t, lsquare.Options{}, &mcp.ClientSessionOptions{ProtocolVersion: modern}, func(h *harness) {
		h.client = first.client
		mcp.AddTool(h.server, &mcp.Tool{Name: "ask"}, func(ctx context.Context, request *mcp.CallToolRequest, input askInput) (*mcp.CallToolResult, any, error) {
			h.client.Capture(ctx, turn)
			return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: "ok"}}}, nil, nil
		})
		Instrument(h.server, h.client, &Options{Identify: identify})
	})
	mu.Lock()
	fail = true
	mu.Unlock()
	if _, err := second.call("ask", raw(`{}`)); err != nil {
		t.Fatal(err)
	}
	// The first server's harness has the client's ingestion.
	if event := first.latest(); event["agent"] != nil || event["userId"] != nil {
		t.Errorf("a capture without the org Identify last returned: %v", event)
	}
}

// A failed Identify with no capture inside the call is limited diagnostics, whatever Identify
// returned before: even nothing.
func TestAFailedIdentifyWithoutACaptureIsLimited(t *testing.T) {
	var mu sync.Mutex
	fail := false
	h := agentServer(t, modern, &Options{Identify: func(ctx context.Context, call Call) (*Identity, error) {
		mu.Lock()
		defer mu.Unlock()
		if fail {
			return nil, errors.New("down")
		}
		return nil, nil
	}}, lsquare.Options{}, func(client *lsquare.Client, ctx context.Context) {})
	if _, err := h.call("ask", raw(`{"question":"first"}`)); err != nil {
		t.Fatal(err)
	}
	h.latest()
	mu.Lock()
	fail = true
	mu.Unlock()
	if _, err := h.call("ask", raw(`{"question":"private"}`)); err != nil {
		t.Fatal(err)
	}
	if event := h.latest(); event["input"] != nil || event["identity"] != "failed" {
		t.Errorf("sent %v", event)
	}
}

// BeforeSend gets a call's agent part and its reference, and a hook that changes nothing keeps
// them; one may change what the agent says, or take it out.
func TestBeforeSendKeepsTheCallsAgent(t *testing.T) {
	var mu sync.Mutex
	edit := ""
	var saw []lsquare.Event
	var agents []lsquare.Turn
	h := agentServer(t, modern, nil, lsquare.Options{BeforeSend: func(ctx context.Context, event *lsquare.Event) (*lsquare.Event, error) {
		mu.Lock()
		defer mu.Unlock()
		saw = append(saw, *event)
		if event.Agent != nil {
			agents = append(agents, *event.Agent)
		}
		switch edit {
		case "change":
			event.Agent.Answer = "Changed for bob@example.com."
			event.Agent.TurnID = "99"
			event.TurnRef = "k/" + strings.Repeat("0", 64)
		case "remove":
			event.Agent = nil
		}
		return event, nil
	}}, func(client *lsquare.Client, ctx context.Context) {
		client.Capture(ctx, lsquare.Turn{ConversationID: "conv_1", TurnID: "3", Question: "q", Answer: "a", Metadata: map[string]any{"plan": "pro"}})
	})
	set := func(to string) {
		mu.Lock()
		edit = to
		mu.Unlock()
	}
	ref := coreTurnKey(nil, "conv_1", "3")
	for _, step := range []struct {
		edit, agent string
	}{
		{"", `{"answer":"a","question":"q","status":"completed","turnId":"3"}`},
		{"change", `{"answer":"Changed for [EMAIL_1].","question":"q","status":"completed","turnId":"3"}`},
		{"remove", `null`},
	} {
		set(step.edit)
		if _, err := h.call("ask", raw(`{}`)); err != nil {
			t.Fatal(err)
		}
		event := h.latest()
		if got := asJSON(event["agent"]); got != step.agent || event["turnRef"] != ref || at(event, "metadata", "plan") != "pro" {
			t.Errorf("%q: agent %s, turnRef %v, metadata %v", step.edit, got, event["turnRef"], event["metadata"])
		}
	}
	mu.Lock()
	defer mu.Unlock()
	if len(agents) != 3 || agents[0].TurnID != "3" || agents[0].Answer != "a" || saw[0].TurnRef != ref || saw[0].Turn != nil {
		t.Errorf("the hook saw %+v, %+v", saw[0], agents)
	}
}

// Merged metadata is in JavaScript's order, as the other SDKs merge it: numbered the same.
func TestMergedMetadataIsInJavaScriptsOrder(t *testing.T) {
	for _, hook := range []func(context.Context, *lsquare.Event) (*lsquare.Event, error){nil, func(ctx context.Context, event *lsquare.Event) (*lsquare.Event, error) { return event, nil }} {
		h := agentServer(t, modern, &Options{Identify: func(ctx context.Context, call Call) (*Identity, error) {
			return &Identity{Metadata: map[string]any{"10": "ten@example.com", "z": "zed@example.com"}}, nil
		}}, lsquare.Options{BeforeSend: hook}, func(client *lsquare.Client, ctx context.Context) {
			client.Capture(ctx, lsquare.Turn{Metadata: map[string]any{"2": "two@example.com", "a": "ay@example.com"}})
		})
		if _, err := h.call("ask", raw(`{}`)); err != nil {
			t.Fatal(err)
		}
		// Sent, then read as it was sent: the order of its keys is what is compared.
		if events := h.events(); len(events) != 1 {
			t.Fatalf("%d events", len(events))
		}
		h.mu.Lock()
		sent := string(h.raw[0])
		h.mu.Unlock()
		if !strings.Contains(sent, `"metadata":{"2":"[EMAIL_1]","10":"[EMAIL_2]","z":"[EMAIL_3]","a":"[EMAIL_4]"}`) {
			t.Errorf("hook %v: %s", hook != nil, sent)
		}
	}
}

// sentSoFar is how many events the ingestion has, without flushing.
func (h *harness) sentSoFar() int {
	h.mu.Lock()
	defer h.mu.Unlock()
	return len(h.raw)
}

// slowly is a BeforeSend that takes its time: an event is sent that long after it is recorded.
func slowly(wait time.Duration) func(context.Context, *lsquare.Event) (*lsquare.Event, error) {
	return func(ctx context.Context, event *lsquare.Event) (*lsquare.Event, error) {
		time.Sleep(wait)
		return event, nil
	}
}

// A Flush inside a tool makes its reply wait for the call's event, and for the turns captured
// inside the call; a reply without one doesn't wait.
func TestAFlushInsideAToolSendsItsEventBeforeTheReply(t *testing.T) {
	flush := true
	var mu sync.Mutex
	h := agentServer(t, modern, nil, lsquare.Options{BeforeSend: slowly(150 * time.Millisecond)}, func(client *lsquare.Client, ctx context.Context) {
		client.Capture(ctx, lsquare.Turn{Question: "the agent part"})
		client.Capture(ctx, lsquare.Turn{Question: "a turn of its own"})
		mu.Lock()
		defer mu.Unlock()
		if flush {
			client.Flush(ctx)
		}
	})
	began := time.Now()
	if _, err := h.call("ask", raw(`{}`)); err != nil {
		t.Fatal(err)
	}
	if waited, sent := time.Since(began), h.sentSoFar(); sent != 2 || waited < 150*time.Millisecond {
		t.Errorf("the reply came after %v, with %d events sent", waited, sent)
	}
	h.events()
	mu.Lock()
	flush = false
	mu.Unlock()
	before := h.sentSoFar()
	began = time.Now()
	if _, err := h.call("ask", raw(`{}`)); err != nil {
		t.Fatal(err)
	}
	if waited, sent := time.Since(began), h.sentSoFar(); sent != before || waited > 100*time.Millisecond {
		t.Errorf("without Flush, the reply came after %v, with %d events sent", waited, sent-before)
	}
	h.events()
}

// FlushBeforeReply makes every reply wait for its event, up to the client's FlushTimeout.
func TestFlushBeforeReply(t *testing.T) {
	h := agentServer(t, modern, &Options{FlushBeforeReply: true}, lsquare.Options{BeforeSend: slowly(150 * time.Millisecond)}, func(client *lsquare.Client, ctx context.Context) {})
	began := time.Now()
	if _, err := h.call("ask", raw(`{}`)); err != nil {
		t.Fatal(err)
	}
	if waited, sent := time.Since(began), h.sentSoFar(); sent != 1 || waited < 150*time.Millisecond {
		t.Errorf("the reply came after %v, with %d events sent", waited, sent)
	}
	// No longer than FlushTimeout: the event goes on being sent after the reply.
	slow := agentServer(t, modern, &Options{FlushBeforeReply: true}, lsquare.Options{FlushTimeout: 200 * time.Millisecond, BeforeSendTimeout: 5 * time.Second, BeforeSend: slowly(time.Second)}, func(client *lsquare.Client, ctx context.Context) {})
	began = time.Now()
	if _, err := slow.call("ask", raw(`{}`)); err != nil {
		t.Fatal(err)
	}
	if waited := time.Since(began); waited < 200*time.Millisecond || waited > 700*time.Millisecond {
		t.Errorf("the reply came after %v", waited)
	}
	if slow.sentSoFar() != 0 {
		t.Error("the event was sent before its hook returned")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	slow.client.Flush(ctx)
	if slow.sentSoFar() != 1 {
		t.Errorf("%d events sent after the reply", slow.sentSoFar())
	}
	// A negative FlushTimeout waits for nothing.
	none := agentServer(t, modern, &Options{FlushBeforeReply: true}, lsquare.Options{FlushTimeout: -1, BeforeSend: slowly(300 * time.Millisecond)}, func(client *lsquare.Client, ctx context.Context) {})
	began = time.Now()
	if _, err := none.call("ask", raw(`{}`)); err != nil {
		t.Fatal(err)
	}
	if waited := time.Since(began); waited > 150*time.Millisecond {
		t.Errorf("with no FlushTimeout, the reply came after %v", waited)
	}
	h.events()
	none.client.Flush(ctx)
}

// An Identify that answers after its deadline teaches the client nothing: its answer doesn't
// count for its own call, and doesn't count for others.
func TestALateIdentifyIsNotNoted(t *testing.T) {
	var mu sync.Mutex
	mode := "on time"
	identify := func(ctx context.Context, call Call) (*Identity, error) {
		mu.Lock()
		current := mode
		mu.Unlock()
		switch current {
		case "late":
			time.Sleep(300 * time.Millisecond)
			return nil, nil
		case "fail":
			return nil, errors.New("down")
		}
		return &Identity{OrgID: "org_1", UserID: "user_1"}, nil
	}
	// With the reply waiting at most 100 ms, Identify's second is cut to that.
	h := agentServer(t, modern, &Options{Identify: identify, FlushBeforeReply: true}, lsquare.Options{FlushTimeout: 100 * time.Millisecond}, func(client *lsquare.Client, ctx context.Context) {
		client.Capture(ctx, lsquare.Turn{Answer: "private"})
	})
	set := func(to string) {
		mu.Lock()
		mode = to
		mu.Unlock()
	}
	for _, step := range []string{"on time", "late"} {
		set(step)
		if _, err := h.call("ask", raw(`{}`)); err != nil {
			t.Fatal(err)
		}
	}
	// Let the late answer come.
	time.Sleep(400 * time.Millisecond)
	set("fail")
	if _, err := h.call("ask", raw(`{"question":"private"}`)); err != nil {
		t.Fatal(err)
	}
	events := h.events()
	if last := events[len(events)-1]; last["agent"] != nil || last["input"] != nil || last["identity"] != "failed" {
		t.Errorf("after a late answer with no IDs, a failed Identify sent %v", last)
	}
}

// The reply waits for the turns captured inside the call that are still being sent, however many
// were captured and sent before.
func TestTheReplyWaitsForTheTurnsStillBeingSent(t *testing.T) {
	h := agentServer(t, modern, nil, lsquare.Options{BeforeSend: func(ctx context.Context, event *lsquare.Event) (*lsquare.Event, error) {
		if event.Turn != nil && event.Turn.Question == "the last" {
			time.Sleep(200 * time.Millisecond)
		}
		return event, nil
	}}, func(client *lsquare.Client, ctx context.Context) {
		client.Capture(ctx, lsquare.Turn{Question: "the agent part"})
		for range 100 {
			client.Capture(ctx, lsquare.Turn{Question: "one of many"})
		}
		// Sends the hundred, and makes the reply wait.
		client.Flush(ctx)
		client.Capture(ctx, lsquare.Turn{Question: "the last"})
	})
	if _, err := h.call("ask", raw(`{}`)); err != nil {
		t.Fatal(err)
	}
	if sent := h.sentSoFar(); sent != 102 {
		t.Errorf("%d events sent before the reply", sent)
	}
}

// A tool that panics: the panic goes on, once the event the reply was to wait for is sent.
func TestAPanickingToolsEventIsSentFirst(t *testing.T) {
	h := newHarnessWith(t, lsquare.Options{BeforeSend: slowly(150 * time.Millisecond)}, &mcp.ClientSessionOptions{ProtocolVersion: modern}, func(h *harness) {
		mcp.AddTool(h.server, &mcp.Tool{Name: "boom"}, func(ctx context.Context, request *mcp.CallToolRequest, input askInput) (*mcp.CallToolResult, any, error) {
			panic("boom")
		})
		Instrument(h.server, h.client, &Options{FlushBeforeReply: true})
		// The app's own, outside the wrapper's: a panic becomes an error reply.
		h.server.AddReceivingMiddleware(func(next mcp.MethodHandler) mcp.MethodHandler {
			return func(ctx context.Context, method string, request mcp.Request) (result mcp.Result, err error) {
				defer func() {
					if recover() != nil {
						result, err = nil, errors.New("recovered")
					}
				}()
				return next(ctx, method, request)
			}
		})
	})
	if _, err := h.call("boom", raw(`{}`)); err == nil {
		t.Fatal("no error")
	}
	if sent := h.sentSoFar(); sent != 1 {
		t.Errorf("%d events sent before the reply", sent)
	}
	if event := h.one(); at(event, "error", "type") != "panic" {
		t.Errorf("sent %v", event)
	}
}

// The turn captured inside a call counts towards the queue as the event will hold it: a long
// answer is cut as it is captured, and a call whose event fits fits, however long the answer was.
func TestALongCapturedAnswerFitsASmallQueue(t *testing.T) {
	answer := strings.Repeat("word ", 22_400)
	h := agentServer(t, modern, nil, lsquare.Options{MaxQueueBytes: 50_000}, func(client *lsquare.Client, ctx context.Context) {
		client.Capture(ctx, lsquare.Turn{UserID: "user_1", Question: "Where?", Answer: answer})
	})
	if result, err := h.call("ask", raw(`{"question":"where"}`)); err != nil || result.IsError {
		t.Fatalf("%v %v", err, result)
	}
	calls, turns := byKind(h.events())
	if len(calls) != 1 || len(turns) != 0 || len(h.reported()) != 0 {
		t.Fatalf("calls %d, turns %d, reported %v", len(calls), len(turns), h.reported())
	}
	agent, _ := calls[0]["agent"].(map[string]any)
	kept, _ := agent["answer"].(string)
	if kept != strings.Repeat("word ", 3_200)+"…[+96000 chars]" {
		t.Errorf("the answer as sent: %d bytes, ending %q", len(kept), kept[max(len(kept)-20, 0):])
	}
}
