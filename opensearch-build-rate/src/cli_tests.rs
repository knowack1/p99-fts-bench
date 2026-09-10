use clap::CommandFactory;

use super::*;

fn parse(argv: &[&str]) -> Args {
    Args::try_parse_from(std::iter::once("osrate").chain(argv.iter().copied())).unwrap()
}

fn a_minimal_command() -> Vec<&'static str> {
    vec!["--corpus", "c.jsonl", "--concurrency", "24"]
}

fn levels(raw: &str) -> Result<Vec<usize>, String> {
    Levels::from_str(raw).map(|parsed| parsed.0)
}

fn setting(args: &Args, key: &str) -> String {
    args.settings()
        .into_iter()
        .find(|(name, _)| name == key)
        .unwrap_or_else(|| panic!("no {key} in the header settings"))
        .1
}

#[test]
fn the_parser_is_internally_consistent() {
    Args::command().debug_assert();
}

#[test]
fn parses_a_list_of_concurrency_levels() {
    assert_eq!(levels("24,48,96").unwrap(), vec![24, 48, 96]);
}

#[test]
fn keeps_a_repeated_level_so_a_warm_up_point_survives() {
    assert_eq!(levels("24,24,48").unwrap(), vec![24, 24, 48]);
}

#[test]
fn tolerates_whitespace_and_trailing_separators() {
    assert_eq!(levels(" 24 , 48 ,").unwrap(), vec![24, 48]);
}

#[test]
fn rejects_a_non_integer_level() {
    assert!(levels("24,many").unwrap_err().contains("not an integer"));
}

#[test]
fn rejects_a_level_below_one() {
    assert!(levels("24,0").unwrap_err().contains("must be >= 1"));
}

#[test]
fn rejects_an_empty_concurrency_list() {
    assert!(levels(",").unwrap_err().contains("at least one level"));
}

#[test]
fn levels_print_back_the_way_they_were_given() {
    assert_eq!(Levels::from_str("24,48").unwrap().to_string(), "24,48");
}

#[test]
fn the_batch_size_defaults_to_the_campaigns_os_batch() {
    assert_eq!(parse(&a_minimal_command()).batch_size, 512);
    assert_eq!(DEFAULT_BATCH_SIZE, "512");
}

#[test]
fn a_requested_batch_size_is_taken() {
    let args = parse(&[
        "--corpus", "c.jsonl", "--concurrency", "24", "--batch-size", "1000",
    ]);
    assert_eq!(args.batch_size, 1000);
}

/// A batch of one document is the single-document loader, which is the shape
/// the ScyllaDB half of the bench measures — it has to remain askable.
#[test]
fn a_batch_size_of_one_is_allowed() {
    let args = parse(&[
        "--corpus", "c.jsonl", "--concurrency", "24", "--batch-size", "1",
    ]);
    assert_eq!(args.batch_size, 1);
}

#[test]
fn rejects_a_batch_size_of_zero() {
    assert!(parse_batch_size("0").unwrap_err().contains("must be >= 1"));
}

#[test]
fn rejects_a_non_integer_batch_size() {
    assert!(parse_batch_size("many")
        .unwrap_err()
        .contains("not an integer"));
}

#[test]
fn the_parser_accepts_a_full_command_line() {
    let args = parse(&[
        "--corpus",
        "c.jsonl",
        "--concurrency",
        "24,48",
        "--batch-size",
        "256",
        "--url",
        "http://10.0.0.1:9200",
        "--index",
        "wiki-ram",
        "--tokio-workers",
        "8",
    ]);
    assert_eq!(
        (
            args.tokio_workers(),
            args.batch_size,
            args.concurrency.0,
            args.url.as_str(),
            args.index.as_str()
        ),
        (8, 256, vec![24, 48], "http://10.0.0.1:9200", "wiki-ram")
    );
}

#[test]
fn the_parser_defaults_to_the_wiki_articles_index() {
    let args = parse(&a_minimal_command());
    assert_eq!(
        (
            args.url.as_str(),
            args.index.as_str(),
            args.max_docs,
            args.out.as_str()
        ),
        ("http://localhost:9200", "wiki-articles", 0, "-")
    );
}

/// The repo's shell scripts all export `OS_URL`, so the flag reads the same
/// variable rather than a second name for the same endpoint.
#[test]
fn the_endpoint_comes_from_os_url_when_the_flag_is_absent() {
    let variable = Args::command()
        .get_arguments()
        .find(|arg| arg.get_id() == "url")
        .and_then(|arg| arg.get_env().map(|env| env.to_string_lossy().to_string()));
    assert_eq!(variable.as_deref(), Some("OS_URL"));
}

#[test]
fn a_trailing_slash_on_the_url_is_trimmed_before_paths_are_appended() {
    let args = parse(&[
        "--corpus",
        "c.jsonl",
        "--concurrency",
        "24",
        "--url",
        "http://localhost:9200/",
    ]);
    assert_eq!(args.connect_options().url, "http://localhost:9200");
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
        "--corpus", "c.jsonl", "--concurrency", "24", "--tokio-workers", "3",
    ]);
    assert_eq!(args.tokio_workers(), 3);
}

#[test]
fn connect_options_carry_the_request_timeout_as_a_duration() {
    let args = parse(&[
        "--corpus",
        "c.jsonl",
        "--concurrency",
        "24",
        "--request-timeout",
        "2.5",
    ]);
    assert_eq!(
        args.connect_options().request_timeout,
        Duration::from_millis(2500)
    );
}

/// A bulk into a saturated engine takes a long time to answer, and a short
/// timeout would report the client giving up as the engine failing.
#[test]
fn the_default_request_timeout_matches_the_python_loaders_bulk_timeout() {
    assert_eq!(parse(&a_minimal_command()).request_timeout, 120.0);
}

#[test]
fn settings_record_the_batch_size_and_what_the_latency_is_per() {
    let args = parse(&[
        "--corpus", "c.jsonl", "--concurrency", "24", "--batch-size", "256",
    ]);
    assert_eq!(setting(&args, "batch_size"), "256");
    assert_eq!(setting(&args, "latency_unit"), "bulk_request");
}

#[test]
fn settings_record_the_corpus_and_the_document_limit() {
    let args = parse(&[
        "--corpus",
        "/tmp/small.jsonl",
        "--concurrency",
        "24",
        "--max-docs",
        "5000",
    ]);
    assert_eq!(setting(&args, "corpus"), "/tmp/small.jsonl");
    assert_eq!(setting(&args, "max_docs"), "5000");
}

#[test]
fn settings_record_the_tokio_worker_count() {
    let args = parse(&[
        "--corpus", "c.jsonl", "--concurrency", "24", "--tokio-workers", "4",
    ]);
    assert_eq!(setting(&args, "tokio_workers"), "4");
}

/// The default is the constant, and the README's memory arithmetic is written
/// against that number: a change to one that leaves the other behind is what
/// this pins.
#[test]
fn the_default_queue_depth_is_the_documented_constant() {
    let args = parse(&a_minimal_command());
    assert_eq!(
        (args.queue_depth, setting(&args, "queue_depth")),
        (QUEUE_DEPTH_PER_WORKER, QUEUE_DEPTH_PER_WORKER.to_string())
    );
    assert_eq!(QUEUE_DEPTH_PER_WORKER, 10);
}

#[test]
fn settings_record_the_queue_depth_that_bounded_the_producer() {
    let args = parse(&[
        "--corpus", "c.jsonl", "--concurrency", "24", "--queue-depth", "6",
    ]);
    assert_eq!((args.queue_depth, setting(&args, "queue_depth")), (6, "6".to_string()));
}

/// Whether the binary can even reach an `https://` endpoint is a build-time
/// choice, so the header has to say which build produced the numbers.
#[test]
fn settings_record_whether_the_binary_was_built_with_tls() {
    assert_eq!(setting(&parse(&a_minimal_command()), "tls"), tls_state());
}

#[test]
fn a_corpus_is_required() {
    assert!(Args::try_parse_from(["osrate", "--concurrency", "24"]).is_err());
}

#[test]
fn a_concurrency_ladder_is_required() {
    assert!(Args::try_parse_from(["osrate", "--corpus", "c.jsonl"]).is_err());
}
