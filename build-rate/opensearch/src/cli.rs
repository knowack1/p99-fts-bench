//! Command line: a corpus, a list of concurrency levels, a batch size, and
//! where the CSV goes.
use std::path::PathBuf;
use std::time::Duration;

pub use build_rate_core::cli::{available_cores, split_fields, Levels};

use clap::Parser;

use crate::build_rate::WatchTiming;
use crate::client::ConnectOptions;
use crate::report::LATENCY_UNIT;
use crate::reset::{GateTiming, IndexConfig, DEFAULT_INDEX_CONFIG};
use crate::sweep::QUEUE_DEPTH_PER_WORKER;

pub const DEFAULT_URL: &str = "http://localhost:9200";
pub const DEFAULT_INDEX: &str = "wiki-articles";
/// The campaign's `OS_BATCH` (tools/loader_capability_sweep.sh), so a ladder
/// run here without the flag is the ladder the Python loader was run at.
pub const DEFAULT_BATCH_SIZE: &str = "512";
/// `ftsbench.opensearch_load.BULK_TIMEOUT_S`. A 512-document bulk into a
/// saturated engine takes a long time to answer, and a shorter timeout would
/// report the client giving up as the engine failing.
pub const DEFAULT_REQUEST_TIMEOUT_S: &str = "120.0";
/// `scyllarate`'s `--reset-timeout`. A gate that waits this long and still has
/// not seen the index it asked for is describing a broken endpoint, not a slow
/// one.
pub const DEFAULT_RESET_TIMEOUT_S: &str = "300.0";
pub const RESET_POLL_INTERVAL_S: f64 = 0.5;
/// The same cadence the ScyllaDB half polls at, on purpose: the growth chart
/// puts both engines on one grid and the grids only line up if the readings do.
pub const DEFAULT_INDEX_INTERVAL_S: &str = "1.0";
/// Longer than the ScyllaDB half's 120s, because this tail can carry a refresh
/// and a merge burst, and at `refresh_interval: 30s` a level can legitimately
/// spend half a minute with nothing to show.
pub const DEFAULT_INDEX_SETTLE_TIMEOUT_S: &str = "180.0";
/// Longer than the ScyllaDB half's 10s, for a bulk still sitting in the write
/// queue plus a poll of jitter. It does not need to scale with
/// `refresh_interval`, because idleness is measured on what the engine has
/// accepted rather than on what is searchable.
pub const DEFAULT_INDEX_IDLE_TIMEOUT_S: &str = "15.0";
pub const STDOUT: &str = "-";

#[derive(Debug, Parser)]
#[command(
    name = "osrate",
    about = "Concurrency sweep for OpenSearch ingest: docs/s and p99 per concurrency level",
    long_about = "Measures how fast this client can submit _bulk requests to OpenSearch, per \
concurrency level. That is a submit rate, not a searchable-index rate: a bulk OpenSearch has \
acknowledged is in the translog and the in-memory buffer, and is not visible to search until a \
refresh.\n\n\
DESTRUCTIVE BY DEFAULT: before every concurrency level this DELETES THE INDEX and creates it \
again from the embedded mapping, so that each level builds from zero documents. Pass --no-reset \
to leave the index alone."
)]
pub struct Args {
    /// Corpus JSONL: one {id, title, text} per line
    #[arg(long)]
    pub corpus: PathBuf,

    /// Comma-separated levels of in-flight _bulk requests, e.g. 24,48,96
    #[arg(long)]
    pub concurrency: Levels,

    /// Documents per _bulk request
    #[arg(long, default_value = DEFAULT_BATCH_SIZE, value_parser = parse_batch_size)]
    pub batch_size: usize,

    /// Documents per point; 0 loads the whole corpus
    #[arg(long, default_value_t = 0)]
    pub max_docs: usize,

    #[arg(long, env = "OS_URL", default_value = DEFAULT_URL)]
    pub url: String,

    #[arg(long, default_value = DEFAULT_INDEX)]
    pub index: String,

    #[arg(long, default_value = DEFAULT_REQUEST_TIMEOUT_S)]
    pub request_timeout: f64,

    /// Batches buffered per worker ahead of the sweep; raises memory by
    /// queue_depth * concurrency * batch_size documents
    #[arg(long, default_value_t = QUEUE_DEPTH_PER_WORKER)]
    pub queue_depth: usize,

    /// Tokio worker threads; defaults to every core the machine reports
    #[arg(long)]
    pub tokio_workers: Option<usize>,

    /// CSV destination; '-' writes to stdout
    #[arg(long, default_value = STDOUT)]
    pub out: String,

    /// Mapping the index is created from: 'ramindex', 'disk', or a path to a
    /// JSON file
    #[arg(long, default_value = DEFAULT_INDEX_CONFIG)]
    pub index_config: String,

    /// refresh_interval to create the index with; unset keeps the config's own
    #[arg(long, env = "OS_REFRESH_INTERVAL")]
    pub refresh_interval: Option<String>,

    /// Seconds each reset gate may wait for the endpoint
    #[arg(long, default_value = DEFAULT_RESET_TIMEOUT_S)]
    pub reset_timeout: f64,

    /// Keep the index: do not delete and recreate it before each level. Levels
    /// after the first then overwrite the same documents, so only the first
    /// measures a build and the rest measure Lucene's update path.
    #[arg(long)]
    pub no_reset: bool,

    /// Do not check that the index analyzes text the way the vector-store does.
    #[arg(long)]
    pub no_analyzer_check: bool,

    /// Also measure how fast documents become searchable, not just how fast
    /// this client delivered them. Off by default: it adds a `_stats` poll per
    /// second and a settle wait per level, and the recorded submit-rate arms
    /// must keep measuring what they measured.
    #[arg(long)]
    pub index_watch: bool,

    /// Per-second series, one CSV per level. Keep it out of the directory the
    /// point CSVs go in.
    #[arg(long)]
    pub samples_dir: Option<PathBuf>,

    /// Seconds between index polls while a level runs
    #[arg(long, default_value = DEFAULT_INDEX_INTERVAL_S)]
    pub index_interval: f64,

    /// Seconds to keep waiting for the index after the last document is sent
    #[arg(long, default_value = DEFAULT_INDEX_SETTLE_TIMEOUT_S)]
    pub index_settle_timeout: f64,

    /// Seconds of no indexing progress that end the wait. Measured on what the
    /// engine has accepted, not on what is searchable: a searchable count sits
    /// still between refreshes, and ending a build there would call an ordinary
    /// pause a finished one.
    #[arg(long, default_value = DEFAULT_INDEX_IDLE_TIMEOUT_S)]
    pub index_idle_timeout: f64,

    /// Do not ask the index to publish what it is holding when it has accepted
    /// everything and stopped. The level then reports what the configured
    /// refresh policy actually delivered — which at `refresh_interval: -1` is
    /// nothing.
    #[arg(long)]
    pub no_index_final_refresh: bool,
}

impl Args {
    pub fn tokio_workers(&self) -> usize {
        self.tokio_workers.unwrap_or_else(available_cores)
    }

    pub fn connect_options(&self) -> ConnectOptions {
        ConnectOptions {
            url: self.url.trim_end_matches('/').to_string(),
            index: self.index.clone(),
            request_timeout: Duration::from_secs_f64(self.request_timeout),
        }
    }

    pub fn resets(&self) -> bool {
        !self.no_reset
    }

    pub fn watches_index(&self) -> bool {
        self.index_watch
    }

    pub fn asks_for_a_final_refresh(&self) -> bool {
        !self.no_index_final_refresh
    }

    pub fn watch_timing(&self) -> WatchTiming {
        WatchTiming {
            poll_interval: Duration::from_secs_f64(self.index_interval),
            settle_timeout: Duration::from_secs_f64(self.index_settle_timeout),
            idle_timeout: Duration::from_secs_f64(self.index_idle_timeout),
        }
    }

    /// Only when the index is one this run built: a `--no-reset` run loads into
    /// whatever was there, and an analyzer it did not choose is the operator's
    /// to vouch for.
    pub fn checks_analyzer(&self) -> bool {
        self.resets() && !self.no_analyzer_check
    }

    pub fn index_config(&self) -> Result<IndexConfig, anyhow::Error> {
        IndexConfig::select(&self.index_config)?
            .with_refresh_interval(self.refresh_interval.as_deref())
    }

    fn refresh_interval_setting(&self) -> String {
        self.refresh_interval
            .clone()
            .unwrap_or_else(|| REFRESH_INTERVAL_FROM_CONFIG.to_string())
    }

    pub fn gate_timing(&self) -> GateTiming {
        GateTiming {
            poll_interval: Duration::from_secs_f64(RESET_POLL_INTERVAL_S),
            timeout: Duration::from_secs_f64(self.reset_timeout),
        }
    }

    pub fn settings(&self) -> Vec<(String, String)> {
        [
            ("batch_size", self.batch_size.to_string()),
            ("latency_unit", LATENCY_UNIT.to_string()),
            ("request_timeout_s", self.request_timeout.to_string()),
            ("queue_depth", self.queue_depth.to_string()),
            ("tls", tls_state().to_string()),
            ("tokio_workers", self.tokio_workers().to_string()),
            ("corpus", self.corpus.display().to_string()),
            ("max_docs", self.max_docs.to_string()),
            ("reset_per_level", self.resets().to_string()),
            ("index_config", self.index_config.clone()),
            (
                "refresh_interval_requested",
                self.refresh_interval_setting(),
            ),
            ("reset_timeout_s", self.reset_timeout.to_string()),
            ("analyzer_check", self.checks_analyzer().to_string()),
        ]
        .into_iter()
        .map(|(key, value)| (key.to_string(), value))
        .collect()
    }
}

/// What was *asked* for, not what the index ended up with: the cluster read
/// reports the interval the endpoint confirms, and the two disagreeing is
/// something the header should be able to show.
pub const REFRESH_INTERVAL_FROM_CONFIG: &str = "from-index-config";

pub fn tls_state() -> &'static str {
    if cfg!(feature = "tls") {
        "rustls"
    } else {
        "off"
    }
}

fn parse_batch_size(raw: &str) -> Result<usize, String> {
    let size: usize = raw
        .parse()
        .map_err(|_| format!("not an integer: {raw:?}"))?;
    if size < 1 {
        return Err(format!("--batch-size must be >= 1, got {size}"));
    }
    Ok(size)
}

#[cfg(test)]
#[path = "cli_tests.rs"]
mod tests;
