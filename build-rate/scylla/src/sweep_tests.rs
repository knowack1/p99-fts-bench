use std::time::Duration;

use super::*;
use crate::build_rate::IndexWatch;
use crate::fakes::{
    a_source, a_truncated_source, no_counter, quiet_notes, some_params, FakeInserter, OneInserter,
    SpokenNotes,
};
use crate::samples::SampleFiles;

const SLOW: Duration = Duration::from_millis(2);

async fn measure(
    inserter: &Arc<FakeInserter>,
    documents: usize,
    concurrency: usize,
) -> Result<PointResult> {
    measure_at_concurrency(
        inserter,
        a_source(documents),
        concurrency,
        &quiet_notes(),
        &no_counter(),
    )
    .await
}

fn watching_with<'a>(
    index: &'a IndexWatch,
    notes: &'a Notes,
    samples: Option<&'a SampleFiles>,
) -> Watchers<'a> {
    Watchers {
        index,
        notes,
        samples,
    }
}

fn an_inserter(latency: Duration) -> Arc<FakeInserter> {
    Arc::new(FakeInserter::with_latency(latency))
}

async fn sweep_levels(
    inserter: &Arc<FakeInserter>,
    documents: usize,
    levels: &[usize],
) -> Result<Vec<PointResult>> {
    let mut results = Vec::new();
    let mut collect = |result: PointResult| {
        results.push(result);
        Ok(())
    };
    let (index, notes) = (IndexWatch::off(), quiet_notes());
    run_sweep(
        &OneInserter(Arc::clone(inserter)),
        || Ok(a_source(documents)),
        levels,
        &watching_with(&index, &notes, None),
        &Cancel::default(),
        &mut collect,
    )
    .await?;
    Ok(results)
}

#[tokio::test]
async fn a_point_delivers_every_document_once() {
    let inserter = an_inserter(Duration::ZERO);
    let result = measure(&inserter, 50, 4).await.unwrap();
    assert_eq!((result.docs, result.errors, inserter.sent()), (50, 0, 50));
}

#[tokio::test]
async fn a_point_sends_the_documents_it_was_given() {
    let inserter = an_inserter(Duration::ZERO);
    measure(&inserter, 10, 3).await.unwrap();
    let mut sent = inserter.params_seen();
    sent.sort_by_key(|params| params.page_id);
    assert_eq!(sent, some_params(10));
}

#[tokio::test]
async fn in_flight_never_exceeds_the_concurrency_level() {
    let inserter = an_inserter(SLOW);
    measure(&inserter, 40, 5).await.unwrap();
    assert!(inserter.max_in_flight() <= 5);
}

#[tokio::test]
async fn in_flight_actually_reaches_the_concurrency_level() {
    let inserter = an_inserter(SLOW);
    measure(&inserter, 40, 5).await.unwrap();
    assert_eq!(inserter.max_in_flight(), 5);
}

#[tokio::test]
async fn a_higher_level_puts_more_requests_in_flight() {
    let low = an_inserter(SLOW);
    let high = an_inserter(SLOW);
    measure(&low, 40, 2).await.unwrap();
    measure(&high, 40, 8).await.unwrap();
    assert!(high.max_in_flight() > low.max_in_flight());
}

#[tokio::test]
async fn a_failed_insert_is_counted_and_the_rest_still_go() {
    let inserter = Arc::new(FakeInserter::new().failing_at(&[3, 7]));
    let result = measure_at_concurrency(&inserter, a_source(20), 4, &quiet_notes(), &no_counter())
        .await
        .unwrap();
    assert_eq!((result.docs, result.errors, inserter.sent()), (18, 2, 20));
}

#[tokio::test]
async fn an_empty_corpus_produces_an_empty_point() {
    let result = measure(&an_inserter(Duration::ZERO), 0, 4).await.unwrap();
    assert_eq!((result.docs, result.errors, result.p99_ms), (0, 0, None));
}

#[tokio::test]
async fn a_point_measures_a_positive_wall_and_rate() {
    let result = measure(&an_inserter(Duration::from_millis(1)), 20, 4)
        .await
        .unwrap();
    assert!(result.wall_s > 0.0 && result.docs_per_s > 0.0);
}

#[tokio::test]
async fn latency_reflects_the_time_the_insert_took() {
    let result = measure(&an_inserter(Duration::from_millis(20)), 8, 8)
        .await
        .unwrap();
    assert!(result.p50_ms.unwrap() >= 20.0);
}

#[tokio::test]
async fn the_channel_is_bounded_so_the_producer_cannot_race_ahead() {
    let (sender, receiver) = async_channel::bounded::<InsertParams>(QUEUE_DEPTH_PER_WORKER);
    let producer = tokio::task::spawn_blocking(move || fill_channel(sender, a_source(50)));
    tokio::time::sleep(Duration::from_millis(20)).await;
    assert!(receiver.len() <= QUEUE_DEPTH_PER_WORKER);
    drop(receiver);
    producer.await.unwrap().unwrap();
}

#[tokio::test]
async fn the_producer_stops_when_every_worker_is_gone() {
    let (sender, receiver) = async_channel::bounded::<InsertParams>(1);
    drop(receiver);
    assert!(fill_channel(sender, a_source(50)).is_ok());
}

#[tokio::test]
async fn a_sweep_returns_one_result_per_level_in_order() {
    let results = sweep_levels(&an_inserter(Duration::ZERO), 12, &[2, 4, 8])
        .await
        .unwrap();
    let levels: Vec<usize> = results.iter().map(|result| result.concurrency).collect();
    assert_eq!(levels, vec![2, 4, 8]);
}

#[tokio::test]
async fn a_repeated_level_is_measured_twice() {
    let inserter = an_inserter(Duration::ZERO);
    let results = sweep_levels(&inserter, 6, &[4, 4]).await.unwrap();
    assert_eq!((results.len(), inserter.sent()), (2, 12));
}

#[tokio::test]
async fn each_level_reads_the_corpus_from_the_start() {
    let inserter = an_inserter(Duration::ZERO);
    sweep_levels(&inserter, 5, &[2, 2, 2]).await.unwrap();
    assert_eq!(inserter.sent(), 15);
}

#[tokio::test]
async fn a_point_announces_its_first_failure() {
    let spoken = SpokenNotes::default();
    let inserter = Arc::new(FakeInserter::new().failing_at(&[1]));
    measure_at_concurrency(
        &inserter,
        a_source(4),
        2,
        &spoken.notes(Duration::from_secs(3600)),
        &no_counter(),
    )
    .await
    .unwrap();
    assert!(spoken.mentions("wire is busy"));
}

#[tokio::test]
async fn a_clean_point_announces_no_failure() {
    let spoken = SpokenNotes::default();
    measure_at_concurrency(
        &an_inserter(Duration::ZERO),
        a_source(4),
        2,
        &spoken.notes(Duration::from_secs(3600)),
        &no_counter(),
    )
    .await
    .unwrap();
    assert!(!spoken.mentions("!!"));
}

#[tokio::test]
async fn a_truncated_corpus_fails_the_point() {
    let outcome = measure_at_concurrency(
        &an_inserter(Duration::ZERO),
        a_truncated_source(4),
        4,
        &quiet_notes(),
        &no_counter(),
    )
    .await;
    assert!(format!("{:#}", outcome.unwrap_err()).contains("truncated JSONL"));
}

#[tokio::test]
async fn a_failed_producer_leaves_no_insert_running() {
    let inserter = an_inserter(Duration::from_millis(5));
    let _ = measure_at_concurrency(
        &inserter,
        a_truncated_source(4),
        4,
        &quiet_notes(),
        &no_counter(),
    )
    .await;
    let settled = inserter.completed();
    tokio::time::sleep(Duration::from_millis(40)).await;
    assert_eq!(inserter.completed(), settled);
}

#[tokio::test]
async fn a_point_that_delivered_nothing_reports_no_latency() {
    let inserter = Arc::new(FakeInserter::new().failing_at(&(1..=10).collect::<Vec<_>>()));
    let result = measure_at_concurrency(&inserter, a_source(10), 4, &quiet_notes(), &no_counter())
        .await
        .unwrap();
    assert_eq!((result.docs, result.errors), (0, 10));
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
    let (index, notes) = (IndexWatch::off(), quiet_notes());
    let outcome = run_sweep(
        &OneInserter(an_inserter(Duration::from_millis(1))),
        || Ok(a_source(20)),
        &[2, 4],
        &watching_with(&index, &notes, None),
        &cancel,
        &mut collect,
    )
    .await;
    assert!(format!("{:#}", outcome.unwrap_err()).contains("interrupted"));
}

#[tokio::test]
async fn a_collector_failure_stops_the_sweep() {
    let mut collect = |_: PointResult| anyhow::bail!("the CSV went away");
    let (index, notes) = (IndexWatch::off(), quiet_notes());
    let outcome = run_sweep(
        &OneInserter(an_inserter(Duration::ZERO)),
        || Ok(a_source(4)),
        &[2, 4],
        &watching_with(&index, &notes, None),
        &Cancel::default(),
        &mut collect,
    )
    .await;
    assert!(format!("{:#}", outcome.unwrap_err()).contains("CSV went away"));
}

#[tokio::test]
async fn an_unopenable_source_stops_the_sweep_before_any_insert() {
    let inserter = an_inserter(Duration::ZERO);
    let mut collect = |_: PointResult| Ok(());
    let (index, notes) = (IndexWatch::off(), quiet_notes());
    let outcome = run_sweep(
        &OneInserter(Arc::clone(&inserter)),
        || -> Result<std::vec::IntoIter<Result<InsertParams>>> { anyhow::bail!("no such corpus") },
        &[2],
        &watching_with(&index, &notes, None),
        &Cancel::default(),
        &mut collect,
    )
    .await;
    assert!(outcome.is_err() && inserter.sent() == 0);
}

#[test]
fn a_zero_wall_cannot_divide_by_zero() {
    let mut counters = Counters::default();
    counters.record_ok(1.0);
    assert_eq!(summarize(1, &counters, 0.0).docs_per_s, 0.0);
}

#[test]
fn summarize_divides_delivered_documents_by_the_wall() {
    let mut counters = Counters::default();
    for latency in [1.0, 2.0, 3.0, 4.0] {
        counters.record_ok(latency);
    }
    let result = summarize(4, &counters, 2.0);
    assert_eq!(
        (result.docs, result.docs_per_s, result.p99_ms),
        (4, 2.0, Some(4.0))
    );
}

#[test]
fn summarize_keeps_failures_out_of_the_rate_and_the_percentiles() {
    let mut counters = Counters::default();
    counters.record_ok(5.0);
    counters.record_error(anyhow::anyhow!("connection busy"));
    let result = summarize(2, &counters, 1.0);
    assert_eq!((result.docs, result.errors, result.docs_per_s), (1, 1, 1.0));
}

#[test]
fn first_failure_is_remembered_for_the_operator() {
    let mut counters = Counters::default();
    counters.record_error(anyhow::anyhow!("connection busy"));
    counters.record_error(anyhow::anyhow!("something later"));
    assert_eq!(counters.first_error(), Some("connection busy"));
}

#[test]
fn merging_workers_keeps_the_earliest_failure_not_the_last_joined() {
    let mut early = Counters::default();
    early.record_error(anyhow::anyhow!("first by clock"));
    let mut late = Counters::default();
    late.record_error(anyhow::anyhow!("later by clock"));

    let mut merged = late;
    merged.merge(early);
    assert_eq!(merged.first_error(), Some("first by clock"));
}

#[test]
fn merging_sums_what_every_worker_counted() {
    let mut left = Counters::default();
    left.record_ok(1.0);
    let mut right = Counters::default();
    right.record_ok(2.0);
    right.record_error(anyhow::anyhow!("busy"));

    left.merge(right);
    assert_eq!((left.ok, left.errors, left.done()), (2, 1, 3));
}

#[test]
fn summarize_sorts_latencies_gathered_out_of_order() {
    let mut counters = Counters::default();
    for latency in [9.0, 1.0, 5.0] {
        counters.record_ok(latency);
    }
    assert_eq!(summarize(3, &counters, 1.0).p50_ms, Some(5.0));
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
