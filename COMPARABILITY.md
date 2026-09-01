# Comparability — what is actually being compared

The obvious objection to this benchmark: ScyllaDB plus vector-store is a
database *and* a search engine, while OpenSearch is a search engine. Comparing
them looks like comparing two things that do different amounts of work.

This document is the answer. It exists because the objection is reasonable,
will be raised from the audience, and a P99 CONF talk that cannot answer it
loses the room.

## First, a partial correction to the premise

OpenSearch is also a document store. In this benchmark's configuration
(`opensearch/index-config.json`) `_source` is **not** disabled, so OpenSearch
durably stores every document body in its Lucene stored fields, plus a
translog for crash recovery. The `"_source": false` in `ftsbench/engines.py`
is a *query-time* flag meaning "do not return the document body in results" —
it does not stop OpenSearch storing it.

So both sides of this benchmark do the same two jobs:

- durably store the full document text
- maintain a full-text inverted index over it

That makes the comparison substantially more symmetric than "database plus
search engine versus search engine" suggests. The asymmetries that remain are
real, but they are specific, and they are listed below rather than waved away.

## The asymmetries that actually remain

| Dimension | ScyllaDB + vector-store | OpenSearch | Advantage |
|---|---|---|---|
| Document durability | SSTables | Lucene stored fields + translog | neutral |
| Index durability | in-RAM, rebuilt by full table scan on restart | on-disk segments, survives restart | OpenSearch |
| Index residency | must fit in RAM | disk-backed, page-cached on demand | OpenSearch (capacity ceiling) |
| Copies of the text | SSTable on disk, plus tokens in Tantivy RAM | source and index in the same segment files | OpenSearch (smaller footprint) |
| Processes / clusters | 2 | 1 | OpenSearch |
| Index sync | built into the product (CDC) | you build and operate it | ScyllaDB |
| Trusted as source of truth | yes | usually not, in practice | ScyllaDB |
| Query surface | CQL, M1 restrictions (mandatory LIMIT <= 1000, no extra WHERE) | full query DSL | OpenSearch |

Note the advantage column is genuinely mixed. Anything that reads as a clean
sweep for either side is a sign the setup is wrong.

### Index size is not a comparable number, and the harness does not pretend it is

`store_size_bytes` comes from OpenSearch's `_stats` and has no counterpart: the
vector-store's status endpoint reports a document `count` and a status, nothing
about size, because the Tantivy index is in RAM rather than a set of files to
measure. `build_report` defaults the field to 0 when the sampler omits it, which
would read as "the ScyllaDB index occupies nothing" if it were ever put in a
table beside OpenSearch's figure. It is not: the rendered C1 write-up carries
the `merges.*` block only for the configurations that actually reported one.

So there is no C1 index-size comparison, by design. The closest honest
comparison is **C4**, which measures cgroup v2 anon RSS for every container on
both sides — the same quantity, measured the same way, and it captures the
in-RAM index precisely because that is where the ScyllaDB index lives.
`SIZING.md` covers the estimation side separately.

## Two valid framings, and what each one licenses

### Framing A — search-layer comparison (what is currently scaffolded)

One box runs ScyllaDB plus vector-store; the other runs OpenSearch.

- Answers: *how fast is each engine at full-text queries over identical data
  with an identical analyzer?*
- The read path is genuinely comparable: a query lands on an inverted index on
  both sides and returns a ranked top-k.
- It is weaker on the write and resource path, because the ScyllaDB side is
  additionally performing durable base-table writes and a CDC hop that the
  OpenSearch side is not doing in the same shape, and because "OpenSearch
  alone" is not an architecture most teams actually deploy.

### Framing B — stack comparison

One box runs ScyllaDB plus vector-store; the other runs ScyllaDB plus a
CDC/dual-write pipeline plus OpenSearch.

- Answers: *what does the real production architecture cost, end to end?*
- Both sides then have ScyllaDB as the source of truth, so the only variable
  is the search layer and who owns the synchronisation. That isolates exactly
  the claim the talk is built on.
- This matches the architecture story already in the talk document ("App ->
  Database -> CDC/Kafka/dual-write pipeline that you build and operate ->
  OpenSearch").
- Cost: somebody has to build the CDC-to-OpenSearch pipeline — which is the
  very thing the talk describes as "a full system by itself" — plus more
  hardware.

## Recommended position

1. **Use Framing A for the query charts.** C5, C6, C7 and C8 are fair as
   scaffolded. A query hits an inverted index on both sides; the extra
   database role does not meaningfully subsidise or penalise the read path.
2. **Do not claim an ingest or resource win from Framing A without stating
   the work asymmetry on the slide.** "ScyllaDB ingests slower" is not a
   finding if ScyllaDB is also writing a durable base table and a CDC log.
3. **Add Framing B for the write-path and resource charts if budget allows.**
   That is where the talk's actual thesis lives, and it is the version of the
   comparison that cannot be attacked.
4. **Resource footprint (C4) will favour OpenSearch, and that is fine.** Two
   processes and two copies of the data cost more memory. The talk already
   commits to showing the in-RAM index cost honestly; this is that commitment
   arriving.

## Per-chart verdict

| Chart | Framing A alone | Requirement |
|---|---|---|
| C1 index build throughput | with disclosure | state both write paths: ScyllaDB side = base-table write + CDC + Tantivy index; OpenSearch side = translog + segment build + merges |
| C2 time until searchable | with disclosure | same as C1 |
| C3 write p99/p999 during ingest | with disclosure | this is the headline write chart, so the disclosure is load-bearing, not a footnote |
| C4 resource usage | with disclosure | ScyllaDB side is structurally higher; say why before the audience asks |
| C5 latency by percentile | fair as-is | none |
| C6 per-class p50/p95/p99 | fair as-is | none |
| C7 QPS vs p99 sweep | fair as-is | none |
| C8 freshness | fair as-is | both measure write-to-searchable: CDC lag vs refresh_interval |

## What this means for hardware

Framing A needs 2 boxes. Framing B needs a third deployment for the
OpenSearch-side ScyllaDB, or accepts colocating it with OpenSearch and
discloses that. Sizing for the ScyllaDB-plus-vector-store box is derived in
`SIZING.md`.

## Open decision

Whether to run Framing B at all is not yet decided. Running only Framing A is
defensible **provided** the write-path and resource charts carry the
disclosure above. Running neither honestly — that is, presenting Framing A
ingest numbers as a clean win — is the failure mode this document exists to
prevent.
