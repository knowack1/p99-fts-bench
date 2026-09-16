//! Fixed-schedule dispatch, so a rate is *offered* rather than discovered.
//!
//! The defect this exists to prevent: a loader that sends the next request only
//! after the previous one returns measures a *slower* engine as *faster*,
//! because while the engine is stalled the loader stops asking it questions.
//! The stall vanishes from the sample instead of dominating it. That is
//! coordinated omission, and a latency column measured that way is a service
//! time wearing a latency's name.
//!
//! The schedule is absolute and computed from the level's start: the document at
//! offset `d` is *intended* to go at `origin + d / rate`, whether or not the
//! producer managed to send it then. A request's latency is reported against
//! that intended time, so a stall shows up at full size in everything queued
//! behind it, and the producer's own lateness is recorded separately as
//! `queue_ms`. That turns "this harness is coordinated-omission safe" from a
//! claim into a number a reader can check: if `queue_ms` is a material fraction
//! of the latency, the chart is measuring the harness.
//!
//! **Closed loop is this same path with no due time of its own.**
//! [`Schedule::unpaced`] yields `None`, [`Timing::measure`] stands the request's
//! own start in for it, and latency collapses to service time with `queue_ms`
//! at zero. That is one expression rather than a second code path, and it is
//! also the only definition that leaves the concurrency ladder measuring what
//! it measured before: a due time stamped when the *producer* released an item
//! would fold the harness's own read-ahead buffer into every latency.
//!
//! Closed loop remains the correct instrument for "how fast can it go", which is
//! what the concurrency ladder measures; it is the wrong one for "what does it
//! do at rate X", which is what a rate ladder measures.
//!
//! Ported from `ftsbench/pacer.py`, whose docstrings are the specification and
//! whose `tests/test_pacer.py` is the behaviour these tests mirror. The Rust
//! side takes no dependency on it.
use std::time::{Duration, Instant};

/// Sleeping for less than this costs more than the lateness it would correct, so
/// the producer proceeds instead.
///
/// The consequence is worth stating rather than discovering: at 50,000 docs/s a
/// document is due every 20 µs, far under this floor, so the producer releases
/// them in bursts of roughly `rate × MIN_SLEEP` — about 25 documents per 0.5 ms.
/// The schedule is still exact *on average*; it is the instantaneous rate that
/// is bursty, at a scale three orders of magnitude below any commit or refresh
/// interval either engine runs. A `_bulk` boundary is coarser still.
pub const MIN_SLEEP: Duration = Duration::from_micros(500);

/// When a document is due, and how late it actually was.
///
/// `Instant` rather than an offset because every consumer compares it against
/// `Instant::now()`, and carrying the origin to each of them is how the two
/// clocks drift apart.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Due(Instant);

impl Due {
    pub fn at(instant: Instant) -> Self {
        Self(instant)
    }

    pub fn instant(self) -> Instant {
        self.0
    }
}

/// The offered-rate schedule for one level.
///
/// Cheap to copy and holds no state that advances: the caller says how many
/// documents have already been offered, so the schedule cannot silently drift
/// out of step with what the producer actually sent.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Schedule {
    origin: Instant,
    /// Documents per second. `None` is closed loop.
    rate: Option<u64>,
}

impl Schedule {
    /// Closed loop: every document is due the moment it is asked for, so latency
    /// collapses to service time and `queue_ms` to zero.
    pub fn unpaced() -> Self {
        Self {
            origin: Instant::now(),
            rate: None,
        }
    }

    /// Open loop at `docs_per_s`, timed from now.
    ///
    /// A zero rate is refused rather than treated as "unpaced": the two are
    /// different measurements, the caller has a way to say which it wants, and
    /// silently promoting one to the other is how a rate ladder ends up with a
    /// closed-loop rung nobody notices.
    pub fn at_rate(docs_per_s: u64) -> Self {
        assert!(docs_per_s > 0, "a paced schedule needs a positive rate");
        Self {
            origin: Instant::now(),
            rate: Some(docs_per_s),
        }
    }

    pub fn from_rate(docs_per_s: Option<u64>) -> Self {
        docs_per_s.map_or_else(Self::unpaced, Self::at_rate)
    }

    pub fn origin(&self) -> Instant {
        self.origin
    }

    pub fn rate(&self) -> Option<u64> {
        self.rate
    }

    pub fn is_paced(&self) -> bool {
        self.rate.is_some()
    }

    /// When the document at `docs_offered` is due — absolute, never cumulative,
    /// so a late send does not push everything after it later too.
    ///
    /// `None` under closed loop. Not "now": an item released by the producer
    /// may sit in the bounded channel before a worker takes it, and calling that
    /// wait *lateness* would charge the harness's own read-ahead to the engine.
    /// Closed loop has no schedule, so it has nothing to be late against.
    pub fn due(&self, docs_offered: u64) -> Option<Due> {
        let rate = self.rate?;
        Some(Due(self.origin + offset_for(docs_offered, rate)))
    }

    /// Wait until that document is due and return the time it was *meant* to go.
    ///
    /// Behind schedule, this returns at once and does not skip the document:
    /// the backlog is the finding, and compressing or dropping work would hide
    /// exactly what the rung was run to discover.
    ///
    /// `stop` is polled while waiting so a Ctrl-C does not have to outlast one
    /// inter-arrival gap. At a campaign rate that gap is microseconds and this
    /// never chunks; at one document per second it is the difference between a
    /// prompt exit and a second of it.
    pub fn wait_for(&self, docs_offered: u64, stop: &dyn Fn() -> bool) -> Option<Due> {
        let due = self.due(docs_offered)?;
        sleep_until_unless(due.0, stop);
        Some(due)
    }

    /// How long a level offering `docs` at this rate ought to take. `None` under
    /// closed loop, where the engine decides and there is nothing to predict.
    pub fn expected_wall(&self, docs: u64) -> Option<Duration> {
        self.rate.map(|rate| offset_for(docs, rate))
    }
}

fn offset_for(docs_offered: u64, rate: u64) -> Duration {
    Duration::from_secs_f64(docs_offered as f64 / rate as f64)
}

/// A paced level running this multiple of its own schedule is not going to
/// recover: the engine is accepting far below the offered rate and every extra
/// second is spent draining a backlog rather than measuring anything.
pub const OVERRUN_FACTOR: f64 = 3.0;

/// Below this the ratio is noise — a level a few hundred milliseconds in has
/// barely started — so the overrun rule does not apply yet.
pub const OVERRUN_GRACE: Duration = Duration::from_secs(10);

impl Schedule {
    /// Whether this level has fallen so far behind its own schedule that
    /// continuing only spends fleet time.
    ///
    /// Always false under closed loop, which has no schedule to fall behind:
    /// there the engine sets the pace and taking a long time is the measurement
    /// rather than a failure of it.
    pub fn overrun(&self, docs_offered: u64) -> bool {
        let Some(expected) = self.expected_wall(docs_offered) else {
            return false;
        };
        let elapsed = self.origin.elapsed();
        elapsed > OVERRUN_GRACE && elapsed > expected.mul_f64(OVERRUN_FACTOR)
    }
}

/// The longest this module will sleep without looking up.
///
/// A paced producer parked for a whole inter-arrival gap cannot notice a
/// Ctrl-C, and `Runtime::drop` waits for outstanding blocking tasks — so a slow
/// rung would hold the process open for as long as its own gap. Waking at this
/// interval bounds that, and costs nothing at any rate whose gap is shorter.
pub const WAKE_INTERVAL: Duration = Duration::from_millis(50);

/// Returns at once when already past the instant — see [`Schedule::wait_for`].
///
/// A blocking sleep is correct *here* and would be a defect one layer up: the
/// producer runs on `spawn_blocking`, off the tokio runtime, so this parks one
/// dedicated thread. On a runtime thread it would stall every in-flight request
/// for the pacing gap, turning the pacer into the thing it exists to measure
/// around.
pub fn sleep_until(instant: Instant) {
    sleep_until_unless(instant, &|| false);
}

/// The same wait, broken into [`WAKE_INTERVAL`] chunks so `stop` is seen.
pub fn sleep_until_unless(instant: Instant, stop: &dyn Fn() -> bool) {
    loop {
        let gap = instant.saturating_duration_since(Instant::now());
        if gap <= MIN_SLEEP || stop() {
            return;
        }
        std::thread::sleep(gap.min(WAKE_INTERVAL));
    }
}

/// What one request's clock produced: latency against the intended start,
/// service against the actual send, and the gap between them.
///
/// Carried together because reporting the first without the third is the claim
/// this module exists to make checkable. With no intended time — closed loop —
/// the request's own start stands in, so latency is service time and the gap is
/// zero.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Timing {
    pub latency_ms: f64,
    pub service_ms: f64,
    pub queue_ms: f64,
}

impl Timing {
    /// `queue_ms` is clamped at zero: a request dispatched marginally early by
    /// clock granularity is not negative queueing, and letting it go negative
    /// would quietly subtract from the coordinated-omission accounting.
    pub fn measure(due: Option<Due>, started: Instant, ended: Instant) -> Self {
        let intended = due.map_or(started, |due| due.0);
        let latency_ms = millis_between(intended, ended);
        let service_ms = millis_between(started, ended);
        Self {
            latency_ms,
            service_ms,
            queue_ms: (latency_ms - service_ms).max(0.0),
        }
    }
}

/// Saturating rather than panicking on a reversed pair: a clock that went
/// backwards is a bad sample, and killing a fleet run over one is worse than
/// recording a zero.
fn millis_between(from: Instant, to: Instant) -> f64 {
    to.saturating_duration_since(from).as_secs_f64() * 1000.0
}

#[cfg(test)]
#[path = "pacer_tests.rs"]
mod tests;
