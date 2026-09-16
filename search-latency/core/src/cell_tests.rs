use std::sync::Arc;
use std::time::Duration;

use build_rate_core::sweep::Cancel;

use super::*;
use crate::report::{Shape, SCYLLADB};
use crate::search::CQL;
use crate::test_support::{a_class, FakeSearcher};

fn settings(warmup_ms: u64, duration_ms: u64) -> CellSettings {
    CellSettings {
        warmup: Duration::from_millis(warmup_ms),
        duration: Duration::from_millis(duration_ms),
    }
}

fn a_shape() -> Shape {
    Shape {
        engine: SCYLLADB,
        interface: CQL,
        limit: 10,
        fetch_documents: false,
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_cell_keeps_exactly_the_requested_number_of_requests_in_flight() {
    let searcher = Arc::new(FakeSearcher::with_latency(Duration::from_millis(5)));
    let dyn_searcher: Arc<dyn Searcher> = searcher.clone();

    measure_cell(
        &dyn_searcher,
        &a_class("rare_term", &["kraken"]),
        4,
        &settings(0, 60),
        &Cancel::default(),
    )
    .await
    .unwrap();

    assert_eq!(searcher.peak_in_flight(), 4);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn the_warm_up_queries_are_not_counted() {
    let searcher = Arc::new(FakeSearcher::with_latency(Duration::from_millis(2)));
    let dyn_searcher: Arc<dyn Searcher> = searcher.clone();

    let measured = measure_cell(
        &dyn_searcher,
        &a_class("rare_term", &["kraken"]),
        1,
        &settings(40, 40),
        &Cancel::default(),
    )
    .await
    .unwrap();

    assert!(
        searcher.asked() > measured.counters.queries,
        "the fake answered {} requests and the cell counted {}",
        searcher.asked(),
        measured.counters.queries
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_cell_cycles_through_every_query_in_its_class() {
    let searcher = Arc::new(FakeSearcher::with_latency(Duration::from_millis(1)));
    let dyn_searcher: Arc<dyn Searcher> = searcher.clone();

    measure_cell(
        &dyn_searcher,
        &a_class("rare_term", &["kraken", "zeppelin"]),
        1,
        &settings(0, 40),
        &Cancel::default(),
    )
    .await
    .unwrap();

    let seen = searcher.queries_seen();
    assert!(seen.contains(&"kraken".to_string()), "{seen:?}");
    assert!(seen.contains(&"zeppelin".to_string()), "{seen:?}");
}

/// A failed request's time was spent on whatever went wrong, so it contributes
/// a count and a message and nothing to the distribution.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_failed_query_is_counted_but_never_timed() {
    let searcher = Arc::new(FakeSearcher::with_latency(Duration::from_millis(1)).failing_every(2));
    let dyn_searcher: Arc<dyn Searcher> = searcher.clone();

    let measured = measure_cell(
        &dyn_searcher,
        &a_class("rare_term", &["kraken"]),
        1,
        &settings(0, 40),
        &Cancel::default(),
    )
    .await
    .unwrap();

    assert!(measured.counters.errors > 0);
    assert_eq!(
        measured.counters.samples.len() as u64,
        measured.counters.queries
    );
    assert!(measured.counters.first_error().unwrap().contains("fail"));
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn an_empty_answer_is_timed_and_counted_apart() {
    let searcher: Arc<dyn Searcher> =
        Arc::new(FakeSearcher::with_latency(Duration::from_millis(1)).finding(0));

    let measured = measure_cell(
        &searcher,
        &a_class("rare_term", &["kraken"]),
        1,
        &settings(0, 30),
        &Cancel::default(),
    )
    .await
    .unwrap();

    assert!(measured.counters.queries > 0);
    assert_eq!(
        measured.counters.zero_hit_queries,
        measured.counters.queries
    );
    assert_eq!(measured.counters.hits, 0);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn an_interrupted_cell_says_where_it_was() {
    let searcher: Arc<dyn Searcher> =
        Arc::new(FakeSearcher::with_latency(Duration::from_millis(1)));
    let cancel = Cancel::default();
    cancel.trigger();

    let refused = measure_cell(
        &searcher,
        &a_class("phrase", &["\"united states\""]),
        8,
        &settings(0, 10_000),
        &cancel,
    )
    .await
    .unwrap_err()
    .to_string();

    assert!(refused.contains("concurrency=8"), "{refused}");
    assert!(refused.contains("phrase"), "{refused}");
}

#[test]
fn merging_keeps_the_earliest_failure_by_clock_not_by_join_order() {
    let mut first = Counters::default();
    first.record_failure(anyhow::anyhow!("early"));
    std::thread::sleep(Duration::from_millis(2));
    let mut late = Counters::default();
    late.record_failure(anyhow::anyhow!("late"));

    late.merge(first);

    assert_eq!(late.first_error(), Some("early"));
    assert_eq!(late.errors, 2);
}

#[test]
fn a_report_carries_the_percentiles_of_the_window_it_measured() {
    let mut counters = Counters::default();
    for (elapsed_s, latency) in [(4.0, 9.0), (0.0, 1.0), (2.0, 5.0), (1.0, 3.0), (3.0, 7.0)] {
        counters.record(Found::new(2), elapsed_s, latency);
    }
    let measured = Measured {
        counters,
        wall_s: 2.0,
    };

    let (result, sorted) = measured.into_report(&a_shape(), &a_class("rare_term", &["k"]), 4);

    let sorted_latencies: Vec<f64> = sorted.iter().map(|sample| sample.latency_ms).collect();
    assert_eq!(sorted_latencies, [1.0, 3.0, 5.0, 7.0, 9.0]);
    let sorted_elapsed: Vec<f64> = sorted.iter().map(|sample| sample.elapsed_s).collect();
    assert_eq!(sorted_elapsed, [0.0, 1.0, 2.0, 3.0, 4.0]);
    assert_eq!(result.p50_ms, Some(5.0));
    assert_eq!(result.p90_ms, Some(9.0));
    assert_eq!(result.max_ms, Some(9.0));
    assert_eq!(result.queries_per_s, 2.5);
    assert_eq!(result.hits_mean, Some(2.0));
    assert_eq!(result.concurrency, 4);
}

/// Nothing answered, so there is nothing to report — and a zero would be the
/// fastest point on the curve.
#[test]
fn a_cell_where_everything_failed_reports_no_latency_at_all() {
    let mut counters = Counters::default();
    counters.record_failure(anyhow::anyhow!("no route to host"));
    let measured = Measured {
        counters,
        wall_s: 1.0,
    };

    let (result, _) = measured.into_report(&a_shape(), &a_class("rare_term", &["k"]), 1);

    assert_eq!(result.p50_ms, None);
    assert_eq!(result.p99_ms, None);
    assert_eq!(result.hits_mean, None);
    assert_eq!(result.queries_per_s, 0.0);
}
