# Build-rate runbook — two arms, the whole corpus, index size on x

**Hand this file to Claude Code as the instruction and it runs the whole
session**: re-enters the (already-running) fleet, checks it has the disk for a
full-corpus build, brings up ScyllaDB + vector-store with the index on disk
behind a 376 MB tantivy writer buffer, builds the **entire 8,967,625-document
enwiki corpus** three times while a probe watches CPU and memory, pulls the
data home, tears that stack down, brings up OpenSearch with its index on disk
at `--batch-size 1024`, builds the same corpus three times, pulls that data
home, tears down, stops the boxes. Every script it needs is inline.

The question is **not** how fast either engine indexes — the concurrency and
rate ladders already answer that. It is whether the answer *changes as the
index fills*: x is documents already in the index, y is documents indexed per
second, and one line is one whole build from empty to the full corpus.

It draws its engine configuration from
[`INDEX-RATE-SCYLLA-RUNBOOK.md`](INDEX-RATE-SCYLLA-RUNBOOK.md)'s **R8**
(`scylla-buf376-disk`) arm and
[`INDEX-RATE-OPENSEARCH-RUNBOOK.md`](INDEX-RATE-OPENSEARCH-RUNBOOK.md)'s
**`os-disk-refresh1`** arm, and its run mechanics from
[`BUILD-RATE-CONCURRENCY-DISK-RUNBOOK.md`](BUILD-RATE-CONCURRENCY-DISK-RUNBOOK.md).
It is not a fourth campaign document — where it and those disagree on an
engine knob or a gate, they are right and this file has drifted.

| | this runbook | `BUILD-RATE-CONCURRENCY-DISK-RUNBOOK.md` |
|---|---|---|
| Axis | **documents already in the index**, read per second inside one build | concurrency, `1,2,4,8,16,32,64,128` |
| Concurrency | **one level, declared: `8`** — a constant, not an axis | the ladder |
| Documents | `--max-docs 0` — the **whole corpus**, 8,967,625 | one flat calibrated budget, ~200 k |
| Arms | 2 — ScyllaDB disk @376 MB buffer, OpenSearch disk @batch 1024 | 3 |
| Reps | **N=3** | N=1 |
| Reading | the per-second series (`--samples-dir`); the point CSV is a by-product | the point CSV |
| Resources | probe per tick, joined **onto the same x axis** by `ftsbench.probe_growth` | probe per rung, summarised by `ftsbench.probe_windows` |
| Session | ~2.5 h of load in one sitting | ~40 min |

## Why one concurrency and not a ladder

A ladder asks "how fast at load X". This asks "how fast at index size X", and
the two cannot share a chart: every rung of a ladder is a *separate build from
zero*, so a ladder sweeps x back to the origin eight times and never reaches
a large index at all. The budgets those runbooks calibrate stop at **2.2% of
the corpus** on the ScyllaDB side (200 k documents) and **8.9%** on the
OpenSearch side (800 k) — the entire region this file exists to measure is off
the right-hand edge of every chart the campaign has drawn so far.

So concurrency is frozen and the corpus is not. **`c=8` on both arms**, for
different reasons on each:

| Arm | At `c=8`, measured 2026-09-16 | Why 8 |
|---|---|---|
| ScyllaDB disk @376 MB | submit 19,063 docs/s, index 15,223 docs/s | The index plateaus at ~15.3 k from `c=8` up and does not improve at 16/32/64/128. `c=8` is the lowest rung on that plateau, so the **index is the bottleneck** while the client keeps ~25% headroom and stays engaged. At `c=32` the client runs 3.3x ahead of the index, finishes early, and most of the build becomes a drain against an idle ScyllaDB — a different measurement. |
| OpenSearch disk, batch 1024 | submit 11,648 docs/s, index 11,480 docs/s | The engine ceiling is ~12 k from `c=4` up, and it survives an 8x raise of the in-flight cap, so it is the engine and not the harness; `c=8` is one rung of headroom above the knee at p99 ≈ 1.3 s. In-flight is 8 x 1,024 = **8,192 documents**, far below the 262 k–524 k band where 429s appear. |

**`c=8` is not a shared unit and no cross-engine line is drawn from it.** One
`scyllarate` request carries one document and one `osrate` request carries
1,024, so "8 in flight" is 8 documents on one side and 8,192 on the other —
the same asymmetry the campaign records as `in_flight_peak` rather than
pretending the x axis is shared. What *is* shared is the x axis of the chart
this run produces: documents in the index means the same thing on both halves.

## What this does not give you

No PRELIMINARY figure fit to quote in the talk, and no cross-engine claim
about *speed*. Y does not mean the same thing on the two halves: on ScyllaDB
it is the vector-store's Tantivy build publishing continuously, and on
OpenSearch it is refresh-gated visibility climbing in steps, which
`rate_vs_index_size.py` widens the bucket to one riser to keep honest. The
comparable reading is the **shape** — whether a line holds flat or decays as
its index fills — not the height of one line against the other.

It also measures nothing about a RAM-backed index (that is R2 in
`INDEX-RATE-SCYLLA-RUNBOOK.md`), nothing about queries, and nothing about
batch sizes other than 1024.

## Assumptions

**The AWS fleet is already running.** This file has no "start the instances"
step. If the boxes are stopped, run
[`INDEX-RATE-SCYLLA-RUNBOOK.md`](INDEX-RATE-SCYLLA-RUNBOOK.md) Phase 1 first,
then come back here.

**`/mnt/nvme` may already be mounted and populated from a prior session on
these same boxes.** The re-entry steps below are the idempotent parts only;
they do **not** blindly reformat the instance store.

**Disk is a first-class prerequisite here and was not in any previous
run.** Every earlier build-rate pass indexed 200 k–800 k documents. This one
indexes 8,967,625, which is a different order of magnitude of bytes on both
boxes, and Phase 1f is a hard gate rather than a formality.

## The fleet

| Alias | Role |
|---|---|
| `fts-harness` | runs `scyllarate`/`osrate`, holds the corpus |
| `fts-sut` | runs the engine under measurement, and the resource probe |

Both `i8g.2xlarge` (8 vCPU Graviton4, 61 GiB, ~1.7 TB instance store,
aarch64), Amazon Linux 2023, user `ec2-user`. Two pairs exist —
`eu-north-1` (SUT `172.31.47.166`, key `KarolNowackiAws.pem`) and `-priv` in
`us-east-1` (SUT `172.31.13.225`, key `KarolNowackiAwsPriv.pem`). **On the
`-priv` pair the ScyllaDB half needs four overrides the campaign runbooks do
not carry**: `SCYLLA_BROADCAST_RPC` and `SCYLLA_VS_URI` pointed at that SUT
(`.env.sut` hardcodes the `eu-north-1` one), `make scylla-schema` and
`make scylla-index` before the first run, and `TARGETARCH=arm64` on the
vector-store image build. The first two bite every `make scylla-up` and are
repeated in Phase 5a; the image build is Phase 1d. There is no AWS CLI
credential on this laptop — the console in Chrome is the only way to stop the
boxes at the end.

## Wall clock, so the session is not started at the wrong hour

| Step | Cost |
|---|---|
| Re-entry, build, venv (Phase 1) | ~10 min, or **+15 min** if the vector-store image must be rebuilt |
| Corpus decompress to 35.4 GB (Phase 2) | ~5–10 min |
| Smoke test (Phase 4) | ~5 min |
| **ScyllaDB arm, 3 reps** (Phase 5a) | **36–66 min** — 12–22 min per rep |
| **OpenSearch arm, 3 reps** (Phase 5b) | **40–63 min** — 13–21 min per rep |
| Pulls home, charts, verification | ~15 min |
| **Total** | **~2.5–3 h of billed fleet time** |

Per-rep ranges come from the 2026-09-16 `c=8` measurements extrapolated to
8,967,625 documents (470 s of submit and 589 s of indexing on the ScyllaDB
side, 770 s on the OpenSearch side) with room for the degradation this run
exists to look for, plus a per-rep reset that now drops a 35 GB keyspace
rather than a 200 k-row one. **If the curve decays steeply the reps run
longer, not shorter** — that is the finding, not an overrun.

Keep the AWS console tab refreshed every few minutes throughout. Its session
expires over a run this long, and there is no CLI credential on the laptop to
fall back on — an expired console is an unstoppable, still-billing fleet.

## Phase 0 — the results directory, on the laptop

```bash
export RUN_ID="build-rate-vs-index-size-$(date -u +%Y-%m-%dT%H%MZ)"
export R="$HOME/Projects/Scylla/p99/bench/results/$RUN_ID"
mkdir -p "$R"/{env,scripts,sut}
mkdir -p "$R"/scylla/{points,samples,logs,probe}
mkdir -p "$R"/opensearch/{points,samples,logs,probe}
printf '%s\n' "$RUN_ID" > "$R/RUN_ID"
echo "results -> $R"
```

If the shell is lost, recover with
`export R="$(readlink -f bench/results/build-rate-vs-index-size-*)"`.

## Phase 1 — fleet re-entry

### 1a. SSH and the private IPs

Public IPs are reassigned on every start; private IPs are not. If SSH already
works, skip to 1b.

```bash
sed -i '/^Host fts-harness$/,/^$/ s/^    HostName .*/    HostName <new-harness-ip>/' ~/.ssh/config
sed -i '/^Host fts-sut$/,/^$/     s/^    HostName .*/    HostName <new-sut-ip>/'     ~/.ssh/config
ssh -o StrictHostKeyChecking=accept-new fts-harness true
ssh -o StrictHostKeyChecking=accept-new fts-sut     true
ssh fts-harness hostname -I
ssh fts-sut     hostname -I        # <- this is SUT_IP below
```

Record the SUT private IP; every later step uses it.

```bash
export SUT_IP=<sut-private-ip>
```

### 1b. The instance store — check before touching it

```bash
ssh fts-harness 'mountpoint -q /mnt/nvme && echo mounted || echo not-mounted'
ssh fts-sut     'mountpoint -q /mnt/nvme && echo mounted || echo not-mounted'
```

**Only if `not-mounted`** on a box — this destroys whatever the instance store
held, which is correct after a stop and wrong on a box that is already up:

```bash
for h in fts-harness fts-sut; do
  ssh $h 'set -e
    sudo mkfs.xfs -f -q /dev/nvme0n1
    sudo mkdir -p /mnt/nvme && sudo mount -o noatime /dev/nvme0n1 /mnt/nvme
    sudo chown ec2-user:ec2-user /mnt/nvme
    sudo systemctl restart docker'
done
```

### 1c. Clocks

The probe-to-series join in Phase 6 is a wall-clock join across two machines,
and `ftsbench.probe_growth` drops any tick more than `--max-skew` (default
1 s) from its nearest index reading. A skew here is silent data loss there.

```bash
ssh fts-sut date +%s.%N; ssh fts-harness date +%s.%N
```

Both must agree to well under a second. Record it:

```bash
{ echo "sut     $(ssh fts-sut date +%s.%N)"
  echo "harness $(ssh fts-harness date +%s.%N)"; } > "$R/env/clock-skew.txt"
```

### 1d. The vector-store image — the ScyllaDB half only

`VS_FTS_INDEX_DIR` (disk-backed index) only exists from commit `94a23ef2`
onward.

```bash
ssh fts-sut docker images scylladb/vector-store   # look for 1.10.0-45-g94a23ef2-arm64
```

If it is not there, build it on the harness and load it onto the SUT:

```bash
ssh fts-harness 'set -e
  sudo systemctl start docker
  mkdir -p /mnt/nvme/build && cd /mnt/nvme/build
  rm -rf vector-store
  git clone -b p99-fts-ingest-optimization \
      https://github.com/knowack1/vector-store.git
  cd vector-store
  git fetch --tags https://github.com/scylladb/vector-store.git
  git rev-parse HEAD                  # expect 94a23ef2c9ff...
  TARGETARCH=arm64 ./scripts/run-with-release-toolchain cargo build --release
  TARGETARCH=arm64 ./scripts/build-dockers arm64'

ssh fts-harness 'docker save scylladb/vector-store:1.10.0-45-g94a23ef2-arm64' \
    | ssh fts-sut docker load
ssh fts-sut docker images scylladb/vector-store
```

~15 min, billed once. Run it in a shell that has **not** sourced
`tools/fleet_env.sh` — with `DOCKER_HOST` pointed at the SUT the build would
land on the wrong daemon. `TARGETARCH=arm64` on **both** lines is load-bearing
on Graviton, and the built tag may come back as `...-g94a23ef-arm64` (7 hex)
rather than the 8 `.env.sut` expects; override `VECTOR_STORE_IMAGE` to the tag
that was actually built rather than retagging. `build-dockers` calls
`run-with-release-toolchain` without `TARGETARCH`, which defaults to amd64 and
dies on Graviton with `exec format error` — after which the version string
comes back empty and the tag degenerates to `scylladb/vector-store:-arm64`.

### 1e. The bench checkout, venv, and both harness binaries

```bash
cd ~/Projects/Scylla/p99/bench && tar czf - --exclude=__pycache__ --exclude=target \
    ftsbench tools docker Makefile requirements.txt scylladb opensearch build-rate \
  | ssh fts-harness 'mkdir -p /mnt/nvme/work/bench && tar xzf - -C /mnt/nvme/work/bench'

ssh fts-harness 'set -e
  echo "export BENCH=/mnt/nvme/work/bench" >> ~/.bashrc
  cd /mnt/nvme/work/bench
  python3.12 -m venv /mnt/nvme/work/venv && ln -sfn /mnt/nvme/work/venv ~/venv
  ln -sfn /mnt/nvme/work/venv .venv
  .venv/bin/pip install -q -r requirements.txt
  .venv/bin/python3 -c "import ftsbench.probe_growth; print(\"ok\")"'

ssh fts-harness 'cd /mnt/nvme/work/bench/build-rate/scylla && . "$HOME/.cargo/env" \
  && CARGO_TARGET_DIR=/mnt/nvme/work/target cargo build --release --locked'

ssh fts-harness 'cd /mnt/nvme/work/bench/build-rate/opensearch && . "$HOME/.cargo/env" \
  && CARGO_TARGET_DIR=/mnt/nvme/work/target-os cargo build --release --locked'

git -C ~/Projects/Scylla/p99/bench log -1 --format='%H %s' > "$R/env/bench-commit.txt"
git -C ~/Projects/Scylla/p99/bench status --short >> "$R/env/bench-commit.txt"
```

The `import ftsbench.probe_growth` line is a gate, not decoration: that module
is what turns the probe into the CPU and memory reading this runbook promises,
and a checkout that predates it will not fail until Phase 6, an hour of billed
load later.

**The SUT needs the venv too** — `tools/sut_probe.sh` runs
`$HOME/venv/bin/python3 -m ftsbench.resource_probe` on the SUT, from
`~/p99/bench`:

```bash
ssh fts-sut 'ls ~/venv/bin/python3 && cd ~/p99/bench && ~/venv/bin/python3 -c "import ftsbench.resource_probe; print(\"ok\")"'
```

**Do not rebuild once an arm has run.** Freeze the binaries here.

### 1f. Disk — the gate this run adds

A 200 k-document build fits anywhere. 8,967,625 does not. Budget, from
`FREEZE.md` (34.3 GB of body text, 35.4 GB of `corpus.jsonl`) and `SIZING.md`
(Tantivy index ≈ 0.42 x body text ≈ **14.4 GB**):

| Where | Holds | Peak, with merge and compaction headroom |
|---|---|---|
| harness `/mnt/nvme` | `corpus.jsonl` + the `.zst` it came from | ~50 GB |
| SUT docker data-root | ScyllaDB base table (~35 GB) + its CDC log (~35 GB, 24 h TTL) + compaction transients | ~150 GB |
| SUT docker data-root | `vector-store-fts` volume: 14.4 GB of index + merge transients | ~40 GB |
| SUT docker data-root | OpenSearch: `_source` + positional index, single shard, + merge transients | ~60 GB |
| SUT docker data-root | **`DROP KEYSPACE` snapshots, x3 reps** — see below | up to ~450 GB |

```bash
ssh fts-harness 'df -h /mnt/nvme | tail -1'
ssh fts-sut     "docker info --format '{{.DockerRootDir}}'"
ssh fts-sut     "df -h \$(docker info --format '{{.DockerRootDir}}') | tail -1"
```

**The SUT's docker data-root must be on the instance store and must show at
least 700 GB free.** If it reports the ~32 GB root volume instead, stop: the
ScyllaDB base table alone will not fit, and what you get is not a slow run but
a wedged engine and, on the OpenSearch arm, a flood-stage watermark that turns
the index read-only mid-build and reports as an engine that stopped accepting
documents. `ENGINE-PREP-PLAN.md` has the `daemon.json` step.

**Why the snapshot line is that large.** Scylla's `auto_snapshot` defaults to
true, so each rep's `DROP KEYSPACE` hard-links the sstables it just dropped
instead of freeing them. Three reps of a 35 GB keyspace accumulate. Check
rather than assume, between reps if the arm is run rep by rep and in any case
before starting the OpenSearch arm:

```bash
ssh fts-harness 'cd $BENCH && . tools/fleet_env.sh && docker exec fts-bench-scylla nodetool listsnapshots'
ssh fts-harness 'cd $BENCH && . tools/fleet_env.sh && docker exec fts-bench-scylla nodetool clearsnapshot -- wiki'
```

Record the free space before and after each arm into `$R/env/disk.txt` — a
build that decays because the filesystem filled is not the finding this run is
looking for, and the only way to tell the two apart afterwards is to have
written the number down.

## Phase 2 — the corpus, on the harness

```bash
ssh fts-harness 'set -e
  mkdir -p /mnt/nvme/data
  pzstd -d -p 8 -f -o /mnt/nvme/data/corpus.jsonl ~/corpus.jsonl.zst
  ls -l /mnt/nvme/data/corpus.jsonl
  sha256sum /mnt/nvme/data/corpus.jsonl'
```

Expect **35,448,823,550 bytes** and sha256
`1700bb6c9b2652cf7b248e8caff7bfecc54fd2376e9a75e43379aaa79c50c432` —
`../FREEZE.md`'s frozen enwiki corpus, 8,967,625 documents. Real text is
required: BM25 term statistics, segment merges and the analyzer all depend on
it, and merge behaviour as the index grows is precisely what is under
measurement. **Do not use a synthetic corpus.**

Confirm the document count, because `--max-docs 0` means "however many lines
are in this file" and a truncated corpus would silently shorten every build:

```bash
ssh fts-harness 'wc -l < /mnt/nvme/data/corpus.jsonl'   # expect 8967625
```

If the archive is not on the harness root volume (a replaced box), re-stage
per `INDEX-RATE-SCYLLA-RUNBOOK.md` Phase 3. Budget ~20 min rather than the
36 that file quotes: parallel `curl` pulls the 38 GB of shards in under a
minute and the cost is `prepare_corpus`.

## Phase 3 — the run scripts

Two scripts, named apart from the ladder runbooks' `~/run-arm.sh` and
`~/run-os-arm.sh` so a session that runs both files cannot overwrite one with
the other.

**Three things differ from the ladder versions and all three matter:**

1. **`MAX_DOCS=0`** — the whole corpus, every rep.
2. **Every timeout is raised.** The defaults are sized for a 200 k build. A
   settle timeout of 120 s against a corpus whose drain alone can run ten
   minutes would cut the tail off every rep; an idle timeout of 10 s would
   read a large segment merge as a finished build.
3. **The samples directory is named `<sweep>-rep<N>`, where `<sweep>` is
   exactly what precedes `-rep<N>` in the stderr tape's filename** — including
   the `-b<batch>` on the OpenSearch side. `ftsbench.probe_growth` derives one
   from the other, and the ladder runbook's OpenSearch script names them
   inconsistently (`os-disk-b1024-rep1.stderr.tsv` beside `os-disk-rep1/`),
   which would make the Phase 6 join fail to find any series at all.

### ScyllaDB

```bash
ssh fts-harness 'cat > ~/run-growth-scylla.sh << "SCRIPT"
#!/bin/bash
set -u
ARM="$1"; shift
REPS="${REPS:-3}"
LEVEL="${LEVEL:-8}"
CORPUS="${CORPUS:-/mnt/nvme/data/corpus.jsonl}"
MAX_DOCS="${MAX_DOCS:-0}"
SINK="${SINK:?set SINK to the SUT private IP}"
PORT="${PORT:-9042}"
VS_PORT="${VS_PORT:-16080}"
OUT_DIR="${OUT_DIR:-/mnt/nvme/work/results-growth}"
SAMPLES_DIR="${SAMPLES_DIR:-/mnt/nvme/work/samples-growth}"
BIN=/mnt/nvme/work/target/release/scyllarate

mkdir -p "$OUT_DIR" "$SAMPLES_DIR"
WINDOWS="$OUT_DIR/run-windows.tsv"
[ -f "$WINDOWS" ] || printf "arm\trep\tstart_epoch\tend_epoch\texit_code\tmax_docs\tcsv\n" > "$WINDOWS"
stamp() { awk "{ printf \"%d\t%s\n\", systime(), \$0; fflush() }"; }

for rep in $(seq 1 "$REPS"); do
    csv="$OUT_DIR/$ARM-rep$rep.csv"
    log="$OUT_DIR/$ARM-rep$rep.stderr.tsv"
    echo "######## arm=$ARM rep=$rep c=$LEVEL max_docs=$MAX_DOCS $(date -u +%H:%M:%S)"
    start=$(date +%s)
    "$BIN" --corpus "$CORPUS" --concurrency "$LEVEL" --max-docs "$MAX_DOCS" \
           --hosts "$SINK" --port "$PORT" \
           --vs-url "http://$SINK:$VS_PORT" \
           --vs-interval 1.0 \
           --vs-settle-timeout 3600 --vs-idle-timeout 120 \
           --reset-timeout 900 \
           --out "$csv" --samples-dir "$SAMPLES_DIR/$ARM-rep$rep" \
           "$@" 2>&1 | stamp > "$log"
    code=${PIPESTATUS[0]}
    printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
        "$ARM" "$rep" "$start" "$(date +%s)" "$code" "$MAX_DOCS" "$csv" >> "$WINDOWS"
    grep -E "docs in" "$log" | tail -3
done
SCRIPT
chmod +x ~/run-growth-scylla.sh'
```

`VS_PORT=16080` is mandatory — the real vector-store's status endpoint, not
the null-sink's `+7000` convention. `--index-watch` is on by default on
`scyllarate`, which is what gives the index-build reading this runbook is for.
`--vs-interval 1.0` rather than the ladder runbooks' `0.25`: a quarter-second
poll exists so a three-second rung has readings at all, and here every build
runs for a quarter of an hour, so 1 s already yields ~750 readings per rep
while putting a quarter as much polling load on the engine being measured. It
also matches the probe's own 1 s tick, which is what keeps the Phase 6 join
inside `--max-skew`.

### OpenSearch

```bash
ssh fts-harness 'cat > ~/run-growth-os.sh << "SCRIPT"
#!/bin/bash
set -u
ARM="$1"; shift
REPS="${REPS:-3}"
LEVEL="${LEVEL:-8}"
BATCH="${BATCH:-1024}"
CORPUS="${CORPUS:-/mnt/nvme/data/corpus.jsonl}"
MAX_DOCS="${MAX_DOCS:-0}"
SINK_URL="${SINK_URL:?set SINK_URL to http://<sut>:9200}"
INDEX="${INDEX:-wiki-articles}"
OUT_DIR="${OUT_DIR:-/mnt/nvme/work/results-growth-os}"
SAMPLES_DIR="${SAMPLES_DIR:-/mnt/nvme/work/samples-growth-os}"
BIN=/mnt/nvme/work/target-os/release/osrate

mkdir -p "$OUT_DIR" "$SAMPLES_DIR"
WINDOWS="$OUT_DIR/os-windows.tsv"
[ -f "$WINDOWS" ] || printf "arm\tbatch\trep\tstart_epoch\tend_epoch\texit_code\tmax_docs\tcsv\n" > "$WINDOWS"
stamp() { awk "{ printf \"%d\t%s\n\", systime(), \$0; fflush() }"; }

for rep in $(seq 1 "$REPS"); do
    sweep="$ARM-b$BATCH"
    csv="$OUT_DIR/$sweep-rep$rep.csv"
    log="$OUT_DIR/$sweep-rep$rep.stderr.tsv"
    echo "######## arm=$ARM batch=$BATCH rep=$rep c=$LEVEL max_docs=$MAX_DOCS $(date -u +%H:%M:%S)"
    start=$(date +%s)
    "$BIN" --corpus "$CORPUS" --concurrency "$LEVEL" --batch-size "$BATCH" \
           --max-docs "$MAX_DOCS" --url "$SINK_URL" --index "$INDEX" \
           --index-watch --index-config disk --refresh-interval 1s \
           --index-interval 1.0 \
           --index-settle-timeout 3600 --index-idle-timeout 120 \
           --reset-timeout 900 \
           --out "$csv" --samples-dir "$SAMPLES_DIR/$sweep-rep$rep" \
           "$@" 2>&1 | stamp > "$log"
    code=${PIPESTATUS[0]}
    printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
        "$ARM" "$BATCH" "$rep" "$start" "$(date +%s)" "$code" "$MAX_DOCS" \
        "$csv" >> "$WINDOWS"
    grep -E "docs in" "$log" | tail -3
done
SCRIPT
chmod +x ~/run-growth-os.sh'
```

`RESET_FLAGS` is gone: the analyzer check is a gate on a real engine and only
the null-sink runs need `--no-analyzer-check`. `--batch-size 1024` matches
`os-disk-refresh1`'s own wire batch.

## Phase 4 — the smoke test, before committing two hours

The one thing a full-corpus run cannot afford is discovering a misconfigured
stack after 20 minutes of load. There is **no `--max-docs` calibration** here —
the corpus is the budget — so this phase exists only to prove the wiring, on
200 k documents and in about five minutes.

Run **Phase 5a's "Bring the stack up" and "Gate the tuning line" steps now**,
then come back here. Everything below assumes the vector-store has already
been confirmed to report `index=disk:...` and `376 MB buffer per thread`.

### Start a probe, exactly as the real arm will

```bash
ssh fts-harness "set -eu
  cd \$BENCH && . tools/fleet_env.sh
  mkdir -p /mnt/nvme/work/probe
  tools/sut_probe.sh start /mnt/nvme/work/probe/smoke.jsonl \
      --engine scylladb \
      --containers fts-bench-scylla:scylladb \
      --containers fts-bench-vector-store:vector-store \
      --vs-url http://127.0.0.1:16080 --keyspace wiki --vs-index articles_body_fts \
      --interval 1 --duration 0 --label 'build-rate-vs-index-size smoke' \
      --corpus /mnt/nvme/data/corpus.jsonl"
```

### One short build

```bash
ssh fts-harness "REPS=1 MAX_DOCS=200000 SINK=$SUT_IP \
    OUT_DIR=/mnt/nvme/work/smoke SAMPLES_DIR=/mnt/nvme/work/smoke-samples \
    ~/run-growth-scylla.sh smoke"
ssh fts-harness 'grep "docs in" /mnt/nvme/work/smoke/smoke-rep1.stderr.tsv'
```

Expect ~200,000 documents in ~13 s at roughly 19 k docs/s submitted and 15 k
indexed — the 2026-09-16 `c=8` numbers. Materially slower here means the stack
is misconfigured in some way the tuning-line gate does not cover, and it is far
cheaper to find that now.

### Stop the probe and prove the join

```bash
ssh fts-harness "set -eu
  cd \$BENCH && . tools/fleet_env.sh
  tools/sut_probe.sh stop /mnt/nvme/work/probe/smoke.jsonl
  .venv/bin/python3 -m ftsbench.probe_growth --arm smoke \
      --probe /mnt/nvme/work/probe/smoke.jsonl \
      --stderr '/mnt/nvme/work/smoke/smoke-rep*.stderr.tsv' \
      --samples-root /mnt/nvme/work/smoke-samples \
      --out /mnt/nvme/work/smoke/resource-vs-index-size.csv"
```

It must report **one window and 0 ticks dropped**, and the CSV must carry a
`docs_indexed` column that climbs from 0 to 200000. A non-zero drop count is a
clock skew (Phase 1c) or a probe that was not running, and on the real arm it
would silently cost most of the resource data — which nobody notices until the
boxes are stopped. Fix it here.

### Clear it away

The smoke run is not campaign data, and its keyspace must not carry into rep 1:

```bash
ssh fts-harness 'rm -rf /mnt/nvme/work/smoke /mnt/nvme/work/smoke-samples /mnt/nvme/work/probe/smoke.jsonl'
ssh fts-harness 'cd $BENCH && . tools/fleet_env.sh && make scylla-reset'
```

`scylla-reset`, not `scylla-down`: rep 1 must build from an empty index and an
empty base table, and plain `down` keeps both named volumes. The stack then
has to come up again — Phase 5a's bring-up is run a second time, which is why
it is written there rather than here.

## Phase 5a — the ScyllaDB arm, disk-backed index, 376 MB writer buffer

R8's knobs: `VS_FTS_WRITER_MEMORY_MB=376`, `VS_FTS_COMMIT_THRESHOLD=0`,
`VS_FTS_COMMIT_INTERVAL=1s`, `VS_FTS_METRICS_INTERVAL=1s`,
`VS_FTS_INDEX_DIR=/var/lib/vector-store/fts`.

### Bring the stack up

```bash
ssh fts-harness "set -eu
  cd \$BENCH && . tools/fleet_env.sh
  export SCYLLA_BROADCAST_RPC=$SUT_IP
  export SCYLLA_VS_URI=http://$SUT_IP:16080
  VS_FTS_WRITER_MEMORY_MB=376 VS_FTS_COMMIT_THRESHOLD=0 \
  VS_FTS_COMMIT_INTERVAL=1s VS_FTS_METRICS_INTERVAL=1s \
  VS_FTS_INDEX_DIR=/var/lib/vector-store/fts \
  make scylla-up && make scylla-wait
  make scylla-schema && make scylla-index && make scylla-serving"
```

`SCYLLA_BROADCAST_RPC` and `SCYLLA_VS_URI` are exported because `.env.sut`
hardcodes the `eu-north-1` SUT; on any other pair the driver reads the
topology and then times out on the wrong address. The three `make` targets on
the last line create the keyspace — `scylla-up` does not, and `scyllarate`
fails with `cannot use keyspace "wiki"` without them.

### Gate the tuning line before spending an hour of load on it

```bash
ssh fts-harness 'docker logs fts-bench-vector-store 2>&1 | grep -E "ingest tuning|tantivy worker"'
```

Must read `index=disk:/var/lib/vector-store/fts` and `commit_interval=1s`.
The `376 MB buffer per thread` half of this gate **cannot pass yet**: that line
comes from `IndexState::new`, which the vector-store does not reach until the
first document arrives, so on a freshly-created index the grep returns the
`ingest tuning` line alone. Prove the knob reached the container here —
`docker exec fts-bench-vector-store env | grep WRITER_MEMORY` — and read the
buffer line off the smoke build in Phase 4. `index=ram` means `VS_FTS_INDEX_DIR`
did not take (or the image predates `94a23ef2`) — and with a RAM index this corpus
needs ~14.4 GB resident and will hit the budget described below. `15 MB` means
`VS_FTS_WRITER_MEMORY_MB` was dropped. Either way, fix and recreate the stack.

### Start the resource probe — one probe for all three reps

```bash
ssh fts-harness "set -eu
  cd \$BENCH && . tools/fleet_env.sh
  mkdir -p /mnt/nvme/work/probe
  tools/sut_probe.sh start /mnt/nvme/work/probe/scylla-growth.jsonl \
      --engine scylladb \
      --containers fts-bench-scylla:scylladb \
      --containers fts-bench-vector-store:vector-store \
      --vs-url http://127.0.0.1:16080 --keyspace wiki --vs-index articles_body_fts \
      --interval 1 --duration 0 --label 'build-rate-vs-index-size scylla' \
      --corpus /mnt/nvme/data/corpus.jsonl"
```

One probe spanning all three reps, not one per rep: `cpu_cores_used` is a rate
differenced against the previous tick and is `null` on the first tick of a
file, so a probe per rep throws away the first reading of every build — which
here is the empty-index end of the x axis, the most interesting point on it.

### Run the arm

```bash
ssh fts-harness "REPS=3 LEVEL=8 MAX_DOCS=0 SINK=$SUT_IP VS_PORT=16080 \
    OUT_DIR=/mnt/nvme/work/results-growth \
    SAMPLES_DIR=/mnt/nvme/work/samples-growth \
    ~/run-growth-scylla.sh scylla-growth"
```

Run it in the background or with a timeout of at least 5,400 s. **Do not poll
every few seconds** — each `docker exec`/`curl` against the SUT competes with
the thing being measured. Once every few minutes is plenty:

```bash
ssh fts-harness 'tail -2 /mnt/nvme/work/results-growth/scylla-growth-rep*.stderr.tsv'
```

### Gates for this arm

Check all four before treating the data as a measurement.

**1. Every rep indexed the whole corpus.** `SIZING.md` is explicit that the
vector-store **silently skips adding documents** when its memory budget is
reached — it logs an error and keeps answering, so a partially-indexed corpus
looks exactly like a completed one except in the count. On this axis that
failure draws as a build rate falling to zero at large index size, which is
indistinguishable by eye from the finding this run is looking for.

```bash
ssh fts-harness "awk -F, '!/^#/ && \$1!=\"concurrency\" {print FILENAME, \"c=\"\$1, \"docs=\"\$2, \"index_docs=\"\$11}' \
    /mnt/nvme/work/results-growth/scylla-growth-rep*.csv"
```

`docs` and `index_docs` must both read **8967625** on all three reps.

**2. The vector-store did not hit its memory budget.**

```bash
ssh fts-harness 'docker logs fts-bench-vector-store 2>&1 | grep -iE "memory|cannot allocate|skip" | tail -20'
```

Empty is the pass. Anything here voids gate 1 even if the count happens to
match.

**3. The index was the bottleneck, not the client.** This arm's whole
justification is that `c=8` leaves the submit side with headroom. If
`docs_per_s` is not above `index_docs_per_s` on every rep, `c=8` became
client-bound at scale, the curve is measuring `scyllarate` rather than the
engine, and the arm must be re-run at `c=16`.

```bash
ssh fts-harness "awk -F, '!/^#/ && \$1!=\"concurrency\" {print FILENAME, (\$5>\$12 ? \"ok\" : \"CLIENT-BOUND\"), \"submit=\"\$5, \"index=\"\$12}' \
    /mnt/nvme/work/results-growth/scylla-growth-rep*.csv"
```

**4. Nothing was OOM-killed and the disk did not fill.**

```bash
ssh fts-harness 'cd $BENCH && . tools/fleet_env.sh
  docker inspect fts-bench-vector-store fts-bench-scylla --format "{{.Name}} oomkilled={{.State.OOMKilled}} exit={{.State.ExitCode}}"
  docker exec fts-bench-scylla nodetool listsnapshots'
ssh fts-sut "df -h \$(docker info --format '{{.DockerRootDir}}') | tail -1" | tee -a "$R/env/disk.txt"
```

### Close the arm out, on the harness, before `scylla-down`

```bash
ssh fts-harness "set -eu
  cd \$BENCH && . tools/fleet_env.sh
  L=/mnt/nvme/work/logs/scylla-growth; mkdir -p \$L
  docker logs fts-bench-vector-store > \$L/vector-store.log 2>&1
  cp docker/.env.sut \$L/env.sut
  tools/sut_probe.sh stop /mnt/nvme/work/probe/scylla-growth.jsonl
  .venv/bin/python3 -m ftsbench.probe_windows --arm scylla-growth \
      --probe /mnt/nvme/work/probe/scylla-growth.jsonl \
      --stderr '/mnt/nvme/work/results-growth/scylla-growth-rep*.stderr.tsv' \
      --out-dir /mnt/nvme/work/probe/scylla-growth --memory-read anon+cache \
      --table \$L/resource-by-rung.csv
  .venv/bin/python3 -m ftsbench.verify_cpu_usage --data-dir /mnt/nvme/work/probe/scylla-growth \
      --containers fts-bench-scylla fts-bench-vector-store \
      --output-json \$L/cpu-utilisation.json
  .venv/bin/python3 -m ftsbench.probe_growth --arm scylla-growth \
      --probe /mnt/nvme/work/probe/scylla-growth.jsonl \
      --stderr '/mnt/nvme/work/results-growth/scylla-growth-rep*.stderr.tsv' \
      --samples-root /mnt/nvme/work/samples-growth \
      --out \$L/resource-vs-index-size.csv"
```

Expect **3 windows** (3 reps x 1 level) from `probe_windows` and **0 ticks
dropped** from `probe_growth`. `anon+cache` is R8's memory read: the index is
file-backed here, so it is page cache and not anonymous memory — and on this
corpus `cache_bytes` is the number that grows, which is exactly what makes a
per-tick series worth having rather than one peak.

**Confirmed by hand, not `verify_arm`** — `ftsbench/target.py` has no target
for a disk-backed index. The four gates above are this arm's gate.

### Pull it home, immediately

Note that the sample directories are kept, not flattened: three reps write
`c8-1.csv` each, and flattening would leave one file and two silently lost
builds.

```bash
d="$R/scylla"
scp    'fts-harness:/mnt/nvme/work/results-growth/scylla-growth-rep*.csv'  "$d/points/"
scp    'fts-harness:/mnt/nvme/work/results-growth/*.tsv'                   "$d/logs/"
scp -r 'fts-harness:/mnt/nvme/work/samples-growth/*'                       "$d/samples/"
scp -r 'fts-harness:/mnt/nvme/work/logs/scylla-growth/*'                   "$d/logs/"
scp    'fts-harness:/mnt/nvme/work/probe/scylla-growth.jsonl'              "$R/sut/cpu-scylla-growth.jsonl"
scp -r 'fts-harness:/mnt/nvme/work/probe/scylla-growth/*'                  "$d/probe/"

ls "$d"/points/*.csv | wc -l                                       # 3
awk -F, '!/^#/ && $1!="concurrency"' "$d"/points/*.csv | wc -l      # 3
ls -d "$d"/samples/scylla-growth-rep*/ | wc -l                     # 3
ls "$d"/probe/cpu-*.jsonl | wc -l                                  # 3
awk -F, 'NR>1' "$d"/logs/resource-vs-index-size.csv | wc -l        # thousands, not zero
```

### Tear down before Phase 5b

```bash
ssh fts-harness 'cd $BENCH && . tools/fleet_env.sh && make scylla-reset'
ssh fts-sut "df -h \$(docker info --format '{{.DockerRootDir}}') | tail -1"
```

**`scylla-reset` (`down -v`), not `scylla-down`** — both the tantivy index and
the ScyllaDB base table are on disk now, and OpenSearch needs that space back.
Confirm the free space actually returned before starting the next arm.

## Phase 5b — the OpenSearch arm, disk-backed index, batch 1024

### Bring the stack up with `os-disk-refresh1`'s config

```bash
ssh fts-harness 'set -eu
  cd $BENCH && . tools/fleet_env.sh
  make os-up && make os-wait'
```

`OS_RAM_INDEX` is left unset, which is what puts segments on the
`opensearch-data` NVMe volume rather than the tmpfs overlay. `osrate` creates
the index itself from `--index-config disk`, so `make os-index` is not run
here.

### Gate the data mount and the watermarks

```bash
ssh fts-harness 'cd $BENCH && . tools/fleet_env.sh
  docker exec fts-bench-opensearch df -h /usr/share/opensearch/data | tail -1'
```

Must show the NVMe-backed volume, **no tmpfs**, and enough free space that the
~60 GB peak stays under OpenSearch's 85% low watermark. The defaults are left
in place deliberately — `make os-relax-watermarks` exists but changes the
engine's configuration away from the campaign's, so reach for it only if the
gate above fails, and record it in `$R/env/` if you do.

### Start the resource probe

```bash
ssh fts-harness "set -eu
  cd \$BENCH && . tools/fleet_env.sh
  mkdir -p /mnt/nvme/work/probe
  tools/sut_probe.sh start /mnt/nvme/work/probe/os-growth.jsonl \
      --engine opensearch --containers fts-bench-opensearch:opensearch \
      --os-url http://127.0.0.1:9200 --os-index wiki-articles \
      --interval 1 --duration 0 --label 'build-rate-vs-index-size os' \
      --corpus /mnt/nvme/data/corpus.jsonl"
```

`--os-url` is what makes `index_size_bytes` a real number rather than `null`,
and on this arm it is the second growth curve worth having: bytes on disk
against documents in the index, from the same file.

### Run the arm

```bash
ssh fts-harness "REPS=3 LEVEL=8 BATCH=1024 MAX_DOCS=0 \
    SINK_URL=http://$SUT_IP:9200 \
    OUT_DIR=/mnt/nvme/work/results-growth-os \
    SAMPLES_DIR=/mnt/nvme/work/samples-growth-os \
    ~/run-growth-os.sh os-growth"
```

Read the run's first level line: it must show `refresh_interval=1s` read back
off the index, not the ramindex config's default. A run that lost
`--refresh-interval` looks identical except for that one field.

### Gates for this arm

**1. Every rep indexed the whole corpus**, and **no bulk items were rejected**:

```bash
ssh fts-harness "awk -F, '!/^#/ && \$1!=\"concurrency\" {print FILENAME, \"docs=\"\$2, \"errors=\"\$3, \"failed_requests=\"\$10, \"index_docs=\"\$11}' \
    /mnt/nvme/work/results-growth-os/os-growth-b1024-rep*.csv"
```

`docs` and `index_docs` must both read **8967625**, with `errors` and
`failed_requests` at **0**. A 429 storm shows up here first. It has been seen only between 262 k and
524 k documents in flight — at 8 x 1,024 = 8,192 it should not appear, and if
it does, something other than this arm's concurrency is the cause.

**2. Flood stage never latched.** A full disk turns the index read-only and
every subsequent bulk fails:

```bash
ssh fts-harness 'cd $BENCH && . tools/fleet_env.sh
  curl -fsS "$OS_URL/wiki-articles/_settings?flat_settings=true" | grep -i read_only || echo "not read-only: ok"
  docker logs fts-bench-opensearch 2>&1 | grep -iE "flood|watermark|read-only" | tail -10'
```

**3. Nothing was OOM-killed.**

```bash
ssh fts-harness 'cd $BENCH && . tools/fleet_env.sh
  docker inspect fts-bench-opensearch --format "oomkilled={{.State.OOMKilled}} exit={{.State.ExitCode}}"'
```

### Close the arm out, before `os-down`

```bash
ssh fts-harness "set -eu
  cd \$BENCH && . tools/fleet_env.sh
  L=/mnt/nvme/work/logs/os-growth; mkdir -p \$L
  docker logs fts-bench-opensearch > \$L/opensearch.log 2>&1
  cp docker/.env.sut \$L/env.sut
  tools/sut_probe.sh stop /mnt/nvme/work/probe/os-growth.jsonl
  .venv/bin/python3 -m ftsbench.probe_windows --arm os-growth \
      --probe /mnt/nvme/work/probe/os-growth.jsonl \
      --stderr '/mnt/nvme/work/results-growth-os/os-growth-b1024-rep*.stderr.tsv' \
      --out-dir /mnt/nvme/work/probe/os-growth --memory-read anon+cache \
      --table \$L/resource-by-rung.csv
  .venv/bin/python3 -m ftsbench.verify_cpu_usage --data-dir /mnt/nvme/work/probe/os-growth \
      --containers fts-bench-opensearch \
      --output-json \$L/cpu-utilisation.json
  .venv/bin/python3 -m ftsbench.probe_growth --arm os-growth \
      --probe /mnt/nvme/work/probe/os-growth.jsonl \
      --stderr '/mnt/nvme/work/results-growth-os/os-growth-b1024-rep*.stderr.tsv' \
      --samples-root /mnt/nvme/work/samples-growth-os \
      --out \$L/resource-vs-index-size.csv"
```

Expect **3 windows** and **0 ticks dropped**. `anon+cache` matches
`os-disk-refresh1`: the index is on the NVMe, so it is page cache.

### Pull it home, immediately

```bash
d="$R/opensearch"
scp    'fts-harness:/mnt/nvme/work/results-growth-os/os-growth-b1024-rep*.csv' "$d/points/"
scp    'fts-harness:/mnt/nvme/work/results-growth-os/*.tsv'                    "$d/logs/"
scp -r 'fts-harness:/mnt/nvme/work/samples-growth-os/*'                        "$d/samples/"
scp -r 'fts-harness:/mnt/nvme/work/logs/os-growth/*'                           "$d/logs/"
scp    'fts-harness:/mnt/nvme/work/probe/os-growth.jsonl'                      "$R/sut/cpu-os-growth.jsonl"
scp -r 'fts-harness:/mnt/nvme/work/probe/os-growth/*'                          "$d/probe/"

ls "$d"/points/*.csv | wc -l                                       # 3
ls -d "$d"/samples/os-growth-b1024-rep*/ | wc -l                   # 3
ls "$d"/probe/cpu-*.jsonl | wc -l                                  # 3
```

### Tear down

```bash
ssh fts-harness 'cd $BENCH && . tools/fleet_env.sh && make os-reset'
```

## Phase 6 — the charts, on the laptop

### The chart this runbook exists for

```bash
cd ~/Projects/Scylla/p99/bench
.venv/bin/python3 build-rate/charts/rate_vs_index_size.py \
    --scylla     "$R/scylla/samples/*/c8-1.csv" \
    --opensearch "$R/opensearch/samples/*/c8-b1024-1.csv" \
    --output     "$R/build-rate-vs-index-size.png" \
    --table      "$R/build-rate-vs-index-size.csv" \
    --title      "Build rate as the index grows — full enwiki, 8,967,625 documents" \
    --subtitle   "ScyllaDB FTS (disk index, 376 MB writer buffer) vs OpenSearch (disk, batch 1024), c=8, N=3"
```

The globs pick up all three reps of each arm — the renderer groups them by
series and draws the median, which is why N=3 was worth the extra hour. Read
the footer: a build it skipped is named there, and the two ways a build goes
quiet rather than wrong (too few readings, nothing searchable) are listed in
[`charts/README.md`](charts/README.md).

**Find where the client stopped before reading the right-hand end of the
ScyllaDB line.** At `c=8` the client submits ~25% faster than the index
builds, so it runs out of corpus first and the rest of the build is a drain
against a ScyllaDB that is no longer being written to. Documents keep becoming
searchable there, so the line continues — but it is a different regime, and a
decay that begins exactly at the crossover is the client leaving, not the
index filling. The crossover is the first reading at which `docs_submitted`
has reached its final value — read that way, not off a `submit_docs_per_s` of
0, which a single tick with no completions can produce mid-run:

```bash
for d in "$R"/scylla/samples/*/; do
  awk -F, -v d="$(basename "$d")" '
    /^#/ || $1=="level" { next }
    FNR==NR { if ($4+0>max) max=$4; next }
    $4+0==max && !seen {
      print d": client stopped at t="$3"s, "($6==""?"n/a":$6)" of "max" submitted docs indexed"
      seen=1 }
  ' "$d"/c8-1.csv "$d"/c8-1.csv
done
```

The file is read twice on purpose: the final `docs_submitted` is not knowable
until the last row, and a one-pass version would have to guess.

On the OpenSearch half there is no such crossover: a `_bulk` does not answer
until its documents are accepted, so the client cannot run ahead.

Leave `--grid-step` alone on the first render. The bucket width is derived
from the data and floored at one riser, which is what keeps OpenSearch's
refresh steps from reading high by the refresh-to-poll ratio. If the resulting
line is too coarse to see a knee, `--grid-step 50000` gives ~180 buckets
across the corpus — but never go below the riser the footer reports.

### CPU and memory, on the same x axis

`resource-vs-index-size.csv` in each arm's `logs/` is one row per probe tick
per container, carrying the index size that tick was measured at. The columns
that matter:

| Column | Read it for |
|---|---|
| `docs_indexed` | the x axis — the same quantity as the chart above |
| `tick_docs_per_s` | the build rate **at this file's resolution**; the honest y to correlate a CPU number against |
| `index_docs_per_s` | the harness's own per-second number, copied verbatim; reads 0 on most ticks because the count advances only at a commit |
| `cpu_cores_used` | cores, against the arm's cpuset quota of **4** |
| `rss_bytes` | anonymous memory — tantivy writer buffers, JVM heap |
| `cache_bytes` | page cache — where a **disk-backed** index actually shows up |
| `mem_limit_bytes` | the cgroup cap the two above are read against |
| `index_size_bytes` | bytes on disk, OpenSearch only |

A quick look at whether either engine ran out of CPU headroom as its index
filled:

```bash
for e in scylla opensearch; do
  echo "== $e =="
  awk -F, 'NR>1 && $8!="" {
      key = int($8/1000000) "Mdocs " $5;
      n[key]++; cpu[key]+=$13;
      if ($14+0>rss[key])   rss[key]   = $14;
      if ($15+0>cache[key]) cache[key] = $15 }
    END { for (k in n) printf "%s: cpu_mean=%.2f rss_peak=%.2fGiB cache_peak=%.2fGiB\n",
      k, cpu[k]/n[k], rss[k]/1073741824, cache[k]/1073741824 }' \
    "$R/$e/logs/resource-vs-index-size.csv" | sort -n
done
```

**`cpu_cores_used` sitting at 4.0 across the whole x axis is the finding to
look for**, not a problem with the run: it says the engine was CPU-saturated
throughout and that any decay in the build rate is work per document growing,
not resources being withheld. `rss_bytes` climbing toward `mem_limit_bytes` is
the opposite — that is the silent-truncation path
[`../SIZING.md`](../SIZING.md) describes, and it voids the arm regardless of
what the count gate said.

### Cross-check the two readings agree

The chart and the resource table are built from different files and must tell
the same story about where each build ended:

```bash
for e in scylla opensearch; do
  echo "== $e =="
  awk -F, 'NR>1 {if ($8+0>m) m=$8} END {print "resource table max docs_indexed:", m}' \
      "$R/$e/logs/resource-vs-index-size.csv"
  awk -F, '!/^#/ && $1!="concurrency" {print "point CSV index_docs:", $11}' "$R/$e"/points/*.csv
done
```

## Phase 7 — verify, then stop the boxes

From `$R` alone, **before the stop**:

```bash
for e in scylla opensearch; do
  echo "== $e =="
  ls "$R/$e"/points/*.csv | wc -l                                       # 3
  ls -d "$R/$e"/samples/*/ | wc -l                                      # 3
  ls "$R/$e"/probe/cpu-*.jsonl | wc -l                                  # 3
  grep -c ',thin$\|,empty$' "$R/$e"/logs/resource-by-rung.csv           # named, not silent
  awk -F, 'NR>1' "$R/$e"/logs/resource-vs-index-size.csv | wc -l        # thousands
done

awk -F, '!/^#/ && $1!="concurrency" && $11!=8967625 {print FILENAME": "$11}' \
    "$R"/scylla/points/*.csv "$R"/opensearch/points/*.csv   # no output = every rep complete

grep -c "index=disk:/var/lib/vector-store/fts" "$R"/scylla/logs/vector-store.log   # >=1
grep -c "376 MB buffer per thread"             "$R"/scylla/logs/vector-store.log   # >=1
grep -h "^# latency_basis" "$R"/scylla/points/*.csv "$R"/opensearch/points/*.csv | sort -u  # service
grep -h "^# max_docs"      "$R"/scylla/points/*.csv "$R"/opensearch/points/*.csv | sort -u  # 0
```

`latency_basis=service` on every row is the closed-loop signature, correct for
a fixed-concurrency run, and what would tell you a `--target-rate` leaked into
a run line. `max_docs=0` is what says the whole corpus was asked for.

**Then stop the boxes**, in Chrome:

```
https://console.aws.amazon.com/ec2/home#Instances:search=k-nowacki
```

Select both rows → **Instance state → Stop instance** → confirm both read
`Stopped`. Report the absolute path of `$R`.

## What can go wrong, and what it looks like

- **A build rate that falls to zero at large index size** is either the
  finding or the vector-store's silent memory truncation. Phase 5a gate 2
  separates them and must be run before the chart is read.
- **A curve that decays and then recovers sharply** is usually a merge
  finishing, not noise — one riser's worth of documents published at once.
  Read it against `tick_docs_per_s` in the resource table rather than off the
  chart.
- **A rep noticeably slower than the two before it** on the ScyllaDB arm: check
  `nodetool listsnapshots` and the free space recorded in `$R/env/disk.txt`.
  `auto_snapshot` keeps every dropped keyspace's sstables, and a filesystem
  filling across reps is a monotonic slowdown that mimics an index-size effect
  exactly.
- **`probe_growth` dropping ticks** means the two clocks drifted apart during
  the run. The rows it kept are still correct; the ones it dropped are gone,
  and re-running the join with a larger `--max-skew` **fabricates** the
  alignment rather than recovering it. Re-check Phase 1c and, if the run is
  otherwise sound, record the drop count rather than widening the window.
- **OpenSearch at `index_docs_per_s` ≈ `docs_per_s` throughout** is normal and
  not a sign the index watch failed: a `_bulk` does not answer until the
  documents are accepted, so the two series track each other on that half in a
  way they never do on the ScyllaDB half.

## Notes

- **N=3 here, N=1 in the concurrency runbook.** Three reps is what lets
  `rate_vs_index_size.py` draw a median rather than a single build, and on a
  curve this long a single build cannot be told apart from a single merge
  storm.
- **This file's data does not pool with the ladder runbooks'.** Same
  `latency_basis`, different instrument: their x is load and this one's is
  index size, and their builds stop at 2.2% of the corpus.
- **Nothing here is quotable.** Like every other build-rate pass, this is
  preliminary until the campaign's own fairness and disclosure rules are
  applied to it. What it can settle is whether the ladder runbooks' numbers,
  all measured on a nearly-empty index, are representative of an index at
  full size — which is a question about the talk's headline claims, not a
  number to put beside them.
