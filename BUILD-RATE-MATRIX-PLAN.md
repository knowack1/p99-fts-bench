# Build-rate knob matrix — the run, and what must be true before it runs

**Status: BLOCKED, deliberately.** Everything the campaign needs is decided and
written down here. What is not ready is the harness: the ladder's x-axis does
not currently measure what the chart says it measures. Running the matrix in
that state would produce 140 complete, plausible, unquotable points, which is
the failure mode this document exists to prevent.

Companion documents: `results/client-model-2026-09-08/README.md` (why we
stopped), `WRITE-PATH-TEST-PLAN.md` (the S11–S15 slides this feeds),
`TUNING.md` (why each knob is set where it is).

## Why this campaign exists

The S12 ceiling ladder in `data/sweep-aws/` was measured 2026-09-01/09-03.
Three things have invalidated it:

1. **The ingest driver was unified** (`295c726`). `--concurrency` used to name
   different quantities per engine, and ScyllaDB's encoding ran on one thread.
2. **Writer-buffer parity was set** (`VS_FTS_WRITER_MEMORY_MB=376`,
   2026-09-07) and never ran a ladder. `BUILD-RATE-LOOP.md` measured it at
   **1.42x**, so the published 8,992 docs/s predates a 42% change.
3. **The client, not the engine, was setting the ceiling** above roughly c=32
   (`results/client-model-2026-09-08/`).

Karol's ask is to re-measure it as a knob matrix: isolate the tantivy writer
buffer and the commit cadence on the ScyllaDB side, against OpenSearch's RAM
index at matching refresh cadences.

## The run table

| # | Arm (`config`) | Target flag | Stack | Knobs vs. the row above | Pairs with |
|---|---|---|---|---|---|
| **R1** | `scylla-cdc-buf15` | `--scylladb-cdc-buf15` | Scylla + vector-store | `VS_FTS_COMMIT_THRESHOLD=0`; `VS_FTS_WRITER_MEMORY_MB` **unset** → tantivy's 15 MB/thread floor; commit interval 3 s; `VS_FTS_METRICS_INTERVAL=1s` | reference for R2 |
| **R2** | `scylla-cdc-buf376` | `--scylladb-cdc-buf376` | Scylla + vector-store | **+ `VS_FTS_WRITER_MEMORY_MB=376`** (writer-budget parity with OpenSearch's 1.4 GiB node total) | R4 (both 3 s) |
| **R3** | `scylla-cdc-buf376-commit30` | `--scylladb-cdc-buf376-commit30` | Scylla + vector-store | **+ `VS_FTS_COMMIT_INTERVAL=30s`** | R5 (both 30 s) |
| **R4** | `opensearch-ramindex` | `--opensearch-ram-nostore-refresh3` | OpenSearch | `OS_RAM_INDEX=1` (tmpfs segments) + `_source: false` + `refresh_interval: 3s` | R2 |
| **R5** | `opensearch-ramindex-refresh30` | `--opensearch-ram-nostore-refresh30` | OpenSearch | **+ `refresh_interval: 30s`** | R3 |

All five arms are registered in `ftsbench/target.py`, carry their knobs there
rather than in `.env.sut`, and are runnable today via `tools/knob_matrix.sh`.
`ftsbench/verify_arm.py` asserts the vector-store actually took the arm's
tuning — an image that ignores a knob looks identical to one that honours it,
and that already cost S11–S15 once.

**30 s, not the 60 s originally asked for.** At 60 s, `C1_IDLE_TIMEOUT=60` is
one commit cycle and every ScyllaDB point aborts as idle; 30 s is two, and the
sweep now floors both timeouts at four cadences regardless.

## Signals recorded per point

| Source | Fields |
|---|---|
| `c1-<config>-c<conc>-<rep>.jsonl` (`build_monitor`, 1 Hz) | `docs_indexed`, `docs_searchable`, `docs_delta`, `docs_per_s`, `docs_per_s_cumulative`, `index_status`; OpenSearch adds `segments_count`, `segments_memory_bytes`, `merges_current{,_docs}`, `merges_total{,_docs,_time_ms}`, `refresh_total{,_time_ms}`, `store_size_bytes` |
| `vslog-<config>-c<conc>-<rep>.log` (R1–R3) | `received/s`, **`added/s`**, `lock_wait_ms/s`, `commits`, `committed/s`, cumulative totals, plus the `ingest tuning` and `index writer using N threads, M MB/thread` startup lines |
| `cpu-<config>-c<conc>-<rep>.jsonl` (`resource_probe`, 1 Hz, on the SUT) | per container: `cpu_cores_used`, `cpu_seconds_total`, `rss_bytes`, `shmem_bytes`, `cache_bytes`, `mem_limit_bytes`, `disk_read_bytes`, `disk_write_bytes`, `index_size_bytes` |
| **generator series (does not exist yet — see gate G1)** | the harness box's own CPU/RSS over the same window |
| `manifest-<config>-c<conc>-<rep>.json` | config, rep, label, cache_state, corpus, max_docs, image pins, live engine versions, commands, host, gates |
| `summary.csv` | `docs_per_s_overall/median/mean/p10/p90/max`, `build_wall_seconds`, `throughput_variability`, `stall_fraction`, `merges_total`, `merge_time_s`, `segments_final`, `time_to_serving_s` |
| derived per arm | ceiling, `c_sat`, knee, `verify_cpu_usage` verdict |

`added/s` is the throughput signal for R1–R3: documents entering the tantivy
writer, ungated by commit. The `/status` count the monitor watches only moves
at a commit, so at 30 s it resolves a 100 s build into three points. Both are
recorded; the commit-gated count becomes the **visibility** signal, which is
what varying the commit interval is actually about.

## Decisions locked

| | |
|---|---|
| Per-point doc cap | **1,000,000** (`SWEEP_DOCS`) |
| Reps | **N=3**, rep-major, **`WARMUP=1`** |
| Slow cadence | **30 s** |
| ScyllaDB arms | commit threshold disabled and metrics interval on, held equal across all three |
| OpenSearch arms | `OS_RAM_INDEX_SIZE=12 GiB` in `.env.sut`, keeping R4/R5 on the same 28 GiB budget as R1–R3 at this cap |

**Why N=3 with a warm-up.** Across the existing 5-rep data the worst
median-of-any-3-subset deviates from the median of 5 by ≤3.0% (`scylla-cdc`),
≤3.5% (`opensearch-ramindex`), and ≤1.2% on the plateau rungs. Ample for the
R1→R2 writer-buffer delta (~+42%). But the 2026-09-01 decision to discard no
warm-up rested on the median of 5 being the *third* value, which one cold rep
cannot move; the median of 3 is the second, which it can.

**Top-up rule.** N=3 is marginal only for the cadence deltas (R2↔R3, R4↔R5),
whose magnitude is unmeasured. If either pair lands within ~5% at `c_sat`, top
up *that pair, at `c_sat` only*, to N=5 rather than pre-paying across the
matrix.

## Decisions REOPENED by the client findings

- **The ladder rungs.** Previously 9 rungs, `4 8 16 32 64 96 128 192 256`,
  chosen when `--concurrency` meant something else on the ScyllaDB side. They
  must be re-derived against the rebuilt client, in terms of *outstanding
  requests to the engine*, and the cost model re-estimated — the low rungs are
  the expensive ones, because a slow rung takes longer to reach the same cap.
- **The time estimate.** The old ~5.2 h assumed those rungs and the old client.
  Re-derive after G1–G3.
- **Generator calibration** (`tools/generator_calibration.sh`). Written to
  answer "does one loader process bind?", and superseded: the answer is now
  known to be yes, for a reason the script was not designed to expose. Keep it
  as a regression check after the rewrite, not as a gate.

## Gates that must clear before the matrix runs

**G1 — the generator is measured.** The harness box's own CPU and RSS are
recorded over each point's window, and a gate decides whether the point was
client-bound. `--containers` has only ever named the three SUT containers, so
every "the client was the bottleneck" conclusion to date — including the ones
in `results/client-model-2026-09-08/` — was inferred from the shape of the
throughput curve rather than measured. Raw CPU% is likely **not** sufficient on
its own: a single Python process can be the constraint at well under full box
utilisation, which is exactly what 13% CPU with 77 threads asleep in futex was.

**G2 — the concurrency model is settled.** `--concurrency` must mean
outstanding requests to the engine, on both engines, with a client that can
actually offer them. Direction agreed: the VectorDBBench model — N processes,
blocking clients, rendezvous barrier, parent aggregates — with per-process
in-flight multiplexing so the top rungs do not need one process each. Note
VectorDBBench's own *ingest* is single-process
(`serial_runner._insert_all_batches`, `max_workers=1`); the process-pool
pattern comes from its **search** runner, so applying it to ingest is an
extension of their concept, not a copy.

**G2a — client shape, decided 2026-09-08.** N worker processes, each holding M
async operations in flight — a superset of VectorDBBench's model, which is the
same thing at M=1. Processes buy CPU parallelism past the GIL; the async layer
inside each buys I/O overlap without one process per unit of concurrency, which
an 8-vCPU harness box cannot afford at the top rungs. This is the shape
`tools/sharded_build_rate.sh` already has (`SHARDS` x `CONC`), with async
replacing threads inside.

**Corpus sharding: contiguous.** Balanced on this corpus — the existing
`corpus-ab-a` / `corpus-ab-b` split is 2,369,432,161 vs 2,373,429,951 bytes for
600,000 documents each, 0.17% apart — and it is what `sharded_build_rate.sh`
already assumes. Round-robin would be balanced regardless of corpus ordering but
needs the frozen corpus re-sharded, and buys nothing measurable here. Order of
insertion does not affect the result: BM25 term statistics are collection-wide,
and the invariant that matters is every document exactly once, same set per
engine — which disjoint shards preserve.

**Note on the cap and sharding together:** at `SWEEP_DOCS=1000000` over N shards,
contiguous sharding takes 1M/N from the head of each shard, which is a different
*set* than the first 1,000,000 documents. Fine for engine-vs-engine as long as
both sides shard identically, but it breaks comparability with the existing
1M-capped ladders and must be stated on the chart rather than discovered later.

**C3 per-operation latency: keep the asymmetry, disclose it.** A ScyllaDB
operation carries `--batch-size` rows sent as individual prepared statements;
one OpenSearch operation is a single `_bulk`. So per-operation `service_ms` is
not comparable across engines, while per-document throughput remains honest.
The alternative — making a ScyllaDB operation one row — would leave
`--batch-size` meaning nothing on that side. C3's footer must state what an
operation is on each engine.

**G3 — the rungs are re-derived** against that client, with the generator probe
in place to prove the top rung is engine-bound. Each rung is `N x M`:
`N = min(c, N_max)` processes, `M = c / N` operations in flight each. N is
pinned at its ceiling rather than varied with `c`, because a knee in a curve
where BOTH moved could be a change in client architecture rather than an engine
effect. The rungs below `N_max` necessarily run fewer processes — `c < N` cannot
be expressed otherwise — and sit well below the knee; that belongs in the
footer. `N_max` and the CPU threshold come from Phase 0, not from this
document.

**G4 — the fleet is restored.** Public IPs change on stop; `/mnt/nvme` is wiped
on both boxes; the corpus needs ~3 h to re-stage (no S3 role yet); and
`scylladb/vector-store:1.10.0-44-g282d9efc-arm64` must be rebuilt from
`knowack1/vector-store` @ `282d9efc` — it is in no registry and has now been
lost with the instance store twice. Push it to ECR or bake an AMI on the way
back up.

## Phase 0 — client calibration (short fleet session, no engines)

**Why it exists.** Two constants the campaign depends on are currently
estimates, and both are derived from evidence that predates the client rewrite:

- **`N_max = 4`** (worker processes) — extrapolated from a per-process ceiling
  of ~9.8k docs/s (ScyllaDB) / ~11.4k (OpenSearch) measured with the OLD
  thread-per-operation client, against engine ceilings of ~11.7–12.2k docs/s.
- **`LOADER_CORE_BOUND_AT = 0.70`** — anchored to `BUILD-RATE-LOOP.md`'s record
  of a loader proven GIL-bound by A/B sitting at ~0.80 of one core. That
  measurement is also pre-rewrite.

Running the campaign on those two numbers would put a guess inside the gate
that exists to stop guesses reaching the deck.

**What it measures.** The client's own ceiling, which cannot be measured against
a real engine: at ~11.7k docs/s the engine saturates first, so the number that
comes back is the engine's. It needs a sink that never becomes the bottleneck.

| Output | Sets |
|---|---|
| per-process docs/s ceiling, per engine client | `N_max` — enough processes for ~3x headroom over the engine ceiling |
| loader `cpu_cores_used` at that ceiling | `LOADER_CORE_BOUND_AT`, from measurement rather than from a pre-rewrite anecdote |
| does raising M raise per-process throughput? | whether the async layer earns its complexity, or `M=1` (VectorDBBench's own shape) would do |
| does N scale linearly? | whether the box or the processes bind first |

**What it needs — and does NOT need.** This is why it is a separate, cheap
session rather than the first hour of the campaign:

| Campaign | Calibration |
|---|---|
| enwiki corpus re-staged, ~3 h | synthetic documents at enwiki's ~3.95 KB average, generated on the box |
| `vector-store:1.10.0-44-g282d9efc-arm64` rebuilt from the fork | no images at all |
| Scylla + vector-store + OpenSearch stacks | a null sink — accept and discard |
| ~5 h of ladder | minutes |

Setup is: start both boxes, update the two `HostName` entries, `mkfs.xfs` and
mount `/mnt/nvme` on each. Nothing else.

**Where each half runs.** The sink runs on **`fts-sut`**, not on localhost: both
boxes come up as a pair anyway, and a localhost sink has far lower RTT than the
private network, which would understate how much in-flight M is needed to cover
latency. The loaders run on **`fts-harness`**, which is the box whose ceiling the
campaign actually depends on — Graviton4, 8 vCPU, arm64. A laptop figure gives
the *shape* of the N/M scaling but not the value, because per-process throughput
is a function of that box's per-core speed.

**Built and validated locally first**, without the fleet: the sink, the
calibration runner, and a deliberately client-bound case used to prove the
generator probe and gate actually fire. The gate has never seen a positive
example, and a constructed one has a known right answer — which is the only way
to test a gate whose job is to catch a condition we hope not to encounter.

**Exit condition.** `N_max` and `LOADER_CORE_BOUND_AT` are recorded here as
measured values with the run that produced them, and G3 (rung derivation) uses
them. Until then the campaign does not start.

## Execution order, once the gates clear

0. **Phase 0 client calibration** (above) — a separate short session, no engines
   and no corpus. Produces `N_max` and `LOADER_CORE_BOUND_AT` as measurements.
1. Restore the fleet; verify the corpus against `FREEZE.md` (8,967,625 docs).
2. Smoke: 2 rungs × all 5 arms at a 20k cap. Gates: 10/10 points complete,
   manifests carry the right `config` *and* tunables, the vector-store's
   `ingest tuning` line matches the intended arm, R3 does not abort on idle,
   R4/R5 tmpfs does not hit ENOSPC.
3. Re-derive the rungs (G3) and re-estimate the budget.
4. The matrix, one arm at a time, stack recreated between arms:
   `tools/knob_matrix.sh 3`, order R1 → R2 → R3 → R4 → R5 — the two ScyllaDB
   knob deltas land before the OpenSearch arms, so a surprise in R2 can still
   change the plan.
5. Summarise, `verify_cpu_usage` per arm, record `c_sat` and the ceiling in
   `TUNING.md`, and render `added/s` against the commit-gated count for R2/R3.

## Caveats to carry into the write-up

- **The 1M cap is not the talk's operating point.** `BUILD-RATE-LOOP.md`
  iteration 9 measured the engine ranking *inverting* between a 1.2M and the
  8.97M corpus (ScyllaDB 1.15x → OpenSearch 1.10x). A ceiling ladder at 1M
  answers "what saturates", not "what wins at scale".
- **Encode cost is asymmetric.** Measured on the laptop at enwiki's average
  document size: OpenSearch's `bulk_payload` costs 8.19 ms per 500-document
  batch against ScyllaDB's 0.31 ms — **26.7x**, ~19% of one core at the engine
  ceiling, landing entirely on OpenSearch's side. It is already outside the
  latency window (`timed_op` wraps only `send`), but it is a harness artifact
  in a comparison whose credibility rests on symmetry.
- **The 50/50 cpuset split is probably wrong for the CDC path**
  (`BUILD-RATE-LOOP.md`): during writes Scylla sits at 3.56/4 while the
  vector-store idles near 1.9/4 — it is CDC-starved, not CPU-bound. Out of
  scope here, but this run's `cpu_cores_used` will strengthen or weaken it.
- **Dropping an FTS index does not release its in-RAM memory.** Measured mild
  at this cap (vector-store RSS 0.74 → 0.82 → 0.83 GiB across three 200k
  points), so a warm-container ladder is safe at 1M — but the probe gate is
  what proves it per arm rather than per campaign.
