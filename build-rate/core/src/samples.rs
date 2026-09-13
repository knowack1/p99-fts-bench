//! The per-second series behind the per-level numbers.
//!
//! `docs_per_s` and `index_docs_per_s` in the point CSV are whole-level
//! averages. An average cannot show a client submitting at full speed while the
//! index falls behind, nor the tail after the client stops and the index is
//! still catching up, nor a build that stalls and resumes — which are the
//! shapes `BUILD-RATE-LOOP.md` keeps asking about. This is that shape, one row
//! per reading.
//!
//! One file per level, because a level is one build: `--concurrency 8,8,16`
//! runs three of them and the two 8s are separate measurements.
//!
//! Rates are computed over the gap between consecutive readings rather than
//! over the interval a ticker asked for. A poll that overran its tick would
//! otherwise report a rate the run never reached, and the same reading feeds
//! both the stderr line and the row, so the two cannot drift apart.
use std::collections::HashMap;
use std::fs::{self, File};
use std::io::{self, BufWriter, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Instant;

use crate::index::IndexState;

/// APPENDED, never inserted, the same discipline `CSV_COLUMNS` follows.
///
/// `docs_indexed` is what a search would find and `docs_accepted` is what the
/// engine has taken in. They differ only where a searchable count lags behind —
/// and there, the pair is what separates an index that has stalled from one
/// that has simply not refreshed yet. **Not the same words
/// `ftsbench.samplers.OpenSearchSampler` uses**: its `docs_indexed` is this
/// file's `docs_accepted`, and its `docs_searchable` is this file's
/// `docs_indexed`. Both are right for their own purpose and the two files must
/// not be read into one another.
pub const SAMPLE_COLUMNS: [&str; 10] = [
    "level",
    "concurrency",
    "t_s",
    "docs_submitted",
    "submit_docs_per_s",
    "docs_indexed",
    "index_docs_per_s",
    "index_status",
    "docs_accepted",
    "accepted_docs_per_s",
];
pub const INDEX_CELLS: usize = 5;

/// Zero rather than infinity when no time passed: a ticker aborted mid-tick can
/// land a reading in the same instant as the next one, and `t_s` beside the
/// running total is what a consumer re-derives from anyway.
pub fn rate(docs: u64, seconds: f64) -> f64 {
    if seconds > 0.0 {
        docs as f64 / seconds
    } else {
        0.0
    }
}

/// What the workers have handed to ScyllaDB so far, readable while they work.
///
/// Successes are counted apart from failures because the submit series has to
/// mean what `docs` means in the point CSV: an insert that failed will never
/// reach the index, so counting it would read as index lag.
#[derive(Debug, Default)]
pub struct Submitted {
    ok: AtomicU64,
    errors: AtomicU64,
}

impl Submitted {
    pub fn record(&self, succeeded: bool) {
        let counter = if succeeded { &self.ok } else { &self.errors };
        counter.fetch_add(1, Ordering::Relaxed);
    }

    /// One request's worth. Split, because a `_bulk` can land some of what it
    /// carried and reject the rest, and only what landed will reach the index.
    pub fn record_many(&self, landed: u64, failed: u64) {
        if landed > 0 {
            self.ok.fetch_add(landed, Ordering::Relaxed);
        }
        if failed > 0 {
            self.errors.fetch_add(failed, Ordering::Relaxed);
        }
    }

    pub fn ok(&self) -> u64 {
        self.ok.load(Ordering::Relaxed)
    }

    pub fn errors(&self) -> u64 {
        self.errors.load(Ordering::Relaxed)
    }

    pub fn total(&self) -> u64 {
        self.ok() + self.errors()
    }
}

/// One reading of both series, at one moment, in one level.
#[derive(Debug, Clone, PartialEq)]
pub struct Sample {
    pub level: usize,
    pub concurrency: usize,
    pub t_s: f64,
    pub docs_submitted: u64,
    pub submit_docs_per_s: f64,
    /// `None` under `--no-index-watch`: blank cells, never zeros, the same
    /// convention `PointResult::index` follows.
    pub indexed: Option<IndexSample>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct IndexSample {
    pub docs: u64,
    pub docs_per_s: f64,
    pub status: String,
    /// `None` on an engine with one counter. Blank cells rather than zeros, the
    /// same rule the whole index block follows.
    pub accepted: Option<u64>,
    pub accepted_per_s: Option<f64>,
}

/// One level's file. Flushed per row: a level killed part way through leaves
/// every sample it took behind, the promise `CsvSink::append_row` already makes.
pub struct SampleSink {
    destination: String,
    handle: Box<dyn Write + Send>,
}

impl SampleSink {
    pub fn create(path: &Path) -> io::Result<Self> {
        Ok(Self {
            destination: path.display().to_string(),
            handle: Box::new(BufWriter::new(File::create(path)?)),
        })
    }

    pub fn destination(&self) -> &str {
        &self.destination
    }

    /// The same `#` block the point CSV carries, so a series found on its own
    /// is still interpretable.
    pub fn write_preamble(&mut self, preamble: &[String]) -> io::Result<()> {
        for line in preamble {
            self.write_line(line)?;
        }
        self.write_line(&SAMPLE_COLUMNS.join(","))
    }

    pub fn append(&mut self, sample: &Sample) -> io::Result<()> {
        self.write_line(&sample_row(sample))
    }

    fn write_line(&mut self, text: &str) -> io::Result<()> {
        writeln!(self.handle, "{text}")?;
        self.handle.flush()
    }
}

fn sample_row(sample: &Sample) -> String {
    format!(
        "{},{},{:.3},{},{:.1},{}",
        sample.level,
        sample.concurrency,
        sample.t_s,
        sample.docs_submitted,
        sample.submit_docs_per_s,
        index_cells(sample.indexed.as_ref())
    )
}

fn index_cells(indexed: Option<&IndexSample>) -> String {
    let Some(indexed) = indexed else {
        return [""; INDEX_CELLS].join(",");
    };
    format!(
        "{},{:.1},{},{},{}",
        indexed.docs,
        indexed.docs_per_s,
        indexed.status,
        blank_or(indexed.accepted, |docs| docs.to_string()),
        blank_or(indexed.accepted_per_s, |rate| format!("{rate:.1}"))
    )
}

fn blank_or<T>(value: Option<T>, render: impl Fn(T) -> String) -> String {
    value.map_or_else(String::new, render)
}

/// Where the series go, or nothing at all when `--samples-dir` was not given.
pub struct SampleFiles {
    dir: PathBuf,
    preamble: Vec<String>,
    taken: Mutex<HashMap<usize, usize>>,
}

impl SampleFiles {
    pub fn new(dir: &Path) -> io::Result<Self> {
        fs::create_dir_all(dir)?;
        Ok(Self {
            dir: dir.to_path_buf(),
            preamble: Vec::new(),
            taken: Mutex::new(HashMap::new()),
        })
    }

    /// Every level's file repeats the run's facts, because a series is moved
    /// and read one file at a time.
    pub fn with_preamble(self, preamble: Vec<String>) -> Self {
        Self { preamble, ..self }
    }

    pub fn open_level(&self, concurrency: usize) -> io::Result<SampleSink> {
        let mut sink = SampleSink::create(&self.dir.join(self.name_for(concurrency)))?;
        sink.write_preamble(&self.preamble)?;
        Ok(sink)
    }

    /// `--concurrency 8,8,16` is the documented warm-up idiom, so a repeated
    /// level must not overwrite the series of the rung before it.
    fn name_for(&self, concurrency: usize) -> String {
        let mut taken = self.taken.lock().unwrap();
        let repetition = taken.entry(concurrency).or_insert(0);
        *repetition += 1;
        format!("c{concurrency}-{repetition}.csv")
    }
}

/// One level's tape, from before the first insert to the end of settle.
///
/// Shared rather than owned by the sampler task because the tape outlives it:
/// `LevelWatch::finish` aborts the ticker and the settle loop then continues
/// the same series. The rate across that handover is only right if both see the
/// same previous reading.
#[derive(Clone)]
pub struct Tape(Arc<Mutex<Reel>>);

struct Reel {
    level: usize,
    concurrency: usize,
    before: u64,
    before_accepted: u64,
    started: Instant,
    last_submit: Option<Reading>,
    last_index: Option<Reading>,
    last_accepted: Option<Reading>,
    sink: Option<SampleSink>,
}

/// One series' last reading. The two series keep their own, because they are
/// not read at the same moments: the settle loop takes index readings after the
/// submit series has stopped, and the submit-end row carries no index count at
/// all. A shared predecessor would measure each rate against the other's clock.
struct Reading {
    at: Instant,
    docs: u64,
}

impl Tape {
    pub fn new(level: usize, concurrency: usize, sink: Option<SampleSink>) -> Self {
        Self::started_at(Instant::now(), level, concurrency, sink)
    }

    /// The level's start is an argument so a rate can be asserted against a gap
    /// the test chose: what this module claims is documents over the time that
    /// actually passed, and a real clock cannot be held still long enough to
    /// check it.
    pub fn started_at(
        started: Instant,
        level: usize,
        concurrency: usize,
        sink: Option<SampleSink>,
    ) -> Self {
        Self(Arc::new(Mutex::new(Reel {
            level,
            concurrency,
            before: 0,
            before_accepted: 0,
            started,
            last_submit: None,
            last_index: None,
            last_accepted: None,
            sink,
        })))
    }

    /// The index this level inherited, so the series counts only what the level
    /// added — the rule `IndexBuild.docs` follows, so the series ends where the
    /// row does. Both series inherit, because an engine that has already
    /// accepted a million documents did not accept them during this level.
    pub fn inherited(&self, before: u64, before_accepted: u64) {
        let mut reel = self.0.lock().unwrap();
        reel.before = before;
        reel.before_accepted = before_accepted;
    }

    /// Records one reading and hands back the sample it produced, so the stderr
    /// line and the row are rendered from the same numbers.
    pub fn record(&self, submitted: u64, index: Option<&IndexState>) -> Sample {
        self.record_at(Instant::now(), submitted, index)
    }

    /// One instant serves the whole reading — `t_s`, both gaps and what the next
    /// reading is measured against — so a row cannot describe two moments.
    pub fn record_at(&self, now: Instant, submitted: u64, index: Option<&IndexState>) -> Sample {
        let mut reel = self.0.lock().unwrap();
        let sample = reel.sample(now, submitted, index);
        reel.keep(now, &sample);
        sample
    }
}

impl Reel {
    fn sample(&self, now: Instant, submitted: u64, index: Option<&IndexState>) -> Sample {
        Sample {
            level: self.level,
            concurrency: self.concurrency,
            t_s: self.seconds_since(self.started, now),
            docs_submitted: submitted,
            submit_docs_per_s: self.rate_since(self.last_submit.as_ref(), submitted, now),
            indexed: index.map(|state| self.index_sample(now, state)),
        }
    }

    fn index_sample(&self, now: Instant, state: &IndexState) -> IndexSample {
        let docs = state.docs().saturating_sub(self.before);
        let accepted = state
            .accepted()
            .map(|accepted| accepted.saturating_sub(self.before_accepted));
        IndexSample {
            docs,
            docs_per_s: self.rate_since(self.last_index.as_ref(), docs, now),
            status: state.status_word().to_string(),
            accepted,
            accepted_per_s: accepted
                .map(|docs| self.rate_since(self.last_accepted.as_ref(), docs, now)),
        }
    }

    /// The level's own start is the first reading's predecessor: zero documents
    /// at zero seconds, which is what makes the first tick a rate and not a
    /// running total divided by one interval.
    fn rate_since(&self, previous: Option<&Reading>, docs: u64, now: Instant) -> f64 {
        let (before, since) = match previous {
            Some(previous) => (previous.docs, previous.at),
            None => (0, self.started),
        };
        rate(docs.saturating_sub(before), self.seconds_since(since, now))
    }

    fn seconds_since(&self, earlier: Instant, now: Instant) -> f64 {
        now.saturating_duration_since(earlier).as_secs_f64()
    }

    /// A reading the index was not part of leaves the index series where it was.
    /// The submit-end row and every failed poll are such readings, and treating
    /// them as an index of zero would make the next real reading a spike —
    /// landing exactly where the client stops and the index is still draining,
    /// which is the part of the build this series exists to show.
    fn keep(&mut self, now: Instant, sample: &Sample) {
        self.last_submit = Some(Reading {
            at: now,
            docs: sample.docs_submitted,
        });
        if let Some(indexed) = sample.indexed.as_ref() {
            self.last_index = Some(Reading {
                at: now,
                docs: indexed.docs,
            });
            if let Some(accepted) = indexed.accepted {
                self.last_accepted = Some(Reading {
                    at: now,
                    docs: accepted,
                });
            }
        }
        self.write(sample);
    }

    /// A sample is a diagnostic and the point CSV is the record, so a file that
    /// stops accepting rows must not take the level down with it.
    fn write(&mut self, sample: &Sample) {
        if let Some(sink) = self.sink.as_mut() {
            let _ = sink.append(sample);
        }
    }
}

#[cfg(test)]
#[path = "samples_tests.rs"]
mod tests;
