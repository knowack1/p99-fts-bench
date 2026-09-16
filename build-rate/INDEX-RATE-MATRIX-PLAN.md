# Index-rate matrix — indexed docs/s against the offered rate, one line per arm

**Status: the axis moved to the offered rate on 2026-09-16 and the harness
implements it; the series set is agreed; the renderer can draw it; the grid
itself waits on Phase A; no engine has been measured.** The harness change is
done and verified against `../engine-mock` — offered 20,000 and 50,000 docs/s
came back at 19,989.5 and 49,962.3 on `scyllarate` and 20,120.8 and 50,291.2 on
`osrate`, with `queue_p99_ms` under 1.5 ms throughout. **Those are pacer
numbers, not engine numbers**, and the only thing they establish is that the
instrument offers the rate it was asked for. Execution step 1 ran all five arms
against `../engine-mock` on the laptop and is written up at
`../results/rehearsal-index-rate-2026-09-15T1938Z/`. It settled the eight-line
question (one image, the by-engine split is retired), found the `VS_PORT`
defect that would have killed every ScyllaDB arm on billed fleet time, and
reproduced the request-count plateau — the artifact that, on 2026-09-16, moved
this campaign off the concurrency axis altogether.
**Nothing in it is an engine number.** The fleet has not been started; steps
2–8 are outstanding. This is the sibling of `../BUILD-RATE-MATRIX-PLAN.md` for the
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
feeding it — and across engines only at **matched cadence**. Every arm below
runs at a 1 s cadence, so there is exactly one cross-engine reading available
and no cadence axis at all: the 30 s arms (a `VS_FTS_COMMIT_INTERVAL=30s`
ScyllaDB arm and a `refresh_interval: 30s` OpenSearch one) were dropped from
this campaign before it ran. **What a slower cadence costs in visibility is
therefore unmeasured here, and no chart off this campaign may claim it.**

Every image carries PRELIMINARY until the fleet pass that produces it has been
written up, and no number from a rehearsal against `../engine-mock` is an
engine number at all.

## The run table — the primary chart

X is the **offered rate in documents per second** on the shared grid below —
2026-09-16, replacing concurrency. Every arm is drawn **twice**: solid with a
filled marker for `docs_per_s`, dashed with a hollow one for
`index_docs_per_s`, in the same colour, against a dotted `y = x` diagonal.
Reps are per rung, N=3.

**Why the axis moved, in one paragraph.** This document already conceded the
defect below under "The x axes are not the same shape": `--concurrency` is
requests in flight on both halves, but a `_bulk` carries 1,024 documents and a
prepared INSERT carries one, so R2 ↔ R4 — *the campaign's only cross-engine
read* — compared two different offers at every x value. `../TUNING.md` §3 records
that the same class of defect already bit the Python loaders once and was ruled
a defect: *"no shared unit of offered client pressure"*, one side measuring its
engine and the other its client, *"and the difference was published as an engine
result."* A document per second is a shared unit; requests in flight is not.
Measured against `../engine-mock` at one identical offered rate of 50,000 docs/s,
`in_flight_peak` came out at **2** on the `osrate` side and **402** on the
`scyllarate` side. The asymmetry did not go away — it became a recorded column
instead of a distorted axis.

**Three more artifacts go with it**, all of them consequences of the closed loop
rather than of the engines: the request-count plateau that made R4's rehearsal
curve go flat (*"a knee that does not exist"*), the read-ahead that front-ran
half the top rung, and `p50_ms`/`p99_ms` being service times. See "The shared
offered-rate grid".

**Closed loop is not retired.** It is the correct instrument for *how fast can
it go*, and that is exactly what Phase A below uses it for. What changed is that
it is no longer the published axis.

**The cadence is 1 s on both halves**, which this table read as 3 s until
2026-09-16; both runbooks were already at 1 s and
`INDEX-RATE-OPENSEARCH-RUNBOOK.md` asked for the disagreement to be settled here
before the fleet started. It is settled: 1 s, and R2 ↔ R4 is cadence-matched.

| # | Arm (`--series` label) | Stack | Engine knobs vs. the row above | Harness command (Phase B) | Reps | Runs |
|---|---|---|---|---|---|---|
| **R1** | `scylla-buf15` | Scylla + vector-store | `VS_FTS_COMMIT_THRESHOLD=0`; `VS_FTS_WRITER_MEMORY_MB` **unset** → tantivy's 15 MB/thread floor; `VS_FTS_COMMIT_INTERVAL=1s`; `VS_FTS_METRICS_INTERVAL=1s` | `scyllarate --target-rate $GRID --max-docs 3500000 --concurrency 512 --vs-interval 0.25` (index watch is on by default); **`VS_PORT=16080`** in the environment | 3 | 21 |
| **R2** | `scylla-buf376` | Scylla + vector-store | **+ `VS_FTS_WRITER_MEMORY_MB=376`** — writer-budget parity with OpenSearch's 1.4 GiB node total | as R1 | 3 | 21 |
| **R8** | `scylla-buf376-disk` | Scylla + vector-store | R2's knobs **+ `VS_FTS_INDEX_DIR=/var/lib/vector-store/fts`** — the Tantivy index on the NVMe instead of in RAM | as R1 | 3 | 21 |
| **R4** | `os-ramindex-refresh1` | OpenSearch | `OS_RAM_INDEX=1` (tmpfs segments) + `_source: false` + `refresh_interval: 1s` | `osrate --target-rate $GRID --max-docs 3500000 --concurrency 128 --queue-depth 1 --index-watch --index-config ramindex --refresh-interval 1s --batch-size 1024 --index-interval 0.25` | 3 | 21 |

`$GRID` is the seven offered rates Phase A picks; it is the same list for every
arm, and one arm running a different list destroys the chart. **Runs are Phase B
only** — Phase A adds six more per arm (six concurrency rungs at N=1), and its
rows are never plotted.

**The numbering has gaps, and they are deliberate.** R3
(`scylla-buf376-commit30`), R5 (`os-ramindex-refresh10`), R6
(`os-ramindex-b128`) and R7 (`os-ramindex-b1`) were arms of this table and were
dropped: the two cadence arms because the campaign no longer measures a 30 s
cadence, the two batch arms because every OpenSearch arm now runs at a single
fixed `--batch-size 1024`. The identifiers are **not** reused — an `r3/`
directory or an "R7" in a companion document refers to an arm that was never
measured, and a gap is what makes that visible. The table is in execution
order, which is also `--series` order.

**Every arm's command additionally carries
`--corpus /mnt/nvme/data/corpus.jsonl`**, the frozen enwiki corpus on the
harness box — the same file, same bytes, for all eight, which is the
"every document exactly once, same set per engine" invariant. It is left out of
the cells above only because repeating it eight times would hide what actually
differs between the rows. See "The corpus" below for how it gets there.
`osrate` does not read the line's `uuid` (it is ScyllaDB's partition key), so
one file serves both halves.

Four arms, 84 runs, eight lines with the pairs kept. The readings the
table is built to give, stated on the chart so a cropped screenshot still
carries them:

| Read | Arms | What it is |
|---|---|---|
| Writer buffer | R1 → R2 | the ~42% delta `../BUILD-RATE-LOOP.md` measured at 1.42x on the submitted axis, re-measured on the indexed one |
| Where the index lives | R2 → R8 | the RAM index against the same build file-backed on the NVMe |
| **Cross-engine, 1 s** | **R2 ↔ R4** | the only cross-engine read this campaign has, at the only cadence it runs |

Three readings, and the list of what this table can **no longer** say is as
load-bearing as the list of what it can. **Cadence cost is gone** on both
engines and cross-engine: nothing here separates a commit or refresh interval
from any other cause. **Bulk size is gone**: `--batch-size 1024` is a constant,
not a measured optimum, and the campaign has no evidence for or against it.
Neither may be reintroduced by inference from the arms that remain.

**Batch size is a constant here, not a series.** Every OpenSearch arm runs
`--batch-size 1024` — R4 and `os-disk-refresh1` alike — so the wire batch never
moves and the campaign has one OpenSearch line per configuration rather than
one per batch level. `HARNESS-AWS-RUNBOOK.md`'s "Batch size is a series, never
an axis" still holds; this campaign simply runs one member of that series. Two
consequences follow, and both are footer clauses rather than footnotes:

**The x axes were not the same shape — and the axis change is what fixed it.**
`--concurrency` is requests in flight on both halves, but a `_bulk` carries
1,024 documents and a prepared INSERT carries one. At `c=128` OpenSearch held
128 requests carrying **131,072 documents** while `scyllarate` held 128 requests
carrying **128** — a 1,024x asymmetry at every rung, which made **R2 ↔ R4
compare two different things per x value**. The arm that used to correct it
(R7, at batch 1) was dropped, and with batch size a campaign-wide constant there
was nothing left to close it from.

**On the offered-rate axis there is nothing to close.** One document per second
is one document per second whether it arrives inside a 1,024-document `_bulk` or
as a single INSERT, so batch size stops being a comparability hazard and becomes
pure transport — *"batch size is a constant, not a series"* gets **stronger**,
not weaker. The asymmetry itself did not vanish and is not hidden: at one
identical offered rate of 50,000 docs/s against `../engine-mock`,
`in_flight_peak` was **2** on the `osrate` side and **402** on the `scyllarate`
side. It is now a recorded column that a reader can check rather than a
distortion baked into the x axis.

**The read-ahead check is retired with the axis that needed it.** The channel is
bounded in batches, so documents buffered ahead of the workers were
`queue_depth × concurrency × batch_size`, and at depth 10, `c=128` and batch 1024
that was **1,310,720 documents ≈ 5.17 GB** — half the old high sweep, front-run.
A paced producer releases on a schedule rather than as fast as the channel will
take, so the read-ahead has no work to do: **`--queue-depth 1`** under pacing
brings it to 131,072 documents ≈ 0.52 GB on the OpenSearch side and 512 documents
on the ScyllaDB one. The pre-flight arithmetic stays in the runbooks for Phase A,
which still runs closed loop at depth 10.

**There is no ScyllaDB batch series and there cannot be one.** `scyllarate`
has no batch flag. One row is one prepared statement, `--concurrency` is
exactly the number of INSERTs in flight, and a batch on that side would only
ever have been a loop window inside the client — `../BUILD-RATE-MATRIX-PLAN.md`
removed the flag from the Python loader for the same reason. The absence of a
second ScyllaDB batch line is a statement, and the footer says so. It is also
why the asymmetry above cannot be closed from the ScyllaDB side: batch 1 is not
a setting there, it is the write path.

**R8's knob is in place; only the image rebuild is outstanding.** The
vector-store's `VECTOR_STORE_FTS_INDEX_DIR` landed in the fork at **`94a23ef2`**
(on top of `282d9efc`): unset keeps `Index::create_in_ram`, set gives each index
its own subdirectory under the root backed by `MmapDirectory`, wiped at create
and removed on drop with no reopen path. `docker/docker-compose.scylla.yml`
passes it through in the same drop-when-unset form every other FTS knob uses and
mounts the `vector-store-fts` volume at `/var/lib/vector-store/fts`
unconditionally; `docker/.env.sut` carries the variable commented out, because
R8 is the only arm that sets it.

What is left is not campaign work: **the SUT image must be rebuilt from
`94a23ef2`** rather than `282d9efc`, which happens on the harness during fleet
re-entry anyway (the image is in no registry and the instance store takes it on
every stop). The manifest records the commit, not the binary's `0.0.0-dev`
version string.

**An image built before `94a23ef2` ignores the variable in silence** — which is
the exact failure that already cost S11–S15 once — so R8's gate is the
vector-store's own startup line, `ingest tuning for …: … index=disk:/var/lib/vector-store/fts`
against `index=ram` on every other ScyllaDB arm. Read the line; do not trust
the environment.

**Framing guard, mandatory.** R1 → R2 is a 42% tuning delta on our own side,
and it is on the same axis as OpenSearch because that is what was asked for.
That makes **"they have to tune, we don't" unsayable** from this chart, and the
footer says so in those words. The sentence this table earns is the one
`../BUILD-RATE-MATRIX-PLAN.md` already licenses: the tuning does not disappear
on the CQL path, it moves — from the client's bulk size to the index's commit
cadence and writer budget.

**Eight lines read as one image. The two-render fallback is retired, and this
is now measured rather than expected.** The synthetic-row render that
established the limit (2026-09-15, renderer check only, no engine) was of the
*sixteen*-line command: at that width the legend covered the rising half of
every curve and the four ScyllaDB arms landed on adjacent purples. Neither
survives at four arms. Execution step 1 rendered the real command off real
CSVs — `(8 lines, 48 points)` — and the palette gives the three ScyllaDB arms
three clearly distinct colours rather than a purple neighbourhood, with the
legend in two columns clear of every curve. S2a renders the same way at eight
lines, S2c at six.

**One cosmetic residual, not a reason to split.** Where a pair's solid and
dashed lines end at nearly the same y, their two right-edge labels overlap. The
rehearsal amplified this — the mock's vector-store status endpoint reports the
*accepted* count, so its ScyllaDB dashed lines sit exactly on their solid ones,
which is the one thing a mock cannot rehearse about this chart. On the fleet
the indexed line sits below the submitted one, which is what separates the
labels; R4, whose pair *is* separated in the rehearsal, labels cleanly. The
legend carries every name either way.

If some future arm set does stop reading, the split is by engine (R1, R2, R8
against R4), off the same points, never a re-measurement and never a dropped
arm.

### `--series`, and why the arms are named by directory

The renderer names a line off the row: engine, and batch size where there is
one (`tools/plot_harness_grid.py`'s `series_of`). That is the right name on the
null-sink campaign, where an arm *is* its batch size. Here R1, R2 and R8 write
rows that are byte-for-byte the same shape — the knobs that separate them live
in the vector-store's environment, not in the CSV — and with batch size now a
campaign-wide constant it cannot distinguish R4 from `os-disk-refresh1` either.
Named off the row, the three ScyllaDB arms collapse to one line and the two
OpenSearch arms to another, which is a plausible-looking chart that is wrong.

So every arm's points go in their own directory and the chart is drawn with
`--series 'LABEL=GLOB'`, one per arm, in run-table order:

```bash
.venv/bin/python3 build-rate/charts/rate_vs_offered.py --keep-warmup \
    --series "R1 scylla-buf15=$R/r1/scylla/points/*.csv" \
    --series "R2 scylla-buf376=$R/r2/scylla/points/*.csv" \
    --series "R8 scylla-buf376-disk=$R/r8/scylla/points/*.csv" \
    --series "R4 os-ramindex-refresh1=$R/r4/opensearch/points/*.csv" \
    --title "Index rate against offered rate — four arms, submitted and indexed" \
    --output "$R/index-rate-vs-offered.png" \
    --table  "$R/index-rate-vs-offered.csv"
```

Named series are drawn first, in the order given, and a label is never parsed
for a batch size. **This campaign adds no series-naming behaviour to
`tools/plot_harness_grid.py`**: `--series` lives entirely in
`charts/rate_vs_offered.py`, which imports `series_of` and everything else that
is not the x axis from `charts/rate_vs_concurrency.py` rather than changing
either, so no row lands on a different line in the AWS runbook's images than it
did before. **Each renderer refuses the other ladder's rows by name**: a
rate-ladder CSV on the concurrency axis would stack every point on one x — the
cap — and draw a plausible chart that is wrong, which is the same failure this
section already guards against for row-derived series naming.

That is narrower than "untouched", which this section used to claim and which
is no longer true: `plot_harness_grid.py` carries a separate, unrelated change
— `footer_lines` now takes `keep_warmup` and writes "Every ladder row is a
measured point" instead of "The ladder's leading warm-up row is dropped" when
the render passes `--keep-warmup`. The runbook's ladders carry no warm-up row
and its render does pass the flag, so that footer sentence *does* move, in the
direction of being correct. Points, lines and series names are unaffected.

### What the renderer will not do for you

Things the execution-step-1 rehearsal established about the images, none of
which is a bug and all of which change how a chart is read or captioned:

- **The `c=32` overlap reading is retired with the sub-sweeps.** It existed
  because two `--max-docs` budgets met at one rung and the `--table` twin merged
  them into a `reps=6` row whose min..max mixed rep spread with sweep
  disagreement. There is one budget now, so there is nothing to disagree.
- **A ring is not a reason to drop a point, and not yet a finding either.** The
  renderer rings a saturated rung and keeps it. Whether it is an engine result
  or a void one is settled by `in_flight_peak` in the `--table` twin, read
  against `--concurrency` — the renderer cannot do that for you because it does
  not know what cap the run was given.
- **A colour is not stable across charts.** `--series` assigns the palette by
  position in the argument list, so R2 is the second colour on the primary
  chart and the first on S2a. `charts/README.md`'s promise that "a reader who
  learned that colour on one finds it on the other" holds for the engine-flag
  naming and **not** for named series. Either caption every chart so it stands
  alone, or keep one arm order across all three and accept that the arm set
  differs.
- **A point CSV existing is not a finished run.** Both binaries create `--out`
  at start, so `pull_arm`'s "6 point CSVs" gate passes while the sixth run is
  still going. Check the arm's last stderr log for its final `-> index …` line
  before trusting the count.

## Secondary charts

Second renders off the same points, plus one extra arm. No chart above six
lines.

- **S2a — where the index lives.** R2, R8, R4 and one new arm,
  **`os-disk-refresh1`**: `osrate --index-config disk --refresh-interval 1s
  --batch-size 1024` with `OS_RAM_INDEX` unset, segments on the NVMe volume. It
  is what a reader would actually run, and it is the only OpenSearch arm that
  can hold the frozen corpus — the ramindex filled its 12 GiB of tmpfs at
  4,025,699 documents. With R8 in the table, both engines now have a RAM arm
  and a disk arm at the same cadence, which is the pairing this chart draws.
  Reps and runs as the table: 3 and 21.
- **S2b — retired with R6 and R7.** There is one batch size, so there is no
  bulk-size chart. The slot is left named rather than renumbered so that a
  reader looking for it learns it was dropped rather than mislaid.
- **S2c — the ScyllaDB knobs alone.** R1 / R2 / R8. One engine, one axis, so
  there is no false comparison available to draw, and the 1.42x result gets the
  chart it can be quoted from.
- **S2d — resource footprint. A render, not a renderer.** `ftsbench/plot_c4.py`
  already takes `--config NAME:GLOB`, the same shape as `--series LABEL=GLOB`,
  and draws grouped RAM / CPU / index-size bars with the ScyllaDB side split by
  role and summed. Point it at the arm slice globs
  (`--config "R2 scylla-buf376:$R/r2/scylla/probe/cpu-*.jsonl"`). No new chart
  code, and `resource-by-rung.csv` is its table twin.
  `ftsbench/plot_growth.py --metric cpu|rss --probe <config>:<glob>` remains
  available for a per-build time series on one arm if the write-up wants one.

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
for arm in r1 r2 r8; do mkdir -p "$R/$arm"/scylla/{points,samples,logs,probe}; done
for arm in r4 osdisk; do mkdir -p "$R/$arm"/opensearch/{points,samples,logs,probe}; done
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
~3.6 h this campaign is still long enough for that to happen.

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
  is built from the fork and `docker save | ssh … docker load`ed — from
  **`94a23ef2`**, the commit that carries `VECTOR_STORE_FTS_INDEX_DIR`, which
  every arm can run because the knob is inert unless set. The build is native
  on the harness — both boxes are aarch64, so no emulation is involved — and
  the tag is `git describe` on the commit, which is where
  `docker/.env.sut`'s `VECTOR_STORE_IMAGE=…:1.10.0-45-g94a23ef2-arm64` comes
  from. It is ~15 min of billed fleet time and it is on the critical path of
  every start:

  ```bash
  ssh fts-harness 'set -e
    mkdir -p /mnt/nvme/build && cd /mnt/nvme/build
    rm -rf vector-store
    git clone -b p99-fts-ingest-optimization \
        https://github.com/knowack1/vector-store.git
    cd vector-store
    # The fork carries NO tags, and both build scripts derive the version
    # from `git describe`, which fails outright without an annotated tag --
    # so the 1.10.0 tag is fetched from upstream before anything is built.
    git fetch --tags https://github.com/scylladb/vector-store.git
    git rev-parse HEAD                  # expect 94a23ef2c9ff…
    git describe --dirty                # expect 1.10.0-45-g94a23ef2, NO -dirty
    # A dirty tree is refused by build-release and would tag the image
    # -dirty anyway, so a fresh clone is the point, not a nicety.
    TARGETARCH=arm64 ./scripts/run-with-release-toolchain cargo build --release
    ./scripts/build-dockers arm64'
  # the clone is on /mnt/nvme, so it too goes with the next stop; the fork
  # branch is the durable copy.

  docker save scylladb/vector-store:1.10.0-45-g94a23ef2-arm64 \
      | ssh fts-sut docker load          # run from the harness
  ```

  `run-with-release-toolchain` runs the build inside `rust:<toolchain>-bookworm`
  with `--platform linux/$TARGETARCH`, so on the harness it needs the arm64
  rust image pulled and the docker daemon *local* — run it in a shell where
  `tools/fleet_env.sh` has **not** been sourced, or `DOCKER_HOST=ssh://<sut>`
  sends both the build and the resulting image to the SUT's daemon, where
  `docker save` on the harness will not find it.

  Then **read the tag back off the SUT** rather than trusting the load:
  `ssh fts-sut docker images scylladb/vector-store`. The image the compose
  file will ask for is exactly the string in `.env.sut`, and a mismatch is a
  `pull access denied` at `scylla-up` — which is the good failure. The bad one
  is an *older* image carrying the same tag, which is why the arm's real gate
  stays the vector-store's own `index=…` startup line.
- **`/mnt/nvme` is re-made and re-mounted on both boxes**, docker restarted.
- **The corpus restages on the harness** — see the next section.
- **The two clocks are compared, not assumed.** Every rung's CPU and RSS come
  from a join between the SUT's probe timestamps and the harness's stderr
  stamps ("CPU and RSS" below), so a skew larger than one probe tick puts a
  level's samples on its neighbour. Measure it and record the number:
  `ssh fts-sut date +%s.%N; ssh fts-harness date +%s.%N` — chrony keeps this in
  the microseconds (`tools/build_rate_point.sh`), and above **1 s** the join is
  refused rather than padded. Padding would pull the adjacent rung's peak in.

**Do not stop the boxes mid-campaign.** Every stop costs a full re-entry before
any arm can run. If the campaign is split across sessions, split it at an arm
boundary and record which arms were measured in which session — a re-entry
between two arms of the same comparison is a provenance difference the footer
has to carry.

### The corpus — on the harness, and only there

**The corpus is a harness-box file.** Both loaders read it locally through
`--corpus`; nothing streams it and the SUT never sees a line of it. So the
staging question is entirely about one box, and it is on the critical path of
every start: no corpus, no arm.

**Where it lives, and what survives a stop.**

| Path | Box | Survives a stop? | What |
|---|---|---|---|
| `~/corpus.jsonl.zst` | harness **root EBS** | **yes** | ~10.2 GB, `pzstd -10`. The root was grown 8 GiB → 32 GiB on 2026-09-11 for exactly this |
| `/mnt/nvme/data/corpus.jsonl` | harness **instance store** | **no** | 35,448,823,550 bytes prepared, re-made from the archive on every start |

`/mnt/nvme` is destroyed on every stop and the root volume is not, which is the
whole reason the compressed copy sits where it does. **The only thing that
belongs on the harness root is `corpus.jsonl.zst`** — build outputs and the
uncompressed corpus go on `/mnt/nvme`, which has 1.9 TB and no reason to be
careful.

**On every start**, once `/mnt/nvme` is mounted:

```bash
ssh fts-harness 'set -e
  mkdir -p /mnt/nvme/data
  pzstd -d -p 8 -f -o /mnt/nvme/data/corpus.jsonl ~/corpus.jsonl.zst
  sha256sum /mnt/nvme/data/corpus.jsonl'
# expect 1700bb6c9b2652cf7b248e8caff7bfecc54fd2376e9a75e43379aaa79c50c432
```

**~1.5–2 min**, bounded by gp3's 125 MB/s baseline read (10.2 GB ≈ 82 s), not
by `pzstd`'s ~2.85 GB/s. The sha256 is `../FREEZE.md`'s and is what proves the
bytes are the frozen corpus rather than a re-download that drifted; check it
every time, not only when something looks wrong.

**One-time, at the first start after 2026-09-11:** the EBS volume is bigger but
Linux does not notice on its own. **Identify the root device first** — re-entry
formats `/dev/nvme0n1` as the *instance store*, so the EBS root is a different
nvme device and `growpart` against the wrong one is destructive:

```bash
findmnt -no SOURCE /          # e.g. /dev/nvme1n1p1
lsblk
sudo growpart <root-disk> 1   # the DISK, then the partition number
sudo xfs_growfs /             # AL2023 root is xfs
df -h /                       # expect ~32 GiB
```

**If the archive is not on the root volume** — a replaced box, a rebuilt
volume, or the first run of this campaign — it has to be put there before
anything else, and this is the expensive path:

1. **Re-stage from the Swedish Wikimedia mirror**, `~36 min` at ~215 MB/s
   against ~5 MB/s from `dumps.wikimedia.org`, then `prepare_corpus`, then
   `pzstd -10` the result back to `~/corpus.jsonl.zst` so the next start is
   2 minutes instead of 36. Verify against `../FREEZE.md` before compressing.
2. **Not from the laptop.** `../S3-CORPUS-STAGING-PLAN.md` costed the upload at
   37–54 min on the measured uplink, and the laptop does not hold the enwiki
   corpus anyway.
3. **Not from S3.** The bucket `knowacki-p99-fts-corpus` exists, but the IAM
   policy and role are blocked (`DeveloperAccessRole` cannot `iam:CreatePolicy`)
   and the harness has no instance profile, so the box has no AWS identity to
   download with. A presigned URL minted from the console is the fallback that
   needs no IAM change, 12 h expiry, regenerated per session.

Budget the 36 min into the session if the archive's presence has not been
confirmed — it is a quarter of the measurement time.

**Do not use `HARNESS-AWS-RUNBOOK.md` Phase 4 here.** That runbook *generates*
a synthetic corpus at enwiki's mean line length, which is right for a null-sink
run where no document is ever indexed and wrong for every arm in this campaign:
BM25 term statistics, segment merges and the analyzer all depend on real text,
and `../FREEZE.md`'s checksum is the provenance every number here rests on.
The synthetic generator and this corpus are not interchangeable.

### Pull an arm home — the Phase 6 equivalent

`HARNESS-AWS-RUNBOOK.md` Phase 6 does not transfer. It copies one flat
`/mnt/nvme/work/results/` into one flat `$R/scylla/points/` at the end of a
one-arm session; this campaign has five arms, five destinations, and one
artifact that does not survive to the end of the session. So the pull runs
**per arm, as that arm's last step, before its stack is torn down**, and
execution step 7 is a verification that it already happened rather than the
copy itself.

**Why per arm rather than once at the end.** The stack is recreated between
arms — `*-down` then `*-up`, never restarted in place — and `*-down` removes
the containers. The vector-store's two startup lines go with them: once R2 is
up, nothing can produce R1's `docker logs`. Those lines are what license the
arm's CSVs — an image that ignored a knob writes a complete, plausible,
wrongly-labelled ladder — so the numbers and the log that justifies them come
home together or the arm is unlabelled data. `fts-bench-opensearch`'s log on
R4 and `os-disk-refresh1` is the same case.

**One remote directory per arm, and no script change to get it.**
`~/run-arm.sh` and `~/run-os-arm.sh` both read `OUT_DIR` from the environment,
and `run-arm.sh` reads `SAMPLES_DIR` too. `run-os-arm.sh` has no samples
variable — it forwards `"$@"`, so `osrate`'s series directory is passed as
`--samples-dir` on the run line, which these arms pass anyway because
`--index-watch` is what this chart measures. Both scripts also default
`CORPUS` to `/mnt/nvme/work/corpus.jsonl`, which is the *synthetic* runbook
corpus: every run line here overrides it to `/mnt/nvme/data/corpus.jsonl`.

Keep the series directory **out of** the points directory on both halves: the
renderer globs `points/*.csv` and would read a per-second series as a set of
points (`core/src/cli.rs`).

One grid means **one sweep name per arm**, where the two sub-sweeps needed two:

| Arm | Sweep name (`ARM` argument) | Remote `OUT_DIR` / samples | Local destination |
|---|---|---|---|
| R1 | `r1` | `…/results/r1`, `…/samples/r1` | `$R/r1/scylla/` |
| R2 | `r2` | `…/results/r2`, `…/samples/r2` | `$R/r2/scylla/` |
| R8 | `r8` | `…/results/r8`, `…/samples/r8` | `$R/r8/scylla/` |
| R4 | `r4` | `…/results/r4`, `…/samples/r4` | `$R/r4/opensearch/` |
| `os-disk-refresh1` | `osdisk` | `…/results/osdisk`, `…/samples/osdisk` | `$R/osdisk/opensearch/` |

Phase A's calibration rows go under `<arm>/calibration/` and are never globbed
by a chart command: they are a different ladder with a different
`latency_basis`, and pooling them with Phase B would average a latency with a
service time.

The arm directory is the whole mapping: the local directory name is what
`--series` reads, so an arm written to the wrong remote directory becomes a
mislabelled line rather than a missing one. A ScyllaDB sweep is then:

```bash
ssh fts-harness 'REPS=3 LADDER=4,8,16,32 MAX_DOCS=600000 \
    CORPUS=/mnt/nvme/data/corpus.jsonl VS_PORT=16080 \
    OUT_DIR=/mnt/nvme/work/results/r2 SAMPLES_DIR=/mnt/nvme/work/samples/r2 \
    ~/run-arm.sh r2-low --vs-interval 0.25'
```

**`VS_PORT=16080` is not optional and is the one line that makes these arms
reach an engine at all.** `run-arm.sh` derives the vector-store port as
`VS_PORT="${VS_PORT:-$((PORT + 7000))}"` — 16042 at `PORT=9042`
(`HARNESS-AWS-RUNBOOK.md`). That `+7000` is the **null sink's** convention, and
it is right for the campaign that script was written for, where one process
serves CQL on 9042 and the index status on 16042. Here the index is answered by
a real vector-store, published on **16080** (`docker/.env.sut`'s
`VS_HOST_PORT`, the same port `SCYLLA_VS_URI` uses). Without the override
`scyllarate` polls a closed port and every ScyllaDB arm dies at its reset gate,
on billed fleet time, before a single document is inserted. It is passed on
every sweep of R1, R2 and R8, in both phases. The script is left alone rather than
patched: `HARNESS-AWS-RUNBOOK.md`'s own campaign depends on the `+7000`
default, and an override in the caller is what keeps both correct.
Found by the execution-step-1 rehearsal, 2026-09-15.

and an OpenSearch sweep the same shape, with the series directory on the run
line:

```bash
ssh fts-harness 'REPS=3 LADDER=4,8,16,32 MAX_DOCS=600000 BATCH=1024 \
    CORPUS=/mnt/nvme/data/corpus.jsonl RESET_FLAGS= \
    OUT_DIR=/mnt/nvme/work/results/r4 \
    ~/run-os-arm.sh r4-low --index-watch --index-config ramindex \
        --refresh-interval 1s --index-interval 0.25 \
        --samples-dir /mnt/nvme/work/samples/r4/r4-low'
```

**Close the arm out on the harness, before `*-down`.** This block assumes the
arm's resource probe was **started** when its stack came up — see "CPU and RSS"
below for that command and for what the slice and the CPU verdict are.
`$BENCH` is the bench checkout re-entry put on the harness — the tree `source tools/fleet_env.sh` is
run from, and the one compose reads `.env.sut` and the compose files from:

```bash
a=r2; flag=--scylladb-cdc-buf376
cons="fts-bench-scylla fts-bench-vector-store"; mread=anon
ssh fts-harness "set -eu
  cd \$BENCH && . tools/fleet_env.sh
  L=/mnt/nvme/work/logs/$a; mkdir -p \$L
  docker logs fts-bench-vector-store > \$L/vector-store.log 2>&1
  cp docker/.env.sut \$L/env.sut
  docker image inspect --format '{{.Id}} {{index .RepoTags 0}}' \
      \$(grep '^VECTOR_STORE_IMAGE=' docker/.env.sut | cut -d= -f2-) > \$L/image.txt
  .venv/bin/python3 -m ftsbench.verify_arm $flag --log \$L/vector-store.log
  tools/sut_probe.sh stop /mnt/nvme/work/probe/$a.jsonl
  .venv/bin/python3 -m ftsbench.probe_windows --arm $a \
      --probe /mnt/nvme/work/probe/$a.jsonl \
      --stderr '/mnt/nvme/work/results/$a/*-rep*.stderr.tsv' \
      --out-dir /mnt/nvme/work/probe/$a --memory-read $mread \
      --table \$L/resource-by-rung.csv
  .venv/bin/python3 -m ftsbench.verify_cpu_usage --data-dir /mnt/nvme/work/probe/$a \
      --containers $cons --output-json \$L/cpu-utilisation.json"
```

**All four commands run on the harness, inside the `ssh`, while the stack is
still up** — the stray `tools/sut_probe.sh stop` that used to sit outside it
would have run on the laptop, which has neither `SUT_IP` nor that path.
**`.venv/bin/python3`, never the box's own `python3`**: Amazon Linux 2023
ships 3.9, `ftsbench.runmeta` is written in 3.10 syntax, and
`probe_windows` imports it — so a bare `python3` dies on the import at the
one moment the arm cannot be repeated cheaply. `$BENCH/.venv` is the 3.12
venv re-entry symlinks to `~/venv`.
`verify_cpu_usage` reads each container's quota with `docker inspect`, so it
cannot be deferred to laptop work either: once `*-down` has run, there is no
quota left to compare the observed cores against.

**`cons` and `mread` change per arm and the defaults are wrong for most of
them.** `verify_cpu_usage`'s built-in container list is the ScyllaDB pair,
which would report nothing on two of five arms:

| Arms | `cons` | `mread` |
|---|---|---|
| R1, R2, R8 | `fts-bench-scylla fts-bench-vector-store` | `anon` (R8: `anon+cache`) |
| R4 | `fts-bench-opensearch` | `anon+shmem` |
| `os-disk-refresh1` | `fts-bench-opensearch` | `anon+cache` |

On R4 and `os-disk-refresh1` the log is `fts-bench-opensearch`'s and there
is no `verify_arm.py` call — the analyzer check inside `osrate` is that half's
equivalent and stays on. `verify_arm.py` covers **R1 and R2 only**:
`ftsbench/target.py` registers `--scylladb-cdc-buf15`,
`--scylladb-cdc-buf376` and `--scylladb-cdc-buf376-commit30` — the third is now
a target with no arm, and is left registered rather than removed — and there is
no target for a disk-backed index, so **R8 is confirmed by reading its
`index=disk:/var/lib/vector-store/fts` startup line out of the captured log
by hand** — which is why the log is captured before the check rather than
piped through it.

**Then pull, from the laptop, in the shell that holds `$R`:**

```bash
pull_arm() {                      # pull_arm <arm-dir> <scylla|opensearch>
    local a="$1" d="$R/$1/$2"
    test -n "$R" && test -d "$d" || { echo "no such arm directory: $d" >&2; return 1; }
    scp    "fts-harness:/mnt/nvme/work/results/$a/*"   "$d/points/"          || return 1
    scp -r "fts-harness:/mnt/nvme/work/samples/$a/"*   "$d/samples/"         || return 1
    scp -r "fts-harness:/mnt/nvme/work/logs/$a/"*      "$d/logs/"            || return 1
    scp    "fts-harness:/mnt/nvme/work/probe/$a.jsonl" "$R/sut/cpu-$a.jsonl" || return 1
    scp -r "fts-harness:/mnt/nvme/work/probe/$a/"*     "$d/probe/"           || return 1
    # run-arm.sh writes its stderr log and run-windows.tsv beside the CSVs.
    # Both are the probe join's other half, so they belong with the logs.
    mv "$d"/points/*.tsv "$d/logs/" 2>/dev/null
    local n; n=$(ls "$d"/points/*.csv 2>/dev/null | wc -l)
    [ "$n" -eq 6 ] || { echo "$a: $n point CSVs, expected 6 (2 sweeps x 3 reps)" >&2; return 1; }
    local w; w=$(ls "$d"/probe/cpu-*.jsonl 2>/dev/null | wc -l)
    [ "$w" -eq 21 ] || { echo "$a: $w rung slices, expected 21" >&2; return 1; }
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

A non-zero return is the arm's gate, not a warning: the stack that produced it
is still up, which is the only moment re-running a lost rep is cheap.

**Before the stop, verify from `$R` alone.** The count check above is per arm;
this is the campaign's:

```bash
for a in r1 r2 r8;               do ls "$R/$a"/scylla/points/*.csv      | wc -l; done
for a in r4 osdisk;              do ls "$R/$a"/opensearch/points/*.csv  | wc -l; done
ls "$R"/sut/cpu-*.jsonl | wc -l          # 5
grep -l "ingest tuning for" "$R"/r{1,2,8}/scylla/logs/vector-store.log | wc -l     # 3

# Every rung of every arm carries a CPU and RSS reading, and none breached.
ls "$R"/*/*/probe/cpu-*.jsonl | wc -l    # 105  (5 arms x 6 rungs x 3 reps + the
                                         #       c=32 overlap, 21 per arm)
ls "$R"/*/*/logs/resource-by-rung.csv | wc -l   # 5
for f in "$R"/*/*/logs/resource-by-rung.csv; do rss_breach "$f" || echo "^ $f"; done
grep -c ',thin$\|,empty$' "$R"/*/*/logs/resource-by-rung.csv   # named, not silent
```

Then render the primary chart on the laptop with the boxes still running. If
the `--series` command above cannot produce its PNG and its `--table` twin
from `$R` without touching the fleet, **the pull is not finished** — and that
is a five-minute fix now and a re-entry plus a re-measured arm after the stop.

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
SUT's resource-probe JSONL and the rung slices, tables and CPU verdicts cut
from it, and the vector-store startup lines that prove each arm took its
tuning. "Pull an arm home" above is how each of those got there
and how to check that all five did; nothing in it can be done after this
section.

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
`vector-store` built from `knowack1/vector-store` @ **`94a23ef2`** and
`docker save | ssh … docker load`ed onto the SUT. That commit supersedes
`282d9efc` for this campaign and is a strict superset of it: the one added
knob, `VECTOR_STORE_FTS_INDEX_DIR`, defaults to unset, and with it unset the
index path is byte-for-byte the previous behaviour — so R1, R2 and R4 measure
the same image R8 does, and no arm pays for the knob's existence. It is in no registry,
and the instance store takes it with every stop.

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
| R8 | R2 + `VS_FTS_INDEX_DIR=/var/lib/vector-store/fts` |
| R4 | `OS_RAM_INDEX=1`, `OS_RAM_INDEX_SIZE=12884901888`; `refresh_interval: 1s` set by `osrate --refresh-interval` at index create, **overriding the 3 s the shipped `index-config-ramindex.json` carries** — omit the flag and R4 runs at 3 s while `os-disk-refresh1` runs at 1 s |
| `os-disk-refresh1` | `OS_RAM_INDEX` unset, `osrate --index-config disk`, data on the `opensearch-data` volume |

**`VS_FTS_COMMIT_INTERVAL=1s` is set on every ScyllaDB arm** and
`--refresh-interval 1s` is passed on every OpenSearch one. Neither is a
variable in this campaign; both are pinned so that R2 ↔ R4 is a matched-cadence
comparison and nothing else.

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

## The shared offered-rate grid

**Every series is measured on the same x values**, and an x value is a rate the
client was *told* to produce, in documents per second. Series that do not share
x values cannot be drawn on one axis, and a ladder tailored per arm silently
destroys the chart. Add a rung to one arm and you add it to five.

**The grid is set from Phase A and cannot be written down before it runs.** It
spans roughly `0.12 × C_max` to `1.2 × C_max` in about **seven uniform rungs**,
where `C_max` is the highest per-arm ceiling Phase A measured. Both ends are
deliberate:

- the floor is low enough that every arm sits on the diagonal, which is the
  baseline the departures are read against;
- the ceiling is above the **fastest** arm's knee, not the slowest'. Stopping at
  the slowest arm's ceiling leaves the fastest arm's knee unmeasured, and a
  slower arm simply saturates at the top rungs — **saturation is a finding, not
  a gap**, and it is what lets a fast arm and a slow arm share one grid and each
  still show its own knee.

`--keep-warmup` on every chart command: the ladders carry no throwaway rung, so
the floor rung is a measured point and the renderer's default first-row drop
would delete it.

### Two phases, because a rate ladder cannot self-scale

A concurrency ladder finds a ceiling without knowing where it is; a rate ladder
has to bracket one. So the ceiling is measured first, with the instrument that
is correct for it.

| Phase | Ladder | Reps | Published | What it produces |
|---|---|---|---|---|
| **A — calibration** | `--concurrency 4,8,16,32,64,128` (closed loop) | 1 | **never** | each arm's ceiling and `c_sat` — the *how fast can it go* number |
| **B — measurement** | `--target-rate <the grid>` (open loop) | 3 | yes | the chart |

Phase A is the old ladder, unchanged, and that is the point: `ftsbench/pacer.py`
is explicit that closed loop is the right instrument for a maximum-throughput
question and the wrong one for "what does it do at rate X". The campaign now
asks both, with the right instrument for each.

### The bound is documents, not wall time

**`--max-docs 3500000`, one constant for every arm and every rung.** A rung ends
after four million documents, at whatever wall time its own rate implies. Two
reasons, both load-bearing:

- **Every rung then ingests the identical documents.** Only the rate differs.
  A fixed *duration* would give each rung a different subset of the corpus —
  different term dictionaries, segment counts and merge work, which are the very
  things this campaign measures — and would break the "every document exactly
  once, same set per engine" invariant.
- **R4 has a hard ceiling.** The ramindex fills its 12 GiB of tmpfs at
  **4,025,699** documents; the corpus is 8,967,625 (`../FREEZE.md`). A
  whole-corpus rung would kill the arm that is half the only cross-engine read.

**Cost falls out of the bound, and it is the opposite shape from the old one.** A
rung costs `max_docs / rate`, so the *top* of the ladder is cheap and the floor
is expensive:

| Offered rate | Load time at 3,500,000 documents |
|---|---|
| 20,000 | 200 s |
| 40,000 | 100 s |
| 60,000 | 67 s |
| 100,000 | 40 s |
| 140,000 | 29 s |

**Extra resolution near the knee — at the top — is nearly free; lowering the
floor is what costs.** The 20,000 rung alone is ~39% of that ladder's load time.
Set the floor no lower than a baseline needs.

### What this retired, named rather than quietly deleted

A reader who knew the old budgets has to learn that they are gone and why.
**All of the following were closed-loop artifacts and no longer exist:**

| Retired | What it was |
|---|---|
| The **600,000 / 2,621,440** split | one `--max-docs` per sub-sweep |
| The **two sub-sweeps** | low `4,8,16,32` and high `32,64,128` |
| The **≥20-requests-per-worker floor** | what set 2,621,440 = `20 × 128 × 1024` |
| The **`c=32` overlap check** | "if the two sweeps disagree at 32 by more than the rep spread" |
| **"A budget moves for every arm in that sweep"** | the rule that kept the two sweeps comparable |
| The **read-ahead share of a level** | 1,310,720 documents, 50% of the high sweep, front-run |

The request-count plateau those budgets existed to suppress cannot occur on this
axis at all: the rehearsal's flat stretch from `c=16` to `c=32` and `c=64` to
`c=128` happened because those levels carried fewer `_bulk` requests than they
had workers. A rung is now a rate and a document count, and neither is a
function of the worker count.

**A document bound remains** — but as one campaign-wide constant rather than a
per-sweep negotiation. That is a large simplification, not a clean sweep, and
this table is what says which.

### What a rung records that it did not before

Five columns, appended behind `engine`, blank on a Phase A row:

| Column | Read it against | What it settles |
|---|---|---|
| `target_docs_per_s` | — | the x value |
| `achieved_offered_ratio` | 0.95 | whether the rung ran at the rate on its axis |
| `in_flight_peak` | `--concurrency` | **the engine, or the harness** |
| `queue_p99_ms` | `p99_ms` | whether the harness held its own schedule |
| `generator_saturated` | — | ring it on the chart |

**`in_flight_peak` is the one that decides whether a short rung is evidence.**
A rung that fell short with its peak sitting at the cap measured the harness,
and is **void and re-run at a higher cap** — never reported as an engine
ceiling. This is the open-loop replacement for the client-headroom gate G7 that
this campaign deliberately did not carry.

**The caps are per engine and are not the same number**, because the cap binds by
Little's Law and a request is one document on one side and 1,024 on the other:
**`--concurrency 512` on `scyllarate`, `--concurrency 128` on `osrate`**, with
`--queue-depth 1` (read-ahead exists to keep closed-loop workers fed; a paced
producer has no use for it, and it drops resident read-ahead from ~5.17 GB to
~0.52 GB on the OpenSearch side). Both are re-derived from Phase A's p99 before
Phase B runs.

**`latency_basis` is a header fact, not a column.** The ladder *redefines*
`p50_ms`/`p99_ms` rather than adding to them — `intended_start` under a rate
ladder, `service` under a concurrency one — and the append-only rule does not
cover a redefinition. It is what stops a Phase A CSV and a Phase B CSV, which
look compatible, from being pooled.


### The settle timeouts — every arm runs the defaults, and that is a consequence

**No arm overrides a settle timeout**, which is true only because the 30 s
cadence arms were dropped. The reasoning is worth keeping, because it is what
any future 30 s arm would have to re-import.

The vector-store's status endpoint reports one count, the searchable one
(`scylla/src/vstore.rs`: `accepted: None`). The harness's idle rule is "stopped
moving, measured on what the engine has accepted", and where there is no
separate accepted count it falls back to the searchable one. At a 1 s commit
`scyllarate`'s default `--vs-idle-timeout 10` is more than three cadences, so
a build in the gap between two commits is never mistaken for a stalled one and
the default `--vs-settle-timeout 120` is forty. At a 30 s commit the same
default declares the build idle 10 s into every gap — every level would end
`index_settled=false` at a fraction of its rate, and the chart would show a
cadence cost that is the harness's, not the engine's. The fix, if such an arm
ever returns, is the one the source campaign used (`C1_IDLE_TIMEOUT=60` was one
60 s commit cycle): floor both timeouts at four cadences, so
`--vs-idle-timeout 75 --vs-settle-timeout 180` at 30 s.

`osrate` needs no such change at any cadence: `_stats` reports accepted and
searchable separately and its idle rule watches the former. Its defaults
(`--index-idle-timeout 15`, `--index-settle-timeout 180`) stand.

The timeouts in force are recorded in every CSV header (`vs_settle_timeout_s`),
so a reader can check that an arm ran the defaults rather than take this
section's word for it.

## CPU and RSS — what the arm cost to produce its rate

A rate without a cost is half a measurement. This campaign already commits to
two claims that only a resource reading can settle: that the vector-store, on
the same four cores OpenSearch gets, is *CDC-starved rather than CPU-bound*
(`../BUILD-RATE-LOOP.md` measured Scylla at 3.56 of 4 while the vector-store
idled near 1.9 of 4), and that R8's file-backed index shows up as page cache
rather than anonymous memory. Both are carried above as caveats. This section
is what turns them into columns.

**`ftsbench/resource_probe.py` on the SUT, one probe per arm**, sampling every
container in the stack at 1 Hz. It is the one campaign component `DOCKER_HOST`
cannot carry — it reads `/sys/fs/cgroup` where it runs — so it goes through
`tools/sut_probe.sh`, which starts it detached on the SUT and copies the series
back on stop. Running it on the harness instead records the *generator* box's
idle cgroups and reports them as engine numbers; that is a mistake this
repository has already paid for once (`../BUILD-RATE-LOOP.md`).

**What each field is, and the decision behind it.** `rss_bytes` is cgroup v2
`memory.stat` **`anon`**, never `memory.current`, which includes page cache and
would flatter whichever engine touched less disk. `cache_bytes` (`file`) and
`shmem_bytes` (`shmem`) are carried separately so the total is recoverable.
`cpu_cores_used` is a rate differenced off the monotonic `cpu.stat`
`usage_usec` counter and is `null` on the first tick, because reporting 0.0
there would draw a container that was saturated at start-up as idle.

**There is no single memory number for five arms, and the plan does not pretend
there is.** `--memory-read` is a required argument on the slicer for that
reason:

| Arms | Read | Why |
|---|---|---|
| R1, R2 | `anon` | the Tantivy index is in the vector-store's heap |
| R4 | `anon + shmem` | the ramindex is **tmpfs**, which is shmem and not anon — reporting `rss_bytes` alone hides up to 12 GiB of index |
| R8, `os-disk-refresh1` | `anon`, with `cache` beside it | the index is on the NVMe, so it is page cache; this is R8's reading |

The R4 row is the one that changes what the chart can say. That arm holds a
14 GiB heap and a 12 GiB tmpfs ceiling inside one 28g cgroup — about 2 GiB of
slack — and this is the measurement that says whether it held. With R5, R6 and
R7 dropped, **R4 is the only arm that will ever exercise that slack**, so a
`thin` or `empty` rung on it has no sibling arm to corroborate against.

**The ScyllaDB side is two containers and stays two.** `fts-bench-scylla` and
`fts-bench-vector-store` get one row each, split and summed, never merged: the
architecture is a ScyllaDB cluster plus a vector-store cluster holding the
index, and a single merged number would hide where the memory actually goes.

### Start it — per arm, after the tuning gate, before the first sweep

On the harness, from `$BENCH` with `tools/fleet_env.sh` sourced
(`sut_probe.sh` needs `SUT_IP`). **Do not pass `--output`** — the wrapper
appends it, and the engine URLs are `127.0.0.1` because the probe is on the SUT:

```bash
mkdir -p /mnt/nvme/work/probe          # sut_probe.sh scp's the series back here

# R1, R2, R8
tools/sut_probe.sh start /mnt/nvme/work/probe/$a.jsonl \
    --engine scylladb \
    --containers fts-bench-scylla:scylladb \
    --containers fts-bench-vector-store:vector-store \
    --vs-url http://127.0.0.1:16080 --keyspace wiki --vs-index articles_body_fts \
    --interval 1 --duration 0 --label "index-rate $a" \
    --corpus /mnt/nvme/data/corpus.jsonl

# R4, os-disk-refresh1
tools/sut_probe.sh start /mnt/nvme/work/probe/$a.jsonl \
    --engine opensearch --containers fts-bench-opensearch:opensearch \
    --os-url http://127.0.0.1:9200 --os-index wiki-articles \
    --interval 1 --duration 0 --label "index-rate $a" \
    --corpus /mnt/nvme/data/corpus.jsonl
```

It adds no wall clock and no rung: steady state is three `/sys/fs/cgroup` file
reads per container per second against a cgroup path resolved once and cached,
plus one `docker inspect` pair per container at arm start. It does share the
SUT's eight cores with the engines, and that reaches the footer.

### Cut it to the rungs — `ftsbench/probe_windows.py`

One probe spanning a whole ladder × 3 reps has to be sliced before it can sit
beside a point. **Nothing in the harness records a wall clock** — the point CSV
carries `wall_s`, the per-second series carries `t_s`, the probe carries
`t_elapsed_s`, and all three are relative. The join is
`header.started_at + t_elapsed_s` against the epoch-stamped stderr log
`run-arm.sh` writes, which is what `ftsbench/plot_growth.py` already does for
the deck's growth charts, and what `HARNESS-AWS-RUNBOOK.md` Phase 8 does by
hand for its sink samplers.

**The window is the build, not the level.** `run_sweep` announces a level
*before* it opens the inserter (`core/src/sweep.rs`), so `[i/N] concurrency=X`
→ `-> N docs in …s` includes the per-level keyspace drop and rebuild — idle
time on the indexing container that drags a median down. The measured window
runs from `index is SERVING at 0 documents` (`scylla/src/reset.rs`) or
`index is answering at 0 documents` (`opensearch/src/reset.rs`) to the result
line. The reset span is kept as `reset_s`, not discarded: a reset that grew
across an arm is a finding. **The arm most likely to show one is now R8**,
where the per-level drop and rebuild has to clear a file-backed index off the
NVMe rather than free heap; R3, which was the previous candidate, is gone.

**One probe per arm is the better shape, not merely the cheaper one.**
`cpu_cores_used` is `null` on the first tick *of the file*; slicing an arm-wide
series loses one sample for the whole arm, where a probe started and stopped at
every point throws one away at every rung.

**BLOCKER — per-rung CPU attribution does not work on this axis yet, and it
fails loudly.** `ftsbench/probe_windows.py` keys a window on
`(sweep, concurrency, rep)`, parsed out of the harness's level announcement by
`LEVEL_RE = r"\[\d+/\d+\] concurrency=(\d+)"` (line 67). On a rate ladder
concurrency is a **constant cap** across every rung, so that key collides and
`probe_windows.py` exits 1 on the duplicate (lines 357–361). That is the right
failure — loud, not a silent mis-slice — but it means **this section cannot be
executed until a one-line change lands in that file**, keying on the rate
instead. The harness already emits it: the announcement now reads
`[i/N] concurrency=512 target_docs_per_s=20000`.

`ftsbench/` is **outside this change's scope** (`bench/build-rate/**` only), so
the fix is a named prerequisite rather than part of it. It is on the critical
path to a fleet run: the RSS-breach gate below is the one gate that voids an
arm, and it is read off these slices.

**Slices were named for the sweep, not the arm**, because the two sub-sweeps
overlapped at `c=32` and both numbered their reps from 1. With one grid and one
budget there is no overlap to disambiguate, so the slice name's discriminator
becomes the rate. Whatever `probe_windows.py` is changed to emit must still
satisfy `ftsbench/verify_cpu_usage.py`'s pattern, which reads everything before
`-c<n>-<rep>` as the configuration: the only CPU gate this repository has runs
on the slices **unchanged**.

## Reps and signals

**N=3, campaign-wide**, one repetition count moved together by `REPS=`
(Karol, 2026-09-09). Rep-major, so a cold-start effect lands in every arm's
first rep rather than in one arm's whole ladder.

**Top-up rule**, carried over and re-pointed: the cadence pairs it named
(R2↔R3, R4↔R5) are gone, so it now applies to the two comparisons that remain
— **R1↔R2** (the writer-buffer delta) and **R2↔R8** (where the index lives).
Either landing within ~5% at `c_sat` is topped up to N=5 *at that rung only* —
2 arms × 2 reps × 1 rung, not a re-run. **R2↔R4 is deliberately excluded**: a
cross-engine gap that small is a finding to report, not a spread to tighten,
and topping it up invites reading a tie into it.

Per point:

| Source | Fields |
|---|---|
| `<arm>/<engine>/points/*.csv` | the twenty-two shared columns; the six that are this chart's — `index_docs`, `index_docs_per_s`, `index_lag_docs`, `index_settle_s`, `index_settled`, `index_status` — plus `batch_size` and `engine`; header stamps the linked driver version, the live engine version and every timeout |
| `<arm>/<engine>/samples/<rep>/c<conc>-*.csv` | the ten-column per-second series, `--samples-dir` — the tail after the last insert is the part a per-level average cannot show, and `charts/rate_vs_index_size.py` draws it |
| `sut/cpu-<arm>.jsonl` (`resource_probe`, 1 Hz, on the SUT) | the arm-wide series, one record per container per tick: `cpu_cores_used`, `cpu_seconds_total`, `rss_bytes` (anon), **`shmem_bytes`** (the ramindex arms' index), **`cache_bytes`** (R8's reading), `mem_limit_bytes`, `disk_read_bytes`, `disk_write_bytes`, `index_size_bytes`, and on the vector-store `index_docs` / `index_status` |
| `<arm>/<engine>/probe/cpu-<sweep>-c<conc>-<rep>.jsonl` | the same records cut to one rung's build window, 21 per arm — the form `ftsbench/verify_cpu_usage.py` reads |
| `<arm>/<engine>/logs/resource-by-rung.csv` | the per-rung twin: `cpu_cores_peak`, `cpu_cores_median`, `rss_peak_bytes`, `shmem_peak_bytes`, `cache_peak_bytes`, `mem_peak_bytes`, `mem_headroom_bytes`, `reset_s`, `build_s`, `samples`, `index_docs_last`, and the `thin`/`empty` note |
| `<arm>/<engine>/logs/cpu-utilisation.json` | `verify_cpu_usage --output-json`: per rung, peak cores against the `docker inspect` quota, and the saturated verdict |
| `<arm>/<engine>/logs/<sweep>-rep<n>.stderr.tsv` | the epoch-stamped run log — the other half of the probe join, and the only wall clock the harness produces |
| vector-store log (R1, R2, R8) | the two startup lines above; `added/s`, `commits`, `committed/s` |
| `index-rate-vs-offered.csv` | the chart's `--table` twin: series, metric, `offered_docs_per_s`, reps, median/min/max, shortest wall, `saturated`, `in_flight_peak` |

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
| Short point | a level under 3 s is not a measurement; the renderer names every one in the footer, and the fix is a bigger `--max-docs` — which is one campaign-wide constant, so it moves for **every** arm and every rung or not at all |
| **The cap did not bind** | **blocking.** `in_flight_peak` against `--concurrency`. A rung with `generator_saturated=true` *and* its peak at the cap measured **the harness**, not the engine: **void it and re-run that rung at a higher cap** — never report it as an engine ceiling. This is the open-loop replacement for the G7 the paragraph above declines to carry, and unlike G7 it removes only points that are not measurements of the engine at all |
| **The harness held its schedule** | `queue_p99_ms` against `p99_ms`, per rung. Under ~10% the latency columns are the engine's; above ~25% the chart is measuring the producer and the rung is annotated as such. This is what makes coordinated-omission safety a number rather than a claim, and it is why `latency_basis=intended_start` is in the header |
| **Achieved vs offered** | `achieved_offered_ratio` ≥ 0.95, or the rung is `generator_saturated` and ringed. Annotating, never dropping: **saturation is a finding, not a gap**, and it is how a fast arm and a slow arm share one x grid while each still shows its own knee |
| Thin series | a build with under three index readings is skipped by name in the growth chart. **Both poll intervals default to 1.0 s and neither sweep command overrides them**, which is enough for a 45–220 s level but not something to leave to chance: pass `--vs-interval 0.25` on the ScyllaDB sweeps and `--index-interval 0.25` on the OpenSearch ones, on the run line, the same way `--samples-dir` is passed. The rehearsal ran at 0.25 s throughout |
| Not settled | `index_settled=false` is a lower bound: drawn hollow, the reason (`index_status`) named in the table |
| Arm took | the vector-store's two startup lines match the arm before its ladder runs; `osrate`'s analyzer check stays on |
| Manifest | every arm directory carries the `.env` it ran with and the image commit, alongside the CSVs |
| **RSS breach** | **blocking, the one gate on this campaign that voids an arm.** The vector-store stops adding documents once `VECTOR_STORE_MEMORY_LIMIT` (26 GiB) is reached, logs an error and keeps answering queries, so a breach is *silent document skipping* and the rate beside it counts documents that were never indexed — `../BUILD-RATE-LOOP.md` caught exactly this at a 27.68 GiB peak. Read three ways: anon `rss_bytes` against the 26 GiB budget, the arm's memory read against the 28g cgroup `mem_limit_bytes` (`rss_breach` above), and `index_docs_last` reaching the cap as corroboration. The arm is re-run, not annotated. **`cache_bytes` on R8 and `os-disk-refresh1` is exempt**: a file-backed index is page cache the cgroup reclaims rather than kills, which is what those arms exist to measure |
| **CPU attribution** | **annotating, never dropping** — the same reason G7 is not carried. Per rung, the indexing container's peak `cpu_cores_used` against its quota: `ok` at ≥0.85, `not-CPU` below it, `?` where no series covers the window. A `?` is not a pass. A plateau at `not-CPU` is still plotted; what changes is that it may not be described as the engine's throughput limit |
| **Probe source** | every sample reads `source=cgroup-anon`. The `docker stats` fallback is not anon-only and has no CPU counter, so one fallback sample destroys both the R8 cache reading and the ramindex shmem reading; `probe_windows` refuses the arm rather than reporting it |
| **Clock skew** | harness-to-SUT skew under one probe tick (1 s), measured at re-entry and recorded. The window is never padded to cover skew — padding pulls the neighbouring rung's peak in |

## The final refresh — disclosed, not hidden

Every arm runs with the harness's default: when the client has stopped, the
engine has accepted everything, and the searchable count is still short, the
harness asks the index to publish once, and `index_status=refreshed` records
every point where it did. **Only `osrate` can ask.** Its probe answers the
settle hint with a `_refresh`; the vector-store's status endpoint offers no
equivalent and `scyllarate`'s probe keeps `core`'s default, which is never to
ask (`core/src/index.rs`, `settle_hint`). So the asymmetry is one-sided:
**R4 and `os-disk-refresh1` are credited with a publish their configured policy
had not yet delivered when the loader finished, and R1, R2 and R8 are not** —
the ScyllaDB arms wait for their own next commit.

**Dropping the 30 s arms shrank this asymmetry without removing it.** At 30 s
the credited publish could be worth up to a full 30 s of build wall; at the 1 s
cadence every arm now runs, it is worth at most 1 s. That is small against a
45–125 s level but it is not nothing, it runs one way only, and it lands on the
side of the one cross-engine comparison this campaign has. It is disclosed, not
netted out.

The alternative for the OpenSearch arms, `--no-index-final-refresh`, reports
what the policy alone delivered and was considered and not chosen: it turns a
build that finished just short of its next refresh into one that reports
nothing. The footer states which it was, and the `refreshed` count per
OpenSearch arm goes in the table.

## Caveats to carry into the write-up

- **The cap is not the talk's operating point.** `../BUILD-RATE-LOOP.md`
  measured the engine ranking *inverting* between 1.2M and the 8.97M corpus.
  This ladder answers "what saturates", not "what wins at scale".
- **`--batch-size 1024` is an assumption this campaign inherits and does not
  test.** With R6 and R7 dropped there is no bulk-size arm at all, so nothing
  here says whether 1024 is good, bad or at a knee — and a batch optimum is not
  a constant in any case: it moves with document size and with segment-merge
  pressure, and the second moves with corpus size. Every OpenSearch number on
  this chart is "OpenSearch at batch 1024", never "OpenSearch".
- **A request is 1,024 documents on one engine and 1 on the other, and the x
  axis no longer inherits that.** An offered rate is documents per second on
  both halves, so R2 ↔ R4 compares one quantity at each x value. What the
  asymmetry still costs is *in-flight depth* at a given rate — measured at
  roughly 200x against `../engine-mock` at 50,000 docs/s — which is recorded per
  rung as `in_flight_peak` and belongs in the write-up as a property of a bulk
  API rather than as a correction to the chart.
- **The loader's read-ahead front-ran half a level, and Phase A still does.**
  At depth 10, `c=128` and batch 1024 the channel holds 1,310,720 documents —
  ~5.17 GB resident on a 61 GiB box with no swap, under the ~8 GB guard, but
  enough that the top rung's submitted rate sat between a drain rate and a
  steady-state one. Phase B does not have this: a paced producer releases on a
  schedule, so **`--queue-depth 1`** brings it to 131,072 documents ≈ 0.52 GB.
  **Phase A still runs closed loop at depth 10 and the arithmetic still applies
  to it** — which is one more reason its rows are not published. The resident
  figure is `queue_depth × concurrency × batch_size` and nothing else. See "This axis
  raises the budget".
- **The ramindex arms cannot hold the corpus** (12 GiB of tmpfs, 4,025,699
  documents) and cannot be raised at parity — tmpfs counts against the same
  28g the heap does. Any full-corpus OpenSearch number comes from the disk
  arm, which is why S2a exists.
- **OpenSearch gets four cores; the ScyllaDB stack gets eight.** Load-bearing
  for R2↔R4 — now the campaign's only cross-engine read, so this caveat carries
  more weight than it did across three — and irrelevant inside any
  single-engine chart. It reaches the footer.
- **Cadence cost is not measured on either engine.** With the 30 s arms gone,
  every arm publishes at 1 s and nothing separates the cost of a cadence from
  any other cause. No chart off this campaign may claim what a slower commit or
  refresh interval costs in visibility, in either direction.
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
- **CPU and RSS are cgroup readings, attributed per rung by a wall-clock join.**
  Peak and median over each rung's build window, with the per-level reset
  excluded and carried separately. `rss_bytes` is `memory.stat` *anon*, so the
  ramindex arms' tmpfs index appears in `shmem_bytes` and R8's in
  `cache_bytes`; the memory read is stated per arm rather than assumed. The
  ScyllaDB side is two containers, reported split and summed, never merged.
  The probe shares the SUT's eight cores with the engines it measures.
- **R4 has about 2 GiB of slack and this is the first pass that will see it.**
  A 14 GiB heap plus a 12 GiB tmpfs ceiling inside one 28g cgroup leaves very
  little, and `rss + shmem` against `mem_limit_bytes` is the reading that says
  whether it held. R4 is now the only ramindex arm, so there is no sibling to
  corroborate a surprise against. A `thin` or `empty` rung is named in
  `resource-by-rung.csv`, never dropped from it.

## Cost

An estimate with its derivation, never a measurement. **On this axis a rung
costs `max_docs / rate` plus reset and settle**, so the arithmetic is arithmetic
rather than extrapolation — but the rates are Phase A's and Phase A has not run,
so the figure below is a shape with a placeholder in it.

At `--max-docs 3500000`, load time alone is 200 s at 20,000 docs/s and 29 s at
140,000. A seven-rung uniform ladder over that span is ~454 s of load per rep;
add reset and settle — **assume ~40 s per rung until an arm closes out and
replaces it** — and a rep is ~13 min per arm. Five arms at N=3 ≈ **3.3 h**,
against the ~3.9 h the concurrency grid carried. **Phase A eats most of that
margin**: six closed-loop rungs at N=1 per arm, short levels, but five arms of
them.

**The floor rung is the expensive one and the top is nearly free** — 20,000
docs/s alone is ~39% of that ladder's load time, while another rung at the knee
costs 30–50 s. Buy resolution at the top; think twice before lowering the floor.

Call it **3.3–4 h**, band 3–5 h, **~$14–22 at the $4.37/h fleet rate** — plus
re-entry, which is billed like anything else:
~15–20 min of image rebuild, corpus decompress and mounts before the first arm
runs, and more if the campaign is split across sessions. **If
`corpus.jsonl.zst` is not on the harness root, add ~40 min** (mirror re-stage,
prepare, compress) — ~$3, and it is one-time only if the archive is written
back.

**Budget 4.5 h and do not plan a session shorter than that.** The campaign
cannot be paused: every stop wipes `/mnt/nvme` and costs a full re-entry, so a
split has to fall on an arm boundary and the provenance difference then reaches
the footer.

**The big movements in this figure, all already taken.** Dropping R3, R5, R6
and R7 took ~3 h *off* — R7 because batch 1 is the slowest way to feed `_bulk`,
R3 because its 30 s cadence set the settle budget for everyone. The move to the
offered-rate axis took the 2,621,440 high sweep off again and put Phase A on,
which roughly cancel. The remaining levers are in "Cost levers" and every one of
them now costs a reading outright.

**The resource probe is not on the cost line.** It adds no run, no rung and no
wall clock: steady state is three `/sys/fs/cgroup` reads per container per
second against a path resolved once and cached, plus one `docker inspect` pair
per container at arm start. What it does cost is a share of the SUT's eight
cores, alongside the engines it is measuring, and that reaches the footer.

**Cost levers, decided before launch, not mid-run.**

| Lever | Saving | What it costs |
|---|---|---|
| Drop `os-disk-refresh1` | −0.7 h | S2a loses its OpenSearch disk arm and R8 has nothing to pair with; the campaign also loses its only OpenSearch arm that can hold the full corpus |
| Drop R8 | −0.7 h | the where-the-index-lives read goes, and S2a loses its ScyllaDB disk arm |
| Raise the grid's floor rung | −0.3 h per rung dropped | the diagonal baseline shortens, and the slowest arm loses the part of its curve that was still keeping up |
| N=3 → N=1 on the middle rungs | −0.8 h | the middle of every curve carries no spread |
| Phase A at three rungs instead of six | −0.3 h | the ceiling each arm's grid is derived from is bracketed more coarsely, and a mis-set grid costs a re-run of Phase B |

**The levers that were here are mostly spent.** Dropping R3, R5, R6 and R7 was
the large saving, and it was taken up front rather than mid-run. What is left
cuts into readings rather than into redundancy: with four arms and three
readings, every remaining lever costs a reading outright. The one direction
worth spending *into* is now **resolution at the knee**, and on this axis it is
cheap — a rung at 100,000 docs/s costs 40 s of load, against the 200 s a rung at
the floor costs. That is the only cheap improvement left in this table, and it
is the one that narrows the ± on every "keeps up to N docs/s" sentence the
campaign will write.

## Execution order

1. **Rehearsal against `../engine-mock`**, on the laptop. **DONE, 2026-09-15 —
   `../results/rehearsal-index-rate-2026-09-15T1938Z/`, which carries its own
   README.** All five arms ran the full grid at N=3 against the mock, one mock
   per arm restarted between them: 30 point CSVs, zero errors, zero failed
   requests, `index_docs_per_s` populated and `index_settled=true` on every
   row, every header carrying a `-null-sink` version, and each mock's
   `docs_accepted` reconciling with its CSVs exactly (504,000 per arm) with
   `unexpected_requests` holding only the two documented HTTP read-backs and
   nothing at all on CQL. **No number from it is an engine number**, and the
   arm rates in it were set by a chosen `--delay-ms` rather than measured.
   What it settled, and what it cost the fleet nothing to learn:
   - **Eight lines read as one image, so the two-render by-engine fallback is
     retired for good.** The "three adjacent ScyllaDB purples" came from the
     *sixteen*-line command; at four named series the palette separates them
     into four clearly distinct colours and the legend sits clear of every
     curve. The primary chart rendered `(8 lines, 48 points)`, S2a the same,
     S2c `(6 lines, 36 points)`.
   - **The `VS_PORT` defect above**, which would have killed all three
     ScyllaDB arms at their reset gate on billed time.
   - **Execution step 5's budget concern, reproduced rather than predicted** —
     see "This axis raises the budget".
   - Three renderer limitations now recorded under "What the renderer will not
     do for you".
2. Build the vector-store image from **`94a23ef2`** on the harness and load it
   onto the SUT — part of re-entry, not extra work. The compose passthrough
   and the `vector-store-fts` volume are already in the tree; `VS_FTS_INDEX_DIR`
   is set for R8's arm only.
3. **"The results directory"**, then **"Start the boxes"**, then **"Fleet
   re-entry"** — `$R` and its five arm subdirectories on the laptop first,
   both boxes started from the console with the tab kept alive, SSH
   re-pointed, private IPs confirmed against `.env.sut`, `/mnt/nvme` re-made,
   images re-pulled and the vector-store image rebuilt, and the corpus
   decompressed onto the harness's `/mnt/nvme` and checked against
   `../FREEZE.md`'s sha256 — budget 36 min instead of 2 if the archive is not
   on the harness root.
4. **Smoke**: 2 rungs × all five arms at a 20k cap. Gates: every point
   complete, every arm's startup lines match, R8's line says `index=disk:`,
   R4 does not hit ENOSPC on its tmpfs, and **every ScyllaDB arm settles on
   `scyllarate`'s default timeouts** — no arm overrides them now, so an
   unsettled ScyllaDB point at a 1 s commit is a finding rather than an
   expected cost. **Plus the
   probe's own smoke, which is where a wrong container name costs two minutes
   instead of an arm**: the probe is running and its file is growing, every
   sample reads `source=cgroup-anon`, `probe_windows` finds one window per rung
   per rep with no `empty` note, and the recorded clock skew is under 1 s.
5. **Phase A, then the grid.** Run the calibration ladder (closed loop, N=1)
   on every arm and read each one's ceiling off it; `C_max` is the highest.
   The grid is then ~7 uniform rungs from `0.12 × C_max` to `1.2 × C_max`,
   **written down once and used by every arm**. Re-derive the two caps from
   Phase A's p99 by Little's Law before Phase B starts, and confirm three
   things a laptop could not answer: that `in_flight_peak` at the top rung
   leaves headroom under the cap on both halves, that the build covers ≥4
   commit intervals at 1 s, and that R4's tmpfs holds 3,500,000 documents
   against its 4,025,699 ceiling with the 14 GiB heap beside it in the same 28g
   cgroup — the arm has about 2 GiB of slack and this is the first thing that
   will touch it. **If the budget moves it moves for every
   arm in that sweep**, never for the one that fell short.
6. **The ladders**, one arm at a time in table order, stack recreated between
   arms: R1, R2, R8, then R4, then `os-disk-refresh1`. A surprise in R2 can
   still change the plan before the OpenSearch arms run.
   Each arm is: `*-up` → confirm the startup lines → **start the probe** →
   the two sweeps → **close the arm out** (probe stop, slice, CPU verdict, all
   on the harness while the stack is live) → `pull_arm` → `*-down`. The arm is
   not finished until its `pull_arm` returns zero, and an RSS breach makes it
   return non-zero while re-running is still cheap.
7. Confirm all five arms came home — the campaign-wide checks, including the
   105 rung slices and the zero-breach sweep, and the chart
   render at the end of "Pull an arm home" — then **"Stop the boxes"**: both rows confirmed
   `Stopped` with no public IP, said so explicitly. Everything after this is
   laptop work.
8. Render the primary chart, S2a and S2c; `--table` for each; record each arm's
   ceiling and `c_sat` in `../TUNING.md` with the run that produced it, with
   the CPU and RSS at that rung beside them out of `resource-by-rung.csv`.

## Where it lands

Nothing new on the main deck until the pass has been written up. The
candidate is one image — the primary chart or, if eight lines do not read, the
by-engine split decided at the rehearsal — for S12's neighbourhood, with the footer clauses
above mandatory: what one operation is on each engine; that ScyllaDB runs at
batch 1 and OpenSearch at 1,024, that this no longer bends the x axis but does
show up as a ~200x spread in `in_flight_peak` at one identical offered rate;
that 1,024 is a fixed assumption and not a measured optimum; that every arm runs
a 1 s cadence, that
**cadence cost is therefore unmeasured and unclaimable**, and that the harness
asked OpenSearch for the final publish while ScyllaDB waited for its own
commit; the 4-against-8-core split **and, now that it is measured rather than
assumed, what each side actually drew of it**; the client floor as context;
which points are lower bounds and why; the cap and the sharding; and
PRELIMINARY.

The CPU and RSS reading earns two clauses of its own wherever it is shown: that
memory is the cgroup's *anon* figure with the tmpfs and file-backed index costs
named separately rather than folded in or dropped, and that a rung marked
`not-CPU` is plotted but may not be called an engine throughput limit.
