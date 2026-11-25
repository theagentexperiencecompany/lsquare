//! Shortening: turns values from the app into event JSON within the limits, before redaction.
//! Strings over a field's limit become [`Cut`]s, which keep what redaction needs to see past the
//! cut; strings that hold a file become [`Shaped::Binary`]. Redaction (`redact`) turns both into
//! JSON.
//!
//! Every count is in Unicode code points, as in the TypeScript and Python SDKs, so all three cut
//! in the same places.

use std::collections::HashMap;
use std::sync::LazyLock;

use regex::Regex;
use serde_json::{Map, Number, Value};

use crate::json::{advance, js_entries, js_held, stringify, utf16_len};

/// Characters (code points) read per event, plus one per value visited, across all its fields.
pub(crate) const SCAN_BUDGET: usize = 1_000_000;
pub(crate) const LIMIT_INPUT: usize = 16_000;
pub(crate) const LIMIT_AGENT: usize = 16_000;
pub(crate) const LIMIT_RESULT: usize = 4_000;
pub(crate) const LIMIT_INTENT: usize = 500;
pub(crate) const LIMIT_DESCRIPTION: usize = 1_000;
pub(crate) const LIMIT_INSTRUCTIONS: usize = 2_000;
pub(crate) const MAX_ENTRIES: usize = 100;
pub(crate) const MAX_LEVELS: usize = 10;
/// Redaction reads this far past a cut, so a secret that runs into it is still recognised.
pub(crate) const LOOKAHEAD: usize = 8_000;
const BINARY_MIN: usize = 1_000;
const BINARY_HEAD: usize = 4_096;
const BINARY_TAIL: usize = 1_024;
const DATA_HEADER_MAX: usize = 1_024;
pub(crate) const CUT_MARK: &str = "…[cut]";

/// A marker this SDK writes, at the end of a string it cut.
static MARKER: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"…\[(?:\+([0-9]{1,16}) chars|cut)\]\z").expect("marker pattern"));
/// A placeholder from redaction, kept as it is under a secret key name.
static WHOLE_PLACEHOLDER: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\A\[(?:EMAIL|PHONE|CARD|SECRET|IP)_[0-9]{1,9}\]\z").expect("placeholder pattern")
});

/// Whether a string is exactly one placeholder.
pub(crate) fn is_whole_placeholder(text: &str) -> bool {
    text.len() <= 20 && WHOLE_PLACEHOLDER.is_match(text)
}

/// A shortened string: `window` is what was kept plus what redaction reads past `cut` (a byte
/// index), cut for a field whose limit is `limit`.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct Cut {
    pub window: String,
    pub cut: usize,
    pub mark: String,
    pub limit: usize,
    /// Already read for values under secret key names, with the lookahead this one no longer has.
    pub scanned: bool,
}

/// Event JSON before redaction.
#[derive(Debug, Clone, PartialEq)]
pub(crate) enum Shaped {
    Null,
    Bool(bool),
    Number(Number),
    String(String),
    Cut(Cut),
    /// A string that held a file: only its decoded size is kept.
    Binary(u64),
    /// A container below the nesting limit.
    TooDeep,
    /// A value under a secret key name, replaced whole before anything else sees it, `before_send`
    /// included. Its identity tells two equal secrets apart from different ones, for numbering:
    /// the raw value, or `None` when it couldn't be read whole, so it is numbered on its own.
    Secret(Option<String>),
    Array(Vec<Shaped>),
    Object(Vec<(String, Shaped)>),
}

/// This event's secret key names: 0.1's, and the app's `redact_keys`.
#[derive(Debug, Clone)]
pub(crate) struct KeyNames(pub Vec<String>);

impl KeyNames {
    /// A key whose name, lowercased and stripped to `a-z0-9`, ends with one of the names.
    pub(crate) fn matches(&self, key: &str) -> bool {
        let name = normalize_key(key);
        self.0.iter().any(|suffix| name.ends_with(suffix.as_str()))
    }
}

/// A key as its name is matched: lowercased, and stripped of everything but `a-z0-9`.
pub(crate) fn normalize_key(key: &str) -> String {
    key.to_lowercase()
        .chars()
        .filter(|ch| ch.is_ascii_lowercase() || ch.is_ascii_digit())
        .collect()
}

pub(crate) struct ShapeState<'a> {
    /// What is left of the scan budget.
    pub remaining: usize,
    /// Something was cut, left out at a limit, or replaced by a size.
    pub shortened: bool,
    /// Something couldn't be kept as JSON and was removed (`not_json`).
    pub lossy: bool,
    /// Shaping `before_send`'s output: a string this SDK cut keeps its marker.
    pub after_hook: bool,
    /// The strings this SDK cut or rewrote before `before_send`, as the hook got them.
    pub cuts: Option<&'a HashMap<String, usize>>,
    /// This event's secret key names; `None` when the `secret` pattern is off.
    pub secret_keys: Option<&'a KeyNames>,
    /// Whether they apply to the field being shaped: one whose keys come from the app's data.
    pub secret_key: bool,
}

impl<'a> ShapeState<'a> {
    pub(crate) fn new(secret_keys: Option<&'a KeyNames>) -> Self {
        ShapeState {
            remaining: SCAN_BUDGET,
            shortened: false,
            lossy: false,
            after_hook: false,
            cuts: None,
            secret_keys,
            secret_key: false,
        }
    }

    pub(crate) fn after_hook(
        secret_keys: Option<&'a KeyNames>,
        cuts: &'a HashMap<String, usize>,
    ) -> Self {
        ShapeState {
            after_hook: true,
            cuts: Some(cuts),
            ..ShapeState::new(secret_keys)
        }
    }

    /// Whether `key` is a secret key name here.
    pub(crate) fn hides(&self, key: &str) -> bool {
        self.secret_key && self.secret_keys.is_some_and(|keys| keys.matches(key))
    }

    /// Whether `key` is one of this event's secret key names, wherever it is.
    pub(crate) fn is_secret_key(&self, key: &str) -> bool {
        self.secret_keys.is_some_and(|keys| keys.matches(key))
    }
}

fn is_base64(bytes: &[u8]) -> bool {
    bytes.iter().all(|byte| {
        byte.is_ascii_alphanumeric() || matches!(byte, b'+' | b'/' | b'-' | b'_' | b'=')
    })
}

/// Bytes a base64 payload of `length` characters decodes to; `text` ends with its padding (at most 2).
pub(crate) fn decoded_size(length: usize, text: &str) -> u64 {
    let padding = text
        .bytes()
        .rev()
        .take(2)
        .take_while(|byte| *byte == b'=')
        .count()
        .min(length);
    (length - padding) as u64 * 3 / 4
}

/// The decoded size of a string that holds a file: any `data:` URI with `;base64,`, or a string of
/// at least 1,000 characters whose first 4 KB and last 1 KB are only base64 characters. A
/// heuristic: the rest isn't read.
pub(crate) fn binary_size(text: &str) -> Option<u64> {
    let bytes = text.as_bytes();
    if bytes.len() >= 5 && bytes[..5].eq_ignore_ascii_case(b"data:") {
        // The header, at most 1,024 characters, up to its comma: UTF-16 units, as JavaScript counts.
        let mut units = 0;
        let header_end = text.char_indices().find(|(_, ch)| {
            units += ch.len_utf16();
            units > DATA_HEADER_MAX + 1
        });
        let header_end = header_end.map_or(text.len(), |(index, _)| index);
        if let Some(comma) = text[5..header_end.max(5)].find(',').map(|at| at + 5) {
            if text[5..comma].to_lowercase().ends_with(";base64") {
                return Some(decoded_size(utf16_len(&text[comma + 1..]), text));
            }
        }
    }
    // Fewer bytes than that means fewer characters. Text that isn't ASCII isn't base64, so the
    // checks below say no.
    if bytes.len() < BINARY_MIN {
        return None;
    }
    if !is_base64(&bytes[..bytes.len().min(BINARY_HEAD)]) {
        return None;
    }
    if bytes.len() > BINARY_HEAD && !is_base64(&bytes[bytes.len() - BINARY_TAIL..]) {
        return None;
    }
    Some(decoded_size(utf16_len(text), text))
}

/// Charges `cost` to the scan budget. Past it, false: the budget is used up, and the value marked shortened.
pub(crate) fn spend(state: &mut ShapeState, cost: usize) -> bool {
    if state.remaining < cost {
        state.remaining = 0;
        state.shortened = true;
        return false;
    }
    state.remaining -= cost;
    true
}

/// The byte index where the last `count` characters of `text` start.
fn tail_start(text: &str, count: usize) -> usize {
    text.char_indices()
        .rev()
        .take(count)
        .last()
        .map_or(text.len(), |(index, _)| index)
}

/// Whether a string ends with a marker this SDK writes.
pub(crate) fn is_marked(text: &str) -> bool {
    MARKER.is_match(&text[tail_start(text, 32)..])
}

/// Shortens one string to `limit` code points. Within the limit it stays a string (charged its
/// length); past it, a `Cut` keeping the first `limit` code points plus `LOOKAHEAD` more for
/// redaction, marked `…[+N chars]` with the exact count of the rest when that fits in the budget,
/// which is charged for counting it. When it doesn't fit, counting it would use the budget up, so
/// it is: the string ends `…[cut]`, and so does every string after it. Once the budget runs out,
/// strings are cut where it ran out.
pub(crate) fn shape_string(text: &str, limit: usize, state: &mut ShapeState) -> Shaped {
    if text.is_empty() {
        return Shaped::String(String::new());
    }
    if state.remaining == 0 {
        state.shortened = true;
        return Shaped::Cut(Cut {
            window: String::new(),
            cut: 0,
            mark: CUT_MARK.into(),
            limit,
            scanned: false,
        });
    }
    if let Some(bytes) = binary_size(text) {
        state.shortened = true;
        return Shaped::Binary(bytes);
    }

    // After before_send: a string this SDK cut, as the hook got it, is kept as it is where its
    // limit is no tighter than the one it was cut at. Redaction may have made it longer than that
    // limit (`a@b.co` is shorter than `[EMAIL_1]`). Any other marked string is shortened as usual,
    // adding the count in its marker to what is cut now.
    let mut text = text;
    let mut earlier: Option<&str> = None;
    if state.after_hook {
        let tail = tail_start(text, 32);
        if let Some(marked) = MARKER.find(&text[tail..]) {
            let body = &text[..tail + marked.start()];
            let cut_at = state.cuts.and_then(|cuts| cuts.get(text)).copied();
            if let Some(cut_at) = cut_at.filter(|cut_at| *cut_at <= limit) {
                let _ = cut_at;
                state.shortened = true;
                let (end, passed) = advance(body, 0, state.remaining);
                if end == body.len() {
                    state.remaining -= passed;
                    return Shaped::Cut(Cut {
                        window: body.into(),
                        cut: body.len(),
                        mark: marked.as_str().into(),
                        limit,
                        scanned: true,
                    });
                }
                state.remaining = 0;
                return Shaped::Cut(Cut {
                    window: body[..end].into(),
                    cut: end,
                    mark: CUT_MARK.into(),
                    limit,
                    scanned: true,
                });
            }
            earlier = Some(&text[tail + marked.start()..]);
            text = body;
        } else if let Some(before) = state.cuts.and_then(|cuts| cuts.get(text)).copied() {
            // A string whose secret values were replaced before the hook, as the hook got it, is
            // kept whole where its length before that fits: placeholders may have made it longer.
            if before <= limit && before <= state.remaining {
                state.remaining -= before;
                return Shaped::String(text.into());
            }
        }
    }

    let readable = limit.min(state.remaining);
    let (cut, kept) = advance(text, 0, readable);
    if cut == text.len() {
        state.remaining -= kept;
        return match earlier {
            None => Shaped::String(text.into()),
            Some(mark) => Shaped::Cut(Cut {
                window: text.into(),
                cut,
                mark: mark.into(),
                limit,
                scanned: false,
            }),
        };
    }
    state.shortened = true;
    // Redaction reads LOOKAHEAD past every cut, also where the budget runs out, so a token running
    // into the cut is matched whole. The budget runs out once, so that reads at most LOOKAHEAD more.
    let (end, ahead) = advance(text, cut, LOOKAHEAD);
    if kept < limit || kept + ahead > state.remaining {
        // The budget ran out inside this string, or inside what redaction reads past the cut.
        state.remaining = 0;
        return Shaped::Cut(Cut {
            window: text[..end].into(),
            cut,
            mark: CUT_MARK.into(),
            limit,
            scanned: false,
        });
    }
    state.remaining -= kept + ahead;
    let mut rest = Some(ahead as u64);
    if end < text.len() {
        let (last, counted) = advance(text, end, state.remaining + 1);
        if last == text.len() && counted <= state.remaining {
            state.remaining -= counted;
            rest = rest.map(|rest| rest + counted as u64);
        } else {
            state.remaining = 0;
            rest = None;
        }
    }
    Shaped::Cut(Cut {
        window: text[..end].into(),
        cut,
        mark: combine(rest, earlier),
        limit,
        scanned: false,
    })
}

/// The marker for `rest` characters cut now (`None`: not counted), plus a marker from before.
fn combine(rest: Option<u64>, earlier: Option<&str>) -> String {
    let Some(rest) = rest else {
        return CUT_MARK.into();
    };
    let Some(earlier) = earlier else {
        return format!("…[+{rest} chars]");
    };
    let before = MARKER
        .captures(earlier)
        .and_then(|found| found.get(1))
        .and_then(|count| count.as_str().parse::<u64>().ok());
    match before {
        // Added as JavaScript adds them, as doubles: a count a hook wrote can be past 2^53.
        Some(before) => format!(
            "…[+{} chars]",
            crate::json::js_f64(rest as f64 + before as f64)
        ),
        None => CUT_MARK.into(),
    }
}

/// Shaped JSON as plain JSON, without redaction: a value's identity under a secret key name.
pub(crate) fn plain(value: &Shaped) -> Value {
    match value {
        Shaped::Null => Value::Null,
        Shaped::Bool(value) => Value::Bool(*value),
        Shaped::Number(number) => Value::Number(number.clone()),
        Shaped::String(text) => Value::String(text.clone()),
        Shaped::Cut(cut) => Value::String(format!("{}{}", &cut.window[..cut.cut], cut.mark)),
        Shaped::Binary(bytes) => binary_marker(*bytes),
        Shaped::TooDeep => depth_marker(),
        Shaped::Secret(_) => Value::String("[SECRET]".into()),
        Shaped::Array(items) => Value::Array(items.iter().map(plain).collect()),
        Shaped::Object(entries) => Value::Object(
            entries
                .iter()
                .map(|(key, item)| (key.clone(), plain(item)))
                .collect(),
        ),
    }
}

pub(crate) fn binary_marker(bytes: u64) -> Value {
    let mut out = Map::new();
    out.insert("omitted".into(), "binary".into());
    out.insert("bytes".into(), bytes.into());
    Value::Object(out)
}

pub(crate) fn depth_marker() -> Value {
    let mut out = Map::new();
    out.insert("omitted".into(), "depth".into());
    Value::Object(out)
}

/// A value under a secret key name, read within the limits but never shortened: its identity is
/// the raw value. A string is read up to the field's limit; anything else is converted, and counts
/// only if nothing in it was cut, left out or turned into a size. Values that can't hold a secret
/// (null, booleans, values empty as given) and placeholders stay as they are.
fn secret_value(value: &Value, limit: usize, state: &mut ShapeState, level: usize) -> Shaped {
    match value {
        Value::Null => return Shaped::Null,
        Value::Bool(value) => return Shaped::Bool(*value),
        Value::String(text) => return secret_string(text, limit, state),
        _ => {}
    }
    let (shortened, lossy, secret_key) = (state.shortened, state.lossy, state.secret_key);
    state.shortened = false;
    state.lossy = false;
    // Read as a whole: secret key names inside it don't matter.
    state.secret_key = false;
    let converted = convert(value, limit, state, level);
    let exact = !state.shortened && !state.lossy;
    state.shortened = shortened;
    state.lossy = lossy;
    state.secret_key = secret_key;
    // Empty as it was given, not made empty by the budget or by leaving out what JSON can't hold.
    let empty = match &converted {
        Shaped::Array(items) => items.is_empty(),
        Shaped::Object(entries) => entries.is_empty(),
        _ => false,
    };
    if exact && empty {
        return converted;
    }
    Shaped::Secret(exact.then(|| stringify(&plain(&converted))))
}

/// A string under a secret key name: its raw value, read up to `limit`, or a number of its own.
pub(crate) fn secret_string(value: &str, limit: usize, state: &mut ShapeState) -> Shaped {
    if value.is_empty() || is_whole_placeholder(value) {
        return Shaped::String(value.into());
    }
    let (end, passed) = advance(value, 0, limit.min(state.remaining));
    state.remaining -= passed;
    Shaped::Secret((end == value.len()).then(|| value.to_owned()))
}

/// Converts a value from the app into shaped JSON, without reading more than the limits allow:
/// lists and objects keep their first 100 entries, nesting stops at 10 levels, and strings are
/// shortened to `limit`. Objects are read in JavaScript's order (see `js_entries`). Under a secret key name, when
/// `state.secret_key` is set, a value becomes a `Secret`. A `__proto__` key is removed and the
/// state marked lossy, as the other SDKs do.
pub(crate) fn shape_json(value: &Value, limit: usize, state: &mut ShapeState) -> Shaped {
    convert(value, limit, state, 0)
}

fn convert(value: &Value, limit: usize, state: &mut ShapeState, level: usize) -> Shaped {
    match value {
        Value::Null => Shaped::Null,
        Value::Bool(value) => Shaped::Bool(*value),
        Value::Number(number) => Shaped::Number(js_held(number)),
        Value::String(text) => shape_string(text, limit, state),
        Value::Array(items) => {
            if level >= MAX_LEVELS {
                state.shortened = true;
                return Shaped::TooDeep;
            }
            if items.len() > MAX_ENTRIES {
                state.shortened = true;
            }
            let mut out = Vec::with_capacity(items.len().min(MAX_ENTRIES));
            for item in items.iter().take(MAX_ENTRIES) {
                if !spend(state, 1) {
                    break;
                }
                out.push(convert(item, limit, state, level + 1));
            }
            Shaped::Array(out)
        }
        Value::Object(entries) => convert_entries(entries, limit, state, level),
    }
}

/// An object's entries, as `convert` reads an object: for one held as a map, such as a call's
/// arguments, which is read where it is.
pub(crate) fn shape_object(
    entries: &Map<String, Value>,
    limit: usize,
    state: &mut ShapeState,
) -> Shaped {
    convert_entries(entries, limit, state, 0)
}

fn convert_entries(
    entries: &Map<String, Value>,
    limit: usize,
    state: &mut ShapeState,
    level: usize,
) -> Shaped {
    if level >= MAX_LEVELS {
        state.shortened = true;
        return Shaped::TooDeep;
    }
    let mut out = Vec::with_capacity(entries.len().min(MAX_ENTRIES));
    // One more than is kept: the entry that says there were more.
    for (key, item) in js_entries(entries, MAX_ENTRIES + 1) {
        if out.len() >= MAX_ENTRIES {
            state.shortened = true;
            break;
        }
        // A key is read whole, never cut: it costs its length, and one that doesn't fit ends the
        // object.
        let cost = 1 + advance(key, 0, state.remaining).1;
        if !spend(state, cost) {
            break;
        }
        if key == "__proto__" {
            state.lossy = true;
            continue;
        }
        let converted = if state.hides(key) {
            secret_value(item, limit, state, level + 1)
        } else {
            convert(item, limit, state, level + 1)
        };
        out.push((key.clone(), converted));
    }
    Shaped::Object(out)
}

/// About what a shaped value holds in memory, in bytes.
pub(crate) fn weight(value: &Shaped) -> usize {
    match value {
        Shaped::String(text) => text.len() + 24,
        Shaped::Cut(cut) => cut.window.len() + cut.mark.len() + 48,
        Shaped::Secret(identity) => identity.as_ref().map_or(0, String::len) + 24,
        Shaped::Array(items) => items.iter().map(weight).sum::<usize>() + 24,
        Shaped::Object(entries) => {
            entries
                .iter()
                .map(|(key, item)| key.len() + 24 + weight(item))
                .sum::<usize>()
                + 24
        }
        _ => 24,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn string(value: Shaped) -> String {
        match value {
            Shaped::String(text) => text,
            Shaped::Cut(cut) => format!("{}{}", &cut.window[..cut.cut], cut.mark),
            other => panic!("not a string: {other:?}"),
        }
    }

    #[test]
    fn a_string_is_cut_at_its_limit_in_code_points() {
        let mut state = ShapeState::new(None);
        let text = "é".repeat(4_010);
        assert_eq!(
            string(shape_string(&text, 4_000, &mut state)),
            format!("{}…[+10 chars]", "é".repeat(4_000))
        );
        assert!(state.shortened);
        assert_eq!(state.remaining, SCAN_BUDGET - 4_010);
    }

    #[test]
    fn a_string_past_the_budget_is_cut_without_a_count() {
        let mut state = ShapeState::new(None);
        state.remaining = 10;
        assert_eq!(
            string(shape_string(&"a".repeat(30), 4_000, &mut state)),
            format!("{}…[cut]", "a".repeat(10))
        );
        assert_eq!(state.remaining, 0);
        assert_eq!(string(shape_string("more", 4_000, &mut state)), "…[cut]");
    }

    #[test]
    fn a_file_becomes_its_size() {
        assert_eq!(binary_size("data:image/png;base64,AAAA"), Some(3));
        assert_eq!(binary_size(&"QUJD".repeat(250)), Some(750));
        assert_eq!(binary_size(&format!("{}==", "QUJD".repeat(250))), Some(750));
        assert_eq!(binary_size(&"lorem ipsum ".repeat(100)), None);
        assert_eq!(binary_size("short"), None);
        // The header is read 1,024 UTF-16 units far: 507 emoji put its comma past that.
        assert_eq!(
            binary_size(&format!("data:{};base64,QQ==", "😀".repeat(507))),
            None
        );
        assert_eq!(
            binary_size(&format!("data:{};base64,QQ==", "😀".repeat(500))),
            Some(1)
        );
    }

    #[test]
    fn an_object_is_read_in_javascripts_order() {
        // `2` and `10` first, as JavaScript holds the object and as the other SDKs read it: so
        // the first 100 entries kept, and the numbers placeholders get, are theirs.
        let value: Value =
            serde_json::from_str(r#"{"b": 1, "10": 2, "2": {"z": 1, "0": 2}}"#).unwrap();
        let mut state = ShapeState::new(None);
        assert_eq!(
            stringify(&plain(&shape_json(&value, 100, &mut state))),
            r#"{"2":{"0":2,"z":1},"10":2,"b":1}"#
        );
        // Of more than 100 entries, the 100 kept are the first in that order.
        let mut many = Map::new();
        for index in 0..150 {
            many.insert(format!("k{index}"), index.into());
        }
        many.insert("5".into(), "kept".into());
        let mut state = ShapeState::new(None);
        let Shaped::Object(kept) = shape_object(&many, 100, &mut state) else {
            panic!("an object")
        };
        assert_eq!(kept.len(), 100);
        assert_eq!(kept[0].0, "5");
        assert_eq!(kept[99].0, "k98");
    }

    #[test]
    fn lists_objects_and_nesting_stop_at_their_limits() {
        let mut state = ShapeState::new(None);
        let list = Value::Array((0..150).map(Value::from).collect());
        let Shaped::Array(items) = shape_json(&list, 100, &mut state) else {
            panic!("not a list")
        };
        assert_eq!(items.len(), 100);
        assert!(state.shortened);

        let mut deep = Value::from(1);
        for _ in 0..12 {
            deep = Value::Array(vec![deep]);
        }
        let mut state = ShapeState::new(None);
        let text = stringify(&plain(&shape_json(&deep, 100, &mut state)));
        assert_eq!(
            text,
            format!(
                "{}{{\"omitted\":\"depth\"}}{}",
                "[".repeat(10),
                "]".repeat(10)
            )
        );
    }

    #[test]
    fn a_value_under_a_secret_key_name_is_replaced_whole() {
        let keys = KeyNames(vec!["password".into()]);
        let mut state = ShapeState::new(Some(&keys));
        state.secret_key = true;
        let value: Value = serde_json::from_str(
            r#"{"dbPassword": {"a": 1.0}, "password": "", "kept": 1, "__proto__": 2}"#,
        )
        .unwrap();
        let shaped = shape_json(&value, 100, &mut state);
        assert_eq!(
            shaped,
            Shaped::Object(vec![
                (
                    "dbPassword".into(),
                    Shaped::Secret(Some("{\"a\":1}".into()))
                ),
                ("password".into(), Shaped::String(String::new())),
                ("kept".into(), Shaped::Number(1.into())),
            ])
        );
        assert!(state.lossy);
    }
}
