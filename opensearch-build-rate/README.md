# osrate — a concurrency sweep for OpenSearch ingest

The OpenSearch half of `scylla-build-rate`. Loads a corpus into
`wiki-articles` once per concurrency level and reports, for each level, how
fast the client delivered documents and what the p99 `_bulk` latency was. The
output is one CSV that feeds two charts:

- **chart 1** — X `concurrency`, Y `docs_per_s`
- **chart 2** — X `concurrency`, Y `p99_ms`

The first seven CSV columns are `scyllarate`'s, in its order, so a chart script
written for one engine reads the other by name or by index.

Latency percentiles are computed from bulks where **every** item landed, so a
point with a non-zero `errors` count has a p99 that excludes whatever the
failures cost — read the columns together.

## Two units, and which one each number is in

`--concurrency` counts **`_bulk` requests** in flight, not documents.
`--batch-size` says how many documents ride in one request. So:

| Number | Unit |
|---|---|
| `concurrency` | in-flight `_bulk` requests |
| `docs`, `errors`, `docs_per_s` | documents |
| `bulks`, `failed_bulks` | `_bulk` requests |
| `p50_ms`, `p99_ms` | **one `_bulk` request**, not one document |

**Documents in flight is `concurrency * batch_size`.** `--concurrency 64
--batch-size 512` offers OpenSearch 32,768 documents at once, not 64. The tool
says the product out loud before each level, and `batch_size` is a CSV column
so a chart cannot mix two of them without showing it. The CSV header also
carries `latency_unit=bulk_request`.

At `--batch-size 1` the two units coincide, which is the shape the ScyllaDB
half measures — useful for asking what bulking is worth.

## This measures a submit rate, not a searchable-index rate

A `_bulk` OpenSearch has acknowledged is in the translog and the in-memory
buffer. It is not visible to search until a refresh, and the segments it lands
in are not merged. Read every number here as *how fast this client could hand
work to OpenSearch* — `ftsbench.build_monitor` and `ftsbench.samplers` are what
watch the index itself.

## Setup

```bash
cargo build --release
```

Rust rather than Python on purpose: the client's own ceiling has to sit far
enough above the engine that a flat curve is the engine's answer and not the
tester's. Against the repo's accept-and-discard sink this binary submits over
440k docs/s on one laptop at `--batch-size 512`.

Its own crate on purpose, too: `bench/.venv` serves the frozen harness
(`FREEZE.md`, `COMPARABILITY.md`), and upgrading this tester must never move a
dependency underneath a recorded run. `Cargo.lock` is committed and the CSV
header names the exact client versions that produced the numbers.

The index does not have to exist: this tool creates it. See "Destructive by
default" below for what that means and how to turn it off.

`cargo build --release --no-default-features` drops rustls for a smaller
binary that reaches `http://` endpoints only; the header says `tls=off`.

## Destructive by default

**Before every concurrency level this deletes the index and creates it again.**
`--no-reset` turns that off; nothing else does, and the run says which it is
doing before it does anything:

```text
index reset ON: DELETING INDEX wiki-articles before every level at http://localhost:9200, recreated from ramindex
```

It exists because `_id` is the page id. Without a reset the second level
rewrites the first level's documents, the index does not grow, and every rung
but the first measures Lucene's *update* path — a delete plus an insert, plus
the merge work of the tombstones — rather than a cold build.
`tools/build_rate_point.sh` solves the same problem externally by running one
point per invocation; this tool runs the whole ladder in one process, so it
does that cycle itself. `scyllarate` makes the same trade on the ScyllaDB side.

**Two gates make it a measurement rather than a hope.** After the delete the
tool polls `HEAD /{index}` until the index is gone, because a create that raced
a settling delete would hand the level the last level's documents. After the
create it polls `GET /{index}/_count` until it answers `0`, because an index
whose primary is not allocated answers 503 rather than 0 — the count is the
readiness check and the emptiness check at once. Each gate fails by name and
says what it last saw; `--reset-timeout` is how long either may wait.

A create refused with 403 is almost always `DiskThresholdMonitor` re-applying
`cluster.blocks.create_index`, so that failure says to run
`make os-relax-watermarks`. This tool does not change cluster settings itself.

### The mapping it creates

`bench/opensearch/index-config-ramindex.json` and `index-config.json` are
embedded in the binary with `include_str!`, so what this creates cannot drift
from what `create_index.sh` PUTs, and a bare run needs no argument.

| `--index-config` | What it is |
|---|---|
| `ramindex` (default) | the ScyllaDB-parity mapping: `m1_parity` analyzer, `_source` disabled so the index carries postings and ids only — Tantivy's schema — and `refresh_interval` 3s |
| `disk` | `index-config.json`: same analyzer, document store on, `refresh_interval` 1s |
| a path | that file, read at startup; a path that cannot be read **fails the run** rather than falling back to a default-configured index |

`--refresh-interval` (or `OS_REFRESH_INTERVAL`) overrides the interval the
config carries, applied at creation rather than by a later `_settings` PUT, so
no document is ever indexed under the other value. The header records both what
was asked for (`refresh_interval_requested`) and what the index came back
saying (`refresh_interval`).

**`source_enabled=false` in the header is not evidence that the segments are in
RAM.** That is the other half of the RAM-parity configuration and it is a
compose knob — `OS_RAM_INDEX=1`, a tmpfs over the data path — which no client
can set or read back. `OPENSEARCH-RAM-INDEX.md` describes both halves.

After the first create, one `_analyze` probe checks that `m1_parity` tokenizes
`The U.S. Army in Washington D.C.` exactly as the vector-store does, positions
included. An analyzer cannot be changed on a live index, so the only useful
moment to fail is before the first document. It is one probe, not a
verification: `bench/opensearch/verify_analyzer.sh` is still the full set of 13,
and what a failure here points at. `--no-analyzer-check` skips it, and so does a
`--index-config` that declares no `m1_parity` analyzer.

**The index is created twice at startup** — once in the preflight and once
before level 1. That is deliberate: the CSV header has to describe an index
built from the config *this* run applied rather than whatever an earlier run
left behind, and every level has to start from an index of the same age.

With `--no-reset` the index must already exist (apply
`bench/opensearch/create_index.sh` first) and no DDL is issued at all.

## Use

```bash
./target/release/osrate \
    --corpus ../data/corpus.jsonl \
    --concurrency 24,24,48,96,192,384 \
    --batch-size 512 \
    --url http://localhost:9200 \
    --tokio-workers 8 \
    --out sweep.csv
```

The first `24` is a warm-up: levels are measured in the order given and repeats
are allowed, so a throwaway leading entry absorbs the cold page cache. Drop its
row before plotting.

| Flag | Default | Notes |
|---|---|---|
| `--corpus` | required | JSONL, one `{id, title, text}` per line |
| `--concurrency` | required | comma-separated levels of in-flight bulks |
| `--batch-size` | 512 | documents per `_bulk`; the campaign's `OS_BATCH` |
| `--max-docs` | 0 | documents per point; 0 loads the whole corpus |
| `--url` | `$OS_URL` or `http://localhost:9200` | the same variable the repo's shell scripts export |
| `--index` | `wiki-articles` | |
| `--request-timeout` | 120.0 | seconds; `opensearch_load.BULK_TIMEOUT_S` |
| `--queue-depth` | 10 | batches buffered per worker; see "Memory" |
| `--tokio-workers` | every core | runtime threads; see "Two different knobs" |
| `--out` | `-` | CSV destination; `-` is stdout |
| `--index-config` | `ramindex` | mapping the index is created from; `disk` or a path |
| `--refresh-interval` | `$OS_REFRESH_INTERVAL` or the config's own | applied at creation |
| `--reset-timeout` | 300.0 | seconds either reset gate may wait |
| `--no-reset` | off | keep the index; see "Destructive by default" |
| `--no-analyzer-check` | off | skip the `_analyze` parity probe |

Progress goes to stderr once a second, the CSV to `--out`, and a summary table
to stderr at the end. Exit status is 1 if any point left a document
undelivered, or if the sweep ended early.

**Each point is written as it finishes.** `--out` is opened and its header
written before the first bulk, so an unwritable destination costs a second
rather than a whole ladder, and a sweep that dies at level 5 — a bad corpus
line, a lost node, a Ctrl-C — leaves levels 1-4 on disk with a note on stderr
saying where they are. Only the level that was in flight is lost.

**An unmeasured latency is blank, never `0`.** If no bulk at a point came back
clean there is no latency distribution to report, so `p50_ms`/`p99_ms` are
written as empty CSV cells (`-` in the summary table) rather than `0.000`.
Zero would plot as the fastest point on chart 2. `docs_per_s` still reports
`0.0`, which is a real measurement: nothing was delivered.

## Two different knobs

`--concurrency` and `--tokio-workers` are independent, and confusing them
misreads the curve.

- **`--concurrency N`** is how many `_bulk` requests are outstanding at once.
  It is the X axis of both charts.
- **`--tokio-workers W`** is how many OS threads the runtime may use to encode
  and serve those N requests. It is a property of the client, recorded in the
  header, and held fixed across a ladder.

One process, one bounded channel, N worker tasks. A producer thread reads the
JSONL and groups parsed documents into batches; each task takes one batch,
encodes it as NDJSON, awaits its `POST /_bulk`, and takes the next. In-flight is
therefore exactly N requests, and the producer runs on a blocking thread so file
reads never stall the runtime.

Raise `--tokio-workers` at a flat point to find out whether the client was the
constraint: if the knee moves, it was. Both numbers land in the CSV header, so a
chart made at 4 workers cannot be silently compared against one made at 16.

### Memory

The channel is bounded in **batches**, so the documents buffered ahead of the
workers are `queue_depth * concurrency * batch_size`.

The default depth is 10, the same number `scyllarate` fixes — but the same
number is not the same buffer. That half loads one document per request, so
depth 10 at `c=384` holds 3,840 documents; depth 10 at `c=384 batch=512` holds
1,966,080. Matching the number here is a 512x larger buffer, not parity.

What that costs depends on the corpus, which is why a laptop run never shows it.
Against the 436 MB simplewiki corpus the ceiling sits above the whole corpus, so
the channel holds at most the corpus. Against enwiki at ~4.5 kB a document the
top of the campaign's ladder buffers **~9 GB resident** on the generator box.

The read-ahead is worth most at the bottom of the ladder, where one worker can
outrun the JSON parser between bulks; at `c=384` the depth is far past what
keeps the workers fed. Lower `--queue-depth` if the generator cannot spare the
resident set — a depth of 2 still buffers 393k documents at
`c=384 batch=512` — and watch the resident set either way.

## Encoding is inside the measured latency

A worker builds its NDJSON before it posts, and the clock starts before the
encode. That is deliberate: the CQL half of this bench serializes its row inside
`execute_unpaged`, so a request's latency there is also the client's cost of
offering it. A bulk timed without its encode would be a request no client could
actually have sent. The cost scales with `--batch-size`, which is one more
reason the batch size is a column rather than a footnote.

## What is OpenSearch-specific here

- **Raw NDJSON, built by hand.** `POST /_bulk` with no query parameters and the
  index named on every action line — the request
  `ftsbench.opensearch_load.send_bulk` makes, down to the URL. `refresh` is
  left off rather than set to `false`: false is already the default, so sending
  it would only put a parameter on the wire the Python loader does not.
- **`_id` is the page id as a string**, the same choice the Python loader makes.
  The corpus line's `uuid` is ScyllaDB's partition key and is not read here.
  Both are deterministic functions of the page id, so on both engines a repeated
  level overwrites rather than growing the store — which is why both halves
  empty the index between levels rather than relying on that.
- **A 2xx is not success.** OpenSearch reports per-item failures inside a 200,
  so every reply's items are read. A batch that came back with any item
  rejected costs exactly the documents it lost, contributes no latency sample,
  and counts in `failed_bulks`. A reply whose item count does not match the
  documents offered fails the point rather than being guessed at.
- **Connection pooling is left at the HTTP client's defaults.** reqwest keeps an
  unbounded idle pool per host, so N bulks in flight get N sockets by
  themselves and `--concurrency` is what the engine is actually being asked at
  once.
- **The proxy is disabled explicitly.** An ambient `HTTP_PROXY` would otherwise
  route every `_bulk` through a third party and silently change the rate.
- **The CSV header records the cluster** — engine version and distribution,
  both client versions, runtime and worker count, index, shards, replicas,
  `refresh_interval`, whether `_source` is on, the body field's analyzer, the
  nodes' `write` thread-pool size, batch size, latency unit, timeout and queue
  depth — and what the reset was told to do: `reset_per_level`, `index_config`,
  `refresh_interval_requested`, `reset_timeout_s`, `analyzer_check`. A chart
  without those facts is not interpretable. The ten CSV *columns* are unchanged.

### One deliberate difference from the Python loader

`serde_json` writes compact JSON where `json.dumps` defaults to `", "` and
`": "` separators, so the same corpus is a few percent fewer bytes on the wire
here. The fields, their order and the NDJSON grammar are identical — a run
against the repo's null sink is counted document-for-document by that sink's own
walk of the alternation, which `tests/live_http.rs` asserts.

## Reading the curve

`docs_per_s` flattening does not by itself mean OpenSearch saturated.

- Check `refresh_interval` and `body_analyzer` in the header. The campaign runs
  OpenSearch at 1s and at 30s because 30s is a real throughput tuning, and
  charts made at the two are not the same measurement. An index whose
  `body_analyzer` is not `m1_parity` is not analyzer-parity with the ScyllaDB
  half at all — see `bench/opensearch/verify_analyzer.sh`.
- Check `source_enabled`. `false` is the ScyllaDB-parity variant
  (`index-config-ramindex.json`), whose index carries no document text.
- Check `write_pool`. A rate that flattened at the write pool's size is a
  thread-pool bound, and `failed_bulks` with 429s in the first failure is
  queue rejection, not saturation.
- Re-run the flat point with a higher `--tokio-workers`. If the knee moves, the
  client was the constraint.
- Re-run it with a different `--batch-size`. If the rate moves a lot, the
  earlier number was as much about request framing as about indexing.

Check `reset_per_level`. With the reset on — the default — every point builds
from zero documents, which is the comparison the chart claims. With
`--no-reset`, `_id` being the page id means every point overwrites the same
documents: the index does not grow between points and all levels see the same
state, but past the first run on an empty index you are measuring the update
path, which in Lucene means a delete plus an insert and more merge work, not a
cold load.

## Tests

```bash
cargo test                              # 207 tests, no endpoint needed
cargo test -- --include-ignored         # adds the live-endpoint tests below
cargo clippy --all-targets -- -D warnings
cargo llvm-cov --summary-only -- --include-ignored
```

The channel-and-workers core is covered against a client-shaped fake that
defers completions, so the in-flight bound is genuinely asserted rather than
assumed, and the fake can reject part of a batch the way OpenSearch does.

The reset is covered against an index-shaped fake endpoint that can be slow in
the two places the gates exist for — a delete that takes several polls to land,
a create whose `_count` answers 503 first — and that can acknowledge a delete
and then do nothing, which is the failure the gates were written for.

The client, the index check, the cluster read, the reset cycle and the
`_bulk`-backed inserter only exist against a real HTTP endpoint, so
`tests/live_http.rs` starts the repo's accept-and-discard sink and drives them
against it. That sink models the index's presence and its document count
(`ftsbench/sink_index.py`), so a reset ladder run against it ends holding one
level's documents rather than the ladder's — which is only true if the deletes
really happened. Those tests are
`#[ignore]`d by default because they shell out to `bench/.venv`:

```bash
cargo test --test live_http -- --ignored
```

That sink answers `/`, `HEAD /{index}`, `PUT`/`DELETE /{index}`, `_bulk`,
`_count` and `/_nodes/thread_pool`, and nothing else — it does not answer
`_analyze`, so the analyzer probe fails against it rather than being skipped,
and such a run reports
`index_shards=unknown`, `replicas=unknown` and `distribution=unknown`, and the
sink notes two unexpected routes on its own stderr. That is correct behaviour,
not a fault: the header must not claim a shape nobody read. What the run gives
you is the client's own ceiling.

Coverage is 90% of lines with them included; what remains uncovered is
`main.rs`'s process-level wiring (argument parsing to runtime to exit code),
which is what a real run exercises.

For an end-to-end run of the binary against that same sink, start it yourself
(from `bench/`):

```bash
.venv/bin/python3 -m ftsbench.null_sink --mode http --port 9299 --duration 300
```
