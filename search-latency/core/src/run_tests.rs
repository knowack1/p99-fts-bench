use super::*;
use crate::cell::Tally;
use crate::report::{Shape, SCYLLADB};
use crate::search::CQL;
use crate::test_support::a_class;

fn a_cell(errors: u64, zero_hit_queries: u64) -> CellResult {
    CellResult::new(
        &Shape {
            engine: SCYLLADB,
            interface: CQL,
            limit: 10,
            fetch_documents: false,
        },
        8,
        &a_class("rare_term", &["kraken"]),
        &Tally {
            queries: 10,
            errors,
            hits: 10 - zero_hit_queries,
            zero_hit_queries,
            wall_s: 1.0,
        },
        &[1.0],
    )
}

fn failed(results: &[CellResult], aborted: bool) -> bool {
    format!("{:?}", exit_code(results, aborted)) == format!("{:?}", ExitCode::FAILURE)
}

#[test]
fn a_clean_matrix_succeeds() {
    assert!(!failed(&[a_cell(0, 0), a_cell(0, 3)], false));
}

#[test]
fn a_failed_request_anywhere_fails_the_run() {
    assert!(failed(&[a_cell(0, 0), a_cell(1, 0)], false));
}

#[test]
fn a_cell_where_every_query_matched_nothing_fails_the_run() {
    assert!(failed(&[a_cell(0, 10)], false));
}

#[test]
fn an_aborted_matrix_fails_even_with_nothing_wrong_in_it() {
    assert!(failed(&[a_cell(0, 0)], true));
}

#[test]
fn the_summary_survives_a_run_that_measured_nothing() {
    echo_summary(&[]);
}
