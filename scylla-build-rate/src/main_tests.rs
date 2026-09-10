use super::*;

fn a_point(concurrency: usize, errors: u64) -> PointResult {
    PointResult {
        concurrency,
        docs: 100,
        errors,
        wall_s: 2.0,
        docs_per_s: 50.0,
        p50_ms: Some(1.5),
        p99_ms: Some(9.0),
    }
}

#[test]
fn a_clean_sweep_exits_zero() {
    assert_eq!(
        exit_code(&[a_point(8, 0), a_point(16, 0)], false),
        ExitCode::SUCCESS
    );
}

#[test]
fn a_sweep_with_any_failed_insert_exits_non_zero() {
    assert_eq!(
        exit_code(&[a_point(8, 0), a_point(16, 3)], false),
        ExitCode::FAILURE
    );
}

#[test]
fn an_aborted_sweep_exits_non_zero_even_if_every_point_was_clean() {
    assert_eq!(exit_code(&[a_point(8, 0)], true), ExitCode::FAILURE);
}

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
        Err(anyhow::anyhow!("truncated")),
        "sweep.csv"
    ));
}

#[test]
fn a_runtime_can_be_built_with_the_requested_worker_count() {
    assert!(build_runtime(3).is_ok());
}

fn a_topology() -> Topology {
    Topology {
        scylla_version: "2026.3.0-rc2".to_string(),
        routing: "DefaultPolicy(token_aware)".to_string(),
        compression: "None".to_string(),
        driver_version: "1.8.0".to_string(),
        protocol_version: "4".to_string(),
        runtime: "tokio multi_thread workers:8".to_string(),
        shard_aware: "true".to_string(),
        shards: "127.0.0.1:9042=shards:3".to_string(),
        connections: "3".to_string(),
        tablets: "false".to_string(),
    }
}

#[test]
fn the_banner_names_the_engine_the_driver_and_the_runtime() {
    let lines = topology_lines(&a_topology());
    assert!(lines[0].contains("scylla 2026.3.0-rc2"));
    assert!(lines[0].contains("driver 1.8.0"));
    assert!(lines[0].contains("workers:8"));
}

/// A flat curve is only interpretable next to the shard counts, so the banner
/// has to carry them before the first level runs.
#[test]
fn the_banner_names_the_shard_topology() {
    let lines = topology_lines(&a_topology());
    assert!(lines[1].contains("shard_aware=true"));
    assert!(lines[1].contains("shards:3"));
    assert!(lines[1].contains("tablets=false"));
}

#[test]
fn an_abort_says_what_went_wrong_and_where_the_measured_levels_are() {
    let lines = abort_lines(&anyhow::anyhow!("truncated"), "/runs/sweep.csv");
    assert!(lines[0].contains("sweep aborted") && lines[0].contains("truncated"));
    assert!(lines[1].contains("/runs/sweep.csv"));
}

#[test]
fn an_abort_keeps_the_whole_error_chain() {
    let cause = anyhow::anyhow!("line 4242").context("cannot read corpus");
    assert!(abort_lines(&cause, "sweep.csv")[0].contains("line 4242"));
}

#[test]
fn a_fresh_interrupt_watch_has_not_fired() {
    let runtime = build_runtime(1).unwrap();
    assert!(!runtime.block_on(async { watch_for_interrupt() }).is_set());
}
