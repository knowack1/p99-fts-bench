"""Render chart C5 — search latency by percentile on a log-percentile axis.

The axis is `-log10(1 - p/100)`, so every decade of rarity gets equal width and
the tail is not compressed into the right-hand pixel. That is also what makes
the chart dangerous: the further right it extends, the fewer observations each
point rests on, and a smooth line drawn to p99.99 from 300 requests is a rename
of the maximum.

So this module refuses. `stats.min_samples_for` gives the sample count at which
a percentile has at least one observation at or beyond it (p99 needs 100,
p99.9 needs 1,001, p99.99 needs 10,000). Percentiles below that floor are not
drawn, the axis is truncated to the deepest percentile any config can support,
and the refusal is printed on the chart — a reader who never opens the sidecar
still sees which percentiles were withheld and why. Percentiles that clear the
floor but not ten times it are drawn hollow: defined, not yet reproducible.

    python3 -m ftsbench.plot_c5 \\
        --config opensearch:data/c5-opensearch-*.jsonl \\
        --config scylla-cdc:data/c5-scylla-cdc-*.jsonl \\
        --query-class rare_term --output results/c5.png
"""
import argparse
import functools
import math
import sys
from typing import Any

from . import plotlib
from .stats import is_stable, min_samples_for

CHART = "C5"
RECORD = "latency_op"
DEFAULT_OUTPUT = "results/c5.png"
DEFAULT_TITLE = "Search latency by percentile"
DEFAULT_SUBTITLE = ("latency_ms on a log-percentile axis; a percentile the sample "
                    "count cannot support is not drawn and the axis stops there")
METRIC_NAME = "search_p99_ms"
CANDIDATE_PERCENTILES = (50.0, 90.0, 95.0, 99.0, 99.9, 99.99)
UNSTABLE_NOTE = ("hollow markers clear the 1/(1-p) floor but not 10x it: defined "
                 "for this run, not yet reproducible between runs")


def parse_args() -> argparse.Namespace:
    parser = plotlib.build_parser(CHART, __doc__, DEFAULT_OUTPUT, DEFAULT_TITLE,
                                  DEFAULT_SUBTITLE, height=6.5)
    parser.add_argument("--query-class", default="",
                        choices=("",) + plotlib.QUERY_CLASSES,
                        help="restrict to one query class (default: all pooled)")
    return parser.parse_args()


def matches_class(record: dict[str, Any], query_class: str) -> bool:
    return not query_class or record.get("class") == query_class


def search_records(run: plotlib.Run, query_class: str) -> list[dict[str, Any]]:
    return [item for item in run.records
            if item.get("op") == "search" and matches_class(item, query_class)]


def run_search_p99(run: plotlib.Run, query_class: str = "") -> float:
    latencies = plotlib.latency_ms_values(search_records(run, query_class))
    if len(latencies) < min_samples_for(99.0):
        return plotlib.UNRESOLVED
    return plotlib.percentiles(latencies, [99.0])[99.0]


def percentile_x(pct: float) -> float:
    """Equal width per decade of rarity: p50 -> 0.30, p99 -> 2, p99.99 -> 4."""
    return -math.log10(1.0 - pct / 100.0)


def support_split(count: int) -> tuple[list[float], list[float]]:
    supported = plotlib.supported_percentiles(count, CANDIDATE_PERCENTILES)
    refused = [pct for pct in CANDIDATE_PERCENTILES if pct not in supported]
    return supported, refused


def stability_split(count: int,
                    supported: list[float]) -> tuple[list[float], list[float]]:
    stable = [pct for pct in supported if is_stable(count, pct)]
    return stable, [pct for pct in supported if pct not in stable]


def draw_curve(axes: Any, label: str, values: dict[float, float],
               style: dict[str, Any]) -> None:
    axes.plot([percentile_x(pct) for pct in values], list(values.values()),
              label=label, marker="o", markersize=5, linewidth=1.7, **style)


def draw_unstable(axes: Any, values: dict[float, float], unstable: list[float],
                  style: dict[str, Any]) -> None:
    axes.plot([percentile_x(pct) for pct in unstable],
              [values[pct] for pct in unstable], linestyle="none", marker="o",
              markersize=9, markerfacecolor="white", markeredgewidth=1.6,
              markeredgecolor=style["color"], zorder=4)


def curve_label(name: str, count: int) -> str:
    return f"{name} (n={count:,})"


def plot_config(axes: Any, config: plotlib.ConfigSeries, index: int,
                query_class: str) -> dict[str, Any]:
    records = search_records(config.chosen, query_class)
    latencies = plotlib.latency_ms_values(records)
    supported, refused = support_split(len(latencies))
    values = plotlib.percentiles(latencies, supported) if supported else {}
    style = plotlib.style_for(config.name, index)
    style.pop("linestyle", None)
    if values:
        draw_curve(axes, curve_label(config.name, len(latencies)), values, style)
        draw_unstable(axes, values, stability_split(len(latencies), supported)[1],
                      style)
    return summary_for(records, latencies, supported, refused, values)


def summary_for(records: list[dict[str, Any]], latencies: list[float],
                supported: list[float], refused: list[float],
                values: dict[float, float]) -> dict[str, Any]:
    stable, unstable = stability_split(len(latencies), supported)
    return {
        "search_operations": len(records),
        "search_operations_ok": len(latencies),
        "errors": plotlib.error_count(records),
        "percentiles_drawn": {plotlib.percentile_label(pct): round(value, 3)
                              for pct, value in values.items()},
        "percentiles_refused": [plotlib.percentile_label(pct) for pct in refused],
        "percentiles_unstable": [plotlib.percentile_label(pct) for pct in unstable],
        "samples_required": {plotlib.percentile_label(pct): min_samples_for(pct)
                             for pct in CANDIDATE_PERCENTILES},
        "stable_percentiles": [plotlib.percentile_label(pct) for pct in stable],
    }


def deepest_drawn(summaries: dict[str, dict[str, Any]]) -> float:
    drawn = [pct for pct in CANDIDATE_PERCENTILES
             if any(plotlib.percentile_label(pct) in summary["percentiles_drawn"]
                    for summary in summaries.values())]
    return max(drawn) if drawn else 50.0


def axis_percentiles(limit: float) -> list[float]:
    return [pct for pct in CANDIDATE_PERCENTILES if pct <= limit]


def label_percentile_axis(axes: Any, limit: float) -> None:
    ticks = axis_percentiles(limit)
    axes.set_xticks([percentile_x(pct) for pct in ticks])
    axes.set_xticklabels([plotlib.percentile_label(pct) for pct in ticks])
    axes.set_xlim(percentile_x(50.0) - 0.12, percentile_x(limit) + 0.12)


def refusal_notes(summaries: dict[str, dict[str, Any]]) -> list[str]:
    notes = []
    for name, summary in summaries.items():
        refused = [pct for pct in CANDIDATE_PERCENTILES
                   if plotlib.percentile_label(pct) in summary["percentiles_refused"]]
        if refused:
            notes.append(plotlib.refusal_note(
                name, summary["search_operations_ok"], refused))
    return notes


def truncation_text(limit: float, summaries: dict[str, dict[str, Any]]) -> str:
    withheld = [pct for pct in CANDIDATE_PERCENTILES if pct > limit]
    if not withheld:
        return ""
    labels = ", ".join(plotlib.percentile_label(pct) for pct in withheld)
    return (f"axis truncated at {plotlib.percentile_label(limit)} — {labels} not "
            f"drawn for any config (needs {min_samples_for(min(withheld)):,}+ "
            f"successful searches)")


def annotate_refusal(axes: Any, lines: list[str]) -> None:
    if not lines:
        return
    axes.text(0.99, 0.03, "\n".join(lines), transform=axes.transAxes,
              fontsize=8, color="#8c1d13", ha="right", va="bottom",
              bbox={"boxstyle": "round,pad=0.4", "facecolor": "#fdecea",
                    "edgecolor": "#8c1d13", "linewidth": 0.8})


def class_note(query_class: str) -> str:
    if query_class:
        return f"one query class only: {query_class}"
    return ("all query classes pooled — a pooled tail mixes cheap and expensive "
            "query shapes; use --query-class for the headline class")


def error_note(summaries: dict[str, dict[str, Any]]) -> str:
    failing = [f"{name}: {summary['errors']:,} of {summary['search_operations']:,}"
               for name, summary in summaries.items() if summary["errors"]]
    if not failing:
        return "no failed searches in the plotted runs"
    return ("failed searches, excluded from the percentiles and counted here — "
            + "; ".join(failing))


def unstable_note(summaries: dict[str, dict[str, Any]]) -> str:
    marked = [f"{name}: {', '.join(summary['percentiles_unstable'])}"
              for name, summary in summaries.items()
              if summary["percentiles_unstable"]]
    return f"{UNSTABLE_NOTE} — {'; '.join(marked)}" if marked else ""


def plot(args: argparse.Namespace,
         configs: list[plotlib.ConfigSeries]) -> tuple[Any, dict[str, Any]]:
    figure, axes = plotlib.plt.subplots(figsize=(args.width, args.height))
    summaries = {config.name: plot_config(axes, config, index, args.query_class)
                 for index, config in enumerate(configs)}
    limit = deepest_drawn(summaries)
    label_percentile_axis(axes, limit)
    axes.set_yscale("log")
    axes.legend(loc="upper left", fontsize=8, framealpha=0.92)
    annotate_refusal(axes, refusal_notes(summaries) + [truncation_text(limit,
                                                                      summaries)])
    plotlib.frame(axes, args, "percentile (log scale of 1/(1-p))",
                  "search latency (ms, log scale)")
    return figure, extras(args, limit, summaries)


def extras(args: argparse.Namespace, limit: float,
           summaries: dict[str, dict[str, Any]]) -> dict[str, Any]:
    notes = [class_note(args.query_class), truncation_text(limit, summaries),
             unstable_note(summaries), error_note(summaries),
             *refusal_notes(summaries)]
    return {
        "metric_name": METRIC_NAME,
        "notes": [note for note in notes if note],
        "query_class": args.query_class or "all pooled",
        "x_axis": "percentile, transformed as -log10(1 - p/100)",
        "y_axis": "latency_ms, log scale",
        "deepest_percentile_drawn": plotlib.percentile_label(limit),
        "percentile_floor_rule": "a percentile is drawn only at 1/(1-p/100) "
                                 "samples or more (stats.min_samples_for)",
        "stability_rule": "10x the floor before a percentile is reproducible; "
                          "below that it is drawn hollow",
        "per_config": summaries,
    }


def main() -> int:
    args = parse_args()
    metric = functools.partial(run_search_p99, query_class=args.query_class)
    return plotlib.emit(args, RECORD, metric, plot)


if __name__ == "__main__":
    sys.exit(main())
