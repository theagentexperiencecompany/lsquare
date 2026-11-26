package lsquare

import (
	"context"
	"encoding/json"
	"math"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/theagentexperiencecompany/lsquare/packages/go/internal/bridge"
	"github.com/theagentexperiencecompany/lsquare/packages/go/internal/core"
	"github.com/theagentexperiencecompany/lsquare/packages/go/internal/jsonv"
)

// Turn is one turn of an agent: what it was asked, what it answered, the tools it called and what
// it cost. Every field is optional: a string left empty, and a nil pointer, slice or map, are left
// out of the event.
//
// The same IDs as Identify's: OrgID, UserID and ConversationID must be internal IDs, not email
// addresses, as events can be deleted by them. Each is at most 512 bytes. A turn with a
// ConversationID and a TurnID gets the same event ID every time it is captured, so a retry or a
// re-import is recognised as a duplicate.
type Turn struct {
	OrgID          string
	UserID         string
	ConversationID string
	// TurnID is the turn's own ID within the conversation. A number is sent as its decimal text,
	// as the other SDKs send one: "7" and 7 are the same turn.
	TurnID string
	// ToolName is the tool the turn is about, when it is about one.
	ToolName string
	// CallID links the turn to a tool call recorded by an instrumented server, by that call's
	// event ID (Event.EventID, as BeforeSend and OnError show it): for a turn captured after the
	// call, in a background job say.
	CallID string

	Question string
	Answer   string
	// Status is StatusCompleted or StatusError; empty is StatusCompleted.
	Status string
	Error  *TurnError
	// ToolCalls are the tools the agent called in the turn. Nil: not recorded; an empty slice:
	// the agent called none.
	ToolCalls []ToolCall
	Usage     *Usage
	// Latency and Verification are JSON of your own, as you'd have them in the dashboard.
	Latency      json.RawMessage
	Verification json.RawMessage
	// Client is the app the person used, such as "web" or "slack", and ClientVersion its version.
	Client        string
	ClientVersion string
	// APIKeyID is the ID of the API key the turn ran under, never the key.
	APIKeyID string
	Surface  string
	// Metadata is recorded with the event: strings, numbers, booleans and nil. In BeforeSend and
	// OnError it is nil: a turn's metadata is the event's, in Event.Metadata.
	Metadata map[string]any

	// The event has the client as {"name": ...}, though without a version: so it stays, through a
	// BeforeSend that changes nothing.
	named bool
}

// A turn's status.
const (
	StatusCompleted = "completed"
	StatusError     = "error"
)

// TurnError is how a turn failed. Type is the error's kind, such as the name of its type.
type TurnError struct {
	Message string
	Type    string
}

// ToolCall is one tool an agent called in a turn. Its result is never recorded, only its size.
type ToolCall struct {
	Name string
	// Input is what the tool was given, as JSON.
	Input         json.RawMessage
	ResultChars   *int
	ResultIsError *bool
	ErrorMessage  string
	DurationMs    *float64
}

// Usage is what a turn cost.
type Usage struct {
	InputTokens  *int
	OutputTokens *int
	CostUSD      *float64
}

// Ptr is a pointer to v, for the optional numbers of a Turn: Usage{InputTokens: lsquare.Ptr(812)}.
func Ptr[T any](v T) *T { return &v }

// The most a capture can be nested in the clients' own callbacks: a BeforeSend that captures a
// turn for each event it is given would otherwise never stop.
const maxCallbackDepth = 2

// Capture records an agent turn. It reads the turn as it is called, so what you change in it
// afterwards changes nothing, and it never waits on the network: the turn is sent with the
// client's other events.
//
// ctx is the context the turn is captured in. Inside a tool on a server instrumented with this
// client, pass the tool's own: the first capture before the tool returns is then the call's
// agent part, and any other a turn linked to the call. The agent part is read as Capture is
// called, as the call's event will hold it; one from another goroutine that is still being read
// as the tool returns is a turn of its own. One a callback of the client's was given
// (a BeforeSend's) makes the turn one of the client's own events, and a hook that captures a
// turn for each event it gets stops two levels down.
func (c *Client) Capture(ctx context.Context, turn Turn) {
	if c == nil || c.mode == modeDisabled {
		return
	}
	defer func() {
		if recover() != nil {
			c.report(&Error{Code: CodeInternal, Message: "Capture failed unexpectedly; the event was dropped."}, nil)
		}
	}()
	capturedAt := time.Now().UTC().Format("2006-01-02T15:04:05.000Z")
	if ctx == nil {
		ctx = context.Background()
	}
	// Captured from the callbacks of clients, nested too far (a hook that captures from each event
	// it is given), or from an OnError or a Logger, where each turn would make another report.
	depth := bridge.CallbackDepth(ctx)
	if depth > maxCallbackDepth || inAnyHandler() {
		return
	}
	// Inside a tool call on a server instrumented with this client, from the app's own code: the
	// first capture before the tool returns is the call's agent part; any other is a turn of its
	// own, linked to the call.
	if call := bridge.CallOf(ctx, c.owner); call != nil && depth == 0 && !c.ending() {
		if call.Claim(func() *bridge.Agent { return c.readAgent(&turn) }) {
			return
		}
		if turn.CallID == "" {
			turn.CallID = call.ID
		}
		// Before the tool returns, its sending is the call's too: a reply that waits for the
		// call's event waits for it.
		if e := c.enqueueTurn(func() *jsonv.Object { return turnInput(&turn) }, capturedAt, depth); e != nil {
			call.Follow(e, c.finishedEntry)
		}
		return
	}
	c.enqueueTurn(func() *jsonv.Object { return turnInput(&turn) }, capturedAt, depth)
}

// CurrentCallID is the ID of the tool call ctx was given to, on a server instrumented with this
// client: "" outside one. A turn captured later, in a background job say, links itself to the call
// with it, as its CallID.
func (c *Client) CurrentCallID(ctx context.Context) string {
	if c == nil {
		return ""
	}
	if call := bridge.CallOf(ctx, c.owner); call != nil {
		return call.ID
	}
	return ""
}

// readAgent reads a capture as a call's agent part: shaped then and there, as the call's event
// will hold it, and its IDs as given, each in a string of its own. An ID longer than a valid one
// keeps one byte past it, which is just as invalid.
func (c *Client) readAgent(turn *Turn) *bridge.Agent {
	ids := jsonv.NewObject()
	for _, id := range []struct{ key, value string }{{"orgId", turn.OrgID}, {"userId", turn.UserID}, {"conversationId", turn.ConversationID}} {
		if id.value != "" {
			ids.Set(id.key, strings.Clone(id.value[:min(len(id.value), core.MaxIDBytes+1)]))
		}
	}
	return &bridge.Agent{IDs: ids, Shaped: core.ShapeAgent(turnInput(turn), c.context)}
}

// turnInput is a turn as the event's builder reads one: the field names the other SDKs' capture
// takes, with only what is set. Content is made valid UTF-8 as JSON writes it; an ID isn't, so
// that one which isn't valid is refused rather than read as another.
func turnInput(turn *Turn) *jsonv.Object {
	out := jsonv.NewObject()
	for _, id := range []struct{ key, value string }{
		{"orgId", turn.OrgID}, {"userId", turn.UserID}, {"conversationId", turn.ConversationID},
		{"toolName", turn.ToolName}, {"callId", turn.CallID}, {"turnId", turn.TurnID},
	} {
		if id.value != "" {
			out.Set(id.key, id.value)
		}
	}
	if !utf8.ValidString(turn.TurnID) {
		// Not text: read as it is, "a\xff" and "a\uFFFD" would be the same turn. It is left out,
		// and noted as of the wrong type, as a turn ID of no use is in the other SDKs.
		out.Set("turnId", false)
	}
	turnContent(out, turn)
	if turn.Metadata != nil {
		out.Set("metadata", jsonv.ObjectFromMap(turn.Metadata))
	}
	return out
}

// turnContent sets what a turn says, as opposed to who and what it is: what BeforeSend may change.
func turnContent(out *jsonv.Object, turn *Turn) {
	text := func(key, value string) {
		if value != "" {
			out.Set(key, jsonv.ValidString(value))
		}
	}
	text("question", turn.Question)
	text("answer", turn.Answer)
	text("status", turn.Status)
	if turn.Error != nil {
		failure := jsonv.NewObject()
		failure.Set("message", jsonv.ValidString(turn.Error.Message))
		if turn.Error.Type != "" {
			failure.Set("type", jsonv.ValidString(turn.Error.Type))
		}
		out.Set("error", failure)
	}
	if turn.ToolCalls != nil {
		// Only as many as an event keeps, and one more to say there were more: the rest is never
		// read.
		kept := turn.ToolCalls[:min(len(turn.ToolCalls), core.MaxEntries)]
		calls := make([]any, len(kept), len(kept)+1)
		if len(turn.ToolCalls) > len(kept) {
			calls = append(calls, jsonv.NewObject())
		}
		for i, call := range kept {
			item := jsonv.NewObject()
			item.Set("name", jsonv.ValidString(call.Name))
			if call.Input != nil {
				item.Set("input", rawValue(call.Input))
			}
			if call.ResultChars != nil {
				item.Set("resultChars", jsonv.Int(int64(*call.ResultChars)))
			}
			if call.ResultIsError != nil {
				item.Set("resultIsError", *call.ResultIsError)
			}
			if call.ErrorMessage != "" {
				item.Set("errorMessage", jsonv.ValidString(call.ErrorMessage))
			}
			if call.DurationMs != nil {
				item.Set("durationMs", number(*call.DurationMs))
			}
			calls[i] = item
		}
		out.Set("toolCalls", calls)
	}
	if usage := turn.Usage; usage != nil {
		cost := jsonv.NewObject()
		if usage.InputTokens != nil {
			cost.Set("inputTokens", jsonv.Int(int64(*usage.InputTokens)))
		}
		if usage.OutputTokens != nil {
			cost.Set("outputTokens", jsonv.Int(int64(*usage.OutputTokens)))
		}
		if usage.CostUSD != nil {
			cost.Set("costUsd", number(*usage.CostUSD))
		}
		out.Set("usage", cost)
	}
	if turn.Latency != nil {
		out.Set("latency", rawValue(turn.Latency))
	}
	if turn.Verification != nil {
		out.Set("verification", rawValue(turn.Verification))
	}
	if turn.Client != "" {
		if turn.ClientVersion != "" || turn.named {
			named := jsonv.NewObject()
			named.Set("name", jsonv.ValidString(turn.Client))
			if turn.ClientVersion != "" {
				named.Set("version", jsonv.ValidString(turn.ClientVersion))
			}
			out.Set("client", named)
		} else {
			out.Set("client", jsonv.ValidString(turn.Client))
		}
	}
	text("apiKeyId", turn.APIKeyID)
	text("surface", turn.Surface)
}

// number is a float as JSON holds it: one that JSON can't hold (infinite, or not a number) is
// left out of the event, and noted in dropped.
func number(f float64) jsonv.Value {
	if n, ok := jsonv.Float(f); ok {
		return n
	}
	return jsonv.NotJSON{}
}

// publicTurn is a turn event's turn, as BeforeSend and OnError get it.
func publicTurn(event *jsonv.Object) *Turn {
	out := &Turn{
		OrgID: textOf(event, "orgId"), UserID: textOf(event, "userId"), ConversationID: textOf(event, "conversationId"),
		TurnID: textOf(event, "turnId"), ToolName: textOf(event, "toolName"), CallID: textOf(event, "callId"),
		Question: textOf(event, "question"), Answer: textOf(event, "answer"), Status: textOf(event, "status"),
		APIKeyID: textOf(event, "apiKeyId"), Surface: textOf(event, "surface"),
	}
	if value, _ := event.Get("error"); value != nil {
		if failure, ok := value.(*jsonv.Object); ok {
			out.Error = &TurnError{Message: textOf(failure, "message"), Type: textOf(failure, "type")}
		}
	}
	if value, ok := event.Get("toolCalls"); ok {
		list, _ := value.([]any)
		out.ToolCalls = make([]ToolCall, 0, len(list))
		for _, item := range list {
			call, isObject := item.(*jsonv.Object)
			if !isObject {
				continue
			}
			public := ToolCall{Name: textOf(call, "name"), ErrorMessage: textOf(call, "errorMessage")}
			if input, has := call.Get("input"); has {
				public.Input = json.RawMessage(jsonv.Marshal(input))
			}
			if n, isNumber := numberOf(call, "resultChars"); isNumber {
				public.ResultChars = Ptr(intOf(n))
			}
			if flag, has := call.Get("resultIsError"); has {
				if b, isBool := flag.(bool); isBool {
					public.ResultIsError = Ptr(b)
				}
			}
			if n, isNumber := numberOf(call, "durationMs"); isNumber {
				public.DurationMs = Ptr(n)
			}
			out.ToolCalls = append(out.ToolCalls, public)
		}
	}
	if value, _ := event.Get("usage"); value != nil {
		if cost, ok := value.(*jsonv.Object); ok {
			out.Usage = &Usage{}
			if n, isNumber := numberOf(cost, "inputTokens"); isNumber {
				out.Usage.InputTokens = Ptr(intOf(n))
			}
			if n, isNumber := numberOf(cost, "outputTokens"); isNumber {
				out.Usage.OutputTokens = Ptr(intOf(n))
			}
			if n, isNumber := numberOf(cost, "costUsd"); isNumber {
				out.Usage.CostUSD = Ptr(n)
			}
		}
	}
	for _, field := range []struct {
		key  string
		into *json.RawMessage
	}{{"latency", &out.Latency}, {"verification", &out.Verification}} {
		if value, ok := event.Get(field.key); ok {
			*field.into = json.RawMessage(jsonv.Marshal(value))
		}
	}
	switch client, _ := event.Get("client"); named := client.(type) {
	case string:
		out.Client = named
	case *jsonv.Object:
		out.Client, out.ClientVersion, out.named = textOf(named, "name"), textOf(named, "version"), true
	}
	return out
}

// intOf is a count as an int, the largest one there is for one past it: a float of the largest
// int is past it, by one, and converting it is undefined.
func intOf(f float64) int {
	switch {
	case f >= float64(math.MaxInt):
		return math.MaxInt
	case f <= float64(math.MinInt):
		return math.MinInt
	}
	return int(f)
}

func numberOf(object *jsonv.Object, key string) (float64, bool) {
	value, _ := object.Get(key)
	n, ok := value.(jsonv.Num)
	if !ok {
		return 0, false
	}
	return n.Float64(), true
}

// hookTurnOutput is what BeforeSend returned of a turn event, as the fields the SDK reads back
// from it: the turn's content and the event's metadata. Its IDs are the event's, as they were.
func hookTurnOutput(event *Event) *jsonv.Object {
	out := jsonv.NewObject()
	if event.Turn != nil {
		turnContent(out, event.Turn)
	}
	if event.Metadata != nil {
		out.Set("metadata", metadataObject(event.Metadata, event.metadataOrder))
	}
	return out
}
