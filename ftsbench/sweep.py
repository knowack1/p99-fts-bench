"""QPS-vs-p99 rate ladder — the driver behind chart C7.

C7 exists to locate the knee: the offered rate above which p99 stops being flat.
That is a statement about the engine only if three things are true of every point
on the curve, so each is built into the record rather than checked afterwards.

- **A warmup window per rung, discarded.** The first seconds after a rate change
  are the pool filling and the engine's caches adjusting to a new working set. A
  ladder without per-rung warmup reports that transient as the rung's tail, and
  the knee lands one rung early.
- **The rungs are geometric.** A knee is a change in slope, and a linear ladder
  spends most of its rungs on the flat part before it. Doubling reaches the
  interesting region in a handful of rungs, which matters when each rung costs
  warmup plus duration.
- **The generator's ceiling is copied into every point.** `generator_saturated`
  is then computable from the record alone, and a reader cannot see the curve
  without also seeing the rate above which the curve is about the harness. On one
  laptop with the generator co-resident with the engine, this will fire; the
  honest response is to mark the points, not to hide the rate.

Points where `generator_saturated` is true are not eligible to define the knee.
If it is true at every rung, C7 has no knee to report from this host — which is a
finding about the measurement, and belongs on the chart as one.

Usage:
  python3 -m ftsbench.sweep --engine opensearch --queries data/queries.json \\
      --output data/c7-opensearch-1.jsonl --duration 60 --warmup 10
"""
import argparse
import sys
from typing import Any, TextIO

from . import runmeta
from .engines import DEFAULT_LIMIT, add_connection_args, build_engine
from .load_gen import (DEFAULT_CONCURRENCY, DEFAULT_SEED, QUERY_CLASSES,
                       Calibration, GeneratorSettings, Query, RunTally,
                       build_plan, calibrate, is_generator_saturated,
                       queue_p99_ms, read_query_set, run_window,
                       warn_about_calibration_query)
from .stats import summarize_or_empty

DEFAULT_RATE_START = 25.0
DEFAULT_RATE_FACTOR = 2.0
DEFAULT_RATE_MAX = 3200.0
DEFAULT_DURATION_S = 60.0
DEFAULT_WARMUP_S = 10.0
DEFAULT_CALIBRATE_DURATION_S = 10.0
CEILING_NOT_MEASURED = -1.0


def geometric_ladder(start_qps: float, factor: float,
                     max_qps: float) -> list[float]:
    if start_qps <= 0 or factor <= 1.0:
        raise ValueError("ladder needs a positive start and a factor above 1")
    rates = []
    rate = start_qps
    while rate <= max_qps:
        rates.append(round(rate, 3))
        rate *= factor
    return rates


def parse_rates(raw: str) -> list[float]:
    return [float(part) for part in raw.split(",") if part.strip()]


def build_ladder(args: argparse.Namespace) -> list[float]:
    if args.rates:
        return parse_rates(args.rates)
    return geometric_ladder(args.rate_start, args.rate_factor, args.rate_max)


def establish_ceiling(engine: Any, settings: GeneratorSettings,
                      args: argparse.Namespace) -> Calibration:
    """Calibration runs before the ladder, never after: every point needs the
    ceiling inside it, and a ceiling measured after a saturating ladder would be
    taken on a machine still working through the backlog it left."""
    if args.ceiling_qps is not None:
        return Calibration(args.ceiling_qps, CEILING_NOT_MEASURED,
                           settings.concurrency, 0.0, 0)
    calibration = calibrate(engine, settings, args.calibrate_duration)
    warn_about_calibration_query(calibration)
    return calibration


def sweep_point_record(index: int, offered_qps: float, tally: RunTally,
                       args: argparse.Namespace,
                       calibration: Calibration) -> dict[str, Any]:
    summary = summarize_or_empty(tally.latency_ms)
    achieved_qps = tally.completed / args.duration
    queue_p99 = queue_p99_ms(tally)
    return {
        "record": "sweep_point",
        "i": index,
        "offered_qps": offered_qps,
        "achieved_qps": round(achieved_qps, 2),
        "duration_s": args.duration,
        "warmup_s": args.warmup,
        "count": summary["count"],
        "errors": tally.errors,
        "p50_ms": summary["p50_ms"],
        "p95_ms": summary["p95_ms"],
        "p99_ms": summary["p99_ms"],
        "p999_ms": summary["p999_ms"],
        "max_ms": summary["max_ms"],
        "queue_p99_ms": queue_p99,
        "unsupported_percentiles": summary["unsupported_percentiles"],
        "generator_ceiling_qps": calibration.generator_ceiling_qps,
        "generator_saturated": is_generator_saturated(
            offered_qps, achieved_qps, queue_p99, summary["p99_ms"],
            calibration.generator_ceiling_qps, summary["count"]),
    }


def run_rung(engine: Any, plan: list[Query], index: int, offered_qps: float,
             settings: GeneratorSettings, args: argparse.Namespace,
             calibration: Calibration) -> dict[str, Any]:
    if args.warmup > 0:
        run_window(engine, plan, offered_qps, args.warmup, settings)
    tally = run_window(engine, plan, offered_qps, args.duration, settings)
    return sweep_point_record(index, offered_qps, tally, args, calibration)


def print_point(point: dict[str, Any]) -> None:
    flag = "  GENERATOR-SATURATED" if point["generator_saturated"] else ""
    print(f"offered={point['offered_qps']:>9g}  "
          f"achieved={point['achieved_qps']:>9.1f}  "
          f"p50={point['p50_ms']:>8}  p99={point['p99_ms']:>9}  "
          f"queue_p99={point['queue_p99_ms']:>9}  "
          f"errors={point['errors']}{flag}", file=sys.stderr)


def run_ladder(engine: Any, plan: list[Query], ladder: list[float],
               settings: GeneratorSettings, args: argparse.Namespace,
               calibration: Calibration, sink: TextIO) -> None:
    for index, offered_qps in enumerate(ladder):
        point = run_rung(engine, plan, index, offered_qps, settings, args,
                         calibration)
        runmeta.write_record(sink, point)
        print_point(point)
        if args.stop_when_saturated and point["generator_saturated"]:
            print("stopping: every higher rung would measure the generator",
                  file=sys.stderr)
            return


def sweep_header(args: argparse.Namespace, query_set: dict[str, Any],
                 calibration: Calibration, ladder: list[float]) -> dict[str, Any]:
    return runmeta.header(
        producer="sweep", engine=args.engine,
        engine_version=args.engine_version, label=args.label,
        cache_state=args.cache_state, corpus=query_set.get("corpus", ""),
        queries=args.queries, query_class=args.query_class or "all",
        ladder_qps=ladder, duration_s=args.duration, warmup_s=args.warmup,
        concurrency=args.concurrency, limit=args.limit, seed=args.seed,
        ceiling_source="override" if args.ceiling_qps is not None else "measured",
        generator_ceiling_qps=calibration.generator_ceiling_qps,
        dispatch_ceiling_qps=calibration.dispatch_ceiling_qps)


def add_ladder_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--rates", default="",
                        help="explicit comma-separated ladder, overrides the "
                             "geometric one")
    parser.add_argument("--rate-start", type=float, default=DEFAULT_RATE_START)
    parser.add_argument("--rate-factor", type=float, default=DEFAULT_RATE_FACTOR)
    parser.add_argument("--rate-max", type=float, default=DEFAULT_RATE_MAX)
    parser.add_argument("--stop-when-saturated", action="store_true",
                        help="end the ladder at the first generator-saturated "
                             "rung instead of completing it")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("opensearch", "scylladb"), required=True)
    parser.add_argument("--queries", required=True)
    parser.add_argument("--output", required=True, help="JSONL sweep_point path")
    parser.add_argument("--duration", type=float, default=DEFAULT_DURATION_S)
    parser.add_argument("--warmup", type=float, default=DEFAULT_WARMUP_S)
    parser.add_argument("--class", dest="query_class", choices=QUERY_CLASSES)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--calibrate-duration", type=float,
                        default=DEFAULT_CALIBRATE_DURATION_S)
    parser.add_argument("--ceiling-qps", type=float,
                        help="reuse a published ceiling instead of measuring one")
    parser.add_argument("--label", default="")
    parser.add_argument("--cache-state", default="unspecified")
    parser.add_argument("--engine-version", default="unknown")
    add_ladder_args(parser)
    add_connection_args(parser)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = GeneratorSettings(args.concurrency, args.limit, args.seed)
    engine = build_engine(args, pool_maxsize=args.concurrency)
    query_set = read_query_set(args.queries)
    plan = build_plan(query_set, args.query_class)
    calibration = establish_ceiling(engine, settings, args)
    ladder = build_ladder(args)
    with open(args.output, "w", encoding="utf-8") as sink:
        runmeta.write_record(sink, sweep_header(args, query_set, calibration, ladder))
        run_ladder(engine, plan, ladder, settings, args, calibration, sink)
    print(f"sweep written to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
