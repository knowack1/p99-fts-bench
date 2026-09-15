//! The query set, and how one class of it is handed out to N workers.
//!
//! The file is `ftsbench.generate_queries`'s output — classes of plain query
//! text valid for both parsers, ScyllaDB's BM25() (Tantivy) and OpenSearch's
//! `query_string` (Lucene). This harness never invents a query: a comparison
//! between engines is only a comparison if both were asked the same thing, and
//! the generator is what makes the set reproducible from a corpus and a seed.
//!
//! **One shared cursor rather than a per-worker stride.** A worker that happens
//! to be slow would, with a stride, visit fewer of its class's queries than the
//! others and shift the class's mix; one atomic hands every query out very
//! nearly the same number of times whatever the workers do. The atomic costs
//! nothing against a network round trip, which is the only thing between two
//! of its increments.
use std::collections::BTreeMap;
use std::path::Path;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use anyhow::{bail, Context, Result};
use serde::Deserialize;

#[derive(Debug, Deserialize)]
struct QueryFile {
    #[serde(default)]
    corpus: String,
    classes: BTreeMap<String, Vec<String>>,
}

/// One class of the matrix's query dimension, and the queries that make it up.
///
/// A class is handed to every cell in its row of the matrix, and cloning one
/// shares the queries rather than copying them — the text is the same text
/// every time, and at high concurrency it is read once per request.
#[derive(Debug, Clone)]
pub struct QueryClass {
    name: String,
    queries: Arc<Vec<String>>,
}

impl QueryClass {
    pub fn name(&self) -> &str {
        &self.name
    }

    pub fn queries(&self) -> &[String] {
        &self.queries
    }

    /// Reported per cell, because a class with four distinct queries and one
    /// with forty are not equally cached after the first second.
    pub fn distinct(&self) -> usize {
        self.queries.len()
    }

    /// A fresh cursor at the first query, so a cell always asks the same
    /// sequence however long the one before it ran.
    pub fn rotation(&self) -> Rotation {
        Rotation {
            queries: Arc::clone(&self.queries),
            next: AtomicUsize::new(0),
        }
    }
}

/// Every class the generator wrote, in name order.
///
/// Name order rather than file order because the JSON object it comes from has
/// no order to preserve; `--query-classes` is how a run says which classes it
/// wants and in which sequence.
#[derive(Debug, Clone)]
pub struct QuerySet {
    corpus: String,
    classes: Vec<QueryClass>,
}

impl QuerySet {
    pub fn load(path: &Path) -> Result<Self> {
        let text = std::fs::read_to_string(path)
            .with_context(|| format!("cannot read query set {}", path.display()))?;
        Self::parse(&text, &path.display().to_string())
    }

    pub fn parse(text: &str, origin: &str) -> Result<Self> {
        let file: QueryFile = serde_json::from_str(text)
            .with_context(|| format!("{origin} is not a query set: {ORIGIN_HINT}"))?;
        if file.classes.is_empty() {
            bail!("{origin} names no query classes");
        }
        for name in file.classes.keys() {
            refuse_an_unwritable_name(name, origin)?;
        }
        Ok(Self {
            corpus: file.corpus,
            classes: file
                .classes
                .into_iter()
                .map(|(name, queries)| QueryClass {
                    name,
                    queries: Arc::new(queries),
                })
                .collect(),
        })
    }

    /// The corpus the generator drew the terms from. Carried into the CSV
    /// header, because a query set built from one corpus and run against
    /// another is how a class quietly becomes a zero-hit class.
    pub fn corpus(&self) -> &str {
        &self.corpus
    }

    pub fn names(&self) -> Vec<&str> {
        self.classes.iter().map(QueryClass::name).collect()
    }

    /// The classes the run asked for, in the order it asked for them; all of
    /// them when it asked for none.
    ///
    /// An unknown name is an error rather than an empty column of the matrix: a
    /// typo would otherwise cost a whole run to discover.
    pub fn select(&self, wanted: &[String]) -> Result<Vec<QueryClass>> {
        if wanted.is_empty() {
            return self.classes.iter().map(Self::usable).collect();
        }
        wanted
            .iter()
            .map(|name| self.class(name).and_then(Self::usable))
            .collect()
    }

    fn class(&self, name: &str) -> Result<&QueryClass> {
        self.classes
            .iter()
            .find(|class| class.name() == name)
            .with_context(|| {
                format!(
                    "no query class named {name:?}; this set has {}",
                    self.names().join(", ")
                )
            })
    }

    /// A class with no queries cannot be measured, and a cell that ran zero
    /// queries would reach the CSV as a blank row rather than as the mistake it
    /// is.
    fn usable(class: &QueryClass) -> Result<QueryClass> {
        if class.queries().is_empty() {
            bail!("query class {:?} is empty", class.name());
        }
        Ok(class.clone())
    }
}

/// The class name is a CSV column in every row it produces, and this harness
/// writes that CSV without quoting: a name carrying a separator would move
/// every column after it by one and a consumer would read `engine` out of
/// `interface`.
fn refuse_an_unwritable_name(name: &str, origin: &str) -> Result<()> {
    if name.contains(',') || name.contains('\n') || name.contains('\r') {
        bail!(
            "{origin} names a query class {name:?} containing a separator; it \
             cannot be written to the cell CSV as one field"
        );
    }
    Ok(())
}

const ORIGIN_HINT: &str =
    "expected {\"classes\": {\"<name>\": [\"<query>\", ...]}}, as written by ftsbench.generate_queries";

/// One class's queries, handed out in order to whichever worker asks next.
/// Every query the selected classes will ask, which is what an interface that
/// has to prepare a statement per query needs before the first cell runs.
///
/// The *selected* classes, not the whole set: preparing a statement for a class
/// the matrix will never run costs a round trip at start-up, and — more to the
/// point — it is what makes "everything this searcher will be asked was
/// prepared" an invariant rather than a hope.
pub fn queries_of(classes: &[QueryClass]) -> Vec<&str> {
    classes
        .iter()
        .flat_map(|class| class.queries().iter().map(String::as_str))
        .collect()
}

pub struct Rotation {
    queries: Arc<Vec<String>>,
    next: AtomicUsize,
}

impl Rotation {
    pub fn next(&self) -> &str {
        let at = self.next.fetch_add(1, Ordering::Relaxed) % self.queries.len();
        &self.queries[at]
    }

    pub fn len(&self) -> usize {
        self.queries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.queries.is_empty()
    }
}

#[cfg(test)]
#[path = "queries_tests.rs"]
mod tests;
