//! Sweep results as one CSV: `#` header lines carry the facts that make the
//! numbers interpretable, the rows feed both charts.
//!
//! Chart 1 plots `concurrency` against `docs_per_s`; chart 2 plots `concurrency`
//! against `p99_ms`. Both matplotlib's `loadtxt` and pandas' `read_csv` skip the
//! `#` lines by default.
//!
//! **`p50_ms` and `p99_ms` are per `_bulk` request, not per document.** A bulk
//! carries `batch_size` documents, so the two are the same measurement only at
//! `--batch-size 1`. The header says so as `latency_unit`, and the batch size
//! is a column so a chart cannot mix two of them without showing it.
use std::fs::File;
use std::io::{self, BufWriter, Write};

use crate::client::Cluster;

/// The first seven columns and their order are `scyllarate`'s, so a chart
/// script written for one engine reads the other by name or by index. The
/// bulk-specific columns are appended rather than interleaved.
pub const CSV_COLUMNS: [&str; 10] = [
    "concurrency",
    "docs",
    "errors",
    "wall_s",
    "docs_per_s",
    "p50_ms",
    "p99_ms",
    "batch_size",
    "bulks",
    "failed_bulks",
];
pub const STDOUT: &str = "-";
pub const LATENCY_UNIT: &str = "bulk_request";

#[derive(Debug, Clone, PartialEq)]
pub struct PointResult {
    pub concurrency: usize,
    pub batch_size: usize,
    pub docs: u64,
    pub errors: u64,
    pub bulks: u64,
    pub failed_bulks: u64,
    pub wall_s: f64,
    pub docs_per_s: f64,
    pub p50_ms: Option<f64>,
    pub p99_ms: Option<f64>,
}

impl PointResult {
    pub fn docs_in_flight(&self) -> usize {
        self.concurrency * self.batch_size
    }
}

/// `None`, never 0.0, when no bulk came back clean. A point where every bulk
/// failed would otherwise plot as the best latency on the curve.
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

pub fn header_lines(cluster: &Cluster, settings: &[(String, String)]) -> Vec<String> {
    cluster
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
        cluster: &Cluster,
        settings: &[(String, String)],
    ) -> io::Result<()> {
        for line in header_lines(cluster, settings) {
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
        "{},{},{},{:.3},{:.1},{},{},{},{},{}",
        result.concurrency,
        result.docs,
        result.errors,
        result.wall_s,
        result.docs_per_s,
        csv_latency(result.p50_ms),
        csv_latency(result.p99_ms),
        result.batch_size,
        result.bulks,
        result.failed_bulks
    )
}

fn csv_latency(value: Option<f64>) -> String {
    value.map_or_else(String::new, |ms| format!("{ms:.3}"))
}

pub fn summary_table(results: &[PointResult]) -> String {
    let header = format!(
        "{:>6} {:>6} {:>9} {:>6} {:>9} {:>10} {:>9} {:>9} {:>8}",
        "conc", "batch", "docs", "err", "wall_s", "docs/s", "p50_ms", "p99_ms", "bulks"
    );
    std::iter::once(header)
        .chain(results.iter().map(summary_row))
        .collect::<Vec<_>>()
        .join("\n")
}

fn summary_row(result: &PointResult) -> String {
    format!(
        "{:>6} {:>6} {:>9} {:>6} {:>9.2} {:>10.1} {:>9} {:>9} {:>8}",
        result.concurrency,
        result.batch_size,
        result.docs,
        result.errors,
        result.wall_s,
        result.docs_per_s,
        latency_text(result.p50_ms),
        latency_text(result.p99_ms),
        result.bulks
    )
}

/// A dash where no bulk came back clean, so an unmeasured point cannot read as
/// a fast one on stderr any more than it can in the CSV.
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
