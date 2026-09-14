//! The canonical corpus as OpenSearch documents, grouped into `_bulk` batches.
//!
//! The corpus line's `uuid` is not read: it is ScyllaDB's partition key, and
//! OpenSearch's `_id` is the page id as a string — the same choice
//! `ftsbench.opensearch_load.bulk_payload` makes. Both are deterministic
//! functions of the page id, so on both engines a sweep point overwrites the
//! same documents instead of growing the store.

use anyhow::Result;
use serde::{Deserialize, Serialize};

/// Field names and their order are the `wiki-articles` mapping's, so the source
/// line this serializes to is the one `bench/opensearch/index-config.json`
/// declares an analyzer for.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct BulkDoc {
    pub page_id: i64,
    pub title: String,
    pub body: String,
}

impl BulkDoc {
    pub fn document_id(&self) -> String {
        self.page_id.to_string()
    }
}

#[derive(Debug, Clone, Deserialize)]
pub struct Document {
    id: i64,
    title: String,
    text: String,
}

impl From<Document> for BulkDoc {
    fn from(doc: Document) -> Self {
        Self {
            page_id: doc.id,
            title: doc.title,
            body: doc.text,
        }
    }
}

/// One `_bulk` request's worth of documents. The last batch of a level is short
/// whenever the corpus does not divide by the batch size, which is why `docs()`
/// is asked rather than assumed to be the configured batch size.
#[derive(Debug, Clone, PartialEq)]
pub struct DocumentBatch(Vec<BulkDoc>);

impl build_rate_core::sweep::WorkItem for DocumentBatch {
    fn docs(&self) -> u64 {
        self.0.len() as u64
    }
}

impl DocumentBatch {
    pub fn new(documents: Vec<BulkDoc>) -> Self {
        Self(documents)
    }

    pub fn documents(&self) -> &[BulkDoc] {
        &self.0
    }

    pub fn docs(&self) -> u64 {
        self.0.len() as u64
    }

    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }
}

/// A fresh reader per level and the `--max-docs` cut are
/// `build_rate_core::corpus`'s, and so is the batching. What is here is the
/// source document this engine sends and the id it sends it under.
pub type CorpusSource = build_rate_core::corpus::CorpusSource;

/// The corpus as `_bulk` batches of `batch_size` documents.
pub fn batches(
    source: &CorpusSource,
    batch_size: usize,
) -> Result<impl Iterator<Item = Result<DocumentBatch>> + Send + 'static> {
    let documents = source
        .open::<Document>()?
        .map(|line| line.map(BulkDoc::from));
    Ok(build_rate_core::corpus::chunks(documents, batch_size)
        .map(|batch| batch.map(DocumentBatch::new)))
}
