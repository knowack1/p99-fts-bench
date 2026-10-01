#!/usr/bin/env python3
"""p99 latency and throughput against concurrency, one facet per query class.

    search-latency/charts/search_by_class.py \\
        --series 'scylladb cql=<R>/d1-cql-disk/scylla/points/*.csv' \\
        --series 'scylladb vector-store=<R>/d2-vstore-disk/scylla/points/*.csv' \\
        --series 'opensearch=<R>/d3-os-disk/opensearch/points/*.csv' \\
        --metric p99 --output <R>/charts/search-p99-by-class.png \\
                    --table  <R>/charts/search-p99-by-class.csv

**Six facets, never one line.** The engine ranking in this campaign REVERSES
between query classes, so a chart that collapses the classes would report
whichever class happened to dominate the average. `query_class` selects the
facet and nothing is averaged across facets.

**Percentiles do not average.** With N reps a cell has N p99s and the p99 of the
union can only be computed from the samples, which live in `latencies/`. This
draws the MEDIAN of the per-rep percentile and shades min-max across reps, so
the rep spread is visible rather than smoothed away. The band is a spread, not
a confidence interval.

**Matrix rows only.** `fetch_documents=true` sweeps are a different measurement
(they move bytes the others do not) and are dropped rather than drawn beside
them; the table twin keeps the count that were dropped.

A cell with fewer reps than the campaign's N is drawn hollow and named in the
footer -- a partially measured arm must not look finished.
"""
from __future__ import annotations

import argparse
import glob
import statistics
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BENCH_DIR))
sys.path.insert(0, str(BENCH_DIR / "tools"))

from harness_charts import (MARKERS, draw_footer, label_right_edge,  # noqa: E402
                            read_csv_rows, read_preamble, write_table)

# Categorical slots 1-3 of the validated default palette. Categorical, NOT the
# `plasma` ramp the build-rate charts use: those series are rungs of one ordered
# knob, these are three different engines, and an ordered ramp would imply a
# sequence that does not exist. Validated all-pairs (small multiples put every
# pair on screen): worst CVD dE 9.2, worst normal-vision dE 24.0. The aqua slot
# sits at 2.74:1 on a light surface, so the relief rule applies and this chart
# ships both direct labels and the table twin.
SERIES_COLOURS = ["#2a78d6", "#eb6834", "#1baf7a"]

CLASSES = ["rare_term", "common_term", "phrase", "bool_and", "bool_not", "bool_mixed"]
METRICS = {
    "p50": ("p50_ms", "p50 latency (ms)", "service time"),
    "p90": ("p90_ms", "p90 latency (ms)", "service time"),
    "p99": ("p99_ms", "p99 latency (ms)", "service time"),
    "qps": ("queries_per_s", "throughput (queries/s)", "throughput"),
}


def parse_series(specs):
    out = []
    for spec in specs:
        name, seam, pattern = spec.partition("=")
        if not seam:
            raise SystemExit(f"--series wants 'name=glob', got {spec!r}")
        paths = sorted(glob.glob(pattern))
        if not paths:
            raise SystemExit(f"no CSVs matched {pattern!r} for series {name!r}")
        out.append((name, paths))
    return out


def cells_of(paths, column, limit):
    """{(class, concurrency): [value per rep]}, matrix rows at ONE top-k.

    The limit filter is not optional. A results directory holding k=10, k=100
    and k=1000 for the same arm would otherwise collapse three different
    measurements into one series, and nothing in the output would say so.
    """
    cells, dropped = {}, 0
    for path in paths:
        for row in read_csv_rows(path):
            if row.get("fetch_documents") == "true":
                dropped += 1
                continue
            if limit is not None and int(row["limit"]) != limit:
                continue
            if row.get(column) in (None, ""):
                continue
            key = (row["query_class"], int(row["concurrency"]))
            cells.setdefault(key, []).append(float(row[column]))
    return cells, dropped


def preamble_facts(paths):
    facts = {}
    for path in paths:
        for key, value in read_preamble(path).items():
            facts.setdefault(key, set()).add(value)
    return facts


def one_value(facts, key):
    values = facts.get(key, set())
    return values.pop() if len(values) == 1 else "|".join(sorted(values)) or "?"


def draw(args):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    column, ylabel, kind = METRICS[args.metric]
    series = parse_series(args.series)
    if args.limit is None:
        seen = {row["limit"] for _, paths in series for p in paths
                for row in read_csv_rows(p)}
        if len(seen) > 1:
            raise SystemExit(
                f"these points hold top-k values {sorted(seen)}; pass --limit to pick one "
                "(drawing them together would average three measurements into one line)")
    if len(series) > len(SERIES_COLOURS):
        raise SystemExit(f"{len(series)} series: the validated categorical order "
                         f"holds {len(SERIES_COLOURS)} for an all-pairs form")

    loaded = [(name, cells_of(paths, column, args.limit), preamble_facts(paths))
              for name, paths in series]
    classes = [k for k in CLASSES
               if any(key[0] == k for _, (cells, _), _ in loaded for key in cells)]

    figure, axeses = plt.subplots(2, 3, figsize=(15, 9), sharex=True)
    figure.patch.set_facecolor("#fcfcfb")
    thin = []
    unmeasured = []

    for index, klass in enumerate(classes):
        axes = axeses.flat[index]
        axes.set_facecolor("#fcfcfb")
        ends = []
        for slot, (name, (cells, _), _) in enumerate(loaded):
            levels = sorted(c for k, c in cells if k == klass)
            if not levels:
                unmeasured.append(f"{name}/{klass}")
                continue
            middles = [statistics.median(cells[(klass, c)]) for c in levels]
            lows = [min(cells[(klass, c)]) for c in levels]
            highs = [max(cells[(klass, c)]) for c in levels]
            reps = [len(cells[(klass, c)]) for c in levels]
            colour = SERIES_COLOURS[slot]
            axes.fill_between(levels, lows, highs, color=colour, alpha=0.15, linewidth=0)
            axes.plot(levels, middles, color=colour, linewidth=2.0,
                      marker=MARKERS[slot], markersize=5, label=name,
                      markerfacecolor=colour, markeredgecolor="#fcfcfb",
                      markeredgewidth=0.8, zorder=3)
            for level, middle, count in zip(levels, middles, reps):
                if count < args.reps:
                    axes.plot([level], [middle], marker=MARKERS[slot], markersize=7,
                              markerfacecolor="#fcfcfb", markeredgecolor=colour,
                              markeredgewidth=1.6, zorder=4)
                    thin.append(f"{name}/{klass}/c{level} n={count}")
            ends.append((levels[-1], middles[-1], name, colour))

        axes.set_xscale("log", base=2)
        axes.set_yscale("log")
        axes.set_xticks([1, 2, 4, 8, 16, 32, 64, 128])
        axes.set_xticklabels(["1", "2", "4", "8", "16", "32", "64", "128"])
        axes.grid(True, which="major", color="#d8d7d2", linewidth=0.6, alpha=0.7)
        axes.grid(True, which="minor", color="#ebeae5", linewidth=0.4, alpha=0.5)
        axes.set_axisbelow(True)
        for spine in ("top", "right"):
            axes.spines[spine].set_visible(False)
        for spine in ("left", "bottom"):
            axes.spines[spine].set_color("#b5b4ae")
        axes.tick_params(colors="#52514e", labelsize=8)
        missing = [n for n, (cells, _), _ in loaded
                   if not any(k == klass for k, _ in cells)]
        if missing:
            axes.text(0.5, 0.06, "not yet measured: " + ", ".join(missing),
                      transform=axes.transAxes, ha="center", fontsize=8,
                      color="#52514e", style="italic")
        title = klass + ("   ← headline" if klass == "rare_term" else "")
        axes.set_title(title, fontsize=10, color="#0b0b0b", loc="left")
        if index % 3 == 0:
            axes.set_ylabel(ylabel, fontsize=9, color="#52514e")
        if index >= 3:
            axes.set_xlabel("concurrency (requests in flight)", fontsize=9, color="#52514e")
        label_right_edge(axes, ends, log_y=True)

    for spare in range(len(classes), 6):
        axeses.flat[spare].set_visible(False)

    handles, labels = axeses.flat[0].get_legend_handles_labels()
    figure.legend(handles, labels, loc="upper right", frameon=False,
                  fontsize=9, ncol=len(labels), bbox_to_anchor=(0.99, 0.985))
    figure.suptitle(args.title, fontsize=13, x=0.02, ha="left", y=0.98,
                    color="#0b0b0b")

    facts = loaded[0][2]
    footer = [
        f"PRELIMINARY -- not quotable. Closed loop: {kind} of a settled index, "
        f"N={args.reps} reps, line = median of the per-rep value, band = min-max across reps."
        + (" Percentiles do not average; a merged percentile needs the samples in latencies/."
           if args.metric != "qps" else ""),
        "BOTH INDEXES ARE ON DISK (NVMe, same box). This is the matched-storage comparison and it is "
        "NOT how ScyllaDB FTS ships today, which answers from RAM. Container memory is at parity "
        "(28 GiB each); the ScyllaDB arms answer from two containers and 8 cores, the OpenSearch arm "
        "from one and 4 -- vector-store is the matched arm.",
        f"limit={args.limit if args.limit is not None else one_value(facts, 'limit')} "
        "(top-k is a constant on this chart, not an axis); "
        f"fetch_documents=false on every row drawn; analyzer parity verified; "
        "the CQL arm prepared its statements while OpenSearch re-parsed every request -- that is not parser parity.",
        "Closed loop offers nothing on a schedule, so coordinated omission does not apply and these may not be "
        "plotted beside open-loop numbers. rare_term is the cheapest query in the set.",
    ]
    if thin:
        footer.append("Hollow marker = fewer reps than N, arm still running: " + "; ".join(thin[:8])
                      + (f" (+{len(thin) - 8} more)" if len(thin) > 8 else ""))
    if unmeasured:
        footer.append("NOT YET MEASURED (absent from the facet, not a failure): "
                      + "; ".join(sorted(set(unmeasured))))
    draw_footer(figure, footer)

    figure.tight_layout(rect=(0, 0.13, 1, 0.95))
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(args.output, dpi=150, facecolor=figure.get_facecolor())

    rows = []
    for klass in classes:
        for level in [1, 2, 4, 8, 16, 32, 64, 128]:
            for name, (cells, _), _ in loaded:
                values = cells.get((klass, level))
                if not values:
                    continue
                rows.append([klass, level, name, len(values),
                             round(statistics.median(values), 3),
                             round(min(values), 3), round(max(values), 3)])
    write_table(args.table, ["query_class", "concurrency", "series", "reps",
                             f"{column}_median", f"{column}_min", f"{column}_max"], rows)
    print(f"wrote {args.output} and {args.table}")
    if thin:
        print(f"  {len(thin)} cell(s) below N={args.reps}, drawn hollow")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--series", action="append", required=True,
                        help="'name=glob' of point CSVs; repeatable, max 3")
    parser.add_argument("--metric", choices=sorted(METRICS), default="p99")
    parser.add_argument("--limit", type=int, default=None,
                        help="top-k to draw; REQUIRED when the points hold more "
                             "than one, or three measurements average into one line")
    parser.add_argument("--reps", type=int, default=3, help="the campaign's N")
    parser.add_argument("--title", default="")
    parser.add_argument("--output", required=True)
    parser.add_argument("--table", required=True)
    draw(parser.parse_args())


if __name__ == "__main__":
    main()
