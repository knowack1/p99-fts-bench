//! The build rate is only a measurement if it knows when the build ended.
//!
//! Three ways a level can stop: the index reached what was submitted, it
//! stopped moving short of that, or the settle budget ran out. Only the first
//! is a build rate; the other two are floors, and the difference has to survive
//! into the CSV or a stalled index reads as a fast one.
use std::sync::Arc;
use std::time::Duration;

use super::*;
use crate::fakes::{quiet_notes, FakeVectorStore, Reply, SpokenNotes};
use crate::vstore::{IndexProbe, DEFAULT_VS_INDEX};

const A_TIMEOUT: Duration = Duration::from_secs(5);

fn brisk(settle: Duration, idle: Duration) -> WatchTiming {
    WatchTiming {
        poll_interval: Duration::from_millis(10),
        settle_timeout: settle,
        idle_timeout: idle,
    }
}

async fn watching(store: &FakeVectorStore, timing: WatchTiming) -> IndexWatch {
    IndexWatch::on(
        Arc::new(IndexProbe::new(store.url(), "wiki", DEFAULT_VS_INDEX, A_TIMEOUT).unwrap()),
        timing,
    )
}

#[tokio::test]
async fn a_watch_that_is_off_reports_nothing_rather_than_zero() {
    let watch = IndexWatch::off();
    assert!(!watch.is_on());
    let level = watch.begin(&quiet_notes()).await.unwrap();
    assert_eq!(level.finish(500).await.unwrap(), None);
}

#[tokio::test]
async fn a_level_that_catches_up_is_settled_with_no_lag() {
    let store = FakeVectorStore::start(Reply::Serving(0)).await;
    let watch = watching(
        &store,
        brisk(Duration::from_secs(5), Duration::from_secs(5)),
    )
    .await;

    let level = watch.begin(&quiet_notes()).await.unwrap();
    store.standing(Reply::Serving(500));
    let build = level.finish(500).await.unwrap().unwrap();

    assert_eq!(build.docs, 500);
    assert_eq!(build.lag_docs, 0);
    assert!(build.settled);
    assert!(build.docs_per_s > 0.0);
}

/// The number the chart exists for: the client stopped at 500, the index was
/// still at 200, and the build kept running afterwards.
#[tokio::test]
async fn an_index_behind_at_submit_end_reports_that_lag_and_still_settles() {
    let store = FakeVectorStore::start(Reply::Serving(0)).await;
    let watch = watching(
        &store,
        brisk(Duration::from_secs(5), Duration::from_secs(5)),
    )
    .await;

    let level = watch.begin(&quiet_notes()).await.unwrap();
    store.then(&[
        Reply::Serving(200),
        Reply::Serving(350),
        Reply::Serving(500),
    ]);
    let build = level.finish(500).await.unwrap().unwrap();

    assert_eq!(build.lag_docs, 300);
    assert_eq!(build.docs, 500);
    assert!(build.settled);
}

#[tokio::test]
async fn an_index_that_stops_short_is_not_settled() {
    let store = FakeVectorStore::start(Reply::Serving(0)).await;
    let watch = watching(
        &store,
        brisk(Duration::from_secs(10), Duration::from_millis(50)),
    )
    .await;

    let level = watch.begin(&quiet_notes()).await.unwrap();
    store.standing(Reply::Serving(300));
    let build = level.finish(500).await.unwrap().unwrap();

    assert!(!build.settled);
    assert_eq!(build.docs, 300);
    assert_eq!(build.lag_docs, 200);
}

/// A build that is still moving when the budget runs out is reported unsettled
/// rather than waited on forever.
#[tokio::test]
async fn a_build_that_outlasts_the_settle_budget_is_not_settled() {
    let store = FakeVectorStore::start(Reply::Serving(0)).await;
    let watch = watching(
        &store,
        brisk(Duration::from_millis(60), Duration::from_secs(10)),
    )
    .await;

    let level = watch.begin(&quiet_notes()).await.unwrap();
    store.then(&[
        Reply::Serving(10),
        Reply::Serving(20),
        Reply::Serving(30),
        Reply::Serving(40),
        Reply::Serving(50),
        Reply::Serving(60),
        Reply::Serving(70),
        Reply::Serving(80),
    ]);
    let build = level.finish(500).await.unwrap().unwrap();

    assert!(!build.settled);
    assert!(build.docs < 500);
}

/// The count only counts what THIS level added: a sink that keeps counting
/// across levels, or a --no-reset run, must not credit a level with the
/// documents already in the index when it started.
#[tokio::test]
async fn only_the_documents_this_level_added_are_counted() {
    let store = FakeVectorStore::start(Reply::Serving(1000)).await;
    let watch = watching(
        &store,
        brisk(Duration::from_secs(5), Duration::from_secs(5)),
    )
    .await;

    let level = watch.begin(&quiet_notes()).await.unwrap();
    store.standing(Reply::Serving(1500));
    let build = level.finish(500).await.unwrap().unwrap();

    assert_eq!(build.docs, 500);
    assert!(build.settled);
}

#[tokio::test]
async fn a_failed_poll_is_retried_and_said_rather_than_counted_as_zero() {
    let store = FakeVectorStore::start(Reply::Serving(0)).await;
    let spoken = SpokenNotes::default();
    let watch = watching(
        &store,
        brisk(Duration::from_secs(5), Duration::from_secs(5)),
    )
    .await;

    let level = watch
        .begin(&spoken.notes(Duration::from_secs(3600)))
        .await
        .unwrap();
    store.then(&[Reply::Failing(503), Reply::Serving(500)]);
    let build = level.finish(500).await.unwrap().unwrap();

    assert_eq!(build.docs, 500);
    assert!(spoken.mentions("index poll failed"));
}

/// An endpoint that never answers cannot produce a build rate, and must not
/// produce a zero one.
#[tokio::test]
async fn an_index_that_is_never_readable_fails_rather_than_reporting_zero() {
    let store = FakeVectorStore::start(Reply::Serving(0)).await;
    let watch = watching(
        &store,
        brisk(Duration::from_millis(50), Duration::from_secs(10)),
    )
    .await;

    let level = watch.begin(&quiet_notes()).await.unwrap();
    store.standing(Reply::Failing(503));
    let failure = level.finish(500).await.unwrap_err();

    assert!(format!("{failure:#}").contains("never readable"));
}

#[tokio::test]
async fn beginning_a_level_against_an_unreachable_store_fails_the_level() {
    let watch = IndexWatch::on(
        Arc::new(
            IndexProbe::new(
                "http://127.0.0.1:1",
                "wiki",
                "idx",
                Duration::from_millis(200),
            )
            .unwrap(),
        ),
        brisk(Duration::from_secs(1), Duration::from_secs(1)),
    );
    assert!(watch.begin(&quiet_notes()).await.is_err());
}
