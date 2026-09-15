use super::*;

use std::time::Instant;

use clap::Parser;

use crate::index::{Clock, IndexStatus, ModelledIndex, Refresh, SERVING};
use crate::provenance::RunStart;

fn args_for(flags: &[&str]) -> Args {
    let argv = ["engine-mock", "--host", "127.0.0.1"]
        .into_iter()
        .chain(flags.iter().copied());
    Args::try_parse_from(argv).expect("the flag surface two runbooks launch this binary by")
}

fn accepted(ops: u64, docs: u64) -> AcceptedWork {
    let work = AcceptedWork::new(4);
    work.add(0, ops, docs);
    work
}

fn an_index() -> ModelledIndex {
    ModelledIndex::created(
        4,
        Duration::ZERO,
        Refresh::immediately(),
        Clock::monotonic(),
    )
}

/// The document is built from what the endpoints turned out to be, so a test
/// that only cares about the counters still has to say which ports they were
/// served on.
fn witness_over<'a>(work: &'a AcceptedWork, index: &'a ModelledIndex, port: u16) -> Witness<'a> {
    Witness {
        work,
        index,
        engine_port: port,
        vector_store_port: None,
    }
}

async fn a_free_port() -> u16 {
    tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("a port to be free")
        .local_addr()
        .expect("the bound address")
        .port()
}

/// A mock with both endpoints up, on ports nothing else in this suite took.
///
/// `--port 0` lets the kernel choose, but `--vs-port 0` cannot: on that flag 0
/// means the vector-store is off. So the vector-store port is picked by binding
/// and releasing one — which is a race with every other test doing the same,
/// and is retried rather than asserted, because a suite that fails on a port
/// collision fails for a reason that has nothing to do with the mock.
async fn a_mock_with_a_vector_store(flags: &[&str]) -> (Mock, u16) {
    for _ in 0..PORT_ATTEMPTS {
        let port = a_free_port().await.to_string();
        let mut argv = vec!["--mode", "cql", "--port", "0", "--vs-port", &port];
        argv.extend_from_slice(flags);
        if let Ok(mock) = start(&args_for(&argv), 1).await {
            let bound = port.parse().expect("the port this test chose");
            return (mock, bound);
        }
    }
    panic!("no free port for the vector-store endpoint in {PORT_ATTEMPTS} attempts");
}

const PORT_ATTEMPTS: usize = 20;

/// Two of these keys are read by a gate rather than by a person: a
/// reconciliation gate reads `docs_accepted` against what the harness CSVs
/// claim to have submitted, and `unexpected_requests` is how a setup call that
/// stopped arriving is caught. The rest is what makes the run attributable to a
/// host and a configuration months later.
#[test]
fn the_stats_document_merges_the_provenance_header_with_what_arrived() {
    let work = accepted(7, 700);
    let args = args_for(&[
        "--mode",
        "cql",
        "--port",
        "9042",
        "--delay-ms",
        "2.5",
        "--tokio-workers",
        "3",
    ]);

    let index = an_index();
    let document = stats_document(
        &args,
        &witness_over(&work, &index, 9042),
        &RunStart::now(),
        work.snapshot(),
    );

    assert_eq!(document["docs_accepted"], json!(700));
    assert_eq!(document["ops_accepted"], json!(7));
    assert_eq!(document["unexpected_requests"], json!({}));
    assert_eq!(document["mode"], json!("cql"));
    assert_eq!(document["port"], json!(9042));
    assert_eq!(document["bind_host"], json!("127.0.0.1"));
    assert_eq!(document["delay_ms"], json!(2.5));
    assert_eq!(document["tokio_workers"], json!(3));
    assert_eq!(document["tcp_ack"], json!(tcp::quickack_note()));
}

/// The `-null-sink` marker is what tells a calibration run's artifacts apart
/// from a real engine's, and a gate greps for it. Without it a number that
/// measures the loader and nothing else can be filed beside one that measures
/// ScyllaDB.
#[test]
fn the_engine_name_says_the_file_came_from_a_mock_and_which_one() {
    let work = accepted(0, 0);
    let index = an_index();
    assert_eq!(
        stats_document(
            &args_for(&["--mode", "cql"]),
            &witness_over(&work, &index, 9042),
            &RunStart::now(),
            work.snapshot()
        )["engine"],
        json!("null-sink-cql")
    );
    assert_eq!(
        stats_document(
            &args_for(&["--mode", "http"]),
            &witness_over(&work, &index, 9200),
            &RunStart::now(),
            work.snapshot()
        )["engine"],
        json!("null-sink-http")
    );
}

/// SIGTERM is how a run ends, and this file is what outlives it: the only count
/// of what arrived that does not come from the thing being measured. JSON a
/// gate cannot parse takes the reconciliation step with it.
#[test]
fn write_stats_leaves_json_a_gate_can_parse() {
    let work = accepted(1, 42);
    let index = an_index();
    let document = stats_document(
        &args_for(&["--mode", "http"]),
        &witness_over(&work, &index, 9200),
        &RunStart::now(),
        work.snapshot(),
    );
    let file = tempfile::NamedTempFile::new().expect("a temporary stats file");

    write_stats(file.path(), &document).expect("the stats file to be written");

    let written = std::fs::read_to_string(file.path()).expect("the stats file to be readable");
    let parsed: serde_json::Value = serde_json::from_str(&written).expect("parseable JSON");
    assert_eq!(parsed, document);
}

#[tokio::test]
async fn an_http_mock_binds_the_one_endpoint_it_serves() {
    let mock = start(&args_for(&["--mode", "http", "--port", "0"]), 1)
        .await
        .expect("the http endpoint to bind");

    let ports = mock.ports();

    mock.stop();
    assert_eq!(ports.len(), 1, "{ports:?}");
    assert_ne!(ports[0], 0, "an ephemeral bind reports the port it got");
}

/// Both halves, not one or the other: the CQL half accepts the documents and
/// the DDL, and the vector-store half is where a loader reads back what that
/// did. A loader that gates on the index cannot be measured against half a
/// mock.
#[tokio::test]
async fn a_cql_mock_binds_the_engine_port_and_the_index_a_loader_reads_back_from() {
    let (mock, vs_port) = a_mock_with_a_vector_store(&[]).await;

    let ports = mock.ports();
    mock.stop();
    assert_eq!(ports.len(), 2, "{ports:?}");
    assert_eq!(ports[1], vs_port);
}

/// The campaign applies `schema.cql` and `index.cql` before anything writes, so
/// a `--no-reset` run must find an index rather than a 404.
#[tokio::test]
async fn the_index_a_loader_meets_already_exists() {
    let (mock, _) = a_mock_with_a_vector_store(&[]).await;

    let status = mock.index.status();
    mock.stop();
    assert_eq!(
        status,
        Some(IndexStatus {
            count: 0,
            status: SERVING
        })
    );
}

#[tokio::test]
async fn await_stop_returns_when_its_duration_elapses() {
    let limit = Duration::from_millis(50);
    let started = Instant::now();

    await_stop(Some(limit)).await.expect("the signal handlers");

    assert!(started.elapsed() >= limit, "{:?}", started.elapsed());
}

#[test]
fn a_vector_store_that_was_turned_off_is_not_announced() {
    let off = args_for(&["--mode", "cql", "--vs-port", "0"]);
    let on = args_for(&["--mode", "cql"]);
    assert_eq!(vector_store_note(&off, off.vs_port()), "");
    assert!(
        vector_store_note(&on, on.vs_port()).contains("6080"),
        "{on:?}"
    );
}

/// The warning is how a dropped setup call is caught, and it only reads that
/// way while a clean run stays silent: one printed after every run is one a
/// reader learns to skip past.
#[test]
fn a_mock_that_answered_everything_it_was_asked_warns_about_nothing() {
    let work = accepted(5, 500);

    assert_eq!(unexpected_warning(&work), None);
}

/// And one that was asked for something it does not answer names it, so the
/// route that stopped arriving is in the run's own log rather than only in the
/// JSON nobody reads until the reconciliation step.
#[test]
fn a_mock_that_was_asked_for_a_route_it_does_not_answer_names_it() {
    let work = accepted(5, 500);
    work.note_unexpected("GET /wiki-articles/_mapping");

    let warning = unexpected_warning(&work).expect("a warning");

    assert!(warning.starts_with("WARNING:"), "{warning}");
    assert!(warning.contains("GET /wiki-articles/_mapping"), "{warning}");
}

/// A document accepted while no index exists means the loader and the mock
/// disagree about the lifecycle — which is the silent, plausible failure this
/// instrument exists to expose. The Python sink counted those documents and
/// reported them nowhere but a unit test.
#[test]
fn the_document_reports_what_arrived_while_there_was_no_index_to_take_it() {
    let work = accepted(1, 1);
    let index = an_index();
    index.drop_index();
    index.add(0, 4);

    let document = stats_document(
        &args_for(&["--mode", "cql"]),
        &witness_over(&work, &index, 9042),
        &RunStart::now(),
        work.snapshot(),
    );

    assert_eq!(document["index_adds_while_absent"], json!(4));
}

/// With `--port 0` the kernel chooses, and an artifact that recorded the 0 it
/// asked for would name a port the run never used. `--vs-port 0` cannot mean
/// the same thing — on that flag 0 is how a run turns the vector-store off —
/// so it is given a port of its own here.
#[tokio::test]
async fn the_document_records_the_ports_the_endpoints_actually_took() {
    let (mock, vector_store) = a_mock_with_a_vector_store(&[]).await;
    let args = args_for(&["--mode", "cql", "--port", "0"]);

    let document = stats_document(
        &args,
        &mock.witness(),
        &RunStart::now(),
        mock.work.snapshot(),
    );
    mock.stop();

    assert_eq!(document["port"], json!(mock.engine_port()));
    assert_ne!(document["port"], json!(0));
    assert_eq!(document["vs_port"], json!(vector_store));
}

/// The same `--stats-out` path is reused every time the mocks are replaced
/// mid-session, so a run that never reaches the rename must leave NO file — not
/// the previous generation's, which the reconciliation gate would read as this
/// run's with nothing in its output to tell them apart.
///
/// The staging file is made un-writable by putting a directory where it goes,
/// which is the one failure that can be arranged without also blocking the
/// removal that has to happen first.
#[test]
fn a_write_that_never_finishes_leaves_no_artifact_rather_than_the_last_one() {
    let home = tempfile::tempdir().expect("a temporary directory");
    let path = home.path().join("sink.json");
    let work = accepted(1, 1);
    let index = an_index();
    let generation = |label: &str| {
        stats_document(
            &args_for(&["--mode", "http", "--label", label]),
            &witness_over(&work, &index, 9200),
            &RunStart::now(),
            work.snapshot(),
        )
    };
    write_stats(&path, &generation("GEN-ONE")).expect("the first generation to be written");
    std::fs::create_dir(path.with_extension("json.partial")).expect("a blocked staging path");

    let second = write_stats(&path, &generation("GEN-TWO"));

    assert!(second.is_err(), "the staged write cannot have succeeded");
    assert!(
        !path.exists(),
        "the first generation's artifact is still there, and a gate would read it as the second's"
    );
}
