//! Matrix results as one CSV: `#` header lines carry the facts that make the
//! numbers interpretable, and one row is one (concurrency, query class) cell.
//!
//! Four charts come out of these rows, and all four are the same shape: X is
//! `concurrency`, Y is one of `p50_ms`, `p90_ms`, `p99_ms`, `queries_per_s`,
//! and a series is an engine and the interface it was reached through.
//! Rendering them is not this crate's job — it writes the columns a renderer
//! can be written against later, and nothing else.
//!
//! **One schema, every interface.** The same rule the sibling tree settled on:
//! every row has every column, and a column a run cannot fill is blank rather
//! than zero, because a zero p99 plots as the best point on the curve while a
//! blank plots as nothing.
//!
//! **A row is only comparable to a row taken the same way.** `limit` is the
//! top-N asked for and `fetch_documents` says whether the engines returned
//! document text or only identities; the second in particular is not a detail,
//! because OpenSearch reads stored fields out of the segment it just searched
//! while ScyllaDB takes the hit list back to the coordinator and reads columns
//! from SSTables. Both are columns so that a chart cannot mix the two without
//! showing it.
use std::fs::File;
use std::io::{self, BufWriter, Write};

use build_rate_core::report::{latency_text, percentile};

use crate::cell::Tally;
use crate::queries::QueryClass;

pub const CSV_COLUMNS: [&str; 17] = [
    "concurrency",
    "query_class",
    "queries",
    "errors",
    "wall_s",
    "queries_per_s",
    "p50_ms",
    "p90_ms",
    "p99_ms",
    "max_ms",
    "hits_mean",
    "zero_hit_queries",
    "distinct_queries",
    "limit",
    "fetch_documents",
    "engine",
    "interface",
];
pub const STDOUT: &str = "-";

/// Which engine answered. The same two words the sibling tree writes, so a
/// consumer that already knows how to tell the halves apart in a build-rate CSV
/// does not have to learn a second vocabulary.
pub const SCYLLADB: &str = "scylladb";
pub const OPENSEARCH: &str = "opensearch";

/// What is true of every cell in one run, carried together so a row cannot be
/// written without the facts that say what it is comparable with.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Shape {
    pub engine: &'static str,
    pub interface: &'static str,
    pub limit: usize,
    pub fetch_documents: bool,
}

#[derive(Debug, Clone, PartialEq)]
pub struct CellResult {
    pub engine: &'static str,
    pub interface: &'static str,
    pub concurrency: usize,
    pub query_class: String,
    pub queries: u64,
    pub errors: u64,
    pub wall_s: f64,
    pub queries_per_s: f64,
    /// `None`, never 0.0, when nothing answered: a cell where every request
    /// failed would otherwise plot as the fastest point on the curve.
    pub p50_ms: Option<f64>,
    pub p90_ms: Option<f64>,
    pub p99_ms: Option<f64>,
    pub max_ms: Option<f64>,
    pub hits_mean: Option<f64>,
    pub zero_hit_queries: u64,
    pub distinct_queries: usize,
    pub limit: usize,
    pub fetch_documents: bool,
}

impl CellResult {
    pub fn new(
        shape: &Shape,
        concurrency: usize,
        class: &QueryClass,
        tally: &Tally,
        sorted_latencies: &[f64],
    ) -> Self {
        Self {
            engine: shape.engine,
            interface: shape.interface,
            concurrency,
            query_class: class.name().to_string(),
            queries: tally.queries,
            errors: tally.errors,
            wall_s: tally.wall_s,
            queries_per_s: per_second(tally.queries, tally.wall_s),
            p50_ms: percentile(sorted_latencies, 0.50),
            p90_ms: percentile(sorted_latencies, 0.90),
            p99_ms: percentile(sorted_latencies, 0.99),
            max_ms: sorted_latencies.last().copied(),
            hits_mean: mean(tally.hits, tally.queries),
            zero_hit_queries: tally.zero_hit_queries,
            distinct_queries: class.distinct(),
            limit: shape.limit,
            fetch_documents: shape.fetch_documents,
        }
    }

    /// Every query in the cell came back empty, which means the class matched
    /// nothing in this index: the latencies beside it are the cost of finding
    /// nothing, not the cost of a search.
    pub fn found_nothing(&self) -> bool {
        self.queries > 0 && self.zero_hit_queries == self.queries
    }
}

/// Zero rather than infinity when no time passed, the convention
/// `build_rate_core::samples::rate` already set for the sibling's rates.
pub fn per_second(count: u64, seconds: f64) -> f64 {
    if seconds > 0.0 {
        count as f64 / seconds
    } else {
        0.0
    }
}

fn mean(total: u64, count: u64) -> Option<f64> {
    if count == 0 {
        return None;
    }
    Some(total as f64 / count as f64)
}

/// Opened before the first query runs, so an unwritable `--out` costs a second
/// rather than a whole matrix.
///
/// Its own sink rather than the sibling's: the two schemas describe different
/// measurements and have to be able to grow apart, and what they would share is
/// a dozen lines of "a file, or stdout when the name is a dash".
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

    /// Flushed per cell: a matrix that dies on cell 14 still leaves 1-13 behind.
    pub fn append_row(&mut self, result: &CellResult) -> io::Result<()> {
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

fn csv_row(result: &CellResult) -> String {
    format!(
        "{},{},{},{},{:.3},{:.1},{},{},{},{},{},{},{},{},{},{},{}",
        result.concurrency,
        result.query_class,
        result.queries,
        result.errors,
        result.wall_s,
        result.queries_per_s,
        csv_number(result.p50_ms, 3),
        csv_number(result.p90_ms, 3),
        csv_number(result.p99_ms, 3),
        csv_number(result.max_ms, 3),
        csv_number(result.hits_mean, 2),
        result.zero_hit_queries,
        result.distinct_queries,
        result.limit,
        result.fetch_documents,
        result.engine,
        result.interface
    )
}

fn csv_number(value: Option<f64>, decimals: usize) -> String {
    value.map_or_else(String::new, |number| format!("{number:.decimals$}"))
}

pub fn summary_table(results: &[CellResult]) -> String {
    let header = format!(
        "{:>6} {:>12} {:>9} {:>6} {:>8} {:>10} {:>9} {:>9} {:>9} {:>8}",
        "conc", "class", "queries", "err", "wall_s", "q/s", "p50_ms", "p90_ms", "p99_ms", "hits"
    );
    std::iter::once(header)
        .chain(results.iter().map(summary_row))
        .collect::<Vec<_>>()
        .join("\n")
}

fn summary_row(result: &CellResult) -> String {
    format!(
        "{:>6} {:>12} {:>9} {:>6} {:>8.2} {:>10.1} {:>9} {:>9} {:>9} {:>8}",
        result.concurrency,
        result.query_class,
        result.queries,
        result.errors,
        result.wall_s,
        result.queries_per_s,
        latency_text(result.p50_ms),
        latency_text(result.p90_ms),
        latency_text(result.p99_ms),
        hits_text(result)
    )
}

/// A cell that found nothing is marked here as well as in its own column, for
/// the same reason the sibling marks a build that never settled: the number
/// beside it is not the number it looks like.
fn hits_text(result: &CellResult) -> String {
    match result.hits_mean {
        None => "-".to_string(),
        Some(hits) if result.found_nothing() => format!("{hits:.1}!"),
        Some(hits) => format!("{hits:.1}"),
    }
}

#[cfg(test)]
#[path = "report_tests.rs"]
mod tests;
