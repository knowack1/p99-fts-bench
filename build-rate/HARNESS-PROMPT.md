# HARNESS-PROMPT.md — the specification the three crates are built to

`core/`, `scylla/` and `opensearch/` are a benchmark, and a benchmark is only
worth what its specification is worth. This is that specification, written as
one prompt: everything the harness had to be told, including the decisions that
are not obvious from the code and would be re-litigated by anyone regenerating
it. Run it against an empty `bench/build-rate/` and you get this tree.

**What this is not.** It is not a transcript. The harness was started on
2026-09-10 from two short messages, reviewed, fixed, and then refactored over
four more days onto the shared `core` crate the two binaries now sit on. This
file is what those messages should have said — reconstructed from the tree that
exists, so the specification can be read, checked and argued with in one place
rather than inferred from 11k lines of Rust. Slide 07a shows it verbatim.

**What it deliberately leaves to the implementer.** Crate versions, module
layout inside each crate, error types, and every name that is not a CSV column,
a CLI flag or a trait. Those are settled by the code; pinning them here would
make this file a second source of truth that drifts.

---

```text
> Build a build-rate benchmark harness in Rust, from scratch, in
  bench/build-rate. One question, two engines: how fast can a
  client fill a full-text index, and how fast does the index
  build behind it? Never answer the second with the first — a
  write ack is not a document in the index. Poll the index, and
  keep polling after the last insert: the build is not over when
  the client stops talking.

LAYOUT
  core/        lib build-rate-core — sweep, watch, report, CSV
  scylla/      bin scyllarate — prepared CQL INSERT, vector store
  opensearch/  bin osrate     — _bulk, _stats
  charts/      two matplotlib renderers over the CSVs below
  Not a cargo workspace. Each binary keeps its own Cargo.lock,
  because its build.rs reads that lock to stamp the linked driver
  version into every CSV header; one shared lock would move one
  engine's recorded version when the other's driver moved. core
  is a path dependency of both.

THE SEAM — an engine supplies four things, core owns the rest
  trait WorkItem    { fn docs(&self) -> u64; }
  trait Inserter    { type Item: WorkItem;
                      fn insert(&self, item) -> impl Future<
                        Output = Result<Accepted>> + Send; }
  trait LevelSource { fn open(&self) -> BoxFuture<Arc<Inserter>>; }
  trait IndexProbe  { fn read(&self) -> BoxFuture<IndexState>;
                      fn endpoint(&self) -> &str;
                      fn settle_hint(&self) -> BoxFuture<bool>; }
  Item is an associated type, not one shared batch type: wrapping
  every single-document CQL insert in a Vec would put a malloc on
  the hottest path of a loader whose whole job is to out-run the
  engine. LevelSource is a trait, not a closure, because the CQL
  half must re-prepare after the table it prepared against is
  dropped. read() never fails — a failure is a reading:
  enum IndexState { Absent, Unreadable(why), Present(reading) }
  struct IndexReading { docs, accepted: Option<u64>, status, ready }
  Two counts, because on one engine they genuinely differ: docs is
  what a search would find, accepted is what the engine took in.

THE SWEEP — one CSV row per rung, on exactly one of two ladders
  --concurrency is a list: CLOSED LOOP, N requests in flight, a
  bounded async channel, N tokio tasks, each awaiting its reply
  before taking the next item. Documents in flight is
  concurrency * batch_size — say it out loud.
  --target-rate is a list of docs/s: OPEN LOOP, and then
  --concurrency is ONE number and it is a cap. The producer, which
  already runs on spawn_blocking, releases document i at
  origin + i/rate; behind schedule it never skips, because the
  backlog is the finding. Closed loop is the same path with no due
  time, so the request's own start stands in and latency collapses
  to service time — one expression, not a second path. Both
  ladders at once is refused: a matrix costs their product and
  reconfounds the axis.
  Concurrency is not a shared unit across a bulk API and a per-row
  one; a document per second is. That is why the rate axis exists.
  Capacity is queue_depth * concurrency requests (10 by default),
  so the producer cannot pull the corpus into memory ahead of the
  workers. The producer runs on spawn_blocking; the workers live
  in a JoinSet, so a Ctrl-C mid-level aborts them instead of
  leaving them writing into a finished run.
  --tokio-workers is a separate knob from --concurrency: cores
  serving the requests, not requests outstanding. Both refuse 0 —
  worker_threads(0) panics rather than failing.
  Per level: reopen the corpus from line one, reopen the inserter,
  reset the index. Repeats are kept — 8,8,16 runs three levels.
  Only a request where every item landed contributes a latency
  sample; a request that failed whole counts every document it
  carried as an error. First error is the earliest by clock, not
  by join order.

THE WATCH — the number this harness exists for
  Before the level, read what the index already holds and subtract
  it. An unreadable poll there is a hard error: taken as zero, the
  level credits itself with everything already indexed and reports
  a complete, plausible, wrong number.
  While it runs, poll every --index-interval and write one row per
  reading into that level's series file.
  After the last insert, settle: poll until docs reaches what was
  submitted, or nothing is accepted for --index-idle-timeout, or
  --index-settle-timeout expires. Idle is measured on accepted,
  never on searchable — a searchable count sits still between
  refreshes, and an idle clock watching it would call an ordinary
  pause a finished build.
  settle_hint (a _refresh) fires at most once, only when the
  engine has accepted everything and published none of it, and
  never mid-build: a harness that forces the engine's hand is no
  longer measuring it. A level whose last documents arrived that
  way reports index_status=refreshed, because that is not what the
  configured refresh policy would have delivered.
  The rate spans first insert to the moment the index stopped, not
  the part the client was talking for. Not settled means the rate
  is a floor — say so on stderr and in the row.

OUTPUT — one schema, both engines, blank cells never zeros
  22 columns: concurrency,docs,errors,wall_s,docs_per_s,p50_ms,
  p99_ms,batch_size,requests,failed_requests,index_docs,
  index_docs_per_s,index_lag_docs,index_settle_s,index_settled,
  index_status,engine,target_docs_per_s,achieved_offered_ratio,
  queue_p99_ms,in_flight_peak,generator_saturated
  The last five are the rate ladder's and are BLANK on a
  concurrency-ladder row — a zero offered rate would plot at the
  origin of an axis it is absent from. in_flight_peak is the one
  that separates "the engine could not keep up" from "the cap
  bound": short with the peak at --concurrency is the harness, and
  the point is void. What p50_ms/p99_ms are measured FROM changes
  with the ladder, which redefines columns rather than adding
  them, so it is a header fact: latency_basis=intended_start on a
  rate ladder, service on a concurrency one.
  --samples-dir adds the per-second series behind those averages,
  one file per level, c<conc>-<n>.csv, repeats never overwriting:
  level,concurrency,t_s,docs_submitted,submit_docs_per_s,
  docs_indexed,index_docs_per_s,index_status,docs_accepted,
  accepted_docs_per_s
  Both carry the same '# key=value' preamble — engine build,
  driver version, analyzer, every flag the run was given. Columns
  are APPENDED, never inserted. A column an engine cannot fill is
  blank: a zero p99 plots as the best point on the curve and a
  zero build rate is a finding. `engine` tells the halves apart
  in one file. Flush every row; open --out before the first insert.

RESET — destructive by default, and gated
  ScyllaDB: DROP KEYSPACE, then CREATE KEYSPACE / TABLE / CUSTOM
  INDEX ... USING 'fulltext_index'. Wait for the old index to
  disappear from the vector store, then for the new one to reach
  SERVING at 0 documents. Two gates, because a create that raced
  the drop hands this level the last level's documents.
  OpenSearch: DELETE then PUT from the mapping, same two gates,
  plus one analyzer probe before any document — an analyzer that
  differs is a comparison of tokenizers, not of engines.
  Each gate names what it waited for and what it last saw. Warn,
  naming the keyspace, before anything is dropped.

RUN
  Ctrl-C ends the ladder and keeps the levels measured so far;
  the abort line says where they went. Exit non-zero if a point
  had errors or the sweep aborted. Echo a summary table at the
  end, marking every level whose index never settled.

DISCIPLINE
  Tests beside each module as <name>_tests.rs, pulled in with
  #[path]. Fakes for both engines, so the sweep, the settle loop
  and the reset gates are testable with nothing running; live
  tests #[ignore]d. No comment that restates the code — extract a
  named function. fmt and clippy clean in all three crates.
```

---

## Checking a regenerated tree against this

The prompt is specific about the things that are testable from the outside, and
those are the things to check first:

| Claim in the prompt | How to falsify it |
|---|---|
| One schema, both engines | `head -1` the two point CSVs after the `#` block — 17 identical column names, in order. |
| Blank cells, never zeros | Run with `--no-index-watch` (Scylla) or without `--index-watch` (OpenSearch); the six index columns must be empty, not `0`. |
| Closed loop at N | `concurrency * batch_size` documents in flight, and no more: the channel is bounded, so peak RSS must not track corpus size. |
| The build is not over when the client stops | A level's `index_settle_s` is non-zero, and the series file has readings after `submit_docs_per_s` reaches 0. |
| Idle is measured on accepted | At `refresh_interval: -1` a level must still finish, and report `index_settled=false` or `index_status=refreshed` — never a silent zero-document build. |
| Repeats are kept | `--concurrency 8,8,16` writes three rows and three series files, `c8-1.csv`, `c8-2.csv`, `c16-1.csv`. |
| Reset is gated | Point the reset at a dead vector store; it must time out naming the endpoint and what it last saw, not proceed. |
| Exit code means something | A run with a non-zero `errors` column must exit non-zero. |
