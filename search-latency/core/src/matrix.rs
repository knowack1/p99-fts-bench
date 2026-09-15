//! The ladder: every concurrency level against every query class, one cell at a
//! time, one CSV row each.
//!
//! **Concurrency is the outer loop and the class the inner one**, so that a
//! repeated ladder — `--concurrency 8,16,32,8,16,32` — interleaves two
//! traversals of the whole matrix instead of measuring one class early and
//! another an hour later. Host drift then spreads across the matrix rather than
//! landing on one curve and tilting it. Repeats are kept: the list is walked
//! exactly as it was given.
//!
//! Nothing is reset between cells and nothing is rebuilt. That is the point of
//! a read benchmark: the index is resident and finished before the first query,
//! and tearing anything down mid-matrix would measure a rebuild.
use std::sync::Arc;

use anyhow::Result;
use build_rate_core::notes::Notes;
use build_rate_core::report::latency_text;
use build_rate_core::sweep::Cancel;

use crate::cell::{measure_cell, CellSettings, Measured};
use crate::latencies::LatencyFiles;
use crate::queries::QueryClass;
use crate::report::{CellResult, Shape};
use crate::search::Searcher;

pub type OnCell<'a> = &'a mut dyn FnMut(CellResult) -> Result<()>;

/// What the matrix is walked with. A bundle rather than six more parameters,
/// which is the count clippy refuses to pass.
pub struct Runner<'a> {
    pub searcher: &'a Arc<dyn Searcher>,
    pub shape: Shape,
    pub settings: CellSettings,
    pub notes: &'a Notes,
    pub latencies: Option<&'a LatencyFiles>,
}

/// The two dimensions, carried together so neither can be walked without the
/// other.
pub struct Plan {
    pub levels: Vec<usize>,
    pub classes: Vec<QueryClass>,
}

impl Plan {
    pub fn cells(&self) -> usize {
        self.levels.len() * self.classes.len()
    }
}

/// `on_cell` is handed each result as it lands, so a cell that fails cannot
/// take the cells already measured down with it.
pub async fn run_matrix(
    runner: &Runner<'_>,
    plan: &Plan,
    cancel: &Cancel,
    on_cell: OnCell<'_>,
) -> Result<()> {
    let total = plan.cells();
    for (position, cell) in cells_of(plan).enumerate() {
        runner.notes.say(&announce(position, total, &cell));
        let result = run_cell(runner, &cell, cancel).await?;
        say_outcome(runner.notes, &result);
        on_cell(result)?;
    }
    Ok(())
}

/// One cell of the matrix, before it has been measured.
struct Cell {
    concurrency: usize,
    class: QueryClass,
}

fn cells_of(plan: &Plan) -> impl Iterator<Item = Cell> + '_ {
    plan.levels.iter().flat_map(move |&concurrency| {
        plan.classes.iter().map(move |class| Cell {
            concurrency,
            class: class.clone(),
        })
    })
}

async fn run_cell(runner: &Runner<'_>, cell: &Cell, cancel: &Cancel) -> Result<CellResult> {
    let measured = measure_cell(
        runner.searcher,
        &cell.class,
        cell.concurrency,
        &runner.settings,
        cancel,
    )
    .await?;
    warn_about_errors(runner.notes, &measured);
    let (result, latencies) = measured.into_report(&runner.shape, &cell.class, cell.concurrency);
    write_distribution(runner, cell, &latencies)?;
    Ok(result)
}

/// The count reaches the CSV, but only the message says what went wrong, and a
/// matrix left to finish with a silent error column is a matrix nobody can
/// diagnose afterwards.
fn warn_about_errors(notes: &Notes, measured: &Measured) {
    if let Some(first) = measured.counters.first_error() {
        notes.say(&format!(
            "  !! {} failed queries, first was {first}",
            measured.counters.errors
        ));
    }
}

/// A cell that cannot write the distribution the operator asked for is a
/// failure, not a warning: finding out at the end of a matrix costs the matrix.
fn write_distribution(runner: &Runner<'_>, cell: &Cell, latencies: &[f64]) -> Result<()> {
    let Some(files) = runner.latencies else {
        return Ok(());
    };
    files.write_cell(cell.class.name(), cell.concurrency, latencies)?;
    Ok(())
}

fn announce(position: usize, total: usize, cell: &Cell) -> String {
    format!(
        "[{}/{}] concurrency={} class={} ({} distinct queries)",
        position + 1,
        total,
        cell.concurrency,
        cell.class.name(),
        cell.class.distinct()
    )
}

fn say_outcome(notes: &Notes, result: &CellResult) {
    notes.say(&outcome_line(result));
    for warning in warnings(result) {
        notes.say(&warning);
    }
}

fn outcome_line(result: &CellResult) -> String {
    format!(
        "  -> {} queries in {:.2}s = {:.1} q/s, p50 {} / p90 {} / p99 {} ms, {} errors",
        result.queries,
        result.wall_s,
        result.queries_per_s,
        latency_text(result.p50_ms),
        latency_text(result.p90_ms),
        latency_text(result.p99_ms),
        result.errors
    )
}

/// Both of these are ways a cell comes back complete and means something other
/// than it appears to, so both are said on stderr as well as written to their
/// columns.
fn warnings(result: &CellResult) -> Vec<String> {
    let mut warnings = Vec::new();
    if result.found_nothing() {
        warnings.push(format!(
            "  !! every query in {} matched nothing: this cell timed an empty \
             result set, not a search",
            result.query_class
        ));
    } else if result.zero_hit_queries > 0 {
        warnings.push(format!(
            "  !! {} of {} queries in {} matched nothing",
            result.zero_hit_queries, result.queries, result.query_class
        ));
    }
    warnings
}

#[cfg(test)]
#[path = "matrix_tests.rs"]
mod tests;
