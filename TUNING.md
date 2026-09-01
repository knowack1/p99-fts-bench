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
| Batch size | `--batch-size 500` (default) | `--batch-size 1000` (default) | Historical defaults, kept so the preliminary C1 run stays reproducible. | **measured, and the defaults are wrong for C3** — see §4 |
| Client concurrency | `--concurrency 1` (default) | `--concurrency 128` (default) | The defaults are *not* comparable; see §4. Both are now settable and both are recorded in the artifact header. | **measured** |
| Meaning of "concurrency" | whole `_bulk` requests in flight | rows in flight inside one batch (`execute_concurrent_with_args`) | The two engines have no common unit of client pressure; the flags share a name, not a quantity. | measured |
| Offered rate | `--target-rate` docs/s, `0` = closed loop | `--target-rate` docs/s, `0` = closed loop | C3 needs a *controlled* offered rate. At saturation the recorded tail is queueing delay, not engine behaviour. | assumed (rate to be picked from the C1 maxima) |
| Dispatch schedule | fixed intended-start schedule (`ftsbench.pacer`) | same | Coordinated-omission safety. `queue_ms` is recorded per op so the reader can check it. | measured (unit-tested) |
| Document identity | explicit `_id = page_id` | `article_id = uuid5(page_id)` as PK | Both loaders are idempotent, so a re-run overwrites instead of doubling the corpus and the exact-doc-count gate stays meaningful. | assumed |
| Request timeout | 120 s (`_bulk`), 30 s (settings) | driver defaults | Long enough that a merge pause is recorded as latency rather than as an error. | assumed |
| HTTP sessions | one `requests.Session` per worker thread | one driver `Session`, thread-safe | A shared `requests.Session` caps at its connection-pool size and would silently re-serialise the concurrency. | assumed |
| Payload encoding | on the dispatcher thread, before the op is scheduled | same (`insert_parameters`) | Keeps `service_ms` to transport plus engine, so the client's CPU cost does not inflate the engine's tail. | assumed |
| Progress reporting | instantaneous docs/s, 5,000-doc window | same | A cumulative average erases the merge sawtooth C1 exists to show. | **measured** (`PROGRESS.md`) |
| Retry on a failed write | 4 attempts, 50 ms exponential backoff, whole `_bulk` resent | 4 attempts, 50 ms exponential backoff, only the failed rows resent | One policy for both sides (`ftsbench.load_retry`): the two ingest paths must differ in what the engines do, not in how hard the client tries. Retries are counted and printed in the loader's closing line, so a run that needed them says so. Exhausting the budget fails the load — documents that never landed must not shorten the corpus. | **measured** — a single client-side `ConnectionBusy` cost the 2026-08-19 campaign two ScyllaDB CDC repetitions (1 and 380 documents short); see `REPAIR-PLAN.md` §D2 |

## 2. Engine knobs

> **SUT supersessions (AWS campaign, 2026-09-01).** The rows below record the
> laptop pass. On the SUT box (`docker/.env.sut`, `SUT-CONFIG.md`) three of
> them are superseded:
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
3. **`--concurrency` means different things.** Whole `_bulk` requests in flight
   on one side, rows in flight within a batch on the other. There is no shared
   unit of offered client pressure, so the honest statement on an ingest chart
   is both raw settings, not a single "concurrency = N".
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
