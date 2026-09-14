//! What the watch must get right is when a build is over.
//!
//! Two clocks decide that, and they are not the same one. A build is *finished*
//! when what a search would find reaches what was submitted; it is *still
//! going* while the engine is accepting documents. On an engine where the
//! searchable count only advances at a refresh those come apart, and a watch
//! that conflated them would call every pause between refreshes a finished
//! build — and at `refresh_interval: -1`, every level a build of nothing.
use std::sync::Arc;
use std::time::Duration;

use anyhow::Result;

use super::*;
use crate::index::{IndexReading, ABSENT};
use crate::notes::Notes;
use crate::test_support::{
    a_reading, accepted_but_not_yet_searchable, no_counter, quiet_notes, ScriptedProbe, SpokenNotes,
};

fn brisk(settle: Duration, idle: Duration) -> WatchTiming {
    WatchTiming {
        poll_interval: Duration::from_millis(1),
        settle_timeout: settle,
        idle_timeout: idle,
    }
}

fn patient() -> WatchTiming {
    brisk(Duration::from_secs(5), Duration::from_secs(5))
}

fn watching(probe: Arc<ScriptedProbe>, timing: WatchTiming) -> IndexWatch {
    IndexWatch::on(probe, timing)
}

fn a_tape() -> Tape {
    Tape::new(1, 8, None)
}

async fn begin(watch: &IndexWatch, notes: &Notes) -> Result<LevelWatch> {
    watch.begin(notes, &a_tape(), &no_counter()).await
}

fn unreadable() -> IndexState {
    IndexState::Unreadable("503 from the index".to_string())
}

#[tokio::test]
async fn a_watch_that_is_off_reports_nothing_rather_than_zero() {
    let watch = IndexWatch::off();
    let level = begin(&watch, &quiet_notes()).await.unwrap();

    assert!(!watch.is_on());
    assert_eq!(level.finish(100).await.unwrap(), None);
}

#[tokio::test]
async fn a_level_that_catches_up_is_settled_with_no_lag() {
    let probe = Arc::new(ScriptedProbe::new(vec![a_reading(0)]));
    let watch = watching(Arc::clone(&probe), patient());
    let level = begin(&watch, &quiet_notes()).await.unwrap();
    probe.standing(a_reading(500));

    let build = level.finish(500).await.unwrap().unwrap();

    assert!(build.settled);
    assert_eq!((build.docs, build.lag_docs), (500, 0));
}

#[tokio::test]
async fn an_index_behind_at_submit_end_reports_that_lag_and_still_settles() {
    let probe = Arc::new(ScriptedProbe::new(vec![a_reading(0)]));
    let watch = watching(Arc::clone(&probe), patient());
    let level = begin(&watch, &quiet_notes()).await.unwrap();
    probe.then(&[a_reading(200), a_reading(350), a_reading(500)]);

    let build = level.finish(500).await.unwrap().unwrap();

    assert!(build.settled);
    assert_eq!((build.docs, build.lag_docs), (500, 300));
}

#[tokio::test]
async fn an_index_that_stops_short_is_not_settled() {
    let probe = Arc::new(ScriptedProbe::new(vec![a_reading(0)]));
    let watch = watching(
        Arc::clone(&probe),
        brisk(Duration::from_secs(5), Duration::from_millis(20)),
    );
    let level = begin(&watch, &quiet_notes()).await.unwrap();
    probe.standing(a_reading(300));

    let build = level.finish(500).await.unwrap().unwrap();

    assert!(!build.settled);
    assert_eq!(build.docs, 300);
}

#[tokio::test]
async fn a_build_that_outlasts_the_settle_budget_is_not_settled() {
    let probe = Arc::new(ScriptedProbe::new(vec![a_reading(0)]));
    let watch = watching(
        Arc::clone(&probe),
        brisk(Duration::from_millis(30), Duration::from_secs(5)),
    );
    let level = begin(&watch, &quiet_notes()).await.unwrap();
    probe.then(&[
        a_reading(10),
        a_reading(20),
        a_reading(30),
        a_reading(40),
        a_reading(50),
    ]);

    let build = level.finish(100_000).await.unwrap().unwrap();

    assert!(!build.settled);
}

#[tokio::test]
async fn only_the_documents_this_level_added_are_counted() {
    let probe = Arc::new(ScriptedProbe::new(vec![a_reading(1_000)]));
    let watch = watching(Arc::clone(&probe), patient());
    let level = begin(&watch, &quiet_notes()).await.unwrap();
    probe.standing(a_reading(1_500));

    let build = level.finish(500).await.unwrap().unwrap();

    assert_eq!(build.docs, 500);
    assert!(build.settled);
}

/// An index nobody could read is not an index that indexed nothing.
#[tokio::test]
async fn a_failed_poll_is_retried_and_said_rather_than_counted_as_zero() {
    let probe = Arc::new(ScriptedProbe::new(vec![a_reading(0)]));
    let watch = watching(Arc::clone(&probe), patient());
    let spoken = SpokenNotes::default();
    let notes = spoken.notes(Duration::from_secs(3600));
    let level = watch.begin(&notes, &a_tape(), &no_counter()).await.unwrap();
    probe.then(&[unreadable(), a_reading(500)]);

    let build = level.finish(500).await.unwrap().unwrap();

    assert!(build.settled);
    assert_eq!(build.docs, 500);
    assert!(spoken.mentions("index poll failed"), "{:?}", spoken.lines());
}

/// Taking an unreadable first poll as zero would credit this level with every
/// document already in the index.
#[tokio::test]
async fn an_index_that_cannot_be_read_before_the_level_fails_by_name() {
    let probe = Arc::new(ScriptedProbe::new(vec![unreadable()]));
    let watch = watching(probe, patient());

    let failed = begin(&watch, &quiet_notes())
        .await
        .err()
        .expect("an unreadable first poll must fail the level");
    let said = format!("{failed:#}");

    assert!(
        said.contains("could not be read before this level"),
        "{said}"
    );
    assert!(said.contains("503 from the index"), "{said}");
}

// --- the two clocks -------------------------------------------------------

/// The searchable count is flat between refreshes. Measuring idleness on it
/// would end the build during an ordinary pause; measuring it on what the
/// engine has accepted does not.
#[tokio::test]
async fn an_index_still_accepting_is_not_idle_even_while_nothing_is_searchable() {
    let probe = Arc::new(ScriptedProbe::new(vec![accepted_but_not_yet_searchable(
        0, 0,
    )]));
    let watch = watching(
        Arc::clone(&probe),
        brisk(Duration::from_secs(5), Duration::from_millis(40)),
    );
    let level = begin(&watch, &quiet_notes()).await.unwrap();
    probe.then(&[
        accepted_but_not_yet_searchable(0, 100),
        accepted_but_not_yet_searchable(0, 200),
        accepted_but_not_yet_searchable(0, 300),
        a_reading(300),
    ]);

    let build = level.finish(300).await.unwrap().unwrap();

    assert!(
        build.settled,
        "the flat searchable count ended the build early"
    );
    assert_eq!(build.docs, 300);
}

/// The last resort: the engine has taken everything and is publishing none of
/// it, which is what `refresh_interval: -1` looks like.
#[tokio::test]
async fn an_engine_holding_everything_unpublished_is_asked_once_to_publish() {
    let probe = Arc::new(
        ScriptedProbe::new(vec![accepted_but_not_yet_searchable(0, 0)]).publishing(a_reading(500)),
    );
    let watch = watching(
        Arc::clone(&probe),
        brisk(Duration::from_secs(5), Duration::from_millis(20)),
    );
    let level = begin(&watch, &quiet_notes()).await.unwrap();
    probe.standing(accepted_but_not_yet_searchable(0, 500));

    let build = level.finish(500).await.unwrap().unwrap();

    assert_eq!(probe.refreshes(), 1, "asked more than once, or not at all");
    assert!(build.settled);
    assert_eq!(build.docs, 500);
}

/// A build the harness had to publish is not the build the engine's own refresh
/// policy would have produced, and the row has to say which one it is.
#[tokio::test]
async fn a_build_that_had_to_be_published_says_so_in_its_status() {
    let probe = Arc::new(
        ScriptedProbe::new(vec![accepted_but_not_yet_searchable(0, 500)])
            .publishing(a_reading(500)),
    );
    let watch = watching(
        Arc::clone(&probe),
        brisk(Duration::from_secs(5), Duration::from_millis(20)),
    );
    let level = begin(&watch, &quiet_notes()).await.unwrap();

    let build = level.finish(500).await.unwrap().unwrap();

    assert_eq!(build.status, REFRESHED);
}

/// A build that genuinely stalled must not be rescued: the engine never took
/// the documents, so there is nothing to publish and the floor is the finding.
#[tokio::test]
async fn an_engine_that_never_accepted_the_documents_is_not_asked_to_publish() {
    let probe = Arc::new(ScriptedProbe::new(vec![accepted_but_not_yet_searchable(
        0, 0,
    )]));
    let watch = watching(
        Arc::clone(&probe),
        brisk(Duration::from_secs(5), Duration::from_millis(20)),
    );
    let level = begin(&watch, &quiet_notes()).await.unwrap();
    probe.standing(accepted_but_not_yet_searchable(0, 120));

    let build = level.finish(500).await.unwrap().unwrap();

    assert_eq!(probe.refreshes(), 0);
    assert!(!build.settled);
}

/// An engine with one counter cannot lag behind itself, so the hint can never
/// fire there — and the ScyllaDB half must behave exactly as it did.
#[tokio::test]
async fn an_engine_with_one_counter_is_never_asked_to_publish() {
    let probe = Arc::new(ScriptedProbe::new(vec![a_reading(0)]));
    let watch = watching(
        Arc::clone(&probe),
        brisk(Duration::from_secs(5), Duration::from_millis(20)),
    );
    let level = begin(&watch, &quiet_notes()).await.unwrap();
    probe.standing(a_reading(300));

    let build = level.finish(500).await.unwrap().unwrap();

    assert_eq!(probe.refreshes(), 0);
    assert!(!build.settled);
    assert_ne!(build.status, REFRESHED);
}

#[tokio::test]
async fn an_absent_index_reads_as_absent_rather_than_as_a_failed_poll() {
    let probe = Arc::new(ScriptedProbe::new(vec![IndexState::Absent]));
    let watch = watching(
        Arc::clone(&probe),
        brisk(Duration::from_millis(30), Duration::from_millis(20)),
    );
    let level = begin(&watch, &quiet_notes()).await.unwrap();

    let build = level.finish(10).await.unwrap().unwrap();

    assert_eq!(build.status, ABSENT);
    assert!(!build.settled);
}

#[tokio::test]
async fn a_reading_is_recorded_with_both_counts_where_the_engine_reports_them() {
    let state = accepted_but_not_yet_searchable(120, 900);
    assert_eq!((state.docs(), state.accepted()), (120, Some(900)));
    assert!(matches!(state, IndexState::Present(IndexReading { .. })));
}

/// A probe that was asked and did nothing must not colour the level
/// `refreshed`: that word says which refresh policy produced the number, and a
/// level nobody refreshed was produced by the engine's.
#[tokio::test]
async fn a_hint_that_publishes_nothing_does_not_claim_the_level_was_refreshed() {
    let probe = Arc::new(ScriptedProbe::new(vec![accepted_but_not_yet_searchable(
        0, 500,
    )]));
    let watch = watching(
        Arc::clone(&probe),
        brisk(Duration::from_secs(5), Duration::from_millis(20)),
    );
    let level = begin(&watch, &quiet_notes()).await.unwrap();

    let build = level.finish(500).await.unwrap().unwrap();

    assert_eq!(probe.refreshes(), 1, "it should still have asked once");
    assert_ne!(build.status, REFRESHED);
    assert!(!build.settled);
    assert_eq!(build.docs, 0);
}
