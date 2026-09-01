"""Closed-loop query latency benchmark skeleton.

Single-threaded and closed-loop: right for smoke tests and per-class
comparisons, WRONG for the QPS-vs-p99 sweep (chart C7) and headline HDR curve
(C5) — those need an open-loop, coordinated-omission-safe load generator, which
is `ftsbench.load_gen`. Its percentiles are service times at whatever rate one
thread happens to produce, so they are not an SLA. Warns about zero-hit queries
so a generated query set can be validated.

Usage:
  python3 -m ftsbench.query_bench --engine opensearch --queries data/queries.json --output data/results-opensearch.json
  python3 -m ftsbench.query_bench --engine scylladb  --queries data/queries.json --output data/results-scylladb.json
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone

from .engines import DEFAULT_LIMIT, add_connection_args, build_engine
from .stats import summarize_latencies

DEFAULT_WARMUP = 3
DEFAULT_ITERATIONS = 30


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("opensearch", "scylladb"), required=True)
    parser.add_argument("--queries", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--warmup", type=int, default=DEFAULT_WARMUP)
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    add_connection_args(parser)
    return parser.parse_args()


def timed_search(engine, text: str, limit: int) -> tuple[float, int]:
    started = time.perf_counter()
    hits = engine.search(text, limit)
    return (time.perf_counter() - started) * 1000.0, len(hits)


def bench_query(engine, text: str, args) -> tuple[dict, list[float]]:
    for _ in range(args.warmup):
        engine.search(text, args.limit)
    latencies = []
    hits = 0
    for _ in range(args.iterations):
        elapsed_ms, hits = timed_search(engine, text, args.limit)
        latencies.append(elapsed_ms)
    return {"query": text, "hits": hits, **summarize_latencies(latencies)}, latencies


def bench_class(engine, queries: list[str], args) -> dict:
    per_query = []
    all_latencies = []
    for text in queries:
        result, latencies = bench_query(engine, text, args)
        per_query.append(result)
        all_latencies.extend(latencies)
    return {"summary": summarize_latencies(all_latencies), "queries": per_query}


def warn_about_zero_hit_queries(name: str, class_result: dict) -> None:
    empty = [q["query"] for q in class_result["queries"] if q["hits"] == 0]
    if empty:
        print(f"  WARNING {name}: {len(empty)} zero-hit queries, e.g. {empty[0]!r}",
              file=sys.stderr)


def print_class_line(name: str, summary: dict) -> None:
    print(f"{name:14} p50={summary['p50_ms']:>9}ms  p95={summary['p95_ms']:>9}ms  "
          f"p99={summary['p99_ms']:>9}ms  ({summary['count']} requests)")


def main() -> int:
    args = parse_args()
    engine = build_engine(args)
    with open(args.queries, encoding="utf-8") as f:
        query_set = json.load(f)
    report_classes = {}
    for name, queries in query_set["classes"].items():
        if not queries:
            print(f"  WARNING {name}: no queries in this class, skipping", file=sys.stderr)
            continue
        class_result = bench_class(engine, queries, args)
        report_classes[name] = class_result
        print_class_line(name, class_result["summary"])
        warn_about_zero_hit_queries(name, class_result)
    report = {
        "engine": args.engine,
        "run_at": datetime.now(timezone.utc).isoformat(),
        "limit": args.limit,
        "warmup": args.warmup,
        "iterations": args.iterations,
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
