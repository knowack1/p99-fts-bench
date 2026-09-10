//! The half of the tool that only exists against a real CQL endpoint: the
//! session, the prepared INSERT, the topology read and the driver-backed
//! inserter. The endpoint is the repo's accept-and-discard sink, so this covers
//! the client's own path without a ScyllaDB node.
//!
//! Ignored by default because it shells out to `bench/.venv`. Run it with:
//!
//! ```text
//! cargo test --test live_cql -- --ignored
//! ```
//!
//! The sink advertises neither the shard extension nor partition-key indexes, so
//! a run against it reports `shard_aware=false` and exercises no shard routing.
//! That is correct behaviour, not a fault — what it gives you is the client's
//! own ceiling.
use std::io::Write;
use std::net::TcpListener;
use std::process::{Child, Command, Stdio};
use std::sync::Arc;
use std::time::{Duration, Instant};

use scyllarate::corpus::CorpusSource;
use scyllarate::insert::CqlInserter;
use scyllarate::notes::Notes;
use scyllarate::report::PointResult;
use scyllarate::session::{self, ConnectOptions};
use scyllarate::sweep::{self, Cancel, Inserter};

const BENCH_ROOT: &str = "..";
const VENV_PYTHON: &str = ".venv/bin/python3";
const KEYSPACE: &str = "wiki";
const TABLE: &str = "articles";
const STARTUP_TIMEOUT: Duration = Duration::from_secs(20);

struct NullSink {
    child: Child,
    port: u16,
}

impl NullSink {
    fn start() -> Self {
        let port = a_free_port();
        let child = Command::new(VENV_PYTHON)
            .args(["-m", "ftsbench.null_sink", "--mode", "cql", "--port"])
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

    fn options(&self) -> ConnectOptions {
        ConnectOptions {
            hosts: vec!["127.0.0.1".to_string()],
            port: self.port,
            keyspace: KEYSPACE.to_string(),
            consistency: session::consistency_from_name("LOCAL_ONE").unwrap(),
            request_timeout: Duration::from_secs(10),
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
        let uuid = uuid::Uuid::new_v5(
            &uuid::Uuid::NAMESPACE_URL,
            format!("wikipedia-page:{n}").as_bytes(),
        );
        writeln!(
            file,
            r#"{{"id": {n}, "uuid": "{uuid}", "title": "t{n}", "text": "body {n}"}}"#
        )
        .unwrap();
    }
    (tmp, path)
}

async fn an_inserter(sink: &NullSink) -> CqlInserter {
    let session = session::connect(&sink.options()).await.unwrap();
    let statement = session::prepare_insert(&session, TABLE).await.unwrap();
    CqlInserter::new(session, statement)
}

#[tokio::test]
#[ignore = "needs bench/.venv to run the null sink"]
async fn a_session_connects_prepares_and_reports_its_topology() {
    let sink = NullSink::start();
    let inserter = an_inserter(&sink).await;
    let topology = session::read_topology(inserter.session(), KEYSPACE, 4)
        .await
        .unwrap();

    assert!(!topology.scylla_version.is_empty());
    assert_eq!(topology.compression, "None");
    assert_eq!(topology.driver_version, session::DRIVER_VERSION);
    assert!(topology.runtime.contains("workers:4"));
}

/// The sink is not sharded, and the header has to say so rather than leave a
/// reader to assume the client reached every shard.
#[tokio::test]
#[ignore = "needs bench/.venv to run the null sink"]
async fn an_unsharded_endpoint_is_reported_as_unsharded() {
    let sink = NullSink::start();
    let inserter = an_inserter(&sink).await;
    let topology = session::read_topology(inserter.session(), KEYSPACE, 1)
        .await
        .unwrap();

    assert_eq!(topology.shard_aware, "false");
    assert!(topology.shards.contains("shards:none"));
}

#[tokio::test]
#[ignore = "needs bench/.venv to run the null sink"]
async fn the_driver_backed_inserter_writes_one_document() {
    let sink = NullSink::start();
    let inserter = an_inserter(&sink).await;
    let params = CorpusSource::new(a_corpus(1).1, 0)
        .open()
        .unwrap()
        .next()
        .unwrap()
        .unwrap();

    assert!(inserter.insert(params).await.is_ok());
}

#[tokio::test]
#[ignore = "needs bench/.venv to run the null sink"]
async fn a_whole_ladder_runs_against_a_live_endpoint() {
    let sink = NullSink::start();
    let (_tmp, path) = a_corpus(500);
    let source = CorpusSource::new(&path, 0);
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
            &Notes::new(Duration::from_secs(3600), Arc::new(|_| {})),
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
    assert!(results.iter().all(|point| point.p99_ms.is_some()));
}

#[tokio::test]
#[ignore = "needs bench/.venv to run the null sink"]
async fn an_endpoint_that_is_not_listening_is_reported_not_hung_on() {
    let options = ConnectOptions {
        hosts: vec!["127.0.0.1".to_string()],
        port: a_free_port(),
        keyspace: KEYSPACE.to_string(),
        consistency: session::consistency_from_name("LOCAL_ONE").unwrap(),
        request_timeout: Duration::from_secs(1),
    };
    let failure = format!("{:#}", session::connect(&options).await.unwrap_err());
    assert!(failure.contains("cannot reach"));
}
