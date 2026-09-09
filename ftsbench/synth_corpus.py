"""Synthetic documents at enwiki's average size, generated on the box.

Phase 0 measures the CLIENT's ceiling, and the client does not read the words:
its per-document cost is a JSON encode, a UTF-8 encode and — on the CQL side —
a parameter bind, all of which are functions of size and shape only. Staging the
frozen enwiki corpus to measure that would cost ~3 h of the fleet session and
change nothing (`BUILD-RATE-MATRIX-PLAN.md`, Phase 0).

**Not a corpus for any engine measurement.** The vocabulary is a few hundred
invented words, so collection-wide BM25 term statistics taken from it would be
meaningless, and every published relevance, recall or build number stays on the
frozen enwiki corpus. Nothing here belongs in a chart.

**The target is the LINE, not the text field.** enwiki's ~3.95 KB per document
is bytes of corpus file per document, and the canonical envelope — id, uuid,
title, and the JSON punctuation — measures 112 bytes a line on the frozen
simplewiki corpus. Sizing the text field to 3,950 bytes instead would offer the
loaders 3% more work per document than the campaign corpus does.

Output is the canonical corpus JSONL `ftsbench.corpus` reads, with the same
deterministic uuid5 of the page id `ftsbench.prepare_corpus` assigns, and
uncompressed because `ftsbench.corpus_shard` refuses anything else.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

ENWIKI_MEAN_LINE_BYTES = 3950
# Wiki article lengths are heavy-tailed; sigma 0.6 puts the middle half of the
# documents between roughly 0.6x and 1.4x the mean, which is close enough to the
# frozen corpus for a client-cost measurement and stays far from zero.
DEFAULT_SIGMA = 0.6
MIN_LINE_BYTES = 300
MAX_SIZE_MULTIPLE = 20
DEFAULT_SEED = 20260908
VOCABULARY_SIZE = 512
WORD_LENGTHS = (3, 4, 5, 6, 7, 8, 9, 11, 13)
DEFAULT_ACCENT_FRACTION = 0.01
ACCENTED = "éèüßñ—–°"
TITLE_WORDS = 3
UUID_NAMESPACE_FORMAT = "wikipedia-page:{page_id}"


@dataclass(frozen=True)
class CorpusStats:
    docs: int
    line_bytes: int
    text_bytes: int

    @property
    def mean_line_bytes(self) -> float:
        return self.line_bytes / self.docs if self.docs else 0.0

    @property
    def mean_text_bytes(self) -> float:
        return self.text_bytes / self.docs if self.docs else 0.0

    def plus(self, line_bytes: int, text_bytes: int) -> "CorpusStats":
        return CorpusStats(self.docs + 1, self.line_bytes + line_bytes,
                           self.text_bytes + text_bytes)

    def as_dict(self) -> dict[str, Any]:
        return {
            "docs": self.docs,
            "bytes": self.line_bytes,
            "mean_line_bytes": round(self.mean_line_bytes, 1),
            "mean_text_bytes": round(self.mean_text_bytes, 1),
        }


def word_pool(seed: int, size: int = VOCABULARY_SIZE) -> tuple[str, ...]:
    """A fixed, seeded vocabulary — invented words, deliberately.

    Real words would invite someone to run a relevance query against this
    corpus; nonsense of the right length and character mix costs the client
    exactly the same and cannot be mistaken for enwiki.
    """
    rng = random.Random(seed)
    letters = "abcdefghijklmnopqrstuvwxyz"
    return tuple("".join(rng.choice(letters)
                         for _ in range(rng.choice(WORD_LENGTHS)))
                 for _ in range(size))


def accented(word: str, rng: random.Random) -> str:
    return word + rng.choice(ACCENTED)


def next_word(rng: random.Random, pool: tuple[str, ...],
              accent_fraction: float) -> str:
    word = pool[rng.randrange(len(pool))]
    if accent_fraction and rng.random() < accent_fraction:
        return accented(word, rng)
    return word


def text_of_size(rng: random.Random, pool: tuple[str, ...], target_bytes: int,
                 accent_fraction: float) -> str:
    """Space-separated words filling `target_bytes` of UTF-8, exactly.

    Exactly, and not approximately: the per-document byte count is the whole
    reason this generator exists, and a filler that rounded to a word boundary
    would let the mean drift with the vocabulary's word lengths.
    """
    if target_bytes <= 0:
        return ""
    parts: list[str] = []
    used = 0
    while True:
        word = next_word(rng, pool, accent_fraction)
        cost = len(word.encode("utf-8")) + (1 if parts else 0)
        if used + cost > target_bytes:
            break
        parts.append(word)
        used += cost
    shortfall = target_bytes - used
    if shortfall > 0:
        parts.append("x" * (shortfall - 1) if parts else "x" * shortfall)
    return " ".join(parts)


def title_of(rng: random.Random, pool: tuple[str, ...]) -> str:
    return " ".join(next_word(rng, pool, 0.0).capitalize()
                    for _ in range(TITLE_WORDS))


def document_id(index: int) -> int:
    return index + 1


def document_uuid(page_id: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL,
                          UUID_NAMESPACE_FORMAT.format(page_id=page_id)))


def envelope_bytes(document: dict[str, Any]) -> int:
    """Bytes the line costs before any text: id, uuid, title, punctuation."""
    return len(line_for({**document, "text": ""}).encode("utf-8"))


def line_for(document: dict[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False) + "\n"


def target_line_bytes(rng: random.Random, mean_bytes: int,
                      sigma: float) -> int:
    """One document's size, lognormal with the configured mean preserved."""
    if sigma <= 0:
        return mean_bytes
    drawn = rng.lognormvariate(math.log(mean_bytes) - sigma * sigma / 2, sigma)
    return int(min(max(drawn, MIN_LINE_BYTES), mean_bytes * MAX_SIZE_MULTIPLE))


def synthetic_document(index: int, rng: random.Random, pool: tuple[str, ...],
                       mean_bytes: int, sigma: float,
                       accent_fraction: float) -> dict[str, Any]:
    page_id = document_id(index)
    skeleton = {"id": page_id, "uuid": document_uuid(page_id),
                "title": title_of(rng, pool), "text": ""}
    wanted = target_line_bytes(rng, mean_bytes, sigma)
    return {**skeleton,
            "text": text_of_size(rng, pool,
                                 wanted - envelope_bytes(skeleton),
                                 accent_fraction)}


@dataclass(frozen=True)
class CorpusSpec:
    docs: int
    mean_bytes: int = ENWIKI_MEAN_LINE_BYTES
    sigma: float = DEFAULT_SIGMA
    accent_fraction: float = DEFAULT_ACCENT_FRACTION
    seed: int = DEFAULT_SEED


def write_documents(stream: TextIO, spec: CorpusSpec, first: int,
                    count: int) -> CorpusStats:
    """`count` documents starting at global index `first`.

    Ids come from the global index so that shards written for a multi-process
    run hold disjoint primary keys: two workers sharing a document would write
    the same row twice and the run would report both as delivered.
    """
    rng = random.Random(spec.seed + first)
    pool = word_pool(spec.seed)
    stats = CorpusStats(0, 0, 0)
    for offset in range(count):
        document = synthetic_document(first + offset, rng, pool,
                                      spec.mean_bytes, spec.sigma,
                                      spec.accent_fraction)
        line = line_for(document)
        stream.write(line)
        stats = stats.plus(len(line.encode("utf-8")),
                           len(document["text"].encode("utf-8")))
    return stats


def shard_paths(output: str, shards: int) -> list[str]:
    if shards == 1:
        return [output]
    path = Path(output)
    return [str(path.with_name(f"{path.stem}-{index}{path.suffix}"))
            for index in range(shards)]


def shard_sizes(docs: int, shards: int) -> list[int]:
    return [docs // shards + (1 if index < docs % shards else 0)
            for index in range(shards)]


def write_corpus(output: str, spec: CorpusSpec,
                 shards: int = 1) -> dict[str, CorpusStats]:
    written: dict[str, CorpusStats] = {}
    first = 0
    for path, count in zip(shard_paths(output, shards),
                           shard_sizes(spec.docs, shards)):
        with open(path, "w", encoding="utf-8") as stream:
            written[path] = write_documents(stream, spec, first, count)
        first += count
    return written


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True,
                        help="corpus JSONL path; with --shards N the name gains "
                             "a -0..-N-1 suffix per shard")
    parser.add_argument("--docs", type=int, required=True,
                        help="documents in total, split across the shards")
    parser.add_argument("--mean-bytes", type=int,
                        default=ENWIKI_MEAN_LINE_BYTES,
                        help="mean bytes per corpus LINE, envelope included")
    parser.add_argument("--sigma", type=float, default=DEFAULT_SIGMA,
                        help="lognormal spread of the size; 0 = every document "
                             "the same size")
    parser.add_argument("--accent-fraction", type=float,
                        default=DEFAULT_ACCENT_FRACTION,
                        help="share of words carrying a multi-byte character, "
                             "so UTF-8 encoding costs what it does on enwiki")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--shards", type=int, default=1,
                        help="write this many disjoint corpora, for a run of N "
                             "loader processes")
    parser.add_argument("--stats-out", default=None,
                        help="write the achieved per-shard sizes as JSON")
    return parser.parse_args(argv)


def report(written: dict[str, CorpusStats]) -> None:
    for path, stats in written.items():
        print(f"{path}: {stats.docs} docs, {stats.line_bytes} bytes, "
              f"mean line {stats.mean_line_bytes:.0f} B, "
              f"mean text {stats.mean_text_bytes:.0f} B", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    spec = CorpusSpec(docs=args.docs, mean_bytes=args.mean_bytes,
                      sigma=args.sigma, accent_fraction=args.accent_fraction,
                      seed=args.seed)
    written = write_corpus(args.output, spec, args.shards)
    report(written)
    if args.stats_out:
        with open(args.stats_out, "w", encoding="utf-8") as stream:
            json.dump({path: stats.as_dict()
                       for path, stats in written.items()}, stream, indent=2)
            stream.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
