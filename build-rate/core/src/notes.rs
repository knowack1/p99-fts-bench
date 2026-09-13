//! Where a running sweep talks. Progress and warnings go to stderr so the CSV
//! on stdout stays a CSV; the sink is a value rather than a bare `eprintln!` so
//! a test can read back what a sweep said.
use std::io::{self, Write};
use std::sync::Arc;
use std::time::Duration;

pub const PROGRESS_INTERVAL: Duration = Duration::from_secs(1);

/// Every line a sweep says, including the ones a failure says on its way out.
/// Flushed per line: a run killed mid-ladder has already said what it got to.
pub fn note(message: &str) {
    let mut stderr = io::stderr();
    let _ = writeln!(stderr, "{message}");
    let _ = stderr.flush();
}

#[derive(Clone)]
pub struct Notes {
    progress_interval: Duration,
    sink: Arc<dyn Fn(&str) + Send + Sync>,
}

impl Notes {
    pub fn stderr() -> Self {
        Self::new(PROGRESS_INTERVAL, Arc::new(note))
    }

    pub fn new(progress_interval: Duration, sink: Arc<dyn Fn(&str) + Send + Sync>) -> Self {
        Self {
            progress_interval,
            sink,
        }
    }

    pub fn say(&self, message: &str) {
        (self.sink)(message);
    }

    pub fn progress_interval(&self) -> Duration {
        self.progress_interval
    }
}

impl Default for Notes {
    fn default() -> Self {
        Self::stderr()
    }
}

impl std::fmt::Debug for Notes {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Notes")
            .field("progress_interval", &self.progress_interval)
            .finish_non_exhaustive()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_stderr_sink_ticks_at_the_documented_interval() {
        assert_eq!(Notes::stderr().progress_interval(), PROGRESS_INTERVAL);
        assert_eq!(Notes::default().progress_interval(), PROGRESS_INTERVAL);
    }

    #[test]
    fn a_note_reaches_the_sink_it_was_built_with() {
        let seen = Arc::new(std::sync::Mutex::new(Vec::new()));
        let collector = Arc::clone(&seen);
        let notes = Notes::new(
            Duration::from_millis(5),
            Arc::new(move |message: &str| collector.lock().unwrap().push(message.to_string())),
        );
        notes.say("halfway");
        assert_eq!(seen.lock().unwrap().as_slice(), ["halfway"]);
    }

    /// The sink is a closure, so the derived `Debug` would not compile; the
    /// hand-written one still has to name the interval it was built with.
    #[test]
    fn a_notes_value_prints_the_interval_it_carries() {
        let rendered = format!("{:?}", Notes::new(Duration::from_secs(7), Arc::new(|_| {})));
        assert!(rendered.contains("Notes") && rendered.contains("7s"));
    }
}
