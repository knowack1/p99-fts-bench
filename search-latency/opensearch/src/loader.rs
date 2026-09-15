//! Filling the index before the first query is timed.
//!
//! Every part of this is `build-rate`'s: the `DELETE`/`PUT` and the two gates
//! that make it a reset rather than a hope, the analyzer probe that keeps this
//! from becoming a comparison of tokenizers, the NDJSON encode and the
//! `_bulk`. What is here is the decision to run exactly one level of it, at one
//! concurrency and one batch size, and to report what came back as a load
//! rather than as a measurement.
use std::sync::Arc;

use anyhow::Result;
use build_rate_core::notes::Notes;
use build_rate_core::samples::Submitted;
use osrate::corpus::{self, CorpusSource};
use osrate::insert::BulkInserter;
use osrate::reset::{IndexReset, ResettingInserter};
use osrate::sweep::{self, LevelSource};
use search_latency_core::bootstrap::{IndexLoader, LoadReport};
use search_latency_core::search::BoxFuture;

/// How the corpus is offered while the index is being filled. Not a
/// measurement: see the module docs.
#[derive(Debug, Clone, Copy)]
pub struct LoadShape {
    pub concurrency: usize,
    pub batch_size: usize,
    pub queue_depth: usize,
}

pub struct BulkLoader {
    inserters: ResettingInserter<BulkInserter>,
    source: CorpusSource,
    shape: LoadShape,
    target: String,
    notes: Notes,
}

impl BulkLoader {
    pub fn new(
        inserter: Arc<BulkInserter>,
        reset: IndexReset,
        source: CorpusSource,
        shape: LoadShape,
        target: impl Into<String>,
        notes: Notes,
    ) -> Self {
        Self {
            inserters: ResettingInserter::new(inserter, Some(reset)),
            source,
            shape,
            target: target.into(),
            notes,
        }
    }

    /// Opening the level is what deletes the index, recreates it from the
    /// configured mapping and waits for it to answer at zero documents.
    async fn fill(&self) -> Result<LoadReport> {
        let inserter = self.inserters.open().await?;
        let submitted = Arc::new(Submitted::default());
        let point = sweep::measure_at_concurrency(
            &inserter,
            corpus::batches(&self.source, self.shape.batch_size)?,
            sweep::loader(self.shape.batch_size, self.shape.queue_depth),
            self.shape.concurrency,
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

impl IndexLoader for BulkLoader {
    fn build(&self) -> BoxFuture<'_, Result<LoadReport>> {
        Box::pin(self.fill())
    }

    fn describe(&self) -> String {
        self.target.clone()
    }
}
