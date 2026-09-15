//! The vector-store index-status endpoint, which is where an FTS index build is
//! actually visible.
//!
//! A completed CQL write says nothing about how many documents reached the
//! index: rows land in the base table first, and the vector-store then either
//! bootstrap-scans the table or tails CDC to catch up. `count` is the number of
//! documents present in the Tantivy index and `status` is `SERVING` once it is
//! queryable — the same two fields `ftsbench.samplers.ScyllaSampler` reads, so
//! the harness ceiling and the engine number come from one reading rather than
//! two.
use std::time::Duration;

use anyhow::{Context, Result};
pub use build_rate_core::index::{IndexProbe, IndexState};

use build_rate_core::index::{BoxFuture, IndexReading};
use serde::Deserialize;

use crate::session::UNKNOWN;

pub const DEFAULT_VS_URL: &str = "http://127.0.0.1:6080";
pub const DEFAULT_VS_INDEX: &str = "articles_body_fts";
pub const SERVING: &str = "SERVING";

#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct IndexStatus {
    #[serde(default)]
    pub count: u64,
    #[serde(default = "unknown_status")]
    pub status: String,
}

fn unknown_status() -> String {
    UNKNOWN.to_string()
}

impl IndexStatus {
    pub fn is_serving(&self) -> bool {
        self.status == SERVING
    }

    /// The vector-store counts what is in the Tantivy index, which is what a
    /// query would find, and it has no second counter for what the base table
    /// has accepted — hence `accepted: None` rather than a zero that would read
    /// as an engine that had taken nothing in.
    fn reading(self) -> IndexReading {
        IndexReading {
            docs: self.count,
            accepted: None,
            ready: self.is_serving(),
            status: self.status,
        }
    }
}

pub struct VectorStoreProbe {
    client: reqwest::Client,
    status_url: String,
    info_url: String,
}

impl VectorStoreProbe {
    pub fn new(base_url: &str, keyspace: &str, index: &str, timeout: Duration) -> Result<Self> {
        let base = base_url.trim_end_matches('/');
        Ok(Self {
            client: reqwest::Client::builder()
                .timeout(timeout)
                .build()
                .context("cannot build an HTTP client for the vector-store")?,
            status_url: format!("{base}/api/v1/indexes/{keyspace}/{index}/status"),
            info_url: format!("{base}/api/v1/info"),
        })
    }

    pub fn status_url(&self) -> &str {
        &self.status_url
    }

    /// A 404 is `Absent`; anything else non-2xx is an error rather than an
    /// absence, because "the index is not there" and "the vector-store is not
    /// answering" are the two states a reset gate must never confuse.
    async fn poll(&self) -> Result<IndexState> {
        let response = self
            .client
            .get(&self.status_url)
            .send()
            .await
            .with_context(|| format!("cannot reach {}", self.status_url))?;
        if response.status() == reqwest::StatusCode::NOT_FOUND {
            return Ok(IndexState::Absent);
        }
        let response = response
            .error_for_status()
            .with_context(|| format!("{} answered an error", self.status_url))?;
        Ok(IndexState::Present(
            response
                .json::<IndexStatus>()
                .await
                .with_context(|| {
                    format!(
                        "{} answered something that is not an index status",
                        self.status_url
                    )
                })?
                .reading(),
        ))
    }

    /// `unknown` rather than a failure: the version annotates the CSV header and
    /// must never be the reason a sweep does not start.
    pub async fn version(&self) -> String {
        match self.read_version().await {
            Ok(version) => version,
            Err(_) => UNKNOWN.to_string(),
        }
    }

    async fn read_version(&self) -> Result<String> {
        let info: serde_json::Value = self
            .client
            .get(&self.info_url)
            .send()
            .await?
            .error_for_status()?
            .json()
            .await?;
        Ok(info
            .get("version")
            .and_then(|value| value.as_str())
            .unwrap_or(UNKNOWN)
            .to_string())
    }
}

impl IndexProbe for VectorStoreProbe {
    fn read(&self) -> BoxFuture<'_, IndexState> {
        Box::pin(async move {
            match self.poll().await {
                Ok(state) => state,
                Err(exc) => IndexState::Unreadable(format!("{exc:#}")),
            }
        })
    }

    fn endpoint(&self) -> &str {
        &self.status_url
    }
}

#[cfg(test)]
#[path = "vstore_tests.rs"]
mod tests;
