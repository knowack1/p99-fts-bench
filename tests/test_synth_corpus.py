"""The synthetic corpus has to be the right SIZE and the right SHAPE.

Size, because the whole point of Phase 0 is a per-document client cost and the
document size is what sets it. Shape, because `ftsbench.corpus`,
`ftsbench.corpus_shard` and both loaders read these lines: a missing `uuid`
would fail the CQL loader at bind time, and ids repeated across shards would let
two workers write the same row while both reported it delivered.
"""
import json

import pytest

from ftsbench import corpus, corpus_shard, opensearch_load, scylla_load
from ftsbench import synth_corpus


def written(path, docs: int, **overrides) -> list[dict]:
    spec = synth_corpus.CorpusSpec(docs=docs, **overrides)
    synth_corpus.write_corpus(str(path), spec)
    return list(corpus.read_corpus(str(path)))


def test_every_line_carries_the_canonical_fields(tmp_path):
    documents = written(tmp_path / "c.jsonl", 20)
    assert len(documents) == 20
    for document in documents:
        assert set(document) == {"id", "uuid", "title", "text"}
        assert document["text"] and document["title"]


def test_the_uuid_is_the_same_derivation_prepare_corpus_uses(tmp_path):
    import uuid

    document = written(tmp_path / "c.jsonl", 3)[0]
    expected = uuid.uuid5(uuid.NAMESPACE_URL,
                          f"wikipedia-page:{document['id']}")
    assert document["uuid"] == str(expected)


def test_a_fixed_size_corpus_hits_the_target_line_length_exactly(tmp_path):
    path = tmp_path / "c.jsonl"
    synth_corpus.write_corpus(
        str(path), synth_corpus.CorpusSpec(docs=50, mean_bytes=3950, sigma=0.0))
    lengths = {len(line) for line in path.read_bytes().splitlines(keepends=True)}
    assert lengths == {3950}


def test_the_mean_line_length_lands_on_enwikis_average(tmp_path):
    path = tmp_path / "c.jsonl"
    stats = synth_corpus.write_corpus(
        str(path), synth_corpus.CorpusSpec(docs=4000, seed=11))[str(path)]
    # Lognormal with sigma 0.6 over 4,000 documents: one standard error of the
    # mean is about 1%, so 4% is a generous band on the mean being preserved.
    assert stats.mean_line_bytes == pytest.approx(
        synth_corpus.ENWIKI_MEAN_LINE_BYTES, rel=0.04)


def test_size_variation_is_heavy_tailed_rather_than_uniform(tmp_path):
    documents = written(tmp_path / "c.jsonl", 500, seed=5)
    sizes = sorted(len(document["text"]) for document in documents)
    assert sizes[-1] > 2 * sizes[len(sizes) // 2]


def test_some_documents_carry_multi_byte_characters(tmp_path):
    documents = written(tmp_path / "c.jsonl", 40)
    text = "".join(document["text"] for document in documents)
    assert len(text.encode("utf-8")) > len(text)


def test_no_multi_byte_characters_when_the_fraction_is_zero(tmp_path):
    documents = written(tmp_path / "c.jsonl", 20, accent_fraction=0.0)
    text = "".join(document["text"] for document in documents)
    assert len(text.encode("utf-8")) == len(text)


def test_the_same_seed_writes_the_same_corpus(tmp_path):
    first = written(tmp_path / "a.jsonl", 30, seed=99)
    second = written(tmp_path / "b.jsonl", 30, seed=99)
    assert first == second


def test_shards_are_disjoint_and_cover_the_whole_budget(tmp_path):
    stem = tmp_path / "corpus.jsonl"
    stats = synth_corpus.write_corpus(
        str(stem), synth_corpus.CorpusSpec(docs=101), shards=4)
    assert len(stats) == 4
    assert sum(shard.docs for shard in stats.values()) == 101
    ids = [document["id"] for path in stats
           for document in corpus.read_corpus(path)]
    assert len(ids) == len(set(ids)) == 101


def test_a_shard_is_readable_by_corpus_shard(tmp_path):
    """The write path splits `--max-docs` by byte offset, so a shard file has to
    survive being split again."""
    path = tmp_path / "c.jsonl"
    written(path, 60)
    halves = [list(corpus_shard.read_shard(str(path), index, 2))
              for index in range(2)]
    assert sum(len(half) for half in halves) == 60


def test_both_loaders_can_encode_a_synthetic_batch(tmp_path):
    documents = written(tmp_path / "c.jsonl", 8)
    payload = opensearch_load.bulk_payload(documents, "wiki-articles")
    assert payload.count(b"\n") == 16
    parameters = scylla_load.insert_parameters(documents)
    assert len(parameters) == 8
    assert all(len(row) == 4 for row in parameters)


def test_text_of_size_fills_the_budget_to_the_byte():
    import random

    pool = synth_corpus.word_pool(1)
    for target in (1, 2, 17, 300, 4000):
        text = synth_corpus.text_of_size(random.Random(3), pool, target, 0.0)
        assert len(text.encode("utf-8")) == target


def test_text_of_size_is_empty_when_the_envelope_already_fills_the_line():
    import random

    assert synth_corpus.text_of_size(random.Random(3),
                                     synth_corpus.word_pool(1), -5, 0.0) == ""


def test_the_cli_writes_a_stats_file(tmp_path):
    output = tmp_path / "c.jsonl"
    stats_out = tmp_path / "stats.json"
    assert synth_corpus.main(["--output", str(output), "--docs", "12",
                              "--stats-out", str(stats_out)]) == 0
    stats = json.loads(stats_out.read_text())
    assert stats[str(output)]["docs"] == 12
