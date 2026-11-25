//! For the tests: JSON read as JavaScript reads it. The shared cases
//! (`packages/contract/cases`) are written for `JSON.parse`: they hold lone surrogates and numbers
//! too large for a double, which `serde_json` doesn't read. An object's entries stay in the order
//! the file writes them: putting them in JavaScript's order is the SDK's own work, which the cases
//! then check.

use serde_json::{Map, Value};

/// A JSON value as JavaScript holds it: strings in UTF-16 code units, numbers as written.
#[derive(Debug, Clone, PartialEq)]
pub(crate) enum Js {
    Null,
    Bool(bool),
    /// The literal, as the file writes it.
    Number(String),
    String(Vec<u16>),
    Array(Vec<Js>),
    Object(Vec<(Vec<u16>, Js)>),
}

pub(crate) fn units(text: &str) -> Vec<u16> {
    text.encode_utf16().collect()
}

pub(crate) fn text(units: &[u16]) -> String {
    String::from_utf16_lossy(units)
}

impl Js {
    pub(crate) fn get(&self, key: &str) -> Option<&Js> {
        match self {
            Js::Object(entries) => entries
                .iter()
                .find(|(name, _)| *name == units(key))
                .map(|(_, value)| value),
            _ => None,
        }
    }

    pub(crate) fn as_text(&self) -> Option<String> {
        match self {
            Js::String(value) => Some(text(value)),
            _ => None,
        }
    }

    pub(crate) fn as_f64(&self) -> Option<f64> {
        match self {
            Js::Number(literal) => literal.parse().ok(),
            _ => None,
        }
    }

    pub(crate) fn string(value: &str) -> Js {
        Js::String(units(value))
    }
}

/// Reads JSON as `JSON.parse` does.
pub(crate) fn parse(source: &str) -> Js {
    let mut reader = Reader {
        bytes: source.as_bytes(),
        at: 0,
    };
    let value = reader.value();
    reader.space();
    assert_eq!(reader.at, reader.bytes.len(), "text after the JSON value");
    value
}

struct Reader<'a> {
    bytes: &'a [u8],
    at: usize,
}

impl Reader<'_> {
    fn space(&mut self) {
        while self.at < self.bytes.len()
            && matches!(self.bytes[self.at], b' ' | b'\t' | b'\n' | b'\r')
        {
            self.at += 1;
        }
    }

    fn expect(&mut self, literal: &str) {
        assert!(
            self.bytes[self.at..].starts_with(literal.as_bytes()),
            "expected {literal} at {}",
            self.at
        );
        self.at += literal.len();
    }

    fn value(&mut self) -> Js {
        self.space();
        match self.bytes[self.at] {
            b'n' => {
                self.expect("null");
                Js::Null
            }
            b't' => {
                self.expect("true");
                Js::Bool(true)
            }
            b'f' => {
                self.expect("false");
                Js::Bool(false)
            }
            b'"' => Js::String(self.string()),
            b'[' => {
                self.at += 1;
                let mut items = Vec::new();
                loop {
                    self.space();
                    if self.bytes[self.at] == b']' {
                        self.at += 1;
                        return Js::Array(items);
                    }
                    if !items.is_empty() {
                        self.expect(",");
                    }
                    items.push(self.value());
                }
            }
            b'{' => {
                self.at += 1;
                let mut entries: Vec<(Vec<u16>, Js)> = Vec::new();
                loop {
                    self.space();
                    if self.bytes[self.at] == b'}' {
                        self.at += 1;
                        return Js::Object(entries);
                    }
                    if !entries.is_empty() {
                        self.expect(",");
                        self.space();
                    }
                    let key = self.string();
                    self.space();
                    self.expect(":");
                    let value = self.value();
                    // A key written twice keeps its first place and its last value.
                    match entries.iter_mut().find(|(name, _)| *name == key) {
                        Some(entry) => entry.1 = value,
                        None => entries.push((key, value)),
                    }
                }
            }
            _ => {
                let start = self.at;
                while self.at < self.bytes.len()
                    && matches!(
                        self.bytes[self.at],
                        b'-' | b'+' | b'.' | b'e' | b'E' | b'0'..=b'9'
                    )
                {
                    self.at += 1;
                }
                assert!(self.at > start, "not JSON at {start}");
                Js::Number(String::from_utf8_lossy(&self.bytes[start..self.at]).into_owned())
            }
        }
    }

    fn string(&mut self) -> Vec<u16> {
        self.expect("\"");
        let mut out = Vec::new();
        loop {
            let start = self.at;
            while !matches!(self.bytes[self.at], b'"' | b'\\') {
                self.at += 1;
            }
            out.extend(
                std::str::from_utf8(&self.bytes[start..self.at])
                    .expect("UTF-8")
                    .encode_utf16(),
            );
            if self.bytes[self.at] == b'"' {
                self.at += 1;
                return out;
            }
            let escape = self.bytes[self.at + 1];
            self.at += 2;
            match escape {
                b'u' => {
                    let digits =
                        std::str::from_utf8(&self.bytes[self.at..self.at + 4]).expect("hex");
                    out.push(u16::from_str_radix(digits, 16).expect("hex"));
                    self.at += 4;
                }
                b'n' => out.push(0x0a),
                b'r' => out.push(0x0d),
                b't' => out.push(0x09),
                b'b' => out.push(0x08),
                b'f' => out.push(0x0c),
                other => out.push(u16::from(other)),
            }
        }
    }
}

/// Expands the compact forms `cases/README.md` describes.
pub(crate) fn expand(value: &Js) -> Js {
    match value {
        Js::Array(items) => Js::Array(items.iter().map(expand).collect()),
        Js::Object(entries) => {
            let count = |value: &Js| value.as_f64().expect("a count") as usize;
            if let Some(length) = value.get("$prose") {
                let length = count(length);
                let prose = "lorem ipsum ".repeat(length.div_ceil(12));
                return Js::string(&prose[..length]);
            }
            if let Some(Js::Array(parts)) = value.get("$repeat") {
                let Js::String(repeated) = &parts[0] else {
                    panic!("$repeat takes a string")
                };
                return Js::String(repeated.repeat(count(&parts[1])));
            }
            if let Some(Js::Array(parts)) = value.get("$concat") {
                let mut joined = Vec::new();
                for part in parts {
                    let Js::String(part) = expand(part) else {
                        panic!("$concat takes strings")
                    };
                    joined.extend(part);
                }
                return Js::String(joined);
            }
            if let Some(length) = value.get("$range") {
                return Js::Array(
                    (0..count(length))
                        .map(|index| Js::Number(index.to_string()))
                        .collect(),
                );
            }
            if let Some(length) = value.get("$keys") {
                return Js::Object(
                    (0..count(length))
                        .map(|index| (units(&format!("k{index}")), Js::Number(index.to_string())))
                        .collect(),
                );
            }
            Js::Object(
                entries
                    .iter()
                    .map(|(key, item)| (key.clone(), expand(item)))
                    .collect(),
            )
        }
        other => other.clone(),
    }
}

/// What a Rust app can hold of a value JavaScript reads.
#[derive(Debug, Default)]
pub(crate) struct Held {
    /// A lone surrogate became U+FFFD: Rust strings can't hold one.
    pub lone_surrogate: bool,
    /// A number too large for a double: `serde_json` refuses it, where JavaScript reads infinity.
    pub not_a_number: bool,
}

fn has_lone_surrogate(units: &[u16]) -> bool {
    char::decode_utf16(units.iter().copied()).any(|decoded| decoded.is_err())
}

/// The value as `serde_json` holds it, in the same order.
pub(crate) fn to_value(value: &Js, held: &mut Held) -> Value {
    match value {
        Js::Null => Value::Null,
        Js::Bool(value) => Value::Bool(*value),
        // One too large for a double is no number here, whether or not this build's
        // `serde_json` can hold it (it can with `arbitrary_precision`).
        Js::Number(literal) => match serde_json::from_str::<Value>(literal) {
            Ok(number) if literal.parse::<f64>().is_ok_and(f64::is_finite) => number,
            _ => {
                held.not_a_number = true;
                Value::Null
            }
        },
        Js::String(units) => {
            held.lone_surrogate |= has_lone_surrogate(units);
            Value::String(text(units))
        }
        Js::Array(items) => Value::Array(items.iter().map(|item| to_value(item, held)).collect()),
        Js::Object(entries) => {
            let mut out = Map::new();
            for (key, item) in entries {
                held.lone_surrogate |= has_lone_surrogate(key);
                out.insert(text(key), to_value(item, held));
            }
            Value::Object(out)
        }
    }
}

/// Where two values differ, as JavaScript's deep equality tells: objects compared regardless of
/// key order, numbers as doubles. `None`: they are equal.
pub(crate) fn difference(actual: &Js, expected: &Js, path: &str) -> Option<String> {
    let show = |value: &Js| {
        let shown = match value {
            Js::String(units) => format!("{:?}", text(units)),
            other => format!("{other:?}"),
        };
        shown.chars().take(160).collect::<String>()
    };
    let differs = || Some(format!("{path}: {} != {}", show(actual), show(expected)));
    match (actual, expected) {
        (Js::Null, Js::Null) => None,
        (Js::Bool(a), Js::Bool(b)) if a == b => None,
        (Js::Number(_), Js::Number(_)) => {
            let (a, b) = (
                actual.as_f64().unwrap_or(f64::NAN),
                expected.as_f64().unwrap_or(f64::NAN),
            );
            if a == b && a.is_sign_negative() == b.is_sign_negative() {
                None
            } else {
                differs()
            }
        }
        (Js::String(a), Js::String(b)) if a == b => None,
        (Js::Array(a), Js::Array(b)) => {
            if a.len() != b.len() {
                return Some(format!("{path}: {} items != {} items", a.len(), b.len()));
            }
            a.iter()
                .zip(b)
                .enumerate()
                .find_map(|(index, (left, right))| {
                    difference(left, right, &format!("{path}[{index}]"))
                })
        }
        (Js::Object(a), Js::Object(b)) => {
            let names = |entries: &[(Vec<u16>, Js)]| {
                let mut names: Vec<String> = entries.iter().map(|(key, _)| text(key)).collect();
                names.sort();
                names
            };
            if names(a) != names(b) {
                return Some(
                    format!("{path}: keys {:?} != {:?}", names(a), names(b))
                        .chars()
                        .take(400)
                        .collect(),
                );
            }
            b.iter().find_map(|(key, right)| {
                let left = a
                    .iter()
                    .find(|(name, _)| name == key)
                    .map(|(_, value)| value)?;
                difference(left, right, &format!("{path}.{}", text(key)))
            })
        }
        _ => differs(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn json_is_read_as_javascript_reads_its_strings_and_numbers() {
        let value =
            parse(r#"{"b": 1, "10": "x", "2": [true, null], "a": "😀\ud800", "big": 1e400}"#);
        let Js::Object(entries) = &value else {
            panic!("not an object")
        };
        let keys: Vec<String> = entries.iter().map(|(key, _)| text(key)).collect();
        // In the order written: the SDK puts them in JavaScript's.
        assert_eq!(keys, ["b", "10", "2", "a", "big"]);
        assert_eq!(
            value.get("a"),
            Some(&Js::String(vec![0xd83d, 0xde00, 0xd800]))
        );
        let mut held = Held::default();
        let held_value = to_value(&value, &mut held);
        assert!(held.lone_surrogate && held.not_a_number);
        assert_eq!(held_value["a"], "😀\u{fffd}");
        assert_eq!(
            difference(
                &parse("{\"a\":1.0,\"b\":[2]}"),
                &parse("{\"b\":[2],\"a\":1}"),
                "$"
            ),
            None
        );
        assert!(difference(&parse("[0]"), &parse("[-0]"), "$").is_some());
    }

    #[test]
    fn compact_forms_expand() {
        let value = expand(&parse(
            r#"[{"$prose": 14}, {"$repeat": ["ab", 3]}, {"$concat": ["x", {"$repeat": ["y", 2]}]}, {"$range": 3}, {"$keys": 2}]"#,
        ));
        assert_eq!(
            value,
            parse(r#"["lorem ipsum lo", "ababab", "xyy", [0, 1, 2], {"k0": 0, "k1": 1}]"#)
        );
    }
}
