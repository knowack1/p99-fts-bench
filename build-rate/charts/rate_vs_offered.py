#!/usr/bin/env python3
"""What the engine accepted and indexed, against the rate it was *offered*.

X is `target_docs_per_s` — a rate the client was told to produce, not a client
knob whose meaning differs per engine. That is the whole reason this chart
exists beside `rate_vs_concurrency.py`: 50,000 docs/s means the same thing
whether it arrives as 49 `_bulk`s of 1,024 or as 50,000 prepared INSERTs, so a
cross-engine reading at one x value compares two engines rather than two
different offers.

    build-rate/charts/rate_vs_offered.py \\
        --series 'R2 scylla-buf376=<R>/r2/scylla/points/*.csv' \\
        --series 'R4 os-ramindex-refresh1=<R>/r4/opensearch/points/*.csv' \\
        --output '<R>/index-rate-vs-offered.png' \\
        --table  '<R>/index-rate-vs-offered.csv'

**The diagonal is the reference and the chart is the departure from it.** A
configuration keeping up sits on `y = x`. Where its solid line leaves the
diagonal is the rate at which the engine stopped accepting everything offered;
where its dashed line leaves is where the *index* stopped keeping up, which is
the number this campaign exists for.

**A hollow ring marks a saturated rung** — `generator_saturated`, i.e. under
95% of the offered rate delivered. Saturation is a finding, not a gap: it is
how a fast arm and a slow arm can share one x grid and each still show its own
knee. Read a ringed point against `in_flight_peak` in the table twin first: a
peak sitting at the `--concurrency` cap means the harness was the limit and the
point is void rather than a measurement of the engine.

Everything that is not the x axis — how a row becomes a series, how repetitions
become a point and a bar, the colours, the table twin — is imported from
`rate_vs_concurrency.py` rather than restated, so an arm lands on the same line
and in the same colour on both charts.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BENCH_DIR))
sys.path.insert(0, str(BENCH_DIR / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import rate_vs_concurrency as rvc  # noqa: E402
from harness_charts import (MARKERS, colours, draw_footer,  # noqa: E402
                            label_right_edge, write_table)

SUBMITTED = rvc.SUBMITTED
INDEXED = rvc.INDEXED
METRICS = rvc.METRICS
SATURATED_FLOOR = 0.95
TABLE_COLUMNS = ["series", "metric", "offered_docs_per_s", "reps",
                 "docs_per_s_median", "docs_per_s_min", "docs_per_s_max",
                 "shortest_wall_s", "saturated", "in_flight_peak"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scylla", default="", help="glob of scyllarate point CSVs")
    parser.add_argument("--opensearch", default="", help="glob of osrate point CSVs")
    parser.add_argument("--series", action="append", default=[], metavar="LABEL=GLOB",
                        help="a line named LABEL from the point CSVs matching GLOB; "
                             "repeatable, drawn first in the order given")
    parser.add_argument("--output", required=True, help="PNG path")
    parser.add_argument("--table", default="", help="also write every plotted point as CSV")
    parser.add_argument("--submitted-only", action="store_true",
                        help="drop the dashed index-rate family")
    parser.add_argument("--no-diagonal", action="store_true",
                        help="omit the y=x reference line")
    parser.add_argument("--keep-warmup", action="store_true",
                        help="plot the ladder's leading row too")
    parser.add_argument("--title",
                        default="Index rate against offered rate — submitted and indexed")
    parser.add_argument("--subtitle", default="")
    parser.add_argument("--width", type=float, default=12.0)
    parser.add_argument("--height", type=float, default=7.5)
    parser.add_argument("--dpi", type=int, default=160)
    return parser.parse_args()


def offered_of(row: dict) -> int | None:
    """The rung's x. Blank on a concurrency-ladder row, which has no offered
    rate at all — such a row belongs on the sibling chart and is refused here
    rather than placed at an x it never had."""
    value = row.get("target_docs_per_s", "")
    return int(value) if value else None


def points_of(row: dict, config: str, metrics: tuple[str, ...]) -> list[tuple]:
    offered = offered_of(row)
    if offered is None:
        raise SystemExit(
            "this CSV was measured on the concurrency ladder (target_docs_per_s "
            "is blank), so it has no offered rate to put on x -- "
            "render it with build-rate/charts/rate_vs_concurrency.py")
    return [
        (config, metric, offered, rate, float(row["wall_s"]))
        for metric in metrics
        for rate in [rvc.rate_of(row, metric)]
        if rate is not None
    ]


def saturation_of(rows: list[dict]) -> dict[tuple[str, int], dict]:
    """Which rungs fell short, and how much room the cap still had.

    A rung is marked if *any* repetition of it saturated: a rate one rep could
    not sustain is not a rate the configuration sustains, and averaging that
    away is how a knee goes missing.
    """
    marks: dict[tuple[str, int], dict] = {}
    for config, offered, saturated, peak in rows:
        seen = marks.setdefault((config, offered), {"saturated": False, "peak": 0})
        seen["saturated"] = seen["saturated"] or saturated
        seen["peak"] = max(seen["peak"], peak)
    return marks


def flags_of(row: dict, config: str) -> list[tuple]:
    offered = offered_of(row)
    if offered is None:
        return []
    return [(config, offered,
             row.get("generator_saturated", "") == "true",
             int(row.get("in_flight_peak", "") or 0))]


def gather(args, metrics: tuple[str, ...]) -> tuple[list[tuple], list[tuple], list[str]]:
    named = [rvc.parse_series(argument) for argument in args.series]
    points: list[tuple] = []
    flags: list[tuple] = []
    for label, pattern in named:
        points += rvc.collect_named(label, pattern, args.keep_warmup, metrics,
                                    contribute=points_of)
        flags += rvc.collect_named(label, pattern, args.keep_warmup, metrics,
                                   contribute=lambda row, config, _m: flags_of(row, config))
    for pattern, engine in ((args.scylla, rvc.SCYLLA_ENGINE),
                            (args.opensearch, rvc.OPENSEARCH_ENGINE)):
        if not pattern:
            continue
        points += rvc.collect(pattern, args.keep_warmup, engine, metrics,
                              contribute=points_of)
        flags += rvc.collect(pattern, args.keep_warmup, engine, metrics,
                             contribute=lambda row, config, _m: flags_of(row, config))
    return points, flags, [label for label, _ in named]


def short_points(table: dict) -> list[str]:
    """Levels too brief to be a measurement.

    Named `offered=` rather than the sibling's `c=`: the key on this axis is a
    rate, and a footer that called 50,000 a concurrency would be read as one.
    """
    return [
        f"{config} offered={offered} "
        f"({levels[offered]['shortest_wall_s']:.1f}s)"
        for config, metrics in table.items()
        for levels in [metrics.get(SUBMITTED, {})]
        for offered in sorted(levels)
        if levels[offered]["shortest_wall_s"] < rvc.SHORT_POINT_S
    ]


def ring_saturated(axes, config: str, levels: dict, marks: dict, colour) -> int:
    """A hollow ring over a point the configuration could not sustain."""
    xs = [x for x in sorted(levels) if marks.get((config, x), {}).get("saturated")]
    if not xs:
        return 0
    ys = [levels[x]["median"] / 1000.0 for x in xs]
    axes.scatter([x / 1000.0 for x in xs], ys, s=150, facecolors="none",
                 edgecolors=colour, linewidths=1.8, zorder=5)
    return len(xs)


def draw_series(axes, config: str, metric: str, levels: dict, colour, marker) -> tuple:
    xs = sorted(levels)
    ys = [levels[x]["median"] / 1000.0 for x in xs]
    lows = [(levels[x]["median"] - levels[x]["min"]) / 1000.0 for x in xs]
    highs = [(levels[x]["max"] - levels[x]["median"]) / 1000.0 for x in xs]
    name = rvc.label_for(config, metric)
    axes.errorbar([x / 1000.0 for x in xs], ys, yerr=[lows, highs], color=colour,
                  marker=marker, markersize=6, linewidth=1.8, capsize=3,
                  elinewidth=0.9, linestyle=rvc.LINE_STYLE[metric],
                  markerfacecolor=colour if metric == SUBMITTED else "none",
                  label=name, zorder=3)
    return (xs[-1] / 1000.0, ys[-1], name, colour)


def draw_diagonal(axes, offered: list[int]) -> None:
    """`y = x`: everything offered was accepted and indexed. The reference the
    whole chart is read against, drawn under the data."""
    edge = max(offered) / 1000.0
    axes.plot([0, edge], [0, edge], color="#777777", linewidth=1.0,
              linestyle=":", zorder=1, label="offered (y = x)")


def footer_lines(marks: dict, short: list[str], indexed: bool,
                 keep_warmup: bool) -> list[str]:
    lines = [
        "x is the rate the client was TOLD to offer, which means the same thing "
        "on both engines: one scyllarate document and one document inside an "
        "osrate _bulk are both one document per second.",
        "The dotted diagonal is y = x, everything offered arriving and indexed. "
        "Where a line LEAVES the diagonal is the reading.",
        "Point is the median of the repetitions, bar is min..max. "
        + ("EVERY row is plotted: this ladder carries no throwaway rung."
           if keep_warmup else "The ladder's leading warm-up row is dropped."),
        "A HOLLOW RING is a saturated rung -- under "
        f"{SATURATED_FLOOR:.0%} of the offered rate delivered. Check "
        "in_flight_peak in the table twin against --concurrency first: a peak at "
        "the cap means the HARNESS was the limit and the point is void.",
    ]
    if indexed:
        lines += [
            "SOLID is docs_per_s, what the engine accepted. DASHED is "
            "index_docs_per_s, what it made searchable -- same colour, same "
            "configuration, read as a pair.",
            "The two dashed families are NOT the same mechanism: on ScyllaDB it "
            "is the vector-store's Tantivy build publishing continuously, on "
            "OpenSearch it is refresh-gated visibility with a floor of "
            "refresh_interval x docs_per_s. Do not read one against the other.",
        ]
    ringed = sum(1 for mark in marks.values() if mark["saturated"])
    if ringed:
        lines.append(f"{ringed} rung(s) saturated and are ringed.")
    if short:
        shown = "; ".join(short[:6])
        more = f" ... and {len(short) - 6} more" if len(short) > 6 else ""
        lines.append(f"SHORT POINTS -- under {rvc.SHORT_POINT_S:.0f} s, not a "
                     f"measurement ({len(short)} levels): {shown}{more}")
    return lines


def table_rows(table: dict, order: list[str], marks: dict) -> list[list]:
    return [
        [config, metric, offered, point["reps"],
         f"{point['median']:.1f}", f"{point['min']:.1f}", f"{point['max']:.1f}",
         f"{point['shortest_wall_s']:.3f}",
         str(marks.get((config, offered), {}).get("saturated", "")).lower(),
         marks.get((config, offered), {}).get("peak", "")]
        for config in order
        for metric in rvc.metric_order(table[config])
        for offered, point in sorted(table[config][metric].items())
    ]


def main() -> int:
    args = parse_args()
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    metrics = (SUBMITTED,) if args.submitted_only else METRICS
    points, flags, named = gather(args, metrics)
    if not points:
        print("no points matched --series / --scylla / --opensearch")
        return 1

    table = rvc.aggregate(points)
    marks = saturation_of(flags)
    order = rvc.series_order(table, named)
    warm = colours(len([name for name in order if name != rvc.SCYLLA_SERIES]))

    figure, axes = plt.subplots(figsize=(args.width, args.height), dpi=args.dpi)
    offered = sorted({x for metrics_of in table.values()
                      for levels in metrics_of.values() for x in levels})
    if not args.no_diagonal:
        draw_diagonal(axes, offered)

    ends: list[tuple] = []
    warm_index = 0
    for index, config in enumerate(order):
        colour, warm_index = rvc.colour_for(config, warm, warm_index)
        marker = MARKERS[index % len(MARKERS)]
        for metric in rvc.metric_order(table[config]):
            ends.append(draw_series(axes, config, metric, table[config][metric],
                                    colour, marker))
        ring_saturated(axes, config, table[config].get(SUBMITTED, {}), marks, colour)

    axes.set_xlabel("offered rate  (thousand docs/s, what the client was told to send)")
    axes.set_ylabel("docs/s (thousands)")
    axes.set_xlim(left=0)
    axes.set_ylim(bottom=0)
    axes.grid(True, linewidth=0.4, alpha=0.4)
    axes.set_axisbelow(True)
    title = args.title + (f"\n{args.subtitle}" if args.subtitle else "")
    axes.set_title(title, fontsize=11, loc="left")
    axes.legend(fontsize=8, ncol=2, loc="upper left", framealpha=0.9)
    label_right_edge(axes, ends)

    figure.subplots_adjust(right=0.78, bottom=0.34)
    draw_footer(figure, footer_lines(marks, short_points(table),
                                     indexed=INDEXED in metrics,
                                     keep_warmup=args.keep_warmup))
    figure.savefig(args.output, dpi=args.dpi)
    print(f"wrote {args.output}  ({len(ends)} lines, "
          f"{rvc.counted_points(table)} points)")

    if args.table:
        write_table(args.table, TABLE_COLUMNS, table_rows(table, order, marks))
        print(f"wrote {args.table}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
