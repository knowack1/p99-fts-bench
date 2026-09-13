//! What one poll of an index found, and the thing that polls it.
//!
//! An FTS index build is only visible where the engine reports it, and the two
//! engines report it in different places — a vector-store status endpoint on one
//! side, `_stats` on the other. What they have in common is this: how many
//! documents a search would find, how many the engine has accepted, whether it
//! is answering at all, and what it calls itself.
//!
//! **Two counts, because on one engine they genuinely differ.** `docs` is the
//! searchable count and it is what says a build has finished. `accepted` is what
//! the engine has taken in, and it is what says a build is still *moving* — the
//! distinction that matters wherever a searchable count only advances at a
//! refresh, where "the count has not changed" means "not refreshed yet" rather
//! than "the engine has stopped".
use std::future::Future;
use std::pin::Pin;

pub type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

pub const ABSENT: &str = "absent";
/// A build whose last documents the harness had to ask for.
pub const REFRESHED: &str = "refreshed";

/// One reading of an index that answered.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IndexReading {
    /// What a search would find now.
    pub docs: u64,
    /// What the engine has accepted, where it counts that separately.
    pub accepted: Option<u64>,
    /// What the engine calls this state. UPPERCASE where the engine said it,
    /// lowercase where the harness minted it, so a reader can tell a quoted
    /// status from a derived one.
    pub status: String,
    /// Answering queries. Not a variant, because a gate that waits for an index
    /// to go away has to accept an index that is present but not yet ready.
    pub ready: bool,
}

/// What one poll saw.
///
/// `Absent` is a real answer, not an error: between the drop and the create
/// there is genuinely no index, and that is the state a reset gate waits to
/// see. `Unreadable` is also a state rather than an error, because the engine
/// is legitimately unable to answer for a moment while a delete settles or a
/// primary is allocated — the deadline is what decides the moment has lasted
/// too long.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum IndexState {
    Absent,
    Unreadable(String),
    Present(IndexReading),
}

impl IndexState {
    pub fn reading(&self) -> Option<&IndexReading> {
        match self {
            Self::Present(reading) => Some(reading),
            _ => None,
        }
    }

    /// Present *and* answering queries. The gate that waits for a new index to
    /// be usable wants this; the one that waits for an old index to go away
    /// wants its absence.
    pub fn ready(&self) -> Option<&IndexReading> {
        self.reading().filter(|reading| reading.ready)
    }

    pub fn is_absent(&self) -> bool {
        matches!(self, Self::Absent)
    }

    /// An index nobody could read is not an index holding nothing, so an
    /// unreadable poll counts as zero here only because every caller pairs it
    /// with a status that says so.
    pub fn docs(&self) -> u64 {
        self.reading().map_or(0, |reading| reading.docs)
    }

    pub fn accepted(&self) -> Option<u64> {
        self.reading().and_then(|reading| reading.accepted)
    }

    pub fn status_word(&self) -> &str {
        match self {
            Self::Present(reading) => &reading.status,
            Self::Absent => ABSENT,
            Self::Unreadable(_) => "unreadable",
        }
    }

    pub fn describe(&self) -> String {
        match self {
            Self::Absent => ABSENT.to_string(),
            Self::Unreadable(why) => format!("unreadable ({why})"),
            Self::Present(reading) => format!("{} at {} docs", reading.status, reading.docs),
        }
    }
}

/// Where an index build is visible on this engine.
///
/// A trait object rather than a type parameter: it is read about once a second
/// against an HTTP round trip, so one boxed future per poll is unmeasurable,
/// while a type parameter would have to be threaded through the watch, the
/// level and the sweep for nothing.
pub trait IndexProbe: Send + Sync + 'static {
    /// Never fails. A failure is an `Unreadable` reading, so that every caller
    /// makes the same choice about what a poll nobody could answer means.
    fn read(&self) -> BoxFuture<'_, IndexState>;

    /// Named in gate timeouts and settle failures, so an operator is told where
    /// the harness was looking.
    fn endpoint(&self) -> &str;

    /// Publish whatever has been indexed but is not yet searchable, and say
    /// whether anything was actually asked of the engine.
    ///
    /// `false` wherever a searchable count does not lag behind — there is
    /// nothing to ask for — and wherever the operator turned the asking off.
    /// The answer reaches the level's `index_status`, so it has to be what
    /// happened rather than what was attempted: a level that reports
    /// `refreshed` without anyone having refreshed it is a lie about which
    /// refresh policy produced the number.
    ///
    /// Where it does ask, this is the last resort after the engine has stopped
    /// making progress, never something done mid-build: a harness that forced
    /// the engine's hand while measuring it would be changing what it measured.
    fn settle_hint(&self) -> BoxFuture<'_, bool> {
        Box::pin(std::future::ready(false))
    }
}

#[cfg(test)]
#[path = "index_tests.rs"]
mod tests;
