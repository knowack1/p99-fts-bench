//! Client-shaped stand-ins: enough of an inserter, a corpus and a cluster to
//! drive the channel, the workers and the CSV without an OpenSearch node.
//!
//! Completions are deferred with a real sleep rather than returning ready, so
//! bulks genuinely overlap and `max_in_flight` measures the real in-flight
//! bound.
use std::collections::HashMap;
use std::future::Future;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use anyhow::{anyhow, Result};

use crate::bulk::BulkOutcome;
use crate::client::Cluster;
use crate::corpus::{BulkDoc, DocumentBatch};
use crate::notes::Notes;
use crate::report::PointResult;
use crate::sweep::{Inserter, Shape};

pub fn a_shape(batch_size: usize) -> Shape {
    Shape {
        batch_size,
        queue_depth: 2,
    }
}

pub fn a_point(concurrency: usize) -> PointResult {
    a_point_with_latency(concurrency, Some(1.5), Some(9.0))
}

pub fn a_point_with_errors(concurrency: usize, errors: u64) -> PointResult {
    PointResult {
        errors,
        failed_bulks: 1,
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
        batch_size: 10,
        docs: 100,
        errors: 0,
        bulks: 10,
        failed_bulks: 0,
        wall_s: 2.0,
        docs_per_s: 50.0,
        p50_ms,
        p99_ms,
    }
}

pub fn a_cluster() -> Cluster {
    Cluster {
        opensearch_version: "2.19.0".to_string(),
        distribution: "opensearch".to_string(),
        client_version: "2.4.0".to_string(),
        http_client_version: "0.13.5".to_string(),
        runtime: "tokio multi_thread workers:8".to_string(),
        index: "wiki-articles".to_string(),
        index_shards: "1".to_string(),
        replicas: "0".to_string(),
        refresh_interval: "1s".to_string(),
        source_enabled: "true".to_string(),
        body_analyzer: "m1_parity".to_string(),
        write_pool: "node-0=write:8".to_string(),
        connection_pool: "reqwest-default(idle unbounded)".to_string(),
    }
}

pub fn some_documents(count: usize) -> Vec<BulkDoc> {
    (0..count).map(a_document).collect()
}

pub fn a_document(n: usize) -> BulkDoc {
    BulkDoc {
        page_id: n as i64,
        title: format!("title {n}"),
        body: format!("text {n}"),
    }
}

pub fn a_batch(count: usize) -> DocumentBatch {
    DocumentBatch::new(some_documents(count))
}

/// `count` documents grouped into batches of `batch_size`, the way
/// `CorpusSource::open` hands them over.
pub fn a_source(
    count: usize,
    batch_size: usize,
) -> impl Iterator<Item = Result<DocumentBatch>> + Send + 'static {
    let documents = some_documents(count);
    documents
        .chunks(batch_size)
        .map(|chunk| Ok(DocumentBatch::new(chunk.to_vec())))
        .collect::<Vec<_>>()
        .into_iter()
}

/// A source that yields whole batches and then fails, the way a truncated
/// JSONL line does part way through a level.
pub fn a_truncated_source(
    count: usize,
    batch_size: usize,
) -> impl Iterator<Item = Result<DocumentBatch>> + Send {
    a_source(count, batch_size).chain(std::iter::once(Err(anyhow!("truncated JSONL line 4242"))))
}

#[derive(Debug, Default)]
struct Seen {
    bulks: usize,
    docs: usize,
    in_flight: usize,
    max_in_flight: usize,
    max_docs_in_flight: usize,
    docs_in_flight: usize,
    batch_sizes: Vec<usize>,
    documents: Vec<BulkDoc>,
}

/// Positions are 1-based over the order batches were handed to the inserter.
/// `failing_positions` maps a position to how many of its documents the engine
/// rejected; `usize::MAX` means the request itself never came back.
pub struct FakeInserter {
    latency: Duration,
    failing_positions: HashMap<usize, usize>,
    seen: Mutex<Seen>,
    completed: AtomicUsize,
}

pub const WHOLE_BULK: usize = usize::MAX;

impl FakeInserter {
    pub fn new() -> Self {
        Self::with_latency(Duration::ZERO)
    }

    pub fn with_latency(latency: Duration) -> Self {
        Self {
            latency,
            failing_positions: HashMap::new(),
            seen: Mutex::new(Seen::default()),
            completed: AtomicUsize::new(0),
        }
    }

    /// The whole request fails at each of these positions.
    pub fn failing_at(self, positions: &[usize]) -> Self {
        self.rejecting(&positions.iter().map(|at| (*at, WHOLE_BULK)).collect::<Vec<_>>())
    }

    /// `(position, documents)` — the request answers 200 and rejects that many
    /// of its items, the way OpenSearch reports a partial bulk.
    pub fn rejecting(mut self, failures: &[(usize, usize)]) -> Self {
        self.failing_positions = failures.iter().copied().collect();
        self
    }

    pub fn bulks(&self) -> usize {
        self.seen.lock().unwrap().bulks
    }

    pub fn docs(&self) -> usize {
        self.seen.lock().unwrap().docs
    }

    pub fn max_in_flight(&self) -> usize {
        self.seen.lock().unwrap().max_in_flight
    }

    pub fn max_docs_in_flight(&self) -> usize {
        self.seen.lock().unwrap().max_docs_in_flight
    }

    pub fn batch_sizes(&self) -> Vec<usize> {
        self.seen.lock().unwrap().batch_sizes.clone()
    }

    pub fn documents_seen(&self) -> Vec<BulkDoc> {
        self.seen.lock().unwrap().documents.clone()
    }

    pub fn completed(&self) -> usize {
        self.completed.load(Ordering::SeqCst)
    }

    fn depart(&self, batch: DocumentBatch) -> usize {
        let mut seen = self.seen.lock().unwrap();
        seen.bulks += 1;
        seen.docs += batch.documents().len();
        seen.in_flight += 1;
        seen.docs_in_flight += batch.documents().len();
        seen.max_in_flight = seen.max_in_flight.max(seen.in_flight);
        seen.max_docs_in_flight = seen.max_docs_in_flight.max(seen.docs_in_flight);
        seen.batch_sizes.push(batch.documents().len());
        seen.documents.extend(batch.documents().iter().cloned());
        seen.bulks
    }

    fn arrive(&self, docs: usize) {
        let mut seen = self.seen.lock().unwrap();
        seen.in_flight -= 1;
        seen.docs_in_flight -= docs;
        drop(seen);
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
    fn insert(&self, batch: DocumentBatch) -> impl Future<Output = Result<BulkOutcome>> + Send {
        async move {
            let docs = batch.documents().len();
            let position = self.depart(batch);
            tokio::time::sleep(self.latency).await;
            self.arrive(docs);
            match self.failing_positions.get(&position) {
                None => Ok(BulkOutcome::default()),
                Some(&WHOLE_BULK) => Err(anyhow!("the socket went away")),
                Some(&rejected) => Ok(BulkOutcome {
                    failed: rejected as u64,
                    first_failure: Some("status 429 too many requests".to_string()),
                }),
            }
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
