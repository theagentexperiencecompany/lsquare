package core

import (
	"crypto/sha256"
	"encoding/hex"
	"regexp"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/theagentexperiencecompany/lsquare/packages/go/internal/jsonv"
)

// SecretKeys are the secret key names. A key whose name ends with one of these, once lowercased
// and stripped of everything but letters and digits, holds a secret whatever its value looks like:
// dbPassword, client_secret, X-Api-Key and AWS_SECRET_ACCESS_KEY match; apiKeyId, maxTokens and
// author don't. Part of the secret pattern. RedactKeys adds more.
var SecretKeys = []string{
	"password", "passwords", "passwd", "passphrase", "secret", "secrets", "token", "auth", "authorization",
	"apikey", "apikeys", "privatekey", "secretkey", "accesskey", "signingkey", "encryptionkey",
	"credential", "credentials", "cookie", "cookies", "connectionstring",
}

var existingPlaceholder = regexp.MustCompile(`\[(EMAIL|PHONE|CARD|SECRET|IP)_([0-9]{1,9})\]`)

// What a secret looks like at its start, for one that runs into a cut and so can't be matched
// whole: a known key prefix or a JWT's eyJ, at the start of the token the cut ends.
var secretStart = regexp.MustCompile(`^(?:lsq_|sk-|[rs]k_(?:live|test)_|whsec_|gh[opsur]_|github_pat_|glpat-|npm_|xox[a-z]-|xapp-|hooks\.slack(?:-gov)?\.com\\{0,2}/|AKIA|ASIA|AIza|ya29\.|GOCSPX-|phx_|eyJ)`)

// A Bearer or Basic token's scheme, just before the token the cut ends.
var authScheme = regexp.MustCompile(`(?:[Bb]earer|BEARER|Basic|BASIC)[` + wsClass + `]+$`)
var pemBeginCut = regexp.MustCompile(`^-----BEGIN [A-Z ]{0,32}(?:PRIVATE KEY-----|$)`)

// A PEM key's closing line, whole: until one follows its BEGIN line, the block runs into the cut.
var pemEndAnywhere = regexp.MustCompile(`-----END [A-Z ]{0,32}PRIVATE KEY-----`)
var cardNumber = regexp.MustCompile(`^(?:[3-6][0-9]{14,15}|2(?:20[0-4]|22[1-9]|2[3-9][0-9]|[3-6][0-9][0-9]|7[01][0-9]|720)[0-9]{11,12}|3[068][0-9]{12})$`)

// Matched values longer than this are numbered by a hash, so a pending event doesn't keep them.
const maxKeptIdentity = 64

// NormalizeKey is a key name as secret key names are matched: lowercased and stripped to a-z0-9.
func NormalizeKey(key string) string {
	lowered := strings.ToLower(key)
	out := make([]byte, 0, len(lowered))
	for i := 0; i < len(lowered); i++ {
		if c := lowered[i]; (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') {
			out = append(out, c)
		}
	}
	return string(out)
}

// ResolvePatterns is the patterns that run, in order, without those skipped.
func ResolvePatterns(skip []Pattern) []Pattern {
	out := make([]Pattern, 0, len(Order))
	for _, pattern := range Order {
		skipped := false
		for _, off := range skip {
			if off == pattern {
				skipped = true
				break
			}
		}
		if !skipped {
			out = append(out, pattern)
		}
	}
	return out
}

// ResolveKeys is the extra secret key names as they are matched, each once, and whether any entry
// wasn't a key name. At most 1,000 are read.
func ResolveKeys(keys []string) (names []string, skipped bool) {
	if len(keys) > 1_000 {
		keys = keys[:1_000]
	}
	seen := map[string]bool{}
	for _, key := range keys {
		name := NormalizeKey(key)
		if name == "" {
			skipped = true
		} else if !seen[name] {
			seen[name] = true
			names = append(names, name)
		}
	}
	return names, skipped
}

// LooksLikeContact reports whether a userId looks like an email address or phone number, which
// the SDK warns about.
func LooksLikeContact(text string) bool {
	if _, _, ok := findEmail(text, 0); ok {
		return true
	}
	_, _, ok := findPhone(text, 0)
	return ok
}

// scope is how a field is finalised.
type scope struct {
	// Redact every string and key. Without it, only the strings that were cut are, and what's under
	// secret key names in JSON a string holds.
	all bool
	// Keys come from the app's data: secret key names apply.
	freeForm bool
	// Numbers that look like card numbers are redacted (tool inputs).
	cards bool
	// The field must be a string: a file becomes [binary, N bytes].
	text bool
}

func jsonField(all bool) scope     { return scope{all: all, freeForm: true, cards: true} }
func textField(all bool) scope     { return scope{all: all, text: true} }
func freeFormField(all bool) scope { return scope{all: all, freeForm: true} }

// count is how many times a pattern replaced something, in the order patterns first did.
type count struct {
	pattern Pattern
	n       int
}

// Redactor is the redaction of one event. Placeholders are numbered per event: the same value
// gets the same number everywhere, also across BeforeSend when the same Redactor finishes the event.
type Redactor struct {
	counts []count
	// What this SDK made of strings before BeforeSend, as the hook got them, each with the
	// tightest limit it keeps its form under after the hook: a cut, the limit it was cut at; a
	// string whose secret values were replaced, its length before that.
	cuts       map[string]int
	used       map[Pattern]map[int]bool
	next       map[Pattern]int
	assigned   map[string]int
	patterns   []Pattern
	secretKeys bool
	hasCard    bool
	keyNames   []string
	work       scanWork
	// What the scan made of strings before BeforeSend, which isn't read again: a placeholder could
	// be taken for part of what's around it.
	scanned map[string]bool
}

// NewRedactor returns the redaction of one event, with the patterns that run and the extra secret
// key names.
func NewRedactor(patterns []Pattern, extraKeys []string) *Redactor {
	r := &Redactor{
		cuts:     map[string]int{},
		used:     map[Pattern]map[int]bool{},
		next:     map[Pattern]int{},
		assigned: map[string]int{},
		patterns: patterns,
		work:     scanWork{left: ScanWork},
		scanned:  map[string]bool{},
	}
	// Its own list, of the patterns there are: one that isn't has no finder to run.
	r.patterns = make([]Pattern, 0, len(patterns))
	for _, pattern := range patterns {
		if finders[pattern] == nil {
			continue
		}
		r.patterns = append(r.patterns, pattern)
		if pattern == Secret {
			r.secretKeys = true
		}
		if pattern == Card {
			r.hasCard = true
		}
	}
	r.keyNames = append(append([]string(nil), SecretKeys...), extraKeys...)
	return r
}

// Enabled reports whether any pattern runs.
func (r *Redactor) Enabled() bool { return len(r.patterns) > 0 }

// Counts is the replacements made, per pattern, in the order patterns first made one.
func (r *Redactor) Counts() *jsonv.Object {
	out := jsonv.NewObject()
	for _, c := range r.counts {
		out.Set(string(c.pattern), jsonv.Int(int64(c.n)))
	}
	return out
}

// reserve records placeholders already in the event, so new ones never reuse their numbers.
func (r *Redactor) reserve(value any) {
	switch v := value.(type) {
	case string:
		r.reserveText(v)
	case *cutString:
		r.reserveText(v.window)
	case []any:
		for _, item := range v {
			r.reserve(item)
		}
	case *jsonv.Object:
		for i := 0; i < v.Len(); i++ {
			key, item := v.At(i)
			r.reserveText(key)
			r.reserve(item)
		}
	}
}

func (r *Redactor) reserveText(text string) {
	if strings.IndexByte(text, '[') < 0 {
		return
	}
	for _, match := range existingPlaceholder.FindAllStringSubmatch(text, -1) {
		if pattern, ok := patternByLabel[match[1]]; ok {
			n, _ := strconv.Atoi(match[2])
			r.usedNumbers(pattern)[n] = true
		}
	}
}

// finalize turns shaped JSON into JSON: cuts redacted and closed, secrets replaced, files and deep
// containers as markers. Secret key names apply in free-form fields and in JSON held in a string
// even before the hook.
func (r *Redactor) finalize(value any, sc scope) jsonv.Value {
	switch v := value.(type) {
	case *cutString:
		text := r.cutText(v.window, v.cut, v.scanned) + v.mark
		if limit, ok := r.cuts[text]; !ok || v.limit < limit {
			r.cuts[text] = v.limit
		}
		return text
	case *secret:
		// Couldn't be read whole: numbered on its own, never equated with another secret.
		return r.placeholder(Secret, v.identity, !v.known)
	case binary:
		if sc.text {
			return "[binary, " + strconv.FormatInt(v.bytes, 10) + " bytes]"
		}
		out := jsonv.NewObject()
		out.Set("omitted", "binary")
		out.Set("bytes", jsonv.Int(v.bytes))
		return out
	case tooDeep:
		out := jsonv.NewObject()
		out.Set("omitted", "depth")
		return out
	case string:
		if sc.all {
			return r.string(v)
		}
		return r.beforeHook(v)
	case jsonv.Num:
		if sc.all && sc.cards {
			return r.number(v)
		}
		return v
	case nil:
		return nil
	case bool:
		return v
	case []any:
		out := make([]any, len(v))
		for i, item := range v {
			out[i] = r.finalize(item, sc)
		}
		return out
	case *jsonv.Object:
		out := jsonv.NewObject()
		for i := 0; i < v.Len(); i++ {
			key, item := v.At(i)
			name := key
			if sc.all {
				name = r.text(key, false)
				// Two keys that redact to the same placeholder must not overwrite each other.
				if out.Has(name) {
					name = r.text(key, true)
				}
			}
			_, isSecret := item.(*secret)
			if sc.freeForm && r.secretKeys && r.IsSecretKey(key) && !isSecret {
				out.Set(name, r.secretValue(plain(item)))
			} else {
				out.Set(name, r.finalize(item, sc))
			}
		}
		return out
	}
	return value
}

// prune keeps only what the event still holds once it has been fitted: the cuts in it, and the
// numbers of the placeholders in it with what they stand for. A pending event then holds no more
// of what redaction matched than what it still shows, and retainedBytes counts that.
func (r *Redactor) prune(event jsonv.Value) {
	stringsSeen := map[string]bool{}
	present := map[Pattern]map[int]bool{}
	note := func(text string) {
		stringsSeen[text] = true
		if strings.IndexByte(text, '[') < 0 {
			return
		}
		for _, match := range existingPlaceholder.FindAllStringSubmatch(text, -1) {
			pattern := patternByLabel[match[1]]
			if present[pattern] == nil {
				present[pattern] = map[int]bool{}
			}
			n, _ := strconv.Atoi(match[2])
			present[pattern][n] = true
		}
	}
	var walk func(value jsonv.Value)
	walk = func(value jsonv.Value) {
		switch v := value.(type) {
		case string:
			note(v)
		case []any:
			for _, item := range v {
				walk(item)
			}
		case *jsonv.Object:
			for i := 0; i < v.Len(); i++ {
				key, item := v.At(i)
				note(key)
				walk(item)
			}
		}
	}
	walk(event)
	for text := range r.cuts {
		if !stringsSeen[text] {
			delete(r.cuts, text)
		}
	}
	for text := range r.scanned {
		if !stringsSeen[text] {
			delete(r.scanned, text)
		}
	}
	for key, n := range r.assigned {
		pattern := Pattern(key[:strings.IndexByte(key, 0)])
		if !present[pattern][n] {
			delete(r.assigned, key)
		}
	}
	r.used = present
}

// retainedBytes is about what this Redactor keeps in memory beyond the event, for the queue's limits.
func (r *Redactor) retainedBytes() int {
	bytes := 0
	for key := range r.assigned {
		bytes += len(key)*2 + 64
	}
	bytes += (len(r.cuts) + len(r.scanned)) * 64
	for _, numbers := range r.used {
		bytes += len(numbers) * 16
	}
	return bytes
}

// secretKeyMatcher is for shaping: nil when the secret pattern is off.
func (r *Redactor) secretKeyMatcher() func(string) bool {
	if !r.secretKeys {
		return nil
	}
	return r.IsSecretKey
}

// IsSecretKey reports whether a key's name ends with a secret key name.
func (r *Redactor) IsSecretKey(key string) bool {
	name := NormalizeKey(key)
	for _, suffix := range r.keyNames {
		if strings.HasSuffix(name, suffix) {
			return true
		}
	}
	return false
}

// secretValue replaces the whole value under a secret key name, whatever its shape. Values that
// can't hold a secret (null, booleans, empty values) and existing placeholders stay as they are.
func (r *Redactor) secretValue(value jsonv.Value) jsonv.Value {
	switch v := value.(type) {
	case nil:
		return nil
	case bool:
		return v
	case string:
		if v == "" || wholePlaceholderRE.MatchString(v) {
			return v
		}
		return r.placeholder(Secret, v, false)
	case []any:
		if len(v) == 0 {
			return v
		}
	case *jsonv.Object:
		if v.Len() == 0 {
			return v
		}
	}
	return r.placeholder(Secret, string(jsonv.Marshal(value)), false)
}

// string redacts a string value: what's under secret key names in JSON it holds, then the patterns.
func (r *Redactor) string(input string) string {
	if r.scanned[input] {
		return r.text(input, false)
	}
	return r.text(r.withoutSecretValues(input), false)
}

// beforeHook redacts a string value before the hook: only what's under secret key names, which
// the hook never sees.
func (r *Redactor) beforeHook(input string) string {
	out := r.withoutSecretValues(input)
	if mayHoldKey(out) {
		r.scanned[out] = true
	}
	// One ending with a marker is read as any other marked string after the hook.
	if out != input && !isMarked(out) {
		_, length := advance(input, 0, len(input))
		if before, ok := r.cuts[out]; !ok || length < before {
			r.cuts[out] = length
		}
	}
	return out
}

func (r *Redactor) withoutSecretValues(input string) string {
	out, _ := r.windowWithoutSecretValues(input, len(input))
	return out
}

// windowWithoutSecretValues replaces what's under secret key names in JSON held in window, which
// reads past the cut at cut: the rewritten window and where the cut now is. A value starting past
// the cut is neither replaced nor counted. One running past it is replaced whole, and its
// placeholder ends what's kept.
func (r *Redactor) windowWithoutSecretValues(window string, cut int) (string, int) {
	if !r.secretKeys || !mayHoldKey(window) {
		return window, cut
	}
	spans := secretSpans(window, r.IsSecretKey, &r.work)
	if len(spans) == 0 {
		return window, cut
	}
	var out strings.Builder
	from := 0
	at := cut
	for _, span := range spans {
		if span.start >= cut {
			break
		}
		placeholder := span.quote + r.placeholder(Secret, span.identity, !span.known) + span.quote
		out.WriteString(window[from:span.start])
		out.WriteString(placeholder)
		from = span.end
		if span.end > cut {
			kept := out.Len()
			out.WriteString(window[from:])
			return out.String(), kept
		}
		at += len(placeholder) - (span.end - span.start)
	}
	out.WriteString(window[from:])
	return out.String(), at
}

// skipPattern is the cheap checks that let a pattern skip text it can't match.
func skipPattern(pattern Pattern, text string) bool {
	switch pattern {
	case Email:
		return strings.IndexByte(text, '@') < 0 && !strings.Contains(text, "%40")
	case IP:
		return strings.IndexByte(text, '.') < 0 && strings.IndexByte(text, ':') < 0
	}
	return false
}

func rejected(pattern Pattern, match string) bool {
	switch pattern {
	case Card:
		return !luhn(match)
	case IP:
		return ipRejected(match)
	}
	return false
}

func digitsOf(text string) string {
	out := make([]byte, 0, len(text))
	for i := 0; i < len(text); i++ {
		if isDigit(text[i]) {
			out = append(out, text[i])
		}
	}
	return string(out)
}

func lowerASCIIString(text string) string {
	for i := 0; i < len(text); i++ {
		if c := text[i]; c >= 'A' && c <= 'Z' {
			out := []byte(text)
			for j := i; j < len(out); j++ {
				out[j] = lowerASCII(out[j])
			}
			return string(out)
		}
	}
	return text
}

// identityOf is what makes two matches "the same value" for numbering.
func identityOf(pattern Pattern, match string) string {
	switch pattern {
	case Email:
		return strings.ReplaceAll(lowerASCIIString(match), "%40", "@")
	case IP:
		return lowerASCIIString(match)
	case Card, Phone:
		return digitsOf(match)
	}
	return match
}

// text redacts text with every pattern that runs. fresh: each match gets a number of its own.
func (r *Redactor) text(input string, fresh bool) string {
	out := input
	for _, pattern := range r.patterns {
		if skipPattern(pattern, out) {
			continue
		}
		whole := out
		out = replaceMatches(whole, finders[pattern], func(match string, offset int) string {
			if rejected(pattern, match) {
				return match
			}
			if pattern == Secret && leavesURL(match, offset, whole) {
				return r.secretsIn(match, fresh)
			}
			return r.placeholder(pattern, identityOf(pattern, match), fresh)
		})
	}
	return out
}

// secretsIn handles a match that holds no credentials: only the secrets in it go.
func (r *Redactor) secretsIn(match string, fresh bool) string {
	return replaceMatches(match, findSecret, func(found string, _ int) string {
		return r.placeholder(Secret, found, fresh)
	})
}

// cutText redacts what's kept of a cut string, reading window past the cut at cut: values under
// secret key names in JSON it holds, then the patterns. A match that runs past the cut is masked
// whole: its placeholder ends what's kept. Matches entirely past the cut are neither replaced nor
// counted. Then a secret that starts like one and runs into the cut, or past the window, is
// masked too.
func (r *Redactor) cutText(window string, cut int, scanned bool) string {
	out, at := window, cut
	if !scanned {
		out, at = r.windowWithoutSecretValues(window, cut)
	}
	for _, pattern := range r.patterns {
		if skipPattern(pattern, out) {
			continue
		}
		shift := 0
		straddled := -1
		whole := out
		limit := at
		out = replaceMatches(whole, finders[pattern], func(match string, offset int) string {
			if offset >= limit || straddled >= 0 || rejected(pattern, match) {
				return match
			}
			fits := offset+len(match) <= limit
			var replaced string
			if fits && pattern == Secret && leavesURL(match, offset, whole) {
				replaced = r.secretsIn(match, false)
			} else {
				replaced = r.placeholder(pattern, identityOf(pattern, match), false)
			}
			if fits {
				shift += len(replaced) - len(match)
			} else {
				straddled = offset + shift + len(replaced)
			}
			return replaced
		})
		if straddled >= 0 {
			at = straddled
		} else {
			at += shift
		}
	}
	return r.maskAtCut(out[:at])
}

func isTokenByte(c byte) bool {
	return isAlnum(c) || c == '.' || c == '_' || c == '~' || c == '+' || c == '/' || c == '=' || c == '-'
}

// writtenEscapeLen is the length of what's after the backslash of an escape a value can start
// straight after, at the start of text, or 0.
func writtenEscapeLen(text string) int {
	if text == "" {
		return 0
	}
	if strings.IndexByte(longEscapes, text[0]) >= 0 {
		return 1
	}
	return escapeBodyLen(text, 0)
}

// lastChars is the last n characters of text. The scheme before a token is looked for in the 64
// UTF-16 units before it; 64 characters hold at least that, whatever whitespace follows the scheme.
func lastChars(text string, n int) string {
	start := len(text)
	for ; n > 0 && start > 0; n-- {
		_, size := utf8.DecodeLastRuneInString(text[:start])
		start -= size
	}
	return text[start:]
}

// maskAtCut masks a secret whose end was cut off: its kept part becomes the placeholder.
func (r *Redactor) maskAtCut(kept string) string {
	if !r.secretKeys {
		return kept
	}
	if begin := strings.LastIndex(kept, pemStart); begin != -1 {
		if pemBeginCut.MatchString(window(kept, begin, 64)) && !pemEndAnywhere.MatchString(kept[begin:]) {
			return kept[:begin] + r.placeholder(Secret, kept[begin:], false)
		}
	}
	run := len(kept)
	for run > 0 && isTokenByte(kept[run-1]) {
		run--
	}
	// What ends an escape written in JSON, such as the n of \n, isn't part of the token.
	if run > 0 && kept[run-1] == '\\' {
		run += writtenEscapeLen(window(kept, run, 9))
	}
	// The earliest place in the run where a secret could start: its beginning, or just after a
	// character that ends one in the patterns, as = does in token=eyJ….
	for start := run; start < len(kept); start++ {
		if start > run {
			if c := kept[start-1]; c != '.' && c != '~' && c != '+' && c != '/' && c != '=' {
				continue
			}
		}
		token := kept[start:]
		scheme := false
		if start == run {
			scheme = authScheme.MatchString(lastChars(kept[:start], 64))
		}
		// The longest of the starts is under 32 characters.
		if scheme || secretStart.MatchString(window(token, 0, 32)) {
			return kept[:start] + r.placeholder(Secret, token, false)
		}
	}
	return kept
}

// number redacts card numbers stored as JSON numbers inside tool inputs.
func (r *Redactor) number(value jsonv.Num) jsonv.Value {
	if !r.hasCard {
		return value
	}
	digits := string(value)
	if !cardNumber.MatchString(digits) || !luhn(digits) {
		return value
	}
	return r.placeholder(Card, digits, false)
}

func (r *Redactor) placeholder(pattern Pattern, id string, fresh bool) string {
	identity := id
	if len(id) > maxKeptIdentity {
		// Of its bytes as they are: two values never share a hash for what JSON would write the same.
		sum := sha256.Sum256([]byte(id))
		identity = "#" + hex.EncodeToString(sum[:])
	}
	key := string(pattern) + "\x00" + identity
	n, ok := 0, false
	if !fresh {
		n, ok = r.assigned[key]
	}
	if !ok {
		n = r.nextFree(pattern)
		if !fresh {
			r.assigned[key] = n
		}
	}
	counted := false
	for i := range r.counts {
		if r.counts[i].pattern == pattern {
			r.counts[i].n++
			counted = true
			break
		}
	}
	if !counted {
		r.counts = append(r.counts, count{pattern: pattern, n: 1})
	}
	return "[" + labels[pattern] + "_" + strconv.Itoa(n) + "]"
}

// nextFree is a running counter per pattern, skipping numbers already in the event: linear overall.
func (r *Redactor) nextFree(pattern Pattern) int {
	used := r.usedNumbers(pattern)
	n := r.next[pattern]
	if n == 0 {
		n = 1
	}
	for used[n] {
		n++
	}
	used[n] = true
	r.next[pattern] = n + 1
	return n
}

func (r *Redactor) usedNumbers(pattern Pattern) map[int]bool {
	set := r.used[pattern]
	if set == nil {
		set = map[int]bool{}
		r.used[pattern] = set
	}
	return set
}

// isWrittenAs reports whether form at `at` is the whole of how a quote is written there: o””brien
// and a shell's '\” hold neither ' nor ”.
func isWrittenAs(text string, at int, form string) bool {
	quote := form[len(form)-1]
	if at > 0 {
		if before := text[at-1]; before == '\\' || before == quote {
			return false
		}
	}
	if after := at + len(form); after < len(text) {
		if c := text[after]; c == '\\' || c == quote {
			return false
		}
	}
	return true
}

func isSchemeByte(c byte) bool {
	return isAlnum(c) || c == '+' || c == '.' || c == '-'
}

// leavesURL reports whether a URL's credentials match ran out of its URL, and so holds none:
// host:443','ok','ops in ['https://host:443','ok','ops@...']. It takes all of this: the URL is
// straight after a ' or a backtick, alone, doubled or after a backslash, as an SQL or JavaScript
// string inside another writes it. That quote closes before any @ and with no word character
// after it, as o'brien has, and it opens again before the match ends, written the same way each
// time. Anything else stays masked whole. Linear: it reads the match and at most 42 characters
// before it, and matches don't overlap.
func leavesURL(match string, offset int, whole string) bool {
	if offset+len(match) >= len(whole) || whole[offset+len(match)] != '@' {
		return false
	}
	slashes := ""
	for _, written := range urlSlashes {
		if offset >= len(written) && strings.HasSuffix(whole[:offset], written) {
			slashes = written
			break
		}
	}
	if slashes == "" {
		return false
	}
	colon := offset - len(slashes)
	scheme := colon
	for scheme > 0 && colon-scheme < 32 && isSchemeByte(whole[scheme-1]) {
		scheme--
	}
	if scheme == colon || scheme == 0 {
		return false
	}
	quote := whole[scheme-1]
	if quote != '\'' && quote != '`' {
		return false
	}
	form := string(quote)
	if scheme >= 2 {
		if previous := whole[scheme-2]; previous == quote || previous == '\\' {
			form = string(previous) + string(quote)
		}
	}
	if !isWrittenAs(whole, scheme-len(form), form) {
		return false
	}
	closedBy := func(at int) bool {
		if !isWrittenAs(match, at, form) {
			return false
		}
		after := at + len(form)
		return after >= len(match) || !isWordByte(match[after])
	}
	closes := strings.Index(match, form)
	for closes != -1 && !closedBy(closes) {
		next := strings.Index(match[closes+1:], form)
		if next == -1 {
			closes = -1
		} else {
			closes += 1 + next
		}
	}
	if closes == -1 || strings.IndexByte(match[:closes], '@') >= 0 {
		return false
	}
	for opens := closes + len(form); opens <= len(match); {
		next := strings.Index(match[opens:], form)
		if next == -1 {
			break
		}
		opens += next
		if isWrittenAs(match, opens, form) {
			return true
		}
		opens++
	}
	return false
}
