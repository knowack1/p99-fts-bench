"""Build the benchmark query set from corpus term statistics.

Classes mirror the talk's query matrix (concept doc §7.3): rare single term,
common single term, exact phrase, boolean AND / NOT / mixed-with-grouping.
Each entry is plain query text valid for both engines — ScyllaDB's BM25()
(Tantivy parser) and OpenSearch's query_string (Lucene parser).

Phrases are taken from raw-adjacent token pairs (stop words kept during
counting) so quoted phrases match real adjacency in both engines' positional
indexes. Deterministic for a given corpus + seed. Run query_bench afterwards
and check its zero-hit warnings to validate the set.

Usage: python3 -m ftsbench.generate_queries --corpus data/corpus.jsonl --output data/queries.json
"""
import argparse
import collections
import json
import random
import sys

from .analyzer import is_stop_word, tokenize
from .corpus import read_corpus

DEFAULT_SAMPLE_DOCS = 10_000
DEFAULT_PER_CLASS = 20
DEFAULT_SEED = 99
MAX_TOKENS_PER_DOC = 500
COMMON_POOL_SIZE = 40
MIN_TERM_LENGTH = 3
MIN_RARE_TERM_LENGTH = 4


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--sample-docs", type=int, default=DEFAULT_SAMPLE_DOCS,
                        help="documents to scan for term statistics")
    parser.add_argument("--per-class", type=int, default=DEFAULT_PER_CLASS)
    parser.add_argument("--common-pool-size", type=int, default=COMMON_POOL_SIZE,
                        help="how many of the most frequent terms count as "
                             "'common'; also the pool the boolean classes draw "
                             "from. Raising it raises the distinct-query count "
                             "for common_term and the boolean classes, which is "
                             "what keeps every class at equal cardinality — "
                             "unequal cardinality would give the smaller class "
                             "more repeats per run and therefore a warmer cache")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    return parser.parse_args()


def adjacent_content_bigrams(raw_tokens: list[str]) -> set[tuple[str, str]]:
    return {
        (first, second)
        for first, second in zip(raw_tokens, raw_tokens[1:])
        if not is_stop_word(first) and not is_stop_word(second)
        and first.isalpha() and second.isalpha()
    }


def collect_term_stats(docs) -> tuple[int, collections.Counter, collections.Counter]:
    doc_count = 0
    term_df: collections.Counter = collections.Counter()
    phrase_df: collections.Counter = collections.Counter()
    for doc in docs:
        raw_tokens = tokenize(doc["text"], keep_stop_words=True)[:MAX_TOKENS_PER_DOC]
        doc_count += 1
        term_df.update({t for t in raw_tokens if not is_stop_word(t)})
        phrase_df.update(adjacent_content_bigrams(raw_tokens))
    return doc_count, term_df, phrase_df


def pick_common_terms(term_df: collections.Counter, count: int) -> list[str]:
    candidates = [
        term for term, _ in term_df.most_common(count * 3)
        if term.isalpha() and len(term) >= MIN_TERM_LENGTH
    ]
    return candidates[:count]


def pick_rare_terms(term_df: collections.Counter, doc_count: int,
                    count: int, rng: random.Random) -> list[str]:
    low = max(5, doc_count // 1000)
    high = max(low + 5, doc_count // 100)
    candidates = [
        term for term, df in term_df.items()
        if low <= df <= high and term.isalpha() and len(term) >= MIN_RARE_TERM_LENGTH
    ]
    rng.shuffle(candidates)
    return sorted(candidates[:count])


def pick_phrases(phrase_df: collections.Counter, count: int) -> list[str]:
    return [f'"{first} {second}"' for (first, second), _ in phrase_df.most_common(count)]


def distinct_samples(terms: list[str], group_size: int, count: int,
                     rng: random.Random) -> list[tuple[str, ...]]:
    if len(terms) < group_size:
        return []
    return [tuple(rng.sample(terms, group_size)) for _ in range(count)]


def build_boolean_queries(pool: list[str], per_class: int,
                          rng: random.Random) -> dict[str, list[str]]:
    return {
        "bool_and": [f"{a} AND {b}" for a, b in distinct_samples(pool, 2, per_class, rng)],
        "bool_not": [f"{a} NOT {b}" for a, b in distinct_samples(pool, 2, per_class, rng)],
        "bool_mixed": [f"({a} OR {b}) AND {c} NOT {d}"
                       for a, b, c, d in distinct_samples(pool, 4, per_class, rng)],
    }


def build_classes(doc_count: int, term_df: collections.Counter,
                  phrase_df: collections.Counter, per_class: int,
                  rng: random.Random,
                  common_pool_size: int = COMMON_POOL_SIZE) -> dict[str, list[str]]:
    common_pool = pick_common_terms(term_df, common_pool_size)
    classes = {
        "rare_term": pick_rare_terms(term_df, doc_count, per_class, rng),
        "common_term": common_pool[:per_class],
        "phrase": pick_phrases(phrase_df, per_class),
    }
    return {**classes, **build_boolean_queries(common_pool, per_class, rng)}


def main() -> int:
    args = parse_args()
    rng = random.Random(args.seed)
    doc_count, term_df, phrase_df = collect_term_stats(
        read_corpus(args.corpus, args.sample_docs)
    )
    if doc_count == 0:
        print("error: corpus is empty", file=sys.stderr)
        return 1
    classes = build_classes(doc_count, term_df, phrase_df, args.per_class, rng,
                            args.common_pool_size)
    output = {
        "corpus": args.corpus,
        "sampled_docs": doc_count,
        "seed": args.seed,
        "per_class": args.per_class,
        "common_pool_size": args.common_pool_size,
        "classes": classes,
    }
    with open(args.output, "w", encoding="utf-8") as out:
        json.dump(output, out, indent=2, ensure_ascii=False)
        out.write("\n")
    for name, queries in classes.items():
        print(f"{name}: {len(queries)} queries", file=sys.stderr)
    print(f"query set written to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
