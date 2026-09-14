use std::sync::Mutex;
use std::time::Duration;

use super::*;
use crate::index::{BoxFuture, IndexReading};

/// Answers a scripted sequence, then repeats its last answer forever.
struct ScriptedProbe {
    script: Mutex<Vec<IndexState>>,
    endpoint: String,
}

impl ScriptedProbe {
    fn new(states: Vec<IndexState>) -> Self {
        Self {
            script: Mutex::new(states),
            endpoint: "http://sut:6080/status".to_string(),
        }
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
}

fn present(docs: u64, ready: bool) -> IndexState {
    IndexState::Present(IndexReading {
        docs,
        accepted: None,
        status: "SERVING".to_string(),
        ready,
    })
}

fn quick() -> GateTiming {
    GateTiming {
        poll_interval: Duration::from_millis(1),
        timeout: Duration::from_millis(200),
    }
}

#[tokio::test]
async fn a_gate_returns_as_soon_as_the_state_is_reached() {
    let probe = ScriptedProbe::new(vec![present(9, true), IndexState::Absent]);
    let timing = quick();
    let gate = Gate::new(&probe, &timing);
    assert!(gate
        .await_state("it to go", IndexState::is_absent)
        .await
        .is_ok());
}

/// A poll the engine could not answer is not a reason to give up: a delete is
/// legitimately settling for a moment, and the deadline is what decides the
/// moment has lasted too long.
#[tokio::test]
async fn an_unreadable_poll_is_waited_through_not_raised() {
    let probe = ScriptedProbe::new(vec![
        IndexState::Unreadable("503".to_string()),
        IndexState::Absent,
    ]);
    let timing = quick();
    let gate = Gate::new(&probe, &timing);
    assert!(gate
        .await_state("it to go", IndexState::is_absent)
        .await
        .is_ok());
}

/// A reset that quietly did not happen produces a complete, plausible, wrong
/// build rate, so the timeout has to name both what it wanted and what it saw.
#[tokio::test]
async fn a_gate_that_times_out_says_what_it_wanted_and_what_it_last_saw() {
    let probe = ScriptedProbe::new(vec![present(42, true)]);
    let timing = quick();
    let gate = Gate::new(&probe, &timing);

    let failed = gate
        .await_state("the deleted index to disappear", IndexState::is_absent)
        .await
        .unwrap_err();
    let said = format!("{failed:#}");

    assert!(said.contains("the deleted index to disappear"), "{said}");
    assert!(said.contains("42 docs"), "{said}");
    assert!(said.contains("http://sut:6080/status"), "{said}");
}

#[tokio::test]
async fn a_gate_waiting_for_readiness_does_not_accept_a_present_but_unready_index() {
    let probe = ScriptedProbe::new(vec![present(0, false)]);
    let timing = quick();
    let gate = Gate::new(&probe, &timing);

    let failed = gate
        .await_state("it to answer", |state| state.ready().is_some())
        .await;
    assert!(failed.is_err());
}
