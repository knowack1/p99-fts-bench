//! What a mock engine accepted, and what it was asked that it did not expect.
//!
//! Shared by every endpoint so the HTTP and CQL halves cannot disagree about
//! what "one operation" and "one document" mean — the same reason one loader
//! schedule serves both loaders.
//!
//! **Sharded, because the port is to threads.** The Python sink these counters
//! come from could add two integers behind no lock: one asyncio loop, one
//! thread, no contention possible. Here every tokio worker answers requests at
//! once, and a single shared `AtomicU64` would put all of them on one cache
//! line — the one line every document in the run has to touch. That is a
//! contention point inside the instrument whose entire purpose is to not be the
//! constraint, so the hot counters are striped across padded shards and summed
//! only when someone reads them, which happens at the report interval rather
//! than per document.
//!
//! Unexpected requests are recorded rather than merely refused. A sink that
//! answered 404 in silence would let a loader change land as a throughput
//! difference: the run would still complete, the number would still look like a
//! client ceiling, and nothing in the artifacts would say the setup call never
//! arrived. They are rare by construction, so they live behind a mutex.
use std::collections::BTreeMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::Instant;

use serde_json::{json, Value};

/// One counter per shard, each alone on its own cache line. Without the
/// padding the shards share a line and the striping buys nothing: the line
/// still bounces between the cores that own the shards on it.
/// A run against a correctly configured harness records two keys; a thousand
/// is far past anything that is still a diagnosis rather than a flood.
const MAX_UNEXPECTED_KEYS: usize = 1024;
const OVERFLOW_KEY: &str = "(further distinct unexpected requests, not listed)";

#[repr(align(64))]
#[derive(Debug, Default)]
struct Shard(AtomicU64);

/// A counter every connection adds to and nobody reads on the hot path.
#[derive(Debug)]
pub struct Striped {
    shards: Box<[Shard]>,
}

impl Striped {
    pub fn new(shards: usize) -> Self {
        Self {
            shards: (0..shards.max(1)).map(|_| Shard::default()).collect(),
        }
    }

    /// `Relaxed` is the whole point: these counters order nothing. A sum is a
    /// progress reading, and the only sum that has to be exact is the one taken
    /// after every connection has closed.
    pub fn add(&self, lane: usize, amount: u64) {
        self.shards[lane % self.shards.len()]
            .0
            .fetch_add(amount, Ordering::Relaxed);
    }

    pub fn total(&self) -> u64 {
        self.shards
            .iter()
            .map(|shard| shard.0.load(Ordering::Relaxed))
            .sum()
    }
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct WorkSnapshot {
    pub ops: u64,
    pub docs: u64,
    pub elapsed_s: f64,
}

impl WorkSnapshot {
    pub fn ops_per_s(&self) -> f64 {
        self.per_second(self.ops)
    }

    pub fn docs_per_s(&self) -> f64 {
        self.per_second(self.docs)
    }

    fn per_second(&self, count: u64) -> f64 {
        if self.elapsed_s > 0.0 {
            count as f64 / self.elapsed_s
        } else {
            0.0
        }
    }

    pub fn summary_line(&self) -> String {
        format!(
            "sink: {} docs, {} ops in {:.1}s ({:.0} docs/s, {:.0} ops/s)",
            self.docs,
            self.ops,
            self.elapsed_s,
            self.docs_per_s(),
            self.ops_per_s()
        )
    }
}

/// Accept-and-discard accounting for one mock process.
#[derive(Debug)]
pub struct AcceptedWork {
    ops: Striped,
    docs: Striped,
    unexpected: Mutex<BTreeMap<String, u64>>,
    origin: Instant,
}

impl AcceptedWork {
    pub fn new(lanes: usize) -> Self {
        Self {
            ops: Striped::new(lanes),
            docs: Striped::new(lanes),
            unexpected: Mutex::new(BTreeMap::new()),
            origin: Instant::now(),
        }
    }

    /// `lane` is the connection's own stripe, handed out at accept. Requests on
    /// one connection are answered by one task, so a lane is never contended
    /// with itself and rarely with anything else.
    pub fn add(&self, lane: usize, ops: u64, docs: u64) {
        self.ops.add(lane, ops);
        self.docs.add(lane, docs);
    }

    /// Bounded, because one of the keys is not a route: an EXECUTE of a
    /// statement id nobody prepared is recorded under that id, and a client
    /// that has lost its prepared statements sends a new one every request. The
    /// overflow key keeps the count honest without letting a confused client
    /// spend the mock's memory on strings.
    pub fn note_unexpected(&self, what: &str) {
        let mut seen = self.unexpected.lock().expect("unexpected-route map");
        if seen.len() >= MAX_UNEXPECTED_KEYS && !seen.contains_key(what) {
            *seen.entry(OVERFLOW_KEY.to_string()).or_insert(0) += 1;
            return;
        }
        *seen.entry(what.to_string()).or_insert(0) += 1;
    }

    pub fn unexpected(&self) -> BTreeMap<String, u64> {
        self.unexpected
            .lock()
            .expect("unexpected-route map")
            .clone()
    }

    pub fn snapshot(&self) -> WorkSnapshot {
        WorkSnapshot {
            ops: self.ops.total(),
            docs: self.docs.total(),
            elapsed_s: self.origin.elapsed().as_secs_f64(),
        }
    }

    /// The keys a run's gates read: `docs_accepted` reconciles against what the
    /// harness CSVs claim to have submitted, and `unexpected_requests` is how a
    /// dropped setup call is caught. Renaming either silently breaks a gate.
    pub fn summary(&self) -> Value {
        self.summary_of(self.snapshot())
    }

    /// Taken from a snapshot the caller already has, so the summary line on
    /// stderr and the JSON on disk report one reading rather than two taken
    /// moments apart — two rates that disagree read as a mock that lost
    /// documents.
    pub fn summary_of(&self, snapshot: WorkSnapshot) -> Value {
        json!({
            "ops_accepted": snapshot.ops,
            "docs_accepted": snapshot.docs,
            "sink_wall_s": round(snapshot.elapsed_s, 3),
            "sink_ops_per_s": round(snapshot.ops_per_s(), 1),
            "sink_docs_per_s": round(snapshot.docs_per_s(), 1),
            "unexpected_requests": self.unexpected(),
        })
    }
}

/// Decimal rounding, to match the `round(x, n)` the Python producers apply to
/// the same fields (`ftsbench/build_report.py`, `build_monitor.py`): these
/// numbers are compared between runs and between producers, so an unrounded
/// one reads as a different measurement rather than as a different formatter.
///
/// Half-away-from-zero, where Python is half-to-even. They differ only on an
/// exact tie at the rounding digit, which a measured rate reaches essentially
/// never, and agreeing with Python there would cost a decimal crate.
fn round(value: f64, places: u32) -> f64 {
    let factor = 10_f64.powi(places as i32);
    (value * factor).round() / factor
}

#[cfg(test)]
#[path = "counters_tests.rs"]
mod tests;
