# Harness-on-laptop runbook — a short run that proves the pipeline works

**Hand this file to Claude Code as the instruction and it runs a short proving
pass: brings up one engine at a time in Docker, builds both harnesses, runs
twelve index builds across three concurrency levels and three OpenSearch batch
sizes, tears each stack down, and renders the two charts.** It is
self-contained; every script it needs is inline below. Nothing else in `bench/`
has to be read first. **Budget 10–15 minutes.**

**This run exists to prove the machinery, not to measure the engines.** Every
level is deliberately tiny, so most of them finish in under the three seconds
that makes a point a measurement. The charts will say so in their own footers,
and that is the expected outcome rather than a fault. **No number from this run
may be repeated at all** — not even as a preliminary one.

What it does establish is narrower and worth having on its own:

| # | The claim it proves |
|---|---|
| 1 | Both stacks come up from `docker/` and both binaries reach them |
| 2 | Reset-per-level really does rebuild an empty index on **both** sides, gates included |
| 3 | `--index-watch` populates the six `index_*` columns on both halves |
| 4 | `--samples-dir` leaves a series with enough readings to draw |
| 5 | Both charts render from `$R` alone, with both engines down |

The full matrix this is a rehearsal for is at the end, under **Then the real
run**. Nothing below changes for it except the numbers in one table.

Two halves, measured in this order, off one corpus, on one box:

| Part | Harness | Binary | Under test | Extra axis |
|---|---|---|---|---|
| **A** | `build-rate/scylla` | `scyllarate` | **real** ScyllaDB + vector-store, in Docker | — |
| **B** | `build-rate/opensearch` | `osrate` | **real** OpenSearch, in Docker, index in RAM | **one full ladder per `--batch-size`** |

Part B repeats its whole ladder once per batch size, because on the OpenSearch
side one request carries many documents. **Batch size is never an axis: it is a
series.** Every batch level runs the same concurrency grid, so they all land on
one chart as separate lines.

Run Part A first. It is the simpler stack, it establishes the corpus checksum
and the host capture, and the ScyllaDB numbers are what Part B is read against.

---

## What this measures, and what it is not

The subject of a **full** run is the engines: both binaries push at a real
ScyllaDB + vector-store and a real OpenSearch, with the index reset to empty
before every level, so what comes back is a genuine build rate and every level's
`index_docs_per_s` is the engine's rather than the client's.

The subject of **this** run is the pipeline that produces that. It exercises
every step end to end on a budget small enough to fit in a coffee break, and it
answers "does this work" rather than "how fast is it".

**And none of it is quotable.** Four separate reasons, each sufficient:

- **The budget is a rehearsal budget.** 50,000 documents a level, 10,000 at
  `batch=1`, N=1. A point that short is dominated by the level's own start-up
  and carries no rate anybody should read.

- `docker/.env`'s own header says every sizing value in it is a
  laptop-simulation value and that nothing produced under them may be quoted.
- This box is shared with whatever else is running on it. The engine containers
  are pinned to `ENGINE_CPUSET` and the loader to `GEN_CPUSET`, which bounds the
  contention but does not remove it.
- One box runs the client and the engine. On the fleet they are separate hosts
  with a measured 0.142 ms between them.

So: **a green pipeline, and no numbers**. The first three reasons still apply
after the matrix is widened, which is why even a full local run is preliminary
and never reaches the deck.

| File | Its job |
|---|---|
| `../HARNESS-AWS-RUNBOOK.md` | the same two binaries against a **null sink** on AWS — the client ceiling these numbers have to stay under |
| `../AWS-RUN-PLAN.md` | the **quotable** engine campaign (C1–C8) on the fleet |
| `../BUILD-RATE-LOOP.md` | the vector-store ingest optimisation journal |
| `../TUNING.md` | where a measured ceiling gets recorded |
| `charts/README.md` | the two renderers this ends in, and why they are not in `tools/` |

This runbook supersedes the local mechanics in all of them **for build-rate runs
only**.

---

## The results directory — fix it first

Everything lands in one timestamped directory, named so that it says on its face
that it is a local harness run and when it happened. Create it before touching a
container, and keep the shell.

```bash
# Run this ONCE, at the very start of the session, and keep the shell.
cd ~/Projects/Scylla/p99/bench
export RUN_ID="local-harness-$(date -u +%Y-%m-%dT%H%MZ)"
export R="$PWD/results/$RUN_ID"
mkdir -p "$R"/{env,scripts}
mkdir -p "$R"/scylla/{points,samples,logs}
mkdir -p "$R"/opensearch/{points,samples,logs}
printf '%s\n' "$RUN_ID" > "$R/RUN_ID"
ln -sfn "$RUN_ID" results/local-harness-latest
echo "results -> $R"
```

giving, for example:

```
bench/results/local-harness-2026-09-14T1204Z/
├── RUN_ID
├── env/            host.txt  docker-env.txt  images.txt  binary-provenance.txt  corpus.txt
├── scripts/        the two arm scripts, as they were actually run
├── scylla/
│   ├── points/     scylla-rep1.csv
│   ├── samples/    scylla-rep1/c4-1.csv c8-1.csv c16-1.csv
│   └── logs/       scylla-rep1.stderr.tsv
├── opensearch/
│   ├── points/     os-b1-rep1.csv  os-b128-rep1.csv  os-b1024-rep1.csv
│   ├── samples/    os-b128-rep1/c4-b128-1.csv c8-b128-1.csv c16-b128-1.csv
│   └── logs/
├── build-rate-vs-concurrency.png / .csv
├── build-rate-vs-index-size.png  / .csv
└── README.md
bench/results/local-harness-latest -> local-harness-2026-09-14T1204Z
```

UTC to the minute, always, so two runs on one afternoon stay distinct and sort
in order. If the shell dies: `export R="$(readlink -f results/local-harness-latest)"`.

---

# Part A — ScyllaDB (`scyllarate`)

## Phase 0 — the matrix, and the memory gate

### The matrix

| | |
|---|---|
| Concurrency ladder | `4,8,16` — three levels, **no throwaway warm-up row**; see below |
| OpenSearch batch sizes | `1, 128, 1024` — the bottom, a middle and the top |
| Repetitions | **N=1** |
| Corpus | `data/corpus.jsonl`, frozen simplewiki, 270,269 documents — **a slice of it** |
| `--max-docs` | `50000`, except `osrate --batch-size 1`, which gets `10000` |
| Index builds | 3 levels x 1 rep x 4 configurations = **12 builds**, every one from an empty index |
| Estimated wall | **10–15 min** including bring-up, teardown and both renders |

Three batch levels rather than four, because the ends are what prove the
mechanism: `1` is one document per request with bulking switched off, `1024` is
the top, and `128` shows the middle is wired up too. N=1, because a repetition
count proves nothing about whether the pipeline runs — it only narrows a number
this run is not allowed to report.

### The shared concurrency grid — do not vary it per arm

**Every series in both parts is measured on the same x values:**

```
4   8   16
```

The deliverable chart puts concurrency on x and every engine-and-batch
combination on it as a series. Series that do not share x values cannot be drawn
on one axis, so a ladder tailored per arm silently destroys the chart. If a
level has to be added, add it to **every** arm. Powers of two, because x is
drawn on a log2 axis.

### The ladder carries no warm-up row, and the charts are told so

`4,8,16` is three levels and three x values. It is **not** the `8,8,16,32,64`
idiom the AWS runbook uses, where the first rung is repeated so the leading one
can be thrown away — and both renderers drop the first data row of every CSV by
default, which on this ladder would delete `c=4` outright rather than delete a
throwaway, leaving a two-point chart.

So the chart command passes **`--keep-warmup`**. On a proving run the cost of
that is irrelevant — `c=4` is measured on a cold process, and no point here is a
measurement anyway — but it is the same flag the real run needs, which is part
of what this rehearses. Putting a throwaway back (`LADDER=4,4,8,16`, and drop
`--keep-warmup`) is the alternative when the numbers start to matter.

What must not happen is the ladder keeping three rungs while the chart keeps its
default: that silently yields a two-point x axis and a `c=4` that was measured
and then discarded.

**Two budgets, and both are rehearsal budgets.** 50,000 documents serves every
level except `osrate --batch-size 1`, which at roughly 800 docs/s at `c=4` would
take a minute on its own and gets 10,000 instead. Neither is a measurement
budget. **Expect most points to come in under three seconds** and expect both
charts to say so — that is the short-point gate working, not the run failing.

### The memory gate — run this before anything comes up

```bash
free -g
```

**Refuse to start an engine with under 12 GiB available**, and say what has to be
closed. `docker/.env` gives the ScyllaDB side `4g + 8g` container caps and
OpenSearch `8g` with a 4 GiB heap; this box has 30 GiB total and a devcontainer
can be holding 20 of them. An engine that gets OOM-killed mid-ladder does not
error — it truncates a series, and the truncated level reports a *faster* build
than the honest one.

### The two engines never run at the same time

Not a preference. They are pinned to the **same** `ENGINE_CPUSET=0-11`, and
their memory budgets do not both fit. Part A brings ScyllaDB up, runs, and takes
it down with `-v` before Part B touches OpenSearch. A run where both were up is
a contention measurement and has to be discarded, not caveated.

---

## Phase 1 — bring up ScyllaDB + vector-store

```bash
cd ~/Projects/Scylla/p99/bench
make scylla-reset   # down -v: the base table survives a plain `down`, and a
                    # level that starts on a non-empty table is not a build
make scylla-up
make scylla-wait    # BOTH halves must answer: CQL, and the vector-store's
                    # /api/v1/status. Fulltext DDL is served by the latter.
```

Capture the environment, once, while it is up:

```bash
{ echo "host: $(uname -srm)"; echo "cores: $(nproc)"; free -h; } > "$R/env/host.txt"
grep -vE '^\s*#|^\s*$' docker/.env > "$R/env/docker-env.txt"
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Ports}}' > "$R/env/images.txt"
{ echo "corpus: $PWD/data/corpus.jsonl";
  echo "lines: $(wc -l < data/corpus.jsonl)";
  echo "bytes: $(stat -c%s data/corpus.jsonl)";
  echo "sha256: $(sha256sum data/corpus.jsonl | cut -d' ' -f1)"; } > "$R/env/corpus.txt"
```

**`docker-env.txt` is not optional.** It is the only record of which
laptop-simulation caps produced these numbers, and the reason the README can say
`NOT QUOTABLE` with a citation rather than a vibe.

## Phase 2 — build the harness, then freeze it

```bash
cd build-rate/scylla && cargo build --release --locked && cd ../..
```

```bash
{ echo "scyllarate: $(cd build-rate/scylla && git rev-parse HEAD)";
  echo "dirty: $(git status --porcelain build-rate | wc -l) files";
  ./build-rate/scylla/target/release/scyllarate --help | head -1; } \
  >> "$R/env/binary-provenance.txt"
```

**Do not rebuild once an arm has run.** A rebuild mid-campaign makes the arms
incomparable, and the driver version stamped into each CSV header stops being
the version that produced it.

## Phase 3 — run the ladder

Write the arm script into `$R/scripts/` so the run records exactly what was run:

```bash
cat > "$R/scripts/run-scylla-arm.sh" << 'EOF'
#!/bin/bash
# One arm: the same concurrency ladder, N times, against the local ScyllaDB.
# Reset is ON by default and that is the point -- every level DROPs the keyspace,
# waits for the vector-store to forget the index, recreates both, and gates on
# SERVING at 0 documents. So every level is a build from empty, which is what
# makes an index-size axis mean anything.
#
# stderr is timestamped per line so a level's wall-clock window can be cut out
# of anything else sampling the box.
set -euo pipefail
BIN=${BIN:-./build-rate/scylla/target/release/scyllarate}
CORPUS=${CORPUS:-./data/corpus.jsonl}
LADDER=${LADDER:-4,8,16}
REPS=${REPS:-1}
MAX_DOCS=${MAX_DOCS:-50000}
GEN_CPUSET=${GEN_CPUSET:-12-19}
stamp() { while IFS= read -r line; do printf '%s\t%s\n' "$(date -u +%H:%M:%S)" "$line"; done; }

for rep in $(seq 1 "$REPS"); do
  echo "=== scylla rep $rep/$REPS  ladder=$LADDER ==="
  taskset -c "$GEN_CPUSET" "$BIN" \
      --corpus "$CORPUS" --concurrency "$LADDER" --max-docs "$MAX_DOCS" \
      --hosts 127.0.0.1 --port 19042 \
      --vs-url http://localhost:16080 --vs-index articles_body_fts \
      --vs-interval 0.25 --vs-settle-timeout 600 --vs-idle-timeout 30 \
      --reset-timeout 600 --tokio-workers 8 \
      --out "$R/scylla/points/scylla-rep$rep.csv" \
      --samples-dir "$R/scylla/samples/scylla-rep$rep" \
      2>&1 | stamp > "$R/scylla/logs/scylla-rep$rep.stderr.tsv"
done
EOF
chmod +x "$R/scripts/run-scylla-arm.sh"
"$R/scripts/run-scylla-arm.sh"
```

Four flags there are not defaults and each has a reason:

- **`--vs-interval 0.25`.** The index-size chart needs **more than three
  readings per build** or it drops the build by name. At one poll per second a
  level that finishes in two seconds leaves two readings and vanishes from the
  chart silently. Quarter-second polling is what keeps the fast levels on it.
- **`--vs-idle-timeout 30`.** Ten seconds of no index progress ends the settle
  wait. A laptop vector-store stalls longer than that mid-build, and ending the
  wait early reports `index_settled=false` and a build rate that is a floor.
- **`--vs-settle-timeout 600`.** Generous on purpose. 50,000 documents settle
  quickly, but the real run's 270,269 do not, and a timeout that only works at
  rehearsal scale has not been rehearsed.
- **`--tokio-workers 8`.** The eight cores `GEN_CPUSET` actually grants. Left at
  every core the runtime spawns 22 threads for 8 cores' worth of CPU.

Run it in the background or with a generous timeout; do not poll it every few
seconds.

## Phase 4 — collect, verify, and only then tear down

Everything already writes into `$R`, so there is nothing to copy. Verify before
the stack goes down, while it can still be re-run.

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

**Gate B — expected to flag on a proving run, and not a failure here:**

```bash
# points too short to be a measurement (col 4 is wall_s). On the rehearsal
# budget most of them will be, which is the gate working rather than the run
# failing. On the real run this list must be EMPTY.
awk -F, '!/^#/ && $1!="concurrency" && $4+0<3 {print FILENAME": c="$1" wall="$4"s -- not a measurement"}' \
    "$R"/scylla/points/*.csv
```

The distinction is the whole reason there are two gates. Gate A says the
machinery ran; Gate B says whether what it produced is readable. A proving run
is allowed to fail B and is never allowed to fail A.

An `index_settled=false` row is not a failure to discard — it is a build rate
that must be reported as `≥`. Say which rows they were.

```bash
make scylla-reset   # down -v again: Part B needs the cores and the RAM, and
                    # the base table must not survive into a later session
free -g             # confirm the memory came back before Part B
```

---

# Part B — OpenSearch (`osrate`), once per batch size

## B0 — what is different about this half

Same box, same session, same corpus, same ladder, same gates. Only the deltas
are written out here; anything not mentioned is unchanged from Part A.

- **`--index-watch` is OFF by default and must be passed.** Without it all six
  `index_*` columns are blank, and this half contributes no dashed line to chart
  1 and nothing at all to chart 2. This is the single easiest way to lose half
  the campaign.
- **A searchable count moves in steps.** `docs.count` advances at a refresh, so
  a build curve here is flat, flat, jump. The renderer widens its bucket to one
  riser and says so in its footer; do not read the flats as stalls.
- **`index_lag_docs` has a floor** of `refresh_interval x docs_per_s`, however
  fast the engine. The number to read is the excess over it.
- **`p50_ms`/`p99_ms` are per `_bulk` request, not per document.** `latency_unit`
  in the header says which. This is the only reason the two subtrees stay
  separate.
- **Reset is per level here too**, and it also runs the analyzer parity probe —
  an index whose analyzer does not match the vector-store's is not a comparison.
  Leave `--no-analyzer-check` off.

## B1 — bring up OpenSearch, index in RAM

```bash
OS_RAM_INDEX=1 make os-reset       # down -v: a named volume outlives `down`,
                                   # and an index from an earlier run is not cold
OS_RAM_INDEX=1 make os-up
make os-wait
make os-relax-watermarks           # this host's Docker root sits above the 90%
                                   # heuristic with tens of GB free; without this
                                   # every index create fails with a bare 403
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Ports}}' >> "$R/env/images.txt"
```

`OS_RAM_INDEX=1` overlays `docker/docker-compose.opensearch.ramindex.yml`, which
puts the segment files on a tmpfs. That is the parity choice: the vector-store's
Tantivy index is RAM-resident and rebuilt on restart, and an OpenSearch index on
disk would be answering a different question. It is **not** a durability
comparison and must never be described as one.

Build the binary without TLS — the compose file publishes plain HTTP, and a run
must not depend on the box's OpenSSL:

```bash
cd build-rate/opensearch && cargo build --release --locked --no-default-features && cd ../..
{ echo "osrate: $(git rev-parse HEAD)";
  ./build-rate/opensearch/target/release/osrate --help | head -1; } \
  >> "$R/env/binary-provenance.txt"
```

## B2 — the batch sweep, which is the point of Part B

**A batch level is a whole ladder, not a point.** The knee moves with batch
size, so one fixed concurrency would compare each level at a concurrency that
suits only one of them — and a single point per batch could not be a line.

```bash
cat > "$R/scripts/run-os-arm.sh" << 'EOF'
#!/bin/bash
# One arm per batch size: the same concurrency ladder, N times, against the
# local OpenSearch. Reset is ON, so every level DELETEs and recreates the index
# from the embedded ramindex mapping, gates on _count = 0, and checks the
# analyzer against m1_parity before a single document goes in.
set -euo pipefail
BIN=${BIN:-./build-rate/opensearch/target/release/osrate}
CORPUS=${CORPUS:-./data/corpus.jsonl}
LADDER=${LADDER:-4,8,16}
REPS=${REPS:-1}
BATCHES=${BATCHES:-1 128 1024}
GEN_CPUSET=${GEN_CPUSET:-12-19}
stamp() { while IFS= read -r line; do printf '%s\t%s\n' "$(date -u +%H:%M:%S)" "$line"; done; }

for b in $BATCHES; do
  # batch=1 is ~1 document per request and 50,000 of them would take a minute
  # per level on its own. Every other level gets the same budget so their
  # index-size lines cover the same x range.
  if [ "$b" = "1" ]; then MAX_DOCS=10000; else MAX_DOCS=50000; fi
  for rep in $(seq 1 "$REPS"); do
    echo "=== osrate batch=$b rep $rep/$REPS  ladder=$LADDER max_docs=$MAX_DOCS ==="
    OS_REFRESH_INTERVAL=1s taskset -c "$GEN_CPUSET" "$BIN" \
        --corpus "$CORPUS" --concurrency "$LADDER" \
        --batch-size "$b" --max-docs "$MAX_DOCS" --queue-depth 2 \
        --url http://localhost:9200 --index wiki-articles \
        --index-config ramindex --refresh-interval 1s \
        --index-watch --index-interval 0.25 \
        --index-settle-timeout 600 --index-idle-timeout 30 \
        --reset-timeout 600 --tokio-workers 8 \
        --out "$R/opensearch/points/os-b$b-rep$rep.csv" \
        --samples-dir "$R/opensearch/samples/os-b$b-rep$rep" \
        2>&1 | stamp > "$R/opensearch/logs/os-b$b-rep$rep.stderr.tsv"
  done
done
EOF
chmod +x "$R/scripts/run-os-arm.sh"
"$R/scripts/run-os-arm.sh"
```

- **`batch=1` is not optional.** It is what makes the curve mean anything: the
  per-request framing cost with bulking switched off, and the only level whose x
  axis is the same shape as Part A's.
- **`--queue-depth 2`, not the default 10.** Buffered documents are
  `queue_depth x concurrency x batch_size`. At `c=16 batch=1024` the default
  holds 163,840 documents — a quarter-gigabyte of corpus in the loader's own
  heap, on a box that already refused to run both engines at once.
- **`--refresh-interval 1s` explicitly**, so the header records it and the
  `index_lag_docs` floor is a number rather than a guess.
- **Do not tailor the ladder per batch level.** A taller ladder for the small
  batches would reach their knee and take them off the shared x grid, and chart
  1 can no longer be drawn. If `batch=1` is still rising at `c=16`, that is a
  result to state — "unresolved above 16" — not a reason to give it its own x
  values.

## B3 — collect, verify, tear down

Same gate as Phase 4 against `"$R"/opensearch/points/*.csv`, plus two more:

```bash
# the batch_size COLUMN (8) agrees with the filename on every data row --
# checking only the header would miss a mislabelled file
for f in "$R"/opensearch/points/*.csv; do
  want=$(basename "$f" | sed 's/^os-b\([0-9]*\)-.*/\1/')
  awk -F, -v w="$want" -v f="$f" '!/^#/ && $1!="concurrency" && $8!=w \
      {print f": row batch="$8" but filename says "w}' "$f"
done

# no rejected requests (col 10). A 429 in the first failure is queue rejection,
# not saturation, and it silently drops documents from the level.
awk -F, '!/^#/ && $1!="concurrency" && $10+0>0 {print FILENAME": failed_requests="$10}' \
    "$R"/opensearch/points/*.csv
```

```bash
OS_RAM_INDEX=1 make os-reset
```

---

## Traps, all of them met in practice

1. **`--index-watch` left off.** Half the campaign, silently. The gate above
   catches it; nothing else does.
2. **Polling at 1 Hz.** A build that finishes in two seconds leaves two readings
   and the index-size chart drops it **by name in its footer** — easy to miss if
   nobody reads the footer. `--index-interval 0.25` / `--vs-interval 0.25`.
3. **Both engines up at once.** They share `ENGINE_CPUSET` and do not both fit in
   memory. Every number from such a run is a contention number. Discard it.
4. **A plain `down` instead of `down -v`.** ScyllaDB's base table and
   OpenSearch's named volume outlive a plain `down`, so the next "build from
   empty" starts on yesterday's data.
5. **Reading the OpenSearch flats as stalls.** They are the refresh interval.
   The excess over `refresh_interval x docs_per_s` is the only part that is lag.
6. **Comparing the two dashed lines on chart 1 to each other.** They are
   different mechanisms — continuous Tantivy publishing versus refresh-gated
   visibility. Each is read against its own solid line.
7. **`index_settled=false` averaged in.** It is a floor. Report it as `≥` and
   name the rows.
8. **Rebuilding a binary mid-campaign.** The arms stop being comparable and the
   CSV headers stop describing the binaries that wrote them.
9. **Quoting anything.** See the top of this file. Four independent reasons.
10. **Treating a short-point warning as a broken run.** On this budget it is the
    expected outcome and it is Gate B, not Gate A. Treating it as a failure
    sends someone chasing a bug that is a deliberate setting.

---

# The charts — run these last, with both engines down

Two of them, off the artifacts alone. If either cannot be produced from `$R`
without a container running, something was not written and a gate was skipped.

## Chart 1 — X is concurrency, and every configuration is a solid/dashed pair

```bash
cd ~/Projects/Scylla/p99/bench
.venv/bin/python3 build-rate/charts/rate_vs_concurrency.py \
    --scylla     "$R/scylla/points/scylla-rep*.csv" \
    --opensearch "$R/opensearch/points/os-b*-rep*.csv" \
    --keep-warmup \
    --output     "$R/build-rate-vs-concurrency.png" \
    --table      "$R/build-rate-vs-concurrency.csv" \
    --title      "Build rate against concurrency — real engines, laptop" \
    --subtitle   "$RUN_ID · PROVING RUN, 50k docs/level, N=1 · NO NUMBER HERE IS A MEASUREMENT"
```

Eight lines off four configurations:

| Colour | Solid | Dashed |
|---|---|---|
| `#2b6cb0` | `scyllarate CQL 1 doc/op` — CQL inserts accepted | the vector-store's FTS build |
| ramp 1–3 | `osrate batch=1 / 128 / 1024` — `_bulk` documents accepted | documents made searchable |

**The chart must print `(8 lines, 24 points)`.** That line is the single best
check in this runbook: eight lines means all four configurations produced both
families, and twenty-four points means three x values on every one of them.
Anything less and a config lost its index columns or a rung never ran — go back
to Gate A.

Expect a `SHORT POINTS` footer naming most of the levels. On the rehearsal
budget that is correct.

## Chart 2 — X is the index itself, at `c=4`

```bash
.venv/bin/python3 build-rate/charts/rate_vs_index_size.py \
    --scylla     "$R/scylla/samples/*/c4-*.csv" \
    --opensearch "$R/opensearch/samples/*/c4-b*-*.csv" \
    --output     "$R/build-rate-vs-index-size.png" \
    --table      "$R/build-rate-vs-index-size.csv" \
    --title      "Build rate as the index grows — real engines, laptop" \
    --subtitle   "$RUN_ID · c=4 · PROVING RUN · NO NUMBER HERE IS A MEASUREMENT"
```

**Four bold lines — one per index build at `c=4`, and the slice is the ladder's
BOTTOM rung on purpose.** A full run slices the top, where the engine is working
hardest. A proving run slices the bottom, because it is the rung whose build
lasts longest, and a build needs **more than three readings** to be drawn at
all. At `c=16 batch=1024` fifty thousand documents are gone in well under a
second and the line would be skipped by name; at `c=4` the same build has time
to leave a shape. The slice is chosen by the glob, and it has to name a rung the
ladder actually has.

**The chart must print `(4 series)`.** Three would mean one build was skipped —
read the `skipped ...` lines it prints underneath, which name the file and the
reason.

It answers what chart 1 cannot:

- **Was the plateau a plateau, or an average of two halves?** A build that ran
  at full speed and then stalled reports the same mean as one that ran evenly.
- **Where did the client stop?** The tick on each line marks it. Everything to
  its right was indexed after the last insert landed — the drain, which is where
  a build that looked fast can still be unfinished.
- **Did the build slow down as the index grew?** That is the whole question, and
  it is the one an average cannot be asked.

The footer states that x means the same thing on both halves — documents this
build made searchable — and that **y does not**. Compare the shape of a line
against its own budget, never one engine's height against the other's.

## Then write it down

```bash
cat > "$R/README.md" << EOF
# Pipeline proving run, both engines, $(date -u +%Y-%m-%d)

**A PROVING RUN. No number in this directory is a measurement** — not even a
preliminary one. Produced by \`bench/build-rate/HARNESS-LOCAL-RUNBOOK.md\` on a
rehearsal budget of 50,000 documents a level (10,000 at batch=1), N=1, under the
laptop-simulation caps recorded in \`env/docker-env.txt\`, on a shared box, with
the client and the engine on the same host.

What it establishes is that the machinery runs end to end: both stacks up, both
binaries reaching them, reset-per-level rebuilding an empty index on each side,
the index columns populated, and both charts rendering from these files alone.

Run \`$RUN_ID\`. Ladder 4,8,16 · N=1 · ScyllaDB then OpenSearch at batch
1/128/1024, never both up at once.

| Chart | What it shows |
|---|---|
| \`build-rate-vs-concurrency.png\` | docs/s against concurrency; solid = submitted, dashed = indexed, one colour per configuration |
| \`build-rate-vs-index-size.png\` | docs/s against documents in the index, one line per build at c=4 |
EOF
```

Fill in, by hand, underneath: whether Gate A passed clean, which levels Gate B
flagged as short, which builds the growth chart skipped and why, and any row
that came back `index_settled=false`. Then hand the user the absolute path of
`$R`. **A results directory nobody can find is the same as no results.**

---

# What a pass looks like

The run succeeded if **all five** of these hold. Report them as a list, by
number, and do not describe the run as working if one of them is missing.

| # | Check | Where it comes from |
|---|---|---|
| 1 | Both stacks came up and both `make *-wait` returned | Phases 1 and B1 |
| 2 | **Gate A printed nothing but row counts, and every count is 3** | Phase 4 and B3 |
| 3 | Chart 1 printed **`(8 lines, 24 points)`** | the chart section |
| 4 | Chart 2 printed **`(4 series)`** and skipped nothing | the chart section |
| 5 | Both PNGs and both CSVs exist in `$R` with both engines down | `ls "$R"` |

Gate B flagging short points does **not** fail the run. That is the expected
outcome on this budget, and it is what check 3's point count already proves was
measured anyway.

If any of the five fails, the fix is almost always one of the traps above —
`--index-watch` missing, a poll interval too coarse for a build this small, a
plain `down` leaving yesterday's data, or both engines up at once.

---

# Then the real run

When the five checks pass, the same document runs the real thing. **Change one
table and two globs; nothing else in this runbook moves.**

| | Proving run | Real run |
|---|---|---|
| `LADDER` | `4,8,16` | `4,8,16,32` |
| `BATCHES` | `1 128 1024` | `1 128 512 1024` |
| `REPS` | `1` | `2` |
| `MAX_DOCS` (scyllarate) | `50000` | `0` — the whole corpus |
| `MAX_DOCS` (osrate) | `50000`, `10000` at batch=1 | `0`, `60000` at batch=1 |
| Chart 2 slice | `c4-*` — the longest-running rung | `c32-*` — the top rung, where the engine works hardest |
| Index builds | 12 | 40 |
| Wall | 10–15 min | 50–75 min |

```bash
REPS=2 LADDER=4,8,16,32 MAX_DOCS=0        "$R/scripts/run-scylla-arm.sh"
REPS=2 LADDER=4,8,16,32 BATCHES="1 128 512 1024" "$R/scripts/run-os-arm.sh"
```

The osrate script picks its own per-batch budget, so only `BATCHES` and `REPS`
have to change there — but **read its `MAX_DOCS` case first**: it hard-codes the
rehearsal numbers, and a real run needs `10000` to become `60000` and `50000` to
become `0`.

Three things become true on the real run that are not true here, and each turns
a "fine" into a failure:

- **Gate B must be empty.** Short points are no longer expected; a level under
  three seconds is a level to re-run with a bigger budget.
- **Chart 2 slices the top rung**, where a build is long enough to have a shape
  even at `batch=1024` — which is the rung whose behaviour anybody actually
  wants to see.
- **N=2 makes the spread visible.** Chart 1 gains min..max bars and chart 2
  gains thin repetition lines behind each bold median; with N=1 both collapse to
  a single line that cannot show disagreement.

Even then the numbers stay unquotable, for the three reasons at the top of this
file that the budget has nothing to do with.
