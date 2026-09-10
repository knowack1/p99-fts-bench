//! The half of the tool that only exists against a real HTTP endpoint: the
//! client, the index check, the cluster read and the `_bulk`-backed inserter.
//! The endpoint is the repo's accept-and-discard sink, so this covers the
//! client's own path without an OpenSearch node.
//!
//! Ignored by default because it shells out to `bench/.venv`. Run it with:
//!
//! ```text
//! cargo test --test live_http -- --ignored
//! ```
//!
//! The sink answers `/`, `HEAD /{index}`, `_bulk`, `_count` and
//! `/_nodes/thread_pool`, and nothing else. It does *not* answer
//! `GET /{index}/_settings` or `_mapping`, so a run against it reports those
//! facts as unknown and notes two unexpected routes on its own stderr. That is
//! correct behaviour, not a fault — what it gives you is the client's own
//! ceiling.
//!
//! The sink counts documents by walking the NDJSON alternation
//! (`null_sink_http.bulk_action_count`), which makes `_count` an independent
//! check that the payload this tool builds really is one action line plus one
//! source line per document.
use std::io::Write;
use std::net::TcpListener;
use std::process::{Child, Command, Stdio};
use std::sync::Arc;
use std::time::{Duration, Instant};

use opensearch::{CountParts, OpenSearch};
use serde_json::Value;

use osrate::client::{self, ConnectOptions, UNKNOWN};
use osrate::corpus::CorpusSource;
use osrate::insert::BulkInserter;
use osrate::notes::Notes;
use osrate::report::PointResult;
use osrate::sweep::{self, Cancel, Inserter, Shape};

const BENCH_ROOT: &str = "..";
const VENV_PYTHON: &str = ".venv/bin/python3";
const INDEX: &str = "wiki-articles";
const STARTUP_TIMEOUT: Duration = Duration::from_secs(20);
const BATCH: usize = 25;

struct NullSink {
    child: Child,
    port: u16,
}

impl NullSink {
    fn start() -> Self {
        let port = a_free_port();
        let child = Command::new(VENV_PYTHON)
            .args(["-m", "ftsbench.null_sink", "--mode", "http", "--port"])
            .arg(port.to_string())
            .current_dir(BENCH_ROOT)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("bench/.venv must exist; see bench/README.md");
        let sink = Self { child, port };
        sink.await_ready();
        sink
    }

    fn await_ready(&self) {
        let deadline = Instant::now() + STARTUP_TIMEOUT;
        while Instant::now() < deadline {
            if std::net::TcpStream::connect(("127.0.0.1", self.port)).is_ok() {
                return;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        panic!("the null sink never opened port {}", self.port);
    }

    fn url(&self) -> String {
        format!("http://127.0.0.1:{}", self.port)
    }

    fn options(&self) -> ConnectOptions {
        ConnectOptions {
            url: self.url(),
            index: INDEX.to_string(),
            request_timeout: Duration::from_secs(30),
        }
    }
}

impl Drop for NullSink {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn a_free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
        .port()
}

fn a_corpus(documents: usize) -> (tempfile::TempDir, std::path::PathBuf) {
    let tmp = tempfile::tempdir().unwrap();
    let path = tmp.path().join("corpus.jsonl");
    let mut file = std::fs::File::create(&path).unwrap();
    for n in 0..documents {
        writeln!(
            file,
            r#"{{"id": {n}, "title": "t{n}", "text": "body {n}"}}"#
        )
        .unwrap();
    }
    (tmp, path)
}

fn a_shape() -> Shape {
    Shape {
        batch_size: BATCH,
        queue_depth: 2,
    }
}

fn quiet() -> Notes {
    Notes::new(Duration::from_secs(3600), Arc::new(|_| {}))
}

async fn an_inserter(sink: &NullSink) -> BulkInserter {
    let client = client::connect(&sink.options()).await.unwrap();
    BulkInserter::new(client, INDEX)
}

/// What the sink counted, by its own walk of the NDJSON alternation.
async fn documents_accepted(client: &OpenSearch) -> u64 {
    let body: Value = client
        .count(CountParts::Index(&[INDEX]))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    body["count"].as_u64().unwrap()
}

#[tokio::test]
#[ignore = "needs bench/.venv to run the null sink"]
async fn a_client_connects_and_reports_its_cluster() {
    let sink = NullSink::start();
    let inserter = an_inserter(&sink).await;
    let cluster = client::read_cluster(inserter.client(), INDEX, 4)
        .await
        .unwrap();

    assert!(cluster.opensearch_version.contains("null-sink"));
    assert_eq!(cluster.client_version, client::CLIENT_VERSION);
    assert_eq!(cluster.http_client_version, client::HTTP_CLIENT_VERSION);
    assert_eq!(cluster.index, INDEX);
    assert!(cluster.runtime.contains("workers:4"));
    assert!(cluster.write_pool.contains("write:"));
}

/// The sink answers `_bulk` but not `_settings` or `_mapping`, and the header
/// has to say it read nothing rather than claim a shape nobody looked at.
#[tokio::test]
#[ignore = "needs bench/.venv to run the null sink"]
async fn facts_the_endpoint_does_not_answer_are_reported_as_unknown() {
    let sink = NullSink::start();
    let inserter = an_inserter(&sink).await;
    let cluster = client::read_cluster(inserter.client(), INDEX, 1)
        .await
        .unwrap();

    assert_eq!(cluster.index_shards, UNKNOWN);
    assert_eq!(cluster.replicas, UNKNOWN);
    assert_eq!(cluster.distribution, UNKNOWN);
}

#[tokio::test]
#[ignore = "needs bench/.venv to run the null sink"]
async fn the_bulk_backed_inserter_writes_one_batch() {
    let sink = NullSink::start();
    let inserter = an_inserter(&sink).await;
    let (_tmp, path) = a_corpus(BATCH);
    let batch = CorpusSource::new(&path, 0, BATCH)
        .open()
        .unwrap()
        .next()
        .unwrap()
        .unwrap();

    let outcome = inserter.insert(batch).await.unwrap();
    assert!(outcome.is_clean());
    assert_eq!(documents_accepted(inserter.client()).await, BATCH as u64);
}

/// An independent check on the wire payload: the sink counts documents by the
/// NDJSON grammar, so a payload missing its source lines would be counted as
/// twice the documents rather than passing quietly.
#[tokio::test]
#[ignore = "needs bench/.venv to run the null sink"]
async fn the_endpoint_counts_exactly_the_documents_that_were_offered() {
    let sink = NullSink::start();
    let inserter = Arc::new(an_inserter(&sink).await);
    let (_tmp, path) = a_corpus(100);
    let source = CorpusSource::new(&path, 0, BATCH);
    let mut collect = |_: PointResult| Ok(());

    sweep::run_sweep(
        Arc::clone(&inserter),
        || source.open(),
        &[4],
        a_shape(),
        &quiet(),
        &Cancel::default(),
        &mut collect,
    )
    .await
    .unwrap();

    assert_eq!(documents_accepted(inserter.client()).await, 100);
}

#[tokio::test]
#[ignore = "needs bench/.venv to run the null sink"]
async fn a_whole_ladder_runs_against_a_live_endpoint() {
    let sink = NullSink::start();
    let (_tmp, path) = a_corpus(500);
    let source = CorpusSource::new(&path, 0, BATCH);
    let mut results: Vec<PointResult> = Vec::new();

    {
        let mut collect = |result: PointResult| {
            results.push(result);
            Ok(())
        };
        sweep::run_sweep(
            Arc::new(an_inserter(&sink).await),
            || source.open(),
            &[4, 16],
            a_shape(),
            &quiet(),
            &Cancel::default(),
            &mut collect,
        )
        .await
        .unwrap();
    }

    let levels: Vec<usize> = results.iter().map(|point| point.concurrency).collect();
    assert_eq!(levels, [4, 16]);
    assert!(results
        .iter()
        .all(|point| point.docs == 500 && point.errors == 0));
    assert!(results.iter().all(|point| point.bulks == 20));
    assert!(results.iter().all(|point| point.p99_ms.is_some()));
}

#[tokio::test]
#[ignore = "needs bench/.venv to run the null sink"]
async fn an_endpoint_that_is_not_listening_is_reported_not_hung_on() {
    let options = ConnectOptions {
        url: format!("http://127.0.0.1:{}", a_free_port()),
        index: INDEX.to_string(),
        request_timeout: Duration::from_secs(1),
    };
    let failure = format!("{:#}", client::connect(&options).await.unwrap_err());
    assert!(failure.contains("cannot reach"));
}
