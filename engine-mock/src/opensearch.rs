//! An OpenSearch-shaped HTTP endpoint that accepts every `_bulk` and stores
//! none.
//!
//! Answers exactly what the loaders and samplers ask for: the root version
//! probe, index create, `_settings`, `_bulk`, `_refresh`, `_count`, `_stats`,
//! and the two node thread-pool endpoints the write-pool reading uses.
//! Everything else is answered 404 *and counted*, because a setup call that
//! stopped arriving would otherwise change the measurement in silence. Two
//! routes a real `osrate` sends are deliberately among them —
//! `GET /{index}/_settings` and `GET /{index}/_mapping` — and the run that uses
//! this mock expects exactly those two in `unexpected_requests` and nothing
//! else.
//!
//! **A 2xx is not enough.** A loader reads per-item statuses out of a 200
//! response and raises if any failed, so the bulk reply has to carry one item
//! per action or a run against this mock would be measuring the error path. The
//! reply bodies are built once per item count and then only copied: at 512
//! documents per bulk the loader sends thousands of identically-shaped
//! responses, and serialising each one would spend the mock's CPU on the very
//! axis being measured.
//!
//! **The version string keeps saying `null-sink`.** It is not decoration and it
//! is not stale: both harnesses stamp the engine version they read into every
//! CSV header, and a documented gate holds that a calibration run's headers
//! contain `-null-sink`. A mock that renamed itself would let a run against a
//! real engine and a run against this one become indistinguishable in the
//! artifacts.
use std::collections::HashMap;
use std::sync::Arc;

use serde_json::{json, Value};

use crate::counters::AcceptedWork;
use crate::http_wire::{Answer, Body, Request, Routes};
use crate::index::{ModelledIndex, SERVING};
use crate::server::Connection;

const DELETE_ACTION_PREFIX: &[u8] = b"{\"delete\"";
pub const VERSION: &str = "2.19.0-null-sink";
pub const WRITE_POOL_SIZE: u64 = 3;

fn shards_ok() -> Value {
    json!({"total": 1, "successful": 1, "failed": 0})
}

/// Actions in one `_bulk` body, by the NDJSON grammar.
///
/// Counted by walking the alternation rather than halving the line count,
/// because a `delete` action carries no source line: a churn bulk mixing adds
/// and deletes would otherwise be reported as fewer documents than it offered.
pub fn bulk_action_count(payload: &[u8]) -> u64 {
    let mut count = 0;
    let mut skipping_source = false;
    for line in payload.split(|byte| *byte == b'\n') {
        if line.is_empty() {
            continue;
        }
        if skipping_source {
            skipping_source = false;
            continue;
        }
        count += 1;
        skipping_source = !line.starts_with(DELETE_ACTION_PREFIX);
    }
    count
}

/// How many distinct item counts one connection remembers a reply for. A run
/// with a fixed batch size uses one; a ramp uses one per rung. The cap is here
/// because the cache is keyed on a number the client chooses, and a client that
/// varied it per request would otherwise have the mock serialising a new body
/// per request and keeping every one of them.
const MAX_CACHED_REPLIES: usize = 64;

/// Bulk response bodies, one per item count, built on first use.
#[derive(Debug, Default)]
pub struct BulkReplies {
    bodies: HashMap<u64, Arc<[u8]>>,
}

impl BulkReplies {
    pub fn body(&mut self, items: u64) -> Arc<[u8]> {
        if self.bodies.len() >= MAX_CACHED_REPLIES && !self.bodies.contains_key(&items) {
            self.bodies.clear();
        }
        Arc::clone(self.bodies.entry(items).or_insert_with(|| {
            let item = json!({"index": {"status": 201, "result": "created"}});
            let body = json!({
                "took": 0,
                "errors": false,
                "items": vec![item; items as usize],
            });
            Arc::from(body.to_string().into_bytes())
        }))
    }
}

/// What one connection keeps: its counter lane, and the bulk replies it has
/// already built.
#[derive(Debug)]
pub struct Session {
    lane: usize,
    replies: BulkReplies,
}

/// The `_all.total` subtree a sampler reads.
///
/// `docs.count` is what a search would find and `indexing.index_total` is what
/// the mock took, which are the sampler's `docs_searchable` and `docs_indexed`
/// respectively. They differ by whatever has not refreshed yet, and telling
/// them apart is the whole reason a build-rate watch can distinguish an index
/// that has stalled from one that has simply not refreshed.
///
/// Every counter that is genuinely unknowable here is 0 rather than absent: the
/// sampler indexes into these keys, and a missing one would fail the monitor
/// rather than record a mock that does not merge.
pub fn index_stats(searchable: u64, accepted: u64, refreshes: u64) -> Value {
    json!({"_all": {"total": {
        "docs": {"count": searchable, "deleted": 0},
        "indexing": {"index_total": accepted, "index_current": 0},
        "segments": {"count": 0, "memory_in_bytes": 0},
        "merges": {"current": 0, "current_docs": 0, "total": 0,
                   "total_docs": 0, "total_time_in_millis": 0},
        "refresh": {"total": refreshes, "total_time_in_millis": 0},
        "store": {"size_in_bytes": 0},
    }}})
}

pub fn index_not_found(path: &str) -> Value {
    let index = named_index(path);
    json!({"error": {"type": "index_not_found_exception",
                     "reason": format!("no such index [{index}]"),
                     "index": index},
           "status": 404})
}

fn named_index(path: &str) -> &str {
    path.trim_matches('/').split('/').next().unwrap_or(path)
}

pub fn no_shard_available(path: &str) -> Value {
    json!({"error": {"type": "no_shard_available_action_exception",
                     "reason": format!("no shard available for [{path}]")},
           "status": 503})
}

fn node_thread_pool_stats() -> Value {
    json!({"nodes": {"null-sink": {"thread_pool": {
        "write": {"active": 0, "queue": 0, "rejected": 0, "threads": WRITE_POOL_SIZE},
    }}}})
}

fn node_thread_pool_info() -> Value {
    json!({"nodes": {"null-sink": {"thread_pool": {
        "write": {"type": "fixed", "size": WRITE_POOL_SIZE},
    }}}})
}

fn is_suffix(path: &str, suffix: &str) -> bool {
    path.trim_end_matches('/').ends_with(suffix)
}

fn names_an_index(path: &str) -> bool {
    path.matches('/').count() == 1 && path != "/"
}

/// The two `HEAD`s a client actually sends: the liveness probe, and the
/// does-this-index-exist gate.
///
/// The Python sink answered *every* `HEAD` from the presence check, so a probe
/// that moved to `HEAD` would have dropped out of `unexpected_requests`
/// entirely — the one record that says a setup call stopped arriving. Any other
/// `HEAD` is refused and recorded here, like every other route the mock does
/// not answer.
fn answers_for_presence(path: &str) -> bool {
    path == "/" || names_an_index(path)
}

/// Path and method to a JSON reply, with `_bulk` counted on the way past.
pub struct Table {
    work: Arc<AcceptedWork>,
    index: Arc<ModelledIndex>,
    root: Arc<[u8]>,
    pool_stats: Arc<[u8]>,
    pool_info: Arc<[u8]>,
    acknowledged: Arc<[u8]>,
    refreshed: Arc<[u8]>,
}

impl Table {
    pub fn new(work: Arc<AcceptedWork>, index: Arc<ModelledIndex>) -> Self {
        Self {
            work,
            index,
            root: encode(&json!({"name": "null-sink", "version": {"number": VERSION}})),
            pool_stats: encode(&node_thread_pool_stats()),
            pool_info: encode(&node_thread_pool_info()),
            acknowledged: encode(&json!({"acknowledged": true})),
            refreshed: encode(&json!({"_shards": shards_ok()})),
        }
    }

    fn bulk(&self, session: &mut Session, payload: &[u8]) -> Answer {
        let items = bulk_action_count(payload);
        self.work.add(session.lane, 1, items);
        self.index.add(session.lane, items);
        (200, Body::Shared(session.replies.body(items)))
    }

    fn control(&self, request: &Request<'_>) -> Answer {
        let path = request.route();
        if request.method == "HEAD" && answers_for_presence(path) {
            return self.presence(path);
        }
        if let Some(answer) = self.progress_route(request.method, path) {
            return answer;
        }
        if let Some(answer) = self.admin_route(request.method, path) {
            return answer;
        }
        self.unanswered(request.method, path)
    }

    fn unanswered(&self, method: &str, path: &str) -> Answer {
        self.work.note_unexpected(&format!("{method} {path}"));
        (
            404,
            body(&json!({"error": "null sink does not answer this route",
                         "method": method, "path": path})),
        )
    }

    /// What a sampler reads: version, counts, index stats, write pool.
    fn progress_route(&self, method: &str, path: &str) -> Option<Answer> {
        if method != "GET" {
            return None;
        }
        match path {
            "/" => Some((200, Body::Shared(Arc::clone(&self.root)))),
            "/_nodes/stats/thread_pool" => Some((200, Body::Shared(Arc::clone(&self.pool_stats)))),
            "/_nodes/thread_pool" => Some((200, Body::Shared(Arc::clone(&self.pool_info)))),
            _ if is_suffix(path, "_count") => {
                Some(self.about_the_index(path, || self.count_body()))
            }
            _ if is_suffix(path, "_stats") => {
                Some(self.about_the_index(path, || self.stats_body()))
            }
            _ => None,
        }
    }

    /// `_count` and `_stats` answer *for an index*, so they have to answer 404
    /// when there is none and 503 while its primary is unallocated.
    ///
    /// Those are the two states `osrate`'s reset gates exist to tell apart, and
    /// a mock that reported zero documents for an index it had just deleted
    /// would let the gate for "the delete landed" pass on the index that was
    /// still there. It is also what lets one endpoint serve both gates: a
    /// `_stats` that 404s is the same answer `HEAD` gives.
    fn about_the_index(&self, path: &str, ready: impl Fn() -> Body) -> Answer {
        let Some(status) = self.index.status() else {
            return (404, body(&index_not_found(path)));
        };
        if status.status != SERVING {
            return (503, body(&no_shard_available(path)));
        }
        (200, ready())
    }

    fn count_body(&self) -> Body {
        body(&json!({"count": self.index.searchable(), "_shards": shards_ok()}))
    }

    fn stats_body(&self) -> Body {
        let stats = self.index.stats();
        body(&index_stats(
            stats.searchable,
            stats.accepted,
            stats.refreshes,
        ))
    }

    /// What a loader does around a load: create, tune, refresh, probe.
    fn admin_route(&self, method: &str, path: &str) -> Option<Answer> {
        if is_suffix(path, "_refresh") {
            return Some(self.refresh());
        }
        if method == "PUT" && is_suffix(path, "_settings") {
            return Some((200, Body::Shared(Arc::clone(&self.acknowledged))));
        }
        if matches!(method, "PUT" | "DELETE") && names_an_index(path) {
            return Some((200, self.lifecycle(method, path)));
        }
        None
    }

    /// A real `_refresh` publishes what has been indexed, and a build-rate
    /// watch that gave up waiting for a scheduled refresh asks for one. A mock
    /// that acknowledged it and published nothing would make that last resort
    /// look like an index that had genuinely stopped.
    ///
    /// Either verb, because real OpenSearch answers both and the Rust client
    /// sends `GET`. Requiring `POST` made the whole path a silent no-op: the
    /// request 404ed, the watch saw no change, and the level reported a build
    /// of zero documents that had in fact been accepted.
    fn refresh(&self) -> Answer {
        self.index.refresh();
        (200, Body::Shared(Arc::clone(&self.refreshed)))
    }

    /// Create and delete move the index the gates read back.
    fn lifecycle(&self, method: &str, path: &str) -> Body {
        if method == "DELETE" {
            self.index.drop_index();
        } else {
            self.index.create();
        }
        body(&json!({"acknowledged": true, "index": path.trim_start_matches('/')}))
    }

    /// What `osrate`'s first reset gate polls: the index it deleted is gone.
    /// Only a path that names an index answers for one — a `HEAD /` is a
    /// liveness probe and is always yes.
    fn presence(&self, path: &str) -> Answer {
        if names_an_index(path) && !self.index.present() {
            return (404, Body::Empty);
        }
        (200, Body::Empty)
    }
}

impl Routes for Table {
    type Session = Session;

    fn session(&self, connection: &Connection) -> Session {
        Session {
            lane: connection.lane,
            replies: BulkReplies::default(),
        }
    }

    fn note_malformed(&self) {
        self.work.note_unexpected("malformed http request");
    }

    fn respond(&self, request: &Request<'_>, session: &mut Session) -> Answer {
        if request.method == "POST" && is_suffix(request.route(), "_bulk") {
            return self.bulk(session, request.body);
        }
        self.control(request)
    }
}

fn encode(value: &Value) -> Arc<[u8]> {
    Arc::from(value.to_string().into_bytes())
}

fn body(value: &Value) -> Body {
    Body::Owned(value.to_string().into_bytes())
}

#[cfg(test)]
#[path = "opensearch_tests.rs"]
mod tests;
