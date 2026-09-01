> **Provenance**: this is the public extract of the benchmark harness for a
> P99 CONF 2026 talk (ScyllaDB native full-text search vs. OpenSearch). It is
> published so the measurement can be reproduced: corpus pipeline, loaders,
> open-loop generator, gates, probes and chart renderers. Result write-ups and
> internal planning documents are not part of this extract, so a few
> cross-references may point at files that live in the internal working repo.
> Engine versions, every flag and every knob: `SUT-CONFIG.md` (what) and
> `TUNING.md` (why). The measured vector-store build comes from
> https://github.com/knowack1/vector-store branch `p99-fts-no-commit-threshold`.

# FTS benchmark toolkit — scaffolding

Tooling for the P99 CONF benchmark: ScyllaDB native FTS vs. OpenSearch over an
English Wikipedia corpus. **Status: scaffolding.** Nothing here has been run at
benchmark scale — the goal of this stage is to de-risk the pipeline: how the
Wikipedia data is obtained, how each ScyllaDB M1 feature maps to OpenSearch,
and how documents get into both engines. The final harness (open-loop load
generator, multi-node deployment) comes later; see "Not here yet" below.

The engine feature mapping lives in [feature-mapping.md](feature-mapping.md) —
read that first if you want the ScyllaDB-CQL ↔ OpenSearch-DSL side-by-side.

## Pipeline

```
Wikimedia cirrus_search_index content shards (.json.bz2, one wiki, N shards)
        │  tools/download_wikipedia.sh          (simplewiki for smoke, enwiki for real)
        ▼
canonical corpus JSONL — one doc/line: {id, uuid, title, text}
        │  python3 -m ftsbench.prepare_corpus
        ├──────────────► OpenSearch   python3 -m ftsbench.opensearch_load   (_bulk NDJSON)
        └──────────────► ScyllaDB     python3 -m ftsbench.scylla_load       (prepared concurrent INSERTs)

corpus term statistics ──► queries.json          python3 -m ftsbench.generate_queries
   (same query text is valid for both engines — Tantivy and Lucene syntax agree
    on terms, "phrases", AND/OR/NOT, and (grouping))
        ▼
python3 -m ftsbench.query_bench --engine {opensearch|scylladb}
        ▼
results JSON: p50/p95/p99 per query class
```

Why `cirrus_search_index` and not the XML dumps: it's the JSON Wikimedia
feeds into its own Elasticsearch/OpenSearch cluster — wikitext already
stripped to plain `text`, one action/source line pair per page. **Note:**
this replaces the older CirrusSearch dumps
(`dumps.wikimedia.org/other/cirrussearch/`), which Wikimedia has deprecated
and stopped producing — see [FREEZE.md](FREEZE.md) for why, and for the
exact pinned snapshot this pipeline is frozen against. No wiki markup
parsing is needed, and the `content` shards contain article namespace only.

## Prerequisites

- Python ≥ 3.10, then `pip install -r requirements.txt`
  (`requests` for OpenSearch, `scylla-driver` for ScyllaDB — the latter is
  only imported by the ScyllaDB commands).
- Docker + compose for the local OpenSearch node.
- A ScyllaDB build with full-text search — the vector-store serves
  `fulltext_index`; stock public images do not. See
  `~/Projects/Scylla/experiments/fts-demo/README.md` for the prerequisites and
  the ground-truth query rules.

## Quickstart (smoke run, simplewiki, laptop)

```bash
cd bench
pip install -r requirements.txt

make download                    # simplewiki cirrus_search_index dump (~560 MB, frozen date)
make corpus MAX_DOCS=20000       # canonical JSONL, capped for a smoke run

make os-up os-wait os-index      # single-node OpenSearch + analyzer-parity index
make os-verify-analyzer          # tokens must match the M1 analyzer behaviour
make os-load MAX_DOCS=20000

make queries                     # term-frequency-bucketed query set, both engines
make bench-os                    # closed-loop latencies -> data/results-opensearch.json

# ScyllaDB side (against the FTS-enabled cluster):
make scylla-schema
make scylla-load MAX_DOCS=20000
make scylla-index                # index AFTER load = bootstrap-scan path;
                                 # swap the order to exercise the CDC tail path
make bench-scylla
```

For the real corpus: `make download WIKI=enwiki` (65 shards, ~40 GB — see
[FREEZE.md](FREEZE.md) for the pinned dump date and why it's frozen).

## C1 — index build throughput

`make c1-os` / `make c1-scylla` run a loader and a sampler together and write
a time series to `data/c1-<engine>.jsonl`, one record per sample:

```bash
make c1-os     MAX_DOCS=150000 LABEL="simplewiki 150k, laptop" CACHE_STATE=cold
make c1-scylla MAX_DOCS=150000 LABEL="simplewiki 150k, laptop" CACHE_STATE=cold
make c1-report                 # summarise both series side by side
```

Two properties this harness deliberately has, because C1 is wrong without
them:

- **Instantaneous docs/s, not a running average.** The finding C1 exists to
  show is the OpenSearch sawtooth as Lucene merges segments; a cumulative
  average erases it. `build_report` reports p10/p90 and a
  `throughput_variability` figure so the dip is quantified, not just drawn.
- **Fixed wall-clock sampling, not every-N-docs.** When throughput collapses
  during a merge, doc-triggered reporting goes sparse exactly where the chart
  needs resolution.

What each side reports as "progress":

| Engine | Source | Note |
|---|---|---|
| OpenSearch | `_stats` → `indexing.index_total` | not `_count`, which only sees *refreshed* docs and would flat-line with `refresh_interval` tuned up |
| ScyllaDB | vector-store `/api/v1/indexes/{ks}/{idx}/status` → `count`, `status` | rows land in the base table first; the index then bootstrap-scans or tails CDC. `time_to_serving_s` is reported once status reaches `SERVING` |

The OpenSearch series additionally carries segment count, active/total merges,
cumulative merge time and store size, so the sawtooth can be *attributed* to
merges rather than merely observed.

Series headers record engine version, cache state and label — the values the
chart footer must state.

**Verified so far:** the OpenSearch path, end to end, against OpenSearch 3.1.0
(Lucene 10.2.1) on simplewiki. The ScyllaDB path is written against the
documented vector-store API but is **not yet verified** — it needs an
FTS-enabled cluster, which stock images do not provide.

## Layout

| Path | What it is |
|---|---|
| `FREEZE.md` | Pinned Wikimedia snapshot date, shard counts, checksums — the "what data did we benchmark" record |
| `COMPARABILITY.md` | What is actually being compared, the remaining asymmetries, and which charts are fair without disclosure |
| `SIZING.md` | Measured Tantivy RAM-per-GB ratio and the resulting instance-size recommendation |
| `HARDWARE.md` | What to provision for the enwiki run, why each box exists, and what it costs |
| `AWS-RUN-PLAN.md` | Runbook for the enwiki/AWS measurement: what to finish on the laptop first, phase order, per-repetition gates, cost control |
| `LAPTOP-RUN-PLAN.md` | The $0 laptop campaign this repo already executed — harness build, verification, and the C1-C8 pass over simplewiki |
| `tantivy-ram/` | Standalone harness replicating the vector-store Tantivy schema, used to measure index RAM footprint |
| `feature-mapping.md` | ScyllaDB M1 ↔ OpenSearch counterpart for every query class, analyzer parity, M2/M3 preview, open verification items |
| `tools/download_wikipedia.sh` | Downloads all content shards for a wiki from a pinned `cirrus_search_index` dump date, verifies `_SUCCESS`, records sha256 checksums |
| `ftsbench/prepare_corpus.py` | cirrus_search_index content shards → canonical corpus JSONL |
| `ftsbench/generate_queries.py` | Corpus term stats → `queries.json` (rare/common/phrase/boolean classes) |
| `ftsbench/opensearch_load.py` | Raw `_bulk` NDJSON loader with docs/s reporting |
| `ftsbench/scylla_load.py` | Prepared, concurrent CQL INSERT loader with docs/s reporting |
| `ftsbench/query_bench.py` | Closed-loop latency skeleton: p50/p95/p99 per class, zero-hit warnings |
| `ftsbench/build_monitor.py` | C1 sampler: polls an engine at a fixed interval, writes a docs/s time series |
| `ftsbench/samplers.py` | Per-engine progress pollers (OpenSearch `_stats`, vector-store status API) |
| `ftsbench/build_report.py` | Summarises a C1 series: sustained rate, p10/p90, stalls, merge attribution |
| `ftsbench/engines.py` | The two query clients — one query text, two engines |
| `ftsbench/analyzer.py` | M1-parity tokenizer used for query generation |
| `opensearch/index-config.json` | Analyzer-parity index settings + mappings |
| `opensearch/create_index.sh` / `verify_analyzer.sh` | Index creation and `_analyze` parity checks |
| `scylladb/schema.cql` / `index.cql` | Keyspace/table and the `fulltext_index`, split so ingest ordering is a choice |
| `docker/docker-compose.opensearch.yml` | Single-node OpenSearch for the local pipeline |

## Fairness knobs already wired (concept doc §7)

- **Analyzer parity**: `opensearch/index-config.json` defines tokenize +
  lowercase + English stop words, no stemming, using a `pattern` tokenizer
  that reproduces Tantivy's `SimpleTokenizer` rather than OpenSearch's
  `standard` (UAX#29) tokenizer — those two disagree on 70% of corpus
  documents. `verify_analyzer.sh` **asserts** thirteen token streams with
  positions and fails the build on any mismatch.
- **Same top-k**: `LIMIT 10` ↔ `"size": 10`, identity-only fetch on both
  sides (`SELECT article_id` ↔ `"_source": false`), `track_total_hits: false`
  because ScyllaDB reports no totals.
- **Same query text** for both engines via `query_string`.
- The docker-compose heap/refresh settings are for the laptop pipeline only —
  the real runs must publish tuned values (internal TODO list).

## Not here yet (deliberately)

- **Open-loop, coordinated-omission-safe load generator** — required for the
  QPS-vs-p99 sweep (chart C7) and the headline HDR curve (C5). The closed-loop
  `query_bench` is for correctness smoke and per-class comparison only.
- **Write-path tail during ingest** (C3): needs per-op latency recording at a
  controlled offered rate, both sinks.
- **Resource footprint capture** (C4) and **freshness probes** (C8:
  write→searchable time vs. refresh interval / CDC lag).
- **Multi-node deployment automation** and RF>1 configs.
- Verification of the open parity questions listed at the end of
  `feature-mapping.md` (default operator, stop-word list identity, phrase
  positions) — do these before trusting any recall-sensitive numbers.
