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

use scyllarate::build_rate::IndexWatch;
use scyllarate::cli::Args;
use scyllarate::corpus::CorpusSource;
use scyllarate::notes::Notes;
use scyllarate::report::{note, summary_table, CsvSink, PointResult};
use scyllarate::reset::ResettingInserters;
use scyllarate::samples::SampleFiles;
use scyllarate::session::{self, Topology};
use scyllarate::sweep::{self, Cancel, Watchers};
use scyllarate::vstore::IndexProbe;

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
    let session = Arc::new(session::connect(&args.connect_options()).await?);
    let topology = session::read_topology(&session, &args.keyspace, workers).await?;
    describe(&topology);

    let probe = open_probe(&args)?;
    announce_reset(&args);
    let settings = settings_with_index(&args, probe.as_deref()).await;
    let mut sink = CsvSink::open(&args.out)?;
    sink.write_preamble(&topology, &settings)?;
    let samples = open_samples(&args, &topology, &settings)?;
    let (results, aborted) = sweep_levels(&args, session, probe, &mut sink, samples.as_ref()).await;

    echo_summary(&results);
    Ok(exit_code(&results, aborted))
}

/// Opened before the first insert, so an unwritable directory costs a second
/// rather than a whole ladder — the rule `CsvSink::open` already follows.
fn open_samples(
    args: &Args,
    topology: &Topology,
    settings: &[(String, String)],
) -> Result<Option<SampleFiles>> {
    let Some(dir) = args.samples_dir.as_ref() else {
        return Ok(None);
    };
    let files = SampleFiles::new(dir)
        .with_context(|| format!("cannot write per-second samples to {}", dir.display()))?;
    note(&format!(
        "per-second samples: {}/c<level>-<n>.csv",
        dir.display()
    ));
    Ok(Some(files.with_preamble(topology, settings)))
}

fn open_probe(args: &Args) -> Result<Option<Arc<IndexProbe>>> {
    if !args.watches_index() {
        return Ok(None);
    }
    Ok(Some(Arc::new(IndexProbe::new(
        &args.vs_url,
        &args.keyspace,
        &args.vs_index,
        std::time::Duration::from_secs_f64(args.request_timeout),
    )?)))
}

/// A run that drops a keyspace says so before it does it, naming the keyspace
/// and the endpoint. The flag defaults to on, so the one line of warning is the
/// only thing standing between a mistyped `--hosts` and someone's data.
fn announce_reset(args: &Args) {
    if !args.resets() {
        note(&format!(
            "index reset OFF: levels after the first rewrite the same rows, so \
             only the first measures a build ({})",
            if args.watches_index() {
                "--no-reset"
            } else {
                "--no-index-watch"
            }
        ));
        return;
    }
    note(&format!(
        "index reset ON: DROPPING KEYSPACE {} before every level, gated on {}",
        args.keyspace, args.vs_url
    ));
}

async fn settings_with_index(args: &Args, probe: Option<&IndexProbe>) -> Vec<(String, String)> {
    let version = match probe {
        Some(probe) => probe.version().await,
        None => "off".to_string(),
    };
    let mut settings = args.settings();
    settings.push(("vector_store".to_string(), version));
    settings
}

async fn sweep_levels(
    args: &Args,
    session: Arc<scylla::client::session::Session>,
    probe: Option<Arc<IndexProbe>>,
    sink: &mut CsvSink,
    samples: Option<&SampleFiles>,
) -> (Vec<PointResult>, bool) {
    let source = CorpusSource::new(&args.corpus, args.max_docs);
    let notes = Notes::stderr();
    let cancel = watch_for_interrupt();
    let index = index_watch(args, probe.clone());
    let inserters = ResettingInserters::new(
        session,
        args.reset_plan(),
        probe,
        args.gate_timing(),
        notes.clone(),
        args.resets(),
    );
    let mut results: Vec<PointResult> = Vec::new();

    let outcome = {
        let mut collect = |result: PointResult| -> Result<()> {
            sink.append_row(&result)?;
            results.push(result);
            Ok(())
        };
        let watchers = Watchers {
            index: &index,
            notes: &notes,
            samples,
        };
        sweep::run_sweep(
            &inserters,
            || source.open(),
            &args.concurrency.0,
            &watchers,
            &cancel,
            &mut collect,
        )
        .await
    };
    (results, report_outcome(outcome, sink.destination()))
}

fn index_watch(args: &Args, probe: Option<Arc<IndexProbe>>) -> IndexWatch {
    match probe {
        Some(probe) => IndexWatch::on(probe, args.watch_timing()),
        None => IndexWatch::off(),
    }
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
