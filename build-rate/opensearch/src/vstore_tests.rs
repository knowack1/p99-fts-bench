//! What the probe must get right is the difference between "no index", "not
//! answering", and "an index whose searchable count is merely behind". A watch
//! that confuses the last two reports a stalled build for an index that is
//! working perfectly and has simply not refreshed.
use super::*;
use crate::client::{build_client, ConnectOptions};
use crate::fakes::FakeIndex;
use std::time::Duration;

fn probe_against(index: &FakeIndex) -> StatsProbe {
    let options = ConnectOptions {
        url: index.url().to_string(),
        index: "wiki-articles".to_string(),
        request_timeout: Duration::from_secs(5),
    };
    StatsProbe::new(build_client(&options).unwrap(), index.url(), "wiki-articles")
}

#[tokio::test]
async fn a_serving_index_reports_what_a_search_would_find() {
    let index = FakeIndex::start().await;
    index.holding(270_269);

    let state = probe_against(&index).read().await;

    assert_eq!(state.docs(), 270_269);
    assert!(state.ready().is_some());
    assert_eq!(state.status_word(), SEARCHABLE);
}

/// The reading that makes a refresh staircase legible: accepted climbs while
/// searchable is flat, and the status says which of the two is happening.
#[tokio::test]
async fn an_index_that_has_not_refreshed_reports_both_counts() {
    let index = FakeIndex::start().await;
    index.holding(0).publishes_after(5).accepted(900);

    let state = probe_against(&index).read().await;

    assert_eq!((state.docs(), state.accepted()), (0, Some(900)));
    assert_eq!(state.status_word(), INDEXING);
}

#[tokio::test]
async fn a_404_is_an_absent_index_rather_than_a_failure() {
    let index = FakeIndex::start().await;
    index.absent();

    assert_eq!(probe_against(&index).read().await, IndexState::Absent);
}

/// The distinction the reset depends on: an index whose primary is not
/// allocated has not told us it is gone.
#[tokio::test]
async fn an_unallocated_primary_is_not_read_as_an_absent_index() {
    let index = FakeIndex::start().await;
    index.answers_after(3);

    let state = probe_against(&index).read().await;

    assert!(!state.is_absent());
    assert!(state.ready().is_none() || state.status_word() == UNREADY);
}

#[tokio::test]
async fn an_unreachable_endpoint_is_unreadable_and_names_the_url() {
    let options = ConnectOptions {
        url: "http://127.0.0.1:1".to_string(),
        index: "wiki-articles".to_string(),
        request_timeout: Duration::from_millis(200),
    };
    let probe = StatsProbe::new(
        build_client(&options).unwrap(),
        "http://127.0.0.1:1",
        "wiki-articles",
    );

    let state = probe.read().await;

    assert!(!state.is_absent());
    assert!(state.describe().contains("cannot reach"), "{state:?}");
}

#[tokio::test]
async fn the_endpoint_is_the_one_the_engine_campaign_polls() {
    let index = FakeIndex::start().await;
    assert_eq!(
        probe_against(&index).endpoint(),
        format!("{}/wiki-articles/_stats", index.url())
    );
}

/// The last resort after the engine has stopped and the searchable count is
/// still short. It has to actually publish, or an index that never refreshes on
/// a timer would report a build of nothing.
#[tokio::test]
async fn a_settle_hint_publishes_what_the_index_accepted() {
    let index = FakeIndex::start().await;
    index.holding(0).publishes_after(1_000).accepted(4_200);
    let probe = probe_against(&index);

    assert_eq!(probe.read().await.docs(), 0);
    probe.settle_hint().await;

    assert_eq!(probe.read().await.docs(), 4_200);
}
