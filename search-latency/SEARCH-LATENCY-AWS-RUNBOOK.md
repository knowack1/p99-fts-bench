# Search-latency runbook — what a full-text search costs on each engine

**Hand this file to Claude Code as the instruction and it runs the campaign
end to end.** It is the real-engine twin of
[`HARNESS-AWS-RUNBOOK.md`](HARNESS-AWS-RUNBOOK.md), which measures the same two
binaries against a null sink and is therefore blocked on `engine-mock` learning
to answer a search. **This runbook is not blocked on that work** — it points the
binaries at ScyllaDB and OpenSearch, which have always been able to answer one.
What it loses without it is named in [Gates](#gates): there is no measured
harness floor for the read path, so the client-headroom question is answered by
the harness box CPU sampler rather than by a number.

The governing plan is [`../AWS-RUN-PLAN.md`](../AWS-RUN-PLAN.md) Phase 3, and
the read-path fairness rules are
[`../COMPARABILITY.md`](../COMPARABILITY.md). Where this file and those
disagree, they are right and this file has drifted.

| Runbook | Arms | Binaries | Stack on the SUT |
|---|---|---|---|
| **this one** | A1–A6: two interfaces × two ScyllaDB index locations, plus OpenSearch on disk and on tmpfs | `scyllasearch`, `ossearch` | four configurations, in sequence |
| [`HARNESS-AWS-RUNBOOK.md`](HARNESS-AWS-RUNBOOK.md) | harness floors | same two | `engine-mock` |
| [`../build-rate/INDEX-RATE-SCYLLA-RUNBOOK.md`](../build-rate/INDEX-RATE-SCYLLA-RUNBOOK.md) | R1, R2, R8 | `scyllarate` | ScyllaDB + vector-store |
| [`../build-rate/INDEX-RATE-OPENSEARCH-RUNBOOK.md`](../build-rate/INDEX-RATE-OPENSEARCH-RUNBOOK.md) | R4, `os-disk-refresh1` | `osrate` | OpenSearch |

The four charts this produces are the ones [`README.md`](README.md)
specifies and none of them are rendered here: X is `concurrency`, Y is
`p50_ms` / `p90_ms` / `p99_ms` / `queries_per_s`, a series is `engine` +
`interface`. Because every arm walks **every query class** at every level, each
of the four is six facets — one per class — with `rare_term` the headline.

---

## The one thing to get right before anything else

**The index is a precondition that dies with its container.** The ScyllaDB
full-text index lives in RAM and is rebuilt from scratch on restart; the
OpenSearch tmpfs index is gone the moment the container stops. Bringing a stack
down between two arms that share an index destroys it, and the next arm pays a
full rebuild — or, worse, measures a half-built one until the gate catches it.

This inverts the sibling runbooks' central rule. There, the stack is recreated
between arms and never restarted in place, because each arm is a *different
build* and a reused container carries the last one's state. Here the index **is**
the state and it is what every arm reads, so rebuilding it between two arms of
one configuration would mean they read two different indexes.

| | build-rate | **this runbook** |
|---|---|---|
| Between arms of one configuration | `*-down` then `*-up`, always | **never down; the stack stays up** |
| Between configurations | same | down and up — the configuration *is* the change |
| The index | rebuilt per arm, and timed | **built once per configuration, then frozen** |
| `--no-index-build` | not used | **on every measured sweep** |

So: the index is built **once per SUT configuration**, by its own dedicated
step, and every sweep after it carries `--no-index-build`, which turns any
attempt to rebuild into a refusal on the flags rather than a silent 5-minute
DROP KEYSPACE in the middle of a campaign.

| Blocked | Consequence |
|---|---|
| a stack comes down between A1 and A2, or between A3 and A4 | the vector-store index is gone; the second arm either rebuilds (an hour of re-measuring to keep the pair comparable) or refuses |
| a sweep is run without `--no-index-build` and the count does not match | **the keyspace is dropped**, on billed fleet time, and every arm before it becomes incomparable |
| the vector-store image predates `94a23ef2` | **`VECTOR_STORE_FTS_INDEX_DIR` is ignored in silence** — A3 and A4 then measure a RAM index while their headers claim disk, and nothing in the artifacts contradicts it |

---

## Three constraints that shape every command here

The first two were found by reading the code rather than by running it, and
neither is a bug to fix on the fleet. The third is a campaign decision that
departs from the binary's own default, and it is written into the sweep script
rather than onto the run lines.

### One query class and one top-k per sweep, always

`ftsbench.probe_windows` keys every window on `(sweep, concurrency, rep)` and
**exits 1 on a duplicate** (`ftsbench/probe_windows.py:353-361`). Its level
regex is `\[\d+/\d+\] concurrency=(\d+)` — it reads the concurrency out of the
harness's own cell announcement and nothing else. A matrix that walks six
classes at one level announces `concurrency=16` six times in one rep, which is
six identical keys, which is a refused arm.

The sweep name comes from the file name (`<sweep>-rep<n>.stderr.tsv`), so the
fix is to put the class **in the sweep name** and run one class per invocation.
Every run line below does. The cell count is unchanged; only the number of
processes goes up.

**The same applies to `--limit`, for the same reason.** Three top-k values at
one concurrency inside one sweep are three identical keys. `--limit` is a CSV
column, so a consumer can tell the rows apart — `probe_windows` cannot, because
it reads only the concurrency out of the announcement. Every sweep name below
carries `k<limit>` as well as its class.

The same rule kills the other obvious shape: `--concurrency 1,2,4,1,2,4` in one
process, which is what [`README.md`](README.md) recommends for spreading host
drift. Three traversals in one file are three identical keys per level. **Reps
are separate invocations here**, one traversal each, exactly as next door.

### The query set samples a prefix, and full corpus is what makes that safe

`--max-docs` takes the **first** N lines of the corpus
(`ftsbench/corpus.py:17-22`), and so does `generate_queries --sample-docs`. When
a campaign indexes a prefix, the two have to be reconciled: a query set sampled
past the bound can name a `rare_term` living only in documents the index never
received, the class then matches nothing, and a cell where every query matches
nothing still has a p99 — the cost of finding nothing — which the harness
reports and exits non-zero over.

**Indexing the whole corpus removes the failure rather than guarding against
it.** Every document the generator can see is a document the index holds, at any
`--sample-docs`. This is the quiet argument for full corpus: a class of silent
comparability bug stops being possible.

`--sample-docs 500000` is kept anyway, for cost — a whole-corpus scan is ~25 min
of Python against ~90 s — and one property of it must reach the write-up:
**the class definitions are relative to the sample, not to the index.** A
`rare_term` sits in a df band of 500–5,000 documents *of the 500,000 sampled*,
which is roughly 9,000–90,000 documents of the 8,967,625 indexed. "Rare" means
rare in the sample. It is the same set for every arm, so it is a constant rather
than a bias — but it is not a statement about the index.

**Do not reuse `data/queries.json` from the laptop pass**: it is simplewiki's,
and its terms mean nothing here.

### The statement mode is `prepared`, campaign-wide

Every CQL request in this campaign goes through a prepared statement. Each
distinct query is prepared once, before the matrix starts, and the matrix then
re-executes it — which is what an application does and what the numbers are
supposed to describe.

**This is an override, not the default.** `scyllasearch` defaults to
`--statement literal`, and it has a reason: a literal statement is re-parsed by
the coordinator per request, which is what Lucene's `query_string` parser does
on the OpenSearch side, so `literal` is the arm that matches the *other engine's*
per-request work. `prepared` matches the *application's*. Both are defensible
and they are not the same measurement.

So the override is set in `run-search-arm.sh` as `STATEMENT=prepared` rather
than on each run line — a sweep cannot be written without it — and it comes with
one obligation, discharged in [Caveats](#caveats-this-half-carries-into-the-write-up):

> **Every chart drawn from the CQL arm must say that its statements were
> prepared while the OpenSearch arm re-parsed per request.** A reader who
> assumes parser parity from the analyzer parity would otherwise read the
> difference as engine speed. `statement=` in the CSV header is the evidence,
> and Phase 7 gates on it reading `prepared` and nothing else.

The size of that difference is **not measured by this campaign**. Quantifying it
would take one `STATEMENT=literal` sweep at `c=16` on `rare_term`, ~4 min, and
it is listed as an add in [Cost](#cost) for the session that wants the number in
hand before a reviewer asks for it.

**`--statement` is ignored by `--interface vector-store`**, which has no
statements — but the flag still reaches that arm's CSV header. See
[Traps](#traps).

---

## What this measures, and what it is not

One cell is one `(concurrency, query class)` pair against a **resident,
finished, idle** index. N workers in a closed loop, each sending its next query
the moment the previous one answers.

- **`p50_ms` / `p90_ms` / `p99_ms` are service times.** Nothing is offered on a
  schedule and there is no queue between the workers and the engine, so
  coordinated omission does not apply and neither does the correction for it.
  These numbers are **not interchangeable** with the open-loop C5/C7 numbers
  the Python harness produced; do not put them on one axis.
- **`queries_per_s` is completed over wall**, the throughput that concurrency
  achieved, never one that was asked for.
- **Nothing here is an index-build measurement.** The build is a precondition
  and its docs/s is printed only so an operator can see it moving
  (`scylla/src/loader.rs`). The build-rate tree owns that question.
- **`cql` minus `vector-store` is ScyllaDB's own read overhead**, and it is the
  only reason A2 exists. The subtraction is valid **only** because neither arm
  fetched documents — the one mode the BM25 endpoint can serve.
- **This is not C7.** There is no offered-rate axis and no SLA line. A knee
  against offered load is a different instrument and the plan's Phase 3 step 4
  owns it.

**Every number this produces is PRELIMINARY until the write-up says otherwise,
and none of it is quotable from the CSVs alone.**

---

## The six arms, four SUT configurations, and the optional axis

Two variables on the SUT, crossed: **which engine**, and **where its full-text
index lives**. The ScyllaDB cell carries two interfaces, because `cql` minus
`vector-store` is ScyllaDB's own read overhead and that subtraction has to be
available in both index locations or it is a property of one of them.

| | index in RAM | index on disk (NVMe) |
|---|---|---|
| **ScyllaDB + vector-store** | **A1** `cql`, **A2** `vector-store` | **A3** `cql`, **A4** `vector-store` |
| **OpenSearch** | **A6** `http`, tmpfs | **A5** `http` |

| Arm | Dir | Binary and interface | SUT knob |
|---|---|---|---|
| **A1** | `a1-cql-ram` | `scyllasearch --interface cql --statement prepared` | `VS_FTS_INDEX_DIR` **unset** — the default, and how ScyllaDB FTS ships today |
| **A2** | `a2-vstore-ram` | `scyllasearch --interface vector-store` | same stack as A1, same index |
| **A3** | `a3-cql-disk` | `scyllasearch --interface cql --statement prepared` | `VS_FTS_INDEX_DIR=/var/lib/vector-store/fts` |
| **A4** | `a4-vstore-disk` | `scyllasearch --interface vector-store` | same stack as A3, same index |
| **A5** | `a5-os-disk` | `ossearch` | `--index-config disk`, segments on the NVMe |
| **A6** | `a6-os-ram` | `ossearch` | `OS_RAM_INDEX=1` + `--index-config ramindex`, segments on a 12 GiB tmpfs |

Every arm walks **the whole matrix**: every concurrency level, against every
query class, at every top-k.

| Sweep set | Ladder | Classes | Top-k | Reps | Sweeps | Cells |
|---|---|---|---|---|---|---|
| **ladder** | `1,2,4,8,16,32,64,128` | all six, one sweep each | `10`, `100`, `1000` | 3 | 18 | **432** |

**2,592 cells across six arms, 18.0 h of measured wall.** The optional axis, run
last and cut first:

| Axis | Arms | Why it is optional |
|---|---|---|
| `--fetch-documents` at `c=16`, `rare_term` | A1, A3, A5 | the projection is what an application does, but it is a transfer cost and not a search cost |
| the refusal self-test | A2 | 3 s, proves `--fetch-documents` fails on the flags before it connects |

**`--fetch-documents` is unavailable on A4 and A6**, for two different reasons
that must not be conflated: A4 is a BM25 endpoint that cannot return text and
**refuses the flag**; A6's `ramindex` mapping sets `_source: {"enabled": false}`
so the index stores no document to return, and the request would succeed while
fetching nothing.

### Top-k is a dimension, not a constant

`--limit` reaches the CSV as column 14 precisely so that two runs at two limits
can be told apart, and the harness's own design note calls it *"one value per
run, not a matrix dimension."* This campaign makes it one anyway, at
**`10`, `100`, `1000`**.

**1000 is not an arbitrary top of the range — it is the engine's ceiling.**
ScyllaDB's M1 makes `LIMIT` mandatory and caps it at 1000 (`MAX_LIMIT`, refused
on the flags above it), so `10 → 100 → 1000` spans two decades and stops exactly
where the product does. The harness enforces the same cap on the OpenSearch side
so the two halves can never ask for different top-Ns.

It is worth a dimension because k is the one query parameter that changes what
the engine *does* rather than what it matches:

- the top-k collector's heap grows with k on both sides, and neither engine is
  counting total hits — `track_total_hits` is false — so k is the entire size of
  the work after matching;
- the reply grows with k even when documents are not projected: 1,000 primary
  keys instead of 10;
- **the ScyllaDB arms re-prepare per k.** `--limit` is formatted into the
  statement text (`QueryShape::statement`), so each k is a different prepared
  statement — which is correct, and means the prepared-statement cost is paid
  three times rather than shared.

Expect k to interact with concurrency: a query that does 100× the collector work
saturates the engine at a lower level, so the ladder's knee should move left as
k rises. That interaction is the reason k is crossed with the ladder rather than
sampled at one level.

### The primary comparison, and the three the 2×2 adds

**A1/A2 against A5 is the deployment-normal comparison** — each engine as it
actually ships, and the pairing the laptop pass and
[`../COMPARABILITY.md`](../COMPARABILITY.md) assumed. It is the headline.

The other three cells exist because the primary pairing is a **diagonal** of
this 2×2: ScyllaDB with its index in RAM against OpenSearch with its index on
disk. That is the honest way to compare the two products, and it is also two
changes at once. Without the off-diagonal cells the campaign cannot say whether
a gap is the engine or the storage tier, and the full 2×2 answers three
questions the diagonal cannot:

| Read | Answers |
|---|---|
| A1/A2 vs A3/A4 | what the vector-store's disk index costs on the read path, which is the read-side counterpart of build-rate's R2↔R8 |
| A5 vs A6 | the same for OpenSearch, and the read-side counterpart of R4↔`os-disk-refresh1` |
| the two RAM arms against each other, and the two disk arms | an engine comparison with the index location **held fixed** — which is the comparison the diagonal cannot make |

**A caveat that cannot be tuned away: A5 vs A6 is not a clean one-variable
test.** `index-config-ramindex.json` differs from `index-config.json` in
`_source` as well as location, and the clean version — `OS_RAM_INDEX=1` with
`--index-config disk` — does not fit at any corpus size this campaign would
use. At full corpus the postings alone need ~24 GiB of tmpfs
(`docker/.env.sut:105`) and stored `_source` would add roughly 10 GiB on top of
that, which no budget on this box reaches while still leaving room for a JVM.
The confound is structural, it is disclosed rather than netted out, and
`--refresh-interval 1s` is forced on both OpenSearch builds so that refresh, at
least, is not a third variable.

**Expect the location reads to be small, and measure them anyway.** The working
set is roughly 14 GB against 61 GiB of box RAM, so the "disk" arms are
page-cache resident after their first warm-up: A3/A4 and A5 are RAM-versus-RAM
against their tmpfs twins, not a storage-tier comparison. That is an arithmetic
prediction, `cache_bytes` from each arm's probe is the evidence for or against
it, and this campaign is the thing that turns it from a claim into a number.

**The statement mode is not an axis.** Every CQL sweep above is prepared; there
is no literal arm. See
[the statement mode](#the-statement-mode-is-prepared-campaign-wide).

### Why the full matrix, and not two slices through it

The cheap design is one ladder on a headline class plus all six classes at one
fixed level — 132 cells instead of 432, and it is what an earlier draft of this
runbook specified. It is a one-factor-at-a-time design, and it assumes the two
axes **do not interact**: that the class ranking is the same at every
concurrency, and that the concurrency curve has the same shape for every class.

The laptop pass says that assumption is false. Per-class p50/p99 at `c=16`,
median repetition, from
`../results/laptop-simplewiki-2026-08/c6-query-matrix/c6.json`:

| class | `opensearch` | `scylla-cdc` |
|---|---|---|
| `rare_term` | 3.34 / 5.23 | 1.83 / **3.03** |
| `common_term` | 3.47 / 6.16 | 2.01 / 3.17 |
| **`phrase`** | 4.03 / **7.46** | 3.38 / **14.60** |
| `bool_and` | 3.71 / 7.64 | 2.61 / 4.06 |
| `bool_not` | 3.97 / 7.33 | 2.60 / 4.46 |
| `bool_mixed` | 5.42 / 9.81 | 3.32 / 5.60 |

`phrase` is **4.8× `rare_term`'s p99 on ScyllaDB** and the only class where
OpenSearch wins the tail. A query costing roughly five times more saturates the
engine at roughly a fifth of the concurrency, so:

- a ladder measured only on `rare_term` — the cheapest class in the set — knees
  at the highest concurrency any class would, and that knee does not transfer;
- a class comparison taken only at `c=16` may be past `phrase`'s knee while
  sitting well below `rare_term`'s, which would make it partly a comparison of
  how saturated each class was rather than how expensive each query is.

Neither slice can detect the interaction, and the interaction is exactly what
the laptop numbers point at. The plane costs 3.0 h instead of 0.9 h and it is
the difference between measuring the surface and assuming it is flat.

**`rare_term` is still the headline class** —
[`../AWS-RUN-PLAN.md`](../AWS-RUN-PLAN.md) Phase 3 step 3 names it and the
laptop pass's C5 is a `rare_term` chart — but it is now one facet of the result
rather than the whole of it. `c=16` keeps its own significance for the same
reason: it is the level the laptop pass ran (`QUERY_CONCURRENCY ?= 16`), so it
is the row of this matrix that is shape-comparable with C6.

---

## The fleet

| Alias | Instance | Role |
|---|---|---|
| `fts-harness` | `i-08d8d2505e16683f7` · `k-nowacki-fts-benchmark-harness` | runs `scyllasearch` and `ossearch`, holds the corpus and the query set, builds the vector-store image |
| `fts-sut` | `i-0e3e4b6b02e654b7f` · `k-nowacki-fts-benchmark-sut` | runs the engine under measurement |

Both `i8g.2xlarge` (8 vCPU Graviton4, 61 GiB, aarch64), `eu-north-1b`, Amazon
Linux 2023, user `ec2-user`, key `~/.ssh/KarolNowackiAws.pem`. Private RTT
0.353 ms, measured — **subtract nothing for it**; it is part of what a client
pays and it is identical on all six arms.

**One engine at a time.** The SUT has eight cores; running both stacks at once
would make every latency a contention measurement. A1 and A2 share one
ScyllaDB stack and one index; A3 runs after `scylla-down`.

**Access, as of 2026-09-16.** There is no AWS CLI credential on the laptop and
the Chrome console session is expired behind a federated sign-in. `aws login`
in the terminal is the better path: there is no session to keep alive, so the
stop at the end cannot be lost. If the console is used instead, click its
refresh control every few minutes for the **whole** run — at ~2.5 h this
campaign is long enough for a session to expire, and an expired session means
the boxes bill until someone else stops them.

`~/.ssh/config` pins stale IPs. Both boxes are stopped and get new public IPs
on start unless those addresses are Elastic. Phase 2 re-points them.

---

## Phase 0 — the results directory, on the laptop

Everything on the fleet is ephemeral: `/mnt/nvme` is destroyed on every stop,
so **the laptop is the only place results survive**. Create the directory
before touching a single instance.

```bash
# Run this ONCE, at the very start of the session, and keep the shell.
export RUN_ID="search-latency-$(date -u +%Y-%m-%dT%H%MZ)"
export R="$HOME/Projects/Scylla/p99/bench/results/$RUN_ID"
for arm in a1-cql-ram a2-vstore-ram a3-cql-disk a4-vstore-disk; do
  mkdir -p "$R/$arm"/scylla/{points,latencies,logs,probe}
done
for arm in a5-os-disk a6-os-ram; do
  mkdir -p "$R/$arm"/opensearch/{points,latencies,logs,probe}
done
mkdir -p "$R"/{env,corpus,queries,scripts,sut}
printf '%s\n' "$RUN_ID" > "$R/RUN_ID"
printf 'session opened: %s\n' "$(date -u +%FT%TZ)" >> "$R/env/sessions.txt"
ln -sfn "$RUN_ID" "$(dirname "$R")/search-latency-latest"
echo "results -> $R"
```

giving

```
bench/results/search-latency-2026-09-16T0900Z/
├── RUN_ID  env/  corpus/  queries/  scripts/  sut/
├── a1-cql-ram/      scylla/     points/ latencies/ logs/ probe/
├── a2-vstore-ram/   scylla/     ...
├── a3-cql-disk/     scylla/     ...
├── a4-vstore-disk/  scylla/     ...
├── a5-os-disk/      opensearch/ ...
└── a6-os-ram/       opensearch/ ...
bench/results/search-latency-latest -> search-latency-2026-09-16T0900Z
```

**The arm directory name carries the SUT configuration, and it is the only
thing that does.** `engine` and `interface` are CSV columns; *where the index
lived* is not, on either engine. An arm written to the wrong directory is a
mislabelled storage tier that no column contradicts — which is the one
mislabelling this campaign cannot detect after the fact.

**The arm directory name is the chart's series set.** An arm written to the
wrong directory becomes a mislabelled line rather than a missing one, and
`engine` + `interface` in columns 16 and 17 are the only thing that would
contradict it.

**`latencies/` is beside `points/` and never inside it.** Phase 7 globs
`points/*.csv`; a latency distribution read as a set of points is a silent
corruption of the analysis.

**Capture `RUN_ID` once and reuse the variable.** If the shell is lost, recover
with `export R="$(readlink -f bench/results/search-latency-latest)"` rather
than recomputing the timestamp. Report the absolute path of `$R` when the run
finishes.

---

## Phase 1 — start the boxes

Prefer the CLI, for the reason in [The fleet](#the-fleet):

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

If only the console is available:

```
https://eu-north-1.console.aws.amazon.com/ec2/home?region=eu-north-1#Instances:search=k-nowacki;v=3
```

Do not type into the filter box — it opens an "API filters" dropdown and
swallows the text. The `search=k-nowacki` in the URL is the filter. Select both
rows with the header checkbox → **Instance state → Start instance**. Wait for
`Running` and `3/3 checks passed`, and **keep the tab alive for the whole
session**.

---

## Phase 2 — fleet re-entry

Every stop wipes the instance store, so this runs on **every** start.

### 2a. SSH and the private IPs

Public IPs are reassigned on every start; private IPs are not.

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
CQL and BM25 routing at once — and on this campaign it breaks them *after* the
index has been built, which is the expensive moment to find out. Editing
`.env.sut` is then part of re-entry, and the file is captured into every arm's
manifest either way.

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

Every cell's CPU and RSS come from a join between the SUT's probe timestamps
and the harness's stderr stamps, so a skew larger than one probe tick puts a
cell's samples on its neighbour.

```bash
ssh fts-sut date +%s.%N; ssh fts-harness date +%s.%N
```

chrony keeps this in the microseconds. **Above 1 s the join is refused rather
than padded** — padding pulls the adjacent cell's peak in. Record the number in
`$R/env/clock-skew.txt`.

### 2d. The bench checkout and the venv on the harness

**The tar has to carry three directories, not two.** `search-latency` depends
on `build-rate` by path (`build-rate-core`, `scyllarate`, `osrate`), and
`ftsbench`/`tools`/`docker`/`Makefile` are what bring the stack up and slice the
probe.

```bash
cd ~/Projects/Scylla/p99/bench && tar czf - --exclude=__pycache__ --exclude=target \
    ftsbench tools docker Makefile build-rate search-latency \
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

### 2e. The vector-store image — the critical path of the ScyllaDB half

`daemon.json` points docker's `data-root` at the instance store, so every image
goes with the stop. The two public images re-pull; the vector-store is in no
registry and must be rebuilt from **`94a23ef2`**. ~15 min of billed fleet time.

**Run this in a shell where `tools/fleet_env.sh` has NOT been sourced.**
`run-with-release-toolchain` needs the docker daemon *local*; with
`DOCKER_HOST=ssh://<sut>` set it sends both the build and the resulting image to
the SUT's daemon, where `docker save` on the harness will not find it.

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

The image the compose file asks for is exactly the string in `docker/.env.sut`'s
`VECTOR_STORE_IMAGE`. A mismatch is a `pull access denied` at `scylla-up` — the
**good** failure. The bad one is an *older* image carrying the same tag.

### 2f. Prove the engine ports are reachable, before the first arm

```bash
ssh fts-harness 'for p in 9042 16080 9200; do
  timeout 3 bash -c "</dev/tcp/172.31.47.166/$p" && echo "$p open" || echo "$p closed"
done'
```

Nothing is listening yet, so `closed` is expected here — this is the command,
not the gate. Re-run it after each `*-up` in Phase 6 and require `open` on that
stack's ports. On the second `-priv` pair added 2026-09-16 these three ports
were blocked by the security group with a confirmed listener behind them; that
pair is **not** the fleet this runbook uses.

---

## Phase 3 — the corpus and the query set, on the harness and only there

Both binaries read the corpus locally through `--corpus`; nothing streams it
and the SUT never sees a line of it. No corpus, no arm.

| Path | Survives a stop? | What |
|---|---|---|
| `~/corpus.jsonl.zst` (harness root EBS) | **yes** | ~10.2 GB, `pzstd -10` |
| `/mnt/nvme/data/corpus.jsonl` (instance store) | **no** | 35,448,823,550 bytes, re-made on every start |

```bash
ssh fts-harness 'set -e
  mkdir -p /mnt/nvme/data
  pzstd -d -p 8 -f -o /mnt/nvme/data/corpus.jsonl ~/corpus.jsonl.zst
  sha256sum /mnt/nvme/data/corpus.jsonl
  wc -l /mnt/nvme/data/corpus.jsonl'
# expect 1700bb6c9b2652cf7b248e8caff7bfecc54fd2376e9a75e43379aaa79c50c432
# expect 8967625 lines
```

~1.5–2 min, bounded by gp3's 125 MB/s baseline read. The sha256 is
[`../FREEZE.md`](../FREEZE.md)'s and is what proves the bytes are the frozen
corpus rather than a re-download that drifted. **Check it every time.**

**Do not use [`HARNESS-AWS-RUNBOOK.md`](HARNESS-AWS-RUNBOOK.md) Phase 4 here.**
That runbook *generates* a synthetic corpus at enwiki's mean line length, which
is right for a null sink that never indexes a document and wrong for every arm
here: BM25 term statistics, segment merges and the analyzer all depend on real
text.

If the archive is not on the root volume — a replaced box, a rebuilt volume —
re-stage from the Swedish Wikimedia mirror, run `prepare_corpus`, verify
against `../FREEZE.md`, then `pzstd -10` the result back to
`~/corpus.jsonl.zst` so the next start is 2 minutes instead of 20. Not from the
laptop (which does not hold enwiki) and not from S3 (the harness has no
instance profile; `DeveloperAccessRole` cannot `iam:CreatePolicy`).

### The query set, generated here and frozen for the campaign

Read [the prefix constraint](#the-query-set-must-be-generated-from-the-indexed-prefix)
before changing any number on this line.

```bash
ssh fts-harness 'set -e
  cd $BENCH
  .venv/bin/python3 -m ftsbench.generate_queries \
      --corpus /mnt/nvme/data/corpus.jsonl \
      --output /mnt/nvme/work/queries-enwiki.json \
      --sample-docs 500000 --per-class 200 --common-pool-size 200 --seed 99
  sha256sum /mnt/nvme/work/queries-enwiki.json
  .venv/bin/python3 -c "
import json
q = json.load(open(\"/mnt/nvme/work/queries-enwiki.json\"))
for name, qs in q[\"classes\"].items():
    print(name, len(qs), len(set(qs)))
"'
```

Gate on all of it before Phase 4:

- **six classes**: `rare_term`, `common_term`, `phrase`, `bool_and`,
  `bool_not`, `bool_mixed`.
- **200 distinct queries in each**, equal across classes. Unequal cardinality
  gives the smaller class more repeats per run and therefore a warmer cache,
  which would show up on the class chart as a query-class difference that is
  really a cache difference.
- `sampled_docs` in the JSON reads **500000**, and it is at or under
  `--max-docs`.

Pull it home immediately — it is campaign input, not fleet state:

```bash
scp fts-harness:/mnt/nvme/work/queries-enwiki.json "$R/queries/"
ssh fts-harness 'sha256sum /mnt/nvme/work/queries-enwiki.json' > "$R/queries/queries.sha256"
```

Record into `$R/corpus/`: the corpus line count, byte count and sha256; and
into `$R/queries/`: the set's sha256, its per-class distinct counts, and the
mean query byte length per class.

---

## Phase 4 — build the harness, then freeze it

```bash
# both read binaries, sharing one target dir -- they share every dependency
ssh fts-harness 'cd /mnt/nvme/work/bench/search-latency/scylla && . "$HOME/.cargo/env" \
  && CARGO_TARGET_DIR=/mnt/nvme/work/target cargo build --release --locked'
ssh fts-harness 'cd /mnt/nvme/work/bench/search-latency/opensearch && . "$HOME/.cargo/env" \
  && CARGO_TARGET_DIR=/mnt/nvme/work/target cargo build --release --locked'
ssh fts-harness '/mnt/nvme/work/target/release/scyllasearch --help | head -3
                 /mnt/nvme/work/target/release/ossearch --help | head -3'
```

If the Rust toolchain went with the stop:

```bash
ssh fts-harness 'curl --proto "=https" --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --profile minimal && . "$HOME/.cargo/env" && rustc --version'
```

**`--locked` is not optional.** Each binary's `build.rs` reads its own
`Cargo.lock` to stamp the linked driver version into every CSV header; a
resolver that moved a patch version mid-campaign would put two driver versions
in one chart and nothing but the headers would say so. The two share a target
directory because they share every dependency and neither writes the other's
lock.

Record, into `$R/env/`, **before** measuring:

```bash
ssh fts-harness 'cd /mnt/nvme/work/bench/search-latency && find . -type f \
  \( -name "*.rs" -o -name "Cargo.*" \) | sort | xargs sha256sum | sha256sum' \
  > "$R/env/harness-tree.sha256"
git -C ~/Projects/Scylla/p99/bench log -1 --format='%H %s' > "$R/env/bench-commit.txt"
git -C ~/Projects/Scylla/p99/bench status --short search-latency build-rate \
  >> "$R/env/bench-commit.txt"
```

**Do not rebuild once an arm has run.** A rebuild mid-campaign makes the arms
incomparable and nothing in the artifacts would say so.

### The sweep script — `scyllasearch`

```bash
ssh fts-harness 'cat > ~/run-search-arm.sh << "SCRIPT"
#!/bin/bash
# One sweep of the read matrix: ONE query class, one concurrency ladder, N reps,
# against the engine on fts-sut.
#
# ONE CLASS PER SWEEP, and the class is in the sweep name. probe_windows keys
# its windows on (sweep, concurrency, rep) and exits 1 on a duplicate, so two
# classes at one level inside one file would refuse the whole arm.
#
# stderr is timestamped per line and the tool announces every cell as it starts
# it ("[k/n] concurrency=C class=X"), so the log carries each cell own
# wall-clock window and the SUT probe can be cut to that window rather than to
# the whole matrix.
# NB: no apostrophes in this script -- it is delivered inside a single-quoted
# ssh argument, and one would close the quote.
set -u
SWEEP="$1"; CLASS="$2"; shift 2
REPS="${REPS:-3}"
LADDER="${LADDER:-1,2,4,8,16,32,64,128}"
WARMUP="${WARMUP:-5}"
DURATION="${DURATION:-20}"
LIMIT="${LIMIT:-10}"
# prepared, campaign-wide. The binary default is literal; it is overridden here
# rather than on the run lines so that no sweep can be written without it.
STATEMENT="${STATEMENT:-prepared}"
CORPUS="${CORPUS:-/mnt/nvme/data/corpus.jsonl}"
QUERIES="${QUERIES:-/mnt/nvme/work/queries-enwiki.json}"
# 0 = the whole corpus. Not a cap: every arm indexes all 8,967,625.
MAX_DOCS="${MAX_DOCS:-0}"
SUT="${SUT:-172.31.47.166}"
PORT="${PORT:-9042}"
# 16080, NOT the mock convention of PORT+7000. See the runbook note.
VS_PORT="${VS_PORT:-16080}"
KEYSPACE="${KEYSPACE:-wiki}"
TABLE="${TABLE:-articles}"
VS_INDEX="${VS_INDEX:-articles_body_fts}"
OUT_DIR="${OUT_DIR:-/mnt/nvme/work/results}"
# Deliberately NOT under $OUT_DIR: Phase 7 globs points/*.csv, and a latency
# distribution read as a set of points is a silent corruption of the analysis.
LAT_DIR="${LAT_DIR:-/mnt/nvme/work/latencies}"
BIN=/mnt/nvme/work/target/release/scyllasearch

mkdir -p "$OUT_DIR" "$LAT_DIR"
WINDOWS="$OUT_DIR/run-windows.tsv"
[ -f "$WINDOWS" ] || printf "sweep\tclass\trep\tstart_epoch\tend_epoch\texit_code\tladder\tcsv\n" > "$WINDOWS"
stamp() { awk "{ printf \"%d\t%s\n\", systime(), \$0; fflush() }"; }

for rep in $(seq 1 "$REPS"); do
    csv="$OUT_DIR/$SWEEP-rep$rep.csv"
    log="$OUT_DIR/$SWEEP-rep$rep.stderr.tsv"
    echo "######## sweep=$SWEEP class=$CLASS rep=$rep $(date -u +%H:%M:%S)"
    start=$(date +%s)
    "$BIN" --corpus "$CORPUS" --queries "$QUERIES" \
           --query-classes "$CLASS" --concurrency "$LADDER" \
           --warmup "$WARMUP" --duration "$DURATION" --max-docs "$MAX_DOCS" \
           --limit "$LIMIT" --statement "$STATEMENT" \
           --hosts "$SUT" --port "$PORT" \
           --keyspace "$KEYSPACE" --table "$TABLE" \
           --vs-url "http://$SUT:$VS_PORT" --vs-index "$VS_INDEX" \
           --out "$csv" --latencies-dir "$LAT_DIR/$SWEEP-rep$rep" \
           "$@" 2>&1 | stamp > "$log"
    code=${PIPESTATUS[0]}
    printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
        "$SWEEP" "$CLASS" "$rep" "$start" "$(date +%s)" "$code" "$LADDER" "$csv" >> "$WINDOWS"
    grep -E "^[0-9]+	  ->" "$log" | tail -8
done
SCRIPT
chmod +x ~/run-search-arm.sh'
```

### The sweep script — `ossearch`

```bash
ssh fts-harness 'cat > ~/run-os-arm.sh << "SCRIPT"
#!/bin/bash
# As run-search-arm.sh, for ossearch: no --hosts/--port/--vs-url, one --url.
# The analyzer probe is NOT suppressed here and must never be: an index built
# with a different analyzer makes every latency below it a comparison of
# tokenizers. The null-sink runbook passes --no-analyzer-check because a mock
# cannot answer _analyze; against a real OpenSearch that flag voids the arm.
# NB: no apostrophes in this script.
set -u
SWEEP="$1"; CLASS="$2"; shift 2
REPS="${REPS:-3}"; LADDER="${LADDER:-1,2,4,8,16,32,64,128}"
WARMUP="${WARMUP:-5}"; DURATION="${DURATION:-20}"; LIMIT="${LIMIT:-10}"
CORPUS="${CORPUS:-/mnt/nvme/data/corpus.jsonl}"
QUERIES="${QUERIES:-/mnt/nvme/work/queries-enwiki.json}"
# 0 = the whole corpus. Not a cap: every arm indexes all 8,967,625.
MAX_DOCS="${MAX_DOCS:-0}"
SUT="${SUT:-172.31.47.166}"; PORT="${PORT:-9200}"
INDEX="${INDEX:-wiki-articles}"
OUT_DIR="${OUT_DIR:-/mnt/nvme/work/os-results}"
LAT_DIR="${LAT_DIR:-/mnt/nvme/work/os-latencies}"
BIN=/mnt/nvme/work/target/release/ossearch

mkdir -p "$OUT_DIR" "$LAT_DIR"
WINDOWS="$OUT_DIR/run-windows.tsv"
[ -f "$WINDOWS" ] || printf "sweep\tclass\trep\tstart_epoch\tend_epoch\texit_code\tladder\tcsv\n" > "$WINDOWS"
stamp() { awk "{ printf \"%d\t%s\n\", systime(), \$0; fflush() }"; }

for rep in $(seq 1 "$REPS"); do
    csv="$OUT_DIR/$SWEEP-rep$rep.csv"
    log="$OUT_DIR/$SWEEP-rep$rep.stderr.tsv"
    echo "######## sweep=$SWEEP class=$CLASS rep=$rep $(date -u +%H:%M:%S)"
    start=$(date +%s)
    "$BIN" --corpus "$CORPUS" --queries "$QUERIES" \
           --query-classes "$CLASS" --concurrency "$LADDER" \
           --warmup "$WARMUP" --duration "$DURATION" --max-docs "$MAX_DOCS" \
           --limit "$LIMIT" \
           --url "http://$SUT:$PORT" --index "$INDEX" \
           --out "$csv" --latencies-dir "$LAT_DIR/$SWEEP-rep$rep" \
           "$@" 2>&1 | stamp > "$log"
    code=${PIPESTATUS[0]}
    printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
        "$SWEEP" "$CLASS" "$rep" "$start" "$(date +%s)" "$code" "$LADDER" "$csv" >> "$WINDOWS"
    grep -E "^[0-9]+	  ->" "$log" | tail -8
done
SCRIPT
chmod +x ~/run-os-arm.sh'
```

### Defaults every run line overrides, and the ones it must not

| Default | Override | Why |
|---|---|---|
| `QUERIES=/mnt/nvme/work/queries-enwiki.json` | never | the laptop `data/queries.json` is simplewiki's and its rare terms are not in this index |
| `CORPUS=/mnt/nvme/data/corpus.jsonl` | never | `/mnt/nvme/work/corpus.jsonl` is the *synthetic* null-sink corpus |
| `VS_PORT=16080` | never | the mock runbook derives `PORT+7000` → 16042; against the real stack that polls a closed port and **every sweep dies at its index probe** |
| `MAX_DOCS=0` (the whole corpus) | never | every arm indexes all 8,967,625 documents. See [the corpus](#the-corpus-is-the-whole-corpus) |
| `STATEMENT=prepared` | never | the binary defaults to `literal`; this campaign prepares every distinct query once before the matrix, which is what an application does. See [the statement mode](#the-statement-mode-is-prepared-campaign-wide) |
| `LIMIT=10` | **`100` and `1000` on their own sweeps** | top-k is a dimension here; the value must also appear in the sweep name as `k<limit>`, or `probe_windows` sees duplicate keys |
| `LADDER` | only on the smoke and the four index builds | every measured sweep walks the full ladder; a shortened one is a thinned matrix and must be declared campaign-wide, not per sweep |
| `--no-index-build` | **passed on every measured sweep** | the index is built once, in Phase 5, and nothing after it may drop the keyspace |

---

## Phase 5 — the bound, the index, and the smoke

### The corpus is the whole corpus

**`--max-docs 0`: every arm indexes all 8,967,625 enwiki documents.** No bound,
no prefix, no cap.

This **diverges from the build-rate runbooks**, which pin `--max-docs 3500000`
and say the bound moves for every arm in both of them if it moves at all. It is
not moved here — it is *absent*, and the reason it does not transfer is that the
build-rate bound exists to make a rate ladder comparable: *"every rung then
ingests the identical documents and only the rate differs."* **There are no
rungs here.** Nothing on the read path varies the ingest, so the bound's primary
justification does not apply, and what it costs — measuring a p99 against 39% of
the corpus — is exactly the kind of thing a posting-list-length-sensitive number
should not be asked to carry.

The consequence is disclosed rather than hidden: **read charts from this
campaign and build-rate's write charts describe different indexes**, and any
slide that puts them side by side says so.

Two things get easier, and one gets harder:

- **The query-set prefix constraint disappears.** With the whole corpus indexed,
  no generated term can name a document the index does not hold.
- **Every arm searches the identical document set** by construction rather than
  by a shared constant.
- **Index build time roughly triples**, and non-linearly: the fleet measured
  **8,408 docs/s at full corpus against 12,229 at 1.2M** for ScyllaDB
  (`../BUILD-RATE-LOOP.md:356`), so a build is ~18 min rather than ~5, four
  times over.

**`--duration` is the per-cell measured window, not a corpus bound.** Do not
reach for it to shorten a build.

### What full corpus costs in memory, per configuration

| Config | Index lives in | Needs | Budget | Verdict |
|---|---|---|---|---|
| 1 — ScyllaDB RAM | vector-store heap | **19.0 GiB** measured, clean rep, zero drops (`../BUILD-RATE-LOOP.md:418`) | `VECTOR_STORE_MEMORY_LIMIT` 26 GiB in a 28g cgroup | fits, ~7 GiB headroom — **unchanged** |
| 2 — ScyllaDB disk | NVMe, page cache | ~27 GB of files | 7.5 TB instance store | trivial — **unchanged** |
| 3 — OpenSearch disk | NVMe | ~27 GB index + ~10 GB `_source` | 7.5 TB | trivial on disk — but see the heap below |
| 4 — OpenSearch tmpfs | tmpfs in the cgroup | **~24 GiB** (`docker/.env.sut:105`) | 12 GiB as shipped | **does not fit. The one config full corpus breaks.** |

**The OpenSearch memory budget is raised, and the JVM heap reservation gives way
to make room.** `.env.sut` records the measured failure: at 8.97M documents the
tmpfs needs ~24 GiB, and *"the variant OOM-killed at 28 GiB on 2026-09-03, which
is why that pass had to be disclosed at a 40 GiB budget."* So:

| Knob | Shipped | **This campaign** | Why |
|---|---|---|---|
| `OS_RAM_INDEX_SIZE` | 12 GiB | **24 GiB** | the repository's own full-corpus arithmetic |
| `OS_HEAP` | `-Xms14g -Xmx14g` | **`-Xms8g -Xmx8g`** | the reservation that has to give. These arms search a settled index; the 14 GiB was sized for the indexing path |
| `OS_MEM_LIMIT` | 28g | **40g** | 24 + 8 + JVM off-heap, and the budget the 2026-09-03 pass already disclosed |

**Both OpenSearch configurations run these numbers, not just A6.** A5 has room
for 14 GiB of heap and does not need the change — but if A5 ran at 28g/14g and
A6 at 40g/8g, the A5↔A6 comparison would confound index location with heap size
and container budget, and that pair is already carrying one confound it cannot
shed. One memory configuration, both arms.

**This ends memory parity with the ScyllaDB arms, and `.env.sut` says so in
advance**: *"Capping ladder points at 1M is what lets these two arms keep the
SAME memory budget as the ScyllaDB arms; raise the cap and that parity is
gone."* Container against container, OpenSearch now has 40g where the
vector-store has 28g. Stack against stack it is still behind — the ScyllaDB pair
holds 56 GiB across two containers. **Both readings go in the footer**; neither
is netted out. Restoring container-level parity would mean raising
`VS_MEM_LIMIT` to 40g too, which is not done here because the vector-store fits
full corpus in 19.0 GiB and a budget it cannot use is not a fair exchange for
one OpenSearch needs.

### The ingest contract: load, then wait, then measure

No latency is measured until the documents are in and the index is built and
serving. That is not an instruction to follow by hand — it is what
`ensure_index` does (`core/src/bootstrap.rs:172-194`), and the two build steps
below are the only runs that invoke it in writing mode.

**The ingest is build-rate's loader at one concurrency, not a second
implementation of it.** `scyllasearch`'s `CqlLoader` and `ossearch`'s loader
both wrap the sibling tree's `ResettingInserters` and call
`sweep::measure_at_concurrency` at **exactly one level** — one rung, never a
ladder. `scylla/src/loader.rs` says why in its own header: *"`--load-concurrency`
is not a result … this runs one rung, chosen to be fast rather than to be
informative, and the docs/s it prints is only there so an operator can see the
bootstrap moving."* How fast an index builds is the build-rate tree's question
and it takes a whole ladder to answer; asking it here would make this campaign
two measurements pretending to be one.

Then the wait, which is four gates and not a sleep:

| Step | What has to be true |
|---|---|
| 1. reset | keyspace/index dropped and recreated, and the new index reaches SERVING **at zero documents** before a single row is sent |
| 2. fill | every document accepted; a load that lost documents is refused here rather than blamed on the engine by the next gate |
| 3. publish | the engine is asked to make what it accepted visible — legitimate here and nowhere in build-rate, because what is timed starts *after* this |
| 4. gate | poll until the index count reaches the corpus count, then compare **exactly**, and confirm the index **answers a query**. A count read from an index that is not serving is a count of something nobody can search |

An unreadable probe is refused rather than retried; an index holding *more* than
the corpus is refused rather than topped up, because it was not built from this
corpus. Only after all four does the first measured cell start.

**`--load-concurrency` is 64 on ScyllaDB and 16 (batch 512) on OpenSearch.**
One level each, engine-bound, ~5 min per build. It is a build knob and has no
bearing on any latency below it — the index is identical whatever rate filled
it, and the sweeps that read it start from a settled, idle index.

### Build the ScyllaDB index, once

```bash
ssh fts-harness 'set -eu
  cd $BENCH && . tools/fleet_env.sh
  make scylla-up && make scylla-wait'
ssh fts-harness 'for p in 9042 16080; do
  timeout 3 bash -c "</dev/tcp/172.31.47.166/$p" && echo "$p open" || echo "$p BLOCKED"
done'
```

Then the build itself — **the one command in this runbook that is allowed to
drop the keyspace**, run as a single one-cell sweep so that the build happens
inside the harness's own gates rather than beside them:

```bash
ssh fts-harness 'REPS=1 LADDER=1 WARMUP=0 DURATION=3 \
                 OUT_DIR=/mnt/nvme/work/results/build \
                 LAT_DIR=/mnt/nvme/work/latencies-build \
                 ~/run-search-arm.sh build-scylla rare_term \
                   --interface cql --rebuild-index --load-concurrency 64'
```

What it does, in the order [`README.md`](README.md) specifies: count the corpus
at the bound, drop the keyspace, recreate the schema **and the index**, wait for
the index to reach SERVING at zero documents, fill it at `--load-concurrency`,
ask the engine to publish, gate on the count, then answer three seconds of
queries to prove the index is serving and not merely counting.

**The index is created before the load, so this is the CDC tail path**, not the
bootstrap base-table scan (`scylla/src/loader.rs`: opening the level is what
resets the keyspace and waits for the new index at zero documents). That is a
header fact and it goes in the write-up; it is not a read-path property once
the index is settled, but "which path built it" is the first thing a reader of
a ScyllaDB FTS number asks.

Expect ~5 min at the ~12,228 docs/s the fleet has measured for this engine.
Gate before going on:

```bash
ssh fts-harness 'curl -s http://172.31.47.166:16080/api/v1/indexes/wiki/articles_body_fts/count'
# expect 8967625
ssh fts-harness 'docker logs fts-bench-vector-store 2>&1 | grep -E "ingest tuning|tantivy worker"'
# expect index=ram -- a disk index here is a different measurement and must be declared
ssh fts-harness 'cd $BENCH && . tools/fleet_env.sh
  docker stats --no-stream --format "{{.Name}} {{.MemUsage}}" fts-bench-vector-store'
# record it: this is the headroom number the 26 GiB budget is checked against
```

**Then delete the build output.** It is a precondition, not campaign data:

```bash
printf 'config 1 scylladb-ram  %s\n' "$(date -u +%FT%TZ)" >> "$R/env/index-builds.txt"
ssh fts-harness 'rm -rf /mnt/nvme/work/results/build /mnt/nvme/work/latencies-build'
```

### Smoke — one short ladder per arm, before the real sweeps

```bash
ssh fts-harness 'REPS=1 LADDER=1,8 DURATION=5 WARMUP=2 \
                 OUT_DIR=/mnt/nvme/work/smoke LAT_DIR=/mnt/nvme/work/smoke-lat \
                 ~/run-search-arm.sh smoke-cql rare_term --interface cql --no-index-build'
ssh fts-harness 'REPS=1 LADDER=1,8 DURATION=5 WARMUP=2 \
                 OUT_DIR=/mnt/nvme/work/smoke LAT_DIR=/mnt/nvme/work/smoke-lat \
                 ~/run-search-arm.sh smoke-vstore rare_term --interface vector-store --no-index-build'
```

Gate on all of it:

- exit code **0** on both, and `errors` is `0` on every row.
- `hits_mean` is near `--limit` (10) and `zero_hit_queries` is **0**. A
  zero-hit class here is the query set built against the wrong prefix, and it is
  cheaper to find now than after six arms.
- `index_docs=8967625` and `index_built_here=false` in both preambles. `true`
  means a sweep rebuilt the index and every earlier number is void.
- `statement=prepared` in the `smoke-cql` preamble. If it reads `literal` here,
  `STATEMENT` is exported somewhere in the session and every CQL sweep after
  this one would inherit it.
- `p50_ms` at `c=1` is a plausible single-request service time, not a timeout.
- `interface=cql` and `interface=vector-store` in column 17, one each.
- the two `p50_ms` values differ — if `cql` and `vector-store` agree exactly at
  `c=1`, one of them is not going where the flag says.
- `$OUT_DIR/run-windows.tsv` has one row per rep with a sane wall.
- `probe_windows` accepts the smoke logs (run the Phase 6e slice against them
  once). It exiting 1 with `duplicate (sweep, concurrency, rep)` means a sweep
  was given more than one class.

The OpenSearch half has no separate smoke: bringing it up requires taking
ScyllaDB down, so its equivalent is the build step's own three seconds of
queries plus the analyzer and count gates in Phase 6c. Read the same list
against `build-os-rep1` before starting A3's sweeps.

**Delete the smoke output before the real sweeps. It is not campaign data.**

```bash
ssh fts-harness 'rm -rf /mnt/nvme/work/smoke /mnt/nvme/work/smoke-lat'
```

---

## Phase 6 — the arms

Four configurations, in this order, each one `*-up` → gate → **build the index
once** → probe → arms → close out → pull → `*-down`:

| # | Configuration | Knob | Arms |
|---|---|---|---|
| 1 | ScyllaDB, index in RAM | `VS_FTS_INDEX_DIR` unset | A1, A2 |
| 2 | ScyllaDB, index on disk | `VS_FTS_INDEX_DIR=/var/lib/vector-store/fts` | A3, A4 |
| 3 | OpenSearch, index on disk | `--index-config disk` | A5 |
| 4 | OpenSearch, index on tmpfs | `OS_RAM_INDEX=1`, `--index-config ramindex` | A6 |

### Four index builds in the whole campaign, and nothing else rebuilds

This is the invariant the ordering exists to serve, stated once so no run line
has to imply it:

| Shares one index, one live stack, no restart | Sweeps | Builds |
|---|---|---|
| **A1 + A2** — both interfaces × all 3 top-k × all 6 classes | 36 | **1** |
| **A3 + A4** — both interfaces × all 3 top-k × all 6 classes | 36 | **1** |
| **A5** — all 3 top-k × all 6 classes | 18 | **1** |
| **A6** — all 3 top-k × all 6 classes | 18 | **1** |

**`--limit` does not touch the index.** Top-k is a query parameter: `k=10`,
`k=100` and `k=1000` read the identical resident index, and the loop that varies
them sits *inside* the configuration, outside the class loop. Nothing between
`k=10` and `k=1000` stops a container, drops a keyspace or re-ingests a
document.

**Neither does the interface.** `cql` and `vector-store` are two doors into the
same vector-store index, which is the entire reason `cql` − `vector-store` is a
meaningful subtraction — it would mean nothing if the two arms had read indexes
built at different moments.

So the whole campaign ingests the corpus **four times**: once per SUT
configuration, because a configuration change is the one thing that does destroy
an index — the RAM index dies with its container, and RAM and disk are different
storage. Four builds at ~17 min is ~1.1 h of the campaign. **Restarts and
rebuilds are not what makes this long**; 2,619 measured cells at 25 s are.

Every measured sweep carries `--no-index-build`, which turns any accidental
rebuild into a refusal on the flags rather than a silent re-ingest, and Phase 7
gates on `index_built_here=false` across every CSV in the campaign.

**The order is not arbitrary.** The deployment-normal comparison — A1/A2 against
A5 — is configurations 1 and 3, so a session that runs out of time still has the
headline. Configurations 2 and 4 are the off-diagonal cells that turn the
diagonal into a plane, and they are the ones to defer.

Within a configuration the stack **never** comes down: A1 and A2 read one index,
A3 and A4 read another. Between configurations it always does — the
configuration is the change, and on both engines the index does not survive it
anyway. **An arm is not finished until its `pull_arm` returns zero.**

### 6a. Start the probe — one per arm, on the SUT

One probe per arm, sampling every container in the stack at 1 Hz. It is the one
component `DOCKER_HOST` cannot carry — it reads `/sys/fs/cgroup` where it runs —
so it goes through `tools/sut_probe.sh`, which starts it detached on the SUT.
**Running it on the harness instead records the generator box's idle cgroups and
reports them as engine numbers**; this repository has paid for that mistake
once. **Do not pass `--output`** — the wrapper appends it.

```bash
a=a1-cql-ram      # then a=a2-vstore-ram, without restarting the stack;
                  # then a3-cql-disk / a4-vstore-disk on configuration 2
ssh fts-harness "set -eu
  cd \$BENCH && . tools/fleet_env.sh
  mkdir -p /mnt/nvme/work/probe
  tools/sut_probe.sh start /mnt/nvme/work/probe/$a.jsonl \
      --engine scylladb \
      --containers fts-bench-scylla:scylladb \
      --containers fts-bench-vector-store:vector-store \
      --vs-url http://127.0.0.1:16080 --keyspace wiki --vs-index articles_body_fts \
      --interval 1 --duration 0 --label 'search-latency $a' \
      --corpus /mnt/nvme/data/corpus.jsonl"
```

And the harness box's own CPU, which is this campaign's only client-headroom
signal:

```bash
ssh fts-harness 'cat > ~/sample-box-cpu.sh << "EOF"
#!/bin/bash
# 1 Hz whole-box CPU on the harness box: epoch, busy and total jiffies.
# APPENDS. Never truncate: one file covers every arm of the session and a
# consumer cuts it to a cell window.
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

`--memory-read` per arm, required and never defaulted:

| Arm | Read | Why |
|---|---|---|
| A1, A2 | **`anon`** | the vector-store index is in RAM and anonymous; ScyllaDB's is anon too |
| A3, A4 | **`anon+cache`** | the Tantivy index is now file-backed on the NVMe, so what a read touches is page cache. Reading `anon` alone here would report an index that occupies nothing — and the A1↔A3 difference is precisely what this arm exists to measure |
| A5 | **`anon+cache`** | the Lucene segments are on the NVMe, same reasoning |
| A6 | **`anon+shmem`** | tmpfs is shmem, not anon. `rss_bytes` alone hides up to 12 GiB of index |

**Getting this wrong does not fail anything.** It silently reports the wrong
number for the one variable the 2×2 was run to isolate, so it is checked against
the arm directory name before the probe starts, not after.

### 6b. Configuration 1 — ScyllaDB with the index in RAM (A1, A2)

Both run against the index Phase 5 built, on the stack that is already up.
**Nothing between these two arms brings the stack down.**

The matrix: the full ladder against every class, one sweep per class. Each loop
is six sweeps of three reps, 144 cells, ~60 min.

```bash
CLASSES="rare_term common_term phrase bool_and bool_not bool_mixed"

for k in 10 100 1000; do for c in $CLASSES; do
  ssh fts-harness "REPS=3 LIMIT=$k OUT_DIR=/mnt/nvme/work/results/a1-cql-ram \
                   LAT_DIR=/mnt/nvme/work/latencies/a1-cql-ram \
                   ~/run-search-arm.sh a1-cql-ram-k$k-ladder-$c $c \
                     --interface cql --no-index-build"
done; done

for k in 10 100 1000; do for c in $CLASSES; do
  ssh fts-harness "REPS=3 LIMIT=$k OUT_DIR=/mnt/nvme/work/results/a2-vstore-ram \
                   LAT_DIR=/mnt/nvme/work/latencies/a2-vstore-ram \
                   ~/run-search-arm.sh a2-vstore-ram-k$k-ladder-$c $c \
                     --interface vector-store --no-index-build"
done; done
```

**Run the classes in the order given.** It is the order
`ftsbench.generate_queries` writes and the order the class chart draws, and it
puts `phrase` — the class the laptop pass flagged and the one most likely to
knee early — third rather than last, where a shortened session would lose it.

The optional axes, cut first if the session is short:

```bash
# the projection, A1 only on this engine -- A2 cannot serve it
for k in 10 100 1000; do
  ssh fts-harness "REPS=3 LADDER=16 LIMIT=$k OUT_DIR=/mnt/nvme/work/results/a1-cql-ram \
                   LAT_DIR=/mnt/nvme/work/latencies/a1-cql-ram \
                   ~/run-search-arm.sh a1-cql-ram-k$k-fetch-rare_term rare_term \
                     --interface cql --fetch-documents --no-index-build"
done
# At k=1000 this projects a thousand title+body pairs per query. That is the
# largest transfer the campaign asks for and it is the point of the sweep.

# the refusal, which must fail on the flags before it connects
ssh fts-harness '/mnt/nvme/work/target/release/scyllasearch \
                   --corpus /mnt/nvme/data/corpus.jsonl \
                   --queries /mnt/nvme/work/queries-enwiki.json \
                   --concurrency 1 --interface vector-store --fetch-documents \
                   ; echo "exit=$?"' 2>&1 | tail -4
# expect a non-zero exit naming --fetch-documents, and NO connection attempt
```

### 6c. Configuration 2 — ScyllaDB with the index on disk (A3, A4)

Only after A1 and A2 have been closed out, pulled and verified: this takes their
stack down, and the RAM index goes with it.

```bash
ssh fts-harness 'cd $BENCH && . tools/fleet_env.sh && make scylla-down'
ssh fts-harness 'set -eu
  cd $BENCH && . tools/fleet_env.sh
  VS_FTS_INDEX_DIR=/var/lib/vector-store/fts make scylla-up
  VS_FTS_INDEX_DIR=/var/lib/vector-store/fts make scylla-wait'
```

The knob reaches the container through compose's `${VAR:+=${VAR}}` form
(`docker/docker-compose.scylla.yml:79`), so an **unset** variable is dropped
rather than passed empty — which is exactly how A1 and A2 got a RAM index
without naming anything.

**The gate, and it is blocking.** An image built before `94a23ef2` accepts
`VECTOR_STORE_FTS_INDEX_DIR` and ignores it **in silence**, which would give
A3 and A4 a RAM index under a disk label — the one mislabelling this campaign
cannot detect after the fact, because index location is not a CSV column:

```bash
ssh fts-harness 'docker logs fts-bench-vector-store 2>&1 | grep -E "ingest tuning|tantivy worker"'
# expect index=disk:/var/lib/vector-store/fts
# index=ram here means the knob was ignored -- STOP, and check the image tag
```

Build the index for this configuration — a second full build, because nothing
survived the restart:

```bash
ssh fts-harness 'REPS=1 LADDER=1 WARMUP=0 DURATION=3 \
                 OUT_DIR=/mnt/nvme/work/results/build \
                 LAT_DIR=/mnt/nvme/work/latencies-build \
                 ~/run-search-arm.sh build-scylla-disk rare_term \
                   --interface cql --rebuild-index --load-concurrency 64'
ssh fts-harness 'curl -s http://172.31.47.166:16080/api/v1/indexes/wiki/articles_body_fts/count'
# expect 8967625
ssh fts-harness 'rm -rf /mnt/nvme/work/results/build /mnt/nvme/work/latencies-build'
```

Then the two arms, probe started with **`--memory-read anon+cache`**:

```bash
for k in 10 100 1000; do for c in $CLASSES; do
  ssh fts-harness "REPS=3 LIMIT=$k OUT_DIR=/mnt/nvme/work/results/a3-cql-disk \
                   LAT_DIR=/mnt/nvme/work/latencies/a3-cql-disk \
                   ~/run-search-arm.sh a3-cql-disk-k$k-ladder-$c $c \
                     --interface cql --no-index-build"
done; done

for k in 10 100 1000; do for c in $CLASSES; do
  ssh fts-harness "REPS=3 LIMIT=$k OUT_DIR=/mnt/nvme/work/results/a4-vstore-disk \
                   LAT_DIR=/mnt/nvme/work/latencies/a4-vstore-disk \
                   ~/run-search-arm.sh a4-vstore-disk-k$k-ladder-$c $c \
                     --interface vector-store --no-index-build"
done; done

for k in 10 100 1000; do
  ssh fts-harness "REPS=3 LADDER=16 LIMIT=$k OUT_DIR=/mnt/nvme/work/results/a3-cql-disk \
                   LAT_DIR=/mnt/nvme/work/latencies/a3-cql-disk \
                   ~/run-search-arm.sh a3-cql-disk-k$k-fetch-rare_term rare_term \
                     --interface cql --fetch-documents --no-index-build"
done
```

Close out and pull both before the next configuration.

### 6d. Configuration 3 — OpenSearch with the index on disk (A5)

```bash
ssh fts-harness 'cd $BENCH && . tools/fleet_env.sh && make scylla-down'
ssh fts-harness 'set -eu
  cd $BENCH && . tools/fleet_env.sh
  OS_MEM_LIMIT=40g OS_HEAP="-Xms8g -Xmx8g" make os-up
  OS_MEM_LIMIT=40g OS_HEAP="-Xms8g -Xmx8g" make os-wait'
ssh fts-harness 'timeout 3 bash -c "</dev/tcp/172.31.47.166/9200" && echo "9200 open"'
```

**The memory knobs go on every `make` call of both OpenSearch configurations.**
A shell variable beats `--env-file` for compose substitution, which is how these
override `.env.sut` without editing a file both build-rate runbooks read. Read
them back off the container rather than trusting the line that set them:

```bash
ssh fts-harness 'cd $BENCH && . tools/fleet_env.sh
  docker inspect -f "{{.HostConfig.Memory}}" fts-bench-opensearch      # 42949672960
  docker exec fts-bench-opensearch bash -c "echo \$OPENSEARCH_JAVA_OPTS"'  # -Xms8g -Xmx8g
```

**`--index-config disk`, not the default.** `ossearch`'s default is `ramindex`,
whose mapping sets `_source: {"enabled": false}` — an index that stores no
document body at all. Three consequences, and the first one is silent:

- `--fetch-documents` against it asks for `_source: ["title","body"]` and gets
  nothing back, so the projection arm would time an empty transfer and report
  it as a document fetch.
- [`../COMPARABILITY.md`](../COMPARABILITY.md)'s framing is that both engines do
  the same two jobs — durably store the text and maintain an inverted index
  over it. An index with `_source` disabled is doing one of them.
- `refresh_interval` is 3 s there and 1 s on the disk config. Once the index is
  complete and idle neither reaches the read path, but the header should say
  1 s beside the ScyllaDB half's 1 s commit interval rather than explaining a 3.

The RAM-versus-disk question the default exists to answer is a *build* question.
On a settled read benchmark with 61 GiB of RAM and a ~10 GB index, the segments
are in page cache after the first warm-up either way — which is why `anon+cache`
is A3's memory read, and why running `ramindex` too is a cost lever rather than
a requirement.

Build the index, once:

```bash
ssh fts-harness 'REPS=1 LADDER=1 WARMUP=0 DURATION=3 \
                 OUT_DIR=/mnt/nvme/work/os-results/build \
                 LAT_DIR=/mnt/nvme/work/os-latencies-build \
                 ~/run-os-arm.sh build-os rare_term \
                   --rebuild-index --index-config disk \
                   --refresh-interval 1s \
                   --load-concurrency 16 --load-batch-size 512'
```

**The analyzer gate.** `ossearch` probes the analyzer whether or not it built
the index, and this is the campaign's single most load-bearing fairness check —
the laptop pass shipped with parity broken and its recall figures are void
because of it. Two ways it can pass without checking anything, both of which
must be excluded:

```bash
ssh fts-harness 'grep -E "m1_parity|analyzer check skipped" \
  /mnt/nvme/work/os-results/build/build-os-rep1.stderr.tsv'
# expect: "m1_parity analyzer matches on one probe"
# a line saying "analyzer check skipped: <config> declares no m1_parity analyzer"
# means the config carries no parity analyzer and the check did nothing
```

- **`--no-analyzer-check` must never be passed here.** It is in the null-sink
  runbook because a mock cannot answer `_analyze`; against a real OpenSearch it
  turns the gate off and leaves `analyzer_check=true` nowhere in the header to
  contradict it.
- **A config that declares no `m1_parity` analyzer skips the check and returns
  Ok** (`build-rate/opensearch/src/reset.rs:318-325`). Both shipped configs
  declare it; a hand-written one passed by path might not.

Then the count and the full probe set:

```bash
ssh fts-harness 'curl -s "http://172.31.47.166:9200/wiki-articles/_count"'   # 8967625
ssh fts-harness 'cd $BENCH && OS_URL=http://172.31.47.166:9200 \
                 opensearch/verify_analyzer.sh wiki-articles'   # exits non-zero on divergence
ssh fts-harness 'rm -rf /mnt/nvme/work/os-results/build /mnt/nvme/work/os-latencies-build'
```

Start A5's probe (`--engine opensearch`, `--memory-read anon+cache`):

```bash
a=a5-os-disk      # a6-os-ram on configuration 4, with --memory-read anon+shmem
ssh fts-harness "set -eu
  cd \$BENCH && . tools/fleet_env.sh
  mkdir -p /mnt/nvme/work/probe
  tools/sut_probe.sh start /mnt/nvme/work/probe/$a.jsonl \
      --engine opensearch --containers fts-bench-opensearch:opensearch \
      --os-url http://127.0.0.1:9200 --os-index wiki-articles \
      --interval 1 --duration 0 --label 'search-latency $a' \
      --corpus /mnt/nvme/data/corpus.jsonl"
```

and run the same six-class matrix:

```bash
for k in 10 100 1000; do for c in $CLASSES; do
  ssh fts-harness "REPS=3 LIMIT=$k OUT_DIR=/mnt/nvme/work/os-results/a5-os-disk \
                   LAT_DIR=/mnt/nvme/work/os-latencies/a5-os-disk \
                   ~/run-os-arm.sh a5-os-disk-k$k-ladder-$c $c --no-index-build"
done; done

for k in 10 100 1000; do
  ssh fts-harness "REPS=3 LADDER=16 LIMIT=$k OUT_DIR=/mnt/nvme/work/os-results/a5-os-disk \
                   LAT_DIR=/mnt/nvme/work/os-latencies/a5-os-disk \
                   ~/run-os-arm.sh a5-os-disk-k$k-fetch-rare_term rare_term \
                     --fetch-documents --no-index-build"
done
```

Close out and pull A5 before the next configuration.

### 6e. Configuration 4 — OpenSearch with the index on tmpfs (A6)

```bash
ssh fts-harness 'cd $BENCH && . tools/fleet_env.sh && make os-down'
ssh fts-harness 'set -eu
  cd $BENCH && . tools/fleet_env.sh
  OS_RAM_INDEX=1 OS_RAM_INDEX_SIZE=25769803776 \
  OS_MEM_LIMIT=40g OS_HEAP="-Xms8g -Xmx8g" make os-up
  OS_RAM_INDEX=1 OS_RAM_INDEX_SIZE=25769803776 \
  OS_MEM_LIMIT=40g OS_HEAP="-Xms8g -Xmx8g" make os-wait'
```

**`OS_RAM_INDEX=1` must be on every `make` call of this configuration**, not
just the first — it selects a compose overlay
(`docker/docker-compose.opensearch.ramindex.yml`), and a later call without it
addresses a different composition. The gate is the data path, read off the
container rather than off the environment that was supposed to set it:

```bash
ssh fts-harness 'cd $BENCH && . tools/fleet_env.sh
  docker exec fts-bench-opensearch df -h /usr/share/opensearch/data | tail -1'
# expect a tmpfs mount of 24 GiB. A disk path here is configuration 3 again,
# and its numbers would be A5 wearing A6 label. 12 GiB here means
# OS_RAM_INDEX_SIZE did not reach compose, and the build will ENOSPC near 4M.
```

Then the build. **`--refresh-interval 1s` is forced**, overriding the ramindex
config's own 3 s, so that refresh is not a third variable between A5 and A6:

```bash
ssh fts-harness 'REPS=1 LADDER=1 WARMUP=0 DURATION=3 \
                 OUT_DIR=/mnt/nvme/work/os-results/build \
                 LAT_DIR=/mnt/nvme/work/os-latencies-build \
                 ~/run-os-arm.sh build-os-ram rare_term \
                   --rebuild-index --index-config ramindex \
                   --refresh-interval 1s \
                   --load-concurrency 16 --load-batch-size 512'
ssh fts-harness 'curl -s "http://172.31.47.166:9200/wiki-articles/_count"'   # 8967625
ssh fts-harness 'grep -E "m1_parity|analyzer check skipped" \
  /mnt/nvme/work/os-results/build/build-os-ram-rep1.stderr.tsv'
ssh fts-harness 'rm -rf /mnt/nvme/work/os-results/build /mnt/nvme/work/os-latencies-build'
```

**Watch the tmpfs during this build. It is the campaign's tightest fit and the
only one with a measured failure behind it.** 8,967,625 documents need ~24 GiB
of tmpfs (`docker/.env.sut:105`), which is the whole of the raised
`OS_RAM_INDEX_SIZE`, and the merge transient holds old and merged segments at
once. The same variant **OOM-killed at a 28 GiB budget on 2026-09-03** with the
14 GiB heap in place; 40g with an 8 GiB heap is what replaces it. An ENOSPC or
an OOM mid-build is the expected failure mode and neither is silent — the
harness's count gate refuses rather than measuring what fits, and the container
exit code says which one happened.

```bash
ssh fts-harness 'docker exec fts-bench-opensearch df -h /usr/share/opensearch/data | tail -1'
# record the used figure: it is the evidence behind the headroom claim
```

Then the arm — six sweeps, **no projection sweep**, because this index stores no
`_source` to project:

```bash
for k in 10 100 1000; do for c in $CLASSES; do
  ssh fts-harness "REPS=3 LIMIT=$k OUT_DIR=/mnt/nvme/work/os-results/a6-os-ram \
                   LAT_DIR=/mnt/nvme/work/os-latencies/a6-os-ram \
                   ~/run-os-arm.sh a6-os-ram-k$k-ladder-$c $c --no-index-build"
done; done
```

### 6d. What to watch while a sweep runs

The script tails the last eight outcome lines of each rep. Read them:

```
  -> 14231 queries in 20.00s = 711.6 q/s, p50 1.9 / p90 3.1 / p99 5.4 ms, 0 errors
```

- **`0 errors`** on every line. A non-zero count is a request that contributed
  nothing to the distribution because its time went on whatever went wrong.
- **`!! every query in <class> matched nothing`** is the run telling you the
  query set and the index disagree. Stop; do not collect the arm.
- **`p50` roughly flat across the ladder** is the closed loop behaving — service
  time is not supposed to move much until the engine saturates. `p50` climbing
  in step with concurrency from `c=1` is a queue somewhere, and on this harness
  there is no queue between the workers and the engine, so it is the engine's.
- **`q/s` flattening** is the plateau; the level where it flattens is the
  chart's most interesting point and the one the write-up has to name.

### 6e. Close the arm out, on the harness, before `*-down`

**Why per arm rather than once at the end.** `*-down` removes the containers and
their startup lines go with them; `verify_cpu_usage` reads each container's
quota with `docker inspect`, so it cannot be deferred to laptop work either.
The numbers and the log that licenses them come home together or the arm is
unlabelled data.

```bash
a=a1-cql-ram; mread=anon    # a2-vstore-ram: same
                            # a3-cql-disk, a4-vstore-disk: mread=anon+cache
                            # a5-os-disk: anon+cache   a6-os-ram: anon+shmem
ssh fts-harness "set -eu
  cd \$BENCH && . tools/fleet_env.sh
  L=/mnt/nvme/work/logs/$a; mkdir -p \$L
  docker logs fts-bench-vector-store > \$L/vector-store.log 2>&1
  docker logs fts-bench-scylla       > \$L/scylla.log 2>&1
  cp docker/.env.sut \$L/env.sut
  docker image inspect --format '{{.Id}} {{index .RepoTags 0}}' \
      \$(grep '^VECTOR_STORE_IMAGE=' docker/.env.sut | cut -d= -f2-) > \$L/image.txt
  tools/sut_probe.sh stop /mnt/nvme/work/probe/$a.jsonl
  .venv/bin/python3 -m ftsbench.probe_windows --arm $a \
      --probe /mnt/nvme/work/probe/$a.jsonl \
      --stderr '/mnt/nvme/work/results/$a/*-rep*.stderr.tsv' \
      --out-dir /mnt/nvme/work/probe/$a --memory-read $mread \
      --table \$L/resource-by-cell.csv
  .venv/bin/python3 -m ftsbench.verify_cpu_usage --data-dir /mnt/nvme/work/probe/$a \
      --containers fts-bench-scylla fts-bench-vector-store \
      --output-json \$L/cpu-utilisation.json"
```

**All of it runs inside the `ssh`, while the stack is still up.**

For A5 and A6 the block is the same with `docker logs fts-bench-opensearch >
\$L/opensearch.log 2>&1`, `docker image inspect …
opensearchproject/opensearch:3.8.0 > \$L/image.txt`, the stderr glob under
`/mnt/nvme/work/os-results/$a/`, no vector-store or scylla log, and
`--containers fts-bench-opensearch`.

**Capture the index-location evidence in the same block, per configuration.**
It is the one fact the CSVs do not carry, so if it is not in the arm's log
directory it does not exist:

```bash
# configurations 1 and 2, into $L/index-location.txt
docker logs fts-bench-vector-store 2>&1 | grep -oE "index=(ram|disk:[^ ]*)" | tail -1
# configurations 3 and 4
docker exec fts-bench-opensearch df -h /usr/share/opensearch/data | tail -1
```

**`--containers` is not optional**: `verify_cpu_usage`'s built-in list is the
ScyllaDB pair, which would report nothing at all on A3.

`probe_windows` exiting 1 with `duplicate (sweep, concurrency, rep)` means some
sweep ran more than one class. The fix is to re-run that sweep one class at a
time — the stack is still up, which is the only moment that is cheap.

### 6f. Pull the arm home, from the laptop

```bash
pull_arm() {              # pull_arm <arm-dir> <engine> <results-root> <latencies-root>
    local a="$1" d="$R/$1/$2" src="$3" lat="$4"
    test -n "$R" && test -d "$d" || { echo "no such arm directory: $d" >&2; return 1; }
    scp    "fts-harness:$src/$a/"*.csv                  "$d/points/"        || return 1
    scp -r "fts-harness:$lat/$a/"*                      "$d/latencies/"     || return 1
    scp    "fts-harness:$src/$a/run-windows.tsv"        "$d/logs/"          || return 1
    scp    "fts-harness:$src/$a/"*.stderr.tsv           "$d/logs/"          || return 1
    scp -r "fts-harness:/mnt/nvme/work/logs/$a/"*       "$d/logs/"          || return 1
    scp    "fts-harness:/mnt/nvme/work/probe/$a.jsonl"  "$R/sut/cpu-$a.jsonl" || return 1
    scp -r "fts-harness:/mnt/nvme/work/probe/$a/"*      "$d/probe/"         || return 1
    # The harness box sampler appends across the whole session, so every arm
    # gets the same file and cuts its own cell windows out of it.
    scp    "fts-harness:/tmp/box-cpu.tsv"               "$d/logs/"          || return 1
    local n; n=$(ls "$d"/points/*.csv 2>/dev/null | wc -l)
    [ "$n" -ge 18 ] || { echo "$a: $n point CSVs, expected >=18 (6 classes x 3 reps)" >&2; return 1; }
    local k; k=$(awk -F, '!/^#/ && $1!="concurrency"' "$d"/points/*.csv | wc -l)
    [ "$k" -ge 144 ] || { echo "$a: $k data rows, expected >=144 (6 classes x 8 levels x 3 reps)" >&2; return 1; }
    zero_hits "$d/points" || return 1
    rss_breach "$d/logs/resource-by-cell.csv" || return 1
    echo "$a: home"
}

# A cell where every query matched nothing still has a p99, and it plots as the
# cheapest point on the curve. The harness exits non-zero over it; this is the
# same refusal read off the artifacts, so a missed exit code cannot hide it.
zero_hits() {
    awk -F, 'FNR==1 { h=0 } /^#/ { next }
        !h { for (i=1;i<=NF;i++) c[$i]=i; h=1; next }
        $(c["zero_hit_queries"]) + 0 > 0 {
            print "ZERO HITS", FILENAME, "c="$(c["concurrency"]), $(c["query_class"]),
                  $(c["zero_hit_queries"])"/"$(c["queries"]); bad=1 }
        END { exit bad }' "$1"/*.csv >&2
}

# The memory gate. The vector-store stops adding documents at its budget and
# keeps answering queries; on this campaign that would have happened during the
# Phase 5 build, but a breach during the sweeps means reclaim is in the tail.
rss_breach() {
    awk -F, -v OFS=, 'NR==1 { for (i=1;i<=NF;i++) c[$i]=i; next }
        $(c["mem_headroom_bytes"]) != "" && $(c["mem_headroom_bytes"]) <= 0 {
            print "BREACH", $(c["sweep"]), $(c["concurrency"]), $(c["rep"]), \
                  $(c["container"]), $(c["mem_peak_bytes"]); bad=1 }
        END { exit bad }' "$1" >&2
}

# configuration 1, before its stack comes down
pull_arm a1-cql-ram     scylla     /mnt/nvme/work/results    /mnt/nvme/work/latencies
pull_arm a2-vstore-ram  scylla     /mnt/nvme/work/results    /mnt/nvme/work/latencies
# configuration 2
pull_arm a3-cql-disk    scylla     /mnt/nvme/work/results    /mnt/nvme/work/latencies
pull_arm a4-vstore-disk scylla     /mnt/nvme/work/results    /mnt/nvme/work/latencies
# configurations 3 and 4
pull_arm a5-os-disk     opensearch /mnt/nvme/work/os-results /mnt/nvme/work/os-latencies
pull_arm a6-os-ram      opensearch /mnt/nvme/work/os-results /mnt/nvme/work/os-latencies
```

**A non-zero return is the arm's gate, not a warning**: the stack that produced
it is still up, which is the only moment re-running a lost rep is cheap.

**A point CSV existing is not a finished run.** `--out` is opened before the
first query, so the count gate passes while the last rep is still going. Check
the sweep's last stderr log for its final `->` line before trusting the count.

Then, and only then:

```bash
ssh fts-harness 'cd $BENCH && . tools/fleet_env.sh && make scylla-down'   # after A1+A2
ssh fts-harness 'cd $BENCH && . tools/fleet_env.sh && make os-down'       # after A3
```

---

## Phase 7 — verify all six arms came home

From `$R` alone, **before the stop**:

```bash
# 1. every arm has its sweeps, and every sweep has its reps (18, +3 with fetch)
for a in "$R"/a?-*/; do
  echo "$(basename "$a") $(ls "$a"*/points/*.csv 2>/dev/null | wc -l)"
done   # expect 63 54 63 54 63 54 across a1..a6
       # 6 classes x 3 top-k x 3 reps = 54, plus 9 fetch sweeps on a1, a3, a5

# 2. the interface column is what the directory claims (cols 16,17)
for a in a1-cql-ram:scylladb/cql a2-vstore-ram:scylladb/vector-store \
         a3-cql-disk:scylladb/cql a4-vstore-disk:scylladb/vector-store \
         a5-os-disk:opensearch/http a6-os-ram:opensearch/http; do
  d=${a%%:*}; want=${a##*:}
  got=$(awk -F, '!/^#/ && $1!="concurrency" {print $16"/"$17}' \
        "$R/$d"/*/points/*.csv | sort -u)
  [ "$got" = "$want" ] || echo "MISLABELLED $d: want $want, got $got"
done

# 2b. THE INDEX LOCATION, which is in no column and only in the arm logs.
#     Without this the 2x2 is four arms with two of them possibly duplicates.
grep -h . "$R"/a1-cql-ram/scylla/logs/index-location.txt \
          "$R"/a2-vstore-ram/scylla/logs/index-location.txt      # index=ram
grep -h . "$R"/a3-cql-disk/scylla/logs/index-location.txt \
          "$R"/a4-vstore-disk/scylla/logs/index-location.txt     # index=disk:/var/...
grep -h . "$R"/a5-os-disk/opensearch/logs/index-location.txt     # an NVMe path
grep -h . "$R"/a6-os-ram/opensearch/logs/index-location.txt      # tmpfs, ~12 GiB

# 3. nothing rebuilt the index mid-campaign, and every arm measured the same one
grep -h "^# index_built_here" "$R"/*/*/points/*.csv | sort -u   # false, only

# 3a. exactly four ingests happened in the whole campaign -- one per SUT
#     configuration. Every top-k and both interfaces shared the index their
#     configuration built, so anything above 4 is a rebuild nobody asked for.
ls "$R"/env/index-builds.txt && wc -l < "$R"/env/index-builds.txt   # 4
grep -h "^# index_docs"       "$R"/*/*/points/*.csv | sort -u   # 8967625, only

# 3b. every CQL sweep prepared its statements. An exported STATEMENT would have
#     beaten the script default silently, and this is the only thing that says so.
grep -h "^# statement" "$R"/a{1-cql-ram,3-cql-disk}/scylla/points/*.csv \
  | sort -u   # prepared, only

# 4. the analyzer gate ran and passed on BOTH OpenSearch arms
grep -h "^# analyzer_check" "$R"/a{5-os-disk,6-os-ram}/opensearch/points/*.csv \
  | sort -u   # true
grep -rh "m1_parity analyzer matches" "$R"/a{5,6}-os-*/opensearch/logs/*.stderr.tsv | wc -l
grep -rh "analyzer check skipped"     "$R"/a{5,6}-os-*/opensearch/logs/*.stderr.tsv   # nothing

# 4b. the two OpenSearch arms differ in _source, and that is expected and
#     disclosed -- but it must be TRUE, not assumed.
grep -h "^# index_config" "$R"/a5-os-disk/opensearch/points/*.csv | sort -u  # disk
grep -h "^# index_config" "$R"/a6-os-ram/opensearch/points/*.csv | sort -u   # ramindex

# 5. no failed queries anywhere (errors is col 4)
awk -F, '!/^#/ && $1!="concurrency" && $4+0>0 {print FILENAME": errors="$4}' \
  "$R"/*/*/points/*.csv

# 6. no blank percentiles. A blank p50 is a cell that measured nothing.
awk -F, '!/^#/ && $1!="concurrency" && ($7=="" || $9=="") {print FILENAME" c="$1" "$2}' \
  "$R"/*/*/points/*.csv

# 7. no zero-hit cell survived into the artifacts (col 12)
awk -F, '!/^#/ && $1!="concurrency" && $12+0>0 {print FILENAME" c="$1" "$2": "$12"/"$3}' \
  "$R"/*/*/points/*.csv

# 8. limit and fetch_documents agree with the sweep name on every data row.
#     The file name is the only thing that says which k a row was meant to be.
for f in "$R"/*/*/points/*.csv; do
  want=$(basename "$f" | sed -n 's/.*-k\([0-9]\+\)-.*/\1/p')
  [ -n "$want" ] || continue          # the build and smoke files carry no k
  awk -F, -v w="$want" -v n="$f" '!/^#/ && $1!="concurrency" && $14 != w {
        print n": limit="$14" but the file name says k"w; }' "$f"
done

# 8b. every arm carries all three top-k values, and nothing else
awk -F, '!/^#/ && $1!="concurrency" {print $14}' "$R"/*/*/points/*.csv \
  | sort -un   # expect exactly 10 100 1000
awk -F, '!/^#/ && $1!="concurrency" && $15=="true" {print FILENAME}' \
  "$R"/*/*/points/*.csv | sort -u          # only the *-fetch-* sweeps

# 9. every cell left a distribution, and none is a header with nothing under it
for d in "$R"/*/*/latencies/*/; do echo "$(ls "$d" | wc -l) $(basename "$d")"; done
awk 'ENDFILE { if (FNR < 100) print FILENAME": "FNR" samples" }' \
  "$R"/*/*/latencies/*/*.csv

# 10. every cell of every arm carries a CPU and RSS reading, and none breached
ls "$R"/*/*/logs/resource-by-cell.csv | wc -l        # 6
for f in "$R"/*/*/logs/resource-by-cell.csv; do rss_breach "$f" || echo "^ $f"; done
ls "$R"/sut/cpu-a?-*.jsonl | wc -l                   # 6

# 11. the harness box was not the thing being measured
head -2 "$R"/a1-cql-ram/scylla/logs/box-cpu.tsv
tail -1 "$R"/a1-cql-ram/scylla/logs/box-cpu.tsv

# 12. the 2x2 is four distinct measurements, not two pairs of duplicates.
#     If a location arm matches its twin to within noise that is a RESULT --
#     but it must be read off the numbers, never assumed from the config.
for pair in "a1-cql-ram a3-cql-disk" "a2-vstore-ram a4-vstore-disk" \
            "a5-os-disk a6-os-ram"; do
  set -- $pair
  echo "== $1 vs $2, rare_term p99 at c=16"
  for d in "$1" "$2"; do
    awk -F, -v d="$d" '!/^#/ && $1=="16" && $2=="rare_term" && $15=="false" \
        {s+=$9; n++} END {if (n) printf "  %-16s %.3f ms (n=%d)\n", d, s/n, n}' \
      "$R/$d"/*/points/*.csv
  done
done
```

### The client-headroom read, which is a judgement and not a gate

Cut `box-cpu.tsv` to each cell's own window using the `[k/n] concurrency=C
class=X` announcement that opens it in the stamped stderr log, and compute
`box_cores` — busy jiffies over total, times 8 — median and peak.

There is **no measured harness floor for the read path**: the loader floors in
[`../TUNING.md`](../TUNING.md) are write-path numbers and the read-path
equivalent is what [`HARNESS-AWS-RUNBOOK.md`](HARNESS-AWS-RUNBOOK.md) exists to
produce once `engine-mock` can answer a search. Until then:

| `box_cores` peak | Verdict | What may be said |
|---|---|---|
| ≤ 4 of 8 | `ok` | the plateau is the engine's |
| 4–7 of 8 | `close` | the plateau is plotted and **named in the footer** as possibly client-bound |
| ≥ 7 of 8 | `HARNESS` | the cell measured the harness; it is plotted, ringed, and may not be called an engine throughput |
| no samples | `?` | **not a pass.** An unmeasured gate must never render as a passed gate |

Expect this to matter only at `c=64` and `c=128`, and expect A3 to reach it
first — the HTTP client has always been the heavier of the two.

### Render before the stop

If the charts cannot be produced from `$R` without touching the fleet, **the
pull is not finished** — a five-minute fix now, or a re-entry plus three
re-measured arms after the stop. The renderer is a separate script written
against the seventeen columns and lives outside this tree; run whatever exists
and confirm it reads the whole matrix:

```bash
grep -h "^concurrency," "$R"/*/*/points/*.csv | sort -u | wc -l    # 1 header shape

# six series-by-arm, 144 matrix rows each (+3 where the projection axis ran)
awk -F, '!/^#/ && $1!="concurrency" {print $16"-"$17}' "$R"/*/*/points/*.csv | sort | uniq -c

# the matrix is complete: every (class, level) pair present 3 times, per arm
for a in a1-cql-ram/scylla a2-vstore-ram/scylla a3-cql-disk/scylla \
         a4-vstore-disk/scylla a5-os-disk/opensearch a6-os-ram/opensearch; do
  echo "== $a"
  awk -F, '!/^#/ && $1!="concurrency" && $15=="false" {print $2, $1}' \
      "$R/$a"/points/*.csv | sort | uniq -c | awk '$1!=3 {print "  NOT 3 REPS:", $0}'
done
```

**The four charts are now six facets each**, one per query class, because the
matrix has a class dimension the `README.md` contract does not: X is
`concurrency`, Y is one of the four columns, a series is `engine` + `interface`,
and `query_class` selects the facet. `rare_term` is the headline facet. Do not
collapse classes into one line — averaging `phrase` into `rare_term` would hide
the 4.8× spread the full matrix was run to measure.

---

## Phase 8 — stop the boxes

**Before stopping: every artifact is on the laptop**, because `/mnt/nvme` is
about to be destroyed. Nothing in Phase 6f or 7 can be done after this section.

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
report.** "I initiated the stop" is not the same as "they are stopped".

The `~/.ssh/config` entries now point at released IPs and must be re-pointed on
the next start.

---

## The SUT, for the record

**The 50/50 cgroup split** — `docker/.env.sut`, applied by compose as
`mem_limit` / `cpus` / `cpuset` on each service:

| Service | `cpuset` | `cpus` | `mem_limit` | In-process budget |
|---|---|---|---|---|
| ScyllaDB — `--smp 4 --memory 24G --overprovisioned 0` | `0-3` | 4 | 28g | 24 GiB |
| vector-store | `4-7` | 4 | 28g | `VECTOR_STORE_MEMORY_LIMIT` = 26 GiB — holds full corpus in **19.0 GiB** measured |
| OpenSearch — **raised for this campaign** | `4-7` | 4 | **40g** | **8 GiB heap** + up to 24 GiB tmpfs on A6 |

**The OpenSearch row diverges from `docker/.env.sut` and from both build-rate
runbooks.** Full corpus on tmpfs needs ~24 GiB, the shipped 28g budget OOM-killed
on 2026-09-03 trying it, and the heap reservation is what gives way to make room.
The engines are no longer on equal container memory: 40g against the
vector-store's 28g. At stack level ScyllaDB is still ahead — 56 GiB across two
containers — and **both readings reach the footer.**

**The asymmetry is larger on this axis than on the build axis, and it is the
first thing a reader will find.** A1 is ScyllaDB *and* the vector-store — eight
cores and 56 GiB across two containers — answering a query that OpenSearch
answers on four cores and 28 GiB. A2 is the vector-store alone on four cores,
which is the matched comparison, and it is the reason A2 exists at all. The
footer says this on every chart drawn from A1.

**Images.** `scylladb/scylla:2026.3.0-rc2`; vector-store built from
`knowack1/vector-store` @ **`94a23ef2`**; `opensearchproject/opensearch:3.8.0`.

**Networking.** `SCYLLA_BROADCAST_RPC` and `SCYLLA_VS_URI` point at the SUT's
private IP so the harness-side driver does not discover the docker-bridge
address; the binaries reach `<sut>:9042`, `<sut>:16080` and `<sut>:9200`.

---

## Gates

| Gate | Rule |
|---|---|
| **Index identity** | **blocking.** `index_docs=8967625` and `index_built_here=false` in every measured sweep's header. A `true` means a sweep rebuilt the index and every arm before it is void — not annotated, re-run |
| **Analyzer parity** | **blocking, and the campaign's defining fairness check.** `ossearch` must print `m1_parity analyzer matches on one probe`. `analyzer check skipped` is a **fail**: it means the config declares no parity analyzer and nothing was compared. `--no-analyzer-check` is never passed. The full probe set is `opensearch/verify_analyzer.sh` and it runs once per index build |
| **Zero-hit cell** | **blocking.** `zero_hit_queries > 0` on any row voids the sweep. Such a cell answers, has a p99, and plots as the cheapest point on the curve. Usual cause was a query set generated past `--max-docs`, which full corpus removes |
| **Errors** | **blocking.** `errors > 0` on any row. A failed request contributes a count and a message and nothing to the distribution, so its time is unaccounted for |
| **Blank percentile** | **blocking.** A blank `p50_ms` is a cell that measured nothing. A blank column is deliberate — a zero would plot as the best point on the curve — so the blank is the signal, not the absence |
| **Limit parity** | **blocking.** Column 14 agrees with the `k<limit>` in the file name on every data row, and each arm carries all three of `10`, `100`, `1000`. ScyllaDB's M1 makes `LIMIT` mandatory and caps it at 1000, so `1000` is the ceiling rather than a choice, and a run above it fails on the flags. A matrix whose arms asked for different top-Ns at the same k is not one matrix |
| **OpenSearch memory budget** | **blocking.** `docker inspect` reports `Memory` = 42949672960 (40g) and `OPENSEARCH_JAVA_OPTS` = `-Xms8g -Xmx8g` on **both** A5 and A6, and A6's data mount is a 24 GiB tmpfs. A shell variable that failed to reach compose leaves the shipped 28g/14g/12 GiB in place, and A6 then OOMs mid-build exactly as the 2026-09-03 pass did. Checked off the container, never off the line that set it |
| **Index location** | **blocking, and the only gate with no CSV column behind it.** Configurations 1 and 2 are told apart by the vector-store's `index=ram` / `index=disk:…` startup line; 3 and 4 by the OpenSearch data mount being NVMe or tmpfs. Both are captured into the arm's `logs/index-location.txt` **while the stack is up**, because `*-down` takes the evidence with it. An arm without that file is unlabelled data: nothing else in the artifacts distinguishes a RAM arm from a disk one, and a vector-store image older than `94a23ef2` produces the mislabel silently |
| **Statement mode** | **blocking.** `statement=prepared` in every A1 header, and nothing else. The script default yields to an exported `STATEMENT`, so this header is the only evidence that the campaign's prepared rule actually held. A `literal` sweep is re-run, not annotated — and the CQL↔OpenSearch parser disclosure depends on knowing which one ran |
| **Projection parity** | `fetch_documents` matches the sweep name. The `cql` − `vector-store` subtraction is valid **only** where both are `false` |
| **One class per sweep** | **blocking.** `probe_windows` exits 1 on a duplicate `(sweep, concurrency, rep)`. A refused arm here is the runbook catching an invocation error, not a tool failing |
| **RSS breach** | **blocking on the ScyllaDB half.** anon `rss_bytes` against the 26 GiB budget and the arm's memory read against the 28g `mem_limit_bytes`. A breach during the sweeps means reclaim is in the tail, which is exactly the part of the distribution these charts are for |
| **CPU attribution** | **annotating, never dropping.** Per cell, the engine container's peak `cpu_cores_used` against its quota: `ok` at ≥0.85, `not-CPU` below it, `?` where no series covers the window. **A `?` is not a pass.** A plateau at `not-CPU` is still plotted; what changes is that it may not be described as the engine's throughput limit |
| **Client headroom** | **annotating, and weaker than next door's.** The harness box CPU per cell, read against the table in Phase 7. There is **no measured read-path harness floor**; this is the substitute and the footer says so |
| **Probe source** | every sample reads `source=cgroup-anon`. The `docker stats` fallback is not anon-only and has no CPU counter, so one fallback sample destroys A3's cache reading |
| **Clock skew** | harness-to-SUT skew under one probe tick (1 s), measured at re-entry and recorded. The window is never padded to cover skew |

**There is no coordinated-omission gate, and that is not an oversight.** The
open-loop gate the Python campaign carried (`coordinated_omission_open_loop`, a
precondition for C5 and C7) exists to decide whether a *scheduled* generator
fell behind. This harness offers nothing on a schedule: N workers, no channel,
next query on answer. There is no schedule to fall behind, so the correction
does not apply — and neither does the comparison. **These numbers may not be
plotted beside the laptop pass's open-loop C5/C7 numbers.**

---

## Traps

1. **`VS_PORT` defaults to 16042 in the sibling script.** That is the null
   sink's `PORT + 7000` convention. The scripts here pin **16080** and no run
   line overrides it; a sweep that reaches a closed port dies at its index
   probe, on billed fleet time, and the message names the endpoint rather than
   the mistake.

2. **`ossearch`'s default index config is `ramindex`, whose mapping disables
   `_source`.** A `--fetch-documents` arm against it silently times an empty
   transfer. `--index-config disk` on every OpenSearch run line here.

3. **The analyzer check returns `Ok` when the config declares no `m1_parity`
   analyzer**, with only a stderr note to say so. `analyzer_check=true` in the
   header means the check was *enabled*, not that it *compared* anything.

4. **`pkill -f` from inside an `ssh` one-liner kills the ssh session**, because
   the wrapper's own command line contains the pattern. Same for `pgrep -f`.
   Put both inside a script on the box.

5. **`--query-classes` takes a comma-separated list and this campaign always
   gives it exactly one name.** An unknown name is an error rather than an empty
   column, which is the one place the harness spends a second to save a run.

6. **Every CQL sweep prepares 200 statements before the matrix starts.** They
   happen before the first warm-up, so a sweep looks like it hangs for a moment
   at start. It is not hanging. `CqlSearcher::open` prepares against the classes
   the matrix will actually walk, so "every query this will be asked was
   prepared" is an invariant of the tool rather than of the run line.

7. **`STATEMENT=prepared` is a shell default and an exported `STATEMENT` beats
   it.** `${STATEMENT:-prepared}` takes an inherited value, so a run line that
   set `STATEMENT=literal` — or a shell that exported it earlier in the session —
   would win silently. Nothing in the sweep output would look wrong. The
   `# statement` header gate in Phase 7 is what catches it, and it is why that
   gate is blocking rather than informational.

8. **A2's CSV header says `statement=prepared` and it means nothing there.**
   `settings()` emits the flag unconditionally, but `open_searcher` sends the
   vector-store arm down `open_bm25`, which never sees it: the BM25 endpoint has
   no statements to prepare. Do not read that header field as a property of the
   endpoint, and do not explain an A1↔A2 difference with it.

9. **A5 and A6 differ in `_source` as well as in index location.** The clean
   one-variable test — `OS_RAM_INDEX=1` with `--index-config disk` — does not
   fit at full corpus either: the postings alone take ~24 GiB of tmpfs and
   `_source` would add ~10 GiB more, so the confound is structural.
   Report A5↔A6 as "on-disk-with-stored-documents against tmpfs-without", never
   as "disk against RAM", and do not difference it against the ScyllaDB
   A1↔A3 pair, which **is** one variable.

10. **A vector-store image older than `94a23ef2` ignores
    `VECTOR_STORE_FTS_INDEX_DIR` in silence.** A3 and A4 then measure a RAM
    index while their directory, their logs and the write-up all say disk. This
    is the campaign's only undetectable-after-the-fact failure, which is why the
    `index=disk:` startup line is a blocking gate before the arms rather than a
    check afterwards.

11. **A CQL run with `--fetch-documents` deserialises the documents.** That is
   deliberate — a run that asked for `title` and `body` and never touched them
   would be timing a transfer nobody paid for — but it means the projection arm
   includes client-side deserialisation, on the harness box, in the latency.
   Say so wherever the projection arm appears.

12. **`probe_windows` looks for index-build lines to compute `build_s`.** Every
   measured sweep here carries `--no-index-build`, so there are none. Confirm in
   the smoke that it emits blanks rather than failing, and treat a blank
   `build_s` as correct on this campaign.

13. **Ctrl-C ends the cell and the matrix, keeping the cells already measured.**
   A half-walked ladder is a valid CSV with fewer rows, and the count gate in
   `pull_arm` is what catches it. Do not read a short CSV as a finished sweep.

---

## Caveats this half carries into the write-up

- **The ScyllaDB arms answer from a two-container stack with eight cores and
  56 GiB; the OpenSearch arm answers from one container with four cores and
  28 GiB.** A2 is the matched arm. Disclosed on every chart, never netted out.
- **The CQL arm prepared its statements; the OpenSearch arm re-parsed every
  request.** `query_string` is parsed by Lucene per request and there is no
  prepared equivalent, so this campaign's CQL numbers exclude a per-request
  parse that the OpenSearch numbers include. That is the application-shaped
  comparison and it is the one this campaign chose, but it is **not** parser
  parity, and analyzer parity does not imply it. Every chart drawn from A1 says
  so. The size of the difference is not measured here — one `STATEMENT=literal`
  sweep at `c=16` would give it, and no session has run one.
- **The ScyllaDB index was built through the CDC tail path**, because the
  harness creates the index before it loads. A bootstrap-built index is a
  different build and, once settled, should be the same index — should be, not
  measured to be.
- **Index location is measured, not assumed.** Both engines run in both
  locations, and the working-set arithmetic — ~14 GB against 61 GiB of box RAM —
  predicts the disk arms are page-cache resident after warm-up and therefore
  RAM-versus-RAM against their twins. `cache_bytes` per arm is the evidence.
  **If the prediction holds, say so with the numbers; do not present it as a
  reason the arms were unnecessary.** A null result that cost 3 h is still the
  only thing standing between the headline comparison and an assumption.
- **The OpenSearch arms run a larger memory budget than the ScyllaDB arms**, at
  40g against 28g per container, with an 8 GiB heap instead of 14. It is what
  full corpus on a tmpfs costs, `docker/.env.sut:107` predicted the parity loss
  in advance, and it is disclosed on every chart rather than netted out. The
  reverse reading — ScyllaDB's stack holds 56 GiB across two containers against
  OpenSearch's 40 — is disclosed in the same breath, because quoting either one
  alone is an argument rather than a fact.
- **The OpenSearch heap is 8 GiB here and 14 GiB in the build-rate campaign.**
  These arms search a settled index and the larger heap was sized for the
  indexing path, but the index builds in Phase 6 run under the smaller heap too.
  A build that struggles at 8 GiB is a finding about the build, not about the
  reads below it, and it must not be quoted as an index-rate number.
- **A5 against A6 carries a `_source` confound that cannot be tuned away** at
  this corpus size. The ScyllaDB pair A1↔A3 is the clean one-variable location
  read; the OpenSearch pair is not, and the two must not be differenced.
- **`rare_term` is the headline facet and it is the cheapest query in the set**,
  so its `queries_per_s` is the most favourable throughput either engine
  produces here. The other five facets are the correction, and a chart that
  shows only the headline must say which one it is.
- **`common_term` spans a wide selectivity range inside one class**, so part of
  its p50→p99 gap is which query ran rather than how the engine behaved.
- **The `phrase` class is corpus-specific.** Do not carry a phrase-class number
  across corpora.
- **The probe shares the SUT's cores with the engine it measures**, at 1 Hz and
  three cgroup reads per container per second. It adds no cell and no wall
  clock, and it reaches the footer anyway.
- **There is no measured read-path harness floor.** The client-headroom verdict
  is a box-CPU judgement, not a ratio against a number, until
  [`HARNESS-AWS-RUNBOOK.md`](HARNESS-AWS-RUNBOOK.md) Phase 0 lands.

---

## Cost

An estimate with its derivation, never a measurement. **Every row is an
estimate until a session times it; time them and write the real numbers here.**

| Item | Time |
|---|---|
| Re-entry (SSH, mounts, corpus decompress, venv, harness build) | ~20 min |
| **Vector-store image rebuild from `94a23ef2`** | **~15 min** |
| Query set generation and gate | ~3 min |
| ScyllaDB index build at 8,967,625 documents (~8,408 docs/s measured at full corpus) | ~18 min |
| Smoke, both ScyllaDB interfaces | ~10 min |
| **Config 1** — ScyllaDB RAM: index build (~18 min), A1, A2, projection, refusal | ~6h 30m |
| **Config 2** — ScyllaDB disk: restart, gate, index build, A3, A4, projection | ~6h 29m |
| **Config 3** — OpenSearch disk: restart, index build (~16 min), A5, projection | ~3h 25m |
| **Config 4** — OpenSearch tmpfs: restart, gate, index build, A6 | ~3h 16m |
| Per-session re-entry, ×4 | ~2h 20m |
| Close out and pull ×6, verify, render, stop | ~45 min |
| **Total** | **~22.8 h, band 20–26 h, ~$87–114 at $4.37/h** |

2,619 measured cells, of which 2,592 are the matrix. At 25 s a cell that is
**18.2 h of pure measurement**, and it dominates everything else in the table — which is
what the [full-matrix decision](#why-the-full-matrix-and-not-two-slices-through-it)
and the [2×2 decision](#the-primary-comparison-and-the-three-the-22-adds) buy
between them, and it is where every lever below acts.

**Cost levers, decided before launch, not mid-run.**

| Lever | Saving | What it costs |
|---|---|---|
| **`--limit 10` only, k=100/1000 on `rare_term` alone** | **−10h 00m → 12.8 h** | keeps the top-k curve at the headline class on every arm and drops it for the other five. The k×class interaction goes; the k×concurrency interaction — the one with a mechanism behind it — survives. **The first lever to reach for** |
| **Drop configurations 2 and 4** | **−9h 45m → 13.1 h** | back to the diagonal: the deployment-normal comparison survives intact, the index-location question goes. This is the lever that undoes the 2×2, and it is the one to reach for first if the session is short |
| Defer configuration 2 or 4 to its own session | −6h 29m or −3h 16m | costs one extra re-entry (~20 min, +15 for the vector-store image on config 2) and the same `RUN_ID` |
| Drop the projection axis | −11 min | the application-shaped numbers go; the matrix is unaffected |
| **Add** one `STATEMENT=literal` sweep at `c=16` on `rare_term` | **+4 min** | nothing — it buys the number behind the parser disclosure, so "prepared beats literal by X ms" stops being a claim the write-up cannot support |
| **`WARMUP=3 DURATION=12`**, campaign-wide | **−7h 17m → 15.5 h** | the cheapest real lever. A cell drops from 25 s to 15 s and keeps thousands of samples at every level — far above the 100 a p99 needs. It costs tail resolution, not the p99 itself, and it must be set for **every** sweep of every arm or the arms are not comparable |
| `REPS=2` instead of 3 | −6h 04m → 16.8 h | percentiles do not average, so a merged p99 across two repeats is thinner than across three. The `latencies/` files are what make the merge possible at all — do not also drop `--latencies-dir` |
| Both of the above | −10h 55m → **11.9 h** | 1,746 cells at 15 s. The shortest honest version of the full three-dimensional matrix |
| Thin the ladder to `1,4,16,64` | −9h 00m | halves the levels, so a knee between two rungs is invisible. Prefer the two levers above: they cost resolution inside a cell, this one costs the curve |
| Drop `bool_not` and `bool_mixed` | −6h 00m | the class axis loses its two most expensive boolean shapes. Cut classes only from the end of the declared order, never `phrase` |

**Budget 24 h across four sessions. One session is not available.** The campaign cannot
be paused: every stop wipes `/mnt/nvme` **and the vector-store's RAM index**, so
a resumed session pays re-entry (~20 min), the image rebuild (~15 min) and the
index build again. At this length that is the difference between one session
and two, so decide before launch rather than at hour four.

**Every configuration boundary is a clean split**, because each one already
tears the stack down and rebuilds the index. The natural three-session shape:

| Session | Contents | Wall |
|---|---|---|
| 1 | re-entry + image + corpus + queries + harness + **config 1** (A1, A2) | ~7.4 h |
| 2 | re-entry + image + **config 2** (A3, A4) | ~7.2 h |
| 3 | re-entry + **config 3** (A5) | ~4.1 h |
| 4 | re-entry + **config 4** (A6) | ~3.9 h |

Sessions 1 and 2 pay the ~15 min vector-store image rebuild; sessions 3 and 4
do not and need no `94a23ef2` image at all. Sessions 1 and 2 are long enough
that **the top-k loop is the natural place to stop and resume**: it is the
outermost loop, so `k=10` for both arms is a complete, self-consistent result
before `k=100` starts. Every session after the first re-stages
the corpus (~2 min from `~/corpus.jsonl.zst`) and rebuilds the harness (~10 min),
and **must reuse session one's `RUN_ID`** — the 2×2 needs all six arms under one
`$R`. Record every window in `$R/env/sessions.txt`.

The query set is generated **once**, in session one, and pulled to `$R/queries/`
immediately. Later sessions copy it back up rather than regenerating it: a
regenerated set with the same seed should be identical, and "should be" is not a
thing to discover at hour six.

**Do not split inside a configuration.** A matrix half-walked before a stop
cannot be completed after one: the index is rebuilt, and a rebuilt index is a
different index whatever its document count says. A1 and A2 must land in the
same session as each other, and so must A3 and A4.

---

## Where it lands

The four charts [`README.md`](README.md) specifies — X `concurrency`, Y
`p50_ms` / `p90_ms` / `p99_ms` / `queries_per_s`, series `engine` + `interface`
— are what this campaign can produce alone, **once per query class**: 24 panels
from one matrix, of which the `rare_term` four are the headline.

The `c=16` row of that matrix, read across all six classes, is the honest AWS
successor to the laptop pass's C6 — and unlike C6 it arrives with the rest of
its column, so a class that knees before `c=16` can be seen doing it rather
than inferred.

It does **not** produce C5 or C7. C5 is a percentile-axis chart from an
open-loop generator; C7 is an offered-rate sweep. Neither axis exists here, and
the laptop pass's C7 is the chart the deck already marks ⚠ as not supporting
its assigned message.

### What goes in `../TUNING.md`

Three numbers, each with the arm and the run that produced it, and each with
its direction of inequality written down:

- **`p50 / p90 / p99 ms` per interface at `c=1`** — the unloaded service time,
  which is the one number from this campaign that needs no headroom argument.
- **The level at which `queries_per_s` flattens, per interface and per class**,
  with its CPU attribution verdict beside it. Written `≥` where the verdict is
  `not-CPU` or the client headroom read `close`. **Per class is the point of
  this campaign**: if the plateau level is the same for all six, the earlier
  two-slice design would have been sufficient and the next campaign can say so;
  if `phrase` knees earlier than `rare_term`, that spread is the result.
- **`cql` − `vector-store` at `c=1` and at the plateau** — ScyllaDB's own read
  overhead, in milliseconds and as a fraction, stated only for the cells where
  both arms had `fetch_documents=false`. **Twice: once in RAM (A1−A2) and once
  on disk (A3−A4).** If the overhead is the same in both, it is a property of
  the read path; if it moves, it is a property of the index location, and one
  number could never have told them apart.
- **The index-location delta per engine** — A1→A3 and A5→A6 — at `c=1` and at
  the plateau, each with its `cache_bytes` reading beside it, and each with its
  confound stated (`_source` on the OpenSearch pair, none on the ScyllaDB pair).

### The write-up

`$R/README.md`, opening with:

```markdown
# <one line: what was measured>

Run `search-latency-2026-09-16T0900Z`, produced by
`bench/search-latency/SEARCH-LATENCY-AWS-RUNBOOK.md`. Fleet up <HH:MM>–<HH:MM>
UTC on <date>. N=<reps> per arm, over
all 8,967,625 enwiki documents. Binaries: crate commit <sha>. Analyzer parity
verified on both OpenSearch indexes: <probe result>. CQL statements: prepared.
Top-k: 10, 100 and 1000 on every arm, every class and every concurrency.

PRELIMINARY. Closed-loop service times, not comparable with the open-loop
C5/C7 numbers. The ScyllaDB arms answer from eight cores and 56 GiB across two
containers; the OpenSearch arms from four cores and a raised 40 GiB in one, with
an 8 GiB heap rather than build-rate's 14 -- what full corpus on a tmpfs costs,
and it ends container-level memory parity. The CQL arms prepared every query
once before the matrix; the OpenSearch arms parsed every request. Analyzer
parity is verified, parser parity is not claimed. The OpenSearch RAM arm stores
no _source; the ScyllaDB location pair carries no such confound. The corpus is
the whole of enwiki, which differs from the build-rate campaign's 3,500,000-
document bound -- read and write charts describe different indexes.
```

Mandatory footer clauses for anything drawn from this campaign: that the
latencies are **service times** from a closed loop and coordinated omission
does not apply; the concurrency each number was taken at; the cgroup asymmetry
between the ScyllaDB stack and OpenSearch; **that the CQL arm used prepared
statements while the OpenSearch arm re-parsed per request, and that this is not
parser parity**; `limit=10` and whether documents were projected; **which
query class the panel is**, and that `rare_term` is the cheapest in the set so
it is a floor rather than a typical case; **where the index lived on both
sides**, since no column carries it; **the OpenSearch memory budget of 40g/8 GiB
heap against the ScyllaDB containers' 28g**, in both directions; the
client-headroom
verdict per plateau and the fact that no read-path harness floor has been
measured; and **PRELIMINARY**.

Hand the user the absolute path of `$R` and say which arms landed in it.

---

## Recorded results

Nothing yet. **This runbook has never been run.**

| Run | Arms | What it established |
|---|---|---|
| — | — | — |
