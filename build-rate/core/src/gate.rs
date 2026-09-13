//! Waiting for an index to reach a named state, and failing by that name.
//!
//! A reset that quietly did not happen produces a complete, plausible, wrong
//! build rate, so every gate says what it was waiting for and what it last saw.
use std::time::{Duration, Instant};

use anyhow::{bail, Result};

use crate::index::{IndexProbe, IndexState};

#[derive(Debug, Clone)]
pub struct GateTiming {
    pub poll_interval: Duration,
    pub timeout: Duration,
}

/// Polls one index until it reaches a state the caller names.
///
/// The predicates stay with the engines. "The old index is gone" is `absent` on
/// one side and "no longer serving" on the other, and collapsing them would
/// silently change which states each gate accepts.
pub struct Gate<'a> {
    probe: &'a dyn IndexProbe,
    timing: &'a GateTiming,
}

impl<'a> Gate<'a> {
    pub fn new(probe: &'a dyn IndexProbe, timing: &'a GateTiming) -> Self {
        Self { probe, timing }
    }

    pub async fn await_state(
        &self,
        what: &str,
        reached: impl Fn(&IndexState) -> bool,
    ) -> Result<()> {
        let deadline = Instant::now() + self.timing.timeout;
        let mut last = "not polled yet".to_string();
        while Instant::now() < deadline {
            let state = self.probe.read().await;
            if reached(&state) {
                return Ok(());
            }
            last = state.describe();
            tokio::time::sleep(self.timing.poll_interval).await;
        }
        bail!(
            "timed out after {:.0}s waiting for {what}; the index was last {last} at {}",
            self.timing.timeout.as_secs_f64(),
            self.probe.endpoint()
        )
    }
}

#[cfg(test)]
#[path = "gate_tests.rs"]
mod tests;
