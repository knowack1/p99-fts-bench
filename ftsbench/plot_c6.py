"""Render chart C6 — search latency per query class, p50 / p95 / p99.

The point of the chart is that it has room for a class the other engine wins;
a per-class chart that only ever shows one colour in front is a chart nobody
needs. So all six classes the query generator emits are always drawn in the
same order, and a class with no data appears as a labelled gap rather than
being dropped — a missing class is a finding about the engine or the harness,
not a reason to shorten the axis.

Within a class each config contributes p50, p95 and p99 as three bars of
increasing opacity. A percentile is drawn only if that class's own sample count
can support it (`stats.min_samples_for`): p99 from 40 queries in a class is the
maximum of 40 queries, and pooling classes to reach the floor would defeat the
chart. Refused percentiles are marked in place and named in the footer.

    python3 -m ftsbench.plot_c6 \\
        --config opensearch:data/c6-opensearch-*.jsonl \\
        --config scylla-cdc:data/c6-scylla-cdc-*.jsonl \\
        --output results/c6.png
"""
import argparse
import collections
import sys
from typing import Any

from matplotlib.patches import Patch

from . import plotlib
from .stats import min_samples_for

CHART = "C6"
RECORD = "latency_op"
DEFAULT_OUTPUT = "results/c6.png"
DEFAULT_TITLE = "Search latency by query class"
DEFAULT_SUBTITLE = ("p50 / p95 / p99 of latency_ms per class, log scale; a class "
                    "with too few queries for a percentile shows the gap")
METRIC_NAME = "search_p50_ms"
PERCENTILES = (50.0, 95.0, 99.0)
PERCENTILE_ALPHA = {50.0: 0.45, 95.0: 0.7, 99.0: 1.0}
GROUP_WIDTH = 0.82


def parse_args() -> argparse.Namespace:
    parser = plotlib.build_parser(CHART, __doc__, DEFAULT_OUTPUT, DEFAULT_TITLE,
                                  DEFAULT_SUBTITLE, width=12.0, height=6.5)
    return parser.parse_args()


def searches(run: plotlib.Run) -> list[dict[str, Any]]:
    return [item for item in run.records if item.get("op") == "search"]


def by_class(run: plotlib.Run) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for record in searches(run):
        grouped[str(record.get("class"))].append(record)
    return {name: grouped.get(name, []) for name in plotlib.QUERY_CLASSES}


def run_p50(run: plotlib.Run) -> float:
    latencies = plotlib.latency_ms_values(searches(run))
    if not latencies:
        return plotlib.UNRESOLVED
    return plotlib.percentiles(latencies, [50.0])[50.0]


def slot_width(config_count: int) -> float:
    return GROUP_WIDTH / (config_count * len(PERCENTILES))


def slot_centre(group: int, config_index: int, pct_index: int,
                config_count: int) -> float:
    width = slot_width(config_count)
    slot = config_index * len(PERCENTILES) + pct_index
    return group - GROUP_WIDTH / 2 + width * (slot + 0.5)


def class_values(latencies: list[float]) -> dict[float, float]:
    supported = plotlib.supported_percentiles(len(latencies), PERCENTILES)
    return plotlib.percentiles(latencies, supported) if supported else {}


def draw_bar(axes: Any, position: float, value: float, pct: float,
             width: float, color: str) -> None:
    axes.bar(position, value, width=width * 0.92, color=color,
             alpha=PERCENTILE_ALPHA[pct], zorder=2, linewidth=0)


def mark_refused(axes: Any, position: float, floor: float) -> None:
    axes.plot([position], [floor], marker="x", markersize=5, color="#8c1d13",
              zorder=3)


def draw_class(axes: Any, group: int, latencies: list[float], color: str,
               config_index: int, config_count: int, floor: float) -> list[float]:
    values = class_values(latencies)
    width = slot_width(config_count)
    for pct_index, pct in enumerate(PERCENTILES):
        position = slot_centre(group, config_index, pct_index, config_count)
        if pct in values:
            draw_bar(axes, position, values[pct], pct, width, color)
        else:
            mark_refused(axes, position, floor)
    return [pct for pct in PERCENTILES if pct not in values]


def plot_config(axes: Any, config: plotlib.ConfigSeries, config_index: int,
                config_count: int, floor: float) -> dict[str, Any]:
    grouped = by_class(config.chosen)
    color = plotlib.style_for(config.name, config_index)["color"]
    per_class = {}
    for group, name in enumerate(plotlib.QUERY_CLASSES):
        latencies = plotlib.latency_ms_values(grouped[name])
        refused = draw_class(axes, group, latencies, color, config_index,
                             config_count, floor)
        per_class[name] = class_summary(grouped[name], latencies, refused)
    return {"per_class": per_class, "color": color,
            "errors": plotlib.error_count(searches(config.chosen))}


def class_summary(records: list[dict[str, Any]], latencies: list[float],
                  refused: list[float]) -> dict[str, Any]:
    values = class_values(latencies)
    return {
        "queries": len(records),
        "queries_ok": len(latencies),
        "errors": plotlib.error_count(records),
        "percentiles_drawn": {plotlib.percentile_label(pct): round(value, 3)
                              for pct, value in values.items()},
        "percentiles_refused": [plotlib.percentile_label(pct) for pct in refused],
        "mean_hits": mean_hits(records),
    }


def mean_hits(records: list[dict[str, Any]]) -> float | None:
    hits = [float(item["hits"]) for item in records if item.get("hits") is not None]
    return round(sum(hits) / len(hits), 1) if hits else None


def empty_classes(summaries: dict[str, dict[str, Any]]) -> list[str]:
    return [name for name in plotlib.QUERY_CLASSES
            if all(summary["per_class"][name]["queries"] == 0
                   for summary in summaries.values())]


def mark_empty_classes(axes: Any, summaries: dict[str, dict[str, Any]],
                       floor: float) -> None:
    for name in empty_classes(summaries):
        axes.text(plotlib.QUERY_CLASSES.index(name), floor, "no data",
                  rotation=90, ha="center", va="bottom", fontsize=8,
                  color="#8c1d13", weight="bold")


def refusal_notes(summaries: dict[str, dict[str, Any]]) -> list[str]:
    notes = []
    for name, summary in summaries.items():
        for query_class, entry in summary["per_class"].items():
            if entry["percentiles_refused"] and entry["queries_ok"]:
                notes.append(plotlib.refusal_note(
                    f"{name}/{query_class}", entry["queries_ok"],
                    [pct for pct in PERCENTILES
                     if plotlib.percentile_label(pct) in entry["percentiles_refused"]]))
    return notes


def empty_note(summaries: dict[str, dict[str, Any]]) -> str:
    missing = empty_classes(summaries)
    if not missing:
        return ""
    return (f"no queries recorded for {', '.join(missing)} in any config — drawn "
            f"as a gap, not removed from the axis")


def error_note(summaries: dict[str, dict[str, Any]]) -> str:
    failing = [f"{name}: {summary['errors']:,}"
               for name, summary in summaries.items() if summary["errors"]]
    if not failing:
        return "no failed searches in the plotted runs"
    return ("failed searches, excluded from the percentiles and counted here — "
            + "; ".join(failing))


def refusal_marker_note() -> str:
    return (f"a red x marks a percentile the class could not support "
            f"(p95 needs {min_samples_for(95.0):,}, p99 needs "
            f"{min_samples_for(99.0):,} queries in that class)")


def percentile_handles(color: str) -> list[Patch]:
    return [Patch(facecolor=color, alpha=PERCENTILE_ALPHA[pct],
                  label=plotlib.percentile_label(pct)) for pct in PERCENTILES]


def legend_handles(summaries: dict[str, dict[str, Any]]) -> list[Patch]:
    configs = [Patch(facecolor=summary["color"], label=name)
               for name, summary in summaries.items()]
    shades = percentile_handles(next(iter(summaries.values()))["color"])
    return configs + shades


def label_classes(axes: Any) -> None:
    axes.set_xticks(range(len(plotlib.QUERY_CLASSES)))
    axes.set_xticklabels(plotlib.QUERY_CLASSES)
    axes.set_xlim(-0.6, len(plotlib.QUERY_CLASSES) - 0.4)


def axis_floor(configs: list[plotlib.ConfigSeries]) -> float:
    values = [value for config in configs
              for latencies in (plotlib.latency_ms_values(searches(config.chosen)),)
              if latencies
              for value in plotlib.percentiles(latencies, [50.0]).values()]
    return min(values) / 3.0 if values else 0.1


def plot(args: argparse.Namespace,
         configs: list[plotlib.ConfigSeries]) -> tuple[Any, dict[str, Any]]:
    figure, axes = plotlib.plt.subplots(figsize=(args.width, args.height))
    floor = axis_floor(configs)
    summaries = {config.name: plot_config(axes, config, index, len(configs), floor)
                 for index, config in enumerate(configs)}
    axes.set_yscale("log")
    axes.set_ylim(bottom=floor)
    label_classes(axes)
    mark_empty_classes(axes, summaries, floor)
    axes.legend(handles=legend_handles(summaries), loc="upper left", fontsize=8,
                framealpha=0.92, ncol=2)
    plotlib.frame(axes, args, "query class", "search latency (ms, log scale)")
    return figure, extras(summaries)


def extras(summaries: dict[str, dict[str, Any]]) -> dict[str, Any]:
    notes = [refusal_marker_note(), empty_note(summaries), error_note(summaries),
             *refusal_notes(summaries)]
    return {
        "metric_name": METRIC_NAME,
        "notes": [note for note in notes if note],
        "query_classes": list(plotlib.QUERY_CLASSES),
        "percentiles": [plotlib.percentile_label(pct) for pct in PERCENTILES],
        "x_axis": "query class, fixed order, empty classes retained",
        "y_axis": "latency_ms, log scale",
        "per_config": {name: {"per_class": summary["per_class"],
                              "errors": summary["errors"]}
                       for name, summary in summaries.items()},
    }


def main() -> int:
    args = parse_args()
    return plotlib.emit(args, RECORD, run_p50, plot)


if __name__ == "__main__":
    sys.exit(main())
