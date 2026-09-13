//! Sweep results as one CSV: `#` header lines carry the facts that make the
//! numbers interpretable, the rows feed both charts.
//!
//! Chart 1 plots `concurrency` against `docs_per_s`; chart 2 plots `concurrency`
//! against `p99_ms`. Both matplotlib's `loadtxt` and pandas' `read_csv` skip the
//! `#` lines by default.
//!
//! **One schema, both engines.** The two harnesses used to write seven shared
//! columns and then diverge, which made an `awk` field index written for one
//! half wrong on the other. Now every row has every column, and a column an
//! engine cannot fill is blank rather than zero — the distinction the latency
//! columns have always made, because a zero plots as the best point on the
//! curve while a blank plots as nothing.
//!
//! **`p50_ms` and `p99_ms` are per request, and a request is not always a
//! document.** One `scyllarate` request carries one document; one `osrate`
//! request carries `batch_size` of them. `batch_size` is a column and
//! `latency_unit` is a header fact so that a chart cannot mix the two without
//! showing it, but a reader still can.
use std::fs::File;
use std::io::{self, BufWriter, Write};

pub const CSV_COLUMNS: [&str; 17] = [
    "concurrency",
    "docs",
    "errors",
    "wall_s",
    "docs_per_s",
    "p50_ms",
    "p99_ms",
    "batch_size",
    "requests",
    "failed_requests",
    "index_docs",
    "index_docs_per_s",
    "index_lag_docs",
    "index_settle_s",
    "index_settled",
    "index_status",
    "engine",
];
pub const INDEX_COLUMNS: usize = 6;
pub const STDOUT: &str = "-";

/// Which engine produced the row.
///
/// The only discriminator that survives what consumers actually do to these
/// files: `tools/plot_harness_grid.py` concatenates rows across globs, and
/// before this column it told the halves apart by whether a `batch_size` column
/// existed — a test that now passes for both and would silently relabel every
/// ScyllaDB point.
pub const SCYLLADB: &str = "scylladb";
pub const OPENSEARCH: &str = "opensearch";

/// What the index did during one level.
#[derive(Debug, Clone, PartialEq)]
pub struct IndexBuild {
    pub docs: u64,
    pub docs_per_s: f64,
    pub lag_docs: u64,
    pub settle_s: f64,
    pub settled: bool,
    pub status: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct PointResult {
    pub engine: &'static str,
    pub concurrency: usize,
    /// Documents per request: 1 wherever a request carries one document, which
    /// is every `scyllarate` row. `docs / requests` is the effective batch size
    /// and a row where the two disagree is a bug in the loader.
    pub batch_size: usize,
    pub docs: u64,
    pub errors: u64,
    pub requests: u64,
    pub failed_requests: u64,
    pub wall_s: f64,
    pub docs_per_s: f64,
    pub p50_ms: Option<f64>,
    pub p99_ms: Option<f64>,
    /// `None` when the index was not watched. Blank cells, never zeros: a zero
    /// build rate is a finding, and an unwatched level is not one.
    pub index: Option<IndexBuild>,
}

impl PointResult {
    pub fn docs_in_flight(&self) -> usize {
        self.concurrency * self.batch_size
    }
}

/// `None`, never 0.0, when nothing succeeded — no insert landed, or no
/// request came back clean. A point where everything failed would otherwise
/// plot as the best latency on the curve.
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

/// A dash where nothing was measured — no insert succeeded, or no request
/// came back clean — so an unmeasured point cannot read as a fast one on
/// stderr any more than it can in the CSV.
pub fn latency_text(value: Option<f64>) -> String {
    value.map_or_else(|| "-".to_string(), |ms| format!("{ms:.2}"))
}

/// The engine's facts first, then the run's settings, both as `# key=value`.
/// Taken as pairs rather than as a topology or a cluster, so this does not have
/// to know which engine was measured.
pub fn header_lines(facts: &[(String, String)], settings: &[(String, String)]) -> Vec<String> {
    facts
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

    pub fn write_preamble(&mut self, lines: &[String]) -> io::Result<()> {
        for line in lines {
            self.write_line(line)?;
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
        "{},{},{},{:.3},{:.1},{},{},{},{},{},{},{}",
        result.concurrency,
        result.docs,
        result.errors,
        result.wall_s,
        result.docs_per_s,
        csv_latency(result.p50_ms),
        csv_latency(result.p99_ms),
        result.batch_size,
        result.requests,
        result.failed_requests,
        csv_index(result.index.as_ref()),
        result.engine
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
        "{:>6} {:>6} {:>9} {:>6} {:>9} {:>10} {:>9} {:>9} {:>9} {:>11} {:>11}",
        "conc",
        "batch",
        "docs",
        "err",
        "wall_s",
        "docs/s",
        "p50_ms",
        "p99_ms",
        "reqs",
        "idx_docs",
        "idx_docs/s"
    );
    std::iter::once(header)
        .chain(results.iter().map(summary_row))
        .collect::<Vec<_>>()
        .join("\n")
}

fn summary_row(result: &PointResult) -> String {
    format!(
        "{:>6} {:>6} {:>9} {:>6} {:>9.2} {:>10.1} {:>9} {:>9} {:>9} {:>11} {:>11}",
        result.concurrency,
        result.batch_size,
        result.docs,
        result.errors,
        result.wall_s,
        result.docs_per_s,
        latency_text(result.p50_ms),
        latency_text(result.p99_ms),
        result.requests,
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

#[cfg(test)]
#[path = "report_tests.rs"]
mod tests;
