//! ossearch --corpus ../../data/corpus.jsonl --queries ../../data/queries.json \
//!          --concurrency 1,2,4,8,16,32,64
//!
//! What a full-text search costs on OpenSearch at N requests in flight, per
//! query class, against an index this tool will build first if it has to.
use std::process::ExitCode;
use std::sync::Arc;

use anyhow::{Context, Result};
use build_rate_core::notes::{note, Notes};
use build_rate_core::report::header_lines;
use clap::Parser;
use opensearch::OpenSearch;
use osrate::client::{self, Cluster};
use osrate::corpus::CorpusSource;
use osrate::insert::BulkInserter;
use osrate::reset::IndexReset;
use osrate::vstore::StatsProbe;
use search_latency_core::bootstrap::{count_documents, ensure_index, IndexReady};
use search_latency_core::latencies::LatencyFiles;
use search_latency_core::matrix::{run_matrix, Plan, Runner};
use search_latency_core::queries::QuerySet;
use search_latency_core::report::{CellResult, CsvSink};
use search_latency_core::run::{
    build_runtime, echo_summary, exit_code, report_outcome, say_each, watch_for_interrupt,
};
use search_latency_core::search::Searcher;

use ossearch::cli::Args;
use ossearch::loader::BulkLoader;
use ossearch::search::HttpSearcher;

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

async fn measure(args: Args, workers: usize) -> Result<ExitCode> {
    let notes = Notes::stderr();
    let query_set = QuerySet::load(&args.queries)?;
    let classes = query_set.select(&args.chosen_classes())?;
    let expected = count_documents(&args.corpus, args.max_docs)?;

    let client = client::connect(&args.connect_options()).await?;
    let index = prepare_index(&args, client.clone(), expected, &notes).await?;
    check_analyzer(&args, client.clone(), &notes).await?;

    let cluster = client::read_cluster(&client, &args.index, workers).await?;
    describe(&cluster);

    let searcher = open_searcher(&args, client);
    let settings = settings_with_index(&args, &query_set, &index);
    let mut sink = CsvSink::open(&args.out)?;
    sink.write_preamble(&header_lines(&cluster.facts(), &settings))?;
    let latencies = open_latencies(&args, &cluster, &settings)?;

    let plan = Plan {
        levels: args.concurrency.0.clone(),
        classes,
    };
    let (results, aborted) = walk_matrix(
        &args,
        &searcher,
        &plan,
        &notes,
        &mut sink,
        latencies.as_ref(),
    )
    .await;

    echo_summary(&results);
    Ok(exit_code(&results, aborted))
}

fn index_reset(args: &Args, client: OpenSearch, notes: &Notes) -> Result<IndexReset> {
    Ok(IndexReset::new(
        client,
        &args.index,
        &args.url,
        args.index_config()?,
        args.gate_timing(),
        notes.clone(),
    ))
}

/// Before anything is measured and before anything is destroyed: the index has
/// to hold exactly as many documents as the corpus does.
///
/// The corpus is counted by the caller, before the client is opened, so that an
/// unreadable one costs a second rather than a connection to an engine it was
/// never going to be measured against.
async fn prepare_index(
    args: &Args,
    client: OpenSearch,
    expected: u64,
    notes: &Notes,
) -> Result<IndexReady> {
    announce_what_a_build_would_cost(args, expected);
    let loader = BulkLoader::new(
        Arc::new(BulkInserter::new(client.clone(), &args.index)),
        index_reset(args, client.clone(), notes)?,
        CorpusSource::new(&args.corpus, args.max_docs),
        args.load_shape(),
        args.load_target(),
        notes.clone(),
    );
    let probe = StatsProbe::new(client, &args.url, &args.index);
    ensure_index(
        &probe,
        &loader,
        expected,
        args.build_policy(),
        &args.build_timing(),
        notes,
    )
    .await
}

/// A run that may delete an index says so before it looks at it, naming the
/// index: the flag defaults to allowing it, so this line is the only thing
/// standing between a mistyped `--url` and someone's data.
fn announce_what_a_build_would_cost(args: &Args, expected: u64) {
    if args.no_index_build {
        note(&format!(
            "--no-index-build: the index must already hold {expected} documents, \
             nothing will be written"
        ));
        return;
    }
    note(&format!(
        "if the index does not already hold {expected} documents, {} WILL BE \
         DELETED and rebuilt from {}",
        args.load_target(),
        args.corpus.display()
    ));
}

/// Read whether or not this run built the index. An index somebody else created
/// with a different analyzer is exactly the case worth catching, and every
/// latency below it would be a comparison of tokenizers rather than of engines.
async fn check_analyzer(args: &Args, client: OpenSearch, notes: &Notes) -> Result<()> {
    if !args.checks_analyzer() {
        return Ok(());
    }
    index_reset(args, client, notes)?.verify_analyzer().await
}

fn open_searcher(args: &Args, client: OpenSearch) -> Arc<dyn Searcher> {
    Arc::new(HttpSearcher::new(
        client,
        &args.url,
        &args.index,
        args.query_shape(),
    ))
}

fn settings_with_index(
    args: &Args,
    query_set: &QuerySet,
    index: &IndexReady,
) -> Vec<(String, String)> {
    let mut settings = args.settings();
    settings.push(("query_corpus".to_string(), query_set.corpus().to_string()));
    settings.push(("index_docs".to_string(), index.docs.to_string()));
    settings.push(("index_built_here".to_string(), index.built.to_string()));
    settings
}

/// Opened before the first query runs, so an unwritable directory costs a
/// second rather than a whole matrix.
fn open_latencies(
    args: &Args,
    cluster: &Cluster,
    settings: &[(String, String)],
) -> Result<Option<LatencyFiles>> {
    let Some(dir) = args.latencies_dir.as_ref() else {
        return Ok(None);
    };
    let files = LatencyFiles::new(dir)
        .with_context(|| format!("cannot write latency distributions to {}", dir.display()))?;
    note(&format!(
        "latency distributions: {}/<class>-c<level>-<n>.csv",
        dir.display()
    ));
    Ok(Some(
        files.with_preamble(header_lines(&cluster.facts(), settings)),
    ))
}

async fn walk_matrix(
    args: &Args,
    searcher: &Arc<dyn Searcher>,
    plan: &Plan,
    notes: &Notes,
    sink: &mut CsvSink,
    latencies: Option<&LatencyFiles>,
) -> (Vec<CellResult>, bool) {
    let cancel = watch_for_interrupt();
    let runner = Runner {
        searcher,
        shape: args.shape(),
        settings: args.cell_settings(),
        notes,
        latencies,
    };
    let mut results: Vec<CellResult> = Vec::new();
    let outcome = {
        let mut collect = |result: CellResult| -> Result<()> {
            sink.append_row(&result)?;
            results.push(result);
            Ok(())
        };
        run_matrix(&runner, plan, &cancel, &mut collect).await
    };
    (results, report_outcome(outcome, sink.destination()))
}

fn describe(cluster: &Cluster) {
    say_each(&[cluster
        .facts()
        .iter()
        .map(|(key, value)| format!("{key}={value}"))
        .collect::<Vec<_>>()
        .join(" ")]);
}
