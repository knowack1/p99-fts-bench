use super::*;

use std::time::Duration;

use osrate::client::{build_client, ConnectOptions};
use search_latency_core::test_support::stub::StubEndpoint;

const TWO_HITS: &str = r#"{"hits": {"hits": [{"_id": "1"}, {"_id": "2"}]}}"#;
const NO_HITS: &str = r#"{"hits": {"hits": []}}"#;

fn a_shape() -> QueryShape {
    QueryShape {
        field: BODY_FIELD.to_string(),
        default_operator: DEFAULT_OPERATOR.to_string(),
        limit: 10,
        fetch_documents: false,
    }
}

fn searcher_against(endpoint: &StubEndpoint, shape: QueryShape) -> HttpSearcher {
    let url = endpoint.url();
    let client = build_client(&ConnectOptions {
        url: url.clone(),
        index: "wiki-articles".to_string(),
        request_timeout: Duration::from_secs(5),
    })
    .unwrap();
    HttpSearcher::new(client, &url, "wiki-articles", shape)
}

/// ScyllaDB reports no result totals at all, so counting every match here would
/// be OpenSearch doing work the other engine was never asked to do.
#[test]
fn the_total_hit_count_is_never_asked_for() {
    assert_eq!(a_shape().body("kraken")["track_total_hits"], false);
}

#[test]
fn the_query_goes_through_the_parser_the_other_engine_shares_a_syntax_with() {
    let body = a_shape().body("(alpha OR beta) AND gamma NOT delta");

    let parsed = &body["query"]["query_string"];
    assert_eq!(parsed["query"], "(alpha OR beta) AND gamma NOT delta");
    assert_eq!(parsed["default_field"], "body");
    assert_eq!(parsed["default_operator"], "OR");
}

#[test]
fn the_top_n_is_the_size_asked_for() {
    let body = QueryShape {
        limit: 1000,
        ..a_shape()
    }
    .body("kraken");

    assert_eq!(body["size"], 1000);
}

#[test]
fn identities_only_unless_documents_were_asked_for() {
    assert_eq!(a_shape().body("kraken")["_source"], false);
}

/// The same two fields under the same two names the CQL half projects, so the
/// two engines return the same bytes and the comparison stays symmetric.
#[test]
fn asking_for_documents_projects_the_columns_the_other_engine_projects() {
    let body = QueryShape {
        fetch_documents: true,
        ..a_shape()
    }
    .body("kraken");

    assert_eq!(body["_source"], serde_json::json!(["title", "body"]));
}

#[tokio::test]
async fn a_search_reaches_the_index_it_was_opened_for() {
    let stub = StubEndpoint::answering(200, TWO_HITS).await;

    let found = searcher_against(&stub, a_shape())
        .search("kraken")
        .await
        .unwrap();

    assert_eq!(found, Found::new(2));
    assert_eq!(stub.seen()[0].path, "/wiki-articles/_search");
}

#[tokio::test]
async fn no_hits_is_an_answer_rather_than_an_error() {
    let stub = StubEndpoint::answering(200, NO_HITS).await;

    let found = searcher_against(&stub, a_shape())
        .search("zzzzz")
        .await
        .unwrap();

    assert!(found.is_empty());
}

#[tokio::test]
async fn an_error_status_names_the_endpoint_it_came_from() {
    let stub = StubEndpoint::answering(503, "{}").await;

    let refused = searcher_against(&stub, a_shape())
        .search("kraken")
        .await
        .unwrap_err()
        .to_string();

    assert!(refused.contains("_search"), "{refused}");
}

#[tokio::test]
async fn an_answer_that_is_not_a_search_result_says_so() {
    let stub = StubEndpoint::answering(200, r#"{"acknowledged": true}"#).await;

    let refused = searcher_against(&stub, a_shape())
        .search("kraken")
        .await
        .unwrap_err()
        .to_string();

    assert!(refused.contains("not a search result"), "{refused}");
}

#[test]
fn the_interface_name_is_the_one_the_csv_column_carries() {
    let stub_url = "http://localhost:9200";
    let client = build_client(&ConnectOptions {
        url: stub_url.to_string(),
        index: "wiki-articles".to_string(),
        request_timeout: Duration::from_secs(1),
    })
    .unwrap();
    let searcher = HttpSearcher::new(client, stub_url, "wiki-articles", a_shape());

    assert_eq!(searcher.interface(), HTTP);
    assert_eq!(
        searcher.endpoint(),
        "http://localhost:9200/wiki-articles/_search"
    );
}
