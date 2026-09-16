use crate::cell::Sample;

use super::*;

fn sample(latency_ms: f64, elapsed_s: f64) -> Sample {
    Sample {
        elapsed_s,
        latency_ms,
    }
}

#[test]
fn a_cell_writes_its_distribution_with_the_run_facts_above_it() {
    let dir = tempfile::tempdir().unwrap();
    let files = LatencyFiles::new(dir.path())
        .unwrap()
        .with_preamble(vec!["# engine=scylladb".to_string()]);

    let path = files
        .write_cell(
            "rare_term",
            16,
            &[sample(1.5, 0.1), sample(2.25, 0.05), sample(9.0, 0.2)],
        )
        .unwrap();

    let written = std::fs::read_to_string(&path).unwrap();
    assert_eq!(
        written,
        "# engine=scylladb\nlatency_ms,elapsed_s\n1.500,0.100\n2.250,0.050\n9.000,0.200\n"
    );
}

/// A repeated ladder measures a cell twice, and the second measurement must not
/// erase the first.
#[test]
fn a_repeated_cell_gets_its_own_file() {
    let dir = tempfile::tempdir().unwrap();
    let files = LatencyFiles::new(dir.path()).unwrap();

    let first = files.write_cell("phrase", 8, &[sample(1.0, 0.0)]).unwrap();
    let second = files.write_cell("phrase", 8, &[sample(2.0, 0.0)]).unwrap();

    assert_eq!(first.file_name().unwrap(), "phrase-c8-1.csv");
    assert_eq!(second.file_name().unwrap(), "phrase-c8-2.csv");
    assert!(first.exists() && second.exists());
}

#[test]
fn two_classes_at_one_concurrency_do_not_collide() {
    let dir = tempfile::tempdir().unwrap();
    let files = LatencyFiles::new(dir.path()).unwrap();

    let rare = files
        .write_cell("rare_term", 8, &[sample(1.0, 0.0)])
        .unwrap();
    let phrase = files.write_cell("phrase", 8, &[sample(1.0, 0.0)]).unwrap();

    assert_ne!(rare, phrase);
}

#[test]
fn the_directory_is_created_rather_than_required() {
    let dir = tempfile::tempdir().unwrap();
    let nested = dir.path().join("run").join("latencies");

    assert!(LatencyFiles::new(&nested).is_ok());
    assert!(nested.is_dir());
}
