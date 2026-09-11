use super::*;

const PAGE_ID: i64 = 193002;

fn a_document_line(page_id: i64) -> String {
    format!(
        r#"{{"id": {page_id}, "uuid": "3d1e-not-read", "title": "Washington (footballer)", "text": "Washington is a Brazilian football player."}}"#
    ) + "\n"
}

fn a_corpus(lines: &str) -> (tempfile::TempDir, PathBuf) {
    let tmp = tempfile::tempdir().unwrap();
    let path = tmp.path().join("corpus.jsonl");
    std::fs::write(&path, lines).unwrap();
    (tmp, path)
}

fn read_batches(path: &Path, max_docs: usize, batch_size: usize) -> Result<Vec<DocumentBatch>> {
    CorpusSource::new(path, max_docs, batch_size).open()?.collect()
}

fn batch_sizes(batches: &[DocumentBatch]) -> Vec<u64> {
    batches.iter().map(DocumentBatch::docs).collect()
}

fn a_corpus_of(documents: i64) -> (tempfile::TempDir, PathBuf) {
    let lines: String = (1..=documents).map(a_document_line).collect();
    a_corpus(&lines)
}

#[test]
fn maps_a_document_onto_the_wiki_articles_mapping() {
    let (_tmp, path) = a_corpus(&a_document_line(PAGE_ID));
    assert_eq!(
        read_batches(&path, 0, 10).unwrap()[0].documents(),
        [BulkDoc {
            page_id: PAGE_ID,
            title: "Washington (footballer)".to_string(),
            body: "Washington is a Brazilian football player.".to_string(),
        }]
    );
}

/// The corpus line's `uuid` is ScyllaDB's partition key. OpenSearch keys by the
/// page id, so a corpus without a uuid at all still loads here.
#[test]
fn a_corpus_line_without_a_uuid_still_reads() {
    let (_tmp, path) = a_corpus("{\"id\": 7, \"title\": \"t\", \"text\": \"b\"}\n");
    assert_eq!(read_batches(&path, 0, 10).unwrap()[0].docs(), 1);
}

#[test]
fn the_document_id_is_the_page_id_as_a_string() {
    assert_eq!(
        BulkDoc {
            page_id: PAGE_ID,
            title: String::new(),
            body: String::new(),
        }
        .document_id(),
        "193002"
    );
}

#[test]
fn groups_documents_into_batches_of_the_requested_size() {
    let (_tmp, path) = a_corpus_of(10);
    assert_eq!(
        batch_sizes(&read_batches(&path, 0, 5).unwrap()),
        vec![5, 5]
    );
}

/// The corpus rarely divides by the batch size, and a short final batch is a
/// real request rather than documents to drop.
#[test]
fn the_last_batch_is_short_rather_than_padded_or_dropped() {
    let (_tmp, path) = a_corpus_of(7);
    assert_eq!(batch_sizes(&read_batches(&path, 0, 3).unwrap()), vec![3, 3, 1]);
}

#[test]
fn a_batch_size_of_one_is_one_document_per_request() {
    let (_tmp, path) = a_corpus_of(3);
    assert_eq!(batch_sizes(&read_batches(&path, 0, 1).unwrap()), vec![1, 1, 1]);
}

#[test]
fn a_batch_larger_than_the_corpus_is_one_short_batch() {
    let (_tmp, path) = a_corpus_of(3);
    assert_eq!(batch_sizes(&read_batches(&path, 0, 512).unwrap()), vec![3]);
}

#[test]
fn every_document_survives_the_batching() {
    let (_tmp, path) = a_corpus_of(10);
    let batches = read_batches(&path, 0, 3).unwrap();
    let ids: Vec<i64> = batches
        .iter()
        .flat_map(|batch| batch.documents().iter().map(|doc| doc.page_id))
        .collect();
    assert_eq!(ids, (1..=10).collect::<Vec<i64>>());
}

#[test]
fn stops_at_the_document_limit_not_at_a_batch_boundary() {
    let (_tmp, path) = a_corpus_of(10);
    assert_eq!(batch_sizes(&read_batches(&path, 4, 3).unwrap()), vec![3, 1]);
}

#[test]
fn a_source_can_be_reopened_from_the_start_for_every_level() {
    let (_tmp, path) = a_corpus_of(4);
    let source = CorpusSource::new(&path, 0, 2);
    let first: Vec<_> = source.open().unwrap().collect();
    let second: Vec<_> = source.open().unwrap().collect();
    assert_eq!((first.len(), second.len()), (2, 2));
}

/// A malformed line fails the batch it fell in, and through it the point: a
/// level that quietly delivered fewer documents is not comparable to the rest.
#[test]
fn a_malformed_line_names_the_file_and_the_line() {
    let (_tmp, path) = a_corpus(&(a_document_line(1) + "{not json\n"));
    let failure = format!("{:#}", read_batches(&path, 0, 10).unwrap_err());
    assert!(failure.contains("line 2") && failure.contains("malformed JSON"));
}

#[test]
fn a_malformed_line_fails_even_when_earlier_batches_were_whole() {
    let lines = (1..=4).map(a_document_line).collect::<String>() + "{not json\n";
    let (_tmp, path) = a_corpus(&lines);
    assert!(read_batches(&path, 0, 2).is_err());
}

#[test]
fn a_missing_corpus_says_which_file_was_missing() {
    let failure = format!(
        "{:#}",
        read_batches(Path::new("/nonexistent/corpus.jsonl"), 0, 10).unwrap_err()
    );
    assert!(failure.contains("cannot read corpus") && failure.contains("corpus.jsonl"));
}

#[test]
fn an_empty_corpus_yields_no_batches_rather_than_one_empty_one() {
    let (_tmp, path) = a_corpus("");
    assert!(read_batches(&path, 0, 10).unwrap().is_empty());
}
