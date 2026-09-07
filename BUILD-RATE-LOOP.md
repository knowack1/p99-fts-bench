# Build-rate optimization loop — closing the vector-store ingest gap

Started 2026-09-07. Goal: raise the `scylla-cdc` index build rate from ~9k
docs/s to within ~2% of OpenSearch's measured ceiling, by changing code rather
than knobs. Loop runs until solved, hypotheses exhausted, or the iteration
budget is spent.

## Target

| | ceiling (S12, 1M-doc cap, N=5) | full corpus (S11, N=5) | CPU during build |
|---|---|---|---|
| `opensearch-refresh3` | **11,063 docs/s** | 9,687 docs/s | pinned 4.0 / 4 cores |
| `scylla-cdc` | **8,992 docs/s** | 7,567 docs/s | VS side ~3.5 / 4 cores |

**Success** = B2 median ≥ 10,900 docs/s, confirmed by B3 (ladder) + B4 (full
corpus). **Exhausted** = queue empty and a fresh profile shows no actionable hot
spot; that outcome is written up as a structural finding, not re-framed.

## Why these hypotheses and not more knobs

`results/fts-bottleneck-2026-08-27/` already excluded, by measurement: writer
buffer (worth ~24%, plateaus by 64 MB), merge threads (2/4/8/12), CPU grant
(2/6/12), the per-document writer lock, CDC polling cadence (fine and wide),
commit interval, commit threshold, and the client. Its closing line: *"black-box
variants have run out of road."* Everything below is a code change.

## Baseline caveat that governs iteration 1

`VS_FTS_WRITER_MEMORY_MB=376` was set on 2026-09-07 and **has never run on the
SUT**. The 8,992 figure predates it. So the flag-off arm of the first A/B is
itself a new measurement, and part of the gap may already be closed by the
buffer fix before any code change gets credit. Both arms are therefore measured
in the same session, on the same image, rep-major interleaved.

## Hypothesis queue

| # | hypothesis | flag | status |
|---|---|---|---|
| H1 | Remove `spawn_blocking` — inline all 7 FTS actor dispatch sites | `VS_FTS_INLINE_INGEST=1` | **built, awaiting measurement** |
| H0 | Profile the vector-store during steady-state ingest | — | deferred (see below) |
| H2 | Batch the actor drain — N ready messages, one lock acquisition | `VS_FTS_INGEST_BATCH=N` | queued |
| H3 | Decouple tantivy `num_worker_threads` from `perf::num_workers()` | `VS_FTS_INGEST_THREADS=N` | queued |
| H4 | `VS_FTS_ADD_LOCK=shared` — knob only; moot under H1 (single adder) | existing | queued (free) |
| H5 | Widen inter-actor channels (`perf::channel_size`, now `3 × workers` = 12) | `VS_FTS_CHANNEL_MULT=N` | queued |
| H6 | Fan out `monitor_items` dispatch, if the constraint proves upstream | `VS_FTS_SCAN_CONCURRENCY=N` | queued |

**H0 deferred, deliberately.** The plan ordered profiling first, but profiling
needs a running steady-state ingest, which needs the corpus, which lands at the
same time H1 can be measured — so profiling first would cost a cycle and buy
nothing. Two prerequisites also need doing before a profile is worth having:
`perf` is **not installed** on the SUT (`perf_event_paranoid=2`, so host-side
profiling of the container PID will need it relaxed), and the release build is
likely stripped, so a symbolised build (`[profile.release] debug = true`) is
needed or the flamegraph is unreadable. Both are cheap but neither is free.
H0 runs as soon as H1's number is known and the gap is still open.

## Interpreting each result

docs/s alone cannot distinguish the two things we care about, so every rep is
bracketed by `resource_probe` on both ScyllaDB-side containers:

| docs/s | VS cores | reading |
|---|---|---|
| up | up toward 4.0 | serialisation removed — hypothesis holds |
| up | flat ~3.5 | per-doc overhead removed, not a serialisation point |
| flat | up | more spinning, no more work — contention moved |
| flat | flat ~3.5 | constraint is upstream (CDC / `monitor_items`) → H6 |

RSS is a gate, not decoration: the vector-store stops adding documents at its
26 GiB budget and keeps answering queries, so a breach is **silent document
skipping**. `rss_bytes` must never reach `mem_limit_bytes`, and the indexed doc
count must equal the cap.

## Log

### 2026-09-07 — fleet restart + H1 implemented

Both boxes restarted with new public IPs (harness 13.49.44.192, SUT
13.60.243.144; private IPs unchanged, so `.env.sut`'s `SCYLLA_BROADCAST_RPC`
and `SCYLLA_VS_URI` still hold). Instance-store NVMe wiped on both as expected:
reformatted, remounted, docker data-root restored. **All docker images gone** —
engine images re-pulled on the SUT, vector-store image rebuilt from source.
Corpus re-download launched on the harness (tmux `dl`), ~1.8 h at ~5.8 MB/s.

H1 implemented on `knowack1/vector-store` branch `p99-fts-ingest-optimization`
(commit `282d9efc`): a `dispatch()` helper replaces the six
`worker.spawn_blocking(...).await` call sites in `fts_index/tantivy.rs`, gated
on `FtsTuning::inline_ingest`, default off. The two threshold-commit sites come
along for free — they sit inside the add/remove closures. Startup log line now
reports `dispatch=inline|worker-pool` so the A/B can assert the arm actually
took effect. 39/39 lib tests pass, including two new ones covering the inline
path (index/remove, and search) — the existing `SHARED_ADD_LOCK_UNDER_TEST`
comment makes exactly this argument: a runtime-selectable path that is never
tested will break silently.

**Harness gap found and fixed before it cost a run:**
`docker-compose.scylla.yml` wires each `VS_FTS_*` variable explicitly, and the
new one was absent — the flag would have been a silent no-op and the A/B would
have reported "no effect" from two identical arms. Added
`VECTOR_STORE_FTS_INLINE_INGEST${VS_FTS_INLINE_INGEST:+=...}`. The A/B script
also asserts the arm off the engine's own startup line rather than trusting the
environment arrived.

Driver: `tools/inline_dispatch_ab.sh` — one rung at `c_sat=64`, 1M-doc budget,
N=3, arms interleaved rep-major, `resource_probe` on both containers,
`VS_FTS_METRICS_INTERVAL=1s` for the vector-store's own received/added/lock-wait
counters.

**Next:** corpus completes → B1 smoke → B2 A/B → verdict.
