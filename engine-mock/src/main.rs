//! engine-mock --mode cql --port 9042 --vs-port 6080
//!
//! Answers a loader correctly and discards every document, so that the number
//! the loader reports is the loader's own.
use std::process::ExitCode;
use std::time::Duration;

use anyhow::{anyhow, Result};
use clap::Parser;

use engine_mock::cli::Args;
use engine_mock::provenance::RunStart;
use engine_mock::run::{
    announce, await_stop, build_runtime, note, report_periodically, start, stats_document,
    warn_about_unexpected, write_stats, Mock,
};

/// How long the runtime is given to end its connection tasks. It is a bound on
/// the shutdown, not a wait for the clients: nothing is served any more by the
/// time it starts, and the accounting below must not be held up by a loader
/// that left a socket open.
const SHUTDOWN_GRACE: Duration = Duration::from_millis(250);

fn main() -> ExitCode {
    match serve(Args::parse()) {
        Ok(()) => ExitCode::SUCCESS,
        Err(exc) => {
            note(&format!("!! {exc:#}"));
            ExitCode::FAILURE
        }
    }
}

/// The counters are read after the runtime has stopped, so that no connection
/// is still answering while `docs_accepted` — the number the reconciliation
/// gate rests on — is taken.
///
/// `shutdown_timeout` is a bound, not a join: if the grace elapses it returns
/// and leaves any still-running worker detached. That is exact here only
/// because nothing on a connection path can outlast it — every handler answers
/// in microseconds, and the one deliberate wait, `--delay-ms`, is a cancellable
/// `sleep`. Anything added to that path which can block for longer makes this
/// an undercount, and an undercount here is read as documents the loader sent
/// and the mock never saw.
fn serve(args: Args) -> Result<()> {
    args.refuse_a_port_collision().map_err(|why| anyhow!(why))?;
    let started = RunStart::now();
    let workers = args.tokio_workers();
    let runtime = build_runtime(workers)?;
    let mock = runtime.block_on(accept_until_stopped(&args, workers))?;
    runtime.shutdown_timeout(SHUTDOWN_GRACE);

    let snapshot = mock.work.snapshot();
    note(&snapshot.summary_line());
    warn_about_unexpected(&mock.work);
    match args.stats_out.as_ref() {
        None => Ok(()),
        Some(path) => write_stats(
            path,
            &stats_document(&args, &mock.witness(), &started, snapshot),
        ),
    }
}

async fn accept_until_stopped(args: &Args, workers: usize) -> Result<Mock> {
    let mock = start(args, workers).await?;
    announce(args, workers, &mock);
    let reporter = tokio::spawn(report_periodically(
        std::sync::Arc::clone(&mock.work),
        args.report_interval(),
    ));
    await_stop(args.duration()).await?;
    reporter.abort();
    mock.stop();
    Ok(mock)
}
