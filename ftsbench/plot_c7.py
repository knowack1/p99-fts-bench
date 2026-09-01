"""Render chart C7 — offered QPS against p99 latency, with the SLA knee.

On a laptop with the load generator on the same box, the knee this chart shows
is the generator's until proven otherwise. Three things keep that visible:

- **Saturated points are drawn differently and cannot define the knee.** The
  rule is the producer's (`SCHEMAS.md`): offered above half the calibrated
  generator ceiling, achieved below 95% of offered, or queue p99 above a quarter
  of latency p99. The recorded flag is what the chart obeys; the rule is
  re-evaluated here as well, and a disagreement is reported rather than hidden.
- **The generator ceiling is drawn**, as a vertical annotated line, plus the
  shaded half-ceiling region where the rule starts firing. A reader cannot see
  this chart without seeing where the harness ran out.
- **A knee is claimed only if an unsaturated point supports it.** Where every
  point is saturated, the chart says no knee is determinable instead of
  pointing at the last bend in the line.

    python3 -m ftsbench.plot_c7 \\
        --config opensearch:data/c7-opensearch-*.jsonl \\
        --config scylla-cdc:data/c7-scylla-cdc-*.jsonl \\
        --sla-ms 50 --output results/c7.png
"""
import argparse
import sys
from typing import Any

from . import plotlib

CHART = "C7"
RECORD = "sweep_point"
DEFAULT_OUTPUT = "results/c7.png"
DEFAULT_TITLE = "Sustained QPS under a p99 SLA"
DEFAULT_SLA_MS = 50.0
METRIC_NAME = "max_unsaturated_offered_qps"
NO_KNEE = "no knee determinable: no unsaturated point met the SLA"
SATURATION_RULE = ("generator_saturated (SCHEMAS.md): offered > 0.5x ceiling, or "
                   "achieved < 0.95x offered, or queue p99 > 0.25x latency p99, "
                   "or no operation succeeded")
NOTHING_SUCCEEDED = "no operation succeeded"


def subtitle_for(sla_ms: float) -> str:
    return (f"p99 of latency_ms at each offered rate; knee = highest unsaturated "
            f"rate holding p99 <= {sla_ms:g} ms")


def parse_args() -> argparse.Namespace:
    parser = plotlib.build_parser(CHART, __doc__, DEFAULT_OUTPUT, DEFAULT_TITLE,
                                  subtitle_for(DEFAULT_SLA_MS), height=6.5)
    parser.add_argument("--sla-ms", type=float, default=DEFAULT_SLA_MS,
                        help="p99 latency SLA drawn on the chart (ms)")
    args = parser.parse_args()
    if args.subtitle == subtitle_for(DEFAULT_SLA_MS):
        args.subtitle = subtitle_for(args.sla_ms)
    return args


def measured_nothing(point: dict[str, Any]) -> bool:
    """A rung whose every operation failed. Its percentiles are computed over an
    empty sample and reported as zero, which is the lowest p99 on the chart: left
    eligible, it wins the knee at the highest rate tested."""
    return int(point.get("count") or 0) <= 0


def is_saturated(point: dict[str, Any]) -> bool:
    """The recorded flag governs; `rule_saturated` only cross-checks it. A rung
    that measured nothing is excluded here too, so an artifact written before the
    producer learned to flag it cannot define a knee either."""
    return bool(point.get("generator_saturated")) or measured_nothing(point)


def rule_saturated(point: dict[str, Any]) -> list[str]:
    offered = float(point.get("offered_qps") or 0.0)
    ceiling = float(point.get("generator_ceiling_qps") or 0.0)
    achieved = float(point.get("achieved_qps") or 0.0)
    p99 = float(point.get("p99_ms") or 0.0)
    queue = float(point.get("queue_p99_ms") or 0.0)
    reasons = []
    if ceiling and offered > 0.5 * ceiling:
        reasons.append("offered > 0.5x generator ceiling")
    if offered and achieved < 0.95 * offered:
        reasons.append("achieved < 0.95x offered")
    if p99 and queue > 0.25 * p99:
        reasons.append("queue p99 > 0.25x latency p99")
    if measured_nothing(point):
        reasons.append(NOTHING_SUCCEEDED)
    return reasons


def unsaturated(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [point for point in points if not is_saturated(point)]


def max_unsaturated_qps(run: plotlib.Run) -> float:
    clean = unsaturated(run.records)
    if not clean:
        return 0.0
    return max(float(point.get("offered_qps") or 0.0) for point in clean)


def ordered_points(run: plotlib.Run) -> list[dict[str, Any]]:
    return sorted(run.records, key=lambda point: float(point.get("offered_qps") or 0.0))


def knee_point(points: list[dict[str, Any]],
               sla_ms: float) -> dict[str, Any] | None:
    """Saturated points are excluded by construction: a knee that is really the
    generator's ceiling is the failure mode this chart exists to avoid."""
    eligible = [point for point in unsaturated(points)
                if float(point.get("p99_ms") or 0.0) <= sla_ms]
    if not eligible:
        return None
    return max(eligible, key=lambda point: float(point.get("offered_qps") or 0.0))


def ceiling_of(points: list[dict[str, Any]]) -> float:
    ceilings = {float(point.get("generator_ceiling_qps") or 0.0) for point in points}
    return max(ceilings) if ceilings else 0.0


def draw_curve(axes: Any, name: str, points: list[dict[str, Any]],
               style: dict[str, Any]) -> None:
    axes.plot([point["offered_qps"] for point in points],
              [point["p99_ms"] for point in points], label=name, linewidth=1.4,
              alpha=0.85, zorder=2, **style)


def draw_markers(axes: Any, points: list[dict[str, Any]], color: str) -> None:
    clean = unsaturated(points)
    axes.plot([point["offered_qps"] for point in clean],
              [point["p99_ms"] for point in clean], linestyle="none", marker="o",
              markersize=6, color=color, zorder=3)
    dirty = [point for point in points if is_saturated(point)]
    axes.plot([point["offered_qps"] for point in dirty],
              [point["p99_ms"] for point in dirty], linestyle="none", marker="s",
              markersize=9, markerfacecolor="white", markeredgewidth=1.6,
              markeredgecolor=color, zorder=3)


SHARED_CEILING_COLOR = "#4a5568"


def ceiling_groups(summaries: dict[str, dict[str, Any]]) -> dict[float, list[str]]:
    """One line per distinct ceiling: configs calibrated on the same generator
    share one, and two identical annotated lines are just an unreadable one."""
    groups: dict[float, list[str]] = {}
    for name, summary in summaries.items():
        ceiling = summary["generator_ceiling_qps"]
        if ceiling:
            groups.setdefault(ceiling, []).append(name)
    return groups


def ceiling_label(names: list[str], ceiling: float, total: int) -> str:
    who = "" if len(names) == total else f"{', '.join(names)} "
    return f"{who}generator ceiling {ceiling:,.0f} qps"


def draw_ceiling(axes: Any, ceiling: float, color: str, label: str) -> None:
    axes.axvline(ceiling, color=color, linestyle=":", linewidth=1.4, alpha=0.9)
    axes.annotate(label, (ceiling, 0.03), xycoords=("data", "axes fraction"),
                  rotation=90, fontsize=8, color=color, ha="right", va="bottom")
    axes.axvspan(0.5 * ceiling, ceiling, color=color, alpha=0.06, zorder=0)


def draw_ceilings(axes: Any, summaries: dict[str, dict[str, Any]],
                  colors: dict[str, str]) -> None:
    groups = ceiling_groups(summaries)
    for ceiling, names in groups.items():
        shared = len(names) == len(summaries)
        color = SHARED_CEILING_COLOR if shared else colors[names[0]]
        draw_ceiling(axes, ceiling, color, ceiling_label(names, ceiling,
                                                         len(summaries)))


def draw_knee(axes: Any, point: dict[str, Any], color: str, name: str) -> None:
    axes.annotate(f"{name} knee: {point['offered_qps']:,.0f} qps "
                  f"@ p99 {point['p99_ms']:,.1f} ms",
                  (point["offered_qps"], point["p99_ms"]),
                  textcoords="offset points", xytext=(-12, 22), ha="right",
                  fontsize=8.5, color=color, weight="bold",
                  arrowprops={"arrowstyle": "->", "color": color, "linewidth": 1.2})


def draw_sla(axes: Any, sla_ms: float) -> None:
    axes.axhline(sla_ms, color="#8c1d13", linestyle="--", linewidth=1.3)
    axes.annotate(f"p99 SLA {sla_ms:g} ms", (0.995, sla_ms),
                  xycoords=("axes fraction", "data"), fontsize=8.5,
                  color="#8c1d13", va="bottom", ha="right")


def point_summary(point: dict[str, Any]) -> dict[str, Any]:
    return {
        "offered_qps": point.get("offered_qps"),
        "achieved_qps": point.get("achieved_qps"),
        "p99_ms": point.get("p99_ms"),
        "queue_p99_ms": point.get("queue_p99_ms"),
        "count": point.get("count"),
        "errors": point.get("errors"),
        "generator_saturated": is_saturated(point),
        "saturation_reasons": rule_saturated(point),
    }


def flag_disagreements(points: list[dict[str, Any]]) -> list[float]:
    return [float(point.get("offered_qps") or 0.0) for point in points
            if is_saturated(point) != bool(rule_saturated(point))]


def config_summary(points: list[dict[str, Any]], knee: dict[str, Any] | None,
                   sla_ms: float) -> dict[str, Any]:
    return {
        "sla_ms": sla_ms,
        "generator_ceiling_qps": ceiling_of(points),
        "points": [point_summary(point) for point in points],
        "points_total": len(points),
        "points_saturated": len(points) - len(unsaturated(points)),
        "knee": point_summary(knee) if knee else None,
        "knee_status": "unsaturated point at or under the SLA" if knee else NO_KNEE,
        "flag_disagreement_at_qps": flag_disagreements(points),
    }


def plot_config(axes: Any, config: plotlib.ConfigSeries, index: int,
                sla_ms: float) -> dict[str, Any]:
    points = ordered_points(config.chosen)
    style = plotlib.style_for(config.name, index)
    color = style["color"]
    draw_curve(axes, config.name, points, style)
    draw_markers(axes, points, color)
    knee = knee_point(points, sla_ms)
    if knee:
        draw_knee(axes, knee, color, config.name)
    return config_summary(points, knee, sla_ms)


def config_colors(configs: list[plotlib.ConfigSeries]) -> dict[str, str]:
    return {config.name: plotlib.style_for(config.name, index)["color"]
            for index, config in enumerate(configs)}


def annotate_no_knee(axes: Any, summaries: dict[str, dict[str, Any]]) -> None:
    missing = [name for name, summary in summaries.items() if not summary["knee"]]
    if not missing:
        return
    axes.text(0.99, 0.03, "\n".join(f"{name}: {NO_KNEE}" for name in missing),
              transform=axes.transAxes, fontsize=8.5, color="#8c1d13",
              ha="right", va="bottom", weight="bold",
              bbox={"boxstyle": "round,pad=0.4", "facecolor": "#fdecea",
                    "edgecolor": "#8c1d13", "linewidth": 0.8})


def saturation_note(summaries: dict[str, dict[str, Any]]) -> str:
    counts = ", ".join(f"{name}: {summary['points_saturated']} of "
                       f"{summary['points_total']}"
                       for name, summary in summaries.items())
    return (f"hollow squares are generator-saturated points, excluded from the "
            f"knee — {counts}. {SATURATION_RULE}")


def disagreement_at(name: str, qps_values: list[float]) -> str:
    return f"{name} at {', '.join(f'{qps:,.0f}' for qps in qps_values)} qps"


def disagreement_note(summaries: dict[str, dict[str, Any]]) -> str:
    parts = [disagreement_at(name, summary["flag_disagreement_at_qps"])
             for name, summary in summaries.items()
             if summary["flag_disagreement_at_qps"]]
    if not parts:
        return ""
    return ("recorded generator_saturated disagrees with the rule re-evaluated "
            "from the same fields at " + "; ".join(parts))


def total_errors(summary: dict[str, Any]) -> int:
    return sum(int(point["errors"] or 0) for point in summary["points"])


def error_note(summaries: dict[str, dict[str, Any]]) -> str:
    failing = [f"{name}: {total_errors(summary):,}"
               for name, summary in summaries.items()
               if total_errors(summary)]
    if not failing:
        return "no errors recorded at any ladder rung"
    return ("errors, excluded from the percentiles and counted here — "
            + "; ".join(failing))


def plot(args: argparse.Namespace,
         configs: list[plotlib.ConfigSeries]) -> tuple[Any, dict[str, Any]]:
    figure, axes = plotlib.plt.subplots(figsize=(args.width, args.height))
    summaries = {config.name: plot_config(axes, config, index, args.sla_ms)
                 for index, config in enumerate(configs)}
    draw_ceilings(axes, summaries, config_colors(configs))
    draw_sla(axes, args.sla_ms)
    annotate_no_knee(axes, summaries)
    axes.set_yscale("log")
    axes.set_xlim(left=0)
    axes.legend(loc="upper left", fontsize=8, framealpha=0.92)
    plotlib.frame(axes, args, "offered QPS", "p99 search latency (ms, log scale)")
    return figure, extras(args, summaries)


def extras(args: argparse.Namespace,
           summaries: dict[str, dict[str, Any]]) -> dict[str, Any]:
    notes = [saturation_note(summaries), disagreement_note(summaries),
             error_note(summaries)]
    return {
        "metric_name": METRIC_NAME,
        "notes": [note for note in notes if note],
        "sla_ms": args.sla_ms,
        "x_axis": "offered_qps (the rate requested, not achieved)",
        "y_axis": "p99_ms over latency_ms, warmup excluded, log scale",
        "knee_rule": "highest offered_qps among points where generator_saturated "
                     "is false and p99_ms <= sla_ms",
        "saturation_rule": SATURATION_RULE,
        "per_config": summaries,
    }


def main() -> int:
    args = parse_args()
    return plotlib.emit(args, RECORD, max_unsaturated_qps, plot)


if __name__ == "__main__":
    sys.exit(main())
