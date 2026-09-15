//! The half of the tool that only exists against a real OpenSearch: the client,
//! the `_search` round trip and the analyzer the index was created with.
//!
//! Ignored by default, because unlike the sibling tree's live tests there is no
//! sink that can stand in — an accept-and-discard endpoint stores nothing, and
//! a search against nothing is the one answer this harness treats as a failure.
//! Bring up the bench stack, build the index, then:
//!
//! ```text
//! OS_URL=http://127.0.0.1:9200 cargo test --test live_search -- --ignored
//! ```
//!
//! The query is a word that appears in any English Wikipedia corpus, so these
//! assert that a search came back with hits rather than asserting a count.
use std::time::Duration;

use osrate::client::{self, ConnectOptions};
use ossearch::search::{HttpSearcher, QueryShape, BODY_FIELD, DEFAULT_OPERATOR};
use search_latency_core::search::Searcher;

const INDEX: &str = "wiki-articles";
const QUERY: &str = "history";
const TIMEOUT: Duration = Duration::from_secs(30);

fn url() -> String {
    std::env::var("OS_URL").unwrap_or_else(|_| "http://localhost:9200".to_string())
}

fn a_shape(fetch_documents: bool) -> QueryShape {
    QueryShape {
        field: BODY_FIELD.to_string(),
        default_operator: DEFAULT_OPERATOR.to_string(),
        limit: 10,
        fetch_documents,
    }
}

async fn searcher(fetch_documents: bool) -> HttpSearcher {
    let url = url();
    let client = client::connect(&ConnectOptions {
        url: url.clone(),
        index: INDEX.to_string(),
        request_timeout: TIMEOUT,
    })
    .await
    .expect("the live tests need a reachable OpenSearch with the corpus loaded");
    HttpSearcher::new(client, &url, INDEX, a_shape(fetch_documents))
}

#[tokio::test]
#[ignore = "needs a running OpenSearch with a built index"]
async fn a_search_finds_documents() {
    let found = searcher(false).await.search(QUERY).await.unwrap();

    assert!(found.hits > 0, "the index answered {QUERY:?} with no hits");
}

/// The projection changes what is measured but must not change what is found.
#[tokio::test]
#[ignore = "needs a running OpenSearch with a built index"]
async fn projecting_documents_returns_the_same_hits_as_projecting_identities() {
    let identities = searcher(false).await.search(QUERY).await.unwrap();
    let documents = searcher(true).await.search(QUERY).await.unwrap();

    assert_eq!(identities, documents);
}

/// Every class the generator writes has to be valid Lucene `query_string`
/// syntax, because the same text is handed to the other engine's parser.
#[tokio::test]
#[ignore = "needs a running OpenSearch with a built index"]
async fn every_query_shape_the_generator_writes_is_accepted() {
    let searcher = searcher(false).await;

    for query in [
        "kraken",
        "\"united states\"",
        "alpha AND beta",
        "alpha NOT beta",
        "(alpha OR beta) AND gamma NOT delta",
    ] {
        searcher
            .search(query)
            .await
            .unwrap_or_else(|exc| panic!("{query:?} was refused: {exc:#}"));
    }
}
