package lsquaremcp

// The extras' rules that don't depend on a call: which of their inputs a tool's listed schema
// leaves to the wrapper, the listed copy that shows them, get_more_tools, and session IDs.
// Parts adapted from AgentCat's TypeScript SDK (MIT, © 2025 AgentCat, Inc.; see
// THIRD_PARTY_NOTICES): adding the extras' inputs to a listed tool's schema.

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"slices"
	"strings"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

const (
	sessionInput = "session_id"
	intentInput  = "intent"
	missingTools = "get_more_tools"
)

// What agents are told, word for word what the TypeScript and Python SDKs tell them.
const (
	sessionDescription      = "The ID of the task this call belongs to. Leave it out on your first call. On every later call for the same task, send the ID an earlier result gave you, and pass it to any subagent working on that task."
	intentDescription       = "In one sentence, why you're calling this tool and what the user is trying to do. No personal data or secrets."
	missingToolsDescription = "Call this when none of the available tools fits what you need, or one fits only in part. It doesn't return new tools: it tells the server's developers what to add."
	needDescription         = "What you needed to do and couldn't, in one or two sentences. No personal data or secrets."
	missingToolsAnswer      = "Noted. No other tools are available right now."
	missingToolsNoNeed      = "Invalid arguments for tool get_more_tools: need is required, as a string saying what you needed to do and couldn't."
)

// The protocol version from which a connection has no session of its own.
const sessionlessProtocol = "2026-07-28"

const sessionAlphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"

// newSessionID is a new session ID: ses_ and 22 random characters of the base64url alphabet.
func newSessionID() string {
	var random [22]byte
	// Never fails: crypto/rand ends the program if the system has no randomness to give.
	_, _ = rand.Read(random[:])
	id := make([]byte, 0, 4+len(random))
	id = append(id, "ses_"...)
	// 64 characters: each byte's low 6 bits pick one, evenly.
	for _, b := range random {
		id = append(id, sessionAlphabet[b&63])
	}
	return string(id)
}

// sessionFormat reports whether id is a session ID an agent may send back: ses_ and 22 letters,
// digits, - or _.
func sessionFormat(id string) bool {
	if len(id) != 26 || !strings.HasPrefix(id, "ses_") {
		return false
	}
	for i := 4; i < len(id); i++ {
		c := id[i]
		if !(c >= 'A' && c <= 'Z' || c >= 'a' && c <= 'z' || c >= '0' && c <= '9' || c == '-' || c == '_') {
			return false
		}
	}
	return true
}

// idLine is the text that gives the agent its task's ID, at the end of a result.
func idLine(id string) string {
	return "Task ID: " + id + ". Send it as session_id on later calls for this task, and pass it to any subagent working on it."
}

// sessionHash is a protocol session's ID as events carry it: the ID itself can be a live handle
// to the session, and grouping only needs the same value each time.
func sessionHash(id string) string {
	sum := sha256.Sum256([]byte(id))
	return hex.EncodeToString(sum[:])
}

// sessionless reports whether a call under this protocol version has no session of its
// connection's: 2026-07-28 and later. A version that isn't a date counts as older.
func sessionless(version string) bool {
	if len(version) != 10 || version[4] != '-' || version[7] != '-' {
		return false
	}
	for i := 0; i < len(version); i++ {
		if i != 4 && i != 7 && (version[i] < '0' || version[i] > '9') {
			return false
		}
	}
	return version >= sessionlessProtocol
}

// property is an input as a schema lists it: a string, with what it is for.
type property struct {
	Type        string `json:"type"`
	Description string `json:"description"`
}

// inputProperty is the member an input is listed as: an optional string, with its description.
// Written by the JSON encoder, as everything the wrapper adds to a schema is.
func inputProperty(name string) string {
	description := sessionDescription
	if name == intentInput {
		description = intentDescription
	}
	member, _ := json.Marshal(map[string]property{name: {Type: "string", Description: description}})
	// The member itself, without the braces of the object it was written in.
	return string(member[1 : len(member)-1])
}

// missingToolsTool is get_more_tools as listed: it takes need, and session_id when sessions are
// on. It changes nothing on the server, so it's read-only, and its need leaves the server.
func missingToolsTool(sessions bool) *mcp.Tool {
	type properties struct {
		Need      property  `json:"need"`
		SessionID *property `json:"session_id,omitempty"`
	}
	schema := struct {
		Type       string     `json:"type"`
		Properties properties `json:"properties"`
		Required   []string   `json:"required"`
	}{Type: "object", Properties: properties{Need: property{Type: "string", Description: needDescription}}, Required: []string{"need"}}
	if sessions {
		schema.Properties.SessionID = &property{Type: "string", Description: sessionDescription}
	}
	written, _ := json.Marshal(schema)
	no, yes := false, true
	return &mcp.Tool{
		Name:        missingTools,
		Description: missingToolsDescription,
		InputSchema: json.RawMessage(written),
		Annotations: &mcp.ToolAnnotations{ReadOnlyHint: true, DestructiveHint: &no, OpenWorldHint: &yes},
	}
}

// missingToolsReply is the wrapper's reply to get_more_tools: an error result, as the MCP SDK
// gives for refused arguments, without a need that is a string.
func missingToolsReply(arguments []byte) *mcp.CallToolResult {
	needed := false
	at := skipSpace(arguments, 0)
	_, _ = eachMember(arguments, at, func(m member) bool {
		if m.is("need") {
			// The last one counts, as a decoder reads a key written twice.
			_, needed = text(arguments[m.value:m.end])
		}
		return true
	})
	if !needed || !valid(arguments) {
		return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: missingToolsNoNeed}}, IsError: true}
	}
	return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: missingToolsAnswer}}}
}

// ownership is what a tool's listed input schema says of the extras' inputs.
type ownership struct {
	// The schema is an object of named properties, so what it names is all the tool takes.
	readable bool
	// Why not, when it isn't.
	why string
	// The tool names the input itself: it is the tool's own, and stays in its calls.
	session, intent bool
	// The schema refuses what it doesn't name (additionalProperties is false).
	closed bool
}

const unreadableListing = "its listed input schema is more than an object of named properties"

// Keys a listed schema's root may hold for inputs to be added to it: the object's own, and those
// that only describe.
var rootKeys = map[string]bool{
	"type": true, "properties": true, "required": true, "additionalProperties": true,
	"$schema": true, "$id": true, "$comment": true, "$defs": true, "definitions": true,
	"title": true, "description": true, "default": true, "examples": true, "deprecated": true,
}

const (
	// How long a schema's required may be for it to be read.
	requiredNames = 10_000
	// How much of a listed schema is read to find its references, at most.
	walkNodes = 10_000
	walkDepth = 64
)

// root is what the wrapper reads of a listed schema's root.
type root struct {
	// Where the root's closing brace is.
	closing int
	// The properties member, when the root has one, and how many members it has.
	properties    *member
	propertyCount int
	// The inputs properties names, and the ones required names.
	named, required struct{ session, intent bool }
	// additionalProperties is false.
	closed bool
}

// readRoot reads a listed schema's root: ok only when it holds nothing but type "object",
// properties, a required list, additionalProperties absent or false, and the keys that only
// describe. With anyRequired, required may hold anything; otherwise only names. A key written
// twice makes it unreadable: which one counts is each reader's own choice.
func readRoot(schema []byte, anyRequired bool) (read root, ok bool) {
	start := skipSpace(schema, 0)
	var kind, properties, required, additional *member
	seen := map[string]bool{}
	fine := true
	end, isObject := eachMember(schema, start, func(m member) bool {
		name, isName := m.name()
		if !isName || !rootKeys[name] || seen[name] {
			fine = false
			return false
		}
		seen[name] = true
		at := m
		switch name {
		case "type":
			kind = &at
		case "properties":
			properties = &at
		case "required":
			required = &at
		case "additionalProperties":
			additional = &at
		}
		return true
	})
	if !isObject || !fine || skipSpace(schema, end) != len(schema) || kind == nil {
		return read, false
	}
	read.closing = end - 1
	if is, isText := text(schema[kind.value:kind.end]); !isText || is != "object" {
		return read, false
	}
	if properties != nil {
		names := map[string]bool{}
		_, isObject := eachMember(schema, properties.value, func(m member) bool {
			name, isName := m.name()
			if !isName || names[name] {
				fine = false
				return false
			}
			names[name] = true
			return true
		})
		if !isObject || !fine {
			return read, false
		}
		read.properties, read.propertyCount = properties, len(names)
		read.named.session, read.named.intent = names[sessionInput], names[intentInput]
	}
	if required != nil {
		count := 0
		_, isList := eachElement(schema, required.value, func(from, to int) bool {
			count++
			name, isName := text(schema[from:to])
			if count > requiredNames || (!isName && !anyRequired) {
				fine = false
				return false
			}
			if isName {
				read.required.session = read.required.session || name == sessionInput
				read.required.intent = read.required.intent || name == intentInput
			}
			return true
		})
		if !isList || !fine {
			return read, false
		}
	}
	if additional != nil {
		if string(schema[additional.value:additional.end]) != "false" {
			return read, false
		}
		read.closed = true
	}
	return read, true
}

// listedOwnership is what a listed JSON Schema says of the extras' inputs: readable only when its
// root holds only type "object", properties, a required of names, additionalProperties absent or
// false, and the keys that only describe. An input it names, in properties or required, is the
// tool's own.
func listedOwnership(schema []byte) ownership {
	read, ok := readRoot(schema, false)
	if !ok || !valid(schema) {
		return ownership{why: unreadableListing}
	}
	return ownership{
		readable: true,
		session:  read.named.session || read.required.session,
		intent:   read.named.intent || read.required.intent,
		closed:   read.closed,
	}
}

// advertise is a copy of a tool's listed input schema with names added as optional string
// properties, each only where the schema takes it as one more without changing what it accepts;
// nil when none can be added. The root may hold only type "object", properties, a required
// without the name, additionalProperties absent or false, and keys that only describe. Every
// reference in it must point into its $defs, definitions or one of its own properties: one to the
// root, as a recursive schema has, would show the inputs at nested levels too, where they aren't
// removed. Every byte of the schema stays as it was: the inputs are put in at the end of its
// properties, or as a properties member at its end.
func advertise(schema []byte, names []string) []byte {
	if len(names) == 0 {
		return nil
	}
	read, ok := readRoot(schema, true)
	if !ok || !valid(schema) {
		return nil
	}
	var adding []string
	for _, name := range names {
		named, required := read.named.session, read.required.session
		if name == intentInput {
			named, required = read.named.intent, read.required.intent
		}
		if !named && !required {
			adding = append(adding, name)
		}
	}
	if len(adding) == 0 || !localReferences(schema, adding) {
		return nil
	}
	added := make([]string, len(adding))
	for i, name := range adding {
		added[i] = inputProperty(name)
	}
	at, put := read.closing, `,"properties":{`+strings.Join(added, ",")+`}`
	if read.properties != nil {
		at, put = read.properties.end-1, strings.Join(added, ",")
		if read.propertyCount > 0 {
			put = "," + put
		}
	}
	return slices.Concat(schema[:at], []byte(put), schema[at:])
}

// localReferences reports whether every $ref in the schema points into its $defs, definitions or
// a property it already has, and it has no dynamic references. Each entry read counts against
// the budget, so a wide object or list costs no more than a deep one.
func localReferences(schema []byte, adding []string) bool {
	type node struct{ at, depth int }
	stack := []node{{skipSpace(schema, 0), 0}}
	budget := walkNodes
	for len(stack) > 0 {
		here := stack[len(stack)-1]
		stack = stack[:len(stack)-1]
		if here.depth > walkDepth || here.at >= len(schema) {
			return false
		}
		fine := true
		enter := func(from int) {
			if schema[from] == '{' || schema[from] == '[' {
				stack = append(stack, node{from, here.depth + 1})
			}
		}
		var read bool
		if schema[here.at] == '{' {
			_, read = eachMember(schema, here.at, func(m member) bool {
				budget--
				name, isName := m.name()
				// Whatever their case: the schema's own reader may take $REF for $ref.
				if budget < 0 || !isName || strings.EqualFold(name, "$dynamicRef") || strings.EqualFold(name, "$recursiveRef") {
					fine = false
					return false
				}
				if strings.EqualFold(name, "$ref") {
					if ref, isText := text(schema[m.value:m.end]); !isText || !localReference(ref, adding) {
						fine = false
						return false
					}
				}
				enter(m.value)
				return true
			})
		} else {
			_, read = eachElement(schema, here.at, func(from, to int) bool {
				budget--
				if budget < 0 {
					fine = false
					return false
				}
				enter(from)
				return true
			})
		}
		if !read || !fine {
			return false
		}
	}
	return true
}

func localReference(ref string, adding []string) bool {
	// Percent-encoding could spell any name: such a reference counts as unknown.
	if strings.Contains(ref, "%") {
		return false
	}
	if strings.HasPrefix(ref, "#/$defs/") || strings.HasPrefix(ref, "#/definitions/") {
		return true
	}
	rest, isProperty := strings.CutPrefix(ref, "#/properties/")
	if !isProperty {
		return false
	}
	name, _, _ := strings.Cut(rest, "/")
	name = strings.ReplaceAll(strings.ReplaceAll(name, "~1", "/"), "~0", "~")
	for _, added := range adding {
		if added == name {
			return false
		}
	}
	return true
}

// taken is what the wrapper took out of a call's arguments.
type taken struct {
	// The arguments without them: the arguments themselves when nothing was taken out, and
	// otherwise a copy of exactly their size, which nothing else holds.
	arguments []byte
	// A session ID in the right format that the agent sent, and a non-empty intent.
	sessionID string
	intent    string
	// Something was taken out.
	cut bool
}

// takeInputs takes session_id and intent, each when asked to, out of a call's arguments, valid
// or not, into a copy: the arguments themselves are never changed. Every other byte stays as it
// was, the spaces around the members that stay included: what goes is the member, and what
// separated it from the next one (or, for the last member, from the one before). Arguments that
// aren't an object, or aren't JSON, are left as they are. A key written twice is taken out
// twice, and its last value counts, as a decoder reads it.
func takeInputs(arguments []byte, sessions, intents bool) taken {
	out := taken{arguments: arguments}
	if !sessions && !intents {
		return out
	}
	type span struct{ from, to int }
	var (
		drops           []span
		session, intent []byte
		// Where the first member starts, the last one ends, and the last one kept ends.
		first, last, kept = -1, 0, -1
		// The last member is taken out: its span ends where the next member starts, if one comes.
		open bool
	)
	start := skipSpace(arguments, 0)
	end, isObject := eachMember(arguments, start, func(m member) bool {
		if first < 0 {
			first = m.start
		}
		if open {
			drops[len(drops)-1].to, open = m.start, false
		}
		last = m.end
		switch {
		case sessions && m.is(sessionInput):
			session = arguments[m.value:m.end]
		case intents && m.is(intentInput):
			intent = arguments[m.value:m.end]
		default:
			kept = m.end
			return true
		}
		drops, open = append(drops, span{m.start, m.end}), true
		return true
	})
	// What the SDK would refuse is left for it to refuse.
	if !isObject || len(drops) == 0 || skipSpace(arguments, end) != len(arguments) || !valid(arguments) {
		return out
	}
	if open {
		// The object's last member goes, with every member taken out just before it and what
		// separated them from the last one kept.
		from := first
		if kept >= 0 {
			from = kept
		}
		for len(drops) > 0 && drops[len(drops)-1].from >= from {
			drops = drops[:len(drops)-1]
		}
		drops = append(drops, span{from, last})
	}
	size := len(arguments)
	for _, drop := range drops {
		size -= drop.to - drop.from
	}
	// Exactly its size: a copy that kept the arguments' own capacity would hold on to all of it.
	without, at := make([]byte, 0, size), 0
	for _, drop := range drops {
		without, at = append(without, arguments[at:drop.from]...), drop.to
	}
	out.arguments, out.cut = append(without, arguments[at:]...), true
	if id, isText := text(session); isText && sessionFormat(id) {
		out.sessionID = id
	}
	if why, isText := text(intent); isText {
		out.intent = why
	}
	return out
}
