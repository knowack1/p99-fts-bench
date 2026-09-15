//! Command line: a corpus, a query set, a ladder of concurrency levels, and
//! which way into the index the queries should go.
use std::path::PathBuf;
use std::time::Duration;

use clap::{Parser, ValueEnum};
use scylla::statement::Consistency;
use scyllarate::reset::{GateTiming, ResetPlan};
use scyllarate::session::{consistency_from_name, consistency_name, ConnectOptions};
use scyllarate::vstore::{DEFAULT_VS_INDEX, DEFAULT_VS_URL};
use search_latency_core::bootstrap::{BuildPolicy, BuildTiming};
use search_latency_core::cell::CellSettings;
use search_latency_core::cli::{
    at_least_one, available_cores, chosen_classes, classes_setting, non_negative_seconds,
    path_setting, positive_seconds, Classes, Levels,
};
use search_latency_core::report::{Shape, SCYLLADB};
use search_latency_core::search::{CQL, VECTOR_STORE};

use crate::cql::{QueryShape, StatementMode};

pub const DEFAULT_HOSTS: &str = "127.0.0.1";
pub const DEFAULT_PORT: &str = "9042";
pub const DEFAULT_KEYSPACE: &str = "wiki";
pub const DEFAULT_TABLE: &str = "articles";
pub const DEFAULT_COLUMN: &str = "body";
pub const DEFAULT_CONSISTENCY: &str = "LOCAL_ONE";
pub const DEFAULT_REQUEST_TIMEOUT_S: &str = "30.0";
pub const DEFAULT_LIMIT: &str = "10";
pub const DEFAULT_WARMUP_S: &str = "5.0";
pub const DEFAULT_DURATION_S: &str = "20.0";
pub const DEFAULT_LOAD_CONCURRENCY: &str = "64";
pub const DEFAULT_INDEX_INTERVAL_S: &str = "1.0";
pub const DEFAULT_INDEX_BUILD_TIMEOUT_S: &str = "3600.0";
pub const DEFAULT_RESET_TIMEOUT_S: &str = "300.0";
pub const STDOUT: &str = "-";

/// Which path into the index the queries take.
#[derive(Debug, Clone, Copy, PartialEq, Eq, ValueEnum)]
pub enum Interface {
    /// ScyllaDB's own read path, coordinator and projection included.
    Cql,
    /// The vector-store's BM25 endpoint, ScyllaDB out of the path.
    VectorStore,
}

impl Interface {
    pub fn name(self) -> &'static str {
        match self {
            Self::Cql => CQL,
            Self::VectorStore => VECTOR_STORE,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, ValueEnum)]
pub enum Statement {
    /// Re-parsed by the coordinator per request, which is what the other
    /// engine's parser does and what this bench's earlier read arm measured.
    Literal,
    /// Prepared once per distinct query before the matrix starts, which is what
    /// an application does.
    Prepared,
}

impl Statement {
    pub fn mode(self) -> StatementMode {
        match self {
            Self::Literal => StatementMode::Literal,
            Self::Prepared => StatementMode::Prepared,
        }
    }
}

#[derive(Debug, Parser)]
#[command(
    name = "scyllasearch",
    about = "Search-latency matrix for ScyllaDB FTS: p50/p90/p99 and q/s per concurrency and query class",
    long_about = "Measures what a full-text search costs at N requests in flight, for every \
(concurrency, query class) cell of a matrix, against a resident index. Closed loop: each worker \
sends the next query the moment the previous one answers, so the latencies are service times and \
the throughput is what the engine gave back.\n\n\
BUILDS THE INDEX FIRST IF IT HAS TO. If the index does not already hold exactly as many \
documents as the corpus, this DROPS THE KEYSPACE and fills it from the corpus before measuring \
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

    /// Which path into the index the queries take
    #[arg(long, value_enum, default_value_t = Interface::Cql)]
    pub interface: Interface,

    /// How a CQL statement reaches the coordinator. Ignored by --interface
    /// vector-store, which has no statements.
    #[arg(long, value_enum, default_value_t = Statement::Literal)]
    pub statement: Statement,

    /// Top-N asked for. One value per run, not a matrix dimension: it reaches
    /// the CSV as a column so two runs at two limits can be told apart.
    #[arg(long, default_value = DEFAULT_LIMIT, value_parser = parse_limit)]
    pub limit: usize,

    /// Project title and body, not just the id — what an application does.
    /// Changes what is measured, and --interface vector-store refuses it.
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

    /// Comma-separated contact points
    #[arg(long, env = "SCYLLA_HOSTS", default_value = DEFAULT_HOSTS)]
    pub hosts: Hosts,

    #[arg(long, env = "SCYLLA_PORT", default_value = DEFAULT_PORT)]
    pub port: u16,

    #[arg(long, default_value = DEFAULT_KEYSPACE)]
    pub keyspace: String,

    #[arg(long, default_value = DEFAULT_TABLE)]
    pub table: String,

    /// The indexed text column, on both sides of the BM25 call
    #[arg(long, default_value = DEFAULT_COLUMN)]
    pub column: String,

    #[arg(long, value_parser = parse_consistency, default_value = DEFAULT_CONSISTENCY)]
    pub consistency: Consistency,

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

    /// Vector-store base URL: where the index count is read, and where
    /// --interface vector-store sends its queries
    #[arg(long, env = "VS_URL", default_value = DEFAULT_VS_URL)]
    pub vs_url: String,

    /// Full-text index name, on the CQL side and in the vector-store path alike
    #[arg(long, default_value = DEFAULT_VS_INDEX)]
    pub vs_index: String,

    /// Requests in flight while the index is being filled. Not a measurement:
    /// see `loader.rs`.
    #[arg(long, default_value = DEFAULT_LOAD_CONCURRENCY, value_parser = parse_load_concurrency)]
    pub load_concurrency: usize,

    /// Seconds between index-count polls, while building and while gating
    #[arg(long, default_value = DEFAULT_INDEX_INTERVAL_S)]
    pub index_interval: f64,

    /// Seconds the index has to reach the corpus's document count
    #[arg(long, default_value = DEFAULT_INDEX_BUILD_TIMEOUT_S)]
    pub index_build_timeout: f64,

    /// Seconds each reset gate may wait for the vector-store
    #[arg(long, default_value = DEFAULT_RESET_TIMEOUT_S)]
    pub reset_timeout: f64,

    /// Rebuild the index even if it already holds every document
    #[arg(long)]
    pub rebuild_index: bool,

    /// Never write: verify the index against the corpus and refuse to measure
    /// if it does not match. For an index somebody else manages.
    #[arg(long)]
    pub no_index_build: bool,
}

impl Args {
    /// Checked before anything connects and long before anything is dropped: a
    /// run whose flags cannot produce a comparable matrix must cost a second,
    /// not an index build.
    pub fn validate(&self) -> Result<(), String> {
        if self.interface == Interface::VectorStore && self.fetch_documents {
            return Err(REFUSED_PROJECTION.to_string());
        }
        Ok(())
    }

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
            hosts: self.hosts.0.clone(),
            port: self.port,
            keyspace: self.keyspace.clone(),
            consistency: self.consistency,
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
            engine: SCYLLADB,
            interface: self.interface.name(),
            limit: self.limit,
            fetch_documents: self.fetch_documents,
        }
    }

    pub fn query_shape(&self) -> QueryShape {
        QueryShape {
            table: self.table.clone(),
            column: self.column.clone(),
            limit: self.limit,
            fetch_documents: self.fetch_documents,
        }
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

    pub fn reset_plan(&self) -> ResetPlan {
        ResetPlan {
            keyspace: self.keyspace.clone(),
            table: self.table.clone(),
            index: self.vs_index.clone(),
        }
    }

    /// What the loader names before it destroys anything.
    pub fn load_target(&self) -> String {
        format!(
            "{}.{} and its index {}",
            self.keyspace, self.table, self.vs_index
        )
    }

    pub fn settings(&self) -> Vec<(String, String)> {
        [
            ("interface", self.interface.name().to_string()),
            ("statement", self.statement.mode().name().to_string()),
            ("limit", self.limit.to_string()),
            ("fetch_documents", self.fetch_documents.to_string()),
            ("concurrency", self.concurrency.to_string()),
            (
                "query_classes",
                classes_setting(self.query_classes.as_ref()),
            ),
            ("queries", self.queries.display().to_string()),
            ("warmup_s", self.warmup.to_string()),
            ("duration_s", self.duration.to_string()),
            ("consistency", consistency_name(self.consistency)),
            ("request_timeout_s", self.request_timeout.to_string()),
            ("tokio_workers", self.tokio_workers().to_string()),
            ("corpus", self.corpus.display().to_string()),
            ("max_docs", self.max_docs.to_string()),
            ("latencies_dir", path_setting(self.latencies_dir.as_ref())),
            ("vs_index", self.vs_index.clone()),
            ("load_concurrency", self.load_concurrency.to_string()),
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

#[derive(Debug, Clone, PartialEq)]
pub struct Hosts(pub Vec<String>);

impl std::str::FromStr for Hosts {
    type Err = String;

    fn from_str(raw: &str) -> Result<Self, Self::Err> {
        let hosts: Vec<String> = search_latency_core::cli::split_fields(raw)
            .map(str::to_string)
            .collect();
        if hosts.is_empty() {
            return Err("--hosts needs at least one contact point".to_string());
        }
        Ok(Self(hosts))
    }
}

impl std::fmt::Display for Hosts {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0.join(","))
    }
}

fn parse_workers(raw: &str) -> Result<usize, String> {
    at_least_one(raw, "--tokio-workers")
}

/// The engine's own floor: M1 makes `LIMIT` mandatory and caps it at 1000, so a
/// run asking for more would fail on its first query rather than on its flags.
fn parse_limit(raw: &str) -> Result<usize, String> {
    let limit = at_least_one(raw, "--limit")?;
    if limit > MAX_LIMIT {
        return Err(format!("--limit must be <= {MAX_LIMIT}, got {limit}"));
    }
    Ok(limit)
}

pub const MAX_LIMIT: usize = 1000;

/// The BM25 endpoint returns primary keys and scores and cannot return text, so
/// a matrix where the CQL arm projected title and body while this one returned
/// identities would put the cost of that fetch on the chart as an engine
/// property.
pub const REFUSED_PROJECTION: &str =
    "--fetch-documents cannot be honoured by --interface vector-store: the BM25 \
endpoint returns primary keys only, and a matrix measured this way is not \
comparable with the other interfaces";

fn parse_load_concurrency(raw: &str) -> Result<usize, String> {
    at_least_one(raw, "--load-concurrency")
}

fn parse_duration(raw: &str) -> Result<f64, String> {
    positive_seconds(raw, "--duration")
}

fn parse_warmup(raw: &str) -> Result<f64, String> {
    non_negative_seconds(raw, "--warmup")
}

fn parse_consistency(raw: &str) -> Result<Consistency, String> {
    consistency_from_name(raw).map_err(|exc| exc.to_string())
}

#[cfg(test)]
#[path = "cli_tests.rs"]
mod tests;
