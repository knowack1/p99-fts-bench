"""Render chart C3 — write latency over wall-clock time during ingest.

The talk's most important chart, and the one this host can least support (see
LAPTOP-RUN-PLAN.md: Tier 3). Three decisions make it able to *refute* its own
claim rather than assume it:

- **Rolling wall-clock buckets, not a fixed operation count.** Two engines
  ingesting at different rates would put a per-1000-op bucket at completely
  different wall-clock positions, so a spike in one and a calm patch in the
  other would be compared as if they were simultaneous. Buckets are a fixed
  number of seconds wide, stated on the chart, and every operation is placed by
  its **intended** send time — the moment the load was offered, which is where
  the pressure arose.
- **A percentile is drawn only where the bucket can carry it.** p99 needs 100
  operations in the bucket and p999 needs 1,000 (`stats.min_samples_for`).
  Thinner buckets leave a gap in the line and are counted in the footer, rather
  than being interpolated into a confident-looking curve.
- **Failed operations are excluded from the percentiles and counted anyway.**
  The error rate rides in the footer; dropping failures silently would turn a
  failing engine into a fast one.

    python3 -m ftsbench.plot_c3 \\
        --config opensearch:data/c3-opensearch-*.jsonl \\
        --config scylla-cdc:data/c3-scylla-cdc-*.jsonl \\
        --bucket-s 5 --output results/c3.png
"""
import argparse
import collections
import math
import sys
from typing import Any

from . import plotlib

CHART = "C3"
RECORD = "latency_op"
DEFAULT_OUTPUT = "results/c3.png"
DEFAULT_TITLE = "Write latency during ingest"
DEFAULT_BUCKET_S = 5.0
WRITE_OPS = ("bulk", "insert")
DRAWN_PERCENTILES = (99.0, 99.9)
PERCENTILE_STYLES = {99.0: {"linestyle": "-", "linewidth": 1.7, "alpha": 0.95},
                     99.9: {"linestyle": "--", "linewidth": 1.2, "alpha": 0.9}}
METRIC_NAME = "write_p99_ms"


def subtitle_for(bucket_s: float) -> str:
    return (f"p99 and p999 of latency_ms over rolling {bucket_s:g} s wall-clock "
            f"buckets, log scale; a gap means the bucket held too few operations")


def parse_args() -> argparse.Namespace:
    parser = plotlib.build_parser(
        CHART, __doc__, DEFAULT_OUTPUT, DEFAULT_TITLE,
        subtitle_for(DEFAULT_BUCKET_S), disclose_write_path=True, height=6.5)
    parser.add_argument("--bucket-s", type=float, default=DEFAULT_BUCKET_S,
                        help="wall-clock bucket width in seconds (stated on the chart)")
    args = parser.parse_args()
    if args.subtitle == subtitle_for(DEFAULT_BUCKET_S):
        args.subtitle = subtitle_for(args.bucket_s)
    return args


def write_records(run: plotlib.Run) -> list[dict[str, Any]]:
    return [item for item in run.records if item.get("op") in WRITE_OPS]


def run_write_p99(run: plotlib.Run) -> float:
    latencies = plotlib.latency_ms_values(write_records(run))
    if not latencies:
        return plotlib.UNRESOLVED
    return plotlib.percentiles(latencies, [99.0])[99.0]


def bucket_of(record: dict[str, Any], width: float) -> int:
    return int(float(record.get("t_intended_s") or 0.0) // width)


def bucketize(records: list[dict[str, Any]],
              width: float) -> dict[int, list[float]]:
    buckets: dict[int, list[float]] = collections.defaultdict(list)
    for record in records:
        if record.get("ok", True) and record.get("latency_ms") is not None:
            buckets[bucket_of(record, width)].append(float(record["latency_ms"]))
    return dict(buckets)


def bucket_centre(index: int, width: float) -> float:
    return (index + 0.5) * width


def bucket_series(buckets: dict[int, list[float]], width: float,
                  pct: float) -> tuple[list[float], list[float], int]:
    """Values for one percentile, with NaN where the bucket cannot carry it."""
    times, values, omitted = [], [], 0
    for index in sorted(buckets):
        latencies = buckets[index]
        drawn = plotlib.supported_percentiles(len(latencies), [pct])
        times.append(bucket_centre(index, width))
        values.append(plotlib.percentiles(latencies, [pct])[pct] if drawn
                      else math.nan)
        omitted += 0 if drawn else 1
    return times, values, omitted


def percentile_label_for(config_name: str, pct: float, omitted: int,
                         total: int) -> str:
    label = f"{config_name} {plotlib.percentile_label(pct)}"
    if omitted:
        return f"{label} ({total - omitted}/{total} buckets)"
    return label


def plot_percentile(axes: Any, config: plotlib.ConfigSeries,
                    buckets: dict[int, list[float]], width: float,
                    pct: float, style: dict[str, Any]) -> int:
    times, values, omitted = bucket_series(buckets, width, pct)
    axes.plot(times, values, label=percentile_label_for(config.name, pct, omitted,
                                                        len(times)),
              **{**style, **PERCENTILE_STYLES[pct]})
    return omitted


def plot_config(axes: Any, config: plotlib.ConfigSeries, index: int,
                width: float) -> dict[str, Any]:
    records = write_records(config.chosen)
    buckets = bucketize(records, width)
    style = plotlib.style_for(config.name, index)
    style.pop("linestyle", None)
    omitted = {pct: plot_percentile(axes, config, buckets, width, pct, style)
               for pct in DRAWN_PERCENTILES}
    return summary_for(records, buckets, width, omitted)


def supported_whole_run(latencies: list[float]) -> dict[float, float]:
    """Whole-run percentiles the sample count can carry.

    The per-bucket path has always called `is_supported`; this one did not, so
    a p999 taken from 541 operations — against a floor of 1,000 — was written to
    the sidecar and quoted in FINDINGS as a result. At that count p999 is the
    slowest single operation wearing a percentile's name."""
    drawn = plotlib.supported_percentiles(len(latencies), DRAWN_PERCENTILES)
    return plotlib.percentiles(latencies, drawn) if latencies else {}


def refused_whole_run(latencies: list[float]) -> list[str]:
    drawn = set(plotlib.supported_percentiles(len(latencies), DRAWN_PERCENTILES))
    return [plotlib.percentile_label(pct) for pct in DRAWN_PERCENTILES
            if pct not in drawn]


def summary_for(records: list[dict[str, Any]], buckets: dict[int, list[float]],
                width: float, omitted: dict[float, int]) -> dict[str, Any]:
    latencies = plotlib.latency_ms_values(records)
    return {
        "write_operations": len(records),
        "write_operations_ok": len(latencies),
        "errors": plotlib.error_count(records),
        "bucket_width_s": width,
        "buckets": len(buckets),
        "buckets_without_support": {plotlib.percentile_label(pct): count
                                    for pct, count in omitted.items()},
        "whole_run": supported_whole_run(latencies),
        "whole_run_refused": refused_whole_run(latencies),
        "documents_written": sum(int(item.get("n_docs") or 0) for item in records),
    }


def support_note(summaries: dict[str, dict[str, Any]]) -> str:
    parts = []
    for name, summary in summaries.items():
        gaps = ", ".join(f"{label} in {count}"
                         for label, count in summary["buckets_without_support"].items()
                         if count)
        if gaps:
            parts.append(f"{name}: {summary['buckets']} buckets, too few operations "
                         f"for {gaps}")
    if not parts:
        return ""
    return ("percentiles not drawn where a bucket held fewer than 1/(1-p) "
            "operations — " + "; ".join(parts))


def error_note(summaries: dict[str, dict[str, Any]]) -> str:
    failing = [f"{name}: {summary['errors']:,} of {summary['write_operations']:,}"
               for name, summary in summaries.items() if summary["errors"]]
    if not failing:
        return "no failed write operations in the plotted runs"
    return ("failed writes, excluded from the percentiles and counted here — "
            + "; ".join(failing))


OPS_PER_BUCKET_FLOOR_NOTE = (
    "operations per bucket is target_rate / batch_size * bucket_s, which does "
    "not grow with the corpus: a longer run buys more buckets, not deeper ones")


def offered_ops_per_bucket(run: plotlib.Run, bucket_s: float) -> str:
    rate = run.header.get("target_rate_docs_per_s")
    batch = run.header.get("batch_size")
    if not rate or not batch:
        return ""
    return (f" — offered {rate:g} docs/s at batch {batch:g} is "
            f"{rate / batch:g} operations/s, {rate / batch * bucket_s:g} per "
            f"{bucket_s:g} s bucket")


def assert_buckets_can_carry_a_percentile(
        configs: list[plotlib.ConfigSeries],
        summaries: dict[str, dict[str, Any]], bucket_s: float) -> None:
    """A config whose every bucket was refused contributes no line at all.

    Left unchecked this draws a legend entry reading "0/28 buckets" and no
    curve, which is what the laptop campaign shipped."""
    runs = {config.name: config.chosen for config in configs}
    for name, summary in summaries.items():
        total = summary["buckets"]
        refused = summary["buckets_without_support"]
        if not total or min(refused.values(), default=0) < total:
            continue
        raise SystemExit(
            f"{name}: all {total} buckets held too few operations for "
            f"{', '.join(sorted(refused))}, so this config draws nothing"
            f"{offered_ops_per_bucket(runs[name], bucket_s)}. "
            f"{OPS_PER_BUCKET_FLOOR_NOTE}. Lower --batch-size, raise the offered "
            f"rate, or widen --bucket-s")


def plot(args: argparse.Namespace,
         configs: list[plotlib.ConfigSeries]) -> tuple[Any, dict[str, Any]]:
    figure, axes = plotlib.plt.subplots(figsize=(args.width, args.height))
    summaries = {config.name: plot_config(axes, config, index, args.bucket_s)
                 for index, config in enumerate(configs)}
    assert_buckets_can_carry_a_percentile(configs, summaries, args.bucket_s)
    axes.set_yscale("log")
    axes.set_xlim(left=0)
    axes.legend(loc="best", fontsize=8, framealpha=0.92, ncol=2)
    plotlib.frame(axes, args, "wall time from run start (s), bucket centre",
                  "write latency (ms, log scale)")
    return figure, extras(args, summaries)


def extras(args: argparse.Namespace,
           summaries: dict[str, dict[str, Any]]) -> dict[str, Any]:
    notes = [f"buckets are {args.bucket_s:g} s of wall clock, placed by each "
             f"operation's intended send time, so both engines are compared over "
             f"the same time windows regardless of their rates",
             support_note(summaries), error_note(summaries)]
    return {
        "metric_name": METRIC_NAME,
        "notes": [note for note in notes if note],
        "bucket_width_s": args.bucket_s,
        "x_axis": "wall time from run start (s), bucketed by t_intended_s",
        "y_axis": "latency_ms, log scale",
        "latency_definition": "latency_ms = (t_end_s - t_intended_s) * 1000, "
                              "measured from the scheduled send time",
        "per_config": summaries,
    }


def main() -> int:
    args = parse_args()
    return plotlib.emit(args, RECORD, run_write_p99, plot)


if __name__ == "__main__":
    sys.exit(main())
