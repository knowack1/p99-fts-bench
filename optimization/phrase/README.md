# Phrase-query optimization case study

Working log for a P99 CONF optimization story: ScyllaDB FTS (vector-store /
Tantivy) phrase queries were 3-6x slower than OpenSearch at small `LIMIT`,
while the *same engine* was 2-3x **faster** than OpenSearch on single-term
queries. This file records the measurements, the diagnosis and each
optimization step in the order they actually happened.

**Status:** done. Phrase p99 **13.83 ms → 3.61 ms (3.8x)**, median of N=3,
taking it from 2.8x slower than OpenSearch to **1.4x faster**. Results
verified unchanged against a measured noise floor; recall unchanged. A second
optimization (v2, columnar `primary_id`) was built, measured, found to change
nothing, and reverted. `common_term` was investigated with the same method and
needs no engine work — it already wins, and 56% of its latency is transport.

**Shipped:** v1 only — a phrase-pruning patch to a vendored Tantivy 0.26.1.

## Environment (all measurements)

| | |
|---|---|
| Host | this laptop, 22 cores, 30 GiB — `docker/.env` laptop-simulation sizing |
| Corpus | frozen simplewiki, 270,269 docs, `data/corpus.jsonl` |
| Queries | `data/queries.json`, first 40 of each class (`measurements/queries-3class-simplewiki.json`) |
| OpenSearch | 3.8.0, 1 shard, 0 replicas, `refresh_interval=1s` |
| vector-store | `1.10.0-45-g94a23ef2-amd64` = source `94a23ef2` (`p99-fts-ingest-optimization`), Tantivy 0.26.1 |
| vector-store tuning | `commit_interval=3s commit_threshold=10000 add_lock=exclusive dispatch=worker-pool index=ram`, 6 writer threads / 15 MB, 4 merge threads |
| Method | `ftsbench.query_bench`, closed-loop single thread, warmup 3, 20 iterations/query, `LIMIT 10` |

Analyzer parity gate passed (`opensearch/verify_analyzer.sh`): both engines
tokenize the probe set identically, positions included.

These are laptop-simulation numbers. They are valid for *relative* engine
comparison under identical conditions, which is all this case study claims.
They are not quotable as absolute performance figures — see `docker/.env`.

## v0 — Baseline

### Per class, three arms

| class | OpenSearch p50 / p99 | vector-store direct p50 / p99 | ScyllaDB CQL p50 / p99 |
|---|---|---|---|
| `phrase` | 3.21 / **7.21** ms | 2.85 / **14.42** ms | 2.84 / **14.58** ms |
| `common_term` | 1.81 / 3.32 ms | 0.73 / 1.41 ms | 0.63 / 1.16 ms |
| `rare_term` | 1.51 / 2.77 ms | 0.61 / 1.36 ms | 0.50 / 1.03 ms |

> **These OpenSearch numbers are cold and were later superseded.** This run
> was the first query traffic against a freshly loaded index, and
> `query_bench`'s 3 warmup queries per query are not enough to JIT-warm a JVM
> search path. Re-measured warm (N=3, same index, same 4 segments) OpenSearch
> is considerably faster — phrase p50 1.93 / p99 4.99 — and every conclusion
> below uses the warm figures. The shape of the finding does not change; the
> size of the gap does. See [Final numbers](#final-numbers-n3).

Two facts that frame the whole story:

1. **The vector-store is not slow in general.** On `common_term` and
   `rare_term` it beats OpenSearch by 2-3x at p50 and p99.
2. **`phrase` inverts only at the tail.** p50 is a tie (2.85 vs 3.21); p99 is
   2x worse (14.42 vs 7.21). p50 → p95 spreads 5x on the vector-store against
   2.2x on OpenSearch, so the tail is a *subset of queries*, not jitter.

**The CQL arm equals the direct arm** (14.58 vs 14.42 p99). The ScyllaDB
coordinator, the CQL round trip and the SSTable projection add nothing
measurable to a phrase query: the entire cost is inside Tantivy. Everything
below therefore works on the vector-store alone.

### Which phrase queries are slow

Per-query p50, vector-store vs OpenSearch, sorted slowest first (full data in
`measurements/v0-baseline-*.json`):

| query | VS p50 | OS p50 | ratio | matching docs |
|---|---|---|---|---|
| `"can help"` | 13.79 | 5.63 | 2.4x | 184,352 |
| `"you can"` | 13.40 | 6.95 | 1.9x | 185,015 |
| `"article about"` | 12.30 | 3.31 | **3.7x** | 176,399 |
| `"help wikipedia"` | 12.15 | 4.22 | 2.9x | 183,985 |
| `"made longer"` | 11.65 | 4.38 | 2.7x | 183,937 |
| `"short article"` | 10.92 | 4.52 | 2.4x | 183,915 |
| `"archived from"` | 8.45 | 3.46 | 2.4x | 68,413 |
| ... | | | | |
| `"new york"` | 2.62 | 2.92 | 0.9x | 23,746 |
| `"census bureau"` | 1.51 | 2.53 | 0.6x | 10,542 |
| `"permanent dead"` | 1.47 | 2.11 | 0.7x | 12,959 |

The distribution is bimodal and the split is not subtle: every slow query is a
fragment of Wikipedia's stub boilerplate ("This **short article** **can** be
**made longer**. **You can** **help Wikipedia** by adding to it."), which
occurs in ~184,000 of the 270,269 documents — 68% of the corpus. On the fast
half, where matches are 10-30k, **the vector-store is already faster than
OpenSearch**.

Vector-store latency tracks match count almost exactly linearly
(~0.065 µs/match); OpenSearch latency at `LIMIT 10` is nearly flat across a
10k-185k match range. That asymmetry is the whole finding.

### The measurement that cracked it: sweep K

Same query (`"you can"`, 185,015 matches), vary only `LIMIT`:

| `LIMIT` | OpenSearch | vector-store | ratio |
|---|---|---|---|
| 1 | 1.92 ms | 11.95 ms | 6.2x slower |
| 10 | 3.64 ms | 12.44 ms | 3.4x slower |
| 100 | 7.49 ms | 12.49 ms | 1.7x slower |
| 1000 | 31.10 ms | 15.20 ms | **0.5x — 2x faster** |

- OpenSearch scales **16x** across the sweep. Latency rising with K is the
  signature of top-K pruning: at small K, Lucene's block-max WAND skips most
  of the posting list; at large K it cannot skip and pays full freight.
- The vector-store is **flat** (1.27x across a 1000x change in K). Flat in K
  means *no pruning at all* — the same work is done whatever is asked for.
- At K=1000, where neither engine can prune, the vector-store is **2x
  faster**. Tantivy's raw phrase matching and scoring throughput is not the
  problem. The missing shortcut is.

## Diagnosis — confirmed in source, not inferred

Tantivy 0.26.1 implements the pruning hook `Weight::for_each_pruning` in
exactly three places:

```
src/query/weight.rs                      <- the default, non-pruning fallback
src/query/term_query/term_weight.rs      <- real block-max WAND
src/query/boolean_query/boolean_weight.rs <- real block-max WAND
```

`src/query/phrase_query/phrase_weight.rs` is **not** among them, so
`PhraseWeight` inherits the default:

```rust
// tantivy-0.26.1/src/query/weight.rs:47
pub(crate) fn for_each_pruning_scorer<TScorer: Scorer + ?Sized>(
    scorer: &mut TScorer, mut threshold: Score,
    callback: &mut dyn FnMut(DocId, Score) -> Score,
) {
    let mut doc = scorer.doc();
    while doc != TERMINATED {
        let score = scorer.score();        // full scoring of EVERY match
        if score > threshold {
            threshold = callback(doc, score);
        }
        doc = scorer.advance();            // walks the ENTIRE intersection
    }
}
```

The `threshold` gates only whether the callback runs. It never skips the
`advance()` — which for a phrase scorer is where the position-list
intersection happens — and never skips `score()`. Cost is therefore
`O(total matches)`, independent of K. That is precisely the measured shape.

This also explains why `common_term` is *fast*: a single-term query goes
through `term_weight.rs`, which does have block-max WAND. The vector-store
already wins the queries that can prune, and only loses the ones that cannot.

### Why the recall symptom shares this root cause

The same query family — high-frequency stub boilerplate — is what produces
the poor cross-engine phrase agreement recorded in
`results/recall-check-2026-09-19/`: ~184k near-identical boilerplate documents
produce massive BM25 score ties, and which 10 of them survive `LIMIT 10` is
decided by each engine's tie-break order. One corpus property (a large block
of near-duplicate templated text) drives both symptoms.

## v1 — give `PhraseWeight` a pruning path

`PhraseScorer` already supports `seek()`, and `PhraseWeight` already holds a
`Bm25Weight` (which exposes `max_score()`), so the machinery exists. Two
layers, cheapest first:

1. **Global early termination.** If `threshold >= similarity_weight.max_score()`
   no remaining document can enter the heap — stop the walk.
2. **Per-document cheap upper bound, skipping the position work.** A phrase
   occurrence requires an occurrence of every term, so
   `phrase_freq(doc) <= min_i term_freq_i(doc)`. Term frequencies are
   available from the postings *without decoding positions*, and BM25 is
   monotonically increasing in frequency, so
   `bm25(fieldnorm(doc), min_i term_freq_i(doc))` is a valid upper bound on
   the phrase score. When that bound is below the current heap threshold,
   skip the position intersection entirely — which is the dominant cost.

Both are exact: they can only skip documents that provably cannot enter the
top-K, so the returned result set is unchanged (ties aside).

Delivered as `~/Projects/Scylla/tantivy-p99` (0.26.1 + the patch), wired into
the worktree `~/Projects/Scylla/vector-store-p99-phrase-prune` (branch
`p99-phrase-prune`) via `[patch.crates-io]`. All **1,038 upstream Tantivy
tests pass** with the patch applied.

### Building the two arms honestly

The prebuilt `1.10.0-45-g94a23ef2-amd64` image could not be the control: it
was built by a different toolchain on a different host, so any difference
would confound the patch with the build. Both arms were therefore built from
the same commit, same toolchain, same flags, differing only in whether the
`tantivy = { path = "../tantivy-p99" }` patch line is active:

- `scylladb/vector-store:p99-v0-control` — stock Tantivy 0.26.1
- `scylladb/vector-store:p99-v1-prune` — patched Tantivy

Two traps worth recording, both of which would have quietly invalidated the
comparison:

- Removing the patch line let Cargo resolve `tantivy = "0.26.1"` **up to
  0.26.2**, so the first "control" was a different Tantivy than the patched
  arm. Pinned with `cargo update -p tantivy --precise 0.26.1`.
- The images were first built `FROM redhat/ubi9-minimal`, which has an older
  glibc than the Fedora 44 build host; the binary died on
  `GLIBC_2.38 not found`. Both arms rebuilt `FROM fedora:44`.

The control build reproduces the prebuilt image's numbers (phrase
p50 2.899 / p99 14.215 against 2.854 / 14.417, K-sweep still flat), so the
local build introduces no bias.

### Final numbers (N=3)

`run_ab.sh 3` — each repetition restarts the container, rebuilds the in-RAM
index from the base table, records whatever segment count it settles on, and
measures. OpenSearch measured warm, N=3, on the same index. Medians, with the
full spread across repetitions:

| class | arm | p50 median (range) | p99 median (range) |
|---|---|---|---|
| `phrase` | OpenSearch | 1.93 (1.87-1.94) | 4.99 (4.79-6.14) |
| `phrase` | v0 control | 2.94 (2.90-2.98) | 13.83 (13.66-16.51) |
| `phrase` | **v1 pruning** | **1.41** (1.37-1.54) | **3.61** (3.54-3.92) |
| `common_term` | OpenSearch | 1.26 (1.24-1.29) | 2.16 (2.15-2.71) |
| `common_term` | v0 control | 0.70 (0.62-0.71) | 1.13 (0.97-1.14) |
| `common_term` | v1 pruning | 0.72 (0.70-0.72) | 1.44 (1.34-1.57) |
| `rare_term` | OpenSearch | 1.13 (1.12-1.20) | 1.90 (1.65-2.32) |
| `rare_term` | v0 control | 0.58 (0.55-0.59) | 0.87 (0.86-1.07) |
| `rare_term` | v1 pruning | 0.58 (0.57-0.59) | 1.15 (0.73-1.25) |

- **Phrase p99: 13.83 → 3.61 ms, 3.8x.** The before/after ranges do not
  overlap or come close (13.66-16.51 against 3.54-3.92).
- **Phrase p50: 2.94 → 1.41 ms, 2.1x.**
- Against OpenSearch, phrase goes from **2.8x slower to 1.4x faster** at p99,
  and from 1.5x slower to 1.4x faster at p50.
- `common_term` and `rare_term` p50 do not move (0.70 → 0.72, 0.58 → 0.58),
  which is the point: the patch touches only the phrase path, and that rules
  out "the index just merged better" — that would have moved every class.

Two honest caveats on this table:

- `common_term` and `rare_term` **p99** read slightly higher on v1
  (1.13 → 1.44, 0.87 → 1.15), which looked like a possible regression. It is
  not: repeating the `common_term` measurement five times **against one
  running v1 instance, with no rebuild in between**, gives p99 values of
  1.31, 1.24, 1.24, 1.29 and **2.05** ms — a within-instance spread of
  0.81 ms, nearly triple the 0.31 ms difference in question. p50 over the
  same five runs is stable at 0.688-0.722 ms. The difference is run-to-run
  noise in a percentile taken from 800 samples of a sub-millisecond query,
  measured rather than assumed.
- The segment count each rebuild settles on is uncontrolled and varied
  (v0: 19, 17, 10; v1: 16, 19, 20). The phrase result survives that spread —
  and note v1 ran on *more* segments on average than v0 and still won by
  3.8x, which is stronger evidence against a segment-count explanation than
  the matched comparison below.

An earlier single-shot pair measured both arms at an identical 9 segments and
agreed: v0 2.87 / 13.98 against v1 1.25 / 3.20. A second incidental pair both
landed on 17 segments: v0 2.80 / 13.87 against v1 1.40 / 3.66.

The queries that were pathological are exactly the ones that got fixed:

| query | matches | v0 | **v1** | OpenSearch |
|---|---|---|---|---|
| `"can help"` | 184,352 | 12.88 ms | **2.06 ms** | 2.66 ms |
| `"you can"` | 185,015 | 12.06 ms | **2.08 ms** | 3.40 ms |
| `"article about"` | 176,399 | 11.05 ms | **2.31 ms** | 2.26 ms |
| `"made longer"` | 183,937 | 11.44 ms | **2.16 ms** | 1.92 ms |
| `"short article"` | 183,915 | 10.60 ms | **1.98 ms** | 2.03 ms |
| `"archived from"` | 68,413 | 8.09 ms | **2.01 ms** | 2.40 ms |

Latency no longer tracks match count: a 185,000-match phrase and a
12,000-match phrase now cost about the same, which is what pruning is *for*.

### The K sweep, re-run — the shape changed

| `LIMIT` | OpenSearch | v0 control | **v1 pruning** |
|---|---|---|---|
| 1 | 1.77 ms | 12.84 ms | **2.01 ms** |
| 10 | 3.25 ms | 13.16 ms | **2.09 ms** |
| 100 | 7.92 ms | 13.84 ms | **2.53 ms** |
| 1000 | 32.88 ms | 15.66 ms | **7.00 ms** |

v0 was flat in K — the signature of no pruning. v1 now *scales* with K
(2.01 → 7.00), which is the signature of a pruning engine doing less work
when less is asked of it. It scales far more gently than OpenSearch
(1.77 → 32.88, an 18.6x rise against 3.5x), so the advantage widens with K:
**4.7x faster than OpenSearch at K=1000.**

## Verification — did the results change?

Pruning is only legitimate if it returns what the full walk would have
returned. `dump_results.py` captures the top-10 primary keys for all 1,199
queries in `data/queries.json`; `--diff` compares two dumps.

Dumped twice against the same running index: **1,199 / 1,199 identical.**
Query execution is deterministic, so any difference must come from the patch
or from the index rebuild.

Comparing control against patched showed 67 of 200 phrase queries with a
different top-10 — which looks alarming until you measure what changing
*nothing* does. Restarting the **same control binary** and rebuilding the
in-RAM index from the base table:

| class | rebuild noise floor (v0 vs v0, rebuilt) | patch (v0 vs v1) |
|---|---|---|
| `phrase` | **65** changed | **67** changed |
| `common_term` | 15 | 10 |
| `rare_term` | 17 | 14 |
| `bool_and` | 8 | 6 |
| `bool_mixed` | 4 | 5 |
| `bool_not` | 14 | 8 |

The patch's differences sit **at or below the noise floor of rebuilding the
identical binary**, in every class. The phrase class churns hardest because
its tie sets are enormous — ~184,000 near-identical boilerplate documents
competing for 10 slots — which is the same corpus property behind the recall
finding. Nothing here is attributable to pruning.

### Recall is unchanged

`ftsbench.recall_check`, full 200 queries/class, jaccard@10 against
OpenSearch, on the full 270,269-doc corpus:

| class | v0 control | v1 pruning |
|---|---|---|
| `phrase` | 0.92 | 0.91 |
| `common_term` | 0.98 | 0.98 |
| `rare_term` | 0.98 | 0.98 |
| `bool_and` | 0.99 | 0.99 |
| `bool_not` | 0.98 | 0.98 |
| `bool_mixed` | 0.99 | 0.99 |

Within tie-churn noise, so the speedup costs no agreement with OpenSearch.

Worth recording separately: phrase agreement is **0.91-0.92 on the full
270,269-doc corpus**, against the 0.43 measured earlier on a 5,000-doc slice
and 0.52 on a 500,000-doc enwiki slice. The "phrase has bad recall" symptom is
substantially a small-corpus tie artifact that dilutes as the corpus grows —
it is not an engine defect, and it is not what the latency fix addressed.

## v2 — a fast field for `primary_id`. Measured, rejected.

With phrase fixed, the same treatment was applied to `common_term`. Profiling
it (`probe_term.py`) decomposed a 0.79 ms single-term query against 204,905
matching documents:

| component | how it was isolated | cost | share |
|---|---|---|---|
| HTTP transport | latency of `GET /api/v1/status`, which does no search | 0.441 ms | 56% |
| parse + per-segment setup | a term matching nothing, minus the above | 0.050 ms | 6% |
| matching 204,905 docs | `LIMIT 1`, minus the above | 0.121 ms | 15% |
| fetching 9 more documents | `LIMIT 10` minus `LIMIT 1` | 0.178 ms | 23% |

Matching 205,000 documents costs 0.12 ms — block-max WAND doing its job. The
apparent target was the document fetch: `handle_search` called
`searcher.doc(doc_address)` for every hit, which decompresses a stored-field
block, purely to read back one `u64`. That worked out at ~20 µs per returned
document.

**The fix that should have worked:** add `FAST` to `primary_id` so it also
lives in the columnar store, and read it from a `Column<u64>` opened once per
segment instead of from the document store per hit. Built, all 59
vector-store FTS tests pass.

**It changed nothing.** K sweep on the same term, v1 against v2:

| `LIMIT` | v1 (doc store) | v2 (fast field) |
|---|---|---|
| 1 | 0.57 ms | 0.63 ms |
| 10 | 0.81 ms | 0.77 ms |
| 100 | 1.40 ms | 1.47 ms |
| 1000 | **3.23 ms** | **3.23 ms** |

Identical at every K, exactly where a per-document saving would have been
most visible. The index, meanwhile, grew from ~172 MB to 183 MB for the
columnar copy (segment counts differ between rebuilds, so treat the size
delta as indicative).

**Why the hypothesis was wrong.** The 20 µs/doc figure came from the
`LIMIT 1` → `LIMIT 10` step, which bundles first-touch costs into the first
few documents. The steady-state marginal cost, read off the `LIMIT 100` →
`LIMIT 1000` segment, was already only ~2 µs/doc. Tantivy's doc store is
cheap here precisely because these documents are tiny — one `u64` each, so a
single decompressed block serves many hits. There was no 20 µs/doc to
reclaim, and opening a column per segment costs something of its own.

v2 is therefore **reverted** — the attempt is preserved as
`v2-fastfield-attempt.patch` and its measurements as
`measurements/v2-fastfield-*`. The shipped change remains v1 alone.

## Conclusion for `common_term`: no engine headroom worth taking

The same investigation that fixed phrase says, for single terms, that there
is nothing to fix:

- The vector-store is **already faster than OpenSearch** — 0.70 ms against
  1.26 ms at p50, 1.13 ms against 2.16 ms at p99 (N=3 medians).
- **56% of the measured latency is HTTP transport**, not the engine. That
  share is an artifact of probing over the vector-store's HTTP API; the
  production path is CQL, which measured *faster* than the direct HTTP probe
  (0.63 ms against 0.73 ms p50).
- Of what is left, matching 205,000 documents is 0.12 ms and the per-document
  cost is ~2 µs. Term queries already take the pruning path that phrase was
  missing.

The remaining engine-side candidate is the `QueryParser` rebuilt per query
(~50 µs, 6%). That is real but small, and it is shared with every other
class rather than specific to `common_term`.

So the honest answer to "do the same for `common_term`" is that the same
method was applied and returned a negative: the class is already won, and the
one hypothesis worth testing was tested and rejected above.

## Files

| path | what it is |
|---|---|
| `run_ab.sh` | N-repetition A/B: swaps image, waits for the index, measures, appends CSV |
| `probe_phrase.py` | match-count table and the K sweep |
| `probe_term.py` | single-term cost decomposition: floor, K sweep, per-doc |
| `v2-fastfield-attempt.patch` | the rejected fast-field change, kept as a record |
| `dump_results.py` | top-K result dump, and `--diff` between two dumps |
| `summarize_report.py` | one `query_bench` report → CSV rows |
| `plot_phrase.py` | the two charts |
| `charts/` | `phrase-k-sweep.png`, `phrase-per-class.png` |
| `measurements/` | every JSON/CSV behind the numbers above |
| `SLIDES-phrase.md` | the talk version of this story |

One trap `run_ab.sh` exists to avoid: right after a restart the status endpoint
can still report the *previous* index as `SERVING` at full count while the new
container is still scanning, so a readiness gate on status alone returns
instantly and the benchmark then dies on `503` from the bm25 endpoint. The
script gates on the bm25 endpoint actually answering.

## Measurement commands (reproducible)

```bash
# stacks, from bench/
export VECTOR_STORE_IMAGE=scylladb/vector-store:1.10.0-45-g94a23ef2-amd64
make os-reset scylla-reset && make os-up scylla-up && make os-wait scylla-wait
make os-index && make scylla-schema scylla-index
make os-load  && make scylla-load && make scylla-serving

# per-class latency, one arm at a time
.venv/bin/python3 -m ftsbench.query_bench --engine vector-store \
  --queries optimization/phrase/measurements/queries-3class-simplewiki.json \
  --output /tmp/out.json --limit 10 --warmup 3 --iterations 20 --port 19042

# match-count table and the K sweep
.venv/bin/python3 optimization/phrase/probe_phrase.py 15 /tmp/probe.json

# segment counts
curl -s localhost:16080/metrics | grep ^fts_segment_count
curl -s 'localhost:9200/wiki-articles/_segments' | python3 -c \
  "import json,sys;print(len(json.load(sys.stdin)['indices']['wiki-articles']['shards']['0'][0]['segments']))"
```

Index shape at v0: vector-store 19 segments / 172 MB, OpenSearch 4 segments.
Segment count is *not* the explanation — it would penalize every query class
equally, and `common_term` is fast — but it is recorded because it changes
what a merge-policy experiment would mean.
