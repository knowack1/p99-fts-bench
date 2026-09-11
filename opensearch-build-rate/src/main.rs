//! osrate --corpus ../data/corpus.jsonl --concurrency 24,48,96,192,384
//!
//! Measures how fast this client can submit `_bulk` requests to OpenSearch, per
//! concurrency level. That is a submit rate, not a searchable-index rate: a
//! bulk OpenSearch has acknowledged is in the translog and the in-memory
//! buffer, and is not visible to search until a refresh.
//!
//! **Destructive by default**: the index is deleted and recreated before every
//! level, so that each level builds from zero documents rather than rewriting
//! the last level's.
use std::process::ExitCode;
use std::sync::Arc;

use anyhow::{Context, Result};
use clap::Parser;
use opensearch::OpenSearch;
use tokio::runtime::Runtime;

use osrate::cli::Args;
use osrate::client::{self, Cluster};
use osrate::corpus::CorpusSource;
use osrate::insert::BulkInserter;
use osrate::notes::Notes;
use osrate::report::{note, summary_table, CsvSink, PointResult};
use osrate::reset::IndexReset;
use osrate::sweep::{self, BeforeLevel, Cancel, Ladder, NothingToPrepare, Shape};

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

/// The knob this tool exists to expose: how many cores tokio may use to encode
/// and serve the in-flight bulks. It is orthogonal to `--concurrency`, which
/// says how many bulks are outstanding at once.
fn build_runtime(workers: usize) -> Result<Runtime> {
    tokio::runtime::Builder::new_multi_thread()
        .worker_threads(workers)
        .enable_all()
        .build()
        .with_context(|| format!("cannot start a tokio runtime with {workers} workers"))
}

async fn measure(args: Args, workers: usize) -> Result<ExitCode> {
    let client = client::connect(&args.connect_options()).await?;
    let notes = Notes::stderr();
    announce_reset(&args);
    let reset = open_reset(&args, &client, &notes)?;
    prepare_the_index(&args, &client, reset.as_deref()).await?;

    let cluster = client::read_cluster(&client, &args.index, workers).await?;
    describe(&cluster);

    let mut sink = CsvSink::open(&args.out)?;
    sink.write_preamble(&cluster, &args.settings())?;
    let (results, aborted) = sweep_levels(&args, client, reset.as_deref(), &notes, &mut sink).await;

    echo_summary(&results);
    Ok(exit_code(&results, aborted))
}

fn open_reset(args: &Args, client: &OpenSearch, notes: &Notes) -> Result<Option<Box<IndexReset>>> {
    if !args.resets() {
        return Ok(None);
    }
    Ok(Some(Box::new(IndexReset::new(
        client.clone(),
        &args.index,
        &args.connect_options().url,
        args.index_config()?,
        args.gate_timing(),
        notes.clone(),
    ))))
}

/// The index is built once here and again before level 1. That is deliberate:
/// the header has to describe an index created from the config this run
/// applied — not whatever an earlier run left behind — and the analyzer cannot
/// be checked before there is an index to check it on.
async fn prepare_the_index(
    args: &Args,
    client: &OpenSearch,
    reset: Option<&IndexReset>,
) -> Result<()> {
    let Some(reset) = reset else {
        return client::require_index(client, &args.index).await;
    };
    reset.ensure_fresh().await?;
    if args.checks_analyzer() {
        reset.verify_analyzer().await?;
    }
    Ok(())
}

/// A run that deletes an index says so before it does it, naming the index and
/// the endpoint. The reset defaults to on, so this one line is the only thing
/// standing between a mistyped `--url` and someone's data.
fn announce_reset(args: &Args) {
    note(&reset_line(args));
}

fn reset_line(args: &Args) -> String {
    if !args.resets() {
        return "index reset OFF (--no-reset): levels after the first rewrite the same \
                documents, so only the first measures a build"
            .to_string();
    }
    format!(
        "index reset ON: DELETING INDEX {} before every level at {}, recreated from {}",
        args.index,
        args.connect_options().url,
        args.index_config
    )
}

async fn sweep_levels(
    args: &Args,
    client: OpenSearch,
    reset: Option<&IndexReset>,
    notes: &Notes,
    sink: &mut CsvSink,
) -> (Vec<PointResult>, bool) {
    let inserter = Arc::new(BulkInserter::new(client, &args.index));
    let source = CorpusSource::new(&args.corpus, args.max_docs, args.batch_size);
    let nothing = NothingToPrepare;
    let before_level: &dyn BeforeLevel = match reset {
        Some(reset) => reset,
        None => &nothing,
    };
    let cancel = watch_for_interrupt();
    let mut results: Vec<PointResult> = Vec::new();

    let outcome = {
        let mut collect = |result: PointResult| -> Result<()> {
            sink.append_row(&result)?;
            results.push(result);
            Ok(())
        };
        sweep::run_sweep(
            Ladder {
                inserter,
                before_level,
                levels: &args.concurrency.0,
                shape: shape(args),
            },
            || source.open(),
            notes,
            &cancel,
            &mut collect,
        )
        .await
    };
    (results, report_outcome(outcome, sink.destination()))
}

fn shape(args: &Args) -> Shape {
    Shape {
        batch_size: args.batch_size,
        queue_depth: args.queue_depth,
    }
}

/// Ctrl-C is the ordinary way a long ladder ends early, and the levels already
/// measured are worth as much then as after a transport error.
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

fn describe(cluster: &Cluster) {
    say_each(&cluster_lines(cluster));
}

fn cluster_lines(cluster: &Cluster) -> Vec<String> {
    vec![
        format!(
            "opensearch {} ({}), client {}, http {}, {}",
            cluster.opensearch_version,
            cluster.distribution,
            cluster.client_version,
            cluster.http_client_version,
            cluster.runtime
        ),
        format!(
            "index={} shards={} replicas={} refresh_interval={} source={} analyzer={} write_pool={}",
            cluster.index,
            cluster.index_shards,
            cluster.replicas,
            cluster.refresh_interval,
            cluster.source_enabled,
            cluster.body_analyzer,
            cluster.write_pool
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
