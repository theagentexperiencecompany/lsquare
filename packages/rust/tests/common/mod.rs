//! For the tests: a stand-in for LSquare's ingestion, and an MCP client and server joined in memory.
#![allow(dead_code)]

use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use lsquare::{Client, Error, Level, Options};
use rmcp::model::{CallToolRequestParams, CallToolResult};
use rmcp::service::{RoleClient, RunningService};
use rmcp::{ServerHandler, ServiceError, ServiceExt};
use serde_json::{json, Map, Value};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

/// One request the ingestion got.
#[derive(Debug, Clone)]
pub struct Received {
    pub path: String,
    pub authorization: Option<String>,
    pub content_type: Option<String>,
    pub events: Vec<Value>,
    pub at: Instant,
}

impl Received {
    pub fn ids(&self) -> Vec<String> {
        self.events
            .iter()
            .filter_map(|event| event["eventId"].as_str().map(str::to_owned))
            .collect()
    }
}

/// What the ingestion answers.
pub struct Reply {
    pub status: u16,
    pub headers: Vec<(&'static str, String)>,
    pub body: String,
    pub delay: Duration,
    /// Held until this is notified.
    pub gate: Option<Arc<tokio::sync::Notify>>,
}

impl Reply {
    /// Every event accepted.
    pub fn accept(received: &Received) -> Reply {
        Reply::json(json!({ "accepted": received.ids(), "duplicates": [], "rejected": [] }))
    }

    pub fn json(body: Value) -> Reply {
        Reply {
            status: 200,
            headers: Vec::new(),
            body: body.to_string(),
            delay: Duration::ZERO,
            gate: None,
        }
    }

    pub fn status(status: u16) -> Reply {
        Reply {
            status,
            headers: Vec::new(),
            body: String::new(),
            delay: Duration::ZERO,
            gate: None,
        }
    }

    pub fn header(mut self, name: &'static str, value: impl Into<String>) -> Reply {
        self.headers.push((name, value.into()));
        self
    }

    pub fn after(mut self, delay: Duration) -> Reply {
        self.delay = delay;
        self
    }

    /// Answers once `gate` is notified, so a test decides when a request ends.
    pub fn when(mut self, gate: &Arc<tokio::sync::Notify>) -> Reply {
        self.gate = Some(gate.clone());
        self
    }
}

/// A local HTTP server that answers each request as `reply` says, given how many came before it.
pub struct Ingestion {
    pub url: String,
    requests: Arc<Mutex<Vec<Received>>>,
}

impl Ingestion {
    pub async fn start(
        reply: impl Fn(usize, &Received) -> Reply + Send + Sync + 'static,
    ) -> Ingestion {
        let listener = TcpListener::bind("127.0.0.1:0")
            .await
            .expect("a local port");
        let url = format!("http://{}", listener.local_addr().expect("its address"));
        let requests: Arc<Mutex<Vec<Received>>> = Arc::default();
        let (seen, reply) = (requests.clone(), Arc::new(reply));
        tokio::spawn(async move {
            loop {
                let Ok((mut socket, _)) = listener.accept().await else {
                    return;
                };
                let (seen, reply) = (seen.clone(), reply.clone());
                tokio::spawn(async move {
                    let mut buffer = Vec::new();
                    let mut chunk = [0u8; 16 * 1024];
                    let (head_end, length) = loop {
                        let Ok(read) = socket.read(&mut chunk).await else {
                            return;
                        };
                        if read == 0 {
                            return;
                        }
                        buffer.extend_from_slice(&chunk[..read]);
                        if let Some(end) =
                            buffer.windows(4).position(|window| window == b"\r\n\r\n")
                        {
                            let head = String::from_utf8_lossy(&buffer[..end]).to_lowercase();
                            let length = head
                                .lines()
                                .find_map(|line| line.strip_prefix("content-length:"))
                                .and_then(|value| value.trim().parse::<usize>().ok())
                                .unwrap_or(0);
                            break (end + 4, length);
                        }
                    };
                    while buffer.len() < head_end + length {
                        let Ok(read) = socket.read(&mut chunk).await else {
                            return;
                        };
                        if read == 0 {
                            return;
                        }
                        buffer.extend_from_slice(&chunk[..read]);
                    }
                    let head = String::from_utf8_lossy(&buffer[..head_end]).into_owned();
                    let header = |name: &str| {
                        head.lines().find_map(|line| {
                            line.split_once(':')
                                .filter(|(key, _)| key.eq_ignore_ascii_case(name))
                                .map(|(_, value)| value.trim().to_owned())
                        })
                    };
                    let body: Value = serde_json::from_slice(&buffer[head_end..head_end + length])
                        .unwrap_or(Value::Null);
                    let received = Received {
                        path: head
                            .split_whitespace()
                            .nth(1)
                            .unwrap_or_default()
                            .to_owned(),
                        authorization: header("authorization"),
                        content_type: header("content-type"),
                        events: body["events"].as_array().cloned().unwrap_or_default(),
                        at: Instant::now(),
                    };
                    let count = {
                        let mut seen = seen.lock().unwrap();
                        seen.push(received.clone());
                        seen.len() - 1
                    };
                    let answer = reply(count, &received);
                    if let Some(gate) = &answer.gate {
                        gate.notified().await;
                    }
                    tokio::time::sleep(answer.delay).await;
                    let mut response = format!(
                        "HTTP/1.1 {} Status\r\ncontent-length: {}\r\nconnection: close\r\n",
                        answer.status,
                        answer.body.len()
                    );
                    for (name, value) in &answer.headers {
                        response.push_str(&format!("{name}: {value}\r\n"));
                    }
                    response.push_str("\r\n");
                    response.push_str(&answer.body);
                    let _ = socket.write_all(response.as_bytes()).await;
                    let _ = socket.shutdown().await;
                });
            }
        });
        Ingestion { url, requests }
    }

    /// An ingestion that accepts everything.
    pub async fn accepting() -> Ingestion {
        Ingestion::start(|_, received| Reply::accept(received)).await
    }

    pub fn requests(&self) -> Vec<Received> {
        self.requests.lock().unwrap().clone()
    }

    /// Every event received, in order.
    pub fn events(&self) -> Vec<Value> {
        self.requests()
            .into_iter()
            .flat_map(|request| request.events)
            .collect()
    }
}

/// An error a client reported, with the event it was about.
pub type Reported = (Error, Option<Value>);

/// What a client reported and logged.
#[derive(Clone, Default)]
pub struct Seen {
    pub errors: Arc<Mutex<Vec<Reported>>>,
    pub lines: Arc<Mutex<Vec<(Level, String)>>>,
}

impl Seen {
    pub fn codes(&self) -> Vec<String> {
        self.errors
            .lock()
            .unwrap()
            .iter()
            .map(|(error, _)| error.code.as_str().to_owned())
            .collect()
    }

    pub fn errors(&self) -> Vec<Error> {
        self.errors
            .lock()
            .unwrap()
            .iter()
            .map(|(error, _)| error.clone())
            .collect()
    }

    pub fn lines(&self) -> Vec<String> {
        self.lines
            .lock()
            .unwrap()
            .iter()
            .map(|(_, line)| line.clone())
            .collect()
    }

    /// Options that report here.
    pub fn options(&self) -> Options {
        let (errors, lines) = (self.errors.clone(), self.lines.clone());
        Options::new()
            .on_error(move |error, event| {
                errors.lock().unwrap().push((
                    error.clone(),
                    event.map(|event| Value::Object(event.as_map().clone())),
                ))
            })
            .logger(move |level, line| lines.lock().unwrap().push((level, line.to_owned())))
    }
}

/// A client that sends to `ingestion`, and what it reports. One request at a time, unless a test
/// says otherwise: with several in flight, events don't reach the ingestion in the order they
/// were recorded, and tests read them by their place.
pub fn client(ingestion: &Ingestion, configure: impl FnOnce(Options) -> Options) -> (Client, Seen) {
    let seen = Seen::default();
    let options = configure(
        seen.options()
            .api_key("lsq_test_key".to_owned())
            .endpoint(ingestion.url.clone())
            .max_concurrent_requests(1),
    );
    (Client::new(options), seen)
}

/// An MCP client that says who it is.
#[derive(Clone, Default)]
pub struct App;

impl rmcp::ClientHandler for App {
    fn get_info(&self) -> rmcp::model::ClientConfig {
        let mut info = rmcp::model::ClientConfig::default();
        info.client_info.name = "test-app".into();
        info.client_info.version = "4.5.6".into();
        info
    }
}

pub type Mcp = RunningService<RoleClient, App>;

/// An MCP client connected to `server` in memory.
pub async fn connect<H: ServerHandler>(server: H) -> Mcp {
    connect_as(server, App).await
}

/// The same, as a client of one's choosing.
pub async fn connect_as<H: ServerHandler, C: rmcp::ClientHandler>(
    server: H,
    app: C,
) -> RunningService<RoleClient, C> {
    let (server_transport, client_transport) = tokio::io::duplex(64 * 1024);
    tokio::spawn(async move {
        if let Ok(running) = server.serve(server_transport).await {
            let _ = running.waiting().await;
        }
    });
    app.serve(client_transport)
        .await
        .expect("the MCP client connects")
}

pub fn arguments(value: Value) -> Map<String, Value> {
    value.as_object().cloned().unwrap_or_default()
}

/// Calls a tool with arguments.
pub async fn call<C: rmcp::ClientHandler>(
    mcp: &RunningService<RoleClient, C>,
    tool: &'static str,
    args: Value,
) -> Result<CallToolResult, ServiceError> {
    mcp.call_tool(CallToolRequestParams::new(tool).with_arguments(arguments(args)))
        .await
}

/// Where the app's own code is held: whoever gets there says so, and stays until it is released
/// (or five seconds have passed, so a test that fails doesn't hang).
#[derive(Clone, Default)]
pub struct Hold {
    state: Arc<(Mutex<(usize, bool)>, std::sync::Condvar)>,
}

impl Hold {
    /// For the code that is held.
    pub fn wait(&self) {
        let (state, changed) = &*self.state;
        let mut held = state.lock().unwrap();
        held.0 += 1;
        changed.notify_all();
        let _unused = changed
            .wait_timeout_while(held, Duration::from_secs(5), |held| !held.1)
            .unwrap();
    }

    /// How many got there.
    pub fn entered(&self) -> usize {
        self.state.0.lock().unwrap().0
    }

    pub fn release(&self) {
        let (state, changed) = &*self.state;
        state.lock().unwrap().1 = true;
        changed.notify_all();
    }
}

/// Waits until `done`, at most a few seconds.
pub async fn until(done: impl Fn() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(5);
    while !done() && Instant::now() < deadline {
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    assert!(done(), "it didn't happen within 5 seconds");
}

// The server under test.

use rmcp::model::{
    CallToolResponse, ContentBlock, ListPromptsResult, ListToolsResult, PaginatedRequestParams,
    Prompt, ServerCapabilities, ServerConfig, Tool,
};
use rmcp::service::{RequestContext, RoleServer};
use rmcp::ErrorData as McpError;

/// A server with a tool for each way a call can end.
#[derive(Clone, Default)]
pub struct Tools {
    /// Whether it answers `get_tool`, as a server made with rmcp's macros does.
    pub registered: bool,
    /// Its `get_tool` panics.
    pub get_tool_panics: bool,
    /// The instructions it gives clients.
    pub instructions: Option<String>,
}

pub const ECHO_DESCRIPTION: &str = "Says back what it was told.";

fn tool(name: &'static str, description: &'static str) -> Tool {
    Tool::new(name, description, arguments(json!({ "type": "object" })))
}

impl ServerHandler for Tools {
    fn get_info(&self) -> ServerConfig {
        let mut info = ServerConfig::default();
        info.server_info.name = "test-server".into();
        info.server_info.version = "1.2.3".into();
        info.instructions = self.instructions.clone();
        info.capabilities = ServerCapabilities::builder()
            .enable_tools()
            .enable_prompts()
            .build();
        info
    }

    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        context: RequestContext<RoleServer>,
    ) -> Result<CallToolResponse, McpError> {
        let text = |key: &str| {
            request
                .arguments
                .as_ref()
                .and_then(|arguments| arguments.get(key))
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_owned()
        };
        let result = match request.name.as_ref() {
            "echo" => CallToolResult::success(vec![ContentBlock::text(text("text"))]),
            "structured" => {
                let mut result = CallToolResult::success(vec![ContentBlock::text("done")]);
                result.structured_content = Some(json!({ "password": "hunter2", "count": 2 }));
                result
            }
            "image" => CallToolResult::success(vec![
                ContentBlock::image("QUJDRA==", "image/png"),
                ContentBlock::text("a picture"),
            ]),
            "long" => CallToolResult::success(vec![ContentBlock::text("lorem ipsum ".repeat(500))]),
            "tool_error" => {
                CallToolResult::error(vec![ContentBlock::text("it broke for ann@example.com")])
            }
            "refused" => return Err(McpError::invalid_params("bad arguments", None)),
            "panic" => panic!("the tool panicked"),
            "never" => std::future::pending().await,
            "pause" => {
                tokio::time::sleep(Duration::from_millis(50)).await;
                CallToolResult::success(vec![ContentBlock::text("rested")])
            }
            "slow" => {
                tokio::select! {
                    () = context.ct.cancelled() => return Err(McpError::internal_error("cancelled", None)),
                    () = tokio::time::sleep(Duration::from_secs(30)) => CallToolResult::success(vec![ContentBlock::text("late")]),
                }
            }
            _ => return Err(McpError::invalid_params("no such tool", None)),
        };
        Ok(result.into())
    }

    async fn list_tools(
        &self,
        _request: Option<PaginatedRequestParams>,
        _context: RequestContext<RoleServer>,
    ) -> Result<ListToolsResult, McpError> {
        Ok(ListToolsResult::with_all_items(vec![
            tool("echo", ECHO_DESCRIPTION),
            tool("image", "Draws a picture."),
        ]))
    }

    fn get_tool(&self, name: &str) -> Option<Tool> {
        assert!(!self.get_tool_panics, "get_tool panicked");
        (self.registered && name == "echo").then(|| tool("echo", "The registered description."))
    }

    async fn list_prompts(
        &self,
        _request: Option<PaginatedRequestParams>,
        _context: RequestContext<RoleServer>,
    ) -> Result<ListPromptsResult, McpError> {
        Ok(ListPromptsResult::with_all_items(vec![Prompt::new(
            "the-handlers-own",
            Some("A prompt only the wrapped handler has."),
            None,
        )]))
    }
}

/// Runs the contract's validator on events, through Node: `valid`, or why each is rejected.
/// `None` where Node isn't installed, away from CI.
pub fn ingestion_says(events: &[Value]) -> Option<Vec<String>> {
    use std::io::Write;
    use std::process::{Command, Stdio};
    let node = Command::new("node")
        .arg("--version")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .is_ok_and(|status| status.success());
    if !node {
        assert!(
            std::env::var_os("CI").is_none(),
            "node isn't installed: the contract's validator can't be run"
        );
        return None;
    }
    let contract = std::env::var_os("LSQUARE_CONTRACT_DIR").map_or_else(
        || std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../contract"),
        std::path::PathBuf::from,
    );
    let validator = format!(
        "file://{}",
        contract
            .join("src/validate.ts")
            .canonicalize()
            .expect("the contract's validator")
            .display()
    );
    let script = "import { readFileSync } from 'node:fs'\nconst { validateEvent } = await import(process.argv[1])\nconst lines = readFileSync(0, 'utf8').split('\\n').filter(Boolean)\nprocess.stdout.write(JSON.stringify(lines.map((line) => { const result = validateEvent(JSON.parse(line)); return result.ok ? 'valid' : result.reason })))";
    let mut child = Command::new("node")
        .args([
            "--import",
            "tsx",
            "--input-type=module",
            "-e",
            script,
            &validator,
        ])
        .current_dir(&contract)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("node starts");
    let lines: String = events.iter().map(|event| format!("{event}\n")).collect();
    child
        .stdin
        .take()
        .expect("node's input")
        .write_all(lines.as_bytes())
        .expect("the events are written");
    let output = child.wait_with_output().expect("node runs");
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    Some(serde_json::from_slice(&output.stdout).expect("the validator's answers"))
}
