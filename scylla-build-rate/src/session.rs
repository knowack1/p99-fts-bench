//! The ScyllaDB half: a shard-aware session, a prepared INSERT, and the topology
//! facts every chart needs carried alongside it.
//!
//! Connection pooling is left at the driver's defaults on purpose. The Rust
//! driver opens one connection per shard by itself and learns the shard count
//! from the server's `SCYLLA_NR_SHARDS` supported option, so there is nothing
//! here to size by hand.
use std::net::SocketAddr;
use std::time::Duration;

use anyhow::{bail, Context, Result};
use scylla::client::execution_profile::ExecutionProfile;
use scylla::client::session::Session;
use scylla::client::session_builder::SessionBuilder;
use scylla::cluster::ClusterState;
use scylla::policies::load_balancing::DefaultPolicy;
use scylla::statement::prepared::PreparedStatement;
use scylla::statement::Consistency;

pub const INSERT_TEMPLATE: &str =
    "INSERT INTO {table} (article_id, page_id, title, body) VALUES (?, ?, ?, ?)";
pub const UNKNOWN: &str = "unknown";
pub const DRIVER_VERSION: &str = env!("SCYLLA_DRIVER_VERSION");
/// The Rust driver speaks one protocol version; `scylla-cql` pins it as
/// `DEFAULT_CQL_PROTOCOL_VERSION = "4.0.0"`. Asserted by a test rather than
/// trusted, because the header claims it as a measured fact.
pub const PROTOCOL_VERSION: &str = "4";

const CONSISTENCY_NAMES: [(&str, Consistency); 11] = [
    ("ANY", Consistency::Any),
    ("ONE", Consistency::One),
    ("TWO", Consistency::Two),
    ("THREE", Consistency::Three),
    ("QUORUM", Consistency::Quorum),
    ("ALL", Consistency::All),
    ("LOCAL_QUORUM", Consistency::LocalQuorum),
    ("EACH_QUORUM", Consistency::EachQuorum),
    ("SERIAL", Consistency::Serial),
    ("LOCAL_SERIAL", Consistency::LocalSerial),
    ("LOCAL_ONE", Consistency::LocalOne),
];

#[derive(Debug, Clone, PartialEq)]
pub struct Topology {
    pub scylla_version: String,
    pub routing: String,
    pub compression: String,
    pub driver_version: String,
    pub protocol_version: String,
    pub runtime: String,
    pub shard_aware: String,
    pub shards: String,
    pub connections: String,
    pub tablets: String,
}

impl Topology {
    pub fn facts(&self) -> Vec<(String, String)> {
        [
            ("scylla_version", &self.scylla_version),
            ("routing", &self.routing),
            ("compression", &self.compression),
            ("driver", &self.driver_version),
            ("protocol", &self.protocol_version),
            ("runtime", &self.runtime),
            ("shard_aware", &self.shard_aware),
            ("shards", &self.shards),
            ("connections", &self.connections),
            ("tablets", &self.tablets),
        ]
        .into_iter()
        .map(|(key, value)| (key.to_string(), value.clone()))
        .collect()
    }
}

#[derive(Debug, Clone)]
pub struct ConnectOptions {
    pub hosts: Vec<String>,
    pub port: u16,
    pub keyspace: String,
    pub consistency: Consistency,
    pub request_timeout: Duration,
    pub write_coalescing: bool,
}

pub fn consistency_from_name(name: &str) -> Result<Consistency> {
    let wanted = name.to_uppercase();
    CONSISTENCY_NAMES
        .iter()
        .find(|(known, _)| *known == wanted)
        .map(|(_, level)| *level)
        .with_context(|| format!("unknown consistency level: {name}"))
}

pub fn consistency_name(level: Consistency) -> String {
    CONSISTENCY_NAMES
        .iter()
        .find(|(_, known)| *known == level)
        .map_or_else(|| UNKNOWN.to_string(), |(name, _)| name.to_string())
}

/// Routing and compression are set explicitly, not left to the defaults. A
/// prepared statement carries a routing key, so token awareness is what turns it
/// into a shard-local write; compression is off because a codec that happens to
/// be compiled in would otherwise silently change the measured rate.
pub async fn connect(options: &ConnectOptions) -> Result<Session> {
    let session = SessionBuilder::new()
        .known_nodes(contact_points(options))
        .compression(None)
        .write_coalescing(options.write_coalescing)
        .default_execution_profile_handle(execution_profile(options).into_handle())
        .build()
        .await
        .with_context(|| format!("cannot reach {}", contact_points(options).join(",")))?;
    use_keyspace(&session, &options.keyspace).await?;
    Ok(session)
}

fn execution_profile(options: &ConnectOptions) -> ExecutionProfile {
    ExecutionProfile::builder()
        .load_balancing_policy(DefaultPolicy::builder().token_aware(true).build())
        .consistency(options.consistency)
        .request_timeout(Some(options.request_timeout))
        .build()
}

fn contact_points(options: &ConnectOptions) -> Vec<String> {
    options
        .hosts
        .iter()
        .map(|host| format!("{host}:{}", options.port))
        .collect()
}

async fn use_keyspace(session: &Session, keyspace: &str) -> Result<()> {
    if let Err(exc) = session.use_keyspace(keyspace, false).await {
        bail!("cannot use keyspace {keyspace:?}: {exc}\napply bench/scylladb/schema.cql first");
    }
    Ok(())
}

pub async fn prepare_insert(session: &Session, table: &str) -> Result<PreparedStatement> {
    session
        .prepare(insert_statement(table))
        .await
        .with_context(|| format!("cannot prepare an INSERT into {table}"))
}

pub fn insert_statement(table: &str) -> String {
    INSERT_TEMPLATE.replace("{table}", table)
}

pub async fn read_topology(
    session: &Session,
    keyspace: &str,
    tokio_workers: usize,
) -> Result<Topology> {
    let state = session.get_cluster_state();
    Ok(Topology {
        scylla_version: scylla_version(session).await,
        routing: "DefaultPolicy(token_aware)".to_string(),
        compression: "None".to_string(),
        driver_version: DRIVER_VERSION.to_string(),
        protocol_version: PROTOCOL_VERSION.to_string(),
        runtime: format!("tokio multi_thread workers:{tokio_workers}"),
        shard_aware: is_shard_aware(&state).to_string(),
        shards: format_shard_stats(&state),
        connections: total_connections(session),
        tablets: keyspace_tablet_state(&state, keyspace),
    })
}

async fn scylla_version(session: &Session) -> String {
    match query_release_version(session).await {
        Ok(Some(version)) => version,
        _ => UNKNOWN.to_string(),
    }
}

async fn query_release_version(session: &Session) -> Result<Option<String>> {
    let rows = session
        .query_unpaged("SELECT release_version FROM system.local", &[])
        .await?
        .into_rows_result()?;
    Ok(rows.rows::<(String,)>()?.next().transpose()?.map(|r| r.0))
}

fn is_shard_aware(state: &ClusterState) -> bool {
    state
        .get_nodes_info()
        .iter()
        .any(|node| node.sharder().is_some())
}

fn format_shard_stats(state: &ClusterState) -> String {
    let mut endpoints: Vec<String> = state
        .get_nodes_info()
        .iter()
        .map(|node| format!("{}={}", node.address, shard_count(node.sharder())))
        .collect();
    if endpoints.is_empty() {
        return "none".to_string();
    }
    endpoints.sort();
    endpoints.join(";")
}

fn shard_count(sharder: Option<scylla::routing::Sharder>) -> String {
    sharder.map_or_else(
        || "shards:none".to_string(),
        |sharder| format!("shards:{}", sharder.nr_shards.get()),
    )
}

#[cfg(feature = "driver-metrics")]
fn total_connections(session: &Session) -> String {
    session.get_metrics().get_total_connections().to_string()
}

#[cfg(not(feature = "driver-metrics"))]
fn total_connections(_session: &Session) -> String {
    UNKNOWN.to_string()
}

/// The Rust driver reports tablet use per keyspace, not per table: a
/// tablet-based keyspace is what decides whether `wiki.articles` is on tablets.
fn keyspace_tablet_state(state: &ClusterState, keyspace: &str) -> String {
    state
        .get_keyspace(keyspace)
        .map_or_else(|| UNKNOWN.to_string(), |ks| ks.tablet_based.to_string())
}

pub fn socket_addr(host: &str, port: u16) -> Option<SocketAddr> {
    format!("{host}:{port}").parse().ok()
}

#[cfg(test)]
#[path = "session_tests.rs"]
mod tests;
