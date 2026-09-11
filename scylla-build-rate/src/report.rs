//! Sweep results as one CSV: `#` header lines carry the facts that make the
//! numbers interpretable, the rows feed both charts.
//!
//! Chart 1 plots `concurrency` against `docs_per_s`; chart 2 plots `concurrency`
//! against `p99_ms`. Both matplotlib's `loadtxt` and pandas' `read_csv` skip the
//! `#` lines by default.
use std::fs::File;
use std::io::{self, BufWriter, Write};

use crate::build_rate::IndexBuild;
use crate::session::Topology;

/// The six index columns are APPENDED, never inserted. `osrate` promises that
/// its first seven columns are these in this order
/// (`opensearch-build-rate/README.md`), and `tools/plot_harness_grid.py` reads
/// both files by position.
pub const CSV_COLUMNS: [&str; 13] = [
    "concurrency",
    "docs",
    "errors",
    "wall_s",
    "docs_per_s",
    "p50_ms",
    "p99_ms",
    "index_docs",
    "index_docs_per_s",
    "index_lag_docs",
    "index_settle_s",
    "index_settled",
    "index_status",
];
pub const INDEX_COLUMNS: usize = 6;
pub const STDOUT: &str = "-";

#[derive(Debug, Clone, PartialEq)]
pub struct PointResult {
    pub concurrency: usize,
    pub docs: u64,
    pub errors: u64,
    pub wall_s: f64,
    pub docs_per_s: f64,
    pub p50_ms: Option<f64>,
    pub p99_ms: Option<f64>,
    /// `None` when the vector-store was not watched. Blank cells, never zeros:
    /// a zero build rate is a finding, and an unwatched level is not one.
    pub index: Option<IndexBuild>,
}

/// `None`, never 0.0, when nothing succeeded. A point where every insert failed
/// would otherwise plot as the best latency on the curve.
pub fn percentile(sorted_values: &[f64], fraction: f64) -> Option<f64> {
    if sorted_values.is_empty() {
        return None;
    }
    let rank = (fraction * sorted_values.len() as f64).ceil() as usize;
    Some(sorted_values[clamp(rank.saturating_sub(1), sorted_values.len())])
}

fn clamp(index: usize, length: usize) -> usize {
    index.min(length - 1)
}

pub fn header_lines(topology: &Topology, settings: &[(String, String)]) -> Vec<String> {
    topology
        .facts()
        .iter()
        .chain(settings.iter())
        .map(|(key, value)| format!("# {key}={value}"))
        .collect()
}

/// Opened before the first point runs, so an unwritable `--out` costs a second
/// rather than a whole sweep.
pub struct CsvSink {
    destination: String,
    handle: Box<dyn Write + Send>,
}

impl CsvSink {
    pub fn open(destination: &str) -> io::Result<Self> {
        Ok(Self {
            destination: destination.to_string(),
            handle: open_writer(destination)?,
        })
    }

    pub fn destination(&self) -> &str {
        &self.destination
    }

    pub fn write_preamble(
        &mut self,
        topology: &Topology,
        settings: &[(String, String)],
    ) -> io::Result<()> {
        for line in header_lines(topology, settings) {
            self.write_line(&line)?;
        }
        self.write_line(&CSV_COLUMNS.join(","))
    }

    /// Flushed per point: a sweep that dies at level 5 still leaves 1-4 behind.
    pub fn append_row(&mut self, result: &PointResult) -> io::Result<()> {
        self.write_line(&csv_row(result))
    }

    fn write_line(&mut self, text: &str) -> io::Result<()> {
        writeln!(self.handle, "{text}")?;
        self.handle.flush()
    }
}

fn open_writer(destination: &str) -> io::Result<Box<dyn Write + Send>> {
    if destination == STDOUT {
        return Ok(Box::new(io::stdout()));
    }
    Ok(Box::new(BufWriter::new(File::create(destination)?)))
}

fn csv_row(result: &PointResult) -> String {
    format!(
        "{},{},{},{:.3},{:.1},{},{},{}",
        result.concurrency,
        result.docs,
        result.errors,
        result.wall_s,
        result.docs_per_s,
        csv_latency(result.p50_ms),
        csv_latency(result.p99_ms),
        csv_index(result.index.as_ref())
    )
}

fn csv_index(build: Option<&IndexBuild>) -> String {
    let Some(build) = build else {
        return [""; INDEX_COLUMNS].join(",");
    };
    format!(
        "{},{:.1},{},{:.3},{},{}",
        build.docs, build.docs_per_s, build.lag_docs, build.settle_s, build.settled, build.status
    )
}

fn csv_latency(value: Option<f64>) -> String {
    value.map_or_else(String::new, |ms| format!("{ms:.3}"))
}

pub fn summary_table(results: &[PointResult]) -> String {
    let header = format!(
        "{:>6} {:>9} {:>6} {:>9} {:>10} {:>9} {:>9} {:>11} {:>11}",
        "conc", "docs", "err", "wall_s", "docs/s", "p50_ms", "p99_ms", "idx_docs", "idx_docs/s"
    );
    std::iter::once(header)
        .chain(results.iter().map(summary_row))
        .collect::<Vec<_>>()
        .join("\n")
}

fn summary_row(result: &PointResult) -> String {
    format!(
        "{:>6} {:>9} {:>6} {:>9.2} {:>10.1} {:>9} {:>9} {:>11} {:>11}",
        result.concurrency,
        result.docs,
        result.errors,
        result.wall_s,
        result.docs_per_s,
        latency_text(result.p50_ms),
        latency_text(result.p99_ms),
        index_docs_text(result.index.as_ref()),
        index_rate_text(result.index.as_ref())
    )
}

/// A level whose index never caught up is marked, because the rate beside it is
/// a floor. A dash is an unwatched level, the same convention as the latencies.
fn index_docs_text(build: Option<&IndexBuild>) -> String {
    build.map_or_else(
        || "-".to_string(),
        |build| {
            let mark = if build.settled { "" } else { "*" };
            format!("{}{mark}", build.docs)
        },
    )
}

fn index_rate_text(build: Option<&IndexBuild>) -> String {
    build.map_or_else(
        || "-".to_string(),
        |build| format!("{:.1}", build.docs_per_s),
    )
}

/// A dash where nothing succeeded, so an unmeasured point cannot read as a fast
/// one on stderr any more than it can in the CSV.
pub fn latency_text(value: Option<f64>) -> String {
    value.map_or_else(|| "-".to_string(), |ms| format!("{ms:.2}"))
}

pub fn note(message: &str) {
    let mut stderr = io::stderr();
    let _ = writeln!(stderr, "{message}");
    let _ = stderr.flush();
}

#[cfg(test)]
#[path = "report_tests.rs"]
mod tests;
