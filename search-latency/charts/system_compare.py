#!/usr/bin/env python3
"""p50, p99 or throughput against concurrency, one line per system under test.

    search-latency/charts/system_compare.py \\
        --series 'os-nosource=<R>/d4-os-nosource/opensearch/points/*ladder-rare_term*.csv' \\
        --series 'os-source=<R>/d5-os-source/opensearch/points/*ladder-rare_term*.csv' \\
        --series 'cql-disk=<R>/d6-cql-disk/scylla/points/*ladder-rare_term*.csv' \\
        --series 'vstore-disk=<R>/d7-vstore-disk/scylla/points/*ladder-rare_term*.csv' \\
        --series 'os-2shard=<R>/d8-os-2shard/opensearch/points/*ladder-rare_term*.csv' \\
        --series 'vstore-ram=<R>/d9-vstore-ram/scylla/points/*ladder-rare_term*.csv' \\
        --class rare_term --limit 10 --metric p99 \\
        --output <R>/charts/system-p99-rare_term-k10.png \\
        --table  <R>/charts/system-p99-rare_term-k10.csv

Sibling of `class_ladder.py` (one class, three metric panels, up to three
engines) and `search_by_class.py` (six class facets, up to three engines).
This one is for the opposite comparison: many SYSTEM CONFIGURATIONS
(index-on-disk variants, shard counts, source on/off, ...) rather than
different engines, at ONE query class and ONE top-k. It draws a single panel
rather than faceting query classes, on purpose: faceting six classes with six
series at once is a small-multiples form, and the validated categorical order
does not clear the CVD all-pairs floor past three slots for that form (see
`references/palette.md` in the dataviz skill). A single-panel line chart with
a fixed legend is the "lines" form instead, which only needs adjacent-pair
safety -- so this trades the six-class facet for up to six systems on one
class.

**Colour order for 6 series is a curated subset of the 8-slot categorical
palette, not slots 1-6.** Slots 1-6 in palette order put orange (slot 2) next
to green (slot 6) and red (slot 8), which fails the CVD all-pairs floor
(Delta E 3.2, deuteranopia). Dropping orange and magenta (slots 2 and 5) and
keeping the rest in their original relative order clears all 15 pairs at
Delta E >= 6.9 (deutan) and >= 15.6 (normal vision) -- validated with
`validate_palette.js "<hexes>" --mode light --pairs all`. It sits in the 6-8
Delta E warning band, which the palette doc says is legal only with secondary
encoding: this script always draws a distinct marker shape and a direct
end-of-line label per series (never colour alone), and ships the table twin.

**Percentiles do not average.** With N reps a cell has N p99s; this draws the
median of the per-rep percentile and shades min-max across reps.

**Matrix rows only.** `fetch_documents=true` rows are dropped.

A cell with fewer reps than `--reps` is drawn hollow and named in the footer.
A series with no data for this class/limit is named "not yet measured" rather
than silently absent -- a partially run campaign must not look finished.
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

# Categorical slots 1,3,4,6,7,8 of the validated default palette (blue, aqua,
# yellow, green, violet, red) -- see the module docstring for why slots 2
# (orange) and 5 (magenta) are dropped rather than reordered.
SERIES_COLOURS = ["#2a78d6", "#1baf7a", "#eda100", "#008300", "#4a3aa7", "#e34948"]

METRICS = {
    "p50": ("p50_ms", "p50 latency (ms)"),
    "p90": ("p90_ms", "p90 latency (ms)"),
    "p99": ("p99_ms", "p99 latency (ms)"),
    "qps": ("queries_per_s", "throughput (queries/s)"),
}


def parse_series(specs):
    out = []
    for spec in specs:
        name, seam, pattern = spec.partition("=")
        if not seam:
            raise SystemExit(f"--series wants 'name=glob', got {spec!r}")
        out.append((name, sorted(glob.glob(pattern))))
    return out


def cells_of(paths, klass, limit, column):
    """{concurrency: [value per rep]}, matrix rows only, one class, one top-k."""
    cells, dropped = {}, 0
    for path in paths:
        for row in read_csv_rows(path):
            if row.get("fetch_documents") == "true":
                dropped += 1
                continue
            if row.get("query_class") != klass:
                continue
            if int(row["limit"]) != limit:
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

    column, ylabel = METRICS[args.metric]
    series = parse_series(args.series)
    if len(series) > len(SERIES_COLOURS):
        raise SystemExit(f"{len(series)} series: this chart's validated colour "
                         f"order holds {len(SERIES_COLOURS)}")

    loaded = [(name, cells_of(paths, args.klass, args.limit, column) if paths
               else ({}, 0), preamble_facts(paths)) for name, paths in series]

    figure, axes = plt.subplots(1, 1, figsize=(9, 6))
    figure.patch.set_facecolor("#fcfcfb")
    axes.set_facecolor("#fcfcfb")

    thin, unmeasured, ends = [], [], []
    for slot, (name, (cells, _), _) in enumerate(loaded):
        levels = sorted(cells)
        if not levels:
            unmeasured.append(name)
            continue
        middles = [statistics.median(cells[c]) for c in levels]
        lows = [min(cells[c]) for c in levels]
        highs = [max(cells[c]) for c in levels]
        reps = [len(cells[c]) for c in levels]
        colour = SERIES_COLOURS[slot]
        axes.fill_between(levels, lows, highs, color=colour, alpha=0.15, linewidth=0)
        axes.plot(levels, middles, color=colour, linewidth=2.0,
                  marker=MARKERS[slot], markersize=6, label=name,
                  markerfacecolor=colour, markeredgecolor="#fcfcfb",
                  markeredgewidth=0.8, zorder=3)
        for level, middle, count in zip(levels, middles, reps):
            if count < args.reps:
                axes.plot([level], [middle], marker=MARKERS[slot], markersize=8,
                          markerfacecolor="#fcfcfb", markeredgecolor=colour,
                          markeredgewidth=1.6, zorder=4)
                thin.append(f"{name}/c{level} n={count}")
        ends.append((levels[-1], middles[-1], name, colour))

    axes.set_xscale("log", base=2)
    axes.set_xticks([1, 2, 4, 8, 16, 32, 64, 128])
    axes.set_xticklabels(["1", "2", "4", "8", "16", "32", "64", "128"])
    if args.metric != "qps":
        axes.set_yscale("log")
    axes.grid(True, which="major", color="#d8d7d2", linewidth=0.6, alpha=0.7)
    axes.grid(True, which="minor", color="#ebeae5", linewidth=0.4, alpha=0.5)
    axes.set_axisbelow(True)
    for spine in ("top", "right"):
        axes.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        axes.spines[spine].set_color("#b5b4ae")
    axes.tick_params(colors="#52514e", labelsize=9)
    axes.set_ylabel(ylabel, fontsize=10, color="#52514e")
    axes.set_xlabel("concurrency (requests in flight)", fontsize=10, color="#52514e")
    label_right_edge(axes, ends, log_y=(args.metric != "qps"))
    if unmeasured:
        axes.text(0.5, 0.04, "not yet measured: " + ", ".join(unmeasured),
                  transform=axes.transAxes, ha="center", fontsize=9,
                  color="#52514e", style="italic")

    handles, labels = axes.get_legend_handles_labels()
    if len(labels) > 1:
        figure.legend(handles, labels, loc="upper right", frameon=False,
                      fontsize=9, ncol=min(len(labels), 3), bbox_to_anchor=(0.99, 0.985))

    facts = next((f for _, _, f in loaded if f), {})
    figure.suptitle(args.title or f"{args.klass}, k={args.limit}, systems compared",
                    fontsize=13, x=0.02, ha="left", y=0.98, color="#0b0b0b")

    footer = [
        f"PRELIMINARY -- not quotable. Closed loop: service time of a settled index, "
        f"N={args.reps} reps, line = median of the per-rep value, band = min-max across reps."
        + (" Percentiles do not average; a merged percentile needs the samples in latencies/."
           if args.metric != "qps" else ""),
        f"query_class={args.klass}, limit={args.limit} held constant, not an axis; "
        "fetch_documents=false on every row drawn.",
        "6-series colour order sits in the CVD 6-8 Delta E warning band (all-pairs); "
        "identity is carried by marker shape and the direct end labels too, not colour alone -- "
        "see the table twin for exact values.",
        "Closed loop offers nothing on a schedule, so coordinated omission does not apply.",
    ]
    if thin:
        footer.append("Hollow marker = fewer reps than N, arm still running: " + "; ".join(thin[:8])
                      + (f" (+{len(thin) - 8} more)" if len(thin) > 8 else ""))
    if unmeasured:
        footer.append("NOT YET MEASURED (absent from the chart, not a failure): "
                      + ", ".join(unmeasured))
    draw_footer(figure, footer)

    figure.tight_layout(rect=(0, 0.15, 1, 0.93))
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(args.output, dpi=150, facecolor=figure.get_facecolor())

    rows = []
    for level in [1, 2, 4, 8, 16, 32, 64, 128]:
        for name, (cells, _), _ in loaded:
            values = cells.get(level)
            if not values:
                continue
            rows.append([args.klass, args.limit, level, name, len(values),
                         round(statistics.median(values), 3),
                         round(min(values), 3), round(max(values), 3)])
    write_table(args.table, ["query_class", "limit", "concurrency", "series", "reps",
                             f"{column}_median", f"{column}_min", f"{column}_max"], rows)
    print(f"wrote {args.output} and {args.table}")
    if thin:
        print(f"  {len(thin)} cell(s) below N={args.reps}, drawn hollow")
    if unmeasured:
        print(f"  not yet measured: {', '.join(unmeasured)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--series", action="append", required=True,
                        help="'name=glob' of ladder point CSVs; repeatable, max 6. "
                             "A glob matching nothing is drawn as 'not yet measured' "
                             "rather than refused, for a campaign still in flight.")
    parser.add_argument("--class", dest="klass", required=True, help="query_class to draw")
    parser.add_argument("--limit", type=int, required=True, help="top-k to draw")
    parser.add_argument("--metric", choices=sorted(METRICS), default="p99")
    parser.add_argument("--reps", type=int, default=3, help="the campaign's N")
    parser.add_argument("--title", default="")
    parser.add_argument("--output", required=True)
    parser.add_argument("--table", required=True)
    draw(parser.parse_args())


if __name__ == "__main__":
    main()
