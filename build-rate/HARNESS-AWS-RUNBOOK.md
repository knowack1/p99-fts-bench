# Harness-on-AWS runbook — proving the loader is not the limit, on the fleet

**Hand this file to Claude Code as the instruction and it runs the whole
campaign: starts the two AWS boxes, builds both harnesses, measures each against
its own accept-and-discard sink, pulls every artifact home, stops the boxes.**
It is self-contained — every script it needs is inline below. Nothing else in
`bench/` has to be read first.

There are **two harnesses and they are measured in this order**, in one fleet
session, off one corpus:

| Part | Harness | Binary | Sink | Extra axis |
|---|---|---|---|---|
| **A** | `bench/build-rate/scylla` | `scyllarate` | `null_sink --mode cql` (+ its vector-store port) | — |
| **B** | `bench/build-rate/opensearch` | `osrate` | `null_sink --mode http` | **`--batch-size 1` only** |

On the OpenSearch side one request can carry many documents, so batch size is a
knob Part A does not have. **This campaign pins it at 1** — one document per
request, the only shape whose x axis matches Part A's, which is what makes the
two halves comparable at all. Part B therefore runs the same ladder as Part A
and nothing else; see "There is no batch sweep" in B4 for what that buys and
what it costs. If a batch level is ever added back: **batch size is never an
axis, it is a series** — every level runs the same concurrency grid and lands on
one chart as its own line.

Run Part A first. It is the simpler instrument and it establishes the corpus,
the samplers and the box's CPU baseline that Part B is read against.

## What this measures, and what it is not

The subject is **the loader**, not an engine. `scyllarate`
(`bench/build-rate/scylla`) pushes prepared `INSERT`s at `ftsbench.null_sink
--mode cql`, which answers the CQL wire and discards every row. What comes back
is what the *client* can offer on a given box, in the shape the engine campaign
loads in: **one process against one endpoint**.

**It produces a floor, not a ceiling, and that is the deliverable.** The sink is
single-threaded, so a plateau here is the sink's number and the harness's own
limit is somewhere above it, unmeasured. That is enough: the campaign only has
to know the client offers far more than an engine can absorb.
`../BUILD-RATE-MATRIX-PLAN.md`'s G7 gate asks for **2x**, and the recorded floor
of `≥266,578 docs/s` against a ~12.2k docs/s ScyllaDB build rate is ~22x. Every
number here is therefore written `≥`, and the day an engine number comes within
~2x of one, the floor stops settling the question — see "There is no N-process
arm" in Phase 5.

**No number from this runbook is an engine number and none belongs in the deck.**

Not to be confused with:

| File | Its job |
|---|---|
| `../AWS-RUN-PLAN.md` | the **engine** campaign on AWS (C1–C8, real ScyllaDB + OpenSearch) |
| `../BUILD-RATE-MATRIX-PLAN.md` § "P0 — client calibration" | the same idea for the **Python** loaders, and the constants they feed |
| `../TUNING.md` § "Per-process client ceilings" | where measured ceilings get recorded |
| `../HARDWARE.md` | why the fleet is shaped the way it is, and what it costs |

This runbook supersedes the AWS mechanics in all of them **for harness runs
only**. When it produces a floor, record it in `../TUNING.md` with the run that
produced it — as a `≥`, never as a ceiling.

## The fleet

| Alias | Instance | Role in a harness run |
|---|---|---|
| `fts-harness` | `i-08d8d2505e16683f7` · `k-nowacki-fts-benchmark-harness` | **the subject.** Runs `scyllarate`. |
| `fts-sut` | `i-0e3e4b6b02e654b7f` · `k-nowacki-fts-benchmark-sut` | **the instrument.** Runs the sink(s). |

Both `i8g.2xlarge` (8 vCPU Graviton4, 61 GiB, aarch64), `eu-north-1b`,
Amazon Linux 2023, user `ec2-user`, key `~/.ssh/KarolNowackiAws.pem`.

**The sink runs on the other box, always.** A loopback sink has far lower RTT
than the private network and would understate how much in-flight concurrency is
needed to cover latency. Measured private RTT between these two: **0.142 ms**.

There is no AWS CLI credential on this laptop — the console in Chrome is the
only way to start and stop the boxes. Instructions for that are in Phase 1
and Phase 7.

## The results directory — fix it first, on the laptop

Everything on the fleet is ephemeral: `/mnt/nvme` is destroyed on every stop, so
**the laptop is the only place results survive**. Create the directory before
touching a single instance, and name it after this runbook plus the run's own
UTC timestamp, so runs from the same day stay distinct and sort in order:

```bash
# Run this ONCE, at the very start of the session, and keep the shell.
export RUN_ID="harness-aws-runbook-$(date -u +%Y-%m-%dT%H%MZ)"
export R="$HOME/Projects/Scylla/p99/bench/results/$RUN_ID"
mkdir -p "$R"/{env,corpus,scripts}
mkdir -p "$R"/scylla/{points,samples,logs,sinks}
mkdir -p "$R"/opensearch/{points,logs,batch,sinks}
printf '%s\n' "$RUN_ID" > "$R/RUN_ID"
ln -sfn "$RUN_ID" "$(dirname "$R")/harness-aws-latest"
echo "results -> $R"
```

giving, for example:

```
bench/results/harness-aws-runbook-2026-09-10T1845Z/
├── RUN_ID  env/  corpus/  scripts/     # shared by both parts
├── scylla/      points/ samples/ logs/ sinks/
└── opensearch/  points/ samples/ logs/ batch/ sinks/
bench/results/harness-aws-latest -> harness-aws-runbook-2026-09-10T1845Z
```

The two harnesses keep separate subtrees because their CSVs are **not
interchangeable**, and one reason is left: `osrate`'s `p50_ms`/`p99_ms` are per
`_bulk` request, not per document. They are the same measurement only at
`--batch-size 1`. Everything else about the two files now lines up — same
seventeen columns in the same order, and column 17 names the engine — so the
separate subtrees are about the latency unit and nothing more.

| | |
|---|---|
| `harness-aws-runbook` | which runbook produced it — this file, not the engine campaign |
| `2026-09-10T1845Z` | when the run **started**, UTC, minute resolution |
| `harness-aws-latest` | symlink to the newest run, so `$R` is recoverable if the shell is lost |

**Capture `RUN_ID` once and reuse the variable.** Re-evaluating `$(date …)` in a
later phase invents a second directory and splits one run's artifacts across
two, which is exactly the confusion this naming exists to prevent. If the shell
is lost mid-session, recover with
`export R="$(readlink -f bench/results/harness-aws-latest)"` rather than
recomputing the timestamp.

Report the absolute path of `$R` to the user when the run finishes — that is the
one thing they need in order to find the results again.

Timestamp the directory in UTC even though the console shows local time; every
epoch in the artifacts is UTC and mixing the two is how a window fails to line
up with a sampler.

---

# Part A — the ScyllaDB harness (`scyllarate`)

## Phase 0 — settle the matrix before anything bills

Decide and write down: the arms, the ladder, the reps, the document size. The
defaults below are what a plain "measure the harness on AWS" should run.

### The default matrix

| Arm | Command shape | Why |
|---|---|---|
| `default-low` | ladder `4,8,16,32`, `--max-docs 400000` | the RTT-bound end; also the only region where the sink is nowhere near its ceiling |
| `default-high` | ladder `32,64,128`, `--max-docs 1250000` | the plateau and the knee |

### The shared concurrency grid — do not vary it per arm

**Every series in both parts is measured on the same x values:**

```
4   8   16   32   64   128
```

The campaign's deliverable chart puts concurrency on x and every
harness-and-batch combination on it as a **series** (see "The charts" at the
end). Series that do not share x values cannot be drawn on one axis, so a
ladder tailored per arm — a taller one for small batches, say — silently
destroys the chart. If a level has to be added, add it to **every** arm.

Powers of two, because x is drawn on a log2 axis.

**And the same `--max-docs` per sweep, in both parts.** `400000` for the low
sweep, `1250000` for the high one, for `scyllarate` and `osrate` alike. The
budget decides how many documents a point averages over and how much of its
wall clock is the per-level reset, so a series measured on a different budget is
not the same measurement drawn on the same axis. Change it for one arm and you
change it for all four.

**The grid stops at 128, and that is a decision about the box.** The loader box
is an `i8g.2xlarge` — `nproc` 8, measured (`../HARDWARE.md`). Concurrency here is
requests in flight rather than threads, so 256 and 512 are not meaningless on 8
cores, but the 2026-09-10 pass had every `scyllarate` point above c≈96 sitting
behind a saturated sink: those levels reported the instrument, not the client,
and each one costs billed fleet time. Any series still rising at `c=128` is
reported as **"unresolved above 128"** — which the OpenSearch half is the likely
candidate for, since documents in flight is `concurrency * batch_size` and at
`batch=1` there are only 128 of them at the top rung.
Raising the cap means raising it for **every** arm in both parts.

**Two sub-sweeps per series, overlapping at 32.** One `--max-docs` must serve
every level in a sweep, and the rate range across `4…128` is wide enough that
one budget makes either the top point 2 s or the bottom point 40 s. So: a low
sweep (`4…32`) at a small budget, a high sweep (`32…128`) at a large one, and
`32` measured by both. If the two disagree at `32` by more than the rep spread,
the budgets are distorting the measurement — report that rather than averaging
it away.

**N=3 repetitions of every ladder, minimum.**

**No throwaway warm-up row.** Ladders carry each level exactly once, so `4` and
`32` are measured points rather than sacrifices. Both renderers drop the first
data row of every CSV by default, which on these ladders would delete `c=4` and
the high sweep's `c=32` outright — so **every chart command here passes
`--keep-warmup`**, and a ladder must never be given a repeated first level
without dropping that flag. The alternative, when the low levels start to
matter more than they cost, is to put the throwaway back (`LADDER=4,4,8,16,32`)
and render without `--keep-warmup`.

**Two sweeps, not one, and they must overlap.** One `--max-docs` has to serve
every level in a sweep, and the rate range across `4…128` is wide, so a
single budget either makes the top point 2 s long or the bottom point 40 s. Pick
each sweep's budget so **every level in it runs ≥5 s**, and overlap the two
sweeps at one level (`32` above). If the overlap level disagrees between sweeps
by more than the rep spread, the budgets are distorting the measurement — say so
rather than averaging them.

### Document size is part of the answer

The harness's cost is roughly **8.2 µs of CPU per document plus 2.3 ns per
corpus byte** (two-point fit, `results/fleet-rust-harness-null-sink-2026-09-10`).
So docs/s depends on document size and **a docs/s figure without the document
size is meaningless**. Default to enwiki's mean corpus line, **3,948 B**. If a
second size is wanted, 400 B is the recorded comparison point.

---

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

---

## Phase 2 — fleet re-entry

Every stop wipes the instance store, so this runs on **every** start.

> **One-time, on the harness, at the first start after 2026-09-11.** The root
> EBS volume was grown 8 GiB -> 32 GiB, but Linux does not pick that up on its
> own. **Identify the root device before touching anything** — the NVMe
> re-init below formats `/dev/nvme0n1` as the *instance store*, so the EBS root
> is a different nvme device and `growpart` against the wrong one is
> destructive:
>
> ```bash
> findmnt -no SOURCE /          # e.g. /dev/nvme1n1p1
> lsblk
> sudo growpart <root-disk> 1   # the DISK, then the partition number
> sudo xfs_growfs /             # AL2023 root is xfs
> df -h /                       # expect ~32 GiB
> ```
>
> Once done it persists. Thereafter the corpus restage is a local decompress,
> `pzstd -d -p 8` from `/` to `/mnt/nvme/data/corpus.jsonl`, **~1.5-2 min**
> (bounded by gp3's 125 MB/s read, not by pzstd's ~2.85 GB/s) — replacing the
> 36 min mirror download. Rationale and the rejected S3 route:
> `../S3-CORPUS-STAGING-PLAN.md`.

**Public IPs are reassigned on every start; private IPs are not.** Read the new
public IPs from the console's "Public IPv4 DNS" column (`ec2-A-B-C-D...` encodes
them), then:

```bash
# 1. re-point the SSH aliases
sed -i '/^Host fts-harness$/,/^$/ s/^    HostName .*/    HostName <new-harness-ip>/' ~/.ssh/config
sed -i '/^Host fts-sut$/,/^$/     s/^    HostName .*/    HostName <new-sut-ip>/'     ~/.ssh/config

# 2. these hosts are not in known_hosts under the new IP
ssh -o StrictHostKeyChecking=accept-new fts-harness true
ssh -o StrictHostKeyChecking=accept-new fts-sut     true

# 3. confirm the private IPs, do not assume them
ssh fts-harness hostname -I     # expect 172.31.38.237
ssh fts-sut     hostname -I     # expect 172.31.47.166  <- SINK_HOST below
```

Then prepare the two boxes:

```bash
# --- loader box: instance store, compiler, Rust toolchain ---
ssh fts-harness 'set -e
  sudo mkfs.xfs -f -q /dev/nvme0n1
  sudo mkdir -p /mnt/nvme && sudo mount /dev/nvme0n1 /mnt/nvme
  sudo chown ec2-user:ec2-user /mnt/nvme
  sudo dnf install -y -q gcc
  curl --proto "=https" --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
  . "$HOME/.cargo/env" && rustc --version'

# --- sink box: ftsbench, on python3.12 ---
# The system python3 is 3.9 and ftsbench.runmeta needs 3.10+ syntax.
# Use python3.12 explicitly for everything on this box.
cd <repo>/bench && tar czf - --exclude=__pycache__ ftsbench \
  | ssh fts-sut 'mkdir -p ~/sink-work && tar xzf - -C ~/sink-work'
ssh fts-sut 'cd ~/sink-work && python3.12 -c "import ftsbench.null_sink; print(\"ok\")"'
```

Root volumes differ between the boxes as of **2026-09-11**: the **harness**
root (`vol-0a789d6c4317a2b7d`) was grown **8 GiB -> 32 GiB** gp3 so the
compressed corpus can survive a stop; the **SUT** root is unchanged (8 GB per
this runbook's original note, not re-verified). Build outputs and the
uncompressed corpus still go on **`/mnt/nvme`** on both. The only thing that
belongs on the harness root is `corpus.jsonl.zst` — see Phase 2.

### Build the harness, then freeze it

```bash
cd <repo>/bench && tar czf - --exclude=target build-rate \
  | ssh fts-harness 'mkdir -p /mnt/nvme/work && tar xzf - -C /mnt/nvme/work'

ssh fts-harness 'cd /mnt/nvme/work/build-rate/scylla && . "$HOME/.cargo/env" \
  && CARGO_TARGET_DIR=/mnt/nvme/work/target cargo build --release --locked'
```

Record, into `$R/env/`, **before** measuring:

```bash
ssh fts-harness 'cd /mnt/nvme/work/build-rate && find . -type f \
  \( -name "*.rs" -o -name "Cargo.*" \) | sort | xargs sha256sum | sha256sum'
git -C <repo>/bench log -1 --format='%H %s'
git -C <repo>/bench status --short build-rate
```

**Do not rebuild once an arm has run.** The working tree may move under you
mid-session — it did on 2026-09-10, and `--no-write-coalescing` was removed from
the crate while the campaign was using it. A rebuild mid-campaign makes the arms
incomparable. Freeze the binary, record exactly what it was built from, and note
any divergence from the current tree in the write-up.

Also verify the source snapshot carries the flags the matrix wants —
`--no-write-coalescing` exists in some revisions and not others.

---

## Phase 3 — the instruments, and the rules that keep them honest

### Start the sinks

**One sink, because there is one loader process.** The launcher still takes a
port list — it is what a restored N-process arm would need — but this campaign
starts a single sink and gives it the box to itself, which also keeps three idle
Python processes off the SUT's 8 cores while an arm runs.

```bash
ssh fts-sut 'cat > ~/start-sinks.sh << "EOF"
#!/bin/bash
# One sink per loader process; this campaign runs one. A sink is single-threaded
# and sits behind a single driver connection, so a second loader process would
# need a second sink on its own port rather than sharing this one.
cd ~/sink-work
: > /tmp/sinks.pids
# Each sink also serves the vector-store index-status endpoint `scyllarate`
# gates every level on, at CQL port + 7000 so the two never collide.
for port in "$@"; do
    setsid python3.12 -m ftsbench.null_sink \
        --mode cql --host 0.0.0.0 --port "$port" \
        --vs-port "$((port + 7000))" \
        --label "harness-$port" --report-interval 30 \
        --stats-out "/tmp/sink-$port.json" \
        < /dev/null > "/tmp/sink-$port.log" 2>&1 &
    echo "$port $!" >> /tmp/sinks.pids
    disown
done
EOF
chmod +x ~/start-sinks.sh'

ssh fts-sut '~/start-sinks.sh 9042'
ssh fts-sut 'cat /tmp/sinks.pids; ss -ltn | grep -E "9042"'
```

### Start the CPU samplers — both boxes

These are what make a number defensible. Without them a plateau has no
attribution and cannot be quoted.

```bash
# --- sink box: per-sink CPU at 1 Hz ---
ssh fts-sut 'cat > ~/sample-sinks-cpu.sh << "EOF"
#!/bin/bash
# 1 Hz CPU per sink: epoch, port, utime+stime ticks.
# APPENDS. Never truncate: the sinks get replaced mid-session and truncating
# destroys the record for every arm already measured. Sink generations are told
# apart by the tick counter resetting; a consumer drops negative deltas.
OUT="${1:-/tmp/sinks-cpu.tsv}"
[ -s "$OUT" ] || printf "epoch\tport\tticks\n" > "$OUT"
while true; do
    now=$(date +%s)
    while read -r port pid; do
        [ -d "/proc/$pid" ] || continue
        printf "%s\t%s\t%s\n" "$now" "$port" "$(awk "{print \$14+\$15}" /proc/$pid/stat)" >> "$OUT"
    done < /tmp/sinks.pids
    sleep 1
done
EOF
chmod +x ~/sample-sinks-cpu.sh
setsid ~/sample-sinks-cpu.sh /tmp/sinks-cpu.tsv </dev/null >/dev/null 2>&1 & disown'

# --- loader box: whole-box CPU at 1 Hz ---
ssh fts-harness 'cat > ~/sample-box-cpu.sh << "EOF"
#!/bin/bash
# 1 Hz whole-box CPU on the loader box: epoch, busy and total jiffies.
OUT="${1:-/tmp/box-cpu.tsv}"
[ -s "$OUT" ] || printf "epoch\tbusy\ttotal\n" > "$OUT"
while true; do
    read -r _ user nice system idle iowait irq softirq steal _ < /proc/stat
    total=$((user+nice+system+idle+iowait+irq+softirq+steal))
    printf "%s\t%s\t%s\n" "$(date +%s)" "$((total-idle-iowait))" "$total" >> "$OUT"
    sleep 1
done
EOF
chmod +x ~/sample-box-cpu.sh
setsid ~/sample-box-cpu.sh /tmp/box-cpu.tsv </dev/null >/dev/null 2>&1 & disown'
```

Both boxes are NTP-synced to well under a microsecond, so windows cut on one
box's clock line up with samples taken on the other. Check it once:
`ssh fts-sut chronyc tracking | grep "System time"`.

### Killing things on these boxes

`pkill -f "ftsbench.null_sink"` from inside an `ssh` one-liner **kills the ssh
session**, because the wrapper's own command line contains the pattern. Put any
`pkill` inside a script on the box and run the script.

---

## Phase 4 — the corpus

Synthetic, generated on the loader box. The client does not read the words: its
per-document cost is a function of size and shape only, so staging the frozen
enwiki corpus would cost fleet hours and change nothing.

```bash
ssh fts-harness 'cat > ~/gen-corpus.sh << "EOF"
#!/bin/bash
set -e
DOCS="${1:-1250000}"; MEAN="${2:-3948}"; SIGMA="${3:-0.6}"
OUT="${4:-/mnt/nvme/work/corpus.jsonl}"
cd /mnt/nvme/work/gen
rm -rf /mnt/nvme/work/parts && mkdir -p /mnt/nvme/work/parts
python3 -m ftsbench.synth_corpus --output /mnt/nvme/work/parts/part.jsonl \
    --docs "$DOCS" --mean-bytes "$MEAN" --sigma "$SIGMA" --shards 8 \
    --stats-out "$OUT.stats.json"
cat /mnt/nvme/work/parts/part-*.jsonl > "$OUT"
wc -l "$OUT"; sha256sum "$OUT"
rm -rf /mnt/nvme/work/parts
echo GEN_DONE
EOF
chmod +x ~/gen-corpus.sh'

# ftsbench must be on the loader box too, for the generator
cd <repo>/bench && tar czf - --exclude=__pycache__ ftsbench \
  | ssh fts-harness 'mkdir -p /mnt/nvme/work/gen && tar xzf - -C /mnt/nvme/work/gen'

ssh fts-harness 'setsid ~/gen-corpus.sh 1250000 3948 0.6 </dev/null >/tmp/gen.log 2>&1 & disown'
# ~12 min for 1.25 M x 3,948 B (4.9 GB). Poll for GEN_DONE.
```

Then **warm the page cache once**, so the reps are comparable and the first one
is not measuring NVMe:

```bash
ssh fts-harness 'cat /mnt/nvme/work/corpus.jsonl > /dev/null'
```

Record documents, bytes, mean line and **sha256** in the results directory. The
corpus dies with the instance store; the checksum is what proves a
regeneration reproduced it. The generator is deterministic given `--seed`
(default 20260908).

---

## Phase 5 — run the arms

```bash
ssh fts-harness 'cat > ~/run-arm.sh << "SCRIPT"
#!/bin/bash
# One arm: the same concurrency ladder, N times, against the sink on fts-sut.
#
# stderr is timestamped per line. The tool announces each level as it starts it,
# so the log carries the exact wall-clock window of every point and the sink CPU
# sampler on the other box can be cut to that window rather than to the whole
# sweep. A point whose sink sat near a full core is the sink ceiling being
# reported as the client ceiling, which is the one way this instrument lies.
set -u
ARM="$1"; shift
REPS="${REPS:-3}"
LADDER="${LADDER:-32,64,128}"
CORPUS="${CORPUS:-/mnt/nvme/work/corpus.jsonl}"
MAX_DOCS="${MAX_DOCS:-1250000}"
SINK="${SINK:-172.31.47.166}"
PORT="${PORT:-9042}"
VS_PORT="${VS_PORT:-$((PORT + 7000))}"
OUT_DIR="${OUT_DIR:-/mnt/nvme/work/results}"
# The per-second series, deliberately NOT under $OUT_DIR: Phase 6 copies that
# directory into $R/points/ and its gate globs points/*.csv, which would read a
# series as a set of points.
SAMPLES_DIR="${SAMPLES_DIR:-/mnt/nvme/work/samples}"
BIN=/mnt/nvme/work/target/release/scyllarate

mkdir -p "$OUT_DIR" "$SAMPLES_DIR"
WINDOWS="$OUT_DIR/run-windows.tsv"
[ -f "$WINDOWS" ] || printf "arm\trep\tstart_epoch\tend_epoch\texit_code\tladder\tmax_docs\tcsv\n" > "$WINDOWS"
stamp() { awk "{ printf \"%d\t%s\n\", systime(), \$0; fflush() }"; }

for rep in $(seq 1 "$REPS"); do
    csv="$OUT_DIR/$ARM-rep$rep.csv"
    log="$OUT_DIR/$ARM-rep$rep.stderr.tsv"
    echo "######## arm=$ARM rep=$rep $(date -u +%H:%M:%S)"
    start=$(date +%s)
    "$BIN" --corpus "$CORPUS" --concurrency "$LADDER" --max-docs "$MAX_DOCS" \
           --hosts "$SINK" --port "$PORT" \
           --vs-url "http://$SINK:$VS_PORT" \
           --out "$csv" --samples-dir "$SAMPLES_DIR/$ARM-rep$rep" \
           "$@" 2>&1 | stamp > "$log"
    code=${PIPESTATUS[0]}
    printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
        "$ARM" "$rep" "$start" "$(date +%s)" "$code" "$LADDER" "$MAX_DOCS" "$csv" >> "$WINDOWS"
    grep -E "docs in" "$log" | tail -12
done
SCRIPT
chmod +x ~/run-arm.sh'
```

Run the default matrix:

```bash
ssh fts-harness 'REPS=3 LADDER=4,8,16,32 MAX_DOCS=400000  ~/run-arm.sh default-low'
ssh fts-harness 'REPS=3 LADDER=32,64,128 MAX_DOCS=1250000 ~/run-arm.sh default-high'
```

The old `4…512` ladder took 80–100 s per rep; these are shorter, and each
sweep's budget still has to keep every level above 5 s. Run them with a generous timeout or in the
background; do not poll every few seconds — it wastes the session.

### There is no N-process arm — one loader process, one sink

The campaign runs **one** `scyllarate` process against **one** sink, and that is
the only shape measured here.

A previous pass ran `nproc1/2/4` — N loader processes, each against its own sink
on its own port, all at one concurrency — because the sink is single-threaded
behind one driver connection, so multiplying the instrument was the only way to
see past one core. It reached ~378,400 docs/s ≈ 1.49 GB/s at 2 processes and
nothing more at 4.

**That number has no consumer.** The engine campaign loads through a single
process against a single endpoint, and against a real ScyllaDB the sink's
one-core limit does not exist at all: a shard-aware driver connection fans out
across shards on its own. What the campaign needs from this runbook is not the
harness's ceiling but a **floor** — proof the client offers far more than any
engine can absorb — and the single-process figure already supplies it:
`≥266,578 docs/s` against a ~12.2k docs/s ScyllaDB build rate is ~22x, where
`../BUILD-RATE-MATRIX-PLAN.md`'s G7 gate asks for 2x.

So the single-process plateau is **the sink's number, written `≥`**, and it is
sufficient while that margin holds. Two things would end that:

- **An engine number within ~2x of the floor.** Then the floor stops settling
  the question and the harness's real ceiling has to be measured — either by
  making one sink reach more than one core (the `system.peers` route in trap 3,
  a change to the frozen harness) or by bringing the N-process arm back.
- **A different document size or box pair.** The floor is 8.2 µs/doc +
  2.3 ns/byte on this instance pair in this AZ; it does not transfer to a
  different corpus line length or a different network path.

**If it is ever restored, the aggregate is not the sum of the per-process
averages.** The processes do not finish together and a survivor speeds up once
the others exit — at N=4 a process ran ~66k docs/s for ten seconds and then
~190k for three, and summing the reported averages overstated N=4 by 16%. Sum
the per-second series across processes instead (`--samples-dir` writes one row
per reading, `t_s` measured from the level's own start, so the join is on `t_s`
rather than line index) and count only the seconds in which every process still
had a reading.

---

## Phase 6 — collect and verify, BEFORE stopping the boxes

`$R` is the directory fixed at the start of the session — see "The results
directory" above. Do not recompute it here.

```bash
test -n "$R" && test -d "$R" || { echo "R is unset: recover it with"; \
  echo '  export R="$(readlink -f bench/results/harness-aws-latest)"'; }

scp 'fts-harness:/mnt/nvme/work/results/*'        $R/scylla/points/
scp -r 'fts-harness:/mnt/nvme/work/samples/*'     $R/scylla/samples/
scp  fts-harness:/tmp/box-cpu.tsv                 $R/scylla/logs/
scp  fts-harness:/tmp/gen.log                     $R/corpus/
scp 'fts-harness:/mnt/nvme/work/*.stats.json'     $R/corpus/
scp  fts-sut:/tmp/sinks-cpu.tsv                   $R/scylla/sinks/
scp 'fts-sut:/tmp/sink-90*.log'                   $R/scylla/sinks/
scp 'fts-harness:~/*.sh' 'fts-sut:~/*.sh'         $R/scripts/
mv $R/scylla/points/*.stderr.tsv $R/scylla/logs/ 2>/dev/null
```

Stop the sinks with **SIGTERM** so each writes its `--stats-out` JSON, then take
those too:

```bash
ssh fts-sut 'cat > ~/stop-sinks.sh << "EOF"
#!/bin/bash
pkill -f "sample-sinks-cpu"
while read -r port pid; do kill -TERM "$pid" 2>/dev/null; done < /tmp/sinks.pids
sleep 4
EOF
chmod +x ~/stop-sinks.sh; setsid ~/stop-sinks.sh </dev/null >/dev/null 2>&1'
scp 'fts-sut:/tmp/sink-90*.json' $R/scylla/sinks/
```

Record the environment of both boxes into `$R/env/` — instance id and type from
IMDS, kernel, `nproc`, `MemTotal`, python versions, chrony offset, and the
harness's build provenance from Phase 2.

**Verification gate — all of it must pass before Phase 7:**

```bash
# every ladder CSV has one row per ladder entry
for f in $R/scylla/points/*.csv; do echo "$(grep -vc '^#\|^concurrency' $f) $(basename $f)"; done
# zero failed inserts anywhere
awk -F, '!/^#/ && $1!="concurrency" && $3+0>0 {print FILENAME": errors="$3}' $R/scylla/points/*.csv
# every arm left a series: one directory per rep, one CSV per ladder level
for d in $R/scylla/samples/*/; do echo "$(ls $d | wc -l) $(basename $d)"; done
# and no series is a header with nothing under it
awk -F, 'FNR==1 { rows=0 } !/^#/ && $1!="level" { rows++ } \
     ENDFILE { if (rows < 3) print FILENAME": "rows" readings" }' $R/scylla/samples/*/*.csv
# the CPU samplers cover every run window
head -2 $R/scylla/logs/box-cpu.tsv; tail -1 $R/scylla/logs/box-cpu.tsv; cat $R/scylla/points/run-windows.tsv
# the analysis reproduces from the downloaded tree alone
```

Once the boxes stop, `/mnt/nvme` is gone. Anything not copied is lost.

---

## Phase 7 — stop the boxes

Same console tab. Select both rows → **Instance state → Stop instance** → check
the dialog names **both** `k-nowacki-fts-benchmark-harness` and
`k-nowacki-fts-benchmark-sut`, leave "Skip OS shutdown" unchecked → **Stop**.

Then refresh and **confirm both rows read `Stopped` with no public IP**. Say so
explicitly in the report; "I initiated the stop" is not the same as "they are
stopped".

The `~/.ssh/config` entries now point at released IPs and must be re-pointed on
the next start.

---

## Phase 8 — analyse and write up

Per measured point, join the CSV row to what both boxes were doing over **that
point's own window**, cut from the timestamped stderr log:

- `sink_cores` — busiest sink's CPU out of its one core, median and peak
- `box_cores` — loader box CPU out of 8, median and peak

Then classify every level with a **three-state** gate, never pass/fail:

| | meaning |
|---|---|
| `ok` | a sink series exists and it stayed under 0.85 of a core |
| `SINK` | the sink reached ≥0.85 of a core — the level is a **lower bound** on the harness |
| `?` | **no sink series for this level. Not a pass.** |

An unmeasured gate must never render as a passed gate, the same way an
unmeasured latency is a blank cell and never `0`.

Reference implementations, ~350 lines total, ready to copy:
`results/fleet-rust-harness-null-sink-2026-09-10/{summarize,summarize-nproc,aggregate-contended}.py`
— `summarize.py` is the one this matrix needs; the other two belong to the
retired N-process arm and are kept for whoever restores it.

Write a `README.md` in `$R` carrying: the topology and RTT, the corpus manifest
with checksums, the binary provenance, the arm table, the gate column explained,
and — first, before any number — what the run does **not** license anyone to
claim. Open it with the run's own identity, so the directory explains itself
without reference to this file:

```markdown
# <one line: what was measured>

Run `harness-aws-runbook-2026-09-10T1845Z`, produced by
`bench/build-rate/HARNESS-AWS-RUNBOOK.md`. Fleet up <HH:MM>–<HH:MM> UTC on <date>.
Arms: <names>, N=<reps> each. Binary: crate commit <sha>.
```

Finally, hand the user the absolute path of `$R` and say which arms landed in
it. A results directory nobody can find is the same as no results.

---

---

# Part B — the OpenSearch harness (`osrate`), once per batch size

Same fleet, same session, same corpus, same samplers, same gates. Only the
deltas are written out here; anything not mentioned is unchanged from Part A.

**Do not stop the boxes between the parts.** Part B reuses the corpus and the
page cache Part A warmed, and the box CPU baseline is only comparable within one
session.

## B0 — what is different about this harness

`osrate` (`bench/build-rate/opensearch`) posts hand-built NDJSON to `POST
/_bulk`. Two units, and mixing them is the standing way to misread this half:

| Number | Unit |
|---|---|
| `concurrency` | in-flight **`_bulk` requests**, not documents |
| `docs`, `docs_per_s` | documents |
| `bulks`, `failed_bulks` | `_bulk` requests |
| `p50_ms`, `p99_ms` | **one `_bulk` request** — never one document |

**Documents in flight is `concurrency * batch_size`.** `--concurrency 64
--batch-size 512` offers 32,768 documents at once, not 64. The CSV header
carries `latency_unit=bulk_request` and `batch_size` is a column, so a chart
cannot silently mix two batch sizes — but a *reader* still can.

At `--batch-size 1` the two units coincide, which is the shape Part A measures.
That level is what makes the batch curve answerable rather than merely drawn.

The CSV columns, in order. **Both halves write all seventeen**, so an `awk`
field index means the same field on either — which it did not before, where the
two diverged after column 7 and any index written past it for one half was
wrong on the other:

```
1 concurrency  2 docs  3 errors  4 wall_s  5 docs_per_s  6 p50_ms  7 p99_ms
8 batch_size  9 requests  10 failed_requests
11 index_docs  12 index_docs_per_s  13 index_lag_docs
14 index_settle_s  15 index_settled  16 index_status
17 engine
```

`requests` and `failed_requests` were `bulks` and `failed_bulks`: at one
document per request they equal `docs` and `errors`, which is what makes
`docs / requests` the effective batch size a reader can check column 8 against.

A column an engine cannot fill is **blank, never zero** — the rule the latency
columns already followed, because a zero plots as the best point on the curve
while a blank plots as nothing. `scyllarate` writes `batch_size=1` always; a
row with no index watch leaves columns 11-16 empty.

**Column 17 is what tells the halves apart.** `tools/plot_harness_grid.py` used
to decide by whether a `batch_size` column existed, which both halves now pass —
so without `engine` every ScyllaDB point would be relabelled `osrate batch=1`,
and with both globs given the two lines would merge and be averaged. It still
falls back to the old test for CSVs recorded before the column existed.

`scyllarate`'s index columns come from the sink's vector-store half, which
reports the documents its CQL half accepted. They give the **build-rate**
figure its own client ceiling, measured with the reading the engine campaign
uses (`ftsbench.samplers.ScyllaSampler`).

**The corpus is the same file.** `osrate` does not read the line's `uuid` — it
is ScyllaDB's partition key — so the corpus built in Phase 4 serves both parts
unchanged. Do not regenerate it; that is ~12 minutes of billed fleet time for
nothing.

## B1 — build it

The sink speaks plain HTTP, so build without the default TLS feature: smaller
binary, and the run cannot depend on the box's OpenSSL.

**`bench/opensearch/` ships too, and the build fails without it.** `osrate`
`include_str!`s `index-config-ramindex.json` and `index-config.json`, so those
two files are needed at *compile* time, not at run time — which is what
guarantees the binary embeds the same bytes the engine campaign applies. Earlier
revisions of this block tarred only the crate and could not have compiled on a
fresh box.

```bash
cd <repo>/bench && tar czf - --exclude=target build-rate opensearch \
  | ssh fts-harness 'tar xzf - -C /mnt/nvme/work'

ssh fts-harness 'cd /mnt/nvme/work/build-rate/opensearch && . "$HOME/.cargo/env" \
  && CARGO_TARGET_DIR=/mnt/nvme/work/target-os \
     cargo build --release --locked --no-default-features'
```

Record its provenance into `$R/env/` exactly as in Phase 2, and freeze it for
the same reason. A separate `CARGO_TARGET_DIR` keeps Part A's binary untouched.

## B2 — the HTTP sink

Same launcher, `--mode http`, port 9200. One sink, one `osrate` process. Start
it **fresh** for Part B and note the time; the CQL sink from Part A can be left
running or stopped, they are on different ports either way.

```bash
ssh fts-sut 'cat > ~/start-http-sinks.sh << "EOF"
#!/bin/bash
# One HTTP sink per loader process, on 9200+; this campaign runs one. Same
# one-core-per-sink limit as the CQL side: the sink scans every _bulk body to
# count its actions, so its cost is per document and it is the first thing to
# saturate -- at batch=1 especially, which is the only batch size measured.
cd ~/sink-work
: > /tmp/sinks.pids
for port in "$@"; do
    setsid python3.12 -m ftsbench.null_sink \
        --mode http --host 0.0.0.0 --port "$port" \
        --label "osrate-$port" --report-interval 30 \
        --stats-out "/tmp/sink-$port.json" \
        < /dev/null > "/tmp/sink-$port.log" 2>&1 &
    echo "$port $!" >> /tmp/sinks.pids
    disown
done
EOF
chmod +x ~/start-http-sinks.sh'

ssh fts-sut '~/start-http-sinks.sh 9200'
ssh fts-sut 'cat /tmp/sinks.pids; ss -ltn | grep -E "9200"'
```

The CPU sampler from Phase 3 reads `/tmp/sinks.pids` and needs no change — but
it appends to the same file, so **record the wall-clock time Part B's sinks
started** and cut Part B's windows after it.

### Part B resets per level, exactly as Part A does

**Both halves reset, and that is the point.** `scyllarate` drops and rebuilds
the keyspace before every level — `resets()` is `watches_index() && !no_reset`
(`scylla/src/cli.rs`), index-watch is on by default and `run-arm.sh` passes
neither flag — so Part A has always paid a per-level reset. `osrate` deletes and
recreates the index before every level by the same default. Leaving that default
alone on both halves is what keeps their per-level overhead comparable, which is
the whole basis on which the two are drawn on one chart.

Against a null sink the reset does not change what is measured — nothing is
stored, so there is no second level rewriting the first's documents and no
Lucene update path to fall into. What it changes is the **cost inside the
ladder**: a `DELETE`, a `PUT` and two gate polls per level, now paid on both
sides rather than on one.

**The sink answers all of it.** `ftsbench/null_sink_http.py` serves the index
lifecycle (`PUT`/`DELETE` on an index path), `HEAD` presence for the first reset
gate, and `_count`/`_stats` with the 404-when-absent and 503-while-unallocated
answers the second gate reads — the two states its own docstring names as "the
two states `osrate`'s reset gates exist to tell apart". An earlier revision of
this runbook said the sink could not answer a reset run; that is no longer true
and was the reason `--no-reset` was passed.

**One probe still has to be suppressed.** A reset run sends `_analyze` once
before the first document to verify the analyzer, and that is the one route the
sink does not answer — it 404s, and unlike the header fields it does not degrade,
it fails the run. It is not bound to the reset: `checks_analyzer()` is
`resets() && !no_analyzer_check` (`opensearch/src/cli.rs`), so
`run-os-arm.sh` passes **`--no-analyzer-check`** and nothing else. `RESET_FLAGS`
overrides it for a run against a real OpenSearch, where the analyzer check is
exactly what you want and the flag should be empty.

### The header will say `unknown`, and that is correct

`osrate` reads index settings, mappings and the node thread pool for its header.
The sink answers the index lifecycle, `HEAD`, `_count`/`_stats`, `_refresh`,
`GET /` and `_bulk`, but not `GET /<index>/_settings`. The crate falls back to `unknown` per field by
design rather than failing the run. So expect `index_shards=unknown`,
`refresh_interval=unknown`, `write_pool=unknown`. **That is the sink being
honest, not a fault — do not report it as one, and do not "fix" it by pointing
the run at a real OpenSearch.**

## B3 — memory, before you launch anything

The channel is bounded in **batches**, so documents buffered ahead of the
workers are `queue_depth * concurrency * batch_size`. The default depth is 10,
which at `c=384 batch=512` is 1,966,080 documents — **~7.8 GB resident** at this
corpus's 3,948 B a line. Multiply by another 2 at `batch=1024` and the box is in
trouble.

Check before every arm, and keep the product under **~8 GB**:

```
queue_depth * concurrency * batch_size * 3948 bytes  <  8 GB
```

**Nothing here sets the depth, and that is the point.** `--queue-depth` is not
passed at all, so both halves run at the same `QUEUE_DEPTH_PER_WORKER = 10`
(`core/src/sweep.rs`) — `scyllarate` hardcodes it and has no flag to change it,
so leaving the `osrate` flag off is what keeps the two halves' read-ahead
identical. **At `batch=1` — the only batch size this campaign runs — the whole
bound is `10 * 128 * 1 * 3948` ≈ 5 MB**, so nothing here can bite. `osrate`
records the depth it used in the CSV header either way; a run at a different
depth must not be plotted against these.

**A batch sweep is what would make this bite, and it is the one case for
lowering it.** At depth 10 the old sweep's worst cell (`c=384 batch=512`) is
1,966,080 documents ≈ 7.8 GB, and `batch=1024` doubles it. If a batch level is
ever restored, check the product above before each arm and drop the depth only
as far as it takes to clear ~8 GB — the read-ahead only ever mattered at the
bottom of the ladder — recording the depth you used, since those runs then no
longer plot against these.

Watch it anyway — the box has 61 GiB and no swap, so an overshoot is an OOM
kill, not a slowdown:

```bash
ssh fts-harness 'while pgrep -x osrate >/dev/null; do \
  ps -o rss= -C osrate | awk "{s+=\$1} END {printf \"osrate RSS %.1f GB\n\", s/1048576}"; \
  sleep 5; done'
```

## B4 — the arms

### Arm 1 — the concurrency ladder at `batch=1`

Locates the knee, and is the direct counterpart of Part A's ladder. It is the
**only** measuring arm on this half: see "There is no batch sweep" below.

```bash
ssh fts-harness 'cat > ~/run-os-arm.sh << "SCRIPT"
#!/bin/bash
# One osrate arm: a concurrency ladder at ONE batch size, N times, against the
# HTTP sink on fts-sut. stderr is timestamped per line so each point's window
# can be cut out of the CPU samplers, exactly as on the ScyllaDB side.
set -u
ARM="$1"; shift
REPS="${REPS:-3}"
LADDER="${LADDER:-32,64,128}"
BATCH="${BATCH:-1}"
CORPUS="${CORPUS:-/mnt/nvme/work/corpus.jsonl}"
MAX_DOCS="${MAX_DOCS:-1250000}"
SINK_URL="${SINK_URL:-http://172.31.47.166:9200}"
INDEX="${INDEX:-wiki-articles}"
# Reset stays ON, as it is on the ScyllaDB side, so both halves pay the same
# per-level overhead. Only the analyzer probe is suppressed: it is the one route
# the sink does not answer, and it fails the run rather than degrading. Set
# RESET_FLAGS= empty for a run against a real OpenSearch. See B2.
RESET_FLAGS="${RESET_FLAGS:---no-analyzer-check}"
OUT_DIR="${OUT_DIR:-/mnt/nvme/work/results-os}"
BIN=/mnt/nvme/work/target-os/release/osrate

mkdir -p "$OUT_DIR"
WINDOWS="$OUT_DIR/os-windows.tsv"
[ -f "$WINDOWS" ] || printf "arm\tbatch\trep\tstart_epoch\tend_epoch\texit_code\tladder\tmax_docs\tcsv\n" > "$WINDOWS"
stamp() { awk "{ printf \"%d\t%s\n\", systime(), \$0; fflush() }"; }

for rep in $(seq 1 "$REPS"); do
    csv="$OUT_DIR/$ARM-b$BATCH-rep$rep.csv"
    log="$OUT_DIR/$ARM-b$BATCH-rep$rep.stderr.tsv"
    echo "######## arm=$ARM batch=$BATCH rep=$rep $(date -u +%H:%M:%S)"
    start=$(date +%s)
    "$BIN" --corpus "$CORPUS" --concurrency "$LADDER" --batch-size "$BATCH" \
           --max-docs "$MAX_DOCS" \
           --url "$SINK_URL" --index "$INDEX" --out "$csv" $RESET_FLAGS "$@" 2>&1 | stamp > "$log"
    code=${PIPESTATUS[0]}
    end=$(date +%s)
    printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
        "$ARM" "$BATCH" "$rep" "$start" "$end" "$code" "$LADDER" "$MAX_DOCS" \
        "$csv" >> "$WINDOWS"
    grep -E "docs in" "$log" | tail -8
done
SCRIPT
chmod +x ~/run-os-arm.sh'

ssh fts-harness 'REPS=3 BATCH=1 LADDER=4,8,16,32 MAX_DOCS=400000  ~/run-os-arm.sh os-conc-low'
ssh fts-harness 'REPS=3 BATCH=1 LADDER=32,64,128 MAX_DOCS=1250000 ~/run-os-arm.sh os-conc-high'
```

**The budgets are Part A's, deliberately: `400000` low and `1250000` high.**
A point's `--max-docs` is how many documents that level pushed, so two halves on
different budgets compare a `scyllarate` point measured over 1.25 M documents
against an `osrate` point measured over 300 k — different amounts of the corpus,
different shares of the run spent in the per-level reset, and a different
exposure to the sink's drift over a session (trap 2). Sharing the budget removes
that as a variable, which is the same reason the concurrency grid is shared.

Part B is the slow end at `batch=1`, so it pays for this in wall time rather
than in validity: every level runs roughly four times longer than it did at the
old `100k`/`300k` budgets. That direction is safe — the ≥5 s floor gets easier,
not harder — and the cost is in the Cost table below. If a session has to be
shortened, cut **reps**, or cut the budget on **both** halves together; never
lower one half's budget alone.

**These arms measure a submit rate, and that is deliberate.** `osrate` can also
measure how fast documents become *searchable* — `--index-watch`, with
`--samples-dir` for the per-second series — but it is off by default and these
arms leave it off, because their numbers are what Part B has always reported and
a watch adds a poll per second and a settle wait per level. The script forwards
`"$@"`, so a build-rate arm is a separate run rather than an edit:

```
ssh fts-harness 'REPS=3 BATCH=1 LADDER=8,16,32 MAX_DOCS=400000 \
    ~/run-os-arm.sh os-build --index-watch --samples-dir /mnt/nvme/work/samples-os/os-build'
```

Read its `index_docs_per_s` with the refresh caveats under "The growth chart"
below — on this half that column is gated by `refresh_interval`, and against the
null sink it is gated by `--os-refresh-interval-ms`.

### There is no batch sweep — `batch=1` only

Part B used to run one whole ladder per batch level (`1, 16, 64, 128, 256,
512, 1024`) and draw the fan they made. **That sweep is not run.** One batch
size is measured, and it is `1`.

**Why `1` is the level to keep if only one is kept.** At `--batch-size 1` the
two units coincide — one document per request — which is the only shape whose x
axis matches Part A's, and therefore the only place a `scyllarate`-vs-`osrate`
gap is about the clients rather than about framing. It is also the level that
isolates per-request framing cost with bulking switched off. Every other level
measures how well bulking amortises that cost, which is a real question about
the client but not the one this campaign is asking.

**What is given up, and it is not nothing.** The batch fan was the diagnostic
that told a sink ceiling from a client ceiling *by contrast*: the small-batch
levels pay the sink's per-request cost in full and the large ones do not, so a
plateau shared by every level was the instrument and a plateau only the small
ones hit was framing. With one level there is no contrast — so the sink CPU
sampler is now the **only** thing standing between a sink ceiling and a number
reported as the client's — and with no N-process arm either, every plateau on
this half is a lower bound by default. Read "Expect to be surprised" below
before quoting anything from this half.

**Restoring the sweep** means putting back a loop over batch levels, each
running the full shared grid in the same two sub-sweeps, each with its own
`--max-docs` (the rate range is wide enough that a budget giving `batch=1024`
six seconds gives `batch=1` several minutes) — never a single point per batch
and never a ladder tailored per level, or the levels leave the shared x grid and
the chart cannot be drawn.

### There is no N-process arm here either

One `osrate` process against one HTTP sink, for the reason Part A gives in
"There is no N-process arm": the deliverable is a floor the engine campaign
compares against, not the harness's ceiling, and the campaign loads through a
single process. A sink that pegs makes the level a **lower bound** — write it
`≥` and say the sink pegged. It does not make the level invalid.

## B5 — collect

```bash
scp 'fts-harness:/mnt/nvme/work/results-os/*'  $R/opensearch/points/
scp -r 'fts-harness:/mnt/nvme/work/samples-os/*' $R/opensearch/samples/  # only with --index-watch
scp  fts-sut:/tmp/sinks-cpu.tsv                $R/opensearch/sinks/
scp 'fts-sut:/tmp/sink-92*.log'                $R/opensearch/sinks/
scp 'fts-sut:/tmp/sink-92*.json'               $R/opensearch/sinks/
mv $R/opensearch/points/*.stderr.tsv           $R/opensearch/logs/
mv $R/opensearch/points/os-windows.tsv         $R/opensearch/
```

Same verification gate as Phase 6, plus two more:

```bash
# the batch_size COLUMN (8) agrees with the filename on every data row --
# checking only the header would miss a mislabelled file
for f in $R/opensearch/points/*.csv; do
  want=$(basename "$f" | sed 's/.*-b\([0-9]*\)-rep.*/\1/')
  got=$(awk -F, '!/^#/ && $1!="concurrency" {print $8}' "$f" | sort -u | paste -sd,)
  [ "$want" = "$got" ] && echo "  OK   $(basename $f) batch=$got" \
                       || echo "  BLAD $(basename $f) name=$want column=$got"
done

# no failed inserts (col 3) and no rejected requests (col 10) anywhere.
# A 429 in the first failure is queue rejection, not saturation.
# Columns 3, 4, 8 and 10 kept their positions through the schema merge, so these
# three gates read the same fields they always did -- and now also run unchanged
# against Part A's CSVs.
awk -F, '!/^#/ && $1!="concurrency" && ($3+0>0 || $10+0>0) \
         {print FILENAME": errors="$3" failed_requests="$10}' $R/opensearch/points/*.csv

# every point ran long enough to be a measurement (wall_s is col 4)
awk -F, '!/^#/ && $1!="concurrency" && $4+0<3 \
         {print FILENAME": c="$1" batch="$8" only "$4"s -- raise --max-docs and re-run"}' \
  $R/opensearch/points/*.csv
```

---

## Traps, all of them met in practice

1. **One sink is one core, and that is the wall.** The sink advertises neither
   the shard extension nor a populated `system.peers`, so the driver opens
   **one** connection (`connections=1`, `shard_aware=false` in every header) and
   all traffic funnels into one Python process. It costs ~3.6 µs of CPU per
   insert, so it saturates near 230–280k inserts/s — which this harness reaches
   on its own. **The single-process plateau is the sink's number, not the
   harness's.** Check `sink_cores` before quoting anything.

2. **The sink degrades as it runs.** The same ladder, same box, same corpus,
   13 minutes later: plateau down from ~227k to ~198k docs/s at unchanged sink
   CPU. Restart the sinks between arms whose numbers will be compared, and note
   in the write-up when each arm's sinks were started.

3. **There is no single-process window where the harness's own ceiling shows.**
   `c=4…8` is bounded by RTT (`in-flight / 0.133 ms`), `c≥96` by the sink, and
   the middle is a transition. A hard single-process ceiling needs the sink
   changed so one driver connection can reach more than one core — advertising
   the sibling sinks in `system.peers` is the obvious route. **Not done, and
   deliberately so**: the campaign needs a floor, not a ceiling, and the
   single-process figure is that floor. It is a lower bound and must be written
   `≥`. Revisit only when an engine number comes within ~2x of it — the change
   is to the frozen harness and needs a decision, not a drive-by patch.

4. **Do not skip `c=4` and `c=8`.** They are cheap, they are what the crate's own
   usage line documents, and they are the only levels where the sink is far from
   its ceiling. The 2026-09-10 pass started at 16 and lost them for ~5 minutes
   of saved runtime.

5. **N=3 does not converge the rising limb.** Over three reps the `c=64` point
   read 139.9k → 172.4k → 208.0k, climbing monotonically, with both boxes drawing
   more CPU as the session went on. That pass still had a warm-up row per ladder
   and it was not enough; these ladders have none at all, so the first rung of
   every rep is now a reported point measured cold. Either run more reps, or
   re-run one arm late in the session and compare, and report the rising limb as
   unconverged if it moves.

6. **Samplers must append, and their files must survive a restart.** A sampler
   that truncates on start destroys the record for every arm already measured.
   The 2026-09-10 pass lost the sink series for two whole arms that way.

7. **`pkill -f` inside an ssh one-liner kills the ssh session.** Put it in a
   script on the box.

8. **The system `python3` on both boxes is 3.9 and cannot import `ftsbench`.**
   Use `python3.12`.

9. **Root volume free space is asymmetric since 2026-09-11.** The harness
   root is 32 GiB (holds `corpus.jsonl.zst`, ~10.2 GB); the SUT root is still
   ~4 GB free. Build outputs and the uncompressed corpus go on `/mnt/nvme`,
   which must be `mkfs`'d after every start.

10. **The working tree can move mid-session.** Freeze the binary, record what it
    was built from, and flag any arm that exercised a flag the current source no
    longer has.

### OpenSearch side only

11. **`osrate`'s `p99` is per `_bulk` request.** It rises with `--batch-size` by
    construction. Reporting that as a latency regression, or plotting it beside
    `scyllarate`'s per-document `p99`, is the standing mistake on this half.

12. **`queue_depth * concurrency * batch_size` is a document count, and it is
    resident.** The default depth of 10 at `c=384 batch=512` is ~7.8 GB on this
    corpus. 61 GiB and **no swap** means an overshoot is an OOM kill. At
    `batch=1` the product is ~5 MB and the depth is left alone; check it before
    each arm the moment a batch level is restored. *(Dormant while Part B runs
    `batch=1` only.)*

13. **One concurrency cannot serve every batch level.** The knee moves with
    batch size, so a fixed `c` compares each level at a concurrency that suits
    only one of them. Ladder every batch level. *(Dormant while Part B runs
    `batch=1` only — live again the moment a second level is added.)*

14. **`--max-docs` cannot serve every batch level either.** The rate range
    across `batch=1 … 1024` is large enough that one budget makes either the
    top level 1 s or the bottom level minutes. Size it per level and record it.
    *(Dormant for the same reason. Note this is a rule about batch **levels**,
    not about the two halves: across the halves the budget is deliberately
    shared — `400k`/`1.25M` on both — and `batch=1` being the slow end is paid
    in wall time, not in a smaller budget. Earlier revisions ran Arm 1 at
    `100k`/`300k`; those CSVs do not plot against these.)*

17. **Batch size is a series, never an axis, and the concurrency grid is
    shared.** Tailoring a ladder to one batch level takes it off the shared x
    values and the campaign's grid chart can no longer be drawn. A level still
    rising at `c=128` is reported as "unresolved above 128".

15. **`unknown` in an `osrate` header against the sink is correct.** The sink
    does not answer `GET /<index>/_settings`; the crate degrades per field
    rather than failing. Do not chase it, and do not repoint the run at a real
    OpenSearch to make it go away. The analyzer probe is the one check that does
    *not* degrade — it fails the run — which is why Part B passes
    `--no-analyzer-check`. It does **not** pass `--no-reset`: both halves reset
    per level, and dropping one half's reset is what would make the two
    incomparable.

16. **The HTTP sink saturates earlier than the CQL one at small batches**, since
    it pays a full request parse per document there. `batch=1` — the only level
    this campaign runs — should be expected to come back sink-bound, and it is
    quoted as a lower bound. That is the deliverable, not a failure: compare it
    against the engine number and say the sink pegged.

---

## Cost

Two `i8g.2xlarge` on-demand in `eu-north-1`.

| | wall |
|---|---|
| bring-up, toolchain, both builds | ~18 min |
| corpus generation (once, serves both parts) | ~12 min |
| Part A measurement (two sweeps, N=3, one process) | ~10 min |
| Part B: one concurrency ladder at `batch=1`, N=3, reset per level | ~20-25 min |
| collect, verify, stop | ~5 min |
| **both parts, one session** | **~1 h 30 min – 1 h 50 min** |

Both measurement rows are estimates until a session times them. They are well
below what the same rows used to say, for three reasons that all landed at once:
the grid now stops at 128, the batch sweep is gone (seven ladders down to one),
and the N-process arms are gone (nine more ladder-equivalents down to zero).
Part B's row grew a little when its per-level reset was restored — a `DELETE`, a
`PUT` and two gate polls on each of six levels, three times over — which is the
price of both halves carrying the same overhead.

It then grew again, and by more, when its budgets were raised to Part A's:
`100k`/`300k` pushed 3.9 M documents across the half, `400k`/`1.25M` pushes
16.05 M — the same count Part A pushes, which is the point — and `batch=1` is
the slower end of the two clients. The ~20-25 min above assumes it is roughly
twice Part A's row; **no session has timed it.** Time it and write the real
number here. If it lands far above that, the lever is reps or a smaller budget
**on both halves**, never on this one alone.

Standing cost between sessions is the root EBS volumes, billed whether the
instances run or not. The harness root went 8 GiB -> 32 GiB gp3 on
2026-09-11, which adds **~$2/month** (~$0.003/h) — a rounding error against
either box's hourly rate, and it buys back ~34 min of corpus staging on every
restart.

The batch sweep used to be the expensive half — seven levels, each a ladder,
each N=3 — and dropping it is most of why a session now fits comfortably in an
hour. If one is ever restored and the session has to be shortened, drop batch
levels from the **middle** (`64`, `256`) and never the ends: `1` is what makes
the curve interpretable and `1024` is what shows whether it has flattened.
Cutting the corpus (fewer documents, or 400 B lines) is the other lever.

## Recorded results

| Run | What it established |
|---|---|
| `results/fleet-rust-harness-null-sink-2026-09-10` | single-process default ≥266,578 docs/s (sink-bound); aggregate ceiling ~378,400 docs/s ≈ 1.49 GB/s at 3,948 B, reached at 2 processes; ~756,700 docs/s at 400 B; cost model 8.2 µs/doc + 2.3 ns/byte. Missing `c=4`/`c=8`. |

# The charts — run these last, after the boxes are stopped

Two of them, and they answer different questions off the same run. **The grid**
is the campaign's deliverable: what one client can offer, per concurrency, for
each harness. **The growth chart** is Part A only, and says what
one level did while it ran, which the grid's per-level averages cannot.

Nothing here touches AWS, so both happen **after Phase 7**, on the laptop, off
the downloaded artifacts alone. If either cannot be produced from `$R` without
an ssh, something was not collected and Phase 6's gate was skipped.

## The grid — X is concurrency

**X is concurrency, Y is docs/s, and every harness-and-batch combination is a
series on it.**

```
.venv/bin/python3 tools/plot_harness_grid.py \
    --keep-warmup \
    --scylla     "$R/scylla/points/default-*-rep*.csv" \
    --opensearch "$R/opensearch/points/os-*-rep*.csv" \
    --output     "$R/harness-grid.png" \
    --table      "$R/harness-grid.csv" \
    --subtitle   "$RUN_ID · i8g.2xlarge · null sink · N=3"
```

Two series off the default matrix:

| Series | From |
|---|---|
| `scyllarate CQL 1 doc/op` | Part A, `default-low` + `default-high` |
| `osrate batch=1` | Part B, `os-conc-low` + `os-conc-high` |

**Two series at one document per request is the whole chart, so column 17 is
now the only thing telling them apart.** Both halves write `batch_size=1`, so
`plot_harness_grid.py`'s pre-`engine` fallback — which decides by whether a
`batch_size` column exists — would label every ScyllaDB point `osrate batch=1`
and average the two lines into one. Any CSV old enough to lack column 17 must
not be plotted with these.

`--table` writes the same numbers as CSV — series, concurrency, reps, median,
min, max, shortest wall — because **the chart is for looking and the table is
for reading**, and a plateau nobody can get the numbers out of is not evidence.

## This is a diagnostic chart, not a deck chart

It draws **every** series on purpose. Picking a legible subset is a later and
separate decision, made for a slide, from this chart.

That is also why it does not use `ftsbench.plotlib`'s deck palette. Eight lines
do not fit the deck's rules — the validated palette separates about three
series inside one hue family, and eight ramp steps land at ΔE ~7 against a floor
of 15 — so identity here is carried by **three redundant channels** instead: an
ordered colour ramp, a per-series marker, and a direct label at each line's
right end. Nobody has to resolve two similar oranges. Do not "fix" this chart
by cutting series; cut them when a slide needs one.

## What the axes do and do not say

The footer states this on the image, and it is the one thing to get right:

- **x is a client knob and does not mean the same thing on both engines.** One
  `scyllarate` unit is one in-flight prepared INSERT carrying **one** document.
  One `osrate` unit is one in-flight `_bulk` carrying `batch_size` of them.
  Documents in flight is `concurrency * batch_size`, so at `c=128 batch=512`
  the OpenSearch side has 65,536 documents outstanding against ScyllaDB's 128.
- **`batch=1` is the only level where the two x axes are the same shape**, and
  therefore the only place a `scyllarate`-vs-`osrate` gap is about the clients
  rather than about framing.
- **Nothing on it is an engine number.** Null sink only.
- A point is the **median** of its repetitions; the bar is min..max.
- Every ladder row is a measured point: the ladders carry no throwaway first
  level, which is why the render passes `--keep-warmup`.

## Read it in this order

1. **Sink CPU first, then the curve.** Cross-check every plateau against
   `SUMMARY.txt` / the sink columns: a series that flattened with its sink at
   ≥0.85 of a core flattened on the *instrument*. On the 2026-09-10 pass every
   `scyllarate` point above c≈96 was in that state. Such a plateau is a **lower
   bound** and must be described as `≥`, whatever the chart looks like.
2. **Then the batch spacing.** The gap between adjacent batch series is what
   bulking bought. If `batch=128`, `256`, `512` and `1024` lie on top of each
   other, that is the result: bulking buys nothing past ~128 for this client on
   this box, and the campaign can pick the smallest batch on the plateau
   instead of the largest.
3. **Then the short-point warning.** The footer names every point under 3 s.
   Those are not measurements; raise their `--max-docs` and re-run those levels
   before anyone reads the shape they make.

## Expect to be surprised in one specific way

**`batch=1` is the level most likely to be measuring the sink, and it is now
the only level there is.** The HTTP sink scans every bulk body to count its
actions, so its cost is per *request* — and at one document per request Part B
pays that cost in full on every document. The batch fan used to expose this by
contrast; with one level, a plateau has nothing to be compared against.

So on this half, treat a plateau as the instrument's until proven otherwise:

- **Sink CPU is the primary evidence.** Cut the sampler to each level's window.
  A level whose sink sat at ≥0.85 of a core is a **lower bound** on the client
  and must be written `≥`, not reported as a ceiling.
- **Nothing here breaks the tie, and nothing needs to.** Seeing past one sink's
  one core would take N processes on N sinks, which this campaign does not run.
  A flattened level is reported `≥` and compared against the engine number; it
  only becomes a problem if an engine number approaches it.
- **Some of the per-request cost is the harness's own.** A worker builds its
  NDJSON before it posts and the clock starts before the encode, deliberately.
  That serialization is the honest answer to "what does the harness do" — not an
  artifact — so say which part is encode and which is sink; do not let the
  reader assume indexing.

For the *Python* OpenSearch loader the per-process ceiling was **flat in batch
size** — 26.5k–27.7k docs/s across 16/64/128/256/512 — because that client's
cost was per document (`../TUNING.md`). This client is different and, with one
batch level, this campaign cannot answer that question at all. Do not let a
single `batch=1` number be read as "batch size does not matter here".

## The growth chart — X is the index itself

Both halves write a series now, but **chart them separately**. Run Part A's as
below; for Part B pass `--samples "$R/opensearch/samples/*/c*.csv"` and its own
`--output`.

```
.venv/bin/python3 tools/plot_build_growth.py \
    --samples  "$R/scylla/samples/default-high-rep*/c*.csv" \
    --output   "$R/build-growth.png" \
    --table    "$R/build-growth.csv" \
    --subtitle "$RUN_ID · i8g.2xlarge · null sink · N=3"
```

One glob covering both is mechanically fine — the series carry their batch size
in the file name, so they cannot collide — but the y axes are not comparable
point by point. The x axis is: "documents this level made searchable" means the
same thing on both. See the refresh note below before putting them on one image.

X is the documents that level put in the index, Y is how fast they went in, one
thin line per repetition and a bold pointwise median per concurrency. It is the
chart that answers questions the grid cannot:

- **Was the plateau a plateau, or an average of two halves?** A level that ran
  at full speed and then stalled reports the same mean as one that ran evenly.
- **Where did the client stop?** The tick on each line marks it. Everything to
  its right was indexed after the last insert landed, and against a real engine
  that tail is the build finishing rather than the client being slow.
- **Is a level long enough to have a shape at all?** Under three readings it is
  skipped by name — which is the same signal as the grid's short-point warning,
  read from the other side.

Against the CQL null sink both series move together by construction: the sink
counts a document into its modelled index as it accepts it, so the two lines lie
on top of each other and any gap between them is the harness's own. **That is
the point of running it here** — it is the zero reading the engine campaign's
version of this chart is read against.

**On the OpenSearch half they do not, and that is not the harness.** A searchable
count only advances when the index refreshes, so `docs_indexed` climbs in steps
while `docs_accepted` climbs smoothly: flat, flat, then a jump carrying the whole
interval's work. Read the gap between the two columns as the refresh policy, not
as lag the loader caused. Three consequences worth knowing before reading the
numbers:

- **`index_lag_docs` has a floor on this half.** It counts documents not yet
  *refreshed* when the client stopped, so at `refresh_interval=3s` it is at least
  three seconds of submit rate for any level however fast the engine. The number
  to read is the excess over `refresh_interval × docs_per_s`.
- **The chart widens its bucket to one riser** when a series steps, and says so
  in its footer. A finer bucket would land inside a jump and read the rate high
  by the refresh-to-poll ratio.
- **`index_status=refreshed` means the harness asked.** After the engine has
  accepted everything and stopped, a level that still has nothing searchable
  asks the index to publish, once. That is a real answer to "how long until it
  is searchable if someone asks", and it is not what the configured refresh
  policy would have delivered — at `refresh_interval: -1`, nothing.
  `--no-index-final-refresh` turns it off and the level reports the policy.

The null sink can produce all of this: `--os-refresh-interval-ms 3000` models a
3s refresh, and `=-1` models an index that never publishes on a timer. Its
default of 0 publishes immediately, which is the behaviour every recorded run
measured.

## Then write it down

Put `harness-grid.png`, `harness-grid.csv` and `build-growth.png` at the top of
`$R/README.md`, with the sink-bound series named in the caption, and hand the
user the absolute path of `$R`.
