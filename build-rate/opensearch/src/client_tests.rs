use serde_json::json;

use super::*;
use crate::fakes::a_cluster;

fn some_options(url: &str) -> ConnectOptions {
    ConnectOptions {
        url: url.to_string(),
        index: "wiki-articles".to_string(),
        request_timeout: Duration::from_secs(120),
    }
}

fn settings_of(index_settings: Value) -> Value {
    index_subtree(
        Ok(json!({"wiki-articles": {"settings": index_settings}})),
        "settings",
    )
}

fn mappings_of(mappings: Value) -> Value {
    index_subtree(Ok(json!({"wiki-articles": {"mappings": mappings}})), "mappings")
}

fn parity_settings() -> Value {
    json!({"index": {
        "number_of_shards": "1",
        "number_of_replicas": "0",
        "refresh_interval": "30s",
    }})
}

#[test]
fn the_cluster_gathers_what_the_csv_header_needs() {
    let keys: Vec<String> = a_cluster().facts().into_iter().map(|(key, _)| key).collect();
    assert_eq!(
        keys,
        [
            "opensearch_version",
            "distribution",
            "client",
            "http_client",
            "runtime",
            "index",
            "index_shards",
            "replicas",
            "refresh_interval",
            "source_enabled",
            "body_analyzer",
            "write_pool",
            "connection_pool"
        ]
    );
}

#[test]
fn the_cluster_records_the_tokio_worker_count_alongside_the_engine() {
    let facts = a_cluster().facts();
    let runtime = facts.iter().find(|(key, _)| key == "runtime").unwrap();
    assert!(runtime.1.contains("workers:8"));
}

/// Both versions are read from the lock file rather than restated by hand,
/// because the header claims them as facts about what was linked.
#[test]
fn the_client_versions_are_read_from_the_lock_file_not_guessed() {
    assert_ne!(CLIENT_VERSION, UNKNOWN);
    assert_ne!(HTTP_CLIENT_VERSION, UNKNOWN);
    assert!(CLIENT_VERSION.starts_with("2."));
}

#[test]
fn an_endpoint_url_is_parsed_before_a_client_is_built() {
    assert!(endpoint("http://localhost:9200").is_ok());
    assert!(endpoint("https://search.internal:9200").is_ok());
    let failure = format!("{:#}", endpoint("not a url").unwrap_err());
    assert!(failure.contains("not a URL"));
}

/// `Url::parse` accepts this: it reads `localhost` as the scheme. Left
/// unchecked, a missing `http://` reaches the first `_bulk` as a puzzle.
#[test]
fn an_endpoint_without_a_scheme_is_rejected_with_the_flag_it_came_from() {
    let failure = format!("{:#}", endpoint("localhost:9200").unwrap_err());
    assert!(failure.contains("not an http(s) endpoint"));
    assert!(failure.contains("http://localhost:9200"));
}

#[test]
fn a_client_can_be_built_for_a_plain_http_endpoint() {
    assert!(build_client(&some_options("http://localhost:9200")).is_ok());
}

#[test]
fn a_client_cannot_be_built_for_an_endpoint_that_is_not_http() {
    assert!(build_client(&some_options("localhost:9200")).is_err());
    assert!(build_client(&some_options("not a url")).is_err());
}

#[test]
fn index_settings_are_read_from_whichever_concrete_index_answered() {
    let settings = settings_of(parity_settings());
    assert_eq!(index_setting(&settings, "number_of_shards"), "1");
    assert_eq!(index_setting(&settings, "number_of_replicas"), "0");
}

/// The campaign runs OpenSearch at 1s and at 30s, because 30s is a real
/// throughput tuning; a chart that does not say which is not interpretable.
#[test]
fn the_refresh_interval_is_read_because_it_is_a_throughput_tuning() {
    assert_eq!(refresh_interval(&settings_of(parity_settings())), "30s");
}

/// An index that never set one refreshes at OpenSearch's default. Saying
/// "unknown" would hide a known value; saying "1s" would claim a read.
#[test]
fn an_unset_refresh_interval_is_named_as_the_default_not_as_unknown() {
    let settings = settings_of(json!({"index": {"number_of_shards": "1"}}));
    assert_eq!(refresh_interval(&settings), IMPLICIT_REFRESH_INTERVAL);
    assert!(refresh_interval(&settings).contains("1s"));
}

#[test]
fn a_missing_setting_is_unknown_rather_than_invented() {
    assert_eq!(index_setting(&settings_of(json!({})), "number_of_shards"), UNKNOWN);
}

/// `_source` off is the ScyllaDB-parity variant, whose index carries no
/// document text at all. The two are not comparable, so the header says which.
#[test]
fn source_disabled_is_recorded_because_it_is_the_scylladb_parity_variant() {
    assert_eq!(
        source_enabled(&mappings_of(json!({"_source": {"enabled": false}}))),
        "false"
    );
}

#[test]
fn a_mapping_that_never_mentions_source_has_it_on() {
    assert_eq!(source_enabled(&mappings_of(json!({"properties": {}}))), "true");
}

/// Analyzer parity is load-bearing for every relevance and BM25 comparison in
/// the talk, so which analyzer indexed the body is recorded per run.
#[test]
fn the_body_analyzer_is_recorded() {
    let mappings = mappings_of(json!({"properties": {
        "body": {"type": "text", "analyzer": "m1_parity"},
    }}));
    assert_eq!(body_analyzer(&mappings), "m1_parity");
}

#[test]
fn a_body_field_with_no_analyzer_is_named_as_the_default() {
    let mappings = mappings_of(json!({"properties": {"body": {"type": "text"}}}));
    assert_eq!(body_analyzer(&mappings), DEFAULT_ANALYZER);
}

#[test]
fn the_write_pool_is_read_per_node() {
    let nodes = json!({"nodes": {
        "node-b": {"thread_pool": {"write": {"type": "fixed", "size": 8}}},
        "node-a": {"thread_pool": {"write": {"type": "fixed", "size": 8}}},
    }});
    assert_eq!(write_pool(Some(nodes)), "node-a=write:8;node-b=write:8");
}

#[test]
fn an_endpoint_that_does_not_answer_the_node_probe_reports_an_unknown_pool() {
    assert_eq!(write_pool(None), UNKNOWN);
    assert_eq!(write_pool(Some(json!({}))), UNKNOWN);
}

/// Index settings come back as strings and node stats as numbers, so both are
/// rendered rather than one of them being demanded.
#[test]
fn a_numeric_field_reads_as_readily_as_a_string_one() {
    let numeric = json!({"index": {"number_of_shards": 3}});
    assert_eq!(index_setting(&settings_of(numeric), "number_of_shards"), "3");
}

#[test]
fn a_read_that_failed_leaves_an_empty_subtree_rather_than_failing_the_run() {
    let subtree = index_subtree(Err(anyhow::anyhow!("connection refused")), "settings");
    assert_eq!(index_setting(&subtree, "number_of_shards"), UNKNOWN);
}

#[test]
fn a_version_probe_that_failed_reports_an_unknown_engine() {
    assert_eq!(version_field(&Value::default(), "number"), UNKNOWN);
}

#[test]
fn a_version_probe_carries_the_engine_and_which_distribution_it_is() {
    let version = json!({"version": {"number": "2.19.0", "distribution": "opensearch"}});
    assert_eq!(version_field(&version, "number"), "2.19.0");
    assert_eq!(version_field(&version, "distribution"), "opensearch");
}
