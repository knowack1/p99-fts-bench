//! Starting a run and ending one: the runtime, the interrupt, the summary, and
//! what the exit code is allowed to mean.
//!
//! The runtime and the interrupt are the sibling tree's, unchanged — the same
//! `--tokio-workers` knob, the same Ctrl-C that ends the ladder and keeps what
//! was measured. What is different is the exit code, because a search harness
//! has a second way of coming back complete and wrong.
use std::process::ExitCode;

pub use build_rate_core::run::{build_runtime, report_outcome, say_each, watch_for_interrupt};

use crate::report::{summary_table, CellResult};

pub fn echo_summary(results: &[CellResult]) {
    say_each(&["".to_string(), summary_table(results)]);
}

/// Non-zero for a failed request, and non-zero for a cell where every query
/// matched nothing.
///
/// The second is the one that is particular to this tree: a class that matches
/// nothing still answers, still has a p99 and still plots, and the number it
/// plots is the cost of finding nothing. A run that produced one has not
/// measured what it was asked to, and no script should pick its numbers up
/// without a human having looked.
pub fn exit_code(results: &[CellResult], aborted: bool) -> ExitCode {
    if aborted || results.iter().any(unusable) {
        return ExitCode::FAILURE;
    }
    ExitCode::SUCCESS
}

fn unusable(result: &CellResult) -> bool {
    result.errors > 0 || result.found_nothing()
}

#[cfg(test)]
#[path = "run_tests.rs"]
mod tests;
