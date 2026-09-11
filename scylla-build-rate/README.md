# scyllarate — a concurrency sweep for ScyllaDB ingest

Loads a corpus into `wiki.articles` once per concurrency level and reports, for
each level, how fast the client delivered documents, what the p99 insert latency
was, and how fast those documents reached the full-text index. The output is one
CSV that feeds three charts:

- **chart 1** — X `concurrency`, Y `docs_per_s`
- **chart 2** — X `concurrency`, Y `p99_ms`
- **chart 3** — X `concurrency`, Y `index_docs_per_s`

Latency percentiles are computed from successful inserts only, so a point with
a non-zero `errors` count has a p99 that excludes whatever the failures cost —
read the two columns together.

## The submit rate and the build rate are two different numbers

`docs_per_s` is how fast this client handed prepared INSERTs to ScyllaDB. A
completed CQL write says nothing about how many documents reached the index:
rows land in the base table first and the vector-store catches up behind them,
either by bootstrap-scanning the table or by tailing CDC.

`index_docs_per_s` is the other number, read from
`{VS_URL}/api/v1/indexes/{keyspace}/{index}/status` — the same two fields
(`count`, `status`) that `ftsbench.samplers.ScyllaSampler` reads for C1, so a
ceiling measured here and an engine number measured there come from one reading
rather than two.

Watching does not stop when the client does. After the last insert the index is
still catching up, so the watch continues until the count reaches what was
submitted, stops moving for `--vs-idle-timeout`, or exhausts
`--vs-settle-timeout`. Only the first of those is a build rate; the other two
report a **floor**, and say so — `index_settled` is `false` and the summary
table marks the count with a `*`.

## Every level builds from zero documents

`article_id` is the corpus's deterministic uuid5, so a second pass rewrites the
first pass's rows: the table does not grow, the index count does not move, and
the build rate of every rung but the first would be unmeasurable. So **before
every level this drops the keyspace and rebuilds it**, which is what
`tools/build_rate_point.sh` does externally by running one point per
invocation.

> **This is destructive and it is on by default.** `DROP KEYSPACE` takes the
> table and the index with it. Check `--hosts` before you run. `--no-reset`
> keeps the keyspace, at the cost of making levels after the first measure no
> build at all.

The cycle, per level:

1. `DROP KEYSPACE IF EXISTS {keyspace}`
2. wait until the vector-store no longer reports a SERVING index — phrased that
   way rather than "404" so it does not depend on which code the vector-store
   picks for an index it no longer has. Without this gate the next one could
   match the index that was just dropped.
3. `CREATE KEYSPACE`, `CREATE TABLE`, `CREATE CUSTOM INDEX` — the index before
   the load, which is the CDC tail path
4. re-prepare the INSERT, whose previous statement id died with the table
5. wait until the index is SERVING **and** holds 0 documents. `CREATE CUSTOM
   INDEX` returns before the index is queryable, and all three conditions are
   needed: SERVING alone could be the pre-drop index, and an empty count alone
   could be one that is not answering yet.

Both waits fail by name and say what they last saw. A reset that quietly did
not happen produces a complete, plausible, wrong build rate, so the gates hang
and then complain rather than let the load start.

The DDL is built from `--keyspace`, `--table` and `--vs-index` rather than read
from `bench/scylladb/*.cql`, which hardcode `wiki` and `articles`. A test holds
the two to each other, so a schema change on one side alone fails rather than
quietly loading a different table.

## Setup

```bash
cargo build --release
```

Rust rather than Python on purpose: the client's own ceiling has to sit far
enough above the engine that a flat curve is the engine's answer and not the
tester's. Against the repo's accept-and-discard sink this binary submits over
200k docs/s on one laptop; the asyncio Python tester it replaces measured
9,024 docs/s (`ftsbench/load_driver.py`).

Its own crate on purpose, too: `bench/.venv` serves the frozen harness
(`FREEZE.md`, `COMPARABILITY.md`), and upgrading this tester must never move a
dependency underneath a recorded run. `Cargo.lock` is committed and the CSV
header names the exact driver version that produced the numbers.

This tool issues DDL: it creates the keyspace, table and index it needs, and
drops them again before each level. Nothing has to exist first. Pass
`--no-reset` and it issues none, in which case the table must already exist —
apply `bench/scylladb/schema.cql` and `index.cql` first.

## Use

```bash
./target/release/scyllarate \
    --corpus ../data/corpus.jsonl \
    --concurrency 8,8,16,32,64,128 \
    --port 19042 \
    --tokio-workers 8 \
    --vs-url http://localhost:6080 \
    --out sweep.csv
```

The first `8` is a warm-up: levels are measured in the order given and repeats
are allowed, so a throwaway leading entry absorbs the cold page cache. Drop its
row before plotting.

| Flag | Default | Notes |
|---|---|---|
| `--corpus` | required | JSONL, one `{id, uuid, title, text}` per line |
| `--concurrency` | required | comma-separated levels, e.g. `8,16,32` |
| `--max-docs` | 0 | documents per point; 0 loads the whole corpus |
| `--hosts` | `$SCYLLA_HOSTS` or `127.0.0.1` | comma-separated contact points |
| `--port` | `$SCYLLA_PORT` or `9042` | `19042` reaches the laptop compose binding |
| `--keyspace` / `--table` | `wiki` / `articles` | |
| `--consistency` | `LOCAL_ONE` | equivalent to LOCAL_QUORUM at RF=1, but recorded |
| `--request-timeout` | 10.0 | seconds; raise if high levels report timeouts |
| `--tokio-workers` | every core | runtime threads; see "Two different knobs" |
| `--out` | `-` | CSV destination; `-` is stdout |
| `--vs-url` | `$VS_URL` or `http://localhost:6080` | vector-store base URL |
| `--vs-index` | `articles_body_fts` | the index name, on the CQL side and in the endpoint path alike |
| `--vs-interval` | 1.0 | seconds between index-count polls |
| `--vs-settle-timeout` | 120.0 | seconds to keep watching after the last insert |
| `--vs-idle-timeout` | 10.0 | seconds of no index progress that end the wait |
| `--reset-timeout` | 300.0 | seconds each reset gate may wait |
| `--no-reset` | off | keep the keyspace; only the first level then measures a build |
| `--no-index-watch` | off | no vector-store traffic at all; implies `--no-reset` |

Progress goes to stderr once a second, the CSV to `--out`, and a summary table
to stderr at the end. Exit status is 1 if any point had a failed insert, or if
the sweep ended early.

**Each point is written as it finishes.** `--out` is opened and its header
written before the first insert, so an unwritable destination costs a second
rather than a whole ladder, and a sweep that dies at level 5 — a bad corpus
line, a lost node, a Ctrl-C — leaves levels 1-4 on disk with a note on stderr
saying where they are. Only the level that was in flight is lost.

**An unmeasured latency is blank, never `0`.** If every insert at a point
failed there is no latency distribution to report, so `p50_ms`/`p99_ms` are
written as empty CSV cells (`-` in the summary table) rather than `0.000`.
Zero would plot as the fastest point on chart 2. `docs_per_s` still reports
`0.0`, which is a real measurement: nothing was delivered.

The same rule covers the index: under `--no-index-watch` all six index columns
are blank, because a zero build rate is a finding and an unwatched level is not
one.

## The CSV columns

The first seven are unchanged and the six index columns are **appended**, never
inserted: `osrate` promises that its first seven columns are these in this
order (`../opensearch-build-rate/README.md`), and `tools/plot_harness_grid.py`
reads both files by position.

| Column | What it is |
|---|---|
| `concurrency` | requests outstanding at once |
| `docs` / `errors` | inserts that succeeded / failed |
| `wall_s` / `docs_per_s` | how long the submit took, and its rate |
| `p50_ms` / `p99_ms` | per-insert latency, successful inserts only |
| `index_docs` | documents **this level** added to the index |
| `index_docs_per_s` | those documents over the whole build, first insert to settle |
| `index_lag_docs` | how far behind the index was when the client stopped submitting |
| `index_settle_s` | seconds spent waiting after the last insert |
| `index_settled` | `false` means the index never caught up — the rate is a floor |
| `index_status` | what the vector-store last reported, normally `SERVING` |

`index_docs` counts only what the level added, never the index it inherited, so
a `--no-reset` ladder still credits each rung with its own work.

## Two different knobs

`--concurrency` and `--tokio-workers` are independent, and confusing them
misreads the curve.

- **`--concurrency N`** is how many requests are outstanding at once. It is the
  X axis of every chart.
- **`--tokio-workers W`** is how many OS threads the runtime may use to serve
  those N requests. It is a property of the client, recorded in the header, and
  held fixed across a ladder.

One process, one bounded channel, N worker tasks. A producer thread reads the
JSONL and puts bound parameters on the channel; each task takes one, awaits its
prepared `INSERT`, and takes the next. In-flight is therefore exactly N. The
channel holds at most `2N` items so the producer cannot pull 456 MB ahead of the
workers, and the producer runs on a blocking thread so file reads never stall
the runtime.

Raise `--tokio-workers` at a flat point to find out whether the client was the
constraint: if the knee moves, it was. Both numbers land in the CSV header, so a
chart made at 4 workers cannot be silently compared against one made at 16.

## What is ScyllaDB-specific here

- **Shard awareness is on, at the driver's defaults.** The Rust driver opens one
  connection per shard by itself and learns the shard count from the server's
  `SCYLLA_NR_SHARDS` supported option, so there is nothing here to size by hand.
- **Token-aware routing is explicit.** A prepared statement carries its routing
  key, and `DefaultPolicy` with `token_aware(true)` is what turns that into a
  shard-local write.
- **Compression is explicitly off**, so whether a codec happens to be compiled
  in cannot silently shift the measured rate.
- **Write coalescing is left at the driver's default, which is on.** The driver
  batches requests that become ready together into one write syscall, which
  flatters a submit-rate measurement at high concurrency. There is no flag for
  it: every run here is a coalescing run, so the ladders stay comparable, and
  `driver=` in the header is what pins the behaviour.
- **The CSV header records the topology** — engine and driver version, protocol,
  runtime and worker count, `shard_aware`, per-endpoint `shards:N`, live
  connection count, tablets, consistency and timeout. A chart without those
  facts is not interpretable.

## Reading the curve

`docs_per_s` flattening does not by itself mean ScyllaDB saturated.

- Check `shards` and `connections` in the header: too few connections for the
  shard count means the client never reached every shard. The compose file
  publishes only `${SCYLLA_HOST_PORT:-9042}:9042`, so Scylla's dedicated
  shard-aware port (19042 inside the container) is not reachable from the host
  and the driver falls back to source-port guessing. Note the coincidence: the
  host binding is also 19042, but it leads to the ordinary port.
- Re-run the flat point with a higher `--tokio-workers`. If the knee moves, the
  client was the constraint.

`index_docs_per_s` flattening is the other half of the reading, and it can
flatten for a reason `docs_per_s` does not: the client kept up and the index
did not. Check `index_lag_docs` and `index_settled` — a level that submitted
fast, fell far behind and never caught up is an index-build ceiling, not a
client one.

Under `--no-reset` every point overwrites the same rows: the table does not
grow, `index_docs` is 0 for every level after the first, and what you are
measuring is the update path rather than a cold load. That is why the reset is
on by default.

`connections` in the header costs a latency sample per request inside the
driver. `cargo build --release --no-default-features` gives that up — the header
then reads `connections=unknown` and `driver_metrics=off` — in exchange for the
purest submit rate.

## Tests

```bash
cargo test                              # 150 tests, no endpoint needed
cargo test -- --include-ignored         # adds the live-endpoint tests below
cargo clippy --all-targets -- -D warnings
cargo llvm-cov --summary-only -- --include-ignored
```

The channel-and-workers core is covered against a driver-shaped fake that defers
completions, so the in-flight bound is genuinely asserted rather than assumed.

The gates and the settle logic are covered against a vector-store-shaped fake
whose answers a test writes, so "the index stopped short" and "the index caught
up" are distinguished by assertion rather than by hope.

The session, the prepared INSERT, the topology read, the driver-backed inserter
and the whole reset cycle only exist against a real CQL endpoint, so
`tests/live_cql.rs` starts the repo's accept-and-discard sink — both halves of
it — and drives them against it. Those tests are `#[ignore]`d by default
because they shell out to `bench/.venv`:

```bash
cargo test --test live_cql -- --ignored
```

Coverage is 91% of lines with them included; what remains uncovered is
`main.rs`'s process-level wiring (argument parsing to runtime to exit code),
which is what a real run exercises.

For an end-to-end run of the binary against that same sink, start it yourself
(from `bench/`):

```bash
.venv/bin/python3 -m ftsbench.null_sink --mode cql --port 9142 \
    --vs-port 6080 --duration 300
```

`--vs-port` is what makes the pairing work: the sink's vector-store half reports
the documents its CQL half accepted, so the build-rate number has a client
ceiling measured the same way the engine's will be. It models the lifecycle too
— `DROP KEYSPACE` deregisters the index and zeroes its count, `CREATE CUSTOM
INDEX` brings it back — so the reset gates are exercised rather than skipped.
`--vs-serving-delay-ms` holds a new index at `BUILDING` for a while, which is
the only way to see the SERVING gate actually wait.

That sink advertises neither the shard extension nor partition-key indexes, so
such a run reports `shard_aware=false` and exercises no shard routing. That is
correct behaviour, not a fault — what it gives you is the client's own ceiling.
