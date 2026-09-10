//! The canonical corpus as ScyllaDB bound parameters.
//!
//! `article_id` is the corpus's own deterministic uuid5 of the page id, so every
//! sweep point overwrites the same rows instead of growing the table.
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
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

#[derive(Deserialize)]
struct Document {
    id: i64,
    uuid: Uuid,
    title: String,
    text: String,
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

/// A fresh reader per call: every concurrency level reads the corpus from the
/// start, so the levels are comparable.
#[derive(Debug, Clone)]
pub struct CorpusSource {
    path: PathBuf,
    max_docs: usize,
}

impl CorpusSource {
    pub fn new(path: impl AsRef<Path>, max_docs: usize) -> Self {
        Self {
            path: path.as_ref().to_path_buf(),
            max_docs,
        }
    }

    pub fn open(&self) -> Result<Documents> {
        let file = File::open(&self.path)
            .with_context(|| format!("cannot read corpus {}", self.path.display()))?;
        Ok(Documents {
            path: self.path.clone(),
            lines: BufReader::new(file).lines(),
            max_docs: self.max_docs,
            seen: 0,
        })
    }
}

pub struct Documents {
    path: PathBuf,
    lines: std::io::Lines<BufReader<File>>,
    max_docs: usize,
    seen: usize,
}

impl Iterator for Documents {
    type Item = Result<InsertParams>;

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

    fn parse(&self, line: std::io::Result<String>) -> Result<InsertParams> {
        let text = line.with_context(|| self.at_current_line("unreadable"))?;
        let document: Document =
            serde_json::from_str(&text).with_context(|| self.at_current_line("malformed JSON"))?;
        Ok(document.into())
    }

    fn at_current_line(&self, what: &str) -> String {
        format!("{} line {}: {what}", self.path.display(), self.seen)
    }
}

#[cfg(test)]
#[path = "corpus_tests.rs"]
mod tests;
