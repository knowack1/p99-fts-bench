//! Driver-shaped stand-ins: enough of an inserter, a corpus and a topology to
//! drive the channel, the workers and the CSV without a ScyllaDB node.
//!
//! Completions are deferred with a real sleep rather than returning ready, so
//! requests genuinely overlap and `max_in_flight` measures the real in-flight
//! bound.
use std::collections::HashSet;
use std::future::Future;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use anyhow::{anyhow, Result};
use uuid::Uuid;

use crate::corpus::InsertParams;
use crate::notes::Notes;
use crate::report::PointResult;
use crate::session::Topology;
use crate::sweep::Inserter;

pub fn a_point(concurrency: usize) -> PointResult {
    a_point_with_latency(concurrency, Some(1.5), Some(9.0))
}

pub fn a_point_with_errors(concurrency: usize, errors: u64) -> PointResult {
    PointResult {
        errors,
        ..a_point(concurrency)
    }
}

pub fn a_point_with_latency(
    concurrency: usize,
    p50_ms: Option<f64>,
    p99_ms: Option<f64>,
) -> PointResult {
    PointResult {
        concurrency,
        docs: 100,
        errors: 0,
        wall_s: 2.0,
        docs_per_s: 50.0,
        p50_ms,
        p99_ms,
    }
}

pub fn a_topology() -> Topology {
    Topology {
        scylla_version: "2026.3.0-rc2".to_string(),
        routing: "DefaultPolicy(token_aware)".to_string(),
        compression: "None".to_string(),
        driver_version: "1.8.0".to_string(),
        protocol_version: "4".to_string(),
        runtime: "tokio multi_thread workers:8".to_string(),
        shard_aware: "true".to_string(),
        shards: "127.0.0.1:9042=shards:3".to_string(),
        connections: "3".to_string(),
        tablets: "false".to_string(),
    }
}

pub fn some_params(count: usize) -> Vec<InsertParams> {
    (0..count).map(a_param).collect()
}

pub fn a_param(n: usize) -> InsertParams {
    InsertParams {
        article_id: Uuid::new_v5(
            &Uuid::NAMESPACE_URL,
            format!("wikipedia-page:{n}").as_bytes(),
        ),
        page_id: n as i64,
        title: format!("title {n}"),
        body: format!("text {n}"),
    }
}

pub fn a_source(count: usize) -> impl Iterator<Item = Result<InsertParams>> + Send + 'static {
    some_params(count).into_iter().map(Ok)
}

/// A source that yields `count` documents and then fails, the way a truncated
/// JSONL line does part way through a level.
pub fn a_truncated_source(count: usize) -> impl Iterator<Item = Result<InsertParams>> + Send {
    a_source(count).chain(std::iter::once(Err(anyhow!("truncated JSONL line 4242"))))
}

#[derive(Debug, Default)]
struct Seen {
    sent: usize,
    in_flight: usize,
    max_in_flight: usize,
    params: Vec<InsertParams>,
}

pub struct FakeInserter {
    latency: Duration,
    failing_positions: HashSet<usize>,
    seen: Mutex<Seen>,
    completed: AtomicUsize,
}

impl FakeInserter {
    pub fn new() -> Self {
        Self::with_latency(Duration::ZERO)
    }

    pub fn with_latency(latency: Duration) -> Self {
        Self {
            latency,
            failing_positions: HashSet::new(),
            seen: Mutex::new(Seen::default()),
            completed: AtomicUsize::new(0),
        }
    }

    /// Positions are 1-based, matching the order requests were handed to the
    /// inserter rather than the order they complete.
    pub fn failing_at(mut self, positions: &[usize]) -> Self {
        self.failing_positions = positions.iter().copied().collect();
        self
    }

    pub fn sent(&self) -> usize {
        self.seen.lock().unwrap().sent
    }

    pub fn max_in_flight(&self) -> usize {
        self.seen.lock().unwrap().max_in_flight
    }

    pub fn params_seen(&self) -> Vec<InsertParams> {
        self.seen.lock().unwrap().params.clone()
    }

    pub fn completed(&self) -> usize {
        self.completed.load(Ordering::SeqCst)
    }

    fn depart(&self, params: InsertParams) -> usize {
        let mut seen = self.seen.lock().unwrap();
        seen.sent += 1;
        seen.in_flight += 1;
        seen.max_in_flight = seen.max_in_flight.max(seen.in_flight);
        seen.params.push(params);
        seen.sent
    }

    fn arrive(&self) {
        self.seen.lock().unwrap().in_flight -= 1;
        self.completed.fetch_add(1, Ordering::SeqCst);
    }
}

impl Default for FakeInserter {
    fn default() -> Self {
        Self::new()
    }
}

impl Inserter for FakeInserter {
    // The explicit `impl Future + Send` is the point: `async fn` in a trait
    // leaves the future's `Send`ness up to the caller, and these futures are
    // spawned onto tokio, which requires it.
    #[allow(clippy::manual_async_fn)]
    fn insert(&self, params: InsertParams) -> impl Future<Output = Result<()>> + Send {
        async move {
            let position = self.depart(params);
            tokio::time::sleep(self.latency).await;
            self.arrive();
            if self.failing_positions.contains(&position) {
                return Err(anyhow!("wire is busy"));
            }
            Ok(())
        }
    }
}

/// Collects what a sweep said, so the progress and warning lines can be read
/// back instead of being asserted about stderr.
#[derive(Clone, Default)]
pub struct SpokenNotes(Arc<Mutex<Vec<String>>>);

impl SpokenNotes {
    pub fn lines(&self) -> Vec<String> {
        self.0.lock().unwrap().clone()
    }

    pub fn mentions(&self, needle: &str) -> bool {
        self.lines().iter().any(|line| line.contains(needle))
    }

    pub fn notes(&self, progress_interval: Duration) -> Notes {
        let sink = self.clone();
        Notes::new(
            progress_interval,
            Arc::new(move |message: &str| sink.0.lock().unwrap().push(message.to_string())),
        )
    }
}

pub fn quiet_notes() -> Notes {
    SpokenNotes::default().notes(Duration::from_secs(3600))
}
