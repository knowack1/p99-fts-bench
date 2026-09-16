//! osrate --corpus ../../data/corpus.jsonl --concurrency 24,48,96,192,384
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

use anyhow::{anyhow, bail, Context, Result};
use clap::Parser;
use opensearch::OpenSearch;

use osrate::build_rate::IndexWatch;
use osrate::cli::Args;
use osrate::client::{self, Cluster};
use osrate::corpus::{self, CorpusSource};
use osrate::insert::BulkInserter;
use osrate::notes::{note, Notes};
use osrate::report::{self, CsvSink, PointResult};
use osrate::reset::{IndexReset, ResettingInserter};
use osrate::run::{
    build_runtime, echo_summary, exit_code, report_outcome, say_each, watch_for_interrupt,
};
use osrate::samples::SampleFiles;
use osrate::sweep::{self, Rung, Watchers};
use osrate::vstore::StatsProbe;

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
    let client = client::connect(&args.connect_options()).await?;
    let notes = Notes::stderr();
    announce_reset(&args);
    let reset = open_reset(&args, &client, &notes)?;
    prepare_the_index(&args, &client, reset.as_deref()).await?;

    let cluster = client::read_cluster(&client, &args.index, workers).await?;
    describe(&cluster);
    check_the_refresh_policy(&args, &cluster)?;

    let mut sink = CsvSink::open(&args.out)?;
    let preamble = report::header_lines(&cluster, &args.settings());
    sink.write_preamble(&preamble)?;
    let samples = open_samples(&args, &preamble)?;
    let (results, aborted) = sweep_levels(
        &args,
        &rungs,
        client,
        reset,
        &notes,
        &mut sink,
        samples.as_ref(),
    )
    .await;

    echo_summary(&results);
    Ok(exit_code(&results, aborted))
}

/// A searchable count that never advances on a timer is a build this tool
/// cannot see the end of. It can still measure one — by asking the index to
/// publish once the engine has stopped — but not if that was turned off too.
fn check_the_refresh_policy(args: &Args, cluster: &client::Cluster) -> Result<()> {
    if !args.watches_index() {
        return Ok(());
    }
    let interval = cluster.refresh_interval.as_str();
    if !interval.starts_with("-1") {
        note(&format!(
            "index watch ON: polling _stats, refresh_interval={interval}"
        ));
        return Ok(());
    }
    if args.asks_for_a_final_refresh() {
        note(
            "index watch ON: refresh_interval=-1, so nothing becomes searchable \
             until this asks. Every level's index_status will read `refreshed` \
             and its settle time is the harness's refresh, not the engine's.",
        );
        return Ok(());
    }
    bail!(
        "--index-watch with refresh_interval=-1 and --no-index-final-refresh: \
         nothing would ever become searchable, so every level would report a \
         build of zero documents that never settled. Drop one of the three."
    )
}

/// Opened before the first document, so an unwritable directory costs a second
/// rather than a whole ladder — the rule `CsvSink::open` already follows.
fn open_samples(args: &Args, preamble: &[String]) -> Result<Option<SampleFiles>> {
    let Some(dir) = args.samples_dir.as_ref() else {
        return Ok(None);
    };
    let files = SampleFiles::new(dir)
        .with_context(|| format!("cannot write per-second samples to {}", dir.display()))?;
    note(&format!(
        "per-second samples: {}/c<level>-b{}-<n>.csv",
        dir.display(),
        args.batch_size
    ));
    Ok(Some(
        files
            .with_preamble(preamble.to_vec())
            .with_batch_size(args.batch_size),
    ))
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
    rungs: &[Rung],
    client: OpenSearch,
    reset: Option<Box<IndexReset>>,
    notes: &Notes,
    sink: &mut CsvSink,
    samples: Option<&SampleFiles>,
) -> (Vec<PointResult>, bool) {
    let index = index_watch(args, &client);
    let inserter = Arc::new(BulkInserter::new(client, &args.index));
    let source = CorpusSource::new(&args.corpus, args.max_docs);
    let inserters = ResettingInserter::new(inserter, reset.map(|reset| *reset));
    let cancel = watch_for_interrupt();
    let mut results: Vec<PointResult> = Vec::new();

    let outcome = {
        let mut collect = |result: PointResult| -> Result<()> {
            sink.append_row(&result)?;
            results.push(result);
            Ok(())
        };
        sweep::run_sweep(
            &inserters,
            || corpus::batches(&source, args.batch_size),
            rungs,
            sweep::loader(args.batch_size, args.queue_depth),
            &Watchers {
                index: &index,
                notes,
                samples,
            },
            &cancel,
            &mut collect,
        )
        .await
    };
    (results, report_outcome(outcome, sink.destination()))
}

/// `_stats` on the index this run loads, or nothing at all.
fn index_watch(args: &Args, client: &OpenSearch) -> IndexWatch {
    if !args.watches_index() {
        return IndexWatch::off();
    }
    let probe = StatsProbe::new(client.clone(), &args.url, &args.index);
    let probe = if args.asks_for_a_final_refresh() {
        probe
    } else {
        probe.without_a_final_refresh()
    };
    IndexWatch::on(Arc::new(probe), args.watch_timing())
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

#[cfg(test)]
#[path = "main_tests.rs"]
mod tests;
