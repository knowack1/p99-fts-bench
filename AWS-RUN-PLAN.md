# AWS + enwiki run plan — the measurement that produces quotable numbers

## What this is, and what it is not

The laptop campaign (`results/laptop-simplewiki-2026-08/`) produced all eight
charts from real measurements over the frozen simplewiki corpus. Every one of
those numbers is disqualified by `docker/.env`'s own header: 2 GiB of Scylla
memory, a 12 GiB engine cgroup, and a load generator sharing 22 heterogeneous
cores with the engines it is measuring.

This plan replaces those numbers. It does **not** redesign the measurement —
the harness, the gates, the chart code and the results tree are all built and
verified. What changes is the corpus (simplewiki 0.42 GB → enwiki ~31 GB of
body text), the hardware (one laptop → three dedicated boxes), and the
repetition count.

The design goal is that **the deck does not change shape**. Slides, chart
axes, footers and findings structure are already in place from the laptop pass;
this run swaps the images and the numbers behind them. Where a number moves
enough to contradict a laptop finding, that is a result to report, not a
problem to smooth over.

Provisioning, instance selection and cost live in `HARDWARE.md`. This file is
the runbook.

---

## Phase 0 — Before any instance is launched

Done on the laptop, for $0. The fleet bills $4.37/hour whether or not it is
measuring.

- [ ] **Parallelise `prepare_corpus.py`.** It streams the 65 bz2 shards
      single-threaded; ~236 GB at bz2 speeds is 3-4 h of billed fleet time
      against ~15 min at one shard per core. This is the single largest
      avoidable line in the budget. Test it against simplewiki's shards and
      assert the output is byte-identical to the current pipeline's.
- [ ] **Decide the vector-store sampling window.** Its `count` endpoint
      advances in ~10,000-document steps, so at `--interval 1` the ScyllaDB C1
      line is partly a sampling artifact. Either sample at a multiple of the
      commit interval or state the smoothing window in the chart footer. Decide
      from the laptop series, before the shape matters. Do not pick whichever
      looks flatter.
- [ ] **Raise `C1_MAX_SECONDS`.** Default 600. The enwiki ScyllaDB bootstrap
      scan is estimated at ~2.5 h, so the wall-clock cap would truncate it and
      `assert_series_complete` would abort the repetition — after two and a
      half hours of billed measurement. Set per config, generously.
- [ ] **Re-check `C1_IDLE_TIMEOUT` against enwiki commit granularity.** At 30 s
      it is already exactly the refresh=30s interval. If enwiki commits are
      coarser than 30 s, a healthy build reads as a stall.
- [ ] **Raise `C7_RATE_MAX`.** The ladder tops out at 3200 qps, which the
      laptop generator could not reach against OpenSearch anyway. On a
      dedicated generator box the knee may sit above the ladder's ceiling, and
      a sweep that never finds a knee produces no C7.
- [ ] **Set N and write it down.** The laptop pass ran N=3 to verify the tools.
      `HARDWARE.md` prices the ingest phase at N=5. At ~2.5 h per ScyllaDB
      bootstrap repetition, N=5 for that config alone is ~12.5 h of fleet time
      (~$55). Recommendation: **N=5 for the three ingest configs, N=3 for the
      query charts** (C5/C6/C7 run against a resident index and their spread
      was tight on the laptop). Record the choice and the reasoning; an N that
      differs per chart must be stated on each chart.
- [ ] **Sanity-check the campaign script's laptop assumptions.** `GEN_CPUSET`
      and `ENGINE_CPUSET` encode this laptop's heterogeneous-core topology and
      must be re-derived per box — or dropped entirely, since on a 3-box fleet
      the generator is already isolated by being on another machine.

---

## Phase 1 — Fleet bring-up and corpus staging

Serialized, one session. Target: 6-9 fleet hours (`HARDWARE.md`).

1. Launch the three boxes single-AZ. Box 1 = ScyllaDB + vector-store, box 2 =
   OpenSearch, box 3 = generator. Identical instance types on boxes 1 and 2 —
   this is the hardware-parity fairness commitment and it is not negotiable.
2. Install Docker + compose v2, `git`, `python3` + venv on each. Clone the
   repo, `pip install -r requirements.txt`.
3. **Download and prepare the corpus once, on box 3**, then push the prepared
   `corpus.jsonl` to S3 in-region:
   ```
   make download WIKI=enwiki DUMP_DATE=<date>
   make corpus   WIKI=enwiki DUMP_DATE=<date>
   make queries
   aws s3 cp data/corpus.jsonl s3://<bucket>/enwiki-<date>/corpus.jsonl
   ```
   `im4gn` NVMe is instance store: stopping a box destroys the corpus. S3 is
   $1.73/month and in-region transfer is free, so re-staging on start is the
   difference between safe stop/start and re-running a 4-hour prepare.
4. **Freeze the corpus.** Extend `FREEZE.md` with the enwiki document count,
   body-text bytes and a checksum, exactly as simplewiki is recorded. Every
   chart footer cites it.
5. **Validate the index-size ratio** before committing to the ingest runs.
   `SIZING.md` estimates Tantivy at 0.42x body text — ~13 GB for enwiki. Load
   1% and measure the actual ratio. If it extrapolates above ~20 GB, the
   `im4gn.8xlarge` branch in `HARDWARE.md` is live, and it is much cheaper to
   learn that now than mid-campaign. **Both engine boxes move together or
   parity breaks.**
6. Run the smoke pass end to end on the real fleet:
   ```
   tools/campaign_laptop.sh --smoke
   ```
   20k docs, rep 99, ~10 min. This is the Phase-2 gate from the laptop plan and
   it exists to catch plumbing — a wrong host, a missing image, a port — before
   hours are spent. Note the script name; renaming it is cosmetic work that can
   wait.

**Do not proceed to Phase 2 until the smoke pass is green on the fleet.**

---

## Phase 2 — Ingest campaign (C1, C2, C3, C4)

One ingest run yields four charts: `build_monitor` (C1/C2), the loader's
latency log (C3) and `resource_probe` (C4) all capture the same run.

```
tools/campaign_laptop.sh --configs opensearch opensearch-refresh30 \
                                   scylla-bootstrap scylla-cdc --reps 5
```

Repetition-major ordering, one engine stack at a time. Every repetition is
cold: container recreated *and* volume dropped.

### Gates that must pass, per repetition

Every outcome is recorded into the repetition's manifest by `ftsbench.gate_log`,
pass or fail, and a failure aborts the repetition rather than averaging it in.
A broken run usually looks *faster* than a good one, which is the whole reason
these are assertions and not log lines.

| Gate | What it catches |
|---|---|
| `opensearch_doc_count` | a truncated load reported as a fast one |
| `scylla_index_complete` | `count == N` **and** `SERVING` — the documented vector-store failure mode is silently skipping documents at its memory limit while still answering queries |
| `c1_series_complete` | a series that ends before every document is *searchable*, not merely written |
| `*_not_oom_killed` (opensearch, scylla, vector-store) | a cgroup kill mid-run |
| `coordinated_omission_open_loop` | a closed-loop generator reporting service times as latencies — precondition for C5 and C7 |

The searchable half of `c1_series_complete` is the one that bit hardest on the
laptop: stopping when the last document was written ended the series one
refresh short, losing 22,554 of 270,269 documents at refresh=1s and **all** of
them at refresh=30s, which made C2 unmeasurable. `build_monitor`'s settle phase
fixes it and `--settle-timeout` bounds the wait. Expect the settle phase to
matter *more* at enwiki scale, and watch for the settle-shortfall warning.

### Expected walls, scaled linearly by body-text bytes (73x)

| Config | simplewiki, measured | enwiki, linear estimate |
|---|---|---|
| OpenSearch | 30.2 s | ~37 min |
| ScyllaDB bootstrap scan | 120.3 s | ~2.5 h (+ base-table load) |
| ScyllaDB CDC tail | 52.2 s | ~1.1 h |

Linear is the central estimate, not a floor: OpenSearch merge cost grows worse
than linearly with corpus size, while the laptop numbers were taken with 9 GB
of swap in use, which cuts the other way.

### Two things to watch specifically

- **CDC reproducibility.** The laptop CDC path showed a 2.4x rep-to-rep spread
  with two truncated loads. Either it becomes reproducible on real hardware, or
  C1/C3 carry the CDC line with an explicit variance band. Not glossed.
- **The C1 sawtooth premise.** `chart-mockups.md` claims OpenSearch shows a
  merge sawtooth while ScyllaDB holds steady. On the laptop that **did not
  appear** — merges did not depress throughput, and the run was loader-bound
  rather than engine-bound. enwiki at 73x with a dedicated generator is the
  real test. If it still does not appear, the narrative changes to match the
  measurement.

---

## Phase 3 — Query campaign (C5, C6, C7) and freshness (C8)

Runs against a resident, fully built index — do not tear down after Phase 2.

1. **Calibrate the generator on box 3, per engine.** On the laptop the
   generator's own ceiling was engine-specific and asymmetric by 4x —
   ~1,090 qps against OpenSearch against ~4,368 qps against ScyllaDB — so the
   OpenSearch sweep was truncated by the client far earlier than the ScyllaDB
   one. Re-measure on the dedicated box:
   ```
   make calibrate-os
   make calibrate-scylla
   ```
   **If either calibrated ceiling is below `C7_RATE_MAX`, C7 is measuring the
   generator, not the engine.** Record both ceilings in `TUNING.md` and state
   them in C7's footer regardless of the outcome.
2. **C6** across all six query classes, then merge per repetition:
   ```
   make c6-os      OS_CONFIG=<config> REP=<n>
   make c6-scylla  SCYLLA_CONFIG=<config> REP=<n>
   ```
3. **C5** on the headline class (`rare_term`) at high sample count — the
   p99.9/p99.99 tail needs the samples.
4. **C7** rate ladder with warmup per point, ceiling raised per Phase 0.
5. **C8** freshness for OpenSearch (both refresh settings) and ScyllaDB CDC.
   This is where the refresh-interval story lives: the laptop measured 0.572 s
   against 27.566 s. Note that the loader forces a `_refresh` on completion, so
   C2 measures build + one forced refresh while C8 owns the interval effect —
   keep those two claims distinct in the write-up.

---

## Phase 4 — Results, and filling the deck

```
make results RUN_NAME=aws-enwiki-<yyyy-mm>
```

`render_results` writes the full tree: eight chart directories, each with its
README (what the chart claims / how it was measured, exact commands /
configuration and tuning / gates that passed / the numbers / confidence tier
and caveats), the PNG, the sidecar JSON, and the raw series and manifests
alongside so any number traces to the record that produced it.

Then, against the laptop tree side by side:

- [ ] **Write the delta.** Per chart: which direction the number moved, by how
      much, and whether the *shape* changed. A number that moved as predicted
      is a validated model; a shape that changed is a finding.
- [ ] **Replace the placeholders in `p99-conf-fts-talk.md`** with the enwiki
      charts, and **remove the PRELIMINARY / not-quotable stamps** the laptop
      pass required. That stamp removal is the deliverable of this whole run.
- [ ] **Update `SLIDES.md`** images and speaker notes.
- [ ] **Mark the laptop tree superseded**, not deleted. It is the record that
      the harness worked before the money was spent, and the comparison is
      itself evidence about the scaling model.
- [ ] **Keep every disclosure.** The work-asymmetry note (ScyllaDB does a
      durable base-table write plus a CDC hop that OpenSearch does not),
      analyzer parity, published tuning, N per chart, and the two-cluster
      architecture framing all survive the hardware change unchanged.
- [ ] **Disclose the analyzer-parity cost.** Matching Tantivy's tokenizer
      requires a `pattern` tokenizer rather than OpenSearch's native
      `standard`, and that costs OpenSearch ~11% indexing throughput on a
      3,000-document bulk. Any build-rate chart (C1, C4, C6) must say the
      OpenSearch side is running a parity analyzer, not its fastest one —
      otherwise the chart quietly credits ScyllaDB with a fairness decision.
- [ ] **Run `opensearch/verify_analyzer.sh` after every `os-index`** and keep
      its output in the run artifacts. It exits non-zero on divergence; the
      laptop pass shipped with parity broken because the old check only
      eyeballed cases where both tokenizers agree.

---

## Cost control during the run

- **Stop the boxes between sessions.** Corpus lives in S3; standing cost is the
  three EBS root volumes at ~$24/month.
- **On-demand, not spot,** for measurement. A reclaim mid-run voids a 2.5-hour
  bootstrap repetition *and* the instance-store corpus to save ~$2.
- **One engine at a time** is deliberate. It leaves an engine box idle and
  billed for ~$38 total across the schedule; a generator splitting cores across
  two targets reintroduces exactly the contention box 3 exists to remove.
- Budget: **~$300-500** all in, per `HARDWARE.md`. The overrun risk is not the
  instance rate, it is fleet hours spent on anything other than measuring.
