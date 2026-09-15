//! The index this harness searches, and the refusal to search a partial one.
//!
//! A latency number is a number about an index. Half a corpus answers faster
//! than a whole one, an index still catching up answers differently from a
//! settled one, and neither difference is visible in a p99 — so the count is
//! checked against the corpus before the first query is timed, and a run that
//! cannot make it match does not start.
//!
//! **Build if missing, skip if complete, refuse if unreadable.** Unreadable is
//! not "empty": the sibling tree learned that taking an unanswered poll as zero
//! produces a complete, plausible, wrong number, and here it would produce a
//! rebuild of an index that was already fine — or worse, a measurement of one
//! that was not.
//!
//! **A build here resets first, always.** An index holding fewer documents than
//! the corpus could be a load that died halfway, or it could be a different
//! corpus at the same ids; topping it up would silently keep whichever it was.
//! One pass from empty is the only state this harness can say anything about.
//!
//! **A build here may force the engine's hand, and the sibling may not.** There
//! the refresh is the measurement; here it is the precondition, so the loader
//! asks for one as soon as it has finished and waits for the count from there.
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::Path;
use std::time::Duration;

use anyhow::{bail, Context, Result};
use build_rate_core::gate::{Gate, GateTiming};
use build_rate_core::index::{BoxFuture, IndexProbe, IndexState};
use build_rate_core::notes::Notes;

/// What the corpus says the index should hold.
///
/// **One line is one document, counted exactly the way the loader reads them.**
/// `build_rate_core::corpus::Lines` takes every physical line, parses it, and
/// spends one of `--max-docs` on it whatever it contained — so this counts the
/// same way rather than skipping blank ones. Two definitions of "how many
/// documents the corpus has" is one more than a harness whose whole gate is
/// that number can afford: a corpus with a blank line in it is a corpus the
/// loader will refuse by name, and this must not quietly disagree about why.
///
/// Lines rather than parsed documents, because this runs before every query in
/// the matrix and the loader reports the real figure it inserted anyway. A line
/// nobody could read is an error rather than a line skipped: undercounting here
/// would make a complete index look short and send the run into a rebuild it did
/// not need.
pub fn count_documents(path: &Path, max_docs: usize) -> Result<u64> {
    let file =
        File::open(path).with_context(|| format!("cannot read corpus {}", path.display()))?;
    let mut counted = 0_u64;
    for line in BufReader::new(file).lines() {
        line.with_context(|| format!("{} line {}: unreadable", path.display(), counted + 1))?;
        counted += 1;
        if max_docs > 0 && counted as usize >= max_docs {
            break;
        }
    }
    Ok(counted)
}

/// What the run may do to the index it finds.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct BuildPolicy {
    /// Rebuild whatever is there, because the operator said so.
    pub rebuild: bool,
    /// Build at all. `false` is for a run against an index somebody else
    /// manages: it may verify, and it may refuse, but it may not write.
    pub may_build: bool,
}

impl BuildPolicy {
    pub const BUILD_IF_NEEDED: Self = Self {
        rebuild: false,
        may_build: true,
    };
}

#[derive(Debug, Clone, PartialEq)]
pub enum Decision {
    Skip { docs: u64 },
    Build { why: String },
    Refuse { why: String },
}

/// The whole of what this harness decides about an index, as a pure function of
/// one reading and one expected count.
pub fn decide(state: &IndexState, expected: u64, policy: BuildPolicy) -> Decision {
    if let IndexState::Unreadable(why) = state {
        return refuse(format!("the index could not be read: {why}"));
    }
    if policy.rebuild {
        return build("--rebuild-index was given".to_string(), policy);
    }
    match state {
        IndexState::Absent => build("there is no index".to_string(), policy),
        _ => decide_by_count(state.docs(), expected, policy),
    }
}

/// More documents than the corpus has is never a partial build: it is an index
/// somebody else filled, and loading this corpus on top of it would leave the
/// extra documents in place and the count still wrong.
fn decide_by_count(docs: u64, expected: u64, policy: BuildPolicy) -> Decision {
    match docs {
        found if found == expected => Decision::Skip { docs: found },
        found if found > expected => refuse(format!(
            "the index holds {found} documents and the corpus has {expected}: \
             it was not built from this corpus"
        )),
        found => build(
            format!("the index holds {found} of {expected} documents"),
            policy,
        ),
    }
}

fn build(why: String, policy: BuildPolicy) -> Decision {
    if policy.may_build {
        return Decision::Build { why };
    }
    refuse(format!("{why}, and --no-index-build forbids filling it"))
}

fn refuse(why: String) -> Decision {
    Decision::Refuse { why }
}

/// What one pass of the loader did. Reported rather than charted: how fast the
/// index built is the sibling tree's question, and a bootstrap that ran at one
/// fixed concurrency is not an answer to it.
#[derive(Debug, Clone, PartialEq)]
pub struct LoadReport {
    pub docs: u64,
    pub errors: u64,
    pub wall_s: f64,
    pub docs_per_s: f64,
}

/// Emptying the index and filling it from the corpus, in one call, because the
/// two must not be separable: a fill that ran without its reset is the partial
/// build this module exists to refuse.
pub trait IndexLoader: Send + Sync {
    fn build(&self) -> BoxFuture<'_, Result<LoadReport>>;

    /// Named in the announcement before anything is destroyed.
    fn describe(&self) -> String;
}

#[derive(Debug, Clone)]
pub struct BuildTiming {
    pub poll_interval: Duration,
    pub timeout: Duration,
}

impl BuildTiming {
    fn gate(&self) -> GateTiming {
        GateTiming {
            poll_interval: self.poll_interval,
            timeout: self.timeout,
        }
    }
}

/// The index, and whether this run is the one that built it.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct IndexReady {
    pub docs: u64,
    pub built: bool,
}

pub async fn ensure_index(
    probe: &dyn IndexProbe,
    loader: &dyn IndexLoader,
    expected: u64,
    policy: BuildPolicy,
    timing: &BuildTiming,
    notes: &Notes,
) -> Result<IndexReady> {
    let decision = decide(&probe.read().await, expected, policy);
    let built = match decision {
        Decision::Refuse { why } => bail!("{why} ({})", probe.endpoint()),
        Decision::Skip { docs } => {
            notes.say(&format!("index already holds all {docs} documents"));
            false
        }
        Decision::Build { why } => {
            fill(probe, loader, expected, timing, notes, &why).await?;
            true
        }
    };
    let docs = verify(probe, expected, timing).await?;
    Ok(IndexReady { docs, built })
}

async fn fill(
    probe: &dyn IndexProbe,
    loader: &dyn IndexLoader,
    expected: u64,
    timing: &BuildTiming,
    notes: &Notes,
    why: &str,
) -> Result<()> {
    notes.say(&format!(
        "building the index because {why}: {} will be emptied and filled with \
         {expected} documents",
        loader.describe()
    ));
    let report = loader.build().await?;
    refuse_a_lossy_load(&report)?;
    notes.say(&format!(
        "loaded {} documents in {:.1}s = {:.0} docs/s; waiting for the index to \
         hold all {expected}",
        report.docs, report.wall_s, report.docs_per_s
    ));
    ask_to_publish(probe, notes).await;
    await_complete(probe, expected, timing).await
}

/// Documents that never landed are documents the index will never hold, so the
/// gate below would time out on them anyway — one line later and with the
/// engine blamed for the client's failure.
fn refuse_a_lossy_load(report: &LoadReport) -> Result<()> {
    if report.errors == 0 {
        return Ok(());
    }
    bail!(
        "{} of {} documents were rejected while filling the index; the index \
         cannot be complete and nothing measured against it would be comparable",
        report.errors,
        report.errors + report.docs
    )
}

/// Legitimate here and nowhere in the sibling tree: what is being timed starts
/// after this, so publishing what the engine already has costs the measurement
/// nothing and saves it a refresh interval of waiting.
async fn ask_to_publish(probe: &dyn IndexProbe, notes: &Notes) {
    if probe.settle_hint().await {
        notes.say("asked the engine to publish what it had accepted");
    }
}

async fn await_complete(probe: &dyn IndexProbe, expected: u64, timing: &BuildTiming) -> Result<()> {
    let gate = timing.gate();
    Gate::new(probe, &gate)
        .await_state(
            &format!("the index to answer with all {expected} documents"),
            |state| {
                state
                    .ready()
                    .is_some_and(|reading| reading.docs >= expected)
            },
        )
        .await
}

/// Read once more after the gate, and compare exactly. The gate accepts "at
/// least", because an index that overshoots has to be caught and named rather
/// than waited on forever; this is where it is caught.
async fn verify(probe: &dyn IndexProbe, expected: u64, timing: &BuildTiming) -> Result<u64> {
    let gate = timing.gate();
    Gate::new(probe, &gate)
        .await_state("the index to answer queries", |state| {
            state.ready().is_some()
        })
        .await?;
    let state = probe.read().await;
    let docs = state.docs();
    if docs != expected {
        bail!(
            "the index at {} answers with {docs} documents and the corpus has \
             {expected}: no query measured against it would be comparable",
            probe.endpoint()
        );
    }
    Ok(docs)
}

#[cfg(test)]
#[path = "bootstrap_tests.rs"]
mod tests;
