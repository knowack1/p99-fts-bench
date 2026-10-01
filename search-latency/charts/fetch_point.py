#!/usr/bin/env python3
"""p50, p99 and throughput for the `--fetch-documents` point, one figure.

    search-latency/charts/fetch_point.py \\
        --series 'scylladb cql=<R>/d1-cql-disk/scylla/points/*fetch-rare_term*.csv' \\
        --output <R>/charts/fetch-rare_term-point.png \\
        --table  <R>/charts/fetch-rare_term-point.csv

The fetch axis is a single point (one concurrency, one query class,
`fetch_documents=true`), not a sweep -- see `SEARCH-LATENCY-DISK-RUNBOOK.md`,
"The three arms". This draws three panels (p50, p99, queries/s), each with one
marker per `--series` at the median across reps and a min-max whisker, the
same summary convention `search_by_class.py` uses for a sweep level. A cell
with fewer reps than `--reps` is drawn hollow.
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

from harness_charts import draw_footer, read_csv_rows, read_preamble, write_table  # noqa: E402

# Categorical slot 1 of the validated default palette -- see search_by_class.py
# for the all-pairs validation this order carries.
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


def rows_of(paths):
    """The fetch-point rows across reps -- one row per rep, all one cell."""
    rows = []
    for path in paths:
        for row in read_csv_rows(path):
            if row.get("fetch_documents") != "true":
                raise SystemExit(f"{path}: fetch_documents != true, wrong CSV for this chart")
            rows.append(row)
    return rows


def one_value(facts, key):
    values = facts.get(key, set())
    return values.pop() if len(values) == 1 else "|".join(sorted(values)) or "?"


def preamble_facts(paths):
    facts = {}
    for path in paths:
        for key, value in read_preamble(path).items():
            facts.setdefault(key, set()).add(value)
    return facts


def draw(args):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    series = parse_series(args.series)
    if len(series) > len(SERIES_COLOURS):
        raise SystemExit(f"{len(series)} series: the validated categorical order "
                         f"holds {len(SERIES_COLOURS)} for an all-pairs form")

    loaded = [(name, rows_of(paths), preamble_facts(paths)) for name, paths in series]
    positions = list(range(len(loaded)))
    thin = []

    figure, axeses = plt.subplots(1, 3, figsize=(12, 4.2))
    figure.patch.set_facecolor("#fcfcfb")

    for panel, (column, ylabel) in enumerate(METRICS):
        axes = axeses[panel]
        axes.set_facecolor("#fcfcfb")
        for position, (name, rows, _) in zip(positions, loaded):
            values = [float(row[column]) for row in rows]
            colour = SERIES_COLOURS[position]
            middle = statistics.median(values)
            low, high = min(values), max(values)
            axes.errorbar([position], [middle], yerr=[[middle - low], [high - middle]],
                          fmt="o", color=colour, markersize=8, markerfacecolor=colour,
                          markeredgecolor="#fcfcfb", markeredgewidth=0.8,
                          ecolor=colour, elinewidth=1.6, capsize=5, zorder=3)
            if len(values) < args.reps:
                axes.plot([position], [middle], marker="o", markersize=11,
                          markerfacecolor="#fcfcfb", markeredgecolor=colour,
                          markeredgewidth=1.8, zorder=4)
                thin.append(f"{name}/{column} n={len(values)}")
            axes.annotate(f"{middle:.3g}", xy=(position, high),
                          xytext=(0, 6), textcoords="offset points",
                          ha="center", fontsize=8, color=colour)

        axes.set_xlim(-0.6, len(loaded) - 0.4)
        axes.set_xticks(positions)
        axes.set_xticklabels([name for name, _, _ in loaded], fontsize=9, color="#0b0b0b")
        axes.set_ylabel(ylabel, fontsize=9, color="#52514e")
        axes.grid(True, axis="y", which="major", color="#d8d7d2", linewidth=0.6, alpha=0.7)
        axes.set_axisbelow(True)
        for spine in ("top", "right"):
            axes.spines[spine].set_visible(False)
        for spine in ("left", "bottom"):
            axes.spines[spine].set_color("#b5b4ae")
        axes.tick_params(colors="#52514e", labelsize=8)
        low, high = axes.get_ylim()
        axes.set_ylim(0, high * 1.12)

    facts = loaded[0][2]
    query_class = one_value(facts, "query_classes")
    concurrency = one_value(facts, "concurrency")
    limit = one_value(facts, "limit")
    figure.suptitle(args.title or f"fetch_documents=true, {query_class}, c={concurrency}",
                    fontsize=13, x=0.02, ha="left", y=0.99, color="#0b0b0b")

    footer = [
        f"PRELIMINARY -- not quotable. Single point (not a sweep): concurrency={concurrency}, "
        f"query_class={query_class}, limit={limit}, fetch_documents=true on every row drawn. "
        f"N={args.reps} reps, marker = median across reps, whisker = min-max across reps.",
        "This projects `title, body` on every hit, which is a transfer cost on top of the search "
        "itself -- compare against the equivalent ladder point at the same concurrency and class "
        "(fetch_documents=false) to see what the projection added, not this chart alone.",
        "BOTH INDEXES ARE ON DISK (NVMe, same box); this is not how ScyllaDB FTS ships today, "
        "which answers from RAM.",
    ]
    if thin:
        footer.append("Hollow marker = fewer reps than N, arm still running: " + "; ".join(thin))
    draw_footer(figure, footer)

    figure.tight_layout(rect=(0, 0.14, 1, 0.89))
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(args.output, dpi=150, facecolor=figure.get_facecolor())

    rows = []
    for name, data_rows, _ in loaded:
        for column, _ in METRICS:
            values = [float(row[column]) for row in data_rows]
            rows.append([name, column, len(values), round(statistics.median(values), 3),
                         round(min(values), 3), round(max(values), 3)])
    write_table(args.table, ["series", "metric", "reps", "median", "min", "max"], rows)
    print(f"wrote {args.output} and {args.table}")
    if thin:
        print(f"  {len(thin)} cell(s) below N={args.reps}, drawn hollow")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--series", action="append", required=True,
                        help="'name=glob' of the fetch point's rep CSVs; repeatable, max 3")
    parser.add_argument("--reps", type=int, default=3, help="the campaign's N")
    parser.add_argument("--title", default="")
    parser.add_argument("--output", required=True)
    parser.add_argument("--table", required=True)
    draw(parser.parse_args())


if __name__ == "__main__":
    main()
