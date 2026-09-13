//! One bounded channel, N worker tasks, one point per concurrency level.
//! Shared: see `build_rate_core::sweep`.
pub use build_rate_core::sweep::{
    measure_at_concurrency, run_sweep, summarize, Accepted, BoxFuture, Cancel, Counters, Inserter,
    LevelSource, Loader, OnPoint, Point, SameInserter, Shape, Source, Watchers, WorkItem,
    QUEUE_DEPTH_PER_WORKER,
};

use crate::report::ENGINE;

/// A `_bulk` carries `batch_size` documents, so documents in flight is
/// `concurrency * batch_size` and the queue is bounded in requests: at
/// `batch=512` a depth in the tens would hold the whole corpus.
pub fn loader(batch_size: usize, queue_depth: usize) -> Loader {
    Loader {
        engine: ENGINE,
        shape: Shape {
            batch_size,
            queue_depth,
        },
    }
}
