//! The per-second series behind the per-level numbers. Shared: see
//! `build_rate_core::samples`.
pub use build_rate_core::samples::{
    rate, IndexSample, Sample, SampleFiles, SampleSink, Submitted, Tape, INDEX_CELLS,
    SAMPLE_COLUMNS,
};
