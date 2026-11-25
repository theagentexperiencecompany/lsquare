//! Checks what an event is made of: IDs, short fields, metadata, and a turn's own fields (a call's
//! `agent` part). A port of `packages/lsquare/src/normalize.ts`, without what exists there only
//! for JavaScript values.

use serde_json::{Map, Number, Value};

use crate::json::{advance, js_entries, js_held, js_number, longer_than, stringify, utf16_len};
use crate::redact::{Redactor, FREE_FORM, JSON_FIELD};
use crate::shape::{
    shape_json, shape_string, spend, ShapeState, Shaped, LIMIT_AGENT, LIMIT_INPUT, MAX_ENTRIES,
};

/// The ingestion's limit for IDs, and for event IDs.
pub(crate) const MAX_ID_BYTES: usize = 512;
/// Short fields kept as they are (`surface`, `apiKeyId`, names, types): longer ones are dropped as too_large.
pub(crate) const MAX_SHORT_CHARS: usize = 16_000;
/// A larger turnId couldn't fit in an event: dropped as too_large before it's hashed.
const MAX_TURN_ID_BYTES: usize = 256 * 1024;
/// Work allowed to measure the non-string tool results of one event, which are never sent.
const RESULT_MEASURE_LIMIT: i64 = 1_000_000;
/// Nesting a measured result may have.
const MAX_MEASURE_DEPTH: usize = 64;
const MAX_SAFE_INTEGER: f64 = 9_007_199_254_740_991.0;

/// Where shaping reports what it removed and shortened, for one event.
#[derive(Debug, Default, Clone)]
pub(crate) struct Notes {
    /// Path to reason. Paths never contain keys from the app's data.
    pub dropped: Map<String, Value>,
    /// Top-level fields shortened, in the order they were.
    pub truncated: Vec<String>,
}

impl Notes {
    pub(crate) fn drop(&mut self, path: impl Into<String>, reason: &'static str) {
        self.dropped
            .insert(path.into(), Value::String(reason.into()));
    }

    pub(crate) fn truncate(&mut self, name: &str) {
        if !self.truncated.iter().any(|known| known == name) {
            self.truncated.push(name.to_owned());
        }
    }

    pub(crate) fn merge(&mut self, from: &Notes) {
        for (path, reason) in &from.dropped {
            self.dropped.insert(path.clone(), reason.clone());
        }
        for name in &from.truncated {
            self.truncate(name);
        }
    }
}

/// What a check makes of a value: nothing was given, it is kept, or it is removed for a reason.
pub(crate) enum Check<T> {
    Absent,
    Valid(T),
    Dropped(&'static str),
}

impl<T> Check<T> {
    /// Keeps a valid value; a removed one is noted at `path`.
    pub(crate) fn apply(self, path: &str, notes: &mut Notes) -> Option<T> {
        match self {
            Check::Absent => None,
            Check::Valid(value) => Some(value),
            Check::Dropped(reason) => {
                notes.drop(path, reason);
                None
            }
        }
    }
}

fn absent(value: Option<&Value>) -> bool {
    matches!(value, None | Some(Value::Null))
}

/// Shapes one field, recording it as truncated if anything in it was shortened.
pub(crate) fn shape_field<T>(
    state: &mut ShapeState,
    notes: &mut Notes,
    name: &str,
    run: impl FnOnce(&mut ShapeState, &mut Notes) -> T,
) -> T {
    let before = state.shortened;
    state.shortened = false;
    let out = run(state, notes);
    if state.shortened {
        notes.truncate(name);
    }
    state.shortened = before || state.shortened;
    out
}

/// A JSON field shaped at `limit`. Partly not kept as JSON: kept, and marked. `free_form`: its
/// keys come from the app's data, so secret key names apply.
pub(crate) fn shape_any(
    value: &Value,
    limit: usize,
    state: &mut ShapeState,
    notes: &mut Notes,
    path: &str,
    free_form: bool,
) -> Shaped {
    let lossy = state.lossy;
    state.lossy = false;
    state.secret_key = free_form;
    let shaped = shape_json(value, limit, state);
    if state.lossy {
        notes.drop(path, "not_json");
    }
    state.lossy = lossy || state.lossy;
    state.secret_key = false;
    shaped
}

/// A field that must be a string, shaped at `limit`.
pub(crate) fn shape_text(
    value: Option<&Value>,
    limit: usize,
    state: &mut ShapeState,
    notes: &mut Notes,
    path: &str,
) -> Option<Shaped> {
    match value {
        None | Some(Value::Null) => None,
        Some(Value::String(text)) => Some(shape_string(text, limit, state)),
        Some(_) => {
            notes.drop(path, "wrong_type");
            None
        }
    }
}

/// An ID: absent, a valid string, or invalid (the event is then dropped: sent without it, it could
/// slip past a deletion block). `""` counts as absent.
pub(crate) enum Id {
    Absent,
    Empty,
    Invalid,
    Valid(String),
}

pub(crate) fn id_value(value: Option<&Value>) -> Id {
    match value {
        None | Some(Value::Null) => Id::Absent,
        Some(Value::String(text)) if text.is_empty() => Id::Empty,
        Some(Value::String(text)) if text.len() > MAX_ID_BYTES || text.contains('\0') => {
            Id::Invalid
        }
        Some(Value::String(text)) => Id::Valid(text.clone()),
        Some(_) => Id::Invalid,
    }
}

/// Whether a string is a valid ID.
pub(crate) fn is_valid_id(text: &str) -> bool {
    !text.is_empty() && text.len() <= MAX_ID_BYTES && !text.contains('\0')
}

/// A turnId: a string, or a safe integer as its decimal string.
pub(crate) fn turn_id_value(value: Option<&Value>) -> Check<String> {
    match value {
        None | Some(Value::Null) => Check::Absent,
        Some(Value::String(text)) if text.is_empty() => Check::Dropped("empty"),
        Some(Value::String(text)) => {
            // Its UTF-8 bytes as the event writes it, in JSON without its quotes.
            if text.len() > MAX_TURN_ID_BYTES || stringify(text).len() - 2 > MAX_TURN_ID_BYTES {
                Check::Dropped("too_large")
            } else {
                Check::Valid(text.clone())
            }
        }
        Some(Value::Number(number)) => {
            let safe = number
                .as_f64()
                .is_some_and(|value| value.fract() == 0.0 && value.abs() <= MAX_SAFE_INTEGER);
            if safe {
                Check::Valid(js_number(number))
            } else {
                Check::Dropped("unsafe_integer")
            }
        }
        Some(_) => Check::Dropped("wrong_type"),
    }
}

/// A short field kept as it is: at most 16,000 code points.
pub(crate) fn short(value: Option<&Value>) -> Check<String> {
    match value {
        None | Some(Value::Null) => Check::Absent,
        Some(Value::String(text)) if longer_than(text, MAX_SHORT_CHARS) => {
            Check::Dropped("too_large")
        }
        Some(Value::String(text)) => Check::Valid(text.clone()),
        Some(_) => Check::Dropped("wrong_type"),
    }
}

fn boolean(value: Option<&Value>) -> Check<bool> {
    match value {
        None | Some(Value::Null) => Check::Absent,
        Some(Value::Bool(value)) => Check::Valid(*value),
        Some(_) => Check::Dropped("wrong_type"),
    }
}

fn number(value: Option<&Value>) -> Check<Number> {
    match value {
        None | Some(Value::Null) => Check::Absent,
        Some(Value::Number(number)) => Check::Valid(number.clone()),
        Some(_) => Check::Dropped("wrong_type"),
    }
}

fn status(value: Option<&Value>) -> Check<&'static str> {
    match value {
        None | Some(Value::Null) => Check::Absent,
        Some(Value::String(text)) if text == "completed" => Check::Valid("completed"),
        Some(Value::String(text)) if text == "error" => Check::Valid("error"),
        Some(_) => Check::Dropped("wrong_type"),
    }
}

/// `{ name, version? }`, as clients and servers name themselves. A version too long to keep is
/// left out, and recorded as too_large at `path.version`; one of another type is ignored.
pub(crate) fn named(value: Option<&Value>, notes: &mut Notes, path: &str) -> Check<Value> {
    let Some(Value::Object(fields)) = value else {
        return Check::Dropped("wrong_type");
    };
    let Some(Value::String(name)) = fields.get("name") else {
        return Check::Dropped("wrong_type");
    };
    if name.is_empty() {
        return Check::Dropped("wrong_type");
    }
    if longer_than(name, MAX_SHORT_CHARS) {
        return Check::Dropped("too_large");
    }
    let mut out = Map::new();
    out.insert("name".into(), Value::String(name.clone()));
    if let Some(Value::String(version)) = fields.get("version") {
        if longer_than(version, MAX_SHORT_CHARS) {
            notes.drop(format!("{path}.version"), "too_large");
        } else {
            out.insert("version".into(), Value::String(version.clone()));
        }
    }
    Check::Valid(Value::Object(out))
}

/// A turn's client: a non-empty string, or `{ name, version? }`.
fn client(value: Option<&Value>, notes: &mut Notes, path: &str) -> Check<Value> {
    match value {
        None | Some(Value::Null) => Check::Absent,
        Some(Value::String(text)) if !text.is_empty() => {
            if longer_than(text, MAX_SHORT_CHARS) {
                Check::Dropped("too_large")
            } else {
                Check::Valid(Value::String(text.clone()))
            }
        }
        Some(Value::Object(_)) => named(value, notes, path),
        Some(_) => Check::Dropped("wrong_type"),
    }
}

/// Shaped turn fields: strings still cuts until finished with a Redactor.
#[derive(Debug, Default, Clone)]
pub(crate) struct ShapedTurn {
    pub question: Option<Shaped>,
    pub answer: Option<Shaped>,
    pub turn_id: Option<String>,
    pub status: Option<&'static str>,
    pub error: Option<ShapedTurnError>,
    pub tool_calls: Option<Vec<ShapedToolCall>>,
    pub usage: Option<Map<String, Value>>,
    pub latency: Option<Shaped>,
    pub verification: Option<Shaped>,
    pub client: Option<Value>,
    pub api_key_id: Option<String>,
    pub surface: Option<String>,
}

#[derive(Debug, Clone)]
pub(crate) struct ShapedTurnError {
    pub message: Shaped,
    pub kind: Option<String>,
}

#[derive(Debug, Clone)]
pub(crate) struct ShapedToolCall {
    pub name: String,
    pub input: Option<Shaped>,
    pub result_chars: Option<Number>,
    /// Its length was measured from the result, which adds it after the other fields.
    pub measured: bool,
    pub result_is_error: Option<bool>,
    pub error_message: Option<Shaped>,
    pub duration_ms: Option<Number>,
}

/// Reads a turn's own fields (a `turn` event's, or a call's `agent`) into shaped form.
pub(crate) fn shape_turn_fields(
    input: &Map<String, Value>,
    turn_id: Option<&Value>,
    state: &mut ShapeState,
    notes: &mut Notes,
    prefix: &str,
) -> ShapedTurn {
    let mut body = ShapedTurn::default();
    let at = |path: &str| format!("{prefix}{path}");
    // A call's agent part is one field for `truncated`; a turn's fields are each their own.
    let field = |name: &str| {
        if prefix.is_empty() {
            name.to_owned()
        } else {
            prefix[..prefix.len() - 1].to_owned()
        }
    };
    let limit = LIMIT_AGENT;

    body.turn_id = turn_id_value(turn_id).apply(&at("turnId"), notes);
    body.status = status(input.get("status")).apply(&at("status"), notes);
    body.question = shape_field(state, notes, &field("question"), |state, notes| {
        shape_text(input.get("question"), limit, state, notes, &at("question"))
    });
    body.answer = shape_field(state, notes, &field("answer"), |state, notes| {
        shape_text(input.get("answer"), limit, state, notes, &at("answer"))
    });
    body.error = shape_field(state, notes, &field("error"), |state, notes| {
        error_value(input.get("error"), state, notes, &at("error"))
    });
    body.tool_calls = shape_field(state, notes, &field("toolCalls"), |state, notes| {
        tool_calls(input.get("toolCalls"), state, notes, &at("toolCalls"))
    });
    body.usage = usage_value(input.get("usage"), notes, &at("usage"));
    for key in ["latency", "verification"] {
        let Some(value) = input.get(key).filter(|value| !value.is_null()) else {
            continue;
        };
        let shaped = shape_field(state, notes, &field(key), |state, notes| {
            shape_any(value, limit, state, notes, &at(key), true)
        });
        if key == "latency" {
            body.latency = Some(shaped);
        } else {
            body.verification = Some(shaped);
        }
    }
    body.client = client(input.get("client"), notes, &at("client")).apply(&at("client"), notes);
    body.api_key_id = short(input.get("apiKeyId")).apply(&at("apiKeyId"), notes);
    body.surface = short(input.get("surface")).apply(&at("surface"), notes);
    body
}

/// Shaped turn fields to JSON. `all`: redact everything; otherwise only what was cut.
pub(crate) fn finish_turn_fields(
    body: &ShapedTurn,
    redactor: &mut Redactor,
    all: bool,
) -> Map<String, Value> {
    let mut out = Map::new();
    if let Some(question) = &body.question {
        out.insert(
            "question".into(),
            redactor.finalize_text(question, all).into(),
        );
    }
    if let Some(answer) = &body.answer {
        out.insert("answer".into(), redactor.finalize_text(answer, all).into());
    }
    if let Some(turn_id) = &body.turn_id {
        out.insert("turnId".into(), turn_id.clone().into());
    }
    if let Some(status) = body.status {
        out.insert("status".into(), status.into());
    }
    if let Some(error) = &body.error {
        let mut finished = Map::new();
        finished.insert(
            "message".into(),
            redactor.finalize_text(&error.message, all).into(),
        );
        if let Some(kind) = &error.kind {
            finished.insert(
                "type".into(),
                if all {
                    redactor.text(kind, false)
                } else {
                    kind.clone()
                }
                .into(),
            );
        }
        out.insert("error".into(), Value::Object(finished));
    }
    if let Some(calls) = &body.tool_calls {
        let finished: Vec<Value> = calls
            .iter()
            .map(|call| {
                let mut finished = Map::new();
                finished.insert("name".into(), call.name.clone().into());
                let chars = call.result_chars.clone().map(Value::Number);
                if let (Some(chars), false) = (&chars, call.measured) {
                    finished.insert("resultChars".into(), chars.clone());
                }
                if let Some(is_error) = call.result_is_error {
                    finished.insert("resultIsError".into(), is_error.into());
                }
                if let Some(duration) = &call.duration_ms {
                    finished.insert("durationMs".into(), Value::Number(duration.clone()));
                }
                if let (Some(chars), true) = (chars, call.measured) {
                    finished.insert("resultChars".into(), chars);
                }
                if let Some(input) = &call.input {
                    finished.insert(
                        "input".into(),
                        redactor.finalize(input, JSON_FIELD.with_all(all)),
                    );
                }
                if let Some(message) = &call.error_message {
                    finished.insert(
                        "errorMessage".into(),
                        redactor.finalize_text(message, all).into(),
                    );
                }
                Value::Object(finished)
            })
            .collect();
        out.insert("toolCalls".into(), Value::Array(finished));
    }
    if let Some(usage) = &body.usage {
        out.insert("usage".into(), Value::Object(usage.clone()));
    }
    if let Some(latency) = &body.latency {
        out.insert(
            "latency".into(),
            redactor.finalize(latency, FREE_FORM.with_all(all)),
        );
    }
    if let Some(verification) = &body.verification {
        out.insert(
            "verification".into(),
            redactor.finalize(verification, FREE_FORM.with_all(all)),
        );
    }
    if let Some(client) = &body.client {
        out.insert("client".into(), client.clone());
    }
    if let Some(api_key_id) = &body.api_key_id {
        out.insert("apiKeyId".into(), api_key_id.clone().into());
    }
    if let Some(surface) = &body.surface {
        out.insert("surface".into(), surface.clone().into());
    }
    out
}

/// Records placeholders already in shaped turn fields: in every one, redacted or not (a tool
/// call's name, `surface`, `client`), as a placeholder anywhere in the event is skipped.
pub(crate) fn reserve_turn_fields(body: &ShapedTurn, redactor: &mut Redactor) {
    for value in [
        &body.question,
        &body.answer,
        &body.latency,
        &body.verification,
    ]
    .into_iter()
    .flatten()
    {
        redactor.reserve(value);
    }
    for text in [&body.turn_id, &body.api_key_id, &body.surface]
        .into_iter()
        .flatten()
    {
        redactor.reserve_text(text);
    }
    if let Some(error) = &body.error {
        redactor.reserve(&error.message);
        if let Some(kind) = &error.kind {
            redactor.reserve_text(kind);
        }
    }
    for call in body.tool_calls.iter().flatten() {
        redactor.reserve_text(&call.name);
        for value in [&call.input, &call.error_message].into_iter().flatten() {
            redactor.reserve(value);
        }
    }
    if let Some(client) = &body.client {
        redactor.reserve_json(client);
    }
}

fn error_value(
    value: Option<&Value>,
    state: &mut ShapeState,
    notes: &mut Notes,
    path: &str,
) -> Option<ShapedTurnError> {
    match value {
        None | Some(Value::Null) => None,
        Some(Value::String(text)) => Some(ShapedTurnError {
            message: shape_string(text, LIMIT_AGENT, state),
            kind: None,
        }),
        Some(Value::Object(source)) => {
            let Some(Value::String(message)) = source.get("message") else {
                notes.drop(path, "wrong_type");
                return None;
            };
            let message = shape_string(message, LIMIT_AGENT, state);
            let named = match source.get("type") {
                Some(Value::String(_)) => source.get("type"),
                _ => source.get("name"),
            };
            let kind = match short(named) {
                Check::Valid(kind) if !kind.is_empty() => Some(kind),
                // Too long to keep, as other short fields are; one of another type is ignored.
                Check::Dropped("too_large") => {
                    notes.drop(format!("{path}.type"), "too_large");
                    None
                }
                _ => None,
            };
            Some(ShapedTurnError { message, kind })
        }
        Some(_) => {
            notes.drop(path, "wrong_type");
            None
        }
    }
}

fn tool_calls(
    value: Option<&Value>,
    state: &mut ShapeState,
    notes: &mut Notes,
    path: &str,
) -> Option<Vec<ShapedToolCall>> {
    let items = match value {
        None | Some(Value::Null) => return None,
        Some(Value::Array(items)) => items,
        Some(_) => {
            notes.drop(path, "wrong_type");
            return None;
        }
    };
    let mut out = Vec::new();
    let mut allowance = RESULT_MEASURE_LIMIT;
    if items.len() > MAX_ENTRIES {
        state.shortened = true;
    }
    for (index, item) in items.iter().take(MAX_ENTRIES).enumerate() {
        // One per item, as for any list: past the budget, no more tool calls are taken.
        if !spend(state, 1) {
            break;
        }
        let at = format!("{path}[{index}]");
        let Value::Object(fields) = item else {
            notes.drop(at, "wrong_type");
            continue;
        };
        let name = match short(fields.get("name")) {
            Check::Valid(name) if !name.is_empty() => name,
            // A call needs its name: one too long to keep drops it as too_large.
            Check::Dropped("too_large") => {
                notes.drop(at, "too_large");
                continue;
            }
            _ => {
                notes.drop(at, "missing_name");
                continue;
            }
        };
        let mut call = ShapedToolCall {
            name,
            input: None,
            result_chars: None,
            measured: false,
            result_is_error: None,
            error_message: None,
            duration_ms: None,
        };
        match fields.get("input") {
            None => {}
            // An explicit null input is kept: the tool was called with none.
            Some(Value::Null) => call.input = Some(Shaped::Null),
            Some(input) => {
                call.input = Some(shape_any(
                    input,
                    LIMIT_INPUT,
                    state,
                    notes,
                    &format!("{at}.input"),
                    true,
                ))
            }
        }
        call.result_chars =
            number(fields.get("resultChars")).apply(&format!("{at}.resultChars"), notes);
        call.result_is_error =
            boolean(fields.get("resultIsError")).apply(&format!("{at}.resultIsError"), notes);
        call.error_message = shape_text(
            fields.get("errorMessage"),
            LIMIT_AGENT,
            state,
            notes,
            &format!("{at}.errorMessage"),
        );
        call.duration_ms =
            number(fields.get("durationMs")).apply(&format!("{at}.durationMs"), notes);
        // Results never leave: keep only their length, in code points for a string.
        if call.result_chars.is_none() && !absent(fields.get("result")) {
            let length = match fields.get("result") {
                Some(Value::String(text)) => result_length(text, &mut allowance),
                Some(other) => json_length(other, &mut allowance),
                None => Err("not_json"),
            };
            match length {
                Ok(length) => {
                    call.result_chars = Some(Number::from(length as u64));
                    call.measured = true;
                }
                Err(reason) => notes.drop(format!("{at}.resultChars"), reason),
            }
        }
        out.push(call);
    }
    // Every item invalid means unknown, not "no tools were called".
    if out.is_empty() && !items.is_empty() {
        None
    } else {
        Some(out)
    }
}

fn result_length(text: &str, allowance: &mut i64) -> Result<usize, &'static str> {
    let (end, passed) = advance(text, 0, (*allowance).max(0) as usize + 1);
    *allowance -= passed as i64;
    if end == text.len() && *allowance >= 0 {
        Ok(passed)
    } else {
        Err("too_large")
    }
}

/// The exact length of a value's compact JSON, in code points, measured without keeping a copy,
/// within a shared work allowance.
fn json_length(value: &Value, allowance: &mut i64) -> Result<usize, &'static str> {
    fn measure(item: &Value, depth: usize, allowance: &mut i64) -> Option<usize> {
        *allowance -= 1;
        if *allowance < 0 || depth >= MAX_MEASURE_DEPTH {
            return None;
        }
        match item {
            Value::Null => Some(4),
            Value::Bool(value) => Some(if *value { 4 } else { 5 }),
            Value::Number(number) => Some(js_number(number).len()),
            Value::String(text) => {
                *allowance -= utf16_len(text) as i64;
                (*allowance >= 0).then(|| stringify(text).chars().count())
            }
            Value::Array(items) => {
                let mut total = 2 + items.len().saturating_sub(1);
                for element in items {
                    total += measure(element, depth + 1, allowance)?;
                }
                Some(total)
            }
            Value::Object(entries) => {
                let mut total = 2;
                let mut first = true;
                // In JavaScript's order, so the work runs out, or the depth does, at the same
                // entry as in the other SDKs. Each entry costs at least one of the allowance.
                let most = usize::try_from(*allowance).unwrap_or(0).saturating_add(1);
                for (key, element) in js_entries(entries, most) {
                    *allowance -= utf16_len(key) as i64;
                    if *allowance < 0 {
                        return None;
                    }
                    let length = measure(element, depth + 1, allowance)?;
                    total += usize::from(!first) + stringify(key).chars().count() + 1 + length;
                    first = false;
                }
                Some(total)
            }
        }
    }
    let length = measure(value, 0, allowance);
    if *allowance < 0 {
        return Err("too_large");
    }
    length.ok_or("not_json")
}

fn usage_value(value: Option<&Value>, notes: &mut Notes, path: &str) -> Option<Map<String, Value>> {
    let fields = match value {
        None | Some(Value::Null) => return None,
        Some(Value::Object(fields)) => fields,
        Some(_) => {
            notes.drop(path, "wrong_type");
            return None;
        }
    };
    let mut usage = Map::new();
    for key in ["inputTokens", "outputTokens", "costUsd"] {
        if let Some(value) = number(fields.get(key)).apply(&format!("{path}.{key}"), notes) {
            usage.insert(key.into(), Value::Number(value));
        }
    }
    (!usage.is_empty()).then_some(usage)
}

/// What metadata can be.
pub(crate) enum Metadata {
    None,
    /// Past its budget: the event is dropped.
    TooLarge,
    Some(Map<String, Value>),
}

/// 0.1's metadata: string, number, boolean and null values only, never shortened.
pub(crate) fn metadata_value(value: Option<&Value>, notes: &mut Notes, budget: i64) -> Metadata {
    let entries = match value {
        None | Some(Value::Null) => return Metadata::None,
        Some(Value::Object(entries)) => entries,
        Some(_) => {
            notes.drop("metadata", "wrong_type");
            return Metadata::None;
        }
    };
    let mut remaining = budget;
    let mut out = Map::new();
    let mut lossy = false;
    // Each entry costs at least one of the budget.
    let most = usize::try_from(budget).unwrap_or(0).saturating_add(1);
    for (key, item) in js_entries(entries, most) {
        remaining -= 1;
        if remaining < 0 {
            return Metadata::TooLarge;
        }
        let primitive = !matches!(item, Value::Array(_) | Value::Object(_));
        if primitive && key != "__proto__" {
            remaining -= (utf16_len(key) + item.as_str().map_or(1, utf16_len)) as i64;
            if remaining < 0 {
                return Metadata::TooLarge;
            }
            let held = match item {
                Value::Number(number) => Value::Number(js_held(number)),
                other => other.clone(),
            };
            out.insert(key.clone(), held);
        } else {
            lossy = true;
        }
    }
    if lossy {
        notes.drop("metadata", "not_json");
    }
    if out.is_empty() {
        Metadata::None
    } else {
        Metadata::Some(out)
    }
}

/// Metadata's size as its budget counts it: one per entry, its key, and a string's length.
pub(crate) fn metadata_size(metadata: &Map<String, Value>) -> i64 {
    metadata
        .iter()
        .map(|(key, value)| (1 + utf16_len(key) + value.as_str().map_or(1, utf16_len)) as i64)
        .sum()
}

/// Metadata to JSON: secret key names apply even before the hook; with `all`, everything is redacted.
pub(crate) fn finish_metadata(
    metadata: &Map<String, Value>,
    redactor: &mut Redactor,
    all: bool,
) -> Value {
    let shaped = Shaped::Object(
        metadata
            .iter()
            .map(|(key, value)| {
                let shaped = match value {
                    Value::String(text) => Shaped::String(text.clone()),
                    Value::Number(number) => Shaped::Number(number.clone()),
                    Value::Bool(value) => Shaped::Bool(*value),
                    _ => Shaped::Null,
                };
                (key.clone(), shaped)
            })
            .collect(),
    );
    redactor.finalize(&shaped, FREE_FORM.with_all(all))
}
