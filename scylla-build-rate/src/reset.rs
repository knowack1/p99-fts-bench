//! Emptying the index before a level, and refusing to load until it is back.
//!
//! Every level builds from zero documents, because `article_id` is the corpus's
//! deterministic uuid5: without a reset the second level rewrites the first
//! level's rows, the index count does not move, and the build rate of every rung
//! but the first is unmeasurable. `tools/build_rate_point.sh` solves that
//! externally by running one point per invocation; this tool runs the whole
//! ladder in one process, so it does the same cycle itself.
//!
//! **This issues DDL and it destroys data.** `DROP KEYSPACE` is the whole reset
//! — the table and the index go with it — and it is on unless `--no-reset` is
//! given.
//!
//! The two gates are the reason this is a measurement rather than a hope. A
//! `DROP` returns before the vector-store has noticed it, so without gate A the
//! second gate could match the index that was just dropped; and `CREATE CUSTOM
//! INDEX` returns before the index is queryable, so without gate B the load
//! would start against an index that is still registering. Each gate fails by
//! name and says what it last saw, because a reset that quietly did not happen
//! produces a complete, plausible, wrong build rate.
use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::{bail, Result};
use scylla::client::session::Session;

use crate::insert::CqlInserter;
use crate::notes::Notes;
use crate::session;
use crate::sweep::{BoxFuture, InserterSource};
use crate::vstore::{IndexProbe, IndexState};

/// Written out here rather than read from `scylladb/schema.cql`, which hardcodes
/// `wiki` and `articles`: only a statement built from the flags can honour
/// `--keyspace`, `--table` and `--vs-index`. A test holds these to that file.
pub const DROP_KEYSPACE: &str = "DROP KEYSPACE IF EXISTS {keyspace}";
pub const CREATE_KEYSPACE: &str = "CREATE KEYSPACE {keyspace} WITH replication = \
{'class': 'NetworkTopologyStrategy', 'replication_factor': 1}";
pub const CREATE_TABLE: &str = "CREATE TABLE {keyspace}.{table} (\
article_id uuid PRIMARY KEY, page_id bigint, title text, body text)";
pub const CREATE_INDEX: &str = "CREATE CUSTOM INDEX {index} ON {keyspace}.{table}(body) \
USING 'fulltext_index'";

#[derive(Debug, Clone)]
pub struct ResetPlan {
    pub keyspace: String,
    pub table: String,
    pub index: String,
}

impl ResetPlan {
    /// No `IF NOT EXISTS` on the creates: after the drop nothing should be
    /// there, and a create that silently succeeded against a survivor would
    /// hand this level the previous level's documents.
    pub fn drop_statement(&self) -> String {
        self.fill(DROP_KEYSPACE)
    }

    pub fn create_statements(&self) -> Vec<String> {
        [CREATE_KEYSPACE, CREATE_TABLE, CREATE_INDEX]
            .into_iter()
            .map(|template| self.fill(template))
            .collect()
    }

    fn fill(&self, template: &str) -> String {
        template
            .replace("{keyspace}", &self.keyspace)
            .replace("{table}", &self.table)
            .replace("{index}", &self.index)
    }
}

#[derive(Debug, Clone)]
pub struct GateTiming {
    pub poll_interval: Duration,
    pub timeout: Duration,
}

/// Polls the vector-store until the index reaches a named state.
pub struct Gate<'a> {
    probe: &'a IndexProbe,
    timing: &'a GateTiming,
    notes: &'a Notes,
}

impl<'a> Gate<'a> {
    pub fn new(probe: &'a IndexProbe, timing: &'a GateTiming, notes: &'a Notes) -> Self {
        Self {
            probe,
            timing,
            notes,
        }
    }

    /// The drop has reached the vector-store. Phrased as "no longer serving"
    /// rather than "404" so it does not depend on which code the vector-store
    /// picks for an index it no longer has.
    pub async fn await_dropped(&self) -> Result<()> {
        self.await_state("the dropped index to disappear", |state| {
            state.serving().is_none()
        })
        .await
    }

    /// The new index exists, is queryable, and holds nothing. All three:
    /// SERVING alone could still be the pre-drop index, and a count of zero
    /// alone could be an index that is not yet answering queries.
    pub async fn await_empty_and_serving(&self) -> Result<()> {
        self.await_state("the new index to reach SERVING at 0 documents", |state| {
            state.serving().is_some_and(|status| status.count == 0)
        })
        .await
    }

    async fn await_state(&self, what: &str, reached: impl Fn(&IndexState) -> bool) -> Result<()> {
        let deadline = Instant::now() + self.timing.timeout;
        let mut last = "not polled yet".to_string();
        while Instant::now() < deadline {
            last = self.look(&reached).await?;
            if last.is_empty() {
                return Ok(());
            }
            tokio::time::sleep(self.timing.poll_interval).await;
        }
        bail!(
            "timed out after {:.0}s waiting for {what}; the index was last {last} at {}",
            self.timing.timeout.as_secs_f64(),
            self.probe.status_url()
        )
    }

    /// An empty description means the state was reached. A failed poll is
    /// described rather than raised: the vector-store is legitimately
    /// unavailable for a moment while a keyspace drop propagates, and the
    /// deadline is what decides that the moment has lasted too long.
    async fn look(&self, reached: &impl Fn(&IndexState) -> bool) -> Result<String> {
        match self.probe.status().await {
            Ok(state) if reached(&state) => Ok(String::new()),
            Ok(state) => Ok(state.describe()),
            Err(exc) => Ok(format!("unreadable ({exc:#})")),
        }
    }

    fn say(&self, message: &str) {
        self.notes.say(message);
    }
}

/// One inserter per level, with the index emptied before it.
pub struct ResettingInserters {
    session: Arc<Session>,
    plan: ResetPlan,
    probe: Option<Arc<IndexProbe>>,
    timing: GateTiming,
    notes: Notes,
    reset: bool,
}

impl ResettingInserters {
    pub fn new(
        session: Arc<Session>,
        plan: ResetPlan,
        probe: Option<Arc<IndexProbe>>,
        timing: GateTiming,
        notes: Notes,
        reset: bool,
    ) -> Self {
        Self {
            session,
            plan,
            probe,
            timing,
            notes,
            reset,
        }
    }

    async fn empty_the_index(&self) -> Result<()> {
        let Some(probe) = self.probe.as_deref() else {
            return Ok(());
        };
        let gate = Gate::new(probe, &self.timing, &self.notes);
        gate.say(&format!("  resetting {}", self.plan.keyspace));
        self.execute(&self.plan.drop_statement()).await?;
        gate.await_dropped().await?;
        for statement in self.plan.create_statements() {
            self.execute(&statement).await?;
        }
        self.session
            .use_keyspace(&self.plan.keyspace, false)
            .await?;
        gate.await_empty_and_serving().await?;
        gate.say("  index is SERVING at 0 documents");
        Ok(())
    }

    async fn execute(&self, statement: &str) -> Result<()> {
        self.session.query_unpaged(statement, &[]).await?;
        Ok(())
    }
}

impl InserterSource for ResettingInserters {
    type Inserter = CqlInserter;

    /// The prepared statement is rebuilt per level because the reset destroyed
    /// the table the previous one was prepared against.
    fn open(&self) -> BoxFuture<'_, Result<Arc<CqlInserter>>> {
        Box::pin(async move {
            if self.reset {
                self.empty_the_index().await?;
            }
            let statement = session::prepare_insert(&self.session, &self.plan.table).await?;
            Ok(Arc::new(CqlInserter::new(
                Arc::clone(&self.session),
                statement,
            )))
        })
    }
}

#[cfg(test)]
#[path = "reset_tests.rs"]
mod tests;
