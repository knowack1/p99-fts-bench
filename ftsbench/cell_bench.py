"""One closed-loop cell of the read-path cube (deck S18-S26).

A cell is (query class, top-N limit, concurrency): N workers in flight, each
sending the next query the moment the previous returns, against a resident
index. The artifact is a runmeta header plus ONE `cell_summary` record —
per-operation logging is deliberately absent, because a 20 s cell at tens of
thousands of qps would write a six-figure line count per cell and the charts
only consume per-run percentiles.

    python3 -m ftsbench.cell_bench --engine opensearch --url http://sut:9200 \\
        --queries data/queries.json --query-class rare_term --limit 10 \\
        --concurrency 8 --warmup 5 --duration 20 \\
        --output data/readcube/cell-opensearch-rare_term-l10-c8-r1.jsonl

Why closed-loop when the C7 pacer is open-loop: the deck's read-path section
(S18 notes) retired the open-loop offered-load sweep — on one box it measured
the generator — and adopted the concurrency-sweep convention: p99 vs workers
in flight, achieved throughput = completed / wall (Little's law view). The
coordinated-omission caveat does not apply to this convention because nothing
is offered on a schedule: latency_ms IS the service time, and `queue_ms` is
carried in the summary so a reader can verify the client's own queue stayed
out of the number.

The cube driver (`tools/read_sweep.sh`) enforces the gates; this module only
refuses to summarize an empty window.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import runmeta
from .engines import add_connection_args, build_engine
from .load_gen import (GeneratorSettings, build_plan, drive_ops,
                       queue_p99_ms, read_query_set, saturating_ops)
from .stats import summarize_or_empty


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", required=True,
                        choices=["opensearch", "scylladb"])
    add_connection_args(parser)
    parser.add_argument("--queries", required=True)
    parser.add_argument("--query-class", required=True)
    parser.add_argument("--limit", type=int, required=True)
    parser.add_argument("--concurrency", type=int, required=True)
    parser.add_argument("--warmup", type=float, default=5.0)
    parser.add_argument("--duration", type=float, default=20.0)
    parser.add_argument("--seed", type=int, default=99)
    parser.add_argument("--output", required=True)
    parser.add_argument("--label", default="")
    parser.add_argument("--cache-state", default="warm")
    parser.add_argument("--rep", type=int, default=0)
    parser.add_argument("--engine-version", default="")
    parser.add_argument("--extra", action="append", default=[],
                        metavar="KEY=VALUE",
                        help="extra fields recorded verbatim in the summary "
                             "(e.g. churn_ops_per_s=2000 for the S28 grid)")
    return parser.parse_args()


def extra_fields(pairs: list[str]) -> dict:
    fields = {}
    for pair in pairs:
        key, _, value = pair.partition("=")
        try:
            fields[key] = json.loads(value)
        except json.JSONDecodeError:
            fields[key] = value
    return fields


def cell_header(args: argparse.Namespace, query_set: dict) -> dict:
    return runmeta.header(
        producer="cell_bench", engine=args.engine,
        engine_version=args.engine_version, label=args.label,
        cache_state=args.cache_state, corpus=query_set.get("corpus", ""),
        queries=args.queries, query_class=args.query_class,
        duration_s=args.duration, warmup_s=args.warmup,
        concurrency=args.concurrency, limit=args.limit, seed=args.seed)


def run_cell(args: argparse.Namespace) -> dict:
    settings = GeneratorSettings(args.concurrency, args.limit, args.seed)
    engine = build_engine(args, pool_maxsize=args.concurrency)
    plan = build_plan(read_query_set(args.queries), args.query_class)
    if args.warmup > 0:
        drive_ops(engine, plan, saturating_ops(args.warmup), settings,
                  time.perf_counter())
    origin = time.perf_counter()
    tally = drive_ops(engine, plan, saturating_ops(args.duration), settings,
                      origin)
    wall = time.perf_counter() - origin
    # service_ms, not latency_ms: with unpaced dispatch the work queue holds
    # QUEUE_DEPTH_PER_WORKER extra ops whose latency clock starts at enqueue,
    # so latency_ms would bill the client's own queue to the engine (a c=64
    # cell measured p50=79 ms while Little's law puts 64 in flight at ~16 ms —
    # the difference was the queue, disclosed in queue_p99_ms).
    summary = summarize_or_empty(tally.service_ms)
    return {
        "record": "cell_summary",
        "query_class": args.query_class,
        "limit": args.limit,
        "concurrency": args.concurrency,
        "rep": args.rep,
        "wall_s": round(wall, 3),
        "completed": tally.completed,
        "errors": tally.errors,
        "first_error": tally.first_error,
        "achieved_qps": round(tally.completed / wall, 2) if wall > 0 else 0.0,
        "queue_p99_ms": queue_p99_ms(tally),
        **extra_fields(args.extra),
        **summary,
    }


def main() -> int:
    args = parse_args()
    query_set = read_query_set(args.queries)
    summary = run_cell(args)
    with open(args.output, "w", encoding="utf-8") as out:
        runmeta.write_record(out, cell_header(args, query_set))
        runmeta.write_record(out, summary)
    print(f"{args.query_class} l={args.limit} c={args.concurrency}: "
          f"p99={summary.get('p99_ms')} ms  "
          f"achieved={summary['achieved_qps']} qps  "
          f"errors={summary['errors']}", file=sys.stderr)
    return 0 if summary["completed"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
