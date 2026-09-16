//! The distribution behind each cell's three percentiles, one file per cell.
//!
//! Off unless `--latencies-dir` is given, and worth giving whenever a run will
//! be repeated: **percentiles do not average**. Three repeats of a cell produce
//! three p99s, and the p99 of the three runs together is not their mean — it
//! can only be computed from the samples. A consumer that wants to merge
//! repeats, or draw a full latency curve rather than three points off it, needs
//! these files; one that only wants the four charts does not.
//!
//! Rows are ordered ascending by `latency_ms`, the order the cell already
//! sorted them in to take its percentiles — not chronological, and a reader
//! must not infer drift from row order. The `elapsed_s` column carries the
//! time series instead: it is seconds since the cell's own measured window
//! started, so sorting a copy of the rows by that column recovers arrival
//! order, including across concurrent workers, without re-running anything.
use std::collections::HashMap;
use std::fs::{self, File};
use std::io::{self, BufWriter, Write};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use crate::cell::Sample;

pub const LATENCY_COLUMN: &str = "latency_ms";
pub const ELAPSED_COLUMN: &str = "elapsed_s";

/// Where the distributions go, or nothing at all when the flag was absent.
pub struct LatencyFiles {
    dir: PathBuf,
    preamble: Vec<String>,
    taken: Mutex<HashMap<String, usize>>,
}

impl LatencyFiles {
    pub fn new(dir: &Path) -> io::Result<Self> {
        fs::create_dir_all(dir)?;
        Ok(Self {
            dir: dir.to_path_buf(),
            preamble: Vec::new(),
            taken: Mutex::new(HashMap::new()),
        })
    }

    /// Every cell's file repeats the run's facts, because a distribution is
    /// moved and read one file at a time.
    pub fn with_preamble(self, preamble: Vec<String>) -> Self {
        Self { preamble, ..self }
    }

    pub fn write_cell(
        &self,
        class: &str,
        concurrency: usize,
        samples: &[Sample],
    ) -> io::Result<PathBuf> {
        let path = self.dir.join(self.name_for(class, concurrency));
        let mut sink = BufWriter::new(File::create(&path)?);
        for line in &self.preamble {
            writeln!(sink, "{line}")?;
        }
        writeln!(sink, "{LATENCY_COLUMN},{ELAPSED_COLUMN}")?;
        for sample in samples {
            writeln!(sink, "{:.3},{:.3}", sample.latency_ms, sample.elapsed_s)?;
        }
        sink.flush()?;
        Ok(path)
    }

    /// `--concurrency 8,16,8,16` is how a matrix interleaves two traversals, so
    /// a repeated cell must not overwrite the distribution of the one before
    /// it.
    ///
    /// A poisoned lock is recovered from rather than propagated: the map behind
    /// it is a counter per cell name, nothing that panicked could have left it
    /// inconsistent, and turning somebody else's panic into a lost matrix is a
    /// worse outcome than a file named `-2` after a `-1` that never landed.
    fn name_for(&self, class: &str, concurrency: usize) -> String {
        let stem = format!("{class}-c{concurrency}");
        let mut taken = self.taken.lock().unwrap_or_else(|held| held.into_inner());
        let repetition = taken.entry(stem.clone()).or_insert(0);
        *repetition += 1;
        format!("{stem}-{repetition}.csv")
    }
}

#[cfg(test)]
#[path = "latencies_tests.rs"]
mod tests;
