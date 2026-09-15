//! Filling the index before the first query is timed.
//!
//! Every part of this is `build-rate`'s: the `DROP KEYSPACE` and the two gates
//! that make it a reset rather than a hope, the prepared INSERT, the bounded
//! channel and the N workers. What is here is the decision to run exactly one
//! level of it, at one concurrency, and to report what came back as a load
//! rather than as a measurement.
//!
//! **`--load-concurrency` is not a result.** How fast an index builds is the
//! sibling tree's question and it takes a whole ladder to answer; this runs one
//! rung, chosen to be fast rather than to be informative, and the docs/s it
//! prints is only there so an operator can see the bootstrap moving.
use std::sync::Arc;

use anyhow::Result;
use build_rate_core::notes::Notes;
use build_rate_core::samples::Submitted;
use scyllarate::corpus::{self, CorpusSource};
use scyllarate::reset::ResettingInserters;
use scyllarate::sweep::{self, LevelSource};
use search_latency_core::bootstrap::{IndexLoader, LoadReport};
use search_latency_core::search::BoxFuture;

pub struct CqlLoader {
    inserters: ResettingInserters,
    source: CorpusSource,
    concurrency: usize,
    target: String,
    notes: Notes,
}

impl CqlLoader {
    pub fn new(
        inserters: ResettingInserters,
        source: CorpusSource,
        concurrency: usize,
        target: impl Into<String>,
        notes: Notes,
    ) -> Self {
        Self {
            inserters,
            source,
            concurrency,
            target: target.into(),
            notes,
        }
    }

    /// Opening the level is what empties the keyspace and waits for the new
    /// index to reach SERVING at zero documents; the prepared INSERT that comes
    /// back is prepared against the table that reset just created.
    async fn fill(&self) -> Result<LoadReport> {
        let inserter = self.inserters.open().await?;
        let submitted = Arc::new(Submitted::default());
        let point = sweep::measure_at_concurrency(
            &inserter,
            corpus::rows(&self.source)?,
            sweep::loader(),
            self.concurrency,
            &self.notes,
            &submitted,
        )
        .await?;
        Ok(LoadReport {
            docs: point.docs,
            errors: point.errors,
            wall_s: point.wall_s,
            docs_per_s: point.docs_per_s,
        })
    }
}

impl IndexLoader for CqlLoader {
    fn build(&self) -> BoxFuture<'_, Result<LoadReport>> {
        Box::pin(self.fill())
    }

    fn describe(&self) -> String {
        self.target.clone()
    }
}
