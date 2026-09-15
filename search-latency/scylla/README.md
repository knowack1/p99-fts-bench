# scyllasearch — what a search costs on ScyllaDB, and how much of that is ScyllaDB

```bash
cargo run --release -- \
  --corpus ../../data/corpus.jsonl \
  --queries ../../data/queries.json \
  --concurrency 1,2,4,8,16,32,64 \
  --hosts 127.0.0.1 --port 19042 \
  --vs-url http://127.0.0.1:16080 \
  --out run/points/scylla-cql.csv \
  --latencies-dir run/latencies/cql
```

Writes the seventeen-column cell CSV described in
[`../README.md`](../README.md): one row per (concurrency, query class).

## Two interfaces, one index

| `--interface` | What it times | `engine`,`interface` in the CSV |
|---|---|---|
| `cql` (default) | the whole round trip: coordinator hop, BM25 dispatch into the vector-store's index, and the read that satisfies the projection | `scylladb`,`cql` |
| `vector-store` | the index alone — `POST /api/v1/indexes/{ks}/{idx}/bm25`, ScyllaDB out of the path | `scylladb`,`vector-store` |

**`cql` minus `vector-store` is ScyllaDB's own read overhead**, and that
subtraction is the only reason the second interface exists. It is valid only
because neither arm fetched documents: the BM25 endpoint returns primary keys
and scores and cannot return text, so `--fetch-documents` is refused there
rather than ignored. A matrix where the CQL arm projected `title` and `body`
while this one returned identities would put the cost of that fetch on the
chart as an engine property.

Both interfaces read the same Tantivy index, so at the same `--limit` they see
the same number of documents. A live test asserts exactly that, because it is
what makes the subtraction mean anything.

## The statement

```sql
SELECT article_id FROM articles
 WHERE BM25(body, 'kraken') > 0
 ORDER BY BM25(body, 'kraken')
 LIMIT 10
```

The M1 shape, and not a template this tool may vary: the `LIMIT` is mandatory
and capped at 1000 (so is `--limit`), the same term appears in the `WHERE` and
in the `ORDER BY`, there is no other restriction, and `BM25()` is not
projectable. The term is **written into the statement** rather than bound —
the identical-term rule is unverified for bound parameters, and a harness that
guessed wrong there would be measuring a query shape the engine does not
promise. Single quotes are doubled, because a query set built from real article
text contains apostrophes.

`--statement` chooses how that statement reaches the coordinator, and the two
are different measurements:

| | What it costs | Why you would pick it |
|---|---|---|
| `literal` (default) | a parse per request | Lucene's `query_string` parser does the same on the other side, and this bench's Python read arm has always done it |
| `prepared` | one parse per *distinct query*, before the matrix starts | what an application does |

Neither is wrong; mixing them in one chart is, so `statement=` is a header fact.

`--fetch-documents` projects `article_id, title, body` — the same two text
columns the OpenSearch half projects, under the same names. The rows are then
deserialized rather than counted off the frame, which is the point rather than
an artefact: that deserialization is what copies the article text into the
client, and a run that asked for the columns and never touched them would be
timing a transfer nobody paid for.

## Before the first query

`--corpus` is not only for the build. It is how the tool knows how many
documents the index is supposed to hold:

```text
count the corpus ─► read /api/v1/indexes/{ks}/{idx}/status
                       ├─ same count      ─► measure
                       ├─ fewer, or absent ─► DROP KEYSPACE, fill, wait, measure
                       ├─ more             ─► refuse: not built from this corpus
                       └─ unreadable       ─► refuse
```

The fill is `scyllarate`'s, whole: the `DROP KEYSPACE`, the two gates that make
it a reset rather than a hope, and the prepared INSERT at `--load-concurrency`.
**This destroys data**, and says so naming the keyspace before it looks at
anything. `--no-index-build` turns every build into a refusal;
`--rebuild-index` forces one.

The keyspace has to exist when the tool connects — the same precondition
`scyllarate` has, and the same message if it does not: apply
`bench/scylladb/schema.cql` first. After that the reset owns it.

## Flags worth knowing

| Flag | Default | Why it matters |
|---|---|---|
| `--concurrency` | — | the X axis. Repeat the ladder (`8,16,32,8,16,32`) to interleave two traversals of the matrix. |
| `--query-classes` | every class | the other dimension; names come from the query set. |
| `--warmup` / `--duration` | 5s / 20s | the discarded and the counted window, per cell. |
| `--limit` | 10 | top-N. One value per run, and a column. |
| `--tokio-workers` | every core | cores serving the in-flight requests, not requests outstanding. |
| `--latencies-dir` | off | every latency behind the percentiles; give it whenever the run will be repeated. |
| `--consistency` | `LOCAL_ONE` | reads, so it is on the measured path. |

## Tests

```bash
cargo fmt --check && cargo clippy --all-targets && cargo test
cargo test --test live_search -- --ignored   # needs a built index
```

The statement shapes, the escaping, the BM25 payload and its hit count are all
pinned without an engine — the BM25 arm against a stub socket, the rest as pure
functions. The live tests need a running ScyllaDB and vector-store with the
corpus indexed; there is no sink that can stand in for them, because a search
against an empty index is the one answer this harness treats as a failure.
