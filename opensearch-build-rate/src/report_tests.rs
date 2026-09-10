use std::fs;

use super::*;
use crate::fakes::{a_cluster, a_point, a_point_with_errors, a_point_with_latency};

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

fn field(result: &PointResult, column: &str) -> String {
    let index = CSV_COLUMNS.iter().position(|name| *name == column).unwrap();
    csv_row(result).split(',').nth(index).unwrap().to_string()
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

/// A chart script written for the ScyllaDB half has to read this CSV by index
/// as readily as by name, so the shared columns keep their positions and the
/// bulk-specific ones are appended.
#[test]
fn the_first_seven_columns_are_the_scylladb_halfs_columns_in_order() {
    assert_eq!(
        &CSV_COLUMNS[..7],
        [
            "concurrency",
            "docs",
            "errors",
            "wall_s",
            "docs_per_s",
            "p50_ms",
            "p99_ms"
        ]
    );
}

#[test]
fn header_carries_the_cluster_as_comment_lines() {
    let lines = header_lines(&a_cluster(), &a_setting("corpus", "data/corpus.jsonl"));
    assert!(lines.iter().all(|line| line.starts_with("# ")));
    assert!(lines.contains(&"# index=wiki-articles".to_string()));
    assert!(lines.contains(&"# corpus=data/corpus.jsonl".to_string()));
}

#[test]
fn header_names_the_clients_that_produced_the_numbers() {
    let lines = header_lines(&a_cluster(), &[]);
    assert!(lines.contains(&"# client=2.4.0".to_string()));
    assert!(lines.contains(&"# http_client=0.13.5".to_string()));
}

/// The refresh interval and the analyzer are the two facts that decide whether
/// two OpenSearch charts are the same measurement at all.
#[test]
fn header_names_the_refresh_interval_and_the_analyzer() {
    let lines = header_lines(&a_cluster(), &[]);
    assert!(lines.contains(&"# refresh_interval=1s".to_string()));
    assert!(lines.contains(&"# body_analyzer=m1_parity".to_string()));
}

#[test]
fn csv_has_a_header_row_and_one_row_per_point() {
    let tmp = tempfile::tempdir().unwrap();
    let text = written(&tmp.path().join("sweep.csv"), |sink| {
        sink.write_preamble(&a_cluster(), &[]).unwrap();
        sink.append_row(&a_point(24)).unwrap();
        sink.append_row(&a_point(48)).unwrap();
    });
    let rows: Vec<&str> = text.lines().filter(|line| !line.starts_with('#')).collect();
    assert_eq!(
        rows[0],
        "concurrency,docs,errors,wall_s,docs_per_s,p50_ms,p99_ms,batch_size,bulks,failed_bulks"
    );
    assert_eq!(rows.len(), 3);
}

#[test]
fn csv_row_reports_the_two_plotted_metrics() {
    let point = a_point(32);
    assert_eq!(field(&point, "concurrency"), "32");
    assert_eq!(field(&point, "docs_per_s"), "50.0");
    assert_eq!(field(&point, "p99_ms"), "9.000");
}

/// `p99_ms` is per bulk, so a chart that mixes two batch sizes has to show it
/// rather than leave the reader to guess from the header alone.
#[test]
fn csv_row_carries_the_batch_size_the_latency_belongs_to() {
    assert_eq!(field(&a_point(32), "batch_size"), "10");
    assert_eq!(LATENCY_UNIT, "bulk_request");
}

#[test]
fn csv_row_reports_the_bulks_the_latency_was_drawn_from() {
    assert_eq!(field(&a_point(32), "bulks"), "10");
}

#[test]
fn an_unmeasured_latency_is_an_empty_csv_cell_not_a_zero() {
    let point = a_point_with_latency(24, None, None);
    assert_eq!((field(&point, "p50_ms"), field(&point, "p99_ms")), (String::new(), String::new()));
}

#[test]
fn an_unmeasured_latency_reads_as_a_dash_in_the_summary() {
    let table = summary_table(&[a_point_with_latency(24, None, None)]);
    let fields: Vec<&str> = table.lines().nth(1).unwrap().split_whitespace().collect();
    assert_eq!(&fields[fields.len() - 3..fields.len() - 1], ["-", "-"]);
}

#[test]
fn the_summary_table_has_a_header_and_one_row_per_point() {
    let table = summary_table(&[a_point(24), a_point(48)]);
    let lines: Vec<&str> = table.lines().collect();
    assert_eq!(
        lines[0].split_whitespace().collect::<Vec<_>>(),
        ["conc", "batch", "docs", "err", "wall_s", "docs/s", "p50_ms", "p99_ms", "bulks"]
    );
    assert_eq!(lines.len(), 3);
}

#[test]
fn the_summary_row_leads_with_the_concurrency_and_the_batch_size() {
    let table = summary_table(&[a_point(64)]);
    let fields: Vec<&str> = table.lines().nth(1).unwrap().split_whitespace().collect();
    assert_eq!(&fields[..2], ["64", "10"]);
}

#[test]
fn documents_in_flight_are_the_product_the_reader_has_to_be_told() {
    assert_eq!(a_point(64).docs_in_flight(), 640);
}

#[test]
fn the_csv_can_be_written_to_a_file() {
    let tmp = tempfile::tempdir().unwrap();
    let text = written(&tmp.path().join("sweep.csv"), |sink| {
        sink.append_row(&a_point(24)).unwrap();
    });
    assert!(text.starts_with("24,100,0,"));
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
    sink.write_preamble(&a_cluster(), &[]).unwrap();
    sink.append_row(&a_point(24)).unwrap();

    let after_first = fs::read_to_string(&destination).unwrap();
    sink.append_row(&a_point(48)).unwrap();
    assert_eq!(measured_rows(&after_first).len(), 1);
}

#[test]
fn the_sink_remembers_where_it_writes_so_an_abort_can_say_so() {
    assert_eq!(CsvSink::open(STDOUT).unwrap().destination(), STDOUT);
}

/// A point with failures has a p99 drawn only from the bulks that came back
/// clean, so the columns have to be readable together.
#[test]
fn an_undelivered_document_count_reaches_both_the_csv_and_the_summary() {
    let point = a_point_with_errors(24, 3);
    assert_eq!(field(&point, "errors"), "3");
    assert_eq!(field(&point, "failed_bulks"), "1");
    assert!(summary_table(&[point])
        .lines()
        .nth(1)
        .unwrap()
        .split_whitespace()
        .any(|field| field == "3"));
}
