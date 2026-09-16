//! scyllarate --corpus ../../data/corpus.jsonl --concurrency 4,8,16,32,64,128
//!
//! Measures how fast this client can submit prepared INSERTs to ScyllaDB, per
//! concurrency level. That is a submit rate, not an FTS index build rate: a
//! completed CQL write says nothing about how many documents reached the index.
use std::process::ExitCode;
use std::sync::Arc;

use anyhow::{anyhow, Context, Result};
use clap::Parser;

use scyllarate::build_rate::IndexWatch;
use scyllarate::cli::Args;
use scyllarate::corpus::{self, CorpusSource};
use scyllarate::notes::{note, Notes};
use scyllarate::report::{self, CsvSink, PointResult};
use scyllarate::reset::ResettingInserters;
use scyllarate::run::{
    build_runtime, echo_summary, exit_code, report_outcome, say_each, watch_for_interrupt,
};
use scyllarate::samples::SampleFiles;
use scyllarate::session::{self, Topology};
use scyllarate::sweep::{self, Rung, Watchers};
use scyllarate::vstore::{IndexProbe, VectorStoreProbe};

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
    let rungs = args.rungs().map_err(|why| anyhow!(why))?;
    let workers = args.tokio_workers();
    build_runtime(workers)?.block_on(measure(args, rungs, workers))
}

async fn measure(args: Args, rungs: Vec<Rung>, workers: usize) -> Result<ExitCode> {
    let session = Arc::new(session::connect(&args.connect_options()).await?);
    let topology = session::read_topology(&session, &args.keyspace, workers).await?;
    describe(&topology);

    let probe = open_probe(&args)?;
    announce_reset(&args);
    let settings = settings_with_index(&args, probe.as_deref()).await;
    let mut sink = CsvSink::open(&args.out)?;
    sink.write_preamble(&report::header_lines(&topology, &settings))?;
    let samples = open_samples(&args, &topology, &settings)?;
    let (results, aborted) =
        sweep_levels(&args, &rungs, session, probe, &mut sink, samples.as_ref()).await;

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
    Ok(Some(
        files.with_preamble(report::header_lines(topology, settings)),
    ))
}

fn open_probe(args: &Args) -> Result<Option<Arc<VectorStoreProbe>>> {
    if !args.watches_index() {
        return Ok(None);
    }
    Ok(Some(Arc::new(VectorStoreProbe::new(
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

async fn settings_with_index(
    args: &Args,
    probe: Option<&VectorStoreProbe>,
) -> Vec<(String, String)> {
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
    rungs: &[Rung],
    session: Arc<scylla::client::session::Session>,
    probe: Option<Arc<VectorStoreProbe>>,
    sink: &mut CsvSink,
    samples: Option<&SampleFiles>,
) -> (Vec<PointResult>, bool) {
    let source = CorpusSource::new(&args.corpus, args.max_docs);
    let notes = Notes::stderr();
    let cancel = watch_for_interrupt();
    let index = index_watch(args, as_probe(probe.clone()));
    let inserters = ResettingInserters::new(
        session,
        args.reset_plan(),
        as_probe(probe),
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
            || corpus::rows(&source),
            rungs,
            sweep::loader(),
            &watchers,
            &cancel,
            &mut collect,
        )
        .await
    };
    (results, report_outcome(outcome, sink.destination()))
}

/// The vector-store is what `IndexProbe` means on this half; the watch and the
/// reset gates take it as the trait so neither has to know that.
fn as_probe(probe: Option<Arc<VectorStoreProbe>>) -> Option<Arc<dyn IndexProbe>> {
    probe.map(|probe| probe as Arc<dyn IndexProbe>)
}

fn index_watch(args: &Args, probe: Option<Arc<dyn IndexProbe>>) -> IndexWatch {
    match probe {
        Some(probe) => IndexWatch::on(probe, args.watch_timing()),
        None => IndexWatch::off(),
    }
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

#[cfg(test)]
#[path = "main_tests.rs"]
mod tests;
