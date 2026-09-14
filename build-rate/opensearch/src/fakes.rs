//! Client-shaped stand-ins: enough of an inserter, a corpus and a cluster to
//! drive the channel, the workers and the CSV without an OpenSearch node.
//!
//! Completions are deferred with a real sleep rather than returning ready, so
//! bulks genuinely overlap and `max_in_flight` measures the real in-flight
//! bound.
use std::sync::{Arc, Mutex};

use crate::client::Cluster;
use crate::corpus::{BulkDoc, DocumentBatch};
pub use build_rate_core::test_support::{quiet_notes, SpokenNotes};

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
    searchable: u64,
    publish_after: usize,
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

    /// Polls the new index takes to answer: `_count` and `_stats` say 503 for
    /// this many.
    pub fn answers_after(&self, polls: usize) -> &Self {
        self.state.lock().unwrap().create_lag = polls;
        self
    }

    /// Polls before what the index accepted becomes searchable, the way an
    /// OpenSearch `refresh_interval` does. Until then `docs.count` is behind
    /// `indexing.index_total` — the state that separates an index which has
    /// stalled from one which has simply not refreshed.
    pub fn publishes_after(&self, polls: usize) -> &Self {
        self.state.lock().unwrap().publish_after = polls;
        self
    }

    pub fn absent(&self) -> &Self {
        self.state.lock().unwrap().present = false;
        self
    }

    pub fn holding(&self, docs: u64) -> &Self {
        let mut model = self.state.lock().unwrap();
        model.docs = docs;
        model.searchable = docs;
        drop(model);
        self
    }

    /// Documents the index has taken in but not yet published. With
    /// `publishes_after` this is the state a build-rate watch has to read
    /// correctly: `indexing.index_total` ahead of `docs.count`.
    pub fn accepted(&self, docs: u64) -> &Self {
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
        ("GET", path) if path.ends_with("/_stats") => index_stats(&mut model),
        // Real OpenSearch answers `_refresh` on either verb, and the client
        // sends GET.
        (_, path) if path.ends_with("/_refresh") => refresh_index(&mut model),
        ("POST", path) if path.ends_with("/_analyze") => analyze_text(&model),
        _ => (
            404,
            r#"{"error":"the fake index does not answer this route"}"#.to_string(),
        ),
    }
}

fn head_index(model: &mut IndexModel) -> (u16, String) {
    if still_present(model) {
        (200, String::new())
    } else {
        (404, String::new())
    }
}

/// Whether the index is there *as of this poll*, ticking the delete lag on the
/// way past.
///
/// Every route that answers for an index consults this, not just `HEAD`: the
/// reset gate polls whichever endpoint the probe uses, and a lag that only
/// counted down on one of them would make the delete land or not depending on
/// which endpoint the harness happened to ask.
fn still_present(model: &mut IndexModel) -> bool {
    if model.delete_lag > 0 && !model.delete_is_a_lie {
        model.delete_lag -= 1;
        if model.delete_lag == 0 {
            model.present = false;
        }
        return true;
    }
    model.present
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
    if !still_present(model) {
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

/// The same lifecycle `_count` answers, plus the second counter. `docs.count`
/// is what a search would find and `indexing.index_total` is what the index has
/// accepted; `publish_after` holds the first apart from the second for a number
/// of polls, which is what an OpenSearch `refresh_interval` does and what a
/// build-rate watch has to be able to measure.
fn index_stats(model: &mut IndexModel) -> (u16, String) {
    if !still_present(model) {
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
    if model.publish_after > 0 {
        model.publish_after -= 1;
    } else {
        model.searchable = model.docs;
    }
    (
        200,
        format!(
            r#"{{"_all":{{"total":{{"docs":{{"count":{}}},"indexing":{{"index_total":{}}}}}}}}}"#,
            model.searchable, model.docs
        ),
    )
}

fn refresh_index(model: &mut IndexModel) -> (u16, String) {
    model.searchable = model.docs;
    model.publish_after = 0;
    (
        200,
        r#"{"_shards":{"total":1,"successful":1,"failed":0}}"#.to_string(),
    )
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
