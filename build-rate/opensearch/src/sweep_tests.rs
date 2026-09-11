use std::time::Duration;

use super::*;
use crate::fakes::{
    a_ladder, a_shape, a_source, a_truncated_source, quiet_notes, some_documents,
    CountingPreparation, FakeInserter, SpokenNotes,
};

const SLOW: Duration = Duration::from_millis(2);

async fn measure(
    inserter: &Arc<FakeInserter>,
    documents: usize,
    concurrency: usize,
    batch_size: usize,
) -> Result<PointResult> {
    measure_at_concurrency(
        inserter,
        a_source(documents, batch_size),
        concurrency,
        a_shape(batch_size),
        &quiet_notes(),
    )
    .await
}

fn an_inserter(latency: Duration) -> Arc<FakeInserter> {
    Arc::new(FakeInserter::with_latency(latency))
}

async fn sweep_levels(
    inserter: &Arc<FakeInserter>,
    documents: usize,
    levels: &[usize],
    batch_size: usize,
) -> Result<Vec<PointResult>> {
    let mut results = Vec::new();
    let mut collect = |result: PointResult| {
        results.push(result);
        Ok(())
    };
    run_sweep(
        a_ladder(Arc::clone(inserter), levels, a_shape(batch_size)),
        || Ok(a_source(documents, batch_size)),
        &quiet_notes(),
        &Cancel::default(),
        &mut collect,
    )
    .await?;
    Ok(results)
}

#[tokio::test]
async fn a_point_delivers_every_document_once() {
    let inserter = an_inserter(Duration::ZERO);
    let result = measure(&inserter, 50, 4, 5).await.unwrap();
    assert_eq!(
        (result.docs, result.errors, inserter.docs()),
        (50, 0, 50)
    );
}

#[tokio::test]
async fn a_point_sends_one_bulk_per_batch() {
    let inserter = an_inserter(Duration::ZERO);
    let result = measure(&inserter, 50, 4, 10).await.unwrap();
    assert_eq!((result.bulks, inserter.bulks()), (5, 5));
}

#[tokio::test]
async fn a_point_sends_the_documents_it_was_given() {
    let inserter = an_inserter(Duration::ZERO);
    measure(&inserter, 10, 3, 2).await.unwrap();
    let mut sent = inserter.documents_seen();
    sent.sort_by_key(|document| document.page_id);
    assert_eq!(sent, some_documents(10));
}

#[tokio::test]
async fn the_batch_size_is_what_reaches_each_request() {
    let inserter = an_inserter(Duration::ZERO);
    measure(&inserter, 12, 2, 4).await.unwrap();
    assert_eq!(inserter.batch_sizes(), vec![4, 4, 4]);
}

#[tokio::test]
async fn the_batch_size_reaches_the_point_it_was_measured_at() {
    let result = measure(&an_inserter(Duration::ZERO), 12, 2, 4).await.unwrap();
    assert_eq!((result.batch_size, result.docs_in_flight()), (4, 8));
}

#[tokio::test]
async fn in_flight_bulks_never_exceed_the_concurrency_level() {
    let inserter = an_inserter(SLOW);
    measure(&inserter, 400, 5, 10).await.unwrap();
    assert!(inserter.max_in_flight() <= 5);
}

#[tokio::test]
async fn in_flight_bulks_actually_reach_the_concurrency_level() {
    let inserter = an_inserter(SLOW);
    measure(&inserter, 400, 5, 10).await.unwrap();
    assert_eq!(inserter.max_in_flight(), 5);
}

/// The number to quote when comparing this against a single-document loader:
/// `c=5 batch=10` offers 50 documents at once, not 5.
#[tokio::test]
async fn documents_in_flight_are_the_concurrency_times_the_batch_size() {
    let inserter = an_inserter(SLOW);
    measure(&inserter, 400, 5, 10).await.unwrap();
    assert_eq!(inserter.max_docs_in_flight(), 50);
}

#[tokio::test]
async fn a_larger_batch_puts_more_documents_in_flight_at_the_same_concurrency() {
    let small = an_inserter(SLOW);
    let large = an_inserter(SLOW);
    measure(&small, 400, 4, 5).await.unwrap();
    measure(&large, 400, 4, 25).await.unwrap();
    assert!(large.max_docs_in_flight() > small.max_docs_in_flight());
    assert_eq!(large.max_in_flight(), small.max_in_flight());
}

#[tokio::test]
async fn a_higher_level_puts_more_bulks_in_flight() {
    let low = an_inserter(SLOW);
    let high = an_inserter(SLOW);
    measure(&low, 400, 2, 5).await.unwrap();
    measure(&high, 400, 8, 5).await.unwrap();
    assert!(high.max_in_flight() > low.max_in_flight());
}

/// A batch that never came back delivered nothing, so every document it
/// carried is an error rather than an unknown.
#[tokio::test]
async fn a_bulk_that_failed_whole_costs_every_document_it_carried() {
    let inserter = Arc::new(FakeInserter::new().failing_at(&[2]));
    let result = measure_at_concurrency(
        &inserter,
        a_source(20, 5),
        2,
        a_shape(5),
        &quiet_notes(),
    )
    .await
    .unwrap();
    assert_eq!(
        (result.docs, result.errors, result.bulks, result.failed_bulks),
        (15, 5, 3, 1)
    );
}

/// OpenSearch reports item failures inside a 200, so a partly-rejected batch
/// has to cost the documents it lost and no more.
#[tokio::test]
async fn a_partly_rejected_bulk_costs_only_the_items_that_were_refused() {
    let inserter = Arc::new(FakeInserter::new().rejecting(&[(1, 2)]));
    let result = measure_at_concurrency(
        &inserter,
        a_source(20, 5),
        1,
        a_shape(5),
        &quiet_notes(),
    )
    .await
    .unwrap();
    assert_eq!((result.docs, result.errors), (18, 2));
}

/// A half-rejected bulk came back early for a reason that is not the engine
/// indexing faster, so it must not join the latency distribution.
#[tokio::test]
async fn only_a_clean_bulk_contributes_a_latency_sample() {
    let inserter = Arc::new(FakeInserter::new().rejecting(&[(1, 1)]));
    let result = measure_at_concurrency(
        &inserter,
        a_source(20, 5),
        1,
        a_shape(5),
        &quiet_notes(),
    )
    .await
    .unwrap();
    assert_eq!((result.bulks, result.failed_bulks), (3, 1));
}

#[tokio::test]
async fn an_empty_corpus_produces_an_empty_point() {
    let result = measure(&an_inserter(Duration::ZERO), 0, 4, 5).await.unwrap();
    assert_eq!(
        (result.docs, result.errors, result.bulks, result.p99_ms),
        (0, 0, 0, None)
    );
}

#[tokio::test]
async fn a_point_measures_a_positive_wall_and_rate() {
    let result = measure(&an_inserter(Duration::from_millis(1)), 20, 4, 5)
        .await
        .unwrap();
    assert!(result.wall_s > 0.0 && result.docs_per_s > 0.0);
}

#[tokio::test]
async fn latency_reflects_the_time_the_bulk_took() {
    let result = measure(&an_inserter(Duration::from_millis(20)), 40, 8, 5)
        .await
        .unwrap();
    assert!(result.p50_ms.unwrap() >= 20.0);
}

#[tokio::test]
async fn the_channel_is_bounded_so_the_producer_cannot_race_ahead() {
    let capacity = a_shape(5).queue_capacity(1);
    let (sender, receiver) = async_channel::bounded::<DocumentBatch>(capacity);
    let producer = tokio::task::spawn_blocking(move || fill_channel(sender, a_source(500, 5)));
    tokio::time::sleep(Duration::from_millis(20)).await;
    assert!(receiver.len() <= capacity);
    drop(receiver);
    producer.await.unwrap().unwrap();
}

/// The bound is in batches, so the documents buffered ahead of the workers
/// scale with the batch size — which is why the depth default is small.
#[tokio::test]
async fn the_queue_bound_counts_batches_per_worker() {
    assert_eq!(a_shape(512).queue_capacity(8), 16);
}

#[tokio::test]
async fn a_queue_depth_of_zero_still_leaves_room_for_one_batch() {
    let shape = Shape {
        batch_size: 512,
        queue_depth: 0,
    };
    assert_eq!(shape.queue_capacity(8), 1);
}

#[tokio::test]
async fn the_producer_stops_when_every_worker_is_gone() {
    let (sender, receiver) = async_channel::bounded::<DocumentBatch>(1);
    drop(receiver);
    assert!(fill_channel(sender, a_source(500, 5)).is_ok());
}

#[tokio::test]
async fn a_sweep_returns_one_result_per_level_in_order() {
    let results = sweep_levels(&an_inserter(Duration::ZERO), 12, &[2, 4, 8], 3)
        .await
        .unwrap();
    let levels: Vec<usize> = results.iter().map(|result| result.concurrency).collect();
    assert_eq!(levels, vec![2, 4, 8]);
}

#[tokio::test]
async fn a_repeated_level_is_measured_twice() {
    let inserter = an_inserter(Duration::ZERO);
    let results = sweep_levels(&inserter, 6, &[4, 4], 2).await.unwrap();
    assert_eq!((results.len(), inserter.docs()), (2, 12));
}

#[tokio::test]
async fn each_level_reads_the_corpus_from_the_start() {
    let inserter = an_inserter(Duration::ZERO);
    sweep_levels(&inserter, 5, &[2, 2, 2], 2).await.unwrap();
    assert_eq!(inserter.docs(), 15);
}

#[tokio::test]
async fn every_level_carries_the_batch_size_the_ladder_was_run_at() {
    let results = sweep_levels(&an_inserter(Duration::ZERO), 12, &[2, 4], 6)
        .await
        .unwrap();
    assert!(results.iter().all(|result| result.batch_size == 6));
}

#[tokio::test]
async fn progress_counts_documents_not_bulks() {
    let spoken = SpokenNotes::default();
    let notes = spoken.notes(Duration::from_millis(5));
    measure_at_concurrency(
        &an_inserter(Duration::from_millis(1)),
        a_source(400, 10),
        4,
        a_shape(10),
        &notes,
    )
    .await
    .unwrap();
    assert!(spoken.mentions("docs/s (total"));
}

/// A ladder read next to a single-document one is misread unless the offer is
/// stated, so the level line says it before the level runs.
#[tokio::test]
async fn a_level_announces_the_documents_it_puts_in_flight() {
    let spoken = SpokenNotes::default();
    let mut collect = |_: PointResult| Ok(());
    run_sweep(
        a_ladder(an_inserter(Duration::ZERO), &[8], a_shape(5)),
        || Ok(a_source(20, 5)),
        &spoken.notes(Duration::from_secs(3600)),
        &Cancel::default(),
        &mut collect,
    )
    .await
    .unwrap();
    assert!(spoken.mentions("concurrency=8 batch=5 (40 docs in flight)"));
}

#[tokio::test]
async fn a_point_announces_its_first_failure() {
    let spoken = SpokenNotes::default();
    let inserter = Arc::new(FakeInserter::new().failing_at(&[1]));
    measure_at_concurrency(
        &inserter,
        a_source(20, 5),
        2,
        a_shape(5),
        &spoken.notes(Duration::from_secs(3600)),
    )
    .await
    .unwrap();
    assert!(spoken.mentions("the socket went away"));
}

/// Bulks and documents are separate counts, and the warning has to carry both
/// or "5 failures" reads as five documents when it was five requests.
#[tokio::test]
async fn a_failure_warning_names_both_the_bulks_and_the_documents() {
    let spoken = SpokenNotes::default();
    let inserter = Arc::new(FakeInserter::new().failing_at(&[1]));
    measure_at_concurrency(
        &inserter,
        a_source(20, 5),
        1,
        a_shape(5),
        &spoken.notes(Duration::from_secs(3600)),
    )
    .await
    .unwrap();
    assert!(spoken.mentions("1 failed bulks, 5 undelivered documents"));
}

#[tokio::test]
async fn a_clean_point_announces_no_failure() {
    let spoken = SpokenNotes::default();
    measure_at_concurrency(
        &an_inserter(Duration::ZERO),
        a_source(20, 5),
        2,
        a_shape(5),
        &spoken.notes(Duration::from_secs(3600)),
    )
    .await
    .unwrap();
    assert!(!spoken.mentions("!!"));
}

#[tokio::test]
async fn a_truncated_corpus_fails_the_point() {
    let outcome = measure_at_concurrency(
        &an_inserter(Duration::ZERO),
        a_truncated_source(20, 5),
        4,
        a_shape(5),
        &quiet_notes(),
    )
    .await;
    assert!(format!("{:#}", outcome.unwrap_err()).contains("truncated JSONL"));
}

#[tokio::test]
async fn a_failed_producer_leaves_no_bulk_running() {
    let inserter = an_inserter(Duration::from_millis(5));
    let _ = measure_at_concurrency(
        &inserter,
        a_truncated_source(20, 5),
        4,
        a_shape(5),
        &quiet_notes(),
    )
    .await;
    let settled = inserter.completed();
    tokio::time::sleep(Duration::from_millis(40)).await;
    assert_eq!(inserter.completed(), settled);
}

#[tokio::test]
async fn a_point_that_delivered_nothing_reports_no_latency() {
    let inserter = Arc::new(FakeInserter::new().failing_at(&(1..=4).collect::<Vec<_>>()));
    let result = measure_at_concurrency(
        &inserter,
        a_source(20, 5),
        4,
        a_shape(5),
        &quiet_notes(),
    )
    .await
    .unwrap();
    assert_eq!((result.docs, result.errors), (0, 20));
    assert!(result.p50_ms.is_none() && result.p99_ms.is_none());
}

#[tokio::test]
async fn a_cancelled_sweep_keeps_the_levels_it_measured() {
    let cancel = Cancel::default();
    let mut results = Vec::new();
    let stop_after_first = {
        let cancel = cancel.clone();
        move |result: PointResult| {
            results.push(result);
            cancel.trigger();
            Ok(())
        }
    };
    let mut collect = stop_after_first;
    let outcome = run_sweep(
        a_ladder(an_inserter(Duration::from_millis(1)), &[2, 4], a_shape(5)),
        || Ok(a_source(100, 5)),
        &quiet_notes(),
        &cancel,
        &mut collect,
    )
    .await;
    assert!(format!("{:#}", outcome.unwrap_err()).contains("interrupted"));
}

#[tokio::test]
async fn a_collector_failure_stops_the_sweep() {
    let mut collect = |_: PointResult| anyhow::bail!("the CSV went away");
    let outcome = run_sweep(
        a_ladder(an_inserter(Duration::ZERO), &[2, 4], a_shape(5)),
        || Ok(a_source(20, 5)),
        &quiet_notes(),
        &Cancel::default(),
        &mut collect,
    )
    .await;
    assert!(format!("{:#}", outcome.unwrap_err()).contains("CSV went away"));
}

#[tokio::test]
async fn an_unopenable_source_stops_the_sweep_before_any_bulk() {
    let inserter = an_inserter(Duration::ZERO);
    let mut collect = |_: PointResult| Ok(());
    let outcome = run_sweep(
        a_ladder(Arc::clone(&inserter), &[2], a_shape(5)),
        || -> Result<std::vec::IntoIter<Result<DocumentBatch>>> {
            anyhow::bail!("no such corpus")
        },
        &quiet_notes(),
        &Cancel::default(),
        &mut collect,
    )
    .await;
    assert!(outcome.is_err() && inserter.bulks() == 0);
}

fn a_test_point(concurrency: usize) -> Point {
    a_shape(10).at(concurrency)
}

fn some_clean_bulks(latencies: &[f64], docs_each: u64) -> Counters {
    let mut counters = Counters::default();
    for latency in latencies {
        counters.record_bulk(docs_each, &BulkOutcome::default(), *latency);
    }
    counters
}

#[test]
fn a_zero_wall_cannot_divide_by_zero() {
    let counters = some_clean_bulks(&[1.0], 10);
    assert_eq!(summarize(a_test_point(1), &counters, 0.0).docs_per_s, 0.0);
}

#[test]
fn summarize_divides_delivered_documents_by_the_wall() {
    let counters = some_clean_bulks(&[1.0, 2.0, 3.0, 4.0], 10);
    let result = summarize(a_test_point(4), &counters, 2.0);
    assert_eq!(
        (result.docs, result.bulks, result.docs_per_s, result.p99_ms),
        (40, 4, 20.0, Some(4.0))
    );
}

#[test]
fn summarize_keeps_failures_out_of_the_rate_and_the_percentiles() {
    let mut counters = some_clean_bulks(&[5.0], 10);
    counters.record_failed_bulk(10, anyhow::anyhow!("connection reset"));
    let result = summarize(a_test_point(2), &counters, 1.0);
    assert_eq!(
        (result.docs, result.errors, result.docs_per_s, result.bulks),
        (10, 10, 10.0, 1)
    );
}

#[test]
fn offered_documents_are_the_delivered_plus_the_failed() {
    let mut counters = some_clean_bulks(&[1.0], 10);
    counters.record_failed_bulk(10, anyhow::anyhow!("reset"));
    counters.record_bulk(
        10,
        &BulkOutcome {
            failed: 3,
            first_failure: Some("status 429".to_string()),
        },
        4.0,
    );
    assert_eq!((counters.docs, counters.doc_errors, counters.offered()), (17, 13, 30));
}

#[test]
fn first_failure_is_remembered_for_the_operator() {
    let mut counters = Counters::default();
    counters.record_failed_bulk(10, anyhow::anyhow!("connection reset"));
    counters.record_failed_bulk(10, anyhow::anyhow!("something later"));
    assert_eq!(counters.first_error(), Some("connection reset"));
}

#[test]
fn an_item_failure_is_remembered_the_same_way_a_dead_socket_is() {
    let mut counters = Counters::default();
    counters.record_bulk(
        10,
        &BulkOutcome {
            failed: 1,
            first_failure: Some("status 429 queue full".to_string()),
        },
        4.0,
    );
    assert_eq!(counters.first_error(), Some("status 429 queue full"));
}

#[test]
fn merging_workers_keeps_the_earliest_failure_not_the_last_joined() {
    let mut early = Counters::default();
    early.record_failed_bulk(1, anyhow::anyhow!("first by clock"));
    let mut late = Counters::default();
    late.record_failed_bulk(1, anyhow::anyhow!("later by clock"));

    let mut merged = late;
    merged.merge(early);
    assert_eq!(merged.first_error(), Some("first by clock"));
}

#[test]
fn merging_sums_what_every_worker_counted() {
    let mut left = some_clean_bulks(&[1.0], 10);
    let mut right = some_clean_bulks(&[2.0], 10);
    right.record_failed_bulk(10, anyhow::anyhow!("reset"));

    left.merge(right);
    assert_eq!(
        (left.docs, left.doc_errors, left.bulks, left.failed_bulks),
        (20, 10, 2, 1)
    );
}

#[test]
fn summarize_sorts_latencies_gathered_out_of_order() {
    let counters = some_clean_bulks(&[9.0, 1.0, 5.0], 10);
    assert_eq!(summarize(a_test_point(3), &counters, 1.0).p50_ms, Some(5.0));
}

#[tokio::test]
async fn a_cancel_that_never_fires_lets_a_waiter_hang() {
    let cancel = Cancel::default();
    assert!(!cancel.is_set());
    assert!(
        tokio::time::timeout(Duration::from_millis(20), cancel.wait())
            .await
            .is_err()
    );
}

#[tokio::test]
async fn a_cancel_triggered_before_the_wait_returns_at_once() {
    let cancel = Cancel::default();
    cancel.trigger();
    cancel.wait().await;
    assert!(cancel.is_set());
}

#[tokio::test]
async fn a_cancel_triggered_during_the_wait_wakes_the_waiter() {
    let cancel = Cancel::default();
    let waiting = cancel.clone();
    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(5)).await;
        waiting.trigger();
    });
    cancel.wait().await;
    assert!(cancel.is_set());
}

/// The reset runs per level, not once per ladder: a level that inherited the
/// last level's documents measures Lucene's update path instead of a build.
#[tokio::test]
async fn every_level_is_prepared_before_it_runs() {
    let preparation = CountingPreparation::default();
    let inserter = an_inserter(Duration::ZERO);
    let mut collect = |_: PointResult| Ok(());
    run_sweep(
        Ladder {
            inserter: Arc::clone(&inserter),
            before_level: &preparation,
            levels: &[2, 4, 8],
            shape: a_shape(5),
        },
        || Ok(a_source(20, 5)),
        &quiet_notes(),
        &Cancel::default(),
        &mut collect,
    )
    .await
    .unwrap();

    assert_eq!((preparation.prepared(), inserter.bulks()), (3, 12));
}

#[tokio::test]
async fn a_level_that_cannot_be_prepared_is_not_measured() {
    let preparation = CountingPreparation::failing_at(2);
    let inserter = an_inserter(Duration::ZERO);
    let mut measured = Vec::new();
    let mut collect = |result: PointResult| {
        measured.push(result.concurrency);
        Ok(())
    };
    let outcome = run_sweep(
        Ladder {
            inserter: Arc::clone(&inserter),
            before_level: &preparation,
            levels: &[2, 4, 8],
            shape: a_shape(5),
        },
        || Ok(a_source(20, 5)),
        &quiet_notes(),
        &Cancel::default(),
        &mut collect,
    )
    .await;

    assert!(format!("{:#}", outcome.unwrap_err()).contains("would not empty"));
    assert_eq!(
        measured,
        [2],
        "the level after the failed reset was measured"
    );
}

/// Preparation comes before the corpus is opened and before any bulk: an index
/// emptied after the first documents landed would take them with it.
#[tokio::test]
async fn nothing_is_offered_before_the_level_is_prepared() {
    let preparation = CountingPreparation::failing_at(1);
    let inserter = an_inserter(Duration::ZERO);
    let mut collect = |_: PointResult| Ok(());
    let outcome = run_sweep(
        Ladder {
            inserter: Arc::clone(&inserter),
            before_level: &preparation,
            levels: &[2],
            shape: a_shape(5),
        },
        || Ok(a_source(20, 5)),
        &quiet_notes(),
        &Cancel::default(),
        &mut collect,
    )
    .await;

    assert!(outcome.is_err() && inserter.bulks() == 0);
}
