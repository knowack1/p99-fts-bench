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

**Iteration corpus decision (2026-09-07).** The frozen enwiki corpus needs
~2.5–3 h to re-download, and B2 only needs 1M documents. Shards download
sequentially, so the first 9 (~138k docs each ≈ 1.24M) arrive in ~25 min.
`make corpus` therefore builds a **separate** 1.2M-doc file at
`/mnt/nvme/data/corpus-ab.jsonl`, leaving the frozen `corpus.jsonl` untouched.

The trade this makes, stated so it is not forgotten: this is **not** the
`FREEZE.md` corpus, so its absolute docs/s is **not comparable** to the 8,992 /
11,063 ceilings, which were measured on the frozen corpus. What the A/B yields
is a **ratio** between two arms over identical documents. The ratio decides
whether a hypothesis is kept; the absolute number against OpenSearch is settled
only by B4 on the frozen corpus. Any figure taken from `corpus-ab.jsonl` is
iteration telemetry and must never reach a slide.

Second disclosure: the A/B runs while the remaining shards are still
downloading on the harness, which also hosts the load generator. Arms are
interleaved rep-major precisely so shared drift lands on both arms instead of
being attributed to one — but if the two arms' spread is wide, the download
overlap is the first thing to suspect, and the A/B should be repeated on a
quiet box before anything is concluded.

**Image:** `scylladb/vector-store:1.10.0-44-g282d9efc-arm64`, built on the
harness and loaded on the SUT. `.env.sut`'s pin moved to it from
`1.10.0-43-ge242fa3-arm64`, which the instance-store wipe erased. The new image
is a strict superset — with `VECTOR_STORE_FTS_INLINE_INGEST` unset the ingest
path is the previous behaviour — so the flag-off arm still measures stock.

**Plumbing validated end to end before spending a run** (no corpus needed —
the actor logs its tuning when the index is created). Both arms read back off
the engine's own startup line on the live SUT:

```
arm off: … commit_threshold=disabled add_lock=exclusive dispatch=worker-pool  metrics_interval=None
arm on:  … commit_threshold=disabled add_lock=exclusive dispatch=inline       metrics_interval=Some(1s)
```

That exercises the whole chain: compose wiring → `.env.sut` → environment →
image → config parsing → actor behaviour → log. It is the check that would have
caught the missing compose entry, and it is now an assertion inside the A/B
script rather than a thing to remember.

**Second fleet trap fixed in the same pass:** the A/B script originally invoked
`ftsbench.resource_probe` directly. The probe reads `/sys/fs/cgroup` on
whichever machine runs it, and `DOCKER_HOST=ssh://` cannot carry that — on the
harness it would have recorded the *generator* box's idle cgroups while
reporting them as engine CPU and RSS. Routed through `tools/sut_probe.sh`, which
runs it on the SUT and copies the series back. This is the same failure that
cost a 1.7 h growth-run redo on 2026-09-03.

`VS_FTS_METRICS_INTERVAL=1s` is set identically for both arms — it is an
instrument, not a variable under test, and setting it on one arm only would make
its cost look like an effect.

### Iteration 1 — H1 (remove `spawn_blocking`): **NULL RESULT, +0.1%**

1M docs, c=64, N=3, arms interleaved rep-major, 1.2M-doc iteration corpus.

| arm | docs/s per rep | median | VS peak CPU | VS peak RSS |
|---|---|---|---|---|
| off (worker-pool) | 9,663 / 9,604 / 9,650 | **9,650** | 3.61 / 4 | 3.2 GiB |
| on (inline) | 9,606 / 9,689 / 9,661 | **9,661** | 3.55 / 4 | 2.4 GiB |

`on/off = 1.001x`. Rep-to-rep spread 0.6–0.9%, far tighter than the ±3% budgeted
— so this is a clean null, not an inconclusive run. **H1 is rejected as a
throughput change.** (Keep the flag: it is harmless, default-off, and removes a
genuine per-document cost that will matter once the real constraint is lifted.)

The vector-store's own counters said so before the medians did:

```
received=10281/s  added=10281/s  lock_wait=0.4ms/s
```

`received == added` means the FTS actor absorbs everything handed to it in real
time. Removing `spawn_blocking` had nothing to remove. This also **reinterprets
the evidence that started the investigation**: the vector-store sitting at ~3.5
of 4 cores was read as "saturated at a parallelism ceiling", but the honest
reading is "not being fed any faster".

### Iteration 2 — where the ceiling actually is

Two decisive measurements, both on the 1.2M iteration corpus:

**a) The FTS index path looked nearly free — RETRACTED, see iteration 4.**

| configuration | docs/s |
|---|---|
| base table only, **no FTS index** | 9,806 |
| base table + CDC + FTS index | 9,650 |

Read at the time as "the whole index path costs **1.6%**". **That conclusion was
wrong**, and the error is instructive: both figures were taken at a
*client-bound* operating point, where the generator was the constraint in both
configurations. Comparing two measurements of the same client tells you nothing
about what sits behind it. Corrected in iteration 4 — measured generator-free,
the FTS path costs ~36%, not 1.6%.

**b) The ~9.7k ceiling is the load generator, not either engine.**

| loaders | per-process docs/s | aggregate |
|---|---|---|
| 1 | 9,806 | 9,806 |
| 2 (disjoint halves) | 9,574 + 9,542 | **~19,100** |

Throughput nearly doubles with a second process, and each loader process sits at
~80% of one core — `scylla_load` is **GIL-bound at ~9.5–9.8k docs/s per
process**. ScyllaDB's base-table write path absorbs at least 19k docs/s.

**This means the published `scylla-cdc` ceiling of 8,992 docs/s is a
client-side artifact, not an engine ceiling.** `TUNING.md` §6–7 documents
exactly this failure on the read side — where it was fixed with a sharded
multi-process runner (`cell_bench_mp`) — and the write path never got the same
treatment. `verify_cpu_usage` did not catch it because it checks *engine* CPU
saturation, and by that test the ScyllaDB side correctly looked unsaturated;
nothing was watching the generator.

Note the asymmetry this creates with OpenSearch, which is the crux for the deck:
OpenSearch's 11,063 was measured **CPU-pinned at 3.97/4.00**, so that one *is* a
genuine engine ceiling. Comparing a client-bound ScyllaDB number against an
engine-bound OpenSearch number is not a comparison of engines.

**Two false alarms recorded, because both were nearly published:**

- A 2-loader run reported the index reaching exactly 1,000,000 of 1.2M docs and
  `build_progress: 100.0` — which looked like the silent-document-skipping
  failure mode. It was not: re-querying minutes later returned 1,200,000. The
  monitor's idle timeout had fired while the index was still draining its CDC
  backlog. **The build-rate figure read off that series (13,282 docs/s) was
  premature and is void.** Re-running with `--idle-timeout 240`.
- The first 2-loader test pointed both loaders at the same corpus prefix, so
  they wrote identical primary keys. Base-table throughput was still valid
  (same write-op count) but the index count was meaningless. Fixed by splitting
  the corpus into disjoint halves and verifying the first ids differ.

### Queue, re-ordered by the evidence

| # | hypothesis | status |
|---|---|---|
| H1 | remove `spawn_blocking` | **rejected** — null, +0.1% |
| **H7** | **shard the write loader across processes** (mirror `cell_bench_mp`) | **new head** — the measured constraint |
| **H8** | **re-measure both engines generator-free**, then re-derive the real ratio | **new** — decides whether any engine work is warranted at all |
| H2/H3/H5 | batch actor drain / tantivy threads / channel widths | **demoted** — all target the 1.6% |
| H4 | `VS_FTS_ADD_LOCK=shared` | moot under H1; untested, free |
| H6 | fan out `monitor_items` | still open, but only reachable once the client stops binding |
| H0 | profile | **not warranted yet** — profiling the vector-store would profile the wrong process |

### Iteration 3 — both engines, generator-free, N=3

`tools/sharded_build_rate.sh`, 2 disjoint corpus shards → 2 loader processes,
1.2M-doc iteration corpus, `c=64`, fresh index per rep, `build_monitor` on each
engine's own searchable count.

| engine | docs/s per rep | median | engine CPU | RSS |
|---|---|---|---|---|
| `scylla-cdc` | 12,229 / 12,228 / 11,917 | **12,228** | scylla 3.91/4 + VS 3.89/4 | 14.9 + 5.8 GiB |
| `opensearch` | 10,181 / 10,952 / 10,656 | **10,656** | 4.01/4 | 14.9 GiB |

`scylla-cdc / opensearch = 1.148x`. All six reps indexed the full 1,200,000
documents; no rep approached its memory limit.

**Both engines are now CPU-saturated** — OpenSearch at 4.01/4, the ScyllaDB
side at 3.91 and 3.89 of 4 each. That is what makes this the first genuine
engine-versus-engine build-rate measurement in the campaign: previously only
OpenSearch was at its ceiling.

**The two honest readings, both of which belong on the slide:**

- **Per box (the campaign's framing):** on one 8-vCPU host, the ScyllaDB stack
  — database *and* index — builds at **12,228 docs/s** against OpenSearch's
  **10,656**, a **1.15x ScyllaDB win**. This is the comparison `SUT-CONFIG.md`'s
  50/50 split was designed for: OpenSearch gets 4 cores and the other 4 sit idle
  as the "database slot", because an OpenSearch deployment still needs a
  database holding the source documents.
- **Per core:** ScyllaDB spends ~7.8 cores to OpenSearch's ~4.0 — **1,568 vs
  2,664 docs/s per core, so OpenSearch is ~1.7x more CPU-efficient.**

Both are true and they point opposite ways. `TUNING.md` §4.1 has flagged this
CPU asymmetry as unresolved since the laptop pass; it is now load-bearing rather
than a footnote, because it is the difference between "ScyllaDB is faster" and
"OpenSearch does more with less". Reporting only the first would be exactly the
re-framing `../CLAUDE.md` forbids.

### Status against the loop's goal

The original target — lift `scylla-cdc` from ~9k to near OpenSearch's ~11.7k —
is **met and passed, at 12,228 docs/s**. But it was not met the way the task
assumed. Nothing about the vector-store was optimised:

| contribution | effect |
|---|---|
| removing `spawn_blocking` (H1) | **+0.1%** — null |
| writer-buffer parity (376 MB/thread, set 2026-09-07, first SUT run) | folded into the flag-off baseline; not separable yet |
| **removing the single-process generator ceiling** | **the rest** |

The gap the task set out to close was substantially **an artifact of the
measurement**, not a property of either engine. The vector-store's FTS ingest
path costs 1.6% of the build and was never the constraint.

**Remaining to make this quotable:** B4 on the frozen `FREEZE.md` corpus. Every
number above is from the 1.2M iteration corpus and is a ratio only.

### Iteration 4 — decomposing the win, and correcting iteration 2

Same sharded generator, N=3, only the tantivy writer buffer changed:

| `VS_FTS_WRITER_MEMORY_MB` | docs/s | VS CPU | VS RSS |
|---|---|---|---|
| 15 (tantivy's floor = stock) | **8,608** | **3.98 / 4** | 3.3 GiB |
| 376 (parity with OpenSearch) | **12,228** | 3.89 / 4 | 5.8 GiB |

**The writer buffer is worth 1.42x** — not the +24% the laptop measured, because
on the laptop the client was *also* binding and masked most of it. The mechanism
is visible in the CPU column: at 15 MB the vector-store burns **more** CPU
(3.98 vs 3.89 of 4) while delivering 30% **fewer** documents. That is the merge
storm `results/fts-bottleneck-2026-08-27` measured at ~9x segment merges —
cycles spent merging undersized segments instead of indexing.

**Both fixes were necessary; neither alone was sufficient.**

| configuration | docs/s |
|---|---|
| 15 MB buffer, 1 loader (≈ the published 8,992) | ~8.6–9.0k |
| 15 MB buffer, 2 loaders | 8,608 — **generator fix alone buys nothing** |
| 376 MB buffer, 1 loader | 9,650 — **buffer fix alone buys little** |
| 376 MB buffer, 2 loaders | **12,228** |

At the stock 15 MB buffer the *vector-store* is genuinely the bottleneck
(CPU-pinned at 3.98/4), so adding client capacity does nothing. Fixing the
buffer lifts the engine ceiling above the single-process client ceiling, at
which point the **client** becomes the constraint. Only removing both gets
12,228. This is why the investigation kept finding "the bottleneck moved": it
did.

**Where the plateau actually is.** A third point settles whether 376 MB is
merely *enough* or merely *arbitrary*:

| `VS_FTS_WRITER_MEMORY_MB` | docs/s | VS CPU | VS RSS |
|---|---|---|---|
| 15 (stock floor) | 8,608 | 3.98 / 4 | 3.3 GiB |
| **376 (parity)** | **12,228** | 3.89 / 4 | 5.8 GiB |
| 768 | 12,229 | 3.85 / 4 | 5.4 GiB |

Doubling the buffer past parity buys **+0.01%** — nothing. So the plateau is
real, it simply sits far above where the laptop placed it (that pass concluded
"gains stop by 64 MB", measured with the client binding). **The parity value is
also the optimal value**, which is the convenient case: there is no tension
between configuring the two engines fairly and configuring the vector-store
well, and the deck does not have to choose.

**Correction to iteration 2's headline claim.** Measured generator-free, the
FTS index path is *not* nearly free:

| configuration (2 loaders) | docs/s | FTS cost |
|---|---|---|
| base table only, no index | ~19,100 | — |
| base + CDC + FTS @ 376 MB | 12,228 | **−36%** |
| base + CDC + FTS @ 15 MB | 8,608 | **−55%** |

The earlier "1.6%" compared two runs that were *both* pinned at the client
ceiling — it measured the generator twice. Building the index costs roughly a
third of ScyllaDB's write throughput, which is a real and quotable number, and
the opposite of what iteration 2 concluded. **H2/H3/H5 are therefore not
"aimed at 1.6% of the problem" and should not have been demoted on that
reasoning** — though they remain unattractive while the vector-store is only at
3.89/4 cores and the remaining headroom is unclear.
