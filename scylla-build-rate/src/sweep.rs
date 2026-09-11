//! One bounded channel, N worker tasks, one measurement point per concurrency
//! level.
//!
//! In-flight is exactly N: each worker takes one document and awaits its INSERT
//! before taking the next. The channel is bounded so the producer cannot pull the
//! whole corpus into memory ahead of the workers. Tokio's worker threads are a
//! separate knob — they say how many cores serve those N in-flight requests, not
//! how many requests are outstanding.
use std::future::Future;
use std::pin::Pin;
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::Instant;

use anyhow::{bail, Result};
use tokio::task::JoinSet;

use crate::build_rate::{IndexBuild, IndexWatch};
use crate::corpus::InsertParams;
use crate::notes::Notes;
use crate::report::{latency_text, percentile, PointResult};
use crate::samples::{rate, SampleFiles, Submitted, Tape};

pub const QUEUE_DEPTH_PER_WORKER: usize = 10;

pub trait Inserter: Send + Sync + 'static {
    /// `impl Future + Send` rather than `async fn`: the workers are spawned onto
    /// tokio, which only accepts `Send` futures, and `async fn` in a trait does
    /// not promise that.
    fn insert(&self, params: InsertParams) -> impl Future<Output = Result<()>> + Send;
}

pub trait Source: Iterator<Item = Result<InsertParams>> + Send + 'static {}
impl<T> Source for T where T: Iterator<Item = Result<InsertParams>> + Send + 'static {}

pub type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// One inserter per level, opened the way the corpus is reopened per level.
///
/// It is a trait rather than a closure because the real implementation resets
/// the keyspace first, and the prepared statement it returns cannot outlive
/// that: the table it was prepared against is dropped. Boxing the future costs
/// one allocation per level, which is not a quantity this tool measures.
pub trait InserterSource: Send + Sync {
    type Inserter: Inserter;

    fn open(&self) -> BoxFuture<'_, Result<Arc<Self::Inserter>>>;
}

pub type OnPoint<'a> = &'a mut dyn FnMut(PointResult) -> Result<()>;

/// What watches a level while it runs. A bundle rather than three more
/// parameters: `run_sweep` was already at the count clippy refuses to pass.
pub struct Watchers<'a> {
    pub index: &'a IndexWatch,
    pub notes: &'a Notes,
    pub samples: Option<&'a SampleFiles>,
}

impl Watchers<'_> {
    /// A level that cannot open its series file is not a level that should run:
    /// the operator asked for the samples, and a sweep that silently drops them
    /// costs the whole ladder to find out.
    fn tape(&self, level: usize, concurrency: usize) -> Result<Tape> {
        let sink = match self.samples {
            Some(files) => Some(files.open_level(concurrency)?),
            None => None,
        };
        Ok(Tape::new(level, concurrency, sink))
    }
}

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
pub async fn run_sweep<P, S, F>(
    inserters: &P,
    open_source: F,
    levels: &[usize],
    watchers: &Watchers<'_>,
    cancel: &Cancel,
    on_point: OnPoint<'_>,
) -> Result<()>
where
    P: InserterSource,
    S: Source,
    F: Fn() -> Result<S>,
{
    let notes = watchers.notes;
    for (position, &concurrency) in levels.iter().enumerate() {
        notes.say(&format!(
            "[{}/{}] concurrency={concurrency}",
            position + 1,
            levels.len()
        ));
        let inserter = inserters.open().await?;
        let result = run_level(
            &inserter,
            open_source()?,
            concurrency,
            position,
            watchers,
            cancel,
        )
        .await?;
        announce(notes, &result);
        on_point(result)?;
    }
    Ok(())
}

/// The tape is opened before the watch begins, so `t_s` zero is the start of
/// the level rather than the first insert: the index was already moving by
/// then, and a series that started later would credit that work to nobody.
async fn run_level<I: Inserter, S: Source>(
    inserter: &Arc<I>,
    source: S,
    concurrency: usize,
    position: usize,
    watchers: &Watchers<'_>,
    cancel: &Cancel,
) -> Result<PointResult> {
    let tape = watchers.tape(position + 1, concurrency)?;
    let submitted = Arc::new(Submitted::default());
    let mut watch = watchers
        .index
        .begin(watchers.notes, &tape, &submitted)
        .await?;
    let outcome = measure_or_cancel(
        inserter,
        source,
        concurrency,
        watchers.notes,
        &submitted,
        cancel,
    )
    .await;
    watch.client_stopped();
    let mut point = close_submit_series(outcome, &tape, &submitted)?;
    point.index = watch.finish(point.docs).await?;
    Ok(point)
}

async fn measure_or_cancel<I: Inserter, S: Source>(
    inserter: &Arc<I>,
    source: S,
    concurrency: usize,
    notes: &Notes,
    submitted: &Arc<Submitted>,
    cancel: &Cancel,
) -> Result<PointResult> {
    let measure = measure_at_concurrency(inserter, source, concurrency, notes, submitted);
    tokio::select! {
        biased;
        _ = cancel.wait() => bail!("interrupted at concurrency={concurrency}"),
        outcome = measure => outcome,
    }
}

/// The series ends at the count the point reports rather than at the last whole
/// second, so the last sample and the CSV row agree about what was submitted.
fn close_submit_series(
    outcome: Result<PointResult>,
    tape: &Tape,
    submitted: &Arc<Submitted>,
) -> Result<PointResult> {
    tape.record(submitted.ok(), None);
    outcome
}

/// The workers live in a `JoinSet`, so abandoning this future — a Ctrl-C mid
/// level — aborts them instead of leaving them writing into a finished run.
pub async fn measure_at_concurrency<I: Inserter, S: Source>(
    inserter: &Arc<I>,
    source: S,
    concurrency: usize,
    notes: &Notes,
    submitted: &Arc<Submitted>,
) -> Result<PointResult> {
    let (sender, receiver) = async_channel::bounded(QUEUE_DEPTH_PER_WORKER * concurrency);
    let producer = tokio::task::spawn_blocking(move || fill_channel(sender, source));
    let mut workers = start_workers(inserter, &receiver, submitted, concurrency);
    drop(receiver);

    let started = Instant::now();
    let counters = collect_counters(&mut workers).await?;
    let wall_s = started.elapsed().as_secs_f64();
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
    submitted: &Arc<Submitted>,
    concurrency: usize,
) -> JoinSet<Counters> {
    let mut workers = JoinSet::new();
    for _ in 0..concurrency {
        workers.spawn(drain_channel(
            Arc::clone(inserter),
            receiver.clone(),
            Arc::clone(submitted),
        ));
    }
    workers
}

async fn drain_channel<I: Inserter>(
    inserter: Arc<I>,
    receiver: async_channel::Receiver<InsertParams>,
    submitted: Arc<Submitted>,
) -> Counters {
    let mut counters = Counters::default();
    while let Ok(params) = receiver.recv().await {
        submitted.record(insert_one(inserter.as_ref(), params, &mut counters).await);
    }
    counters
}

/// `true` when ScyllaDB accepted it. The submit series counts accepted inserts
/// because the curve beside the index build has to be the work the index will
/// actually have to do: a failed insert never reaches the index, and counting
/// it would read as lag.
async fn insert_one<I: Inserter>(
    inserter: &I,
    params: InsertParams,
    counters: &mut Counters,
) -> bool {
    let started = Instant::now();
    match inserter.insert(params).await {
        Ok(()) => {
            counters.record_ok(started.elapsed().as_secs_f64() * 1000.0);
            true
        }
        Err(exc) => {
            counters.record_error(exc);
            false
        }
    }
}

async fn collect_counters(workers: &mut JoinSet<Counters>) -> Result<Counters> {
    let mut merged = Counters::default();
    while let Some(finished) = workers.join_next().await {
        merged.merge(finished?);
    }
    Ok(merged)
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
        index: None,
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
    if let Some(build) = result.index.as_ref() {
        notes.say(&announce_build(build));
    }
}

/// An unsettled build is said out loud rather than left to the CSV: it means
/// the index never caught up with what was submitted, so the rate beside it is
/// a floor and not the build rate.
fn announce_build(build: &IndexBuild) -> String {
    let caught_up = if build.settled {
        format!("settled in {:.1}s", build.settle_s)
    } else {
        format!(
            "NOT settled after {:.1}s, {} docs short",
            build.settle_s, build.lag_docs
        )
    };
    format!(
        "  -> index {} docs = {:.1} docs/s, {} behind at submit end, {caught_up}",
        build.docs, build.docs_per_s, build.lag_docs
    )
}

#[cfg(test)]
#[path = "sweep_tests.rs"]
mod tests;
