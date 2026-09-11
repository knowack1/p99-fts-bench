//! The OpenSearch half: a client, a check that the index is there, and the
//! cluster facts every chart needs carried alongside the numbers.
//!
//! Connection pooling is left at the HTTP client's defaults on purpose. reqwest
//! keeps an unbounded idle pool per host, so N bulks in flight get N sockets by
//! themselves and `--concurrency` is what the engine is actually being asked at
//! once — there is nothing here to size by hand.
use std::time::Duration;

use anyhow::{bail, Context, Result};
use opensearch::http::transport::{SingleNodeConnectionPool, TransportBuilder};
use opensearch::indices::{
    IndicesAnalyzeParts, IndicesExistsParts, IndicesGetMappingParts, IndicesGetSettingsParts,
};
use opensearch::nodes::NodesInfoParts;
use opensearch::{CountParts, OpenSearch};
use serde_json::{json, Value};
use url::Url;

pub const UNKNOWN: &str = "unknown";
pub const CLIENT_VERSION: &str = env!("OPENSEARCH_CLIENT_VERSION");
pub const HTTP_CLIENT_VERSION: &str = env!("HTTP_CLIENT_VERSION");
pub const BODY_FIELD: &str = "body";
/// What OpenSearch refreshes at when the index never said. Reported as a
/// default rather than as a read value, because nothing was read.
pub const IMPLICIT_REFRESH_INTERVAL: &str = "unset(default 1s)";
pub const DEFAULT_ANALYZER: &str = "unset(default standard)";
pub const CONNECTION_POOL: &str = "reqwest-default(idle unbounded)";
pub const ENDPOINT_SCHEMES: [&str; 2] = ["http", "https"];

#[derive(Debug, Clone, PartialEq)]
pub struct Cluster {
    pub opensearch_version: String,
    pub distribution: String,
    pub client_version: String,
    pub http_client_version: String,
    pub runtime: String,
    pub index: String,
    pub index_shards: String,
    pub replicas: String,
    pub refresh_interval: String,
    pub source_enabled: String,
    pub body_analyzer: String,
    pub write_pool: String,
    pub connection_pool: String,
}

impl Cluster {
    pub fn facts(&self) -> Vec<(String, String)> {
        [
            ("opensearch_version", &self.opensearch_version),
            ("distribution", &self.distribution),
            ("client", &self.client_version),
            ("http_client", &self.http_client_version),
            ("runtime", &self.runtime),
            ("index", &self.index),
            ("index_shards", &self.index_shards),
            ("replicas", &self.replicas),
            ("refresh_interval", &self.refresh_interval),
            ("source_enabled", &self.source_enabled),
            ("body_analyzer", &self.body_analyzer),
            ("write_pool", &self.write_pool),
            ("connection_pool", &self.connection_pool),
        ]
        .into_iter()
        .map(|(key, value)| (key.to_string(), value.clone()))
        .collect()
    }
}

#[derive(Debug, Clone)]
pub struct ConnectOptions {
    pub url: String,
    pub index: String,
    pub request_timeout: Duration,
}

/// The proxy is disabled explicitly, not left to the environment: an ambient
/// `HTTP_PROXY` would otherwise route every `_bulk` through a third party and
/// silently change the measured rate.
///
/// Whether the index has to be there already is not decided here: a run that
/// resets creates it, and only `--no-reset` requires one it did not build. The
/// caller makes that choice with `require_index`.
pub async fn connect(options: &ConnectOptions) -> Result<OpenSearch> {
    let client = build_client(options)?;
    reach(&client, &options.url).await?;
    Ok(client)
}

pub fn build_client(options: &ConnectOptions) -> Result<OpenSearch> {
    let transport = TransportBuilder::new(SingleNodeConnectionPool::new(endpoint(&options.url)?))
        .timeout(options.request_timeout)
        .disable_proxy()
        .build()
        .with_context(|| format!("cannot build a client for {}", options.url))?;
    Ok(OpenSearch::new(transport))
}

/// The scheme is checked, not just the parse. `Url::parse("localhost:9200")`
/// succeeds — it reads `localhost` as the scheme and `9200` as the path — so a
/// missing `http://` would otherwise reach the first `_bulk` as a puzzle rather
/// than as a message about the flag.
pub fn endpoint(url: &str) -> Result<Url> {
    let parsed = Url::parse(url).with_context(|| format!("not a URL: {url}"))?;
    if !ENDPOINT_SCHEMES.contains(&parsed.scheme()) {
        bail!(
            "{url} is not an http(s) endpoint: its scheme is {:?}\ngive --url a scheme, e.g. http://{url}",
            parsed.scheme()
        );
    }
    Ok(parsed)
}

async fn reach(client: &OpenSearch, url: &str) -> Result<()> {
    client
        .info()
        .send()
        .await
        .with_context(|| format!("cannot reach {url}"))?
        .error_for_status_code()
        .with_context(|| format!("{url} answered the version probe with an error"))?;
    Ok(())
}

/// Only `--no-reset` needs this: it is the mode that loads into an index this
/// tool did not build.
pub async fn require_index(client: &OpenSearch, index: &str) -> Result<()> {
    if index_exists(client, index).await? {
        return Ok(());
    }
    bail!(
        "index {index:?} does not exist\ndrop --no-reset to let osrate create it, or apply \
         bench/opensearch/create_index.sh first"
    );
}

/// `HEAD /{index}`. A 404 is an answer, not a failure — the gates poll this
/// waiting for exactly that.
pub async fn index_exists(client: &OpenSearch, index: &str) -> Result<bool> {
    let response = client
        .indices()
        .exists(IndicesExistsParts::Index(&[index]))
        .send()
        .await
        .with_context(|| format!("cannot ask whether index {index:?} exists"))?;
    Ok(response.status_code().is_success())
}

/// `GET /{index}/_count`. This doubles as the readiness check: an index whose
/// primary is not allocated yet answers 503 rather than 0, which is the
/// keep-polling case rather than a count of nothing.
pub async fn document_count(client: &OpenSearch, index: &str) -> Result<u64> {
    let body: Value = json_of(
        client
            .count(CountParts::Index(&[index]))
            .send()
            .await
            .with_context(|| format!("cannot count the documents in index {index:?}"))?,
    )
    .await?;
    body.get("count")
        .and_then(Value::as_u64)
        .with_context(|| format!("the _count reply for index {index:?} carried no count"))
}

/// `POST /{index}/_analyze`, rendered as the `position:token` stream
/// `opensearch/verify_analyzer.sh` compares against.
pub async fn analyze(
    client: &OpenSearch,
    index: &str,
    analyzer: &str,
    text: &str,
) -> Result<String> {
    let body: Value = json_of(
        client
            .indices()
            .analyze(IndicesAnalyzeParts::Index(index))
            .body(json!({"analyzer": analyzer, "text": text}))
            .send()
            .await
            .with_context(|| format!("cannot analyze text with {analyzer:?} on index {index:?}"))?,
    )
    .await?;
    Ok(token_stream(&body))
}

fn token_stream(analyzed: &Value) -> String {
    analyzed
        .get("tokens")
        .and_then(Value::as_array)
        .map(|tokens| tokens.iter().map(one_token).collect::<Vec<_>>().join(" "))
        .unwrap_or_default()
}

fn one_token(token: &Value) -> String {
    let position = token
        .get("position")
        .map_or(0, |at| at.as_u64().unwrap_or(0));
    let text = token
        .get("token")
        .and_then(Value::as_str)
        .unwrap_or_default();
    format!("{position}:{text}")
}

/// Every read falls back to `unknown` rather than failing the run: an endpoint
/// that answers `_bulk` but not `_settings` — the repo's null sink is one — is
/// still worth measuring, as long as the header does not claim facts nobody
/// read.
pub async fn read_cluster(
    client: &OpenSearch,
    index: &str,
    tokio_workers: usize,
) -> Result<Cluster> {
    let version = json_or_none(engine_version(client).await).unwrap_or_default();
    let settings = index_subtree(client_settings(client, index).await, "settings");
    let mappings = index_subtree(client_mappings(client, index).await, "mappings");
    Ok(Cluster {
        opensearch_version: version_field(&version, "number"),
        distribution: version_field(&version, "distribution"),
        client_version: CLIENT_VERSION.to_string(),
        http_client_version: HTTP_CLIENT_VERSION.to_string(),
        runtime: format!("tokio multi_thread workers:{tokio_workers}"),
        index: index.to_string(),
        index_shards: index_setting(&settings, "number_of_shards"),
        replicas: index_setting(&settings, "number_of_replicas"),
        refresh_interval: refresh_interval(&settings),
        source_enabled: source_enabled(&mappings),
        body_analyzer: body_analyzer(&mappings),
        write_pool: write_pool(json_or_none(node_thread_pools(client).await)),
        connection_pool: CONNECTION_POOL.to_string(),
    })
}

async fn engine_version(client: &OpenSearch) -> Result<Value> {
    json_of(client.info().send().await?).await
}

async fn client_settings(client: &OpenSearch, index: &str) -> Result<Value> {
    json_of(
        client
            .indices()
            .get_settings(IndicesGetSettingsParts::Index(&[index]))
            .send()
            .await?,
    )
    .await
}

async fn client_mappings(client: &OpenSearch, index: &str) -> Result<Value> {
    json_of(
        client
            .indices()
            .get_mapping(IndicesGetMappingParts::Index(&[index]))
            .send()
            .await?,
    )
    .await
}

async fn node_thread_pools(client: &OpenSearch) -> Result<Value> {
    json_of(
        client
            .nodes()
            .info(NodesInfoParts::Metric(&["thread_pool"]))
            .send()
            .await?,
    )
    .await
}

async fn json_of(response: opensearch::http::response::Response) -> Result<Value> {
    Ok(response.error_for_status_code()?.json::<Value>().await?)
}

fn json_or_none(read: Result<Value>) -> Option<Value> {
    read.ok()
}

/// Both `_settings` and `_mapping` answer keyed by the *concrete* index, which
/// is not the name that was asked for when that name was an alias, so the one
/// entry is taken rather than looked up.
fn index_subtree(read: Result<Value>, key: &str) -> Value {
    json_or_none(read)
        .as_ref()
        .and_then(Value::as_object)
        .and_then(|indices| indices.values().next())
        .and_then(|index| index.get(key))
        .cloned()
        .unwrap_or_default()
}

fn version_field(version: &Value, field: &str) -> String {
    text_at(version, &["version", field]).unwrap_or_else(|| UNKNOWN.to_string())
}

fn index_setting(settings: &Value, name: &str) -> String {
    text_at(settings, &["index", name]).unwrap_or_else(|| UNKNOWN.to_string())
}

fn refresh_interval(settings: &Value) -> String {
    text_at(settings, &["index", "refresh_interval"])
        .unwrap_or_else(|| IMPLICIT_REFRESH_INTERVAL.to_string())
}

/// `_source` off is the ScyllaDB-parity variant: the index then carries no
/// document text, matching Tantivy's schema. A chart cannot be compared across
/// the two, so the header has to say which it was.
fn source_enabled(mappings: &Value) -> String {
    text_at(mappings, &["_source", "enabled"]).unwrap_or_else(|| true.to_string())
}

fn body_analyzer(mappings: &Value) -> String {
    text_at(mappings, &["properties", BODY_FIELD, "analyzer"])
        .unwrap_or_else(|| DEFAULT_ANALYZER.to_string())
}

fn write_pool(nodes: Option<Value>) -> String {
    let mut sizes: Vec<String> = nodes
        .as_ref()
        .and_then(|nodes| nodes.get("nodes"))
        .and_then(Value::as_object)
        .map(|nodes| nodes.iter().map(|(id, node)| pool_size(id, node)).collect())
        .unwrap_or_default();
    if sizes.is_empty() {
        return UNKNOWN.to_string();
    }
    sizes.sort();
    sizes.join(";")
}

fn pool_size(node_id: &str, node: &Value) -> String {
    let size = text_at(node, &["thread_pool", "write", "size"])
        .unwrap_or_else(|| UNKNOWN.to_string());
    format!("{node_id}=write:{size}")
}

/// Settings come back as strings and stats as numbers, so both are rendered
/// rather than one of them being demanded.
fn text_at(root: &Value, path: &[&str]) -> Option<String> {
    let mut cursor = root;
    for step in path {
        cursor = cursor.get(step)?;
    }
    match cursor {
        Value::String(text) => Some(text.clone()),
        Value::Null => None,
        other => Some(other.to_string()),
    }
}

#[cfg(test)]
#[path = "client_tests.rs"]
mod tests;
