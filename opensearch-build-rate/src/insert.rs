//! The one thing a worker does: encode a batch as NDJSON and wait for
//! OpenSearch to answer the `_bulk`.
//!
//! Encoding is inside the timed window on purpose. The CQL half of this bench
//! serializes its row inside `execute_unpaged`, so a request's latency there is
//! also the client's cost of offering it; a bulk measured without its encode
//! would be a request no client could actually have sent.
use std::future::Future;

use anyhow::{Context, Result};
use opensearch::{BulkParts, OpenSearch};
use serde_json::Value;

use crate::bulk::{self, BulkOutcome};
use crate::corpus::DocumentBatch;
use crate::sweep::Inserter;

pub struct BulkInserter {
    client: OpenSearch,
    index: String,
}

impl BulkInserter {
    pub fn new(client: OpenSearch, index: impl Into<String>) -> Self {
        Self {
            client,
            index: index.into(),
        }
    }

    pub fn client(&self) -> &OpenSearch {
        &self.client
    }

    pub fn index(&self) -> &str {
        &self.index
    }

    /// `POST /_bulk` with the index named on every action line, and no query
    /// parameters at all — the request `ftsbench.opensearch_load.send_bulk`
    /// makes, down to the URL. `refresh` is left off rather than set to
    /// `false`: false is already the default for `_bulk`, so sending it would
    /// only put a parameter on the wire that the Python loader does not.
    async fn post(&self, payload: bytes::Bytes, offered: u64) -> Result<BulkOutcome> {
        let response = self
            .client
            .bulk(BulkParts::None)
            .body(vec![payload])
            .send()
            .await
            .with_context(|| format!("a _bulk of {offered} documents never came back"))?
            .error_for_status_code()
            .context("OpenSearch refused the _bulk")?;
        let body: Value = response
            .json()
            .await
            .context("a _bulk reply was not readable JSON")?;
        bulk::read_outcome(&body, offered)
    }
}

impl Inserter for BulkInserter {
    // The explicit `impl Future + Send` is the point: `async fn` in a trait
    // leaves the future's `Send`ness up to the caller, and these futures are
    // spawned onto tokio, which requires it.
    #[allow(clippy::manual_async_fn)]
    fn insert(&self, batch: DocumentBatch) -> impl Future<Output = Result<BulkOutcome>> + Send {
        async move {
            let offered = batch.docs();
            let payload = bulk::ndjson(&batch, &self.index)?;
            self.post(payload, offered).await
        }
    }
}
