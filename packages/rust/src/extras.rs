//! The extras: the `session_id` and `intent` inputs, which the wrapper adds to every tool it can,
//! and `get_more_tools`, the tool it adds and answers itself.
//! A port of the JSON Schema half of `packages/lsquare/src/mcp/extras.ts`: in Rust a tool's
//! schema is all there is to read of it, as for a low-level server in TypeScript.
//!
//! Parts adapted from AgentCat's TypeScript SDK (MIT, © 2025 AgentCat, Inc.; see
//! `THIRD_PARTY_NOTICES`), by way of that file: adding the extras' inputs to a listed tool's schema.

use serde_json::{Map, Value};

pub(crate) const SESSION_ID: &str = "session_id";
pub(crate) const INTENT: &str = "intent";

/// How each input is described to the agent: TypeScript's words, which a test compares.
pub(crate) const SESSION_DESCRIPTION: &str = "The ID of the task this call belongs to. Leave it out on your first call. On every later call for the same task, send the ID an earlier result gave you, and pass it to any subagent working on that task.";
pub(crate) const INTENT_DESCRIPTION: &str = "In one sentence, why you're calling this tool and what the user is trying to do. No personal data or secrets.";

/// The tool the wrapper adds, for an agent to say what it needed and couldn't do.
pub(crate) const MISSING_TOOLS: &str = "get_more_tools";
const NEED: &str = "need";
/// How the tool and its input are described to the agent, and what it is answered: TypeScript's
/// words, which a test compares.
pub(crate) const MISSING_TOOLS_DESCRIPTION: &str = "Call this when none of the available tools fits what you need, or one fits only in part. It doesn't return new tools: it tells the server's developers what to add.";
const NEED_DESCRIPTION: &str =
    "What you needed to do and couldn't, in one or two sentences. No personal data or secrets.";
const NOTED: &str = "Noted. No other tools are available right now.";
const NO_NEED: &str = "Invalid arguments for tool get_more_tools: need is required, as a string saying what you needed to do and couldn't.";

const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
const ID_LENGTH: usize = 22;

fn description(name: &str) -> &'static str {
    if name == SESSION_ID {
        SESSION_DESCRIPTION
    } else {
        INTENT_DESCRIPTION
    }
}

/// A session ID an agent may send back: `ses_` and 22 letters, digits, `-` or `_`.
pub(crate) fn is_session_id(text: &str) -> bool {
    text.strip_prefix("ses_").is_some_and(|rest| {
        rest.len() == ID_LENGTH
            && rest
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
    })
}

/// A new session ID: `ses_` and 22 random base64url characters.
pub(crate) fn new_session_id() -> String {
    // Two random UUIDs give 32 bytes, of which 12 bits are fixed: none of those is among the low
    // 6 bits of the 22 bytes taken (the version and variant are in bytes 6 and 8, which are skipped).
    let random: Vec<u8> = uuid::Uuid::new_v4()
        .into_bytes()
        .into_iter()
        .chain(uuid::Uuid::new_v4().into_bytes())
        .enumerate()
        .filter(|(index, _)| index % 16 != 6 && index % 16 != 8)
        .map(|(_, byte)| byte)
        .collect();
    let mut id = String::with_capacity(4 + ID_LENGTH);
    id.push_str("ses_");
    // 64 characters: each byte's low 6 bits pick one, evenly.
    for byte in random.iter().take(ID_LENGTH) {
        id.push(ALPHABET[usize::from(byte & 63)] as char);
    }
    id
}

/// What the text block at the end of a result says, to give the agent its task's ID.
pub(crate) fn id_block(id: &str) -> String {
    format!("Task ID: {id}. Send it as session_id on later calls for this task, and pass it to any subagent working on it.")
}

/// `get_more_tools`' input schema as it is listed: a required `need`, and `session_id` when
/// sessions are on.
pub(crate) fn missing_tools_schema(sessions: bool) -> Map<String, Value> {
    let input = |text: &str| {
        let mut input = Map::new();
        input.insert("type".into(), "string".into());
        input.insert("description".into(), text.into());
        Value::Object(input)
    };
    let mut properties = Map::new();
    properties.insert(NEED.into(), input(NEED_DESCRIPTION));
    if sessions {
        properties.insert(SESSION_ID.into(), input(SESSION_DESCRIPTION));
    }
    let mut schema = Map::new();
    schema.insert("type".into(), "object".into());
    schema.insert("properties".into(), Value::Object(properties));
    schema.insert("required".into(), Value::Array(vec![NEED.into()]));
    schema
}

/// The wrapper's reply to a call of `get_more_tools`: its text, and whether it is an error, which
/// it is without a `need` that is a string, as the MCP SDKs refuse arguments.
pub(crate) fn missing_tools_reply(arguments: Option<&Map<String, Value>>) -> (&'static str, bool) {
    match arguments.and_then(|arguments| arguments.get(NEED)) {
        Some(Value::String(_)) => (NOTED, false),
        _ => (NO_NEED, true),
    }
}

/// What a tool's listed input schema says of the extras' inputs.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Ownership {
    /// The schema is an object of named properties: the inputs it names are the tool's own, and
    /// the others are left to the wrapper.
    Readable { session_id: bool, intent: bool },
    /// Anything else could read or need any key: the tool is left alone.
    Unreadable,
}

impl Ownership {
    /// Whether the tool declares an input of this name itself.
    pub(crate) fn declares(self, name: &str) -> bool {
        match self {
            Ownership::Readable { session_id, intent } => {
                (name == SESSION_ID && session_id) || (name == INTENT && intent)
            }
            Ownership::Unreadable => false,
        }
    }
}

/// Keys a listed schema's root may hold for inputs to be added to it: the object's own, and those
/// that only describe.
const ROOT_KEYS: [&str; 14] = [
    "type",
    "properties",
    "required",
    "additionalProperties",
    "$schema",
    "$id",
    "$comment",
    "$defs",
    "definitions",
    "title",
    "description",
    "default",
    "examples",
    "deprecated",
];
/// How much of a listed schema is read to find its references, at most.
const WALK_NODES: usize = 10_000;
const WALK_DEPTH: usize = 64;
const MAX_REQUIRED: usize = 10_000;

/// The root of a schema inputs can be added to, or be taken out of calls for: only
/// `type: "object"`, `properties` as an object, `additionalProperties` absent or false, and keys
/// that only describe. Its properties, when it has some.
fn plain_root(schema: &Map<String, Value>) -> Option<Option<&Map<String, Value>>> {
    if !schema.keys().all(|key| ROOT_KEYS.contains(&key.as_str())) {
        return None;
    }
    if schema.get("type").and_then(Value::as_str) != Some("object") {
        return None;
    }
    let properties = match schema.get("properties") {
        None => None,
        Some(Value::Object(properties)) => Some(properties),
        Some(_) => return None,
    };
    if !matches!(
        schema.get("additionalProperties"),
        None | Some(Value::Bool(false))
    ) {
        return None;
    }
    Some(properties)
}

/// The names a schema's `required` holds: none unless it is a list of at most 10,000 entries,
/// which are strings unless `any`.
fn required_names(required: Option<&Value>, any: bool) -> Option<&[Value]> {
    match required {
        None => Some(&[]),
        Some(Value::Array(names))
            if names.len() <= MAX_REQUIRED && (any || names.iter().all(Value::is_string)) =>
        {
            Some(names)
        }
        Some(_) => None,
    }
}

/// What a listed JSON Schema says of the extras' inputs: readable only when its root holds only
/// `type: "object"`, `properties`, a `required` of names, `additionalProperties` absent or false,
/// and the keys that only describe.
pub(crate) fn listed_ownership(schema: &Map<String, Value>) -> Ownership {
    let Some(properties) = plain_root(schema) else {
        return Ownership::Unreadable;
    };
    let Some(required) = required_names(schema.get("required"), false) else {
        return Ownership::Unreadable;
    };
    let declares = |name: &str| {
        required
            .iter()
            .any(|required| required.as_str() == Some(name))
            || properties.is_some_and(|properties| properties.contains_key(name))
    };
    Ownership::Readable {
        session_id: declares(SESSION_ID),
        intent: declares(INTENT),
    }
}

/// A copy of a tool's listed input schema with `names` added as optional string properties, each
/// only where the schema takes it as one more without changing what it accepts; `None` when none
/// can be added. Every reference in it must point into its `$defs`, `definitions` or one of its
/// own properties: one to the root, as a recursive schema has, would show the inputs at nested
/// levels too, where they aren't removed.
pub(crate) fn advertise(schema: &Map<String, Value>, names: &[&str]) -> Option<Map<String, Value>> {
    if names.is_empty() {
        return None;
    }
    let properties = plain_root(schema)?;
    let required = required_names(schema.get("required"), true)?;
    let adding: Vec<&str> = names
        .iter()
        .copied()
        .filter(|name| {
            !properties.is_some_and(|properties| properties.contains_key(*name))
                && !required
                    .iter()
                    .any(|required| required.as_str() == Some(*name))
        })
        .collect();
    if adding.is_empty() || !local_references(schema, &adding) {
        return None;
    }
    let mut added = properties.cloned().unwrap_or_default();
    for name in &adding {
        let mut input = Map::new();
        input.insert("type".into(), "string".into());
        input.insert("description".into(), description(name).into());
        added.insert((*name).to_owned(), Value::Object(input));
    }
    // In place where the schema had them, and last where it had none, as TypeScript's copy does.
    let mut out = schema.clone();
    out.insert("properties".into(), Value::Object(added));
    Some(out)
}

/// Every `$ref` in the schema points into its `$defs`, `definitions` or a property it already
/// has, and it has no dynamic references. Each entry read counts against the budget, so a wide
/// object or list costs no more than a deep one.
fn local_references(root: &Map<String, Value>, adding: &[&str]) -> bool {
    enum Node<'a> {
        Object(&'a Map<String, Value>),
        List(&'a [Value]),
    }
    let mut stack: Vec<(Node, usize)> = vec![(Node::Object(root), 0)];
    let mut budget = WALK_NODES;
    while let Some((node, depth)) = stack.pop() {
        if depth > WALK_DEPTH {
            return false;
        }
        let values: Box<dyn Iterator<Item = &Value>> = match node {
            Node::Object(object) => {
                if object.contains_key("$dynamicRef") || object.contains_key("$recursiveRef") {
                    return false;
                }
                if let Some(reference) = object.get("$ref") {
                    if !local_reference(reference, adding) {
                        return false;
                    }
                }
                Box::new(object.values())
            }
            Node::List(list) => Box::new(list.iter()),
        };
        for value in values {
            if budget == 0 {
                return false;
            }
            budget -= 1;
            match value {
                Value::Object(object) => stack.push((Node::Object(object), depth + 1)),
                Value::Array(list) => stack.push((Node::List(list), depth + 1)),
                _ => {}
            }
        }
    }
    true
}

fn local_reference(reference: &Value, adding: &[&str]) -> bool {
    // Percent-encoding could spell any name: such a reference counts as unknown.
    let Some(reference) = reference.as_str().filter(|text| !text.contains('%')) else {
        return false;
    };
    if reference.starts_with("#/$defs/") || reference.starts_with("#/definitions/") {
        return true;
    }
    let Some(path) = reference.strip_prefix("#/properties/") else {
        return false;
    };
    let name = path
        .split('/')
        .next()
        .unwrap_or_default()
        .replace("~1", "/")
        .replace("~0", "~");
    !adding.contains(&name.as_str())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn object(value: Value) -> Map<String, Value> {
        value.as_object().cloned().expect("an object")
    }

    #[test]
    fn a_session_id_is_ses_and_22_url_safe_characters() {
        assert!(is_session_id("ses_abcdefghijklmnopqrstuv"));
        assert!(is_session_id("ses_ABC-def_0123456789xyzQ"));
        for other in [
            "ses_abcdefghijklmnopqrstu",
            "ses_abcdefghijklmnopqrstuvw",
            "sess_abcdefghijklmnopqrstu",
            "ses_abcdefghijklmnopqrstu!",
            "ses_abcdefghijklmnopqrst\u{e9}",
            "",
            "SES_abcdefghijklmnopqrstuv",
        ] {
            assert!(!is_session_id(other), "{other}");
        }
        let (one, other) = (new_session_id(), new_session_id());
        assert!(is_session_id(&one) && is_session_id(&other));
        assert_ne!(one, other);
        assert_eq!(
            id_block("ses_abcdefghijklmnopqrstuv"),
            "Task ID: ses_abcdefghijklmnopqrstuv. Send it as session_id on later calls for this task, and pass it to any subagent working on it."
        );
    }

    /// What agents are told, and the rules a schema is read by, are TypeScript's: read from its
    /// source, where this is checked out next to it.
    #[test]
    fn the_words_and_the_rules_are_typescripts() {
        let source = std::env::var_os("LSQUARE_TYPESCRIPT_SRC").map_or_else(
            || crate::cases_test::contract_dir().join("../lsquare/src"),
            std::path::PathBuf::from,
        );
        let Ok(typescript) = std::fs::read_to_string(source.join("mcp/extras.ts")) else {
            eprintln!(
                "skipped: the TypeScript SDK's source isn't at {}",
                source.display()
            );
            return;
        };
        let alphabet = std::str::from_utf8(ALPHABET).unwrap();
        let keys: Vec<String> = ROOT_KEYS.iter().map(|key| format!("'{key}'")).collect();
        let spaced = |number: usize| {
            let digits = number.to_string();
            match digits.len() {
                0..=3 => digits,
                length => format!("{}_{}", &digits[..length - 3], &digits[length - 3..]),
            }
        };
        for expected in [
            format!("export const SESSION_ID = '{SESSION_ID}'"),
            format!("export const INTENT = '{INTENT}'"),
            format!("'{SESSION_DESCRIPTION}'"),
            format!("\"{INTENT_DESCRIPTION}\""),
            format!("text: `{}`", id_block("${id}")),
            format!("export const MISSING_TOOLS = '{MISSING_TOOLS}'"),
            format!("\"{MISSING_TOOLS_DESCRIPTION}\""),
            format!("const NEED_DESCRIPTION = \"{NEED_DESCRIPTION}\""),
            format!("const REPLY = \"{NOTED}\""),
            format!("const NO_NEED = \"{NO_NEED}\""),
            format!("export const SESSION_FORMAT = /^ses_[A-Za-z0-9_-]{{{ID_LENGTH}}}$/"),
            format!("const ALPHABET = '{alphabet}'"),
            format!("randomBytes({ID_LENGTH})"),
            format!("const ROOT_KEYS = new Set([{}])", keys.join(", ")),
            format!("const WALK_NODES = {}", spaced(WALK_NODES)),
            format!("const WALK_DEPTH = {WALK_DEPTH}"),
            format!("!(length <= {})", spaced(MAX_REQUIRED)),
        ] {
            assert!(
                typescript.contains(&expected),
                "TypeScript has no {expected}"
            );
        }
    }

    /// Schemas of every kind the rules tell apart, at their limits too.
    fn fixtures() -> Vec<Value> {
        let property = |schema: Value| json!({ "type": "object", "properties": { "a": schema } });
        let nested = |depth: usize| {
            let mut schema = json!({ "type": "string" });
            for _ in 0..depth {
                schema = json!({ "items": schema });
            }
            property(schema)
        };
        let wide = |count: usize| {
            let entries: Map<String, Value> = (0..count)
                .map(|index| (format!("p{index}"), json!(true)))
                .collect();
            json!({ "type": "object", "properties": entries })
        };
        let required = |count: usize| {
            let names: Vec<String> = (0..count).map(|index| format!("r{index}")).collect();
            json!({ "type": "object", "required": names })
        };
        let mut all = vec![
            json!({}),
            json!({ "type": "object" }),
            json!({ "type": "object", "properties": {} }),
            json!({ "type": "object", "properties": { "q": { "type": "string" } }, "required": ["q"] }),
            json!({ "$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "x", "$comment": "c", "title": "T", "description": "d", "default": {}, "examples": [{}], "deprecated": false, "type": "object", "properties": { "q": {} }, "required": [], "additionalProperties": false, "$defs": { "A": { "type": "string" } }, "definitions": { "B": {} } }),
            json!({ "type": "object", "additionalProperties": true }),
            json!({ "type": "object", "additionalProperties": {} }),
            json!({ "type": "object", "additionalProperties": { "type": "string" } }),
            json!({ "type": "object", "additionalProperties": null }),
            json!({ "type": "object", "properties": { "session_id": { "type": "number" } } }),
            json!({ "type": "object", "properties": { "intent": true } }),
            json!({ "type": "object", "properties": { "intent": {}, "session_id": {} } }),
            json!({ "type": "object", "required": ["session_id"] }),
            json!({ "type": "object", "properties": { "q": {} }, "required": ["intent", "q"] }),
            json!({ "type": "object", "required": "q" }),
            json!({ "type": "object", "required": [1] }),
            json!({ "type": "object", "required": ["q", null] }),
            json!({ "type": "object", "required": {} }),
            json!({ "type": ["object", "null"] }),
            json!({ "type": ["object"] }),
            json!({ "type": "string" }),
            json!({ "type": "Object" }),
            json!({ "properties": {} }),
            json!({ "type": "object", "properties": null }),
            json!({ "type": "object", "properties": [] }),
            json!({ "type": "object", "properties": "q" }),
            json!({ "type": "object", "properties": { "$ref": { "type": "string" } } }),
            json!({ "type": "object", "properties": { "a": { "properties": { "session_id": {} } } } }),
            json!({ "type": "object", "$defs": { "session_id": {} } }),
            json!({ "type": "object", "$ref": "#/$defs/A", "$defs": { "A": {} } }),
        ];
        for key in [
            "anyOf",
            "oneOf",
            "allOf",
            "not",
            "if",
            "then",
            "else",
            "patternProperties",
            "propertyNames",
            "maxProperties",
            "minProperties",
            "dependentRequired",
            "dependentSchemas",
            "unevaluatedProperties",
            "enum",
            "const",
            "$anchor",
            "x-own",
        ] {
            all.push(json!({ "type": "object", key: [] }));
        }
        for reference in [
            json!("#"),
            json!("#/"),
            json!("#/$defs/A"),
            json!("#/$defs/"),
            json!("#/definitions/C"),
            json!("#/properties/a"),
            json!("#/properties/"),
            json!("#/properties"),
            json!("#/properties/session_id"),
            json!("#/properties/intent"),
            json!("#/properties/intent/items"),
            json!("#/properties/session~1id"),
            json!("#/properties/session~0id"),
            json!("#/properties/sess%69on_id"),
            json!("#/properties/a%2Fb"),
            json!("https://example.com/schema"),
            json!("other.json#/a"),
            json!("#x"),
            json!(""),
            json!(5),
            json!(null),
            json!({}),
        ] {
            all.push(property(json!({ "$ref": reference })));
            all.push(property(json!({ "items": [{ "$ref": reference }] })));
        }
        all.push(property(json!({ "$dynamicRef": "#x" })));
        all.push(property(json!({ "$recursiveRef": "#" })));
        all.push(property(json!({ "$dynamicAnchor": "x" })));
        // How deep and how wide a schema is read, on each side of the limits.
        for depth in [61, 62, 63, 64, 65, 66] {
            all.push(nested(depth));
        }
        for count in [9_996, 9_997, 9_998, 9_999, 10_000, 10_001] {
            all.push(wide(count));
            all.push(required(count));
        }
        all
    }

    const TYPESCRIPT: &str = "import { readFileSync } from 'node:fs'
const { listedOwnership, advertise } = await import(process.argv[1])
const names = ['session_id', 'intent']
const sets = [names, ['session_id'], ['intent'], []]
const read = JSON.parse(readFileSync(0, 'utf8')).map((schema) => {
  const owner = listedOwnership(schema)
  return {
    readable: owner.readable,
    declared: owner.readable ? names.filter((name) => owner.declared.has(name)) : [],
    shown: sets.map((set) => {
      const shown = advertise(schema, set)
      return shown === undefined ? null : JSON.stringify(shown)
    }),
  }
})
process.stdout.write(JSON.stringify(read))
";

    /// TypeScript's own functions, run on the same schemas: what each says of the two names, and
    /// the schema it shows, character for character.
    #[test]
    fn typescript_reads_every_schema_the_same() {
        use crate::cases_test::{contract_dir, file_url, node, skip_without_node};
        let source = std::env::var_os("LSQUARE_TYPESCRIPT_SRC")
            .map_or_else(
                || contract_dir().join("../lsquare/src"),
                std::path::PathBuf::from,
            )
            .join("mcp/extras.ts");
        if !source.exists() {
            assert!(
                std::env::var_os("CI").is_none(),
                "the TypeScript SDK's source isn't at {}",
                source.display()
            );
            eprintln!(
                "skipped: the TypeScript SDK's source isn't at {}",
                source.display()
            );
            return;
        }
        if skip_without_node("that TypeScript reads tool schemas the same") {
            return;
        }
        let schemas = fixtures();
        let out = node(
            TYPESCRIPT,
            &[file_url(&source)],
            &Value::Array(schemas.clone()).to_string(),
        );
        let theirs: Vec<Value> = serde_json::from_str(&out).expect("TypeScript's answers");
        assert_eq!(theirs.len(), schemas.len());
        let sets: [&[&str]; 4] = [&[SESSION_ID, INTENT], &[SESSION_ID], &[INTENT], &[]];
        let mut differing = Vec::new();
        for (schema, theirs) in schemas.iter().zip(&theirs) {
            let schema = schema.as_object().expect("a schema is an object");
            let ownership = listed_ownership(schema);
            let declared: Vec<&str> = [SESSION_ID, INTENT]
                .into_iter()
                .filter(|name| ownership.declares(name))
                .collect();
            let shown: Vec<Value> = sets
                .iter()
                .map(|names| {
                    advertise(schema, names)
                        .map_or(Value::Null, |shown| Value::Object(shown).to_string().into())
                })
                .collect();
            let mine = json!({ "readable": ownership != Ownership::Unreadable, "declared": declared, "shown": shown });
            if mine != *theirs {
                let text: String = Value::Object(schema.clone())
                    .to_string()
                    .chars()
                    .take(160)
                    .collect();
                let brief =
                    |answer: &Value| -> String { answer.to_string().chars().take(400).collect() };
                differing.push(format!(
                    "{text}\n  rust:       {}\n  typescript: {}",
                    brief(&mine),
                    brief(theirs)
                ));
            }
        }
        println!(
            "{} schemas are read as TypeScript reads them",
            schemas.len() - differing.len()
        );
        assert!(
            differing.is_empty(),
            "{} of {} differ:\n{}",
            differing.len(),
            schemas.len(),
            differing.join("\n")
        );
    }

    #[test]
    fn a_schema_is_read_only_when_it_is_an_object_of_named_properties() {
        let readable = |declared: (bool, bool)| Ownership::Readable {
            session_id: declared.0,
            intent: declared.1,
        };
        for (schema, expected) in [
            (json!({ "type": "object" }), readable((false, false))),
            (
                json!({ "type": "object", "properties": { "q": { "type": "string" } }, "required": ["q"], "additionalProperties": false, "$schema": "x", "title": "T", "description": "d" }),
                readable((false, false)),
            ),
            (
                json!({ "type": "object", "properties": { "session_id": {} } }),
                readable((true, false)),
            ),
            (
                json!({ "type": "object", "required": ["intent"] }),
                readable((false, true)),
            ),
            // What could read or need any key.
            (
                json!({ "type": "object", "additionalProperties": true }),
                Ownership::Unreadable,
            ),
            (
                json!({ "type": "object", "additionalProperties": {} }),
                Ownership::Unreadable,
            ),
            (
                json!({ "type": "object", "anyOf": [] }),
                Ownership::Unreadable,
            ),
            (
                json!({ "type": "object", "patternProperties": {} }),
                Ownership::Unreadable,
            ),
            (json!({ "type": ["object", "null"] }), Ownership::Unreadable),
            (json!({ "properties": {} }), Ownership::Unreadable),
            (
                json!({ "type": "object", "properties": [] }),
                Ownership::Unreadable,
            ),
            (
                json!({ "type": "object", "required": "q" }),
                Ownership::Unreadable,
            ),
            (
                json!({ "type": "object", "required": [1] }),
                Ownership::Unreadable,
            ),
            (
                json!({ "type": "object", "maxProperties": 2 }),
                Ownership::Unreadable,
            ),
            (
                json!({ "type": "object", "enum": [{}] }),
                Ownership::Unreadable,
            ),
        ] {
            assert_eq!(
                listed_ownership(&object(schema.clone())),
                expected,
                "{schema}"
            );
        }
    }

    #[test]
    fn inputs_are_added_only_where_the_schema_takes_one_more() {
        let both = [SESSION_ID, INTENT];
        let schema = object(
            json!({ "type": "object", "properties": { "q": { "type": "string" } }, "required": ["q"], "title": "Search" }),
        );
        let shown = advertise(&schema, &both).expect("both are added");
        assert_eq!(
            Value::Object(shown),
            json!({
                "type": "object",
                "properties": {
                    "q": { "type": "string" },
                    "session_id": { "type": "string", "description": SESSION_DESCRIPTION },
                    "intent": { "type": "string", "description": INTENT_DESCRIPTION }
                },
                "required": ["q"],
                "title": "Search"
            })
        );
        // Optional: never in `required`. And the schema it was given is as it was.
        assert_eq!(schema["required"], json!(["q"]));
        assert!(!schema["properties"]
            .as_object()
            .unwrap()
            .contains_key(SESSION_ID));
        // Without properties, they come last.
        let bare = advertise(&object(json!({ "type": "object" })), &[INTENT]).expect("added");
        assert_eq!(bare.keys().collect::<Vec<_>>(), ["type", "properties"]);
        // One the tool has, or requires, isn't added again; with none left, nothing changes.
        let own = object(
            json!({ "type": "object", "properties": { "intent": { "type": "number" } }, "required": ["session_id"] }),
        );
        assert!(advertise(&own, &both).is_none());
        let one =
            object(json!({ "type": "object", "properties": { "intent": { "type": "number" } } }));
        let shown = advertise(&one, &both).expect("one is added");
        assert_eq!(shown["properties"]["intent"], json!({ "type": "number" }));
        assert!(shown["properties"]
            .as_object()
            .unwrap()
            .contains_key(SESSION_ID));
        assert!(advertise(&schema, &[]).is_none());
        for refused in [
            json!({ "type": "object", "additionalProperties": true }),
            json!({ "type": "object", "oneOf": [{ "type": "object" }] }),
            json!({ "type": "string" }),
            json!({ "type": "object", "properties": { "self": { "$ref": "#" } } }),
            json!({ "type": "object", "properties": { "a": { "$ref": "#/properties/session_id" } } }),
            json!({ "type": "object", "properties": { "a": { "$ref": "#/properties/sess%69on_id" } } }),
            json!({ "type": "object", "properties": { "a": { "$dynamicRef": "#x" } } }),
            json!({ "type": "object", "properties": { "a": { "$ref": "https://example.com/schema" } } }),
        ] {
            assert!(
                advertise(&object(refused.clone()), &both).is_none(),
                "{refused}"
            );
        }
        // References into its own definitions or properties are fine.
        let local = object(
            json!({ "type": "object", "properties": { "a": { "$ref": "#/$defs/A" }, "b": { "$ref": "#/properties/a" }, "c": { "$ref": "#/definitions/C" } }, "$defs": { "A": { "type": "string" } }, "definitions": { "C": {} } }),
        );
        assert!(advertise(&local, &both).is_some());
    }

    #[test]
    fn a_schema_too_wide_or_too_deep_to_walk_gets_no_inputs() {
        let wide: Map<String, Value> = (0..10_001)
            .map(|index| (format!("p{index}"), json!(true)))
            .collect();
        let schema = object(json!({ "type": "object", "properties": wide }));
        assert!(advertise(&schema, &[INTENT]).is_none());
        let mut deep = json!({ "type": "string" });
        for _ in 0..70 {
            deep = json!({ "type": "array", "items": deep });
        }
        let schema = object(json!({ "type": "object", "properties": { "d": deep } }));
        assert!(advertise(&schema, &[INTENT]).is_none());
        // It is still an object of named properties: the inputs are taken out of its calls.
        assert_eq!(
            listed_ownership(&schema),
            Ownership::Readable {
                session_id: false,
                intent: false
            }
        );
    }
}
