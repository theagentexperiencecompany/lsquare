package lsquare

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/http/cookiejar"
	"net/http/httptest"
	"net/url"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/theagentexperiencecompany/lsquare/packages/go/internal/bridge"
	"github.com/theagentexperiencecompany/lsquare/packages/go/internal/core"
	"github.com/theagentexperiencecompany/lsquare/packages/go/internal/jsonv"
)

// ingestion is a stand-in for LSquare's: it keeps what it was sent, and answers as told, or by
// accepting every event.
type ingestion struct {
	*httptest.Server
	mu       sync.Mutex
	requests []received
	answer   func(n int, events []map[string]any) (status int, header http.Header, body string)
}

type received struct {
	path          string
	authorization string
	contentType   string
	events        []map[string]any
}

func newIngestion(t *testing.T) *ingestion {
	t.Helper()
	in := &ingestion{}
	in.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		var envelope struct {
			Events []map[string]any `json:"events"`
		}
		_ = json.Unmarshal(body, &envelope)
		in.mu.Lock()
		in.requests = append(in.requests, received{r.URL.Path, r.Header.Get("Authorization"), r.Header.Get("Content-Type"), envelope.Events})
		n := len(in.requests)
		answer := in.answer
		in.mu.Unlock()
		if answer == nil {
			_, _ = w.Write([]byte(accept(envelope.Events)))
			return
		}
		status, header, reply := answer(n, envelope.Events)
		for key, values := range header {
			w.Header()[key] = values
		}
		w.WriteHeader(status)
		_, _ = w.Write([]byte(reply))
	}))
	t.Cleanup(in.Close)
	return in
}

func accept(events []map[string]any) string {
	ids := make([]string, len(events))
	for i, event := range events {
		ids[i] = fmt.Sprint(event["eventId"])
	}
	out, _ := json.Marshal(map[string]any{"accepted": ids, "duplicates": []string{}, "rejected": []any{}})
	return string(out)
}

func (in *ingestion) received() []received {
	in.mu.Lock()
	defer in.mu.Unlock()
	return append([]received(nil), in.requests...)
}

func (in *ingestion) events() []map[string]any {
	var events []map[string]any
	for _, request := range in.received() {
		events = append(events, request.events...)
	}
	return events
}

// problems collects what a client reports.
type problems struct {
	mu     sync.Mutex
	errors []*Error
	events []*Event
}

func (p *problems) onError(err *Error, event *Event) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.errors = append(p.errors, err)
	p.events = append(p.events, event)
}

func (p *problems) codes() []string {
	p.mu.Lock()
	defer p.mu.Unlock()
	codes := make([]string, len(p.errors))
	for i, err := range p.errors {
		codes[i] = string(err.Code)
	}
	return codes
}

func (p *problems) first(t *testing.T) *Error {
	t.Helper()
	p.mu.Lock()
	defer p.mu.Unlock()
	if len(p.errors) == 0 {
		t.Fatal("nothing was reported")
	}
	return p.errors[0]
}

// lines collects what a client logs.
type lines struct {
	mu    sync.Mutex
	info  []string
	warns []string
}

func (l *lines) Info(message string) { l.mu.Lock(); l.info = append(l.info, message); l.mu.Unlock() }
func (l *lines) Warn(message string) { l.mu.Lock(); l.warns = append(l.warns, message); l.mu.Unlock() }
func (l *lines) all() (info, warns []string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	return append([]string(nil), l.info...), append([]string(nil), l.warns...)
}

var callNumber atomic.Int64

// call is a call's record, as the wrapper hands one to the client.
func call(input, text string) *jsonv.Object {
	record := jsonv.NewObject()
	record.Set("callId", core.NewUUID())
	record.Set("capturedAt", "2026-11-24T10:00:00.000Z")
	record.Set("toolName", "search_orders")
	record.Set("status", "completed")
	record.Set("durationMs", jsonv.Int(callNumber.Add(1)))
	ids := jsonv.NewObject()
	ids.Set("userId", "user_1")
	record.Set("ids", ids)
	arguments := jsonv.NewObject()
	arguments.Set("query", input)
	holder := jsonv.NewObject()
	holder.Set("value", arguments)
	record.Set("input", holder)
	block := jsonv.NewObject()
	block.Set("type", "text")
	block.Set("text", text)
	result := jsonv.NewObject()
	result.Set("content", []any{block})
	holder = jsonv.NewObject()
	holder.Set("value", result)
	record.Set("result", holder)
	record.Set("diagnostics", false)
	return record
}

func record(c *Client, input, text string) string {
	r := call(input, text)
	id, _ := fieldText(r, "callId")
	bridge.Of(c).Record(r, nil)
	return id
}

func flush(t *testing.T, c *Client) FlushResult {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	return c.Flush(ctx)
}

func TestSendsAnEvent(t *testing.T) {
	in := newIngestion(t)
	c := New(Options{APIKey: " lsq_test ", Endpoint: in.URL + "/", DryRun: DryRunOff})
	id := record(c, "orders of alice@example.com", "3 orders")
	// The flush counts the event if it was still on its way, so only what must be zero is checked.
	if result := flush(t, c); result.Failed != 0 || result.Pending != 0 {
		t.Fatalf("flush gave %+v", result)
	}
	requests := in.received()
	if len(requests) != 1 || len(requests[0].events) != 1 {
		t.Fatalf("got %+v", requests)
	}
	request := requests[0]
	if request.path != "/v1/turns" || request.authorization != "Bearer lsq_test" || request.contentType != "application/json" {
		t.Errorf("request to %s with %q, %q", request.path, request.authorization, request.contentType)
	}
	event := request.events[0]
	sdk, _ := event["sdk"].(map[string]any)
	if event["eventId"] != id || event["callId"] != id || event["kind"] != "tool_call" || event["schemaVersion"] != float64(2) || event["source"] != "live" ||
		event["userId"] != "user_1" || sdk["name"] != "lsquare" || sdk["language"] != "go" || sdk["version"] != Version || !strings.HasPrefix(fmt.Sprint(sdk["runtime"]), "go/1.") {
		t.Errorf("the event is %v", event)
	}
	if input, _ := event["input"].(map[string]any); input["query"] != "orders of [EMAIL_1]" {
		t.Errorf("the input is %v", event["input"])
	}
	if redactions, _ := event["redactions"].(map[string]any); redactions["email"] != float64(1) {
		t.Errorf("redactions are %v", event["redactions"])
	}
}

func TestBatchesWithinTheLimits(t *testing.T) {
	in := newIngestion(t)
	release := make(chan struct{})
	in.answer = func(n int, events []map[string]any) (int, http.Header, string) {
		if n == 1 {
			<-release
		}
		return 200, nil, accept(events)
	}
	c := New(Options{APIKey: "k", Endpoint: in.URL, DryRun: DryRunOff, MaxConcurrentRequests: 1})
	for i := 0; i < 250; i++ {
		record(c, fmt.Sprint(i), "ok")
	}
	close(release)
	// The flush counts what was still on its way: how many that is depends on how far sending got.
	if result := flush(t, c); result.Pending != 0 || result.Failed != 0 {
		t.Fatalf("flush gave %+v", result)
	}
	total := 0
	for i, request := range in.received() {
		if len(request.events) > core.MaxBatchEvents {
			t.Errorf("request %d carries %d events", i, len(request.events))
		}
		total += len(request.events)
	}
	// The first request takes what was ready when it left, the others up to 100 each.
	if requests := in.received(); total != 250 || len(requests) < 3 || len(requests) > 4 {
		t.Errorf("%d events in %d requests; want 250 in 3 or 4", total, len(requests))
	}
}

func TestRetriesAndGivesUp(t *testing.T) {
	for _, test := range []struct {
		name     string
		statuses []int
		header   http.Header
		sent     bool
		requests int
		message  string
	}{
		{"a 500 then a 200", []int{500, 200}, nil, true, 2, ""},
		{"a 429 with Retry-After", []int{429, 200}, http.Header{"Retry-After": {"0"}}, true, 2, ""},
		{"three 503s", []int{503, 503, 503}, nil, false, 3, "LSquare answered 503 after 3 attempts."},
		{"a 400", []int{400}, nil, false, 1, "LSquare answered 400; not retried."},
		{"a redirect, never followed", []int{302, 302, 302}, http.Header{"Location": {"http://127.0.0.1:1/elsewhere"}}, false, 3, "Network error after 3 attempts."},
	} {
		t.Run(test.name, func(t *testing.T) {
			in := newIngestion(t)
			in.answer = func(n int, events []map[string]any) (int, http.Header, string) {
				status := test.statuses[min(n, len(test.statuses))-1]
				if status == 200 {
					return 200, nil, accept(events)
				}
				return status, test.header, "no"
			}
			var reported problems
			c := New(Options{APIKey: "k", Endpoint: in.URL, DryRun: DryRunOff, OnError: reported.onError})
			record(c, "q", "ok")
			if result := flush(t, c); result.Pending != 0 || (test.sent && result.Failed != 0) || (!test.sent && result.Sent != 0) {
				t.Errorf("flush gave %+v", result)
			}
			if got := len(in.received()); got != test.requests {
				t.Errorf("%d requests, want %d", got, test.requests)
			}
			if test.message == "" {
				if codes := reported.codes(); len(codes) != 0 {
					t.Errorf("reported %v", codes)
				}
				return
			}
			if err := reported.first(t); err.Code != CodeSendFailed || err.Message != test.message || err.EventID == "" {
				t.Errorf("reported %+v", err)
			}
		})
	}
}

func TestReadsEachEventsAcknowledgement(t *testing.T) {
	in := newIngestion(t)
	// Each event is answered by what its query asks for, whichever request it comes in.
	in.answer = func(n int, events []map[string]any) (int, http.Header, string) {
		accepted, duplicates, rejected := []string{}, []string{}, []map[string]string{}
		for _, event := range events {
			id := fmt.Sprint(event["eventId"])
			input, _ := event["input"].(map[string]any)
			switch input["query"] {
			case "accepted":
				accepted = append(accepted, id)
			case "duplicate":
				duplicates = append(duplicates, id)
			case "invalid":
				rejected = append(rejected, map[string]string{"eventId": id, "reason": "invalid_field"})
			case "new reason":
				rejected = append(rejected, map[string]string{"eventId": id, "reason": "something_new"})
			case "both":
				accepted = append(accepted, id)
				rejected = append(rejected, map[string]string{"eventId": id, "reason": "invalid_field"})
			}
		}
		out, _ := json.Marshal(map[string]any{"accepted": accepted, "duplicates": duplicates, "rejected": rejected})
		return 200, nil, string(out)
	}
	var reported problems
	c := New(Options{APIKey: "k", Endpoint: in.URL, DryRun: DryRunOff, OnError: reported.onError})
	ids := map[string]string{}
	for _, query := range []string{"accepted", "duplicate", "invalid", "new reason", "both", "not named"} {
		ids[query] = record(c, query, "ok")
	}
	if result := flush(t, c); result.Pending != 0 {
		t.Fatalf("flush gave %+v", result)
	}
	messages := map[string]string{}
	reported.mu.Lock()
	for _, err := range reported.errors {
		messages[err.EventID] = string(err.Code) + ": " + err.Message
	}
	reported.mu.Unlock()
	for query, want := range map[string]string{
		"invalid":    "rejected: LSquare rejected the event: invalid_field.",
		"new reason": "rejected: LSquare rejected the event: unspecified.",
		"both":       "send_failed: Unexpected response: LSquare did not acknowledge the event.",
		"not named":  "send_failed: Unexpected response: LSquare did not acknowledge the event.",
	} {
		if got := messages[ids[query]]; got != want {
			t.Errorf("%s: reported %q, want %q", query, got, want)
		}
	}
	if len(messages) != 4 {
		t.Errorf("reported %v", messages)
	}
}

func TestBeforeSend(t *testing.T) {
	in := newIngestion(t)
	var reported problems
	c := New(Options{APIKey: "k", Endpoint: in.URL, DryRun: DryRunOff, OnError: reported.onError, BeforeSendTimeout: 300 * time.Millisecond,
		BeforeSend: func(ctx context.Context, event *Event) (*Event, error) {
			var input struct{ Query string }
			_ = json.Unmarshal(event.Input, &input)
			switch input.Query {
			case "drop":
				return nil, nil
			case "fail":
				return nil, errors.New("no")
			case "panic":
				panic("no")
			case "slow":
				<-ctx.Done()
				return event, nil
			}
			// The hook sees the input before redaction, and what it adds is redacted like the rest.
			if input.Query != "orders of alice@example.com" || *event.Result.Content[0].Text != "3 orders" || event.UserID != "user_1" {
				return nil, fmt.Errorf("the hook got %+v", event)
			}
			event.Input = json.RawMessage(`{"query":"rewritten for bob@example.com","password":"hunter2"}`)
			event.Result = nil
			event.Metadata = map[string]any{"plan": "pro", "seats": 3, "bad": []string{"x"}}
			event.UserID = "someone_else"
			event.ToolName = "another_tool"
			return event, nil
		}})
	kept := record(c, "orders of alice@example.com", "3 orders")
	for _, query := range []string{"drop", "fail", "panic", "slow"} {
		record(c, query, "ok")
	}
	// The flush counts what was outstanding when it was called: the slow one for sure, and any
	// other whose hook hadn't answered yet.
	if result := flush(t, c); result.Pending != 0 || result.Failed < 1 || result.Failed > 3 || result.Sent > 1 || result.Skipped > 1 {
		t.Fatalf("flush gave %+v", result)
	}
	events := in.events()
	if len(events) != 1 {
		t.Fatalf("%d events were sent", len(events))
	}
	event := events[0]
	input, _ := event["input"].(map[string]any)
	if event["eventId"] != kept || event["userId"] != "user_1" || event["toolName"] != "search_orders" {
		t.Errorf("the hook changed what it can't: %v", event)
	}
	if input["query"] != "rewritten for [EMAIL_1]" || input["password"] != "[SECRET_1]" {
		t.Errorf("the hook's input wasn't redacted: %v", input)
	}
	if _, has := event["result"]; has {
		t.Errorf("the result the hook removed is there: %v", event["result"])
	}
	metadata, _ := event["metadata"].(map[string]any)
	dropped, _ := event["dropped"].(map[string]any)
	if metadata["plan"] != "pro" || metadata["seats"] != float64(3) || len(metadata) != 2 || dropped["beforeSend.metadata"] != "not_json" {
		t.Errorf("metadata is %v, dropped %v", metadata, dropped)
	}
	codes := reported.codes()
	if strings.Join(codes, ",") != "hook_failed,hook_failed,hook_timeout" && strings.Join(codes, ",") != "hook_failed,hook_timeout,hook_failed" {
		// Two fail at once, the slow one at its deadline.
		t.Errorf("reported %v", codes)
	}
}

func TestDryRunLogsAndSendsNothing(t *testing.T) {
	in := newIngestion(t)
	var log lines
	t.Setenv("LSQUARE_DRY_RUN", "1")
	c := New(Options{Endpoint: in.URL, Logger: &log})
	record(c, "orders of alice@example.com", "3 orders")
	// The event was finished as it was recorded: the flush only waits for its line to be logged.
	if result := flush(t, c); result != (FlushResult{}) {
		t.Fatalf("flush gave %+v", result)
	}
	info, warns := log.all()
	if len(in.received()) != 0 || len(warns) != 0 || len(info) != 1 {
		t.Fatalf("sent %d requests, logged %v and %v", len(in.received()), info, warns)
	}
	want := `[lsquare dry-run] {"kind":"tool_call","eventId":"the call","fields":["callId","durationMs","input","result","status","toolName","userId"],"bytes":`
	if !strings.HasPrefix(info[0], want) || !strings.HasSuffix(info[0], `,"redactions":{"email":1}}`) || strings.Contains(info[0], "alice") {
		t.Errorf("logged %s", info[0])
	}
	// Explicitly off, the environment's dry run doesn't hold.
	if sending := New(Options{APIKey: "k", Endpoint: in.URL, DryRun: DryRunOff}); sending.mode != modeSend {
		t.Error("DryRunOff didn't override LSQUARE_DRY_RUN")
	}
}

func TestDisabledWithoutAKeyOrAUsableEndpoint(t *testing.T) {
	t.Setenv("LSQUARE_DRY_RUN", "")
	t.Setenv("LSQUARE_ENDPOINT", "")
	for _, test := range []struct {
		options Options
		warning string
	}{
		{Options{}, "[lsquare] No APIKey: capture is disabled."},
		{Options{APIKey: "k", Endpoint: "http://example.com"}, "[lsquare] The endpoint must use https (http only for localhost): capture is disabled."},
		{Options{APIKey: "k", Endpoint: "ingest.example.com"}, "[lsquare] The endpoint is not a valid URL: capture is disabled."},
	} {
		var log lines
		test.options.Logger = &log
		c := New(test.options)
		record(c, "q", "ok")
		if result := flush(t, c); result != (FlushResult{}) {
			t.Errorf("%+v: flush gave %+v", test.options, result)
		}
		c.Shutdown(context.Background())
		if _, warns := log.all(); len(warns) != 1 || warns[0] != test.warning {
			t.Errorf("warned %v, want %q", warns, test.warning)
		}
	}
	for endpoint, want := range map[string]string{
		"https://ingest.example.com":      "https://ingest.example.com/v1/turns",
		"https://ingest.example.com/eu//": "https://ingest.example.com/eu/v1/turns",
		"http://localhost:8787":           "http://localhost:8787/v1/turns",
		"http://127.0.0.1:8787/":          "http://127.0.0.1:8787/v1/turns",
		"http://[::1]:8787":               "http://[::1]:8787/v1/turns",
		// As a browser would resolve it: the host in lowercase, no dot segments.
		"http://LOCALHOST:8080":              "http://localhost:8080/v1/turns",
		"HTTPS://Ingest.Example.COM/a/../b/": "https://ingest.example.com/b/v1/turns",
		// An empty segment is kept, a dot segment applied however it is written, and the scheme's
		// own port left out.
		"https://example.com/a//b":           "https://example.com/a//b/v1/turns",
		"https://example.com/a/%2E%2e/b/./":  "https://example.com/b/v1/turns",
		"https://example.com:443/x":          "https://example.com/x/v1/turns",
		"https://example.com:8443":           "https://example.com:8443/v1/turns",
		"http://localhost:80/":               "http://localhost/v1/turns",
		"https://user:pw@example.com/?q=1#f": "https://example.com/v1/turns",
		"https://[2001:DB8::1]/x":            "https://[2001:db8::1]/x/v1/turns",
	} {
		if got, problem := resolveEndpoint(endpoint); got != want || problem != "" {
			t.Errorf("%s resolves to %q (%s), want %q", endpoint, got, problem, want)
		}
	}
}

func TestEndpointsThatArentURLsAreRefused(t *testing.T) {
	for _, endpoint := range []string{"https://:443", "https://ingest.example.com:99999", "https://", "mailto:ops@example.com", "",
		// In brackets there is an IPv6 address, without a zone, or nothing a request can be made for;
		// and an IPv6 address is in brackets.
		"https://[example.test]:443/path", "https://[fe80::1%25en0]/x", "https://[127.0.0.1]/",
		"https://2001:db8::1:443/path", "http://::1:8080/test"} {
		if got, problem := resolveEndpoint(endpoint); problem != "The endpoint is not a valid URL" {
			t.Errorf("%q resolves to %q (%s)", endpoint, got, problem)
		}
	}
	if _, problem := resolveEndpoint("ftp://ingest.example.com"); problem != "The endpoint must use https (http only for localhost)" {
		t.Errorf("ftp: %s", problem)
	}
}

func TestTheQueueDropsItsOldestWhenFull(t *testing.T) {
	in := newIngestion(t)
	release := make(chan struct{})
	in.answer = func(n int, events []map[string]any) (int, http.Header, string) {
		<-release
		return 200, nil, accept(events)
	}
	var reported problems
	c := New(Options{APIKey: "k", Endpoint: in.URL, DryRun: DryRunOff, OnError: reported.onError, MaxQueueEvents: 3, MaxConcurrentRequests: 1})
	first := record(c, "1", "ok")
	for len(in.received()) == 0 {
		time.Sleep(time.Millisecond)
	}
	second := record(c, "2", "ok")
	record(c, "3", "ok")
	record(c, "4", "ok")
	// The first is in flight, so the second, the oldest that isn't, went.
	deadline := time.Now().Add(2 * time.Second)
	for len(reported.codes()) == 0 && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	if err := reported.first(t); err.Code != CodeQueueFull || err.EventID != second || err.Message != "Dropped to stay within the memory limits." {
		t.Errorf("reported %+v; the first was %s", err, first)
	}
	close(release)
	if result := flush(t, c); result.Pending != 0 || result.Failed != 0 || len(in.events()) != 3 {
		t.Errorf("flush gave %+v, and %d events were sent", result, len(in.events()))
	}
}

func TestShutdownDropsWhatComesLater(t *testing.T) {
	in := newIngestion(t)
	var reported problems
	c := New(Options{APIKey: "k", Endpoint: in.URL, DryRun: DryRunOff, OnError: reported.onError})
	record(c, "1", "ok")
	if result := c.Shutdown(context.Background()); result.Failed != 0 || result.Pending != 0 {
		t.Fatalf("shutdown gave %+v", result)
	}
	record(c, "2", "ok")
	record(c, "3", "ok")
	c.Shutdown(context.Background())
	if codes := reported.codes(); len(codes) != 1 || codes[0] != "shutdown" {
		t.Errorf("reported %v, want shutdown once", codes)
	}
	if got := len(in.events()); got != 1 {
		t.Errorf("%d events were sent", got)
	}
}

func TestShutdownGivesUpAtItsDeadline(t *testing.T) {
	in := newIngestion(t)
	release := make(chan struct{})
	defer close(release)
	in.answer = func(n int, events []map[string]any) (int, http.Header, string) {
		<-release
		return 200, nil, accept(events)
	}
	var reported problems
	c := New(Options{APIKey: "k", Endpoint: in.URL, DryRun: DryRunOff, OnError: reported.onError})
	record(c, "1", "ok")
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	started := time.Now()
	if result := c.Shutdown(ctx); result != (FlushResult{Failed: 1}) {
		t.Errorf("shutdown gave %+v", result)
	}
	if took := time.Since(started); took > 2*time.Second {
		t.Errorf("shutdown took %v", took)
	}
	if err := reported.first(t); err.Code != CodeShutdown || err.Message != "Shutdown ended before the event was sent." {
		t.Errorf("reported %+v", err)
	}
}

func TestFlushReportsWhatIsStillPending(t *testing.T) {
	in := newIngestion(t)
	release := make(chan struct{})
	in.answer = func(n int, events []map[string]any) (int, http.Header, string) {
		<-release
		return 200, nil, accept(events)
	}
	var reported problems
	c := New(Options{APIKey: "k", Endpoint: in.URL, DryRun: DryRunOff, OnError: reported.onError, FlushTimeout: 50 * time.Millisecond})
	id := record(c, "q for alice@example.com", "ok")
	if result := c.Flush(context.Background()); result != (FlushResult{Pending: 1}) {
		t.Errorf("flush gave %+v", result)
	}
	err := reported.first(t)
	if err.Code != CodePendingAfterFlush || err.EventID != id {
		t.Errorf("reported %+v", err)
	}
	// OnError gets the event as it is sent: redacted.
	reported.mu.Lock()
	event := reported.events[0]
	reported.mu.Unlock()
	if event == nil || event.CallID != id || !strings.Contains(string(event.Input), "[EMAIL_1]") || event.Redactions[Email] != 1 {
		t.Errorf("OnError got %+v", event)
	}
	close(release)
	if result := flush(t, c); result.Failed != 0 || result.Pending != 0 || len(in.events()) != 1 {
		t.Errorf("the second flush gave %+v", result)
	}
}

func TestOnErrorMayFlush(t *testing.T) {
	in := newIngestion(t)
	in.answer = func(n int, events []map[string]any) (int, http.Header, string) { return 400, nil, "no" }
	var c *Client
	flushed := make(chan FlushResult, 8)
	c = New(Options{APIKey: "k", Endpoint: in.URL, DryRun: DryRunOff, OnError: func(err *Error, event *Event) {
		flushed <- c.Flush(context.Background())
		panic("a handler that panics breaks nothing")
	}})
	record(c, "1", "ok")
	record(c, "2", "ok")
	done := make(chan FlushResult, 1)
	go func() { done <- flush(t, c) }()
	select {
	case result := <-done:
		if result.Pending != 0 {
			t.Errorf("flush gave %+v", result)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("a flush from OnError deadlocked the client")
	}
	if len(flushed) == 0 {
		t.Error("OnError never ran")
	}
}

func TestADailyLimitHoldsEventsBack(t *testing.T) {
	in := newIngestion(t)
	resetsAt := time.Now().Add(2 * time.Hour).UTC().Format("2006-01-02T15:04:05.000Z")
	in.answer = func(n int, events []map[string]any) (int, http.Header, string) {
		rejected := make([]map[string]string, len(events))
		for i, event := range events {
			rejected[i] = map[string]string{"eventId": fmt.Sprint(event["eventId"]), "reason": "quota_exceeded", "resetsAt": resetsAt}
		}
		out, _ := json.Marshal(map[string]any{"accepted": []string{}, "duplicates": []string{}, "rejected": rejected})
		return 200, nil, string(out)
	}
	var reported problems
	c := New(Options{APIKey: "k", Endpoint: in.URL, DryRun: DryRunOff, OnError: reported.onError})
	record(c, "1", "ok")
	flush(t, c)
	for i := 0; i < 5; i++ {
		record(c, "later", "ok")
	}
	if result := flush(t, c); result != (FlushResult{}) {
		t.Errorf("events recorded under the limit were queued: %+v", result)
	}
	if got := len(in.received()); got != 1 {
		t.Errorf("%d requests were sent, want only the first", got)
	}
	err := reported.first(t)
	if codes := reported.codes(); len(codes) != 1 || err.Code != CodeQuotaExceeded || err.ResetsAt != resetsAt {
		t.Errorf("reported %v: %+v", codes, err)
	}
}

func TestAnInvalidIDDropsTheEvent(t *testing.T) {
	var reported problems
	c := New(Options{DryRun: DryRunOn, OnError: reported.onError, Logger: &lines{}})
	r := call("q", "ok")
	ids := jsonv.NewObject()
	ids.Set("userId", strings.Repeat("x", 513))
	r.Set("ids", ids)
	bridge.Of(c).Record(r, nil)
	flush(t, c)
	if err := reported.first(t); err.Code != CodeInvalidID || len(err.Paths) != 1 || err.Paths[0] != "ids.userId" {
		t.Errorf("reported %+v", err)
	}
}

func TestRetryAfter(t *testing.T) {
	now := time.Date(2026, 11, 24, 10, 0, 0, 0, time.UTC)
	for header, want := range map[string]time.Duration{
		"0": 0, "7": 7 * time.Second, " 12 ": 12 * time.Second, "999999999999999999999": retryAfterCap,
		"Tue, 24 Nov 2026 10:00:20 GMT": 20 * time.Second, "Tue, 24 Nov 2026 09:00:00 GMT": 0,
	} {
		if got, ok := parseRetryAfter(header, now); !ok || got != want {
			t.Errorf("Retry-After %q is %v (%v), want %v", header, got, ok, want)
		}
	}
	for _, header := range []string{"", "soon", "-1", "1.5"} {
		if _, ok := parseRetryAfter(header, now); ok {
			t.Errorf("Retry-After %q was read", header)
		}
	}
}

// Many goroutines recording at once, through a hook, to an ingestion that fails now and then:
// every event ends once, as sent or failed, and each one sent reached the ingestion.
func TestManyGoroutinesRecordAtOnce(t *testing.T) {
	in := newIngestion(t)
	var acceptedMu sync.Mutex
	accepted := map[string]int{}
	in.answer = func(n int, events []map[string]any) (int, http.Header, string) {
		if n%5 == 0 {
			return 503, http.Header{"Retry-After": {"0"}}, "busy"
		}
		acceptedMu.Lock()
		for _, event := range events {
			accepted[fmt.Sprint(event["eventId"])]++
		}
		acceptedMu.Unlock()
		return 200, nil, accept(events)
	}
	var reported problems
	var hooks atomic.Int64
	var droppedMu sync.Mutex
	dropped := map[string]bool{}
	c := New(Options{APIKey: "k", Endpoint: in.URL, DryRun: DryRunOff, OnError: reported.onError, MaxAttempts: 5,
		BeforeSend: func(ctx context.Context, event *Event) (*Event, error) {
			if hooks.Add(1)%50 == 0 {
				droppedMu.Lock()
				dropped[event.EventID] = true
				droppedMu.Unlock()
				return nil, nil
			}
			return event, nil
		}})
	const workers, each = 20, 50
	var wg sync.WaitGroup
	ids := make(chan string, workers*each)
	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := 0; i < each; i++ {
				ids <- record(c, "q", "ok")
				if i%10 == 0 {
					c.Flush(context.Background())
				}
			}
		}()
	}
	wg.Wait()
	close(ids)
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	// Shutdown drops what still waits for its hook, so everything is sent first.
	if result := c.Flush(ctx); result.Pending != 0 {
		t.Errorf("the flush left %+v", result)
	}
	if result := c.Shutdown(ctx); result.Pending != 0 {
		t.Errorf("shutdown left %+v", result)
	}
	failed := map[string]bool{}
	reported.mu.Lock()
	for _, err := range reported.errors {
		failed[err.EventID] = true
	}
	reported.mu.Unlock()
	// Each event was accepted once, or else reported or dropped by the hook.
	total := 0
	for id := range ids {
		total++
		if sent := accepted[id]; sent > 1 || (sent == 1) == (failed[id] || dropped[id]) {
			t.Errorf("event %s: accepted %d times, reported %v, dropped by the hook %v", id, sent, failed[id], dropped[id])
		}
	}
	if total != workers*each || len(dropped) != workers*each/50 {
		t.Errorf("%d events, %d dropped by the hook", total, len(dropped))
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.live.Len() != 0 || c.liveBytes != 0 || c.queued.Len() != 0 || c.waiting.Len() != 0 || c.activeHooks != 0 || c.zombies != 0 {
		t.Errorf("after shutdown: %d live (%d bytes), %d queued, %d waiting, %d hooks, %d zombies", c.live.Len(), c.liveBytes, c.queued.Len(), c.waiting.Len(), c.activeHooks, c.zombies)
	}
}

// stuck is a transport that never answers, whatever becomes of the request's context, until released.
type stuck struct {
	release chan struct{}
	started atomic.Int64
}

func (s *stuck) RoundTrip(*http.Request) (*http.Response, error) {
	s.started.Add(1)
	<-s.release
	return nil, errors.New("released")
}

// A request's slot comes back at its deadline even if its transport never returns: the events
// behind it aren't held up, and what the transport answers later counts for nothing.
func TestARequestThatIgnoresItsDeadlineGivesItsSlotBack(t *testing.T) {
	transport := &stuck{release: make(chan struct{})}
	defer close(transport.release)
	var reported problems
	c := New(Options{APIKey: "k", Endpoint: "https://ingest.example.com", DryRun: DryRunOff, OnError: reported.onError,
		HTTPClient: &http.Client{Transport: transport}, MaxConcurrentRequests: 1, MaxAttempts: 1, RequestTimeout: 20 * time.Millisecond})
	record(c, "1", "ok")
	// Queued behind the first, which has the only slot.
	for transport.started.Load() == 0 {
		time.Sleep(time.Millisecond)
	}
	record(c, "2", "ok")
	started := time.Now()
	if result := flush(t, c); result.Pending != 0 || result.Sent != 0 {
		t.Errorf("flush gave %+v", result)
	}
	if took := time.Since(started); took > 2*time.Second {
		t.Errorf("the flush took %v", took)
	}
	codes := reported.codes()
	if len(codes) != 2 || codes[0] != "send_failed" || codes[1] != "send_failed" || reported.first(t).Message != "Network error after 1 attempts." {
		t.Errorf("reported %v", codes)
	}
	if got := transport.started.Load(); got != 2 {
		t.Errorf("%d requests were started, want both", got)
	}
}

// An acknowledgement that stops short is no answer, so the events are sent again; one that starts
// with a byte order mark is read as any other.
func TestAnAcknowledgementCutShortIsRetried(t *testing.T) {
	var requests atomic.Int64
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		var envelope struct {
			Events []map[string]any `json:"events"`
		}
		_ = json.Unmarshal(body, &envelope)
		if requests.Add(1) == 1 {
			// More promised than sent, then the connection closed.
			w.Header().Set("Content-Length", "500")
			_, _ = w.Write([]byte(`{"accepted":[`))
			if connection, _, err := w.(http.Hijacker).Hijack(); err == nil {
				_ = connection.Close()
			}
			return
		}
		_, _ = w.Write([]byte("\xEF\xBB\xBF" + accept(envelope.Events)))
	}))
	defer server.Close()
	var reported problems
	c := New(Options{APIKey: "k", Endpoint: server.URL, DryRun: DryRunOff, OnError: reported.onError})
	record(c, "q", "ok")
	if result := flush(t, c); result.Pending != 0 || result.Failed != 0 {
		t.Errorf("flush gave %+v", result)
	}
	if codes := reported.codes(); len(codes) != 0 || requests.Load() != 2 {
		t.Errorf("reported %v after %d requests", codes, requests.Load())
	}
}

// While a daily limit holds, one event in a while checks whether it still does. An event told it
// is that check and then dropped (an invalid ID, a full queue) gives the turn to the next one.
func TestTheCheckOfADailyLimitIsNotLostWithItsEvent(t *testing.T) {
	in := newIngestion(t)
	// The check isn't answered until the event after it is recorded: its acceptance ends the
	// hold-back, and that event would then be sent.
	release := make(chan struct{})
	in.answer = func(n int, events []map[string]any) (int, http.Header, string) {
		if n == 1 {
			<-release
		}
		return 200, nil, accept(events)
	}
	var reported problems
	c := New(Options{APIKey: "k", Endpoint: in.URL, DryRun: DryRunOff, OnError: reported.onError})
	c.mu.Lock()
	c.holdback = &holdback{until: time.Now().Add(time.Hour), resetsAt: "later", nextCheck: time.Now().Add(-time.Second)}
	c.mu.Unlock()
	invalid := call("q", "ok")
	invalid.Set("toolName", strings.Repeat("x", 600))
	bridge.Of(c).Record(invalid, nil)
	record(c, "the check", "ok")
	record(c, "dropped", "ok")
	close(release)
	flush(t, c)
	events := in.events()
	if len(events) != 1 || at(events[0], "input", "query") != "the check" {
		t.Fatalf("sent %v", events)
	}
	// Its acceptance ends the hold-back: what was dropped meanwhile is counted, and sending goes on.
	record(c, "after", "ok")
	flush(t, c)
	if got := len(in.events()); got != 2 {
		t.Errorf("%d events were sent in all", got)
	}
	if codes := strings.Join(reported.codes(), ","); codes != "invalid_id,quota_dropped" {
		t.Errorf("reported %s", codes)
	}
}

func at(value any, path ...string) any {
	for _, step := range path {
		object, _ := value.(map[string]any)
		value = object[step]
	}
	return value
}

// A hook that ends its goroutine, as a test's FailNow does, still gives its slot back.
func TestAHookThatEndsItsGoroutineGivesItsSlotBack(t *testing.T) {
	var reported problems
	c := New(Options{DryRun: DryRunOn, Logger: &lines{}, OnError: reported.onError, MaxConcurrentHooks: 1,
		BeforeSend: func(ctx context.Context, event *Event) (*Event, error) {
			runtime.Goexit()
			return event, nil
		}})
	record(c, "1", "ok")
	record(c, "2", "ok")
	if result := flush(t, c); result.Pending != 0 {
		t.Errorf("flush gave %+v", result)
	}
	if codes := strings.Join(reported.codes(), ","); codes != "hook_failed,hook_failed" {
		t.Errorf("reported %s", codes)
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.activeHooks != 0 || c.zombies != 0 || c.live.Len() != 0 {
		t.Errorf("%d hooks, %d zombies, %d live", c.activeHooks, c.zombies, c.live.Len())
	}
}

// The hook's time is its own: what the SDK does with its answer afterwards doesn't count.
func TestAHooksDeadlineIsCountedToItsReturn(t *testing.T) {
	c := New(Options{DryRun: DryRunOn, Logger: &lines{}, BeforeSendTimeout: time.Hour})
	e := &entry{createdAt: time.Now().Add(-30 * time.Minute), stage: stageHook}
	c.mu.Lock()
	defer c.mu.Unlock()
	e.live = c.live.PushBack(e)
	c.activeHooks = 1
	if reshape := c.hookDone(e, false, false, time.Now()); !reshape || e.outcome != outcomeNone || e.stage != stageReshape {
		t.Errorf("a hook back in time: reshape %v, outcome %v, stage %v", reshape, e.outcome, e.stage)
	}
	// Its deadline's timer, had it fired while the hook's return was being taken, does nothing now.
	c.mu.Unlock()
	c.hookDeadline(e)
	c.mu.Lock()
	if e.outcome != outcomeNone {
		t.Errorf("the deadline of a hook that was back in time dropped its event: outcome %v", e.outcome)
	}
	late := &entry{createdAt: time.Now().Add(-2 * time.Hour), stage: stageHook}
	late.live = c.live.PushBack(late)
	c.activeHooks = 1
	if reshape := c.hookDone(late, false, false, time.Now()); reshape || late.outcome != outcomeFailed {
		t.Errorf("a hook back late: reshape %v, outcome %v", reshape, late.outcome)
	}
}

func TestAnEventIsNeverLargerThanTheIngestionTakes(t *testing.T) {
	if c := New(Options{DryRun: DryRunOn, MaxEventBytes: 512 * 1024}); c.maxEventBytes != core.DefaultMaxEventBytes {
		t.Errorf("maxEventBytes is %d", c.maxEventBytes)
	}
	if c := New(Options{DryRun: DryRunOn, MaxEventBytes: 1024}); c.maxEventBytes != 1024 {
		t.Errorf("maxEventBytes is %d", c.maxEventBytes)
	}
}

// OnError flushing from inside itself is told nothing new: the event it was just told is pending
// isn't reported to it again, and again.
func TestOnErrorFlushingIsntToldAgain(t *testing.T) {
	in := newIngestion(t)
	in.answer = func(n int, events []map[string]any) (int, http.Header, string) {
		return 503, http.Header{"Retry-After": {"30"}}, "busy"
	}
	var c *Client
	var calls atomic.Int64
	c = New(Options{APIKey: "k", Endpoint: in.URL, DryRun: DryRunOff, FlushTimeout: 50 * time.Millisecond, OnError: func(err *Error, event *Event) {
		calls.Add(1)
		c.Flush(context.Background())
	}})
	record(c, "q", "ok")
	for len(in.received()) == 0 {
		time.Sleep(time.Millisecond)
	}
	if result := c.Flush(context.Background()); result.Pending != 1 {
		t.Errorf("flush gave %+v", result)
	}
	time.Sleep(200 * time.Millisecond)
	if got := calls.Load(); got != 1 {
		t.Errorf("OnError was called %d times, want once", got)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	c.Shutdown(ctx)
}

// A Flush whose time is up doesn't wait on a slow OnError for long.
func TestFlushKeepsToItsDeadlineWithASlowOnError(t *testing.T) {
	in := newIngestion(t)
	in.answer = func(n int, events []map[string]any) (int, http.Header, string) { return 400, nil, "no" }
	release := make(chan struct{})
	defer close(release)
	c := New(Options{APIKey: "k", Endpoint: in.URL, DryRun: DryRunOff, OnError: func(*Error, *Event) { <-release }})
	record(c, "q", "ok")
	for len(in.received()) == 0 {
		time.Sleep(time.Millisecond)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	started := time.Now()
	c.Flush(ctx)
	if took := time.Since(started); took > 500*time.Millisecond {
		t.Errorf("a cancelled flush took %v", took)
	}
}

func TestTimesAreReadAsTheyAreWritten(t *testing.T) {
	want := time.Date(2026, 11, 24, 10, 0, 12, 0, time.UTC)
	for _, text := range []string{"2026-11-24T10:00:12Z", "2026-11-24T10:00:12.000Z", "2026-11-24T11:00:12+01:00"} {
		if got, ok := parseTime(text); !ok || !got.Equal(want) {
			t.Errorf("%s is read as %v (%v)", text, got, ok)
		}
	}
	// Without a zone it is local time, as JavaScript reads it.
	if got, ok := parseTime("2026-11-24T10:00:12"); !ok || !got.Equal(time.Date(2026, 11, 24, 10, 0, 12, 0, time.Local)) {
		t.Errorf("a time without a zone is read as %v (%v)", got, ok)
	}
	if got, ok := parseTime("2026-11-24"); !ok || !got.Equal(time.Date(2026, 11, 24, 0, 0, 0, 0, time.UTC)) {
		t.Errorf("a date alone is read as %v (%v)", got, ok)
	}
	if _, ok := parseTime("tomorrow"); ok {
		t.Error("tomorrow was read")
	}
	now := want.Add(-12 * time.Second)
	if got, ok := parseRetryAfter("2026-11-24T10:00:12.000Z", now); !ok || got != 12*time.Second {
		t.Errorf("an ISO Retry-After is %v (%v)", got, ok)
	}
	// A reset already past, written as a date alone, starts no hold-back.
	c := New(Options{DryRun: DryRunOn, Logger: &lines{}})
	c.mu.Lock()
	held := c.startHoldback("2020-01-01")
	c.mu.Unlock()
	if held {
		t.Error("a limit that reset in 2020 holds events back")
	}
}

// A hook that changes nothing changes no placeholder's number, in metadata as elsewhere: its
// keys are read back in the order they were first read in.
func TestAHookThatChangesNothingKeepsTheSameEvent(t *testing.T) {
	sent := func(hook func(context.Context, *Event) (*Event, error)) map[string]any {
		in := newIngestion(t)
		c := New(Options{APIKey: "k", Endpoint: in.URL, DryRun: DryRunOff, BeforeSend: hook})
		r := call("orders of zoe@example.com", "ok")
		r.Set("metadata", jsonv.ObjectFromMap(map[string]any{"10": "ann@example.com", "2": "bob@example.com", "plan": "pro", "seats": 3, "bad": []string{"x"}}))
		bridge.Of(c).Record(r, nil)
		flush(t, c)
		events := in.events()
		if len(events) != 1 {
			t.Fatalf("%d events", len(events))
		}
		for _, key := range []string{"eventId", "callId", "durationMs"} {
			delete(events[0], key)
		}
		return events[0]
	}
	plain, _ := json.Marshal(sent(nil))
	hooked, _ := json.Marshal(sent(func(ctx context.Context, event *Event) (*Event, error) { return event, nil }))
	// The hook's event says the bad value was already left out; nothing else differs.
	want := strings.Replace(string(plain), `"dropped":{"metadata":"not_json"}`, `"dropped":{"metadata":"not_json"}`, 1)
	if string(hooked) != want {
		t.Errorf("with a hook that changes nothing:\n%s\nwithout:\n%s", hooked, plain)
	}
	if !strings.Contains(string(plain), `"metadata":{"10":"[EMAIL_3]","2":"[EMAIL_2]","plan":"pro","seats":3}`) {
		t.Errorf("metadata: %s", plain)
	}
}

// The calls whose events are still being built count against the queue's limits too.
func TestTooManyCallsBeingBuiltAreRefused(t *testing.T) {
	var reported problems
	c := New(Options{DryRun: DryRunOn, Logger: &lines{}, OnError: reported.onError, MaxQueueEvents: 2, MaxQueueBytes: 1000})
	b := bridge.Of(c)
	first, ok := b.Track("a", 600)
	if !ok {
		t.Fatal("the first call was refused")
	}
	if _, ok := b.Track("b", 600); ok {
		t.Error("a call past the byte limit was taken")
	}
	second, ok := b.Track("c", 100)
	if !ok {
		t.Fatal("a call within the limits was refused")
	}
	if _, ok := b.Track("d", 1); ok {
		t.Error("a call past the count limit was taken")
	}
	first()
	second()
	if done, ok := b.Track("e", 900); !ok {
		t.Error("room wasn't given back")
	} else {
		done()
	}
	flush(t, c)
	if codes := strings.Join(reported.codes(), ","); codes != "queue_full,queue_full" {
		t.Errorf("reported %s", codes)
	}
}

// goexit is a transport, a hook or a handler that ends its goroutine instead of returning.
type goexit struct{ started atomic.Int64 }

func (g *goexit) RoundTrip(*http.Request) (*http.Response, error) {
	g.started.Add(1)
	runtime.Goexit()
	return nil, nil
}

// A transport that ends its goroutine is a network error, like one that panics: the attempt is
// counted, its slot comes back, and nothing is left behind that later attempts would wait on.
func TestATransportThatEndsItsGoroutineIsANetworkError(t *testing.T) {
	transport := &goexit{}
	var reported problems
	c := New(Options{APIKey: "k", Endpoint: "https://ingest.example.com", DryRun: DryRunOff, OnError: reported.onError,
		HTTPClient: &http.Client{Transport: transport}, MaxConcurrentRequests: 1, MaxAttempts: 1})
	for i := 0; i < 40; i++ {
		record(c, fmt.Sprint(i), "ok")
		if result := flush(t, c); result.Pending != 0 {
			t.Fatalf("event %d: flush gave %+v", i, result)
		}
	}
	if got := transport.started.Load(); got != 40 {
		t.Errorf("%d requests were started, want 40", got)
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.abandoned != 0 || c.activeRequests != 0 || c.live.Len() != 0 {
		t.Errorf("%d left behind, %d in flight, %d live", c.abandoned, c.activeRequests, c.live.Len())
	}
}

// The client's requests share nothing with the app's own: not its cookies.
func TestTheAppsCookiesAreNotSent(t *testing.T) {
	var cookies atomic.Value
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		cookies.Store(r.Header.Get("Cookie"))
		http.SetCookie(w, &http.Cookie{Name: "session", Value: "from-ingestion"})
		body, _ := io.ReadAll(r.Body)
		var envelope struct {
			Events []map[string]any `json:"events"`
		}
		_ = json.Unmarshal(body, &envelope)
		_, _ = w.Write([]byte(accept(envelope.Events)))
	}))
	defer server.Close()
	jar, _ := cookiejar.New(nil)
	target, _ := url.Parse(server.URL)
	jar.SetCookies(target, []*http.Cookie{{Name: "session", Value: "the-apps"}})
	c := New(Options{APIKey: "k", Endpoint: server.URL, DryRun: DryRunOff, HTTPClient: &http.Client{Jar: jar}})
	record(c, "q", "ok")
	if result := flush(t, c); result.Pending != 0 || result.Failed != 0 {
		t.Fatalf("flush gave %+v", result)
	}
	if sent, _ := cookies.Load().(string); sent != "" {
		t.Errorf("the request carried the app's cookie: %q", sent)
	}
	if kept := jar.Cookies(target); len(kept) != 1 || kept[0].Value != "the-apps" {
		t.Errorf("the app's cookies are now %v", kept)
	}
}

// BeforeSend flushing with its own context doesn't wait for its own event, nor for one that
// waits for the hook's slot: it returns at once, and the event is sent.
func TestBeforeSendMayFlush(t *testing.T) {
	in := newIngestion(t)
	var reported problems
	var c *Client
	var inside atomic.Int64
	c = New(Options{APIKey: "k", Endpoint: in.URL, DryRun: DryRunOff, OnError: reported.onError, MaxConcurrentHooks: 1, BeforeSendTimeout: 300 * time.Millisecond,
		BeforeSend: func(ctx context.Context, event *Event) (*Event, error) {
			started := time.Now()
			c.Flush(ctx)
			inside.Store(int64(time.Since(started)))
			return event, nil
		}})
	record(c, "1", "ok")
	record(c, "2", "ok")
	if result := flush(t, c); result.Pending != 0 || result.Failed != 0 {
		t.Errorf("flush gave %+v", result)
	}
	if took := time.Duration(inside.Load()); took > 150*time.Millisecond {
		t.Errorf("the flush inside the hook took %v", took)
	}
	if codes := reported.codes(); len(codes) != 0 || len(in.events()) != 2 {
		t.Errorf("reported %v, sent %d", codes, len(in.events()))
	}
}

// Another client's callback is no callback of this one's: its flush waits as any other.
func TestACallbackOfAnotherClientFlushesAsUsual(t *testing.T) {
	in := newIngestion(t)
	// The hook answers only once the flush has been waiting on it for a while: a flush that left
	// events inside their hooks out, as a callback's own does, would have returned by then.
	release := make(chan struct{})
	other := New(Options{APIKey: "k", Endpoint: in.URL, DryRun: DryRunOff, BeforeSend: func(ctx context.Context, event *Event) (*Event, error) {
		<-release
		return event, nil
	}})
	mine := New(Options{DryRun: DryRunOn, Logger: &lines{}})
	marked := bridge.MarkCallback(context.Background(), mine)
	if !bridge.InCallback(marked, mine) || bridge.InCallback(marked, other) || bridge.InCallback(context.Background(), mine) {
		t.Fatal("a callback's context is told from others wrongly")
	}
	record(other, "theirs", "ok")
	for {
		other.mu.Lock()
		inside := other.activeHooks == 1
		other.mu.Unlock()
		if inside {
			break
		}
		time.Sleep(time.Millisecond)
	}
	ctx, cancel := context.WithTimeout(marked, 5*time.Second)
	defer cancel()
	time.AfterFunc(100*time.Millisecond, func() { close(release) })
	started := time.Now()
	if result := other.Flush(ctx); result.Sent != 1 || time.Since(started) < 90*time.Millisecond {
		t.Errorf("flush gave %+v after %v", result, time.Since(started))
	}
}

// The check of a daily limit that its hook drops was never sent: the next event checks instead.
func TestACheckItsHookDropsGivesItsTurnUp(t *testing.T) {
	in := newIngestion(t)
	c := New(Options{APIKey: "k", Endpoint: in.URL, DryRun: DryRunOff, BeforeSend: func(ctx context.Context, event *Event) (*Event, error) {
		if string(event.Input) == `{"query":"drop"}` {
			return nil, nil
		}
		return event, nil
	}})
	c.mu.Lock()
	c.holdback = &holdback{until: time.Now().Add(time.Hour), resetsAt: "later", nextCheck: time.Now().Add(-time.Second)}
	c.mu.Unlock()
	record(c, "drop", "ok")
	flush(t, c)
	record(c, "the check", "ok")
	flush(t, c)
	events := in.events()
	if len(events) != 1 || at(events[0], "input", "query") != "the check" {
		t.Errorf("sent %v", events)
	}
}

// An OnError that ends its goroutine, as a test's FailNow does, loses no later report.
func TestOnErrorThatEndsItsGoroutine(t *testing.T) {
	var calls atomic.Int64
	c := New(Options{DryRun: DryRunOn, Logger: &lines{}, OnError: func(*Error, *Event) {
		calls.Add(1)
		runtime.Goexit()
	}})
	for i := 0; i < 3; i++ {
		c.report(&Error{Code: CodeInternal, Message: "a report"}, nil)
	}
	flush(t, c)
	if got := calls.Load(); got != 3 {
		t.Errorf("OnError was called %d times, want 3", got)
	}
	c.reports.mu.Lock()
	defer c.reports.mu.Unlock()
	if c.reports.delivered != c.reports.queued {
		t.Errorf("%d of %d delivered", c.reports.delivered, c.reports.queued)
	}
}

// blocks is a value whose own JSON must never be asked for: asking would block for ever.
type blocks struct{}

func (blocks) MarshalJSON() ([]byte, error) { select {} }

type plan string

func (plan) MarshalJSON() ([]byte, error) { panic("the app's code") }

// What a hook returns is read by its kind, never by its methods: metadata that isn't a plain
// value is left out on its own, and no code of the app's runs after the hook has returned.
func TestAHooksMetadataIsReadWithoutItsMethods(t *testing.T) {
	in := newIngestion(t)
	var reported problems
	name := "ann"
	c := New(Options{APIKey: "k", Endpoint: in.URL, DryRun: DryRunOff, OnError: reported.onError, BeforeSend: func(ctx context.Context, event *Event) (*Event, error) {
		event.Metadata = map[string]any{"stuck": blocks{}, "plan": plan("pro"), "name": &name, "seats": uint8(3), "ratio": float32(0.1),
			"big": int64(99_999_999_999_999_999), "count": json.Number("12"), "odd": json.Number("0x10"), "list": []string{"x"}, "none": (*string)(nil)}
		return event, nil
	}})
	record(c, "q", "ok")
	if result := flush(t, c); result.Pending != 0 || result.Failed != 0 {
		t.Fatalf("flush gave %+v (%v)", result, reported.codes())
	}
	event := in.events()[0]
	metadata, _ := json.Marshal(event["metadata"])
	if want := `{"big":100000000000000000,"count":12,"name":"ann","none":null,"plan":"pro","ratio":0.1,"seats":3}`; string(metadata) != want {
		t.Errorf("metadata is %s, want %s", metadata, want)
	}
	if at(event, "dropped", "beforeSend.metadata") != "not_json" {
		t.Errorf("dropped is %v", event["dropped"])
	}
}

// An error's message that is empty is still there after a hook that leaves it alone.
func TestAnEmptyErrorMessageIsKeptThroughAHook(t *testing.T) {
	sent := func(hook func(context.Context, *Event) (*Event, error)) any {
		in := newIngestion(t)
		c := New(Options{APIKey: "k", Endpoint: in.URL, DryRun: DryRunOff, BeforeSend: hook})
		r := call("q", "ok")
		r.Set("status", "error")
		failure := jsonv.NewObject()
		failure.Set("category", "thrown")
		failure.Set("message", "")
		r.Set("error", failure)
		bridge.Of(c).Record(r, nil)
		flush(t, c)
		events := in.events()
		if len(events) != 1 {
			t.Fatalf("%d events", len(events))
		}
		return events[0]["error"]
	}
	plain, _ := json.Marshal(sent(nil))
	hooked, _ := json.Marshal(sent(func(ctx context.Context, event *Event) (*Event, error) { return event, nil }))
	if string(plain) != string(hooked) || !strings.Contains(string(plain), `"message":""`) {
		t.Errorf("without a hook %s, with one %s", plain, hooked)
	}
}

// What a call being built holds counts with the events the queue holds: the oldest of those make
// room for it, and a call larger than the queue is refused.
func TestACallBeingBuiltTakesRoomInTheQueue(t *testing.T) {
	in := newIngestion(t)
	hold := make(chan struct{})
	in.answer = func(n int, events []map[string]any) (int, http.Header, string) {
		<-hold
		return 200, nil, accept(events)
	}
	defer close(hold)
	var reported problems
	c := New(Options{APIKey: "k", Endpoint: in.URL, DryRun: DryRunOff, OnError: reported.onError, MaxQueueBytes: 4000, MaxConcurrentRequests: 1})
	b := bridge.Of(c)
	if _, ok := b.Track("too large", 4001); ok {
		t.Error("a call larger than the queue was taken")
	}
	first := record(c, "1", "ok")
	for len(in.received()) == 0 {
		time.Sleep(time.Millisecond)
	}
	second := record(c, "2", "ok")
	// The first is in a request; the second, waiting for the next one, is dropped for the call.
	done, ok := b.Track("a call", 3200)
	if !ok {
		t.Fatal("the call was refused")
	}
	defer done()
	flushed := make(chan struct{})
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
		defer cancel()
		c.Flush(ctx)
		close(flushed)
	}()
	<-flushed
	var dropped []string
	reported.mu.Lock()
	for _, err := range reported.errors {
		if err.Code == CodeQueueFull {
			dropped = append(dropped, err.EventID)
		}
	}
	reported.mu.Unlock()
	if len(dropped) != 2 || dropped[0] != "too large" || dropped[1] != second {
		t.Errorf("dropped %v; the events were %s and %s", dropped, first, second)
	}
}

// A retry's timer is stopped once none of its events waits for it.
func TestARetrysTimerEndsWithItsEvents(t *testing.T) {
	in := newIngestion(t)
	in.answer = func(n int, events []map[string]any) (int, http.Header, string) {
		return 503, http.Header{"Retry-After": {"30"}}, "busy"
	}
	c := New(Options{APIKey: "k", Endpoint: in.URL, DryRun: DryRunOff, OnError: func(*Error, *Event) {}})
	record(c, "q", "ok")
	var group *retryGroup
	for group == nil {
		time.Sleep(time.Millisecond)
		c.mu.Lock()
		if e := c.live.Front(); e != nil {
			group = e.Value.(*entry).retry
		}
		c.mu.Unlock()
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	c.Shutdown(ctx)
	c.mu.Lock()
	defer c.mu.Unlock()
	if group.waiting != 0 || group.timer.Stop() {
		t.Errorf("%d events still wait for the retry, or its timer still ran", group.waiting)
	}
}

// An acknowledgement that is there when a request's time runs out is taken: LSquare has the
// events, and sending them again would only repeat them.
func TestAnAcknowledgementThereAtTheDeadlineCounts(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	for i := 0; i < 1_000; i++ {
		answers := make(chan *reply, 1)
		answers <- &reply{status: 200}
		if answer, left := await(ctx, answers); answer == nil || left {
			t.Fatalf("try %d: the answer was %v, left behind %v", i, answer, left)
		}
	}
	if answer, left := await(ctx, make(chan *reply, 1)); answer != nil || !left {
		t.Errorf("without an answer: %v, left behind %v", answer, left)
	}
}

// A call whose event will be refused takes nobody's room: after Shutdown, and while a daily limit
// holds, nothing is dropped for it. Nor is a size that makes no sense ever counted.
func TestACallThatWillBeRefusedDropsNothing(t *testing.T) {
	in := newIngestion(t)
	hold := make(chan struct{})
	in.answer = func(n int, events []map[string]any) (int, http.Header, string) {
		<-hold
		return 200, nil, accept(events)
	}
	defer close(hold)
	var reported problems
	c := New(Options{APIKey: "k", Endpoint: in.URL, DryRun: DryRunOff, OnError: reported.onError, MaxQueueEvents: 1})
	b := bridge.Of(c)
	record(c, "in flight", "ok")
	for len(in.received()) == 0 {
		time.Sleep(time.Millisecond)
	}
	live := func() int {
		c.mu.Lock()
		defer c.mu.Unlock()
		return c.live.Len()
	}
	// While a daily limit holds.
	c.mu.Lock()
	c.holdback = &holdback{until: time.Now().Add(time.Hour), resetsAt: "later", nextCheck: time.Now().Add(time.Hour)}
	c.mu.Unlock()
	if _, ok := b.Track("under the limit", 10); ok || live() != 1 {
		t.Errorf("a call was taken under a daily limit, or an event dropped for it: %d live", live())
	}
	c.mu.Lock()
	// It is one more dropped for the limit, not for room.
	if c.holdback.dropped != 1 || len(reported.codes()) != 0 {
		t.Errorf("%d dropped for the limit, reported %v", c.holdback.dropped, reported.codes())
	}
	c.holdback = nil
	// After Shutdown.
	c.closed = true
	c.mu.Unlock()
	if _, ok := b.Track("after shutdown", 10); ok || live() != 1 {
		t.Errorf("a call was taken after Shutdown, or an event dropped for it: %d live", live())
	}
	c.mu.Lock()
	c.closed = false
	c.maxQueueEvents = 10
	c.mu.Unlock()
	done, ok := b.Track("a call", 100)
	if !ok {
		t.Fatal("a call within the limits was refused")
	}
	defer done()
	if _, ok := b.Track("too large to count", math.MaxInt); ok {
		t.Error("a call of the largest size there is was taken")
	}
	c.mu.Lock()
	count, held := c.held()
	c.mu.Unlock()
	if count != 2 || held < 100 || held > c.maxQueueBytes {
		t.Errorf("the queue holds %d, of %d bytes", count, held)
	}
}

// An event whose hook's answer is being shaped still holds what it held: nothing takes its room,
// and dropped meanwhile it gives the room back only when the shaping ends.
func TestAnEventBeingShapedKeepsItsRoom(t *testing.T) {
	c := New(Options{DryRun: DryRunOn, Logger: &lines{}, OnError: func(*Error, *Event) {}})
	c.mu.Lock()
	defer c.mu.Unlock()
	e := &entry{createdAt: time.Now(), stage: stageReshape, bytes: 500}
	e.live = c.live.PushBack(e)
	c.liveBytes += e.bytes
	if c.evictUntil(func() bool { return false }, nil) || e.outcome != outcomeNone {
		t.Fatalf("an event being shaped was dropped for room: outcome %v", e.outcome)
	}
	if count, held := c.pinned(nil); count != 1 || held != 500 {
		t.Errorf("pinned: %d events, %d bytes", count, held)
	}
	c.finish(e, outcomeFailed, nil)
	if _, held := c.held(); held != 500 || c.zombies != 1 {
		t.Errorf("dropped while shaped, it holds %d bytes (%d zombies)", held, c.zombies)
	}
	c.reshaped(e, false, nil, nil)
	if count, held := c.held(); count != 0 || held != 0 || c.zombies != 0 {
		t.Errorf("after its shaping: %d events, %d bytes, %d zombies", count, held, c.zombies)
	}
}

// A hook that has returned, its return only waiting for the client's lock, isn't dropped by its
// deadline coming due meanwhile: when it returned decides.
func TestAHookBackBeforeItsDeadlineWinsTheLock(t *testing.T) {
	c := New(Options{DryRun: DryRunOn, Logger: &lines{}, BeforeSendTimeout: time.Hour})
	e := &entry{createdAt: time.Now(), stage: stageHook, bytes: 10}
	c.mu.Lock()
	e.live = c.live.PushBack(e)
	c.liveBytes += e.bytes
	c.activeHooks = 1
	c.mu.Unlock()
	e.returned.Store(true)
	c.hookDeadline(e)
	c.mu.Lock()
	defer c.mu.Unlock()
	if e.outcome != outcomeNone {
		t.Fatalf("the deadline dropped a hook that was back: outcome %v", e.outcome)
	}
	if reshape := c.hookDone(e, false, false, time.Now()); !reshape || e.stage != stageReshape {
		t.Errorf("reshape %v, stage %v", reshape, e.stage)
	}
}

// BeforeSend flushing doesn't wait for OnError either: OnError may be waiting for it.
func TestBeforeSendFlushingDoesntWaitForOnError(t *testing.T) {
	in := newIngestion(t)
	held := make(chan struct{})
	var inside atomic.Int64
	var c *Client
	c = New(Options{APIKey: "k", Endpoint: in.URL, DryRun: DryRunOff, BeforeSendTimeout: 400 * time.Millisecond,
		OnError: func(*Error, *Event) { <-held },
		BeforeSend: func(ctx context.Context, event *Event) (*Event, error) {
			started := time.Now()
			c.Flush(ctx)
			inside.Store(int64(time.Since(started)))
			return event, nil
		}})
	defer close(held)
	// A report OnError never returns from.
	c.report(&Error{Code: CodeInternal, Message: "held"}, nil)
	record(c, "q", "ok")
	deadline := time.Now().Add(3 * time.Second)
	for len(in.events()) == 0 && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	if took := time.Duration(inside.Load()); len(in.events()) != 1 || took > 200*time.Millisecond {
		t.Errorf("%d events sent; the flush inside the hook took %v", len(in.events()), took)
	}
}

// A time written without a zone is read as JavaScript reads it, where the clocks change too: by
// the offset before the change.
func TestALocalTimeTheClocksSkipOrRepeat(t *testing.T) {
	losAngeles, err := time.LoadLocation("America/Los_Angeles")
	if err != nil {
		t.Skip("no time zone data here")
	}
	for wall, want := range map[string]string{
		"2026-06-01T12:00:00": "2026-06-01T19:00:00Z",
		// Skipped: the clocks go from 02:00 to 03:00.
		"2026-03-08T02:30:00": "2026-03-08T10:30:00Z",
		"2026-03-08T01:59:59": "2026-03-08T09:59:59Z",
		"2026-03-08T03:00:00": "2026-03-08T10:00:00Z",
		// Repeated: the clocks go from 02:00 back to 01:00.
		"2026-11-01T01:30:00": "2026-11-01T08:30:00Z",
		"2026-11-01T00:59:59": "2026-11-01T07:59:59Z",
		"2026-11-01T02:00:00": "2026-11-01T10:00:00Z",
	} {
		shown, _ := time.Parse("2006-01-02T15:04:05", wall)
		if got := localTime(shown, losAngeles).UTC().Format(time.RFC3339); got != want {
			t.Errorf("%s in Los Angeles is %s, want %s", wall, got, want)
		}
	}
}

// Once Shutdown is called no new call is taken, though it is still waiting for the calls that
// had replied before it: a call that would be refused in the end takes nobody's room.
func TestShutdownTakesNoNewCallWhileItWaits(t *testing.T) {
	in := newIngestion(t)
	sent, release := make(chan struct{}), make(chan struct{})
	in.answer = func(n int, events []map[string]any) (int, http.Header, string) {
		if n == 1 {
			close(sent)
			<-release
		}
		return 200, nil, accept(events)
	}
	c := New(Options{APIKey: "k", Endpoint: in.URL, DryRun: DryRunOff, MaxQueueEvents: 2, OnError: func(*Error, *Event) {}})
	b := bridge.Of(c)
	record(c, "in flight", "ok")
	<-sent
	earlier := call("replied before", "ok")
	earlierID, _ := fieldText(earlier, "callId")
	built, ok := b.Track(earlierID, 1)
	if !ok {
		t.Fatal("a call before Shutdown was refused")
	}
	finished := make(chan FlushResult, 1)
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		finished <- c.Shutdown(ctx)
	}()
	for !b.Closed() {
		time.Sleep(time.Millisecond)
	}
	if _, ok := b.Track("a call after", 1); ok {
		t.Error("a call was taken after Shutdown was called")
	}
	b.Record(earlier, nil)
	built()
	close(release)
	if result := <-finished; result.Sent != 2 || result.Failed != 0 {
		t.Errorf("shutdown gave %+v", result)
	}
}

// Sizes are never added up past what an int holds, whatever limit the app sets.
func TestTheQueuesLimitIsNeverLargeEnoughToOverflow(t *testing.T) {
	c := New(Options{DryRun: DryRunOn, Logger: &lines{}, MaxQueueBytes: math.MaxInt, MaxQueueEvents: 3})
	release, ok := bridge.Of(c).Track("held", c.maxQueueBytes-100)
	if !ok {
		t.Fatal("a call within the limit was refused")
	}
	defer release()
	c.mu.Lock()
	defer c.mu.Unlock()
	grown := &entry{stage: stageQueued, bytes: 200}
	grown.live = c.live.PushBack(grown)
	c.liveBytes = 200
	if c.fitsAfterGrowth(grown) {
		t.Error("an event that doesn't fit was kept")
	}
	if _, held := c.held(); held < 0 {
		t.Errorf("the queue holds %d bytes", held)
	}
}

// BeforeSend shutting the client down with its own context, which ends as its event is dropped:
// what was already on its way is still waited for.
func TestBeforeSendMayShutDown(t *testing.T) {
	in := newIngestion(t)
	arrived := make(chan struct{})
	in.answer = func(n int, events []map[string]any) (int, http.Header, string) {
		if n == 1 {
			close(arrived)
			time.Sleep(100 * time.Millisecond)
		}
		return 200, nil, accept(events)
	}
	var c *Client
	results := make(chan FlushResult, 1)
	c = New(Options{APIKey: "k", Endpoint: in.URL, DryRun: DryRunOff, OnError: func(*Error, *Event) {}, BeforeSendTimeout: 3 * time.Second,
		BeforeSend: func(ctx context.Context, event *Event) (*Event, error) {
			if string(event.Input) == `{"query":"shut down"}` {
				results <- c.Shutdown(ctx)
			}
			return event, nil
		}})
	record(c, "on its way", "ok")
	<-arrived
	record(c, "shut down", "ok")
	select {
	case result := <-results:
		// The one on its way was sent; the caller's own was dropped, as Shutdown drops what is
		// inside BeforeSend.
		if result.Sent != 1 || result.Failed != 1 {
			t.Errorf("shutdown gave %+v", result)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("Shutdown never returned")
	}
}
