//! The one index the CQL endpoint and the vector-store endpoint both pretend to
//! hold.
//!
//! `scyllarate` empties the keyspace before every concurrency level and will
//! not start loading until the vector-store reports the *new* index at count 0
//! and status SERVING. Against an accept-and-discard mock that gate has to be
//! answerable, or the harness cannot be measured against the instrument built
//! to measure it — so the DDL arriving on the CQL side moves the state the HTTP
//! side reports.
//!
//! **Not `AcceptedWork`.** That counter is cumulative for the whole process: it
//! feeds the summary line and `--stats-out`, and a `DROP KEYSPACE` zeroing it
//! would make a six-level ladder report the documents of its last level as the
//! documents of the run. This one is per-index and resets with the index.
//!
//! **Documents arriving while no index exists are not counted.** The harness
//! creates the index before it loads, so an add against an absent index means
//! the loader and the mock disagree about the lifecycle — recorded, not
//! silently absorbed, for the same reason the endpoints record unexpected
//! routes.
//!
//! **Accepted and searchable are two numbers, because on OpenSearch they are.**
//! `count` is what the mock took; `searchable` is what a search would find, and
//! it only catches up at a refresh. With the refresh interval at its default of
//! zero the two are equal at every instant, which is what the vector-store half
//! means and what every run recorded before this existed measured. Set it and
//! the searchable count climbs in steps instead — the shape `osrate` has to be
//! able to measure, and one an accept-everything mock would otherwise never
//! show it.
//!
//! **Lock-free where the documents are.** `add` is called once per `_bulk` and
//! once per CQL mutation, from every worker at once; it reads one atomic flag
//! and adds to one striped counter, and never takes the lock. Everything that
//! does take it — create, drop, refresh, and every read of the count — happens
//! at the poll interval or at a level boundary, where a mutex costs nothing.
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use crate::counters::Striped;

pub const SERVING: &str = "SERVING";
pub const BUILDING: &str = "BUILDING";

/// When what has been accepted becomes what a search would find.
///
/// `Never` is OpenSearch's `refresh_interval: -1`, and it is not the same as a
/// very long interval: nothing becomes visible on a timer at all, only an
/// explicit refresh. That is the setting under which a build-rate watch that
/// waited on the searchable count alone would wait forever, so the mock has to
/// be able to produce it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Refresh {
    Every(Duration),
    Never,
}

impl Refresh {
    pub fn immediately() -> Self {
        Self::Every(Duration::ZERO)
    }
}

/// Monotonic time, or time a test moves by hand.
///
/// The index's two timed behaviours — the delay before a fresh index serves,
/// and the interval between refreshes — are the ones a test would otherwise
/// have to sleep through, one real second at a time.
#[derive(Debug, Clone)]
pub enum Clock {
    Monotonic(Instant),
    Manual(Arc<Mutex<Duration>>),
}

impl Clock {
    pub fn monotonic() -> Self {
        Self::Monotonic(Instant::now())
    }

    pub fn manual() -> Self {
        Self::Manual(Arc::new(Mutex::new(Duration::ZERO)))
    }

    pub fn now(&self) -> Duration {
        match self {
            Self::Monotonic(origin) => origin.elapsed(),
            Self::Manual(held) => *held.lock().expect("manual clock"),
        }
    }

    pub fn advance(&self, by: Duration) {
        if let Self::Manual(held) = self {
            *held.lock().expect("manual clock") += by;
        }
    }
}

impl Default for Clock {
    fn default() -> Self {
        Self::monotonic()
    }
}

/// What one `_stats` reply reports, taken as one reading.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct IndexStats {
    pub searchable: u64,
    pub accepted: u64,
    pub refreshes: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IndexStatus {
    pub count: u64,
    pub status: &'static str,
}

impl IndexStatus {
    pub fn as_json(&self) -> Value {
        json!({"count": self.count, "status": self.status})
    }

    pub fn is_serving(&self) -> bool {
        self.status == SERVING
    }
}

/// What survives a create and a drop: everything that is not a running total.
#[derive(Debug)]
struct Lifecycle {
    created_at: Option<Duration>,
    /// The accepted total at the last lifecycle change. The index's own count
    /// is measured from here, so a reset never has to zero a counter that
    /// running connections are still adding to.
    origin: u64,
    searchable: u64,
    refreshed_at: Duration,
    refresh_total: u64,
}

/// One index's lifecycle: absent, then building, then serving.
#[derive(Debug)]
pub struct ModelledIndex {
    present: AtomicBool,
    accepted: Striped,
    absent_adds: Striped,
    state: Mutex<Lifecycle>,
    serving_delay: Duration,
    refresh: Refresh,
    clock: Clock,
}

impl ModelledIndex {
    pub fn new(lanes: usize, serving_delay: Duration, refresh: Refresh, clock: Clock) -> Self {
        Self {
            present: AtomicBool::new(false),
            accepted: Striped::new(lanes),
            absent_adds: Striped::new(lanes),
            state: Mutex::new(Lifecycle {
                created_at: None,
                origin: 0,
                searchable: 0,
                refreshed_at: Duration::ZERO,
                refresh_total: 0,
            }),
            serving_delay,
            refresh,
            clock,
        }
    }

    /// Created up front, because that is the state a loader meets: the campaign
    /// applies its DDL before anything writes, and a `--no-reset` ladder issues
    /// none at all. A mock that started with no index would answer 404 to a run
    /// that was right to expect one.
    pub fn created(lanes: usize, serving_delay: Duration, refresh: Refresh, clock: Clock) -> Self {
        let index = Self::new(lanes, serving_delay, refresh, clock);
        index.create();
        index
    }

    pub fn present(&self) -> bool {
        self.present.load(Ordering::Relaxed)
    }

    /// The base offset is captured **before** the index becomes present, and
    /// that order is the whole correctness of the pair.
    ///
    /// `add` reads the flag and then adds, and nothing makes those two steps
    /// one. With the flag set first, an add that lands in the window between
    /// them is counted into `accepted` *and* into the offset measured from it —
    /// dropped, and dropped in the only way this instrument cannot report.
    /// Capturing the offset first turns the same window into an add that finds
    /// no index, which is recorded in `index_adds_while_absent` and visible in
    /// the artifact. The documents in it are real either way; the question is
    /// only whether the mock admits to them.
    pub fn create(&self) {
        let now = self.clock.now();
        let mut state = self.state.lock().expect("index lifecycle");
        self.forget_documents(&mut state);
        self.present.store(true, Ordering::Relaxed);
        state.created_at = Some(now);
        state.refreshed_at = now;
    }

    /// The same window as `create`'s, in the direction the reorder cannot close.
    ///
    /// A thread can read `present` as true and be descheduled arbitrarily long
    /// before it adds, so no offset this takes — first or last — bounds it.
    /// Closing it would need a generation the add stamps atomically with its
    /// increment, or a drain, and both put shared state back on the path this
    /// index exists to keep off it. It is left open because what falls in it is
    /// a document of the generation being dropped: left out of that index's
    /// count, which nothing reads once `status` answers `None`, and absorbed by
    /// the next `create`. It is never carried into the new index, and it is
    /// never missing from `docs_accepted` — `AcceptedWork` is a separate
    /// cumulative total that this check does not gate. The one thing it costs
    /// is that a loader still writing during its own `DROP` has that document
    /// attributed to the old index rather than named in
    /// `index_adds_while_absent`.
    pub fn drop_index(&self) {
        let mut state = self.state.lock().expect("index lifecycle");
        self.present.store(false, Ordering::Relaxed);
        state.created_at = None;
        self.forget_documents(&mut state);
    }

    fn forget_documents(&self, state: &mut Lifecycle) {
        state.origin = self.accepted.total();
        state.searchable = 0;
        state.refresh_total = 0;
    }

    pub fn add(&self, lane: usize, docs: u64) {
        if self.present() {
            self.accepted.add(lane, docs);
            return;
        }
        self.absent_adds.add(lane, docs);
    }

    pub fn adds_while_absent(&self) -> u64 {
        self.absent_adds.total()
    }

    pub fn count(&self) -> u64 {
        let state = self.state.lock().expect("index lifecycle");
        self.count_locked(&state)
    }

    fn count_locked(&self, state: &Lifecycle) -> u64 {
        self.accepted.total().saturating_sub(state.origin)
    }

    /// What a search would find now, publishing first if a scheduled refresh
    /// has come due since anyone last looked.
    pub fn searchable(&self) -> u64 {
        let mut state = self.state.lock().expect("index lifecycle");
        if self.due_for_refresh(&state) {
            self.publish(&mut state);
        }
        state.searchable
    }

    pub fn refresh_total(&self) -> u64 {
        self.state.lock().expect("index lifecycle").refresh_total
    }

    /// The three numbers an OpenSearch `_stats` reports, read under one lock and
    /// in the order the reply needs them.
    ///
    /// Order first: reading the searchable count is what performs a scheduled
    /// refresh, so a reply that read `refresh_total` before it would report the
    /// refresh one poll behind the documents it just published — which is the
    /// exact signature a build-rate watch reads as "the index published without
    /// a refresh".
    ///
    /// One lock second: three separate reads can interleave with a fourth
    /// poller's refresh and report an accepted count from before it beside a
    /// searchable count from after, which is a negative lag.
    pub fn stats(&self) -> IndexStats {
        let mut state = self.state.lock().expect("index lifecycle");
        if self.due_for_refresh(&state) {
            self.publish(&mut state);
        }
        IndexStats {
            searchable: state.searchable,
            accepted: self.count_locked(&state),
            refreshes: state.refresh_total,
        }
    }

    /// An explicit refresh, which is what a build-rate watch asks for when it
    /// has given up waiting for a scheduled one.
    pub fn refresh(&self) {
        let mut state = self.state.lock().expect("index lifecycle");
        self.publish(&mut state);
    }

    fn due_for_refresh(&self, state: &Lifecycle) -> bool {
        match self.refresh {
            Refresh::Never => false,
            Refresh::Every(interval) => {
                self.clock.now().saturating_sub(state.refreshed_at) >= interval
            }
        }
    }

    /// `refresh_total` counts refreshes that published something, not refreshes
    /// that happened.
    ///
    /// The scheduled ones are modelled lazily — they occur when someone looks —
    /// so counting every one would report how often the harness polled rather
    /// than how often the index turned over, and that number would move with
    /// `--index-interval` while nothing about the build had changed.
    fn publish(&self, state: &mut Lifecycle) {
        let count = self.count_locked(state);
        if state.searchable != count {
            state.refresh_total += 1;
        }
        state.searchable = count;
        state.refreshed_at = self.clock.now();
    }

    /// `None` where the real vector-store answers 404: no such index.
    pub fn status(&self) -> Option<IndexStatus> {
        let state = self.state.lock().expect("index lifecycle");
        let created_at = state.created_at?;
        Some(IndexStatus {
            count: self.count_locked(&state),
            status: self.phase(created_at),
        })
    }

    fn phase(&self, created_at: Duration) -> &'static str {
        if self.clock.now().saturating_sub(created_at) < self.serving_delay {
            return BUILDING;
        }
        SERVING
    }
}

#[cfg(test)]
#[path = "index_tests.rs"]
mod tests;
