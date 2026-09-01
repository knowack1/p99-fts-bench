"""The parallel corpus pipeline must be indistinguishable from the serial one.

The output corpus is frozen and checksummed (`FREEZE.md`), and every chart
footer cites it — so parallelising `prepare_corpus` is only safe if the bytes
it writes are identical for any --jobs value. Three failure modes get a test
each because all three produce a corpus that loads without complaint:

- shard results merged in completion order instead of shard order would
  reorder documents and change the frozen checksum;
- a worker that re-serialises with default `ensure_ascii` would escape every
  non-ASCII title and body, changing bytes while keeping JSON-equality;
- a --max-docs cap applied per worker instead of globally would write more
  documents than the serial pipeline caps at.
"""
import bz2
import json
from pathlib import Path

import pytest

from ftsbench import prepare_corpus

MIN_CHARS = 10
LONG_TEXT = "x" * MIN_CHARS


def action_line(page_id):
    return json.dumps({"index": {"_id": page_id}})


def source_line(title, text):
    return json.dumps({"title": title, "text": text})


def write_shard(path, docs):
    lines = []
    for page_id, title, text in docs:
        lines.append(action_line(page_id))
        lines.append(source_line(title, text))
    path.write_bytes(bz2.compress(("\n".join(lines) + "\n").encode("utf-8")))


def make_shards(tmp_path, per_shard_docs):
    shard_dir = tmp_path / "shards"
    shard_dir.mkdir()
    for i, docs in enumerate(per_shard_docs):
        write_shard(shard_dir / f"content-{i:05d}.json.bz2", docs)
    return str(shard_dir / "content-*.json.bz2")


def run(input_pattern, output, jobs, max_docs=0):
    return prepare_corpus.run(input_pattern=input_pattern, output=str(output),
                              max_docs=max_docs, min_chars=MIN_CHARS, jobs=jobs)


def shard_docs(start_id, count, title_prefix="doc"):
    return [(start_id + i, f"{title_prefix}-{start_id + i}", LONG_TEXT)
            for i in range(count)]


@pytest.fixture
def three_shards(tmp_path):
    return make_shards(tmp_path, [
        shard_docs(100, 4),
        [(200, "short", "tiny"), ("not-an-id", "bad", LONG_TEXT),
         (201, "zażółć gęślą jaźń — ☃", LONG_TEXT + " zażółć")],
        shard_docs(300, 3),
    ])


def test_parallel_output_is_byte_identical_to_serial(three_shards, tmp_path):
    serial_out = tmp_path / "serial.jsonl"
    parallel_out = tmp_path / "parallel.jsonl"

    serial_counts = run(three_shards, serial_out, jobs=1)
    parallel_counts = run(three_shards, parallel_out, jobs=3)

    assert parallel_out.read_bytes() == serial_out.read_bytes()
    assert parallel_counts == serial_counts


def test_documents_follow_shard_order_not_completion_order(three_shards, tmp_path):
    out = tmp_path / "out.jsonl"

    run(three_shards, out, jobs=3)

    ids = [json.loads(line)["id"] for line in out.read_text().splitlines()]
    assert ids == [100, 101, 102, 103, 201, 300, 301, 302]


def test_non_ascii_text_is_not_escaped(three_shards, tmp_path):
    out = tmp_path / "out.jsonl"

    run(three_shards, out, jobs=3)

    assert "zażółć gęślą jaźń — ☃" in out.read_text(encoding="utf-8")


def test_max_docs_cap_is_global_and_matches_serial(three_shards, tmp_path):
    serial_out = tmp_path / "serial.jsonl"
    parallel_out = tmp_path / "parallel.jsonl"

    run(three_shards, serial_out, jobs=1, max_docs=5)
    run(three_shards, parallel_out, jobs=3, max_docs=5)

    assert len(parallel_out.read_text().splitlines()) == 5
    assert parallel_out.read_bytes() == serial_out.read_bytes()


def test_short_and_unparsable_documents_are_skipped(three_shards, tmp_path):
    out = tmp_path / "out.jsonl"

    written, skipped = run(three_shards, out, jobs=3)

    assert written == 8
    assert skipped == 2
