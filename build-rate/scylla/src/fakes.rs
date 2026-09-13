//! Doubles for the parts of this half that are its own.
//!
//! The generic ones — notes a test can read back, an inserter that records what
//! it was offered — are `build_rate_core::test_support`'s, because both
//! harnesses need the same ones and the properties they prove belong to the
//! sweep rather than to a payload.
use std::sync::{Arc, Mutex};

pub use build_rate_core::test_support::quiet_notes;

use crate::session::Topology;

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

