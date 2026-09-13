use std::io::Write;

use serde::Deserialize;

use super::*;

#[derive(Debug, Clone, PartialEq, Deserialize)]
struct Doc {
    id: i64,
}

fn a_corpus(lines: &str) -> (tempfile::TempDir, PathBuf) {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("corpus.jsonl");
    let mut file = std::fs::File::create(&path).unwrap();
    file.write_all(lines.as_bytes()).unwrap();
    (dir, path)
}

fn three_documents() -> String {
    (1..=3)
        .map(|id| format!("{{\"id\":{id}}}\n"))
        .collect::<String>()
}

#[test]
fn every_line_becomes_a_document() {
    let (_dir, path) = a_corpus(&three_documents());
    let read: Vec<Doc> = CorpusSource::new(&path, 0)
        .open::<Doc>()
        .unwrap()
        .map(Result::unwrap)
        .collect();

    assert_eq!(read.len(), 3);
    assert_eq!(read[0], Doc { id: 1 });
}

/// Zero means the whole corpus, which is what every full run passes.
#[test]
fn max_docs_zero_reads_everything() {
    let (_dir, path) = a_corpus(&three_documents());
    assert_eq!(CorpusSource::new(&path, 0).open::<Doc>().unwrap().count(), 3);
}

#[test]
fn max_docs_stops_the_level_where_it_was_asked_to() {
    let (_dir, path) = a_corpus(&three_documents());
    assert_eq!(CorpusSource::new(&path, 2).open::<Doc>().unwrap().count(), 2);
}

/// A fresh reader per call, so every concurrency level reads the corpus from
/// the start and the levels are comparable.
#[test]
fn each_level_reads_the_corpus_from_the_beginning() {
    let (_dir, path) = a_corpus(&three_documents());
    let source = CorpusSource::new(&path, 0);

    let first: Vec<Doc> = source.open::<Doc>().unwrap().map(Result::unwrap).collect();
    let second: Vec<Doc> = source.open::<Doc>().unwrap().map(Result::unwrap).collect();

    assert_eq!(first, second);
}

/// A truncated corpus happens, and "line 2" is what lets someone go and look at
/// it.
#[test]
fn a_malformed_line_names_the_file_and_the_line() {
    let (_dir, path) = a_corpus("{\"id\":1}\n{not json}\n");
    let failed = CorpusSource::new(&path, 0)
        .open::<Doc>()
        .unwrap()
        .nth(1)
        .unwrap()
        .unwrap_err();
    let said = format!("{failed:#}");

    assert!(said.contains("line 2"), "{said}");
    assert!(said.contains("malformed JSON"), "{said}");
    assert!(said.contains("corpus.jsonl"), "{said}");
}

#[test]
fn a_corpus_that_is_not_there_fails_before_the_first_level() {
    assert!(CorpusSource::new("/nonexistent/corpus.jsonl", 0)
        .open::<Doc>()
        .is_err());
}

// --- batching -------------------------------------------------------------

fn batched(lines: usize, size: usize) -> Vec<Vec<Doc>> {
    chunks((1..=lines).map(|id| Ok(Doc { id: id as i64 })), size)
        .map(Result::unwrap)
        .collect()
}

#[test]
fn documents_are_grouped_into_runs_of_the_asked_size() {
    let batches = batched(6, 2);
    assert_eq!(batches.len(), 3);
    assert!(batches.iter().all(|batch| batch.len() == 2));
}

/// The last run is short whenever the corpus does not divide, which is why a
/// consumer asks a batch how many documents it carries.
#[test]
fn the_last_run_is_short_rather_than_padded() {
    let batches = batched(5, 2);
    assert_eq!(
        batches.iter().map(Vec::len).collect::<Vec<_>>(),
        [2, 2, 1]
    );
}

#[test]
fn an_empty_corpus_yields_no_runs_rather_than_one_empty_one() {
    assert!(batched(0, 4).is_empty());
}

/// A truncated line has to stop the level, not silently shorten it.
#[test]
fn a_failure_part_way_through_a_run_reaches_the_caller() {
    let documents = vec![
        Ok(Doc { id: 1 }),
        Err(anyhow::anyhow!("truncated JSONL line 4242")),
    ];
    let failed = chunks(documents.into_iter(), 4).next().unwrap().unwrap_err();

    assert!(format!("{failed:#}").contains("truncated JSONL line 4242"));
}
