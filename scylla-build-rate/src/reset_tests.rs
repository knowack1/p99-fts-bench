//! The gates are the difference between a reset and a hope.
//!
//! `DROP KEYSPACE` returns before the vector-store has noticed, and `CREATE
//! CUSTOM INDEX` returns before the index answers queries. A gate that passed
//! early would let a level load against the index it just dropped, or against
//! one that is not registering documents yet, and either produces a complete,
//! plausible, wrong build rate.
use std::path::PathBuf;
use std::time::Duration;

use super::*;
use crate::fakes::{quiet_notes, FakeVectorStore, Reply};
use crate::vstore::DEFAULT_VS_INDEX;

const A_TIMEOUT: Duration = Duration::from_secs(5);

fn a_plan() -> ResetPlan {
    ResetPlan {
        keyspace: "wiki".to_string(),
        table: "articles".to_string(),
        index: DEFAULT_VS_INDEX.to_string(),
    }
}

fn brisk(timeout: Duration) -> GateTiming {
    GateTiming {
        poll_interval: Duration::from_millis(10),
        timeout,
    }
}

async fn probe_for(store: &FakeVectorStore) -> IndexProbe {
    IndexProbe::new(store.url(), "wiki", DEFAULT_VS_INDEX, A_TIMEOUT).unwrap()
}

// --- the statements -------------------------------------------------------

#[test]
fn the_drop_takes_the_keyspace_and_with_it_the_table_and_index() {
    assert_eq!(a_plan().drop_statement(), "DROP KEYSPACE IF EXISTS wiki");
}

#[test]
fn the_creates_are_keyspace_then_table_then_index() {
    let statements = a_plan().create_statements();
    assert!(statements[0].starts_with("CREATE KEYSPACE wiki"));
    assert!(statements[1].starts_with("CREATE TABLE wiki.articles"));
    assert!(statements[2].starts_with("CREATE CUSTOM INDEX articles_body_fts ON wiki.articles"));
}

/// An index must exist before the load, not after: that is the CDC tail path,
/// which is what the campaign measures.
#[test]
fn the_index_is_created_before_any_document_is_written() {
    let statements = a_plan().create_statements();
    assert_eq!(statements.len(), 3);
    assert!(statements.last().unwrap().contains("fulltext_index"));
}

#[test]
fn no_create_says_if_not_exists() {
    for statement in a_plan().create_statements() {
        assert!(
            !statement.to_lowercase().contains("if not exists"),
            "a create that tolerates a survivor would hand this level the last \
             level's documents: {statement}"
        );
    }
}

#[test]
fn the_flags_reach_every_statement() {
    let plan = ResetPlan {
        keyspace: "ks2".to_string(),
        table: "docs".to_string(),
        index: "docs_fts".to_string(),
    };
    let rendered = format!(
        "{} {}",
        plan.drop_statement(),
        plan.create_statements().join(" ")
    );

    for placeholder in ["{keyspace}", "{table}", "{index}"] {
        assert!(
            !rendered.contains(placeholder),
            "unfilled {placeholder} in: {rendered}"
        );
    }
    assert!(rendered.contains("CREATE TABLE ks2.docs"));
    assert!(rendered.contains("CREATE CUSTOM INDEX docs_fts ON ks2.docs(body)"));
}

// --- conformance with the campaign's own schema ---------------------------

fn campaign_cql(name: &str) -> String {
    let path: PathBuf = [env!("CARGO_MANIFEST_DIR"), "..", "scylladb", name]
        .iter()
        .collect();
    let text = std::fs::read_to_string(&path)
        .unwrap_or_else(|exc| panic!("cannot read {}: {exc}", path.display()));
    strip_comments(&text)
}

fn strip_comments(text: &str) -> String {
    let body: Vec<&str> = text
        .lines()
        .map(|line| line.split("--").next().unwrap_or(""))
        .collect();
    normalize(&body.join(" "))
}

fn normalize(text: &str) -> String {
    text.split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .to_lowercase()
}

/// Everything between the outermost parentheses, split into column definitions.
fn column_definitions(statement: &str) -> Vec<String> {
    let open = statement.find('(').expect("a table statement has columns");
    let close = statement.rfind(')').expect("a table statement closes them");
    statement[open + 1..close]
        .split(',')
        .map(normalize)
        .collect()
}

fn replication_map(statement: &str) -> String {
    let open = statement
        .find('{')
        .expect("a keyspace statement replicates");
    let close = statement.rfind('}').expect("and closes the map");
    statement[open..=close]
        .replace([' ', '\n'], "")
        .to_lowercase()
}

/// `scylladb/schema.cql` is the campaign's source of truth and these statements
/// are a second copy of it. A column added to one alone would not fail
/// anything — the harness would simply load a different table from the one
/// every other producer reads.
#[test]
fn the_table_matches_the_campaigns_schema_cql() {
    let theirs = campaign_cql("schema.cql");
    let ours = normalize(&a_plan().create_statements()[1]);

    let mut expected = column_definitions(&theirs);
    let mut actual = column_definitions(&ours);
    expected.sort();
    actual.sort();
    assert_eq!(actual, expected);
}

#[test]
fn the_replication_matches_the_campaigns_schema_cql() {
    assert_eq!(
        replication_map(&a_plan().create_statements()[0]),
        replication_map(&campaign_cql("schema.cql"))
    );
}

#[test]
fn the_index_matches_the_campaigns_index_cql() {
    let theirs = campaign_cql("index.cql");
    let ours = normalize(&a_plan().create_statements()[2]);

    assert!(theirs.contains(DEFAULT_VS_INDEX), "index name: {theirs}");
    assert!(ours.contains(DEFAULT_VS_INDEX));
    for fragment in ["on wiki.articles(body)", "using 'fulltext_index'"] {
        assert!(
            theirs.contains(fragment),
            "{fragment} missing from index.cql"
        );
        assert!(ours.contains(fragment), "{fragment} missing from ours");
    }
}

// --- gate A: the drop reached the vector-store ----------------------------

#[tokio::test]
async fn the_drop_gate_passes_once_the_index_is_gone() {
    let store = FakeVectorStore::start(Reply::Serving(270_269)).await;
    let probe = probe_for(&store).await;
    let timing = brisk(Duration::from_secs(5));
    store.then(&[
        Reply::Serving(270_269),
        Reply::Serving(270_269),
        Reply::Absent,
    ]);

    Gate::new(&probe, &timing, &quiet_notes())
        .await_dropped()
        .await
        .unwrap();
}

/// Not phrased as "404": an index the vector-store has stopped serving is gone
/// for this purpose, whatever code it chooses to say so with.
#[tokio::test]
async fn an_index_that_stopped_serving_counts_as_dropped() {
    let store = FakeVectorStore::start(Reply::Building(0)).await;
    let probe = probe_for(&store).await;

    Gate::new(&probe, &brisk(Duration::from_secs(5)), &quiet_notes())
        .await_dropped()
        .await
        .unwrap();
}

#[tokio::test]
async fn the_drop_gate_refuses_to_pass_while_the_old_index_still_serves() {
    let store = FakeVectorStore::start(Reply::Serving(270_269)).await;
    let probe = probe_for(&store).await;
    let failure = Gate::new(&probe, &brisk(Duration::from_millis(60)), &quiet_notes())
        .await_dropped()
        .await
        .unwrap_err();
    let said = format!("{failure:#}");

    assert!(said.contains("the dropped index to disappear"));
    assert!(said.contains("SERVING at 270269 docs"));
    assert!(said.contains("/api/v1/indexes/wiki/articles_body_fts/status"));
}

// --- gate B: the new index is ready ---------------------------------------

#[tokio::test]
async fn the_ready_gate_passes_on_a_serving_empty_index() {
    let store = FakeVectorStore::start(Reply::Absent).await;
    let probe = probe_for(&store).await;
    store.then(&[Reply::Absent, Reply::Building(0), Reply::Serving(0)]);

    Gate::new(&probe, &brisk(Duration::from_secs(5)), &quiet_notes())
        .await_empty_and_serving()
        .await
        .unwrap();
}

/// The reason gate A exists: an index that is SERVING with the previous
/// level's documents in it must not satisfy the gate that starts the load.
#[tokio::test]
async fn a_serving_index_that_still_holds_documents_is_not_ready() {
    let store = FakeVectorStore::start(Reply::Serving(270_269)).await;
    let probe = probe_for(&store).await;
    let failure = Gate::new(&probe, &brisk(Duration::from_millis(60)), &quiet_notes())
        .await_empty_and_serving()
        .await
        .unwrap_err();

    assert!(format!("{failure:#}").contains("SERVING at 270269 docs"));
}

#[tokio::test]
async fn an_index_still_building_is_not_ready() {
    let store = FakeVectorStore::start(Reply::Building(0)).await;
    let probe = probe_for(&store).await;
    let failure = Gate::new(&probe, &brisk(Duration::from_millis(60)), &quiet_notes())
        .await_empty_and_serving()
        .await
        .unwrap_err();

    assert!(format!("{failure:#}").contains("BUILDING at 0 docs"));
}

/// A vector-store is legitimately unreachable for a moment while a keyspace
/// drop propagates; the deadline, not the first failed poll, decides that the
/// moment has lasted too long.
#[tokio::test]
async fn a_transient_failure_is_polled_through() {
    let store = FakeVectorStore::start(Reply::Failing(503)).await;
    let probe = probe_for(&store).await;
    store.then(&[Reply::Failing(503), Reply::Failing(503), Reply::Serving(0)]);

    Gate::new(&probe, &brisk(Duration::from_secs(5)), &quiet_notes())
        .await_empty_and_serving()
        .await
        .unwrap();
}

#[tokio::test]
async fn an_endpoint_that_never_answers_times_out_saying_so() {
    let probe = IndexProbe::new(
        "http://127.0.0.1:1",
        "wiki",
        "idx",
        Duration::from_millis(50),
    )
    .unwrap();
    let failure = Gate::new(&probe, &brisk(Duration::from_millis(120)), &quiet_notes())
        .await_empty_and_serving()
        .await
        .unwrap_err();

    assert!(format!("{failure:#}").contains("unreadable"));
}
