#!/usr/bin/env python3
"""Every harness series on one axis: x concurrency, y docs/s.

A DIAGNOSTIC chart, not a deck chart. It deliberately draws **all** series —
`scyllarate` plus one `osrate` line per `--batch-size` — because the question it
answers is "what does this box's loader do, everywhere", and picking a legible
subset is a later, separate decision made for a slide.

That is also why it does not use `ftsbench.plotlib`'s deck palette or its
`CONFIG_STYLES`: those exist to keep four or five lines apart on a projector,
and refuse the eight lines wanted here. Identity is carried by three redundant
channels instead — an ordered colour ramp, a per-series marker, and a direct
label at each line's right end — so no reader has to resolve two similar
oranges.

**Concurrency does not mean the same thing on both engines**, and the footer
says so: one `scyllarate` unit is one in-flight prepared INSERT carrying one
document, one `osrate` unit is one in-flight `_bulk` carrying `batch_size` of
them. The x axis is a client-side knob, not a workload, and the only level
where the two are the same shape is `batch=1`. The chart is honest about a
comparison it cannot make for the reader.

    .venv/bin/python3 tools/plot_harness_grid.py \\
        --scylla     '<R>/scylla/points/default-*-rep*.csv' \\
        --opensearch '<R>/opensearch/points/os-*-rep*.csv' \\
        --output     '<R>/harness-grid.png' \\
        --table      '<R>/harness-grid.csv'
"""
from __future__ import annotations

import argparse
import glob
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness_charts import (MARKERS, colours, draw_footer,  # noqa: E402
                            label_right_edge, read_csv_rows, write_table)

SCYLLA_SERIES = "scyllarate CQL 1 doc/op"
SCYLLA_COLOR = "#2b6cb0"
TABLE_COLUMNS = ["series", "concurrency", "reps", "docs_per_s_median",
                 "docs_per_s_min", "docs_per_s_max", "shortest_wall_s"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scylla", default="", help="glob of scyllarate CSVs")
    parser.add_argument("--opensearch", default="", help="glob of osrate CSVs")
    parser.add_argument("--output", required=True, help="PNG path")
    parser.add_argument("--table", default="", help="also write every plotted point as CSV")
    parser.add_argument("--keep-warmup", action="store_true",
                        help="plot the ladder's leading warm-up row too; by "
                             "default the first data row of each CSV is dropped")
    parser.add_argument("--title", default="Harness submit rate against the null sink")
    parser.add_argument("--subtitle", default="")
    parser.add_argument("--width", type=float, default=12.0)
    parser.add_argument("--height", type=float, default=7.5)
    parser.add_argument("--dpi", type=int, default=160)
    return parser.parse_args()


def read_points(path: Path, keep_warmup: bool) -> list[dict]:
    rows = read_csv_rows(path)
    return rows if keep_warmup else rows[1:]


def series_of(row: dict) -> str:
    batch = row.get("batch_size")
    return f"osrate batch={int(batch)}" if batch else SCYLLA_SERIES


def collect(pattern: str, keep_warmup: bool) -> list[tuple[str, int, float, float]]:
    out = []
    for name in sorted(glob.glob(pattern)):
        for row in read_points(Path(name), keep_warmup):
            out.append((series_of(row), int(row["concurrency"]),
                        float(row["docs_per_s"]), float(row["wall_s"])))
    return out


def aggregate(points: list[tuple[str, int, float, float]]) -> dict[str, dict[int, dict]]:
    grouped = defaultdict(list)
    for series, concurrency, rate, wall in points:
        grouped[(series, concurrency)].append((rate, wall))
    table: dict[str, dict[int, dict]] = defaultdict(dict)
    for (series, concurrency), values in grouped.items():
        rates = [rate for rate, _ in values]
        table[series][concurrency] = {
            "reps": len(rates),
            "median": statistics.median(rates),
            "min": min(rates),
            "max": max(rates),
            "shortest_wall_s": min(wall for _, wall in values),
        }
    return table


def series_order(table: dict[str, dict[int, dict]]) -> list[str]:
    """ScyllaDB first, then the osrate lines by ascending batch size.

    Ordered so the colour ramp runs with the batch axis rather than with
    whatever order the files were globbed in.
    """
    batches = sorted(
        (int(name.split("=")[1]) for name in table if name != SCYLLA_SERIES)
    )
    ordered = [f"osrate batch={batch}" for batch in batches]
    return ([SCYLLA_SERIES] if SCYLLA_SERIES in table else []) + ordered


def draw_series(axes, name: str, levels: dict[int, dict], colour, marker) -> tuple:
    xs = sorted(levels)
    ys = [levels[x]["median"] / 1000.0 for x in xs]
    lows = [(levels[x]["median"] - levels[x]["min"]) / 1000.0 for x in xs]
    highs = [(levels[x]["max"] - levels[x]["median"]) / 1000.0 for x in xs]
    axes.errorbar(xs, ys, yerr=[lows, highs], color=colour, marker=marker,
                  markersize=6, linewidth=1.8, capsize=3, elinewidth=0.9,
                  label=name, zorder=3)
    return (xs[-1], ys[-1], name, colour)


def footer_lines(table: dict[str, dict[int, dict]], short: list[str]) -> list[str]:
    lines = [
        "x is a CLIENT knob and does not mean the same thing on both engines: one "
        "scyllarate unit is one in-flight prepared INSERT carrying ONE document, "
        "one osrate unit is one in-flight _bulk carrying batch_size of them.",
        "Documents in flight is concurrency x batch_size. batch=1 is the only "
        "level where the two engines' x axes are the same shape.",
        "Point is the median of the repetitions, bar is min..max. The ladder's "
        "leading warm-up row is dropped. Null sink only - no engine ran, so no "
        "number here is an engine number.",
    ]
    if short:
        shown = "; ".join(short[:6])
        more = f" ... and {len(short) - 6} more" if len(short) > 6 else ""
        lines.append(f"SHORT POINTS -- under 3 s, not a measurement ({len(short)} "
                     f"of {sum(len(v) for v in table.values())} points): {shown}{more}"
                     f"  -> raise --max-docs for these levels and re-run them.")
    return lines


def short_points(table: dict[str, dict[int, dict]]) -> list[str]:
    return [
        f"{name} c={concurrency} ({levels[concurrency]['shortest_wall_s']:.1f}s)"
        for name, levels in table.items()
        for concurrency in sorted(levels)
        if levels[concurrency]["shortest_wall_s"] < 3.0
    ]


def table_rows(table: dict[str, dict[int, dict]], order: list[str]) -> list[list]:
    return [
        [name, concurrency, table[name][concurrency]["reps"],
         f"{table[name][concurrency]['median']:.1f}",
         f"{table[name][concurrency]['min']:.1f}",
         f"{table[name][concurrency]['max']:.1f}",
         f"{table[name][concurrency]['shortest_wall_s']:.3f}"]
        for name in order
        for concurrency in sorted(table[name])
    ]


def main() -> int:
    args = parse_args()
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    points = []
    if args.scylla:
        points += collect(args.scylla, args.keep_warmup)
    if args.opensearch:
        points += collect(args.opensearch, args.keep_warmup)
    if not points:
        print("no points matched --scylla / --opensearch")
        return 1

    table = aggregate(points)
    order = series_order(table)
    warm = colours(len([name for name in order if name != SCYLLA_SERIES]))

    figure, axes = plt.subplots(figsize=(args.width, args.height), dpi=args.dpi)
    ends: list[tuple] = []
    warm_index = 0
    for index, name in enumerate(order):
        if name == SCYLLA_SERIES:
            colour = SCYLLA_COLOR
        else:
            colour = warm[warm_index]
            warm_index += 1
        ends.append(draw_series(axes, name, table[name], colour,
                                MARKERS[index % len(MARKERS)]))

    axes.set_xscale("log", base=2)
    axes.set_xticks(sorted({c for levels in table.values() for c in levels}))
    axes.get_xaxis().set_major_formatter(FuncFormatter(lambda v, _: f"{int(v)}"))
    axes.set_xlabel("concurrency  (in-flight requests, log2)")
    axes.set_ylabel("docs/s (thousands)")
    axes.set_ylim(bottom=0)
    axes.grid(True, which="major", linewidth=0.4, alpha=0.4)
    axes.set_axisbelow(True)
    axes.set_title(args.title + (f"\n{args.subtitle}" if args.subtitle else ""),
                   fontsize=11, loc="left")
    axes.legend(fontsize=8, ncol=2, loc="upper left", framealpha=0.9)
    label_right_edge(axes, ends)
    figure.subplots_adjust(right=0.78, bottom=0.30)
    draw_footer(figure, footer_lines(table, short_points(table)))
    figure.savefig(args.output)
    print(f"wrote {args.output}")
    if args.table:
        write_table(Path(args.table), TABLE_COLUMNS, table_rows(table, order))
        print(f"wrote {args.table}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
