//! Doubles both harnesses' tests need, and core's own.
//!
//! Behind a feature rather than `#[cfg(test)]`, because `#[cfg(test)]` items are
//! invisible across a crate boundary and the two binaries test against these
//! too. A dev-dependency feature never reaches `cargo build --release`, so the
//! shipped binaries do not carry any of it.
use std::collections::HashMap;
use std::future::Future;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use anyhow::{anyhow, Result};

use crate::index::{BoxFuture, IndexProbe, IndexReading, IndexState};
use crate::notes::Notes;
use crate::samples::Submitted;
use crate::sweep::{Accepted, Inserter, WorkItem};

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

    fn settle_hint(&self) -> BoxFuture<'_, bool> {
        *self.refreshes.lock().unwrap() += 1;
        let published = self.publishes.clone();
        let asked = published.is_some();
        if let Some(published) = published {
            *self.script.lock().unwrap() = vec![published];
        }
        Box::pin(std::future::ready(asked))
    }
}

/// The position value that means "this request never came back at all", as
/// against one that came back having rejected some of what it carried.
pub const WHOLE_REQUEST: usize = usize::MAX;

/// An inserter that records what it was offered and can be told to fail.
///
/// Generic over the work item, because the two harnesses offer different ones
/// and every property worth asserting — in-flight never exceeds the level, the
/// channel is bounded, the earliest failure survives the merge — is a property
/// of the sweep rather than of the payload.
pub struct FakeInserter<W: WorkItem> {
    seen: Arc<Mutex<Seen<W>>>,
    completed: AtomicUsize,
    latency: Duration,
    failing_positions: HashMap<usize, u64>,
}

struct Seen<W> {
    requests: usize,
    docs: u64,
    in_flight: usize,
    docs_in_flight: u64,
    max_in_flight: usize,
    max_docs_in_flight: u64,
    request_sizes: Vec<u64>,
    items: Vec<W>,
}

impl<W> Default for Seen<W> {
    fn default() -> Self {
        Self {
            requests: 0,
            docs: 0,
            in_flight: 0,
            docs_in_flight: 0,
            max_in_flight: 0,
            max_docs_in_flight: 0,
            request_sizes: Vec::new(),
            items: Vec::new(),
        }
    }
}

impl<W: WorkItem> Default for FakeInserter<W> {
    fn default() -> Self {
        Self::new()
    }
}

impl<W: WorkItem> FakeInserter<W> {
    pub fn new() -> Self {
        Self::with_latency(Duration::ZERO)
    }

    pub fn with_latency(latency: Duration) -> Self {
        Self {
            seen: Arc::new(Mutex::new(Seen::default())),
            completed: AtomicUsize::new(0),
            latency,
            failing_positions: HashMap::new(),
        }
    }

    /// Requests, by 1-based position, that never come back.
    pub fn failing_at(mut self, positions: &[usize]) -> Self {
        self.failing_positions = positions
            .iter()
            .map(|&at| (at, WHOLE_REQUEST as u64))
            .collect();
        self
    }

    /// `(position, documents)` — the request answers success and rejects that
    /// many of its items, the way OpenSearch reports a partial bulk.
    pub fn rejecting(mut self, failures: &[(usize, u64)]) -> Self {
        self.failing_positions = failures.iter().copied().collect();
        self
    }

    pub fn requests(&self) -> usize {
        self.seen.lock().unwrap().requests
    }

    pub fn docs(&self) -> u64 {
        self.seen.lock().unwrap().docs
    }

    pub fn max_in_flight(&self) -> usize {
        self.seen.lock().unwrap().max_in_flight
    }

    pub fn max_docs_in_flight(&self) -> u64 {
        self.seen.lock().unwrap().max_docs_in_flight
    }

    pub fn request_sizes(&self) -> Vec<u64> {
        self.seen.lock().unwrap().request_sizes.clone()
    }

    pub fn completed(&self) -> usize {
        self.completed.load(Ordering::SeqCst)
    }

    fn depart(&self, item: W) -> usize {
        let docs = item.docs();
        let mut seen = self.seen.lock().unwrap();
        seen.requests += 1;
        seen.docs += docs;
        seen.in_flight += 1;
        seen.docs_in_flight += docs;
        seen.max_in_flight = seen.max_in_flight.max(seen.in_flight);
        seen.max_docs_in_flight = seen.max_docs_in_flight.max(seen.docs_in_flight);
        seen.request_sizes.push(docs);
        seen.items.push(item);
        seen.requests
    }

    fn arrive(&self, docs: u64) {
        let mut seen = self.seen.lock().unwrap();
        seen.in_flight -= 1;
        seen.docs_in_flight -= docs;
        drop(seen);
        self.completed.fetch_add(1, Ordering::SeqCst);
    }
}

impl<W: WorkItem + Clone> FakeInserter<W> {
    pub fn items_seen(&self) -> Vec<W> {
        self.seen.lock().unwrap().items.clone()
    }
}

impl<W: WorkItem> Inserter for FakeInserter<W> {
    type Item = W;

    // The explicit `impl Future + Send` is the point: `async fn` in a trait
    // leaves the future's `Send`ness up to the caller, and these futures are
    // spawned onto tokio, which requires it.
    #[allow(clippy::manual_async_fn)]
    fn insert(&self, item: W) -> impl Future<Output = Result<Accepted>> + Send {
        async move {
            let docs = item.docs();
            let position = self.depart(item);
            tokio::time::sleep(self.latency).await;
            self.arrive(docs);
            match self.failing_positions.get(&position) {
                None => Ok(Accepted::CLEAN),
                Some(&rejected) if rejected == WHOLE_REQUEST as u64 => {
                    Err(anyhow!("the socket went away"))
                }
                Some(&rejected) => Ok(Accepted {
                    failed: rejected,
                    first_failure: Some("status 429 too many requests".to_string()),
                }),
            }
        }
    }
}
