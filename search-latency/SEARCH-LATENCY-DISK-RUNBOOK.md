# Search-latency runbook — both engines, both indexes on disk, top-k 100

**Hand this file to Claude Code as the instruction and it runs the campaign
end to end.** It is the short form of
[`SEARCH-LATENCY-AWS-RUNBOOK.md`](SEARCH-LATENCY-AWS-RUNBOOK.md): one storage
tier instead of two, one top-k instead of three, three arms instead of six, and
**one session of ~5 h instead of four sessions of ~23 h**.

| | this runbook | [the full one](SEARCH-LATENCY-AWS-RUNBOOK.md) |
|---|---|---|
| SUT configurations | 2 | 4 |
| Arms | 3 | 6 |
| Index location | **disk only, both engines** | RAM and disk, both engines |
| Top-k | **`100` only** | `10`, `100`, `1000` |
| Query classes | all six | all six |
| Concurrency ladder | `1,2,4,8,16,32,64,128` | same |
| Corpus | full enwiki, 8,967,625 docs | same |
| Measured cells | **438** | 2,619 |
| Wall | **~5.1 h, ~$22** | ~22.8 h, ~$100 |

Everything it writes is a strict subset of the full campaign's schema — same
seventeen columns, same sweep naming with `k100` in it — so a later full pass
can merge these arms rather than re-measuring them.

The governing plan is [`../AWS-RUN-PLAN.md`](../AWS-RUN-PLAN.md) Phase 3 and the
read-path fairness rules are [`../COMPARABILITY.md`](../COMPARABILITY.md). Where
this file and those disagree, they are right and this file has drifted.

---

## What holding the storage tier fixed buys, and what it costs

Both engines answer from an index on the same NVMe, in the same size cgroup,
with the same analyzer. That is the cleanest engine comparison this repository
can produce, and it is cleaner than the full campaign's headline pairing:

| | this runbook | the full campaign's diagonal |
|---|---|---|
| Index location | **held fixed — both on NVMe** | ScyllaDB RAM vs OpenSearch disk |
| Container memory | **28g each — parity intact** | OpenSearch raised to 40g for its tmpfs arm |
| JVM heap | **14 GiB, as shipped** | cut to 8 GiB to make tmpfs room |
| `_source` | **enabled — both engines store the documents** | disabled on the OpenSearch RAM arm |

Three disclosures the full campaign has to carry disappear here, because the
configurations that forced them are not run. **A gap measured by this runbook is
an engine difference, not a storage-tier difference and not a memory-budget
difference.**

**What it costs, and it is not small: this is not how ScyllaDB FTS ships.**
The full-text index is in RAM today, and `VECTOR_STORE_FTS_INDEX_DIR` is a fork
knob (`94a23ef2`) exploring what a durable index would look like — see
`../../CLAUDE.md`, where index durability sits under *hardening*, not under M1.
So:

- **the matched-storage reading** — engine against engine with the tier held
  constant — is what this campaign is for, and it is honest;
- **the deployment reading** — "what does ScyllaDB FTS cost today" — is **not
  answered here**, because today it answers from RAM. The full campaign's A1/A2
  are the arms that answer it.

Both readings go on every chart. A slide that shows this number and says "ScyllaDB
FTS" without saying "with the index on disk" is wrong.

### What this runbook cannot answer

| Question | Needs |
|---|---|
| Where the index lives, per engine | the RAM arms — full runbook, configs 1 and 4 |
| How ScyllaDB FTS performs as it ships | the RAM arms — full runbook, config 1 |
| How latency scales with top-k | `k=10` and `k=1000` — full runbook |

---

## Why `k=100`

One value, a header fact and a column, not a dimension — which is what
`--limit` was designed to be (`core/src/report.rs`, column 14).

`100` rather than `10` because it is the middle of the legal range and the more
demanding of the two realistic ones: ScyllaDB's M1 makes `LIMIT` mandatory and
caps it at **1000**, so `10 → 100 → 1000` is the whole span, and 100 sits where
a real result page with over-fetch for re-ranking sits. `10` would be the
friendliest number to both engines and the least likely to separate them.

**`k100` still goes in every sweep name.** `ftsbench.probe_windows` keys windows
on `(sweep, concurrency, rep)`, and a later run at another k merging into this
`$R` would collide without it. It costs nothing now and it is what makes these
arms mergeable later.

---

## The three arms

| Arm | Dir | Binary and interface | Request |
|---|---|---|---|
| **D1** | `d1-cql-disk` | `scyllasearch --interface cql --statement prepared` | `SELECT … WHERE BM25(body,'q') > 0 ORDER BY BM25(body,'q') LIMIT 100` |
| **D2** | `d2-vstore-disk` | `scyllasearch --interface vector-store` | `POST /api/v1/indexes/wiki/articles_body_fts/bm25` |
| **D3** | `d3-os-disk` | `ossearch --index-config disk` | `POST /wiki-articles/_search`, `query_string` |

**D1 and D2 share one stack and one index** — `cql` minus `vector-store` is
ScyllaDB's own read overhead, and the subtraction is only valid because both
arms read the same index at the same moment and neither fetched documents.

| Sweep set | Ladder | Classes | Top-k | Reps | Sweeps | Cells |
|---|---|---|---|---|---|---|
| **ladder** | `1,2,4,8,16,32,64,128` | all six, one sweep each | `100` | 3 | 6 | **144** |

432 cells across three arms, plus the optional axis:

| Axis | Arms | Note |
|---|---|---|
| `--fetch-documents` at `c=16`, `rare_term` | D1, D3 | D2 **refuses** it — the BM25 endpoint returns primary keys only |
| the refusal self-test | D2 | 3 s, proves the refusal happens on the flags before it connects |

---

## Two constraints that shape every command here

### One query class per sweep, always

`ftsbench.probe_windows` keys every window on `(sweep, concurrency, rep)` and
**exits 1 on a duplicate** (`ftsbench/probe_windows.py:353-361`); its level
regex reads only the concurrency out of the harness's cell announcement. Six
classes at one level in one file are six identical keys and a refused arm. So
the class goes in the sweep name and each sweep runs one class — and reps are
separate invocations, one ladder traversal each, for the same reason.

### The statement mode is `prepared`

Every CQL request goes through a prepared statement, prepared once per distinct
query before the matrix starts. **This is an override**: `scyllasearch` defaults
to `--statement literal`, which re-parses per request and is what Lucene's
`query_string` does on the other side. Prepared is the application-shaped
measurement; it is not parser parity, and every D1 chart says so.

It is set as a default inside the sweep script rather than on the run lines, so
no sweep can be written without it — and Phase 7 gates on the `# statement`
header, because `${VAR:-default}` yields to an exported variable and nothing
else in the output would reveal a literal run.

---

## The fleet

| Alias | Instance | Role |
|---|---|---|
| `fts-harness` | `i-08d8d2505e16683f7` · `k-nowacki-fts-benchmark-harness` | runs `scyllasearch` and `ossearch`, holds the corpus, builds the vector-store image |
| `fts-sut` | `i-0e3e4b6b02e654b7f` · `k-nowacki-fts-benchmark-sut` | runs the engine under measurement |

Both `i8g.2xlarge` (8 vCPU Graviton4, 61 GiB, aarch64), `eu-north-1b`, Amazon
Linux 2023, user `ec2-user`, key `~/.ssh/KarolNowackiAws.pem`. Private RTT
0.353 ms, measured — subtract nothing for it; it is part of what a client pays
and it is identical on all three arms.

**One engine at a time.** The SUT has eight cores; running both stacks at once
would make every latency a contention measurement.

---

## The one thing to get right before anything else

**`VECTOR_STORE_FTS_INDEX_DIR` is what makes D1 and D2 disk arms, and an image
older than `94a23ef2` ignores it in silence.** The arms then measure a RAM index
while the directory name, the logs and the write-up all say disk — and **index
location is in no CSV column**, so nothing in the artifacts contradicts it.

This is the campaign's only undetectable-after-the-fact failure. It is gated
before the arms run, off the container's own startup line, never off the
environment that was supposed to set it.

| Blocked | Consequence |
|---|---|
| the vector-store image predates `94a23ef2` | D1 and D2 are RAM arms wearing disk labels; the whole ScyllaDB half is void and looks fine |
| the stack comes down between D1 and D2 | the index is rebuilt, the two arms read different indexes, and `cql` − `vector-store` stops meaning anything |
| a sweep runs without `--no-index-build` and the count does not match | **the keyspace is dropped**, on billed fleet time |

---

## Phase 0 — the results directory, on the laptop

Everything on the fleet is ephemeral: `/mnt/nvme` is destroyed on every stop, so
**the laptop is the only place results survive**.

```bash
# Run this ONCE, at the very start of the session, and keep the shell.
export RUN_ID="search-latency-disk-$(date -u +%Y-%m-%dT%H%MZ)"
export R="$HOME/Projects/Scylla/p99/bench/results/$RUN_ID"
for arm in d1-cql-disk d2-vstore-disk; do
  mkdir -p "$R/$arm"/scylla/{points,latencies,logs,probe}
done
mkdir -p "$R/d3-os-disk"/opensearch/{points,latencies,logs,probe}
mkdir -p "$R"/{env,corpus,queries,scripts,sut}
printf '%s\n' "$RUN_ID" > "$R/RUN_ID"
printf 'session opened: %s\n' "$(date -u +%FT%TZ)" >> "$R/env/sessions.txt"
ln -sfn "$RUN_ID" "$(dirname "$R")/search-latency-disk-latest"
echo "results -> $R"
```

**`latencies/` is beside `points/` and never inside it.** Phase 7 globs
`points/*.csv`; a latency distribution read as a set of points is a silent
corruption of the analysis.

If the shell is lost, recover with
`export R="$(readlink -f bench/results/search-latency-disk-latest)"` rather than
recomputing the timestamp.

---

## Phases 1 to 4 — identical to the full runbook

Run [`SEARCH-LATENCY-AWS-RUNBOOK.md`](SEARCH-LATENCY-AWS-RUNBOOK.md) verbatim
for all of these; nothing in them differs:

| Phase | What | Notes for this campaign |
|---|---|---|
| **1** | start the boxes | unchanged |
| **2a–2d** | SSH, private IPs, instance store, clocks, bench checkout, venv | unchanged |
| **2e** | **the vector-store image from `94a23ef2`** | **required** — this campaign depends on it more than the full one does |
| **2f** | prove ports 9042/16080/9200 reachable | unchanged |
| **3** | corpus (`pzstd -d`, verify sha256, 8,967,625 lines) and the query set | unchanged. `--sample-docs 500000`, `--per-class 200`, `--common-pool-size 200`, `--seed 99` |
| **4** | build both binaries `--release --locked`, freeze the tree hash | unchanged |

**Do not skip the image build to save 15 minutes.** It is the one component
that cannot fail loudly.

### The sweep scripts

Write both exactly as the full runbook's Phase 4 does — same
`~/run-search-arm.sh` and `~/run-os-arm.sh`, byte for byte. This campaign
changes nothing inside them; it only passes `LIMIT=100` on the run lines and
puts `k100` in the sweep names.

Defaults that matter, and that no run line here overrides:

| Default | Why it must not move |
|---|---|
| `MAX_DOCS=0` (whole corpus) | every arm indexes all 8,967,625 documents |
| `VS_PORT=16080` | the mock convention `PORT+7000` → 16042 polls a closed port and kills every sweep at its index probe |
| `STATEMENT=prepared` | the binary defaults to `literal`; see above |
| `QUERIES=/mnt/nvme/work/queries-enwiki.json` | the laptop set is simplewiki's |
| `WARMUP=5 DURATION=20` | 20 s of measured latency per cell |

---

## Phase 5 — configuration A: ScyllaDB with the index on disk

### Bring the stack up with the knob

```bash
ssh fts-harness 'set -eu
  cd $BENCH && . tools/fleet_env.sh
  VS_FTS_INDEX_DIR=/var/lib/vector-store/fts make scylla-up
  VS_FTS_INDEX_DIR=/var/lib/vector-store/fts make scylla-wait'
ssh fts-harness 'for p in 9042 16080; do
  timeout 3 bash -c "</dev/tcp/172.31.47.166/$p" && echo "$p open" || echo "$p BLOCKED"
done'
```

The knob reaches the container through compose's `${VAR:+=${VAR}}` form
(`docker/docker-compose.scylla.yml:79`), so an unset variable is dropped rather
than passed empty.

### The gate, and it is blocking

```bash
ssh fts-harness 'docker logs fts-bench-vector-store 2>&1 | grep -E "ingest tuning|tantivy worker"'
# expect  index=disk:/var/lib/vector-store/fts
# index=ram here means the knob was ignored -- STOP, and check the image tag
ssh fts-harness 'cd $BENCH && . tools/fleet_env.sh
  docker image inspect --format "{{index .RepoTags 0}}" \
    $(grep "^VECTOR_STORE_IMAGE=" docker/.env.sut | cut -d= -f2-)'
# expect 1.10.0-45-g94a23ef2-arm64
```

**Do not start the sweeps until that line reads `index=disk:`.** An image that
ignored the knob writes a complete, plausible, wrongly-labelled campaign.

### Build the index, once

```bash
ssh fts-harness 'REPS=1 LADDER=1 WARMUP=0 DURATION=3 LIMIT=100 \
                 OUT_DIR=/mnt/nvme/work/results/build \
                 LAT_DIR=/mnt/nvme/work/latencies-build \
                 ~/run-search-arm.sh build-scylla-disk rare_term \
                   --interface cql --rebuild-index --load-concurrency 64'
```

What it does, in order: count the corpus, drop the keyspace, recreate the schema
**and the index**, wait for the index to reach SERVING at zero documents, fill it
with `build-rate`'s loader at one concurrency, ask the engine to publish, gate on
the count, then answer three seconds of queries to prove the index serves rather
than merely counts.

The index is created before the load, so this is **the CDC tail path**, not the
bootstrap scan. Header fact; it goes in the write-up.

Expect **~18 min** — the fleet measured 8,408 docs/s at full corpus against
12,229 at 1.2M (`../BUILD-RATE-LOOP.md:356`), so this is slower than a capped
build, non-linearly.

```bash
ssh fts-harness 'curl -s http://172.31.47.166:16080/api/v1/indexes/wiki/articles_body_fts/count'
# expect 8967625
printf 'config A scylladb-disk  %s\n' "$(date -u +%FT%TZ)" >> "$R/env/index-builds.txt"
ssh fts-harness 'rm -rf /mnt/nvme/work/results/build /mnt/nvme/work/latencies-build'
```

### Smoke, then delete it

```bash
ssh fts-harness 'REPS=1 LADDER=1,8 DURATION=5 WARMUP=2 LIMIT=100 \
                 OUT_DIR=/mnt/nvme/work/smoke LAT_DIR=/mnt/nvme/work/smoke-lat \
                 ~/run-search-arm.sh smoke-cql rare_term --interface cql --no-index-build'
ssh fts-harness 'REPS=1 LADDER=1,8 DURATION=5 WARMUP=2 LIMIT=100 \
                 OUT_DIR=/mnt/nvme/work/smoke LAT_DIR=/mnt/nvme/work/smoke-lat \
                 ~/run-search-arm.sh smoke-vstore rare_term --interface vector-store --no-index-build'
```

Gate on all of it:

- exit code **0** on both, `errors` **0** on every row.
- `hits_mean` near 100 and `zero_hit_queries` **0**.
- `index_docs=8967625`, `index_built_here=false`, `limit=100`,
  `statement=prepared` in both preambles.
- `interface=cql` and `interface=vector-store` in column 17, one each, and their
  `p50_ms` at `c=1` **differ** — if they agree exactly, one arm is not going
  where its flag says.
- run the Phase 6c slice against the smoke logs once. `probe_windows` exiting 1
  with `duplicate (sweep, concurrency, rep)` means a sweep was given more than
  one class; a crash on the missing index-build lines means `--no-index-build`
  needs handling before an arm depends on it.

```bash
ssh fts-harness 'rm -rf /mnt/nvme/work/smoke /mnt/nvme/work/smoke-lat'
```

### Start the probe

One probe per arm, at 1 Hz, on the SUT — it reads `/sys/fs/cgroup` where it
runs, so `DOCKER_HOST` cannot carry it. **Running it on the harness records the
generator box's idle cgroups and reports them as engine numbers.** Do not pass
`--output`; the wrapper appends it.

```bash
a=d1-cql-disk     # then a=d2-vstore-disk, without restarting the stack
ssh fts-harness "set -eu
  cd \$BENCH && . tools/fleet_env.sh
  mkdir -p /mnt/nvme/work/probe
  tools/sut_probe.sh start /mnt/nvme/work/probe/$a.jsonl \
      --engine scylladb \
      --containers fts-bench-scylla:scylladb \
      --containers fts-bench-vector-store:vector-store \
      --vs-url http://127.0.0.1:16080 --keyspace wiki --vs-index articles_body_fts \
      --interval 1 --duration 0 --label 'search-latency-disk $a' \
      --corpus /mnt/nvme/data/corpus.jsonl"
```

**`--memory-read anon+cache` on every arm of this campaign.** Both engines hold
file-backed indexes here, so what a read touches is page cache; `anon` alone
would report a search engine that uses no memory to search.

And the harness box's own CPU, this campaign's only client-headroom signal —
write and start `~/sample-box-cpu.sh` exactly as the full runbook's Phase 6a
does.

### D1 and D2 — the matrix

**Nothing between these two arms brings the stack down.**

```bash
CLASSES="rare_term common_term phrase bool_and bool_not bool_mixed"

for c in $CLASSES; do
  ssh fts-harness "REPS=3 LIMIT=100 OUT_DIR=/mnt/nvme/work/results/d1-cql-disk \
                   LAT_DIR=/mnt/nvme/work/latencies/d1-cql-disk \
                   ~/run-search-arm.sh d1-cql-disk-k100-ladder-$c $c \
                     --interface cql --no-index-build"
done

for c in $CLASSES; do
  ssh fts-harness "REPS=3 LIMIT=100 OUT_DIR=/mnt/nvme/work/results/d2-vstore-disk \
                   LAT_DIR=/mnt/nvme/work/latencies/d2-vstore-disk \
                   ~/run-search-arm.sh d2-vstore-disk-k100-ladder-$c $c \
                     --interface vector-store --no-index-build"
done
```

Each loop is six sweeps of three reps, 144 cells, **~60 min**. Run the classes
in the order given: it is the order `generate_queries` writes and the class
chart draws, and it puts `phrase` — the class the laptop pass flagged, and the
one most likely to knee early — third rather than last.

Then the optional axis:

```bash
ssh fts-harness 'REPS=3 LADDER=16 LIMIT=100 OUT_DIR=/mnt/nvme/work/results/d1-cql-disk \
                 LAT_DIR=/mnt/nvme/work/latencies/d1-cql-disk \
                 ~/run-search-arm.sh d1-cql-disk-k100-fetch-rare_term rare_term \
                   --interface cql --fetch-documents --no-index-build'

ssh fts-harness '/mnt/nvme/work/target/release/scyllasearch \
                   --corpus /mnt/nvme/data/corpus.jsonl \
                   --queries /mnt/nvme/work/queries-enwiki.json \
                   --concurrency 1 --interface vector-store --fetch-documents \
                   ; echo "exit=$?"' 2>&1 | tail -4
# expect a non-zero exit naming --fetch-documents, and NO connection attempt
```

### What to watch while a sweep runs

The script tails the last eight outcome lines of each rep:

```
  -> 14231 queries in 20.00s = 711.6 q/s, p50 1.9 / p90 3.1 / p99 5.4 ms, 0 errors
```

- **`0 errors`** on every line.
- **`!! every query in <class> matched nothing`** means the query set and the
  index disagree. Stop; do not collect the arm.
- **`p50` roughly flat across the ladder** is the closed loop behaving. There is
  no queue between the workers and the engine, so p50 climbing with concurrency
  from `c=1` is the engine's queue, not the harness's.
- **`q/s` flattening** is the plateau, and the level it flattens at is the
  chart's most interesting point.

### Close the arm out, before the stack comes down

`scylla-down` removes the containers and their startup lines go with them, and
`verify_cpu_usage` reads each container's quota with `docker inspect`. The
numbers and the log that licenses them come home together or the arm is
unlabelled data.

```bash
a=d1-cql-disk; mread=anon+cache        # d2-vstore-disk: same
ssh fts-harness "set -eu
  cd \$BENCH && . tools/fleet_env.sh
  L=/mnt/nvme/work/logs/$a; mkdir -p \$L
  docker logs fts-bench-vector-store > \$L/vector-store.log 2>&1
  docker logs fts-bench-scylla       > \$L/scylla.log 2>&1
  docker logs fts-bench-vector-store 2>&1 | grep -oE 'index=(ram|disk:[^ ]*)' \
      | tail -1 > \$L/index-location.txt
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
`index-location.txt` is the only record of the storage tier anywhere in the
artifacts — no CSV column carries it.

### Pull both arms, then take the stack down

```bash
pull_arm() {              # pull_arm <arm-dir> <engine> <results-root> <latencies-root>
    local a="$1" d="$R/$1/$2" src="$3" lat="$4"
    test -n "$R" && test -d "$d" || { echo "no such arm directory: $d" >&2; return 1; }
    scp    "fts-harness:$src/$a/"*.csv                 "$d/points/"    || return 1
    scp -r "fts-harness:$lat/$a/"*                     "$d/latencies/" || return 1
    scp    "fts-harness:$src/$a/run-windows.tsv"       "$d/logs/"      || return 1
    scp    "fts-harness:$src/$a/"*.stderr.tsv          "$d/logs/"      || return 1
    scp -r "fts-harness:/mnt/nvme/work/logs/$a/"*      "$d/logs/"      || return 1
    scp    "fts-harness:/mnt/nvme/work/probe/$a.jsonl" "$R/sut/cpu-$a.jsonl" || return 1
    scp -r "fts-harness:/mnt/nvme/work/probe/$a/"*     "$d/probe/"     || return 1
    scp    "fts-harness:/tmp/box-cpu.tsv"              "$d/logs/"      || return 1
    local n; n=$(ls "$d"/points/*.csv 2>/dev/null | wc -l)
    [ "$n" -ge 18 ] || { echo "$a: $n point CSVs, expected >=18 (6 classes x 3 reps)" >&2; return 1; }
    local k; k=$(awk -F, '!/^#/ && $1!="concurrency"' "$d"/points/*.csv | wc -l)
    [ "$k" -ge 144 ] || { echo "$a: $k data rows, expected >=144 (6 x 8 x 3)" >&2; return 1; }
    zero_hits "$d/points" || return 1
    rss_breach "$d/logs/resource-by-cell.csv" || return 1
    echo "$a: home"
}

# A cell where every query matched nothing still has a p99, and it plots as the
# cheapest point on the curve. This is that refusal, read off the artifacts.
zero_hits() {
    awk -F, 'FNR==1 { h=0 } /^#/ { next }
        !h { for (i=1;i<=NF;i++) c[$i]=i; h=1; next }
        $(c["zero_hit_queries"]) + 0 > 0 {
            print "ZERO HITS", FILENAME, "c="$(c["concurrency"]), $(c["query_class"]),
                  $(c["zero_hit_queries"])"/"$(c["queries"]); bad=1 }
        END { exit bad }' "$1"/*.csv >&2
}

rss_breach() {
    awk -F, -v OFS=, 'NR==1 { for (i=1;i<=NF;i++) c[$i]=i; next }
        $(c["mem_headroom_bytes"]) != "" && $(c["mem_headroom_bytes"]) <= 0 {
            print "BREACH", $(c["sweep"]), $(c["concurrency"]), $(c["rep"]), \
                  $(c["container"]), $(c["mem_peak_bytes"]); bad=1 }
        END { exit bad }' "$1" >&2
}

pull_arm d1-cql-disk    scylla /mnt/nvme/work/results /mnt/nvme/work/latencies
pull_arm d2-vstore-disk scylla /mnt/nvme/work/results /mnt/nvme/work/latencies
```

**A non-zero return is the arm's gate, not a warning** — the stack that produced
it is still up, which is the only moment re-running a lost rep is cheap. And **a
point CSV existing is not a finished run**: `--out` is opened before the first
query, so the count gate passes while the last rep is still going. Check the
sweep's last stderr log for its final `->` line.

```bash
ssh fts-harness 'cd $BENCH && . tools/fleet_env.sh && make scylla-down'
```

---

## Phase 6 — configuration B: OpenSearch with the index on disk

```bash
ssh fts-harness 'set -eu
  cd $BENCH && . tools/fleet_env.sh
  make os-up && make os-wait'
ssh fts-harness 'timeout 3 bash -c "</dev/tcp/172.31.47.166/9200" && echo "9200 open"'
```

**No memory overrides.** This campaign runs `.env.sut` as shipped — 28g,
`-Xms14g -Xmx14g` — because there is no tmpfs arm to make room for. That is what
keeps container memory at parity with the ScyllaDB half, and it is a fairness
property this runbook has and the full one gives up.

### Build the index, once

```bash
ssh fts-harness 'REPS=1 LADDER=1 WARMUP=0 DURATION=3 LIMIT=100 \
                 OUT_DIR=/mnt/nvme/work/os-results/build \
                 LAT_DIR=/mnt/nvme/work/os-latencies-build \
                 ~/run-os-arm.sh build-os-disk rare_term \
                   --rebuild-index --index-config disk \
                   --refresh-interval 1s \
                   --load-concurrency 16 --load-batch-size 512'
```

**`--index-config disk`, never the default.** `ossearch` defaults to `ramindex`,
whose mapping sets `_source: {"enabled": false}` — an index that stores no
document body, cannot serve the projection sweep, and is doing one of the two
jobs [`../COMPARABILITY.md`](../COMPARABILITY.md) says both engines do.

### The analyzer gate — the campaign's defining fairness check

```bash
ssh fts-harness 'grep -E "m1_parity|analyzer check skipped" \
  /mnt/nvme/work/os-results/build/build-os-disk-rep1.stderr.tsv'
# expect: "m1_parity analyzer matches on one probe"
ssh fts-harness 'curl -s "http://172.31.47.166:9200/wiki-articles/_count"'   # 8967625
ssh fts-harness 'cd $BENCH && OS_URL=http://172.31.47.166:9200 \
                 opensearch/verify_analyzer.sh wiki-articles'   # non-zero on divergence
ssh fts-harness 'docker inspect -f "{{.HostConfig.Memory}}" fts-bench-opensearch'  # 30064771072
printf 'config B opensearch-disk  %s\n' "$(date -u +%FT%TZ)" >> "$R/env/index-builds.txt"
ssh fts-harness 'rm -rf /mnt/nvme/work/os-results/build /mnt/nvme/work/os-latencies-build'
```

Two ways this gate passes without checking anything, both excluded:
**`--no-analyzer-check` is never passed** (it is in the null-sink runbook only
because a mock cannot answer `_analyze`), and **a config declaring no
`m1_parity` analyzer skips the check and returns Ok**
(`build-rate/opensearch/src/reset.rs:318-325`) — so `analyzer check skipped` is
a **fail**, not a note. The laptop pass shipped with parity broken and its
recall figures are void because of it.

### D3 — the matrix

Probe first (`--engine opensearch --memory-read anon+cache`), exactly as the
full runbook's Phase 6d does, then:

```bash
for c in $CLASSES; do
  ssh fts-harness "REPS=3 LIMIT=100 OUT_DIR=/mnt/nvme/work/os-results/d3-os-disk \
                   LAT_DIR=/mnt/nvme/work/os-latencies/d3-os-disk \
                   ~/run-os-arm.sh d3-os-disk-k100-ladder-$c $c --no-index-build"
done

ssh fts-harness 'REPS=3 LADDER=16 LIMIT=100 OUT_DIR=/mnt/nvme/work/os-results/d3-os-disk \
                 LAT_DIR=/mnt/nvme/work/os-latencies/d3-os-disk \
                 ~/run-os-arm.sh d3-os-disk-k100-fetch-rare_term rare_term \
                   --fetch-documents --no-index-build'
```

Close out as configuration A does — `docker logs fts-bench-opensearch`,
`docker image inspect … opensearchproject/opensearch:3.8.0`, the data mount into
`index-location.txt`, `--containers fts-bench-opensearch` (**not optional**:
`verify_cpu_usage`'s built-in list is the ScyllaDB pair and would report nothing
here) — then:

```bash
pull_arm d3-os-disk opensearch /mnt/nvme/work/os-results /mnt/nvme/work/os-latencies
ssh fts-harness 'cd $BENCH && . tools/fleet_env.sh && make os-down'
```

---

## Phase 7 — verify all three arms came home

From `$R` alone, **before the stop**:

```bash
# 1. every arm has its sweeps and its reps
for a in "$R"/d?-*/; do
  echo "$(basename "$a") $(ls "$a"*/points/*.csv 2>/dev/null | wc -l)"
done   # expect 21 18 21 -- 6 classes x 3 reps, plus 3 fetch sweeps on d1 and d3

# 2. the interface column is what the directory claims (cols 16,17)
for a in d1-cql-disk:scylladb/cql d2-vstore-disk:scylladb/vector-store \
         d3-os-disk:opensearch/http; do
  d=${a%%:*}; want=${a##*:}
  got=$(awk -F, '!/^#/ && $1!="concurrency" {print $16"/"$17}' \
        "$R/$d"/*/points/*.csv | sort -u)
  [ "$got" = "$want" ] || echo "MISLABELLED $d: want $want, got $got"
done

# 2b. THE INDEX LOCATION, which is in no column and only in the arm logs
grep -h . "$R"/d1-cql-disk/scylla/logs/index-location.txt \
          "$R"/d2-vstore-disk/scylla/logs/index-location.txt   # index=disk:/var/...
grep -h . "$R"/d3-os-disk/opensearch/logs/index-location.txt   # an NVMe path

# 3. nothing rebuilt an index mid-campaign, and exactly two ingests happened
grep -h "^# index_built_here" "$R"/*/*/points/*.csv | sort -u   # false, only
grep -h "^# index_docs"       "$R"/*/*/points/*.csv | sort -u   # 8967625, only
wc -l < "$R"/env/index-builds.txt                               # 2

# 4. every CQL sweep prepared its statements, and every row is k=100
grep -h "^# statement" "$R"/d1-cql-disk/scylla/points/*.csv | sort -u   # prepared
awk -F, '!/^#/ && $1!="concurrency" {print $14}' "$R"/*/*/points/*.csv \
  | sort -un   # expect exactly 100

# 5. the analyzer gate ran and passed
grep -h "^# analyzer_check" "$R"/d3-os-disk/opensearch/points/*.csv | sort -u  # true
grep -rh "analyzer check skipped" "$R"/d3-os-disk/opensearch/logs/*.stderr.tsv # nothing

# 6. no errors, no blank percentiles, no zero-hit cells
awk -F, '!/^#/ && $1!="concurrency" && $4+0>0 {print FILENAME": errors="$4}' \
  "$R"/*/*/points/*.csv
awk -F, '!/^#/ && $1!="concurrency" && ($7=="" || $9=="") {print FILENAME" c="$1" "$2}' \
  "$R"/*/*/points/*.csv
awk -F, '!/^#/ && $1!="concurrency" && $12+0>0 {print FILENAME" c="$1" "$2": "$12"/"$3}' \
  "$R"/*/*/points/*.csv

# 7. every cell left a distribution, and none is too thin to carry a p99
for d in "$R"/*/*/latencies/*/; do echo "$(ls "$d" | wc -l) $(basename "$d")"; done
awk 'ENDFILE { if (FNR < 100) print FILENAME": "FNR" samples -- below the p99 floor" }' \
  "$R"/*/*/latencies/*/*.csv

# 8. the matrix is complete: every (class, level) pair present 3 times, per arm
for a in d1-cql-disk/scylla d2-vstore-disk/scylla d3-os-disk/opensearch; do
  echo "== $a"
  awk -F, '!/^#/ && $1!="concurrency" && $15=="false" {print $2, $1}' \
      "$R/$a"/points/*.csv | sort | uniq -c | awk '$1!=3 {print "  NOT 3 REPS:", $0}'
done

# 9. resources, and the harness box was not the thing being measured
ls "$R"/*/*/logs/resource-by-cell.csv | wc -l   # 3
for f in "$R"/*/*/logs/resource-by-cell.csv; do rss_breach "$f" || echo "^ $f"; done
head -2 "$R"/d1-cql-disk/scylla/logs/box-cpu.tsv
tail -1 "$R"/d1-cql-disk/scylla/logs/box-cpu.tsv
```

### The client-headroom read, which is a judgement and not a gate

Cut `box-cpu.tsv` to each cell's window using the `[k/n] concurrency=C class=X`
announcement that opens it, and compute `box_cores` — busy over total, times 8.

There is **no measured harness floor for the read path**: the floors in
[`../TUNING.md`](../TUNING.md) are write-path numbers, and the read-path
equivalent is what [`HARNESS-AWS-RUNBOOK.md`](HARNESS-AWS-RUNBOOK.md) exists to
produce once `engine-mock` can answer a search.

| `box_cores` peak | Verdict | What may be said |
|---|---|---|
| ≤ 4 of 8 | `ok` | the plateau is the engine's |
| 4–7 of 8 | `close` | plotted, and **named in the footer** as possibly client-bound |
| ≥ 7 of 8 | `HARNESS` | the cell measured the harness; plotted, ringed, never called an engine throughput |
| no samples | `?` | **not a pass.** An unmeasured gate must never render as a passed gate |

Expect it to matter only at `c=64` and `c=128`, and expect D3 to reach it first.

### Render before the stop

If the charts cannot be produced from `$R` without touching the fleet, **the
pull is not finished**.

```bash
grep -h "^concurrency," "$R"/*/*/points/*.csv | sort -u | wc -l   # 1 header shape
awk -F, '!/^#/ && $1!="concurrency" {print $16"-"$17}' "$R"/*/*/points/*.csv \
  | sort | uniq -c   # three series, 144 matrix rows each (+3 on d1 and d3)
```

**The four charts are six facets each**, one per query class: X is
`concurrency`, Y is `p50_ms` / `p90_ms` / `p99_ms` / `queries_per_s`, a series is
`engine` + `interface`, and `query_class` selects the facet. `rare_term` is the
headline. Do not collapse classes into one line.

---

## Phase 8 — stop the boxes

**Before stopping: every artifact is on the laptop**, because `/mnt/nvme` is
about to be destroyed.

```bash
aws ec2 stop-instances --region eu-north-1 \
  --instance-ids i-08d8d2505e16683f7 i-0e3e4b6b02e654b7f
aws ec2 wait instance-stopped --region eu-north-1 \
  --instance-ids i-08d8d2505e16683f7 i-0e3e4b6b02e654b7f
aws ec2 describe-instances --region eu-north-1 \
  --instance-ids i-08d8d2505e16683f7 i-0e3e4b6b02e654b7f \
  --query 'Reservations[].Instances[].[InstanceId,State.Name,PublicIpAddress]' --output text
```

**Confirm both read `stopped` with no public IP, and say so explicitly.**
"I initiated the stop" is not the same as "they are stopped".

---

## The SUT, for the record

| Service | `cpuset` | `cpus` | `mem_limit` | In-process budget |
|---|---|---|---|---|
| ScyllaDB — `--smp 4 --memory 24G --overprovisioned 0` | `0-3` | 4 | 28g | 24 GiB |
| vector-store, `VS_FTS_INDEX_DIR` set | `4-7` | 4 | 28g | `VECTOR_STORE_MEMORY_LIMIT` 26 GiB; the index is page cache, not anon |
| OpenSearch, `--index-config disk` | `4-7` | 4 | 28g | 14 GiB heap |

**`.env.sut` as shipped, unmodified.** Container memory is at parity across the
engines, which the full campaign gives up to fit a tmpfs index.

The asymmetry that remains and reaches every footer: **D1 answers from ScyllaDB
*and* the vector-store — eight cores and 56 GiB across two containers — while D3
answers on four cores and 28 GiB in one. D2 is the matched arm**, and it is why
D2 exists.

A file-backed index is page cache, which the cgroup reclaims rather than kills,
so the failure mode on both engines is a slower read under reclaim rather than
an OOM. `cache_bytes` against `rss_bytes` is what tells them apart.

**Images.** `scylladb/scylla:2026.3.0-rc2`; vector-store from
`knowack1/vector-store` @ **`94a23ef2`**; `opensearchproject/opensearch:3.8.0`.

---

## Gates

| Gate | Rule |
|---|---|
| **Index location** | **blocking, and the only gate with no CSV column behind it.** The vector-store's startup line reads `index=disk:/var/lib/vector-store/fts`, and OpenSearch's data mount is an NVMe path. Both captured into `logs/index-location.txt` **while the stack is up** — `*-down` takes the evidence with it. An image older than `94a23ef2` produces the mislabel silently |
| **Index identity** | **blocking.** `index_docs=8967625` and `index_built_here=false` in every measured sweep's header, and exactly 2 lines in `env/index-builds.txt`. A `true` means a sweep rebuilt an index and every arm before it is void |
| **Analyzer parity** | **blocking.** `ossearch` must print `m1_parity analyzer matches on one probe`. `analyzer check skipped` is a **fail**. `--no-analyzer-check` is never passed |
| **Top-k** | **blocking.** Column 14 reads `100` on every data row of every arm. `100` is a constant here, not a dimension, and a stray `10` is a sweep that missed its `LIMIT` |
| **Statement mode** | **blocking.** `statement=prepared` in every D1 header. The script default yields to an exported `STATEMENT`, so this header is the only evidence the rule held |
| **Zero-hit cell** | **blocking.** `zero_hit_queries > 0` voids the sweep: the cell answers, has a p99, and plots as the cheapest point on the curve |
| **Errors** | **blocking.** `errors > 0` on any row. A failed request contributes a count and nothing to the distribution |
| **Blank percentile** | **blocking.** A blank `p50_ms` is a cell that measured nothing. The blank is the signal — a zero would plot as the best point |
| **Thin cell** | **annotating.** Under 100 samples cannot support a p99. Expect it only at `c=1` on the expensive classes; name every one in the footer |
| **CPU attribution** | **annotating, never dropping.** Per cell, the engine container's peak `cpu_cores_used` against its quota: `ok` at ≥0.85, `not-CPU` below, `?` where no series covers the window. **A `?` is not a pass** |
| **Client headroom** | **annotating.** The harness box CPU per cell, against the table above. No read-path harness floor has been measured; this is the substitute and the footer says so |
| **Probe source** | every sample reads `source=cgroup-anon`. The `docker stats` fallback has no CPU counter and is not anon-only, which destroys the `cache_bytes` reading both arms depend on |
| **Clock skew** | harness-to-SUT skew under one probe tick (1 s), measured at re-entry and recorded. The window is never padded to cover skew |

**There is no coordinated-omission gate, and that is not an oversight.** This
harness offers nothing on a schedule — N workers, no channel, next query on
answer — so there is no schedule to fall behind and the correction does not
apply. **These numbers may not be plotted beside the laptop pass's open-loop
C5/C7 numbers.**

---

## Traps

1. **`VS_PORT` defaults to 16042 in the sweep script's `PORT+7000` convention.**
   That is the null sink's. The script pins **16080**; a sweep that reaches a
   closed port dies at its index probe on billed fleet time.

2. **A vector-store image older than `94a23ef2` ignores
   `VECTOR_STORE_FTS_INDEX_DIR` in silence.** The whole ScyllaDB half then
   measures a RAM index under a disk label. Gated before the arms, not after.

3. **`ossearch`'s default index config is `ramindex`, not `disk`.** Its mapping
   disables `_source`, so the projection sweep would time an empty transfer.

4. **The analyzer check returns `Ok` when the config declares no `m1_parity`
   analyzer**, with only a stderr note. `analyzer_check=true` in the header means
   the check was *enabled*, not that it *compared* anything.

5. **`pkill -f` from inside an `ssh` one-liner kills the ssh session**, because
   the wrapper's own command line contains the pattern. Put it in a script.

6. **Every CQL sweep prepares 200 statements before the matrix starts.** A sweep
   looks like it hangs for a moment at start. It is not hanging.

7. **`STATEMENT=prepared` is a shell default and an exported `STATEMENT` beats
   it.** Nothing in the sweep output would look wrong; the `# statement` header
   gate is what catches it.

8. **D2's CSV header says `statement=prepared` and it means nothing there.**
   `open_searcher` sends the vector-store arm down `open_bm25`, which never sees
   the flag. Do not explain a D1↔D2 difference with it.

9. **A CQL run with `--fetch-documents` deserialises the documents** — at
   `k=100` that is 100 title+body pairs per query, and the client-side
   deserialisation is on the harness box, inside the latency. Say so wherever
   the projection arm appears.

10. **`probe_windows` looks for index-build lines to compute `build_s`.** Every
    measured sweep carries `--no-index-build`, so there are none. Confirm in the
    smoke that it emits blanks rather than failing.

11. **Ctrl-C ends the cell and the matrix, keeping the cells already measured.**
    A half-walked ladder is a valid CSV with fewer rows; the count gate in
    `pull_arm` is what catches it.

---

## Caveats this campaign carries into the write-up

- **This is not how ScyllaDB FTS ships.** The index is in RAM today;
  `VECTOR_STORE_FTS_INDEX_DIR` is a fork knob exploring a durable index. Every
  chart says "with the index on disk", and the deployment question is answered
  only by the full campaign's RAM arms.
- **D1 answers from a two-container stack with eight cores and 56 GiB; D3 from
  one container with four cores and 28 GiB.** D2 is the matched arm. Disclosed
  on every chart, never netted out.
- **The CQL arm prepared its statements; the OpenSearch arm re-parsed every
  request.** Analyzer parity is verified; **parser parity is not claimed**, and
  the size of the difference is not measured here.
- **The ScyllaDB index was built through the CDC tail path**, because the
  harness creates the index before it loads.
- **Both indexes are page-cache resident after warm-up.** The working set is
  ~37 GB against 61 GiB of box RAM, so "on disk" describes where the bytes live,
  not where a read finds them. `cache_bytes` per arm is the evidence, and this is
  a property of the box rather than of either engine.
- **`k=100` is one point on a curve nobody measured here.** Do not extrapolate to
  `k=10` or `k=1000`.
- **`common_term` spans a wide selectivity range inside one class**, so part of
  its p50→p99 gap is which query ran. **The `phrase` class is corpus-specific** —
  do not carry a phrase-class number across corpora.
- **`rare_term` is the headline facet and the cheapest query in the set**, so its
  `queries_per_s` is the most favourable throughput either engine produces here.
- **The probe shares the SUT's cores with the engine it measures**, at 1 Hz. It
  adds no cell and no wall clock, and it reaches the footer anyway.
- **There is no measured read-path harness floor.** The client-headroom verdict
  is a box-CPU judgement, not a ratio against a number.

---

## Cost

An estimate with its derivation, never a measurement. **Time them and write the
real numbers here.**

| Item | Time |
|---|---|
| Re-entry (SSH, mounts, corpus decompress, venv, harness build) | ~35 min |
| **Vector-store image rebuild from `94a23ef2`** | **~15 min** |
| Query set generation and gate | ~3 min |
| ScyllaDB index build at 8,967,625 documents (~8,408 docs/s) | ~18 min |
| Smoke, both ScyllaDB interfaces | ~5 min |
| D1 matrix (6 classes × 8 levels × 3 reps × 25 s) | ~60 min |
| D2 matrix (same) | ~60 min |
| D1 projection + D2 refusal | ~2 min |
| Close out and pull the ScyllaDB half | ~10 min |
| OpenSearch index build (~16 min) and analyzer gate | ~18 min |
| D3 matrix + projection | ~62 min |
| Close out, pull, verify, render, stop | ~15 min |
| **Total** | **~5.1 h, band 4.5–6.5 h, ~$20–28 at $4.37/h** |

438 measured cells, of which 432 are the matrix. At 25 s a cell that is 3.04 h
of measurement, and it dominates everything else in the table.

**Cost levers, decided before launch, not mid-run.**

| Lever | Saving | What it costs |
|---|---|---|
| Drop the projection axis | −2 min | the application-shaped numbers go; the matrix is unaffected |
| `WARMUP=3 DURATION=12` | −1h 13m → 3.9 h | a cell drops 25 s → 15 s. It costs tail resolution in the `c=1` cells, which are already the thinnest — **check gate 7 before reaching for this** |
| `REPS=2` instead of 3 | −1h 00m → 4.1 h | percentiles do not average; a merged p99 across two repeats is thinner than across three. Do not also drop `--latencies-dir` |
| Drop `bool_not` and `bool_mixed` | −30 min | the class axis loses its two most expensive boolean shapes. Cut from the end of the declared order, never `phrase` |
| **Add** the RAM arms | +2h 35m | this becomes the full campaign's configurations 1 and 3, and answers the deployment question |

**Budget 6 h and run it in one session.** Unlike the full campaign this fits in
one, which is most of the point of it. If it must be split, split at the
`scylla-down` boundary between D2 and D3, reuse the same `RUN_ID`, and record
both windows in `$R/env/sessions.txt`. **Do not split inside a configuration** —
the index is rebuilt on restart, and a rebuilt index is a different index
whatever its document count says.

---

## Where it lands

The four charts [`README.md`](README.md) specifies — X `concurrency`, Y
`p50_ms` / `p90_ms` / `p99_ms` / `queries_per_s`, series `engine` + `interface`
— once per query class, 24 panels, of which the `rare_term` four are the
headline. Plus the `c=16` row read across all six classes, which is the AWS
successor to the laptop pass's C6.

It does **not** produce C5 or C7: C5 is a percentile-axis chart from an
open-loop generator, C7 an offered-rate sweep, and neither axis exists here.

### What goes in `../TUNING.md`

Each with the arm and the run that produced it, and each with its direction of
inequality written down:

- **`p50 / p90 / p99 ms` per interface at `c=1`** — the unloaded service time,
  the one number here that needs no headroom argument.
- **The level at which `queries_per_s` flattens, per interface and per class**,
  with its CPU attribution verdict beside it. Written `≥` where the verdict is
  `not-CPU` or the client headroom read `close`.
- **`cql` − `vector-store` at `c=1` and at the plateau** — ScyllaDB's own read
  overhead, in milliseconds and as a fraction, stated only for cells where both
  arms had `fetch_documents=false`. **With the index on disk**, which is the
  qualifier that makes it quotable.

### The write-up

`$R/README.md`, opening with:

```markdown
# <one line: what was measured>

Run `search-latency-disk-2026-09-16T0900Z`, produced by
`bench/search-latency/SEARCH-LATENCY-DISK-RUNBOOK.md`. Fleet up <HH:MM>–<HH:MM>
UTC on <date>. Arms: d1-cql-disk, d2-vstore-disk, d3-os-disk, N=3 each, over all
8,967,625 enwiki documents at top-k 100. Binaries: crate commit <sha>. Analyzer
parity verified: <probe result>. CQL statements: prepared.

PRELIMINARY. Closed-loop service times, not comparable with the open-loop C5/C7
numbers. BOTH INDEXES ARE ON DISK — this is the matched-storage comparison, and
it is NOT how ScyllaDB FTS ships today, which is from RAM. Container memory is
at parity (28g each); the ScyllaDB arms answer from two containers and eight
cores, the OpenSearch arm from one and four. One top-k only.
```

Mandatory footer clauses: that the latencies are **service times** from a closed
loop and coordinated omission does not apply; the concurrency each number was
taken at; **that both indexes were on disk and that this is not ScyllaDB's
shipping configuration**; the cgroup asymmetry between the ScyllaDB stack and
OpenSearch; that the CQL arm prepared while OpenSearch re-parsed, and that this
is not parser parity; **`limit=100`**, and whether documents were projected;
which query class the panel is, and that `rare_term` is the cheapest in the set;
the client-headroom verdict per plateau and the absence of a measured read-path
harness floor; and **PRELIMINARY**.

Hand the user the absolute path of `$R` and say which arms landed in it.

---

## Recorded results

Nothing yet. **This runbook has never been run.**

| Run | Arms | What it established |
|---|---|---|
| — | — | — |
