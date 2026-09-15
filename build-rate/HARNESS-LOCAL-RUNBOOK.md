# Harness-on-laptop runbook — a short run that proves the pipeline works

**Hand this file to Claude Code as the instruction and it runs a short proving
pass: starts both null sinks, builds both harnesses, runs six index builds
across three concurrency levels, stops the sinks, and renders the two charts.**
It is self-contained; every script it needs is inline below. Nothing else in
`bench/` has to be read first. **Budget 4 minutes**, of which the measurement
itself is well under one.

**There is no engine in this run.** Both halves push at
`ftsbench.null_sink` — an accept-and-discard sink that answers correctly,
stores nothing, and models just enough of an index to keep the harness's own
gates answerable. Nothing produced here is an engine number, a comparison, or a
build rate. **No number from this run may be repeated at all** — not even as a
preliminary one.

What it does establish is narrower and worth having on its own:

| # | The claim it proves |
|---|---|
| 1 | Both sinks come up and both binaries reach them — CQL + vector-store status, and HTTP |
| 2 | Reset-per-level really does rebuild an empty index on **both** sides, gates included |
| 3 | `--index-watch` populates the six `index_*` columns on both halves |
| 4 | `--samples-dir` leaves a series with enough readings to draw |
| 5 | Both charts render from `$R` alone, with both sinks down |

Two halves, measured in this order, off one corpus, on one box:

| Part | Harness | Binary | Under test | Extra axis |
|---|---|---|---|---|
| **A** | `build-rate/scylla` | `scyllarate` | `null_sink --mode cql` + its vector-store status port | — |
| **B** | `build-rate/opensearch` | `osrate` | `null_sink --mode http` | — |

Part B runs **one arm, at `--batch-size 1`** — one document per request, bulking
switched off. It is the only OpenSearch level whose x axis is the same shape as
Part A's, and against this instrument the only one whose points last long enough
to read: the sink takes the whole corpus in well under a second at `batch=128`
and above, so those arms could only ever produce start-up and drain with nothing
in between. **Batch size is still a series and never an axis** — the sweep is one
variable away (`BATCHES="1 128 1024"`) and comes back for the longer run at the
end of this file, where a bigger corpus makes its points measurable.

Run Part A first. It establishes the corpus checksum and the host capture. It is
not, on a fast box, the half whose points last longest — that is now `batch=1`;
see **Where the points are long enough**.

---

## What this measures, and what it is not

The subject of an **engine** run is the engines: both binaries push at a real
ScyllaDB + vector-store and a real OpenSearch, with the index reset to empty
before every level, so what comes back is a genuine build rate.

The subject of **this** run is the pipeline that produces that, plus the client
at the top of it. It exercises every step end to end against an instrument that
cannot be a storage engine, and it answers "does this work" rather than "how
fast is anything".

**And none of it is quotable.** Four separate reasons, each sufficient:

- **Nothing is stored.** The sink counts what arrived and drops it. There is no
  segment, no commit, no merge, no fsync, no schema and no relevance. A number
  from here describes a client talking to a counter.
- **One sink process is one Python core**, and it is measured: the CPU gate in
  Phase 4 exists because on the OpenSearch half this box has already driven the
  HTTP sink to **0.85 of a core**, which makes those levels a floor on the
  client rather than a reading of it. The fleet answers this with N sinks on N
  ports; one laptop cannot.
- **This box runs the client and the sink**, over loopback, with whatever else
  is on it. On the fleet they are separate hosts with a measured 0.142 ms
  between them, and the in-flight count covers that RTT.
- **N=1.** A single repetition cannot show disagreement, so the charts' spread
  bars collapse to a single line.

So: **a green pipeline, and no numbers**. All four reasons survive any widening
of the matrix, which is why even a long local run against the sink is a
rehearsal and never reaches the deck.

| File | Its job |
|---|---|
| `HARNESS-AWS-RUNBOOK.md` | the same two binaries against the same null sink **on the fleet** — the client ceiling engine numbers have to stay under |
| `../AWS-RUN-PLAN.md` | the **quotable** engine campaign (C1–C8) on the fleet |
| `../BUILD-RATE-LOOP.md` | the vector-store ingest optimisation journal |
| `../TUNING.md` | where a measured ceiling gets recorded |
| `charts/README.md` | the two renderers this ends in, and why they are not in `tools/` |
| `../ftsbench/null_sink.py` | the instrument: its own docstring is the authority on what it does and does not model |

This runbook supersedes the local mechanics in all of them **for build-rate runs
only**.

### What replaced the engines, and what that changed

This runbook used to bring ScyllaDB and OpenSearch up in Docker. Retargeting it
at the sink removed four things outright, and each removal is load-bearing
rather than a simplification:

| Gone | Why it no longer applies |
|---|---|
| `make scylla-up` / `os-up` / `*-reset` / `os-relax-watermarks` | no containers; the sinks are two Python processes |
| The 12 GiB memory gate | nothing but the loader's own buffer is resident — `queue_depth x concurrency x batch_size` documents, which at this matrix's worst cell is a few dozen, and under 60 MB even with the batch sweep restored |
| "The two engines never run at the same time" | they shared `ENGINE_CPUSET` and did not both fit in RAM. Two idle sinks on two ports cost nothing, so **both are started once, up front**, and the arms stay sequential only because the loader wants the cores |
| `docker/.env` as the provenance record | there are no sizing caps to record. The provenance is now the sink's own `--stats-out` JSON and the `-null-sink` version string both binaries stamp into their CSV headers |

**Do not reintroduce a Docker step to "make it more realistic".** A run with one
engine up is an engine run measured under a sink runbook's gates, and it is the
one result here that would look plausible and be wrong.

---

## The results directory — fix it first

Everything lands in one timestamped directory, named so that it says on its face
that it is a local sink run and when it happened. Create it before starting a
sink, and keep the shell.

```bash
# Run this ONCE, at the very start of the session, and keep the shell.
cd ~/Projects/Scylla/p99/bench
export RUN_ID="local-sink-$(date -u +%Y-%m-%dT%H%MZ)"
export R="$PWD/results/$RUN_ID"
mkdir -p "$R"/{env,scripts,sinks}
mkdir -p "$R"/scylla/{points,samples,logs}
mkdir -p "$R"/opensearch/{points,samples,logs}
printf '%s\n' "$RUN_ID" > "$R/RUN_ID"
ln -sfn "$RUN_ID" results/local-sink-latest
echo "results -> $R"
```

giving, for example:

```
bench/results/local-sink-2026-09-14T1204Z/
├── RUN_ID
├── env/            host.txt  images.txt  binary-provenance.txt  corpus.txt
├── scripts/        the sink launcher and the two arm scripts, as they were run
├── sinks/          sinks.pids  sinks-cpu.tsv  sink-cql-9042.{log,json}  sink-http-9200.{log,json}
├── scylla/
│   ├── points/     scylla-rep1.csv
│   ├── samples/    scylla-rep1/c4-1.csv c8-1.csv c16-1.csv
│   └── logs/       scylla-rep1.stderr.tsv
├── opensearch/
│   ├── points/     os-b1-rep1.csv
│   ├── samples/    os-b1-rep1/c4-1.csv c8-1.csv c16-1.csv
│   └── logs/
├── build-rate-vs-concurrency.png / .csv
├── build-rate-vs-index-size.png  / .csv
└── README.md
bench/results/local-sink-latest -> local-sink-2026-09-14T1204Z
```

UTC to the minute, always, so two runs on one afternoon stay distinct and sort
in order. If the shell dies: `export R="$(readlink -f results/local-sink-latest)"`.

---

# Part A — ScyllaDB harness (`scyllarate`) against the CQL sink

## Phase 0 — the matrix, and the ports gate

### The matrix

| | |
|---|---|
| Concurrency ladder | `4,8,16` — three levels, **no throwaway warm-up row**; see below |
| OpenSearch batch sizes | `1` only — one document per request, bulking off |
| Repetitions | **N=1** |
| Corpus | `data/corpus.jsonl`, frozen simplewiki, 270,269 documents — **all of it** |
| `--max-docs` | `0` (the whole corpus) on **every** arm, both halves |
| Index builds | 3 levels x 1 rep x 2 configurations = **6 builds**, every one from an empty modelled index |
| Estimated wall | Part A ~10–25 s, Part B ~15 s, **4 min** including builds and both renders |

One batch level rather than three, because the larger ones prove nothing here:
against the sink they are over before the index watcher has taken a handful of
readings, so every point they produce is start-up and drain. `batch=1` is the
level that carries the mechanism — per-request framing cost with bulking off —
and it is the one that lasts. N=1, because a repetition count proves nothing
about whether the pipeline runs.

**One budget, not two.** Against a real OpenSearch, `osrate --batch-size 1` was
slow enough to need its own smaller `--max-docs`. Against the sink it is not:
the whole corpus at `batch=1` takes a handful of seconds, so every arm gets
`--max-docs 0` and every series therefore covers the same x range on the growth
chart. That is a simplification the sink bought, and it is worth keeping.

### The shared concurrency grid — do not vary it per arm

**Every series in both parts is measured on the same x values:**

```
4   8   16
```

The deliverable chart puts concurrency on x and every configuration on it as a
series. Series that do not share x values cannot be drawn on one axis, so a
ladder tailored per arm silently destroys the chart. If a level has to be added,
add it to **every** arm. Powers of two, because x is drawn on a log2 axis.

### The ladder carries no warm-up row, and the charts are told so

`4,8,16` is three levels and three x values. It carries no repeated first rung —
and neither do the AWS runbook's ladders any more, which pass `--keep-warmup`
for exactly the reason this one does — and both renderers drop the first data
row of every CSV by default, which on this ladder would delete `c=4` outright rather than delete a
throwaway, leaving a two-point chart.

So the chart command passes **`--keep-warmup`**. Putting a throwaway back
(`LADDER=4,4,8,16`, and drop `--keep-warmup`) is the alternative when the
numbers start to matter.

What must not happen is the ladder keeping three rungs while the chart keeps its
default: that silently yields a two-point x axis and a `c=4` that was measured
and then discarded.

### Where the points are long enough, and where they cannot be

Against the sink, **`osrate --batch-size 1` clears three seconds on every rung
with the whole corpus, and Part A clears it only on the slower rungs.** Dropping
`batch=128` and `batch=1024` removes the six levels Gate B used to name every
time — the sink took the entire 270,269-document corpus in well under a second
at those sizes — but it does not empty Gate B, because Part A's upper rungs run
into the same wall from the other side: measured on a 22-core box, `scyllarate`
reached 160,565 docs/s at `c=16` and put the whole corpus away in **1.7 s**, with
`c=8` at 2.5 s. On a slower box all three rungs clear three seconds.

**That is the corpus being too small for this instrument, not the run failing.**
Those levels are still measured — the chart's point count proves it — they are
just too short to read as rates, so name them and never quote them as rates. The
only fix is a bigger corpus. See **Then the longer run**.

### The ports gate — run this before starting a sink

```bash
ss -ltn | grep -E ':(9042|9200|6080) ' && echo "SOMETHING IS ALREADY LISTENING" || echo "ports free"
docker ps --format '{{.Names}}' | grep -Ei 'scylla|opensearch|vector' && echo "AN ENGINE IS UP -- STOP IT"
```

**Refuse to start if either line fires.** A leftover engine container on 9042 or
9200 would answer the harness perfectly well, and the run would then be a
one-engine measurement wearing this runbook's gates and this runbook's
`NOT QUOTABLE` banner. The launcher below refuses on a busy port for the same
reason, but check first: the failure is much cheaper to read here.

The memory gate this runbook used to open with is gone. Nothing here is
resident but the loader's own read-ahead buffer, which is
`queue_depth x concurrency x batch_size` documents — at this matrix's worst cell
(`c=16 batch=1`, depth 2) thirty-two documents, and even with the sweep restored
(`c=16 batch=1024`) about 33k documents, under 60 MB at this corpus's ~1.7 kB a
line.

---

## Phase 1 — start both sinks

Both of them, once, at the start. Write the launcher into `$R/scripts/` so the
run records exactly what was started:

```bash
cat > "$R/scripts/start-sinks.sh" << 'EOF'
#!/bin/bash
# Both null sinks, on their default ports, pinned off the loader's cores.
#
# The pid recorded is the PYTHON's, not setsid's wrapper: the wrapper exits
# immediately and a CPU gate reading its /proc would report a sink that used no
# CPU at all -- which is indistinguishable from a sink that was never the
# constraint, and is the one way this instrument lies quietly.
#
# --os-refresh-interval-ms 0 publishes each accepted document to _count and
# _stats at once. See "The modelled refresh" in Part B before changing it.
set -euo pipefail
cd ~/Projects/Scylla/p99/bench
LOG_DIR=${LOG_DIR:?set LOG_DIR to "$R/sinks"}
SINK_CPUSET=${SINK_CPUSET:-0-1}
OS_REFRESH_MS=${OS_REFRESH_MS:-0}
PY=${PY:-.venv/bin/python3}

refuse_if_busy() {
    ss -ltn | grep -q ":$1 " && { echo "port $1 is already listening -- stop it first"; exit 1; }
    return 0
}
start() {
    local mode=$1 port=$2; shift 2
    refuse_if_busy "$port"
    setsid taskset -c "$SINK_CPUSET" "$PY" -m ftsbench.null_sink \
        --mode "$mode" --host 127.0.0.1 --port "$port" "$@" \
        --label "local-$mode-$port" --report-interval 30 \
        --stats-out "$LOG_DIR/sink-$mode-$port.json" \
        < /dev/null > "$LOG_DIR/sink-$mode-$port.log" 2>&1 &
    local wrapper=$!
    sleep 1
    echo "$mode $port $(pgrep -P "$wrapper" || echo "$wrapper")" >> "$LOG_DIR/sinks.pids"
}

mkdir -p "$LOG_DIR"
# Every port is checked BEFORE the pid file is touched. A refusal that had
# already truncated it would leave a running pair of sinks with no recorded
# pids -- which is exactly the state the CPU gate and the stop script cannot
# recover from.
for port in 6080 9042 9200; do refuse_if_busy "$port"; done
: > "$LOG_DIR/sinks.pids"
start cql  9042 --vs-port 6080 --vs-keyspace wiki --vs-index articles_body_fts
start http 9200 --os-refresh-interval-ms "$OS_REFRESH_MS"
cat "$LOG_DIR/sinks.pids"
EOF
chmod +x "$R/scripts/start-sinks.sh"
```

```bash
LOG_DIR="$R/sinks" "$R/scripts/start-sinks.sh"
```

**Write the script in one command and run it in the next.** Every script in this
runbook that matches processes by pattern will kill the shell that holds `$R` if
it is created and run in the same command — the creating command line contains
the pattern, so `pgrep -f`/`pkill -f` matches it. This has happened three times
in practice.

Then confirm both sinks answer what their half will actually ask:

```bash
curl -s http://127.0.0.1:6080/api/v1/indexes/wiki/articles_body_fts/status; echo
curl -s http://127.0.0.1:9200/; echo
```

expecting `{"count": 0, "status": "SERVING"}` and a `null-sink` version
document. **The keyspace and index in that URL are not decoration.** The sink
answers a count for exactly one `{keyspace}/{index}` and 404s every other,
precisely so a harness pointed at the wrong one cannot sail through its own
SERVING gate and report a complete, plausible, wrong build rate. `wiki` and
`articles_body_fts` are `scyllarate`'s defaults; the launcher passes them
explicitly so the pairing is visible rather than implied.

Start the per-sink CPU sampler. Without it the Phase 4 gate has nothing to read,
and a sink-bound level renders as a client reading:

```bash
cat > "$R/scripts/sample-sinks-cpu.sh" << 'EOF'
#!/bin/bash
# 1 Hz CPU per sink: epoch, port, utime+stime ticks. APPENDS, never truncates.
LOG_DIR=${LOG_DIR:?}
OUT="$LOG_DIR/sinks-cpu.tsv"
[ -s "$OUT" ] || printf "epoch\tport\tticks\n" > "$OUT"
while true; do
    now=$(date +%s)
    while read -r _ port pid; do
        [ -d "/proc/$pid" ] || continue
        printf "%s\t%s\t%s\n" "$now" "$port" "$(awk '{print $14+$15}' /proc/$pid/stat)" >> "$OUT"
    done < "$LOG_DIR/sinks.pids"
    sleep 1
done
EOF
chmod +x "$R/scripts/sample-sinks-cpu.sh"
```

```bash
LOG_DIR="$R/sinks" setsid "$R/scripts/sample-sinks-cpu.sh" </dev/null >/dev/null 2>&1 &
sleep 3 && tail -4 "$R/sinks/sinks-cpu.tsv"
```

Capture the environment, once:

```bash
{ echo "host: $(uname -srm)"; echo "cores: $(nproc)"; free -h; } > "$R/env/host.txt"
{ echo "sink: ftsbench.null_sink"; echo "python: $(.venv/bin/python3 -V)";
  echo "ftsbench commit: $(git rev-parse --short HEAD)";
  cat "$R/sinks/sinks.pids"; } > "$R/env/images.txt"
{ echo "corpus: $PWD/data/corpus.jsonl";
  echo "lines: $(wc -l < data/corpus.jsonl)";
  echo "bytes: $(stat -c%s data/corpus.jsonl)";
  echo "sha256: $(sha256sum data/corpus.jsonl | cut -d' ' -f1)"; } > "$R/env/corpus.txt"
```

## Phase 2 — build the harness, then freeze it

```bash
cd build-rate/scylla && cargo build --release --locked && cd ../..
```

```bash
{ echo "scyllarate: $(git rev-parse HEAD)";
  echo "dirty: $(git status --porcelain build-rate | wc -l) files";
  ./build-rate/scylla/target/release/scyllarate --help | head -1; } \
  >> "$R/env/binary-provenance.txt"
```

**Do not rebuild once an arm has run.** A rebuild mid-campaign makes the arms
incomparable, and the driver version stamped into each CSV header stops being
the version that produced it.

## Phase 3 — run the ladder

```bash
cat > "$R/scripts/run-scylla-arm.sh" << 'EOF'
#!/bin/bash
# One arm: the same concurrency ladder, N times, against the local CQL sink.
# Reset is ON by default and that is the point -- every level DROPs the keyspace,
# waits for the vector-store endpoint to forget the index, recreates both, and
# gates on SERVING at 0 documents. The sink moves its modelled index in step
# with the DDL arriving on the CQL side, so all of that is answerable here and
# every level is a build from empty.
#
# stderr is timestamped per line so a level's wall-clock window can be cut out
# of the sink CPU samples.
set -euo pipefail
BIN=${BIN:-./build-rate/scylla/target/release/scyllarate}
CORPUS=${CORPUS:-./data/corpus.jsonl}
LADDER=${LADDER:-4,8,16}
REPS=${REPS:-1}
MAX_DOCS=${MAX_DOCS:-0}
GEN_CPUSET=${GEN_CPUSET:-12-19}
stamp() { while IFS= read -r line; do printf '%s\t%s\n' "$(date -u +%H:%M:%S)" "$line"; done; }

for rep in $(seq 1 "$REPS"); do
  echo "=== scylla rep $rep/$REPS  ladder=$LADDER ==="
  taskset -c "$GEN_CPUSET" "$BIN" \
      --corpus "$CORPUS" --concurrency "$LADDER" --max-docs "$MAX_DOCS" \
      --hosts 127.0.0.1 --port 9042 \
      --vs-url http://127.0.0.1:6080 --vs-index articles_body_fts \
      --vs-interval 0.05 --vs-settle-timeout 600 --vs-idle-timeout 30 \
      --reset-timeout 600 --tokio-workers 8 \
      --out "$R/scylla/points/scylla-rep$rep.csv" \
      --samples-dir "$R/scylla/samples/scylla-rep$rep" \
      2>&1 | stamp > "$R/scylla/logs/scylla-rep$rep.stderr.tsv"
done
EOF
chmod +x "$R/scripts/run-scylla-arm.sh"
```

```bash
"$R/scripts/run-scylla-arm.sh"
```

Four flags there are not defaults and each has a reason:

- **`--vs-interval 0.05`.** The index-size chart needs **more than three
  readings per build** or it drops the build by name. The sink publishes as fast
  as it accepts, so a build here is short and a one-second poll would leave two
  readings and no line. Twenty hertz is what keeps every level on the chart, and
  it costs the sink nothing beside the load it is already taking.
- **`--vs-idle-timeout 30`.** Ten seconds of no index progress ends the settle
  wait. Generous here because the wait is what a real vector-store needs, and a
  timeout tuned to an instrument that never stalls has not been rehearsed.
- **`--vs-settle-timeout 600`.** Same reasoning: the sink settles instantly, the
  real thing does not, and the timeout the real run needs is the one to exercise.
- **`--tokio-workers 8`.** The eight cores `GEN_CPUSET` actually grants. Left at
  every core the runtime spawns 22 threads for 8 cores' worth of CPU, on a box
  whose other cores are running the sink.

The whole arm is well under a minute — 8 s on a 22-core box, ~25 s on a slower
one. Run it in the foreground.

## Phase 4 — collect and verify

Everything already writes into `$R`, so there is nothing to copy.

**Gate A — the pipeline works. Every line must print nothing but the row
counts, and a row count that is not 3 is a failure:**

```bash
# every ladder CSV has one row per ladder entry (3), plus the header
for f in "$R"/scylla/points/*.csv; do
  n=$(grep -vc '^#' "$f"); echo "$f rows=$((n-1))"; done

# zero failed inserts anywhere (col 3 is errors)
awk -F, '!/^#/ && NR>1 && $3+0>0 {print FILENAME": errors="$3}' "$R"/scylla/points/*.csv

# the index was watched on every row -- col 12 is index_docs_per_s, and a blank
# there is half of chart 1 missing for this arm
awk -F, '!/^#/ && $1!="concurrency" && $12=="" {print FILENAME" line "FNR": index unwatched"}' \
    "$R"/scylla/points/*.csv

# every level left a series with more than 3 readings, or chart 2 drops it
for d in "$R"/scylla/samples/*/; do for f in "$d"c*.csv; do
  n=$(grep -vc '^#' "$f"); [ "$((n-1))" -le 3 ] && echo "$f only $((n-1)) readings"; done; done

# the settle finished rather than timing out (col 15 is index_settled)
awk -F, '!/^#/ && $1!="concurrency" && $15!="true" {print FILENAME": c="$1" NOT settled -- rate is a floor"}' \
    "$R"/scylla/points/*.csv
```

**Gate A also has to hold that the CSV says `null-sink`.** Both binaries stamp
the version the endpoint reported into the header, and that is the cheapest
proof no engine was involved:

```bash
grep -h -E '^# (scylla_version|vector_store)=' "$R"/scylla/points/*.csv
```

Both must contain `-null-sink`. Anything else means something real was
listening on 9042 or 6080 and every gate below is measuring it.

**Gate C — the sink was not the constraint.** This is the gate the sink exists
to make possible, and it is three-state, never pass/fail:

```bash
awk -F'\t' 'NR>1 {d=$3-prev[$2]; if (seen[$2] && d>=0 && d>max[$2]) max[$2]=d; prev[$2]=$3; seen[$2]=1}
     END {for (p in max) printf "port %s peak %.2f cores in a 1 s window\n", p, max[p]/100}' \
    "$R/sinks/sinks-cpu.tsv"
```

| | meaning |
|---|---|
| under 0.85 core | `ok` — the level is a reading of the client |
| 0.85 core or more | `SINK` — the level is a **lower bound**; say so and never average it in |
| no rows for that port | `?` — **not a pass.** The sampler died or read the wrong pid |

An unmeasured gate must never render as a passed gate, the same way an
unmeasured latency is a blank cell and never `0`. On this box the CQL sink has
been seen at **0.34** of a core and the HTTP sink at **0.85** — so expect Part A
to read `ok` and expect Part B's large-batch levels to sit on the line.

**Gate B — points too short to be a measurement. On a slow enough box this half
is empty; on a fast one expect the top one or two rungs:**

```bash
# col 4 is wall_s. On a 22-core box only c=4 clears 3 s; c=8 and c=16 do not.
awk -F, '!/^#/ && $1!="concurrency" && $4+0<3 {print FILENAME": c="$1" wall="$4"s -- not a measurement"}' \
    "$R"/scylla/points/*.csv
```

The distinction is the whole reason there are separate gates. Gate A says the
machinery ran, Gate C says whether the instrument or the client was the
constraint, and Gate B says whether the point lasted long enough to mean
anything. A proving run is never allowed to fail A.

An `index_settled=false` row is not a failure to discard — it is a build rate
that must be reported as `≥`. Say which rows they were.

---

# Part B — OpenSearch harness (`osrate`) against the HTTP sink, at `batch=1`

## B0 — what is different about this half

Same box, same session, same corpus, same ladder, same gates, **and the sink is
already up**. Only the deltas are written out here; anything not mentioned is
unchanged from Part A.

- **`--index-watch` is OFF by default and must be passed.** Without it all six
  `index_*` columns are blank, and this half contributes no dashed line to chart
  1 and nothing at all to chart 2. This is the single easiest way to lose half
  the run.
- **`--no-analyzer-check` is required here, and only here.** A reset run probes
  `POST /{index}/_analyze` once before the first document, to prove the index
  analyzes text the way the vector-store does. The sink does not answer that
  route and the run would fail on it. Dropping the probe costs nothing against
  an instrument with no analyzer — but it means **this run proves nothing about
  analyzer parity**, and the engine run must not inherit the flag.
- **Reset stays ON.** `DELETE`, `PUT` from the embedded `ramindex` mapping, and
  the gate on `_count = 0` are all routes the sink answers, so per-level reset is
  exercised here exactly as it is in Part A. Only the analyzer probe is dropped.
- **Several header fields will read `unknown` or `unset`, and that is correct.**
  `osrate` reads index settings, mappings and the node thread pool for its
  header; the sink answers `HEAD`, `GET /`, the lifecycle routes, `_bulk`,
  `_refresh`, `_count`, `_stats` and the thread-pool endpoints, but not
  `GET /{index}/_settings` or `_mapping`. So expect
  `index_shards=unknown`, `replicas=unknown`,
  `refresh_interval=unset(default 1s...)`, `source_enabled=true` and
  `body_analyzer=unset(default standard)`. **The last two are the trap**: they
  are the read-back failing, not the `ramindex` mapping being ignored —
  `index_config=ramindex` on the line below is what actually went out. Do not
  "fix" any of this by pointing the run at a real OpenSearch.
- **`p50_ms`/`p99_ms` are per `_bulk` request, not per document.** `latency_unit`
  in the header says which. This is the only reason the two subtrees stay
  separate.

### The modelled refresh

A real OpenSearch makes documents searchable in steps, at the refresh interval,
so a build curve there is flat, flat, jump. The sink models that with
`--os-refresh-interval-ms`, and **the launcher above sets it to `0`** — publish
immediately, the way the vector-store half behaves.

That is deliberate, and it is a trade this run makes on purpose: a `batch=1`
build against the sink is over in about five seconds, so any refresh coarse
enough to be realistic produces a handful of risers across the whole build, and
the growth chart becomes a spike train whose height is the ratio between the
refresh and the poll rather than a rate. At `0` the OpenSearch series has a
shape that can be read.

**So this run does not rehearse the refresh-step path.** To exercise it,
restart the HTTP sink with `OS_REFRESH_MS=250` and expect the growth chart to go
to steps — it is a supported mode of the instrument, not a broken run. The real
shape is what the engine run measures.

## B1 — build the binary

Without TLS — nothing here speaks it, and a run must not depend on the box's
OpenSSL:

```bash
cd build-rate/opensearch && cargo build --release --locked --no-default-features && cd ../..
{ echo "osrate: $(git rev-parse HEAD)";
  ./build-rate/opensearch/target/release/osrate --help | head -1; } \
  >> "$R/env/binary-provenance.txt"
```

## B2 — the ladder, at `batch=1`

**A batch level is a whole ladder, not a point.** The knee moves with batch
size, so one fixed concurrency would compare levels at a concurrency that suits
only one of them — and a single point per batch could not be a line. That is why
the script below still loops over `BATCHES`: restoring the sweep is setting one
variable, and nothing else about the run changes when you do.

```bash
cat > "$R/scripts/run-os-arm.sh" << 'EOF'
#!/bin/bash
# One arm per batch size: the same concurrency ladder, N times, against the
# local HTTP sink. Reset is ON, so every level DELETEs and recreates the index
# from the embedded ramindex mapping and gates on _count = 0 before a single
# document goes in. The analyzer probe is the one part of reset the sink cannot
# answer, so --no-analyzer-check is passed here and NOWHERE ELSE.
set -euo pipefail
BIN=${BIN:-./build-rate/opensearch/target/release/osrate}
CORPUS=${CORPUS:-./data/corpus.jsonl}
LADDER=${LADDER:-4,8,16}
REPS=${REPS:-1}
MAX_DOCS=${MAX_DOCS:-0}
BATCHES=${BATCHES:-1}
GEN_CPUSET=${GEN_CPUSET:-12-19}
stamp() { while IFS= read -r line; do printf '%s\t%s\n' "$(date -u +%H:%M:%S)" "$line"; done; }

for b in $BATCHES; do
  for rep in $(seq 1 "$REPS"); do
    echo "=== osrate batch=$b rep $rep/$REPS  ladder=$LADDER max_docs=$MAX_DOCS ==="
    taskset -c "$GEN_CPUSET" "$BIN" \
        --corpus "$CORPUS" --concurrency "$LADDER" \
        --batch-size "$b" --max-docs "$MAX_DOCS" --queue-depth 2 \
        --url http://127.0.0.1:9200 --index wiki-articles \
        --index-config ramindex --refresh-interval 1s --no-analyzer-check \
        --index-watch --index-interval 0.05 \
        --index-settle-timeout 600 --index-idle-timeout 30 \
        --reset-timeout 600 --tokio-workers 8 \
        --out "$R/opensearch/points/os-b$b-rep$rep.csv" \
        --samples-dir "$R/opensearch/samples/os-b$b-rep$rep" \
        2>&1 | stamp > "$R/opensearch/logs/os-b$b-rep$rep.stderr.tsv"
  done
done
EOF
chmod +x "$R/scripts/run-os-arm.sh"
```

```bash
"$R/scripts/run-os-arm.sh"
```

- **`batch=1` is the whole of Part B now.** It is what makes the curve mean
  anything: the per-request framing cost with bulking switched off, the only
  level whose x axis is the same shape as Part A's, and — against this
  instrument — the only OpenSearch-side level whose points are long enough to
  read. The larger sizes come back with a corpus big enough to hold them.
- **`--queue-depth 2`, not the default 10.** Buffered documents are
  `queue_depth x concurrency x batch_size`. Depth is recorded in the CSV header,
  and holding it fixed is what makes the batch levels comparable.
- **`--refresh-interval 1s` explicitly**, so the *requested* value is recorded
  in `refresh_interval_requested`. The sink does not implement it — what gates
  visibility here is the sink's own `--os-refresh-interval-ms`.
- **Do not tailor the ladder per batch level.** A taller ladder for the small
  batches would take them off the shared x grid and chart 1 can no longer be
  drawn. If `batch=1` is still rising at `c=16`, that is a result to state —
  "unresolved above 16" — not a reason to give it its own x values.

The whole arm is about 15 seconds.

## B3 — collect and verify

Same Gate A, Gate B and Gate C as Phase 4, against `"$R"/opensearch/points/*.csv`,
plus three more:

```bash
# the header says null-sink here too
grep -h -E '^# opensearch_version=' "$R"/opensearch/points/*.csv

# the batch_size COLUMN (8) agrees with the filename on every data row --
# checking only the header would miss a mislabelled file
for f in "$R"/opensearch/points/*.csv; do
  want=$(basename "$f" | sed 's/^os-b\([0-9]*\)-.*/\1/')
  awk -F, -v w="$want" -v f="$f" '!/^#/ && $1!="concurrency" && $8!=w \
      {print f": row batch="$8" but filename says "w}' "$f"
done

# no rejected requests (col 10). Against a sink there should be none at all.
awk -F, '!/^#/ && $1!="concurrency" && $10+0>0 {print FILENAME": failed_requests="$10}' \
    "$R"/opensearch/points/*.csv
```

**Gate B must be empty on this half.** `batch=1` clears three seconds on every
rung against the sink, on every box this runbook has been run on. Anything
flagged here is a real finding — most likely a `--max-docs` that never reached
`0`.

---

## Phase 5 — stop the sinks, and reconcile what they counted

Stop them with **SIGTERM** so each writes its `--stats-out` JSON. That file is
the run's independent witness: it is the only count of what arrived that does
not come from the thing being measured.

```bash
cat > "$R/scripts/stop-sinks.sh" << 'EOF'
#!/bin/bash
# SIGTERM each sink so it writes its --stats-out JSON. Kills by RECORDED PID,
# never by pattern: a pkill -f whose pattern appears in the command line that
# launched it kills the shell running it.
LOG_DIR=${LOG_DIR:?}
[ -s "$LOG_DIR/sinks.pids" ] || { echo "$LOG_DIR/sinks.pids is empty -- nothing to stop BY PID."
    echo "The sinks may still be up; find them with: ss -ltnp | grep -E ':(9042|9200|6080) '"; exit 1; }
pkill -f sample-sinks-cpu 2>/dev/null
while read -r mode port pid; do
    kill -TERM "$pid" 2>/dev/null && echo "TERM $mode $port pid $pid"
done < "$LOG_DIR/sinks.pids"
sleep 3
left=0
while read -r mode port pid; do
    [ -d "/proc/$pid" ] && { echo "STILL UP: $mode $port pid $pid"; left=1; }
done < "$LOG_DIR/sinks.pids"
ss -ltn | grep -E ':(9042|9200|6080) ' && { echo "A PORT IS STILL LISTENING"; left=1; }
[ "$left" = 0 ] && echo "stopped, and every stats-out written"
exit "$left"
EOF
chmod +x "$R/scripts/stop-sinks.sh"
```

```bash
LOG_DIR="$R/sinks" "$R/scripts/stop-sinks.sh"
ss -ltn | grep -E ':(9042|9200|6080) ' || echo "all ports free"
```

Then reconcile. Two checks, and both are end-to-end in a way no single-sided
gate is:

```bash
.venv/bin/python3 - "$R" << 'EOF'
import csv, json, pathlib, sys
R = pathlib.Path(sys.argv[1])

def submitted(pattern):
    total = 0
    for path in sorted(R.glob(pattern)):
        rows = [ln for ln in path.read_text().splitlines() if not ln.startswith("#")]
        for row in csv.DictReader(rows):
            total += int(row["docs"])
    return total

for sink, pattern in (("cql-9042", "scylla/points/*.csv"),
                      ("http-9200", "opensearch/points/*.csv")):
    stats = json.loads((R / "sinks" / f"sink-{sink}.json").read_text())
    accepted, claimed = stats["docs_accepted"], submitted(pattern)
    print(f"{sink}: sink accepted {accepted:,}, CSVs claim {claimed:,}  "
          f"{'OK' if accepted == claimed else 'MISMATCH'}")
    print(f"{sink}: unexpected routes {stats['unexpected_requests']}")
EOF
```

- **`docs_accepted` must equal the sum of the CSVs' `docs` column** — 3 x 270,269
  = 810,807 on each side. A shortfall is documents the loader believes it sent
  and the sink never saw.
- **`unexpected_requests` must be exactly the two unanswered read-backs** on the
  HTTP side — `GET /wiki-articles/_settings` and `GET /wiki-articles/_mapping`,
  once each, one per arm — and **empty on the CQL side**. If
  `POST /wiki-articles/_analyze` appears there, `--no-analyzer-check` was
  dropped. If a status path appears with a keyspace or index you did not expect,
  a harness was pointed somewhere the gate would not have caught.

---

## Traps, all of them met in practice

1. **`pkill -f` / `pgrep -f` in the same command that writes the script.** The
   creating command line contains the pattern, so the match is the shell itself
   and it dies — taking `$R` with it. Write the script in one command, run it in
   the next. This is the most common failure in this runbook.
2. **Recording setsid's wrapper pid instead of the Python's.** The wrapper exits
   at once, the CPU gate then reads an empty `/proc` and reports a sink that
   used no CPU — which reads as "not the constraint" and is really "not
   measured". `pgrep -P "$wrapper"` is what the launcher does about it.
3. **`--index-watch` left off.** Half the run, silently. The gate catches it;
   nothing else does.
4. **Polling at 1 Hz.** A build that finishes in half a second leaves one
   reading and the index-size chart drops it **by name in its footer** — easy to
   miss if nobody reads the footer. `--index-interval 0.05` / `--vs-interval 0.05`.
5. **Chart 2's OpenSearch glob written as `c4-b*-*.csv`.** At `--batch-size 1`
   the series files are named `c4-1.csv`, with **no `b1` segment** — so that
   glob silently misses the whole `batch=1` build, which is now the entire
   OpenSearch half: the chart draws the ScyllaDB line alone, and nothing is
   printed about it because the file never matched. Glob `c4-*.csv` on both
   halves.
6. **`--no-analyzer-check` leaking into an engine run.** Here it is required;
   against a real OpenSearch it disables the one probe that makes a BM25
   comparison trustworthy.
7. **Reading `source_enabled=true` / `body_analyzer=unset` as the mapping having
   been ignored.** It is the settings read-back the sink does not answer.
   `index_config=ramindex` is what went out.
8. **A sink-bound level averaged in.** Gate C says which. Report those as `≥`.
9. **`index_settled=false` averaged in.** Same treatment: it is a floor.
10. **Rebuilding a binary mid-run.** The arms stop being comparable and the CSV
    headers stop describing the binaries that wrote them.
11. **Starting an engine container "to compare".** See the top of this file: one
    engine up under these gates is the one result that looks plausible and is
    wrong.
12. **Quoting anything.** Four independent reasons, none of which a bigger
    matrix removes.

---

# The charts — run these last, with both sinks down

Two of them, off the artifacts alone. If either cannot be produced from `$R`
with nothing running, something was not written and a gate was skipped.

## Chart 1 — X is concurrency, and every configuration is a solid/dashed pair

```bash
cd ~/Projects/Scylla/p99/bench
.venv/bin/python3 build-rate/charts/rate_vs_concurrency.py \
    --scylla     "$R/scylla/points/scylla-rep*.csv" \
    --opensearch "$R/opensearch/points/os-b*-rep*.csv" \
    --keep-warmup \
    --output     "$R/build-rate-vs-concurrency.png" \
    --table      "$R/build-rate-vs-concurrency.csv" \
    --title      "Client rate against concurrency — NULL SINK, no engine" \
    --subtitle   "$RUN_ID · null_sink, laptop, N=1 · NOTHING HERE IS AN ENGINE NUMBER"
```

Four lines off two configurations:

| Colour | Solid | Dashed |
|---|---|---|
| `#2b6cb0` | `scyllarate CQL 1 doc/op` — CQL inserts accepted | the modelled index's count |
| ramp 1 | `osrate batch=1` — `_bulk` documents accepted | documents the sink published |

**The chart must print `(4 lines, 12 points)`.** That line is the single best
check in this runbook: four lines means both configurations produced both
families, and twelve points means three x values on every one of them. Anything
less and a config lost its index columns or a rung never ran — go back to
Gate A.

Expect a `SHORT POINTS` footer only if Part A's upper rungs came in under three
seconds — it must name those and nothing on the OpenSearch side.

**The subtitle is doing real work here.** Against the sink the two dashed lines
are not two mechanisms — they are one counter answering two protocols — so the
footer's standing warning not to compare them is, for once, understating it:
there is nothing to compare.

## Chart 2 — X is the index itself, at `c=4`

```bash
.venv/bin/python3 build-rate/charts/rate_vs_index_size.py \
    --scylla     "$R/scylla/samples/*/c4-*.csv" \
    --opensearch "$R/opensearch/samples/*/c4-*.csv" \
    --output     "$R/build-rate-vs-index-size.png" \
    --table      "$R/build-rate-vs-index-size.csv" \
    --title      "Client rate as the modelled index grows — NULL SINK" \
    --subtitle   "$RUN_ID · c=4 · null_sink · NOTHING HERE IS AN ENGINE NUMBER"
```

**Both globs are `c4-*.csv`.** On the OpenSearch side that is not a
simplification of `c4-b*-*.csv` — it is the fix for trap 5, and with `batch=1`
the only arm it is now fatal rather than merely lossy: that series carries no
`b` segment in its filename, so the narrower glob drops the whole OpenSearch
half and says nothing about it.

**Two bold lines — one per build at `c=4`, and the slice is the ladder's BOTTOM
rung on purpose.** It is the rung whose build lasts longest, and a build needs
**more than three readings** to be drawn at all. **The chart must print
`(2 series)`.** One would mean a build was skipped — read the `skipped ...`
lines it prints underneath, which name the file and the reason — or that the
glob missed a file, which prints nothing at all.

It answers what chart 1 cannot: whether a plateau was a plateau or the mean of
two halves, where the client stopped submitting (the tick on each line), and
whether the rate moved as the modelled index grew. Against this instrument the
honest expectation for that last one is **flat**, because nothing is being
built — a line that sags here is the loader or the box, not an index.

**One footer line on this chart is now wrong.** The renderer hard-codes
`Laptop, shared box, docker/.env laptop-simulation caps: NOT QUOTABLE.` There is
no `docker/.env` in a sink run. The verdict still holds and the subtitle carries
the true provenance, but `rate_vs_index_size.py:197` should be parameterised —
noted here rather than silently patched.

## Then write it down

```bash
cat > "$R/README.md" << EOF
# Pipeline proving run, NULL SINK, $(date -u +%Y-%m-%d)

**A PROVING RUN AGAINST AN ACCEPT-AND-DISCARD SINK. No number in this directory
is a measurement of anything but this client** — not even a preliminary one.
There was no ScyllaDB, no vector-store and no OpenSearch: both halves talked to
\`ftsbench.null_sink\`, which stores nothing. Produced by
\`bench/build-rate/HARNESS-LOCAL-RUNBOOK.md\` over the whole 270,269-document
frozen simplewiki corpus, N=1, with the client and the sink on one box.

What it establishes is that the machinery runs end to end: both sinks up, both
binaries reaching them, reset-per-level rebuilding an empty modelled index on
each side, the index columns populated, the sink's own count reconciling with
the CSVs, and both charts rendering from these files alone.

Run \`$RUN_ID\`. Ladder 4,8,16 · N=1 · scyllarate against the CQL sink, then
osrate against the HTTP sink at batch 1.

| Chart | What it shows |
|---|---|
| \`build-rate-vs-concurrency.png\` | docs/s against concurrency; solid = submitted, dashed = what the sink published, one colour per configuration |
| \`build-rate-vs-index-size.png\` | docs/s against documents in the modelled index, one line per build at c=4 |
EOF
```

Fill in, by hand, underneath: whether Gate A passed clean, what Gate C said per
port, which levels Gate B flagged as short, which builds the growth chart
skipped and why, whether the sink reconciliation matched, and any row that came
back `index_settled=false`. Then hand the user the absolute path of `$R`. **A
results directory nobody can find is the same as no results.**

---

# What a pass looks like

The run succeeded if **all seven** of these hold. Report them as a list, by
number, and do not describe the run as working if one of them is missing.

| # | Check | Where it comes from |
|---|---|---|
| 1 | Both sinks came up and both probe URLs answered | Phase 1 |
| 2 | **Gate A printed nothing but row counts, and every count is 3** | Phase 4 and B3 |
| 3 | Every CSV header carries a `-null-sink` version | Phase 4 and B3 |
| 4 | **Gate C reported a peak for both ports**, and each is named `ok` or `SINK` | Phase 4 |
| 5 | The sink's `docs_accepted` reconciled with the CSVs, and `unexpected_requests` held only the two read-backs | Phase 5 |
| 6 | Chart 1 printed **`(4 lines, 12 points)`** | the chart section |
| 7 | Chart 2 printed **`(2 series)`** and skipped nothing | the chart section |

Gate B flagging Part A's upper rungs does **not** fail the run: it is the corpus
being smaller than this instrument needs, it is what a fast box does to a
270,269-document corpus, and check 6's point count already proves those levels
were measured. Name them, and never quote them as rates. Gate B flagging
**`batch=1`** **does** fail it — that half clears three seconds on every rung.

If any of the seven fails, the fix is almost always one of the traps above — a
pattern-kill that took the shell, `--index-watch` missing, a poll interval too
coarse for a build this short, chart 2's glob missing the `batch=1` series, or
something real still listening on 9042.

---

# Then the longer run

When the seven checks pass, the same document runs a wider matrix. **Change one
table and one glob; nothing else in this runbook moves.**

| | Proving run | Longer run |
|---|---|---|
| `LADDER` | `4,8,16` | `4,8,16,32` |
| `BATCHES` | `1` | `1 128 512 1024` |
| `REPS` | `1` | `3` |
| Chart 2 slice | `c4-*` — the longest-running rung | `c32-*` — the top rung, where the client works hardest |
| Index builds | 6 | 60 |
| Wall | under 1 min of measurement | 3–6 min |

```bash
REPS=3 LADDER=4,8,16,32                          "$R/scripts/run-scylla-arm.sh"
REPS=3 LADDER=4,8,16,32 BATCHES="1 128 512 1024" "$R/scripts/run-os-arm.sh"
```

Two things become true then that are not true here:

- **N=3 makes the spread visible.** Chart 1 gains min..max bars and chart 2
  gains thin repetition lines behind each bold median; with N=1 both collapse to
  a single line that cannot show disagreement.
- **Gate C starts to matter more than Gate A.** With three repetitions at four
  rungs the HTTP sink spends real time near its one core, and a level that
  crosses 0.85 is a lower bound however clean its CSV looks.

**Gate B still cannot be emptied by rerunning.** The 270,269-document corpus is
simply too small for this instrument: the whole of it is accepted in well under
a second at `batch=128` and above, and `scyllarate` puts it away in 1.7 s at
`c=16`. The only fix is
a bigger corpus, generated the way the fleet does it:

```bash
mkdir -p /tmp/synth                      # synth_corpus does NOT create it
.venv/bin/python3 -m ftsbench.synth_corpus --output /tmp/synth/part.jsonl \
    --docs 1250000 --mean-bytes 1688 --sigma 0.6 --shards 8 \
    --stats-out /tmp/synth/corpus.stats.json
cat /tmp/synth/part-*.jsonl > /tmp/synth/corpus.jsonl
```

`--mean-bytes 1688` matches the frozen simplewiki corpus's own mean line, so the
per-document cost stays comparable; 1.25 M documents is about 2.1 GB and is what
gives `batch=1024` a three-second point. Then run both arms with
`CORPUS=/tmp/synth/corpus.jsonl` and record the new `sha256` in
`$R/env/corpus.txt`. The client does not read the words — its per-document cost
is a function of size and shape only — so a synthetic corpus costs this run
nothing but disk.

Even then the numbers stay unquotable, for the four reasons at the top of this
file that no corpus and no matrix can touch. **The engine numbers come from
`../AWS-RUN-PLAN.md`, and the client ceiling they must stay under comes from
`HARNESS-AWS-RUNBOOK.md`, where the sink runs on its own box and there is one
of them per loader process.**
