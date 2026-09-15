use super::*;
use crate::search::{CQL, HTTP};
use crate::test_support::a_class;

fn a_shape() -> Shape {
    Shape {
        engine: SCYLLADB,
        interface: CQL,
        limit: 10,
        fetch_documents: false,
    }
}

fn a_tally() -> Tally {
    Tally {
        queries: 100,
        errors: 0,
        hits: 1000,
        zero_hit_queries: 0,
        wall_s: 10.0,
    }
}

fn a_result() -> CellResult {
    CellResult::new(
        &a_shape(),
        16,
        &a_class("rare_term", &["kraken", "zeppelin"]),
        &a_tally(),
        &[1.0, 2.0, 3.0, 4.0],
    )
}

#[test]
fn the_header_and_the_row_have_the_same_number_of_fields() {
    let row = csv_row(&a_result());

    assert_eq!(row.split(',').count(), CSV_COLUMNS.len());
}

#[test]
fn the_row_carries_the_engine_and_the_interface_it_was_reached_through() {
    let row = csv_row(&a_result());
    let fields: Vec<&str> = row.split(',').collect();

    assert_eq!(fields[CSV_COLUMNS.len() - 2], SCYLLADB);
    assert_eq!(fields[CSV_COLUMNS.len() - 1], CQL);
}

/// Every chart this tree exists for reads one of these four against
/// `concurrency`, so their names and their places are the schema's contract.
#[test]
fn the_four_charted_columns_are_where_a_renderer_will_look_for_them() {
    for column in ["p50_ms", "p90_ms", "p99_ms", "queries_per_s"] {
        assert!(CSV_COLUMNS.contains(&column), "{column} is missing");
    }
    assert_eq!(CSV_COLUMNS[0], "concurrency");
    assert_eq!(CSV_COLUMNS[1], "query_class");
}

#[test]
fn a_cell_that_measured_nothing_leaves_blank_cells_rather_than_zeros() {
    let nothing = CellResult::new(
        &a_shape(),
        8,
        &a_class("rare_term", &["kraken"]),
        &Tally {
            queries: 0,
            errors: 12,
            hits: 0,
            zero_hit_queries: 0,
            wall_s: 3.0,
        },
        &[],
    );

    let row = csv_row(&nothing);
    let fields: Vec<&str> = row.split(',').collect();
    for at in [6, 7, 8, 9, 10] {
        assert_eq!(fields[at], "", "{} should be blank", CSV_COLUMNS[at]);
    }
}

#[test]
fn a_class_that_matched_nothing_is_visible_in_the_row_and_in_the_table() {
    let empty = CellResult::new(
        &a_shape(),
        8,
        &a_class("rare_term", &["kraken"]),
        &Tally {
            queries: 40,
            errors: 0,
            hits: 0,
            zero_hit_queries: 40,
            wall_s: 2.0,
        },
        &[1.0],
    );

    assert!(empty.found_nothing());
    assert!(summary_table(&[empty]).contains("0.0!"));
}

#[test]
fn a_class_that_mostly_matched_is_not_marked_as_finding_nothing() {
    let mostly = CellResult::new(
        &a_shape(),
        8,
        &a_class("rare_term", &["kraken"]),
        &Tally {
            queries: 40,
            errors: 0,
            hits: 30,
            zero_hit_queries: 3,
            wall_s: 2.0,
        },
        &[1.0],
    );

    assert!(!mostly.found_nothing());
}

#[test]
fn the_projection_and_the_top_n_are_columns_because_they_change_what_was_measured() {
    let fetched = CellResult::new(
        &Shape {
            interface: HTTP,
            engine: OPENSEARCH,
            limit: 1000,
            fetch_documents: true,
        },
        8,
        &a_class("phrase", &["\"united states\""]),
        &a_tally(),
        &[1.0],
    );

    let row = csv_row(&fetched);
    assert!(row.contains(",1000,true,opensearch,http"), "{row}");
}

#[test]
fn a_rate_over_no_time_is_zero_rather_than_infinite() {
    assert_eq!(per_second(10, 0.0), 0.0);
}

#[test]
fn the_preamble_is_written_before_the_column_names() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("points.csv");
    let mut sink = CsvSink::open(path.to_str().unwrap()).unwrap();

    sink.write_preamble(&["# engine=scylladb".to_string()])
        .unwrap();
    sink.append_row(&a_result()).unwrap();

    let written = std::fs::read_to_string(&path).unwrap();
    let lines: Vec<&str> = written.lines().collect();
    assert_eq!(lines[0], "# engine=scylladb");
    assert_eq!(lines[1], CSV_COLUMNS.join(","));
    assert!(lines[2].starts_with("16,rare_term,"));
}

#[test]
fn the_summary_table_names_every_cell_it_was_given() {
    let table = summary_table(&[a_result(), a_result()]);

    assert_eq!(table.lines().count(), 3);
    assert!(table.contains("p99_ms"));
}
