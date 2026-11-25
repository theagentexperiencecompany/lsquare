package core

import (
	"regexp"
	"strings"
	"unicode/utf8"
)

// The built-in redaction patterns. They are the TypeScript SDK's (packages/lsquare/src/redact.ts),
// written for Go's regexp, which has no lookbehind or lookahead and always matches in linear time.
// Each TypeScript pattern is one regex of alternatives tried in order at every position. Here
// each alternative is a start condition checked in code (a word boundary, "straight after an
// escape written in a string", "after ://"), then a body anchored at that position: a regex, or a
// matcher written by hand where the body looks ahead. The scan walks the text left to right, and
// at each position the first alternative that matches wins, as in the regex. Go's regexp picks
// the match a backtracking engine would, so an anchored body ends where TypeScript's does.

// Pattern names a built-in redaction pattern.
type Pattern string

const (
	Email  Pattern = "email"
	Phone  Pattern = "phone"
	Card   Pattern = "card"
	Secret Pattern = "secret"
	IP     Pattern = "ip"
)

// Order is the order patterns run in: secrets first, so a token's digits are never read as a card
// or phone number.
var Order = []Pattern{Secret, Email, Card, Phone, IP}

var labels = map[Pattern]string{Email: "EMAIL", Phone: "PHONE", Card: "CARD", Secret: "SECRET", IP: "IP"}
var patternByLabel = map[string]Pattern{"EMAIL": Email, "PHONE": Phone, "CARD": Card, "SECRET": Secret, "IP": IP}

// JavaScript's \s, written out: the same characters in every SDK.
const wsClass = `\t\n\v\f\r \x{00a0}\x{1680}\x{2000}-\x{200a}\x{2028}\x{2029}\x{202f}\x{205f}\x{3000}\x{feff}`

func isWS(r rune) bool {
	switch r {
	case '\t', '\n', '\v', '\f', '\r', ' ', 0x00a0, 0x1680, 0x2028, 0x2029, 0x202f, 0x205f, 0x3000, 0xfeff:
		return true
	}
	return r >= 0x2000 && r <= 0x200a
}

// skipWS returns the index after the whitespace at from.
func skipWS(text string, from int) int {
	index := from
	for index < len(text) {
		if c := text[index]; c < utf8.RuneSelf {
			if !isWS(rune(c)) {
				break
			}
			index++
			continue
		}
		r, size := utf8.DecodeRuneInString(text[index:])
		if !isWS(r) {
			break
		}
		index += size
	}
	return index
}

func isDigit(c byte) bool { return c >= '0' && c <= '9' }
func isLetter(c byte) bool {
	return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z')
}
func isAlnum(c byte) bool    { return isDigit(c) || isLetter(c) }
func isWordByte(c byte) bool { return isAlnum(c) || c == '_' }
func isHexByte(c byte) bool {
	return isDigit(c) || (c >= 'A' && c <= 'F') || (c >= 'a' && c <= 'f')
}

func allHex(text string) bool {
	for i := 0; i < len(text); i++ {
		if !isHexByte(text[i]) {
			return false
		}
	}
	return true
}

// The single letters of an escape a value can start straight after: JSON's and a repr's, other
// languages' printers', and YAML's. An email takes only the first set.
const (
	shortEscapes = "nrtfb"
	longEscapes  = "nrtfb0aevNLP_"
)

// afterEscape reports whether an escape written in a string ends at p: a backslash and one of
// letters, or \uXXXX, \xXX or \UXXXXXXXX. What ends the escape isn't taken for part of a value.
func afterEscape(text string, p int, letters string) bool {
	if p >= 2 && text[p-2] == '\\' && strings.IndexByte(letters, text[p-1]) >= 0 {
		return true
	}
	if p >= 6 && text[p-6] == '\\' && text[p-5] == 'u' && allHex(text[p-4:p]) {
		return true
	}
	if p >= 4 && text[p-4] == '\\' && text[p-3] == 'x' && allHex(text[p-2:p]) {
		return true
	}
	return p >= 10 && text[p-10] == '\\' && text[p-9] == 'U' && allHex(text[p-8:p])
}

// wordStart is \b before a word character at p, or the end of a written escape.
func wordStart(text string, p int) bool {
	return p == 0 || !isWordByte(text[p-1]) || afterEscape(text, p, longEscapes)
}

// window is text from p, at most n bytes: enough for a body of bounded length and what follows it.
func window(text string, p, n int) string {
	if len(text)-p > n {
		return text[p : p+n]
	}
	return text[p:]
}

// A finder returns the first match at or after from.
type finder func(text string, from int) (start, end int, ok bool)

var finders = map[Pattern]finder{Email: findEmail, Phone: findPhone, Card: findCard, Secret: findSecret, IP: findIP}

// replaceMatches replaces each match with what fn returns for it, scanning on from the match's
// end, as String.replace with a global regex does.
func replaceMatches(text string, find finder, fn func(match string, offset int) string) string {
	var out strings.Builder
	last := 0
	changed := false
	for from := 0; from <= len(text); {
		start, end, ok := find(text, from)
		if !ok {
			break
		}
		match := text[start:end]
		replaced := fn(match, start)
		if replaced != match {
			if !changed {
				out.Grow(len(text))
				changed = true
			}
			out.WriteString(text[last:start])
			out.WriteString(replaced)
			last = end
		}
		from = end
	}
	if !changed {
		return text
	}
	out.WriteString(text[last:])
	return out.String()
}

// Email.

func isLocalByte(c byte) bool {
	return isAlnum(c) || c == '.' || c == '_' || c == '%' || c == '+' || c == '-'
}

var emailBody = regexp.MustCompile(`^[A-Za-z0-9._%+-]+(?:@|%40)[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}`)

// escapeBodyLen is the length of what follows a backslash in \uXXXX, \xXX or \UXXXXXXXX at p, or 0.
func escapeBodyLen(text string, p int) int {
	if p >= len(text) {
		return 0
	}
	n := 0
	switch text[p] {
	case 'u':
		n = 4
	case 'x':
		n = 2
	case 'U':
		n = 8
	default:
		return 0
	}
	if p+1+n > len(text) || !allHex(text[p+1:p+1+n]) {
		return 0
	}
	return 1 + n
}

// emailStart reports whether an address may start at p, where a local-part character is. Either
// nothing of a local part is before it, unless it is what ends an escape (the n of \n, or the
// u00e9 of é with more of the address after it); or an escape ends just before it. A \u, \x
// or \U escape straight before @ or %40 reads as the address, since one can end that way.
func emailStart(text string, p int) bool {
	if afterEscape(text, p, shortEscapes) {
		return true
	}
	if p > 0 && isLocalByte(text[p-1]) {
		return false
	}
	if p == 0 || text[p-1] != '\\' {
		return true
	}
	if strings.IndexByte(shortEscapes, text[p]) >= 0 {
		return false
	}
	if n := escapeBodyLen(text, p); n > 0 {
		after := p + n
		if after < len(text) && isLocalByte(text[after]) && !strings.HasPrefix(text[after:], "%40") {
			return false
		}
	}
	return true
}

func findEmail(text string, from int) (int, int, bool) {
	for p := from; p < len(text); p++ {
		if !isLocalByte(text[p]) || !emailStart(text, p) {
			continue
		}
		// The run of local-part characters from here: an address needs an @ after it or %40 in it.
		end := p + 1
		for end < len(text) && isLocalByte(text[end]) {
			end++
		}
		at := end < len(text) && text[end] == '@'
		if !at && !strings.Contains(text[p:end], "%40") {
			continue
		}
		// The domain and its top level are at most 279 characters past the run.
		if loc := emailBody.FindStringIndex(window(text, p, end-p+280)); loc != nil {
			return p, p + loc[1], true
		}
	}
	return 0, 0, false
}

// Phone.

// The body of an international number after its +, as the TypeScript SDK builds it
// (scripts/phone-pattern.mjs): every way to write the groups, none reading a run of digits twice.
const phoneBodySource = `(?:\d{1,3}(?:[ .-]\(?|\()(?:\d{1,4}(?:\)[ .-]?|[ .-])(?:\d{2,4}[ .-](?:\d{2,4}(?:[ .-](?:\d{2,4}(?:[ .-]\d{2,4}\b|\b)|\d{4,8}\b)|\b)|\d{4,8}(?:[ .-]\d{2,4}\b|\b)|\d{6,12}\b)|\d{4,8}(?:[ .-](?:\d{2,4}(?:[ .-]\d{2,4}\b|\b)|\d{4,8}\b)|\b)|\d{6,12}(?:[ .-]\d{2,4}\b|\b)|\d{8,16}\b)|\d{3,8}[ .-](?:\d{2,4}(?:[ .-](?:\d{2,4}(?:[ .-]\d{2,4}\b|\b)|\d{4,8}\b)|\b)|\d{4,8}(?:[ .-]\d{2,4}\b|\b)|\d{6,12}\b)|\d{5,12}(?:[ .-](?:\d{2,4}(?:[ .-]\d{2,4}\b|\b)|\d{4,8}\b)|\b)|\d{7,16}(?:[ .-]\d{2,4}\b|\b)|\d{9,20}\b)|\d{2,7}(?:\)[ .-]?|[ .-])(?:\d{2,4}[ .-](?:\d{2,4}(?:[ .-](?:\d{2,4}(?:[ .-]\d{2,4}\b|\b)|\d{4,8}\b)|\b)|\d{4,8}(?:[ .-]\d{2,4}\b|\b)|\d{6,12}\b)|\d{4,8}(?:[ .-](?:\d{2,4}(?:[ .-]\d{2,4}\b|\b)|\d{4,8}\b)|\b)|\d{6,12}(?:[ .-]\d{2,4}\b|\b)|\d{8,16}\b)|\d{4,11}[ .-](?:\d{2,4}(?:[ .-](?:\d{2,4}(?:[ .-]\d{2,4}\b|\b)|\d{4,8}\b)|\b)|\d{4,8}(?:[ .-]\d{2,4}\b|\b)|\d{6,12}\b)|\d{6,15}(?:[ .-](?:\d{2,4}(?:[ .-]\d{2,4}\b|\b)|\d{4,8}\b)|\b)|\d{8,19}(?:[ .-]\d{2,4}\b|\b)|\d{10,23}\b)`

var (
	phoneLookahead = regexp.MustCompile(`^[1-9](?:[ .()-]{0,2}\d){5}`)
	phoneBody      = regexp.MustCompile(`^` + phoneBodySource)
	phoneParens    = regexp.MustCompile(`^\(\d{3}\) ?\d{3}-\d{4}\b`)
	phoneDashes    = regexp.MustCompile(`^\d{3}-\d{3}-\d{4}\b`)
)

func findPhone(text string, from int) (int, int, bool) {
	for p := from; p < len(text); p++ {
		switch c := text[p]; {
		case c == '+':
			rest := window(text, p+1, 128)
			if !phoneLookahead.MatchString(rest) {
				continue
			}
			if loc := phoneBody.FindStringIndex(rest); loc != nil {
				return p, p + 1 + loc[1], true
			}
		case c == '(':
			if loc := phoneParens.FindStringIndex(window(text, p, 16)); loc != nil {
				return p, p + loc[1], true
			}
		case isDigit(c):
			if !wordStart(text, p) {
				continue
			}
			if loc := phoneDashes.FindStringIndex(window(text, p, 14)); loc != nil {
				return p, p + loc[1], true
			}
		}
	}
	return 0, 0, false
}

// Card.

func isCardSeparator(c byte) bool { return c == ' ' || c == '-' }

// cardGroups reads (?:[ -]?\d) up to max times from p. It returns where each repeat ended.
func cardGroups(text string, p, max int) []int {
	ends := make([]int, 0, max)
	for len(ends) < max {
		q := p
		if q < len(text) && isCardSeparator(text[q]) {
			q++
		}
		if q >= len(text) || !isDigit(text[q]) {
			break
		}
		p = q + 1
		ends = append(ends, p)
	}
	return ends
}

func wordEnd(text string, e int) bool { return e >= len(text) || !isWordByte(text[e]) }

// matchCard is the body of the card pattern at p: 15 or 16 digits of a known range, not ending in
// one or two digits after a space or a dash (as an expiry does), or Diners' 14.
func matchCard(text string, p int) int {
	if p+4 > len(text) {
		return -1
	}
	head := text[p : p+4]
	for i := 0; i < 4; i++ {
		if !isDigit(head[i]) {
			return -1
		}
	}
	long := head[0] >= '3' && head[0] <= '6'
	if head[0] == '2' {
		n := int(head[1]-'0')*100 + int(head[2]-'0')*10 + int(head[3]-'0')
		long = (n >= 200 && n <= 204) || (n >= 221 && n <= 720)
	}
	if long {
		ends := cardGroups(text, p+4, 12)
		for count := len(ends); count >= 11; count-- {
			e := ends[count-1]
			if !wordEnd(text, e) {
				continue
			}
			// The last digit is at e-1. Not after a separator, alone or with one digit before it.
			if isCardSeparator(text[e-2]) {
				continue
			}
			if isCardSeparator(text[e-3]) && isDigit(text[e-2]) {
				continue
			}
			return e
		}
	}
	if head[0] == '3' && (head[1] == '0' || head[1] == '6' || head[1] == '8') {
		ends := cardGroups(text, p+4, 10)
		if len(ends) == 10 && wordEnd(text, ends[9]) {
			return ends[9]
		}
	}
	return -1
}

func findCard(text string, from int) (int, int, bool) {
	for p := from; p < len(text); p++ {
		if c := text[p]; c < '2' || c > '6' {
			continue
		}
		// Not straight after a digit and a period, as a float's fraction is.
		if p >= 2 && text[p-1] == '.' && isDigit(text[p-2]) {
			continue
		}
		if !wordStart(text, p) {
			continue
		}
		if e := matchCard(text, p); e >= 0 {
			return p, e, true
		}
	}
	return 0, 0, false
}

// luhn reports whether the digits in text pass the Luhn check.
func luhn(text string) bool {
	sum, count := 0, 0
	for i := len(text) - 1; i >= 0; i-- {
		if !isDigit(text[i]) {
			continue
		}
		digit := int(text[i] - '0')
		if count%2 == 1 {
			digit *= 2
			if digit > 9 {
				digit -= 9
			}
		}
		sum += digit
		count++
	}
	return count > 0 && sum%10 == 0
}

// IP.

var (
	ipv4Body = regexp.MustCompile(`^(?:25[0-5]|2[0-4]\d|1\d\d|\d\d?)(?:\.(?:25[0-5]|2[0-4]\d|1\d\d|\d\d?)){3}\b`)
	// What must not follow an IPv6 address ((?![0-9A-Za-z:]|\.\d) in TypeScript) is read here as
	// what may: the end, another character, or a period that no digit follows.
	ipv6Body = regexp.MustCompile(`^((?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}|(?:[0-9A-Fa-f]{1,4}:){1,7}:|(?:[0-9A-Fa-f]{1,4}:){1,6}:[0-9A-Fa-f]{1,4}|(?:[0-9A-Fa-f]{1,4}:){1,5}(?::[0-9A-Fa-f]{1,4}){1,2}|(?:[0-9A-Fa-f]{1,4}:){1,4}(?::[0-9A-Fa-f]{1,4}){1,3}|(?:[0-9A-Fa-f]{1,4}:){1,3}(?::[0-9A-Fa-f]{1,4}){1,4}|(?:[0-9A-Fa-f]{1,4}:){1,2}(?::[0-9A-Fa-f]{1,4}){1,5}|[0-9A-Fa-f]{1,4}:(?::[0-9A-Fa-f]{1,4}){1,6}|:(?::[0-9A-Fa-f]{1,4}){1,7})(?:[^0-9A-Za-z:.]|\.(?:[^0-9]|\z)|\z)`)
)

// ipv6Start reports whether an IPv6 address may start at p: not inside a word, an address or a
// number, or at the end of a written escape.
func ipv6Start(text string, p int) bool {
	if p == 0 {
		return true
	}
	if c := text[p-1]; !isAlnum(c) && c != ':' && c != '.' {
		return true
	}
	return afterEscape(text, p, longEscapes)
}

func findIP(text string, from int) (int, int, bool) {
	for p := from; p < len(text); p++ {
		c := text[p]
		if isDigit(c) && wordStart(text, p) {
			// The longest IPv4 address is 15 characters; one more is read for its end.
			if loc := ipv4Body.FindStringIndex(window(text, p, 16)); loc != nil {
				return p, p + loc[1], true
			}
		}
		if (isHexByte(c) || c == ':') && ipv6Start(text, p) && ipv6Ahead(text, p) {
			// The longest IPv6 address is 39 characters; two more are read for what follows it.
			if loc := ipv6Body.FindStringSubmatchIndex(window(text, p, 48)); loc != nil {
				return p, p + loc[3], true
			}
		}
	}
	return 0, 0, false
}

// ipv6Ahead is a cheap check before the pattern runs: a group of at most four hex digits and a
// colon, or two colons.
func ipv6Ahead(text string, p int) bool {
	q := p
	for q < len(text) && q-p < 4 && isHexByte(text[q]) {
		q++
	}
	if q >= len(text) || text[q] != ':' {
		return false
	}
	return q > p || (q+1 < len(text) && text[q+1] == ':')
}

// ipRejected reports whether an IPv6 match has fewer than three groups: ::1, fe80::1 and code
// such as data[::10] or A::B stay.
func ipRejected(match string) bool {
	if !strings.Contains(match, ":") {
		return false
	}
	groups := 0
	for _, part := range strings.Split(match, ":") {
		if part != "" {
			groups++
		}
	}
	return groups < 3
}

// Secret.

var (
	// Known key formats, each after a word boundary or a written escape.
	secretKeysBody = regexp.MustCompile(`^(?:lsq_[A-Za-z0-9_-]{40,}|sk-[A-Za-z0-9_-]{20,}|[rs]k_(?:live|test)_[A-Za-z0-9]{16,}|whsec_[A-Za-z0-9]{24,}|gh[opsur]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{20,}|npm_[A-Za-z0-9]{36}|xox[a-z]-[A-Za-z0-9%+/=-]{10,}|xapp-[A-Za-z0-9-]{10,}|hooks\.slack(?:-gov)?\.com\\{0,2}/(?:services|triggers|workflows|commands|actions)\\{0,2}/[A-Za-z0-9/\\_-]{20,}|(?:AKIA|ASIA)[0-9A-Z]{16}|(?:aws_secret_access_key|AWS_SECRET_ACCESS_KEY)(?:\\{0,7}["'])?[` + wsClass + `]*[:=][` + wsClass + `]*(?:\\{0,7}["'])?(?:[A-Za-z0-9/+]|\\{1,2}/){40}|AIza[0-9A-Za-z_-]{35}|ya29\.[A-Za-z0-9._-]{20,}|GOCSPX-[A-Za-z0-9_-]{20,}|phx_[A-Za-z0-9]{20,})`)
	jwtBody        = regexp.MustCompile(`^eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}`)
	pemBegin       = regexp.MustCompile(`^-----BEGIN [A-Z ]{0,32}PRIVATE KEY-----`)
	pemEnd         = regexp.MustCompile(`^-----END [A-Z ]{0,32}PRIVATE KEY-----`)
	// A closing line the string's end cut off.
	pemCutEnd = regexp.MustCompile(`^-----[A-Z ]{0,48}-{0,4}$`)
)

// How ://  is written in text, in JSON and in JSON in JSON.
var urlSlashes = []string{"://", `:\/\/`, `:\\/\\/`}

// afterURLSlashes reports whether a URL's authority starts at p.
func afterURLSlashes(text string, p int) bool {
	if p == 0 || text[p-1] != '/' {
		return false
	}
	for _, written := range urlSlashes {
		if strings.HasSuffix(text[:p], written) {
			return true
		}
	}
	return false
}

// utf16Len is how many UTF-16 units a character takes: the patterns' lengths are counted in them.
func utf16Len(r rune) int {
	if r >= 0x10000 {
		return 2
	}
	return 1
}

// matchCredentials is a URL's user and password at p, up to the last @ they can end at: a user
// of at most 256 characters without whitespace, /, :, ?, # or ", a colon, and a password of 1 to
// 256 without whitespace, /, ?, # or ". The @ isn't part of the match.
func matchCredentials(text string, p int) int {
	index, units := p, 0
	for {
		if index >= len(text) {
			return -1
		}
		r, size := utf8.DecodeRuneInString(text[index:])
		if r == ':' {
			break
		}
		if isWS(r) || r == '/' || r == '?' || r == '#' || r == '"' {
			return -1
		}
		units += utf16Len(r)
		if units > 256 {
			return -1
		}
		index += size
	}
	index++
	units = 0
	last := -1
	for index < len(text) {
		r, size := utf8.DecodeRuneInString(text[index:])
		if isWS(r) || r == '/' || r == '?' || r == '#' || r == '"' {
			break
		}
		if r == '@' && units >= 1 {
			last = index
		}
		units += utf16Len(r)
		if units > 256 {
			break
		}
		index += size
	}
	return last
}

func hasAnyPrefix(text string, prefixes ...string) int {
	for _, prefix := range prefixes {
		if strings.HasPrefix(text, prefix) {
			return len(prefix)
		}
	}
	return 0
}

func isBearerByte(c byte) bool {
	return isAlnum(c) || c == '.' || c == '_' || c == '~' || c == '+' || c == '/' || c == '-'
}

func isBase64Byte(c byte) bool { return isAlnum(c) || c == '+' || c == '/' }

// matchBearer is a Bearer token at p: the scheme, whitespace, and 16 or more token characters with
// a digit among the first 513, so prose such as "the bearer responsibilities" is left alone.
func matchBearer(text string, p int) int {
	n := hasAnyPrefix(text[p:], "Bearer", "bearer", "BEARER")
	if n == 0 {
		return -1
	}
	start := skipWS(text, p+n)
	if start == p+n {
		return -1
	}
	end := start
	digit := false
	for end < len(text) && isBearerByte(text[end]) {
		if end-start <= 512 && isDigit(text[end]) {
			digit = true
		}
		end++
	}
	if end-start < 16 || !digit {
		return -1
	}
	for end < len(text) && text[end] == '=' {
		end++
	}
	return end
}

// matchBasic is a Basic token at p: the scheme, whitespace, and 16 or more base64 characters with
// a digit, + or = among the first 513.
func matchBasic(text string, p int) int {
	n := hasAnyPrefix(text[p:], "Basic", "BASIC")
	if n == 0 {
		return -1
	}
	start := skipWS(text, p+n)
	if start == p+n {
		return -1
	}
	end := start
	marked := false
	for end < len(text) && isBase64Byte(text[end]) {
		if c := text[end]; end-start <= 512 && (isDigit(c) || c == '+') {
			marked = true
		}
		end++
	}
	if end-start < 16 {
		return -1
	}
	if !marked && !(end-start <= 512 && end < len(text) && text[end] == '=') {
		return -1
	}
	for padding := 0; padding < 2 && end < len(text) && text[end] == '='; padding++ {
		end++
	}
	return end
}

// matchPEM is a private key block at p: its BEGIN line, a body up to the next five dashes, and
// its END line, or the string's end where that cuts the block, even inside its closing line.
func matchPEM(text string, p int) int {
	loc := pemBegin.FindStringIndex(window(text, p, 64))
	if loc == nil {
		return -1
	}
	body := p + loc[1]
	dashes := strings.Index(text[body:], "-----")
	if dashes < 0 {
		return len(text)
	}
	at := body + dashes
	if end := pemEnd.FindStringIndex(window(text, at, 64)); end != nil {
		return at + end[1]
	}
	if len(text)-at <= 64 && pemCutEnd.MatchString(text[at:]) {
		return len(text)
	}
	return -1
}

// secretKeyPrefix reports whether one of the known key formats can start with what is at p.
func secretKeyPrefix(text string, p int) bool {
	rest := text[p:]
	switch rest[0] {
	case 'l':
		return strings.HasPrefix(rest, "lsq_")
	case 's':
		return strings.HasPrefix(rest, "sk-") || strings.HasPrefix(rest, "sk_")
	case 'r':
		return strings.HasPrefix(rest, "rk_")
	case 'w':
		return strings.HasPrefix(rest, "whsec_")
	case 'g':
		return strings.HasPrefix(rest, "gh") || strings.HasPrefix(rest, "github_pat_") || strings.HasPrefix(rest, "glpat-")
	case 'n':
		return strings.HasPrefix(rest, "npm_")
	case 'x':
		return strings.HasPrefix(rest, "xox") || strings.HasPrefix(rest, "xapp-")
	case 'h':
		return strings.HasPrefix(rest, "hooks.slack")
	case 'A':
		return strings.HasPrefix(rest, "AKIA") || strings.HasPrefix(rest, "ASIA") || strings.HasPrefix(rest, "AIza") || strings.HasPrefix(rest, "AWS_SECRET_ACCESS_KEY")
	case 'a':
		return strings.HasPrefix(rest, "aws_secret_access_key")
	case 'y':
		return strings.HasPrefix(rest, "ya29.")
	case 'G':
		return strings.HasPrefix(rest, "GOCSPX-")
	case 'p':
		return strings.HasPrefix(rest, "phx_")
	}
	return false
}

func isJWTByte(c byte) bool { return isAlnum(c) || c == '_' || c == '-' }

// matchJWT is jwtBody at p, where the text starts with eyJ: three runs of a JWT's characters, of
// at least 10 each after eyJ, with a dot between them. It is read by hand, each run once, and
// given up at the first run that is too short: the pattern would read the rest of a long dotted
// text again at every eyJ in it.
func matchJWT(text string, p int) int {
	end := p + len("eyJ")
	for part := 0; part < 3; part++ {
		start := end
		for end < len(text) && isJWTByte(text[end]) {
			end++
		}
		if end-start < 10 {
			return -1
		}
		if part == 2 {
			return end
		}
		if end == len(text) || text[end] != '.' {
			return -1
		}
		end++
	}
	return -1
}

// matchSecret is the first of the secret pattern's alternatives to match at p, in its order: a
// URL's credentials, a known key format, a JWT, a Bearer token, a Basic token, a private key.
func matchSecret(text string, p int) int {
	if afterURLSlashes(text, p) {
		if end := matchCredentials(text, p); end > p {
			return end
		}
	}
	switch c := text[p]; c {
	case 'e':
		if strings.HasPrefix(text[p:], "eyJ") && (p == 0 || !isJWTByte(text[p-1]) || afterEscape(text, p, longEscapes)) {
			if end := matchJWT(text, p); end >= 0 {
				return end
			}
		}
	case 'B', 'b':
		if end := matchBearer(text, p); end >= 0 {
			return end
		}
		if c == 'B' && wordStart(text, p) {
			return matchBasic(text, p)
		}
	case '-':
		if strings.HasPrefix(text[p:], "-----BEGIN ") {
			return matchPEM(text, p)
		}
	default:
		if secretKeyPrefix(text, p) && wordStart(text, p) {
			if loc := secretKeysBody.FindStringIndex(text[p:]); loc != nil {
				return p + loc[1]
			}
		}
	}
	return -1
}

func findSecret(text string, from int) (int, int, bool) {
	for p := from; p < len(text); p++ {
		if end := matchSecret(text, p); end > p {
			return p, end, true
		}
	}
	return 0, 0, false
}

// findPEMBlock is the first private key block at or after from.
func findPEMBlock(text string, from int) (int, int, bool) {
	for p := from; p < len(text); {
		at := strings.Index(text[p:], "-----BEGIN ")
		if at < 0 {
			break
		}
		p += at
		if end := matchPEM(text, p); end > p {
			return p, end, true
		}
		p++
	}
	return 0, 0, false
}
