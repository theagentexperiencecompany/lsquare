//! Built-in redaction (`packages/contract/REDACTION.md`): the five patterns, the secret key
//! names, placeholders numbered per event, and what is done at a cut.

pub(crate) mod patterns;
pub(crate) mod secret_spans;

use std::collections::{HashMap, HashSet};
use std::sync::LazyLock;

use regex::Regex;
use serde_json::{Map, Number, Value};
use sha2::{Digest, Sha256};

use crate::json::{js_f64, js_number, stringify, utf16_len};
use crate::shape::{
    binary_marker, depth_marker, is_marked, is_whole_placeholder, plain, Cut, KeyNames, Shaped,
};
pub(crate) use patterns::{Pattern, ORDER};
use secret_spans::{may_hold_key, secret_spans, ScanWork, SecretSpan, SCAN_WORK};

/// 0.1's secret key names. A key whose name ends with one of these, once lowercased and stripped
/// of everything but letters and digits, holds a secret whatever its value looks like:
/// `dbPassword`, `client_secret`, `X-Api-Key` and `AWS_SECRET_ACCESS_KEY` match, `apiKeyId`,
/// `maxTokens` and `author` don't. Part of the `secret` pattern. `redact_keys` adds more.
pub(crate) const SECRET_KEYS: [&str; 21] = [
    "password",
    "passwords",
    "passwd",
    "passphrase",
    "secret",
    "secrets",
    "token",
    "auth",
    "authorization",
    "apikey",
    "apikeys",
    "privatekey",
    "secretkey",
    "accesskey",
    "signingkey",
    "encryptionkey",
    "credential",
    "credentials",
    "cookie",
    "cookies",
    "connectionstring",
];

static EXISTING_PLACEHOLDER: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\[(EMAIL|PHONE|CARD|SECRET|IP)_([0-9]{1,9})\]").expect("placeholder pattern")
});
/// What a secret looks like at its start, for one that runs into a cut and so can't be matched
/// whole: a known key prefix or a JWT's `eyJ`, at the start of the token the cut ends.
static SECRET_START: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\A(?:lsq_|sk-|[rs]k_(?:live|test)_|whsec_|gh[opsur]_|github_pat_|glpat-|npm_|xox[a-z]-|xapp-|hooks\.slack(?:-gov)?\.com\\{0,2}/|AKIA|ASIA|AIza|ya29\.|GOCSPX-|phx_|eyJ)").expect("secret start pattern")
});
/// A Bearer or Basic token's scheme, just before the token the cut ends.
static AUTH_SCHEME: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?:[Bb]earer|BEARER|Basic|BASIC)[\t\n\x0B\x0C\r \u{a0}\u{1680}\u{2000}-\u{200a}\u{2028}\u{2029}\u{202f}\u{205f}\u{3000}\u{feff}]+\z").expect("scheme pattern")
});
static PEM_BEGIN: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\A-----BEGIN [A-Z ]{0,32}(?:PRIVATE KEY-----|\z)").expect("PEM pattern")
});
/// A PEM key's closing line, whole: until one follows its BEGIN line, the block runs into the cut.
static PEM_END: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"-----END [A-Z ]{0,32}PRIVATE KEY-----").expect("PEM pattern"));
/// What's after the backslash of an escape a value can start straight after.
static WRITTEN_ESCAPE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\A(?:[nrtfb0aevNLP_]|u[0-9A-Fa-f]{4}|x[0-9A-Fa-f]{2}|U[0-9A-Fa-f]{8})")
        .expect("escape pattern")
});
/// Matched values longer than this are numbered by a hash, so a pending event doesn't keep them.
const MAX_KEPT_IDENTITY: usize = 64;
/// A card number written as a JSON number.
static CARD_NUMBER: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\A(?:[3-6][0-9]{14,15}|2(?:20[0-4]|22[1-9]|2[3-9][0-9]|[3-6][0-9][0-9]|7[01][0-9]|720)[0-9]{11,12}|3[068][0-9]{12})\z").expect("card pattern")
});

/// The patterns an event is redacted with, as the shared cases give the option: all of them, none
/// (`redact: false`), or all but those turned off.
#[cfg(test)]
pub(crate) fn resolve_patterns(option: Option<&Value>) -> Vec<Pattern> {
    match option {
        Some(Value::Bool(false)) => Vec::new(),
        Some(Value::Object(settings)) => ORDER
            .into_iter()
            .filter(|pattern| settings.get(pattern.name()) != Some(&Value::Bool(false)))
            .collect(),
        _ => ORDER.to_vec(),
    }
}

/// `redact_keys`, as the names are matched: lowercased, stripped to `a-z0-9`, each once. A name
/// with nothing left is skipped.
pub(crate) fn resolve_keys<'a>(names: impl IntoIterator<Item = &'a str>) -> Vec<String> {
    let mut keys: Vec<String> = Vec::new();
    for name in names.into_iter().take(1_000) {
        let name = crate::shape::normalize_key(name);
        if !name.is_empty() && !keys.contains(&name) {
            keys.push(name);
        }
    }
    keys
}

/// Whether a `userId` looks like an email address or phone number, which the SDK warns about.
pub(crate) fn looks_like_contact(text: &str) -> bool {
    !patterns::find_all(Pattern::Email, text).is_empty()
        || !patterns::find_all(Pattern::Phone, text).is_empty()
}

/// How a field is finalised.
#[derive(Debug, Clone, Copy)]
pub(crate) struct Scope {
    /// Redact every string and key. Without it, only the strings that were cut are, and what's
    /// under secret key names in JSON a string holds.
    pub all: bool,
    /// Keys come from the app's data: secret key names apply.
    pub free_form: bool,
    /// Numbers that look like card numbers are redacted (tool inputs).
    pub cards: bool,
    /// The field must be a string: a file becomes `[binary, N bytes]`.
    pub text: bool,
}

pub(crate) const JSON_FIELD: Scope = Scope {
    all: true,
    free_form: true,
    cards: true,
    text: false,
};
pub(crate) const TEXT_FIELD: Scope = Scope {
    all: true,
    free_form: false,
    cards: false,
    text: true,
};
/// Free-form JSON that isn't a tool input: secret key names apply, card numbers don't.
pub(crate) const FREE_FORM: Scope = Scope {
    cards: false,
    ..JSON_FIELD
};

impl Scope {
    pub(crate) fn with_all(self, all: bool) -> Scope {
        Scope { all, ..self }
    }
}

/// Redaction of one event. Placeholders are numbered per event: the same value gets the same
/// number everywhere, also across `before_send`, when the same Redactor finishes the event.
#[derive(Debug)]
pub(crate) struct Redactor {
    /// Replacements made, per pattern, in the order each was first made.
    counts: Vec<(Pattern, u64)>,
    /// What this SDK made of strings before `before_send`, as the hook got them, each with the
    /// tightest limit it keeps its form under after the hook: a cut, the limit it was cut at; a
    /// string whose secret values were replaced, its length before that, since placeholders may
    /// have made it longer than the limit it fitted. Only what is still in the event, after `prune`.
    pub cuts: HashMap<String, usize>,
    used: HashMap<Pattern, HashSet<u64>>,
    next: HashMap<Pattern, u64>,
    assigned: HashMap<(Pattern, String), u64>,
    patterns: Vec<Pattern>,
    secret_keys: bool,
    key_names: KeyNames,
    scan_work: ScanWork,
    /// What the scan made of strings before `before_send`, which isn't read again: a placeholder
    /// could be taken for part of what's around it.
    scanned: HashSet<String>,
}

impl Redactor {
    pub(crate) fn new(patterns: &[Pattern], extra_keys: &[String]) -> Redactor {
        let mut names: Vec<String> = SECRET_KEYS.iter().map(|name| (*name).to_owned()).collect();
        names.extend(extra_keys.iter().cloned());
        Redactor {
            counts: Vec::new(),
            cuts: HashMap::new(),
            used: HashMap::new(),
            next: HashMap::new(),
            assigned: HashMap::new(),
            secret_keys: patterns.contains(&Pattern::Secret),
            patterns: patterns.to_vec(),
            key_names: KeyNames(names),
            scan_work: ScanWork { left: SCAN_WORK },
            scanned: HashSet::new(),
        }
    }

    /// The replacements made, as an event's `redactions`; `None` when there were none.
    pub(crate) fn counts(&self) -> Option<Value> {
        if self.counts.is_empty() {
            return None;
        }
        Some(Value::Object(
            self.counts
                .iter()
                .map(|(pattern, count)| (pattern.name().to_owned(), Value::from(*count)))
                .collect(),
        ))
    }

    /// For shaping: `None` when the `secret` pattern is off.
    pub(crate) fn key_names(&self) -> Option<KeyNames> {
        self.secret_keys.then(|| self.key_names.clone())
    }

    pub(crate) fn is_secret_key(&self, key: &str) -> bool {
        self.key_names.matches(key)
    }

    /// Records placeholders already in the event, so new ones never reuse their numbers.
    pub(crate) fn reserve(&mut self, value: &Shaped) {
        match value {
            Shaped::String(text) => self.reserve_text(text),
            Shaped::Cut(cut) => self.reserve_text(&cut.window),
            Shaped::Array(items) => items.iter().for_each(|item| self.reserve(item)),
            Shaped::Object(entries) => {
                for (key, item) in entries {
                    self.reserve_text(key);
                    self.reserve(item);
                }
            }
            // A secret's identity may hold placeholders, but it is sent as a new one: nothing to reserve.
            _ => {}
        }
    }

    /// The same, for what is JSON already: an event's fixed fields.
    pub(crate) fn reserve_json(&mut self, value: &Value) {
        match value {
            Value::String(text) => self.reserve_text(text),
            Value::Array(items) => items.iter().for_each(|item| self.reserve_json(item)),
            Value::Object(entries) => {
                for (key, item) in entries {
                    self.reserve_text(key);
                    self.reserve_json(item);
                }
            }
            _ => {}
        }
    }

    pub(crate) fn reserve_text(&mut self, text: &str) {
        if !text.contains('[') {
            return;
        }
        for found in EXISTING_PLACEHOLDER.captures_iter(text) {
            if let (Some(pattern), Ok(number)) =
                (Pattern::from_label(&found[1]), found[2].parse::<u64>())
            {
                self.used.entry(pattern).or_default().insert(number);
            }
        }
    }

    /// Shaped JSON to JSON: cuts redacted and closed, secrets replaced, files and deep containers
    /// as markers. Secret key names apply in free-form fields and in JSON held in a string even
    /// before the hook.
    pub(crate) fn finalize(&mut self, value: &Shaped, scope: Scope) -> Value {
        match value {
            Shaped::Cut(cut) => Value::String(self.finalize_cut(cut)),
            // Couldn't be read whole: numbered on its own, never equated with another secret.
            Shaped::Secret(identity) => Value::String(self.placeholder(
                Pattern::Secret,
                identity.as_deref().unwrap_or(""),
                identity.is_none(),
            )),
            Shaped::Binary(bytes) => {
                if scope.text {
                    Value::String(format!("[binary, {bytes} bytes]"))
                } else {
                    binary_marker(*bytes)
                }
            }
            Shaped::TooDeep => depth_marker(),
            Shaped::String(text) => Value::String(if scope.all {
                self.string(text)
            } else {
                self.before_hook(text)
            }),
            Shaped::Number(number) => {
                if scope.all && scope.cards {
                    self.number(number)
                } else {
                    Value::Number(number.clone())
                }
            }
            Shaped::Null => Value::Null,
            Shaped::Bool(value) => Value::Bool(*value),
            Shaped::Array(items) => Value::Array(
                items
                    .iter()
                    .map(|item| self.finalize(item, scope))
                    .collect(),
            ),
            Shaped::Object(entries) => {
                let mut out = Map::with_capacity(entries.len());
                for (key, item) in entries {
                    let mut name = if scope.all {
                        self.text(key, false)
                    } else {
                        key.clone()
                    };
                    // Two keys that redact to the same placeholder must not overwrite each other.
                    if scope.all && out.contains_key(&name) {
                        name = self.text(key, true);
                    }
                    let finished = if scope.free_form
                        && self.secret_keys
                        && self.is_secret_key(key)
                        && !matches!(item, Shaped::Secret(_))
                    {
                        self.secret_value(plain(item))
                    } else {
                        self.finalize(item, scope)
                    };
                    out.insert(name, finished);
                }
                Value::Object(out)
            }
        }
    }

    /// A text field, which is always a string once finalised.
    pub(crate) fn finalize_text(&mut self, value: &Shaped, all: bool) -> String {
        match self.finalize(value, TEXT_FIELD.with_all(all)) {
            Value::String(text) => text,
            other => stringify(&other),
        }
    }

    fn finalize_cut(&mut self, cut: &Cut) -> String {
        let text = format!(
            "{}{}",
            self.cut_text(&cut.window, cut.cut, cut.scanned),
            cut.mark
        );
        let limit = self
            .cuts
            .get(&text)
            .map_or(cut.limit, |limit| (*limit).min(cut.limit));
        self.cuts.insert(text.clone(), limit);
        text
    }

    /// Keeps only what the event still holds once it has been fitted: the cuts in it, and the
    /// numbers of the placeholders in it with what they stand for. An event waiting for its hook
    /// then holds no more of what redaction matched than what it still shows.
    pub(crate) fn prune(&mut self, event: &Value) {
        let mut strings: HashSet<&str> = HashSet::new();
        let mut present: HashMap<Pattern, HashSet<u64>> = HashMap::new();
        fn walk<'a>(value: &'a Value, note: &mut impl FnMut(&'a str)) {
            match value {
                Value::String(text) => note(text),
                Value::Array(items) => items.iter().for_each(|item| walk(item, note)),
                Value::Object(entries) => {
                    for (key, item) in entries {
                        note(key);
                        walk(item, note);
                    }
                }
                _ => {}
            }
        }
        walk(event, &mut |text: &str| {
            strings.insert(text);
            if !text.contains('[') {
                return;
            }
            for found in EXISTING_PLACEHOLDER.captures_iter(text) {
                if let (Some(pattern), Ok(number)) =
                    (Pattern::from_label(&found[1]), found[2].parse::<u64>())
                {
                    present.entry(pattern).or_default().insert(number);
                }
            }
        });
        self.cuts.retain(|text, _| strings.contains(text.as_str()));
        self.scanned.retain(|text| strings.contains(text.as_str()));
        self.assigned.retain(|(pattern, _), number| {
            present
                .get(pattern)
                .is_some_and(|numbers| numbers.contains(number))
        });
        self.used = present;
    }

    /// The whole value under a secret key name, whatever its shape. Values that can't hold a secret
    /// (null, booleans, empty values) and existing placeholders stay as they are.
    fn secret_value(&mut self, value: Value) -> Value {
        match &value {
            Value::Null | Value::Bool(_) => value,
            Value::String(text) if text.is_empty() || is_whole_placeholder(text) => value,
            Value::Array(items) if items.is_empty() => value,
            Value::Object(entries) if entries.is_empty() => value,
            Value::String(text) => Value::String(self.placeholder(Pattern::Secret, text, false)),
            other => Value::String(self.placeholder(Pattern::Secret, &stringify(other), false)),
        }
    }

    /// A string value: what's under secret key names in JSON it holds, then the patterns.
    pub(crate) fn string(&mut self, input: &str) -> String {
        if self.scanned.contains(input) {
            return self.text(input, false);
        }
        let without = self.without_secret_values(input);
        self.text(&without, false)
    }

    /// A string value before the hook: only what's under secret key names, which the hook never sees.
    fn before_hook(&mut self, input: &str) -> String {
        let out = self.without_secret_values(input);
        if may_hold_key(&out) {
            self.scanned.insert(out.clone());
        }
        // One ending with a marker is read as any other marked string after the hook.
        if out != input && !is_marked(&out) {
            let length = input.chars().count();
            let limit = self
                .cuts
                .get(&out)
                .map_or(length, |limit| (*limit).min(length));
            self.cuts.insert(out.clone(), limit);
        }
        out
    }

    fn without_secret_values(&mut self, input: &str) -> String {
        self.window_without_secret_values(input, input.len()).0
    }

    /// Replaces what's under secret key names in JSON held in `window`, which reads past the cut at
    /// `cut`: the rewritten window and where the cut now is. A value starting past the cut is
    /// neither replaced nor counted. One running past it is replaced whole, and its placeholder
    /// ends what's kept.
    fn window_without_secret_values(&mut self, window: &str, cut: usize) -> (String, usize) {
        if !self.secret_keys || !may_hold_key(window) {
            return (window.to_owned(), cut);
        }
        let spans = {
            let names = &self.key_names;
            secret_spans(window, &|key| names.matches(key), &mut self.scan_work)
        };
        if spans.is_empty() {
            return (window.to_owned(), cut);
        }
        let mut out = String::with_capacity(window.len());
        let mut from = 0;
        let mut at = cut as isize;
        for span in &spans {
            if span.start >= cut {
                break;
            }
            let placeholder = self.span_placeholder(span);
            out.push_str(&window[from..span.start]);
            out.push_str(&placeholder);
            from = span.end;
            if span.end > cut {
                let kept = out.len();
                out.push_str(&window[from..]);
                return (out, kept);
            }
            at += placeholder.len() as isize - (span.end - span.start) as isize;
        }
        out.push_str(&window[from..]);
        (out, at.max(0) as usize)
    }

    fn span_placeholder(&mut self, span: &SecretSpan) -> String {
        let placeholder = self.placeholder(
            Pattern::Secret,
            span.identity.as_deref().unwrap_or(""),
            span.identity.is_none(),
        );
        format!("{}{}{}", span.quote, placeholder, span.quote)
    }

    /// The patterns, on text that isn't read for secret key names: a key, an error's type.
    pub(crate) fn text(&mut self, input: &str, fresh: bool) -> String {
        let mut out = input.to_owned();
        for pattern in self.patterns.clone() {
            if patterns::skip(pattern, &out) {
                continue;
            }
            let found = patterns::find_all(pattern, &out);
            if found.is_empty() {
                continue;
            }
            let mut replaced = String::with_capacity(out.len());
            let mut from = 0;
            for (start, end) in found {
                let matched = &out[start..end];
                replaced.push_str(&out[from..start]);
                if patterns::rejected(pattern, matched) {
                    replaced.push_str(matched);
                } else if pattern == Pattern::Secret && leaves_url(&out, start, end) {
                    replaced.push_str(&self.secrets_in(matched, fresh));
                } else {
                    replaced.push_str(&self.placeholder(
                        pattern,
                        &patterns::identity(pattern, matched),
                        fresh,
                    ));
                }
                from = end;
            }
            replaced.push_str(&out[from..]);
            out = replaced;
        }
        out
    }

    /// A match that holds no credentials: only the secrets in it go.
    fn secrets_in(&mut self, matched: &str, fresh: bool) -> String {
        let mut out = String::with_capacity(matched.len());
        let mut from = 0;
        for (start, end) in patterns::find_all(Pattern::Secret, matched) {
            out.push_str(&matched[from..start]);
            out.push_str(&self.placeholder(Pattern::Secret, &matched[start..end], fresh));
            from = end;
        }
        out.push_str(&matched[from..]);
        out
    }

    /// Redacts what's kept of a cut string, reading `window` past the cut at `cut`: values under
    /// secret key names in JSON it holds, then the patterns. A match that runs past the cut is
    /// masked whole: its placeholder ends what's kept. Matches entirely past the cut are neither
    /// replaced nor counted. Then a secret that starts like one and runs into the cut, or past the
    /// window, is masked too.
    pub(crate) fn cut_text(&mut self, window: &str, cut: usize, scanned: bool) -> String {
        let (mut out, mut at) = if scanned {
            (window.to_owned(), cut)
        } else {
            self.window_without_secret_values(window, cut)
        };
        for pattern in self.patterns.clone() {
            if patterns::skip(pattern, &out) {
                continue;
            }
            let found = patterns::find_all(pattern, &out);
            if found.is_empty() {
                continue;
            }
            let mut replaced = String::with_capacity(out.len());
            let mut from = 0;
            // What's kept so far ends here, once a match has run past the cut.
            let mut straddled: Option<usize> = None;
            let mut shift: isize = 0;
            for (start, end) in found {
                let matched = &out[start..end];
                replaced.push_str(&out[from..start]);
                from = end;
                if start >= at || straddled.is_some() || patterns::rejected(pattern, matched) {
                    replaced.push_str(matched);
                    continue;
                }
                let fits = end <= at;
                let placeholder =
                    if fits && pattern == Pattern::Secret && leaves_url(&out, start, end) {
                        self.secrets_in(matched, false)
                    } else {
                        self.placeholder(pattern, &patterns::identity(pattern, matched), false)
                    };
                replaced.push_str(&placeholder);
                if fits {
                    shift += placeholder.len() as isize - matched.len() as isize;
                } else {
                    straddled = Some(replaced.len());
                }
            }
            replaced.push_str(&out[from..]);
            at = straddled.unwrap_or((at as isize + shift).max(0) as usize);
            out = replaced;
        }
        self.mask_at_cut(&out[..at])
    }

    /// A secret whose end was cut off: its kept part becomes the placeholder.
    fn mask_at_cut(&mut self, kept: &str) -> String {
        if !self.secret_keys {
            return kept.to_owned();
        }
        let bytes = kept.as_bytes();
        if let Some(begin) = kept.rfind("-----BEGIN ") {
            // Its first 64 characters, as far as the string goes.
            let head = kept[begin..]
                .char_indices()
                .nth(64)
                .map_or(kept.len(), |(index, _)| begin + index);
            if PEM_BEGIN.is_match(&kept[begin..head]) && !PEM_END.is_match(&kept[begin..]) {
                return format!(
                    "{}{}",
                    &kept[..begin],
                    self.placeholder(Pattern::Secret, &kept[begin..], false)
                );
            }
        }
        let is_token = |byte: u8| {
            byte.is_ascii_alphanumeric()
                || matches!(byte, b'.' | b'_' | b'~' | b'+' | b'/' | b'=' | b'-')
        };
        let mut run = kept.len();
        while run > 0 && is_token(bytes[run - 1]) {
            run -= 1;
        }
        // What ends an escape written in JSON, such as the `n` of `\\n`, isn't part of the token.
        if run > 0 && bytes[run - 1] == b'\\' {
            run += WRITTEN_ESCAPE
                .find(&kept[run..(run + 9).min(kept.len())])
                .map_or(0, |found| found.end());
        }
        // The earliest place in the run where a secret could start: its beginning, or just after a
        // character that ends one in the patterns, as `=` does in `token=eyJ…`.
        for start in run..kept.len() {
            if start > run && !matches!(bytes[start - 1], b'.' | b'~' | b'+' | b'/' | b'=') {
                continue;
            }
            let token = &kept[start..];
            let scheme = start == run && {
                // The 64 characters before the token.
                let from = kept[..start]
                    .char_indices()
                    .rev()
                    .nth(63)
                    .map_or(0, |(index, _)| index);
                AUTH_SCHEME.is_match(&kept[from..start])
            };
            if SECRET_START.is_match(token) || scheme {
                return format!(
                    "{}{}",
                    &kept[..start],
                    self.placeholder(Pattern::Secret, token, false)
                );
            }
        }
        kept.to_owned()
    }

    /// Card numbers stored as JSON numbers inside tool inputs.
    fn number(&mut self, number: &Number) -> Value {
        let unchanged = || Value::Number(number.clone());
        if !self.patterns.contains(&Pattern::Card) {
            return unchanged();
        }
        // A positive integer, however it was written: `4111111111111111.0` is one too, read
        // exactly because this crate turns on `serde_json`'s `float_roundtrip` feature.
        let digits = match (number.as_u64(), number.as_f64()) {
            (Some(value), _) if value > 0 => js_number(number),
            (None, Some(value)) if value > 0.0 && value < 1e17 && value.fract() == 0.0 => {
                js_f64(value)
            }
            _ => return unchanged(),
        };
        if CARD_NUMBER.is_match(&digits) && patterns::luhn(&digits) {
            Value::String(self.placeholder(Pattern::Card, &digits, false))
        } else {
            unchanged()
        }
    }

    fn placeholder(&mut self, pattern: Pattern, identity: &str, fresh: bool) -> String {
        // A long value is numbered by its hash, so an event waiting for its hook doesn't keep it.
        let key = if utf16_len(identity) > MAX_KEPT_IDENTITY {
            let digest: String = Sha256::digest(identity.as_bytes())
                .iter()
                .map(|byte| format!("{byte:02x}"))
                .collect();
            (pattern, format!("#{digest}"))
        } else {
            (pattern, identity.to_owned())
        };
        let number = match (fresh, self.assigned.get(&key)) {
            (false, Some(number)) => *number,
            _ => {
                let number = self.next_free(pattern);
                if !fresh {
                    self.assigned.insert(key, number);
                }
                number
            }
        };
        match self
            .counts
            .iter_mut()
            .find(|(counted, _)| *counted == pattern)
        {
            Some((_, count)) => *count += 1,
            None => self.counts.push((pattern, 1)),
        }
        format!("[{}_{number}]", pattern.label())
    }

    /// A running counter per pattern, skipping numbers already in the event: linear overall.
    fn next_free(&mut self, pattern: Pattern) -> u64 {
        let used = self.used.entry(pattern).or_default();
        let mut number = self.next.get(&pattern).copied().unwrap_or(1);
        while used.contains(&number) {
            number += 1;
        }
        used.insert(number);
        self.next.insert(pattern, number + 1);
        number
    }
}

/// Whether `form` at `at` is the whole of how a quote is written there: `o''''brien` and a shell's
/// `'\''` hold neither `'` nor `''`.
fn is_written_as(text: &[u8], at: usize, form: &[u8]) -> bool {
    let quote = form[form.len() - 1];
    let before = at.checked_sub(1).map(|index| text[index]);
    let after = text.get(at + form.len()).copied();
    before != Some(b'\\') && before != Some(quote) && after != Some(b'\\') && after != Some(quote)
}

fn find_from(text: &[u8], pattern: &[u8], from: usize) -> Option<usize> {
    if from > text.len() {
        return None;
    }
    text[from..]
        .windows(pattern.len())
        .position(|window| window == pattern)
        .map(|index| from + index)
}

/// Whether a URL's credentials match ran out of its URL, and so holds none: `host:443','ok','ops`
/// in `['https://host:443','ok','ops@...']`. It takes all of this: the URL is straight after a `'`
/// or a backtick, alone, doubled or after a backslash, as an SQL or JavaScript string inside
/// another writes it. That quote closes before any `@` and with no word character after it, as
/// `o'brien` has, and it opens again before the match ends, written the same way each time.
/// Anything else stays masked whole. Linear: it reads the match and at most 42 characters before
/// it, and matches don't overlap.
fn leaves_url(whole: &str, start: usize, end: usize) -> bool {
    let bytes = whole.as_bytes();
    if bytes.get(end) != Some(&b'@') {
        return false;
    }
    let Some(slashes) = patterns::URL_SLASHES
        .iter()
        .find(|written| bytes[..start].ends_with(written.as_bytes()))
    else {
        return false;
    };
    let colon = start - slashes.len();
    let mut scheme = colon;
    while scheme > 0
        && colon - scheme < 32
        && (bytes[scheme - 1].is_ascii_alphanumeric()
            || matches!(bytes[scheme - 1], b'+' | b'.' | b'-'))
    {
        scheme -= 1;
    }
    if scheme == colon || scheme == 0 {
        return false;
    }
    let quote = bytes[scheme - 1];
    if quote != b'\'' && quote != b'`' {
        return false;
    }
    let previous = scheme.checked_sub(2).map(|index| bytes[index]);
    let form: Vec<u8> = match previous {
        Some(previous) if previous == quote || previous == b'\\' => vec![previous, quote],
        _ => vec![quote],
    };
    if !is_written_as(bytes, scheme - form.len(), &form) {
        return false;
    }
    let matched = &bytes[start..end];
    let is_word =
        |byte: Option<&u8>| byte.is_some_and(|byte| byte.is_ascii_alphanumeric() || *byte == b'_');
    let closed_by =
        |at: usize| is_written_as(matched, at, &form) && !is_word(matched.get(at + form.len()));
    let mut closes = find_from(matched, &form, 0);
    while let Some(at) = closes {
        if closed_by(at) {
            break;
        }
        closes = find_from(matched, &form, at + 1);
    }
    let Some(closes) = closes else { return false };
    if matched[..closes].contains(&b'@') {
        return false;
    }
    let mut opens = find_from(matched, &form, closes + form.len());
    while let Some(at) = opens {
        if is_written_as(matched, at, &form) {
            return true;
        }
        opens = find_from(matched, &form, at + 1);
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::shape::{shape_json, shape_string, ShapeState};

    fn redactor() -> Redactor {
        Redactor::new(&ORDER, &[])
    }

    #[test]
    fn the_same_value_gets_the_same_number() {
        let mut redactor = redactor();
        assert_eq!(
            redactor.string("a@b.co wrote to c@d.co, then A@B.co again"),
            "[EMAIL_1] wrote to [EMAIL_2], then [EMAIL_1] again"
        );
        assert_eq!(redactor.counts(), Some(serde_json::json!({ "email": 3 })));
    }

    #[test]
    fn numbering_skips_placeholders_already_in_the_event() {
        let mut redactor = redactor();
        redactor.reserve_text("already [EMAIL_1] and [EMAIL_3]");
        assert_eq!(
            redactor.string("a@b.co c@d.co e@f.co"),
            "[EMAIL_2] [EMAIL_4] [EMAIL_5]"
        );
    }

    #[test]
    fn a_secret_under_a_key_and_by_a_pattern_share_a_number() {
        let mut redactor = redactor();
        let key = format!("sk-{}", "a".repeat(24));
        let text = format!("{{\"apiKey\":\"{key}\"}} and {key}");
        assert_eq!(
            redactor.string(&text),
            "{\"apiKey\":\"[SECRET_1]\"} and [SECRET_1]"
        );
    }

    #[test]
    fn a_list_of_strings_holds_no_credentials() {
        let mut redactor = redactor();
        assert_eq!(
            redactor.string("['https://host:443','ok','ops@example.com']"),
            "['https://host:443','ok','[EMAIL_1]']"
        );
        assert_eq!(
            redactor.string("https://host:443,ops@example.com"),
            "https://[SECRET_1]@example.com"
        );
    }

    #[test]
    fn a_secret_running_into_the_cut_is_masked_whole() {
        let mut redactor = redactor();
        let mut state = ShapeState::new(None);
        // An emoji before a key that straddles the cut: positions are bytes here, units in the scanner.
        let text = format!("{}😀 sk-{}", "a".repeat(3_990), "b".repeat(40));
        let Shaped::Cut(cut) = shape_string(&text, 4_000, &mut state) else {
            panic!("not cut")
        };
        assert_eq!(
            redactor.finalize_cut(&cut),
            format!("{}😀 [SECRET_1]…[+35 chars]", "a".repeat(3_990))
        );
    }

    #[test]
    fn a_card_number_as_a_number_is_replaced_in_a_tool_input() {
        let mut redactor = redactor();
        let mut state = ShapeState::new(None);
        let value =
            serde_json::json!({ "card": 4111111111111111u64, "count": 4111111111111112u64 });
        let shaped = shape_json(&value, 16_000, &mut state);
        assert_eq!(
            redactor.finalize(&shaped, JSON_FIELD),
            serde_json::json!({ "card": "[CARD_1]", "count": 4111111111111112u64 })
        );
    }

    #[test]
    fn a_card_number_written_as_a_float_is_replaced_too() {
        // From the JSON text, as an MCP client sends it: serde_json reads the first a half off.
        for literal in [
            "4000000000000150.0",
            "4111111111111111.0",
            "4111111111111111",
        ] {
            let value: Value = serde_json::from_str(&format!("{{\"n\": {literal}}}")).unwrap();
            let mut redactor = redactor();
            let mut state = ShapeState::new(None);
            let shaped = shape_json(&value, 16_000, &mut state);
            assert_eq!(
                redactor.finalize(&shaped, JSON_FIELD),
                serde_json::json!({ "n": "[CARD_1]" }),
                "{literal}"
            );
        }
    }

    #[test]
    fn a_long_secret_is_numbered_by_its_hash() {
        let mut redactor = redactor();
        let long = format!("sk-{}", "a".repeat(80));
        assert_eq!(
            redactor.string(&format!("{long} {long} sk-{}", "b".repeat(80))),
            "[SECRET_1] [SECRET_1] [SECRET_2]"
        );
        assert!(redactor
            .assigned
            .keys()
            .all(|(_, identity)| identity.starts_with('#') && identity.len() == 65));
    }

    #[test]
    fn a_pattern_can_be_turned_off() {
        let patterns = resolve_patterns(Some(&serde_json::json!({ "ip": false })));
        let mut redactor = Redactor::new(&patterns, &[]);
        assert_eq!(redactor.string("10.0.0.1 a@b.co"), "10.0.0.1 [EMAIL_1]");
        assert!(resolve_patterns(Some(&Value::Bool(false))).is_empty());
    }
}
