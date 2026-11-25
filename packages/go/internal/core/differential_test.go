package core

// Beyond the shared cases: generated inputs, each built by the TypeScript SDK (through Node, from
// packages/lsquare/src) and by this one. What Go's event shows, TypeScript's must show too. The
// inputs are made of what redaction looks for (addresses, numbers, keys, JSON and reprs holding
// secret key names, URLs' parameters) joined by what can come between them: escapes written in
// strings, quotes, non-ASCII text.
//
// Skipped where the TypeScript SDK isn't next to the contract (a copy of a released contract
// alone), or without Node.

import (
	"bytes"
	"fmt"
	"math/rand"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"unicode/utf8"

	"github.com/theagentexperiencecompany/lsquare/packages/go/internal/contracttest"
	"github.com/theagentexperiencecompany/lsquare/packages/go/internal/jsonv"
)

var differentialPieces = []string{
	// What the patterns match.
	"alice@example.com", "Bob.Smith+tag@mail.example.org", "ann%40example.com", "jos\\u00e9@example.com",
	"+1 415 555 2671", "+44 20 7946 0958", "(415) 555-2671", "415-555-2671", "+33123456789",
	"4111 1111 1111 1111", "4111111111111111", "378282246310005", "3056 9309 0259 04", "5555-5555-5555-4444", "4111111111111112",
	"192.168.1.20", "10.0.0.1", "2001:db8::1", "fe80::1", "::1", "2001:0db8:85a3:0000:0000:8a2e:0370:7334", "1.2.3", "12:30:45",
	"sk-abcdefghijklmnopqrstuvwx", "lsq_" + strings.Repeat("Ab9_-", 8) + "xyz", "ghp_0123456789abcdefghijklmnopqrstuvwxyz",
	"sk_live_0123456789abcdef", "whsec_0123456789abcdefghijklmn", "xoxb-123456789012-abcdefghij", "AKIAIOSFODNN7EXAMPLE",
	"AIzaSyA-0123456789abcdefghijklmnopqrstu", "ya29.a0AfH6SMBx-0123456789abcd", "npm_0123456789abcdefghijklmnopqrstuvwxyz",
	"github_pat_0123456789abcdefghij_AB", "gho_0123456789abcdefghijklmnopqrstuvwxyz", "glpat-0123456789abcdefghij", "rk_test_0123456789abcdef",
	"xapp-1-A0123456789-abc", "ASIAIOSFODNN7EXAMPLE", "GOCSPX-0123456789abcdefghij_", "phx_0123456789abcdefghij", "hooks.slack-gov.com\\/triggers\\/T0000/1234567890123/abcdefgh",
	"AWS_SECRET_ACCESS_KEY: \"wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY\"", "data:" + strings.Repeat("é", 510) + ";base64,U0FNUExF", "data:image/png;base64,iVBORw0KGgo=",
	"eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
	"Bearer abcdefghijklmno1", "bearer the responsibilities", "Basic QUFBQUFBQUFBQUFB=", "BASIC dXNlcjpwYXNzd29yZDEyMw==",
	"https://user:hunter2@db.example.com/x", "postgres://admin:s3cret@10.0.0.5:5432/app", "https:\\/\\/u:p@host", "https://host:443/path",
	"-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----", "-----BEGIN PRIVATE KEY-----\nMIIEvQ",
	"aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "hooks.slack.com/services/T0AAAA/B0BBBB/" + strings.Repeat("j", 24),
	// Values under secret key names, as text holds them.
	`{"password":"hunter2"}`, `{"token": "abc", "note": "ok"}`, `{"api_key":12345,"n":1}`, `{"secret":{"a":1},"b":2}`, `{"auth":["x","y"]}`,
	`{'password': 'x', 'note': "it's"}`, `{'token': token}`, `Map { 'password' => 'x' }`, `"password": correct horse battery`,
	`{\"password\":\"hunter2\"}`, `{\\\"token\\\":\\\"abc\\\"}`, `"{\"cookie\":\"a=b\"}"`, `{"password":"hun"ter2","x":1}`,
	`[type=string_type, input_value='hunter2', input_type=str]`, `[type=x, input_value={'password': 'p'}, input_type=dict]`,
	"?token=abc123&x=1", "#access_token=abc.def", "https://h/cb?code=4/0Ab&state=x", "&api_key=K%3D1", "?sig=abcdef&ok=1", "?password=%7Bp%7D",
	"https://h/r?next=https%3A%2F%2Fa%2F%3Ftoken%3Dabc", "?token={token}", "?token=$TOKEN",
	`password: "x"`, `"token" : "v"`, `case "token": return x`, `x == "token": y`, `{"apiKeyId":"k1"}`, `{"maxTokens":5}`,
	"[EMAIL_1]", "[SECRET_3]", `{"password":"[SECRET_2]"}`,
	// What comes between.
	" ", " ", "\n", "\t", ", ", ". ", ": ", "; ", "=", "/", "-", "_", "(", ")", "[", "]", "{", "}", "<", ">", "'", "\"", "`", "\\", "\\n", "\\t", "\\\\", "\\\"",
	"\\u00e9", "\\x41", "\\U0001f511", "\\_", "é", "界", "😀", "\u00a0", "\u3000", "ß", "İ", "K",
	"the", "user", "said", "order", "42", "3.14", "0", "id", "name", "true", "null", "x", "abc", "ABC", "token", "password", "key",
	// What an encoder writes its own way: control characters, line separators, what HTML escapes.
	"\u2028", "\u2029", "\u0001", "\u001f", "\u007f", "\ufeff", "\b", "\f", "\r", "&", "\u00ad",
	// What a bounded read counted in bytes would get wrong: long runs of characters of several bytes.
	strings.Repeat("界", 90), strings.Repeat("😀", 130), strings.Repeat("\u3000", 22), `{"pass`, `word":"CANDLE"}`, `{'token': 'first'`, `CANDLE'}`,
	`[type=x, input_value='left\n`, `', input_type=str]`, `\L\L\L\L{\"password\":\"CANDLE\"}`, "https://u:p@", "Bearer", "@one@host", "%40bad", "-7", ".x",
}

var differentialKeys = []string{
	"q", "note", "id", "password", "dbPassword", "api_key", "X-Api-Key", "token", "maxTokens", "apiKeyId", "author", "auth", "cookie",
	"name", "user@example.com", "pass界word", "TOKEN", "to ken", "", "sig", "code", "data", "2", "10", "0", "007", "4294967295",
}

func differentialText(r *rand.Rand) string {
	var out strings.Builder
	for n := 1 + r.Intn(9); n > 0; n-- {
		out.WriteString(differentialPieces[r.Intn(len(differentialPieces))])
	}
	return out.String()
}

// differentialListed gives some calls what is the same on every call of a tool or a server: the
// tool's description and the server's instructions, some long enough to be cut. And what the
// extras add to a call: why it was made, some long enough to be cut too, and its session.
func differentialListed(r *rand.Rand, call *jsonv.Object) {
	for _, key := range []string{"intent", "toolDescription", "serverInstructions"} {
		switch r.Intn(6) {
		case 0:
			call.Set(key, differentialText(r))
		case 1:
			text := differentialText(r)
			call.Set(key, strings.Repeat(differentialText(r)+" ", 1+2500/(1+len(text)))+text)
		}
	}
	if r.Intn(4) == 0 {
		call.Set("sessionId", "ses_"+strings.Repeat("Ab9_-", 4)+"xy")
		call.Set("sessionSource", []string{"sent", "issued", "protocol"}[r.Intn(3)])
	}
	if r.Intn(9) == 0 {
		call.Set("extra", "missing_tools")
	}
}

func differentialValue(r *rand.Rand, depth int) jsonv.Value {
	switch pick := r.Intn(12); {
	case pick < 6 || depth >= 3:
		return differentialText(r)
	case pick == 6:
		return jsonv.Int(int64(r.Intn(100000)))
	case pick == 7:
		return []jsonv.Value{jsonv.Num("4111111111111111"), jsonv.Num("378282246310005"), jsonv.Num("1234567890123456"), true, nil}[r.Intn(5)]
	case pick == 8 || pick == 9:
		list := make([]any, r.Intn(4))
		for i := range list {
			list[i] = differentialValue(r, depth+1)
		}
		return list
	default:
		object := jsonv.NewObject()
		for n := r.Intn(5); n > 0; n-- {
			object.Set(differentialKeys[r.Intn(len(differentialKeys))], differentialValue(r, depth+1))
		}
		return object
	}
}

// differentialCases are generated cases, in the shared cases' form.
func differentialCases(seed int64, count int) []*jsonv.Object {
	r := rand.New(rand.NewSource(seed))
	cases := make([]*jsonv.Object, count)
	for i := range cases {
		c := jsonv.NewObject()
		c.Set("name", fmt.Sprintf("generated %d", i))
		switch r.Intn(4) {
		case 0:
			turn := jsonv.NewObject()
			turn.Set("question", differentialText(r))
			if r.Intn(3) == 0 {
				turn.Set("answer", differentialText(r))
			}
			if r.Intn(4) == 0 {
				turn.Set("error", differentialText(r))
			}
			c.Set("turn", turn)
		case 1:
			call := jsonv.NewObject()
			input := jsonv.NewObject()
			for n := 1 + r.Intn(4); n > 0; n-- {
				input.Set(differentialKeys[r.Intn(len(differentialKeys))], differentialValue(r, 0))
			}
			call.Set("input", input)
			differentialListed(r, call)
			c.Set("call", call)
		default:
			call := jsonv.NewObject()
			result := jsonv.NewObject()
			block := jsonv.NewObject()
			block.Set("type", "text")
			text := differentialText(r)
			// Some long enough to be cut, so the window past the cut is read too.
			if r.Intn(6) == 0 {
				text = strings.Repeat(differentialText(r)+" ", 1+4000/(1+len(text))) + text
			}
			block.Set("text", text)
			result.Set("content", []any{block})
			if r.Intn(4) == 0 {
				result.Set("structuredContent", differentialValue(r, 0))
			}
			call.Set("result", result)
			if r.Intn(5) == 0 {
				failure := jsonv.NewObject()
				message := differentialText(r)
				// Some long enough to be cut, on a call whose intent may be too.
				if r.Intn(3) == 0 {
					message = strings.Repeat(message+" ", 1+4500/(1+len(message)))
				}
				failure.Set("message", message)
				failure.Set("category", "tool_error")
				call.Set("error", failure)
				call.Set("status", "error")
			}
			differentialListed(r, call)
			c.Set("call", call)
		}
		if r.Intn(5) == 0 {
			c.Set("hook", jsonv.NewObject())
		}
		cases[i] = c
	}
	return cases
}

// The TypeScript SDK's own event for each case, built as its cases.test.ts builds one.
const typescriptScript = `
import { readFileSync } from 'node:fs'
const { afterHook, buildCall, buildTurn } = await import(process.argv[1])
const { resolveKeys, resolvePatterns } = await import(process.argv[2])
const cases = JSON.parse(readFileSync(0, 'utf8'))
const out = cases.map((test) => {
  const context = { sdk: { name: 'lsquare', version: '0.2.0', language: 'js' }, patterns: resolvePatterns(undefined), redactKeys: resolveKeys(undefined), maxEventBytes: 256 * 1024 }
  const hook = test.hook !== undefined
  const withHook = (built) => (hook && built.ok ? afterHook(structuredClone(built.draft.event), built.draft, context) : built)
  let built
  if (test.turn) built = withHook(buildTurn(test.turn, '2026-10-28T00:00:00.000Z', context, hook))
  else {
    const { input, result, ...rest } = test.call
    const record = { callId: 'case-call', capturedAt: '2026-10-28T00:00:00.000Z', toolName: 'search_orders', status: 'completed', durationMs: 1, ids: {}, diagnostics: false, ...rest,
      ...(input === undefined ? {} : { input: { value: input } }), ...(result === undefined ? {} : { result: { value: result } }) }
    built = withHook(buildCall(record, context, hook))
  }
  if (!built.ok) return { error: built.code }
  // The event's JSON as TypeScript itself writes it, with what differs between two SDKs' events
  // for the same call taken out here, in TypeScript: the SDK's own name, and a turn's random ID.
  const sent = { ...built.draft.event, sdk: { name: 'lsquare' } }
  if (typeof sent.eventId === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(sent.eventId)) sent.eventId = '$uuid'
  return { event: built.draft.event, json: JSON.stringify(sent) }
})
process.stdout.write(JSON.stringify(out))
`

// typescript is what the TypeScript SDK made of a case: its event, and the event's JSON as
// TypeScript wrote it (see sentJSON); neither when it dropped the case.
type typescript struct {
	event *jsonv.Object
	json  string
}

func typescriptEvents(t *testing.T, cases []*jsonv.Object) []typescript {
	t.Helper()
	contract := contractDir(t)
	repo := filepath.Dir(filepath.Dir(contract))
	source := filepath.Join(filepath.Dir(contract), "lsquare", "src")
	if _, err := os.Stat(filepath.Join(source, "event.ts")); err != nil {
		contracttest.Missing(t, "the TypeScript SDK isn't next to the contract: nothing to compare with")
	}
	if _, err := exec.LookPath("node"); err != nil {
		contracttest.Missing(t, "node isn't installed: nothing to compare with")
	}
	list := make([]any, len(cases))
	for i, c := range cases {
		list[i] = c
	}
	cmd := exec.Command("node", "--import", "tsx", "--input-type=module", "-e", typescriptScript,
		"file://"+filepath.ToSlash(filepath.Join(source, "event.ts")), "file://"+filepath.ToSlash(filepath.Join(source, "redact.ts")))
	cmd.Dir = repo
	cmd.Stdin = bytes.NewReader(jsonv.Marshal(list))
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil {
		t.Fatalf("the TypeScript SDK failed: %v\n%s", err, stderr.String())
	}
	decoded, err := jsonv.Decode(out)
	if err != nil {
		t.Fatal(err)
	}
	results := decoded.([]any)
	if len(results) != len(cases) {
		t.Fatalf("%d answers for %d cases", len(results), len(cases))
	}
	events := make([]typescript, len(results))
	for i, result := range results {
		events[i].event, _ = asObject(field(result.(*jsonv.Object), "event"))
		events[i].json, _ = field(result.(*jsonv.Object), "json").(string)
	}
	return events
}

// sentJSON is an event's JSON as this package writes it, with what differs between two SDKs'
// events for the same call taken out: the SDK's own name, and a turn's random ID.
func sentJSON(event *jsonv.Object) string {
	out := jsonv.Clone(event).(*jsonv.Object)
	sdk := jsonv.NewObject()
	sdk.Set("name", "lsquare")
	out.Set("sdk", sdk)
	if id, _ := field(out, "eventId").(string); uuidRE.MatchString(id) {
		out.Set("eventId", "$uuid")
	}
	return string(jsonv.Marshal(out))
}

// Every generated event is TypeScript's, byte for byte, its fields in the same order. Should one
// differ, the test says whether it shows more than TypeScript's, which is the difference that
// matters most.
func TestGeneratedEventsAreTypeScripts(t *testing.T) {
	count, seed := 4000, int64(28)
	if testing.Short() {
		count = 500
	}
	// For a longer hunt: LSQUARE_DIFFERENTIAL=<count>,<seed>.
	if hunt := os.Getenv("LSQUARE_DIFFERENTIAL"); hunt != "" {
		if _, err := fmt.Sscanf(hunt, "%d,%d", &count, &seed); err != nil {
			t.Fatalf("LSQUARE_DIFFERENTIAL must be <count>,<seed>: %v", err)
		}
	}
	cases := differentialCases(seed, count)
	expected := typescriptEvents(t, cases)
	equal := 0
	for i, generated := range cases {
		// As a call's arguments reach the SDK: decoded from JSON.
		decoded, err := jsonv.Decode(jsonv.Marshal(generated))
		if err != nil {
			t.Fatal(err)
		}
		object := decoded.(*jsonv.Object)
		c := &sharedCase{file: "generated", name: field(object, "name").(string), event: expected[i].event}
		if turn, ok := object.Get("turn"); ok {
			c.turn, c.isTurn = turn, true
		}
		c.call, _ = asObject(field(object, "call"))
		c.hook, c.hasHook = asObject(field(object, "hook"))
		draft, failed := buildCase(t, c)
		input := excerpt(string(jsonv.Marshal(object)))
		if expected[i].event == nil {
			if failed == nil {
				t.Errorf("%s: TypeScript drops it, Go doesn't\n  input %s", c.name, input)
			}
			continue
		}
		if failed != nil {
			t.Errorf("%s: dropped as %s\n  input %s", c.name, failed.Code, input)
			continue
		}
		// This package's writing of its event against TypeScript's writing of its own: the bytes
		// each would send.
		got, want := sentJSON(draft.Event), expected[i].json
		if got == want {
			equal++
			continue
		}
		var problems []string
		leaks("event", comparable(c, draft.Event, new([]string)), comparable(c, expected[i].event, new([]string)), &problems)
		verdict := "it shows no more than TypeScript's"
		if len(problems) > 0 {
			verdict = "it shows more than TypeScript's:\n  " + strings.Join(problems, "\n  ")
		}
		t.Errorf("%s: the event isn't TypeScript's; %s\n  input    %s\n  %s", c.name, verdict, input, firstDifference(got, want))
	}
	t.Logf("%d of %d generated events are TypeScript's, byte for byte", equal, len(cases))
}

// firstDifference shows two texts from a little before where they first differ.
func firstDifference(got, want string) string {
	at := 0
	for at < len(got) && at < len(want) && got[at] == want[at] {
		at++
	}
	from := max(at-60, 0)
	for from > 0 && !utf8.RuneStart(got[from]) {
		from--
	}
	return fmt.Sprintf("from byte %d:\n  got      %s\n  expected %s", from, excerpt(got[from:]), excerpt(want[from:]))
}
