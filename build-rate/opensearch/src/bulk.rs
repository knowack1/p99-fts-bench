//! One `_bulk` body, built by hand.
//!
//! Raw NDJSON on purpose, the same choice `ftsbench.opensearch_load` documents:
//! this is exactly what the ingest path looks like on the wire, rather than
//! whatever a helper decided to send. The grammar is one action line per
//! document followed by that document's source line, both newline-terminated.
//!
//! One deliberate difference from the Python loader: `serde_json` writes
//! compact JSON where `json.dumps` defaults to `", "` and `": "` separators, so
//! the same corpus is a few percent fewer bytes here. See the README.
use anyhow::{bail, Result};
use bytes::{BufMut, Bytes, BytesMut};
use serde::Serialize;
use serde_json::Value;

use crate::corpus::{BulkDoc, DocumentBatch};

/// Item statuses OpenSearch reports inside a 200 response. Anything at 300 or
/// above is a document that did not land.
pub const FIRST_FAILING_STATUS: u64 = 300;
const ASSUMED_STATUS: u64 = 200;
const BYTES_PER_DOCUMENT: usize = 2048;

/// `_id` is named on every action, so resending a batch overwrites rather than
/// duplicating. That keeps a repeated level's document count honest; what makes
/// the level a *build* rather than an update is `reset.rs` emptying the index
/// before it.
#[derive(Serialize)]
struct IndexAction<'a> {
    index: IndexTarget<'a>,
}

#[derive(Serialize)]
struct IndexTarget<'a> {
    _index: &'a str,
    _id: String,
}

pub fn ndjson(batch: &DocumentBatch, index: &str) -> Result<Bytes> {
    let mut payload = BytesMut::with_capacity(batch.documents().len() * BYTES_PER_DOCUMENT);
    for document in batch.documents() {
        write_line(&mut payload, &action_for(document, index))?;
        write_line(&mut payload, document)?;
    }
    Ok(payload.freeze())
}

fn action_for<'a>(document: &BulkDoc, index: &'a str) -> IndexAction<'a> {
    IndexAction {
        index: IndexTarget {
            _index: index,
            _id: document.document_id(),
        },
    }
}

fn write_line(payload: &mut BytesMut, value: &impl Serialize) -> Result<()> {
    serde_json::to_writer((&mut *payload).writer(), value)?;
    payload.put_u8(b'\n');
    Ok(())
}

/// What one `_bulk` response says happened, in documents.
///
/// A 2xx is not success: OpenSearch reports per-item failures inside a 200
/// response, so a batch whose items were rejected has to be counted here or a
/// failing engine would read as a fast one.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct BulkOutcome {
    pub failed: u64,
    pub first_failure: Option<String>,
}

impl BulkOutcome {
    pub fn is_clean(&self) -> bool {
        self.failed == 0
    }
}

/// `offered` is checked against the item count rather than trusted: a reply
/// with the wrong number of items cannot be accounted for document by
/// document, and must not read as a batch that landed.
pub fn read_outcome(body: &Value, offered: u64) -> Result<BulkOutcome> {
    let items = items_of(body)?;
    if items.len() as u64 != offered {
        bail!(
            "bulk reply carried {} item(s) for {offered} document(s) offered",
            items.len()
        );
    }
    Ok(summarize_items(&items))
}

fn items_of(body: &Value) -> Result<Vec<&Value>> {
    match body.get("items").and_then(Value::as_array) {
        Some(items) => Ok(items.iter().collect()),
        None => bail!("bulk reply carried no items array"),
    }
}

fn summarize_items(items: &[&Value]) -> BulkOutcome {
    let failures: Vec<&Value> = items.iter().copied().filter(|item| is_failure(item)).collect();
    BulkOutcome {
        failed: failures.len() as u64,
        first_failure: failures.first().map(|item| describe_failure(item)),
    }
}

/// Each item is `{"<action>": {...}}`, and which action it was does not change
/// whether the document landed.
fn outcome_of(item: &Value) -> Option<&Value> {
    item.as_object()?.values().next()
}

fn is_failure(item: &Value) -> bool {
    status_of(item) >= FIRST_FAILING_STATUS
}

/// A missing status is read as success: OpenSearch always sends one, and
/// inventing a failure would refuse a point for a parse disagreement.
fn status_of(item: &Value) -> u64 {
    outcome_of(item)
        .and_then(|outcome| outcome.get("status"))
        .and_then(Value::as_u64)
        .unwrap_or(ASSUMED_STATUS)
}

fn describe_failure(item: &Value) -> String {
    let outcome = outcome_of(item);
    let reason = outcome
        .and_then(|outcome| outcome.get("error"))
        .map_or_else(|| "no error given".to_string(), ToString::to_string);
    format!("status {} {reason}", status_of(item))
}

#[cfg(test)]
#[path = "bulk_tests.rs"]
mod tests;
