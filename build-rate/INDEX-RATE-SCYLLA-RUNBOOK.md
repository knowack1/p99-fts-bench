# Index-rate runbook — ScyllaDB half (R1, R2, R8)

**Hand this file to Claude Code as the instruction and it runs the ScyllaDB
half of the index-rate campaign**: starts the two AWS boxes, rebuilds and loads
the vector-store image, stages the frozen corpus, measures three arms against a
real ScyllaDB + vector-store stack, pulls every artifact home, stops the boxes.
Every script it needs is inline.

It is one of two runbooks cut from
[`INDEX-RATE-MATRIX-PLAN.md`](INDEX-RATE-MATRIX-PLAN.md), which remains the
campaign's decision record — why each arm exists, what each reading is worth,
and what may not be said off the chart. Nothing here overrides it; where this
file and the plan disagree, the plan is right and this file has drifted.

| Runbook | Arms | Binary | Stack on the SUT |
|---|---|---|---|
| **this file** | R1 `scylla-buf15`, R2 `scylla-buf376`, R8 `scylla-buf376-disk` | `scyllarate` | ScyllaDB + vector-store |
| [`INDEX-RATE-OPENSEARCH-RUNBOOK.md`](INDEX-RATE-OPENSEARCH-RUNBOOK.md) | R4 `os-ramindex-refresh1`, `os-disk-refresh1` | `osrate` | OpenSearch |

**Run this half first.** The plan's execution order is R1, R2, R8, then the
OpenSearch arms, and the reason is that a surprise in R2 can still change the
plan before the other half bills any time.

**With the rate ladder that order applies twice, not once.** Phase A calibrates
every arm in the campaign, then the ladder is chosen, then Phase B measures every
arm — so this half's three arms run first within each pass rather than the whole
half running first. See "The two phases" and Phase 8.

## The one thing to get right before anything else

**Both runbooks must write into the same `$R`.** The primary chart draws four
arms across both halves and `--series` names each line by the directory its
rows came from, so two run directories means two half-charts and no
cross-engine read at all. If the OpenSearch half has already run, **reuse its
`RUN_ID`** rather than minting a new one — see "The results directory".

**If the two halves run in different fleet sessions, that is a provenance
difference and it reaches the footer.** Every stop wipes `/mnt/nvme` and forces
a full re-entry, so the split between these two runbooks is exactly the "split
at an arm boundary" the plan permits. Record which arms were measured in which
session, in `$R/env/`, at the time — not from memory afterwards.

## Blocker on the critical path — the probe cannot slice a rate ladder

**`ftsbench/probe_windows.py` is out of scope for the axis change and is now
broken for every rate-laddered run.** It keys a resource window on
`(sweep, concurrency, rep)` — `LEVEL_RE = r"\[\d+/\d+\] concurrency=(\d+)"`
(`ftsbench/probe_windows.py:67`). On a concurrency ladder that key is unique per
rung. On a rate ladder concurrency is a **constant in-flight cap across every
rung**, so all seven rungs of a rep produce the same key, and the duplicate
check exits 1 (`ftsbench/probe_windows.py:357-361`).

**This is a loud failure, not a silent mis-slice**, which is the one good thing
about it. What it costs is everything downstream of the slicer:

| Blocked for Phase B | Consequence |
|---|---|
| `resource-by-rung.csv` | does not exist, so **the RSS-breach gate — the one blocking gate that voids an arm — has no per-rung input** |
| `probe/cpu-*.jsonl` rung slices | 21 per arm, none of them produced |
| `ftsbench/verify_cpu_usage.py` | reads the slices, so the CPU-attribution gate is blocked with them |

Phase A is unaffected: it *is* a concurrency ladder and slices normally.

**The fix is one line in that out-of-scope file.** The harness's level
announcement now reads `[i/N] concurrency=512 target_docs_per_s=20000`
(`core/src/sweep.rs`, `announce_level`), so the rate is already on the line and
the key has only to include it. **Until that lands, do not start the fleet for
Phase B** — an arm measured without its resource table is an arm whose blocking
gate was never run, and the stack that could have re-run it is gone by the time
anyone notices. The raw `$R/sut/cpu-<arm>.jsonl` still comes home and still
gives an **arm-wide** peak, but an arm-wide peak cannot say which rung breached
and the CPU verdict this campaign wrote down is per rung.

## What this measures, and what it is not

`index_docs_per_s` is searchable documents divided by the **whole** build wall:
from the first insert to the moment the vector-store's searchable count reaches
what was sent, or stops moving, or runs out of settle budget
(`core/src/build_rate.rs`, `Level::summarize`). A write ack is not a document
in the index, and this axis is the one that refuses to pretend otherwise.

On this half the index is the vector-store's Tantivy build, fed through CDC,
publishing at every commit. The count is read off
`GET /api/v1/indexes/{ks}/{index}/status` on port **16080**. That endpoint
reports **one** count, the searchable one (`scylla/src/vstore.rs`:
`accepted: None`), so the harness's idle rule falls back to it — which is fine
at a 1 s commit and is why no arm here overrides a settle timeout.

**No number from this runbook is a cross-engine number on its own.** The only
cross-engine read the campaign has is R2 ↔ R4, and R4 is in the other runbook.
Every image carries PRELIMINARY until the pass that produced it is written up.

## The three arms

X is the **offered rate in documents per second** on the shared grid — a rate
the client was told to produce, not a client knob whose meaning differs per
engine. Every arm is drawn **twice**: solid with a filled marker for
`docs_per_s`, dashed with a hollow one for `index_docs_per_s`, in the same
colour, against a dotted `y = x` diagonal. Where a line leaves the diagonal is
the reading.

Each arm is measured in two phases. **Phase A is a concurrency ladder at N=1
and is never published** — it exists to find each arm's ceiling, which is what
places Phase B's rates. **Phase B is the rate ladder at N=3 and is the
measurement.** See "The two phases".

| # | Arm (`--series` label) | Engine knobs vs. the row above | Phase B reps | Phase B points |
|---|---|---|---|---|
| **R1** | `scylla-buf15` | `VS_FTS_COMMIT_THRESHOLD=0`; `VS_FTS_WRITER_MEMORY_MB` **unset** → tantivy's 15 MB/thread floor; **`VS_FTS_COMMIT_INTERVAL=1s`**; `VS_FTS_METRICS_INTERVAL=1s` | 3 | 21 |
| **R2** | `scylla-buf376` | **+ `VS_FTS_WRITER_MEMORY_MB=376`** — writer-budget parity with OpenSearch's 1.4 GiB node total | 3 | 21 |
| **R8** | `scylla-buf376-disk` | R2's knobs **+ `VS_FTS_INDEX_DIR=/var/lib/vector-store/fts`** — the Tantivy index on the NVMe instead of in RAM | 3 | 21 |

Three arms, 63 Phase B points at a 7-rung ladder, six lines — plus three Phase A
ladders that never reach a chart. The readings this half is built to give:

| Read | Arms | What it is |
|---|---|---|
| Writer buffer | R1 → R2 | the ~42% delta `../BUILD-RATE-LOOP.md` measured at 1.42x on the submitted axis, re-measured on the indexed one |
| Where the index lives | R2 → R8 | the RAM index against the same build file-backed on the NVMe |
| Cross-engine | R2 ↔ R4 | **needs the other runbook; it is cadence-matched at 1 s** — see "The cadence is 1 s on both halves, and R2 ↔ R4 is matched". It is now read at one **offered rate** rather than at one concurrency, which is what makes the two sides comparable at all. Nothing here can produce it |

**The numbering has gaps and they are deliberate.** R3 (`scylla-buf376-commit30`)
was an arm of this table and was dropped when the campaign stopped measuring a
30 s cadence. The identifier is **not** reused — an `r3/` directory refers to an
arm that was never measured.

**Framing guard, mandatory.** R1 → R2 is a 42% tuning delta on our own side.
That makes **"they have to tune, we don't" unsayable** from this chart, and the
footer says so in those words. The tuning does not disappear on the CQL path,
it moves — from the client's bulk size to the index's commit cadence and writer
budget.

**There is no ScyllaDB batch series and there cannot be one.** `scyllarate` has
no batch flag. One row is one prepared statement and `--concurrency` is exactly
the number of INSERTs in flight. Batch 1 is not a setting here, it is the write
path.

**That is exactly what took concurrency off the x axis.** At `--batch-size 1024`
one `osrate` request carries 1,024 documents and one `scyllarate` request
carries 1, so the same x value was two different offers and R2 ↔ R4 — *the
campaign's only cross-engine read* — compared two different things per x value.
A document per second means the same thing on both halves. **The asymmetry did
not go away; it stopped being the axis and became a recorded column**,
`in_flight_peak`, read against the `--concurrency` cap. See "The in-flight cap,
and why it differs per engine".

## The fleet

| Alias | Instance | Role |
|---|---|---|
| `fts-harness` | `i-08d8d2505e16683f7` · `k-nowacki-fts-benchmark-harness` | runs `scyllarate`, holds the corpus, builds the vector-store image |
| `fts-sut` | `i-0e3e4b6b02e654b7f` · `k-nowacki-fts-benchmark-sut` | runs ScyllaDB + vector-store |

Both `i8g.2xlarge` (8 vCPU Graviton4, 61 GiB, aarch64), `eu-north-1b`, Amazon
Linux 2023, user `ec2-user`, key `~/.ssh/KarolNowackiAws.pem`. Private RTT
0.353 ms, measured.

There is no AWS CLI credential on this laptop — the console in Chrome is the
only way to start and stop the boxes.

## Phase 0 — the results directory, on the laptop

Everything on the fleet is ephemeral: `/mnt/nvme` is destroyed on every stop,
so **the laptop is the only place results survive**. Create the directory
before touching a single instance.

```bash
# Run this ONCE, at the very start of the session, and keep the shell.
# If the OpenSearch half has already run, REUSE its RUN_ID here instead of
# minting a new one -- the primary chart needs all four arms under one $R.
export RUN_ID="index-rate-matrix-$(date -u +%Y-%m-%dT%H%MZ)"
export R="$HOME/Projects/Scylla/p99/bench/results/$RUN_ID"
for arm in r1 r2 r8; do mkdir -p "$R/$arm"/scylla/{points,calibration,samples,logs,probe}; done
mkdir -p "$R"/{env,scripts,sut}
printf '%s\n' "$RUN_ID" > "$R/RUN_ID"
printf 'scylla half: %s\n' "$(date -u +%FT%TZ)" >> "$R/env/sessions.txt"
echo "results -> $R"
```

The arm directory name **is** the chart's series set: an arm written to the
wrong directory becomes a mislabelled line rather than a missing one.

**`calibration/` is beside `points/` and never inside it.** Phase A writes
concurrency-ladder CSVs, the renderer globs `points/*.csv`, and
`rate_vs_offered.py` **refuses a concurrency-ladder CSV by name** — so a
calibration file in `points/` fails the render outright rather than drawing a
line. That refusal is the safety net, not the plan.

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
cannot stop the boxes from here, and they bill until someone else does. At
~2.9 h this half is long enough for that to happen.

## Phase 2 — fleet re-entry

Every stop wipes the instance store, so this runs on **every** start.

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

**The SUT's private IP is hard-coded in `docker/.env.sut`** as
`SCYLLA_BROADCAST_RPC` and `SCYLLA_VS_URI`. A changed private IP breaks off-box
CQL and BM25 routing at once, and editing `.env.sut` is then part of re-entry.

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

### 2d. The vector-store image — the critical path of this half

`daemon.json` points docker's `data-root` at the instance store, so every image
goes with the stop. The two public images re-pull; the vector-store is in no
registry and must be rebuilt from **`94a23ef2`**, the commit that carries
`VECTOR_STORE_FTS_INDEX_DIR`. ~15 min of billed fleet time, on the critical
path of every start.

**Run this in a shell where `tools/fleet_env.sh` has NOT been sourced.**
`run-with-release-toolchain` needs the docker daemon *local*; with
`DOCKER_HOST=ssh://<sut>` set it sends both the build and the resulting image
to the SUT's daemon, where `docker save` on the harness will not find it.

```bash
ssh fts-harness 'set -e
  mkdir -p /mnt/nvme/build && cd /mnt/nvme/build
  rm -rf vector-store
  git clone -b p99-fts-ingest-optimization \
      https://github.com/knowack1/vector-store.git
  cd vector-store
  # The fork carries NO tags, and both build scripts derive the version from
  # `git describe`, which fails outright without an annotated tag -- so the
  # 1.10.0 tag is fetched from upstream before anything is built.
  git fetch --tags https://github.com/scylladb/vector-store.git
  git rev-parse HEAD                  # expect 94a23ef2c9ff...
  git describe --dirty                # expect 1.10.0-45-g94a23ef2, NO -dirty
  TARGETARCH=arm64 ./scripts/run-with-release-toolchain cargo build --release
  ./scripts/build-dockers arm64'

# Run from the harness.
ssh fts-harness 'docker save scylladb/vector-store:1.10.0-45-g94a23ef2-arm64' \
    | ssh fts-sut docker load

# Read the tag back off the SUT rather than trusting the load.
ssh fts-sut docker images scylladb/vector-store
```

The image the compose file asks for is exactly the string in
`docker/.env.sut`'s `VECTOR_STORE_IMAGE`. A mismatch is a `pull access denied`
at `scylla-up` — the **good** failure. The bad one is an *older* image carrying
the same tag, which is why every arm's real gate stays the vector-store's own
`index=…` startup line. **An image built before `94a23ef2` ignores
`VS_FTS_INDEX_DIR` in silence**, which is the exact failure that already cost
S11–S15 once.

A dirty tree is refused by `build-release` and would tag the image `-dirty`
anyway, which is why the fresh clone is the point rather than a nicety. The
clone is on `/mnt/nvme` and goes with the next stop; the fork branch is the
durable copy.

### 2e. The bench checkout and the venv on the harness

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

`scyllarate` reads the corpus locally through `--corpus`; nothing streams it
and the SUT never sees a line of it. No corpus, no arm.

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

**~1.5–2 min**, bounded by gp3's 125 MB/s baseline read, not by `pzstd`. The
sha256 is `../FREEZE.md`'s and is what proves the bytes are the frozen corpus
rather than a re-download that drifted. **Check it every time.**

**If the archive is not on the root volume** — a replaced box, a rebuilt
volume — re-stage from the Swedish Wikimedia mirror (~36 min at ~215 MB/s),
run `prepare_corpus`, verify against `../FREEZE.md`, then `pzstd -10` the
result back to `~/corpus.jsonl.zst` so the next start is 2 minutes instead of
36. Not from the laptop (which does not hold enwiki) and not from S3 (the
harness has no instance profile; `DeveloperAccessRole` cannot `iam:CreatePolicy`).
Budget the 36 min into the session if the archive's presence has not been
confirmed.

**Do not use `HARNESS-AWS-RUNBOOK.md` Phase 4 here.** That runbook *generates*
a synthetic corpus at enwiki's mean line length, which is right for a null-sink
run where no document is ever indexed and wrong for every arm here: BM25 term
statistics, segment merges and the analyzer all depend on real text.

## Phase 4 — build the harness, then freeze it

```bash
ssh fts-harness 'cd /mnt/nvme/work/bench/build-rate/scylla && . "$HOME/.cargo/env" \
  && CARGO_TARGET_DIR=/mnt/nvme/work/target cargo build --release --locked'
```

If the Rust toolchain went with the stop:

```bash
ssh fts-harness 'curl --proto "=https" --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --profile minimal && . "$HOME/.cargo/env" && rustc --version'
```

Record, into `$R/env/`, **before** measuring:

```bash
ssh fts-harness 'cd /mnt/nvme/work/bench/build-rate && find . -type f \
  \( -name "*.rs" -o -name "Cargo.*" \) | sort | xargs sha256sum | sha256sum' \
  > "$R/env/harness-tree.sha256"
git -C ~/Projects/Scylla/p99/bench log -1 --format='%H %s' > "$R/env/bench-commit.txt"
git -C ~/Projects/Scylla/p99/bench status --short build-rate >> "$R/env/bench-commit.txt"
```

**Do not rebuild once an arm has run.** A rebuild mid-campaign makes the arms
incomparable. Freeze the binary, record what it was built from, and note any
divergence from the current tree in the write-up.

### The sweep script

```bash
ssh fts-harness 'cat > ~/run-arm.sh << "SCRIPT"
#!/bin/bash
# One arm sweep: the same ladder, N times, against the SUT.
#
# stderr is timestamped per line. The tool announces each level as it starts
# it, so the log carries the exact wall-clock window of every point and the
# resource probe on the SUT can be cut to that window rather than the whole
# sweep. That log is the only wall clock the harness produces.
set -u
ARM="$1"; shift
REPS="${REPS:-3}"
LADDER="${LADDER:-32,64,128}"
RATES="${RATES:-}"
CAP="${CAP:-512}"
CORPUS="${CORPUS:-/mnt/nvme/work/corpus.jsonl}"
MAX_DOCS="${MAX_DOCS:-1250000}"
SINK="${SINK:-172.31.47.166}"
PORT="${PORT:-9042}"
VS_PORT="${VS_PORT:-$((PORT + 7000))}"
OUT_DIR="${OUT_DIR:-/mnt/nvme/work/results}"
SAMPLES_DIR="${SAMPLES_DIR:-/mnt/nvme/work/samples}"
BIN=/mnt/nvme/work/target/release/scyllarate

if [ -n "$RATES" ]; then
    AXIS=(--target-rate "$RATES" --concurrency "$CAP")
    AXIS_LABEL="rate:$RATES@c$CAP"
else
    AXIS=(--concurrency "$LADDER")
    AXIS_LABEL="conc:$LADDER"
fi

mkdir -p "$OUT_DIR" "$SAMPLES_DIR"
WINDOWS="$OUT_DIR/run-windows.tsv"
[ -f "$WINDOWS" ] || printf "arm\trep\tstart_epoch\tend_epoch\texit_code\taxis\tmax_docs\tcsv\n" > "$WINDOWS"
stamp() { awk "{ printf \"%d\t%s\n\", systime(), \$0; fflush() }"; }

for rep in $(seq 1 "$REPS"); do
    csv="$OUT_DIR/$ARM-rep$rep.csv"
    log="$OUT_DIR/$ARM-rep$rep.stderr.tsv"
    echo "######## arm=$ARM rep=$rep axis=$AXIS_LABEL $(date -u +%H:%M:%S)"
    start=$(date +%s)
    "$BIN" --corpus "$CORPUS" "${AXIS[@]}" --max-docs "$MAX_DOCS" \
           --hosts "$SINK" --port "$PORT" \
           --vs-url "http://$SINK:$VS_PORT" \
           --out "$csv" --samples-dir "$SAMPLES_DIR/$ARM-rep$rep" \
           "$@" 2>&1 | stamp > "$log"
    code=${PIPESTATUS[0]}
    printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
        "$ARM" "$rep" "$start" "$(date +%s)" "$code" "$AXIS_LABEL" "$MAX_DOCS" "$csv" >> "$WINDOWS"
    grep -E "docs in" "$log" | tail -12
done
SCRIPT
chmod +x ~/run-arm.sh'
```

**`RATES` and `LADDER` are the two axes and exactly one of them may be a list.**
`scyllarate` refuses `--target-rate` and a multi-valued `--concurrency` together
**at parse time, before it connects to anything** (`core/src/sweep.rs`), with:

```
--target-rate makes the offered rate the ladder, so --concurrency must be a
single in-flight cap, not N levels
```

Exit code 1, no reset, no documents, no billed level. That is the cheap failure
and it is worth provoking once during the smoke rather than discovering that the
script quoted a list into `CAP`.

**`VS_PORT=16080` is not optional and is the one line that makes these arms
reach an engine at all.** The script derives it as `$((PORT + 7000))` → 16042,
which is the **null sink's** convention and right for the campaign the script
was written for. Here the index is answered by a real vector-store on **16080**
(`docker/.env.sut`'s `VS_HOST_PORT`, the same port `SCYLLA_VS_URI` uses).
Without the override `scyllarate` polls a closed port and **every arm dies at
its reset gate, on billed fleet time, before a single document is inserted.**
It is passed on **both phases** of all three arms. The `+7000` default is left
in place rather than patched out: `HARNESS-AWS-RUNBOOK.md`'s own campaign
depends on it. Found by the execution-step-1 rehearsal, 2026-09-15.

`CORPUS` also defaults to the *synthetic* runbook corpus, so every run line
here overrides it to `/mnt/nvme/data/corpus.jsonl`.

### The in-flight cap, and why it differs per engine

Under a rate ladder `--concurrency` is no longer an axis. It is a **single
in-flight cap**: a ceiling on outstanding requests, there so that a rung whose
engine has stopped keeping up cannot run the harness out of memory instead of
reporting saturation.

**`--concurrency 512` on `scyllarate`, `--concurrency 128` on `osrate`.** The
cap binds by Little's Law — `in_flight = requests/s × latency` — and a request
is one document on this side and 1,024 on the other, so the same offered rate
asks for ~1,024x more requests here. That is measured, not assumed: against
`../engine-mock` at one identical offered rate of 50,000 docs/s, `in_flight_peak`
came out at **402** on the `scyllarate` side and **2** on the `osrate` side.

**A shared cap would be the axis asymmetry in a new place.** A cap of 8 would
bind hard on every ScyllaDB rung and never bind on an OpenSearch one, and the
ScyllaDB curve would then be a picture of the cap. Each half gets the cap its
request shape needs, and `in_flight_peak` in the CSV is what proves the cap did
not bind — see the gate table.

`../engine-mock` is a mock: those two numbers place the caps, and **neither is
an engine number**. A real engine has higher latency than a mock, so the real
`in_flight_peak` will be higher, which is exactly why the gate reads it per rung
rather than trusting the derivation.

## Phase 5 — the two phases, the document bound, and the smoke

### The two phases

| Phase | Ladder | N | Published? | What it is for |
|---|---|---|---|---|
| **A — calibration** | `--concurrency 4,8,16,32,64,128` | 1 | **never** | each arm's ceiling and `c_sat`. Closed loop is the correct instrument for *how fast can it go*, and this is the one thing it is still used for |
| **B — the measurement** | `--target-rate <~7 rungs>` with `--concurrency 512` as a cap | 3 | **yes** | the campaign's chart |

**Phase A is not a warm-up and not a smoke.** It is the only thing that can
place Phase B's rates, and it is thrown away afterwards because a closed-loop
curve on the published axis is the defect the axis change exists to remove.
Its CSVs go to `calibration/`, never to `points/`.

**Phase B's rates come out of Phase A and cannot be written down yet.**

    C_max  = the highest docs_per_s any arm reached in Phase A
    floor  = 0.12 x C_max
    top    = 1.20 x C_max
    rungs  = 7, uniformly spaced from floor to top (step = 0.18 x C_max)

**`C_max` is the FASTEST arm's ceiling, not each arm's own.** Every arm has to
be drawn on the same x grid or the chart has no cross-arm reading at all, and a
grid topping out below the fastest arm's knee leaves that arm's knee
unmeasured — which is the one thing this chart exists to show. The slow arms
saturate on the upper rungs instead, and a saturated rung is drawn and kept, not
dropped. That is the trade, taken deliberately.

**Round the seven rates to something a caption can carry** (a 1,000 docs/s grid
is fine) and keep them uniform after rounding; the ladder is a list, so the
harness does not care, but a reader reading a knee off the x axis does.

**These are placeholders until Phase A has run on the fleet. Nothing on this
page is a measured rate.** Record the seven chosen rates, and the `C_max` they
came from, in `$R/env/rate-ladder.txt` at the time they are chosen.

### The document bound — `--max-docs 3500000`, campaign-wide

One constant, for every arm and every rung of both phases. Two reasons, both
load-bearing:

- **Every rung then ingests the identical documents and only the rate differs.**
  That is the campaign's "every document exactly once, same set per engine"
  invariant, held rung to rung as well as arm to arm. A time bound would give
  every rung a different subset and make a rate difference and a corpus
  difference inseparable.
- **It must stay under R4's ramindex ceiling of 4,025,699 documents** (12 GiB of
  tmpfs, the other runbook). The enwiki corpus is 8,967,625 documents
  (`../FREEZE.md:55`), so a literal whole-corpus rung would kill R4 — half of
  the campaign's only cross-engine read.

**3,500,000 clears that ceiling by 525,699 documents, 13%.** The bound was
4,000,000 until 2026-09-16, which cleared it by 0.64% — thinner than the
uncertainty in the ceiling itself, which is a byte budget divided by a *mean*
document size rather than a counted limit. A corpus whose mean document ran 1%
large would have put R4 into ENOSPC mid-rung, on billed fleet time, on the arm
that has no sibling to corroborate against. 13% is cheap insurance: it costs
~12% of every rung's load time and nothing else, and 3,500,000 documents is
still 39% of the corpus at every rung.

**The OpenSearch runbook's pre-flight rung still has to clear it**, and if it
does not, the bound moves **for every arm in both runbooks** — a point's
`--max-docs` is how much of the corpus that rung pushed, and two halves on
different bounds are not comparable.

**`--duration` does not exist.** Do not reach for it.

### What a rung costs, and where to add one

A rung's load time is `max_docs / rate`, so the cost of the ladder is
front-loaded onto its slowest rung. At an illustrative 7-rung ladder spaced
20,000 docs/s apart:

| Offered rate | Load time at 3,500,000 documents |
|---|---|
| 20,000 | **175 s** |
| 40,000 | 88 s |
| 60,000 | 58 s |
| 80,000 | 44 s |
| 100,000 | 35 s |
| 120,000 | 29 s |
| 140,000 | **25 s** |

**~454 s a rep, and the floor rung alone is ~39% of it.** So **adding rungs at
the top, where the knee is, is nearly free, and the floor rung is the expensive
one.** If the ladder has to be cut, cut it at the bottom and say so; do not thin
the top out to save time it was not costing.

**`max_docs / rate` is a floor, not the wall.** A rung the arm cannot keep up
with costs `max_docs / achieved` instead, and by construction the top rung is
above `C_max` and will be one of those. Reset and settle sit on top of every
rung either way.

**The loader's read-ahead is dormant on this half.** The channel is bounded in
batches and `scyllarate`'s batch is one document, so even at the `c=512` cap it
holds 5,120 documents at the default depth — a few MB on a 61 GiB box. The
~8 GB guard the OpenSearch half has to check before every arm does not apply
here.

**`--keep-warmup` on every chart command**: the ladder carries no throwaway
rung, so the floor rate is a measured point and the renderer's default first-row
drop would delete it.

### What the old grid section carried, and what became of it

A reader who worked from the previous version of this runbook knew a set of
numbers that are now **retired**, and they are named here rather than quietly
deleted:

| Retired | Why it is gone |
|---|---|
| the `4 8 16 32 64 128` grid as the **published** axis | it is Phase A only now; concurrency is not a shared unit between the halves |
| the **600,000 / 2,621,440** split | one `--max-docs` for the whole campaign, so there is no split to have |
| the ≥20-requests-per-worker floor | a closed-loop artifact: it existed because a level had to carry enough `_bulk` requests to have workers. A paced rung is bounded by documents and a rate |
| the two disjoint sub-sweeps | one ladder per arm per phase |
| the `16`→`32` budget seam, and the `c=32` overlap check that used to guard it | there is one budget, so there is no seam. **This is the one retirement that removes a stated limit rather than adding one** |
| "if a budget moves it moves for every arm in that sweep" | still true, but it is now campaign-wide: if `--max-docs` moves it moves for every arm in **both runbooks** |

### The settle timeouts — every arm runs the defaults

No arm overrides a settle timeout, and at a 1 s commit that has more margin
than it did at 3 s, not less. `scyllarate`'s default `--vs-idle-timeout 10` is
ten cadences, so a build in the gap between two commits is never mistaken for a
stalled one, and the default `--vs-settle-timeout 120` is a hundred and twenty.
**An unsettled ScyllaDB point at a 1 s commit is therefore a finding, and a
starker one than the 3 s cadence would have produced** — an arm that cannot
settle inside 120 cadences is not waiting on the cadence. The timeouts in
force are recorded in every CSV header (`vs_settle_timeout_s`).

The four-cadences rule is not the binding constraint on either phase. The
shortest rung the ladder plans for is the top one at ~29 s of load, which is
~29 intervals at a 1 s commit.

### The latency columns change meaning under pacing — read the header

**The rate ladder redefines `p50_ms` and `p99_ms` rather than adding a column.**
Under pacing they run from the moment a request was **due**, not from the moment
it was sent, which is what makes a coordinated-omission claim a number instead of
a promise. The CSV header says which:

| Header fact | On a rate ladder | On a concurrency ladder |
|---|---|---|
| `latency_basis` | `intended_start` | `service` |
| `target_rate_docs_per_s` | the rate list | `off` |

**Two CSVs that look compatible must not be pooled across that line.** Phase A's
latencies and Phase B's are different measurements wearing the same column
names, which is the other reason `calibration/` is not `points/`.

### Smoke — 2 rungs × 3 arms at a 20k cap, on both axes

Run the full Phase 6 shape for each arm into a throwaway `OUT_DIR`, twice:
`LADDER=4,8 MAX_DOCS=20000 REPS=1` for the Phase A shape, then
`RATES=5000,10000 CAP=512 MAX_DOCS=20000 REPS=1` for the Phase B shape. Gate on:

- every point complete, no failed inserts;
- every arm's vector-store startup lines match the arm (§ 6b);
- **every arm's tuning line says `commit_interval=1s`** — the knob is dropped
  silently when misspelled, so the smoke is where a 3 s arm wearing a 1 s label
  is caught, not the ladder;
- **R8's line says `index=disk:/var/lib/vector-store/fts`**;
- every ScyllaDB arm settles on `scyllarate`'s default timeouts;
- **`verify_arm` on R1 and R2 reports its one expected `commit_interval`
  mismatch and nothing else** (§ 6e) — the smoke is also where that triage is
  rehearsed, so a second mismatch is not first seen at an arm close-out;
- **the rate run's header reads `latency_basis=intended_start` and its rows
  carry all five of `target_docs_per_s`, `achieved_offered_ratio`,
  `queue_p99_ms`, `in_flight_peak`, `generator_saturated`** — populated, not
  blank. Blank is what a concurrency-ladder row writes, so blanks here mean the
  run line lost its `RATES`;
- **the concurrency run's rows leave those five blank** — blank, never zero;
- **the level line reads `[i/N] concurrency=512 target_docs_per_s=<rate>`** on
  the rate run. That line is the probe join's only key, and it is the line the
  `probe_windows` fix has to learn to read;
- **one deliberate `RATES=5000,10000 LADDER=4,8` run, to see it refused at parse
  time** with exit 1 and the `--target-rate makes the offered rate the ladder`
  message, before anything connects;
- **the probe's own smoke**, which is where a wrong container name costs two
  minutes instead of an arm: the probe is running and its file is growing,
  every sample reads `source=cgroup-anon`, `probe_windows` finds one window per
  rung per rep with no `empty` note on the **Phase A** run, and the recorded
  clock skew is under 1 s. **On the Phase B run `probe_windows` exits 1 on
  duplicate keys until the fix lands** — see "Blocker on the critical path". The
  smoke is where that is confirmed to be the known failure and not a new one.

Delete the smoke output before the real ladders. It is not campaign data.

### Bound check — one Phase B floor rung

The `--max-docs 3500000` bound is decided and is not re-litigated on the fleet;
its one binding confirmation is R4's tmpfs and it belongs to the other runbook.
What this half runs, after Phase A has produced its rates, is **one Phase B
floor rung on R2** — the slowest rung in the campaign and the one that sets the
session's length. It confirms the rung's wall against `3,500,000 / floor_rate`
and that `achieved_offered_ratio` at the floor is ~1.0; a floor rung that is
already saturated means `C_max` was read off the wrong arm and the whole ladder
has to be replaced before any arm is measured.

## Phase 6 — the ladders, one arm at a time

Order: **R1, R2, R8**. The stack is recreated between arms — `*-down` then
`*-up`, **never restarted in place**. The vector-store's RAM index is rebuilt
on start, and an image that ignored a knob looks identical to one that honoured
it.

Each arm is: `scylla-up` → **confirm the startup lines** → **start the probe** →
**Phase A** → **Phase B** → **close the arm out** (probe stop, slice, CPU
verdict, all on the harness while the stack is live) → `pull_arm` →
`scylla-down`. **The arm is not finished until its `pull_arm` returns zero.**

**Phase A runs on every arm in the campaign before Phase B runs on any of
them.** `C_max` is the highest ceiling across **all five arms**, including the
other runbook's two, and every arm has to land on the same x grid or there is no
cross-arm reading. So the session is two passes over the arm order — a
calibration pass and a measurement pass — with the stack torn down and recreated
for each arm in each pass, exactly as it is between arms. **Six stack cycles on
this half instead of three**, and an ordering dependency between the two
runbooks that the concurrency grid did not have: neither half can start Phase B
until both halves have finished Phase A.

**This is the cost of one shared x grid and it is paid deliberately.** The
alternative — each arm on its own grid, chosen from its own ceiling — draws
arms that cannot be read against each other, which is the whole chart.

### 6a. Bring the stack up with the arm's knobs

`source tools/fleet_env.sh` on the harness sets `DOCKER_HOST=ssh://<sut>` and
`COMPOSE_ENV=docker/.env.sut`, so every `docker compose` call reads the compose
files and env **locally** and creates the containers **on the SUT**.

| Arm | On top of `docker/.env.sut` |
|---|---|
| R1 | `VS_FTS_WRITER_MEMORY_MB=` (unset → 15 MB/thread floor), `VS_FTS_COMMIT_THRESHOLD=0`, **`VS_FTS_COMMIT_INTERVAL=1s`**, `VS_FTS_METRICS_INTERVAL=1s` |
| R2 | `VS_FTS_WRITER_MEMORY_MB=376`, otherwise as R1 |
| R8 | R2 + `VS_FTS_INDEX_DIR=/var/lib/vector-store/fts` |

Knobs are passed through compose's `${VAR:+=${VAR}}` form, so an unset knob is
dropped rather than passed empty.

```bash
ssh fts-harness 'set -eu
  cd $BENCH && . tools/fleet_env.sh
  make scylla-up && make scylla-wait'
```

### The cadence is 1 s on both halves, and R2 ↔ R4 is matched

**`VS_FTS_COMMIT_INTERVAL=1s` is set explicitly on every arm**, overriding the
vector-store's 3 s default. It is not a variable in this campaign — it is
pinned at one value, so **cadence cost is unmeasured and no chart off this
campaign may claim it.** What changed is which value.

**Both halves moved to 1 s together, and that is what keeps R2 ↔ R4 matched.**
The other runbook passes `--refresh-interval 1s` on every OpenSearch arm. This
file claimed a 3 s OpenSearch cadence until 2026-09-16 and was stale; the
disagreement was settled in `INDEX-RATE-MATRIX-PLAN.md`, which the other runbook
had asked to adjudicate it before the fleet started. **Where this file and the
plan disagree, the plan is right.**

So the campaign's only cross-engine read compares a 1 s cadence against a 1 s
cadence, and no cadence correction is owed on it. What is still owed is the
disclosure that **the cadence is pinned and therefore unmeasured**: nothing on
this campaign separates the cost of a commit or refresh interval from any other
cause, in either direction.

The one asymmetry that survives at matched cadence is the final refresh — the
harness asks OpenSearch to publish and waits for ScyllaDB's own commit — which
is worth at most 1 s against a rung of tens to hundreds of seconds. It is
disclosed under "The final refresh", never netted out.

R1 → R2 and R2 → R8 are unaffected either way: both sides of each of those reads
run the same cadence, and they are the only reads this half can produce alone.

### 6b. The tuning gate — read the log, do not trust the environment

```bash
ssh fts-harness 'docker logs fts-bench-vector-store 2>&1 | grep -E "ingest tuning|tantivy worker"'
```

Two lines, and they are the arm's licence:

```
ingest tuning for ...: commit_interval=1s commit_threshold=disabled ... index=ram
index writer using 4 tantivy worker threads, 376 MB buffer per thread
```

| Arm | Expect |
|---|---|
| R1 | `index=ram`, `15 MB buffer per thread` |
| R2 | `index=ram`, `376 MB buffer per thread` |
| R8 | **`index=disk:/var/lib/vector-store/fts`**, `376 MB buffer per thread` |

**Every arm must also read `commit_interval=1s`.** The knob is passed through
compose's `${VAR:+=${VAR}}` form, so a typo drops it silently and the arm runs
the 3 s default while every label says 1 s. A wrong cadence in this line voids
the arm the same way a wrong buffer does.

`VS_CPUSET=4-7` is what fixes the tantivy worker count at 4
(`available_parallelism` honours the cpuset), so `376 × 4 = 1,504 MB` is the
parity with OpenSearch's 1.4 GiB `indices.memory.index_buffer_size`.

**Do not start the sweeps until these lines match.** An image that ignored a
knob writes a complete, plausible, wrongly-labelled ladder.

### 6c. Start the resource probe

One probe per arm, sampling every container in the stack at 1 Hz. It is the one
component `DOCKER_HOST` cannot carry — it reads `/sys/fs/cgroup` where it runs —
so it goes through `tools/sut_probe.sh`, which starts it detached on the SUT.
**Running it on the harness instead records the generator box's idle cgroups
and reports them as engine numbers**; this repository has paid for that mistake
once (`../BUILD-RATE-LOOP.md`).

**Do not pass `--output`** — the wrapper appends it. The engine URLs are
`127.0.0.1` because the probe is on the SUT.

```bash
a=r2    # r1 | r2 | r8
ssh fts-harness "set -eu
  cd \$BENCH && . tools/fleet_env.sh
  mkdir -p /mnt/nvme/work/probe
  tools/sut_probe.sh start /mnt/nvme/work/probe/$a.jsonl \
      --engine scylladb \
      --containers fts-bench-scylla:scylladb \
      --containers fts-bench-vector-store:vector-store \
      --vs-url http://127.0.0.1:16080 --keyspace wiki --vs-index articles_body_fts \
      --interval 1 --duration 0 --label 'index-rate $a' \
      --corpus /mnt/nvme/data/corpus.jsonl"
```

It adds no wall clock and no rung: steady state is three `/sys/fs/cgroup` reads
per container per second against a path resolved once and cached, plus one
`docker inspect` pair per container at arm start. **It does share the SUT's
eight cores with the engines, and that reaches the footer.**

**`fts-bench-scylla` and `fts-bench-vector-store` get one row each, split and
summed, never merged.** The architecture is a ScyllaDB cluster plus a
vector-store cluster holding the index, and a single merged number would hide
where the memory actually goes.

### 6d. The two phases

**Phase A — calibration, N=1, never published.**

```bash
a=r2
ssh fts-harness "REPS=1 LADDER=4,8,16,32,64,128 MAX_DOCS=3500000 \
    CORPUS=/mnt/nvme/data/corpus.jsonl VS_PORT=16080 \
    OUT_DIR=/mnt/nvme/work/results-cal/$a SAMPLES_DIR=/mnt/nvme/work/samples/$a \
    ~/run-arm.sh $a-cal --vs-interval 0.25"
```

Read the arm's ceiling and `c_sat` off its `docs_per_s` column and record both
in `$R/env/rate-ladder.txt` beside the arm name. **`OUT_DIR` is `results-cal`,
not `results`** — `pull_arm` globs `results/$a/*` into `points/`, and a
concurrency-ladder CSV there fails the render.

**Phase B — the measurement, N=3, published.** `RATES` is the seven rates chosen
from the campaign's `C_max`; the list below is the illustration from "What a
rung costs", **not a measured ladder**.

```bash
a=r2
RATES=20000,40000,60000,80000,100000,120000,140000
ssh fts-harness "REPS=3 RATES=$RATES CAP=512 MAX_DOCS=3500000 \
    CORPUS=/mnt/nvme/data/corpus.jsonl VS_PORT=16080 \
    OUT_DIR=/mnt/nvme/work/results/$a SAMPLES_DIR=/mnt/nvme/work/samples/$a \
    ~/run-arm.sh $a-rate --vs-interval 0.25"
```

| Arm | Sweep names | Remote `OUT_DIR` / samples | Local destination |
|---|---|---|---|
| R1 | `r1-cal`, `r1-rate` | `…/results-cal/r1`, `…/results/r1`, `…/samples/r1` | `$R/r1/scylla/` |
| R2 | `r2-cal`, `r2-rate` | `…/results-cal/r2`, `…/results/r2`, `…/samples/r2` | `$R/r2/scylla/` |
| R8 | `r8-cal`, `r8-rate` | `…/results-cal/r8`, `…/results/r8`, `…/samples/r8` | `$R/r8/scylla/` |

**Slices are still named for the sweep, not the arm.** It is what `probe_windows`
`--stderr` glob matches, and it is what keeps Phase A's windows and Phase B's
apart in one probe file — the two phases run under one probe per arm and the
sweep name is the only thing in the filename that distinguishes them.

**`--vs-interval 0.25` on both phases.** The default is 1.0 s, and the ladder's
top rung is planned at ~29 s of load: at 1.0 s that is a thin series risk the
campaign has no reason to take. A build with under three index readings is
skipped by name in the growth chart. `--index-watch` is on by default on
`scyllarate` and is what this chart measures.

**Phase B is ~23 min of load per arm** (3 reps × ~454 s at the illustrative
ladder) before reset and settle, against Phase A's ~8 min at N=1. Run them with
a generous timeout or in the background; do not poll every few seconds — it
wastes the session.

### 6e. Close the arm out, on the harness, before `*-down`

**Why per arm rather than once at the end.** `*-down` removes the containers,
and the vector-store's two startup lines go with them: once R2 is up, nothing
can produce R1's `docker logs`. Those lines are what license the arm's CSVs, so
the numbers and the log that justifies them come home together or the arm is
unlabelled data. `verify_cpu_usage` reads each container's quota with
`docker inspect`, so it cannot be deferred to laptop work either.

```bash
a=r2; flag=--scylladb-cdc-buf376; mread=anon    # R8: mread=anon+cache
ssh fts-harness "set -eu
  cd \$BENCH && . tools/fleet_env.sh
  L=/mnt/nvme/work/logs/$a; mkdir -p \$L
  docker logs fts-bench-vector-store > \$L/vector-store.log 2>&1
  cp docker/.env.sut \$L/env.sut
  docker image inspect --format '{{.Id}} {{index .RepoTags 0}}' \
      \$(grep '^VECTOR_STORE_IMAGE=' docker/.env.sut | cut -d= -f2-) > \$L/image.txt
  .venv/bin/python3 -m ftsbench.verify_arm $flag --log \$L/vector-store.log \
      > \$L/verify-arm.txt 2> \$L/verify-arm.err || true
  grep -c 'ARM MISMATCH' \$L/verify-arm.err || true
  tools/sut_probe.sh stop /mnt/nvme/work/probe/$a.jsonl
  .venv/bin/python3 -m ftsbench.probe_windows --arm $a \
      --probe /mnt/nvme/work/probe/$a.jsonl \
      --stderr '/mnt/nvme/work/results/$a/*-rep*.stderr.tsv' \
      --out-dir /mnt/nvme/work/probe/$a --memory-read $mread \
      --table \$L/resource-by-rung.csv
  .venv/bin/python3 -m ftsbench.verify_cpu_usage --data-dir /mnt/nvme/work/probe/$a \
      --containers fts-bench-scylla fts-bench-vector-store \
      --output-json \$L/cpu-utilisation.json"
```

**All of it runs inside the `ssh`, while the stack is still up.**

**The `probe_windows` line exits 1 on Phase B and the close-out stops there**
until the one-line fix lands — `set -eu` is in force and the duplicate-key check
returns 1. See "Blocker on the critical path". The `--stderr` glob points at
`results/`, which is Phase B alone; Phase A's logs are under `results-cal/` and
slice normally, but they are calibration and never reach a chart. **Do not
"fix" this by pointing the glob at the calibration logs** — that produces a
resource table for a ladder that was never published and labels it as the arm's.

Per arm:

| Arm | `flag` | `mread` |
|---|---|---|
| R1 | `--scylladb-cdc-buf15` | `anon` |
| R2 | `--scylladb-cdc-buf376` | `anon` |
| R8 | **none — drop the `verify_arm` line** | `anon+cache` |

**`verify_arm` covers R1 and R2 only.** `ftsbench/target.py` registers
`--scylladb-cdc-buf15`, `--scylladb-cdc-buf376` and
`--scylladb-cdc-buf376-commit30` (a target with no arm, left registered rather
than removed), and there is **no target for a disk-backed index**. So **R8 is
confirmed by reading its `index=disk:/var/lib/vector-store/fts` startup line
out of the captured log by hand** — which is why the log is captured before the
check rather than piped through it.

#### `verify_arm` will disagree about the cadence, and it is expected to

**On R1 and R2 `verify_arm` exits 1 with exactly one mismatch, about
`commit_interval`, and that is this campaign's 1 s cadence, not a bad arm.**
Both targets leave `VS_FTS_COMMIT_INTERVAL` at `UNSET`
(`ftsbench/target.py`), and `verify_arm.expected()` substitutes
`DEFAULT_COMMIT_INTERVAL = "3s"` for an unset knob
(`ftsbench/verify_arm.py:45`). It therefore asks for the old default while the
arm runs 1 s:

```
ARM MISMATCH: scylla-cdc-buf376: commit_interval is '1s', arm asks for '3s'
```

That nonzero exit is why the line is run `|| true` with its streams captured:
under `set -eu` an aborting `verify_arm` would take the whole close-out with it
— **the probe would still be running, the arm unsliced, and `*-down` would then
destroy the log it needed**. Failing open here is deliberate, and it moves the
gate onto the operator:

- **`$L/verify-arm.err` must contain exactly one `ARM MISMATCH` line, and it
  must be the `commit_interval` one.** One is the expected count. Any other
  count, or any mismatch naming `buffer_mb` or `commit_threshold`, **voids the
  arm** — those are the checks that still bind.
- **The cadence itself is confirmed by hand**, off the same log, against the
  § 6b requirement that every arm reads `commit_interval=1s`.
- **`verify-arm.txt` and `verify-arm.err` are both pulled home** with the arm;
  the expected mismatch is part of the manifest, not noise to discard.

**The one-line proper fix is outside this runbook**: registering
`VS_FTS_COMMIT_INTERVAL = "1s"` on both targets (and their
`variant="…-commit3s"` strings, which name a cadence the arms no longer run)
would make the gate bind again and this subsection unnecessary. Until that
lands, the triage above is the gate.

`--memory-read` is required rather than defaulted because there is no one
memory number for the campaign's arms. `rss_bytes` is cgroup v2 `memory.stat`
**`anon`**, never `memory.current`, which includes page cache and would flatter
whichever engine touched less disk. On R8 the index is on the NVMe, so it is
page cache and `cache_bytes` is the reading.

### 6f. Pull the arm home, from the laptop

```bash
pull_arm() {                      # pull_arm <arm-dir> scylla
    local a="$1" d="$R/$1/$2"
    test -n "$R" && test -d "$d" || { echo "no such arm directory: $d" >&2; return 1; }
    scp    "fts-harness:/mnt/nvme/work/results/$a/*"     "$d/points/"          || return 1
    scp    "fts-harness:/mnt/nvme/work/results-cal/$a/*" "$d/calibration/"     || return 1
    scp -r "fts-harness:/mnt/nvme/work/samples/$a/"*     "$d/samples/"         || return 1
    scp -r "fts-harness:/mnt/nvme/work/logs/$a/"*        "$d/logs/"            || return 1
    scp    "fts-harness:/mnt/nvme/work/probe/$a.jsonl"   "$R/sut/cpu-$a.jsonl" || return 1
    scp -r "fts-harness:/mnt/nvme/work/probe/$a/"*       "$d/probe/"           || return 1
    # run-arm.sh writes its stderr log and run-windows.tsv beside the CSVs.
    # Both are the probe join's other half, so they belong with the logs.
    mv "$d"/points/*.tsv "$d/calibration"/*.tsv "$d/logs/" 2>/dev/null
    local n; n=$(ls "$d"/points/*.csv 2>/dev/null | wc -l)
    [ "$n" -eq 3 ] || { echo "$a: $n point CSVs, expected 3 (1 rate ladder x 3 reps)" >&2; return 1; }
    local c; c=$(ls "$d"/calibration/*.csv 2>/dev/null | wc -l)
    [ "$c" -eq 1 ] || { echo "$a: $c calibration CSVs, expected 1" >&2; return 1; }
    local w; w=$(ls "$d"/probe/cpu-*.jsonl 2>/dev/null | wc -l)
    [ "$w" -eq 21 ] || { echo "$a: $w rung slices, expected 21 (7 rungs x 3 reps)" >&2; return 1; }
    rss_breach "$d/logs/resource-by-rung.csv" || return 1
    echo "$a: home"
}

# The blocking gate. The vector-store stops adding documents at its budget and
# keeps answering queries, so a breach is silent document skipping and the rate
# beside it counts documents that were never indexed.
rss_breach() {
    awk -F, -v OFS=, 'NR==1 { for (i=1;i<=NF;i++) c[$i]=i; next }
        $(c["mem_headroom_bytes"]) != "" && $(c["mem_headroom_bytes"]) <= 0 {
            print "BREACH", $(c["sweep"]), $(c["concurrency"]), $(c["rep"]), \
                  $(c["container"]), $(c["mem_peak_bytes"]); bad=1 }
        END { exit bad }' "$1" >&2
}

pull_arm r2 scylla
```

**A non-zero return is the arm's gate, not a warning**: the stack that produced
it is still up, which is the only moment re-running a lost rep is cheap.

**A point CSV existing is not a finished run.** `scyllarate` creates `--out` at
start, so the "3 point CSVs" gate passes while the third run is still going.
Check the arm's last stderr log for its final `-> index …` line before trusting
the count.

**The 21-slice check and `rss_breach` are the two lines this arm's `pull_arm`
cannot pass** until `probe_windows` learns the rate key. Do not comment them
out to get an arm home — they are the arm's blocking resource gate, and an arm
that came home past a disabled gate is an arm nobody can say was valid. See
"Blocker on the critical path".

Then, and only then:

```bash
ssh fts-harness 'cd $BENCH && . tools/fleet_env.sh && make scylla-down'
```

## Phase 7 — verify all three arms came home

From `$R` alone, **before the stop**:

```bash
for a in r1 r2 r8; do ls "$R/$a"/scylla/points/*.csv | wc -l; done   # 3 each
for a in r1 r2 r8; do ls "$R/$a"/scylla/calibration/*.csv | wc -l; done  # 1 each
ls "$R"/sut/cpu-r{1,2,8}.jsonl | wc -l                              # 3
grep -l "ingest tuning for" "$R"/r{1,2,8}/scylla/logs/vector-store.log | wc -l   # 3
grep -c "index=disk:/var/lib/vector-store/fts" "$R"/r8/scylla/logs/vector-store.log  # >=1

# Every published row is a rate-ladder row: the axis is in the header, not
# inferred from the filename.
grep -h "^# latency_basis" "$R"/r{1,2,8}/scylla/points/*.csv | sort -u   # intended_start
grep -h "^# latency_basis" "$R"/r{1,2,8}/scylla/calibration/*.csv | sort -u  # service

# Every rung of every arm carries a CPU and RSS reading, and none breached.
ls "$R"/r{1,2,8}/scylla/probe/cpu-*.jsonl | wc -l          # 63 (3 arms x 21)
ls "$R"/r{1,2,8}/scylla/logs/resource-by-rung.csv | wc -l  # 3
for f in "$R"/r{1,2,8}/scylla/logs/resource-by-rung.csv; do rss_breach "$f" || echo "^ $f"; done
grep -c ',thin$\|,empty$' "$R"/r{1,2,8}/scylla/logs/resource-by-rung.csv  # named, not silent
```

**The last three lines are what the `probe_windows` blocker takes out.** They
are left in rather than removed: the campaign's verification is what it is, and
a run that cannot execute them has not been verified.

**Then render S2c on the laptop with the boxes still running.** If it cannot
produce its PNG and its `--table` twin from `$R` without touching the fleet,
**the pull is not finished** — a five-minute fix now and a re-entry plus a
re-measured arm after the stop.

```bash
.venv/bin/python3 build-rate/charts/rate_vs_offered.py --keep-warmup \
    --series "R1 scylla-buf15=$R/r1/scylla/points/*.csv" \
    --series "R2 scylla-buf376=$R/r2/scylla/points/*.csv" \
    --series "R8 scylla-buf376-disk=$R/r8/scylla/points/*.csv" \
    --title "Index rate against offered rate — the ScyllaDB knobs (PRELIMINARY)" \
    --output "$R/s2c-scylla-knobs.png" \
    --table  "$R/s2c-scylla-knobs.csv"
```

Expect `(6 lines, 42 points)` at a 7-rung ladder — `2 × arms` lines and
`2 × arms × rungs` points.

**`rate_vs_offered.py`, never `rate_vs_concurrency.py`, and the renderer
enforces it.** Each refuses the other ladder's CSVs by name: a
concurrency-ladder row has no `target_docs_per_s` and `rate_vs_offered.py` says
so and stops, rather than drawing a blank x. Phase A's CSVs are the ones that
would trip it, which is why they live in `calibration/`.

**The `--table` twin is the chart's other half and its columns are new**:
`series, metric, offered_docs_per_s, reps, docs_per_s_median, docs_per_s_min,
docs_per_s_max, shortest_wall_s, saturated, in_flight_peak`. Read `saturated`
and `in_flight_peak` together before reading any knee — see the gate table. A
hollow ring on the chart is a saturated rung, and it is **kept, not dropped**:
it is how a fast arm and a slow arm share one x grid and each still show its own
knee.

**Where a line leaves the dotted `y = x` diagonal is the reading.** The solid
line leaving it is where the engine stopped accepting everything offered; the
dashed line leaving it is where the *index* stopped keeping up, which is the
number this campaign exists for.

**There is no budget seam to check.** One `--max-docs` covers every rung of
every arm, so every row of the `--table` twin is `reps=3` at one document bound,
and the `16`→`32` seam the previous grid carried is gone rather than unverified.

**Top-up rule.** If **R1↔R2** or **R2↔R8** lands within ~5% at the knee, top up
to N=5 *at that rung only* — 2 arms × 2 reps × 1 rung, not a re-run. Do it now,
while the stack can still be brought back up cheaply. **Do not top up a rung
that is `generator_saturated` on either arm**: a tie between two saturated rungs
is a tie between two client ceilings.

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

**If the OpenSearch half runs next in the same session, do not stop the boxes.**
Hand over to `INDEX-RATE-OPENSEARCH-RUNBOOK.md` at its Phase 5 — its re-entry,
corpus and harness-build phases are already satisfied, and it does not need the
vector-store image at all.

**The rate ladder puts a hard ordering between the halves that did not exist
before.** `C_max` is the highest ceiling across all five arms, so **the correct
order is now Phase A on all five arms, then the ladder is chosen, then Phase B
on all five** — this half's three and the other half's two. A session that runs
this half end to end and then starts the other has already fixed its x grid off
three arms and will discover the OpenSearch ceiling afterwards. If the halves
must run in separate sessions, **the calibration pass has to span both of them
and its result recorded in `$R/env/rate-ladder.txt` before either half measures
anything.**

## The SUT, for the record

**The 50/50 cgroup split** — `docker/.env.sut`, applied by compose as
`mem_limit` / `cpus` / `cpuset` on each service:

| Service | `cpuset` | `cpus` | `mem_limit` | In-process budget |
|---|---|---|---|---|
| ScyllaDB — `--smp 4 --memory 24G --overprovisioned 0` | `0-3` | 4 | 28g | 24 GiB |
| vector-store | `4-7` | 4 | 28g | `VECTOR_STORE_MEMORY_LIMIT` = 26 GiB |

The vector-store — the process doing the indexing this chart measures — sits on
the same four cores OpenSearch gets in the other runbook, which is the sense in
which the indexing halves are matched. **The ScyllaDB stack has eight cores and
56 GiB of cgroup across two containers; OpenSearch has four cores and 28 GiB in
one.** That is load-bearing for R2↔R4 and irrelevant inside any chart drawn
from this runbook alone, and it reaches the footer either way.

`../BUILD-RATE-LOOP.md`'s finding that the split is probably wrong for the CDC
path — Scylla at 3.56 of 4 while the vector-store idles near 1.9 of 4 — is
carried as a caveat, not changed here. This campaign's per-rung CPU reading is
what turns it from a caveat into a column.

**R8 and the memory gate.** A file-backed index is page cache, not anonymous
memory: the allocation gate behind `VECTOR_STORE_MEMORY_LIMIT` does not see it
growing. The cgroup's 28g `mem_limit` **does** count page cache, and it
reclaims rather than kills, so R8's failure mode is a slower build under
reclaim rather than an OOM — which is exactly what the arm exists to measure.
`cache_bytes` against `rss_bytes` is the reading that tells the two apart.

**Images.** `scylladb/scylla:2026.3.0-rc2`; vector-store built from
`knowack1/vector-store` @ **`94a23ef2`**. That commit supersedes `282d9efc` and
is a strict superset of it: with `VECTOR_STORE_FTS_INDEX_DIR` unset the index
path is byte-for-byte the previous behaviour, so R1 and R2 measure the same
image R8 does and no arm pays for the knob's existence.

**Networking.** `SCYLLA_BROADCAST_RPC` and `SCYLLA_VS_URI` point at the SUT's
private IP so the harness-side driver does not discover the docker-bridge
address; `scyllarate` reaches `<sut>:9042` and reads the index at
`http://<sut>:16080`.

## Gates

| Gate | Rule |
|---|---|
| **Cap did not bind** | **blocking.** `in_flight_peak` read against the `--concurrency` cap (512 here). A rung with `generator_saturated=true` **and** `in_flight_peak` at the cap measured **the harness**, not the engine: the point is **void, and re-run at a higher cap** — never reported, never annotated. Saturation alone is a finding; saturation *at the cap* is an instrument reading |
| **Schedule held** | **annotating.** `queue_p99_ms` against `p99_ms` on the same row. If queueing is a material fraction of the latency, the chart is measuring the harness rather than the engine. **Pick a threshold before the fleet runs, write it into `$R/env/rate-ladder.txt` with its justification, and apply it to every rung** — this is what makes coordinated-omission safety a number rather than a claim. A rung over it is plotted and named |
| **Achieved vs offered** | `achieved_offered_ratio` ≥ 0.95, or the rung is `generator_saturated`. **Saturation is a finding, not a gap**: the rung is drawn with a hollow ring and kept, which is how a fast arm and a slow arm share one x grid and each still show its own knee. What it may not be called is the offered rate |
| Short point | a level under 3 s is not a measurement; the renderer names every one in the footer, and the fix is a bigger campaign-wide `--max-docs` and a re-run of **every arm in both runbooks** |
| Thin series | a build with under three index readings is skipped by name in the growth chart. `--vs-interval 0.25` on every sweep is what keeps it off |
| Not settled | `index_settled=false` is a lower bound: drawn hollow, the reason (`index_status`) named in the table. At a 1 s commit this is a finding |
| Arm took | the vector-store's two startup lines match the arm **before** its ladder runs, `commit_interval=1s` among them; R8 by hand |
| Manifest | every arm directory carries the `.env` it ran with and the image commit, alongside the CSVs |
| **RSS breach** | **blocking, the one gate that voids an arm.** The vector-store stops adding documents once `VECTOR_STORE_MEMORY_LIMIT` (26 GiB) is reached, logs an error and keeps answering queries, so a breach is *silent document skipping* — `../BUILD-RATE-LOOP.md` caught exactly this at a 27.68 GiB peak. Read three ways: anon `rss_bytes` against the 26 GiB budget, the arm's memory read against the 28g `mem_limit_bytes` (`rss_breach`), and `index_docs_last` reaching the cap as corroboration. **The arm is re-run, not annotated.** `cache_bytes` on R8 is **exempt** — a file-backed index is page cache the cgroup reclaims rather than kills, which is what the arm exists to measure |
| **CPU attribution** | **annotating, never dropping.** Per rung, the indexing container's peak `cpu_cores_used` against its quota: `ok` at ≥0.85, `not-CPU` below it, `?` where no series covers the window. **A `?` is not a pass.** A plateau at `not-CPU` is still plotted; what changes is that it may not be described as the engine's throughput limit |
| **Probe source** | every sample reads `source=cgroup-anon`. The `docker stats` fallback is not anon-only and has no CPU counter, so one fallback sample destroys R8's cache reading; `probe_windows` refuses the arm rather than reporting it |
| **Clock skew** | harness-to-SUT skew under one probe tick (1 s), measured at re-entry and recorded. The window is never padded to cover skew |

**There is no client-headroom gate.** Every rung is measured and plotted on its
own merits. The source campaign's G7 — a level clears only at ≥2x under the
measured client ceiling — is deliberately **not** carried over: it exists to
decide whether a *client* ceiling may be quoted as an *engine* number, and on
this axis the engine's searchable rate is what is quoted. The client floor
`HARNESS-AWS-RUNBOOK.md` recorded (≥266,578 docs/s on `scyllarate` at batch 1
against the null sink) is quoted in the footer as context, never applied to a
point.

## Caveats this half carries into the write-up

- **No arm here asks the index to publish.** `scyllarate`'s probe keeps
  `core`'s default, which is never to ask (`core/src/index.rs`, `settle_hint`);
  the ScyllaDB arms wait for their own next commit, now at most 1 s away.
  **The OpenSearch arms in the other runbook are credited with a publish their
  configured policy had not yet delivered.** That is worth at most 3 s at R4's
  cadence against a 45–220 s level, but it runs one way only and it lands on
  the side of the one cross-engine comparison this campaign has —
  **compounding, not offsetting, the 1 s-vs-3 s cadence mismatch, which also
  favours this half.** Both are disclosed, neither is netted out.
- **The ladder's rates were placed off a single N=1 calibration pass.** `C_max`
  is one closed-loop reading of the fastest arm, not a distribution, and every
  arm's x grid hangs off it. A `C_max` read low leaves the fastest arm's knee
  off the top of the chart; read high, it spends rungs above every arm's
  ceiling. **Phase A's ladder is recorded in `$R/env/rate-ladder.txt` so a
  reader can see what the grid was hung on.**
- **A saturated rung is a client reading and is drawn as one.** The hollow ring
  says the arm did not deliver what it was offered; `achieved_offered_ratio`
  says by how much. The x value of such a point is **what was asked for, not
  what arrived**, and a knee read off a run of saturated rungs is a knee in the
  generator.
- **`p50_ms`/`p99_ms` on this half are per request, and a request is one
  document.** Under pacing they are also measured from when a request was
  **due** — `latency_basis=intended_start` in the header. **Phase A's latencies
  are `service` and are a different measurement wearing the same column names;
  the two may not be pooled.**
- **Cadence cost is not measured.** Every arm here publishes at 1 s and
  nothing separates the cost of a cadence from any other cause. **R4 publishes
  at 3 s**, so the cross-engine pairing carries a cadence difference it cannot
  quantify — see "The cadence is 1 s on both halves, and R2 ↔ R4 is matched".
- **The x axis is now a document per second on both halves, and the request-shape
  asymmetry moved into the in-flight cap.** `--concurrency 512` here against
  `--concurrency 128` on the other half is the same 1,024-documents-per-request
  difference in a new place. It is no longer on the axis, so curve *shape* is
  readable across the engines for the first time — but only where **both** sides
  of a comparison clear the cap-did-not-bind gate. `in_flight_peak` is in every
  row and in the `--table` twin so a reader can check that rather than take it
  on trust.
- **The cap is not the talk's operating point.** `../BUILD-RATE-LOOP.md`
  measured the engine ranking *inverting* between 1.2M and the 8.97M corpus.
  This ladder answers "what saturates", not "what wins at scale".
- **Contiguous sharding at the cap is not the first N documents.** Fine for
  engine against engine as long as both sides shard identically; stated on the
  chart rather than discovered later.
- **Dropping an FTS index does not release its RAM.** Mild at this cap; the
  probe is what proves it per arm.
- **R8 is a scratch directory, not durability.** The index is wiped at create
  and removed on drop and is never reopened. The arm measures where the
  segments live during a build, and nothing about restart.
- **The per-level reset is excluded from the CPU window and carried separately
  as `reset_s`.** A reset that grew across an arm is a finding, and **R8 is the
  arm most likely to show one** — its per-level drop and rebuild has to clear a
  file-backed index off the NVMe rather than free heap.
- **Encode cost is ~0.62 µs per document here against ~16.4 µs on the
  OpenSearch client.** Outside the latency window but a harness artifact in a
  comparison whose credibility rests on symmetry.
- **The probe shares the SUT's eight cores with the engines it measures.**

## Cost

An estimate with its derivation, never a measurement — and **the derivation now
depends on rates that do not exist yet**, so the totals below are a shape with a
placeholder in it, not a budget anyone has checked.

A Phase B rung's load time is `max_docs / rate`, floored: at the illustrative
7-rung ladder that is ~454 s a rep, so **~23 min of load per arm at N=3** before
reset and settle. Phase A is one N=1 concurrency ladder at the same 3,500,000
bound, ~8 min an arm. Call it **~40 min an arm** across both phases with reset,
settle and two stack cycles — and **re-derive it the moment Phase A produces
real rates**, because the floor rung alone is ~39% of the ladder.

| Item | Time |
|---|---|
| Re-entry (SSH, mounts, corpus decompress, harness build) | ~20 min |
| **Vector-store image rebuild from `94a23ef2`** | **~15 min** |
| Smoke, both axes, + the Phase B floor-rung bound check | ~20 min |
| Phase A on R1, R2, R8 | ~35 min |
| Phase B on R1, R2, R8 | ~1.6 h |
| Pull, verify, render, stop | ~15 min |
| **Total** | **~3.4 h, band 2.8–4.5 h, ~$12–20 at $4.37/h — placeholder until Phase A lands** |

**If `corpus.jsonl.zst` is not on the harness root, add ~40 min** (~$3), and it
is one-time only if the archive is written back.

**Budget 3.5 h and do not plan a session shorter than that.** The half cannot
be paused: every stop wipes `/mnt/nvme` and costs a full re-entry. If it must
be split, split it at an arm boundary and record which arms ran in which
session — **a re-entry between R1 and R2 is a provenance difference inside the
writer-buffer comparison, and the footer has to carry it.**

**Cost levers, decided before launch, not mid-run.**

| Lever | Saving | What it costs |
|---|---|---|
| Drop R8 | −0.7 h | the where-the-index-lives read goes, and S2a loses its ScyllaDB disk arm |
| **Drop the ladder's floor rung** | **−0.3 h** | the largest single saving on the page — the floor rung is ~39% of a rep — and it costs the low end of every curve, where an engine is furthest from saturation |
| Drop a top rung | ~−0.04 h | almost nothing, and it costs the knee. **Never the first lever** |
| N=3 → N=1 on the middle rungs | −0.3 h | the middle of every curve carries no spread |

With three arms and two single-engine readings, every lever costs a reading
outright. **The resource probe is not on the cost line** — it adds no run, no
rung and no wall clock.

## Where it lands

Nothing on the main deck until the pass has been written up. S2c (R1/R2/R8) is
the chart this runbook can produce alone, and it is where the 1.42x
writer-buffer result gets a figure it can be quoted from — one engine, one
axis, no false comparison available to draw.

The primary chart and S2a both need R4 from the other runbook. Record each
arm's ceiling and `c_sat` in `../TUNING.md` with the run that produced it, and
the CPU and RSS at that rung beside them out of `resource-by-rung.csv`.

Mandatory footer clauses for anything drawn from this half: that x is the
**offered** rate and a ringed point did not achieve it; that one operation is one
document and the in-flight cap was **512 here against 128 on the OpenSearch
half**, with `in_flight_peak` showing the cap did not bind; that the rates were
placed off a single **N=1** calibration pass and `C_max` is one reading; that
`p50_ms`/`p99_ms` are measured from when a request was **due**
(`latency_basis=intended_start`); that every arm ran one campaign-wide
`--max-docs 3500000`, so every rung ingested the identical documents and only
the rate differed; that every arm runs a **1 s** cadence and **cadence cost is
therefore unmeasured and unclaimable**; that anything pairing this half with R4
states both cadences — **1 s here, 3 s there** — and that the gap is not
cadence-corrected; that the ScyllaDB stack drew eight
cores across two containers and **what each side actually drew of it**, now
that it is measured rather than assumed; that memory is the cgroup's *anon*
figure with R8's file-backed index cost named separately rather than folded in;
that a rung marked `not-CPU` is plotted but may not be called an engine
throughput limit; which points are lower bounds and why; the cap and the
sharding; the client floor as context; and **PRELIMINARY**.
