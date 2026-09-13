//! One bounded channel, N worker tasks, one point per concurrency level.
//! Shared: see `build_rate_core::sweep`.
pub use build_rate_core::sweep::{
    measure_at_concurrency, run_sweep, summarize, Accepted, Cancel, Counters, Inserter, Loader,
    OnPoint, Point, Shape, Source, Watchers, WorkItem, BoxFuture, LevelSource,
    QUEUE_DEPTH_PER_WORKER,
};

use build_rate_core::sweep::Loader as CoreLoader;

use crate::report::{BATCH_SIZE, ENGINE};

/// One document per prepared INSERT, buffered ten deep per worker — the shape
/// this half has always offered and not a knob. A CQL `BATCH` is a different
/// write path and would read as parity with a `_bulk` that it is not.
pub fn loader() -> CoreLoader {
    CoreLoader {
        engine: ENGINE,
        shape: Shape {
            batch_size: BATCH_SIZE,
            queue_depth: QUEUE_DEPTH_PER_WORKER,
        },
    }
}

#[cfg(test)]
#[path = "sweep_tests.rs"]
mod tests;
