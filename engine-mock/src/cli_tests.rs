use super::*;

use clap::Parser;

use crate::index::Refresh;

/// The CQL launcher in `build-rate/HARNESS-LOCAL-RUNBOOK.md`, which refuses to
/// start unless 6080, 9042 and 9200 are all free.
const LOCAL_CQL_LAUNCHER: &[&str] = &[
    "--mode",
    "cql",
    "--host",
    "127.0.0.1",
    "--port",
    "9042",
    "--vs-port",
    "6080",
    "--vs-keyspace",
    "wiki",
    "--vs-index",
    "articles_body_fts",
    "--label",
    "local-cql-9042",
    "--report-interval",
    "30",
    "--stats-out",
    "/tmp/sinks/sink-cql-9042.json",
];

const LOCAL_HTTP_LAUNCHER: &[&str] = &[
    "--mode",
    "http",
    "--host",
    "127.0.0.1",
    "--port",
    "9200",
    "--os-refresh-interval-ms",
    "0",
    "--label",
    "local-http-9200",
    "--report-interval",
    "30",
    "--stats-out",
    "/tmp/sinks/sink-http-9200.json",
];

/// The CQL launcher in `build-rate/HARNESS-AWS-RUNBOOK.md`, which puts the
/// vector store at CQL port + 7000 so the two never collide.
const AWS_CQL_LAUNCHER: &[&str] = &[
    "--mode",
    "cql",
    "--host",
    "0.0.0.0",
    "--port",
    "9042",
    "--vs-port",
    "16042",
    "--label",
    "harness-9042",
    "--report-interval",
    "30",
    "--stats-out",
    "/tmp/sink-9042.json",
];

const AWS_HTTP_LAUNCHER: &[&str] = &[
    "--mode",
    "http",
    "--host",
    "0.0.0.0",
    "--port",
    "9200",
    "--label",
    "osrate-9200",
    "--report-interval",
    "30",
    "--stats-out",
    "/tmp/sink-9200.json",
];

fn parsed(flags: &[&str]) -> Args {
    let argv = std::iter::once("engine-mock").chain(flags.iter().copied());
    Args::try_parse_from(argv).expect("the flag surface two runbooks launch this binary by")
}

fn refused(flags: &[&str]) -> String {
    let argv = std::iter::once("engine-mock").chain(flags.iter().copied());
    Args::try_parse_from(argv)
        .expect_err("these flags must not parse")
        .to_string()
}

#[test]
fn each_mode_defaults_to_the_port_its_engine_listens_on() {
    assert_eq!(parsed(&["--mode", "http"]).port(), DEFAULT_HTTP_PORT);
    assert_eq!(parsed(&["--mode", "cql"]).port(), DEFAULT_CQL_PORT);
}

#[test]
fn an_explicit_port_wins_in_either_mode() {
    for mode in ["http", "cql"] {
        assert_eq!(parsed(&["--mode", mode, "--port", "7100"]).port(), 7100);
    }
}

#[test]
fn the_cql_mock_serves_the_endpoint_scyllarate_gates_on_by_default() {
    assert_eq!(
        parsed(&["--mode", "cql"]).vs_port(),
        Some(DEFAULT_VS_PORT),
        "scyllarate gates every level on the vector-store status endpoint"
    );
}

/// Part B runs N http mocks at once. A fixed default port here would make the
/// second of them fail to bind, and there is no index to report on the
/// OpenSearch-shaped side anyway.
#[test]
fn the_http_mock_serves_no_vector_store_by_default() {
    assert_eq!(parsed(&["--mode", "http"]).vs_port(), None);
}

#[test]
fn an_explicit_vector_store_port_is_obeyed_in_either_mode() {
    for mode in ["cql", "http"] {
        assert_eq!(
            parsed(&["--mode", mode, "--vs-port", "7000"]).vs_port(),
            Some(7000)
        );
    }
}

#[test]
fn zero_turns_the_vector_store_off_where_it_is_the_default() {
    assert_eq!(parsed(&["--mode", "cql", "--vs-port", "0"]).vs_port(), None);
}

/// The runbooks' CQL command lines pass `--vs-serving-delay-ms`, and they must
/// keep working now that the state has a mode-neutral name.
#[test]
fn the_serving_delay_flag_keeps_its_old_name() {
    let old = parsed(&["--mode", "cql", "--vs-serving-delay-ms", "500"]);
    let new = parsed(&["--mode", "http", "--index-ready-delay-ms", "500"]);
    assert_eq!(old.serving_delay(), new.serving_delay());
    assert_eq!(old.serving_delay(), Duration::from_millis(500));
}

/// `refresh_interval: -1` is a state a build-rate watch has to survive, so it
/// must not become a sub-millisecond delay on the way through the flag.
#[test]
fn a_negative_refresh_interval_stays_never() {
    let never = parsed(&["--mode", "http", "--os-refresh-interval-ms=-1"]);
    assert_eq!(never.refresh(), Refresh::Never);
    assert_eq!(
        parsed(&["--mode", "http"]).refresh(),
        Refresh::Every(Duration::ZERO)
    );
}

/// argparse took the value either way and the flag's own help text tells the
/// reader to pass a negative, so the form a reader will type must not be the
/// one that dies — at launch, on a mock a runbook has already backgrounded.
#[test]
fn a_negative_refresh_interval_can_be_written_the_way_the_help_text_reads() {
    for spelling in [
        vec!["--os-refresh-interval-ms", "-1"],
        vec!["--os-refresh-interval-ms=-1"],
    ] {
        let flags: Vec<&str> = ["--mode", "http"].into_iter().chain(spelling).collect();
        assert_eq!(parsed(&flags).refresh(), Refresh::Never, "{flags:?}");
    }
}

#[test]
fn tokio_workers_default_to_every_core_the_machine_reports() {
    assert_eq!(
        parsed(&["--mode", "http"]).tokio_workers(),
        available_cores()
    );
    assert_eq!(
        parsed(&["--mode", "http", "--tokio-workers", "3"]).tokio_workers(),
        3
    );
}

/// Zero reaches `Builder::worker_threads(0)`, which panics rather than
/// failing: a mock that took the flag would die after the fleet was up, with a
/// backtrace where a usage error belongs.
#[test]
fn zero_tokio_workers_is_refused_by_the_flag() {
    let said = refused(&["--mode", "http", "--tokio-workers", "0"]);
    assert!(said.contains("must be >= 1"), "{said}");
}

#[test]
fn a_duration_of_zero_runs_until_terminated() {
    assert_eq!(parsed(&["--mode", "http"]).duration(), None);
    assert_eq!(
        parsed(&["--mode", "http", "--duration", "1.5"]).duration(),
        Some(Duration::from_secs_f64(1.5))
    );
}

/// Both runbooks launch this binary by these exact flag names. A rename lands
/// as a run that never started — after the fleet is up and the corpus is
/// staged, with nothing but a launcher's exit code to say why.
#[test]
fn the_local_runbooks_launcher_command_lines_parse() {
    let cql = parsed(LOCAL_CQL_LAUNCHER);
    let http = parsed(LOCAL_HTTP_LAUNCHER);
    assert_eq!((cql.port(), cql.vs_port()), (9042, Some(6080)));
    assert_eq!((http.port(), http.vs_port()), (9200, None));
}

#[test]
fn the_aws_runbooks_launcher_command_lines_parse() {
    let cql = parsed(AWS_CQL_LAUNCHER);
    let http = parsed(AWS_HTTP_LAUNCHER);
    assert_eq!((cql.port(), cql.vs_port()), (9042, Some(16042)));
    assert_eq!((http.port(), http.vs_port()), (9200, None));
}

/// Both endpoints bind before anything is served, so a collision otherwise
/// fails on the second one with an address-in-use that a launcher reports as a
/// mock that would not start — true, but not why.
#[test]
fn one_port_cannot_serve_both_the_engine_and_the_vector_store() {
    let collided = parsed(&["--mode", "cql", "--port", "9042", "--vs-port", "9042"]);
    let apart = parsed(&["--mode", "cql", "--port", "9042"]);

    assert!(collided.refuse_a_port_collision().is_err());
    assert!(apart.refuse_a_port_collision().is_ok());
}

/// A zero report interval is a `sleep(0)` loop printing to stderr as fast as a
/// core allows, competing for the machine with the thing being measured; a
/// negative delay would reach `Duration::from_secs_f64` as a panic. Both are
/// refused at the flag rather than clamped, because a launcher that asked for
/// one is a launcher with a typo in it.
#[test]
fn a_flag_that_would_make_the_mock_the_constraint_is_refused_at_the_flag() {
    for refused in [
        vec!["--report-interval", "0"],
        vec!["--delay-ms", "-5"],
        vec!["--duration", "-1"],
        vec!["--tokio-workers", "0"],
        vec!["--vs-serving-delay-ms", "-1"],
    ] {
        let flags: Vec<&str> = ["--mode", "http"]
            .into_iter()
            .chain(refused.clone())
            .collect();
        assert!(
            Args::try_parse_from(["engine-mock"].into_iter().chain(flags)).is_err(),
            "{refused:?} was accepted"
        );
    }
}
