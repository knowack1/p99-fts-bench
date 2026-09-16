# search-latency — what a full-text search costs, per concurrency and per query class

Two binaries asking one question of three interfaces, over a crate they share.

| Directory | What it is |
|---|---|
| [`core/`](core) | `search-latency-core`: the query set, the closed loop, the percentiles, the matrix, the CSV, and the refusal to measure a partial index. Every part that does not depend on which interface is underneath. Not a binary, and not a workspace member. |
| [`scylla/`](scylla/README.md) | `scyllasearch`: `BM25()` over CQL, or the vector-store's `/bm25` endpoint with ScyllaDB out of the path. |
| [`opensearch/`](opensearch/README.md) | `ossearch`: `query_string` over `_search`. |
| [`SEARCH-LATENCY-DISK-RUNBOOK.md`](SEARCH-LATENCY-DISK-RUNBOOK.md) | The short campaign: both engines with the index **on disk**, top-k 100, all six classes, one session of ~5 h. Holds the storage tier fixed, so a gap it measures is an engine difference — at the cost of not measuring ScyllaDB the way it ships, which is from RAM. |
| [`SEARCH-LATENCY-AWS-RUNBOOK.md`](SEARCH-LATENCY-AWS-RUNBOOK.md) | The full fleet campaign: six arms — both interfaces × both ScyllaDB index locations, plus OpenSearch on disk and on tmpfs — over the whole enwiki corpus, every concurrency level × every query class × top-k 10/100/1000. Four SUT configurations, four index builds, ~23 h across four sessions. |
| [`HARNESS-AWS-RUNBOOK.md`](HARNESS-AWS-RUNBOOK.md) | The fleet campaign that measures **this harness** rather than an engine: its throughput floor, the service-time constant it adds to every engine number, and whether the closed loop is closed at fleet concurrency. Blocked on `engine-mock` learning to answer a search — its Phase 0 says exactly what that means. |

Both write **the same seventeen-column cell CSV**. Columns 16 and 17 are
`engine` and `interface`, which is how a consumer tells three series apart once
three runs are in one file.

```
bench/data/corpus.jsonl ─┐
                         ├─► the index (built here if it is not already complete)
bench/data/queries.json ─┘        │
                                  ▼
        concurrency × query class ──► one CSV row per cell
```

## The question, and the shape of the answer

One cell is one (concurrency, query class) pair. N workers, closed loop: each
sends the next query of its class the moment the previous one answers. So

- **`p50_ms` / `p90_ms` / `p99_ms` are service times.** Nothing is offered on a
  schedule and there is no queue between the workers and the engine, so
  coordinated omission does not apply — and neither does the correction for it.
- **`queries_per_s` is completed over wall**, which is the throughput that
  concurrency actually achieved rather than one that was asked for.
- Both are only meaningful next to the concurrency they were taken at, which is
  why it is column 1 and not a footnote.

The matrix is walked **concurrency-outermost**, so repeating the ladder —
`--concurrency 8,16,32,8,16,32` — interleaves two traversals of the whole
matrix. Host drift then spreads across the matrix instead of landing on one
curve and tilting it. Repeats are kept: the list is walked exactly as given.

## The index is a precondition, not a measurement

This is the whole difference from [`../build-rate`](../build-rate/README.md).
That tree exists to time the index build, so it may never force an engine's
hand. This one exists to time reads against a finished index, so at start-up it:

1. counts the corpus — one document per line, cut at `--max-docs`;
2. reads the index count from the same probe `build-rate` watches;
3. **skips the build** if the two match exactly;
4. **builds** otherwise — resetting first, always, and filling with
   `build-rate`'s own loader at `--load-concurrency`;
5. asks the engine to publish what it has accepted, then waits for the count;
6. **refuses to measure** if it still does not match.

An unreadable probe is refused rather than guessed at, and an index holding
*more* documents than the corpus is refused rather than topped up: it was not
built from this corpus. `--rebuild-index` forces step 4; `--no-index-build`
turns every build into a refusal, for an index somebody else manages.

Because the loader is `build-rate`'s, "the index was complete before the first
query" is one claim rather than two implementations of it.

## What a chart script reads

Four charts, all the same shape: X is `concurrency`, Y is one of the four
columns below, and a series is `engine` + `interface`.

| Chart | Y column |
|---|---|
| 1 | `p50_ms` |
| 2 | `p90_ms` |
| 3 | `p99_ms` |
| 4 | `queries_per_s` |

Every row also carries what makes it comparable — `query_class`, `limit`,
`fetch_documents`, `distinct_queries` — and what says it might not be a
measurement at all: `errors`, `hits_mean` and `zero_hit_queries`. **A cell whose
every query matched nothing still has a p99**, and it is the cost of finding
nothing; such a run exits non-zero and says so on stderr, so that a script
cannot pick the number up quietly.

Rendering is deliberately not in this tree. The harness writes columns; the
renderer is a separate script written against them.

### The two files a run leaves

```text
points.csv                        one row per cell, the seventeen columns above
latencies/<class>-c<level>-<n>.csv   every latency of that cell, ascending, plus elapsed_s
```

The second is off unless `--latencies-dir` is given, and is worth giving
whenever a run will be repeated: **percentiles do not average**. Three repeats
of a cell give three p99s, and the p99 of the three together can only be
computed from the samples. A consumer that only wants the four charts does not
need it.

Rows are ordered ascending by `latency_ms`, not chronologically — do not infer
drift from row order. Each row's `elapsed_s` is seconds since the cell's own
measured window started; sort a copy of the rows by that column instead to
recover arrival order (this holds across concurrent workers too, since they
all share the same cell start).

Both carry the same `# key=value` preamble — engine build, driver version,
analyzer, the index count the run measured against, and every flag the run was
given.

## What belongs to an interface, and what does not

The seam is one method: take this query text, come back when the answer is
complete, and say how many documents it held.

| | `scyllasearch --interface cql` | `scyllasearch --interface vector-store` | `ossearch` |
|---|---|---|---|
| Request | `SELECT … WHERE BM25(body,'q') > 0 ORDER BY BM25(body,'q') LIMIT n` | `POST /api/v1/indexes/{ks}/{idx}/bm25` | `POST /{index}/_search`, `query_string` |
| Hit count | rows returned | length of a primary-key column | `hits.hits` |
| `--fetch-documents` | projects `title, body` | **refused** — the endpoint cannot return text | `_source: ["title","body"]` |

`cql` minus `vector-store` is ScyllaDB's own read overhead, and that subtraction
is the only reason the second interface exists. It is valid only because neither
arm fetched documents, which is the one mode the BM25 endpoint can serve.

## Why three locks and not one workspace

Each binary keeps its own `Cargo.lock` beside its own `Cargo.toml`, for the
reason [`../build-rate/README.md`](../build-rate/README.md) gives: its
`build.rs` reads that lock to stamp the linked driver version into every CSV
header, and one shared lock would move one engine's recorded version when the
other's driver moved. `core` is a path dependency, and so are `scyllarate` and
`osrate`.

Test, lint and format each crate in its own directory:

```bash
for crate in core scylla opensearch; do
  (cd "$crate" && cargo fmt --check && cargo clippy --all-targets && cargo test)
done
```

The live tests in `scylla/tests/` and `opensearch/tests/` are `#[ignore]`d and
need a running engine with a built index; `cargo test -- --ignored` with one up.
Unlike the sibling tree's live tests there is no sink that can stand in: an
accept-and-discard endpoint stores nothing, and a search against nothing is the
one answer this harness treats as a failure. What it would take for one to
stand in — a *null search*, answering a fixed synthetic shape without matching
anything — is specified in Phase 0 of
[`HARNESS-AWS-RUNBOOK.md`](HARNESS-AWS-RUNBOOK.md).

## What still differs between the halves

Not the columns — those are identical. What sits behind them:

- **`literal` vs `prepared` is a ScyllaDB-only axis.** `--statement literal`
  re-parses on the coordinator per request, which is what Lucene's
  `query_string` parser does on the other side and what this bench's Python read
  arm has always done; it is the default for both reasons. `--statement
  prepared` prepares each distinct query once before the matrix starts, which is
  what an application does. `statement=` in the header says which.
- **The analyzer probe is OpenSearch's alone**, because parity is a property of
  the index config there and of the engine here. `ossearch` runs it whether or
  not it built the index: an index somebody else created with a different
  analyzer would make every latency below it a comparison of tokenizers.
- **`refresh_interval` reaches the read path only through the build.** Once the
  index is complete and settled, it does not move again — which is exactly the
  state these numbers are supposed to describe.
