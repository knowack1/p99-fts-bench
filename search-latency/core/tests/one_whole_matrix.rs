//! One matrix end to end, against a searcher rather than an engine: the CSV a
//! consumer reads, the distributions beside it, and the exit code.
//!
//! The engine clients have their own tests against a socket; what this pins is
//! the shape of what a finished run leaves on disk, which is the only part of
//! this tree a chart script will ever see.
use std::sync::Arc;
use std::time::Duration;

use build_rate_core::sweep::Cancel;
use build_rate_core::test_support::quiet_notes;
use search_latency_core::cell::CellSettings;
use search_latency_core::latencies::LatencyFiles;
use search_latency_core::matrix::{run_matrix, Plan, Runner};
use search_latency_core::queries::QuerySet;
use search_latency_core::report::{CellResult, CsvSink, Shape, CSV_COLUMNS, SCYLLADB};
use search_latency_core::run::exit_code;
use search_latency_core::search::{Searcher, CQL};
use search_latency_core::test_support::FakeSearcher;

const QUERIES: &str = r#"{
  "corpus": "data/corpus.jsonl",
  "classes": {
    "phrase": ["\"united states\""],
    "rare_term": ["kraken", "zeppelin"]
  }
}"#;

struct Run {
    points: String,
    results: Vec<CellResult>,
    latency_files: Vec<String>,
    dir: tempfile::TempDir,
}

async fn measure(levels: &[usize], searcher: FakeSearcher) -> Run {
    let dir = tempfile::tempdir().unwrap();
    let points_path = dir.path().join("points.csv");
    let latency_dir = dir.path().join("latencies");
    let query_set = QuerySet::parse(QUERIES, "test").unwrap();
    let plan = Plan {
        levels: levels.to_vec(),
        classes: query_set.select(&[]).unwrap(),
    };

    let mut sink = CsvSink::open(points_path.to_str().unwrap()).unwrap();
    sink.write_preamble(&["# engine=scylladb".to_string()])
        .unwrap();
    let files = LatencyFiles::new(&latency_dir).unwrap();
    let searcher: Arc<dyn Searcher> = Arc::new(searcher);
    let notes = quiet_notes();
    let runner = Runner {
        searcher: &searcher,
        shape: Shape {
            engine: SCYLLADB,
            interface: CQL,
            limit: 10,
            fetch_documents: false,
        },
        settings: CellSettings {
            warmup: Duration::ZERO,
            duration: Duration::from_millis(30),
        },
        notes: &notes,
        latencies: Some(&files),
    };

    let mut results = Vec::new();
    let mut collect = |result: CellResult| {
        sink.append_row(&result)?;
        results.push(result);
        Ok(())
    };
    run_matrix(&runner, &plan, &Cancel::default(), &mut collect)
        .await
        .unwrap();

    let mut latency_files: Vec<String> = std::fs::read_dir(&latency_dir)
        .unwrap()
        .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    latency_files.sort();
    Run {
        points: std::fs::read_to_string(&points_path).unwrap(),
        results,
        latency_files,
        dir,
    }
}

fn rows(points: &str) -> Vec<&str> {
    points
        .lines()
        .filter(|line| !line.starts_with('#') && !line.starts_with("concurrency,"))
        .collect()
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_finished_run_leaves_one_row_per_cell_under_one_header() {
    let run = measure(&[1, 2], FakeSearcher::new()).await;

    let lines: Vec<&str> = run.points.lines().collect();
    assert_eq!(lines[0], "# engine=scylladb");
    assert_eq!(lines[1], CSV_COLUMNS.join(","));
    assert_eq!(rows(&run.points).len(), 4);
    for row in rows(&run.points) {
        assert_eq!(row.split(',').count(), CSV_COLUMNS.len(), "{row}");
    }
    drop(run.dir);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn every_cell_leaves_a_distribution_beside_the_row_it_summarises() {
    let run = measure(&[4], FakeSearcher::new()).await;

    assert_eq!(run.latency_files, ["phrase-c4-1.csv", "rare_term-c4-1.csv"]);
    for (result, file) in run.results.iter().zip(&run.latency_files) {
        let written = std::fs::read_to_string(run.dir.path().join("latencies").join(file)).unwrap();
        let samples = written.lines().skip(1).count();
        assert_eq!(samples as u64, result.queries, "{file}");
    }
}

/// A repeated ladder measures every cell twice, and both rows and both
/// distributions have to survive it.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_repeated_ladder_keeps_both_traversals() {
    let run = measure(&[2, 2], FakeSearcher::new()).await;

    assert_eq!(rows(&run.points).len(), 4);
    assert_eq!(
        run.latency_files,
        [
            "phrase-c2-1.csv",
            "phrase-c2-2.csv",
            "rare_term-c2-1.csv",
            "rare_term-c2-2.csv"
        ]
    );
}

/// A class that matches nothing still answers, still has a p99 and still plots.
/// The run has to come back non-zero so no script picks those numbers up.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_run_that_searched_an_empty_index_does_not_come_back_successful() {
    let run = measure(&[2], FakeSearcher::new().finding(0)).await;

    assert!(run.results.iter().all(CellResult::found_nothing));
    assert_eq!(
        format!("{:?}", exit_code(&run.results, false)),
        format!("{:?}", std::process::ExitCode::FAILURE)
    );
}
