# Build-rate knob matrix — what is left to measure on the fleet

**Status: the harness is built and proven on the fleet; what remains is fleet
time.** This document used to carry the harness work as well, and the harness
was the reason the campaign was blocked: the ladder's x-axis did not measure
what the chart said it measured, and batch size reached no artifact at all.
Both are fixed and both are now demonstrated by a run rather than by a test —
see "Measured on the fleet" below. What is left is the measurement, and that is
all this document describes.

Companion documents: `results/aws-batch-axis-2026-09-08/` (the evidence behind
every number quoted here), `WRITE-PATH-TEST-PLAN.md` (the S11–S15 slides this
feeds), `TUNING.md` (why each knob is set where it is), `FREEZE.md` (the
corpus).

## Start here — which pass am I in?

Three passes, in this order. Each depends on a number only the one before it
can produce, so running them together is what the split exists to prevent.

| # | Pass | Needs | Produces | Est. wall |
|---|---|---|---|---|
| P1 | Concurrency ladder, both OpenSearch arms | fleet, corpus | `c_sat` and the ceiling per arm | **done** at N=1 in 23 min, then **superseded by P3 at N=3** |
| P2 | Batch-size axis, both OpenSearch arms | P1's `c_sat` | docs/s vs batch size at `c_sat`, with a pin probe | **done**, 62 min |
| P3 | The five-arm concurrency matrix | fleet, corpus; **R1–R3 additionally need four loader processes, per P0** | the R1→R2→R3 knob deltas against R4/R5 | R4/R5 **done**, 63 min, N=3; R1–R3 not yet run — the loader processes are plumbed, the campaign does not ask for them |
| P0 | Client calibration, no engines | fleet only | `N_max`, `LOADER_CORE_BOUND_AT`, per-level client operations/s ceilings | **done**, 17 min |

**P0 is last in the table and independent of the others on purpose.** It needs
no engines, no images and no corpus, and its output is a *post-hoc* gate:
`ftsbench.verify_generator` reads a recorded generator series and a ceilings
JSON, both files, so a pass measured before P0 can still be certified after it.
Nothing waits on it except the certification.

**How to tell which pass you are in:** look at the table below. A constant that
still reads "owed" has not been measured.

## Measured on the fleet

Everything here was measured on 2026-09-08 on the two-box fleet and is the
provenance for the numbers the later passes pin to.

| Constant | Value | How |
|---|---|---|
| Fleet | 2 x `i8g.2xlarge`, **8 vCPU** and 61 GB each, eu-north-1, private RTT 0.353 ms | `nproc`, `ping`. `HARDWARE.md`'s 16 vCPU is wrong; the deck's 8 is right |
| Corpus | 8,967,625 docs, 35,448,823,550 bytes, sha256 `1700bb6c…c50c432` | matches `FREEZE.md` exactly, re-staged from the Swedish Wikimedia mirror in 36 min |
| `vector-store` image | rebuilt from `knowack1/vector-store` @ `282d9efc` | the fork carries no annotated tag, so the provenance is the commit; the binary reports `0.0.0-dev` and the manifest must record the commit rather than that string |
| ScyllaDB FTS path | index SERVING in 6 s; `commit_threshold=disabled`; writer **4 threads x 376 MB/thread**; BM25 returns ranked rows | the `ingest tuning` and `index writer using` lines `verify_arm.py` gates on |
| R4 `opensearch-ramindex` | `c_sat` = **8**, ceiling **12,913 docs/s** | P3, batch 512, **N=3**, 1M cap. Flat from c=8 in a 2.7% band. **Supersedes P1's N=1 reading of `c_sat`=16 / 13,431 docs/s** — that curve's monotonic rise did not reproduce at N=3, so it was noise |
| R5 `opensearch-ramindex-refresh30` | `c_sat` = **8**, ceiling **13,997 docs/s** | P3, same. Refresh 30 s buys +8.4% over 3 s. **This ceiling is a LOWER BOUND**: at 1.98x under the single-process client ceiling it does not clear G7's 2x rule |
| `N_max` | **≥4, a lower bound** — neither worker ladder stopped scaling | P0: OpenSearch 27,686 → 52,469 → 99,443 docs/s at N=1/2/4; ScyllaDB 8,003 → 15,438 → 28,530 |
| `LOADER_CORE_BOUND_AT` | **0.850** OpenSearch client, **0.747** ScyllaDB client | P0, against the null sink; the old 0.70 was too conservative for OpenSearch |
| Client operations/s ceiling per batch level | **1,658.9 / 430.3 / 213.3 / 107.7 / 54.1** at batch 16/64/128/256/512 — a flat 26.5–27.7k docs/s | P0. ScyllaDB at batch 1: 8,002.7 |
| **Every arm runs four loader processes** | 8,003 docs/s in one process is 0.66x of a ~12.2k engine ceiling; N=2 is 1.26x; N=4 is 2.34x | P0. `ftsbench.mp_load` is plumbed through `sweep_build_rate.sh` (`WORKERS`/`N_MAX`) to `build_rate_point.sh` (`--workers`/`--n-max`) as of `262a227`, and the campaign now asks for it — `WORKERS=nproc/2`, which is 4 on the as-built 8-vCPU fleet. See "Four loader processes, on every arm" below |

**The concurrency axis now provably means what the chart says.** Across every
rung of P1 the OpenSearch write thread pool sat at 4.0 of 4 active with a queue
of almost exactly `c - 5` — 3 at c=8, 11 at c=16, 27 at c=32, 59 at c=64, 91 at
c=96, 123 at c=128 — and zero rejections throughout. `--concurrency` is
outstanding requests to the engine, measured rather than assumed, which is what
G2 asked for.

**And the client is not the constraint, by a wide margin.** During a c=16 point
the loader used **0.47 of one core in one thread** while the engine was pinned
at 3.99 of its 4, and the harness box as a whole sat at 0.48 of 8 cores with
1% CPU pressure. Every "the client was the bottleneck" conclusion in this
campaign's history was inferred from the shape of a throughput curve; this one
is measured. It does not retire P0 — a per-level operations/s ceiling is still
owed, and small batch levels are where it matters — but it does mean the
published-ceiling anxiety does not apply to these arms at these rungs.

**Two defects found while validating, neither in the campaign path.**
`opensearch/create_index.sh` resolves `OS_INDEX_CONFIG` relative to its own
directory and creates the index even when reading the config fails, which
yields a silently default-configured index; `os-index` runs
`os-verify-analyzer` immediately afterwards and that gate catches it (HTTP 400
on `_analyze`), so the campaign path is safe and the defect is latent.
`HARDWARE.md` records 16 vCPU per box against the measured 8.

### Four loader processes, on every arm

`262a227` wired `ftsbench.mp_load` into the campaign's scripts:
`sweep_build_rate.sh` takes `WORKERS`/`N_MAX` from the environment and
`build_rate_point.sh` forwards them as `--workers`/`--n-max` to whichever
loader the arm runs, where `mp_load.client_shape` turns them into N spawned
worker processes sharing `--concurrency` operations in flight.

`build_rate_campaign.sh` now asks for it, campaign-wide:
`WORKERS="${WORKERS:-$(( CPUS / 2 ))}"` from `nproc`, which is **4** on the
as-built `i8g.2xlarge` fleet where `nproc` reports 8 (`HARDWARE.md` "As
built" — the 16 in that document's own table is the `im4gn.4xlarge` that never
ran). Below, `N` means worker processes, not the campaign's `N=3` repetition
count.

Half the box, for two reasons that agree here. The budget: the other half stays
for the parent, the `mp.Manager`, the resource monitor and the open-loop
generator whose CPU headroom C5/C6/C7 rest on — N=8 would put 11 processes on
8 vCPU. The measurement: P0's worker ladder makes N=4 the first rung clearing
G7's 2x rule, at **2.34x** on ScyllaDB against **0.66x** in one process. So the
division is not a claim that N should track vCPU — on a box where the two
disagree, `WORKERS` is pinned rather than divided, and
`tests/test_build_rate_campaign.py` fixes both halves of that contract.

**Both decisions the earlier draft of this section left open are settled by
the count being campaign-wide.**

- **Whether the OpenSearch arms move too — yes.** `client_shape` puts `auto`
  through the process pool even at N=1 precisely so a ladder does not change
  client architecture inside its own curve; the same argument applies across
  arms being compared. Pinning only R1–R3 would make R2↔R4 and R3↔R5 cross a
  client-shape boundary, so every arm gets the same N and R4/R5 are re-measured
  at it. Their N=1 ceilings in the run table above are superseded by the AWS
  pass, not compared against it.
- **`N_MAX` stays unset.** It is the ceiling `--workers auto` divides
  concurrency by, and `mp_load` refuses to guess it because it reaches the
  artifact header as a *measured* ceiling. P0 put `N_max` at **≥4, a lower
  bound** — neither worker ladder stopped scaling — so recording 4 there would
  assert a ceiling P0 declined to claim. A fixed `WORKERS` records `workers=4`
  and claims nothing beyond it.

Two things this does **not** fix, both of which bite on the AWS pass:

1. **`verify_generator` compares against a single-process ceiling.** The
   per-level operations/s ceilings in the ceilings document are built from
   `client_ceilings.single_process()` — the N=1 points — so a 4-worker run is
   judged against roughly a quarter of the client capacity it actually had.
   That is conservative rather than wrong-way (no point can falsely pass G7),
   but it will refuse points that have 4x headroom: a ScyllaDB point at
   12.2k docs/s reads 0.66x against the N=1 figure of 8,003 and 2.34x against
   the N=4 figure of 28,530. Either P0's per-level calibration is re-run at
   N=4, or G7's verdicts on this pass are read as a floor and the arms that
   fail it are re-checked by hand against `aggregate_docs_per_s_by_workers`.
2. **The point label still does not carry the client shape.** The per-worker
   artifact header records `workers`, `shard`, `run_concurrency` and `n_max`
   (`mp_load.shard_header_fields`), so the choice stays auditable from the
   files, and the ladder logs `loader processes: workers=N` per arm. But
   `build_rate_point.sh:162` builds a label from `concurrency=` and `batch=`
   only, so one artifact's label alone does not distinguish N=4 from N=1.

## Why this campaign exists

The S12 ceiling ladder in `data/sweep-aws/` was measured 2026-09-01/09-03.
Three things invalidated it, and P1 has now replaced the first and third:

1. **The ingest driver was unified** (`295c726`). `--concurrency` used to name
   different quantities per engine, and ScyllaDB's encoding ran on one thread.
2. **Writer-buffer parity was set** (`VS_FTS_WRITER_MEMORY_MB=376`,
   2026-09-07) and never ran a ladder. `BUILD-RATE-LOOP.md` measured it at
   **1.42x**, so the published 8,992 docs/s predates a 42% change. Still owed:
   P3 is what re-measures it.
3. **The client, not the engine, was setting the ceiling** above roughly c=32
   (`results/client-model-2026-09-08/`). P3 shows the rebuilt client saturating
   the engine at c=8 and not improving above it — **12,913 docs/s** against the
   published 11,063 at c_sat=64 — with the engine CPU-saturated and its write
   pool queued throughout.

Karol's ask is to re-measure it as a knob matrix: isolate the tantivy writer
buffer and the commit cadence on the ScyllaDB side, against OpenSearch's RAM
index at matching refresh cadences, and to sweep the loader's batch size on the
OpenSearch side.

## The run table

| # | Arm (`config`) | Target flag | Stack | Knobs vs. the row above | Pairs with |
|---|---|---|---|---|---|
| **R1** | `scylla-cdc-buf15` | `--scylladb-cdc-buf15` | Scylla + vector-store | `VS_FTS_COMMIT_THRESHOLD=0`; `VS_FTS_WRITER_MEMORY_MB` **unset** → tantivy's 15 MB/thread floor; commit interval 3 s; `VS_FTS_METRICS_INTERVAL=1s` | reference for R2 |
| **R2** | `scylla-cdc-buf376` | `--scylladb-cdc-buf376` | Scylla + vector-store | **+ `VS_FTS_WRITER_MEMORY_MB=376`** (writer-budget parity with OpenSearch's 1.4 GiB node total) | R4 (both 3 s) |
| **R3** | `scylla-cdc-buf376-commit30` | `--scylladb-cdc-buf376-commit30` | Scylla + vector-store | **+ `VS_FTS_COMMIT_INTERVAL=30s`** | R5 (both 30 s) |
| **R4** | `opensearch-ramindex` | `--opensearch-ram-nostore-refresh3` | OpenSearch | `OS_RAM_INDEX=1` (tmpfs segments) + `_source: false` + `refresh_interval: 3s` | R2 |
| **R5** | `opensearch-ramindex-refresh30` | `--opensearch-ram-nostore-refresh30` | OpenSearch | **+ `refresh_interval: 30s`** | R3 |

All five arms are registered in `ftsbench/target.py`, carry their knobs there
rather than in `.env.sut`, and are the first five run lines of
`tools/build_rate_campaign.sh` (which replaced `tools/knob_matrix.sh` and
`tools/batch_matrix.sh`). Since `262a227` that script takes `run` or `dry-run`
and nothing else: it has no arm selection and no resume, so a re-run
re-measures every line. To run one arm, run its line — each is a complete
`sweep_build_rate.sh` command you can paste.
`ftsbench/verify_arm.py` asserts the vector-store actually took the arm's
tuning — an image that ignores a knob looks identical to one that honours it,
and that already cost S11–S15 once. Both gate lines were confirmed present on
the rebuilt image.

**30 s, not the 60 s originally asked for.** At 60 s, `C1_IDLE_TIMEOUT=60` is
one commit cycle and every ScyllaDB point aborts as idle; 30 s is two, and the
sweep now floors both timeouts at four cadences regardless.

## The batch-size axis (OpenSearch only)

**The ScyllaDB loader has no batch flag, so `--concurrency` is exactly the
number of simultaneous INSERTs in flight.** Karol's call, 2026-09-08, made
structural 2026-09-09 by removing `--batch-size` and `--rows-in-flight` from
that loader entirely. One operation is one document is one prepared statement.
Three things follow. Outstanding requests to the engine are `--concurrency`
exactly, on both engines, which is what G2 asked for and did not have. A knob
that could be silently wrong stops existing rather than merely being pinned.
And per-operation `service_ms` becomes the latency of one INSERT rather than of
`--batch-size` sequential round trips.

**There is no ScyllaDB batch axis to sweep.** On OpenSearch `--batch-size` is a
wire batch: N documents, one `_bulk`, one request, and the engine sees it. On
ScyllaDB there is no wire batch — every row is its own prepared statement — so
a batch would only ever have been a loop window inside the client. Sweeping it
would have measured `ftsbench`, not ScyllaDB. The ScyllaDB side therefore
contributes a **sentence**, not a series, and the flag is gone so no future
sweep can reintroduce one.

What one row per operation buys beyond honesty: `tools/sharded_build_rate.sh`
passed no `--rows-in-flight` and therefore ran at `rif = batch_size`, the mode
recorded collapsing to 488 docs/s at c=8. With no batch and no second bound that
defect cannot recur. The numbers that script already produced — the
12,228 docs/s ScyllaDB ceiling and the **1.42x** writer-buffer result R1→R2's
expectation rests on — were taken in the collapsed mode and are what P3
re-measures.

**Batch size is an OpenSearch quantity only.** The Makefile once pinned a single
`BATCH_SIZE` for both engines, for a reason that still bites on C3: batch size
sets how many documents one recorded latency covers, so a 500-document p99 is
not comparable to a 1,000-row p99. On the C1 build path the compared quantity is
documents per second, which is honest at any batch size. **C3's two arms
therefore no longer record the same quantity** — one OpenSearch latency covers a
`C3_BATCH`-document `_bulk`, one ScyllaDB latency covers one INSERT — and since
there is no CQL batch to match, the chart states the difference instead. C3 is
not in the deck and is kept as a diagnostic.

**The grid.**

| | |
|---|---|
| Arms swept | **R4** `opensearch-ramindex`, **R5** `opensearch-ramindex-refresh30` |
| Levels | `--batch-size` **16, 64, 128, 256, 512** |
| ScyllaDB arms | `--batch-size 1`, pinned, never swept |
| Concurrency | each arm's own `c_sat` from P1 — R4 at 16, R5 at 8 — plus one probe rung at `2 x c_sat` |
| Reps | **N=3** at `c_sat` and at the probe rung (Karol, 2026-09-09: one repetition count for the whole campaign) |
| Everything else | inherited from the five-arm matrix unchanged — 1,000,000-document cap, contiguous sharding, `WARMUP=1`, `OS_RAM_INDEX_SIZE=12 GiB` |

`2 arms × 5 levels × 3 reps = 30, + 2 arms × 5 levels × 3 probe reps = 30, + 4 warm-ups = 64 run points`

**Concurrency is not pinned on trust.** Offered document pressure is
`c x batch` on OpenSearch, so the `c_sat` measured at batch 512 can sit below
`c_sat` at batch 16 — and pinning would under-report the small batches by
exactly the amount that confirms "a bigger batch is faster". Each level
therefore carries one N=1 probe at `2 x c_sat`. A probe that beats its level's
median by more than the rep spread means that level has not been shown to be a
ceiling: it escalates to a three-rung mini-ladder at N=3, or it is drawn as a
lower bound with the gate that marked it named.

**What decides whether a level is starved is the write pool, not the document
count.** P1 measured the pool at 4.0 of 4 active with a queue of `c - 5`, so
what the engine needs is concurrent *requests* above its four write threads,
which every level clears at both pins. That is why the pins are the ladder's
own `c_sat` rather than something raised to keep the small levels fed, and it
is also the per-level starvation test: a level whose pool sits below 4.0 active
with an empty queue is a starved cell and a client-bound reading, however fast
its number looks.

**Both OpenSearch arms, not one.** R5 differs from R4 only in
`refresh_interval` — 30 s against 3 s — which is engine-side and has no
documented interaction with a client-side payload size. Running both answers
whether the batch optimum moves with the refresh cadence, a real question
because at 30 s the engine has more freedom in when it merges. P1 already
measured R5's ceiling **4.3% above** R4's, which is the expected direction.

**One repetition count for the whole campaign (Karol, 2026-09-09).** Every run
in `tools/build_rate_campaign.sh` is N=3, the probe rungs included, and `REPS=`
moves all of them together. The N=1 probe was defensible on cost — its verdict
is only "does this beat the median by more than the spread" — but it produced a
curve whose markers carried a spread on some points and not on others, with
nothing on the chart to say which.

**Pre-committed, so it is not decided under schedule pressure.** If the curve
is still climbing at 512, or a smaller level beats it by more than the rep
spread, the primary matrix's `OS_BATCH_SIZE` moves to the plateau value and P1
and P3 re-run at it before the deck quotes a ceiling. This axis can damage the
ceiling number as well as defend it, and the damaging case is the one a
reviewer finds first: an under-sized bulk understates OpenSearch, which is an
error in our own favour, on the side that has the bulk API.

**Cost: ~1.5 h, band 1.4–1.7 h, ~$6 at the $4.37/h fleet rate** (was ~1.4 h at
N=1 probes; the whole campaign, both passes, is ~4.0 h / 174 points at the
measured cadence). Unit costs are
manifest-to-manifest marginals, which include `reset_index`, loader startup and
settle; P1 measured a plateau point at 76–85 s of build wall against a ~82 s
point-to-point cadence, so the overhead is roughly 8%.

**Cost levers, if the schedule must shrink (decide before launch, not
mid-run).**

| Lever | Saving | What it costs |
|---|---|---|
| Drop R5, keep R4 | −0.7 h | Whether the batch optimum moves with the refresh cadence goes unanswered |
| Drop the `2 x c_sat` probes | −0.3 h | Every level becomes a slice at a pinned `c`, and the curve inherits a bias in the hypothesis's own direction |
| N=3 → N=1 on the three middle levels | −0.4 h | The middle of the curve carries no spread, so a knee inside it is not distinguishable from noise |
| N=3 → N=1 on the `2 x c_sat` probes | −0.4 h | Back to where this started: the probe's verdict ("does it beat the median by more than the spread") then has no spread of its own |

**Where it lands.** Nothing new on the main deck. S12's footer gains one line,
and the curve goes to one backup slide.

> Bulk size: OpenSearch 512 documents per `_bulk`; ScyllaDB one prepared
> statement per document — the CQL path has no wire batch, and `--concurrency`
> is one outstanding request per unit on both engines. Curve in backup B6.

A ceiling chart that cannot name its own most obvious tuning knob fails the
published-tuning fairness commitment on its face. That — not a new story — is
what licenses this second exception to `WRITE-PATH-TEST-PLAN.md`'s
one-campaign principle. The concurrency ladder was the first exception because
concurrency *is* S12's x-axis; this one is licensed because S12's footer is
presently wrong by omission.

**Backup slide `B6 — Bulk size: where we set it`.** One engine, one x-axis, so
there is no false comparison available to draw: x = documents per `_bulk`,
log2, five levels; y = docs/s at `c_sat`; R4 and R5 as two series, N=3 thin
plus median, bars min..max. Client-bound and lower-bound levels drawn hollow
with the gate that marked them named. Overlaid dashed: the measured client
operation ceiling at each level times that level, so "engine or client?" is
visible on the axes rather than buried in the footer. Rendered by
`tools/plot_batch_ceiling.py`, which refuses two engines on one axis.

Footer clauses, mandatory: what one operation is on each engine, repeated in
the footer so a cropped screenshot still carries it; that ScyllaDB runs at
batch 1 and why, so the absence of a second series is a statement and not an
omission; requests against documents in flight — at c=16 and batch 512
OpenSearch holds 16 requests carrying 8,192 documents while ScyllaDB holds
16 requests carrying 16 documents; the write pool's active/queue depth per
level, which is what says engine-bound or starved; the measured client
operations/s ceiling per level, the box it was measured on, and the ≥2x rule;
which levels are lower bounds and which gate marked them; that encode cost is
per-document work — ~16.4 µs/document on OpenSearch against ~0.62 µs on
ScyllaDB — and therefore does **not** amortise over batch size, so it is not
what makes small batches slow, the cost that amortises being the per-request
HTTP cost, which a reader will assume the opposite way round; the batch-512
bridge check against P1's `c_sat` point and by how much they agreed; the
inherited disclosures (the 1M cap is not the talk's operating point; contiguous
sharding at the cap is not the first 1,000,000 documents); and PRELIMINARY.

**The one main-deck sentence this earns.** The batching decision does not
disappear on the ScyllaDB side — it moves. On OpenSearch you size the bulk in
your client; on the CQL path each document is one statement and the cadence is
set at the index. It claims a relocation rather than fewer knobs, which is the
only version that survives Q&A next to this campaign's own 1.42x writer-buffer
result. **"They have to tune, we don't" must not be said** — R1→R2 is a 42%
tuning delta on our own side.

## Signals recorded per point

| Source | Fields |
|---|---|
| `c1-<config>-c<conc>-<rep>.jsonl` (`build_monitor`, 1 Hz) | header carries **`batch_size`** (1 on the ScyllaDB arm); samples carry `docs_indexed`, `docs_searchable`, `docs_delta`, `docs_per_s`, `docs_per_s_cumulative`, `index_status`; OpenSearch adds `segments_count`, `segments_memory_bytes`, `merges_current{,_docs}`, `merges_total{,_docs,_time_ms}`, `refresh_total{,_time_ms}`, `store_size_bytes`, and **`write_active`, `write_queue`, `write_rejected`, `write_pool_size`** |
| `vslog-<config>-c<conc>-<rep>.log` (R1–R3) | `received/s`, **`added/s`**, `lock_wait_ms/s`, `commits`, `committed/s`, cumulative totals, plus the `ingest tuning` and `index writer using N threads, M MB/thread` startup lines |
| `cpu-<config>-c<conc>-<rep>.jsonl` (`resource_probe`, 1 Hz, on the SUT) | per container: `cpu_cores_used`, `cpu_seconds_total`, `rss_bytes`, `shmem_bytes`, `cache_bytes`, `mem_limit_bytes`, `disk_read_bytes`, `disk_write_bytes`, `index_size_bytes` |
| `generator-<pass>.jsonl` (`generator_probe`, 1 Hz, on the harness) | `generator_sample` per loader process: `pid`, `cmd`, `cpu_cores_used`, `busiest_thread_cores`, `threads`, `rss_bytes`; `generator_box_sample` for the box: `cores_available`, `cpu_cores_used`, `steal_cores`, `cpu_pressure_some_ratio`, `mem_available_bytes` |
| `manifest-<config>-c<conc>-<rep>.json` | config, rep, label, cache_state, corpus, max_docs, **`batch_size`**, image pins, live engine versions, commands, host, gates |
| `summary.csv` | `config, engine, concurrency, **batch_size**, **rows_in_flight** (empty for new runs; retained so archived sweeps still concatenate), rep, docs_per_s_overall/median/mean/p10/p90/max`, `build_wall_seconds`, `throughput_variability`, `stall_fraction`, `merges_total`, `merge_time_s`, `segments_final`, `time_to_serving_s` |
| derived per arm | ceiling, `c_sat`, knee, `verify_cpu_usage` verdict, `verify_generator` verdict |

`added/s` is the throughput signal for R1–R3: documents entering the tantivy
writer, ungated by commit. The `/status` count the monitor watches only moves
at a commit, so at 30 s it resolves a 100 s build into three points. Both are
recorded; the commit-gated count becomes the **visibility** signal, which is
what varying the commit interval is actually about.

**Every level's artifacts sit in their own `OUT_DIR/b<batch>/` subdirectory**,
which is what keeps every `summary.csv`, every rendered chart and every
`verify_cpu_usage` verdict internally single-batch.

## Decisions locked

| | |
|---|---|
| Per-point doc cap | **1,000,000** (`SWEEP_DOCS`) |
| Reps | **N=3**, rep-major, **`WARMUP=1`** |
| Slow cadence | **30 s** |
| Batch size | OpenSearch **512** (the reference and the axis's top level); ScyllaDB **1** |
| Concurrency | **outstanding requests to the engine** on both sides — N `_bulk` requests, N `INSERT`s — measured, see above |
| Rows in flight | gone: `--rows-in-flight` was removed 2026-09-09, and `rows_in_flight` is no longer written to any new artifact |
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
matrix. The batch axis multiplies this contingency: with five levels per
OpenSearch arm, a top-up on the R4/R5 pair is 2 arms × 5 levels × 2 reps = 20
points, ~0.53 h.

**P1's rungs, and why they are not the published nine.** `4 8 16 32 64 96 128`,
N=1, at batch 512. The previous nine were chosen when `--concurrency` meant
something else on the ScyllaDB side; these were run against the rebuilt client
with the generator probe recording the harness box, and the write-pool depth
confirms each rung's meaning. `c_sat` is the **smallest rung reaching 97% of
the best observed**, stated rather than eyeballed: the argmax would chase noise
on a plateau and would pin the batch axis to a concurrency the engine does not
need.

## Gates that must clear

**G7 — the client's operation ceiling is measured per batch level, on both
clients.** P0 as written produces docs/s ceilings, and batch size is the one
axis on which docs/s and operations/s ceilings diverge. The gate needs a
per-process **operations per second** ceiling against the null sink, on the
async multiprocess client: for the OpenSearch client at each of the five
levels, and for the ScyllaDB client at batch 1, where one operation is one
document and the two rates are equal. A level clears at ≥2x below that ceiling
— `READ-PATH-TEST-PLAN.md`'s own threshold — with `generator_probe`'s
`cpu_cores_used` below the calibrated bound. A level that fails is marked
client-bound and drawn hollow as a lower bound — never dropped, and never
plotted as an engine number. `ftsbench.verify_generator` refuses, with a
distinct exit status, any level whose ceiling the JSON does not carry: until P0
runs, no cell is *proven* engine-bound, and a gate that defaults is a guess
inside the mechanism that exists to stop guesses.

**G8 — the pin was checked.** A level whose `2 x c_sat` probe beats its median
by more than the rep spread has not been shown to be a ceiling at that batch
size. It escalates to a three-rung mini-ladder at N=3, or it is drawn as a
lower bound with the gate that marked it named.

**Fleet re-entry, every time.** Public IPs change on stop and `/mnt/nvme` is
wiped on both boxes, taking the docker image store with it —
`daemon.json` points `data-root` at the instance store, so *every* image is
lost on stop, and that has now happened three times. Re-entry is: update the
two `HostName` entries, `mkfs.xfs` and mount `/mnt/nvme`, restart docker,
re-pull the two public images, and **rebuild the vector-store image from
`knowack1/vector-store` @ `282d9efc`** (~2 min of `cargo build --release` once
the crates are cached, plus a `docker build`; it is in no registry). The corpus
re-stages in ~36 min from `https://mirror.accum.se/mirror/wikimedia.org` at
~215 MB/s against ~5 MB/s from `dumps.wikimedia.org`, and
`FREEZE.md`'s sha256 of the prepared corpus is what proves the mirror served
the frozen bytes. Push the image to ECR or bake an AMI to stop paying this.

**Corpus sharding: contiguous.** Balanced on this corpus — the existing
`corpus-ab-a` / `corpus-ab-b` split is 2,369,432,161 vs 2,373,429,951 bytes for
600,000 documents each, 0.17% apart — and it is what `sharded_build_rate.sh`
already assumes. Order of insertion does not affect the result: BM25 term
statistics are collection-wide, and the invariant that matters is every
document exactly once, same set per engine.

**Note on the cap and sharding together:** at `SWEEP_DOCS=1000000` over N
shards, contiguous sharding takes 1M/N from the head of each shard, which is a
different *set* than the first 1,000,000 documents. Fine for engine-vs-engine
as long as both sides shard identically, but it breaks comparability with the
existing 1M-capped ladders and must be stated on the chart rather than
discovered later.

**C3 per-operation latency: keep the asymmetry, disclose it.** A ScyllaDB
operation now carries one row; one OpenSearch operation is a single `_bulk` of
`OS_BATCH_SIZE` documents. So per-operation `service_ms` is not comparable
across engines, while per-document throughput remains honest. C3's footer must
state what an operation is on each engine, and C3 itself still passes one
shared `--batch-size` to both loaders.

## P0 — client calibration (short fleet session, no engines)

**Why it exists.** Two constants the campaign depends on are estimates derived
from evidence that predates the client rewrite: `N_max = 4`, extrapolated from
a per-process ceiling measured with the OLD thread-per-operation client, and
`LOADER_CORE_BOUND_AT = 0.70`, anchored to a pre-rewrite loader at ~0.80 of one
core. The ScyllaDB anchor at `--batch-size 1` does not exist at all: every
ScyllaDB figure in the repo was taken at batch 500 or 1,000, where one
operation covered hundreds of documents.

**What it measures.** The client's own ceiling, which cannot be measured
against a real engine: at ~13,400 docs/s the engine saturates first, so the
number that comes back is the engine's. It needs a sink that never becomes the
bottleneck — `ftsbench.null_sink`, accept and discard — over synthetic
documents at enwiki's ~3.95 KB average.

| Output | Sets |
|---|---|
| per-process docs/s ceiling, per engine client | `N_max` — enough processes for ~3x headroom over the engine ceiling |
| loader `cpu_cores_used` at that ceiling | `LOADER_CORE_BOUND_AT`, from measurement rather than from a pre-rewrite anecdote |
| per-process operations/s ceiling, OpenSearch client, at 16/64/128/256/512 | G7's threshold; the docs/s ceiling does not imply it |
| per-process operations/s ceiling, ScyllaDB client, at batch 1 | G7's threshold on the tight side, and `N_max`, which the pre-rewrite figure set at a batch size the campaign no longer uses |
| is per-operation client cost fixed or proportional to payload? | which half of the per-bulk cost the small levels actually pay |
| does raising M raise per-process throughput? | whether the async layer earns its complexity, or `M=1` would do |
| does N scale linearly? | whether the box or the processes bind first |

**Where each half runs.** The sink runs on **`fts-sut`** — a localhost sink has
far lower RTT than the private network, which would understate how much
in-flight M is needed to cover latency, and the measured private RTT is
0.353 ms. The loaders run on **`fts-harness`**, the box whose ceiling the
campaign depends on. Start the sink by hand there and run with
`START_SINK=0 SINK_HOST=<fts-sut>`. The sink sets `TCP_QUICKACK`: without it a
c=8 point measured 404 docs/s against 7,845, a 40 ms kernel timer reported as
the client's ceiling.

**It ends by constructing both answers the gate can give** — a run with nothing
in the way, which must read client-bound, and the same run against a
deliberately delayed sink, which must not. The gate has never seen a positive
example, and a constructed one is the only way to test a gate whose job is to
catch a condition we hope not to meet.

**Exit condition.** The constants above are recorded in the "Measured on the
fleet" table with the run that produced them, and `verify_generator` is applied
to every already-recorded generator series.

## Execution order

1. Fleet re-entry per the gate above; verify the corpus against `FREEZE.md`.
2. Smoke: 2 rungs × all 5 arms at a 20k cap. Gates: 10/10 points complete,
   manifests carry the right `config` *and* tunables, the vector-store's
   `ingest tuning` line matches the intended arm, R3 does not abort on idle,
   R4/R5 tmpfs does not hit ENOSPC.
3. **P1** — the concurrency ladder on the arms about to be pinned. Done for
   R4/R5; P3's ScyllaDB arms need their own.
4. **P2** — the batch axis, roster rows `R4b R4p R5b R5p`, wrapped in a
   harness-box generator probe.
5. **P3** — the five-arm matrix, one arm at a time, stack recreated between
   arms. The five arm lines in `tools/build_rate_campaign.sh` are already in
   R1→R5 order, so the two ScyllaDB knob deltas land before the OpenSearch arms
   and a surprise in R2 can still change the plan; `dry-run` first, and settle
   the loader-process question above before the R1–R3 lines run. The script
   takes no arm arguments — `run R1 R2 R3` runs the whole campaign and ignores
   the names — so a subset means running those lines directly. The generator
   probe now runs per point, so no outer wrapper is needed for it.
6. **P0** — client calibration, then apply `verify_generator` to every
   recorded generator series.
7. Summarise; `verify_cpu_usage` per arm; record `c_sat` and the ceiling in
   `TUNING.md`; render `added/s` against the commit-gated count for R2/R3, and
   the batch curve with `tools/plot_batch_ceiling.py`.

## Caveats to carry into the write-up

- **The 1M cap is not the talk's operating point.** `BUILD-RATE-LOOP.md`
  iteration 9 measured the engine ranking *inverting* between a 1.2M and the
  8.97M corpus (ScyllaDB 1.15x → OpenSearch 1.10x). A ceiling ladder at 1M
  answers "what saturates", not "what wins at scale".
- **The ramindex arm cannot hold the frozen corpus.** Measured 2026-09-08:
  `OS_RAM_INDEX_SIZE=12 GiB` filled at **4,025,699 documents** — 45% of the
  corpus — with `No space left on device`, a failed shard and a red cluster
  mid-run, at roughly 3 GB of tmpfs per million documents. It cannot be raised
  at parity, because tmpfs counts against the container's memory and the full
  corpus would need an `OS_MEM_LIMIT` near 55–70g on a 61 GB box. So any
  full-corpus OpenSearch number has to come from the disk-store arm, which does
  not pair with a ramindex ceiling, and growth curves on the ramindex arm are
  bounded by the arm rather than by choice. `TUNING.md` §8 carries the
  arithmetic.
- **A batch optimum found at the 1M cap is not the batch optimum.** The best
  bulk size is a function of document size and of the engine's segment-merge
  pressure, and the second changes with corpus size. The axis answers "what did
  we set it to, and was that defensible", not "what is optimal at scale".
- **One row per operation buys the axis its meaning and spends ScyllaDB's
  client headroom.** Making `--concurrency` one INSERT in flight is what lets
  the two ladders be read against each other at all, and removing
  `--rows-in-flight` retires a knob that could be silently wrong. The price is
  that a ScyllaDB point dispatches one asyncio task per document, so the
  client's own ceiling sits closest to the engine's on that side. If P0 finds
  the ScyllaDB client binding, the answer is more worker processes — a batch
  is no longer available, which is the point: it would restore the very
  ambiguity this decision removed.
- **OpenSearch gets 4 cores; the ScyllaDB stack gets 8.** `.env.sut` pins
  `OS_CPUS=4`/`OS_CPUSET=4-7` against `SCYLLA_CPUSET=0-3` plus
  `VS_CPUSET=4-7`, so the two-container ScyllaDB stack has twice the CPU
  allocation of the one-container OpenSearch arm. Irrelevant *within* the batch
axis, which compares OpenSearch to itself at a fixed 4 cores, and load-bearing
  for any R2↔R4 or R3↔R5 comparison. It must reach the S12 footer.
- **Encode cost is asymmetric.** Measured on the laptop at enwiki's average
  document size: OpenSearch's `bulk_payload` costs 8.19 ms per 500-document
  batch against ScyllaDB's 0.31 ms — **26.7x**, ~19% of one core at the engine
  ceiling, landing entirely on OpenSearch's side. It is already outside the
  latency window (`timed_op` wraps only `send`), but it is a harness artifact
  in a comparison whose credibility rests on symmetry.
- **The 50/50 cpuset split is probably wrong for the CDC path**
  (`BUILD-RATE-LOOP.md`): during writes Scylla sits at 3.56/4 while the
  vector-store idles near 1.9/4 — it is CDC-starved, not CPU-bound. Out of
  scope here, but P3's `cpu_cores_used` will strengthen or weaken it.
- **Dropping an FTS index does not release its in-RAM memory.** Measured mild
  at this cap (vector-store RSS 0.74 → 0.82 → 0.83 GiB across three 200k
  points), so a warm-container ladder is safe at 1M — but the probe gate is
  what proves it per arm rather than per campaign.
