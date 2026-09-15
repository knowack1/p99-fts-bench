//! Starting the endpoints and stopping them: what gets served in each mode, the
//! progress line, and the exit that has to happen even when the run ends by
//! signal.
//!
//! **SIGTERM is the ordinary end.** The runbooks stop a mock by sending it one,
//! and then read the `--stats-out` JSON it leaves behind as the run's
//! independent witness — the only count of what arrived that does not come from
//! the thing being measured. A process that died without writing it would take
//! the reconciliation gate with it.
use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result};
use serde_json::json;
use tokio::runtime::Runtime;
use tokio::signal::unix::{signal, SignalKind};

use crate::cli::{Args, Mode};
use crate::counters::AcceptedWork;
use crate::cql::{self, NodeIdentity};
use crate::index::{Clock, ModelledIndex};
use crate::server::{self, Endpoint};
use crate::{opensearch, provenance, tcp, vstore};

/// Both the striping of the counters and the number of tokio workers are sized
/// from here. More lanes than workers costs a few cache lines and removes the
/// case where two busy connections share one.
const LANES_PER_WORKER: usize = 4;

pub fn build_runtime(workers: usize) -> Result<Runtime> {
    tokio::runtime::Builder::new_multi_thread()
        .worker_threads(workers)
        .enable_all()
        .build()
        .with_context(|| format!("cannot start a tokio runtime with {workers} workers"))
}

/// Everything the `--stats-out` document is made of, borrowed.
pub struct Witness<'a> {
    pub work: &'a AcceptedWork,
    pub index: &'a ModelledIndex,
    pub engine_port: u16,
    pub vector_store_port: Option<u16>,
}

pub struct Mock {
    pub work: Arc<AcceptedWork>,
    pub index: Arc<ModelledIndex>,
    endpoints: Vec<Endpoint>,
}

impl Mock {
    pub fn ports(&self) -> Vec<u16> {
        self.endpoints.iter().map(Endpoint::port).collect()
    }

    /// The engine-shaped endpoint is started first and is always there; the
    /// vector-store one is optional and follows it.
    pub fn engine_port(&self) -> u16 {
        self.endpoints[0].port()
    }

    pub fn vector_store_port(&self) -> Option<u16> {
        self.endpoints.get(1).map(Endpoint::port)
    }

    /// What the artifact is written from: the counters, the index, and the
    /// ports the endpoints actually took. Separate from `Mock` so that the
    /// document can be built — and checked — without binding anything.
    pub fn witness(&self) -> Witness<'_> {
        Witness {
            work: &self.work,
            index: &self.index,
            engine_port: self.engine_port(),
            vector_store_port: self.vector_store_port(),
        }
    }

    pub fn stop(&self) {
        for endpoint in &self.endpoints {
            endpoint.stop();
        }
    }
}

/// The engine's own endpoint, and beside it the index that endpoint feeds.
///
/// Both, not one or the other: the CQL half accepts the documents and the DDL,
/// and the vector-store half is where a loader reads back what that did. A
/// loader that gates on the index cannot be measured against half a mock.
pub async fn start(args: &Args, workers: usize) -> Result<Mock> {
    let lanes = workers * LANES_PER_WORKER;
    let work = Arc::new(AcceptedWork::new(lanes));
    let index = Arc::new(ModelledIndex::created(
        lanes,
        args.serving_delay(),
        args.refresh(),
        Clock::monotonic(),
    ));
    let mut endpoints = vec![engine_endpoint(args, &work, &index).await?];
    if let Some(port) = args.vs_port() {
        endpoints.push(
            server::serve_http(
                &args.host,
                port,
                Arc::new(vstore::Table::new(
                    Arc::clone(&work),
                    Arc::clone(&index),
                    &args.vs_keyspace,
                    &args.vs_index,
                )),
                args.delay(),
            )
            .await?,
        );
    }
    Ok(Mock {
        work,
        index,
        endpoints,
    })
}

async fn engine_endpoint(
    args: &Args,
    work: &Arc<AcceptedWork>,
    index: &Arc<ModelledIndex>,
) -> Result<Endpoint> {
    match args.mode {
        Mode::Http => {
            server::serve_http(
                &args.host,
                args.port(),
                Arc::new(opensearch::Table::new(Arc::clone(work), Arc::clone(index))),
                args.delay(),
            )
            .await
        }
        Mode::Cql => {
            server::serve_cql(
                &args.host,
                args.port(),
                Arc::new(cql::Node::new(
                    NodeIdentity::default(),
                    Arc::clone(work),
                    Arc::clone(index),
                )),
                args.delay(),
            )
            .await
        }
    }
}

/// The ports announced are the ones actually bound, not the ones asked for:
/// with `--port 0` the kernel chooses, and a readiness line that said `0` would
/// leave nothing able to find the endpoint it just started.
pub fn announce(args: &Args, workers: usize, mock: &Mock) {
    let delay = if args.delay_ms > 0.0 {
        format!(", delay {} ms", args.delay_ms)
    } else {
        String::new()
    };
    note(&format!(
        "engine mock ready: {} on {}:{}{delay}{}, {workers} tokio workers",
        args.mode.as_str(),
        args.host,
        mock.engine_port(),
        vector_store_note(args, mock.vector_store_port())
    ));
}

fn vector_store_note(args: &Args, port: Option<u16>) -> String {
    match port {
        None => String::new(),
        Some(port) => format!(
            ", vector-store {}/{} on {}:{port}",
            args.vs_keyspace, args.vs_index, args.host
        ),
    }
}

/// Progress on stderr, off the request path.
///
/// The mock's own rate is not the measurement — the loader's artifact is — but
/// one that printed nothing would leave a stalled run indistinguishable from a
/// slow one for the length of the point.
pub async fn report_periodically(work: Arc<AcceptedWork>, interval: Duration) {
    let mut ticks = tokio::time::interval(interval);
    ticks.tick().await;
    loop {
        ticks.tick().await;
        note(&work.snapshot().summary_line());
    }
}

/// Either signal ends the run, and a duration ends it on its own. Both are
/// awaited rather than raced against a sleep so that a `--duration 0` mock
/// waits forever without a timer.
pub async fn await_stop(duration: Option<Duration>) -> Result<()> {
    let mut interrupt = signal(SignalKind::interrupt()).context("cannot watch for SIGINT")?;
    let mut terminate = signal(SignalKind::terminate()).context("cannot watch for SIGTERM")?;
    let stopped = async {
        tokio::select! {
            _ = interrupt.recv() => (),
            _ = terminate.recv() => (),
        }
    };
    match duration {
        None => stopped.await,
        Some(limit) => {
            let _ = tokio::time::timeout(limit, stopped).await;
        }
    }
    Ok(())
}

/// The run's independent witness: what the mock was, and what arrived at it.
///
/// `index_adds_while_absent` has no counterpart in the Python sink, which
/// counted those documents and reported them nowhere. A document accepted while
/// no index exists means the loader and the mock disagree about the lifecycle,
/// which is precisely the silent, plausible failure this instrument is built to
/// expose — so it is in the artifact rather than in a debugger.
pub fn stats_document(
    args: &Args,
    witness: &Witness<'_>,
    started: &provenance::RunStart,
    snapshot: crate::counters::WorkSnapshot,
) -> serde_json::Value {
    let header = provenance::header(
        &format!("null-sink-{}", args.mode.as_str()),
        &args.label,
        started,
        vec![
            ("mode", json!(args.mode.as_str())),
            ("bind_host", json!(args.host)),
            ("port", json!(witness.engine_port)),
            ("vs_port", json!(witness.vector_store_port)),
            ("delay_ms", json!(args.delay_ms)),
            ("tokio_workers", json!(args.tokio_workers())),
            ("tcp_ack", json!(tcp::quickack_note())),
            (
                "purpose",
                json!("client calibration; not an engine measurement"),
            ),
            (
                "index_adds_while_absent",
                json!(witness.index.adds_while_absent()),
            ),
        ],
    );
    merge(header, witness.work.summary_of(snapshot))
}

fn merge(header: serde_json::Value, summary: serde_json::Value) -> serde_json::Value {
    let (serde_json::Value::Object(mut into), serde_json::Value::Object(from)) = (header, summary)
    else {
        unreachable!("both the header and the summary are objects");
    };
    into.extend(from);
    serde_json::Value::Object(into)
}

/// The destination is removed first, then written beside itself and renamed on.
///
/// Both halves matter, and they answer different failures. The rename is so a
/// reader never sees half a document. The removal is so a run that never
/// reaches the rename — SIGKILL, an unwritable path — leaves NO file rather
/// than the previous mock generation's, which the reconciliation gate would
/// read as this run's: the same fixed `--stats-out` path is reused every time
/// the sinks are replaced mid-session, and nothing the gate prints would tell
/// the two apart. An absent artifact reads as "this run did not stop cleanly",
/// which is what actually happened.
pub fn write_stats(path: &std::path::Path, document: &serde_json::Value) -> Result<()> {
    let mut text = serde_json::to_string_pretty(document)?;
    text.push('\n');
    let _ = std::fs::remove_file(path);
    let staged = path.with_extension("json.partial");
    std::fs::write(&staged, text).with_context(|| {
        format!(
            "cannot write the accepted-work JSON to {}",
            staged.display()
        )
    })?;
    std::fs::rename(&staged, path)
        .with_context(|| format!("cannot move the accepted-work JSON onto {}", path.display()))
}

/// Every line the mock says, flushed per line: a process killed mid-run has
/// already said what it got to.
pub fn note(message: &str) {
    use std::io::Write;
    let mut stderr = std::io::stderr();
    let _ = writeln!(stderr, "{message}");
    let _ = stderr.flush();
}

/// A mock that was asked for routes it does not answer has to say so on its way
/// out: the run would otherwise complete, the number would still look like a
/// client ceiling, and nothing would say the setup call never arrived.
pub fn warn_about_unexpected(work: &AcceptedWork) {
    if let Some(warning) = unexpected_warning(work) {
        note(&warning);
    }
}

/// The line, or `None` when there is nothing to warn about.
///
/// Separated from the printing so a test can assert what a clean run stays
/// silent about and what a dirty one names. The warning only reads as a warning
/// while a clean run prints nothing: one printed after every run is one a
/// reader learns to skip past.
pub fn unexpected_warning(work: &AcceptedWork) -> Option<String> {
    let unexpected = work.unexpected();
    if unexpected.is_empty() {
        return None;
    }
    Some(format!(
        "WARNING: the mock was asked for routes it does not answer: {} — a \
         loader or sampler changed, and the run may be measuring the error path",
        serde_json::to_string(&unexpected).unwrap_or_default()
    ))
}

#[cfg(test)]
#[path = "run_tests.rs"]
mod tests;
