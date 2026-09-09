"""What the harness box can offer: docs/s against total offered concurrency.

Six lines — two engines by three loader-process counts — from the null-sink
campaign (`tools/loader_capability_campaign.sh`). The question is the one
`BUILD-RATE-MATRIX-PLAN.md` leaves open: `verify_generator` judges the build-rate
campaign's four-worker points against ceilings measured at N=1, and nothing says
what this box does at N > 4.

Four decisions make this chart able to refute its own premise as well as support
it:

- **The flatness is computed and stated, not left to the eye.** P0 measured this
  client flat in concurrency to within +-3% at N=1. An arm whose whole ladder
  range sits inside one rung's repetition spread gets named in the footer as
  carrying no signal on x. That sentence appearing is the finding.
- **Every repetition is drawn.** The median is bold, each repetition is a thin
  line of its own, and nothing is averaged: a curve whose error bars span it is
  visibly that.
- **A ceiling found at the top rung is drawn hollow**, because it was never
  bracketed and the rung above might have been higher.
- **Colour is the engine; N is line style, marker, width and a direct label.**
  Six hues do not fit — see `loader_grid` for the validator run that says so.

    python3 -m ftsbench.plot_loader_capability \\
        --data-dir data/loader-cap-2026-09-09/points \\
        --output results/loader-capability.png
"""
from __future__ import annotations

import argparse
import sys
from typing import Any, Sequence

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402  (Agg must be set first)
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

from . import loader_grid, plotlib  # noqa: E402

CHART = "LOADER-CAP"
DEFAULT_OUTPUT = "results/loader-capability.png"
DEFAULT_TITLE = "Loader capability: what the harness box can offer"
DEFAULT_SUBTITLE = ("documents/s against total operations in flight, per "
                    "loader-process count, against a null sink")
METRIC_NAME = "docs_per_s"
CLAIM = ("Adding loader processes buys offered throughput up to the point the "
         "harness box runs out of cores, and the campaign's pinned N=4 sits "
         "far enough below the engine ceilings to measure them.")
Y_HEADROOM = 1.12
LABEL_PAD = 1.02


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", required=True,
                        help="the campaign's points directory, holding "
                             "lat-<engine>-b<n>-c<n>-w<n>-r<n>-s<shard>.jsonl "
                             "and gen-<...>.jsonl")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="PNG path")
    parser.add_argument("--sidecar", default="",
                        help="sidecar JSON path (default: --output with .json)")
    parser.add_argument("--title", default=DEFAULT_TITLE)
    parser.add_argument("--subtitle", default=DEFAULT_SUBTITLE)
    parser.add_argument("--footer-extra", default="",
                        help="appended to the provenance footer")
    parser.add_argument("--no-preliminary-stamp", dest="stamp",
                        action="store_false",
                        help="drop the PRELIMINARY stamp; only legitimate for "
                             "a run on the harness box itself")
    parser.add_argument("--stamp-text", default=plotlib.PRELIMINARY_STAMP,
                        help="override the stamp wording for a run that is "
                             "preliminary for a different reason")
    parser.add_argument("--write-path-disclosure",
                        action=argparse.BooleanOptionalAction, default=False,
                        help="off by default: no engine was running, so there "
                             "is no base-table-plus-CDC asymmetry to disclose")
    parser.add_argument("--width", type=float, default=11.0)
    parser.add_argument("--height", type=float, default=6.4)
    parser.add_argument("--dpi", type=int, default=160)
    parser.set_defaults(chart=CHART)
    return parser.parse_args(argv)


def draw_repetitions(axes: Any, arm: loader_grid.Arm,
                     metric: loader_grid.Metric) -> None:
    """One thin line per repetition. Never averaged: a ladder whose repetitions
    cross each other is a ladder with no shape, and only drawing them says so."""
    style = arm.style
    for rep in arm.reps:
        pairs = [(rung.concurrency, rung.rep_of(rep, metric))
                 for rung in arm.rungs]
        pairs = [(x, y) for x, y in pairs if y is not None]
        if len(pairs) < 2:
            continue
        axes.plot([x for x, _ in pairs], [y for _, y in pairs],
                  color=style["color"], linestyle=style["linestyle"],
                  **loader_grid.THIN)


def draw_median(axes: Any, arm: loader_grid.Arm,
                metric: loader_grid.Metric) -> None:
    rungs = arm.drawable(metric)
    if not rungs:
        return
    style = arm.style
    x = [rung.concurrency for rung in rungs]
    medians = [rung.median(metric) or 0.0 for rung in rungs]
    lows = [median - rung.spread(metric)["min"]
            for median, rung in zip(medians, rungs)]
    highs = [rung.spread(metric)["max"] - median
             for median, rung in zip(medians, rungs)]
    axes.errorbar(x, medians, yerr=[lows, highs], capsize=4,
                  color=style["color"], linestyle=style["linestyle"],
                  linewidth=style["linewidth"], zorder=3)
    hollow = arm.best_rung(metric) if arm.is_lower_bound(metric) else None
    for rung, median in zip(rungs, medians):
        bound = rung is hollow
        axes.plot([rung.concurrency], [median], marker=style["marker"],
                  color=style["color"], zorder=4,
                  markersize=11 if bound else 7,
                  markerfacecolor="white" if bound else style["color"],
                  markeredgewidth=1.8 if bound else 1.0)


def label_median(axes: Any, arm: loader_grid.Arm, metric: loader_grid.Metric,
                 index: int) -> None:
    """The direct label, which is what makes N readable without the legend."""
    rungs = arm.drawable(metric)
    if not rungs:
        return
    last = rungs[-1]
    axes.annotate(f"N={arm.workers}",
                  xy=(last.concurrency, last.median(metric) or 0.0),
                  xytext=(6, 12 - 13 * (index % 3)), textcoords="offset points",
                  color=arm.style["color"], weight="bold", fontsize=9,
                  ha="left", va="center")


def legend_handles(arms: Sequence[loader_grid.Arm]) -> list[Line2D]:
    """Two legends in one: the hue says which engine, the stroke says which N.

    Both are needed because both encodings are load-bearing, and a chart with
    two or more series always carries a legend even when it also direct-labels.
    """
    engines, workers, handles = [], [], []
    for arm in arms:
        if arm.engine not in engines:
            engines.append(arm.engine)
            handles.append(Line2D([], [], color=arm.style["color"],
                                  linewidth=2.6,
                                  label=f"{arm.engine} (batch {arm.batch})"))
    for arm in arms:
        if arm.workers not in workers:
            workers.append(arm.workers)
            handles.append(Line2D([], [], color="#4a5568",
                                  linestyle=arm.style["linestyle"],
                                  linewidth=arm.style["linewidth"],
                                  marker=arm.style["marker"], markersize=6,
                                  label=f"N={arm.workers} loader processes"))
    return handles


def ticks_of(arms: Sequence[loader_grid.Arm]) -> list[int]:
    return sorted({rung.concurrency for arm in arms for rung in arm.rungs})


def render(args: argparse.Namespace, arms: Sequence[loader_grid.Arm],
           metric: loader_grid.Metric) -> Figure:
    figure, axes = plt.subplots(figsize=(args.width, args.height))
    for index, arm in enumerate(arms):
        draw_repetitions(axes, arm, metric)
        draw_median(axes, arm, metric)
        label_median(axes, arm, metric, index)
    ticks = ticks_of(arms)
    axes.set_xscale("log", base=2)
    axes.set_xticks(ticks)
    axes.set_xticklabels([str(tick) for tick in ticks])
    axes.set_ylim(0, top_of(arms, metric))
    plotlib.frame(axes, args, "total operations in flight (split across N)",
                  "documents/s offered")
    axes.legend(handles=legend_handles(arms), fontsize=8, loc="upper left",
                frameon=True, framealpha=0.9, ncol=2)
    return figure


def top_of(arms: Sequence[loader_grid.Arm],
           metric: loader_grid.Metric) -> float:
    highest = max((rung.spread(metric).get("max", 0.0)
                   for arm in arms for rung in arm.drawable(metric)),
                  default=1.0)
    return highest * Y_HEADROOM or 1.0


def footer_notes(arms: Sequence[loader_grid.Arm],
                 metric: loader_grid.Metric) -> list[str]:
    return [note for note in (
        loader_grid.flatness_note(arms, metric),
        loader_grid.oversubscription_note(arms),
        loader_grid.floor_note(arms),
        loader_grid.HOLLOW_NOTE,
        loader_grid.SINK_NOTE,
        "requests/s is in the sidecar, not here: one OpenSearch operation is a "
        "_bulk of many documents and one ScyllaDB operation is a single "
        "prepared INSERT, so the two are not the same unit.",
    ) if note]


def sidecar_document(args: argparse.Namespace,
                     arms: Sequence[loader_grid.Arm],
                     series: Sequence[plotlib.ConfigSeries],
                     metric: loader_grid.Metric,
                     notes: Sequence[str]) -> dict[str, Any]:
    return {
        "chart": CHART,
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
        "chart_notes": list(notes),
        "arms": {arm.name: loader_grid.arm_sidecar(arm, metric)
                 for arm in arms},
        "configs": {config.name: plotlib.config_sidecar(config, METRIC_NAME)
                    for config in series},
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    metric = loader_grid.METRICS[METRIC_NAME]
    arms = loader_grid.load_arms(args.data_dir)
    if not arms:
        print(f"error: no points in {args.data_dir} (expected "
              f"lat-<engine>-b<n>-c<n>-w<n>-r<n>-s<shard>.jsonl)",
              file=sys.stderr)
        return 1
    series = [loader_grid.config_series(arm, metric) for arm in arms]
    notes = footer_notes(arms, metric)
    plotlib.ensure_parent_dir(args.output)
    plotlib.finish_figure(render(args, arms, metric), args, series, notes)
    sidecar = plotlib.sidecar_path(args)
    plotlib.write_sidecar(sidecar, sidecar_document(args, arms, series,
                                                    metric, notes))
    for line in loader_grid.summary_lines(arms, metric, METRIC_NAME):
        print(line, file=sys.stderr)
    for note in notes[:1]:
        print(note, file=sys.stderr)
    print(f"wrote {args.output} and {sidecar}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
