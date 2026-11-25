//! Builds events: a call's, from what the wrapper saw, and what `before_send` made of one. A port
//! of `packages/lsquare/src/event.ts`.
//!
//! A call is built in two steps. `shape_call` reads the call's own data within the limits, in a
//! fixed order which the scan budget makes part of the result; it borrows the data, so the wrapper
//! runs it before the tool's result leaves. `finish_call` adds who the call was for, redacts, and
//! fits the event; it runs once `identify` has answered.

use std::sync::LazyLock;

use regex::Regex;
use serde_json::{Map, Number, Value};

use crate::json::utf16_len;
use crate::normalize::{
    finish_metadata, finish_turn_fields, id_value, metadata_size, metadata_value, named,
    reserve_turn_fields, shape_any, shape_field, shape_text, shape_turn_fields, short, Check, Id,
    Metadata, Notes, ShapedTurn,
};
use crate::redact::{Pattern, Redactor, JSON_FIELD};
use crate::shape::{
    decoded_size, secret_string, shape_object, shape_string, spend, weight, KeyNames, ShapeState,
    Shaped, LIMIT_DESCRIPTION, LIMIT_INPUT, LIMIT_INTENT, LIMIT_RESULT, MAX_ENTRIES, SCAN_BUDGET,
};

/// Parts left out, in order, until an event fits: biggest and least needed first. A call's tool
/// description goes first of all: it is the same on every call of the tool, so nothing that only
/// this call has is left out to make room for it.
const CALL_ORDER: [&[&str]; 5] = [
    &["toolDescription"],
    &["result"],
    &["input"],
    &["agent", "toolCalls"],
    &["agent"],
];
#[cfg_attr(not(test), allow(dead_code))]
const TURN_ORDER: [&[&str]; 5] = [
    &["toolCalls"],
    &["answer"],
    &["question"],
    &["verification"],
    &["latency"],
];
/// Metadata's own budget, as for a turn: the whole event's size.
const METADATA_BUDGET: i64 = 256 * 1024;
const CATEGORIES: [&str; 4] = ["thrown", "tool_error", "protocol", "timeout"];
static TURN_REF: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\Ak/[0-9a-f]{64}\z").expect("turn reference pattern"));

/// What every event needs, besides its content.
#[derive(Debug, Clone)]
pub(crate) struct Context {
    /// The event's `sdk` field.
    pub sdk: Value,
    pub patterns: Vec<Pattern>,
    pub redact_keys: Vec<String>,
    pub max_event_bytes: usize,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Kind {
    #[cfg_attr(not(test), allow(dead_code))]
    Turn,
    ToolCall,
}

/// An event on its way: built, maybe waiting for `before_send`, then finished.
#[derive(Debug)]
pub(crate) struct Draft {
    pub kind: Kind,
    /// The event as `before_send` sees it, or as sent when there is none.
    pub event: Map<String, Value>,
    /// Keeps numbering across `before_send`.
    redactor: Redactor,
    /// Fields the hook can't change, restored from here.
    identity: Map<String, Value>,
    category: Option<&'static str>,
    /// The agent part's turnId, restored after the hook.
    agent_turn_id: Option<String>,
    diagnostics: bool,
    notes: Notes,
}

/// Why an event couldn't be built.
#[derive(Debug, PartialEq)]
pub(crate) struct Refused {
    /// `invalid_input`, `invalid_id` or `too_large`.
    pub code: &'static str,
    pub paths: Vec<String>,
}

pub(crate) type Built = Result<Box<Draft>, Refused>;

fn refused(code: &'static str, paths: &[&str]) -> Refused {
    Refused {
        code,
        paths: paths.iter().map(|path| (*path).to_owned()).collect(),
    }
}

/// MCP's result, as the wrapper has it or as JSON.
pub(crate) enum ResultIn<'a> {
    Json(&'a Value),
    Typed(TypedResult<'a>),
}

/// A result read from the MCP SDK's own types, which borrows its text and files.
pub(crate) struct TypedResult<'a> {
    /// Its first blocks, at most `MAX_ENTRIES`.
    pub blocks: Vec<BlockIn<'a>>,
    /// How many it has in all.
    pub total: usize,
    pub structured: Option<&'a Value>,
    pub is_error: Option<bool>,
}

/// One content block's fields, as MCP names them.
#[derive(Default)]
pub(crate) struct BlockIn<'a> {
    pub kind: Option<&'a str>,
    pub text: Option<&'a str>,
    pub data: Option<&'a str>,
    pub mime_type: Option<&'a str>,
    pub uri: Option<&'a str>,
    pub name: Option<&'a str>,
    pub resource: Option<ResourceIn<'a>>,
    /// A size this SDK worked out before the hook.
    pub bytes: Option<&'a Value>,
}

#[derive(Default)]
pub(crate) struct ResourceIn<'a> {
    pub uri: Option<&'a str>,
    pub mime_type: Option<&'a str>,
    pub text: Option<&'a str>,
    pub blob: Option<&'a str>,
}

impl<'a> BlockIn<'a> {
    pub(crate) fn from_json(value: &'a Value) -> Option<BlockIn<'a>> {
        let fields = value.as_object()?;
        let text = |key: &str| fields.get(key).and_then(Value::as_str);
        let resource = fields
            .get("resource")
            .and_then(Value::as_object)
            .map(|resource| {
                let text = |key: &str| resource.get(key).and_then(Value::as_str);
                ResourceIn {
                    uri: text("uri"),
                    mime_type: text("mimeType"),
                    text: text("text"),
                    blob: text("blob"),
                }
            });
        Some(BlockIn {
            kind: text("type"),
            text: text("text"),
            data: text("data"),
            mime_type: text("mimeType"),
            uri: text("uri"),
            name: text("name"),
            resource,
            bytes: fields.get("bytes"),
        })
    }
}

/// A call's own data, as the wrapper saw it. Values from the app or the client are checked and
/// shaped here: the wrapper passes them on as it found them.
#[derive(Default)]
pub(crate) struct CallData<'a> {
    /// The tool's description as agents were listed it when the call came.
    pub tool_description: Option<&'a Value>,
    /// The arguments, when inputs are recorded for this tool.
    pub input: Option<&'a Value>,
    /// MCP's result, when results are recorded for this tool.
    pub result: Option<ResultIn<'a>>,
    /// `{ message?, type?, code?, category }`.
    pub error: Option<&'a Value>,
    pub task: Option<&'a Value>,
    pub intent: Option<&'a Value>,
    /// A turn's own fields, and its metadata.
    pub agent: Option<&'a Value>,
}

/// Who and what a call was, around its data.
#[derive(Debug, Default, Clone)]
pub(crate) struct CallHead {
    pub call_id: String,
    pub captured_at: String,
    pub tool_name: String,
    pub status: String,
    pub duration_ms: Value,
    /// `{ orgId?, userId?, conversationId? }`.
    pub ids: Option<Value>,
    pub turn_ref: Option<Value>,
    pub server: Option<Value>,
    pub client: Option<Value>,
    pub protocol_version: Option<Value>,
    pub session_id: Option<Value>,
    pub session_source: Option<String>,
    pub round: Option<String>,
    pub extra: Option<String>,
    /// `identify`'s.
    pub metadata: Option<Value>,
    /// `identify` failed: sent as limited diagnostics.
    pub diagnostics: bool,
}

#[derive(Debug, Clone)]
struct ShapedBlock {
    kind: String,
    text: Option<Shaped>,
    uri: Option<Shaped>,
    name: Option<Shaped>,
    mime_type: Option<Shaped>,
    bytes: Option<Number>,
}

#[derive(Debug, Clone, Default)]
struct ShapedResult {
    content: Option<Vec<ShapedBlock>>,
    structured: Option<Shaped>,
    is_error: Option<bool>,
}

#[derive(Debug, Clone, Default)]
struct ShapedError {
    message: Option<Shaped>,
    kind: Option<String>,
    code: Option<Value>,
    category: Option<&'static str>,
}

#[derive(Debug, Clone, Default)]
struct ShapedCall {
    /// Fixed fields that `before_send` may change: what the calling app and the server called themselves.
    fixed: Map<String, Value>,
    task: Option<(String, String)>,
    input: Option<Shaped>,
    result: Option<ShapedResult>,
    error: Option<ShapedError>,
    intent: Option<Shaped>,
    tool_description: Option<Shaped>,
    agent: Option<ShapedTurn>,
    metadata: Option<Map<String, Value>>,
}

/// A call's data, read and shortened, waiting for who it was for.
#[derive(Debug)]
pub(crate) struct ShapedCallData {
    shaped: ShapedCall,
    notes: Notes,
    redactor: Redactor,
    category: Option<&'static str>,
    /// The agent part's metadata, which the event's is merged with.
    agent_metadata: Option<Value>,
    agent_given: bool,
}

impl ShapedCallData {
    /// About what it holds in memory until its event is built, for the queue's limits: what was
    /// kept of the call's data, not what was read of it.
    pub(crate) fn held_bytes(&self) -> usize {
        let shaped = &self.shaped;
        let mut bytes = 512;
        for value in [&shaped.input, &shaped.intent, &shaped.tool_description]
            .into_iter()
            .flatten()
        {
            bytes += weight(value);
        }
        if let Some(result) = &shaped.result {
            for block in result.content.iter().flatten() {
                bytes += 64;
                for value in [&block.text, &block.uri, &block.name, &block.mime_type]
                    .into_iter()
                    .flatten()
                {
                    bytes += weight(value);
                }
            }
            bytes += result.structured.as_ref().map_or(0, weight);
        }
        if let Some(message) = shaped
            .error
            .as_ref()
            .and_then(|error| error.message.as_ref())
        {
            bytes += weight(message);
        }
        bytes
    }
}

/// Reads a call's data as the wrapper comes by it: its arguments before the tool takes them, and
/// the rest once the tool has answered. Each is read where it is, within the limits, so nothing
/// of a call is copied to be measured.
///
/// The one difference from reading it all at once (`shape_call`): the error, which comes first
/// there, is read here after the arguments, with a scan budget of its own, so it is never the
/// part a very large input leaves nothing for. What it took is then charged to the result's.
pub(crate) struct CallShaper {
    shaped: ShapedCall,
    input_notes: Notes,
    redactor: Redactor,
    keys: Option<KeyNames>,
    remaining: usize,
}

impl CallShaper {
    pub(crate) fn new(context: &Context) -> CallShaper {
        let redactor = Redactor::new(&context.patterns, &context.redact_keys);
        let keys = redactor.key_names();
        CallShaper {
            shaped: ShapedCall::default(),
            input_notes: Notes::default(),
            redactor,
            keys,
            remaining: SCAN_BUDGET,
        }
    }

    /// The call's arguments: none are an empty object.
    pub(crate) fn arguments(&mut self, arguments: Option<&Map<String, Value>>) {
        let empty = Map::new();
        let arguments = arguments.unwrap_or(&empty);
        let mut state = ShapeState::new(self.keys.as_ref());
        state.remaining = self.remaining;
        let input = shape_field(
            &mut state,
            &mut self.input_notes,
            "input",
            |state, notes| {
                // Its keys come from the app's data, so secret key names apply.
                state.secret_key = true;
                let shaped = shape_object(arguments, LIMIT_INPUT, state);
                if state.lossy {
                    notes.drop("input", "not_json");
                }
                state.secret_key = false;
                shaped
            },
        );
        self.remaining = state.remaining;
        self.shaped.input = Some(input);
    }

    /// The rest of the call, once it has ended: `data` without its input.
    pub(crate) fn finish(self, data: &CallData) -> ShapedCallData {
        let CallShaper {
            mut shaped,
            input_notes,
            redactor,
            keys,
            remaining,
        } = self;
        let mut notes = Notes::default();
        let error = data.error.and_then(Value::as_object);
        if data.error.is_some() && error.is_none() {
            notes.drop("error", "wrong_type");
        }
        let category = error
            .and_then(|error| error.get("category"))
            .and_then(Value::as_str)
            .and_then(|given| CATEGORIES.into_iter().find(|category| *category == given));
        shaped.task = task_value(data.task, &mut notes, "task");
        let mut used = 0;
        if let Some(error) = error {
            let mut state = ShapeState::new(keys.as_ref());
            let mut fields = error_fields(error, &mut state, &mut notes);
            used = SCAN_BUDGET - state.remaining;
            fields.category = category;
            if fields.message.is_some() || fields.category.is_some() {
                shaped.error = Some(fields);
            } else {
                notes.drop("error", "wrong_type");
            }
        }
        // The input's notes in their place: after the error's, before the result's.
        notes.merge(&input_notes);
        let mut state = ShapeState::new(keys.as_ref());
        state.remaining = remaining.saturating_sub(used);
        if let Some(result) = &data.result {
            shaped.result = shape_field(&mut state, &mut notes, "result", |state, notes| {
                shape_result(result, state, notes, false)
            });
        }
        if let Some(description) = data.tool_description {
            shaped.tool_description =
                shape_field(&mut state, &mut notes, "toolDescription", |state, notes| {
                    shape_text(
                        Some(description),
                        LIMIT_DESCRIPTION,
                        state,
                        notes,
                        "toolDescription",
                    )
                });
        }
        ShapedCallData {
            shaped,
            notes,
            redactor,
            category,
            agent_metadata: None,
            agent_given: false,
        }
    }
}

/// Reads a call's own data within the limits, in a fixed order which the scan budget makes part
/// of the result: the intent and error first, as the smallest and most telling, then the input,
/// the agent's turn and the result. The tool's description comes last: it is the same on every
/// call, so it never takes budget from what only this call has.
pub(crate) fn shape_call(data: &CallData, context: &Context) -> ShapedCallData {
    let mut notes = Notes::default();
    let redactor = Redactor::new(&context.patterns, &context.redact_keys);
    let keys = redactor.key_names();
    let mut state = ShapeState::new(keys.as_ref());
    let mut shaped = ShapedCall::default();

    let error = data.error.and_then(Value::as_object);
    if data.error.is_some() && error.is_none() {
        notes.drop("error", "wrong_type");
    }
    let category = error
        .and_then(|error| error.get("category"))
        .and_then(Value::as_str)
        .and_then(|given| CATEGORIES.into_iter().find(|category| *category == given));

    shaped.task = task_value(data.task, &mut notes, "task");
    if let Some(intent) = data.intent {
        shaped.intent = shape_field(&mut state, &mut notes, "intent", |state, notes| {
            shape_text(Some(intent), LIMIT_INTENT, state, notes, "intent")
        });
    }
    if let Some(error) = error {
        let mut fields = error_fields(error, &mut state, &mut notes);
        fields.category = category;
        // Neither a message nor a category: nothing the ingestion would take as an error.
        if fields.message.is_some() || fields.category.is_some() {
            shaped.error = Some(fields);
        } else {
            notes.drop("error", "wrong_type");
        }
    }
    if let Some(input) = data.input {
        let empty = Value::Object(Map::new());
        let value = if input.is_null() { &empty } else { input };
        shaped.input = Some(shape_field(
            &mut state,
            &mut notes,
            "input",
            |state, notes| shape_any(value, LIMIT_INPUT, state, notes, "input", true),
        ));
    }
    let mut agent_metadata = None;
    if let Some(agent) = data.agent.filter(|agent| !agent.is_null()) {
        match agent.as_object() {
            Some(fields) => {
                let mut turn = shape_turn_fields(
                    fields,
                    fields.get("turnId"),
                    &mut state,
                    &mut notes,
                    "agent.",
                );
                // As for a turn of its own: one without a status completed.
                turn.status.get_or_insert("completed");
                shaped.agent = Some(turn);
                agent_metadata = fields.get("metadata").cloned();
            }
            None => notes.drop("agent", "wrong_type"),
        }
    }
    if let Some(result) = &data.result {
        shaped.result = shape_field(&mut state, &mut notes, "result", |state, notes| {
            shape_result(result, state, notes, false)
        });
    }
    if let Some(description) = data.tool_description {
        shaped.tool_description =
            shape_field(&mut state, &mut notes, "toolDescription", |state, notes| {
                shape_text(
                    Some(description),
                    LIMIT_DESCRIPTION,
                    state,
                    notes,
                    "toolDescription",
                )
            });
    }
    let agent_given = data.agent.is_some_and(Value::is_object);
    ShapedCallData {
        shaped,
        notes,
        redactor,
        category,
        agent_metadata,
        agent_given,
    }
}

/// Adds who the call was for and what it was, redacts it and fits it. With a hook pending, only
/// cut strings and what's under secret key names are redacted yet.
pub(crate) fn finish_call(
    data: ShapedCallData,
    head: &CallHead,
    context: &Context,
    hook: bool,
) -> Built {
    let ShapedCallData {
        mut shaped,
        mut notes,
        mut redactor,
        category,
        agent_metadata,
        agent_given,
    } = data;
    let (ids, invalid) = check_call(head);
    if !invalid.is_empty() {
        return Err(Refused {
            code: "invalid_id",
            paths: invalid,
        });
    }
    // identify's metadata, merged with that of the capture() inside the call, which wins per key.
    // Each is read within the budget, and the merged result must fit it too: a value one replaces
    // doesn't count. Oversized metadata drops the event, unless it's diagnostics, which leave it out.
    let identified = metadata_value(head.metadata.as_ref(), &mut notes, METADATA_BUDGET);
    let captured = if agent_given {
        metadata_value(agent_metadata.as_ref(), &mut notes, METADATA_BUDGET)
    } else {
        Metadata::None
    };
    let merged = match (identified, captured) {
        (Metadata::TooLarge, _) | (_, Metadata::TooLarge) => Metadata::TooLarge,
        (Metadata::None, Metadata::None) => Metadata::None,
        (identified, captured) => {
            let mut merged = Map::new();
            for part in [identified, captured] {
                if let Metadata::Some(part) = part {
                    merged.extend(part);
                }
            }
            if metadata_size(&merged) > METADATA_BUDGET {
                Metadata::TooLarge
            } else {
                // The two together, in JavaScript's order again.
                Metadata::Some(crate::json::js_ordered(merged))
            }
        }
    };
    match merged {
        Metadata::TooLarge if !head.diagnostics => return Err(refused("too_large", &["metadata"])),
        Metadata::Some(metadata) => shaped.metadata = Some(metadata),
        _ => {}
    }
    let identity = call_identity(head, &ids, context, &mut notes);
    reserve_call(&shaped, &identity, &mut redactor);
    let event = call_event(&identity, &shaped, &mut redactor, !hook);
    let draft = Draft {
        kind: Kind::ToolCall,
        event,
        redactor,
        identity,
        category,
        agent_turn_id: shaped
            .agent
            .as_ref()
            .and_then(|agent| agent.turn_id.clone()),
        diagnostics: head.diagnostics,
        notes,
    };
    pending(finish(draft, !hook, context), hook)
}

/// A call from the wrapper, in one step.
#[cfg_attr(not(test), allow(dead_code))]
pub(crate) fn build_call(data: &CallData, head: &CallHead, context: &Context, hook: bool) -> Built {
    finish_call(shape_call(data, context), head, context, hook)
}

/// An event waiting for its hook keeps only the redaction state of what it still holds.
fn pending(built: Built, hook: bool) -> Built {
    let mut draft = built?;
    if hook {
        let event = Value::Object(std::mem::take(&mut draft.event));
        draft.redactor.prune(&event);
        if let Value::Object(event) = event {
            draft.event = event;
        }
    }
    Ok(draft)
}

/// The call's IDs and outline, checked as the ingestion checks them.
fn check_call(head: &CallHead) -> (Map<String, Value>, Vec<String>) {
    let mut invalid = Vec::new();
    let mut ids = Map::new();
    if !crate::normalize::is_valid_id(&head.call_id) {
        invalid.push("callId".to_owned());
    }
    if !crate::normalize::is_valid_id(&head.tool_name) {
        invalid.push("toolName".to_owned());
    }
    let given = head.ids.as_ref().and_then(Value::as_object);
    for key in ["orgId", "userId", "conversationId"] {
        match id_value(given.and_then(|given| given.get(key))) {
            Id::Invalid => invalid.push(format!("ids.{key}")),
            Id::Valid(value) => {
                ids.insert(key.into(), value.into());
            }
            Id::Absent | Id::Empty => {}
        }
    }
    if let Some(session) = &head.session_id {
        if !matches!(id_value(Some(session)), Id::Valid(_)) {
            invalid.push("sessionId".to_owned());
        }
    }
    if let Some(turn_ref) = &head.turn_ref {
        if !turn_ref
            .as_str()
            .is_some_and(|text| TURN_REF.is_match(text))
        {
            invalid.push("turnRef".to_owned());
        }
    }
    (ids, invalid)
}

/// `before_send`'s output, checked like the app's input: identity restored, shaped again (strings
/// already shortened keep their markers), everything redacted, fitted, and diagnostics reduced.
/// Values removed from it are recorded under `beforeSend.`.
pub(crate) fn after_hook(out: &Map<String, Value>, draft: Box<Draft>, context: &Context) -> Built {
    let Draft {
        kind,
        event: _,
        mut redactor,
        identity,
        category,
        agent_turn_id,
        diagnostics,
        mut notes,
    } = *draft;
    let keys = redactor.key_names();
    let cuts = redactor.cuts.clone();
    let mut state = ShapeState::after_hook(keys.as_ref(), &cuts);
    let mut hook_notes = Notes::default();
    let (event, identity) = match kind {
        Kind::Turn => {
            let event = turn::after_hook(
                out,
                &identity,
                &mut state,
                &mut hook_notes,
                &mut redactor,
                context,
            )?;
            (event, identity)
        }
        Kind::ToolCall => {
            let Some(shaped) = reshape_call(
                out,
                category,
                agent_turn_id.as_deref(),
                diagnostics,
                &mut state,
                &mut hook_notes,
            ) else {
                return Err(refused("too_large", &["metadata"]));
            };
            // The hook may remove or change what the calling app and the server called themselves:
            // taken out, and what it left of them added after the other fixed fields, as in TypeScript.
            let mut restored = identity.clone();
            for key in ["client", "server", "protocolVersion"] {
                restored.shift_remove(key);
                if let Some(value) = shaped.fixed.get(key) {
                    restored.insert(key.into(), value.clone());
                }
            }
            reserve_call(&shaped, &restored, &mut redactor);
            (
                call_event(&restored, &shaped, &mut redactor, true),
                restored,
            )
        }
    };
    for (path, reason) in &hook_notes.dropped {
        notes
            .dropped
            .insert(format!("beforeSend.{path}"), reason.clone());
    }
    for name in &hook_notes.truncated {
        notes.truncate(name);
    }
    finish(
        Draft {
            kind,
            event,
            redactor,
            identity,
            category,
            agent_turn_id,
            diagnostics,
            notes,
        },
        true,
        context,
    )
}

/// Adds the SDK's bookkeeping, fits the event, and once it's final, reduces diagnostics.
fn finish(mut draft: Draft, last: bool, context: &Context) -> Built {
    // Diagnostics keep only a few small fields, whatever the full event's size.
    if last && draft.diagnostics {
        draft.event = diagnostics(&draft);
        return Ok(Box::new(draft));
    }
    if last {
        if let Some(counts) = draft.redactor.counts() {
            draft.event.insert("redactions".into(), counts);
        }
    }
    if !draft.notes.dropped.is_empty() {
        draft
            .event
            .insert("dropped".into(), Value::Object(draft.notes.dropped.clone()));
    }
    if !draft.notes.truncated.is_empty() {
        draft
            .event
            .insert("truncated".into(), draft.notes.truncated.clone().into());
    }
    let order: &[&[&str]] = if draft.kind == Kind::ToolCall {
        &CALL_ORDER
    } else {
        &TURN_ORDER
    };
    let fits = fit(
        &mut draft.event,
        context.max_event_bytes,
        order,
        &mut draft.notes,
    );
    // A diagnostics event waiting for its hook is reduced after it, so its size doesn't count yet.
    if !fits && !draft.diagnostics {
        return Err(refused("too_large", &[]));
    }
    Ok(Box::new(draft))
}

fn size(event: &Map<String, Value>) -> usize {
    // As it is sent, and as the ingestion measures it, without copying the event.
    crate::json::json_size(event)
}

/// Leaves parts out, in order, until the event's compact JSON fits. False if it never does.
fn fit(
    event: &mut Map<String, Value>,
    max_bytes: usize,
    order: &[&[&str]],
    notes: &mut Notes,
) -> bool {
    let mut bytes = size(event);
    for path in order {
        if bytes <= max_bytes {
            return true;
        }
        let key = path[path.len() - 1];
        let removed = if path.len() == 1 {
            // In place, so the fields after it keep their order.
            event.shift_remove(key)
        } else {
            event
                .get_mut(path[0])
                .and_then(Value::as_object_mut)
                .and_then(|parent| parent.shift_remove(key))
        };
        if removed.is_none() {
            continue;
        }
        notes.truncate(&path.join("."));
        event.insert("truncated".into(), notes.truncated.clone().into());
        bytes = size(event);
    }
    bytes <= max_bytes
}

/// What a limited-diagnostics event keeps: what every event needs, and the call's outline.
fn diagnostics(draft: &Draft) -> Map<String, Value> {
    let mut out = Map::new();
    let event = &draft.event;
    let copy = |out: &mut Map<String, Value>, key: &str| {
        if let Some(value) = event.get(key) {
            out.insert(key.into(), value.clone());
        }
    };
    copy(&mut out, "eventId");
    out.insert("kind".into(), "tool_call".into());
    out.insert("schemaVersion".into(), 2.into());
    copy(&mut out, "capturedAt");
    out.insert("source".into(), "live".into());
    for key in ["sdk", "callId", "toolName", "status", "durationMs"] {
        copy(&mut out, key);
    }
    out.insert("identity".into(), "failed".into());
    if let Some(category) = draft.category {
        let mut error = Map::new();
        error.insert("category".into(), category.into());
        out.insert("error".into(), Value::Object(error));
    }
    out
}

fn call_identity(
    head: &CallHead,
    ids: &Map<String, Value>,
    context: &Context,
    notes: &mut Notes,
) -> Map<String, Value> {
    let mut identity = Map::new();
    identity.insert("eventId".into(), head.call_id.clone().into());
    identity.insert("schemaVersion".into(), 2.into());
    identity.insert("kind".into(), "tool_call".into());
    identity.insert("capturedAt".into(), head.captured_at.clone().into());
    identity.insert("source".into(), "live".into());
    identity.insert("sdk".into(), context.sdk.clone());
    identity.insert("callId".into(), head.call_id.clone().into());
    identity.insert("toolName".into(), head.tool_name.clone().into());
    identity.extend(ids.clone());
    if let Some(turn_ref) = &head.turn_ref {
        identity.insert("turnRef".into(), turn_ref.clone());
    }
    named_fields(
        head.server.as_ref(),
        head.client.as_ref(),
        head.protocol_version.as_ref(),
        &mut identity,
        notes,
    );
    if let Some(session) = &head.session_id {
        identity.insert("sessionId".into(), session.clone());
    }
    if let Some(source) = &head.session_source {
        identity.insert("sessionSource".into(), source.clone().into());
    }
    identity.insert("status".into(), head.status.clone().into());
    identity.insert("durationMs".into(), head.duration_ms.clone());
    if let Some(round) = &head.round {
        identity.insert("round".into(), round.clone().into());
    }
    if let Some(extra) = &head.extra {
        identity.insert("extra".into(), extra.clone().into());
    }
    identity
}

/// What the server and the calling app called themselves, and the protocol version, as the
/// protocol (or `before_send`) gave them. One too long to keep is left out and recorded as
/// too_large, as other short fields are; one of another type is left out without a word, as the
/// protocol may not give one.
fn named_fields(
    server: Option<&Value>,
    client: Option<&Value>,
    protocol: Option<&Value>,
    into: &mut Map<String, Value>,
    notes: &mut Notes,
) {
    for (key, value) in [("server", server), ("client", client)] {
        match named(value, notes, key) {
            Check::Valid(value) => {
                into.insert(key.into(), value);
            }
            Check::Dropped("too_large") => notes.drop(key, "too_large"),
            _ => {}
        }
    }
    match short(protocol) {
        Check::Valid(version) => {
            into.insert("protocolVersion".into(), version.into());
        }
        Check::Dropped("too_large") => notes.drop("protocolVersion", "too_large"),
        _ => {}
    }
}

/// A task's ID and status: short strings, both needed.
fn task_value(value: Option<&Value>, notes: &mut Notes, path: &str) -> Option<(String, String)> {
    let value = value.filter(|value| !value.is_null())?;
    let fields = value.as_object();
    let id = short(fields.and_then(|fields| fields.get("id")));
    let status = short(fields.and_then(|fields| fields.get("status")));
    if let (Check::Valid(id), Check::Valid(status)) = (&id, &status) {
        if !id.is_empty() {
            return Some((id.clone(), status.clone()));
        }
    }
    // Both strings, the ID not empty, but one too long to keep: too_large, as other short fields are.
    let strings = [&id, &status]
        .into_iter()
        .all(|check| matches!(check, Check::Valid(_) | Check::Dropped("too_large")));
    let empty = matches!(&id, Check::Valid(id) if id.is_empty());
    notes.drop(
        path,
        if strings && !empty {
            "too_large"
        } else {
            "wrong_type"
        },
    );
    None
}

/// An error's message, type and code, as the app or the client gave them.
fn error_fields(
    fields: &Map<String, Value>,
    state: &mut ShapeState,
    notes: &mut Notes,
) -> ShapedError {
    let mut error = ShapedError {
        message: shape_field(state, notes, "error", |state, notes| {
            shape_text(
                fields.get("message"),
                LIMIT_RESULT,
                state,
                notes,
                "error.message",
            )
        }),
        ..ShapedError::default()
    };
    error.kind = short(fields.get("type"))
        .apply("error.type", notes)
        .filter(|kind| !kind.is_empty());
    error.code = match fields.get("code") {
        Some(Value::Number(code)) => Some(Value::Number(code.clone())),
        other => short(other).apply("error.code", notes).map(Value::String),
    };
    error
}

/// `before_send`'s output for a call: shaped again by the same rules, the result's blocks
/// included, so a hook can't add a file's data back. The error keeps its category, which says what
/// happened when the message is left out; the agent part keeps its turnId. `None`: its metadata is
/// too large.
fn reshape_call(
    out: &Map<String, Value>,
    category: Option<&'static str>,
    agent_turn_id: Option<&str>,
    diagnostics: bool,
    state: &mut ShapeState,
    notes: &mut Notes,
) -> Option<ShapedCall> {
    let mut shaped = ShapedCall::default();
    let given = |key: &str| out.get(key).filter(|value| !value.is_null());
    named_fields(
        out.get("server"),
        out.get("client"),
        out.get("protocolVersion"),
        &mut shaped.fixed,
        notes,
    );
    shaped.task = task_value(out.get("task"), notes, "task");
    if let Some(intent) = given("intent") {
        shaped.intent = shape_field(state, notes, "intent", |state, notes| {
            shape_text(Some(intent), LIMIT_INTENT, state, notes, "intent")
        });
    }
    let only_category = || {
        category.map(|category| ShapedError {
            category: Some(category),
            ..ShapedError::default()
        })
    };
    match given("error") {
        Some(Value::Object(fields)) => {
            let mut error = error_fields(fields, state, notes);
            error.category = category;
            if error.message.is_some() || error.category.is_some() {
                shaped.error = Some(error);
            }
        }
        Some(_) => {
            notes.drop("error", "wrong_type");
            // Replaced by something that isn't an error: the category stays, as below.
            shaped.error = only_category();
        }
        // Removed by the hook: the category, which isn't content, stays.
        None => shaped.error = only_category(),
    }
    if let Some(input) = out.get("input") {
        shaped.input = Some(shape_field(state, notes, "input", |state, notes| {
            shape_any(input, LIMIT_INPUT, state, notes, "input", true)
        }));
    }
    if let Some(agent) = given("agent") {
        match agent.as_object() {
            Some(fields) => {
                let mut turn =
                    shape_turn_fields(fields, fields.get("turnId"), state, notes, "agent.");
                turn.turn_id = agent_turn_id.map(str::to_owned);
                shaped.agent = Some(turn);
            }
            None => notes.drop("agent", "wrong_type"),
        }
    }
    if let Some(result) = given("result") {
        shaped.result = shape_field(state, notes, "result", |state, notes| {
            shape_result(&ResultIn::Json(result), state, notes, true)
        });
    }
    if let Some(description) = given("toolDescription") {
        shaped.tool_description = shape_field(state, notes, "toolDescription", |state, notes| {
            shape_text(
                Some(description),
                LIMIT_DESCRIPTION,
                state,
                notes,
                "toolDescription",
            )
        });
    }
    match metadata_value(out.get("metadata"), notes, METADATA_BUDGET) {
        Metadata::TooLarge if !diagnostics => return None,
        Metadata::Some(metadata) => shaped.metadata = Some(metadata),
        _ => {}
    }
    Some(shaped)
}

/// MCP's result without anything binary: files become their size, text is shortened. `sized`:
/// `before_send`'s output, whose files may already be only their size.
fn shape_result(
    value: &ResultIn,
    state: &mut ShapeState,
    notes: &mut Notes,
    sized: bool,
) -> Option<ShapedResult> {
    let mut out = ShapedResult::default();
    let json_blocks: Vec<Option<BlockIn>>;
    let (blocks, total, structured): (&[Option<BlockIn>], usize, Option<&Value>) = match value {
        ResultIn::Json(value) => {
            let Some(fields) = value.as_object() else {
                notes.drop("result", "wrong_type");
                return None;
            };
            if let Some(Value::Bool(is_error)) = fields.get("isError") {
                out.is_error = Some(*is_error);
            }
            match fields.get("content") {
                Some(Value::Array(content)) => {
                    json_blocks = content
                        .iter()
                        .take(MAX_ENTRIES)
                        .map(BlockIn::from_json)
                        .collect();
                    (&json_blocks, content.len(), fields.get("structuredContent"))
                }
                other => {
                    if other.is_some() {
                        notes.drop("result.content", "wrong_type");
                    }
                    return finish_result(out, None, fields.get("structuredContent"), state, notes);
                }
            }
        }
        ResultIn::Typed(typed) => {
            out.is_error = typed.is_error;
            return shape_blocks(
                out,
                typed.blocks.iter().map(Some),
                typed.total,
                typed.structured,
                state,
                notes,
                sized,
            );
        }
    };
    shape_blocks(
        out,
        blocks.iter().map(Option::as_ref),
        total,
        structured,
        state,
        notes,
        sized,
    )
}

fn shape_blocks<'a, 'b: 'a>(
    out: ShapedResult,
    blocks: impl Iterator<Item = Option<&'a BlockIn<'b>>>,
    total: usize,
    structured: Option<&Value>,
    state: &mut ShapeState,
    notes: &mut Notes,
    sized: bool,
) -> Option<ShapedResult> {
    if total > MAX_ENTRIES {
        state.shortened = true;
    }
    let mut shaped = Vec::new();
    for (index, block) in blocks.take(MAX_ENTRIES).enumerate() {
        // One per block, as for any list: past the budget, no more blocks are taken.
        if !spend(state, 1) {
            break;
        }
        match block.and_then(|block| shape_block(block, state, sized)) {
            Some(block) => shaped.push(block),
            None => notes.drop(format!("result.content[{index}]"), "wrong_type"),
        }
    }
    finish_result(out, Some(shaped), structured, state, notes)
}

fn finish_result(
    mut out: ShapedResult,
    content: Option<Vec<ShapedBlock>>,
    structured: Option<&Value>,
    state: &mut ShapeState,
    notes: &mut Notes,
) -> Option<ShapedResult> {
    out.content = content;
    if let Some(structured) = structured {
        out.structured = Some(shape_any(
            structured,
            LIMIT_RESULT,
            state,
            notes,
            "result.structuredContent",
            true,
        ));
    }
    Some(out)
}

fn shape_block(block: &BlockIn, state: &mut ShapeState, sized: bool) -> Option<ShapedBlock> {
    let kind = block
        .kind
        .filter(|kind| !kind.is_empty() && utf16_len(kind) <= 64)?;
    // A block's fields are MCP's, but a secret key name or one of `redact_keys` still replaces
    // one whole, numbered by its raw value.
    let text = |field: Option<&str>, key: &str, state: &mut ShapeState| {
        let field = field?;
        Some(if state.is_secret_key(key) {
            secret_string(field, LIMIT_RESULT, state)
        } else {
            shape_string(field, LIMIT_RESULT, state)
        })
    };
    let mime = |field: Option<&str>, state: &mut ShapeState| {
        let field = field.filter(|field| utf16_len(field) <= 256)?;
        Some(if state.is_secret_key("mimeType") {
            secret_string(field, 256, state)
        } else {
            Shaped::String(field.to_owned())
        })
    };
    // A size this SDK worked out before the hook: kept, as nothing else of the file is.
    let size_only = || {
        let bytes = block.bytes.filter(|_| sized)?;
        // A safe integer that isn't negative, however the hook wrote it: `3.0` is 3.
        let whole = bytes.as_u64().or_else(|| {
            bytes
                .as_f64()
                .filter(|value| *value >= 0.0 && value.fract() == 0.0)
                .map(|value| value as u64)
        });
        whole
            .filter(|bytes| *bytes <= 9_007_199_254_740_991)
            .map(Number::from)
    };
    let mut out = ShapedBlock {
        kind: kind.to_owned(),
        text: None,
        uri: None,
        name: None,
        mime_type: None,
        bytes: None,
    };
    match kind {
        "text" => {
            out.text = Some(
                text(block.text, "text", state).unwrap_or_else(|| Shaped::String(String::new())),
            )
        }
        "image" | "audio" => {
            out.mime_type = mime(block.mime_type, state);
            match block.data {
                Some(data) => {
                    out.bytes = Some(decoded_size(utf16_len(data), data).into());
                    state.shortened = true;
                }
                None => out.bytes = size_only(),
            }
        }
        "resource_link" => {
            out.uri = text(block.uri, "uri", state);
            out.name = text(block.name, "name", state);
            out.mime_type = mime(block.mime_type, state);
        }
        "resource" => {
            // After the hook, the block is in this SDK's flat form; before it, in MCP's.
            let flat = ResourceIn {
                uri: block.uri,
                mime_type: block.mime_type,
                text: block.text,
                blob: None,
            };
            let empty = ResourceIn::default();
            let resource = block
                .resource
                .as_ref()
                .unwrap_or(if sized { &flat } else { &empty });
            out.uri = text(resource.uri, "uri", state);
            out.mime_type = mime(resource.mime_type, state);
            if resource.text.is_some() {
                out.text = text(resource.text, "text", state);
            } else if let Some(blob) = resource.blob {
                out.bytes = Some(decoded_size(utf16_len(blob), blob).into());
                state.shortened = true;
            } else {
                out.bytes = size_only();
            }
        }
        // Unknown kinds keep only their type: their fields could hold anything.
        _ => {}
    }
    Some(out)
}

/// Records placeholders already in the call's event: in every field, redacted or not (its tool
/// name, IDs, client and server, a block's type), as for a turn.
fn reserve_call(shaped: &ShapedCall, identity: &Map<String, Value>, redactor: &mut Redactor) {
    for value in shaped.fixed.values() {
        redactor.reserve_json(value);
    }
    if let Some((id, status)) = &shaped.task {
        redactor.reserve_text(id);
        redactor.reserve_text(status);
    }
    for value in [&shaped.input, &shaped.intent, &shaped.tool_description]
        .into_iter()
        .flatten()
    {
        redactor.reserve(value);
    }
    if let Some(result) = &shaped.result {
        for block in result.content.iter().flatten() {
            redactor.reserve_text(&block.kind);
            for value in [&block.text, &block.uri, &block.name, &block.mime_type]
                .into_iter()
                .flatten()
            {
                redactor.reserve(value);
            }
        }
        if let Some(structured) = &result.structured {
            redactor.reserve(structured);
        }
    }
    if let Some(error) = &shaped.error {
        if let Some(message) = &error.message {
            redactor.reserve(message);
        }
        if let Some(kind) = &error.kind {
            redactor.reserve_text(kind);
        }
        if let Some(Value::String(code)) = &error.code {
            redactor.reserve_text(code);
        }
    }
    if let Some(agent) = &shaped.agent {
        reserve_turn_fields(agent, redactor);
    }
    if let Some(metadata) = &shaped.metadata {
        for (key, value) in metadata {
            redactor.reserve_text(key);
            redactor.reserve_json(value);
        }
    }
    for value in identity.values() {
        redactor.reserve_json(value);
    }
}

fn call_event(
    identity: &Map<String, Value>,
    shaped: &ShapedCall,
    redactor: &mut Redactor,
    all: bool,
) -> Map<String, Value> {
    let mut event = identity.clone();
    if let Some(input) = &shaped.input {
        event.insert(
            "input".into(),
            redactor.finalize(input, JSON_FIELD.with_all(all)),
        );
    }
    if let Some(shaped) = &shaped.result {
        let mut result = Map::new();
        if let Some(content) = &shaped.content {
            let blocks: Vec<Value> = content
                .iter()
                .map(|block| {
                    let mut out = Map::new();
                    out.insert(
                        "type".into(),
                        redactor
                            .finalize_text(&Shaped::String(block.kind.clone()), all)
                            .into(),
                    );
                    for (key, value) in [
                        ("uri", &block.uri),
                        ("name", &block.name),
                        ("mimeType", &block.mime_type),
                        ("text", &block.text),
                    ] {
                        if let Some(value) = value {
                            out.insert(key.into(), redactor.finalize_text(value, all).into());
                        }
                    }
                    if let Some(bytes) = &block.bytes {
                        out.insert("bytes".into(), Value::Number(bytes.clone()));
                    }
                    Value::Object(out)
                })
                .collect();
            result.insert("content".into(), Value::Array(blocks));
        }
        if let Some(structured) = &shaped.structured {
            result.insert(
                "structuredContent".into(),
                redactor.finalize(structured, JSON_FIELD.with_all(all)),
            );
        }
        if let Some(is_error) = shaped.is_error {
            result.insert("isError".into(), is_error.into());
        }
        event.insert("result".into(), Value::Object(result));
    }
    if let Some(shaped) = &shaped.error {
        let mut error = Map::new();
        if let Some(message) = &shaped.message {
            error.insert(
                "message".into(),
                redactor.finalize_text(message, all).into(),
            );
        }
        if let Some(kind) = &shaped.kind {
            error.insert(
                "type".into(),
                if all {
                    redactor.text(kind, false)
                } else {
                    kind.clone()
                }
                .into(),
            );
        }
        match &shaped.code {
            Some(Value::String(code)) => {
                error.insert(
                    "code".into(),
                    if all {
                        redactor.text(code, false)
                    } else {
                        code.clone()
                    }
                    .into(),
                );
            }
            Some(code) => {
                error.insert("code".into(), code.clone());
            }
            None => {}
        }
        if let Some(category) = shaped.category {
            error.insert("category".into(), category.into());
        }
        event.insert("error".into(), Value::Object(error));
    }
    if let Some(intent) = &shaped.intent {
        event.insert("intent".into(), redactor.finalize_text(intent, all).into());
    }
    if let Some(description) = &shaped.tool_description {
        event.insert(
            "toolDescription".into(),
            redactor.finalize_text(description, all).into(),
        );
    }
    if let Some((id, status)) = &shaped.task {
        let mut task = Map::new();
        task.insert(
            "id".into(),
            redactor
                .finalize_text(&Shaped::String(id.clone()), all)
                .into(),
        );
        task.insert(
            "status".into(),
            redactor
                .finalize_text(&Shaped::String(status.clone()), all)
                .into(),
        );
        event.insert("task".into(), Value::Object(task));
    }
    if let Some(agent) = &shaped.agent {
        event.insert(
            "agent".into(),
            Value::Object(finish_turn_fields(agent, redactor, all)),
        );
    }
    if let Some(metadata) = &shaped.metadata {
        event.insert("metadata".into(), finish_metadata(metadata, redactor, all));
    }
    event
}

/// Turn events, which `capture()` will send. Built here so the shared cases, half of which are
/// turns, all exercise this SDK's shortening and redaction; nothing public reaches them yet.
#[cfg_attr(not(test), allow(dead_code))]
pub(crate) mod turn {
    use super::*;
    use crate::normalize::Notes;

    const ID_FIELDS: [&str; 5] = ["orgId", "userId", "conversationId", "toolName", "callId"];

    struct ShapedTurnEvent {
        ids: Map<String, Value>,
        fields: ShapedTurn,
        metadata: Option<Map<String, Value>>,
        notes: Notes,
    }

    /// Turns `capture()` input (or `before_send`'s output for a turn, with `identity`) into a
    /// shaped turn. Only an invalid ID or oversized metadata drops it.
    fn shape_turn(
        raw: Option<&Map<String, Value>>,
        state: &mut ShapeState,
        metadata_budget: i64,
        identity: Option<(&Map<String, Value>, Option<&Value>)>,
    ) -> Result<ShapedTurnEvent, Refused> {
        let mut notes = Notes::default();
        let empty = Map::new();
        let input = match (raw, &identity) {
            (Some(input), _) => input,
            (None, Some(_)) => &empty,
            (None, None) => return Err(refused("invalid_input", &[])),
        };
        // After the hook, the turnId is the one the event had, whatever the hook made of it.
        let turn_id = identity.map_or(input.get("turnId"), |(_, turn_id)| turn_id);
        let ids = match identity {
            Some((ids, _)) => ids.clone(),
            None => {
                let mut ids = Map::new();
                let mut invalid = Vec::new();
                for key in ID_FIELDS {
                    match id_value(input.get(key)) {
                        Id::Invalid => invalid.push(key.to_owned()),
                        Id::Empty => notes.drop(key, "empty"),
                        Id::Valid(value) => {
                            ids.insert(key.into(), value.into());
                        }
                        Id::Absent => {}
                    }
                }
                if !invalid.is_empty() {
                    return Err(Refused {
                        code: "invalid_id",
                        paths: invalid,
                    });
                }
                ids
            }
        };
        let fields = shape_turn_fields(input, turn_id, state, &mut notes, "");
        let metadata = match metadata_value(input.get("metadata"), &mut notes, metadata_budget) {
            Metadata::TooLarge => return Err(refused("too_large", &["metadata"])),
            Metadata::Some(metadata) => Some(metadata),
            Metadata::None => None,
        };
        Ok(ShapedTurnEvent {
            ids,
            fields,
            metadata,
            notes,
        })
    }

    fn turn_event(
        head: &Map<String, Value>,
        ids: &Map<String, Value>,
        fields: &ShapedTurn,
        metadata: Option<&Map<String, Value>>,
        redactor: &mut Redactor,
        all: bool,
    ) -> Map<String, Value> {
        let mut event = head.clone();
        event.extend(ids.clone());
        event.extend(finish_turn_fields(fields, redactor, all));
        if let Some(metadata) = metadata {
            event.insert("metadata".into(), finish_metadata(metadata, redactor, all));
        }
        event
    }

    /// A turn from `capture()`. `event_id` makes its ID from its conversation and turn, or a random one.
    pub(crate) fn build_turn(
        input: &Value,
        captured_at: &str,
        context: &Context,
        hook: bool,
        event_id: impl FnOnce(&Map<String, Value>, Option<&str>) -> String,
    ) -> Built {
        let mut redactor = Redactor::new(&context.patterns, &context.redact_keys);
        let keys = redactor.key_names();
        let mut state = ShapeState::new(keys.as_ref());
        let ShapedTurnEvent {
            ids,
            mut fields,
            metadata,
            notes,
        } = shape_turn(
            input.as_object(),
            &mut state,
            context.max_event_bytes as i64,
            None,
        )?;
        // As in 0.1, a turn without a status completed.
        fields.status.get_or_insert("completed");
        reserve_turn_fields(&fields, &mut redactor);
        if let Some(metadata) = &metadata {
            redactor.reserve_json(&Value::Object(metadata.clone()));
        }
        let mut head = Map::new();
        head.insert(
            "eventId".into(),
            event_id(&ids, fields.turn_id.as_deref()).into(),
        );
        head.insert("schemaVersion".into(), 2.into());
        head.insert("kind".into(), "turn".into());
        head.insert("capturedAt".into(), captured_at.into());
        head.insert("source".into(), "live".into());
        head.insert("sdk".into(), context.sdk.clone());
        let mut identity = head.clone();
        identity.extend(ids.clone());
        if let Some(turn_id) = &fields.turn_id {
            identity.insert("turnId".into(), turn_id.clone().into());
        }
        // The IDs too, which are never redacted: a placeholder anywhere in the event is skipped.
        redactor.reserve_json(&Value::Object(identity.clone()));
        let event = turn_event(
            &head,
            &ids,
            &fields,
            metadata.as_ref(),
            &mut redactor,
            !hook,
        );
        let draft = Draft {
            kind: Kind::Turn,
            event,
            redactor,
            identity,
            category: None,
            agent_turn_id: None,
            diagnostics: false,
            notes,
        };
        pending(finish(draft, !hook, context), hook)
    }

    pub(super) fn after_hook(
        out: &Map<String, Value>,
        identity: &Map<String, Value>,
        state: &mut ShapeState,
        hook_notes: &mut Notes,
        redactor: &mut Redactor,
        context: &Context,
    ) -> Result<Map<String, Value>, Refused> {
        let mut head = Map::new();
        let mut ids = Map::new();
        for (key, value) in identity {
            match key.as_str() {
                "eventId" | "schemaVersion" | "kind" | "capturedAt" | "source" | "sdk" => {
                    head.insert(key.clone(), value.clone());
                }
                "turnId" => {}
                _ => {
                    ids.insert(key.clone(), value.clone());
                }
            }
        }
        let mut shaped = shape_turn(
            Some(out),
            state,
            context.max_event_bytes as i64,
            Some((&ids, identity.get("turnId"))),
        )?;
        shaped.fields.status.get_or_insert("completed");
        reserve_turn_fields(&shaped.fields, redactor);
        if let Some(metadata) = &shaped.metadata {
            redactor.reserve_json(&Value::Object(metadata.clone()));
        }
        redactor.reserve_json(&Value::Object(identity.clone()));
        hook_notes.merge(&shaped.notes);
        Ok(turn_event(
            &head,
            &ids,
            &shaped.fields,
            shaped.metadata.as_ref(),
            redactor,
            true,
        ))
    }
}
