use super::*;

const PAGE_ID: i64 = 193002;

fn a_document_line(page_id: i64) -> String {
    format!(
        r#"{{"id": {page_id}, "uuid": "{}", "title": "Washington (footballer)", "text": "Washington is a Brazilian football player."}}"#,
        page_uuid(page_id)
    ) + "\n"
}

fn page_uuid(page_id: i64) -> Uuid {
    Uuid::new_v5(
        &Uuid::NAMESPACE_URL,
        format!("wikipedia-page:{page_id}").as_bytes(),
    )
}

fn a_corpus(lines: &str) -> (tempfile::TempDir, PathBuf) {
    let tmp = tempfile::tempdir().unwrap();
    let path = tmp.path().join("corpus.jsonl");
    std::fs::write(&path, lines).unwrap();
    (tmp, path)
}

fn read_all(path: &Path, max_docs: usize) -> Result<Vec<InsertParams>> {
    CorpusSource::new(path, max_docs).open()?.collect()
}

#[test]
fn maps_a_document_onto_the_articles_columns() {
    let (_tmp, path) = a_corpus(&a_document_line(PAGE_ID));
    assert_eq!(
        read_all(&path, 0).unwrap(),
        vec![InsertParams {
            article_id: page_uuid(PAGE_ID),
            page_id: PAGE_ID,
            title: "Washington (footballer)".to_string(),
            body: "Washington is a Brazilian football player.".to_string(),
        }]
    );
}

#[test]
fn reads_every_document_when_no_limit_is_given() {
    let lines: String = (1..=3).map(a_document_line).collect();
    let (_tmp, path) = a_corpus(&lines);
    assert_eq!(read_all(&path, 0).unwrap().len(), 3);
}

#[test]
fn stops_at_the_document_limit() {
    let lines: String = (1..=3).map(a_document_line).collect();
    let (_tmp, path) = a_corpus(&lines);
    assert_eq!(read_all(&path, 2).unwrap().len(), 2);
}

#[test]
fn a_source_can_be_reopened_from_the_start_for_every_level() {
    let (_tmp, path) = a_corpus(&a_document_line(1));
    let source = CorpusSource::new(&path, 0);
    let first: Vec<_> = source.open().unwrap().collect();
    let second: Vec<_> = source.open().unwrap().collect();
    assert_eq!((first.len(), second.len()), (1, 1));
}

#[test]
fn a_malformed_line_names_the_file_and_the_line() {
    let (_tmp, path) = a_corpus(&(a_document_line(1) + "{not json\n"));
    let failure = format!("{:#}", read_all(&path, 0).unwrap_err());
    assert!(failure.contains("line 2") && failure.contains("malformed JSON"));
}

#[test]
fn a_missing_corpus_says_which_file_was_missing() {
    let failure = format!(
        "{:#}",
        read_all(Path::new("/nonexistent/corpus.jsonl"), 0).unwrap_err()
    );
    assert!(failure.contains("cannot read corpus") && failure.contains("corpus.jsonl"));
}

#[test]
fn an_empty_corpus_yields_nothing() {
    let (_tmp, path) = a_corpus("");
    assert!(read_all(&path, 0).unwrap().is_empty());
}
