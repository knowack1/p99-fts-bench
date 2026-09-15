use std::borrow::Cow;
use std::net::SocketAddr;
use std::time::Duration;

use super::*;
use crate::index::{Clock, Refresh};

const LANES: usize = 1;

/// A routing table over a fresh counter, with one connection's session open.
struct Mock {
    work: Arc<AcceptedWork>,
    table: Table,
    session: Session,
}

impl Mock {
    fn over(index: ModelledIndex) -> Self {
        let work = Arc::new(AcceptedWork::new(LANES));
        let table = Table::new(Arc::clone(&work), Arc::new(index));
        let session = table.session(&Connection {
            lane: 0,
            local: local_address(),
        });
        Self {
            work,
            table,
            session,
        }
    }

    fn answer(&mut self, method: &str, path: &str, body: &[u8]) -> Answer {
        let request = Request {
            method,
            path: Cow::Borrowed(path),
            body,
        };
        self.table.respond(&request, &mut self.session)
    }

    fn json(&mut self, method: &str, path: &str, body: &[u8]) -> (u16, Value) {
        let (status, reply) = self.answer(method, path, body);
        (status, parsed(&reply))
    }

    fn get(&mut self, path: &str) -> (u16, Value) {
        self.json("GET", path, b"")
    }

    fn bulk(&mut self, ids: &[u64]) -> Answer {
        self.answer("POST", "/_bulk", &bulk_body("wiki-articles", ids))
    }

    fn docs_accepted(&self) -> u64 {
        self.work.snapshot().docs
    }

    fn recorded(&self) -> Vec<String> {
        self.work.unexpected().into_keys().collect()
    }
}

fn local_address() -> SocketAddr {
    "127.0.0.1:9200".parse().expect("a test address")
}

fn created_index(serving_delay: Duration, refresh: Refresh, clock: Clock) -> ModelledIndex {
    ModelledIndex::created(LANES, serving_delay, refresh, clock)
}

fn a_mock() -> Mock {
    Mock::over(created_index(
        Duration::ZERO,
        Refresh::immediately(),
        Clock::default(),
    ))
}

fn a_mock_that_never_refreshes() -> Mock {
    Mock::over(created_index(
        Duration::ZERO,
        Refresh::Never,
        Clock::manual(),
    ))
}

fn bulk_body(index: &str, ids: &[u64]) -> Vec<u8> {
    let mut lines = Vec::new();
    for id in ids {
        lines.push(json!({"index": {"_index": index, "_id": id.to_string()}}).to_string());
        lines.push(json!({"page_id": id, "title": "t", "body": "b"}).to_string());
    }
    (lines.join("\n") + "\n").into_bytes()
}

fn delete_action(id: u64) -> Vec<u8> {
    format!(
        "{}\n",
        json!({"delete": {"_index": "i", "_id": id.to_string()}})
    )
    .into_bytes()
}

fn parsed(body: &Body) -> Value {
    serde_json::from_slice(body.as_bytes()).expect("a JSON reply")
}

fn shared(body: &Body) -> Arc<[u8]> {
    match body {
        Body::Shared(bytes) => Arc::clone(bytes),
        other => panic!("a bulk reply is answered from the cache, got {other:?}"),
    }
}

fn total_of(stats: &Value) -> &Value {
    &stats["_all"]["total"]
}

#[test]
fn a_bulk_counts_one_action_per_document_offered() {
    assert_eq!(bulk_action_count(&bulk_body("i", &[1, 2, 3])), 3);
}

/// A `delete` action carries no source line, so counting by halving the lines
/// would report a churn bulk as fewer documents than it offered.
#[test]
fn a_delete_action_that_carries_no_source_line_still_counts_one() {
    let payload = [delete_action(1), bulk_body("i", &[2])].concat();
    assert_eq!(bulk_action_count(&payload), 2);
}

#[test]
fn a_bulk_mixing_adds_and_deletes_counts_every_action() {
    let payload = [
        delete_action(1),
        bulk_body("i", &[2, 3]),
        delete_action(4),
        bulk_body("i", &[5]),
    ]
    .concat();
    assert_eq!(bulk_action_count(&payload), 5);
}

#[test]
fn a_trailing_blank_line_changes_no_count() {
    let payload = [bulk_body("i", &[1]), b"\n".to_vec()].concat();
    assert_eq!(bulk_action_count(&payload), 1);
}

/// A loader reads per-item statuses out of a 200 response and raises if any
/// failed, so a reply that did not carry one item per action would make a run
/// against this mock measure the error path.
#[test]
fn a_bulk_reply_carries_one_item_per_action() {
    let mut mock = a_mock();

    let (status, reply) = mock.bulk(&[1, 2]);
    let reply = parsed(&reply);
    let work = mock.work.snapshot();

    assert_eq!(status, 200);
    assert_eq!(reply["errors"], false);
    assert_eq!(reply["items"].as_array().expect("items").len(), 2);
    assert_eq!((work.ops, work.docs), (1, 2));
}

/// At 512 documents a bulk the loader sends thousands of identically-shaped
/// responses, and serialising each one would spend the mock's CPU on the very
/// axis being measured.
#[test]
fn a_reply_body_for_a_given_item_count_is_built_once_and_reused() {
    let mut mock = a_mock();

    let (_, first) = mock.bulk(&[1, 2]);
    let (_, again) = mock.bulk(&[3, 4]);

    assert!(Arc::ptr_eq(&shared(&first), &shared(&again)));
}

#[test]
fn count_reports_what_the_mock_accepted() {
    let mut mock = a_mock();
    mock.bulk(&[1, 2, 3]);

    let (_, counted) = mock.get("/wiki-articles/_count");

    assert_eq!(counted["count"], 3);
}

/// `osrate` empties the index before every concurrency level.
///
/// Every route that answers *for an index* has to say it is gone, not just
/// `HEAD`: a `_count` or `_stats` reporting zero documents for an index that
/// does not exist would let the gate waiting for the delete to land pass on the
/// index that is still there.
#[test]
fn a_deleted_index_answers_absent_everywhere_it_is_asked() {
    let mut mock = a_mock();
    mock.bulk(&[1, 2, 3]);
    mock.json("DELETE", "/wiki-articles", b"");

    let (present, _) = mock.answer("HEAD", "/wiki-articles", b"");
    let (counted, body) = mock.get("/wiki-articles/_count");
    let (stats, _) = mock.get("/wiki-articles/_stats");

    assert_eq!((present, counted, stats), (404, 404, 404));
    assert_eq!(body["error"]["type"], "index_not_found_exception");
    assert_eq!(
        mock.docs_accepted(),
        3,
        "the run's own total is not the index's"
    );
}

/// A created index answers before its primary is allocated, and `osrate`'s
/// second gate waits for that. 200 with zero documents would let the gate pass
/// on an index nothing could be loaded into yet.
#[test]
fn an_index_that_is_not_serving_yet_is_503_not_empty() {
    let clock = Clock::manual();
    let mut mock = Mock::over(created_index(
        Duration::from_secs(5),
        Refresh::immediately(),
        clock.clone(),
    ));

    let (unready, body) = mock.get("/wiki-articles/_count");
    clock.advance(Duration::from_secs(6));
    let (ready, _) = mock.get("/wiki-articles/_count");

    assert_eq!(unready, 503);
    assert_eq!(body["error"]["type"], "no_shard_available_action_exception");
    assert_eq!(ready, 200);
}

/// The shape a build-rate watch has to be able to measure: `_count` steps at
/// the refresh interval while `index_total` climbs continuously.
#[test]
fn documents_are_searchable_only_after_a_refresh() {
    let clock = Clock::manual();
    let mut mock = Mock::over(created_index(
        Duration::ZERO,
        Refresh::Every(Duration::from_secs(3)),
        clock.clone(),
    ));
    mock.bulk(&[1, 2, 3]);

    clock.advance(Duration::from_secs(1));
    let (_, before) = mock.get("/wiki-articles/_stats");
    clock.advance(Duration::from_secs(3));
    let (_, after) = mock.get("/wiki-articles/_stats");

    assert_eq!(total_of(&before)["docs"]["count"], 0);
    assert_eq!(total_of(&before)["indexing"]["index_total"], 3);
    assert_eq!(total_of(&after)["docs"]["count"], 3);
}

/// `refresh_interval: -1` publishes nothing on a timer, so a watch that gave up
/// waiting asks for a refresh. A mock that acknowledged it without publishing
/// would make that last resort look like a stalled index.
#[test]
fn a_refresh_request_publishes_what_the_mock_accepted() {
    let mut mock = a_mock_that_never_refreshes();
    mock.bulk(&[1, 2, 3]);

    let (_, never) = mock.get("/wiki-articles/_count");
    mock.get("/wiki-articles/_refresh");
    let (_, asked) = mock.get("/wiki-articles/_count");

    assert_eq!(never["count"], 0);
    assert_eq!(asked["count"], 3);
}

/// Real OpenSearch answers `_refresh` on both, and the Rust client sends `GET`.
/// Requiring `POST` made the forced refresh a silent no-op: 404, no change, and
/// a level reporting a build of zero documents it had accepted.
#[test]
fn a_refresh_is_answered_on_either_verb() {
    for method in ["GET", "POST"] {
        let mut mock = a_mock_that_never_refreshes();
        mock.bulk(&[1, 2, 3]);

        let (acknowledged, _) = mock.json(method, "/wiki-articles/_refresh", b"");
        let (_, counted) = mock.get("/wiki-articles/_count");

        assert_eq!(acknowledged, 200, "{method}");
        assert_eq!(counted["count"], 3, "{method}");
    }
}

/// Every run recorded before the refresh model existed measured a mock where
/// accepted and searchable were the same number, and the default has to keep
/// meaning that.
#[test]
fn by_default_nothing_waits_for_a_refresh() {
    let mut mock = a_mock();
    mock.bulk(&[1, 2, 3]);

    let (_, counted) = mock.get("/wiki-articles/_count");
    let (_, stats) = mock.get("/wiki-articles/_stats");

    assert_eq!(counted["count"], 3);
    assert_eq!(
        total_of(&stats)["docs"]["count"],
        total_of(&stats)["indexing"]["index_total"]
    );
}

#[test]
fn a_recreated_index_is_present_and_empty() {
    let mut mock = a_mock();
    mock.bulk(&[1, 2, 3]);
    mock.json("DELETE", "/wiki-articles", b"");
    mock.json("PUT", "/wiki-articles", b"{}");

    let (present, _) = mock.answer("HEAD", "/wiki-articles", b"");
    let (_, counted) = mock.get("/wiki-articles/_count");

    assert_eq!(present, 200);
    assert_eq!(counted["count"], 0);
}

/// A `--no-reset` run loads into an index it never created.
#[test]
fn an_index_is_there_before_anything_created_it() {
    let mut mock = a_mock();

    let (present, _) = mock.answer("HEAD", "/wiki-articles", b"");

    assert_eq!(present, 200);
}

#[test]
fn a_head_that_does_not_name_an_index_is_still_answered() {
    let mut mock = a_mock();
    mock.json("DELETE", "/wiki-articles", b"");

    let (alive, _) = mock.answer("HEAD", "/", b"");

    assert_eq!(alive, 200);
}

/// The sampler indexes into these keys, so a missing one fails the monitor
/// rather than leaving a null column.
#[test]
fn stats_answers_every_field_the_sampler_indexes_into() {
    let mut mock = a_mock();
    mock.bulk(&[1, 2, 3, 4, 5, 6, 7]);

    let (_, stats) = mock.get("/wiki-articles/_stats");
    let reported = total_of(&stats);

    assert_eq!(reported["indexing"]["index_total"], 7);
    for key in ["docs", "indexing", "segments", "merges", "refresh", "store"] {
        assert!(reported.get(key).is_some(), "{key}");
    }
}

/// A setup call that stopped arriving would otherwise change the measurement in
/// silence: the run completes, the number still looks like a client ceiling,
/// and nothing in the artifacts says the call never landed.
#[test]
fn an_unknown_route_is_404_and_recorded() {
    let mut mock = a_mock();

    let (status, _) = mock.get("/_cat/indices");

    assert_eq!(status, 404);
    assert_eq!(mock.work.unexpected().get("GET /_cat/indices"), Some(&1));
}

/// A real `osrate` run sends exactly these two routes the mock does not answer,
/// and the run that uses it expects exactly them in `unexpected_requests` and
/// nothing else — so an answered route appearing here, or one of these two
/// going missing, breaks a gate either way.
#[test]
fn the_settings_and_mapping_reads_an_osrate_run_sends_are_the_recorded_ones() {
    let mut mock = a_mock();

    mock.get("/wiki-articles/_settings");
    mock.get("/wiki-articles/_mapping");
    mock.json("PUT", "/wiki-articles/_settings", b"{}");

    assert_eq!(
        mock.recorded(),
        [
            "GET /wiki-articles/_mapping",
            "GET /wiki-articles/_settings"
        ]
    );
}

#[test]
fn a_create_a_settings_change_and_a_refresh_are_each_acknowledged() {
    let mut mock = a_mock();

    for (method, path) in [
        ("PUT", "/wiki-articles"),
        ("PUT", "/wiki-articles/_settings"),
        ("POST", "/wiki-articles/_refresh"),
    ] {
        let (status, body) = mock.json(method, path, b"{}");

        assert_eq!(status, 200, "{method} {path}");
        assert!(
            !body.as_object().expect("a JSON object").is_empty(),
            "{method} {path}"
        );
    }
}

/// Both harnesses stamp the engine version they read into every CSV header, and
/// a documented gate holds that a calibration run's headers contain
/// `-null-sink`. A mock that renamed itself would let a run against a real
/// engine and a run against this one become indistinguishable in the artifacts.
#[test]
fn the_root_probe_names_a_version_that_says_null_sink() {
    let mut mock = a_mock();

    let (status, root) = mock.get("/");
    let version = root["version"]["number"]
        .as_str()
        .expect("a version string");

    assert_eq!(status, 200);
    assert!(version.contains("-null-sink"), "{version}");
}

#[test]
fn the_two_node_thread_pool_endpoints_answer_the_write_pool_shape() {
    let mut mock = a_mock();

    let (_, stats) = mock.get("/_nodes/stats/thread_pool");
    let (_, info) = mock.get("/_nodes/thread_pool");
    let running = &stats["nodes"]["null-sink"]["thread_pool"]["write"];
    let configured = &info["nodes"]["null-sink"]["thread_pool"]["write"];

    assert_eq!(running["threads"], WRITE_POOL_SIZE);
    assert_eq!(running["queue"], 0);
    assert_eq!(configured["size"], WRITE_POOL_SIZE);
    assert_eq!(configured["type"], "fixed");
}

/// The Python sink answered every `HEAD` from the presence check, whatever it
/// named, so a probe that moved to `HEAD` would have dropped out of
/// `unexpected_requests` entirely — the one record that says a setup call
/// stopped arriving. The two a client really sends still answer for the index.
#[test]
fn a_head_of_something_that_is_not_an_index_is_refused_and_recorded() {
    let mut mock = a_mock();

    let (probe, _) = mock.answer("HEAD", "/", b"");
    let (index, _) = mock.answer("HEAD", "/wiki-articles", b"");
    let (elsewhere, _) = mock.answer("HEAD", "/_cat/indices", b"");

    assert_eq!((probe, index, elsewhere), (200, 200, 404));
    assert_eq!(mock.recorded(), vec!["HEAD /_cat/indices".to_string()]);
}

/// The reply cache is keyed on a number the client chooses. A client that
/// varied it per request would otherwise have the mock serialising a new body
/// per request and keeping every one of them — an unbounded footprint on the
/// path that answers documents.
#[test]
fn the_reply_cache_does_not_grow_without_end_when_every_bulk_is_a_new_size() {
    let mut replies = BulkReplies::default();

    for items in 1..=(MAX_CACHED_REPLIES as u64 * 2) {
        replies.body(items);
    }

    assert!(replies.bodies.len() <= MAX_CACHED_REPLIES);
}

/// A reply built once and then only copied is the difference between
/// serialising thousands of identical documents-accepted bodies and memcpying
/// them, on the axis the run is measuring.
#[test]
fn a_bulk_of_a_size_already_answered_hands_back_the_same_body() {
    let mut mock = a_mock();

    let (_, first) = mock.bulk(&[1, 2, 3]);
    let (_, again) = mock.bulk(&[4, 5, 6]);

    match (first, again) {
        (Body::Shared(first), Body::Shared(again)) => {
            assert!(Arc::ptr_eq(&first, &again), "the body was rebuilt");
        }
        other => panic!("a bulk reply should be shared, got {other:?}"),
    }
}

/// The Python matched `_bulk` on the raw path, so a query string turned a bulk
/// into an unanswered route: 404, no items, and the loader bailing on a reply
/// whose item count disagreed with what it offered. Every other route here
/// already stripped the query, and now this one does too.
#[test]
fn a_bulk_that_carries_a_query_string_is_still_a_bulk() {
    let mut mock = a_mock();

    let (status, reply) = mock.json(
        "POST",
        "/wiki-articles/_bulk?refresh=false",
        &bulk_body("wiki-articles", &[1, 2]),
    );

    assert_eq!(status, 200);
    assert_eq!(reply["items"].as_array().expect("items").len(), 2);
    assert_eq!(mock.docs_accepted(), 2);
    assert!(mock.recorded().is_empty(), "{:?}", mock.recorded());
}

/// `is_suffix` trims a trailing slash before matching, which is the difference
/// between answering `/wiki-articles/_count/` and recording it as a route the
/// mock does not have.
#[test]
fn a_route_is_matched_with_or_without_its_trailing_slash() {
    let mut mock = a_mock();
    mock.bulk(&[1, 2, 3]);

    let (plain, counted) = mock.get("/wiki-articles/_count");
    let (slashed, also) = mock.get("/wiki-articles/_count/");

    assert_eq!((plain, slashed), (200, 200));
    assert_eq!(counted["count"], also["count"]);
}

/// A path with a byte over 0x7F reaches the routing table, because the framing
/// deliberately carries one rather than dropping the connection. Naming the
/// created index used to slice that path by byte index, which panics inside a
/// multi-byte character and takes the connection's task with it — `PUT é/x` is
/// enough, because a path is only required to hold one `/` to name an index.
#[test]
fn a_path_that_is_not_ascii_does_not_panic_the_connection() {
    let mut mock = a_mock();

    let (named, body) = mock.json("PUT", "/é", b"{}");
    let (unslashed, _) = mock.json("PUT", "é/x", b"{}");

    assert_eq!(named, 200);
    assert_eq!(body["index"], json!("é"));
    assert_eq!(unslashed, 200);
}
