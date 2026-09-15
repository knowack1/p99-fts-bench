use super::*;

use clap::CommandFactory;

fn parse(extra: &[&str]) -> Args {
    let mut argv = vec![
        "scyllasearch",
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

#[test]
fn the_parser_itself_is_well_formed() {
    Args::command().debug_assert();
}

#[test]
fn the_ladder_keeps_repeats_so_a_matrix_can_interleave_traversals() {
    let args = Args::try_parse_from([
        "scyllasearch",
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
    assert!(args
        .settings()
        .contains(&("query_classes".to_string(), "phrase,rare_term".to_string())));
}

#[test]
fn cql_is_the_interface_a_run_gets_unless_it_asks_for_the_other_one() {
    assert_eq!(parse(&[]).interface, Interface::Cql);
    assert_eq!(
        parse(&["--interface", "vector-store"]).interface,
        Interface::VectorStore
    );
}

/// `literal` re-parses per request the way the other engine's parser does, and
/// the way this bench's earlier read arm did; `prepared` is the opt-in.
#[test]
fn statements_are_literal_unless_the_run_asks_to_prepare_them() {
    assert_eq!(parse(&[]).statement, Statement::Literal);
    assert_eq!(
        parse(&["--statement", "prepared"]).statement.mode(),
        StatementMode::Prepared
    );
}

#[test]
fn the_header_names_the_interface_and_the_statement_mode_it_was_measured_in() {
    let settings = parse(&["--interface", "vector-store"]).settings();

    assert!(settings.contains(&("interface".to_string(), "vector-store".to_string())));
    assert!(settings.contains(&("statement".to_string(), "literal".to_string())));
}

#[test]
fn a_top_n_beyond_what_m1_allows_is_refused_by_the_flag_and_not_by_the_engine() {
    assert!(Args::try_parse_from([
        "scyllasearch",
        "--corpus",
        "c",
        "--queries",
        "q",
        "--concurrency",
        "8",
        "--limit",
        "1001",
    ])
    .is_err());
    assert_eq!(parse(&["--limit", "1000"]).limit, 1000);
}

#[test]
fn a_measured_window_of_zero_seconds_is_refused_and_a_warm_up_of_zero_is_not() {
    assert!(Args::try_parse_from([
        "scyllasearch",
        "--corpus",
        "c",
        "--queries",
        "q",
        "--concurrency",
        "8",
        "--duration",
        "0",
    ])
    .is_err());
    assert_eq!(parse(&["--warmup", "0"]).warmup, 0.0);
}

#[test]
fn zero_tokio_workers_is_refused_rather_than_reaching_the_runtime_builder() {
    assert!(Args::try_parse_from([
        "scyllasearch",
        "--corpus",
        "c",
        "--queries",
        "q",
        "--concurrency",
        "8",
        "--tokio-workers",
        "0",
    ])
    .is_err());
}

#[test]
fn the_worker_count_defaults_to_every_core_the_machine_reports() {
    assert_eq!(parse(&[]).tokio_workers(), available_cores());
    assert_eq!(parse(&["--tokio-workers", "3"]).tokio_workers(), 3);
}

#[test]
fn the_shape_a_row_carries_is_the_one_the_flags_asked_for() {
    let shape = parse(&["--limit", "100", "--fetch-documents"]).shape();

    assert_eq!(shape.engine, SCYLLADB);
    assert_eq!(shape.interface, CQL);
    assert_eq!(shape.limit, 100);
    assert!(shape.fetch_documents);
}

#[test]
fn the_reset_plan_is_built_from_the_flags_and_not_from_the_shipped_schema() {
    let plan = parse(&["--keyspace", "other", "--vs-index", "other_fts"]).reset_plan();

    assert_eq!(plan.keyspace, "other");
    assert_eq!(plan.index, "other_fts");
}

#[test]
fn a_run_may_be_forbidden_to_write_and_may_be_told_to_rebuild() {
    assert_eq!(parse(&[]).build_policy(), BuildPolicy::BUILD_IF_NEEDED);
    assert!(parse(&["--rebuild-index"]).build_policy().rebuild);
    assert!(!parse(&["--no-index-build"]).build_policy().may_build);
}

#[test]
fn the_loader_names_what_it_would_destroy() {
    let target = parse(&["--keyspace", "wiki", "--table", "articles"]).load_target();

    assert!(target.contains("wiki.articles"), "{target}");
    assert!(target.contains("articles_body_fts"), "{target}");
}

#[test]
fn a_header_without_a_latency_directory_says_off_rather_than_leaving_a_blank() {
    let settings = parse(&[]).settings();

    assert!(settings.contains(&("latencies_dir".to_string(), "off".to_string())));
}

/// The BM25 endpoint cannot return text, and finding that out after an index
/// build would cost the build.
#[test]
fn a_projection_the_bm25_endpoint_cannot_serve_is_refused_before_anything_connects() {
    let refused = parse(&["--interface", "vector-store", "--fetch-documents"])
        .validate()
        .unwrap_err();

    assert!(refused.contains("primary keys only"), "{refused}");
}

#[test]
fn every_other_combination_of_interface_and_projection_is_allowed() {
    assert!(parse(&["--fetch-documents"]).validate().is_ok());
    assert!(parse(&["--interface", "vector-store"]).validate().is_ok());
}
