#!/usr/bin/env python3
"""What the client offered and what the engine indexed, against concurrency.

X is concurrency, Y is docs/s, and every engine-and-batch configuration appears
**twice**: a solid line for the documents it got the engine to accept
(`docs_per_s`) and a dashed line, in the same colour, for the documents the
engine made searchable (`index_docs_per_s`). The gap inside a pair is the chart:
how far the index build falls behind the client feeding it.

    build-rate/charts/rate_vs_concurrency.py \\
        --scylla     '<R>/scylla/points/scylla-rep*.csv' \\
        --opensearch '<R>/opensearch/points/os-b*-rep*.csv' \\
        --output     '<R>/build-rate-vs-concurrency.png' \\
        --table      '<R>/build-rate-vs-concurrency.csv'

Sibling of `tools/plot_harness_grid.py`, which draws the submit rate alone
against a null sink and is the AWS runbook's deliverable. This one is for
`build-rate/HARNESS-LOCAL-RUNBOOK.md`, where both halves run against **real
engines**, so the index columns are populated and are half the point. The
naming of a row's series — ScyllaDB, or osrate at a batch size — is imported
from that module rather than restated, so a row lands on the same line in both
charts.

**The two dashed families are not the same mechanism.** On the ScyllaDB half
the index is the vector-store's Tantivy build, fed through CDC and publishing
continuously. On the OpenSearch half it is refresh-gated visibility, which
advances in steps and has a floor of `refresh_interval x docs_per_s` however
fast the engine indexes. Read them against their own solid line, not against
each other.

**`--series LABEL=GLOB` names a line by where its rows came from.** The
engine-flag naming above reads a series off the row — engine and batch size —
which is all the CSV carries. Arms that differ by an engine knob (a writer
buffer, a commit interval, a refresh interval) write identical rows and would
collapse onto one line. `--series` is repeatable, each one is its own line
with exactly the label given, and named series are drawn first in the order
given, ahead of anything collected by `--scylla`/`--opensearch`:

    build-rate/charts/rate_vs_concurrency.py \\
        --series 'R2 scylla-buf376=<R>/r2/scylla/points/*.csv' \\
        --series 'R4 os-ramindex-refresh3=<R>/r4/opensearch/points/*.csv' \\
        --output <R>/index-rate-vs-concurrency.png
"""
from __future__ import annotations

import argparse
import glob
import statistics
import sys
from collections import defaultdict
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BENCH_DIR))
sys.path.insert(0, str(BENCH_DIR / "tools"))

import plot_harness_grid as grid  # noqa: E402
from harness_charts import (MARKERS, colours, draw_footer,  # noqa: E402
                            label_right_edge, write_table)

SCYLLA_SERIES = grid.SCYLLA_SERIES
SCYLLA_ENGINE = grid.SCYLLA_ENGINE
OPENSEARCH_ENGINE = grid.OPENSEARCH_ENGINE
SCYLLA_COLOR = grid.SCYLLA_COLOR

SUBMITTED = "submitted"
INDEXED = "indexed"
METRICS = (SUBMITTED, INDEXED)
RATE_COLUMN = {SUBMITTED: "docs_per_s", INDEXED: "index_docs_per_s"}
LINE_STYLE = {SUBMITTED: "-", INDEXED: "--"}
SHORT_POINT_S = 3.0
TABLE_COLUMNS = ["series", "metric", "concurrency", "reps", "docs_per_s_median",
                 "docs_per_s_min", "docs_per_s_max", "shortest_wall_s"]

# The order the configurations are drawn in, and therefore which ramp step each
# takes. Keys only, which is all `plot_harness_grid.series_order` reads, so the
# two charts cannot disagree about where ScyllaDB sits or how the batch levels
# are sorted.
config_order = grid.series_order


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scylla", default="", help="glob of scyllarate point CSVs")
    parser.add_argument("--opensearch", default="", help="glob of osrate point CSVs")
    parser.add_argument("--series", action="append", default=[], metavar="LABEL=GLOB",
                        help="a line named LABEL from the point CSVs matching GLOB; "
                             "repeatable, drawn first in the order given. For arms "
                             "that differ by an engine knob the rows do not carry")
    parser.add_argument("--output", required=True, help="PNG path")
    parser.add_argument("--table", default="", help="also write every plotted point as CSV")
    parser.add_argument("--submitted-only", action="store_true",
                        help="drop the dashed index-rate family; what "
                             "tools/plot_harness_grid.py draws")
    parser.add_argument("--keep-warmup", action="store_true",
                        help="plot the ladder's leading warm-up row too; by "
                             "default the first data row of each CSV is dropped")
    parser.add_argument("--title", default="Build rate against concurrency — submitted and indexed")
    parser.add_argument("--subtitle", default="")
    parser.add_argument("--width", type=float, default=12.0)
    parser.add_argument("--height", type=float, default=7.5)
    parser.add_argument("--dpi", type=int, default=160)
    return parser.parse_args()


def rate_of(row: dict, metric: str) -> float | None:
    """The rate this row carries for one metric, or None where it carries none.

    A blank `index_docs_per_s` is a level that ran with the index unwatched. It
    is not an engine that indexed nothing, and reading it as zero would draw
    exactly that — so the point is dropped rather than invented.
    """
    value = row.get(RATE_COLUMN[metric], "")
    return float(value) if value else None


def points_of(row: dict, config: str,
              metrics: tuple[str, ...]) -> list[tuple[str, str, int, float, float]]:
    return [
        (config, metric, int(row["concurrency"]), rate, float(row["wall_s"]))
        for metric in metrics
        for rate in [rate_of(row, metric)]
        if rate is not None
    ]


def collect(pattern: str, keep_warmup: bool, engine: str,
            metrics: tuple[str, ...]) -> list[tuple[str, str, int, float, float]]:
    out = []
    for name in sorted(glob.glob(pattern)):
        for row in grid.read_points(Path(name), keep_warmup):
            out += points_of(row, grid.series_of(row, engine), metrics)
    return out


def collect_named(label: str, pattern: str, keep_warmup: bool,
                  metrics: tuple[str, ...]) -> list[tuple[str, str, int, float, float]]:
    """Every row under the glob lands on the line called `label`, whatever its
    engine or batch column says: the arm is known from where the files are,
    not from what they contain."""
    out = []
    for name in sorted(glob.glob(pattern)):
        for row in grid.read_points(Path(name), keep_warmup):
            out += points_of(row, label, metrics)
    return out


def parse_series(argument: str) -> tuple[str, str]:
    label, seam, pattern = argument.partition("=")
    if not seam or not label or not pattern:
        raise SystemExit(f"--series wants LABEL=GLOB, got {argument!r}")
    return label, pattern


def series_order(table: dict[str, dict[str, dict[int, dict]]],
                 named: list[str]) -> list[str]:
    """Named series first, as given; then whatever the engine flags collected,
    in the sibling chart's order. A label is never parsed for a batch size."""
    leading = [label for label in named if label in table]
    rest = {config: levels for config, levels in table.items()
            if config not in leading}
    return leading + config_order(rest)


def aggregate(points: list[tuple[str, str, int, float, float]]) -> dict[str, dict[str, dict[int, dict]]]:
    grouped = defaultdict(list)
    for config, metric, concurrency, rate, wall in points:
        grouped[(config, metric, concurrency)].append((rate, wall))
    table: dict[str, dict[str, dict[int, dict]]] = defaultdict(lambda: defaultdict(dict))
    for (config, metric, concurrency), values in grouped.items():
        rates = [rate for rate, _ in values]
        table[config][metric][concurrency] = {
            "reps": len(rates),
            "median": statistics.median(rates),
            "min": min(rates),
            "max": max(rates),
            "shortest_wall_s": min(wall for _, wall in values),
        }
    return table


def label_for(config: str, metric: str) -> str:
    return config if metric == SUBMITTED else f"{config} indexed"


def metric_order(metrics: dict[str, dict[int, dict]]) -> list[str]:
    return [metric for metric in METRICS if metric in metrics]


def draw_series(axes, config: str, metric: str, levels: dict[int, dict],
                colour, marker) -> tuple:
    """One line. Solid with a filled marker is what the client submitted;
    dashed with a hollow one is what the engine indexed, in the same colour so
    the pair reads as one configuration seen twice."""
    xs = sorted(levels)
    ys = [levels[x]["median"] / 1000.0 for x in xs]
    lows = [(levels[x]["median"] - levels[x]["min"]) / 1000.0 for x in xs]
    highs = [(levels[x]["max"] - levels[x]["median"]) / 1000.0 for x in xs]
    name = label_for(config, metric)
    axes.errorbar(xs, ys, yerr=[lows, highs], color=colour, marker=marker,
                  markersize=6, linewidth=1.8, capsize=3, elinewidth=0.9,
                  linestyle=LINE_STYLE[metric],
                  markerfacecolor=colour if metric == SUBMITTED else "none",
                  label=name, zorder=3)
    return (xs[-1], ys[-1], name, colour)


def short_points(table: dict[str, dict[str, dict[int, dict]]]) -> list[str]:
    """Points that did not run long enough to be a measurement.

    Read off the submitted family only: wall time belongs to the level, not to
    the two rates taken off it, and naming each short level twice would double
    a footer that is already the longest thing on the image.
    """
    return [
        f"{config} c={concurrency} ({levels[concurrency]['shortest_wall_s']:.1f}s)"
        for config, metrics in table.items()
        for levels in [metrics.get(SUBMITTED, {})]
        for concurrency in sorted(levels)
        if levels[concurrency]["shortest_wall_s"] < SHORT_POINT_S
    ]


def counted_points(table: dict[str, dict[str, dict[int, dict]]]) -> int:
    return sum(len(levels) for metrics in table.values()
               for levels in metrics.values())


def footer_lines(table: dict[str, dict[str, dict[int, dict]]],
                 short: list[str], indexed: bool,
                 keep_warmup: bool = False) -> list[str]:
    lines = [
        "x is a CLIENT knob and does not mean the same thing on both engines: one "
        "scyllarate unit is one in-flight prepared INSERT carrying ONE document, "
        "one osrate unit is one in-flight _bulk carrying batch_size of them.",
        "Documents in flight is concurrency x batch_size. batch=1 is the only "
        "level where the two engines' x axes are the same shape.",
        "Point is the median of the repetitions, bar is min..max. "
        + ("EVERY row is plotted: this ladder carries no throwaway rung, so its "
           "first and lowest point was measured on a cold process."
           if keep_warmup else "The ladder's leading warm-up row is dropped."),
    ]
    if indexed:
        lines += [
            "SOLID is docs_per_s, what the client got the engine to accept. "
            "DASHED is index_docs_per_s, what the engine made searchable -- same "
            "colour, same configuration, read as a pair.",
            "The two dashed families are NOT the same mechanism: on ScyllaDB it "
            "is the vector-store's Tantivy build publishing continuously, on "
            "OpenSearch it is refresh-gated visibility with a floor of "
            "refresh_interval x docs_per_s. Do not read one against the other.",
        ]
    if short:
        shown = "; ".join(short[:6])
        more = f" ... and {len(short) - 6} more" if len(short) > 6 else ""
        lines.append(f"SHORT POINTS -- under {SHORT_POINT_S:.0f} s, not a "
                     f"measurement ({len(short)} levels): {shown}{more}"
                     f"  -> raise --max-docs for these levels and re-run them.")
    return lines


def table_rows(table: dict[str, dict[str, dict[int, dict]]],
               order: list[str]) -> list[list]:
    return [
        [config, metric, concurrency, point["reps"],
         f"{point['median']:.1f}", f"{point['min']:.1f}", f"{point['max']:.1f}",
         f"{point['shortest_wall_s']:.3f}"]
        for config in order
        for metric in metric_order(table[config])
        for concurrency, point in sorted(table[config][metric].items())
    ]


def colour_for(config: str, warm: list, warm_index: int) -> tuple:
    if config == SCYLLA_SERIES:
        return SCYLLA_COLOR, warm_index
    return warm[warm_index], warm_index + 1


def main() -> int:
    args = parse_args()
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    metrics = (SUBMITTED,) if args.submitted_only else METRICS
    named = [parse_series(argument) for argument in args.series]
    points = []
    for label, pattern in named:
        points += collect_named(label, pattern, args.keep_warmup, metrics)
    if args.scylla:
        points += collect(args.scylla, args.keep_warmup, SCYLLA_ENGINE, metrics)
    if args.opensearch:
        points += collect(args.opensearch, args.keep_warmup, OPENSEARCH_ENGINE,
                          metrics)
    if not points:
        print("no points matched --series / --scylla / --opensearch")
        return 1

    table = aggregate(points)
    order = series_order(table, [label for label, _ in named])
    warm = colours(len([name for name in order if name != SCYLLA_SERIES]))

    figure, axes = plt.subplots(figsize=(args.width, args.height), dpi=args.dpi)
    ends: list[tuple] = []
    warm_index = 0
    for index, config in enumerate(order):
        colour, warm_index = colour_for(config, warm, warm_index)
        marker = MARKERS[index % len(MARKERS)]
        for metric in metric_order(table[config]):
            ends.append(draw_series(axes, config, metric, table[config][metric],
                                    colour, marker))

    concurrencies = sorted({c for metrics_of in table.values()
                            for levels in metrics_of.values() for c in levels})
    axes.set_xscale("log", base=2)
    axes.set_xticks(concurrencies)
    axes.get_xaxis().set_major_formatter(FuncFormatter(lambda v, _: f"{int(v)}"))
    axes.set_xlabel("concurrency  (in-flight requests, log2)")
    axes.set_ylabel("docs/s (thousands)")
    axes.set_ylim(bottom=0)
    axes.grid(True, linewidth=0.4, alpha=0.4)
    axes.set_axisbelow(True)
    title = args.title + (f"\n{args.subtitle}" if args.subtitle else "")
    axes.set_title(title, fontsize=11, loc="left")
    axes.legend(fontsize=8, ncol=2, loc="upper left", framealpha=0.9)
    label_right_edge(axes, ends)

    figure.subplots_adjust(right=0.78, bottom=0.34)
    draw_footer(figure, footer_lines(table, short_points(table),
                                     indexed=INDEXED in metrics,
                                     keep_warmup=args.keep_warmup))
    figure.savefig(args.output, dpi=args.dpi)
    print(f"wrote {args.output}  ({len(ends)} lines, "
          f"{counted_points(table)} points)")

    if args.table:
        write_table(args.table, TABLE_COLUMNS, table_rows(table, order))
        print(f"wrote {args.table}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
