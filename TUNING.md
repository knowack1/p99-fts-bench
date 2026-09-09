# Published tuning — every knob, both sides

`../CLAUDE.md` makes published tuning a load-bearing fairness commitment: a
P99 CONF comparison that cannot say what each engine was configured with is not
a comparison. This document is that publication. It covers the **laptop
shakeout** values in `docker/.env`, `opensearch/index-config.json`,
`scylladb/schema.cql` and the two loaders, and it marks each value **measured**
(chosen because a measurement said so) or **assumed** (chosen by default,
symmetry, or judgement, and not yet tested).

> Every value below is a laptop-simulation value, disqualified from quotation by
> the header of `docker/.env`. The AWS/enwiki campaign replaces the numbers in
> this table; it does not replace the table.

## 1. Client / loader knobs

| Knob | OpenSearch (`opensearch_load`) | ScyllaDB (`scylla_load`) | Why | Evidence |
|---|---|---|---|---|
| Batch size | `--batch-size 500` (default); the axis the build-rate matrix sweeps | **no such flag** — one operation is one prepared INSERT | There is no CQL wire batch to size, so a batch flag on that side could only ever set a client-side dispatch window while reading like a wire quantity. Removed 2026-09-09. | **measured** — see §4 and "Batch size on the C1 build path" |
| Client concurrency | `--concurrency` = operations in flight | `--concurrency` = operations in flight, **and the only write-side knob** | **Unified 2026-09-08** (`ftsbench.load_driver`). The Makefile passes the same `INGEST_CONCURRENCY` to each. Recorded in the artifact header with an explicit `concurrency_unit`. | **measured** |
| Meaning of "concurrency" | operations (`_bulk` requests) in flight | operations (single INSERTs) in flight | Was the harness's worst asymmetry: the flags shared a name, not a quantity. Both now dispatch through one driver, so they share the quantity too. What one request *carries* still differs — N documents versus one row — and that is the real difference between a bulk API and per-row CQL, so it belongs in the chart footer rather than in a knob. The ScyllaDB-only `--rows-in-flight` was a second bound that could only disagree with `--concurrency`; removed 2026-09-09. | measured |
| Offered rate | `--target-rate` docs/s, `0` = closed loop | `--target-rate` docs/s, `0` = closed loop | C3 needs a *controlled* offered rate. At saturation the recorded tail is queueing delay, not engine behaviour. | assumed (rate to be picked from the C1 maxima) |
| Dispatch schedule | fixed intended-start schedule (`ftsbench.pacer`) | same | Coordinated-omission safety. `queue_ms` is recorded per op so the reader can check it. | measured (unit-tested) |
| Document identity | explicit `_id = page_id` | `article_id = uuid5(page_id)` as PK | Both loaders are idempotent, so a re-run overwrites instead of doubling the corpus and the exact-doc-count gate stays meaningful. | assumed |
| Request timeout | 120 s (`_bulk`), 30 s (settings) | driver defaults | Long enough that a merge pause is recorded as latency rather than as an error. | assumed |
| HTTP sessions | one `requests.Session` per worker thread | one driver `Session`, thread-safe | A shared `requests.Session` caps at its connection-pool size and would silently re-serialise the concurrency. | assumed |
| Payload encoding | on the dispatcher thread, before the op is scheduled | same (`insert_parameters`) | Keeps `service_ms` to transport plus engine, so the client's CPU cost does not inflate the engine's tail. | assumed |
| Progress reporting | instantaneous docs/s, 5,000-doc window | same | A cumulative average erases the merge sawtooth C1 exists to show. | **measured** (`PROGRESS.md`) |
| Retry on a failed write | 4 attempts, 50 ms exponential backoff, whole `_bulk` resent | 4 attempts, 50 ms exponential backoff, only the failed rows resent | One policy for both sides (`ftsbench.load_retry`): the two ingest paths must differ in what the engines do, not in how hard the client tries. Retries are counted and printed in the loader's closing line, so a run that needed them says so. Exhausting the budget fails the load — documents that never landed must not shorten the corpus. | **measured** — a single client-side `ConnectionBusy` cost the 2026-08-19 campaign two ScyllaDB CDC repetitions (1 and 380 documents short); see `REPAIR-PLAN.md` §D2 |

## 2. Engine knobs

> **SUT supersessions (AWS campaign, 2026-09-01; writer buffer added
> 2026-09-07).** The rows below record the laptop pass. On the SUT box
> (`docker/.env.sut`, `SUT-CONFIG.md`) four of them are superseded:
>
> - **vector-store image** is `scylladb/vector-store:1.10.0-43-ge242fa3-arm64`
>   (published-source build, knowack1/vector-store branch
>   `p99-fts-no-commit-threshold` @ `e242fa3b`) — not the 1.10.0 release.
> - **`VECTOR_STORE_FTS_COMMIT_THRESHOLD=0`** disables the compiled-in
>   10,000-uncommitted-docs commit trigger, so commits are purely
>   interval-driven (3 s) at every load level. This removes the
>   threshold-bound regime described in the refresh row below. DEVIATION FROM
>   RELEASE BEHAVIOUR — disclosed on every chart footer.
> - **OpenSearch `refresh_interval: 3s`** is therefore clean parity across the
>   whole load range, and is the SUT default for every build-rate measurement.
> - **`VECTOR_STORE_FTS_WRITER_MEMORY_MB=376`** raises tantivy's per-thread
>   `IndexWriter` buffer off its 15 MB floor. The knob is decimal MB
>   (`megabytes * 1_000_000`); 376 MB × 4 worker threads
>   (`num_worker_threads = perf::num_workers() = tokio workers = 4`) ≈ 1.5 GB,
>   matching OpenSearch's default `indices.memory.index_buffer_size` (10% of
>   the 14 GiB heap = 1.4 GiB, node-total for the single shard). The vector
>   store states worker count and per-thread buffer in a startup log line —
>   verify both there. The 15 MB floor cost the vector-store ~9× more segment
>   merges and ~24% build throughput on the laptop
>   (`results/fts-bottleneck-2026-08-27`); the gain plateaus by 64 MB/thread,
>   so this is a parity correction, not a tuning maximum. Needs the tunables
>   build. Invalidates the laptop-pass build-rate numbers (S11–S15) — those
>   must be re-measured on the SUT before the deck quotes them.

| Knob | OpenSearch | ScyllaDB + vector-store | Why | Evidence |
|---|---|---|---|---|
| Image | `opensearchproject/opensearch:3.8.0` (Lucene 10.5.0) | `scylladb/scylla:2026.3.0-rc2` + `scylladb/vector-store:1.10.0` | Pinned, not `:latest`, so a chart traces to a build. | measured (probed live) |
| Shards / replicas | `number_of_shards: 1`, `number_of_replicas: 0` | `--smp 2`, RF=1, single node | Single-node on both sides; no replication work on either. | assumed |
| Parallel index units | 1 shard = 1 Lucene index | 2 shards, 1 Tantivy index in the vector-store | **Not equivalent.** OpenSearch indexing parallelism is bounded by shard count; ScyllaDB's write parallelism is bounded by `--smp` while its *index* build is a single vector-store process. See §4. | measured |
| In-process memory budget | `-Xms2g -Xmx2g` JVM heap | `--memory 2G` (Scylla) + `VECTOR_STORE_MEMORY_LIMIT=2147483648` (2 GiB) | Chosen so neither side has an obvious advantage at the same order of magnitude. Sums are **not** equal — see §4. | assumed |
| Container memory cap | `mem_limit 4g` | `mem_limit 4g` (Scylla) + `4g` (vector-store) | Blast-radius limit, not tuning: an OOM-kill silently truncates a series, so the cap sits above the in-process budget. | assumed |
| Container CPU cap | `cpus 3` | `cpus 3` (Scylla) + `cpus 2` (vector-store) | **Not equal.** 3 vs 5 total. See §4. | assumed |
| Refresh / visibility | `refresh_interval: 1s`, optional `-1` during load via `--no-refresh-during-load`, restored to `1s`; `3s` in the build-rate sweep | **commit on a 3 s interval OR at 10,000 uncommitted documents, whichever comes first** — compiled in, not a runtime knob | vector-store 1.10.0 `fts_index/tantivy.rs`: `COMMIT_INTERVAL = 3s`, `MAX_UNCOMMITTED_THRESHOLD = 10_000`. A cadence does exist and 1.10.0 is the image the benchmark pins, so `refresh_interval: 3s` is the parity setting at low rates. It is **not** parity during a saturating build: above ~3,300 docs/s the 10,000-document threshold fires before the tick, and ScyllaDB is threshold-bound rather than interval-bound. Visible in both directions — C8's ScyllaDB lag is bimodal (~0.7 s / ~3.3 s) and C1's ScyllaDB document count advances in ~10,000-document quanta. | **measured** (source + C8 + C1) |
| Index durability | on-disk Lucene segments, translog | **in-RAM Tantivy index**, rebuilt by a full base-table scan on restart | Not a knob, a design difference. Enumerated in `COMPARABILITY.md`; drives the C4 story. | measured |
| Index location | on disk by default; **optionally RAM-resident** via `OS_RAM_INDEX=1` (tmpfs over the data path) + `OS_INDEX_CONFIG=index-config-ramindex.json` (`_source: false`) | RAM only, not configurable | `index.store.type` has no in-RAM option on 3.8 (`memory`/`ram` removed in ES 5.0), and Lucene cannot split stored fields from the inverted index across paths — so an index-only OpenSearch on tmpfs is the closest parity, and it stores no documents. Index sizes then land within ~30%: ~138 MB OpenSearch vs ~179 MB Tantivy. See `OPENSEARCH-RAM-INDEX.md`. | **measured** |
| Overprovisioning | none | `--overprovisioned 1` | A laptop is never a dedicated Scylla host; without it Scylla assumes exclusive CPU. Must be **removed** on dedicated benchmark hardware. | assumed |
| Analyzer | custom `m1_parity`: **`pattern` tokenizer `[^\p{IsAlphabetic}\p{N}]+`** + `lowercase` + `_english_` stop words, no stemming | vector-store `standard` analyzer (`SimpleTokenizer` + `LowerCaser` + `StopWordFilter(English)`), positions on | Analyzer parity is a fairness prerequisite. **The laptop pass ran with a `standard` tokenizer, and that was not parity.** Tantivy's `SimpleTokenizer` splits on every non-alphanumeric character; OpenSearch's `standard` follows UAX#29 and keeps `don't`, `u.s`, `3.14`, `foo_bar`, `www.fifa.com` whole, and splits CJK per character. Measured over 3,000 simplewiki documents: **2,110 of 3,000 documents (70.3%) tokenized differently, 31,777 divergent tokens, 525,434 OpenSearch tokens vs 533,388 Tantivy**. The `pattern` tokenizer above reproduces `SimpleTokenizer` exactly — same token text, same positions, same total count (533,388). Stop-word lists were already identical (Tantivy uses Lucene's own 33-word English list, cited in its source). Residual: 12 tokens in 533,388 (0.002%), Turkish `İ` U+0130, from Rust vs Java lowercasing — not fixable on the OpenSearch side. Costs OpenSearch **~11% indexing throughput** (13,407 -> 11,883 docs/s on a 3,000-doc bulk, 5 reps); that is the price of parity, not an engine property, and C1/C4/C6 build-rate numbers must say so. | **measured** (`opensearch/verify_analyzer.sh`, asserted) |
| Stored document | `_source` enabled (Lucene stored fields + translog) | SSTables | Both sides durably store the full text; this is not "search engine vs database + search engine". | measured (`COMPARABILITY.md`) |
| BM25 `k1` / `b` | OpenSearch defaults | Tantivy defaults | **Open parity gate** — not yet compared. Must be closed before any recall- or ranking-sensitive number. | not yet measured |
| Default boolean operator | `query_string` `default_operator` (harness flag) | Tantivy query-parser default | **Open parity gate** — `feature-mapping.md` lists it unresolved. | not yet measured |
| Disk watermarks | `low/high/flood_stage` moved to `97%/98%/99%` on this host (`make os-relax-watermarks`) | n/a | **Deviation from stock, required to run at all here.** Docker's data root sits on an 855 GB filesystem at 93% use — 66 GB free, ample for a <1 GB index, but past OpenSearch's default 90% high watermark. `DiskThresholdMonitor` responds by applying a cluster index-create block, and every `os-index` then fails with a bare 403. The block is applied at *runtime*: it does not appear in `_cluster/settings` and cannot be cleared by writing `null`, only by the monitor's next ~30 s cycle once the thresholds are raised. Any chart produced on this host must say the watermarks were moved. | **measured** (it happened) |
| Host ports | 9200 | 19042 (CQL), 16080 (vector-store) | Moved off 9042/6080, which devcontainer port-forwards hold. A load that dialled 9042 would have written to the wrong cluster silently. | **measured** (it happened) |

## 3. Measured evidence behind the client-concurrency choice

`MAX_DOCS=50000`, OpenSearch 3.8.0, serial `_bulk` (`--concurrency 1`):

| `--batch-size` | docs/s | `write` pool mean active (of 3) | `write` pool queue |
|---|---|---|---|
| 500 | 7,709 | 0.77 | 0.00 |
| 1000 | 8,182 | 0.75 | 0.00 |
| 2000 | 9,798 | 0.50 | 0.00 |

Throughput rises 27% from batch size alone and the engine's write pool never
queues a single request: the client was the bottleneck, so 8,962 docs/s was a
measurement of `opensearch_load`, not of OpenSearch. `scylla_load` has driven
128-way row concurrency from the start and pushed the base table at ~14,900
docs/s. `--concurrency` on the OpenSearch loader exists to remove that
asymmetry; the loader prints a warning and the header records `concurrency: 1`
whenever a run is taken serially.

## 4. Where the two sides are NOT equivalent

Stated here rather than buried, because these are the things an audience will
find if we do not.

1. **Total container CPU: 3 (OpenSearch) vs 5 (Scylla 3 + vector-store 2).**
   The ScyllaDB side is handed 1.67x the CPU cap. It has two processes to run,
   but that is an argument about *why*, not a reason the numbers are comparable.
   Either the OpenSearch container gets the sum, or the ScyllaDB side is capped
   at 3 in total, or every ingest chart states the ratio. Unresolved.
2. **Total in-process memory budget: 2 GiB heap vs 4 GiB (2 GiB Scylla + 2 GiB
   vector-store).** Same shape of problem as CPU. The vector-store's budget is
   also the index's residency limit, and exceeding it makes the vector-store
   stop adding documents while still answering queries — which is why every run
   must assert the index doc count equals the corpus count.
3. **`--concurrency` meant different things — RESOLVED 2026-09-08.** Whole
   `_bulk` requests in flight on one side, rows in flight within a batch on the
   other, so there was no shared unit of offered client pressure. Both loaders
   now dispatch through `ftsbench.load_driver` and the flag counts operations
   in flight on both sides, so an ingest chart may state a single
   "concurrency = N" again.

   The asymmetry was not only cosmetic. `scylla_load` ran its whole dispatch
   loop on one thread, so all row encoding was serialised behind the GIL and it
   capped near **9,800 docs/s** against `opensearch_load`'s **~11,400**.
   OpenSearch's client ceiling sat *above* its engine ceiling and ScyllaDB's sat
   *below* — so one side measured its engine and the other measured its client,
   and the difference was published as an engine result. **Every ingest number
   recorded before this change carries that defect**; the ScyllaDB ones are
   lower bounds.
4. **Per-operation latency is only comparable at equal `--batch-size`.** The
   defaults differ (500 vs 1000), so a C3 taken at defaults would compare the
   p99 of a 500-document `_bulk` against the p99 of a 1000-row batch. **Every
   C3 run must pass the same `--batch-size` to both loaders**, and the value
   goes in the artifact header (it already does).
5. **ScyllaDB's `refresh_interval` equivalent is compiled in, not absent.**
   An earlier revision of this file claimed no equivalent existed; that was
   wrong and it mattered, because it made the 1 s OpenSearch series look like
   the neutral choice when it is in fact *stricter* than what ScyllaDB does.
   vector-store 1.10.0 commits on a 3 s interval or every 10,000 uncommitted
   documents, whichever comes first (`fts_index/tantivy.rs`). Consequences:
   - At low ingest rates the interval binds, so `refresh_interval: 3s` is the
     honest OpenSearch parity setting — this is what the build-rate sweep uses.
   - During a saturating build the *threshold* binds instead, and it is
     document-count-based, so no time-based `refresh_interval` is equivalent to
     it. A build-rate comparison cannot be made refresh-fair by a time knob.
   - The ingest-tuned variant (config D, `refresh_interval=30s`) still has no
     ScyllaDB counterpart and stays labelled as an OpenSearch-only option.
6. **The three ingest paths do different amounts of work.** OpenSearch: one
   bulk write that stores and indexes. ScyllaDB bootstrap: base table already
   loaded (that write is setup, excluded), measured work is the vector-store's
   table scan. ScyllaDB CDC: measured work includes the durable base-table write
   *and* the CDC hop. Per `COMPARABILITY.md`, no ingest or resource win may be
   claimed from these without the asymmetry on the slide.
7. **`--overprovisioned 1` is on for ScyllaDB and has no OpenSearch analogue.**
   It exists because the laptop is shared. It must come off for the real
   campaign, and until it does the ScyllaDB ingest numbers carry a laptop-only
   caveat that the OpenSearch numbers do not.
8. **The load generator shares the host with the engine.** Unavoidable on one
   laptop. Bounded rather than removed: `queue_ms` is recorded per operation, and
   a run whose `queue_ms` p99 is a material fraction of its `latency_ms` p99 is
   generator-bound and must be labelled so (SCHEMAS.md).
9. **Index writer buffer — laptop pass ran it ~24x apart; SUT equalises it.**
   The laptop build-rate charts compared OpenSearch's ~410 MB Lucene index
   buffer against tantivy at its **15 MB per-thread minimum**
   (`memory_budget_per_thread` unset). `results/fts-bottleneck-2026-08-27`
   measured the cost: **~9x more segment merges (44 vs 5) and ~24% build
   throughput** on the ScyllaDB side; OpenSearch did not move. On the SUT
   (`docker/.env.sut`, §2 box) `VECTOR_STORE_FTS_WRITER_MEMORY_MB=376` sets the
   per-thread buffer so the total (× 4 worker threads ≈ 1.5 GB) matches
   OpenSearch's node-total default (10% of the 14 GiB heap ≈ 1.4 GiB). This is a config disparity, not an engine property —
   any laptop-pass build-rate number carries it as a caveat, and it is one of
   the reasons S11–S15 must be re-measured on the SUT.

## 5. What a C3 run must state

Pulled out because C3 is the talk's headline write chart and the caveats are
load-bearing, not a footnote:

- both `--batch-size` values (equal), both `--concurrency` values (with the two
  meanings spelled out), and the `--target-rate` in docs/s;
- whether the run was paced at all — an unpaced run reports
  `latency_ms == service_ms` by construction and is not an SLA latency;
- the `queue_ms` p99 next to the `latency_ms` p99;
- the error count, since failed operations are recorded and excluded from the
  percentiles by design;
- `refresh_interval` on the OpenSearch side, and on the ScyllaDB side which of
  the two commit triggers was actually binding at that ingest rate (the 3 s
  interval below ~3,300 docs/s, the 10,000-document threshold above it).

All of these are in the artifact header or the records already, so the chart
footer can be generated from the run rather than remembered.

## 6. The read side has the same problem, and it is worse

Section 3 is about the *loader*. The query generator has the identical defect
and a larger asymmetry, measured on the laptop over the frozen simplewiki
corpus at `--concurrency 16`:

| Config | `generator_ceiling_qps` |
|---|---|
| opensearch, refresh=1s | 1,090.09 |
| opensearch-refresh30 | 1,106.51 |
| scylla-bootstrap | 4,240.14 |
| scylla-cdc | 4,368.12 |

Same box, same corpus, same generator, same concurrency — a **4x** gap. The
HTTP client is the slow one; the CQL driver is not. This is a property of
`load_gen` on this machine, not of either engine.

Two consequences, both of which must be stated wherever C5 or C7 appears:

- **C7's usable range differs per engine.** The `generator_saturated` rule fires
  above half the ceiling (`SCHEMAS.md`), so the unsaturated sweep stops near
  545 offered qps against OpenSearch and near 2,120 against ScyllaDB. The two
  curves therefore do not cover the same offered-rate range, and the lower
  OpenSearch knee is a client limit until proven otherwise.
- **A knee found below the ceiling is still suspect.** `--calibrate` measures
  what the generator can offer on this machine; it cannot prove the engine was
  the thing that kneed. `make calibrate-os` / `make calibrate-scylla` must be
  re-run on any new host, and both numbers recorded, before a C7 knee is read
  as an engine result.

The fix is hardware, not code: a generator on its own box, which is why
`HARDWARE.md` provisions a third machine and `AWS-RUN-PLAN.md` gates the query
phase on re-calibrating there.

## 7. Read-cube generator ceilings (AWS fleet, measured 2026-09-02)

Closed-loop calibration per concurrency, unmatchable query, single python
process on the harness box (`make calibrate-os` / `calibrate-scylla`):

| Client path | Single-process ceiling | Dispatch-only ceiling | Sharded runner |
|---|---|---|---|
| HTTP → OpenSearch | ~2,300 qps (plateau from c=4) | ~90k qps | 6 processes ≈ 13.8k qps |
| CQL → ScyllaDB | ~6,300 qps (plateau from c=8) | ~92k qps | 6 processes ≈ 38k qps |

The GIL-bound request path is why every cube cell runs through
`ftsbench.cell_bench_mp` (K spawned processes, raw-sample merge); each cell
carries `shard_qps` so the client-bound gate can be re-checked per artifact.

## 8. Write-path build-rate ceilings and `c_sat` (AWS fleet, measured 2026-09-08)

Concurrency ladder `4 8 16 32 64 96 128` at `--batch-size 512`,
1,000,000-doc points, **N=3**, on the frozen enwiki corpus. `c_sat` is the
smallest rung reaching 97% of the best rung observed — stated rather than
eyeballed, because the argmax on a shallow plateau pins later runs to a
concurrency the engine does not need. Evidence:
`results/aws-batch-axis-2026-09-08/matrix/`.

| Arm | `c_sat` | Ceiling (N=3) | Plateau band | G7 headroom |
|---|---|---|---|---|
| `opensearch-ramindex` (refresh 3 s) | **8** | **12,913 docs/s** | 12,576–12,913 from c=8, flat | 2.15x — clears |
| `opensearch-ramindex-refresh30` | **8** | **13,997 docs/s** | 13,267–13,997 from c=8, noisier | **1.98x — LOWER BOUND** |
| `scylla-cdc-buf15` / `-buf376` / `-buf376-commit30` | owed | owed | — | needs its own ladder at `--batch-size 1`, and P0 says that needs **four loader processes** — see below |

Refresh 30 s buys **+8.4%** over refresh 3 s at N=3.

**These supersede the withdrawn 11,063 / 8,992 docs/s at `c_sat`=64.** Those
were taken with the thread-per-request client at concurrencies where it was the
constraint. The rebuilt async client saturates at c=8 and does not improve
above it.

**They also supersede an N=1 ladder run the same evening**, which read
`c_sat`=16 and 13,431 docs/s for the refresh-3 s arm on an apparently
monotonic curve. At N=3 that rise does not reproduce: the curve is flat from
c=8 with a 2.7% band and no trend, so the N=1 rise was noise. This is the
argument for N=3 stated as a measurement rather than as a policy.

**`opensearch-ramindex-refresh30`'s ceiling is a lower bound.** A single loader
process ceilings at 27,699 docs/s at this batch size, so 13,997 sits at 1.98x
— under the 2x rule that separates an engine number from a client-bound one.
Certifying it needs a second loader process, not a longer run.

**`--concurrency` now demonstrably means outstanding requests to the engine.**
The OpenSearch write thread pool held 4.0 of its 4 threads active with a queue
of almost exactly `c - 5` at every rung — 3 at c=8, 11 at c=16, 27 at c=32, 59
at c=64, 91 at c=96, 123 at c=128 — with zero rejections throughout. The pool
depth is recorded per sample in every C1 series from this pass onward
(`write_active`, `write_queue`, `write_rejected`, `write_pool_size`).

| | |
|---|---|
| Engine CPU at `c_sat` | **3.99 of 4 cores** (`OS_CPUS=4`, `OS_CPUSET=4-7`) — CPU-bound, not concurrency-bound |
| Loader CPU at `c_sat` | **0.59–0.61 of one core**, single-threaded (`busiest_thread_cores` equals the process total) |
| Harness box | 0.60–0.64 of 8 cores, 1% CPU pressure |

The loader figure is the one the generator gate reads, and P0 measured the
bound the same evening: **`LOADER_CORE_BOUND_AT` = 0.850** for the OpenSearch
client (0.85 of a fully saturated 1.000-core thread, 48 points) and **0.747**
for the ScyllaDB client (0.85 of a measured 0.879 thread, 12 points). The old
0.70 was too conservative for OpenSearch. At 0.6 of a core the loader sits
about 1.4x under its bound — not the ~13x the box-level number suggests — so
the headroom is in processes, which is what the `N x M` shape exists for.

### Per-process client ceilings (P0, null sink, one process)

| Client | Ceiling | Note |
|---|---|---|
| OpenSearch, any of `--batch-size` 16/64/128/256/512 | **26.5k–27.7k docs/s** (1,658.9 → 54.1 operations/s) | flat in batch size: the client's cost is per-document, so batch moves how many requests it makes, not how fast it can go |
| ScyllaDB at `--batch-size 1` | **8,003 docs/s** | one operation is one document is one prepared statement |

| Processes | OpenSearch | ScyllaDB (batch 1) |
|---|---|---|
| 1 | 27,686 docs/s | 8,003 docs/s |
| 2 | 52,469 (1.90x) | 15,438 (1.93x) |
| 4 | 99,443 (3.59x) | 28,530 (3.56x) |

**`N_max` = 4 is a lower bound** — neither ladder had stopped scaling.

**The ScyllaDB arms need four loader processes at batch 1.** 8,003 docs/s in
one process against a ScyllaDB engine ceiling near 12,228 is 0.66x: the client
would be the constraint. N=2 gives 1.26x and still fails the 2x rule; N=4
gives 2.34x. `ftsbench.mp_load`'s `--workers` now reaches the loaders through
`tools/build_rate_point.sh` (`WORKERS=` on the ladder, a roster column per
row), which is what unblocks R1–R3 — but it is **off by default and not yet
measured on the fleet**: with `WORKERS` unset a point is exactly the
single-process run every archived artifact was taken with. Turning it on is a
change to the client, so it needs its own pass with `verify_generator` applied.
That is the price of the batch-1 decision, and the answer is more processes,
never a larger batch.

### The ramindex arm cannot hold the frozen corpus (measured 2026-09-08)

`OS_RAM_INDEX_SIZE=12 GiB` holds about **4.0 million documents** of the frozen
enwiki corpus — 45% of it. A full-corpus growth run filled the tmpfs at
**4,025,699 documents**, OpenSearch threw `No space left on device`, the shard
failed and the cluster went red mid-run. Roughly **3 GB of tmpfs per million
documents**, merge working space included.

**The budget cannot be raised at parity.** tmpfs pages count against the
container's memory, so with `OS_MEM_LIMIT=28g` and `OS_HEAP=-Xmx14g` the tmpfs
cannot exceed about 14 GiB, while the full corpus needs ~27 GB of index plus
headroom for merges. Reaching it would mean an `OS_MEM_LIMIT` near 55–70g on a
61 GB box — which is both most of the machine and a larger memory budget than
the ScyllaDB side gets, so it would break the equal-budget claim the ramindex
parity choice exists to support.

Consequences, in the order they bite:

- **Any full-corpus OpenSearch measurement must use the disk-store arm**
  (`--opensearch-disk-store-refresh1` / `-refresh30`), whose index lives on
  `/mnt/nvme`. That is a different arm from the one the deck's write-path
  slides use, so its numbers do not pair with a ramindex ceiling.
- **Growth curves on the ramindex arm are bounded by the arm, not by choice.**
  Measured at a 3,000,000-document cap, which leaves ~3 GB of the tmpfs for
  merges.
- The failure is loud rather than silent — ENOSPC, a red cluster and a
  `sample failed: 'merges'` storm in the monitor — but it is loud *after* four
  million documents, so a full-corpus run has to be planned around it rather
  than discovered by it.

### Batch size on the C1 build path

**An OpenSearch quantity only.** `OS_BATCH_SIZE` is 512; the ScyllaDB loader
has no batch flag at all, because one ScyllaDB operation is one document is one
prepared statement. That is what makes `--concurrency` the same quantity on both
engines, and the ceiling on the ScyllaDB arm is found by raising it alone.

**Consequence for C3:** its two arms no longer record the same quantity. One
OpenSearch latency covers a `C3_BATCH`-document `_bulk`; one ScyllaDB latency
covers one INSERT. There is no CQL batch to match, so the chart has to say so
rather than the harness pretending otherwise. C3 is not in the deck and is kept
as a diagnostic.

Measured on `opensearch-ramindex` at `c_sat`=16, N=3
(`results/aws-batch-axis-2026-09-08/axis/`):

| `--batch-size` | docs/s | docs per engine core-second |
|---|---|---|
| 16 | 12,273 | 3,076 |
| 64 | 13,078 | 3,279 |
| 128 | 13,093 | 3,283 |
| 256 | 13,246 | 3,321 |
| 512 | 13,237 | 3,319 |

**Worth 6.8% between 16 and the plateau, and nothing measurable above 64** —
the plateau spans 0.7% against repetition spreads of 1.1–3.8%. The mechanism is
engine-side: the engine held 3.99/4 cores and a 4.0/4-active pool with 11
queued at *every* level, so what the batch size changed was CPU per document,
not how busy the engine was. The loader's own CPU is flat across a 32-fold
change in requests per document, which is the measured form of §4's point that
encode cost is per-document work and does not amortise over batch size.

Doubling the concurrency at every level moved the result by −2.5% to +0.0%, so
512 is not a starved reading of a knob that wanted more parallelism.

`opensearch-ramindex-refresh30` at `c_sat`=8 reads the same shape a little
higher and a lot noisier — 12,584 / 13,431 / 13,822 / 13,639 / **13,993**
docs/s at batch 16/64/128/256/512, spreads 2.9–5.7%, plateau 13,731, batch 16
at −8.4%. **Two of its cells are lower bounds, not ceilings.** A single loader
process ceilings near 27.5k docs/s, so the G7 headroom is 2.08–2.16x on the
`opensearch-ramindex` arm but only **1.98x** at refresh30's batch 128 and
batch 512 — just under the 2x rule. **Its 13,993 docs/s is the highest figure
in the pass and the gate declines to certify it**; promoting those two cells
needs a second loader process, not a longer run.
