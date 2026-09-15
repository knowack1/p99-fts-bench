//! Doubles the binary crates and this crate test against, so the loop, the
//! matrix and the bootstrap decision are all testable with nothing running.
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use anyhow::{bail, Result};

use crate::bootstrap::{IndexLoader, LoadReport};
use crate::queries::{QueryClass, QuerySet};
use crate::search::{BoxFuture, Found, Searcher, CQL};

/// A searcher that answers after a fixed delay, remembers what it was asked,
/// and can be told to fail.
pub struct FakeSearcher {
    latency: Duration,
    hits: usize,
    fail_every: usize,
    asked: AtomicU64,
    in_flight: AtomicUsize,
    peak_in_flight: AtomicUsize,
    seen: Mutex<Vec<String>>,
}

impl Default for FakeSearcher {
    fn default() -> Self {
        Self::new()
    }
}

impl FakeSearcher {
    pub fn new() -> Self {
        Self {
            latency: Duration::from_millis(1),
            hits: 3,
            fail_every: 0,
            asked: AtomicU64::new(0),
            in_flight: AtomicUsize::new(0),
            peak_in_flight: AtomicUsize::new(0),
            seen: Mutex::new(Vec::new()),
        }
    }

    pub fn with_latency(latency: Duration) -> Self {
        Self {
            latency,
            ..Self::new()
        }
    }

    pub fn finding(mut self, hits: usize) -> Self {
        self.hits = hits;
        self
    }

    /// Every nth request comes back as an error, counting from the first.
    pub fn failing_every(mut self, nth: usize) -> Self {
        self.fail_every = nth;
        self
    }

    pub fn asked(&self) -> u64 {
        self.asked.load(Ordering::SeqCst)
    }

    pub fn peak_in_flight(&self) -> usize {
        self.peak_in_flight.load(Ordering::SeqCst)
    }

    pub fn queries_seen(&self) -> Vec<String> {
        self.seen.lock().unwrap().clone()
    }

    fn enter(&self, query: &str) -> u64 {
        self.seen.lock().unwrap().push(query.to_string());
        let in_flight = self.in_flight.fetch_add(1, Ordering::SeqCst) + 1;
        self.peak_in_flight.fetch_max(in_flight, Ordering::SeqCst);
        self.asked.fetch_add(1, Ordering::SeqCst) + 1
    }

    fn leave(&self, nth: u64) -> Result<Found> {
        self.in_flight.fetch_sub(1, Ordering::SeqCst);
        if self.fail_every > 0 && nth.is_multiple_of(self.fail_every as u64) {
            bail!("the fake searcher was told to fail request {nth}");
        }
        Ok(Found::new(self.hits))
    }
}

impl Searcher for FakeSearcher {
    fn search<'a>(&'a self, query: &'a str) -> BoxFuture<'a, Result<Found>> {
        Box::pin(async move {
            let nth = self.enter(query);
            tokio::time::sleep(self.latency).await;
            self.leave(nth)
        })
    }

    fn interface(&self) -> &'static str {
        CQL
    }

    fn endpoint(&self) -> &str {
        "fake://searcher"
    }
}

/// A loader that reports a load without doing one, and counts how often it was
/// asked.
pub struct FakeLoader {
    report: LoadReport,
    builds: AtomicUsize,
}

impl Default for FakeLoader {
    fn default() -> Self {
        Self::loading(100)
    }
}

impl FakeLoader {
    pub fn loading(docs: u64) -> Self {
        Self {
            report: LoadReport {
                docs,
                errors: 0,
                wall_s: 1.0,
                docs_per_s: docs as f64,
            },
            builds: AtomicUsize::new(0),
        }
    }

    pub fn rejecting(mut self, errors: u64) -> Self {
        self.report.errors = errors;
        self
    }

    pub fn builds(&self) -> usize {
        self.builds.load(Ordering::SeqCst)
    }
}

impl IndexLoader for FakeLoader {
    fn build(&self) -> BoxFuture<'_, Result<LoadReport>> {
        self.builds.fetch_add(1, Ordering::SeqCst);
        Box::pin(std::future::ready(Ok(self.report.clone())))
    }

    fn describe(&self) -> String {
        "fake loader".to_string()
    }
}

/// The smallest query set that is still a query set: one class, named queries.
pub fn a_query_set(class: &str, queries: &[&str]) -> QuerySet {
    let listed: Vec<String> = queries.iter().map(|text| format!("{text:?}")).collect();
    QuerySet::parse(
        &format!("{{\"classes\": {{{class:?}: [{}]}}}}", listed.join(",")),
        "test",
    )
    .expect("the test's own query set has to parse")
}

pub fn a_class(name: &str, queries: &[&str]) -> QueryClass {
    a_query_set(name, queries)
        .select(&[])
        .expect("a class built here is never empty")
        .remove(0)
}

pub fn a_searcher(searcher: FakeSearcher) -> Arc<dyn Searcher> {
    Arc::new(searcher)
}

/// A local HTTP endpoint an engine client can be pointed at.
///
/// Both binaries reach their engine over HTTP for at least one interface, and
/// what is worth pinning there is the payload — the path, the body, and how a
/// hit count is read back out of the answer. Those are properties of the
/// protocol rather than of the network, so they are tested against a socket
/// that answers in milliseconds instead of against a running engine.
pub mod stub {
    use std::net::SocketAddr;

    use std::sync::{Arc, Mutex};

    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::{TcpListener, TcpStream};

    /// What one request looked like when it arrived.
    #[derive(Debug, Clone, PartialEq)]
    pub struct Seen {
        pub path: String,
        pub body: String,
    }

    pub struct StubEndpoint {
        address: SocketAddr,
        seen: Arc<Mutex<Vec<Seen>>>,
    }

    impl StubEndpoint {
        /// Answers every request with the same body, and remembers what it was
        /// asked. One request per connection, closed afterwards, because what is
        /// being tested is the payload rather than keep-alive.
        pub async fn answering(status: u16, body: &'static str) -> Self {
            let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
            let address = listener.local_addr().unwrap();
            let seen = Arc::new(Mutex::new(Vec::new()));
            let record = Arc::clone(&seen);
            tokio::spawn(async move {
                while let Ok((stream, _)) = listener.accept().await {
                    serve_one(stream, status, body, &record).await;
                }
            });
            Self { address, seen }
        }

        pub fn url(&self) -> String {
            format!("http://{}", self.address)
        }

        pub fn seen(&self) -> Vec<Seen> {
            self.seen.lock().unwrap().clone()
        }
    }

    async fn serve_one(mut stream: TcpStream, status: u16, body: &str, seen: &Mutex<Vec<Seen>>) {
        let Some(request) = read_request(&mut stream).await else {
            return;
        };
        seen.lock().unwrap().push(request);
        let _ = stream.write_all(response(status, body).as_bytes()).await;
        let _ = stream.flush().await;
    }

    async fn read_request(stream: &mut TcpStream) -> Option<Seen> {
        let mut raw = Vec::new();
        let mut chunk = [0_u8; 1024];
        loop {
            let read = stream.read(&mut chunk).await.ok()?;
            if read == 0 {
                break;
            }
            raw.extend_from_slice(&chunk[..read]);
            if is_complete(&raw) {
                break;
            }
        }
        parse(&String::from_utf8_lossy(&raw))
    }

    /// Every request this stub sees is a POST with a Content-Length, so the request
    /// is complete once the body has reached that length.
    fn is_complete(raw: &[u8]) -> bool {
        let text = String::from_utf8_lossy(raw);
        let Some((head, body)) = text.split_once("\r\n\r\n") else {
            return false;
        };
        content_length(head).is_some_and(|wanted| body.len() >= wanted)
    }

    fn content_length(head: &str) -> Option<usize> {
        head.lines()
            .find(|line| line.to_ascii_lowercase().starts_with("content-length:"))
            .and_then(|line| line.split_once(':'))
            .and_then(|(_, value)| value.trim().parse().ok())
    }

    fn parse(text: &str) -> Option<Seen> {
        let (head, body) = text.split_once("\r\n\r\n")?;
        let path = head.lines().next()?.split_whitespace().nth(1)?.to_string();
        Some(Seen {
            path,
            body: body.to_string(),
        })
    }

    fn response(status: u16, body: &str) -> String {
        format!(
            "HTTP/1.1 {status} X\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        )
    }
}
