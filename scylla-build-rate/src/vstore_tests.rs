//! What the probe must get right is the difference between "no index", "not
//! answering" and "an index with a count". A reset gate that confuses the first
//! two waits forever on a dead endpoint or starts loading into nothing.
use std::time::Duration;

use super::*;
use crate::fakes::{FakeVectorStore, Reply};

const A_TIMEOUT: Duration = Duration::from_secs(5);

async fn probe_against(store: &FakeVectorStore) -> IndexProbe {
    IndexProbe::new(store.url(), "wiki", DEFAULT_VS_INDEX, A_TIMEOUT).unwrap()
}

#[tokio::test]
async fn a_serving_index_reports_its_count() {
    let store = FakeVectorStore::start(Reply::Serving(270_269)).await;
    let state = probe_against(&store).await.status().await.unwrap();

    assert_eq!(state.count(), 270_269);
    assert!(state.serving().is_some());
}

#[tokio::test]
async fn an_index_that_is_still_building_is_present_but_not_serving() {
    let store = FakeVectorStore::start(Reply::Building(12)).await;
    let state = probe_against(&store).await.status().await.unwrap();

    assert_eq!(state.count(), 12);
    assert!(state.serving().is_none());
}

#[tokio::test]
async fn a_404_is_an_absent_index_rather_than_a_failure() {
    let store = FakeVectorStore::start(Reply::Absent).await;
    assert_eq!(
        probe_against(&store).await.status().await.unwrap(),
        IndexState::Absent
    );
}

/// The distinction the reset depends on: a vector-store that is down has not
/// told us the index is gone.
#[tokio::test]
async fn an_endpoint_that_errors_is_not_read_as_an_absent_index() {
    let store = FakeVectorStore::start(Reply::Failing(503)).await;
    let failure = probe_against(&store).await.status().await.unwrap_err();

    assert!(format!("{failure:#}").contains("answered an error"));
}

#[tokio::test]
async fn an_unreachable_vector_store_names_the_url_it_could_not_reach() {
    let probe = IndexProbe::new(
        "http://127.0.0.1:1",
        "wiki",
        "idx",
        Duration::from_millis(200),
    )
    .unwrap();
    let failure = format!("{:#}", probe.status().await.unwrap_err());

    assert!(failure.contains("cannot reach"));
    assert!(failure.contains("/api/v1/indexes/wiki/idx/status"));
}

#[tokio::test]
async fn the_status_url_is_the_one_the_campaign_polls() {
    let probe = IndexProbe::new(
        "http://localhost:6080/",
        "wiki",
        "articles_body_fts",
        A_TIMEOUT,
    )
    .unwrap();
    assert_eq!(
        probe.status_url(),
        "http://localhost:6080/api/v1/indexes/wiki/articles_body_fts/status"
    );
}

#[tokio::test]
async fn a_version_that_cannot_be_read_is_unknown_rather_than_fatal() {
    let probe = IndexProbe::new(
        "http://127.0.0.1:1",
        "wiki",
        "idx",
        Duration::from_millis(200),
    )
    .unwrap();
    assert_eq!(probe.version().await, UNKNOWN);
}

#[tokio::test]
async fn the_version_annotates_the_header_when_it_can_be_read() {
    let store = FakeVectorStore::start(Reply::Absent).await;
    assert_eq!(probe_against(&store).await.version().await, "1.10.0-fake");
}
