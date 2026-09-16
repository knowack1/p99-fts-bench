use super::*;
use crate::report::{IndexBuild, SCYLLADB};

fn a_point(concurrency: usize, errors: u64) -> PointResult {
    PointResult {
        engine: SCYLLADB,
        concurrency,
        batch_size: 1,
        docs: 100,
        errors,
        requests: 100,
        failed_requests: errors,
        wall_s: 2.0,
        docs_per_s: 50.0,
        p50_ms: Some(1.5),
        p99_ms: Some(9.0),
        index: None,
        target_docs_per_s: None,
        achieved_offered_ratio: None,
        queue_p99_ms: None,
        in_flight_peak: 0,
        saturated: None,
    }
}

#[test]
fn a_clean_sweep_exits_zero() {
    assert_eq!(
        exit_code(&[a_point(8, 0), a_point(16, 0)], false),
        ExitCode::SUCCESS
    );
}

/// A level with failures in it is not a level a script should pick numbers off.
#[test]
fn any_failed_request_fails_the_run() {
    assert_eq!(
        exit_code(&[a_point(8, 0), a_point(16, 3)], false),
        ExitCode::FAILURE
    );
}

#[test]
fn an_abort_fails_the_run_even_where_every_measured_level_was_clean() {
    assert_eq!(exit_code(&[a_point(8, 0)], true), ExitCode::FAILURE);
}

/// The whole chain, not just the outermost message: an operator needs to know
/// which layer gave up.
#[test]
fn an_abort_keeps_the_whole_error_chain() {
    let exc = anyhow::anyhow!("connection refused").context("cannot reach the index");
    let said = abort_lines(&exc, "sweep.csv").join("\n");

    assert!(said.contains("cannot reach the index"), "{said}");
    assert!(said.contains("connection refused"), "{said}");
}

#[test]
fn an_abort_says_where_the_levels_it_measured_went() {
    let exc = anyhow::anyhow!("interrupted at concurrency=64");
    let said = abort_lines(&exc, "/mnt/nvme/work/results/high-rep1.csv").join("\n");

    assert!(
        said.contains("/mnt/nvme/work/results/high-rep1.csv"),
        "{said}"
    );
}

#[test]
fn the_summary_carries_a_watched_level_and_an_unwatched_one() {
    let watched = PointResult {
        index: Some(IndexBuild {
            docs: 500,
            docs_per_s: 250.0,
            lag_docs: 0,
            settle_s: 2.0,
            settled: true,
            status: "SERVING".to_string(),
        }),
        ..a_point(8, 0)
    };
    let table = summary_table(&[watched, a_point(16, 0)]);

    assert!(table.contains("500"), "{table}");
    assert!(table.contains(" - "), "{table}");
}

/// Not a failure: a `--max-docs 0` smoke run measures nothing and nothing went
/// wrong.
#[test]
fn a_sweep_that_measured_nothing_and_was_not_aborted_exits_zero() {
    assert_eq!(exit_code(&[], false), ExitCode::SUCCESS);
}

#[test]
fn a_successful_outcome_is_not_an_abort() {
    assert!(!report_outcome(Ok(()), "sweep.csv"));
}

#[test]
fn a_failed_outcome_is_an_abort() {
    assert!(report_outcome(
        Err(anyhow::anyhow!("the driver gave up")),
        "sweep.csv"
    ));
}

#[test]
fn a_runtime_can_be_built_with_the_requested_worker_count() {
    assert!(build_runtime(2).is_ok());
}

/// The handler arms the flag; nothing has pressed Ctrl-C yet.
#[test]
fn a_fresh_interrupt_watch_has_not_fired() {
    let runtime = build_runtime(1).unwrap();
    let cancel = runtime.block_on(async { watch_for_interrupt() });
    assert!(!cancel.is_set());
}

/// The shape `sweep::measure_at_concurrency` really creates: one blocking task
/// parked for the level's duration while other blocking work still has to
/// finish. At a pool of 1 this hangs instead of failing, so it is bounded.
#[test]
fn a_parked_blocking_task_does_not_starve_the_rest_of_the_pool() {
    let runtime = build_runtime(2).unwrap();
    let (release, parked) = std::sync::mpsc::channel::<()>();

    runtime.block_on(async move {
        let producer = tokio::task::spawn_blocking(move || {
            let _ = parked.recv();
        });
        let other = tokio::task::spawn_blocking(|| 7);

        let answer = tokio::time::timeout(std::time::Duration::from_secs(10), other)
            .await
            .expect("a second blocking task never ran: the pool is too small")
            .unwrap();
        assert_eq!(answer, 7);

        drop(release);
        producer.await.unwrap();
    });
}
