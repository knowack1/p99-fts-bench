//! Sweep results as one CSV. Shared: see `build_rate_core::report`.
//!
//! What stays here is the OpenSearch half of the header — the cluster facts
//! that make the numbers interpretable — and the engine's own name for its
//! rows.
pub use build_rate_core::report::{
    latency_text, percentile, summary_table, CsvSink, IndexBuild, PointResult, CSV_COLUMNS,
    INDEX_COLUMNS, STDOUT,
};
use build_rate_core::report::{header_lines as core_header_lines, OPENSEARCH};

use crate::client::Cluster;

/// A `_bulk` carries `batch_size` documents, so a latency here is per request
/// and not per document. The two are the same measurement only at
/// `--batch-size 1`.
pub const LATENCY_UNIT: &str = "bulk_request";
pub const ENGINE: &str = OPENSEARCH;

pub fn header_lines(cluster: &Cluster, settings: &[(String, String)]) -> Vec<String> {
    core_header_lines(&cluster.facts(), settings)
}

#[cfg(test)]
#[path = "report_tests.rs"]
mod tests;
