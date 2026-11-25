//! The five redaction patterns, matching what the TypeScript SDK's regular expressions match
//! (`packages/lsquare/src/redact.ts`, and `packages/contract/REDACTION.md`).
//!
//! Rust's `regex` has no lookaround, by design: every search is linear in the text. So each
//! alternative of a pattern is split in three. Where it may start (a lookbehind in TypeScript) is
//! checked here, against the bytes before the position. Its body is an anchored regex run from
//! that position. What must follow it (a lookahead) is either checked here or written into the
//! body as text it reads and gives back. Positions are tried left to right and, at each, the
//! alternatives in the pattern's order, so the match found is the one JavaScript's finds.
//!
//! Each pattern starts at most a few times per run of the characters it is made of, so the work
//! stays linear in the text, as TypeScript's does.

use std::sync::LazyLock;

use regex::Regex;

/// A built-in redaction pattern, as an event's `redactions` names them. Each can be turned off
/// with `Options::redact_pattern`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Pattern {
    /// Known key formats, credentials in URLs, private keys, and what's under secret key names.
    Secret,
    /// Email addresses.
    Email,
    /// Card numbers that pass the Luhn check.
    Card,
    /// Phone numbers in international or US format.
    Phone,
    /// IPv4 and IPv6 addresses.
    Ip,
}

/// Secrets first, so a token's digits are never read as a card or phone number.
pub(crate) const ORDER: [Pattern; 5] = [
    Pattern::Secret,
    Pattern::Email,
    Pattern::Card,
    Pattern::Phone,
    Pattern::Ip,
];

impl Pattern {
    /// Its name in the `redact` option and in an event's `redactions`.
    pub(crate) fn name(self) -> &'static str {
        match self {
            Pattern::Secret => "secret",
            Pattern::Email => "email",
            Pattern::Card => "card",
            Pattern::Phone => "phone",
            Pattern::Ip => "ip",
        }
    }

    /// Its name in a placeholder.
    pub(crate) fn label(self) -> &'static str {
        match self {
            Pattern::Secret => "SECRET",
            Pattern::Email => "EMAIL",
            Pattern::Card => "CARD",
            Pattern::Phone => "PHONE",
            Pattern::Ip => "IP",
        }
    }

    pub(crate) fn from_label(label: &str) -> Option<Pattern> {
        ORDER.into_iter().find(|pattern| pattern.label() == label)
    }
}

/// JavaScript's `\s`, written out: Rust's own is a different set.
const WS: &str = r"\t\n\x0B\x0C\r \u{a0}\u{1680}\u{2000}-\u{200a}\u{2028}\u{2029}\u{202f}\u{205f}\u{3000}\u{feff}";

pub(crate) fn is_js_space(ch: char) -> bool {
    matches!(
        ch,
        '\t' | '\n' | '\u{b}' | '\u{c}' | '\r' | ' ' | '\u{a0}' | '\u{1680}' | '\u{2000}'
            ..='\u{200a}'
                | '\u{2028}'
                | '\u{2029}'
                | '\u{202f}'
                | '\u{205f}'
                | '\u{3000}'
                | '\u{feff}'
    )
}

fn regex(pattern: &str) -> Regex {
    Regex::new(pattern).expect("a redaction pattern")
}

static EMAIL: LazyLock<Regex> =
    LazyLock::new(|| regex(r"\A[A-Za-z0-9._%+-]+(?:@|%40)[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}"));
/// What must follow the `+` of an international number.
static PHONE_AHEAD: LazyLock<Regex> = LazyLock::new(|| regex(r"\A[1-9](?:[ .()-]{0,2}[0-9]){5}"));
/// 0.1's international number after its `+`, as `scripts/phone-pattern.mjs` of the TypeScript SDK builds it.
static PHONE_INTERNATIONAL: LazyLock<Regex> =
    LazyLock::new(|| regex(&format!(r"\A{}", include_str!("phone_intl.regex").trim())));
static PHONE_PARENTHESES: LazyLock<Regex> =
    LazyLock::new(|| regex(r"\A\([0-9]{3}\) ?[0-9]{3}-[0-9]{4}(?-u:\b)"));
static PHONE_DASHES: LazyLock<Regex> =
    LazyLock::new(|| regex(r"\A[0-9]{3}-[0-9]{3}-[0-9]{4}(?-u:\b)"));
static CARD_PREFIX: LazyLock<Regex> = LazyLock::new(|| {
    regex(r"\A(?:[3-6][0-9]{3}|2(?:20[0-4]|22[1-9]|2[3-9][0-9]|[3-6][0-9][0-9]|7[01][0-9]|720))")
});
static SECRET_KEYS: LazyLock<Regex> = LazyLock::new(|| {
    regex(&format!(
        r#"\A(?:lsq_[A-Za-z0-9_-]{{40,}}|sk-[A-Za-z0-9_-]{{20,}}|[rs]k_(?:live|test)_[A-Za-z0-9]{{16,}}|whsec_[A-Za-z0-9]{{24,}}|gh[opsur]_[A-Za-z0-9]{{36,}}|github_pat_[A-Za-z0-9_]{{20,}}|glpat-[A-Za-z0-9_-]{{20,}}|npm_[A-Za-z0-9]{{36}}|xox[a-z]-[A-Za-z0-9%+/=-]{{10,}}|xapp-[A-Za-z0-9-]{{10,}}|hooks\.slack(?:-gov)?\.com\\{{0,2}}/(?:services|triggers|workflows|commands|actions)\\{{0,2}}/[A-Za-z0-9/\\_-]{{20,}}|(?:AKIA|ASIA)[0-9A-Z]{{16}}|(?:aws_secret_access_key|AWS_SECRET_ACCESS_KEY)(?:\\{{0,7}}["'])?[{WS}]*[:=][{WS}]*(?:\\{{0,7}}["'])?(?:[A-Za-z0-9/+]|\\{{1,2}}/){{40}}|AIza[0-9A-Za-z_-]{{35}}|ya29\.[A-Za-z0-9._-]{{20,}}|GOCSPX-[A-Za-z0-9_-]{{20,}}|phx_[A-Za-z0-9]{{20,}})"#
    ))
});
static JWT: LazyLock<Regex> =
    LazyLock::new(|| regex(r"\AeyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"));
static BEARER: LazyLock<Regex> =
    LazyLock::new(|| regex(&format!(r"\A(?:[Bb]earer|BEARER)[{WS}]+")));
static BASIC: LazyLock<Regex> = LazyLock::new(|| regex(&format!(r"\A(?:Basic|BASIC)[{WS}]+")));
static PEM_BEGIN: LazyLock<Regex> =
    LazyLock::new(|| regex(r"\A-----BEGIN [A-Z ]{0,32}PRIVATE KEY-----"));
static PEM_END: LazyLock<Regex> =
    LazyLock::new(|| regex(r"\A-----END [A-Z ]{0,32}PRIVATE KEY-----"));
/// A block the string's end cuts, even inside its END line.
static PEM_CUT: LazyLock<Regex> = LazyLock::new(|| regex(r"\A-----[A-Z ]{0,48}-{0,4}\z"));
static IPV4: LazyLock<Regex> = LazyLock::new(|| {
    let octet = "(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[0-9][0-9]?)";
    regex(&format!(r"\A{octet}(?:\.{octet}){{3}}(?-u:\b)"))
});
/// An IPv6 address, and what may follow it: not a letter, a digit or a colon, nor a period and a
/// digit. That is read as part of the match, so that a shorter way of matching is tried when it
/// fails, as a lookahead is in JavaScript, and given back: the address is the first group.
static IPV6: LazyLock<Regex> = LazyLock::new(|| {
    let h = "[0-9A-Fa-f]{1,4}";
    regex(&format!(
        r"\A((?:{h}:){{7}}{h}|(?:{h}:){{1,7}}:|(?:{h}:){{1,6}}:{h}|(?:{h}:){{1,5}}(?::{h}){{1,2}}|(?:{h}:){{1,4}}(?::{h}){{1,3}}|(?:{h}:){{1,3}}(?::{h}){{1,4}}|(?:{h}:){{1,2}}(?::{h}){{1,5}}|{h}:(?::{h}){{1,6}}|:(?::{h}){{1,7}})(?:\z|[^0-9A-Za-z:.]|\.(?:\z|[^0-9]))"
    ))
});

/// The escapes a value can start straight after, besides those that take digits.
const ESCAPE_LETTERS: &[u8] = b"nrtfb0aevNLP_";
/// Those an email address can start straight after.
const EMAIL_ESCAPE_LETTERS: &[u8] = b"nrtfb";

fn is_word(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || byte == b'_'
}

fn is_hex(bytes: &[u8]) -> bool {
    bytes.iter().all(u8::is_ascii_hexdigit)
}

/// `\b` before a word character at `at`: nothing, or something that isn't one, comes before it.
fn boundary_before(bytes: &[u8], at: usize) -> bool {
    at == 0 || !is_word(bytes[at - 1])
}

/// `\b` after a word character that ends at `at`.
fn boundary_after(bytes: &[u8], at: usize) -> bool {
    at >= bytes.len() || !is_word(bytes[at])
}

/// Whether `at` comes straight after an escape written in a string: a backslash and one of
/// `letters`, or `\uXXXX`, `\xXX` or `\UXXXXXXXX`.
fn after_escape(bytes: &[u8], at: usize, letters: &[u8]) -> bool {
    let written = |length: usize, letter: u8| {
        at >= length
            && bytes[at - length] == b'\\'
            && bytes[at - length + 1] == letter
            && is_hex(&bytes[at - length + 2..at])
    };
    (at >= 2 && bytes[at - 2] == b'\\' && letters.contains(&bytes[at - 1]))
        || written(6, b'u')
        || written(4, b'x')
        || written(10, b'U')
}

/// Where a token (a key, a number, an address) may start: at a word boundary, or after a written escape.
fn token_start(bytes: &[u8], at: usize) -> bool {
    boundary_before(bytes, at) || after_escape(bytes, at, ESCAPE_LETTERS)
}

/// The length in bytes of the character at `at`.
fn char_len(bytes: &[u8], at: usize) -> usize {
    match bytes[at] {
        byte if byte < 0x80 => 1,
        byte if byte >= 0xf0 => 4,
        byte if byte >= 0xe0 => 3,
        _ => 2,
    }
}

/// Every match of `pattern` in `text`, left to right and not overlapping, as byte ranges: what
/// JavaScript's `text.matchAll(pattern)` gives.
pub(crate) fn find_all(pattern: Pattern, text: &str) -> Vec<(usize, usize)> {
    let at: fn(&str, usize) -> Option<usize> = match pattern {
        Pattern::Secret => secret_at,
        Pattern::Email => email_at,
        Pattern::Card => card_at,
        Pattern::Phone => phone_at,
        Pattern::Ip => ip_at,
    };
    let bytes = text.as_bytes();
    let mut found = Vec::new();
    let mut position = 0;
    while position < bytes.len() {
        match at(text, position) {
            // A match is never empty.
            Some(end) if end > position => {
                found.push((position, end));
                position = end;
            }
            _ => position += char_len(bytes, position),
        }
    }
    found
}

/// Cheap checks that let a pattern skip text it can't match.
pub(crate) fn skip(pattern: Pattern, text: &str) -> bool {
    match pattern {
        Pattern::Email => !text.contains('@') && !text.contains("%40"),
        Pattern::Ip => !text.contains('.') && !text.contains(':'),
        _ => false,
    }
}

/// A match the pattern's shape allows but its rule doesn't.
pub(crate) fn rejected(pattern: Pattern, found: &str) -> bool {
    match pattern {
        Pattern::Card => !luhn(found),
        // IPv6 needs three groups: `::1`, `fe80::1` and code such as `data[::10]` or `A::B` stay.
        Pattern::Ip => {
            found.contains(':') && found.split(':').filter(|group| !group.is_empty()).count() < 3
        }
        _ => false,
    }
}

pub(crate) fn luhn(text: &str) -> bool {
    let digits: Vec<u32> = text
        .bytes()
        .filter(u8::is_ascii_digit)
        .map(|digit| u32::from(digit - b'0'))
        .collect();
    let mut sum = 0;
    for (index, digit) in digits.iter().rev().enumerate() {
        let mut digit = *digit;
        if index % 2 == 1 {
            digit *= 2;
            if digit > 9 {
                digit -= 9;
            }
        }
        sum += digit;
    }
    !digits.is_empty() && sum % 10 == 0
}

/// What makes two matches "the same value" for numbering.
pub(crate) fn identity(pattern: Pattern, found: &str) -> String {
    match pattern {
        Pattern::Email => found.to_lowercase().replace("%40", "@"),
        Pattern::Ip => found.to_lowercase(),
        Pattern::Card | Pattern::Phone => found.chars().filter(char::is_ascii_digit).collect(),
        Pattern::Secret => found.to_owned(),
    }
}

// Email.

fn is_local(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'%' | b'+' | b'-')
}

/// After a backslash, what makes this an escape and not an address: `\n` and its like, or a
/// `\u`, `\x` or `\U` escape with more of the address after it. Straight before `@` or `%40` such
/// an escape reads as the address, since one can end that way.
fn email_escape(bytes: &[u8], at: usize) -> bool {
    let more = |digits: usize| {
        let after = at + 1 + digits;
        after < bytes.len()
            && is_hex(&bytes[at + 1..after])
            && !bytes[after..].starts_with(b"%40")
            && is_local(bytes[after])
    };
    match bytes[at] {
        b'n' | b'r' | b't' | b'f' | b'b' => true,
        b'u' => more(4),
        b'x' => more(2),
        b'U' => more(8),
        _ => false,
    }
}

fn email_at(text: &str, at: usize) -> Option<usize> {
    let bytes = text.as_bytes();
    if !is_local(bytes[at]) {
        return None;
    }
    let after_local = at > 0 && is_local(bytes[at - 1]);
    let start = !(after_local || at > 0 && bytes[at - 1] == b'\\' && email_escape(bytes, at))
        || after_escape(bytes, at, EMAIL_ESCAPE_LETTERS);
    if !start {
        return None;
    }
    EMAIL.find(&text[at..]).map(|found| at + found.end())
}

// Phone.

fn phone_at(text: &str, at: usize) -> Option<usize> {
    let bytes = text.as_bytes();
    match bytes[at] {
        b'+' => {
            if !PHONE_AHEAD.is_match(&text[at + 1..]) {
                return None;
            }
            PHONE_INTERNATIONAL
                .find(&text[at + 1..])
                .map(|found| at + 1 + found.end())
        }
        b'(' => PHONE_PARENTHESES
            .find(&text[at..])
            .map(|found| at + found.end()),
        byte if byte.is_ascii_digit() && token_start(bytes, at) => {
            PHONE_DASHES.find(&text[at..]).map(|found| at + found.end())
        }
        _ => None,
    }
}

// Card.

/// `count` digits from `at`, each after at most one space or `-`: where they end.
fn card_digits(bytes: &[u8], at: usize, count: usize) -> Option<usize> {
    let mut index = at;
    for _ in 0..count {
        if index < bytes.len() && matches!(bytes[index], b' ' | b'-') {
            index += 1;
        }
        if index >= bytes.len() || !bytes[index].is_ascii_digit() {
            return None;
        }
        index += 1;
    }
    Some(index)
}

fn card_at(text: &str, at: usize) -> Option<usize> {
    let bytes = text.as_bytes();
    if !bytes[at].is_ascii_digit() {
        return None;
    }
    // Not straight after a digit and a period, as a float's fraction is.
    if at >= 2 && bytes[at - 1] == b'.' && bytes[at - 2].is_ascii_digit() {
        return None;
    }
    if !token_start(bytes, at) {
        return None;
    }
    if let Some(prefix) = CARD_PREFIX.find(&text[at..]) {
        let from = at + prefix.end();
        // Twelve more digits, then eleven: one or two digits after a space or `-`, as in an
        // expiry, aren't read as the end of 15 or 16.
        for count in [12, 11] {
            let Some(end) = card_digits(bytes, from, count) else {
                continue;
            };
            let separator = |index: usize| matches!(bytes[index], b' ' | b'-');
            let expiry =
                separator(end - 2) || (separator(end - 3) && bytes[end - 2].is_ascii_digit());
            if boundary_after(bytes, end) && !expiry {
                return Some(end);
            }
        }
    }
    // Diners' 14 digits, which can end in a group of two.
    if bytes[at] == b'3'
        && at + 3 < bytes.len()
        && matches!(bytes[at + 1], b'0' | b'6' | b'8')
        && bytes[at + 2].is_ascii_digit()
        && bytes[at + 3].is_ascii_digit()
    {
        let end = card_digits(bytes, at + 4, 10)?;
        if boundary_after(bytes, end) {
            return Some(end);
        }
    }
    None
}

// Secret.

/// `://` as it's written in text, in JSON and in JSON in JSON.
pub(crate) const URL_SLASHES: [&str; 3] = ["://", ":\\/\\/", ":\\\\/\\\\/"];

/// What a URL's user and password can't hold, besides a colon in the user.
fn ends_credentials(ch: char) -> bool {
    is_js_space(ch) || matches!(ch, '/' | '?' | '#' | '"')
}

/// A URL's credentials from `at`, straight after its slashes: a user of up to 256 characters, a
/// colon, and a password that runs to the last `@` within 256 characters. The lengths are counted
/// in UTF-16 code units, as JavaScript counts them: a longer reach could take a match past the
/// `@` TypeScript stops at, which decides whether it is credentials at all (see `leaves_url`).
fn url_credentials(text: &str, at: usize) -> Option<usize> {
    let mut units = 0;
    let mut colon = None;
    for (index, ch) in text[at..].char_indices() {
        if ch == ':' {
            colon = Some(at + index);
            break;
        }
        if ends_credentials(ch) {
            return None;
        }
        units += ch.len_utf16();
        if units > 256 {
            return None;
        }
    }
    let password = colon? + 1;
    let mut units = 0;
    let mut end = None;
    for (index, ch) in text[password..].char_indices() {
        if units > 256 || ends_credentials(ch) {
            break;
        }
        if ch == '@' && units >= 1 {
            end = Some(password + index);
        }
        units += ch.len_utf16();
    }
    end
}

/// The first bytes the known key formats start with.
const KEY_STARTS: &[u8] = b"lsrwgnxhAayGp";

fn bearer_at(text: &str, at: usize) -> Option<usize> {
    let bytes = text.as_bytes();
    let from = at + BEARER.find(&text[at..])?.end();
    let token = bytes[from..]
        .iter()
        .take_while(|byte| {
            byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'~' | b'+' | b'/' | b'-')
        })
        .count();
    // 16 characters or more, with a digit among the first 513: "the bearer responsibilities" stays.
    if token < 16
        || !bytes[from..from + token.min(513)]
            .iter()
            .any(u8::is_ascii_digit)
    {
        return None;
    }
    let padding = bytes[from + token..]
        .iter()
        .take_while(|byte| **byte == b'=')
        .count();
    Some(from + token + padding)
}

fn basic_at(text: &str, at: usize) -> Option<usize> {
    let bytes = text.as_bytes();
    if !token_start(bytes, at) {
        return None;
    }
    let from = at + BASIC.find(&text[at..])?.end();
    let token = bytes[from..]
        .iter()
        .take_while(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'+' | b'/'))
        .count();
    let padding = bytes[from + token..]
        .iter()
        .take_while(|byte| **byte == b'=')
        .count();
    // A digit or `+` among the first 513 characters, or padding within them.
    let marked = bytes[from..from + token.min(513)]
        .iter()
        .any(|byte| byte.is_ascii_digit() || *byte == b'+')
        || (token <= 512 && padding > 0);
    if token < 16 || !marked {
        return None;
    }
    Some(from + token + padding.min(2))
}

/// A private key block from `at`: up to its END line, or cut off by the string's end.
pub(crate) fn pem_at(text: &str, at: usize) -> Option<usize> {
    let begin = at + PEM_BEGIN.find(&text[at..])?.end();
    // Its body can't run past the next five dashes.
    let Some(dashes) = text[begin..].find("-----").map(|index| begin + index) else {
        return Some(text.len());
    };
    if let Some(end) = PEM_END.find(&text[dashes..]) {
        return Some(dashes + end.end());
    }
    PEM_CUT.is_match(&text[dashes..]).then_some(text.len())
}

fn secret_at(text: &str, at: usize) -> Option<usize> {
    let bytes = text.as_bytes();
    // A URL's credentials come first, so a key as their user goes with the password.
    if URL_SLASHES
        .iter()
        .any(|slashes| bytes[..at].ends_with(slashes.as_bytes()))
    {
        if let Some(end) = url_credentials(text, at) {
            return Some(end);
        }
    }
    match bytes[at] {
        byte if KEY_STARTS.contains(&byte) => {
            if !token_start(bytes, at) {
                return None;
            }
            SECRET_KEYS.find(&text[at..]).map(|found| at + found.end())
        }
        b'e' => {
            let start = at == 0
                || !(bytes[at - 1].is_ascii_alphanumeric() || matches!(bytes[at - 1], b'_' | b'-'))
                || after_escape(bytes, at, ESCAPE_LETTERS);
            if !start {
                return None;
            }
            JWT.find(&text[at..]).map(|found| at + found.end())
        }
        b'B' => bearer_at(text, at).or_else(|| basic_at(text, at)),
        b'b' => bearer_at(text, at),
        b'-' => pem_at(text, at),
        _ => None,
    }
}

// IP.

fn ip_at(text: &str, at: usize) -> Option<usize> {
    let bytes = text.as_bytes();
    let byte = bytes[at];
    if byte.is_ascii_digit() && token_start(bytes, at) {
        if let Some(found) = IPV4.find(&text[at..]) {
            return Some(at + found.end());
        }
    }
    if byte.is_ascii_hexdigit() || byte == b':' {
        let start = at == 0
            || !(bytes[at - 1].is_ascii_alphanumeric() || matches!(bytes[at - 1], b':' | b'.'))
            || after_escape(bytes, at, ESCAPE_LETTERS);
        if start {
            return IPV6
                .captures(&text[at..])
                .and_then(|found| found.get(1))
                .map(|address| at + address.end());
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    fn matches(pattern: Pattern, text: &str) -> Vec<&str> {
        find_all(pattern, text)
            .into_iter()
            .map(|(start, end)| &text[start..end])
            .collect()
    }

    #[test]
    fn email_starts_where_an_address_can() {
        assert_eq!(
            matches(Pattern::Email, "to a.b+c@example.com, x%40y.org."),
            ["a.b+c@example.com", "x%40y.org"]
        );
        // After a written escape, what ends the escape isn't part of the address.
        assert_eq!(matches(Pattern::Email, r"\nfoo@bar.com"), ["foo@bar.com"]);
        assert!(matches(Pattern::Email, r"\n@click.command").is_empty());
        assert_eq!(
            matches(Pattern::Email, r"jos\u00e9@example.com"),
            ["u00e9@example.com"]
        );
        assert_eq!(
            matches(Pattern::Email, r"CORP\alice@example.com"),
            ["alice@example.com"]
        );
        assert_eq!(
            matches(Pattern::Email, r"\u201cbob@example.com"),
            ["bob@example.com"]
        );
    }

    #[test]
    fn phone_needs_a_plus_or_us_punctuation() {
        assert_eq!(
            matches(
                Pattern::Phone,
                "call +44 20 7946 0958 or (555) 123-4567 or 555-123-4567."
            ),
            ["+44 20 7946 0958", "(555) 123-4567", "555-123-4567"]
        );
        assert!(matches(
            Pattern::Phone,
            "x555-123-4567 and 1699999999999 and 555 123 4567"
        )
        .is_empty());
        assert_eq!(matches(Pattern::Phone, r"\n555-123-4567"), ["555-123-4567"]);
    }

    #[test]
    fn card_reads_15_or_16_digits_but_not_an_expiry() {
        assert_eq!(
            matches(Pattern::Card, "card 4111 1111 1111 1111 exp"),
            ["4111 1111 1111 1111"]
        );
        assert_eq!(
            matches(Pattern::Card, "4111111111111111 12"),
            ["4111111111111111"]
        );
        assert_eq!(
            matches(Pattern::Card, "378282246310005 12/27"),
            ["378282246310005"]
        );
        assert_eq!(
            matches(Pattern::Card, "3056 9309 0259 04"),
            ["3056 9309 0259 04"]
        );
        assert!(matches(Pattern::Card, "0.4111111111111111").is_empty());
        assert!(matches(Pattern::Card, "x4111111111111111").is_empty());
    }

    #[test]
    fn secret_tries_its_alternatives_in_order_at_each_position() {
        let key = format!("sk-{}", "a".repeat(20));
        // Credentials first: the key goes with its password.
        assert_eq!(
            matches(Pattern::Secret, &format!("https://{key}:pw@host")),
            [format!("{key}:pw")]
        );
        // No credentials there, so the key is still found at the same position.
        assert_eq!(
            matches(Pattern::Secret, &format!("https://{key}/path")),
            std::slice::from_ref(&key)
        );
        assert_eq!(
            matches(Pattern::Secret, "Authorization: Basic abcdefghijklmnop="),
            ["Basic abcdefghijklmnop="]
        );
        assert_eq!(
            matches(Pattern::Secret, "Bearer abcdefghijklmnop1=="),
            ["Bearer abcdefghijklmnop1=="]
        );
        assert!(matches(Pattern::Secret, "the bearer responsibilities are many").is_empty());
        assert_eq!(
            matches(Pattern::Secret, "x -----BEGIN PRIVATE KEY-----\nMIIE"),
            ["-----BEGIN PRIVATE KEY-----\nMIIE"]
        );
        assert_eq!(
            matches(
                Pattern::Secret,
                "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY----- after"
            ),
            ["-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----"]
        );
        assert!(matches(
            Pattern::Secret,
            "-----BEGIN PRIVATE KEY-----\nMIIE\n-----BEGIN CERTIFICATE-----\nx"
        )
        .is_empty());
    }

    #[test]
    fn a_password_is_bounded_in_utf16_units() {
        // 128 emoji are 256 units: the second `@` is out of reach, as in JavaScript.
        let text = format!("'https://u:CANARY',@{}'x@host", "😀".repeat(128));
        assert_eq!(matches(Pattern::Secret, &text), ["u:CANARY',"]);
    }

    #[test]
    fn ip_reads_ipv4_and_ipv6() {
        assert_eq!(
            matches(Pattern::Ip, "from 10.0.0.1 and 2001:db8::1."),
            ["10.0.0.1", "2001:db8::1"]
        );
        assert_eq!(
            matches(Pattern::Ip, "1.1.1.1.1.1.1.1"),
            ["1.1.1.1", "1.1.1.1"]
        );
        assert!(matches(Pattern::Ip, "256.1.1.1 and 1.2.3.456 and v1.2.3.4").is_empty());
        assert_eq!(matches(Pattern::Ip, "::1 fe80::1"), ["::1", "fe80::1"]);
        assert!(
            rejected(Pattern::Ip, "::1")
                && rejected(Pattern::Ip, "fe80::1")
                && !rejected(Pattern::Ip, "2001:db8::1")
        );
        // A period and a digit after it: not an address's end.
        assert!(matches(Pattern::Ip, "2001:db8::1.5").is_empty());
    }
}
