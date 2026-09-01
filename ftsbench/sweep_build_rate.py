"""Summarize a build-rate saturation sweep into a CSV, a JSON sidecar and a plot.

X is loader concurrency, Y is the docs/s ceiling at that concurrency. The
per-point statistics come from ftsbench.build_report.summarize, the same
function the C1 sidecar uses, so a point here and a C1 number over the same
series cannot disagree.

Comparability caveat, recorded here because it travels with the artifact:
`--concurrency` is not the same quantity on both sides. For OpenSearch it is
whole `_bulk` requests in flight; for ScyllaDB it is rows in flight inside one
batch, held by the driver. The two curves therefore share an axis label but not
an axis, and a point-for-point reading across engines at equal x is not valid.
The ceiling each curve reaches is the comparable part.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
import statistics
from dataclasses import dataclass

from ftsbench import build_report

SERIES_RE = re.compile(r"c1-(?P<config>.+)-c(?P<concurrency>\d+)-(?P<rep>\d+)\.jsonl$")

AXIS_CAVEAT = (
    "x is not one quantity across engines: OpenSearch --concurrency is whole "
    "_bulk requests in flight, ScyllaDB --concurrency is rows in flight inside "
    "one batch (driver-held). Compare the ceilings, not equal-x points."
)
COMMIT_NOTE = (
    "OpenSearch runs refresh_interval=3s to match the vector-store's "
    "COMMIT_INTERVAL=3s. scylla-cdc is stock 1.10.0, where commits also fire at "
    "MAX_UNCOMMITTED_THRESHOLD=10,000 docs — the trigger that actually binds "
    "above ~3,300 docs/s. scylla-cdc-nothreshold is 1.10.0 with that threshold "
    "raised to usize::MAX, so the 3s interval is the only trigger and the "
    "refresh parity with OpenSearch holds across the whole ladder."
)

CSV_COLUMNS = (
    "config", "engine", "concurrency", "rep", "docs_total",
    "build_wall_seconds", "docs_per_s_overall", "docs_per_s_median",
    "docs_per_s_mean", "docs_per_s_p10", "docs_per_s_p90", "docs_per_s_max",
    "throughput_variability", "stall_fraction", "merges_total",
    "merge_time_s", "segments_final", "time_to_serving_s",
    "engine_version", "cache_state", "swap_used_bytes", "load_avg_1m",
    "series_file",
)


@dataclass(frozen=True)
class Point:
    config: str
    concurrency: int
    rep: int
    summary: dict
    header: dict
    path: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="data/sweep",
                        help="directory holding c1-<config>-c<N>-<rep>.jsonl series")
    parser.add_argument("--output-csv", default="results/build-rate-sweep.csv")
    parser.add_argument("--output-json", default="results/build-rate-sweep.json")
    parser.add_argument("--output-png", default="results/build-rate-sweep.png")
    parser.add_argument("--metric", default="docs_per_s_overall",
                        help="per-run metric plotted and aggregated")
    parser.add_argument("--title", default="Build rate saturation sweep")
    parser.add_argument("--footer-extra", default="")
    return parser.parse_args()


def discover_series(data_dir: str) -> list[str]:
    found = sorted(glob.glob(os.path.join(data_dir, "c1-*-c*-*.jsonl")))
    return [p for p in found if measured_repetition(p)]


def measured_repetition(path: str) -> bool:
    """Repetition 0 is the discarded warmup, kept on disk as evidence that the
    cold-start run was paid before the ladder rather than inside it."""
    match = SERIES_RE.search(os.path.basename(path))
    return bool(match) and int(match.group("rep")) > 0


def load_point(path: str) -> Point | None:
    match = SERIES_RE.search(os.path.basename(path))
    if not match:
        return None
    header, samples = build_report.load_series(path)
    summary = build_report.summarize(header, samples)
    if "error" in summary:
        print(f"skipped {path}: {summary['error']}")
        return None
    return Point(config=match.group("config"),
                 concurrency=int(match.group("concurrency")),
                 rep=int(match.group("rep")),
                 summary=summary, header=header, path=path)


def csv_row(point: Point) -> dict:
    env = point.header.get("env", {})
    row = {
        "config": point.config,
        "concurrency": point.concurrency,
        "rep": point.rep,
        "swap_used_bytes": env.get("swap_used_bytes"),
        "load_avg_1m": round(env.get("load_avg_1m", 0.0), 3),
        "series_file": point.path,
    }
    for column in CSV_COLUMNS:
        if column not in row:
            row[column] = point.summary.get(column)
    return row


def write_csv(path: str, points: list[Point]) -> None:
    ensure_parent(path)
    ordered = sorted(points, key=lambda p: (p.config, p.concurrency, p.rep))
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CSV_COLUMNS))
        writer.writeheader()
        for point in ordered:
            writer.writerow(csv_row(point))


def aggregate(points: list[Point], metric: str) -> dict:
    """One row per (config, concurrency): the median over repetitions is the
    reported value and the max is the observed ceiling. Both are kept because a
    saturation sweep is read for its ceiling but a median is what survives a
    noisy host."""
    grouped: dict[tuple[str, int], list[float]] = {}
    for point in points:
        value = point.summary.get(metric)
        if value is not None:
            grouped.setdefault((point.config, point.concurrency), []).append(value)
    table: dict[str, dict[int, dict]] = {}
    for (config, concurrency), values in sorted(grouped.items()):
        table.setdefault(config, {})[concurrency] = {
            "reps": len(values),
            "values": sorted(values),
            "median": round(statistics.median(values), 1),
            "min": round(min(values), 1),
            "max": round(max(values), 1),
        }
    return table


def write_wide_csv(path: str, table: dict) -> None:
    """A second CSV shaped for a spreadsheet chart: one row per concurrency,
    one column per config, so the plot is a two-click job in Excel."""
    ensure_parent(path)
    configs = sorted(table)
    concurrencies = sorted({c for config in configs for c in table[config]})
    header = ["concurrency"]
    for config in configs:
        header += [f"{config}_median", f"{config}_min", f"{config}_max", f"{config}_reps"]
    # Header on row 1 with nothing above it: a spreadsheet picks the columns up
    # as a table only if the first row is the header, and comment rows ahead of
    # it turn the whole import into a manual step. The caveats travel in the
    # JSON sidecar and the README instead.
    with open(path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for concurrency in concurrencies:
            row = [concurrency]
            for config in configs:
                cell = table[config].get(concurrency)
                row += ([cell["median"], cell["min"], cell["max"], cell["reps"]]
                        if cell else ["", "", "", 0])
            writer.writerow(row)


def ensure_parent(path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)


def plot(path: str, table: dict, args: argparse.Namespace) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from ftsbench import plotlib

    figure, axes = plt.subplots(figsize=(11.0, 7.0))
    for index, config in enumerate(sorted(table)):
        draw_config(axes, config, table[config], index, plotlib)
    finish_axes(axes, table)
    annotate(figure, axes, args)
    ensure_parent(path)
    figure.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(figure)


def draw_config(axes, config: str, points: dict, index: int, plotlib) -> None:
    concurrencies = sorted(points)
    medians = [points[c]["median"] for c in concurrencies]
    lows = [points[c]["median"] - points[c]["min"] for c in concurrencies]
    highs = [points[c]["max"] - points[c]["median"] for c in concurrencies]
    style = plotlib.style_for(config, index)
    axes.errorbar(concurrencies, medians, yerr=[lows, highs], label=config,
                  marker="o", markersize=6, capsize=4, linewidth=2.0,
                  color=style["color"], linestyle=style["linestyle"])
    ceiling = max(medians)
    axes.annotate(f"{config} — ceiling ≈ {ceiling / 1000:.1f}k docs/s",
                  xy=(concurrencies[-1], medians[-1]),
                  xytext=(-10, 14 - 22 * index),
                  textcoords="offset points", ha="right", fontsize=9.5,
                  color=style["color"], weight="bold")


def finish_axes(axes, table: dict) -> None:
    concurrencies = sorted({c for config in table for c in table[config]})
    axes.set_xscale("log", base=2)
    axes.set_xticks(concurrencies)
    axes.set_xticklabels([str(c) for c in concurrencies])
    axes.set_xlabel("loader concurrency (see caveat below — not one quantity across engines)")
    axes.set_ylabel("build rate (docs/s)")
    axes.set_ylim(bottom=0)
    axes.grid(True, which="major", alpha=0.25, linewidth=0.6)
    axes.set_axisbelow(True)
    axes.legend(loc="center right", frameon=True, fontsize=10)


def annotate(figure, axes, args: argparse.Namespace) -> None:
    from ftsbench import plotlib
    axes.set_title(args.title, fontsize=14, loc="left", pad=18)
    axes.text(0.0, 1.02, "docs/s ceiling vs loader concurrency, median of repetitions "
                         "(bars span min..max)", transform=axes.transAxes,
              fontsize=9, color="#444444")
    footer = "\n".join(part for part in (AXIS_CAVEAT, COMMIT_NOTE, args.footer_extra) if part)
    figure.text(0.01, -0.02, footer, fontsize=7.8, color="#444444", ha="left", va="top",
                wrap=True)
    plotlib.stamp(figure)


def main() -> int:
    args = parse_args()
    paths = discover_series(args.data_dir)
    if not paths:
        print(f"no measured series in {args.data_dir}")
        return 1
    points = [p for p in (load_point(path) for path in paths) if p]
    if not points:
        print("no usable series")
        return 1
    table = aggregate(points, args.metric)

    write_csv(args.output_csv, points)
    wide = f"{os.path.splitext(args.output_csv)[0]}-wide.csv"
    write_wide_csv(wide, table)
    ensure_parent(args.output_json)
    with open(args.output_json, "w") as handle:
        json.dump({"metric": args.metric, "axis_caveat": AXIS_CAVEAT,
                   "commit_policy_note": COMMIT_NOTE, "configs": table,
                   "runs": [csv_row(p) for p in points]}, handle, indent=1)
    plot(args.output_png, table, args)

    print(f"per-run CSV  : {args.output_csv}  ({len(points)} runs)")
    print(f"wide CSV     : {wide}")
    print(f"JSON sidecar : {args.output_json}")
    print(f"plot         : {args.output_png}")
    for config in sorted(table):
        ceiling = max(v["median"] for v in table[config].values())
        print(f"  {config}: ceiling {ceiling:,.0f} docs/s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
