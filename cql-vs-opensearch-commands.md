# CQL ↔ OpenSearch — equivalent commands side by side

The exact commands each engine uses in this benchmark for the three core
operations: insert data, create index, search. All commands are taken
verbatim from this repo; sources are cited per section. For the full query
class matrix and parity discussion see [feature-mapping.md](feature-mapping.md).

## 1. Create index

**ScyllaDB CQL** ([scylladb/index.cql](scylladb/index.cql)) — on the
pre-existing `wiki.articles` table:

```sql
CREATE CUSTOM INDEX IF NOT EXISTS articles_body_fts
  ON wiki.articles(body) USING 'fulltext_index';
```

**OpenSearch** ([opensearch/create_index.sh](opensearch/create_index.sh)) —
one call creates index, analyzer, and mappings together:

```bash
curl -fsS -X PUT "http://localhost:9200/wiki-articles" \
  -H 'Content-Type: application/json' \
  --data-binary "@index-config.json"
```

[opensearch/index-config.json](opensearch/index-config.json) defines the
`m1_parity` analyzer (a `pattern` tokenizer on `[^\p{IsAlphabetic}\p{N}]+`,
which reproduces Tantivy's `SimpleTokenizer`, + lowercase + `_english_` stop
words, **no stemmer**) and maps `body` as `text` with that analyzer. It is
deliberately *not* the `standard` tokenizer — see `feature-mapping.md`.

**Structural asymmetry:** in ScyllaDB the table exists first
([scylladb/schema.cql](scylladb/schema.cql)) and indexing is a separate,
orderable step — index-before-load exercises the CDC tail path,
load-before-index the bootstrap base-table scan. In OpenSearch the inverted
index *is* the write path, so no such split exists.

## 2. Insert data

**ScyllaDB CQL** ([ftsbench/scylla_load.py](ftsbench/scylla_load.py)) — a
prepared statement executed concurrently (128-way by default):

```sql
INSERT INTO articles (article_id, page_id, title, body) VALUES (?, ?, ?, ?)
```

**OpenSearch** ([ftsbench/opensearch_load.py](ftsbench/opensearch_load.py)) —
NDJSON batches to the `_bulk` API, 500 docs per request:

```
POST /_bulk
Content-Type: application/x-ndjson

{"index": {"_index": "wiki-articles", "_id": "12345"}}
{"page_id": 12345, "title": "…", "body": "…"}
...
```

Each is the engine's native fast path. Freshness caveat: OpenSearch
documents become searchable after `refresh_interval` (1s, set explicitly in
the index config), while ScyllaDB's CDC-fed index commits at ~3s — the
knobs are not equivalent; chart C8 measures the difference.

## 3. Search

**ScyllaDB CQL** — the one M1 query shape; only `<q>` and `LIMIT` ever vary:

```sql
SELECT article_id FROM wiki.articles
WHERE BM25(body, '<q>') > 0
ORDER BY BM25(body, '<q>')
LIMIT 10;
```

**OpenSearch** — the benchmark counterpart, chosen so the *identical query
text* `<q>` (bare terms, `"quoted phrases"`, `AND`/`OR`/`NOT`, grouping)
goes to both engines:

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

The envelope mirrors the CQL constraints line by line:

| CQL constraint | OpenSearch mirror |
|---|---|
| `LIMIT 10` | `"size": 10` |
| `SELECT article_id` only | `"_source": false` → `_id` only |
| No result-count reporting | `"track_total_hits": false` |
| `BM25()` not projectable | score not fetched (ranking still by `_score` internally) |

An OpenSearch practitioner would idiomatically write
`match`/`match_phrase`/`bool` instead of `query_string` — the per-class
idiomatic forms are in [feature-mapping.md](feature-mapping.md), along with
the open item to spot-check that both DSL forms perform the same before
publishing numbers.
