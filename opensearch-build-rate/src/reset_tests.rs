//! The gates are the difference between a reset and a hope.
//!
//! A `DELETE` can still be settling when the `PUT` goes out, and a created
//! index answers before its primary is allocated. A gate that passed early
//! would let a level load into the index it just deleted, or into one that is
//! not serving yet, and either produces a complete, plausible, wrong build
//! rate.
use std::time::Duration;

use super::*;
use crate::client::ConnectOptions;
use crate::fakes::{quiet_notes, FakeIndex, SpokenNotes};

const INDEX: &str = "wiki-articles";
const A_TIMEOUT: Duration = Duration::from_secs(5);

fn brisk(timeout: Duration) -> GateTiming {
    GateTiming {
        poll_interval: Duration::from_millis(1),
        timeout,
    }
}

fn a_config() -> IndexConfig {
    IndexConfig::select(DEFAULT_INDEX_CONFIG).unwrap()
}

fn reset_for(endpoint: &FakeIndex, notes: Notes) -> IndexReset {
    reset_with(endpoint, a_config(), notes, A_TIMEOUT)
}

fn reset_with(
    endpoint: &FakeIndex,
    config: IndexConfig,
    notes: Notes,
    timeout: Duration,
) -> IndexReset {
    let client = client::build_client(&ConnectOptions {
        url: endpoint.url().to_string(),
        index: INDEX.to_string(),
        request_timeout: A_TIMEOUT,
    })
    .unwrap();
    IndexReset::new(client, INDEX, endpoint.url(), config, brisk(timeout), notes)
}

// --- the config -----------------------------------------------------------

#[test]
fn the_default_config_is_the_ram_parity_one() {
    let config = a_config();
    assert_eq!(config.name(), RAMINDEX);
    assert_eq!(
        config.body().pointer("/mappings/_source/enabled"),
        Some(&Value::Bool(false)),
        "the default must be the variant whose index carries no document text"
    );
}

#[test]
fn the_disk_config_keeps_the_document_store() {
    let config = IndexConfig::select(DISK).unwrap();
    assert_eq!(config.name(), DISK);
    assert_eq!(config.body().pointer("/mappings/_source/enabled"), None);
}

#[test]
fn both_embedded_configs_analyze_the_body_with_the_parity_analyzer() {
    for name in [RAMINDEX, DISK] {
        let config = IndexConfig::select(name).unwrap();
        assert_eq!(
            config.body().pointer("/mappings/properties/body/analyzer"),
            Some(&Value::String(PARITY_ANALYZER.to_string())),
            "{name} must analyze body with the vector-store's analyzer"
        );
        assert!(config.declares_parity_analyzer(), "{name}");
    }
}

#[test]
fn a_config_that_declares_no_parity_analyzer_is_not_probed() {
    let (_directory, path) = a_config_file("{}");
    let config = IndexConfig::select(path.to_str().unwrap()).unwrap();
    assert!(!config.declares_parity_analyzer());
}

#[test]
fn an_index_config_can_be_read_from_a_path() {
    let (_directory, path) =
        a_config_file(r#"{"settings": {"index": {"refresh_interval": "7s"}}}"#);
    let config = IndexConfig::select(path.to_str().unwrap()).unwrap();
    assert_eq!(
        config.body().pointer("/settings/index/refresh_interval"),
        Some(&Value::String("7s".to_string()))
    );
}

/// `create_index.sh` creates a default-configured index when its own config
/// read fails. An index that is silently not the one that was asked for is
/// worse than no index.
#[test]
fn an_unreadable_index_config_fails_the_run() {
    let exc = IndexConfig::select("/no/such/index-config.json").unwrap_err();
    let said = format!("{exc:#}");
    assert!(said.contains("/no/such/index-config.json"), "{said}");
    assert!(said.contains(RAMINDEX) && said.contains(DISK), "{said}");
}

#[test]
fn an_index_config_that_is_not_json_fails_by_name() {
    let (_directory, path) = a_config_file("not json at all");
    let exc = IndexConfig::select(path.to_str().unwrap()).unwrap_err();
    assert!(format!("{exc:#}").contains("is not JSON"));
}

#[test]
fn the_refresh_interval_is_patched_into_the_config() {
    let config = a_config().with_refresh_interval(Some("30s")).unwrap();
    assert_eq!(
        config.body().pointer("/settings/index/refresh_interval"),
        Some(&Value::String("30s".to_string()))
    );
}

#[test]
fn an_unset_refresh_interval_leaves_the_config_alone() {
    let config = a_config().with_refresh_interval(None).unwrap();
    assert_eq!(
        config.body().pointer("/settings/index/refresh_interval"),
        Some(&Value::String("3s".to_string()))
    );
}

#[test]
fn a_config_without_settings_index_cannot_take_a_refresh_interval() {
    let (_directory, path) = a_config_file("{}");
    let config = IndexConfig::select(path.to_str().unwrap()).unwrap();
    let exc = config.with_refresh_interval(Some("30s")).unwrap_err();
    assert!(format!("{exc:#}").contains("refresh_interval"));
}

/// The directory comes back with the path because dropping it deletes the
/// file, and a config the test cannot read is not the failure it is asserting.
fn a_config_file(contents: &str) -> (tempfile::TempDir, std::path::PathBuf) {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("index-config.json");
    std::fs::write(&path, contents).unwrap();
    (directory, path)
}

// --- the cycle ------------------------------------------------------------

#[tokio::test]
async fn the_cycle_deletes_the_index_then_creates_it_from_the_config() {
    let endpoint = FakeIndex::start().await;
    reset_for(&endpoint, quiet_notes())
        .ensure_fresh()
        .await
        .unwrap();

    assert_eq!(endpoint.times("DELETE /wiki-articles"), 1);
    assert_eq!(endpoint.times("PUT /wiki-articles"), 1);
    assert_eq!(endpoint.last_config().as_ref(), Some(a_config().body()));
}

#[tokio::test]
async fn the_config_that_reaches_the_wire_carries_the_requested_refresh_interval() {
    let endpoint = FakeIndex::start().await;
    let config = a_config().with_refresh_interval(Some("30s")).unwrap();
    reset_with(&endpoint, config, quiet_notes(), A_TIMEOUT)
        .ensure_fresh()
        .await
        .unwrap();

    assert_eq!(
        endpoint
            .last_config()
            .and_then(|body| body.pointer("/settings/index/refresh_interval").cloned()),
        Some(Value::String("30s".to_string()))
    );
}

/// Gate A. Without it the create can land while the delete is still settling,
/// and the level then loads into the index that was about to disappear.
#[tokio::test]
async fn the_create_waits_until_the_delete_has_landed() {
    let endpoint = FakeIndex::start().await;
    endpoint.delete_lands_after(3);
    reset_for(&endpoint, quiet_notes())
        .ensure_fresh()
        .await
        .unwrap();

    let events = endpoint.events();
    let created = events
        .iter()
        .position(|at| at == "PUT /wiki-articles")
        .unwrap();
    let heads = events[..created]
        .iter()
        .filter(|at| *at == "HEAD /wiki-articles")
        .count();
    assert!(
        heads >= 3,
        "the create did not wait for the delete: {events:?}"
    );
}

/// Gate B. Without it the level starts against an index whose primary is not
/// allocated, and the first bulks measure the wait rather than the engine.
#[tokio::test]
async fn the_level_waits_until_the_new_index_answers() {
    let endpoint = FakeIndex::start().await;
    endpoint.answers_after(3);
    reset_for(&endpoint, quiet_notes())
        .ensure_fresh()
        .await
        .unwrap();

    assert!(
        endpoint.times("GET /wiki-articles/_count") >= 4,
        "{:?}",
        endpoint.events()
    );
}

#[tokio::test]
async fn an_index_that_was_not_there_is_created_without_complaint() {
    let endpoint = FakeIndex::start().await;
    endpoint.absent();
    reset_for(&endpoint, quiet_notes())
        .ensure_fresh()
        .await
        .unwrap();

    assert_eq!(endpoint.times("PUT /wiki-articles"), 1);
}

#[tokio::test]
async fn a_gate_that_never_opens_names_the_index_and_the_endpoint() {
    let endpoint = FakeIndex::start().await;
    endpoint.delete_lands_after(usize::MAX);
    let reset = reset_with(
        &endpoint,
        a_config(),
        quiet_notes(),
        Duration::from_millis(30),
    );
    let said = format!("{:#}", reset.ensure_fresh().await.unwrap_err());

    assert!(said.contains("wiki-articles"), "{said}");
    assert!(said.contains(endpoint.url()), "{said}");
    assert!(said.contains("was last present"), "{said}");
}

#[tokio::test]
async fn a_delete_that_was_refused_fails_the_level() {
    let endpoint = FakeIndex::start().await;
    endpoint.refusing_deletes(500);
    let reset = reset_for(&endpoint, quiet_notes());
    let said = format!("{:#}", reset.ensure_fresh().await.unwrap_err());

    assert!(said.contains("was refused with 500"), "{said}");
    assert_eq!(endpoint.times("PUT /wiki-articles"), 0, "it created anyway");
}

/// `DiskThresholdMonitor` re-applies `cluster.blocks.create_index` on its own
/// schedule, which is why `build_rate_point.sh` re-relaxes the watermarks per
/// point. A bare 403 leaves an operator with nothing to do about it.
#[tokio::test]
async fn a_create_refused_by_a_cluster_block_points_at_the_watermarks() {
    let endpoint = FakeIndex::start().await;
    endpoint.refusing_creates(403);
    let reset = reset_for(&endpoint, quiet_notes());
    let said = format!("{:#}", reset.ensure_fresh().await.unwrap_err());

    assert!(said.contains("os-relax-watermarks"), "{said}");
}

#[tokio::test]
async fn the_reset_says_what_it_did() {
    let endpoint = FakeIndex::start().await;
    let spoken = SpokenNotes::default();
    reset_for(&endpoint, spoken.notes(Duration::from_secs(3600)))
        .ensure_fresh()
        .await
        .unwrap();

    assert!(
        spoken.mentions("resetting index wiki-articles"),
        "{:?}",
        spoken.lines()
    );
    assert!(spoken.mentions("0 documents"), "{:?}", spoken.lines());
}

#[tokio::test]
async fn a_reset_is_what_a_level_prepares_with() {
    let endpoint = FakeIndex::start().await;
    let reset = reset_for(&endpoint, quiet_notes());
    (&reset as &dyn BeforeLevel).prepare().await.unwrap();

    assert_eq!(endpoint.times("PUT /wiki-articles"), 1);
}

// --- the analyzer ---------------------------------------------------------

#[tokio::test]
async fn the_analyzer_probe_passes_when_the_token_stream_matches() {
    let endpoint = FakeIndex::start().await;
    reset_for(&endpoint, quiet_notes())
        .verify_analyzer()
        .await
        .unwrap();

    assert_eq!(endpoint.times("POST /wiki-articles/_analyze"), 1);
}

#[tokio::test]
async fn a_divergent_analyzer_fails_and_points_at_the_full_probe_set() {
    let endpoint = FakeIndex::start().await;
    endpoint.analyzing_as("0:the 1:u.s. 2:army");
    let reset = reset_for(&endpoint, quiet_notes());
    let said = format!("{:#}", reset.verify_analyzer().await.unwrap_err());

    assert!(said.contains(ANALYZER_PROBE_TOKENS), "{said}");
    assert!(said.contains("0:the 1:u.s. 2:army"), "{said}");
    assert!(said.contains("verify_analyzer.sh"), "{said}");
}

#[tokio::test]
async fn an_endpoint_that_cannot_analyze_fails_the_check() {
    let endpoint = FakeIndex::start().await;
    endpoint.refusing_analyze(400);
    let reset = reset_for(&endpoint, quiet_notes());
    let said = format!("{:#}", reset.verify_analyzer().await.unwrap_err());

    assert!(said.contains(PARITY_ANALYZER), "{said}");
}

#[tokio::test]
async fn a_config_without_the_parity_analyzer_is_not_probed_at_all() {
    let endpoint = FakeIndex::start().await;
    let (_directory, path) = a_config_file("{}");
    let config = IndexConfig::select(path.to_str().unwrap()).unwrap();
    let spoken = SpokenNotes::default();
    reset_with(
        &endpoint,
        config,
        spoken.notes(Duration::from_secs(3600)),
        A_TIMEOUT,
    )
    .verify_analyzer()
    .await
    .unwrap();

    assert_eq!(endpoint.times("POST /wiki-articles/_analyze"), 0);
    assert!(
        spoken.mentions("analyzer check skipped"),
        "{:?}",
        spoken.lines()
    );
}

/// The failure the gates exist for: everything is acknowledged, nothing
/// happened, and without a gate the level would load into the index it thinks
/// it emptied and report a build rate for an update.
#[tokio::test]
async fn a_delete_that_was_acknowledged_but_did_nothing_fails_the_level() {
    let endpoint = FakeIndex::start().await;
    endpoint.lying_about_deletes().holding(42);
    let reset = reset_with(
        &endpoint,
        a_config(),
        quiet_notes(),
        Duration::from_millis(30),
    );
    let said = format!("{:#}", reset.ensure_fresh().await.unwrap_err());

    assert!(
        said.contains("was last present with 42 document(s)"),
        "{said}"
    );
    assert_eq!(endpoint.times("PUT /wiki-articles"), 0, "it created anyway");
}
