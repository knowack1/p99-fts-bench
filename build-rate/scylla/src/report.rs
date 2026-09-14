//! Sweep results as one CSV. Shared: see `build_rate_core::report`.
//!
//! What stays here is the ScyllaDB half of the header — the topology facts that
//! make the numbers interpretable — and the engine's own name for its rows.
use build_rate_core::report::{header_lines as core_header_lines, SCYLLADB};
pub use build_rate_core::report::{
    latency_text, percentile, summary_table, CsvSink, IndexBuild, PointResult, CSV_COLUMNS,
    INDEX_COLUMNS, STDOUT,
};

use crate::session::Topology;

/// One document per request on this half, always. Not a knob: a CQL `BATCH` is
/// a different write path, and reporting it here would read as parity with
/// OpenSearch's `_bulk`.
pub const BATCH_SIZE: usize = 1;
pub const ENGINE: &str = SCYLLADB;

pub fn header_lines(topology: &Topology, settings: &[(String, String)]) -> Vec<String> {
    core_header_lines(&topology.facts(), settings)
}

#[cfg(test)]
#[path = "report_tests.rs"]
mod tests;
