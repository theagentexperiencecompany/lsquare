// Package bridge is how the MCP wrapper reaches the client: through this internal package, so
// nothing of it shows on the client's own API.
package bridge

import (
	"context"
	"slices"
	"sync"
	"time"

	"github.com/theagentexperiencecompany/lsquare/packages/go/internal/core"
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
	// the call kept. tracked is the call's, for Settle.
	Track(callID string, bytes int) (done func(), tracked Tracked, ok bool)
	// Report reports a problem that isn't about a queued event.
	Report(report Report)
	// WarnOnce logs a warning, once per key.
	WarnOnce(key, message string)
	// AcquireHook takes one of the slots Identify runs in. ok is false when all are taken.
	AcquireHook() (release func(), ok bool)
	// CallbackContext marks ctx as one given to a callback of this client's (see MarkCallback).
	CallbackContext(ctx context.Context) context.Context
	// WithCall is ctx with a tool call in progress in it, as the tool is given ctx: a Capture of
	// this client's from the tool finds the call there (see CallOf).
	WithCall(ctx context.Context, call *Call) context.Context
	// IdentifyReturned notes that an Identify returned, now, and gives the time. learn, given that
	// time, says whether the client learns from it (it returned in time, and its IDs are valid):
	// keys, the ID fields it gave, are then what a call whose Identify fails needs of a capture,
	// on any server instrumented with the client, from then on. It is noted as its time is taken,
	// under the client's lock on what it learned, and learn takes no lock: what was in effect at
	// a time no later than now is known for good. IdentifiedKeysAt gives it, for a call decided
	// at at, whenever it asks: the fields of the last answer before at and of any at at itself,
	// a userId before any, and every ID field once the client has let go of answers that old.
	IdentifyReturned(keys []string, learn func(at time.Time) bool) time.Time
	IdentifiedKeysAt(at time.Time) []string
	// FlushTimeout is how long a reply waits for its call's event at most (see Settle): 0, none.
	FlushTimeout() time.Duration
	// Settle waits for the call's event to be built and then sent or dropped (tracked, as Track
	// gave it; nil when Track refused the call), and for the turns captured inside the call before
	// the tool returned, until from plus FlushTimeout or ctx's end.
	Settle(ctx context.Context, tracked Tracked, turns []any, from time.Time)
}

// Tracked is a call Track took, as the client knows it.
type Tracked any

// Call is a tool call in progress on an instrumented server, as a Capture inside it sees it.
type Call struct {
	// ID is the call's, which is its event's.
	ID string

	mu sync.Mutex
	// The tool returned: a capture from now on is a turn of its own.
	returned bool
	// The first capture before the tool returned is the call's agent part: claimed as it starts
	// being read, and the call's once read, if the tool still hasn't returned by then.
	claimed bool
	agent   *Agent
	// The other turns captured before the tool returned, as the client queued them: the reply
	// waits for them with the call's event, when it waits.
	turns []any
	// Flush was called inside the call: its reply waits for its event.
	flushed bool
}

// Agent is a call's agent part, as Capture read it.
type Agent struct {
	// The IDs it gave (orgId, userId, conversationId), each in a string of its own, and no longer
	// than one that is valid by a byte: the call's IDs and its turnRef's.
	IDs *jsonv.Object
	// The turn, shaped as the call's event will hold it.
	Shaped *core.ShapedAgent
}

// Bytes is about what the agent part holds, for the queue's limits.
func (a *Agent) Bytes() int {
	if a == nil {
		return 0
	}
	size := a.Shaped.Bytes()
	for i := 0; i < a.IDs.Len(); i++ {
		key, value := a.IDs.At(i)
		text, _ := value.(string)
		size += 32 + len(key) + len(text)
	}
	return size
}

// The most turns of a call a reply waits for: those past it are sent all the same.
const maxCallTurns = 100

// NewCall is a call that has just arrived.
func NewCall(id string) *Call { return &Call{ID: id} }

// Claim makes the capture the call's agent part, if it is the first before the tool returned:
// read reads it, then and there, outside the call's lock, so that nothing else of the call waits
// on it. A capture still being read as the tool returns is too late: a turn of its own, as a
// capture after the return is. False: a turn of its own. A read that panics leaves the call
// without an agent part.
func (c *Call) Claim(read func() *Agent) bool {
	c.mu.Lock()
	if c.returned || c.claimed {
		c.mu.Unlock()
		return false
	}
	c.claimed = true
	c.mu.Unlock()
	agent := read()
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.returned {
		return false
	}
	c.agent = agent
	return true
}

// Follow notes a turn captured inside the call, as the client queued it, while the tool runs. Of
// the most it notes at once, those already sent or dropped (finished, which must not wait on
// anything) make room.
func (c *Call) Follow(turn any, finished func(any) bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.returned || turn == nil {
		return
	}
	if len(c.turns) >= maxCallTurns {
		c.turns = slices.DeleteFunc(c.turns, finished)
	}
	if len(c.turns) < maxCallTurns {
		c.turns = append(c.turns, turn)
	}
}

// Flush says Flush was called inside the call, while the tool runs: its reply waits for its event.
// False once the tool has returned.
func (c *Call) Flush() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.returned {
		return false
	}
	c.flushed = true
	return true
}

// Ended is what a call was when its tool returned.
type Ended struct {
	// The call's agent part: nil without one.
	Agent *Agent
	// The turns captured besides, and whether Flush was called inside the call.
	Turns   []any
	Flushed bool
}

// Close says the tool returned, and gives what the call holds, which it no longer does: a
// context that outlives the call holds only its ID. It never waits.
func (c *Call) Close() Ended {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.returned = true
	ended := Ended{Agent: c.agent, Turns: c.turns, Flushed: c.flushed}
	c.agent, c.turns = nil, nil
	return ended
}

type callKey struct{ owner any }

// WithCall is ctx with the call in it, for owner's captures alone: a client's captures never
// find another client's call.
func WithCall(ctx context.Context, owner any, call *Call) context.Context {
	return context.WithValue(ctx, callKey{owner}, call)
}

// CallOf is the call ctx was given to, as one of owner's: nil outside one.
func CallOf(ctx context.Context, owner any) *Call {
	if ctx == nil || owner == nil {
		return nil
	}
	call, _ := ctx.Value(callKey{owner}).(*Call)
	return call
}

// Of returns the wrapper's view of a client. Set by package lsquare.
var Of func(client any) Client

type callbackKey struct{}

// callback is what a context given to a callback says: whose callback it is, and how deep in the
// client's callbacks it runs (1 for a callback the app's own code led to).
type callback struct {
	owner any
	depth int
}

// MarkCallback marks a context as one a client gives a callback of the app's (BeforeSend,
// Identify): a Flush of that client given it knows it runs inside one, and doesn't wait on what
// may be waiting on it, and a capture made in it knows how deep it is. owner is the client. The
// callback runs one level deeper than ctx does.
func MarkCallback(ctx context.Context, owner any) context.Context {
	return MarkCallbackAt(ctx, owner, CallbackDepth(ctx)+1)
}

// MarkCallbackAt is MarkCallback for a callback at the given depth: one run for an event, at one
// level deeper than the event was captured.
func MarkCallbackAt(ctx context.Context, owner any, depth int) context.Context {
	return context.WithValue(ctx, callbackKey{}, callback{owner: owner, depth: depth})
}

// InCallback reports whether ctx is, or comes from, one a callback of owner's was given. Another
// client's callback is no callback of this one's.
func InCallback(ctx context.Context, owner any) bool {
	if ctx == nil || owner == nil {
		return false
	}
	mark, ok := ctx.Value(callbackKey{}).(callback)
	return ok && mark.owner == owner
}

// CallbackDepth is how deep in the callbacks of clients ctx runs, whichever client's they are:
// 0 outside them. A hook of one client that captures on another, whose hook captures on the
// first, goes one level deeper each time, as a hook that captures on its own client does.
func CallbackDepth(ctx context.Context) int {
	if ctx == nil {
		return 0
	}
	mark, _ := ctx.Value(callbackKey{}).(callback)
	return mark.depth
}
