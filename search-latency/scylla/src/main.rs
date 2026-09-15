//! scyllasearch --corpus ../../data/corpus.jsonl --queries ../../data/queries.json \
//!              --concurrency 1,2,4,8,16,32,64
//!
//! What a full-text search costs on ScyllaDB at N requests in flight, per query
//! class, against an index this tool will build first if it has to.
use std::process::ExitCode;
use std::sync::Arc;

use anyhow::{Context, Result};
use build_rate_core::index::IndexProbe;
use build_rate_core::notes::{note, Notes};
use build_rate_core::report::header_lines;
use scylla::client::session::Session;
use scyllarate::corpus::CorpusSource;
use scyllarate::reset::ResettingInserters;
use scyllarate::session::{self, Topology};
use scyllarate::vstore::VectorStoreProbe;
use search_latency_core::bootstrap::{count_documents, ensure_index, IndexReady};
use search_latency_core::latencies::LatencyFiles;
use search_latency_core::matrix::{run_matrix, Plan, Runner};
use search_latency_core::queries::{queries_of, QueryClass, QuerySet};
use search_latency_core::report::{CellResult, CsvSink};
use search_latency_core::run::{
    build_runtime, echo_summary, exit_code, report_outcome, say_each, watch_for_interrupt,
};
use search_latency_core::search::Searcher;

use scyllasearch::bm25::Bm25Searcher;
use scyllasearch::cli::{Args, Interface};
use scyllasearch::cql::{CqlSearcher, QueryShape};
use scyllasearch::loader::CqlLoader;

use clap::Parser;

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
    args.validate().map_err(|why| anyhow::anyhow!(why))?;
    let notes = Notes::stderr();
    let query_set = QuerySet::load(&args.queries)?;
    let classes = query_set.select(&args.chosen_classes())?;
    let expected = count_documents(&args.corpus, args.max_docs)?;

    let session = Arc::new(session::connect(&args.connect_options()).await?);
    let topology = session::read_topology(&session, &args.keyspace, workers).await?;
    describe(&topology);

    let probe = Arc::new(VectorStoreProbe::new(
        &args.vs_url,
        &args.keyspace,
        &args.vs_index,
        args.timeout(),
    )?);
    let index = prepare_index(
        &args,
        Arc::clone(&session),
        Arc::clone(&probe),
        expected,
        &notes,
    )
    .await?;

    let searcher = open_searcher(&args, session, &classes).await?;
    let settings = settings_with_index(&args, probe.as_ref(), &query_set, &index).await;
    let mut sink = CsvSink::open(&args.out)?;
    sink.write_preamble(&header_lines(&topology.facts(), &settings))?;
    let latencies = open_latencies(&args, &topology, &settings)?;

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

/// Before anything is measured and before anything is destroyed: the index has
/// to hold exactly as many documents as the corpus does.
///
/// The corpus is counted by the caller, before the session is opened, so that
/// an unreadable one costs a second rather than a connection to an engine it
/// was never going to be measured against.
async fn prepare_index(
    args: &Args,
    session: Arc<Session>,
    probe: Arc<VectorStoreProbe>,
    expected: u64,
    notes: &Notes,
) -> Result<IndexReady> {
    announce_what_a_build_would_cost(args, expected);
    let loader = CqlLoader::new(
        ResettingInserters::new(
            session,
            args.reset_plan(),
            Some(Arc::clone(&probe) as Arc<dyn IndexProbe>),
            args.gate_timing(),
            notes.clone(),
            true,
        ),
        CorpusSource::new(&args.corpus, args.max_docs),
        args.load_concurrency,
        args.load_target(),
        notes.clone(),
    );
    ensure_index(
        probe.as_ref(),
        &loader,
        expected,
        args.build_policy(),
        &args.build_timing(),
        notes,
    )
    .await
}

/// A run that may drop a keyspace says so before it looks at the index, naming
/// the keyspace: the flag defaults to allowing it, so this line is the only
/// thing standing between a mistyped `--hosts` and someone's data.
fn announce_what_a_build_would_cost(args: &Args, expected: u64) {
    if args.no_index_build {
        note(&format!(
            "--no-index-build: the index must already hold {expected} documents, \
             nothing will be written"
        ));
        return;
    }
    note(&format!(
        "if the index does not already hold {expected} documents, KEYSPACE {} \
         WILL BE DROPPED and rebuilt from {}",
        args.keyspace,
        args.corpus.display()
    ));
}

async fn open_searcher(
    args: &Args,
    session: Arc<Session>,
    classes: &[QueryClass],
) -> Result<Arc<dyn Searcher>> {
    match args.interface {
        Interface::Cql => open_cql(args, session, classes).await,
        Interface::VectorStore => open_bm25(args),
    }
}

/// Opened against the classes the matrix will actually walk, not the whole
/// query set: in `--statement prepared` that is what makes "every query this
/// will be asked was prepared" an invariant rather than a hope.
async fn open_cql(
    args: &Args,
    session: Arc<Session>,
    classes: &[QueryClass],
) -> Result<Arc<dyn Searcher>> {
    let shape: QueryShape = args.query_shape();
    let endpoint = format!("{}:{}/{}", args.hosts, args.port, args.keyspace);
    Ok(Arc::new(
        CqlSearcher::open(
            session,
            shape,
            args.statement.mode(),
            &queries_of(classes),
            endpoint,
        )
        .await?,
    ))
}

fn open_bm25(args: &Args) -> Result<Arc<dyn Searcher>> {
    Ok(Arc::new(Bm25Searcher::new(
        &args.vs_url,
        &args.keyspace,
        &args.vs_index,
        args.limit,
        args.fetch_documents,
        args.timeout(),
    )?))
}

async fn settings_with_index(
    args: &Args,
    probe: &VectorStoreProbe,
    query_set: &QuerySet,
    index: &IndexReady,
) -> Vec<(String, String)> {
    let mut settings = args.settings();
    settings.push(("vector_store".to_string(), probe.version().await));
    settings.push(("query_corpus".to_string(), query_set.corpus().to_string()));
    settings.push(("index_docs".to_string(), index.docs.to_string()));
    settings.push(("index_built_here".to_string(), index.built.to_string()));
    settings
}

/// Opened before the first query runs, so an unwritable directory costs a
/// second rather than a whole matrix.
fn open_latencies(
    args: &Args,
    topology: &Topology,
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
        files.with_preamble(header_lines(&topology.facts(), settings)),
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

fn describe(topology: &Topology) {
    say_each(&[topology
        .facts()
        .iter()
        .map(|(key, value)| format!("{key}={value}"))
        .collect::<Vec<_>>()
        .join(" ")]);
}
