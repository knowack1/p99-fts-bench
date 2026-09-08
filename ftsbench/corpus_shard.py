"""Split the corpus into contiguous byte ranges, one per worker process.

The write path is moving to N worker processes so that client CPU stops being
the ceiling (`results/client-model-2026-09-08/README.md`), and each process must
load a disjoint part of the corpus. Disjoint is the whole contract: two workers
sharing any document write identical primary keys, so the base-table write count
still looks right while the index count silently means something else — the
defect `tools/sharded_build_rate.sh` warns about in its header.

**Contiguous byte ranges, not line-modulo.** Order of insertion does not affect
the measurement — BM25 term statistics are collection-wide, and the invariant
that matters is every document exactly once with the same set per engine, which
either scheme preserves. Byte ranges win on cost: no line counting over a 35 GB
corpus at startup, and no re-shard step when the worker count changes. They are
balanced here because the corpus is: the existing two-way split is 0.17% apart
by bytes over 600,000 documents a side.

Uncompressed only, deliberately. A `.gz` corpus cannot be seeked to a proportional
offset, and the alternatives are both bad — sequentially skipping to the shard's
first line costs a full decompress per worker, and falling back to line-modulo
would silently change the sharding scheme under a caller who asked for
contiguous. The campaign corpus is plain `.jsonl`; anything else refuses.
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any


class UnshardableCorpus(RuntimeError):
    """Raised rather than silently falling back to a different scheme."""


def _refuse_compressed(path: str) -> None:
    if path.endswith((".gz", ".bz2")):
        raise UnshardableCorpus(
            f"{path} is compressed and cannot be split by byte offset; "
            "decompress it, or run a single worker")


def _line_start_at_or_after(handle, offset: int) -> int:
    """The offset of the first whole line at or after `offset`.

    Every boundary is computed this way, by both the shard that ends there and
    the shard that begins there, so the two agree exactly and no line is
    duplicated or dropped between them.
    """
    if offset <= 0:
        return 0
    handle.seek(offset)
    handle.readline()
    return handle.tell()


def shard_bounds(path: str, shard: int, shards: int) -> tuple[int, int]:
    """Byte range `[start, end)` of whole lines belonging to this shard."""
    if not 0 <= shard < shards:
        raise ValueError(f"shard {shard} out of range for {shards} shards")
    _refuse_compressed(path)
    size = os.path.getsize(path)
    with open(path, "rb") as handle:
        start = _line_start_at_or_after(handle, size * shard // shards)
        if shard + 1 == shards:
            return start, size
        return start, _line_start_at_or_after(handle, size * (shard + 1) // shards)


def read_shard(path: str, shard: int, shards: int,
               max_docs: int = 0) -> Iterator[dict[str, Any]]:
    """Documents of one shard, in corpus order, capped at `max_docs`.

    `max_docs` is per shard, not per run: the driver caps the whole run by
    dividing its budget across workers, and a cap applied here to the total
    would give worker 0 the entire budget and the rest nothing.
    """
    start, end = shard_bounds(path, shard, shards)
    if start >= end:
        return
    with open(path, "r", encoding="utf-8") as handle:
        handle.seek(start)
        yielded = 0
        while handle.tell() < end:
            line = handle.readline()
            if not line:
                return
            if not line.strip():
                continue
            yield json.loads(line)
            yielded += 1
            if max_docs and yielded >= max_docs:
                return


def split_budget(total: int, parts: int, index: int) -> int:
    """One part's share of an integer budget, remainder included.

    Used for both the document cap and the concurrency budget, because both are
    a whole-run number the workers divide between them and both are compared
    against exactly afterwards. A plain `total // parts` leaves up to
    `parts - 1` unallocated: for documents that means the sweep's completeness
    gate sets a point aside as truncated for an arithmetic reason rather than an
    engine one, and for concurrency it means the rung labelled c=64 offered 63.
    """
    if not total:
        return 0
    return total // parts + (1 if index < total % parts else 0)
