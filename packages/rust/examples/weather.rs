//! A server with one tool, served over HTTP on your machine, whose calls are recorded in LSquare.
//!
//! See the first event without sending anything:
//!
//! ```sh
//! LSQUARE_DRY_RUN=1 cargo run --example weather
//! ```
//!
//! and in another terminal, call the tool:
//!
//! ```sh
//! curl -s http://127.0.0.1:3000/mcp \
//!   -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' \
//!   -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_forecast","arguments":{"city":"Lisbon"}}}'
//! ```

use lsquare::mcp::{instrument, InstrumentOptions};
use rmcp::handler::server::wrapper::Parameters;
use rmcp::transport::streamable_http_server::session::local::LocalSessionManager;
use rmcp::transport::{StreamableHttpServerConfig, StreamableHttpService};
use rmcp::{tool, tool_handler, tool_router, ServerHandler};

#[derive(Clone)]
struct Weather;

#[derive(serde::Deserialize, schemars::JsonSchema)]
struct Forecast {
    /// The city to give the forecast for.
    city: String,
}

#[tool_router]
impl Weather {
    #[tool(description = "Gives tomorrow's forecast for a city.")]
    async fn get_forecast(&self, Parameters(Forecast { city }): Parameters<Forecast>) -> String {
        format!("Tomorrow in {city}: sunny, 21°C.")
    }
}

#[tool_handler(name = "weather", version = "1.0.0")]
impl ServerHandler for Weather {}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let lsquare =
        lsquare::Client::new(lsquare::Options::new().api_key(std::env::var("LSQUARE_KEY").ok()));

    // A new server for each session, as rmcp serves them over HTTP; instrument each one.
    let recorder = lsquare.clone();
    let service = StreamableHttpService::new(
        move || Ok(instrument(Weather, &recorder, InstrumentOptions::new())),
        LocalSessionManager::default().into(),
        // Each request on its own, answered as JSON: one `curl` is a whole call.
        StreamableHttpServerConfig::default()
            .with_legacy_session_mode(false)
            .with_json_response(true),
    );
    let router = axum::Router::new().nest_service("/mcp", service);
    // On port 3000, or the one `PORT` names.
    let port = std::env::var("PORT")
        .ok()
        .and_then(|port| port.parse::<u16>().ok())
        .unwrap_or(3000);
    let listener = tokio::net::TcpListener::bind(("127.0.0.1", port)).await?;
    eprintln!("Listening on http://127.0.0.1:{port}/mcp");
    let stopped = async {
        tokio::signal::ctrl_c().await.ok();
    };
    axum::serve(listener, router)
        .with_graceful_shutdown(stopped)
        .await?;

    // Sends what is still queued before the process exits.
    lsquare.shutdown().await;
    Ok(())
}
