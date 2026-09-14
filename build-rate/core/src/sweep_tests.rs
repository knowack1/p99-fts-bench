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

async fn measure(
    inserter: &Arc<FakeInserter<Batch>>,
    source: impl Source<Work = Batch>,
    loader: Loader,
    concurrency: usize,
) -> Result<PointResult> {
    measure_at_concurrency(
        inserter,
        source,
        loader,
        concurrency,
        &quiet_notes(),
        &no_counter(),
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
    measure_at_concurrency(
        &inserter,
        a_source(3, 1),
        ONE_DOC,
        1,
        &spoken.notes(Duration::from_secs(3600)),
        &no_counter(),
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
    counters.record(50, &Accepted::CLEAN, 1.0);
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
    let said = announce_level(0, &[8, 16], ONE_DOC.at(8));

    assert_eq!(said, "[1/2] concurrency=8");
    assert!(!said.contains("docs in"));
}

#[test]
fn a_batching_loader_announces_the_documents_in_flight() {
    let said = announce_level(0, &[8, 16], batched(512).at(8));

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
