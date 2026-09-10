//! The canonical corpus as OpenSearch documents, grouped into `_bulk` batches.
//!
//! The corpus line's `uuid` is not read: it is ScyllaDB's partition key, and
//! OpenSearch's `_id` is the page id as a string — the same choice
//! `ftsbench.opensearch_load.bulk_payload` makes. Both are deterministic
//! functions of the page id, so on both engines a sweep point overwrites the
//! same documents instead of growing the store.
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
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

#[derive(Deserialize)]
struct Document {
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

/// A fresh reader per call: every concurrency level reads the corpus from the
/// start, so the levels are comparable.
#[derive(Debug, Clone)]
pub struct CorpusSource {
    path: PathBuf,
    max_docs: usize,
    batch_size: usize,
}

impl CorpusSource {
    pub fn new(path: impl AsRef<Path>, max_docs: usize, batch_size: usize) -> Self {
        Self {
            path: path.as_ref().to_path_buf(),
            max_docs,
            batch_size,
        }
    }

    pub fn open(&self) -> Result<Batches> {
        let file = File::open(&self.path)
            .with_context(|| format!("cannot read corpus {}", self.path.display()))?;
        Ok(Batches {
            documents: Documents {
                path: self.path.clone(),
                lines: BufReader::new(file).lines(),
                max_docs: self.max_docs,
                seen: 0,
            },
            batch_size: self.batch_size,
        })
    }
}

struct Documents {
    path: PathBuf,
    lines: std::io::Lines<BufReader<File>>,
    max_docs: usize,
    seen: usize,
}

impl Iterator for Documents {
    type Item = Result<BulkDoc>;

    fn next(&mut self) -> Option<Self::Item> {
        if self.reached_limit() {
            return None;
        }
        let line = self.lines.next()?;
        self.seen += 1;
        Some(self.parse(line))
    }
}

impl Documents {
    fn reached_limit(&self) -> bool {
        self.max_docs > 0 && self.seen >= self.max_docs
    }

    fn parse(&self, line: std::io::Result<String>) -> Result<BulkDoc> {
        let text = line.with_context(|| self.at_current_line("unreadable"))?;
        let document: Document =
            serde_json::from_str(&text).with_context(|| self.at_current_line("malformed JSON"))?;
        Ok(document.into())
    }

    fn at_current_line(&self, what: &str) -> String {
        format!("{} line {}: {what}", self.path.display(), self.seen)
    }
}

/// Groups the corpus into batches of `batch_size`. A malformed line fails the
/// batch it fell in, and through it the whole point: a level that quietly
/// delivered fewer documents than the others is not comparable to them.
pub struct Batches {
    documents: Documents,
    batch_size: usize,
}

impl Iterator for Batches {
    type Item = Result<DocumentBatch>;

    fn next(&mut self) -> Option<Self::Item> {
        match self.take_batch() {
            Err(exc) => Some(Err(exc)),
            Ok(batch) if batch.is_empty() => None,
            Ok(batch) => Some(Ok(batch)),
        }
    }
}

impl Batches {
    fn take_batch(&mut self) -> Result<DocumentBatch> {
        let mut documents = Vec::with_capacity(self.batch_size);
        while documents.len() < self.batch_size {
            match self.documents.next() {
                None => break,
                Some(document) => documents.push(document?),
            }
        }
        Ok(DocumentBatch::new(documents))
    }
}

#[cfg(test)]
#[path = "corpus_tests.rs"]
mod tests;
