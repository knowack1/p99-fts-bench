# engine-mock

An accept-and-discard OpenSearch endpoint and an accept-and-discard ScyllaDB
endpoint, in one binary. It answers a real client correctly, stores nothing, and
is built so that it cannot be the thing a benchmark ends up measuring.

```bash
cargo build --release --locked
./target/release/engine-mock --mode cql  --port 9042 --vs-port 6080
./target/release/engine-mock --mode http --port 9200
```

## Why it exists

A client's own ceiling cannot be measured against a real engine. At the
~11.7k docs/s where the engine saturates, the engine is what the number
describes — which is how a loader's core-bound thresholds came to be
extrapolations from a client that no longer exists (`BUILD-RATE-MATRIX-PLAN.md`,
Phase 0). So the engine is replaced by something that answers the same wire
protocol and does no work.

`--delay-ms` is the other half of the instrument. A mock with no delay makes the
loader client-bound by construction, which is the positive example a generator
gate has never had; the same mock with a delay puts the constraint back outside
the client, which is the negative one. A gate whose job is to catch a condition
we hope not to meet cannot be tested any other way.

**What it is not.** No storage, no consistency, no schema, no relevance. A run
against this measures the loader and nothing else, and no number taken from one
belongs beside an engine result. Both endpoints say so in their version string:
`2.19.0-null-sink`, `1.10.0-null-sink`, `6.2.0-null-sink`. Those strings are
load-bearing — both harnesses stamp the engine version they read into every CSV
header, and a runbook gate holds that a calibration run's headers contain
`-null-sink`.

## This is a port

It replaces `ftsbench/null_sink.py` and its modules, which served every
connection from one asyncio loop on one thread. That made the instrument's own
CPU a documented gate of every run against it, and on the HTTP half it was
reached: measured on this laptop, the same `osrate --batch-size 1` ladder
against both sinks, the loader given 16 tokio workers and the corpus 60,000
documents a level:

| concurrency | `ftsbench.null_sink` | `engine-mock` | |
|---|---|---|---|
| 16 | 39,363 docs/s, p99 0.77 ms | 70,288 docs/s, p99 0.60 ms | 1.8× |
| 64 | 39,341 docs/s, p99 6.23 ms | 174,370 docs/s, p99 0.87 ms | 4.4× |
| 256 | 43,385 docs/s, p99 8.87 ms | 199,091 docs/s, p99 2.85 ms | 4.6× |

The ratio is not the point. The **shape** is: the Python curve is flat while its
p99 grows 12×, which is a saturated single thread queueing — the ladder was
measuring the sink. The Rust curve still climbs at c=256, which means the number
is the loader's again.

The sink's own CPU says the same thing directly. Same arm, `c=64 --batch-size 1`,
60,000 documents, reading `utime+stime` out of `/proc` across the run:

| | docs/s | CPU per document | CPU used |
|---|---|---|---|
| `ftsbench.null_sink` | 41,135 | 21.3 µs | 0.88 of its one core |
| `engine-mock` | 195,874 | 9.5 µs | 1.84 of 22 cores |

Half the cost per document, and it is spread. The Python sink's ceiling is one
core divided by 21.3 µs — about 47,000 documents per second, which is where the
table above shows it flattening. This one has 22 cores to divide, and at the
rate the loader could actually reach it was using 8% of the machine.

The CQL half was never as badly off, because it has always drained every whole
frame per read, and at high concurrency a read carries many frames. It still
gains, and it gains most where the ladder is quietest:

| concurrency | `ftsbench.null_sink` | `engine-mock` | |
|---|---|---|---|
| 8 | 35,187 docs/s | 43,456 docs/s | 1.23× |
| 64 | 131,609 docs/s | 156,877 docs/s | 1.19× |
| 256 | 257,734 docs/s | 291,964 docs/s | 1.13× |

## What is served

| Mode | Endpoint | Shaped like |
|---|---|---|
| `--mode http` | `--port` (default 9200) | OpenSearch: `_bulk`, `_count`, `_stats`, `_refresh`, `_settings`, index create/delete, `HEAD`, the node thread-pool routes |
| `--mode cql` | `--port` (default 9042) | ScyllaDB: CQL native protocol v4 — handshake, `USE`, DDL, `PREPARE`, `EXECUTE`, `BATCH` |
| either | `--vs-port` (default 6080 in cql mode, off in http mode) | the vector-store index-status API: `/api/v1/indexes/{ks}/{idx}/status`, `/api/v1/info` |

Both halves share **one modelled index**, which is the point of serving them
together: the CQL side accepts the documents and the DDL, and the vector-store
side is where a loader reads back what that did. A loader that gates on the
index cannot be measured against half a mock.

The index models what a loader gates on and nothing else: absent → `BUILDING` →
`SERVING` (`--vs-serving-delay-ms`), and accepted-versus-searchable, which
converge only at a refresh (`--os-refresh-interval-ms`; negative is OpenSearch's
`refresh_interval: -1`, where only an explicit `_refresh` publishes).

## Routes it deliberately refuses

Every unanswered route is 404 **and recorded** under `unexpected_requests` in
the `--stats-out` JSON, because a setup call that stopped arriving would
otherwise change a measurement in silence. Which routes those are is itself a
gate: a local run's reconciliation step asserts that the HTTP side saw exactly
`GET /{index}/_settings` and `GET /{index}/_mapping` — the two header read-backs
`osrate` makes and this mock does not answer — and that the CQL side saw
nothing. `POST /{index}/_analyze` appearing there means `--no-analyzer-check`
was dropped from the osrate command line.

Answering those routes would be a regression, not an improvement.

## Concurrency

`--tokio-workers` defaults to every core the machine reports. Each accepted
connection is a tokio task; the runtime spreads them.

- **One write per read, never one per request.** A read commonly carries several
  whole requests, and answering them one at a time puts a `recvfrom`, a `sendto`
  and a `setsockopt` between the client and its own ceiling — per document, once
  a batch size of 1 makes a request a document. This is quantified: on the
  Python sink, draining per read took 603,782 syscalls per 100,000 documents
  down to 306,759.
- **Nothing shared on the per-document path.** The counters are striped across
  padded cache lines with a lane per connection; the index's accepted count is
  one of those stripes behind one atomic flag; the prepared-statement registry
  is read through a per-connection cache whose first entry answers every
  document of a level. The index's lock is taken by pollers and by DDL, never by
  a document. A shared handle counts as shared: that cache answers out of a
  borrow rather than handing back the `Arc` every connection holds, because a
  refcount touched once per document is the one cache line in this binary
  guaranteed to be contended. Cloning one shared `Arc` from 22 threads on this
  laptop costs 46.4 ns against 0.8 ns for a per-thread one — the refcount alone,
  not a mock measurement, but it is per document either way.
- **Nothing allocated on it either.** A CQL frame body is borrowed from the read
  buffer, a mutation's 13-byte answer is written straight into the connection's
  reusable output buffer, and a bulk reply body is built once per item count and
  then only copied.

### Two things in the runbooks must change before they launch this

Both `build-rate/HARNESS-LOCAL-RUNBOOK.md` and `build-rate/HARNESS-AWS-RUNBOOK.md`
encode the Python sink's cost model, and against a multi-threaded instrument
each of them fails in the direction that hides the failure.

- **The `taskset` pin.** The local launcher starts the sink under
  `taskset -c ${SINK_CPUSET:-0-1}`. Two cores was generous for one asyncio
  thread; here it is a budget below the 1.84 cores the HTTP half draws at
  195,874 docs/s, and it also cuts `--tokio-workers` to 2, because that default
  honours the affinity mask. The pin puts the instrument straight back into the
  position this rewrite exists to escape.
- **Gate C's threshold.** It classifies "0.85 core or more" as `SINK — the level
  is a lower bound`, and the AWS runbook calls it "the one-core-per-sink limit".
  Against a mock that is *meant* to use several cores that flags every level, so
  the gate the instrument exists to make passable can never pass. The artifact
  records `tokio_workers` and `env.cpu_affinity`, so the threshold can become
  `0.85 × tokio_workers` — a fraction of the cores the mock actually had.

Neither is changed here: those runbooks describe runs already taken with the
Python sink, and their numbers belong to it.

## Differences from `ftsbench.null_sink`

Behaviour was ported as it was found, including the parts that read like
mistakes but are load-bearing. These are the deliberate departures:

| | Python | Here | Why |
|---|---|---|---|
| `TCP_NODELAY` | set by asyncio, invisibly | set explicitly at accept | tokio does not set it, and Nagle on the mock's side would hold small replies behind the delayed-ACK timer `TCP_QUICKACK` exists to remove |
| Shutdown | `wait_closed()` waits for live connections, so a single idle socket can stop `--duration` from ever returning and the `--stats-out` file from ever being written | connection tasks are ended with the runtime; the JSON is always written | that file is the run's only independent witness |
| `503` reason phrase | `HTTP/1.1 503 OK` (the status-text map has no 503) | `503 Service Unavailable` | no client reads the phrase; the code is unchanged |
| `_bulk` with a query string | not matched, 404, recorded as a missing route | matched and counted | every route but this one strips the query already |
| CQL frame length | unbounded — a frame claiming 4 GiB is buffered | refused over 256 MiB | in Rust that buffer is an OOM, not a slow read |
| HTTP body length | unbounded | refused over 64 MiB | same |
| `HEAD` of a path that is not an index | always 200, never recorded | 404 and recorded | a probe that moved to `HEAD` would otherwise drop out of the witness entirely |
| Documents offered while no index exists | counted internally, reported nowhere | reported as `index_adds_while_absent` | a loader/mock lifecycle disagreement is exactly the silent failure this instrument exists to expose |
| Order inside `create` | the index is made present, then its count is zeroed — safe, because one asyncio thread cannot interleave the two | the base offset is captured first, then the index is made present | on a runtime with real threads an add between the two steps is counted into the accepted total *and* into the offset that total is measured from, so it is dropped and reported nowhere. Taking the offset first turns that window into an add that finds no index, which `index_adds_while_absent` reports |
| `--port 0` | the announce line and the stats header report `0` | both report the port actually bound | otherwise nothing can learn an ephemeral port |
| `--report-interval 0` | a spin loop printing to stderr as fast as one core allows | refused by the flag parser | the spin loop competes with the request path |
| Malformed request or frame | the connection dies silently | the connection dies and the reason is recorded | same reason every other refusal is recorded |
| `host.cpu_count` | `os.cpu_count()` — the machine's cores | `/proc/cpuinfo` — the machine's cores | `available_parallelism()` would have honoured the affinity mask, making `cpu_count == len(cpu_affinity)` an identity and the "was this run pinned?" reading impossible |
| A `git` call that hangs | bounded at 5 s | bounded at 5 s | it runs on the way out, after the signal handlers have stopped listening, so an unbounded one is a mock that ignores the SIGTERM its stop script just sent |
| `started_at` and the env block | read when the artifact is written, i.e. at the end | captured at process start | the field is a time base for aligning artifacts, and the error was exactly one run long |
| A non-finite or absurd numeric flag | accepted, then a no-op or a wrong value | refused at the flag | `Duration::from_secs_f64` panics on them, and it would panic after the readiness line had already been printed |
| The stderr summary and the JSON | two snapshots taken moments apart, so their rates disagree | one snapshot, taken after the runtime has stopped | the two disagreeing reads as a mock that lost documents |

One window is left open deliberately. `add` reads the presence flag and then
adds, and nothing makes those two steps one; on `create` the offset is captured
first, which bounds every add that will ever see the index present, but the same
reorder does nothing on `drop` — a thread can read the flag as true and be
descheduled arbitrarily long before it adds. Closing that would need a
generation stamped atomically with the increment, or a drain, and both put
shared state back on the one path this mock keeps clear of it. What falls in it
is a document of the generation being dropped: never carried into the new index,
never missing from `docs_accepted`, and named in `index_adds_while_absent` only
if it observed the index already gone. `src/index_tests.rs` pins each of those.

Known limitations kept from the Python, because no client sends them and
guessing would be worse than refusing: `TRUNCATE` and `ALTER` are answered as an
empty result and do not move the index; a `BEGIN BATCH ... APPLY BATCH` sent as
a text query is not counted (a BATCH *frame* is); `system.peers_v2` is answered
from the `system.peers` column set.

## The artifact

`--stats-out` writes one JSON document on exit — SIGTERM is the ordinary way to
ask for it. It is the header every producer in this bench shares (`SCHEMAS.md`)
merged with what the mock accepted. Two keys are read by name:

- `docs_accepted` — reconciled against the sum of the harness CSVs' `docs`
  column. A shortfall is documents the loader believes it sent and the mock
  never saw.
- `unexpected_requests` — the routes above.

## Tests

```bash
cargo test            # unit tests beside each module, plus tests/live_endpoints.rs
cargo clippy --all-targets && cargo fmt --check
```

The unit suite is a port of `bench/tests/test_null_sink.py`,
`test_null_sink_vstore.py` and `test_sink_http_wire.py`; the integration suite
drives both endpoints over real sockets in the sequences `osrate` and
`scyllarate` actually send.
