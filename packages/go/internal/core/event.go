package core

import (
	"regexp"
	"slices"
	"strconv"

	"github.com/theagentexperiencecompany/lsquare/packages/go/internal/jsonv"
)

// Parts left out, in order, until an event fits: biggest and least needed first. A call's server
// instructions and tool description go first of all: they are the same on every call of the
// server or the tool, so nothing that only this call has is left out to make room for them.
var (
	turnOrder = [][]string{{"toolCalls"}, {"answer"}, {"question"}, {"verification"}, {"latency"}}
	callOrder = [][]string{{"serverInstructions"}, {"toolDescription"}, {"result"}, {"input"}, {"agent", "toolCalls"}, {"agent"}}
)

// Context is what every event needs, besides its content.
type Context struct {
	// The event's sdk field: name, version, language and runtime.
	SDK           *jsonv.Object
	Patterns      []Pattern
	RedactKeys    []string
	MaxEventBytes int
}

// DefaultMaxEventBytes is the largest event the ingestion takes.
const DefaultMaxEventBytes = 256 * 1024

// Metadata's own budget: the whole event's size.
const metadataBudget = 256 * 1024

var turnRefRE = regexp.MustCompile(`^k/[0-9a-f]{64}$`)

var categories = setOf("thrown", "tool_error", "protocol", "timeout")

var agentFields = turnFields

// Draft is an event on its way: built, maybe waiting for BeforeSend, then finished.
type Draft struct {
	// "turn" or "tool_call".
	Kind string
	// The event as BeforeSend sees it, or as sent when there is none.
	Event *jsonv.Object
	// Keeps numbering across BeforeSend.
	redactor *Redactor
	// Fields the hook can't change, restored from here.
	identity *jsonv.Object
	category string
	// The agent part's turnId, restored after the hook: the call's turnRef was worked out from it.
	agentTurnID *string
	diagnostics bool
	notes       *notes
	// Unknown top-level fields of a turn, for the dry run.
	IgnoredFields *jsonv.Object
}

// Limited reports whether the event is sent as limited diagnostics: Identify failed.
func (d *Draft) Limited() bool { return d.diagnostics }

// IgnoredToolCallFields are the unknown fields of a turn's tool calls, for the dry run.
func (d *Draft) IgnoredToolCallFields() *jsonv.Object { return d.notes.ignoredToolCallFields }

// RetainedBytes is what a draft keeps beyond its event, for the queue's limits: its redaction
// state, and the agent part's turnId, which fitting may have left out of the event.
func (d *Draft) RetainedBytes() int {
	bytes := d.redactor.retainedBytes()
	if d.agentTurnID != nil {
		bytes += len(*d.agentTurnID) * 2
	}
	return bytes
}

// Failure is why an event couldn't be built: invalid_input, invalid_id or too_large.
type Failure struct {
	Code  string
	Paths []string
}

// BuildTurn builds a turn. With a hook pending, only cut strings and what's under secret key
// names are redacted yet.
func BuildTurn(input jsonv.Value, capturedAt string, ctx *Context, hook bool) (*Draft, *Failure) {
	redactor := NewRedactor(ctx.Patterns, ctx.RedactKeys)
	st := newState(redactor.secretKeyMatcher())
	shaped, failed := shapeTurn(input, st, ctx.MaxEventBytes, nil, nil)
	if failed != nil {
		return nil, &Failure{Code: failed.code, Paths: failed.paths}
	}
	// A turn without a status completed.
	if !shaped.fields.Has("status") {
		shaped.fields.Set("status", "completed")
	}
	redactor.reserve(shaped.fields)
	redactor.reserve(shaped.metadata)
	var turnID *string
	if value, ok := field(shaped.fields, "turnId").(string); ok {
		turnID = &value
	}
	head := jsonv.NewObject()
	head.Set("eventId", turnEventID(shaped.ids, turnID))
	head.Set("schemaVersion", jsonv.Int(2))
	head.Set("kind", "turn")
	head.Set("capturedAt", capturedAt)
	head.Set("source", "live")
	head.Set("sdk", ctx.SDK)
	identity := jsonv.Clone(head).(*jsonv.Object)
	for i := 0; i < shaped.ids.Len(); i++ {
		key, value := shaped.ids.At(i)
		identity.Set(key, value)
	}
	if turnID != nil {
		identity.Set("turnId", *turnID)
	}
	// The IDs too, which are never redacted: a placeholder anywhere in the event is skipped.
	redactor.reserve(identity)
	event := turnEvent(head, shaped.ids, shaped.fields, shaped.metadata, redactor, !hook)
	draft := &Draft{Kind: "turn", Event: event, redactor: redactor, identity: identity, notes: shaped.notes, IgnoredFields: shaped.ignoredFields}
	return pending(draft, hook, ctx)
}

// pending finishes a draft as far as it can be before its hook. An event waiting for its hook
// keeps only the redaction state of what it still holds.
func pending(draft *Draft, hook bool, ctx *Context) (*Draft, *Failure) {
	finished, failed := finish(draft, !hook, ctx)
	if failed == nil && hook {
		finished.redactor.prune(finished.Event)
	}
	return finished, failed
}

// BuildCall builds a call from its record: an object with callId, capturedAt, toolName,
// toolDescription, serverInstructions, status, durationMs, ids, turnRef, server, client,
// protocolVersion, sessionId,
// sessionSource, input and result (each { value }), error, round, task, intent, extra, agent,
// metadata and diagnostics. Values from the app or the client are checked and shaped here.
func BuildCall(record *jsonv.Object, ctx *Context, hook bool) (*Draft, *Failure) {
	if !outlined(record) {
		return nil, &Failure{Code: "invalid_input"}
	}
	n := newNotes()
	ids, invalid := checkCall(record)
	if len(invalid) > 0 {
		return nil, &Failure{Code: "invalid_id", Paths: invalid}
	}
	// The error read once, its category included, for the event and for the draft.
	failure, hasError := asObject(field(record, "error"))
	if record.Has("error") && !hasError {
		n.drop("error", "wrong_type")
	}
	category := ""
	if hasError {
		if value, ok := field(failure, "category").(string); ok && categories[value] {
			category = value
		}
	}
	redactor := NewRedactor(ctx.Patterns, ctx.RedactKeys)
	st := newState(redactor.secretKeyMatcher())
	diagnostic := field(record, "diagnostics") == true
	shaped, tooLarge := shapeCall(record, failure, category, st, n, diagnostic)
	if tooLarge {
		return nil, &Failure{Code: "too_large", Paths: []string{"metadata"}}
	}
	identity := callIdentity(record, ids, ctx, n)
	redactor.reserve(shaped)
	redactor.reserve(identity)
	event := callEvent(identity, shaped, redactor, !hook)
	draft := &Draft{Kind: "tool_call", Event: event, redactor: redactor, identity: identity, category: category, diagnostics: diagnostic, notes: n, IgnoredFields: jsonv.NewObject()}
	if agent, ok := asObject(field(shaped, "agent")); ok {
		if turnID, has := field(agent, "turnId").(string); has {
			draft.agentTurnID = &turnID
		}
	}
	return pending(draft, hook, ctx)
}

// outlined reports whether a record has what the wrapper itself gives every call, as the event
// needs it: when it arrived, how it ended and how long it took, and nothing but the words the
// contract knows where it has a fixed set of them.
func outlined(record *jsonv.Object) bool {
	if record == nil {
		return false
	}
	if at, ok := field(record, "capturedAt").(string); !ok || at == "" {
		return false
	}
	if status := field(record, "status"); status != "completed" && status != "error" {
		return false
	}
	if took, ok := field(record, "durationMs").(jsonv.Num); !ok || took.Float64() < 0 {
		return false
	}
	for key, words := range map[string][]string{
		"round":         {"input_required", "continuation"},
		"extra":         {"missing_tools"},
		"sessionSource": {"sent", "issued", "protocol"},
	} {
		if value, has := record.Get(key); has {
			text, ok := value.(string)
			if !ok || !slices.Contains(words, text) {
				return false
			}
		}
	}
	return true
}

// checkCall checks the call's IDs and outline, as the ingestion checks them.
func checkCall(record *jsonv.Object) (*jsonv.Object, []string) {
	var invalid []string
	ids := jsonv.NewObject()
	for _, key := range []string{"callId", "toolName"} {
		if _, kind := idValue(field(record, key)); kind != idValid {
			invalid = append(invalid, key)
		}
	}
	given, _ := asObject(field(record, "ids"))
	for _, key := range []string{"orgId", "userId", "conversationId"} {
		value, kind := idValue(field(given, key))
		if kind == idInvalid {
			invalid = append(invalid, "ids."+key)
		} else if kind == idValid {
			ids.Set(key, value)
		}
	}
	if record.Has("sessionId") {
		if _, kind := idValue(field(record, "sessionId")); kind != idValid {
			invalid = append(invalid, "sessionId")
		}
	}
	if record.Has("turnRef") {
		if ref, ok := field(record, "turnRef").(string); !ok || !turnRefRE.MatchString(ref) {
			invalid = append(invalid, "turnRef")
		}
	}
	return ids, invalid
}

// AfterHook takes BeforeSend's output, checked like the app's input: identity restored, shaped
// again (strings already shortened keep their markers), everything redacted, fitted, and
// diagnostics reduced. Values removed from it are recorded under beforeSend.
func AfterHook(out *jsonv.Object, draft *Draft, ctx *Context) (*Draft, *Failure) {
	redactor := draft.redactor
	st := newState(redactor.secretKeyMatcher())
	st.afterHook = true
	st.cuts = redactor.cuts
	hookNotes := newNotes()
	var event *jsonv.Object
	if draft.Kind == "turn" {
		head := jsonv.NewObject()
		ids := jsonv.NewObject()
		var turnID *string
		for i := 0; i < draft.identity.Len(); i++ {
			key, value := draft.identity.At(i)
			switch key {
			case "eventId", "schemaVersion", "kind", "capturedAt", "source", "sdk":
				head.Set(key, value)
			case "turnId":
				text := value.(string)
				turnID = &text
			default:
				ids.Set(key, value)
			}
		}
		shaped, failed := shapeTurn(out, st, ctx.MaxEventBytes, ids, turnID)
		if failed != nil {
			return nil, &Failure{Code: failed.code, Paths: failed.paths}
		}
		if !shaped.fields.Has("status") {
			shaped.fields.Set("status", "completed")
		}
		redactor.reserve(shaped.fields)
		redactor.reserve(shaped.metadata)
		redactor.reserve(draft.identity)
		mergeNotes(hookNotes, shaped.notes)
		event = turnEvent(head, ids, shaped.fields, shaped.metadata, redactor, true)
	} else {
		shaped, tooLarge := reshapeCall(out, draft, st, hookNotes)
		if tooLarge {
			return nil, &Failure{Code: "too_large", Paths: []string{"metadata"}}
		}
		identity := jsonv.Clone(draft.identity).(*jsonv.Object)
		// The hook may remove or change what the calling app and the server called themselves.
		fixed := field(shaped, "fixed").(*jsonv.Object)
		for _, key := range []string{"client", "server", "protocolVersion"} {
			// Taken out and put back, at the end, as TypeScript does: the event's JSON is then the same.
			identity.Delete(key)
			if value, ok := fixed.Get(key); ok {
				identity.Set(key, value)
			}
		}
		redactor.reserve(shaped)
		redactor.reserve(identity)
		event = callEvent(identity, shaped, redactor, true)
	}
	n := draft.notes
	for i := 0; i < hookNotes.dropped.Len(); i++ {
		path, reason := hookNotes.dropped.At(i)
		n.dropped.Set("beforeSend."+path, reason)
	}
	for _, name := range hookNotes.truncated {
		n.truncate(name)
	}
	next := *draft
	next.Event = event
	return finish(&next, true, ctx)
}

// finish adds the SDK's bookkeeping, fits the event, and once it's final, reduces diagnostics.
func finish(draft *Draft, final bool, ctx *Context) (*Draft, *Failure) {
	// Diagnostics keep only a few small fields, whatever the full event's size.
	if final && draft.diagnostics {
		draft.Event = diagnosticsEvent(draft)
		return draft, nil
	}
	event := draft.Event
	n, redactor := draft.notes, draft.redactor
	if final && len(redactor.counts) > 0 {
		event.Set("redactions", redactor.Counts())
	}
	if n.dropped.Len() > 0 {
		event.Set("dropped", jsonv.Clone(n.dropped))
	}
	if len(n.truncated) > 0 {
		event.Set("truncated", n.truncatedList())
	}
	order := callOrder
	if draft.Kind == "turn" {
		order = turnOrder
	}
	fits := fit(event, ctx.MaxEventBytes, order, n)
	// A diagnostics event waiting for its hook is reduced after it, so its size doesn't count yet.
	if !fits && !draft.diagnostics {
		return nil, &Failure{Code: "too_large"}
	}
	return draft, nil
}

// fit leaves parts out, in order, until the event's compact JSON fits. False if it never does.
func fit(event *jsonv.Object, maxBytes int, order [][]string, n *notes) bool {
	bytes := jsonv.Size(event)
	for _, path := range order {
		if bytes <= maxBytes {
			return true
		}
		parent := event
		if len(path) > 1 {
			inner, ok := asObject(field(event, path[0]))
			if !ok {
				continue
			}
			parent = inner
		}
		key := path[len(path)-1]
		if !parent.Has(key) {
			continue
		}
		parent.Delete(key)
		name := path[0]
		if len(path) > 1 {
			name = path[0] + "." + path[1]
		}
		n.truncate(name)
		event.Set("truncated", n.truncatedList())
		bytes = jsonv.Size(event)
	}
	return bytes <= maxBytes
}

// diagnosticsEvent is what a limited-diagnostics event keeps: what every event needs, and the
// call's outline.
func diagnosticsEvent(draft *Draft) *jsonv.Object {
	event := draft.Event
	out := jsonv.NewObject()
	out.Set("eventId", field(event, "eventId"))
	out.Set("kind", "tool_call")
	out.Set("schemaVersion", jsonv.Int(2))
	out.Set("capturedAt", field(event, "capturedAt"))
	out.Set("source", "live")
	out.Set("sdk", field(event, "sdk"))
	out.Set("callId", field(event, "callId"))
	out.Set("toolName", field(event, "toolName"))
	out.Set("status", field(event, "status"))
	out.Set("durationMs", field(event, "durationMs"))
	out.Set("identity", "failed")
	if draft.category != "" {
		failure := jsonv.NewObject()
		failure.Set("category", draft.category)
		out.Set("error", failure)
	}
	return out
}

func mergeNotes(into, from *notes) {
	for i := 0; i < from.dropped.Len(); i++ {
		path, reason := from.dropped.At(i)
		into.dropped.Set(path, reason)
	}
	for _, name := range from.truncated {
		into.truncate(name)
	}
}

func turnEvent(head, ids, fields, metadata *jsonv.Object, r *Redactor, all bool) *jsonv.Object {
	event := jsonv.Clone(head).(*jsonv.Object)
	for i := 0; i < ids.Len(); i++ {
		key, value := ids.At(i)
		event.Set(key, value)
	}
	finished := finishTurnFields(fields, r, all)
	for i := 0; i < finished.Len(); i++ {
		key, value := finished.At(i)
		event.Set(key, value)
	}
	if metadata != nil {
		event.Set("metadata", r.finalize(metadata, freeFormField(all)))
	}
	return event
}

// Calls.

func callIdentity(record, ids *jsonv.Object, ctx *Context, n *notes) *jsonv.Object {
	identity := jsonv.NewObject()
	identity.Set("eventId", field(record, "callId"))
	identity.Set("schemaVersion", jsonv.Int(2))
	identity.Set("kind", "tool_call")
	identity.Set("capturedAt", field(record, "capturedAt"))
	identity.Set("source", "live")
	identity.Set("sdk", ctx.SDK)
	identity.Set("callId", field(record, "callId"))
	identity.Set("toolName", field(record, "toolName"))
	for i := 0; i < ids.Len(); i++ {
		key, value := ids.At(i)
		identity.Set(key, value)
	}
	if record.Has("turnRef") {
		identity.Set("turnRef", field(record, "turnRef"))
	}
	namedFields(record, identity, n)
	for _, key := range []string{"sessionId", "sessionSource"} {
		if record.Has(key) {
			identity.Set(key, field(record, key))
		}
	}
	identity.Set("status", field(record, "status"))
	identity.Set("durationMs", field(record, "durationMs"))
	for _, key := range []string{"round", "extra"} {
		if record.Has(key) {
			identity.Set(key, field(record, key))
		}
	}
	return identity
}

// namedFields reads what the server and the calling app called themselves, and the protocol
// version, as the protocol (or BeforeSend) gave them. One too long to keep is left out and
// recorded as too_large, as other short fields are; one of another type is left out without a
// word, as the protocol may not give one.
func namedFields(source, into *jsonv.Object, n *notes) {
	for _, key := range []string{"server", "client"} {
		named, reason := namedValue(field(source, key), n, key)
		if named != nil {
			into.Set(key, named)
		} else if reason == "too_large" {
			n.drop(key, "too_large")
		}
	}
	if text, reason, present := short(field(source, "protocolVersion")); present {
		if reason == "" {
			into.Set("protocolVersion", text)
		} else if reason == "too_large" {
			n.drop("protocolVersion", "too_large")
		}
	}
}

// taskValue reads a task's ID and status: short strings, both needed.
func taskValue(value jsonv.Value, n *notes, path string) *jsonv.Object {
	if value == nil {
		return nil
	}
	object, _ := asObject(value)
	id, idReason, idPresent := short(field(object, "id"))
	status, statusReason, statusPresent := short(field(object, "status"))
	idValid := idPresent && idReason == ""
	statusValid := statusPresent && statusReason == ""
	if idValid && id != "" && statusValid {
		out := jsonv.NewObject()
		out.Set("id", id)
		out.Set("status", status)
		return out
	}
	// Both strings, the ID not empty, but one too long to keep: too_large, as other short fields are.
	stringsBoth := idPresent && statusPresent && (idValid || idReason == "too_large") && (statusValid || statusReason == "too_large")
	empty := idValid && id == ""
	if stringsBoth && !empty {
		n.drop(path, "too_large")
	} else {
		n.drop(path, "wrong_type")
	}
	return nil
}

// errorFields reads an error's message, type and code, as the app or the client gave them.
func errorFields(fields *jsonv.Object, st *state, n *notes) *jsonv.Object {
	failure := jsonv.NewObject()
	shapeField(st, n, "error", func() {
		if text, ok := shapeText(field(fields, "message"), LimitResult, st, n, "error.message"); ok {
			failure.Set("message", text)
		}
	})
	applyShort(field(fields, "type"), "error.type", n, func(v string) {
		if v != "" {
			failure.Set("type", v)
		}
	})
	if code, ok := field(fields, "code").(jsonv.Num); ok {
		failure.Set("code", code)
	} else {
		applyShort(field(fields, "code"), "error.code", n, func(v string) { failure.Set("code", v) })
	}
	return failure
}

// shapeCall shapes a call in a fixed order, which the scan budget makes part of the result: the
// intent and error first, as the smallest and most telling, then the input, the agent's turn and
// the result. The tool's description and the server's instructions come last, in that order: they
// are the same on every call, so they never take budget from what only this call has. Oversized
// metadata drops the event, as for a turn, unless it's diagnostics, which leave it out.
func shapeCall(record, failure *jsonv.Object, category string, st *state, n *notes, diagnostic bool) (*jsonv.Object, bool) {
	shaped := jsonv.NewObject()
	shaped.Set("fixed", jsonv.NewObject())
	if task := taskValue(field(record, "task"), n, "task"); task != nil {
		shaped.Set("task", task)
	}
	if record.Has("intent") {
		shapeField(st, n, "intent", func() {
			if intent, ok := shapeText(field(record, "intent"), LimitIntent, st, n, "intent"); ok {
				shaped.Set("intent", intent)
			}
		})
	}
	if failure != nil {
		fields := errorFields(failure, st, n)
		if category != "" {
			fields.Set("category", category)
		}
		// Neither a message nor a category: nothing the ingestion would take as an error.
		if fields.Has("message") || fields.Has("category") {
			shaped.Set("error", fields)
		} else {
			n.drop("error", "wrong_type")
		}
	}
	if holder, ok := asObject(field(record, "input")); ok {
		value := field(holder, "value")
		if value == nil {
			value = jsonv.NewObject()
		}
		shapeField(st, n, "input", func() {
			if input, ok := shapeAny(value, LimitInput, st, n, "input", true); ok {
				shaped.Set("input", input)
			}
		})
	}
	agent, hasAgent := asObject(field(record, "agent"))
	if hasAgent {
		body := shapeTurnFields(pick(agent, agentFields), st, n, "agent.")
		// As for a turn of its own: one without a status completed.
		if !body.Has("status") {
			body.Set("status", "completed")
		}
		shaped.Set("agent", body)
	} else if field(record, "agent") != nil {
		n.drop("agent", "wrong_type")
	}
	if holder, ok := asObject(field(record, "result")); ok {
		shapeField(st, n, "result", func() {
			if result := shapeResult(field(holder, "value"), st, n, false); result != nil {
				shaped.Set("result", result)
			}
		})
	}
	if record.Has("toolDescription") {
		shapeField(st, n, "toolDescription", func() {
			if description, ok := shapeText(field(record, "toolDescription"), LimitDescription, st, n, "toolDescription"); ok {
				shaped.Set("toolDescription", description)
			}
		})
	}
	if record.Has("serverInstructions") {
		shapeField(st, n, "serverInstructions", func() {
			if instructions, ok := shapeText(field(record, "serverInstructions"), LimitInstructions, st, n, "serverInstructions"); ok {
				shaped.Set("serverInstructions", instructions)
			}
		})
	}
	// identify's metadata, merged with the agent part's, which wins per key. Each is read within
	// the budget, and the merged result must fit it too: a value one replaces doesn't count.
	budget := metadataBudget
	identified, tooLarge := metadataValue(field(record, "metadata"), n, &budget)
	var captured *jsonv.Object
	capturedTooLarge := false
	if hasAgent {
		budget = metadataBudget
		captured, capturedTooLarge = metadataValue(field(agent, "metadata"), n, &budget)
	}
	var merged *jsonv.Object
	if !tooLarge && !capturedTooLarge && (identified != nil || captured != nil) {
		merged = jsonv.NewObject()
		for _, part := range []*jsonv.Object{identified, captured} {
			for i := 0; i < part.Len(); i++ {
				key, value := part.At(i)
				merged.Set(key, value)
			}
		}
	}
	if tooLarge || capturedTooLarge || (merged != nil && metadataSize(merged) > metadataBudget) {
		if !diagnostic {
			return nil, true
		}
	} else if merged != nil {
		shaped.Set("metadata", merged)
	}
	return shaped, false
}

// pick is the fields of object named in keys.
func pick(object *jsonv.Object, keys []string) *jsonv.Object {
	out := jsonv.NewObject()
	for _, key := range keys {
		if value, ok := object.Get(key); ok {
			out.Set(key, value)
		}
	}
	return out
}

// reshapeCall takes BeforeSend's output for a call: shaped again by the same rules, the result's
// blocks included, so a hook can't add a file's data back. The error keeps its category, which
// says what happened when the message is left out; the agent part keeps its turnId, which the
// call's turnRef is from.
func reshapeCall(out *jsonv.Object, draft *Draft, st *state, n *notes) (*jsonv.Object, bool) {
	shaped := jsonv.NewObject()
	fixed := jsonv.NewObject()
	shaped.Set("fixed", fixed)
	namedFields(out, fixed, n)
	if task := taskValue(field(out, "task"), n, "task"); task != nil {
		shaped.Set("task", task)
	}
	if value := field(out, "intent"); value != nil {
		shapeField(st, n, "intent", func() {
			if intent, ok := shapeText(value, LimitIntent, st, n, "intent"); ok {
				shaped.Set("intent", intent)
			}
		})
	}
	categoryOnly := func() *jsonv.Object {
		failure := jsonv.NewObject()
		failure.Set("category", draft.category)
		return failure
	}
	if value := field(out, "error"); value != nil {
		if object, ok := asObject(value); ok {
			failure := errorFields(pick(object, []string{"message", "type", "code"}), st, n)
			if draft.category != "" {
				failure.Set("category", draft.category)
			}
			if failure.Has("message") || failure.Has("category") {
				shaped.Set("error", failure)
			}
		} else {
			n.drop("error", "wrong_type")
			// Replaced by something that isn't an error: the category stays, as below.
			if draft.category != "" {
				shaped.Set("error", categoryOnly())
			}
		}
	} else if draft.category != "" {
		// Removed by the hook: the category, which isn't content, stays.
		shaped.Set("error", categoryOnly())
	}
	if value, ok := out.Get("input"); ok {
		shapeField(st, n, "input", func() {
			if input, ok := shapeAny(value, LimitInput, st, n, "input", true); ok {
				shaped.Set("input", input)
			}
		})
	}
	if value := field(out, "agent"); value != nil {
		if object, ok := asObject(value); ok {
			agent := shapeTurnFields(pick(object, agentFields), st, n, "agent.")
			agent.Delete("turnId")
			if draft.agentTurnID != nil {
				agent.Set("turnId", *draft.agentTurnID)
			}
			shaped.Set("agent", agent)
		} else {
			n.drop("agent", "wrong_type")
		}
	}
	if value := field(out, "result"); value != nil {
		shapeField(st, n, "result", func() {
			if result := shapeResult(value, st, n, true); result != nil {
				shaped.Set("result", result)
			}
		})
	}
	if value := field(out, "toolDescription"); value != nil {
		shapeField(st, n, "toolDescription", func() {
			if description, ok := shapeText(value, LimitDescription, st, n, "toolDescription"); ok {
				shaped.Set("toolDescription", description)
			}
		})
	}
	if value := field(out, "serverInstructions"); value != nil {
		shapeField(st, n, "serverInstructions", func() {
			if instructions, ok := shapeText(value, LimitInstructions, st, n, "serverInstructions"); ok {
				shaped.Set("serverInstructions", instructions)
			}
		})
	}
	budget := metadataBudget
	metadata, tooLarge := metadataValue(field(out, "metadata"), n, &budget)
	if tooLarge {
		if !draft.diagnostics {
			return nil, true
		}
	} else if metadata != nil {
		shaped.Set("metadata", metadata)
	}
	return shaped, false
}

// shapeResult keeps MCP's result without anything binary: files become their size, text is
// shortened. sized: BeforeSend's output, whose files may already be only their size.
func shapeResult(value jsonv.Value, st *state, n *notes, sized bool) *jsonv.Object {
	fields, ok := asObject(value)
	if !ok {
		n.drop("result", "wrong_type")
		return nil
	}
	out := jsonv.NewObject()
	if content, has := fields.Get("content"); has {
		if list, ok := content.([]any); ok {
			if len(list) > MaxEntries {
				st.shortened = true
			}
			blocks := []any{}
			for index := 0; index < len(list) && index < MaxEntries; index++ {
				// One per block, as for any list: past the budget, no more blocks are taken.
				if !spend(st, 1) {
					break
				}
				if block := shapeBlock(list[index], st, sized); block != nil {
					blocks = append(blocks, block)
				} else {
					n.drop("result.content["+strconv.Itoa(index)+"]", "wrong_type")
				}
			}
			out.Set("content", blocks)
		} else {
			n.drop("result.content", "wrong_type")
		}
	}
	if structured, has := fields.Get("structuredContent"); has {
		if shaped, ok := shapeAny(structured, LimitResult, st, n, "result.structuredContent", true); ok {
			out.Set("structuredContent", shaped)
		}
	}
	if flag, ok := field(fields, "isError").(bool); ok {
		out.Set("isError", flag)
	}
	return out
}

func shapeBlock(value jsonv.Value, st *state, sized bool) *jsonv.Object {
	block, ok := asObject(value)
	if !ok {
		return nil
	}
	kind, ok := field(block, "type").(string)
	if !ok || kind == "" || utf16Length(kind) > 64 {
		return nil
	}
	// A block's fields are MCP's, but a secret key name or one of RedactKeys still replaces one
	// whole, numbered by its raw value.
	hidden := func(key string) bool { return st.secretKeys != nil && st.secretKeys(key) }
	text := func(item jsonv.Value, key string) (any, bool) {
		value, ok := item.(string)
		if !ok {
			return nil, false
		}
		if hidden(key) {
			return secretString(value, LimitResult, st), true
		}
		return shapeString(value, LimitResult, st), true
	}
	mime := func(item jsonv.Value) (any, bool) {
		value, ok := item.(string)
		if !ok || utf16Length(value) > 256 {
			return nil, false
		}
		if hidden("mimeType") {
			return secretString(value, 256, st), true
		}
		return value, true
	}
	out := jsonv.NewObject()
	out.Set("type", kind)
	set := func(key string, item any, ok bool) {
		if ok {
			out.Set(key, item)
		}
	}
	// The size of a file: counted from its data, or as the wrapper measured it.
	size := func(item jsonv.Value) bool {
		switch data := item.(type) {
		case string:
			out.Set("bytes", jsonv.Int(decodedSize(utf16Length(data), data)))
		case jsonv.Blob:
			out.Set("bytes", jsonv.Int(data.Bytes))
		default:
			return false
		}
		st.shortened = true
		return true
	}
	// A size this SDK worked out before the hook: kept, as nothing else of the file is.
	sizeOnly := func() {
		if !sized {
			return
		}
		if number, ok := field(block, "bytes").(jsonv.Num); ok {
			if f := number.Float64(); f >= 0 && f <= maxSafeInteger && f == float64(int64(f)) {
				out.Set("bytes", number)
			}
		}
	}
	switch kind {
	case "text":
		if shaped, ok := text(field(block, "text"), "text"); ok {
			out.Set("text", shaped)
		} else {
			out.Set("text", "")
		}
	case "image", "audio":
		shaped, ok := mime(field(block, "mimeType"))
		set("mimeType", shaped, ok)
		if !size(field(block, "data")) {
			sizeOnly()
		}
	case "resource_link":
		shaped, ok := text(field(block, "uri"), "uri")
		set("uri", shaped, ok)
		shaped, ok = text(field(block, "name"), "name")
		set("name", shaped, ok)
		shaped, ok = mime(field(block, "mimeType"))
		set("mimeType", shaped, ok)
	case "resource":
		// After the hook, the block is in this SDK's flat form; before it, in MCP's.
		resource, nested := asObject(field(block, "resource"))
		if !nested {
			if sized {
				resource = block
			} else {
				resource = jsonv.NewObject()
			}
		}
		shaped, ok := text(field(resource, "uri"), "uri")
		set("uri", shaped, ok)
		shaped, ok = mime(field(resource, "mimeType"))
		set("mimeType", shaped, ok)
		if shaped, ok := text(field(resource, "text"), "text"); ok {
			out.Set("text", shaped)
		} else if !size(field(resource, "blob")) {
			sizeOnly()
		}
	default:
		// Unknown kinds keep only their type: their fields could hold anything.
	}
	return out
}

func callEvent(identity, shaped *jsonv.Object, r *Redactor, all bool) *jsonv.Object {
	text := func(value any) jsonv.Value { return r.finalize(value, textField(all)) }
	event := jsonv.Clone(identity).(*jsonv.Object)
	if input, ok := shaped.Get("input"); ok {
		event.Set("input", r.finalize(input, jsonField(all)))
	}
	if shapedResult, ok := asObject(field(shaped, "result")); ok {
		result := jsonv.NewObject()
		if content, has := field(shapedResult, "content").([]any); has {
			blocks := make([]any, len(content))
			for i, item := range content {
				block := item.(*jsonv.Object)
				out := jsonv.NewObject()
				out.Set("type", text(field(block, "type")))
				for _, key := range []string{"uri", "name", "mimeType", "text"} {
					if value, ok := block.Get(key); ok {
						out.Set(key, text(value))
					}
				}
				if bytes, ok := block.Get("bytes"); ok {
					out.Set("bytes", bytes)
				}
				blocks[i] = out
			}
			result.Set("content", blocks)
		}
		if structured, has := shapedResult.Get("structuredContent"); has {
			result.Set("structuredContent", r.finalize(structured, jsonField(all)))
		}
		if flag, has := shapedResult.Get("isError"); has {
			result.Set("isError", flag)
		}
		event.Set("result", result)
	}
	if shapedError, ok := asObject(field(shaped, "error")); ok {
		failure := jsonv.NewObject()
		if message, has := shapedError.Get("message"); has {
			failure.Set("message", text(message))
		}
		if kind, has := field(shapedError, "type").(string); has {
			if all {
				kind = r.text(kind, false)
			}
			failure.Set("type", kind)
		}
		if code, has := shapedError.Get("code"); has {
			if codeText, isText := code.(string); isText && all {
				code = r.text(codeText, false)
			}
			failure.Set("code", code)
		}
		if category, has := shapedError.Get("category"); has {
			failure.Set("category", category)
		}
		event.Set("error", failure)
	}
	for _, key := range []string{"intent", "toolDescription", "serverInstructions"} {
		if value, ok := shaped.Get(key); ok {
			event.Set(key, text(value))
		}
	}
	if task, ok := asObject(field(shaped, "task")); ok {
		out := jsonv.NewObject()
		out.Set("id", text(field(task, "id")))
		out.Set("status", text(field(task, "status")))
		event.Set("task", out)
	}
	if agent, ok := asObject(field(shaped, "agent")); ok {
		event.Set("agent", finishTurnFields(agent, r, all))
	}
	if metadata, ok := asObject(field(shaped, "metadata")); ok {
		event.Set("metadata", r.finalize(metadata, freeFormField(all)))
	}
	return event
}
