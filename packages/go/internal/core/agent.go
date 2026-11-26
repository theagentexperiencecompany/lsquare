package core

import (
	"strings"

	"github.com/theagentexperiencecompany/lsquare/packages/go/internal/jsonv"
)

// ShapedAgent is a call's agent part, shaped as its event will hold it when Capture is called
// inside the call, as TypeScript's readAgent shapes it: with a scan budget of its own, which the
// call's is charged for in its place. It holds only what the event takes of the turn, in strings
// of its own: nothing of what the app gave.
type ShapedAgent struct {
	body             *jsonv.Object
	notes            *notes
	metadata         *jsonv.Object
	metadataTooLarge bool
	metadataNotes    *notes
	scanned          int
	bytes            int
}

// ShapeAgent shapes a turn's fields, as a capture gives them, as a call's agent part.
func ShapeAgent(input *jsonv.Object, ctx *Context) *ShapedAgent {
	redactor := NewRedactor(ctx.Patterns, ctx.RedactKeys)
	st := newState(redactor.secretKeyMatcher())
	n := newNotes()
	body := shapeTurnFields(pick(input, agentFields), st, n, "agent.")
	// As for a turn of its own: one without a status completed.
	if !body.Has("status") {
		body.Set("status", "completed")
	}
	metadataNotes := newNotes()
	budget := metadataBudget
	metadata, tooLarge := metadataValue(field(input, "metadata"), metadataNotes, &budget)
	if tooLarge {
		metadata = nil
	}
	agent := &ShapedAgent{
		body: detach(body).(*jsonv.Object), notes: detachNotes(n), metadataTooLarge: tooLarge,
		metadataNotes: detachNotes(metadataNotes), scanned: ScanBudget - max(st.remaining, 0),
	}
	if metadata != nil {
		agent.metadata = detach(metadata).(*jsonv.Object)
	}
	agent.bytes = 64 + retained(agent.body) + retained(agent.metadata) + notesBytes(agent.notes) + notesBytes(agent.metadataNotes)
	return agent
}

// Bytes is about what the agent part holds, for the queue's limits.
func (a *ShapedAgent) Bytes() int { return a.bytes }

// TurnID is the turnId the event keeps: nil when it keeps none.
func (a *ShapedAgent) TurnID() jsonv.Value {
	value, _ := a.body.Get("turnId")
	return value
}

// charge takes what the agent part read of the scan budget from the call's, in its place: with
// more left than it read, what's left for the result is what shaping it now would leave. With
// less, the budget ran out there: it keeps what it read, and the result gets none.
func (a *ShapedAgent) charge(st *state) {
	if st.remaining > a.scanned {
		st.remaining -= a.scanned
	} else {
		st.remaining = 0
	}
}

// addNotes adds notes taken earlier in their place among the event's.
func addNotes(into, from *notes) {
	mergeNotes(into, from)
	for i := 0; i < from.ignoredToolCallFields.Len(); i++ {
		name, count := from.ignoredToolCallFields.At(i)
		have := int64(0)
		if value, ok := field(into.ignoredToolCallFields, name).(jsonv.Num); ok {
			have = int64(value.Float64())
		}
		if value, ok := count.(jsonv.Num); ok {
			into.ignoredToolCallFields.Set(name, jsonv.Int(have+int64(value.Float64())))
		}
	}
}

// detach is a shaped value in strings of its own: a cut of the app's string otherwise keeps all
// of it alive.
func detach(value any) any {
	switch v := value.(type) {
	case string:
		return strings.Clone(v)
	case jsonv.Num:
		return jsonv.Num(strings.Clone(string(v)))
	case *cutString:
		copied := *v
		copied.window, copied.mark = strings.Clone(v.window), strings.Clone(v.mark)
		return &copied
	case *secret:
		copied := *v
		copied.identity = strings.Clone(v.identity)
		return &copied
	case []any:
		out := make([]any, len(v))
		for i, item := range v {
			out[i] = detach(item)
		}
		return out
	case *jsonv.Object:
		out := jsonv.NewObject()
		for i := 0; i < v.Len(); i++ {
			key, item := v.At(i)
			out.Set(strings.Clone(key), detach(item))
		}
		return out
	}
	return value
}

func detachNotes(n *notes) *notes {
	return &notes{
		dropped:               detach(n.dropped).(*jsonv.Object),
		truncated:             append([]string(nil), n.truncated...),
		ignoredToolCallFields: detach(n.ignoredToolCallFields).(*jsonv.Object),
	}
}

// retained is about what a shaped value holds, in bytes, as Go lays it out: a string or a number
// boxed in an interface, a list's and an object's slices with room to grow, and an object's
// index of its keys once it has one (from 12 keys).
func retained(value any) int {
	switch v := value.(type) {
	case string:
		return 32 + len(v)
	case jsonv.Num:
		return 32 + len(v)
	case *cutString:
		return 80 + len(v.window) + len(v.mark)
	case *secret:
		return 48 + len(v.identity)
	case []any:
		size := 32
		for _, item := range v {
			size += 32 + retained(item)
		}
		return size
	case *jsonv.Object:
		if v == nil {
			return 0
		}
		size := 64
		for i := 0; i < v.Len(); i++ {
			key, item := v.At(i)
			size += 64 + len(key) + retained(item)
			if v.Len() >= 12 {
				size += 56
			}
		}
		return size
	}
	return 16
}

func notesBytes(n *notes) int {
	size := retained(n.dropped) + retained(n.ignoredToolCallFields)
	for _, name := range n.truncated {
		size += 16 + len(name)
	}
	return size
}
