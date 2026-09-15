# HARNESS-PROMPT.md — the specification the three crates are built to

`core/`, `scylla/` and `opensearch/` are a benchmark, and a benchmark is only
worth what its specification is worth. This is that specification, written as
one prompt: everything the harness had to be told, including the decisions that
are not obvious from the code and would be re-litigated by anyone regenerating
it. Run it against an empty `bench/search-latency/` and you get this tree.

It is the read-path twin of
[`../build-rate/HARNESS-PROMPT.md`](../build-rate/HARNESS-PROMPT.md), and it
leans on it: the loader, the reset and its gates, the index probe, the interrupt
and the runtime are that tree's and are depended on rather than restated.

**What it deliberately leaves to the implementer.** Crate versions, module
layout inside each crate, error types, and every name that is not a CSV column,
a CLI flag or a trait.

---

```text
> Build a search-latency benchmark harness in Rust, from scratch, in
  bench/search-latency. One question, three interfaces: what does a
  full-text search cost at N requests in flight? Never answer it
  against an index that is not finished — half a corpus answers
  faster than a whole one and a p99 does not show it.

LAYOUT
  core/        lib search-latency-core — query set, cell, matrix, CSV
  scylla/      bin scyllasearch — BM25 over CQL, and /bm25 direct
  opensearch/  bin ossearch     — query_string over _search
  Not a cargo workspace, and the same reason as next door: each
  binary's build.rs reads its own lock to stamp the linked driver
  version into every CSV header.
  Depend on ../build-rate — core for the probe, the gate, the
  corpus reader, the interrupt and the runtime; scyllarate and
  osrate for the loader that fills the index. Do not reimplement
  any of it. "The index was complete" has to be one claim, not two.

THE SEAM — an interface supplies one method, core owns the rest
  trait Searcher { fn search<'a>(&'a self, q: &'a str)
                       -> BoxFuture<'a, Result<Found>>;
                   fn interface(&self) -> &'static str;
                   fn endpoint(&self) -> &str; }
  struct Found { hits: usize }
  Boxed, unlike the sibling's Inserter: this is not trying to
  out-run anything, one Box per network round trip is
  unmeasurable, and the ScyllaDB binary picks its interface at run
  time. The query borrows for as long as the future, so an
  implementation that builds a statement around it may, and one
  that can send the text straight out is not made to copy it.
  hits is not decoration. A class that matches nothing is timing
  an empty result set, and that has to be sayable.

THE QUERY SET — ftsbench.generate_queries' output, never invented
  {"corpus": ..., "classes": {"<name>": ["<query>", ...]}}
  Classes in name order; --query-classes selects and orders. An
  unknown name is an error, not an empty column of the matrix: a
  typo would otherwise cost a whole run to discover. An empty
  class is an error too.
  One shared atomic cursor per cell, not a per-worker stride: a
  slow worker would visit fewer of its class's queries and shift
  the class's mix. The atomic costs nothing against a round trip.
  A fresh cursor per cell, so two repetitions of a cell are
  repetitions.

THE CELL — closed loop, N in flight, warm up then count
  N tokio tasks in a JoinSet, each asking the next query the
  moment the previous answers. NO CHANNEL between the workers and
  the engine: a work queue whose latency clock starts at enqueue
  bills the client's own backlog to the engine, which is how the
  Python predecessor once measured p50=79 ms at c=64 where
  Little's law put it near 16 ms.
  --warmup seconds first, counters dropped; then --duration
  seconds counted. A query in flight at the deadline is awaited,
  not abandoned — throughput is completed over elapsed and a
  truncated request would cost its own reply and count the time.
  A request that failed contributes a count and a message and
  nothing to the distribution: its time went on whatever went
  wrong. An empty answer contributes its latency AND a zero-hit
  count, because it is a real answer and a real cost.
  Ctrl-C ends the cell and the matrix, keeping the cells measured.

THE MATRIX — concurrency outermost, class innermost
  Every level against every class, levels walked exactly as given
  so `8,16,32,8,16,32` interleaves two traversals of the whole
  matrix. Outermost because host drift must land across the matrix
  rather than tilting one concurrency curve.
  Nothing is reset between cells and nothing is rebuilt: the index
  is resident and finished, and tearing anything down mid-matrix
  would measure a rebuild.
  --limit is one value per run, not a dimension, and a column.

THE INDEX — built if it has to be, and never measured incomplete
  Count the corpus (lines, cut at --max-docs). Read the probe.
    unreadable      -> refuse. An unanswered poll is not zero.
    docs == corpus  -> skip
    docs >  corpus  -> refuse: not built from this corpus
    otherwise       -> build
  A build resets first, always. A partial index could be a load
  that died or a different corpus at the same ids, and topping it
  up would keep whichever it was.
  Fill at --load-concurrency with the sibling's loader, one level,
  and refuse a load that lost documents rather than letting the
  gate blame the engine for it. Then ask the engine to publish
  what it accepted — legitimate here and nowhere next door,
  because what is timed starts after this — and gate on the count.
  Gate on "at least", then compare exactly, so an index that
  overshoots is named rather than waited on forever. Gate on
  answering queries too: a count read from an index that is not
  serving is a count of something nobody can search.
  --rebuild-index forces it; --no-index-build turns every build
  into a refusal, for an index somebody else manages.
  Warn, naming the keyspace or the index, before anything is
  dropped — the flag defaults to allowing it.

OUTPUT — one schema, three interfaces, blank cells never zeros
  17 columns: concurrency,query_class,queries,errors,wall_s,
  queries_per_s,p50_ms,p90_ms,p99_ms,max_ms,hits_mean,
  zero_hit_queries,distinct_queries,limit,fetch_documents,engine,
  interface
  --latencies-dir adds every latency behind those percentiles, one
  file per cell, <class>-c<conc>-<n>.csv, repeats never
  overwriting, ascending — because percentiles do not average and
  a consumer merging repeats can only recompute them.
  Both carry the same '# key=value' preamble. Columns are
  APPENDED, never inserted. A column a run cannot fill is blank: a
  zero p99 plots as the best point on the curve. engine and
  interface tell three series apart in one file.
  Flush every row; open --out before the first query.

  Four charts come out of this and none of them are in this tree:
  X is concurrency, Y is p50_ms / p90_ms / p99_ms / queries_per_s,
  a series is engine+interface. Write the columns, not the images.

THE INTERFACES
  cql: SELECT {projection} FROM {table}
       WHERE BM25(col,'q') > 0 ORDER BY BM25(col,'q') LIMIT n.
       The M1 shape, not a template to vary. The term is written
       in and single quotes are doubled — the identical-term rule
       is unverified for bound parameters and a harness must not
       guess there. --statement literal re-parses per request,
       which is what the other engine's parser does and what the
       Python read arm did, and is the default; prepared prepares
       each distinct query once before the matrix and is what an
       application does. Header fact, so the two never mix.
       With documents projected, deserialize them: a run that
       asked for title and body and never touched them would be
       timing a transfer nobody paid for.
  vector-store: POST /api/v1/indexes/{ks}/{idx}/bm25
       {"query": ..., "limit": n}. The hit count is the length of
       a primary-key COLUMN, never the number of columns — a
       composite key would otherwise report 2 for any number of
       hits and queries_per_s is computed off it.
       --fetch-documents is REFUSED, not ignored: the endpoint
       cannot return text, and a matrix where the other arms
       projected title and body would put that fetch on the chart
       as an engine property.
  http: POST /{index}/_search, query_string, default_field=body,
       default_operator=OR — the syntax the query set is valid in
       on both sides. track_total_hits FALSE, because ScyllaDB
       reports no totals and counting every match would be work
       the other engine was never asked to do. _source false, or
       ["title","body"] — the same two names the CQL half
       projects. Probe the analyzer whether or not this run built
       the index: one built elsewhere with a different analyzer
       makes every latency a comparison of tokenizers.

EXIT CODE — two ways a run is not a measurement
  Non-zero for a failed request, and non-zero for a cell where
  every query matched nothing. The second is particular to this
  tree: such a cell still answers, still has a p99, and still
  plots, and the number it plots is the cost of finding nothing.
  Say it on stderr too.

DISCIPLINE
  Tests beside each module as <name>_tests.rs, pulled in with
  #[path]. A fake searcher and a fake loader, so the loop, the
  matrix and the bootstrap decision are testable with nothing
  running; a stub HTTP endpoint in core, shared, so both binaries
  pin their payloads against a socket rather than an engine; live
  tests #[ignore]d. No comment that restates the code — extract a
  named function. fmt and clippy clean in all three crates.
```

---

## Checking a regenerated tree against this

| Claim in the prompt | How to falsify it |
|---|---|
| One schema, three interfaces | `head -1` the cell CSVs after the `#` block — 17 identical column names, in order, for both binaries and both `--interface` values. |
| Blank cells, never zeros | Point a run at an endpoint that refuses every query; `p50_ms` … `hits_mean` must be empty, not `0`. |
| Closed loop at N | A fake searcher with a fixed latency must never see more than N requests in flight, at any level. |
| No queue between workers and engine | At a fixed engine latency L, `p50_ms` ≈ L at every concurrency, and `queries_per_s` ≈ N/L. A queue shows up as p50 growing with N. |
| The warm-up is not counted | A cell with `--warmup` equal to `--duration` answers about twice as many requests as it reports. |
| Repeats are kept | `--concurrency 8,8,16` writes six rows for three classes and six distribution files, `…-c8-1.csv`, `…-c8-2.csv`, `…-c16-1.csv`. |
| The index is never measured incomplete | Delete half the documents and run with `--no-index-build`; it must refuse, naming both counts, before any query. |
| An unreadable probe is refused | Point `--vs-url` at a dead port; it must refuse naming the endpoint, not rebuild and not measure. |
| A build resets first | Load a different corpus at the same ids, then run; the final count must be this corpus's, not the sum. |
| Zero-hit cells are not silent | Run a class of nonsense terms; stderr says so, `zero_hit_queries == queries`, and the exit code is non-zero. |
| `--fetch-documents` is refused on the BM25 arm | `scyllasearch --interface vector-store --fetch-documents` must fail on its flags, before it connects. |
