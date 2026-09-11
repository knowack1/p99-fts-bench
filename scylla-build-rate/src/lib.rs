//! Concurrency sweep for ScyllaDB FTS ingest: docs/s and p99 per concurrency level.
pub mod build_rate;
pub mod cli;
pub mod corpus;
pub mod insert;
pub mod notes;
pub mod report;
pub mod reset;
pub mod session;
pub mod sweep;
pub mod vstore;

#[cfg(test)]
pub(crate) mod fakes;

#[cfg(test)]
#[path = "durability_tests.rs"]
mod durability_tests;
