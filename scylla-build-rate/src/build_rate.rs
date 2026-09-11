//! The index build rate beside the submit rate, per concurrency level.
//!
//! `docs_per_s` in the CSV is how fast this client handed prepared INSERTs to
//! ScyllaDB. That is not how fast documents reached the full-text index: rows
//! land in the base table first and the vector-store catches up behind them.
//! This watches the index while a level runs, and keeps watching after the last
//! insert until the index stops moving — the build is not over when the client
//! stops talking.
//!
//! Against the accept-and-discard sink the two numbers coincide, because the
//! sink indexes at the speed it accepts. That is the point: it makes the
//! build-rate figure's client ceiling measurable with the same reading the
//! engine will be measured with.
use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::{bail, Result};
use tokio::task::JoinSet;

use crate::notes::Notes;
use crate::vstore::{IndexProbe, IndexState};

#[derive(Debug, Clone)]
pub struct WatchTiming {
    pub poll_interval: Duration,
    pub settle_timeout: Duration,
    pub idle_timeout: Duration,
}

/// What the index did during one level.
#[derive(Debug, Clone, PartialEq)]
pub struct IndexBuild {
    pub docs: u64,
    pub docs_per_s: f64,
    pub lag_docs: u64,
    pub settle_s: f64,
    pub settled: bool,
    pub status: String,
}

/// The vector-store, or nothing at all when `--no-index-watch` was given.
pub struct IndexWatch {
    watcher: Option<Watcher>,
}

struct Watcher {
    probe: Arc<IndexProbe>,
    timing: WatchTiming,
}

impl IndexWatch {
    pub fn off() -> Self {
        Self { watcher: None }
    }

    pub fn on(probe: Arc<IndexProbe>, timing: WatchTiming) -> Self {
        Self {
            watcher: Some(Watcher { probe, timing }),
        }
    }

    pub fn is_on(&self) -> bool {
        self.watcher.is_some()
    }

    pub async fn begin(&self, notes: &Notes) -> Result<LevelWatch> {
        let Some(watcher) = self.watcher.as_ref() else {
            return Ok(LevelWatch { level: None });
        };
        let before = watcher.probe.status().await?.count();
        Ok(LevelWatch {
            level: Some(Level {
                probe: Arc::clone(&watcher.probe),
                timing: watcher.timing.clone(),
                notes: notes.clone(),
                before,
                started: Instant::now(),
                ticker: follow_index(&watcher.probe, &watcher.timing, notes, before),
            }),
        })
    }
}

/// One level's watch, from the first insert to the moment the index settles.
pub struct LevelWatch {
    level: Option<Level>,
}

struct Level {
    probe: Arc<IndexProbe>,
    timing: WatchTiming,
    notes: Notes,
    before: u64,
    started: Instant,
    ticker: JoinSet<()>,
}

impl LevelWatch {
    pub async fn finish(self, submitted: u64) -> Result<Option<IndexBuild>> {
        let Some(mut level) = self.level else {
            return Ok(None);
        };
        level.ticker.abort_all();
        Ok(Some(level.settle(submitted).await?))
    }
}

impl Level {
    async fn settle(&self, submitted: u64) -> Result<IndexBuild> {
        let target = self.before + submitted;
        let settling_from = Instant::now();
        let mut seen = Progress::new(settling_from);

        loop {
            self.sample_into(&mut seen).await;
            if seen.reached(target) || self.done_waiting(&seen) {
                break;
            }
            tokio::time::sleep(self.timing.poll_interval).await;
        }
        let Some(state) = seen.last else {
            bail!(
                "the index was never readable at {} during this level, so its \
                 build rate cannot be reported",
                self.probe.status_url()
            );
        };
        Ok(self.summarize(&state, target, seen.first_count, settling_from))
    }

    async fn sample_into(&self, seen: &mut Progress) {
        match self.probe.status().await {
            Ok(state) => seen.record(state),
            Err(exc) => self.notes.say(&format!("  !! index poll failed: {exc:#}")),
        }
    }

    /// Two ways to stop short of the target: the index stopped moving, or the
    /// whole settle budget ran out. Both leave `settled` false, which is what
    /// says the reported rate is a floor rather than a build.
    fn done_waiting(&self, seen: &Progress) -> bool {
        seen.idle_for() >= self.timing.idle_timeout || seen.elapsed() >= self.timing.settle_timeout
    }

    /// The rate spans the whole build — from the first insert to the moment the
    /// index stopped — not just the part the client was talking for. A build
    /// that keeps going for a minute after the loader finishes did not run at
    /// the loader's rate.
    fn summarize(
        &self,
        state: &IndexState,
        target: u64,
        at_submit_end: u64,
        settling_from: Instant,
    ) -> IndexBuild {
        let docs = state.count().saturating_sub(self.before);
        let wall_s = self.started.elapsed().as_secs_f64();
        IndexBuild {
            docs,
            docs_per_s: rate(docs, wall_s),
            lag_docs: target.saturating_sub(at_submit_end),
            settle_s: settling_from.elapsed().as_secs_f64(),
            settled: state.count() >= target,
            status: status_of(state),
        }
    }
}

/// What the polls have seen so far, and when the count last changed.
struct Progress {
    last: Option<IndexState>,
    first_count: u64,
    count: u64,
    since: Instant,
    moved_at: Instant,
}

impl Progress {
    fn new(started: Instant) -> Self {
        Self {
            last: None,
            first_count: 0,
            count: 0,
            since: started,
            moved_at: started,
        }
    }

    fn record(&mut self, state: IndexState) {
        let count = state.count();
        if self.last.is_none() {
            self.first_count = count;
        }
        if count != self.count {
            self.moved_at = Instant::now();
        }
        self.count = count;
        self.last = Some(state);
    }

    fn reached(&self, target: u64) -> bool {
        self.last.is_some() && self.count >= target
    }

    fn idle_for(&self) -> Duration {
        self.moved_at.elapsed()
    }

    fn elapsed(&self) -> Duration {
        self.since.elapsed()
    }
}

fn status_of(state: &IndexState) -> String {
    match state {
        IndexState::Absent => "absent".to_string(),
        IndexState::Present(status) => status.status.clone(),
    }
}

fn rate(docs: u64, wall_s: f64) -> f64 {
    if wall_s > 0.0 {
        docs as f64 / wall_s
    } else {
        0.0
    }
}

/// A line of its own, beside the submit-rate line: the two rates are the whole
/// point, and a level that is submitting fast while the index crawls has to be
/// visible while it happens rather than only in the CSV afterwards.
fn follow_index(
    probe: &Arc<IndexProbe>,
    timing: &WatchTiming,
    notes: &Notes,
    before: u64,
) -> JoinSet<()> {
    let mut ticker = JoinSet::new();
    ticker.spawn(report_index(
        Arc::clone(probe),
        timing.poll_interval,
        notes.clone(),
        before,
    ));
    ticker
}

async fn report_index(probe: Arc<IndexProbe>, interval: Duration, notes: Notes, before: u64) {
    let mut previous = before;
    let mut ticker = tokio::time::interval(interval);
    ticker.tick().await;
    loop {
        ticker.tick().await;
        if let Ok(state) = probe.status().await {
            let count = state.count();
            notes.say(&format!(
                "  index {} docs ({:.0} docs/s)",
                count.saturating_sub(before),
                rate(count.saturating_sub(previous), interval.as_secs_f64())
            ));
            previous = count;
        }
    }
}

#[cfg(test)]
#[path = "build_rate_tests.rs"]
mod tests;
