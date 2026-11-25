package core

import (
	"regexp"
	"sort"
	"strings"
	"unicode/utf16"
	"unicode/utf8"
)

// Finds the values under secret key names in JSON held in a string: a text block, an error quoting
// a response, a stringified argument. They are found by scanning, not by parsing the string: it
// may be cut, or be JSON inside prose or a code fence, and rewriting it would change what isn't a
// secret. Only the spans of the values are found; everything else stays as it was.
//
// A port of the TypeScript SDK's secret-spans.ts, line for line, and like it and the Python SDK's
// it reads UTF-16 code units: every bounded read (a key's last 256 units, a quote looked for 256
// units on) then ends where theirs does, whatever the text holds. Counted in bytes, a key such as
// "pass" + 90 CJK characters + "word" would be read from its 256th-last byte, past "pass", and
// its value kept. The spans it returns are byte offsets in the string given.
//
// Its work is linear: each piece of text between two unescaped quotes is read a fixed number of
// times per level of nesting, and nesting stops at maxNesting.

// secretSpan is a value to replace. start and end are byte offsets in the string given.
type secretSpan struct {
	start, end int
	// What makes it "the same value" for numbering. Not known: it couldn't be read whole, so it is
	// numbered on its own.
	identity string
	known    bool
	// What goes either side of its placeholder: nothing inside a string's quotes, otherwise a quote
	// at its level of nesting.
	quote string
}

const (
	// How many times a string may hold JSON in a string. Past this, a string still holding a key
	// is replaced whole.
	maxNesting = 4
	// The longest whole placeholder: [SECRET_ + 9 digits + ].
	longestPlaceholder = 18
	maxCodePoint       = 0x10ffff
	// A name is matched by its end, so only this much of a long key is read.
	keyRead = 256
	// How far past its start a value printed by pydantic is looked for its end.
	inputRead = 1024
	// How many times a URL held in a URL is read: a redirect chain encodes it again each time.
	maxPercent = 3
	// How far past a closing quote a run of what ends a sentence is read.
	sentenceRead = 5
	// How far past a Python string's first quote a quote that can end it is looked for.
	quoteRead = 256
	// What containerEnd gives for a container with a string in other quotes: it's not JSON.
	notJSONContainer = -2
	// No position.
	none = -1
)

// What text ending in isn't a key, unless it starts an entry of an object.
const notAKeyEnd = " \t=([+?,.<|&"

// What a sentence or code can have after a closing quote, before a space or a line's end.
const sentenceEnds = ".;)'`"

// What a query value stops at: where a URL ends in text, or the next parameter.
const queryValueEnd = " \t\r\n\f\v&#\"'<>`)]}\\"

const (
	inputValueWord = "input_value="
	inputTypeWord  = ", input_type="
	pemStart       = "-----BEGIN "
)

var jsonLiterals = []string{"true", "false", "null"}
var pythonLiterals = []string{"None", "True", "False", "true", "false", "null"}

var (
	// Parameters that hold secrets only in a URL: a signed URL's, and an OAuth or Azure Functions code.
	urlOnlyNames = regexp.MustCompile(`(?i)(?:^|[^A-Za-z0-9])(?:sig|signature|hmac)$|^(?:code|code_verifier)$`)
	// A query value that is a template, not a value: {token}, {{token}}, $TOKEN, ${TOKEN}.
	queryTemplate = regexp.MustCompile(`^(?:\{[A-Za-z0-9_.-]+\}|\{\{ ?[A-Za-z0-9_.-]+ ?\}\}|\$\{[A-Za-z0-9_.-]+\}|\$[A-Z][A-Z0-9_]*)`)
	// A key at the nesting limit: a quote, whitespace and a colon, each as it is or encoded any number of times.
	encodedKey = regexp.MustCompile(`(?:"|\\u0022)(?:[ \t\r\n]|\\+[nrt]|\\+u00(?:20|09|0[aAdD]))*(?::|\\+u003[aA])`)
)

// ScanWork is how much scanning one event may do. A unit is roughly one quote or bracket read, one
// backslash or percent sign decoded, one JSON value checked, one key looked at, or 16 characters
// decoded or of a parameter's name. So a crafted string can't hold the app up for long, where
// ordinary text doesn't come near it. Past this, a string mayHoldKey lets through is replaced
// whole, as nothing unread may be sent.
const ScanWork = 1_000_000

// scanWork is what's left of ScanWork for an event.
type scanWork struct{ left int }

type overWork struct{}

func spendWork(work *scanWork, amount int) {
	work.left -= amount
	if work.left < 0 {
		panic(overWork{})
	}
}

// reading is what one call of secretSpans shares.
type reading struct {
	isSecretKey func(key string) bool
	spans       []unitSpan
	work        *scanWork
	// A decoded view where a container didn't close with its line ends as escapes: the rest are
	// read with them as line ends.
	linesIn *view
}

// unitSpan is a span in UTF-16 units of the string given.
type unitSpan struct {
	start, end int
	identity   string
	known      bool
	quote      string
}

// view is a string to scan, and where each of its positions is in the string given. The string
// itself has no map.
type view struct {
	text []uint16
	pos  []int32
}

// mayHoldKey reports whether a string could hold a secret value at all: the cheap check before
// scanning it. It needs a quote and a colon or =>, as they are or encoded any number of times; or
// an =, as it is or percent-encoded, and what can start a URL's parameter; or pydantic's
// input_value=.
func mayHoldKey(text string) bool {
	if (strings.IndexByte(text, '"') >= 0 || strings.IndexByte(text, '\'') >= 0 || strings.Contains(text, "u0022")) &&
		(strings.IndexByte(text, ':') >= 0 || strings.Contains(text, "=>") || strings.Contains(text, "u003a") || strings.Contains(text, "u003A")) {
		return true
	}
	if strings.IndexByte(text, '%') >= 0 && percentEqualsString(text) {
		return true
	}
	return strings.IndexByte(text, '=') >= 0 &&
		(strings.IndexByte(text, '?') >= 0 || strings.IndexByte(text, '&') >= 0 || strings.IndexByte(text, '#') >= 0 || strings.Contains(text, inputValueWord))
}

// percentEqualsString is %(?:25)*3[Dd] in a string: an = percent-encoded any number of times.
func percentEqualsString(text string) bool {
	for i := strings.IndexByte(text, '%'); i >= 0 && i < len(text); {
		j := i + 1
		for j+1 < len(text) && text[j] == '2' && text[j+1] == '5' {
			j += 2
		}
		if j+1 < len(text) && text[j] == '3' && (text[j+1] == 'D' || text[j+1] == 'd') {
			return true
		}
		next := strings.IndexByte(text[i+1:], '%')
		if next < 0 {
			break
		}
		i += 1 + next
	}
	return false
}

func percentEquals(text []uint16) bool {
	for i := 0; i < len(text); i++ {
		if text[i] != '%' {
			continue
		}
		j := i + 1
		for j+1 < len(text) && text[j] == '2' && text[j+1] == '5' {
			j += 2
		}
		if j+1 < len(text) && text[j] == '3' && (text[j+1] == 'D' || text[j+1] == 'd') {
			return true
		}
	}
	return false
}

func toUnits(text string) []uint16 {
	ascii := true
	for i := 0; i < len(text); i++ {
		if text[i] >= utf8.RuneSelf {
			ascii = false
			break
		}
	}
	if ascii {
		units := make([]uint16, len(text))
		for i := 0; i < len(text); i++ {
			units[i] = uint16(text[i])
		}
		return units
	}
	return utf16.Encode([]rune(text))
}

// fromUnits is the string the units make. Half of a surrogate pair without the other, which an
// escape can write, becomes three bytes that are no character's, each half its own: so two
// values that differ only there are still two, where U+FFFD would make them one.
func fromUnits(units []uint16) string {
	ascii := true
	for _, unit := range units {
		if unit >= utf8.RuneSelf {
			ascii = false
			break
		}
	}
	if ascii {
		out := make([]byte, len(units))
		for i, unit := range units {
			out[i] = byte(unit)
		}
		return string(out)
	}
	out := make([]byte, 0, len(units)+len(units)/2)
	for i := 0; i < len(units); i++ {
		unit := rune(units[i])
		switch {
		case unit < 0xD800 || unit > 0xDFFF:
			out = utf8.AppendRune(out, unit)
		case unit < 0xDC00 && i+1 < len(units) && units[i+1] >= 0xDC00 && units[i+1] <= 0xDFFF:
			out = utf8.AppendRune(out, utf16.DecodeRune(unit, rune(units[i+1])))
			i++
		default:
			out = append(out, 0xED, byte(0x80|(unit>>6)&0x3F), byte(0x80|unit&0x3F))
		}
	}
	return string(out)
}

// byteOffsets maps each UTF-16 unit index of text, and its end, to a byte offset. Nil when they
// are the same.
func byteOffsets(text string, units int) []int32 {
	if units == len(text) {
		return nil
	}
	offsets := make([]int32, 0, units+1)
	for i, r := range text {
		offsets = append(offsets, int32(i))
		if r >= 0x10000 {
			// The second half of a pair: no character starts there.
			offsets = append(offsets, int32(i))
		}
	}
	return append(offsets, int32(len(text)))
}

// secretSpans returns the values under secret key names in text, in order and not overlapping.
func secretSpans(text string, isSecretKey func(string) bool, work *scanWork) (spans []secretSpan) {
	units := toUnits(text)
	r := &reading{isSecretKey: isSecretKey, work: work}
	over := false
	func() {
		defer func() {
			if recovered := recover(); recovered != nil {
				if _, ok := recovered.(overWork); !ok {
					panic(recovered)
				}
				over = true
			}
		}()
		read(&view{text: units}, 0, false, r)
	}()
	if over {
		return []secretSpan{{start: 0, end: len(text)}}
	}
	found := merged(r.spans)
	offsets := byteOffsets(text, len(units))
	at := func(unit int) int {
		if offsets == nil {
			return unit
		}
		return int(offsets[unit])
	}
	spans = make([]secretSpan, len(found))
	for i, span := range found {
		spans[i] = secretSpan{start: at(span.start), end: at(span.end), identity: span.identity, known: span.known, quote: span.quote}
	}
	return withWholeKeys(text, spans)
}

// withWholeKeys widens a span over a private key block it runs into: a value can stop at the
// block's first line, which would leave the block without the BEGIN its pattern looks for.
func withWholeKeys(text string, spans []secretSpan) []secretSpan {
	if len(spans) == 0 || !strings.Contains(text, pemStart) {
		return spans
	}
	out := append([]secretSpan(nil), spans...)
	index := 0
	for from := 0; ; {
		start, end, ok := findPEMBlock(text, from)
		if !ok {
			break
		}
		for index < len(spans) && spans[index].end <= start {
			index++
		}
		if index < len(spans) && spans[index].start < end {
			out = append(out, secretSpan{start: start, end: end})
		}
		from = end
	}
	if len(out) == len(spans) {
		return spans
	}
	asUnits := make([]unitSpan, len(out))
	for i, span := range out {
		asUnits[i] = unitSpan(span)
	}
	again := merged(asUnits)
	result := make([]secretSpan, len(again))
	for i, span := range again {
		result[i] = secretSpan(span)
	}
	return result
}

func merged(spans []unitSpan) []unitSpan {
	sort.SliceStable(spans, func(a, b int) bool {
		if spans[a].start != spans[b].start {
			return spans[a].start < spans[b].start
		}
		return spans[a].end > spans[b].end
	})
	out := make([]unitSpan, 0, len(spans))
	for _, span := range spans {
		if len(out) == 0 || span.start >= out[len(out)-1].end {
			out = append(out, span)
		} else if last := out[len(out)-1]; span.end > last.end {
			out[len(out)-1] = unitSpan{start: last.start, end: span.end, quote: last.quote}
		}
	}
	return out
}

// Reading units as JavaScript reads a string: past its ends there is nothing, which equals no
// character.

func unitAt(text []uint16, index int) int {
	if index < 0 || index >= len(text) {
		return none
	}
	return int(text[index])
}

func startsWith(text []uint16, literal string, at int) bool {
	if at < 0 {
		at = 0
	}
	if at+len(literal) > len(text) {
		return false
	}
	for i := 0; i < len(literal); i++ {
		if text[at+i] != uint16(literal[i]) {
			return false
		}
	}
	return true
}

func indexOfUnit(text []uint16, unit uint16, from int) int {
	if from < 0 {
		from = 0
	}
	for i := from; i < len(text); i++ {
		if text[i] == unit {
			return i
		}
	}
	return none
}

func indexOfLiteral(text []uint16, literal string, from int) int {
	if from < 0 {
		from = 0
	}
	first := uint16(literal[0])
	for i := from; i+len(literal) <= len(text); i++ {
		if text[i] == first && startsWith(text, literal, i) {
			return i
		}
	}
	return none
}

// inSet reports whether the unit at index is one of set's characters. Past the text's ends it is:
// JavaScript's "".includes("") holds.
func inSet(set string, text []uint16, index int) bool {
	if index < 0 || index >= len(text) {
		return true
	}
	unit := text[index]
	return unit < utf8.RuneSelf && strings.IndexByte(set, byte(unit)) >= 0
}

func isDigitUnit(unit int) bool  { return unit >= '0' && unit <= '9' }
func isLetterUnit(unit int) bool { return (unit >= 'A' && unit <= 'Z') || (unit >= 'a' && unit <= 'z') }
func isWordUnit(unit int) bool   { return isDigitUnit(unit) || isLetterUnit(unit) }

// isSpaceUnit is JSON's whitespace.
func isSpaceUnit(unit int) bool {
	return unit == ' ' || unit == '\t' || unit == '\n' || unit == '\r'
}

// endsBare is what ends a value that isn't JSON: so "password": correct horse battery is masked whole.
func endsBare(unit int) bool {
	return unit == '\n' || unit == '\r' || unit == ',' || unit == '}' || unit == ']'
}

func skipSpace(text []uint16, from int) int {
	index := from
	for index < len(text) && isSpaceUnit(int(text[index])) {
		index++
	}
	return index
}

func skipBlank(text []uint16, from int) int {
	index := from
	for index < len(text) && (text[index] == ' ' || text[index] == '\t') {
		index++
	}
	return index
}

func position(v *view, index int) int {
	if v.pos == nil {
		return index
	}
	return int(v.pos[index])
}

func (r *reading) add(v *view, start, end int, identity string, known bool, quote string) {
	r.spans = append(r.spans, unitSpan{start: position(v, start), end: position(v, end), identity: identity, known: known, quote: quote})
}

// read scans a view every way a value can be written in it. Python's repr and Node's util.inspect
// double JSON's backslashes and leave its quotes, so \\" reads as the end of a string. Decoded
// once, it's JSON again. So a string that isn't JSON and holds a backslash is read a second time
// decoded, once for each chain of nesting, which keeps the work linear.
func read(v *view, depth int, printed bool, r *reading) {
	scan(v, depth, printed, r)
	scanSingleQuoted(v, r)
	scanInputValues(v, r)
	scanQueries(v, r)
	if printed || depth == maxNesting || indexOfUnit(v.text, '\\', 0) == none || isValidJSON(v.text, r.work) {
		return
	}
	decoded := decode(v, 0, len(v.text), r.work)
	if len(decoded.text) < len(v.text) {
		read(decoded, depth+1, true, r)
	}
}

// isValidJSON reports whether the text is one JSON value.
func isValidJSON(text []uint16, work *scanWork) bool {
	var open []int
	index := skipSpace(text, 0)
	for {
		spendWork(work, 1)
		unit := unitAt(text, index)
		end := none
		if unit == '{' || unit == '[' {
			inner := skipSpace(text, index+1)
			if unitAt(text, inner) != unit+2 {
				open = append(open, unit)
				if unit == '{' {
					index = jsonKeyEnd(text, inner)
				} else {
					index = inner
				}
				if index == none {
					return false
				}
				continue
			}
			end = inner + 1
		} else {
			end = jsonScalarEnd(text, index)
		}
		if end == none {
			return false
		}
		index = skipSpace(text, end)
		for len(open) > 0 && unitAt(text, index) == open[len(open)-1]+2 {
			open = open[:len(open)-1]
			index = skipSpace(text, index+1)
		}
		if len(open) == 0 {
			return index == len(text)
		}
		if unitAt(text, index) != ',' {
			return false
		}
		index = skipSpace(text, index+1)
		if open[len(open)-1] == '{' {
			index = jsonKeyEnd(text, index)
		}
		if index == none {
			return false
		}
	}
}

// jsonKeyEnd is where the value after the key at `at` starts.
func jsonKeyEnd(text []uint16, at int) int {
	if unitAt(text, at) != '"' {
		return none
	}
	end := jsonStringEnd(text, at+1)
	if end == none {
		return none
	}
	colon := skipSpace(text, end)
	if unitAt(text, colon) == ':' {
		return skipSpace(text, colon+1)
	}
	return none
}

func jsonScalarEnd(text []uint16, at int) int {
	if unitAt(text, at) == '"' {
		return jsonStringEnd(text, at+1)
	}
	for _, literal := range jsonLiterals {
		if at >= 0 && startsWith(text, literal, at) {
			return at + len(literal)
		}
	}
	if end := jsonNumberEnd(text, at); end > at {
		return end
	}
	return none
}

func jsonStringEnd(text []uint16, from int) int {
	for index := from; index < len(text); index++ {
		unit := int(text[index])
		if unit == '"' {
			return index + 1
		}
		if unit < ' ' {
			return none
		}
		if unit == '\\' {
			index++
			if unitAt(text, index) == 'u' {
				if !hasHex(text, index+1, 4, len(text)) {
					return none
				}
				index += 4
			} else if _, ok := escapedChar(unitAt(text, index)); !ok {
				return none
			}
		}
	}
	return none
}

// jsonNumberEnd is the end of the JSON number at `at`, or `at` when none is there.
func jsonNumberEnd(text []uint16, at int) int {
	index := at
	if unitAt(text, index) == '-' {
		index++
	}
	switch first := unitAt(text, index); {
	case first == '0':
		index++
	case first >= '1' && first <= '9':
		index++
		for isDigitUnit(unitAt(text, index)) {
			index++
		}
	default:
		return at
	}
	if unitAt(text, index) == '.' && isDigitUnit(unitAt(text, index+1)) {
		index += 2
		for isDigitUnit(unitAt(text, index)) {
			index++
		}
	}
	if e := unitAt(text, index); e == 'e' || e == 'E' {
		exponent := index + 1
		if sign := unitAt(text, exponent); sign == '+' || sign == '-' {
			exponent++
		}
		if isDigitUnit(unitAt(text, exponent)) {
			exponent++
			for isDigitUnit(unitAt(text, exponent)) {
				exponent++
			}
			index = exponent
		}
	}
	return index
}

// scan reads JSON's keys. Every piece of text before an unescaped quote is a candidate, whether
// it really is a string or lies between two of them. A key is one that a colon follows. So a
// stray quote in prose can't put the scan out of step, and a value holding quotes needs no
// special case.
func scan(v *view, depth int, printed bool, r *reading) {
	text := v.text
	start := 0
	for {
		spendWork(r.work, 1)
		end, escaped := lex(text, start)
		// A key is read too: hiding a value in JSON used as a key is worth two such keys coming out the same.
		if escaped {
			nest(v, start, end, depth, printed, r)
		}
		if end >= len(text) {
			return
		}
		colon := skipSpace(text, end+1)
		arrow := startsWith(text, "=>", colon)
		if (arrow || (unitAt(text, colon) == ':' && unitAt(text, colon+1) != ':')) && isKey(v, start, end, colon, escaped, r) {
			level := depth
			if printed {
				level = depth - 1
			}
			skip := 1
			if arrow {
				skip = 2
			}
			next := readValue(v, skipSpace(text, colon+skip), level, startsEntry(text, start), r)
			if next == none {
				return
			}
			start = next
		} else {
			start = end + 1
		}
	}
}

// keyName is the end of a key, as far as a name is read.
func keyName(key []uint16) string {
	if len(key) > keyRead {
		key = key[len(key)-keyRead:]
	}
	return fromUnits(key)
}

// isKey reports whether the piece is a key with a secret key name. What's before a quote can be
// code or a sentence, as in cond ? "token" : "x", but not where an object has a key: there, JSON's
// keys are read as they are.
func isKey(v *view, start, end, colon int, escaped bool, r *reading) bool {
	if !r.isSecretKey(keyName(body(v, start, end, escaped, r.work))) {
		return false
	}
	text := v.text
	before := beforeKey(text, start)
	unit := unitAt(text, before)
	if unit == '?' && (before == start-2 || colon > end+1) {
		return false
	}
	if !inSet(notAKeyEnd, text, end-1) || unit == '{' || unit == ',' || unit == '\n' || unit == '\r' {
		return true
	}
	return before < 0 && unitAt(text, start-1) == '"'
}

// startsEntry reports whether a value that isn't a quoted string can follow this key. Not when the
// key comes straight after a word or an operator, where it is code or a sentence quoting
// something: case "token": return x, x == "token": y, "users"."token"::text. A line number, a
// timestamp or a colour code isn't a word: a word is two letters.
func startsEntry(text []uint16, start int) bool {
	index := beforeKey(text, start)
	unit := unitAt(text, index)
	previous := unitAt(text, index-1)
	if unit == '=' || (isLetterUnit(unit) && isLetterUnit(previous)) {
		return false
	}
	// -> "Token":, not a quoted line's > "token":.
	if unit == '>' {
		return previous != '-' && previous != '='
	}
	// Failed. "token": and "users"."token":, not a list's 1. "token": or an elided ..."token":.
	return unit != '.' || !(isLetterUnit(previous) || previous == '"')
}

// beforeKey is where what's before the quote opening the piece at start is, past its escapes,
// spaces and tabs. -1: nothing is, or no quote opens it, as in a string cut in its key.
func beforeKey(text []uint16, start int) int {
	if unitAt(text, start-1) != '"' {
		return none
	}
	index := start - 2
	for index >= 0 && (text[index] == ' ' || text[index] == '\t' || text[index] == '\\') {
		index--
	}
	return index
}

// scanSingleQuoted reads Python's repr of a dict, which is how pydantic's errors quote an input,
// and other text with single-quoted keys: {'password': 'x', 'note': "it's"}, and
// Map { 'password' => 'x' }. Any two quotes in a row can hold a key, whatever is between them, so
// an apostrophe can't put the scan out of step. Only one that opens an entry is a key, so a
// sentence's the 'token': 'x' isn't, and a SQL cast's 'token'::text isn't either.
func scanSingleQuoted(v *view, r *reading) {
	text := v.text
	open := indexOfUnit(text, '\'', 0)
	for open != none {
		closing := indexOfUnit(text, '\'', open+1)
		if closing == none {
			return
		}
		after := skipBlank(text, closing+1)
		arrow := startsWith(text, "=>", after)
		if (arrow || (unitAt(text, after) == ':' && unitAt(text, after+1) != ':')) && opensEntry(text, open) {
			spendWork(r.work, 1)
			from := open + 1
			if closing-keyRead > from {
				from = closing - keyRead
			}
			if r.isSecretKey(fromUnits(text[from:closing])) {
				skip := 1
				if arrow {
					skip = 2
				}
				next := readPythonValue(v, skipSpace(text, after+skip), r)
				if next == none {
					return
				}
				open = indexOfUnit(text, '\'', next)
				continue
			}
		}
		open = closing
	}
}

// opensEntry reports whether the quote at open, or the b or u before it, comes straight after {,
// ( or ,, or at the start of a line or the text.
func opensEntry(text []uint16, open int) bool {
	index := open - 1
	if u := unitAt(text, index); u == 'b' || u == 'u' {
		index--
	}
	for index >= 0 && (text[index] == ' ' || text[index] == '\t') {
		index--
	}
	if index < 0 {
		return true
	}
	unit := text[index]
	return unit == '{' || unit == '(' || unit == ',' || unit == '\n' || unit == '\r'
}

// pythonQuoteAt is where a string in Python's quotes starts at or after a b or u at `at`, or none.
func pythonQuoteAt(text []uint16, at int) int {
	quoteAt := at
	if u := unitAt(text, at); u == 'b' || u == 'u' {
		quoteAt = at + 1
	}
	if unit := unitAt(text, quoteAt); unit == '\'' || unit == '"' || unit == '`' {
		return quoteAt
	}
	return none
}

// readPythonValue adds the span of the value at `at`, as Python or Node's util.inspect writes one.
// It returns where scanning goes on; none: it ran to the end of the text.
func readPythonValue(v *view, at int, r *reading) int {
	text := v.text
	if at >= len(text) {
		return at
	}
	if quoteAt := pythonQuoteAt(text, at); quoteAt != none {
		return readPythonString(v, quoteAt, r)
	}
	// An escaped quote is read decoded, where it is a string's: \\"it's\\", in JSON holding a repr.
	if text[at] == '\\' {
		return at
	}
	if bracket := pythonBracketAt(text, at); bracket != none {
		closing := pythonContainersEnd(v, bracket, r)
		if closing == none {
			r.add(v, at, len(text), "", false, "'")
			return none
		}
		if (bracket == at && skipSpace(text, at+1) == closing-1) || isPlaceholder(v, at, closing) {
			return closing
		}
		if !endsToken(text, closing) {
			return bareValue(v, at, closing, "'", r)
		}
		r.add(v, at, closing, fromUnits(text[at:closing]), true, "'")
		return closing
	}
	// What util.inspect and repr write in angle brackets: <Buffer 68 69> holds a value's bytes.
	if text[at] == '<' {
		limit := at + keyRead
		if limit > len(text) {
			limit = len(text)
		}
		closing := none
		for i := at; i < limit; i++ {
			if text[i] == '>' {
				closing = i - at
				break
			}
		}
		if closing != none && endsToken(text, at+closing+1) {
			r.add(v, at, at+closing+1, fromUnits(text[at:at+closing+1]), true, "'")
			return at + closing + 1
		}
	}
	for _, literal := range pythonLiterals {
		if startsWith(text, literal, at) && endsToken(text, at+len(literal)) {
			return at + len(literal)
		}
	}
	if number := jsonNumberEnd(text, at); number > at && endsToken(text, number) {
		r.add(v, at, number, fromUnits(text[at:number]), true, "'")
		return number
	}
	// One word, as {'token': token} has. More than that is a sentence: , 'password': explains it.
	limit := at + keyRead
	if limit > len(text) {
		limit = len(text)
	}
	end := at
	for end < limit && !endsBare(int(text[end])) && !isSpaceUnit(int(text[end])) {
		end++
	}
	if end < limit && !endsToken(text, end) {
		return at
	}
	if end > at {
		return bareValue(v, at, at, "'", r)
	}
	return at
}

// readPythonString adds the span of the string whose opening quote is at quoteAt, and of what
// continues it: pprint writes a long value as adjacent strings, and util.inspect joins them with
// +. It returns where scanning goes on.
func readPythonString(v *view, quoteAt int, r *reading) int {
	text := v.text
	first := int(text[quoteAt])
	end, escaped := pythonStringEnd(v, quoteAt+1, first, false, r.work, false)
	last := end
	lastQuote := first
	for {
		if unitAt(text, last) != lastQuote {
			break
		}
		next := skipSpace(text, last+1)
		if unitAt(text, next) == '+' {
			next = skipSpace(text, next+1)
		}
		piece := pythonQuoteAt(text, next)
		if piece == none {
			break
		}
		quote := int(text[piece])
		ended, _ := pythonStringEnd(v, piece+1, quote, true, r.work, false)
		if unitAt(text, ended) == quote && unitAt(text, skipBlank(text, ended+1)) == ':' {
			break
		}
		last = ended
		lastQuote = quote
	}
	closed := unitAt(text, last) == lastQuote
	if last == end && isPlaceholder(v, quoteAt+1, end) {
		if closed {
			return end + 1
		}
		return end
	}
	// Pieces with different quotes: their placeholder takes one kind, so that they stay balanced.
	if closed && lastQuote != first {
		r.add(v, quoteAt, last+1, "", false, "'")
		return last + 1
	}
	if last > quoteAt+1 {
		if closed && last == end {
			r.add(v, quoteAt+1, last, fromUnits(body(v, quoteAt+1, end, escaped, r.work)), true, "")
		} else {
			r.add(v, quoteAt+1, last, "", false, "")
		}
	}
	if closed {
		return last + 1
	}
	return last
}

// pythonStringEnd is where a string in Python's quotes ends: a quote that something able to
// follow it follows. Without one soon after its first quote, on its line, that first quote, so
// that a sentence after it stays; without a quote, its line's end, which repr never has in a
// string. A line end that was an escape, in text read decoded, is one only if no quote follows,
// or with lines.
func pythonStringEnd(v *view, from, quote int, inContainer bool, work *scanWork, lines bool) (int, bool) {
	text := v.text
	escaped := false
	firstQuote, firstLine := none, none
	index := from
	for index < len(text) {
		if firstQuote != none && index > firstQuote+quoteRead {
			return firstQuote, escaped
		}
		unit := int(text[index])
		if unit == '\\' {
			escaped = true
			index += 2
		} else if (unit == '\n' || unit == '\r') && (lines || !fromEscape(v, index)) {
			if firstQuote != none {
				return firstQuote, escaped
			}
			return index, escaped
		} else {
			if unit == quote {
				spendWork(work, 1)
				if endsPythonString(text, index+1, inContainer) {
					return index, escaped
				}
				if firstQuote == none {
					firstQuote = index
				}
			} else if unit == '\n' || unit == '\r' {
				if firstLine == none {
					firstLine = index
				}
			}
			index++
		}
	}
	if firstQuote != none {
		return firstQuote, escaped
	}
	if firstLine != none {
		return firstLine, escaped
	}
	return len(text), escaped
}

// fromEscape reports whether what's at `at` in a decoded view was written as an escape.
func fromEscape(v *view, at int) bool {
	return v.pos != nil && position(v, at+1)-position(v, at) != 1
}

// endsPythonString: not a colon outside a container, which is the next key's quote in a repr
// pydantic cut short: {'token': 'abc...i_key': b'x'}.
func endsPythonString(text []uint16, at int, inContainer bool) bool {
	index := skipBlank(text, at)
	if index >= len(text) || isLineEnd(text, index) || isWrittenLineEnd(text, index) {
		return true
	}
	unit := int(text[index])
	if unit == ',' || unit == '}' || unit == ']' || unit == ')' {
		return true
	}
	if unit == '+' {
		return pythonQuoteAt(text, skipSpace(text, index+1)) != none
	}
	// A key's end, and <class 'list'>.
	if inContainer && (unit == ':' || unit == '>' || startsWith(text, "=>", index)) {
		return true
	}
	// One run of what ends a sentence, read at most a few characters on: a long run isn't one.
	for end := index; end < index+sentenceRead; end++ {
		if end >= len(text) || isSpaceUnit(int(text[end])) {
			return end > index
		}
		if !inSet(sentenceEnds, text, end) {
			return false
		}
	}
	return false
}

// inspectRefEnd is past util.inspect's mark on a container something inside it refers back to
// (<ref *1> ), or `at`.
func inspectRefEnd(text []uint16, at int) int {
	if !startsWith(text, "<ref *", at) {
		return at
	}
	index := at + 6
	digits := 0
	for digits < 9 && isDigitUnit(unitAt(text, index)) {
		index++
		digits++
	}
	if digits == 0 || !startsWith(text, "> ", index) {
		return at
	}
	return index + 2
}

// pythonBracketAt is the bracket of the container at `at`, which may have any name: [1, 2],
// OrderedDict(a='b'), Foo { a: 'b' }, $e { a: 'b' }, f.<locals>.Foo(a='b').
func pythonBracketAt(text []uint16, at int) int {
	from := inspectRefEnd(text, at)
	limit := from + keyRead
	if limit > len(text) {
		limit = len(text)
	}
	index := from
	for index < limit && !endsContainerName(int(text[index])) {
		index++
	}
	if index > from && unitAt(text, index) == ' ' {
		index++
	}
	if opensContainer(unitAt(text, index)) {
		return index
	}
	return none
}

func endsContainerName(unit int) bool {
	return isSpaceUnit(unit) || unit == '"' || unit == '\'' || unit == '`' || unit == ',' || opensContainer(unit) || unit == '}' || unit == ']' || unit == ')'
}

func opensContainer(unit int) bool {
	return unit == '{' || unit == '[' || unit == '('
}

// pythonContainersEnd is where the container whose first bracket is at `at` ends, past what's
// written before its contents: Set(2) { 'a' }, Foo(2) [Map] { 'a' => 'b' },
// [Object: null prototype] { a: 'b' }, Resp[int](a=1).
func pythonContainersEnd(v *view, at int, r *reading) int {
	text := v.text
	closing := pythonContainerRead(v, at, r)
	for closing != none {
		next := closing
		if unitAt(text, closing) == ' ' {
			next = closing + 1
		}
		if !opensContainer(unitAt(text, next)) {
			break
		}
		closing = pythonContainerRead(v, next, r)
	}
	return closing
}

// pythonContainerRead reads a container. A decoded view's line ends were escapes: a container
// that doesn't close is read again with them as line ends, as in text that has them as they are,
// and so is every later one in that view, so that many of them don't each read to the far end
// first.
func pythonContainerRead(v *view, at int, r *reading) int {
	if v.pos == nil {
		return pythonContainerEnd(v, at, false, r.work)
	}
	if r.linesIn == v {
		return pythonContainerEnd(v, at, true, r.work)
	}
	if closing := pythonContainerEnd(v, at, false, r.work); closing != none {
		return closing
	}
	r.linesIn = v
	return pythonContainerEnd(v, at, true, r.work)
}

// pythonContainerEnd is where a container in Python's syntax ends, past its closing bracket.
// None: its brackets don't match, or it never closes.
func pythonContainerEnd(v *view, at int, lines bool, work *scanWork) int {
	text := v.text
	var open []int
	index := at
	for index < len(text) {
		unit := int(text[index])
		if unit == '\'' || unit == '"' || unit == '`' {
			spendWork(work, 1)
			if unit != '`' && unitAt(text, index-1) == '\\' {
				index = escapedStringEnd(text, index, unit)
			} else {
				end, _ := pythonStringEnd(v, index+1, unit, true, work, lines)
				index = end + 1
			}
			continue
		}
		if unit == '{' || unit == '[' || unit == '(' {
			spendWork(work, 1)
			open = append(open, unit)
		} else if unit == '}' || unit == ']' || unit == ')' {
			spendWork(work, 1)
			want := unit - 2
			if unit == ')' {
				want = '('
			}
			if len(open) == 0 || open[len(open)-1] != want {
				return none
			}
			open = open[:len(open)-1]
			if len(open) == 0 {
				return index + 1
			}
		}
		index++
	}
	return none
}

// escapedStringEnd is past a string whose opening quote has a backslash before it, as JSON holding
// a repr has: its closing quote has one too.
func escapedStringEnd(text []uint16, at, quote int) int {
	for i := at + 1; i+1 < len(text); i++ {
		if text[i] == '\\' && int(text[i+1]) == quote {
			return i + 2
		}
	}
	return len(text)
}

// pydanticValue finds [type=…, input_value= from `from`: where pydantic's errors have the value
// they refused, straight after the error's type, which a custom error can spell any way (up to
// 128 characters on its line). It returns where the match starts and ends.
func pydanticValue(text []uint16, from int) (int, int, bool) {
	const head, tail = "[type=", ", input_value="
	for at := indexOfLiteral(text, head, from); at != none; at = indexOfLiteral(text, head, at+1) {
		start := at + len(head)
		for end := start + 1; end <= start+128 && end <= len(text); end++ {
			if previous := text[end-1]; previous == '\r' || previous == '\n' {
				break
			}
			if startsWith(text, tail, end) {
				return at, end + len(tail), true
			}
		}
	}
	return 0, 0, false
}

// scanInputValues reads what pydantic's errors quote of what they refused: Input should be a valid
// string [type=string_type, input_value=123, input_type=int]. It can be cut anywhere, list its key
// on another line or not at all, and the event's input has it anyway, so it always goes, up to
// , input_type= and the type's name. Without that soon after it, it was cut: up to its line's
// end, however that's written.
func scanInputValues(v *view, r *reading) {
	text := v.text
	// Each looked for only once it's behind: a search per value would read to a far one every time.
	inputType, line, written := -1, -1, -1
	for from := 0; ; {
		_, start, ok := pydanticValue(text, from)
		if !ok {
			return
		}
		spendWork(r.work, 1)
		if inputType < start {
			inputType = inputTypeAt(text, start)
		}
		if line < start {
			line = lineEnd(text, start, len(text))
			if line == none {
				line = len(text)
			}
		}
		end := inputType
		if inputType > line || inputType-start > inputRead {
			if written < start {
				written = writtenLineEnd(text, start)
			}
			end = line
			if written < end {
				end = written
			}
		}
		if end > start && !isPlaceholder(v, start, end) {
			identity, known := reprText(text[start:end])
			r.add(v, start, end, identity, known, "")
		}
		from = end
	}
}

// inputTypeAt is the next , input_type= that ends a value; a value can hold those words. Past the
// text's end if none does.
func inputTypeAt(text []uint16, from int) int {
	for at := indexOfLiteral(text, inputTypeWord, from); at != none; at = indexOfLiteral(text, inputTypeWord, at+1) {
		// The type's name and the bracket that closes the error's.
		for index := at + len(inputTypeWord); index < len(text); index++ {
			unit := text[index]
			if unit == ' ' || unit == '\t' || unit == '\r' || unit == '\n' || unit == '\'' || unit == '"' || unit == '=' {
				break
			}
			if unit == ']' {
				return at
			}
		}
	}
	return len(text) + 1
}

// writtenLineEnd is the next line end as text printed more than once has it: backslashes, then n
// or r, from where the backslashes start.
func writtenLineEnd(text []uint16, from int) int {
	index := from
	// From inside a run of backslashes, nothing starts before the run's end.
	if index > 0 && unitAt(text, index-1) == '\\' {
		for unitAt(text, index) == '\\' {
			index++
		}
	}
	for {
		start := indexOfUnit(text, '\\', index)
		if start == none {
			return len(text)
		}
		end := start
		for unitAt(text, end) == '\\' {
			end++
		}
		if next := unitAt(text, end); next == 'n' || next == 'r' {
			return start
		}
		index = end
	}
}

// reprText is what a value printed by repr holds, so a string shares its number with the same
// value elsewhere. Not known: pydantic cut it.
func reprText(value []uint16) (string, bool) {
	if indexOfLiteral(value, "...", 0) != none {
		return "", false
	}
	if len(value) > 1 && (value[0] == '\'' || value[0] == '"') && value[len(value)-1] == value[0] {
		return fromUnits(value[1 : len(value)-1]), true
	}
	return fromUnits(value), true
}

// scanQueries reads a URL's parameters: ?token=abc&api_key=def, a fragment's #access_token=abc,
// and a URL held in a parameter, percent-encoded once or more. Their names follow the same rule as
// keys', and a few go only here: a signed URL's sig, an OAuth code.
func scanQueries(v *view, r *reading) {
	current := v
	for level := 0; ; level++ {
		scanParameters(current, r)
		if level == maxPercent || !percentEquals(current.text) {
			return
		}
		current = percentDecoded(current, r.work)
	}
}

func scanParameters(v *view, r *reading) {
	text := v.text
	equals := indexOfUnit(text, '=', 0)
	for equals != none {
		limit := equals - keyRead
		if limit < 0 {
			limit = 0
		}
		start := equals
		for start > limit && isQueryName(int(text[start-1])) {
			start--
		}
		next := equals + 1
		if start < equals && text[start] != ']' && startsParameter(text, start) {
			spendWork(r.work, 1+((equals-start)>>4))
			name := parameterName(text[start:equals])
			if r.isSecretKey(name) || urlOnlyNames.MatchString(name) {
				next = readQueryValue(v, next, r)
			}
		}
		equals = indexOfUnit(text, '=', next)
	}
}

func hexValue(unit uint16) int {
	switch {
	case unit >= '0' && unit <= '9':
		return int(unit - '0')
	case unit >= 'a' && unit <= 'f':
		return int(unit-'a') + 10
	case unit >= 'A' && unit <= 'F':
		return int(unit-'A') + 10
	}
	return -1
}

// parameterName is a name as it was before encoding, without PHP's and Rails' [] at its end.
func parameterName(name []uint16) string {
	decoded := name
	if indexOfUnit(name, '%', 0) != none {
		decoded = make([]uint16, 0, len(name))
		for i := 0; i < len(name); i++ {
			if name[i] == '%' && i+2 < len(name) && hexValue(name[i+1]) >= 0 && hexValue(name[i+2]) >= 0 {
				decoded = append(decoded, uint16(hexValue(name[i+1])*16+hexValue(name[i+2])))
				i += 2
				continue
			}
			decoded = append(decoded, name[i])
		}
	}
	end := len(decoded)
	for unitAt(decoded, end-1) == ']' {
		open := end - 2
		for open >= 0 && isDigitUnit(int(decoded[open])) {
			open--
		}
		if open < 0 || decoded[open] != '[' {
			break
		}
		end = open
	}
	return fromUnits(decoded[:end])
}

func isQueryName(unit int) bool {
	return isWordUnit(unit) || unit == '_' || unit == '-' || unit == '.' || unit == '~' || unit == '%' || unit == '+' || unit == '[' || unit == ']'
}

// startsParameter: after ?, &, #, a hash route's #/, #! or #!/, or & as HTML writes it. Not code's &&.
func startsParameter(text []uint16, start int) bool {
	unit := unitAt(text, start-1)
	if unit == '?' || unit == '#' {
		return true
	}
	if unit == '&' {
		return unitAt(text, start-2) != '&'
	}
	if unit == ';' {
		return startsWith(text, "&amp", start-5)
	}
	if unit == '/' || unit == '!' {
		return unitAt(text, start-2) == '#' || (unit == '/' && startsWith(text, "#!", start-3))
	}
	return false
}

// readQueryValue adds the span of the parameter's value at `at`. It returns where scanning goes on.
func readQueryValue(v *view, at int, r *reading) int {
	text := v.text
	end := at
	for end < len(text) && !inSet(queryValueEnd, text, end) {
		end++
	}
	// A placeholder's bracket ends a value, so it is looked for by its own shape.
	if unitAt(text, at) == '[' {
		limit := at + longestPlaceholder
		if limit > len(text) {
			limit = len(text)
		}
		for i := at; i < limit; i++ {
			if text[i] == ']' {
				if isPlaceholder(v, at, i+1) {
					return i + 1
				}
				break
			}
		}
	}
	limit := at + keyRead
	if limit > len(text) {
		limit = len(text)
	}
	if at <= limit {
		if template := queryTemplate.FindString(fromUnits(text[at:limit])); template != "" {
			after := at + len(template)
			if after >= len(text) || inSet(queryValueEnd, text, after) {
				return after
			}
		}
	}
	if end > at {
		r.add(v, at, end, fromUnits(text[at:end]), true, "")
	}
	return end
}

// percentDecoded decodes %XX for an ASCII character, keeping where each character came from.
// Anything else stays as it is.
func percentDecoded(v *view, work *scanWork) *view {
	text := v.text
	out := make([]uint16, 0, len(text))
	pos := make([]int32, 0, len(text)+1)
	spendWork(work, 1+(len(text)>>4))
	index := 0
	for {
		found := indexOfUnit(text, '%', index)
		stop := found
		if found == none {
			stop = len(text)
		}
		for at := index; at < stop; at++ {
			out = append(out, text[at])
			pos = append(pos, int32(position(v, at)))
		}
		if found == none {
			break
		}
		spendWork(work, 1)
		if hasHex(text, found+1, 2, len(text)) {
			if value := hexValue(text[found+1])*16 + hexValue(text[found+2]); value < 0x80 {
				out = append(out, uint16(value))
				pos = append(pos, int32(position(v, found)))
				index = found + 3
				continue
			}
		}
		out = append(out, '%')
		pos = append(pos, int32(position(v, found)))
		index = found + 1
	}
	pos = append(pos, int32(position(v, len(text))))
	return &view{text: out, pos: pos}
}

// mayHoldKeyUnits is mayHoldKey for a decoded piece.
func mayHoldKeyUnits(text []uint16) bool {
	has := func(unit uint16) bool { return indexOfUnit(text, unit, 0) != none }
	holds := func(literal string) bool { return indexOfLiteral(text, literal, 0) != none }
	if (has('"') || has('\'') || holds("u0022")) && (has(':') || holds("=>") || holds("u003a") || holds("u003A")) {
		return true
	}
	if has('%') && percentEquals(text) {
		return true
	}
	return has('=') && (has('?') || has('&') || has('#') || holds(inputValueWord))
}

// nest reads JSON in a string: its quotes, colons and spaces are all encoded, so it is scanned decoded.
func nest(v *view, start, end, depth int, printed bool, r *reading) {
	inner := decode(v, start, end, r.work)
	// With nothing decoded, it holds no more than it shows: a Windows path, a regular expression.
	if len(inner.text) == end-start || !mayHoldKeyUnits(inner.text) {
		return
	}
	if depth < maxNesting {
		read(inner, depth+1, printed, r)
	} else if encodedKey.MatchString(fromUnits(inner.text)) {
		r.add(v, start, end, "", false, "")
	}
}

// readValue adds the span of the value at `at`, if it can hold a secret. It returns where
// scanning goes on; none: it ran to the end of the text.
func readValue(v *view, at, level int, entry bool, r *reading) int {
	text := v.text
	if at >= len(text) {
		return at
	}
	first := int(text[at])
	if first == '"' {
		// Replaced already, whatever follows it: a string a hook changed is read a second time.
		quoteAt, _ := lex(text, at+1)
		whole := lineEnd(text, at+1, quoteAt)
		if whole == none {
			whole = quoteAt
		}
		if isPlaceholder(v, at+1, whole) {
			if whole < len(text) {
				return whole + 1
			}
			return none
		}
		end, escaped := valueEnd(text, at+1, false, r.work)
		closed := end < len(text)
		if end > at+1 {
			if closed {
				r.add(v, at+1, end, fromUnits(body(v, at+1, end, escaped, r.work)), true, "")
			} else {
				r.add(v, at+1, end, "", false, "")
			}
		}
		if closed {
			return end + 1
		}
		return none
	}
	// Quoted any other way: "password": 'x', as a repr has for a key with an apostrophe.
	if pythonQuoteAt(text, at) != none {
		return readPythonValue(v, at, r)
	}
	// Only a quoted string is a value wherever its key is: not Post "https://x/token": timeout, or
	// case "token": return x.
	if !entry {
		return at
	}
	backslashes := 0
	if level > 0 {
		backslashes = 1<<uint(level) - 1
	}
	quote := strings.Repeat(`\`, backslashes) + `"`
	if first == '{' || first == '[' {
		closing := containerEnd(text, at, r.work)
		if closing == notJSONContainer {
			return readPythonValue(v, at, r)
		}
		if closing == none {
			r.add(v, at, len(text), "", false, quote)
			return none
		}
		if isPlaceholder(v, at, closing) {
			return closing
		}
		if endsToken(text, closing) {
			if skipSpace(text, at+1) == closing-1 {
				return closing
			}
			r.add(v, at, closing, fromUnits(text[at:closing]), true, quote)
			return closing
		}
		// What's written before a container's contents: [Object: null prototype] { a: 'b' }.
		next := closing
		if unitAt(text, closing) == ' ' {
			next = closing + 1
		}
		if opensContainer(unitAt(text, next)) {
			return readPythonValue(v, at, r)
		}
		return bareValue(v, at, closing, quote, r)
	}
	// A container with a name: Foo(a='b'), Set(2) { 'a' }.
	if pythonBracketAt(text, at) != none {
		return readPythonValue(v, at, r)
	}
	for _, literal := range pythonLiterals {
		if startsWith(text, literal, at) && endsToken(text, at+len(literal)) {
			return at + len(literal)
		}
	}
	if number := jsonNumberEnd(text, at); number > at && endsToken(text, number) {
		r.add(v, at, number, fromUnits(text[at:number]), true, quote)
		return number
	}
	return bareValue(v, at, at, quote, r)
}

// bareValue adds a value that isn't JSON, from `at`, read on from `from` up to the end of its line
// or the next comma or closing bracket.
func bareValue(v *view, at, from int, quote string, r *reading) int {
	text := v.text
	next := from
	for next < len(text) && !endsBare(int(text[next])) {
		next++
	}
	end := next
	for end > at && isSpaceUnit(int(text[end-1])) {
		end--
	}
	if end > at {
		r.add(v, at, end, fromUnits(text[at:end]), true, quote)
	}
	return next
}

// valueEnd is where a string value ends: at an unescaped quote that something able to follow a
// string follows. So a value with a quote left unescaped in it, as hand-built JSON has, is read
// whole, up to a line that starts as JSON's do.
func valueEnd(text []uint16, from int, inContainer bool, work *scanWork) (int, bool) {
	escaped := false
	index := from
	for {
		spendWork(work, 1)
		end, pieceEscaped := lex(text, index)
		escaped = escaped || pieceEscaped
		if index > from {
			if line := entryLineEnd(text, index, end); line != none {
				return line, escaped
			}
		}
		if end >= len(text) {
			return len(text), escaped
		}
		if endsString(text, end+1, inContainer) {
			return end, escaped
		}
		index = end + 1
	}
}

// entryLineEnd is the end of the first line in the range whose next line starts with a quote or a
// closing bracket.
func entryLineEnd(text []uint16, from, to int) int {
	index := from
	for index < to {
		unit := text[index]
		if unit != '\n' && unit != '\r' {
			index++
			continue
		}
		next := skipSpace(text, index+1)
		if first := unitAt(text, next); first == '"' || first == '}' || first == ']' {
			return index
		}
		index = next
	}
	return none
}

func lineEnd(text []uint16, from, to int) int {
	for index := from; index < to && index < len(text); index++ {
		if unit := text[index]; unit == '\n' || unit == '\r' {
			return index
		}
	}
	return none
}

// isWrittenLineEnd: a line's end, as text printed more than once has it: any number of
// backslashes, then n or r.
func isWrittenLineEnd(text []uint16, at int) bool {
	index := at
	for unitAt(text, index) == '\\' {
		index++
	}
	next := unitAt(text, index)
	return index > at && (next == 'n' || next == 'r')
}

// endsToken reports whether what's at `at`, after spaces and tabs, can follow a number, a literal
// or a container.
func endsToken(text []uint16, at int) bool {
	index := skipBlank(text, at)
	return index >= len(text) || endsBare(int(text[index])) || isLineEnd(text, index)
}

// endsString reports whether what's at `at`, after spaces and tabs, can follow a string: the
// text's or a line's end, a comma that a value or a line's end follows, a closing bracket with no
// word straight after it, what ends a sentence, or inside a container, a key's colon or arrow.
func endsString(text []uint16, at int, inContainer bool) bool {
	index := skipBlank(text, at)
	if index >= len(text) || isLineEnd(text, index) {
		return true
	}
	unit := int(text[index])
	if unit == ',' {
		return followsComma(text, skipBlank(text, index+1))
	}
	if unit == '}' || unit == ']' {
		return !isWordUnit(unitAt(text, index+1))
	}
	return (inContainer && (unit == ':' || startsWith(text, "=>", index))) || endsSentence(text, index)
}

func endsSentence(text []uint16, at int) bool {
	index := at
	for index < len(text) && inSet(sentenceEnds, text, index) {
		index++
	}
	return index > at && (index >= len(text) || isSpaceUnit(int(text[index])) || isLineEnd(text, index))
}

func followsComma(text []uint16, at int) bool {
	if at >= len(text) || isLineEnd(text, at) {
		return true
	}
	unit := int(text[at])
	if unit == '"' || unit == '\'' || unit == '`' || unit == '{' || unit == '[' || unit == '}' || unit == ']' || unit == '-' {
		return true
	}
	if isDigitUnit(unit) {
		return true
	}
	for _, literal := range jsonLiterals {
		if startsWith(text, literal, at) {
			return true
		}
	}
	return false
}

// isLineEnd: a line's end, written as it is or as printed JSON has it.
func isLineEnd(text []uint16, at int) bool {
	unit := unitAt(text, at)
	if unit == '\n' || unit == '\r' {
		return true
	}
	if unit != '\\' {
		return false
	}
	next := unitAt(text, at+1)
	return next == 'n' || next == 'r'
}

// containerEnd is where a container ends, past its closing bracket. None: its brackets don't
// match, or it never closes.
func containerEnd(text []uint16, at int, work *scanWork) int {
	var open []int
	index := at
	for index < len(text) {
		unit := int(text[index])
		if unit == '"' {
			end, _ := valueEnd(text, index+1, true, work)
			index = end + 1
			continue
		}
		if unit == '\'' || unit == '`' {
			return notJSONContainer
		}
		if unit == '{' || unit == '[' {
			spendWork(work, 1)
			open = append(open, unit)
		} else if unit == '}' || unit == ']' {
			spendWork(work, 1)
			if len(open) == 0 || open[len(open)-1] != unit-2 {
				return none
			}
			open = open[:len(open)-1]
			if len(open) == 0 {
				return index + 1
			}
		}
		index++
	}
	return none
}

// lex is the next unescaped quote from `from`, or the end of the text, and whether a backslash
// came before it.
func lex(text []uint16, from int) (int, bool) {
	escaped := false
	for index := from; index < len(text); index++ {
		unit := text[index]
		if unit == '"' {
			return index, escaped
		}
		if unit == '\\' {
			escaped = true
			index++
		}
	}
	return len(text), escaped
}

func body(v *view, start, end int, escaped bool, work *scanWork) []uint16 {
	if escaped {
		return decode(v, start, end, work).text
	}
	return v.text[start:end]
}

// The escapes printed text and YAML have that JSON doesn't, besides those that take digits.
func otherEscape(unit int) (uint16, bool) {
	switch unit {
	case '\'':
		return '\'', true
	case ' ':
		return ' ', true
	case '0':
		return 0, true
	case 'a':
		return 0x07, true
	case 'e':
		return 0x1b, true
	case 'v':
		return '\v', true
	case 'N':
		return 0x85, true
	case '_':
		return 0xa0, true
	case 'L':
		return 0x2028, true
	case 'P':
		return 0x2029, true
	}
	return 0, false
}

func hexNumber(text []uint16, at, count int) int {
	value := 0
	for i := at; i < at+count; i++ {
		value = value*16 + hexValue(text[i])
	}
	return value
}

// decode decodes escapes, keeping where each character came from: JSON's, and those printed text
// and YAML have, including YAML's folded lines. Anything else stays as it is, so a truncated
// escape or a Windows path does no harm. JSON itself holds none of the others.
func decode(v *view, start, end int, work *scanWork) *view {
	text := v.text
	out := make([]uint16, 0, end-start)
	pos := make([]int32, 0, end-start+1)
	spendWork(work, 1+((end-start)>>4))
	index := start
	for index < end {
		found := none
		for i := index; i < end; i++ {
			if text[i] == '\\' {
				found = i
				break
			}
		}
		stop := end
		if found != none {
			stop = found
		}
		for at := index; at < stop; at++ {
			out = append(out, text[at])
			pos = append(pos, int32(position(v, at)))
		}
		if stop == end {
			break
		}
		spendWork(work, 1)
		next := none
		if found+1 < end {
			next = int(text[found+1])
		}
		var chars [2]uint16
		count := 0
		length := 2
		if char, ok := escapedChar(next); ok {
			chars[0], count = char, 1
		} else if char, ok := otherEscape(next); ok {
			chars[0], count = char, 1
		}
		switch {
		case next == 'u' && hasHex(text, found+2, 4, end):
			chars[0], count = uint16(hexNumber(text, found+2, 4)), 1
			length = 6
		case next == 'x' && hasHex(text, found+2, 2, end):
			chars[0], count = uint16(hexNumber(text, found+2, 2)), 1
			length = 4
		case next == 'U' && hasHex(text, found+2, 8, end) && hexNumber(text, found+2, 8) <= maxCodePoint:
			if point := rune(hexNumber(text, found+2, 8)); point >= 0x10000 {
				high, low := utf16.EncodeRune(point)
				chars[0], chars[1], count = uint16(high), uint16(low), 2
			} else {
				chars[0], count = uint16(point), 1
			}
			length = 10
		case next == '\n' || next == '\r':
			count = 0
			length = foldEnd(text, found+1, end) - found
		case count == 0:
			chars[0], count = '\\', 1
			length = 1
		}
		from := int32(position(v, found))
		for i := 0; i < count; i++ {
			out = append(out, chars[i])
			pos = append(pos, from)
		}
		index = found + length
	}
	pos = append(pos, int32(position(v, end)))
	return &view{text: out, pos: pos}
}

func hasHex(text []uint16, at, count, end int) bool {
	if at+count > end || at < 0 {
		return false
	}
	for index := at; index < at+count; index++ {
		if hexValue(text[index]) < 0 {
			return false
		}
	}
	return true
}

// foldEnd is past the line's end at `at` and the next line's indentation.
func foldEnd(text []uint16, at, end int) int {
	index := at + 1
	if text[at] == '\r' && unitAt(text, at+1) == '\n' && at+1 < end {
		index = at + 2
	}
	for index < end && (text[index] == ' ' || text[index] == '\t') {
		index++
	}
	return index
}

func escapedChar(unit int) (uint16, bool) {
	switch unit {
	case '"', '\\', '/':
		return uint16(unit), true
	case 'b':
		return '\b', true
	case 'f':
		return '\f', true
	case 'n':
		return '\n', true
	case 'r':
		return '\r', true
	case 't':
		return '\t', true
	}
	return 0, false
}

// isPlaceholder: spelled out as a placeholder in the string given, so that reserve has seen its number.
func isPlaceholder(v *view, start, end int) bool {
	if end-start > longestPlaceholder || end < start || position(v, end)-position(v, start) != end-start {
		return false
	}
	return wholePlaceholderRE.MatchString(fromUnits(v.text[start:end]))
}
