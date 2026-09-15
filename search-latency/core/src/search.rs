//! What an interface under test has to be able to do.
//!
//! One method, because that is genuinely all a read benchmark needs of an
//! engine: take this query text, come back when the answer is complete, and say
//! how many documents it held. The three interfaces this tree measures —
//! ScyllaDB over CQL, the vector-store's BM25 endpoint over HTTP, OpenSearch
//! over HTTP — differ in everything else and in nothing that belongs here.
//!
//! **The hit count is not decoration.** A query class that matches nothing is
//! timing an empty result set, not a search, and the two are different numbers
//! on every engine. `Found::hits` is what lets a cell say so rather than
//! reporting a suspiciously good p99.
//!
//! **Boxed, unlike the sibling's `Inserter`.** `build-rate` kept its hot path
//! allocation-free because its job was to out-run the engine it measured; this
//! harness is not trying to out-run anything, one `Box` per network round trip
//! is unmeasurable, and the ScyllaDB binary picks between two interfaces at run
//! time — which a trait object expresses and a type parameter would only
//! duplicate.
use anyhow::Result;

pub use build_rate_core::index::BoxFuture;

/// ScyllaDB's own read path: `SELECT ... WHERE BM25(...) > 0 ORDER BY BM25(...)`.
pub const CQL: &str = "cql";
/// The vector-store's BM25 endpoint, with ScyllaDB out of the path entirely.
/// `cql` minus `vector-store` is ScyllaDB's own read overhead, which is the
/// only reason this interface exists.
pub const VECTOR_STORE: &str = "vector-store";
/// OpenSearch's `_search`.
pub const HTTP: &str = "http";

/// What one search returned.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Found {
    pub hits: usize,
}

impl Found {
    pub fn new(hits: usize) -> Self {
        Self { hits }
    }

    pub fn is_empty(&self) -> bool {
        self.hits == 0
    }
}

pub trait Searcher: Send + Sync + 'static {
    /// The query borrows for as long as the future lives, so an implementation
    /// that has to build a statement string around it may, and one that can
    /// send the text straight out is not made to copy it first.
    fn search<'a>(&'a self, query: &'a str) -> BoxFuture<'a, Result<Found>>;

    /// The CSV's `interface` column: which path into the engine this was.
    fn interface(&self) -> &'static str;

    /// Named in errors, so an operator is told where the harness was looking.
    fn endpoint(&self) -> &str;
}
