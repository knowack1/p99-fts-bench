# Harness output schemas (contract)

Every harness output is **JSONL**: one `header` record, then N data records.
This matches the existing `build_monitor` convention and keeps every artifact
streamable and appendable-safe. Consumers must ignore unknown fields, so a
producer may add fields without breaking a plot.

`schema_version` is `1` for every record type below. Bump it in the header only,
never per record.

## Header — required on every file

```json
{
  "record": "header",
  "schema_version": 1,
  "producer": "load_gen",
  "engine": "opensearch",
  "engine_version": "3.8.0",
  "label": "C5 prelim: simplewiki 270k, laptop",
  "cache_state": "cold",
  "corpus": "data/corpus.jsonl",
  "max_docs": 0,
  "started_at": "2026-08-19T18:00:00+00:00",
  "git_commit": "ef62eb7",
  "host": {"hostname": "...", "cpu_count": 22, "total_ram_bytes": 0},
  "env": {"swap_used_bytes": 0, "load_avg_1m": 0.0, "cpu_affinity": [0,1,2]}
}
```

`env` is what makes a laptop number auditable rather than merely small.
`swap_used_bytes` and `load_avg_1m` are read at run start; `cpu_affinity` is
`os.sched_getaffinity(0)` sorted, so a run that was *not* pinned is visible as
the full core list.

Shared helper: `ftsbench.runmeta.header(...)` builds this dict. Producers must
not hand-roll it — a header that differs between producers breaks the results
tree generator.

## `sample` — index build progress (C1, C2) — EXISTS, unchanged

Produced by `build_monitor`. Documented here for completeness only; do not
change its fields.

Key fields: `t_elapsed_s`, `docs_indexed`, `docs_searchable`, `docs_per_s`,
`docs_per_s_cumulative`, `segments_count`, `merges_current`, `merges_total`,
`merges_total_time_ms`, `store_size_bytes`, `index_status`.

C2 (time until searchable) is **derived** from this series, not measured
separately: the first sample where `index_status == "SERVING"` (ScyllaDB) or
where `docs_searchable` reaches the corpus count (OpenSearch).

Because C2 is derived that way, the series must not end before that sample
exists. `docs_indexed` and `docs_searchable` are two different numbers on the
OpenSearch side — a document is written a refresh interval before it is
findable — so `build_monitor` keeps sampling after the last document is
*written* until every one is *searchable*, and records how long it was willing
to wait as `settle_timeout_s` in the header. A series that ends with documents
written but not searchable makes C2 unmeasurable rather than merely imprecise
(the first pass lost 22,554 of 270,269 documents at `refresh_interval=1s`, and
at 30s no document was ever searchable). The monitor warns on stderr when that
happens; the `c1_series_complete` gate is what turns it into a failed
repetition.

ScyllaDB's vector-store status endpoint answers one `count`, reported as both
fields, so the settle phase is a no-op there by construction — it still waits
for `index_status == "SERVING"`.

## `latency_op` — one record per operation (C3, C5, C6)

```json
{
  "record": "latency_op",
  "i": 12345,
  "t_intended_s": 12.340000,
  "t_start_s": 12.342100,
  "t_end_s": 12.349800,
  "latency_ms": 9.8,
  "service_ms": 7.7,
  "queue_ms": 2.1,
  "op": "bulk",
  "n_docs": 500,
  "class": null,
  "query_i": null,
  "hits": null,
  "ok": true,
  "error": null
}
```

- `t_*_s` are seconds from run start (`time.perf_counter()` origin), float.
- **`latency_ms = (t_end_s - t_intended_s) * 1000`** — measured from the moment
  the operation was *scheduled* to be sent. This is the coordinated-omission-safe
  number and the **only one a percentile may be taken from** for C5 and C7.
- `service_ms = (t_end_s - t_start_s) * 1000` — engine-observed service time.
- `queue_ms = latency_ms - service_ms` — how long the generator itself held the
  request. Reporting it is what makes coordinated omission a measured quantity
  instead of a claim. **A run whose `queue_ms` p99 is a material fraction of its
  `latency_ms` p99 is generator-bound and must be labelled so.**
- `op`: `"bulk"` | `"insert"` | `"search"`.
- `n_docs`: documents in this operation (ingest); `null` for searches.
- `class`: query class for searches (`rare_term`, `common_term`, `phrase`,
  `bool_and`, `bool_not`, `bool_mixed`); `null` for ingest.
- Failed operations are recorded with `ok: false` and a message, and are
  **excluded from latency percentiles but counted in the error rate**. Silently
  dropping them would turn a failing engine into a fast one.

## `resource_sample` — resource footprint (C4)

```json
{
  "record": "resource_sample",
  "i": 42,
  "t_elapsed_s": 42.0,
  "container": "fts-bench-vector-store",
  "role": "vector-store",
  "rss_bytes": 0,
  "cache_bytes": 0,
  "cpu_seconds_total": 0.0,
  "cpu_cores_used": 0.0,
  "disk_read_bytes": 0,
  "disk_write_bytes": 0,
  "index_size_bytes": null
}
```

- One record **per container per sample tick**. `role` is
  `"opensearch"` | `"scylladb"` | `"vector-store"`, so C4 can show the ScyllaDB
  side both split and summed. The honest C4 story requires the split: the in-RAM
  Tantivy index is why that bar is bigger, and it lives in the vector-store
  process, not in ScyllaDB.
- `rss_bytes` is the cgroup `anon` figure, not `memory.current` — the latter
  includes page cache and would flatter whichever engine touched less disk.
- `cpu_seconds_total` is a monotonic counter; `cpu_cores_used` is the derived
  rate over the tick, for the C4 CPU bar.
- `index_size_bytes` is on-disk index size where the engine reports one
  (OpenSearch `store.size_in_bytes`), `null` for the in-RAM Tantivy index —
  whose cost shows up in `rss_bytes` instead. Do not synthesise a zero there;
  `null` and the RAM bar together are the truthful representation.

## `sweep_point` — QPS ladder (C7)

```json
{
  "record": "sweep_point",
  "i": 3,
  "offered_qps": 400,
  "achieved_qps": 398.7,
  "duration_s": 60.0,
  "warmup_s": 10.0,
  "count": 23922,
  "errors": 0,
  "p50_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0, "p999_ms": 0.0, "max_ms": 0.0,
  "queue_p99_ms": 0.0,
  "generator_ceiling_qps": 2500.0,
  "generator_saturated": false
}
```

- Percentiles are over `latency_ms`, warmup excluded.
- `generator_ceiling_qps` comes from the generator's self-calibration pass and
  is copied into every point, so a chart can never be read without it.
- **`generator_saturated` is true when `offered_qps > 0.5 *
  generator_ceiling_qps` or `achieved_qps < 0.95 * offered_qps` or `queue_p99_ms
  > 0.25 * p99_ms`.** Points where it is true are plotted differently and are
  not eligible to define the SLA knee. On a laptop this will fire; the chart
  must show where.

## `freshness_probe` — write to searchable (C8)

```json
{
  "record": "freshness_probe",
  "i": 0,
  "marker": "ftsfresh7f3a9c21",
  "t_write_s": 3.201,
  "t_searchable_s": 4.233,
  "lag_s": 1.032,
  "poll_interval_s": 0.05,
  "engine": "opensearch",
  "refresh_interval": "1s",
  "timed_out": false
}
```

- The marker is a random token that cannot occur in the corpus, written into a
  document body; searchability is the moment a search for that token returns it.
- `poll_interval_s` bounds the resolution and must be recorded: a 1 s poll
  cannot resolve a 1 s refresh interval. Use 50 ms.
- `timed_out: true` records a probe that never became searchable. It is a
  finding, not a run to discard.
- N repetitions per configuration; C8 plots the median with the observed range,
  because a single sample of a refresh interval is a coin toss about where in
  the cycle the write landed.

## Naming

`data/<chart>-<config>-<rep>.jsonl`, e.g. `c3-opensearch-2.jsonl`,
`c5-scylla-1.jsonl`. Configs: `opensearch`, `opensearch-refresh30`,
`scylla-bootstrap`, `scylla-cdc`. Plot modules glob `<chart>-<config>-*.jsonl`,
matching the `plot_c1` convention already in use.
