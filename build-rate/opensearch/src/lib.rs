//! Concurrency sweep for OpenSearch ingest: docs/s and p99 per concurrency level.
pub mod build_rate;
pub mod bulk;
pub mod cli;
pub mod client;
pub mod corpus;
pub mod insert;
pub mod notes;
pub mod report;
pub mod samples;
pub mod reset;
pub mod sweep;
pub mod vstore;

#[cfg(test)]
pub(crate) mod fakes;

