//! The half of the tool that only exists against real endpoints: a CQL session
//! answering `BM25()`, and the vector-store answering `/bm25`.
//!
//! Ignored by default, because unlike the sibling tree's live tests there is no
//! sink that can stand in — an accept-and-discard endpoint stores nothing, and
//! a search against nothing is the one answer this harness treats as a failure.
//! Bring up the bench stack, build the index, then:
//!
//! ```text
//! SCYLLA_HOSTS=127.0.0.1 SCYLLA_PORT=19042 VS_URL=http://127.0.0.1:16080 \
//!   cargo test --test live_search -- --ignored
//! ```
//!
//! The query is a word that appears in any English Wikipedia corpus, so these
//! assert that a search came back with hits rather than asserting a count.
use std::sync::Arc;
use std::time::Duration;

use scylla::statement::Consistency;
use scyllarate::session::{self, ConnectOptions};
use scyllasearch::bm25::Bm25Searcher;
use scyllasearch::cql::{CqlSearcher, QueryShape, StatementMode};
use search_latency_core::search::Searcher;

const KEYSPACE: &str = "wiki";
const TABLE: &str = "articles";
const COLUMN: &str = "body";
const INDEX: &str = "articles_body_fts";
const QUERY: &str = "history";
const TIMEOUT: Duration = Duration::from_secs(30);

fn hosts() -> Vec<String> {
    std::env::var("SCYLLA_HOSTS")
        .unwrap_or_else(|_| "127.0.0.1".to_string())
        .split(',')
        .map(str::to_string)
        .collect()
}

fn port() -> u16 {
    std::env::var("SCYLLA_PORT")
        .ok()
        .and_then(|raw| raw.parse().ok())
        .unwrap_or(9042)
}

fn vs_url() -> String {
    std::env::var("VS_URL").unwrap_or_else(|_| "http://127.0.0.1:6080".to_string())
}

fn a_shape(fetch_documents: bool) -> QueryShape {
    QueryShape {
        table: TABLE.to_string(),
        column: COLUMN.to_string(),
        limit: 10,
        fetch_documents,
    }
}

async fn connect() -> Arc<scylla::client::session::Session> {
    Arc::new(
        session::connect(&ConnectOptions {
            hosts: hosts(),
            port: port(),
            keyspace: KEYSPACE.to_string(),
            consistency: Consistency::LocalOne,
            request_timeout: TIMEOUT,
        })
        .await
        .expect("the live tests need a reachable ScyllaDB with the corpus loaded"),
    )
}

async fn cql_searcher(mode: StatementMode, fetch_documents: bool) -> CqlSearcher {
    CqlSearcher::open(
        connect().await,
        a_shape(fetch_documents),
        mode,
        &[QUERY],
        "live",
    )
    .await
    .expect("opening a live CQL searcher")
}

#[tokio::test]
#[ignore = "needs a running ScyllaDB with a built index"]
async fn a_literal_statement_finds_documents() {
    let found = cql_searcher(StatementMode::Literal, false)
        .await
        .search(QUERY)
        .await
        .unwrap();

    assert!(found.hits > 0, "the index answered {QUERY:?} with no hits");
}

#[tokio::test]
#[ignore = "needs a running ScyllaDB with a built index"]
async fn a_prepared_statement_finds_the_same_documents() {
    let literal = cql_searcher(StatementMode::Literal, false)
        .await
        .search(QUERY)
        .await
        .unwrap();
    let prepared = cql_searcher(StatementMode::Prepared, false)
        .await
        .search(QUERY)
        .await
        .unwrap();

    assert_eq!(literal, prepared);
}

/// The projection changes what is measured but must not change what is found.
#[tokio::test]
#[ignore = "needs a running ScyllaDB with a built index"]
async fn projecting_documents_returns_the_same_hits_as_projecting_identities() {
    let identities = cql_searcher(StatementMode::Literal, false)
        .await
        .search(QUERY)
        .await
        .unwrap();
    let documents = cql_searcher(StatementMode::Literal, true)
        .await
        .search(QUERY)
        .await
        .unwrap();

    assert_eq!(identities, documents);
}

#[tokio::test]
#[ignore = "needs a running vector-store with a built index"]
async fn the_bm25_endpoint_finds_documents() {
    let searcher = Bm25Searcher::new(&vs_url(), KEYSPACE, INDEX, 10, false, TIMEOUT).unwrap();

    let found = searcher.search(QUERY).await.unwrap();

    assert!(found.hits > 0, "the index answered {QUERY:?} with no hits");
}

/// Both interfaces read the same Tantivy index, so at the same top-N they see
/// the same number of documents — which is what makes subtracting one latency
/// from the other mean anything.
#[tokio::test]
#[ignore = "needs both a running ScyllaDB and a running vector-store"]
async fn both_interfaces_agree_on_how_many_documents_matched() {
    let over_cql = cql_searcher(StatementMode::Literal, false)
        .await
        .search(QUERY)
        .await
        .unwrap();
    let over_http = Bm25Searcher::new(&vs_url(), KEYSPACE, INDEX, 10, false, TIMEOUT)
        .unwrap()
        .search(QUERY)
        .await
        .unwrap();

    assert_eq!(over_cql, over_http);
}
