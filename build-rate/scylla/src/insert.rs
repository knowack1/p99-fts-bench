//! The one thing a worker does: bind a document to the prepared INSERT and wait
//! for ScyllaDB to acknowledge it.
//!
//! `execute_unpaged` on a prepared statement is what carries the routing key, so
//! this is also where token- and shard-aware routing actually happens.
use std::future::Future;
use std::sync::Arc;

use anyhow::Result;
use scylla::client::session::Session;
use scylla::statement::prepared::PreparedStatement;

use crate::corpus::InsertParams;
use crate::sweep::Inserter;

pub struct CqlInserter {
    session: Arc<Session>,
    statement: PreparedStatement,
}

impl CqlInserter {
    /// The session is shared rather than owned: it outlives any one level,
    /// while the prepared statement does not — a reset drops the table it was
    /// prepared against, so each level gets a fresh inserter over the same
    /// connection pool.
    pub fn new(session: Arc<Session>, statement: PreparedStatement) -> Self {
        Self { session, statement }
    }

    pub fn session(&self) -> &Session {
        &self.session
    }
}

impl Inserter for CqlInserter {
    // The explicit `impl Future + Send` is the point: `async fn` in a trait
    // leaves the future's `Send`ness up to the caller, and these futures are
    // spawned onto tokio, which requires it.
    #[allow(clippy::manual_async_fn)]
    fn insert(&self, params: InsertParams) -> impl Future<Output = Result<()>> + Send {
        async move {
            self.session
                .execute_unpaged(&self.statement, params)
                .await?;
            Ok(())
        }
    }
}
