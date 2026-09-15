//! ScyllaDB's own read path: `SELECT ... WHERE BM25(col, 'q') > 0 ORDER BY
//! BM25(col, 'q') LIMIT n`.
//!
//! The shape is fixed by what M1 supports and is not a knob: the `LIMIT` is
//! mandatory, the same term has to appear in the `WHERE` and in the `ORDER BY`,
//! there may be no other `WHERE` restriction, and `BM25()` is not projectable.
//!
//! **The term is written into the statement rather than bound.** The M1 rule
//! about the identical term is unverified for bound parameters, and a harness
//! that guessed wrong there would be measuring a query shape the engine does
//! not actually promise. Escaping is plain CQL single-quote doubling.
//!
//! **Two statement modes, because they are two different measurements.**
//! `literal` re-parses the statement on the coordinator for every request,
//! which is what OpenSearch's `query_string` does on its side and what this
//! bench's Python read arm has always done — it is the default for both those
//! reasons. `prepared` prepares each distinct query once before the matrix
//! starts and pays the parse never again, which is what an application does.
//! Neither is wrong; mixing them in one chart is, so the mode is a header fact.
use std::collections::HashMap;
use std::sync::Arc;

use anyhow::{Context, Result};
use scylla::client::session::Session;
use scylla::response::query_result::QueryRowsResult;
use scylla::statement::prepared::PreparedStatement;
use search_latency_core::search::{BoxFuture, Found, Searcher, CQL};
use uuid::Uuid;

pub const IDENTITY_PROJECTION: &str = "article_id";
/// What an application asks for, and what the OpenSearch half projects when it
/// is asked the same way — the same two columns under the same two names, so
/// the two engines return the same thing.
pub const DOCUMENT_PROJECTION: &str = "article_id, title, body";
pub const LITERAL: &str = "literal";
pub const PREPARED: &str = "prepared";

/// How the statement reaches the coordinator.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StatementMode {
    Literal,
    Prepared,
}

impl StatementMode {
    pub fn name(self) -> &'static str {
        match self {
            Self::Literal => LITERAL,
            Self::Prepared => PREPARED,
        }
    }
}

/// Everything about the statement except the query text.
#[derive(Debug, Clone)]
pub struct QueryShape {
    pub table: String,
    pub column: String,
    pub limit: usize,
    pub fetch_documents: bool,
}

impl QueryShape {
    pub fn statement(&self, query: &str) -> String {
        let scoring = bm25_call(&self.column, query);
        format!(
            "SELECT {} FROM {} WHERE {scoring} > 0 ORDER BY {scoring} LIMIT {}",
            self.projection(),
            self.table,
            self.limit
        )
    }

    fn projection(&self) -> &'static str {
        if self.fetch_documents {
            DOCUMENT_PROJECTION
        } else {
            IDENTITY_PROJECTION
        }
    }
}

fn bm25_call(column: &str, query: &str) -> String {
    format!("BM25({column}, '{}')", escape(query))
}

/// CQL escapes a single quote by doubling it, and a query set built from real
/// article text does contain apostrophes.
pub fn escape(query: &str) -> String {
    query.replace('\'', "''")
}

pub struct CqlSearcher {
    session: Arc<Session>,
    shape: QueryShape,
    mode: StatementMode,
    prepared: HashMap<String, PreparedStatement>,
    endpoint: String,
}

impl CqlSearcher {
    /// Every query the matrix will ask is known before it starts, which is what
    /// makes `prepared` a mode this can offer at all: preparing lazily inside
    /// the timed loop would put one round trip on whichever cell saw a query
    /// first.
    pub async fn open(
        session: Arc<Session>,
        shape: QueryShape,
        mode: StatementMode,
        queries: &[&str],
        endpoint: impl Into<String>,
    ) -> Result<Self> {
        let prepared = match mode {
            StatementMode::Literal => HashMap::new(),
            StatementMode::Prepared => prepare_every(&session, &shape, queries).await?,
        };
        Ok(Self {
            session,
            shape,
            mode,
            prepared,
            endpoint: endpoint.into(),
        })
    }

    pub fn mode(&self) -> StatementMode {
        self.mode
    }

    async fn ask(&self, query: &str) -> Result<Found> {
        let rows = self.rows(query).await?;
        self.count(&rows)
    }

    async fn rows(&self, query: &str) -> Result<QueryRowsResult> {
        let result = match prepared_for(self.mode, &self.prepared, query)? {
            Some(statement) => self.session.execute_unpaged(statement, &[]).await,
            None => {
                self.session
                    .query_unpaged(self.shape.statement(query), &[])
                    .await
            }
        };
        result
            .with_context(|| format!("searching for {query:?} failed"))?
            .into_rows_result()
            .with_context(|| format!("searching for {query:?} answered no rows at all"))
    }

    /// Identities are counted off the frame; documents are deserialized out of
    /// it. That is the point of the second branch rather than an artefact of
    /// it: deserializing into `String` is what copies the article text into the
    /// client, and a run that projected `title` and `body` and never touched
    /// them would be timing a transfer nobody paid for.
    fn count(&self, rows: &QueryRowsResult) -> Result<Found> {
        if !self.shape.fetch_documents {
            return Ok(Found::new(rows.rows_num()));
        }
        let mut hits = 0;
        for row in rows.rows::<(Uuid, String, String)>()? {
            row?;
            hits += 1;
        }
        Ok(Found::new(hits))
    }
}

/// The mode decides, and the map only answers for the mode that has one.
///
/// Asking the map alone would make a miss in `Prepared` mode fall back to
/// `literal` silently — the header would still say `statement=prepared`, the
/// row would carry a coordinator parse it claims not to have paid for, and
/// nothing in the output could tell. A searcher opened for one query set and
/// asked another's query is a bug; this is where it becomes a loud one.
fn prepared_for<'a>(
    mode: StatementMode,
    prepared: &'a HashMap<String, PreparedStatement>,
    query: &str,
) -> Result<Option<&'a PreparedStatement>> {
    match mode {
        StatementMode::Literal => Ok(None),
        StatementMode::Prepared => Ok(Some(prepared.get(query).with_context(|| {
            format!(
                "no prepared statement for {query:?}: this searcher was opened for \
                 a different set of queries"
            )
        })?)),
    }
}

async fn prepare_every(
    session: &Session,
    shape: &QueryShape,
    queries: &[&str],
) -> Result<HashMap<String, PreparedStatement>> {
    let mut prepared = HashMap::with_capacity(queries.len());
    for query in queries {
        prepared.insert(
            (*query).to_string(),
            prepare_one(session, shape, query).await?,
        );
    }
    Ok(prepared)
}

async fn prepare_one(
    session: &Session,
    shape: &QueryShape,
    query: &str,
) -> Result<PreparedStatement> {
    session
        .prepare(shape.statement(query))
        .await
        .with_context(|| format!("cannot prepare the search for {query:?}"))
}

impl Searcher for CqlSearcher {
    fn search<'a>(&'a self, query: &'a str) -> BoxFuture<'a, Result<Found>> {
        Box::pin(self.ask(query))
    }

    fn interface(&self) -> &'static str {
        CQL
    }

    fn endpoint(&self) -> &str {
        &self.endpoint
    }
}

#[cfg(test)]
#[path = "cql_tests.rs"]
mod tests;
