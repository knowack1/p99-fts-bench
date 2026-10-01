# Index-rate runbook — OpenSearch half (R4, `os-disk-refresh1`)

**Hand this file to Claude Code as the instruction and it runs the OpenSearch
half of the index-rate campaign**: starts the two AWS boxes, stages the frozen
corpus, measures two arms against a real OpenSearch, pulls every artifact home,
stops the boxes. Every script it needs is inline.

It is one of two runbooks cut from
[`INDEX-RATE-MATRIX-PLAN.md`](INDEX-RATE-MATRIX-PLAN.md), which remains the
campaign's decision record — why each arm exists, what each reading is worth,
and what may not be said off the chart. Nothing here overrides it; where this
file and the plan disagree, the plan is right and this file has drifted.

| Runbook | Arms | Binary | Stack on the SUT |
|---|---|---|---|
| [`INDEX-RATE-SCYLLA-RUNBOOK.md`](INDEX-RATE-SCYLLA-RUNBOOK.md) | R1 `scylla-buf15`, R2 `scylla-buf376`, R8 `scylla-buf376-disk` | `scyllarate` | ScyllaDB + vector-store |
| **this file** | R4 `os-ramindex-refresh1`, `os-disk-refresh1` | `osrate` | OpenSearch |

**Run this half second.** The plan's execution order is R1, R2, R8, then these
two, and the reason is that a surprise in R2 can still change the plan before
this half bills any time. Running it first is allowed and costs nothing but
that option.

## The one thing to get right before anything else

**Both runbooks must write into the same `$R`.** The primary chart draws four
arms across both halves and `--series` names each line by the directory its
rows came from, so two run directories means two half-charts and no
cross-engine read at all. If the ScyllaDB half has already run, **reuse its
`RUN_ID`** rather than minting a new one — see "The results directory".

**If the two halves run in different fleet sessions, that is a provenance
difference and it reaches the footer.** Every stop wipes `/mnt/nvme` and forces
a full re-entry, so the split between these two runbooks is exactly the "split
at an arm boundary" the plan permits. Record which arms were measured in which
session, in `$R/env/`, at the time — not from memory afterwards. **R2 ↔ R4 is
the campaign's only cross-engine read and it spans the split**, so this is not
a bookkeeping note.

**The rate ladder imposes no ordering between the halves.** Its x grid is
declared — `1024,2048,4096,16384,32768` — not derived from anything measured, so
either half can be measured first, alone, and its rows land on the same grid as
the other's. What the halves owe each other is that they run **the same five
rates with the same five document counts**. See "The ladder".

[`INDEX-RATE-SCYLLA-RUNBOOK.md`](INDEX-RATE-SCYLLA-RUNBOOK.md) and
[`INDEX-RATE-MATRIX-PLAN.md`](INDEX-RATE-MATRIX-PLAN.md) **still carry the
calibration pass and the `C_max` derivation this file has dropped.** Settle it
in the plan and cut it into the ScyllaDB half before the fleet is started: two
halves on two grids is not a discrepancy a footer can carry.

## The probe blocker, and how the per-rung document bound cleared it

**`ftsbench/probe_windows.py` keys a resource window on
`(sweep, concurrency, rep)`** — `LEVEL_RE = r"\[\d+/\d+\] concurrency=(\d+)"`
(`ftsbench/probe_windows.py:67`), with `sweep` taken from the stderr filename
(`windows_of`, `REP_RE`). Under a **single** `osrate` invocation carrying a
multi-rung rate ladder, concurrency is a constant in-flight cap across every
rung, so every rung of a rep produced the same key and the duplicate check
exited 1 (`ftsbench/probe_windows.py:357-361`). That took out
`resource-by-rung.csv`, all the `probe/cpu-*.jsonl` rung slices, and
`ftsbench/verify_cpu_usage.py` with them — and on R4 the RSS-breach gate those
feed is **blocking**.

**The per-rung `--max-docs` dissolved it.** `--max-docs` is one value per
invocation (`core/src/corpus.rs` builds a fresh reader per level from it), so
giving every rung its own document count forces **one `osrate` invocation per
rung**. `run-os-arm.sh` names each one `$ARM-r<rate>-b<batch>`, so `sweep`
differs per rung, each log holds exactly one level, and
`(sweep, concurrency, rep)` is unique again. **No change to
`probe_windows.py` is needed and none should be made** — the out-of-scope file
stays out of scope.

**Confirm it in the smoke rather than assuming it.** The slicer is what stands
between R4 and an unmeasured tmpfs, and the smoke is the last cheap place to
find out that a sweep name lost its `-r<rate>`.

## What this measures, and what it is not

`index_docs_per_s` is searchable documents divided by the **whole** build wall:
from the first insert to the moment the searchable count reaches what was sent,
or stops moving, or runs out of settle budget (`core/src/build_rate.rs`,
`Level::summarize`). A write ack is not a document in the index, and this axis
is the one that refuses to pretend otherwise.

On this half the index is **refresh-gated visibility**. `docs.count` advances
at a refresh, so:

- **the build curve is flat, flat, jump** — a searchable count moves in steps;
- **`index_lag_docs` has a floor** of `refresh_interval × docs_per_s` — one
  second of arrivals at the 1 s cadence every arm here runs, however fast
  Lucene indexes. **The number to read is the excess**, not the lag;
- **`index_status=refreshed` means the harness asked**, which is a different
  measurement from what the configured policy delivers. See "The final
  refresh".

`_stats` reports accepted and searchable **separately** and `osrate`'s idle
rule watches the former, which is why no arm here needs a settle-timeout
override at any cadence.

**`p50_ms`/`p99_ms` are per request, and a request is not a document.**
`latency_unit` in the CSV header says which — `bulk_request` here,
`insert_request` on the other half. This is the only reason the two subtrees
stay separate.

**Under the rate ladder they are also measured from a different instant.** The
ladder **redefines** those two columns rather than adding a column: under pacing
they run from when a request was **due**, not from when it was sent. The header
says which, and every CSV this half publishes says the same thing:

| Header fact | Every published CSV |
|---|---|
| `latency_basis` | `intended_start` |
| `target_rate_docs_per_s` | the rung's single rate |

A CSV reading `latency_basis=service` is a closed-loop run, did not come out of
this campaign, and may not be pooled with these rows.

**No number from this runbook is a cross-engine number on its own.** The only
cross-engine read the campaign has is R2 ↔ R4, and R2 is in the other runbook.
Every image carries PRELIMINARY until the pass that produced it is written up.

## The two arms

X is the **offered rate in documents per second** on the shared grid — a rate
the client was told to produce, not a client knob whose meaning differs per
engine. Every arm is drawn **twice**: solid with a filled marker for
`docs_per_s`, dashed with a hollow one for `index_docs_per_s`, in the same
colour, against a dotted `y = x` diagonal. Where a line leaves the diagonal is
the reading.

Each arm is measured once: **one ladder of five declared rungs, at N=3.** There
is no calibration pass — the rates are a constant of the campaign, not something
read off a ceiling. See "The ladder".

| Arm (`--series` label) | Engine config | Harness command | Reps | Points |
|---|---|---|---|---|
| **R4** `os-ramindex-refresh1` | `OS_RAM_INDEX=1` (tmpfs segments), `OS_RAM_INDEX_SIZE=12884901888`, `_source: false`, `refresh_interval: 1s` | `osrate --index-watch --index-config ramindex --refresh-interval 1s --batch-size 1024 --queue-depth 1 --index-interval 0.25` | 3 | 15 |
| **`os-disk-refresh1`** | `OS_RAM_INDEX` unset, segments on the `opensearch-data` NVMe volume, `refresh_interval: 1s` | `osrate --index-watch --index-config disk --refresh-interval 1s --batch-size 1024 --queue-depth 1 --index-interval 0.25` | 3 | 15 |

Two arms, 30 points at a 5-rung ladder, four lines. The readings this half is
built to give:

| Read | Arms | What it is |
|---|---|---|
| Where the index lives | R4 → `os-disk-refresh1` | the tmpfs index against the same build on the NVMe; the OpenSearch half of S2a's pairing |
| Cross-engine, 1 s | R2 ↔ R4 | **needs the other runbook.** Both halves publish at 1 s, so it is cadence-matched, and both are now offered the same **documents per second**, which is what makes the pairing a comparison at all. Nothing here can produce it |

`os-disk-refresh1` is what a reader would actually run, and it is **the only
OpenSearch arm that can hold the frozen corpus** — the ramindex filled its
12 GiB of tmpfs at 4,025,699 documents.

**The numbering has gaps and they are deliberate.** R5 (`os-ramindex-refresh30`),
R6 (`os-ramindex-b128`) and R7 (`os-ramindex-b1`) were arms of this table and
were dropped: R5 because the campaign no longer measures a 30 s cadence, R6 and
R7 because every OpenSearch arm now runs at a single fixed `--batch-size 1024`.
The identifiers are **not** reused — an "R7" in a companion document refers to
an arm that was never measured.

**The labels moved too.** Both arms were `os-ramindex-refresh3` and
`os-disk-refresh3` until 2026-09-16, when this half went to the 1 s cadence the
ScyllaDB half already ran. Those names state a cadence no arm in this campaign
publishes at, so a chart, directory or companion document still carrying one is
stale rather than a fifth arm.

### Batch size is a constant here, not a series

Every arm runs `--batch-size 1024`, so the wire batch never moves and this half
has one line per configuration rather than one per batch level.
`HARNESS-AWS-RUNBOOK.md`'s "Batch size is a series, never an axis" still holds;
this campaign simply runs one member of that series. Two consequences follow,
and both are **footer clauses rather than footnotes**:

**The x-axis asymmetry is what took concurrency off the x axis — and it is now a
column instead.** `--concurrency` was requests in flight on both halves, but a
`_bulk` carries 1,024 documents and a prepared INSERT carries one. At `c=128`
OpenSearch held 128 requests carrying **131,072 documents** while `scyllarate`
held 128 requests carrying **128** — a 1,024x asymmetry at every rung, so
**R2 ↔ R4 compared two different things per x value**. R7, at batch 1, existed
to correct this and is gone, and it could never be closed from the ScyllaDB side
either: batch 1 is not a setting there, it is the write path.

**A document per second means the same thing on both halves, so that is the
axis.** The asymmetry did not disappear; it moved off the axis and into the
in-flight cap and `in_flight_peak` — see "The in-flight cap, and why it differs
per engine". Reading a *shape* across the two engines' curves is what the old
axis broke and what the new one restores, **but only at rungs where both sides
clear the cap-did-not-bind gate.**

**`--batch-size 1024` is an assumption this campaign inherits and does not
test.** Nothing here says whether 1024 is good, bad or at a knee — and a batch
optimum is not a constant in any case: it moves with document size and with
segment-merge pressure, and the second moves with corpus size. **Every
OpenSearch number on this chart is "OpenSearch at batch 1024", never
"OpenSearch".**

### The loader's read-ahead — `--queue-depth 1` under pacing

The channel is bounded in batches, so documents buffered ahead of the workers
are `queue_depth × concurrency × batch_size` and **all of them are resident**
(`HARNESS-AWS-RUNBOOK.md` B3).

| `queue_depth` | Resident read-ahead at `c=128`, batch 1024 |
|---|---|
| 10 — the harness default, which **no run here uses** | 1,310,720 documents ≈ 5.17 GB |
| **1** — every run in this runbook | **131,072 documents ≈ 0.52 GB** |

**Read-ahead exists to keep closed-loop workers fed, and a paced producer has no
use for it.** Under a rate ladder the producer emits on a schedule; a deep queue
in front of it buys nothing and costs 4.65 GB of resident memory on a harness
with no swap. `--queue-depth 1`, which `run-os-arm.sh` pins on every
invocation, drops it by a factor of ten.

**It also removes an artifact, not just memory.** At depth 10 the channel held
1,310,720 documents — half of the old 2,621,440 high sweep — so the top rung's
submitted rate sat between a drain and a steady state rather than at either. At
depth 1 the read-ahead is 3.3% of the 3,932,160-document top rung. **At the
122,880-document floor rung the channel could hold the whole rung** — but it is
a capacity, not a fill: under pacing the producer emits on a schedule and the
channel stays near empty, which is the entire reason depth 1 is enough. Read the
figure as the memory bound it is, never as a statement about how far the loader
runs ahead.

**The arithmetic is still a pre-flight check, not a measurement: run it whenever
the cap, the batch size or the depth moves.** The resident figure does not move
with `--max-docs`; it is `queue_depth × concurrency × batch_size` and nothing
else. With the calibration pass retired, **nothing in this half runs at depth 10
any more**, so the ~8 GB loader guard no longer binds anywhere here.

## The fleet

| Alias | Instance | Role |
|---|---|---|
| `fts-harness` | `i-08d8d2505e16683f7` · `k-nowacki-fts-benchmark-harness` | runs `osrate`, holds the corpus |
| `fts-sut` | `i-0e3e4b6b02e654b7f` · `k-nowacki-fts-benchmark-sut` | runs OpenSearch |

Both `i8g.2xlarge` (8 vCPU Graviton4, 61 GiB, aarch64), `eu-north-1b`, Amazon
Linux 2023, user `ec2-user`, key `~/.ssh/KarolNowackiAws.pem`. Private RTT
0.353 ms, measured.

There is no AWS CLI credential on this laptop — the console in Chrome is the
only way to start and stop the boxes.

**This half needs no vector-store image.** The build-and-load step that sits on
the critical path of every ScyllaDB start — ~15 min of billed fleet time — does
not apply: both OpenSearch images re-pull from the registry. If this runbook
runs alone, that is 15 minutes it does not pay.

## Phase 0 — the results directory, on the laptop

Everything on the fleet is ephemeral: `/mnt/nvme` is destroyed on every stop,
so **the laptop is the only place results survive**. Create the directory
before touching a single instance.

```bash
# Run this ONCE, at the very start of the session, and keep the shell.
# If the ScyllaDB half has already run, REUSE its RUN_ID here instead of
# minting a new one -- the primary chart needs all four arms under one $R.
export RUN_ID="index-rate-matrix-$(date -u +%Y-%m-%dT%H%MZ)"
export R="$HOME/Projects/Scylla/p99/bench/results/$RUN_ID"
for arm in r4 osdisk; do mkdir -p "$R/$arm"/opensearch/{points,samples,logs,probe}; done
mkdir -p "$R"/{env,scripts,sut}
printf '%s\n' "$RUN_ID" > "$R/RUN_ID"
printf 'opensearch half: %s\n' "$(date -u +%FT%TZ)" >> "$R/env/sessions.txt"
echo "results -> $R"
```

The arm directory name **is** the chart's series set: an arm written to the
wrong directory becomes a mislabelled line rather than a missing one. Note that
`os-disk-refresh1`'s directory is `osdisk` and its `--series` label is the full
name.

**There is no `calibration/` directory, because there is no calibration pass.**
Everything this half produces is a published rate-ladder CSV and belongs in
`points/`. The renderer's safety net still stands — `rate_vs_offered.py`
**refuses a concurrency-ladder CSV by name** — so a stray closed-loop file from
an older run swept into `points/` fails the render outright rather than drawing
a line. Nothing this runbook runs can put one there.

## Phase 1 — start the boxes

In Chrome, open the EC2 instance list filtered to this fleet:

```
https://eu-north-1.console.aws.amazon.com/ec2/home?region=eu-north-1#Instances:search=k-nowacki;v=3
```

Do not type into the filter box — it opens an "API filters" dropdown and
swallows the text. The `search=k-nowacki` in the URL is the filter.

Select both rows with the header checkbox → **Instance state → Start instance**.
Wait for `Running` and `3/3 checks passed`.

**Keep the console tab alive for the whole session.** Click the refresh icon
next to "Last updated" every few minutes. If the console session expires you
cannot stop the boxes from here, and they bill until someone else does.

## Phase 2 — fleet re-entry

Every stop wipes the instance store, so this runs on **every** start. **Skip
this phase entirely if the ScyllaDB half just ran in the same session** — its
re-entry, corpus and harness build already satisfy everything below except the
`osrate` binary in Phase 4.

### 2a. SSH and the private IPs

Public IPs are reassigned on every start; private IPs are not. Read the new
public IPs from the console's "Public IPv4 DNS" column, then:

```bash
sed -i '/^Host fts-harness$/,/^$/ s/^    HostName .*/    HostName <new-harness-ip>/' ~/.ssh/config
sed -i '/^Host fts-sut$/,/^$/     s/^    HostName .*/    HostName <new-sut-ip>/'     ~/.ssh/config
ssh -o StrictHostKeyChecking=accept-new fts-harness true
ssh -o StrictHostKeyChecking=accept-new fts-sut     true

# Confirm the private IPs, do not assume them.
ssh fts-harness hostname -I     # expect 172.31.38.237
ssh fts-sut     hostname -I     # expect 172.31.47.166
```

`osrate` reaches `http://<sut>:9200`. The SUT's private IP is also hard-coded
in `docker/.env.sut` as `SCYLLA_BROADCAST_RPC` and `SCYLLA_VS_URI`, which this
half does not use — but leave them correct, because the file is captured into
every arm's manifest.

### 2b. The instance store, on both boxes

```bash
for h in fts-harness fts-sut; do
  ssh $h 'set -e
    sudo mkfs.xfs -f -q /dev/nvme0n1
    sudo mkdir -p /mnt/nvme && sudo mount /dev/nvme0n1 /mnt/nvme
    sudo chown ec2-user:ec2-user /mnt/nvme
    sudo systemctl restart docker'
done
```

`daemon.json` points docker's `data-root` at the instance store, so both
OpenSearch images re-pull on the first `os-up`. **`os-disk-refresh1`'s segments
live on the `opensearch-data` volume, which is on that same instance store and
goes with every stop** — which is correct for this arm, since it measures a
build and nothing about restart.

> **One-time, on the harness, at the first start after 2026-09-11.** The root
> EBS volume was grown 8 GiB → 32 GiB and Linux does not pick that up on its
> own. **Identify the root device first** — the step above formats
> `/dev/nvme0n1` as the *instance store*, so the EBS root is a different nvme
> device and `growpart` against the wrong one is destructive:
>
> ```bash
> ssh fts-harness 'findmnt -no SOURCE /; lsblk'   # e.g. /dev/nvme1n1p1
> ssh fts-harness 'sudo growpart <root-disk> 1 && sudo xfs_growfs / && df -h /'
> ```

### 2c. The clocks, measured rather than assumed

Every rung's CPU and RSS come from a join between the SUT's probe timestamps
and the harness's stderr stamps, so a skew larger than one probe tick puts a
level's samples on its neighbour.

```bash
ssh fts-sut date +%s.%N; ssh fts-harness date +%s.%N
```

chrony keeps this in the microseconds. **Above 1 s the join is refused rather
than padded** — padding pulls the adjacent rung's peak in. Record the number in
`$R/env/clock-skew.txt`.

### 2d. The bench checkout and the venv on the harness

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
```

**`.venv/bin/python3`, never the box's own `python3`.** Amazon Linux 2023 ships
3.9, `ftsbench.runmeta` is 3.10 syntax, and `probe_windows` imports it — so a
bare `python3` dies on the import at the one moment an arm cannot be repeated
cheaply.

## Phase 3 — the corpus, on the harness and only there

`osrate` reads the corpus locally through `--corpus`; nothing streams it and
the SUT never sees a line of it. No corpus, no arm.

| Path | Survives a stop? | What |
|---|---|---|
| `~/corpus.jsonl.zst` (harness root EBS) | **yes** | ~10.2 GB, `pzstd -10` |
| `/mnt/nvme/data/corpus.jsonl` (instance store) | **no** | 35,448,823,550 bytes, re-made on every start |

```bash
ssh fts-harness 'set -e
  mkdir -p /mnt/nvme/data
  pzstd -d -p 8 -f -o /mnt/nvme/data/corpus.jsonl ~/corpus.jsonl.zst
  sha256sum /mnt/nvme/data/corpus.jsonl'
# expect 1700bb6c9b2652cf7b248e8caff7bfecc54fd2376e9a75e43379aaa79c50c432
```

**~1.5–2 min**, bounded by gp3's 125 MB/s baseline read. The sha256 is
`../FREEZE.md`'s and is what proves the bytes are the frozen corpus rather than
a re-download that drifted. **Check it every time.**

**It is the same file, same bytes, as the ScyllaDB half reads** — the "every
document exactly once, same set per engine" invariant. `osrate` does not read
the line's `uuid` (it is ScyllaDB's partition key), so one file serves both
halves.

**If the archive is not on the root volume**, re-stage from the Swedish
Wikimedia mirror (~36 min at ~215 MB/s), run `prepare_corpus`, verify against
`../FREEZE.md`, then `pzstd -10` the result back to `~/corpus.jsonl.zst` so the
next start is 2 minutes instead of 36. Not from the laptop (which does not hold
enwiki) and not from S3 (the harness has no instance profile). Budget the
36 min into the session if the archive's presence has not been confirmed.

**Do not use `HARNESS-AWS-RUNBOOK.md` Phase 4 here.** That runbook *generates*
a synthetic corpus at enwiki's mean line length, which is right for a null-sink
run where no document is ever indexed and wrong for every arm here: BM25 term
statistics, segment merges and the analyzer all depend on real text.

## Phase 4 — build the harness, then freeze it

```bash
ssh fts-harness 'cd /mnt/nvme/work/bench/build-rate/opensearch && . "$HOME/.cargo/env" \
  && CARGO_TARGET_DIR=/mnt/nvme/work/target-os cargo build --release --locked'
```

If the Rust toolchain went with the stop:

```bash
ssh fts-harness 'curl --proto "=https" --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --profile minimal && . "$HOME/.cargo/env" && rustc --version'
```

**The target directory is `target-os`, not `target`.** Each binary keeps its own
`Cargo.lock` beside its own `Cargo.toml` because its `build.rs` reads that lock
to stamp the linked driver version into every CSV header; sharing a target
directory with the ScyllaDB half invites a rebuild that moves the other half's
recorded version.

Record, into `$R/env/`, **before** measuring:

```bash
ssh fts-harness 'cd /mnt/nvme/work/bench/build-rate && find . -type f \
  \( -name "*.rs" -o -name "Cargo.*" \) | sort | xargs sha256sum | sha256sum' \
  > "$R/env/harness-tree.sha256"
git -C ~/Projects/Scylla/p99/bench log -1 --format='%H %s' > "$R/env/bench-commit.txt"
git -C ~/Projects/Scylla/p99/bench status --short build-rate >> "$R/env/bench-commit.txt"
```

**Do not rebuild once an arm has run.** A rebuild mid-campaign makes the arms
incomparable.

### The sweep script

```bash
ssh fts-harness 'cat > ~/run-os-arm.sh << "SCRIPT"
#!/bin/bash
# One osrate arm: every rung of the ladder, N times, against the SUT.
# ONE INVOCATION PER RUNG, because --max-docs is a per-invocation value and
# this campaign gives every rung its own document count. The sweep name carries
# the rate, which is what keeps the probe slicer keys unique.
# stderr is timestamped per line so every point window can be cut out of the
# resource probe, exactly as on the ScyllaDB side. That log is the only wall
# clock the harness produces.
set -u
ARM="$1"; shift
REPS="${REPS:-3}"
# rate:max_docs -- max_docs is rate x 120, so every rung loads for 2 minutes.
RUNGS="${RUNGS:-1024:122880 2048:245760 4096:491520 16384:1966080 32768:3932160}"
CAP="${CAP:-128}"
BATCH="${BATCH:-1}"
CORPUS="${CORPUS:-/mnt/nvme/work/corpus.jsonl}"
SAMPLES_DIR="${SAMPLES_DIR:-}"
SINK_URL="${SINK_URL:-http://172.31.47.166:9200}"
INDEX="${INDEX:-wiki-articles}"
# The analyzer probe is the one route the null sink does not answer, so the
# null-sink campaign suppresses it. Against a REAL OpenSearch it must stay on:
# set RESET_FLAGS= empty on every run line here. No apostrophes in this
# script: the whole heredoc is inside a single-quoted ssh command, and one
# would end the quote. It is this half of the campaign equivalent of the
# verify_arm check on the ScyllaDB side.
RESET_FLAGS="${RESET_FLAGS:---no-analyzer-check}"
OUT_DIR="${OUT_DIR:-/mnt/nvme/work/results-os}"
BIN=/mnt/nvme/work/target-os/release/osrate

mkdir -p "$OUT_DIR"
WINDOWS="$OUT_DIR/os-windows.tsv"
[ -f "$WINDOWS" ] || printf "arm\tbatch\trate\tmax_docs\trep\tstart_epoch\tend_epoch\texit_code\tcsv\n" > "$WINDOWS"
stamp() { awk "{ printf \"%d\t%s\n\", systime(), \$0; fflush() }"; }

for rep in $(seq 1 "$REPS"); do
    for rung in $RUNGS; do
        rate="${rung%%:*}"; docs="${rung##*:}"
        sweep="$ARM-r$rate-b$BATCH"
        csv="$OUT_DIR/$sweep-rep$rep.csv"
        log="$OUT_DIR/$sweep-rep$rep.stderr.tsv"
        SAMPLES=()
        if [ -n "$SAMPLES_DIR" ]; then SAMPLES=(--samples-dir "$SAMPLES_DIR/$sweep-rep$rep"); fi
        echo "######## arm=$ARM rate=$rate docs=$docs rep=$rep $(date -u +%H:%M:%S)"
        start=$(date +%s)
        "$BIN" --corpus "$CORPUS" --target-rate "$rate" --concurrency "$CAP" \
               --queue-depth "${QUEUE_DEPTH:-1}" --batch-size "$BATCH" \
               --max-docs "$docs" "${SAMPLES[@]}" \
               --url "$SINK_URL" --index "$INDEX" --out "$csv" $RESET_FLAGS "$@" 2>&1 | stamp > "$log"
        code=${PIPESTATUS[0]}
        printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
            "$ARM" "$BATCH" "$rate" "$docs" "$rep" "$start" "$(date +%s)" "$code" \
            "$csv" >> "$WINDOWS"
        grep -E "docs in" "$log" | tail -4
    done
done
SCRIPT
chmod +x ~/run-os-arm.sh'
```

Three defaults every run line here overrides, and all three are silent
failures if missed:

| Default | Override | Why |
|---|---|---|
| `RESET_FLAGS=--no-analyzer-check` | **`RESET_FLAGS=`** (empty) | the analyzer check is this half's arm gate, and the default suppresses it |
| `CORPUS=/mnt/nvme/work/corpus.jsonl` | `/mnt/nvme/data/corpus.jsonl` | the default is the *synthetic* runbook corpus |
| `BATCH=1` | `BATCH=1024` | batch 1 is the null-sink campaign's pin, not this one's |

**`RUNGS` is the fourth and it is the one that is deliberately *not* overridden
on a measured run.** It defaults to the campaign ladder, rate and document count
together, so the run line carries neither and there is nothing there to mistype
or to lose off the wrong arm. The consequence is that the ladder is a property
of **the script on the harness**: a stale `run-os-arm.sh` is a wrong ladder
rather than an error, and § 6d says where that shows.

**`MAX_DOCS` is gone as a variable.** It was one campaign-wide constant; it is
now the second field of each rung and there is no single value left to set. A
run line still carrying `MAX_DOCS=` is being ignored, silently — which is the
one regression this restructure introduced, and the reason the rung line in
§ 6d is worth reading.

`QUEUE_DEPTH` is not in the table because every run here is a rate run and the
script pins it at 1. Override it only to test the read-ahead arithmetic, and
never on a measured run.

**`SAMPLES_DIR` is now a variable rather than a forwarded flag.** With one
invocation per rung, a single `--samples-dir` on the run line would have fifteen
runs writing into one directory; the script appends `$sweep-rep$rep` so each
rung's per-second series lands on its own. Keep it **out of** the points
directory: the renderer globs `points/*.csv` and would read a per-second series
as a set of points (`core/src/cli.rs`).

**One `osrate` guard is no longer reachable and that is worth knowing.** It
refuses `--target-rate` together with a multi-valued `--concurrency` at parse
time (`core/src/sweep.rs`) — exit 1, no index reset, no billed level. The script
now passes a single rate and a single cap on every invocation, so nothing it can
be handed provokes it. It remains the backstop for a hand-run `osrate`.

## Phase 5 — the ladder, the document bound, and the smoke

### The ladder

Five rungs, declared, with `--concurrency 128` as the in-flight cap and N=3.
**Every rung carries its own `--max-docs`, set so that every rung loads for two
minutes:**

    max_docs = rate x 120

| Offered rate | `--max-docs` | Load time |
|---|---|---|
| 1,024 | 122,880 | 120 s |
| 2,048 | 245,760 | 120 s |
| 4,096 | 491,520 | 120 s |
| 16,384 | 1,966,080 | 120 s |
| 32,768 | **3,932,160** | 120 s |

**There is no calibration pass.** The rates are a constant of the campaign, so
nothing has to be measured before the measurement. That is what removed the
`C_max` derivation, the concurrency ladder that fed it, the `calibration/`
subtree and the cross-half ordering the derivation forced. Every arm in both
runbooks is drawn on this grid — which is all a shared x axis ever needed. The
requirement was that the arms share a grid, never that the grid be derived.

**The grid is geometric and it has a gap.** 1024 → 2048 → 4096 doubles, then
4096 → 16384 **quadruples**, then 16384 → 32768 doubles. **8192 is absent.** The
chart spaces rungs by value, so a shape read across the 4096 → 16384 segment is
read across a 4x step with no rung inside it. Say so in the caption.

**A declared ladder can be in the wrong place and nothing here will notice.**
The derived grid it replaces straddled the fastest arm's ceiling by
construction; this one is a prior about where the engines are. If every rung
sits below every arm's ceiling, every curve is a straight line on the diagonal,
`achieved_offered_ratio` is ~1.0 everywhere and **the chart shows no knee at
all**. The derived ladder was illustrated at 20,000–140,000 docs/s and 32,768 is
below that whole range, so **that is the outcome to expect.** It is reportable
as itself — *both engines kept up with every rate offered* — and it is not a
saturation result, a ranking or a knee. Record it rather than re-spacing the
ladder mid-campaign: a grid that moves between arms draws arms that cannot be
read against each other.

Record the five rates and their document counts in `$R/env/rate-ladder.txt` at
the start of the session, stated as **declared, not derived**.

### What the per-rung document bound costs

**It ends the campaign's "every rung ingests the identical documents"
invariant.** That was the reason for a single campaign-wide `--max-docs`, and it
is gone: each rung now reads a different-length prefix of the frozen corpus.

**Cross-arm reading survives intact. Reading along x does not.**

| Comparison | Still clean? |
|---|---|
| Two arms at the **same** rate (R4 ↔ `os-disk-refresh1`, R2 ↔ R4) | **yes** — same rate means the same `--max-docs` on every arm, so the two sides ingest the identical documents. **This is the campaign's only cross-engine read and it is unaffected** |
| One arm **across** rungs — the shape of its own curve | **no** — offered rate and index size now move together |

**The confound has a known direction and this repository has measured it.**
Index rate falls as an index grows, because merge pressure grows with it, and
`../BUILD-RATE-LOOP.md` recorded the engine ranking *inverting* between 1.2M and
the 8.97M-document corpus. On this ladder the floor rung builds a
122,880-document index and the top rung builds a 3,932,160-document one — a 32x
span — so **a curve that sags at high offered rate cannot be attributed to the
rate**: a larger index would sag there too. That belongs in the footer of every
chart drawn from this half, not in a caveat nobody reads.

The rungs are **nested prefixes**, not disjoint samples: 122,880 ⊂ 245,760 ⊂
491,520 ⊂ 1,966,080 ⊂ 3,932,160, all read from the head of the same frozen
corpus. That is the weakest form the loss could take — no rung sees a document
another did not — but it is still a second variable on the x axis.

**The alternative was 12 hours.** At the previous campaign-wide
`--max-docs 3500000` the floor rung alone ran 57 minutes and this half came to
~12 h of billed fleet. Two minutes a rung puts it at ~1 h of load. **That trade
— a confound along x in exchange for a session that fits in a sitting — is the
decision this section records, and it is reversible only by re-running every arm
in both runbooks.**

### The document bound — per rung, and the top rung is the one that binds

The bound is `rate x 120` per rung, not one campaign-wide constant. What it must
still clear is unchanged: **R4's ramindex ceiling of 4,025,699 documents**
(12 GiB of tmpfs). The enwiki corpus is 8,967,625 documents (`../FREEZE.md:55`),
so a literal whole-corpus rung would kill R4 — half of the campaign's only
cross-engine read. **This is the only hard ceiling in the campaign and it is on
this half's arm.**

**Only the top rung comes near it, and its margin is the tightest the campaign
has ever run.**

| Bound | Documents | Clears the 4,025,699 ceiling by |
|---|---|---|
| the old high sweep | 2,621,440 | 35% |
| campaign-wide `3500000` (retired) | 3,500,000 | 13% |
| campaign-wide `4000000` (retired 2026-09-16) | 4,000,000 | 0.64% |
| **this ladder's top rung** | **3,932,160** | **2.3%** |

The ceiling is itself a derivation — 12 GiB of tmpfs divided by a *mean*
document size — not a counted limit. **0.64% was inside the uncertainty of the
number it was being checked against**, which is why it was retired; 2.3% is
93,539 documents and is better than that, but it is **not** the 13% the 3.5M
bound bought, and a corpus whose mean document runs 2% large puts R4 into ENOSPC
mid-rung on billed fleet time. The pre-flight rung below is what has to clear it
in practice, and **if it does not, the 120 s budget drops for the top rung in
both runbooks** — for every arm, never for the arm that fell short.

Working against the margin: `_source: false` on R4, and the 1 s cadence cutting
roughly three times as many segments as 3 s did before merges catch them up —
that transient segment space comes out of the same tmpfs.

**`--duration` does not exist**, which is why the two-minute budget is expressed
as a document count per rung rather than as a time limit. Do not reach for it.

### What a rung costs, and where to add one

**Every rung costs the same 120 s, by construction** — that is what the per-rung
document bound buys. The ladder is **600 s a rep, 30 min an arm at N=3, and 1 h
of load for this half's two arms.**

| | Load |
|---|---|
| a rung | 120 s |
| a rep (5 rungs) | 600 s |
| an arm (N=3) | 1,800 s — 30 min |
| this half (2 arms) | 3,600 s — 1 h |
| the campaign (5 arms) | 9,000 s — 2.5 h |

**A rung is now priced in documents, not in time, and that inverts the old
advice.** Under a campaign-wide bound the floor rung was 39% of a rep and the
lesson was "cut at the bottom". Here **every rung costs the same**, so adding or
dropping one changes the ladder by a flat 120 s per rep and the only question is
what the rung is worth. Cut the rung you need least, not the slowest one.

**`max_docs / rate` is a floor, not the wall.** A rung the arm cannot keep up
with costs `max_docs / achieved` instead, and the 120 s is the *offered*
duration — a saturated rung runs longer. Reset and settle sit on top of every
rung either way, and with one invocation per rung there are now **15 resets an
arm rather than 5**, which is the overhead this restructure added.

At a 1 s refresh every rung covers ~120 cadence intervals, so the four-cadences
rule is nowhere near binding on any rung — which is the other thing a flat time
budget buys: the shortest rung is no longer the one to check.

**`--keep-warmup` on every chart command**: the ladder carries no throwaway
rung, so the floor rate is a measured point and the renderer's default first-row
drop would delete it.

### What the old grid section carried, and what became of it

A reader who worked from the previous version of this runbook knew a set of
numbers that are now **retired**, and they are named here rather than quietly
deleted:

| Retired | Why it is gone |
|---|---|
| the `4 8 16 32 64 128` grid as the **published** axis | it is gone outright — it survived as Phase A's calibration ladder and Phase A is retired two rows down; a request means 1,024 documents here and 1 on the other half, so concurrency was never a shared unit |
| the **600,000 / 2,621,440** split | superseded twice — first by one campaign-wide `--max-docs`, now by a per-rung bound of `rate x 120` |
| **one campaign-wide `--max-docs`**, and the "every rung ingests the identical documents" invariant it existed to hold | each rung now has its own document count so that every rung loads for 120 s. **The invariant is genuinely lost, not relocated** — see "What the per-rung document bound costs" |
| the **Phase A calibration pass**, `C_max`, and the `0.12–1.20 x C_max` derivation | the ladder is declared, so there is nothing to derive and nothing to calibrate. The cross-half ordering constraint and two of the four stack cycles went with it, and so did `calibration/` |
| the **seven uniformly spaced rungs** | five declared rungs, geometrically spaced, with 8192 absent. A caption saying "uniform" is describing the retired grid |
| the **≥20-`_bulk`-requests-per-worker floor**, and `2,621,440 = 20 × 128 × 1024` derived from it | a closed-loop artifact. It existed because a level had to carry enough `_bulk` requests to have workers, and it is what produced the rehearsal's *flat* R4 curve from `c=16` to `c=32` and again from `c=64` to `c=128` — **a knee that did not exist**. A paced rung is bounded by documents and a rate, and the whole failure mode goes with the axis |
| the two disjoint sub-sweeps | one ladder per arm per phase |
| the `16`→`32` budget seam, and the `c=32` overlap check that used to guard it | there is one budget, so there is no seam. **This is the one retirement that removes a stated limit rather than adding one** |
| "read-ahead is half the level" at depth 10 | `--queue-depth 1` under pacing puts it at 3.3% of the top rung, and nothing here runs at depth 10 at all |
| "if a budget moves it moves for every arm in that sweep" | still true, and still campaign-wide: if the 120 s rung budget moves, it moves for every arm in **both runbooks** |

**What is *not* retired is the lesson.** The request-count plateau was a
generator artifact reported as an engine result, and the rate ladder's answer to
that class of defect is the `generator_saturated` flag and
`achieved_offered_ratio` — a named column on every row instead of a plateau
nobody could see.

### The settle timeouts — every arm runs the defaults

`osrate` needs no override at any cadence: `_stats` reports accepted and
searchable separately and its idle rule watches the former. The defaults
(`--index-idle-timeout 15`, `--index-settle-timeout 180`) stand, and at a 1 s
cadence they carry more margin than they did at 3 s, not less — 15 idle
cadences and 180 settle ones. **An unsettled OpenSearch point at a 1 s cadence
is therefore a finding, not a build waiting on its next refresh.** The timeouts
in force are recorded in every CSV header, so a reader can check that an arm
ran the defaults rather than take this section's word for it.

### The in-flight cap, and why it differs per engine

Under a rate ladder `--concurrency` is no longer an axis. It is a **single
in-flight cap**: a ceiling on outstanding requests, there so that a rung whose
engine has stopped keeping up cannot run the harness out of memory instead of
reporting saturation.

**`--concurrency 128` on `osrate`, `--concurrency 512` on `scyllarate`.** The
cap binds by Little's Law — `in_flight = requests/s × latency` — and a request
is 1,024 documents here and one on the other side, so the same offered rate asks
for ~1,024x fewer requests here. That is measured, not assumed: against
`../engine-mock` at one identical offered rate of 50,000 docs/s, `in_flight_peak`
came out at **2** on the `osrate` side and **402** on the `scyllarate` side.

**A shared cap would be the axis asymmetry in a new place.** A cap of 8 would
bind hard on every ScyllaDB rung and never bind on one here, and the ScyllaDB
curve would then be a picture of the cap. Each half gets the cap its request
shape needs, and `in_flight_peak` in the CSV is what proves the cap did not
bind — see the gate table.

**128 is generous here on purpose.** It is far above the mock's peak of 2 and it
is also what keeps the read-ahead arithmetic readable at `queue_depth × 128 ×
1024`. `../engine-mock` is a mock: those two numbers place the caps and
**neither is an engine number.** A real engine has higher latency than a mock,
so the real `in_flight_peak` will be higher, which is exactly why the gate reads
it per rung rather than trusting the derivation.

### Smoke — 2 rungs × 2 arms, the real rates at a 20k bound

Run the full Phase 6 shape for each arm into a throwaway `OUT_DIR`:
`RUNGS="1024:20000 32768:20000" REPS=1` — the ladder's own floor and top rates
at a document count that makes them 20 s and 0.6 s instead of 120 s each.
**Smoke the real rates rather than invented ones**; it is free, and it is the
only cheap place a rate the engine refuses outright shows up. Gate on:

- every point complete, no failed inserts and **no rejected requests** — a 429
  in the first failure is queue rejection, not saturation;
- `osrate`'s analyzer check passes (`RESET_FLAGS=` empty);
- **both arms' preamble line reads `refresh_interval=1s`** (§ 6b) — a run line
  that lost its `--refresh-interval` runs R4 at the ramindex config's own 3 s
  and writes a complete, plausible ladder wearing a 1 s label, so the smoke is
  where that is caught rather than the ladder;
- **R4 does not hit ENOSPC on its tmpfs**;
- **the rate run's header reads `latency_basis=intended_start` and
  `queue_depth=1`, and its rows carry all five of `target_docs_per_s`,
  `achieved_offered_ratio`, `queue_p99_ms`, `in_flight_peak`,
  `generator_saturated`** — populated, not blank. Blank is what a
  concurrency-ladder row writes, so a blank here means the invocation lost its
  `--target-rate`;
- **the level line reads `[1/1] concurrency=128 target_docs_per_s=<rate>
  batch=1024 (131072 docs in flight)`** on every invocation. **`[1/1]`, not
  `[1/5]`** — one rung per invocation is what makes the probe keys unique, and a
  `[1/5]` means the harness is carrying a `run-os-arm.sh` that still passes the
  whole ladder to one `osrate`;
- **two stderr logs per arm per rep, named `<arm>-r<rate>-b1024-rep<n>.stderr.tsv`**,
  with the rate in the name — `r4-r1024-b1024-rep1.stderr.tsv`. That name *is*
  the probe join key;
- **the probe's own smoke**, which is where a wrong container name costs two
  minutes instead of an arm: the probe is running and its file is growing, every
  sample reads `source=cgroup-anon`, and the recorded clock skew is under 1 s.
  **`probe_windows` now runs to completion** — one window per rung per rep, no
  `empty` note, no duplicate-key exit. That is the whole of the old blocker and
  this is where it is confirmed cleared rather than assumed; if it still exits 1
  on duplicate keys, a sweep name lost its `-r<rate>` and the fix is in
  `run-os-arm.sh`, not in `probe_windows.py`.

Delete the smoke output before the real ladders. It is not campaign data.

### Bound check — the top rung on R4, and it is the campaign's tightest

The `rate x 120` bound is decided and is **not re-litigated on the fleet**; the
derivation is above. **The one thing that can still refuse it is R4's tmpfs, and
it is on this half.** Run the ladder's **top** rung on R4 —
`RUNGS="32768:3932160" REPS=1`, the largest document count in the campaign, the
fastest fill and the most transient segment space — for the three things a
laptop could not answer:

1. **R4's tmpfs holds 3,932,160 documents against its 4,025,699 ceiling with the
   14 GiB heap beside it in the same 28g cgroup.** That is 93,539 documents of
   slack, **2.3%**, against a ceiling that is itself a derivation. **This is the
   check the campaign's document bound rests on, nothing has ever touched it,
   and the per-rung bound made it 5.7 points tighter than the retired 3.5M
   constant did.** ENOSPC on the tmpfs, or a breach of the 28g cgroup, means the
   120 s budget drops for the top rung in both runbooks;
2. the loader's resident set stays far under the ~8 GB guard — expect ~0.52 GB
   at `--queue-depth 1`, against 5.17 GB at the old default, which is the other
   thing the depth change bought;
3. the build covers ≥4 refresh intervals at 1 s — a formality at 120 s, checked
   because it is the same rule every other arm clears.

**There is no floor-rung check any more.** Under a campaign-wide bound the floor
rung was the campaign's longest point and worth timing; at `rate x 120` it is
120 s like every other rung, and what it would have falsified — a `C_max` read
off the wrong arm — no longer exists. `achieved_offered_ratio` at 1,024 docs/s
is read off the ladder itself.

## Phase 6 — the ladders, one arm at a time

Order: **R4, then `os-disk-refresh1`**. The stack is recreated between arms —
`os-down` then `os-up`, **never restarted in place** — because the ramindex
overlay is what puts the segments on tmpfs and a restart in place would leave
the previous arm's index config underneath.

Each arm is: `os-up` → **confirm the config** → **start the probe** → **the
ladder** → **close the arm out** (probe stop, slice, CPU verdict, all on the
harness while the stack is live) → `pull_arm` → `os-down`.
**The arm is not finished until its `pull_arm` returns zero.**

**One pass over the arm order, and two stack cycles on this half.** The declared
ladder bought those back: under a derived grid every arm in the campaign had to
be calibrated before any arm could be measured, which meant two passes here and
four stack cycles. The requirement that arms share an x grid did not go away —
it is satisfied by declaration now instead of by ordering.

**The stack is not recreated between rungs.** One arm is 15 `osrate`
invocations against one live OpenSearch; each resets the index itself, and the
arm's config is what `os-up` put there.

### 6a. Bring the stack up with the arm's config

`source tools/fleet_env.sh` on the harness sets `DOCKER_HOST=ssh://<sut>` and
`COMPOSE_ENV=docker/.env.sut`, so every `docker compose` call reads the compose
files and env **locally** and creates the containers **on the SUT**.

| Arm | On top of `docker/.env.sut` |
|---|---|
| R4 | `OS_RAM_INDEX=1`, `OS_RAM_INDEX_SIZE=12884901888`; `refresh_interval: 1s` set by `osrate --refresh-interval` at index create |
| `os-disk-refresh1` | `OS_RAM_INDEX` **unset**, `osrate --index-config disk`, data on the `opensearch-data` volume; `refresh_interval: 1s` set the same way, which is also what that config ships |

`OS_RAM_INDEX=1` adds the `docker-compose.opensearch.ramindex.yml` overlay
(`Makefile`'s `COMPOSE_OS_OVERLAY`).

```bash
# R4
ssh fts-harness 'set -eu
  cd $BENCH && . tools/fleet_env.sh
  OS_RAM_INDEX=1 make os-up && OS_RAM_INDEX=1 make os-wait'

# os-disk-refresh1
ssh fts-harness 'set -eu
  cd $BENCH && . tools/fleet_env.sh
  make os-up && make os-wait'
```

`--refresh-interval 1s` is passed on **every** run line. It is not a variable in
this campaign; it is pinned at OpenSearch's own default so that R2 ↔ R4 is a
matched-cadence comparison — the ScyllaDB half runs
`VS_FTS_COMMIT_INTERVAL=1s` — and nothing else. **Cadence cost is therefore
unmeasured and no chart off this campaign may claim it.**

**Passing the flag is not the same as leaving the cadence alone, and that is
the whole reason it is on the line.** The two shipped index configs do not
agree: `../opensearch/index-config-ramindex.json` sets `refresh_interval: 3s`
and `../opensearch/index-config.json` sets `1s`. Omit the flag and R4 runs at
3 s while `os-disk-refresh1` runs at 1 s — the two arms would then differ in
cadence as well as in where the index lives, and S2a would stop being a
single-variable pairing. **Editing those two files to agree is the worse fix:**
they are the null-sink campaign's configs as well, and `include_str!` compiles
them into the binary (`opensearch/src/reset.rs`), so changing one means
rebuilding the harness — which Phase 4 forbids once an arm has run. The flag
costs nothing in exchange: `osrate` applies the interval at index create, so
there is no window in which documents were indexed under the other value
(`with_refresh_interval`).

**Nor is 1 s the same as unset.** An index created with no `refresh_interval`
goes search-idle after 30 s without a query and stops refreshing on a timer at
all (`opensearch/src/client.rs`, `IMPLICIT_REFRESH_INTERVAL`) — and a loader
is the only thing talking to this index, so it would publish nothing and every
level would report the harness's final refresh as the entire build. **1 s is
the default written down, never the default left out.**

**This closes a gap the ScyllaDB half opened and recorded.** That half moved to
1 s first and carries a section saying R2 ↔ R4 is no longer cadence-matched
because this one still ran 3 s; with both halves at 1 s that is no longer true.
**The decision has not been cut back into
[`INDEX-RATE-MATRIX-PLAN.md`](INDEX-RATE-MATRIX-PLAN.md)**, whose arm table
still reads 3 s, nor into `INDEX-RATE-SCYLLA-RUNBOOK.md`'s "The cadence is 1 s,
and R2 ↔ R4 is no longer matched". **Settle it in the plan before the fleet
is started** — this file's own rule is that the plan is right and this file
has drifted, and that rule cannot adjudicate a cadence the plan has not yet
been told about.

### 6b. The config gate

```bash
ssh fts-harness 'cd $BENCH && . tools/fleet_env.sh
  docker exec fts-bench-opensearch df -h /usr/share/opensearch/data | tail -1'
```

| Arm | Expect |
|---|---|
| R4 | a **tmpfs** mount of ~12 GiB on the data path |
| `os-disk-refresh1` | the `opensearch-data` volume on the NVMe, no tmpfs |

There is **no `verify_arm.py` on this half** — `ftsbench/target.py` registers no
OpenSearch targets. **The analyzer check inside `osrate` is this half's
equivalent and it stays on** (`RESET_FLAGS=` empty). It runs per level, at
reset, and fails the run rather than degrading.

`osrate` sets `refresh_interval` and `_source: false` at index create, so the
refresh cadence is confirmed from the run's own stderr rather than from the
container. **The preamble line must read `refresh_interval=1s` on both arms**,
and it is worth reading rather than assuming: that value is read back off the
index's own settings (`opensearch/src/client.rs`, `read_cluster`), not echoed
from the flag, so it is the one place a run line that lost its
`--refresh-interval` shows up — as `3s` on R4, which is the ramindex config's
own value and looks like nothing being wrong. `refresh_interval_requested` in
the CSV header says what was asked for; the preamble says what the index got.
A mismatch voids the arm, the same way a wrong data mount does.

### 6c. Start the resource probe

One probe per arm, sampling the container at 1 Hz. It is the one component
`DOCKER_HOST` cannot carry — it reads `/sys/fs/cgroup` where it runs — so it
goes through `tools/sut_probe.sh`, which starts it detached on the SUT.
**Running it on the harness instead records the generator box's idle cgroups
and reports them as engine numbers**; this repository has paid for that mistake
once (`../BUILD-RATE-LOOP.md`).

**Do not pass `--output`** — the wrapper appends it. The engine URL is
`127.0.0.1` because the probe is on the SUT.

```bash
a=r4    # r4 | osdisk
ssh fts-harness "set -eu
  cd \$BENCH && . tools/fleet_env.sh
  mkdir -p /mnt/nvme/work/probe
  tools/sut_probe.sh start /mnt/nvme/work/probe/$a.jsonl \
      --engine opensearch --containers fts-bench-opensearch:opensearch \
      --os-url http://127.0.0.1:9200 --os-index wiki-articles \
      --interval 1 --duration 0 --label 'index-rate $a' \
      --corpus /mnt/nvme/data/corpus.jsonl"
```

It adds no wall clock and no rung. **It does share the SUT's eight cores with
the engine, and that reaches the footer.**

**There is no single memory number for these two arms**, which is why
`--memory-read` is required on the slicer:

| Arm | Read | Why |
|---|---|---|
| **R4** | **`anon + shmem`** | the ramindex is **tmpfs**, which is shmem and not anon — **reporting `rss_bytes` alone hides up to 12 GiB of index** |
| `os-disk-refresh1` | `anon`, with `cache` beside it | the index is on the NVMe, so it is page cache |

`rss_bytes` is cgroup v2 `memory.stat` **`anon`**, never `memory.current`,
which includes page cache and would flatter whichever engine touched less disk.
`cache_bytes` (`file`) and `shmem_bytes` (`shmem`) are carried separately so
the total is recoverable. `cpu_cores_used` is a rate differenced off the
monotonic `cpu.stat` `usage_usec` counter and is `null` on the first tick,
because reporting 0.0 there would draw a container that was saturated at
start-up as idle.

**The R4 row is the one that changes what the chart can say.** That arm holds a
14 GiB heap and a 12 GiB tmpfs ceiling inside one 28g cgroup — about 2 GiB of
slack — and this is the measurement that says whether it held. **R4 is the only
ramindex arm left, so a `thin` or `empty` rung on it has no sibling arm to
corroborate against.** It is named in `resource-by-rung.csv`, never dropped
from it.

### 6d. The ladder

**N=3, five rungs, every one published.** `RUNGS` carries the rates and their
document counts and is `run-os-arm.sh`'s own default, so the run line carries
neither — and there is no `MAX_DOCS` on it, because there is no longer a single
one to set.

```bash
a=r4; cfg=ramindex     # osdisk: a=osdisk; cfg=disk
ssh fts-harness "REPS=3 CAP=128 BATCH=1024 \
    CORPUS=/mnt/nvme/data/corpus.jsonl RESET_FLAGS= \
    OUT_DIR=/mnt/nvme/work/results/$a \
    SAMPLES_DIR=/mnt/nvme/work/samples/$a \
    ~/run-os-arm.sh $a --index-watch --index-config $cfg \
        --refresh-interval 1s --index-interval 0.25"
```

**Read the first two lines of the run rather than trusting the run line.**
The ladder now lives in the script on the harness, so a stale `run-os-arm.sh` is
a wrong ladder rather than an error, and this is the only place it shows:

```
######## arm=r4 rate=1024 docs=122880 rep=1 ...
[1/1] concurrency=128 target_docs_per_s=1024 batch=1024 (131072 docs in flight)
```

**`rate=1024 docs=122880`, and `[1/1]`.** A `[1/5]` means the script is still
handing the whole ladder to one `osrate`, which restores the duplicate-key
failure; any other `docs` for a given rate means the two-minute budget is not
what is running.

`--queue-depth 1` is **not** on the run line: the script pins it, so it cannot
be lost off one arm.

| Arm | Sweep names | Remote `OUT_DIR` / samples | Local destination |
|---|---|---|---|
| R4 | `r4-r<rate>-b1024`, one per rung | `…/results/r4`, `…/samples/r4` | `$R/r4/opensearch/` |
| `os-disk-refresh1` | `osdisk-r<rate>-b1024`, one per rung | `…/results/osdisk`, `…/samples/osdisk` | `$R/osdisk/opensearch/` |

**The sweep name carries the rate, and that is now load-bearing rather than
cosmetic.** `probe_windows` keys a window on `(sweep, concurrency, rep)` and
takes `sweep` from the stderr filename, so `-r<rate>` is what makes the fifteen
windows of an arm distinct — see "The probe blocker". The name also still has to
satisfy `ftsbench/verify_cpu_usage.py`'s pattern, which reads everything before
`-c<conc>-<rep>` as the configuration.

**`--index-interval 0.25`.** The default is 1.0 s, and at 120 s a rung either
value clears the three-reading floor comfortably. 0.25 is kept because it costs
nothing and because a build with under three index readings is skipped by name
in the growth chart.

**The ladder is 30 min of load per arm** (3 reps × 600 s) before reset and
settle, with **15 index resets an arm** on top — one per invocation. Call it
~45 min an arm. Run it in the background rather than polling.

### 6e. Close the arm out, on the harness, before `os-down`

**Why per arm rather than once at the end.** `os-down` removes the container
and its log goes with it. `verify_cpu_usage` reads the container's quota with
`docker inspect`, so it cannot be deferred to laptop work either: once
`os-down` has run, there is no quota left to compare the observed cores
against.

```bash
a=r4; mread=anon+shmem     # osdisk: a=osdisk; mread=anon+cache
ssh fts-harness "set -eu
  cd \$BENCH && . tools/fleet_env.sh
  L=/mnt/nvme/work/logs/$a; mkdir -p \$L
  docker logs fts-bench-opensearch > \$L/opensearch.log 2>&1
  cp docker/.env.sut \$L/env.sut
  docker image inspect --format '{{.Id}} {{index .RepoTags 0}}' \
      opensearchproject/opensearch:3.8.0 > \$L/image.txt
  tools/sut_probe.sh stop /mnt/nvme/work/probe/$a.jsonl
  .venv/bin/python3 -m ftsbench.probe_windows --arm $a \
      --probe /mnt/nvme/work/probe/$a.jsonl \
      --stderr '/mnt/nvme/work/results/$a/*-rep*.stderr.tsv' \
      --out-dir /mnt/nvme/work/probe/$a --memory-read $mread \
      --table \$L/resource-by-rung.csv
  .venv/bin/python3 -m ftsbench.verify_cpu_usage --data-dir /mnt/nvme/work/probe/$a \
      --containers fts-bench-opensearch \
      --output-json \$L/cpu-utilisation.json"
```

**All of it runs inside the `ssh`, while the stack is still up.**

**The `probe_windows` line runs to completion now**, because one invocation per
rung makes `(sweep, concurrency, rep)` unique — see "The probe blocker". It
should report **15 windows** for the arm. `set -eu` is in force, so a duplicate
key still stops the close-out dead; if that happens, the sweep names lost their
`-r<rate>` and the fix is in `run-os-arm.sh`. **Do not patch
`probe_windows.py`** — it is out of scope for this campaign and it no longer
needs to change.

**`--containers fts-bench-opensearch` is not optional.** `verify_cpu_usage`'s
built-in container list is the ScyllaDB pair, which would report nothing on
either arm here.

### 6f. Pull the arm home, from the laptop

```bash
pull_arm() {                      # pull_arm <arm-dir> opensearch
    local a="$1" d="$R/$1/$2"
    test -n "$R" && test -d "$d" || { echo "no such arm directory: $d" >&2; return 1; }
    scp    "fts-harness:/mnt/nvme/work/results/$a/*"     "$d/points/"          || return 1
    scp -r "fts-harness:/mnt/nvme/work/samples/$a/"*     "$d/samples/"         || return 1
    scp -r "fts-harness:/mnt/nvme/work/logs/$a/"*        "$d/logs/"            || return 1
    scp    "fts-harness:/mnt/nvme/work/probe/$a.jsonl"   "$R/sut/cpu-$a.jsonl" || return 1
    scp -r "fts-harness:/mnt/nvme/work/probe/$a/"*       "$d/probe/"           || return 1
    # run-os-arm.sh writes its stderr log and os-windows.tsv beside the CSVs.
    # Both are the probe join's other half, so they belong with the logs.
    mv "$d"/points/*.tsv "$d/logs/" 2>/dev/null
    local n; n=$(ls "$d"/points/*.csv 2>/dev/null | wc -l)
    [ "$n" -eq 15 ] || { echo "$a: $n point CSVs, expected 15 (5 rungs x 3 reps)" >&2; return 1; }
    local w; w=$(ls "$d"/probe/cpu-*.jsonl 2>/dev/null | wc -l)
    [ "$w" -eq 15 ] || { echo "$a: $w rung slices, expected 15 (5 rungs x 3 reps)" >&2; return 1; }
    rss_breach "$d/logs/resource-by-rung.csv" || return 1
    echo "$a: home"
}

# The memory gate. On R4 a breach means the 14 GiB heap and the 12 GiB tmpfs
# ceiling did not both fit in the 28g cgroup, and the rate beside it counts
# documents the index could not hold.
rss_breach() {
    awk -F, -v OFS=, 'NR==1 { for (i=1;i<=NF;i++) c[$i]=i; next }
        $(c["mem_headroom_bytes"]) != "" && $(c["mem_headroom_bytes"]) <= 0 {
            print "BREACH", $(c["sweep"]), $(c["concurrency"]), $(c["rep"]), \
                  $(c["container"]), $(c["mem_peak_bytes"]); bad=1 }
        END { exit bad }' "$1" >&2
}

pull_arm r4 opensearch
```

**A non-zero return is the arm's gate, not a warning**: the stack that produced
it is still up, which is the only moment re-running a lost rep is cheap.

**A point CSV existing is not a finished run.** `osrate` creates `--out` at
start, so the "15 point CSVs" gate passes while the fifteenth run is still
going. Check the arm's last stderr log for its final `-> index …` line before
trusting the count.

**One CSV per rung per rep is the shape now**, not one per rep: the renderer
globs `points/*.csv` and pools them, so fifteen single-rung files draw the same
five-rung line three deep that three five-rung files used to. What changed is
that a **missing rung is now a missing file**, which the count gate catches,
rather than a short row set inside a file, which it did not.

**`cache_bytes` on `os-disk-refresh1` is exempt from the breach gate** — a
file-backed index is page cache the cgroup reclaims rather than kills. On R4
nothing is exempt: tmpfs is not reclaimable and `rss + shmem` against
`mem_limit_bytes` is the whole reading.

Then, and only then:

```bash
ssh fts-harness 'cd $BENCH && . tools/fleet_env.sh && make os-down'
```

## Phase 7 — verify both arms came home, then render

From `$R` alone, **before the stop**:

```bash
for a in r4 osdisk; do ls "$R/$a"/opensearch/points/*.csv | wc -l; done   # 15 each
ls "$R"/sut/cpu-r4.jsonl "$R"/sut/cpu-osdisk.jsonl | wc -l                # 2

# Every published row is a rate-ladder row at queue depth 1: the axis and the
# read-ahead are in the header, not inferred from the filename.
grep -h "^# latency_basis" "$R"/{r4,osdisk}/opensearch/points/*.csv | sort -u   # intended_start
grep -h "^# queue_depth"   "$R"/{r4,osdisk}/opensearch/points/*.csv | sort -u   # 1

# The ladder that RAN is the ladder that was declared, rate AND document count.
# The rungs live in the harness script, so this is the only place after the fact
# that a stale run-os-arm.sh is caught. Expect exactly the five declared pairs.
awk -F, 'FNR==1 { h=0 } /^#/ { next }
         !h { for (i=1;i<=NF;i++) c[$i]=i; h=1; next }
         { print $(c["target_docs_per_s"]), $(c["docs"]) }' \
    "$R"/{r4,osdisk}/opensearch/points/*.csv | sort -un
# 1024 122880 / 2048 245760 / 4096 491520 / 16384 1966080 / 32768 3932160

# Every rung of every arm carries a CPU and RSS reading, and none breached.
ls "$R"/{r4,osdisk}/opensearch/probe/cpu-*.jsonl | wc -l          # 30 (2 arms x 15)
ls "$R"/{r4,osdisk}/opensearch/logs/resource-by-rung.csv | wc -l  # 2
for f in "$R"/{r4,osdisk}/opensearch/logs/resource-by-rung.csv; do rss_breach "$f" || echo "^ $f"; done
grep -c ',thin$\|,empty$' "$R"/{r4,osdisk}/opensearch/logs/resource-by-rung.csv

# The batch_size COLUMN agrees with the run line on every data row -- checking
# only the header would miss a mislabelled file.
awk -F, 'FNR>1 && $8 != 1024 { print FILENAME": "$8 }' "$R"/{r4,osdisk}/opensearch/points/*.csv

# The refreshed count per arm, for the table. This is disclosed, not netted out.
grep -c 'refreshed' "$R"/{r4,osdisk}/opensearch/points/*.csv
```

**The three resource lines are what the `probe_windows` blocker takes out.**
They are left in rather than removed: the campaign's verification is what it is,
and a run that cannot execute them has not been verified.

### If both halves are under this `$R` — the campaign's charts

Run these on the laptop **with the boxes still running**. If they cannot
produce their PNGs and `--table` twins from `$R` without touching the fleet,
**the pull is not finished** — a five-minute fix now and a re-entry plus a
re-measured arm after the stop.

```bash
# Campaign-wide, across both runbooks.
ls "$R"/sut/cpu-*.jsonl | wc -l          # 5
ls "$R"/*/*/probe/cpu-*.jsonl | wc -l    # 75 (5 arms x 15)

# The primary chart -- named series are drawn in the order given, in run-table
# order, and a label is never parsed for a batch size.
.venv/bin/python3 build-rate/charts/rate_vs_offered.py --keep-warmup \
    --series "R1 scylla-buf15=$R/r1/scylla/points/*.csv" \
    --series "R2 scylla-buf376=$R/r2/scylla/points/*.csv" \
    --series "R8 scylla-buf376-disk=$R/r8/scylla/points/*.csv" \
    --series "R4 os-ramindex-refresh1=$R/r4/opensearch/points/*.csv" \
    --title "Index rate against offered rate — four arms, submitted and indexed (PRELIMINARY)" \
    --output "$R/index-rate-vs-offered.png" \
    --table  "$R/index-rate-vs-offered.csv"

# S2a -- where the index lives, both engines, RAM arm and disk arm at 1 s.
.venv/bin/python3 build-rate/charts/rate_vs_offered.py --keep-warmup \
    --series "R2 scylla-buf376=$R/r2/scylla/points/*.csv" \
    --series "R8 scylla-buf376-disk=$R/r8/scylla/points/*.csv" \
    --series "R4 os-ramindex-refresh1=$R/r4/opensearch/points/*.csv" \
    --series "os-disk-refresh1=$R/osdisk/opensearch/points/*.csv" \
    --title "Where the index lives — RAM against disk, both engines (PRELIMINARY)" \
    --output "$R/s2a-where-the-index-lives.png" \
    --table  "$R/s2a-where-the-index-lives.csv"
```

Expect `(8 lines, 40 points)` on each at the 5-rung ladder — `2 × arms` lines
and `2 × arms × rungs` points. **Eight lines read as one image** — the
execution-step-1 rehearsal rendered the real command off real CSVs and the
palette gives clearly distinct colours with the legend in two columns clear of
every curve. The two-render by-engine fallback is retired. **That rehearsal was
on the concurrency renderer**; `rate_vs_offered.py` imports its series handling,
colours and table twin from `rate_vs_concurrency.py` rather than restating them,
so an arm lands on the same line and the same colour on both — but the eight-line
legibility finding has not been re-checked on the diagonal, which adds one more
line to the plot.

**If only this half is under `$R`**, draw the OpenSearch pair alone and say so
in the caption:

```bash
.venv/bin/python3 build-rate/charts/rate_vs_offered.py --keep-warmup \
    --series "R4 os-ramindex-refresh1=$R/r4/opensearch/points/*.csv" \
    --series "os-disk-refresh1=$R/osdisk/opensearch/points/*.csv" \
    --title "OpenSearch at 1 s — tmpfs against disk, batch 1024 (PRELIMINARY)" \
    --output "$R/os-ramindex-vs-disk.png" \
    --table  "$R/os-ramindex-vs-disk.csv"
```

Expect `(4 lines, 20 points)`.

### Five things the renderer will not do for you

- **`rate_vs_offered.py`, never `rate_vs_concurrency.py`, and each refuses the
  other ladder's CSVs by name.** A concurrency-ladder row has no
  `target_docs_per_s`, and the offered-rate renderer says so and stops rather
  than drawing a blank x. With calibration retired nothing this campaign
  produces can trip it — the refusal now guards only against an older run's CSVs
  being swept into `points/`.
- **The `--table` twin's columns are new and two of them are the gate's input**:
  `series, metric, offered_docs_per_s, reps, docs_per_s_median, docs_per_s_min,
  docs_per_s_max, shortest_wall_s, saturated, in_flight_peak`. A hollow ring on
  the chart is a `saturated` rung and it is **kept, not dropped** — but read it
  against `in_flight_peak` before reading a knee off it: a peak at the
  `--concurrency` cap means the harness was the limit and the point is void.
  The renderer draws the ring; it does not make that judgement.
- **There is no budget seam to check, but there is now a document column to
  read.** The `16`→`32` seam the previous grid carried is gone. What replaces it
  is that **`--max-docs` differs per rung**, so the `docs` column moves along x
  by design and a reader comparing two rungs of one arm is comparing two index
  sizes as well as two rates — see "What the per-rung document bound costs".
- **A colour is not stable across charts.** `--series` assigns the palette by
  position in the argument list, so R4 is the fourth colour on the primary chart
  and the third on S2a. Either caption every chart so it stands alone, or keep
  one arm order across all of them.
- **A point CSV existing is not a finished run** — see 6f.

**Top-up rule.** This half has no top-up pair. **R2 ↔ R4 is deliberately
excluded** from it: a cross-engine gap that small is a finding to report, not a
spread to tighten, and topping it up invites reading a tie into it. **Nor is a
`generator_saturated` rung ever topped up on either half** — a tie between two
saturated rungs is a tie between two client ceilings.

## Phase 8 — stop the boxes

**Before stopping: every artifact is on the laptop**, because `/mnt/nvme` is
about to be destroyed. Nothing in Phase 6f or 7 can be done after this section.

Same console tab. Select both rows → **Instance state → Stop instance** → check
the dialog names **both** `k-nowacki-fts-benchmark-harness` and
`k-nowacki-fts-benchmark-sut`, leave "Skip OS shutdown" unchecked → **Stop**.

Then refresh and **confirm both rows read `Stopped` with no public IP**. Say so
explicitly in the report; "I initiated the stop" is not the same as "they are
stopped".

The `~/.ssh/config` entries now point at released IPs and must be re-pointed on
the next start.

## The final refresh — disclosed, not hidden

Every arm here runs with the harness's default: when the client has stopped,
the engine has accepted everything, and the searchable count is still short,
the harness asks the index to publish once, and `index_status=refreshed`
records every point where it did.

**Only `osrate` can ask.** Its probe answers the settle hint with a `_refresh`;
the vector-store's status endpoint offers no equivalent and `scyllarate`'s
probe keeps `core`'s default, which is never to ask (`core/src/index.rs`,
`settle_hint`). So the asymmetry is one-sided: **R4 and `os-disk-refresh1` are
credited with a publish their configured policy had not yet delivered when the
loader finished, and the three ScyllaDB arms are not** — they wait for their own
next commit.

**Dropping the 30 s arms shrank this, and the 1 s cadence shrinks it again
without removing it.** At 30 s the credited publish could be worth up to a full
30 s of build wall; at 3 s, up to 3 s; at the 1 s cadence every arm now runs,
at most 1 s against a 45–220 s level. **It is still not nothing, it still runs
one way only, and it still lands on the side of the one cross-engine comparison
this campaign has** — the three ScyllaDB arms wait for their own next commit
and are credited with nothing.

The alternative, `--no-index-final-refresh`, reports what the policy alone
delivered and was **considered and not chosen**: it turns a build that finished
just short of its next refresh into one that reports nothing. The footer states
which it was, and the `refreshed` count per arm goes in the table.

## The SUT, for the record

**The 50/50 cgroup split** — `docker/.env.sut`, applied by compose as
`mem_limit` / `cpus` / `cpuset` on each service:

| Service | `cpuset` | `cpus` | `mem_limit` | In-process budget |
|---|---|---|---|---|
| OpenSearch — `-Xms14g -Xmx14g`, `bootstrap.memory_lock=true`, memlock and nofile ulimits | `4-7` | 4 | 28g | 14 GiB heap (+ 12 GiB tmpfs ceiling on R4, counted against the same 28g) |
| database slot | `0-3` | — | — | **deliberately idle** — models the database OpenSearch deploys beside |

**What this means for the cross-engine line.** OpenSearch has four cores and
28 GiB in one container; the ScyllaDB stack has eight cores and 56 GiB across
two. The vector-store — the process doing the indexing on that side — sits on
the same four cores OpenSearch does, which is the sense in which the indexing
halves are matched. **Load-bearing for R2↔R4, now the campaign's only
cross-engine read, and irrelevant inside any chart drawn from this runbook
alone.** It reaches the footer either way.

**Images.** `opensearchproject/opensearch:3.8.0`, re-pulled from the registry
on every start.

**Networking.** `osrate` reaches `http://<sut>:9200` over the private network.

## Gates

| Gate | Rule |
|---|---|
| **Cap did not bind** | **blocking.** `in_flight_peak` read against the `--concurrency` cap (128 here). A rung with `generator_saturated=true` **and** `in_flight_peak` at the cap measured **the harness**, not the engine: the point is **void, and re-run at a higher cap** — never reported, never annotated. Saturation alone is a finding; saturation *at the cap* is an instrument reading |
| **Schedule held** | **annotating.** `queue_p99_ms` against `p99_ms` on the same row. If queueing is a material fraction of the latency, the chart is measuring the harness rather than the engine. **Pick a threshold before the fleet runs, write it into `$R/env/rate-ladder.txt` with its justification, and apply it to every rung** — this is what makes coordinated-omission safety a number rather than a claim. A rung over it is plotted and named |
| **Achieved vs offered** | `achieved_offered_ratio` ≥ 0.95, or the rung is `generator_saturated`. **Saturation is a finding, not a gap**: the rung is drawn with a hollow ring and kept, which is how a fast arm and a slow arm share one x grid and each still show its own knee. What it may not be called is the offered rate |
| Short point | a level under 3 s is not a measurement. Every rung is budgeted at 120 s, so a short point means the rung ended early rather than that the budget was thin; the renderer names every one in the footer |
| Thin series | a build with under three index readings is skipped by name in the growth chart. `--index-interval 0.25` on every sweep is what keeps it off |
| Not settled | `index_settled=false` is a lower bound: drawn hollow, the reason (`index_status`) named in the table |
| Arm took | `osrate`'s analyzer check stays on (`RESET_FLAGS=` empty); the data mount is tmpfs on R4 and the NVMe volume on `os-disk-refresh1` |
| No rejections | zero failed inserts and zero rejected requests anywhere. **A 429 in the first failure is queue rejection, not saturation** |
| Manifest | every arm directory carries the `.env` it ran with and the image id, alongside the CSVs |
| **RSS breach** | **blocking on R4, and the top rung is where it bites.** A 14 GiB heap plus a 12 GiB tmpfs ceiling inside one 28g cgroup leaves ~2 GiB, and `rss + shmem` against `mem_limit_bytes` is the reading that says whether it held. The 32,768 rung loads 3,932,160 documents — **2.3% under R4's derived tmpfs ceiling, the tightest margin the campaign has run**. The arm is re-run, not annotated. **`cache_bytes` on `os-disk-refresh1` is exempt** — a file-backed index is page cache the cgroup reclaims rather than kills |
| **CPU attribution** | **annotating, never dropping.** Per rung, the container's peak `cpu_cores_used` against its quota: `ok` at ≥0.85, `not-CPU` below it, `?` where no series covers the window. **A `?` is not a pass.** A plateau at `not-CPU` is still plotted; what changes is that it may not be described as the engine's throughput limit |
| **Probe source** | every sample reads `source=cgroup-anon`. The `docker stats` fallback is not anon-only and has no CPU counter, so **one fallback sample destroys R4's shmem reading**; `probe_windows` refuses the arm rather than reporting it |
| **Clock skew** | harness-to-SUT skew under one probe tick (1 s), measured at re-entry and recorded. The window is never padded to cover skew |

**There is no client-headroom gate.** Every rung is measured and plotted on its
own merits. The source campaign's G7 — a level clears only at ≥2x under the
measured client ceiling — is deliberately **not** carried over: it exists to
decide whether a *client* ceiling may be quoted as an *engine* number, and on
this axis the engine's searchable rate is what is quoted.

## Caveats this half carries into the write-up

- **A searchable count moves in steps.** `docs.count` advances at a refresh, so
  every build curve here is flat, flat, jump.
- **`index_lag_docs` has a floor** of `refresh_interval × docs_per_s` — one
  second of arrivals here, however fast Lucene indexes. **The number to read is
  the excess.**
- **`index_status=refreshed` means the harness asked** — see "The final
  refresh". One-sided, and it lands on the cross-engine comparison.
- **Cadence cost is not measured.** Every arm publishes at 1 s and nothing
  separates the cost of a cadence from any other cause. The ScyllaDB half runs
  the same 1 s cadence, so R2 ↔ R4 carries no cadence difference — what it
  cannot say is what a *different* cadence would have cost either engine.
- **The ladder's rates are declared, not measured.**
  `1024,2048,4096,16384,32768` was chosen before the fleet ran and is hung on
  nothing — no ceiling, no calibration pass, no reading of either engine. **A
  declared grid can sit entirely below every arm's ceiling, in which case every
  curve is a straight line on the diagonal and the chart shows no knee at all**,
  and the derived ladder it replaces was illustrated at 20,000–140,000 docs/s,
  well above this one's top rung. **On the campaign's own working numbers that
  is the expected outcome.** It is reportable as itself — both engines kept up
  with every rate offered — but it is not a saturation result, a ranking or a
  knee. `$R/env/rate-ladder.txt` records the grid as declared rather than
  derived.
- **The grid is geometric and has a gap.** 1024/2048/4096 double, 4096→16384
  quadruples, 16384→32768 doubles. **8192 is absent**, so a shape read across
  the 4096→16384 segment crosses a 4x step with no rung inside it.
- **Every rung ingests a different number of documents, and that is a second
  variable on the x axis.** `--max-docs` is `rate x 120` so that every rung loads
  for two minutes, which means the floor rung builds a 122,880-document index and
  the top rung a 3,932,160-document one. Index rate falls as an index grows, and
  `../BUILD-RATE-LOOP.md` measured the engine ranking *inverting* between 1.2M
  and 8.97M documents — so **a curve that sags at high offered rate cannot be
  attributed to the rate.** Comparing two **arms at the same rate** is unaffected,
  because the same rate means the same document count on every arm; comparing
  two **rungs of one arm** is not. This is the campaign's "every rung ingests the
  identical documents" invariant, traded for a session that fits in a sitting.
- **A saturated rung is a client reading and is drawn as one.** The hollow ring
  says the arm did not deliver what it was offered; `achieved_offered_ratio`
  says by how much. The x value of such a point is **what was asked for, not
  what arrived**, and a knee read off a run of saturated rungs is a knee in the
  generator.
- **`--batch-size 1024` is untested.** Every number here is "OpenSearch at
  batch 1024", never "OpenSearch".
- **The x axis is now a document per second on both halves, and the
  request-shape asymmetry moved into the in-flight cap.** `--concurrency 128`
  here against `--concurrency 512` on the other half is the same
  1,024-documents-per-request difference in a new place. It is no longer on the
  axis, so curve *shape* is readable across the engines for the first time — but
  only where **both** sides of a comparison clear the cap-did-not-bind gate.
  `in_flight_peak` is in every row and in the `--table` twin so a reader can
  check that rather than take it on trust.
- **The caps were placed off `../engine-mock`, not off an engine.** 2 and 402
  `in_flight_peak` at 50,000 docs/s is a mock's latency; a real engine's is
  higher and its peaks will be too.
- **The loader's read-ahead is 3.3% of the top rung under pacing** —
  `--queue-depth 1`, 131,072 documents, ~0.52 GB — and **exceeds the
  122,880-document floor rung outright**. It is a capacity and not a fill: the
  pacer feeds the channel on a schedule, so it stays near empty. Nothing in this
  half runs at the closed-loop default of 10 any more.
- **The ramindex arm cannot hold the corpus** (12 GiB of tmpfs, 4,025,699
  documents) and cannot be raised at parity — tmpfs counts against the same 28g
  the heap does. **Any full-corpus OpenSearch number comes from
  `os-disk-refresh1`**, which is why S2a exists.
- **R4 has about 2 GiB of cgroup slack and its top rung clears the arm's derived
  document ceiling by 2.3%, and this is the first pass that will see either.**
  It is the only ramindex arm, so there is no sibling to corroborate a surprise
  against — and the tightest margin the campaign has ever run sits on it.
- **`p50_ms`/`p99_ms` are per request, a request is 1,024 documents, and under
  pacing they run from when the request was *due*** —
  `latency_basis=intended_start`. Every CSV this half publishes says so; a
  `service` basis is a closed-loop measurement wearing the same column names and
  **the two may not be pooled**.
- **OpenSearch gets four cores; the ScyllaDB stack gets eight.**
- **Encode cost is ~16.4 µs per document here against ~0.62 µs on the ScyllaDB
  client.** Outside the latency window but a harness artifact in a comparison
  whose credibility rests on symmetry. **It does not amortise over batch size**
  — the per-request HTTP cost is what does — which a reader will assume the
  other way round.
- **The cap is not the talk's operating point.** `../BUILD-RATE-LOOP.md`
  measured the engine ranking *inverting* between 1.2M and the 8.97M corpus.
  This ladder answers "what saturates", not "what wins at scale".
- **Contiguous sharding at the cap is not the first N documents.** Fine for
  engine against engine as long as both sides shard identically.
- **The per-level reset is excluded from the CPU window and carried separately
  as `reset_s`.** A reset that grew across an arm is a finding.
- **The probe shares the SUT's eight cores with the engine it measures.**

## Cost

An estimate with its derivation, never a measurement — but unlike the previous
version of this section the derivation has **real numbers** in it, because the
ladder is declared and every rung is budgeted at a flat 120 s.

Every rung loads for 120 s by construction, so a rep is 600 s and an arm at N=3
is **30 min of load**. On top of that sit 15 index resets an arm — one per
invocation, the overhead the per-rung document bound added — plus settle and one
stack cycle. Call it **~45 min an arm.**

| Item | Time |
|---|---|
| Re-entry (SSH, mounts, corpus decompress, harness build) — **zero if the ScyllaDB half just ran** | ~20 min |
| Smoke + the R4 top-rung tmpfs bound check | ~15 min |
| The ladder on R4 | ~45 min |
| The ladder on `os-disk-refresh1` | ~45 min |
| Pull, verify, render, stop | ~15 min |
| **Total standalone** | **~2.3 h, band 2.0–2.8 h, ~$9–12 at $4.37/h** |
| **Total after the ScyllaDB half, same session** | **~2.0 h** |

**A standalone half is now worth running.** The grid no longer needs the
ScyllaDB arms' ceilings, so this half can run alone, first, and its rows still
land on the campaign's x grid and still belong on the primary chart. What it
cannot produce alone is the cross-engine *line* — R2 ↔ R4 needs R2 — but that is
a missing arm, not a mismatched axis.

**No vector-store image build.** That is ~15 min of billed fleet time this half
does not pay, and it is the main reason a standalone OpenSearch session is
cheaper per arm than a ScyllaDB one.

**If `corpus.jsonl.zst` is not on the harness root, add ~40 min** (~$3), and it
is one-time only if the archive is written back.

**Budget 2.5 h standalone and do not plan a session shorter than that.** The
half cannot be paused: every stop wipes `/mnt/nvme` and costs a full re-entry.
If it must be split, split it at an arm boundary — an arm is ~45 min — and record
which arms ran in which session.

**Cost levers, decided before launch, not mid-run.**

| Lever | Saving | What it costs |
|---|---|---|
| Drop `os-disk-refresh1` | −0.75 h | S2a loses its OpenSearch disk arm and R8 has nothing to pair with; **the campaign also loses its only OpenSearch arm that can hold the full corpus** |
| Drop any one rung | −0.2 h | a flat 120 s a rep whichever rung it is — the per-rung time budget priced every rung the same, so **cut the rung you need least, not the slowest one**. **Campaign-wide**: the grid is shared, so dropping it here drops it everywhere |
| N=3 → N=1 on the middle rungs | −0.4 h | the middle of every curve carries no spread |
| Shorten the 120 s rung budget | linear | every rung's index shrinks with it, and R4's tmpfs check — which is what the top rung exists to exercise — goes with it. **Lengthening it is what costs**: 300 s a rung would put the top rung at 9,830,400 documents, past both R4's ceiling and the corpus |

With two arms and one single-engine reading, every lever costs a reading
outright. **The resource probe is not on the cost line** — it adds no run, no
rung and no wall clock.

## Where it lands

Nothing on the main deck until the pass has been written up. The candidate is
one image — the primary chart — for S12's neighbourhood, and it needs the
ScyllaDB half. S2a needs both halves too. **This runbook alone produces the
tmpfs-against-disk pair and nothing cross-engine.**

Record each arm's ceiling and `c_sat` in `../TUNING.md` with the run that
produced it, and the CPU and RSS at that rung beside them out of
`resource-by-rung.csv`.

Mandatory footer clauses for anything drawn from this half: that x is the
**offered** rate and a ringed point did not achieve it; **what one operation is
on each engine**; that OpenSearch runs at batch 1,024 and ScyllaDB at 1, **that
this now lives in the in-flight cap — 128 here against 512 there — rather than
in the x axis, and that `in_flight_peak` is what shows the cap did not bind**;
that 1,024 is a fixed assumption and not a measured optimum; that the rates were
a **declared** ladder of five rates — chosen before the run, not measured off
either engine, with 8192 absent between 4096 and 16384; that
`p50_ms`/`p99_ms` are measured from when a request was **due**
(`latency_basis=intended_start`); **that `--max-docs` is `rate x 120` so every
rung loads for two minutes and therefore builds a different-sized index — 122,880
documents at the floor against 3,932,160 at the top — so two arms at one rate are
comparable but two rungs of one arm differ in index size as well as in rate**;
that **R4's top rung cleared its tmpfs ceiling by 2.3%**; that
every arm runs a **1 s** cadence — OpenSearch's own
default, set explicitly — that the ScyllaDB half runs the same 1 s cadence so
R2 ↔ R4 is cadence-matched, and that **cadence cost is therefore unmeasured
and unclaimable**; **that the harness asked OpenSearch for the final
publish while ScyllaDB waited for its own commit**; the 4-against-8-core split
and, now that it is measured rather than assumed, what each side actually drew
of it; that memory is the cgroup's *anon* figure **with R4's tmpfs index named
separately rather than folded in or dropped**; that a rung marked `not-CPU` is
plotted but may not be called an engine throughput limit; which points are
lower bounds and why; the cap and the sharding; the client floor as context;
and **PRELIMINARY**.
