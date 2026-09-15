//! The query loop and measurement both search-latency harnesses share.
//!
//! `scyllasearch` and `ossearch` ask one question of three interfaces — what
//! does a full-text search cost at N requests in flight — and the part that
//! depends on which interface is underneath is one method: answer this query,
//! say how many documents came back. Everything around it is here: the query
//! set, the closed loop, the percentiles, the matrix, the CSV, and the rule
//! that the index must be complete before the first query is timed.
//!
//! **The index build is a precondition here, not a measurement.** That is the
//! whole difference from the sibling tree: `../../build-rate` exists to time
//! the build, so it may never force an engine's hand; this one exists to time
//! reads against a finished index, so it fills the index as fast as it can,
//! asks for a refresh, and refuses to measure until the count matches the
//! corpus. The loader it fills with is `build-rate`'s, so "the index was
//! complete" is one claim rather than two implementations of it.
//!
//! **Not a workspace member**, for the reason the sibling gives: each binary
//! crate keeps its own `Cargo.lock` beside its own `Cargo.toml`, because its
//! `build.rs` reads that lock to stamp the linked driver version into every
//! CSV header.
pub mod bootstrap;
pub mod cell;
pub mod cli;
pub mod latencies;
pub mod matrix;
pub mod queries;
pub mod report;
pub mod run;
pub mod search;

#[cfg(any(test, feature = "test-support"))]
pub mod test_support;
