use clap::CommandFactory;

use super::*;

fn parse(argv: &[&str]) -> Args {
    Args::try_parse_from(std::iter::once("scyllarate").chain(argv.iter().copied())).unwrap()
}

fn a_minimal_command() -> Vec<&'static str> {
    vec!["--corpus", "c.jsonl", "--concurrency", "8"]
}

fn levels(raw: &str) -> Result<Vec<usize>, String> {
    Levels::from_str(raw).map(|parsed| parsed.0)
}

#[test]
fn the_parser_is_internally_consistent() {
    Args::command().debug_assert();
}

#[test]
fn parses_a_list_of_concurrency_levels() {
    assert_eq!(levels("8,16,32").unwrap(), vec![8, 16, 32]);
}

#[test]
fn keeps_a_repeated_level_so_a_warm_up_point_survives() {
    assert_eq!(levels("8,8,16").unwrap(), vec![8, 8, 16]);
}

#[test]
fn tolerates_whitespace_and_trailing_separators() {
    assert_eq!(levels(" 8 , 16 ,").unwrap(), vec![8, 16]);
}

#[test]
fn rejects_a_non_integer_level() {
    assert!(levels("8,many").unwrap_err().contains("not an integer"));
}

#[test]
fn rejects_a_level_below_one() {
    assert!(levels("8,0").unwrap_err().contains("must be >= 1"));
}

#[test]
fn rejects_an_empty_concurrency_list() {
    assert!(levels(",").unwrap_err().contains("at least one level"));
}

#[test]
fn splits_contact_points() {
    assert_eq!(
        Hosts::from_str("10.0.0.1, 10.0.0.2").unwrap().0,
        ["10.0.0.1", "10.0.0.2"]
    );
}

#[test]
fn rejects_an_empty_host_list() {
    assert!(Hosts::from_str(" ")
        .unwrap_err()
        .contains("at least one contact point"));
}

#[test]
fn hosts_print_back_the_way_they_were_given() {
    assert_eq!(Hosts::from_str("a,b").unwrap().to_string(), "a,b");
}

#[test]
fn resolves_a_consistency_level_by_name() {
    assert_eq!(
        parse_consistency("local_quorum").unwrap(),
        consistency_from_name("LOCAL_QUORUM").unwrap()
    );
}

#[test]
fn rejects_an_unknown_consistency_level() {
    assert!(parse_consistency("EVENTUALLY_MAYBE")
        .unwrap_err()
        .contains("unknown consistency"));
}

#[test]
fn the_parser_accepts_a_full_command_line() {
    let args = parse(&[
        "--corpus",
        "c.jsonl",
        "--concurrency",
        "8,16",
        "--hosts",
        "10.0.0.1,10.0.0.2",
        "--port",
        "19042",
        "--consistency",
        "LOCAL_QUORUM",
        "--tokio-workers",
        "8",
    ]);
    assert_eq!(
        (
            args.tokio_workers(),
            args.port,
            args.concurrency.0,
            args.hosts.0
        ),
        (
            8,
            19042,
            vec![8, 16],
            vec!["10.0.0.1".to_string(), "10.0.0.2".to_string()]
        )
    );
}

#[test]
fn the_parser_defaults_to_the_wiki_articles_table() {
    let args = parse(&a_minimal_command());
    assert_eq!(
        (
            args.keyspace.as_str(),
            args.table.as_str(),
            args.max_docs,
            args.out.as_str()
        ),
        ("wiki", "articles", 0, "-")
    );
}

#[test]
fn tokio_workers_default_to_every_core_the_machine_reports() {
    assert_eq!(
        parse(&a_minimal_command()).tokio_workers(),
        available_cores()
    );
}

#[test]
fn a_requested_worker_count_overrides_the_core_count() {
    let args = parse(&[
        "--corpus",
        "c.jsonl",
        "--concurrency",
        "8",
        "--tokio-workers",
        "3",
    ]);
    assert_eq!(args.tokio_workers(), 3);
}

#[test]
fn write_coalescing_is_on_unless_it_is_turned_off() {
    assert!(parse(&a_minimal_command()).write_coalescing());
    let mut argv = a_minimal_command();
    argv.push("--no-write-coalescing");
    assert!(!parse(&argv).write_coalescing());
}

#[test]
fn connect_options_carry_the_request_timeout_as_a_duration() {
    let args = parse(&[
        "--corpus",
        "c.jsonl",
        "--concurrency",
        "8",
        "--request-timeout",
        "2.5",
    ]);
    assert_eq!(
        args.connect_options().request_timeout,
        Duration::from_millis(2500)
    );
}

#[test]
fn settings_record_the_consistency_by_name() {
    let settings = parse(&a_minimal_command()).settings();
    assert!(settings.contains(&("consistency".to_string(), "LOCAL_ONE".to_string())));
}

#[test]
fn settings_record_the_corpus_and_the_document_limit() {
    let args = parse(&[
        "--corpus",
        "/tmp/small.jsonl",
        "--concurrency",
        "8",
        "--max-docs",
        "5000",
    ]);
    let settings = args.settings();
    assert!(settings.contains(&("corpus".to_string(), "/tmp/small.jsonl".to_string())));
    assert!(settings.contains(&("max_docs".to_string(), "5000".to_string())));
}

/// A chart made with the driver recording its own latency histogram is not the
/// same measurement as one made without, so the header has to say which it was.
#[test]
fn settings_record_whether_the_driver_was_collecting_metrics() {
    let settings = parse(&a_minimal_command()).settings();
    let recorded = settings
        .iter()
        .find(|(key, _)| key == "driver_metrics")
        .unwrap();
    assert_eq!(recorded.1, driver_metrics_state());
}

#[test]
fn settings_record_the_tokio_worker_count() {
    let args = parse(&[
        "--corpus",
        "c.jsonl",
        "--concurrency",
        "8",
        "--tokio-workers",
        "4",
    ]);
    assert!(args
        .settings()
        .contains(&("tokio_workers".to_string(), "4".to_string())));
}

#[test]
fn a_corpus_is_required() {
    assert!(Args::try_parse_from(["scyllarate", "--concurrency", "8"]).is_err());
}

#[test]
fn a_concurrency_ladder_is_required() {
    assert!(Args::try_parse_from(["scyllarate", "--corpus", "c.jsonl"]).is_err());
}
