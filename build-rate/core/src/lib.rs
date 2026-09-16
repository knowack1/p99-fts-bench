//! The ingestion and measurement both build-rate harnesses share.
//!
//! `scyllarate` and `osrate` ask the same question of two engines — how fast
//! does a client fill an index, and how fast does the index actually build —
//! and they answered it with two copies of the same code that drifted. What
//! belongs to an engine is small and nameable: create the index, ingest a
//! document, read how many documents are indexed, reset. Everything else is
//! here.
//!
//! **Not a workspace member.** Each binary crate keeps its own `Cargo.lock`
//! beside its own `Cargo.toml`, because its `build.rs` reads that lock to stamp
//! the linked driver version into every CSV header and falls back silently to
//! `unknown` if it is not there. This crate's own lock governs nothing but
//! `cargo test` run from this directory: as a path dependency, the binary's
//! lock is what resolves these dependencies.
pub mod build_rate;
pub mod cli;
pub mod corpus;
pub mod gate;
pub mod index;
pub mod notes;
pub mod pacer;
pub mod report;
pub mod run;
pub mod samples;
pub mod sweep;

#[cfg(any(test, feature = "test-support"))]
pub mod test_support;

#[cfg(test)]
#[path = "durability_tests.rs"]
mod durability_tests;
