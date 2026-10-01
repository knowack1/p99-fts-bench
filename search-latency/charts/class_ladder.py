#!/usr/bin/env python3
"""p50, p99 and throughput against concurrency, for one query class.

    search-latency/charts/class_ladder.py \\
        --series 'scylladb cql=<R>/d1-cql-disk/scylla/points/*ladder-rare_term*.csv' \\
        --class rare_term \\
        --output <R>/charts/rare_term-ladder.png \\
        --table  <R>/charts/rare_term-ladder.csv

Sibling of `search_by_class.py`, which facets all six classes at once for an
all-engines comparison. This draws one class only, as three metric panels
(p50, p99, queries/s) side by side rather than one metric per invocation --
the natural shape when the ask is "how does this one class behave", not "how
does the ranking change class to class". Same summary convention: line =
median of the per-rep value across concurrency, band = min-max across reps, a
cell with fewer reps than `--reps` is drawn hollow. `fetch_documents=true`
rows are dropped -- that is a different measurement, see `fetch_point.py`.
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

# Categorical slots 1-3 of the validated default palette -- see
# search_by_class.py for the all-pairs validation this order carries.
SERIES_COLOURS = ["#2a78d6", "#eb6834", "#1baf7a"]

METRICS = [
    ("p50_ms", "p50 latency (ms)"),
    ("p99_ms", "p99 latency (ms)"),
    ("queries_per_s", "throughput (queries/s)"),
]


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


def cells_of(paths, klass, column):
    """{concurrency: [value per rep]}, matrix rows only, one class."""
    cells, dropped = {}, 0
    for path in paths:
        for row in read_csv_rows(path):
            if row.get("fetch_documents") == "true":
                dropped += 1
                continue
            if row.get("query_class") != klass:
                continue
            if row.get(column) in (None, ""):
                continue
            cells.setdefault(int(row["concurrency"]), []).append(float(row[column]))
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

    series = parse_series(args.series)
    if len(series) > len(SERIES_COLOURS):
        raise SystemExit(f"{len(series)} series: the validated categorical order "
                         f"holds {len(SERIES_COLOURS)} for an all-pairs form")

    loaded = [(name, paths, preamble_facts(paths)) for name, paths in series]
    thin = []

    figure, axeses = plt.subplots(1, 3, figsize=(15, 4.6))
    figure.patch.set_facecolor("#fcfcfb")

    for panel, (column, ylabel) in enumerate(METRICS):
        axes = axeses[panel]
        axes.set_facecolor("#fcfcfb")
        ends = []
        for slot, (name, paths, _) in enumerate(loaded):
            cells, _ = cells_of(paths, args.klass, column)
            levels = sorted(cells)
            if not levels:
                continue
            middles = [statistics.median(cells[c]) for c in levels]
            lows = [min(cells[c]) for c in levels]
            highs = [max(cells[c]) for c in levels]
            reps = [len(cells[c]) for c in levels]
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
                    thin.append(f"{name}/c{level}/{column} n={count}")
            ends.append((levels[-1], middles[-1], name, colour))

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
        axes.set_ylabel(ylabel, fontsize=9, color="#52514e")
        axes.set_xlabel("concurrency (requests in flight)", fontsize=9, color="#52514e")
        low, high = axes.get_ylim()
        axes.set_ylim(0, high)
        label_right_edge(axes, ends, log_y=False)

    handles, labels = axeses[0].get_legend_handles_labels()
    if len(labels) > 1:
        figure.legend(handles, labels, loc="upper right", frameon=False,
                      fontsize=9, ncol=len(labels), bbox_to_anchor=(0.99, 0.985))

    facts = loaded[0][2]
    figure.suptitle(args.title or f"{args.klass}, ladder", fontsize=13, x=0.02,
                    ha="left", y=0.99, color="#0b0b0b")

    footer = [
        f"PRELIMINARY -- not quotable. Closed loop: service time of a settled index, "
        f"N={args.reps} reps, line = median of the per-rep value, band = min-max across reps.",
        "BOTH INDEXES ARE ON DISK (NVMe, same box). This is the matched-storage comparison and it is "
        "NOT how ScyllaDB FTS ships today, which answers from RAM.",
        f"limit={one_value(facts, 'limit')} (top-k is a constant here, not an axis); "
        "fetch_documents=false on every row drawn; analyzer parity verified.",
        "Closed loop offers nothing on a schedule, so coordinated omission does not apply.",
    ]
    if thin:
        footer.append("Hollow marker = fewer reps than N, arm still running: " + "; ".join(thin[:8])
                      + (f" (+{len(thin) - 8} more)" if len(thin) > 8 else ""))
    draw_footer(figure, footer)

    figure.tight_layout(rect=(0, 0.16, 1, 0.89))
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(args.output, dpi=150, facecolor=figure.get_facecolor())

    rows = []
    for level in [1, 2, 4, 8, 16, 32, 64, 128]:
        for name, paths, _ in loaded:
            for column, _ in METRICS:
                cells, _ = cells_of(paths, args.klass, column)
                values = cells.get(level)
                if not values:
                    continue
                rows.append([args.klass, level, name, column, len(values),
                             round(statistics.median(values), 3),
                             round(min(values), 3), round(max(values), 3)])
    write_table(args.table, ["query_class", "concurrency", "series", "metric", "reps",
                             "median", "min", "max"], rows)
    print(f"wrote {args.output} and {args.table}")
    if thin:
        print(f"  {len(thin)} cell(s) below N={args.reps}, drawn hollow")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--series", action="append", required=True,
                        help="'name=glob' of ladder point CSVs; repeatable, max 3")
    parser.add_argument("--class", dest="klass", required=True, help="query_class to draw")
    parser.add_argument("--reps", type=int, default=3, help="the campaign's N")
    parser.add_argument("--title", default="")
    parser.add_argument("--output", required=True)
    parser.add_argument("--table", required=True)
    draw(parser.parse_args())


if __name__ == "__main__":
    main()
