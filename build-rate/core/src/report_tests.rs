use std::fs;

use super::*;

fn a_point(concurrency: usize) -> PointResult {
    PointResult {
        engine: SCYLLADB,
        concurrency,
        batch_size: 1,
        docs: 100,
        errors: 0,
        requests: 100,
        failed_requests: 0,
        wall_s: 2.0,
        docs_per_s: 50.0,
        p50_ms: Some(4.0),
        p99_ms: Some(9.0),
        index: None,
    }
}

fn a_batched_point(concurrency: usize, batch_size: usize) -> PointResult {
    PointResult {
        engine: OPENSEARCH,
        batch_size,
        requests: 20,
        ..a_point(concurrency)
    }
}

fn a_point_with_errors(concurrency: usize, errors: u64) -> PointResult {
    PointResult {
        errors,
        failed_requests: errors,
        ..a_point(concurrency)
    }
}

fn a_point_with_latency(concurrency: usize, p50: Option<f64>, p99: Option<f64>) -> PointResult {
    PointResult {
        p50_ms: p50,
        p99_ms: p99,
        ..a_point(concurrency)
    }
}

fn a_point_with_index(concurrency: usize, build: IndexBuild) -> PointResult {
    PointResult {
        index: Some(build),
        ..a_point(concurrency)
    }
}

fn an_index_build(docs: u64, settled: bool) -> IndexBuild {
    IndexBuild {
        docs,
        docs_per_s: 1234.5,
        lag_docs: 12,
        settle_s: 3.5,
        settled,
        status: "SERVING".to_string(),
    }
}

fn a_setting(key: &str, value: &str) -> Vec<(String, String)> {
    vec![(key.to_string(), value.to_string())]
}

fn written(destination: &std::path::Path, write: impl FnOnce(&mut CsvSink)) -> String {
    let mut sink = CsvSink::open(destination.to_str().unwrap()).unwrap();
    write(&mut sink);
    drop(sink);
    fs::read_to_string(destination).unwrap()
}

fn measured_rows(text: &str) -> Vec<&str> {
    text.lines()
        .filter(|line| !line.starts_with('#'))
        .skip(1)
        .collect()
}

fn fields(result: &PointResult) -> Vec<String> {
    csv_row(result).split(',').map(str::to_string).collect()
}

#[test]
fn percentile_picks_the_nearest_rank() {
    let values: Vec<f64> = (1..=100).map(f64::from).collect();
    assert_eq!(
        (percentile(&values, 0.50), percentile(&values, 0.99)),
        (Some(50.0), Some(99.0))
    );
}

#[test]
fn percentile_of_a_single_sample_is_that_sample() {
    assert_eq!(percentile(&[7.0], 0.99), Some(7.0));
}

#[test]
fn percentile_of_nothing_is_unmeasured_not_zero() {
    assert_eq!(percentile(&[], 0.99), None);
}

#[test]
fn percentile_of_zero_fraction_is_the_fastest_sample() {
    assert_eq!(percentile(&[1.0, 2.0, 3.0], 0.0), Some(1.0));
}

#[test]
fn the_header_is_the_facts_then_the_settings_as_comment_lines() {
    let facts = a_setting("scylla_version", "2026.1.0");
    let lines = header_lines(&facts, &a_setting("corpus", "data/corpus.jsonl"));
    assert!(lines.iter().all(|line| line.starts_with("# ")));
    assert_eq!(
        lines,
        ["# scylla_version=2026.1.0", "# corpus=data/corpus.jsonl"]
    );
}

/// The whole point of the merge: one schema, so a field index written for one
/// engine reads the same field on the other.
#[test]
fn both_engines_write_the_same_columns_in_the_same_order() {
    let tmp = tempfile::tempdir().unwrap();
    let text = written(&tmp.path().join("sweep.csv"), |sink| {
        sink.write_preamble(&[]).unwrap();
        sink.append_row(&a_point(8)).unwrap();
        sink.append_row(&a_batched_point(8, 512)).unwrap();
    });
    let rows: Vec<&str> = text.lines().filter(|line| !line.starts_with('#')).collect();
    assert_eq!(
        rows[0],
        "concurrency,docs,errors,wall_s,docs_per_s,p50_ms,p99_ms,\
         batch_size,requests,failed_requests,\
         index_docs,index_docs_per_s,index_lag_docs,index_settle_s,\
         index_settled,index_status,engine"
    );
    assert_eq!(rows.len(), 3);
    assert_eq!(
        rows[1].split(',').count(),
        rows[2].split(',').count(),
        "the two halves must not diverge in width"
    );
}

/// Concatenating both engines' rows is what `tools/plot_harness_grid.py` does,
/// and before this column it told them apart by whether `batch_size` existed —
/// a test both halves now pass.
#[test]
fn every_row_names_the_engine_that_produced_it() {
    assert_eq!(fields(&a_point(8))[16], SCYLLADB);
    assert_eq!(fields(&a_batched_point(8, 512))[16], OPENSEARCH);
}

#[test]
fn csv_row_reports_the_two_plotted_metrics() {
    let row = fields(&a_point(32));
    assert_eq!((&row[0][..], &row[4][..], &row[6][..]), ("32", "50.0", "9.000"));
}

#[test]
fn csv_row_carries_the_batch_size_the_latency_belongs_to() {
    assert_eq!(fields(&a_batched_point(8, 512))[7], "512");
    assert_eq!(fields(&a_point(8))[7], "1");
}

#[test]
fn csv_row_reports_the_requests_the_latency_was_drawn_from() {
    assert_eq!(fields(&a_batched_point(8, 512))[8], "20");
}

/// At one document per request the two numbers coincide, which is what makes
/// `docs / requests` the effective batch size a reader can check the column
/// against.
#[test]
fn a_one_document_request_counts_once_as_a_document_and_once_as_a_request() {
    let row = fields(&a_point(8));
    assert_eq!((&row[1][..], &row[8][..]), ("100", "100"));
}

#[test]
fn an_unmeasured_latency_is_an_empty_csv_cell_not_a_zero() {
    let row = fields(&a_point_with_latency(8, None, None));
    assert_eq!((&row[5][..], &row[6][..]), ("", ""));
}

#[test]
fn an_unmeasured_latency_reads_as_a_dash_in_the_summary() {
    let table = summary_table(&[a_point_with_latency(8, None, None)]);
    let row: Vec<&str> = table.lines().nth(1).unwrap().split_whitespace().collect();
    assert_eq!(&row[6..8], ["-", "-"]);
}

#[test]
fn the_summary_table_has_a_header_and_one_row_per_point() {
    let table = summary_table(&[a_point(8), a_point(16)]);
    let lines: Vec<&str> = table.lines().collect();
    assert_eq!(
        lines[0].split_whitespace().collect::<Vec<_>>(),
        [
            "conc", "batch", "docs", "err", "wall_s", "docs/s", "p50_ms", "p99_ms", "reqs",
            "idx_docs", "idx_docs/s"
        ]
    );
    assert_eq!(lines.len(), 3);
}

#[test]
fn the_summary_row_leads_with_the_concurrency_and_the_batch_size() {
    let table = summary_table(&[a_batched_point(64, 512)]);
    let row: Vec<&str> = table.lines().nth(1).unwrap().split_whitespace().collect();
    assert_eq!(&row[..2], ["64", "512"]);
}

#[test]
fn documents_in_flight_are_the_product_the_reader_has_to_be_told() {
    assert_eq!(a_batched_point(64, 512).docs_in_flight(), 32_768);
    assert_eq!(a_point(64).docs_in_flight(), 64);
}

#[test]
fn the_csv_can_be_written_to_a_file() {
    let tmp = tempfile::tempdir().unwrap();
    let text = written(&tmp.path().join("sweep.csv"), |sink| {
        sink.append_row(&a_point(8)).unwrap();
    });
    assert!(text.starts_with("8,100,0,"));
}

#[test]
fn an_unwritable_destination_fails_before_any_point_runs() {
    assert!(CsvSink::open("/nonexistent-dir/sweep.csv").is_err());
}

#[test]
fn a_point_reaches_the_file_before_the_next_one_starts() {
    let tmp = tempfile::tempdir().unwrap();
    let destination = tmp.path().join("sweep.csv");
    let mut sink = CsvSink::open(destination.to_str().unwrap()).unwrap();
    sink.write_preamble(&[]).unwrap();
    sink.append_row(&a_point(8)).unwrap();

    let after_first = fs::read_to_string(&destination).unwrap();
    sink.append_row(&a_point(16)).unwrap();
    assert_eq!(measured_rows(&after_first).len(), 1);
}

#[test]
fn the_sink_remembers_where_it_writes_so_an_abort_can_say_so() {
    assert_eq!(CsvSink::open(STDOUT).unwrap().destination(), STDOUT);
}

/// A point with failures has a p99 drawn only from what landed, so the two
/// columns have to be readable together.
#[test]
fn a_failure_count_reaches_both_the_csv_and_the_summary() {
    let point = a_point_with_errors(8, 3);
    assert_eq!(fields(&point)[2], "3");
    assert!(summary_table(&[point])
        .lines()
        .nth(1)
        .unwrap()
        .split_whitespace()
        .any(|field| field == "3"));
}

/// The same rule the latencies follow: an unwatched level leaves blank cells,
/// because a zero build rate is a finding and an unwatched level is not one.
#[test]
fn an_unwatched_level_leaves_the_index_cells_empty_not_zero() {
    let row = fields(&a_point(8));

    assert_eq!(row.len(), CSV_COLUMNS.len());
    assert!(row[10..16].iter().all(|cell| cell.is_empty()), "{row:?}");
}

#[test]
fn a_watched_level_reports_its_build_beside_its_submit_rate() {
    let row = fields(&a_point_with_index(8, an_index_build(270_269, true)));

    assert_eq!(row.len(), CSV_COLUMNS.len());
    assert_eq!(row[10], "270269");
    assert_eq!(row[11], "1234.5");
    assert_eq!(row[14], "true");
    assert_eq!(row[15], "SERVING");
}

/// A level whose index never caught up reports a floor, not a build rate, and
/// the summary has to say which it is looking at.
#[test]
fn an_unsettled_build_is_marked_in_the_summary() {
    let settled = summary_table(&[a_point_with_index(8, an_index_build(500, true))]);
    let short = summary_table(&[a_point_with_index(8, an_index_build(500, false))]);

    assert!(settled.contains(" 500 "));
    assert!(short.contains("500*"));
}

#[test]
fn an_unwatched_level_reads_as_a_dash_in_the_summary() {
    let table = summary_table(&[a_point(8)]);
    let row: Vec<&str> = table.lines().nth(1).unwrap().split_whitespace().collect();

    assert_eq!(&row[9..11], ["-", "-"]);
}
