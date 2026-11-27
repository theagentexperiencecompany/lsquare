//! What [`Client::capture`](crate::Client::capture) takes: one turn of an agent.

use serde_json::{Map, Number, Value};

/// How a turn ended.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
#[non_exhaustive]
pub enum TurnStatus {
    /// The agent answered. What a turn without a status is taken for.
    Completed,
    /// The turn failed.
    Error,
}

/// One turn of an agent: what it was asked and what it answered, the tools it called, what it
/// used. For [`Client::capture`](crate::Client::capture). Every field is optional.
///
/// ```
/// use lsquare::{ToolCall, Turn, Usage};
///
/// let turn = Turn::new()
///     .conversation_id("conv_42")
///     .turn_id("7")
///     .user_id("user_8")
///     .question("Where is my order?")
///     .answer("It left the warehouse this morning.")
///     .tool_call(ToolCall::new("find_order").duration_ms(120))
///     .usage(Usage::new().input_tokens(812).output_tokens(64));
/// ```
///
/// Long text is shortened and private data replaced by placeholders before anything is sent, as
/// for a tool call. The IDs are sent as they are given: use internal IDs, never an email address.
#[derive(Debug, Clone, Default)]
#[must_use = "a turn is recorded once it is given to Client::capture"]
pub struct Turn {
    fields: Map<String, Value>,
    tool_calls: Option<Vec<Value>>,
    error: Option<String>,
    error_type: Option<String>,
    client: Option<String>,
    client_version: Option<String>,
    metadata: Option<Map<String, Value>>,
}

impl Turn {
    /// A turn with nothing in it yet.
    pub fn new() -> Turn {
        Turn::default()
    }

    fn set(mut self, field: &str, value: impl Into<Value>) -> Turn {
        self.fields.insert(field.to_owned(), value.into());
        self
    }

    /// The customer organisation the turn was for, by your own ID for it.
    pub fn org_id(self, id: impl Into<String>) -> Turn {
        self.set("orgId", id.into())
    }

    /// The person the turn was for. An internal ID, not an email address: events can be deleted
    /// by it.
    pub fn user_id(self, id: impl Into<String>) -> Turn {
        self.set("userId", id.into())
    }

    /// The conversation the turn is part of.
    pub fn conversation_id(self, id: impl Into<String>) -> Turn {
        self.set("conversationId", id.into())
    }

    /// The turn's own ID within its conversation. With a conversation, the same turn captured
    /// twice is one event: its event ID is made from the two (and the org).
    pub fn turn_id(self, id: impl Into<String>) -> Turn {
        self.set("turnId", id.into())
    }

    /// The tool call the turn belongs to, by the ID [`Client::current_call_id`] gave inside the
    /// tool: for a turn captured after it, in a background job say. Inside a tool, a turn is
    /// linked to its call without it.
    ///
    /// [`Client::current_call_id`]: crate::Client::current_call_id
    pub fn call_id(self, id: impl Into<String>) -> Turn {
        self.set("callId", id.into())
    }

    /// The tool the turn is about, when it is about one.
    pub fn tool_name(self, name: impl Into<String>) -> Turn {
        self.set("toolName", name.into())
    }

    /// What the agent was asked.
    pub fn question(self, text: impl Into<String>) -> Turn {
        self.set("question", text.into())
    }

    /// What the agent answered.
    pub fn answer(self, text: impl Into<String>) -> Turn {
        self.set("answer", text.into())
    }

    /// How the turn ended. Left out, it completed.
    pub fn status(self, status: TurnStatus) -> Turn {
        self.set(
            "status",
            match status {
                TurnStatus::Completed => "completed",
                TurnStatus::Error => "error",
            },
        )
    }

    /// What went wrong, in words. It doesn't set the status: give `status(TurnStatus::Error)` too
    /// for a turn that failed.
    pub fn error(mut self, message: impl Into<String>) -> Turn {
        self.error = Some(message.into());
        self
    }

    /// The kind of error, such as its type's name. Recorded with [`Turn::error`]'s message, not
    /// without one.
    pub fn error_type(mut self, kind: impl Into<String>) -> Turn {
        self.error_type = Some(kind.into());
        self
    }

    /// Adds a tool the agent called in the turn.
    pub fn tool_call(mut self, call: ToolCall) -> Turn {
        self.tool_calls
            .get_or_insert_with(Vec::new)
            .push(Value::Object(call.fields));
        self
    }

    /// The tools the agent called in the turn, replacing any given so far. An empty list says it
    /// called none; a turn that never names its tool calls says nothing of them.
    pub fn tool_calls(mut self, calls: impl IntoIterator<Item = ToolCall>) -> Turn {
        self.tool_calls = Some(
            calls
                .into_iter()
                .map(|call| Value::Object(call.fields))
                .collect(),
        );
        self
    }

    /// What this turn used: tokens and cost.
    pub fn usage(self, usage: Usage) -> Turn {
        self.set("usage", Value::Object(usage.fields))
    }

    /// How long the turn's parts took: JSON of your own, as you want to see it in LSquare.
    pub fn latency(self, value: impl Into<Value>) -> Turn {
        self.set("latency", value)
    }

    /// How the answer was checked: JSON of your own.
    pub fn verification(self, value: impl Into<Value>) -> Turn {
        self.set("verification", value)
    }

    /// The app the person used, such as `web` or `slack`.
    pub fn client(mut self, name: impl Into<String>) -> Turn {
        self.client = Some(name.into());
        self
    }

    /// That app's version. Recorded with [`Turn::client`]'s name, not without one.
    pub fn client_version(mut self, version: impl Into<String>) -> Turn {
        self.client_version = Some(version.into());
        self
    }

    /// The ID of the API key the turn ran under: never the key.
    pub fn api_key_id(self, id: impl Into<String>) -> Turn {
        self.set("apiKeyId", id.into())
    }

    /// Where in your product the turn happened.
    pub fn surface(self, surface: impl Into<String>) -> Turn {
        self.set("surface", surface.into())
    }

    /// Adds a value of your own to the event's metadata: a string, a number, a boolean or null.
    /// Anything else is left out, and the event says so in `dropped`. Metadata is never
    /// shortened: an event with more than fits is dropped.
    pub fn metadata(mut self, key: impl Into<String>, value: impl Into<Value>) -> Turn {
        self.metadata
            .get_or_insert_with(Map::new)
            .insert(key.into(), value.into());
        self
    }

    /// The turn as the event layer reads it: `capture()`'s input in the other SDKs.
    pub(crate) fn into_fields(self) -> Map<String, Value> {
        let Turn {
            mut fields,
            tool_calls,
            error,
            error_type,
            client,
            client_version,
            metadata,
        } = self;
        if let Some(calls) = tool_calls {
            fields.insert("toolCalls".into(), Value::Array(calls));
        }
        if let Some(message) = error {
            let mut error = Map::new();
            error.insert("message".into(), message.into());
            if let Some(kind) = error_type {
                error.insert("type".into(), kind.into());
            }
            fields.insert("error".into(), Value::Object(error));
        }
        if let Some(name) = client {
            let client = match client_version {
                Some(version) => {
                    let mut named = Map::new();
                    named.insert("name".into(), name.into());
                    named.insert("version".into(), version.into());
                    Value::Object(named)
                }
                None => Value::String(name),
            };
            fields.insert("client".into(), client);
        }
        if let Some(metadata) = metadata {
            fields.insert("metadata".into(), Value::Object(metadata));
        }
        fields
    }
}

/// A tool the agent called in a turn: one of the agent's own, such as `search_docs`. For
/// [`Turn::tool_call`].
#[derive(Debug, Clone)]
#[must_use = "a tool call is recorded once it is added to a Turn"]
pub struct ToolCall {
    fields: Map<String, Value>,
}

impl ToolCall {
    /// A call of the tool `name`.
    pub fn new(name: impl Into<String>) -> ToolCall {
        let mut fields = Map::new();
        fields.insert("name".into(), name.into().into());
        ToolCall { fields }
    }

    fn set(mut self, field: &str, value: impl Into<Value>) -> ToolCall {
        self.fields.insert(field.to_owned(), value.into());
        self
    }

    /// What the tool was called with, as JSON. Shortened and redacted like a recorded call's
    /// arguments.
    pub fn input(self, input: impl Into<Value>) -> ToolCall {
        self.set("input", input)
    }

    /// How long the tool's result was, in characters. The result itself is never recorded.
    pub fn result_chars(self, chars: u64) -> ToolCall {
        self.set("resultChars", chars)
    }

    /// Whether the tool's result was an error.
    pub fn result_is_error(self, is_error: bool) -> ToolCall {
        self.set("resultIsError", is_error)
    }

    /// What went wrong, in words.
    pub fn error_message(self, message: impl Into<String>) -> ToolCall {
        self.set("errorMessage", message.into())
    }

    /// How long the tool took, in milliseconds.
    pub fn duration_ms(self, milliseconds: u64) -> ToolCall {
        self.set("durationMs", milliseconds)
    }
}

/// What one turn used. For [`Turn::usage`].
#[derive(Debug, Clone, Default)]
#[must_use = "usage is recorded once it is added to a Turn"]
pub struct Usage {
    fields: Map<String, Value>,
}

impl Usage {
    /// Nothing counted yet.
    pub fn new() -> Usage {
        Usage::default()
    }

    /// Tokens the model read.
    pub fn input_tokens(mut self, tokens: u64) -> Usage {
        self.fields.insert("inputTokens".into(), tokens.into());
        self
    }

    /// Tokens the model wrote.
    pub fn output_tokens(mut self, tokens: u64) -> Usage {
        self.fields.insert("outputTokens".into(), tokens.into());
        self
    }

    /// What the turn cost, in US dollars. A value that isn't a finite number is left out, and
    /// the event says so in `dropped`.
    pub fn cost_usd(mut self, dollars: f64) -> Usage {
        // What isn't a number goes as something that plainly isn't one, never sent: the event
        // then notes it as the other SDKs do (`usage.costUsd`: `wrong_type`).
        let cost = Number::from_f64(dollars).map_or(Value::Bool(false), Value::Number);
        self.fields.insert("costUsd".into(), cost);
        self
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn a_turn_is_what_the_other_sdks_take() {
        let turn = Turn::new()
            .org_id("org_1")
            .user_id("user_8")
            .conversation_id("conv_42")
            .turn_id("7")
            .call_id("call_3")
            .tool_name("ask")
            .question("Where is my order?")
            .answer("On its way.")
            .status(TurnStatus::Error)
            .error("The carrier didn't answer")
            .error_type("Timeout")
            .tool_call(
                ToolCall::new("find_order")
                    .input(json!({ "id": 12 }))
                    .result_chars(240)
                    .result_is_error(false)
                    .duration_ms(120),
            )
            .tool_call(ToolCall::new("carrier").error_message("timed out"))
            .usage(
                Usage::new()
                    .input_tokens(812)
                    .output_tokens(64)
                    .cost_usd(0.0031),
            )
            .latency(json!({ "totalMs": 950 }))
            .verification(json!({ "grounded": true }))
            .client("web")
            .client_version("2.4.0")
            .api_key_id("key_5")
            .surface("support")
            .metadata("plan", "pro")
            .metadata("seats", 12);
        assert_eq!(
            Value::Object(turn.into_fields()),
            json!({
                "orgId": "org_1",
                "userId": "user_8",
                "conversationId": "conv_42",
                "turnId": "7",
                "callId": "call_3",
                "toolName": "ask",
                "question": "Where is my order?",
                "answer": "On its way.",
                "status": "error",
                "usage": { "inputTokens": 812, "outputTokens": 64, "costUsd": 0.0031 },
                "latency": { "totalMs": 950 },
                "verification": { "grounded": true },
                "apiKeyId": "key_5",
                "surface": "support",
                "toolCalls": [
                    { "name": "find_order", "input": { "id": 12 }, "resultChars": 240, "resultIsError": false, "durationMs": 120 },
                    { "name": "carrier", "errorMessage": "timed out" },
                ],
                "error": { "message": "The carrier didn't answer", "type": "Timeout" },
                "client": { "name": "web", "version": "2.4.0" },
                "metadata": { "plan": "pro", "seats": 12 },
            })
        );
    }

    #[test]
    fn what_a_turn_never_named_is_left_out() {
        assert!(Turn::new().into_fields().is_empty());
        // A list with nothing in it says the agent called no tools.
        let none = Turn::new().tool_calls([]).into_fields();
        assert_eq!(Value::Object(none), json!({ "toolCalls": [] }));
        // A client without a version is its name; a version or an error type alone is nothing.
        let named = Turn::new().client("slack").into_fields();
        assert_eq!(Value::Object(named), json!({ "client": "slack" }));
        let alone = Turn::new().client_version("1").error_type("Timeout");
        assert!(alone.into_fields().is_empty());
        // A cost that isn't a number isn't given as one: the event notes it, and leaves it out.
        let usage = Turn::new()
            .usage(Usage::new().cost_usd(1.5).cost_usd(f64::NAN))
            .into_fields();
        assert_eq!(
            Value::Object(usage),
            json!({ "usage": { "costUsd": false } })
        );
    }
}
