package core

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"math"
	"strconv"
	"unicode/utf8"

	"github.com/theagentexperiencecompany/lsquare/packages/go/internal/jsonv"
)

const (
	// MaxIDBytes is the ingestion's limit for IDs, and for event IDs.
	MaxIDBytes = 512
	// Short fields kept as they are (surface, apiKeyId, names, types): longer ones are dropped as too_large.
	maxShortChars = 16_000
	// A larger turnId couldn't fit in an event: dropped as too_large before it's hashed.
	maxTurnIDBytes = 256 * 1024
	// Work allowed to measure the non-string tool results of one event, which are never sent.
	resultMeasureLimit = 1_000_000
	// Unknown top-level fields counted for the dry run; the rest are ignored without looking.
	maxIgnoredFields = 1_000
	// How deep a tool result is measured.
	measureDepth = 64
)

// Fields that identify the event: checked, never shaped or redacted, and restored after BeforeSend.
var idFields = []string{"orgId", "userId", "conversationId", "toolName", "callId"}

var turnFields = []string{
	"question", "answer", "turnId", "status", "error", "toolCalls", "usage", "latency", "verification", "client",
	"apiKeyId", "surface",
}

var inputFields = setOf(append(append([]string{}, idFields...), append(turnFields, "metadata")...)...)
var toolCallFields = []string{"name", "input", "resultChars", "resultIsError", "errorMessage", "durationMs"}
var toolCallFieldSet = setOf(toolCallFields...)

// SDKFields are the fields the SDK adds to an event.
var SDKFields = setOf("eventId", "capturedAt", "source", "schemaVersion", "kind", "sdk", "redactions", "dropped", "truncated")

// Field names likely to be used instead of ours. Only these are reported by name: any other key
// could be data, so it is counted as "(other)".
var knownAliases = setOf(
	"tool", "tool_name", "toolName", "args", "arguments", "params", "parameters", "result", "output", "response",
	"isError", "is_error", "error", "duration", "duration_ms", "latency", "latencyMs", "id", "type",
	"sessionId", "session_id", "user_id", "user", "messages", "content", "role", "createdAt", "timestamp",
	"inputTokens", "outputTokens", "cost", "costUsd",
)

func setOf(names ...string) map[string]bool {
	out := make(map[string]bool, len(names))
	for _, name := range names {
		out[name] = true
	}
	return out
}

// utf16Length is a string's length as JavaScript counts it.
func utf16Length(text string) int {
	n := 0
	for i := 0; i < len(text); {
		if text[i] < utf8.RuneSelf {
			i++
			n++
			continue
		}
		r, size := utf8.DecodeRuneInString(text[i:])
		i += size
		n += utf16Len(r)
	}
	return n
}

// boundedUTF16Length is utf16Length, without reading a string that is over the allowance anyway: a
// UTF-16 unit is at most three bytes, so a string of more than three times the allowance in bytes
// is over it, and its byte length says so.
func boundedUTF16Length(text string, allowance int) int {
	if allowance < 0 {
		allowance = 0
	}
	if len(text)/3 > allowance {
		return len(text) / 3
	}
	return utf16Length(text)
}

// notes is where shaping reports what it removed and shortened, for one event.
type notes struct {
	// Values the SDK removed: path → reason.
	dropped *jsonv.Object
	// Top-level fields shortened, in the order they were.
	truncated []string
	// Unknown fields of tool calls: known aliases by name, everything else as "(other)".
	ignoredToolCallFields *jsonv.Object
}

func newNotes() *notes {
	return &notes{dropped: jsonv.NewObject(), ignoredToolCallFields: jsonv.NewObject()}
}

func (n *notes) drop(path, reason string) { n.dropped.Set(path, reason) }

func (n *notes) truncate(name string) {
	for _, have := range n.truncated {
		if have == name {
			return
		}
	}
	n.truncated = append(n.truncated, name)
}

func (n *notes) truncatedList() []any {
	out := make([]any, len(n.truncated))
	for i, name := range n.truncated {
		out[i] = name
	}
	return out
}

// shapeField shapes one field, recording it as truncated if anything in it was shortened.
func shapeField(st *state, n *notes, name string, run func()) {
	before := st.shortened
	st.shortened = false
	run()
	if st.shortened {
		n.truncate(name)
	}
	st.shortened = before || st.shortened
}

// shapeAny shapes a JSON field at limit. Not JSON: dropped as not_json; partly not JSON: kept,
// and marked. freeForm: its keys come from the app's data, so secret key names apply.
func shapeAny(value jsonv.Value, limit int, st *state, n *notes, path string, freeForm bool) (any, bool) {
	lossy := st.lossy
	st.lossy = false
	if freeForm {
		st.secretKey = st.secretKeys
	} else {
		st.secretKey = nil
	}
	shaped := shapeJSON(value, limit, st)
	if isNotJSON(shaped) || st.lossy {
		n.drop(path, "not_json")
	}
	st.lossy = lossy || st.lossy
	st.secretKey = nil
	if isNotJSON(shaped) {
		return nil, false
	}
	return shaped, true
}

// longerThan reports whether a string has more than max code points, reading at most max + 1.
func longerThan(text string, max int) bool {
	if len(text) <= max {
		return false
	}
	_, passed := advance(text, 0, max+1)
	return passed > max
}

// shapeText shapes a field that must be a string, at limit.
func shapeText(value jsonv.Value, limit int, st *state, n *notes, path string) (any, bool) {
	if value == nil {
		return nil, false
	}
	text, ok := value.(string)
	if !ok {
		n.drop(path, "wrong_type")
		return nil, false
	}
	return shapeString(text, limit, st), true
}

type idKind int

const (
	idAbsent idKind = iota
	idEmpty
	idInvalid
	idValid
)

// idValue reads an ID: absent, a valid string, or invalid (the event is then dropped: sent without
// it, it could slip past a deletion block). "" counts as absent.
func idValue(value jsonv.Value) (string, idKind) {
	if value == nil {
		return "", idAbsent
	}
	text, ok := value.(string)
	if !ok {
		return "", idInvalid
	}
	if text == "" {
		return "", idEmpty
	}
	if len(text) > MaxIDBytes || !utf8.ValidString(text) {
		return "", idInvalid
	}
	for i := 0; i < len(text); i++ {
		if text[i] == 0 {
			return "", idInvalid
		}
	}
	return text, idValid
}

// ValidID reports whether a string is an ID the ingestion takes.
func ValidID(text string) bool {
	_, kind := idValue(text)
	return kind == idValid
}

func field(object *jsonv.Object, key string) jsonv.Value {
	value, _ := object.Get(key)
	return value
}

func asObject(value jsonv.Value) (*jsonv.Object, bool) {
	object, ok := value.(*jsonv.Object)
	return object, ok && object != nil
}

// readIDs reads the IDs; invalid lists those that would drop the event.
func readIDs(input *jsonv.Object, n *notes) (*jsonv.Object, []string) {
	ids := jsonv.NewObject()
	var invalid []string
	for _, key := range idFields {
		value, kind := idValue(field(input, key))
		switch kind {
		case idInvalid:
			invalid = append(invalid, key)
		case idEmpty:
			n.drop(key, "empty")
		case idValid:
			ids.Set(key, value)
		}
	}
	return ids, invalid
}

// TurnRef is the reference a call's event has to the turn captured inside the call: from the
// turnId as the event keeps it, the conversation the capture gave, and the call's org. ok is false
// where there is none: a turn ID the event leaves out, or an ID that isn't valid.
func TurnRef(orgID *string, conversationID string, turnID jsonv.Value) (ref string, ok bool) {
	text, reason, present := turnIDValue(turnID)
	if !present || reason != "" || !ValidID(conversationID) || (orgID != nil && !ValidID(*orgID)) {
		return "", false
	}
	return TurnKey(orgID, conversationID, text), true
}

// TurnKey is k/ and the SHA-256 of [orgId, conversationId, turnId] as compact JSON, a missing org
// as null: the same for a turn whichever SDK sends it, and different for two orgs' turns.
func TurnKey(orgID *string, conversationID, turnID string) string {
	text := []byte{'['}
	if orgID == nil {
		text = append(text, "null"...)
	} else {
		text = jsonv.AppendString(text, *orgID)
	}
	text = append(text, ',')
	text = jsonv.AppendString(text, conversationID)
	text = append(text, ',')
	text = jsonv.AppendString(text, turnID)
	text = append(text, ']')
	sum := sha256.Sum256(text)
	return "k/" + hex.EncodeToString(sum[:])
}

// NewUUID returns a random (version 4) UUID.
func NewUUID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		panic(err)
	}
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	var out [36]byte
	hex.Encode(out[0:8], b[0:4])
	out[8] = '-'
	hex.Encode(out[9:13], b[4:6])
	out[13] = '-'
	hex.Encode(out[14:18], b[6:8])
	out[18] = '-'
	hex.Encode(out[19:23], b[8:10])
	out[23] = '-'
	hex.Encode(out[24:36], b[10:16])
	return string(out[:])
}

// turnEventID is a turn's event ID: its k/ key with a conversation and a turnId, or else random.
func turnEventID(ids *jsonv.Object, turnID *string) string {
	conversation, ok := field(ids, "conversationId").(string)
	if ok && turnID != nil {
		var org *string
		if value, has := field(ids, "orgId").(string); has {
			org = &value
		}
		return TurnKey(org, conversation, *turnID)
	}
	return NewUUID()
}

const maxSafeInteger = 9007199254740991

// turnIDValue reads a turnId: a string, or an integer hashed as its decimal string.
func turnIDValue(value jsonv.Value) (text string, reason string, present bool) {
	switch v := value.(type) {
	case nil:
		return "", "", false
	case string:
		if v == "" {
			return "", "empty", true
		}
		// Its bytes first: one already too long is never read.
		if len(v) > maxTurnIDBytes || len(jsonv.AppendString(nil, v))-2 > maxTurnIDBytes {
			return "", "too_large", true
		}
		return v, "", true
	case jsonv.Num:
		f := v.Float64()
		if f != math.Trunc(f) || math.Abs(f) > maxSafeInteger {
			return "", "unsafe_integer", true
		}
		return string(v), "", true
	case jsonv.NotJSON:
		return "", "unsafe_integer", true
	}
	return "", "wrong_type", true
}

// short reads a short field kept as it is: at most 16,000 code points.
func short(value jsonv.Value) (text string, reason string, present bool) {
	if value == nil {
		return "", "", false
	}
	v, ok := value.(string)
	if !ok {
		return "", "wrong_type", true
	}
	if longerThan(v, maxShortChars) {
		return "", "too_large", true
	}
	return v, "", true
}

// applyShort records a short field, or why it was dropped.
func applyShort(value jsonv.Value, path string, n *notes, set func(string)) {
	text, reason, present := short(value)
	if !present {
		return
	}
	if reason != "" {
		n.drop(path, reason)
		return
	}
	set(text)
}

func applyNumber(value jsonv.Value, path string, n *notes, set func(jsonv.Num)) {
	if value == nil {
		return
	}
	if number, ok := value.(jsonv.Num); ok {
		set(number)
		return
	}
	n.drop(path, "wrong_type")
}

// shapeTurnFields reads a turn's own fields (a turn event's, or a call's agent) into shaped form:
// an object whose strings are still cuts until finished with a Redactor.
func shapeTurnFields(input *jsonv.Object, st *state, n *notes, prefix string) *jsonv.Object {
	body := jsonv.NewObject()
	at := func(path string) string { return prefix + path }
	// A call's agent part is one field for truncated; a turn's fields are each their own.
	within := func(name string, run func()) {
		if prefix != "" {
			name = prefix[:len(prefix)-1]
		}
		shapeField(st, n, name, run)
	}
	limit := LimitAgent

	if text, reason, present := turnIDValue(field(input, "turnId")); present {
		if reason != "" {
			n.drop(at("turnId"), reason)
		} else {
			body.Set("turnId", text)
		}
	}
	if value := field(input, "status"); value != nil {
		if value == "completed" || value == "error" {
			body.Set("status", value)
		} else {
			n.drop(at("status"), "wrong_type")
		}
	}
	for _, key := range []string{"question", "answer"} {
		within(key, func() {
			if shaped, ok := shapeText(field(input, key), limit, st, n, at(key)); ok {
				body.Set(key, shaped)
			}
		})
	}
	within("error", func() {
		if shaped := errorValue(field(input, "error"), st, n, at("error")); shaped != nil {
			body.Set("error", shaped)
		}
	})
	within("toolCalls", func() {
		if calls, ok := toolCalls(field(input, "toolCalls"), st, n, at("toolCalls")); ok {
			body.Set("toolCalls", calls)
		}
	})
	if usage := usageValue(field(input, "usage"), n, at("usage")); usage != nil {
		body.Set("usage", usage)
	}
	for _, key := range []string{"latency", "verification"} {
		value := field(input, key)
		if value == nil {
			continue
		}
		within(key, func() {
			if shaped, ok := shapeAny(value, limit, st, n, at(key), true); ok {
				body.Set(key, shaped)
			}
		})
	}
	if value, reason, present := clientValue(field(input, "client"), n, at("client")); present {
		if reason != "" {
			n.drop(at("client"), reason)
		} else {
			body.Set("client", value)
		}
	}
	applyShort(field(input, "apiKeyId"), at("apiKeyId"), n, func(v string) { body.Set("apiKeyId", v) })
	applyShort(field(input, "surface"), at("surface"), n, func(v string) { body.Set("surface", v) })
	return body
}

// finishTurnFields turns shaped turn fields into JSON. all: redact everything; otherwise only
// what was cut.
func finishTurnFields(body *jsonv.Object, r *Redactor, all bool) *jsonv.Object {
	text := func(value any) jsonv.Value { return r.finalize(value, textField(all)) }
	out := jsonv.NewObject()
	for _, key := range []string{"question", "answer"} {
		if value, ok := body.Get(key); ok {
			out.Set(key, text(value))
		}
	}
	for _, key := range []string{"turnId", "status"} {
		if value, ok := body.Get(key); ok {
			out.Set(key, value)
		}
	}
	if shaped, ok := asObject(field(body, "error")); ok {
		failure := jsonv.NewObject()
		failure.Set("message", text(field(shaped, "message")))
		if kind, has := field(shaped, "type").(string); has {
			if all {
				kind = r.text(kind, false)
			}
			failure.Set("type", kind)
		}
		out.Set("error", failure)
	}
	if calls, ok := field(body, "toolCalls").([]any); ok {
		finished := make([]any, len(calls))
		for i, item := range calls {
			call := item.(*jsonv.Object)
			done := jsonv.NewObject()
			for k := 0; k < call.Len(); k++ {
				key, value := call.At(k)
				if key != "input" && key != "errorMessage" {
					done.Set(key, value)
				}
			}
			if input, has := call.Get("input"); has {
				done.Set("input", r.finalize(input, jsonField(all)))
			}
			if message, has := call.Get("errorMessage"); has {
				done.Set("errorMessage", text(message))
			}
			finished[i] = done
		}
		out.Set("toolCalls", finished)
	}
	if usage, ok := body.Get("usage"); ok {
		out.Set("usage", usage)
	}
	for _, key := range []string{"latency", "verification"} {
		if value, ok := body.Get(key); ok {
			out.Set(key, r.finalize(value, freeFormField(all)))
		}
	}
	for _, key := range []string{"client", "apiKeyId", "surface"} {
		if value, ok := body.Get(key); ok {
			out.Set(key, value)
		}
	}
	return out
}

// shapedTurn is what shapeTurn gives.
type shapedTurn struct {
	ids      *jsonv.Object
	fields   *jsonv.Object
	metadata *jsonv.Object
	notes    *notes
	// Unknown top-level fields: known aliases by name, everything else as "(other)".
	ignoredFields *jsonv.Object
}

// failure is why an event couldn't be built.
type failure struct {
	code  string
	paths []string
}

// shapeTurn turns a turn's input (or BeforeSend's output for a turn, with identity) into a shaped
// turn. Only an invalid ID or oversized metadata drops it.
func shapeTurn(raw jsonv.Value, st *state, metadataBudget int, identity *jsonv.Object, identityTurnID *string) (*shapedTurn, *failure) {
	n := newNotes()
	object, plainObject := asObject(raw)
	if !plainObject && identity == nil {
		return nil, &failure{code: "invalid_input"}
	}
	input := jsonv.NewObject()
	if plainObject {
		for i := 0; i < object.Len(); i++ {
			if key, value := object.At(i); inputFields[key] {
				input.Set(key, value)
			}
		}
	}
	var ids *jsonv.Object
	if identity != nil {
		ids = identity
		if identityTurnID != nil {
			input.Set("turnId", *identityTurnID)
		} else {
			input.Delete("turnId")
		}
	} else {
		read, invalid := readIDs(input, n)
		if len(invalid) > 0 {
			return nil, &failure{code: "invalid_id", paths: invalid}
		}
		ids = read
	}
	fields := shapeTurnFields(input, st, n, "")
	budget := metadataBudget
	metadata, tooLarge := metadataValue(field(input, "metadata"), n, &budget)
	if tooLarge {
		return nil, &failure{code: "too_large", paths: []string{"metadata"}}
	}
	ignored := jsonv.NewObject()
	if plainObject && identity == nil {
		for i := 0; i < object.Len() && i < maxIgnoredFields; i++ {
			if key, _ := object.At(i); !inputFields[key] && !SDKFields[key] {
				countField(ignored, key)
			}
		}
	}
	return &shapedTurn{ids: ids, fields: fields, metadata: metadata, notes: n, ignoredFields: ignored}, nil
}

func countField(counts *jsonv.Object, key string) {
	name := key
	if !knownAliases[key] {
		name = "(other)"
	}
	have := int64(0)
	if value, ok := field(counts, name).(jsonv.Num); ok {
		have = int64(value.Float64())
	}
	counts.Set(name, jsonv.Int(have+1))
}

func errorValue(value jsonv.Value, st *state, n *notes, path string) *jsonv.Object {
	switch v := value.(type) {
	case nil:
		return nil
	case string:
		out := jsonv.NewObject()
		out.Set("message", shapeString(v, LimitAgent, st))
		return out
	case *jsonv.Object:
		message, ok := field(v, "message").(string)
		if !ok {
			n.drop(path, "wrong_type")
			return nil
		}
		out := jsonv.NewObject()
		out.Set("message", shapeString(message, LimitAgent, st))
		kind := field(v, "type")
		if _, isString := kind.(string); !isString {
			kind = field(v, "name")
		}
		if text, reason, present := short(kind); present {
			if reason == "" {
				if text != "" {
					out.Set("type", text)
				}
			} else if reason == "too_large" {
				// Too long to keep, as other short fields are; one of another type is ignored.
				n.drop(path+".type", "too_large")
			}
		}
		return out
	}
	n.drop(path, "wrong_type")
	return nil
}

// clientValue reads a client: a non-empty string, or { name, version? }.
func clientValue(value jsonv.Value, n *notes, path string) (jsonv.Value, string, bool) {
	if value == nil {
		return nil, "", false
	}
	if text, ok := value.(string); ok && text != "" {
		if longerThan(text, maxShortChars) {
			return nil, "too_large", true
		}
		return text, "", true
	}
	if _, ok := asObject(value); !ok {
		return nil, "wrong_type", true
	}
	named, reason := namedValue(value, n, path)
	return named, reason, true
}

// namedValue reads { name, version? }, as clients and servers name themselves. A version too long
// to keep is left out, and with notes, recorded as too_large at path.version; one of another type
// is ignored.
func namedValue(value jsonv.Value, n *notes, path string) (*jsonv.Object, string) {
	object, ok := asObject(value)
	if !ok {
		return nil, "wrong_type"
	}
	name, ok := field(object, "name").(string)
	if !ok || name == "" {
		return nil, "wrong_type"
	}
	if longerThan(name, maxShortChars) {
		return nil, "too_large"
	}
	out := jsonv.NewObject()
	out.Set("name", name)
	if version, ok := field(object, "version").(string); ok {
		if !longerThan(version, maxShortChars) {
			out.Set("version", version)
		} else if n != nil {
			n.drop(path+".version", "too_large")
		}
	}
	return out, ""
}

func toolCalls(value jsonv.Value, st *state, n *notes, path string) ([]any, bool) {
	if value == nil {
		return nil, false
	}
	list, ok := value.([]any)
	if !ok {
		n.drop(path, "wrong_type")
		return nil, false
	}
	out := []any{}
	allowance := resultMeasureLimit
	if len(list) > MaxEntries {
		st.shortened = true
	}
	end := len(list)
	if end > MaxEntries {
		end = MaxEntries
	}
	for index := 0; index < end; index++ {
		// One per item, as for any list: past the budget, no more tool calls are taken.
		if !spend(st, 1) {
			break
		}
		at := path + "[" + strconv.Itoa(index) + "]"
		item, ok := asObject(list[index])
		if !ok {
			n.drop(at, "wrong_type")
			continue
		}
		// Counted before validation: field names an app uses instead of ours show up in the dry run.
		for i := 0; i < item.Len() && i < maxIgnoredFields; i++ {
			if key, _ := item.At(i); !toolCallFieldSet[key] {
				countField(n.ignoredToolCallFields, key)
			}
		}
		name, reason, present := short(field(item, "name"))
		if !present || reason != "" || name == "" {
			// A call needs its name: one too long to keep drops it as too_large.
			if reason == "too_large" {
				n.drop(at, "too_large")
			} else {
				n.drop(at, "missing_name")
			}
			continue
		}
		call := jsonv.NewObject()
		call.Set("name", name)
		if input, has := item.Get("input"); has {
			// An explicit null input is kept: the tool was called with none.
			if input == nil {
				call.Set("input", nil)
			} else if shaped, ok := shapeAny(input, LimitInput, st, n, at+".input", true); ok {
				call.Set("input", shaped)
			}
		}
		applyNumber(field(item, "resultChars"), at+".resultChars", n, func(v jsonv.Num) { call.Set("resultChars", v) })
		if value := field(item, "resultIsError"); value != nil {
			if flag, ok := value.(bool); ok {
				call.Set("resultIsError", flag)
			} else {
				n.drop(at+".resultIsError", "wrong_type")
			}
		}
		if shaped, ok := shapeText(field(item, "errorMessage"), LimitAgent, st, n, at+".errorMessage"); ok {
			call.Set("errorMessage", shaped)
		}
		applyNumber(field(item, "durationMs"), at+".durationMs", n, func(v jsonv.Num) { call.Set("durationMs", v) })
		// Results never leave: keep only their length, in code points for a string.
		if result := field(item, "result"); !call.Has("resultChars") && result != nil {
			length, reason := 0, ""
			if text, ok := result.(string); ok {
				length, reason = resultLength(text, &allowance)
			} else {
				length, reason = jsonLength(result, &allowance)
			}
			if reason == "" {
				call.Set("resultChars", jsonv.Int(int64(length)))
			} else {
				n.drop(at+".resultChars", reason)
			}
		}
		out = append(out, call)
	}
	// Every item invalid means unknown, not "no tools were called".
	if len(out) == 0 && len(list) > 0 {
		return nil, false
	}
	return out, true
}

func resultLength(text string, allowance *int) (int, string) {
	read := *allowance
	if read < 0 {
		read = 0
	}
	end, passed := advance(text, 0, read+1)
	*allowance -= passed
	if end == len(text) && *allowance >= 0 {
		return passed, ""
	}
	return 0, "too_large"
}

// jsonLength is the exact length of a value's JSON text, in code points, measured without keeping
// a copy, within a shared work allowance. It says why when it can't: the allowance ran out, or the
// value isn't plain JSON.
func jsonLength(value jsonv.Value, allowance *int) (int, string) {
	charge := func(cost int) bool {
		*allowance -= cost
		return *allowance >= 0
	}
	quoted := func(text string) int { return utf8.RuneCount(jsonv.AppendString(nil, text)) }
	var measure func(item jsonv.Value, depth int) (int, bool)
	measure = func(item jsonv.Value, depth int) (int, bool) {
		if !charge(1) || depth >= measureDepth {
			return 0, false
		}
		switch v := item.(type) {
		case nil:
			return 4, true
		case string:
			if !charge(boundedUTF16Length(v, *allowance)) {
				return 0, false
			}
			return quoted(v), true
		case bool:
			if v {
				return 4, true
			}
			return 5, true
		case jsonv.Num:
			return len(v), true
		case jsonv.NotJSON:
			return 4, true
		case []any:
			total := 2
			if len(v) > 1 {
				total += len(v) - 1
			}
			for _, element := range v {
				length, ok := measure(element, depth+1)
				if !ok {
					return 0, false
				}
				total += length
			}
			return total, true
		case *jsonv.Object:
			total := 2
			first := true
			for i := 0; i < v.Len(); i++ {
				key, element := v.At(i)
				if !charge(boundedUTF16Length(key, *allowance)) {
					return 0, false
				}
				length, ok := measure(element, depth+1)
				if !ok {
					return 0, false
				}
				if !first {
					total++
				}
				total += quoted(key) + 1 + length
				first = false
			}
			return total, true
		}
		return 0, false
	}
	length, ok := measure(value, 0)
	if *allowance < 0 {
		return 0, "too_large"
	}
	if !ok {
		return 0, "not_json"
	}
	return length, ""
}

func usageValue(value jsonv.Value, n *notes, path string) *jsonv.Object {
	if value == nil {
		return nil
	}
	object, ok := asObject(value)
	if !ok {
		n.drop(path, "wrong_type")
		return nil
	}
	usage := jsonv.NewObject()
	for _, key := range []string{"inputTokens", "outputTokens", "costUsd"} {
		applyNumber(field(object, key), path+"."+key, n, func(v jsonv.Num) { usage.Set(key, v) })
	}
	if usage.Len() == 0 {
		return nil
	}
	return usage
}

// metadataValue reads metadata: string, number, boolean and null values only, never shortened.
// tooLarge: it doesn't fit its budget, counted as one per entry, its key, and a string's length.
func metadataValue(value jsonv.Value, n *notes, budget *int) (metadata *jsonv.Object, tooLarge bool) {
	if value == nil {
		return nil, false
	}
	object, ok := asObject(value)
	if !ok {
		n.drop("metadata", "wrong_type")
		return nil, false
	}
	out := jsonv.NewObject()
	lossy := false
	for i := 0; i < object.Len(); i++ {
		key, item := object.At(i)
		*budget--
		if *budget < 0 {
			return nil, true
		}
		cost := 1
		switch v := item.(type) {
		case nil, bool, jsonv.Num:
		case string:
			cost = boundedUTF16Length(v, *budget)
		default:
			lossy = true
			continue
		}
		if key == "__proto__" {
			lossy = true
			continue
		}
		*budget -= boundedUTF16Length(key, *budget) + cost
		if *budget < 0 {
			return nil, true
		}
		out.Set(key, item)
	}
	if lossy {
		n.drop("metadata", "not_json")
	}
	if out.Len() == 0 {
		return nil, false
	}
	return out, false
}

// metadataSize is metadata's size as its budget counts it.
func metadataSize(metadata *jsonv.Object) int {
	size := 0
	for i := 0; i < metadata.Len(); i++ {
		key, value := metadata.At(i)
		size += 1 + utf16Length(key)
		if text, ok := value.(string); ok {
			size += utf16Length(text)
		} else {
			size++
		}
	}
	return size
}
