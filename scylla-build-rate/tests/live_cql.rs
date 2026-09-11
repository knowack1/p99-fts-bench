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

use scyllarate::build_rate::{IndexWatch, WatchTiming};
use scyllarate::corpus::CorpusSource;
use scyllarate::insert::CqlInserter;
use scyllarate::notes::Notes;
use scyllarate::report::PointResult;
use scyllarate::reset::{GateTiming, ResetPlan, ResettingInserters};
use scyllarate::session::{self, ConnectOptions};
use scyllarate::sweep::{self, Cancel, Inserter, Watchers};
use scyllarate::vstore::{IndexProbe, DEFAULT_VS_INDEX};

const BENCH_ROOT: &str = "..";
const VENV_PYTHON: &str = ".venv/bin/python3";
const KEYSPACE: &str = "wiki";
const TABLE: &str = "articles";
const STARTUP_TIMEOUT: Duration = Duration::from_secs(20);

struct NullSink {
    child: Child,
    port: u16,
    vs_port: u16,
}

impl NullSink {
    /// Both halves, on ports nobody else in the suite holds: the CQL endpoint
    /// that accepts the documents and the vector-store endpoint that reports
    /// the index they landed in.
    fn start() -> Self {
        let (port, vs_port) = (a_free_port(), a_free_port());
        let child = Command::new(VENV_PYTHON)
            .args(["-m", "ftsbench.null_sink", "--mode", "cql", "--port"])
            .arg(port.to_string())
            .arg("--vs-port")
            .arg(vs_port.to_string())
            .current_dir(BENCH_ROOT)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("bench/.venv must exist; see bench/README.md");
        let sink = Self {
            child,
            port,
            vs_port,
        };
        sink.await_ready();
        sink
    }

    fn vs_url(&self) -> String {
        format!("http://127.0.0.1:{}", self.vs_port)
    }

    fn probe(&self) -> Arc<IndexProbe> {
        Arc::new(
            IndexProbe::new(
                &self.vs_url(),
                KEYSPACE,
                DEFAULT_VS_INDEX,
                Duration::from_secs(10),
            )
            .unwrap(),
        )
    }

    fn plan(&self) -> ResetPlan {
        ResetPlan {
            keyspace: KEYSPACE.to_string(),
            table: TABLE.to_string(),
            index: DEFAULT_VS_INDEX.to_string(),
        }
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
    let session = Arc::new(session::connect(&sink.options()).await.unwrap());
    let statement = session::prepare_insert(&session, TABLE).await.unwrap();
    CqlInserter::new(session, statement)
}

/// The real per-level setup, so the live test exercises the reset the binary
/// performs rather than a stand-in for it.
async fn inserters_for(sink: &NullSink, reset: bool) -> ResettingInserters {
    ResettingInserters::new(
        Arc::new(session::connect(&sink.options()).await.unwrap()),
        sink.plan(),
        Some(sink.probe()),
        GateTiming {
            poll_interval: Duration::from_millis(50),
            timeout: Duration::from_secs(20),
        },
        quiet(),
        reset,
    )
}

fn quiet() -> Notes {
    Notes::new(Duration::from_secs(3600), Arc::new(|_| {}))
}

fn watching(sink: &NullSink) -> IndexWatch {
    IndexWatch::on(
        sink.probe(),
        WatchTiming {
            poll_interval: Duration::from_millis(50),
            settle_timeout: Duration::from_secs(20),
            idle_timeout: Duration::from_secs(2),
        },
    )
}

async fn ladder(
    sink: &NullSink,
    documents: usize,
    levels: &[usize],
    reset: bool,
) -> Vec<PointResult> {
    let (_tmp, path) = a_corpus(documents);
    let source = CorpusSource::new(&path, 0);
    let inserters = inserters_for(sink, reset).await;
    let mut results: Vec<PointResult> = Vec::new();
    {
        let mut collect = |result: PointResult| {
            results.push(result);
            Ok(())
        };
        let (index, notes) = (watching(sink), quiet());
        sweep::run_sweep(
            &inserters,
            || source.open(),
            levels,
            &Watchers {
                index: &index,
                notes: &notes,
                samples: None,
            },
            &Cancel::default(),
            &mut collect,
        )
        .await
        .unwrap();
    }
    results
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
    let results = ladder(&sink, 500, &[4, 16], false).await;

    let levels: Vec<usize> = results.iter().map(|point| point.concurrency).collect();
    assert_eq!(levels, [4, 16]);
    assert!(results
        .iter()
        .all(|point| point.docs == 500 && point.errors == 0));
    assert!(results.iter().all(|point| point.p99_ms.is_some()));
}

/// The end the whole feature exists for: the harness empties the index, loads,
/// and reads back what reached it — over the real wire, against the sink that
/// establishes the client ceiling.
#[tokio::test]
#[ignore = "needs bench/.venv to run the null sink"]
async fn every_level_resets_the_index_and_measures_what_it_built() {
    let sink = NullSink::start();
    let results = ladder(&sink, 500, &[4, 16], true).await;

    for point in &results {
        let build = point
            .index
            .as_ref()
            .expect("a watched level reports its build");
        assert_eq!(build.docs, 500, "each level builds from zero documents");
        assert_eq!(build.lag_docs, 0);
        assert!(build.settled);
        assert_eq!(build.status, "SERVING");
    }
    assert_eq!(results.len(), 2);
}

/// Without the reset the second level rewrites the first level's rows. The sink
/// counts inserts rather than documents, so it keeps climbing — what this pins
/// is that a level is credited only with what it added, never with the index it
/// inherited.
#[tokio::test]
#[ignore = "needs bench/.venv to run the null sink"]
async fn a_level_is_credited_only_with_the_documents_it_added() {
    let sink = NullSink::start();
    let results = ladder(&sink, 500, &[4, 16], false).await;

    assert!(results
        .iter()
        .all(|point| point.index.as_ref().unwrap().docs == 500));
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
