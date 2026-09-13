//! Doubles both harnesses' tests need, and core's own.
//!
//! Behind a feature rather than `#[cfg(test)]`, because `#[cfg(test)]` items are
//! invisible across a crate boundary and the two binaries test against these
//! too. A dev-dependency feature never reaches `cargo build --release`, so the
//! shipped binaries do not carry any of it.
use std::sync::{Arc, Mutex};
use std::time::Duration;

use crate::index::{BoxFuture, IndexProbe, IndexReading, IndexState};
use crate::notes::Notes;
use crate::samples::Submitted;

/// Notes a test can read back, instead of asserting about stderr.
#[derive(Clone, Default)]
pub struct SpokenNotes(Arc<Mutex<Vec<String>>>);

impl SpokenNotes {
    pub fn lines(&self) -> Vec<String> {
        self.0.lock().unwrap().clone()
    }

    pub fn mentions(&self, needle: &str) -> bool {
        self.lines().iter().any(|line| line.contains(needle))
    }

    pub fn notes(&self, progress_interval: Duration) -> Notes {
        let sink = self.clone();
        Notes::new(
            progress_interval,
            Arc::new(move |message: &str| sink.0.lock().unwrap().push(message.to_string())),
        )
    }
}

pub fn quiet_notes() -> Notes {
    SpokenNotes::default().notes(Duration::from_secs(3600))
}

/// For the tests that measure a level without watching the series it feeds: the
/// workers still count what they submitted, nobody reads it.
pub fn no_counter() -> Arc<Submitted> {
    Arc::new(Submitted::default())
}

pub fn a_reading(docs: u64) -> IndexState {
    IndexState::Present(IndexReading {
        docs,
        accepted: None,
        status: "SERVING".to_string(),
        ready: true,
    })
}

/// An engine that counts what it has taken in apart from what a search would
/// find — the shape a refresh-gated index has.
pub fn accepted_but_not_yet_searchable(docs: u64, accepted: u64) -> IndexState {
    IndexState::Present(IndexReading {
        docs,
        accepted: Some(accepted),
        status: "indexing".to_string(),
        ready: true,
    })
}

/// Answers a scripted sequence, then repeats its last answer forever.
///
/// In memory rather than over HTTP: the settle loop is what these tests are
/// about, and a socket would only add a way for them to be flaky.
pub struct ScriptedProbe {
    script: Mutex<Vec<IndexState>>,
    refreshes: Mutex<usize>,
    publishes: Option<IndexState>,
    endpoint: String,
}

impl ScriptedProbe {
    pub fn new(states: Vec<IndexState>) -> Self {
        Self {
            script: Mutex::new(states),
            refreshes: Mutex::new(0),
            publishes: None,
            endpoint: "http://sut/index/status".to_string(),
        }
    }

    /// What the index answers once someone asks it to publish. Without this a
    /// scripted probe ignores the hint, which is itself a case worth testing.
    pub fn publishing(mut self, state: IndexState) -> Self {
        self.publishes = Some(state);
        self
    }

    pub fn refreshes(&self) -> usize {
        *self.refreshes.lock().unwrap()
    }

    /// What every poll from now on answers.
    pub fn standing(&self, state: IndexState) {
        *self.script.lock().unwrap() = vec![state];
    }

    /// The next few answers, then the last of them forever.
    pub fn then(&self, states: &[IndexState]) {
        *self.script.lock().unwrap() = states.to_vec();
    }
}

impl IndexProbe for ScriptedProbe {
    fn read(&self) -> BoxFuture<'_, IndexState> {
        let mut script = self.script.lock().unwrap();
        let state = if script.len() > 1 {
            script.remove(0)
        } else {
            script.first().cloned().unwrap_or(IndexState::Absent)
        };
        Box::pin(std::future::ready(state))
    }

    fn endpoint(&self) -> &str {
        &self.endpoint
    }

    fn settle_hint(&self) -> BoxFuture<'_, ()> {
        *self.refreshes.lock().unwrap() += 1;
        if let Some(published) = self.publishes.clone() {
            *self.script.lock().unwrap() = vec![published];
        }
        Box::pin(std::future::ready(()))
    }
}
