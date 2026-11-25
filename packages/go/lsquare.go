// Package lsquare sends LSquare what an MCP server's tools were called with and what they
// returned, redacted in the app before anything leaves it.
//
// Make one client, and hand it to the wrapper for your MCP server (package lsquaremcp, for the
// official Go SDK):
//
//	client := lsquare.New(lsquare.Options{APIKey: os.Getenv("LSQUARE_API_KEY")})
//	defer client.Shutdown(context.Background())
//	lsquaremcp.Instrument(server, client, nil)
//
// The client never returns an error and never panics into the app: a problem drops an event and
// is reported to Options.OnError, or logged.
package lsquare

import (
	"context"
	"encoding/json"
	"net/http"
	"time"

	"github.com/theagentexperiencecompany/lsquare/packages/go/internal/core"
)

// Version is this SDK's version, sent with every event.
const Version = "0.1.0-beta.1"

// DefaultEndpoint is where events go unless Options.Endpoint or LSQUARE_ENDPOINT says otherwise.
const DefaultEndpoint = "https://ingest.lsquare.io"

// A Pattern is one of the built-in redaction patterns.
type Pattern string

const (
	// Email addresses, also URL-encoded.
	Email Pattern = "email"
	// Phone numbers: international with a leading +, or in a US format.
	Phone Pattern = "phone"
	// Card numbers that pass the Luhn check.
	Card Pattern = "card"
	// Keys and tokens of known formats, credentials in URLs, private keys, and the values under
	// secret key names such as password.
	Secret Pattern = "secret"
	// IPv4 and IPv6 addresses.
	IP Pattern = "ip"
)

// AllPatterns returns every built-in pattern: as Options.SkipRedaction, it turns built-in
// redaction off.
func AllPatterns() []Pattern { return []Pattern{Email, Phone, Card, Secret, IP} }

// DryRun says whether the client sends events.
type DryRun int

const (
	// DryRunFromEnv is the default: a dry run when LSQUARE_DRY_RUN is 1 or true.
	DryRunFromEnv DryRun = iota
	// DryRunOn runs the whole pipeline and sends nothing: each event is logged as one line with
	// its fields and size, never its content.
	DryRunOn
	// DryRunOff sends events, whatever LSQUARE_DRY_RUN says.
	DryRunOff
)

// Logger is where the client writes its warnings and its dry run lines. Each call gets one line.
type Logger interface {
	Info(message string)
	Warn(message string)
}

// Options configures a client. The zero value of each field is its default.
type Options struct {
	// APIKey is the LSquare project's API key. Without it, outside a dry run, the client is
	// disabled: it records nothing, and says so once.
	APIKey string
	// Endpoint is the base URL of the ingestion. It defaults to LSQUARE_ENDPOINT, then
	// DefaultEndpoint. It must use https, or http for localhost.
	Endpoint string
	// DryRun runs the whole pipeline but sends nothing, and needs no API key. It defaults to
	// LSQUARE_DRY_RUN, on when that is 1 or true.
	DryRun DryRun

	// FlushTimeout is how long Flush waits when its context has no deadline: 2 seconds by default.
	// Negative: it doesn't wait. Shutdown waits 5 seconds, or this when it is longer.
	FlushTimeout time.Duration

	// BeforeSend gets every event the client takes (not one dropped before: an invalid ID, a full
	// queue, a daily limit, or its own deadline passing while it waited for a free hook), before
	// built-in redaction and sending. It may change the event and
	// return it, or return nil to drop it. What it returns is checked, shortened and redacted
	// like the app's own data, and is what is sent: an input, result or description it adds is
	// sent though the wrapper's own options left it out. An error or a panic drops the event.
	// ctx is done at the hook's deadline.
	BeforeSend func(ctx context.Context, event *Event) (*Event, error)
	// BeforeSendTimeout is the hook's deadline, counted from when the event was recorded. It
	// defaults to FlushTimeout, or 2 seconds when that is negative.
	BeforeSendTimeout time.Duration
	// MaxConcurrentHooks is how many BeforeSend calls, and how many Identify calls, run at once:
	// 16 by default.
	MaxConcurrentHooks int
	// MaxConcurrentRequests is how many requests to LSquare are in flight at once: 4 by default.
	// Other events wait, and go in batches. A request still unanswered at RequestTimeout is
	// given up and no longer counts, though a transport that ignores its context may still be
	// working on it.
	MaxConcurrentRequests int

	// SkipRedaction turns built-in patterns off. AllPatterns() turns built-in redaction off.
	SkipRedaction []Pattern
	// RedactKeys are more key names whose values are replaced whole, as for password: matched
	// lowercased, stripped to a-z0-9, by their end, so "name" also covers firstName (and fileName).
	RedactKeys []string

	// OnError is told of the events dropped, failed or still pending, with the event when it
	// was already redacted. It is called from the client's own goroutine, one call at a time, and
	// about 100 times a second at most (100 at once, then 100 a second, and 100 waiting for it):
	// Error.Suppressed counts the reports left out since the last one.
	// Without it, problems are logged, at most one line a minute.
	OnError func(err *Error, event *Event)
	// Logger defaults to standard error, so a stdio server's standard output stays clean.
	Logger Logger

	// MaxQueueEvents is how many events are held at once, waiting to be sent or in a request, with
	// the calls whose events are still being built: 10,000 by default. Past it, the oldest events
	// are dropped and reported as queue_full: first those not yet in a request, and never one
	// that is inside BeforeSend.
	MaxQueueEvents int
	// MaxQueueBytes is their size together: 10 MB by default. An event counts as its JSON, a call
	// still being built as its arguments and result. A call that holds more than this by itself
	// is not recorded.
	MaxQueueBytes int

	// The limits below are mostly for tests.

	// MaxEventBytes is the largest event sent: 256 KB by default, which is the ingestion's limit,
	// and never more. An event of limited diagnostics, which is a few hundred bytes, is sent
	// whatever this says.
	MaxEventBytes int
	// MaxAttempts is how many times an event is sent before it fails: 3 by default.
	MaxAttempts int
	// RequestTimeout is how long one request may take: 10 seconds by default.
	RequestTimeout time.Duration
	// HTTPClient sends the requests, through a copy of it: redirects are never followed, whatever
	// its own policy, and its cookie jar isn't used.
	HTTPClient *http.Client
}

// FlushResult counts what became of the events a Flush or Shutdown waited for.
type FlushResult struct {
	// Sent and acknowledged by LSquare.
	Sent int
	// Completed without sending: a dry run, or BeforeSend returned nil.
	Skipped int
	// Dropped: see OnError for why.
	Failed int
	// Still waiting or in flight when it returned.
	Pending int
}

// An ErrorCode says why an event was dropped, failed or is still pending.
type ErrorCode string

const (
	// An ID that isn't a non-empty string of at most 512 bytes without NUL.
	CodeInvalidID ErrorCode = "invalid_id"
	// The event is larger than MaxEventBytes even without its largest parts, or its metadata is
	// larger than an event can carry.
	CodeTooLarge ErrorCode = "too_large"
	// The queue is full: the event, or an older one, was dropped.
	CodeQueueFull ErrorCode = "queue_full"
	// BeforeSend returned an error or panicked.
	CodeHookFailed ErrorCode = "hook_failed"
	// BeforeSend didn't finish by its deadline.
	CodeHookTimeout ErrorCode = "hook_timeout"
	// Building or redacting the event failed: it was dropped, never sent unredacted.
	CodeRedactionFailed ErrorCode = "redaction_failed"
	// The request failed, or LSquare answered with an error, after every attempt.
	CodeSendFailed ErrorCode = "send_failed"
	// LSquare rejected the event.
	CodeRejected ErrorCode = "rejected"
	// The project's daily limit was reached: events are dropped until ResetsAt.
	CodeQuotaExceeded ErrorCode = "quota_exceeded"
	// Sending resumed after a daily limit: Count events were dropped meanwhile.
	CodeQuotaDropped ErrorCode = "quota_dropped"
	// Still pending when Flush returned.
	CodePendingAfterFlush ErrorCode = "pending_after_flush"
	// Identify failed: the call was sent as limited diagnostics.
	CodeIdentifyFailed ErrorCode = "identify_failed"
	// Dropped by Shutdown, or recorded after it.
	CodeShutdown ErrorCode = "shutdown"
	// A bug in the SDK: the event was dropped.
	CodeInternal ErrorCode = "internal_error"
)

// Error is what OnError gets. Its message never contains event content.
type Error struct {
	Code    ErrorCode
	Message string
	// EventID is the event it is about, when it is about one.
	EventID string
	// Paths are the fields at fault, for invalid_id and too_large.
	Paths []string
	// Status is the HTTP status, when the error comes from a response.
	Status int
	// ResetsAt is when the daily limit resets, for quota_exceeded.
	ResetsAt string
	// Count is how many events were dropped, for quota_dropped.
	Count int
	// Suppressed is how many reports the rate limit dropped since the last one delivered.
	Suppressed int
}

func (e *Error) Error() string { return "lsquare: " + string(e.Code) + ": " + e.Message }

// Event is a tool call's event, as BeforeSend and OnError get it.
//
// BeforeSend may change ToolDescription, Client, Server, ProtocolVersion, Input, Result, Error
// and Metadata. The other fields say which call this is and how it went: changes to them are
// ignored.
type Event struct {
	// EventID is the event's ID, which is the call's.
	EventID string
	// Kind is "tool_call".
	Kind string
	// CapturedAt is when the call arrived, in UTC, as RFC 3339 with milliseconds.
	CapturedAt string
	CallID     string
	ToolName   string
	// OrgID, UserID and ConversationID are what Identify returned.
	OrgID          string
	UserID         string
	ConversationID string
	// Status is "completed" or "error".
	Status string
	// DurationMs is how long the call took, from its arrival to its reply.
	DurationMs float64
	// LimitedDiagnostics is set when Identify failed: the event is then sent with only its tool's
	// name, status, duration and error category. BeforeSend still gets the whole event, and what
	// it changes in the content is left out with the rest.
	LimitedDiagnostics bool
	// Round is "input_required" for a call that asked the client for input, "continuation" for
	// the call that brought it, and empty for a call of one round.
	Round string

	// ToolDescription is the tool's description as agents were listed it, cut at 1,000 characters.
	ToolDescription string
	// Client and Server are what the calling app and the server call themselves.
	Client          *Named
	Server          *Named
	ProtocolVersion string
	// Input is the call's arguments, as JSON; nil when inputs aren't recorded for the tool.
	Input json.RawMessage
	// Result is what the tool returned; nil when results aren't recorded for the tool.
	Result *Result
	// Error says how the call failed; nil when it completed.
	Error *CallError
	// Metadata is what Identify returned: strings, numbers, booleans and nil.
	Metadata map[string]any

	// Redactions counts the replacements built-in redaction made, per pattern: only on an event
	// OnError gets, since BeforeSend runs before redaction.
	Redactions map[Pattern]int
	// Dropped lists the values the SDK removed: path to reason.
	Dropped map[string]string
	// Truncated lists the fields shortened, or left out for size.
	Truncated []string
}

// Named is a name and version, as clients and servers give them.
type Named struct {
	Name    string
	Version string
}

// Result is a tool's result, without anything binary: files are only their size.
type Result struct {
	// Content is the result's blocks. A result with an empty list of them and one with no list
	// are different events: nil is the second.
	Content []Block
	// StructuredContent is the result's structured content, as JSON; nil when it had none.
	StructuredContent json.RawMessage
	IsError           bool
}

// Block is one block of a result. A field a block doesn't have is nil.
type Block struct {
	// Type is "text", "image", "audio", "resource_link", "resource", or another kind, which keeps
	// only its type.
	Type string
	// Text is a text block's text, or an embedded text resource's.
	Text     *string
	URI      *string
	Name     *string
	MIMEType *string
	// Bytes is the size of an image, an audio clip or a binary resource: its data is never kept.
	Bytes *int64
}

// CallError says how a call failed.
type CallError struct {
	// Message is the error's text: recorded only when both inputs and results are recorded for the
	// tool, since it can quote either. A cancelled call's is the SDK's own sentence, always there.
	Message string
	// The event had a message, and it was empty: kept through a hook that leaves it so.
	emptyMessage bool
	// Type is the name of the Go type of an error the handler returned, when its type has a
	// name, and "panic" for a handler that panicked.
	Type string
	// Code is a JSON-RPC error's code, a float64, or nil when the error has none. BeforeSend may
	// set a number or a string.
	Code any
	// Category is "thrown", "tool_error", "protocol" or "timeout". BeforeSend can't change it.
	Category string
}

func corePatterns(skip []Pattern) []core.Pattern {
	out := make([]core.Pattern, len(skip))
	for i, pattern := range skip {
		out[i] = core.Pattern(pattern)
	}
	return core.ResolvePatterns(out)
}
