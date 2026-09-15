//! `POST /{index}/_search`, shaped so that what it asks for is what the other
//! engine can be asked for.
//!
//! **`query_string`, not `match`.** Single terms, `"quoted phrases"`, `AND` /
//! `OR` / `NOT` and `(grouping)` mean the same thing in Lucene's `query_string`
//! parser as they do in the Tantivy parser behind `BM25()`, so one query set is
//! valid for both engines and a class means the same thing on both sides.
//!
//! **`track_total_hits: false`**, because ScyllaDB reports no result totals at
//! all. Leaving it on would make OpenSearch count every match while the other
//! engine counted the top N, and that is a real amount of work on a common
//! term.
//!
//! **`_source` mirrors the other engine's projection.** Off, and the hit is an
//! identity; on, and it is `title` and `body` — the same two fields under the
//! same two names the CQL half projects. The two modes are different
//! measurements, which is why `fetch_documents` is a column in the CSV.
use anyhow::{Context, Result};
use opensearch::{OpenSearch, SearchParts};
use search_latency_core::search::{BoxFuture, Found, Searcher, HTTP};
use serde::Deserialize;
use serde_json::{json, Value};

pub const BODY_FIELD: &str = "body";
pub const DEFAULT_OPERATOR: &str = "OR";
/// Projected when documents are asked for. The same names the `wiki.articles`
/// table uses, so the two engines return the same bytes.
pub const DOCUMENT_FIELDS: [&str; 2] = ["title", "body"];

#[derive(Debug, Deserialize)]
struct SearchAnswer {
    hits: Hits,
}

#[derive(Debug, Deserialize)]
struct Hits {
    #[serde(default)]
    hits: Vec<Value>,
}

/// Everything about the request except the query text.
#[derive(Debug, Clone)]
pub struct QueryShape {
    pub field: String,
    pub default_operator: String,
    pub limit: usize,
    pub fetch_documents: bool,
}

impl QueryShape {
    pub fn body(&self, query: &str) -> Value {
        json!({
            "size": self.limit,
            "_source": self.source(),
            "track_total_hits": false,
            "query": {
                "query_string": {
                    "query": query,
                    "default_field": self.field,
                    "default_operator": self.default_operator,
                }
            }
        })
    }

    fn source(&self) -> Value {
        if self.fetch_documents {
            return json!(DOCUMENT_FIELDS);
        }
        json!(false)
    }
}

pub struct HttpSearcher {
    client: OpenSearch,
    index: String,
    shape: QueryShape,
    endpoint: String,
}

impl HttpSearcher {
    pub fn new(client: OpenSearch, url: &str, index: impl Into<String>, shape: QueryShape) -> Self {
        let index = index.into();
        Self {
            endpoint: format!("{}/{index}/_search", url.trim_end_matches('/')),
            client,
            index,
            shape,
        }
    }

    /// The reply is deserialized rather than counted off the frame, which is
    /// what makes `--fetch-documents` cost what it costs: with `_source` on,
    /// the article text is in this body and parsing it is the client's half of
    /// the fetch.
    async fn ask(&self, query: &str) -> Result<Found> {
        let answer: SearchAnswer = self
            .client
            .search(SearchParts::Index(&[&self.index]))
            .body(self.shape.body(query))
            .send()
            .await
            .with_context(|| format!("searching {} for {query:?} failed", self.endpoint))?
            .error_for_status_code()
            .with_context(|| format!("{} answered an error for {query:?}", self.endpoint))?
            .json()
            .await
            .with_context(|| {
                format!(
                    "{} answered something that is not a search result",
                    self.endpoint
                )
            })?;
        Ok(Found::new(answer.hits.hits.len()))
    }
}

impl Searcher for HttpSearcher {
    fn search<'a>(&'a self, query: &'a str) -> BoxFuture<'a, Result<Found>> {
        Box::pin(self.ask(query))
    }

    fn interface(&self) -> &'static str {
        HTTP
    }

    fn endpoint(&self) -> &str {
        &self.endpoint
    }
}

#[cfg(test)]
#[path = "search_tests.rs"]
mod tests;
