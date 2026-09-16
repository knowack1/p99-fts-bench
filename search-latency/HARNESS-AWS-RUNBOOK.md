# Harness-on-AWS runbook — what a search costs before the engine has done any of it

**Hand this file to Claude Code as the instruction and it runs the whole
campaign: starts the two AWS boxes, builds `scyllasearch`, `ossearch` and
`engine-mock`, measures the read harness against an engine that answers every
search instantly and matches nothing real, pulls every artifact home, stops the
boxes.** It is self-contained — every script it needs is inline below.

It is the read-path twin of
[`../build-rate/HARNESS-AWS-RUNBOOK.md`](../build-rate/HARNESS-AWS-RUNBOOK.md)
and it leans on it hard: the fleet, the re-entry, the samplers, the collection
gate and most of the traps are that file's and are referenced rather than
restated. What is different is the subject. There the instrument's job was to
get out of a **write** path; here it is to get out of a **read** path, and a
read path has a second number — the latency — that a null instrument can say
something about which a real engine never can.

| Part | Harness | Binary | Interfaces | Instrument |
|---|---|---|---|---|
| **A** | `bench/search-latency/scylla` | `scyllasearch` | `--interface cql`, `--interface vector-store` | `engine-mock --mode cql` (+ its vector-store port) |
| **B** | `bench/search-latency/opensearch` | `ossearch` | `_search` | `engine-mock --mode http` |

Run Part A first. It establishes the corpus, the query set, the samplers and the
box's CPU baseline that Part B is read against.

---

## Phase 0 — STOP. The instrument cannot answer a search yet

**`engine-mock` at HEAD serves no search route, and nothing below can run until
it does.** This is not a configuration gap; it is a feature the mock was never
asked for, because its only consumer so far was the write path. Checked against
`bench/engine-mock/src/` on 2026-09-16:

| Arm | What it sends | What the mock does today |
|---|---|---|
| `ossearch` | `POST /{index}/_search` | **404**, recorded in `unexpected_requests` |
| `scyllasearch --interface vector-store` | `POST /api/v1/indexes/{ks}/{idx}/bm25` | **404**, recorded in `unexpected_requests` — `vstore.rs` answers `/status` and `/api/v1/info` and nothing else |
| `scyllasearch --interface cql` | `SELECT … WHERE BM25(body,'q') > 0 …` | **an empty Rows result**, by `cql.rs`'s one-off-SELECT path |

The CQL arm is the dangerous one, because it *succeeds*. Every query comes back
with zero rows, so every cell reports a real service time, `hits_mean=0`,
`zero_hit_queries == queries`, and a non-zero exit code — a complete, plausible
matrix of the cost of finding nothing, which is precisely the reading
`search-latency`'s exit code exists to refuse. It would also break under
`--fetch-documents`, which asks the driver to deserialize `title` and `body` out
of a placeholder column, and under `--statement prepared`, which the mock
answers with a Void result.

`../README.md` says this outright: *"there is no sink that can stand in: an
accept-and-discard endpoint stores nothing, and a search against nothing is the
one answer this harness treats as a failure."* That sentence is about
`engine-mock` as it stands, and this runbook is the request to change it.

### What must land in `engine-mock` first

A **null search**: an answer of fixed, synthetic shape, produced without
matching, parsing or scoring anything. It is the read-path counterpart of
accept-and-discard, and the same rule governs it — *it must not be the thing the
benchmark ends up measuring.*

1. **`--search-hits N`** (default something small and non-zero, e.g. `10`),
   clamped at the request's own limit. Every search answers with `min(N, limit)`
   synthetic hits. Zero must remain *reachable* (`--search-hits 0`) because a
   run that wants to see the harness's zero-hit refusal fire is a test of the
   harness; it must not be the default, because then every arm below refuses.
2. **`POST /{index}/_search`** on the HTTP side: a `hits.hits` array of
   `min(N, size)` entries, `_source` present only when the request asked for it
   and carrying the same two fields (`title`, `body`) the CQL arm projects, with
   `track_total_hits` honoured by omitting the total rather than inventing one.
3. **`POST /api/v1/indexes/{ks}/{idx}/bm25`** on the vector-store side: the
   primary-key **column** shape `scyllasearch` counts by length, of
   `min(N, limit)` entries. Not a column count — `search.rs` on that arm reads
   the length of one column, and a composite key that reported `2` for any
   number of hits would put a constant where `queries_per_s` is computed from.
4. **CQL `SELECT … BM25(…)`**: a Rows result of `min(N, LIMIT)` rows over the
   statement's actual projection, so `--fetch-documents` deserializes rather
   than fails, and so a `PREPARE`+`EXECUTE` of the same statement returns Rows
   and not Void.
5. **A witness counter** — `searches_answered`, beside `docs_accepted` in the
   `--stats-out` JSON, per endpoint. Phase 6 reconciles against it, and without
   it this campaign has no independent record that the searches it thinks it
   sent ever arrived.
6. **The version strings do not change.** `2.19.0-null-sink`, `1.10.0-null-sink`,
   `6.2.0-null-sink` stay exactly as they are, and Phase 6 still gates on
   `-null-sink` appearing in every CSV header. A mock that answers searches is
   still a null sink and must still be impossible to mistake for an engine.

**The synthetic body must be sized deliberately and recorded.** With
`--fetch-documents` the reply carries text, and how much text is a property of
the instrument, not of the engine — so the mock must have a flag for it
(`--search-doc-bytes`, defaulting to the corpus's mean line) and its value must
reach the `--stats-out` JSON. A floor measured against 100-byte documents does
not transfer to a corpus of 3,948-byte ones.

**Do not work around any of this.** In particular: do not run the CQL arm
against the current mock and read its latencies "because the timings are real".
They are timings of a reply with no rows in it, the one shape the read path is
cheapest at, and the number would be quoted later as a floor it is not.

### The gate that says the change landed

Every arm below is checked by Phase 6 against exactly this, and it is the
cheapest real check in the file:

```
unexpected_requests on the CQL mock  == {}
unexpected_requests on the HTTP mock == exactly the two header read-backs
                                        (GET /{index}/_settings, GET /{index}/_mapping)
```

`POST /{index}/_search` or a `/bm25` path appearing in either is the Phase 0
change not being in the binary that ran — which is otherwise invisible, because
the harness would report those runs as failed cells with blank percentiles and a
non-zero exit, and a tired operator reads that as a flaky box.

---

## What this measures, and what it is not

The subject is **the read harness**, not an engine. Three things come out of it
that nothing else in `bench/` can produce, and the third is the reason to spend
a fleet session on it at all.

**1. The harness's own throughput ceiling — a floor, written `≥`.** How many
queries per second one `scyllasearch` or `ossearch` process can drive when the
answer costs the far end nothing. The engine campaign needs to know this is far
above anything a real engine returns, for the same reason `../build-rate` needs
its loader floor: a chart of engine latency measured by a saturated client is a
chart of the client.

**2. The harness's own service-time floor — also a floor, written `≤` on the
latency axis.** `p50`/`p90`/`p99` at zero engine cost is the client, the driver,
the kernel and 0.142 ms of private network, and it is **added to every number
the engine campaign reports**. A p99 of 0.9 ms measured here means a real
engine's measured 4 ms p99 is at best a 3.1 ms engine and at worst something
else entirely. Nobody has this number for the read path, and until they do every
low-latency claim in the deck carries an unquantified constant.

**3. Whether the closed loop is actually closed.** `HARNESS-PROMPT.md` states
the property and states how to falsify it: *at a fixed engine latency L,
`p50_ms` ≈ L at every concurrency and `queries_per_s` ≈ N/L; a queue shows up as
p50 growing with N.* `engine-mock --delay-ms L` is a fixed engine latency, and
this is the only place that test can be run **over a real network at fleet
concurrency** rather than against an in-process fake. The Python predecessor
failed exactly this check — p50=79 ms at c=64 where Little's law put it near
16 ms — and the Rust harness is asserted to have fixed it. Asserted, on
loopback, in a unit test. Arm L is that assertion meeting a 0.142 ms link and a
ladder to 128.

**What it is not.** Not an engine number. Not a relevance number — the mock
matches nothing, so `hits_mean` is a constant the mock was told to return and
`zero_hit_queries` is a check on the instrument rather than on the corpus. Not a
number about query *classes*: the mock does not parse, so the class axis here
measures only the bytes of the query text, which is Arm C's whole and only
purpose.

**No number from this runbook is an engine number and none belongs in the
deck.**

| File | Its job |
|---|---|
| `../build-rate/HARNESS-AWS-RUNBOOK.md` | the same idea for the **write** path; the fleet mechanics this file references |
| `../AWS-RUN-PLAN.md` | the **engine** campaign on AWS (C1–C8, real ScyllaDB + OpenSearch) |
| `../engine-mock/README.md` | the instrument: what it serves, what it deliberately refuses, what it does not model |
| `README.md` | the harness: the seam, the matrix, the seventeen columns, the index precondition |
| `HARNESS-PROMPT.md` | the specification, and the falsification table Arm L implements |
| `../TUNING.md` | where measured ceilings and floors get recorded |

---

## The fleet

Identical to the sibling runbook — same two boxes, same aliases, same key, same
0.142 ms private RTT, same "the instrument runs on the other box, always".

| Alias | Instance | Role |
|---|---|---|
| `fts-harness` | `i-08d8d2505e16683f7` · `k-nowacki-fts-benchmark-harness` | **the subject.** Runs `scyllasearch` / `ossearch`. |
| `fts-sut` | `i-0e3e4b6b02e654b7f` · `k-nowacki-fts-benchmark-sut` | **the instrument.** Runs `engine-mock`. |

Both `i8g.2xlarge` (8 vCPU Graviton4, 61 GiB, aarch64), `eu-north-1b`, Amazon
Linux 2023, user `ec2-user`, key `~/.ssh/KarolNowackiAws.pem`.

A loopback instrument would understate how much concurrency is needed to cover
latency, which on a **read** path is the entire shape of the chart. It matters
more here than it did next door.

**Access, as of 2026-09-16.** There is no AWS CLI credential on the laptop and
the Chrome console session is expired behind a federated sign-in. `aws login` in
the terminal is the better path than the console: there is no session to keep
alive, so the stop at the end cannot be lost. If the console is used instead,
click its refresh control every few minutes for the **whole** run — the run is
longer than the idle timeout, and an expired session means the boxes cannot be
stopped from that tab and bill until someone re-authenticates by hand.

`~/.ssh/config` pins stale IPs (`fts-harness` 16.171.42.149, `fts-sut`
16.170.250.1). Both boxes are stopped and will get new public IPs on start
unless those addresses are Elastic. Phase 2 re-points them.

---

## The results directory — fix it first, on the laptop

`/mnt/nvme` is destroyed on every stop, so the laptop is the only place results
survive. Create the directory before touching an instance:

```bash
# Run this ONCE, at the very start of the session, and keep the shell.
export RUN_ID="search-latency-aws-runbook-$(date -u +%Y-%m-%dT%H%MZ)"
export R="$HOME/Projects/Scylla/p99/bench/results/$RUN_ID"
mkdir -p "$R"/{env,corpus,queries,scripts}
mkdir -p "$R"/scylla/{points,latencies,logs,sinks}
mkdir -p "$R"/opensearch/{points,latencies,logs,sinks}
printf '%s\n' "$RUN_ID" > "$R/RUN_ID"
ln -sfn "$RUN_ID" "$(dirname "$R")/search-latency-aws-latest"
echo "results -> $R"
```

giving

```
bench/results/search-latency-aws-runbook-2026-09-16T0900Z/
├── RUN_ID  env/  corpus/  queries/  scripts/
├── scylla/      points/ latencies/ logs/ sinks/
└── opensearch/  points/ latencies/ logs/ sinks/
bench/results/search-latency-aws-latest -> search-latency-aws-runbook-2026-09-16T0900Z
```

**The two halves keep separate subtrees even though their CSVs *are*
interchangeable here.** Unlike `build-rate`, both binaries write the same
seventeen columns with the same unit — one latency is one search on every arm —
and columns 16 and 17 name the series, so a consumer may legitimately `cat` them
together. The subtrees exist so that a half that has to be re-run can be
replaced without disturbing the other, and so the per-half witness JSON sits
beside the CSVs it reconciles against.

**Capture `RUN_ID` once and reuse the variable.** If the shell is lost, recover
with `export R="$(readlink -f bench/results/search-latency-aws-latest)"` rather
than recomputing the timestamp. Report the absolute path of `$R` when the run
finishes.

---

## Phase 1 — start the boxes

Prefer the CLI, for the reason in "The fleet" above:

```bash
aws ec2 start-instances --region eu-north-1 \
  --instance-ids i-08d8d2505e16683f7 i-0e3e4b6b02e654b7f
aws ec2 wait instance-status-ok --region eu-north-1 \
  --instance-ids i-08d8d2505e16683f7 i-0e3e4b6b02e654b7f
aws ec2 describe-instances --region eu-north-1 \
  --instance-ids i-08d8d2505e16683f7 i-0e3e4b6b02e654b7f \
  --query 'Reservations[].Instances[].[InstanceId,PublicIpAddress,PrivateIpAddress]' \
  --output text
```

If only the console is available, use the sibling runbook's Phase 1 verbatim
(`https://eu-north-1.console.aws.amazon.com/ec2/home?region=eu-north-1#Instances:search=k-nowacki;v=3`,
do not type into the filter box) — and keep the tab alive.

---

## Phase 2 — fleet re-entry and build

Every stop wipes the instance store, so this runs on **every** start. The
re-entry itself — `mkfs.xfs` on `/dev/nvme0n1`, the rustup install, the
`growpart` note, the `ssh-keyscan` of the new IPs, reading the private IPs
rather than assuming them — is the sibling runbook's Phase 2 unchanged. Do that
first, then the part that is specific to this tree.

**The tar has to carry three directories, not two.** `search-latency` depends on
`build-rate` by path (`build-rate-core`, `scyllarate`, `osrate` — that is the
whole point of the tree: the loader that fills the index is the sibling's, so
"the index was complete before the first query" is one claim and not two
implementations of it), and `engine-mock` sits beside both.

```bash
cd <repo>/bench && tar czf - --exclude=target build-rate engine-mock search-latency \
  | ssh fts-harness 'mkdir -p /mnt/nvme/work && tar xzf - -C /mnt/nvme/work'

# the two read binaries, sharing one target dir -- they share every dependency
ssh fts-harness 'cd /mnt/nvme/work/search-latency/scylla && . "$HOME/.cargo/env" \
  && CARGO_TARGET_DIR=/mnt/nvme/work/target cargo build --release --locked'
ssh fts-harness 'cd /mnt/nvme/work/search-latency/opensearch && . "$HOME/.cargo/env" \
  && CARGO_TARGET_DIR=/mnt/nvme/work/target cargo build --release --locked'

# the instrument, on its own target dir so neither harness binary is disturbed
ssh fts-harness 'cd /mnt/nvme/work/engine-mock && . "$HOME/.cargo/env" \
  && CARGO_TARGET_DIR=/mnt/nvme/work/target-mock cargo build --release --locked'
```

Note the three `Cargo.lock` files are deliberate and `--locked` is not optional:
each binary's `build.rs` reads **its own** lock to stamp the linked driver
version into every CSV header, and a resolver run on the box would move a
recorded version without anything saying so.

Copy the mock to the SUT and check it runs there before anything depends on it:

```bash
scp fts-harness:/mnt/nvme/work/target-mock/release/engine-mock /tmp/engine-mock
scp /tmp/engine-mock fts-sut:~/sink-work/engine-mock
ssh fts-sut 'chmod +x ~/sink-work/engine-mock && ~/sink-work/engine-mock --help | head -3'
ssh fts-harness 'sha256sum /mnt/nvme/work/target-mock/release/engine-mock'
ssh fts-sut     'sha256sum ~/sink-work/engine-mock'
```

**The checksums must match, and the `--help` must show `--search-hits`.** That
second check is the cheapest possible confirmation that the Phase 0 change is in
the binary that was shipped, and it costs one line. Do it before the corpus, not
after — discovering it at the first arm costs the corpus build as well.

Record into `$R/env/`, before measuring: the per-tree source digests (all three
trees), the laptop's `git log -1` and `git status --short` over
`build-rate engine-mock search-latency`, both checksums, and the mock's
readiness line. The mock's own `--stats-out` will report `git_commit: unknown`
because the tar excludes `.git`; that is expected, and the commit has to come
from the laptop.

**Do not rebuild once an arm has run.** Same rule, same reason, as next door: a
mid-session rebuild of the instrument changes what every arm before it was
measured against and nothing in the artifacts would say so.

---

## Phase 3 — the instrument, and the samplers

### Start the mock

One mock, because there is one harness process at a time. The launcher is the
sibling's with two changes: the mode is parameterised, and `--search-hits` is
passed explicitly rather than left at its default, because it is a property of
every number the campaign produces and must appear in the witness JSON as a
value somebody chose.

```bash
ssh fts-sut 'cat > ~/start-mock.sh << "EOF"
#!/bin/bash
# One engine-mock, in one mode, on one port. --mode cql also brings up the
# vector-store endpoint; its port is given explicitly at CQL port + 7000 so the
# two can never collide and a second mock would need no other change here.
# NB: no apostrophes in this script -- it is delivered inside a single-quoted
# ssh argument, and one would close the quote.
set -u
MODE="$1"; PORT="$2"; HITS="${3:-10}"; DELAY="${4:-0}"
cd ~/sink-work
EXTRA=""
[ "$MODE" = "cql" ] && EXTRA="--vs-port $((PORT + 7000))"
setsid ./engine-mock \
    --mode "$MODE" --host 0.0.0.0 --port "$PORT" $EXTRA \
    --search-hits "$HITS" --delay-ms "$DELAY" \
    --label "searchlat-$MODE-$PORT-d$DELAY" --report-interval 30 \
    --stats-out "/tmp/mock-$MODE-$PORT-d$DELAY.json" \
    < /dev/null > "/tmp/mock-$MODE-$PORT-d$DELAY.log" 2>&1 &
disown
# Wait for the readiness line rather than for a fixed sleep, so a mock that
# failed to bind is an error here and not a sampler file full of nothing.
for _ in $(seq 1 100); do
    grep -q "engine mock ready" "/tmp/mock-$MODE-$PORT-d$DELAY.log" && break
    sleep 0.1
done
# The pid of the process that is serving, not of the wrapper that started it.
# setsid ... & echo $! records the wrapper, which exits, and a sampler pointed
# at a dead pid reports an instrument that used no CPU -- which reads as ok on
# every level.
pid=$(pgrep -f "engine-mock --mode $MODE --host 0.0.0.0 --port $PORT" | head -1)
[ -n "$pid" ] || { echo "no engine-mock on $PORT -- see the log" >&2; exit 1; }
printf "%s %s\n" "$PORT" "$pid" > /tmp/mock.pid
grep -h "engine mock ready" "/tmp/mock-$MODE-$PORT-d$DELAY.log"
EOF
chmod +x ~/start-mock.sh'

ssh fts-sut '~/start-mock.sh cql 9042 10 0'
ssh fts-sut 'cat /tmp/mock.pid; ss -ltn | grep -E "9042|16042"'
```

### Prove the ports are reachable from the harness box, before the first arm

A listener bound on the SUT is not the same as a port the harness can reach, and
the difference is a security group. On the second `-priv` pair added
2026-09-16 (`fts-harness-priv` 172.31.8.140, `fts-sut-priv` 172.31.13.225,
us-east-1, `KarolNowackiAwsPriv.pem`) TCP/22 works between the boxes and
**9042, 9200 and 16080 do not** — verified against a confirmed listener, so it
is the SG and not the engine. Opening it needs the console and there are no AWS
credentials on the laptop, so it is Karol's action and it blocks the campaign
rather than delaying it.

Check from the box that will do the driving, not from the laptop:

```bash
ssh fts-harness 'for p in 9042 16042; do
  timeout 3 bash -c "</dev/tcp/172.31.47.166/$p" && echo "$p open" || echo "$p BLOCKED"
done'
```

Both must read `open` before Phase 5, and `9200` must read `open` before Part B.
A `BLOCKED` here with a readiness line on the SUT is the security group; nothing
below can run until an inbound rule allows the harness's SG (or `172.31.0.0/16`)
on those ports.

Read the readiness line and **record the worker count** — Phase 8's CPU budget
is a fraction of it, and it is also in the `--stats-out` JSON:

```
engine mock ready: cql on 0.0.0.0:9042, vector-store wiki/articles_body_fts
                   on 0.0.0.0:16042, 8 tokio workers
```

### Stopping it — defined here because Phase 5 restarts it three times

```bash
ssh fts-sut 'cat > ~/stop-mock.sh << "EOF"
#!/bin/bash
# SIGTERM is how the mock is asked for its --stats-out JSON; it ends its
# connection tasks with the runtime rather than waiting for them to close, so
# the file is written even if the harness left a socket open. Give it the
# moment it needs to write and rename.
while read -r port pid; do kill -TERM "$pid" 2>/dev/null; done < /tmp/mock.pid
sleep 4
EOF
chmod +x ~/stop-mock.sh'
```

Each `--delay-ms` setting gets its own `--stats-out` file, because each restart
opens a new one. Collect all of them.

### The samplers

```bash
# --- SUT: the instrument's own CPU at 1 Hz ---
ssh fts-sut 'cat > ~/sample-mock-cpu.sh << "EOF"
#!/bin/bash
# epoch, port, utime+stime ticks summed over every thread in the group -- which
# is what Phase 8 budget is a fraction of, and needs no change for a
# multi-threaded mock. APPENDS. Never truncate: the mock is restarted between
# delay settings and truncating destroys the record for every arm before it.
# Mock generations are told apart by the tick counter resetting; a consumer
# drops negative deltas.
OUT="${1:-/tmp/mock-cpu.tsv}"
[ -s "$OUT" ] || printf "epoch\tport\tticks\n" > "$OUT"
while true; do
    now=$(date +%s)
    while read -r port pid; do
        [ -d "/proc/$pid" ] || continue
        printf "%s\t%s\t%s\n" "$now" "$port" "$(awk "{print \$14+\$15}" /proc/$pid/stat)" >> "$OUT"
    done < /tmp/mock.pid
    sleep 1
done
EOF
chmod +x ~/sample-mock-cpu.sh
setsid ~/sample-mock-cpu.sh /tmp/mock-cpu.tsv </dev/null >/dev/null 2>&1 & disown'
```

The harness box's whole-box sampler is the sibling runbook's `sample-box-cpu.sh`
verbatim — epoch, busy and total jiffies out of `/proc/stat`, also appending.

Both boxes are NTP-synced to well under a microsecond, so windows cut on the
harness's clock line up with samples taken on the SUT. Check once:
`ssh fts-sut chronyc tracking | grep "System time"`.

### One script does every restart, and it has to be a script

`/tmp/mock.pid` is rewritten on every mock restart, so the sampler has to be
restarted with it — a sampler left pointing at the old pid finds no `/proc`
entry, writes nothing, and leaves a gap Phase 8 must render `?` rather than
`ok`. And `pkill -f sample-mock-cpu` from inside an `ssh` one-liner **kills the
ssh session**, because the wrapper's own command line contains the pattern. Both
problems go away if the whole sequence lives in one script on the box, and every
restart below calls it:

```bash
ssh fts-sut 'cat > ~/restart-mock.sh << "EOF"
#!/bin/bash
# restart-mock.sh <mode> <port> <search-hits> <delay-ms>
# Stops the sampler, stops the mock (so it writes its witness JSON), starts the
# new one, points the sampler at the new pid. The pkill is safe here and only
# here: inside a script its own command line does not carry the pattern.
set -u
pkill -f sample-mock-cpu
~/stop-mock.sh
~/start-mock.sh "$1" "$2" "$3" "$4"
setsid ~/sample-mock-cpu.sh /tmp/mock-cpu.tsv </dev/null >/dev/null 2>&1 &
disown
EOF
chmod +x ~/restart-mock.sh'
```

The sampler appends, so a restart keeps every arm already measured; generations
are told apart by the tick counter resetting.

### Killing things on these boxes

`pkill -f "engine-mock"` from inside an `ssh` one-liner **kills the ssh
session**, because the wrapper's own command line contains the pattern. Same for
`pgrep -f`. Put both inside a script on the box, as above.

---

## Phase 4 — the corpus and the query set

Two files, and they deliberately do **not** come from the same place.

### The corpus is synthetic and small, and only has to satisfy the gate

The harness refuses to measure an index that does not hold exactly as many
documents as the corpus, so an index has to exist — but against a mock that
matches nothing, its **size changes no measured number**. The mock returns
`--search-hits` whatever the index holds. So the corpus is sized for the cheapest
build that still exercises the real bootstrap path, not for realism:

```bash
# ftsbench is needed on the LOADER box, for the corpus generator only
cd <repo>/bench && tar czf - --exclude=__pycache__ ftsbench \
  | ssh fts-harness 'mkdir -p /mnt/nvme/work/gen && tar xzf - -C /mnt/nvme/work/gen'

ssh fts-harness 'cd /mnt/nvme/work/gen && python3.12 -m ftsbench.synth_corpus \
  --output /mnt/nvme/work/parts/part.jsonl --docs 200000 --mean-bytes 3948 \
  --sigma 0.6 --shards 8 --stats-out /mnt/nvme/work/corpus.jsonl.stats.json \
  && cat /mnt/nvme/work/parts/part-*.jsonl > /mnt/nvme/work/corpus.jsonl \
  && rm -rf /mnt/nvme/work/parts && wc -l /mnt/nvme/work/corpus.jsonl \
  && sha256sum /mnt/nvme/work/corpus.jsonl'
ssh fts-harness 'cat /mnt/nvme/work/corpus.jsonl > /dev/null'   # warm page cache
```

200,000 documents at the sibling's 3,948 B mean is ~790 MB and roughly two
seconds of build against the mock. The system `python3` on the loader box is 3.9
and cannot import `ftsbench`; use `python3.12`.

### The query set is the real one, and that is the point

Copy `bench/data/queries.json` — **the frozen set the engine campaign uses** —
rather than generating one from the synthetic corpus:

```bash
scp <repo>/bench/data/queries.json fts-harness:/mnt/nvme/work/queries.json
cp  <repo>/bench/data/queries.json "$R/queries/"
```

The mock never matches, so the only property of a query that reaches a measured
number is **its bytes** — how much text the client formats, sends, and (on the
CQL arm) how much of a statement the coordinator would have to re-parse. Those
bytes have to be the campaign's real ones or the floor does not transfer to it.
The corpus, conversely, only has to make the gate pass.

**This mismatch is on purpose and must be named in the write-up.** The CSV
header records `corpus=` (synthetic) and `queries=` (the frozen set) separately,
so the artifact says so on its own; a reader who finds them disagreeing must
find this paragraph, not a bug.

Record into `$R/`: the corpus line count, byte count, mean line and sha256; the
query set's sha256 and its per-class distinct counts (`rare_term`,
`common_term`, `phrase`, `bool_and`, `bool_not`, `bool_mixed`, 200 each on the
frozen set) and the **mean query byte length per class**, which is the axis Arm
C is about.

---

# Part A — `scyllasearch`

## Phase 5 — the arms

```bash
ssh fts-harness 'cat > ~/run-search-arm.sh << "SCRIPT"
#!/bin/bash
# One arm of the read matrix: the same concurrency ladder, N times, against the
# mock on fts-sut.
#
# stderr is timestamped per line and the tool announces every cell as it starts
# it ("[k/n] concurrency=C class=X"), so the log carries each cell own
# wall-clock window and the instrument CPU sampler on the other box can be cut
# to that window rather than to the whole matrix. A cell whose mock sat at its
# CPU budget (Phase 8) is the instrument ceiling being reported as the harness
# ceiling, which is the one way this setup lies.
# NB: no apostrophes in this script -- it is delivered inside a single-quoted
# ssh argument, and one would close the quote.
set -u
ARM="$1"; shift
REPS="${REPS:-3}"
LADDER="${LADDER:-1,2,4,8,16,32,64,128}"
CLASSES="${CLASSES:-common_term}"
WARMUP="${WARMUP:-3}"
DURATION="${DURATION:-12}"
CORPUS="${CORPUS:-/mnt/nvme/work/corpus.jsonl}"
QUERIES="${QUERIES:-/mnt/nvme/work/queries.json}"
MAX_DOCS="${MAX_DOCS:-200000}"
SINK="${SINK:-172.31.47.166}"
PORT="${PORT:-9042}"
VS_PORT="${VS_PORT:-$((PORT + 7000))}"
OUT_DIR="${OUT_DIR:-/mnt/nvme/work/results}"
# Deliberately NOT under $OUT_DIR: Phase 6 globs points/*.csv, and a latency
# distribution read as a set of points is a silent corruption of the analysis.
LAT_DIR="${LAT_DIR:-/mnt/nvme/work/latencies}"
BIN=/mnt/nvme/work/target/release/scyllasearch

mkdir -p "$OUT_DIR" "$LAT_DIR"
WINDOWS="$OUT_DIR/run-windows.tsv"
[ -f "$WINDOWS" ] || printf "arm\trep\tstart_epoch\tend_epoch\texit_code\tladder\tclasses\tcsv\n" > "$WINDOWS"
stamp() { awk "{ printf \"%d\t%s\n\", systime(), \$0; fflush() }"; }

for rep in $(seq 1 "$REPS"); do
    csv="$OUT_DIR/$ARM-rep$rep.csv"
    log="$OUT_DIR/$ARM-rep$rep.stderr.tsv"
    echo "######## arm=$ARM rep=$rep $(date -u +%H:%M:%S)"
    start=$(date +%s)
    "$BIN" --corpus "$CORPUS" --queries "$QUERIES" \
           --query-classes "$CLASSES" --concurrency "$LADDER" \
           --warmup "$WARMUP" --duration "$DURATION" --max-docs "$MAX_DOCS" \
           --hosts "$SINK" --port "$PORT" \
           --vs-url "http://$SINK:$VS_PORT" \
           --out "$csv" --latencies-dir "$LAT_DIR/$ARM-rep$rep" \
           "$@" 2>&1 | stamp > "$log"
    code=${PIPESTATUS[0]}
    printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
        "$ARM" "$rep" "$start" "$(date +%s)" "$code" "$LADDER" "$CLASSES" "$csv" >> "$WINDOWS"
    grep -E "^[0-9]+	  ->" "$log" | tail -12
done
SCRIPT
chmod +x ~/run-search-arm.sh'
```

### The ladder, the cell length, and why one class

**Ladder `1,2,4,8,16,32,64,128`.** Wider than the write path's, and `c=1` is
mandatory rather than cheap: it is the only level at which the per-request cost
is unambiguous, it is the level Little's law is checked at, and — see Arm L — it
is the **only** level at which the closed-loop check is valid on the CQL arm.

**`--warmup 3 --duration 12`.** There is no cache to warm and no compaction to
settle; the warm-up here covers the connection pool, the tokio runtime and the
first-touch of the query cursor, and three seconds is generous for that. Twelve
seconds is chosen against the p99: at c=1 and a sub-millisecond floor that is
tens of thousands of samples per cell, which is far more than a p99 needs, and
at c=128 it is millions. Anything shorter starts to be dominated by the ladder's
own step overhead; anything longer buys nothing.

**One query class per ladder arm — `common_term`.** Against a mock that does not
parse, all six classes differ only in the bytes of the query text, so running
six of them multiplies every arm by six and measures one thing. The class axis
gets its own arm instead, at one concurrency, where it is the whole subject.
`common_term` is the middle of the length distribution; record which class was
used, because the floor is a function of it.

Per rep: 8 levels × 15 s ≈ 2 min, plus the bootstrap. N=3 ≈ 6 min an arm.

### The index is built once, and then must not be rebuilt

The first `scyllasearch` run finds an empty mock, resets, and fills it with
`scyllarate`'s loader at `--load-concurrency`. Every later run reads a count
that already matches the corpus and **skips the build**. So:

- **Run the arms back to back against one mock generation.** A restart zeroes
  the modelled index and costs every subsequent arm a rebuild.
- **Never pass `--rebuild-index`** in an arm. It is for recovering from a mock
  restart, and it is a write the measured matrix does not need.
- **`--no-index-build` is the right flag for every arm after the first**, if the
  campaign wants the guarantee in writing: it turns any unexpected rebuild into
  a refusal instead of a silent two-second write in the middle of a read
  measurement. Use it, and treat a refusal as "the mock was restarted", not as a
  harness bug.

The sibling's trap 2 — restart the instrument between arms whose numbers will be
compared — **does not apply here and is deliberately inverted.** There the
instrument held no state a run depended on; here it holds the index the run
refuses to proceed without.

### Arm A1 — the floor, `--interface cql`

```bash
ssh fts-sut  '~/start-mock.sh cql 9042 10 0'
ssh fts-harness 'REPS=3 ~/run-search-arm.sh a1-cql-floor --interface cql'
```

This is the first arm, so it builds the index. Watch its stderr for the build
line and the `index already holds all 200000 documents` on rep 2 — if rep 2
rebuilds, the mock restarted and the run is incomparable.

Add `--no-index-build` from Arm A2 onward.

### Arm A2 — the floor, `--interface vector-store`

```bash
ssh fts-harness 'REPS=3 ~/run-search-arm.sh a2-vs-floor --interface vector-store --no-index-build'
```

`cql` minus `vector-store` is ScyllaDB's own read overhead, and it is the only
reason the second interface exists — but **against the mock that subtraction is
not that**. It is the CQL driver's cost minus reqwest's cost, over the same
link, against the same process. That is still worth having: it is the part of
the real subtraction that is the client's, and the engine campaign's version of
it is the sum of the two. Say which one a number is.

Neither arm passes `--fetch-documents`, and the vector-store arm would refuse it
on its flags. See Arm A4.

### Arm A3 — the closed loop, `--delay-ms 2`

**This is the arm the runbook exists for.** Restart the mock with a fixed
2 ms answer delay — ~10× any plausible floor, small enough that the ladder still
finishes — then run the same two interfaces:

```bash
ssh fts-sut     '~/restart-mock.sh cql 9042 10 2'
ssh fts-harness 'REPS=1 ~/run-search-arm.sh a3-vs-delay2 --interface vector-store'
ssh fts-harness 'REPS=1 LADDER=1 ~/run-search-arm.sh a3-cql-delay2-c1 --interface cql --no-index-build'
```

The mock restart zeroes the index, so `a3-vs-delay2` rebuilds — that is why it
runs first and without `--no-index-build`, and why the CQL half of this arm
follows it.

**Read it against two predictions, per level:**

| | expected | what a deviation means |
|---|---|---|
| `p50_ms` | ≈ 2 ms + the Arm A2 floor at that level, **flat across the ladder** | p50 rising with N is a queue between the workers and the engine — the property `HARNESS-PROMPT.md` asserts does not exist |
| `queries_per_s` | ≈ N / (2 ms + floor) | a throughput below that with a flat p50 is the client not keeping N in flight |

**The CQL arm of this check is valid at `c=1` only, and that is a property of
the instrument, not of the harness.** `engine-mock` sleeps `--delay-ms` once per
socket **read**, not once per request (`conn.rs`: the delay is applied before the
one write that answers everything a read carried). On the vector-store and
OpenSearch arms reqwest gives every in-flight request its own socket, so a read
carries one request and per-read is per-request. On the CQL arm the driver opens
**one** connection — the mock advertises no shard extension and an empty
`system.peers` — so at `c>1` a single read carries several frames, one sleep
covers all of them, and the effective per-request delay is below 2 ms by an
amount that depends on how the frames happened to coalesce. p50 below 2 ms there
is the mock's delay model, not a fast client, and it must not be reported as
either.

If the CQL closed-loop check is ever wanted across the ladder, the change is to
the instrument: a delay applied per answered request rather than per read. That
is a change to a frozen instrument and needs a decision, not a drive-by patch —
the same standing as trap 3 next door.

### Arm A4 — the class axis and the projection, one level each

Two small arms at one concurrency, N=1, which exist to bound two constants
rather than to draw a curve:

```bash
ssh fts-sut     '~/restart-mock.sh cql 9042 10 0'
ssh fts-harness 'REPS=1 LADDER=32 CLASSES=rare_term,common_term,phrase,bool_and,bool_not,bool_mixed \
                 ~/run-search-arm.sh a4-classes --interface cql'
ssh fts-harness 'REPS=1 LADDER=32 ~/run-search-arm.sh a4-fetch --interface cql --fetch-documents --no-index-build'
```

- **`a4-classes`.** The mock does not parse, so the six classes must agree
  within noise. Whatever spread there **is** is the harness's own cost as a
  function of query text — formatting, the wire, and on the CQL arm the
  statement the coordinator would re-parse — and it is the ceiling on how much
  of a real class spread can be attributed to the engine. Plot it against the
  mean query byte length recorded in Phase 4; if it is not roughly linear in
  bytes, something other than text size is class-sensitive and that is a finding.
- **`a4-fetch`.** `--fetch-documents` against `--search-hits 10` and
  `--search-doc-bytes <corpus mean>` is the cost of pulling ten synthetic
  documents back and deserializing them, which is the client's share of what the
  engine campaign's `--fetch-documents` runs measure. Refused by
  `--interface vector-store` on its flags, before it connects — confirm that
  refusal once, as a check that the binary is the one this file describes:
  `scyllasearch --interface vector-store --fetch-documents` must fail without
  connecting.

### Arm A5 — the instrument's own refusal still works

One command, ten seconds, and it is the only proof in the campaign that the
zero-hit gate is still armed:

```bash
ssh fts-sut     '~/restart-mock.sh cql 9042 0 0'   # --search-hits 0
ssh fts-harness 'REPS=1 LADDER=4 ~/run-search-arm.sh a5-zerohit --interface cql; echo "exit=$?"'
ssh fts-sut     '~/restart-mock.sh cql 9042 10 0'
```

Expected: `zero_hit_queries == queries` in every row, the
`!! every query in … matched nothing` warning on stderr, and a **non-zero exit
code**. A zero exit here means the gate that stops the whole campaign reporting
the cost of finding nothing is not working, and every other arm's exit code has
been meaningless. Keep `a5-*` out of the analysis globs; it is a self-test, not
data.

---

## Phase 6 — collect and verify, BEFORE stopping the boxes

```bash
test -n "$R" && test -d "$R" || { echo "R is unset: recover it with"; \
  echo '  export R="$(readlink -f bench/results/search-latency-aws-latest)"'; }

scp 'fts-harness:/mnt/nvme/work/results/*'     $R/scylla/points/
scp -r 'fts-harness:/mnt/nvme/work/latencies/*' $R/scylla/latencies/
scp  fts-harness:/tmp/box-cpu.tsv              $R/scylla/logs/
scp 'fts-harness:/mnt/nvme/work/*.stats.json'  $R/corpus/
scp  fts-sut:/tmp/mock-cpu.tsv                 $R/scylla/sinks/
scp 'fts-sut:/tmp/mock-cql-*.log'              $R/scylla/sinks/
scp 'fts-harness:~/*.sh' 'fts-sut:~/*.sh'      $R/scripts/
mv $R/scylla/points/*.stderr.tsv $R/scylla/logs/ 2>/dev/null
```

Stop the sampler and the mock — the latter with the `~/stop-mock.sh` defined in
Phase 3, so it writes its `--stats-out` JSON — then take every one of those
files, one per delay setting:

```bash
ssh fts-sut 'cat > ~/stop-all.sh << "EOF"
#!/bin/bash
pkill -f sample-mock-cpu
~/stop-mock.sh
EOF
chmod +x ~/stop-all.sh'
ssh fts-sut 'setsid ~/stop-all.sh </dev/null >/dev/null 2>&1'
ssh fts-sut 'ls -l /tmp/mock-cql-*.json'     # one per delay setting; none may be empty
scp 'fts-sut:/tmp/mock-cql-*.json' $R/scylla/sinks/
```

### The gate — all of it must pass before Phase 7

```bash
# 1. every CSV has one row per (level x class), and the header names a null sink
for f in $R/scylla/points/*.csv; do
  echo "$(grep -vc '^#\|^concurrency' $f) rows  $(basename $f)"
done
grep -L 'null-sink' $R/scylla/points/*.csv     # must print nothing

# 2. no failed queries anywhere except the a5 self-test (errors is col 4)
awk -F, '!/^#/ && $1!="concurrency" && $4+0>0 {print FILENAME": errors="$4}' \
  $R/scylla/points/*.csv | grep -v a5-

# 3. no blank percentiles. A blank p50 is a cell that measured nothing, and it
#    is what a missing search route looks like from this side.
awk -F, '!/^#/ && $1!="concurrency" && ($7=="" || $9=="") {print FILENAME" c="$1" "$2}' \
  $R/scylla/points/*.csv

# 4. hits_mean is the constant the mock was told to return, on every row
#    except a5. Anything else means the mock is matching, which it cannot be.
awk -F, '!/^#/ && $1!="concurrency" {print $11}' $R/scylla/points/*.csv | sort -u

# 5. every cell left a distribution, and none is a header with nothing under it
for d in $R/scylla/latencies/*/; do echo "$(ls $d | wc -l) $(basename $d)"; done
awk 'ENDFILE { if (FNR < 100) print FILENAME": "FNR" samples" }' \
  $R/scylla/latencies/*/*.csv

# 6. the CPU samplers cover every run window
head -2 $R/scylla/sinks/mock-cpu.tsv; tail -1 $R/scylla/sinks/mock-cpu.tsv
cat $R/scylla/points/run-windows.tsv
```

### Then reconcile against the instrument's own witness

This is the check the Phase 0 change exists to make possible, and the one that
catches a campaign that measured nothing:

```bash
# A. the mock must have seen NO unexpected route on the CQL side.
python3 -c 'import json,glob,sys; [print(p, json.load(open(p))["unexpected_requests"]) for p in glob.glob(sys.argv[1])]' \
  "$R/scylla/sinks/*.json"
# expect: {} on every file.
# A "/bm25" or "_search" here is the Phase 0 change NOT in the binary that ran.

# B. searches_answered >= the sum of the CSVs' queries column (col 3).
python3 - "$R/scylla" << 'EOF'
import csv, glob, json, sys
root = sys.argv[1]
asked = 0
for path in glob.glob(f"{root}/points/*.csv"):
    for row in csv.reader(l for l in open(path) if not l.startswith(("#", "concurrency"))):
        asked += int(row[2])
seen = sum(json.load(open(p)).get("searches_answered", 0) for p in glob.glob(f"{root}/sinks/*.json"))
print(f"csv queries={asked}  mock searches_answered={seen}  delta={seen - asked}")
EOF

# C. index_adds_while_absent is 0, and docs_accepted is corpus x (number of builds)
python3 -c 'import json,glob,sys; [print(p, json.load(open(p))["index_adds_while_absent"], json.load(open(p))["docs_accepted"]) for p in glob.glob(sys.argv[1])]' \
  "$R/scylla/sinks/*.json"
```

**The delta in B is expected to be positive, and by a predictable amount.** The
CSV counts only the measured window; the mock also answered every warm-up. With
`--warmup 3 --duration 12` the excess should be ≈ 3/12 = 25% of the measured
total, plus the handful of bootstrap probes. A delta near zero means warm-ups
did not reach the mock; a **negative** delta is searches the harness counted and
the instrument never saw, and it blocks the run.

Once the boxes stop, `/mnt/nvme` is gone. Anything not copied is lost.

---

## Phase 7 — stop the boxes

```bash
aws ec2 stop-instances --region eu-north-1 \
  --instance-ids i-08d8d2505e16683f7 i-0e3e4b6b02e654b7f
aws ec2 wait instance-stopped --region eu-north-1 \
  --instance-ids i-08d8d2505e16683f7 i-0e3e4b6b02e654b7f
aws ec2 describe-instances --region eu-north-1 \
  --instance-ids i-08d8d2505e16683f7 i-0e3e4b6b02e654b7f \
  --query 'Reservations[].Instances[].[InstanceId,State.Name,PublicIpAddress]' --output text
```

**Confirm both read `stopped` with no public IP, and say so explicitly in the
report.** "I initiated the stop" is not the same as "they are stopped". The
`~/.ssh/config` entries now point at released IPs and must be re-pointed on the
next start.

---

## Phase 8 — analyse and write up

Per measured cell, join the CSV row to what both boxes were doing over **that
cell's own window**, cut from the timestamped stderr log using the
`[k/n] concurrency=C class=X` announcement that opens it:

- `mock_cores` — the instrument's whole-process CPU (all threads), median and peak
- `box_cores` — harness box CPU out of 8, median and peak

### The instrument's CPU budget, per interface

Same arithmetic as next door, and the same reason neither end of the `min` can
be dropped:

```
mock_budget_cores = 0.85 x min(tokio_workers, connections the harness held)
```

| Arm | `connections` | Where it comes from | Budget on an 8-core SUT |
|---|---|---|---|
| `--interface cql` | **1** | the CSV header's `connections=` — one, because the mock advertises no shard extension and an empty `system.peers` | `0.85` cores |
| `--interface vector-store` | the level's `concurrency` | reqwest gives every in-flight request its own socket | `0.85 x min(8, c)` |
| `ossearch` | the level's `concurrency` | same | `0.85 x min(8, c)` |

Then classify every cell with a **three-state** gate, never pass/fail:

| | meaning |
|---|---|
| `ok` | an instrument series exists and it stayed under `mock_budget_cores` |
| `MOCK` | the mock reached ≥`mock_budget_cores` — the cell is a **bound**, not a measurement: its q/s is a lower bound and its latency an upper one |
| `?` | **no instrument series for this cell. Not a pass.** |

An unmeasured gate must never render as a passed gate, the same way an
unmeasured latency is a blank cell and never `0`. **An instrument series of all
zeros is a `?`, not an `ok`** — that is what a sampler pointed at the wrapper pid
produces, and it is the failure the `pgrep` in Phase 3 exists to prevent.

### What goes in `../TUNING.md`

Three numbers, each with the arm and the run that produced it, and each with its
direction of inequality written down:

- **`≥ q/s`** per interface, at the level where the gate still reads `ok` — the
  read harness's throughput floor.
- **`≤ p50 / p90 / p99 ms`** per interface at `c=1` — the constant added to every
  engine latency the campaign reports. This is the number the deck's low-latency
  claims are currently carrying unquantified.
- **The closed-loop verdict**, as a sentence and not a number: at `--delay-ms 2`,
  whether p50 stayed flat across the ladder, and up to which concurrency.

The floors are properties of this instance pair, this AZ, this query set's byte
lengths and this `--search-hits`. They do not transfer to a different corpus, a
different query set or a different box pair, and the write-up must say so beside
them.

### The write-up

Write a `README.md` in `$R` carrying the topology and RTT, the corpus and query
set manifests with checksums, the binary provenance including the mock's
`--search-hits` and `--search-doc-bytes`, the arm table, the gate column
explained, and — first, before any number — what the run does **not** license
anyone to claim. Open it with the run's own identity:

```markdown
# <one line: what was measured>

Run `search-latency-aws-runbook-2026-09-16T0900Z`, produced by
`bench/search-latency/HARNESS-AWS-RUNBOOK.md`. Fleet up <HH:MM>–<HH:MM> UTC on <date>.
Arms: <names>, N=<reps> each. Binaries: crate commit <sha>; engine-mock <sha>,
--search-hits <n>, --search-doc-bytes <n>.

NOT AN ENGINE MEASUREMENT. The far end matched nothing and scored nothing.
Every latency here is a lower bound on a real one and every q/s an upper bound.
```

Hand the user the absolute path of `$R` and say which arms landed in it.

---

# Part B — `ossearch`

Everything above applies; this section is only what differs.

## B1 — the mock runs in HTTP mode

```bash
ssh fts-sut '~/restart-mock.sh http 9200 10 0'
```

`--mode http` brings up no vector-store port; `ossearch` needs none. The index
probe on this half is OpenSearch's own `_count`, which the mock answers from the
same modelled index the CQL half used — but it is a **different mock process**,
so its index starts empty and the first `ossearch` arm rebuilds. Expected; let
it, and use `--no-index-build` from the second arm onward.

## B2 — `--no-analyzer-check` is mandatory, `--no-index-build` is not

`ossearch` probes the analyzer whether or not it built the index, because an
index somebody else created with a different analyzer would make every latency
below it a comparison of tokenizers. The mock answers no `_analyze` route, and
this is the one check that **fails the run** rather than degrading, so every arm
here passes `--no-analyzer-check`.

Two consequences:

- **`POST /{index}/_analyze` appearing in `unexpected_requests` means the flag
  was dropped.** That is the sibling runbook's gate and it holds here unchanged.
- **`unknown` in the header is correct.** The mock does not answer
  `GET /{index}/_settings` or `GET /{index}/_mapping`; the crate degrades per
  field rather than failing. Do not chase it, and do not repoint the run at a
  real OpenSearch to make it go away. Those two routes are the *only* entries
  Phase 6 permits in this half's `unexpected_requests`.

## B3 — the arms

```bash
ssh fts-harness 'cat > ~/run-os-arm.sh << "SCRIPT"
#!/bin/bash
# As run-search-arm.sh, for ossearch: no --hosts/--port/--vs-url, one --url.
# NB: no apostrophes in this script.
set -u
ARM="$1"; shift
REPS="${REPS:-3}"; LADDER="${LADDER:-1,2,4,8,16,32,64,128}"
CLASSES="${CLASSES:-common_term}"; WARMUP="${WARMUP:-3}"; DURATION="${DURATION:-12}"
CORPUS="${CORPUS:-/mnt/nvme/work/corpus.jsonl}"
QUERIES="${QUERIES:-/mnt/nvme/work/queries.json}"
MAX_DOCS="${MAX_DOCS:-200000}"
SINK="${SINK:-172.31.47.166}"; PORT="${PORT:-9200}"
OUT_DIR="${OUT_DIR:-/mnt/nvme/work/os-results}"
LAT_DIR="${LAT_DIR:-/mnt/nvme/work/os-latencies}"
BIN=/mnt/nvme/work/target/release/ossearch
mkdir -p "$OUT_DIR" "$LAT_DIR"
WINDOWS="$OUT_DIR/run-windows.tsv"
[ -f "$WINDOWS" ] || printf "arm\trep\tstart_epoch\tend_epoch\texit_code\tladder\tclasses\tcsv\n" > "$WINDOWS"
stamp() { awk "{ printf \"%d\t%s\n\", systime(), \$0; fflush() }"; }
for rep in $(seq 1 "$REPS"); do
    csv="$OUT_DIR/$ARM-rep$rep.csv"; log="$OUT_DIR/$ARM-rep$rep.stderr.tsv"
    echo "######## arm=$ARM rep=$rep $(date -u +%H:%M:%S)"
    start=$(date +%s)
    "$BIN" --corpus "$CORPUS" --queries "$QUERIES" \
           --query-classes "$CLASSES" --concurrency "$LADDER" \
           --warmup "$WARMUP" --duration "$DURATION" --max-docs "$MAX_DOCS" \
           --url "http://$SINK:$PORT" --no-analyzer-check \
           --out "$csv" --latencies-dir "$LAT_DIR/$ARM-rep$rep" \
           "$@" 2>&1 | stamp > "$log"
    code=${PIPESTATUS[0]}
    printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
        "$ARM" "$rep" "$start" "$(date +%s)" "$code" "$LADDER" "$CLASSES" "$csv" >> "$WINDOWS"
    grep -E "^[0-9]+	  ->" "$log" | tail -12
done
SCRIPT
chmod +x ~/run-os-arm.sh'

ssh fts-harness 'REPS=3 ~/run-os-arm.sh b1-os-floor'
ssh fts-sut     '~/restart-mock.sh http 9200 10 2'
ssh fts-harness 'REPS=1 ~/run-os-arm.sh b2-os-delay2'
ssh fts-sut     '~/restart-mock.sh http 9200 10 0'
ssh fts-harness 'REPS=1 LADDER=32 CLASSES=rare_term,common_term,phrase,bool_and,bool_not,bool_mixed \
                 ~/run-os-arm.sh b3-os-classes'
ssh fts-harness 'REPS=1 LADDER=32 ~/run-os-arm.sh b3-os-fetch --fetch-documents --no-index-build'
```

**The closed-loop check is valid across the whole ladder on this half**, unlike
the CQL arm — reqwest gives every in-flight `_search` its own socket, so the
mock's per-read delay is a per-request delay. If p50 is flat here at 2 ms up to
`c=128` and the CQL arm's `c=1` point agrees, the property is established for
the harness as a whole and the CQL arm's deviation above `c=1` is attributable
to the instrument. If p50 grows **here**, the harness has a queue and every read
chart in the campaign is affected.

## B4 — collect

The same commands with `opensearch` for `scylla`, `/mnt/nvme/work/os-results`
and `/mnt/nvme/work/os-latencies` for the Part A paths, and `/tmp/mock-http-*`
for the mock's logs and witness JSONs. `/tmp/mock-cpu.tsv` is **one file across
both halves** — the sampler appends and the mock's generations are told apart by
the tick counter resetting — so copy it into both subtrees, or into `$R/env/`
once and reference it from both; do not let one half's copy be the only one.

One changed expectation in the gate:

```bash
python3 -c 'import json,glob,sys; [print(p, json.load(open(p))["unexpected_requests"]) for p in glob.glob(sys.argv[1])]' \
  "$R/opensearch/sinks/*.json"
# expect EXACTLY: GET /<index>/_settings and GET /<index>/_mapping.
# More is a setup call that changed; less is one that stopped arriving;
# "_analyze" means --no-analyzer-check was dropped; "_search" means the
# Phase 0 change is not in this binary.
```

---

## Traps

The sibling runbook's traps 1–10 all apply to the fleet mechanics and are not
repeated. These are this tree's.

1. **A search route that 404s does not look like a broken instrument.** It looks
   like a clean run of failed cells: blank percentiles, an error count, a
   non-zero exit. The gates that catch it are the header `-null-sink` check, the
   blank-percentile check and `unexpected_requests`. Run all three before
   reading a single number.

2. **The CQL arm succeeds against a mock with no search support, and lies.**
   Empty Rows, real timings, `zero_hit_queries == queries`. This is the single
   most likely way a session produces a number that gets quoted. Phase 0's
   `--help | grep search-hits` and Phase 6's gate 4 (`hits_mean` is the constant
   the mock was told to return) are what stop it.

3. **`--delay-ms` is per socket read, not per request.** On one shared CQL
   connection at `c>1` one sleep covers several frames. The closed-loop check is
   valid on the two HTTP arms across the ladder and on the CQL arm at `c=1`
   only. Do not average the CQL arm's delayed p50 across the ladder and do not
   report it as a client number.

4. **A mock restart zeroes the index and the next arm rebuilds.** Order the arms
   so each restart is followed by an arm that may build, and pass
   `--no-index-build` to every arm that must not. A rebuild in the middle of a
   read measurement is a two-second write nobody asked for, and it moves the
   first cell of that arm.

5. **`hits_mean` is an instrument setting, not a measurement.** It is
   `min(--search-hits, --limit)` and nothing else. It must never be read as
   relevance, and a change to `--search-hits` between arms makes their
   `--fetch-documents` numbers incomparable.

6. **The corpus and the query set come from different places on purpose.** The
   header will say so. Anyone who reads that as a mistake and "fixes" it by
   generating queries from the synthetic corpus has changed the query byte
   lengths, which is the one corpus-derived property that reaches a measured
   number here.

7. **Percentiles do not average, which is why `--latencies-dir` is not
   optional.** Three reps of a cell give three p99s and the p99 of the three
   together can only be computed from the samples. The distributions are the
   larger half of this campaign's artifacts and they are the half that cannot be
   reconstructed.

8. **`c=1` is not optional either.** It is the only level where the
   per-request cost is unambiguous, the level Little's law is checked at, and
   the only valid CQL closed-loop point.

9. **The samplers must append.** The mock is restarted between delay settings on
   this campaign — more often than on the sibling's — so a sampler that
   truncates on start loses the series for every arm before the restart.

---

## Cost

Two `i8g.2xlarge` on-demand in `eu-north-1`. Every measurement row is an
estimate until a session times it; **time them and write the real numbers here.**

| | wall |
|---|---|
| bring-up, toolchain, three crate builds | ~20 min |
| `engine-mock` build + copy + `--help` check | ~4 min |
| corpus (200 k × 3,948 B) + query set staging | ~3 min |
| A1 + A2 — two floors, ladder to 128, N=3 | ~14 min |
| A3 — closed loop, N=1, vector-store ladder + CQL `c=1` | ~4 min |
| A4 + A5 — class axis, projection, zero-hit self-test | ~4 min |
| B1 — OpenSearch floor, N=3 | ~7 min |
| B2 + B3 — closed loop and class axis, N=1 | ~5 min |
| collect, verify, stop | ~6 min |
| **one session** | **~1 h 5 min – 1 h 20 min** |

Cheaper than the write-path campaign, for one reason: the corpus is 200,000
documents rather than 2.5 M, because here it is a precondition rather than the
budget. If the session has to be shortened, cut **reps on the floor arms**, never
the closed-loop arm and never `c=1`. The floors are already conservative
statements; the closed-loop check is the thing that cannot be inferred from
anything else in `bench/`.

Standing cost between sessions is the root EBS volumes, billed whether the
instances run or not.

---

## Recorded results

Nothing yet. **This runbook has never been run**, and it cannot be until Phase 0
lands. When it is, add a row here with the run id, the `engine-mock` commit and
`--search-hits`, and the three numbers Phase 8 sends to `../TUNING.md`.

| Run | Instrument | What it established |
|---|---|---|
| — | — | — |
