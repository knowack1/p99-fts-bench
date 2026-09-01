# ScyllaDB FTS ↔ OpenSearch — feature & query mapping

How every ScyllaDB M1 capability looks on the OpenSearch side, plus the
OpenSearch shapes of the M2/M3 features (useful for the roadmap slides).
Ground truth for the ScyllaDB side: `~/Projects/Scylla/experiments/fts-demo/`.
Benchmark schema used throughout: ScyllaDB `wiki.articles` (indexed column
`body`), OpenSearch index `wiki-articles` (field `body`).

## The one ScyllaDB query shape (recap)

```sql
SELECT article_id FROM wiki.articles
WHERE BM25(body, '<q>') > 0
ORDER BY BM25(body, '<q>')
LIMIT 10;
```

Rules (each violation raises a specific `InvalidRequest`): `LIMIT` mandatory
and ≤ 1000; identical query text in `WHERE` and `ORDER BY`; no other `WHERE`
restriction of any kind; `BM25()` not allowed in the `SELECT` list (no visible
score). Only `<q>` and `LIMIT` ever vary.

## The direct OpenSearch counterpart: `query_string`

`<q>` is parsed by Tantivy; OpenSearch's `query_string` query is parsed by
Lucene's query parser — and the two syntaxes agree on everything M1 supports:
bare terms, `"quoted phrases"`, `AND` / `OR` / `NOT`, `(grouping)`. So the
benchmark can send **the identical query text to both engines**:

```
POST /wiki-articles/_search
{
  "size": 10,
  "_source": false,
  "track_total_hits": false,
  "query": {
    "query_string": {
      "query": "<q>",
      "default_field": "body"
    }
  }
}
```

Fairness mirroring built into that envelope:

| M1 behaviour | OpenSearch mirror |
|---|---|
| `LIMIT 10` | `"size": 10` |
| Returns row identity only (`SELECT article_id`) | `"_source": false` → `_id` only |
| No result-count reporting | `"track_total_hits": false` (default counts up to 10k hits — extra work ScyllaDB doesn't do) |
| Score not projectable | ranking still by `_score` internally, same as ScyllaDB — we just don't fetch extra data |
| BM25 similarity | Lucene BM25 is the default; Tantivy also uses BM25 with the same textbook defaults (k1=1.2, b=0.75) — verify, see below |

## Query class mapping (the benchmark matrix, concept doc §7.3)

Same `<q>` both sides; the CQL is always the one shape above.

| Class | Query text `<q>` (both engines) | Idiomatic OpenSearch DSL equivalent |
|---|---|---|
| Rare single term | `photosynthesis` | `{"match": {"body": "photosynthesis"}}` |
| Common term, ranked top-10 | `database` | `{"match": {"body": "database"}}` |
| Exact phrase | `"theory of relativity"` | `{"match_phrase": {"body": "theory of relativity"}}` |
| Boolean AND | `database AND distributed` | `{"match": {"body": {"query": "database distributed", "operator": "and"}}}` |
| Boolean NOT | `python NOT snake` | `bool`: `must` match python, `must_not` match snake |
| Mixed + grouping | `(jupiter OR saturn) AND planet NOT rings` | nested `bool` (below) |

The idiomatic `bool` form of the mixed query, for reference:

```json
{
  "query": {
    "bool": {
      "must": [
        {"bool": {"should": [
          {"match": {"body": "jupiter"}},
          {"match": {"body": "saturn"}}
        ], "minimum_should_match": 1}},
        {"match": {"body": "planet"}}
      ],
      "must_not": [{"match": {"body": "rings"}}]
    }
  }
}
```

**Which form to benchmark:** `query_string`, because it guarantees text-level
parity with what ScyllaDB parses. But an OpenSearch practitioner would write
`match`/`bool` — so before publishing, spot-check that `query_string` and the
idiomatic DSL perform the same for our classes, or someone will claim we
benchmarked "the slow API". If they differ, bench both and say so.

## Analyzer parity

M1 analyzer: tokenize on non-alphanumeric, lowercase, English stop words,
**no stemming**. The OpenSearch mirror (in `opensearch/index-config.json`):

```json
"tokenizer": {
  "alnum_split": {
    "type": "pattern",
    "pattern": "[^\\p{IsAlphabetic}\\p{N}]+"
  }
},
"analyzer": {
  "m1_parity": {
    "type": "custom",
    "tokenizer": "alnum_split",
    "filter": ["lowercase", "english_stop"]
  }
}
```

with `english_stop` = `"stopwords": "_english_"` (Lucene's 33-word English
list) and **no stemmer filter**.

**The tokenizer is not `standard`, and that is the whole point.** Tantivy's
`SimpleTokenizer` breaks on every rune that is not `Alphabetic | N`.
OpenSearch's `standard` tokenizer implements UAX#29 word segmentation, which
deliberately holds `don't`, `u.s`, `3.14`, `foo_bar` and `www.fifa.com`
together and splits CJK one character at a time. Those are different indexes
over the same corpus: measured across 3,000 simplewiki documents, **70.3% of
documents tokenized differently** (31,777 divergent tokens; 525,434 OpenSearch
tokens against Tantivy's 533,388). The `pattern` tokenizer above reproduces
`SimpleTokenizer` exactly — identical token text, identical positions,
identical total (533,388) — at a cost of ~11% OpenSearch indexing throughput.

`opensearch/verify_analyzer.sh` **asserts** thirteen token streams, positions
included, and exits non-zero on any mismatch. Its probes are chosen from the
divergence classes above; the four it used to check by eyeball (case folding,
stop-word drop, hyphen split, no stemming) all sit in the region where the two
tokenizers happen to agree, which is why parity looked fine while 70% of the
corpus disagreed.

Note the default OpenSearch behaviour we are deliberately turning OFF for
parity: the `standard` analyzer alone would keep stop words; the common
`english` analyzer would stem. Both would change recall and ranking and make
latency comparisons meaningless.

## Ingest parity notes

- **Freshness:** OpenSearch `refresh_interval` (default 1s; our config states
  it explicitly) vs. ScyllaDB CDC-fed index (~3s commit interval). Chart C8
  measures this; don't equate the knobs silently.
- **Bulk vs. CQL:** `_bulk` NDJSON batches vs. prepared concurrent INSERTs —
  each engine's native fast path (`ftsbench/opensearch_load.py`,
  `ftsbench/scylla_load.py`).
- **Build ordering is a real variable on the ScyllaDB side:** index before
  load (CDC tail path) vs. load before index (bootstrap base-table scan).
  OpenSearch has no such split — the inverted index is the write path.
- OpenSearch `refresh_interval: -1` during bulk load is a standard
  build-throughput tuning; if used for C1/C2, publish it.

## M2/M3 preview — what the OpenSearch shapes are today

Useful for the roadmap slides: these are the OpenSearch queries whose ScyllaDB
counterparts error today (show the real M2 error from `demo-m2.md`).

**M2 — filter alongside text** (`... AND author = 'John Smith'` fails today):

```json
{
  "query": {
    "bool": {
      "must": [{"match": {"body": "relativity"}}],
      "filter": [{"term": {"author": "John Smith"}}]
    }
  }
}
```

**M2 — hybrid BM25 + vector fusion** (`USING FUSION = {RRF | WEIGHTED}`
planned): OpenSearch does this with a k-NN field plus a search pipeline
(normalization/RRF processor). Out of benchmark scope — capability-matrix
row only.

**M3 — fuzzy** (`reletivity~1` parses but matches nothing today):

```json
{"query": {"query_string": {"query": "reletivity~1", "default_field": "body"}}}
```

(idiomatic: `{"match": {"body": {"query": "reletivity", "fuzziness": 1}}}`)

**M3 — trailing-wildcard prefix** (`photo*`):

```json
{"query": {"query_string": {"query": "photo*", "default_field": "body"}}}
```

(idiomatic: `{"prefix": {"body": "photo"}}`)

## Open parity questions — verify before any published number

1. **Default operator for multi-term bare queries** (`theory relativity`
   unquoted): Lucene's `query_string` defaults to OR; Tantivy's parser default
   must be established empirically. Procedure: two documents, one containing
   only `theory`, one containing both terms; run the unquoted two-term query
   on ScyllaDB; if the single-term doc matches, Tantivy is OR — set
   `default_operator` accordingly (engine client flag `--default-operator`).
2. ~~**Stop-word list identity:**~~ **CLOSED — identical.** Tantivy's
   `Language::English` list is Lucene's own 33-word
   `EnglishAnalyzer.ENGLISH_STOP_WORDS_SET`, cited as such in
   `tokenizer/stop_word_filter/mod.rs`. Same 33 words as `_english_`.
3. **Phrase queries across removed stop words** (`"theory of relativity"`):
   position *handling* is now confirmed identical — a dropped stop word leaves
   the same position gap on both sides (`theory of relativity` →
   `0:theory 2:relativity` on both), because Tantivy's `StopWordFilter` passes
   the tokenizer's own position through rather than renumbering, exactly like
   Lucene's `StopFilter`. `verify_analyzer.sh` asserts positions. Still to
   replay end-to-end: that `relativity theory` reversed does NOT match.
4. ~~**Tokenizer edge cases:**~~ **CLOSED — the divergence was real and is
   now fixed.** Diffed over a 3,000-document simplewiki sample against
   Tantivy's own analyzer: `standard` disagreed on 70.3% of documents.
   Replaced by a `pattern` tokenizer that matches exactly; asserted by
   `verify_analyzer.sh`. One residual, 0.002% of tokens: Turkish `İ` (U+0130)
   lowercases to `i` + U+0307 in Rust and to `i` in Java. Not fixable from the
   OpenSearch side — a char filter runs *before* the tokenizer, so injecting
   U+0307 splits the token instead.
5. **BM25 parameters:** k1/b defaults believed identical (1.2 / 0.75) — confirm
   in both engines' docs for the pinned versions and state on the methodology
   slide.
