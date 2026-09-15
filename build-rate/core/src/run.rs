//! Starting a run and ending one: the runtime, the interrupt, the exit code,
//! and how an abort says where the levels it did measure went.
//!
//! None of it depends on which engine is being measured, and all of it was
//! written twice.
use std::process::ExitCode;

use anyhow::{Context, Result};
use tokio::runtime::Runtime;

use crate::notes::note;
use crate::report::{summary_table, PointResult};
use crate::sweep::Cancel;

/// Tokio's own ceiling is 512, which an endpoint given as a hostname reaches:
/// the HTTP client resolves through a blocking `getaddrinfo`, one pool thread
/// per in-flight lookup, so a concurrency ladder against `http://localhost:9200`
/// has been measured at 408 threads for 8 workers' worth of CPU.
///
/// The floor is 2, and it is not a matter of taste. Two things want this pool:
/// the corpus producer, which `sweep::measure_at_concurrency` hands to
/// `spawn_blocking` for a whole level and which parks inside it on a full
/// channel, and DNS. At 1 the producer takes the only slot, every lookup queues
/// behind it, the channel never drains and the level never ends. 16 leaves the
/// producer one slot and resolution fifteen. It is not tied to `--tokio-workers`
/// because none of this work is CPU-bound.
const MAX_BLOCKING_THREADS: usize = 16;

/// The knob this tool exists to expose: how many cores tokio may use to serve
/// the in-flight requests. It is orthogonal to `--concurrency`, which says how
/// many requests are outstanding at once.
pub fn build_runtime(workers: usize) -> Result<Runtime> {
    tokio::runtime::Builder::new_multi_thread()
        .worker_threads(workers)
        .max_blocking_threads(MAX_BLOCKING_THREADS)
        .enable_all()
        .build()
        .with_context(|| format!("cannot start a tokio runtime with {workers} workers"))
}

/// Ctrl-C is the ordinary way a long ladder ends early, and the levels already
/// measured are worth as much then as after an engine error.
pub fn watch_for_interrupt() -> Cancel {
    let cancel = Cancel::default();
    let trigger = cancel.clone();
    tokio::spawn(async move {
        if tokio::signal::ctrl_c().await.is_ok() {
            trigger.trigger();
        }
    });
    cancel
}

pub fn report_outcome(outcome: Result<()>, destination: &str) -> bool {
    match outcome {
        Ok(()) => false,
        Err(exc) => {
            say_each(&abort_lines(&exc, destination));
            true
        }
    }
}

/// An abort has to say where the levels it did measure ended up, or the
/// operator has to guess whether anything survived.
pub fn abort_lines(exc: &anyhow::Error, destination: &str) -> Vec<String> {
    vec![
        format!("!! sweep aborted: {exc:#}"),
        format!("!! the levels measured before it are in {destination}"),
    ]
}

pub fn say_each(lines: &[String]) {
    for line in lines {
        note(line);
    }
}

pub fn echo_summary(results: &[PointResult]) {
    say_each(&["".to_string(), summary_table(results)]);
}

/// A run that measured nothing, or measured something with failed requests in
/// it, is not a run whose numbers should be picked up by a script.
pub fn exit_code(results: &[PointResult], aborted: bool) -> ExitCode {
    if aborted || results.iter().any(|result| result.errors > 0) {
        return ExitCode::FAILURE;
    }
    ExitCode::SUCCESS
}

#[cfg(test)]
#[path = "run_tests.rs"]
mod tests;
