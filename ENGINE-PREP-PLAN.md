# Engine preparation plan — SUT bring-up for the AWS campaign

Written 2026-09-01, at the end of the fleet-provisioning session. The next
session starts at "Phase A" below and works down. `SUT-CONFIG.md` holds every
version/flag/knob (the *what*); `TUNING.md` holds the rationale (the *why*);
this file holds the *order of work* and the state a future session needs.

## Fleet state (as of 2026-09-01)

| Box | Instance | Private IP | SSH alias | Role |
|---|---|---|---|---|
| harness | `i-08d8d2505e16683f7` | 172.31.38.237 | `fts-harness` | corpus + loaders + query generator + control; runs nothing under measurement |
| SUT | `i-0e3e4b6b02e654b7f` | 172.31.47.166 | `fts-sut` | one engine stack at a time; nothing else |

Both `i8g.2xlarge` (8 vCPU Graviton4 / 64 GiB / 1.9 TB instance-store NVMe),
eu-north-1b, subnet `subnet-08b5b770`, security group `k-nowacki-fts-bench`
(SSH from Karol's IP + all-traffic self-reference; engine ports verified
unreachable from the internet). **Public IPs change on every stop/start** —
update `~/.ssh/config` HostName lines first thing each session; private IPs
are stable while the instances exist.

Provisioned on both boxes (root EBS, survives stop): repo at `~/p99`
(rsynced from the laptop — the laptop is the source of truth, there is no git
remote), python3.12 venv at `~/venv`, docker with data-root on `/mnt/nvme`,
symlinks `bench/.venv → ~/venv` and `bench/data → /mnt/nvme/data`.

**Instance-store NVMe is erased on stop.** After every stop/start:
`mkfs.xfs /dev/<1.7T disk> && mount -o noatime … /mnt/nvme`, recreate
`/mnt/nvme/data` (+ `/mnt/nvme/docker`), restart docker, re-download or
re-stage the corpus (S3 staging still pending an IAM role — see "Open
items").

On the harness NVMe today: 65 enwiki shards (38 GB, sha256 manifest),
`corpus.jsonl` (8,967,625 docs, sha256 in `FREEZE.md`), `queries.json`.
The SUT never needs the corpus — loaders run on the harness and stream over
the private network.

## Engine artifacts

- **OpenSearch**: `opensearchproject/opensearch:3.8.0` (Docker Hub, arm64).
- **ScyllaDB**: `scylladb/scylla:2026.3.0-rc2` (Docker Hub, arm64). No GA of
  2026.3 exists; RC is disclosed on the slide.
- **vector-store**: built from source, `scylladb/vector-store:1.10.0-43-ge242fa3-arm64`
  — knowack1/vector-store branch `p99-fts-no-commit-threshold` commit
  `e242fa3b`, built on the harness with the repo's own
  `scripts/build-release arm64 && scripts/build-dockers arm64`
  (clone lives in `/mnt/nvme/build/vector-store`; NVMe, so a stop erases it —
  the fork branch is the durable copy). Move to the SUT with
  `docker save … | ssh fts-sut docker load`.

## Phase A — configuration (laptop, $0, do before touching the fleet)

All in `bench/` on the laptop, then rsync to both boxes.

1. **`docker/.env.sut`** — new env file implementing the 50/50 cgroup split
   from `SUT-CONFIG.md`: scylla cpuset `0-3` / 28g / `--smp 4 --memory 24G`,
   vector-store cpuset `4-7` / 28g / `VECTOR_STORE_MEMORY_LIMIT=24GiB` /
   `VECTOR_STORE_FTS_COMMIT_THRESHOLD=0`, opensearch cpuset `4-7` / 28g /
   heap 14g / `refresh_interval: 3s`. Image pins per `SUT-CONFIG.md`.
2. **Compose cpuset parameterization** — the compose files use one shared
   `ENGINE_CPUSET`; the split needs per-service `SCYLLA_CPUSET` / `VS_CPUSET`
   / `OS_CPUSET`. Also `--overprovisioned` must become env-driven (laptop=1,
   SUT=0) and the SUT uses host port 9042 (no devcontainer conflict).
3. **`tools/sut_engine.sh`** — `{opensearch|scylla|none|status}`, run from
   the harness, drives the SUT over SSH; always `down -v` of the other stack
   before `up` (cold repetition rule).
4. **Makefile caps for enwiki** — `C1_MAX_SECONDS` (bootstrap est. ~2.7 h →
   set ≥ 14400 per config), `C1_IDLE_TIMEOUT` (commit cadence is now pure 3 s,
   but leave ≥ 60 s headroom), `C7_RATE_MAX` (raise; calibrate first),
   `C1_UNTIL_DOCS=8967625` for every enwiki run (`FREEZE.md`).
5. **Remote-engine URLs** — runs from the harness use
   `OS_URL=http://172.31.47.166:9200`, `SCYLLA_HOSTS=172.31.47.166`,
   `SCYLLA_PORT=9042`, `VS_URL=http://172.31.47.166:16080`.
6. **`TUNING.md` update** — mirror the new image pin, threshold=0, refresh 3s
   rows; mark laptop-only rows as superseded for the SUT.

## Phase B — deploy (fleet, minutes)

1. rsync repo laptop → both boxes.
2. `docker save` the vector-store image harness → SUT; `docker pull` scylla +
   opensearch on the SUT.
3. `sut_engine.sh opensearch` → health, record from the live node into
   `SUT-CONFIG.md`: JDK version (`/_nodes/jvm`) and effective
   `search.concurrent_segment_search.*`.
4. `sut_engine.sh scylla` → health (cqlsh + vector-store `/api/v1/…` up),
   confirm the tunables are live (metrics interval log line, or set
   `VECTOR_STORE_FTS_METRICS_INTERVAL=10s` for the smoke and read the log).

## Phase C — smoke (fleet, ~30 min)

20k docs end-to-end from the harness against each stack in turn: load, gates
(`opensearch_doc_count` / `scylla_index_complete` / not-OOM), a C5-style
query burst, `verify_analyzer.sh` after `os-index` (exits non-zero on
divergence — the laptop shipped broken parity once; never skip it).

## Phase D — memory validation (fleet, ~1 h) — GATE for the 50/50 split

Load 10% of enwiki into the scylla stack; watch vector-store RSS via
`resource_probe`. `SIZING.md` projects a ~39 GB peak for the full corpus —
**over the 28 GiB budget**; the failure mode is silent document skipping,
caught only by the `scylla_index_complete` gate. Decision rule: extrapolated
peak < 26 GiB → keep 50/50; otherwise switch to an asymmetric split (e.g.
Scylla 16 / VS 40, OpenSearch then gets 40 to keep the parity rule
"OpenSearch = vector-store's share") and update `SUT-CONFIG.md` + `TUNING.md`
before any measured run.

## Phase E — campaign

Per `AWS-RUN-PLAN.md` phases 2–4 (ingest → query → results), plus two
sensitivity runs, one rep each: `opensearch-uncapped` (full box) and
`opensearch` with `number_of_shards: 4` (answers "why 1 shard" with a
number). Calibrate the generator per engine before C7 (`make calibrate-os` /
`make calibrate-scylla`) — if a calibrated ceiling is below `C7_RATE_MAX`,
C7 is measuring the client.

## Open items (not blocking A–C)

- **IAM role + S3 bucket** (eu-north-1) — corpus staging; until then a
  harness stop costs a ~3 h re-download+prepare (fully scripted, see
  `tools/parallel_fetch_remaining.sh` for the 2-connection fetch).
- **Publish the vector-store build** properly before quoting numbers
  (fork branch is public; consider an upstream PR or a registry push).
- Commit the uncommitted laptop work: parallel `prepare_corpus` + tests,
  `FREEZE.md` enwiki section, `SUT-CONFIG.md`, this file.
- Scylla `2026.3.0-rc3` exists; staying on rc2 unless retested.

## Notes for the AI running the next session

- Aliases `fts-harness` / `fts-sut` are in `~/.ssh/config`; verify both
  connect before anything else — if a box was stopped, fix HostName (new
  public IP from the user) and re-init the NVMe per "Fleet state" above.
- Long-running work on the boxes goes in **tmux** (sessions survive SSH
  drops); monitor from the laptop with a Monitor/until-loop. Never park a
  long job in a bare SSH call.
- `pkill -f` on a remote host can match the SSH session's own command line —
  it killed a session once. Prefer `tmux kill-session` / exact `pgrep` ids.
- The laptop repo is the source of truth; boxes get rsync copies. Don't edit
  files on the boxes.
- Every measured run must trace to `SUT-CONFIG.md` + `FREEZE.md` values; if a
  knob changes mid-campaign, change the doc in the same breath.
- Session memory (`~/.claude/.../memory/`) has the fleet history:
  `fts-harness-box-restart`, `slides-hardware-conflict`,
  `corpus-size-vs-shards-coincidence`.
