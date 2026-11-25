// Package jsonv holds JSON as the SDK reads and writes it: objects keep the order their keys came
// in, so two runs over the same input number placeholders and cut lists the same way, and the text
// written is what JavaScript's JSON.stringify writes, which the event's size limit is counted in.
package jsonv

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"math"
	"reflect"
	"slices"
	"sort"
	"strconv"
	"strings"
	"unicode/utf8"
)

// A Value is nil, a bool, a Num, a string, a []Value, an *Object, a Blob or NotJSON.
type Value = any

// Num is a JSON number as JavaScript writes it. Make one with Int or Float, or get one from
// Decode: never from text, which may not be written that way.
type Num string

// Blob stands for binary data of a known size, which is never held as text.
type Blob struct{ Bytes int64 }

// NotJSON stands where the input had something JSON can't hold: a number too large for a double.
type NotJSON struct{}

// Object is a JSON object whose keys keep the order they were first set in.
type Object struct {
	keys  []string
	vals  []Value
	index map[string]int
}

// indexAt is the size past which lookups go through a map instead of a scan.
const indexAt = 12

// NewObject returns an empty object.
func NewObject() *Object { return &Object{} }

// Len is the number of keys. A nil object has none.
func (o *Object) Len() int {
	if o == nil {
		return 0
	}
	return len(o.keys)
}

// Keys returns the keys in order. The slice is the object's own: don't change it.
func (o *Object) Keys() []string {
	if o == nil {
		return nil
	}
	return o.keys
}

// At returns the key and value at position i.
func (o *Object) At(i int) (string, Value) { return o.keys[i], o.vals[i] }

// find never changes the object, so an object nothing is changing can be read from several
// goroutines: the index is built by Set.
func (o *Object) find(key string) int {
	if o == nil {
		return -1
	}
	if o.index != nil {
		if i, ok := o.index[key]; ok {
			return i
		}
		return -1
	}
	for i, k := range o.keys {
		if k == key {
			return i
		}
	}
	return -1
}

// Get returns the value under key, and whether the object has the key.
func (o *Object) Get(key string) (Value, bool) {
	if i := o.find(key); i >= 0 {
		return o.vals[i], true
	}
	return nil, false
}

// Has reports whether the object has the key.
func (o *Object) Has(key string) bool { return o.find(key) >= 0 }

// Set puts a value under key: in place when the key is there, at the end otherwise.
func (o *Object) Set(key string, value Value) {
	if i := o.find(key); i >= 0 {
		o.vals[i] = value
		return
	}
	o.keys = append(o.keys, key)
	o.vals = append(o.vals, value)
	if o.index != nil {
		o.index[key] = len(o.keys) - 1
	} else if len(o.keys) >= indexAt {
		o.reindex()
	}
}

func (o *Object) reindex() {
	o.index = make(map[string]int, len(o.keys))
	for i, k := range o.keys {
		o.index[k] = i
	}
}

// Delete removes key, keeping the order of the others. What it held is let go of.
func (o *Object) Delete(key string) {
	i := o.find(key)
	if i < 0 {
		return
	}
	o.keys = slices.Delete(o.keys, i, i+1)
	o.vals = slices.Delete(o.vals, i, i+1)
	if o.index != nil {
		o.reindex()
	}
}

// Clone copies the value: its objects and lists are new, its strings shared.
func Clone(value Value) Value {
	switch v := value.(type) {
	case *Object:
		if v == nil {
			return v
		}
		out := &Object{keys: append([]string(nil), v.keys...), vals: make([]Value, len(v.vals))}
		for i, item := range v.vals {
			out.vals[i] = Clone(item)
		}
		return out
	case []Value:
		out := make([]Value, len(v))
		for i, item := range v {
			out[i] = Clone(item)
		}
		return out
	default:
		return value
	}
}

// Int returns the number for an integer, written as JavaScript writes it: one past 2^53, which
// JavaScript can't hold exactly, is the nearest double, as it is for the ingestion.
func Int(n int64) Num {
	const safe = 1<<53 - 1
	if n > safe || n < -safe {
		return Num(formatFloat(float64(n)))
	}
	return Num(strconv.FormatInt(n, 10))
}

// Float returns the number for a float, written as JavaScript writes it, and false for one JSON
// can't hold (an infinity or NaN).
func Float(f float64) (Num, bool) {
	if math.IsInf(f, 0) || math.IsNaN(f) {
		return "", false
	}
	return Num(formatFloat(f)), true
}

// Float64 reads the number back.
func (n Num) Float64() float64 {
	f, _ := strconv.ParseFloat(string(n), 64)
	return f
}

// formatFloat writes a double as JavaScript's Number.prototype.toString does: the shortest digits
// that read back the same, an exponent only below 1e-6 or from 1e21, and -0 as 0.
func formatFloat(f float64) string {
	if f == 0 {
		return "0"
	}
	abs := math.Abs(f)
	format := byte('f')
	if abs < 1e-6 || abs >= 1e21 {
		format = 'e'
	}
	b := strconv.AppendFloat(make([]byte, 0, 24), f, format, -1, 64)
	if format == 'e' {
		// e-07 as e-7.
		if n := len(b); n >= 4 && b[n-4] == 'e' && (b[n-3] == '-' || b[n-3] == '+') && b[n-2] == '0' {
			b[n-2] = b[n-1]
			b = b[:n-1]
		}
	}
	return string(b)
}

// numFromLiteral reads a JSON number literal as JavaScript does: as a double, written back as
// JavaScript writes it. So an integer past 2^53 is the nearest double, as it is for the ingestion,
// which reads the event in JavaScript and counts its size as it writes it back. False when the
// number is too large for a double.
func numFromLiteral(lit string) (Num, bool) {
	f, err := strconv.ParseFloat(lit, 64)
	if err != nil || math.IsInf(f, 0) || math.IsNaN(f) {
		return "", false
	}
	return Num(formatFloat(f)), true
}

// ErrTrailing is returned when text follows the JSON value.
var ErrTrailing = errors.New("jsonv: text after the JSON value")

// MaxDepth is how deep Decode builds lists and objects. One nested deeper is read past and left
// empty: nothing of an event is read that deep (shaping stops at 10 levels, and a measured tool
// result at 64), and a value nested without end must not use the stack up.
const MaxDepth = 128

// Decode reads one JSON value, as JavaScript's JSON.parse does:
//   - a key given twice keeps its first place and its last value;
//   - an object's keys that are array indices ("0", "7", "42") come first, in ascending order,
//     then the others in the order they came in. So the first 100 entries of an object, which are
//     the ones an event keeps, are the same ones in every SDK.
//
// A lone surrogate escape becomes U+FFFD, which is what a Go string can hold of it. A number too
// large for a double becomes NotJSON.
func Decode(data []byte) (Value, error) {
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber()
	tok, err := dec.Token()
	if err != nil {
		return nil, err
	}
	value, err := decodeFrom(dec, tok, 0)
	if err != nil {
		return nil, err
	}
	if _, err := dec.Token(); err != io.EOF {
		return nil, ErrTrailing
	}
	return value, nil
}

// skip reads past the rest of a list or an object whose opening token was just read.
func skip(dec *json.Decoder) error {
	for depth := 1; depth > 0; {
		tok, err := dec.Token()
		if err != nil {
			return err
		}
		if delim, ok := tok.(json.Delim); ok {
			if delim == '{' || delim == '[' {
				depth++
			} else {
				depth--
			}
		}
	}
	return nil
}

func decodeFrom(dec *json.Decoder, tok json.Token, depth int) (Value, error) {
	switch t := tok.(type) {
	case json.Delim:
		switch t {
		case '{':
			obj := &Object{}
			if depth >= MaxDepth {
				return obj, skip(dec)
			}
			for dec.More() {
				keyTok, err := dec.Token()
				if err != nil {
					return nil, err
				}
				key, ok := keyTok.(string)
				if !ok {
					return nil, errors.New("jsonv: an object key must be a string")
				}
				valueTok, err := dec.Token()
				if err != nil {
					return nil, err
				}
				item, err := decodeFrom(dec, valueTok, depth+1)
				if err != nil {
					return nil, err
				}
				obj.Set(key, item)
			}
			if _, err := dec.Token(); err != nil {
				return nil, err
			}
			obj.indicesFirst()
			return obj, nil
		case '[':
			list := []Value{}
			if depth >= MaxDepth {
				return list, skip(dec)
			}
			for dec.More() {
				itemTok, err := dec.Token()
				if err != nil {
					return nil, err
				}
				item, err := decodeFrom(dec, itemTok, depth+1)
				if err != nil {
					return nil, err
				}
				list = append(list, item)
			}
			if _, err := dec.Token(); err != nil {
				return nil, err
			}
			return list, nil
		}
		return nil, errors.New("jsonv: unexpected delimiter")
	case json.Number:
		if n, ok := numFromLiteral(string(t)); ok {
			return n, nil
		}
		return NotJSON{}, nil
	case string:
		return t, nil
	case bool:
		return t, nil
	case nil:
		return nil, nil
	}
	return nil, errors.New("jsonv: unexpected token")
}

// arrayIndex is the number a key is, when it is one JavaScript orders first: an integer from 0 to
// 2^32 - 2, written without a sign or leading zeros.
func arrayIndex(key string) (uint32, bool) {
	if key == "" || len(key) > 10 || (len(key) > 1 && key[0] == '0') {
		return 0, false
	}
	var n uint64
	for i := 0; i < len(key); i++ {
		if key[i] < '0' || key[i] > '9' {
			return 0, false
		}
		n = n*10 + uint64(key[i]-'0')
	}
	if n > math.MaxUint32-1 {
		return 0, false
	}
	return uint32(n), true
}

// indicesFirst puts the object's keys in JavaScript's order.
func (o *Object) indicesFirst() {
	type indexed struct {
		n     uint32
		key   string
		value Value
	}
	var indices []indexed
	for i, key := range o.keys {
		if n, ok := arrayIndex(key); ok {
			indices = append(indices, indexed{n, key, o.vals[i]})
		}
	}
	if len(indices) == 0 {
		return
	}
	sort.Slice(indices, func(a, b int) bool { return indices[a].n < indices[b].n })
	keys := make([]string, 0, len(o.keys))
	vals := make([]Value, 0, len(o.vals))
	for _, item := range indices {
		keys = append(keys, item.key)
		vals = append(vals, item.value)
	}
	for i, key := range o.keys {
		if _, ok := arrayIndex(key); !ok {
			keys = append(keys, key)
			vals = append(vals, o.vals[i])
		}
	}
	o.keys, o.vals = keys, vals
	if o.index != nil {
		o.reindex()
	}
}

var jsonNumber = reflect.TypeFor[json.Number]()

// PlainValue is a Go value the app gave where a plain JSON value is wanted (a metadata value, an
// error's code): a string, a number, a boolean or nil, of any type of those kinds, or a pointer
// to one. Anything else is NotJSON. It is read by its kind, and never by a method of its own: no
// MarshalJSON is called, which is the app's code and could block, panic or answer differently.
func PlainValue(value any) Value {
	v := reflect.ValueOf(value)
	for depth := 0; v.IsValid() && (v.Kind() == reflect.Pointer || v.Kind() == reflect.Interface); depth++ {
		if v.IsNil() {
			return nil
		}
		if depth > 8 {
			return NotJSON{}
		}
		v = v.Elem()
	}
	if !v.IsValid() {
		return nil
	}
	if v.Type() == jsonNumber {
		if literal := v.String(); numberLiteral(literal) {
			if n, ok := numFromLiteral(literal); ok {
				return n
			}
		}
		return NotJSON{}
	}
	switch v.Kind() {
	case reflect.String:
		return ValidString(v.String())
	case reflect.Bool:
		return v.Bool()
	case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
		return Int(v.Int())
	case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64, reflect.Uintptr:
		if u := v.Uint(); u <= math.MaxInt64 {
			return Int(int64(u))
		} else {
			return Num(formatFloat(float64(u)))
		}
	case reflect.Float32:
		// As encoding/json writes a float32: the shortest digits that read back as the same float32.
		if n, ok := numFromLiteral(strconv.FormatFloat(v.Float(), 'g', -1, 32)); ok {
			return n
		}
	case reflect.Float64:
		if n, ok := Float(v.Float()); ok {
			return n
		}
	}
	return NotJSON{}
}

// numberLiteral reports whether text is a number as JSON writes one.
func numberLiteral(text string) bool {
	i := 0
	digits := func() bool {
		start := i
		for i < len(text) && text[i] >= '0' && text[i] <= '9' {
			i++
		}
		return i > start
	}
	if i < len(text) && text[i] == '-' {
		i++
	}
	if i < len(text) && text[i] == '0' {
		i++
	} else if !digits() {
		return false
	}
	if i < len(text) && text[i] == '.' {
		i++
		if !digits() {
			return false
		}
	}
	if i < len(text) && (text[i] == 'e' || text[i] == 'E') {
		i++
		if i < len(text) && (text[i] == '+' || text[i] == '-') {
			i++
		}
		if !digits() {
			return false
		}
	}
	return i == len(text)
}

// ObjectFromMap is a Go map as an object, each value converted on its own (PlainValue), so one
// that isn't a plain value doesn't take the others with it. Its keys are in the order Decode would read
// the map's JSON in: array indices first, then the others sorted.
func ObjectFromMap(values map[string]any) *Object {
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	out := NewObject()
	for _, key := range keys {
		out.Set(ValidString(key), PlainValue(values[key]))
	}
	out.indicesFirst()
	return out
}

// ValidString returns text as valid UTF-8, each invalid byte replaced by U+FFFD, as encoding/json
// writes it for the client.
func ValidString(text string) string {
	if utf8.ValidString(text) {
		return text
	}
	var out strings.Builder
	// At least as long: each byte that isn't valid becomes three.
	out.Grow(len(text))
	for i := 0; i < len(text); {
		r, size := utf8.DecodeRuneInString(text[i:])
		if r == utf8.RuneError && size == 1 {
			out.WriteString("\uFFFD")
		} else {
			out.WriteString(text[i : i+size])
		}
		i += size
	}
	return out.String()
}

// Marshal writes the value as JavaScript's JSON.stringify does: compact, non-ASCII characters as
// themselves, and only quotes, backslashes and control characters escaped.
func Marshal(value Value) []byte { return Append(nil, value) }

// Size is the length in bytes of the value's JSON.
func Size(value Value) int { return len(Append(nil, value)) }

// Append appends the value's JSON to dst.
func Append(dst []byte, value Value) []byte {
	switch v := value.(type) {
	case nil:
		return append(dst, "null"...)
	case bool:
		if v {
			return append(dst, "true"...)
		}
		return append(dst, "false"...)
	case Num:
		if v == "" {
			return append(dst, '0')
		}
		return append(dst, v...)
	case string:
		return AppendString(dst, v)
	case []Value:
		dst = append(dst, '[')
		for i, item := range v {
			if i > 0 {
				dst = append(dst, ',')
			}
			dst = Append(dst, item)
		}
		return append(dst, ']')
	case *Object:
		if v == nil {
			return append(dst, "null"...)
		}
		dst = append(dst, '{')
		for i, key := range v.keys {
			if i > 0 {
				dst = append(dst, ',')
			}
			dst = AppendString(dst, key)
			dst = append(dst, ':')
			dst = Append(dst, v.vals[i])
		}
		return append(dst, '}')
	case int:
		return strconv.AppendInt(dst, int64(v), 10)
	case int64:
		return strconv.AppendInt(dst, v, 10)
	case float64:
		if n, ok := Float(v); ok {
			return append(dst, n...)
		}
		return append(dst, "null"...)
	case []string:
		dst = append(dst, '[')
		for i, item := range v {
			if i > 0 {
				dst = append(dst, ',')
			}
			dst = AppendString(dst, item)
		}
		return append(dst, ']')
	default:
		// A Blob or NotJSON is never part of an event; written as null if one gets here.
		return append(dst, "null"...)
	}
}

const hexDigits = "0123456789abcdef"

// AppendString appends text as a JSON string, as JSON.stringify writes one.
func AppendString(dst []byte, text string) []byte {
	dst = append(dst, '"')
	start := 0
	for i := 0; i < len(text); {
		c := text[i]
		if c >= utf8.RuneSelf {
			r, size := utf8.DecodeRuneInString(text[i:])
			if r == utf8.RuneError && size == 1 {
				dst = append(dst, text[start:i]...)
				dst = append(dst, "�"...)
				i++
				start = i
				continue
			}
			i += size
			continue
		}
		if c >= 0x20 && c != '"' && c != '\\' {
			i++
			continue
		}
		dst = append(dst, text[start:i]...)
		switch c {
		case '"':
			dst = append(dst, '\\', '"')
		case '\\':
			dst = append(dst, '\\', '\\')
		case '\b':
			dst = append(dst, '\\', 'b')
		case '\f':
			dst = append(dst, '\\', 'f')
		case '\n':
			dst = append(dst, '\\', 'n')
		case '\r':
			dst = append(dst, '\\', 'r')
		case '\t':
			dst = append(dst, '\\', 't')
		default:
			dst = append(dst, '\\', 'u', '0', '0', hexDigits[c>>4], hexDigits[c&0xf])
		}
		i++
		start = i
	}
	dst = append(dst, text[start:]...)
	return append(dst, '"')
}

// Quote returns text as a JSON string.
func Quote(text string) string { return string(AppendString(nil, text)) }
