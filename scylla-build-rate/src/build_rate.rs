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
use crate::samples::{rate, status_of, IndexSample, Sample, Submitted, Tape};
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

    /// The sampler runs whether or not the index is watched: the submit series
    /// does not depend on the vector-store, and `--no-index-watch` must not cost
    /// the operator the progress they had before this file existed.
    pub async fn begin(
        &self,
        notes: &Notes,
        tape: &Tape,
        submitted: &Arc<Submitted>,
    ) -> Result<LevelWatch> {
        let Some(watcher) = self.watcher.as_ref() else {
            let interval = notes.progress_interval();
            return Ok(LevelWatch {
                level: None,
                ticker: follow(None, interval, tape, submitted, notes),
            });
        };
        let before = watcher.probe.status().await?.count();
        tape.inherited(before);
        Ok(LevelWatch {
            ticker: follow(
                Some(Arc::clone(&watcher.probe)),
                watcher.timing.poll_interval,
                tape,
                submitted,
                notes,
            ),
            level: Some(Level::new(watcher, notes, tape, submitted, before)),
        })
    }
}

/// One level's watch, from the first insert to the moment the index settles.
pub struct LevelWatch {
    level: Option<Level>,
    ticker: JoinSet<()>,
}

struct Level {
    probe: Arc<IndexProbe>,
    timing: WatchTiming,
    notes: Notes,
    tape: Tape,
    submitted: Arc<Submitted>,
    before: u64,
    started: Instant,
}

impl Level {
    fn new(
        watcher: &Watcher,
        notes: &Notes,
        tape: &Tape,
        submitted: &Arc<Submitted>,
        before: u64,
    ) -> Self {
        Self {
            probe: Arc::clone(&watcher.probe),
            timing: watcher.timing.clone(),
            notes: notes.clone(),
            tape: tape.clone(),
            submitted: Arc::clone(submitted),
            before,
            started: Instant::now(),
        }
    }
}

impl LevelWatch {
    /// The last insert has landed. Stopping the ticker here rather than at
    /// `finish` is what keeps the handover ordered: the row that closes the
    /// submit series is written next, and a tick landing between the two would
    /// put the two readings in the file in either order.
    pub fn client_stopped(&mut self) {
        self.ticker.abort_all();
    }

    pub async fn finish(mut self, submitted: u64) -> Result<Option<IndexBuild>> {
        self.client_stopped();
        let Some(level) = self.level else {
            return Ok(None);
        };
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

    /// The settle polls are readings like any other: the client has stopped, so
    /// the submit rate falls to zero while the index rate does not, and that
    /// tail is the part of the build a per-level average cannot show.
    async fn sample_into(&self, seen: &mut Progress) {
        match self.probe.status().await {
            Ok(state) => self.keep(state, seen),
            Err(exc) => self.notes.say(&format!("  !! index poll failed: {exc:#}")),
        }
    }

    fn keep(&self, state: IndexState, seen: &mut Progress) {
        let sample = self.tape.record(self.submitted.ok(), Some(&state));
        self.notes.say(&level_line(&sample));
        seen.record(state);
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

/// Both rates on one line, from one reading: a level that is submitting fast
/// while the index crawls has to be visible while it happens rather than only
/// in the files afterwards, and two lines from two readings invite the reader
/// to compare numbers that were never taken together.
fn follow(
    probe: Option<Arc<IndexProbe>>,
    interval: Duration,
    tape: &Tape,
    submitted: &Arc<Submitted>,
    notes: &Notes,
) -> JoinSet<()> {
    let mut ticker = JoinSet::new();
    ticker.spawn(report_level(
        probe,
        interval,
        tape.clone(),
        Arc::clone(submitted),
        notes.clone(),
    ));
    ticker
}

async fn report_level(
    probe: Option<Arc<IndexProbe>>,
    interval: Duration,
    tape: Tape,
    submitted: Arc<Submitted>,
    notes: Notes,
) {
    let mut ticker = tokio::time::interval(interval);
    ticker.tick().await;
    loop {
        ticker.tick().await;
        let state = read_index(probe.as_deref(), &notes).await;
        notes.say(&level_line(&tape.record(submitted.ok(), state.as_ref())));
    }
}

/// A poll that failed leaves the index cells blank rather than zero, the same
/// distinction the point CSV makes: an index nobody could read is not an index
/// that indexed nothing.
async fn read_index(probe: Option<&IndexProbe>, notes: &Notes) -> Option<IndexState> {
    let probe = probe?;
    match probe.status().await {
        Ok(state) => Some(state),
        Err(exc) => {
            notes.say(&format!("  !! index poll failed: {exc:#}"));
            None
        }
    }
}

fn level_line(sample: &Sample) -> String {
    format!(
        "  c={} {:.0} docs/s (total {}){}",
        sample.concurrency,
        sample.submit_docs_per_s,
        sample.docs_submitted,
        index_phrase(sample.indexed.as_ref())
    )
}

fn index_phrase(indexed: Option<&IndexSample>) -> String {
    indexed.map_or_else(String::new, |indexed| {
        format!(
            ", index {} docs ({:.0} docs/s)",
            indexed.docs, indexed.docs_per_s
        )
    })
}

#[cfg(test)]
#[path = "build_rate_tests.rs"]
mod tests;
