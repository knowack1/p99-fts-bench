//! The stats header names the compiler that produced the binary, so the version
//! is read from the toolchain that actually ran rather than restated by hand
//! where it could drift away from what was linked.
use std::process::Command;

const UNKNOWN: &str = "unknown";

fn main() {
    println!("cargo:rerun-if-env-changed=RUSTC");
    println!(
        "cargo:rustc-env=ENGINE_MOCK_RUSTC_VERSION={}",
        rustc_version()
    );
}

fn rustc_version() -> String {
    let rustc = std::env::var("RUSTC").unwrap_or_else(|_| "rustc".to_string());
    let Ok(output) = Command::new(rustc).arg("--version").output() else {
        return UNKNOWN.to_string();
    };
    String::from_utf8(output.stdout)
        .map(|version| version.trim().to_string())
        .unwrap_or_else(|_| UNKNOWN.to_string())
}
