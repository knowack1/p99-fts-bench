"""Cross-engine top-K agreement, as a fast parity smoke check.

This is NOT recall in the IR sense: there is no relevance-judged ground truth
in this repo (see TUNING.md's open BM25 k1/b parity gate), so a low score here
means "the two engines disagree", not "one of them is wrong". Useful as a fast
regression check on analyzer/BM25 parity across a small, quickly-loaded corpus;
not a number to quote, and not predictive of the full-corpus figure — BM25's
IDF and avgdl are corpus-relative, and a multi-shard AWS OpenSearch cluster
computes IDF per-shard rather than globally the way this single-node run does.

OpenSearch's `_id` is the corpus page id (ftsbench.opensearch_load); ScyllaDB's
primary key is a uuid5 of that same page id, so the comparison is instead done
on ScyllaDB's plain `page_id` column, which both loaders write.

Usage:
  python3 -m ftsbench.recall_check --queries data/queries-small.json \
      --output data/recall-small.json --limit 10 --sample-per-class 10
"""
import argparse
import json
import sys
from datetime import datetime, timezone

from .engines import DEFAULT_LIMIT, ScyllaEngine, add_connection_args, build_engine

DEFAULT_SAMPLE_PER_CLASS = 10


class ScyllaPageIdEngine(ScyllaEngine):
    """ScyllaEngine's same BM25 query, projecting `page_id` instead of
    `article_id` so results line up with OpenSearch's page-id-keyed `_id`."""

    def __init__(self, hosts: list[str], port: int, keyspace: str, table: str,
                 column: str):
        super().__init__(hosts, port=port, keyspace=keyspace, table=table,
                         column=column)
        self._projection = "page_id"

    def search(self, query_text: str, limit: int = DEFAULT_LIMIT) -> list[str]:
        rows = list(self._session.execute(self._build_query(query_text, limit)))
        return [str(row.page_id) for row in rows]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queries", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--sample-per-class", type=int,
                        default=DEFAULT_SAMPLE_PER_CLASS,
                        help="first N queries of each class; keeps a smoke "
                             "check fast without needing a smaller query file")
    parser.add_argument("--classes", nargs="*", default=None,
                        help="restrict to these classes; default is every "
                             "class in the query file")
    add_connection_args(parser)
    return parser.parse_args()


def overlap(a: list[str], b: list[str]) -> int:
    return len(set(a) & set(b))


def jaccard(a: list[str], b: list[str]) -> float:
    """1.0 when both sides agree there are no results — an empty union is
    agreement, not a comparison with nothing to say."""
    union = set(a) | set(b)
    if not union:
        return 1.0
    return len(set(a) & set(b)) / len(union)


def recall_at_k(reference: list[str], other: list[str]) -> float:
    """Fraction of `reference`'s top-K also present in `other`'s top-K.

    1.0 when `reference` is empty: an engine that found nothing has nothing
    for the other engine to have missed.
    """
    if not reference:
        return 1.0
    return overlap(reference, other) / len(reference)


def compare_query(opensearch, scylla, text: str, limit: int) -> dict:
    os_ids = opensearch.search(text, limit)
    scylla_ids = scylla.search(text, limit)
    return {
        "query": text,
        "opensearch_hits": len(os_ids),
        "scylladb_hits": len(scylla_ids),
        "jaccard_at_k": jaccard(os_ids, scylla_ids),
        "recall_at_k_scylladb_vs_opensearch": recall_at_k(os_ids, scylla_ids),
        "recall_at_k_opensearch_vs_scylladb": recall_at_k(scylla_ids, os_ids),
    }


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def summarize_class(per_query: list[dict]) -> dict:
    zero_overlap = [q["query"] for q in per_query
                    if q["jaccard_at_k"] == 0.0
                    and (q["opensearch_hits"] or q["scylladb_hits"])]
    return {
        "mean_jaccard_at_k": mean([q["jaccard_at_k"] for q in per_query]),
        "mean_recall_at_k_scylladb_vs_opensearch":
            mean([q["recall_at_k_scylladb_vs_opensearch"] for q in per_query]),
        "mean_recall_at_k_opensearch_vs_scylladb":
            mean([q["recall_at_k_opensearch_vs_scylladb"] for q in per_query]),
        "zero_overlap_queries": zero_overlap,
    }


def print_class_line(name: str, summary: dict) -> None:
    flagged = f" ({len(summary['zero_overlap_queries'])} zero-overlap)" \
        if summary["zero_overlap_queries"] else ""
    print(f"{name:14} jaccard={summary['mean_jaccard_at_k']:.2f}  "
          f"recall(scylla|os)={summary['mean_recall_at_k_scylladb_vs_opensearch']:.2f}  "
          f"recall(os|scylla)={summary['mean_recall_at_k_opensearch_vs_scylladb']:.2f}"
          f"{flagged}")


def build_scylla_engine(args: argparse.Namespace) -> ScyllaPageIdEngine:
    return ScyllaPageIdEngine(args.hosts.split(","), port=args.port,
                              keyspace=args.keyspace, table=args.table,
                              column=args.column)


def sampled_classes(query_set: dict, classes: list[str] | None,
                    sample_per_class: int) -> dict[str, list[str]]:
    names = classes if classes else list(query_set["classes"].keys())
    return {name: query_set["classes"][name][:sample_per_class] for name in names}


def main() -> int:
    args = parse_args()
    args.engine = "opensearch"
    opensearch = build_engine(args)
    scylla = build_scylla_engine(args)
    with open(args.queries, encoding="utf-8") as f:
        query_set = json.load(f)
    report_classes = {}
    for name, queries in sampled_classes(query_set, args.classes,
                                         args.sample_per_class).items():
        if not queries:
            print(f"  WARNING {name}: no queries in this class, skipping",
                  file=sys.stderr)
            continue
        per_query = [compare_query(opensearch, scylla, text, args.limit)
                    for text in queries]
        summary = summarize_class(per_query)
        report_classes[name] = {"summary": summary, "queries": per_query}
        print_class_line(name, summary)
    report = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "limit": args.limit,
        "sample_per_class": args.sample_per_class,
        "queries_file": args.queries,
        "classes": report_classes,
    }
    with open(args.output, "w", encoding="utf-8") as out:
        json.dump(report, out, indent=2, ensure_ascii=False)
        out.write("\n")
    print(f"results written to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
