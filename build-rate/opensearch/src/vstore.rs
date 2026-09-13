//! `GET /{index}/_stats`, which is where an OpenSearch index build is visible.
//!
//! The same request `ftsbench.samplers.OpenSearchSampler` makes for the engine
//! campaign, so a harness ceiling and an engine number come from one reading
//! rather than two — the rule the ScyllaDB half already follows against the
//! vector-store's status endpoint.
//!
//! **Two counts, and they mean different things.** `docs.count` is what a
//! search would find; it only advances when the index refreshes, so against a
//! `refresh_interval` of 1s or 3s it climbs in steps and is flat in between.
//! `indexing.index_total` is what the engine has accepted, and it climbs
//! smoothly. A watch that had only the first could not tell an index that has
//! stalled from one that has simply not refreshed yet.
//!
//! One endpoint serves both reset gates and the watch: `_stats` answers 404 for
//! an index that is not there, exactly as `HEAD` does, and 503 while a primary
//! is unallocated, exactly as `_count` does.
use anyhow::{Context, Result};
use build_rate_core::index::{BoxFuture, IndexProbe, IndexReading, IndexState};
use opensearch::indices::IndicesStatsParts;
use opensearch::OpenSearch;
use serde_json::Value;

/// What the harness minted, never what the engine said — lowercase, so a reader
/// can tell a derived status from a quoted one.
pub const SEARCHABLE: &str = "searchable";
pub const INDEXING: &str = "indexing";
pub const IDLE: &str = "idle";
pub const UNREADY: &str = "unready";
pub const REFRESHED: &str = "refreshed";

pub struct StatsProbe {
    client: OpenSearch,
    index: String,
    endpoint: String,
    may_ask_for_a_refresh: bool,
}

impl StatsProbe {
    pub fn new(client: OpenSearch, url: &str, index: &str) -> Self {
        Self {
            client,
            index: index.to_string(),
            endpoint: format!("{}/{index}/_stats", url.trim_end_matches('/')),
            may_ask_for_a_refresh: true,
        }
    }

    /// Report what the configured refresh policy delivered, and nothing more.
    pub fn without_a_final_refresh(mut self) -> Self {
        self.may_ask_for_a_refresh = false;
        self
    }

    async fn poll(&self) -> Result<IndexState> {
        let response = self
            .client
            .indices()
            .stats(IndicesStatsParts::Index(&[&self.index]))
            .send()
            .await
            .with_context(|| format!("cannot reach {}", self.endpoint))?;
        if response.status_code() == opensearch::http::StatusCode::NOT_FOUND {
            return Ok(IndexState::Absent);
        }
        let body: Value = response
            .error_for_status_code()
            .with_context(|| format!("{} answered an error", self.endpoint))?
            .json()
            .await
            .with_context(|| format!("{} answered something that is not JSON", self.endpoint))?;
        Ok(reading_of(&body))
    }

    /// Asked once, after the engine has stopped making progress and the
    /// searchable count is still short. Never mid-build: a harness that forced
    /// a refresh while measuring would be changing what it measured.
    async fn ask_for_a_refresh(&self) -> Result<()> {
        self.client
            .indices()
            .refresh(opensearch::indices::IndicesRefreshParts::Index(&[
                &self.index
            ]))
            .send()
            .await
            .with_context(|| format!("cannot refresh index {:?}", self.index))?
            .error_for_status_code()
            .with_context(|| format!("index {:?} refused a refresh", self.index))?;
        Ok(())
    }
}

/// A `_stats` body with no `_all.total` is an index that answered without being
/// allocated — the keep-polling case, not a count of nothing.
fn reading_of(body: &Value) -> IndexState {
    let Some(total) = body.pointer("/_all/total") else {
        return IndexState::Present(IndexReading {
            docs: 0,
            accepted: None,
            status: UNREADY.to_string(),
            ready: false,
        });
    };
    let docs = counter_at(total, "/docs/count");
    let accepted = counter_at(total, "/indexing/index_total");
    IndexState::Present(IndexReading {
        docs,
        accepted: Some(accepted),
        status: status_for(docs, accepted),
        ready: true,
    })
}

/// Derived from the same reply, never from a second request: naming the state
/// must not cost a poll of the system under test.
fn status_for(docs: u64, accepted: u64) -> String {
    if docs >= accepted {
        SEARCHABLE.to_string()
    } else {
        INDEXING.to_string()
    }
}

fn counter_at(total: &Value, path: &str) -> u64 {
    total.pointer(path).and_then(Value::as_u64).unwrap_or(0)
}

impl IndexProbe for StatsProbe {
    fn read(&self) -> BoxFuture<'_, IndexState> {
        Box::pin(async move {
            match self.poll().await {
                Ok(state) => state,
                Err(exc) => IndexState::Unreadable(format!("{exc:#}")),
            }
        })
    }

    fn endpoint(&self) -> &str {
        &self.endpoint
    }

    fn settle_hint(&self) -> BoxFuture<'_, bool> {
        Box::pin(async move {
            if !self.may_ask_for_a_refresh {
                return false;
            }
            self.ask_for_a_refresh().await.is_ok()
        })
    }
}

#[cfg(test)]
#[path = "vstore_tests.rs"]
mod tests;
