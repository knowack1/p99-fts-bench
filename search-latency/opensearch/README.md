# ossearch — what a search costs on OpenSearch

```bash
cargo run --release -- \
  --corpus ../../data/corpus.jsonl \
  --queries ../../data/queries.json \
  --concurrency 1,2,4,8,16,32,64 \
  --url http://127.0.0.1:9200 \
  --out run/points/opensearch.csv \
  --latencies-dir run/latencies/opensearch
```

Writes the seventeen-column cell CSV described in
[`../README.md`](../README.md): one row per (concurrency, query class), with
`engine=opensearch` and `interface=http`.

## The request

```json
{
  "size": 10,
  "_source": false,
  "track_total_hits": false,
  "query": {"query_string": {"query": "alpha AND beta",
                             "default_field": "body",
                             "default_operator": "OR"}}
}
```

Three deliberate choices, each of them a parity decision rather than a taste
one:

- **`query_string`, not `match`.** Single terms, `"quoted phrases"`, `AND` /
  `OR` / `NOT` and `(grouping)` mean the same thing in Lucene's `query_string`
  parser as in the Tantivy parser behind ScyllaDB's `BM25()`. One query set is
  therefore valid for both engines, and a class means the same thing on both
  sides. A live test asks every shape the generator writes.
- **`track_total_hits: false`.** ScyllaDB reports no result totals at all.
  Leaving this on would have OpenSearch count every match while the other engine
  counted the top N, which on a common term is a real amount of work.
- **`_source` mirrors the other engine's projection.** Off, and a hit is an
  identity; `--fetch-documents`, and it is `title` and `body` — the same two
  fields under the same two names the CQL half projects. The reply is
  deserialized, which is what makes the flag cost what it costs: with `_source`
  on, the article text is in that body and parsing it is the client's half of
  the fetch.

`--limit` is capped at 1000 here too, which is ScyllaDB's ceiling rather than
OpenSearch's: a matrix whose two halves could ask for different top-Ns would not
be one matrix.

## Before the first query

`--corpus` is not only for the build. It is how the tool knows how many
documents the index is supposed to hold:

```text
count the corpus ─► read /{index}/_stats
                       ├─ same count      ─► measure
                       ├─ fewer, or absent ─► DELETE, PUT, fill, refresh, wait, measure
                       ├─ more             ─► refuse: not built from this corpus
                       └─ unreadable       ─► refuse
```

The fill is `osrate`'s, whole: the `DELETE`/`PUT` from the embedded index
config, the two gates, and the `_bulk` at `--load-concurrency` ×
`--load-batch-size`. **This destroys data**, and says so naming the index before
it looks at anything. `--no-index-build` turns every build into a refusal;
`--rebuild-index` forces one.

A `_refresh` is asked for once the load is done. That is legitimate here and
forbidden next door in [`../../build-rate`](../../build-rate/README.md): there
the refresh is the measurement, here it is the precondition, and what is timed
starts after it.

## The analyzer probe

Runs whether or not this run built the index, unlike the sibling tree where it
is part of the reset. An index somebody else created — with the default
`standard` analyzer, say, instead of the parity one — would make every latency
below it a comparison of tokenizers rather than of engines, and that is a
failure mode a read benchmark against a resident index is *more* exposed to, not
less. `--no-analyzer-check` turns it off and `analyzer_check=false` reaches the
header.

`--index-config` (`ramindex`, `disk`, or a path) and `--refresh-interval` are
`osrate`'s flags and mean exactly what they mean there. They only affect a run
that actually builds.

## Flags worth knowing

| Flag | Default | Why it matters |
|---|---|---|
| `--concurrency` | — | the X axis. Repeat the ladder (`8,16,32,8,16,32`) to interleave two traversals of the matrix. |
| `--query-classes` | every class | the other dimension; names come from the query set. |
| `--warmup` / `--duration` | 5s / 20s | the discarded and the counted window, per cell. |
| `--limit` | 10 | top-N. One value per run, and a column. |
| `--default-operator` | `OR` | how bare terms combine; `OR` is what both parsers do by default. |
| `--tokio-workers` | every core | cores serving the in-flight requests, not requests outstanding. |
| `--latencies-dir` | off | every latency behind the percentiles; give it whenever the run will be repeated. |

Connection pooling is left at the HTTP client's defaults on purpose, the same as
`osrate`: reqwest keeps an unbounded idle pool per host, so N searches in flight
get N sockets by themselves and `--concurrency` is what the engine is actually
being asked at once.

## Tests

```bash
cargo fmt --check && cargo clippy --all-targets && cargo test
cargo test --test live_search -- --ignored   # needs a built index
```

The request body and the hit count are pinned against a stub socket rather than
an engine. The live tests need a running OpenSearch with the corpus indexed;
there is no sink that can stand in for it, because a search against an empty
index is the one answer this harness treats as a failure.
