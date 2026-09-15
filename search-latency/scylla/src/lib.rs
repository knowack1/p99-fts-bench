//! Search-latency matrix for ScyllaDB FTS, over two interfaces into one index.
//!
//! `cql` is ScyllaDB's own read path and `vector-store` is the BM25 endpoint
//! with ScyllaDB out of it. The difference between the two is ScyllaDB's read
//! overhead, and it is the only reason this binary has two interfaces rather
//! than one.
pub mod bm25;
pub mod cli;
pub mod cql;
pub mod loader;
