//! The canonical corpus as ScyllaDB bound parameters.
//!
//! `article_id` is the corpus's own deterministic uuid5 of the page id, so every
//! sweep point overwrites the same rows instead of growing the table.

use scylla::SerializeRow;
use serde::Deserialize;
use uuid::Uuid;

/// Field names are the `wiki.articles` column names: the driver binds the
/// prepared statement's markers by the names its metadata carries.
#[derive(Debug, Clone, PartialEq, SerializeRow)]
pub struct InsertParams {
    pub article_id: Uuid,
    pub page_id: i64,
    pub title: String,
    pub body: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Document {
    id: i64,
    uuid: Uuid,
    title: String,
    text: String,
}

impl build_rate_core::sweep::WorkItem for InsertParams {
    fn docs(&self) -> u64 {
        1
    }
}

impl From<Document> for InsertParams {
    fn from(doc: Document) -> Self {
        Self {
            article_id: doc.uuid,
            page_id: doc.id,
            title: doc.title,
            body: doc.text,
        }
    }
}

/// A fresh reader per level, and the `--max-docs` cut, are
/// `build_rate_core::corpus`'s. What is here is the row this engine binds.
pub type CorpusSource = build_rate_core::corpus::CorpusSource;

/// The corpus as bound parameters, one row per line.
pub fn rows(
    source: &CorpusSource,
) -> anyhow::Result<impl Iterator<Item = anyhow::Result<InsertParams>> + Send + 'static> {
    Ok(source
        .open::<Document>()?
        .map(|line| line.map(InsertParams::from)))
}

#[cfg(test)]
#[path = "corpus_tests.rs"]
mod tests;
