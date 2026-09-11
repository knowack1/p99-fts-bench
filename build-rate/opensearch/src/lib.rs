//! Concurrency sweep for OpenSearch ingest: docs/s and p99 per concurrency level.
pub mod bulk;
pub mod cli;
pub mod client;
pub mod corpus;
pub mod insert;
pub mod notes;
pub mod report;
pub mod reset;
pub mod sweep;

#[cfg(test)]
pub(crate) mod fakes;

#[cfg(test)]
#[path = "durability_tests.rs"]
mod durability_tests;
