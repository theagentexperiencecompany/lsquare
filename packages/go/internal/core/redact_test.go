package core

import (
	"math/rand/v2"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
	"time"

	"github.com/theagentexperiencecompany/lsquare/packages/go/internal/contracttest"
	"github.com/theagentexperiencecompany/lsquare/packages/go/internal/jsonv"
)

func redactText(text string) string {
	return NewRedactor(ResolvePatterns(nil), nil).string(text)
}

// What each pattern's start and end conditions must get right, where a simpler translation of the
// TypeScript regex would redact less, or leave more for the next pattern to miss.
func TestPatterns(t *testing.T) {
	for _, test := range []struct{ name, in, want string }{
		{"an email", "write to alice@example.com.", "write to [EMAIL_1]."},
		{"an email straight after an escape", `line\nbob@example.com`, `line\n[EMAIL_1]`},
		{"an escape that is the local part", `\u0041%40example.com`, `\[EMAIL_1]`},
		{"an escape before the rest of the local part", `jos\u00e9bob@example.com`, `jos\u00e9[EMAIL_1]`},
		{"an escape that ends the local part", `jos\u00e9@example.com`, `jos\[EMAIL_1]`},
		{"the first %40 that leaves a domain", "ann%40example.com%40bad", "[EMAIL_1]%40bad"},
		{"no email from an escape alone", `\n@click.command`, `\n@click.command`},
		{"a phone number", "call +1 415 555 2671 now", "call [PHONE_1] now"},
		{"a US phone number", "(415) 555-2671", "[PHONE_1]"},
		{"no phone number in a longer run of digits", "id 1415-555-26711", "id 1415-555-26711"},
		{"a card", "4111 1111 1111 1111", "[CARD_1]"},
		{"a card that fails the Luhn check", "4111 1111 1111 1112", "4111 1111 1111 1112"},
		{"fifteen digits before one more after a dash", "378282246310005-7", "[CARD_1]-7"},
		{"no card in a float's fraction", "0.4111111111111111", "0.4111111111111111"},
		{"an IPv4 address", "from 192.168.1.20:8080", "from [IP_1]:8080"},
		{"an IPv6 address before a period and a letter", "2001:db8::1.x", "[IP_1].x"},
		{"no IPv6 address in two groups", "fe80::1 and ::1 and data[::10]", "fe80::1 and ::1 and data[::10]"},
		{"a key", "sk-abcdefghijklmnopqrst", "[SECRET_1]"},
		{"a key after a YAML escape", `\_sk-abcdefghijklmnopqrst`, `\_[SECRET_1]`},
		{"no key inside a word", "task-abcdefghijklmnopqrst", "task-abcdefghijklmnopqrst"},
		{"a Bearer token needs no boundary, and takes any whitespace", "preBearer abcdefghijklmno1", "pre[SECRET_1]"},
		{"prose after bearer", "the bearer responsibilities are many", "the bearer responsibilities are many"},
		{"a Basic token whose padding is its only mark", "Basic QUFBQUFBQUFBQUFB=", "[SECRET_1]"},
		{"a URL's credentials, up to the last @", `https:\/\/é:p@one@host`, `https:\/\/[SECRET_1]@host`},
		{"a private key the string's end cuts", "-----BEGIN RSA PRIVATE KEY-----\nMIIEow", "[SECRET_1]"},
		{"a private key", "a -----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY----- b", "a [SECRET_1] b"},
		{"a value under a secret key name in JSON", `{"password":"hunter2","n":1}`, `{"password":"[SECRET_1]","n":1}`},
	} {
		if got := redactText(test.in); got != test.want {
			t.Errorf("%s: %q gave %q, want %q", test.name, test.in, got, test.want)
		}
	}
}

// Every key format the secret pattern knows, each on its own and inside a sentence.
func TestEveryKeyFormatIsFound(t *testing.T) {
	for _, key := range []string{
		"lsq_" + strings.Repeat("Ab9_-", 8) + "xyz", "sk-abcdefghijklmnopqrstuvwx", "sk_live_0123456789abcdef", "sk_test_0123456789abcdef",
		"rk_live_0123456789abcdef", "rk_test_0123456789abcdef", "whsec_0123456789abcdefghijklmn",
		"ghp_0123456789abcdefghijklmnopqrstuvwxyz", "gho_0123456789abcdefghijklmnopqrstuvwxyz", "ghs_0123456789abcdefghijklmnopqrstuvwxyz",
		"ghu_0123456789abcdefghijklmnopqrstuvwxyz", "ghr_0123456789abcdefghijklmnopqrstuvwxyz", "github_pat_0123456789abcdefghij_AB",
		"glpat-0123456789abcdefghij", "npm_0123456789abcdefghijklmnopqrstuvwxyz", "xoxb-123456789012-abcdefghij", "xoxp-1234567890", "xapp-1-A0123456789-abc",
		"hooks.slack.com/services/T0AAAA/B0BBBB/" + strings.Repeat("j", 24), `hooks.slack-gov.com\/triggers\/T0000/1234567890123/abcdefgh`,
		"AKIAIOSFODNN7EXAMPLE", "ASIAIOSFODNN7EXAMPLE", "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
		`AWS_SECRET_ACCESS_KEY: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY`, "AIzaSyA-0123456789abcdefghijklmnopqrstu", "ya29.a0AfH6SMBx-0123456789abcd",
		"GOCSPX-0123456789abcdefghij", "phx_0123456789abcdefghij",
		"eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
		"Bearer abcdefghijklmno1", "bearer abcdefghijklmno1", "BEARER abcdefghijklmno1", "Basic dXNlcjpwYXNzd29yZDEyMw==", "BASIC dXNlcjpwYXNzd29yZDEyMw==",
	} {
		for _, text := range []string{key, "see " + key + " for more", "(" + key + ")"} {
			got := redactText(text)
			if !strings.Contains(got, "[SECRET_1]") || strings.Contains(got, key[len(key)-8:]) {
				t.Errorf("%q gave %q", text, got)
			}
		}
	}
}

// A data: URI's header is looked for in UTF-16 units, as in TypeScript: counted in bytes, one of
// accented letters would hide its payload.
func TestADataURIsHeaderIsCountedInUTF16Units(t *testing.T) {
	uri := "data:" + strings.Repeat("é", 510) + ";base64,U0FNUExF"
	if size, ok := binarySize(uri); !ok || size != 6 {
		t.Errorf("the URI is %d bytes (%v), want 6", size, ok)
	}
	if _, ok := binarySize("data:" + strings.Repeat("é", 1100) + ";base64,U0FNUExF"); ok {
		t.Error("a header past 1,025 units was read")
	}
	if size, _ := binarySize("data:;base64,éééé"); size != 3 {
		t.Errorf("a payload of four characters is %d bytes, want 3", size)
	}
}

// A password's 256 characters are UTF-16 units, as in TypeScript: counted in code points, the
// match would run on to the second @ and take the address's name, leaving its domain for no
// pattern to find.
func TestCredentialsAreCountedInUTF16Units(t *testing.T) {
	got := redactText("https://u:p@" + strings.Repeat("😀", 130) + "alice@example.com")
	if strings.Contains(got, "example.com") || !strings.Contains(got, "[EMAIL_1]") {
		t.Errorf("the address after the credentials isn't replaced whole: %q", got)
	}
}

// The scanner's bounded reads are UTF-16 units too. Counted in bytes, each of these would leave
// the value in the event.
func TestSecretSpansReadUTF16Units(t *testing.T) {
	wide := func(n int) string { return strings.Repeat("界", n) }
	for _, test := range []struct{ name, in string }{
		{"a key whose name ends past 256 bytes", `{"pass` + wide(90) + `word":"CANDLE"}`},
		{"a single-quoted key whose name ends past 256 bytes", `{'pass` + wide(90) + `word': 'CANDLE'}`},
		{"a quote that ends the value past 256 bytes", `{'token': 'first'` + wide(90) + `CANDLE'}`},
		{"an input_type past 1,024 bytes", `[type=x, input_value='left\n` + wide(345) + `CANDLE', input_type=str]`},
		{"escapes that decode to as many bytes as they were written in", `\L\L\L\L{\"password\":\"CANDLE\"}`},
	} {
		if got := redactText(test.in); strings.Contains(got, "CANDLE") {
			t.Errorf("%s: the value is still there: %q", test.name, got)
		}
	}
}

func TestACutSecretIsMaskedAfterItsScheme(t *testing.T) {
	// The scheme is looked for in the 64 units before the token: whitespace of three bytes each
	// must not push it out of reach.
	kept := "Bearer" + strings.Repeat("　", 20) + "abcdefghijkl"
	r := NewRedactor(ResolvePatterns(nil), nil)
	if got := r.maskAtCut(kept); strings.Contains(got, "abcdefghijkl") {
		t.Errorf("the cut token is still there: %q", got)
	}
}

// The same value gets the same number, and a placeholder already in the text keeps its own: new
// ones skip it.
func TestPlaceholdersAreNumberedPerValue(t *testing.T) {
	const text = "a@b.co, c@d.co, A@B.CO and [EMAIL_2]"
	r := NewRedactor(ResolvePatterns(nil), nil)
	r.reserve(text)
	if got, want := r.string(text), "[EMAIL_1], [EMAIL_3], [EMAIL_1] and [EMAIL_2]"; got != want {
		t.Errorf("got %q, want %q", got, want)
	}
	if got := string(jsonv.Marshal(r.Counts())); got != `{"email":3}` {
		t.Errorf("counted %s", got)
	}
}

func TestSkippedPatternsDontRun(t *testing.T) {
	r := NewRedactor(ResolvePatterns([]Pattern{IP, Email}), nil)
	if got, want := r.string("10.0.0.1 a@b.co 4111 1111 1111 1111"), "10.0.0.1 a@b.co [CARD_1]"; got != want {
		t.Errorf("got %q, want %q", got, want)
	}
	if r := NewRedactor(ResolvePatterns(Order), nil); r.Enabled() {
		t.Error("with every pattern skipped, redaction is still on")
	}
}

func TestRedactKeys(t *testing.T) {
	keys, skipped := ResolveKeys([]string{"Customer-Name", "customername", "  ", "ssn"})
	if len(keys) != 2 || keys[0] != "customername" || keys[1] != "ssn" || !skipped {
		t.Fatalf("got %v, skipped %v", keys, skipped)
	}
	r := NewRedactor(ResolvePatterns(nil), keys)
	for key, want := range map[string]bool{"customerName": true, "SSN": true, "user_ssn": true, "ssnLast4": false, "password": true, "maxTokens": false, "apiKeyId": false, "X-Api-Key": true} {
		if got := r.IsSecretKey(key); got != want {
			t.Errorf("IsSecretKey(%q) = %v, want %v", key, got, want)
		}
	}
}

// Redaction runs in the app, on strings the limits bound: none of the patterns may take more than
// linear time on text made to be slow for a backtracking engine.
func TestPatternsAreLinear(t *testing.T) {
	for _, text := range []string{
		strings.Repeat("a", 200_000) + "@",
		strings.Repeat("a.", 100_000) + "@x",
		strings.Repeat("+1 ", 70_000),
		strings.Repeat("1", 200_000),
		strings.Repeat("4111 ", 40_000),
		strings.Repeat("1:", 100_000),
		strings.Repeat("a:", 100_000),
		strings.Repeat("sk-", 70_000),
		strings.Repeat("Bearer ", 30_000),
		strings.Repeat("-----BEGIN PRIVATE KEY-----", 8_000),
		strings.Repeat("://", 70_000),
		"://" + strings.Repeat("a:", 100_000),
		strings.Repeat(`\n`, 100_000) + "@x.co",
		strings.Repeat(`"password":`, 20_000),
		strings.Repeat(`{"a":`, 40_000),
		strings.Repeat(`'`, 200_000) + ":",
		strings.Repeat(`\\`, 100_000) + `"password":"x"`,
		strings.Repeat("?token=", 30_000),
		strings.Repeat("%253D", 40_000),
	} {
		started := time.Now()
		redactText(text)
		if took := time.Since(started); took > 2*time.Second {
			t.Errorf("%d bytes starting %q took %v", len(text), text[:12], took)
		}
	}
}

// The secret key names are the contract's (REDACTION.md), as the TypeScript and Python SDKs' are.
func TestSecretKeysAreTheContracts(t *testing.T) {
	text, err := os.ReadFile(filepath.Join(contractDir(t), "REDACTION.md"))
	if err != nil {
		t.Fatal(err)
	}
	block := regexp.MustCompile(`(?s)const SECRET_KEYS = \[(.*?)\]`).FindSubmatch(text)
	if block == nil {
		t.Fatal("REDACTION.md has no SECRET_KEYS block")
	}
	var want []string
	for _, name := range regexp.MustCompile(`'([a-z0-9]+)'`).FindAllSubmatch(block[1], -1) {
		want = append(want, string(name[1]))
	}
	if strings.Join(SecretKeys, ",") != strings.Join(want, ",") {
		t.Errorf("SecretKeys is %v, REDACTION.md has %v", SecretKeys, want)
	}
}

// The patterns' bodies are the TypeScript SDK's own text, where they could be kept as they are:
// a change there must be made here too.
func TestPatternBodiesAreTypeScripts(t *testing.T) {
	source, err := os.ReadFile(filepath.Join(filepath.Dir(contractDir(t)), "lsquare", "src", "redact.ts"))
	if err != nil {
		contracttest.Missing(t, "the TypeScript SDK isn't next to the contract")
	}
	typescript := strings.ReplaceAll(strings.ReplaceAll(string(source), `\/`, `/`), "${WS}", wsClass)
	for name, body := range map[string]string{
		"phone":          phoneBodySource,
		"email":          strings.TrimPrefix(emailBody.String(), "^"),
		"IPv4":           strings.TrimPrefix(ipv4Body.String(), "^"),
		"secret keys":    strings.TrimPrefix(secretKeysBody.String(), "^"),
		"JWT":            strings.TrimPrefix(jwtBody.String(), "^"),
		"a cut's secret": strings.TrimPrefix(secretStart.String(), "^"),
		"a card number":  strings.ReplaceAll(strings.Trim(cardNumber.String(), "^$"), "[0-9]", `\d`),
	} {
		if !strings.Contains(typescript, body) {
			t.Errorf("the %s pattern isn't in redact.ts any more: %s", name, body)
		}
	}
	// IPv6's alternatives, without the capture and what stands in for the lookahead.
	ipv6 := ipv6Body.String()
	ipv6 = ipv6[strings.Index(ipv6, "(")+1 : strings.LastIndex(ipv6, ")(?:[^0-9A-Za-z:.]")]
	if !strings.Contains(typescript, "(?:"+ipv6+")(?![0-9A-Za-z:]|\\.\\d)") {
		t.Errorf("the IPv6 pattern isn't in redact.ts any more: %s", ipv6)
	}
}

// The limits are the contract's and the TypeScript SDK's: a change there must be made here too.
func TestLimitsAreTheContracts(t *testing.T) {
	contract := contractDir(t)
	limits, err := os.ReadFile(filepath.Join(contract, "src", "limits.ts"))
	if err != nil {
		t.Fatal(err)
	}
	constant := func(text []byte, name string) int {
		found := regexp.MustCompile(name + ` = ([0-9_* ]+)`).FindSubmatch(text)
		if found == nil {
			t.Fatalf("no %s", name)
		}
		value := 1
		for _, factor := range strings.Split(strings.ReplaceAll(string(found[1]), "_", ""), "*") {
			n := 0
			for _, digit := range strings.TrimSpace(factor) {
				n = n*10 + int(digit-'0')
			}
			value *= n
		}
		return value
	}
	for name, have := range map[string]int{"MAX_EVENT_BYTES": DefaultMaxEventBytes, "MAX_ID_BYTES": MaxIDBytes, "MAX_EVENTS": MaxBatchEvents, "MAX_BODY_BYTES": MaxBatchBytes, "MAX_ACK_BYTES": MaxAckBytes} {
		if want := constant(limits, name); have != want {
			t.Errorf("%s is %d here, %d in the contract", name, have, want)
		}
	}
	shape, err := os.ReadFile(filepath.Join(filepath.Dir(contract), "lsquare", "src", "shape.ts"))
	if err != nil {
		contracttest.Missing(t, "the TypeScript SDK isn't next to the contract")
	}
	for name, have := range map[string]int{"SCAN_BUDGET": ScanBudget, "MAX_ENTRIES": MaxEntries, "MAX_LEVELS": MaxLevels, "LOOKAHEAD": Lookahead} {
		if want := constant(shape, name); have != want {
			t.Errorf("%s is %d here, %d in shape.ts", name, have, want)
		}
	}
	want := "{ input: 16_000, agent: 16_000, result: 4_000, intent: 500, description: 1_000, instructions: 2_000 }"
	if !strings.Contains(string(shape), "LIMITS = "+want) || LimitInput != 16_000 || LimitAgent != 16_000 || LimitResult != 4_000 || LimitIntent != 500 || LimitDescription != 1_000 || LimitInstructions != 2_000 {
		t.Errorf("the field limits aren't shape.ts's %s", want)
	}
}

func TestNumbersAreWrittenAsJavaScriptWritesThem(t *testing.T) {
	for in, want := range map[string]string{
		"1.0": "1", "-0": "0", "-0.0": "0", "1e2": "100", "1e21": "1e+21", "1e-7": "1e-7", "0.000001": "0.000001", "123456789012345680000": "123456789012345680000",
		"9999999999999999999": "10000000000000000000", "9007199254740993": "9007199254740992", "1.5E3": "1500", "0.1": "0.1", "-12.50": "-12.5",
	} {
		value, err := jsonv.Decode([]byte(in))
		if err != nil {
			t.Fatal(err)
		}
		if got := string(jsonv.Marshal(value)); got != want {
			t.Errorf("%s is written %s, want %s", in, got, want)
		}
	}
	if value, _ := jsonv.Decode([]byte("1e999")); value != (jsonv.NotJSON{}) {
		t.Errorf("a number too large for a double is %v", value)
	}
}

// The JWT matcher is written by hand: it must match what the pattern matches, where it starts.
func TestTheJWTMatcherIsThePattern(t *testing.T) {
	random := rand.New(rand.NewPCG(7, 11))
	const alphabet = "eyJaZ09_-..."
	for i := 0; i < 200_000; i++ {
		text := []byte("eyJ")
		for n := random.IntN(60); n > 0; n-- {
			if random.IntN(8) == 0 {
				text = append(text, "abcdefghij0123456789"[:10+random.IntN(10)]...)
			} else {
				text = append(text, alphabet[random.IntN(len(alphabet))])
			}
		}
		want := -1
		if loc := jwtBody.FindIndex(text); loc != nil {
			want = loc[1]
		}
		if got := matchJWT(string(text), 0); got != want {
			t.Fatalf("%q: matched to %d, the pattern to %d", text, got, want)
		}
	}
}

// A long text of what looks like the start of a JWT, again and again, is read once.
func TestManyStartsOfAJWTAreReadOnce(t *testing.T) {
	text := strings.Repeat("eyJ.", 64_000)
	started := time.Now()
	if start, end, found := findSecret(text, 0); found {
		t.Errorf("found a secret from %d to %d", start, end)
	}
	if took := time.Since(started); took > 500*time.Millisecond {
		t.Errorf("256 KB of eyJ. took %v", took)
	}
}
