# Putting OpenSearch's index in RAM (ScyllaDB-parity experiment)

ScyllaDB FTS splits storage across two services: documents durable on disk in
Scylla's SSTables, and the Tantivy index RAM-resident only
(`Index::create_in_ram()`, lost on restart, rebuilt by a base-table scan). This
records what the equivalent is for OpenSearch, what part of it is achievable,
and what it costs.

## What is not available

**There is no in-RAM store type.** `index.store.type` on OpenSearch 3.8 accepts
`fs`, `niofs`, `mmapfs`, `hybridfs` and `simplefs`; `memory` and `ram` are
rejected with `Unknown store type` — they were removed upstream in Elasticsearch
5.0 and OpenSearch inherited that.

**Documents and index cannot be split across paths.** Lucene writes stored
fields (`.fdt`) and the inverted index (`.tim`/`.doc`/`.pos`) into the *same*
segment files in the *same* shard directory. `path.data` can list several paths
but a shard is assigned to one path whole, never split by file type. So
"documents on disk, index in RAM" is not expressible inside one OpenSearch
index. What *is* expressible is an index-only OpenSearch whose files are
RAM-resident — which is exactly the vector-store's role.

## The configuration

Two independent changes, both wired into the harness:

| Knob | How | Effect |
|---|---|---|
| Index in RAM | `OS_RAM_INDEX=1` — adds `docker/docker-compose.opensearch.ramindex.yml`, a tmpfs over `/usr/share/opensearch/data`, sized from `OS_RAM_INDEX_SIZE` (default 2 GiB, matching `VECTOR_STORE_MEMORY_LIMIT`) | segment files never touch disk |
| No document store | `OS_INDEX_CONFIG=index-config-ramindex.json` — same analyzer, plus `_source: {enabled: false}` | index carries postings and ids only, like Tantivy's schema |

```sh
make os-down OS_RAM_INDEX=1
make os-up os-wait os-relax-watermarks OS_RAM_INDEX=1
make os-index OS_REFRESH=3s OS_INDEX_CONFIG=index-config-ramindex.json
make os-load INGEST_CONCURRENCY=16
```

`OS_RAM_INDEX=1` must be passed to *every* compose target in a run. With it on
`up` but off `down`, the two act on different service definitions.

The tmpfs needed `mode: 01777`: Docker mounts a tmpfs root-owned at mode 755 and
the image runs as uid 1000, so without it OpenSearch dies at startup with
`AccessDeniedException` on its own data directory.

## Measured, 270,269-document simplewiki

| | on disk, `_source` on (the sweep's config) | in RAM, `_source` off |
|---|---|---|
| Where segments live | `opensearch-data` volume | tmpfs, `/usr/share/opensearch/data` |
| Total after force-merge | 413 MB | **138 MB** |
| `.fdt` stored fields | 119.1 MB | **1.5 MB** |
| `.pos` positions | 33.1 MB | 72.8 MB |
| `.doc` postings | 22.6 MB | 51.3 MB |
| `.tim`/`.tip` terms | 8.4 MB | 14.6 MB |
| translog | on disk | 8 KB after flush (in RAM) |
| load rate | 23,710 docs/s | 23,727 docs/s |

The per-extension figures are not comparable row by row between the two columns
— the left column was 6 segments with much of the data still inside compound
`.cfs` files, the right column is one force-merged segment where every file
type is broken out. The comparable numbers are the totals.

Against the ScyllaDB side, from `SIZING.md`'s standalone Tantivy harness on the
same corpus with the same analyzer and positions on:

| | index size |
|---|---|
| OpenSearch, index only, RAM-resident | **~138 MB** |
| Tantivy `space_usage().total()` | **~179 MB** |

So the two inverted indexes are within ~30% of each other and OpenSearch's is
the smaller one. That is a much fairer comparison than C4 currently makes, where
OpenSearch's recorded 442 MB includes 119 MB of `_source` that Tantivy's schema
deliberately does not hold, and the ScyllaDB `index_size_bytes` was recorded as
`0`.

## Three findings worth carrying forward

**1. `_source: false` does not remove the stored-fields cost until a merge.**
Immediately after load the index still held **114 MB** of `.fdt` despite
`_source` being disabled, because OpenSearch writes `_recovery_source` in its
place for operation-based peer recovery. Force-merging to one segment pruned it
to 1.5 MB. **Any index-size measurement taken without a flush and force-merge
overstates an OpenSearch `_source: false` index by ~114 MB** — nearly the same
magnitude as the `_source` it was supposed to remove.

**2. Throughput did not change: 23,727 vs 23,710 docs/s.** Moving the index off
disk bought nothing measurable, which is consistent with the finding in
`results/build-rate-sweep-2026-08-26/WHY-SCYLLA-IS-SLOWER.md` that this harness
is client-bound at ~1 GIL core. Until the loader is fixed, this configuration
cannot show whether a RAM index helps OpenSearch.

**3. This is a write-path parity device, and only that.** With `_source: false`
OpenSearch stores no documents, so "documents on disk" has to be served by
something else — which is the sync-pipeline architecture S2/S3 argue against.
But that extra database is a **write-path and operational cost, not a read-path
one**, exactly as `COMPARABILITY.md` §"Recommended position" item 1 already
states. Both engines answer a query from an inverted index and return a ranked
top-k; the database is not in that path.

The bench's own read path confirms it is symmetric today. Neither side fetches
document text:

| | query | returns |
|---|---|---|
| `OpenSearchEngine` | `"_source": false`, `track_total_hits: false` | `hit["_id"]` |
| `ScyllaEngine` | `SELECT article_id ... WHERE BM25(...) > 0 ORDER BY BM25(...)` | `row.article_id` |

So use this configuration for storage-footprint and write-path work only. **Do
not build the query charts (C5/C6/C7) on a `_source: false` index** — not
because the missing database matters, but because a smaller index is a mild
cache-locality advantage, and mixing it against the `_source`-enabled numbers
already measured would be comparing two different OpenSearch indexes.

### The read-path question this does raise, which nothing measures yet

The symmetry above holds because both sides return *ids*. A real application
returns document fields, and there the two architectures diverge:

- **ScyllaDB**: coordinator → vector-store for the BM25 hit list → back to the
  coordinator → SSTable reads for the projected columns. Two internal hops, and
  the document read comes off disk.
- **OpenSearch with `_source` on**: the stored fields sit in the *same* segment
  files that were just searched, very likely already in page cache. One hop.

That asymmetry would plausibly favour **OpenSearch**, and it is untested — the
harness has never issued a document-returning query on either side. If the talk
wants to claim anything about end-to-end search latency rather than index-lookup
latency, this is the gap to close first.

## When not to use it

- **Any durability, restart or bootstrap comparison (C2).** The tmpfs index dies
  with the container, which matches the vector-store's non-durability — so it
  removes exactly the disadvantage C2 exists to show. Use the disk config there.
- **Anything needing document retrieval**: `_source: false` breaks fetching
  documents, reindex, update-by-query, and highlighting from source. The bench's
  read path is unaffected — `ftsbench/engines.py` already sends
  `"_source": False` and reads only `hit["_id"]`, verified end to end against
  this index.
- **Watch the memory arithmetic.** tmpfs pages count against the container's
  limit, so `OS_MEM_LIMIT` must exceed `OS_HEAP` + `OS_RAM_INDEX_SIZE`.
  Currently 8 GiB > 4 GiB + 2 GiB, which holds — but a bigger corpus needs both
  the tmpfs and the container limit raised, or the index silently hits ENOSPC.
