use std::borrow::Cow;
use std::collections::BTreeMap;
use std::time::Duration;

use super::*;
use crate::index::{Clock, Refresh, BUILDING, SERVING};

const LANES: usize = 1;
const KEYSPACE: &str = "wiki";
const INDEX: &str = "articles_body_fts";
const STATUS_PATH: &str = "/api/v1/indexes/wiki/articles_body_fts/status";

/// One routing table over one index, with the counters it records into.
struct Vstore {
    work: Arc<AcceptedWork>,
    table: Table,
}

impl Vstore {
    fn over(index: &Arc<ModelledIndex>) -> Self {
        let work = Arc::new(AcceptedWork::new(LANES));
        let table = Table::new(Arc::clone(&work), Arc::clone(index), KEYSPACE, INDEX);
        Self { work, table }
    }

    fn get(&self, path: &str) -> (u16, Value) {
        self.ask("GET", path)
    }

    fn ask(&self, method: &str, path: &str) -> (u16, Value) {
        let request = Request {
            method,
            path: Cow::Borrowed(path),
            body: b"",
        };
        let (status, body) = self.table.respond(&request, &mut ());
        (
            status,
            serde_json::from_slice(body.as_bytes()).expect("a JSON reply"),
        )
    }

    fn unexpected(&self) -> BTreeMap<String, u64> {
        self.work.unexpected()
    }
}

fn an_index(serving_delay: Duration, clock: Clock) -> Arc<ModelledIndex> {
    Arc::new(ModelledIndex::new(
        LANES,
        serving_delay,
        Refresh::immediately(),
        clock,
    ))
}

fn an_index_nobody_created() -> Arc<ModelledIndex> {
    an_index(Duration::ZERO, Clock::monotonic())
}

fn a_serving_index() -> Arc<ModelledIndex> {
    let index = an_index_nobody_created();
    index.create();
    index
}

fn recorded_once(what: &str) -> BTreeMap<String, u64> {
    BTreeMap::from([(what.to_string(), 1)])
}

#[test]
fn status_reports_what_the_cql_half_accepted() {
    let index = a_serving_index();
    index.add(0, 270_269);

    let (status, body) = Vstore::over(&index).get(STATUS_PATH);

    assert_eq!(status, 200);
    assert_eq!(body, json!({"count": 270_269, "status": SERVING}));
}

/// `scyllarate` reads `count` and `status` by name and defaults each when it is
/// missing (`build-rate/scylla/src/vstore.rs`), so a third field is harmless
/// but a renamed one is not: a missing `count` reads as zero documents, which
/// is exactly the state the post-reset gate is waiting for.
#[test]
fn the_status_body_names_exactly_the_two_fields_scyllarate_reads() {
    let index = a_serving_index();
    index.add(0, 7);

    let (_, body) = Vstore::over(&index).get(STATUS_PATH);

    let fields: Vec<String> = body
        .as_object()
        .expect("a JSON object")
        .keys()
        .cloned()
        .collect();
    assert_eq!(fields, ["count", "status"]);
}

/// A count answered for an index nobody created lets a misdirected run pass its
/// own gate and report a complete, plausible, wrong build rate.
#[test]
fn an_index_nobody_created_is_404() {
    let (status, _) = Vstore::over(&an_index_nobody_created()).get(STATUS_PATH);

    assert_eq!(status, 404);
}

/// A count that survives a DROP lets level 2 inherit level 1's documents.
#[test]
fn a_dropped_index_goes_back_to_404() {
    let index = a_serving_index();
    index.add(0, 5);
    index.drop_index();

    let (status, _) = Vstore::over(&index).get(STATUS_PATH);

    assert_eq!(status, 404);
}

/// A harness pointed at the wrong index must fail its gate, not sail through it
/// on another index's count.
#[test]
fn another_index_is_refused_and_recorded() {
    let vstore = Vstore::over(&a_serving_index());

    let (status, _) = vstore.get("/api/v1/indexes/wiki/some_other_index/status");

    assert_eq!(status, 404);
    assert_eq!(
        vstore.unexpected(),
        recorded_once("GET /api/v1/indexes/wiki/some_other_index/status")
    );
}

#[test]
fn another_keyspace_is_refused_and_recorded() {
    let vstore = Vstore::over(&a_serving_index());

    let (status, _) = vstore.get("/api/v1/indexes/other/articles_body_fts/status");

    assert_eq!(status, 404);
    assert_eq!(
        vstore.unexpected(),
        recorded_once("GET /api/v1/indexes/other/articles_body_fts/status")
    );
}

/// The version annotates a run's own header, and `-null-sink` in it is what
/// keeps a mock reading out of a table of engine results.
#[test]
fn info_answers_the_version_a_run_header_records() {
    let (status, body) = Vstore::over(&an_index_nobody_created()).get(INFO_PATH);

    assert_eq!(status, 200);
    let version = body["version"].as_str().expect("a version string");
    assert_eq!(version, VERSION);
    assert!(version.contains("-null-sink"), "{version}");
}

#[test]
fn an_unknown_route_is_refused_and_recorded() {
    let vstore = Vstore::over(&a_serving_index());

    let (status, _) = vstore.get("/api/v1/indexes");

    assert_eq!(status, 404);
    assert_eq!(vstore.unexpected(), recorded_once("GET /api/v1/indexes"));
}

/// Refusing in silence would let a harness that started writing to the status
/// path land as a throughput difference: the run still completes, the number
/// still looks like a client ceiling, and no artifact says the call was dropped.
#[test]
fn a_write_to_the_status_path_is_refused_and_recorded() {
    let vstore = Vstore::over(&a_serving_index());

    let (status, _) = vstore.ask("POST", STATUS_PATH);

    assert_eq!(status, 404);
    assert_eq!(
        vstore.unexpected(),
        recorded_once("POST /api/v1/indexes/wiki/articles_body_fts/status")
    );
}

#[test]
fn status_target_reads_the_keyspace_and_index_a_status_path_names() {
    assert_eq!(status_target(STATUS_PATH), Some((KEYSPACE, INDEX)));
}

#[test]
fn status_target_refuses_anything_that_is_not_one_keyspace_and_one_index() {
    for path in [
        "/api/v1/indexes/wiki/status",
        "/api/v1/indexes/wiki/articles/body_fts/status",
        "/api/v1/indexes//articles_body_fts/status",
        "/api/v1/indexes/wiki//status",
        "/api/v1/indexes//status",
        "/api/v1/info",
    ] {
        assert_eq!(status_target(path), None, "{path}");
    }
}

/// The reset gate is only proven to work against a mock that can fail it once.
#[test]
fn an_index_inside_its_serving_delay_reports_building_with_its_count() {
    let clock = Clock::manual();
    let index = an_index(Duration::from_millis(500), clock.clone());
    index.create();
    index.add(0, 12);
    let vstore = Vstore::over(&index);

    clock.advance(Duration::from_millis(100));
    let building = vstore.get(STATUS_PATH);
    clock.advance(Duration::from_millis(800));
    let serving = vstore.get(STATUS_PATH);

    assert_eq!(building, (200, json!({"count": 12, "status": BUILDING})));
    assert_eq!(serving, (200, json!({"count": 12, "status": SERVING})));
}
