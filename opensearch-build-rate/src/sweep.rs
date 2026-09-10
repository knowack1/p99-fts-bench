//! One bounded channel, N worker tasks, one measurement point per concurrency
//! level.
//!
//! In-flight is exactly N `_bulk` requests: each worker takes one batch and
//! awaits its bulk before taking the next. Documents in flight is therefore
//! `N * batch_size`, which is the number to quote when comparing this against a
//! single-document loader. The channel is bounded so the producer cannot pull
//! the whole corpus into memory ahead of the workers. Tokio's worker threads
//! are a separate knob — they say how many cores encode and serve those N
//! in-flight bulks, not how many are outstanding.
use std::future::Future;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Instant;

use anyhow::{bail, Result};
use tokio::task::JoinSet;

use crate::bulk::BulkOutcome;
use crate::corpus::DocumentBatch;
use crate::notes::Notes;
use crate::report::{latency_text, percentile, PointResult};

pub const QUEUE_DEPTH_PER_WORKER: usize = 10;

pub trait Inserter: Send + Sync + 'static {
    /// `impl Future + Send` rather than `async fn`: the workers are spawned onto
    /// tokio, which only accepts `Send` futures, and `async fn` in a trait does
    /// not promise that.
    ///
    /// `Err` is a batch that failed whole — no socket, a timeout, an HTTP
    /// status. `Ok` carries the per-item verdict, because OpenSearch reports
    /// item failures inside a 200.
    fn insert(&self, batch: DocumentBatch) -> impl Future<Output = Result<BulkOutcome>> + Send;
}

pub trait Source: Iterator<Item = Result<DocumentBatch>> + Send + 'static {}
impl<T> Source for T where T: Iterator<Item = Result<DocumentBatch>> + Send + 'static {}

pub type OnPoint<'a> = &'a mut dyn FnMut(PointResult) -> Result<()>;

/// Documents and bulks are counted separately because they answer different
/// questions: `docs`/`errors` say how much of the corpus landed, `bulks` says
/// how many requests the latency distribution was drawn from.
#[derive(Debug, Default)]
pub struct Counters {
    pub docs: u64,
    pub doc_errors: u64,
    pub bulks: u64,
    pub failed_bulks: u64,
    pub latencies_ms: Vec<f64>,
    first_error: Option<(Instant, String)>,
}

impl Counters {
    /// Only a batch where every item landed contributes a latency sample: a
    /// half-rejected bulk returned early for a reason that is not the engine
    /// indexing faster.
    pub fn record_bulk(&mut self, offered: u64, outcome: &BulkOutcome, latency_ms: f64) {
        self.docs += offered - outcome.failed;
        self.doc_errors += outcome.failed;
        match &outcome.first_failure {
            None => {
                self.bulks += 1;
                self.latencies_ms.push(latency_ms);
            }
            Some(failure) => {
                self.failed_bulks += 1;
                self.remember_first_error(Instant::now(), failure.clone());
            }
        }
    }

    /// A batch that never came back delivered nothing, so every document it
    /// carried is an error rather than an unknown.
    pub fn record_failed_bulk(&mut self, offered: u64, exc: anyhow::Error) {
        self.doc_errors += offered;
        self.failed_bulks += 1;
        self.remember_first_error(Instant::now(), format!("{exc:#}"));
    }

    pub fn offered(&self) -> u64 {
        self.docs + self.doc_errors
    }

    pub fn first_error(&self) -> Option<&str> {
        self.first_error.as_ref().map(|(_, text)| text.as_str())
    }

    /// Workers count in parallel, so "first" is the earliest failure by clock,
    /// not the earliest one this process happened to join.
    pub fn merge(&mut self, other: Counters) {
        self.docs += other.docs;
        self.doc_errors += other.doc_errors;
        self.bulks += other.bulks;
        self.failed_bulks += other.failed_bulks;
        self.latencies_ms.extend(other.latencies_ms);
        if let Some((at, text)) = other.first_error {
            self.remember_first_error(at, text);
        }
    }

    fn remember_first_error(&mut self, at: Instant, text: String) {
        if self.first_error.as_ref().is_none_or(|(seen, _)| at < *seen) {
            self.first_error = Some((at, text));
        }
    }
}

/// Set from a Ctrl-C handler: a long ladder ends early often enough that the
/// levels already measured have to survive it.
#[derive(Clone, Default)]
pub struct Cancel {
    flag: Arc<std::sync::atomic::AtomicBool>,
    notify: Arc<tokio::sync::Notify>,
}

impl Cancel {
    pub fn trigger(&self) {
        self.flag.store(true, Ordering::SeqCst);
        self.notify.notify_waiters();
    }

    pub fn is_set(&self) -> bool {
        self.flag.load(Ordering::SeqCst)
    }

    pub async fn wait(&self) {
        loop {
            let notified = self.notify.notified();
            if self.is_set() {
                return;
            }
            notified.await;
            if self.is_set() {
                return;
            }
        }
    }
}

/// `on_point` is handed each result as it lands, so a level that fails cannot
/// take the levels already measured down with it.
pub async fn run_sweep<I, S, F>(
    inserter: Arc<I>,
    open_source: F,
    levels: &[usize],
    shape: Shape,
    notes: &Notes,
    cancel: &Cancel,
    on_point: OnPoint<'_>,
) -> Result<()>
where
    I: Inserter,
    S: Source,
    F: Fn() -> Result<S>,
{
    for (position, &concurrency) in levels.iter().enumerate() {
        notes.say(&announce_level(position, levels, shape.at(concurrency)));
        let result =
            measure_or_cancel(&inserter, open_source()?, concurrency, shape, notes, cancel).await?;
        announce(notes, &result);
        on_point(result)?;
    }
    Ok(())
}

/// The documents-in-flight product is said out loud, because `c=64` against a
/// single-document loader and `c=64 batch=512` are not the same offer.
fn announce_level(position: usize, levels: &[usize], point: Point) -> String {
    format!(
        "[{}/{}] concurrency={} batch={} ({} docs in flight)",
        position + 1,
        levels.len(),
        point.concurrency,
        point.batch_size,
        point.concurrency * point.batch_size
    )
}

/// The two knobs a point is measured at, carried together so neither can be
/// recorded without the other.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Point {
    pub concurrency: usize,
    pub batch_size: usize,
}

/// How this client offers work, held fixed across a ladder while `concurrency`
/// is what moves. `batch_size` is the X axis's other half and reaches every CSV
/// row; `queue_depth` is only how far the producer may run ahead, and reaches
/// the header.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Shape {
    pub batch_size: usize,
    pub queue_depth: usize,
}

impl Shape {
    pub fn at(&self, concurrency: usize) -> Point {
        Point {
            concurrency,
            batch_size: self.batch_size,
        }
    }

    /// In batches, not documents: `queue_depth * concurrency * batch_size`
    /// documents are buffered ahead of the workers, and at the campaign's
    /// `batch=512` a depth in the tens would hold the whole corpus.
    pub fn queue_capacity(&self, concurrency: usize) -> usize {
        (self.queue_depth * concurrency).max(1)
    }
}

async fn measure_or_cancel<I: Inserter, S: Source>(
    inserter: &Arc<I>,
    source: S,
    concurrency: usize,
    shape: Shape,
    notes: &Notes,
    cancel: &Cancel,
) -> Result<PointResult> {
    tokio::select! {
        biased;
        _ = cancel.wait() => bail!("interrupted at concurrency={concurrency}"),
        outcome = measure_at_concurrency(inserter, source, concurrency, shape, notes) => outcome,
    }
}

/// The workers live in a `JoinSet`, so abandoning this future — a Ctrl-C mid
/// level — aborts them instead of leaving them writing into a finished run.
pub async fn measure_at_concurrency<I: Inserter, S: Source>(
    inserter: &Arc<I>,
    source: S,
    concurrency: usize,
    shape: Shape,
    notes: &Notes,
) -> Result<PointResult> {
    let (sender, receiver) = async_channel::bounded(shape.queue_capacity(concurrency));
    let delivered = Arc::new(AtomicU64::new(0));
    let producer = tokio::task::spawn_blocking(move || fill_channel(sender, source));
    let mut workers = start_workers(inserter, &receiver, &delivered, concurrency);
    let mut reporter = start_progress(&delivered, concurrency, notes);
    drop(receiver);

    let started = Instant::now();
    let counters = collect_counters(&mut workers).await?;
    let wall_s = started.elapsed().as_secs_f64();
    reporter.abort_all();
    producer.await??;

    warn_about_errors(notes, &counters);
    Ok(summarize(shape.at(concurrency), &counters, wall_s))
}

/// A send that fails means every worker is gone, so the run this was feeding is
/// over and there is nothing left to read the rest of the corpus for.
fn fill_channel<S: Source>(sender: async_channel::Sender<DocumentBatch>, source: S) -> Result<()> {
    for batch in source {
        if sender.send_blocking(batch?).is_err() {
            return Ok(());
        }
    }
    Ok(())
}

fn start_workers<I: Inserter>(
    inserter: &Arc<I>,
    receiver: &async_channel::Receiver<DocumentBatch>,
    delivered: &Arc<AtomicU64>,
    concurrency: usize,
) -> JoinSet<Counters> {
    let mut workers = JoinSet::new();
    for _ in 0..concurrency {
        workers.spawn(drain_channel(
            Arc::clone(inserter),
            receiver.clone(),
            Arc::clone(delivered),
        ));
    }
    workers
}

async fn drain_channel<I: Inserter>(
    inserter: Arc<I>,
    receiver: async_channel::Receiver<DocumentBatch>,
    delivered: Arc<AtomicU64>,
) -> Counters {
    let mut counters = Counters::default();
    while let Ok(batch) = receiver.recv().await {
        let docs = batch.docs();
        insert_one(inserter.as_ref(), batch, &mut counters).await;
        delivered.fetch_add(docs, Ordering::Relaxed);
    }
    counters
}

async fn insert_one<I: Inserter>(inserter: &I, batch: DocumentBatch, counters: &mut Counters) {
    let offered = batch.docs();
    let started = Instant::now();
    let latency_ms = |started: Instant| started.elapsed().as_secs_f64() * 1000.0;
    match inserter.insert(batch).await {
        Ok(outcome) => counters.record_bulk(offered, &outcome, latency_ms(started)),
        Err(exc) => counters.record_failed_bulk(offered, exc),
    }
}

async fn collect_counters(workers: &mut JoinSet<Counters>) -> Result<Counters> {
    let mut merged = Counters::default();
    while let Some(finished) = workers.join_next().await {
        merged.merge(finished?);
    }
    Ok(merged)
}

fn start_progress(delivered: &Arc<AtomicU64>, concurrency: usize, notes: &Notes) -> JoinSet<()> {
    let mut reporter = JoinSet::new();
    reporter.spawn(follow_progress(
        Arc::clone(delivered),
        concurrency,
        notes.clone(),
    ));
    reporter
}

async fn follow_progress(delivered: Arc<AtomicU64>, concurrency: usize, notes: Notes) {
    let mut previous = 0;
    let mut ticker = tokio::time::interval(notes.progress_interval());
    ticker.tick().await;
    loop {
        ticker.tick().await;
        let done = delivered.load(Ordering::Relaxed);
        notes.say(&format!(
            "  c={concurrency} {} docs/s (total {done})",
            done - previous
        ));
        previous = done;
    }
}

fn warn_about_errors(notes: &Notes, counters: &Counters) {
    if let Some(first) = counters.first_error() {
        notes.say(&format!(
            "  !! {} failed bulks, {} undelivered documents, first failure was {first}",
            counters.failed_bulks, counters.doc_errors
        ));
    }
}

pub fn summarize(point: Point, counters: &Counters, wall_s: f64) -> PointResult {
    let mut latencies = counters.latencies_ms.clone();
    latencies.sort_by(f64::total_cmp);
    PointResult {
        concurrency: point.concurrency,
        batch_size: point.batch_size,
        docs: counters.docs,
        errors: counters.doc_errors,
        bulks: counters.bulks,
        failed_bulks: counters.failed_bulks,
        wall_s,
        docs_per_s: rate(counters.docs, wall_s),
        p50_ms: percentile(&latencies, 0.50),
        p99_ms: percentile(&latencies, 0.99),
    }
}

fn rate(docs: u64, wall_s: f64) -> f64 {
    if wall_s > 0.0 {
        docs as f64 / wall_s
    } else {
        0.0
    }
}

fn announce(notes: &Notes, result: &PointResult) {
    notes.say(&format!(
        "  -> {} docs in {} bulks in {:.2}s = {:.1} docs/s, p99 {} ms/bulk, {} undelivered docs",
        result.docs,
        result.bulks,
        result.wall_s,
        result.docs_per_s,
        latency_text(result.p99_ms),
        result.errors
    ));
}

#[cfg(test)]
#[path = "sweep_tests.rs"]
mod tests;
