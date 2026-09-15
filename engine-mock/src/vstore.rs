//! A vector-store-shaped HTTP endpoint that reports the index the CQL endpoint
//! holds.
//!
//! Answers what `ftsbench.samplers.ScyllaSampler` and the Rust `scyllarate`
//! harness ask of a real vector-store: `/api/v1/indexes/{ks}/{idx}/status` for
//! the document count and the SERVING gate, and `/api/v1/info` for the version
//! a run header records. Nothing is indexed — the count is what the CQL half
//! accepted, which is the whole point: it makes the harness's build-rate number
//! measurable against a mock that cannot be the constraint.
//!
//! **An index nobody created is 404, and so is one under another name.** The
//! real vector-store has no entry to answer for either, and answering a count
//! anyway would let a harness pointed at the wrong keyspace or index sail
//! through its own SERVING gate and report a complete, plausible, wrong build
//! rate. A misdirected status request is therefore refused *and* recorded, like
//! every other unexpected route.
use std::sync::Arc;

use serde_json::{json, Value};

use crate::counters::AcceptedWork;
use crate::http_wire::{Answer, Body, Request, Routes};
use crate::index::ModelledIndex;
use crate::server::Connection;

pub const VERSION: &str = "1.10.0-null-sink";
pub const ENGINE: &str = "null-sink — accept and discard, nothing is indexed";
pub const INDEXES_PREFIX: &str = "/api/v1/indexes/";
pub const STATUS_SUFFIX: &str = "/status";
pub const INFO_PATH: &str = "/api/v1/info";

/// The `{keyspace}/{index}` a status request names, or `None` if it is not one.
pub fn status_target(path: &str) -> Option<(&str, &str)> {
    let middle = path
        .strip_prefix(INDEXES_PREFIX)?
        .strip_suffix(STATUS_SUFFIX)?;
    let (keyspace, index) = middle.split_once('/')?;
    if keyspace.is_empty() || index.is_empty() || index.contains('/') {
        return None;
    }
    Some((keyspace, index))
}

/// Path and method to a JSON reply, for one named index.
pub struct Table {
    work: Arc<AcceptedWork>,
    index: Arc<ModelledIndex>,
    keyspace: String,
    name: String,
    info: Arc<[u8]>,
    absent: Arc<[u8]>,
}

impl Table {
    pub fn new(
        work: Arc<AcceptedWork>,
        index: Arc<ModelledIndex>,
        keyspace: &str,
        name: &str,
    ) -> Self {
        Self {
            work,
            index,
            keyspace: keyspace.to_string(),
            name: name.to_string(),
            info: Arc::from(
                json!({"version": VERSION, "engine": ENGINE})
                    .to_string()
                    .into_bytes(),
            ),
            absent: Arc::from(
                json!({"error": "no such index", "keyspace": keyspace, "index": name})
                    .to_string()
                    .into_bytes(),
            ),
        }
    }

    fn status(&self, target: (&str, &str)) -> Answer {
        if target != (self.keyspace.as_str(), self.name.as_str()) {
            let (keyspace, index) = target;
            return self.unanswered(
                "GET",
                &format!("{INDEXES_PREFIX}{keyspace}/{index}{STATUS_SUFFIX}"),
            );
        }
        match self.index.status() {
            None => (404, Body::Shared(Arc::clone(&self.absent))),
            Some(status) => (200, body(&status.as_json())),
        }
    }

    fn unanswered(&self, method: &str, path: &str) -> Answer {
        self.work.note_unexpected(&format!("{method} {path}"));
        (
            404,
            body(&json!({"error": "null sink does not answer this route",
                         "method": method, "path": path})),
        )
    }
}

impl Routes for Table {
    type Session = ();

    fn session(&self, _connection: &Connection) -> Self::Session {}

    fn note_malformed(&self) {
        self.work.note_unexpected("malformed http request");
    }

    fn respond(&self, request: &Request<'_>, _session: &mut Self::Session) -> Answer {
        let path = request.route();
        if request.method == "GET" && path == INFO_PATH {
            return (200, Body::Shared(Arc::clone(&self.info)));
        }
        match status_target(path) {
            Some(target) if request.method == "GET" => self.status(target),
            _ => self.unanswered(request.method, path),
        }
    }
}

fn body(value: &Value) -> Body {
    Body::Owned(value.to_string().into_bytes())
}

#[cfg(test)]
#[path = "vstore_tests.rs"]
mod tests;
