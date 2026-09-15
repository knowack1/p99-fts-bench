//! Accept-and-discard OpenSearch and ScyllaDB endpoints: the instrument a
//! client ceiling is measured against.
//!
//! A client's own ceiling cannot be measured against a real engine. At the
//! ~11.7k docs/s where the engine saturates, the engine is what the number
//! describes, which is how a loader's core-bound thresholds came to be
//! extrapolations from a client that no longer exists
//! (`BUILD-RATE-MATRIX-PLAN.md`, Phase 0). So: an endpoint that answers
//! correctly, stores nothing, and cannot be the constraint.
//!
//! ```text
//! engine-mock --mode http --port 9200
//! engine-mock --mode cql  --port 9042
//! ```
//!
//! `--delay-ms` is the other half of the instrument. A mock with no delay makes
//! the loader client-bound by construction, which is the positive example a
//! generator gate has never had; the same mock with a delay puts the constraint
//! back outside the client, which is the negative one. A gate whose job is to
//! catch a condition we hope not to meet cannot be tested any other way.
//!
//! **What it is not.** No storage, no consistency, no schema, no relevance. A
//! run against this measures the loader and nothing else, and no number taken
//! from one belongs beside an engine result.
//!
//! **Why it is Rust.** This is a port of `ftsbench/null_sink*.py`, which served
//! every connection from one asyncio loop on one thread. That made the
//! instrument's own CPU a documented gate of every run against it — the HTTP
//! half saturated one core at ~61,000 documents per second, and a loader that
//! went faster was measuring the sink. Here each connection is a tokio task and
//! the runtime spreads them over every core the machine reports.
pub mod cli;
pub mod conn;
pub mod counters;
pub mod cql;
pub mod cql_wire;
pub mod http_wire;
pub mod index;
pub mod opensearch;
pub mod provenance;
pub mod run;
pub mod server;
pub mod tcp;
pub mod vstore;
