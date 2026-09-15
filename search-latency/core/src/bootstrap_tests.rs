use std::io::Write;

use build_rate_core::index::IndexReading;
use build_rate_core::test_support::{a_reading, quiet_notes, ScriptedProbe};

use super::*;
use crate::test_support::FakeLoader;

const MAY_NOT_BUILD: BuildPolicy = BuildPolicy {
    rebuild: false,
    may_build: false,
};
const REBUILD: BuildPolicy = BuildPolicy {
    rebuild: true,
    may_build: true,
};

fn timing() -> BuildTiming {
    BuildTiming {
        poll_interval: Duration::from_millis(1),
        timeout: Duration::from_millis(200),
    }
}

fn a_corpus(lines: &[&str]) -> tempfile::NamedTempFile {
    let mut file = tempfile::NamedTempFile::new().unwrap();
    for line in lines {
        writeln!(file, "{line}").unwrap();
    }
    file.flush().unwrap();
    file
}

#[test]
fn the_corpus_count_is_the_number_of_documents_the_loader_will_read() {
    let corpus = a_corpus(&[r#"{"id":1}"#, r#"{"id":2}"#, r#"{"id":3}"#]);

    assert_eq!(count_documents(corpus.path(), 0).unwrap(), 3);
}

#[test]
fn the_corpus_count_honours_the_same_cut_the_loader_takes() {
    let corpus = a_corpus(&[r#"{"id":1}"#, r#"{"id":2}"#, r#"{"id":3}"#]);

    assert_eq!(count_documents(corpus.path(), 2).unwrap(), 2);
}

/// `build_rate_core::corpus::Lines` reads every physical line and spends one of
/// `--max-docs` on it whatever it held, so this counts the same way. A blank
/// line is a corpus the loader will refuse by name; skipping it here would only
/// make the two disagree about how many documents there were.
#[test]
fn a_blank_line_is_counted_the_way_the_loader_will_read_it() {
    let corpus = a_corpus(&[r#"{"id":1}"#, "", r#"{"id":2}"#]);

    assert_eq!(count_documents(corpus.path(), 0).unwrap(), 3);
}

#[test]
fn a_missing_corpus_names_itself() {
    let refused = count_documents(std::path::Path::new("/nowhere/corpus.jsonl"), 0)
        .unwrap_err()
        .to_string();

    assert!(refused.contains("corpus.jsonl"), "{refused}");
}

#[test]
fn an_index_holding_every_document_is_left_alone() {
    let decision = decide(&a_reading(100), 100, BuildPolicy::BUILD_IF_NEEDED);

    assert_eq!(decision, Decision::Skip { docs: 100 });
}

#[test]
fn an_absent_index_is_built() {
    let decision = decide(&IndexState::Absent, 100, BuildPolicy::BUILD_IF_NEEDED);

    assert!(matches!(decision, Decision::Build { .. }));
}

#[test]
fn a_partial_index_is_rebuilt_and_the_shortfall_is_named() {
    let Decision::Build { why } = decide(&a_reading(40), 100, BuildPolicy::BUILD_IF_NEEDED) else {
        panic!("a partial index has to be rebuilt");
    };

    assert!(why.contains("40"), "{why}");
    assert!(why.contains("100"), "{why}");
}

/// Loading this corpus on top of an index that already holds more documents
/// than it has would leave the extra ones in place and the count still wrong.
#[test]
fn an_index_holding_more_than_the_corpus_is_refused_rather_than_topped_up() {
    let Decision::Refuse { why } = decide(&a_reading(200), 100, BuildPolicy::BUILD_IF_NEEDED)
    else {
        panic!("an over-full index is not this corpus's index");
    };

    assert!(why.contains("not built from this corpus"), "{why}");
}

/// The sibling tree learned this one the expensive way: an unanswered poll
/// taken as zero produces a complete, plausible, wrong number.
#[test]
fn an_unreadable_index_is_never_guessed_at() {
    let unreadable = IndexState::Unreadable("connection refused".to_string());

    for policy in [BuildPolicy::BUILD_IF_NEEDED, REBUILD, MAY_NOT_BUILD] {
        let Decision::Refuse { why } = decide(&unreadable, 100, policy) else {
            panic!("an index nobody could read is not an index anybody may rebuild");
        };
        assert!(why.contains("connection refused"), "{why}");
    }
}

#[test]
fn a_complete_index_is_still_rebuilt_when_the_operator_asked_for_it() {
    assert!(matches!(
        decide(&a_reading(100), 100, REBUILD),
        Decision::Build { .. }
    ));
}

#[test]
fn a_run_forbidden_to_build_refuses_rather_than_measuring_a_partial_index() {
    let Decision::Refuse { why } = decide(&a_reading(40), 100, MAY_NOT_BUILD) else {
        panic!("--no-index-build must not silently measure 40 of 100 documents");
    };

    assert!(why.contains("--no-index-build"), "{why}");
}

#[tokio::test]
async fn a_complete_index_is_verified_and_not_touched() {
    let probe = ScriptedProbe::new(vec![a_reading(100)]);
    let loader = FakeLoader::loading(100);

    let ready = ensure_index(
        &probe,
        &loader,
        100,
        BuildPolicy::BUILD_IF_NEEDED,
        &timing(),
        &quiet_notes(),
    )
    .await
    .unwrap();

    assert_eq!(
        ready,
        IndexReady {
            docs: 100,
            built: false
        }
    );
    assert_eq!(loader.builds(), 0);
}

#[tokio::test]
async fn an_absent_index_is_filled_and_then_waited_for() {
    let probe = ScriptedProbe::new(vec![IndexState::Absent]);
    probe.then(&[IndexState::Absent, a_reading(0), a_reading(100)]);
    let loader = FakeLoader::loading(100);

    let ready = ensure_index(
        &probe,
        &loader,
        100,
        BuildPolicy::BUILD_IF_NEEDED,
        &timing(),
        &quiet_notes(),
    )
    .await
    .unwrap();

    assert_eq!(loader.builds(), 1);
    assert!(ready.built);
    assert_eq!(ready.docs, 100);
}

/// Legitimate here and nowhere in the sibling tree: what is timed starts after
/// the index is complete, so publishing what the engine already holds costs the
/// measurement nothing.
#[tokio::test]
async fn the_engine_is_asked_to_publish_what_it_has_before_the_wait_begins() {
    let probe = ScriptedProbe::new(vec![IndexState::Absent]).publishing(a_reading(100));

    ensure_index(
        &probe,
        &FakeLoader::loading(100),
        100,
        BuildPolicy::BUILD_IF_NEEDED,
        &timing(),
        &quiet_notes(),
    )
    .await
    .unwrap();

    assert_eq!(probe.refreshes(), 1);
}

#[tokio::test]
async fn a_load_that_lost_documents_stops_the_run_rather_than_the_gate() {
    let probe = ScriptedProbe::new(vec![IndexState::Absent]);

    let refused = ensure_index(
        &probe,
        &FakeLoader::loading(90).rejecting(10),
        100,
        BuildPolicy::BUILD_IF_NEEDED,
        &timing(),
        &quiet_notes(),
    )
    .await
    .unwrap_err()
    .to_string();

    assert!(refused.contains("10 of 100"), "{refused}");
}

#[tokio::test]
async fn an_index_that_never_catches_up_names_what_it_was_last_seen_holding() {
    let probe = ScriptedProbe::new(vec![IndexState::Absent]);
    probe.then(&[a_reading(60)]);

    let refused = ensure_index(
        &probe,
        &FakeLoader::loading(100),
        100,
        BuildPolicy::BUILD_IF_NEEDED,
        &timing(),
        &quiet_notes(),
    )
    .await
    .unwrap_err()
    .to_string();

    assert!(refused.contains("60"), "{refused}");
    assert!(refused.contains("http://sut/index/status"), "{refused}");
}

/// The gate accepts "at least", so that an index which overshoots is named
/// rather than waited on forever. This is where it is named.
#[tokio::test]
async fn an_index_that_overshoots_is_refused_after_the_gate_lets_it_through() {
    let probe = ScriptedProbe::new(vec![a_reading(150)]);

    let refused = ensure_index(
        &probe,
        &FakeLoader::loading(100),
        100,
        REBUILD,
        &timing(),
        &quiet_notes(),
    )
    .await
    .unwrap_err()
    .to_string();

    assert!(refused.contains("150"), "{refused}");
}

/// A count read from an index that is not answering queries is a count of
/// something nobody can search.
#[tokio::test]
async fn an_index_that_is_not_answering_queries_is_not_ready_to_be_measured() {
    let not_serving = IndexState::Present(IndexReading {
        docs: 100,
        accepted: None,
        status: "BUILDING".to_string(),
        ready: false,
    });
    let probe = ScriptedProbe::new(vec![not_serving]);

    let refused = ensure_index(
        &probe,
        &FakeLoader::loading(100),
        100,
        BuildPolicy::BUILD_IF_NEEDED,
        &timing(),
        &quiet_notes(),
    )
    .await
    .unwrap_err()
    .to_string();

    assert!(refused.contains("answer queries"), "{refused}");
}

/// Undercounting here would make a complete index look short and send the run
/// into a rebuild it did not need.
#[test]
fn a_line_nobody_can_read_is_an_error_rather_than_a_line_skipped() {
    let mut file = tempfile::NamedTempFile::new().unwrap();
    file.write_all(b"{\"id\":1}\n\xff\xfe not utf8 \n{\"id\":2}\n")
        .unwrap();
    file.flush().unwrap();

    let refused = count_documents(file.path(), 0).unwrap_err().to_string();

    assert!(refused.contains("line 2"), "{refused}");
}
