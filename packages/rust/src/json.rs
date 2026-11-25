//! JSON as JavaScript holds and writes it, so an event's bytes and size are those the TypeScript
//! SDK gives for the same values: compact, numbers as JavaScript writes them, and an object's
//! entries in JavaScript's order.

use std::cmp::Ordering;
use std::collections::BinaryHeap;
use std::io;

use serde::Serialize;
use serde_json::ser::Formatter;
use serde_json::{Map, Number, Value};

/// A number as JavaScript's `Number.prototype.toString` writes it: shortest digits, no `.0`,
/// an exponent only below 1e-6 or from 1e21, and `-0` as `0`.
pub(crate) fn js_f64(value: f64) -> String {
    if value == 0.0 || !value.is_finite() {
        // JSON has no infinity or NaN; serde_json never holds one. Both zeros are written `0`.
        return if value == 0.0 {
            "0".into()
        } else {
            "null".into()
        };
    }
    // Rust's `{:e}` gives the shortest digits that read back as the same double, as JavaScript does.
    let formatted = format!("{:e}", value.abs());
    let (mantissa, exponent) = formatted.split_once('e').unwrap_or((&formatted, "0"));
    let mut digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    let exponent: i32 = exponent.parse().unwrap_or(0);
    // Where the double lies exactly halfway between two candidates of that length, JavaScript
    // takes the even one and Rust the upper one: `1000000000000000.25` is `…000.2` there.
    let exact = format!("{:.60e}", value.abs());
    let exact: Vec<u8> = exact
        .bytes()
        .take_while(|byte| *byte != b'e')
        .filter(u8::is_ascii_digit)
        .collect();
    let length = digits.len();
    let halfway =
        exact.get(length) == Some(&b'5') && exact[length + 1..].iter().all(|digit| *digit == b'0');
    if halfway
        && exact[..length] != *digits.as_bytes()
        && (exact[length - 1] - b'0').is_multiple_of(2)
    {
        // The lower candidate, which is the exact digits cut there, is the even one.
        digits = String::from_utf8_lossy(&exact[..length]).into_owned();
    }
    let k = digits.len() as i32;
    // The decimal point comes after `n` digits.
    let n = exponent + 1;
    let mut out = String::with_capacity(digits.len() + 8);
    if value < 0.0 {
        out.push('-');
    }
    if k <= n && n <= 21 {
        out.push_str(&digits);
        out.extend(std::iter::repeat_n('0', (n - k) as usize));
    } else if 0 < n && n <= 21 {
        out.push_str(&digits[..n as usize]);
        out.push('.');
        out.push_str(&digits[n as usize..]);
    } else if -6 < n && n <= 0 {
        out.push_str("0.");
        out.extend(std::iter::repeat_n('0', (-n) as usize));
        out.push_str(&digits);
    } else {
        out.push_str(&digits[..1]);
        if k > 1 {
            out.push('.');
            out.push_str(&digits[1..]);
        }
        out.push('e');
        out.push(if n - 1 < 0 { '-' } else { '+' });
        out.push_str(&(n - 1).abs().to_string());
    }
    out
}

/// The largest integer a JavaScript number holds exactly, with every one below it: 2^53 - 1.
const MAX_SAFE_INTEGER: u64 = 9_007_199_254_740_991;

/// An integer as JavaScript writes it. JavaScript reads every number as a double, so one past
/// 2^53 is the nearest double there (`9007199254740993` is `9007199254740992`): in the TypeScript
/// and Python SDKs, and in the ingestion, which reads the event with JavaScript and measures it
/// as it writes it back. It is written that way here too, so the event sent is the one stored.
fn js_u64(value: u64) -> String {
    if value > MAX_SAFE_INTEGER {
        js_f64(value as f64)
    } else {
        value.to_string()
    }
}

fn js_i64(value: i64) -> String {
    if value.unsigned_abs() > MAX_SAFE_INTEGER {
        js_f64(value as f64)
    } else {
        value.to_string()
    }
}

/// A JSON number as JavaScript writes it.
pub(crate) fn js_number(number: &Number) -> String {
    if let Some(value) = number.as_u64() {
        js_u64(value)
    } else if let Some(value) = number.as_i64() {
        js_i64(value)
    } else {
        js_f64(number.as_f64().unwrap_or(0.0))
    }
}

/// A number as an event holds it: an integer past 2^53 is the double JavaScript reads, so what
/// `before_send` is given is what is sent, as it is in the TypeScript SDK.
pub(crate) fn js_held(number: &Number) -> Number {
    const TWO_64: f64 = 18_446_744_073_709_551_616.0;
    match (number.as_u64(), number.as_i64()) {
        (Some(value), _) if value > MAX_SAFE_INTEGER => {
            let rounded = value as f64;
            if rounded < TWO_64 {
                Number::from(rounded as u64)
            } else {
                Number::from_f64(rounded).unwrap_or_else(|| number.clone())
            }
        }
        (None, Some(value)) if value.unsigned_abs() > MAX_SAFE_INTEGER => {
            Number::from((value as f64) as i64)
        }
        _ => number.clone(),
    }
}

/// A number's text as JavaScript writes the number: for a build with `serde_json`'s
/// `arbitrary_precision` feature, where a number is held as it was written.
fn js_literal(text: &str) -> String {
    if let Ok(value) = text.parse::<u64>() {
        js_u64(value)
    } else if let Ok(value) = text.parse::<i64>() {
        js_i64(value)
    } else {
        text.parse::<f64>().map_or_else(|_| "null".into(), js_f64)
    }
}

struct JsFormatter;

impl Formatter for JsFormatter {
    fn write_f64<W: ?Sized + io::Write>(&mut self, writer: &mut W, value: f64) -> io::Result<()> {
        writer.write_all(js_f64(value).as_bytes())
    }

    fn write_f32<W: ?Sized + io::Write>(&mut self, writer: &mut W, value: f32) -> io::Result<()> {
        writer.write_all(js_f64(f64::from(value)).as_bytes())
    }

    fn write_u64<W: ?Sized + io::Write>(&mut self, writer: &mut W, value: u64) -> io::Result<()> {
        writer.write_all(js_u64(value).as_bytes())
    }

    fn write_i64<W: ?Sized + io::Write>(&mut self, writer: &mut W, value: i64) -> io::Result<()> {
        writer.write_all(js_i64(value).as_bytes())
    }

    /// Where numbers go in a build that has `serde_json`'s `arbitrary_precision` feature on,
    /// which an app can turn on for reasons of its own: they are written as JavaScript would all
    /// the same.
    fn write_number_str<W: ?Sized + io::Write>(
        &mut self,
        writer: &mut W,
        value: &str,
    ) -> io::Result<()> {
        writer.write_all(js_literal(value).as_bytes())
    }
}

/// The number a key is, when it is one JavaScript orders first: an array index, which is an
/// integer from 0 to 2^32 - 2 written without a sign or leading zeros.
fn array_index(key: &str) -> Option<u32> {
    let digits = key.as_bytes();
    if digits.is_empty() || digits.len() > 10 || (digits.len() > 1 && digits[0] == b'0') {
        return None;
    }
    if !digits.iter().all(u8::is_ascii_digit) {
        return None;
    }
    key.parse::<u32>().ok().filter(|index| *index < u32::MAX)
}

/// An entry whose key is an array index, ordered by that index.
struct Indexed<'a>(u32, &'a String, &'a Value);

impl PartialEq for Indexed<'_> {
    fn eq(&self, other: &Self) -> bool {
        self.0 == other.0
    }
}

impl Eq for Indexed<'_> {}

impl PartialOrd for Indexed<'_> {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for Indexed<'_> {
    fn cmp(&self, other: &Self) -> Ordering {
        self.0.cmp(&other.0)
    }
}

/// An object's entries in JavaScript's order, as `JSON.parse` holds them and as every SDK reads
/// them: the keys that are array indexes (`"0"`, `"7"`, `"42"`) first, ascending, then the others
/// in the order they came in. So placeholders are numbered alike everywhere, and of an object of
/// more than 100 entries the 100 kept are the same ones.
///
/// `most` is how many entries the caller reads at most. Every key is looked at once, since an
/// index can be anywhere, but only the `most` lowest indexes are kept and sorted: an object of a
/// million entries costs a pass over its keys, no more, to read its first hundred.
pub(crate) fn js_entries(
    entries: &Map<String, Value>,
    most: usize,
) -> impl Iterator<Item = (&String, &Value)> {
    let mut lowest: BinaryHeap<Indexed> = BinaryHeap::new();
    let mut reordered = false;
    for (key, value) in entries {
        if let Some(index) = array_index(key) {
            reordered = true;
            if lowest.len() < most {
                lowest.push(Indexed(index, key, value));
            } else if lowest.peek().is_some_and(|highest| index < highest.0) {
                lowest.pop();
                lowest.push(Indexed(index, key, value));
            }
        }
    }
    // Most objects have none: they are read as they are.
    lowest
        .into_sorted_vec()
        .into_iter()
        .map(|Indexed(_, key, value)| (key, value))
        .chain(
            entries
                .iter()
                .filter(move |(key, _)| !reordered || array_index(key).is_none()),
        )
}

/// A map with its entries in JavaScript's order: what `{ ...a, ...b }` gives there.
pub(crate) fn js_ordered(entries: Map<String, Value>) -> Map<String, Value> {
    js_entries(&entries, usize::MAX)
        .map(|(key, value)| (key.clone(), value.clone()))
        .collect()
}

/// Counts what is written to it.
struct Counter(usize);

impl io::Write for Counter {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.0 += bytes.len();
        Ok(bytes.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

/// The size of a value's JSON as `stringify` writes it, in UTF-8 bytes, without writing it out:
/// what the event limit counts, and what the ingestion counts as it writes the event back.
pub(crate) fn json_size<T: Serialize + ?Sized>(value: &T) -> usize {
    let mut counter = Counter(0);
    let mut serializer = serde_json::Serializer::with_formatter(&mut counter, JsFormatter);
    match value.serialize(&mut serializer) {
        Ok(()) => counter.0,
        Err(_) => usize::MAX,
    }
}

/// Compact JSON, as `JSON.stringify` writes the same value.
pub(crate) fn stringify<T: Serialize + ?Sized>(value: &T) -> String {
    let mut out = Vec::with_capacity(128);
    let mut serializer = serde_json::Serializer::with_formatter(&mut out, JsFormatter);
    // Writing to a Vec can't fail, and the values written are JSON already.
    if value.serialize(&mut serializer).is_err() {
        return "null".into();
    }
    // serde_json writes UTF-8.
    String::from_utf8(out).unwrap_or_else(|_| "null".into())
}

/// A string's length in UTF-16 code units, which is how JavaScript measures one.
pub(crate) fn utf16_len(text: &str) -> usize {
    if text.is_ascii() {
        text.len()
    } else {
        text.chars().map(char::len_utf16).sum()
    }
}

/// Advances `count` code points from byte index `from`: the index reached, and how many were passed.
pub(crate) fn advance(text: &str, from: usize, count: usize) -> (usize, usize) {
    let mut passed = 0;
    let mut index = from;
    for ch in text[from..].chars() {
        if passed >= count {
            break;
        }
        index += ch.len_utf8();
        passed += 1;
    }
    (index, passed)
}

/// Whether a string has more than `max` code points, reading at most `max + 1`.
pub(crate) fn longer_than(text: &str, max: usize) -> bool {
    // Fewer bytes than that can't be more code points.
    text.len() > max && advance(text, 0, max + 1).1 > max
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    #[allow(clippy::excessive_precision)]
    fn numbers_are_written_as_javascript_writes_them() {
        let cases: &[(f64, &str)] = &[
            (1e-7, "1e-7"),
            (1e21, "1e+21"),
            (0.30000000000000004, "0.30000000000000004"),
            (-0.0, "0"),
            (1.5e-7, "1.5e-7"),
            (123e18, "123000000000000000000"),
            (1e16, "10000000000000000"),
            (0.000001, "0.000001"),
            (100.0, "100"),
            (1.5e300, "1.5e+300"),
            (5e-324, "5e-324"),
            (-1e21, "-1e+21"),
            (2.5, "2.5"),
            (-0.0000015, "-0.0000015"),
            (1.0, "1"),
            // Halfway between two candidates: the even one, as in JavaScript.
            (1000000000000000.25, "1000000000000000.2"),
            (1000000000000000.75, "1000000000000000.8"),
            (0.5, "0.5"),
            (0.1, "0.1"),
            (123.456, "123.456"),
        ];
        for (value, expected) in cases {
            assert_eq!(js_f64(*value), *expected);
        }
    }

    #[test]
    fn values_are_written_compact_in_the_order_they_hold() {
        let value: Value =
            serde_json::from_str(r#"{"b": 1.0, "a": [1e21, "é\n", -0.0], "c": 7}"#).unwrap();
        assert_eq!(
            stringify(&value),
            "{\"b\":1,\"a\":[1e+21,\"é\\n\",0],\"c\":7}"
        );
    }

    #[test]
    fn an_integer_past_2_53_is_the_nearest_double_as_in_javascript() {
        // Each is what `JSON.stringify(JSON.parse(text))` gives in Node.
        for (text, written) in [
            ("9007199254740991", "9007199254740991"),
            ("9007199254740992", "9007199254740992"),
            ("9007199254740993", "9007199254740992"),
            ("-9007199254740993", "-9007199254740992"),
            ("123456789012345678", "123456789012345680"),
            ("999999999999999999", "1000000000000000000"),
            ("-999999999999999999", "-1000000000000000000"),
            ("18446744073709551615", "18446744073709552000"),
            ("-9223372036854775808", "-9223372036854776000"),
            ("1152921504606846976", "1152921504606847000"),
        ] {
            let value: Value = serde_json::from_str(text).unwrap();
            assert_eq!(stringify(&value), written, "{text}");
            assert_eq!(json_size(&value), written.len(), "{text}");
            let Value::Number(number) = &value else {
                panic!("{text} is a number")
            };
            assert_eq!(js_number(number), written, "{text}");
        }
    }

    #[test]
    fn an_objects_entries_are_read_in_javascripts_order() {
        let first = |text: &str, most: usize| -> Vec<String> {
            let value: Value = serde_json::from_str(text).unwrap();
            js_entries(value.as_object().unwrap(), most)
                .map(|(key, _)| key.clone())
                .collect()
        };
        let order = |text: &str| first(text, usize::MAX);
        // Array indexes first, ascending; the others as they came.
        assert_eq!(
            order(r#"{"b":1,"10":2,"a":3,"2":4,"0":5}"#),
            ["0", "2", "10", "b", "a"]
        );
        // Not array indexes: a leading zero, a sign, a fraction, 2^32 - 1 and beyond.
        assert_eq!(
            order(
                r#"{"007":1,"-1":2,"1.5":3,"4294967295":4,"4294967294":5,"99999999999":6,"":7,"1":8}"#
            ),
            [
                "1",
                "4294967294",
                "007",
                "-1",
                "1.5",
                "4294967295",
                "99999999999",
                ""
            ]
        );
        assert_eq!(order(r#"{"b":1,"a":2}"#), ["b", "a"]);
        assert_eq!(order("{}"), Vec::<String>::new());
        // Not array indexes either: other digits, a space, an exponent.
        assert_eq!(
            order(r#"{"\u0661":1," 1":2,"1e3":3,"-0":4,"00":5,"3":6}"#),
            ["3", "\u{661}", " 1", "1e3", "-0", "00"]
        );
        // A key given twice keeps its first place and its last value, as in JavaScript.
        let twice: Value = serde_json::from_str(r#"{"a":1,"7":2,"a":3,"7":4}"#).unwrap();
        assert_eq!(
            stringify(&js_ordered(twice.as_object().unwrap().clone())),
            r#"{"7":4,"a":3}"#
        );
        // A caller that reads two entries is given the two lowest indexes, however many there are.
        let wide: String = format!(
            "{{{}}}",
            (0..5_000)
                .rev()
                .map(|index| format!("\"{index}\":0"))
                .collect::<Vec<_>>()
                .join(",")
        );
        assert_eq!(first(&wide, 2)[..2], ["0", "1"]);
    }

    #[test]
    fn a_number_is_held_as_it_is_sent() {
        for (text, held) in [
            ("9007199254740991", "9007199254740991"),
            ("9007199254740993", "9007199254740992"),
            ("-9007199254740993", "-9007199254740992"),
            ("18446744073709551615", "18446744073709552000"),
            ("-9223372036854775808", "-9223372036854776000"),
            ("1.5", "1.5"),
            ("7", "7"),
        ] {
            let Value::Number(number) = serde_json::from_str(text).unwrap() else {
                panic!("{text} is a number")
            };
            let now = js_held(&number);
            assert_eq!(js_number(&now), held, "{text}");
            // Held as the number it is sent as: what a hook reads is what goes.
            assert_eq!(now.as_f64(), held.parse::<f64>().ok(), "{text}");
        }
    }

    #[test]
    fn a_number_held_as_text_is_written_as_javascript_writes_it() {
        for (text, written) in [
            ("999999999999999999", "1000000000000000000"),
            ("-999999999999999999", "-1000000000000000000"),
            ("100000000000000008193", "100000000000000020000"),
            ("9007199254740993.0", "9007199254740992"),
            ("-0", "0"),
            ("-0.0", "0"),
            ("1E5", "100000"),
            ("1.50", "1.5"),
            ("1e21", "1e+21"),
            ("42", "42"),
            // Too large for a double: JavaScript reads infinity, which JSON writes as null.
            ("1e999", "null"),
        ] {
            assert_eq!(js_literal(text), written, "{text}");
        }
    }

    #[test]
    fn numbers_are_read_as_javascript_reads_them() {
        // `serde_json` reads these a little off without its `float_roundtrip` feature, which
        // this crate turns on. Each is what `JSON.stringify(JSON.parse(text))` gives in Node.
        for (text, written) in [
            ("9007199254740993.0", "9007199254740992"),
            ("100000000000000008193", "100000000000000020000"),
            ("4000000000000150.0", "4000000000000150"),
            ("0.1e1", "1"),
            ("123456789012345678901234567890", "1.2345678901234568e+29"),
            ("2.2250738585072011e-308", "2.225073858507201e-308"),
            ("1.7976931348623157e308", "1.7976931348623157e+308"),
            ("8.41e21", "8.41e+21"),
            ("4.35", "4.35"),
        ] {
            let value: Value = serde_json::from_str(text).unwrap();
            assert_eq!(stringify(&value), written, "{text}");
        }
    }

    #[test]
    fn lengths_count_code_points_and_units() {
        assert_eq!(utf16_len("a😀"), 3);
        assert_eq!(advance("a😀b", 0, 2), (5, 2));
        assert!(longer_than("😀😀😀", 2));
        assert!(!longer_than("😀😀", 2));
    }
}
