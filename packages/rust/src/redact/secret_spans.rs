//! Finds the values under secret key names in JSON held in a string: a text block, an error
//! quoting a response, a stringified argument. They are found by scanning, not by parsing the
//! string: it may be cut, or be JSON inside prose or a code fence, and rewriting it would change
//! what isn't a secret. Only the spans of the values are found; everything else stays as it was.
//!
//! A port of `packages/lsquare/src/secret-spans.ts`, function for function, on UTF-16 code units
//! as there and in the Python SDK, so every length it reads (a key's last 256 characters, 256
//! past a quote, 1,024 past pydantic's `input_value=`) and every unit of work means what it means
//! there. Positions are `isize`, as they go below zero in the original too. The spans it gives are
//! byte ranges of the string it was given.

use super::patterns;

/// A value to replace. `start` and `end` are byte positions in the string given.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct SecretSpan {
    pub start: usize,
    pub end: usize,
    /// What makes it "the same value" for numbering. `None`: it couldn't be read whole, so it is numbered on its own.
    pub identity: Option<String>,
    /// What goes either side of its placeholder: nothing inside a string's quotes, otherwise a quote at its level of nesting.
    pub quote: String,
}

/// How much scanning one event may do. A unit is roughly one quote or bracket read, one backslash
/// or percent sign decoded, one JSON value checked, one key looked at, or 16 characters decoded or
/// of a parameter's name. Past this, a string `may_hold_key` lets through is replaced whole, as
/// nothing unread may be sent.
pub(crate) const SCAN_WORK: i64 = 1_000_000;

/// What's left of `SCAN_WORK` for an event.
#[derive(Debug)]
pub(crate) struct ScanWork {
    pub left: i64,
}

/// The scan work is used up.
struct Over;
type Scan<T> = Result<T, Over>;

/// How many times a string may hold JSON in a string. Past this, a string still holding a key is replaced whole.
const MAX_NESTING: u32 = 4;
/// The longest whole placeholder: `[SECRET_` + 9 digits + `]`.
const LONGEST_PLACEHOLDER: isize = 18;
const MAX_CODE_POINT: u32 = 0x10ffff;
const LITERALS: [&str; 3] = ["true", "false", "null"];
/// A name is matched by its end, so only this much of a long key is read.
const KEY_READ: isize = 256;
const INPUT_VALUE: &str = "input_value=";
const INPUT_TYPE: &str = ", input_type=";
/// How far past its start a value printed by pydantic is looked for its end: it cuts long ones to well under this.
const INPUT_READ: isize = 1024;
const PEM_START: &str = "-----BEGIN ";
/// How many times a URL held in a URL is read: a redirect chain encodes it again each time.
const MAX_PERCENT: u32 = 3;
/// How far past a closing quote a run of what ends a sentence is read.
const SENTENCE_READ: isize = 5;
/// How far past a Python string's first quote a quote that can end it is looked for: each string reads this at most, so the work stays linear.
const QUOTE_READ: isize = 256;
const PYTHON_LITERALS: [&str; 6] = ["None", "True", "False", "true", "false", "null"];

const TAB: i32 = 9;
const LINE_FEED: i32 = 10;
const CARRIAGE_RETURN: i32 = 13;
const SPACE: i32 = 32;
const BANG: i32 = 33;
const QUOTE: i32 = 34;
const HASH: i32 = 35;
const PERCENT: i32 = 37;
const AMPERSAND: i32 = 38;
const APOSTROPHE: i32 = 39;
const OPEN_PAREN: i32 = 40;
const CLOSE_PAREN: i32 = 41;
const PLUS: i32 = 43;
const COMMA: i32 = 44;
const MINUS: i32 = 45;
const PERIOD: i32 = 46;
const SLASH: i32 = 47;
const COLON: i32 = 58;
const SEMICOLON: i32 = 59;
const LESS: i32 = 60;
const EQUALS: i32 = 61;
const GREATER: i32 = 62;
const QUESTION: i32 = 63;
const OPEN_BRACKET: i32 = 91;
const BACKSLASH: i32 = 92;
const CLOSE_BRACKET: i32 = 93;
const UNDERSCORE: i32 = 95;
const BACKTICK: i32 = 96;
const LOWER_B: i32 = 98;
const LOWER_U: i32 = 117;
const OPEN_BRACE: i32 = 123;
const CLOSE_BRACE: i32 = 125;
const TILDE: i32 = 126;

/// A span while scanning: positions in UTF-16 units of the string given.
struct Found {
    start: isize,
    end: isize,
    identity: Option<Vec<u16>>,
    quote: String,
}

/// What one call of `secret_spans` shares.
struct Reading<'a> {
    is_secret_key: &'a dyn Fn(&str) -> bool,
    spans: Vec<Found>,
    work: &'a mut ScanWork,
    /// A decoded view where a container didn't close with its line ends as escapes: the rest are read with them as line ends.
    lines_in: Option<u32>,
    views: u32,
}

impl Reading<'_> {
    fn is_secret_key(&self, key: &[u16]) -> bool {
        (self.is_secret_key)(&String::from_utf16_lossy(key))
    }

    fn view(&mut self, text: Vec<u16>, map: Vec<isize>) -> View {
        self.views += 1;
        View {
            text,
            map: Some(map),
            id: self.views,
        }
    }
}

/// A string to scan, and where each of its positions is in the string given. The string itself has no map.
struct View {
    text: Vec<u16>,
    map: Option<Vec<isize>>,
    id: u32,
}

impl View {
    fn len(&self) -> isize {
        self.text.len() as isize
    }
}

// Reading UTF-16 units as JavaScript does: past either end there is nothing, which equals nothing.

/// The unit at `index`, or -1 past either end.
fn unit(text: &[u16], index: isize) -> i32 {
    if index < 0 {
        return -1;
    }
    text.get(index as usize).map_or(-1, |unit| i32::from(*unit))
}

fn clamp(text: &[u16], index: isize) -> usize {
    index.clamp(0, text.len() as isize) as usize
}

fn slice(text: &[u16], from: isize, to: isize) -> &[u16] {
    let (from, to) = (clamp(text, from), clamp(text, to));
    if from >= to {
        &[]
    } else {
        &text[from..to]
    }
}

fn matches_at(text: &[u16], pattern: &str, at: usize) -> bool {
    let bytes = pattern.as_bytes();
    at + bytes.len() <= text.len()
        && bytes
            .iter()
            .zip(&text[at..])
            .all(|(byte, unit)| u16::from(*byte) == *unit)
}

/// `text.startsWith(pattern, at)`: a position below zero is the start.
fn starts_with(text: &[u16], pattern: &str, at: isize) -> bool {
    matches_at(text, pattern, clamp(text, at))
}

fn ends_with(text: &[u16], pattern: &str) -> bool {
    text.len() >= pattern.len() && matches_at(text, pattern, text.len() - pattern.len())
}

/// `text.indexOf(pattern, from)`, -1 when it isn't there.
fn index_of(text: &[u16], pattern: &str, from: isize) -> isize {
    let first = u16::from(pattern.as_bytes()[0]);
    let mut at = clamp(text, from);
    while at + pattern.len() <= text.len() {
        match text[at..].iter().position(|unit| *unit == first) {
            None => return -1,
            Some(offset) => at += offset,
        }
        if matches_at(text, pattern, at) {
            return at as isize;
        }
        at += 1;
    }
    -1
}

fn index_of_unit(text: &[u16], wanted: i32, from: isize) -> isize {
    let from = clamp(text, from);
    text[from..]
        .iter()
        .position(|unit| i32::from(*unit) == wanted)
        .map_or(-1, |offset| (from + offset) as isize)
}

fn contains(text: &[u16], pattern: &str) -> bool {
    index_of(text, pattern, 0) != -1
}

fn is_one_of(unit: i32, set: &str) -> bool {
    (0..128).contains(&unit) && set.as_bytes().contains(&(unit as u8))
}

/// What text ending in isn't a key, unless it starts an entry of an object: it is code or a
/// sentence before a string, as in `token == ":"` or `createToken(":", a)`. Nothing at all counts
/// too, as in the original, where it is the empty string every string includes.
fn not_a_key_end(unit: i32) -> bool {
    unit == -1 || is_one_of(unit, " \t=([+?,.<|&")
}

/// What a sentence or code can have after a closing quote, before a space or a line's end.
fn is_sentence_end(unit: i32) -> bool {
    is_one_of(unit, ".;)'`")
}

/// What a query value stops at: where a URL ends in text, or the next parameter.
fn is_query_value_end(unit: i32) -> bool {
    is_one_of(unit, " \t\r\n\x0c\x0b&#\"'<>`)]}\\")
}

fn is_digit(unit: i32) -> bool {
    (48..=57).contains(&unit)
}

fn is_letter(unit: i32) -> bool {
    (65..=90).contains(&unit) || (97..=122).contains(&unit)
}

fn is_word(unit: i32) -> bool {
    is_digit(unit) || is_letter(unit)
}

fn is_hex_unit(unit: i32) -> bool {
    is_digit(unit) || (65..=70).contains(&unit) || (97..=102).contains(&unit)
}

/// JSON's whitespace, the same in every language.
fn is_space(unit: i32) -> bool {
    unit == SPACE || unit == TAB || unit == LINE_FEED || unit == CARRIAGE_RETURN
}

/// What ends a value that isn't JSON: so `"password": correct horse battery` is masked whole.
fn ends_bare(unit: i32) -> bool {
    unit == LINE_FEED
        || unit == CARRIAGE_RETURN
        || unit == COMMA
        || unit == CLOSE_BRACE
        || unit == CLOSE_BRACKET
}

fn skip_space(text: &[u16], from: isize) -> isize {
    let mut index = from;
    while index < text.len() as isize && is_space(unit(text, index)) {
        index += 1;
    }
    index
}

fn skip_blank(text: &[u16], from: isize) -> isize {
    let mut index = from;
    while index < text.len() as isize && (unit(text, index) == SPACE || unit(text, index) == TAB) {
        index += 1;
    }
    index
}

fn parse_hex(text: &[u16]) -> u32 {
    text.iter().fold(0, |value, unit| {
        value * 16
            + char::from_u32(u32::from(*unit))
                .and_then(|digit| digit.to_digit(16))
                .unwrap_or(0)
    })
}

fn spend(work: &mut ScanWork, amount: i64) -> Scan<()> {
    work.left -= amount;
    if work.left < 0 {
        Err(Over)
    } else {
        Ok(())
    }
}

fn position(view: &View, index: isize) -> isize {
    match &view.map {
        None => index,
        Some(map) => map[index as usize],
    }
}

/// `%` and any number of `25`, then `3D`: an `=`, percent-encoded any number of times.
fn has_percent_equals(text: &[u16]) -> bool {
    let mut from = 0;
    loop {
        let at = index_of_unit(text, PERCENT, from);
        if at == -1 {
            return false;
        }
        let mut index = at + 1;
        while starts_with(text, "25", index) && index + 2 <= text.len() as isize {
            index += 2;
        }
        if unit(text, index) == 51 && (unit(text, index + 1) == 68 || unit(text, index + 1) == 100)
        {
            return true;
        }
        from = at + 1;
    }
}

/// Whether a string could hold a secret value at all: the cheap check before scanning it. It needs
/// a quote and a colon or `=>`, as they are or encoded any number of times; or an `=`, as it is or
/// percent-encoded, and what can start a URL's parameter; or pydantic's `input_value=`.
pub(crate) fn may_hold_key(text: &str) -> bool {
    if (text.contains('"') || text.contains('\'') || text.contains("u0022"))
        && (text.contains(':')
            || text.contains("=>")
            || text.contains("u003a")
            || text.contains("u003A"))
    {
        return true;
    }
    if text.contains('%') {
        let units: Vec<u16> = text.encode_utf16().collect();
        if has_percent_equals(&units) {
            return true;
        }
    }
    text.contains('=')
        && (text.contains('?')
            || text.contains('&')
            || text.contains('#')
            || text.contains(INPUT_VALUE))
}

fn may_hold_key_units(text: &[u16]) -> bool {
    let has = |wanted: i32| index_of_unit(text, wanted, 0) != -1;
    if (has(QUOTE) || has(APOSTROPHE) || contains(text, "u0022"))
        && (has(COLON)
            || contains(text, "=>")
            || contains(text, "u003a")
            || contains(text, "u003A"))
    {
        return true;
    }
    if has(PERCENT) && has_percent_equals(text) {
        return true;
    }
    has(EQUALS) && (has(QUESTION) || has(AMPERSAND) || has(HASH) || contains(text, INPUT_VALUE))
}

/// The values under secret key names in `text`, in order and not overlapping.
pub(crate) fn secret_spans(
    text: &str,
    is_secret_key: &dyn Fn(&str) -> bool,
    work: &mut ScanWork,
) -> Vec<SecretSpan> {
    let units: Vec<u16> = text.encode_utf16().collect();
    let mut reading = Reading {
        is_secret_key,
        spans: Vec::new(),
        work,
        lines_in: None,
        views: 0,
    };
    let view = View {
        text: units,
        map: None,
        id: 0,
    };
    if read(&view, 0, false, &mut reading).is_err() {
        return vec![SecretSpan {
            start: 0,
            end: text.len(),
            identity: None,
            quote: String::new(),
        }];
    }
    // Where each UTF-16 unit starts in the string's bytes. A span never starts or ends inside a
    // character: the second unit of a pair stands for the character's end.
    let mut bytes = Vec::with_capacity(view.text.len() + 1);
    for (index, ch) in text.char_indices() {
        bytes.push(index);
        if ch.len_utf16() == 2 {
            bytes.push(index + ch.len_utf8());
        }
    }
    bytes.push(text.len());
    let spans = merged(reading.spans)
        .into_iter()
        .map(|found| SecretSpan {
            start: bytes[found.start as usize],
            end: bytes[found.end as usize],
            identity: found.identity.map(|identity| identity_text(&identity)),
            quote: found.quote,
        })
        .collect();
    with_whole_keys(text, spans)
}

/// A value's identity as a string. One holding a lone surrogate, which a written escape can make
/// (`"\\ud800"`), is spelled out by its units, so two such values don't become the same string.
fn identity_text(units: &[u16]) -> String {
    match String::from_utf16(units) {
        Ok(text) => text,
        Err(_) => format!(
            "\u{0}utf16:{}",
            units
                .iter()
                .map(|unit| format!("{unit:04x}"))
                .collect::<String>()
        ),
    }
}

/// A value can stop at a private key block's first line, which would leave the block without the
/// BEGIN its pattern looks for. So a block that a value runs into goes whole.
fn with_whole_keys(text: &str, spans: Vec<SecretSpan>) -> Vec<SecretSpan> {
    if spans.is_empty() || !text.contains(PEM_START) {
        return spans;
    }
    let mut out = spans.clone();
    let mut index = 0;
    let mut from = 0;
    while let Some(offset) = text[from..].find(PEM_START) {
        let begin = from + offset;
        let Some(end) = patterns::pem_at(text, begin) else {
            from = begin + 1;
            continue;
        };
        while index < spans.len() && spans[index].end <= begin {
            index += 1;
        }
        if index < spans.len() && spans[index].start < end {
            out.push(SecretSpan {
                start: begin,
                end,
                identity: None,
                quote: String::new(),
            });
        }
        from = end.max(begin + 1);
    }
    if out.len() == spans.len() {
        return spans;
    }
    out.sort_by(|a, b| a.start.cmp(&b.start).then(b.end.cmp(&a.end)));
    let mut merged: Vec<SecretSpan> = Vec::with_capacity(out.len());
    for span in out {
        match merged.last_mut() {
            Some(last) if span.start < last.end => {
                if span.end > last.end {
                    last.end = span.end;
                    last.identity = None;
                }
            }
            _ => merged.push(span),
        }
    }
    merged
}

fn merged(mut spans: Vec<Found>) -> Vec<Found> {
    spans.sort_by(|a, b| a.start.cmp(&b.start).then(b.end.cmp(&a.end)));
    let mut out: Vec<Found> = Vec::with_capacity(spans.len());
    for span in spans {
        match out.last_mut() {
            Some(last) if span.start < last.end => {
                if span.end > last.end {
                    last.end = span.end;
                    last.identity = None;
                }
            }
            _ => out.push(span),
        }
    }
    out
}

/// Python's repr and Node's util.inspect double JSON's backslashes and leave its quotes, so `\\"`
/// reads as the end of a string. Decoded once, it's JSON again. So a string that isn't JSON and
/// holds a backslash is read a second time decoded, once for each chain of nesting, which keeps
/// the work linear. JSON as it is doesn't need that, and could come out wrong: its `\\"` does end
/// a string.
fn read(view: &View, depth: u32, printed: bool, reading: &mut Reading) -> Scan<()> {
    scan(view, depth, printed, reading)?;
    scan_single_quoted(view, reading)?;
    scan_input_values(view, reading)?;
    scan_queries(view, reading)?;
    if printed
        || depth == MAX_NESTING
        || index_of_unit(&view.text, BACKSLASH, 0) == -1
        || is_valid_json(&view.text, reading.work)?
    {
        return Ok(());
    }
    let decoded = decode(view, 0, view.len(), reading)?;
    if decoded.text.len() < view.text.len() {
        read(&decoded, depth + 1, true, reading)?;
    }
    Ok(())
}

/// Whether the text is one JSON value. Checked here, not by a parser, so that the languages agree
/// at any depth.
fn is_valid_json(text: &[u16], work: &mut ScanWork) -> Scan<bool> {
    let length = text.len() as isize;
    let mut open: Vec<i32> = Vec::new();
    let mut index = skip_space(text, 0);
    loop {
        spend(work, 1)?;
        let first = unit(text, index);
        let end;
        if first == OPEN_BRACE || first == OPEN_BRACKET {
            let inner = skip_space(text, index + 1);
            if unit(text, inner) != first + 2 {
                open.push(first);
                match if first == OPEN_BRACE {
                    json_key_end(text, inner)
                } else {
                    Some(inner)
                } {
                    None => return Ok(false),
                    Some(next) => index = next,
                }
                continue;
            }
            end = Some(inner + 1);
        } else {
            end = json_scalar_end(text, index);
        }
        let Some(end) = end else { return Ok(false) };
        index = skip_space(text, end);
        while let Some(last) = open.last() {
            if unit(text, index) != last + 2 {
                break;
            }
            open.pop();
            index = skip_space(text, index + 1);
        }
        let Some(last) = open.last() else {
            return Ok(index == length);
        };
        if unit(text, index) != COMMA {
            return Ok(false);
        }
        index = skip_space(text, index + 1);
        if *last == OPEN_BRACE {
            match json_key_end(text, index) {
                None => return Ok(false),
                Some(next) => index = next,
            }
        }
    }
}

/// Where the value after the key at `at` starts.
fn json_key_end(text: &[u16], at: isize) -> Option<isize> {
    if unit(text, at) != QUOTE {
        return None;
    }
    let end = json_string_end(text, at + 1)?;
    let colon = skip_space(text, end);
    (unit(text, colon) == COLON).then(|| skip_space(text, colon + 1))
}

fn json_scalar_end(text: &[u16], at: isize) -> Option<isize> {
    if unit(text, at) == QUOTE {
        return json_string_end(text, at + 1);
    }
    for literal in LITERALS {
        if starts_with(text, literal, at) {
            return Some(at + literal.len() as isize);
        }
    }
    let end = json_number_end(text, at);
    (end > at).then_some(end)
}

fn json_string_end(text: &[u16], from: isize) -> Option<isize> {
    let length = text.len() as isize;
    let mut index = from;
    while index < length {
        let current = unit(text, index);
        if current == QUOTE {
            return Some(index + 1);
        }
        if current < SPACE {
            return None;
        }
        if current == BACKSLASH {
            index += 1;
            if unit(text, index) == LOWER_U {
                let digits = slice(text, index + 1, index + 5);
                if digits.len() != 4 || !digits.iter().all(|digit| is_hex_unit(i32::from(*digit))) {
                    return None;
                }
                index += 4;
            } else if escaped_char(unit(text, index)).is_none() {
                return None;
            }
        }
        index += 1;
    }
    None
}

/// Where a JSON number that starts at `at` ends, or `at` when none does.
fn json_number_end(text: &[u16], at: isize) -> isize {
    let mut index = at;
    if unit(text, index) == MINUS {
        index += 1;
    }
    if unit(text, index) == 48 {
        index += 1;
    } else if (49..=57).contains(&unit(text, index)) {
        while is_digit(unit(text, index)) {
            index += 1;
        }
    } else {
        return at;
    }
    if unit(text, index) == PERIOD && is_digit(unit(text, index + 1)) {
        index += 2;
        while is_digit(unit(text, index)) {
            index += 1;
        }
    }
    if unit(text, index) == 69 || unit(text, index) == 101 {
        let mut exponent = index + 1;
        if unit(text, exponent) == PLUS || unit(text, exponent) == MINUS {
            exponent += 1;
        }
        if is_digit(unit(text, exponent)) {
            while is_digit(unit(text, exponent)) {
                exponent += 1;
            }
            index = exponent;
        }
    }
    index
}

/// Every piece of text before an unescaped quote is a candidate, whether it really is a string or
/// lies between two of them. A key is one that a colon follows. So a stray quote in prose can't
/// put the scan out of step, and a value holding quotes needs no special case.
fn scan(view: &View, depth: u32, printed: bool, reading: &mut Reading) -> Scan<()> {
    let text = &view.text;
    let length = view.len();
    let mut start = 0;
    loop {
        spend(reading.work, 1)?;
        let (end, escaped) = lex(text, start);
        // A key is read too: hiding a value in JSON used as a key is worth two such keys coming out the same.
        if escaped {
            nest(view, start, end, depth, printed, reading)?;
        }
        if end >= length {
            return Ok(());
        }
        let colon = skip_space(text, end + 1);
        let arrow = starts_with(text, "=>", colon);
        if (arrow || (unit(text, colon) == COLON && unit(text, colon + 1) != COLON))
            && is_key(view, start, end, colon, escaped, reading)?
        {
            let level = if printed { depth - 1 } else { depth };
            match read_value(
                view,
                skip_space(text, colon + if arrow { 2 } else { 1 }),
                level,
                starts_entry(text, start),
                reading,
            )? {
                None => return Ok(()),
                Some(next) => start = next,
            }
        } else {
            start = end + 1;
        }
    }
}

/// A key with a secret key name. What's before a quote can be code or a sentence, as in
/// `cond ? "token" : "x"`, but not where an object has a key: there, JSON's keys are read as they are.
fn is_key(
    view: &View,
    start: isize,
    end: isize,
    colon: isize,
    escaped: bool,
    reading: &mut Reading,
) -> Scan<bool> {
    let key = body(view, start, end, escaped, reading)?;
    let read = if key.len() as isize > KEY_READ {
        &key[key.len() - KEY_READ as usize..]
    } else {
        &key[..]
    };
    if !reading.is_secret_key(read) {
        return Ok(false);
    }
    let text = &view.text;
    let before = before_key(text, start);
    let previous = unit(text, before);
    if previous == QUESTION && (before == start - 2 || colon > end + 1) {
        return Ok(false);
    }
    if !not_a_key_end(unit(text, end - 1))
        || previous == OPEN_BRACE
        || previous == COMMA
        || previous == LINE_FEED
        || previous == CARRIAGE_RETURN
    {
        return Ok(true);
    }
    Ok(before < 0 && unit(text, start - 1) == QUOTE)
}

/// Whether a value that isn't a quoted string can follow this key. Not when the key comes straight
/// after a word or an operator, where it is code or a sentence quoting something: `case "token":
/// return x`, `x == "token": y`, `"users"."token"::text`. A line number, a timestamp or a colour
/// code isn't a word: a word is two letters.
fn starts_entry(text: &[u16], start: isize) -> bool {
    let index = before_key(text, start);
    let current = unit(text, index);
    let previous = unit(text, index - 1);
    if current == EQUALS || (is_letter(current) && is_letter(previous)) {
        return false;
    }
    // `-> "Token":`, not a quoted line's `> "token":`.
    if current == GREATER {
        return previous != MINUS && previous != EQUALS;
    }
    // `Failed. "token":` and `"users"."token":`, not a list's `1. "token":` or an elided `..."token":`.
    current != PERIOD || !(is_letter(previous) || previous == QUOTE)
}

/// Where what's before the quote opening the piece at `start` is, past its escapes, spaces and
/// tabs. -1: nothing is, or no quote opens it, as in a string cut in its key.
fn before_key(text: &[u16], start: isize) -> isize {
    if unit(text, start - 1) != QUOTE {
        return -1;
    }
    let mut index = start - 2;
    while index >= 0
        && (unit(text, index) == SPACE
            || unit(text, index) == TAB
            || unit(text, index) == BACKSLASH)
    {
        index -= 1;
    }
    index
}

/// Python's repr of a dict, which is how pydantic's errors quote an input, and other text with
/// single-quoted keys: `{'password': 'x', 'note': "it's"}`, and `Map { 'password' => 'x' }`. Any
/// two quotes in a row can hold a key, whatever is between them, so an apostrophe can't put the
/// scan out of step. Only one that opens an entry is a key, so a sentence's `the 'token': 'x'`
/// isn't, and a SQL cast's `'token'::text` isn't either.
fn scan_single_quoted(view: &View, reading: &mut Reading) -> Scan<()> {
    let text = &view.text;
    let mut open = index_of_unit(text, APOSTROPHE, 0);
    while open != -1 {
        let close = index_of_unit(text, APOSTROPHE, open + 1);
        if close == -1 {
            return Ok(());
        }
        let after = skip_blank(text, close + 1);
        let arrow = starts_with(text, "=>", after);
        if (arrow || (unit(text, after) == COLON && unit(text, after + 1) != COLON))
            && opens_entry(text, open)
        {
            spend(reading.work, 1)?;
            if reading.is_secret_key(slice(text, (open + 1).max(close - KEY_READ), close)) {
                match read_python_value(
                    view,
                    skip_space(text, after + if arrow { 2 } else { 1 }),
                    reading,
                )? {
                    None => return Ok(()),
                    Some(next) => {
                        open = index_of_unit(text, APOSTROPHE, next);
                        continue;
                    }
                }
            }
        }
        open = close;
    }
    Ok(())
}

/// Whether the quote at `open`, or the `b` or `u` before it, comes straight after `{`, `(` or `,`,
/// or at the start of a line or the text.
fn opens_entry(text: &[u16], open: isize) -> bool {
    let mut index = open - 1;
    if unit(text, index) == LOWER_B || unit(text, index) == LOWER_U {
        index -= 1;
    }
    while index >= 0 && (unit(text, index) == SPACE || unit(text, index) == TAB) {
        index -= 1;
    }
    if index < 0 {
        return true;
    }
    let current = unit(text, index);
    current == OPEN_BRACE
        || current == OPEN_PAREN
        || current == COMMA
        || current == LINE_FEED
        || current == CARRIAGE_RETURN
}

/// Where a string in Python's quotes starts at or after a `b` or `u` at `at`, or `None`: there is no such string.
fn python_quote_at(text: &[u16], at: isize) -> Option<isize> {
    let quote_at = if unit(text, at) == LOWER_B || unit(text, at) == LOWER_U {
        at + 1
    } else {
        at
    };
    let current = unit(text, quote_at);
    (current == APOSTROPHE || current == QUOTE || current == BACKTICK).then_some(quote_at)
}

fn push(
    view: &View,
    reading: &mut Reading,
    start: isize,
    end: isize,
    identity: Option<Vec<u16>>,
    quote: &str,
) {
    reading.spans.push(Found {
        start: position(view, start),
        end: position(view, end),
        identity,
        quote: quote.to_owned(),
    });
}

/// Adds the span of the value at `at`, as Python or Node's util.inspect writes one. Where scanning
/// goes on; `None`: it ran to the end of the text.
fn read_python_value(view: &View, at: isize, reading: &mut Reading) -> Scan<Option<isize>> {
    let text = &view.text;
    let length = view.len();
    if at >= length {
        return Ok(Some(at));
    }
    if let Some(quote_at) = python_quote_at(text, at) {
        return Ok(Some(read_python_string(view, quote_at, reading)?));
    }
    // An escaped quote is read decoded, where it is a string's: `\\"it's\\"`, in JSON holding a repr.
    if unit(text, at) == BACKSLASH {
        return Ok(Some(at));
    }
    if let Some(bracket) = python_bracket_at(text, at) {
        let Some(close) = python_containers_end(view, bracket, reading)? else {
            push(view, reading, at, length, None, "'");
            return Ok(None);
        };
        if (bracket == at && skip_space(text, at + 1) == close - 1)
            || is_placeholder(view, at, close)
        {
            return Ok(Some(close));
        }
        if !ends_token(text, close) {
            return Ok(Some(bare_value(view, at, close, "'", reading)));
        }
        push(
            view,
            reading,
            at,
            close,
            Some(slice(text, at, close).to_vec()),
            "'",
        );
        return Ok(Some(close));
    }
    // What util.inspect and repr write in angle brackets: `<Buffer 68 69>` holds a value's bytes.
    if unit(text, at) == LESS {
        let close = index_of_unit(slice(text, at, at + KEY_READ), GREATER, 0);
        if close != -1 && ends_token(text, at + close + 1) {
            push(
                view,
                reading,
                at,
                at + close + 1,
                Some(slice(text, at, at + close + 1).to_vec()),
                "'",
            );
            return Ok(Some(at + close + 1));
        }
    }
    for literal in PYTHON_LITERALS {
        if starts_with(text, literal, at) && ends_token(text, at + literal.len() as isize) {
            return Ok(Some(at + literal.len() as isize));
        }
    }
    let number = json_number_end(text, at);
    if number > at && ends_token(text, number) {
        push(
            view,
            reading,
            at,
            number,
            Some(slice(text, at, number).to_vec()),
            "'",
        );
        return Ok(Some(number));
    }
    // One word, as `{'token': token}` has. More than that is a sentence: `, 'password': explains it.`
    let limit = (at + KEY_READ).min(length);
    let mut end = at;
    while end < limit && !ends_bare(unit(text, end)) && !is_space(unit(text, end)) {
        end += 1;
    }
    if end < limit && !ends_token(text, end) {
        return Ok(Some(at));
    }
    Ok(Some(if end > at {
        bare_value(view, at, at, "'", reading)
    } else {
        at
    }))
}

/// Adds the span of the string whose opening quote is at `quote_at`, and of what continues it:
/// pprint writes a long value as adjacent strings, and util.inspect joins them with `+`. Where
/// scanning goes on.
fn read_python_string(view: &View, quote_at: isize, reading: &mut Reading) -> Scan<isize> {
    let text = &view.text;
    let first = unit(text, quote_at);
    let (end, escaped) = python_string_end(view, quote_at + 1, first, false, reading.work, false)?;
    let mut last = end;
    let mut last_quote = first;
    loop {
        if unit(text, last) != last_quote {
            break;
        }
        let mut next = skip_space(text, last + 1);
        if unit(text, next) == PLUS {
            next = skip_space(text, next + 1);
        }
        let Some(piece) = python_quote_at(text, next) else {
            break;
        };
        let quote = unit(text, piece);
        let (ended, _) = python_string_end(view, piece + 1, quote, true, reading.work, false)?;
        if unit(text, ended) == quote && unit(text, skip_blank(text, ended + 1)) == COLON {
            break;
        }
        last = ended;
        last_quote = quote;
    }
    let closed = unit(text, last) == last_quote;
    if last == end && is_placeholder(view, quote_at + 1, end) {
        return Ok(if closed { end + 1 } else { end });
    }
    // Pieces with different quotes: their placeholder takes one kind, so that they stay balanced.
    if closed && last_quote != first {
        push(view, reading, quote_at, last + 1, None, "'");
        return Ok(last + 1);
    }
    if last > quote_at + 1 {
        let identity = if closed && last == end {
            Some(body(view, quote_at + 1, end, escaped, reading)?)
        } else {
            None
        };
        push(view, reading, quote_at + 1, last, identity, "");
    }
    Ok(if closed { last + 1 } else { last })
}

/// Where a string in Python's quotes ends: a quote that something able to follow it follows.
/// Without one soon after its first quote, on its line, that first quote, so that a sentence after
/// it stays; without a quote, its line's end, which repr never has in a string. A line end that
/// was an escape, in text read decoded, is one only if no quote follows, or with `lines`.
fn python_string_end(
    view: &View,
    from: isize,
    quote: i32,
    in_container: bool,
    work: &mut ScanWork,
    lines: bool,
) -> Scan<(isize, bool)> {
    let text = &view.text;
    let length = view.len();
    let mut escaped = false;
    let mut first_quote: Option<isize> = None;
    let mut first_line: Option<isize> = None;
    let mut index = from;
    while index < length {
        if let Some(first_quote) = first_quote {
            if index > first_quote + QUOTE_READ {
                return Ok((first_quote, escaped));
            }
        }
        let current = unit(text, index);
        if current == BACKSLASH {
            escaped = true;
            index += 2;
        } else if (current == LINE_FEED || current == CARRIAGE_RETURN)
            && (lines || !from_escape(view, index))
        {
            return Ok((first_quote.unwrap_or(index), escaped));
        } else {
            if current == quote {
                spend(work, 1)?;
                if ends_python_string(text, index + 1, in_container) {
                    return Ok((index, escaped));
                }
                first_quote.get_or_insert(index);
            } else if current == LINE_FEED || current == CARRIAGE_RETURN {
                first_line.get_or_insert(index);
            }
            index += 1;
        }
    }
    Ok((first_quote.or(first_line).unwrap_or(length), escaped))
}

/// Whether what's at `at` in a decoded view was written as an escape.
fn from_escape(view: &View, at: isize) -> bool {
    view.map.is_some() && position(view, at + 1) - position(view, at) != 1
}

/// Not a colon outside a container, which is the next key's quote in a repr pydantic cut short: `{'token': 'abc...i_key': b'x'}`.
fn ends_python_string(text: &[u16], at: isize, in_container: bool) -> bool {
    let length = text.len() as isize;
    let index = skip_blank(text, at);
    if index >= length || is_line_end(text, index) || is_written_line_end(text, index) {
        return true;
    }
    let current = unit(text, index);
    if current == COMMA
        || current == CLOSE_BRACE
        || current == CLOSE_BRACKET
        || current == CLOSE_PAREN
    {
        return true;
    }
    if current == PLUS {
        return python_quote_at(text, skip_space(text, index + 1)).is_some();
    }
    // A key's end, and `<class 'list'>`.
    if in_container && (current == COLON || current == GREATER || starts_with(text, "=>", index)) {
        return true;
    }
    // One run of what ends a sentence, read at most a few characters on: a long run isn't one.
    let mut end = index;
    while end < index + SENTENCE_READ {
        if end >= length || is_space(unit(text, end)) {
            return end > index;
        }
        if !is_sentence_end(unit(text, end)) {
            return false;
        }
        end += 1;
    }
    false
}

/// Past util.inspect's mark on a container something inside it refers back to: `<ref *1> `.
fn inspect_ref_end(text: &[u16], at: isize) -> Option<isize> {
    if !starts_with(text, "<ref *", at) || at < 0 {
        return None;
    }
    let mut index = at + 6;
    let digits = index;
    while is_digit(unit(text, index)) && index - digits < 9 {
        index += 1;
    }
    (index > digits && starts_with(text, "> ", index) && index + 2 <= text.len() as isize)
        .then_some(index + 2)
}

/// The bracket of the container at `at`, which may have any name: `[1, 2]`, `OrderedDict(a='b')`,
/// `Foo { a: 'b' }`, `$e { a: 'b' }`, `f.<locals>.Foo(a='b')`.
fn python_bracket_at(text: &[u16], at: isize) -> Option<isize> {
    let from = inspect_ref_end(text, at).unwrap_or(at);
    let limit = (from + KEY_READ).min(text.len() as isize);
    let mut index = from;
    while index < limit && !ends_container_name(unit(text, index)) {
        index += 1;
    }
    if index > from && unit(text, index) == SPACE {
        index += 1;
    }
    opens_container(unit(text, index)).then_some(index)
}

fn ends_container_name(current: i32) -> bool {
    is_space(current)
        || current == QUOTE
        || current == APOSTROPHE
        || current == BACKTICK
        || current == COMMA
        || opens_container(current)
        || current == CLOSE_BRACE
        || current == CLOSE_BRACKET
        || current == CLOSE_PAREN
}

fn opens_container(current: i32) -> bool {
    current == OPEN_BRACE || current == OPEN_BRACKET || current == OPEN_PAREN
}

/// Where the container whose first bracket is at `at` ends, past what's written before its
/// contents: `Set(2) { 'a' }`, `Foo(2) [Map] { 'a' => 'b' }`, `[Object: null prototype] { a: 'b' }`,
/// `Resp[int](a=1)`.
fn python_containers_end(view: &View, at: isize, reading: &mut Reading) -> Scan<Option<isize>> {
    let text = &view.text;
    let mut close = python_container_read(view, at, reading)?;
    while let Some(closed) = close {
        let next = if unit(text, closed) == SPACE {
            closed + 1
        } else {
            closed
        };
        if !opens_container(unit(text, next)) {
            break;
        }
        close = python_container_read(view, next, reading)?;
    }
    Ok(close)
}

/// A decoded view's line ends were escapes. A container that doesn't close is read again with them
/// as line ends, as in text that has them as they are, and so is every later one in that view, so
/// that many of them don't each read to the far end first.
fn python_container_read(view: &View, at: isize, reading: &mut Reading) -> Scan<Option<isize>> {
    if view.map.is_none() {
        return python_container_end(view, at, false, reading.work);
    }
    if reading.lines_in == Some(view.id) {
        return python_container_end(view, at, true, reading.work);
    }
    if let Some(close) = python_container_end(view, at, false, reading.work)? {
        return Ok(Some(close));
    }
    reading.lines_in = Some(view.id);
    python_container_end(view, at, true, reading.work)
}

/// Where a container in Python's syntax ends, past its closing bracket. `None`: its brackets don't match, or it never closes.
fn python_container_end(
    view: &View,
    at: isize,
    lines: bool,
    work: &mut ScanWork,
) -> Scan<Option<isize>> {
    let text = &view.text;
    let length = view.len();
    let mut open: Vec<i32> = Vec::new();
    let mut index = at;
    while index < length {
        let current = unit(text, index);
        if current == APOSTROPHE || current == QUOTE || current == BACKTICK {
            spend(work, 1)?;
            index = if current != BACKTICK && unit(text, index - 1) == BACKSLASH {
                escaped_string_end(text, index, current)
            } else {
                python_string_end(view, index + 1, current, true, work, lines)?.0 + 1
            };
            continue;
        }
        if current == OPEN_BRACE || current == OPEN_BRACKET || current == OPEN_PAREN {
            spend(work, 1)?;
            open.push(current);
        } else if current == CLOSE_BRACE || current == CLOSE_BRACKET || current == CLOSE_PAREN {
            spend(work, 1)?;
            if open.pop()
                != Some(if current == CLOSE_PAREN {
                    OPEN_PAREN
                } else {
                    current - 2
                })
            {
                return Ok(None);
            }
            if open.is_empty() {
                return Ok(Some(index + 1));
            }
        }
        index += 1;
    }
    Ok(None)
}

/// Past a string whose opening quote has a backslash before it, as JSON holding a repr has: its closing quote has one too.
fn escaped_string_end(text: &[u16], at: isize, quote: i32) -> isize {
    let length = text.len() as isize;
    let mut index = at + 1;
    while index + 1 < length {
        if unit(text, index) == BACKSLASH && unit(text, index + 1) == quote {
            return index + 2;
        }
        index += 1;
    }
    length
}

/// Where pydantic's errors have a value: straight after the error's type, which a custom error can
/// spell any way. The match's start and end, from `from` on.
fn pydantic_value(text: &[u16], from: isize) -> Option<(isize, isize)> {
    let mut at = index_of(text, "[type=", from);
    while at != -1 {
        // The type: up to 128 characters on one line, as few as reach `, input_value=`.
        let name = at + 6;
        for length in 1..=128 {
            let last = unit(text, name + length - 1);
            if last == -1 || last == LINE_FEED || last == CARRIAGE_RETURN {
                break;
            }
            if matches_at(text, ", input_value=", (name + length) as usize) {
                return Some((at, name + length + 14));
            }
        }
        at = index_of(text, "[type=", at + 1);
    }
    None
}

/// Pydantic's errors quote what they refused: `Input should be a valid string [type=string_type,
/// input_value=123, input_type=int]`. It can be cut anywhere, list its key on another line or not
/// at all, and the event's input has it anyway, so it always goes, up to `, input_type=` and the
/// type's name. Without that soon after it, it was cut: up to its line's end, however that's written.
fn scan_input_values(view: &View, reading: &mut Reading) -> Scan<()> {
    let text = &view.text;
    let length = view.len();
    // Each looked for only once it's behind: a search per value would read to a far one every time.
    let mut input_type = -1;
    let mut line = -1;
    let mut written = -1;
    let mut from = 0;
    while let Some((_, start)) = pydantic_value(text, from) {
        spend(reading.work, 1)?;
        if input_type < start {
            input_type = input_type_at(text, start);
        }
        if line < start {
            line = line_end(text, start, length).unwrap_or(length);
        }
        let mut end = input_type;
        if input_type > line || input_type - start > INPUT_READ {
            if written < start {
                written = written_line_end(text, start);
            }
            end = line.min(written);
        }
        if end > start && !is_placeholder(view, start, end) {
            push(
                view,
                reading,
                start,
                end,
                repr_text(slice(text, start, end)),
                "",
            );
        }
        from = end;
    }
    Ok(())
}

/// The next `, input_type=` that ends a value; a value can hold those words. Past the text's end if none does.
fn input_type_at(text: &[u16], from: isize) -> isize {
    let mut at = index_of(text, INPUT_TYPE, from);
    while at != -1 {
        // What's after it where it does end a value: the type's name and the bracket that closes the error's.
        let mut index = at + INPUT_TYPE.len() as isize;
        loop {
            let current = unit(text, index);
            if current == CLOSE_BRACKET {
                return at;
            }
            if current == -1
                || current == SPACE
                || current == TAB
                || current == CARRIAGE_RETURN
                || current == LINE_FEED
                || current == APOSTROPHE
                || current == QUOTE
                || current == EQUALS
            {
                break;
            }
            index += 1;
        }
        at = index_of(text, INPUT_TYPE, at + 1);
    }
    text.len() as isize + 1
}

/// A line's end as text printed once or more has it, from `from` on: backslashes, none before them, then `n` or `r`.
fn written_line_end(text: &[u16], from: isize) -> isize {
    let length = text.len() as isize;
    let mut index = from.max(0);
    while index < length {
        if unit(text, index) != BACKSLASH {
            index += 1;
            continue;
        }
        let mut end = index;
        while unit(text, end) == BACKSLASH {
            end += 1;
        }
        let next = unit(text, end);
        if unit(text, index - 1) != BACKSLASH && (next == 110 || next == 114) {
            return index;
        }
        index = end;
    }
    length
}

/// What a value printed by repr holds, so a string shares its number with the same value elsewhere. `None`: pydantic cut it.
fn repr_text(value: &[u16]) -> Option<Vec<u16>> {
    if contains(value, "...") {
        return None;
    }
    let first = unit(value, 0);
    if value.len() > 1
        && (first == APOSTROPHE || first == QUOTE)
        && unit(value, value.len() as isize - 1) == first
    {
        return Some(value[1..value.len() - 1].to_vec());
    }
    Some(value.to_vec())
}

/// A URL's parameters: `?token=abc&api_key=def`, a fragment's `#access_token=abc`, and a URL held
/// in a parameter, percent-encoded once or more. Their names follow the same rule as keys', and a
/// few go only here: a signed URL's `sig`, an OAuth `code`.
fn scan_queries(view: &View, reading: &mut Reading) -> Scan<()> {
    scan_parameters(view, reading)?;
    if !has_percent_equals(&view.text) {
        return Ok(());
    }
    let mut current = percent_decoded(view, reading)?;
    for level in 1.. {
        scan_parameters(&current, reading)?;
        if level == MAX_PERCENT || !has_percent_equals(&current.text) {
            break;
        }
        current = percent_decoded(&current, reading)?;
    }
    Ok(())
}

fn scan_parameters(view: &View, reading: &mut Reading) -> Scan<()> {
    let text = &view.text;
    let mut equals = index_of_unit(text, EQUALS, 0);
    while equals != -1 {
        let limit = (equals - KEY_READ).max(0);
        let mut start = equals;
        while start > limit && is_query_name(unit(text, start - 1)) {
            start -= 1;
        }
        let mut next = equals + 1;
        if start < equals && unit(text, start) != CLOSE_BRACKET && starts_parameter(text, start) {
            spend(reading.work, 1 + ((equals - start) >> 4) as i64)?;
            let name = parameter_name(slice(text, start, equals));
            if reading.is_secret_key(&name) || is_url_only_name(&name) {
                next = read_query_value(view, next, reading);
            }
        }
        equals = index_of_unit(text, EQUALS, next);
    }
    Ok(())
}

/// Parameters that hold secrets only in a URL: a signed URL's, and an OAuth or Azure Functions `code`.
fn is_url_only_name(name: &[u16]) -> bool {
    let lower: Vec<u16> = name
        .iter()
        .map(|unit| {
            if (65..=90).contains(unit) {
                unit + 32
            } else {
                *unit
            }
        })
        .collect();
    if lower == "code".encode_utf16().collect::<Vec<_>>()
        || lower == "code_verifier".encode_utf16().collect::<Vec<_>>()
    {
        return true;
    }
    ["sig", "signature", "hmac"].iter().any(|suffix| {
        ends_with(&lower, suffix) && {
            let before = lower.len() - suffix.len();
            before == 0 || !is_word(i32::from(lower[before - 1]))
        }
    })
}

/// A name as it was before encoding, without PHP's and Rails' `[]` at its end.
fn parameter_name(name: &[u16]) -> Vec<u16> {
    let mut decoded = Vec::with_capacity(name.len());
    let mut index = 0;
    while index < name.len() {
        if i32::from(name[index]) == PERCENT
            && index + 2 < name.len()
            && is_hex_unit(i32::from(name[index + 1]))
            && is_hex_unit(i32::from(name[index + 2]))
        {
            decoded.push(parse_hex(&name[index + 1..index + 3]) as u16);
            index += 3;
        } else {
            decoded.push(name[index]);
            index += 1;
        }
    }
    let mut end = decoded.len() as isize;
    while unit(&decoded, end - 1) == CLOSE_BRACKET {
        let mut open = end - 2;
        while open >= 0 && is_digit(unit(&decoded, open)) {
            open -= 1;
        }
        if open < 0 || unit(&decoded, open) != OPEN_BRACKET {
            break;
        }
        end = open;
    }
    decoded.truncate(end as usize);
    decoded
}

fn is_query_name(current: i32) -> bool {
    is_word(current)
        || current == UNDERSCORE
        || current == MINUS
        || current == PERIOD
        || current == TILDE
        || current == PERCENT
        || current == PLUS
        || current == OPEN_BRACKET
        || current == CLOSE_BRACKET
}

/// After `?`, `&`, `#`, a hash route's `#/`, `#!` or `#!/`, or `&` as HTML writes it. Not code's `&&`.
fn starts_parameter(text: &[u16], start: isize) -> bool {
    let current = unit(text, start - 1);
    if current == QUESTION || current == HASH {
        return true;
    }
    if current == AMPERSAND {
        return unit(text, start - 2) != AMPERSAND;
    }
    if current == SEMICOLON {
        return starts_with(text, "&amp", start - 5);
    }
    if current == SLASH || current == BANG {
        return unit(text, start - 2) == HASH
            || (current == SLASH && starts_with(text, "#!", start - 3));
    }
    false
}

/// A query value that is a template, not a value: `{token}`, `{{token}}`, `$TOKEN`, `${TOKEN}`. Its length.
fn query_template(text: &[u16]) -> Option<isize> {
    let name = |unit: i32| is_word(unit) || unit == UNDERSCORE || unit == PERIOD || unit == MINUS;
    let names = |from: isize| {
        let mut index = from;
        while name(unit(text, index)) {
            index += 1;
        }
        index
    };
    // `{name}`.
    if unit(text, 0) == OPEN_BRACE {
        let end = names(1);
        if end > 1 && unit(text, end) == CLOSE_BRACE {
            return Some(end + 1);
        }
    }
    // `{{ name }}`.
    if starts_with(text, "{{", 0) && text.len() >= 2 {
        let from = if unit(text, 2) == SPACE { 3 } else { 2 };
        let end = names(from);
        if end > from {
            let close = if unit(text, end) == SPACE {
                end + 1
            } else {
                end
            };
            if unit(text, close) == CLOSE_BRACE && unit(text, close + 1) == CLOSE_BRACE {
                return Some(close + 2);
            }
        }
    }
    // `${name}`.
    if unit(text, 0) == 36 && unit(text, 1) == OPEN_BRACE {
        let end = names(2);
        if end > 2 && unit(text, end) == CLOSE_BRACE {
            return Some(end + 1);
        }
    }
    // `$NAME`.
    if unit(text, 0) == 36 && (65..=90).contains(&unit(text, 1)) {
        let mut index = 2;
        while (65..=90).contains(&unit(text, index))
            || is_digit(unit(text, index))
            || unit(text, index) == UNDERSCORE
        {
            index += 1;
        }
        return Some(index);
    }
    None
}

/// Adds the span of the parameter's value at `at`. Where scanning goes on.
fn read_query_value(view: &View, at: isize, reading: &mut Reading) -> isize {
    let text = &view.text;
    let length = view.len();
    let mut end = at;
    while end < length && !is_query_value_end(unit(text, end)) {
        end += 1;
    }
    // A placeholder's bracket ends a value, so it is looked for by its own shape.
    if unit(text, at) == OPEN_BRACKET {
        let close = index_of_unit(slice(text, at, at + LONGEST_PLACEHOLDER), CLOSE_BRACKET, 0);
        if close != -1 && is_placeholder(view, at, at + close + 1) {
            return at + close + 1;
        }
    }
    if let Some(template) = query_template(slice(text, at, at + KEY_READ)) {
        let after = at + template;
        if after >= length || is_query_value_end(unit(text, after)) {
            return after;
        }
    }
    if end > at {
        push(
            view,
            reading,
            at,
            end,
            Some(slice(text, at, end).to_vec()),
            "",
        );
    }
    end
}

/// `%XX` for an ASCII character decoded, keeping where each character came from. Anything else stays as it is.
fn percent_decoded(view: &View, reading: &mut Reading) -> Scan<View> {
    let text = &view.text;
    let length = view.len();
    let mut out: Vec<u16> = Vec::with_capacity(text.len());
    let mut map: Vec<isize> = Vec::with_capacity(text.len() + 1);
    spend(reading.work, 1 + (length >> 4) as i64)?;
    let mut index = 0;
    loop {
        let found = index_of_unit(text, PERCENT, index);
        let stop = if found == -1 { length } else { found };
        for at in index..stop {
            out.push(text[at as usize]);
            map.push(position(view, at));
        }
        if found == -1 {
            break;
        }
        spend(reading.work, 1)?;
        let byte = has_hex(text, found + 1, 2, length)
            .then(|| parse_hex(slice(text, found + 1, found + 3)));
        match byte {
            Some(byte) if byte < 0x80 => {
                out.push(byte as u16);
                index = found + 3;
            }
            _ => {
                out.push(PERCENT as u16);
                index = found + 1;
            }
        }
        map.push(position(view, found));
    }
    map.push(position(view, length));
    Ok(reading.view(out, map))
}

/// A key at the nesting limit: a quote, whitespace and a colon, each as it is or encoded any number of times.
fn has_encoded_key(text: &[u16]) -> bool {
    let length = text.len() as isize;
    let backslashes = |from: isize| {
        let mut index = from;
        while unit(text, index) == BACKSLASH {
            index += 1;
        }
        index
    };
    let mut start = 0;
    while start < length {
        let mut index = if unit(text, start) == QUOTE {
            start + 1
        } else if starts_with(text, "\\u0022", start) {
            start + 6
        } else {
            start += 1;
            continue;
        };
        loop {
            let current = unit(text, index);
            if current == SPACE
                || current == TAB
                || current == CARRIAGE_RETURN
                || current == LINE_FEED
            {
                index += 1;
                continue;
            }
            if current == COLON {
                return true;
            }
            if current != BACKSLASH {
                break;
            }
            let after = backslashes(index);
            if is_one_of(unit(text, after), "nrt") {
                index = after + 1;
            } else if ["u0020", "u0009", "u000a", "u000A", "u000d", "u000D"]
                .iter()
                .any(|escape| starts_with(text, escape, after) && after + 5 <= length)
            {
                index = after + 5;
            } else if (starts_with(text, "u003a", after) || starts_with(text, "u003A", after))
                && after + 5 <= length
            {
                return true;
            } else {
                break;
            }
        }
        start += 1;
    }
    false
}

/// JSON in a string: its quotes, colons and spaces are all encoded, so it is scanned decoded.
fn nest(
    view: &View,
    start: isize,
    end: isize,
    depth: u32,
    printed: bool,
    reading: &mut Reading,
) -> Scan<()> {
    let inner = decode(view, start, end, reading)?;
    // With nothing decoded, it holds no more than it shows: a Windows path, a regular expression.
    if inner.len() == end - start || !may_hold_key_units(&inner.text) {
        return Ok(());
    }
    if depth < MAX_NESTING {
        read(&inner, depth + 1, printed, reading)?;
    } else if has_encoded_key(&inner.text) {
        push(view, reading, start, end, None, "");
    }
    Ok(())
}

/// Adds the span of the value at `at`, if it can hold a secret. Where scanning goes on; `None`: it ran to the end of the text.
fn read_value(
    view: &View,
    at: isize,
    level: u32,
    entry: bool,
    reading: &mut Reading,
) -> Scan<Option<isize>> {
    let text = &view.text;
    let length = view.len();
    if at >= length {
        return Ok(Some(at));
    }
    let first = unit(text, at);
    if first == QUOTE {
        // Replaced already, whatever follows it: a string a hook changed is read a second time.
        let quote_at = lex(text, at + 1).0;
        let whole = line_end(text, at + 1, quote_at).unwrap_or(quote_at);
        if is_placeholder(view, at + 1, whole) {
            return Ok((whole < length).then_some(whole + 1));
        }
        let (end, escaped) = value_end(text, at + 1, false, reading.work)?;
        let closed = end < length;
        if end > at + 1 {
            let identity = if closed {
                Some(body(view, at + 1, end, escaped, reading)?)
            } else {
                None
            };
            push(view, reading, at + 1, end, identity, "");
        }
        return Ok(closed.then_some(end + 1));
    }
    // Quoted any other way: `"password": 'x'`, as a repr has for a key with an apostrophe.
    if python_quote_at(text, at).is_some() {
        return read_python_value(view, at, reading);
    }
    // Only a quoted string is a value wherever its key is: not `Post "https://x/token": timeout`, or `case "token": return x`.
    if !entry {
        return Ok(Some(at));
    }
    let quote = format!("{}\"", "\\".repeat((1usize << level) - 1));
    if first == OPEN_BRACE || first == OPEN_BRACKET {
        let close = match container_end(text, at, reading.work)? {
            Container::NotJson => return read_python_value(view, at, reading),
            Container::Open => {
                push(view, reading, at, length, None, &quote);
                return Ok(None);
            }
            Container::End(close) => close,
        };
        if is_placeholder(view, at, close) {
            return Ok(Some(close));
        }
        if ends_token(text, close) {
            if skip_space(text, at + 1) == close - 1 {
                return Ok(Some(close));
            }
            push(
                view,
                reading,
                at,
                close,
                Some(slice(text, at, close).to_vec()),
                &quote,
            );
            return Ok(Some(close));
        }
        // What's written before a container's contents: `[Object: null prototype] { a: 'b' }`.
        if opens_container(unit(
            text,
            if unit(text, close) == SPACE {
                close + 1
            } else {
                close
            },
        )) {
            return read_python_value(view, at, reading);
        }
        return Ok(Some(bare_value(view, at, close, &quote, reading)));
    }
    // A container with a name: `Foo(a='b')`, `Set(2) { 'a' }`.
    if python_bracket_at(text, at).is_some() {
        return read_python_value(view, at, reading);
    }
    for literal in PYTHON_LITERALS {
        if starts_with(text, literal, at) && ends_token(text, at + literal.len() as isize) {
            return Ok(Some(at + literal.len() as isize));
        }
    }
    let number = json_number_end(text, at);
    if number > at && ends_token(text, number) {
        push(
            view,
            reading,
            at,
            number,
            Some(slice(text, at, number).to_vec()),
            &quote,
        );
        return Ok(Some(number));
    }
    Ok(Some(bare_value(view, at, at, &quote, reading)))
}

/// A value that isn't JSON, from `at`, read on from `from` up to the end of its line or the next comma or closing bracket.
fn bare_value(view: &View, at: isize, from: isize, quote: &str, reading: &mut Reading) -> isize {
    let text = &view.text;
    let length = view.len();
    let mut next = from;
    while next < length && !ends_bare(unit(text, next)) {
        next += 1;
    }
    let mut end = next;
    while end > at && is_space(unit(text, end - 1)) {
        end -= 1;
    }
    if end > at {
        push(
            view,
            reading,
            at,
            end,
            Some(slice(text, at, end).to_vec()),
            quote,
        );
    }
    next
}

/// Where a string value ends: at an unescaped quote that something able to follow a string
/// follows. So a value with a quote left unescaped in it, as hand-built JSON has, is read whole,
/// up to a line that starts as JSON's do.
fn value_end(
    text: &[u16],
    from: isize,
    in_container: bool,
    work: &mut ScanWork,
) -> Scan<(isize, bool)> {
    let length = text.len() as isize;
    let mut escaped = false;
    let mut index = from;
    loop {
        spend(work, 1)?;
        let (end, piece_escaped) = lex(text, index);
        escaped |= piece_escaped;
        if index > from {
            if let Some(line) = entry_line_end(text, index, end) {
                return Ok((line, escaped));
            }
        }
        if end >= length {
            return Ok((length, escaped));
        }
        if ends_string(text, end + 1, in_container) {
            return Ok((end, escaped));
        }
        index = end + 1;
    }
}

/// The end of the first line in the range whose next line starts with a quote or a closing bracket.
fn entry_line_end(text: &[u16], from: isize, to: isize) -> Option<isize> {
    let mut index = from;
    while index < to {
        let current = unit(text, index);
        if current != LINE_FEED && current != CARRIAGE_RETURN {
            index += 1;
            continue;
        }
        let next = skip_space(text, index + 1);
        let first = unit(text, next);
        if first == QUOTE || first == CLOSE_BRACE || first == CLOSE_BRACKET {
            return Some(index);
        }
        index = next;
    }
    None
}

fn line_end(text: &[u16], from: isize, to: isize) -> Option<isize> {
    (from..to).find(|index| {
        let current = unit(text, *index);
        current == LINE_FEED || current == CARRIAGE_RETURN
    })
}

/// A line's end, as text printed more than once has it: any number of backslashes, then `n` or `r`.
fn is_written_line_end(text: &[u16], at: isize) -> bool {
    let mut index = at;
    while unit(text, index) == BACKSLASH {
        index += 1;
    }
    index > at && (unit(text, index) == 110 || unit(text, index) == 114)
}

/// Whether what's at `at`, after spaces and tabs, can follow a number, a literal or a container.
fn ends_token(text: &[u16], at: isize) -> bool {
    let index = skip_blank(text, at);
    index >= text.len() as isize || ends_bare(unit(text, index)) || is_line_end(text, index)
}

/// Whether what's at `at`, after spaces and tabs, can follow a string: the text's or a line's end,
/// a comma that a value or a line's end follows, a closing bracket with no word straight after it,
/// what ends a sentence, or inside a container, a key's colon or arrow.
fn ends_string(text: &[u16], at: isize, in_container: bool) -> bool {
    let index = skip_blank(text, at);
    if index >= text.len() as isize || is_line_end(text, index) {
        return true;
    }
    let current = unit(text, index);
    if current == COMMA {
        return follows_comma(text, skip_blank(text, index + 1));
    }
    if current == CLOSE_BRACE || current == CLOSE_BRACKET {
        return !is_word(unit(text, index + 1));
    }
    (in_container && (current == COLON || starts_with(text, "=>", index)))
        || ends_sentence(text, index)
}

fn ends_sentence(text: &[u16], at: isize) -> bool {
    let length = text.len() as isize;
    let mut index = at;
    while index < length && is_sentence_end(unit(text, index)) {
        index += 1;
    }
    index > at && (index >= length || is_space(unit(text, index)) || is_line_end(text, index))
}

fn follows_comma(text: &[u16], at: isize) -> bool {
    if at >= text.len() as isize || is_line_end(text, at) {
        return true;
    }
    let current = unit(text, at);
    if current == QUOTE
        || current == APOSTROPHE
        || current == BACKTICK
        || current == OPEN_BRACE
        || current == OPEN_BRACKET
        || current == CLOSE_BRACE
        || current == CLOSE_BRACKET
        || current == MINUS
    {
        return true;
    }
    is_digit(current)
        || LITERALS
            .iter()
            .any(|literal| starts_with(text, literal, at))
}

/// A line's end, written as it is or as printed JSON has it.
fn is_line_end(text: &[u16], at: isize) -> bool {
    let current = unit(text, at);
    if current == LINE_FEED || current == CARRIAGE_RETURN {
        return true;
    }
    if current != BACKSLASH {
        return false;
    }
    let next = unit(text, at + 1);
    next == 110 || next == 114
}

enum Container {
    /// A container with a string in other quotes: it's not JSON.
    NotJson,
    /// Its brackets don't match, or it never closes.
    Open,
    /// Past its closing bracket.
    End(isize),
}

fn container_end(text: &[u16], at: isize, work: &mut ScanWork) -> Scan<Container> {
    let length = text.len() as isize;
    let mut open: Vec<i32> = Vec::new();
    let mut index = at;
    while index < length {
        let current = unit(text, index);
        if current == QUOTE {
            index = value_end(text, index + 1, true, work)?.0 + 1;
            continue;
        }
        if current == APOSTROPHE || current == BACKTICK {
            return Ok(Container::NotJson);
        }
        if current == OPEN_BRACE || current == OPEN_BRACKET {
            spend(work, 1)?;
            open.push(current);
        } else if current == CLOSE_BRACE || current == CLOSE_BRACKET {
            spend(work, 1)?;
            if open.pop() != Some(current - 2) {
                return Ok(Container::Open);
            }
            if open.is_empty() {
                return Ok(Container::End(index + 1));
            }
        }
        index += 1;
    }
    Ok(Container::Open)
}

/// The next unescaped quote from `from`, or the end of the text, and whether a backslash came before it.
fn lex(text: &[u16], from: isize) -> (isize, bool) {
    let length = text.len() as isize;
    let mut escaped = false;
    let mut index = from;
    while index < length {
        let current = unit(text, index);
        if current == QUOTE {
            return (index, escaped);
        }
        if current == BACKSLASH {
            escaped = true;
            index += 1;
        }
        index += 1;
    }
    (length, escaped)
}

fn body(
    view: &View,
    start: isize,
    end: isize,
    escaped: bool,
    reading: &mut Reading,
) -> Scan<Vec<u16>> {
    if escaped {
        Ok(decode(view, start, end, reading)?.text)
    } else {
        Ok(slice(&view.text, start, end).to_vec())
    }
}

/// The escapes printed text and YAML have that JSON doesn't, besides those that take digits.
fn other_escape(current: i32) -> Option<u16> {
    Some(match u8::try_from(current).ok()? {
        b'\'' => 0x27,
        b' ' => 0x20,
        b'0' => 0,
        b'a' => 0x07,
        b'e' => 0x1b,
        b'v' => 0x0b,
        b'N' => 0x85,
        b'_' => 0xa0,
        b'L' => 0x2028,
        b'P' => 0x2029,
        _ => return None,
    })
}

fn escaped_char(current: i32) -> Option<u16> {
    Some(match u8::try_from(current).ok()? {
        b'"' => 0x22,
        b'\\' => 0x5c,
        b'/' => 0x2f,
        b'b' => 0x08,
        b'f' => 0x0c,
        b'n' => 0x0a,
        b'r' => 0x0d,
        b't' => 0x09,
        _ => return None,
    })
}

/// Escapes decoded, keeping where each character came from: JSON's, and those printed text and
/// YAML have, including YAML's folded lines. Anything else stays as it is, so a truncated escape
/// or a Windows path does no harm. JSON itself holds none of the others.
fn decode(view: &View, start: isize, end: isize, reading: &mut Reading) -> Scan<View> {
    let text = &view.text;
    let mut out: Vec<u16> = Vec::with_capacity((end - start).max(0) as usize);
    let mut map: Vec<isize> = Vec::with_capacity((end - start).max(0) as usize + 1);
    spend(reading.work, 1 + ((end - start) >> 4) as i64)?;
    let mut index = start;
    while index < end {
        let found = index_of_unit(text, BACKSLASH, index);
        let stop = if found == -1 || found >= end {
            end
        } else {
            found
        };
        for at in index..stop {
            out.push(text[at as usize]);
            map.push(position(view, at));
        }
        if stop == end {
            break;
        }
        spend(reading.work, 1)?;
        let next = if found + 1 < end {
            unit(text, found + 1)
        } else {
            -1
        };
        let mut chars: Vec<u16> = Vec::with_capacity(2);
        let mut length = 2;
        if next == LOWER_U && has_hex(text, found + 2, 4, end) {
            chars.push(parse_hex(slice(text, found + 2, found + 6)) as u16);
            length = 6;
        } else if next == 120 && has_hex(text, found + 2, 2, end) {
            chars.push(parse_hex(slice(text, found + 2, found + 4)) as u16);
            length = 4;
        } else if next == 85
            && has_hex(text, found + 2, 8, end)
            && parse_hex(slice(text, found + 2, found + 10)) <= MAX_CODE_POINT
        {
            let code = parse_hex(slice(text, found + 2, found + 10));
            if code >= 0x10000 {
                let offset = code - 0x10000;
                chars.push(0xd800 + (offset >> 10) as u16);
                chars.push(0xdc00 + (offset & 0x3ff) as u16);
            } else {
                chars.push(code as u16);
            }
            length = 10;
        } else if next == LINE_FEED || next == CARRIAGE_RETURN {
            length = fold_end(text, found + 1, end) - found;
        } else if let Some(decoded) = escaped_char(next).or_else(|| other_escape(next)) {
            chars.push(decoded);
        } else {
            chars.push(BACKSLASH as u16);
            length = 1;
        }
        let from = position(view, found);
        for unit in chars {
            out.push(unit);
            map.push(from);
        }
        index = found + length;
    }
    map.push(position(view, end));
    Ok(reading.view(out, map))
}

fn has_hex(text: &[u16], at: isize, count: isize, end: isize) -> bool {
    at + count <= end && (at..at + count).all(|index| is_hex_unit(unit(text, index)))
}

/// Past the line's end at `at` and the next line's indentation.
fn fold_end(text: &[u16], at: isize, end: isize) -> isize {
    let mut index = at
        + if unit(text, at) == CARRIAGE_RETURN && unit(text, at + 1) == LINE_FEED && at + 1 < end {
            2
        } else {
            1
        };
    while index < end && (unit(text, index) == SPACE || unit(text, index) == TAB) {
        index += 1;
    }
    index
}

/// Spelled out as a placeholder in the string given, so that its number is already taken.
fn is_placeholder(view: &View, start: isize, end: isize) -> bool {
    if end - start > LONGEST_PLACEHOLDER
        || position(view, end) - position(view, start) != end - start
    {
        return false;
    }
    let text = slice(&view.text, start, end);
    let Some(open) = ["[EMAIL_", "[PHONE_", "[CARD_", "[SECRET_", "[IP_"]
        .iter()
        .find(|label| starts_with(text, label, 0) && text.len() >= label.len())
    else {
        return false;
    };
    let digits = &text[open.len()..];
    digits.len() >= 2
        && digits.len() <= 10
        && i32::from(digits[digits.len() - 1]) == CLOSE_BRACKET
        && digits[..digits.len() - 1]
            .iter()
            .all(|digit| is_digit(i32::from(*digit)))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::shape::KeyNames;

    fn spans(text: &str) -> Vec<(String, Option<String>, String)> {
        let keys = KeyNames(vec![
            "password".into(),
            "token".into(),
            "apikey".into(),
            "secret".into(),
        ]);
        let mut work = ScanWork { left: SCAN_WORK };
        secret_spans(text, &|key| keys.matches(key), &mut work)
            .into_iter()
            .map(|span| {
                (
                    text[span.start..span.end].to_owned(),
                    span.identity,
                    span.quote,
                )
            })
            .collect()
    }

    fn values(text: &str) -> Vec<String> {
        spans(text).into_iter().map(|(value, _, _)| value).collect()
    }

    #[test]
    fn values_under_secret_key_names_in_json() {
        assert_eq!(
            values(r#"{"user":"ann","password":"hunter2","n":1}"#),
            ["hunter2"]
        );
        assert_eq!(
            spans(r#"{"password": 12345, "x": 1}"#),
            [(
                "12345".to_owned(),
                Some("12345".to_owned()),
                "\"".to_owned()
            )]
        );
        assert_eq!(
            values(r#"{"token": {"a": [1, 2]}, "x": 1}"#),
            [r#"{"a": [1, 2]}"#]
        );
        assert!(
            values(r#"{"password": null, "token": true, "apiKey": "", "secret": {}}"#).is_empty()
        );
        assert!(values(r#"{"password": "[SECRET_3]"}"#).is_empty());
    }

    #[test]
    fn json_inside_a_string_is_read_decoded() {
        assert_eq!(
            values(r#"{"body":"{\"password\":\"hunter2\"}"}"#),
            ["hunter2"]
        );
        assert_eq!(
            spans(r#"{"body":"{\"password\":5}"}"#),
            [("5".to_owned(), Some("5".to_owned()), "\\\"".to_owned())]
        );
    }

    #[test]
    fn code_and_sentences_are_left_alone() {
        assert!(values(r#"Post "https://x/token": timeout"#).is_empty());
        assert!(values(r#"case "token": return x"#).is_empty());
        assert!(values(r#"cond ? "token" : "x""#).is_empty());
        assert_eq!(
            values(r#""password": correct horse, next"#),
            ["correct horse"]
        );
    }

    #[test]
    fn python_reprs_and_urls() {
        assert_eq!(values("{'password': 'x', 'note': \"it's\"}"), ["x"]);
        assert_eq!(
            values("https://h/cb?token=abc&ok=1#access_token=def"),
            ["abc", "def"]
        );
        assert_eq!(values("https://h/cb?token={token}&sig=xyz"), ["xyz"]);
        assert_eq!(values("Input should be a valid string [type=string_type, input_value=123, input_type=int]"), ["123"]);
    }

    #[test]
    fn values_that_differ_only_in_a_written_lone_surrogate_differ() {
        let found = spans(r#"{"body":"{\"token\":\"\\ud800\",\"password\":\"\\ud801\"}"}"#);
        assert_eq!(found.len(), 2);
        assert_ne!(found[0].1, found[1].1);
    }

    #[test]
    fn spans_are_byte_ranges_after_wide_characters() {
        assert_eq!(values("😀é{\"token\":\"x😀\"} é"), ["x😀"]);
    }

    #[test]
    fn work_past_the_limit_replaces_the_string_whole() {
        let keys = KeyNames(vec!["password".into()]);
        let mut work = ScanWork { left: 3 };
        let text = r#"{"a":"b","c":"d","password":"x"}"#;
        let found = secret_spans(text, &|key| keys.matches(key), &mut work);
        assert_eq!(
            found,
            [SecretSpan {
                start: 0,
                end: text.len(),
                identity: None,
                quote: String::new()
            }]
        );
    }
}
