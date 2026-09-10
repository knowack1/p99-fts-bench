//! Command line: a corpus, a list of concurrency levels, and where the CSV goes.
use std::fmt;
use std::path::PathBuf;
use std::str::FromStr;
use std::time::Duration;

use clap::Parser;
use scylla::statement::Consistency;

use crate::session::{consistency_from_name, consistency_name, ConnectOptions};

pub const DEFAULT_HOSTS: &str = "127.0.0.1";
pub const DEFAULT_PORT: &str = "9042";
pub const DEFAULT_KEYSPACE: &str = "wiki";
pub const DEFAULT_TABLE: &str = "articles";
pub const DEFAULT_CONSISTENCY: &str = "LOCAL_ONE";
pub const DEFAULT_REQUEST_TIMEOUT_S: &str = "10.0";
pub const STDOUT: &str = "-";

#[derive(Debug, Parser)]
#[command(
    name = "scyllarate",
    about = "Concurrency sweep for ScyllaDB ingest: docs/s and p99 per concurrency level",
    long_about = "Measures how fast this client can submit prepared INSERTs to ScyllaDB, per \
concurrency level. That is a submit rate, not an FTS index build rate: a completed CQL write \
says nothing about how many documents reached the index."
)]
pub struct Args {
    /// Corpus JSONL: one {id, uuid, title, text} per line
    #[arg(long)]
    pub corpus: PathBuf,

    /// Comma-separated levels, e.g. 8,16,32,64,128
    #[arg(long)]
    pub concurrency: Levels,

    /// Documents per point; 0 loads the whole corpus
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

    #[arg(long, value_parser = parse_consistency, default_value = DEFAULT_CONSISTENCY)]
    pub consistency: Consistency,

    #[arg(long, default_value = DEFAULT_REQUEST_TIMEOUT_S)]
    pub request_timeout: f64,

    /// Tokio worker threads; defaults to every core the machine reports
    #[arg(long)]
    pub tokio_workers: Option<usize>,

    /// CSV destination; '-' writes to stdout
    #[arg(long, default_value = STDOUT)]
    pub out: String,
}

impl Args {
    pub fn tokio_workers(&self) -> usize {
        self.tokio_workers.unwrap_or_else(available_cores)
    }

    pub fn connect_options(&self) -> ConnectOptions {
        ConnectOptions {
            hosts: self.hosts.0.clone(),
            port: self.port,
            keyspace: self.keyspace.clone(),
            consistency: self.consistency,
            request_timeout: Duration::from_secs_f64(self.request_timeout),
        }
    }

    pub fn settings(&self) -> Vec<(String, String)> {
        [
            ("consistency", consistency_name(self.consistency)),
            ("request_timeout_s", self.request_timeout.to_string()),
            ("tokio_workers", self.tokio_workers().to_string()),
            ("driver_metrics", driver_metrics_state().to_string()),
            ("corpus", self.corpus.display().to_string()),
            ("max_docs", self.max_docs.to_string()),
        ]
        .into_iter()
        .map(|(key, value)| (key.to_string(), value))
        .collect()
    }
}

pub fn available_cores() -> usize {
    std::thread::available_parallelism().map_or(1, |cores| cores.get())
}

pub fn driver_metrics_state() -> &'static str {
    if cfg!(feature = "driver-metrics") {
        "on"
    } else {
        "off"
    }
}

/// Repeats are kept: a throwaway leading level absorbs the cold page cache, and
/// dropping it silently would make the ladder disagree with what was asked for.
#[derive(Debug, Clone, PartialEq)]
pub struct Levels(pub Vec<usize>);

impl FromStr for Levels {
    type Err = String;

    fn from_str(raw: &str) -> Result<Self, Self::Err> {
        let levels = split_fields(raw)
            .map(parse_level)
            .collect::<Result<Vec<_>, _>>()?;
        if levels.is_empty() {
            return Err("--concurrency needs at least one level".to_string());
        }
        Ok(Self(levels))
    }
}

fn parse_level(field: &str) -> Result<usize, String> {
    let level: usize = field
        .parse()
        .map_err(|_| format!("not an integer: {field:?}"))?;
    if level < 1 {
        return Err(format!("concurrency must be >= 1, got {level}"));
    }
    Ok(level)
}

#[derive(Debug, Clone, PartialEq)]
pub struct Hosts(pub Vec<String>);

impl FromStr for Hosts {
    type Err = String;

    fn from_str(raw: &str) -> Result<Self, Self::Err> {
        let hosts: Vec<String> = split_fields(raw).map(str::to_string).collect();
        if hosts.is_empty() {
            return Err("--hosts needs at least one contact point".to_string());
        }
        Ok(Self(hosts))
    }
}

impl fmt::Display for Hosts {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0.join(","))
    }
}

fn split_fields(raw: &str) -> impl Iterator<Item = &str> {
    raw.split(',')
        .map(str::trim)
        .filter(|field| !field.is_empty())
}

fn parse_consistency(raw: &str) -> Result<Consistency, String> {
    consistency_from_name(raw).map_err(|exc| exc.to_string())
}

#[cfg(test)]
#[path = "cli_tests.rs"]
mod tests;
