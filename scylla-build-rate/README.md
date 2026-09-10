# scyllarate — a concurrency sweep for ScyllaDB ingest

Loads a corpus into `wiki.articles` once per concurrency level and reports, for
each level, how fast the client delivered documents and what the p99 insert
latency was. The output is one CSV that feeds two charts:

- **chart 1** — X `concurrency`, Y `docs_per_s`
- **chart 2** — X `concurrency`, Y `p99_ms`

Latency percentiles are computed from successful inserts only, so a point with
a non-zero `errors` count has a p99 that excludes whatever the failures cost —
read the two columns together.

## This measures a submit rate, not an index build rate

A completed CQL write says nothing about how many documents reached the
full-text index. The index build is visible only at
`{VS_URL}/api/v1/indexes/wiki/articles_body_fts/status`, which this tool does
not poll — `ftsbench.build_monitor` is the thing that does. Read every number
here as *how fast this client could hand work to ScyllaDB*.

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

The table must already exist — apply `bench/scylladb/schema.cql` first. This
tool never issues DDL.

## Use

```bash
./target/release/scyllarate \
    --corpus ../data/corpus.jsonl \
    --concurrency 8,8,16,32,64,128 \
    --port 19042 \
    --tokio-workers 8 \
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
| `--no-write-coalescing` | off | one write syscall per request; see below |
| `--out` | `-` | CSV destination; `-` is stdout |

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

## Two different knobs

`--concurrency` and `--tokio-workers` are independent, and confusing them
misreads the curve.

- **`--concurrency N`** is how many requests are outstanding at once. It is the
  X axis of both charts.
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
- **Write coalescing is the driver's default and is recorded.** The driver
  batches requests that become ready together into one write syscall, which
  flatters a submit-rate measurement at high concurrency. `--no-write-coalescing`
  turns it off; the header says which was used either way.
- **The CSV header records the topology** — engine and driver version, protocol,
  runtime and worker count, `shard_aware`, per-endpoint `shards:N`, live
  connection count, tablets, consistency, timeout and write coalescing. A chart
  without those facts is not interpretable.

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
- Re-run it with `--no-write-coalescing`. If the rate drops sharply, the earlier
  number was partly a syscall-batching artifact, not delivered work.

Because `article_id` is the corpus's deterministic uuid5, every point overwrites
the same rows. The table does not grow between points and all levels see the
same state — good for comparing levels, but past the first run on an empty table
you are measuring the update path, not a cold load.

`connections` in the header costs a latency sample per request inside the
driver. `cargo build --release --no-default-features` gives that up — the header
then reads `connections=unknown` and `driver_metrics=off` — in exchange for the
purest submit rate.

## Tests

```bash
cargo test                              # 114 tests, no endpoint needed
cargo test -- --include-ignored         # adds the live-endpoint tests below
cargo clippy --all-targets -- -D warnings
cargo llvm-cov --summary-only -- --include-ignored
```

The channel-and-workers core is covered against a driver-shaped fake that defers
completions, so the in-flight bound is genuinely asserted rather than assumed.

The session, the prepared INSERT, the topology read and the driver-backed
inserter only exist against a real CQL endpoint, so `tests/live_cql.rs` starts
the repo's accept-and-discard sink and drives them against it. Those tests are
`#[ignore]`d by default because they shell out to `bench/.venv`:

```bash
cargo test --test live_cql -- --ignored
```

Coverage is 91% of lines with them included; what remains uncovered is
`main.rs`'s process-level wiring (argument parsing to runtime to exit code),
which is what a real run exercises.

For an end-to-end run of the binary against that same sink, start it yourself
(from `bench/`):

```bash
.venv/bin/python3 -m ftsbench.null_sink --mode cql --port 9142 --duration 300
```

That sink advertises neither the shard extension nor partition-key indexes, so
such a run reports `shard_aware=false` and exercises no shard routing. That is
correct behaviour, not a fault — what it gives you is the client's own ceiling.
