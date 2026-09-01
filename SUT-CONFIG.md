# SUT configuration — every version, flag and knob, slide-ready

Single place to look up **what exactly runs on the SUT box** during the AWS
campaign: versions of every component and the complete configuration surface
of both engine stacks. `TUNING.md` explains *why* each value was chosen and
what evidence backs it; this file is the flat *what*, in the shape a slide or
an appendix wants. If the two ever disagree, `TUNING.md` wins and this file
has a bug.

Status: resource values below implement the 50/50 cgroup split (see
"Resource budgets") and are **proposed** until the phase-D memory validation
passes on the SUT. Version pins are current as of 2026-09-01.

## Versions

| Component | Version | Release status | Notes |
|---|---|---|---|
| ScyllaDB | `scylladb/scylla:2026.3.0-rc2` | **release candidate** — no GA of 2026.3 exists (Docker Hub: rc0–rc3 only, checked 2026-09-01) | FTS ships in 2026.3; the feature itself is pre-GA, so an RC is the only option. Say "2026.3.0-rc2" on the slide, not "2026.3". Newer rc3 exists — staying on rc2 unless retested. |
| vector-store | `scylladb/vector-store:1.10.0-43-ge242fa3-arm64` | **published-source build**: upstream master (42 commits past the 1.10.0 tag) + one commit exposing FTS ingest tunables as env vars | Source: github.com/knowack1/vector-store, branch `p99-fts-no-commit-threshold`, commit `e242fa3b`. Built with the repo's own `scripts/build-release arm64` + `build-dockers arm64` on the harness box (image id `06b84b923570`, loaded on the SUT 2026-09-01; binary tarball `vector-store-1.10.0-43-ge242fa3-arm64.tar.gz` alongside). Required by the decision to disable the 10k commit threshold (compiled-in on releases). Base is NOT the 1.10.0 tag itself — it is upstream master at the time of the fork; say the full version string on the slide. |
| OpenSearch | `opensearchproject/opensearch:3.8.0` | release | Bundles its own JDK (probe `GET /_nodes/jvm` at deploy and record below). |
| Lucene (inside OpenSearch) | 10.5.0 | release | Probed live on 3.8.0 (`TUNING.md` §2). |
| Tantivy (inside vector-store) | per vector-store 1.10.0 Cargo.lock | release | The analyzer-parity work (`opensearch/verify_analyzer.sh`) is calibrated against this build's `SimpleTokenizer`. |
| OpenSearch bundled JDK | _record at deploy_ | release | `curl :9200/_nodes/jvm \| jq ..` on first SUT bring-up. |
| OS | Amazon Linux 2023 (`al2023-ami-2023.12.20260817.0-kernel-6.18-arm64`), kernel 6.18 | release | Same AMI on harness and SUT. |
| Docker / compose | 25.0.14 / compose v2 | release | data-root on instance-store NVMe. |
| Python (harness tooling) | 3.12 (venv) | release | Loaders and probes run on the harness box, not the SUT. |
| scylla-driver (loader) | 3.29.11 | release | |
| Hardware | `i8g.2xlarge` — 8 vCPU Graviton4, 64 GiB, 1.9 TB instance-store NVMe | — | Identical instance type for SUT and harness; single AZ (eu-north-1b). |
| Corpus | enwiki `cirrus_search_index` 2026-08-16, 8,967,625 docs | frozen | `FREEZE.md` — checksums there. |

## Resource budgets (cgroups via docker compose)

8 cores / ~61 GiB usable; ~5 GiB reserved for OS + Docker. One engine stack
runs at a time. Rationale — the "database slot" argument — in `TUNING.md`;
disclosed on every chart footer.

| Config | Service | cpuset | cgroup mem | In-process budget |
|---|---|---|---|---|
| `scylla` | ScyllaDB | `0-3` | 28 GiB | `--memory 24G` |
| `scylla` | vector-store | `4-7` | 28 GiB | `VECTOR_STORE_MEMORY_LIMIT=25769803776` (24 GiB) |
| `opensearch` | OpenSearch | `4-7` | 28 GiB | JVM heap `-Xms14g -Xmx14g` |
| `opensearch` | (database slot) | `0-3` | 28 GiB | deliberately idle — models the database OpenSearch deploys beside |

Both engine stacks write to the same instance-store NVMe (docker data-root).

## ScyllaDB — full command line

```
--smp 4                    # one shard per dedicated core (cpuset 0-3)
--memory 24G               # pre-allocated budget, below the 28 GiB cgroup cap
--overprovisioned 0        # dedicated cores: keep production poll-mode
--vector-store-primary-uri http://vector-store:6080
```

Schema: `scylladb/schema.cql` — keyspace `wiki`, RF=1, one table
(`id uuid PRIMARY KEY, title text, body text`), one
`CREATE CUSTOM INDEX ... USING 'fulltext_index'` on `body`.
CDC: enabled by the index (bootstrap scan vs CDC tail are the two measured
ingest paths). CQL host port 9042 (SUT is single-purpose; the laptop's 19042
remap is not needed).

## vector-store — full environment

```
VECTOR_STORE_URI=0.0.0.0:6080
VECTOR_STORE_SCYLLADB_URI=scylla:9042
VECTOR_STORE_MEMORY_LIMIT=25769803776      # 24 GiB
VECTOR_STORE_FTS_COMMIT_THRESHOLD=0
# ^ 0 disables the 10k-uncommitted-docs commit trigger (release default:
#   10,000, compiled-in). Commits become purely interval-driven — every 3 s —
#   matching OpenSearch's refresh_interval: 3s at every load level, not only
#   below ~3.3k docs/s. DEVIATION FROM RELEASE BEHAVIOUR: disclosed on every
#   chart footer. Requires the tunables build below; the public 1.10.0
#   silently ignores this variable.
# All other VS_FTS_* / VS_CDC_* tunables: UNSET — release defaults.
```

Index: in-RAM Tantivy, rebuilt by full base-table scan on restart (design
property, not a knob — see `COMPARABILITY.md`). Host port 16080 on the SUT’s
private address only (security group blocks everything external).

## OpenSearch — full configuration

Environment / JVM:

```
DISABLE_SECURITY_PLUGIN=true
DISABLE_INSTALL_DEMO_CONFIG=true
discovery.type=single-node
OPENSEARCH_JAVA_OPTS=-Xms14g -Xmx14g
```

Index settings (`opensearch/index-config.json`):

```
number_of_shards: 1, number_of_replicas: 0
refresh_interval: 3s        # parity with vector-store's commit interval (3 s);
                            # clean at every load level because the 10k commit
                            # threshold is disabled on the vector-store side —
                            # both engines are purely interval-driven.
                            # per-config: 30s in refresh30; -1 during load where stated
analyzer m1_parity:
  tokenizer: pattern  [^\p{IsAlphabetic}\p{N}]+   # reproduces Tantivy SimpleTokenizer exactly
  filters:  lowercase, stop (_english_)           # no stemming — matches ScyllaDB M1
```

Analyzer parity is asserted, not assumed: `opensearch/verify_analyzer.sh`
runs after every `os-index` and fails the run on divergence. Cost of parity:
~11% OpenSearch indexing throughput vs its native `standard` tokenizer —
disclosed on every build-rate chart.

Cluster settings: stock. Query threading: with 1 shard a query classically
executes on one search thread; whether 3.8 enables **concurrent segment
search** by default changes that — record the effective
`search.concurrent_segment_search.*` values from the live node at deploy,
next to the JDK version. (The laptop's relaxed disk watermarks are **not**
carried to the SUT — its NVMe is nearly empty; any deviation from stock that
a run turns out to need gets recorded here first.)

## Control-plane configs (for completeness)

| Config name | What it is |
|---|---|
| `opensearch` | refresh_interval 3s (vector-store parity), budgets above |
| `opensearch-refresh30` | identical, refresh_interval 30s |
| `scylla-bootstrap` | index created after the base table is loaded → bootstrap full scan |
| `scylla-cdc` | index created first → CDC tail during load |
| `opensearch-uncapped` | sensitivity check: full box (cpuset 0-7, 56 GiB, heap 28 GiB), one rep — answers "what did the cap cost" |

Switching between stacks: `tools/sut_engine.sh {opensearch|scylla|none|status}`
from the harness — always `down -v` of the other stack first (cold start per
repetition is a campaign rule).
