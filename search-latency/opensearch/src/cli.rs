//! Command line: a corpus, a query set, a ladder of concurrency levels, and an
//! OpenSearch endpoint to ask.
use std::path::PathBuf;
use std::time::Duration;

use clap::Parser;
use osrate::client::ConnectOptions;
use osrate::reset::{GateTiming, IndexConfig, DEFAULT_INDEX_CONFIG};
use search_latency_core::bootstrap::{BuildPolicy, BuildTiming};
use search_latency_core::cell::CellSettings;
use search_latency_core::cli::{
    at_least_one, available_cores, chosen_classes, classes_setting, non_negative_seconds,
    path_setting, positive_seconds, Classes, Levels,
};
use search_latency_core::report::{Shape, OPENSEARCH};
use search_latency_core::search::HTTP;

use crate::loader::LoadShape;
use crate::search::{QueryShape, BODY_FIELD, DEFAULT_OPERATOR};

pub const DEFAULT_URL: &str = "http://localhost:9200";
pub const DEFAULT_INDEX: &str = "wiki-articles";
pub const DEFAULT_REQUEST_TIMEOUT_S: &str = "30.0";
pub const DEFAULT_LIMIT: &str = "10";
pub const DEFAULT_WARMUP_S: &str = "5.0";
pub const DEFAULT_DURATION_S: &str = "20.0";
pub const DEFAULT_LOAD_CONCURRENCY: &str = "16";
pub const DEFAULT_LOAD_BATCH_SIZE: &str = "512";
pub const DEFAULT_INDEX_INTERVAL_S: &str = "1.0";
pub const DEFAULT_INDEX_BUILD_TIMEOUT_S: &str = "3600.0";
pub const DEFAULT_RESET_TIMEOUT_S: &str = "300.0";
pub const QUEUE_DEPTH_PER_WORKER: usize = 10;
pub const STDOUT: &str = "-";

#[derive(Debug, Parser)]
#[command(
    name = "ossearch",
    about = "Search-latency matrix for OpenSearch: p50/p90/p99 and q/s per concurrency and query class",
    long_about = "Measures what a full-text search costs at N requests in flight, for every \
(concurrency, query class) cell of a matrix, against a resident index. Closed loop: each worker \
sends the next query the moment the previous one answers, so the latencies are service times and \
the throughput is what the engine gave back.\n\n\
BUILDS THE INDEX FIRST IF IT HAS TO. If the index does not already hold exactly as many \
documents as the corpus, this DELETES IT and rebuilds it from the corpus before measuring \
anything. Pass --no-index-build to refuse instead of writing."
)]
pub struct Args {
    /// Corpus JSONL: one {id, uuid, title, text} per line. The index is
    /// expected to hold exactly as many documents as this has lines.
    #[arg(long)]
    pub corpus: PathBuf,

    /// Query set JSON, as written by ftsbench.generate_queries
    #[arg(long)]
    pub queries: PathBuf,

    /// Comma-separated query classes; every class in the set unless given
    #[arg(long)]
    pub query_classes: Option<Classes>,

    /// Comma-separated levels, e.g. 1,2,4,8,16,32,64. Repeat the ladder to
    /// interleave traversals: 8,16,32,8,16,32 measures every cell twice with
    /// host drift spread across the matrix rather than across one curve.
    #[arg(long)]
    pub concurrency: Levels,

    #[arg(long, env = "OS_URL", default_value = DEFAULT_URL)]
    pub url: String,

    #[arg(long, default_value = DEFAULT_INDEX)]
    pub index: String,

    /// The indexed text field, which `query_string` searches by default
    #[arg(long, default_value = BODY_FIELD)]
    pub field: String,

    /// How bare terms in a query combine. `OR` is Lucene's default and is what
    /// the Tantivy parser behind the other engine's BM25() does.
    #[arg(long, default_value = DEFAULT_OPERATOR)]
    pub default_operator: String,

    /// Top-N asked for. One value per run, not a matrix dimension: it reaches
    /// the CSV as a column so two runs at two limits can be told apart.
    #[arg(long, default_value = DEFAULT_LIMIT, value_parser = parse_limit)]
    pub limit: usize,

    /// Project title and body, not just the id — what an application does.
    /// Changes what is measured, and the other engine's vector-store interface
    /// cannot honour it at all.
    #[arg(long)]
    pub fetch_documents: bool,

    /// Seconds of queries per cell before the counting starts
    #[arg(long, default_value = DEFAULT_WARMUP_S, value_parser = parse_warmup)]
    pub warmup: f64,

    /// Seconds of measured queries per cell
    #[arg(long, default_value = DEFAULT_DURATION_S, value_parser = parse_duration)]
    pub duration: f64,

    /// Documents the index should hold; 0 takes the whole corpus
    #[arg(long, default_value_t = 0)]
    pub max_docs: usize,

    #[arg(long, default_value = DEFAULT_REQUEST_TIMEOUT_S)]
    pub request_timeout: f64,

    /// Tokio worker threads; defaults to every core the machine reports
    #[arg(long, value_parser = parse_workers)]
    pub tokio_workers: Option<usize>,

    /// CSV destination; '-' writes to stdout
    #[arg(long, default_value = STDOUT)]
    pub out: String,

    /// Directory for each cell's full latency distribution, one CSV per cell.
    /// Off unless given, and worth giving whenever the run will be repeated:
    /// percentiles cannot be averaged across repeats, only recomputed.
    #[arg(long)]
    pub latencies_dir: Option<PathBuf>,

    /// Requests in flight while the index is being filled. Not a measurement:
    /// see `loader.rs`.
    #[arg(long, default_value = DEFAULT_LOAD_CONCURRENCY, value_parser = parse_load_concurrency)]
    pub load_concurrency: usize,

    /// Documents per `_bulk` while the index is being filled
    #[arg(long, default_value = DEFAULT_LOAD_BATCH_SIZE, value_parser = parse_batch_size)]
    pub load_batch_size: usize,

    /// `ramindex`, `disk`, or a path to an index config JSON
    #[arg(long, default_value = DEFAULT_INDEX_CONFIG)]
    pub index_config: String,

    /// Overrides the config's `refresh_interval` when the index is created
    #[arg(long, env = "OS_REFRESH_INTERVAL")]
    pub refresh_interval: Option<String>,

    /// Seconds between index-count polls, while building and while gating
    #[arg(long, default_value = DEFAULT_INDEX_INTERVAL_S)]
    pub index_interval: f64,

    /// Seconds the index has to reach the corpus's document count
    #[arg(long, default_value = DEFAULT_INDEX_BUILD_TIMEOUT_S)]
    pub index_build_timeout: f64,

    /// Seconds each reset gate may wait for the index
    #[arg(long, default_value = DEFAULT_RESET_TIMEOUT_S)]
    pub reset_timeout: f64,

    /// Rebuild the index even if it already holds every document
    #[arg(long)]
    pub rebuild_index: bool,

    /// Never write: verify the index against the corpus and refuse to measure
    /// if it does not match. For an index somebody else manages.
    #[arg(long)]
    pub no_index_build: bool,

    /// Skip the analyzer probe. An analyzer that differs from the other
    /// engine's makes every latency below a comparison of tokenizers.
    #[arg(long)]
    pub no_analyzer_check: bool,
}

impl Args {
    /// Empty means every class the query set has, which is what
    /// `QuerySet::select` takes an empty list to mean.
    pub fn chosen_classes(&self) -> Vec<String> {
        chosen_classes(self.query_classes.as_ref())
    }

    pub fn tokio_workers(&self) -> usize {
        self.tokio_workers.unwrap_or_else(available_cores)
    }

    pub fn connect_options(&self) -> ConnectOptions {
        ConnectOptions {
            url: self.url.clone(),
            index: self.index.clone(),
            request_timeout: self.timeout(),
        }
    }

    pub fn timeout(&self) -> Duration {
        Duration::from_secs_f64(self.request_timeout)
    }

    pub fn cell_settings(&self) -> CellSettings {
        CellSettings {
            warmup: Duration::from_secs_f64(self.warmup),
            duration: Duration::from_secs_f64(self.duration),
        }
    }

    pub fn shape(&self) -> Shape {
        Shape {
            engine: OPENSEARCH,
            interface: HTTP,
            limit: self.limit,
            fetch_documents: self.fetch_documents,
        }
    }

    pub fn query_shape(&self) -> QueryShape {
        QueryShape {
            field: self.field.clone(),
            default_operator: self.default_operator.clone(),
            limit: self.limit,
            fetch_documents: self.fetch_documents,
        }
    }

    pub fn load_shape(&self) -> LoadShape {
        LoadShape {
            concurrency: self.load_concurrency,
            batch_size: self.load_batch_size,
            queue_depth: QUEUE_DEPTH_PER_WORKER,
        }
    }

    pub fn index_config(&self) -> anyhow::Result<IndexConfig> {
        IndexConfig::select(&self.index_config)?
            .with_refresh_interval(self.refresh_interval.as_deref())
    }

    pub fn build_policy(&self) -> BuildPolicy {
        BuildPolicy {
            rebuild: self.rebuild_index,
            may_build: !self.no_index_build,
        }
    }

    pub fn build_timing(&self) -> BuildTiming {
        BuildTiming {
            poll_interval: Duration::from_secs_f64(self.index_interval),
            timeout: Duration::from_secs_f64(self.index_build_timeout),
        }
    }

    pub fn gate_timing(&self) -> GateTiming {
        GateTiming {
            poll_interval: Duration::from_secs_f64(self.index_interval),
            timeout: Duration::from_secs_f64(self.reset_timeout),
        }
    }

    /// The probe is a read, so it is worth doing whether or not this run is the
    /// one that created the index — an index somebody else built with the wrong
    /// analyzer is exactly the case it exists to catch.
    pub fn checks_analyzer(&self) -> bool {
        !self.no_analyzer_check
    }

    /// What the loader names before it destroys anything.
    pub fn load_target(&self) -> String {
        format!("index {} at {}", self.index, self.url)
    }

    pub fn settings(&self) -> Vec<(String, String)> {
        [
            ("interface", HTTP.to_string()),
            ("limit", self.limit.to_string()),
            ("fetch_documents", self.fetch_documents.to_string()),
            ("query_field", self.field.clone()),
            ("default_operator", self.default_operator.clone()),
            ("concurrency", self.concurrency.to_string()),
            (
                "query_classes",
                classes_setting(self.query_classes.as_ref()),
            ),
            ("queries", self.queries.display().to_string()),
            ("warmup_s", self.warmup.to_string()),
            ("duration_s", self.duration.to_string()),
            ("request_timeout_s", self.request_timeout.to_string()),
            ("tokio_workers", self.tokio_workers().to_string()),
            ("corpus", self.corpus.display().to_string()),
            ("max_docs", self.max_docs.to_string()),
            ("latencies_dir", path_setting(self.latencies_dir.as_ref())),
            ("index_config", self.index_config.clone()),
            ("analyzer_check", self.checks_analyzer().to_string()),
            ("load_concurrency", self.load_concurrency.to_string()),
            ("load_batch_size", self.load_batch_size.to_string()),
            (
                "index_build_timeout_s",
                self.index_build_timeout.to_string(),
            ),
        ]
        .into_iter()
        .map(|(key, value)| (key.to_string(), value))
        .collect()
    }
}

fn parse_workers(raw: &str) -> Result<usize, String> {
    at_least_one(raw, "--tokio-workers")
}

/// The other engine's ceiling, kept here too: a matrix whose two halves could
/// ask for different top-Ns would not be one matrix.
fn parse_limit(raw: &str) -> Result<usize, String> {
    let limit = at_least_one(raw, "--limit")?;
    if limit > MAX_LIMIT {
        return Err(format!("--limit must be <= {MAX_LIMIT}, got {limit}"));
    }
    Ok(limit)
}

pub const MAX_LIMIT: usize = 1000;

fn parse_load_concurrency(raw: &str) -> Result<usize, String> {
    at_least_one(raw, "--load-concurrency")
}

fn parse_batch_size(raw: &str) -> Result<usize, String> {
    at_least_one(raw, "--load-batch-size")
}

fn parse_duration(raw: &str) -> Result<f64, String> {
    positive_seconds(raw, "--duration")
}

fn parse_warmup(raw: &str) -> Result<f64, String> {
    non_negative_seconds(raw, "--warmup")
}

#[cfg(test)]
#[path = "cli_tests.rs"]
mod tests;
