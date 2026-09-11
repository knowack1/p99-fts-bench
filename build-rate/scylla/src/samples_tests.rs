//! A series is only worth charting if every reading means what its row says.
//! These hold the two rules the rest of the crate already lives by: a rate is
//! documents over the time that actually passed, and a cell nobody measured is
//! blank rather than zero.
use std::fs;
use std::time::Duration;

use super::*;
use crate::fakes::a_topology;
use crate::vstore::IndexStatus;

const NO_SETTINGS: [(String, String); 0] = [];

fn serving(count: u64) -> IndexState {
    IndexState::Present(IndexStatus {
        count,
        status: "SERVING".to_string(),
    })
}

fn a_tape() -> Tape {
    Tape::new(1, 8, None)
}

/// A level whose clock the test holds, so a rate is a number to assert rather
/// than a sign to check.
fn a_timed_tape(started: Instant) -> Tape {
    Tape::started_at(started, 1, 8, None)
}

fn after(started: Instant, seconds: f64) -> Instant {
    started + Duration::from_secs_f64(seconds)
}

fn cells(sample: &Sample) -> Vec<String> {
    sample_row(sample).split(',').map(str::to_string).collect()
}

fn rows(text: &str) -> Vec<&str> {
    text.lines()
        .filter(|line| !line.starts_with('#'))
        .skip(1)
        .collect()
}

#[test]
fn a_rate_is_documents_over_the_time_that_passed() {
    assert_eq!(rate(500, 2.0), 250.0);
}

#[test]
fn a_rate_over_no_time_is_zero_rather_than_infinity() {
    assert_eq!(rate(500, 0.0), 0.0);
}

#[test]
fn successes_are_counted_apart_from_failures() {
    let submitted = Submitted::default();
    submitted.record(true);
    submitted.record(false);
    submitted.record(true);

    assert_eq!(
        (submitted.ok(), submitted.errors(), submitted.total()),
        (2, 1, 3)
    );
}

#[test]
fn a_reading_carries_the_level_and_the_concurrency_that_produced_it() {
    let sample = Tape::new(3, 64, None).record(10, None);

    assert_eq!((sample.level, sample.concurrency), (3, 64));
}

#[test]
fn a_reading_counts_only_the_documents_since_the_one_before_it() {
    let tape = a_tape();
    tape.record(400, None);

    let sample = tape.record(700, None);

    assert_eq!(sample.docs_submitted, 700);
    assert!(sample.submit_docs_per_s > 0.0);
}

/// The number the chart exists for: a second reading that saw nothing new is a
/// stall, and a stall has to plot as zero rather than as the running total
/// divided by the time so far.
#[test]
fn a_series_that_did_not_move_reports_a_zero_rate() {
    let tape = a_tape();
    tape.record(400, None);

    assert_eq!(tape.record(400, None).submit_docs_per_s, 0.0);
}

#[test]
fn a_rate_is_measured_over_the_gap_the_two_readings_left() {
    let started = Instant::now();
    let tape = a_timed_tape(started);
    tape.record_at(after(started, 1.0), 400, None);

    let sample = tape.record_at(after(started, 3.0), 1000, None);

    assert_eq!(sample.submit_docs_per_s, 300.0);
}

/// The interval a ticker asked for is not the interval it got. A poll that
/// overran its tick would otherwise report a rate the run never reached.
#[test]
fn a_reading_that_overran_its_tick_is_measured_over_the_time_it_took() {
    let started = Instant::now();
    let tape = a_timed_tape(started);
    tape.record_at(after(started, 1.0), 0, None);

    let sample = tape.record_at(after(started, 5.0), 1000, None);

    assert_eq!(sample.submit_docs_per_s, 250.0);
}

#[test]
fn the_first_reading_is_measured_from_the_start_of_the_level() {
    let started = Instant::now();

    let sample = a_timed_tape(started).record_at(after(started, 4.0), 1000, None);

    assert_eq!(sample.submit_docs_per_s, 250.0);
}

#[test]
fn a_row_says_how_far_into_the_level_it_was_taken() {
    let started = Instant::now();

    let sample = a_timed_tape(started).record_at(after(started, 2.5), 10, None);

    assert_eq!(sample.t_s, 2.5);
}

/// The reading that ends the submit series carries no index count, and neither
/// does a failed poll. Counting either as an index of zero makes the next real
/// reading a spike — at the handover from the client to the drain, which is the
/// shape this series exists to show.
#[test]
fn a_reading_without_an_index_leaves_the_index_series_where_it_was() {
    let started = Instant::now();
    let tape = a_timed_tape(started);
    tape.record_at(after(started, 1.0), 0, Some(&serving(1000)));
    tape.record_at(after(started, 1.1), 0, None);

    let sample = tape.record_at(after(started, 2.0), 0, Some(&serving(1200)));

    assert_eq!(sample.indexed.unwrap().docs_per_s, 200.0);
}

#[test]
fn an_index_reading_counts_only_what_this_level_added() {
    let tape = a_tape();
    tape.inherited(1000);

    let sample = tape.record(0, Some(&serving(1250)));

    assert_eq!(sample.indexed.unwrap().docs, 250);
}

#[test]
fn an_index_that_went_backwards_reports_no_rate_rather_than_underflowing() {
    let tape = a_tape();
    tape.record(0, Some(&serving(500)));

    let sample = tape.record(0, Some(&serving(200)));

    assert_eq!(sample.indexed.unwrap().docs_per_s, 0.0);
}

#[test]
fn an_index_reading_carries_the_status_the_store_reported() {
    let sample = a_tape().record(0, Some(&serving(5)));

    assert_eq!(sample.indexed.unwrap().status, "SERVING");
}

#[test]
fn an_index_that_is_absent_is_said_so_rather_than_left_unnamed() {
    let sample = a_tape().record(0, Some(&IndexState::Absent));

    assert_eq!(sample.indexed.unwrap().status, "absent");
}

#[test]
fn a_row_has_one_cell_per_column() {
    let sample = a_tape().record(10, Some(&serving(5)));

    assert_eq!(cells(&sample).len(), SAMPLE_COLUMNS.len());
}

#[test]
fn an_unwatched_index_leaves_blank_cells_rather_than_zeros() {
    let sample = a_tape().record(10, None);

    assert_eq!(cells(&sample)[5..8], ["", "", ""]);
}

#[test]
fn a_watched_index_fills_the_cells_an_unwatched_one_leaves_blank() {
    let sample = a_tape().record(10, Some(&serving(7)));

    assert_eq!(cells(&sample)[5], "7");
    assert_eq!(cells(&sample)[7], "SERVING");
}

#[test]
fn a_repeated_level_gets_its_own_file() {
    let dir = tempfile::tempdir().unwrap();
    let files = SampleFiles::new(dir.path()).unwrap();

    files.open_level(8).unwrap();
    files.open_level(8).unwrap();
    files.open_level(16).unwrap();

    for name in ["c8-1.csv", "c8-2.csv", "c16-1.csv"] {
        assert!(dir.path().join(name).exists(), "missing {name}");
    }
}

#[test]
fn every_level_file_carries_the_run_facts_and_the_column_header() {
    let dir = tempfile::tempdir().unwrap();
    let files = SampleFiles::new(dir.path())
        .unwrap()
        .with_preamble(&a_topology(), &NO_SETTINGS);

    drop(files.open_level(8).unwrap());

    let written = fs::read_to_string(dir.path().join("c8-1.csv")).unwrap();
    assert!(written.contains("# scylla_version="));
    assert!(written.contains(&SAMPLE_COLUMNS.join(",")));
}

/// A level killed part way through is the case the samples matter most for, so
/// a reading is on disk before the next one is taken.
#[test]
fn a_reading_is_on_disk_as_soon_as_it_is_taken() {
    let dir = tempfile::tempdir().unwrap();
    let files = SampleFiles::new(dir.path()).unwrap();
    let tape = Tape::new(1, 8, Some(files.open_level(8).unwrap()));

    tape.record(100, None);
    tape.record(200, None);

    let written = fs::read_to_string(dir.path().join("c8-1.csv")).unwrap();
    assert_eq!(rows(&written).len(), 2);
}

#[test]
fn a_row_names_the_level_before_the_documents_it_counted() {
    let dir = tempfile::tempdir().unwrap();
    let files = SampleFiles::new(dir.path()).unwrap();
    let tape = Tape::new(2, 16, Some(files.open_level(16).unwrap()));

    tape.record(100, None);

    let written = fs::read_to_string(dir.path().join("c16-1.csv")).unwrap();
    let fields: Vec<&str> = rows(&written)[0].split(',').collect();
    assert_eq!((fields[0], fields[1], fields[3]), ("2", "16", "100"));
}

#[test]
fn a_tape_without_a_file_still_hands_back_its_readings() {
    let sample = Tape::new(1, 8, None).record(42, None);

    assert_eq!(sample.docs_submitted, 42);
}

#[test]
fn a_sink_names_the_file_it_was_opened_on() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("series.csv");

    let sink = SampleSink::create(&path).unwrap();

    assert_eq!(sink.destination(), path.display().to_string());
}

#[test]
fn a_directory_that_cannot_be_created_is_refused_rather_than_silently_dropped() {
    let dir = tempfile::tempdir().unwrap();
    let blocked = dir.path().join("file");
    fs::write(&blocked, "not a directory").unwrap();

    assert!(SampleFiles::new(&blocked.join("under")).is_err());
}
