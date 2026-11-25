//! The wrapper passes every method of rmcp's `ServerHandler` on to the handler it wraps. One it
//! left out would answer with rmcp's default in place of the handler's own, without a word. So the
//! methods rmcp's trait has, read from the source of the version in use, must be the ones the
//! wrapper implements: a new rmcp with a new method fails here until the wrapper forwards it.

use std::collections::BTreeSet;
use std::path::PathBuf;
use std::process::Command;

/// The names of the functions declared in `source` between `from` and `to`.
fn methods(source: &str, from: &str, to: &str) -> BTreeSet<String> {
    let start = source
        .find(from)
        .unwrap_or_else(|| panic!("{from:?} isn't in the source"));
    let end = start
        + source[start..]
            .find(to)
            .unwrap_or_else(|| panic!("{to:?} isn't in the source"));
    source[start..end]
        .lines()
        .filter(|line| !line.trim_start().starts_with("//"))
        .filter_map(|line| {
            let line = line.trim_start();
            line.strip_prefix("async fn ")
                .or_else(|| line.strip_prefix("fn "))
        })
        .map(|rest| {
            rest.chars()
                .take_while(|ch| ch.is_alphanumeric() || *ch == '_')
                .collect()
        })
        .collect()
}

/// Where the rmcp this crate is built with has its source: asked of cargo, so a vendored or
/// patched rmcp is the one read.
fn rmcp_source() -> PathBuf {
    let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("Cargo.toml");
    let output = Command::new(env!("CARGO"))
        .args(["metadata", "--format-version", "1", "--manifest-path"])
        .arg(&manifest)
        .output()
        .expect("cargo metadata runs");
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let metadata: serde_json::Value =
        serde_json::from_slice(&output.stdout).expect("cargo's metadata");
    let package = metadata["packages"]
        .as_array()
        .expect("packages")
        .iter()
        .find(|package| package["name"] == "rmcp")
        .expect("rmcp is a dependency");
    PathBuf::from(package["manifest_path"].as_str().expect("its manifest"))
        .parent()
        .expect("its folder")
        .join("src/handler/server.rs")
}

#[test]
fn the_wrapper_forwards_every_method_of_rmcps_server_handler() {
    let rmcp = std::fs::read_to_string(rmcp_source()).expect("rmcp's ServerHandler");
    let theirs = methods(
        &rmcp,
        "macro_rules! server_handler_methods",
        "pub trait ServerHandler",
    );
    let wrapper =
        std::fs::read_to_string(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("src/mcp.rs"))
            .expect("the wrapper");
    let ours = methods(
        &wrapper,
        "impl<H: ServerHandler> ServerHandler for Instrumented<H>",
        "\n}\n",
    );
    assert!(
        theirs.len() >= 30,
        "rmcp's methods weren't found: {theirs:?}"
    );
    let missing: Vec<&String> = theirs.difference(&ours).collect();
    let unknown: Vec<&String> = ours.difference(&theirs).collect();
    assert!(
        missing.is_empty(),
        "rmcp's ServerHandler has methods the wrapper doesn't forward: {missing:?}"
    );
    assert!(
        unknown.is_empty(),
        "the wrapper implements methods rmcp's ServerHandler doesn't have: {unknown:?}"
    );
}
