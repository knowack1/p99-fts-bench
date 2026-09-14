//! One bounded channel, N worker tasks, one measurement point per concurrency
//! level.
//!
//! In-flight is exactly N requests: each worker takes one item of work and
//! awaits its reply before taking the next. What an item carries is the
//! engine's business — one document on a prepared INSERT, `batch_size` of them
//! in a `_bulk` — and the only thing this module asks of it is how many
//! documents it is worth. Documents in flight is therefore
//! `concurrency * batch_size`, which is the number to quote when comparing a
//! batching loader against a single-document one.
//!
//! The channel is bounded so the producer cannot pull the whole corpus into
//! memory ahead of the workers. Tokio's worker threads are a separate knob —
//! they say how many cores serve those N in-flight requests, not how many are
//! outstanding.
use std::future::Future;
use std::pin::Pin;
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::Instant;

use anyhow::{bail, Result};
use tokio::task::JoinSet;

use crate::build_rate::IndexWatch;
use crate::notes::Notes;
use crate::report::{latency_text, percentile, PointResult};
use crate::samples::{rate, SampleFiles, Submitted, Tape};

pub type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// What one request carries.
///
/// An associated type rather than one shared batch type, and that is a
/// measurement decision rather than a taste one: wrapping every ScyllaDB
/// document in a one-element `Vec` would put a `malloc`/`free` pair on the
/// hottest path in a harness whose whole purpose is to have a higher ceiling
/// than the engine it measures. The only thing core needs is `docs()`, which
/// monomorphizes to a constant where a request carries one document.
pub trait WorkItem: Send + 'static {
    fn docs(&self) -> u64;
}

/// What one request's reply said happened, in documents.
///
/// `Err` from `insert` is a request that failed whole — no socket, a timeout,
/// an HTTP status. `Ok` carries the per-item verdict, because OpenSearch
/// reports item failures inside a 200 and CQL does not.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Accepted {
    pub failed: u64,
    pub first_failure: Option<String>,
}

impl Accepted {
    pub const CLEAN: Self = Self {
        failed: 0,
        first_failure: None,
    };

    pub fn is_clean(&self) -> bool {
        self.failed == 0
    }
}

pub trait Inserter: Send + Sync + 'static {
    type Item: WorkItem;

    /// `impl Future + Send` rather than `async fn`: the workers are spawned onto
    /// tokio, which only accepts `Send` futures, and `async fn` in a trait does
    /// not promise that.
    ///
    /// Serialization belongs inside this call. The CQL half serializes its row
    /// inside `execute_unpaged`, so a request's latency there is also the
    /// client's cost of offering it; a bulk measured without its encode would be
    /// a request no client could actually have sent.
    fn insert(&self, item: Self::Item) -> impl Future<Output = Result<Accepted>> + Send;
}

pub trait Source: Iterator<Item = Result<Self::Work>> + Send + 'static {
    type Work: WorkItem;
}

impl<W: WorkItem, T> Source for T
where
    T: Iterator<Item = Result<W>> + Send + 'static,
{
    type Work = W;
}

/// One inserter per level, opened the way the corpus is reopened per level.
///
/// A trait rather than a closure because the real implementations reset the
/// index first, and on ScyllaDB the prepared statement cannot outlive that: the
/// table it was prepared against is dropped. An engine with nothing to rebuild
/// hands back the handle it already holds. Boxing the future costs one
/// allocation per level, which is not a quantity this tool measures.
pub trait LevelSource: Send + Sync {
    type Inserter: Inserter;

    fn open(&self) -> BoxFuture<'_, Result<Arc<Self::Inserter>>>;
}

/// The level needs nothing done to it first — `--no-reset`, and the tests that
/// are not about the reset.
pub struct SameInserter<I: Inserter>(pub Arc<I>);

impl<I: Inserter> LevelSource for SameInserter<I> {
    type Inserter = I;

    fn open(&self) -> BoxFuture<'_, Result<Arc<I>>> {
        Box::pin(std::future::ready(Ok(Arc::clone(&self.0))))
    }
}

pub type OnPoint<'a> = &'a mut dyn FnMut(PointResult) -> Result<()>;

/// The two knobs a point is measured at, carried together so neither can be
/// recorded without the other.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Point {
    pub concurrency: usize,
    pub batch_size: usize,
}

impl Point {
    pub fn docs_in_flight(&self) -> usize {
        self.concurrency * self.batch_size
    }
}

/// How this client offers work, held fixed across a ladder while `concurrency`
/// is what moves. `batch_size` is the X axis's other half and reaches every CSV
/// row; `queue_depth` is only how far the producer may run ahead.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Shape {
    pub batch_size: usize,
    pub queue_depth: usize,
}

/// Who is offering the work, and how. Carried together so a row cannot be
/// written without the engine that produced it — the column every consumer
/// tells the two halves apart by.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Loader {
    pub engine: &'static str,
    pub shape: Shape,
}

impl Loader {
    pub fn at(&self, concurrency: usize) -> Point {
        self.shape.at(concurrency)
    }
}

impl Shape {
    /// One document per request, buffered the way `scyllarate` has always
    /// buffered it.
    pub const ONE_DOCUMENT: Self = Self {
        batch_size: 1,
        queue_depth: QUEUE_DEPTH_PER_WORKER,
    };

    pub fn at(&self, concurrency: usize) -> Point {
        Point {
            concurrency,
            batch_size: self.batch_size,
        }
    }

    /// In requests, not documents: at `batch=512` a depth in the tens would hold
    /// the whole corpus.
    pub fn queue_capacity(&self, concurrency: usize) -> usize {
        (self.queue_depth * concurrency).max(1)
    }
}

pub const QUEUE_DEPTH_PER_WORKER: usize = 10;

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

/// Documents and requests are counted separately because they answer different
/// questions: `docs`/`errors` say how much of the corpus landed, `requests`
/// says how many replies the latency distribution was drawn from. At one
/// document per request the two coincide, which is what makes `docs / requests`
/// the effective batch size a reader can check the column against.
#[derive(Debug, Default)]
pub struct Counters {
    pub docs: u64,
    pub errors: u64,
    pub requests: u64,
    pub failed_requests: u64,
    pub latencies_ms: Vec<f64>,
    first_error: Option<(Instant, String)>,
}

impl Counters {
    /// Only a request where every item landed contributes a latency sample: a
    /// half-rejected reply came back early for a reason that is not the engine
    /// indexing faster.
    pub fn record(&mut self, offered: u64, accepted: &Accepted, latency_ms: f64) {
        self.docs += offered.saturating_sub(accepted.failed);
        self.errors += accepted.failed;
        match &accepted.first_failure {
            None => {
                self.requests += 1;
                self.latencies_ms.push(latency_ms);
            }
            Some(failure) => {
                self.failed_requests += 1;
                self.remember_first_error(Instant::now(), failure.clone());
            }
        }
    }

    /// A request that never came back delivered nothing, so every document it
    /// carried is an error rather than an unknown.
    pub fn record_failure(&mut self, offered: u64, exc: anyhow::Error) {
        self.errors += offered;
        self.failed_requests += 1;
        self.remember_first_error(Instant::now(), format!("{exc:#}"));
    }

    pub fn offered(&self) -> u64 {
        self.docs + self.errors
    }

    pub fn first_error(&self) -> Option<&str> {
        self.first_error.as_ref().map(|(_, text)| text.as_str())
    }

    /// Workers count in parallel, so "first" is the earliest failure by clock,
    /// not the earliest one this process happened to join.
    pub fn merge(&mut self, other: Counters) {
        self.docs += other.docs;
        self.errors += other.errors;
        self.requests += other.requests;
        self.failed_requests += other.failed_requests;
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
    loader: Loader,
    watchers: &Watchers<'_>,
    cancel: &Cancel,
    on_point: OnPoint<'_>,
) -> Result<()>
where
    P: LevelSource,
    S: Source<Work = ItemOf<P>>,
    F: Fn() -> Result<S>,
{
    let notes = watchers.notes;
    for (position, &concurrency) in levels.iter().enumerate() {
        notes.say(&announce_level(position, levels, loader.at(concurrency)));
        let inserter = inserters.open().await?;
        let result = run_level(
            &inserter,
            open_source()?,
            loader,
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

/// The item type a `LevelSource` ultimately produces work for.
pub type ItemOf<P> = <<P as LevelSource>::Inserter as Inserter>::Item;

/// The documents-in-flight product is said out loud where a request carries
/// more than one document, because `c=64` and `c=64 batch=512` are not the same
/// offer.
///
/// Silent at one document per request — not only because the clause would say
/// nothing, but because `run-arm.sh` greps stderr for `docs in` to pull each
/// level's result line, and an announcement carrying that phrase would return
/// two lines per level where it expects one.
fn announce_level(position: usize, levels: &[usize], point: Point) -> String {
    let ladder = format!(
        "[{}/{}] concurrency={}",
        position + 1,
        levels.len(),
        point.concurrency
    );
    if point.batch_size == 1 {
        return ladder;
    }
    format!(
        "{ladder} batch={} ({} docs in flight)",
        point.batch_size,
        point.docs_in_flight()
    )
}

/// The tape is opened before the watch begins, so `t_s` zero is the start of
/// the level rather than the first insert: the index was already moving by
/// then, and a series that started later would credit that work to nobody.
async fn run_level<I: Inserter, S: Source<Work = I::Item>>(
    inserter: &Arc<I>,
    source: S,
    loader: Loader,
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
        loader,
        concurrency,
        watchers.notes,
        &submitted,
        cancel,
    )
    .await;
    watch.client_stopped();
    let mut result = close_submit_series(outcome, &tape, &submitted)?;
    result.index = watch.finish(result.docs).await?;
    Ok(result)
}

async fn measure_or_cancel<I: Inserter, S: Source<Work = I::Item>>(
    inserter: &Arc<I>,
    source: S,
    loader: Loader,
    concurrency: usize,
    notes: &Notes,
    submitted: &Arc<Submitted>,
    cancel: &Cancel,
) -> Result<PointResult> {
    let measure = measure_at_concurrency(inserter, source, loader, concurrency, notes, submitted);
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
pub async fn measure_at_concurrency<I: Inserter, S: Source<Work = I::Item>>(
    inserter: &Arc<I>,
    source: S,
    loader: Loader,
    concurrency: usize,
    notes: &Notes,
    submitted: &Arc<Submitted>,
) -> Result<PointResult> {
    let (sender, receiver) = async_channel::bounded(loader.shape.queue_capacity(concurrency));
    let producer = tokio::task::spawn_blocking(move || fill_channel(sender, source));
    let mut workers = start_workers(inserter, &receiver, submitted, concurrency);
    drop(receiver);

    let started = Instant::now();
    let counters = collect_counters(&mut workers).await?;
    let wall_s = started.elapsed().as_secs_f64();
    producer.await??;

    warn_about_errors(notes, &counters);
    Ok(summarize(loader, concurrency, &counters, wall_s))
}

/// A send that fails means every worker is gone, so the run this was feeding is
/// over and there is nothing left to read the rest of the corpus for.
fn fill_channel<S: Source>(sender: async_channel::Sender<S::Work>, source: S) -> Result<()> {
    for item in source {
        if sender.send_blocking(item?).is_err() {
            return Ok(());
        }
    }
    Ok(())
}

fn start_workers<I: Inserter>(
    inserter: &Arc<I>,
    receiver: &async_channel::Receiver<I::Item>,
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
    receiver: async_channel::Receiver<I::Item>,
    submitted: Arc<Submitted>,
) -> Counters {
    let mut counters = Counters::default();
    while let Ok(item) = receiver.recv().await {
        insert_one(inserter.as_ref(), item, &mut counters, &submitted).await;
    }
    counters
}

/// The submit series counts documents the engine accepted, because the curve
/// beside the index build has to be the work the index will actually have to
/// do: a failed insert never reaches the index, and counting it would read as
/// lag.
async fn insert_one<I: Inserter>(
    inserter: &I,
    item: I::Item,
    counters: &mut Counters,
    submitted: &Submitted,
) {
    let offered = item.docs();
    let started = Instant::now();
    match inserter.insert(item).await {
        Ok(accepted) => {
            let landed = offered.saturating_sub(accepted.failed);
            counters.record(offered, &accepted, started.elapsed().as_secs_f64() * 1000.0);
            submitted.record_many(landed, accepted.failed);
        }
        Err(exc) => {
            counters.record_failure(offered, exc);
            submitted.record_many(0, offered);
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
            "  !! {} failed requests, first was {first}",
            counters.failed_requests
        ));
    }
}

pub fn summarize(
    loader: Loader,
    concurrency: usize,
    counters: &Counters,
    wall_s: f64,
) -> PointResult {
    let mut latencies = counters.latencies_ms.clone();
    latencies.sort_by(f64::total_cmp);
    PointResult {
        engine: loader.engine,
        concurrency,
        batch_size: loader.shape.batch_size,
        docs: counters.docs,
        errors: counters.errors,
        requests: counters.requests,
        failed_requests: counters.failed_requests,
        wall_s,
        docs_per_s: rate(counters.docs, wall_s),
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
fn announce_build(build: &crate::report::IndexBuild) -> String {
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
