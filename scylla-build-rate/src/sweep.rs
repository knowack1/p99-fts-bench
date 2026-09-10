//! One bounded channel, N worker tasks, one measurement point per concurrency
//! level.
//!
//! In-flight is exactly N: each worker takes one document and awaits its INSERT
//! before taking the next. The channel is bounded so the producer cannot pull the
//! whole corpus into memory ahead of the workers. Tokio's worker threads are a
//! separate knob — they say how many cores serve those N in-flight requests, not
//! how many requests are outstanding.
use std::future::Future;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Instant;

use anyhow::{bail, Result};
use tokio::task::JoinSet;

use crate::corpus::InsertParams;
use crate::notes::Notes;
use crate::report::{latency_text, percentile, PointResult};

pub const QUEUE_DEPTH_PER_WORKER: usize = 10;

pub trait Inserter: Send + Sync + 'static {
    /// `impl Future + Send` rather than `async fn`: the workers are spawned onto
    /// tokio, which only accepts `Send` futures, and `async fn` in a trait does
    /// not promise that.
    fn insert(&self, params: InsertParams) -> impl Future<Output = Result<()>> + Send;
}

pub trait Source: Iterator<Item = Result<InsertParams>> + Send + 'static {}
impl<T> Source for T where T: Iterator<Item = Result<InsertParams>> + Send + 'static {}

pub type OnPoint<'a> = &'a mut dyn FnMut(PointResult) -> Result<()>;

#[derive(Debug, Default)]
pub struct Counters {
    pub ok: u64,
    pub errors: u64,
    pub latencies_ms: Vec<f64>,
    first_error: Option<(Instant, String)>,
}

impl Counters {
    pub fn record_ok(&mut self, latency_ms: f64) {
        self.ok += 1;
        self.latencies_ms.push(latency_ms);
    }

    pub fn record_error(&mut self, exc: anyhow::Error) {
        self.errors += 1;
        self.remember_first_error(Instant::now(), format!("{exc:#}"));
    }

    pub fn done(&self) -> u64 {
        self.ok + self.errors
    }

    pub fn first_error(&self) -> Option<&str> {
        self.first_error.as_ref().map(|(_, text)| text.as_str())
    }

    /// Workers count in parallel, so "first" is the earliest failure by clock,
    /// not the earliest one this process happened to join.
    pub fn merge(&mut self, other: Counters) {
        self.ok += other.ok;
        self.errors += other.errors;
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
        notes.say(&format!(
            "[{}/{}] concurrency={concurrency}",
            position + 1,
            levels.len()
        ));
        let result =
            measure_or_cancel(&inserter, open_source()?, concurrency, notes, cancel).await?;
        announce(notes, &result);
        on_point(result)?;
    }
    Ok(())
}

async fn measure_or_cancel<I: Inserter, S: Source>(
    inserter: &Arc<I>,
    source: S,
    concurrency: usize,
    notes: &Notes,
    cancel: &Cancel,
) -> Result<PointResult> {
    tokio::select! {
        biased;
        _ = cancel.wait() => bail!("interrupted at concurrency={concurrency}"),
        outcome = measure_at_concurrency(inserter, source, concurrency, notes) => outcome,
    }
}

/// The workers live in a `JoinSet`, so abandoning this future — a Ctrl-C mid
/// level — aborts them instead of leaving them writing into a finished run.
pub async fn measure_at_concurrency<I: Inserter, S: Source>(
    inserter: &Arc<I>,
    source: S,
    concurrency: usize,
    notes: &Notes,
) -> Result<PointResult> {
    let (sender, receiver) = async_channel::bounded(QUEUE_DEPTH_PER_WORKER * concurrency);
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
    Ok(summarize(concurrency, &counters, wall_s))
}

/// A send that fails means every worker is gone, so the run this was feeding is
/// over and there is nothing left to read the rest of the corpus for.
fn fill_channel<S: Source>(sender: async_channel::Sender<InsertParams>, source: S) -> Result<()> {
    for params in source {
        if sender.send_blocking(params?).is_err() {
            return Ok(());
        }
    }
    Ok(())
}

fn start_workers<I: Inserter>(
    inserter: &Arc<I>,
    receiver: &async_channel::Receiver<InsertParams>,
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
    receiver: async_channel::Receiver<InsertParams>,
    delivered: Arc<AtomicU64>,
) -> Counters {
    let mut counters = Counters::default();
    while let Ok(params) = receiver.recv().await {
        insert_one(inserter.as_ref(), params, &mut counters).await;
        delivered.fetch_add(1, Ordering::Relaxed);
    }
    counters
}

async fn insert_one<I: Inserter>(inserter: &I, params: InsertParams, counters: &mut Counters) {
    let started = Instant::now();
    match inserter.insert(params).await {
        Ok(()) => counters.record_ok(started.elapsed().as_secs_f64() * 1000.0),
        Err(exc) => counters.record_error(exc),
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
            "  !! {} failed inserts, first was {first}",
            counters.errors
        ));
    }
}

pub fn summarize(concurrency: usize, counters: &Counters, wall_s: f64) -> PointResult {
    let mut latencies = counters.latencies_ms.clone();
    latencies.sort_by(f64::total_cmp);
    PointResult {
        concurrency,
        docs: counters.ok,
        errors: counters.errors,
        wall_s,
        docs_per_s: rate(counters.ok, wall_s),
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
        "  -> {} docs in {:.2}s = {:.1} docs/s, p99 {} ms, {} errors",
        result.docs,
        result.wall_s,
        result.docs_per_s,
        latency_text(result.p99_ms),
        result.errors
    ));
}

#[cfg(test)]
#[path = "sweep_tests.rs"]
mod tests;
