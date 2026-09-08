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

**[OVERSTATED — corrected in iteration 4.]** This was read at the time as
"the published 8,992 is a client-side artifact, not an engine ceiling". That
goes too far. At the **stock 15 MB writer buffer** the vector-store is
CPU-pinned at 3.98/4 and a second loader process buys *nothing* (8,608 vs
~8,992) — so at the configuration that produced 8,992 the **engine** was the
binding constraint, not the client. The generator ceiling is real, but it only
becomes the constraint *after* the buffer fix raises the engine above it. The
correct statement is the one in iteration 4: two constraints, in series,
neither sufficient alone. `TUNING.md` §6–7 documents
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

### Iteration 5 — is two loaders actually enough?

The 2-loader number is only an engine ceiling if the generator is no longer the
constraint. Both ScyllaDB services sat at ~3.9/4 cores, which *suggests* engine-
bound — but that is the same inference that produced the wrong answer in
iteration 2, so it is tested rather than assumed.

| loader processes | index docs/s (N=3) | client capacity offered |
|---|---|---|
| 2 | 12,228 | ~19,100 |
| 3 | 11,918 | **~28,000** (9,424 + 9,455 + 9,134) |

A third process adds 47% more client capacity and the index rate does not move
(−2.5%, inside noise). With ~28k docs/s on offer and 11.9k absorbed, the
generator is delivering more than twice what the engine takes.

**12,228 docs/s is therefore a genuine ScyllaDB engine ceiling**, at
`VS_FTS_WRITER_MEMORY_MB=376` on the 50/50 8-vCPU split — the first one this
campaign has measured, since every prior ScyllaDB build-rate figure was taken
through a single GIL-bound loader.

### Iteration 6 — H1 re-tested at the corrected operating point

Iteration 1 rejected H1 at 9,650 docs/s — a **client-bound** point, where saving
CPU inside the vector-store could not possibly show up. That is the same flaw
that produced the retracted "1.6%" claim, so the null had to be re-earned rather
than trusted.

| dispatch | docs/s (N=3) | VS CPU |
|---|---|---|
| worker-pool | 12,228 | 3.89 / 4 |
| inline (`VS_FTS_INLINE_INGEST=1`) | 12,229 | 3.86 / 4 |

**Still null: +0.008%**, now measured where the vector-store *is* the
constraint. H1 is properly rejected. Inline dispatch does free a sliver of CPU
(3.86 vs 3.89) — the per-document channel hop and atomics are real — but too
little to convert into throughput.

**Why no dispatch-level change will help from here.** Both engines are now
CPU-saturated inside their cgroup allocations:

| | docs/s | CPU | of allocation |
|---|---|---|---|
| ScyllaDB (scylla 3.90 + VS 3.86) | 12,229 | ~7.76 cores | **97% of 8** |
| OpenSearch | 10,656 | ~4.01 cores | **100% of 4** |

The vector-store's remaining cycles go to tokenization and tantivy indexing —
inherent work, not coordination overhead. H2 (batch drain) and H5 (wider
channels) attack coordination, which H1 just measured at under 0.03 cores of
headroom. H3 (more tantivy threads) cannot help a process already at 97% of a
4-core cpuset. **The configuration- and dispatch-level hypotheses are
exhausted**; further gains need either more cores or a change inside tantivy's
indexing itself, neither of which is a benchmark-harness question.

### Iteration 7 — full scale behaves differently from 1.2M (B4, in flight)

The frozen corpus is verified byte-identical to `FREEZE.md`
(8,967,625 docs, sha256 `1700bb6c…c50c432`). B4 rep 1, sharded generator,
376 MB buffer: **8,408 docs/s** — against **12,229 on the 1.2M corpus**.

**So the 12,229 headline is corpus-size-dependent and must not be quoted as
"the" build rate.** The right published comparison for a full-corpus number is
S11's **7,567 docs/s**, not S12's 1M-capped 8,992; against that the honest gain
is **≈+11%**, not the +36% the small-corpus runs implied. This is the third
instance of the same error class in this loop — comparing measurements taken at
different operating points — and it is exactly what S13 ("build rate as the
index grows") exists to show.

**The constraint moves during a full-scale build.** Per-quartile mean CPU,
rep 1:

| phase | scylladb | vector-store |
|---|---|---|
| first 25% | **3.34** | 2.37 |
| mid | 2.99 | 2.23 |
| last 25% | 2.24 | **3.16** |

Two distinct regimes. The loaders deliver all 8.97M rows in ~8 minutes at
~19k docs/s, but the index needs ~18 — so the back half is a **drain phase**
with no client pressure, where the vector-store works alone on the CDC backlog.
Early on the database side dominates; late, the index does.

Neither side is CPU-saturated on average (~5.5 of 8 cores total), unlike the
1.2M runs where both sat at ~3.9/4. **This reopens the demoted hypotheses at
full scale**: during the drain the vector-store is the constraint at only
~3.16/4 cores, so something inside it serialises with ~0.8 cores of headroom —
the conditions under which H2 (batch drain) or H3 (tantivy threads) could
matter, and which the 1.2M tests could not have detected because there the
vector-store was already at 3.86/4.

**Next after B4:** re-run H1 inline at *full* scale against B4's worker-pool
baseline. H1 was rejected twice, but both tests were at 1.2M where the
vector-store was near-pinned; the drain phase is a different regime.

### Iteration 8 — B4 finds a vector-store memory leak, and a bug in my own driver

ScyllaDB B4, full frozen corpus, N=3: **8,408 / 8,430 / 6,562 docs/s** — and
rep 3 **failed its document-count gate at 8,952,708 of 8,967,625: 14,917
documents missing.** The vector-store log says exactly what happened:

```
15:38:05 INFO  Memory usage above limit (27935911936), cannot allocate more memory
15:38:05 ERROR Unable to add document for index wiki.articles_body_fts: not enough memory
15:38:06 INFO  Memory usage below limit (27592896512), can allocate more memory
```

A ~1 second budget breach, documents dropped, then recovery — and the index
still reported `SERVING`. This is the **silent document skipping** failure mode
the harness gates for, observed for real. In
`fts_index/tantivy.rs` the `AddDocument` arm does
`if !can_allocate_memory(..) { continue; }` — the message is discarded, with no
retry and nothing propagated to the writer.

**Root cause: dropping an FTS index does not release its in-RAM memory.**
Per-rep vector-store RSS, same container throughout:

| rep | start | peak | end |
|---|---|---|---|
| 1 | **0.25 GiB** (fresh container) | 19.00 | 12.82 |
| 2 | **12.36 GiB** | 24.17 | 13.14 |
| 3 | **12.38 GiB** | **27.68 → breach** | 24.78 |

`DROP INDEX` + `DROP TABLE` + `DROP KEYSPACE` between reps leaves **~12.4 GiB**
resident. The baseline never returns to 0.25 GiB, so each rep starts higher
than the last until the budget is breached. **This is a genuine product finding,
independent of the benchmark**: for an index that is in-RAM by design, memory
not being reclaimed on drop is an operational problem, and its failure mode is
silent data loss rather than an error.

**And a defect in `tools/sharded_build_rate.sh`:** it calls `start_stack` once
and only resets the *index* per rep, inheriting the warm-container approach from
`sweep_build_rate.sh`. That is correct for capped ladder points — deliberately
so, to avoid paying a cold-JVM artifact per point — but wrong for full-corpus
reps, where `WRITE-PATH-TEST-PLAN.md` Step 3 specifies **cold repetitions with
the container recreated**. Combined with the leak it compounds per rep.

**Consequences:**

- **Only rep 1 (8,408 docs/s, 19.0 GiB peak, zero drops) is a clean full-corpus
  measurement.** Reps 2 and 3 are void — 3 for the gate failure, 2 because it
  built on 12.4 GiB of retained heap.
- The 1.2M-corpus results **stand**: they ran warm too, but at that scale the
  index is small, and the three reps agreed to 2.5% (12,229 / 12,228 / 11,917),
  so the retained memory did not measurably affect throughput there.
- **`VS_FTS_WRITER_MEMORY_MB=376` is not safe at full corpus scale on this box.**
  It lifts vector-store RSS from the ~14.8 GiB the campaign measured at the
  15 MB floor to ~19 GiB on a *clean* rep — leaving only ~7 GiB of headroom
  under the 26 GiB budget, and none at all once anything is retained. The
  1.2M runs that blessed 376 MB could not have detected this.

**Next:** fix the driver to recreate the stack per rep at full corpus, then
re-run. Also worth testing 128 MB at full scale — at 1.2M it was
indistinguishable from 376 MB, and it would buy back several GiB of headroom.

### Iteration 9 — the ranking INVERTS at full scale

OpenSearch's B4 reps are **not** affected by the leak: its RSS is flat at
14.75 → 14.85 → 14.85 GiB, bounded by the fixed 14 GiB JVM heap with the index
on disk. So its warm reps are valid, and only the ScyllaDB side needs a
cold-rep re-run.

Clean full-corpus reps, sharded generator, both engines:

| | 1.2M iteration corpus | **8.97M frozen corpus** |
|---|---|---|
| `scylla-cdc` | 12,228 | **8,408** |
| `opensearch` | 10,656 | **9,265** |
| ratio | **ScyllaDB 1.15x** | **OpenSearch 1.10x** |

**The ranking reverses.** ScyllaDB wins on a small index and loses on the real
corpus. Everything this loop concluded from the 1.2M corpus — including the
"ScyllaDB is 1.148x faster" headline — describes an operating point the talk
does not care about.

Against the published pair (S11: OS 9,687 vs CDC 7,567, **1.28x OpenSearch**),
the corrected full-corpus pair is **1.10x OpenSearch**. So the writer-buffer and
generator fixes are real and worth ~+11% on the ScyllaDB side, but they
**narrow** the gap rather than reverse it. That is the honest headline, and it
is not the one the small-corpus runs suggested.

Why the inversion is plausible rather than an artifact: the vector-store's index
is in-RAM and its cost grows with index size — the same growth that drives the
drain phase in iteration 7 and the memory pressure in iteration 8 — while
Lucene's tiered merge policy over on-disk segments scales more gently. A
comparison taken at 13% of the corpus flatters the in-RAM engine.

**Status: preliminary at N=1 per engine.** OpenSearch reps 2–3 are running;
ScyllaDB needs cold reps. Direction is clear but the magnitude is not yet
settled.

### Iteration 10 — B4 final, and what the premise actually was

| engine | docs/s per rep | median | CPU | RSS |
|---|---|---|---|---|
| `opensearch` | 9,265 / 9,525 / 9,787 | **9,525** | 4.00 / 4 | 14.9 GiB (flat) |
| `scylla-cdc` | 8,408 / 8,430 / ~~6,562~~ | **8,408** | 3.95 + 3.96 / 4 each | 23.8 + 24.2 GiB |

ScyllaDB rep 3 is void (gate failure, 14,917 docs lost) and rep 2 is
methodologically compromised (built on 12.4 GiB of retained heap), so
**ScyllaDB is N=1-clean** pending the cold-rep re-run. OpenSearch is a clean
N=3 — its RSS is flat, so warm reps are valid for it.

**Full-corpus result: OpenSearch 9,525 vs ScyllaDB 8,408 = 1.13x OpenSearch.**

**The original premise conflated two different measurements.** "Scylla ~9k vs
OpenSearch 11.7k" put ScyllaDB's full-corpus-scale number against OpenSearch's
**1M-document capped ceiling**. There was never a single operating point at
which that pair was measured. Put on equal footing, the two engines *cross over*:

| operating point | ScyllaDB | OpenSearch | winner |
|---|---|---|---|
| 1.2M docs (capped) | 12,228 | 10,656 | ScyllaDB 1.15x |
| 8.97M docs (full corpus) | 8,408 | 9,525 | **OpenSearch 1.13x** |

That crossover is the real finding, and it is a better talk point than either
number alone: the in-RAM index is faster while it is small and loses as it
grows, which is exactly what an in-RAM design predicts and what S13 is for.

**Net movement against the published campaign** (S11: OS 9,687 vs CDC 7,567 =
1.28x OpenSearch): the gap closes to **1.13x**. ScyllaDB gained ~+11%
(7,567 → 8,408) from the writer buffer plus the generator fix; OpenSearch is
unchanged within noise (9,687 → 9,525), which independently confirms it was
never generator-bound at full corpus.

**CPU asymmetry, restated at the scale that matters:** ScyllaDB spends ~7.91
cores to OpenSearch's 4.00 — **1,063 vs 2,381 docs/s per core, OpenSearch 2.2x
more efficient.** At full corpus it wins on both axes, so the "per box vs per
core" tension from iteration 3 disappears: that tension only existed at the
small-corpus operating point.

### Iteration 11 — the merge storm, measured directly in CPU

Same sharded generator, same 1.2M corpus, N=3, **only the writer buffer
differs** — so this isolates the buffer from the generator:

| buffer | scylladb | vector-store | VS / scylla |
|---|---|---|---|
| 15 MB (stock) | 2.00 | **3.76** | **1.88x** |
| 376 MB (parity) | 2.77 | **2.99** | **1.08x** |

**The vector-store drops 3.76 → 2.99 cores (−20%) from the buffer alone.**
That is the merge storm `results/fts-bottleneck-2026-08-27` inferred from merge
counts (~9x more merges at the 15 MB floor), now visible directly as **~0.8 of a
core spent merging undersized segments instead of indexing**. It also explains
the long-standing observation that the vector-store "always ate more CPU than
ScyllaDB": at stock it did, by 1.88x; at parity the two sides are level.

**ScyllaDB's 2.00 → 2.77 is not more work** — it is a measurement-window
artifact. Scylla performs identical writes in both configurations and the
loaders finish at ~63 s either way, but the whole build shortens from 139 s to
98 s, so the write-saturated phase is 64% of the window instead of 45% and the
median rises. Same numerator, smaller denominator.

**Consequence for the growth charts.** The full-corpus S14-style chart at
376 MB shows ScyllaDB *above* the vector-store early, inverting the original.
That inversion is **two effects compounded**, and only the first is an engine
property:

- buffer → vector-store down ~0.8 core (isolated above);
- sharded generator → ScyllaDB up early, because two loader processes deliver
  ~18,900 docs/s while the index consumes ~6,500. Measured on b4 rep 1: the
  loaders finished all 8,967,625 rows at **t=474 s**, when the index held only
  **3,060,955** — which is exactly where the curves cross. The remaining
  **592 s (56% of the build) is pure drain with no write load at all.**

**So the sharded generator is right for ceilings (S12) and wrong for growth
charts (S13/S14/S15)**: it front-loads every write into the first 44% of the
build instead of pacing them alongside indexing, which is what the original
single-loader runs did. Those charts should use `scylla_load --target-rate`
(the pacer already exists) to hold writes near the index's own rate, rather
than saturating and then draining.

**Retraction:** the earlier note that "the ~7.0–7.5M dip disappeared after the
buffer fix" **cannot be supported** — that comparison changed the buffer *and*
the generator, and the generator alone reshapes the curve. Settling it needs a
15 MB full-corpus run through the same sharded generator, which was never taken.

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
