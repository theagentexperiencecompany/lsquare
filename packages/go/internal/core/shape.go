// Package core turns what a call gave into an event: shortened, redacted and within the size
// limit. It is a port of the TypeScript SDK's shape.ts, redact.ts, secret-spans.ts, normalize.ts
// and event.ts, on UTF-8 strings: the limits, markers and bookkeeping are the same, and every count
// of characters is in Unicode code points.
package core

import (
	"regexp"
	"strconv"
	"unicode/utf8"

	"github.com/theagentexperiencecompany/lsquare/packages/go/internal/jsonv"
)

const (
	// ScanBudget is the characters (code points) read per event, plus one per value visited.
	ScanBudget = 1_000_000

	LimitInput       = 16_000
	LimitAgent       = 16_000
	LimitResult      = 4_000
	LimitIntent      = 500
	LimitDescription = 1_000

	// MaxEntries is how many entries a list or an object keeps.
	MaxEntries = 100
	// MaxLevels is how deep lists and objects nest before they are left out.
	MaxLevels = 10
	// Lookahead is how far redaction reads past a cut, so a secret running into it is recognised.
	Lookahead = 8_000

	binaryMin     = 1_000
	binaryHead    = 4_096
	binaryTail    = 1_024
	dataHeaderMax = 1_024

	// CutMark ends a string cut where the scan budget ran out, so what was left isn't counted.
	CutMark = "…[cut]"
)

// A marker this SDK writes at the end of a string it cut.
var markerRE = regexp.MustCompile(`…\[(?:\+([0-9]{1,16}) chars|cut)\]$`)

// A placeholder from redaction, kept as it is under a secret key name.
var wholePlaceholderRE = regexp.MustCompile(`^\[(?:EMAIL|PHONE|CARD|SECRET|IP)_[0-9]{1,9}\]$`)

// cutString is a shortened string: window is what was kept plus what redaction reads past cut (a
// byte index), cut for a field whose limit is limit.
type cutString struct {
	window string
	cut    int
	mark   string
	limit  int
	// Already read for values under secret key names, with the lookahead this one no longer has.
	scanned bool
}

// binary is a string that held a file: only its decoded size is kept.
type binary struct{ bytes int64 }

// tooDeep is a container below the nesting limit.
type tooDeep struct{}

// secret is a value under a secret key name, replaced whole before anything else sees it. Its
// identity tells two equal secrets apart from different ones, for numbering; when it couldn't be
// read whole (known is false) it is numbered on its own.
type secret struct {
	identity string
	known    bool
}

// notJSON is what shaping gives for a value that isn't JSON at all.
type notJSON struct{}

func isNotJSON(value any) bool {
	_, ok := value.(notJSON)
	return ok
}

// state is what shaping one event shares across its fields.
type state struct {
	// What is left of the scan budget.
	remaining int
	// Something was cut, left out at a limit, or replaced by a size.
	shortened bool
	// Something couldn't be encoded as JSON and was removed.
	lossy bool
	// Shaping BeforeSend's output: a string this SDK cut keeps its marker.
	afterHook bool
	// The strings this SDK cut or rewrote before BeforeSend, as the hook got them.
	cuts map[string]int
	// This event's secret key names; nil when the secret pattern is off.
	secretKeys func(key string) bool
	// The same, while shaping a field whose keys come from the app's data; otherwise nil.
	secretKey func(key string) bool
}

func newState(secretKeys func(string) bool) *state {
	return &state{remaining: ScanBudget, secretKeys: secretKeys}
}

// advance moves count code points on from byte index from. It returns the index reached and how
// many code points were passed.
func advance(text string, from, count int) (index, passed int) {
	index = from
	for passed < count && index < len(text) {
		if text[index] < utf8.RuneSelf {
			index++
		} else {
			_, size := utf8.DecodeRuneInString(text[index:])
			index += size
		}
		passed++
	}
	return index, passed
}

func isBase64Run(text string) bool {
	for i := 0; i < len(text); i++ {
		c := text[i]
		switch {
		case c >= 'A' && c <= 'Z', c >= 'a' && c <= 'z', c >= '0' && c <= '9':
		case c == '+', c == '/', c == '-', c == '_', c == '=':
		default:
			return false
		}
	}
	return true
}

// decodedSize is the bytes a base64 payload of length characters decodes to; text ends with its
// padding (at most 2).
func decodedSize(length int, text string) int64 {
	padding := 0
	for padding < 2 && padding < length && text[len(text)-1-padding] == '=' {
		padding++
	}
	return int64(length-padding) * 3 / 4
}

func lowerASCII(c byte) byte {
	if c >= 'A' && c <= 'Z' {
		return c + 'a' - 'A'
	}
	return c
}

func hasPrefixFold(text, lower string) bool {
	if len(text) < len(lower) {
		return false
	}
	for i := 0; i < len(lower); i++ {
		if lowerASCII(text[i]) != lower[i] {
			return false
		}
	}
	return true
}

func hasSuffixFold(text, lower string) bool {
	if len(text) < len(lower) {
		return false
	}
	return hasPrefixFold(text[len(text)-len(lower):], lower)
}

// binarySize is the decoded size of a string that holds a file: any data: URI with ;base64, or a
// string of at least 1,000 characters whose first 4 KB and last 1 KB are only base64 characters.
// A heuristic: the rest isn't read. Bounded work, not charged to the scan budget.
func binarySize(text string) (int64, bool) {
	if hasPrefixFold(text, "data:") {
		// The header is looked for in the first 1,025 UTF-16 units, and the payload's length is in
		// them too, as in TypeScript: counted in bytes, a header of accented letters would hide
		// its payload from this.
		units := 5
		for comma := 5; comma < len(text) && units <= dataHeaderMax; {
			if text[comma] == ',' {
				if hasSuffixFold(text[5:comma], ";base64") {
					payload := text[comma+1:]
					return decodedSize(utf16Length(payload), payload), true
				}
				break
			}
			r, size := utf8.DecodeRuneInString(text[comma:])
			units += utf16Len(r)
			comma += size
		}
	}
	if len(text) < binaryMin {
		return 0, false
	}
	head := text
	if len(head) > binaryHead {
		head = head[:binaryHead]
	}
	if !isBase64Run(head) {
		return 0, false
	}
	if len(text) > binaryHead && !isBase64Run(text[len(text)-binaryTail:]) {
		return 0, false
	}
	return decodedSize(utf16Length(text), text), true
}

// markerTail is the end of text a marker would be in: its last bytes, from the start of a character.
func markerTail(text string) string {
	const read = 40
	if len(text) <= read {
		return text
	}
	start := len(text) - read
	for start < len(text) && !utf8.RuneStart(text[start]) {
		start++
	}
	return text[start:]
}

// isMarked reports whether a string ends with a marker this SDK writes.
func isMarked(text string) bool {
	return markerRE.MatchString(markerTail(text))
}

// shapeString shortens one string to limit code points. Within the limit it stays a string
// (charged its length); past it, a cutString keeping the first limit code points plus Lookahead
// more for redaction, marked …[+N chars] with the exact count of the rest when that fits in the
// budget, which is charged for counting it. When it doesn't fit, the string ends …[cut], and so
// does every string after it. Once the budget runs out, strings are cut where it ran out.
func shapeString(text string, limit int, st *state) any {
	if text == "" {
		return text
	}
	if st.remaining <= 0 {
		st.shortened = true
		return &cutString{window: "", cut: 0, mark: CutMark, limit: limit}
	}
	if bytes, ok := binarySize(text); ok {
		st.shortened = true
		return binary{bytes: bytes}
	}

	// After BeforeSend: a string this SDK cut, as the hook got it, is kept as it is where its limit
	// is no tighter than the one it was cut at. Any other marked string is shortened as usual,
	// adding the count in its marker to what is cut now.
	earlier, hasEarlier := "", false
	if st.afterHook {
		tail := markerTail(text)
		if loc := markerRE.FindStringIndex(tail); loc != nil {
			marked := tail[loc[0]:]
			body := text[:len(text)-len(marked)]
			if cutAt, ok := st.cuts[text]; ok && cutAt <= limit {
				st.shortened = true
				end, passed := advance(body, 0, st.remaining)
				if end == len(body) {
					st.remaining -= passed
					return &cutString{window: body, cut: len(body), mark: marked, limit: limit, scanned: true}
				}
				st.remaining = 0
				return &cutString{window: body[:end], cut: end, mark: CutMark, limit: limit, scanned: true}
			}
			earlier, hasEarlier = marked, true
			text = body
		} else if before, ok := st.cuts[text]; ok && before <= limit && before <= st.remaining {
			// A string whose secret values were replaced before the hook is kept whole where its
			// length before that fits: placeholders may have made it longer than that.
			st.remaining -= before
			return text
		}
	}

	readable := limit
	if st.remaining < readable {
		readable = st.remaining
	}
	cut, kept := advance(text, 0, readable)
	if cut == len(text) {
		st.remaining -= kept
		if !hasEarlier {
			return text
		}
		return &cutString{window: text, cut: cut, mark: earlier, limit: limit}
	}
	st.shortened = true
	// Redaction reads Lookahead past every cut, also where the budget runs out, so a token running
	// into the cut is matched whole.
	end, ahead := advance(text, cut, Lookahead)
	if kept < limit || kept+ahead > st.remaining {
		// The budget ran out inside this string, or inside what redaction reads past the cut.
		st.remaining = 0
		return &cutString{window: text[:end], cut: cut, mark: CutMark, limit: limit}
	}
	st.remaining -= kept + ahead
	rest, counted := ahead, true
	if end < len(text) {
		last, more := advance(text, end, st.remaining+1)
		if last == len(text) && more <= st.remaining {
			st.remaining -= more
			rest += more
		} else {
			st.remaining = 0
			counted = false
		}
	}
	return &cutString{window: text[:end], cut: cut, mark: combine(rest, counted, earlier, hasEarlier), limit: limit}
}

// combine is the marker for rest characters cut now (not counted: the plain mark), plus a marker
// from before.
func combine(rest int, counted bool, earlier string, hasEarlier bool) string {
	if !counted {
		return CutMark
	}
	if !hasEarlier {
		return "…[+" + strconv.Itoa(rest) + " chars]"
	}
	groups := markerRE.FindStringSubmatch(earlier)
	if groups == nil || groups[1] == "" {
		return CutMark
	}
	// Added as JavaScript adds numbers, and written as it writes them: a marker a hook made up
	// can hold a count past 2^53.
	before, err := strconv.ParseFloat(groups[1], 64)
	if err != nil {
		return CutMark
	}
	total, _ := jsonv.Float(float64(rest) + before)
	return "…[+" + string(total) + " chars]"
}

// plain is shaped JSON as plain JSON, without redaction: a value's identity under a secret key,
// and output with redaction off.
func plain(value any) jsonv.Value {
	switch v := value.(type) {
	case *cutString:
		return v.window[:v.cut] + v.mark
	case binary:
		out := jsonv.NewObject()
		out.Set("omitted", "binary")
		out.Set("bytes", jsonv.Int(v.bytes))
		return out
	case tooDeep:
		out := jsonv.NewObject()
		out.Set("omitted", "depth")
		return out
	case *secret:
		return "[SECRET]"
	case []any:
		out := make([]any, len(v))
		for i, item := range v {
			out[i] = plain(item)
		}
		return out
	case *jsonv.Object:
		out := jsonv.NewObject()
		for i := 0; i < v.Len(); i++ {
			key, item := v.At(i)
			out.Set(key, plain(item))
		}
		return out
	default:
		return value
	}
}

// secretValue is a value under a secret key name, read within the limits but never shortened: its
// identity is the raw value. A string is read up to the field's limit; anything else is converted,
// and counts only if nothing in it was cut, left out or turned into a size. Values that can't hold
// a secret (null, booleans, values empty as given) and placeholders stay as they are.
func secretValue(value jsonv.Value, limit int, st *state, level int) any {
	switch v := value.(type) {
	case nil:
		return nil
	case bool:
		return v
	case string:
		if v == "" {
			return v
		}
		return secretString(v, limit, st)
	}
	shortened, lossy, secretKey := st.shortened, st.lossy, st.secretKey
	st.shortened, st.lossy = false, false
	// Read as a whole: secret key names inside it don't matter.
	st.secretKey = nil
	converted := convert(value, limit, st, level)
	exact := !st.shortened && !st.lossy
	st.shortened, st.lossy, st.secretKey = shortened, lossy, secretKey
	if isNotJSON(converted) {
		return converted
	}
	// Empty as it was given, not made empty by the budget or by leaving out what JSON can't hold.
	if exact {
		switch c := converted.(type) {
		case []any:
			if len(c) == 0 {
				return converted
			}
		case *jsonv.Object:
			if c.Len() == 0 {
				return converted
			}
		}
		return &secret{identity: string(jsonv.Marshal(plain(converted))), known: true}
	}
	return &secret{}
}

// secretString is a string under a secret key name: its raw value, read up to limit, or a number
// of its own.
func secretString(value string, limit int, st *state) any {
	if value == "" || (len(value) <= 20 && wholePlaceholderRE.MatchString(value)) {
		return value
	}
	read := limit
	if st.remaining < read {
		read = st.remaining
	}
	end, passed := advance(value, 0, read)
	st.remaining -= passed
	if end == len(value) {
		return &secret{identity: value, known: true}
	}
	return &secret{}
}

// shapeJSON converts a value into shaped JSON without reading more than the limits allow: lists
// and objects keep their first 100 entries, nesting stops at 10 levels, and strings are shortened
// to limit. Objects are read in the order their keys came in. Under a secret key name, when
// st.secretKey is set, a value becomes a secret. Parts JSON can't hold are removed and the state
// marked lossy; a value that is itself not JSON gives notJSON.
func shapeJSON(value jsonv.Value, limit int, st *state) any {
	return convert(value, limit, st, 0)
}

// spend charges cost to the scan budget. Past it, false: the budget is used up, and the value
// marked shortened.
func spend(st *state, cost int) bool {
	if st.remaining < cost {
		st.remaining = 0
		st.shortened = true
		return false
	}
	st.remaining -= cost
	return true
}

func convert(value jsonv.Value, limit int, st *state, level int) any {
	switch v := value.(type) {
	case nil:
		return nil
	case string:
		return shapeString(v, limit, st)
	case bool:
		return v
	case jsonv.Num:
		return v
	case jsonv.Blob:
		st.shortened = true
		return binary{bytes: v.Bytes}
	case []any:
		if level >= MaxLevels {
			st.shortened = true
			return tooDeep{}
		}
		if len(v) > MaxEntries {
			st.shortened = true
		}
		end := len(v)
		if end > MaxEntries {
			end = MaxEntries
		}
		out := make([]any, 0, end)
		for index := 0; index < end; index++ {
			if !spend(st, 1) {
				break
			}
			converted := convert(v[index], limit, st, level+1)
			if isNotJSON(converted) {
				st.lossy = true
				out = append(out, nil)
			} else {
				out = append(out, converted)
			}
		}
		return out
	case *jsonv.Object:
		if v == nil {
			return nil
		}
		if level >= MaxLevels {
			st.shortened = true
			return tooDeep{}
		}
		out := jsonv.NewObject()
		kept := 0
		for index := 0; index < v.Len(); index++ {
			key, item := v.At(index)
			if kept >= MaxEntries {
				st.shortened = true
				break
			}
			// A key is read whole, never cut: it costs its length, and one that doesn't fit ends
			// the object.
			_, length := advance(key, 0, st.remaining)
			if !spend(st, 1+length) {
				break
			}
			if key == "__proto__" {
				// Never sent: what reads the event in JavaScript must not take it for a prototype.
				st.lossy = true
				continue
			}
			var converted any
			if st.secretKey != nil && st.secretKey(key) {
				converted = secretValue(item, limit, st, level+1)
			} else {
				converted = convert(item, limit, st, level+1)
			}
			if isNotJSON(converted) {
				st.lossy = true
				continue
			}
			out.Set(key, converted)
			kept++
		}
		return out
	default:
		return notJSON{}
	}
}
