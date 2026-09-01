# Frozen Wikipedia corpus inputs

Single source of truth for "what data did this benchmark run against". Every
corpus build for this benchmark must use exactly these inputs; if the freeze
needs to move (newer snapshot, new wiki), update this file in the same commit
as the script/Makefile change and say why.

## Source

Wikimedia's legacy CirrusSearch dumps
(`https://dumps.wikimedia.org/other/cirrussearch/`) are **deprecated and no
longer produced** — see the `DEPRECATED.txt` notice in that directory. This
benchmark uses the replacement, actively-maintained source:

```
https://dumps.wikimedia.org/other/cirrus_search_index/<DATE>/index_name=<wiki>_content/
```

Each wiki's content is sharded into one or more numbered `.json.bz2` files
(`<wiki>_content-<DATE>-NNNNN.json.bz2`), plus a `_SUCCESS` marker confirming
the dump finished publishing.

## Frozen snapshot

| Field | Value |
|---|---|
| Dump date | `20260816` |
| Frozen on | 2026-08-18 |
| `enwiki_content` shards | 65 (`00000`–`00064`, ~625 MB each, ~40 GB total) |
| `simplewiki_content` shards | 1 (`00000`, ~560 MB) |
| `_SUCCESS` verified | yes, both wikis |

Download with the pinned date (already the default):

```bash
tools/download_wikipedia.sh simplewiki data 20260816   # smoke run
tools/download_wikipedia.sh enwiki     data 20260816   # full benchmark
```

`tools/download_wikipedia.sh` writes a `sha256sum`-format checksum manifest
next to each wiki's shards
(`data/<DATE>/index_name=<wiki>_content/<wiki>_content-<DATE>.sha256`) —
verify with `sha256sum -c` before trusting a re-download or a copy handed to
someone else for reproduction.

## Prepared corpus (canonical JSONL)

Output of `make corpus` over the frozen shards with the default
`--min-chars 200`; every chart footer cites these numbers. The parallel and
serial `prepare_corpus` paths are byte-identical (asserted by
`tests/test_prepare_corpus.py` and verified against these checksums), so
`--jobs` does not affect the freeze.

| Field | simplewiki | enwiki |
|---|---|---|
| Documents | 270,269 | 8,967,625 |
| Skipped (short/unparsable) | — | 6,172,226 |
| Body-text bytes (`text` UTF-8) | 426,076,366 (~0.43 GB) | 34,295,420,210 (~34.3 GB) |
| `corpus.jsonl` bytes | 456,217,584 | 35,448,823,550 |
| `corpus.jsonl` sha256 | `c1be2adb382985f4e664546370e9016aef84d100afdad0a1eed39f0b691c5149` | `1700bb6c9b2652cf7b248e8caff7bfecc54fd2376e9a75e43379aaa79c50c432` |
| Prepared on | 2026-08-18 (laptop) | 2026-09-01 (`fts-harness`, eu-north-1) |

enwiki / simplewiki scale: **33.2x by documents, 80.5x by body-text bytes**
(the run plan's "73x" walls estimate was linear on an assumed ~31 GB; the
measured 34.3 GB stretches those estimates ~10% upward).

`C1_UNTIL_DOCS` must equal the corpus document count exactly: `270269`
(Makefile default, simplewiki) or `8967625` for any enwiki run.

## Parsing scripts frozen alongside this data

The commit that pins `DUMP_DATE = 20260816` in `Makefile` and
`tools/download_wikipedia.sh` is the frozen version of the parsing pipeline
(`ftsbench/prepare_corpus.py`, `ftsbench/corpus.py`). Any change to corpus
extraction logic (`--min-chars`, uuid assignment, field selection) after this
point must bump this document, not silently change what "the benchmark
corpus" means.

## Still open (tracked in the internal TODO, not this file)

- Hardware freeze (i3en instance type, node count, RF)
- OpenSearch / ScyllaDB tuning publication
- Pinned engine versions (ScyllaDB FTS build, OpenSearch, Tantivy, Lucene)
- Cache-state / run-count (N) policy for chart footers
