use super::*;

fn a_point(concurrency: usize, errors: u64) -> PointResult {
    PointResult {
        concurrency,
        batch_size: 512,
        docs: 100,
        errors,
        bulks: 10,
        failed_bulks: if errors > 0 { 1 } else { 0 },
        wall_s: 2.0,
        docs_per_s: 50.0,
        p50_ms: Some(1.5),
        p99_ms: Some(9.0),
    }
}

#[test]
fn a_clean_sweep_exits_zero() {
    assert_eq!(
        exit_code(&[a_point(24, 0), a_point(48, 0)], false),
        ExitCode::SUCCESS
    );
}

#[test]
fn a_sweep_with_any_undelivered_document_exits_non_zero() {
    assert_eq!(
        exit_code(&[a_point(24, 0), a_point(48, 3)], false),
        ExitCode::FAILURE
    );
}

#[test]
fn an_aborted_sweep_exits_non_zero_even_if_every_point_was_clean() {
    assert_eq!(exit_code(&[a_point(24, 0)], true), ExitCode::FAILURE);
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

fn a_cluster() -> Cluster {
    Cluster {
        opensearch_version: "2.19.0".to_string(),
        distribution: "opensearch".to_string(),
        client_version: "2.4.0".to_string(),
        http_client_version: "0.13.5".to_string(),
        runtime: "tokio multi_thread workers:8".to_string(),
        index: "wiki-articles".to_string(),
        index_shards: "1".to_string(),
        replicas: "0".to_string(),
        refresh_interval: "30s".to_string(),
        source_enabled: "true".to_string(),
        body_analyzer: "m1_parity".to_string(),
        write_pool: "node-0=write:8".to_string(),
        connection_pool: "reqwest-default(idle unbounded)".to_string(),
    }
}

#[test]
fn the_banner_names_the_engine_the_clients_and_the_runtime() {
    let lines = cluster_lines(&a_cluster());
    assert!(lines[0].contains("opensearch 2.19.0"));
    assert!(lines[0].contains("client 2.4.0"));
    assert!(lines[0].contains("http 0.13.5"));
    assert!(lines[0].contains("workers:8"));
}

/// A flat curve is only interpretable next to the refresh interval, the
/// analyzer and the shard count, so the banner carries them before the first
/// level runs.
#[test]
fn the_banner_names_the_index_shape_that_decides_comparability() {
    let lines = cluster_lines(&a_cluster());
    assert!(lines[1].contains("index=wiki-articles"));
    assert!(lines[1].contains("shards=1"));
    assert!(lines[1].contains("refresh_interval=30s"));
    assert!(lines[1].contains("analyzer=m1_parity"));
    assert!(lines[1].contains("write_pool=node-0=write:8"));
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

#[test]
fn the_shape_carries_both_knobs_from_the_command_line() {
    let args = Args::try_parse_from([
        "osrate",
        "--corpus",
        "c.jsonl",
        "--concurrency",
        "24",
        "--batch-size",
        "256",
        "--queue-depth",
        "3",
    ])
    .unwrap();
    let shape = shape(&args);
    assert_eq!((shape.batch_size, shape.queue_depth), (256, 3));
}

fn args_from(argv: &[&str]) -> Args {
    Args::try_parse_from(
        ["osrate", "--corpus", "c.jsonl", "--concurrency", "24"]
            .into_iter()
            .chain(argv.iter().copied()),
    )
    .unwrap()
}

/// The one line between a mistyped `--url` and someone's data, so it has to
/// name both the index and the endpoint.
#[test]
fn a_destructive_run_says_what_it_is_about_to_delete_and_where() {
    let said = reset_line(&args_from(&[
        "--url",
        "http://os-1:9200",
        "--index",
        "wiki-articles",
    ]));

    assert!(said.contains("DELETING INDEX wiki-articles"), "{said}");
    assert!(said.contains("http://os-1:9200"), "{said}");
    assert!(said.contains("ramindex"), "{said}");
}

#[test]
fn a_run_that_keeps_the_index_says_what_that_costs() {
    let said = reset_line(&args_from(&["--no-reset"]));

    assert!(said.contains("--no-reset"), "{said}");
    assert!(said.contains("only the first measures a build"), "{said}");
}
