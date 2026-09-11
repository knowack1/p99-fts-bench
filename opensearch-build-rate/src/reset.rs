//! Emptying the index before a level, and creating the index this tool
//! measures.
//!
//! Every level builds from zero documents, because `_id` is the corpus's page
//! id: without a reset the second level rewrites the first level's documents,
//! the index does not grow, and every rung but the first measures Lucene's
//! update path — a delete plus an insert, plus the merge work of the tombstones
//! — instead of a cold load. `tools/build_rate_point.sh` solves that externally
//! by running one point per invocation; this tool runs the whole ladder in one
//! process, so it does the same cycle itself.
//!
//! **This issues DDL and it destroys data.** `DELETE /{index}` is the whole
//! reset, and it is on unless `--no-reset` is given.
//!
//! The two gates are the reason this is a measurement rather than a hope. A
//! `DELETE` can still be settling when the `PUT` is sent, so without gate A the
//! create could lose the race and hand this level the last level's documents;
//! and a created index answers before its primary is allocated, so without gate
//! B the load would start against an index that is not serving yet. Each gate
//! fails by name and says what it last saw, because a reset that quietly did
//! not happen produces a complete, plausible, wrong build rate.
use std::path::Path;
use std::time::{Duration, Instant};

use anyhow::{bail, Context, Result};
use opensearch::indices::{IndicesCreateParts, IndicesDeleteParts};
use opensearch::OpenSearch;
use serde_json::Value;

use crate::client;
use crate::notes::Notes;
use crate::sweep::{BeforeLevel, BoxFuture};

/// Embedded rather than read from a path at run time, so that the mapping this
/// tool applies cannot drift from the repo's and a bare run needs no argument.
/// `include_str!` is what guarantees it: these are the same bytes
/// `opensearch/create_index.sh` PUTs.
pub const RAMINDEX_CONFIG: &str = include_str!("../../opensearch/index-config-ramindex.json");
pub const DISK_CONFIG: &str = include_str!("../../opensearch/index-config.json");

/// The RAM/ScyllaDB-parity mapping is the default: same `m1_parity` analyzer,
/// `_source` disabled so the index carries postings and ids only, the way
/// Tantivy's schema does. `OPENSEARCH-RAM-INDEX.md` describes the other half of
/// that parity — the tmpfs under the data path — which is a compose knob no
/// client can set, so `source_enabled=false` in the header is not by itself
/// evidence that the segments are in RAM.
pub const RAMINDEX: &str = "ramindex";
pub const DISK: &str = "disk";
pub const DEFAULT_INDEX_CONFIG: &str = RAMINDEX;

pub const PARITY_ANALYZER: &str = "m1_parity";
/// One probe, chosen from `opensearch/verify_analyzer.sh` because it covers two
/// divergence classes at once: a dotted abbreviation the UAX#29 `standard`
/// tokenizer would keep whole, and a stop word that has to leave its position
/// gap behind for exact phrases to line up.
pub const ANALYZER_PROBE: &str = "The U.S. Army in Washington D.C.";
pub const ANALYZER_PROBE_TOKENS: &str = "1:u 2:s 3:army 5:washington 6:d 7:c";

const REFRESH_INTERVAL: &str = "refresh_interval";
const SETTINGS_INDEX: &str = "/settings/index";

/// What the index is created from: a name for the CSV header and a body for the
/// `PUT`.
#[derive(Debug, Clone, PartialEq)]
pub struct IndexConfig {
    name: String,
    body: Value,
}

impl IndexConfig {
    /// A name selects one of the embedded configs; anything else is a path, and
    /// a path that cannot be read fails the run. `create_index.sh` creates a
    /// default-configured index when its own config read fails, which is a
    /// silently wrong index rather than an error — do not reproduce that.
    pub fn select(choice: &str) -> Result<Self> {
        match choice {
            RAMINDEX => Self::parse(RAMINDEX, RAMINDEX_CONFIG),
            DISK => Self::parse(DISK, DISK_CONFIG),
            path => Self::read(Path::new(path)),
        }
    }

    fn read(path: &Path) -> Result<Self> {
        let name = path.display().to_string();
        let text = std::fs::read_to_string(path).with_context(|| {
            format!("cannot read the index config {name}\nnamed configs are {RAMINDEX} and {DISK}")
        })?;
        Self::parse(&name, &text)
    }

    fn parse(name: &str, text: &str) -> Result<Self> {
        Ok(Self {
            name: name.to_string(),
            body: serde_json::from_str(text)
                .with_context(|| format!("the index config {name} is not JSON"))?,
        })
    }

    /// Applied at creation rather than by a later `_settings` PUT, the choice
    /// `opensearch/create_index.sh` documents: there is then no window in which
    /// documents were indexed under the other interval.
    pub fn with_refresh_interval(mut self, interval: Option<&str>) -> Result<Self> {
        let Some(interval) = interval else {
            return Ok(self);
        };
        let settings = self
            .body
            .pointer_mut(SETTINGS_INDEX)
            .and_then(Value::as_object_mut)
            .with_context(|| {
                format!(
                    "the index config {} has no settings.index to set {REFRESH_INTERVAL} on",
                    self.name
                )
            })?;
        settings.insert(
            REFRESH_INTERVAL.to_string(),
            Value::String(interval.to_string()),
        );
        Ok(self)
    }

    pub fn name(&self) -> &str {
        &self.name
    }

    pub fn body(&self) -> &Value {
        &self.body
    }

    /// The analyzer probe only means something against a config that asked for
    /// the parity analyzer; a hand-written config is free not to.
    pub fn declares_parity_analyzer(&self) -> bool {
        self.body
            .pointer("/settings/analysis/analyzer")
            .and_then(Value::as_object)
            .is_some_and(|analyzers| analyzers.contains_key(PARITY_ANALYZER))
    }
}

#[derive(Debug, Clone)]
pub struct GateTiming {
    pub poll_interval: Duration,
    pub timeout: Duration,
}

/// What one poll saw. `Unreadable` is a state rather than an error: the engine
/// is legitimately unable to answer for a moment while a delete settles or a
/// primary is allocated, and the deadline is what decides that the moment has
/// lasted too long.
#[derive(Debug, Clone, PartialEq)]
pub enum IndexState {
    Absent,
    Present { docs: u64 },
    Unreadable(String),
}

impl IndexState {
    pub fn describe(&self) -> String {
        match self {
            Self::Absent => "absent".to_string(),
            Self::Present { docs } => format!("present with {docs} document(s)"),
            Self::Unreadable(why) => format!("unreadable ({why})"),
        }
    }

    pub fn is_absent(&self) -> bool {
        matches!(self, Self::Absent)
    }

    pub fn is_empty(&self) -> bool {
        matches!(self, Self::Present { docs: 0 })
    }
}

/// Polls the endpoint until the index reaches a named state.
pub struct Gate<'a> {
    client: &'a OpenSearch,
    index: &'a str,
    url: &'a str,
    timing: &'a GateTiming,
}

impl<'a> Gate<'a> {
    pub fn new(
        client: &'a OpenSearch,
        index: &'a str,
        url: &'a str,
        timing: &'a GateTiming,
    ) -> Self {
        Self {
            client,
            index,
            url,
            timing,
        }
    }

    /// The delete has landed. Phrased as "absent" rather than "404" so it does
    /// not depend on which code the endpoint picks for an index it no longer
    /// has.
    pub async fn await_absent(&self) -> Result<()> {
        self.await_state("the deleted index to disappear", IndexState::is_absent)
            .await
    }

    /// The new index answers queries and holds nothing. Both: a `HEAD` that
    /// says present could still be the pre-delete index, and a count of zero
    /// cannot be read from an index whose primary is not allocated.
    pub async fn await_empty(&self) -> Result<()> {
        self.await_state(
            "the new index to answer at 0 documents",
            IndexState::is_empty,
        )
        .await
    }

    async fn await_state(&self, what: &str, reached: impl Fn(&IndexState) -> bool) -> Result<()> {
        let deadline = Instant::now() + self.timing.timeout;
        let mut last = "not polled yet".to_string();
        while Instant::now() < deadline {
            let state = self.look().await;
            if reached(&state) {
                return Ok(());
            }
            last = state.describe();
            tokio::time::sleep(self.timing.poll_interval).await;
        }
        bail!(
            "timed out after {:.0}s waiting for {what}; index {:?} at {} was last {last}",
            self.timing.timeout.as_secs_f64(),
            self.index,
            self.url
        )
    }

    async fn look(&self) -> IndexState {
        match client::index_exists(self.client, self.index).await {
            Err(exc) => IndexState::Unreadable(format!("{exc:#}")),
            Ok(false) => IndexState::Absent,
            Ok(true) => self.count().await,
        }
    }

    async fn count(&self) -> IndexState {
        match client::document_count(self.client, self.index).await {
            Ok(docs) => IndexState::Present { docs },
            Err(exc) => IndexState::Unreadable(format!("{exc:#}")),
        }
    }
}

/// The delete-and-create cycle, run once before every level.
pub struct IndexReset {
    client: OpenSearch,
    index: String,
    url: String,
    config: IndexConfig,
    timing: GateTiming,
    notes: Notes,
}

impl IndexReset {
    pub fn new(
        client: OpenSearch,
        index: impl Into<String>,
        url: impl Into<String>,
        config: IndexConfig,
        timing: GateTiming,
        notes: Notes,
    ) -> Self {
        Self {
            client,
            index: index.into(),
            url: url.into(),
            config,
            timing,
            notes,
        }
    }

    pub async fn ensure_fresh(&self) -> Result<()> {
        self.notes.say(&format!("  resetting index {}", self.index));
        self.delete().await?;
        self.gate().await_absent().await?;
        self.create().await?;
        self.gate().await_empty().await?;
        self.notes.say("  index is answering at 0 documents");
        Ok(())
    }

    /// An index that was not there is the state this asks for, so a 404 is
    /// success. Nothing else is: a delete that was refused leaves the previous
    /// level's documents in place.
    async fn delete(&self) -> Result<()> {
        let response = self
            .client
            .indices()
            .delete(IndicesDeleteParts::Index(&[&self.index]))
            .send()
            .await
            .with_context(|| format!("cannot delete index {:?} at {}", self.index, self.url))?;
        let status = response.status_code().as_u16();
        if response.status_code().is_success() || status == NOT_FOUND {
            return Ok(());
        }
        bail!(
            "deleting index {:?} at {} was refused with {status}: {}",
            self.index,
            self.url,
            body_text(response).await
        )
    }

    /// No "if not exists": after the delete nothing should be there, and a
    /// create that silently succeeded against a survivor would hand this level
    /// the previous level's documents.
    async fn create(&self) -> Result<()> {
        let response = self
            .client
            .indices()
            .create(IndicesCreateParts::Index(&self.index))
            .body(self.config.body())
            .send()
            .await
            .with_context(|| format!("cannot create index {:?} at {}", self.index, self.url))?;
        if response.status_code().is_success() {
            return Ok(());
        }
        let status = response.status_code().as_u16();
        bail!(
            "creating index {:?} at {} from {} was refused with {status}: {}{}",
            self.index,
            self.url,
            self.config.name(),
            body_text(response).await,
            watermark_hint(status)
        )
    }

    fn gate(&self) -> Gate<'_> {
        Gate::new(&self.client, &self.index, &self.url, &self.timing)
    }

    /// Run once, after the first create and before any document: an analyzer
    /// cannot be changed on a live index, so the only useful moment to fail is
    /// now. One probe — `opensearch/verify_analyzer.sh` is still the full set,
    /// and what a failure here points at.
    pub async fn verify_analyzer(&self) -> Result<()> {
        if !self.config.declares_parity_analyzer() {
            self.notes.say(&format!(
                "  analyzer check skipped: {} declares no {PARITY_ANALYZER} analyzer",
                self.config.name()
            ));
            return Ok(());
        }
        let seen = client::analyze(&self.client, &self.index, PARITY_ANALYZER, ANALYZER_PROBE)
            .await
            .with_context(|| analyzer_context(&self.index))?;
        if seen != ANALYZER_PROBE_TOKENS {
            bail!(
                "the {PARITY_ANALYZER} analyzer on index {:?} does not match the vector-store's\n  \
                 probe:    {ANALYZER_PROBE}\n  expected: {ANALYZER_PROBE_TOKENS}\n  actual:   \
                 {seen}\nrecall and BM25 comparisons are not trustworthy until this passes; run \
                 bench/opensearch/verify_analyzer.sh for the full probe set",
                self.index
            )
        }
        self.notes.say(&format!(
            "  {PARITY_ANALYZER} analyzer matches on one probe"
        ));
        Ok(())
    }
}

impl BeforeLevel for IndexReset {
    fn prepare(&self) -> BoxFuture<'_, Result<()>> {
        Box::pin(self.ensure_fresh())
    }
}

const NOT_FOUND: u16 = 404;
const FORBIDDEN: u16 = 403;

/// A create refused with 403 is almost always `cluster_block_exception` from
/// `DiskThresholdMonitor`, which `tools/build_rate_point.sh` re-relaxes per
/// point for exactly this reason. Say so rather than leaving an operator with a
/// bare status.
fn watermark_hint(status: u16) -> &'static str {
    if status == FORBIDDEN {
        "\na 403 here is usually a cluster block on index creation; \
         run `make os-relax-watermarks` and try again"
    } else {
        ""
    }
}

fn analyzer_context(index: &str) -> String {
    format!("cannot check the {PARITY_ANALYZER} analyzer on index {index:?}")
}

async fn body_text(response: opensearch::http::response::Response) -> String {
    response
        .text()
        .await
        .unwrap_or_else(|exc| format!("<unreadable body: {exc}>"))
}

#[cfg(test)]
#[path = "reset_tests.rs"]
mod tests;
