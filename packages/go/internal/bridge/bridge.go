// Package bridge is how the MCP wrapper reaches the client: through this internal package, so
// nothing of it shows on the client's own API.
package bridge

import (
	"context"

	"github.com/theagentexperiencecompany/lsquare/packages/go/internal/jsonv"
)

// A Report is something the client reports to OnError, or logs.
type Report struct {
	Code    string
	Message string
}

// Client is what the wrapper needs of a client.
type Client interface {
	// Enabled reports whether the client records anything: false without an API key, outside dry run.
	Enabled() bool
	// Closed reports whether Shutdown was called, as a call arrives: the call then runs
	// unrecorded, which is reported once.
	Closed() bool
	// Ended reports whether Shutdown was called, and reports nothing.
	Ended() bool
	// Record builds a call's event from its record (see core.BuildCall) and queues it. diagnostic,
	// if any, is reported once the event is admitted, before anything else about it.
	Record(record *jsonv.Object, diagnostic *Report)
	// Track tells the client a call has replied and its event is still being built, holding bytes
	// until then, so that Flush and Shutdown wait for it. The bytes count against the queue's
	// limits, with the events it holds: older events are dropped to make room, as for a new
	// event. done is called once the event is queued or dropped. ok is false, and the refusal
	// reported, when there is no room even so: the call's event is then dropped, and nothing of
	// the call kept.
	Track(callID string, bytes int) (done func(), ok bool)
	// Report reports a problem that isn't about a queued event.
	Report(report Report)
	// WarnOnce logs a warning, once per key.
	WarnOnce(key, message string)
	// AcquireHook takes one of the slots Identify runs in. ok is false when all are taken.
	AcquireHook() (release func(), ok bool)
	// CallbackContext marks ctx as one given to a callback of this client's (see MarkCallback).
	CallbackContext(ctx context.Context) context.Context
}

// Of returns the wrapper's view of a client. Set by package lsquare.
var Of func(client any) Client

type callbackKey struct{}

// MarkCallback marks a context as one a client gives a callback of the app's (BeforeSend,
// Identify): a Flush of that client given it knows it runs inside one, and doesn't wait on what
// may be waiting on it. owner is the client.
func MarkCallback(ctx context.Context, owner any) context.Context {
	return context.WithValue(ctx, callbackKey{}, owner)
}

// InCallback reports whether ctx is, or comes from, one a callback of owner's was given. Another
// client's callback is no callback of this one's.
func InCallback(ctx context.Context, owner any) bool {
	return ctx != nil && owner != nil && ctx.Value(callbackKey{}) == owner
}
