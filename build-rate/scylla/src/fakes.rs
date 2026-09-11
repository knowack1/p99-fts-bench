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

use crate::build_rate::IndexBuild;
use crate::corpus::InsertParams;
use crate::notes::Notes;
use crate::report::PointResult;
use crate::samples::Submitted;
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
        index: None,
    }
}

pub fn a_point_with_index(concurrency: usize, build: IndexBuild) -> PointResult {
    PointResult {
        index: Some(build),
        ..a_point(concurrency)
    }
}

pub fn an_index_build(docs: u64, settled: bool) -> IndexBuild {
    IndexBuild {
        docs,
        docs_per_s: 1234.5,
        lag_docs: if settled { 0 } else { 42 },
        settle_s: 3.5,
        settled,
        status: "SERVING".to_string(),
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

/// For the tests that measure a level without watching the series it feeds: the
/// workers still count what they submitted, nobody reads it.
pub fn no_counter() -> Arc<Submitted> {
    Arc::new(Submitted::default())
}

/// What one poll of the fake vector-store finds.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Reply {
    Absent,
    Serving(u64),
    Building(u64),
    Failing(u16),
}

impl Reply {
    fn parts(&self) -> (u16, String) {
        match self {
            Self::Absent => (404, r#"{"error":"no such index"}"#.to_string()),
            Self::Serving(count) => (200, format!(r#"{{"count":{count},"status":"SERVING"}}"#)),
            Self::Building(count) => (200, format!(r#"{{"count":{count},"status":"BUILDING"}}"#)),
            Self::Failing(code) => (*code, r#"{"error":"unavailable"}"#.to_string()),
        }
    }
}

#[derive(Debug)]
struct Script {
    queued: std::collections::VecDeque<Reply>,
    standing: Reply,
}

impl Script {
    /// Queued replies are consumed one per poll; the last one then stands. A
    /// gate that polls until a condition holds has to be able to see a
    /// sequence, not just an end state.
    fn next(&mut self) -> Reply {
        match self.queued.pop_front() {
            Some(reply) => {
                self.standing = reply.clone();
                reply
            }
            None => self.standing.clone(),
        }
    }
}

/// A vector-store-shaped endpoint whose answers a test writes.
pub struct FakeVectorStore {
    base_url: String,
    script: Arc<Mutex<Script>>,
    server: tokio::task::JoinHandle<()>,
}

impl FakeVectorStore {
    pub async fn start(standing: Reply) -> Self {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base_url = format!("http://{}", listener.local_addr().unwrap());
        let script = Arc::new(Mutex::new(Script {
            queued: std::collections::VecDeque::new(),
            standing,
        }));
        let server = tokio::spawn(serve_index_status(listener, Arc::clone(&script)));
        Self {
            base_url,
            script,
            server,
        }
    }

    pub fn url(&self) -> &str {
        &self.base_url
    }

    pub fn standing(&self, reply: Reply) {
        let mut script = self.script.lock().unwrap();
        script.queued.clear();
        script.standing = reply;
    }

    pub fn then(&self, replies: &[Reply]) {
        self.script
            .lock()
            .unwrap()
            .queued
            .extend(replies.iter().cloned());
    }
}

impl Drop for FakeVectorStore {
    fn drop(&mut self) {
        self.server.abort();
    }
}

async fn serve_index_status(listener: tokio::net::TcpListener, script: Arc<Mutex<Script>>) {
    while let Ok((stream, _)) = listener.accept().await {
        answer_one(stream, &script).await;
    }
}

/// One request per connection, answered with `Connection: close`. Keep-alive
/// would buy nothing here: these polls are one a second at most.
async fn answer_one(mut stream: tokio::net::TcpStream, script: &Arc<Mutex<Script>>) {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    let mut head = [0_u8; 1024];
    let read = stream.read(&mut head).await.unwrap_or(0);
    if read == 0 {
        return;
    }
    let (code, body) = if String::from_utf8_lossy(&head[..read]).contains("/api/v1/info") {
        (200, r#"{"version":"1.10.0-fake"}"#.to_string())
    } else {
        script.lock().unwrap().next().parts()
    };
    let response = format!(
        "HTTP/1.1 {code} X\r\nContent-Type: application/json\r\n\
         Content-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    let _ = stream.write_all(response.as_bytes()).await;
    let _ = stream.shutdown().await;
}

/// The same inserter at every level, for tests that are not about the reset.
pub struct OneInserter<I>(pub Arc<I>);

impl<I: Inserter> OneInserter<I> {
    pub fn new(inserter: I) -> Self {
        Self(Arc::new(inserter))
    }
}

impl<I: Inserter> crate::sweep::InserterSource for OneInserter<I> {
    type Inserter = I;

    fn open(&self) -> crate::sweep::BoxFuture<'_, Result<Arc<I>>> {
        Box::pin(std::future::ready(Ok(Arc::clone(&self.0))))
    }
}
