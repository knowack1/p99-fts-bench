# Full-deck laptop run — plan

## Goal

Produce **all eight charts C1–C8** from real measurements on this laptop over
the frozen simplewiki corpus, saved in a reviewable results tree, and fill the
presentation with them — so the whole talk can be built, reviewed and rehearsed
before any AWS money is spent. The AWS/enwiki run then *replaces numbers in an
already-finished deck* instead of being the thing the deck waits for.

Explicit non-goal: quotable numbers. Every artifact this plan produces is
disqualified from being quoted by `docker/.env`'s own header. The deliverable is
**real shapes, real methodology, real gaps found** — and a hand-off list of what
changes on real hardware.

## The honest constraint: what a laptop can and cannot support

Host: Intel Core Ultra 7 155H, 22 logical CPUs, 30 GB RAM (~11 GB available,
**7 GB of swap already in use**), three ScyllaDB devcontainers running.

Two properties of this machine matter more than its speed:

1. **Heterogeneous cores.** Max frequency by CPU: 4.8 GHz on 1–4, 4.5 GHz on
   0 and 5–11, 3.8 GHz on 12–19, **2.5 GHz on 20–21**. A thread migrated from
   cpu1 to cpu20 by the scheduler shows a **1.92x** latency step
   that has nothing to do with the engine. Untreated, this *is* the p99 on
   every latency chart.
2. **The load generator shares the machine with the engine.** This is exactly
   the defect that justified a third AWS box. On one laptop it cannot be
   removed, only measured and bounded.

So the charts are not equally trustworthy, and the results tree must say so
per chart:

| Tier | Charts | Status |
|---|---|---|
| **1 — ordering likely survives AWS** | C2 time-to-searchable, C4 resource usage, C8 freshness, C6 at p50 | Structural or design-governed. Ratios move; the ordering probably does not. |
| **2 — shape informative, absolutes will move a lot** | C1 build throughput, C6 at p95 | CPU- and loader-bound in ways that change entirely on 16 dedicated vCPU. |
| **3 — produce for layout, do not believe the numbers** | **C3 write p99/p999**, C5 p99.9/p99.99, C7 QPS knee | Tail measurements on a swapping laptop with a co-resident generator. |

**C3 is the talk's most important chart and the one this machine can least
support.** That is not a reason to skip it — a real C3 with real axes, real
merge markers and a stamped caveat is what lets the slide be written now. It is
a reason to never let a laptop C3 number reach a slide unstamped.

Mitigations applied throughout, not as an afterthought:

- `taskset` pins engine containers and the generator to **disjoint sets of
  high-frequency cores**: engine on `0-11` (six whole P-cores,
  4.5–4.8 GHz), generator on `12-19` (eight E-cores, uniform 3.8 GHz, no SMT,
  so the generator's own dispatch cannot inject frequency steps into
  `t_start_s`), and the 2.5 GHz pair cpu20/21 left to the OS and the Docker
  daemon — out of both measured paths. Recorded in every artifact as
  `env.cpu_affinity`, so an unpinned run is visible in the data rather than
  having to be remembered.
- The generator **self-calibrates**: before each sweep it measures its own
  ceiling, and only offered rates below a stated fraction of it are reported.
  A C7 knee at or above that fraction is labelled *generator-bound*, not
  engine-bound.
- Every latency record carries both `latency_ms` (intended-start to response,
  coordinated-omission-safe) and `service_ms` (actual-start to response). The
  gap between them is reported, which turns coordinated omission from a claim
  into a measured quantity.
- Swap state, load average and the presence of the devcontainers are recorded
  in each run's environment manifest.

**Prerequisite the user should run before the measurement campaign** (not done
unilaterally — these are live dev environments):

```bash
docker stop crazy_goldwasser optimistic_rhodes sharp_perlman
```

If they stay up, the campaign still runs; the caveat is recorded and the Tier-3
charts get worse.

## Phase 0 — Output schema contract

Written **first and by one hand**, because it is what lets four workstreams
proceed in parallel without integrating at the end. Goes in
`bench/SCHEMAS.md`. All outputs stay JSONL with a leading `{"record":"header"}`,
matching the existing `build_monitor` convention, and every header gains
`git_commit`, `host`, `swap_used_bytes`, `load_avg`, `cpu_affinity`.

| Record | Chart | Key fields |
|---|---|---|
| `sample` (exists) | C1, C2 | `t_elapsed_s`, `docs_indexed`, `docs_per_s`, `merges_current`, `index_status` |
| `latency_op` (new) | C3, C5, C6 | `t_intended_s`, `t_start_s`, `t_end_s`, `latency_ms`, `service_ms`, `op`, `n_docs`, `class`, `ok`, `error` |
| `resource_sample` (new) | C4 | `t_elapsed_s`, `container`, `rss_bytes`, `cpu_seconds_total`, `index_size_bytes`, `disk_write_bytes` |
| `sweep_point` (new) | C7 | `offered_qps`, `achieved_qps`, `duration_s`, `p50/p95/p99/p999_ms`, `errors`, `generator_saturated` |
| `freshness_probe` (new) | C8 | `marker`, `t_write_s`, `t_searchable_s`, `lag_s`, `engine`, `refresh_interval` |

## Phase 1 — Build the missing harness (four parallel workstreams)

Five of eight charts have no harness. These four workstreams touch disjoint
files and are handed to parallel agents against the Phase 0 contract.

### W1 — write path (unblocks C1 honestly, C3 at all)

- `ftsbench/latency_log.py` — shared `latency_op` writer plus a
  coordinated-omission-safe pacer (fixed intended-start schedule, never
  "sleep until the last one finished").
- `opensearch_load.py` — **concurrent `_bulk`** with `--concurrency`. Today it
  is one serial request at a time and the measured 8,962 docs/s is the client,
  not the engine: throughput rose 27% from batch size alone and the `write`
  pool never queued a request. This is the blocking defect for C1/C2/C3.
- Both loaders — `--target-rate` for sub-saturation ingest, and per-op latency
  recording. C3 requires a *controlled offered rate*; at saturation the
  latencies are queueing delay.
- Loader tuning published to `bench/TUNING.md` as it is chosen, per §7.

### W2 — read path (C5, C6, C7)

- `ftsbench/load_gen.py` — open-loop generator: worker pool, fixed rate
  schedule, `--rate`, `--duration`, `--calibrate`, records `latency_op`.
- `stats.py` — add p90, p99.9, p99.99 (C5's axis needs them; today it stops
  at p99).
- `ftsbench/sweep.py` — C7 driver: rate ladder, warmup per point, emits
  `sweep_point`, marks `generator_saturated`.
- **Two bugs to fix on the way:** `engines.ScyllaEngine` ignores the port and
  hardcodes 9042 (the stack is on 19042), and `query_bench` has no `--port`
  argument while the `bench-scylla` target passes one — `make bench-scylla`
  currently fails outright.

### W3 — probes (C4, C8)

- `ftsbench/resource_probe.py` — samples container RSS/CPU/disk from the
  Docker API or cgroup files, plus index size on disk per engine. Must attribute
  ScyllaDB + vector-store **separately and as a sum**, since the honest C4
  story is that the in-RAM Tantivy index makes that bar bigger.
- `ftsbench/freshness_probe.py` — writes a marker-token document, polls until
  it is returned by a search, records `lag_s`. Runs for OpenSearch at
  `refresh_interval` 1s and 30s, and for the ScyllaDB CDC tail.

### W4 — charts and the results tree

- `plot_c2` … `plot_c8` following `plot_c1`'s established conventions (median
  run not mean, per-rep spread in a sidecar JSON, footer naming engine
  versions / cache state / N / corpus).
- `ftsbench/results_tree.py` — assembles the results directory and generates
  each per-chart README from the run manifests, so the write-up is built from
  records rather than memory.

Depends only on the Phase 0 contract, so it runs concurrently with W1–W3.

### Phase 1 gates

Unit tests on the pure logic — pacer schedule, percentile interpolation,
coordinated-omission accounting, sweep ladder generation. These are exactly the
functions where a silent error produces a plausible wrong chart. Then a
`python-reviewer` + `code-reviewer` pass on the combined diff.

## Phase 2 — Verify the harness before trusting it

- Every new module runs end to end against a live stack at `MAX_DOCS=20000`.
- **Generator self-calibration recorded**: the ceiling number goes in
  `TUNING.md`. Every C5/C7 point above 50% of it is flagged.
- **Coordinated-omission check**: at a rate deliberately above capacity,
  `latency_ms` must diverge sharply from `service_ms`. If it does not, the
  pacer is wrong and C5/C7 are worthless.
- **Parity gates from `feature-mapping.md`** still open: Tantivy default
  operator, BM25 k1/b. Do these before any recall-sensitive number.
- Existing per-rep gates keep applying: doc counts exact, `status == SERVING`,
  no OOM-kill, no vector-store allocation errors.

## Phase 3 — Measurement campaign (serialized by necessity)

Only one engine stack fits in memory, so nothing runs concurrently. Four
configurations:

| Config | What it is |
|---|---|
| A | OpenSearch, `refresh_interval=1s` (primary) |
| B | ScyllaDB, bootstrap scan |
| C | ScyllaDB, CDC tail |
| D | OpenSearch, `refresh_interval=30s` (ingest-tuned variant) |

**One ingest run yields four charts.** `build_monitor` (C1/C2), the loader's
latency log (C3) and `resource_probe` (C4) all capture the same run. That is
both cheaper and more comparable than three separate runs.

- **3A — ingest, N=5 per config** (up from N=3). Cold every rep: container
  recreated *and volume dropped*.
- **3B — query, index resident.** C6 across all six classes; C5 on the headline
  class at high sample count for the p99.9/p99.99 tail; C7 rate ladder with
  warmup per point.
- **3C — freshness.** C8 for A, C and D.
- **3D — C3 supplement.** simplewiki's whole OpenSearch build is ~30 s and
  produced **one** merge. That is too thin a window for a latency-over-time
  chart. A 4x corpus with distinct synthetic ids (~1.08M docs) gives a
  multi-minute window and several merges. Labelled
  `corpus=simplewiki-x4-synthetic-ids` everywhere it appears; it does **not**
  feed C1/C2/C4, which stay on the frozen corpus.
- **3E — CDC reproducibility.** The CDC path showed a 2.4x rep-to-rep spread
  and two truncated loads. Either it becomes reproducible at N=5 or C1/C3 carry
  the CDC line with an explicit variance band. Not glossed.

## Phase 4 — Results tree

```
bench/results/laptop-simplewiki-2026-08/
├── README.md            # what this run is, host, corpus, confidence tiers, chart index
├── ENVIRONMENT.md       # host facts, image pins, engine versions, published tuning, caveats
├── FINDINGS.md          # cross-chart narrative + what changes on enwiki/AWS
├── c1-build-throughput/{README.md, c1.png, c1.json, raw/}
├── c2-time-to-searchable/…
├── c3-write-tail-latency/…
├── c4-resource-usage/…
├── c5-headline-percentiles/…
├── c6-query-matrix/…
├── c7-qps-sweep/…
└── c8-freshness/…
```

Every per-chart README uses one template: **what the chart claims / how it was
measured (exact commands) / configuration and tuning / gates that passed /
the numbers / confidence tier and caveats / what will change on enwiki+AWS.**
Raw series and manifests ship next to each chart so any number can be traced
to the record that produced it.

## Phase 5 — Fill the presentation

- `p99-conf-fts-talk.md`: replace each chart placeholder with the real chart,
  its headline number and a one-line finding. Every one stamped
  **PRELIMINARY — laptop, simplewiki, not quotable**, and the status line at
  the top updated to say what now exists.
- `SLIDES.md` (new): slide-by-slide deck skeleton following the existing S1–S16
  outline, with the real chart images placed and speaker notes carrying the
  disclosure each chart needs.
- `chart-mockups.md`: left in place, marked superseded per chart. It is the
  invented-numbers preview and deleting it would lose the visual intent.
- Where a measured shape **contradicts** the mock-up, the narrative changes to
  match the measurement. The C1 sawtooth premise is already unconfirmed at this
  scale; a slide that still claims it would be the exact failure
  `COMPARABILITY.md` exists to prevent.

## Phase 6 — AWS hand-off

Written up as its own runbook: **`AWS-RUN-PLAN.md`**. It carries the phase
order, the per-repetition gate list, the laptop work that must be finished
before an instance is launched (parallel `prepare_corpus`, the raised
`C1_MAX_SECONDS` and `C7_RATE_MAX`, the N decision), and the cost controls.
`HARDWARE.md` keeps provisioning and pricing.

The point of the laptop pass is that the AWS run becomes a number-replacement
exercise on a finished deck. The delta write-up — which numbers moved, in which
direction, and where a *shape* changed rather than a value — is Phase 4 of that
runbook.

## Estimated effort

| Phase | Effort |
|---|---|
| 0 — schema contract | ~1 h |
| 1 — harness, 4 parallel workstreams | 3–5 h wall |
| 2 — verification | ~2 h |
| 3 — measurement campaign | 4–5 h |
| 4 — results tree | ~2 h |
| 5 — presentation | ~2 h |
| **Total** | **~15–18 h**, several sessions, $0 |
