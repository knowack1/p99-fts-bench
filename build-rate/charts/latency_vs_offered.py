#!/usr/bin/env python3
"""What one write request cost, against the rate the client was *offered*.

Y is `p50_ms` and `p99_ms` off the same rows `rate_vs_offered.py` draws, so the
two images are read as a pair: that one says how much the engine accepted, this
one says what a request waited to be accepted.

    build-rate/charts/latency_vs_offered.py \\
        --series 'R4 os-ramindex-refresh1=<R>/r4/opensearch/points/*.csv' \\
        --concurrency-cap 128 \\
        --output '<R>/index-latency-vs-offered.png' \\
        --table  '<R>/index-latency-vs-offered.csv'

**A request is not a document.** `latency_unit` in the CSV preamble says what
one is — `bulk_request` on the OpenSearch half, where it carries `batch_size`
documents, `insert_request` on the ScyllaDB half, where it carries one. Those
are not the same quantity and this chart refuses to put both on one y axis
unless told to.

**Under the rate ladder these columns are measured from when a request was
DUE**, not from when it was sent (`latency_basis=intended_start`), so a rung the
client could not keep up with shows the backlog rather than hiding it. That is
also why the y value can exceed anything the engine did: `queue_p99_ms` is the
part of `p99_ms` that was spent waiting to be sent, and where it dominates the
chart is measuring the harness. Every rung over `--queue-share` is annotated
with its share and named in the footer — the campaign's "Schedule held" gate,
drawn rather than asserted.

**A ringed point is saturated and a crossed one is void.** Saturation alone is a
finding. Saturation with `in_flight_peak` at the `--concurrency` cap is an
instrument reading: the harness ran out of in-flight slots, latency is what the
cap did, and the point may not be quoted as the engine's. Pass the cap and the
renderer marks those points and names them; omit it and they are drawn as
ordinary saturated rungs with `in_flight_peak` left in the table twin, which is
what the sibling does.
"""
from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BENCH_DIR))
sys.path.insert(0, str(BENCH_DIR / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import rate_vs_concurrency as rvc  # noqa: E402
import rate_vs_offered as rvo  # noqa: E402
from harness_charts import (MARKERS, colours, draw_footer,  # noqa: E402
                            label_right_edge, read_preamble, write_table)

P50 = "p50"
P99 = "p99"
METRICS = (P50, P99)
LATENCY_COLUMN = {P50: "p50_ms", P99: "p99_ms"}
LINE_STYLE = {P50: "-", P99: "--"}
DEFAULT_QUEUE_SHARE = 0.10
PACED_BASIS = "intended_start"
TABLE_COLUMNS = ["series", "metric", "offered_docs_per_s", "reps",
                 "latency_ms_median", "latency_ms_min", "latency_ms_max",
                 "shortest_wall_s", "queue_p99_ms", "queue_share_of_p99",
                 "saturated", "in_flight_peak", "verdict"]


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
    parser.add_argument("--concurrency-cap", type=int, default=0,
                        help="the --concurrency the ladder ran at; a saturated "
                             "rung whose in_flight_peak reached it is marked VOID")
    parser.add_argument("--queue-share", type=float, default=DEFAULT_QUEUE_SHARE,
                        help="queue_p99_ms/p99_ms above which a rung is annotated "
                             f"with its share (default {DEFAULT_QUEUE_SHARE:.2f})")
    parser.add_argument("--p99-only", action="store_true", help="drop the p50 family")
    parser.add_argument("--log-x", action="store_true",
                        help="offered rate on a log2 axis, one tick per rung; "
                             "linear by default so the image stacks on the "
                             "rate chart's x")
    parser.add_argument("--allow-mixed-units", action="store_true",
                        help="draw two latency_unit values on one axis anyway")
    parser.add_argument("--keep-warmup", action="store_true",
                        help="plot the ladder's leading row too")
    parser.add_argument("--title",
                        default="Write request latency against offered rate — p50 and p99")
    parser.add_argument("--subtitle", default="")
    parser.add_argument("--width", type=float, default=12.0)
    parser.add_argument("--height", type=float, default=7.5)
    parser.add_argument("--dpi", type=int, default=160)
    return parser.parse_args()


def latency_of(row: dict, metric: str) -> float | None:
    """A blank percentile is a row that recorded none. Reading it as zero would
    draw a request that cost nothing, so the point is dropped instead."""
    value = row.get(LATENCY_COLUMN[metric], "")
    return float(value) if value else None


def points_of(row: dict, config: str, metrics: tuple[str, ...]) -> list[tuple]:
    offered = rvo.offered_of(row)
    if offered is None:
        raise SystemExit(
            "this CSV was measured on the concurrency ladder (target_docs_per_s "
            "is blank), so it has no offered rate to put on x -- and its "
            "latencies are closed-loop service times, which are not the "
            "quantity this chart draws")
    return [
        (config, metric, offered, latency, float(row["wall_s"]))
        for metric in metrics
        for latency in [latency_of(row, metric)]
        if latency is not None
    ]


def flags_of(row: dict, config: str) -> list[tuple]:
    offered = rvo.offered_of(row)
    if offered is None:
        return []
    return [(config, offered,
             row.get("generator_saturated", "") == "true",
             int(row.get("in_flight_peak", "") or 0),
             float(row.get("queue_p99_ms", "") or 0.0),
             float(row.get("p99_ms", "") or 0.0))]


def queue_share(queue_p99: float, p99: float) -> float:
    return queue_p99 / p99 if p99 > 0 else 0.0


def marks_of(rows: list[tuple], cap: int) -> dict[tuple[str, int], dict]:
    """What a rung has to be read against before its latency is read at all.

    The worst repetition wins every field: a rate one rep could not sustain is
    not a rate the configuration sustains, and a rep that spent most of its p99
    queueing is not made safe by a quieter one beside it.
    """
    marks: dict[tuple[str, int], dict] = {}
    for config, offered, saturated, peak, queue_p99, p99 in rows:
        seen = marks.setdefault((config, offered),
                                {"saturated": False, "peak": 0,
                                 "queue_p99": 0.0, "share": 0.0})
        seen["saturated"] = seen["saturated"] or saturated
        seen["peak"] = max(seen["peak"], peak)
        share = queue_share(queue_p99, p99)
        if share >= seen["share"]:
            seen["share"] = share
            seen["queue_p99"] = queue_p99
    for mark in marks.values():
        mark["void"] = bool(cap and mark["saturated"] and mark["peak"] >= cap)
    return marks


def verdict_of(mark: dict) -> str:
    if mark.get("void"):
        return "void"
    return "saturated" if mark.get("saturated") else "valid"


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


def patterns_of(args) -> list[str]:
    named = [rvc.parse_series(argument)[1] for argument in args.series]
    return named + [pattern for pattern in (args.scylla, args.opensearch) if pattern]


def preamble_facts(patterns: list[str], key: str) -> set[str]:
    return {read_preamble(Path(name)).get(key, "")
            for pattern in patterns for name in sorted(glob.glob(pattern))}


def latency_unit(patterns: list[str], allow_mixed: bool) -> str:
    """One name for what a request is, or a refusal.

    `insert_request` carries one document and `bulk_request` carries
    `batch_size` of them, so a millisecond on one axis would mean two different
    amounts of work and the taller line would read as the slower engine.
    """
    units = {unit for unit in preamble_facts(patterns, "latency_unit") if unit}
    if len(units) > 1 and not allow_mixed:
        raise SystemExit(
            f"these CSVs carry {len(units)} latency units ({', '.join(sorted(units))}) "
            "and one millisecond does not mean the same amount of work in each -- "
            "draw them separately, or pass --allow-mixed-units and caption it")
    return "/".join(sorted(units)) if units else "request"


def latency_basis(patterns: list[str]) -> str:
    """`intended_start` includes the wait to be sent, `service` does not, so the
    two are different measurements and may not be pooled onto one line."""
    bases = {basis for basis in preamble_facts(patterns, "latency_basis") if basis}
    if len(bases) > 1:
        raise SystemExit(
            f"these CSVs mix latency bases ({', '.join(sorted(bases))}): "
            "intended_start counts the wait to be sent and service does not, "
            "so pooling them would draw two different measurements as one line")
    return bases.pop() if bases else "unknown"


def format_offered(value: float, _position=None) -> str:
    """The rung's declared rate, not a rounded thousand: the ladder's steps are
    powers of two and `1.024k` reads as a different number from `1024`."""
    return f"{value * 1000.0:,.0f}"


def format_latency(value: float, _position=None) -> str:
    if value >= 1000.0:
        return f"{value / 1000.0:g} s"
    return f"{value:g} ms"


def label_for(config: str, metric: str) -> str:
    return f"{config} {metric}"


def metric_order(metrics: dict) -> list[str]:
    return [metric for metric in METRICS if metric in metrics]


def draw_series(axes, config: str, metric: str, levels: dict, colour, marker) -> tuple:
    xs = sorted(levels)
    ys = [levels[x]["median"] for x in xs]
    lows = [levels[x]["median"] - levels[x]["min"] for x in xs]
    highs = [levels[x]["max"] - levels[x]["median"] for x in xs]
    name = label_for(config, metric)
    axes.errorbar([x / 1000.0 for x in xs], ys, yerr=[lows, highs], color=colour,
                  marker=marker, markersize=6, linewidth=1.8, capsize=3,
                  elinewidth=0.9, linestyle=LINE_STYLE[metric],
                  markerfacecolor=colour if metric == P50 else "none",
                  label=name, zorder=3)
    return (xs[-1] / 1000.0, ys[-1], name, colour)


def ring_saturated(axes, config: str, levels: dict, marks: dict, colour) -> int:
    xs = [x for x in sorted(levels) if marks.get((config, x), {}).get("saturated")]
    if not xs:
        return 0
    axes.scatter([x / 1000.0 for x in xs], [levels[x]["median"] for x in xs],
                 s=150, facecolors="none", edgecolors=colour, linewidths=1.8,
                 zorder=5)
    return len(xs)


def cross_void(axes, config: str, levels: dict, marks: dict, colour) -> int:
    """The harness ran out of in-flight slots, so this latency is the cap's."""
    xs = [x for x in sorted(levels) if marks.get((config, x), {}).get("void")]
    if not xs:
        return 0
    axes.scatter([x / 1000.0 for x in xs], [levels[x]["median"] for x in xs],
                 s=190, marker="x", color=colour, linewidths=2.0, zorder=6)
    return len(xs)


def annotate_queue(axes, config: str, levels: dict, marks: dict, colour,
                   threshold: float) -> None:
    for x in sorted(levels):
        share = marks.get((config, x), {}).get("share", 0.0)
        if share < threshold:
            continue
        axes.annotate(f"{share:.0%} queued", xy=(x / 1000.0, levels[x]["median"]),
                      xytext=(-12, -16), textcoords="offset points", color=colour,
                      fontsize=7, ha="right", va="top", zorder=7)


def queued_rungs(marks: dict, threshold: float) -> list[str]:
    return [f"{config} offered={offered} ({mark['share']:.0%} of p99, "
            f"queue_p99={mark['queue_p99']:.0f} ms)"
            for (config, offered), mark in sorted(marks.items())
            if mark["share"] >= threshold]


def void_rungs(marks: dict) -> list[str]:
    return [f"{config} offered={offered} (in_flight_peak={mark['peak']})"
            for (config, offered), mark in sorted(marks.items())
            if mark.get("void")]


def short_points(table: dict) -> list[str]:
    return [
        f"{config} offered={offered} ({levels[offered]['shortest_wall_s']:.1f}s)"
        for config, metrics in table.items()
        for levels in [metrics.get(P99, {})]
        for offered in sorted(levels)
        if levels[offered]["shortest_wall_s"] < rvc.SHORT_POINT_S
    ]


def footer_lines(marks: dict, short: list[str], unit: str, basis: str,
                 threshold: float, cap: int, keep_warmup: bool) -> list[str]:
    lines = [
        f"y is the latency of ONE {unit} and a request is not a document: on the "
        "OpenSearch half it carries batch_size documents, on the ScyllaDB half "
        "one. Read it beside the rate chart, never instead of it.",
        f"latency_basis={basis}. "
        + ("Measured from when a request was DUE, not from when it was sent, so "
           "a rung the client could not keep up with shows its backlog here "
           "instead of hiding it."
           if basis == PACED_BASIS else
           "Measured from when a request was SENT, which cannot show a backlog "
           "-- this is a closed-loop CSV and did not come off the rate ladder."),
        "Point is the median ACROSS REPETITIONS of that repetition's own "
        "percentile, bar is min..max. Percentiles are never pooled across reps. "
        + ("EVERY row is plotted: this ladder carries no throwaway rung."
           if keep_warmup else "The ladder's leading warm-up row is dropped."),
        "SOLID filled is p50, DASHED hollow is p99 -- same colour, same "
        "configuration, and the gap between them is the tail.",
        "A HOLLOW RING is a saturated rung: under 95% of the offered rate "
        "delivered, so its latency is what a client got while falling behind.",
    ]
    if cap:
        lines.append(
            f"A CROSS is VOID -- saturated with in_flight_peak at the "
            f"--concurrency cap of {cap}. That latency is the CAP's, not the "
            "engine's, and may not be quoted: re-run the rung at a higher cap.")
    else:
        lines.append(
            "No --concurrency cap was given, so nothing is marked void. Read "
            "in_flight_peak in the table twin against the cap the ladder ran "
            "at: a peak sitting on it means the HARNESS was the limit.")
    void = void_rungs(marks)
    if void:
        lines.append(f"VOID ({len(void)}): " + "; ".join(void))
    queued = queued_rungs(marks, threshold)
    if queued:
        lines.append(
            f"SCHEDULE HELD gate at queue_p99/p99 >= {threshold:.0%} -- these "
            f"rungs are annotated with their share ({len(queued)}): "
            + "; ".join(queued[:4])
            + (f" ... and {len(queued) - 4} more" if len(queued) > 4 else ""))
    else:
        lines.append(f"No rung spent {threshold:.0%} or more of its p99 queueing; "
                     "the schedule held on every point drawn.")
    if short:
        shown = "; ".join(short[:6])
        more = f" ... and {len(short) - 6} more" if len(short) > 6 else ""
        lines.append(f"SHORT POINTS -- under {rvc.SHORT_POINT_S:.0f} s, not a "
                     f"measurement ({len(short)} levels): {shown}{more}")
    return lines


def table_rows(table: dict, order: list[str], marks: dict) -> list[list]:
    return [
        [config, metric, offered, point["reps"],
         f"{point['median']:.3f}", f"{point['min']:.3f}", f"{point['max']:.3f}",
         f"{point['shortest_wall_s']:.3f}",
         f"{mark.get('queue_p99', 0.0):.3f}", f"{mark.get('share', 0.0):.4f}",
         str(mark.get("saturated", False)).lower(), mark.get("peak", ""),
         verdict_of(mark)]
        for config in order
        for metric in metric_order(table[config])
        for offered, point in sorted(table[config][metric].items())
        for mark in [marks.get((config, offered), {})]
    ]


def main() -> int:
    args = parse_args()
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    metrics = (P99,) if args.p99_only else METRICS
    patterns = patterns_of(args)
    unit = latency_unit(patterns, args.allow_mixed_units)
    basis = latency_basis(patterns)
    points, flags, named = gather(args, metrics)
    if not points:
        print("no points matched --series / --scylla / --opensearch")
        return 1

    table = rvc.aggregate(points)
    marks = marks_of(flags, args.concurrency_cap)
    order = rvc.series_order(table, named)
    warm = colours(len([name for name in order if name != rvc.SCYLLA_SERIES]))

    figure, axes = plt.subplots(figsize=(args.width, args.height), dpi=args.dpi)
    ends: list[tuple] = []
    warm_index = 0
    for index, config in enumerate(order):
        colour, warm_index = rvc.colour_for(config, warm, warm_index)
        marker = MARKERS[index % len(MARKERS)]
        for metric in metric_order(table[config]):
            ends.append(draw_series(axes, config, metric, table[config][metric],
                                    colour, marker))
        tail = table[config].get(P99, {})
        ring_saturated(axes, config, tail, marks, colour)
        cross_void(axes, config, tail, marks, colour)
        annotate_queue(axes, config, tail, marks, colour, args.queue_share)

    axes.set_yscale("log")
    axes.get_yaxis().set_major_formatter(FuncFormatter(format_latency))
    offered = sorted({x for metrics_of in table.values()
                      for levels in metrics_of.values() for x in levels})
    if args.log_x:
        axes.set_xscale("log", base=2)
        axes.set_xticks([x / 1000.0 for x in offered])
        axes.get_xaxis().set_major_formatter(FuncFormatter(format_offered))
    else:
        axes.set_xlim(left=0)
    axes.set_xlabel(
        "offered rate  (docs/s, log2 -- one tick per ladder rung)" if args.log_x
        else "offered rate  (thousand docs/s, what the client was told to send)")
    axes.set_ylabel(f"{unit} latency, log scale  (basis: {basis})")
    axes.grid(True, which="both", linewidth=0.4, alpha=0.4)
    axes.set_axisbelow(True)
    title = args.title + (f"\n{args.subtitle}" if args.subtitle else "")
    axes.set_title(title, fontsize=11, loc="left")
    axes.legend(fontsize=8, ncol=2, loc="upper left", framealpha=0.9)
    label_right_edge(axes, ends, log_y=True)

    figure.subplots_adjust(right=0.72, bottom=0.34)
    draw_footer(figure, footer_lines(marks, short_points(table), unit, basis,
                                     args.queue_share, args.concurrency_cap,
                                     args.keep_warmup))
    figure.savefig(args.output, dpi=args.dpi)
    print(f"wrote {args.output}  ({len(ends)} lines, "
          f"{rvc.counted_points(table)} points)")

    if args.table:
        write_table(args.table, TABLE_COLUMNS, table_rows(table, order, marks))
        print(f"wrote {args.table}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
