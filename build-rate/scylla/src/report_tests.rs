use super::*;
use crate::fakes::a_topology;

fn a_setting(key: &str, value: &str) -> Vec<(String, String)> {
    vec![(key.to_string(), value.to_string())]
}

#[test]
fn header_carries_the_topology_as_comment_lines() {
    let lines = header_lines(&a_topology(), &a_setting("corpus", "data/corpus.jsonl"));
    assert!(lines.iter().all(|line| line.starts_with("# ")));
    assert!(lines.contains(&"# shard_aware=true".to_string()));
    assert!(lines.contains(&"# corpus=data/corpus.jsonl".to_string()));
}

#[test]
fn header_names_the_driver_that_produced_the_numbers() {
    let lines = header_lines(&a_topology(), &[]);
    assert!(lines.contains(&"# driver=1.8.0".to_string()));
}

/// One document per prepared INSERT, and not a knob. A CQL `BATCH` is a
/// different write path, so a `batch_size` above 1 on this half would read as
/// parity with a `_bulk` that it is not.
#[test]
fn this_half_carries_one_document_per_request() {
    assert_eq!(BATCH_SIZE, 1);
    assert_eq!(ENGINE, "scylladb");
}
