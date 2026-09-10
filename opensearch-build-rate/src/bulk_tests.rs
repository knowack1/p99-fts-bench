use serde_json::json;

use super::*;
use crate::fakes::{a_batch, a_document};

const INDEX: &str = "wiki-articles";

fn payload_lines(batch: &DocumentBatch) -> Vec<String> {
    let bytes = ndjson(batch, INDEX).unwrap();
    String::from_utf8(bytes.to_vec())
        .unwrap()
        .lines()
        .map(str::to_string)
        .collect()
}

fn an_item(status: u64) -> Value {
    json!({"index": {"_id": "1", "status": status}})
}

fn a_failing_item(status: u64, reason: &str) -> Value {
    json!({"index": {"_id": "1", "status": status, "error": {"reason": reason}}})
}

fn a_reply(items: Vec<Value>) -> Value {
    json!({"took": 3, "errors": items.iter().any(|item| item != &an_item(201)), "items": items})
}

/// Ground truth is `ftsbench.opensearch_load.bulk_payload`: one action line
/// naming the index and the `_id`, then that document's source line, both
/// newline-terminated. Ours is compact where `json.dumps` spaces its
/// separators; the fields, their order and the grammar are the same.
#[test]
fn a_batch_encodes_as_the_bulk_grammar_the_python_loader_sends() {
    let lines = payload_lines(&DocumentBatch::new(vec![a_document(7)]));
    assert_eq!(
        lines,
        [
            r#"{"index":{"_index":"wiki-articles","_id":"7"}}"#,
            r#"{"page_id":7,"title":"title 7","body":"text 7"}"#,
        ]
    );
}

#[test]
fn a_payload_is_two_lines_per_document() {
    assert_eq!(payload_lines(&a_batch(5)).len(), 10);
}

#[test]
fn every_action_names_the_index_it_was_built_for() {
    let bytes = ndjson(&a_batch(3), "other-index").unwrap();
    let text = String::from_utf8(bytes.to_vec()).unwrap();
    assert_eq!(text.matches(r#""_index":"other-index""#).count(), 3);
}

/// `_id` on every action is what makes a repeated level an overwrite rather
/// than a store that grows between points.
#[test]
fn every_action_names_the_document_id() {
    let lines = payload_lines(&a_batch(3));
    let ids: Vec<&String> = lines.iter().step_by(2).collect();
    assert!(ids[0].contains(r#""_id":"0""#) && ids[2].contains(r#""_id":"2""#));
}

#[test]
fn the_payload_ends_with_a_newline_as_the_ndjson_grammar_requires() {
    let bytes = ndjson(&a_batch(2), INDEX).unwrap();
    assert_eq!(bytes.last(), Some(&b'\n'));
}

#[test]
fn an_empty_batch_encodes_as_an_empty_payload() {
    assert!(ndjson(&DocumentBatch::new(vec![]), INDEX).unwrap().is_empty());
}

/// `ensure_ascii=False` in the Python loader; serde_json writes raw UTF-8 by
/// default, so an analyzer-parity probe reaches the engine as itself.
#[test]
fn non_ascii_text_is_sent_as_utf8_not_escaped() {
    let batch = DocumentBatch::new(vec![BulkDoc {
        page_id: 1,
        title: "t".to_string(),
        body: "Tokyo 東京都 café".to_string(),
    }]);
    let text = String::from_utf8(ndjson(&batch, INDEX).unwrap().to_vec()).unwrap();
    assert!(text.contains("東京都") && text.contains("café"));
}

#[test]
fn a_reply_where_every_item_landed_is_clean() {
    let outcome = read_outcome(&a_reply(vec![an_item(201), an_item(201)]), 2).unwrap();
    assert_eq!(outcome, BulkOutcome::default());
    assert!(outcome.is_clean());
}

/// A 2xx is not success: OpenSearch reports item failures inside a 200, and a
/// batch whose items were rejected must not read as a fast one.
#[test]
fn a_two_hundred_carrying_item_failures_counts_the_failed_documents() {
    let reply = a_reply(vec![
        an_item(201),
        a_failing_item(429, "rejected"),
        a_failing_item(503, "unavailable"),
    ]);
    let outcome = read_outcome(&reply, 3).unwrap();
    assert_eq!(outcome.failed, 2);
    assert!(!outcome.is_clean());
}

#[test]
fn the_first_item_failure_is_kept_for_the_operator() {
    let reply = a_reply(vec![a_failing_item(429, "queue full"), an_item(201)]);
    let failure = read_outcome(&reply, 2).unwrap().first_failure.unwrap();
    assert!(failure.contains("429") && failure.contains("queue full"));
}

#[test]
fn an_item_failure_with_no_error_object_still_names_its_status() {
    let reply = a_reply(vec![an_item(400)]);
    let failure = read_outcome(&reply, 1).unwrap().first_failure.unwrap();
    assert!(failure.contains("400") && failure.contains("no error given"));
}

/// A redirect is not an indexed document either, so the boundary is stated
/// once and tested at it.
#[test]
fn the_failure_boundary_is_three_hundred() {
    assert_eq!(FIRST_FAILING_STATUS, 300);
    assert!(read_outcome(&a_reply(vec![an_item(299)]), 1).unwrap().is_clean());
    assert!(!read_outcome(&a_reply(vec![an_item(300)]), 1).unwrap().is_clean());
}

/// A reply that cannot be accounted for document by document must not read as
/// a batch that landed.
#[test]
fn a_reply_with_the_wrong_item_count_is_an_error_not_a_success() {
    let failure = format!(
        "{:#}",
        read_outcome(&a_reply(vec![an_item(201)]), 512).unwrap_err()
    );
    assert!(failure.contains("1 item(s)") && failure.contains("512 document(s)"));
}

#[test]
fn a_reply_with_no_items_array_is_an_error() {
    let failure = format!("{:#}", read_outcome(&json!({"took": 1}), 1).unwrap_err());
    assert!(failure.contains("no items array"));
}

/// Which action an item reports is not what decides whether the document
/// landed, and this tool sends only `index` actions anyway.
#[test]
fn an_item_is_read_whatever_the_action_was_called() {
    let reply = json!({"items": [{"create": {"status": 409}}]});
    assert_eq!(read_outcome(&reply, 1).unwrap().failed, 1);
}

/// OpenSearch always sends a status; inventing a failure from its absence
/// would refuse a point over a parse disagreement.
#[test]
fn an_item_without_a_status_is_read_as_landed() {
    let reply = json!({"items": [{"index": {"_id": "1"}}]});
    assert!(read_outcome(&reply, 1).unwrap().is_clean());
}
