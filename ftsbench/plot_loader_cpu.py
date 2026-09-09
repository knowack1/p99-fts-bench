"""What the loaders cost the harness box, over the same grid.

Two modes, because a flat throughput line has two different explanations and
only CPU separates them:

- `--mode summary`: box cores used against total concurrency, one line per arm.
  The median tick is solid, the peak tick is a hollow marker above it — peak
  rather than mean for the reason `verify_cpu_usage` gives, that a run has a
  ramp and a tail and the mean over both understates what was reached. Two
  reference lines: the box's own core count, and `loader_core_bound_at x cores`,
  the level above which `verify_generator` calls a point client-bound.
- `--mode timeline`: cores used against elapsed time, one facet per arm at that
  arm's best rung, median repetition. Summed loader CPU against whole-box CPU,
  so the gap between them is the probe, the parent and everything else on the
  box, and `steal_cores` on a twin axis.

Why both ship together: a throughput plateau at 40% of the box is a client that
stopped scaling for some reason other than CPU, and a plateau at 95% is a box
that ran out. The capability chart cannot tell those apart and must not be read
without this one.

    python3 -m ftsbench.plot_loader_cpu --mode summary \\
        --data-dir data/loader-cap-2026-09-09/points \\
        --output results/loader-cpu-summary.png
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from typing import Any, Sequence

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402  (Agg must be set first)
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

from . import client_ceilings, loader_grid, plotlib, runmeta  # noqa: E402

CHART = "LOADER-CPU"
DEFAULT_OUTPUT = "results/loader-cpu.png"
TITLES = {
    "summary": "Loader capability: what it cost the harness box",
    "timeline": "Loader capability: harness CPU through a run",
}
SUBTITLES = {
    "summary": ("box cores used against total operations in flight — median "
                "tick solid, peak tick hollow"),
    "timeline": ("cores used against elapsed time, each arm at its best rung, "
                 "median repetition"),
}
CLAIM = ("A throughput plateau is an engine-side result only if the harness box "
         "still had CPU left; these figures say whether it did.")
METRIC_NAME = "box_cores_p50"
# From P0 on the fleet, against the null sink where the client is bound by
# construction. Used only to draw the line verify_generator judges against, and
# overridable because it is a measurement rather than a constant.
DEFAULT_CORE_BOUND = 0.85
Y_HEADROOM = 1.12
# generator_probe's first tick has no rate at all and its second still covers
# connect and prepare. client_ceilings drops both, and so must a timeline, or
# every facet opens with a dip that is an artifact of differencing.
WARM_IN_TICKS = client_ceilings.WARM_IN_TICKS
FACET_COLUMNS = 3


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=sorted(TITLES), required=True,
                        help="summary reads against the capability chart; "
                             "timeline shows ramp, steady state and tail")
    parser.add_argument("--data-dir", required=True,
                        help="the campaign's points directory")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="PNG path")
    parser.add_argument("--sidecar", default="",
                        help="sidecar JSON path (default: --output with .json)")
    parser.add_argument("--core-bound", type=float, default=DEFAULT_CORE_BOUND,
                        help="measured loader_core_bound_at, drawn as the "
                             "level above which verify_generator calls a point "
                             "client-bound")
    parser.add_argument("--title", default="")
    parser.add_argument("--subtitle", default="")
    parser.add_argument("--footer-extra", default="")
    parser.add_argument("--no-preliminary-stamp", dest="stamp",
                        action="store_false")
    parser.add_argument("--stamp-text", default=plotlib.PRELIMINARY_STAMP)
    parser.add_argument("--write-path-disclosure",
                        action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--width", type=float, default=11.0)
    parser.add_argument("--height", type=float, default=6.4)
    parser.add_argument("--dpi", type=int, default=160)
    args = parser.parse_args(argv)
    args.chart = CHART
    args.title = args.title or TITLES[args.mode]
    args.subtitle = args.subtitle or SUBTITLES[args.mode]
    return args


# --- summary ---------------------------------------------------------------

def draw_arm_summary(axes: Any, arm: loader_grid.Arm) -> None:
    """The median tick solid, the peak tick hollow above it.

    Peak as well as median because a build has a ramp and a tail: the median
    says what it sat at, the peak says what it reached, and a gate that reads
    one without the other either fires on a single spike or misses saturation
    entirely.
    """
    p50 = loader_grid.METRICS["box_cores_p50"]
    peak = loader_grid.METRICS["box_cores_max"]
    rungs = arm.drawable(p50)
    if not rungs:
        return
    style = arm.style
    axes.plot([rung.concurrency for rung in rungs],
              [rung.median(p50) for rung in rungs],
              color=style["color"], linestyle=style["linestyle"],
              linewidth=style["linewidth"], marker=style["marker"],
              markersize=7, zorder=3)
    peaks = [(rung.concurrency, rung.median(peak)) for rung in rungs]
    peaks = [(x, y) for x, y in peaks if y is not None]
    axes.plot([x for x, _ in peaks], [y for _, y in peaks],
              linestyle="none", marker=style["marker"], markersize=11,
              markerfacecolor="white", markeredgecolor=style["color"],
              markeredgewidth=1.6, zorder=4)


def reference_lines(axes: Any, arms: Sequence[loader_grid.Arm],
                    core_bound: float) -> None:
    cores = max((arm.cores_available for arm in arms), default=0)
    if not cores:
        return
    axes.axhline(cores, color=plotlib.ROLE_COLORS["total"], linestyle="-",
                 linewidth=1.4, alpha=0.8)
    axes.annotate(f"{cores} cores on the box", xy=(1.0, cores),
                  xycoords=("axes fraction", "data"), xytext=(-4, 5),
                  textcoords="offset points", ha="right", fontsize=8,
                  color=plotlib.ROLE_COLORS["total"])
    bound = core_bound * cores
    axes.axhline(bound, color=plotlib.ROLE_COLORS["total"], linestyle="--",
                 linewidth=1.2, alpha=0.7)
    axes.annotate(f"client-bound above {core_bound:.2f} x cores = {bound:.1f}",
                  xy=(1.0, bound), xycoords=("axes fraction", "data"),
                  xytext=(-4, 5), textcoords="offset points", ha="right",
                  fontsize=8, color=plotlib.ROLE_COLORS["total"])


def render_summary(args: argparse.Namespace,
                   arms: Sequence[loader_grid.Arm]) -> Figure:
    figure, axes = plt.subplots(figsize=(args.width, args.height))
    for arm in arms:
        draw_arm_summary(axes, arm)
    reference_lines(axes, arms, args.core_bound)
    ticks = sorted({rung.concurrency for arm in arms for rung in arm.rungs})
    axes.set_xscale("log", base=2)
    axes.set_xticks(ticks)
    axes.set_xticklabels([str(tick) for tick in ticks])
    cores = max((arm.cores_available for arm in arms), default=1)
    axes.set_ylim(0, cores * Y_HEADROOM)
    plotlib.frame(axes, args, "total operations in flight (split across N)",
                  "harness-box cores used")
    handles = loader_grid_legend(arms) + [
        Line2D([], [], color="#4a5568", marker="o", linestyle="none",
               markersize=10, markerfacecolor="white", markeredgewidth=1.6,
               label="peak tick (hollow)")]
    axes.legend(handles=handles, fontsize=8, loc="upper left", ncol=2,
                frameon=True, framealpha=0.9)
    return figure


def loader_grid_legend(arms: Sequence[loader_grid.Arm]) -> list[Line2D]:
    engines, workers, handles = [], [], []
    for arm in arms:
        if arm.engine not in engines:
            engines.append(arm.engine)
            handles.append(Line2D([], [], color=arm.style["color"],
                                  linewidth=2.6, label=arm.engine))
    for arm in arms:
        if arm.workers not in workers:
            workers.append(arm.workers)
            handles.append(Line2D([], [], color="#4a5568",
                                  linestyle=arm.style["linestyle"],
                                  linewidth=arm.style["linewidth"],
                                  marker=arm.style["marker"], markersize=6,
                                  label=f"N={arm.workers}"))
    return handles


# --- timeline --------------------------------------------------------------

def probe_series(arm: loader_grid.Arm) -> tuple[str, list[dict[str, Any]]]:
    """The gen-*.jsonl of the arm's best rung, median repetition.

    The name comes from `client_ceilings.PointKey.probe_name()` rather than
    being rebuilt here, so the campaign, the existing reducer and this chart
    cannot drift apart about what a probe artifact is called.
    """
    metric = loader_grid.METRICS["box_cores_p50"]
    rung = arm.best_rung(metric)
    point = rung.median_point(metric) if rung else None
    if point is None:
        return "", []
    path = os.path.join(arm.data_dir, point.key.probe_name())
    if not os.path.exists(path):
        return path, []
    _, records = runmeta.read_jsonl(path)
    return path, records


def ticks_of(records: Sequence[dict[str, Any]], name: str,
             ) -> list[dict[str, Any]]:
    return [record for record in records
            if record.get("record") == name
            and (record.get("i") or 0) >= WARM_IN_TICKS]


def loader_totals(records: Sequence[dict[str, Any]]
                  ) -> list[tuple[float, float]]:
    """Loader CPU summed within a tick, so N processes read as one figure —
    the same reduction `client_ceilings._tick_sums` performs."""
    by_tick: dict[int, list[float]] = {}
    elapsed: dict[int, float] = {}
    for record in ticks_of(records, "generator_sample"):
        cores = record.get("cpu_cores_used")
        if cores is None:
            continue
        by_tick.setdefault(record["i"], []).append(float(cores))
        elapsed[record["i"]] = float(record.get("t_elapsed_s") or 0.0)
    return [(elapsed[i], sum(values)) for i, values in sorted(by_tick.items())]


def box_totals(records: Sequence[dict[str, Any]], field: str
               ) -> list[tuple[float, float]]:
    return [(float(record.get("t_elapsed_s") or 0.0), float(record[field]))
            for record in ticks_of(records, "generator_box_sample")
            if record.get(field) is not None]


def draw_facet(axes: Any, arm: loader_grid.Arm,
               records: Sequence[dict[str, Any]], core_bound: float) -> None:
    loaders = loader_totals(records)
    box = box_totals(records, "cpu_cores_used")
    steal = box_totals(records, "steal_cores")
    style = arm.style
    if loaders:
        axes.plot([x for x, _ in loaders], [y for _, y in loaders],
                  color=style["color"], linewidth=2.2, label="loaders, summed")
    if box:
        axes.plot([x for x, _ in box], [y for _, y in box],
                  color=plotlib.ROLE_COLORS["total"], linewidth=1.4,
                  linestyle="--", label="whole box")
    cores = arm.cores_available
    if cores:
        axes.axhline(cores, color=plotlib.ROLE_COLORS["total"], linewidth=1.0,
                     alpha=0.6)
        axes.axhline(core_bound * cores, color=plotlib.ROLE_COLORS["total"],
                     linewidth=1.0, linestyle=":", alpha=0.6)
        axes.set_ylim(0, cores * Y_HEADROOM)
    if any(value for _, value in steal):
        axes.plot([x for x, _ in steal], [y for _, y in steal],
                  color="#97266d", linewidth=1.2, linestyle=":",
                  label="steal")
    rung = arm.best_rung(loader_grid.METRICS["box_cores_p50"])
    axes.set_title(f"{arm.name}  c={rung.concurrency if rung else '?'}",
                   fontsize=9, loc="left", color=style["color"])
    axes.grid(True, alpha=0.25, linewidth=0.6)
    axes.set_axisbelow(True)


def render_timeline(args: argparse.Namespace,
                    arms: Sequence[loader_grid.Arm],
                    series_of: dict[str, list[dict[str, Any]]]) -> Figure:
    rows = max(1, math.ceil(len(arms) / FACET_COLUMNS))
    figure, grid = plt.subplots(rows, FACET_COLUMNS, sharex=False, sharey=True,
                                figsize=(args.width, args.height))
    panels = list(grid.flat) if hasattr(grid, "flat") else [grid]
    for axes, arm in zip(panels, arms):
        draw_facet(axes, arm, series_of[arm.name], args.core_bound)
    for axes in panels[len(arms):]:
        axes.set_axis_off()
    for axes in panels[: len(arms)]:
        axes.set_xlabel("elapsed s", fontsize=8)
    panels[0].set_ylabel("cores used")
    panels[0].legend(fontsize=7, loc="lower right", frameon=True,
                     framealpha=0.9)
    plotlib.figure_title(figure, args)
    return figure


# --- shared ----------------------------------------------------------------

def footer_notes(arms: Sequence[loader_grid.Arm], mode: str,
                 core_bound: float) -> list[str]:
    metric = loader_grid.METRICS["box_cores_p50"]
    notes = [
        loader_grid.oversubscription_note(arms),
        loader_grid.flatness_note(arms, loader_grid.METRICS["docs_per_s"]),
        f"CPU source: generator_probe over the loaders' own cpuset — "
        f"/proc/<pid>/task/<tid>/stat utime+stime per process, per-core rows of "
        f"/proc/stat for the box. The first {WARM_IN_TICKS} ticks are dropped: "
        f"a rate is a difference, and the second still covers connect and "
        f"prepare.",
        f"loader_core_bound_at = {core_bound:.2f} of a core, measured in Phase 0 "
        f"against the null sink where the client is bound by construction. "
        f"Above it verify_generator calls a point client-bound.",
        loader_grid.SINK_NOTE,
    ]
    if mode == "summary":
        notes.insert(2, "A hollow marker is the rung's peak tick; the solid "
                        "line is its median. A plateau in the capability chart "
                        "with the box well under its cores is not a CPU "
                        "ceiling.")
    return [note for note in notes if note]


def sidecar_document(args: argparse.Namespace,
                     arms: Sequence[loader_grid.Arm],
                     series: Sequence[plotlib.ConfigSeries],
                     notes: Sequence[str]) -> dict[str, Any]:
    metric = loader_grid.METRICS[METRIC_NAME]
    return {
        "chart": CHART,
        "mode": args.mode,
        "title": args.title,
        "subtitle": args.subtitle,
        "png": args.output,
        "command": " ".join(sys.argv),
        "claim": CLAIM,
        "claim_status": plotlib.CLAIM_UNASSESSED,
        "preliminary": bool(args.stamp),
        "preliminary_stamp": args.stamp_text if args.stamp else "",
        "run_selection": plotlib.MEDIAN_RUN_SELECTION,
        "metric": METRIC_NAME,
        "loader_core_bound_at": args.core_bound,
        "chart_notes": list(notes),
        "arms": {arm.name: loader_grid.arm_sidecar(arm, metric)
                 for arm in arms},
        "configs": {config.name: plotlib.config_sidecar(config, METRIC_NAME)
                    for config in series},
    }


def figure_for(args: argparse.Namespace,
               arms: Sequence[loader_grid.Arm]) -> Figure:
    if args.mode == "summary":
        return render_summary(args, arms)
    series_of = {arm.name: probe_series(arm)[1] for arm in arms}
    empty = [name for name, records in series_of.items() if not records]
    if empty:
        print(f"warning: no probe series for {', '.join(empty)} — those facets "
              f"will be empty", file=sys.stderr)
    return render_timeline(args, arms, series_of)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    metric = loader_grid.METRICS[METRIC_NAME]
    arms = loader_grid.load_arms(args.data_dir)
    if not arms:
        print(f"error: no points in {args.data_dir}", file=sys.stderr)
        return 1
    series = [loader_grid.config_series(arm, metric) for arm in arms]
    notes = footer_notes(arms, args.mode, args.core_bound)
    plotlib.ensure_parent_dir(args.output)
    plotlib.finish_figure(figure_for(args, arms), args, series, notes)
    sidecar = plotlib.sidecar_path(args)
    plotlib.write_sidecar(sidecar,
                          sidecar_document(args, arms, series, notes))
    for line in loader_grid.summary_lines(arms, metric, "box_cores_p50"):
        print(line, file=sys.stderr)
    print(f"wrote {args.output} and {sidecar}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
