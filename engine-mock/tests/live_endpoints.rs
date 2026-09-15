//! Both endpoints over real sockets, in the sequences the real harnesses send.
//!
//! Every other file in the suite proves a piece: the frame walk, the routing
//! table, the counters, the index lifecycle. This one proves the mock — that
//! the pieces are wired to a listening port, that a keep-alive connection
//! carries a whole `osrate` reset through them in order, and that documents
//! pushed in over CQL come back out as a count over HTTP. A run against a mock
//! that failed here would still complete and still produce a number that looked
//! like a client ceiling.

use std::time::Duration;

use clap::Parser;
use engine_mock::cli::Args;
use engine_mock::cql::CQL_VERSION;
use engine_mock::cql_wire as wire;
use engine_mock::http_wire::HEADER_TERMINATOR;
use engine_mock::index::SERVING;
use engine_mock::opensearch;
use engine_mock::run::{self, Mock};
use serde_json::{json, Value};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::time::timeout;

/// A read that never returns has to fail this test rather than hang the suite.
const PATIENCE: Duration = Duration::from_secs(10);
const WORKERS: usize = 4;
const INDEX_PATH: &str = "/wiki-articles";
const COUNT_PATH: &str = "/wiki-articles/_count";
const STATS_PATH: &str = "/wiki-articles/_stats";
const BULK_PATH: &str = "/wiki-articles/_bulk";
const STATUS_PATH: &str = "/api/v1/indexes/wiki/articles_body_fts/status";
const AN_INSERT: &str = "INSERT INTO wiki.articles (article_id, page_id, title, body) \
                         VALUES (?, ?, ?, ?)";

fn a_free_port() -> u16 {
    std::net::TcpListener::bind("127.0.0.1:0")
        .expect("a free port")
        .local_addr()
        .expect("the bound address")
        .port()
}

async fn started(argv: &[&str]) -> Mock {
    let args = Args::try_parse_from(argv).expect("the mock's own flags");
    run::start(&args, WORKERS)
        .await
        .expect("the endpoints to bind")
}

const PORT_ATTEMPTS: usize = 20;

async fn an_opensearch_mock() -> Mock {
    started(&[
        "engine-mock",
        "--mode",
        "http",
        "--host",
        "127.0.0.1",
        "--port",
        "0",
    ])
    .await
}

/// The vector-store half is asked for by port, and 0 there means off rather
/// than ephemeral, so this one is picked before the mock binds it — which is a
/// race with anything else picking one the same way, and is retried rather than
/// asserted: a suite that fails on a port collision fails for a reason that has
/// nothing to do with the mock.
async fn a_scylla_mock() -> Mock {
    for _ in 0..PORT_ATTEMPTS {
        let vs_port = a_free_port().to_string();
        let args = Args::try_parse_from([
            "engine-mock",
            "--mode",
            "cql",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--vs-port",
            &vs_port,
        ])
        .expect("the mock's own flags");
        if let Ok(mock) = run::start(&args, WORKERS).await {
            return mock;
        }
    }
    panic!("no free port for the vector-store endpoint in {PORT_ATTEMPTS} attempts");
}

fn engine_port(mock: &Mock) -> u16 {
    mock.ports()[0]
}

fn vector_store_port(mock: &Mock) -> u16 {
    mock.ports()[1]
}

fn http_request(method: &str, path: &str, body: &[u8]) -> Vec<u8> {
    let mut out = format!(
        "{method} {path} HTTP/1.1\r\nHost: mock\r\nContent-Length: {}\r\n\r\n",
        body.len()
    )
    .into_bytes();
    out.extend_from_slice(body);
    out
}

fn bulk_body(page_ids: &[u64]) -> Vec<u8> {
    let mut lines = String::new();
    for page_id in page_ids {
        let action = json!({"index": {"_index": "wiki-articles", "_id": page_id.to_string()}});
        let source = json!({"page_id": page_id, "title": "t", "body": "b"});
        lines.push_str(&format!("{action}\n{source}\n"));
    }
    lines.into_bytes()
}

/// A REQUEST on the wire. `cql_wire::frame` stamps the response version, so
/// using it here would drive the version refusal instead of the frame walk.
fn request_frame(opcode: u8, stream: i16, body: &[u8]) -> Vec<u8> {
    let mut out = vec![wire::REQUEST_VERSION, 0];
    out.extend_from_slice(&stream.to_be_bytes());
    out.push(opcode);
    out.extend_from_slice(&(body.len() as u32).to_be_bytes());
    out.extend_from_slice(body);
    out
}

fn startup_body() -> Vec<u8> {
    let mut out = 1_u16.to_be_bytes().to_vec();
    wire::write_short_string(&mut out, "CQL_VERSION");
    wire::write_short_string(&mut out, CQL_VERSION);
    out
}

fn prepare_body(query: &str) -> Vec<u8> {
    let mut out = Vec::new();
    wire::write_long_string(&mut out, query);
    out
}

fn execute_body(statement: &[u8]) -> Vec<u8> {
    let mut out = Vec::new();
    wire::write_short_bytes(&mut out, statement);
    out.extend_from_slice(&1_u16.to_be_bytes());
    out.push(0);
    out
}

fn pipelined_executes(statement: &[u8], count: i16) -> Vec<u8> {
    (0..count)
        .flat_map(|stream| request_frame(wire::OPCODE_EXECUTE, stream, &execute_body(statement)))
        .collect()
}

fn result_kind(body: &[u8]) -> i32 {
    i32::from_be_bytes(body[..4].try_into().expect("a result kind"))
}

fn prepared_statement_id(body: &[u8]) -> Vec<u8> {
    let (id, _) = wire::read_short_bytes(body, 4).expect("the prepared statement id");
    id.to_vec()
}

fn find(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack
        .windows(needle.len())
        .position(|window| window == needle)
}

fn status_of(head: &str) -> u16 {
    head.split(' ')
        .nth(1)
        .and_then(|code| code.parse().ok())
        .expect("a status line")
}

fn content_length(head: &str) -> usize {
    head.split("\r\n")
        .skip(1)
        .filter_map(|line| line.split_once(':'))
        .find(|(name, _)| name.eq_ignore_ascii_case("content-length"))
        .and_then(|(_, value)| value.trim().parse().ok())
        .unwrap_or(0)
}

struct Reply {
    status: u16,
    body: Vec<u8>,
}

impl Reply {
    fn json(&self) -> Value {
        serde_json::from_slice(&self.body).expect("a JSON body")
    }
}

/// One CQL answer, read back by hand: `cql_wire` is the mock's spelling book
/// rather than a client, so the assertions decode the bytes themselves.
struct Answer {
    stream: i16,
    opcode: u8,
    body: Vec<u8>,
}

/// A socket the test drives by hand — the crate has no HTTP client and must not
/// grow one.
struct Wire {
    stream: TcpStream,
    read: Vec<u8>,
}

impl Wire {
    async fn to(port: u16) -> Self {
        let stream = TcpStream::connect(("127.0.0.1", port))
            .await
            .expect("a connection to the mock");
        Self {
            stream,
            read: Vec::new(),
        }
    }

    async fn send(&mut self, bytes: &[u8]) {
        self.stream
            .write_all(bytes)
            .await
            .expect("the request to go out");
    }

    async fn read_more(&mut self) {
        let mut chunk = [0_u8; 1 << 14];
        let read = timeout(PATIENCE, self.stream.read(&mut chunk))
            .await
            .expect("the mock to answer before the test gives up")
            .expect("a readable socket");
        assert!(read > 0, "the mock closed the connection mid-answer");
        self.read.extend_from_slice(&chunk[..read]);
    }

    async fn filled_until(&mut self, enough: impl Fn(&[u8]) -> Option<usize>) -> usize {
        loop {
            if let Some(end) = enough(&self.read) {
                return end;
            }
            self.read_more().await;
        }
    }

    async fn filled_to(&mut self, bytes: usize) {
        self.filled_until(move |read| (read.len() >= bytes).then_some(bytes))
            .await;
    }

    fn take(&mut self, end: usize) -> Vec<u8> {
        self.read.drain(..end).collect()
    }

    async fn ask(&mut self, method: &str, path: &str, body: &[u8]) -> Reply {
        self.send(&http_request(method, path, body)).await;
        self.reply().await
    }

    async fn reply(&mut self) -> Reply {
        let head_end = self
            .filled_until(|read| find(read, HEADER_TERMINATOR))
            .await;
        let head = String::from_utf8_lossy(&self.read[..head_end]).into_owned();
        let body_start = head_end + HEADER_TERMINATOR.len();
        let end = body_start + content_length(&head);
        self.filled_to(end).await;
        Reply {
            status: status_of(&head),
            body: self.take(end)[body_start..].to_vec(),
        }
    }

    async fn exchange(&mut self, opcode: u8, stream: i16, body: &[u8]) -> Answer {
        self.send(&request_frame(opcode, stream, body)).await;
        self.answer().await
    }

    async fn answer(&mut self) -> Answer {
        self.filled_to(wire::HEADER_BYTES).await;
        let head: [u8; wire::HEADER_BYTES] = self.read[..wire::HEADER_BYTES]
            .try_into()
            .expect("a whole frame header");
        assert_eq!(head[0], wire::RESPONSE_VERSION);
        let length = u32::from_be_bytes([head[5], head[6], head[7], head[8]]) as usize;
        let end = wire::HEADER_BYTES + length;
        self.filled_to(end).await;
        Answer {
            stream: i16::from_be_bytes([head[2], head[3]]),
            opcode: head[4],
            body: self.take(end)[wire::HEADER_BYTES..].to_vec(),
        }
    }
}

/// `osrate` empties the index before every concurrency level and will not start
/// loading until both halves of that reset have been read back: the delete is
/// gone, and the new index serves at zero. Each route has to answer *for the
/// index* — a `_count` reporting zero documents for an index that does not
/// exist would let the gate waiting for the delete to land pass on the index
/// that is still there, and the level would inherit the last one's documents.
#[tokio::test]
async fn the_opensearch_reset_sequence_is_answered_in_order_on_one_connection() {
    let mock = an_opensearch_mock().await;
    let mut wire = Wire::to(engine_port(&mock)).await;

    let version = wire.ask("GET", "/", b"").await;
    let deleted = wire.ask("DELETE", INDEX_PATH, b"").await;
    let gone = wire.ask("HEAD", INDEX_PATH, b"").await;
    let created = wire.ask("PUT", INDEX_PATH, b"{}").await;
    let empty = wire.ask("GET", COUNT_PATH, b"").await;
    let bulked = wire.ask("POST", BULK_PATH, &bulk_body(&[1, 2, 3])).await;
    let stats = wire.ask("GET", STATS_PATH, b"").await;

    assert_eq!(version.status, 200);
    assert_eq!(version.json()["version"]["number"], opensearch::VERSION);
    assert_eq!(deleted.status, 200);
    assert_eq!(gone.status, 404);
    assert_eq!(created.status, 200);
    assert_eq!(empty.status, 200);
    assert_eq!(empty.json()["count"], 0);
    assert_eq!(bulked.status, 200);
    assert_eq!(bulked.json()["items"].as_array().map(Vec::len), Some(3));
    assert_eq!(stats.status, 200);
    assert_eq!(stats.json()["_all"]["total"]["indexing"]["index_total"], 3);
}

/// The loaders hold many requests outstanding on few connections, so a read
/// commonly carries several whole requests and they are answered in one write.
/// A client pairs replies to requests by position, so a dropped or reordered
/// answer mis-pairs every reply after it without either side erroring.
#[tokio::test]
async fn every_request_written_at_once_is_answered_in_the_order_it_was_sent() {
    let mock = an_opensearch_mock().await;
    let mut wire = Wire::to(engine_port(&mock)).await;
    let mut pipelined = Vec::new();
    for page_id in 1..=4 {
        pipelined.extend_from_slice(&http_request("POST", BULK_PATH, &bulk_body(&[page_id])));
        pipelined.extend_from_slice(&http_request("GET", COUNT_PATH, b""));
    }

    wire.send(&pipelined).await;

    let mut counted = Vec::new();
    for _ in 1..=4 {
        assert_eq!(wire.reply().await.status, 200);
        counted.push(wire.reply().await.json()["count"].clone());
    }
    assert_eq!(counted, vec![json!(1), json!(2), json!(3), json!(4)]);
}

/// Documents in over CQL, the count out over HTTP: `scyllarate` reads its build
/// rate off the half it never wrote to, so the two endpoints have to be sharing
/// one index. Halves that disagreed would report a complete, plausible, wrong
/// build rate rather than an error.
#[tokio::test]
async fn documents_executed_over_cql_are_counted_by_the_vector_store_endpoint() {
    const DOCUMENTS: i16 = 5;
    let mock = a_scylla_mock().await;
    let mut cql = Wire::to(engine_port(&mock)).await;

    let supported = cql.exchange(wire::OPCODE_OPTIONS, 1, b"").await;
    let ready = cql.exchange(wire::OPCODE_STARTUP, 2, &startup_body()).await;
    let prepared = cql
        .exchange(wire::OPCODE_PREPARE, 3, &prepare_body(AN_INSERT))
        .await;
    let statement = prepared_statement_id(&prepared.body);
    cql.send(&pipelined_executes(&statement, DOCUMENTS)).await;
    let mut executed = Vec::new();
    for _ in 0..DOCUMENTS {
        executed.push(cql.answer().await);
    }
    let status = Wire::to(vector_store_port(&mock))
        .await
        .ask("GET", STATUS_PATH, b"")
        .await;

    assert_eq!(supported.opcode, wire::OPCODE_SUPPORTED);
    assert_eq!((ready.opcode, ready.stream), (wire::OPCODE_READY, 2));
    assert_eq!(result_kind(&prepared.body), wire::RESULT_PREPARED);
    assert!(executed
        .iter()
        .all(|answer| answer.opcode == wire::OPCODE_RESULT
            && result_kind(&answer.body) == wire::RESULT_VOID));
    let streams: Vec<i16> = executed.iter().map(|answer| answer.stream).collect();
    assert_eq!(streams, (0..DOCUMENTS).collect::<Vec<i16>>());
    assert_eq!(status.status, 200);
    assert_eq!(
        status.json(),
        json!({"count": DOCUMENTS, "status": SERVING})
    );
}

/// The reason the port to Rust exists: every connection is answered on whichever
/// worker is free, and the counters they all add to are striped across shards
/// keyed by the lane handed out at accept. A lane collision or a shard summed
/// wrongly would lose or double documents at exactly the rate the instrument
/// exists to report, and nothing in a run's artifacts would say so.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn documents_driven_down_sixteen_connections_at_once_are_counted_exactly() {
    const CONNECTIONS: usize = 16;
    const BULKS_EACH: usize = 8;
    const DOCS_PER_BULK: usize = 4;
    let mock = an_opensearch_mock().await;
    let port = engine_port(&mock);

    let loading: Vec<_> = (0..CONNECTIONS)
        .map(|_| tokio::spawn(load_documents(port, BULKS_EACH, DOCS_PER_BULK)))
        .collect();
    for connection in loading {
        connection
            .await
            .expect("every connection to finish loading");
    }

    let offered = (CONNECTIONS * BULKS_EACH * DOCS_PER_BULK) as u64;
    assert_eq!(mock.work.snapshot().docs, offered);
    assert_eq!(mock.work.snapshot().ops, (CONNECTIONS * BULKS_EACH) as u64);
    assert_eq!(mock.index.count(), offered);
}

async fn load_documents(port: u16, bulks: usize, docs_per_bulk: usize) {
    let page_ids: Vec<u64> = (0..docs_per_bulk as u64).collect();
    let mut wire = Wire::to(port).await;
    for _ in 0..bulks {
        let accepted = wire.ask("POST", BULK_PATH, &bulk_body(&page_ids)).await;
        assert_eq!(accepted.status, 200);
    }
}

/// A harness pointed at the wrong index must fail its own SERVING gate rather
/// than sail through it on another index's count, and the misdirection has to
/// be recorded: a status answered anyway would produce a complete, plausible,
/// wrong build rate with nothing in the artifacts to say the run was measuring
/// an index nobody created.
#[tokio::test]
async fn a_status_request_for_another_index_is_refused_over_the_socket_and_recorded() {
    let misdirected = "/api/v1/indexes/wiki/some_other_index/status";
    let mock = a_scylla_mock().await;
    let mut vector_store = Wire::to(vector_store_port(&mock)).await;

    let refused = vector_store.ask("GET", misdirected, b"").await;

    assert_eq!(refused.status, 404);
    assert_eq!(
        mock.work.unexpected().get(&format!("GET {misdirected}")),
        Some(&1)
    );
}
