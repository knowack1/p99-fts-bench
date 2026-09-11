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
use crate::sweep::{Inserter, Ladder, NothingToPrepare, Shape};

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

/// An OpenSearch-shaped endpoint that models one index: present or absent, a
/// document count, and how long each of those takes to become true.
///
/// The gates exist because a delete and a create are not instantaneous, so the
/// fake has to be able to be slow in exactly those two places — `delete_lag`
/// keeps `HEAD` answering present after the delete, `create_lag` keeps `_count`
/// answering 503 after the create. Every request is recorded in order, which is
/// how a test asserts the create waited rather than that it eventually
/// happened.
pub struct FakeIndex {
    base_url: String,
    state: Arc<Mutex<IndexModel>>,
    server: tokio::task::JoinHandle<()>,
}

#[derive(Default)]
pub struct IndexModel {
    present: bool,
    docs: u64,
    delete_lag: usize,
    create_lag: usize,
    delete_status: u16,
    create_status: u16,
    delete_is_a_lie: bool,
    tokens: String,
    analyze_status: u16,
    events: Vec<String>,
    last_config: Option<serde_json::Value>,
}

impl IndexModel {
    fn new() -> Self {
        Self {
            present: true,
            delete_status: 200,
            create_status: 200,
            analyze_status: 200,
            tokens: crate::reset::ANALYZER_PROBE_TOKENS.to_string(),
            ..Self::default()
        }
    }
}

impl FakeIndex {
    pub async fn start() -> Self {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base_url = format!("http://{}", listener.local_addr().unwrap());
        let state = Arc::new(Mutex::new(IndexModel::new()));
        let server = tokio::spawn(serve_index(listener, Arc::clone(&state)));
        Self {
            base_url,
            state,
            server,
        }
    }

    pub fn url(&self) -> &str {
        &self.base_url
    }

    /// Polls the delete takes to land: `HEAD` keeps saying present for this
    /// many before the index disappears.
    pub fn delete_lands_after(&self, polls: usize) -> &Self {
        self.state.lock().unwrap().delete_lag = polls;
        self
    }

    /// Polls the new index takes to answer: `_count` says 503 for this many.
    pub fn answers_after(&self, polls: usize) -> &Self {
        self.state.lock().unwrap().create_lag = polls;
        self
    }

    pub fn absent(&self) -> &Self {
        self.state.lock().unwrap().present = false;
        self
    }

    pub fn holding(&self, docs: u64) -> &Self {
        self.state.lock().unwrap().docs = docs;
        self
    }

    /// The delete is acknowledged and nothing happens — the failure the gates
    /// exist to catch, because everything else about the run still looks fine.
    pub fn lying_about_deletes(&self) -> &Self {
        self.state.lock().unwrap().delete_is_a_lie = true;
        self
    }

    pub fn refusing_deletes(&self, status: u16) -> &Self {
        self.state.lock().unwrap().delete_status = status;
        self
    }

    pub fn refusing_creates(&self, status: u16) -> &Self {
        self.state.lock().unwrap().create_status = status;
        self
    }

    pub fn analyzing_as(&self, tokens: &str) -> &Self {
        self.state.lock().unwrap().tokens = tokens.to_string();
        self
    }

    pub fn refusing_analyze(&self, status: u16) -> &Self {
        self.state.lock().unwrap().analyze_status = status;
        self
    }

    pub fn events(&self) -> Vec<String> {
        self.state.lock().unwrap().events.clone()
    }

    pub fn times(&self, event: &str) -> usize {
        self.events().iter().filter(|seen| *seen == event).count()
    }

    pub fn last_config(&self) -> Option<serde_json::Value> {
        self.state.lock().unwrap().last_config.clone()
    }
}

impl Drop for FakeIndex {
    fn drop(&mut self) {
        self.server.abort();
    }
}

struct FakeRequest {
    method: String,
    path: String,
    body: Vec<u8>,
}

impl FakeRequest {
    fn event(&self) -> String {
        format!(
            "{} {}",
            self.method,
            self.path.split('?').next().unwrap_or("")
        )
    }
}

async fn serve_index(listener: tokio::net::TcpListener, state: Arc<Mutex<IndexModel>>) {
    while let Ok((stream, _)) = listener.accept().await {
        tokio::spawn(one_connection(stream, Arc::clone(&state)));
    }
}

/// Keep-alive is honoured rather than answered with `Connection: close`: the
/// client under test keeps an idle pool per host, and a server that hung up
/// after every reply would have it racing a closed socket on the next poll.
async fn one_connection(mut stream: tokio::net::TcpStream, state: Arc<Mutex<IndexModel>>) {
    let mut buffered = Vec::new();
    while let Some(request) = read_request(&mut stream, &mut buffered).await {
        let (code, body) = answer(&request, &state);
        let body = if request.method == "HEAD" {
            String::new()
        } else {
            body
        };
        if write_reply(&mut stream, code, &body).await.is_err() {
            return;
        }
    }
}

async fn write_reply(
    stream: &mut tokio::net::TcpStream,
    code: u16,
    body: &str,
) -> std::io::Result<()> {
    use tokio::io::AsyncWriteExt;

    let head = format!(
        "HTTP/1.1 {code} X\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n",
        body.len()
    );
    stream.write_all(head.as_bytes()).await?;
    stream.write_all(body.as_bytes()).await
}

async fn read_request(
    stream: &mut tokio::net::TcpStream,
    buffered: &mut Vec<u8>,
) -> Option<FakeRequest> {
    let head_end = read_until_head(stream, buffered).await?;
    let head = String::from_utf8_lossy(&buffered[..head_end]).to_string();
    let length = content_length(&head);
    let total = head_end + 4 + length;
    while buffered.len() < total {
        if read_more(stream, buffered).await == 0 {
            return None;
        }
    }
    let body = buffered[head_end + 4..total].to_vec();
    buffered.drain(..total);
    let mut words = head.split_whitespace();
    Some(FakeRequest {
        method: words.next()?.to_string(),
        path: words.next()?.to_string(),
        body,
    })
}

async fn read_until_head(
    stream: &mut tokio::net::TcpStream,
    buffered: &mut Vec<u8>,
) -> Option<usize> {
    loop {
        if let Some(at) = find_blank_line(buffered) {
            return Some(at);
        }
        if read_more(stream, buffered).await == 0 {
            return None;
        }
    }
}

async fn read_more(stream: &mut tokio::net::TcpStream, buffered: &mut Vec<u8>) -> usize {
    use tokio::io::AsyncReadExt;

    let mut chunk = [0_u8; 4096];
    let read = stream.read(&mut chunk).await.unwrap_or(0);
    buffered.extend_from_slice(&chunk[..read]);
    read
}

fn find_blank_line(buffered: &[u8]) -> Option<usize> {
    buffered.windows(4).position(|window| window == b"\r\n\r\n")
}

fn content_length(head: &str) -> usize {
    head.lines()
        .find_map(|line| {
            line.to_lowercase()
                .strip_prefix("content-length:")
                .map(str::trim)
                .map(str::to_string)
        })
        .and_then(|value| value.parse().ok())
        .unwrap_or(0)
}

fn answer(request: &FakeRequest, state: &Arc<Mutex<IndexModel>>) -> (u16, String) {
    let mut model = state.lock().unwrap();
    model.events.push(request.event());
    let path = request.path.split('?').next().unwrap_or("");
    match (request.method.as_str(), path) {
        ("GET", "/") => (
            200,
            r#"{"version":{"number":"2.19.0","distribution":"opensearch"}}"#.to_string(),
        ),
        ("HEAD", _) => head_index(&mut model),
        ("DELETE", _) => delete_index(&mut model),
        ("PUT", _) => create_index(&mut model, &request.body),
        ("GET", path) if path.ends_with("/_count") => count_documents(&mut model),
        ("POST", path) if path.ends_with("/_analyze") => analyze_text(&model),
        _ => (
            404,
            r#"{"error":"the fake index does not answer this route"}"#.to_string(),
        ),
    }
}

fn head_index(model: &mut IndexModel) -> (u16, String) {
    if model.delete_lag > 0 && !model.delete_is_a_lie {
        model.delete_lag -= 1;
        if model.delete_lag == 0 {
            model.present = false;
        }
        return (200, String::new());
    }
    if model.present {
        (200, String::new())
    } else {
        (404, String::new())
    }
}

fn delete_index(model: &mut IndexModel) -> (u16, String) {
    if model.delete_status != 200 {
        return (model.delete_status, r#"{"error":"refused"}"#.to_string());
    }
    if !model.present {
        return (
            404,
            r#"{"error":{"type":"index_not_found_exception"}}"#.to_string(),
        );
    }
    if model.delete_is_a_lie {
        return (200, r#"{"acknowledged":true}"#.to_string());
    }
    if model.delete_lag == 0 {
        model.present = false;
    }
    model.docs = 0;
    (200, r#"{"acknowledged":true}"#.to_string())
}

fn create_index(model: &mut IndexModel, body: &[u8]) -> (u16, String) {
    if model.create_status != 200 {
        return (
            model.create_status,
            r#"{"error":{"type":"cluster_block_exception"}}"#.to_string(),
        );
    }
    model.last_config = serde_json::from_slice(body).ok();
    model.present = true;
    model.docs = 0;
    (200, r#"{"acknowledged":true}"#.to_string())
}

fn count_documents(model: &mut IndexModel) -> (u16, String) {
    if !model.present {
        return (
            404,
            r#"{"error":{"type":"index_not_found_exception"}}"#.to_string(),
        );
    }
    if model.create_lag > 0 {
        model.create_lag -= 1;
        return (
            503,
            r#"{"error":{"type":"no_shard_available_action_exception"}}"#.to_string(),
        );
    }
    (200, format!(r#"{{"count":{}}}"#, model.docs))
}

fn analyze_text(model: &IndexModel) -> (u16, String) {
    if model.analyze_status != 200 {
        return (
            model.analyze_status,
            r#"{"error":"no such analyzer"}"#.to_string(),
        );
    }
    let tokens: Vec<serde_json::Value> = model
        .tokens
        .split_whitespace()
        .filter_map(|pair| pair.split_once(':'))
        .map(|(position, token)| {
            serde_json::json!({"token": token, "position": position.parse::<u64>().unwrap_or(0)})
        })
        .collect();
    (200, serde_json::json!({"tokens": tokens}).to_string())
}

/// The ladder a test that is not about the reset runs: one inserter, nothing
/// done to the index first.
pub static NOTHING_TO_PREPARE: NothingToPrepare = NothingToPrepare;

pub fn a_ladder<'a, I: Inserter>(
    inserter: Arc<I>,
    levels: &'a [usize],
    shape: Shape,
) -> Ladder<'a, I> {
    Ladder {
        inserter,
        before_level: &NOTHING_TO_PREPARE,
        levels,
        shape,
    }
}

/// Counts the levels it was asked to prepare, and can refuse the nth — a reset
/// that fails must take its level with it rather than being loaded past.
#[derive(Default)]
pub struct CountingPreparation {
    prepared: AtomicUsize,
    failing_at: Option<usize>,
}

impl CountingPreparation {
    pub fn failing_at(position: usize) -> Self {
        Self {
            prepared: AtomicUsize::new(0),
            failing_at: Some(position),
        }
    }

    pub fn prepared(&self) -> usize {
        self.prepared.load(Ordering::SeqCst)
    }
}

impl crate::sweep::BeforeLevel for CountingPreparation {
    fn prepare(&self) -> crate::sweep::BoxFuture<'_, Result<()>> {
        let position = self.prepared.fetch_add(1, Ordering::SeqCst) + 1;
        Box::pin(async move {
            if self.failing_at == Some(position) {
                anyhow::bail!("the index would not empty");
            }
            Ok(())
        })
    }
}
