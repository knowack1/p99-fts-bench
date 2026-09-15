//! One cell of the matrix: N requests in flight against a finished index.
//!
//! Closed loop, and that is the convention the whole tree is built on — each
//! worker sends the next query the moment the previous one answers, so latency
//! *is* service time and throughput is what the engine gave back rather than
//! what a generator offered. There is no queue between the workers and the
//! engine, deliberately: a work queue whose latency clock starts at enqueue
//! bills the client's own backlog to the engine, which is exactly how this
//! bench's Python predecessor once measured p50=79 ms at c=64 where Little's
//! law put it near 16 ms.
//!
//! Coordinated omission does not apply to what this reports, because nothing is
//! offered on a schedule. `queries_per_s` is completed over wall, and `p99_ms`
//! is the service time at that concurrency. Both are only meaningful next to
//! the concurrency they were taken at, which is why it is a column and not a
//! footnote.
use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::{bail, Result};
use build_rate_core::sweep::Cancel;
use tokio::task::JoinSet;

use crate::queries::{QueryClass, Rotation};
use crate::report::{CellResult, Shape};
use crate::search::{Found, Searcher};

/// How long a cell asks for, and how long it asks before it starts counting.
///
/// The warm-up is not politeness: the first queries of a class pay for page
/// cache, JIT, connection setup and an empty query cache, and a 20-second cell
/// that included them would report the cost of starting rather than the cost of
/// searching.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct CellSettings {
    pub warmup: Duration,
    pub duration: Duration,
}

/// What the workers of one cell counted, merged.
#[derive(Debug, Default)]
pub struct Counters {
    pub queries: u64,
    pub errors: u64,
    pub hits: u64,
    pub zero_hit_queries: u64,
    pub latencies_ms: Vec<f64>,
    first_error: Option<(Instant, String)>,
}

impl Counters {
    /// A search that answered contributes its latency whatever it found: zero
    /// hits is a real answer and a real cost. It is counted separately instead,
    /// because a class that matches nothing is timing an empty result set and
    /// the cell has to be able to say so.
    pub fn record(&mut self, found: Found, latency_ms: f64) {
        self.queries += 1;
        self.hits += found.hits as u64;
        if found.is_empty() {
            self.zero_hit_queries += 1;
        }
        self.latencies_ms.push(latency_ms);
    }

    /// A request that failed contributes no latency. Its time was spent on
    /// whatever went wrong, and putting that in the distribution would move the
    /// tail by an amount that has nothing to do with search.
    pub fn record_failure(&mut self, exc: anyhow::Error) {
        self.errors += 1;
        self.remember_first_error(Instant::now(), format!("{exc:#}"));
    }

    pub fn first_error(&self) -> Option<&str> {
        self.first_error.as_ref().map(|(_, text)| text.as_str())
    }

    /// Workers count in parallel, so "first" is the earliest failure by clock,
    /// not the earliest one this process happened to join.
    pub fn merge(&mut self, other: Counters) {
        self.queries += other.queries;
        self.errors += other.errors;
        self.hits += other.hits;
        self.zero_hit_queries += other.zero_hit_queries;
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

/// One cell's measured window, before it is turned into a row.
#[derive(Debug)]
pub struct Measured {
    pub counters: Counters,
    pub wall_s: f64,
}

impl Measured {
    /// Sorted once, because the percentiles and the optional per-cell dump are
    /// the same values seen twice.
    pub fn into_report(
        self,
        shape: &Shape,
        class: &QueryClass,
        concurrency: usize,
    ) -> (CellResult, Vec<f64>) {
        let mut latencies = self.counters.latencies_ms;
        latencies.sort_by(f64::total_cmp);
        let result = CellResult::new(
            shape,
            concurrency,
            class,
            &Tally {
                queries: self.counters.queries,
                errors: self.counters.errors,
                hits: self.counters.hits,
                zero_hit_queries: self.counters.zero_hit_queries,
                wall_s: self.wall_s,
            },
            &latencies,
        );
        (result, latencies)
    }
}

/// The counts a row is built from, apart from the latency distribution.
#[derive(Debug, Clone, Copy)]
pub struct Tally {
    pub queries: u64,
    pub errors: u64,
    pub hits: u64,
    pub zero_hit_queries: u64,
    pub wall_s: f64,
}

/// Warm up, then measure. The warm-up drives the same class through the same
/// loop and its counters are dropped on the floor.
pub async fn measure_cell(
    searcher: &Arc<dyn Searcher>,
    class: &QueryClass,
    concurrency: usize,
    settings: &CellSettings,
    cancel: &Cancel,
) -> Result<Measured> {
    if !settings.warmup.is_zero() {
        drive_or_cancel(searcher, class, concurrency, settings.warmup, cancel).await?;
    }
    let started = Instant::now();
    let counters = drive_or_cancel(searcher, class, concurrency, settings.duration, cancel).await?;
    Ok(Measured {
        counters,
        wall_s: started.elapsed().as_secs_f64(),
    })
}

async fn drive_or_cancel(
    searcher: &Arc<dyn Searcher>,
    class: &QueryClass,
    concurrency: usize,
    window: Duration,
    cancel: &Cancel,
) -> Result<Counters> {
    tokio::select! {
        biased;
        _ = cancel.wait() => bail!(
            "interrupted at concurrency={concurrency} class={}", class.name()
        ),
        counters = drive(searcher, class, concurrency, window) => counters,
    }
}

/// The workers live in a `JoinSet`, so abandoning this future — a Ctrl-C mid
/// cell — aborts them instead of leaving them querying into a finished run.
async fn drive(
    searcher: &Arc<dyn Searcher>,
    class: &QueryClass,
    concurrency: usize,
    window: Duration,
) -> Result<Counters> {
    let rotation = Arc::new(class.rotation());
    let deadline = Instant::now() + window;
    let mut workers = JoinSet::new();
    for _ in 0..concurrency {
        workers.spawn(ask_until(
            Arc::clone(searcher),
            Arc::clone(&rotation),
            deadline,
        ));
    }
    collect(&mut workers).await
}

/// A query already in flight at the deadline is awaited rather than abandoned,
/// and the wall clock it lands in is the real one: a cell's throughput is
/// completed over elapsed, so a truncated request would cost the cell its own
/// last reply and count the time anyway.
async fn ask_until(
    searcher: Arc<dyn Searcher>,
    rotation: Arc<Rotation>,
    deadline: Instant,
) -> Counters {
    let mut counters = Counters::default();
    while Instant::now() < deadline {
        ask_once(searcher.as_ref(), rotation.next(), &mut counters).await;
    }
    counters
}

async fn ask_once(searcher: &dyn Searcher, query: &str, counters: &mut Counters) {
    let started = Instant::now();
    match searcher.search(query).await {
        Ok(found) => counters.record(found, started.elapsed().as_secs_f64() * 1000.0),
        Err(exc) => counters.record_failure(exc),
    }
}

async fn collect(workers: &mut JoinSet<Counters>) -> Result<Counters> {
    let mut merged = Counters::default();
    while let Some(finished) = workers.join_next().await {
        merged.merge(finished?);
    }
    Ok(merged)
}

#[cfg(test)]
#[path = "cell_tests.rs"]
mod tests;
