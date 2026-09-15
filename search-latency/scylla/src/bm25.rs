//! BM25 straight against the vector-store's index, with ScyllaDB out of the
//! path entirely.
//!
//! Its purpose is subtraction. The `cql` interface times the whole round trip —
//! coordinator hop, BM25 dispatch into this same index, then the read that
//! satisfies the projection — so on its own it cannot say whether a gap against
//! OpenSearch lives in the index or in the path around it. This times the index
//! alone, and the difference is ScyllaDB's own read overhead.
//!
//! **`--fetch-documents` is refused here rather than ignored.** The endpoint
//! returns primary keys and scores and has no way to return document text. A
//! matrix where the other interfaces projected `title` and `body` while this one
//! returned identities would put the cost of that fetch on the chart as an
//! engine property.
use std::time::Duration;

use anyhow::{bail, Context, Result};
use search_latency_core::search::{BoxFuture, Found, Searcher, VECTOR_STORE};
use serde::Deserialize;
use serde_json::{json, Value};

/// The endpoint returns one array per primary-key column rather than one object
/// per row, so a hit count is the length of a column and never the number of
/// them: on a composite key, counting columns would report 2 for any number of
/// hits, and `queries_per_s` is computed off that number.
#[derive(Debug, Deserialize)]
struct Bm25Answer {
    #[serde(default)]
    primary_keys: serde_json::Map<String, Value>,
}

impl Bm25Answer {
    fn hits(&self) -> usize {
        self.primary_keys
            .values()
            .next()
            .and_then(Value::as_array)
            .map_or(0, Vec::len)
    }
}

#[derive(Debug)]
pub struct Bm25Searcher {
    client: reqwest::Client,
    endpoint: String,
    limit: usize,
}

impl Bm25Searcher {
    pub fn new(
        base_url: &str,
        keyspace: &str,
        index: &str,
        limit: usize,
        fetch_documents: bool,
        timeout: Duration,
    ) -> Result<Self> {
        if fetch_documents {
            bail!(
                "the vector-store's BM25 endpoint returns primary keys only, so \
                 --fetch-documents cannot be honoured; a matrix measured this way \
                 is not comparable with the other interfaces"
            );
        }
        let base = base_url.trim_end_matches('/');
        Ok(Self {
            client: reqwest::Client::builder()
                .timeout(timeout)
                .build()
                .context("cannot build an HTTP client for the vector-store")?,
            endpoint: format!("{base}/api/v1/indexes/{keyspace}/{index}/bm25"),
            limit,
        })
    }

    async fn ask(&self, query: &str) -> Result<Found> {
        let answer: Bm25Answer = self
            .client
            .post(&self.endpoint)
            .json(&json!({"query": query, "limit": self.limit}))
            .send()
            .await
            .with_context(|| format!("cannot reach {}", self.endpoint))?
            .error_for_status()
            .with_context(|| format!("{} answered an error", self.endpoint))?
            .json()
            .await
            .with_context(|| {
                format!(
                    "{} answered something that is not a BM25 result",
                    self.endpoint
                )
            })?;
        Ok(Found::new(answer.hits()))
    }
}

impl Searcher for Bm25Searcher {
    fn search<'a>(&'a self, query: &'a str) -> BoxFuture<'a, Result<Found>> {
        Box::pin(self.ask(query))
    }

    fn interface(&self) -> &'static str {
        VECTOR_STORE
    }

    fn endpoint(&self) -> &str {
        &self.endpoint
    }
}

#[cfg(test)]
#[path = "bm25_tests.rs"]
mod tests;
