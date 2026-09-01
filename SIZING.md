# Hardware sizing — measured basis

How the benchmark hardware size was derived, and from what evidence. This
exists so the instance choice on the methodology slide is defensible rather
than a guess. Numbers here are **measured locally**, not on the final
benchmark hardware; see "Caveats" for what that does and does not license.

## What was measured

`tantivy-ram/` is a standalone harness that replicates the vector-store's
Tantivy configuration exactly, as read from
`vector-store/crates/vector-store/src/fts_index/tantivy.rs`:

- `Index::create_in_ram()` — the index is RAM-resident, no disk backing
- `body`: text field, `IndexRecordOption::WithFreqsAndPositions` (positions
  are required for phrase queries and are the expensive part), **not**
  `STORED`
- `primary_id`: `u64`, `INDEXED | STORED`
- analyzer: `SimpleTokenizer` + `LowerCaser` + English `StopWordFilter`
  (no stemming — matches M1)

Running it against the frozen simplewiki corpus:

| Metric | Value |
|---|---|
| Docs indexed | 270,269 |
| Body text indexed | 426,076,366 B (0.43 GB) |
| Tantivy `space_usage().total()` | ~179 MB |
| **Index / source-text ratio** | **~0.42** (0.40–0.44 across runs) |

The ratio was stable across four runs with writer memory budgets of 32 / 64 /
128 / 256 MB per thread — `space_usage` stayed in the 171–186 MB band while
process RSS swung 381–758 MB. That separation matters: **`space_usage` is the
structural index size that must be resident; RSS additionally carries writer
buffers, merge transients and allocator fragmentation**, which are partly
fixed cost and partly tunable, and which glibc does not return to the OS.

## Extrapolation to full enwiki

Measured from the frozen dump listing and a local decompression:

- simplewiki bz2 expansion: **×5.79** (559 MB → 3.24 GB)
- article text is only **13.2%** of the decompressed dump (the rest is
  CirrusSearch metadata — categories, headings, links, templates — which
  `prepare_corpus.py` discards)

Applying both to enwiki's known 40.75 GB compressed size:

| Quantity | Value |
|---|---|
| enwiki compressed (measured) | 40.75 GB |
| enwiki decompressed (estimated) | ~236 GB |
| enwiki body text (estimated) | ~31 GB |
| **Tantivy index RAM (estimated)** | **~13 GB** |

Sensitivity, because the text estimate is the weakest link:

| If enwiki text is | Index needs |
|---|---|
| 25 GB | 10.5 GB |
| 31 GB (estimate) | 13.0 GB |
| 40 GB | 16.8 GB |
| 50 GB | 21.0 GB |

## Box sizing (vector-store colocated with ScyllaDB)

| Component | GB |
|---|---|
| Tantivy index structure | 13 |
| Writer / merge / allocator overhead (×2 on index) | 26 |
| ScyllaDB base process (colocated) | 16 |
| OS + page cache | 4 |
| **Total working set** | **~46** |
| **With 30% headroom** | **~60** |

**Recommendation: `im4gn.4xlarge`** (16 vCPU, 64 GB RAM, 7.5 TB NVMe) for
both boxes — the ScyllaDB+vector-store node and the OpenSearch node, same
type on each side to satisfy the hardware-parity fairness rule.

`im4gn.8xlarge` (32 vCPU / 128 GB) is the safe upgrade if the enwiki text
size lands at the high end of the sensitivity table, or if the colocated
ScyllaDB working set turns out larger than the 16 GB assumed here.

`im4gn.2xlarge` (32 GB) is **not** viable for full enwiki — it is below the
working set before any headroom.

## Why undersizing is dangerous here, not just slow

`vector-store/crates/vector-store/src/memory.rs` computes its limit from
**total system memory** (or the cgroup limit) minus a safety buffer (1%, min
200 MB), and watches system-wide used memory. When the limit is reached,
`can_allocate_memory()` returns false and the FTS index actor **silently skips
adding documents** — it logs an error, but indexing continues and the index
simply ends up incomplete.

An undersized box therefore yields a **partially-indexed corpus that still
answers queries**, producing recall and latency numbers that look plausible
and are wrong. Mitigation: after every load, assert that the index's
`num_docs` equals the corpus document count before trusting any measurement.

## Caveats

1. **Architecture**: measured on x86_64, target is ARM Graviton. Index data
   structures are 64-bit on both, so the ratio should transfer for capacity
   planning, but it is an estimate — this is part of why headroom is not
   trimmed to the bone.
2. **Corpus**: the ratio comes from simplewiki, which has shorter articles
   (avg 1,576 B) and a smaller vocabulary than enwiki. Term-dictionary growth
   is sublinear (Heaps' law) while positions grow linearly, and positions
   dominate under `WithFreqsAndPositions`, so the ratio should hold roughly —
   **but this has not been validated against real enwiki text.** Validating it
   against a single enwiki shard is the cheapest way to de-risk the number and
   is still open.
3. **Commit cadence**: the harness batches commits; the production
   vector-store commits after every document (`handle_add_document`). This
   affects build-time transients and segment counts, not the final structural
   size.
4. **ScyllaDB's own 16 GB** in the table is an assumption, not a measurement.
5. These numbers size the machine. They are **not** benchmark results and must
   not appear on a results chart.

## Reproducing

```bash
cd bench
make download WIKI=simplewiki
make corpus WIKI=simplewiki
cd tantivy-ram && cargo build --release
./target/release/tantivy-ram ../data/corpus.jsonl 0 25000 256
```
