//! Record every tool call your MCP server handles in [LSquare](https://lsquare.io).
//!
//! A [`Client`] sends events to LSquare; [`mcp::instrument`] wraps a server built with the
//! official Rust MCP SDK, [`rmcp`], so that each `tools/call` becomes one event. Before anything
//! leaves the process, long values are shortened and common private data (emails, phone numbers,
//! card numbers, keys and tokens, IP addresses) is replaced by placeholders, by the same rules as
//! LSquare's TypeScript and Python SDKs.
//!
#![cfg_attr(feature = "rmcp", doc = "```no_run")]
#![cfg_attr(not(feature = "rmcp"), doc = "```ignore")]
//! # use rmcp::{ServerHandler, ServiceExt};
//! # #[derive(Clone)] struct Weather;
//! # impl ServerHandler for Weather {}
//! # async fn run() -> Result<(), Box<dyn std::error::Error>> {
//! use lsquare::mcp::{instrument, InstrumentOptions};
//!
//! let lsquare = lsquare::Client::new(lsquare::Options::new().api_key(std::env::var("LSQUARE_KEY").ok()));
//! let server = instrument(Weather, &lsquare, InstrumentOptions::new());
//! let running = server.serve(rmcp::transport::stdio()).await?;
//! running.waiting().await?;
//! // Sends what is still queued: nothing is sent once the runtime is gone.
//! lsquare.shutdown().await;
//! # Ok(()) }
//! ```
//!
//! Nothing here can make a tool call fail. The SDK never panics and returns no errors: what goes
//! wrong is reported to [`Options::on_error`], or logged. With `LSQUARE_DRY_RUN=1` it sends
//! nothing, and logs one line per event with its fields and size.
//!
//! This is a first beta: tool calls on `rmcp` servers only. `README.md` lists what the other SDKs
//! do that this one doesn't yet.
// Without the wrapper nothing records an event yet, so most of the crate has no caller.
#![cfg_attr(not(feature = "rmcp"), allow(dead_code))]
#![warn(missing_docs)]

mod client;
mod event;
#[cfg(feature = "rmcp")]
mod extras;
mod json;
#[cfg(feature = "rmcp")]
pub mod mcp;
mod normalize;
mod redact;
mod shape;
mod time;

pub use client::{Client, Error, ErrorCode, Event, FlushResult, Level, Options, DEFAULT_ENDPOINT};
pub use redact::patterns::Pattern;

#[cfg(test)]
mod cases_test;
#[cfg(test)]
mod contract_test;
#[cfg(test)]
mod generated_test;
#[cfg(test)]
mod testing;
