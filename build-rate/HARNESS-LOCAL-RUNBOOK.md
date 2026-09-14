# Harness-on-laptop runbook — what the two engines actually build, locally

**Hand this file to Claude Code as the instruction and it runs the whole
campaign: brings up one engine at a time in Docker, builds both harnesses,
measures each engine's real index build across a concurrency ladder — OpenSearch
once per batch size — tears each stack down, and renders the two charts.** It is
self-contained; every script it needs is inline below. Nothing else in `bench/`
has to be read first.

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

The subject here is **the engines**. Both binaries push at a real ScyllaDB +
vector-store and a real OpenSearch, with the index reset to empty before every
level, so what comes back is a genuine build rate — and every level's
`index_docs_per_s` is the engine's, not the client's.

**And none of it is quotable.** Three separate reasons, each sufficient:

- `docker/.env`'s own header says every sizing value in it is a
  laptop-simulation value and that nothing produced under them may be quoted.
- This box is shared with whatever else is running on it. The engine containers
  are pinned to `ENGINE_CPUSET` and the loader to `GEN_CPUSET`, which bounds the
  contention but does not remove it.
- One box runs the client and the engine. On the fleet they are separate hosts
  with a measured 0.142 ms between them.

So: real engine behaviour, real shapes, **preliminary numbers**. Say that
wherever a number from here is repeated, and never let one into the deck.

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
│   ├── points/     scylla-rep1.csv  scylla-rep2.csv
│   ├── samples/    scylla-rep1/c4-1.csv c8-1.csv c16-1.csv c32-1.csv  (and rep2)
│   └── logs/       scylla-rep1.stderr.tsv  ...
├── opensearch/
│   ├── points/     os-b1-rep1.csv  os-b128-rep1.csv  ...  (4 batches x 2 reps)
│   ├── samples/    os-b512-rep1/c4-b512-1.csv ...
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
| Concurrency ladder | `4,8,16,32` — four levels, **no throwaway warm-up row**; see below |
| OpenSearch batch sizes | `1, 128, 512, 1024` — each a whole ladder |
| Repetitions | **N=2** |
| Corpus | `data/corpus.jsonl`, frozen simplewiki, **270,269 documents, ~1,688 B mean** |
| `--max-docs` | `0` (whole corpus) everywhere **except** `osrate --batch-size 1`, which gets `60000` |
| Index builds | 4 levels x 2 reps x 5 configurations = **40 builds**, every one from an empty index |
| Estimated wall | 50–75 min including bring-up, teardown and both renders |

### The shared concurrency grid — do not vary it per arm

**Every series in both parts is measured on the same x values:**

```
4   8   16   32
```

The deliverable chart puts concurrency on x and every engine-and-batch
combination on it as a series. Series that do not share x values cannot be drawn
on one axis, so a ladder tailored per arm silently destroys the chart. If a
level has to be added, add it to **every** arm. Powers of two, because x is
drawn on a log2 axis.

### The ladder carries no warm-up row, and the charts are told so

`4,8,16,32` is four levels and four x values. It is **not** the
`8,8,16,32,64` idiom the AWS runbook uses, where the first rung is repeated so
the leading one can be thrown away — and both renderers drop the first data row
of every CSV by default, which on this ladder would delete `c=4` outright rather
than delete a throwaway.

So the chart command passes **`--keep-warmup`**, and that has a cost worth
stating rather than burying: `c=4` is now measured on a cold process — page
cache, connection pool and JIT all unwarmed — and is the one point on the chart
carrying that. Two ways to deal with it, in order of preference:

- **Read `c=4` as a soft point.** With N=2 and everything here already
  preliminary, a cold first rung is within the noise the run reports anyway.
- **Put the throwaway back** with `LADDER=4,4,8,16,32` and drop `--keep-warmup`
  from the chart command. That is five levels again, restoring the AWS idiom and
  about a fifth of the wall time.

What must not happen is the ladder keeping four rungs while the chart keeps its
default: that silently yields a three-point x axis (`8 16 32`) and a `c=4` that
was measured and then discarded.

**One budget, not a per-batch table.** The whole corpus serves every level:
it puts every series on the same x range on the index-size chart, and at 270,269
documents no level falls under the three-second floor that makes a point "not a
measurement". `osrate --batch-size 1` is the one exception — at roughly 800
docs/s at `c=8` the whole corpus is ~5.6 minutes for one level, which alone
would double the campaign. It is cut to 60,000 documents, and its shorter line
on the second chart is footnoted rather than hidden.

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
LADDER=${LADDER:-4,8,16,32}
REPS=${REPS:-2}
GEN_CPUSET=${GEN_CPUSET:-12-19}
stamp() { while IFS= read -r line; do printf '%s\t%s\n' "$(date -u +%H:%M:%S)" "$line"; done; }

for rep in $(seq 1 "$REPS"); do
  echo "=== scylla rep $rep/$REPS  ladder=$LADDER ==="
  taskset -c "$GEN_CPUSET" "$BIN" \
      --corpus "$CORPUS" --concurrency "$LADDER" \
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
- **`--vs-settle-timeout 600`.** 270,269 documents take well over the default
  120 s to finish indexing here.
- **`--tokio-workers 8`.** The eight cores `GEN_CPUSET` actually grants. Left at
  every core the runtime spawns 22 threads for 8 cores' worth of CPU.

Run it in the background or with a generous timeout; do not poll it every few
seconds.

## Phase 4 — collect, verify, and only then tear down

Everything already writes into `$R`, so there is nothing to copy. Verify before
the stack goes down, while it can still be re-run.

**Verification gate — all of it must pass:**

```bash
# every ladder CSV has one row per ladder entry (5), plus the header
for f in "$R"/scylla/points/*.csv; do
  n=$(grep -vc '^#' "$f"); echo "$f rows=$((n-1))"; done

# zero failed inserts anywhere (col 3 is errors)
awk -F, '!/^#/ && NR>1 && $3+0>0 {print FILENAME": errors="$3}' "$R"/scylla/points/*.csv

# the index was watched on every row -- col 12 is index_docs_per_s, and a blank
# there is half of chart 1 missing for this arm
awk -F, '!/^#/ && $1!="concurrency" && $12=="" {print FILENAME" line "FNR": index unwatched"}' \
    "$R"/scylla/points/*.csv

# every level ran long enough to be a measurement (col 4 is wall_s)
awk -F, '!/^#/ && $1!="concurrency" && $4+0<3 {print FILENAME": c="$1" wall="$4"s -- NOT a measurement"}' \
    "$R"/scylla/points/*.csv

# every level left a series with more than 3 readings, or chart 2 drops it
for d in "$R"/scylla/samples/*/; do for f in "$d"c*.csv; do
  n=$(grep -vc '^#' "$f"); [ "$((n-1))" -le 3 ] && echo "$f only $((n-1)) readings"; done; done

# the settle finished rather than timing out (col 15 is index_settled)
awk -F, '!/^#/ && $1!="concurrency" && $15!="true" {print FILENAME": c="$1" NOT settled -- rate is a floor"}' \
    "$R"/scylla/points/*.csv
```

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
LADDER=${LADDER:-4,8,16,32}
REPS=${REPS:-2}
BATCHES=${BATCHES:-1 128 512 1024}
GEN_CPUSET=${GEN_CPUSET:-12-19}
stamp() { while IFS= read -r line; do printf '%s\t%s\n' "$(date -u +%H:%M:%S)" "$line"; done; }

for b in $BATCHES; do
  # batch=1 is ~1 document per request and the whole corpus would take minutes
  # per level. Every other level gets the whole corpus so their index-size lines
  # cover the same x range.
  if [ "$b" = "1" ]; then MAX_DOCS=60000; else MAX_DOCS=0; fi
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
  `queue_depth x concurrency x batch_size`. At `c=32 batch=1024` the default
  holds 327,680 documents — over half a gigabyte of corpus in the loader's own
  heap, on a box that already refused to run both engines at once.
- **`--refresh-interval 1s` explicitly**, so the header records it and the
  `index_lag_docs` floor is a number rather than a guess.
- **Do not tailor the ladder per batch level.** A taller ladder for the small
  batches would reach their knee and take them off the shared x grid, and chart
  1 can no longer be drawn. If `batch=1` is still rising at `c=32`, that is a
  result to state — "unresolved above 32" — not a reason to give it its own x
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
9. **Quoting anything.** See the top of this file. Three independent reasons.

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
    --subtitle   "$RUN_ID · simplewiki 270,269 docs · N=2 · PRELIMINARY, NOT QUOTABLE"
```

Ten lines off five configurations:

| Colour | Solid | Dashed |
|---|---|---|
| `#2b6cb0` | `scyllarate CQL 1 doc/op` — CQL inserts accepted | the vector-store's FTS build |
| ramp 1–4 | `osrate batch=1 / 128 / 512 / 1024` — `_bulk` documents accepted | documents made searchable |

The chart should print `(10 lines, 40 points)`. Fewer lines means a config did
not produce one of its two families — go back to the gate.

**Read it in this order.** First the gap inside each pair: that is how far the
index fell behind the client, and it is the finding. Then the spacing between
the batch series: that is what bulking bought, and if `128`, `512` and `1024`
lie on top of each other, bulking buys nothing past ~128 on this box. Then the
short-point footer, which names every level under three seconds — those are not
measurements, raise their `--max-docs` and re-run them.

## Chart 2 — X is the index itself, at `c=32`

```bash
.venv/bin/python3 build-rate/charts/rate_vs_index_size.py \
    --scylla     "$R/scylla/samples/*/c32-*.csv" \
    --opensearch "$R/opensearch/samples/*/c32-b*-*.csv" \
    --output     "$R/build-rate-vs-index-size.png" \
    --table      "$R/build-rate-vs-index-size.csv" \
    --title      "Build rate as the index grows — real engines, laptop" \
    --subtitle   "$RUN_ID · c=32 · N=2 · PRELIMINARY, NOT QUOTABLE"
```

Five bold lines — one per index build at `c=32`, the ladder's top rung — each
with its two repetitions thin behind it. The slice is chosen by the glob, and
it has to name a rung the ladder actually has; widening it to `c*` puts
every level of every ladder on one axis, which is complete and unreadable.

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
# Build rate on the laptop, both engines, $(date -u +%Y-%m-%d)

**PRELIMINARY — not quotable, and not a fleet measurement.** Produced by
\`bench/build-rate/HARNESS-LOCAL-RUNBOOK.md\` under the laptop-simulation caps
recorded in \`env/docker-env.txt\`, on a shared box, with the client and the
engine on the same host.

Run \`$RUN_ID\`. Ladder 4,8,16,32 · N=2 · corpus 270,269 docs ·
ScyllaDB then OpenSearch at batch 1/128/512/1024, never both up at once.

| Chart | What it shows |
|---|---|
| \`build-rate-vs-concurrency.png\` | docs/s against concurrency; solid = submitted, dashed = indexed, one colour per configuration |
| \`build-rate-vs-index-size.png\` | docs/s against documents in the index, one line per build at c=32 |
EOF
```

Fill in, by hand, underneath: which rows came back `index_settled=false` (their
rates are floors, written `≥`), which levels the charts named as short or
skipped, and what the two images actually show. Then hand the user the absolute
path of `$R`. **A results directory nobody can find is the same as no results.**
