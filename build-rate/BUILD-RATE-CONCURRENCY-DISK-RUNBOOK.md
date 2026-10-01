# Build-rate runbook — three arms, concurrency ladder

**Hand this file to Claude Code as the instruction and it runs the whole
session**: re-enters the (already-running) fleet, brings up ScyllaDB +
vector-store with the index on disk behind tantivy's default writer buffer,
measures a concurrency ladder, pulls the data home, tears that stack down,
brings the same pair up again with the index on disk behind a 376 MB buffer,
measures the same ladder, pulls that data home, tears down, brings up
OpenSearch with its index on disk, measures the same ladder again, pulls that
data home too, stops the boxes. Every script it needs is inline.

It draws its engine configuration from
[`INDEX-RATE-SCYLLA-RUNBOOK.md`](INDEX-RATE-SCYLLA-RUNBOOK.md)'s **R8**
(`scylla-buf376-disk`) arm and from **R1**'s (`scylla-buf15`) writer-buffer
knob, and
[`INDEX-RATE-OPENSEARCH-RUNBOOK.md`](INDEX-RATE-OPENSEARCH-RUNBOOK.md)'s
**`os-disk-refresh1`** arm, and its ladder mechanics from
[`HARNESS-AWS-RUNBOOK.md`](HARNESS-AWS-RUNBOOK.md)'s concurrency-ladder run
scripts. It is not a fourth campaign document — where it and those three
disagree on an engine knob or a gate, they are right and this file has
drifted.

| | this runbook | the campaign runbooks it draws from |
|---|---|---|
| Axis | **concurrency**, declared: `1,2,4,8,16,32,64,128` | offered rate (`--target-rate`), a 5-rung grid |
| Arms | 3 — ScyllaDB disk @15 MB buffer, ScyllaDB disk @376 MB buffer, OpenSearch disk | 5 across both files (RAM and disk arms, calibration passes) |
| Reps | **N=1** | N=3 |
| Document budget | one flat `--max-docs`, calibrated once on the fleet and shared by all three arms | a rung-scaled or campaign-wide constant, already settled |
| Session | one sitting, all three engines | R1/R2/R8 alone is ~3.4 h; the OpenSearch half is separate again |
| Probe slicer | **works out of the box** — `ftsbench/probe_windows.py` keys a window on `(sweep, concurrency, rep)`, which a concurrency ladder gives it directly | **blocked on the rate ladder** for the ScyllaDB half (see that file's "Blocker on the critical path") — the reason this runbook uses concurrency at all |

**Why all three arms share one budget.** Every arm here is disk-backed, so
each one is read against the other two: 5a → 5b isolates writer-buffer size
with the index location held constant, and 5b → 5c is the cross-engine pair.
A shared `--max-docs` is what keeps those reads comparable in wall-clock
terms. Calibrate once (Phase 4) and do not re-derive it per arm — an arm whose
`c=1` rung lands outside the 30–90 s band under the shared budget is a
*recorded property of that arm*, not a reason to re-budget it.

**Why concurrency here and not the rate ladder the campaign moved to.** The
campaign axis change was for cross-engine comparability at a shared documents
per-second grid — not needed here, since this is two single-engine passes, not
a merged chart. Running the concurrency ladder instead sidesteps the ScyllaDB
probe-slicer blocker entirely and is materially simpler to execute in one
sitting.

## What this does not give you

No cross-engine chart, no PRELIMINARY figure fit to quote in the talk. This is
a smaller, faster pass for a build-rate-against-concurrency read, one engine
(or one config) at a time. If a cross-engine rate-ladder chart is wanted
later, run the two campaign runbooks instead — this file's data does not
merge with theirs (different axis, different `latency_basis`). Nothing here
measures a RAM-backed index at all; if the where-the-index-lives read is what
is wanted, that is R2 → R8 in `INDEX-RATE-SCYLLA-RUNBOOK.md`.

## Assumptions

**The AWS fleet is already running.** This file has no "start the instances"
step. If the boxes are stopped, run
[`INDEX-RATE-SCYLLA-RUNBOOK.md`](INDEX-RATE-SCYLLA-RUNBOOK.md) Phase 1 first,
then come back here.

**`/mnt/nvme` may already be mounted and populated from a prior session on
these same boxes.** The re-entry steps below are the idempotent parts only
(SSH re-point, build, corpus check); they do **not** blindly reformat the
instance store. Check before mounting — reformatting a live corpus or a live
build is not something this file will do for you.

## The fleet

| Alias | Instance | Role |
|---|---|---|
| `fts-harness` | `i-08d8d2505e16683f7` · `k-nowacki-fts-benchmark-harness` | runs `scyllarate`/`osrate`, holds the corpus |
| `fts-sut` | `i-0e3e4b6b02e654b7f` · `k-nowacki-fts-benchmark-sut` | runs the engine under measurement |

Both `i8g.2xlarge` (8 vCPU Graviton4, 61 GiB, aarch64), `eu-north-1b`, Amazon
Linux 2023, user `ec2-user`, key `~/.ssh/KarolNowackiAws.pem`. There is no AWS
CLI credential on this laptop — the console in Chrome is the only way to stop
the boxes at the end.

## Phase 0 — the results directory, on the laptop

```bash
export RUN_ID="build-rate-concurrency-disk-$(date -u +%Y-%m-%dT%H%MZ)"
export R="$HOME/Projects/Scylla/p99/bench/results/$RUN_ID"
mkdir -p "$R"/{env,scripts,sut}
mkdir -p "$R"/scylla-buf15-disk/{points,samples,logs,probe}
mkdir -p "$R"/scylla/{points,samples,logs,probe}
mkdir -p "$R"/opensearch/{points,samples,logs,probe}
printf '%s\n' "$RUN_ID" > "$R/RUN_ID"
echo "results -> $R"
```

If the shell is lost, recover with
`export R="$(readlink -f bench/results/build-rate-concurrency-disk-*)"` (there
should be exactly one match if this is the only session of the day).

## Phase 1 — fleet re-entry

### 1a. SSH and the private IPs

Public IPs are reassigned on every start; private IPs are not. If SSH already
works (the boxes were never stopped this session), skip straight to 1b.

```bash
sed -i '/^Host fts-harness$/,/^$/ s/^    HostName .*/    HostName <new-harness-ip>/' ~/.ssh/config
sed -i '/^Host fts-sut$/,/^$/     s/^    HostName .*/    HostName <new-sut-ip>/'     ~/.ssh/config
ssh -o StrictHostKeyChecking=accept-new fts-harness true
ssh -o StrictHostKeyChecking=accept-new fts-sut     true
ssh fts-harness hostname -I     # expect 172.31.38.237
ssh fts-sut     hostname -I     # expect 172.31.47.166  <- SUT, used below
```

### 1b. The instance store — check before touching it

```bash
ssh fts-harness 'mountpoint -q /mnt/nvme && echo mounted || echo not-mounted'
ssh fts-sut     'mountpoint -q /mnt/nvme && echo mounted || echo not-mounted'
```

**Only if `not-mounted`** on a box, format and mount it — this destroys
whatever the instance store held, which is correct the first time after a stop
and wrong on a box that is already up:

```bash
for h in fts-harness fts-sut; do
  ssh $h 'set -e
    sudo mkfs.xfs -f -q /dev/nvme0n1
    sudo mkdir -p /mnt/nvme && sudo mount /dev/nvme0n1 /mnt/nvme
    sudo chown ec2-user:ec2-user /mnt/nvme
    sudo systemctl restart docker'
done
```

### 1c. Clocks

```bash
ssh fts-sut date +%s.%N; ssh fts-harness date +%s.%N
```

Both need to agree to well under a second — the resource probe join refuses a
skew above one tick. Record it in `$R/env/clock-skew.txt`.

### 1d. The vector-store image — needed for the ScyllaDB half only

`VS_FTS_INDEX_DIR` (disk-backed index) only exists from commit `94a23ef2`
onward. If this image is already loaded on `fts-sut` from an earlier session
today, skip this:

```bash
ssh fts-sut docker images scylladb/vector-store   # look for 1.10.0-45-g94a23ef2-arm64
```

If it is not there:

```bash
ssh fts-harness 'set -e
  mkdir -p /mnt/nvme/build && cd /mnt/nvme/build
  rm -rf vector-store
  git clone -b p99-fts-ingest-optimization \
      https://github.com/knowack1/vector-store.git
  cd vector-store
  git fetch --tags https://github.com/scylladb/vector-store.git
  git rev-parse HEAD                  # expect 94a23ef2c9ff...
  TARGETARCH=arm64 ./scripts/run-with-release-toolchain cargo build --release
  ./scripts/build-dockers arm64'

ssh fts-harness 'docker save scylladb/vector-store:1.10.0-45-g94a23ef2-arm64' \
    | ssh fts-sut docker load
ssh fts-sut docker images scylladb/vector-store
```

~15 min, billed once. Run this in a shell that has **not** sourced
`tools/fleet_env.sh` — with `DOCKER_HOST` pointed at the SUT the build and
image would land on the wrong daemon.

### 1e. The bench checkout, venv, and both harness binaries

```bash
cd ~/Projects/Scylla/p99/bench && tar czf - --exclude=__pycache__ --exclude=target \
    ftsbench tools docker Makefile build-rate \
  | ssh fts-harness 'mkdir -p /mnt/nvme/work/bench && tar xzf - -C /mnt/nvme/work/bench'

ssh fts-harness 'set -e
  echo "export BENCH=/mnt/nvme/work/bench" >> ~/.bashrc
  cd /mnt/nvme/work/bench
  python3.12 -m venv /mnt/nvme/work/venv && ln -sfn /mnt/nvme/work/venv ~/venv
  ln -sfn /mnt/nvme/work/venv .venv
  .venv/bin/pip install -q -r requirements.txt
  .venv/bin/python3 -c "import ftsbench.runmeta; print(\"ok\")"'

# ScyllaDB binary
ssh fts-harness 'cd /mnt/nvme/work/bench/build-rate/scylla && . "$HOME/.cargo/env" \
  && CARGO_TARGET_DIR=/mnt/nvme/work/target cargo build --release --locked'

# OpenSearch binary -- its own target dir; each crate's build.rs stamps its
# own Cargo.lock's driver version into the CSV header.
ssh fts-harness 'cd /mnt/nvme/work/bench/build-rate/opensearch && . "$HOME/.cargo/env" \
  && CARGO_TARGET_DIR=/mnt/nvme/work/target-os cargo build --release --locked'

git -C ~/Projects/Scylla/p99/bench log -1 --format='%H %s' > "$R/env/bench-commit.txt"
git -C ~/Projects/Scylla/p99/bench status --short build-rate >> "$R/env/bench-commit.txt"
```

**Do not rebuild once an arm has run.** Freeze the binaries here and reuse
them for both arms.

## Phase 2 — the corpus, on the harness

```bash
ssh fts-harness 'set -e
  mkdir -p /mnt/nvme/data
  pzstd -d -p 8 -f -o /mnt/nvme/data/corpus.jsonl ~/corpus.jsonl.zst
  sha256sum /mnt/nvme/data/corpus.jsonl'
# expect 1700bb6c9b2652cf7b248e8caff7bfecc54fd2376e9a75e43379aaa79c50c432
```

That sha256 is `../FREEZE.md`'s frozen enwiki corpus (8,967,625 documents) —
real text, which is required here: BM25 term statistics, segment merges and
the analyzer all depend on it. **Do not use a synthetic corpus** — that is
right for a null-sink pacing run and wrong for a real index build.

If the archive is not on the harness root volume (a replaced box), re-stage
per `INDEX-RATE-SCYLLA-RUNBOOK.md` Phase 3 (~36 min) before continuing.

## Phase 3 — the ladder scripts

**ScyllaDB — reuses `INDEX-RATE-SCYLLA-RUNBOOK.md` Phase 4's `run-arm.sh`**,
which already supports a plain concurrency ladder when `RATES` is left unset:

```bash
ssh fts-harness 'cat > ~/run-arm.sh << "SCRIPT"
#!/bin/bash
set -u
ARM="$1"; shift
REPS="${REPS:-1}"
LADDER="${LADDER:-1,2,4,8,16,32,64,128}"
RATES="${RATES:-}"
CAP="${CAP:-512}"
CORPUS="${CORPUS:-/mnt/nvme/data/corpus.jsonl}"
MAX_DOCS="${MAX_DOCS:-200000}"
SINK="${SINK:-172.31.47.166}"
PORT="${PORT:-9042}"
VS_PORT="${VS_PORT:-16080}"
OUT_DIR="${OUT_DIR:-/mnt/nvme/work/results}"
SAMPLES_DIR="${SAMPLES_DIR:-/mnt/nvme/work/samples}"
BIN=/mnt/nvme/work/target/release/scyllarate

if [ -n "$RATES" ]; then
    AXIS=(--target-rate "$RATES" --concurrency "$CAP")
else
    AXIS=(--concurrency "$LADDER")
fi

mkdir -p "$OUT_DIR" "$SAMPLES_DIR"
WINDOWS="$OUT_DIR/run-windows.tsv"
[ -f "$WINDOWS" ] || printf "arm\trep\tstart_epoch\tend_epoch\texit_code\tmax_docs\tcsv\n" > "$WINDOWS"
stamp() { awk "{ printf \"%d\t%s\n\", systime(), \$0; fflush() }"; }

for rep in $(seq 1 "$REPS"); do
    csv="$OUT_DIR/$ARM-rep$rep.csv"
    log="$OUT_DIR/$ARM-rep$rep.stderr.tsv"
    echo "######## arm=$ARM rep=$rep ladder=$LADDER $(date -u +%H:%M:%S)"
    start=$(date +%s)
    "$BIN" --corpus "$CORPUS" "${AXIS[@]}" --max-docs "$MAX_DOCS" \
           --hosts "$SINK" --port "$PORT" \
           --vs-url "http://$SINK:$VS_PORT" \
           --out "$csv" --samples-dir "$SAMPLES_DIR/$ARM-rep$rep" \
           "$@" 2>&1 | stamp > "$log"
    code=${PIPESTATUS[0]}
    printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
        "$ARM" "$rep" "$start" "$(date +%s)" "$code" "$MAX_DOCS" "$csv" >> "$WINDOWS"
    grep -E "docs in" "$log" | tail -14
done
SCRIPT
chmod +x ~/run-arm.sh'
```

**`VS_PORT=16080` is mandatory** — the real vector-store's status endpoint,
not the null-sink's `+7000` convention. `--index-watch` is on by default on
`scyllarate`, which is what gives the index-build reading this runbook is for.

**OpenSearch — reuses `HARNESS-AWS-RUNBOOK.md` Part B's concurrency-ladder
`run-os-arm.sh`**, pointed at the real engine (`RESET_FLAGS` empty, so the
analyzer check runs — the mock-only default is `--no-analyzer-check`):

```bash
ssh fts-harness 'cat > ~/run-os-arm.sh << "SCRIPT"
#!/bin/bash
set -u
ARM="$1"; shift
REPS="${REPS:-1}"
LADDER="${LADDER:-1,2,4,8,16,32,64,128}"
BATCH="${BATCH:-1024}"
CORPUS="${CORPUS:-/mnt/nvme/data/corpus.jsonl}"
MAX_DOCS="${MAX_DOCS:-200000}"
SINK_URL="${SINK_URL:-http://172.31.47.166:9200}"
INDEX="${INDEX:-wiki-articles}"
RESET_FLAGS="${RESET_FLAGS:-}"
OUT_DIR="${OUT_DIR:-/mnt/nvme/work/results-os}"
SAMPLES_DIR="${SAMPLES_DIR:-/mnt/nvme/work/samples-os}"
BIN=/mnt/nvme/work/target-os/release/osrate

mkdir -p "$OUT_DIR" "$SAMPLES_DIR"
WINDOWS="$OUT_DIR/os-windows.tsv"
[ -f "$WINDOWS" ] || printf "arm\tbatch\trep\tstart_epoch\tend_epoch\texit_code\tladder\tmax_docs\tcsv\n" > "$WINDOWS"
stamp() { awk "{ printf \"%d\t%s\n\", systime(), \$0; fflush() }"; }

for rep in $(seq 1 "$REPS"); do
    csv="$OUT_DIR/$ARM-b$BATCH-rep$rep.csv"
    log="$OUT_DIR/$ARM-b$BATCH-rep$rep.stderr.tsv"
    echo "######## arm=$ARM batch=$BATCH rep=$rep ladder=$LADDER $(date -u +%H:%M:%S)"
    start=$(date +%s)
    "$BIN" --corpus "$CORPUS" --concurrency "$LADDER" --batch-size "$BATCH" \
           --max-docs "$MAX_DOCS" --samples-dir "$SAMPLES_DIR/$ARM-rep$rep" \
           --url "$SINK_URL" --index "$INDEX" --out "$csv" $RESET_FLAGS "$@" 2>&1 | stamp > "$log"
    code=${PIPESTATUS[0]}
    printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
        "$ARM" "$BATCH" "$rep" "$start" "$(date +%s)" "$code" "$LADDER" "$MAX_DOCS" \
        "$csv" >> "$WINDOWS"
    grep -E "docs in" "$log" | tail -14
done
SCRIPT
chmod +x ~/run-os-arm.sh'
```

**`--batch-size 1024`, always** — matches `os-disk-refresh1`'s own wire batch
in `INDEX-RATE-OPENSEARCH-RUNBOOK.md`. It is **not** the shape that matches the
ScyllaDB side (one prepared `INSERT` per request there, 1,024 documents per
request here), so `--concurrency` means a different number of documents in
flight per engine on this file's chart too — same asymmetry the campaign
records as `in_flight_peak` rather than pretending the x axis is shared. Not a
problem here: this file draws no cross-engine line.

## Phase 4 — size the shared `--max-docs`, on the fleet

**One flat budget covers every rung of the ladder on all three arms**, so `c=1`
(the slowest level) sets the session length. Do not guess this number —
calibrate it, exactly as `HARNESS-AWS-RUNBOOK.md` Part A/B do, because a real
engine's document rate at `c=1` is not known until it is measured. Target
30–90 s at `c=1` — long enough to be a real measurement, short enough that an
8-level ladder does not eat the session.

Bring up the ScyllaDB stack in Phase 5b's config — the 376 MB buffer, the
faster of the two ScyllaDB arms:

```bash
ssh fts-harness 'set -eu
  cd $BENCH && . tools/fleet_env.sh
  VS_FTS_WRITER_MEMORY_MB=376 VS_FTS_COMMIT_THRESHOLD=0 \
  VS_FTS_COMMIT_INTERVAL=1s VS_FTS_METRICS_INTERVAL=1s \
  VS_FTS_INDEX_DIR=/var/lib/vector-store/fts \
  make scylla-up && make scylla-wait
  make scylla-schema && make scylla-index && make scylla-serving'
```

`scylla-up` does not create the keyspace — the three `make` targets on the
last line do, and a calibration run against a stack without them fails on a
missing keyspace, not on anything to do with the budget. Then from the
harness:

```bash
ssh fts-harness 'REPS=1 LADDER=1 MAX_DOCS=20000 ~/run-arm.sh calib-scylla'
ssh fts-harness 'grep "docs in" /mnt/nvme/work/results/calib-scylla-rep1.stderr.tsv'
```

Read the wall time of that one `c=1` level and scale `MAX_DOCS` linearly to
land in the 30–90 s band; call the result `<budget>`. **Use the same
`<budget>` for the Phase 5a and OpenSearch arms too** — a shared budget is
what keeps the three disk arms comparable in wall-clock terms, and
re-calibrating per arm would defeat that. Delete the calibration output; it is
not campaign data.

**Calibrating on the 376 MB arm is deliberate.** It is the faster ScyllaDB
config, so a budget sized to land *it* in the band leaves Phase 5a's smaller
buffer at or above the 30 s floor rather than under it. If 5a's `c=1` runs
long past 90 s, that is the buffer-size delta this file exists to measure —
record it and keep the budget.

**Tear the calibration stack down before Phase 5a** —
`make scylla-reset` — it carries the 376 MB buffer and Phase 5a must come up
without it:

```bash
ssh fts-harness 'cd $BENCH && . tools/fleet_env.sh && make scylla-reset'
```

## Phase 5a — the ScyllaDB arm, disk-backed index, default tantivy buffer

`VS_FTS_WRITER_MEMORY_MB` **unset** (tantivy's 15 MB/thread floor),
`VS_FTS_COMMIT_THRESHOLD=0`, `VS_FTS_COMMIT_INTERVAL=1s`,
`VS_FTS_METRICS_INTERVAL=1s`, `VS_FTS_INDEX_DIR=/var/lib/vector-store/fts`.

**This is not a registered campaign arm.** It is
[`INDEX-RATE-SCYLLA-RUNBOOK.md`](INDEX-RATE-SCYLLA-RUNBOOK.md) **R1**'s writer
buffer on **R8**'s index location — R1 is RAM-backed and R8 carries the 376 MB
buffer, so neither name fits and neither `verify_arm` target applies. Call it
`scylla-buf15-disk` everywhere and do not relabel it R1 in any output. Against
Phase 5b it isolates writer-buffer size with the index held on disk.

Run this arm first, on `<budget>` from Phase 4 — it has no calibration of its
own.

### Bring the stack up

```bash
ssh fts-harness 'set -eu
  cd $BENCH && . tools/fleet_env.sh
  VS_FTS_COMMIT_THRESHOLD=0 \
  VS_FTS_COMMIT_INTERVAL=1s VS_FTS_METRICS_INTERVAL=1s \
  VS_FTS_INDEX_DIR=/var/lib/vector-store/fts \
  make scylla-up && make scylla-wait
  make scylla-schema && make scylla-index && make scylla-serving'
```

`VS_FTS_WRITER_MEMORY_MB` is absent from that line on purpose — it is the one
knob separating this arm from Phase 5b.

### Confirm the tuning line before spending a minute of load on it

```bash
ssh fts-harness 'docker logs fts-bench-vector-store 2>&1 | grep -E "ingest tuning|tantivy worker"'
```

Must read `index=disk:/var/lib/vector-store/fts` **and** `15 MB buffer per
thread`. Both halves are gates and each fails its own way: `index=ram` means
`VS_FTS_INDEX_DIR` did not take (or the image predates `94a23ef2`), `376 MB`
means `VS_FTS_WRITER_MEMORY_MB` leaked in from the environment and this arm
has silently become Phase 5b. Either way, unset/fix and recreate the stack
before running anything.

### Start the resource probe

```bash
ssh fts-harness "set -eu
  cd \$BENCH && . tools/fleet_env.sh
  mkdir -p /mnt/nvme/work/probe
  tools/sut_probe.sh start /mnt/nvme/work/probe/scylla-buf15-disk.jsonl \
      --engine scylladb \
      --containers fts-bench-scylla:scylladb \
      --containers fts-bench-vector-store:vector-store \
      --vs-url http://127.0.0.1:16080 --keyspace wiki --vs-index articles_body_fts \
      --interval 1 --duration 0 --label 'build-rate-conc-disk scylla-buf15-disk' \
      --corpus /mnt/nvme/data/corpus.jsonl"
```

### Run the ladder

```bash
ssh fts-harness "REPS=1 LADDER=1,2,4,8,16,32,64,128 MAX_DOCS=<budget> \
    VS_PORT=16080 OUT_DIR=/mnt/nvme/work/results-buf15 \
    SAMPLES_DIR=/mnt/nvme/work/samples-buf15 \
    ~/run-arm.sh scylla-buf15-disk --vs-interval 0.25"
```

### Close the arm out, on the harness, before `scylla-down`

```bash
ssh fts-harness "set -eu
  cd \$BENCH && . tools/fleet_env.sh
  L=/mnt/nvme/work/logs/scylla-buf15-disk; mkdir -p \$L
  docker logs fts-bench-vector-store > \$L/vector-store.log 2>&1
  cp docker/.env.sut \$L/env.sut
  tools/sut_probe.sh stop /mnt/nvme/work/probe/scylla-buf15-disk.jsonl
  .venv/bin/python3 -m ftsbench.probe_windows --arm scylla-buf15-disk \
      --probe /mnt/nvme/work/probe/scylla-buf15-disk.jsonl \
      --stderr '/mnt/nvme/work/results-buf15/scylla-buf15-disk-rep*.stderr.tsv' \
      --out-dir /mnt/nvme/work/probe/scylla-buf15-disk --memory-read anon+cache \
      --table \$L/resource-by-rung.csv
  .venv/bin/python3 -m ftsbench.verify_cpu_usage --data-dir /mnt/nvme/work/probe/scylla-buf15-disk \
      --containers fts-bench-scylla fts-bench-vector-store \
      --output-json \$L/cpu-utilisation.json"
```

`anon+cache`, matching Phase 5b — the index is file-backed here, so it is page
cache and not anonymous memory. Expect **8 windows** (1 rep × 8 levels).

**Confirmed by hand, not `verify_arm`** — `ftsbench/target.py` has no target
for a disk-backed index, and R1's `--scylladb-cdc-buf15` target describes a
RAM-backed rate-ladder arm, which this is not on either count. The two-part
tuning-line check above is the gate.

### Pull it home, immediately

```bash
d="$R/scylla-buf15-disk"
scp    'fts-harness:/mnt/nvme/work/results-buf15/scylla-buf15-disk-rep*'   "$d/points/"
scp -r 'fts-harness:/mnt/nvme/work/samples-buf15/scylla-buf15-disk-rep1/'* "$d/samples/"
scp -r 'fts-harness:/mnt/nvme/work/logs/scylla-buf15-disk/'*               "$d/logs/"
scp    'fts-harness:/mnt/nvme/work/probe/scylla-buf15-disk.jsonl'  "$R/sut/cpu-scylla-buf15-disk.jsonl"
scp -r 'fts-harness:/mnt/nvme/work/probe/scylla-buf15-disk/'*              "$d/probe/"
mv "$d"/points/*.tsv "$d/logs/" 2>/dev/null

ls "$d"/points/*.csv | wc -l                    # 1 (N=1)
awk -F, '!/^#/ && $1!="concurrency"' "$d"/points/*.csv | wc -l   # 8 (one row per level)
ls "$d"/probe/cpu-*.jsonl | wc -l                # 8
```

### Tear down before Phase 5b

```bash
ssh fts-harness 'cd $BENCH && . tools/fleet_env.sh && make scylla-reset'
```

**`scylla-reset` (`down -v`), not `scylla-down`** — Phase 5b needs a truly
empty index, and plain `down` keeps the named volume. Both this arm's tantivy
index and the ScyllaDB base table are on disk now, so both must go.

## Phase 5b — the ScyllaDB arm, disk-backed index

### Bring the stack up with R8's knobs

```bash
ssh fts-harness 'set -eu
  cd $BENCH && . tools/fleet_env.sh
  VS_FTS_WRITER_MEMORY_MB=376 VS_FTS_COMMIT_THRESHOLD=0 \
  VS_FTS_COMMIT_INTERVAL=1s VS_FTS_METRICS_INTERVAL=1s \
  VS_FTS_INDEX_DIR=/var/lib/vector-store/fts \
  make scylla-up && make scylla-wait'
```

### Confirm the tuning line before spending a minute of load on it

```bash
ssh fts-harness 'docker logs fts-bench-vector-store 2>&1 | grep -E "ingest tuning|tantivy worker"'
```

Must read `index=disk:/var/lib/vector-store/fts`, `376 MB buffer per thread`,
`commit_interval=1s`. If it does not, the image predates `94a23ef2` or a knob
was dropped — stop and fix before running anything.

### Start the resource probe

```bash
ssh fts-harness "set -eu
  cd \$BENCH && . tools/fleet_env.sh
  mkdir -p /mnt/nvme/work/probe
  tools/sut_probe.sh start /mnt/nvme/work/probe/scylla-disk.jsonl \
      --engine scylladb \
      --containers fts-bench-scylla:scylladb \
      --containers fts-bench-vector-store:vector-store \
      --vs-url http://127.0.0.1:16080 --keyspace wiki --vs-index articles_body_fts \
      --interval 1 --duration 0 --label 'build-rate-conc-disk scylla' \
      --corpus /mnt/nvme/data/corpus.jsonl"
```

### Run the ladder

```bash
ssh fts-harness "REPS=1 LADDER=1,2,4,8,16,32,64,128 MAX_DOCS=<budget> \
    VS_PORT=16080 OUT_DIR=/mnt/nvme/work/results \
    SAMPLES_DIR=/mnt/nvme/work/samples \
    ~/run-arm.sh scylla-disk --vs-interval 0.25"
```

Runs in the background or with a generous timeout; do not poll every few
seconds.

### Close the arm out, on the harness, before `scylla-down`

```bash
ssh fts-harness "set -eu
  cd \$BENCH && . tools/fleet_env.sh
  L=/mnt/nvme/work/logs/scylla-disk; mkdir -p \$L
  docker logs fts-bench-vector-store > \$L/vector-store.log 2>&1
  cp docker/.env.sut \$L/env.sut
  tools/sut_probe.sh stop /mnt/nvme/work/probe/scylla-disk.jsonl
  .venv/bin/python3 -m ftsbench.probe_windows --arm scylla-disk \
      --probe /mnt/nvme/work/probe/scylla-disk.jsonl \
      --stderr '/mnt/nvme/work/results/scylla-disk-rep*.stderr.tsv' \
      --out-dir /mnt/nvme/work/probe/scylla-disk --memory-read anon+cache \
      --table \$L/resource-by-rung.csv
  .venv/bin/python3 -m ftsbench.verify_cpu_usage --data-dir /mnt/nvme/work/probe/scylla-disk \
      --containers fts-bench-scylla fts-bench-vector-store \
      --output-json \$L/cpu-utilisation.json"
```

This is a **concurrency** ladder, so `probe_windows` runs to completion here —
no per-rung workaround needed. Expect **8 windows** (1 rep × 8 levels).
`anon+cache` is R8's memory read: the index is file-backed, so it is page
cache and not anonymous memory.

**Confirmed by hand, not by `verify_arm`** — there is no registered target for
a disk-backed index (`ftsbench/target.py` has none). The tuning-line check
above is the gate.

### Pull it home, immediately

```bash
d="$R/scylla"
scp    'fts-harness:/mnt/nvme/work/results/scylla-disk-rep*'     "$d/points/"
scp -r 'fts-harness:/mnt/nvme/work/samples/scylla-disk-rep1/'*   "$d/samples/"
scp -r 'fts-harness:/mnt/nvme/work/logs/scylla-disk/'*           "$d/logs/"
scp    'fts-harness:/mnt/nvme/work/probe/scylla-disk.jsonl'      "$R/sut/cpu-scylla-disk.jsonl"
scp -r 'fts-harness:/mnt/nvme/work/probe/scylla-disk/'*          "$d/probe/"
mv "$d"/points/*.tsv "$d/logs/" 2>/dev/null

ls "$d"/points/*.csv | wc -l                    # 1 (N=1)
awk -F, '!/^#/ && $1!="concurrency"' "$d"/points/*.csv | wc -l   # 8 (one row per level)
ls "$d"/probe/cpu-*.jsonl | wc -l                # 8
```

### Tear down and prepare Part B

```bash
ssh fts-harness 'cd $BENCH && . tools/fleet_env.sh && make scylla-down'
```

## Phase 5c — the OpenSearch arm, disk-backed index

### Bring the stack up with `os-disk-refresh1`'s config

```bash
ssh fts-harness 'set -eu
  cd $BENCH && . tools/fleet_env.sh
  make os-up && make os-wait'
```

`OS_RAM_INDEX` is left unset, which is what puts segments on the
`opensearch-data` NVMe volume rather than the tmpfs overlay.

### Confirm the data mount

```bash
ssh fts-harness 'cd $BENCH && . tools/fleet_env.sh
  docker exec fts-bench-opensearch df -h /usr/share/opensearch/data | tail -1'
```

Must show the NVMe-backed volume, **no tmpfs**.

### Start the resource probe

```bash
ssh fts-harness "set -eu
  cd \$BENCH && . tools/fleet_env.sh
  mkdir -p /mnt/nvme/work/probe
  tools/sut_probe.sh start /mnt/nvme/work/probe/os-disk.jsonl \
      --engine opensearch --containers fts-bench-opensearch:opensearch \
      --os-url http://127.0.0.1:9200 --os-index wiki-articles \
      --interval 1 --duration 0 --label 'build-rate-conc-disk os' \
      --corpus /mnt/nvme/data/corpus.jsonl"
```

### Run the ladder

```bash
ssh fts-harness "REPS=1 LADDER=1,2,4,8,16,32,64,128 BATCH=1024 MAX_DOCS=<budget> \
    OUT_DIR=/mnt/nvme/work/results-os SAMPLES_DIR=/mnt/nvme/work/samples-os \
    ~/run-os-arm.sh os-disk --index-watch --index-config disk \
        --refresh-interval 1s --index-interval 0.25"
```

Read the run's first level line: it must show `refresh_interval=1s` read back
off the index (not the ramindex config's default). A run that lost
`--refresh-interval` looks identical except for that one field.

### Close the arm out, on the harness, before `os-down`

```bash
ssh fts-harness "set -eu
  cd \$BENCH && . tools/fleet_env.sh
  L=/mnt/nvme/work/logs/os-disk; mkdir -p \$L
  docker logs fts-bench-opensearch > \$L/opensearch.log 2>&1
  cp docker/.env.sut \$L/env.sut
  tools/sut_probe.sh stop /mnt/nvme/work/probe/os-disk.jsonl
  .venv/bin/python3 -m ftsbench.probe_windows --arm os-disk \
      --probe /mnt/nvme/work/probe/os-disk.jsonl \
      --stderr '/mnt/nvme/work/results-os/os-disk-b1024-rep*.stderr.tsv' \
      --out-dir /mnt/nvme/work/probe/os-disk --memory-read anon+cache \
      --table \$L/resource-by-rung.csv
  .venv/bin/python3 -m ftsbench.verify_cpu_usage --data-dir /mnt/nvme/work/probe/os-disk \
      --containers fts-bench-opensearch \
      --output-json \$L/cpu-utilisation.json"
```

`anon+cache`, matching `os-disk-refresh1`: the index is on the NVMe, so it is
page cache. Expect **8 windows** (1 rep × 8 levels) — a concurrency ladder
gives `probe_windows` a unique key per level with no per-rung-invocation
workaround needed, same as the ScyllaDB side.

### Pull it home, immediately

```bash
d="$R/opensearch"
scp    'fts-harness:/mnt/nvme/work/results-os/os-disk-b1024-rep*'   "$d/points/"
scp -r 'fts-harness:/mnt/nvme/work/samples-os/os-disk-rep1/'*    "$d/samples/"
scp -r 'fts-harness:/mnt/nvme/work/logs/os-disk/'*               "$d/logs/"
scp    'fts-harness:/mnt/nvme/work/probe/os-disk.jsonl'           "$R/sut/cpu-os-disk.jsonl"
scp -r 'fts-harness:/mnt/nvme/work/probe/os-disk/'*               "$d/probe/"
mv "$d"/points/*.tsv "$d/logs/" 2>/dev/null

ls "$d"/points/*.csv | wc -l                    # 1 (N=1)
awk -F, '!/^#/ && $1!="concurrency"' "$d"/points/*.csv | wc -l   # 8 (one row per level)
ls "$d"/probe/cpu-*.jsonl | wc -l                # 8
```

### Tear down

```bash
ssh fts-harness 'cd $BENCH && . tools/fleet_env.sh && make os-down'
```

## Phase 6 — verify all three arms came home, then stop the boxes

From `$R` alone, **before the stop**:

```bash
for e in scylla-buf15-disk scylla opensearch; do
  echo "== $e =="
  ls "$R/$e"/points/*.csv | wc -l                                  # 1
  awk -F, '!/^#/ && $1!="concurrency"' "$R/$e"/points/*.csv | wc -l  # 8
  ls "$R/$e"/probe/cpu-*.jsonl | wc -l                             # 8
  cat "$R/$e"/logs/resource-by-rung.csv | grep -c ',thin$\|,empty$'  # named, not silent
done

grep -c "index=disk:/var/lib/vector-store/fts" "$R"/scylla-buf15-disk/logs/vector-store.log  # >=1
grep -c "15 MB buffer per thread"  "$R"/scylla-buf15-disk/logs/vector-store.log   # >=1
grep -c "index=disk:/var/lib/vector-store/fts" "$R"/scylla/logs/vector-store.log  # >=1
grep -c "376 MB buffer per thread" "$R"/scylla/logs/vector-store.log              # >=1
grep -h "^# latency_basis" "$R"/scylla-buf15-disk/points/*.csv "$R"/scylla/points/*.csv "$R"/opensearch/points/*.csv | sort -u  # service
```

`latency_basis=service` on every row is the closed-loop signature — the
correct one for a concurrency ladder, and the thing that would tell you if a
`--target-rate` leaked into a run line by mistake.

**Then stop the boxes**, in Chrome (adjust region/search to whichever fleet
pair this session used):

```
https://console.aws.amazon.com/ec2/home#Instances:search=k-nowacki
```

Select both rows → **Instance state → Stop instance** → confirm both read
`Stopped`. Report the absolute path of `$R` to the user.

## Notes carried over from the source runbooks

- **N=1 is this file's simplification.** Re-running any arm's ladder with
  `REPS=3` (same script, `REPS=3 ~/run-arm.sh` / `~/run-os-arm.sh`, same
  budget) is a trivial follow-up if more statistical confidence is wanted —
  it does not require rebuilding anything or re-deriving the budget.
- **This is not the campaign's cross-engine read.** R2 ↔ R4 (or R8 ↔
  `os-disk-refresh1`) on the *rate* ladder is what the two big runbooks exist
  for; this file's arms are not on that axis and its CSVs do not pool with
  theirs (`latency_basis` differs: `service` here, `intended_start` there).
- **Phase 5a has no campaign counterpart.** `scylla-buf15-disk` (R1's buffer,
  R8's index location) is measured nowhere else, so its numbers have nothing
  to be cross-checked against. Treat a surprising 5a → 5b delta as unverified
  until a second run reproduces it.
- **The RSS/cache memory gate applies to all three arms.** Every index here is
  disk-backed, so it is page cache, which the cgroup reclaims rather than
  kills — read `cache_bytes` against the container's `mem_limit_bytes` in
  `resource-by-rung.csv` before trusting a level, the same as R8 and
  `os-disk-refresh1` do.
