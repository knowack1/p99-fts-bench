//! What a level must get right is the offer it makes: exactly `concurrency`
//! requests outstanding, a producer that cannot race the whole corpus into
//! memory, and counts that mean the same thing whether a request carried one
//! document or five hundred.
use std::time::Duration;

use anyhow::anyhow;

use super::*;
use crate::test_support::{no_counter, quiet_notes, FakeInserter, SpokenNotes};

/// A work item carrying a known number of documents, so a test can offer one
/// document per request or many without needing an engine.
#[derive(Debug, Clone, PartialEq)]
struct Batch(u64);

impl WorkItem for Batch {
    fn docs(&self) -> u64 {
        self.0
    }
}

const ONE_DOC: Loader = Loader {
    engine: "scylladb",
    shape: Shape::ONE_DOCUMENT,
};

fn batched(batch_size: usize) -> Loader {
    Loader {
        engine: "opensearch",
        shape: Shape {
            batch_size,
            queue_depth: QUEUE_DEPTH_PER_WORKER,
        },
    }
}

fn a_source(requests: usize, docs_each: u64) -> impl Source<Work = Batch> {
    (0..requests).map(move |_| Ok(Batch(docs_each)))
}

fn a_truncated_source(good: usize, docs_each: u64) -> impl Source<Work = Batch> {
    a_source(good, docs_each).chain(std::iter::once(Err(anyhow!("truncated JSONL line 4242"))))
}

/// A request that took `ms` and was sent exactly when it was due — the closed
/// loop's timing, where latency is service time and nothing queued.
fn took(ms: f64) -> Timing {
    Timing {
        latency_ms: ms,
        service_ms: ms,
        queue_ms: 0.0,
    }
}

async fn measure(
    inserter: &Arc<FakeInserter<Batch>>,
    source: impl Source<Work = Batch>,
    loader: Loader,
    concurrency: usize,
) -> Result<PointResult> {
    measure_at_rung(
        inserter,
        source,
        loader,
        Rung::closed_loop(concurrency),
        &quiet_notes(),
        &no_counter(),
        &Cancel::default(),
    )
    .await
}

/// The same level, offered at a fixed rate instead of as fast as it will go.
async fn measure_at(
    inserter: &Arc<FakeInserter<Batch>>,
    source: impl Source<Work = Batch>,
    loader: Loader,
    rung: Rung,
) -> Result<PointResult> {
    measure_at_rung(
        inserter,
        source,
        loader,
        rung,
        &quiet_notes(),
        &no_counter(),
        &Cancel::default(),
    )
    .await
}

#[tokio::test]
async fn a_point_delivers_every_document_once() {
    let inserter = Arc::new(FakeInserter::default());
    let result = measure(&inserter, a_source(5, 1), ONE_DOC, 2)
        .await
        .unwrap();

    assert_eq!((result.docs, result.errors), (5, 0));
    assert_eq!(inserter.docs(), 5);
}

/// The offer the ladder is a ladder of. More than N in flight would measure a
/// concurrency nobody asked for.
#[tokio::test]
async fn in_flight_never_exceeds_the_concurrency_level() {
    let inserter = Arc::new(FakeInserter::with_latency(Duration::from_millis(5)));
    measure(&inserter, a_source(40, 1), ONE_DOC, 4)
        .await
        .unwrap();

    assert!(
        inserter.max_in_flight() <= 4,
        "{}",
        inserter.max_in_flight()
    );
}

/// `c=64` and `c=64 batch=512` are not the same offer, and the product is what
/// a reader has to be told.
#[tokio::test]
async fn documents_in_flight_are_the_product_of_the_two_knobs() {
    let inserter = Arc::new(FakeInserter::with_latency(Duration::from_millis(5)));
    measure(&inserter, a_source(40, 500), batched(500), 4)
        .await
        .unwrap();

    assert!(inserter.max_docs_in_flight() <= 4 * 500);
    assert!(inserter.max_docs_in_flight() > 500, "nothing was in flight");
}

#[tokio::test]
async fn the_channel_is_bounded_so_the_producer_cannot_race_ahead() {
    let inserter = Arc::new(FakeInserter::with_latency(Duration::from_millis(20)));
    let inserter_for_task = Arc::clone(&inserter);
    let measuring =
        tokio::spawn(
            async move { measure(&inserter_for_task, a_source(10_000, 1), ONE_DOC, 1).await },
        );
    tokio::time::sleep(Duration::from_millis(30)).await;

    let buffered = inserter.requests();
    measuring.abort();

    assert!(
        buffered < 10_000,
        "the producer pulled the whole corpus in: {buffered}"
    );
}

// --- what the counts mean -------------------------------------------------

/// At one document per request the two numbers coincide, which is what lets a
/// reader check `batch_size` against `docs / requests`.
#[tokio::test]
async fn one_document_per_request_makes_docs_and_requests_agree() {
    let inserter = Arc::new(FakeInserter::default());
    let result = measure(&inserter, a_source(7, 1), ONE_DOC, 2)
        .await
        .unwrap();

    assert_eq!((result.docs, result.requests), (7, 7));
    assert_eq!(result.batch_size, 1);
}

#[tokio::test]
async fn a_batching_loader_counts_documents_and_requests_apart() {
    let inserter = Arc::new(FakeInserter::default());
    let result = measure(&inserter, a_source(5, 100), batched(100), 2)
        .await
        .unwrap();

    assert_eq!((result.docs, result.requests), (500, 5));
    assert_eq!(result.docs / result.requests, result.batch_size as u64);
}

/// A request that never came back delivered nothing, so every document it
/// carried is an error rather than an unknown.
#[tokio::test]
async fn a_request_that_never_returned_loses_every_document_it_carried() {
    let inserter = Arc::new(FakeInserter::default().failing_at(&[2]));
    let result = measure(&inserter, a_source(4, 50), batched(50), 1)
        .await
        .unwrap();

    assert_eq!((result.docs, result.errors), (150, 50));
    assert_eq!((result.requests, result.failed_requests), (3, 1));
}

/// Only a request where everything landed contributes a latency sample: a
/// half-rejected reply came back early for a reason that is not speed.
#[tokio::test]
async fn a_partly_rejected_request_contributes_no_latency_sample() {
    let inserter = Arc::new(FakeInserter::default().rejecting(&[(2, 10)]));
    let result = measure(&inserter, a_source(3, 50), batched(50), 1)
        .await
        .unwrap();

    assert_eq!((result.docs, result.errors), (140, 10));
    assert_eq!((result.requests, result.failed_requests), (2, 1));
}

#[tokio::test]
async fn a_failure_is_said_out_loud_rather_than_left_to_the_csv() {
    let spoken = SpokenNotes::default();
    let inserter = Arc::new(FakeInserter::default().failing_at(&[1]));
    measure_at_rung(
        &inserter,
        a_source(3, 1),
        ONE_DOC,
        Rung::closed_loop(1),
        &spoken.notes(Duration::from_secs(3600)),
        &no_counter(),
        &Cancel::default(),
    )
    .await
    .unwrap();

    assert!(spoken.mentions("failed requests"), "{:?}", spoken.lines());
    assert!(
        spoken.mentions("the socket went away"),
        "{:?}",
        spoken.lines()
    );
}

/// Workers count in parallel, so "first" has to be the earliest failure by
/// clock rather than the earliest one this process happened to join.
#[test]
fn merging_workers_keeps_the_earliest_failure_not_the_last_joined() {
    let mut early = Counters::default();
    early.record_failure(1, anyhow!("the first thing that went wrong"));
    std::thread::sleep(Duration::from_millis(5));
    let mut late = Counters::default();
    late.record_failure(1, anyhow!("a later consequence"));

    late.merge(early);

    assert_eq!(late.first_error(), Some("the first thing that went wrong"));
    assert_eq!(late.failed_requests, 2);
}

#[test]
fn offered_is_what_landed_plus_what_did_not() {
    let mut counters = Counters::default();
    counters.record(50, &Accepted::CLEAN, took(1.0));
    counters.record_failure(50, anyhow!("gone"));

    assert_eq!(
        (counters.docs, counters.errors, counters.offered()),
        (50, 50, 100)
    );
}

// --- the ladder -----------------------------------------------------------

#[tokio::test]
async fn a_source_that_breaks_stops_the_level_rather_than_reporting_a_short_one() {
    let inserter = Arc::new(FakeInserter::<Batch>::default());
    let failed = measure(&inserter, a_truncated_source(3, 1), ONE_DOC, 1)
        .await
        .unwrap_err();

    assert!(format!("{failed:#}").contains("truncated JSONL line 4242"));
}

#[tokio::test]
async fn every_row_carries_the_engine_that_produced_it() {
    let inserter = Arc::new(FakeInserter::default());
    let scylla = measure(&inserter, a_source(1, 1), ONE_DOC, 1)
        .await
        .unwrap();
    let opensearch = measure(&inserter, a_source(1, 1), batched(1), 1)
        .await
        .unwrap();

    assert_eq!(scylla.engine, "scylladb");
    assert_eq!(opensearch.engine, "opensearch");
}

// --- what the level says out loud -----------------------------------------

/// `run-arm.sh` greps stderr for `docs in` to pull each level's result line. An
/// announcement carrying that phrase would hand it two lines per level where it
/// expects one, so the batch clause is silent where a request is a document.
#[test]
fn a_one_document_loader_announces_a_level_without_the_phrase_the_fleet_greps() {
    let said = announce_level(0, 2, Rung::closed_loop(8), ONE_DOC.at(8));

    assert_eq!(said, "[1/2] concurrency=8");
    assert!(!said.contains("docs in"));
}

#[test]
fn a_batching_loader_announces_the_documents_in_flight() {
    let said = announce_level(0, 2, Rung::closed_loop(8), batched(512).at(8));

    assert!(said.contains("batch=512"), "{said}");
    assert!(said.contains("4096 docs in flight"), "{said}");
}

#[test]
fn the_queue_is_bounded_in_requests_not_documents() {
    let shape = Shape {
        batch_size: 512,
        queue_depth: 2,
    };
    assert_eq!(shape.queue_capacity(64), 128);
    assert_eq!(Shape::ONE_DOCUMENT.queue_capacity(64), 640);
}

/// The rule is "every item landed", and the count is the only thing that says
/// so. A reply that rejected items without explaining itself must not put its
/// latency in the distribution or its request in the `docs / requests` divisor.
#[test]
fn a_reply_that_rejected_items_without_a_reason_is_still_a_failed_request() {
    let mut counters = Counters::default();
    counters.record(
        512,
        &Accepted {
            failed: 3,
            first_failure: None,
        },
        took(12.0),
    );

    assert_eq!(counters.requests, 0);
    assert_eq!(counters.failed_requests, 1);
    assert_eq!(counters.docs, 509);
    assert_eq!(counters.errors, 3);
    assert!(counters.latencies_ms.is_empty());
    assert_eq!(
        counters.first_error(),
        Some("3 item(s) rejected, no reason given")
    );
}

/// And the other way: a reply that rejected nothing is clean, so it earns its
/// latency sample.
#[test]
fn a_reply_that_rejected_nothing_contributes_its_latency() {
    let mut counters = Counters::default();
    counters.record(512, &Accepted::CLEAN, took(12.0));

    assert_eq!(counters.requests, 1);
    assert_eq!(counters.failed_requests, 0);
    assert_eq!(counters.docs, 512);
    assert_eq!(counters.latencies_ms, vec![12.0]);
}

// --- which ladder is the axis ---------------------------------------------

#[test]
fn without_a_rate_every_concurrency_level_is_a_closed_loop_rung() {
    let rungs = Rung::ladder(&[4, 8, 16], None).unwrap();

    assert_eq!(rungs.len(), 3);
    assert!(rungs.iter().all(|rung| rung.target_docs_per_s.is_none()));
    assert_eq!(rungs[1].concurrency, 8);
}

#[test]
fn with_a_rate_the_single_concurrency_becomes_a_cap_every_rung_shares() {
    let rungs = Rung::ladder(&[512], Some(&[20_000, 40_000])).unwrap();

    assert_eq!(
        rungs,
        vec![Rung::at_rate(512, 20_000), Rung::at_rate(512, 40_000)]
    );
}

/// Both ladders at once would be a cross product: the cost of the two
/// multiplied, and a point that moved for two reasons at once.
#[test]
fn two_ladders_at_once_are_refused_rather_than_run_as_a_matrix() {
    let refused = Rung::ladder(&[4, 8], Some(&[20_000])).unwrap_err();

    assert!(refused.contains("single in-flight cap"), "{refused}");
    assert!(refused.contains("2 levels"), "{refused}");
}

#[test]
fn a_closed_loop_rung_schedules_nothing_and_a_paced_one_does() {
    assert!(!Rung::closed_loop(8).schedule().is_paced());
    assert_eq!(Rung::at_rate(512, 20_000).schedule().rate(), Some(20_000));
}

#[test]
fn a_paced_level_announces_the_rate_beside_the_cap_it_kept() {
    let said = announce_level(0, 3, Rung::at_rate(512, 20_000), ONE_DOC.at(512));

    assert!(said.starts_with("[1/3] concurrency=512"), "{said}");
    assert!(said.contains("target_docs_per_s=20000"), "{said}");
    assert!(!said.contains("docs in"), "{said}");
}

// --- what a paced level measures ------------------------------------------

#[tokio::test]
async fn a_paced_level_takes_the_time_its_rate_promised() {
    let inserter = Arc::new(FakeInserter::<Batch>::default());
    let result = measure_at(
        &inserter,
        a_source(200, 1),
        ONE_DOC,
        Rung::at_rate(4, 2_000),
    )
    .await
    .unwrap();

    assert_eq!(result.docs, 200);
    assert!(
        result.wall_s >= 0.08,
        "finished too early: {}",
        result.wall_s
    );
}

#[tokio::test]
async fn a_paced_level_records_what_it_offered_beside_what_it_achieved() {
    let inserter = Arc::new(FakeInserter::<Batch>::default());
    let result = measure_at(
        &inserter,
        a_source(200, 1),
        ONE_DOC,
        Rung::at_rate(4, 2_000),
    )
    .await
    .unwrap();

    assert_eq!(result.target_docs_per_s, Some(2_000));
    let ratio = result.achieved_offered_ratio.unwrap();
    assert!((0.5..=1.5).contains(&ratio), "ratio {ratio}");
    assert_eq!(result.saturated, Some(false));
}

/// An engine that cannot take the offered rate is the finding the column
/// exists for — not a slower point drawn as though it kept up.
#[tokio::test]
async fn a_rate_the_engine_cannot_take_is_marked_saturated() {
    let inserter = Arc::new(FakeInserter::<Batch>::with_latency(Duration::from_millis(
        20,
    )));
    let result = measure_at(
        &inserter,
        a_source(20, 1),
        ONE_DOC,
        Rung::at_rate(1, 10_000),
    )
    .await
    .unwrap();

    assert_eq!(result.saturated, Some(true));
    assert!(result.achieved_offered_ratio.unwrap() < ACHIEVED_FLOOR);
}

#[tokio::test]
async fn a_closed_loop_level_reports_no_offer_to_have_fallen_short_of() {
    let inserter = Arc::new(FakeInserter::<Batch>::default());
    let result = measure(&inserter, a_source(20, 1), ONE_DOC, 2)
        .await
        .unwrap();

    assert_eq!(result.target_docs_per_s, None);
    assert_eq!(result.achieved_offered_ratio, None);
    assert_eq!(result.saturated, None);
}

/// Closed loop is the paced path with `intended = now`, so it queues nothing —
/// by construction rather than by a branch that could rot.
#[tokio::test]
async fn a_closed_loop_level_queues_nothing() {
    let inserter = Arc::new(FakeInserter::<Batch>::with_latency(Duration::from_millis(
        2,
    )));
    let result = measure(&inserter, a_source(20, 1), ONE_DOC, 2)
        .await
        .unwrap();

    assert_eq!(result.queue_p99_ms, Some(0.0));
}

// --- the cap, and proving it did not bind ---------------------------------

#[tokio::test]
async fn the_in_flight_peak_is_recorded_and_never_exceeds_the_cap() {
    let inserter = Arc::new(FakeInserter::<Batch>::with_latency(Duration::from_millis(
        5,
    )));
    let result = measure(&inserter, a_source(60, 1), ONE_DOC, 4)
        .await
        .unwrap();

    assert!(result.in_flight_peak > 1, "{}", result.in_flight_peak);
    assert!(result.in_flight_peak <= 4, "{}", result.in_flight_peak);
}

/// The reading that separates "the engine saturated" from "the cap bound": a
/// paced rung well under the cap leaves headroom, and a short point with the
/// peak pinned at the cap is the harness, not the engine.
#[tokio::test]
async fn a_paced_level_well_under_its_cap_leaves_headroom_in_the_peak() {
    let inserter = Arc::new(FakeInserter::<Batch>::default());
    let result = measure_at(
        &inserter,
        a_source(100, 1),
        ONE_DOC,
        Rung::at_rate(64, 1_000),
    )
    .await
    .unwrap();

    assert!(result.in_flight_peak < 64, "{}", result.in_flight_peak);
}
