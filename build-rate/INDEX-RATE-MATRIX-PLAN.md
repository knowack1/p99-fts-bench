# Index-rate matrix — indexed docs/s against concurrency, one line per arm

**Status: the series set is agreed; the renderer can draw it; nothing has
been measured.** This is the sibling of `../BUILD-RATE-MATRIX-PLAN.md` for the
axis that document never had. That campaign's y is `docs_per_s`, what the
client got the engine to **accept**. This one's y is `index_docs_per_s`, what
the engine made **searchable** — read off the vector-store's status endpoint on
one side and `_stats` on the other, over the whole build *including the settle
after the client stops*. The Rust harness in this directory is the first one
that records that number as a column, and this document is what decides which
arms become lines on it.

Companion documents: `../BUILD-RATE-MATRIX-PLAN.md` (the arms and every
constant this one pins to), `HARNESS-AWS-RUNBOOK.md` (the shared grid and the
client floor), `README.md` and `charts/README.md` (the columns and the
renderer), `../SUT-CONFIG.md` and `../docker/.env.sut` (the deployment),
`../TUNING.md` (why each knob is where it is), `../FREEZE.md` (the corpus).

## What this measures, and what it is not

`index_docs_per_s` is searchable documents divided by the **whole** build wall:
from the first insert to the moment the searchable count reaches what was sent,
or stops moving, or runs out of settle budget (`core/src/build_rate.rs`,
`Level::summarize` — "a build that keeps going for a minute after the loader
finishes did not run at the loader's rate"). A write ack is not a document in
the index, and this axis is the one that refuses to pretend otherwise.

**The two indexed families are not the same mechanism**, and that is the
premise rather than a footnote. On ScyllaDB the index is the vector-store's
Tantivy build, fed through CDC, publishing at every commit. On OpenSearch it is
refresh-gated visibility: `docs.count` advances at a refresh, so the curve is
flat, flat, jump, and it has a floor of `refresh_interval × docs_per_s` however
fast Lucene indexes. Each dashed line is read against its own solid line first
— the gap inside a pair is how far the index build falls behind the client
feeding it — and across engines only at **matched cadence**, which is why the
arms below come in cadence pairs.

Every image carries PRELIMINARY until the fleet pass that produces it has been
written up, and no number from a rehearsal against `../engine-mock` is an
engine number at all.

## The run table — the primary chart

X is concurrency on the shared grid below. Every arm is drawn **twice**: solid
with a filled marker for `docs_per_s`, dashed with a hollow one for
`index_docs_per_s`, in the same colour. Reps are per rung, N=3 on the full
ladder; the grid's two sub-sweeps overlap at `c=32`, so one rep is seven runs
across six rungs.

| # | Arm (`--series` label) | Stack | Engine knobs vs. the row above | Harness command | Reps | Runs |
|---|---|---|---|---|---|---|
| **R1** | `scylla-buf15` | Scylla + vector-store | `VS_FTS_COMMIT_THRESHOLD=0`; `VS_FTS_WRITER_MEMORY_MB` **unset** → tantivy's 15 MB/thread floor; commit interval 3 s; `VS_FTS_METRICS_INTERVAL=1s` | `scyllarate` (index watch is on by default) | 3 | 21 |
| **R2** | `scylla-buf376` | Scylla + vector-store | **+ `VS_FTS_WRITER_MEMORY_MB=376`** — writer-budget parity with OpenSearch's 1.4 GiB node total | as R1 | 3 | 21 |
| **R3** | `scylla-buf376-commit30` | Scylla + vector-store | **+ `VS_FTS_COMMIT_INTERVAL=30s`** | as R1, **`--vs-idle-timeout 75 --vs-settle-timeout 180`** (see "The settle timeouts") | 3 | 21 |
| **R4** | `os-ramindex-refresh3` | OpenSearch | `OS_RAM_INDEX=1` (tmpfs segments) + `_source: false` + `refresh_interval: 3s` | `osrate --index-watch --index-config ramindex --refresh-interval 3s --batch-size 512` | 3 | 21 |
| **R5** | `os-ramindex-refresh30` | OpenSearch | **+ `refresh_interval: 30s`** | as R4, `--refresh-interval 30s` | 3 | 21 |
| **R6** | `os-ramindex-b128` | OpenSearch | R4's knobs | as R4, **`--batch-size 128`** | 3 | 21 |
| **R7** | `os-ramindex-b1` | OpenSearch | R4's knobs | as R4, **`--batch-size 1`** | 3 | 21 |
| **R8** | `scylla-buf376-disk` | Scylla + vector-store | R2's knobs **+ `VS_FTS_INDEX_DIR=/var/lib/vector-store/fts`** — the Tantivy index on the NVMe instead of in RAM | as R1 | 3 | 21 |

Eight arms, 168 runs, sixteen lines with the pairs kept. The readings the
table is built to give, stated on the chart so a cropped screenshot still
carries them:

| Read | Arms | What it is |
|---|---|---|
| Writer buffer | R1 → R2 | the ~42% delta `../BUILD-RATE-LOOP.md` measured at 1.42x on the submitted axis, re-measured on the indexed one |
| Commit cadence | R2 → R3 | what a 30 s commit costs in *visibility* — the same submitted rate, a different searchable one |
| Refresh cadence | R4 → R5 | the same question on the OpenSearch side |
| Bulk size | R4 / R6 / R7 | whether the wire batch moves the indexed rate or only the submitted one |
| Where the index lives | R2 → R8 | the RAM index against the same build file-backed on the NVMe |
| **Cross-engine, 3 s** | **R2 ↔ R4** | the only cross-engine read at the fast cadence |
| **Cross-engine, 30 s** | **R3 ↔ R5** | the only cross-engine read at the slow cadence |
| Cross-engine, same x shape | R2 ↔ R7 | one document per in-flight request on both sides — see below |

**Three batch sizes are series, never an axis.** R4, R6 and R7 differ only in
`--batch-size`, so the only thing that moves between them is the wire batch.
Each runs the whole concurrency grid (`HARNESS-AWS-RUNBOOK.md`, "Batch size is
a series, never an axis"). R7 at batch 1 matters for a reason beyond the bulk
question: it is the only OpenSearch arm whose x axis is the same *shape* as the
ScyllaDB arms' — one document per in-flight request on both sides. At R4 and
`c=128` OpenSearch holds 128 requests carrying 65,536 documents while
`scyllarate` holds 128 requests carrying 128; at R7 both hold 128.

**There is no ScyllaDB batch series and there cannot be one.** `scyllarate`
has no batch flag. One row is one prepared statement, `--concurrency` is
exactly the number of INSERTs in flight, and a batch on that side would only
ever have been a loop window inside the client — `../BUILD-RATE-MATRIX-PLAN.md`
removed the flag from the Python loader for the same reason. The absence of a
second ScyllaDB batch line is a statement, and the footer says so.

**R8 is owed an image and two lines of compose.** The knob exists in the
vector-store fork — `VECTOR_STORE_FTS_INDEX_DIR`, read in `config_manager.rs`,
honoured in `fts_index/tantivy.rs` (`create_in_dir` under that root instead of
`create_in_ram`; the directory is cleared at create and removed on drop, never
reopened) — but as of 2026-09-15 it is **uncommitted** in
`~/Projects/Scylla/vector-store` on top of `282d9efc`, which is the commit the
SUT image was built from. Before R8 can run: the change is committed and the
image rebuilt and loaded from that commit (the manifest records the commit, not
the `0.0.0-dev` version string); `docker/docker-compose.scylla.yml` gains
`VECTOR_STORE_FTS_INDEX_DIR${VS_FTS_INDEX_DIR:+=${VS_FTS_INDEX_DIR}}` in the
vector-store's environment, in the same drop-when-unset form every other FTS
knob uses, plus a named volume at that path so the segments land on the
instance-store NVMe where docker's `data-root` already is. Until both are done
R8 is a row in this table and not a line on a chart. The startup line that
proves the arm took is `ingest tuning for …: … index=disk:/var/lib/vector-store/fts`
against `index=ram` on every other ScyllaDB arm.

**Framing guard, mandatory.** R1 → R2 is a 42% tuning delta on our own side,
and it is on the same axis as OpenSearch because that is what was asked for.
That makes **"they have to tune, we don't" unsayable** from this chart, and the
footer says so in those words. The sentence this table earns is the one
`../BUILD-RATE-MATRIX-PLAN.md` already licenses: the tuning does not disappear
on the CQL path, it moves — from the client's bulk size to the index's commit
cadence and writer budget.

**Sixteen lines is past the readability limit, and that is a rendering
decision, not a measurement one.** A synthetic-row render of the exact command
below (2026-09-15, renderer check only, no engine) draws all sixteen: the
legend covers the rising half of every curve and the four ScyllaDB arms land
on adjacent purples. So the primary image is expected to be its own two-render
fallback — the ScyllaDB arms with their two matched OpenSearch arms (R1, R2,
R3, R8, R4, R5: twelve lines) and the batch trio (R4, R6, R7: six) — off the
same points, never a re-measurement and never a dropped arm. The eight-arm
render is still produced, because its `--table` twin is the one file that
holds every point.

### `--series`, and why the arms are named by directory

The renderer names a line off the row: engine, and batch size where there is
one (`tools/plot_harness_grid.py`'s `series_of`). That is the right name on the
null-sink campaign, where an arm *is* its batch size. Here R1, R2, R3 and R8
write rows that are byte-for-byte the same shape — the knobs that separate them
live in the vector-store's environment, not in the CSV — and R4 and R5 differ
only by a refresh interval the CSV does not carry either. Named off the row
they collapse to one ScyllaDB line and one OpenSearch line at each batch size,
which is a plausible-looking chart that is wrong.

So every arm's points go in their own directory and the chart is drawn with
`--series 'LABEL=GLOB'`, one per arm, in run-table order:

```bash
.venv/bin/python3 build-rate/charts/rate_vs_concurrency.py --keep-warmup \
    --series "R1 scylla-buf15=$R/r1/scylla/points/*.csv" \
    --series "R2 scylla-buf376=$R/r2/scylla/points/*.csv" \
    --series "R3 scylla-buf376-commit30=$R/r3/scylla/points/*.csv" \
    --series "R8 scylla-buf376-disk=$R/r8/scylla/points/*.csv" \
    --series "R4 os-ramindex-refresh3=$R/r4/opensearch/points/*.csv" \
    --series "R5 os-ramindex-refresh30=$R/r5/opensearch/points/*.csv" \
    --series "R6 os-ramindex-b128=$R/r6/opensearch/points/*.csv" \
    --series "R7 os-ramindex-b1=$R/r7/opensearch/points/*.csv" \
    --title "Index rate against concurrency — eight arms, submitted and indexed" \
    --output "$R/index-rate-vs-concurrency.png" \
    --table  "$R/index-rate-vs-concurrency.csv"
```

Named series are drawn first, in the order given, and a label is never parsed
for a batch size. `tools/plot_harness_grid.py` is untouched: the AWS runbook's
images must not move.

## Secondary charts

Second renders off the same points, plus one extra arm. No chart above six
lines.

- **S2a — where the index lives.** R2, R8, R4 and one new arm,
  **`os-disk-refresh3`**: `osrate --index-config disk --refresh-interval 3s
  --batch-size 512` with `OS_RAM_INDEX` unset, segments on the NVMe volume. It
  is what a reader would actually run, and it is the only OpenSearch arm that
  can hold the frozen corpus — the ramindex filled its 12 GiB of tmpfs at
  4,025,699 documents. With R8 in the table, both engines now have a RAM arm
  and a disk arm at the same cadence, which is the pairing this chart draws.
  Reps and runs as the table: 3 and 21.
- **S2b — bulk size.** R4 / R6 / R7. Six lines where sixteen may not read.
  Costs nothing: it is a render.
- **S2c — the ScyllaDB knobs alone.** R1 / R2 / R3 / R8. One engine, one axis,
  so there is no false comparison available to draw, and the 1.42x result gets
  the chart it can be quoted from.

## The fleet

| Alias | Instance | Role in this campaign |
|---|---|---|
| `fts-harness` | `i-08d8d2505e16683f7` · `k-nowacki-fts-benchmark-harness` | runs `scyllarate` and `osrate` |
| `fts-sut` | `i-0e3e4b6b02e654b7f` · `k-nowacki-fts-benchmark-sut` | runs the engine stack under test |

Both `i8g.2xlarge` (8 vCPU Graviton4, 61 GiB, aarch64), `eu-north-1b`, Amazon
Linux 2023, user `ec2-user`, key `~/.ssh/KarolNowackiAws.pem`.

There is no AWS CLI credential on this laptop — the console in Chrome is the
only way to start and stop the boxes. Instructions for that are in "Start the
boxes" and "Stop the boxes" below, and they are `HARNESS-AWS-RUNBOOK.md`'s
Phase 1 and Phase 7 unchanged.

### The results directory — fix it first, on the laptop

Everything on the fleet is ephemeral: `/mnt/nvme` is destroyed on every stop,
so **the laptop is the only place results survive**. Create the directory
before touching a single instance, and give every arm its own subdirectory —
`--series` names a line by where its rows came from, so the layout *is* the
chart's series set:

```bash
# Run this ONCE, at the very start of the session, and keep the shell.
export RUN_ID="index-rate-matrix-$(date -u +%Y-%m-%dT%H%MZ)"
export R="$HOME/Projects/Scylla/p99/bench/results/$RUN_ID"
for arm in r1 r2 r3 r8; do mkdir -p "$R/$arm"/scylla/{points,samples,logs}; done
for arm in r4 r5 r6 r7 osdisk; do mkdir -p "$R/$arm"/opensearch/{points,samples,logs}; done
mkdir -p "$R"/{env,scripts,sut}
printf '%s\n' "$RUN_ID" > "$R/RUN_ID"
echo "results -> $R"
```

### Start the boxes

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
~6.6 h this campaign is long enough for that to happen more than once.

### Fleet re-entry

Every stop wipes the instance store, so this runs on **every** start. The full
procedure is `HARNESS-AWS-RUNBOOK.md` Phase 2 and is not restated here; what
this campaign additionally depends on:

- **Public IPs are reassigned on every start; private IPs are not.** Re-point
  both `HostName` entries in `~/.ssh/config`, accept the new host keys, and
  then **confirm** the private IPs rather than assuming them:
  `172.31.38.237` is the harness, `172.31.47.166` the SUT. That second one is
  hard-coded in `docker/.env.sut` as `SCYLLA_BROADCAST_RPC` and
  `SCYLLA_VS_URI`, so a changed private IP breaks off-box CQL and BM25 routing
  at once, and the `.env.sut` edit is part of re-entry when it changes.
- **The vector-store image must be rebuilt and loaded.** `daemon.json` points
  docker's `data-root` at the instance store, so every image goes with the
  stop. The two public images re-pull; the vector-store is in no registry and
  is built from the fork and `docker save | ssh … docker load`ed (for R8, from
  the commit that carries `VECTOR_STORE_FTS_INDEX_DIR`).
- **`/mnt/nvme` is re-made and re-mounted on both boxes**, docker restarted.
- **The corpus restages in ~1.5–2 min**, a local `pzstd -d -p 8` from the grown
  EBS root — not the 36 min mirror download `../BUILD-RATE-MATRIX-PLAN.md`
  describes. `../FREEZE.md`'s sha256 of the prepared corpus is what proves the
  bytes.

**Do not stop the boxes mid-campaign.** Every stop costs a full re-entry before
any arm can run. If the campaign is split across sessions, split it at an arm
boundary and record which arms were measured in which session — a re-entry
between two arms of the same comparison is a provenance difference the footer
has to carry.

### Stop the boxes

Same console tab. Select both rows → **Instance state → Stop instance** → check
the dialog names **both** `k-nowacki-fts-benchmark-harness` and
`k-nowacki-fts-benchmark-sut`, leave "Skip OS shutdown" unchecked → **Stop**.

Then refresh and **confirm both rows read `Stopped` with no public IP**. Say so
explicitly in the report; "I initiated the stop" is not the same as "they are
stopped".

The `~/.ssh/config` entries now point at released IPs and must be re-pointed on
the next start.

Before stopping: **every artifact is on the laptop**, because `/mnt/nvme` is
about to be destroyed. That is every arm's `points/`, `samples/` and logs, the
SUT's resource-probe JSONL, and the vector-store startup lines that prove each
arm took its tuning.

## How the engines are deployed on the SUT

Everything here is what the repository already does; it is restated in one
place so the campaign is reproducible from this file.

**Fleet.** Two `i8g.2xlarge` — 8 vCPU Graviton4, 64 GiB, 1.9 TB instance-store
NVMe — in eu-north-1b, Amazon Linux 2023 on kernel 6.18, Docker 25.0.14 with
`data-root` on the instance store (`../HARDWARE.md`, `../SUT-CONFIG.md`).
`fts-harness` runs `scyllarate` and `osrate`; `fts-sut` runs **one engine
stack at a time**. Private RTT 0.353 ms, measured.

**How the SUT is driven.** `source tools/fleet_env.sh` on the harness sets
`DOCKER_HOST=ssh://<sut>` and `COMPOSE_ENV=docker/.env.sut`, so every `docker
compose` call reads the compose files and env **locally** and creates the
containers **on the SUT**. The Makefile's `scylla-up` / `os-up` / `*-down`
targets are the deploy commands; `OS_RAM_INDEX=1` adds the
`docker-compose.opensearch.ramindex.yml` overlay. The engine-side resource
probe is the one thing `DOCKER_HOST` cannot carry — it reads `/sys/fs/cgroup`
where it runs — so `tools/sut_probe.sh start/stop` runs it on the SUT.

**Images.** `scylladb/scylla:2026.3.0-rc2`; `opensearchproject/opensearch:3.8.0`;
`vector-store` built from `knowack1/vector-store` @ `282d9efc` (R8: from the
commit that adds `VECTOR_STORE_FTS_INDEX_DIR`, see above) and `docker save |
ssh … docker load`ed onto the SUT. It is in no registry, and the instance
store takes it with every stop — fleet re-entry is as
`../BUILD-RATE-MATRIX-PLAN.md` describes it.

**The 50/50 cgroup split** — `docker/.env.sut`, applied by compose as
`mem_limit` / `cpus` / `cpuset` on each service:

| Stack | Service | `cpuset` | `cpus` | `mem_limit` | In-process budget |
|---|---|---|---|---|---|
| `scylla` | ScyllaDB — `--smp 4 --memory 24G --overprovisioned 0` | `0-3` | 4 | 28g | 24 GiB |
| `scylla` | vector-store | `4-7` | 4 | 28g | `VECTOR_STORE_MEMORY_LIMIT` = 26 GiB |
| `opensearch` | OpenSearch — `-Xms14g -Xmx14g`, `bootstrap.memory_lock=true`, memlock and nofile ulimits | `4-7` | 4 | 28g | 14 GiB heap (+ 12 GiB tmpfs ceiling on the ramindex arms, counted against the same 28g) |
| `opensearch` | database slot | `0-3` | — | — | **deliberately idle** — models the database OpenSearch deploys beside |

**What this means for every cross-engine line.** The ScyllaDB stack has eight
cores and 56 GiB of cgroup across two containers; OpenSearch has four cores and
28 GiB in one. The vector-store — the process doing the indexing this chart
measures — sits on the same four cores OpenSearch does, which is the sense in
which the indexing halves are matched. `VS_CPUSET=4-7` is also what fixes the
tantivy worker count at 4 (`available_parallelism` honours the cpuset), so
`VS_FTS_WRITER_MEMORY_MB=376 × 4 = 1,504 MB` is the parity with OpenSearch's
1.4 GiB `indices.memory.index_buffer_size`. `../BUILD-RATE-LOOP.md`'s finding
that the split is probably wrong for the CDC path — Scylla at 3.56 of 4 while
the vector-store idles near 1.9 of 4 — is carried as a caveat, not changed
here. All of it reaches the footer.

**R8 and the memory gate.** A file-backed index is page cache, not anonymous
memory: the allocation gate behind `VECTOR_STORE_MEMORY_LIMIT` does not see it
growing (the vector-store's own startup line says so). The cgroup's 28g
`mem_limit` **does** count page cache, and it reclaims rather than kills, so
R8's failure mode is a slower build under reclaim rather than an OOM — which is
exactly what the arm exists to measure. The resource probe's `cache_bytes`
against `rss_bytes` is the reading that tells the two apart.

**Per-arm engine env** — the only thing that varies on the SUT between arms,
passed through compose's `${VAR:+=${VAR}}` form so an unset knob is dropped
rather than passed empty:

| Arm | On top of `.env.sut` |
|---|---|
| R1 | `VS_FTS_WRITER_MEMORY_MB=` (unset → 15 MB/thread floor), `VS_FTS_COMMIT_THRESHOLD=0`, `VS_FTS_METRICS_INTERVAL=1s` |
| R2 | `VS_FTS_WRITER_MEMORY_MB=376`, otherwise as R1 |
| R3 | R2 + `VS_FTS_COMMIT_INTERVAL=30s` |
| R8 | R2 + `VS_FTS_INDEX_DIR=/var/lib/vector-store/fts` |
| R4 / R6 / R7 | `OS_RAM_INDEX=1`, `OS_RAM_INDEX_SIZE=12884901888`; `refresh_interval: 3s` set by `osrate --refresh-interval` at index create |
| R5 | as R4, `--refresh-interval 30s` |
| `os-disk-refresh3` | `OS_RAM_INDEX` unset, `osrate --index-config disk`, data on the `opensearch-data` volume |

**The stack is recreated between arms** — `*-down` then `*-up` — never
restarted in place. The vector-store's RAM index is rebuilt on start, and an
image that ignored a knob looks identical to one that honoured it: that already
cost S11–S15 once. The arm's tuning is confirmed from the vector-store's own
startup lines before its ladder runs — `ingest tuning for …:
commit_interval=… commit_threshold=disabled … index=ram|disk:…` and `index
writer using 4 tantivy worker threads, 376 MB buffer per thread` — the same
two lines `ftsbench/verify_arm.py` gates on, read the same way.

**Networking.** `SCYLLA_BROADCAST_RPC` and `SCYLLA_VS_URI` in `.env.sut` point
at the SUT's private IP so the harness-side driver does not discover the
docker-bridge address; `osrate` reaches `http://<sut>:9200`, `scyllarate`
reaches `<sut>:9042` and reads the index at `http://<sut>:16080`.

## The shared concurrency grid

**Every series is measured on the same x values:**

```
4   8   16   32   64   128
```

Powers of two on a log2 axis, never varied per arm — series that do not share
x values cannot be drawn on one axis, and a ladder tailored per arm silently
destroys the chart. Add a rung to one arm and you add it to nine.

Two sub-sweeps per arm, overlapping at `32`, one `--max-docs` per sweep for
**every** arm. `--keep-warmup` on every chart command: the ladders carry no
throwaway rung, so `4` and the high sweep's `32` are measured points and the
renderer's default first-row drop would delete both.

**This axis raises the budget, and the slowest-publishing arm sets it.** On the
submitted axis a point only has to be long enough to average over. On this one
a level whose build covers a couple of commit intervals is mostly settle, and
the settle is in the denominator. The rule: **every level's build covers at
least four cadence intervals** — at 30 s, at least 120 s of build. At the
~12–14k docs/s ceilings the source campaign measured, that puts the high sweep
at **1,500,000 documents** (`32,64,128`) and the low sweep at **600,000**
(`4,8,16,32`), where `c=4` is the rung that decides it. Both are estimates from
the source campaign's ceilings; the smoke pass confirms them before any ladder
runs, and if a rung comes in under four cadences the budget goes up **for
every arm in that sweep**, never for the one that fell short.

If the two sweeps disagree at `32` by more than the rep spread, the budgets are
distorting the measurement — say so rather than average it away.

### The settle timeouts — and why R3 needs its own

The vector-store's status endpoint reports one count, the searchable one
(`scylla/src/vstore.rs`: `accepted: None`). The harness's idle rule is "stopped
moving, measured on what the engine has accepted", and where there is no
separate accepted count it falls back to the searchable one. On a 3 s commit
that is harmless. On R3's 30 s commit, `scyllarate`'s default
`--vs-idle-timeout 10` declares the build idle 10 s into every 30 s gap — every
R3 level would end `index_settled=false` at a fraction of its rate, and the
chart would show a cadence cost that is the harness's, not the engine's.

So R3 runs with **`--vs-idle-timeout 75`** (two and a half cadences: one full
gap plus margin) and **`--vs-settle-timeout 180`** (six cadences; the default
120 is four, and the tail after the last insert can legitimately wait one full
interval plus the commit itself). Both are recorded in the CSV header
(`vs_settle_timeout_s`), so a reader can check. `osrate` needs no such change:
`_stats` reports accepted and searchable separately, its idle rule watches the
former, and its defaults (`--index-idle-timeout 15`, `--index-settle-timeout
180`) already clear R5. The source campaign hit the same wall
(`C1_IDLE_TIMEOUT=60` was one 60 s commit cycle) and floored both timeouts at
four cadences; this is the same fix.

A point that reaches its target ends the settle at the commit that brought it
there, so a settled R3 point pays no idle padding. Only an unsettled one does,
and it is marked.

## Reps and signals

**N=3, campaign-wide**, one repetition count moved together by `REPS=`
(Karol, 2026-09-09). Rep-major, so a cold-start effect lands in every arm's
first rep rather than in one arm's whole ladder.

**Top-up rule**, carried over: a cadence pair (R2↔R3, R4↔R5) landing within
~5% at `c_sat` is topped up to N=5 *at that rung only* — 2 arms × 2 reps × 1
rung, not a re-run.

Per point:

| Source | Fields |
|---|---|
| `<arm>/<engine>/points/*.csv` | the seventeen shared columns; the six that are this chart's — `index_docs`, `index_docs_per_s`, `index_lag_docs`, `index_settle_s`, `index_settled`, `index_status` — plus `batch_size` and `engine`; header stamps the linked driver version, the live engine version and every timeout |
| `<arm>/<engine>/samples/<rep>/c<conc>-*.csv` | the ten-column per-second series, `--samples-dir` — the tail after the last insert is the part a per-level average cannot show, and `charts/rate_vs_index_size.py` draws it |
| `cpu-*.jsonl` (`resource_probe`, on the SUT) | per container `cpu_cores_used`, `rss_bytes`, **`cache_bytes`** (R8's reading), `disk_read_bytes`, `disk_write_bytes`, `index_size_bytes` |
| vector-store log (R1–R3, R8) | the two startup lines above; `added/s`, `commits`, `committed/s` |
| `index-rate-vs-concurrency.csv` | the chart's `--table` twin: series, metric, concurrency, reps, median/min/max, shortest wall |

## Gates

**There is no client-headroom gate on this campaign.** Every rung of every arm
is measured and plotted on its own merits, whatever its submitted rate is next
to the client's own floor. The source document's G7 — a level clears only at
≥2x under the measured client ceiling — is deliberately **not** carried over.
It exists to decide whether a *client* ceiling may be quoted as an *engine*
number; on this axis the engine's searchable rate is what is quoted, and a
gate that removes points from the chart costs more than it protects. The
client floor `HARNESS-AWS-RUNBOOK.md` recorded (≥266,578 docs/s on `scyllarate`
at batch 1 against the null sink) is quoted in the footer as context, never
applied to a point.

| Gate | Rule |
|---|---|
| Short point | a level under 3 s is not a measurement; the renderer names every one in the footer, and the fix is a bigger `--max-docs` for that sweep and a re-run of every arm in it |
| Thin series | a build with under three index readings is skipped by name in the growth chart; poll at `--vs-interval 0.25` / `--index-interval 0.25` where levels are fast |
| Not settled | `index_settled=false` is a lower bound: drawn hollow, the reason (`index_status`) named in the table |
| Arm took | the vector-store's two startup lines match the arm before its ladder runs; `osrate`'s analyzer check stays on |
| Manifest | every arm directory carries the `.env` it ran with and the image commit, alongside the CSVs |

## The final refresh — disclosed, not hidden

Every arm runs with the harness's default: when the client has stopped, the
engine has accepted everything, and the searchable count is still short, the
harness asks the index to publish once, and `index_status=refreshed` records
every point where it did. **Only `osrate` can ask.** Its probe answers the
settle hint with a `_refresh`; the vector-store's status endpoint offers no
equivalent and `scyllarate`'s probe keeps `core`'s default, which is never to
ask (`core/src/index.rs`, `settle_hint`). So the asymmetry is one-sided:
**R5 is credited with a publish its configured policy had not yet delivered
when the loader finished, and R3 is not** — R3 waits for its own next commit,
which is what the timeouts above are sized for. The alternative for the
OpenSearch arms, `--no-index-final-refresh`, reports what the policy alone
delivered and was considered and not chosen: at 30 s it turns a build that
finished 29 s before its next refresh into one that reports nothing. The
footer states which it was, on both charts, and the `refreshed` count per
OpenSearch arm goes in the table.

## Caveats to carry into the write-up

- **The cap is not the talk's operating point.** `../BUILD-RATE-LOOP.md`
  measured the engine ranking *inverting* between 1.2M and the 8.97M corpus.
  This ladder answers "what saturates", not "what wins at scale".
- **A batch optimum found at the cap is not the batch optimum.** It moves with
  document size and with segment-merge pressure, and the second moves with
  corpus size. R4/R6/R7 answer "what did we set it to, and was that
  defensible".
- **The ramindex arms cannot hold the corpus** (12 GiB of tmpfs, 4,025,699
  documents) and cannot be raised at parity — tmpfs counts against the same
  28g the heap does. Any full-corpus OpenSearch number comes from the disk
  arm, which is why S2a exists.
- **OpenSearch gets four cores; the ScyllaDB stack gets eight.** Load-bearing
  for R2↔R4, R3↔R5 and R2↔R7, and irrelevant inside any single-engine chart.
  It reaches the footer.
- **Encode cost is asymmetric and per-document.** ~16.4 µs per document on
  the OpenSearch client against ~0.62 µs on ScyllaDB; outside the latency
  window but a harness artifact in a comparison whose credibility rests on
  symmetry. It does not amortise over batch size — the per-request HTTP cost
  is what does — which a reader will assume the other way round.
- **Contiguous sharding at the cap is not the first N documents.** Fine for
  engine against engine as long as both sides shard identically; stated on the
  chart rather than discovered later.
- **Dropping an FTS index does not release its RAM.** Mild at this cap; the
  probe is what proves it per arm.
- **R8 is a scratch directory, not durability.** The index is wiped at create
  and removed on drop and is never reopened. The arm measures where the
  segments live during a build, and nothing about restart.

## Cost

An estimate with its derivation, never a measurement. The source campaign
measured a ~82 s point-to-point cadence at a 1M cap, ~8% of it reset and
settle. At the budgets above a low-sweep point runs ~45–120 s and a high-sweep
point ~110–125 s, so call it **~125 s per run** all-in. Nine arms
(the table's eight plus `os-disk-refresh3`) × 21 runs × 125 s ≈ **6.6 h**,
band 6–8 h, **~$26–35 at the $4.37/h fleet rate** — plus re-entry, which is
billed like anything else: ~15–20 min of image rebuild, restage and mounts
before the first arm runs, and more if the campaign is split across sessions. R7 is the long pole on the
OpenSearch side — batch 1 is the slowest way to feed `_bulk` — and R3 on the
ScyllaDB side, where the 30 s cadence sets the budget for everyone.

**Cost levers, decided before launch, not mid-run.**

| Lever | Saving | What it costs |
|---|---|---|
| Drop R7 | −0.7 h | the only OpenSearch arm whose x axis is the same shape as ScyllaDB's |
| Drop R6 | −0.7 h | the bulk-size read becomes two points, and a knee between 1 and 512 is invisible |
| Drop `os-disk-refresh3` | −0.7 h | S2a loses its OpenSearch disk arm and R8 has nothing to pair with |
| Drop the low sweep's `4` | −0.4 h | the RTT-bound end goes unmeasured on every arm |
| N=3 → N=1 on the middle rungs | −1.5 h | the middle of every curve carries no spread |

## Execution order

1. **Rehearsal against `../engine-mock`**, on the laptop. `--os-refresh-interval-ms`
   and `--vs-serving-delay-ms` model exactly the two mechanisms this chart
   contrasts, so the eight-arm series set is rendered from real CSVs before
   fleet time is spent: eight arm directories, the `--series` command above,
   sixteen lines, footer intact. **No number from it is an engine number.**
2. Commit the `VECTOR_STORE_FTS_INDEX_DIR` change in the vector-store fork,
   rebuild the image from that commit, add the compose passthrough and volume
   for R8.
3. **"The results directory"**, then **"Start the boxes"**, then **"Fleet
   re-entry"** — `$R` and its nine arm subdirectories on the laptop first,
   both boxes started from the console with the tab kept alive, SSH
   re-pointed, private IPs confirmed against `.env.sut`, `/mnt/nvme` re-made,
   images re-pulled and the vector-store image rebuilt, corpus restaged and
   verified against `../FREEZE.md`.
4. **Smoke**: 2 rungs × all nine arms at a 20k cap. Gates: every point
   complete, every arm's startup lines match, R3 does not end unsettled, R8's
   line says `index=disk:`, the ramindex arms do not hit ENOSPC.
5. **Budget check**: one high-sweep rung on R3 at 1.5M — the build covers ≥4
   commit intervals or the budget moves for every arm.
6. **The ladders**, one arm at a time in table order, stack recreated between
   arms: R1, R2, R3, R8, then R4, R5, R6, R7, then `os-disk-refresh3`. A
   surprise in R2 can still change the plan before the OpenSearch arms run.
7. Pull every artifact home, then **"Stop the boxes"**: both rows confirmed
   `Stopped` with no public IP, said so explicitly. Everything after this is
   laptop work.
8. Render the primary chart and S2a–S2c; `--table` for each; record each arm's
   ceiling and `c_sat` in `../TUNING.md` with the run that produced it.

## Where it lands

Nothing new on the main deck until the pass has been written up. The
candidate is one image — the primary chart or, if sixteen lines do not read,
its two-render fallback — for S12's neighbourhood, with the footer clauses
above mandatory: what one operation is on each engine; that ScyllaDB runs at
batch 1 and why; the cadence of every arm and that the harness asked for the
final publish; the 4-against-8-core split; the client floor as context; which
points are lower bounds and why; the cap and the sharding; and PRELIMINARY.
