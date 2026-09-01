"""Render chart C8 — write-to-searchable freshness, median and observed range.

A single freshness sample is a coin toss about where in the refresh cycle the
write landed, so the unit of observation here is the **probe, not the run**: all
repetitions of a config are pooled and every probe is drawn. The bar is the
median, the whisker is the full observed min-max, and the individual probes sit
on top of it, because with 20 probes a box plot's quartiles would be decoration.

Two things this chart must not hide:

- **Probes that never became searchable.** `timed_out: true` is a finding. Those
  probes have no lag to plot, so they are drawn as red crosses above the bar
  with a count, never dropped and never treated as a large lag.
- **The poll resolution.** `poll_interval_s` bounds what the harness can see; a
  median at or below one poll interval is the poll interval, and the chart says
  so instead of claiming sub-poll freshness.

    python3 -m ftsbench.plot_c8 \\
        --config scylla-cdc:data/c8-scylla-cdc-*.jsonl \\
        --config opensearch:data/c8-opensearch-*.jsonl \\
        --config opensearch-refresh30:data/c8-opensearch-refresh30-*.jsonl \\
        --output results/c8.png
"""
import argparse
import statistics
import sys
from typing import Any

from . import plotlib

CHART = "C8"
RECORD = "freshness_probe"
DEFAULT_OUTPUT = "results/c8.png"
DEFAULT_TITLE = "Write-to-searchable freshness"
DEFAULT_SUBTITLE = ("median lag with the full observed range over pooled probes; "
                    "probes that never became searchable are shown, not dropped")
METRIC_NAME = "median_lag_s"
POOLED_PROBES = ("probes from every repetition are pooled — the unit of "
                 "observation is a probe, not a run, because one probe only "
                 "samples where in the refresh cycle the write landed")
JITTER_STEP = 0.05
JITTER_SPAN = 5


def parse_args() -> argparse.Namespace:
    parser = plotlib.build_parser(CHART, __doc__, DEFAULT_OUTPUT, DEFAULT_TITLE,
                                  DEFAULT_SUBTITLE, disclose_write_path=True,
                                  height=6.5)
    return parser.parse_args()


def lag_values(probes: list[dict[str, Any]]) -> list[float]:
    return [float(probe["lag_s"]) for probe in probes
            if not probe.get("timed_out") and probe.get("lag_s") is not None]


def timed_out(probes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [probe for probe in probes if probe.get("timed_out")]


def run_median_lag(run: plotlib.Run) -> float:
    lags = lag_values(run.records)
    return statistics.median(lags) if lags else plotlib.UNRESOLVED


def pooled_probes(config: plotlib.ConfigSeries) -> list[dict[str, Any]]:
    return [probe for run in config.runs for probe in run.records]


def poll_interval(probes: list[dict[str, Any]]) -> float:
    intervals = {float(probe["poll_interval_s"]) for probe in probes
                 if probe.get("poll_interval_s") is not None}
    return max(intervals) if intervals else 0.0


def refresh_interval(probes: list[dict[str, Any]], header_value: str = "") -> str:
    values = {str(probe.get("refresh_interval")) for probe in probes
              if probe.get("refresh_interval") not in (None, "", "n/a")}
    if values:
        return ", ".join(sorted(values))
    return str(header_value) if header_value else "UNRECORDED"


def jitter(index: int) -> float:
    return (index % JITTER_SPAN - (JITTER_SPAN - 1) / 2) * JITTER_STEP


def draw_bar(axes: Any, position: int, median: float, color: str) -> None:
    axes.bar(position, median, width=0.5, color=color, alpha=0.85, zorder=2)


def draw_labels(axes: Any, position: int, median: float, lags: list[float]) -> None:
    """Both labels stack above the whisker: on a short bar a label at the bar top
    and a label at the range top land on each other."""
    high = max(lags)
    axes.annotate(f"median {median:,.2f} s", (position, high),
                  textcoords="offset points", xytext=(0, 20), ha="center",
                  fontsize=9, weight="bold")
    axes.annotate(f"observed {min(lags):,.2f}–{high:,.2f} s", (position, high),
                  textcoords="offset points", xytext=(0, 7), ha="center",
                  fontsize=7.5, color="#1a202c")


def draw_range(axes: Any, position: int, lags: list[float]) -> None:
    low, high = min(lags), max(lags)
    axes.plot([position, position], [low, high], color="#1a202c", linewidth=1.3,
              zorder=4)
    for value in (low, high):
        axes.plot([position - 0.12, position + 0.12], [value, value],
                  color="#1a202c", linewidth=1.3, zorder=4)


def draw_probes(axes: Any, position: int, lags: list[float], color: str) -> None:
    axes.plot([position + jitter(index) for index in range(len(lags))], lags,
              linestyle="none", marker="o", markersize=3.5, color=color,
              markeredgecolor="white", markeredgewidth=0.4, alpha=0.9, zorder=5)


def draw_timeouts(axes: Any, position: int, count: int, total: int,
                  height: float) -> None:
    if not count:
        return
    axes.plot([position], [height], marker="x", markersize=11, markeredgewidth=2.2,
              color="#8c1d13", zorder=6)
    axes.annotate(f"{count} of {total} probes\nnever became searchable",
                  (position, height), textcoords="offset points", xytext=(12, 0),
                  ha="left", va="center", fontsize=8, weight="bold",
                  color="#8c1d13")


def config_summary(probes: list[dict[str, Any]], lags: list[float],
                   repetitions: int, header_refresh: str = "") -> dict[str, Any]:
    return {
        "probes": len(probes),
        "probes_searchable": len(lags),
        "probes_timed_out": len(timed_out(probes)),
        "repetitions_pooled": repetitions,
        "poll_interval_s": poll_interval(probes),
        "refresh_interval": refresh_interval(probes, header_refresh),
        "median_lag_s": round(statistics.median(lags), 3) if lags else None,
        "observed_range_s": plotlib.spread(lags) if lags else {},
        "at_or_below_poll_interval": bool(lags) and
                                     statistics.median(lags) <= poll_interval(probes),
    }


def plot_config(axes: Any, config: plotlib.ConfigSeries,
                position: int) -> dict[str, Any]:
    probes = pooled_probes(config)
    lags = lag_values(probes)
    color = plotlib.style_for(config.name, position)["color"]
    if lags:
        draw_bar(axes, position, statistics.median(lags), color)
        draw_range(axes, position, lags)
        draw_probes(axes, position, lags, color)
        draw_labels(axes, position, statistics.median(lags), lags)
    return config_summary(probes, lags, config.repetitions,
                          config.chosen.field("refresh_interval", ""))


def observed_ceiling(summaries: dict[str, dict[str, Any]]) -> float:
    highs = [summary["observed_range_s"].get("max", 0.0)
             for summary in summaries.values() if summary["observed_range_s"]]
    return max(highs) if highs else 1.0


def timeout_height(summaries: dict[str, dict[str, Any]]) -> float:
    return observed_ceiling(summaries) * 1.26


def any_timeouts(summaries: dict[str, dict[str, Any]]) -> bool:
    return any(summary["probes_timed_out"] for summary in summaries.values())


def mark_timeouts(axes: Any, summaries: dict[str, dict[str, Any]]) -> None:
    height = timeout_height(summaries)
    for position, summary in enumerate(summaries.values()):
        draw_timeouts(axes, position, summary["probes_timed_out"],
                      summary["probes"], height)


def label_configs(axes: Any, summaries: dict[str, dict[str, Any]]) -> None:
    labels = [f"{name}\nrefresh={summary['refresh_interval']}"
              for name, summary in summaries.items()]
    axes.set_xticks(range(len(labels)))
    axes.set_xticklabels(labels)
    axes.set_xlim(-0.6, len(labels) - 0.4)


def poll_note(summaries: dict[str, dict[str, Any]]) -> str:
    intervals = sorted({summary["poll_interval_s"] for summary in summaries.values()})
    stated = ", ".join(f"{value:g} s" for value in intervals)
    return (f"poll interval {stated} bounds the resolution: a lag cannot be "
            f"resolved finer than one poll")


def resolution_warning(summaries: dict[str, dict[str, Any]]) -> str:
    hit = [name for name, summary in summaries.items()
           if summary["at_or_below_poll_interval"]]
    if not hit:
        return ""
    return (f"median at or below one poll interval for {', '.join(hit)} — that "
            f"number is the poll interval, not the engine")


def timeout_note(summaries: dict[str, dict[str, Any]]) -> str:
    parts = [f"{name}: {summary['probes_timed_out']} of {summary['probes']}"
             for name, summary in summaries.items() if summary["probes_timed_out"]]
    if not parts:
        return "every probe became searchable in the plotted runs"
    return ("probes that never became searchable, drawn as a cross and excluded "
            "from the median and range — " + "; ".join(parts))


def pooling_note(summaries: dict[str, dict[str, Any]]) -> str:
    counts = ", ".join(f"{name}: {summary['probes']} probes over "
                       f"{summary['repetitions_pooled']} repetitions"
                       for name, summary in summaries.items())
    return f"{POOLED_PROBES} — {counts}"


def plot(args: argparse.Namespace,
         configs: list[plotlib.ConfigSeries]) -> tuple[Any, dict[str, Any]]:
    figure, axes = plotlib.plt.subplots(figsize=(args.width, args.height))
    summaries = {config.name: plot_config(axes, config, position)
                 for position, config in enumerate(configs)}
    mark_timeouts(axes, summaries)
    label_configs(axes, summaries)
    headroom = 1.42 if any_timeouts(summaries) else 1.22
    axes.set_ylim(0, observed_ceiling(summaries) * headroom)
    plotlib.frame(axes, args, "configuration", "write-to-searchable lag (s)")
    return figure, extras(summaries)


def extras(summaries: dict[str, dict[str, Any]]) -> dict[str, Any]:
    notes = [pooling_note(summaries), poll_note(summaries),
             resolution_warning(summaries), timeout_note(summaries)]
    return {
        "metric_name": METRIC_NAME,
        "notes": [note for note in notes if note],
        "run_selection": POOLED_PROBES,
        "x_axis": "configuration, labelled with its refresh interval",
        "y_axis": "lag_s = t_searchable_s - t_write_s, per probe",
        "central_tendency": "median over pooled probes; the whisker is the full "
                            "observed min-max, not a percentile",
        "per_config": summaries,
    }


def main() -> int:
    args = parse_args()
    return plotlib.emit(args, RECORD, run_median_lag, plot)


if __name__ == "__main__":
    sys.exit(main())
