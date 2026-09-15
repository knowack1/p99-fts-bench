//! The CSV header names the client that produced the numbers, so the versions
//! are read from the resolved lock file rather than restated by hand where they
//! could drift away from what was actually linked.
//!
//! Two of them: the OpenSearch client builds the request, and the HTTP client
//! underneath it is what actually holds the connections and speaks the wire.
use std::path::PathBuf;

const UNKNOWN: &str = "unknown";

fn main() {
    println!("cargo:rerun-if-changed=Cargo.lock");
    export("OPENSEARCH_CLIENT_VERSION", "opensearch");
    export("HTTP_CLIENT_VERSION", "reqwest");
}

fn export(variable: &str, package: &str) {
    let version = locked_version(package).unwrap_or_else(|| UNKNOWN.to_string());
    println!("cargo:rustc-env={variable}={version}");
}

fn locked_version(package: &str) -> Option<String> {
    let text = std::fs::read_to_string(lock_path()?).ok()?;
    version_after_name(&text, package)
}

fn lock_path() -> Option<PathBuf> {
    let manifest = std::env::var("CARGO_MANIFEST_DIR").ok()?;
    Some(PathBuf::from(manifest).join("Cargo.lock"))
}

fn version_after_name(lock: &str, package: &str) -> Option<String> {
    let needle = format!("name = \"{package}\"");
    let mut lines = lock.lines().skip_while(|line| line.trim() != needle);
    lines.next()?;
    lines.next().and_then(quoted_value)
}

fn quoted_value(line: &str) -> Option<String> {
    let (key, value) = line.split_once('=')?;
    if key.trim() != "version" {
        return None;
    }
    Some(value.trim().trim_matches('"').to_string())
}
