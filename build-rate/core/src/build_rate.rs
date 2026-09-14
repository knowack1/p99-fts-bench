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

use crate::index::{IndexProbe, IndexState, REFRESHED};
use crate::notes::Notes;
use crate::report::IndexBuild;
use crate::samples::{rate, IndexSample, Sample, Submitted, Tape};

#[derive(Debug, Clone)]
pub struct WatchTiming {
    pub poll_interval: Duration,
    pub settle_timeout: Duration,
    pub idle_timeout: Duration,
}

/// The vector-store, or nothing at all when `--no-index-watch` was given.
pub struct IndexWatch {
    watcher: Option<Watcher>,
}

struct Watcher {
    probe: Arc<dyn IndexProbe>,
    timing: WatchTiming,
}

impl IndexWatch {
    pub fn off() -> Self {
        Self { watcher: None }
    }

    pub fn on(probe: Arc<dyn IndexProbe>, timing: WatchTiming) -> Self {
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
        let inherited = inherited_counts(watcher.probe.as_ref()).await?;
        tape.inherited(inherited.docs, inherited.accepted);
        Ok(LevelWatch {
            ticker: follow(
                Some(Arc::clone(&watcher.probe)),
                watcher.timing.poll_interval,
                tape,
                submitted,
                notes,
            ),
            level: Some(Level::new(watcher, notes, tape, submitted, inherited.docs)),
        })
    }
}

/// What the index already held when this level started, so the level's own
/// build can be counted apart from it.
///
/// A poll nobody could answer is not zero documents. Taking it as zero would
/// make the level credit itself with everything already in the index, and the
/// `index_docs` it reported would be a complete, plausible, wrong number rather
/// than a failure.
async fn inherited_counts(probe: &dyn IndexProbe) -> Result<Inherited> {
    match probe.read().await {
        IndexState::Unreadable(why) => bail!(
            "the index at {} could not be read before this level started, so \
             what it already held is unknown: {why}",
            probe.endpoint()
        ),
        state => Ok(Inherited {
            docs: state.docs(),
            accepted: state.accepted().unwrap_or_else(|| state.docs()),
        }),
    }
}

/// What was in the index before this level, on both series.
#[derive(Debug, Clone, Copy)]
struct Inherited {
    docs: u64,
    accepted: u64,
}

/// One level's watch, from the first insert to the moment the index settles.
pub struct LevelWatch {
    level: Option<Level>,
    ticker: JoinSet<()>,
}

struct Level {
    probe: Arc<dyn IndexProbe>,
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
    ///
    /// Aborting is not enough to promise that. `abort_all` only marks the task;
    /// a ticker already past its poll runs the rest of its turn — including the
    /// `tape.record` — and is cancelled at the *next* await. So this waits for
    /// the task to be gone, which is the only point at which nothing else can
    /// still write to the tape.
    pub async fn client_stopped(&mut self) {
        self.ticker.abort_all();
        while self.ticker.join_next().await.is_some() {}
    }

    /// Whether anything is still able to write to this level's tape.
    pub fn is_quiet(&self) -> bool {
        self.ticker.is_empty()
    }

    pub async fn finish(mut self, submitted: u64) -> Result<Option<IndexBuild>> {
        self.client_stopped().await;
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

        let mut asked = false;
        let mut published = false;

        loop {
            self.sample_into(&mut seen).await;
            if seen.reached(target) {
                break;
            }
            if self.done_waiting(&seen) {
                if asked || !self.worth_a_refresh(&seen, target) {
                    break;
                }
                asked = true;
                published = self.probe.settle_hint().await;
                if !published {
                    break;
                }
                seen.given_another_chance();
            }
            tokio::time::sleep(self.timing.poll_interval).await;
        }
        let Some(state) = seen.last else {
            bail!(
                "the index was never readable at {} during this level, so its \
                 build rate cannot be reported",
                self.probe.endpoint()
            );
        };
        Ok(self.summarize(&state, target, seen.first_count, settling_from, published))
    }

    /// The engine has everything and is not publishing it.
    ///
    /// Only then, and only once. Asking while documents were still arriving
    /// would make the harness change what it was measuring; asking when the
    /// engine has not accepted everything would hide a build that genuinely
    /// stalled. Where a searchable count does not lag at all this never fires,
    /// because `docs` and `accepted` move together.
    fn worth_a_refresh(&self, seen: &Progress, target: u64) -> bool {
        seen.accepted() >= target && seen.count < target
    }

    /// The settle polls are readings like any other: the client has stopped, so
    /// the submit rate falls to zero while the index rate does not, and that
    /// tail is the part of the build a per-level average cannot show.
    async fn sample_into(&self, seen: &mut Progress) {
        match self.probe.read().await {
            IndexState::Unreadable(why) => {
                self.notes.say(&format!("  !! index poll failed: {why}"));
            }
            state => self.keep(state, seen),
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
    ///
    /// "Stopped moving" is measured on what the engine has *accepted*, never on
    /// what is searchable. A searchable count legitimately sits still between
    /// refreshes, so an idle timeout watching it would call an ordinary pause a
    /// finished build — and at `refresh_interval: -1` it would call every level
    /// a build of nothing.
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
        published: bool,
    ) -> IndexBuild {
        let docs = state.docs().saturating_sub(self.before);
        let wall_s = self.started.elapsed().as_secs_f64();
        IndexBuild {
            docs,
            docs_per_s: rate(docs, wall_s),
            lag_docs: target.saturating_sub(at_submit_end),
            settle_s: settling_from.elapsed().as_secs_f64(),
            settled: state.docs() >= target,
            status: terminal_status(state, published),
        }
    }
}

/// What the polls have seen so far, and when the count last changed.
struct Progress {
    last: Option<IndexState>,
    first_count: u64,
    /// The settle authority: what a search would find.
    count: u64,
    /// The idle authority: what the engine has accepted, or the searchable
    /// count on an engine that does not report the two separately.
    moving: u64,
    since: Instant,
    moved_at: Instant,
}

impl Progress {
    fn new(started: Instant) -> Self {
        Self {
            last: None,
            first_count: 0,
            count: 0,
            moving: 0,
            since: started,
            moved_at: started,
        }
    }

    fn record(&mut self, state: IndexState) {
        let count = state.docs();
        let moving = state.accepted().unwrap_or(count);
        if self.last.is_none() {
            self.first_count = count;
        }
        if moving != self.moving {
            self.moved_at = Instant::now();
        }
        self.count = count;
        self.moving = moving;
        self.last = Some(state);
    }

    fn accepted(&self) -> u64 {
        self.moving
    }

    /// After a forced refresh, the idle clock starts again: the engine was
    /// asked to do something, and it deserves a poll to have done it.
    fn given_another_chance(&mut self) {
        self.moved_at = Instant::now();
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
    probe: Option<Arc<dyn IndexProbe>>,
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
    probe: Option<Arc<dyn IndexProbe>>,
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
async fn read_index(probe: Option<&dyn IndexProbe>, notes: &Notes) -> Option<IndexState> {
    let probe = probe?;
    match probe.read().await {
        IndexState::Unreadable(why) => {
            notes.say(&format!("  !! index poll failed: {why}"));
            None
        }
        state => Some(state),
    }
}

/// A build whose last documents were published because the harness asked says
/// so, because that is not the same measurement as one the engine's own refresh
/// policy would have produced — at `refresh_interval: -1` the policy would
/// never have produced it at all.
fn terminal_status(state: &IndexState, published: bool) -> String {
    if published {
        return REFRESHED.to_string();
    }
    state.status_word().to_string()
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
