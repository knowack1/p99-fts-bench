# Harness-on-AWS runbook — measuring what the loader can offer, on the fleet

**Hand this file to Claude Code as the instruction and it runs the whole
campaign: starts the two AWS boxes, builds both harnesses, measures each against
its own accept-and-discard sink, pulls every artifact home, stops the boxes.**
It is self-contained — every script it needs is inline below. Nothing else in
`bench/` has to be read first.

There are **two harnesses and they are measured in this order**, in one fleet
session, off one corpus:

| Part | Harness | Binary | Sink | Extra axis |
|---|---|---|---|---|
| **A** | `bench/scylla-build-rate` | `scyllarate` | `null_sink --mode cql` (+ its vector-store port) | — |
| **B** | `bench/opensearch-build-rate` | `osrate` | `null_sink --mode http` | **one full run per `--batch-size`** |

Part B repeats its whole ladder **once per batch size**, because on the
OpenSearch side one request carries many documents and the point is to see what
batch size does to the harness's throughput. **Batch size is never an axis: it
is a series.** Every batch level is measured on the same concurrency grid, so
they all land on one chart as separate lines — see "The charts" at the end.
Part A has no batch levels at all: one CQL operation is one document.

Run Part A first. It is the simpler instrument and it establishes the corpus,
the samplers and the box's CPU baseline that Part B is read against.

## What this measures, and what it is not

The subject is **the loader**, not an engine. `scyllarate`
(`bench/scylla-build-rate`) pushes prepared `INSERT`s at `ftsbench.null_sink
--mode cql`, which answers the CQL wire and discards every row. What comes back
is what the *client* can offer on a given box — the ceiling a real build-rate
point has to stay below to be measuring ScyllaDB rather than the tester.

**No number from this runbook is an engine number and none belongs in the deck.**

Not to be confused with:

| File | Its job |
|---|---|
| `AWS-RUN-PLAN.md` | the **engine** campaign on AWS (C1–C8, real ScyllaDB + OpenSearch) |
| `BUILD-RATE-MATRIX-PLAN.md` § "P0 — client calibration" | the same idea for the **Python** loaders, and the constants they feed |
| `TUNING.md` § "Per-process client ceilings" | where measured ceilings get recorded |
| `HARDWARE.md` | why the fleet is shaped the way it is, and what it costs |

This runbook supersedes the AWS mechanics in all of them **for harness runs
only**. When it produces a ceiling, record it in `TUNING.md` with the run that
produced it.

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
mkdir -p "$R"/scylla/{points,samples,samples-nproc,logs,nproc,sinks}
mkdir -p "$R"/opensearch/{points,logs,batch,sinks}
printf '%s\n' "$RUN_ID" > "$R/RUN_ID"
ln -sfn "$RUN_ID" "$(dirname "$R")/harness-aws-latest"
echo "results -> $R"
```

giving, for example:

```
bench/results/harness-aws-runbook-2026-09-10T1845Z/
├── RUN_ID  env/  corpus/  scripts/     # shared by both parts
├── scylla/      points/ samples/ samples-nproc/ logs/ nproc/ sinks/
└── opensearch/  points/ logs/ batch/ sinks/
bench/results/harness-aws-latest -> harness-aws-runbook-2026-09-10T1845Z
```

The two harnesses keep separate subtrees because their CSVs are **not
interchangeable**: `osrate`'s `p50_ms`/`p99_ms` are per `_bulk` request and its
rows carry a `batch_size` column. A flat directory invites someone to plot one
against the other.

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
| `default-low` | ladder `4,4,8,16,32`, `--max-docs 400000` | the RTT-bound end; also the only region where the sink is nowhere near its ceiling |
| `default-high` | ladder `32,32,64,128,256,512`, `--max-docs 1250000` | the plateau and the knee |
| `nproc1/2/4` | one process per own sink, at the knee | the only arm that can see past one sink's one core (see the trap below) |

### The shared concurrency grid — do not vary it per arm

**Every series in both parts is measured on the same x values:**

```
4   8   16   32   64   128   256   512
```

The campaign's deliverable chart puts concurrency on x and every
harness-and-batch combination on it as a **series** (see "The charts" at the
end). Series that do not share x values cannot be drawn on one axis, so a
ladder tailored per arm — a taller one for small batches, say — silently
destroys the chart. If a level has to be added, add it to **every** arm.

Powers of two, because x is drawn on a log2 axis.

**Two sub-sweeps per series, overlapping at 32.** One `--max-docs` must serve
every level in a sweep, and the rate range across `4…512` is wide enough that
one budget makes either the top point 2 s or the bottom point 40 s. So: a low
sweep (`4…32`) at a small budget, a high sweep (`32…512`) at a large one, and
`32` measured by both. If the two disagree at `32` by more than the rep spread,
the budgets are distorting the measurement — report that rather than averaging
it away.

**N=3 repetitions of every ladder, minimum.** Every ladder repeats its first
level as a throwaway warm-up row that is dropped from all aggregates.

**Two sweeps, not one, and they must overlap.** One `--max-docs` has to serve
every level in a sweep, and the rate range across `4…512` is over 20x, so a
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
> `S3-CORPUS-STAGING-PLAN.md`.

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
cd <repo>/bench && tar czf - --exclude=target scylla-build-rate \
  | ssh fts-harness 'mkdir -p /mnt/nvme/work && tar xzf - -C /mnt/nvme/work'

ssh fts-harness 'cd /mnt/nvme/work/scylla-build-rate && . "$HOME/.cargo/env" \
  && CARGO_TARGET_DIR=/mnt/nvme/work/target cargo build --release --locked'
```

Record, into `$R/env/`, **before** measuring:

```bash
ssh fts-harness 'cd /mnt/nvme/work/scylla-build-rate && find . -type f \
  \( -name "*.rs" -o -name "Cargo.*" \) | sort | xargs sha256sum | sha256sum'
git -C <repo>/bench log -1 --format='%H %s'
git -C <repo>/bench status --short scylla-build-rate
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

One sink per loader process. Even a single-process arm should be run against a
sink that nothing else is touching.

```bash
ssh fts-sut 'cat > ~/start-sinks.sh << "EOF"
#!/bin/bash
# One sink per loader process. A sink is single-threaded and sits behind a
# single driver connection, so N loader processes need N sinks on N ports.
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

ssh fts-sut '~/start-sinks.sh 9042 9043 9044 9045'
ssh fts-sut 'cat /tmp/sinks.pids; ss -ltn | grep -E "904[2-5]"'
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
LADDER="${LADDER:-32,32,64,96,128,192,256,384,512}"
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
ssh fts-harness 'REPS=3 LADDER=4,4,8,16,32        MAX_DOCS=400000  ~/run-arm.sh default-low'
ssh fts-harness 'REPS=3 LADDER=32,32,64,128,256,512 MAX_DOCS=1250000 ~/run-arm.sh default-high'
```

Each sweep takes 80–100 s per rep. Run them with a generous timeout or in the
background; do not poll every few seconds — it wastes the session.

### The N-process arm

```bash
ssh fts-harness 'cat > ~/run-nproc.sh << "SCRIPT"
#!/bin/bash
# N loader processes, each with its OWN sink on its own port, all at one
# concurrency. This is the only arm that can see past one sink's one core.
set -u
NPROC="$1"
REPS="${REPS:-3}"; CONC="${CONC:-128}"
CORPUS="${CORPUS:-/mnt/nvme/work/corpus.jsonl}"
MAX_DOCS="${MAX_DOCS:-1250000}"
SINK="${SINK:-172.31.47.166}"; BASE_PORT="${BASE_PORT:-9042}"
OUT_DIR="${OUT_DIR:-/mnt/nvme/work/results-nproc}"
SAMPLES_DIR="${SAMPLES_DIR:-/mnt/nvme/work/samples-nproc}"
BIN=/mnt/nvme/work/target/release/scyllarate
mkdir -p "$OUT_DIR" "$SAMPLES_DIR"
WINDOWS="$OUT_DIR/nproc-windows.tsv"
[ -f "$WINDOWS" ] || printf "nproc\trep\tproc\tport\tstart_epoch\tend_epoch\texit_code\tcsv\n" > "$WINDOWS"

for rep in $(seq 1 "$REPS"); do
    echo "######## nproc=$NPROC rep=$rep conc=$CONC $(date -u +%H:%M:%S)"
    start=$(date +%s); pids=()
    for proc in $(seq 0 $((NPROC - 1))); do
        "$BIN" --corpus "$CORPUS" --concurrency "$CONC" --max-docs "$MAX_DOCS" \
               --hosts "$SINK" --port $((BASE_PORT + proc)) \
               --vs-url "http://$SINK:$((BASE_PORT + proc + 7000))" \
               --out "$OUT_DIR/nproc$NPROC-rep$rep-p$proc.csv" \
               --samples-dir "$SAMPLES_DIR/nproc$NPROC-rep$rep-p$proc" \
               > "$OUT_DIR/nproc$NPROC-rep$rep-p$proc.stderr.log" 2>&1 &
        pids+=($!)
    done
    for proc in $(seq 0 $((NPROC - 1))); do
        wait "${pids[$proc]}"
        # capture immediately: any command substitution below would overwrite $?
        code=$?
        end=$(date +%s)
        printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" "$NPROC" "$rep" "$proc" \
            "$((BASE_PORT + proc))" "$start" "$end" "$code" \
            "$OUT_DIR/nproc$NPROC-rep$rep-p$proc.csv" >> "$WINDOWS"
    done
done
SCRIPT
chmod +x ~/run-nproc.sh'

ssh fts-harness 'for n in 1 2 4; do REPS=3 CONC=128 ~/run-nproc.sh $n; done'
```

**The aggregate is not the sum of the per-process averages.** The processes do
not finish together, and a survivor speeds up once the others exit — at N=4 a
process ran at ~66k docs/s for ten seconds and then ~190k for three. Summing the
reported averages overstated N=4 by 16%. Sum the per-second series across
processes instead: `--samples-dir` writes one row per reading, with `t_s`
measured from the level's own start, so the join is on `t_s` rather than on
line index — and count only the seconds in which every process still had a
reading. (Before the series existed this was done by hand off the stderr
progress lines, which is where the numbers above came from.)

---

## Phase 6 — collect and verify, BEFORE stopping the boxes

`$R` is the directory fixed at the start of the session — see "The results
directory" above. Do not recompute it here.

```bash
test -n "$R" && test -d "$R" || { echo "R is unset: recover it with"; \
  echo '  export R="$(readlink -f bench/results/harness-aws-latest)"'; }

scp 'fts-harness:/mnt/nvme/work/results/*'        $R/scylla/points/
scp -r 'fts-harness:/mnt/nvme/work/samples/*'     $R/scylla/samples/
scp -r 'fts-harness:/mnt/nvme/work/samples-nproc/*' $R/scylla/samples-nproc/
scp 'fts-harness:/mnt/nvme/work/results-nproc/*'  $R/scylla/nproc/
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
awk -F, '!/^#/ && $1!="concurrency" && $3+0>0 {print FILENAME": errors="$3}' $R/scylla/points/*.csv $R/scylla/nproc/*.csv
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
`results/fleet-rust-harness-null-sink-2026-09-10/{summarize,summarize-nproc,aggregate-contended}.py`.

Write a `README.md` in `$R` carrying: the topology and RTT, the corpus manifest
with checksums, the binary provenance, the arm table, the gate column explained,
and — first, before any number — what the run does **not** license anyone to
claim. Open it with the run's own identity, so the directory explains itself
without reference to this file:

```markdown
# <one line: what was measured>

Run `harness-aws-runbook-2026-09-10T1845Z`, produced by
`bench/HARNESS-AWS-RUNBOOK.md`. Fleet up <HH:MM>–<HH:MM> UTC on <date>.
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

`osrate` (`bench/opensearch-build-rate`) posts hand-built NDJSON to `POST
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

The CSV columns, in order — the first seven are shared, and then the two halves
diverge: `osrate` has ten columns where `scyllarate` has thirteen, so an `awk`
field index written past column 7 for one half is wrong on the other:

```
both     1 concurrency  2 docs  3 errors  4 wall_s  5 docs_per_s  6 p50_ms  7 p99_ms
osrate   8 batch_size  9 bulks  10 failed_bulks
scyllarate  8 index_docs  9 index_docs_per_s  10 index_lag_docs
            11 index_settle_s  12 index_settled  13 index_status
```

`scyllarate`'s index columns come from the sink's vector-store half, which
reports the documents its CQL half accepted. They give the **build-rate**
figure its own client ceiling, measured with the reading the engine campaign
uses (`ftsbench.samplers.ScyllaSampler`). `osrate` has no counterpart today.

**The corpus is the same file.** `osrate` does not read the line's `uuid` — it
is ScyllaDB's partition key — so the corpus built in Phase 4 serves both parts
unchanged. Do not regenerate it; that is ~12 minutes of billed fleet time for
nothing.

## B1 — build it

The sink speaks plain HTTP, so build without the default TLS feature: smaller
binary, and the run cannot depend on the box's OpenSSL.

```bash
cd <repo>/bench && tar czf - --exclude=target opensearch-build-rate \
  | ssh fts-harness 'tar xzf - -C /mnt/nvme/work'

ssh fts-harness 'cd /mnt/nvme/work/opensearch-build-rate && . "$HOME/.cargo/env" \
  && CARGO_TARGET_DIR=/mnt/nvme/work/target-os \
     cargo build --release --locked --no-default-features'
```

Record its provenance into `$R/env/` exactly as in Phase 2, and freeze it for
the same reason. A separate `CARGO_TARGET_DIR` keeps Part A's binary untouched.

## B2 — the HTTP sinks

Same launcher, `--mode http`, ports 9200+. Start them **fresh** for Part B and
note the time; the CQL sinks from Part A can be left running or stopped, they
are on different ports either way.

```bash
ssh fts-sut 'cat > ~/start-http-sinks.sh << "EOF"
#!/bin/bash
# One HTTP sink per loader process, on 9200+. Same one-core-per-sink limit as
# the CQL side: the sink scans every _bulk body to count its actions, so its
# cost is per document and it is the first thing to saturate.
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

ssh fts-sut '~/start-http-sinks.sh 9200 9201 9202 9203'
ssh fts-sut 'cat /tmp/sinks.pids; ss -ltn | grep -E "920[0-3]"'
```

The CPU sampler from Phase 3 reads `/tmp/sinks.pids` and needs no change — but
it appends to the same file, so **record the wall-clock time Part B's sinks
started** and cut Part B's windows after it.

### Part B runs with `--no-reset`, and that is deliberate

`osrate` deletes and recreates the index before every level by default, so that
each level builds from zero documents. Part B is not measuring an index build —
it is measuring the client against an accept-and-discard sink — so the reset
buys nothing here, and the analyzer probe a reset run makes (`_analyze`, once,
before the first document) is a route the sink does not answer, which would fail
the run. `run-os-arm.sh` therefore passes `--no-reset`; `RESET_FLAGS` overrides
it if a run against a real OpenSearch is ever driven by the same script, where
the default (reset on) is what you want.

### The header will say `unknown`, and that is correct

`osrate` reads index settings, mappings and the node thread pool for its header.
The sink answers `HEAD` (so the index-exists check `--no-reset` makes passes),
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

**Run the whole of Part B at `--queue-depth 2`** and hold it there. At depth 2
the worst cell below (`c=192 batch=1024`) buffers 393k documents ≈ 1.6 GB, and
the read-ahead only ever mattered at the bottom of the ladder anyway. Depth is
in the CSV header; holding it fixed is what makes the batch levels comparable,
and a run at a different depth must not be plotted against these.

Watch it anyway — the box has 61 GiB and no swap, so an overshoot is an OOM
kill, not a slowdown:

```bash
ssh fts-harness 'while pgrep -x osrate >/dev/null; do \
  ps -o rss= -C osrate | awk "{s+=\$1} END {printf \"osrate RSS %.1f GB\n\", s/1048576}"; \
  sleep 5; done'
```

## B4 — the arms

### Arm 1 — the concurrency ladder at the default batch

Locates the knee, and is the direct counterpart of Part A's ladder.

```bash
ssh fts-harness 'cat > ~/run-os-arm.sh << "SCRIPT"
#!/bin/bash
# One osrate arm: a concurrency ladder at ONE batch size, N times, against the
# HTTP sink on fts-sut. stderr is timestamped per line so each point's window
# can be cut out of the CPU samplers, exactly as on the ScyllaDB side.
set -u
ARM="$1"; shift
REPS="${REPS:-3}"
LADDER="${LADDER:-24,24,48,96,192,384}"
BATCH="${BATCH:-512}"
QUEUE_DEPTH="${QUEUE_DEPTH:-2}"
CORPUS="${CORPUS:-/mnt/nvme/work/corpus.jsonl}"
MAX_DOCS="${MAX_DOCS:-1250000}"
SINK_URL="${SINK_URL:-http://172.31.47.166:9200}"
INDEX="${INDEX:-wiki-articles}"
# Part B measures the CLIENT against a null sink, not an engine's index build,
# so the per-level index reset buys nothing here — and the sink cannot answer
# the analyzer probe a reset run makes. See B0 below.
RESET_FLAGS="${RESET_FLAGS:---no-reset}"
OUT_DIR="${OUT_DIR:-/mnt/nvme/work/results-os}"
BIN=/mnt/nvme/work/target-os/release/osrate

mkdir -p "$OUT_DIR"
WINDOWS="$OUT_DIR/os-windows.tsv"
[ -f "$WINDOWS" ] || printf "arm\tbatch\trep\tstart_epoch\tend_epoch\texit_code\tladder\tmax_docs\tqueue_depth\tcsv\n" > "$WINDOWS"
stamp() { awk "{ printf \"%d\t%s\n\", systime(), \$0; fflush() }"; }

for rep in $(seq 1 "$REPS"); do
    csv="$OUT_DIR/$ARM-b$BATCH-rep$rep.csv"
    log="$OUT_DIR/$ARM-b$BATCH-rep$rep.stderr.tsv"
    echo "######## arm=$ARM batch=$BATCH rep=$rep $(date -u +%H:%M:%S)"
    start=$(date +%s)
    "$BIN" --corpus "$CORPUS" --concurrency "$LADDER" --batch-size "$BATCH" \
           --max-docs "$MAX_DOCS" --queue-depth "$QUEUE_DEPTH" \
           --url "$SINK_URL" --index "$INDEX" --out "$csv" $RESET_FLAGS "$@" 2>&1 | stamp > "$log"
    code=${PIPESTATUS[0]}
    end=$(date +%s)
    printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
        "$ARM" "$BATCH" "$rep" "$start" "$end" "$code" "$LADDER" "$MAX_DOCS" \
        "$QUEUE_DEPTH" "$csv" >> "$WINDOWS"
    grep -E "docs in" "$log" | tail -8
done
SCRIPT
chmod +x ~/run-os-arm.sh'

ssh fts-harness 'REPS=3 BATCH=512 LADDER=4,4,8,16,32        MAX_DOCS=400000  QUEUE_DEPTH=2 ~/run-os-arm.sh os-conc-low'
ssh fts-harness 'REPS=3 BATCH=512 LADDER=32,32,64,128,256,512 MAX_DOCS=1250000 QUEUE_DEPTH=2 ~/run-os-arm.sh os-conc-high'
```

### Arm 2 — one series per batch size, which is the point of Part B

**A batch level is a whole ladder, not a point.** The knee moves with batch
size, so one fixed concurrency would compare each level at a concurrency that
suits only one of them — and a single point per batch could not be a line on the
final chart. Every batch level therefore runs the full shared grid.

Every batch level runs **the same shared grid** — that is what makes them
series on one chart — in the same two overlapping sub-sweeps, with the budget
scaled to the level's expected rate so no point falls under 5 s:

```bash
ssh fts-harness 'for b in 1 16 64 128 256 512 1024; do
    case $b in
      1)        LOW=100000;  HIGH=300000  ;;   # ~1 doc per request: slow
      16|64)    LOW=200000;  HIGH=800000  ;;
      *)        LOW=400000;  HIGH=1250000 ;;
    esac
    REPS=3 BATCH=$b QUEUE_DEPTH=2 LADDER=4,4,8,16,32 \
        MAX_DOCS=$LOW  ~/run-os-arm.sh os-batch-low
    REPS=3 BATCH=$b QUEUE_DEPTH=2 LADDER=32,32,64,128,256,512 \
        MAX_DOCS=$HIGH ~/run-os-arm.sh os-batch-high
done'
```

- **`batch=1` is not optional.** It is what makes the curve mean anything: the
  per-request framing cost with bulking switched off, and the only level whose x
  axis is the same shape as Part A's.
- **`--max-docs` cannot be one number across the batch levels.** The rate
  range is large enough that a budget which gives `batch=1024` six seconds gives
  `batch=1` several minutes. The budgets above are a starting estimate — size
  them from the first rep's observed rate and re-run any level whose points came
  in short. `max_docs` is a column in `os-windows.tsv` so a short point cannot
  hide, and `plot_harness_grid.py` names every point under 3 s in the chart
  footer.
- **Do not tailor the ladder per batch level.** A taller ladder for the small
  batches would reach their knee, but it also takes them off the shared x grid
  and the grid chart can no longer be drawn. If the small batches turn out to be
  still rising at `c=512`, that is a result to state — "unresolved above 512" —
  not a reason to give them their own x values.

### Arm 3 — N processes, if the sink pegs

Identical in purpose to Part A's N-process arm and needed for the same reason:
one sink is one core. Give each `osrate` process its own port (9200+i) and
aggregate over the **contended window only**, never by summing per-process
averages.

## B5 — collect

```bash
scp 'fts-harness:/mnt/nvme/work/results-os/*'  $R/opensearch/points/
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

# no failed inserts (col 3) and no rejected bulks (col 10) anywhere.
# A 429 in the first failure is queue rejection, not saturation.
awk -F, '!/^#/ && $1!="concurrency" && ($3+0>0 || $10+0>0) \
         {print FILENAME": errors="$3" failed_bulks="$10}' $R/opensearch/points/*.csv

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
   the sibling sinks in `system.peers` is the obvious route. **Not yet done; it
   is a change to the frozen harness and needs a decision, not a drive-by
   patch.** Until then the single-process figure is a lower bound and must be
   written as `≥`.

4. **Do not skip `c=4` and `c=8`.** They are cheap, they are what the crate's own
   usage line documents, and they are the only levels where the sink is far from
   its ceiling. The 2026-09-10 pass started at 16 and lost them for ~5 minutes
   of saved runtime.

5. **N=3 does not converge the rising limb.** Over three reps the `c=64` point
   read 139.9k → 172.4k → 208.0k, climbing monotonically, with both boxes drawing
   more CPU as the session went on. One warm-up row per ladder is not enough for
   the low levels. Either run more reps, or re-run one arm late in the session
   and compare, and report the rising limb as unconverged if it moves.

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
    resident.** Default depth 10 at `c=384 batch=512` is ~7.8 GB on this corpus.
    61 GiB and **no swap** means an overshoot is an OOM kill. Run Part B at
    `--queue-depth 2`, hold it there, and check the product before each arm.

13. **One concurrency cannot serve every batch level.** The knee moves with
    batch size, so a fixed `c` compares each level at a concurrency that suits
    only one of them. Ladder every batch level.

14. **`--max-docs` cannot serve every batch level either.** The rate range
    across `batch=1 … 1024` is large enough that one budget makes either the
    top level 1 s or the bottom level minutes. Size it per level and record it.

17. **Batch size is a series, never an axis, and the concurrency grid is
    shared.** Tailoring a ladder to one batch level takes it off the shared x
    values and the campaign's grid chart can no longer be drawn. A level still
    rising at `c=512` is reported as "unresolved above 512".

15. **`unknown` in an `osrate` header against the sink is correct.** The sink
    does not answer `GET /<index>/_settings`; the crate degrades per field
    rather than failing. Do not chase it, and do not repoint the run at a real
    OpenSearch to make it go away. The analyzer probe is the one check that does
    *not* degrade — it fails the run — which is why Part B passes `--no-reset`.

16. **The HTTP sink saturates earlier than the CQL one at small batches**, since
    it pays a full request parse per document there. Expect `batch=1` to be
    sink-bound and to need the N-sink arm before it can be quoted as anything
    but a lower bound.

---

## Cost

Two `i8g.2xlarge` on-demand in `eu-north-1`.

| | wall |
|---|---|
| bring-up, toolchain, both builds | ~18 min |
| corpus generation (once, serves both parts) | ~12 min |
| Part A measurement | ~15 min |
| Part B: concurrency ladder + 7 batch levels x 3 reps | ~30–40 min |
| collect, verify, stop | ~5 min |
| **both parts, one session** | **~1 h 20 min – 1 h 35 min** |

Standing cost between sessions is the root EBS volumes, billed whether the
instances run or not. The harness root went 8 GiB -> 32 GiB gp3 on
2026-09-11, which adds **~$2/month** (~$0.003/h) — a rounding error against
either box's hourly rate, and it buys back ~34 min of corpus staging on every
restart.

The batch sweep is the expensive half: seven levels, each a ladder, each N=3.
If the session has to be shortened, drop batch levels from the **middle**
(`64`, `256`) and never the ends — `1` is what makes the curve interpretable
and `1024` is what shows whether it has flattened. Cutting the corpus (fewer
documents, or 400 B lines) is the other lever.

## Recorded results

| Run | What it established |
|---|---|
| `results/fleet-rust-harness-null-sink-2026-09-10` | single-process default ≥266,578 docs/s (sink-bound); aggregate ceiling ~378,400 docs/s ≈ 1.49 GB/s at 3,948 B, reached at 2 processes; ~756,700 docs/s at 400 B; cost model 8.2 µs/doc + 2.3 ns/byte. Missing `c=4`/`c=8`. |

# The charts — run these last, after the boxes are stopped

Two of them, and they answer different questions off the same run. **The grid**
is the campaign's deliverable: what one client can offer, per concurrency, for
each harness and batch level. **The growth chart** is Part A only, and says what
one level did while it ran, which the grid's per-level averages cannot.

Nothing here touches AWS, so both happen **after Phase 7**, on the laptop, off
the downloaded artifacts alone. If either cannot be produced from `$R` without
an ssh, something was not collected and Phase 6's gate was skipped.

## The grid — X is concurrency

**X is concurrency, Y is docs/s, and every harness-and-batch combination is a
series on it.**

```
.venv/bin/python3 tools/plot_harness_grid.py \
    --scylla     "$R/scylla/points/default-*-rep*.csv" \
    --opensearch "$R/opensearch/points/os-*-rep*.csv" \
    --output     "$R/harness-grid.png" \
    --table      "$R/harness-grid.csv" \
    --subtitle   "$RUN_ID · i8g.2xlarge · null sink · N=3"
```

Eight series off the default matrix:

| Series | From |
|---|---|
| `scyllarate CQL 1 doc/op` | Part A, `default-low` + `default-high` |
| `osrate batch=1` | Part B, `os-batch-*` at `--batch-size 1` |
| `osrate batch=16` / `=64` / `=128` / `=256` / `=512` / `=1024` | the rest of the batch sweep |

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
- The leading warm-up row of every ladder is dropped.

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

For the *Python* OpenSearch loader the per-process ceiling was **flat in batch
size** — 26.5k–27.7k docs/s across 16/64/128/256/512 — because that client's
cost was per document (`TUNING.md`). This is a different client and the question
is open. Two ways the batch spread can be something other than what it looks
like:

- **It is the sink's.** The HTTP sink scans every bulk body to count actions, so
  it pays a per-request cost that only the small-batch levels pay in full.
  `batch=1` will very likely peg it, which would put the bottom of the fan on
  the instrument rather than on the harness.
- **It is the encode.** A worker builds its NDJSON before it posts and the clock
  starts before the encode, deliberately. Per-request cost therefore scales with
  batch size, and part of the batch effect is the harness's own serialization —
  which is the honest answer to "what does the harness do", not an artifact.
  Say which it is; do not let the reader assume indexing.

## The growth chart — X is the index itself

Part A only: `osrate` writes no per-second series yet, and against the HTTP null
sink there is no index to count.

```
.venv/bin/python3 tools/plot_build_growth.py \
    --samples  "$R/scylla/samples/default-high-rep*/c*.csv" \
    --output   "$R/build-growth.png" \
    --table    "$R/build-growth.csv" \
    --subtitle "$RUN_ID · i8g.2xlarge · null sink · N=3"
```

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

Against the null sink both series move together by construction: the sink counts
a document into its modelled index as it accepts it, so the two lines lie on top
of each other and any gap between them is the harness's own. **That is the point
of running it here** — it is the zero reading the engine campaign's version of
this chart is read against.

## Then write it down

Put `harness-grid.png`, `harness-grid.csv` and `build-growth.png` at the top of
`$R/README.md`, with the sink-bound series named in the caption, and hand the
user the absolute path of `$R`.
