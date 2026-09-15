use std::time::Duration;

use build_rate_core::test_support::{quiet_notes, SpokenNotes};

use super::*;
use crate::report::{OPENSEARCH, SCYLLADB};
use crate::search::{CQL, HTTP};
use crate::test_support::{a_class, FakeSearcher};

fn a_plan(levels: &[usize], classes: &[&str]) -> Plan {
    Plan {
        levels: levels.to_vec(),
        classes: classes
            .iter()
            .map(|name| a_class(name, &["kraken", "zeppelin"]))
            .collect(),
    }
}

fn a_shape() -> Shape {
    Shape {
        engine: SCYLLADB,
        interface: CQL,
        limit: 10,
        fetch_documents: false,
    }
}

fn a_settings() -> CellSettings {
    CellSettings {
        warmup: Duration::ZERO,
        duration: Duration::from_millis(20),
    }
}

async fn walk(plan: &Plan, searcher: FakeSearcher, notes: &Notes) -> Result<Vec<CellResult>> {
    let searcher: Arc<dyn Searcher> = Arc::new(searcher);
    let runner = Runner {
        searcher: &searcher,
        shape: a_shape(),
        settings: a_settings(),
        notes,
        latencies: None,
    };
    let mut measured = Vec::new();
    let mut collect = |result: CellResult| {
        measured.push(result);
        Ok(())
    };
    run_matrix(&runner, plan, &Cancel::default(), &mut collect).await?;
    Ok(measured)
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn the_matrix_is_every_concurrency_against_every_class() {
    let plan = a_plan(&[1, 2], &["phrase", "rare_term"]);

    let measured = walk(&plan, FakeSearcher::new(), &quiet_notes())
        .await
        .unwrap();

    assert_eq!(plan.cells(), 4);
    let cells: Vec<(usize, &str)> = measured
        .iter()
        .map(|cell| (cell.concurrency, cell.query_class.as_str()))
        .collect();
    assert_eq!(
        cells,
        [
            (1, "phrase"),
            (1, "rare_term"),
            (2, "phrase"),
            (2, "rare_term")
        ]
    );
}

/// Concurrency outside, class inside: a repeated ladder then interleaves two
/// traversals of the whole matrix rather than measuring one class an hour after
/// another.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_repeated_ladder_interleaves_traversals_and_keeps_both() {
    let plan = a_plan(&[4, 8, 4], &["rare_term"]);

    let measured = walk(&plan, FakeSearcher::new(), &quiet_notes())
        .await
        .unwrap();

    let levels: Vec<usize> = measured.iter().map(|cell| cell.concurrency).collect();
    assert_eq!(levels, [4, 8, 4]);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn every_cell_carries_the_shape_the_run_was_measured_in() {
    let searcher: Arc<dyn Searcher> = Arc::new(FakeSearcher::new());
    let notes = quiet_notes();
    let runner = Runner {
        searcher: &searcher,
        shape: Shape {
            engine: OPENSEARCH,
            interface: HTTP,
            limit: 100,
            fetch_documents: true,
        },
        settings: a_settings(),
        notes: &notes,
        latencies: None,
    };
    let mut measured = Vec::new();
    let mut collect = |result: CellResult| {
        measured.push(result);
        Ok(())
    };

    run_matrix(
        &runner,
        &a_plan(&[2], &["rare_term"]),
        &Cancel::default(),
        &mut collect,
    )
    .await
    .unwrap();

    assert_eq!(measured[0].engine, OPENSEARCH);
    assert_eq!(measured[0].interface, HTTP);
    assert_eq!(measured[0].limit, 100);
    assert!(measured[0].fetch_documents);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_class_that_matches_nothing_is_said_out_loud() {
    let spoken = SpokenNotes::default();
    let notes = spoken.notes(Duration::from_millis(5));

    walk(
        &a_plan(&[1], &["rare_term"]),
        FakeSearcher::new().finding(0),
        &notes,
    )
    .await
    .unwrap();

    assert!(spoken.mentions("matched nothing"), "{:?}", spoken.lines());
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_failing_engine_is_named_on_the_way_past() {
    let spoken = SpokenNotes::default();
    let notes = spoken.notes(Duration::from_millis(5));

    walk(
        &a_plan(&[1], &["rare_term"]),
        FakeSearcher::new().failing_every(1),
        &notes,
    )
    .await
    .unwrap();

    assert!(spoken.mentions("failed queries"), "{:?}", spoken.lines());
}

/// The cells already measured are worth as much after an interrupt as after a
/// clean finish, so they must have reached the caller before the error did.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn an_interrupt_keeps_the_cells_measured_before_it() {
    let searcher: Arc<dyn Searcher> = Arc::new(FakeSearcher::new());
    let notes = quiet_notes();
    let runner = Runner {
        searcher: &searcher,
        shape: a_shape(),
        settings: a_settings(),
        notes: &notes,
        latencies: None,
    };
    let cancel = Cancel::default();
    let mut measured = Vec::new();
    let stop = cancel.clone();
    let mut collect = |result: CellResult| {
        measured.push(result);
        stop.trigger();
        Ok(())
    };

    let outcome = run_matrix(
        &runner,
        &a_plan(&[1, 2, 4], &["rare_term"]),
        &cancel,
        &mut collect,
    )
    .await;

    assert!(outcome.is_err());
    assert_eq!(measured.len(), 1);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn each_cell_leaves_its_distribution_where_it_was_asked_to() {
    let dir = tempfile::tempdir().unwrap();
    let files = LatencyFiles::new(dir.path()).unwrap();
    let searcher: Arc<dyn Searcher> = Arc::new(FakeSearcher::new());
    let notes = quiet_notes();
    let runner = Runner {
        searcher: &searcher,
        shape: a_shape(),
        settings: a_settings(),
        notes: &notes,
        latencies: Some(&files),
    };
    let mut collect = |_: CellResult| Ok(());

    run_matrix(
        &runner,
        &a_plan(&[2], &["rare_term"]),
        &Cancel::default(),
        &mut collect,
    )
    .await
    .unwrap();

    assert!(dir.path().join("rare_term-c2-1.csv").exists());
}
