use super::*;

use clap::CommandFactory;

fn parse(extra: &[&str]) -> Args {
    let mut argv = vec![
        "ossearch",
        "--corpus",
        "data/corpus.jsonl",
        "--queries",
        "data/queries.json",
        "--concurrency",
        "1,8,64",
    ];
    argv.extend_from_slice(extra);
    Args::try_parse_from(argv).expect("these flags have to parse")
}

fn refused(extra: &[&str]) -> bool {
    let mut argv = vec![
        "ossearch",
        "--corpus",
        "c",
        "--queries",
        "q",
        "--concurrency",
        "8",
    ];
    argv.extend_from_slice(extra);
    Args::try_parse_from(argv).is_err()
}

#[test]
fn the_parser_itself_is_well_formed() {
    Args::command().debug_assert();
}

#[test]
fn the_ladder_keeps_repeats_so_a_matrix_can_interleave_traversals() {
    let args = Args::try_parse_from([
        "ossearch",
        "--corpus",
        "c",
        "--queries",
        "q",
        "--concurrency",
        "8,16,8,16",
    ])
    .unwrap();

    assert_eq!(args.concurrency.0, [8, 16, 8, 16]);
}

/// The absence of the flag has to reach `QuerySet::select` as an empty list —
/// a default rendered and parsed back would arrive as a request for one class
/// named after the word that stands for all of them.
#[test]
fn a_run_that_did_not_narrow_the_query_set_asks_for_every_class() {
    let args = parse(&[]);

    assert!(args.chosen_classes().is_empty());
    assert!(args
        .settings()
        .contains(&("query_classes".to_string(), "all".to_string())));
}

#[test]
fn a_run_that_did_narrow_it_keeps_the_order_it_asked_in() {
    let args = parse(&["--query-classes", "phrase,rare_term"]);

    assert_eq!(args.chosen_classes(), ["phrase", "rare_term"]);
}

#[test]
fn the_row_shape_says_opensearch_over_http() {
    let shape = parse(&[]).shape();

    assert_eq!(shape.engine, OPENSEARCH);
    assert_eq!(shape.interface, HTTP);
}

#[test]
fn the_query_shape_is_the_one_the_flags_asked_for() {
    let shape = parse(&["--limit", "100", "--fetch-documents"]).query_shape();

    assert_eq!(shape.limit, 100);
    assert!(shape.fetch_documents);
    assert_eq!(shape.field, "body");
    assert_eq!(shape.default_operator, "OR");
}

#[test]
fn a_top_n_beyond_what_the_other_engine_allows_is_refused_by_the_flag() {
    assert!(refused(&["--limit", "1001"]));
    assert_eq!(parse(&["--limit", "1000"]).limit, 1000);
}

#[test]
fn a_measured_window_of_zero_seconds_is_refused_and_a_warm_up_of_zero_is_not() {
    assert!(refused(&["--duration", "0"]));
    assert_eq!(parse(&["--warmup", "0"]).warmup, 0.0);
}

#[test]
fn zero_tokio_workers_is_refused_rather_than_reaching_the_runtime_builder() {
    assert!(refused(&["--tokio-workers", "0"]));
}

#[test]
fn a_zero_batch_carries_nothing_and_is_refused() {
    assert!(refused(&["--load-batch-size", "0"]));
    assert!(refused(&["--load-concurrency", "0"]));
}

/// The probe is a read, so it runs whether or not this run created the index —
/// an index somebody else built with the wrong analyzer is the case it exists
/// to catch.
#[test]
fn the_analyzer_is_checked_unless_the_run_says_not_to() {
    assert!(parse(&[]).checks_analyzer());
    assert!(!parse(&["--no-analyzer-check"]).checks_analyzer());
}

#[test]
fn the_header_names_the_analyzer_check_and_the_index_config() {
    let settings = parse(&["--index-config", "disk"]).settings();

    assert!(settings.contains(&("analyzer_check".to_string(), "true".to_string())));
    assert!(settings.contains(&("index_config".to_string(), "disk".to_string())));
}

#[test]
fn a_named_index_config_resolves_and_an_unknown_path_does_not() {
    assert!(parse(&["--index-config", "ramindex"])
        .index_config()
        .is_ok());
    assert!(parse(&["--index-config", "/nowhere.json"])
        .index_config()
        .is_err());
}

#[test]
fn a_run_may_be_forbidden_to_write_and_may_be_told_to_rebuild() {
    assert_eq!(parse(&[]).build_policy(), BuildPolicy::BUILD_IF_NEEDED);
    assert!(parse(&["--rebuild-index"]).build_policy().rebuild);
    assert!(!parse(&["--no-index-build"]).build_policy().may_build);
}

#[test]
fn the_loader_names_what_it_would_destroy() {
    let target = parse(&["--index", "wiki-articles"]).load_target();

    assert!(target.contains("wiki-articles"), "{target}");
    assert!(target.contains("localhost:9200"), "{target}");
}

#[test]
fn a_header_without_a_latency_directory_says_off_rather_than_leaving_a_blank() {
    let settings = parse(&[]).settings();

    assert!(settings.contains(&("latencies_dir".to_string(), "off".to_string())));
}
