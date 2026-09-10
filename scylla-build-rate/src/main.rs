//! scyllarate --corpus ../data/corpus.jsonl --concurrency 4,8,16,32,64,128
//!
//! Measures how fast this client can submit prepared INSERTs to ScyllaDB, per
//! concurrency level. That is a submit rate, not an FTS index build rate: a
//! completed CQL write says nothing about how many documents reached the index.
use std::process::ExitCode;
use std::sync::Arc;

use anyhow::{Context, Result};
use clap::Parser;
use tokio::runtime::Runtime;

use scyllarate::cli::Args;
use scyllarate::corpus::CorpusSource;
use scyllarate::insert::CqlInserter;
use scyllarate::notes::Notes;
use scyllarate::report::{note, summary_table, CsvSink, PointResult};
use scyllarate::session::{self, Topology};
use scyllarate::sweep::{self, Cancel};

fn main() -> ExitCode {
    match run(Args::parse()) {
        Ok(code) => code,
        Err(exc) => {
            note(&format!("!! {exc:#}"));
            ExitCode::FAILURE
        }
    }
}

fn run(args: Args) -> Result<ExitCode> {
    let workers = args.tokio_workers();
    build_runtime(workers)?.block_on(measure(args, workers))
}

/// The knob this tool exists to expose: how many cores tokio may use to serve
/// the in-flight requests. It is orthogonal to `--concurrency`, which says how
/// many requests are outstanding at once.
fn build_runtime(workers: usize) -> Result<Runtime> {
    tokio::runtime::Builder::new_multi_thread()
        .worker_threads(workers)
        .enable_all()
        .build()
        .with_context(|| format!("cannot start a tokio runtime with {workers} workers"))
}

async fn measure(args: Args, workers: usize) -> Result<ExitCode> {
    let session = session::connect(&args.connect_options()).await?;
    let statement = session::prepare_insert(&session, &args.table).await?;
    let topology = session::read_topology(&session, &args.keyspace, workers).await?;
    describe(&topology);

    let mut sink = CsvSink::open(&args.out)?;
    sink.write_preamble(&topology, &args.settings())?;
    let (results, aborted) = sweep_levels(&args, session, statement, &mut sink).await;

    echo_summary(&results);
    Ok(exit_code(&results, aborted))
}

async fn sweep_levels(
    args: &Args,
    session: scylla::client::session::Session,
    statement: scylla::statement::prepared::PreparedStatement,
    sink: &mut CsvSink,
) -> (Vec<PointResult>, bool) {
    let inserter = Arc::new(CqlInserter::new(session, statement));
    let source = CorpusSource::new(&args.corpus, args.max_docs);
    let notes = Notes::stderr();
    let cancel = watch_for_interrupt();
    let mut results: Vec<PointResult> = Vec::new();

    let outcome = {
        let mut collect = |result: PointResult| -> Result<()> {
            sink.append_row(&result)?;
            results.push(result);
            Ok(())
        };
        sweep::run_sweep(
            inserter,
            || source.open(),
            &args.concurrency.0,
            &notes,
            &cancel,
            &mut collect,
        )
        .await
    };
    (results, report_outcome(outcome, sink.destination()))
}

/// Ctrl-C is the ordinary way a long ladder ends early, and the levels already
/// measured are worth as much then as after a driver error.
fn watch_for_interrupt() -> Cancel {
    let cancel = Cancel::default();
    let trigger = cancel.clone();
    tokio::spawn(async move {
        if tokio::signal::ctrl_c().await.is_ok() {
            trigger.trigger();
        }
    });
    cancel
}

fn report_outcome(outcome: Result<()>, destination: &str) -> bool {
    match outcome {
        Ok(()) => false,
        Err(exc) => {
            announce_abort(&exc, destination);
            true
        }
    }
}

fn announce_abort(exc: &anyhow::Error, destination: &str) {
    say_each(&abort_lines(exc, destination));
}

/// An abort has to say where the levels it did measure ended up, or the operator
/// has to guess whether anything survived.
fn abort_lines(exc: &anyhow::Error, destination: &str) -> Vec<String> {
    vec![
        format!("!! sweep aborted: {exc:#}"),
        format!("!! the levels measured before it are in {destination}"),
    ]
}

fn describe(topology: &Topology) {
    say_each(&topology_lines(topology));
}

fn topology_lines(topology: &Topology) -> Vec<String> {
    vec![
        format!(
            "scylla {}, driver {}, protocol {}, {}",
            topology.scylla_version,
            topology.driver_version,
            topology.protocol_version,
            topology.runtime
        ),
        format!(
            "shard_aware={} shards={} connections={} tablets={}",
            topology.shard_aware, topology.shards, topology.connections, topology.tablets
        ),
    ]
}

fn echo_summary(results: &[PointResult]) {
    say_each(&["".to_string(), summary_table(results)]);
}

fn say_each(lines: &[String]) {
    for line in lines {
        note(line);
    }
}

fn exit_code(results: &[PointResult], aborted: bool) -> ExitCode {
    if aborted || results.iter().any(|result| result.errors > 0) {
        return ExitCode::FAILURE;
    }
    ExitCode::SUCCESS
}

#[cfg(test)]
#[path = "main_tests.rs"]
mod tests;
