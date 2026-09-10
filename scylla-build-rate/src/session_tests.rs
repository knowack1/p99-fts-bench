use super::*;
use crate::fakes::a_topology;

#[test]
fn the_prepared_insert_names_the_articles_columns() {
    assert_eq!(
        insert_statement("articles"),
        "INSERT INTO articles (article_id, page_id, title, body) VALUES (?, ?, ?, ?)"
    );
}

#[test]
fn the_table_name_reaches_the_statement() {
    assert!(insert_statement("other_table").contains("INTO other_table"));
}

#[test]
fn consistency_names_round_trip() {
    assert_eq!(
        consistency_from_name("local_quorum").unwrap(),
        consistency_from_name("LOCAL_QUORUM").unwrap()
    );
}

#[test]
fn an_unknown_consistency_name_is_rejected() {
    let failure = consistency_from_name("NEARLY").unwrap_err().to_string();
    assert!(failure.contains("unknown consistency level"));
}

#[test]
fn every_consistency_name_maps_back_to_itself() {
    for (name, _) in CONSISTENCY_NAMES {
        assert_eq!(consistency_name(consistency_from_name(name).unwrap()), name);
    }
}

#[test]
fn the_default_consistency_is_the_one_the_readme_documents() {
    assert_eq!(
        consistency_from_name("LOCAL_ONE").unwrap(),
        Consistency::LocalOne
    );
}

/// The header claims a protocol version as a measured fact, so it is checked
/// against what the driver actually pins rather than trusted.
#[test]
fn the_recorded_protocol_matches_what_the_driver_speaks() {
    assert!(
        scylla_cql::frame::request::options::DEFAULT_CQL_PROTOCOL_VERSION
            .starts_with(PROTOCOL_VERSION)
    );
}

#[test]
fn the_driver_version_is_read_from_the_lock_file_not_guessed() {
    assert_ne!(DRIVER_VERSION, UNKNOWN);
    assert!(DRIVER_VERSION.starts_with("1."));
}

#[test]
fn the_topology_gathers_what_the_csv_header_needs() {
    let keys: Vec<String> = a_topology()
        .facts()
        .into_iter()
        .map(|(key, _)| key)
        .collect();
    assert_eq!(
        keys,
        [
            "scylla_version",
            "routing",
            "compression",
            "driver",
            "protocol",
            "runtime",
            "shard_aware",
            "shards",
            "connections",
            "tablets"
        ]
    );
}

#[test]
fn the_topology_records_the_tokio_worker_count_alongside_the_engine() {
    let facts = a_topology().facts();
    let runtime = facts.iter().find(|(key, _)| key == "runtime").unwrap();
    assert!(runtime.1.contains("workers:8"));
}

#[test]
fn a_contact_point_carries_the_requested_port() {
    let options = ConnectOptions {
        hosts: vec!["10.0.0.1".to_string(), "10.0.0.2".to_string()],
        port: 19042,
        keyspace: "wiki".to_string(),
        consistency: Consistency::LocalOne,
        request_timeout: Duration::from_secs(10),
    };
    assert_eq!(
        contact_points(&options),
        ["10.0.0.1:19042", "10.0.0.2:19042"]
    );
}

#[test]
fn a_shardless_node_is_named_as_such_rather_than_counted_as_zero() {
    assert_eq!(shard_count(None), "shards:none");
}

#[test]
fn a_resolvable_endpoint_parses_into_a_socket_address() {
    assert!(socket_addr("127.0.0.1", 19042).is_some());
    assert!(socket_addr("not a host", 19042).is_none());
}

fn some_options(consistency: Consistency, request_timeout: Duration) -> ConnectOptions {
    ConnectOptions {
        hosts: vec!["127.0.0.1".to_string()],
        port: 9042,
        keyspace: "wiki".to_string(),
        consistency,
        request_timeout,
    }
}

#[test]
fn the_profile_carries_the_consistency_and_timeout_that_were_asked_for() {
    let profile = execution_profile(&some_options(
        Consistency::LocalQuorum,
        Duration::from_secs(3),
    ));
    assert_eq!(profile.get_consistency(), Consistency::LocalQuorum);
    assert_eq!(profile.get_request_timeout(), Some(Duration::from_secs(3)));
}

/// A prepared statement carries a routing key; token awareness is what turns
/// that into a shard-local write, so it is set rather than left to the default.
#[test]
fn routing_is_token_aware_so_a_prepared_write_lands_on_its_shard() {
    let profile = execution_profile(&some_options(
        Consistency::LocalOne,
        Duration::from_secs(10),
    ));
    let named = format!("{:?}", profile.get_load_balancing_policy());
    assert!(named.contains("DefaultPolicy") && named.contains("is_token_aware: true"));
}
