"""Sharded closed-loop cell: cell_bench split across worker PROCESSES.

Calibration on the fleet measured a single python process at ~2,300 qps
against OpenSearch (GIL-bound request path; the no-op dispatch ceiling is
~90k), which is below where several cube cells live. This wrapper splits a
cell's concurrency across K spawned processes — each with its own GIL, its
own engine client and its own worker pool — and merges the RAW latencies in
the parent, so the percentiles in the artifact are computed over the union
sample, never averaged from shard summaries. The artifact is byte-compatible
with cell_bench's, plus a `processes` field and per-shard achieved rates.

    python3 -m ftsbench.cell_bench_mp --processes 6 <cell_bench args>

Shards start together (a Barrier before the warmup) so their measurement
windows overlap; achieved_qps is the sum of per-shard completed/wall rates.
`--processes 1` is exactly cell_bench.
"""
from __future__ import annotations

import argparse
import multiprocessing as mp
import sys
import time

from . import runmeta
from .cell_bench import cell_header, extra_fields, parse_args as cell_parse_args
from .engines import build_engine
from .load_gen import (GeneratorSettings, build_plan, drive_ops,
                       read_query_set, saturating_ops)
from .stats import summarize_or_empty


def shard_concurrency(total: int, processes: int) -> list[int]:
    base, remainder = divmod(total, processes)
    shares = [base + (1 if i < remainder else 0) for i in range(processes)]
    return [share for share in shares if share > 0]


def shard_main(args: argparse.Namespace, share: int, barrier, pipe) -> None:
    settings = GeneratorSettings(share, args.limit, args.seed + share)
    engine = build_engine(args, pool_maxsize=share)
    plan = build_plan(read_query_set(args.queries), args.query_class)
    barrier.wait()
    if args.warmup > 0:
        drive_ops(engine, plan, saturating_ops(args.warmup), settings,
                  time.perf_counter())
    origin = time.perf_counter()
    tally = drive_ops(engine, plan, saturating_ops(args.duration), settings,
                      origin)
    wall = time.perf_counter() - origin
    pipe.send({"service_ms": tally.service_ms, "queue_ms": tally.queue_ms,
               "completed": tally.completed, "errors": tally.errors,
               "first_error": tally.first_error, "wall_s": wall,
               "share": share})
    pipe.close()


def merged_summary(args: argparse.Namespace, shards: list[dict]) -> dict:
    # service_ms, not latency_ms — see cell_bench.run_cell; the client queue
    # is disclosed via queue_p99_ms, never billed to the engine.
    latencies = [ms for shard in shards for ms in shard["service_ms"]]
    queue_ms = sorted(ms for shard in shards for ms in shard["queue_ms"])
    achieved = sum(s["completed"] / s["wall_s"] for s in shards if s["wall_s"])
    errors = sum(s["errors"] for s in shards)
    first_error = next((s["first_error"] for s in shards
                        if s["first_error"]), None)
    from .stats import percentile
    return {
        "record": "cell_summary",
        "query_class": args.query_class,
        "limit": args.limit,
        "concurrency": args.concurrency,
        "rep": args.rep,
        "processes": len(shards),
        "shard_qps": [round(s["completed"] / s["wall_s"], 1)
                      for s in shards if s["wall_s"]],
        "wall_s": round(max(s["wall_s"] for s in shards), 3),
        "completed": sum(s["completed"] for s in shards),
        "errors": errors,
        "first_error": first_error,
        "achieved_qps": round(achieved, 2),
        "queue_p99_ms": round(percentile(queue_ms, 99), 3) if queue_ms else 0.0,
        **extra_fields(args.extra),
        **summarize_or_empty(latencies),
    }


def main() -> int:
    parser_probe = argparse.ArgumentParser(add_help=False)
    parser_probe.add_argument("--processes", type=int, default=0)
    known, rest = parser_probe.parse_known_args()
    sys.argv = [sys.argv[0], *rest]
    args = cell_parse_args()
    processes = known.processes or min(6, max(1, args.concurrency // 4))
    shares = shard_concurrency(args.concurrency, processes)

    context = mp.get_context("spawn")
    barrier = context.Barrier(len(shares))
    children, pipes = [], []
    for share in shares:
        parent_end, child_end = context.Pipe(duplex=False)
        process = context.Process(target=shard_main,
                                  args=(args, share, barrier, child_end))
        process.start()
        children.append(process)
        pipes.append(parent_end)
    shards = [pipe.recv() for pipe in pipes]
    for process in children:
        process.join()

    summary = merged_summary(args, shards)
    query_set = read_query_set(args.queries)
    with open(args.output, "w", encoding="utf-8") as out:
        runmeta.write_record(out, cell_header(args, query_set))
        runmeta.write_record(out, summary)
    print(f"{args.query_class} l={args.limit} c={args.concurrency} "
          f"x{len(shards)}proc: p99={summary.get('p99_ms')} ms  "
          f"achieved={summary['achieved_qps']} qps  "
          f"errors={summary['errors']}", file=sys.stderr)
    return 0 if summary["completed"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
