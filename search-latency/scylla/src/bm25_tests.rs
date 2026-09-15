use super::*;

use search_latency_core::test_support::stub::StubEndpoint;

const TWO_HITS: &str = r#"{"primary_keys": {"article_id": [1, 2]}, "scores": [2.5, 1.5]}"#;
const NO_HITS: &str = r#"{"primary_keys": {}}"#;
const COMPOSITE_KEY: &str =
    r#"{"primary_keys": {"part": ["a", "b", "c"], "clust": [1, 2, 3]}, "scores": [3.0, 2.0, 1.0]}"#;

async fn searcher_against(endpoint: &StubEndpoint, limit: usize) -> Bm25Searcher {
    Bm25Searcher::new(
        &endpoint.url(),
        "wiki",
        "articles_body_fts",
        limit,
        false,
        Duration::from_secs(5),
    )
    .unwrap()
}

#[tokio::test]
async fn the_documented_endpoint_and_payload_are_what_reaches_the_wire() {
    let stub = StubEndpoint::answering(200, TWO_HITS).await;
    let searcher = searcher_against(&stub, 100).await;

    searcher.search("kraken").await.unwrap();

    let seen = stub.seen();
    assert_eq!(seen[0].path, "/api/v1/indexes/wiki/articles_body_fts/bm25");
    assert_eq!(seen[0].body, r#"{"limit":100,"query":"kraken"}"#);
}

#[tokio::test]
async fn a_hit_count_is_the_length_of_a_key_column() {
    let stub = StubEndpoint::answering(200, TWO_HITS).await;

    let found = searcher_against(&stub, 10)
        .await
        .search("kraken")
        .await
        .unwrap();

    assert_eq!(found, Found::new(2));
}

/// The endpoint returns one array per key column. Counting the columns would
/// make a composite key report 2 hits for any number of them, and
/// `queries_per_s` is computed off that number.
#[tokio::test]
async fn a_composite_key_counts_rows_and_not_columns() {
    let stub = StubEndpoint::answering(200, COMPOSITE_KEY).await;

    let found = searcher_against(&stub, 10)
        .await
        .search("kraken")
        .await
        .unwrap();

    assert_eq!(found, Found::new(3));
}

#[tokio::test]
async fn no_hits_is_an_answer_rather_than_an_error() {
    let stub = StubEndpoint::answering(200, NO_HITS).await;

    let found = searcher_against(&stub, 10)
        .await
        .search("kraken")
        .await
        .unwrap();

    assert!(found.is_empty());
}

#[tokio::test]
async fn an_error_status_names_the_endpoint_it_came_from() {
    let stub = StubEndpoint::answering(500, "{}").await;

    let refused = searcher_against(&stub, 10)
        .await
        .search("kraken")
        .await
        .unwrap_err()
        .to_string();

    assert!(refused.contains("/bm25"), "{refused}");
}

#[tokio::test]
async fn an_answer_that_is_not_a_bm25_result_says_so() {
    let stub = StubEndpoint::answering(200, "not json at all").await;

    let refused = searcher_against(&stub, 10)
        .await
        .search("kraken")
        .await
        .unwrap_err()
        .to_string();

    assert!(refused.contains("not a BM25 result"), "{refused}");
}

/// Accepting the flag and quietly returning identities would put the cost of
/// the other interfaces' document fetch on the chart as an engine difference.
#[test]
fn fetching_documents_is_refused_rather_than_ignored() {
    let refused = Bm25Searcher::new(
        "http://localhost:1",
        "wiki",
        "articles_body_fts",
        10,
        true,
        Duration::from_secs(1),
    )
    .unwrap_err()
    .to_string();

    assert!(refused.contains("primary keys only"), "{refused}");
}

#[test]
fn a_trailing_slash_on_the_base_url_does_not_double_up() {
    let searcher = Bm25Searcher::new(
        "http://localhost:6080/",
        "wiki",
        "articles_body_fts",
        10,
        false,
        Duration::from_secs(1),
    )
    .unwrap();

    assert_eq!(
        searcher.endpoint(),
        "http://localhost:6080/api/v1/indexes/wiki/articles_body_fts/bm25"
    );
}

#[test]
fn the_interface_name_is_the_one_the_csv_column_carries() {
    let searcher = Bm25Searcher::new(
        "http://localhost:6080",
        "wiki",
        "articles_body_fts",
        10,
        false,
        Duration::from_secs(1),
    )
    .unwrap();

    assert_eq!(searcher.interface(), VECTOR_STORE);
}
