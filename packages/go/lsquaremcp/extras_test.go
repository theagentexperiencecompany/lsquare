package lsquaremcp

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// quoted is text as a JSON string.
func quoted(text string) string {
	out, _ := json.Marshal(text)
	return string(out)
}

func TestSessionIDs(t *testing.T) {
	seen := map[string]bool{}
	for range 200 {
		id := newSessionID()
		if !sessionFormat(id) {
			t.Fatalf("a new ID isn't in the format: %q", id)
		}
		if seen[id] {
			t.Fatalf("the same ID twice: %q", id)
		}
		seen[id] = true
	}
	for id, want := range map[string]bool{
		"ses_" + strings.Repeat("a", 22):        true,
		"ses_AZaz09-_AZaz09-_AZaz09":            true,
		"ses_" + strings.Repeat("a", 21):        false,
		"ses_" + strings.Repeat("a", 23):        false,
		"SES_" + strings.Repeat("a", 22):        false,
		"ses_" + strings.Repeat("a", 21) + "+":  false,
		"ses_" + strings.Repeat("a", 21) + "\n": false,
		"ses_" + strings.Repeat("a", 22) + "\n": false,
		"":                                      false,
	} {
		if got := sessionFormat(id); got != want {
			t.Errorf("sessionFormat(%q) = %v, want %v", id, got, want)
		}
	}
	if got, want := idLine("ses_x"), "Task ID: ses_x. Send it as session_id on later calls for this task, and pass it to any subagent working on it."; got != want {
		t.Errorf("idLine = %q", got)
	}
	// SHA-256 of "abc", as every SDK writes it.
	if got := sessionHash("abc"); got != "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad" {
		t.Errorf("sessionHash = %q", got)
	}
}

func TestSessionlessFromTheNewProtocol(t *testing.T) {
	for version, want := range map[string]bool{
		"2026-07-28": true, "2026-07-29": true, "2027-01-01": true,
		"2026-07-27": false, "2025-11-25": false, "2025-06-18": false,
		"": false, "draft": false, "2026-7-28": false, "2026-07-28x": false, "9999-99-9x": false,
	} {
		if got := sessionless(version); got != want {
			t.Errorf("sessionless(%q) = %v, want %v", version, got, want)
		}
	}
}

func TestTakeInputs(t *testing.T) {
	id := "ses_" + strings.Repeat("k", 22)
	cases := []struct {
		name              string
		arguments         string
		sessions, intents bool
		want              string
		id, intent        string
	}{
		{"both", `{"city":"Rome","session_id":"` + id + `","intent":"find the weather"}`, true, true, `{"city":"Rome"}`, id, "find the weather"},
		{"first and last", `{"session_id":"` + id + `","a":1,"b":[1,{"session_id":2}],"intent":"x"}`, true, true, `{"a":1,"b":[1,{"session_id":2}]}`, id, "x"},
		{"only them", `{"intent":"x","session_id":"` + id + `"}`, true, true, `{}`, id, "x"},
		{"every other byte stays", "{ \"a\" : 1 ,\n \"intent\" : \"x\" , \"b\" : { \"c\" : [ 1 , 2 ] } }\n", true, true, "{ \"a\" : 1 ,\n \"b\" : { \"c\" : [ 1 , 2 ] } }\n", "", "x"},
		{"the first of several", "{\"intent\":null,\"n\":2,  \"v\" : [0, 2]  }", true, true, "{\"n\":2,  \"v\" : [0, 2]  }", "", ""},
		{"the last, and what came before it", "{ \"a\" : 1 , \"intent\" : \"x\" ,\t\"session_id\" : 2 }", true, true, "{ \"a\" : 1 }", "", "x"},
		{"between two that stay, twice", `{"a":1, "intent":"x", "b":2, "session_id":"s", "c":3}`, true, true, `{"a":1, "b":2, "c":3}`, "", "x"},
		{"all of them, with spaces", "{ \"intent\" : \"x\" , \"session_id\" : null }", true, true, "{  }", "", "x"},
		{"an ID in another format is taken out and ignored", `{"session_id":"abc","a":1}`, true, true, `{"a":1}`, "", ""},
		{"an ID that isn't text", `{"session_id":42,"a":1}`, true, true, `{"a":1}`, "", ""},
		{"a null intent", `{"intent":null,"a":1}`, true, true, `{"a":1}`, "", ""},
		{"an empty intent", `{"intent":"","a":1}`, true, true, `{"a":1}`, "", ""},
		{"an intent that is a list", `{"intent":["x"],"a":1}`, true, true, `{"a":1}`, "", ""},
		{"written twice: the last counts", `{"session_id":"` + id + `","session_id":"nope","intent":"a","intent":"b"}`, true, true, `{}`, "", "b"},
		{"an escaped key", `{"session\u005fid":"` + id + `","a":1}`, true, true, `{"a":1}`, id, ""},
		{"an escaped value", `{"intent":"caf\u00e9 \"quoted\" \\ end","a":1}`, true, true, `{"a":1}`, "", `café "quoted" \ end`},
		{"sessions only", `{"session_id":"` + id + `","intent":"x"}`, true, false, `{"intent":"x"}`, id, ""},
		{"intent only", `{"session_id":"` + id + `","intent":"x"}`, false, true, `{"session_id":"` + id + `"}`, "", "x"},
		{"a key that only looks like one", `{"Session_ID":"` + id + `","intent ":"x","xintent":1}`, true, true, "", "", ""},
		{"a value that mentions them", `{"a":"session_id and intent","b":{"intent":"x"}}`, true, true, "", "", ""},
		{"nothing to take", `{"a":1}`, true, true, "", "", ""},
		{"neither is on", `{"session_id":"` + id + `","intent":"x"}`, false, false, "", "", ""},
		{"no arguments", ``, true, true, "", "", ""},
		{"null", `null`, true, true, "", "", ""},
		{"a list", `[{"intent":"x"}]`, true, true, "", "", ""},
		{"text", `"intent"`, true, true, "", "", ""},
		{"not JSON: a trailing comma", `{"intent":"x",}`, true, true, "", "", ""},
		{"not JSON: a bad value", `{"intent":"x","a":tru}`, true, true, "", "", ""},
		{"not JSON: more after it", `{"intent":"x"} {}`, true, true, "", "", ""},
		{"not JSON: cut short", `{"intent":"x","a":{"b":`, true, true, "", "", ""},
		{"not JSON: a bad escape in a string", `{"intent":"x","a":"\q"}`, true, true, "", "", ""},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			arguments := []byte(c.arguments)
			before := string(arguments)
			got := takeInputs(arguments, c.sessions, c.intents)
			if string(arguments) != before {
				t.Fatalf("the arguments themselves changed: %s", arguments)
			}
			want := c.want
			if want == "" {
				// Left as they are: the very same bytes, not a copy.
				want = c.arguments
				if len(arguments) > 0 && (len(got.arguments) == 0 || &got.arguments[0] != &arguments[0]) {
					t.Errorf("arguments left as they are should be the same slice")
				}
			} else if !json.Valid(got.arguments) {
				t.Errorf("what is passed on isn't JSON: %s", got.arguments)
			}
			if string(got.arguments) != want {
				t.Errorf("arguments = %s, want %s", got.arguments, want)
			}
			if got.sessionID != c.id || got.intent != c.intent {
				t.Errorf("took session %q and intent %q, want %q and %q", got.sessionID, got.intent, c.id, c.intent)
			}
		})
	}
}

func TestTakeInputsFromLargeArguments(t *testing.T) {
	var arguments strings.Builder
	arguments.WriteString(`{"intent":"x"`)
	for i := range 50_000 {
		arguments.WriteString(`,"k` + strings.Repeat("0", i%7) + `":[{"session_id":1,"deep":"` + strings.Repeat("y", 20) + `"}]`)
	}
	arguments.WriteString(`,"session_id":"` + "ses_" + strings.Repeat("z", 22) + `"}`)
	got := takeInputs([]byte(arguments.String()), true, true)
	if got.intent != "x" || got.sessionID != "ses_"+strings.Repeat("z", 22) {
		t.Fatalf("took %q and %q", got.sessionID, got.intent)
	}
	if !json.Valid(got.arguments) || strings.Contains(string(got.arguments), `"intent"`) || !strings.Contains(string(got.arguments), `{"session_id":1,"deep"`) {
		t.Fatal("the rest didn't come through as it was")
	}
	// Deep values cost no stack. Past the 10,000 levels Go's JSON reader takes, the arguments
	// aren't JSON to it, and are left as they are.
	nested := func(levels int) string {
		return `{"a":` + strings.Repeat("[", levels) + strings.Repeat("]", levels) + `,"intent":"x"}`
	}
	if got := takeInputs([]byte(nested(5_000)), true, true); got.intent != "x" || string(got.arguments) != strings.TrimSuffix(nested(5_000), `,"intent":"x"}`)+"}" {
		t.Fatal("a deep value was mishandled")
	}
	if got := takeInputs([]byte(nested(200_000)), true, true); got.intent != "" || string(got.arguments) != nested(200_000) {
		t.Fatal("arguments deeper than a JSON reader takes should be left as they are")
	}
}

// What the MCP SDK lists for a tool whose input is a struct.
type weatherInput struct {
	City string `json:"city" jsonschema:"the city"`
	Days int    `json:"days,omitempty"`
}

type sessionOwner struct {
	SessionID string `json:"session_id"`
	Query     string `json:"query"`
}

// inferred is the input schema the MCP SDK lists for a tool whose input is a T, as a client
// receives it.
func inferred[T any](t *testing.T) []byte {
	t.Helper()
	server := mcp.NewServer(&mcp.Implementation{Name: "schemas", Version: "1"}, nil)
	mcp.AddTool(server, &mcp.Tool{Name: "tool"}, func(ctx context.Context, request *mcp.CallToolRequest, input T) (*mcp.CallToolResult, any, error) {
		return &mcp.CallToolResult{}, nil, nil
	})
	ctx := context.Background()
	serverTransport, clientTransport := mcp.NewInMemoryTransports()
	serving, err := server.Connect(ctx, serverTransport, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = serving.Close() }()
	session, err := mcp.NewClient(&mcp.Implementation{Name: "test", Version: "1"}, nil).Connect(ctx, clientTransport, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = session.Close() }()
	listed, err := session.ListTools(ctx, nil)
	if err != nil || len(listed.Tools) != 1 {
		t.Fatalf("listing: %v", err)
	}
	out, err := json.Marshal(listed.Tools[0].InputSchema)
	if err != nil {
		t.Fatal(err)
	}
	return out
}

func TestListedOwnership(t *testing.T) {
	if got := listedOwnership(inferred[weatherInput](t)); !got.readable || !got.closed || got.session || got.intent {
		t.Errorf("a struct's schema: %+v (%s)", got, inferred[weatherInput](t))
	}
	if got := listedOwnership(inferred[sessionOwner](t)); !got.readable || !got.session || got.intent {
		t.Errorf("a struct with its own session_id: %+v", got)
	}
	if got := listedOwnership(inferred[map[string]any](t)); got.readable {
		t.Errorf("a map's schema keeps any key, so it can't be read: %+v (%s)", got, inferred[map[string]any](t))
	}
	cases := []struct {
		name                              string
		schema                            string
		readable, session, intent, closed bool
	}{
		{"bare object", `{"type":"object"}`, true, false, false, false},
		{"closed", `{"type":"object","properties":{"a":{"type":"string"}},"required":["a"],"additionalProperties":false}`, true, false, false, true},
		{"describing keys", `{"$schema":"x","$id":"y","$comment":"c","$defs":{},"definitions":{},"title":"t","description":"d","default":{},"examples":[],"deprecated":false,"type":"object"}`, true, false, false, false},
		{"names intent", `{"type":"object","properties":{"intent":{"type":"string"}}}`, true, false, true, false},
		{"requires session_id without naming it", `{"type":"object","required":["session_id"]}`, true, true, false, false},
		{"an escaped name", `{"type":"object","properties":{"session\u005fid":{}}}`, true, true, false, false},
		{"spaces", " {\n \"type\" : \"object\" ,\n \"properties\" : { } }\n", true, false, false, false},
		{"no type", `{"properties":{}}`, false, false, false, false},
		{"another type", `{"type":"string"}`, false, false, false, false},
		{"a list of types", `{"type":["object","null"]}`, false, false, false, false},
		{"keeps other keys", `{"type":"object","additionalProperties":true}`, false, false, false, false},
		{"other keys have a schema", `{"type":"object","additionalProperties":{"type":"string"}}`, false, false, false, false},
		{"a combinator", `{"type":"object","anyOf":[{"required":["a"]}]}`, false, false, false, false},
		{"a pattern", `{"type":"object","patternProperties":{"^s":{}}}`, false, false, false, false},
		{"a limit", `{"type":"object","maxProperties":2}`, false, false, false, false},
		{"a reference at the root", `{"type":"object","$ref":"#/$defs/a"}`, false, false, false, false},
		{"properties that are a list", `{"type":"object","properties":[]}`, false, false, false, false},
		{"properties that are null", `{"type":"object","properties":null}`, false, false, false, false},
		{"required that isn't a list", `{"type":"object","required":"a"}`, false, false, false, false},
		{"required that isn't names", `{"type":"object","required":["a",1]}`, false, false, false, false},
		{"a key twice", `{"type":"object","properties":{},"properties":{"session_id":{}}}`, false, false, false, false},
		{"a property twice", `{"type":"object","properties":{"a":{},"a":{}}}`, false, false, false, false},
		{"a list", `[{"type":"object"}]`, false, false, false, false},
		{"true", `true`, false, false, false, false},
		{"nothing", ``, false, false, false, false},
		{"not JSON", `{"type":"object",}`, false, false, false, false},
		{"more after it", `{"type":"object"}{}`, false, false, false, false},
	}
	for _, c := range cases {
		got := listedOwnership([]byte(c.schema))
		if got.readable != c.readable || got.session != c.session || got.intent != c.intent || got.closed != c.closed {
			t.Errorf("%s: %+v", c.name, got)
		}
		if !got.readable && got.why == "" {
			t.Errorf("%s: unreadable without saying why", c.name)
		}
	}
	long := `{"type":"object","required":[` + strings.Repeat(`"a",`, 10_000) + `"a"]}`
	if got := listedOwnership([]byte(long)); got.readable {
		t.Error("a required of more than 10,000 names should not be read")
	}
}

func TestAdvertise(t *testing.T) {
	both := []string{sessionInput, intentInput}
	session := `"session_id":{"type":"string","description":"The ID of the task this call belongs to. Leave it out on your first call. On every later call for the same task, send the ID an earlier result gave you, and pass it to any subagent working on that task."}`
	intent := `"intent":{"type":"string","description":"In one sentence, why you're calling this tool and what the user is trying to do. No personal data or secrets."}`
	cases := []struct {
		name   string
		schema string
		names  []string
		want   string
	}{
		{"added after the tool's own", `{"type":"object","properties":{"city":{"type":"string"}},"required":["city"],"additionalProperties":false}`, both,
			`{"type":"object","properties":{"city":{"type":"string"},` + session + `,` + intent + `},"required":["city"],"additionalProperties":false}`},
		{"empty properties", `{"type":"object","properties":{}}`, both, `{"type":"object","properties":{` + session + `,` + intent + `}}`},
		{"no properties", `{"type":"object"}`, both, `{"type":"object","properties":{` + session + `,` + intent + `}}`},
		{"no properties, other keys", `{"type":"object","additionalProperties":false,"title":"t"}`, []string{intentInput}, `{"type":"object","additionalProperties":false,"title":"t","properties":{` + intent + `}}`},
		{"the tool has one already", `{"type":"object","properties":{"intent":{"type":"number"}}}`, both, `{"type":"object","properties":{"intent":{"type":"number"},` + session + `}}`},
		{"the tool requires one", `{"type":"object","required":["session_id",7]}`, both, `{"type":"object","required":["session_id",7],"properties":{` + intent + `}}`},
		{"the tool has both", `{"type":"object","properties":{"intent":{},"session_id":{}}}`, both, ``},
		{"no names", `{"type":"object"}`, nil, ``},
		{"spaces kept", "{ \"type\": \"object\",\n  \"properties\": { \"a\": {} }\n}", []string{intentInput}, "{ \"type\": \"object\",\n  \"properties\": { \"a\": {} ," + intent + "}\n}"},
		{"local references", `{"type":"object","properties":{"a":{"$ref":"#/$defs/A"},"b":{"$ref":"#/properties/a"},"c":{"$ref":"#/definitions/C/items"}},"$defs":{"A":{"type":"string"}},"definitions":{"C":{}}}`, []string{intentInput},
			`{"type":"object","properties":{"a":{"$ref":"#/$defs/A"},"b":{"$ref":"#/properties/a"},"c":{"$ref":"#/definitions/C/items"},` + intent + `},"$defs":{"A":{"type":"string"}},"definitions":{"C":{}}}`},
		{"a reference to the root", `{"type":"object","properties":{"child":{"$ref":"#"}}}`, both, ``},
		{"a reference elsewhere", `{"type":"object","properties":{"a":{"$ref":"https://example.com/s.json"}}}`, both, ``},
		{"a reference to the input being added", `{"type":"object","properties":{"a":{"$ref":"#/properties/session_id"}}}`, both, ``},
		{"the same, escaped", `{"type":"object","properties":{"a":{"$ref":"#/properties/session~0id"}}}`, []string{"session~id"}, ``},
		{"a percent-encoded reference", `{"type":"object","properties":{"a":{"$ref":"#/properties/%61"}}}`, both, ``},
		{"a reference that isn't text", `{"type":"object","properties":{"$ref":{"type":"string"}}}`, both, ``},
		{"a dynamic reference", `{"type":"object","properties":{"a":{"$dynamicRef":"#x"}}}`, both, ``},
		{"a recursive reference", `{"type":"object","$defs":{"a":{"$recursiveRef":"#"}}}`, both, ``},
		{"a reference in other letters, which a reader may take for one", `{"type":"object","properties":{"child":{"$REF":"#"}}}`, both, ``},
		{"a dynamic reference in other letters", `{"type":"object","properties":{"a":{"$DynamicRef":"#x"}}}`, both, ``},
		{"a combinator", `{"type":"object","oneOf":[{}]}`, both, ``},
		{"open to other keys by a schema", `{"type":"object","additionalProperties":{}}`, both, ``},
		{"not an object", `{"type":"array"}`, both, ``},
		{"not JSON", `{"type":"object","properties":{"a":}}`, both, ``},
	}
	for _, c := range cases {
		got := advertise([]byte(c.schema), c.names)
		if string(got) != c.want {
			t.Errorf("%s:\n got %s\nwant %s", c.name, got, c.want)
		}
		if got != nil && !json.Valid(got) {
			t.Errorf("%s: not JSON: %s", c.name, got)
		}
	}
	// What the MCP SDK infers for a struct takes both.
	got := advertise(inferred[weatherInput](t), both)
	var listed struct {
		Properties map[string]struct{ Type, Description string } `json:"properties"`
		Required   []string                                      `json:"required"`
	}
	if err := json.Unmarshal(got, &listed); err != nil {
		t.Fatalf("%v: %s", err, got)
	}
	if listed.Properties["session_id"].Type != "string" || listed.Properties["intent"].Description != intentDescription || len(listed.Properties) != 4 || len(listed.Required) != 1 {
		t.Errorf("a struct's schema with the inputs: %s", got)
	}
}

func TestAdvertiseBudgets(t *testing.T) {
	// 10,000 entries are read, and no more.
	wide := func(n int) string {
		return `{"type":"object","examples":[` + strings.TrimSuffix(strings.Repeat("1,", n), ",") + `]}`
	}
	// The root's two members, and the list's entries.
	if advertise([]byte(wide(walkNodes-2)), []string{intentInput}) == nil {
		t.Error("a schema of exactly 10,000 entries should take the input")
	}
	if advertise([]byte(wide(walkNodes-1)), []string{intentInput}) != nil {
		t.Error("a schema of more than 10,000 entries should be left alone")
	}
	deep := func(n int) string {
		return `{"type":"object","default":` + strings.Repeat(`{"a":`, n) + `1` + strings.Repeat(`}`, n) + `}`
	}
	if advertise([]byte(deep(walkDepth)), []string{intentInput}) == nil {
		t.Error("a schema 64 levels deep should take the input")
	}
	if advertise([]byte(deep(walkDepth+1)), []string{intentInput}) != nil {
		t.Error("a schema deeper than 64 levels should be left alone")
	}
}

func TestMissingTools(t *testing.T) {
	listed, err := json.Marshal(missingToolsTool(true))
	if err != nil {
		t.Fatal(err)
	}
	var tool struct {
		Name        string `json:"name"`
		Description string `json:"description"`
		InputSchema struct {
			Type       string                                        `json:"type"`
			Properties map[string]struct{ Type, Description string } `json:"properties"`
			Required   []string                                      `json:"required"`
		} `json:"inputSchema"`
		Annotations map[string]any `json:"annotations"`
	}
	if err := json.Unmarshal(listed, &tool); err != nil {
		t.Fatal(err)
	}
	if tool.Name != "get_more_tools" || tool.Description != missingToolsDescription || tool.InputSchema.Type != "object" ||
		tool.InputSchema.Properties["need"].Description != needDescription || tool.InputSchema.Properties["session_id"].Description != sessionDescription ||
		len(tool.InputSchema.Properties) != 2 || len(tool.InputSchema.Required) != 1 || tool.InputSchema.Required[0] != "need" {
		t.Errorf("get_more_tools as listed: %s", listed)
	}
	if tool.Annotations["readOnlyHint"] != true || tool.Annotations["destructiveHint"] != false || tool.Annotations["openWorldHint"] != true {
		t.Errorf("its annotations: %v", tool.Annotations)
	}
	if listed, _ := json.Marshal(missingToolsTool(false)); strings.Contains(string(listed), "session_id") {
		t.Errorf("without sessions it takes no session_id: %s", listed)
	}
	for arguments, noted := range map[string]bool{
		`{"need":"a refund tool"}`:    true,
		`{"need":""}`:                 true,
		`{"need":1,"need":"last"}`:    true,
		`{"need":"first","need":1}`:   false,
		`{"need":["a"]}`:              false,
		`{"need":null}`:               false,
		`{}`:                          false,
		``:                            false,
		`null`:                        false,
		`["need"]`:                    false,
		`{"other":{"need":"nested"}}`: false,
	} {
		reply := missingToolsReply([]byte(arguments))
		block, _ := json.Marshal(reply.Content[0])
		if noted && (reply.IsError || !strings.Contains(string(block), missingToolsAnswer)) {
			t.Errorf("%s should be noted: %s", arguments, block)
		}
		if !noted && (!reply.IsError || !strings.Contains(string(block), "need is required")) {
			t.Errorf("%s should be refused: %s", arguments, block)
		}
	}
}
