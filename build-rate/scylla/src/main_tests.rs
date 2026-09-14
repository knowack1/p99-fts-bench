use super::*;

fn a_topology() -> Topology {
    Topology {
        scylla_version: "2026.3.0-rc2".to_string(),
        routing: "DefaultPolicy(token_aware)".to_string(),
        compression: "None".to_string(),
        driver_version: "1.8.0".to_string(),
        protocol_version: "4".to_string(),
        runtime: "tokio multi_thread workers:8".to_string(),
        shard_aware: "true".to_string(),
        shards: "127.0.0.1:9042=shards:3".to_string(),
        connections: "3".to_string(),
        tablets: "false".to_string(),
    }
}

#[test]
fn the_banner_names_the_engine_the_driver_and_the_runtime() {
    let lines = topology_lines(&a_topology());
    assert!(lines[0].contains("scylla 2026.3.0-rc2"));
    assert!(lines[0].contains("driver 1.8.0"));
    assert!(lines[0].contains("workers:8"));
}

/// A flat curve is only interpretable next to the shard counts, so the banner
/// has to carry them before the first level runs.
#[test]
fn the_banner_names_the_shard_topology() {
    let lines = topology_lines(&a_topology());
    assert!(lines[1].contains("shard_aware=true"));
    assert!(lines[1].contains("shards:3"));
    assert!(lines[1].contains("tablets=false"));
}
