# build-rate — how fast a client fills an index, and how fast the index builds

Two binaries asking one question of two engines, over a crate they share.

| Directory | What it is |
|---|---|
| [`core/`](core) | `build-rate-core`: the sweep, the watch, the report. Every part of the measurement that does not depend on which engine is underneath. Not a binary, and not a workspace member. |
| [`scylla/`](scylla/README.md) | `scyllarate`: prepared CQL INSERTs into `wiki.articles`, index build read from the vector-store's status endpoint. |
| [`opensearch/`](opensearch/README.md) | `osrate`: `_bulk` into `wiki-articles`, index build read from `_stats`. |
| [`charts/`](charts/README.md) | The images a local build-rate run ends in: docs/s against concurrency, docs/s against the **offered** rate, and docs/s against the index already built. Reads the CSVs below; imports the null-sink renderers in `../tools` rather than copying them. |
| [`INDEX-RATE-MATRIX-PLAN.md`](INDEX-RATE-MATRIX-PLAN.md) | The engine campaign for the indexed axis: which arms become lines on "indexed docs/s against concurrency", how each is deployed on the SUT, the shared grid, the timeouts, the gates. Nothing in it has been measured. |
| [`INDEX-RATE-SCYLLA-RUNBOOK.md`](INDEX-RATE-SCYLLA-RUNBOOK.md) | The plan above, cut into something runnable: the ScyllaDB half (R1, R2, R8) end to end, from starting the boxes to stopping them. |
| [`INDEX-RATE-OPENSEARCH-RUNBOOK.md`](INDEX-RATE-OPENSEARCH-RUNBOOK.md) | The same, for the OpenSearch half (R4, `os-disk-refresh3`). Both runbooks must write into one results directory — the campaign's only cross-engine read spans them. |

Both write **the same twenty-two-column point CSV** and, with `--samples-dir`,
the same ten-column per-second series. Column 17 is `engine`, which is how a
consumer tells rows apart once two runs are in one file. Columns 18–22 belong to
the rate ladder below and are blank on a concurrency-ladder row; they were
**appended**, so an `awk` field index written against the older seventeen still
points at the same column.

## Two ladders, and exactly one per run

`--concurrency` and `--target-rate` are alternative axes, and passing both as
lists is refused before anything connects.

| Flag | What moves | What the loader does | `latency_basis` |
|---|---|---|---|
| `--concurrency 4,8,16` | requests in flight | **closed loop** — a worker takes the next item when its reply lands. Unchanged, and still the right instrument for *how fast can it go* | `service` |
| `--target-rate 20000,40000` + `--concurrency 512` | documents per second **offered** | **open loop** — the producer releases document *i* at `origin + i/rate`, never skipping when behind. `--concurrency` becomes an in-flight cap that should never bind | `intended_start` |

The rate axis exists because concurrency is not a shared unit: at
`--batch-size 1024` one osrate request carries 1,024 documents and one
`scyllarate` request carries one, so the same x value is two different offers.
A document per second is the same quantity on both halves. Measured against
`../engine-mock` at one identical offered rate of 50,000 docs/s, `in_flight_peak`
came out at **2** on the osrate side and **402** on the `scyllarate` side — the
asymmetry, now a recorded column instead of a distorted axis.

Three columns make a paced row readable, and they are read together:

- `achieved_offered_ratio` — under 0.95 sets `generator_saturated`.
- `in_flight_peak` — against `--concurrency`. A short rung whose peak sat at the
  cap measured **the harness**, and is void rather than a finding.
- `queue_p99_ms` — the producer's own lateness. Against `p99_ms` it is what
  turns "coordinated-omission safe" into a number a reader can check.

Against the **null sink**, both halves are driven by
[`HARNESS-LOCAL-RUNBOOK.md`](HARNESS-LOCAL-RUNBOOK.md) on a laptop — a short
proving pass over the pipeline — and by
[`HARNESS-AWS-RUNBOOK.md`](HARNESS-AWS-RUNBOOK.md) on the fleet, which is
where the client ceiling is actually measured. Neither produces an engine
number: the sink stores nothing. The **real engines** are driven by
[`../AWS-RUN-PLAN.md`](../AWS-RUN-PLAN.md).

## What belongs to an engine, and what does not

The seam is small and nameable. An engine supplies four things:

| Trait | `scyllarate` | `osrate` |
|---|---|---|
| `Inserter` | bind and `execute_unpaged` a prepared INSERT | encode NDJSON and `POST /_bulk` |
| `WorkItem` | one document | `batch_size` documents |
| `LevelSource` | `DROP KEYSPACE` + recreate, then re-prepare | `DELETE`/`PUT` the index |
| `IndexProbe` | `GET /api/v1/indexes/{ks}/{index}/status` | `GET /{index}/_stats` |

Everything else — the bounded channel and N workers, the per-level ladder, the
settle loop, the two tapes, the CSV schemas, the interrupt and the exit code —
is `core`'s, and was written twice before it was.

## Why three locks and not one workspace

Each binary keeps its own `Cargo.lock` beside its own `Cargo.toml`, because its
`build.rs` reads that lock to stamp the linked driver version into every CSV
header. A workspace would give them one lock, and upgrading one engine's driver
would then move the other engine's recorded version without either run having
changed. `core` is a path dependency, so the binary's lock is what resolves it;
`core/Cargo.lock` governs nothing but `cargo test` run from `core/`.

Test, lint and format each crate in its own directory:

```bash
for crate in core scylla opensearch; do
  (cd "$crate" && cargo fmt --check && cargo clippy --all-targets && cargo test)
done
```

The live tests in `scylla/tests/` and `opensearch/tests/` are `#[ignore]`d and
need a running engine; `cargo test -- --ignored` with one up.

## What still differs between the halves

Not the columns — those are identical. The engine underneath them:

- **A searchable count moves in steps** on the OpenSearch side. `docs.count`
  advances at a refresh, so a build curve there is flat, flat, jump.
- **`index_lag_docs` has a floor there** of `refresh_interval × docs_per_s`,
  however fast the engine. The number to read is the excess.
- **`index_status=refreshed` means the harness asked**, which is a different
  measurement from what the configured policy delivers.
- **`p50_ms`/`p99_ms` are per request**, and a request is not always a
  document. `latency_unit` in the header says which — `insert_request` on one
  side, `bulk_request` on the other. This is the only reason the two subtrees
  stay separate.
