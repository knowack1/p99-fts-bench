use std::fs;

use super::*;
use crate::fakes::{a_point, a_point_with_errors, a_point_with_latency, a_topology};

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
fn header_carries_the_topology_as_comment_lines() {
    let lines = header_lines(&a_topology(), &a_setting("corpus", "data/corpus.jsonl"));
    assert!(lines.iter().all(|line| line.starts_with("# ")));
    assert!(lines.contains(&"# shard_aware=true".to_string()));
    assert!(lines.contains(&"# corpus=data/corpus.jsonl".to_string()));
}

#[test]
fn header_names_the_driver_that_produced_the_numbers() {
    let lines = header_lines(&a_topology(), &[]);
    assert!(lines.contains(&"# driver=1.8.0".to_string()));
}

#[test]
fn csv_has_a_header_row_and_one_row_per_point() {
    let tmp = tempfile::tempdir().unwrap();
    let text = written(&tmp.path().join("sweep.csv"), |sink| {
        sink.write_preamble(&a_topology(), &[]).unwrap();
        sink.append_row(&a_point(8)).unwrap();
        sink.append_row(&a_point(16)).unwrap();
    });
    let rows: Vec<&str> = text.lines().filter(|line| !line.starts_with('#')).collect();
    assert_eq!(
        rows[0],
        "concurrency,docs,errors,wall_s,docs_per_s,p50_ms,p99_ms"
    );
    assert_eq!(rows.len(), 3);
}

#[test]
fn csv_row_reports_the_two_plotted_metrics() {
    let row = csv_row(&a_point(32));
    let fields: Vec<&str> = row.split(',').collect();
    assert_eq!((fields[0], fields[4], fields[6]), ("32", "50.0", "9.000"));
}

#[test]
fn an_unmeasured_latency_is_an_empty_csv_cell_not_a_zero() {
    let row = csv_row(&a_point_with_latency(8, None, None));
    let fields: Vec<&str> = row.split(',').collect();
    assert_eq!((fields[5], fields[6]), ("", ""));
}

#[test]
fn an_unmeasured_latency_reads_as_a_dash_in_the_summary() {
    let table = summary_table(&[a_point_with_latency(8, None, None)]);
    let last_two: Vec<&str> = table.lines().nth(1).unwrap().split_whitespace().collect();
    assert_eq!(&last_two[last_two.len() - 2..], ["-", "-"]);
}

#[test]
fn the_summary_table_has_a_header_and_one_row_per_point() {
    let table = summary_table(&[a_point(8), a_point(16)]);
    let lines: Vec<&str> = table.lines().collect();
    assert_eq!(
        lines[0].split_whitespace().collect::<Vec<_>>(),
        ["conc", "docs", "err", "wall_s", "docs/s", "p50_ms", "p99_ms"]
    );
    assert_eq!(lines.len(), 3);
}

#[test]
fn the_summary_row_leads_with_the_concurrency_level() {
    let table = summary_table(&[a_point(64)]);
    assert_eq!(
        table.lines().nth(1).unwrap().split_whitespace().next(),
        Some("64")
    );
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
    sink.write_preamble(&a_topology(), &[]).unwrap();
    sink.append_row(&a_point(8)).unwrap();

    let after_first = fs::read_to_string(&destination).unwrap();
    sink.append_row(&a_point(16)).unwrap();
    assert_eq!(measured_rows(&after_first).len(), 1);
}

#[test]
fn the_sink_remembers_where_it_writes_so_an_abort_can_say_so() {
    assert_eq!(CsvSink::open(STDOUT).unwrap().destination(), STDOUT);
}

/// A point with failures has a p99 drawn only from the inserts that landed, so
/// the two columns have to be readable together.
#[test]
fn a_failed_insert_count_reaches_both_the_csv_and_the_summary() {
    let point = a_point_with_errors(8, 3);
    assert_eq!(csv_row(&point).split(',').nth(2), Some("3"));
    assert!(summary_table(&[point])
        .lines()
        .nth(1)
        .unwrap()
        .split_whitespace()
        .any(|field| field == "3"));
}
