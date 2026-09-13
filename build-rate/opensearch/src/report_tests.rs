use super::*;
use crate::fakes::a_cluster;

fn a_setting(key: &str, value: &str) -> Vec<(String, String)> {
    vec![(key.to_string(), value.to_string())]
}

#[test]
fn header_carries_the_cluster_as_comment_lines() {
    let lines = header_lines(&a_cluster(), &a_setting("corpus", "data/corpus.jsonl"));
    assert!(lines.iter().all(|line| line.starts_with("# ")));
    assert!(lines.contains(&"# index=wiki-articles".to_string()));
    assert!(lines.contains(&"# corpus=data/corpus.jsonl".to_string()));
}

#[test]
fn header_names_the_clients_that_produced_the_numbers() {
    let lines = header_lines(&a_cluster(), &[]);
    assert!(lines.contains(&"# client=2.4.0".to_string()));
    assert!(lines.contains(&"# http_client=0.13.5".to_string()));
}

#[test]
fn header_names_the_refresh_interval_and_the_analyzer() {
    let lines = header_lines(&a_cluster(), &[]);
    assert!(lines.contains(&"# refresh_interval=1s".to_string()));
    assert!(lines.contains(&"# body_analyzer=m1_parity".to_string()));
}

/// A `_bulk` carries `batch_size` documents, so this half's latency is per
/// request and the header has to say so — the standing misreading on this side.
#[test]
fn this_half_says_what_its_latency_is_measured_per() {
    assert_eq!(LATENCY_UNIT, "bulk_request");
    assert_eq!(ENGINE, "opensearch");
}
