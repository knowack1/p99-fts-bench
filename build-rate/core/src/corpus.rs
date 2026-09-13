//! Reading the canonical corpus, one JSONL line at a time.
//!
//! What a line becomes is the engine's business — bound parameters on one side,
//! a `_bulk` source document on the other, and the two deliberately derive
//! their document id differently. What is shared is everything around that: a
//! fresh reader per level so the levels are comparable, the `--max-docs` cut,
//! and an error that names the file and the line rather than the byte.
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::marker::PhantomData;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde::de::DeserializeOwned;

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

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn open<D: DeserializeOwned>(&self) -> Result<Lines<D>> {
        let file = File::open(&self.path)
            .with_context(|| format!("cannot read corpus {}", self.path.display()))?;
        Ok(Lines {
            path: self.path.clone(),
            lines: BufReader::new(file).lines(),
            max_docs: self.max_docs,
            seen: 0,
            document: PhantomData,
        })
    }
}

pub struct Lines<D> {
    path: PathBuf,
    lines: std::io::Lines<BufReader<File>>,
    max_docs: usize,
    seen: usize,
    document: PhantomData<D>,
}

impl<D: DeserializeOwned> Iterator for Lines<D> {
    type Item = Result<D>;

    fn next(&mut self) -> Option<Self::Item> {
        if self.reached_limit() {
            return None;
        }
        let line = self.lines.next()?;
        self.seen += 1;
        Some(self.parse(line))
    }
}

impl<D: DeserializeOwned> Lines<D> {
    fn reached_limit(&self) -> bool {
        self.max_docs > 0 && self.seen >= self.max_docs
    }

    fn parse(&self, line: std::io::Result<String>) -> Result<D> {
        let text = line.with_context(|| self.at_current_line("unreadable"))?;
        serde_json::from_str(&text).with_context(|| self.at_current_line("malformed JSON"))
    }

    /// A truncated corpus is a thing that happens, and "line 41,203" is what
    /// lets someone go and look at it.
    fn at_current_line(&self, what: &str) -> String {
        format!("{} line {}: {what}", self.path.display(), self.seen)
    }
}

/// Groups a fallible stream of documents into fixed-size runs.
///
/// The last run of a level is short whenever the corpus does not divide by the
/// size, which is why a consumer asks a batch how many documents it carries
/// rather than assuming the configured one: a level that delivered fewer
/// documents than the others is not comparable to them.
pub struct Chunks<I> {
    documents: I,
    size: usize,
}

pub fn chunks<I>(documents: I, size: usize) -> Chunks<I> {
    Chunks { documents, size }
}

impl<I, D> Iterator for Chunks<I>
where
    I: Iterator<Item = Result<D>>,
{
    type Item = Result<Vec<D>>;

    fn next(&mut self) -> Option<Self::Item> {
        match self.take_batch() {
            Err(exc) => Some(Err(exc)),
            Ok(batch) if batch.is_empty() => None,
            Ok(batch) => Some(Ok(batch)),
        }
    }
}

impl<I, D> Chunks<I>
where
    I: Iterator<Item = Result<D>>,
{
    fn take_batch(&mut self) -> Result<Vec<D>> {
        let mut batch = Vec::with_capacity(self.size);
        while batch.len() < self.size {
            match self.documents.next() {
                None => break,
                Some(document) => batch.push(document?),
            }
        }
        Ok(batch)
    }
}

#[cfg(test)]
#[path = "corpus_tests.rs"]
mod tests;
