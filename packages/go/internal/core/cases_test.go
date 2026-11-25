package core

// The shared cases in packages/contract/cases (cases/README.md says what they hold), which the
// TypeScript and Python SDKs must match exactly. The Go SDK must keep their guarantees:
//
//   - a case that expects an error gives that error;
//   - every event passes the ingestion's checks, as the contract has them (validate.ts, run through
//     Node on the JSON this SDK writes);
//   - what a string shows in Go's event, the same string shows in the expected event: nothing the
//     expected event redacted or cut away is visible;
//   - no string shows more than its field's limit.
//
// Most cases come out equal to the expected event, and the test counts them. The ones that don't
// are listed in knownDifferences, each with its reason and what of the checks above it is let off:
// a case that starts to differ, or one listed that no longer does, fails.
//
// The cases write lone surrogates as \u escapes, which a Go string can't hold. They are read here
// as the README says and as Go's JSON decoder reads what a client sends: the two halves of a pair
// are one character, also across $concat parts, and a half without its pair is U+FFFD.

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"testing"
	"unicode/utf8"

	"github.com/theagentexperiencecompany/lsquare/packages/go/internal/contracttest"
	"github.com/theagentexperiencecompany/lsquare/packages/go/internal/jsonv"
)

const caseCapturedAt = "2026-10-28T00:00:00.000Z"

// contractDir is where the contract is: packages/contract of this repository, unless
// LSQUARE_CONTRACT_DIR says otherwise (a copy of a released contract).
func contractDir(t testing.TB) string {
	t.Helper()
	return contracttest.Dir(t)
}

type sharedCase struct {
	file    string
	name    string
	turn    jsonv.Value
	isTurn  bool
	call    *jsonv.Object
	options *jsonv.Object
	hook    *jsonv.Object
	hasHook bool
	event   *jsonv.Object
	err     string
}

func (c *sharedCase) id() string { return c.file + ": " + c.name }

func caseFiles(t testing.TB) []string {
	t.Helper()
	names, err := filepath.Glob(filepath.Join(contractDir(t), "cases", "*.json"))
	if err != nil {
		t.Fatal(err)
	}
	sort.Strings(names)
	return names
}

func loadCases(t testing.TB) []*sharedCase {
	t.Helper()
	var out []*sharedCase
	for _, path := range caseFiles(t) {
		data, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		decoded, err := jsonv.Decode(markSurrogates(t, data))
		if err != nil {
			t.Fatalf("%s: %v", path, err)
		}

		list, ok := decoded.([]any)
		if !ok || len(list) == 0 {
			t.Fatalf("%s holds no cases", path)
		}
		for _, item := range list {
			object := item.(*jsonv.Object)
			c := &sharedCase{file: filepath.Base(path), name: field(object, "name").(string)}
			if turn, ok := object.Get("turn"); ok {
				c.turn, c.isTurn = joinSurrogates(expand(turn)), true
			}
			if call, ok := object.Get("call"); ok {
				c.call = joinSurrogates(expand(call)).(*jsonv.Object)
			}
			c.options, _ = asObject(field(object, "options"))
			if hook, ok := object.Get("hook"); ok {
				c.hook, _ = asObject(hook)
				c.hasHook = true
			}
			if event, ok := object.Get("event"); ok {
				c.event = joinSurrogates(expand(event)).(*jsonv.Object)
			}
			if code, ok := field(object, "error").(string); ok {
				c.err = code
			}
			out = append(out, c)
		}
	}
	return out
}

// A surrogate half is carried through decoding as a private-use character, so that $concat can
// join two halves written apart.
const surrogateMark = 0xF0000

// markSurrogates rewrites each \uD800-\uDFFF escape in a case file as its private-use character.
func markSurrogates(t testing.TB, data []byte) []byte {
	t.Helper()
	if bytes.ContainsRune(data, surrogateMark) || bytes.ContainsRune(data, surrogateMark+0x7FF) {
		t.Fatal("a case file holds the private-use characters this test stands surrogates in with")
	}
	out := make([]byte, 0, len(data))
	for i := 0; i < len(data); i++ {
		if data[i] != '\\' || i+1 >= len(data) {
			out = append(out, data[i])
			continue
		}
		if data[i+1] == 'u' && i+6 <= len(data) {
			var unit rune
			if _, err := fmt.Sscanf(string(data[i+2:i+6]), "%04x", &unit); err == nil && unit >= 0xD800 && unit <= 0xDFFF {
				out = utf8.AppendRune(out, surrogateMark+unit-0xD800)
				i += 5
				continue
			}
		}
		out = append(out, data[i], data[i+1])
		i++
	}
	return out
}

func joinSurrogateText(text string) string {
	if !hasSurrogateMark(text) {
		return text
	}
	runes := []rune(text)
	out := make([]rune, 0, len(runes))
	for i := 0; i < len(runes); i++ {
		r := runes[i]
		if r < surrogateMark || r > surrogateMark+0x7FF {
			out = append(out, r)
			continue
		}
		unit := 0xD800 + r - surrogateMark
		if unit < 0xDC00 && i+1 < len(runes) && runes[i+1] >= surrogateMark+0x400 && runes[i+1] <= surrogateMark+0x7FF {
			low := 0xD800 + runes[i+1] - surrogateMark
			out = append(out, 0x10000+(unit-0xD800)<<10+(low-0xDC00))
			i++
			continue
		}
		out = append(out, utf8.RuneError)
	}
	return string(out)
}

func hasSurrogateMark(text string) bool {
	for _, r := range text {
		if r >= surrogateMark && r <= surrogateMark+0x7FF {
			return true
		}
	}
	return false
}

// joinSurrogates reads the surrogates of an expanded case: pairs joined, lone halves as U+FFFD.
func joinSurrogates(value jsonv.Value) jsonv.Value {
	switch v := value.(type) {
	case string:
		return joinSurrogateText(v)
	case []any:
		for i, item := range v {
			v[i] = joinSurrogates(item)
		}
		return v
	case *jsonv.Object:
		out := jsonv.NewObject()
		for i := 0; i < v.Len(); i++ {
			key, item := v.At(i)
			out.Set(joinSurrogateText(key), joinSurrogates(item))
		}
		return out
	}
	return value
}

// expand expands the compact forms the README describes.
func expand(value jsonv.Value) jsonv.Value {
	switch v := value.(type) {
	case []any:
		out := make([]any, len(v))
		for i, item := range v {
			out[i] = expand(item)
		}
		return out
	case *jsonv.Object:
		if n, ok := field(v, "$prose").(jsonv.Num); ok {
			length := int(n.Float64())
			return strings.Repeat("lorem ipsum ", (length+11)/12)[:length]
		}
		if pair, ok := field(v, "$repeat").([]any); ok {
			return strings.Repeat(pair[0].(string), int(pair[1].(jsonv.Num).Float64()))
		}
		if parts, ok := field(v, "$concat").([]any); ok {
			var out strings.Builder
			for _, part := range parts {
				out.WriteString(expand(part).(string))
			}
			return out.String()
		}
		if n, ok := field(v, "$range").(jsonv.Num); ok {
			out := make([]any, int(n.Float64()))
			for i := range out {
				out[i] = jsonv.Int(int64(i))
			}
			return out
		}
		if n, ok := field(v, "$keys").(jsonv.Num); ok {
			out := jsonv.NewObject()
			for i := 0; i < int(n.Float64()); i++ {
				out.Set(fmt.Sprintf("k%d", i), jsonv.Int(int64(i)))
			}
			return out
		}
		out := jsonv.NewObject()
		for i := 0; i < v.Len(); i++ {
			key, item := v.At(i)
			out.Set(key, expand(item))
		}
		return out
	}
	return value
}

func caseContext(c *sharedCase) *Context {
	sdk := jsonv.NewObject()
	sdk.Set("name", "lsquare")
	sdk.Set("version", "0.2.0")
	sdk.Set("language", "go")
	ctx := &Context{SDK: sdk, Patterns: ResolvePatterns(nil), MaxEventBytes: DefaultMaxEventBytes}
	if redact, ok := c.options.Get("redact"); ok {
		switch v := redact.(type) {
		case bool:
			if !v {
				ctx.Patterns = nil
			}
		case *jsonv.Object:
			var skip []Pattern
			for i := 0; i < v.Len(); i++ {
				if name, on := v.At(i); on == false {
					skip = append(skip, Pattern(name))
				}
			}
			ctx.Patterns = ResolvePatterns(skip)
		}
	}
	switch keys := field(c.options, "redactKeys").(type) {
	case string:
		ctx.RedactKeys, _ = ResolveKeys([]string{keys})
	case []any:
		var names []string
		for _, key := range keys {
			if name, ok := key.(string); ok {
				names = append(names, name)
			} else {
				names = append(names, "")
			}
		}
		ctx.RedactKeys, _ = ResolveKeys(names)
	}
	return ctx
}

// buildCase runs one case as the TypeScript SDK's cases.test.ts does.
func buildCase(t testing.TB, c *sharedCase) (*Draft, *Failure) {
	t.Helper()
	ctx := caseContext(c)
	withHook := func(draft *Draft, failed *Failure) (*Draft, *Failure) {
		if !c.hasHook || failed != nil {
			return draft, failed
		}
		given := jsonv.Clone(draft.Event).(*jsonv.Object)
		if seen, ok := asObject(joinSurrogates(expand(field(c.hook, "seen")))); ok {
			for i := 0; i < seen.Len(); i++ {
				key, want := seen.At(i)
				if got := field(given, key); canonical(got) != canonical(want) {
					checkSeen(t, c, key, got, want)
				}
			}
		}
		return AfterHook(given, draft, ctx)
	}
	if c.isTurn {
		return withHook(BuildTurn(c.turn, caseCapturedAt, ctx, c.hasHook))
	}
	record := jsonv.NewObject()
	record.Set("callId", "case-call")
	record.Set("capturedAt", caseCapturedAt)
	record.Set("toolName", "search_orders")
	record.Set("status", "completed")
	record.Set("durationMs", jsonv.Int(1))
	record.Set("ids", jsonv.NewObject())
	record.Set("diagnostics", false)
	for i := 0; i < c.call.Len(); i++ {
		key, value := c.call.At(i)
		if key == "input" || key == "result" {
			holder := jsonv.NewObject()
			holder.Set("value", value)
			value = holder
		}
		record.Set(key, value)
	}
	return withHook(BuildCall(record, ctx, c.hasHook))
}

// canonical is a value's JSON with every object's keys sorted: two values that differ only in
// the order of their keys have the same.
func canonical(value jsonv.Value) string {
	var sorted func(value jsonv.Value) jsonv.Value
	sorted = func(value jsonv.Value) jsonv.Value {
		switch v := value.(type) {
		case []any:
			out := make([]any, len(v))
			for i, item := range v {
				out[i] = sorted(item)
			}
			return out
		case *jsonv.Object:
			keys := append([]string(nil), v.Keys()...)
			sort.Strings(keys)
			out := jsonv.NewObject()
			for _, key := range keys {
				out.Set(key, sorted(field(v, key)))
			}
			return out
		}
		return value
	}
	return string(jsonv.Marshal(sorted(value)))
}

var uuidRE = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`)

// comparable is an event as the cases compare it: without its time and SDK, and with a random ID
// that looks like one replaced by the case's $uuid.
func comparable(c *sharedCase, event *jsonv.Object, problems *[]string) *jsonv.Object {
	out := jsonv.Clone(event).(*jsonv.Object)
	if got := field(out, "capturedAt"); got != caseCapturedAt {
		*problems = append(*problems, fmt.Sprintf("capturedAt is %v", got))
	}
	out.Delete("capturedAt")
	out.Delete("sdk")
	if field(c.event, "eventId") == "$uuid" {
		if id, _ := field(out, "eventId").(string); uuidRE.MatchString(id) {
			out.Set("eventId", "$uuid")
		} else {
			*problems = append(*problems, "eventId isn't a random UUID")
		}
	}
	return out
}

// What a string shows: everything but the placeholders redaction left, the marker of a cut, and
// the size a file was replaced by.
var (
	placeholderRE = regexp.MustCompile(`\[(?:EMAIL|PHONE|CARD|SECRET|IP)_[0-9]{1,9}\]`)
	cutMarkerRE   = regexp.MustCompile(`…\[(?:\+[0-9]{1,16} chars|cut)\]$`)
	binaryRE      = regexp.MustCompile(`^\[binary, [0-9]+ bytes\]$`)
)

func visibleParts(text string) []string {
	if binaryRE.MatchString(text) {
		return nil
	}
	text = cutMarkerRE.ReplaceAllString(text, "")
	var parts []string
	for _, part := range placeholderRE.Split(text, -1) {
		if part != "" {
			parts = append(parts, part)
		}
	}
	return parts
}

// showsNoMore reports whether everything got shows, want shows too, in the same order: each run
// of text between got's placeholders lies within one such run of want's.
func showsNoMore(got, want string) bool {
	hidden := strings.Join(visibleParts(want), "\x00")
	from := 0
	for _, part := range visibleParts(got) {
		at := strings.Index(hidden[from:], part)
		if at < 0 {
			return false
		}
		from += at + len(part)
	}
	return true
}

// keyShape is a key with its placeholders' numbers taken out: two SDKs may number them differently.
func keyShape(key string) string {
	return placeholderRE.ReplaceAllStringFunc(key, func(found string) string {
		return found[:strings.IndexByte(found, '_')] + "]"
	})
}

// leaks lists where got shows what want doesn't: a string showing more, a value where want has a
// placeholder, or a field want doesn't have at all.
func leaks(path string, got, want jsonv.Value, found *[]string) {
	switch g := got.(type) {
	case string:
		switch w := want.(type) {
		case string:
			if !showsNoMore(g, w) {
				*found = append(*found, fmt.Sprintf("%s shows more than expected: %s (expected %s)", path, excerpt(g), excerpt(w)))
			}
		default:
			if len(visibleParts(g)) > 0 {
				*found = append(*found, fmt.Sprintf("%s is a string where %s was expected", path, kindOf(want)))
			}
		}
	case []any:
		w, ok := want.([]any)
		if !ok {
			*found = append(*found, fmt.Sprintf("%s is a list where %s was expected", path, kindOf(want)))
			return
		}
		for i, item := range g {
			if i >= len(w) {
				*found = append(*found, fmt.Sprintf("%s[%d] isn't in the expected event", path, i))
				continue
			}
			leaks(fmt.Sprintf("%s[%d]", path, i), item, w[i], found)
		}
	case *jsonv.Object:
		w, ok := want.(*jsonv.Object)
		if !ok {
			*found = append(*found, fmt.Sprintf("%s is an object where %s was expected", path, kindOf(want)))
			return
		}
		// Keys are matched by their shape, in order: a key redacted to a placeholder has another
		// number where the SDKs read the object's keys in another order.
		byShape := map[string][]jsonv.Value{}
		for i := 0; i < w.Len(); i++ {
			key, item := w.At(i)
			byShape[keyShape(key)] = append(byShape[keyShape(key)], item)
		}
		for i := 0; i < g.Len(); i++ {
			key, item := g.At(i)
			shape := keyShape(key)
			candidates := byShape[shape]
			if len(candidates) == 0 {
				*found = append(*found, fmt.Sprintf("%s.%s isn't in the expected event", path, key))
				continue
			}
			byShape[shape] = candidates[1:]
			leaks(path+"."+key, item, candidates[0], found)
		}
	case jsonv.Num, bool:
		// A number or a flag where the expected event has a placeholder was left unredacted.
		if w, ok := want.(string); ok && placeholderRE.MatchString(w) {
			*found = append(*found, fmt.Sprintf("%s is %v where a placeholder was expected", path, got))
		}
	}
}

func kindOf(value jsonv.Value) string {
	switch value.(type) {
	case nil:
		return "nothing"
	case string:
		return "a string"
	case []any:
		return "a list"
	case *jsonv.Object:
		return "an object"
	}
	return "a value"
}

func excerpt(text string) string {
	const max = 160
	if len(text) <= max {
		return fmt.Sprintf("%q", text)
	}
	cut := max
	for cut > 0 && !utf8.RuneStart(text[cut]) {
		cut--
	}
	return fmt.Sprintf("%q… (%d bytes)", text[:cut], len(text))
}

// limitOf is the most a string at path may show, in code points; 0 for a field that isn't
// shortened: an ID, a name, metadata, the SDK's own bookkeeping.
func limitOf(kind string, path []string) int {
	if len(path) == 0 {
		return 0
	}
	last := path[len(path)-1]
	if kind == "turn" {
		switch path[0] {
		case "question", "answer", "latency", "verification":
			return LimitAgent
		case "error":
			if last == "message" {
				return LimitAgent
			}
		case "toolCalls":
			if len(path) > 2 && path[2] == "input" {
				return LimitInput
			}
			if last == "errorMessage" {
				return LimitAgent
			}
		}
		return 0
	}
	switch path[0] {
	case "toolDescription":
		return LimitDescription
	case "serverInstructions":
		return LimitInstructions
	case "intent":
		return LimitIntent
	case "input":
		return LimitInput
	case "result":
		return LimitResult
	case "error":
		if last == "message" {
			return LimitResult
		}
	case "agent":
		return limitOf("turn", path[1:])
	}
	return 0
}

// overLimit lists the strings that show more than their field's limit. A placeholder that stands
// for a value which wasn't a string comes with the quotes of its level of nesting, at most 16
// characters either side, which the limit doesn't count.
func overLimit(kind string, path []string, value jsonv.Value, found *[]string) {
	switch v := value.(type) {
	case string:
		limit := limitOf(kind, path)
		if limit == 0 {
			return
		}
		shown := 0
		for _, part := range visibleParts(v) {
			shown += utf8.RuneCountInString(part)
		}
		if shown > limit+32*len(placeholderRE.FindAllStringIndex(v, -1)) {
			*found = append(*found, fmt.Sprintf("%s shows %d characters, over its limit of %d", strings.Join(path, "."), shown, limit))
		}
	case []any:
		for i, item := range v {
			overLimit(kind, append(path, fmt.Sprint(i)), item, found)
		}
	case *jsonv.Object:
		for i := 0; i < v.Len(); i++ {
			key, item := v.At(i)
			overLimit(kind, append(path, key), item, found)
		}
	}
}

// seenDiffers are the cases whose hook got a field that isn't the one the case says it must get.
var seenDiffers = map[string]bool{}

// checkSeen checks what the hook got of a field that isn't equal to what the case says it must
// get: it must still show no more.
func checkSeen(t testing.TB, c *sharedCase, key string, got, want jsonv.Value) {
	t.Helper()
	var found []string
	leaks("hook.seen."+key, got, want, &found)
	if len(found) > 0 {
		t.Errorf("%s: the hook got more than it may:\n  %s", c.id(), strings.Join(found, "\n  "))
	}
	seenDiffers[c.id()] = true
}

type caseResult struct {
	c     *sharedCase
	event *jsonv.Object
}

func runCases(t *testing.T) []caseResult {
	t.Helper()
	var results []caseResult
	for _, c := range loadCases(t) {
		draft, failed := buildCase(t, c)
		if c.err != "" {
			if failed == nil || failed.Code != c.err {
				t.Errorf("%s: expected the error %s, got %+v", c.id(), c.err, failed)
			}
			continue
		}
		if failed != nil {
			t.Errorf("%s: dropped as %s %v", c.id(), failed.Code, failed.Paths)
			continue
		}
		results = append(results, caseResult{c: c, event: draft.Event})
	}
	return results
}

func TestSharedCases(t *testing.T) {
	results := runCases(t)
	if len(results) < 700 {
		t.Fatalf("only %d cases gave an event", len(results))
	}
	equal := 0
	different := map[string]bool{}
	for _, result := range results {
		c := result.c
		var problems []string
		got := comparable(c, result.event, &problems)
		kind, _ := field(got, "kind").(string)
		overLimit(kind, nil, got, &problems)
		if canonical(got) == canonical(c.event) && !seenDiffers[c.id()] {
			equal++
		} else {
			different[c.id()] = true
			leaks("event", got, c.event, &problems)
			if os.Getenv("LSQUARE_CASES_REPORT") != "" {
				t.Logf("DIFFERENT %s\n  got      %s\n  expected %s", c.id(), excerpt(canonical(got)), excerpt(canonical(c.event)))
			}
		}
		known := knownDifferences[c.id()]
		for _, problem := range problems {
			allowed := false
			for _, allow := range known.allow {
				allowed = allowed || problem == allow
			}
			if !allowed {
				t.Errorf("%s: %s", c.id(), problem)
			}
		}
		for _, allow := range known.allow {
			found := false
			for _, problem := range problems {
				found = found || problem == allow
			}
			if !found {
				t.Errorf("%s: knownDifferences lets it off %q, which it no longer needs", c.id(), allow)
			}
		}
	}
	for id := range different {
		if _, known := knownDifferences[id]; !known {
			t.Errorf("%s: the event isn't the expected one, and the case isn't in knownDifferences", id)
		}
	}
	for id := range knownDifferences {
		if !different[id] {
			t.Errorf("%s is in knownDifferences, but its event is the expected one", id)
		}
	}
	t.Logf("%d of %d events are equal to the expected ones; %d differ, each for a known reason", equal, len(results), len(different))
}

func TestTheIngestionAcceptsEachEvent(t *testing.T) {
	results := runCases(t)
	events := make([][]byte, len(results))
	for i, result := range results {
		events[i] = jsonv.Marshal(result.event)
	}
	for i, verdict := range contracttest.Ingestion(t, events) {
		if verdict != "valid" {
			t.Errorf("%s: the ingestion rejects the event as %s", results[i].c.id(), verdict)
		}
	}
}
