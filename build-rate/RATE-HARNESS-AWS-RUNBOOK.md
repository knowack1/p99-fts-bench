# Rate-harness-on-AWS runbook — proving the loader can *offer* a rate, on the fleet

**Hand this file to Claude Code as the instruction and it runs the whole
campaign: starts the two AWS boxes, builds both harnesses, measures each against
its own accept-and-discard sink on the **offered-rate** axis, pulls every
artifact home, stops the boxes.** It is self-contained — every script it needs
is inline below. Nothing else in `bench/` has to be read first.

It is the sibling of [`HARNESS-AWS-RUNBOOK.md`](HARNESS-AWS-RUNBOOK.md) on the
other axis. Same fleet, same boxes, same `engine-mock`, same samplers, same
collection discipline. **What changes is the ladder, and with it the
deliverable, the budget arithmetic, the gates and the chart.**

| | [`HARNESS-AWS-RUNBOOK.md`](HARNESS-AWS-RUNBOOK.md) | **this file** |
|---|---|---|
| Ladder | `--concurrency 4,8,…,128` | `--target-rate 25000,…,400000` |
| Loop | closed — the engine sets the pace | **open — the client sets the pace** |
| `--concurrency` | the axis | **one number: an in-flight cap that must never bind** |
| Question | *how fast can this client go* | ***can this client offer rate X, faithfully*** |
| Deliverable | a floor, written `≥` | **a fidelity verdict and a pacing ceiling** |
| `latency_basis` | `service` | **`intended_start`** — coordinated-omission safe |
| Chart | `tools/plot_harness_grid.py` | **`charts/rate_vs_offered.py`** |

**Neither supersedes the other and both are worth running.** `ftsbench/pacer.py`
is explicit, and `core/src/pacer.rs` inherits the reasoning: closed loop is the
right instrument for a maximum-throughput question and the wrong one for "what
does it do at rate X". This runbook asks the second question of the *harness*,
which is the question the index-rate campaign's whole axis rests on and which no
fleet session has ever asked.

## Why this exists — one dependency, currently unverified

[`INDEX-RATE-MATRIX-PLAN.md`](INDEX-RATE-MATRIX-PLAN.md) moved its campaign onto
the offered-rate axis on 2026-09-16. Every line on its chart is an engine's
response to a rate **the client claims to have offered**. That claim has been
checked exactly once, on a laptop, against `engine-mock`: offered 20,000 and
50,000 docs/s came back at 19,989.5 and 49,962.3 on `scyllarate`, and 20,120.8
and 50,291.2 on `osrate`, with `queue_p99_ms` under 1.5 ms throughout.

**Two things are missing from that, and both are what this runbook supplies.**

1. It was a laptop, not the fleet. Different cores, different network, different
   corpus line.
2. It was 50,000 docs/s. The plan's grid goes higher, and **nobody has measured
   where the claim stops being true.**

Until that ceiling is a number, the index-rate campaign cannot tell a rung where
the *engine* fell short from a rung where the *harness* never offered the rate
in the first place. Those two look identical on the chart and mean opposite
things.

## The three deliverables

**1. Pacer fidelity, per rate.** At every unsaturated rung,
`achieved_offered_ratio` within a few percent of 1.0 and `queue_p99_ms` a small
fraction of `p99_ms`. This is what licenses the sentence "the engine was offered
50,000 docs/s" in any downstream write-up.

**2. The faithful-offer ceiling** — the highest rate one loader process can
offer on this box before something in the harness gives way. This number bounds
the top rung of every rate grid in the index-rate campaign. **If that campaign's
grid tops out above this number, its top rungs measure the harness and not the
engine.** Record it in `../TUNING.md` with the run that produced it.

**3. Coordinated-omission-safe latency.** Under a rate ladder a request's clock
starts when it was *due*, so a stall lands at full size on everything queued
behind it. The concurrency ladder cannot produce this reading at all — it
reports service time. These are the first `intended_start` latencies the fleet
has recorded.

## The finding this runbook is most likely to produce

**The paced producer is one thread, and it is shared by both halves.**

`core/src/sweep.rs` spawns `fill_channel` on a single `tokio::task::spawn_blocking`
thread. That one thread does, per document: read a line
(`core/src/corpus.rs`), `serde_json` deserialize it, wait until the document is
due, and hand it to the channel. **No amount of `--concurrency` raises that
throughput** — the cap adds consumers, not producers.

So there is a single-threaded ceiling on the offered rate, it lives in `core`,
and **both halves inherit exactly the same one**. That gives this campaign a
cross-check no other runbook here has:

> **If Part A and Part B stop being able to offer their rate at roughly the same
> docs/s, the ceiling is the shared producer — not either client, and not either
> sink.** If they stop at different rates, the ceiling is whatever differs
> between them, and the sink CPU column says which.

Design the reading around that comparison. It is the one result here that
would change `core`.

## What this measures, and what it is not

The subject is **the loader**, not an engine. `scyllarate` pushes prepared
`INSERT`s at `engine-mock --mode cql`, which answers the CQL wire and discards
every row; `osrate` posts `_bulk` at `engine-mock --mode http`, same deal.

**No number from this runbook is an engine number and none belongs in the deck.**

The mock can still be the thing that gives way, and on the CQL half it probably
is: it advertises no shard extension and an empty `system.peers` (trap 1), so
the driver opens **one** connection, one connection is one tokio task, and about
one core is all that half can reach. A rung that fell short with the mock's CPU
at its budget is **the instrument's ceiling wearing the harness's name** — which
is why Phase 8's sink gate is not optional here either.

Not to be confused with:

| File | Its job |
|---|---|
| [`HARNESS-AWS-RUNBOOK.md`](HARNESS-AWS-RUNBOOK.md) | the same fleet on the **concurrency** axis — the client floor |
| [`INDEX-RATE-MATRIX-PLAN.md`](INDEX-RATE-MATRIX-PLAN.md) | the **engine** campaign this one is a precondition for |
| [`INDEX-RATE-SCYLLA-RUNBOOK.md`](INDEX-RATE-SCYLLA-RUNBOOK.md) · [`…-OPENSEARCH-…`](INDEX-RATE-OPENSEARCH-RUNBOOK.md) | that campaign, made runnable, against real engines |
| `../TUNING.md` § "Per-process client ceilings" | where the ceiling this produces gets recorded |
| `../engine-mock/README.md` | the instrument: what it serves, refuses, and does not model |
| `README.md` § "Two ladders, and exactly one per run" | the columns, in the crate's own words |

## The fleet

| Alias | Instance | Role |
|---|---|---|
| `fts-harness` | `i-08d8d2505e16683f7` · `k-nowacki-fts-benchmark-harness` | **the subject.** Runs the loaders. |
| `fts-sut` | `i-0e3e4b6b02e654b7f` · `k-nowacki-fts-benchmark-sut` | **the instrument.** Runs the sink. |

Both `i8g.2xlarge` (8 vCPU Graviton4, 61 GiB, aarch64), `eu-north-1b`,
Amazon Linux 2023, user `ec2-user`, key `~/.ssh/KarolNowackiAws.pem`.

**The sink runs on the other box, always.** A loopback sink has far lower RTT
than the private network. Measured private RTT between these two: **0.142 ms**.
On this axis RTT matters in a way it did not before — it sets how much in-flight
the cap has to allow at a given rate (Phase 0, "The cap").

There is no AWS CLI credential on this laptop — the console in Chrome is the
only way to start and stop the boxes. Phase 1 and Phase 7.

## The results directory — fix it first, on the laptop

Everything on the fleet is ephemeral: `/mnt/nvme` is destroyed on every stop, so
**the laptop is the only place results survive**.

```bash
# Run this ONCE, at the very start of the session, and keep the shell.
export RUN_ID="rate-harness-aws-runbook-$(date -u +%Y-%m-%dT%H%MZ)"
export R="$HOME/Projects/Scylla/p99/bench/results/$RUN_ID"
mkdir -p "$R"/{env,corpus,scripts}
mkdir -p "$R"/scylla/{points,samples,logs,sinks}
mkdir -p "$R"/opensearch/{points,samples,logs,sinks}
printf '%s\n' "$RUN_ID" > "$R/RUN_ID"
ln -sfn "$RUN_ID" "$(dirname "$R")/rate-harness-aws-latest"
echo "results -> $R"
```

**A different prefix and a different symlink from the concurrency runbook, on
purpose.** The two produce CSVs that look compatible and are not: a rate-ladder
row carries one concurrency for every rung — its cap — so pooling the two would
stack a whole ladder on one x. `charts/rate_vs_offered.py` and
`charts/rate_vs_concurrency.py` each refuse the other's file by name, and the
directory prefix is what keeps a glob from having to.

**Capture `RUN_ID` once and reuse the variable.** If the shell is lost, recover
with `export R="$(readlink -f bench/results/rate-harness-aws-latest)"` rather
than recomputing the timestamp.

Report the absolute path of `$R` to the user when the run finishes.

---

# Part A — the ScyllaDB harness (`scyllarate`)

Run Part A first. It is the simpler instrument and it establishes the corpus,
the samplers and the box's CPU baseline that Part B is read against.

## Phase 0 — settle the matrix before anything bills

### One ladder, one budget, and the wall clock is arithmetic

**This is the largest simplification the axis change buys, so take it.** Under a
concurrency ladder nobody knows how long a level will run until it has run, which
is why the sibling runbook opens with a calibration rep whose only job is to size
`--max-docs`. Under a rate ladder:

```
wall_s  =  max_docs / target_rate
```

A rung's duration is known before the box is started. So **one `--max-docs` for
the whole campaign, both halves, every rung** — no sub-sweeps, no per-arm budget,
no overlap level to reconcile.

**`--max-docs 3500000`.** The same constant the index-rate campaign pins to, for
the same two reasons: every rung then ingests the identical documents and only
the rate differs, and one number is one thing to check rather than four.

| Offered rate | `wall_s` at 3,500,000 |
|---|---|
| 25,000 | 140 s |
| 50,000 | 70 s |
| 100,000 | 35 s |
| 200,000 | 17.5 s |
| 400,000 | 8.75 s |

**The cost shape is inverted from the concurrency ladder's, and it is worth
internalising: the top of the ladder is cheap and the floor is expensive.**
Extra resolution near the ceiling — which is the only place this campaign is
looking — is nearly free. The 25,000 rung alone is half the ladder's load time
and answers a question nobody asked. Do not lower the floor to be thorough.

**`--max-docs` is still bounded by the corpus.** `core/src/corpus.rs` opens the
file fresh per level and stops at EOF — it does not cycle — so a budget above the
line count silently runs a **shorter** rung instead of a longer one. Phase 4
generates 3,500,000 for exactly this reason. A budget raised past it is a
regeneration, not an edit.

### The default grid

```
25000   50000   100000   200000   400000
```

Doubling, five rungs, and **shared by both halves without exception** — a rate
means the same thing on `scyllarate` and `osrate`, which is the entire reason
this axis is preferable to concurrency for a cross-engine read. A rung added to
one arm is added to all of them.

**The grid is a starting point that Phase 5's bracket confirms or moves**, and
there are two rules on the answer:

- **At least two rungs strictly below the *slower* half's closed-loop plateau**,
  so every line has unsaturated points and the chart has a diagonal to sit on.
  A line whose every rung saturated is not a measurement of fidelity, it is a
  single ceiling drawn five times.
- **At least one rung above the *faster* half's plateau**, so the ceiling is
  **bracketed rather than merely approached.** A top rung that comes back
  faithful means the ceiling is above the grid, which is not a result — it is
  **"unresolved above 400,000"**, and it is fixed by adding a rung to every arm,
  not by reporting the top rung as the ceiling.

**Saturation is a finding here, not a failure.** On the concurrency ladder a
flattened level is an ambiguity to be attributed. Here, a rung that could not
hold its rate is the answer to the question being asked, and the chart rings it
(`generator_saturated`). What must not happen is a saturated rung being read as
an engine ceiling — Phase 8's decision table exists for that and nothing else.

### The cap — one number, and it must never bind

Under `--target-rate`, `--concurrency` stops being the axis and becomes a single
in-flight cap. `core/src/sweep.rs` refuses a list outright, before anything
connects:

```
--target-rate makes the offered rate the ladder, so --concurrency must be a
single in-flight cap, not N levels
```

**Size it by Little's Law, then multiply by four.** In-flight requests at a
sustained rate are `rate × latency`:

```
cap  =  4 × (top_rate × p99_seconds_from_the_bracket),  rounded up to a power of
        two, never below 512
```

The factor of four is headroom, and it is not generosity — it is what keeps the
cap from being the thing that fails. **A rung whose `in_flight_peak` sat at the
cap measured the harness's cap and nothing else. It is void and re-run higher —
never reported as a ceiling of any kind.**

**Default `--concurrency 4096`**, which is `4 × 400,000 × 2 ms` rounded up. Two
consequences to check before launching, one per half:

- **Memory.** Read-ahead is `queue_depth × concurrency × batch_size` documents
  at `QUEUE_DEPTH_PER_WORKER = 10`. At `4096 × 1` and this corpus's 3,948 B
  line that is **~162 MB** — comfortable on 61 GiB. It is not comfortable if
  a batch level is ever restored; see B3.
- **File descriptors, on the OpenSearch half only.** `opensearch/src/client.rs`
  leaves reqwest's pool at its default, which is unbounded per host, so **N
  bulks in flight take N sockets.** A cap of 4096 against AL2023's default
  soft `ulimit -n` of 1024 is `EMFILE` partway up the ladder. B4's arm script
  raises it; do not drop that line.

`scyllarate` has no `--queue-depth` flag — it hardcodes the shared default — so
leaving `osrate`'s flag alone is what keeps the two halves' read-ahead
identical. Do not pass it on either half.

### What this axis retires, named rather than quietly dropped

A reader who knows the concurrency runbook has to learn that these are gone and
why. **All of them were closed-loop artifacts:**

| Retired | What it was |
|---|---|
| The **two sub-sweeps** | low `4,8,16,32` and high `32,64,128` |
| The **`400000` / `1250000` split** | one `--max-docs` per sub-sweep |
| The **`c=32` overlap check** | "if the two sweeps disagree at 32 by more than the rep spread" |
| The **calibration rep that sized a budget** | replaced by `wall_s = max_docs / rate`, computed on the laptop |
| The **≥5 s floor as a thing to discover** | now a property of the grid, checkable before the boxes start |
| **"Any series still rising at c=128 is unresolved"** | becomes "unresolved above the top rate", same rule, new axis |

**One rule survives unchanged and catches people every time.** Both renderers
drop the first data row of every CSV by default, which on a five-rung ladder
would delete the 25,000 rung outright. **Every chart command here passes
`--keep-warmup`**, and a ladder must never be given a repeated first rung
without dropping that flag.

### N=3 repetitions of every ladder, minimum

And read trap 5 before trusting a single pass: on the concurrency ladder the
rising limb did not converge over three reps, with both boxes drawing more CPU
as the session went on. There is no reason to assume the pacer is immune to
whatever that was.

### Document size is part of the answer

The harness's cost is roughly **8.2 µs of CPU per document plus 2.3 ns per
corpus byte** (two-point fit, `results/fleet-rust-harness-null-sink-2026-09-10`).
Both terms bear on the producer thread, so **the pacing ceiling this runbook
measures is a ceiling at 3,948 B and does not transfer to another line length.**
Default to enwiki's mean corpus line, 3,948 B. Say the size beside every rate.

---

## Phase 1 — start the boxes

*Identical to the concurrency runbook's Phase 1.*

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

*Identical to the concurrency runbook's Phase 2.* Every stop wipes the instance
store, so this runs on **every** start.

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

**Public IPs are reassigned on every start; private IPs are not.**

```bash
# 1. re-point the SSH aliases
sed -i '/^Host fts-harness$/,/^$/ s/^    HostName .*/    HostName <new-harness-ip>/' ~/.ssh/config
sed -i '/^Host fts-sut$/,/^$/     s/^    HostName .*/    HostName <new-sut-ip>/'     ~/.ssh/config

# 2. these hosts are not in known_hosts under the new IP
ssh -o StrictHostKeyChecking=accept-new fts-harness true
ssh -o StrictHostKeyChecking=accept-new fts-sut     true

# 3. confirm the private IPs, do not assume them
ssh fts-harness hostname -I     # expect 172.31.38.237
ssh fts-sut     hostname -I     # expect 172.31.47.166  <- SINK below
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

# --- sink box: nothing but a directory ---
ssh fts-sut 'mkdir -p ~/sink-work'
```

### Build both harnesses and the instrument, then freeze them

**Build both halves now, in Phase 2.** The concurrency runbook defers the
OpenSearch build to B1; this one does not, because the bracket in Phase 5 needs
*both* halves' plateaus before the grid can be settled, and a rebuild between
the bracket and the measurement would make them incomparable.

```bash
cd <repo>/bench && tar czf - --exclude=target build-rate engine-mock opensearch \
  | ssh fts-harness 'mkdir -p /mnt/nvme/work && tar xzf - -C /mnt/nvme/work'

# Part A
ssh fts-harness 'cd /mnt/nvme/work/build-rate/scylla && . "$HOME/.cargo/env" \
  && CARGO_TARGET_DIR=/mnt/nvme/work/target cargo build --release --locked'

# Part B -- plain HTTP, so no default TLS feature
ssh fts-harness 'cd /mnt/nvme/work/build-rate/opensearch && . "$HOME/.cargo/env" \
  && CARGO_TARGET_DIR=/mnt/nvme/work/target-os \
     cargo build --release --locked --no-default-features'

# the instrument, on its own target dir so neither harness binary is disturbed
ssh fts-harness 'cd /mnt/nvme/work/engine-mock && . "$HOME/.cargo/env" \
  && CARGO_TARGET_DIR=/mnt/nvme/work/target-mock cargo build --release --locked'
```

**`bench/opensearch/` is in that tar and the build fails without it.** `osrate`
`include_str!`s `index-config-ramindex.json` and `index-config.json`, so those
files are needed at *compile* time.

Then put the mock on the sink box and check it runs there:

```bash
scp fts-harness:/mnt/nvme/work/target-mock/release/engine-mock /tmp/engine-mock
scp /tmp/engine-mock fts-sut:~/sink-work/engine-mock
ssh fts-sut 'chmod +x ~/sink-work/engine-mock && ~/sink-work/engine-mock --help | head -3'

ssh fts-harness 'sha256sum /mnt/nvme/work/target-mock/release/engine-mock'
ssh fts-sut     'sha256sum ~/sink-work/engine-mock'
```

The two checksums must match. Record both into `$R/env/` — **the mock's own
`--stats-out` JSON will report `git_commit: unknown`**, because the tar excludes
`.git`. That is expected, and the commit has to come from the laptop instead.

```bash
# one digest per crate tree
ssh fts-harness 'for tree in build-rate engine-mock; do
  printf "%s " "$tree"
  cd "/mnt/nvme/work/$tree" && find . -type f \
    \( -name "*.rs" -o -name "Cargo.*" \) | sort | xargs sha256sum | sha256sum
  cd - >/dev/null
done'
git -C <repo>/bench log -1 --format='%H %s'
git -C <repo>/bench status --short build-rate engine-mock opensearch
```

**Do not rebuild once an arm has run.** A mid-session rebuild of any of the
three makes the arms incomparable and nothing in the artifacts would say so.

**Verify the pacer is actually in this build before spending a box on it.** The
rate ladder is recent; a stale tree produces a binary whose `--target-rate` does
not exist and whose failure arrives after the corpus has been generated:

```bash
ssh fts-harness '/mnt/nvme/work/target/release/scyllarate --help | grep -A2 -- --target-rate'
ssh fts-harness '/mnt/nvme/work/target-os/release/osrate   --help | grep -A2 -- --target-rate'
```

Both must print the flag. If either does not, stop — the tree predates the rate
ladder and this runbook cannot run against it.

---

## Phase 3 — the instruments, and the rules that keep them honest

*Identical to the concurrency runbook's Phase 3.* Reproduced here because this
file is meant to be runnable on its own.

**Both sinks come up once, here, and stay up for the whole session.** The
sibling runbook starts the CQL mock now and the HTTP one at B2, because nothing
there needs both at the same time. **Phase 5A does:** the grid is shared, so it
cannot be settled until both halves have been bracketed, and neither half can be
measured until the grid is settled.

Bringing both up once is also the tidier answer. Each mock serves exactly its
own half's bracket and its own half's measurement, so **the two have symmetric
exposure** — the thing trap C2 asks for — and neither `--stats-out` file is
overwritten by a mid-session restart. An idle mock costs nothing; the CPU
sampler records both continuously and attributes by port.

**The pid comes from `pgrep`, not from `$!`.** The wrapper `setsid … &` records
exits, and a sampler pointed at its pid finds no `/proc` entry, skips the sink
every second, and leaves a file that reads as a sink that used no CPU — which
Phase 8 would classify `ok` on every rung.

```bash
ssh fts-sut 'cat > ~/start-sinks.sh << "EOF"
#!/bin/bash
# Bring up every sink this session needs, in one call, and record the pid that
# is actually serving each one. Arguments are mode:port pairs:
#   ~/start-sinks.sh cql:9042 http:9200
# A cql sink also serves the vector-store index-status endpoint that scyllarate
# gates every level on, at its port + 7000 so the two never collide. --mode cql
# would bring one up on 6080 unasked; the explicit port is what keeps N of them
# from colliding on it. --mode http gets none, because osrate reads nothing
# there and a fixed default would make a second HTTP mock fail to bind.
# NB: no apostrophes in this script -- it is delivered inside a single-quoted
# ssh argument, and one would close the quote.
cd ~/sink-work
: > /tmp/sinks.pids
for spec in "$@"; do
    mode="${spec%%:*}"; port="${spec##*:}"
    extra=""
    [ "$mode" = cql ] && extra="--vs-port $((port + 7000))"
    setsid ./engine-mock \
        --mode "$mode" --host 0.0.0.0 --port "$port" $extra \
        --label "rate-$mode-$port" --report-interval 30 \
        --stats-out "/tmp/sink-$port.json" \
        < /dev/null > "/tmp/sink-$port.log" 2>&1 &
    disown
done
# The pid of the process that is actually serving, not of the wrapper that
# started it. Wait for the readiness line rather than for a fixed sleep, so a
# mock that failed to bind is an error here and not a sampler file full of
# nothing.
for spec in "$@"; do
    mode="${spec%%:*}"; port="${spec##*:}"
    for _ in $(seq 1 100); do
        grep -q "engine mock ready" "/tmp/sink-$port.log" && break
        sleep 0.1
    done
    pid=$(pgrep -f "engine-mock --mode $mode --host 0.0.0.0 --port $port" | head -1)
    [ -n "$pid" ] || { echo "no engine-mock on $port -- see /tmp/sink-$port.log" >&2; exit 1; }
    echo "$port $pid" >> /tmp/sinks.pids
done
EOF
chmod +x ~/start-sinks.sh'

ssh fts-sut '~/start-sinks.sh cql:9042 http:9200'
ssh fts-sut 'cat /tmp/sinks.pids; ss -ltn | grep -E "9042|16042|9200"'
ssh fts-sut 'grep -h "engine mock ready" /tmp/sink-9*.log'
# engine mock ready: cql on 0.0.0.0:9042, vector-store wiki/articles_body_fts
#                    on 0.0.0.0:16042, 8 tokio workers
# engine mock ready: http on 0.0.0.0:9200, 8 tokio workers
```

Three ports must be listening: `9042` (CQL), `16042` (its vector store) and
`9200` (HTTP). Two pids must be in `/tmp/sinks.pids`. If either readiness line
is missing, stop — a sampler with nothing to point at is a whole campaign of
`?`.

Record that worker count. Phase 8's sink gate is a fraction of it.

**`VS_PORT` is CQL port + 7000 and the arm script must agree with the launcher.**
`scyllarate` gates every level on the vector-store status endpoint; a mismatch
here kills every ScyllaDB rung, and it is the defect the index-rate rehearsal
caught before it reached billed fleet time.

### Start the CPU samplers — both boxes

These are what make a number defensible. Without them a saturated rung has no
attribution and cannot be quoted.

```bash
# --- sink box: per-sink CPU at 1 Hz ---
ssh fts-sut 'cat > ~/sample-sinks-cpu.sh << "EOF"
#!/bin/bash
# 1 Hz CPU per sink: epoch, port, utime+stime ticks summed over all its threads.
# APPENDS. Never truncate: this file is the only CPU record for every arm in the
# session, both halves, and truncating destroys the ones already measured. These
# sinks are not replaced mid-session, but if one ever is, its generations are
# told apart by the tick counter resetting; a consumer drops negative deltas.
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

**The loader-box sampler earns its keep on this axis in a way it did not
before.** The producer-thread ceiling (see "The finding this runbook is most
likely to produce") shows up as **one core pinned while the box as a whole is
idle**. Whole-box CPU out of 8 is what makes that visible, and it is the
difference between "the harness ran out" and "one thread in the harness ran
out" — which are different findings with different fixes.

Both boxes are NTP-synced to well under a microsecond. Check it once:
`ssh fts-sut chronyc tracking | grep "System time"`.

### Killing things on these boxes

`pkill -f "engine-mock"` from inside an `ssh` one-liner **kills the ssh
session**, because the wrapper's own command line contains the pattern. Put any
`pkill` — and any `pgrep -f` — inside a script on the box and run the script.

---

## Phase 4 — the corpus

Synthetic, generated on the loader box. The client does not read the words: its
per-document cost is a function of size and shape only.

**Generate 3,500,000, matching `--max-docs`.** The corpus is the hard ceiling on
the budget, and on this axis the budget is a single campaign-wide constant that
every rung's wall clock is computed from — so a corpus short of it does not make
one level shorter, it makes **every rung's duration wrong** while the CSV still
looks plausible.

```bash
ssh fts-harness 'cat > ~/gen-corpus.sh << "EOF"
#!/bin/bash
set -e
DOCS="${1:-3500000}"; MEAN="${2:-3948}"; SIGMA="${3:-0.6}"
OUT="${4:-/mnt/nvme/work/corpus.jsonl}"
cd /mnt/nvme/work/gen
rm -rf /mnt/nvme/work/parts && mkdir -p /mnt/nvme/work/parts
python3.12 -m ftsbench.synth_corpus --output /mnt/nvme/work/parts/part.jsonl \
    --docs "$DOCS" --mean-bytes "$MEAN" --sigma "$SIGMA" --shards 8 \
    --stats-out "$OUT.stats.json"
cat /mnt/nvme/work/parts/part-*.jsonl > "$OUT"
wc -l "$OUT"; sha256sum "$OUT"
rm -rf /mnt/nvme/work/parts
echo GEN_DONE
EOF
chmod +x ~/gen-corpus.sh'

# ftsbench is needed on the LOADER box, for the corpus generator only
cd <repo>/bench && tar czf - --exclude=__pycache__ ftsbench \
  | ssh fts-harness 'mkdir -p /mnt/nvme/work/gen && tar xzf - -C /mnt/nvme/work/gen'

ssh fts-harness 'setsid ~/gen-corpus.sh 3500000 3948 0.6 </dev/null >/tmp/gen.log 2>&1 & disown'
# ~34 min for 3.5 M x 3,948 B (13.8 GB) on /mnt/nvme, extrapolated from the
# recorded ~24 min for 2.5 M. Poll for GEN_DONE; do not poll every few seconds.
```

Then **warm the page cache once**, so the reps are comparable and the first is
not measuring NVMe:

```bash
ssh fts-harness 'cat /mnt/nvme/work/corpus.jsonl > /dev/null'
ssh fts-harness 'wc -l /mnt/nvme/work/corpus.jsonl'   # must be >= 3500000
```

**That line count is a gate, not a note.** Check it before Phase 5.

**The page cache matters more on this axis.** The producer thread reads the
corpus inline, so a cold read is latency charged directly to the pacer and shows
up as `queue_p99_ms` — indistinguishable, in the CSV, from a producer that
cannot keep up. Warm it, and warm it once for the whole session.

Record documents, bytes, mean line and **sha256** into `$R/corpus/`. The
generator is deterministic given `--seed` (default 20260908).

---

## Phase 5 — run the arms

**Two phases, and the first one is on the other axis.** A concurrency ladder
finds a ceiling without knowing where it is; a rate ladder has to bracket one.
So the ceiling is found first, with the instrument that is correct for it.

| Phase | Ladder | Reps | Published | What it produces |
|---|---|---|---|---|
| **5A — the bracket** | `--concurrency 4,8,16,32,64,128` (closed loop) | 1 | **never** | each half's plateau and its p99 — what sets the grid and the cap |
| **5B — measurement** | `--target-rate <the grid>` (open loop) | 3 | yes | the chart, the fidelity verdict, the ceiling |

### The reader — write it before anything runs

An `awk` program does not survive an `ssh` one-liner: the remote shell expands
`$1` before `awk` ever sees it. This goes on the box, and it is used by both
phases and again in Phase 6.

```bash
ssh fts-harness 'cat > ~/rungs.sh << "EOF"
#!/bin/bash
# One line per rung, in the order that decides what the rung is worth.
# Columns: 18 target rate, 5 achieved, 19 ratio, 21 in_flight_peak,
#          20 queue_p99_ms, 7 p99_ms, 4 wall_s, 22 generator_saturated.
# A blank column 18 means this CSV is a closed-loop ladder -- 5A, not 5B.
awk -F, '"'"'!/^#/ && $1!="concurrency" {
    if ($18 == "") {
        printf "c=%-6s %11.0f docs/s  p99=%8.2fms  %7.1fs  %s\n",
               $1, $5, $7, $4, FILENAME
    } else {
        printf "offered=%-8s achieved=%11.0f  ratio=%-7s peak=%-7s queue_p99=%-9s p99=%-9s %7.1fs  sat=%-5s %s\n",
               $18, $5, $19, $21, $20, $7, $4, $22, FILENAME
    }
}'"'"' "$@"
EOF
chmod +x ~/rungs.sh'
```

### Phase 5A — the bracket, one rep per half

This is the sibling runbook's ladder, unchanged and unpublished. Its only job is
to hand Phase 5B two numbers per half: the **plateau** in docs/s and the **p99**
at it.

```bash
ssh fts-harness 'cat > ~/run-bracket.sh << "SCRIPT"
#!/bin/bash
# The closed-loop bracket. One rep, both halves, never published.
# NB: no apostrophes in this script -- it is delivered inside a single-quoted
# ssh argument, and one would close the quote.
set -u
HALF="$1"; shift
LADDER="${LADDER:-4,8,16,32,64,128}"
CORPUS="${CORPUS:-/mnt/nvme/work/corpus.jsonl}"
MAX_DOCS="${MAX_DOCS:-1250000}"
SINK="${SINK:-172.31.47.166}"
OUT_DIR="${OUT_DIR:-/mnt/nvme/work/results-bracket}"
mkdir -p "$OUT_DIR"
stamp() { awk "{ printf \"%d\t%s\n\", systime(), \$0; fflush() }"; }
csv="$OUT_DIR/bracket-$HALF.csv"
log="$OUT_DIR/bracket-$HALF.stderr.tsv"

if [ "$HALF" = scylla ]; then
    /mnt/nvme/work/target/release/scyllarate \
        --corpus "$CORPUS" --concurrency "$LADDER" --max-docs "$MAX_DOCS" \
        --hosts "$SINK" --port 9042 --vs-url "http://$SINK:16042" \
        --out "$csv" "$@" 2>&1 | stamp > "$log"
else
    ulimit -n 65536
    /mnt/nvme/work/target-os/release/osrate \
        --corpus "$CORPUS" --concurrency "$LADDER" --batch-size 1 \
        --max-docs "$MAX_DOCS" \
        --url "http://$SINK:9200" --index wiki-articles \
        --out "$csv" --no-analyzer-check "$@" 2>&1 | stamp > "$log"
fi
echo "exit=${PIPESTATUS[0]} -> $csv"
SCRIPT
chmod +x ~/run-bracket.sh'
```

**Both sinks are already up from Phase 3**, so the two halves run back to back
with nothing to swap between them:

```bash
ssh fts-harness '~/run-bracket.sh scylla'
ssh fts-harness '~/run-bracket.sh os'
ssh fts-harness '~/rungs.sh /mnt/nvme/work/results-bracket/bracket-*.csv'
```

They run **one at a time, never concurrently.** Each would otherwise be
measuring a loader box the other is also using, and the whole point of the
bracket is an uncontended plateau.

**`--max-docs 1250000` here, not 3,500,000.** The bracket is closed loop, so its
wall clock is not arithmetic and the campaign budget would make its slow rungs
enormous for a number nobody publishes. This is the one place in the runbook
where a different budget is correct, precisely because nothing from 5A is
plotted against anything from 5B.

Then settle three numbers and write them down before spending another minute:

| From the bracket | Feeds |
|---|---|
| `plateau` = the highest `docs_per_s` either half reached | the grid's top rung must be **above** the larger of the two |
| `plateau_slow` = the lower of the two halves' plateaus | the grid needs **two rungs below** this |
| `p99` at each half's plateau | `cap = 4 × top_rate × p99_seconds`, power of two, ≥512 |

**If the default grid already satisfies both rules, keep it.** Moving a grid
that works costs a re-read of everything written about it. If it does not, move
it by doubling or halving whole rungs — the geometric spacing is what makes the
chart's x readable.

**The bracket is not data and must not be plotted.** Its files are
`bracket-*` so the measurement globs (`rate-*`) cannot pick them up. Keep them in
`$R` anyway: they are how the grid and the cap are justified.

### Phase 5B — the rate ladder

```bash
ssh fts-harness 'cat > ~/run-rate-arm.sh << "SCRIPT"
#!/bin/bash
# One arm: the same offered-rate ladder, N times, against the sink on fts-sut.
#
# --target-rate is the ladder, so --concurrency is ONE number and it is a cap
# that must never bind. A rung whose in_flight_peak reached it measured the cap.
#
# stderr is timestamped per line. The tool announces each rung as it starts it,
# so the log carries the exact wall-clock window of every point and the sink CPU
# sampler on the other box can be cut to that window rather than to the whole
# sweep.
# NB: no apostrophes in this script -- it is delivered inside a single-quoted
# ssh argument, and one would close the quote.
set -u
ARM="$1"; shift
REPS="${REPS:-3}"
RATES="${RATES:-25000,50000,100000,200000,400000}"
CAP="${CAP:-4096}"
CORPUS="${CORPUS:-/mnt/nvme/work/corpus.jsonl}"
MAX_DOCS="${MAX_DOCS:-3500000}"
SINK="${SINK:-172.31.47.166}"
PORT="${PORT:-9042}"
# The launcher in Phase 3 puts the vector-store endpoint at CQL port + 7000.
# These two must agree or every rung fails its index gate.
VS_PORT="${VS_PORT:-$((PORT + 7000))}"
OUT_DIR="${OUT_DIR:-/mnt/nvme/work/results-rate}"
# The per-second series, deliberately NOT under $OUT_DIR: Phase 6 copies that
# directory into $R/points/ and its gate globs points/*.csv, which would read a
# series as a set of points.
SAMPLES_DIR="${SAMPLES_DIR:-/mnt/nvme/work/samples-rate}"
BIN=/mnt/nvme/work/target/release/scyllarate

mkdir -p "$OUT_DIR" "$SAMPLES_DIR"
WINDOWS="$OUT_DIR/run-windows.tsv"
[ -f "$WINDOWS" ] || printf "arm\trep\tstart_epoch\tend_epoch\texit_code\trates\tcap\tmax_docs\tcsv\n" > "$WINDOWS"
stamp() { awk "{ printf \"%d\t%s\n\", systime(), \$0; fflush() }"; }

for rep in $(seq 1 "$REPS"); do
    csv="$OUT_DIR/$ARM-rep$rep.csv"
    log="$OUT_DIR/$ARM-rep$rep.stderr.tsv"
    echo "######## arm=$ARM rep=$rep rates=$RATES cap=$CAP $(date -u +%H:%M:%S)"
    start=$(date +%s)
    "$BIN" --corpus "$CORPUS" --target-rate "$RATES" --concurrency "$CAP" \
           --max-docs "$MAX_DOCS" \
           --hosts "$SINK" --port "$PORT" \
           --vs-url "http://$SINK:$VS_PORT" \
           --out "$csv" --samples-dir "$SAMPLES_DIR/$ARM-rep$rep" \
           "$@" 2>&1 | stamp > "$log"
    code=${PIPESTATUS[0]}
    printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
        "$ARM" "$rep" "$start" "$(date +%s)" "$code" "$RATES" "$CAP" "$MAX_DOCS" "$csv" >> "$WINDOWS"
    ~/rungs.sh "$csv"
done
SCRIPT
chmod +x ~/run-rate-arm.sh'
```

One rep first — the ladder is long enough that a wrong cap is worth catching
before three of them:

```bash
ssh fts-harness 'REPS=1 CAP=<cap> ~/run-rate-arm.sh probe-scylla'
ssh fts-harness '~/rungs.sh /mnt/nvme/work/results-rate/probe-scylla-rep1.csv'
```

**Read `peak=` against the cap on every rung before committing.** If any rung's
`in_flight_peak` reached the cap, raise the cap and re-run the probe. That is
the one failure that invalidates a rung silently — the CSV is well-formed, the
ratio is honestly below 0.95, and the number means nothing about the client.

Then the matrix:

```bash
ssh fts-harness 'REPS=3 CAP=<cap> ~/run-rate-arm.sh rate-scylla'
```

Per rep: ~271 s of load at the default grid and budget, plus a reset per rung.
Run it in the background or with a generous timeout; **do not poll every few
seconds — it wastes the session.**

### There is no N-process arm

One `scyllarate` process against one sink, as in the sibling runbook, and here
the reason is sharper rather than merely inherited: **the producer thread this
campaign is trying to find the ceiling of is per process.** Running N processes
would multiply it and measure something no consumer has — the index-rate
campaign loads through a single process, so a single process is the ceiling that
bounds it.

If the ceiling turns out to be the producer and someone wants it raised, the fix
is in `core` — a producer that parses off the pacing thread — not more
processes. Say so in the write-up; that is the actionable form of the finding.

---

## Phase 6 — collect and verify Part A, BEFORE stopping the boxes

`$R` is the directory fixed at the start of the session. Do not recompute it.

**This phase does not stop the sinks.** Both mocks are serving until Part B is
done, and killing them here takes the CPU sampler with them. The single
SIGTERM-and-collect step lives at the end of B5, after both halves have run.
Everything else in this phase can and should run now, while Part A is fresh —
a gate that fails is cheaper to act on before the other half bills time.

```bash
test -n "$R" && test -d "$R" || { echo "R is unset: recover it with"; \
  echo '  export R="$(readlink -f bench/results/rate-harness-aws-latest)"'; }

scp 'fts-harness:/mnt/nvme/work/results-rate/*'     $R/scylla/points/
# the bracket ran both halves; each half's file goes to its own subtree, or the
# witness reconciliation below counts osrate documents against the CQL mock
scp 'fts-harness:/mnt/nvme/work/results-bracket/bracket-scylla*' $R/scylla/points/
scp 'fts-harness:/mnt/nvme/work/results-bracket/bracket-os*'     $R/opensearch/points/
scp -r 'fts-harness:/mnt/nvme/work/samples-rate/*'  $R/scylla/samples/
scp  fts-harness:/tmp/box-cpu.tsv                   $R/scylla/logs/
scp  fts-harness:/tmp/gen.log                       $R/corpus/
scp 'fts-harness:/mnt/nvme/work/*.stats.json'       $R/corpus/
scp 'fts-sut:/tmp/sink-9042.log'                    $R/scylla/sinks/
mv $R/scylla/points/*.stderr.tsv $R/scylla/logs/ 2>/dev/null
# the CPU series and the mock witnesses are collected once, in B5, after the
# single SIGTERM -- the series is still being appended to while Part B runs.
```

The mock's `--stats-out` JSON is written on SIGTERM and is collected in B5, with
the sink stop. Part A's `unexpected_requests` and `docs_accepted` reconciliation
therefore also runs there — it is the one gate in this phase that has to wait.


### Verification gate — all of it must pass before Phase 7

The first four are the sibling runbook's, unchanged. **The last four are this
axis's own, and they are the ones that decide whether a rung is a measurement.**

```bash
# 1. every ladder CSV has one row per rung
for f in $R/scylla/points/rate-*.csv; do
  echo "$(grep -vc '^#\|^concurrency' $f) $(basename $f)"; done

# 2. zero failed inserts anywhere
awk -F, '!/^#/ && $1!="concurrency" && $3+0>0 {print FILENAME": errors="$3}' \
  $R/scylla/points/*.csv

# 3. every arm left a series: one directory per rep, one CSV per rung
for d in $R/scylla/samples/*/; do echo "$(ls $d | wc -l) $(basename $d)"; done
awk -F, 'FNR==1 { rows=0 } !/^#/ && $1!="level" { rows++ } \
     ENDFILE { if (rows < 3) print FILENAME": "rows" readings" }' $R/scylla/samples/*/*.csv

# 4. the CPU samplers cover every run window
head -2 $R/scylla/logs/box-cpu.tsv; tail -1 $R/scylla/logs/box-cpu.tsv
cat $R/scylla/points/run-windows.tsv
```

```bash
# 5. THE CAP NEVER BOUND. in_flight_peak (col 21) against the cap in the
#    windows file. A rung at the cap is VOID -- not a finding, not a ceiling.
CAP=<the cap the arms ran at>
awk -F, -v cap="$CAP" '!/^#/ && $1!="concurrency" && $21+0 >= cap \
  {print "VOID "FILENAME": offered="$18" peak="$21" == cap -- re-run at a higher cap"}' \
  $R/scylla/points/rate-*.csv

# 6. THIS IS A RATE LADDER. target_docs_per_s (col 18) is populated on every
#    data row. A blank means a closed-loop CSV got into the measurement glob,
#    and the renderer will refuse the whole set.
awk -F, '!/^#/ && $1!="concurrency" && $18=="" \
  {print "BLAD "FILENAME": row has no target_docs_per_s -- this is a bracket CSV"}' \
  $R/scylla/points/rate-*.csv

# 7. THE LADDER IS THE ONE THAT WAS ASKED FOR. The set of offered rates is
#    identical in every rep and every arm -- a shared x is the whole basis on
#    which the halves are drawn together.
for f in $R/scylla/points/rate-*.csv; do
  printf "%s  %s\n" "$(awk -F, '!/^#/ && $1!="concurrency" {print $18}' $f | paste -sd,)" \
                    "$(basename $f)"; done | sort | uniq -c

# 8. latency_basis says intended_start. If it says service, the run was closed
#    loop and every p99 in it is a service time wearing a latency's name.
grep -h "latency_basis" $R/scylla/points/rate-*.csv | sort -u
```

Gates 5 and 6 are **blocking**. Gate 7 failing means the reps cannot be
aggregated and the arm is re-run. Gate 8 failing means the arm was not a rate
ladder at all.

**A note on the wall-clock gate the sibling has and this one does not.** There
is no "every point ran at least 3 s" check here, because `wall_s` is
`max_docs / rate` and was known before the boxes started. If a rung came back
much shorter than its arithmetic says, that is not a short point — it is the
corpus running out, and Phase 4's line-count gate is what catches it. Check it
that way round:

```bash
awk -F, -v md=3500000 '!/^#/ && $1!="concurrency" && $18>0 {
    want = md / $18; if ($4 < 0.8 * want)
      print "SHORT "FILENAME": offered="$18" wall="$4"s, arithmetic says "want"s -- corpus exhausted?"
}' $R/scylla/points/rate-*.csv
```

### Then reconcile against the instrument's own witness — in B5, once the sinks stop

The mock counted what it accepted, independently of what the loader believes it
sent. This is the cheapest real check in the runbook, and it is the one gate
here that cannot run yet: the JSON it reads is written on SIGTERM. **Run this
block in B5, against both halves' `sinks/` directories.** It is written out here
because it belongs to the verification gate, not because it runs now.

```bash
# 1. docs_accepted >= the sum of the CSVs' docs column (col 2).
python3 - "$R/scylla" << 'EOF'
import glob, json, sys, csv
root = sys.argv[1]
sent = 0
for path in glob.glob(f"{root}/points/*.csv"):
    for row in csv.reader(l for l in open(path) if not l.startswith(("#", "concurrency"))):
        sent += int(row[1])
seen = sum(json.load(open(p))["docs_accepted"] for p in glob.glob(f"{root}/sinks/*.json"))
print(f"csv docs={sent}  mock docs_accepted={seen}  delta={seen - sent}")
EOF

# 2. the CQL mock must have seen NO unexpected route.
python3 -c 'import json,glob,sys; [print(p, json.load(open(p))["unexpected_requests"]) for p in glob.glob(sys.argv[1])]' \
  "$R/scylla/sinks/*.json"
# expect: {}

# 3. index_adds_while_absent is 0.
python3 -c 'import json,glob,sys; [print(p, json.load(open(p))["index_adds_while_absent"]) for p in glob.glob(sys.argv[1])]' \
  "$R/scylla/sinks/*.json"
```

**Negative delta blocks the run** — documents the loader counted and the mock
never saw. Positive means the mock accepted documents no collected CSV accounts
for; find which. A small positive delta has happened before
(`results/harness-aws-runbook-2026-09-15T1740Z` returns `delta=20000`) and has
never been explained.

Once the boxes stop, `/mnt/nvme` is gone. Anything not copied is lost.

---

## Phase 7 — stop the boxes

Same console tab. Select both rows → **Instance state → Stop instance** → check
the dialog names **both** `k-nowacki-fts-benchmark-harness` and
`k-nowacki-fts-benchmark-sut`, leave "Skip OS shutdown" unchecked → **Stop**.

Then refresh and **confirm both rows read `Stopped` with no public IP**. Say so
explicitly in the report; "I initiated the stop" is not the same as "they are
stopped".

The `~/.ssh/config` entries now point at released IPs.

---

## Phase 8 — analyse and write up

Per rung, join the CSV row to what both boxes were doing over **that rung's own
window**, cut from the timestamped stderr log:

- `sink_cores` — the mock's whole-process CPU (all threads), median and peak
- `box_cores` — loader box CPU out of 8, median and peak
- `hottest_core` — whether any single core was pinned while the box was not

That third one is new and it is the producer-thread signature. A box at 1.1 of 8
cores with one of them at ~100% is a single-threaded ceiling; a box at 7 of 8 is
not.

### The decision table — what a rung is worth

**Read the columns in this order, and stop at the first row that matches.**
Getting the order wrong is how a harness limit gets published as an engine
ceiling.

| `achieved_offered_ratio` | `in_flight_peak` | Then | Verdict |
|---|---|---|---|
| **≥ 0.95** | below the cap | `queue_p99_ms` small beside `p99_ms` | **FAITHFUL.** The rung measured what its x says. |
| **≥ 0.95** | below the cap | `queue_p99_ms` a material fraction of `p99_ms` | **FAITHFUL ON AVERAGE ONLY.** The schedule held over the rung but not instant to instant. Usable for throughput, **not** for latency. |
| < 0.95 | **at the cap** | — | **VOID.** The cap bound. Re-run the rung at a higher cap. Never a ceiling of anything. |
| < 0.95 | **well below the cap** | `sink_cores` under budget | **THE PRODUCER IS THE CEILING.** Workers were starved: the single paced producer thread could not release documents fast enough. **This is the number this runbook exists to find.** |
| < 0.95 | **well below the cap** | `sink_cores` at budget | the mock gave way, not the client. Lower bound, write it `≥`. |
| < 0.95 | between | — | ambiguous. Report as such; do not pick. |

**`in_flight_peak` is the discriminator and neither neighbour substitutes for
it.** A sink that cannot keep up and a producer that cannot keep up both drive
`queue_p99_ms` up — the first because the channel fills and `hand_off` starts
polling, the second directly. What tells them apart is where the work is: a slow
sink leaves requests **outstanding** (peak climbs toward the cap), a slow
producer leaves workers **idle** (peak stays low while the rate falls short).

**`cut_short` is a hard stop, not a degraded rung.** A rung running at more than
`OVERRUN_FACTOR = 3.0` times its own schedule, after a 10 s grace, is abandoned
and the stderr log says so:

```
!! offered rate abandoned: 3x behind its own schedule, level cut short
```

It sets `generator_saturated` too, so the chart rings it, but the `wall_s` of an
abandoned rung is not `max_docs / rate` and must not be read as one.

### The sink's CPU budget

`engine-mock` runs a tokio task per connection across `tokio_workers` threads,
so the budget is **the cores this half can actually reach**:

```
sink_budget_cores = 0.85 x min(tokio_workers, connections the loader held)
```

| Half | `connections` | Where it comes from | Budget on an 8-core SUT |
|---|---|---|---|
| **A** `scyllarate` | **1** | the CSV header's `connections=` — one, because the mock advertises no shard extension and an empty `system.peers` (trap 1) | `0.85` cores |
| **B** `osrate` | the rung's `in_flight_peak`, **not** the cap | reqwest gives every in-flight `_bulk` its own socket | `0.85 x min(8, peak)` |

**On this axis Part B's denominator is `in_flight_peak`, not `concurrency`.**
Under a concurrency ladder those were the same number by construction. Under a
rate ladder `concurrency` is a cap the run is trying *not* to reach, so using it
would divide by 4096 and make the gate unfireable. `in_flight_peak` is how many
sockets the mock actually had.

Then classify every rung with a **three-state** gate, never pass/fail:

| | meaning |
|---|---|
| `ok` | a sink series exists and it stayed under `sink_budget_cores` |
| `SINK` | the sink reached ≥`sink_budget_cores` — the rung is a **lower bound** |
| `?` | **no sink series for this rung. Not a pass.** |

**A sink series of all zeros is a `?`, not an `ok`** — that is what a sampler
pointed at a wrapper pid produces, and it is the failure the `pgrep` in Phase 3
exists to prevent.

### The three numbers to write down

Everything above exists to produce these. Put them at the top of `$R/README.md`:

1. **The faithful-offer ceiling, per half**, as the highest grid rung that came
   back FAITHFUL, and the lowest that did not. Write it as a bracket — *"between
   200,000 and 400,000 docs/s at 3,948 B"* — not as a point the grid cannot
   resolve.
2. **What gave way**, from the decision table: the producer, the cap, or the
   mock. If both halves stopped at the same rate with peaks low and one core
   pinned, say plainly that the ceiling is `core`'s single paced producer and
   that it is shared by every rate-ladder campaign in this directory.
3. **The fidelity band** across all FAITHFUL rungs: the range of
   `achieved_offered_ratio` and the worst `queue_p99_ms` as a fraction of
   `p99_ms`. This is the sentence the index-rate campaign cites.

Then open `$R/README.md` with the run's own identity, so the directory explains
itself without reference to this file:

```markdown
# <one line: what was measured>

Run `rate-harness-aws-runbook-2026-09-16T0900Z`, produced by
`bench/build-rate/RATE-HARNESS-AWS-RUNBOOK.md`. Fleet up <HH:MM>-<HH:MM> UTC on <date>.
Offered-rate ladder <grid> at cap <cap>, --max-docs 3500000, N=3 per half.
Binary: crate commit <sha>. Instrument: engine-mock <sha256>.
**No number here is an engine number.**
```

Finally, hand the user the absolute path of `$R`.

---

# Part B — the OpenSearch harness (`osrate`), at `batch=1`

Same fleet, same session, same corpus, same samplers, same gates. Only the
deltas are written out here; anything not mentioned is unchanged from Part A.

**Do not stop the boxes between the parts.** Part B reuses the corpus and the
page cache Part A warmed, and the box CPU baseline is only comparable within one
session.

## B0 — what is different about this harness

`osrate` posts hand-built NDJSON to `POST /_bulk`. Two units, and mixing them is
the standing way to misread this half:

| Number | Unit |
|---|---|
| `concurrency` (here: the cap) | in-flight **`_bulk` requests**, not documents |
| `target_docs_per_s`, `docs`, `docs_per_s` | documents |
| `requests`, `failed_requests` | `_bulk` requests |
| `p50_ms`, `p99_ms` | **one `_bulk` request** — never one document |

**`--batch-size 1` only, and on this axis that is a smaller restriction than it
was.** On the concurrency ladder, `batch=1` was the only level whose x meant the
same thing on both halves — that was the entire argument for pinning it. **A
document per second already means the same thing on both halves at any batch
size**, so the rate axis does not need `batch=1` to be comparable.

It is pinned anyway, for two reasons that survive the axis change:

- **It is the shape the producer ceiling is hardest on.** One document per
  request is the most work per document the producer has to do downstream of
  its own pacing, so `batch=1` is where the shared-producer hypothesis is
  tested most directly against Part A.
- **It keeps this campaign one session.** A batch sweep is a ladder per level.

**If a batch level is ever added, it is a series and never an axis** — every
level runs the same *rate* grid and lands on one chart as its own line. That
rule is unchanged; only the grid it refers to has moved.

### `in_flight_peak` is the column that makes this half legible

`README.md` records the asymmetry: at one identical offered rate of 50,000
docs/s, `in_flight_peak` came out at **2** on the `osrate` side and **402** on
the `scyllarate` side. That was `osrate` at a large batch — two requests in
flight carry thousands of documents. **At `batch=1` the two should converge**,
since both then carry one document per request and face the same RTT. They are
measured on the same box pair at the same rates, so a persistent gap between
their peaks at equal offered rate is a real difference between the two clients'
request paths, and it is worth a line in the write-up either way.

## B1 — already built

`osrate` and `engine-mock` were both built in Phase 2, deliberately, so the
bracket could cover both halves before the grid was settled. **Nothing is built
here.** If a rebuild feels necessary, the arms already run are invalidated.

## B2 — the HTTP sink is already up

Phase 3 started `cql:9042` and `http:9200` together and Phase 5A has already
driven both. **Nothing is started here.** Verify and move on:

```bash
ssh fts-sut 'cat /tmp/sinks.pids; ss -ltn | grep 9200'
ssh fts-sut 'grep -h "engine mock ready" /tmp/sink-9200.log'
ssh fts-sut 'tail -3 /tmp/sinks-cpu.tsv'   # both ports, ticks rising
```

**If the CPU sampler is not running, Part B is a whole half of `?` in Phase 8's
gate** — which, on the half where that gate is the only thing between a sink
ceiling and a client number, is the run failing quietly:

```bash
ssh fts-sut 'pgrep -f sample-sinks-cpu >/dev/null || echo "SAMPLER DOWN"'
```

Restart it only if that prints `SAMPLER DOWN`; `sample-sinks-cpu.sh` appends, so
a restart costs nothing but the seconds it was off.

**Do not run `stop-sinks.sh` before Part B.** It kills both mocks and the
sampler, and it is Phase 6's step, not this one. Running it here loses the HTTP
mock's witness and every subsequent rung's sink series.

### Both halves reset per rung, exactly as before

`scyllarate` drops and rebuilds the keyspace before every rung; `osrate` deletes
and recreates the index before every rung. Leaving both defaults alone is what
keeps the halves' per-rung overhead comparable.

**On this axis the reset also costs wall clock that is not in the arithmetic.**
`wall_s = max_docs / rate` is the *load*; a `DELETE`, a `PUT` and two gate polls
sit outside it on every rung. That is why the cost table below adds a reset
allowance rather than quoting the arithmetic as the total.

**One probe still has to be suppressed.** A reset run sends `_analyze` once
before the first document, and that is the one route the mock does not answer —
it 404s, and unlike the header fields it does not degrade, it fails the run.
`engine-mock` refuses it deliberately and **records it**, so `POST
/<index>/_analyze` appearing in `unexpected_requests` is precisely the signal
that `--no-analyzer-check` was dropped. `RESET_FLAGS` overrides it for a run
against a real OpenSearch, where the analyzer check is what you want.

### The header will say `unknown`, and that is correct

The mock does not answer `GET /<index>/_settings` or `GET /<index>/_mapping`, so
expect `index_shards=unknown`, `refresh_interval=unknown`, `write_pool=unknown`.
**That is the mock being honest, not a fault.** Refusing those two routes is a
gate: a correct Part B run leaves `unexpected_requests` containing exactly those
two and nothing else. B5 checks it.

## B3 — file descriptors and memory, before you launch anything

**Descriptors first, because this is the failure that is new on this axis.**
`opensearch/src/client.rs` leaves reqwest's pool at its default, which is
unbounded per host: **N bulks in flight take N sockets.** The cap here is
4096 by default, and AL2023's soft `ulimit -n` is 1024. The arm script raises it
to 65536 in the same shell that execs the binary — **that is the only place it
can be raised**, since `ulimit` does not cross an `ssh` invocation.

```bash
ssh fts-harness 'ulimit -n; ulimit -Hn'   # expect a soft limit near 1024
```

If the hard limit is also low, raise it in `/etc/security/limits.conf` and open
a fresh session; a cap the descriptors cannot support is an `EMFILE` partway up
the ladder, reported as failed requests rather than as a setup fault.

**Then memory.** Read-ahead is `queue_depth * concurrency * batch_size`
documents at the shared default depth of 10:

```
10 * 4096 * 1 * 3948 bytes  ~  162 MB
```

Comfortable on 61 GiB. **Nothing sets the depth, and that is the point** —
`scyllarate` hardcodes it and has no flag, so leaving `osrate`'s flag off is
what keeps the two halves' read-ahead identical. It becomes a real constraint
only if a batch level is restored: at `batch=512` the same cap is ~83 GB and the
box has no swap, so an overshoot is an OOM kill. Check the product before any
arm that changes either factor.

```bash
ssh fts-harness 'while pgrep -x osrate >/dev/null; do \
  ps -o rss= -C osrate | awk "{s+=\$1} END {printf \"osrate RSS %.1f GB\n\", s/1048576}"; \
  sleep 5; done'
```

## B4 — the arm

```bash
ssh fts-harness 'cat > ~/run-os-rate-arm.sh << "SCRIPT"
#!/bin/bash
# One osrate arm: the offered-rate ladder at ONE batch size, N times, against
# the HTTP mock on fts-sut. The rates and the budget come from Part A, unchanged --
# a document per second is the same quantity on both halves and sharing the
# grid is the whole reason this axis was chosen.
# NB: no apostrophes in this script -- it is delivered inside a single-quoted
# ssh argument, and one would close the quote.
set -u
ARM="$1"; shift
REPS="${REPS:-3}"
RATES="${RATES:-25000,50000,100000,200000,400000}"
CAP="${CAP:-4096}"
BATCH="${BATCH:-1}"
CORPUS="${CORPUS:-/mnt/nvme/work/corpus.jsonl}"
MAX_DOCS="${MAX_DOCS:-3500000}"
SINK_URL="${SINK_URL:-http://172.31.47.166:9200}"
INDEX="${INDEX:-wiki-articles}"
# Reset stays ON, as on the ScyllaDB side. Only the analyzer probe is
# suppressed: it is the one route the sink does not answer, and it fails the
# run rather than degrading. Set RESET_FLAGS= empty against a real OpenSearch.
RESET_FLAGS="${RESET_FLAGS:---no-analyzer-check}"
OUT_DIR="${OUT_DIR:-/mnt/nvme/work/results-os-rate}"
BIN=/mnt/nvme/work/target-os/release/osrate

# One socket per in-flight bulk, and the cap is in the thousands. This must be
# raised in the shell that execs the binary; it does not cross an ssh.
ulimit -n 65536

mkdir -p "$OUT_DIR"
WINDOWS="$OUT_DIR/os-windows.tsv"
[ -f "$WINDOWS" ] || printf "arm\tbatch\trep\tstart_epoch\tend_epoch\texit_code\trates\tcap\tmax_docs\tcsv\n" > "$WINDOWS"
stamp() { awk "{ printf \"%d\t%s\n\", systime(), \$0; fflush() }"; }

for rep in $(seq 1 "$REPS"); do
    csv="$OUT_DIR/$ARM-b$BATCH-rep$rep.csv"
    log="$OUT_DIR/$ARM-b$BATCH-rep$rep.stderr.tsv"
    echo "######## arm=$ARM batch=$BATCH rep=$rep rates=$RATES cap=$CAP $(date -u +%H:%M:%S)"
    start=$(date +%s)
    "$BIN" --corpus "$CORPUS" --target-rate "$RATES" --concurrency "$CAP" \
           --batch-size "$BATCH" --max-docs "$MAX_DOCS" \
           --url "$SINK_URL" --index "$INDEX" --out "$csv" \
           $RESET_FLAGS "$@" 2>&1 | stamp > "$log"
    code=${PIPESTATUS[0]}
    printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
        "$ARM" "$BATCH" "$rep" "$start" "$(date +%s)" "$code" "$RATES" "$CAP" \
        "$MAX_DOCS" "$csv" >> "$WINDOWS"
    ~/rungs.sh "$csv"
done
SCRIPT
chmod +x ~/run-os-rate-arm.sh'
```

One rep first, same as Part A, and check `peak=` against the cap on every rung:

```bash
ssh fts-harness 'REPS=1 CAP=<cap> ~/run-os-rate-arm.sh probe-os'
ssh fts-harness '~/rungs.sh /mnt/nvme/work/results-os-rate/probe-os-b1-rep1.csv'
```

Then the matrix:

```bash
ssh fts-harness 'REPS=3 CAP=<cap> ~/run-os-rate-arm.sh rate-os'
```

**The grid and the budget are Part A's and are not negotiable per half.** On the
concurrency ladder that sharing had to be argued for, because a rung's document
count depended on the client's speed. Here `--max-docs` is a constant and the
rates are the axis, so **both halves push exactly the same documents at exactly
the same schedule** — which is as close to a controlled comparison as these two
clients get. If a session has to be shortened, cut **reps**, or drop the bottom
rung from **both** halves; never change one half's grid alone.

### Measuring searchability is a separate run

These arms measure a submit rate. `osrate` can also measure how fast documents
become *searchable* (`--index-watch`, with `--samples-dir`), and the script
forwards `"$@"` so it is a separate run rather than an edit:

```
ssh fts-harness 'REPS=3 CAP=<cap> RATES=25000,50000,100000 \
    ~/run-os-rate-arm.sh os-build --index-watch \
    --samples-dir /mnt/nvme/work/samples-os-rate/os-build'
```

Against `engine-mock` that column is gated by `--os-refresh-interval-ms`, left
at its default of 0 (publish immediately), so it is a zero reading rather than a
refresh policy. It is optional and outside the cost estimate below.

## B5 — collect, and stop the sinks

The point CSVs and the logs first, while the mocks are still running:

```bash
scp 'fts-harness:/mnt/nvme/work/results-os-rate/*'    $R/opensearch/points/
scp -r 'fts-harness:/mnt/nvme/work/samples-os-rate/*' $R/opensearch/samples/  # only with --index-watch
scp 'fts-sut:/tmp/sink-9200.log'                      $R/opensearch/sinks/
mv $R/opensearch/points/*.stderr.tsv                  $R/opensearch/logs/
mv $R/opensearch/points/os-windows.tsv                $R/opensearch/
```

**Now stop both sinks with SIGTERM**, which is what makes each write its
`--stats-out` JSON. This is the single stop for the whole session — Phase 6
deliberately left it here — so it must not run until both halves are done:

```bash
ssh fts-sut 'cat > ~/stop-sinks.sh << "EOF"
#!/bin/bash
# The one stop for the session. SIGTERM so each mock writes its --stats-out
# JSON; the sampler goes with them because nothing is left to sample.
pkill -f "sample-sinks-cpu"
while read -r port pid; do kill -TERM "$pid" 2>/dev/null; done < /tmp/sinks.pids
sleep 4
while read -r port pid; do
    [ -s "/tmp/sink-$port.json" ] || echo "no witness for $port" >&2
done < /tmp/sinks.pids
EOF
chmod +x ~/stop-sinks.sh; setsid ~/stop-sinks.sh </dev/null >/dev/null 2>&1'
```

`no witness for <port>` on either port blocks the run: without that JSON there
is no independent count to reconcile the CSVs against. Then take the witnesses,
the CPU series and the scripts — the series covers the whole session, so **both
halves get a copy** and each is cut to its own windows:

```bash
scp 'fts-sut:/tmp/sink-9042.json'  $R/scylla/sinks/
scp 'fts-sut:/tmp/sink-9200.json'  $R/opensearch/sinks/
scp  fts-sut:/tmp/sinks-cpu.tsv    $R/scylla/sinks/
scp  fts-sut:/tmp/sinks-cpu.tsv    $R/opensearch/sinks/
scp 'fts-harness:~/*.sh' 'fts-sut:~/*.sh' $R/scripts/
```

**Then run Phase 6's witness reconciliation, now, for both halves** — the block
under "Then reconcile against the instrument's own witness", once against
`$R/scylla` and once against `$R/opensearch`. The CQL mock must show `{}` for
`unexpected_requests`; the HTTP mock must show exactly the two header read-backs
checked below.

Run Phase 6's gates 1–8 against `$R/opensearch/points/rate-*.csv`, plus these
three:

```bash
# the batch_size COLUMN (8) agrees with the filename on every data row
for f in $R/opensearch/points/*.csv; do
  want=$(basename "$f" | sed 's/.*-b\([0-9]*\)-rep.*/\1/')
  got=$(awk -F, '!/^#/ && $1!="concurrency" {print $8}' "$f" | sort -u | paste -sd,)
  [ "$want" = "$got" ] && echo "  OK   $(basename $f) batch=$got" \
                       || echo "  BLAD $(basename $f) name=$want column=$got"
done

# no failed inserts (col 3) and no rejected requests (col 10) anywhere.
# On this axis a 429 is the sink shedding an offered rate it could not take --
# a finding, but one that makes the rung a sink measurement, not a client one.
# EMFILE shows up here too, which is what B3's ulimit exists to prevent.
awk -F, '!/^#/ && $1!="concurrency" && ($3+0>0 || $10+0>0) \
         {print FILENAME": offered="$18" errors="$3" failed_requests="$10}' \
  $R/opensearch/points/rate-*.csv

# the HTTP mock must have seen EXACTLY the two header read-backs it refuses.
python3 - "$R/opensearch/sinks" << 'EOF'
import glob, json, sys
expected = {"GET /wiki-articles/_settings", "GET /wiki-articles/_mapping"}
for path in glob.glob(f"{sys.argv[1]}/*.json"):
    seen = set(json.load(open(path))["unexpected_requests"])
    verdict = "OK" if seen == expected else "BLAD"
    print(f"  {verdict} {path}: extra={sorted(seen - expected)} missing={sorted(expected - seen)}")
EOF
```

Substitute the real index name if `INDEX` was changed from `wiki-articles`.

---

## Traps, all of them met in practice

The sibling runbook's traps apply here unless this list says otherwise. These
are the ones the axis change adds, changes or retires.

### New on this axis

**R1. A cap that binds turns a harness limit into a published ceiling.** This is
the single most expensive mistake available here, because the CSV is well-formed
and the ratio is honestly below 0.95. `in_flight_peak` against `--concurrency`
is the only thing that catches it. Phase 6 gate 5 is blocking for this reason.

**R2. The paced producer is one thread, and `--concurrency` does not help it.**
`fill_channel` runs on a single `spawn_blocking` thread and does the corpus read,
the `serde_json` deserialize and the pacing wait per document. Raising the cap
adds consumers. If the rate is short and `in_flight_peak` is low, **nothing about
the cap, the sink or the network is the problem** — see Phase 8's table.

**R3. Both halves share that producer, so a shared ceiling is a `core` finding,
not a coincidence.** Design the read around the comparison; it is the one result
that would change the crate.

**R4. A cold page cache is charged to the pacer.** The producer reads the corpus
inline, so an unwarmed corpus shows up as `queue_p99_ms` and is indistinguishable
in the CSV from a producer that cannot keep up. Phase 4 warms it once per
session; do not skip it and do not re-warm mid-campaign.

**R5. `tools/plot_harness_grid.py` will silently draw these CSVs wrong.** It
knows nothing about `target_docs_per_s` — grep it and see — so it puts every rung
at the same x, the cap, and produces a plausible chart of one stacked column.
**Use `charts/rate_vs_offered.py`**, which reads column 18 and refuses a
concurrency-ladder CSV by name.

**R6. `--target-rate` and a multi-level `--concurrency` are refused, and that is
a feature.** The error arrives before anything connects, so it costs a second
rather than a level. Do not "fix" it by looping the runbook over concurrencies —
that reconfounds the axis the rate ladder exists to disentangle.

**R7. `--max-docs` above the corpus makes every rung's arithmetic a lie.** On the
concurrency ladder that mistake shortened one level. Here `wall_s =
max_docs / rate` is how every duration in this file was computed, so a short
corpus makes all of them wrong at once while the CSV still looks clean. Phase 4
gates the line count; Phase 6's SHORT check catches what gets through.

**R8. `ulimit -n` does not cross an `ssh`.** It has to be raised inside the
script that execs `osrate`, which B4 does. A cap of 4096 against a 1024 soft
limit is `EMFILE` reported as failed requests.

**R9. An abandoned rung's `wall_s` is not `max_docs / rate`.** `cut_short` fires
at `3.0x` the schedule after a 10 s grace. Read the stderr log for
`offered rate abandoned` before treating any short rung as arithmetic.

**R10. `VS_PORT` must match the launcher's `--vs-port`.** Phase 3 puts it at CQL
port + 7000 and Phase 5's script computes the same. A mismatch fails every
ScyllaDB rung on its index gate — the defect the index-rate rehearsal caught
before it reached billed time.

### Carried over, unchanged

**C1. One connection is one task, and on the CQL half that is still the wall.**
The mock advertises neither the shard extension nor a populated `system.peers`,
so the driver opens **one** connection and reaches about one core however many
`tokio_workers` the mock was given. **Phase 8's sink budget for Part A is `0.85`
of ONE core, not of eight.** *On the HTTP half this is retired:* reqwest opens a
socket per in-flight bulk.

**C2. The Python sink degraded as it ran; whether `engine-mock` does is
unmeasured.** This runbook answers it by **symmetry rather than by restarting**:
both mocks come up once in Phase 3 and each serves only its own half's bracket
and its own half's measurement, so the two halves face equally-aged instruments
and the comparison that matters is not tilted. What that does **not** protect is
a comparison *within* a half — rep 3 meets an older mock than rep 1. If the reps
drift monotonically, suspect the mock before the client, and re-run rep 1 at the
end of the session to check. `docs_accepted` is cumulative across the whole
session, which is what the Phase 6 reconciliation relies on.

**C3. N=3 did not converge the rising limb on the concurrency ladder** — `c=64`
read 139.9k → 172.4k → 208.0k over three reps with both boxes drawing more CPU
as the session went on. Nothing establishes that the pacer is immune. Re-run one
arm late in the session and compare.

**C4. Samplers must append, and their files must survive a restart.** A sampler
that truncates destroys the record for every arm already measured.

**C5. `pkill -f` inside an ssh one-liner kills the ssh session.** Put it in a
script on the box.

**C6. The system `python3` on the loader box is 3.9 and cannot import
`ftsbench`.** Phase 4's generator calls `python3.12` explicitly.

**C7. Root volume free space is asymmetric.** Harness root 32 GiB, SUT root
~4 GB free. Build outputs and the corpus go on `/mnt/nvme`.

**C8. The working tree can move mid-session.** Freeze all three binaries, record
what they were built from.

**C9. `osrate`'s `p99` is per `_bulk` request.** At `batch=1` that is one
document; at any other batch size it is not, and plotting it beside
`scyllarate`'s per-document p99 is the standing mistake on this half.

**C10. `unknown` in an `osrate` header against the mock is correct.** Do not
chase it and do not repoint the run at a real OpenSearch to make it go away.

### Retired by this axis

**The `c=32` overlap check**, **the two sub-sweeps**, **the per-sweep budget**,
and **"a budget moves for every arm in that sweep"** — all closed-loop artifacts.
Phase 0 has the full table.

**"One concurrency cannot serve every batch level"** is also retired *as
written*: the knee moved with batch size because concurrency was the axis. A
rate is a rate at any batch size. What survives is that a batch level is a
**series** and runs the whole shared grid.

---

## Cost

Two `i8g.2xlarge` on-demand in `eu-north-1`.

| | wall |
|---|---|
| bring-up, toolchain, both harness builds, mock build + copy | ~21 min |
| corpus generation (3.5 M, once, serves both parts) | ~34 min |
| Phase 5A bracket (1 rep per half, closed loop) | ~6 min |
| Part A probe rep + N=3 at the default grid | ~18 min |
| Part B probe rep + N=3 at the default grid | ~18 min |
| collect, verify, stop | ~6 min |
| **both parts, one session** | **~1 h 40 min – 2 h** |

**The measurement rows are arithmetic plus an allowance, which is the one thing
this axis gives that the concurrency ladder could not.** One rep of the default
grid at `--max-docs 3500000` is 140+70+35+17.5+8.75 = **271 s of load**. Four
reps per half (one probe, three measured) is ~18 min, plus five resets per rep
outside that figure. **Both halves push identical documents on identical
schedules, so their load times are equal by construction** — any difference in
the observed wall clock is reset overhead and saturation, and is itself a
reading.

**The corpus row is the session's largest line item and it grew.** 3.5 M rather
than 2.5 M costs ~10 extra minutes once, and it is what makes `--max-docs` a
constant instead of a negotiation. Both of the two rows above it are smaller
than the sibling's because the grid is five rungs rather than seven across two
sub-sweeps.

**The floor rung is where the money is.** The 25,000 rung is 140 s of every 271,
i.e. ~52% of the ladder's load time, and it is the rung least likely to be near
anything interesting. If a session has to be shortened, drop it from **both**
halves before touching reps.

**Every row here is an estimate; none has been timed on the fleet.** Time them
and write the real numbers in.

Standing cost between sessions is the root EBS volumes, billed whether the
instances run or not.

---

# The charts — run these last, after the boxes are stopped

Nothing here touches AWS. If the chart cannot be produced from `$R` without an
ssh, something was not collected and Phase 6's gate was skipped.

## The deliverable — X is the offered rate

```
.venv/bin/python3 build-rate/charts/rate_vs_offered.py \
    --keep-warmup \
    --submitted-only \
    --scylla     "$R/scylla/points/rate-scylla-rep*.csv" \
    --opensearch "$R/opensearch/points/rate-os-b1-rep*.csv" \
    --output     "$R/rate-fidelity.png" \
    --table      "$R/rate-fidelity.csv" \
    --title      "Offered rate against achieved rate — harness against engine-mock" \
    --subtitle   "$RUN_ID · i8g.2xlarge · engine-mock · batch=1 · cap=<cap> · N=3"
```

Four flags, and three of them are load-bearing:

- **`--keep-warmup` is mandatory.** Without it the renderer drops the first data
  row of every CSV, which on a five-rung ladder deletes the 25,000 rung.
- **`--submitted-only` drops the dashed index-rate family.** Against
  `engine-mock --mode cql` the mock counts a document into its modelled index as
  it accepts it, so the two families lie on top of each other by construction
  and the second one carries no information. Run it **once without the flag** as
  a zero reading — any daylight between the families is the harness's own — then
  use the flag for the chart people read.
- **Do not pass `--no-diagonal`.** The `y=x` line *is* the measurement here.
  Fidelity is points sitting on it; the rate at which the line departs is the
  ceiling. A fidelity chart without its diagonal is a throughput chart.
- **The globs must exclude `bracket-*` and `probe-*`.** `rate-scylla-rep*` and
  `rate-os-b1-rep*` do that; `*-rep*` would not.

**A hollow ring marks a saturated rung** — `generator_saturated`, i.e. under 95%
of the offered rate delivered. Rung markers are rung verdicts: **a ring is a
finding, not a gap**, and Phase 8's table says which of the four findings it is.
A rung is the median of its repetitions and **a rung is rung-marked if *any* rep
saturated**, because a rate one rep could not sustain is not a rate the
configuration sustains.

`--table` writes the same numbers as CSV — series, offered rate, reps, median,
min, max, shortest wall, saturated, `in_flight_peak` — because **the chart is
for looking and the table is for reading.** The `in_flight_peak` column in that
table is what a reader checks R1 against without opening a CSV.

## How to read it

1. **The diagonal first, then anything else.** Every FAITHFUL rung sits on `y=x`
   by definition; the chart's content is entirely in **where each line leaves
   it** and how. A line that departs gradually is meeting a soft limit; one that
   falls off a cliff met a hard one.
2. **Then `in_flight_peak`, from the table.** A departure with the peak at the
   cap is **not on this chart's subject** — it is R1, and those rungs are void
   and re-run, not interpreted.
3. **Then the two lines against each other.** If both leave the diagonal at
   about the same x, the ceiling is the shared producer (R3). If they leave at
   different x, the difference is between the clients or their sinks, and the
   sink CPU column says which.
4. **Then sink CPU, for any line that departed alone.** Part A's budget is
   `0.85` of **one** core; Part B's is `0.85 x min(8, in_flight_peak)`. A
   departure with the mock at its budget is the instrument, and the rung is a
   lower bound written `≥`.

## What the axes do and do not say

- **X is a rate the client was *told* to produce, not a rate anything achieved.**
  That is the whole point: y is what happened, x is what was asked.
- **X means the same thing on both halves**, at any batch size, which
  concurrency never did. This is the axis's one structural advantage and it is
  worth stating on the image.
- **Nothing on it is an engine number.** `engine-mock` only.
- **Latency on these rungs is `intended_start`-based** and is not comparable to
  a `service`-based p99 from the sibling runbook's CSVs. The header says which.
- A point is the **median** of its repetitions; the bar is min..max.

## The growth chart, if `--samples-dir` was passed

```
.venv/bin/python3 build-rate/charts/rate_vs_index_size.py \
    --scylla   "$R/scylla/samples/rate-scylla-rep*/*.csv" \
    --output   "$R/build-growth.png" \
    --table    "$R/build-growth.csv" \
    --title    "Build rate as the index grows (rate ladder, engine-mock)" \
    --subtitle "$RUN_ID · i8g.2xlarge · engine-mock · N=3"
```

Under a rate ladder this chart answers something it could not before: **a paced
rung should be a flat line.** The client is offering a constant rate, so a
series that sags partway through is the rung failing to hold its schedule,
located in time rather than averaged into one `achieved_offered_ratio`. That is
the per-second view of the same evidence Phase 8's table reads from the point
CSV, and it is how a rung that saturated *late* is told from one that never got
up to rate at all.

## Then write it down

Put `rate-fidelity.png`, `rate-fidelity.csv` and the three numbers from Phase 8
at the top of `$R/README.md`, name the saturated rungs and their verdicts in the
caption, and hand the user the absolute path of `$R`.

**Then record the ceiling where the next campaign will look for it:**
`../TUNING.md` § "Per-process client ceilings", as a bracket, with the document
size, the box pair and the run id beside it. A ceiling nobody can find is a
ceiling the index-rate campaign will exceed without noticing.
