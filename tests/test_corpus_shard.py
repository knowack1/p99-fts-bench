"""Contiguous corpus sharding for the multi-process write path.

The contract is disjointness. Two workers sharing any document write identical
primary keys, so the base-table write count still looks right while the index
count means something else — `tools/sharded_build_rate.sh` calls that out in its
header, and it is the reason these tests check boundaries rather than counts
alone.
"""
import json

import pytest

from ftsbench import corpus_shard


def write_corpus(path, count, width=0):
    """`width` pads documents so shard boundaries fall mid-line, which is where
    an off-by-one between the shard that ends at a boundary and the one that
    begins there would show up."""
    with open(path, "w", encoding="utf-8") as handle:
        for i in range(count):
            handle.write(json.dumps(
                {"id": i, "uuid": f"u{i}", "title": f"t{i}",
                 "text": "x" * (width + i % 7)}) + "\n")
    return str(path)


def ids(path, shard, shards, max_docs=0):
    return [d["id"] for d in corpus_shard.read_shard(path, shard, shards, max_docs)]


@pytest.mark.parametrize("shards", [1, 2, 3, 4, 7, 8])
def test_every_document_appears_in_exactly_one_shard(tmp_path, shards):
    path = write_corpus(tmp_path / "c.jsonl", 200, width=40)
    seen = [i for s in range(shards) for i in ids(path, s, shards)]
    assert sorted(seen) == list(range(200))
    assert len(seen) == len(set(seen)), "a document landed in two shards"


@pytest.mark.parametrize("shards", [2, 3, 5])
def test_shards_are_contiguous_and_in_corpus_order(tmp_path, shards):
    """Contiguous, not modulo — a caller who asked for contiguous must not get
    an interleaved split, because the two differ in how balanced the shards are
    on a corpus whose document sizes drift."""
    path = write_corpus(tmp_path / "c.jsonl", 120, width=30)
    per_shard = [ids(path, s, shards) for s in range(shards)]
    for chunk in per_shard:
        assert chunk == sorted(chunk)
    flattened = [i for chunk in per_shard for i in chunk]
    assert flattened == sorted(flattened), "shards are not in corpus order"


def test_boundaries_agree_between_neighbouring_shards(tmp_path):
    """The end of shard k and the start of shard k+1 are computed by the same
    function, so a line split by a byte boundary belongs to exactly one of
    them."""
    path = write_corpus(tmp_path / "c.jsonl", 97, width=13)
    for shard in range(4 - 1):
        _, end = corpus_shard.shard_bounds(path, shard, 4)
        next_start, _ = corpus_shard.shard_bounds(path, shard + 1, 4)
        assert end == next_start


def test_the_last_shard_reaches_the_end_of_the_file(tmp_path):
    """Rounding down on the final boundary would drop the corpus's tail, and a
    run short by a handful of documents fails the completeness gate for a
    reason that has nothing to do with the engine."""
    path = write_corpus(tmp_path / "c.jsonl", 53, width=9)
    assert ids(path, 4, 5)[-1] == 52


def test_more_shards_than_documents_yields_empty_shards_not_errors(tmp_path):
    """The ladder's low rungs can ask for more workers than a smoke corpus has
    lines; an empty shard is correct, a crash is not."""
    path = write_corpus(tmp_path / "c.jsonl", 3)
    collected = [ids(path, s, 8) for s in range(8)]
    assert sorted(i for chunk in collected for i in chunk) == [0, 1, 2]


def test_a_per_shard_cap_limits_that_shard_only(tmp_path):
    path = write_corpus(tmp_path / "c.jsonl", 100, width=20)
    assert len(ids(path, 0, 4, max_docs=5)) == 5
    assert len(ids(path, 1, 4, max_docs=5)) == 5


def test_the_document_budget_splits_without_losing_the_remainder(tmp_path):
    """`max_docs // shards` would leave up to shards-1 documents unloaded, and
    the sweep's gate compares the indexed count against the cap exactly."""
    assert sum(corpus_shard.docs_per_shard(1_000_000, 3, s) for s in range(3)) == 1_000_000
    assert sum(corpus_shard.docs_per_shard(101, 4, s) for s in range(4)) == 101


def test_an_uncapped_budget_stays_uncapped_per_shard():
    assert corpus_shard.docs_per_shard(0, 4, 0) == 0


def test_a_compressed_corpus_is_refused_rather_than_silently_resharded(tmp_path):
    """Falling back to line-modulo would change the sharding scheme under a
    caller who asked for contiguous, and nothing in the artifacts would say so."""
    with pytest.raises(corpus_shard.UnshardableCorpus, match="compressed"):
        corpus_shard.shard_bounds(str(tmp_path / "c.jsonl.gz"), 0, 2)


def test_an_out_of_range_shard_is_refused(tmp_path):
    path = write_corpus(tmp_path / "c.jsonl", 10)
    with pytest.raises(ValueError, match="out of range"):
        corpus_shard.shard_bounds(path, 4, 4)
