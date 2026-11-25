package lsquaremcp

import "encoding/json"

// Reading JSON by where its bytes are. What the wrapper changes of a call's arguments, or of a
// listed schema, is a member taken out or put in: every other byte stays as it was, in the order
// it was in, which decoding and writing again wouldn't promise.
//
// These read JSON that is valid (see valid): on anything else they stop, and say so. They never
// recurse, so a deep value costs no stack.

// member is one member of an object: its key between the quotes, as written, and where the member
// and its value are.
type member struct {
	key []byte
	// The key has an escape, so it isn't its own text.
	escaped bool
	// The key's opening quote, the value's first byte, and the byte after the value.
	start, value, end int
}

// is reports whether the member's key is name, without decoding a key that has no escape.
func (m member) is(name string) bool {
	if !m.escaped {
		return string(m.key) == name
	}
	text, ok := m.name()
	return ok && text == name
}

// name is the member's key.
func (m member) name() (string, bool) {
	if !m.escaped {
		return string(m.key), true
	}
	quoted := append(append([]byte{'"'}, m.key...), '"')
	var text string
	if err := json.Unmarshal(quoted, &text); err != nil {
		return "", false
	}
	return text, true
}

func skipSpace(raw []byte, at int) int {
	for at < len(raw) {
		switch raw[at] {
		case ' ', '\t', '\n', '\r':
			at++
		default:
			return at
		}
	}
	return at
}

// stringEnd is the byte after the string that starts at raw[at].
func stringEnd(raw []byte, at int) (end int, escaped, ok bool) {
	if at >= len(raw) || raw[at] != '"' {
		return 0, false, false
	}
	for i := at + 1; i < len(raw); i++ {
		switch raw[i] {
		case '\\':
			escaped = true
			i++
		case '"':
			return i + 1, escaped, true
		}
	}
	return 0, false, false
}

// valueEnd is the byte after the value that starts at raw[at].
func valueEnd(raw []byte, at int) (int, bool) {
	if at >= len(raw) {
		return 0, false
	}
	switch raw[at] {
	case '"':
		end, _, ok := stringEnd(raw, at)
		return end, ok
	case '{', '[':
		depth := 0
		for i := at; i < len(raw); i++ {
			switch raw[i] {
			case '"':
				end, _, ok := stringEnd(raw, i)
				if !ok {
					return 0, false
				}
				i = end - 1
			case '{', '[':
				depth++
			case '}', ']':
				depth--
				if depth == 0 {
					return i + 1, true
				}
			}
		}
		return 0, false
	}
	end := at
scalar:
	for end < len(raw) {
		switch raw[end] {
		case ',', '}', ']', ' ', '\t', '\n', '\r':
			break scalar
		}
		end++
	}
	return end, end > at
}

// eachMember calls visit with each member of the object that starts at raw[at], in order, until
// visit returns false. It returns the byte after the object, when visit saw every member.
func eachMember(raw []byte, at int, visit func(member) bool) (end int, ok bool) {
	if at >= len(raw) || raw[at] != '{' {
		return 0, false
	}
	at = skipSpace(raw, at+1)
	if at < len(raw) && raw[at] == '}' {
		return at + 1, true
	}
	for {
		keyEnd, escaped, isString := stringEnd(raw, at)
		if !isString {
			return 0, false
		}
		colon := skipSpace(raw, keyEnd)
		if colon >= len(raw) || raw[colon] != ':' {
			return 0, false
		}
		value := skipSpace(raw, colon+1)
		valueEnds, isValue := valueEnd(raw, value)
		if !isValue {
			return 0, false
		}
		if !visit(member{key: raw[at+1 : keyEnd-1], escaped: escaped, start: at, value: value, end: valueEnds}) {
			return 0, false
		}
		next := skipSpace(raw, valueEnds)
		if next >= len(raw) {
			return 0, false
		}
		switch raw[next] {
		case '}':
			return next + 1, true
		case ',':
			at = skipSpace(raw, next+1)
		default:
			return 0, false
		}
	}
}

// eachElement calls visit with where each element of the list that starts at raw[at] is, in
// order, until visit returns false. It returns the byte after the list, when visit saw them all.
func eachElement(raw []byte, at int, visit func(start, end int) bool) (end int, ok bool) {
	if at >= len(raw) || raw[at] != '[' {
		return 0, false
	}
	at = skipSpace(raw, at+1)
	if at < len(raw) && raw[at] == ']' {
		return at + 1, true
	}
	for {
		elementEnd, isValue := valueEnd(raw, at)
		if !isValue || !visit(at, elementEnd) {
			return 0, false
		}
		next := skipSpace(raw, elementEnd)
		if next >= len(raw) {
			return 0, false
		}
		switch raw[next] {
		case ']':
			return next + 1, true
		case ',':
			at = skipSpace(raw, next+1)
		default:
			return 0, false
		}
	}
}

// text is the string a JSON string value is. Not a string: ok is false.
func text(value []byte) (string, bool) {
	if len(value) < 2 || value[0] != '"' {
		return "", false
	}
	var out string
	if err := json.Unmarshal(value, &out); err != nil {
		return "", false
	}
	return out, true
}

// valid reports whether raw is one JSON value, as the MCP SDK's own decoder would take it.
func valid(raw []byte) bool { return json.Valid(raw) }
