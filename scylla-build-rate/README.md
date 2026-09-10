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
python3 -m venv .venv
.venv/bin/python3 -m pip install -r requirements.txt
```

Its own virtualenv on purpose: `bench/.venv` serves the frozen harness
(`FREEZE.md`, `COMPARABILITY.md`), and upgrading this tester must never move the
driver underneath a recorded run.

The table must already exist — apply `bench/scylladb/schema.cql` first. This
tool never issues DDL.

## Use

```bash
.venv/bin/python3 -m scyllarate \
    --corpus ../data/corpus.jsonl \
    --concurrency 8,8,16,32,64,128 \
    --port 19042 \
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
| `--request-timeout` | 10.0 | raise if high levels report `OperationTimedOut` |
| `--executor-threads` | 2 | driver callback pool; see "Reading the curve" |
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

## How concurrency is realised

One process, one `asyncio.Queue`, N worker tasks. A producer reads the JSONL and
puts bound parameters on the queue; each worker takes one, awaits its prepared
`INSERT`, and takes the next. In-flight is therefore exactly N. The queue holds
at most `2N` items so the producer cannot pull 456 MB ahead of the workers.

Threads were not considered: `ftsbench/load_driver.py` records 9,024 docs/s for
this asyncio shape against 2,594 docs/s for a `ThreadPoolExecutor`.

To scale past one core later, shard the input rather than sharing the queue — an
`asyncio.Queue` is an in-process object and does not cross a fork.
`ftsbench/corpus_shard.py` already splits the corpus by byte range.

## What is ScyllaDB-specific here

- **Shard awareness is on, at the driver's defaults.** `scylla-driver` opens one
  connection per shard and sizes the pool from `SCYLLA_NR_SHARDS`. The
  Cassandra-era `core_connections_per_host` knobs do not exist in the fork, so
  there is nothing to tune and no flag to switch it off.
- **Token-aware routing is explicit.** A prepared statement carries its routing
  key, and `TokenAwarePolicy(DCAwareRoundRobinPolicy())` is what turns that into
  a shard-local write.
- **Compression is explicitly off**, so whether `lz4` happens to be importable
  cannot silently shift the measured rate.
- **The CSV header records the topology** — engine and driver version, protocol,
  reactor, `shard_aware`, per-endpoint `shards:N,connected:M`, tablets,
  consistency, timeout and thread pool. A chart without those facts is not
  interpretable.

## Reading the curve

`docs_per_s` flattening does not by itself mean ScyllaDB saturated.

- Check `shards` in the header: if `connected` is below `shards_count`, the
  client never reached every shard. The compose file publishes only
  `${SCYLLA_HOST_PORT:-9042}:9042`, so Scylla's dedicated shard-aware port
  (19042 inside the container) is not reachable from the host and the driver
  falls back to source-port guessing. Note the coincidence: the host binding is
  also 19042, but it leads to the ordinary port.
- Re-run the flat point with `--executor-threads 8`. That pool defaults to 2 and
  handles driver callbacks; if the knee moves, the client was the constraint.

Because `article_id` is the corpus's deterministic uuid5, every point overwrites
the same rows. The table does not grow between points and all levels see the
same state — good for comparing levels, but past the first run on an empty table
you are measuring the update path, not a cold load.

## Tests

```bash
.venv/bin/python3 -m pytest tests/ -q
```

The queue-and-workers core is covered against a driver-shaped fake that defers
completions, so the in-flight bound is genuinely asserted rather than assumed.
Only `__main__._main`/`_measure` need a live endpoint.

For an end-to-end run without ScyllaDB, use the repo's accept-and-discard sink
(from `bench/`):

```bash
.venv/bin/python3 -m ftsbench.null_sink --mode cql --port 9142 --duration 300
```

That sink advertises neither the shard extension nor partition-key indexes, so
such a run reports `shard_aware=False` and exercises no shard routing. That is
correct behaviour, not a fault — what it gives you is the client's own ceiling.
